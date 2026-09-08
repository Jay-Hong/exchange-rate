"""과거 후보 lookback 이 결과로 어떻게 접히는가 — 생성기 배선.

실제 SQLite·CRUD 를 쓰고 HTTP·Redis·FCM·topic 부수효과만 막는다. 여기서 잠그는 계약:
  · 역사 후보는 당일 OBSERVED 로 승격하지 않는다(부모의 기대 날짜가 흔들리면 안 된다)
  · 그래도 관측 메타데이터(조회일·완료시각)는 보존한다
  · 주말 skip 만으로 닿은 후보에 "무고시 응답을 받았다" 를 지어내지 않는다
  · 예산 소진과 horizon 정상 소진은 다른 종료다
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
from app.ibk_result_protocol import IbkReason, IbkStatus
from app.ibk_run_context import IbkRunContext

KST = ibk.KST
RUN_ID = "e" * 32
RATES = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
COMPLETED_AT = "11:30:00"   # 실행(12:10) 이전으로 풀린다 — 05:59:55 는 익일 미래다
PAYLOAD = (dict(RATES), COMPLETED_AT)
FRI = datetime.date(2026, 8, 28)
THU = datetime.date(2026, 8, 27)


class LookbackResultTest(unittest.TestCase):
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

    def _run(self, decide, *, day=28, hour=12, budget=True):
        moment = KST.localize(datetime.datetime(2026, 8, day, hour, 10))
        ctx = IbkRunContext(RUN_ID, moment.astimezone(datetime.timezone.utc))
        clock = [0.0] * 80 if budget else [0.0, 99.0] * 40
        with patch.object(ibk, "_fetch_ibk_rates_for_date",
                          side_effect=lambda query_date, **_kw: decide(query_date)), \
             patch.object(ibk.time, "monotonic", side_effect=clock):
            return ibk.produce_ibk_dated_result(self.db, ctx, now=ctx.reference_time)

    # ── 역사 후보 ────────────────────────────────────────────
    def test_a_past_candidate_is_preserved_not_promoted_to_observed(self):
        """당일 OBSERVED 로 올리면 부모의 기대 날짜와 어긋나 결과가 통째로 거부된다."""
        self._seed(KST.localize(datetime.datetime(2026, 8, 26, 23)))
        result = self._run(lambda day: PAYLOAD if day < FRI else None)

        self.assertIs(result.status, IbkStatus.PRESERVED)
        self.assertNotEqual(result.status, IbkStatus.OBSERVED)
        self.assertEqual(result.expected_service_date, FRI.isoformat(),
                         "기대 날짜는 부모 값 그대로여야 한다")
        self.assertEqual(result.observed_service_date, THU.isoformat(),
                         "실제 관측일은 따로 보존된다")

    def test_a_past_candidate_keeps_its_exact_completion_metadata(self):
        """isNotNone 으로는 '어느 날짜의 완료시각인지' 를 잠그지 못한다."""
        self._seed(KST.localize(datetime.datetime(2026, 8, 26, 23)))
        result = self._run(lambda day: PAYLOAD if day < FRI else None)

        expected = ibk._ibk_completion_kst(THU, COMPLETED_AT)
        self.assertEqual(result.official_completed_at, expected.isoformat(),
                         "과거 후보의 완료시각은 그 후보의 날짜로 풀려야 한다")

    def test_a_same_day_candidate_is_still_observed(self):
        """역사 후보 처리를 넣었다고 정상 당일 관측이 격하되면 안 된다 — 회귀 잠금."""
        self._seed(KST.localize(datetime.datetime(2026, 8, 26, 23)))
        result = self._run(lambda day: PAYLOAD)

        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertIs(result.reason, IbkReason.NORMAL)
        self.assertEqual(result.observed_service_date, result.expected_service_date)

    # ── 주말 skip 근거 ───────────────────────────────────────
    def test_a_weekend_reached_candidate_does_not_claim_a_no_session_response(self):
        """토요일 실행은 금요일 후보에 주말 skip 으로 닿는다 — 무고시 응답은 없었다."""
        self._seed(KST.localize(datetime.datetime(2026, 8, 26, 23)))
        result = self._run(lambda day: PAYLOAD, day=29)

        self.assertIs(result.status, IbkStatus.PRESERVED)
        self.assertIs(result.reason, IbkReason.OFFICIAL_NO_SESSION)
        self.assertEqual(result.observed_service_date, FRI.isoformat())

    # ── 종료 원인 구분 ───────────────────────────────────────
    def test_budget_exhaustion_is_its_own_reason(self):
        """허용 범위를 정상 소진한 것과 예산이 끊긴 것은 다른 사실이다."""
        self._seed(KST.localize(datetime.datetime(2026, 8, 27, 23)))
        result = self._run(lambda day: PAYLOAD, budget=False)

        self.assertIs(result.reason, IbkReason.BUDGET_EXHAUSTED)
        self.assertNotEqual(result.reason, IbkReason.OFFICIAL_NO_SESSION)
        self.assertIs(result.status, IbkStatus.DEGRADED, "기존 DB 가 완전하면 DEGRADED 다")

    def test_budget_exhaustion_with_an_empty_db_fails(self):
        result = self._run(lambda day: PAYLOAD, budget=False)
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.BUDGET_EXHAUSTED)

    def test_horizon_exhaustion_preserves_as_no_session(self):
        self._seed(KST.localize(datetime.datetime(2026, 8, 27, 23)))
        result = self._run(lambda day: None)

        self.assertIs(result.status, IbkStatus.PRESERVED)
        self.assertIs(result.reason, IbkReason.OFFICIAL_NO_SESSION)
        self.assertIsNone(result.observed_service_date)

    def test_a_technical_failure_does_not_walk_further_back(self):
        seen = []

        def fetch(day):
            seen.append(day)
            raise ibk.requests.RequestException("net")

        self._seed(KST.localize(datetime.datetime(2026, 8, 27, 23)))
        result = self._run(fetch)

        self.assertEqual(len(seen), 1, "기술 실패에서 과거로 더 가면 오래된 값을 쓰게 된다")
        self.assertIs(result.reason, IbkReason.TRANSPORT_ERROR)

    # ── 배타성·창 판정 회귀 (상호 검토에서 재현된 결함) ──────────
    def test_a_historical_candidate_with_unusable_db_values_does_not_break_exclusivity(self):
        """역사 후보의 보존 승인을 만든 뒤 실패로 전환하면 둘이 공존해 결과가 예외로 끝난다."""
        with self.Session() as seed:
            for pair, rate in (("usd-krw", 99.0),            # 범위 밖 → 유지 불가
                               ("jpy-krw", RATES["jpy-krw"]),
                               ("eur-krw", RATES["eur-krw"])):
                seed.add(models.BankExchangeRate(
                    bank=ibk.BANK_NAME, currency=pair, rate=rate,
                    timestamp=datetime.datetime(2026, 8, 29, 10)))
            seed.commit()

        result = self._run(lambda day: PAYLOAD if day < FRI else None)

        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.DB_ERROR)

    def test_a_past_table_absence_is_not_treated_as_pre_open(self):
        """개장 전 창을 기대 날짜로 고정하면 과거 표 부재까지 창 안으로 새어 나간다."""
        seen = []

        def fetch(day):
            seen.append(day)
            if day >= THU:
                raise ibk.IbkRateTableAbsentError("표 없음")
            return PAYLOAD

        result = self._run(fetch, hour=8)

        self.assertEqual(seen, [FRI, THU], "과거 표 부재에서 멈춰야 한다")
        self.assertIs(result.reason, IbkReason.AMBIGUOUS_TABLE_ABSENT)
        self.assertNotEqual(result.reason, IbkReason.PREOPEN_PENDING)

    # ── 회귀 가드 유지 ───────────────────────────────────────
    def test_the_regression_guard_still_runs_on_a_same_day_candidate(self):
        """역사 후보 판정을 넣었다고 **동일 날짜** 보호가 사라지면 안 된다.

        회귀는 "DB 저장시각이 후보의 고시완료시각보다 뒤" 로 판정하므로, 시각을 완료시각
        기준 오프셋으로 만든다. 달력 시각으로 짐작하면 완료시각이 익일로 풀리는 것을 놓친다.
        """
        completion = ibk._ibk_completion_kst(FRI, COMPLETED_AT)
        newer = completion + datetime.timedelta(
            seconds=ibk.IBK_DB_SAVE_LAG_TOLERANCE_SECONDS + 1)
        with self.Session() as seed:
            for pair in RATES:
                seed.add(models.BankExchangeRate(
                    bank=ibk.BANK_NAME, currency=pair, rate=RATES[pair] + 5,
                    timestamp=newer.astimezone(datetime.timezone.utc).replace(tzinfo=None)))
            seed.commit()

        result = self._run(lambda day: PAYLOAD)
        self.assertIs(result.reason, IbkReason.REGRESSION_GUARD)


if __name__ == "__main__":
    unittest.main()
