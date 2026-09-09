"""IBK strict 캡처의 **실제 WebDriver 조작**. 계약만 잠근다.

⛔ 브라우저를 띄우지 않는다. 여기서 보는 것은 "무엇을 어떻게 묻는가" — 어떤 스크립트를
   실행하고 어떤 대상에 대기를 거는가 — 이지 크롬의 실제 반응이 아니다.
   실제 브라우저 확인은 별도로 수행했고 그 결과를 아래 주석에 적는다.

2026-09-09 로컬 크롬(headless) + 로컬 HTML 실측:
  - 로드 직후 `served_date` = 2026.09.09
  - `send_keys("2026.09.04")` 후 `served_date` = **2026.09.09** (내용 속성이라 불변)
    같은 시점 `element.get_attribute("value")` = 2026.09.04 (property 라 변함)
  - 실제 제출 후 `wait_replaced` = True, `served_date` = 2026.09.04
  - 제출 없이 property 만 바꾼 뒤 `wait_replaced` = **False** (1.03초 대기 후)
"""

import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import NoAlertPresentException, TimeoutException

from app import ibk_selenium_adapter as adapter


class TheServedDateComesFromTheContentAttribute(unittest.TestCase):
    """⛔ `get_attribute("value")` 는 input 에서 property 를 우선 반환한다 — 입력만 되고
    제출되지 않은 값이 나온다. 파서가 읽을 값은 내용 속성이다."""

    def test_it_asks_the_document_not_the_element_property(self):
        driver = MagicMock()
        node = object()
        driver.find_element.return_value = node
        driver.execute_script.return_value = "2026.09.09"

        self.assertEqual(adapter.make_served_date("#inDate")(driver), "2026.09.09")
        driver.execute_script.assert_called_once_with(adapter.SERVED_DATE_SCRIPT, node)
        self.assertIn("getAttribute", adapter.SERVED_DATE_SCRIPT)

    def test_the_script_reads_the_attribute_not_the_property(self):
        # `arguments[0].value` 였다면 property 를 읽어 같은 결함으로 돌아간다.
        self.assertNotIn(".value", adapter.SERVED_DATE_SCRIPT)


class TheConfirmationWatchesTheDocumentRoot(unittest.TestCase):
    def test_the_root_is_the_html_element(self):
        driver = MagicMock()
        adapter.document_root(driver)
        args = driver.find_element.call_args[0]
        self.assertEqual(args[1], adapter.DOCUMENT_ROOT_SELECTOR)
        self.assertEqual(adapter.DOCUMENT_ROOT_SELECTOR, "html",
                         "입력 요소를 보면 부분 갱신을 문서 교체로 오독한다")


class TheTwoWaitsShareOneDeadline(unittest.TestCase):
    """⛔ 각자 상한을 쓰면 예산이 두 배가 된다."""

    def _wait(self, driver, clock, timeout=10.0):
        return adapter.make_wait_replaced(driver, monotonic=lambda: clock[0])(
            object(), timeout)

    def _run_with_waits(self, clock, spent, timeout=10.0):
        given = []

        class Wait:
            def __init__(self, driver, wait_timeout):
                given.append(wait_timeout)

            def until(self, condition):
                clock[0] += spent[min(len(given) - 1, len(spent) - 1)]
                return True

        with patch.object(adapter, "WebDriverWait", Wait):
            return self._wait(MagicMock(), clock, timeout=timeout), given

    def test_the_second_wait_gets_only_what_the_first_left(self):
        """양성 대조는 **기한 안**이어야 한다 — 두 대기 합이 예산을 넘으면 그 자체가 거부다."""
        clock = [100.0]
        ok, given = self._run_with_waits(clock, [3.0, 2.0], timeout=10.0)
        self.assertTrue(ok)
        self.assertEqual(len(given), 2)
        self.assertAlmostEqual(given[0], 10.0)
        self.assertAlmostEqual(given[1], 7.0, msg=f"두 번째가 잔여를 받아야 한다: {given}")
        self.assertLessEqual(clock[0], 110.0)

    def test_a_success_that_arrived_after_the_deadline_is_refused(self):
        """⛔ `WebDriverWait.until` 은 조건이 참이면 **시간 초과 검사 전에** 돌아올 수 있다.
        기한 110 에 111 에서 True 가 나왔다(실측). 대기 뒤 기한을 다시 본다."""
        clock = [100.0]
        ok, _ = self._run_with_waits(clock, [6.0, 5.5], timeout=10.0)
        self.assertGreater(clock[0], 110.0, "전제 — 기한을 넘겨 끝났어야 한다")
        self.assertFalse(ok)

    def test_no_time_left_after_the_first_wait_is_not_confirmed(self):
        clock = [100.0]

        class Wait:
            def __init__(self, driver, timeout):
                pass

            def until(self, condition):
                clock[0] += 11.0         # 첫 대기가 기한을 다 쓴다
                return True

        with patch.object(adapter, "WebDriverWait", Wait):
            self.assertFalse(self._wait(MagicMock(), clock, timeout=10.0))

    def test_a_timeout_is_reported_as_not_confirmed_rather_than_raised(self):
        """캡처러가 `submit_not_confirmed` 로 접어 page_source 를 읽지 않게 한다."""
        for failing in (0, 1):
            with self.subTest(wait=failing):
                clock, calls = [100.0], []

                class Wait:
                    def __init__(self, driver, timeout):
                        pass

                    def until(self, condition):
                        calls.append(1)
                        if len(calls) == failing + 1:
                            raise TimeoutException("timed out")
                        return True

                with patch.object(adapter, "WebDriverWait", Wait):
                    self.assertFalse(self._wait(MagicMock(), clock))

    def test_the_ready_state_is_what_the_second_wait_checks(self):
        self.assertIn("readyState", adapter.READY_STATE_SCRIPT)


class TheClockIsReadWhenCalled(unittest.TestCase):
    """⛔ `monotonic=time.monotonic` 을 기본 인자로 묶으면 정의 시점의 함수가 박혀 모듈 패치가
    듣지 않는다. 이 리포에서 재발한 결함이라 **주입 없이** 패치가 먹는지 본다."""

    def test_the_module_clock_is_used_when_none_is_injected(self):
        clock = [100.0]

        class Wait:
            def __init__(self, driver, timeout):
                pass

            def until(self, condition):
                clock[0] += 6.0
                return True

        # monotonic 을 **주입하지 않는다** — 모듈 시계를 패치해 그것이 쓰이는지 본다.
        with patch.object(adapter, "WebDriverWait", Wait), \
             patch.object(adapter.time, "monotonic", lambda: clock[0]):
            self.assertFalse(adapter.make_wait_replaced(MagicMock())(object(), 10.0),
                             "패치한 시계가 쓰였다면 기한 초과로 거부된다")
        self.assertEqual(clock[0], 112.0, "전제 — 대기가 시계를 진행시켰다")

    def test_the_injected_clock_still_wins(self):
        """양성 대조 — 주입 경로가 죽지 않았다."""
        clock = [0.0]

        class Wait:
            def __init__(self, driver, timeout):
                pass

            def until(self, condition):
                clock[0] += 1.0
                return True

        with patch.object(adapter, "WebDriverWait", Wait):
            self.assertTrue(adapter.make_wait_replaced(
                MagicMock(), monotonic=lambda: clock[0])(object(), 10.0))


class AlertHandling(unittest.TestCase):
    def test_a_pending_alert_is_accepted_and_its_text_returned(self):
        driver = MagicMock()
        driver.switch_to.alert.text = "새로고침은 연속으로 할 수 없습니다."
        self.assertEqual(adapter.take_alert(driver), "새로고침은 연속으로 할 수 없습니다.")
        driver.switch_to.alert.accept.assert_called_once()

    def test_no_alert_is_reported_as_none(self):
        driver = MagicMock()
        type(driver.switch_to).alert = property(
            lambda self: (_ for _ in ()).throw(NoAlertPresentException()))
        self.assertIsNone(adapter.take_alert(driver))

    def test_an_empty_alert_text_is_still_reported_as_present(self):
        driver = MagicMock()
        driver.switch_to.alert.text = ""
        self.assertEqual(adapter.take_alert(driver), "alert")


class TheOperationBundleMatchesTheCapturerContract(unittest.TestCase):
    def test_a_missing_reader_is_refused(self):
        with self.assertRaises(ValueError):
            adapter.build_operations(MagicMock(), input_selector="#inDate",
                                     read_page_source=None)

    def test_it_supplies_exactly_what_the_capturer_needs(self):
        from app.ibk_selenium_strict import IbkSeleniumStrictCapturer

        ops = adapter.build_operations(MagicMock(), input_selector="#inDate",
                                       read_page_source=lambda d: ("<html/>", None))
        # 캡처러가 받아들이면 계약이 맞는 것이다 — 이름·개수를 따로 적지 않는다.
        IbkSeleniumStrictCapturer(parse=lambda *a, **k: None, **ops)
        for name, value in ops.items():
            with self.subTest(operation=name):
                self.assertTrue(callable(value))


if __name__ == "__main__":
    unittest.main()
