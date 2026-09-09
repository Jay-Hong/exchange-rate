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
    (실측: alert 차단이 CONTRACT_ERROR 로, 요소 부재가 TRANSPORT_ERROR 로 기록됐다).

    ⛔ 그리고 **확정하지 못한 것을 확정해서도 안 된다.** TRANSPORT_ERROR 는 브라우저와
       실제로 주고받다가 실패했을 때만, CONTRACT_ERROR 는 문서에 그 요소가 없다고 관측했을
       때만 쓴다. 나머지는 UNATTRIBUTED_ERROR 다.
    """

    CASES = {
        # 브라우저와 주고받은 **결과를 보고** 판정했다 — 관측된 사실이다.
        "submit_blocked_by_alert": "TRANSPORT_ERROR",
        "submit_not_confirmed": "TRANSPORT_ERROR",
        "alert_present": "TRANSPORT_ERROR",
        # 안전망 바깥에서 잡은 것. produce 는 이 사유에서 HTTP 진단을 유지하므로 이 분류에
        # 실제로 닿지 않는다.
        "driver_failed:WebDriverException": "TRANSPORT_ERROR",
        # ⛔ 조작 콜백 예외는 **어느 콜백에서 났는지만** 알려준다. 브라우저까지 갔는지는
        #    증명하지 않으므로 종류와 무관하게 출처 미확정이다.
        "submit_failed:WebDriverException": "UNATTRIBUTED_ERROR",
        "submit_failed:TypeError": "UNATTRIBUTED_ERROR",
        "alert_check:TypeError": "UNATTRIBUTED_ERROR",
        "confirm_failed:TimeoutException": "UNATTRIBUTED_ERROR",
        "confirm_failed:TypeError": "UNATTRIBUTED_ERROR",
        # 예산·신선도.
        "deadline_passed:after_driver": "BUDGET_EXHAUSTED",
        "page_too_old": "BUDGET_EXHAUSTED",
        "page_too_old_after_read": "BUDGET_EXHAUSTED",
        # 문서에 요소가 없다 — 이것도 관측된 사실이다.
        "input_absent": "CONTRACT_ERROR",
        "document_root_absent": "CONTRACT_ERROR",
        "find_input:NoSuchElementException": "CONTRACT_ERROR",
        "document_root:NoSuchElementException": "CONTRACT_ERROR",
        "served_date_unreadable:NoSuchElementException": "CONTRACT_ERROR",
        # 우리 쪽 시계·입력이 못 쓸 값이었다 — 은행 쪽 사실이 아니다.
        "clock_unusable": "UNATTRIBUTED_ERROR",
        "clock_went_backwards": "UNATTRIBUTED_ERROR",
        "page_loaded_at_unusable": "UNATTRIBUTED_ERROR",
        "document_ready_at_unusable": "UNATTRIBUTED_ERROR",
        # 읽기 — 실패는 종류가 버려지고, 시간 초과는 우리가 정한 상한 안에 결과가 없었다는
        # 사실만 증명한다.
        "page_source_failed": "UNATTRIBUTED_ERROR",
        "page_source_timeout": "UNATTRIBUTED_ERROR",
        "page_source:TypeError": "UNATTRIBUTED_ERROR",
        # 요소를 찾는 자리의 **다른** 예외는 출처를 모른다.
        "find_input:WebDriverException": "UNATTRIBUTED_ERROR",
        "document_root:TypeError": "UNATTRIBUTED_ERROR",
        "served_date_unreadable:RuntimeError": "UNATTRIBUTED_ERROR",
        # 파서가 낸 예상 밖 예외는 종류 이름만 남는다.
        "TypeError": "UNATTRIBUTED_ERROR",
    }

    def test_each_detail_folds_into_the_agreed_reason(self):
        for detail, expected in self.CASES.items():
            with self.subTest(detail=detail):
                self.assertEqual(ibk._selenium_unavailable_reason(detail).value, expected)

    def test_an_unknown_detail_is_not_attributed_to_the_document_or_the_transport(self):
        """⛔ 기본값이 CONTRACT_ERROR 이면 다음 미등록 사유에서 같은 문제가 재발한다 —
        모르는 것을 문서 탓으로 적게 된다."""
        for detail in ("something_new", None, "", "brand_new_reason:Whatever"):
            with self.subTest(detail=detail):
                self.assertEqual(ibk._selenium_unavailable_reason(detail).value,
                                 "UNATTRIBUTED_ERROR")

    def test_a_confirmed_reason_is_matched_exactly_not_by_prefix(self):
        """⛔ 확정 사유를 접두사로 맞추면 **그 이름으로 시작하는 새 사유를 조용히 삼킨다.**
        캡처러가 나중에 `input_absent_but_recovered` 같은 사유를 내면 우리가 검토한 적 없는
        상태가 "문서에 요소가 없다" 는 확정 관측으로 기록된다. 모르는 것은 중립이어야 한다."""
        for grown in ("input_absent_but_recovered", "alert_present_and_dismissed",
                      "clock_unusable_but_recovered", "page_source_failed_then_retried"):
            with self.subTest(detail=grown):
                self.assertEqual(ibk._selenium_unavailable_reason(grown).value,
                                 "UNATTRIBUTED_ERROR")
        # 양성 대조 — 정확히 같은 이름은 그대로 확정 사유다.
        self.assertEqual(ibk._selenium_unavailable_reason("input_absent").value,
                         "CONTRACT_ERROR")

    def test_the_lookup_labels_carry_a_colon_so_they_do_not_swallow_absences(self):
        """⛔ 조작 라벨은 `document_root:` 처럼 **콜론까지** 포함해야 한다. 콜론을 빼면
        `document_root_absent` 가 그 라벨에 걸리고, 뒤가 `NoSuchElementException` 이 아니므로
        요소 부재가 **중립 사유로 떨어진다** — 관측한 사실을 잃는다."""
        for label in ibk.IBK_SELENIUM_LOOKUP_LABELS:
            with self.subTest(label=label):
                self.assertTrue(label.endswith(":"), f"콜론이 빠졌다: {label}")
        self.assertEqual(ibk._selenium_unavailable_reason("document_root_absent").value,
                         "CONTRACT_ERROR")
        self.assertEqual(ibk._selenium_unavailable_reason("input_absent").value,
                         "CONTRACT_ERROR")


class TheSameExceptionIsNotSortedByWhereItFired(unittest.TestCase):
    """⛔ 문자열 매핑 시험만으로는 부족하다. **서명이 정상인 콜백 안에서** 예외를 내어
    실제 캡처러가 붙이는 사유를 받고, 그것이 어떻게 분류되는지 본다.

    ⛔ 같은 `TypeError` 인데 제출·경고 확인·교체 확인에서 나면 통신 오류가 되고 읽기에서
       나면 출처 미확정이 되던 것이 이 시험이 막는 것이다(실측). `_guarded` 가 알려주는 것은
       **어느 콜백에서 났는지**뿐이고, 브라우저 통신까지 갔는지는 증명하지 않는다.
    """

    REFERENCE = datetime.datetime(2026, 9, 9, 3, 0, tzinfo=datetime.timezone.utc)
    QUERY_DATE = datetime.date(2026, 9, 4)

    def _capturer(self, **overrides):
        from app.ibk_selenium_strict import IbkSeleniumStrictCapturer

        base = dict(
            parse=lambda html, *, query_date, reference_time: ({"usd-krw": 1.0}, "06:00:02"),
            served_date=lambda d: "2026.09.01", find_input=lambda d: MagicMock(),
            submit=lambda e, t: None, document_root=lambda d: object(),
            read_page_source=lambda d, *, timeout=None: ("<html/>", None),
            wait_replaced=lambda r, t: True, take_alert=lambda d: None,
            monotonic=lambda: 1000.0, sleep=lambda seconds: None)
        return IbkSeleniumStrictCapturer(**{**base, **overrides})

    def _capture(self, **overrides):
        return self._capturer(**overrides).capture(
            MagicMock(), query_date=self.QUERY_DATE, reference_time=self.REFERENCE,
            page_loaded_at=1000.0)

    def test_a_healthy_run_is_accepted(self):
        """양성 대조 — 아무것도 던지지 않으면 관측이 성립한다. 이게 없으면 아래 사례들이
        다른 이유로 실패해도 알 수 없다."""
        capture = self._capture()
        self.assertEqual(capture.verdict, "accepted", capture.reason)

    def test_an_internal_error_in_any_operation_is_unattributed(self):
        def boom(*args, **kwargs):
            raise TypeError("내부 오류")

        cases = {"제출": ("submit", "submit_failed:TypeError"),
                 "경고 확인": ("take_alert", "alert_check:TypeError"),
                 "교체 확인": ("wait_replaced", "confirm_failed:TypeError"),
                 "화면 읽기": ("read_page_source", "page_source:TypeError")}
        for label, (operation, expected_detail) in cases.items():
            with self.subTest(where=label):
                capture = self._capture(**{operation: boom})
                self.assertEqual(capture.reason, expected_detail)
                self.assertEqual(
                    ibk._selenium_unavailable_reason(capture.reason).value,
                    "UNATTRIBUTED_ERROR",
                    f"{label} 에서 난 같은 예외가 다른 분류가 됐다")

    def test_a_missing_element_is_still_a_contract_error(self):
        """⛔ 출처 미확정으로 옮기면서 **관측된 요소 부재까지** 중립으로 만들면 안 된다."""
        from selenium.common.exceptions import NoSuchElementException

        def absent(*args, **kwargs):
            raise NoSuchElementException("없다")

        capture = self._capture(find_input=absent)
        self.assertEqual(capture.reason, "find_input:NoSuchElementException")
        self.assertEqual(ibk._selenium_unavailable_reason(capture.reason).value,
                         "CONTRACT_ERROR")

    def test_a_confirmed_site_guard_is_still_a_transport_error(self):
        """⛔ 브라우저와 주고받은 **결과를 보고** 판정한 것은 그대로 남는다."""
        capture = self._capture(take_alert=lambda d: "새로고침은 연속으로 할 수 없습니다")
        self.assertEqual(capture.reason, "alert_present")
        self.assertEqual(ibk._selenium_unavailable_reason(capture.reason).value,
                         "TRANSPORT_ERROR")


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


class AWiringErrorFailsBeforeTheBrowserOpens(unittest.TestCase):
    """⛔ 배선 오류가 **관측 실패로 접히면** 원인이 사라진다. 안전망의 바깥 `except` 는
    무엇이든 `driver_failed:<종류>` 로 바꾸고, 그 사유는 `IBK_SELENIUM_NEVER_OBSERVED` 라
    HTTP 진단을 그대로 유지시킨다 — 은행 통신 장애처럼 보인다(실측).

    ⛔ 캡처러 생성은 `driver.get()` **뒤**라, 생성자 검사만으로는 이미 Chrome 을 띄우고
    은행 페이지를 조회한 뒤가 된다. 그래서 `opener()` 앞에서 본다.
    """

    def _harness(self):
        harness = Harness(IbkSeleniumStrictCapture(NO_SESSION))
        harness.clock[0] = 0.0
        return harness

    def test_an_old_contract_reader_raises_instead_of_being_observed(self):
        harness = self._harness()
        with patch.object(ibk, "_read_page_source_bounded", lambda driver: ("", None)):
            with self.assertRaises(ibk.IbkSeleniumWiringError) as caught:
                harness.run(deadline=1000.0, now=0.0)
        self.assertIn("read_page_source", str(caught.exception))

    def test_the_browser_is_not_opened_for_a_wiring_error(self):
        harness = self._harness()
        with patch.object(ibk, "_read_page_source_bounded", lambda driver: ("", None)):
            with self.assertRaises(ibk.IbkSeleniumWiringError):
                harness.run(deadline=1000.0, now=0.0)
        self.assertEqual(harness.opened, 0, "드라이버를 열었다")
        harness.driver.get.assert_not_called()

    def test_the_wiring_error_is_not_folded_into_a_driver_failure(self):
        """⛔ 이 시험이 없으면 사전 검사를 `try` 안으로 옮겨도 통과한다 — 그 자리에서는
        `driver_failed:IbkSeleniumWiringError` 라는 **관측 결과**가 되어 예외가 사라진다."""
        harness = self._harness()
        with patch.object(ibk, "_read_page_source_bounded", lambda driver: ("", None)):
            try:
                capture = harness.run(deadline=1000.0, now=0.0)
            except ibk.IbkSeleniumWiringError:
                return
        self.fail(f"예외가 관측으로 접혔다: verdict={capture.verdict} reason={capture.reason}")

    def test_a_short_budget_does_not_hide_the_wiring_error(self):
        """⛔ 예산 검사 뒤에 두면 예산이 모자란 회차에서 배선 오류가 숨는다 — 그 회차는
        `None`(시도하지 않았다)로 조용히 끝난다."""
        harness = self._harness()
        with patch.object(ibk, "_read_page_source_bounded", lambda driver: ("", None)):
            with self.assertRaises(ibk.IbkSeleniumWiringError):
                harness.run(deadline=1.0, now=0.0)      # 잔여 1초 — 진입 예산 미달
        self.assertEqual(harness.opened, 0)

    def test_the_real_wiring_passes_the_check(self):
        """양성 대조 — 운영이 실제로 주입하는 읽기 함수는 계약을 만족한다."""
        self.assertIsNone(
            ibk.read_page_source_contract_error(ibk._read_page_source_bounded))


class TheProductionCapturerCarriesTheReadCap(unittest.TestCase):
    """⛔ 상한을 정해 두고 **캡처러에 넘기지 않으면** 읽기가 잔여를 그대로 받는다. 잔여 30초
    에서 3초짜리 읽기가 30초를 쓸 수 있게 된다 — 배선 한 줄이 빠져도 다른 시험은 전부
    통과한다(변이로 실증). 그래서 주입한 가짜가 아니라 **운영이 만드는 캡처러**로 본다.
    """

    def _run(self, *, budget_seconds):
        import time as _time

        from selenium.common.exceptions import NoAlertPresentException

        recorded = []

        def recording_read(driver, *, timeout=None):
            recorded.append(timeout)
            return "<html></html>", None

        element = MagicMock()
        driver = MagicMock()
        driver.find_element.return_value = element
        # 서비스 날짜가 이미 맞으면 제출을 건너뛴다 — 이 시험이 보려는 것은 읽기 예산이다.
        driver.execute_script.return_value = QUERY_DATE.strftime("%Y.%m.%d")
        type(driver).switch_to = property(
            lambda self: (_ for _ in ()).throw(NoAlertPresentException()))

        with patch.object(ibk, "_read_page_source_bounded", recording_read):
            capturer = ibk._build_selenium_capturer(driver)
        started = _time.monotonic()
        capturer.capture(driver, query_date=QUERY_DATE, reference_time=REFERENCE,
                         page_loaded_at=started, document_ready_at=started,
                         deadline=started + budget_seconds)
        return recorded

    def test_a_wide_budget_keeps_the_modules_read_cap(self):
        recorded = self._run(budget_seconds=30.0)
        self.assertEqual(recorded, [ibk.SHADOW_PAGE_SOURCE_TIMEOUT],
                         f"운영 배선이 상한을 안 넘겼다: {recorded}")

    def test_a_tight_budget_wins_over_the_read_cap(self):
        recorded = self._run(budget_seconds=0.5)
        self.assertEqual(len(recorded), 1, f"읽기를 한 번 해야 한다: {recorded}")
        self.assertLessEqual(recorded[0], 0.5, f"잔여를 넘겼다: {recorded}")
        self.assertGreater(recorded[0], 0, f"음수·0 상한: {recorded}")


if __name__ == "__main__":
    unittest.main()
