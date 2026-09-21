"""D1 slice 5b-2 contract — admission layer A (ported from C1d) plus the D1 approval check. Read-only for the implementer.

Written before the implementation (Claude) from the slice-5b design agreement with Codex (r1–r2, 2026-09-21), amendment 1
§A2–§A3, and the C1d layer-A tests (worktree `wt-c1d`, `tests/test_bank_capture_integrity.py`). Evidence here is synthetic:
real `capture_route` Artifacts built from the synthetic pages (network refused, provenance pinned), then mutated in memory.

API — `tools/fixture_capture/admission.py` (layer A; imports neither `capture` nor `registry`):
- `EXTRACTORS` — the historical route → extractor map (the five routes).
- `load_json(raw, location)` — strict: no BOM, duplicate key, NaN/Infinity, overflow or undecodable bytes (`invalid_json`).
- `validate_structure(soup)`, `validate_stored_recording(record, route_name)` — as in C1d layer A.
- `validate_integrity(fixture, metadata_bytes, route_name) -> Evidence` — layer A only, no D1.
- `Evidence(route, fixture, metadata_bytes, metadata)` with `.fixture_id` and `.soup()`.
- `admit(fixture, metadata_bytes, route_name, approval_bytes) -> list` — layer A first, then the D1 check; returns the
  recomputed D1 findings (`[]` is a normal success; failures are exceptions). `approval_bytes=None` — and only that —
  means no approval file: bytes that parse to JSON `null` or any non-object are a malformed approval.
- `load_evidence(route, root)`; `approval_path(reviews_root, evidence) -> Path`
  (`<reviews_root>/<route>/<capture_id>.json`, from the validated ids); `read_approval(path) -> bytes | None`
  (None only when nothing exists at the path; a directory, symlink or unreadable file refuses).
API — `tools/fixture_capture/d1_approval.py` (never imports `admission`):
- `SCHEMA_VERSION == 1`, `DECISIONS == ("name_removed", "not_person_name_context")`.
- The current policy digest and review runtime are obtained on **every** check, through the module attributes
  `d1_digest.policy_descriptor()`, `d1_digest.policy_digest()` and `d1_digest.runtime_descriptor()` looked up at call time;
  a failure to obtain either refuses, with or without findings.
- `reviewer` is a reviewer handle `[A-Za-z0-9_.-]{1,64}` — it keeps page text out of the record; it does not guarantee
  that a handle is not someone's name.
- The approval JSON is closed: `schema_version, route, capture_id, fixture_sha256, metadata_sha256, policy_digest, runtime,
  items, reviewer, reviewed_at`; `runtime` is exactly the amendment-1 review-environment record; `items` is a list of
  `{"finding": <object>, "decision": <DECISIONS>}`.
Rules pinned here: `d1_approval_missing`, `d1_approval_schema`, `d1_approval_binding`, `d1_approval_items`,
`d1_approval_unreadable`, plus `d1_runtime_unavailable` (amendment A2) and the existing D1/A-layer codes.

Decision table (design r1): 0 findings + no file → admitted; 0 + file → the file is fully checked and must have `items == []`;
≥1 + no file → `d1_approval_missing`; ≥1 + file → full check and exact match. "Cannot check" is never 0 findings.
Findings are compared as `canonical_json` bytes of the list, in order and by type (`False` is not `0`).
The review runtime is validated for shape only — never compared with the current one — but the current runtime must be
obtainable on every check (amendment A2/A3).
"""

import copy
import json
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

import bs4
from tests._fixture_capture import citi_html, getter, mibank_html, mibank_row, no_network, official_html  # noqa: F401
from tools.fixture_capture import admission as A
from tools.fixture_capture import d1_approval as P
from tools.fixture_capture import capture, d1_digest
from tools.fixture_capture.d1_digest import canonical_json, policy_descriptor, policy_digest, runtime_descriptor
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.limits import HTML_LIMIT, METADATA_LIMIT
from tools.fixture_capture.registry import Registry

ROUTES = ("bs_official", "citi_primary", "citi_secondary", "bs_mibank", "citi_mibank")
PAGES = {"bs_official": official_html, "citi_primary": citi_html,
         "citi_secondary": lambda: official_html("citi_secondary"),
         "bs_mibank": mibank_html, "citi_mibank": mibank_html}
# Derived by hand from the synthetic citi page, not copied from findings(): html(0) > body(1) > the disclosure div, body's
# second child (1) > footer, its fifth child (4) > div, the footer's fifth child (4) > div (0) > ul, second (1) > li (0).
CITI_FINDING = {"rule_id": "d1_role_context", "source": "html_text", "owner_path": [0, 1, 1, 4, 4, 0, 1, 0],
                "segment_index": 0, "token": "대표자", "start": 0, "end": 3, "occurrence_index": 1, "total_count": 1}
EXPECTED = {"bs_official": [], "citi_primary": [CITI_FINDING], "citi_secondary": [CITI_FINDING],
            "bs_mibank": [], "citi_mibank": []}


@lru_cache(maxsize=None)
def _artifact(route):
    get, _ = getter(PAGES[route]())
    with patch.object(capture, "source_identity", lambda registry: ("a" * 40, "c1b/1:" + "b" * 64)):
        artifact = capture.capture_route(route, registry=Registry(), get=get)
    return artifact.fixture, artifact.metadata


def pair(route):
    return _artifact(route)


def meta_of(route):
    return json.loads(pair(route)[1])


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def refused(call, rule=None, location=None):
    with pytest.raises(CaptureError) as caught:
        call()
    if rule is not None:
        assert caught.value.rule == rule, (caught.value.rule, caught.value.location)
    if location is not None:
        assert caught.value.location == location, (caught.value.rule, caught.value.location)
    return caught.value


def approval(for_route, fixture=None, metadata=None, items=None, **overrides):
    # `for_route`, not `route`: overrides may replace the recorded "route" itself.
    fixture = pair(for_route)[0] if fixture is None else fixture
    metadata = pair(for_route)[1] if metadata is None else metadata
    record = {"schema_version": 1, "route": for_route, "capture_id": json.loads(metadata)["capture_id"],
              "fixture_sha256": sha(fixture), "metadata_sha256": sha(metadata),
              "policy_digest": policy_digest(policy_descriptor()), "runtime": runtime_descriptor(),
              "items": [{"finding": f, "decision": "name_removed"} for f in EXPECTED[for_route]] if items is None else items,
              "reviewer": "jay", "reviewed_at": "2026-09-22T00:00:00Z"}
    record.update(overrides)
    return record


def admit(route, record=None, fixture=None, metadata=None, raw=None):
    fixture = pair(route)[0] if fixture is None else fixture
    metadata = pair(route)[1] if metadata is None else metadata
    body = raw if raw is not None else (None if record is None else encode(record))
    return A.admit(fixture, metadata, route, body)


# ════ A. layer A, ported from C1d onto synthetic evidence ═════════════════════

def test_the_route_list_is_historical_and_owned_here():
    assert set(A.EXTRACTORS) == set(ROUTES)


@pytest.mark.parametrize("route", ROUTES)
def test_a_synthetic_capture_passes_layer_a(route):
    evidence = A.validate_integrity(*pair(route), route)
    assert evidence.fixture_id == f"{route}/{meta_of(route)['capture_id']}"


def test_a_missing_pair_member_fails_by_name(tmp_path):                          # C1d I18
    for route in ROUTES:
        refused(lambda: A.load_evidence(route, tmp_path), location=f"{route}/fixture.html.missing")
        (tmp_path / route).mkdir()
        (tmp_path / route / "fixture.html").write_bytes(pair(route)[0])
        refused(lambda: A.load_evidence(route, tmp_path), location=f"{route}/metadata.json.missing")
        (tmp_path / route / "metadata.json").write_bytes(pair(route)[1])
        assert A.load_evidence(route, tmp_path).route == route


@pytest.mark.parametrize("key,value,location", [
    ("extra", 1, None), ("origin", "server", "metadata.origin"),                                  # I19
    ("route", "citi_primary", "metadata.route"),                                                  # I15
    ("capture_id", "1ce8b6f2-1a5d-1572-9ff4-d588e81b0884", "metadata.capture_id"),               # I16 (version 1)
    ("capture_id", "1CE8B6F2-1A5D-4572-9FF4-D588E81B0884", "metadata.capture_id"),
    ("source_commit", "x" * 40, None), ("extraction_contract", "c1b/1:" + "x" * 64, None),
    ("original_body_sha256", "a" * 63, None), ("fixture_sha256", "0" * 64, "metadata.fixture_sha256"),  # I14
    ("fetched_at", "2026-9-19T06:06:29Z", None), ("fetched_at", "2026-02-30T06:06:29Z", None),
    ("http_status", True, None), ("http_status", 199, None), ("http_status", 300, None),
    ("content_type", "text/html user@example.invalid", "metadata.content_type"),               # I17
    ("charset", "https://example.invalid", None), ("source_url_base", "file:///private/example", None),
    ("parser", {"name": "html.parser"}, None),
])
def test_metadata_mutations_fail_in_layer_a(key, value, location):
    route = "bs_official"
    meta = meta_of(route)
    meta[key] = value
    if key == "capture_id" and location is None:
        location = "metadata.capture_id"
    error = refused(lambda: A.validate_integrity(pair(route)[0], encode(meta), route), location=location)
    assert not error.rule.startswith("d1_")


def test_historical_provenance_does_not_require_current_versions():
    route = "bs_official"
    meta = meta_of(route)
    meta.update(source_commit="c" * 40, extraction_contract="c1b/1:" + "d" * 64)
    meta["parser"].update(python="3.10.0", beautifulsoup4="4.10.0", soupsieve="2.0")
    A.validate_integrity(pair(route)[0], encode(meta), route)


@pytest.mark.parametrize("name", ["lxml", "html5lib", "", None])                    # I21
def test_the_stored_parser_name_must_be_html_parser(name):
    meta = meta_of("bs_official")
    meta["parser"]["name"] = name
    refused(lambda: A.validate_integrity(pair("bs_official")[0], encode(meta), "bs_official"),
            location="metadata.parser.name")


@pytest.mark.parametrize("raw", [b'{}\xff', b'{"x":1,"x":2}', b'{"x":{"y":1,"y":2}}', b'{"x":NaN}',   # I01, I02
                                 b'{"x":Infinity}', b'{"x":1e999}', b'\xef\xbb\xbf{}'])
def test_non_canonical_json_is_refused(raw):
    refused(lambda: A.load_json(raw, "synthetic"), "invalid_json", "synthetic")


@pytest.mark.parametrize("target,limit", [("fixture", HTML_LIMIT), ("metadata", METADATA_LIMIT)])
def test_size_boundaries(target, limit):
    route = "bs_official"
    meta = meta_of(route)
    fixture = pair(route)[0]
    if target == "fixture":
        fixture = fixture + b" " * (limit - len(fixture))
        meta["fixture_sha256"] = sha(fixture)
        metadata = encode(meta)
    else:
        metadata = encode(meta)
        metadata += b" " * (limit - len(metadata))
    A.validate_integrity(fixture, metadata, route)
    refused(lambda: A.validate_integrity(fixture + (b" " if target == "fixture" else b""),
                                         metadata + (b" " if target == "metadata" else b""), route),
            location=f"{target}.size")


@pytest.mark.parametrize("html,location", [
    ('<!--x--><p></p>', "fixture.special_node"), ('<!DOCTYPE html><p></p>', "fixture.special_node"),       # I06
    ('<?x y?><p></p>', "fixture.special_node"),
    *[(f'<{tag}>x</{tag}>', "fixture.tag_body") for tag in ("script", "style", "noscript", "template")],  # I07
    ('<p data-x="safe"></p>', "fixture.attributes"), ('<p style="x"></p>', "fixture.attributes"),         # I12
    ('<p href="?currency=USD"></p>', "fixture.attributes.href"),                                          # I08
    ('<a href="?currency=U"></a>', "fixture.attributes.href"), ('<a href="?currency=US"></a>', "fixture.attributes.href"),  # I09
    ('<a href="?currency=USDD"></a>', "fixture.attributes.href"),
    ('<a href="?currency=USD&x=EUR"></a>', "fixture.attributes.href"),
    ('<p src="flag_USD_"></p>', "fixture.attributes.src"), ('<img src="flag_US_">', "fixture.attributes.src"),  # I10
    ('<img src="flag_USD_s.png">', "fixture.attributes.src"),
    ('<p colspan="0"></p>', "fixture.attributes.colspan"), ('<p rowspan="21"></p>', "fixture.attributes.rowspan"),  # I11
    ('<p colspan="1.0"></p>', "fixture.attributes.colspan"),
    ('<p id="bad.token"></p>', "fixture.attributes.id"),                                                  # I20
    ('<p class="bad:token"></p>', "fixture.attributes.class"),
    ('<p>user@example.invalid</p>', None),                                                                # I13
])
def test_structure_mutations_fail(html, location):
    refused(lambda: A.validate_structure(BeautifulSoup(html, "html.parser")), location=location)


@pytest.mark.parametrize("html", [
    '<a href="?currency="></a>', '<a href="?currency=uSd&currency=&currency=EUR"></a>',
    '<img src="flag_jPy_">', '<img src="flag_EUR.">',
    *[f'<p colspan="{span}"></p>' for span in ("1", "20", "+1", "01", " 20 ")],
    '<p id="historic_id" class="historical class_2"></p>', '<script></script>', '<meta><link><input>',
])
def test_safe_historical_structure_passes(html):
    A.validate_structure(BeautifulSoup(html, "html.parser"))


def _mibank_record():
    return copy.deepcopy(meta_of("bs_mibank")["recorded_extraction"])


def test_stored_exception_sites_are_historical_and_scanned():                   # I03, I04
    record = {"events": [], "queries": [], "returned": None,
              "exception": {"type": "ValueError", "args": ["broken"],
                            "site": [["utils.py", "removed_old_function", 999999]]}}
    A.validate_stored_recording(record, "bs_mibank")
    for frame in (["utils.py", "f", 0], ["utils.py", "f", True], ["outside.py", "f", 1],
                  ["../utils.py", "f", 1], ["utils.py", "bad function", 1], ["utils.py", "f"]):
        altered = copy.deepcopy(record)
        altered["exception"]["site"] = [frame]
        refused(lambda: A.validate_stored_recording(altered, "bs_mibank"))
    record["exception"]["args"] = [["person@example.invalid"]]
    refused(lambda: A.validate_stored_recording(record, "bs_mibank"), "at_token")


@pytest.mark.parametrize("target", ["fact_extra", "rate_text", "code", "label", "found_code", "query_type",   # I05
                                    "query_extra", "path_bool", "rate_bool", "event_after_completed"])
def test_recording_schema_and_page_string_mutations(target):
    record = _mibank_record()
    event = next(e for e in record["events"] if e["kind"] == "observed")
    if target == "fact_extra":
        event["facts"]["extra"] = 1
    elif target in ("rate_text", "code"):
        event["facts"][target] = "person@example.invalid"
    elif target == "label":
        event["labels"]["row_text"] = "person@example.invalid"
    elif target == "found_code":
        record["returned"][1].append("person@example.invalid")
    elif target == "query_type":
        record["queries"][0]["args"] = "table"
    elif target == "query_extra":
        record["queries"][0]["extra"] = 1
    elif target == "path_bool":
        record["queries"][0]["root"] = [True]
    elif target == "rate_bool":
        event["facts"]["rate"] = True
    else:
        record["events"].append(copy.deepcopy(event))
    refused(lambda: A.validate_stored_recording(record, "bs_mibank"))
    if target == "found_code":
        refused(lambda: A.validate_stored_recording(record, "bs_mibank"), "at_token")


@pytest.mark.parametrize("field", ["selector", "order", "matched_code"])
@pytest.mark.parametrize("value,rule", [
    ("person@example.invalid", "at_token"), ("https://example.invalid", "url_scheme"),
    ("a" * 20, "long_alphanumeric"), ("0123456789abcdef", "long_hex"), ("1234567890", "long_number"),
])
def test_historical_code_fields_are_scanned_without_the_registry(field, value, rule):
    record = copy.deepcopy(meta_of("citi_primary")["recorded_extraction"])
    target = next(e["facts"] for e in record["events"] if e["kind"] == "observed")
    target[field] = "historical_safe_token"
    A.validate_stored_recording(record, "citi_primary")          # historical membership is layer B's business
    target[field] = value
    refused(lambda: A.validate_stored_recording(record, "citi_primary"), rule)


def test_the_fallback_selector_is_scanned_too():
    from tools.fixture_capture.roundtrip import record_extraction
    registry = Registry()
    record = record_extraction(BeautifulSoup(mibank_html(mibank_row(), header=None), "html.parser"),
                               registry.routes["bs_mibank"], registry)
    basis = next(e for e in record["events"] if e["kind"] == "observed")["facts"]["value_basis"]
    assert basis["branch"] == "fallback"
    basis["selector"] = None
    A.validate_stored_recording(record, "bs_mibank")
    basis["selector"] = "person@example.invalid"
    refused(lambda: A.validate_stored_recording(record, "bs_mibank"), "at_token")


def test_admission_runs_layer_a_before_d1():
    # A layer-A fault must be reported as itself, not hidden behind a missing approval or a hash mismatch.
    route = "citi_primary"
    meta = meta_of(route)
    meta["origin"] = "server"
    refused(lambda: admit(route, None, metadata=encode(meta)), location="metadata.origin")
    fixture = pair(route)[0].replace(b"<footer>", b"<footer><!--x-->", 1)
    meta = meta_of(route)
    meta["fixture_sha256"] = sha(fixture)
    refused(lambda: admit(route, None, fixture=fixture, metadata=encode(meta)), location="fixture.special_node")


# ════ B. the D1 approval check ══════════════════════════════════════════════

def test_the_expected_findings_are_the_hand_derived_ones():
    for route in ROUTES:
        found = admit(route, approval(route) if EXPECTED[route] else None)
        assert canonical_json(found) == canonical_json(EXPECTED[route])


@pytest.mark.parametrize("route", ["bs_official", "bs_mibank", "citi_mibank"])
def test_no_findings_and_no_file_is_admitted(route):
    assert admit(route) == []


@pytest.mark.parametrize("route", ["bs_official", "citi_mibank"])
def test_no_findings_with_a_file_is_still_fully_checked(route):
    assert admit(route, approval(route)) == []
    refused(lambda: admit(route, approval(route, fixture_sha256="0" * 64)), "d1_approval_binding")
    refused(lambda: admit(route, approval(route, items=[{"finding": CITI_FINDING, "decision": "name_removed"}])),
            "d1_approval_items")


@pytest.mark.parametrize("route", ["bs_official", "citi_primary"])
@pytest.mark.parametrize("raw", [b"null", b"[]", b"1", b'"x"', b"true"])
def test_a_file_that_is_not_a_json_object_is_malformed_not_absent(route, raw):
    # Absence is `approval_bytes=None` alone. A file holding `null` must not read as "no file" (it would admit 0 findings).
    refused(lambda: admit(route, raw=raw), "d1_approval_schema")


@pytest.mark.parametrize("route", ["citi_primary", "citi_secondary"])
def test_findings_without_a_file_are_refused(route):
    refused(lambda: admit(route), "d1_approval_missing")


@pytest.mark.parametrize("route", ["citi_primary", "citi_secondary"])
def test_findings_with_the_exact_approval_are_admitted(route):
    assert canonical_json(admit(route, approval(route))) == canonical_json([CITI_FINDING])


@pytest.mark.parametrize("field", ["fixture_sha256", "metadata_sha256", "policy_digest"])
def test_every_binding_is_checked(field):
    refused(lambda: admit("citi_primary", approval("citi_primary", **{field: "0" * 64})), "d1_approval_binding")


@pytest.mark.parametrize("field,value", [("route", "citi_secondary"),
                                         ("capture_id", "00000000-0000-4000-8000-000000000000")])
def test_the_approval_names_this_capture(field, value):
    refused(lambda: admit("citi_primary", approval("citi_primary", **{field: value})), "d1_approval_binding")


def test_the_metadata_hash_is_of_the_raw_bytes_not_a_reserialization():
    route = "citi_primary"
    raw = pair(route)[1]
    reserialized = json.dumps(json.loads(raw), ensure_ascii=True, allow_nan=False).encode()
    assert reserialized != raw
    record = approval(route, metadata_sha256=sha(reserialized))
    refused(lambda: admit(route, record), "d1_approval_binding")


@pytest.mark.parametrize("mutate", [
    lambda items: [],                                                                     # missing
    lambda items: items + items,                                                          # duplicated
    lambda items: [{"finding": {**items[0]["finding"], "segment_index": False}, "decision": "name_removed"}],
    lambda items: [{"finding": {**items[0]["finding"], "start": 0.0}, "decision": "name_removed"}],
    lambda items: [{"finding": {**items[0]["finding"], "end": "3"}, "decision": "name_removed"}],
    lambda items: [{"finding": {**items[0]["finding"], "extra": 1}, "decision": "name_removed"}],
    lambda items: [{"finding": {k: v for k, v in items[0]["finding"].items() if k != "total_count"},
                    "decision": "name_removed"}],
])
def test_the_findings_must_match_exactly_and_by_type(mutate):
    route = "citi_primary"
    record = approval(route)
    record["items"] = mutate(record["items"])
    refused(lambda: admit(route, record), "d1_approval_items")


def test_key_order_inside_a_finding_does_not_matter():
    route = "citi_primary"
    record = approval(route)
    record["items"] = [{"decision": "name_removed", "finding": dict(reversed(list(CITI_FINDING.items())))}]
    assert admit(route, record)


def test_the_order_of_several_findings_matters():
    route = "citi_primary"
    fixture = pair(route)[0].replace(b"</body>", "<p>은행장 인사말</p></body>".encode(), 1)
    meta = meta_of(route)
    meta["fixture_sha256"] = sha(fixture)
    metadata = encode(meta)
    # Numbering is global: the first finding's total_count becomes 2. The appended <p> is body's third child.
    found = A.admit(fixture, metadata, route, encode(approval(route, fixture, metadata, items=[
        {"finding": {**CITI_FINDING, "total_count": 2}, "decision": "name_removed"},
        {"finding": {"rule_id": "d1_role_context", "source": "html_text", "owner_path": [0, 1, 2], "segment_index": 0,
                     "token": "은행장", "start": 0, "end": 3, "occurrence_index": 2, "total_count": 2},
         "decision": "not_person_name_context"}])))
    assert [f["token"] for f in found] == ["대표자", "은행장"]
    swapped = approval(route, fixture, metadata, items=[
        {"finding": found[1], "decision": "not_person_name_context"},
        {"finding": found[0], "decision": "name_removed"}])
    refused(lambda: A.admit(fixture, metadata, route, encode(swapped)), "d1_approval_items")


def test_a_title_only_in_the_metadata_needs_its_own_approval():
    route = "bs_official"
    meta = meta_of(route)
    meta["content_type"] = "text/html; charset=utf-8; note=대표자"
    metadata = encode(meta)
    refused(lambda: A.admit(pair(route)[0], metadata, route, None), "d1_approval_missing")
    found_raw = A.admit(pair(route)[0], metadata, route, encode(approval(route, metadata=metadata, items=[
        # The metadata key path, then the field's own count; "text/html; charset=utf-8; note=" is 31 characters.
        {"finding": {"rule_id": "d1_role_context", "source": "metadata", "path": ["content_type"],
                     "field_occurrence": 1, "field_count": 1, "token": "대표자", "start": 31, "end": 34,
                     "occurrence_index": 1, "total_count": 1},
         "decision": "not_person_name_context"}])))
    assert [f["source"] for f in found_raw] == ["metadata"]


@pytest.mark.parametrize("decision", ["name_kept", "", None, "NAME_REMOVED", 1])
def test_only_the_two_decisions_exist(decision):
    route = "citi_primary"
    record = approval(route)
    record["items"][0]["decision"] = decision
    refused(lambda: admit(route, record), "d1_approval_schema")


def test_the_two_decisions_are_named_as_agreed():
    assert P.DECISIONS == ("name_removed", "not_person_name_context")
    assert P.SCHEMA_VERSION == 1


@pytest.mark.parametrize("breaks", [
    lambda r: r.update(schema_version=True),                     # True == 1 must not pass
    lambda r: r.update(schema_version=2),
    lambda r: r.update(schema_version=1.0),
    lambda r: r.update(extra=1),
    lambda r: r.pop("reviewer"),
    lambda r: r.update(reviewer=""),
    lambda r: r.update(reviewer="홍길동"),                        # an identifier, not a name in page text
    lambda r: r.update(reviewer="a b"),
    lambda r: r.update(reviewer="a" * 65),
    lambda r: r.update(reviewed_at="2026-09-22 00:00:00"),
    lambda r: r.update(reviewed_at="2026-02-30T00:00:00Z"),
    lambda r: r.update(items={"finding": CITI_FINDING, "decision": "name_removed"}),
    lambda r: r["items"][0].update(note="x"),
    lambda r: r["items"][0].pop("decision"),
    lambda r: r["items"].__setitem__(0, [CITI_FINDING, "name_removed"]),
    lambda r: r.update(fixture_sha256="A" * 64),
    lambda r: r.update(capture_id=1),
])
def test_the_approval_schema_is_closed(breaks):
    route = "citi_primary"
    record = approval(route)
    breaks(record)
    refused(lambda: admit(route, record), "d1_approval_schema")


@pytest.mark.parametrize("breaks", [
    lambda rt: rt.pop("markupbase_sha256"),
    lambda rt: rt.update(extra="x"),
    lambda rt: rt.update(parser="lxml"),
    lambda rt: rt.update(html_parser_sha256="z" * 64),
    lambda rt: rt.update(python_version=""),
    lambda rt: rt.update(bs4_version=4),
])
def test_the_recorded_review_runtime_has_the_amendment_shape(breaks):
    route = "citi_primary"
    record = approval(route)
    breaks(record["runtime"])
    refused(lambda: admit(route, record), "d1_approval_schema")


@pytest.mark.parametrize("handle", ["a", "a" * 64, "jay.hong_2-x"])
def test_reviewer_handles_within_the_limit_are_accepted(handle):
    assert admit("citi_primary", approval("citi_primary", reviewer=handle))


def test_a_different_review_runtime_is_recorded_not_compared():
    route = "citi_primary"
    record = approval(route)
    record["runtime"].update(python_version="3.13.15", html_parser_sha256="f" * 64, bs4_version="4.99.0")
    assert admit(route, record)


@pytest.mark.parametrize("route, with_file", [("bs_official", False), ("bs_official", True), ("citi_primary", True)])
def test_the_current_runtime_must_be_obtainable_even_with_nothing_to_approve(route, with_file, monkeypatch):
    record = approval(route) if with_file else None
    monkeypatch.delattr(bs4, "__version__")
    refused(lambda: admit(route, record), "d1_runtime_unavailable")


def test_the_policy_is_read_on_every_check(monkeypatch):
    # An approval made under one policy, then the policy changes: the same bytes must stop being admitted.
    route = "citi_primary"
    record = approval(route)
    assert admit(route, record)
    current = d1_digest.policy_descriptor()
    changed = {**current, "policy_amendments": current["policy_amendments"] + ["e" * 64]}
    monkeypatch.setattr(d1_digest, "policy_descriptor", lambda root=None: dict(changed))
    refused(lambda: admit(route, record), "d1_approval_binding")


@pytest.mark.parametrize("route, with_file", [("bs_official", False), ("bs_official", True), ("citi_primary", True)])
def test_an_unreadable_policy_refuses_even_with_nothing_to_approve(route, with_file, monkeypatch):
    record = approval(route) if with_file else None

    def unreadable(root=None):
        raise CaptureError("d1_digest_unreadable", "policy_spec")

    monkeypatch.setattr(d1_digest, "policy_descriptor", unreadable)
    refused(lambda: admit(route, record), "d1_digest_unreadable")


def test_cannot_check_is_not_zero_findings():
    route = "bs_official"
    fixture = pair(route)[0].replace(b"</body>", b"<x-unknown></x-unknown></body>", 1)
    meta = meta_of(route)
    meta["fixture_sha256"] = sha(fixture)
    error = refused(lambda: A.admit(fixture, encode(meta), route, None))
    assert error.rule.startswith("d1_") and error.rule != "d1_approval_missing"


def test_the_approval_file_is_parsed_strictly():
    route = "citi_primary"
    raw = encode(approval(route))
    duplicated = raw[:-1] + b',"reviewer":"jay"}'
    refused(lambda: admit(route, raw=duplicated), "invalid_json")
    refused(lambda: admit(route, raw=b"\xef\xbb\xbf" + raw), "invalid_json")


# ════ C. loading the approval file ═══════════════════════════════════════════

def test_the_approval_path_comes_from_the_validated_ids(tmp_path):
    evidence = A.validate_integrity(*pair("citi_primary"), "citi_primary")
    assert A.approval_path(tmp_path, evidence) == tmp_path / "citi_primary" / f"{evidence.metadata['capture_id']}.json"


def test_nothing_at_the_path_is_the_only_absence(tmp_path):
    path = tmp_path / "citi_primary" / "x.json"
    assert A.read_approval(path) is None
    path.parent.mkdir()
    path.write_bytes(b"{}")
    assert A.read_approval(path) == b"{}"


def test_a_read_error_is_not_absence(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    path.write_bytes(b"{}")
    real = Path.read_bytes

    def denied(self):
        if self == path:
            raise PermissionError(13, "Permission denied", "/secret/reviewer/path")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    error = refused(lambda: A.read_approval(path), "d1_approval_unreadable")
    assert "/secret" not in str(error) and str(tmp_path) not in str(error)
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


@pytest.mark.parametrize("shape", ["directory", "symlink", "dangling_symlink"])
def test_an_unusual_file_is_not_absence(tmp_path, shape):
    path = tmp_path / "x.json"
    if shape == "directory":
        path.mkdir()
    elif shape == "symlink":
        target = tmp_path / "real.json"
        target.write_bytes(b"{}")
        path.symlink_to(target)
    else:
        path.symlink_to(tmp_path / "missing.json")
    refused(lambda: A.read_approval(path), "d1_approval_unreadable")


# ════ the modules keep their boundaries ══════════════════════════════════════

def test_neither_module_imports_capture_registry_or_runtime():
    from tests.test_d1_digest_contract import _imports
    package = Path(A.__file__).parent
    assert not ({"capture", "registry", "runtime", "fetch", "roundtrip"} & _imports(package / "admission.py"))
    assert not ({"admission", "capture", "registry", "runtime"} & _imports(package / "d1_approval.py"))
