"""C1d B/C: replay compatibility and evidence limited to this fixture's scope."""

import copy
import json
import re
import unicodedata

from bs4 import Tag

from tests import bank_capture_gate
from tools.fixture_capture import detector as d
from tools.fixture_capture.admission import require
from tools.fixture_capture.deidentify import deidentify
from tools.fixture_capture.queries import query_key
from tools.fixture_capture.roundtrip import record_extraction


def normalize_recording(record):
    """Ignore only exception line numbers; never sort frames/events/queries."""
    result = copy.deepcopy(record)
    if result["exception"] is not None:
        result["exception"]["site"] = [frame[:2] for frame in result["exception"]["site"]]
    return result


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def replay(evidence, registry):
    return record_extraction(evidence.soup(), registry.routes[evidence.route], registry)


def validate_registry(record, registry, *, current_sites):
    """B only. A already established closed shapes, types and page-string scans."""
    for event in record["events"]:
        facts = event["facts"]
        if "selector" in facts:
            require(facts["selector"] in registry.evidence_selectors, "B.selector")
        if "order" in facts:
            require(facts["order"] in registry.sources.citi.CITI_BANK_SELECTORS, "B.order")
        if "matched_code" in facts:
            require(facts["matched_code"] in registry.sources.citi.CURRENCY_TEXTS, "B.matched_code")
        basis = facts.get("value_basis", {})
        if basis.get("branch") == "fallback":
            require(basis["selector"] is None or basis["selector"] in
                    registry.sources.utils.MIBANK_RATE_CELL_SELECTORS, "B.fallback_selector")
    for query in record["queries"]:
        require(query_key(query["method"], query["args"], query["kwargs"]) in registry.allowed_queries,
                "B.query")
    if current_sites and record["exception"] is not None:
        d._exception_record(record["exception"], registry, "B.current_exception_site")


def validate_compatibility(evidence, registry, current=None):
    stored = evidence.metadata["recorded_extraction"]
    current = replay(evidence, registry) if current is None else current
    validate_registry(stored, registry, current_sites=False)
    d.validate_recording(current, registry.routes[evidence.route], registry)
    require(canonical_json(normalize_recording(stored)) == canonical_json(normalize_recording(current)),
            "B.replay")
    # 고정점은 같은 파싱 결과끼리 비교한다. bs4 는 공백만 있는 문자열을 줄바꿈 하나로 접으므로, 캡처 때 지운 요소 양옆의
    # 줄바꿈이 원본 바이트에서는 둘, 재파싱 뒤에는 하나가 된다 — 원본 바이트와 비교하면 비식별과 무관하게 어긋난다.
    parsed = evidence.soup()
    require(deidentify(parsed, registry).encode("utf-8") == parsed.encode("utf-8"), "B.idempotence")


def _element(soup, path):
    element = soup
    for index in path:
        element = [child for child in element.children if isinstance(child, Tag)][index]
    return element


def _jpy_hundred(text):
    # A bounded textual search, not a general unit inference policy. Accept
    # whitespace/fullwidth digits and the spellings relevant to these pages.
    text = unicodedata.normalize("NFKC", text)
    return re.search(r"(?:100\s*(?:엔|円|yen|JPY)|(?:JPY|엔|円|yen)\s*[:(/]?\s*100)", text, re.I)


def validate_contract_evidence(evidence, registry):
    """C: re-extract + _label_candidates (via recorder), never stored metadata.

    Header text, cell index and spans are separate facts. They do NOT establish
    that the value belongs under that header; column correspondence is deferred.
    Unit absence below means only 'not found in this fixture's page text'.
    """
    current = replay(evidence, registry)
    require(current["exception"] is None, "C.extraction")
    observed = [event for event in current["events"] if event["kind"] == "observed"]
    require(len(observed) == 3 and {e["facts"]["pair"] for e in observed}
            == {"usd-krw", "jpy-krw", "eur-krw"}, "C.three_pairs")
    by_pair = {event["facts"]["pair"]: event for event in observed}
    soup = evidence.soup()
    if evidence.route in ("bs_official", "citi_secondary"):
        header = "매매 기준율" if evidence.route == "bs_official" else "고시 기준율"
        for event in observed:
            labels = event["labels"]
            require(header in labels.get("header_text", ""), "C.header_text")
            require(labels.get("cell_index") == 1, "C.cell_index")
            require(labels.get("header_has_span") is True, "C.header_span")
        if evidence.route == "bs_official":
            for pair, label in (("usd-krw", "(USD)"), ("jpy-krw", "(JPY(100))"), ("eur-krw", "(EUR)")):
                require(label in by_pair[pair]["labels"]["row_text"], "C.row_label")
        else:
            require("100엔" in by_pair["jpy-krw"]["labels"]["row_text"], "C.row_unit")
            # Currency-list text is outside _label_candidates; inspect its DOM.
            require(any("[JPY] 일본 100 엔" in node.get_text(" ", strip=True)
                        for node in soup.select("option")), "C.page_currency_list")
    elif evidence.route == "citi_primary":
        for pair in by_pair:
            require(f"({pair[:3].upper()})" in by_pair[pair]["labels"].get("item_text", ""), "C.item_label")
        require(not _jpy_hundred(soup.get_text(" ", strip=True)), "C.fixture_page_text_no_jpy_100")
    else:
        structures = [event for event in current["events"] if event["kind"] == "table_structure"]
        require(len(structures) == 1, "C.table_structure")
        facts = structures[0]["facts"]
        require(facts["column_index"] == 8 and facts["column_basis"] == "label_found", "C.header_index")
        tbody = _element(soup, facts["tbody"]["element_path"])
        header = tbody.find_parent("table").select_one("thead tr")
        require(header is not None and len(header.find_all(["th", "td"], recursive=False)) == 9,
                "C.header_cell_count")  # Separate DOM assertion, not label metadata.
        for event in observed:
            labels, facts = event["labels"], event["facts"]
            require(labels.get("row_cell_count") == 9 and labels.get("row_has_span") is False,
                    "C.row_structure")
            require(facts["code"] == facts["pair"][:3].upper() and facts["code_basis"] == "flag_filename",
                    "C.code_basis")
        require(not _jpy_hundred(soup.get_text(" ", strip=True)), "C.fixture_page_text_no_jpy_100")
    return current


def check_compatibility(route, registry, *, root=None, reviews_root=None, admitted=None):
    """A and D1 precede B, including when a hold waives compatibility."""
    # Lazy import breaks the contract/hold dependency cycle.
    from tests.bank_capture_hold import hold_present

    evidence = bank_capture_gate.admitted_evidence(
        route, root=root, reviews_root=reviews_root, admitted=admitted)
    if not hold_present(route, root):
        validate_compatibility(evidence, registry)


def check_scoped_evidence(route, registry, *, root=None, reviews_root=None, admitted=None):
    """Judge C on the same snapshot that passed A and D1."""
    evidence = bank_capture_gate.admitted_evidence(
        route, root=root, reviews_root=reviews_root, admitted=admitted)
    return validate_contract_evidence(evidence, registry)
