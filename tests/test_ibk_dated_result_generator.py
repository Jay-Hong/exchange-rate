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
from app.ibk_selenium_strict import (
    ACCEPTED, NO_SESSION, REJECTED, UNAVAILABLE, IbkSeleniumStrictCapture,
)
from app.ibk_candidate_policy import CandidateSearchStop
from app.ibk_result_protocol import IbkProtocolError, IbkReason, IbkSource, IbkStatus
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
        # ⛔ 안전망이 **진짜 크롬을 띄우지 않게** 막는다. 막지 않으면 예산이 남는 사례마다
        #    드라이버 생성이 일어나 시험이 26초로 늘어난다(실측). 브라우저가 필요한 사례는
        #    각자 명시적으로 주입한다.
        driver_guard = patch.object(
            ibk, "selenium_driver_context",
            side_effect=AssertionError("시험에서 드라이버를 띄우면 안 된다"))
        driver_guard.start()
        self.addCleanup(driver_guard.stop)

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

    # ── 작업 기한 결속 ───────────────────────────────────────
    # ⛔ 생성기가 진입 시 12초 예산을 **새로 시작하면** spawn·import 지연이 공짜가 된다.
    #    부모가 넘긴 작업 기한과 묶여야 한다.

    def test_the_search_stops_at_the_parents_work_deadline(self):
        """부모 기한이 12초보다 빨리 오면 그 기한이 이긴다."""
        clock = [1000.0]
        context = IbkRunContext(RUN_ID, REFERENCE, clock[0] + 2.0)   # 2초만 남았다
        attempts = []

        def fetch(query_date, **kwargs):
            attempts.append(query_date)
            clock[0] += 1.5          # 요청마다 1.5초 소모
            raise ibk.IbkRateTableAbsentError("표 없음")

        with patch.object(ibk.time, "monotonic", lambda: clock[0]), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            result = ibk.produce_ibk_dated_result(self.db, context, now=NOW)

        self.assertLessEqual(len(attempts), 2, f"기한을 넘겨 계속 조회했다: {attempts}")
        self.assertIs(result.status, IbkStatus.FAILED)
        self.assertIs(result.reason, IbkReason.BUDGET_EXHAUSTED)

    def test_a_generous_work_deadline_leaves_the_http_budget_in_charge(self):
        """양성 대조 — 부모 기한이 넉넉하면 12초 몫이 그대로 판정한다."""
        clock = [1000.0]
        context = IbkRunContext(RUN_ID, REFERENCE, clock[0] + 200.0)
        attempts = []

        def fetch(query_date, **kwargs):
            attempts.append(query_date)
            clock[0] += 5.0
            raise ibk.IbkRateTableAbsentError("표 없음")

        with patch.object(ibk.time, "monotonic", lambda: clock[0]), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            ibk.produce_ibk_dated_result(self.db, context, now=NOW)

        self.assertEqual(len(attempts), 3, f"12초 안에 5초짜리 3회여야 한다: {attempts}")

    def test_a_request_is_not_started_when_too_little_time_remains(self):
        """⛔ 최소치를 timeout 의 하한으로 쓰면 잔여 0.1초에 0.5초짜리 요청을 걸어 기한을
        넘긴다. 최소치는 **시작 여부** 쪽에 두어야 상한이 언제나 잔여 이하다."""
        clock = [1000.0]
        context = IbkRunContext(RUN_ID, REFERENCE, clock[0] + 0.3)   # 최소치보다 적다
        attempts = []

        def fetch(query_date, **kwargs):
            attempts.append(kwargs.get("timeout"))
            raise ibk.IbkRateTableAbsentError("표 없음")

        with patch.object(ibk.time, "monotonic", lambda: clock[0]), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            result = ibk.produce_ibk_dated_result(self.db, context, timeout=10, now=NOW)

        self.assertEqual(attempts, [], "댈 수 없는 요청을 시작하면 안 된다")
        self.assertIs(result.reason, IbkReason.BUDGET_EXHAUSTED)

    def test_a_clock_that_moved_past_the_deadline_stops_instead_of_requesting(self):
        """⛔ 최소치를 상한의 **하한**으로 쓰면 기한이 27초 지난 뒤에도 0.5초짜리 요청을
        건다(실측). 그러면 "초과는 최대 0.5초" 가 거짓이 된다. 부족하면 멈춘다."""
        seq = iter([1001.0, 1002.0] + [1030.0] * 40)   # 시작 판정 뒤 28초 선점
        context = IbkRunContext(RUN_ID, REFERENCE, 1002.5)
        calls = []

        def fetch(query_date, *, reference_time, timeout):
            calls.append(timeout)
            return (dict(RATES), COMPLETED_AT)

        with patch.object(ibk.time, "monotonic", lambda: next(seq)), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            result = ibk.produce_ibk_dated_result(self.db, context, timeout=10, now=NOW)

        self.assertEqual(calls, [], "기한이 지났으면 호출하지 않는다")
        self.assertIs(result.reason, IbkReason.BUDGET_EXHAUSTED,
                      "기술 실패로 분류하면 안전망 진입 조건이 잘못 열린다")
        self.assertEqual(self._rows(), 0, "저장도 없어야 한다")

    def _search_outcome(self, context, seq, *, error=None, timeout=10):
        """실제 검색 함수를 감싸 **반환된 종료 계약**까지 관측한다."""
        captured, calls = {}, []
        real = ibk.search_candidates

        def spy(*args, **kwargs):
            captured["result"] = real(*args, **kwargs)
            return captured["result"]

        def fetch(query_date, *, reference_time, timeout):
            calls.append(query_date)
            raise (error or ibk.IbkRateTableAbsentError("표 없음"))

        clock = iter(seq)
        with patch.object(ibk.time, "monotonic", lambda: next(clock, seq[-1])), \
             patch.object(ibk, "search_candidates", spy), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            ibk.produce_ibk_dated_result(self.db, context, timeout=timeout, now=NOW)
        return captured["result"], calls

    def test_both_budget_paths_end_the_search_the_same_way(self):
        """⛔ 요청을 하지 않았는데 TECHNICAL_FAILURE 로 끝나면 실패 날짜가 남아, 안전망이
        "그 날짜가 기술적으로 실패했다" 로 오해한다(실측: fetch 0회인데 attempted=1 /
        failure_date 채워짐). 두 경합 경로가 같은 종료 계약이어야 한다."""
        cases = {
            "시작 검사에서 부족": (1000.3, [1000.0] * 40),
            "요청 직전 만료": (1002.5, [1001.0, 1002.0] + [1030.0] * 40),
        }
        for label, (deadline, seq) in cases.items():
            with self.subTest(case=label):
                self._reset_rates()
                context = IbkRunContext(RUN_ID, REFERENCE, deadline)
                search, calls = self._search_outcome(context, seq)
                self.assertEqual(calls, [], "요청하지 않았어야 한다")
                self.assertIs(search.stop, CandidateSearchStop.BUDGET_EXHAUSTED)
                self.assertEqual(search.attempted, 0, "요청하지 않은 후보를 세면 안 된다")
                self.assertIsNone(search.failure_date, "실패 날짜를 남기면 안 된다")

    def test_a_real_technical_failure_still_carries_its_date(self):
        """양성 대조 — 예산 처리가 진짜 기술 실패까지 삼키지 않는다."""
        import requests

        context = IbkRunContext(RUN_ID, REFERENCE, 1200.0)
        search, calls = self._search_outcome(
            context, [1000.0] * 40, error=requests.RequestException("net"))
        self.assertEqual(len(calls), 1, "실제로 한 번은 요청했다")
        self.assertIs(search.stop, CandidateSearchStop.TECHNICAL_FAILURE)
        self.assertEqual(search.attempted, 1)
        self.assertIsNotNone(search.failure_date, "안전망이 조회할 날짜가 필요하다")

    def test_earlier_observations_survive_a_budget_stop(self):
        """앞서 받은 무고시 사실은 예산 소진으로 사라지지 않는다."""
        clock = [1000.0]
        context = IbkRunContext(RUN_ID, REFERENCE, 1010.0)
        captured, seen = {}, []
        real = ibk.search_candidates

        def spy(*args, **kwargs):
            captured["result"] = real(*args, **kwargs)
            return captured["result"]

        def fetch(query_date, *, reference_time, timeout):
            seen.append(query_date)
            clock[0] += 9.6          # 첫 요청 뒤 예산이 바닥난다
            return None              # 무고시

        with patch.object(ibk.time, "monotonic", lambda: clock[0]), \
             patch.object(ibk, "search_candidates", spy), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            ibk.produce_ibk_dated_result(self.db, context, timeout=10, now=NOW)

        search = captured["result"]
        self.assertEqual(len(seen), 1)
        self.assertIs(search.stop, CandidateSearchStop.BUDGET_EXHAUSTED)
        self.assertTrue(search.saw_no_session, "받은 무고시 사실이 사라지면 안 된다")
        self.assertEqual(search.attempted, 1, "실제로 한 요청은 그대로 센다")

    def test_the_callers_timeout_is_never_enlarged(self):
        """⛔ 최소치를 하한으로 쓰면 호출자의 timeout=0.1 이 0.5 로 **늘어난다**(실측)."""
        clock = [1000.0]
        context = IbkRunContext(RUN_ID, REFERENCE, clock[0] + 10.0)
        timeouts = []

        def fetch(query_date, *, reference_time, timeout):
            timeouts.append(timeout)
            raise ibk.IbkRateTableAbsentError("표 없음")

        with patch.object(ibk.time, "monotonic", lambda: clock[0]), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            ibk.produce_ibk_dated_result(self.db, context, timeout=0.1, now=NOW)

        self.assertTrue(timeouts)
        for value in timeouts:
            self.assertLessEqual(value, 0.1, f"호출자 상한을 늘렸다: {timeouts}")

    def test_an_implausible_work_deadline_is_not_bypassed(self):
        """⛔ context.work_deadline 을 직접 읽으면 과대 기한 검증을 우회한다 — 실측에서
        잘못된 기한으로 10초짜리 요청 8회와 저장까지 진행됐다."""
        context = IbkRunContext(RUN_ID, REFERENCE, 1_000_000.0)
        calls = []

        def fetch(query_date, *, reference_time, timeout):
            calls.append(timeout)
            return (dict(RATES), COMPLETED_AT)

        with patch.object(ibk.time, "monotonic", lambda: 1000.0), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            with self.assertRaises(IbkProtocolError):
                ibk.produce_ibk_dated_result(self.db, context, timeout=10, now=NOW)

        self.assertEqual(calls, [], "검증 실패면 요청하지 않는다")
        self.assertEqual(self._rows(), 0, "저장도 없어야 한다")

    def test_the_timeout_handed_to_the_request_is_capped_by_the_remaining(self):
        """⛔ 이 시험이 잠그는 것은 **전달한 상한**이지 요청의 실제 수명이 아니다.
        requests 의 timeout 은 전체 응답의 벽시계 상한이 아니다."""
        clock = [1000.0]
        context = IbkRunContext(RUN_ID, REFERENCE, clock[0] + 0.6)
        timeouts = []

        def fetch(query_date, *, reference_time, timeout):
            timeouts.append(timeout)
            clock[0] += 1.0
            raise ibk.IbkRateTableAbsentError("표 없음")

        with patch.object(ibk.time, "monotonic", lambda: clock[0]), \
             patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=fetch), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=True):
            ibk.produce_ibk_dated_result(self.db, context, timeout=10, now=NOW)

        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], 0.6 + 1e-9)
        self.assertGreater(timeouts[0], 0)

    # ── 안전망 결과가 생성기 판정으로 이어지는가 ─────────────
    # ⛔ 매핑 헬퍼만 직접 부르면 "생성기를 통과한 결과" 는 보이지 않는다 — 변이가 그 창을
    #    지나갔다. 여기서는 실제 생성기를 돌려 최종 사유를 본다.

    def _run_with_net(self, capture, *, http_error):
        """HTTP 를 기술적으로 실패시키고 안전망 결과를 주입해 최종 판정을 본다."""
        with patch.object(ibk, "_fetch_ibk_rates_for_date", side_effect=http_error), \
             patch.object(ibk, "_selenium_safety_net", return_value=capture), \
             patch.object(ibk, "_is_preopen_pending_window", return_value=False):
            return ibk.produce_ibk_dated_result(self.db, self.context, now=NOW)

    def test_an_observed_selenium_failure_replaces_the_http_reason(self):
        """⛔ alert 차단이 HTTP 의 CONTRACT_ERROR 로 기록되면 원인이 사라진다(실측)."""
        import requests

        result = self._run_with_net(
            IbkSeleniumStrictCapture(UNAVAILABLE, reason="submit_blocked_by_alert"),
            http_error=ValueError("계약 오류"))          # HTTP → CONTRACT_ERROR
        self.assertIs(result.reason, IbkReason.TRANSPORT_ERROR,
                      "안전망이 돌면서 실패한 사유가 남아야 한다")

        result = self._run_with_net(
            IbkSeleniumStrictCapture(UNAVAILABLE, reason="input_absent"),
            http_error=requests.RequestException("전송 오류"))  # HTTP → TRANSPORT_ERROR
        self.assertIs(result.reason, IbkReason.CONTRACT_ERROR,
                      "요소 부재는 계약 이상이다")

    def test_a_net_that_never_ran_keeps_the_http_diagnosis(self):
        """돌지 못한 것은 우리 쪽 사정이다 — HTTP 가 낸 진단이 유일한 관측이다."""
        for detail in ("driver_failed:WebDriverException", "deadline_passed:after_driver"):
            with self.subTest(detail=detail):
                self._reset_rates()
                result = self._run_with_net(
                    IbkSeleniumStrictCapture(UNAVAILABLE, reason=detail),
                    http_error=ValueError("계약 오류"))
                self.assertIs(result.reason, IbkReason.CONTRACT_ERROR)

    def test_a_net_that_was_not_attempted_keeps_the_http_diagnosis(self):
        """⛔ None 은 "시도하지 않았다" 다. 실패를 만들지 않으면 결과가 통째로 어긋난다."""
        import requests

        result = self._run_with_net(None, http_error=requests.RequestException("전송"))
        self.assertIs(result.reason, IbkReason.TRANSPORT_ERROR)
        self.assertIs(result.status, IbkStatus.FAILED)

    def test_a_strict_rejection_is_its_own_reason(self):
        result = self._run_with_net(
            IbkSeleniumStrictCapture(REJECTED, reason="readback 불일치"),
            http_error=ValueError("계약 오류"))
        self.assertIs(result.reason, IbkReason.SELENIUM_STRICT_REJECTED)

    def test_a_usable_capture_is_saved_through_the_shared_path(self):
        """검증 통과분은 POST 와 같은 경로로 저장되고 source 만 다르다."""
        import requests

        capture = IbkSeleniumStrictCapture(
            ACCEPTED, service_date=SERVICE_DATE, rates=dict(RATES),
            completed_at=COMPLETED_AT)
        result = self._run_with_net(capture, http_error=requests.RequestException("전송"))
        self.assertIs(result.status, IbkStatus.OBSERVED)
        self.assertEqual(result.source, IbkSource.OFFICIAL_SELENIUM)
        self.assertEqual(self._rows(), len(RATES), "저장 경로는 POST 와 같다")

    def test_a_selenium_no_session_is_preserved_not_rejected(self):
        import requests

        result = self._run_with_net(
            IbkSeleniumStrictCapture(NO_SESSION, service_date=SERVICE_DATE),
            http_error=requests.RequestException("전송"))
        self.assertIsNot(result.reason, IbkReason.SELENIUM_STRICT_REJECTED)
        self.assertIn(result.status, (IbkStatus.PRESERVED, IbkStatus.FAILED))

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
