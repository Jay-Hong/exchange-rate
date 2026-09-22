"""C1d: replay equality and explicitly scoped contract evidence."""

import copy
from dataclasses import replace
from functools import lru_cache
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from tests._fixture_capture import citi_html, getter, mibank_html, mibank_row, no_network, official_html
from tests.bank_capture_contract import (
    canonical_json, check_compatibility, check_scoped_evidence, normalize_recording, replay, validate_compatibility,
    validate_contract_evidence, validate_registry,
)
from tests.bank_capture_gate import ADMITTED_ROUTES
from tools.fixture_capture import capture
from tools.fixture_capture.admission import validate_integrity, validate_stored_recording
from tools.fixture_capture.capture import ROUTES
from tools.fixture_capture.deidentify import deidentify
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.registry import Registry
from tools.fixture_capture.roundtrip import record_extraction


@pytest.fixture
def registry():
    return Registry()


@pytest.mark.parametrize("route", ADMITTED_ROUTES)
def test_original_pair_current_compatibility(route, registry):
    check_compatibility(route, registry)
    # HoldSession records the call phase and enforces every used hold at finish.


@pytest.mark.parametrize("route", ADMITTED_ROUTES)
def test_original_pair_scoped_contract_evidence(route, registry):
    check_scoped_evidence(route, registry)


def synthetic_page(route):
    """C-passing pages built only from the synthetic capture helpers."""
    if route in ("bs_official", "citi_secondary"):
        header = "매매 기준율" if route == "bs_official" else "고시 기준율"
        html = official_html(route).replace("기준환율", header)
        html = html.replace(f"<th>{header}", f'<th colspan="1">{header}')
        if route == "bs_official":
            for code, label in (("USD", "미국(USD)"), ("JPY", "일본(JPY(100))"), ("EUR", "유로(EUR)")):
                html = html.replace(f"<td>{code}</td>", f"<td>{label}</td>")
        else:
            html = html.replace('<div id="tab01">',
                                '<div id="tab01"><select><option>[JPY] 일본 100 엔</option></select>')
            html = html.replace("<td>JPY</td>", "<td>JPY 100엔</td>")
        return html
    if route == "citi_primary":
        return citi_html(labels=("미국 (USD)", "중국 (CNY)", "유로 (EUR)", "일본 (JPY)"))
    header = "<th>통화</th>" + "".join(f"<th>항목{i}</th>" for i in range(7)) + "<th>기준환율</th>"
    rows = "".join(
        mibank_row(code, links=f'<a href="https://example.invalid/rates">{code}</a>',
                   flag=f"/img/flag_{code.lower()}_synthetic.png",
                   cells="<td>-</td>" * 7 + f'<td><span class="counter">{rate}</span></td>')
        for code, rate in (("USD", "1,300.25"), ("JPY", "900.5"), ("EUR", "1,500")))
    return mibank_html(rows=rows, header=header)


@lru_cache(maxsize=None)
def synthetic_artifact(route):
    get, _ = getter(synthetic_page(route))
    with patch.object(capture, "source_identity", lambda registry: ("a" * 40, "c1b/1:" + "b" * 64)):
        return capture.capture_route(route, registry=Registry(), get=get)


def synthetic_evidence(route):
    # Cache immutable Artifact bytes only; each caller gets fresh decoded metadata.
    artifact = synthetic_artifact(route)
    return validate_integrity(artifact.fixture, artifact.metadata, route)


def exception_record(registry):
    html = mibank_html(mibank_row(rate="broken"))
    return record_extraction(BeautifulSoup(html, "html.parser"), registry.routes["bs_mibank"], registry)


def test_historical_line_motion_passes_but_current_ast_is_checked(registry):
    current = exception_record(registry)
    historical = copy.deepcopy(current)
    for frame in historical["exception"]["site"]:
        frame[2] += 100000
    validate_stored_recording(historical, "bs_mibank")
    validate_registry(historical, registry, current_sites=False)
    assert normalize_recording(historical) == normalize_recording(current)
    validate_registry(current, registry, current_sites=True)
    with pytest.raises(CaptureError, match="B.current_exception_site"):
        validate_registry(historical, registry, current_sites=True)


@pytest.mark.parametrize("field", ["events", "queries", "returned", "exception_type", "exception_args", "frame_order", "function", "file"])
def test_normalization_drops_only_lines(registry, field):
    record = exception_record(registry)
    record["exception"]["site"] = [["utils.py", "outer", 1], ["utils.py", "inner", 2]]
    altered = copy.deepcopy(record)
    if field in ("events", "queries"):
        altered[field].reverse()
        if altered[field] == record[field]:
            altered[field].append(copy.deepcopy(altered[field][0]))
    elif field == "returned":
        altered[field] = {"usd-krw": 1}
    elif field.startswith("exception_"):
        altered["exception"][field.split("_", 1)[1]] = "TypeError" if field.endswith("type") else ["different"]
    elif field == "frame_order":
        altered["exception"]["site"].reverse()
    else:
        altered["exception"]["site"][0][1 if field == "function" else 0] = "different"
    assert canonical_json(normalize_recording(record)) != canonical_json(normalize_recording(altered))


def test_historical_selector_query_change_is_b_only(registry):
    evidence = synthetic_evidence("bs_official")
    record = copy.deepcopy(evidence.metadata["recorded_extraction"])
    record["events"][0]["facts"]["selector"] = "#historic td"
    validate_stored_recording(record, evidence.route)
    with pytest.raises(CaptureError, match="B.selector"):
        validate_registry(record, registry, current_sites=False)
    record = copy.deepcopy(evidence.metadata["recorded_extraction"])
    record["queries"][0]["args"] = ["#historic td"]
    validate_stored_recording(record, evidence.route)
    with pytest.raises(CaptureError, match="B.query"):
        validate_registry(record, registry, current_sites=False)


def test_idempotence_catches_safe_unregistered_attribute(registry):
    evidence = synthetic_evidence("bs_official")
    # Serialized once through the parser so the synthetic mutation below edits a stable baseline.
    evidence = replace(evidence, fixture=evidence.soup().encode("utf-8"))
    validate_compatibility(evidence, registry)
    mutated = replace(evidence, fixture=evidence.fixture.replace(b'<html>', b'<html id="old_token">', 1))
    # Attribute doesn't change the selected elements or numeric DOM paths.
    assert replay(mutated, registry) == replay(evidence, registry)
    with pytest.raises(CaptureError, match="B.idempotence"):
        validate_compatibility(mutated, registry)


@pytest.mark.parametrize("route", ROUTES)
def test_replay_mismatch_is_rejected_when_all_other_b_checks_pass(registry, route):
    evidence = synthetic_evidence(route)
    meta = copy.deepcopy(evidence.metadata)
    facts = next(e["facts"] for e in meta["recorded_extraction"]["events"] if e["kind"] == "observed")
    facts["rate_text"] += "0"  # Safe decimal text; leave all other recorded fields intact.
    mutated = validate_integrity(evidence.fixture, canonical_json(meta), route)
    current = replay(mutated, registry)
    assert current == replay(evidence, registry)
    validate_registry(meta["recorded_extraction"], registry, current_sites=False)
    validate_registry(current, registry, current_sites=True)
    parsed = mutated.soup()
    assert deidentify(parsed, registry).encode() == parsed.encode()
    with pytest.raises(CaptureError, match="B.replay"):
        validate_compatibility(mutated, registry)


@pytest.mark.parametrize("route", ["bs_official", "citi_secondary"])
def test_cell_index_is_not_implied_by_nth_child(registry, route):
    evidence = synthetic_evidence(route)
    soup = evidence.soup()
    selected = soup.select_one(registry.routes[route].selectors["usd-krw"])
    # nth-child still selects the same second element. The first element stops
    # being a table cell, so the rate is now cell 0; labels/header stay intact.
    selected.find_parent("tr").find(["td", "th"], recursive=False).name = "span"
    mutated = replace(evidence, fixture=soup.encode())
    assert replay(mutated, registry)["returned"] == replay(evidence, registry)["returned"]
    with pytest.raises(CaptureError, match="C.cell_index"):
        validate_contract_evidence(mutated, registry)


@pytest.mark.parametrize("code", ["USD", "JPY", "EUR"])
def test_citi_item_code_match_does_not_prove_parenthesized_label(registry, code):
    evidence = synthetic_evidence("citi_primary")
    mutated = replace(evidence, fixture=evidence.fixture.replace(f"({code})".encode(), code.encode()))
    assert replay(mutated, registry)["returned"] == replay(evidence, registry)["returned"]
    with pytest.raises(CaptureError, match="C.item_label"):
        validate_contract_evidence(mutated, registry)


@pytest.mark.parametrize("route", ["bs_mibank", "citi_mibank"])
@pytest.mark.parametrize("mutation", ["column_moved", "label_absent"])
def test_mibank_header_index_and_basis_are_required(registry, route, mutation):
    evidence = synthetic_evidence(route)
    soup = evidence.soup()
    table = soup.select_one('img[src="flag_jpy_"]').find_parent("table")
    header = table.select_one("thead tr").find_all(["th", "td"], recursive=False)
    if mutation == "column_moved":
        header[7].insert_before(header[8])
        for row in table.select("tbody tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) == 9:
                cells[7].insert_before(cells[8])
    else:
        header[8].string = "다른 환율"
    mutated = replace(evidence, fixture=soup.encode())
    assert replay(mutated, registry)["returned"] == replay(evidence, registry)["returned"]
    with pytest.raises(CaptureError, match="C.header_index"):
        validate_contract_evidence(mutated, registry)


@pytest.mark.parametrize("mutation,count", [("missing", 2), ("empty", 0),
                                           ("extra_duplicate", 4), ("duplicate_pair", 3)])
def test_exactly_three_distinct_pairs_are_required(registry, mutation, count):
    evidence = synthetic_evidence("citi_primary")
    html = evidence.fixture.decode()
    if mutation == "empty":
        for code in ("USD", "JPY", "EUR"):
            html = html.replace(code, "XYZ")
    elif mutation == "missing":
        html = html.replace("USD", "XYZ")
    else:
        html = html.replace("CNY", "USD")
        if mutation == "duplicate_pair":
            html = html.replace("JPY", "XYZ")
    mutated = replace(evidence, fixture=html.encode())
    current = replay(mutated, registry)
    assert current["exception"] is None
    assert sum(e["kind"] == "observed" for e in current["events"]) == count
    with pytest.raises(CaptureError, match="C.three_pairs"):
        validate_contract_evidence(mutated, registry)


@pytest.mark.parametrize("route,old,new,rule", [
    ("bs_official", "매매", "다른", "header_text"),
    ("bs_official", "(JPY(100))", "(JPY)", "row_label"),
    ("citi_secondary", "100엔", "엔", "row_unit"),
    ("citi_secondary", "[JPY] 일본 100 엔", "[JPY] 일본 엔", "page_currency_list"),
    ("citi_primary", "(JPY)", "(JPY) 100엔", "no_jpy_100"),
    ("bs_mibank", "</body>", "<p>JPY 100엔</p></body>", "no_jpy_100"),
])
def test_contract_uses_fresh_dom_not_unchanged_metadata(registry, route, old, new, rule):
    evidence = synthetic_evidence(route)
    assert old.encode() in evidence.fixture
    mutated = replace(evidence, fixture=evidence.fixture.replace(old.encode(), new.encode()))
    with pytest.raises(CaptureError, match=rule):
        validate_contract_evidence(mutated, registry)


def test_header_cells_are_separate_dom_evidence(registry):
    evidence = synthetic_evidence("bs_mibank")
    soup = evidence.soup()
    header = soup.select_one("table thead tr")
    header.append(soup.new_tag("th"))  # Empty cell leaves the label text/index unchanged.
    with pytest.raises(CaptureError, match="header_cell_count"):
        validate_contract_evidence(replace(evidence, fixture=soup.encode()), registry)


@pytest.mark.parametrize("route", ["bs_official", "citi_secondary"])
def test_header_span_is_required_in_this_fixture(registry, route):
    evidence = synthetic_evidence(route)
    soup = evidence.soup()
    selector = "#resultTable thead tr" if route == "bs_official" else "#tab01 table thead tr"
    for cell in soup.select_one(selector).find_all(["th", "td"], recursive=False):
        cell.attrs.pop("colspan", None)
        cell.attrs.pop("rowspan", None)
    with pytest.raises(CaptureError, match="header_span"):
        validate_contract_evidence(replace(evidence, fixture=soup.encode()), registry)


@pytest.mark.parametrize("mutation,rule", [("span", "row_structure"), ("link", "code_basis")])
@pytest.mark.parametrize("route", ["bs_mibank", "citi_mibank"])
def test_mibank_row_structure_and_code_basis(registry, route, mutation, rule):
    evidence = synthetic_evidence(route)
    soup = evidence.soup()
    flag = soup.select_one('img[src="flag_jpy_"]')
    assert flag is not None
    cell = flag.find_parent("td")
    if mutation == "span":
        cell["rowspan"] = "1"
    else:
        link = soup.new_tag("a", href="?currency=JPY")
        cell.append(link)
    with pytest.raises(CaptureError, match=rule):
        validate_contract_evidence(replace(evidence, fixture=soup.encode()), registry)
