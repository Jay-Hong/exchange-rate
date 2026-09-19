"""C1a — 순수 추출 함수(받은 HTML → 값). 요청·DB·Redis 없이 사건 콜백만 부른다.

⛔ 합성 HTML 은 실제 페이지의 의미를 주장하지 않는다(bs·citi 보고 시험과 같은 원칙).
운영 루틴의 로그·보고 관측자 호출이 리팩터 전과 같은지는 `tests/test_bank_report.py` 가 잠근다. 여기서는 추출 함수 자체의 계약을 잠근다:
사건 순서, 중간 예외 때 이미 부른 사건의 범위, 추출 함수 스스로는 로그를 남기지 않음(fixture 캡처 경로에 원문 로그가 들어오지 않게).
"""

import logging

import pytest
import requests
from bs4 import BeautifulSoup

from app.crawlers import citi, utils
from tests.test_bank_report import (
    BS_NORMAL, CITI_NORMAL, MIBANK_NORMAL, bs_html, citi_first_html, mibank_html, mrow)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real I/O forbidden")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(utils.requests, "get", forbidden)


class Events:
    def __init__(self):
        self.seen = []

    def __call__(self, kind, **facts):
        self.seen.append((kind, facts))

    def kinds(self):
        return [kind for kind, _ in self.seen]


def soup(html):
    return BeautifulSoup(html, "html.parser")


@pytest.fixture
def no_logs(caplog):
    caplog.set_level(logging.DEBUG)
    yield
    assert caplog.records == [], "추출 함수는 로그를 남기지 않는다 — 로그는 호출자 콜백의 몫"


# --- extract_selector_rates (bs 공식·citi 2차) ------------------------------------------------------

def test_selector_rates_normal_order(no_logs):
    events = Events()
    rates = utils.extract_selector_rates(soup(bs_html(BS_NORMAL)), utils_selectors(), events)
    assert rates == {"usd-krw": 1393.5, "jpy-krw": 942.64, "eur-krw": 1641.12}
    assert events.kinds() == ["observed", "observed", "observed", "loop_completed"]
    kind, facts = events.seen[0]
    assert facts["pair"] == "usd-krw" and facts["rate_text"] == "1,393.50" and facts["rate"] == 1393.5
    assert facts["selector"] == utils_selectors()["usd-krw"] and facts["element"].name == "td"


def test_selector_rates_miss_and_parse_error_continue(no_logs):
    events = Events()
    html = bs_html([("미국 USD", "N/A"), BS_NORMAL[1]])
    rates = utils.extract_selector_rates(soup(html), utils_selectors(), events)
    assert rates == {"jpy-krw": 942.64}
    assert events.kinds() == ["parse_error", "observed", "selector_miss", "loop_completed"]
    assert events.seen[0][1] == {"pair": "usd-krw", "selector": utils_selectors()["usd-krw"], "rate_text": "N/A"}
    assert events.seen[2][1] == {"pair": "eur-krw", "selector": utils_selectors()["eur-krw"]}


def test_selector_rates_exception_mid_loop_keeps_earlier_events(no_logs):
    """중간 예외는 그대로 전파되고, 그 전 사건은 이미 불렸다 — `loop_completed` 는 없다."""
    events = Events()
    page = soup(bs_html(BS_NORMAL))
    original = page.select_one
    calls = []

    def select_one(selector):
        calls.append(selector)
        if len(calls) == 2:
            raise RuntimeError("dom broke")
        return original(selector)

    page.select_one = select_one
    with pytest.raises(RuntimeError, match="dom broke"):
        utils.extract_selector_rates(page, utils_selectors(), events)
    assert events.kinds() == ["observed"]


def utils_selectors():
    from app.crawlers import bs
    return bs.BS_BANK_SELECTORS


# --- extract_citi_items (citi 1차) --------------------------------------------------------------

def test_citi_items_normal_and_non_required_item(no_logs):
    events = Events()
    rates = citi.extract_citi_items(soup(citi_first_html(CITI_NORMAL)), citi.CITI_BANK_SELECTORS, events)
    assert rates == {"usd-krw": 1393.5, "eur-krw": 1641.12, "jpy-krw": 942.64}
    assert events.kinds() == ["observed", "observed", "observed", "loop_completed"]
    assert [f["matched_code"] for k, f in events.seen if k == "observed"] == ["USD", "EUR", "JPY"]
    assert [f["order"] for k, f in events.seen if k == "observed"] == ["1st", "3rd", "4th"]


def test_citi_item_with_two_codes_attributes_same_element_to_both(no_logs):
    events = Events()
    items = [("USD JPY", "1,393.50")] + CITI_NORMAL[1:3]
    rates = citi.extract_citi_items(soup(citi_first_html(items)), citi.CITI_BANK_SELECTORS, events)
    observed = [(f["pair"], f["order"], f["element"]) for k, f in events.seen if k == "observed"]
    assert observed[0][:2] == ("usd-krw", "1st") and observed[1][:2] == ("jpy-krw", "1st")
    assert observed[0][2] is observed[1][2]
    assert rates["usd-krw"] == rates["jpy-krw"] == 1393.5
    # 1st: USD·JPY 둘 다 귀속 / 2nd(CNY): 필수 통화 문자열 없음 → 사건 없음 / 3rd: EUR / 4th: 항목 없음
    assert events.kinds() == ["observed", "observed", "observed", "item_miss", "loop_completed"]


def test_citi_later_item_overwrites_same_code(no_logs):
    events = Events()
    items = [("미국(USD)", "1,393.50"), ("미국(USD)", "1,400.00")]
    rates = citi.extract_citi_items(soup(citi_first_html(items)), citi.CITI_BANK_SELECTORS, events)
    assert rates == {"usd-krw": 1400.0}
    assert [f["rate"] for k, f in events.seen if k == "observed"] == [1393.5, 1400.0]
    assert events.kinds().count("item_miss") == 2


def test_citi_missing_span_and_parse_error(no_logs):
    events = Events()
    html = ('<div id="content"><ul>'
            '<li><div><div>미국(USD)</div><div></div></div></li>'
            '<li><div><div>유럽(EUR)</div><div><span>abc</span></div></div></li>'
            '</ul></div>')
    rates = citi.extract_citi_items(soup(html), citi.CITI_BANK_SELECTORS, events)
    assert rates == {}
    assert events.seen[0] == ("selector_miss", {
        "order": "1st", "pair": "usd-krw",
        "selector": citi.CITI_BANK_SELECTORS["1st"] + citi.AFTER_CITI_BANK_SELECTORS})
    assert events.seen[1] == ("parse_error", {"order": "2nd", "pair": "eur-krw", "rate_text": "abc"})
    assert events.kinds()[2:] == ["item_miss", "item_miss", "loop_completed"]


# --- extract_mibank_rates -----------------------------------------------------------------------

def test_mibank_events_and_found_codes(no_logs):
    events = Events()
    rates, found = utils.extract_mibank_rates(soup(mibank_html(MIBANK_NORMAL)), ("USD", "JPY", "EUR"), events)
    assert rates == {"usd-krw": 1390.0, "jpy-krw": 940.0, "eur-krw": 1640.0}
    assert found == ["USD", "CNY", "JPY", "EUR"]
    assert events.kinds() == ["table_structure", "observed", "code_outside_required", "observed", "observed",
                              "loop_completed"]
    table = events.seen[0][1]
    assert table["column_index"] == 2 and table["column_basis"] == "label_found" and table["tbody"].name == "tbody"
    assert events.seen[2][1] == {"code": "CNY", "code_basis": "explicit_code_param"}
    jpy = events.seen[3][1]
    assert (jpy["pair"], jpy["code"], jpy["code_basis"], jpy["row_index"]) == ("jpy-krw", "JPY", "flag_filename", 2)


def test_mibank_empty_value_then_parse_error_propagates(no_logs):
    events = Events()
    rows = [mrow("USD", ["1,380.00", "-"]), mrow("JPY", ["930.00", "x.y"]), MIBANK_NORMAL[3]]
    with pytest.raises(ValueError):
        utils.extract_mibank_rates(soup(mibank_html(rows)), ("USD", "JPY", "EUR"), events)
    assert events.kinds() == ["table_structure", "empty_value", "parse_error"]
    assert events.seen[2][1]["rate_text"] == "x.y" and events.seen[2][1]["pair"] == "jpy-krw"


def test_mibank_duplicate_code_overwrites_and_reports_each_row(no_logs):
    events = Events()
    rows = MIBANK_NORMAL + [mrow("USD", ["1,381.00", "1,391.00"])]
    rates, found = utils.extract_mibank_rates(soup(mibank_html(rows)), ("USD", "JPY", "EUR"), events)
    assert rates["usd-krw"] == 1391.0
    assert [f["rate"] for k, f in events.seen if k == "observed" and f["pair"] == "usd-krw"] == [1390.0, 1391.0]
    assert found.count("USD") == 2


def test_mibank_missing_table_raises_before_any_event(no_logs):
    events = Events()
    with pytest.raises(RuntimeError, match="테이블"):
        utils.extract_mibank_rates(soup("<div>nothing</div>"), ("USD",), events)
    assert events.seen == []


def test_mibank_required_check_is_callers(no_logs):
    """필수 통화 누락은 추출 함수가 판정하지 않는다 — 운영은 디버그 로그 뒤에 `crawl_mibank_rates` 가 검사한다."""
    events = Events()
    rates, _ = utils.extract_mibank_rates(soup(mibank_html(MIBANK_NORMAL[:2])), ("USD", "JPY", "EUR"), events)
    assert rates == {"usd-krw": 1390.0}
    assert events.kinds()[-1] == "loop_completed"


def test_mibank_selector_constants_are_the_ones_used():
    """인라인 선택자를 상수로 올렸다 — fixture 캡처 도구가 등록부를 코드 상수에서 도출한다."""
    assert utils.MIBANK_CODE_LINK_SELECTOR == 'a[href*="currency="]'
    assert utils.MIBANK_FLAG_IMAGE_SELECTOR == 'img[src*="flag_"]'
    assert utils.MIBANK_HEADER_ROW_SELECTOR == "thead tr"
    assert utils.MIBANK_COUNTER_SELECTOR == "span.counter"
