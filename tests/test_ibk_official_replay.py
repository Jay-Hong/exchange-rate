"""IBK 공식 응답 계약 — **실제 캡처 원본**으로 재현한다.

리포의 다른 IBK 테스트는 HTML 을 코드로 조립한다. 그러면 파서가 쓰는 CSS 셀렉터
(`#inDate`, `p.standard`)와 실제 마크업이 어긋나도 **어떤 테스트도 실패하지 않는다** — fixture 가
셀렉터에 맞춰 만들어지기 때문이다. 여기서는 2026-09-09 에 받은 실제 응답 원본을 그대로 재생한다.

⛔ **경계**: `RealCaptures*` 는 실제로 관측된 상태만 다룬다. `InjectedFailures` 는 그 원본을
   코드로 훼손해 만든 **인위적 실패**다. 사이트에서 관측한 적 없는 상태를 관측 사실처럼 쓰지 않는다.
⛔ 이 테스트는 네트워크를 쓰지 않는다. DB·Redis·알림에도 연결하지 않는다.
⛔ 여기서 잠그지 않는 것: 운영 Selenium 루틴의 저장 판단. 정확한 경계는 **"strict 파싱 결과를
   저장 허용·거부에 쓰지 않는다"** 이다 — `crawl_and_save_ibk_routine_selenium` 은 고정 셀렉터로
   읽어 먼저 저장하고, shadow 가 켜져 있으면 저장 **뒤에** 같은 파서로 관측만 한다
   (`_observe_selenium_capture` 가 `_parse_ibk_official_response` 를 호출한다). 아래 stale 시험은
   **strict 계약이 그 상태를 거부한다**는 것이지 운영 저장이 막힌다는 뜻이 아니다.
   저장 전 차단은 배선 slice 의 몫이다.
"""

import datetime
import gzip
import json
import pathlib
import unittest

from app.crawlers import ibk

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ibk"

# 2026-09-07 Playwright 관측과 2026-09-09 HTTP·Selenium 캡처가 함께 가리키는 값.
OBSERVED_20260904 = {"usd-krw": 1351.80, "jpy-krw": 865.26, "eur-krw": 1569.85}
OBSERVED_20260904_COMPLETED = "06:00:02"
OBSERVED_20260909 = {"usd-krw": 1336.00, "jpy-krw": 871.27, "eur-krw": 1554.30}
OBSERVED_20260909_COMPLETED = "14:24:52"

DAY_0904 = datetime.date(2026, 9, 4)
DAY_0909 = datetime.date(2026, 9, 9)
DAY_0717 = datetime.date(2026, 7, 17)
# 캡처 시각보다 뒤. 완료시각 미래 판정이 발화하지 않는 기준시각이다.
REFERENCE = ibk.KST.localize(datetime.datetime(2026, 9, 9, 18, 0))


def fixture(name: str) -> str:
    return gzip.decompress((FIXTURES / f"{name}.html.gz").read_bytes()).decode("utf-8")


class Captured:
    """`_parse_ibk_official_response` 가 읽는 것은 `.text` 뿐이다."""

    def __init__(self, text):
        self.text = text


def parse(html, query_date, reference_time=REFERENCE):
    return ibk._parse_ibk_official_response(
        Captured(html), query_date=query_date, reference_time=reference_time)


class TheFixturesAreRealCaptures(unittest.TestCase):
    def test_the_manifest_says_what_each_fixture_is(self):
        manifest = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        named = {row["fixture"] for row in manifest["fixtures"]}
        on_disk = {p.name for p in FIXTURES.glob("*.html.gz")}
        self.assertEqual(named, on_disk, "manifest 와 파일이 어긋난다")
        for row in manifest["fixtures"]:
            self.assertTrue(row["설명"] and row["취득"] and row["원본 sha256"])

    def test_a_fixture_still_hashes_to_what_was_captured(self):
        import hashlib
        manifest = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        for row in manifest["fixtures"]:
            raw = gzip.decompress((FIXTURES / row["fixture"]).read_bytes())
            with self.subTest(fixture=row["fixture"]):
                self.assertEqual(len(raw), row["원본 bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), row["원본 sha256"])


class RealCapturesParseAsObserved(unittest.TestCase):
    """정상 화면은 통과한다 — 그리고 관측된 값 그대로 나온다."""

    def test_the_selectors_the_parser_depends_on_exist_in_real_markup(self):
        # 이 단언이 조립 fixture 로는 성립할 수 없는 지점이다.
        from bs4 import BeautifulSoup
        for name in ("http_today_get", "http_dated_20260904",
                     "selenium_after_submit_20260904"):
            with self.subTest(fixture=name):
                soup = BeautifulSoup(fixture(name), "html.parser")
                self.assertIsNotNone(soup.select_one(ibk.INPUT_SELECTOR), "#inDate 없음")
                self.assertIsNotNone(soup.select_one(ibk.INPUT_SELECTOR).get("value"),
                                     "#inDate 에 value 속성 없음")
                self.assertIsNotNone(soup.select_one("p.standard"), "p.standard 없음")
                self.assertIsNotNone(ibk._find_official_rate_table(soup), "고시 표 없음")

    def test_the_today_get_response_yields_the_observed_rates(self):
        rates, completed = parse(fixture("http_today_get"), DAY_0909)
        self.assertEqual(rates, OBSERVED_20260909)
        self.assertEqual(completed, OBSERVED_20260909_COMPLETED)

    def test_the_dated_post_response_yields_the_observed_rates(self):
        rates, completed = parse(fixture("http_dated_20260904"), DAY_0904)
        self.assertEqual(rates, OBSERVED_20260904)
        self.assertEqual(completed, OBSERVED_20260904_COMPLETED)

    def test_a_submitted_selenium_page_source_yields_the_same_values_as_http(self):
        # 같은 서비스일을 HTTP 와 Selenium 두 경로로 받아 값이 같은지 본다.
        http_rates, http_completed = parse(fixture("http_dated_20260904"), DAY_0904)
        dom_rates, dom_completed = parse(fixture("selenium_after_submit_20260904"), DAY_0904)
        self.assertEqual(dom_rates, http_rates)
        self.assertEqual(dom_completed, http_completed)
        self.assertEqual(dom_rates, OBSERVED_20260904)

    def test_the_no_session_response_is_reported_as_no_session_not_as_an_error(self):
        self.assertIsNone(parse(fixture("http_dated_no_session_20260717"), DAY_0717))

    def test_currencies_outside_the_contract_are_ignored_rather_than_failing(self):
        # 실제 표에는 CNY 등이 함께 있다. 계약 밖 통화가 파싱을 깨뜨리면 안 된다.
        self.assertIn("CNY", fixture("http_dated_20260904"))
        self.assertEqual(set(parse(fixture("http_dated_20260904"), DAY_0904)[0]),
                         set(OBSERVED_20260904))


class RealCapturesRejectTheStaleScreen(unittest.TestCase):
    """날짜만 바뀌고 표는 이전 상태인 화면을 거부한다.

    2026-09-07 관측이 서술만 남긴 상태를, 2026-09-09 에 운영과 같은 `send_keys` 제스처로
    `page_source` 까지 캡처했다. 입력 property 는 2026.09.04 였지만 직렬화된 `value` **속성**은
    서버 렌더 날짜 2026.09.09 로 남는다.
    """

    def test_the_typed_date_never_reaches_the_serialized_value_attribute(self):
        from bs4 import BeautifulSoup
        stale = BeautifulSoup(fixture("selenium_stale_typed_not_submitted"), "html.parser")
        self.assertEqual(stale.select_one(ibk.INPUT_SELECTOR).get("value"), "2026.09.09")
        submitted = BeautifulSoup(fixture("selenium_after_submit_20260904"), "html.parser")
        self.assertEqual(submitted.select_one(ibk.INPUT_SELECTOR).get("value"), "2026.09.04")

    def test_the_stale_screen_is_rejected_for_the_date_that_was_only_typed(self):
        with self.assertRaises(ValueError) as caught:
            parse(fixture("selenium_stale_typed_not_submitted"), DAY_0904)
        self.assertIn("readback", str(caught.exception))

    def test_the_same_screen_passes_for_the_date_it_actually_serves(self):
        # ⛔ 양성 대조. 이게 없으면 "무조건 거부하는" 검증기도 위 시험을 통과한다.
        rates, completed = parse(fixture("selenium_stale_typed_not_submitted"), DAY_0909)
        self.assertEqual(set(rates), set(OBSERVED_20260909))
        self.assertRegex(completed, r"^\d{2}:\d{2}:\d{2}$")

    def test_the_stale_table_carries_the_other_days_rates(self):
        # 거부하지 않으면 무엇이 저장됐을지 — 09.04 를 기대하며 09.09 값을 쓰게 된다.
        stale_rates, _ = parse(fixture("selenium_stale_typed_not_submitted"), DAY_0909)
        self.assertNotEqual(stale_rates, OBSERVED_20260904)


class InjectedFailures(unittest.TestCase):
    """⛔ 여기부터는 **인위적 주입**이다. 아래 상태를 사이트에서 관측한 적은 없다.

    실제 캡처를 코드로 훼손해 만든다. 손으로 쓴 HTML 이 아니라 실물에서 파생시키는 이유는,
    직접 조립한 fixture 는 형식 유효성부터 의심스럽기 때문이다.
    """

    def setUp(self):
        self.real = fixture("http_dated_20260904")

    def test_a_partial_table_is_rejected(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(self.real, "html.parser")
        table = ibk._find_official_rate_table(soup)
        removed = 0
        for row in table.find_all("tr"):
            if ibk._row_currency_code(row) == "EUR":
                row.decompose()
                removed += 1
        self.assertEqual(removed, 1, "주입 대조 — EUR 행을 실제로 하나 지웠어야 한다")
        with self.assertRaises(ValueError) as caught:
            parse(str(soup), DAY_0904)
        self.assertIn("필수 통화 누락", str(caught.exception))

    def test_a_completion_time_in_the_future_is_rejected(self):
        # ⛔ 기준시각을 잘못 잡으면 **원본도 같은 이유로 거부**되어 주입이 판정을 바꿨다는
        #    증거가 사라진다(초판이 그랬다). 조회기준일 D 의 00:00~07:59 완료시각은
        #    `_ibk_completion_kst` 가 D+1 로 해석하므로, 09.04 의 06:00:02 는 09.05 06:00:02 다.
        #    그래서 원본이 **통과하는** 기준시각을 잡고 주입본만 넘어가게 만든다.
        reference = ibk.KST.localize(datetime.datetime(2026, 9, 5, 7, 0))
        rates, completed = parse(self.real, DAY_0904, reference_time=reference)
        self.assertEqual(completed, OBSERVED_20260904_COMPLETED,
                         "양성 대조 — 이 기준시각에서 원본은 통과해야 한다")
        self.assertEqual(rates, OBSERVED_20260904)

        injected = self.real.replace(OBSERVED_20260904_COMPLETED, "07:59:59")
        self.assertNotEqual(injected, self.real, "주입 대조 — 치환이 실제로 일어났어야 한다")
        with self.assertRaises(ValueError) as caught:
            parse(injected, DAY_0904, reference_time=reference)
        self.assertIn("미래", str(caught.exception))

    def test_a_missing_rate_column_header_is_rejected(self):
        injected = self.real.replace("매매기준율", "매매기준율_변형")
        self.assertNotEqual(injected, self.real)
        with self.assertRaises(ValueError) as caught:
            parse(injected, DAY_0904)
        self.assertIn("헤더 누락", str(caught.exception))

    def test_a_missing_completion_line_is_rejected(self):
        injected = self.real.replace("고시완료", "고시__완료")
        self.assertNotEqual(injected, self.real)
        with self.assertRaises(ValueError) as caught:
            parse(injected, DAY_0904)
        self.assertIn("고시완료 시각 누락", str(caught.exception))

    def test_a_table_without_the_official_caption_is_not_treated_as_official(self):
        # ⛔ 이번 캡처의 정상 화면에는 `<table>` 이 **1개뿐**이다(무고시 화면은 0개).
        #    따라서 "여러 표 중 공식 표만 고른다" 는 축은 **이번 실물로 검증되지 않았다**.
        #    여기서 보는 것은 caption 이 어긋나면 공식 표로 치지 않는다는 것뿐이다.
        self.assertEqual(self.real.count("<table"), 1, "전제 — 실물에 표는 하나다")
        injected = self.real.replace(ibk.OFFICIAL_RATE_TABLE_CAPTION, "다른 표")
        self.assertNotEqual(injected, self.real)
        with self.assertRaises(ibk.IbkRateTableAbsentError):
            parse(injected, DAY_0904)


if __name__ == "__main__":
    unittest.main()
