"""Synthetic holds only: exact bindings, approvals, KST and real pytest reports."""

import copy
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._fixture_capture import no_network
from tests import bank_capture_hold
from tests.bank_capture_contract import canonical_json, normalize_recording, replay
from tests.bank_capture_hold import (
    BINDING_FIELDS, COMPATIBILITY_PATH, HoldSession, KST, approval_path, validate_hold,
)
from tests.test_bank_capture_contract import synthetic_evidence
from tools.fixture_capture.admission import Evidence, sha256, validate_integrity
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.registry import Registry

pytest_plugins = ["pytester"]
NODEID = "test_replacement.py::test_replacement[USD]"
NOW = datetime(2026, 9, 19, 12, tzinfo=KST)
PASSED = [(phase, "passed", False) for phase in ("setup", "call", "teardown")]


def hold_case():
    evidence = synthetic_evidence("bs_official")
    current = replay(evidence, Registry())
    hold = {"fixture_id": evidence.fixture_id, "fixture_sha256": sha256(evidence.fixture),
            "metadata_sha256": sha256(evidence.metadata_bytes),
            "replay_sha256": sha256(canonical_json(normalize_recording(current))),
            "replacement_nodeid": NODEID, "review_by": "2026-09-20"}
    approval = {"approvals": [dict(hold, agent=agent, verdict="APPROVE_HOLD", statement="synthetic approval only")
                              for agent in ("Claude Code", "OpenAI Codex")]}
    hold["approval_record_sha256"] = sha256(canonical_json(approval))
    return evidence, current, hold, approval


def check(case, reports=None, now=NOW):
    evidence, current, hold, approval = case
    validate_hold(evidence, current, canonical_json(hold), canonical_json(approval),
                  {NODEID: PASSED} if reports is None else reports, now=now)


def test_valid_hold_and_exact_kst_midnight():
    case = hold_case()
    check(case)
    midnight = datetime(2026, 9, 20, tzinfo=KST)
    check(case, now=midnight - timedelta(microseconds=1))
    with pytest.raises(CaptureError, match="expired"):
        check(case, now=midnight)
    with pytest.raises(CaptureError, match="expired"):
        check(case, now=midnight.astimezone(timezone.utc))


@pytest.mark.parametrize("now,expired", [
    (datetime(2026, 9, 19, 14, 59, 59, 999999, tzinfo=timezone.utc), False),
    (datetime(2026, 9, 19, 15, tzinfo=timezone.utc), True),
    (datetime(2026, 9, 19, 19, 30, tzinfo=timezone.utc), True),
    (datetime(2026, 9, 19, 23, 59, 59, tzinfo=timezone.utc), True),
])
def test_expiry_in_the_nine_hour_kst_utc_gap(now, expired):
    # UTC inputs are independent of the implementation's KST constant.
    if expired:
        with pytest.raises(CaptureError, match="hold.expired"):
            check(hold_case(), now=now)
    else:
        check(hold_case(), now=now)


def test_fixture_id_binding_is_not_implied_by_metadata_hash():
    case = hold_case()
    check(case)
    evidence, _, hold, approval = case
    # Correct metadata bytes/hash still coexist with a consistently wrong ID in
    # the hold AND both approvals. Only the direct evidence-ID comparison stops it.
    wrong_id = "bs_official/11111111-1111-4111-8111-111111111111"
    assert wrong_id != evidence.fixture_id
    hold["fixture_id"] = wrong_id
    for item in approval["approvals"]:
        item["fixture_id"] = wrong_id
    hold["approval_record_sha256"] = sha256(canonical_json(approval))
    assert hold["metadata_sha256"] == sha256(evidence.metadata_bytes)
    with pytest.raises(CaptureError, match="hold.fixture_id"):
        check(case)


@pytest.mark.parametrize("mutation", ["whitespace", "claude_statement", "codex_statement"])
def test_approval_file_bytes_cannot_change_under_an_unchanged_hash(mutation):
    case = hold_case()
    check(case)
    evidence, current, hold, approval = case
    if mutation == "whitespace":
        raw = canonical_json(approval) + b"\n"
    else:
        index = 0 if mutation == "claude_statement" else 1
        approval["approvals"][index]["statement"] = "changed synthetic statement"
        raw = canonical_json(approval)
    with pytest.raises(CaptureError, match="hold.approval_record_sha256"):
        validate_hold(evidence, current, canonical_json(hold), raw, {NODEID: PASSED}, now=NOW)


@pytest.mark.parametrize("statement", ["", " \t\n", None, 7])
@pytest.mark.parametrize("agent_index", [0, 1])
def test_approval_statement_must_be_nonempty_text(statement, agent_index):
    case = hold_case()
    case[3]["approvals"][agent_index]["statement"] = statement
    case[2]["approval_record_sha256"] = sha256(canonical_json(case[3]))
    with pytest.raises(CaptureError, match="hold.approval.statement"):
        check(case)


@pytest.mark.parametrize("field", [*BINDING_FIELDS, "approval_record_sha256"])
def test_each_binding_change_invalidates_hold(field):
    case = hold_case()
    case[2][field] = "changed"
    with pytest.raises(CaptureError):
        check(case)


@pytest.mark.parametrize("target", ["fixture", "metadata", "replay"])
def test_changed_evidence_invalidates_an_unchanged_hold(target):
    evidence, current, hold, approval = hold_case()
    if target == "fixture":
        evidence = replace(evidence, fixture=evidence.fixture + b" ")
    elif target == "metadata":
        # Semantically identical JSON still has a distinct byte identity.
        evidence = replace(evidence, metadata_bytes=evidence.metadata_bytes + b" ")
    else:
        current["returned"]["usd-krw"] += 1
    with pytest.raises(CaptureError, match=f"hold.{target}_sha256"):
        check((evidence, current, hold, approval))


@pytest.mark.parametrize("field", BINDING_FIELDS)
@pytest.mark.parametrize("agent_index", [0, 1])
def test_approval_all_six_fields_exactly_match(field, agent_index):
    case = hold_case()
    case[3]["approvals"][agent_index][field] = "different"
    case[2]["approval_record_sha256"] = sha256(canonical_json(case[3]))
    with pytest.raises(CaptureError, match="hold.approval"):
        check(case)


@pytest.mark.parametrize("mutation", ["reject", "duplicate", "missing", "third", "extra", "statement_only"])
def test_approval_structure_not_prose(mutation):
    case = hold_case()
    approvals = case[3]["approvals"]
    if mutation == "reject":
        for item in approvals:
            item.update(verdict="REJECT", statement="Claude Code OpenAI Codex APPROVE_HOLD")
    elif mutation == "duplicate":
        approvals[1]["agent"] = approvals[0]["agent"]
    elif mutation == "missing":
        approvals.pop()
    elif mutation == "third":
        approvals.append(copy.deepcopy(approvals[0]))
    elif mutation == "extra":
        approvals[0]["extra"] = "unexpected"
    else:
        del approvals[0]["verdict"]
    case[2]["approval_record_sha256"] = sha256(canonical_json(case[3]))
    with pytest.raises(CaptureError, match="hold.approval"):
        check(case)


@pytest.mark.parametrize("review_by", [None, "", "2026-9-20", "2026-02-30", "2026-09-20T00:00:00+09:00"])
def test_review_date_format(review_by):
    case = hold_case()
    case[2]["review_by"] = review_by
    with pytest.raises(CaptureError, match="review_by"):
        check(case)


def test_review_date_is_required():
    case = hold_case()
    del case[2]["review_by"]
    with pytest.raises(CaptureError, match="hold"):
        check(case)


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("outcome,xfail", [("failed", False), ("skipped", False), ("passed", True)])
def test_all_phases_must_pass_without_wasxfail(phase, outcome, xfail):
    reports = [(p, outcome if p == phase else "passed", xfail if p == phase else False)
               for p in ("setup", "call", "teardown")]
    with pytest.raises(CaptureError, match="replacement_reports"):
        check(hold_case(), reports={NODEID: reports})


@pytest.mark.parametrize("reports", [{}, {NODEID: PASSED[:2]}, {NODEID: PASSED + PASSED},
                                     {"test_replacement.py::test_replacement[JPY]": PASSED}])
def test_unrun_incomplete_duplicate_or_wrong_parameter_reports_fail(reports):
    with pytest.raises(CaptureError, match="replacement_reports"):
        check(hold_case(), reports=reports)


def test_fresh_session_does_not_reuse_reports():
    first, second = HoldSession(), HoldSession()
    for phase in ("setup", "call", "teardown"):
        first.pytest_runtest_logreport(SimpleNamespace(nodeid=NODEID, when=phase, outcome="passed"))
    check(hold_case(), reports=first.reports)
    with pytest.raises(CaptureError, match="replacement_reports"):
        check(hold_case(), reports=second.reports)
    # Presence of wasxfail is disqualifying even if the value is empty/false.
    first.pytest_runtest_logreport(SimpleNamespace(nodeid="other", when="call", outcome="passed", wasxfail=""))
    assert first.reports["other"] == [("call", "passed", True)]


def test_fresh_session_does_not_reuse_waivers(tmp_path, monkeypatch):
    synthetic_hold(tmp_path)
    first, second = (HoldSession(tmp_path, reviews_root=tmp_path / "reviews", admitted=("bs_official",))
                     for _ in range(2))
    name = "test_original_pair_current_compatibility[bs_official]"
    monkeypatch.setattr(bank_capture_hold, "COMPATIBILITY_CASES", {name: "bs_official"})
    first.pytest_runtest_call(SimpleNamespace(path=COMPATIBILITY_PATH, name=name))
    assert first.held_routes == {"bs_official"}
    assert second.held_routes == set()


def test_serialization_and_line_only_replay_hash():
    assert canonical_json({"z": "일본", "a": [2, 1]}) == '{"a":[2,1],"z":"일본"}'.encode()
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    record = {"exception": {"type": "ValueError", "args": [], "site": [["utils.py", "f", 1]]}}
    moved = copy.deepcopy(record)
    moved["exception"]["site"][0][2] = 99
    assert canonical_json(normalize_recording(record)) == canonical_json(normalize_recording(moved))


def synthetic_hold(root):
    """New synthetic HTML/metadata/approvals; never write the original pairs."""
    probe = synthetic_evidence("bs_official")
    fixture = probe.fixture
    meta = copy.deepcopy(probe.metadata)
    current = replay(probe, Registry())
    # A legitimate B mismatch is deliberately held; A and C still pass.
    meta["recorded_extraction"]["queries"][0]["args"] = ["#historical td"]
    evidence = validate_integrity(fixture, canonical_json(meta), "bs_official")
    directory = root / evidence.route
    directory.mkdir(parents=True)
    (directory / "fixture.html").write_bytes(fixture)
    (directory / "metadata.json").write_bytes(evidence.metadata_bytes)
    hold = {"fixture_id": evidence.fixture_id, "fixture_sha256": sha256(fixture),
            "metadata_sha256": sha256(evidence.metadata_bytes),
            "replay_sha256": sha256(canonical_json(normalize_recording(current))),
            "replacement_nodeid": NODEID, "review_by": "2999-01-01"}
    approvals = {"approvals": [dict(hold, agent=agent, verdict="APPROVE_HOLD", statement="synthetic only")
                              for agent in ("Claude Code", "OpenAI Codex")]}
    path = approval_path(root, evidence.fixture_id)
    path.parent.mkdir()
    path.write_bytes(canonical_json(approvals))
    hold["approval_record_sha256"] = sha256(path.read_bytes())
    (directory / "hold.json").write_bytes(canonical_json(hold))
    return directory


@pytest.mark.parametrize("mode,success", [("passed", True), ("uncollected", False), ("deselected", False),
    ("skip", False), ("xfail", False), ("xpass", False), ("setup_skip", False),
    ("setup_error", False), ("teardown_error", False), ("wrong_parameter", False),
    ("bad_a", False), ("bad_c", False), ("missing_approval", False), ("expired", False),
    ("bare_return", False), ("hold_removed", False), ("unrelated_hold", True),
    ("partial_only", True), ("b_deselected", True), ("b_setup_skip", True),
    ("wrong_b_nodeid", True), ("partial_expired", True), ("partial_bad_a", True),
    ("b_rootdir_changed", False)])
def test_real_pytest_session_exit_gate(pytester, monkeypatch, mode, success):
    root = pytester.path / "bank_capture"
    directory = synthetic_hold(root)
    if mode in ("bad_a", "bad_c"):
        path = directory / "fixture.html"
        path.write_bytes(path.read_bytes().replace("매매".encode(), "다른".encode()) if mode == "bad_c"
                         else path.read_bytes() + b"<!--secret-->")
        # Update A's byte hash so bad_c specifically reaches the C gate.
        meta = json.loads((directory / "metadata.json").read_bytes())
        meta["fixture_sha256"] = sha256(path.read_bytes())
        (directory / "metadata.json").write_bytes(canonical_json(meta))
        # Rebind the synthetic approval to the mutated bytes/replay, so an A/C
        # bypass cannot be masked by an unrelated stale-hash failure.
        changed = Evidence("bs_official", path.read_bytes(), canonical_json(meta), meta)
        hold_path = directory / "hold.json"
        hold = json.loads(hold_path.read_bytes())
        hold.update(fixture_sha256=sha256(changed.fixture), metadata_sha256=sha256(changed.metadata_bytes),
                    replay_sha256=sha256(canonical_json(normalize_recording(replay(changed, Registry())))))
        approvals_path = approval_path(root, changed.fixture_id)
        approvals = json.loads(approvals_path.read_bytes())
        for item in approvals["approvals"]:
            item.update({key: hold[key] for key in BINDING_FIELDS})
        approvals_path.write_bytes(canonical_json(approvals))
        hold["approval_record_sha256"] = sha256(approvals_path.read_bytes())
        hold_path.write_bytes(canonical_json(hold))
    elif mode == "missing_approval":
        for path in (root / "approvals").iterdir():
            path.unlink()
    elif mode in ("expired", "partial_expired"):
        path = directory / "hold.json"
        hold = json.loads(path.read_bytes())
        hold["review_by"] = "2000-01-01"
        path.write_bytes(canonical_json(hold))
    elif mode == "partial_bad_a":
        (directory / "fixture.html").write_bytes(b"<!--synthetic invalid A-->")
    elif mode == "unrelated_hold":
        other = root / "citi_primary"
        other.mkdir()
        (other / "hold.json").write_bytes(b"{}")  # Never used by the selected B route.
    repo = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(repo), os.environ.get("PYTHONPATH", "")])))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    pytester.makeconftest(f'''
import importlib
from pathlib import Path
from tests._fixture_capture import redirect_app_logs, no_network
_guard = redirect_app_logs({str(pytester.path / 'logs')!r})
_guard.__enter__()
def pytest_configure(config):
    from tests import bank_capture_gate as gate
    gate.FIXTURE_ROOT = Path({str(root)!r})
    gate.REVIEWS_ROOT = Path({str(pytester.path / 'reviews')!r})
    setattr(gate, "ADMITTED_ROUTES", ("bs_official",))
    hold = importlib.reload(importlib.import_module("tests.bank_capture_hold"))
    config.pluginmanager.register(hold.HoldSession(
        compatibility_path={str(pytester.path / 'tests/test_bank_capture_contract.py')!r}), 'synthetic_holds')
def pytest_unconfigure(config):
    _guard.__exit__(None, None, None)
''')
    # Run the actual B entrypoint under its exact production nodeid. Its historical
    # selector mismatch must get a waiver here, and only here incur the finish gate.
    b_file = pytester.path / "tests" / "test_bank_capture_contract.py"
    b_file.parent.mkdir()
    b_source = '''
import pytest
from tests.test_bank_capture_contract import registry, test_original_pair_current_compatibility
'''
    if mode in ("bare_return", "hold_removed"):
        action = "return" if mode == "bare_return" else f"Path({str(directory / 'hold.json')!r}).unlink()"
        b_source = f'''
from pathlib import Path
import pytest
@pytest.mark.parametrize("route", ["bs_official"])
def test_original_pair_current_compatibility(route):
    {action}
'''
    elif mode == "b_setup_skip":
        b_source += '\npytestmark = pytest.mark.skip(reason="synthetic B setup skip")\n'
    if mode == "wrong_b_nodeid":
        b_file = b_file.with_name("test_other_contract.py")
        # Similar name in another file cannot incur a waiver for the canonical B test.
        b_source = '''
import pytest
@pytest.mark.parametrize("route", ["bs_official"])
def test_original_pair_current_compatibility(route):
    pass
'''
    b_file.write_text(b_source)
    b_nodeid = str(b_file.relative_to(pytester.path)) + "::test_original_pair_current_compatibility[bs_official]"
    marker = {"skip": '@pytest.mark.skip(reason="synthetic")',
              "xfail": '@pytest.mark.xfail(reason="synthetic")',
              "xpass": '@pytest.mark.xfail(reason="synthetic")'}.get(mode, "")
    body = 'assert False' if mode == "xfail" else 'pass'
    setup = {"setup_skip": 'pytest.skip("synthetic")', "setup_error": 'raise RuntimeError("synthetic")'}.get(mode, 'pass')
    teardown = 'raise RuntimeError("synthetic")' if mode == 'teardown_error' else 'pass'
    parameter = "JPY" if mode == "wrong_parameter" else "USD"
    pytester.makepyfile(test_replacement=f'''
import pytest
@pytest.fixture(autouse=True)
def boundary():
    {setup}
    yield
    {teardown}
{marker}
@pytest.mark.parametrize('currency', [{parameter!r}])
def test_replacement(currency):
    {body}
def test_other():
    pass
''')
    args = ["-q", "-p", "no:cacheprovider", "--confcutdir", str(pytester.path),
            "--rootdir", str(pytester.path)]
    if mode == "b_rootdir_changed":
        args += ["--rootdir", str(pytester.path.parent), b_nodeid, "test_replacement.py::test_other"]
    elif mode in ("uncollected", "bare_return"):
        args += [b_nodeid, "test_replacement.py::test_other"]
    elif mode == "deselected":
        args += [b_nodeid, "test_replacement.py", "-k", "test_other or test_original_pair_current_compatibility"]
    elif mode in ("partial_only", "partial_expired", "partial_bad_a"):
        args += ["test_replacement.py::test_other"]
    elif mode == "b_deselected":
        args += [b_nodeid, "test_replacement.py", "-k", "test_other"]
    else:
        args += [b_nodeid, "test_replacement.py"]
    result = pytester.runpytest_subprocess(*args, timeout=30)
    assert result.ret == (0 if success else 1)
    assert ("bank capture hold FAILED" in result.stdout.str()) is (not success)
    if mode in ("bad_a", "bad_c"):
        assert ("fixture.special_node" if mode == "bad_a" else "C.header_text") in result.stdout.str()
