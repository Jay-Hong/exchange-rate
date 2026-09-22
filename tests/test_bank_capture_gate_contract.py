"""D1 slice 5c-1 contract — every real-fixture entry point passes A → D1 before B/C or hold. Read-only for the implementer.

Written before the implementation (Claude) from the slice-5c design agreement with Codex (2026-09-22: entry plan
`design/c1b2/slice5c_entry.md`, Codex r1 review, Claude's per-point evaluation, Codex r2 sentence revisions). The
implementation ports C1d's B/C layer and hold (worktree `wt-c1d`, uncommitted, base `190f0d0`) onto master's
`tools/fixture_capture/admission.py`. Evidence here is synthetic: real `capture_route` Artifacts built from the synthetic
pages (network refused, provenance pinned) and written to tmp roots — never the stored captures.

API — `tests/bank_capture_gate.py` (new):
- `FIXTURE_ROOT` (`tests/fixtures/bank_capture`), `REVIEWS_ROOT` (`tests/fixture_reviews/bank_capture`).
- `ADMITTED_ROUTES` — the explicit admitted-fixture catalog: exactly one module-level assignment of a tuple literal of
  string constants, a subset of `admission.EXTRACTORS`, no duplicates. Never generated from files or `capture.ROUTES`; it
  grows by an edit in each admission commit (5c-2, 5c-3). It is the single source of the real-fixture tests and of the hold
  mapping.
- `admitted_evidence(route, *, root=None, reviews_root=None, admitted=None) -> Evidence` — `None` means the module
  attribute, looked up at call time. Order: (1) `route` not in `admitted` → `CaptureError("field_contract", "gate.route")`
  before any lookup or open of anything under `root` or `reviews_root` (stat, lstat, access, open, os.open, scandir,
  listdir; judged on resolved paths, a relative path given with `dir_fd` resolved against that directory); (2) `admission.load_evidence(route, root)` (layer A); (3) the D1 approval bytes are
  `admission.read_approval(admission.approval_path(reviews_root, evidence))`; (4) the `admission.admit` judgment on that
  evidence's bytes and those approval bytes; (5) return the A-validated evidence. `admit` returning `[]` is success.
  One snapshot: the pair is read once, and A, D1 and the returned evidence all use those bytes (how the admission
  functions are imported is not constrained). D1 is judged anew on every call (no cache).
API — `tests/bank_capture_contract.py` (C1d B/C, ported): `normalize_recording`, `canonical_json`, `replay`,
  `validate_registry`, `validate_compatibility`, `validate_contract_evidence` with C1d's meaning, plus
- `check_compatibility(route, registry, *, root=None, reviews_root=None, admitted=None)` — the gate first; then B unless a
  hold is present for the route in that root; a hold never waives the gate.
- `check_scoped_evidence(route, registry, *, root=None, reviews_root=None, admitted=None)` — the gate first, then C.
  Both run B/C on the gated snapshot (no re-read). C judges the current re-extraction, never the stored recording.
API — `tests/bank_capture_hold.py` (C1d hold, ported): `COMPATIBILITY_CASES` (derived from `ADMITTED_ROUTES`),
  `hold_present(route, root=None)`, `approval_path(root, fixture_id)` (`<root>/approvals/<route>__<capture_id>.json`),
  `validate_hold(evidence, current, raw, approval_raw, reports, *, now=None)`,
  `HoldSession(root=None, *, reviews_root=None, admitted=None, compatibility_path=...)`. At session finish, for each route
  whose compatibility test entered its call phase with a hold: gate (A → D1) → C → `validate_hold`. A failure sets
  `TESTS_FAILED` and writes `bank capture hold FAILED: <items>` (joined by ", "), an item being
  `<route> (<location>: <rule>)` for a CaptureError and the bare route otherwise — never page text, approval statements
  or exception text, in the report or on stdout, stderr or logging (the gate's refusals likewise). A hold present on disk
  creates no obligation unless the call hook saw the compatibility test enter its call phase; `pytest_runtest_call`
  records the obligation (`held_routes`) and `pytest_runtest_logreport` records `(when, outcome, wasxfail)` per nodeid
  (`reports`); the replacement test must pass setup, call and teardown in this session without `wasxfail`. The hold binds
  the evidence id, both raw hashes, the normalized replay hash, the replacement nodeid, the review date (expiring at KST
  00:00 of that date) and the approval file hash; both agents (`Claude Code`, `OpenAI Codex`) approve with
  `APPROVE_HOLD`, a non-empty statement and every binding field equal to the hold's.
Real-fixture tests: every `test_original_pair_*` function in `tests/test_bank_capture_*.py` is parametrized over
  `ADMITTED_ROUTES` and never calls layer-A functions directly. Collected and run by pytest with the default
  configuration against tmp roots and a non-empty catalog, each case runs (no skip/xfail) and passes the gate before B
  (compatibility) or C (scoped evidence) — setup, call and teardown each passed without `wasxfail` when it should pass,
  on every supported route — and A → D1 runs on every route whatever the number of findings (a wrong approval present
  for a 0-finding page is refused); the default configuration registers a fresh `HoldSession` per session, so a
  used hold whose replacement test did not pass fails the session. Every catalogued route has its pair under
  `FIXTURE_ROOT`.
The port keeps C1d's B/C and hold judgments: the functions in `PORTED_SOURCE` (C1d's source, embedded here and parsed by
  the running interpreter) have exactly that AST, and the names they use are the very objects of master (`require`, `query_key`, `load_json`, `sha256`, `matches`,
  `HASH_PATTERN`, `_keys`, `detector`, `deidentify`, `record_extraction`) with C1d's constants. `approval_record_sha256`
  binds the hold approval file's raw bytes. Every other bank-capture test reads neither `FIXTURE_ROOT` nor `REVIEWS_ROOT` — judged
  on resolved paths, including after a directory change and in the pytest subprocesses it starts.
Kept from earlier slices: A accepts historical provenance by format (no current-version requirement is added by the
  gate); `admission` imports neither `registry`, `capture`, `runtime` nor anything under `tests`.

The two approval systems stay separate and neither substitutes for the other: D1 (`<reviews_root>/<route>/<capture_id>.json`,
closed schema v1) and hold (`<root>/approvals/...`, both agents' APPROVE_HOLD). D1 with 0 findings and no approval file still
admits (the 5b-2 decision table). A hold waives B only — never A, D1 or C.
"""

import ast
import copy
import importlib
import io
import json
import logging
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from tests._fixture_capture import citi_html, getter, mibank_html, no_network, official_html  # noqa: F401
from tools.fixture_capture import admission as A
from tools.fixture_capture import capture, queries
from tools.fixture_capture.d1_digest import policy_descriptor, policy_digest, runtime_descriptor
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.registry import Registry

REPO = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")
NODEID = "test_replacement.py::test_replacement[USD]"
PASSED = [(phase, "passed", False) for phase in ("setup", "call", "teardown")]
ENTRY_CALLS = {"admitted_evidence", "check_compatibility", "check_scoped_evidence"}
BYPASS_CALLS = {"load_evidence", "validate_integrity", "admit", "validate_structure", "validate_stored_recording"}
BINDING_FIELDS = ("fixture_id", "fixture_sha256", "metadata_sha256", "replay_sha256", "replacement_nodeid", "review_by")
MARK = "SECRET-MARK-5C1"
REQUIRED_REAL_TESTS = {"test_original_pair_current_compatibility", "test_original_pair_scoped_contract_evidence"}
# Hand-derived in the 5b-2 contract for the synthetic citi pages (not copied from findings()): the disclosure li at
# html > body (1) > the disclosure div (1) > footer (4) > div (4) > div (0) > ul (1) > li (0).
CITI_FINDING = {"rule_id": "d1_role_context", "source": "html_text", "owner_path": [0, 1, 1, 4, 4, 0, 1, 0],
                "segment_index": 0, "token": "대표자", "start": 0, "end": 3, "occurrence_index": 1, "total_count": 1}


# C1d's functions (worktree wt-c1d, 2026-09-22), verbatim: the port changes imports, not these bodies. Compared as ASTs
# parsed by the running interpreter, so the check does not depend on the Python patch version.
PORTED_SOURCE = {
    ('bank_capture_contract', 'normalize_recording'): r'''
def normalize_recording(record):
    """Ignore only exception line numbers; never sort frames/events/queries."""
    result = copy.deepcopy(record)
    if result["exception"] is not None:
        result["exception"]["site"] = [frame[:2] for frame in result["exception"]["site"]]
    return result
''',
    ('bank_capture_contract', 'canonical_json'): r'''
def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
''',
    ('bank_capture_contract', 'replay'): r'''
def replay(evidence, registry):
    return record_extraction(evidence.soup(), registry.routes[evidence.route], registry)
''',
    ('bank_capture_contract', 'validate_registry'): r'''
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
''',
    ('bank_capture_contract', 'validate_compatibility'): r'''
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
''',
    ('bank_capture_contract', '_element'): r'''
def _element(soup, path):
    element = soup
    for index in path:
        element = [child for child in element.children if isinstance(child, Tag)][index]
    return element
''',
    ('bank_capture_contract', '_jpy_hundred'): r'''
def _jpy_hundred(text):
    # A bounded textual search, not a general unit inference policy. Accept
    # whitespace/fullwidth digits and the spellings relevant to these pages.
    text = unicodedata.normalize("NFKC", text)
    return re.search(r"(?:100\s*(?:엔|円|yen|JPY)|(?:JPY|엔|円|yen)\s*[:(/]?\s*100)", text, re.I)
''',
    ('bank_capture_contract', 'validate_contract_evidence'): r'''
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
''',
    ('bank_capture_hold', 'approval_path'): r'''
def approval_path(root, fixture_id):
    # fixture_id is derived only from A-validated route + canonical UUID4.
    return Path(root) / "approvals" / (fixture_id.replace("/", "__") + ".json")
''',
    ('bank_capture_hold', 'validate_hold'): r'''
def validate_hold(evidence, current, raw, approval_raw, reports, *, now=None):
    hold = load_json(raw, "hold")
    _keys(hold, (*BINDING_FIELDS, "approval_record_sha256"), "hold")
    expected = {"fixture_id": evidence.fixture_id, "fixture_sha256": sha256(evidence.fixture),
                "metadata_sha256": sha256(evidence.metadata_bytes),
                "replay_sha256": sha256(canonical_json(normalize_recording(current)))}
    for key, value in expected.items():
        require(hold[key] == value, "hold." + key)
    for key in ("fixture_sha256", "metadata_sha256", "replay_sha256", "approval_record_sha256"):
        require(matches(hold[key], HASH_PATTERN), "hold." + key)
    nodeid = hold["replacement_nodeid"]
    require(type(nodeid) is str and "::" in nodeid and nodeid == nodeid.strip(), "hold.replacement_nodeid")
    require(matches(hold["review_by"], r"[0-9]{4}-[0-9]{2}-[0-9]{2}"), "hold.review_by")
    try:
        expires = datetime.combine(datetime.strptime(hold["review_by"], "%Y-%m-%d").date(), time(), KST)
    except ValueError:
        raise CaptureError("field_contract", "hold.review_by") from None
    now = datetime.now(KST) if now is None else now
    require(now.tzinfo is not None and now < expires, "hold.expired")
    require(hold["approval_record_sha256"] == sha256(approval_raw), "hold.approval_record_sha256")
    approvals = load_json(approval_raw, "hold.approvals")
    _keys(approvals, ("approvals",), "hold.approvals")
    require(type(approvals["approvals"]) is list and len(approvals["approvals"]) == 2, "hold.approvals")
    agents = []
    for item in approvals["approvals"]:
        _keys(item, (*BINDING_FIELDS, "agent", "verdict", "statement"), "hold.approval")
        require(type(item["agent"]) is str and item["agent"] in AGENTS, "hold.approval.agent")
        agents.append(item["agent"])
        require(item["verdict"] == "APPROVE_HOLD", "hold.approval.verdict")
        require(type(item["statement"]) is str and bool(item["statement"].strip()), "hold.approval.statement")
        for key in BINDING_FIELDS:
            require(type(item[key]) is str and item[key] == hold[key], "hold.approval." + key)
    require(set(agents) == AGENTS, "hold.approval.agents")
    phases = reports.get(nodeid, [])
    require(len(phases) == 3 and {phase for phase, _, _ in phases} == {"setup", "call", "teardown"}
            and all(outcome == "passed" and not xfail for _, outcome, xfail in phases), "hold.replacement_reports")
''',
    ('bank_capture_hold', 'HoldSession.pytest_runtest_call'): r'''
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(self, item):
    # nodeid's path prefix changes with pytest --rootdir. Match the actual
    # file and parametrized test name so that cannot bypass a used hold.
    if Path(item.path).resolve() != self.compatibility_path:
        return
    route = COMPATIBILITY_CASES.get(item.name)
    if route is not None and hold_present(route, self.root):
        # Track at the hook boundary: simply returning from the B test still
        # incurs the gate. A deselected/skipped-before-call B test does not.
        self.held_routes.add(route)
''',
    ('bank_capture_hold', 'HoldSession.pytest_runtest_logreport'): r'''
def pytest_runtest_logreport(self, report):
    self.reports.setdefault(report.nodeid, []).append(
        (report.when, report.outcome, hasattr(report, "wasxfail")))
''',
}


def G():
    from tests import bank_capture_gate
    return bank_capture_gate


def C():
    from tests import bank_capture_contract
    return bank_capture_contract


def H():
    from tests import bank_capture_hold
    return bank_capture_hold


# ── synthetic evidence ─────────────────────────────────────────────────────────

def c_passing_official():
    # C1d's synthetic_hold page: the bs_official scoped facts (header text with a span, cell 1, labelled rows).
    html = official_html().replace("기준환율", "매매 기준율").replace("<th>매매", '<th colspan="1">매매')
    for code, label in (("USD", "미국(USD)"), ("JPY", "일본(JPY(100))"), ("EUR", "유로(EUR)")):
        html = html.replace(f"<td>{code}</td>", f"<td>{label}</td>")
    return html


def c_passing_secondary():
    # citi_secondary scoped facts (header text with a span, cell 1, "100엔" on the JPY row, the page currency list),
    # added inside the table div so the reviewed disclosure stays body's second child (the registered replacement path).
    html = official_html("citi_secondary")
    html = html.replace('<div id="tab01">', '<div id="tab01"><select><option>[JPY] 일본 100 엔</option></select>')
    html = html.replace("기준환율", "고시 기준율").replace("<th>고시", '<th colspan="1">고시')
    return html.replace("<td>JPY</td>", "<td>JPY 100엔</td>")


def c_passing_citi():
    return citi_html(labels=("미국 (USD)", "중국 (CNY)", "유로 (EUR)", "일본 (JPY)"))


def _mibank_row(code, rate):
    return (f'<tr><td><a href="https://example.invalid/rates">{code}</a><img src="/img/flag_{code.lower()}.png"></td>'
            + "<td>-</td>" * 7 + f'<td><span class="counter">{rate}</span></td></tr>')


def c_passing_mibank():
    # Nine header cells with the base-rate label ninth, nine cells per row, currency codes from flag filenames only.
    header = "<th>통화</th>" + "".join(f"<th>항목{i}</th>" for i in range(7)) + "<th>기준환율</th>"
    return mibank_html(rows=_mibank_row("USD", "1,300.25") + _mibank_row("JPY", "900.5") + _mibank_row("EUR", "1,500"),
                       header=header)


def marked_secondary():
    return c_passing_secondary().replace('<div id="tab01">', f'<div id="tab01"><p>{MARK}</p>')


PAGES = {
    "official_c": c_passing_official,        # bs_official: A ✓, D1 0 findings ✓, C ✓
    "official_plain": official_html,         # bs_official: A ✓, D1 0 findings ✓, C ✗ (no span, unlabelled rows)
    "secondary": lambda: official_html("citi_secondary"),  # citi_secondary: A ✓, D1 one finding (CITI_FINDING)
    "secondary_c": c_passing_secondary,      # citi_secondary: A ✓, D1 one finding (CITI_FINDING), C ✓
    "secondary_mark": marked_secondary,      # as secondary_c, with a marker in the page text
    "citi": citi_html,                       # citi_primary: A ✓, D1 one finding (CITI_FINDING)
    "mibank": mibank_html,                   # bs_mibank / citi_mibank: A ✓, D1 0 findings, C ✗ (C.header_index)
    "citi_c": c_passing_citi,                # citi_primary: A ✓, D1 one finding (CITI_FINDING), C ✓
    "mibank_c": c_passing_mibank,            # bs_mibank / citi_mibank: A ✓, D1 0 findings, C ✓
}
# route: (C-passing page, C-failing page, the C refusal location, D1 findings of both pages)
ROUTE_PAGES = {
    "bs_official": ("official_c", "official_plain", "C.header_text", []),
    "citi_primary": ("citi_c", "citi", "C.item_label", [CITI_FINDING]),
    "citi_secondary": ("secondary_c", "secondary", "C.header_text", [CITI_FINDING]),
    "bs_mibank": ("mibank_c", "mibank", "C.header_index", []),
    "citi_mibank": ("mibank_c", "mibank", "C.header_index", []),
}


@lru_cache(maxsize=None)
def artifact(page, route):
    get, _ = getter(PAGES[page]())
    with patch.object(capture, "source_identity", lambda registry: ("a" * 40, "c1b/1:" + "b" * 64)):
        result = capture.capture_route(route, registry=Registry(), get=get)
    return result.fixture, result.metadata


def sha(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def d1_record(route, fixture, metadata, findings, **overrides):
    record = {"schema_version": 1, "route": route, "capture_id": json.loads(metadata)["capture_id"],
              "fixture_sha256": sha(fixture), "metadata_sha256": sha(metadata),
              "policy_digest": policy_digest(policy_descriptor()), "runtime": runtime_descriptor(),
              "items": [{"finding": f, "decision": "name_removed"} for f in findings],
              "reviewer": "jay", "reviewed_at": "2026-09-22T00:00:00Z"}
    record.update(overrides)
    return record


@pytest.fixture
def roots(tmp_path, no_network):
    return SimpleNamespace(fixtures=tmp_path / "fixtures", reviews=tmp_path / "reviews")


def store(roots, route, page, *, metadata=None, d1=None):
    """Write a pair (optionally with replaced metadata) and, when `d1` is a findings list, its exact D1 approval."""
    fixture, original = artifact(page, route)
    metadata = original if metadata is None else metadata
    directory = roots.fixtures / route
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fixture.html").write_bytes(fixture)
    (directory / "metadata.json").write_bytes(metadata)
    if d1 is not None:
        write_d1(roots, route, fixture, metadata, d1_record(route, fixture, metadata, d1))
    return fixture, metadata


def d1_path(roots, route, metadata):
    return roots.reviews / route / f"{json.loads(metadata)['capture_id']}.json"


def write_d1(roots, route, fixture, metadata, record):
    path = d1_path(roots, route, metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(record))
    return path


def refused(call, rule=None, location=None):
    with pytest.raises(CaptureError) as caught:
        call()
    if rule is not None:
        assert caught.value.rule == rule, (caught.value.rule, caught.value.location)
    if location is not None:
        assert caught.value.location == location, (caught.value.rule, caught.value.location)
    return caught.value


def gate(roots, route, admitted=None):
    return G().admitted_evidence(route, root=roots.fixtures, reviews_root=roots.reviews,
                                 admitted=(route,) if admitted is None else admitted)


def selector_mutated(route, page):
    """Metadata whose stored recording names a historical selector: A accepts it, B refuses it (C1d `B.selector`)."""
    fixture, metadata = artifact(page, route)
    meta = json.loads(metadata)
    meta["recorded_extraction"]["events"][0]["facts"]["selector"] = "#historic td"
    return encode(meta)


# ════ A. the admitted catalog ═════════════════════════════════════════════════

def _module_tree(name):
    return ast.parse((REPO / "tests" / f"{name}.py").read_text(encoding="utf-8"))


def test_catalog_is_one_literal_tuple_of_strings():
    tree = _module_tree("bank_capture_gate")
    writes = [node for node in ast.walk(tree)
              if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
              and any(isinstance(t, ast.Name) and t.id == "ADMITTED_ROUTES"
                      for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
    assert len(writes) == 1 and writes[0] in tree.body, "exactly one module-level assignment"
    value = writes[0].value
    assert isinstance(value, ast.Tuple) and all(
        isinstance(e, ast.Constant) and type(e.value) is str for e in value.elts), "a tuple literal of string constants"
    routes = G().ADMITTED_ROUTES
    assert type(routes) is tuple and len(set(routes)) == len(routes) and set(routes) <= set(A.EXTRACTORS)


def test_catalog_is_not_rebound_elsewhere():
    for path in (REPO / "tests").glob("*bank_capture*.py"):
        if path.name == "test_bank_capture_gate_contract.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "ADMITTED_ROUTES" and isinstance(node.ctx, ast.Store):
                pytest.fail(f"{path.name} rebinds ADMITTED_ROUTES")
            if (isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and path.name != "bank_capture_gate.py"
                    and any(isinstance(t, ast.Name) and t.id == "ADMITTED_ROUTES"
                            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))):
                pytest.fail(f"{path.name} assigns ADMITTED_ROUTES")


@pytest.fixture
def catalog(monkeypatch):
    """Reload the hold module under a synthetic catalog; restore both afterwards."""
    def use(routes):
        monkeypatch.setattr(G(), "ADMITTED_ROUTES", tuple(routes))
        return importlib.reload(H())
    yield use
    monkeypatch.undo()
    importlib.reload(H())


@pytest.mark.parametrize("routes", [(), ("bs_official",), ("citi_mibank", "bs_official")])
def test_hold_mapping_is_derived_from_the_catalog(catalog, routes):
    hold = catalog(routes)
    assert hold.COMPATIBILITY_CASES == {f"test_original_pair_current_compatibility[{route}]": route for route in routes}


def test_every_catalogued_route_has_its_pair():
    for route in G().ADMITTED_ROUTES:
        for name in ("fixture.html", "metadata.json"):
            assert (Path(G().FIXTURE_ROOT) / route / name).is_file(), (route, name)


def test_roots_are_the_repository_locations():
    assert Path(G().FIXTURE_ROOT).resolve() == REPO / "tests" / "fixtures" / "bank_capture"
    assert Path(G().REVIEWS_ROOT).resolve() == REPO / "tests" / "fixture_reviews" / "bank_capture"


def _real_test_functions():
    found = {}
    for path in sorted((REPO / "tests").glob("test_bank_capture_*.py")):
        if path.name == "test_bank_capture_gate_contract.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_original_pair_"):
                found[(path.name, node.name)] = node
    return found


def _called_names(node):
    names = set()
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        target = call.func
        names.add(target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None))
    return names


def test_real_fixture_tests_use_the_catalog_and_the_gate():
    found = _real_test_functions()
    assert {name for _, name in found} >= REQUIRED_REAL_TESTS
    for (filename, name), node in found.items():
        marks = [d for d in node.decorator_list if isinstance(d, ast.Call)
                 and isinstance(d.func, ast.Attribute) and d.func.attr == "parametrize"]
        assert len(marks) == 1, (filename, name)
        args = marks[0].args
        source = args[1] if len(args) == 2 else None
        assert (isinstance(args[0], ast.Constant) and args[0].value == "route" and (
            (isinstance(source, ast.Name) and source.id == "ADMITTED_ROUTES")
            or (isinstance(source, ast.Attribute) and source.attr == "ADMITTED_ROUTES"))), (filename, name)
        called = _called_names(node)
        assert called & ENTRY_CALLS, (filename, name, "does not pass the gate")
        assert not called & BYPASS_CALLS, (filename, name, sorted(called & BYPASS_CALLS))


def _ported_functions(module):
    tree = _module_tree(module)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    found[f"{node.name}.{sub.name}"] = sub
    return found


@pytest.mark.parametrize("module,name", sorted(PORTED_SOURCE))
def test_the_port_keeps_c1d_function_bodies(module, name):
    node = _ported_functions(module).get(name)
    assert node is not None, (module, name)
    expected = ast.parse(PORTED_SOURCE[(module, name)]).body[0]
    assert ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False), (module, name)


def test_the_ported_names_resolve_to_masters_objects():
    from tools.fixture_capture import deidentify, detector, roundtrip
    assert C().require is A.require
    assert C().query_key is queries.query_key and C().d is detector
    assert C().deidentify is deidentify.deidentify and C().record_extraction is roundtrip.record_extraction
    for name in ("load_json", "sha256", "matches"):
        assert getattr(H(), name) is getattr(A, name), name
    assert H().HASH_PATTERN == A.HASH_PATTERN and H()._keys is detector._keys
    assert H().require is A.require
    assert H().BINDING_FIELDS == BINDING_FIELDS and H().AGENTS == {"Claude Code", "OpenAI Codex"}
    assert str(H().KST) == "Asia/Seoul"


# ════ B. the gate: A → D1, in that order, before anything else ═════════════════

def fd_path(fd):
    """The directory an fd refers to: F_GETPATH on macOS, /proc on Linux."""
    import fcntl
    if hasattr(fcntl, "F_GETPATH"):
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
    return os.readlink(f"/proc/self/fd/{fd}")


@pytest.fixture
def accesses(monkeypatch):
    """Record filesystem calls whose resolved target lies under the given roots."""
    seen, watched, busy = [], [], []
    real = {"stat": os.stat, "lstat": os.lstat, "open": io.open, "scandir": os.scandir, "listdir": os.listdir,
            "osopen": os.open, "access": os.access}

    def hit(path, dir_fd=None):
        if isinstance(path, int) or busy:
            return
        busy.append(1)
        try:
            text = os.fsdecode(path)
            if dir_fd is not None and not os.path.isabs(text):
                text = os.path.join(fd_path(dir_fd), text)
            text = os.path.realpath(text)
            if any(text == str(w) or text.startswith(str(w) + os.sep) for w in watched):
                seen.append(text)
        finally:
            busy.pop()

    def wrap(name):
        def inner(path=".", *args, **kwargs):
            hit(path, kwargs.get("dir_fd"))
            return real[name](path, *args, **kwargs)
        return inner

    for name in ("stat", "lstat", "scandir", "listdir"):
        monkeypatch.setattr(os, name, wrap(name))
    monkeypatch.setattr(os, "open", wrap("osopen"))
    monkeypatch.setattr(os, "access", wrap("access"))
    monkeypatch.setattr(io, "open", wrap("open"))
    monkeypatch.setattr("builtins.open", wrap("open"))

    def watch(*roots):
        watched.extend(os.path.realpath(r) for r in roots)
        return seen
    return watch


ACCESS_OPERATIONS = {
    "os.open": lambda path: os.close(os.open(path, os.O_RDONLY)),
    "os.stat": lambda path: os.stat(path),         # looked up at call time: the recorder patches the module
    "os.lstat": lambda path: os.lstat(path),
    "os.access": lambda path: os.access(path, os.R_OK),
    "os.path.exists": lambda path: os.path.exists(path),
    "Path.is_file": lambda path: Path(path).is_file(),
    "Path.read_bytes": lambda path: Path(path).read_bytes(),
    "open": lambda path: open(path, "rb").close(),
    "os.listdir": lambda path: os.listdir(os.path.dirname(path)),
    "os.scandir": lambda path: list(os.scandir(os.path.dirname(path))),
    # The directory fd is outside the watched root; the relative path reaches into it through the alias.
    "os.access dir_fd": lambda path: _through_outer_fd(path, lambda rel, fd: os.access(rel, os.R_OK, dir_fd=fd)),
    "os.stat dir_fd": lambda path: _through_outer_fd(path, lambda rel, fd: os.stat(rel, dir_fd=fd)),
    "os.open dir_fd": lambda path: _through_outer_fd(path, lambda rel, fd: os.close(os.open(rel, os.O_RDONLY, dir_fd=fd))),
}


def _through_outer_fd(path, call):
    outer = os.path.dirname(os.path.dirname(os.path.dirname(path)))     # tmp_path: outside the watched root
    fd = os.open(outer, os.O_RDONLY)
    try:
        return call(os.path.relpath(path, outer), fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("operation", sorted(ACCESS_OPERATIONS))
def test_the_access_recorder_sees_each_operation_through_an_alias(tmp_path, accesses, operation):
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    (root / "d" / "f").write_bytes(b"x")
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    resolved = os.path.realpath(root)
    seen = accesses(root)
    ACCESS_OPERATIONS[operation](str(alias / "d" / "f"))
    entries = list(seen)                     # copy first: resolving paths here would be recorded too
    assert entries and all(entry.startswith(resolved) for entry in entries), (operation, entries)


def test_a_route_outside_the_catalog_is_refused_before_any_file_access(roots, accesses):
    store(roots, "bs_official", "official_c")        # a readable pair: reading first would succeed, then refuse
    seen = accesses(roots.fixtures, roots.reviews)
    refused(lambda: gate(roots, "bs_official", admitted=()), "field_contract", "gate.route")
    refused(lambda: gate(roots, "bs_official", admitted=("citi_primary",)), "field_contract", "gate.route")
    assert seen == []
    gate(roots, "bs_official")
    assert seen, "positive control: the recorder sees the gate's own reads"


def test_a_catalogued_route_without_its_pair_fails_closed(roots):
    refused(lambda: gate(roots, "bs_official"), "field_contract", "bs_official/fixture.html.missing")
    store(roots, "bs_official", "official_c")
    (roots.fixtures / "bs_official" / "metadata.json").unlink()
    refused(lambda: gate(roots, "bs_official"), "field_contract", "bs_official/metadata.json.missing")


def test_zero_findings_and_no_approval_file_admits_and_returns_the_evidence(roots):
    fixture, metadata = store(roots, "bs_official", "official_c")
    evidence = gate(roots, "bs_official")
    assert isinstance(evidence, A.Evidence)
    assert (evidence.route, evidence.fixture, evidence.metadata_bytes) == ("bs_official", fixture, metadata)


def test_a_finding_without_its_approval_is_refused(roots):
    store(roots, "citi_secondary", "secondary")
    refused(lambda: gate(roots, "citi_secondary"), "d1_approval_missing", "approval")


def test_a_finding_with_its_exact_approval_admits(roots):
    fixture, metadata = store(roots, "citi_secondary", "secondary", d1=[CITI_FINDING])
    assert gate(roots, "citi_secondary").fixture_id == f"citi_secondary/{json.loads(metadata)['capture_id']}"


def test_a_present_approval_is_checked_even_with_zero_findings(roots):
    fixture, metadata = store(roots, "bs_official", "official_c")
    write_d1(roots, "bs_official", fixture, metadata, d1_record("bs_official", fixture, metadata, [CITI_FINDING]))
    refused(lambda: gate(roots, "bs_official"), "d1_approval_items")


def test_an_unreadable_approval_is_not_absence(roots):
    fixture, metadata = store(roots, "bs_official", "official_c")
    d1_path(roots, "bs_official", metadata).mkdir(parents=True)
    refused(lambda: gate(roots, "bs_official"), "d1_approval_unreadable", "approval")


def test_a_layer_a_failure_precedes_d1(roots):
    fixture, metadata = store(roots, "citi_secondary", "secondary")  # no approval: D1 would refuse too
    (roots.fixtures / "citi_secondary" / "fixture.html").write_bytes(fixture + b" ")
    refused(lambda: gate(roots, "citi_secondary"), "field_contract", "metadata.fixture_sha256")


def test_the_d1_approval_is_read_only_from_its_own_path(roots):
    fixture, metadata = store(roots, "citi_secondary", "secondary")
    record = encode(d1_record("citi_secondary", fixture, metadata, [CITI_FINDING]))
    capture_id = json.loads(metadata)["capture_id"]
    for wrong in (roots.fixtures / "approvals" / f"citi_secondary__{capture_id}.json",   # the hold path
                  roots.reviews / f"{capture_id}.json",                                 # no route level
                  roots.reviews / "citi_primary" / f"{capture_id}.json"):               # another route
        wrong.parent.mkdir(parents=True, exist_ok=True)
        wrong.write_bytes(record)
    refused(lambda: gate(roots, "citi_secondary"), "d1_approval_missing")


@pytest.fixture
def second_read_differs(monkeypatch):
    """After the first open of a watched pair file, every later open reads other bytes."""
    real_open, counts, watched = io.open, {}, set()

    def opener(path, mode="r", *args, **kwargs):
        if not isinstance(path, int) and "r" in mode and "+" not in mode:
            key = os.path.realpath(os.fsdecode(path))
            if key in watched:
                counts[key] = counts.get(key, 0) + 1
                if counts[key] > 1:
                    return io.BytesIO(b"{}") if "b" in mode else io.StringIO("{}")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", opener)
    monkeypatch.setattr("builtins.open", opener)

    def watch(roots, route):
        for name in ("fixture.html", "metadata.json"):
            watched.add(os.path.realpath(roots.fixtures / route / name))
        return counts
    return watch


def test_the_gate_judges_the_bytes_it_returns(roots, second_read_differs):
    fixture, metadata = store(roots, "citi_secondary", "secondary", d1=[CITI_FINDING])
    counts = second_read_differs(roots, "citi_secondary")
    evidence = gate(roots, "citi_secondary")
    assert (evidence.fixture, evidence.metadata_bytes) == (fixture, metadata)
    assert sorted(counts.values()) == [1, 1], "the pair is read exactly once"


def test_b_and_c_use_the_gated_snapshot(roots, second_read_differs):
    # Each call takes its own snapshot; within one call a second read of the pair would see other bytes.
    store(roots, "citi_secondary", "secondary_c", d1=[CITI_FINDING])
    counts = second_read_differs(roots, "citi_secondary")
    for check in (C().check_compatibility, C().check_scoped_evidence):
        counts.clear()
        check("citi_secondary", Registry(), **kwargs(roots, "citi_secondary"))
        assert sorted(counts.values()) == [1, 1], check.__name__


def test_d1_is_rejudged_on_every_call(roots, monkeypatch):
    store(roots, "bs_official", "official_c")
    gate(roots, "bs_official")
    from tools.fixture_capture import d1_digest

    def unavailable(*args, **kwargs):
        raise CaptureError("d1_runtime_unavailable", "runtime")

    monkeypatch.setattr(d1_digest, "runtime_descriptor", unavailable)
    refused(lambda: gate(roots, "bs_official"), "d1_runtime_unavailable")


def test_defaults_are_the_module_attributes_at_call_time(roots, monkeypatch):
    store(roots, "citi_secondary", "secondary", d1=[CITI_FINDING])
    monkeypatch.setattr(G(), "FIXTURE_ROOT", roots.fixtures)
    monkeypatch.setattr(G(), "REVIEWS_ROOT", roots.reviews)
    monkeypatch.setattr(G(), "ADMITTED_ROUTES", ("citi_secondary",))
    assert G().admitted_evidence("citi_secondary").route == "citi_secondary"
    monkeypatch.setattr(G(), "ADMITTED_ROUTES", ())
    refused(lambda: G().admitted_evidence("citi_secondary"), "field_contract", "gate.route")


def test_the_gate_keeps_historical_provenance_acceptance(roots):
    fixture, metadata = artifact("official_c", "bs_official")
    meta = json.loads(metadata)
    meta["parser"].update({"beautifulsoup4": "4.0.0", "soupsieve": "1.0", "python": "3.9.1"})
    meta.update({"source_commit": "c" * 40, "extraction_contract": "c1b/1:" + "d" * 64})
    store(roots, "bs_official", "official_c", metadata=encode(meta))
    assert gate(roots, "bs_official").metadata["parser"]["python"] == "3.9.1"


def test_admission_stays_outside_registry_capture_runtime_and_tests():
    tree = ast.parse((REPO / "tools" / "fixture_capture" / "admission.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "") + ":" + ",".join(alias.name for alias in node.names))
    joined = " ".join(names)
    for forbidden in ("registry", "capture", "runtime", "tests"):
        assert forbidden not in joined.replace("fixture_capture", ""), (forbidden, sorted(names))


def test_gate_refusals_print_nothing(roots, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    store(roots, "citi_secondary", "secondary_mark")
    error = refused(lambda: gate(roots, "citi_secondary"), "d1_approval_missing")
    out, err = capsys.readouterr()
    assert MARK not in out + err + caplog.text + str(error) + repr(error.args)


# ════ C. B and C run only on gated evidence ════════════════════════════════════

def kwargs(roots, route):
    return {"root": roots.fixtures, "reviews_root": roots.reviews, "admitted": (route,)}


def test_compatibility_passes_on_a_clean_gated_pair(roots):
    store(roots, "citi_secondary", "secondary", d1=[CITI_FINDING])
    C().check_compatibility("citi_secondary", Registry(), **kwargs(roots, "citi_secondary"))


def test_compatibility_refuses_d1_before_b(roots):
    store(roots, "citi_secondary", "secondary", metadata=selector_mutated("citi_secondary", "secondary"))
    refused(lambda: C().check_compatibility("citi_secondary", Registry(), **kwargs(roots, "citi_secondary")),
            "d1_approval_missing")


def test_compatibility_runs_b_after_the_gate(roots):
    store(roots, "citi_secondary", "secondary", metadata=selector_mutated("citi_secondary", "secondary"),
          d1=[CITI_FINDING])
    refused(lambda: C().check_compatibility("citi_secondary", Registry(), **kwargs(roots, "citi_secondary")),
            location="B.selector")


def test_b_refuses_a_stored_recording_the_page_no_longer_replays(roots):
    fixture, metadata = artifact("secondary_c", "citi_secondary")
    meta = json.loads(metadata)
    meta["recorded_extraction"]["events"][0]["facts"]["rate_text"] = "1,300.26"
    store(roots, "citi_secondary", "secondary_c", metadata=encode(meta), d1=[CITI_FINDING])
    gate(roots, "citi_secondary")                                     # A and D1 accept it
    refused(lambda: C().check_compatibility("citi_secondary", Registry(), **kwargs(roots, "citi_secondary")),
            "field_contract", "B.replay")


def test_a_hold_waives_b_but_not_the_gate(roots):
    store(roots, "citi_secondary", "secondary", metadata=selector_mutated("citi_secondary", "secondary"),
          d1=[CITI_FINDING])
    (roots.fixtures / "citi_secondary" / "hold.json").write_bytes(b"{}")
    C().check_compatibility("citi_secondary", Registry(), **kwargs(roots, "citi_secondary"))  # B waived
    d1_path(roots, "citi_secondary", (roots.fixtures / "citi_secondary" / "metadata.json").read_bytes()).unlink()
    refused(lambda: C().check_compatibility("citi_secondary", Registry(), **kwargs(roots, "citi_secondary")),
            "d1_approval_missing")


def test_scoped_evidence_refuses_d1_before_c(roots):
    store(roots, "citi_primary", "citi")
    refused(lambda: C().check_scoped_evidence("citi_primary", Registry(), **kwargs(roots, "citi_primary")),
            "d1_approval_missing")


def test_scoped_evidence_runs_c_after_the_gate(roots):
    store(roots, "bs_official", "official_plain")
    refused(lambda: C().check_scoped_evidence("bs_official", Registry(), **kwargs(roots, "bs_official")),
            location="C.header_text")
    store(roots, "bs_official", "official_c")
    C().check_scoped_evidence("bs_official", Registry(), **kwargs(roots, "bs_official"))


def test_scoped_evidence_passes_on_an_approved_finding(roots):
    store(roots, "citi_secondary", "secondary_c", d1=[CITI_FINDING])
    C().check_scoped_evidence("citi_secondary", Registry(), **kwargs(roots, "citi_secondary"))


def test_c_judges_the_current_extraction_not_the_stored_recording(roots):
    # The plain page fails C; its metadata carries the C-passing page's recording (A accepts the shape).
    plain_fixture, plain_metadata = artifact("official_plain", "bs_official")
    meta = json.loads(plain_metadata)
    meta["recorded_extraction"] = json.loads(artifact("official_c", "bs_official")[1])["recorded_extraction"]
    store(roots, "bs_official", "official_plain", metadata=encode(meta))
    refused(lambda: C().check_scoped_evidence("bs_official", Registry(), **kwargs(roots, "bs_official")),
            location="C.header_text")


def test_c_ignores_a_hold(roots):
    store(roots, "bs_official", "official_plain")
    (roots.fixtures / "bs_official" / "hold.json").write_bytes(b"{}")
    refused(lambda: C().check_scoped_evidence("bs_official", Registry(), **kwargs(roots, "bs_official")),
            location="C.header_text")


def wrong_approval(roots, route, page, findings):
    """Store the passing page with a D1 approval whose items are wrong — present even when there are 0 findings."""
    store(roots, route, page)
    fixture, metadata = artifact(page, route)
    for path in (roots.reviews / route).glob("*.json"):   # this route's approvals only
        path.unlink()
    write_d1(roots, route, fixture, metadata, d1_record(route, fixture, metadata, [] if findings else [CITI_FINDING]))


def replay_mutated(route, page):
    fixture, metadata = artifact(page, route)
    meta = json.loads(metadata)
    event = next(e for e in meta["recorded_extraction"]["events"] if "rate_text" in e["facts"])
    event["facts"]["rate_text"] = event["facts"]["rate_text"] + "0"
    return encode(meta)


@pytest.mark.parametrize("route", sorted(ROUTE_PAGES))
def test_every_route_runs_b_and_c_behind_the_gate(roots, route):
    good, bad, location, findings = ROUTE_PAGES[route]
    registry = Registry()
    store(roots, route, good, d1=findings)
    C().check_compatibility(route, registry, **kwargs(roots, route))
    C().check_scoped_evidence(route, registry, **kwargs(roots, route))
    store(roots, route, bad, d1=findings)
    refused(lambda: C().check_scoped_evidence(route, registry, **kwargs(roots, route)), location=location)
    store(roots, route, good, metadata=replay_mutated(route, good), d1=findings)
    refused(lambda: C().check_compatibility(route, registry, **kwargs(roots, route)), "field_contract", "B.replay")
    wrong_approval(roots, route, good, findings)                        # B/C would pass: only D1 can refuse
    refused(lambda: C().check_compatibility(route, registry, **kwargs(roots, route)), "d1_approval_items")
    refused(lambda: C().check_scoped_evidence(route, registry, **kwargs(roots, route)), "d1_approval_items")
    if findings:
        store(roots, route, good)
        for path in roots.reviews.rglob("*.json"):
            path.unlink()
        refused(lambda: C().check_scoped_evidence(route, registry, **kwargs(roots, route)), "d1_approval_missing")
        refused(lambda: C().check_compatibility(route, registry, **kwargs(roots, route)), "d1_approval_missing")


# ════ D. hold: session finish re-runs the gate, then C, then the hold ═════════

def hold_for(evidence, review_by="2099-12-31", nodeid=NODEID):
    current = C().replay(evidence, Registry())
    hold = {"fixture_id": evidence.fixture_id, "fixture_sha256": sha(evidence.fixture),
            "metadata_sha256": sha(evidence.metadata_bytes),
            "replay_sha256": sha(C().canonical_json(C().normalize_recording(current))),
            "replacement_nodeid": nodeid, "review_by": review_by}
    approval = {"approvals": [dict(hold, agent=agent, verdict="APPROVE_HOLD", statement="synthetic approval only")
                              for agent in ("Claude Code", "OpenAI Codex")]}
    hold["approval_record_sha256"] = sha(C().canonical_json(approval))
    return current, hold, approval


def write_hold(roots, evidence, hold, approval):
    (roots.fixtures / evidence.route / "hold.json").write_bytes(C().canonical_json(hold))
    path = H().approval_path(roots.fixtures, evidence.fixture_id)
    assert path == roots.fixtures / "approvals" / (evidence.fixture_id.replace("/", "__") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(C().canonical_json(approval))


class Reporter:
    def __init__(self):
        self.lines = []

    def write_sep(self, sep, text, **kwargs):
        self.lines.append(text)


def run_finish(session_obj):
    reporter = Reporter()
    session = SimpleNamespace(exitstatus=0, config=SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter if name == "terminalreporter" else None)))
    session_obj.pytest_sessionfinish(session, 0)
    return session.exitstatus, reporter.lines


def finish(roots, route, reports=None):
    # Direct state injection for finish-only cases; the hooks that build this state are tested in their own section.
    session_obj = H().HoldSession(roots.fixtures, reviews_root=roots.reviews, admitted=(route,))
    session_obj.held_routes.add(route)
    session_obj.reports.update({NODEID: list(PASSED)} if reports is None else reports)
    return run_finish(session_obj)


def held_official(roots):
    """bs_official passes A, D1 (0 findings, no file) and C; a valid hold and hold approval are written."""
    store(roots, "bs_official", "official_c")
    evidence = gate(roots, "bs_official")
    current, hold, approval = hold_for(evidence)
    write_hold(roots, evidence, hold, approval)
    return evidence, current, hold, approval


def test_a_valid_hold_on_gated_evidence_passes(roots):
    held_official(roots)
    assert finish(roots, "bs_official") == (0, [])


def test_a_valid_hold_on_an_approved_finding_passes(roots):
    # The D1 approval is found under the session's reviews root, not under the fixture root.
    store(roots, "citi_secondary", "secondary_c", d1=[CITI_FINDING])
    evidence = gate(roots, "citi_secondary")
    write_hold(roots, evidence, *hold_for(evidence)[1:])
    assert finish(roots, "citi_secondary") == (0, [])


@pytest.mark.parametrize("route", sorted(ROUTE_PAGES))
def test_finish_runs_gate_c_and_hold_on_every_route(roots, route):
    good, bad, location, findings = ROUTE_PAGES[route]
    store(roots, route, good, d1=findings)
    evidence = gate(roots, route)
    write_hold(roots, evidence, *hold_for(evidence)[1:])
    assert finish(roots, route) == (0, [])
    current, hold, approval = hold_for(evidence)
    hold["replay_sha256"] = "0" * 64                                    # A, D1, C and reports fine; only the hold is wrong
    write_hold(roots, evidence, hold, approval)
    assert finish(roots, route) == (pytest.ExitCode.TESTS_FAILED,
                                    [f"bank capture hold FAILED: {route} (hold.replay_sha256: field_contract)"])
    write_hold(roots, evidence, *hold_for(evidence)[1:])
    wrong_approval(roots, route, good, findings)                        # same valid hold; only D1 can refuse
    assert finish(roots, route) == (pytest.ExitCode.TESTS_FAILED,
                                    [f"bank capture hold FAILED: {route} (approval.items: d1_approval_items)"])
    store(roots, route, bad, d1=findings)
    evidence = gate(roots, route)
    write_hold(roots, evidence, *hold_for(evidence)[1:])
    status, lines = finish(roots, route)
    assert (status, lines) == (pytest.ExitCode.TESTS_FAILED,
                               [f"bank capture hold FAILED: {route} ({location}: field_contract)"])


def test_finish_refuses_d1_even_with_a_valid_hold(roots):
    store(roots, "citi_secondary", "secondary", d1=[CITI_FINDING])
    evidence = gate(roots, "citi_secondary")
    current, hold, approval = hold_for(evidence)
    write_hold(roots, evidence, hold, approval)
    d1_path(roots, "citi_secondary", evidence.metadata_bytes).unlink()
    status, lines = finish(roots, "citi_secondary")
    assert status == pytest.ExitCode.TESTS_FAILED
    assert lines == ["bank capture hold FAILED: citi_secondary (approval: d1_approval_missing)"]


def test_finish_rechecks_d1_from_its_own_path_not_the_hold_approval(roots):
    store(roots, "citi_secondary", "secondary")
    evidence = A.load_evidence("citi_secondary", roots.fixtures)
    current, hold, approval = hold_for(evidence)
    write_hold(roots, evidence, hold, approval)
    status, lines = finish(roots, "citi_secondary")
    assert (status, lines) == (pytest.ExitCode.TESTS_FAILED,
                               ["bank capture hold FAILED: citi_secondary (approval: d1_approval_missing)"])


def test_finish_runs_c_before_the_hold(roots):
    store(roots, "bs_official", "official_plain")
    evidence = gate(roots, "bs_official")
    current, hold, approval = hold_for(evidence)
    write_hold(roots, evidence, hold, approval)
    status, lines = finish(roots, "bs_official")
    assert (status, lines) == (pytest.ExitCode.TESTS_FAILED,
                               ["bank capture hold FAILED: bs_official (C.header_text: field_contract)"])


def test_an_invalid_hold_on_gated_evidence_fails(roots):
    evidence, current, hold, approval = held_official(roots)
    hold["replay_sha256"] = "0" * 64
    (roots.fixtures / "bs_official" / "hold.json").write_bytes(C().canonical_json(hold))
    status, lines = finish(roots, "bs_official")
    assert (status, lines) == (pytest.ExitCode.TESTS_FAILED,
                               ["bank capture hold FAILED: bs_official (hold.replay_sha256: field_contract)"])


def test_a_used_hold_cannot_be_erased_by_deleting_it(roots):
    held_official(roots)
    (roots.fixtures / "bs_official" / "hold.json").unlink()
    status, lines = finish(roots, "bs_official")
    assert status == pytest.ExitCode.TESTS_FAILED and lines == ["bank capture hold FAILED: bs_official"]


def test_hold_expiry_is_kst_midnight_of_review_by(roots):
    store(roots, "bs_official", "official_c")
    evidence = gate(roots, "bs_official")
    current, hold, approval = hold_for(evidence, review_by="2026-09-20")
    args = (evidence, current, C().canonical_json(hold), C().canonical_json(approval), {NODEID: list(PASSED)})
    midnight = datetime(2026, 9, 20, tzinfo=KST)
    H().validate_hold(*args, now=midnight - timedelta(microseconds=1))
    H().validate_hold(*args, now=datetime(2026, 9, 19, 14, 59, 59, 999999, tzinfo=ZoneInfo("UTC")))
    for now in (midnight, datetime(2026, 9, 19, 15, tzinfo=ZoneInfo("UTC"))):
        refused(lambda: H().validate_hold(*args, now=now), "field_contract", "hold.expired")
    write_hold(roots, evidence, hold, approval)
    assert finish(roots, "bs_official") == (
        pytest.ExitCode.TESTS_FAILED, ["bank capture hold FAILED: bs_official (hold.expired: field_contract)"])


@pytest.mark.parametrize("reports", [
    {},
    {NODEID: [("setup", "passed", False), ("call", "passed", True), ("teardown", "passed", False)]},
    {NODEID: [("setup", "passed", False), ("call", "failed", False), ("teardown", "passed", False)]},
    {NODEID: [("setup", "passed", False), ("call", "passed", False)]},
])
def test_the_replacement_test_must_fully_pass_this_session(roots, reports):
    held_official(roots)
    assert finish(roots, "bs_official", reports=reports) == (
        pytest.ExitCode.TESTS_FAILED,
        ["bank capture hold FAILED: bs_official (hold.replacement_reports: field_contract)"])


def test_failure_output_carries_no_page_text_or_statement(roots):
    evidence, current, hold, approval = held_official(roots)
    secret = "재검토자 비밀 문장 SECRET-7"
    approval["approvals"][0]["statement"] = secret
    path = H().approval_path(roots.fixtures, evidence.fixture_id)
    path.write_bytes(C().canonical_json(approval))
    status, lines = finish(roots, "bs_official")
    assert status == pytest.ExitCode.TESTS_FAILED
    assert lines == ["bank capture hold FAILED: bs_official (hold.approval_record_sha256: field_contract)"]
    assert all(secret not in line and "resultTable" not in line for line in lines)


def test_finish_failures_print_nothing_else(roots, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    store(roots, "citi_secondary", "secondary_mark", d1=[CITI_FINDING])
    evidence = gate(roots, "citi_secondary")
    current, hold, approval = hold_for(evidence)
    approval["approvals"][1]["statement"] = MARK
    write_hold(roots, evidence, hold, approval)                         # the hold hash now disagrees
    status, lines = finish(roots, "citi_secondary")
    out, err = capsys.readouterr()
    assert status == pytest.ExitCode.TESTS_FAILED
    assert MARK not in out + err + caplog.text + " ".join(lines)


def test_an_invalid_hold_without_an_obligation_is_not_checked(roots):
    store(roots, "bs_official", "official_plain")                     # C fails too
    (roots.fixtures / "bs_official" / "hold.json").write_bytes(b"not json")
    session_obj = H().HoldSession(roots.fixtures, reviews_root=roots.reviews, admitted=("bs_official",))
    assert run_finish(session_obj) == (0, [])


def test_finish_rejudges_d1_under_the_current_policy(roots, monkeypatch):
    store(roots, "citi_secondary", "secondary_c", d1=[CITI_FINDING])
    evidence = gate(roots, "citi_secondary")
    write_hold(roots, evidence, *hold_for(evidence)[1:])
    assert finish(roots, "citi_secondary") == (0, [])
    from tools.fixture_capture import d1_digest
    monkeypatch.setattr(d1_digest, "policy_digest", lambda descriptor: "0" * 64)
    status, lines = finish(roots, "citi_secondary")
    assert status == pytest.ExitCode.TESTS_FAILED
    assert len(lines) == 1 and lines[0].startswith("bank capture hold FAILED: citi_secondary (")
    assert lines[0].endswith(": d1_approval_binding)"), lines


def validate(evidence, current, hold, approval, reports=None, now=None):
    H().validate_hold(evidence, current, C().canonical_json(hold), C().canonical_json(approval),
                      {NODEID: list(PASSED)} if reports is None else reports,
                      now=datetime(2026, 9, 19, 12, tzinfo=KST) if now is None else now)


WRONG = {"fixture_id": "bs_official/11111111-1111-4111-8111-111111111111", "fixture_sha256": "0" * 64,
         "metadata_sha256": "1" * 64, "replay_sha256": "2" * 64, "replacement_nodeid": "test_other.py::test_other",
         "review_by": "2099-12-30", "approval_record_sha256": "3" * 64}


@pytest.mark.parametrize("field", [*BINDING_FIELDS, "approval_record_sha256"])
def test_every_hold_binding_is_checked(roots, field):
    evidence, current, hold, approval = held_official(roots)
    validate(evidence, current, hold, approval)
    hold[field] = WRONG[field]
    refused(lambda: validate(evidence, current, hold, approval))


@pytest.mark.parametrize("field", ["fixture_id", "fixture_sha256", "metadata_sha256", "replay_sha256"])
def test_a_consistently_wrong_binding_is_caught_against_the_evidence(roots, field):
    # Hold, both approvals and the approval hash agree with each other; only the evidence comparison can stop it.
    evidence, current, hold, approval = held_official(roots)
    hold[field] = WRONG[field]
    for item in approval["approvals"]:
        item[field] = WRONG[field]
    hold["approval_record_sha256"] = sha(C().canonical_json(approval))
    refused(lambda: validate(evidence, current, hold, approval), "field_contract", "hold." + field)


@pytest.mark.parametrize("change", [
    "same_agent_twice", "unknown_agent", "reject", "empty_statement", "one_approval", "three_approvals",
    *[f"item_{field}" for field in BINDING_FIELDS],
])
def test_both_agents_must_approve_the_exact_hold(roots, change):
    evidence, current, hold, approval = held_official(roots)
    items = approval["approvals"]
    if change == "same_agent_twice":
        items[1]["agent"] = items[0]["agent"]
    elif change == "unknown_agent":
        items[1]["agent"] = "Someone Else"
    elif change == "reject":
        items[0]["verdict"] = "REJECT"
    elif change == "empty_statement":
        items[1]["statement"] = " "
    elif change == "one_approval":
        del items[1]
    elif change == "three_approvals":
        items.append(copy.deepcopy(items[0]))
    else:
        items[0][change[len("item_"):]] = WRONG[change[len("item_"):]]
    hold["approval_record_sha256"] = sha(C().canonical_json(approval))
    refused(lambda: validate(evidence, current, hold, approval))


def test_the_hold_binds_the_approval_files_raw_bytes(roots):
    evidence, current, hold, approval = held_official(roots)
    raw = C().canonical_json(approval)
    H().validate_hold(evidence, current, C().canonical_json(hold), raw, {NODEID: list(PASSED)},
                      now=datetime(2026, 9, 19, 12, tzinfo=KST))
    for changed in (raw + b"\n", b" " + raw, json.dumps(approval, indent=1).encode()):
        refused(lambda: H().validate_hold(evidence, current, C().canonical_json(hold), changed, {NODEID: list(PASSED)},
                                          now=datetime(2026, 9, 19, 12, tzinfo=KST)),
                "field_contract", "hold.approval_record_sha256")


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("result", [("failed", False), ("skipped", False), ("passed", True)])
def test_every_replacement_phase_must_pass_without_xfail(roots, phase, result):
    evidence, current, hold, approval = held_official(roots)
    reports = {NODEID: [(p, *result) if p == phase else (p, "passed", False) for p in ("setup", "call", "teardown")]}
    refused(lambda: validate(evidence, current, hold, approval, reports=reports), "field_contract",
            "hold.replacement_reports")


# ── the hooks that create the obligation and collect reports ──────────────────

COMPAT_PATH = REPO / "tests" / "test_bank_capture_contract.py"


def item(name, path=COMPAT_PATH):
    return SimpleNamespace(path=path, name=name, nodeid=f"tests/{Path(path).name}::{name}")


def report(when, outcome, xfail=False, nodeid=NODEID):
    fields = {"nodeid": nodeid, "when": when, "outcome": outcome}
    if xfail:
        fields["wasxfail"] = ""
    return SimpleNamespace(**fields)


def hooked_session(hold, roots, route):
    return hold.HoldSession(roots.fixtures, reviews_root=roots.reviews, admitted=(route,),
                            compatibility_path=COMPAT_PATH)


def test_the_call_hook_creates_the_obligation_only_for_a_held_compatibility_call(roots, catalog):
    hold = catalog(("bs_official", "citi_mibank"))
    (roots.fixtures / "bs_official").mkdir(parents=True)
    (roots.fixtures / "bs_official" / "hold.json").write_bytes(b"{}")
    cases = [
        ("test_original_pair_current_compatibility[bs_official]", COMPAT_PATH, {"bs_official"}),
        ("test_original_pair_current_compatibility[citi_mibank]", COMPAT_PATH, set()),         # no hold file
        ("test_original_pair_scoped_contract_evidence[bs_official]", COMPAT_PATH, set()),      # not the B test
        ("test_original_pair_current_compatibility[bs_official]", REPO / "tests" / "test_other.py", set()),
    ]
    for name, path, expected in cases:
        session_obj = hooked_session(hold, roots, "bs_official")
        session_obj.pytest_runtest_call(item(name, path))
        assert session_obj.held_routes == expected, name


def test_the_hooks_feed_the_finish_gate(roots, catalog):
    hold = catalog(("bs_official",))
    evidence, current, hold_record, approval = held_official(roots)
    session_obj = hooked_session(hold, roots, "bs_official")
    session_obj.pytest_runtest_call(item("test_original_pair_current_compatibility[bs_official]"))
    for when in ("setup", "call", "teardown"):
        session_obj.pytest_runtest_logreport(report(when, "passed"))
    assert run_finish(session_obj) == (0, [])
    session_obj = hooked_session(hold, roots, "bs_official")
    session_obj.pytest_runtest_call(item("test_original_pair_current_compatibility[bs_official]"))
    for when in ("setup", "call", "teardown"):
        session_obj.pytest_runtest_logreport(report(when, "passed", xfail=(when == "call")))
    assert run_finish(session_obj) == (
        pytest.ExitCode.TESTS_FAILED,
        ["bank capture hold FAILED: bs_official (hold.replacement_reports: field_contract)"])
    fresh = hooked_session(hold, roots, "bs_official")
    assert fresh.held_routes == set() and fresh.reports == {}, "each session starts empty"


# ════ G. real pytest sessions: collection, skip/xfail, conftest registration ════

SESSION_PLUGIN = textwrap.dedent('''
    import importlib, json, os
    from pathlib import Path

    import pytest


    @pytest.hookimpl(tryfirst=True)
    def pytest_configure(config):
        # Before collection and before the default configuration builds its HoldSession.
        spec = json.loads(os.environ["BANK_CAPTURE_SESSION"])
        gate = importlib.import_module("tests.bank_capture_gate")
        gate.FIXTURE_ROOT, gate.REVIEWS_ROOT = Path(spec["root"]), Path(spec["reviews"])
        gate.ADMITTED_ROUTES = tuple(spec["routes"])
        importlib.reload(importlib.import_module("tests.bank_capture_hold"))


    def pytest_runtest_logreport(report):
        with open(os.environ["BANK_CAPTURE_REPORTS"], "a", encoding="utf-8") as log:
            log.write(json.dumps([report.nodeid, report.when, report.outcome, hasattr(report, "wasxfail"),
                                  str(report.longrepr)[-2000:] if report.failed else ""]) + "\\n")
''')


def real_session(tmp_path, roots, routes, nodes=None):
    """Run the real-fixture tests in a real pytest session; return (result, {nodeid: [(when, outcome, wasxfail, text)]})."""
    site = tmp_path / "session_site"
    site.mkdir(exist_ok=True)
    (site / "bank_capture_session_plugin.py").write_text(SESSION_PLUGIN, encoding="utf-8")
    log = tmp_path / f"reports-{len(list(tmp_path.glob('reports-*.jsonl')))}.jsonl"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, BANK_CAPTURE_SESSION=json.dumps(
        {"root": str(roots.fixtures), "reviews": str(roots.reviews), "routes": list(routes)}),
        BANK_CAPTURE_REPORTS=str(log),
        PYTHONPATH=os.pathsep.join(filter(None, [str(site), os.environ.get("PYTHONPATH", "")])))
    nodes = [f"tests/{f}::{n}" for f, n in sorted(_real_test_functions())] if nodes is None else nodes
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             "-p", "bank_capture_session_plugin", *nodes],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
    reports = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        nodeid, when, outcome, xfail, text = json.loads(line)
        reports.setdefault(nodeid, []).append((when, outcome, xfail, text))
    return result, reports


def expected_nodeids(routes, names=None):
    names = [n for _, n in _real_test_functions()] if names is None else names
    files = {n: f for f, n in _real_test_functions()}
    return {f"tests/{files[n]}::{n}[{r}]" for n in names for r in routes}


def fully_passed(phases):
    return [(w, o, x) for w, o, x, _ in phases] == [("setup", "passed", False), ("call", "passed", False),
                                                     ("teardown", "passed", False)]


def failed_with(phases, text):
    return any(w == "call" and o == "failed" and not x and text in t for w, o, x, t in phases)


def real_file(name):
    return next(f for f, n in _real_test_functions() if n == name)


def test_real_sessions_run_every_case_through_the_gate(tmp_path, roots):
    routes = tuple(sorted(ROUTE_PAGES))
    compat, scoped = "test_original_pair_current_compatibility", "test_original_pair_scoped_contract_evidence"
    files = {n: f for f, n in _real_test_functions()}
    for route, (good, bad, location, findings) in ROUTE_PAGES.items():
        store(roots, route, good, d1=findings)
    result, reports = real_session(tmp_path, roots, routes)
    assert set(reports) == expected_nodeids(routes), sorted(reports)
    assert all(fully_passed(phases) for phases in reports.values()), reports
    assert result.returncode == 0, result.stdout[-3000:]

    for route, (good, bad, location, findings) in ROUTE_PAGES.items():
        wrong_approval(roots, route, good, findings)                    # every route, 0 findings included
    result, reports = real_session(tmp_path, roots, routes)
    assert set(reports) == expected_nodeids(routes), sorted(reports)
    assert all(failed_with(phases, "d1_approval_items") for phases in reports.values()), reports
    for route, (good, bad, location, findings) in ROUTE_PAGES.items():
        store(roots, route, good, d1=findings)

    for route, (good, bad, location, findings) in ROUTE_PAGES.items():
        store(roots, route, bad, d1=findings)                           # C fails on every route; B still holds
    result, reports = real_session(tmp_path, roots, routes)
    for route, (good, bad, location, findings) in ROUTE_PAGES.items():
        assert fully_passed(reports[f"tests/{files[compat]}::{compat}[{route}]"]), route
        assert failed_with(reports[f"tests/{files[scoped]}::{scoped}[{route}]"], location), route

    for route, (good, bad, location, findings) in ROUTE_PAGES.items():
        store(roots, route, good, metadata=replay_mutated(route, good), d1=findings)
    for path in roots.reviews.rglob("*.json"):
        path.unlink()                                                   # findings routes now lack their approval
    result, reports = real_session(tmp_path, roots, routes)
    for route, (good, bad, location, findings) in ROUTE_PAGES.items():
        for name in files:
            phases = reports[f"tests/{files[name]}::{name}[{route}]"]
            if findings:
                assert failed_with(phases, "d1_approval_missing"), (route, name)
            elif name == compat:
                assert failed_with(phases, "B.replay"), (route, name)
            elif name == scoped:
                assert fully_passed(phases), (route, name)             # C re-extracts; the stored text is not used
    assert result.returncode == 1


def test_a_used_hold_is_enforced_by_the_default_configuration(tmp_path, roots):
    store(roots, "citi_secondary", "secondary_c", metadata=selector_mutated("citi_secondary", "secondary_c"),
          d1=[CITI_FINDING])                                           # B fails; a hold waives it
    evidence = gate(roots, "citi_secondary")
    compat = f"tests/{real_file('test_original_pair_current_compatibility')}::test_original_pair_current_compatibility"
    scoped_file = real_file("test_original_pair_scoped_contract_evidence")
    replacement = f"tests/{scoped_file}::test_original_pair_scoped_contract_evidence[citi_secondary]"

    write_hold(roots, evidence, *hold_for(evidence, nodeid="tests/test_absent.py::test_absent")[1:])
    result, reports = real_session(tmp_path, roots, ("citi_secondary",), nodes=[compat])
    assert set(reports) == {compat + "[citi_secondary]"} and fully_passed(reports[compat + "[citi_secondary]"]), reports
    assert result.returncode == 1
    assert "bank capture hold FAILED: citi_secondary (hold.replacement_reports: field_contract)" in result.stdout

    write_hold(roots, evidence, *hold_for(evidence, nodeid=replacement)[1:])
    result, reports = real_session(tmp_path, roots, ("citi_secondary",),
                                   nodes=[compat, f"tests/{scoped_file}::test_original_pair_scoped_contract_evidence"])
    assert set(reports) == {compat + "[citi_secondary]", replacement}, reports
    assert all(fully_passed(phases) for phases in reports.values()), reports
    assert result.returncode == 0, result.stdout[-3000:]


# ════ E. B normalization and query keys keep C1d's meaning ═════════════════════

def test_normalization_drops_only_exception_line_numbers():
    record = {"events": [{"kind": "observed", "n": 2}, {"kind": "observed", "n": 1}],
              "queries": [{"method": "b", "args": [2, 1]}, {"method": "a", "args": []}],
              "returned": {"usd-krw": 1300.25, "jpy-krw": None},
              "exception": {"type": "ValueError", "args": ["bad", 3], "site": [["utils.py", "f", 7], ["x.py", "g", 9]]}}
    before = copy.deepcopy(record)
    expected = copy.deepcopy(record)
    expected["exception"]["site"] = [["utils.py", "f"], ["x.py", "g"]]
    assert C().normalize_recording(record) == expected
    assert record == before, "the input is not mutated"
    plain = {"events": [{"kind": "observed"}], "queries": [], "returned": {}, "exception": None}
    assert C().normalize_recording(plain) == plain


def test_query_keys_come_from_the_queries_module():
    assert C().query_key is queries.query_key


# ════ F. synthetic bank-capture tests never read the stored roots ══════════════

GUARD = textwrap.dedent('''
    import builtins, importlib.machinery, io, os, sys
    _roots = [os.path.realpath(p) for p in os.environ["BANK_CAPTURE_GUARD_ROOTS"].split(os.pathsep)]
    _log = os.environ["BANK_CAPTURE_GUARD_LOG"]

    _busy = []

    def _fd_path(fd):
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\\0", 1)[0].decode()
        return os.readlink("/proc/self/fd/%d" % fd)

    def _hit(path, dir_fd=None):
        if _busy:
            return  # realpath and the log write call the wrapped functions themselves
        try:
            text = os.fsdecode(path)
        except TypeError:
            return
        _busy.append(1)
        try:
            if dir_fd is not None and not os.path.isabs(text):
                text = os.path.join(_fd_path(dir_fd), text)
            real = os.path.realpath(text)
            if any(real == root or real.startswith(root + os.sep) for root in _roots):
                with _open(_log, "a", encoding="utf-8") as log:
                    log.write(real + "\\n")
        finally:
            _busy.pop()

    _open, _stat, _lstat, _scandir, _listdir, _osopen, _chdir, _access = (
        io.open, os.stat, os.lstat, os.scandir, os.listdir, os.open, os.chdir, os.access)

    def _wrap(real):
        def inner(path, *args, **kwargs):
            if not isinstance(path, int):
                _hit(path, kwargs.get("dir_fd"))
            return real(path, *args, **kwargs)
        return inner

    io.open = builtins.open = _wrap(_open)
    os.stat, os.lstat, os.open, os.chdir, os.access = (
        _wrap(_stat), _wrap(_lstat), _wrap(_osopen), _wrap(_chdir), _wrap(_access))
    os.scandir = lambda path=".": (_hit(path), _scandir(path))[1]
    os.listdir = lambda path=".": (_hit(path), _listdir(path))[1]

    _here = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.machinery.PathFinder.find_spec(
        "sitecustomize", [p for p in sys.path if os.path.abspath(p or ".") != _here])
    if _spec is not None and _spec.loader is not None:
        _module = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
''')


def guarded(tmp_path, args, roots):
    site = tmp_path / "guard_site"
    site.mkdir()
    (site / "sitecustomize.py").write_text("import importlib.util\n" + GUARD, encoding="utf-8")
    log = tmp_path / "guard.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, BANK_CAPTURE_GUARD_ROOTS=os.pathsep.join(str(r) for r in roots),
               BANK_CAPTURE_GUARD_LOG=str(log),
               PYTHONPATH=os.pathsep.join(filter(None, [str(site), os.environ.get("PYTHONPATH", "")])))
    result = subprocess.run([sys.executable, *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
    return result, log.read_text(encoding="utf-8").split()


def test_the_read_guard_sees_reads_including_in_child_processes(tmp_path):
    # Positive control on a root whose name says nothing: absolute read, a nested child, a relative read after a
    # directory change, and a symlinked alias.
    watched = tmp_path / "plain_root"
    (watched / "r").mkdir(parents=True)
    target = watched / "r" / "fixture.html"
    target.write_bytes(b"x")
    alias = tmp_path / "alias"
    alias.symlink_to(watched)
    script = ("import os, pathlib, subprocess, sys; pathlib.Path(sys.argv[1]).read_bytes(); "
              "subprocess.run([sys.executable, '-c', 'import os, sys; os.stat(sys.argv[1])', sys.argv[1]], check=True); "
              "os.chdir(sys.argv[2]); open('fixture.html', 'rb').read(); "
              "pathlib.Path(sys.argv[3]).read_bytes()")
    # os.access alone, in the parent and in a child, each must be seen on its own.
    access_parent = "import os, sys; os.access(sys.argv[1], os.R_OK)"
    access_child = ("import subprocess, sys; subprocess.run([sys.executable, '-c', "
                    "'import os, sys; os.access(sys.argv[1], os.R_OK)', sys.argv[1]], check=True)")
    # dir_fd: an fd on tmp_path (outside the root) and a relative path through the alias into the root.
    rel = os.path.relpath(alias / "r" / "fixture.html", tmp_path)
    dir_fd_parent = ("import os, sys; fd = os.open(sys.argv[2], os.O_RDONLY); "
                     "os.stat(sys.argv[3], dir_fd=fd); os.access(sys.argv[3], os.R_OK, dir_fd=fd)")
    dir_fd_child = ("import subprocess, sys; subprocess.run([sys.executable, '-c', "
                    "'import os, sys; fd = os.open(sys.argv[1], os.O_RDONLY); os.stat(sys.argv[2], dir_fd=fd)', "
                    "sys.argv[2], sys.argv[3]], check=True)")
    for name, only, want in (("access_parent", access_parent, 1), ("access_child", access_child, 1),
                             ("dir_fd_parent", dir_fd_parent, 2), ("dir_fd_child", dir_fd_child, 1)):
        (tmp_path / name).mkdir()
        result, hits = guarded(tmp_path / name, ["-c", only, str(target), str(tmp_path), rel], [watched])
        assert result.returncode == 0 and hits == [str(target.resolve())] * want, (name, hits, result.stderr[-1000:])
    result, hits = guarded(tmp_path, ["-c", script, str(target), str(watched / "r"),
                                      str(alias / "r" / "fixture.html")], [watched])
    assert result.returncode == 0, result.stderr[-2000:]
    assert hits.count(str(target.resolve())) >= 4, hits


def test_synthetic_bank_capture_tests_never_touch_the_stored_roots(tmp_path):
    files = sorted(p.name for p in (REPO / "tests").glob("test_bank_capture_*.py")
                   if p.name != "test_bank_capture_gate_contract.py")
    assert {"test_bank_capture_contract.py", "test_bank_capture_hold.py"} <= set(files), "the ported tests exist"
    deselect = [arg for filename, name in sorted(_real_test_functions())
                for arg in ("--deselect", f"tests/{filename}::{name}")]
    result, hits = guarded(tmp_path, ["-m", "pytest", "-q", "-p", "no:cacheprovider", *deselect,
                                      *(f"tests/{name}" for name in files)],
                           [Path(G().FIXTURE_ROOT), Path(G().REVIEWS_ROOT)])
    assert result.returncode == 0, result.stdout[-4000:]
    summary = result.stdout.strip().splitlines()[-1]
    assert " passed" in summary and "failed" not in summary and "error" not in summary, summary
    assert hits == [], sorted(set(hits))
