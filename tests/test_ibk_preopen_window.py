"""개장 전 보존 창 — 표 부재를 실패가 아니라 보존으로 접는 경계.

IBK 정규 job 은 매분 :34 에 돈다. 08:00~08:34:59 에는 당일 조회기준일 화면에 날짜만 있고
표가 없는 것이 정상인데, 창 판정이 없으면 그 구간에서 매 실행이 AMBIGUOUS_TABLE_ABSENT
실패로 기록된다. 여기 시험은 그 경계가 **정확히 어디까지인지**를 잠근다.

실제 SQLite·CRUD 를 쓰고 HTTP·Redis·FCM·topic 부수효과만 막는다. 로컬 경계의 검증이며
실제 HTTP·운영 PostgreSQL 검증이 아니다.
"""

import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models
from app.crawlers import ibk
from app.ibk_result_protocol import IbkReason, IbkSource, IbkStatus
from app.ibk_run_context import SERVICE_DATE_ROLLOVER_TIME, IbkRunContext

KST = ibk.KST
RUN_ID = "d" * 32
RATES = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
FRI = datetime.date(2026, 8, 28)


def _context(hour, minute, second=0, *, day=28):
    """2026-08-28(금) 기준 KST 시각으로 실행 맥락을 만든다."""
    moment = KST.localize(datetime.datetime(2026, 8, day, hour, minute, second))
    return IbkRunContext(RUN_ID, moment.astimezone(datetime.timezone.utc))


class PreopenWindowBoundaryTest(unittest.TestCase):
    """창 판정 자체 — DB 없이 순수 판정만 본다."""

    def _judge(self, hour, minute, second=0, *, service_offset=0):
        ctx = _context(hour, minute, second)
        service = datetime.date.fromisoformat(ctx.expected_service_date)
        service += datetime.timedelta(days=service_offset)
        return ibk._is_preopen_pending_window(service, ctx.reference_time)

    def test_window_opens_exactly_at_the_rollover_boundary(self):
        self.assertFalse(self._judge(7, 59, 59), "07:59:59 는 아직 전 기준일이라 창 밖이다")
        self.assertTrue(self._judge(8, 0, 0), "08:00:00 부터 당일 기준일 개장 전이다")

    def test_window_closes_before_08_35(self):
        self.assertTrue(self._judge(8, 34, 59))
        self.assertFalse(self._judge(8, 35, 0), "08:35 부터는 표 부재를 계약 이상으로 본다")

    def test_daytime_and_night_are_outside_the_window(self):
        self.assertFalse(self._judge(12, 0))
        self.assertFalse(self._judge(23, 30))
        self.assertFalse(self._judge(3, 0), "야간은 전 기준일을 보므로 창이 아니다")

    def test_a_past_service_date_is_never_inside_the_window(self):
        """과거 기준일의 표 부재는 개장 전이 아니라 계약 이상이다."""
        self.assertTrue(self._judge(8, 10))
        self.assertFalse(self._judge(8, 10, service_offset=-1))

    def test_the_boundary_is_the_single_sourced_constant(self):
        """부모의 기준일 계산과 같은 상수를 쓰는지 잠근다 — 갈라지면 결과가 전량 거부된다."""
        self.assertIs(ibk.IBK_SERVICE_DATE_ROLLOVER_TIME, SERVICE_DATE_ROLLOVER_TIME)


class PreopenWindowResultTest(unittest.TestCase):
    """생성기 전체 — 표 부재가 어떤 결과로 접히는가."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.engine = create_engine(f"sqlite:///{self.path}")
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        for name, value in (("_write_changed_bank_rates_to_redis", []),
                            ("process_rate_alerts", 0),
                            ("_emit_fx_alert_canary", False),
                            ("_emit_fx_alert_shadow", None),
                            ("_emit_topic_triggers", None)):
            patcher = patch.object(crud, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _seed(self, when):
        with self.Session() as seed:
            for pair, rate in RATES.items():
                seed.add(models.BankExchangeRate(
                    bank=ibk.BANK_NAME, currency=pair, rate=rate,
                    timestamp=when.astimezone(datetime.timezone.utc).replace(tzinfo=None)))
            seed.commit()

    def _snapshot(self):
        """행 개수가 아니라 (통화, 값, 시각) 집합을 본다."""
        with self.Session() as fresh:
            return sorted(
                (row.currency, row.rate, row.timestamp)
                for row in fresh.query(models.BankExchangeRate).all()
            )

    @staticmethod
    def _preopen_then_no_session(reference_date):
        """당일은 표 부재(개장 전), 그 이전 날짜는 무고시 — 개장 전 창의 현실적 조합.

        ⛔ 모든 날짜를 무고시로 만들면 당일이 '표 부재' 가 아니게 되어 개장 전 상황
           자체가 사라진다. 그러면 이 시험이 검증하려던 것을 안 보게 된다.
        """
        def decide(query_date):
            if query_date == reference_date:
                raise ibk.IbkRateTableAbsentError("표 없음")
            return None
        return decide

    def _run(self, ctx, decide=None):
        """기본은 모든 날짜 표 부재. `decide` 로 과거 후보 존재를 주입할 수 있다.

        ⛔ lookback 도입 뒤로 "창 안 표 부재" 하나만으로는 결과가 정해지지 않는다 —
           그 다음 후보가 무엇을 주느냐가 최종 판정을 가른다.
        """
        def fetch(query_date, **_kw):
            if decide is not None:
                return decide(query_date)
            raise ibk.IbkRateTableAbsentError("표 없음")

        with patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch):
            return ibk.produce_ibk_dated_result(self.db, ctx, now=ctx.reference_time)

    def test_absent_table_inside_the_window_preserves(self):
        """행 개수만 보면 '같은 개수로 덮어썼다' 를 놓친다 — 값·시각과 쓰기 호출까지 본다."""
        ctx = _context(8, 10)
        self._seed(KST.localize(datetime.datetime(2026, 8, 27, 23, 0)))
        before = self._snapshot()

        with patch.object(crud, "insert_bank_rates_into_db") as write:
            result = self._run(ctx, decide=self._preopen_then_no_session(FRI))

        self.assertEqual(result.status, IbkStatus.PRESERVED)
        self.assertEqual(result.reason, IbkReason.PREOPEN_PENDING,
                         "창 안 표 부재가 최초 사유이므로 무고시가 이를 덮지 않는다")
        self.assertEqual(result.source, IbkSource.DB_SNAPSHOT)
        self.assertEqual(sorted(result.preserved_pairs), sorted(RATES))
        write.assert_not_called()
        self.assertEqual(self._snapshot(), before, "보존은 값도 시각도 바꾸지 않는다")

    def test_absent_table_outside_the_window_is_not_preserved(self):
        """창 밖 표 부재는 보존 승인이 아니다 — 기존 DB 가 완전하면 DEGRADED 로 남는다."""
        ctx = _context(12, 0)
        self._seed(KST.localize(datetime.datetime(2026, 8, 28, 9, 0)))

        result = self._run(ctx)

        self.assertEqual(result.status, IbkStatus.DEGRADED)
        self.assertEqual(result.reason, IbkReason.AMBIGUOUS_TABLE_ABSENT)
        self.assertNotEqual(result.reason, IbkReason.PREOPEN_PENDING)

    def test_absent_table_outside_the_window_with_empty_db_fails(self):
        """빈 DB 면 같은 표 부재가 FAILED 다 — 두 등급의 경계를 함께 잠근다."""
        result = self._run(_context(12, 0))

        self.assertEqual(result.status, IbkStatus.FAILED)
        self.assertEqual(result.reason, IbkReason.AMBIGUOUS_TABLE_ABSENT)

    def test_preopen_reason_is_not_overwritten_by_the_no_session_branch(self):
        """개장 전 보존이 무고시 보존으로 위조되면 두 상황을 구분할 수 없다."""
        result = self._run(_context(8, 10), decide=self._preopen_then_no_session(FRI))
        self.assertEqual(result.reason, IbkReason.PREOPEN_PENDING)
        self.assertNotEqual(result.reason, IbkReason.OFFICIAL_NO_SESSION)

    def test_preopen_with_a_failed_db_pre_read_ends_as_db_error(self):
        """보존 승인을 DB 사실 확보 전에 만들면 실패와 공존해 FAILURE_IS_EXCLUSIVE 로 터진다."""
        ctx = _context(8, 10)
        with patch.object(crud, "get_last_bank_rates_with_ts", side_effect=RuntimeError("DB down")):
            # ⛔ 입력을 바꾸면 이 시험이 조용히 무력해진다. 모든 날짜를 무고시로 만들면
            #    당일이 '표 부재' 가 아니게 되어 개장 전 승인 자체가 생기지 않고, 그러면
            #    "승인 + 실패 공존" 을 검사할 수 없다(상호 검토에서 결함 재주입으로 실증).
            result = self._run(ctx, decide=self._preopen_then_no_session(FRI))

        self.assertEqual(result.status, IbkStatus.FAILED)
        self.assertEqual(result.reason, IbkReason.DB_ERROR)
        self.assertNotEqual(result.reason, IbkReason.PREOPEN_PENDING,
                            "DB 를 못 읽었으면 보존을 승인한 것이 아니다")

    def test_preopen_pre_read_failure_survives_a_recovered_final_db_read(self):
        """최종 조회가 회복해도 사전 조회 실패는 DB_ERROR 로 남는다 — 같은 배타성 경로다.

        사전 조회와 최종 스냅샷이 **같은 crud 함수**를 쓰므로, 첫 호출만 실패시켜야
        '회복' 이 실제로 재현된다. 통째로 패치하면 두 조회가 함께 깨져 다른 사례가 된다.
        """
        ctx = _context(8, 10)
        self._seed(KST.localize(datetime.datetime(2026, 8, 27, 23, 0)))
        real = crud.get_last_bank_rates_with_ts
        calls = {"n": 0}

        def fail_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("DB down")
            return real(*args, **kwargs)

        with patch.object(crud, "get_last_bank_rates_with_ts", side_effect=fail_once):
            # 위와 같은 이유로 개장 전 입력을 유지한다.
            result = self._run(ctx, decide=self._preopen_then_no_session(FRI))

        self.assertGreaterEqual(calls["n"], 2, "최종 조회가 실제로 일어나야 이 사례가 성립한다")
        self.assertTrue(result.db_snapshot_complete, "최종 조회 자체는 성공했다")
        self.assertEqual(result.reason, IbkReason.DB_ERROR)
        self.assertNotEqual(result.reason, IbkReason.PREOPEN_PENDING,
                            "사전 조회를 못 했으면 보존을 승인한 것이 아니다")
        # 기존 DB 가 완전하므로 등급은 DEGRADED 다 — 빈 DB 였다면 FAILED 다(위 시험).
        self.assertEqual(result.status, IbkStatus.DEGRADED)

    def test_no_session_outside_the_window_still_preserves_as_no_session(self):
        """창 밖의 정상 무고시는 종전대로 OFFICIAL_NO_SESSION 이어야 한다 — 회귀 잠금."""
        ctx = _context(12, 0)
        self._seed(KST.localize(datetime.datetime(2026, 8, 28, 9, 0)))
        with patch.object(ibk, "_fetch_ibk_rates_for_date", return_value=None):
            result = ibk.produce_ibk_dated_result(self.db, ctx, now=ctx.reference_time)
        self.assertEqual(result.status, IbkStatus.PRESERVED)
        self.assertEqual(result.reason, IbkReason.OFFICIAL_NO_SESSION)


if __name__ == "__main__":
    unittest.main()
