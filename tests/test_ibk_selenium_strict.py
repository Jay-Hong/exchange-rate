"""Selenium 보조 경로 — 저장 전 검증과 **제출 완료 확인**의 순서를 잠근다.

이 슬라이스의 계약은 둘이다.
1. 검증을 통과한 관측만 호출자에게 넘어간다. 이 모듈은 DB 를 건드리지 않는다.
2. **파싱보다 제출 완료 확인이 먼저다.** 날짜 readback 은 다른 날짜만 걸러낸다 —
   같은 날짜를 다시 조회하면 제출이 일어나지 않아도 통과한다(실물 캡처로 실증).

파서는 실제 응답 원본(`tests/fixtures/ibk/`)으로 돌리고, 드라이버 조작만 가짜로 바꾼다.

⛔ 여기서 잠그지 않는 것: 이 캡처가 운영 흐름에 어떻게 연결되는지. 배선은 다음 조각이다.
⛔ 브라우저를 띄우지 않는다. 네트워크도 쓰지 않는다.
"""

import datetime
import gzip
import pathlib
import unittest

from app.crawlers import ibk
from app.ibk_selenium_strict import (
    ACCEPTED,
    NO_SESSION,
    REJECTED,
    UNAVAILABLE,
    IbkSeleniumStrictCapture,
    IbkSeleniumStrictCapturer,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ibk"
DAY_0904 = datetime.date(2026, 9, 4)
DAY_0909 = datetime.date(2026, 9, 9)
DAY_0717 = datetime.date(2026, 7, 17)
REFERENCE = ibk.KST.localize(datetime.datetime(2026, 9, 9, 18, 0))
OBSERVED_0904 = {"usd-krw": 1351.80, "jpy-krw": 865.26, "eur-krw": 1569.85}


def fixture(name):
    return gzip.decompress((FIXTURES / f"{name}.html.gz").read_bytes()).decode("utf-8")


class FakeElement:
    def __init__(self, value):
        self._value = value
        self.submitted = []
        self.stale = False

    def get_attribute(self, name):
        return self._value if name == "value" else None


class FakeDriver:
    """DOM 을 문자열 하나로 들고 있는 최소 드라이버."""

    def __init__(self, html, served_date, *, alerts=(), navigates=True):
        self.html = html
        self.element = FakeElement(served_date)
        self.alerts = list(alerts)
        self.navigates = navigates
        self.submits = 0
        self.page_source_reads = 0


class Harness:
    """운영에서 주입할 조작을 가짜로 바꾼다. 파서만 진짜다."""

    def __init__(self, driver, *, submit_raises=None, read_failure=None,
                 stale_raises=None, clock=None, advance=None):
        self.driver = driver
        self.slept = []
        self._submit_raises = submit_raises
        self._read_failure = read_failure
        self._stale_raises = stale_raises
        self._now = 1000.0
        self._clock = clock
        self._advance = advance or (lambda _seconds: None)

    # --- 주입되는 조작 ---
    def find_input(self, driver):
        return driver.element

    def submit(self, element, text):
        if self._submit_raises:
            raise self._submit_raises
        self.driver.submits += 1
        element.submitted.append(text)
        if self.driver.navigates:
            element.stale = True
            self.driver.element = FakeElement(text)
            self.driver.html = self._after_submit_html(text)

    def _after_submit_html(self, text):
        return fixture("selenium_after_submit_20260904")

    def read_page_source(self, driver):
        driver.page_source_reads += 1
        if self._read_failure:
            return None, self._read_failure
        return driver.html, None

    def wait_stale(self, element, timeout):
        if self._stale_raises:
            raise self._stale_raises
        return element.stale

    def take_alert(self, driver):
        return driver.alerts.pop(0) if driver.alerts else None

    def monotonic(self):
        if self._clock is not None:
            return self._clock()
        return self._now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self._now += seconds
        if self._clock is not None:
            # 주입된 시계를 쓸 때도 대기가 시간에 반영되어야 나이 검사가 정직해진다.
            self._advance(seconds)

    def capturer(self, **kwargs):
        return IbkSeleniumStrictCapturer(
            parse=lambda html, *, query_date, reference_time: ibk._parse_ibk_official_response(
                _Text(html), query_date=query_date, reference_time=reference_time),
            find_input=self.find_input, submit=self.submit,
            read_page_source=self.read_page_source, wait_stale=self.wait_stale,
            take_alert=self.take_alert, monotonic=self.monotonic, sleep=self.sleep,
            **kwargs)


class _Text:
    def __init__(self, text):
        self.text = text


def run(harness, query_date, *, page_loaded_at=1000.0, **kwargs):
    return harness.capturer(**kwargs).capture(
        harness.driver, query_date=query_date, reference_time=REFERENCE,
        page_loaded_at=page_loaded_at)


class SubmissionIsConfirmedBeforeParsing(unittest.TestCase):
    """⛔ 이 클래스가 이 모듈의 존재 이유다."""

    def test_a_same_date_requery_that_never_navigated_is_not_accepted(self):
        # 화면은 09.09 를 서비스하고 우리도 09.09 를 원한다. 제출은 필요 없다 —
        # 신선도는 호출자의 항해에서 온다. 문서가 신선하면 받아들인다.
        harness = Harness(FakeDriver(fixture("selenium_stale_typed_not_submitted"), "2026.09.09"))
        fresh = run(harness, DAY_0909)
        self.assertEqual(fresh.verdict, ACCEPTED)
        self.assertFalse(fresh.submitted)
        self.assertIn("served_date_already_matched", fresh.notes)

        # 같은 화면인데 문서가 오래됐으면 읽지 않는다. readback 은 여기서 아무것도
        # 증명하지 못하므로 나이 검사가 유일한 방어다.
        stale_doc = run(harness, DAY_0909, page_loaded_at=0.0)
        self.assertEqual(stale_doc.verdict, UNAVAILABLE)
        self.assertEqual(stale_doc.reason, "page_too_old")

    def test_a_submit_that_did_not_replace_the_document_is_not_parsed(self):
        driver = FakeDriver(fixture("selenium_stale_typed_not_submitted"), "2026.09.09",
                            navigates=False)
        harness = Harness(driver)
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "submit_not_confirmed")
        self.assertEqual(driver.page_source_reads, 0, "확인 전에 읽으면 안 된다")

    def test_a_confirmed_submit_yields_the_observed_rates(self):
        harness = Harness(FakeDriver(fixture("selenium_stale_typed_not_submitted"),
                                     "2026.09.09"))
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, ACCEPTED)
        self.assertTrue(capture.submitted)
        self.assertEqual(capture.rates, OBSERVED_0904)
        self.assertEqual(capture.completed_at, "06:00:02")
        self.assertEqual(capture.service_date, DAY_0904)
        self.assertTrue(capture.usable)


class TheConsecutiveSubmitGuardIsHonoured(unittest.TestCase):
    def test_a_submit_waits_out_the_pages_three_second_guard(self):
        harness = Harness(FakeDriver(fixture("selenium_stale_typed_not_submitted"),
                                     "2026.09.09"))
        capture = run(harness, DAY_0904, page_loaded_at=999.0)  # 로드 1초 뒤
        self.assertEqual(capture.verdict, ACCEPTED)
        self.assertEqual(len(harness.slept), 1)
        self.assertAlmostEqual(harness.slept[0], 2.5, places=6)
        self.assertTrue(any(n.startswith("submit_guard_waited=") for n in capture.notes))

    def test_no_wait_when_the_guard_has_already_elapsed(self):
        harness = Harness(FakeDriver(fixture("selenium_stale_typed_not_submitted"),
                                     "2026.09.09"))
        capture = run(harness, DAY_0904, page_loaded_at=990.0)  # 10초 전 로드
        self.assertEqual(capture.verdict, ACCEPTED)
        self.assertEqual(harness.slept, [])

    def test_an_alert_after_submitting_is_not_retried(self):
        driver = FakeDriver(fixture("selenium_stale_typed_not_submitted"), "2026.09.09",
                            alerts=[None, "새로고침은 연속으로 할 수 없습니다."])
        harness = Harness(driver)
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "submit_blocked_by_alert")
        self.assertEqual(driver.submits, 1, "다시 치면 같은 가드에 또 걸린다")
        self.assertEqual(driver.page_source_reads, 0)

    def test_an_alert_already_open_on_entry_aborts_before_touching_the_dom(self):
        driver = FakeDriver(fixture("selenium_stale_typed_not_submitted"), "2026.09.09",
                            alerts=["새로고침은 연속으로 할 수 없습니다."])
        capture = run(Harness(driver), DAY_0904)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "alert_present")
        self.assertEqual(driver.submits, 0)
        self.assertEqual(driver.page_source_reads, 0)


class VerdictsFromRealCaptures(unittest.TestCase):
    def test_the_no_session_screen_is_its_own_verdict_not_a_rejection(self):
        harness = Harness(FakeDriver(fixture("http_dated_no_session_20260717"), "2026.07.17"))
        capture = run(harness, DAY_0717)
        self.assertEqual(capture.verdict, NO_SESSION)
        self.assertIsNone(capture.rates)
        self.assertFalse(capture.usable)

    def test_a_contract_violation_is_a_rejection_carrying_its_reason(self):
        broken = fixture("http_dated_20260904").replace("매매기준율", "매매기준율_변형")
        harness = Harness(FakeDriver(broken, "2026.09.04"))
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, REJECTED)
        self.assertIn("헤더 누락", capture.reason)
        self.assertFalse(capture.usable)

    def test_a_readback_mismatch_in_the_captured_dom_is_a_rejection(self):
        # 요소 속성과 직렬화된 DOM 이 어긋나면(요소는 09.04, 문서는 09.03) 제출 없이
        # 파싱으로 넘어가고 거기서 걸린다.
        broken = fixture("http_dated_20260904").replace("2026.09.04", "2026.09.03")
        capture = run(Harness(FakeDriver(broken, "2026.09.04")), DAY_0904)
        self.assertEqual(capture.verdict, REJECTED)
        self.assertIn("readback", capture.reason)

    def test_a_long_multiline_rejection_reason_is_bounded_to_one_line(self):
        harness = Harness(FakeDriver(fixture("http_dated_20260904"), "2026.09.04"))
        capturer = harness.capturer()
        noisy = "줄1\n" + "가" * 500
        capturer._parse = lambda *a, **k: (_ for _ in ()).throw(ValueError(noisy))
        capture = capturer.capture(harness.driver, query_date=DAY_0904,
                                   reference_time=REFERENCE, page_loaded_at=1000.0)
        self.assertEqual(capture.verdict, REJECTED)
        self.assertNotIn("\n", capture.reason)
        self.assertEqual(len(capture.reason), 120)


class FailuresAreTypedNotRaised(unittest.TestCase):
    def test_a_missing_input_is_unavailable(self):
        harness = Harness(FakeDriver(fixture("http_dated_20260904"), "2026.09.04"))
        harness.find_input = lambda driver: None
        self.assertEqual(run(harness, DAY_0904).reason, "input_absent")

    def test_a_stalled_page_source_read_is_unavailable(self):
        harness = Harness(FakeDriver(fixture("http_dated_20260904"), "2026.09.04"),
                          read_failure="page_source_timeout")
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "page_source_timeout")

    def test_a_submit_failure_is_unavailable(self):
        harness = Harness(FakeDriver(fixture("http_dated_20260904"), "2026.09.09"),
                          submit_raises=RuntimeError("PRIVATE_DRIVER_DETAIL"))
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertIn("submit_failed:RuntimeError", capture.reason)
        self.assertNotIn("PRIVATE_DRIVER_DETAIL", str(capture))

    def test_a_staleness_wait_failure_is_unavailable(self):
        harness = Harness(FakeDriver(fixture("http_dated_20260904"), "2026.09.09"),
                          stale_raises=RuntimeError("boom"))
        self.assertIn("staleness_failed:RuntimeError", run(harness, DAY_0904).reason)

    def test_an_unreadable_input_attribute_is_unavailable(self):
        driver = FakeDriver(fixture("http_dated_20260904"), "2026.09.04")

        class Exploding:
            def get_attribute(self, name):
                raise RuntimeError("nope")

        driver.element = Exploding()
        self.assertIn("input_unreadable:RuntimeError", run(Harness(driver), DAY_0904).reason)

    def test_a_parser_crash_is_unavailable_rather_than_an_exception(self):
        harness = Harness(FakeDriver(fixture("http_dated_20260904"), "2026.09.04"))
        capturer = harness.capturer()
        capturer._parse = lambda *a, **k: (_ for _ in ()).throw(TypeError("bad"))
        capture = capturer.capture(harness.driver, query_date=DAY_0904,
                                   reference_time=REFERENCE, page_loaded_at=1000.0)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "TypeError")


class TheModuleNeverWrites(unittest.TestCase):
    # ⛔ 원문 문자열로 훑으면 산문("이 세션의 DOM", "selenium 을 임포트하지 않는다")까지
    #    걸린다. 실제로 봐야 하는 것은 **임포트와 호출**이므로 AST 로 본다.
    FORBIDDEN_MODULES = ("selenium", "sqlalchemy", "bs4", "app.crud", "app.database")
    FORBIDDEN_CALLS = ("insert_bank_rates_into_db", "commit", "rollback", "Chrome",
                       "webdriver")

    def _tree(self):
        import ast
        return ast.parse(pathlib.Path("app/ibk_selenium_strict.py").read_text())

    def test_the_module_imports_no_driver_db_or_parser_library(self):
        import ast
        imported = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertTrue(imported, "양성 대조 — 임포트를 실제로 수집했어야 한다")
        offenders = sorted(name for name in imported
                           if any(name == bad or name.startswith(bad + ".")
                                  for bad in self.FORBIDDEN_MODULES))
        self.assertEqual(offenders, [], f"금지 임포트가 생겼다: {offenders}")

    def test_the_module_calls_nothing_that_writes_or_builds_a_driver(self):
        import ast
        names = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call):
                target = node.func
                names.add(target.attr if isinstance(target, ast.Attribute)
                          else getattr(target, "id", ""))
        # ⛔ 초판의 양성 대조는 `assertIn("capture", names | {"capture"})` 였다 —
        #    수집이 비어도 통과하는 **실패할 수 없는 단언**이었다. 실제로 있어야 하는
        #    호출명을 직접 요구한다.
        # 주입된 조작은 `_guarded` 에 **참조로** 넘어가므로 호출 노드가 아니다 —
        # 실제로 호출되는 이름을 요구한다(초판은 여기서 어긋나 대조가 발화했다).
        for expected in ("_guarded", "_document_age_reason", "strftime"):
            self.assertIn(expected, names, f"양성 대조 — {expected} 호출을 수집했어야 한다")
        offenders = sorted(names & set(self.FORBIDDEN_CALLS))
        self.assertEqual(offenders, [], f"쓰기·드라이버 생성 호출이 생겼다: {offenders}")

    def test_a_capture_is_frozen_so_a_caller_cannot_promote_it(self):
        capture = IbkSeleniumStrictCapture(REJECTED, reason="x")
        with self.assertRaises(Exception):
            capture.verdict = ACCEPTED
        self.assertFalse(capture.usable)

    def test_only_an_accepted_capture_with_rates_is_usable(self):
        for verdict in (NO_SESSION, REJECTED, UNAVAILABLE):
            with self.subTest(verdict=verdict):
                self.assertFalse(IbkSeleniumStrictCapture(
                    verdict, rates={"usd-krw": 1.0}).usable)
        self.assertFalse(IbkSeleniumStrictCapture(ACCEPTED, rates={}).usable)
        self.assertTrue(IbkSeleniumStrictCapture(ACCEPTED, rates={"usd-krw": 1.0}).usable)


class DriverFailuresNeverEscape(unittest.TestCase):
    """⛔ 조작이 예외를 던지면 호출자까지 전파되던 결함의 회귀 잠금(실측 3건)."""

    OPERATIONS = ("take_alert", "find_input", "read_page_source", "submit", "wait_stale")

    def test_every_injected_operation_failure_becomes_a_typed_result(self):
        for name in self.OPERATIONS:
            with self.subTest(operation=name):
                # submit·wait_stale 은 제출 경로에서만 불리므로 다른 날짜를 서비스시킨다.
                served = "2026.09.01" if name in ("submit", "wait_stale") else "2026.09.04"
                harness = Harness(FakeDriver(fixture("http_dated_20260904"), served))

                def boom(*args, **kwargs):
                    raise RuntimeError("PRIVATE_DRIVER_DETAIL")

                setattr(harness, name, boom)
                capture = run(harness, DAY_0904)
                self.assertEqual(capture.verdict, UNAVAILABLE)
                self.assertIn("RuntimeError", capture.reason)
                self.assertNotIn("PRIVATE_DRIVER_DETAIL", str(capture),
                                 "드라이버 예외 원문을 옮기면 안 된다")


class DocumentAgeIsCheckedAtRead(unittest.TestCase):
    """⛔ 진입 때 한 번만 보던 결함의 회귀 잠금 — 진입 후 61초에 accepted 가 나왔었다."""

    def test_time_passing_after_entry_is_caught_before_reading(self):
        clock = [1000.0]
        driver = FakeDriver(fixture("http_dated_20260904"), "2026.09.04")
        harness = Harness(driver, clock=lambda: clock[0])
        original = harness.find_input

        def slow_find(d):
            clock[0] += 61.0
            return original(d)

        harness.find_input = slow_find
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "page_too_old")
        self.assertEqual(driver.page_source_reads, 0)

    def test_a_read_that_itself_took_too_long_is_not_accepted(self):
        clock = [1000.0]
        driver = FakeDriver(fixture("http_dated_20260904"), "2026.09.04")
        harness = Harness(driver, clock=lambda: clock[0])
        original = harness.read_page_source

        def slow_read(d):
            clock[0] += 61.0
            return original(d)

        harness.read_page_source = slow_read
        capture = run(harness, DAY_0904)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "page_too_old_after_read")

    def test_a_fresh_document_after_submit_is_measured_from_the_submit(self):
        # 기존 문서가 55초 됐어도, 제출 뒤 7초 된 새 문서는 60초 상한에서 허용한다.
        clock = [1055.0]
        harness = Harness(FakeDriver(fixture("selenium_stale_typed_not_submitted"),
                                     "2026.09.01"), clock=lambda: clock[0],
                          advance=lambda s: clock.__setitem__(0, clock[0] + s))
        original = harness.wait_stale

        def confirm_after(element, timeout):
            clock[0] += 7.0
            return original(element, timeout)

        harness.wait_stale = confirm_after
        capture = run(harness, DAY_0904, page_loaded_at=1000.0)
        self.assertEqual(capture.verdict, ACCEPTED)
        self.assertEqual(capture.rates, OBSERVED_0904)

    def test_a_late_confirmation_does_not_reset_the_new_documents_age(self):
        # ⛔ 확인이 **반환된** 시각을 기준으로 삼으면 이미 늙은 문서의 나이가 0 이 된다
        #    (실측: 70초 된 문서가 60초 상한에서 accepted). 기준은 제출 직전 시각이다.
        clock = [1000.0]
        harness = Harness(FakeDriver(fixture("selenium_stale_typed_not_submitted"),
                                     "2026.09.01"), clock=lambda: clock[0],
                          advance=lambda s: clock.__setitem__(0, clock[0] + s))
        original = harness.wait_stale

        def confirm_late(element, timeout):
            clock[0] += 7.0
            return original(element, timeout)

        harness.wait_stale = confirm_late
        capture = run(harness, DAY_0904, page_loaded_at=1000.0,
                      max_page_age_seconds=5.0, staleness_timeout=10.0,
                      submit_guard_seconds=0.0)
        self.assertEqual(capture.verdict, UNAVAILABLE)
        self.assertEqual(capture.reason, "page_too_old")

    def test_non_finite_and_backwards_clocks_are_refused(self):
        cases = {
            "page_loaded_at_unusable": dict(page_loaded_at=float("nan")),
            "clock_went_backwards": dict(page_loaded_at=2000.0),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                harness = Harness(FakeDriver(fixture("http_dated_20260904"), "2026.09.04"))
                capture = run(harness, DAY_0904, **kwargs)
                self.assertEqual(capture.verdict, UNAVAILABLE)
                self.assertEqual(capture.reason, reason)


class DependencyValidation(unittest.TestCase):
    def test_bad_dependencies_are_refused(self):
        ok = dict(parse=lambda *a, **k: None, find_input=lambda d: None,
                  submit=lambda e, t: None, read_page_source=lambda d: (None, "x"),
                  wait_stale=lambda e, t: True, take_alert=lambda d: None)
        for override in ({"parse": None}, {"submit": "x"}, {"take_alert": 3},
                         {"submit_guard_seconds": -1}, {"staleness_timeout": "x"},
                         {"max_page_age_seconds": -0.5},
                         {"max_page_age_seconds": float("nan")},
                         {"submit_guard_seconds": float("inf")}):
            with self.subTest(override=sorted(override)):
                with self.assertRaises(ValueError):
                    IbkSeleniumStrictCapturer(**{**ok, **override})


if __name__ == "__main__":
    unittest.main()
