"""IBK Selenium 검증 관측(shadow) — 관측이 수집을 바꾸지 않는지 잠근다.

이 슬라이스의 계약은 하나다: **shadow 는 기록만 한다.** 저장·날짜후퇴·폴백 행동이 조금이라도
달라지면 관측이 예측력을 잃고, 그 상태로 강제 적용을 켜면 무엇이 바뀌었는지 귀속할 수 없다.

⛔ 여기서 잠그지 않는 것: "준비 완료" 판정 기준. 그 기준을 정할 데이터가 아직 없어서 이번
   슬라이스는 관측만 한다. parser 통과는 "캡처된 HTML 이 parser 계약을 통과했다" 는 뜻뿐이다.
⛔ 실제 Selenium DOM 과 HTTP 응답의 구조 호환성은 미검증이다 — 리포에 실제 캡처 fixture 가
   없고 여기 HTML 도 코드로 조립한 것이다.
"""

import datetime
import time
import unittest
from unittest.mock import MagicMock, patch

from app import ibk_selenium_config
from app.crawlers import ibk
from app.ibk_selenium_observation import (
    MAPPING_INCOMPARABLE,
    MAPPING_MATCH,
    MAPPING_MISMATCH,
    IbkSeleniumObservation,
    compare_mappings,
)

RATES = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}
QUERY_DATE = datetime.date(2026, 8, 27)
REFERENCE = ibk.KST.localize(datetime.datetime(2026, 8, 28, 12, 0))


def _official_html(*, selected_date="2026.08.27", completed_at="11:30:00", rows=True):
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


class MappingComparisonTest(unittest.TestCase):
    """세 갈래여야 한다 — 비교 불가를 불일치로 세면 계약 거절과 추출 차이가 섞인다."""

    def test_identical_mappings_match(self):
        self.assertEqual(compare_mappings(dict(RATES), dict(RATES)), (MAPPING_MATCH, ()))

    def test_a_differing_value_is_a_mismatch_naming_only_the_pair(self):
        other = dict(RATES, **{"usd-krw": 1.0})
        status, diff = compare_mappings(other, dict(RATES))
        self.assertEqual((status, diff), (MAPPING_MISMATCH, ("usd-krw",)))

    def test_a_partial_legacy_mapping_is_a_mismatch(self):
        status, diff = compare_mappings({"usd-krw": RATES["usd-krw"]}, dict(RATES))
        self.assertEqual(status, MAPPING_MISMATCH)
        self.assertEqual(diff, ("eur-krw", "jpy-krw"))

    def test_a_missing_validated_mapping_is_incomparable_not_a_mismatch(self):
        """parser 가 후보를 못 내면 빈 mapping 으로 취급해 불일치로 세면 안 된다."""
        self.assertEqual(compare_mappings(dict(RATES), None), (MAPPING_INCOMPARABLE, ()))

    def test_a_missing_legacy_mapping_is_also_incomparable(self):
        self.assertEqual(compare_mappings(None, dict(RATES)), (MAPPING_INCOMPARABLE, ()))


class ObservationCaptureTest(unittest.TestCase):
    """캡처가 어떤 사실을 들고 나오는가 — 필드별 독립성이 핵심이다."""

    def _observe(self, html, *, legacy_rates=None, query_date=QUERY_DATE):
        driver = MagicMock()
        if isinstance(html, Exception):
            type(driver).page_source = property(lambda _self: (_ for _ in ()).throw(html))
        else:
            driver.page_source = html
        return ibk._observe_selenium_capture(
            driver, query_date=query_date, reference_time=REFERENCE,
            legacy_rates=dict(RATES) if legacy_rates is None else legacy_rates,
        )

    def test_a_clean_capture_is_accepted_and_matches(self):
        result = self._observe(_official_html())

        self.assertEqual(result.parser_verdict, "accept")
        self.assertEqual(result.input_value_date, "2026.08.27")
        self.assertEqual(result.requested_date, "2026.08.27")
        self.assertEqual(result.observed_pairs, ("eur-krw", "jpy-krw", "usd-krw"))
        self.assertEqual(result.mapping_comparison, MAPPING_MATCH)
        self.assertIsNotNone(result.captured_at)

    def test_a_stale_date_is_rejected_but_the_read_value_is_still_recorded(self):
        """거부돼도 '무엇을 읽었는가' 가 남아야 사후에 원인을 가릴 수 있다."""
        result = self._observe(_official_html(selected_date="2026.08.20"))

        self.assertEqual(result.parser_verdict, "reject")
        self.assertIsNotNone(result.parser_reject_reason)
        self.assertEqual(result.input_value_date, "2026.08.20",
                         "거부 사유와 별개로 읽은 value 속성은 기록된다")

    def test_the_currency_list_survives_a_parser_reject(self):
        """거부돼도 '표에 어떤 통화가 있었나' 는 남아야 한다.

        ⛔ parser 성공에 종속시키면 완료시각 형식 하나가 틀렸을 때 통화 목록까지 사라져,
           '표가 비었다' 와 '형식이 틀렸다' 를 사후에 구분할 수 없다.
        """
        result = self._observe(_official_html(completed_at="99:99:99"))

        self.assertNotEqual(result.parser_verdict, "accept")
        self.assertEqual(result.observed_pairs, ("eur-krw", "jpy-krw", "usd-krw"),
                         "통화 목록은 통화 코드 기준으로 parser 와 독립 수집된다")

    def test_the_currency_cell_may_be_a_td(self):
        """코드 칸이 `td` 여도 parser 와 같은 통화를 봐야 한다.

        ⛔ `th` 만 보면 parser 는 통과시키는데 관측만 빈 목록이 되어 "표가 비었다" 로
           오독된다(상호 검토에서 실증).
        """
        html = _official_html()
        for code in ("USD", "JPY", "EUR"):
            html = html.replace(f"<th>{code}</th>", f"<td>{code}</td>")
        result = self._observe(html)

        self.assertEqual(result.parser_verdict, "accept")
        self.assertEqual(result.observed_pairs, ("eur-krw", "jpy-krw", "usd-krw"))

    def test_only_the_official_table_is_counted(self):
        """문서의 다른 표가 통화 목록에 섞이면 안 된다 — parser 는 공식 표만 본다."""
        html = _official_html()
        for code, rate in (("JPY", RATES["jpy-krw"]), ("EUR", RATES["eur-krw"])):
            html = html.replace(
                f"<tr><th>{code}</th><th>{code} name</th><td>{rate}</td><td>0</td></tr>", "")
        html = html.replace("</body>", "<table><caption>다른 표</caption><tbody>"
                            "<tr><th>JPY</th><td>1</td></tr>"
                            "<tr><th>EUR</th><td>2</td></tr></tbody></table></body>")
        result = self._observe(html)

        self.assertEqual(result.observed_pairs, ("usd-krw",),
                         "공식 caption 밖의 표는 세지 않는다")

    def test_no_session_is_not_counted_as_an_observation_failure(self):
        """정상 무고시(parser 가 None)는 관측 실패가 아니다 — 분모가 오염된다."""
        with patch.object(ibk, "_parse_ibk_official_response", return_value=None):
            result = self._observe(_official_html())

        self.assertEqual(result.parser_verdict, "no_session")
        self.assertIsNone(result.parser_reject_reason)

    def test_a_reject_leaves_the_comparison_incomparable(self):
        """검증 후보가 없으므로 legacy 와 대조할 수 없다 — 불일치가 아니다."""
        result = self._observe(_official_html(rows=False))
        self.assertEqual(result.mapping_comparison, MAPPING_INCOMPARABLE)

    def test_a_mapping_difference_is_recorded_without_naming_a_cause(self):
        """행 순서 뒤바뀜·날짜 혼합 등은 원인을 단정하지 않고 차이만 남긴다."""
        wrong = dict(RATES, **{"usd-krw": RATES["jpy-krw"], "jpy-krw": RATES["usd-krw"]})
        result = self._observe(_official_html(), legacy_rates=wrong)

        self.assertEqual(result.mapping_comparison, MAPPING_MISMATCH)
        self.assertEqual(result.mapping_diff_pairs, ("jpy-krw", "usd-krw"))

    def test_the_observation_issues_exactly_one_driver_command(self):
        """관측이 쓰는 driver 명령은 `page_source` **하나뿐**이어야 한다.

        ⛔ 이 개수가 곧 위험 반경이다. ChromeDriver 는 세션 명령을 직렬 처리하므로 멈춘 명령
           하나가 `driver.quit()` 을 막고, 부모 kill → worker 재등록으로 IBK 회차가 통째로
           한 번 더 돈다(상호 검토에서 실증). 명령을 하나 더 늘리면 그 확률이 늘어난다.
        """
        touched = []

        class Recording:
            def __getattr__(self, name):
                touched.append(name)
                if name == "page_source":
                    return _official_html()
                raise AttributeError(name)

        result = ibk._observe_selenium_capture(
            Recording(), query_date=QUERY_DATE, reference_time=REFERENCE, legacy_rates=dict(RATES))

        self.assertEqual(result.parser_verdict, "accept")
        self.assertEqual(touched, ["page_source"],
                         f"관측이 driver 명령을 추가로 썼다: {touched}")

    def test_a_stalled_read_is_a_timeout_not_a_failure(self):
        """멈춤과 고장을 합치면 '관측이 느리다' 와 'driver 가 죽었다' 를 못 가린다."""
        import time as _time

        class Stalled:
            @property
            def page_source(self):
                _time.sleep(1.0)
                return ""

        with patch.object(ibk, "SHADOW_PAGE_SOURCE_TIMEOUT", 0.05):
            result = ibk._observe_selenium_capture(
                Stalled(), query_date=QUERY_DATE, reference_time=REFERENCE, legacy_rates=dict(RATES))

        self.assertEqual(result.parser_verdict, "unavailable")
        self.assertIn("page_source_timeout", result.notes)
        self.assertNotIn("page_source_failed", result.notes)

    def test_a_page_source_failure_is_unavailable_not_a_reject(self):
        """관측 실패를 정상 판정으로 세면 분모가 오염된다."""
        result = self._observe(RuntimeError("driver gone"))

        self.assertEqual(result.parser_verdict, "unavailable")
        self.assertIn("page_source_failed", result.notes)

    def test_the_observer_never_raises(self):
        """관측이 예외를 내면 legacy 수집이 깨진다 — shadow 의 존재 이유가 사라진다."""
        for html in ("", "<html>", "not html at all", _official_html(completed_at="99:99:99")):
            with self.subTest(html=html[:20]):
                self.assertIsInstance(self._observe(html), IbkSeleniumObservation)

    def test_the_log_payload_keeps_absent_fields(self):
        """부재가 곧 사실이다 — None 을 지우면 '못 봤다' 를 사후에 알 수 없다."""
        payload = self._observe(RuntimeError("x")).as_log_extra()
        self.assertIn("ibk_selenium_input_value_date", payload)
        self.assertIsNone(payload["ibk_selenium_input_value_date"])


class ModeResolutionTest(unittest.TestCase):
    """설정 해석 — 오타가 조용히 기본값으로 떨어지면 '켜 뒀다' 고 믿은 채 관측이 꺼진다."""

    def test_unset_and_blank_fall_back_to_legacy(self):
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(ibk_selenium_config.resolve_mode(raw), "legacy")

    def test_values_are_normalised(self):
        self.assertEqual(ibk_selenium_config.resolve_mode(" SHADOW "), "shadow")

    def test_an_unknown_value_fails_loudly(self):
        with self.assertRaises(ValueError):
            ibk_selenium_config.resolve_mode("shadwo")

    def test_enforce_is_rejected_until_its_behaviour_exists(self):
        with self.assertRaises(ValueError):
            ibk_selenium_config.resolve_mode("enforce")

    def test_env_reading_is_injectable(self):
        """시험이 환경을 주입할 수 있어야 한다 — 전역 os.environ 을 흔들지 않고."""
        self.assertEqual(
            ibk_selenium_config.resolve_mode_from_env({"IBK_SELENIUM_VALIDATION_MODE": "shadow"}),
            "shadow",
        )
        self.assertEqual(ibk_selenium_config.resolve_mode_from_env({}), "legacy")

    def test_the_setting_is_not_defined_in_the_cited_config_module(self):
        """app/config.py 는 C2 인용 경로다 — 여기 두면 IBK 슬라이스마다 재결속이 붙는다."""
        from app import config

        self.assertFalse(hasattr(config, "IBK_SELENIUM_VALIDATION_MODE"))


class LogReachabilityTest(unittest.TestCase):
    """관측이 **조회 가능한 로그에 도달**하는지. 설정 존재와 도달은 별개다.

    부모는 자식의 exit code 로만 성공을 판정하고 정상 반환 시 stdout 을 전달하지 않는다.
    프로세스 내부 카운터나 콘솔 출력만 늘리면 관측을 사후에 못 읽는다.
    """

    def _run_shadow_once(self):
        driver = MagicMock()
        driver.page_source = _official_html()
        element = MagicMock()
        element.text = "1382.0"
        wait = MagicMock()
        wait.until.return_value = element
        context = MagicMock()
        context.__enter__.return_value = driver

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk.crud, "insert_bank_rates_into_db", return_value=1):
            ibk.crawl_and_save_ibk_routine_selenium("http://x", ibk.IBK_BANK_SELECTORS, MagicMock())

    def test_the_observation_propagates_to_a_root_handler(self):
        """**root** handler 로 전달되는지 본다 — 운영의 app.log 가 root 에 붙기 때문이다.

        ⛔ crawler logger 에 직접 handler 를 붙여 확인하면 아무것도 증명 못 한다. 내가 붙인
           handler 가 동작한다는 것만 보게 된다 — root 를 다 떼고 propagate 를 꺼도 통과했다
           (상호 검토에서 음성 대조로 실증). 그래서 root 에 붙이고, 아래 음성 대조를 함께 둔다.

        ⛔ 이 시험이 보는 것은 **root 로의 전달까지**다. 운영 `app.log` 의 RotatingFileHandler
           기록과 subprocess 도달은 여기서 증명하지 않는다 — `LOG_DIR` 이 `BASE_DIR/logs` 로
           고정이라 시험이 실제 로그를 오염시키지 않고 확인할 방법이 없다. 그 둘은 별도
           일회 실측으로 확인했다(별개 프로세스 emit → app.log 에 구조화 레코드 적재).
        """
        import logging

        root = logging.getLogger()
        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Collector()
        root.addHandler(handler)
        self.addCleanup(lambda: root.removeHandler(handler))
        previous = root.level
        root.setLevel(logging.INFO)
        self.addCleanup(lambda: root.setLevel(previous))

        self._run_shadow_once()

        self.assertIn("IBK_SELENIUM_SHADOW_OBSERVATION", records)

    def test_the_reachability_check_fails_when_propagation_is_off(self):
        """음성 대조 — 전달이 끊기면 위 시험이 반드시 실패해야 한다.

        이게 없으면 도달 시험이 공허해진 것을 알 수 없다.
        """
        import logging

        root = logging.getLogger()
        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Collector()
        root.addHandler(handler)
        self.addCleanup(lambda: root.removeHandler(handler))
        # ⛔ 양성 시험과 **같은 레벨 조건**이어야 한다. root 가 WARNING 인 환경에서는 INFO 가
        #    레벨에서 걸러져, 전달을 끊지 않아도 아무것도 안 잡힌다 — 그러면 이 음성 대조가
        #    전달을 보는지 레벨을 보는지 구분되지 않는다(상호 검토에서 실증).
        previous = root.level
        root.setLevel(logging.INFO)
        self.addCleanup(lambda: root.setLevel(previous))
        crawler_logger = logging.getLogger("exchange_rate.crawler.ibk")
        was_propagating = crawler_logger.propagate
        crawler_logger.propagate = False
        self.addCleanup(lambda: setattr(crawler_logger, "propagate", was_propagating))

        self._run_shadow_once()

        self.assertNotIn("IBK_SELENIUM_SHADOW_OBSERVATION", records,
                         "전달이 끊겼는데도 잡히면 이 검사가 무엇을 보고 있는지 알 수 없다")


class ModeGateTest(unittest.TestCase):
    def test_the_default_mode_is_legacy(self):
        self.assertEqual(ibk_selenium_config.VALIDATION_MODE, "legacy")

    def test_enforce_is_not_an_accepted_value_yet(self):
        """값만 받고 동작이 없으면 설정과 실제가 어긋난다 — 강제는 그 커밋에서 함께 추가한다."""
        self.assertNotIn("enforce", ibk_selenium_config.ALLOWED_MODES)
        self.assertEqual(ibk_selenium_config.ALLOWED_MODES, ("legacy", "shadow"))

    def _run_selenium_routine(self):
        """실제 수집 함수를 돌린다 — driver·DB 만 대역으로 세우고 경로는 진짜다.

        ⛔ 모드 게이트를 '설정값을 읽어 비교' 로만 시험하면 아무것도 잠기지 않는다.
           수집 경로가 실제로 관측기를 부르는지/안 부르는지를 봐야 한다.
        """
        driver = MagicMock()
        driver.page_source = _official_html()
        element = MagicMock()
        element.text = "1382.0"

        wait = MagicMock()
        wait.until.return_value = element
        context = MagicMock()
        context.__enter__.return_value = driver

        with patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk.crud, "insert_bank_rates_into_db", return_value=1) as write:
            ibk.crawl_and_save_ibk_routine_selenium("http://x", ibk.IBK_BANK_SELECTORS, MagicMock())
        return write

    def test_legacy_mode_does_not_call_the_observer(self):
        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "legacy"), \
             patch.object(ibk, "_observe_selenium_capture") as spy:
            write = self._run_selenium_routine()

        spy.assert_not_called()
        write.assert_called_once()

    def test_shadow_mode_calls_the_observer_but_still_saves(self):
        """shadow 는 기록만 한다 — 저장이 사라지면 legacy 동작이 바뀐 것이다."""
        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "_observe_selenium_capture",
                          return_value=IbkSeleniumObservation()) as spy:
            write = self._run_selenium_routine()

        spy.assert_called_once()
        write.assert_called_once()

    def _run_with_slow_page_source(self, *, mode, delay, bound):
        """`page_source` 가 멈춘 상황을 시간으로 흉내낸다."""
        class SlowDriver:
            def __init__(self, d): self._d = d
            @property
            def page_source(self):
                time.sleep(self._d)
                return _official_html()
            def get(self, url): pass

        context = MagicMock()
        context.__enter__.return_value = SlowDriver(delay)
        element = MagicMock()
        element.text = "1382.0"
        wait = MagicMock()
        wait.until.return_value = element
        write = MagicMock(return_value=1)

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", mode), \
             patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk, "SHADOW_PAGE_SOURCE_TIMEOUT", bound), \
             patch.object(ibk.crud, "insert_bank_rates_into_db", write):
            started = time.monotonic()
            ibk.crawl_and_save_ibk_routine_selenium("http://x", ibk.IBK_BANK_SELECTORS, MagicMock())
            elapsed = time.monotonic() - started
        return write, elapsed

    def test_a_stalled_observation_does_not_cost_the_save(self):
        """관측이 멈춰도 저장은 legacy 와 같이 일어나야 한다.

        ⛔ hang 은 예외가 아니라 경계 wrapper 가 못 막는다. 관측이 저장 앞에 있으면 부모의
           수집 제한시간이 자식을 죽여, legacy 라면 저장됐을 값이 사라진다(상호 검토에서 실증).
        """
        legacy_write, _ = self._run_with_slow_page_source(mode="legacy", delay=1.0, bound=0.05)
        shadow_write, _ = self._run_with_slow_page_source(mode="shadow", delay=1.0, bound=0.05)

        legacy_write.assert_called_once()
        shadow_write.assert_called_once()
        self.assertEqual(shadow_write.call_args.kwargs["current_rates"],
                         legacy_write.call_args.kwargs["current_rates"])

    def test_the_observation_read_is_bounded(self):
        """멈춘 읽기는 상한에서 잘려야 한다 — 폴백 기회를 통째로 삼키면 안 된다."""
        _, elapsed = self._run_with_slow_page_source(mode="shadow", delay=5.0, bound=0.2)

        self.assertLess(elapsed, 2.0,
                        "상한이 실제로 적용되지 않으면 관측이 수집 제한시간을 먹는다")

    def test_the_writer_return_count_is_passed_through(self):
        """저장 함수의 반환 개수가 그대로 나와야 한다 — 부모 결과가 이 값을 쓴다."""
        for expected in (0, 1, 3):
            with self.subTest(expected=expected):
                driver = MagicMock()
                driver.page_source = _official_html()
                context = MagicMock()
                context.__enter__.return_value = driver
                element = MagicMock()
                element.text = "1382.0"
                wait = MagicMock()
                wait.until.return_value = element

                with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
                     patch.object(ibk, "selenium_driver_context", return_value=context), \
                     patch.object(ibk, "WebDriverWait", return_value=wait), \
                     patch.object(ibk.crud, "insert_bank_rates_into_db", return_value=expected):
                    written = ibk.crawl_and_save_ibk_routine_selenium(
                        "http://x", ibk.IBK_BANK_SELECTORS, MagicMock())

                self.assertEqual(written, expected)

    def test_the_failure_branch_does_not_observe(self):
        """추출 실패 분기에서는 관측하지 않는다.

        ⛔ 이 분기 뒤에는 Selenium 재시도 3회와 MIBANK 폴백이 온다. 관측의 읽기 대기가
           시도마다 쌓이면 부모의 45초 제한 안에서 폴백 저장 기회가 사라진다
           (상호 검토에서 실증).
        """
        reads = []
        driver = MagicMock()
        driver.page_source = _official_html()
        context = MagicMock()
        context.__enter__.return_value = driver
        element = MagicMock()
        element.text = ""          # 값 추출 실패
        wait = MagicMock()
        wait.until.return_value = element

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk, "_read_page_source_bounded",
                          side_effect=lambda *a, **k: (reads.append(1), (None, "x"))[1]):
            with self.assertRaises(Exception):
                ibk.crawl_and_save_ibk_routine_selenium("http://x", ibk.IBK_BANK_SELECTORS, MagicMock())

        self.assertEqual(reads, [], "실패 분기에서 관측이 폴백 예산을 먹으면 안 된다")

    def test_the_budget_guard_skips_the_observation(self):
        """예산을 이미 썼으면 관측을 건너뛴다 — 저장은 그대로 일어난다."""
        reads = []
        driver = MagicMock()
        driver.page_source = _official_html()
        context = MagicMock()
        context.__enter__.return_value = driver
        element = MagicMock()
        element.text = "1382.0"
        wait = MagicMock()
        wait.until.return_value = element
        write = MagicMock(return_value=1)

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk, "_read_page_source_bounded",
                          side_effect=lambda *a, **k: (reads.append(1), (None, "x"))[1]), \
             patch.object(ibk.crud, "insert_bank_rates_into_db", write):
            ibk.crawl_and_save_ibk_routine_selenium(
                "http://x", ibk.IBK_BANK_SELECTORS, MagicMock(),
                run_started_at=time.monotonic() - (ibk.SHADOW_BUDGET_GUARD_SECONDS + 10))

        self.assertEqual(reads, [])
        write.assert_called_once()

    def test_without_a_run_anchor_the_guard_does_not_apply(self):
        """기준을 모르면 건너뛰지 않는다 — 오래 산 프로세스에서 관측이 조용히 사라지면 안 된다.

        ⛔ 기준을 모듈 import 시각으로 잡았을 때 전체 스위트에서 관측이 항상 꺼졌다(실측).
           격리 실행만 보면 통과해 거짓 초록이 된다.
        """
        reads = []
        driver = MagicMock()
        driver.page_source = _official_html()
        context = MagicMock()
        context.__enter__.return_value = driver
        element = MagicMock()
        element.text = "1382.0"
        wait = MagicMock()
        wait.until.return_value = element

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk, "_read_page_source_bounded",
                          side_effect=lambda *a, **k: (reads.append(1), (None, "x"))[1]), \
             patch.object(ibk.crud, "insert_bank_rates_into_db", return_value=1):
            ibk.crawl_and_save_ibk_routine_selenium("http://x", ibk.IBK_BANK_SELECTORS, MagicMock())

        self.assertEqual(len(reads), 1, "기준 부재는 '예산 소진'이 아니다")

    def test_a_skipped_observation_is_recorded(self):
        """조용히 꺼지면 표본의 공백을 '관측 대상 없음' 으로 오독한다 — 건너뜀도 남긴다."""
        import logging

        root = logging.getLogger()
        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Collector()
        root.addHandler(handler)
        self.addCleanup(lambda: root.removeHandler(handler))
        previous = root.level
        root.setLevel(logging.INFO)
        self.addCleanup(lambda: root.setLevel(previous))

        driver = MagicMock()
        driver.page_source = _official_html()
        context = MagicMock()
        context.__enter__.return_value = driver
        element = MagicMock()
        element.text = "1382.0"
        wait = MagicMock()
        wait.until.return_value = element

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk.crud, "insert_bank_rates_into_db", return_value=1):
            ibk.crawl_and_save_ibk_routine_selenium(
                "http://x", ibk.IBK_BANK_SELECTORS, MagicMock(),
                run_started_at=time.monotonic() - (ibk.SHADOW_BUDGET_GUARD_SECONDS + 10))

        self.assertIn("IBK_SELENIUM_SHADOW_OBSERVATION_SKIPPED", records)
        self.assertNotIn("IBK_SELENIUM_SHADOW_OBSERVATION", records)

    def test_the_save_happens_before_the_observation(self):
        """순서가 뒤집히면 위 보장이 무너진다 — 순서 자체를 잠근다."""
        order = []
        driver = MagicMock()
        driver.page_source = _official_html()
        context = MagicMock()
        context.__enter__.return_value = driver
        element = MagicMock()
        element.text = "1382.0"
        wait = MagicMock()
        wait.until.return_value = element

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "selenium_driver_context", return_value=context), \
             patch.object(ibk, "WebDriverWait", return_value=wait), \
             patch.object(ibk, "_emit_selenium_shadow_observation",
                          side_effect=lambda *a, **k: order.append("observe")), \
             patch.object(ibk.crud, "insert_bank_rates_into_db",
                          side_effect=lambda **k: (order.append("save"), 1)[1]):
            ibk.crawl_and_save_ibk_routine_selenium("http://x", ibk.IBK_BANK_SELECTORS, MagicMock())

        self.assertEqual(order, ["save", "observe"])

    def test_a_logging_failure_does_not_change_the_save_path(self):
        """기록이 실패해도 저장은 legacy 와 같아야 한다.

        ⛔ 관측기 내부 방어만으로는 부족하다 — 로그 직렬화·sink 는 관측기 **밖**이라
           경계에서 한 번 더 삼키지 않으면 legacy 저장이 MIBANK 폴백으로 바뀐다.
        """
        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "legacy"):
            baseline = self._run_selenium_routine().call_args.kwargs["current_rates"]

        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk.logger, "info", side_effect=RuntimeError("sink down")):
            write = self._run_selenium_routine()

        write.assert_called_once()
        self.assertEqual(write.call_args.kwargs["current_rates"], baseline)

    def test_a_comparison_failure_does_not_escape_the_boundary(self):
        """비교 함수가 터져도 수집으로 새어 나가면 안 된다."""
        with patch.object(ibk_selenium_config, "VALIDATION_MODE", "shadow"), \
             patch.object(ibk, "compare_mappings", side_effect=RuntimeError("compare boom")):
            write = self._run_selenium_routine()

        write.assert_called_once()

    def test_shadow_saves_the_same_rates_as_legacy(self):
        """관측을 켜도 저장 인자가 달라지면 안 된다."""
        seen = {}
        for mode in ("legacy", "shadow"):
            with patch.object(ibk_selenium_config, "VALIDATION_MODE", mode):
                write = self._run_selenium_routine()
            seen[mode] = write.call_args.kwargs["current_rates"]

        self.assertEqual(seen["legacy"], seen["shadow"])


if __name__ == "__main__":
    unittest.main()
