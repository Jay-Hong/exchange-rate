"""미연결 IBK 결과 생성기 — 실제 SQLite·CRUD로 공식 검증→저장→재조회→판정을 잇는다.

HTTP는 기존 fetch 함수를 대체해 차단하고, Redis/FCM/topic 부수효과도 막는다. 이 통과는
로컬 SQLite 경계의 검증이며 실제 HTTP·Selenium·운영 PostgreSQL·부모 경보 검증이 아니다.
"""

import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_write_runtime as awr
from app import crud, models
from app.crawlers import ibk
from app.ibk_result_protocol import IbkReason, IbkSource, IbkStatus
from app.ibk_run_context import IbkRunContext

KST = ibk.KST
RUN_ID = "a" * 32
REFERENCE = KST.localize(datetime.datetime(2026, 8, 28, 6, 5))
SERVICE_DATE = datetime.date(2026, 8, 27)
COMPLETED_AT = "05:59:55"
COMPLETION = ibk._ibk_completion_kst(SERVICE_DATE, COMPLETED_AT)
NOW = REFERENCE.astimezone(datetime.timezone.utc)
RATES = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
OTHER = {"usd-krw": 1383.5, "jpy-krw": 868.5, "eur-krw": 1611.5}
TOLERANCE = ibk.IBK_DB_SAVE_LAG_TOLERANCE_SECONDS


def _official_html(*, rows=True, selected_date="2026.08.27", completed_at=COMPLETED_AT):
    """실제 파서를 통과시키기 위한 최소 공식 응답. rows=False면 표에 행이 없다."""
    body = ""
    if rows:
        body = ("<thead><tr><th>통화</th><th>통화명</th><th>매매기준율</th><th>기타</th></tr></thead>"
                "<tbody>" + "".join(
                    f"<tr><th>{code}</th><th>{code} name</th><td>{rate}</td><td>0</td></tr>"
                    for code, rate in (("USD", RATES["usd-krw"]), ("JPY", RATES["jpy-krw"]),
                                       ("EUR", RATES["eur-krw"]))) + "</tbody>")
    return (f"<html><body><input id=\"inDate\" value=\"{selected_date}\">"
            f"<p class='standard'>고시완료 시각 : {completed_at}</p>"
            f"<table><caption>일반고시환율 표</caption>{body}</table></body></html>")


def _http_response(html):
    response = MagicMock()
    response.text = html
    response.raise_for_status.return_value = None
    return response


def _saved(offset_seconds):
    """후보 완료시각 기준 오프셋을 DB 저장 형식(UTC naive)으로 바꾼다."""
    moment = COMPLETION + datetime.timedelta(seconds=offset_seconds)
    return moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)


class DatedResultGeneratorTest(unittest.TestCase):
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
        self.context = IbkRunContext(RUN_ID, REFERENCE)
        self.assertEqual(self.context.expected_service_date, SERVICE_DATE.isoformat())

    def _seed(self, rates, saved_offset):
        with self.Session() as seed:
            for pair, rate in rates.items():
                seed.add(models.BankExchangeRate(bank=ibk.BANK_NAME, currency=pair,
                                                 rate=rate, timestamp=_saved(saved_offset)))
            seed.commit()

    def _reset_rates(self):
        """사례마다 DB 상태를 독립시킨다. 이전 행이 다음 판정에 남지 않게 한다."""
        with self.Session() as fresh:
            fresh.query(models.BankExchangeRate).delete()
            fresh.commit()

    def _rows(self):
        with self.Session() as fresh:
            return fresh.query(models.BankExchangeRate).count()

    def _run(self, fetch_result=None, fetch_error=None):
        kwargs = {"side_effect": fetch_error} if fetch_error else {"return_value": fetch_result}
        with patch.object(ibk, "_fetch_ibk_rates_for_date", **kwargs):
            return ibk.produce_ibk_dated_result(self.db, self.context, now=NOW)

    # ── 정상 관측 ────────────────────────────────────────────
    def test_fresh_write_is_observed(self):
        result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertIs(result.reason, IbkReason.NORMAL)
        self.assertEqual(result.changed_count, 3)
        self.assertIs(result.source, IbkSource.OFFICIAL_POST)
        self.assertEqual(result.official_completed_at, COMPLETION.isoformat())
        self.assertEqual(result.observed_service_date, SERVICE_DATE.isoformat())

    def test_unchanged_complete_db_is_observed_with_zero_changes(self):
        self._seed(RATES, -3600)
        result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertEqual(result.changed_count, 0)
        self.assertEqual(self._rows(), 3)          # 새 행이 생기지 않았다

    # ── 쓰기 차단 ────────────────────────────────────────────
    def test_write_block_with_complete_db_is_degraded(self):
        self._seed(OTHER, -3600)
        awr._reset_for_test()
        result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.WRITE_POLICY_BLOCKED)
        self.assertEqual(self._rows(), 3)          # 한 줄도 추가되지 않았다

    def test_write_block_with_empty_db_is_failed(self):
        awr._reset_for_test()
        result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.WRITE_POLICY_BLOCKED)
        self.assertEqual(self._rows(), 0)

    # ── 회귀 제외 ────────────────────────────────────────────
    def test_partial_regression_keeps_newer_pairs(self):
        self._seed({"usd-krw": OTHER["usd-krw"], "jpy-krw": OTHER["jpy-krw"]}, TOLERANCE + 1)
        self._seed({"eur-krw": OTHER["eur-krw"]}, -1)
        result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.REGRESSION_GUARD)
        self.assertEqual(result.changed_count, 1)   # eur 만 저장됐다
        with self.Session() as fresh:
            rows = crud.get_last_bank_rates_with_ts(fresh, ibk.BANK_NAME,
                                                    list(ibk.MIBANK_REQUIRED_PAIRS))
        self.assertEqual(rows["usd-krw"]["rate"], OTHER["usd-krw"])   # 유지됨
        self.assertEqual(rows["eur-krw"]["rate"], RATES["eur-krw"])   # 보충됨

    def test_full_regression_writes_nothing(self):
        self._seed(OTHER, TOLERANCE + 1)
        with patch.object(crud, "insert_bank_rates_into_db") as mock_insert:
            result = self._run((dict(RATES), COMPLETED_AT))
        mock_insert.assert_not_called()
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.REGRESSION_GUARD)
        self.assertIsNone(result.changed_count)

    def test_retained_value_changed_after_decision_is_detected(self):
        self._seed({"usd-krw": OTHER["usd-krw"], "jpy-krw": OTHER["jpy-krw"]}, TOLERANCE + 1)
        self._seed({"eur-krw": OTHER["eur-krw"]}, -1)
        real_insert = crud.insert_bank_rates_into_db

        def insert_then_drift(*args, **kwargs):
            count = real_insert(*args, **kwargs)
            with self.Session() as other:      # 다른 실행이 유지 대상 통화를 바꾼다
                other.add(models.BankExchangeRate(bank=ibk.BANK_NAME, currency="usd-krw",
                                                  rate=1399.0, timestamp=_saved(TOLERANCE + 60)))
                other.commit()
            return count

        with patch.object(crud, "insert_bank_rates_into_db", side_effect=insert_then_drift):
            result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.DB_APPLY_MISMATCH)

    # ── 무고시 보존 ──────────────────────────────────────────
    def test_official_no_session_with_complete_db_is_preserved(self):
        self._seed(OTHER, -3600)
        result = self._run(None)
        self.assertIs(result.status, IbkStatus.PRESERVED)
        self.assertIs(result.reason, IbkReason.OFFICIAL_NO_SESSION)
        self.assertIs(result.source, IbkSource.DB_SNAPSHOT)
        self.assertIsNone(result.official_completed_at)

    def test_official_no_session_with_partial_db_is_failed(self):
        self._seed({"usd-krw": OTHER["usd-krw"]}, -3600)
        result = self._run(None)
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.OFFICIAL_NO_SESSION)
        self.assertEqual(result.missing_pairs, ("eur-krw", "jpy-krw"))

    # ── DB 예외 ──────────────────────────────────────────────
    def test_pre_read_exception_does_not_write(self):
        self._seed(OTHER, -3600)
        with patch.object(crud, "get_last_bank_rates_with_ts",
                          side_effect=RuntimeError("db down")), \
             patch.object(crud, "insert_bank_rates_into_db") as mock_insert:
            with patch.object(ibk, "_fetch_ibk_rates_for_date",
                              return_value=(dict(RATES), COMPLETED_AT)):
                result = ibk.produce_ibk_dated_result(self.db, self.context, now=NOW)
        mock_insert.assert_not_called()
        self.assertIs(result.reason, IbkReason.DB_ERROR)
        self.assertIs(result.status, IbkStatus.FAILED)   # 재조회도 같은 예외로 실패

    def test_pre_read_exception_alone_is_degraded_when_db_is_fine(self):
        # 사전 조회만 실패하고 최종 재조회는 성공하는 축을 분리한다.
        self._seed(OTHER, -3600)
        with self.Session() as fresh:
            healthy = crud.get_last_bank_rates_with_ts(fresh, ibk.BANK_NAME,
                                                       list(ibk.MIBANK_REQUIRED_PAIRS))
        with patch.object(crud, "get_last_bank_rates_with_ts",
                          side_effect=[RuntimeError("db down"), healthy]), \
             patch.object(crud, "insert_bank_rates_into_db") as mock_insert, \
             patch.object(ibk, "_fetch_ibk_rates_for_date",
                          return_value=(dict(RATES), COMPLETED_AT)):
            result = ibk.produce_ibk_dated_result(self.db, self.context, now=NOW)
        mock_insert.assert_not_called()
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.DB_ERROR)
        self.assertIs(result.db_snapshot_complete, True)

    def test_commit_failure_is_reported_as_db_error(self):
        self._seed(OTHER, -3600)
        with patch.object(crud, "insert_bank_rates_into_db",
                          side_effect=RuntimeError("commit failed")):
            result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.DB_ERROR)
        self.assertIsNone(result.changed_count)

    # ── 수집 실패 분류 ───────────────────────────────────────
    def test_fetch_failures_map_to_distinct_reasons(self):
        self._seed(OTHER, -3600)
        cases = ((ibk.IbkRateTableAbsentError("표 없음"), IbkReason.AMBIGUOUS_TABLE_ABSENT),
                 (requests.RequestException("net"), IbkReason.TRANSPORT_ERROR),
                 (ValueError("readback 불일치"), IbkReason.CONTRACT_ERROR))
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                result = self._run(fetch_error=error)
                self.assertIs(result.status, IbkStatus.DEGRADED)
                self.assertIs(result.reason, expected)

    # ── 실제 파서를 통과하는 경계 ────────────────────────────
    def _run_over_http(self, html):
        with patch.object(ibk.requests, "post", return_value=_http_response(html)):
            return ibk.produce_ibk_dated_result(self.db, self.context, now=NOW)

    def test_real_parser_valid_response_is_observed(self):
        result = self._run_over_http(_official_html())
        self.assertIs(result.status, IbkStatus.OBSERVED)   # 대조군: 파서를 실제로 통과한다
        self.assertEqual(result.changed_count, 3)

    def test_real_parser_table_without_rows_ends_as_contract_error(self):
        self._seed(OTHER, -3600)
        with patch.object(crud, "insert_bank_rates_into_db") as mock_insert:
            result = self._run_over_http(_official_html(rows=False))
        mock_insert.assert_not_called()
        self.assertIs(result.status, IbkStatus.DEGRADED)
        self.assertIs(result.reason, IbkReason.CONTRACT_ERROR)

    def test_parser_rejects_missing_header_row_explicitly(self):
        with self.assertRaises(ValueError):
            ibk._parse_ibk_official_response(
                _http_response(_official_html(rows=False)),
                query_date=SERVICE_DATE,
                reference_time=REFERENCE,
            )

    # ── 사용 불가한 보존 대상 ────────────────────────────────
    def test_unusable_retained_values_end_as_db_error(self):
        # usd·jpy 는 회귀로 유지 대상, eur 는 보충 대상이 되도록 저장시각을 갈라 둔다.
        # 유한값 대조군에서는 eur 저장이 실제로 일어나므로, 저장 0회가 의미를 갖는다.
        original_insert = crud.insert_bank_rates_into_db
        cases = (
            ("유한값(대조군)", OTHER["usd-krw"], IbkStatus.DEGRADED, IbkReason.REGRESSION_GUARD, 1),
            ("양의 무한대", float("inf"), IbkStatus.FAILED, IbkReason.DB_ERROR, 0),
            ("음의 무한대", float("-inf"), IbkStatus.FAILED, IbkReason.DB_ERROR, 0),
            ("범위 밖", 99.0, IbkStatus.FAILED, IbkReason.DB_ERROR, 0),
        )
        for label, usd, status, reason, writes in cases:
            with self.subTest(case=label):
                self._reset_rates()
                self._seed({"usd-krw": usd, "jpy-krw": OTHER["jpy-krw"]}, TOLERANCE + 1)
                self._seed({"eur-krw": OTHER["eur-krw"]}, -1)
                with patch.object(crud, "insert_bank_rates_into_db",
                                  side_effect=original_insert) as spy:
                    result = self._run((dict(RATES), COMPLETED_AT))
                self.assertIs(result.status, status)
                self.assertIs(result.reason, reason)
                self.assertEqual(spy.call_count, writes)
                if writes:
                    self.assertEqual(result.changed_count, 1)   # eur 만 보충됐다

    def test_partial_db_bootstraps_missing_pairs_and_keeps_existing(self):
        self._seed({"usd-krw": RATES["usd-krw"]}, -3600)     # 기존 1통화가 공식값과 같다
        result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertEqual(result.changed_count, 2)            # 나머지 2통화만 보충
        with self.Session() as fresh:
            rows = crud.get_last_bank_rates_with_ts(fresh, ibk.BANK_NAME,
                                                    list(ibk.MIBANK_REQUIRED_PAIRS))
            total = fresh.query(models.BankExchangeRate).count()
        self.assertEqual(total, 3)                           # 기존 1 + 신규 2, 중복 없음
        self.assertEqual({pair: rows[pair]["rate"] for pair in rows}, RATES)

    def test_empty_db_bootstrap_is_not_blocked_by_the_usability_check(self):
        result = self._run((dict(RATES), COMPLETED_AT))     # 빈 DB 보충은 계속 허용된다
        self.assertIs(result.status, IbkStatus.OBSERVED)

    # ── 실제 CRUD 를 돌리며 commit 만 실패시키는 경계 ────────
    def test_real_crud_commit_failure_rolls_back_and_preserves_rows(self):
        self._seed(OTHER, -3600)
        original_rollback = self.db.rollback
        with patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")), \
             patch.object(self.db, "rollback", side_effect=original_rollback) as mock_rollback:
            result = self._run((dict(RATES), COMPLETED_AT))
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.DB_ERROR)
        self.assertIsNone(result.changed_count)
        self.assertEqual(mock_rollback.call_count, 1)
        self.assertEqual(self._rows(), 3)        # 별 연결에서 기존 행 3개 유지
