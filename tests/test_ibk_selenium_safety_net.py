"""IBK 안전망 — **언제 열리고 무엇을 하지 않는가**.

⛔ 브라우저를 띄우지 않는다. 드라이버 컨텍스트와 캡처러를 주입해 흐름만 본다.
⛔ 이 슬라이스가 잠그는 것: 진입 조건, 새 항해 보장, 한 세션 한 번, 저장하지 않음.
   운영 연결(`IBK_RESULT_CRAWLER`)은 여전히 꺼져 있다.
"""

import contextlib
import datetime
import unittest
from unittest.mock import MagicMock, patch

from app.crawlers import ibk
from app.ibk_run_context import IbkRunContext
from app.ibk_selenium_strict import ACCEPTED, NO_SESSION, REJECTED, UNAVAILABLE
from app.ibk_selenium_strict import IbkSeleniumStrictCapture

RUN_ID = "n" * 32
REFERENCE = datetime.datetime(2026, 9, 9, 3, 0, tzinfo=datetime.timezone.utc)
QUERY_DATE = datetime.date(2026, 9, 4)


class Harness:
    def __init__(self, capture=None, *, get_raises=None, navigation_seconds=0.0,
                 creation_seconds=0.0):
        self.driver = MagicMock()
        self.clock = [0.0]
        self._navigation_seconds = navigation_seconds
        self._creation_seconds = creation_seconds
        if get_raises:
            self.driver.get.side_effect = get_raises
        else:
            def navigate(url):
                self.clock[0] += self._navigation_seconds   # 항해에 시간이 걸린다
            self.driver.get.side_effect = navigate
        self.opened = 0
        self.closed = 0
        self.captures = []
        self._capture = capture or IbkSeleniumStrictCapture(UNAVAILABLE, reason="x")

    @contextlib.contextmanager
    def driver_context(self):
        self.opened += 1
        self.clock[0] += self._creation_seconds     # 드라이버 생성도 예산을 쓴다
        try:
            yield self.driver
        finally:
            self.closed += 1

    def capturer_factory(self, driver):
        harness = self

        class Capturer:
            def capture(self, drv, *, query_date, reference_time, page_loaded_at,
                        document_ready_at=None, deadline=None):
                harness.captures.append(
                    {"query_date": query_date, "page_loaded_at": page_loaded_at,
                     "document_ready_at": document_ready_at, "deadline": deadline,
                     "reference_time": reference_time})
                return harness._capture

        return Capturer()

    def run(self, *, deadline=1000.0, now=900.0, query_date=QUERY_DATE):
        context = IbkRunContext(RUN_ID, REFERENCE, deadline)
        self.clock[0] = now
        with patch.object(ibk.time, "monotonic", lambda: self.clock[0]):
            return ibk._selenium_safety_net(
                context, query_date, deadline=deadline,
                driver_context=self.driver_context,
                capturer_factory=self.capturer_factory)


class TheNetOpensOnlyWhenItCanBeFunded(unittest.TestCase):
    def test_too_little_budget_creates_no_driver(self):
        harness = Harness()
        # 잔여 5초 < 진입 기준 12초
        self.assertIsNone(harness.run(deadline=1000.0, now=995.0))
        self.assertEqual(harness.opened, 0, "예산이 없으면 드라이버를 만들지 않는다")

    def test_exactly_the_entry_budget_is_enough(self):
        """양성 대조 — 거부가 정상 경계까지 삼키지 않는다."""
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        now = 1000.0 - ibk.IBK_SELENIUM_ENTRY_BUDGET_SECONDS
        self.assertIsNotNone(harness.run(deadline=1000.0, now=now))
        self.assertEqual(harness.opened, 1)

    def test_a_hair_under_the_entry_budget_is_refused(self):
        harness = Harness()
        now = 1000.0 - ibk.IBK_SELENIUM_ENTRY_BUDGET_SECONDS + 0.01
        self.assertIsNone(harness.run(deadline=1000.0, now=now))
        self.assertEqual(harness.opened, 0)

    def test_an_expired_deadline_creates_no_driver(self):
        harness = Harness()
        self.assertIsNone(harness.run(deadline=1000.0, now=1100.0))
        self.assertEqual(harness.opened, 0)


class TheNetNavigatesOnceThisRun(unittest.TestCase):
    def test_it_navigates_and_anchors_freshness_to_the_navigation_start(self):
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        harness.run(deadline=1000.0, now=900.0)
        harness.driver.get.assert_called_once_with(ibk.IBK_BANK_URL)
        self.assertEqual(len(harness.captures), 1, "한 세션에 한 번만 관측한다")
        self.assertEqual(harness.captures[0]["page_loaded_at"], 900.0,
                         "문서 나이의 기준은 항해 **시작** 시각이다")
        self.assertEqual(harness.captures[0]["query_date"], QUERY_DATE)

    def test_freshness_is_anchored_before_the_navigation_not_after(self):
        """⛔ `get()` 반환 시각을 쓰면 **로딩에 걸린 시간만큼 문서 나이를 작게** 센다.
        항해가 8초 걸렸다면 그 8초도 문서가 늙은 시간이다."""
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION), navigation_seconds=8.0)
        harness.run(deadline=1000.0, now=900.0)
        self.assertEqual(harness.captures[0]["page_loaded_at"], 900.0,
                         "항해 시작 시각이어야 한다 (완료 시각이면 908.0)")

    def test_a_rejection_does_not_navigate_again(self):
        harness = Harness(IbkSeleniumStrictCapture(REJECTED, reason="readback"))
        capture = harness.run()
        self.assertEqual(capture.verdict, REJECTED)
        self.assertEqual(harness.driver.get.call_count, 1)
        self.assertEqual(harness.opened, 1)

    def test_the_driver_is_always_released(self):
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        harness.run()
        self.assertEqual((harness.opened, harness.closed), (1, 1))

    def test_a_navigation_failure_is_an_attempt_that_failed_not_a_skip(self):
        """⛔ None 은 "시도하지 않았다" 로 예약한다. 항해 실패는 시도했고 실패한 것이다."""
        harness = Harness(get_raises=RuntimeError("PRIVATE_DRIVER_DETAIL"))
        capture = harness.run()
        self.assertIsNotNone(capture, "예산 미달과 구분돼야 한다")
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertTrue(capture.reason.startswith("driver_failed:"))
        self.assertNotIn("PRIVATE_DRIVER_DETAIL", str(capture))
        self.assertEqual(harness.closed, 1, "실패해도 세션은 닫는다")

    def test_the_guard_anchor_is_the_document_ready_time(self):
        """문서 나이는 항해 시작, 제출 가드는 문서 준비 — 기준이 다르다."""
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION), navigation_seconds=8.0)
        harness.run(deadline=1000.0, now=900.0)
        record = harness.captures[0]
        self.assertEqual(record["page_loaded_at"], 900.0, "나이는 항해 시작 기준")
        self.assertEqual(record["document_ready_at"], 908.0, "가드는 문서 준비 기준")

    def test_time_spent_creating_the_driver_is_consumed(self):
        """⛔ 진입 시 한 번만 보면 생성이 예산을 다 먹은 뒤에도 항해·관측을 시작한다
        (실측: 잔여 12초에서 생성에 20초를 쓰고도 get·capture 각 1회)."""
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION), creation_seconds=20.0)
        capture = harness.run(deadline=1000.0, now=988.0)
        self.assertEqual(harness.driver.get.call_count, 0, "만료 뒤 항해하지 않는다")
        self.assertEqual(harness.captures, [], "만료 뒤 관측하지 않는다")
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "deadline_passed:after_driver")

    def test_time_spent_navigating_is_consumed(self):
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION), navigation_seconds=20.0)
        capture = harness.run(deadline=1000.0, now=988.0)
        self.assertEqual(harness.driver.get.call_count, 1)
        self.assertEqual(harness.captures, [], "만료 뒤 관측하지 않는다")
        self.assertEqual(capture.reason, "deadline_passed:after_navigation")

    def test_the_capturer_receives_the_shared_deadline(self):
        """⛔ 기한을 안 넘기면 캡처러가 가드 대기 뒤 만료를 알 수 없어 제출·읽기를 계속한다."""
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        harness.run(deadline=1000.0, now=900.0)
        self.assertEqual(harness.captures[0]["deadline"], 1000.0)

    def test_time_spent_setting_driver_limits_is_consumed(self):
        """제한 설정도 시간을 먹는다 — 그 뒤에도 시작 여부를 다시 본다."""
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        harness.driver.set_page_load_timeout.side_effect = (
            lambda v: harness.clock.__setitem__(0, harness.clock[0] + 20.0))
        capture = harness.run(deadline=1000.0, now=988.0)
        self.assertEqual(harness.driver.get.call_count, 0, "만료 뒤 항해하지 않는다")
        self.assertEqual(capture.reason, "deadline_passed:after_limits")

    def test_the_driver_is_limited_to_the_remaining_budget(self):
        """기존 42초는 그 자체로 부모 45초를 넘지 않지만 앞선 HTTP·드라이버 생성과
        합치면 넘길 수 있다 — 남은 예산으로 낮춘다."""
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        harness.run(deadline=1000.0, now=970.0)
        for setter in ("set_page_load_timeout", "set_script_timeout"):
            with self.subTest(setter=setter):
                call = getattr(harness.driver, setter).call_args
                self.assertIsNotNone(call, f"{setter} 를 걸지 않았다")
                self.assertLessEqual(call[0][0], 30.0)


class TheNetNeverWrites(unittest.TestCase):
    FORBIDDEN_CALLS = ("insert_bank_rates_into_db", "commit", "_crawl_mibank_ibk")

    def test_the_safety_net_body_holds_no_write_or_mibank_call(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(ibk._selenium_safety_net))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                names.add(func.attr if isinstance(func, ast.Attribute)
                          else getattr(func, "id", ""))
        self.assertIn("capture", names, "양성 대조 — 호출을 실제로 수집했어야 한다")
        self.assertEqual(sorted(names & set(self.FORBIDDEN_CALLS)), [],
                         f"저장·MIBANK 호출이 생겼다: {names & set(self.FORBIDDEN_CALLS)}")

    def test_it_returns_an_observation_not_a_decision(self):
        harness = Harness(IbkSeleniumStrictCapture(
            ACCEPTED, service_date=QUERY_DATE, rates={"usd-krw": 1.0},
            completed_at="06:00:02"))
        capture = harness.run()
        self.assertTrue(capture.usable)
        # 저장 여부는 호출자가 정한다 — 이 값 자체는 저장 승인이 아니다.
        self.assertFalse(hasattr(capture, "written"))


class TheClockIsReadWhenCalled(unittest.TestCase):
    """⛔ `monotonic=time.monotonic` 을 기본 인자로 묶으면 정의 시점의 함수가 박혀 패치가
    듣지 않는다. 이 리포에서 두 번째 재발이라 시험으로 잠근다."""

    def test_patching_the_module_clock_changes_the_entry_decision(self):
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        self.assertIsNone(harness.run(deadline=1000.0, now=999.0))   # 잔여 1초
        self.assertEqual(harness.opened, 0)
        self.assertIsNotNone(harness.run(deadline=1000.0, now=900.0))  # 잔여 100초
        self.assertEqual(harness.opened, 1)


class UnavailableReasonsAreNotOverwritten(unittest.TestCase):
    """⛔ Selenium 이 관측하지 못한 사유를 HTTP 사유로 덮으면 원인이 사라진다
    (실측: alert 차단이 CONTRACT_ERROR 로, 요소 부재가 TRANSPORT_ERROR 로 기록됐다)."""

    CASES = {
        "submit_blocked_by_alert": "TRANSPORT_ERROR",
        "submit_not_confirmed": "TRANSPORT_ERROR",
        "alert_present": "TRANSPORT_ERROR",
        "confirm_failed:TimeoutException": "TRANSPORT_ERROR",
        "page_source_timeout": "TRANSPORT_ERROR",
        "driver_failed:WebDriverException": "TRANSPORT_ERROR",
        "clock_went_backwards": "TRANSPORT_ERROR",
        "deadline_passed:after_driver": "BUDGET_EXHAUSTED",
        "page_too_old": "BUDGET_EXHAUSTED",
        "input_absent": "CONTRACT_ERROR",
        "document_root_absent": "CONTRACT_ERROR",
        "served_date_unreadable:RuntimeError": "CONTRACT_ERROR",
    }

    def test_each_detail_folds_into_the_agreed_reason(self):
        for detail, expected in self.CASES.items():
            with self.subTest(detail=detail):
                self.assertEqual(ibk._selenium_unavailable_reason(detail).value, expected)

    def test_an_unknown_detail_is_a_contract_error_not_a_transport_one(self):
        self.assertEqual(ibk._selenium_unavailable_reason("something_new").value,
                         "CONTRACT_ERROR")
        self.assertEqual(ibk._selenium_unavailable_reason(None).value, "CONTRACT_ERROR")


class WhoseDiagnosisSurvives(unittest.TestCase):
    """안전망이 **돌지 못한 것**과 **돌면서 관측에 실패한 것**은 다르다.

    전자는 우리 쪽 사정이라 HTTP 진단(은행 응답에 대한 실제 관측)이 남아야 하고,
    후자는 안전망의 사유가 남아야 한다 — HTTP 사유로 덮으면 원인이 사라진다.
    """

    NEVER_OBSERVED = ("driver_failed:WebDriverException", "deadline_passed:after_driver")
    OBSERVED_BUT_FAILED = ("submit_blocked_by_alert", "submit_not_confirmed",
                           "input_absent", "page_source_timeout")

    def test_the_never_observed_details_are_recognised(self):
        for detail in self.NEVER_OBSERVED:
            with self.subTest(detail=detail):
                self.assertTrue(detail.startswith(ibk.IBK_SELENIUM_NEVER_OBSERVED),
                                "HTTP 진단을 남겨야 하는 사유다")

    def test_the_observed_failures_are_not_treated_as_never_observed(self):
        for detail in self.OBSERVED_BUT_FAILED:
            with self.subTest(detail=detail):
                self.assertFalse(detail.startswith(ibk.IBK_SELENIUM_NEVER_OBSERVED),
                                 "안전망의 사유가 남아야 하는 경우다")


class HistoricalPreservationMatchesThePostPath(unittest.TestCase):
    """⛔ 같은 상황에서 source 만 다른데 보존 사유가 갈리면 안 된다
    (실측: POST 는 PREOPEN_PENDING, Selenium 은 OFFICIAL_NO_SESSION)."""

    def _search(self, *, preopen):
        return MagicMock(saw_preopen_pending=preopen)

    def test_a_preopen_observation_is_preserved_as_preopen(self):
        self.assertEqual(
            ibk._historical_preservation_reason(self._search(preopen=True)).value,
            "PREOPEN_PENDING")

    def test_without_a_preopen_observation_it_is_no_session(self):
        self.assertEqual(
            ibk._historical_preservation_reason(self._search(preopen=False)).value,
            "OFFICIAL_NO_SESSION")


if __name__ == "__main__":
    unittest.main()
