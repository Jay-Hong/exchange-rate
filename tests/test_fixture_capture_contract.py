"""C1b synthetic registry, recorder, deidentification and roundtrip contracts."""

import json
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup, Tag

from tests._fixture_capture import citi_html, getter, mibank_html, mibank_row, no_network, official_html
from tools.fixture_capture.capture import capture_route
from tools.fixture_capture.deidentify import deidentify
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.limits import Deadline
from tools.fixture_capture.recorder import QueryRecorder
from tools.fixture_capture.registry import Registry, selector_tokens
from tools.fixture_capture.roundtrip import record_extraction, roundtrip


@pytest.fixture
def registry():
    return Registry()


def test_registry_exactly_derived_from_every_production_constant(registry):
    s = registry.sources
    constants = {*s.bs.BS_BANK_SELECTORS.values(), *s.citi.CITI_BANK_SELECTORS.values(),
                 s.citi.AFTER_CITI_BANK_SELECTORS, *s.citi.SECOND_CITI_BANK_SELECTORS.values(),
                 *s.utils.MIBANK_TABLE_SELECTORS, *s.utils.MIBANK_RATE_CELL_SELECTORS,
                 s.utils.MIBANK_CODE_LINK_SELECTOR, s.utils.MIBANK_FLAG_IMAGE_SELECTOR,
                 s.utils.MIBANK_HEADER_ROW_SELECTOR, s.utils.MIBANK_COUNTER_SELECTOR}
    assert set(registry.selectors) == constants
    tokens = [selector_tokens(value) for value in constants]
    assert registry.ids == set().union(*(ids for ids, classes in tokens)) == {"resultTable", "content", "tab01"}
    assert registry.classes == set().union(*(classes for ids, classes in tokens)) == {
        "box_contents1", "main_table", "content", "right", "counter", "rollsty01"}


@pytest.mark.parametrize("selector", ["", " div", "div ", "div:not(.secret)", "div + p", "div~p",
                                    "div,span", "*", "div[attr=x]", "div:nth-child(2n)",
                                    "div:nth-child(0)", "div:nth-of-type(2)", "div:nth-last-child(2)",
                                    "div:nth-last-of-type(2)", "div:unknown(2)",
                                    "div >> span", "div>", "div\\x", "div::before"])
def test_unsupported_selector_grammar_fails_closed(selector):
    with pytest.raises(CaptureError, match="selector_syntax"):
        selector_tokens(selector)


def test_supported_compound_grammar():
    assert selector_tokens('table#rates.a.b > tr:nth-child(2) td a[href*="currency="]') == ({"rates"}, {"a", "b"})


@pytest.mark.parametrize("method,args,kwargs", [
    ("select", ("a[href*=\"currency=\"]",), {}),
    ("find", ("tr",), {}), ("find_all", ("td",), {}),
    ("find_all", ("td",), {"recursive": True}),
    ("find_all", ("td",), {"recursive": 0}),
    ("find_parent", ("tr",), {"recursive": False}),
    ("select_one", ("span.counter",), {"limit": 1}),
    ("select_one", (".unregistered",), {}),
])
def test_actual_method_and_arguments_are_enforced_and_restored(registry, method, args, kwargs):
    soup = BeautifulSoup('<table><tr><td>1</td></tr></table>', 'html.parser')
    original = {name: getattr(Tag, name) for name in QueryRecorder.METHODS}
    with pytest.raises(CaptureError, match="unregistered_query"):
        with QueryRecorder(registry) as recorder:
            try:
                getattr(soup, method)(*args, **kwargs)
            except CaptureError:
                pass  # A reporting helper swallowed it; context exit must still fail.
            assert recorder.violations
    assert {name: getattr(Tag, name) for name in QueryRecorder.METHODS} == original


def test_label_lookup_swallowing_violation_still_rejects(registry, monkeypatch):
    original = registry.sources.bank_report._label_candidates

    def swallowed(element, item, row):
        try:
            element.select_one(".unregistered")
        except Exception:
            pass
        return original(element, item, row)
    monkeypatch.setattr(registry.sources.bank_report, "_label_candidates", swallowed)
    get, _ = getter(official_html())
    with pytest.raises(CaptureError, match="unregistered_query"):
        capture_route("bs_official", registry=registry, get=get)


@pytest.mark.parametrize("route,html", [
    ("bs_official", official_html()), ("citi_primary", citi_html()),
    ("citi_secondary", official_html("citi_secondary")),
    ("bs_mibank", mibank_html()), ("citi_mibank", mibank_html()),
])
def test_all_five_routes_roundtrip_with_labels_and_queries(registry, route, html):
    get, response = getter(html)
    artifact = capture_route(route, registry=registry, get=get)
    meta = json.loads(artifact.metadata)
    assert meta["route"] == route
    assert meta["recorded_extraction"]["exception"] is None
    assert meta["recorded_extraction"]["events"][-1]["kind"] == "loop_completed"
    observed = [e for e in meta["recorded_extraction"]["events"] if e["kind"] == "observed"]
    assert len(observed) == 3
    assert all(e["labels"] for e in observed)
    assert any(q["method"] == "find_parent" for q in meta["recorded_extraction"]["queries"])
    assert response.raw.closed
    assert get.call_count == 1


def test_deidentification_removes_sensitive_attributes_and_bodies(registry):
    secret = "UserSession0123456789abcdef"
    rows = mibank_row(links=f'<a id="{secret}" href="https://host.invalid/{secret}?token={secret}&currency=uSd">USD</a>',
                      flag=f"https://host.invalid/{secret}/flag_jPy_s.png", rate_class="counter " + secret)
    extra = f'<aside><input type="hidden" value="{secret}"><meta content="{secret}"><link href="{secret}"></aside>'
    html = mibank_html(rows, extra=extra).replace('<table>', f'<table data-session="{secret}" onclick="{secret}">')
    html = html.replace('<tbody>', f'<tbody><!--{secret}--><script>{secret}</script><style>{secret}</style>')
    get, _ = getter(html)
    artifact = capture_route("bs_mibank", registry=registry, get=get)
    assert secret.encode() not in artifact.fixture + artifact.metadata
    assert b'https://host.invalid' not in artifact.fixture + artifact.metadata
    soup = BeautifulSoup(artifact.fixture, 'html.parser')
    assert soup.select_one('a')["href"] == "?currency=uSd"
    assert soup.select_one('img')["src"] == "flag_jPy_"
    assert soup.select_one('span')["class"] == ["counter"]
    assert not soup.select('input,meta,link')
    assert soup.select_one('script') is not None and soup.select_one('script').get_text() == ""
    assert soup.select_one('style').get_text() == ""


@pytest.mark.parametrize("links,flag,expected_code,basis", [
    ('<a href="?currency="></a><a href="?currency=USD"></a>', 'flag_jpy_s.png', 'JPY', 'flag_filename'),
    ('<a href="?currency=&currency=USD"></a>', 'flag_jpy_s.png', 'USD', 'explicit_code_param'),
    ('<a href="?currency=uSd&currency=&currency=EUR"></a>', None, 'USD', 'explicit_code_param'),
    ('<a href="?currency=&currency="></a>', 'flag_eUr.png', 'EUR', 'flag_filename'),
])
def test_currency_empty_duplicate_order_and_case_preserved(registry, links, flag, expected_code, basis):
    html = mibank_html(mibank_row(links=links, flag=flag))
    fixture, soup, record = roundtrip(html, registry.routes["bs_mibank"], registry, Deadline())
    observed = next(event for event in record["events"] if event["kind"] == "observed")
    assert observed["facts"]["code"] == expected_code
    assert observed["facts"]["code_basis"] == basis
    assert [a["href"] for a in soup.select('a')] == [a["href"] for a in BeautifulSoup(links, 'html.parser').select('a')]


def test_invalid_first_currency_link_rejects_instead_of_selecting_next(registry):
    row = mibank_row(links='<a href="?currency=US1"></a><a href="?currency=USD"></a>', flag='flag_jpy_s.png')
    with pytest.raises(CaptureError, match="roundtrip_mismatch"):
        roundtrip(mibank_html(row), registry.routes["bs_mibank"], registry, Deadline())


@pytest.mark.parametrize("span", ["0", "21", "-1", "1.5", "no", ""])
@pytest.mark.parametrize("name", ["colspan", "rowspan"])
def test_out_of_range_spans_reject(registry, span, name):
    html = official_html().replace('<td>', f'<td {name}="{span}">', 1)
    with pytest.raises(CaptureError, match="span_out_of_range"):
        roundtrip(html, registry.routes["bs_official"], registry, Deadline())


@pytest.mark.parametrize("span", ["1", "20", "01", "+1", " 20 "])
def test_valid_spans_preserve_value_and_labels(registry, span):
    html = official_html().replace('<td>', f'<td colspan="{span}">', 1)
    fixture, _, record = roundtrip(html, registry.routes["bs_official"], registry, Deadline())
    assert f'colspan="{span}"'.encode() in fixture
    assert record["events"][0]["labels"]["header_has_span"] is False


@pytest.mark.parametrize("tag", ["script", "style", "noscript", "template"])
def test_boundary_bodies_cleared_without_deleting_sibling(registry, tag):
    html = citi_html(extra=f'<{tag}>GhJkLmNpQrStUvWx</{tag}>')
    fixture, soup, _ = roundtrip(html, registry.routes["citi_primary"], registry, Deadline())
    assert b'GhJkLmNpQrStUvWx' not in fixture
    assert soup.select_one(tag) is not None and not soup.select_one(tag).contents


def test_clearing_label_content_changes_roundtrip_and_rejects(registry):
    html = official_html().replace('USD', '<noscript>USD</noscript>', 1)
    with pytest.raises(CaptureError, match="roundtrip_mismatch"):
        roundtrip(html, registry.routes["bs_official"], registry, Deadline())


def test_paths_detect_selection_change_even_with_identical_return(registry):
    row = mibank_row(links='<a href="?currency=USD"></a><a href="?currency=USD"></a>')

    def change_selection(soup, registry):
        result = deidentify(soup, registry)
        del result.select_one('a')["href"]
        return result
    with patch('tools.fixture_capture.roundtrip.deidentify', change_selection):
        with pytest.raises(CaptureError, match="recorded_extraction.queries: roundtrip_mismatch"):
            roundtrip(mibank_html(row), registry.routes["bs_mibank"], registry, Deadline())


def test_full_events_no_report_caps_or_label_clipping(registry):
    label = '긴 라벨 ' * 100
    rows = ''.join(mibank_row(rate=str(1300 + i), links=f'<a href="?currency=USD">{label}</a>') for i in range(12))
    _, _, record = roundtrip(mibank_html(rows), registry.routes["bs_mibank"], registry, Deadline())
    observed = [e for e in record["events"] if e["kind"] == "observed"]
    assert len(observed) == 12
    assert len(observed[0]["labels"]["row_text"]) > registry.sources.bank_report.SNIPPET_MAX_CHARS
    assert [e["facts"]["rate"] for e in observed] == list(range(1300, 1312))
    assert record["returned"][0] == {"usd-krw": 1311.0}


@pytest.mark.parametrize("header,cells,branch,reason", [
    ('<th>통화</th><th>기준환율</th>', '<td><span class="counter">1300</span></td>', 'header_index', None),
    ('<th>통화</th><th>다른 값</th><th>기준환율</th>', '<td class="right counter rollsty01">1300</td>', 'fallback', 'row_cells_insufficient'),
    (None, '<td class="right counter rollsty01">1300</td>', 'fallback', 'column_index_unresolved'),
])
def test_mibank_all_value_branches_are_recorded(registry, header, cells, branch, reason):
    get, _ = getter(mibank_html(mibank_row(cells=cells), header=header))
    record = json.loads(capture_route('bs_mibank', registry=registry, get=get).metadata)['recorded_extraction']
    event = next(e for e in record['events'] if e['kind'] == 'observed')
    assert event['facts']['value_basis']['branch'] == branch
    assert event['facts']['value_basis'].get('reason') == reason


def test_parse_exception_and_prior_events_are_reproduced(registry):
    html = mibank_html(mibank_row(rate='1300') + mibank_row(rate='broken'))
    get, _ = getter(html)
    record = json.loads(capture_route('bs_mibank', registry=registry, get=get).metadata)['recorded_extraction']
    assert [e['kind'] for e in record['events']] == ['table_structure', 'observed', 'parse_error']
    assert record['exception']['type'] == 'ValueError'
    assert record['exception']['args'] == ["could not convert string to float: 'broken'"]
    assert record['exception']['site']
    assert record['returned'] is None


def test_missing_terminal_callback_is_incomplete(registry, monkeypatch):
    monkeypatch.setattr(registry, 'extract', lambda route, soup, on_event: {})
    with pytest.raises(CaptureError, match='incomplete_events'):
        record_extraction(BeautifulSoup('', 'html.parser'), registry.routes['bs_official'], registry)


def test_citi_overwrite_order_and_parse_failures_are_preserved(registry):
    html = citi_html(labels=('USD JPY', 'EUR', 'USD', 'JPY'))
    html = html.replace('<span>1301</span>', '<span>broken</span>')
    get, _ = getter(html)
    record = json.loads(capture_route('citi_primary', registry=registry, get=get).metadata)['recorded_extraction']
    assert [event['kind'] for event in record['events']] == [
        'observed', 'observed', 'parse_error', 'observed', 'observed', 'loop_completed']
    assert record['returned'] == {'usd-krw': 1302.0, 'jpy-krw': 1303.0}


def test_mibank_empty_header_cell_does_not_fallback(registry):
    row = mibank_row(cells='<td>-</td><td class="right counter rollsty01">1300</td>')
    get, _ = getter(mibank_html(row))
    record = json.loads(capture_route('bs_mibank', registry=registry, get=get).metadata)['recorded_extraction']
    event = next(e for e in record['events'] if e['kind'] == 'empty_value')
    assert event['facts']['value_basis']['branch'] == 'header_index'
    assert record['returned'] == [{}, ['USD']]
