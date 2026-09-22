"""C1d hold validation and per-session replacement-test proof.

No persistent pass cache: reports and B waivers are collected anew. Only routes
whose B test entered its call phase with a hold are checked at session finish.
"""

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests import bank_capture_gate
from tests.bank_capture_contract import canonical_json, normalize_recording, validate_contract_evidence
from tools.fixture_capture.admission import HASH_PATTERN, load_json, matches, require, sha256
from tools.fixture_capture.detector import _keys
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.registry import Registry

BINDING_FIELDS = ("fixture_id", "fixture_sha256", "metadata_sha256", "replay_sha256",
                  "replacement_nodeid", "review_by")
AGENTS = {"Claude Code", "OpenAI Codex"}
KST = ZoneInfo("Asia/Seoul")
COMPATIBILITY_PATH = Path(__file__).with_name("test_bank_capture_contract.py")
COMPATIBILITY_CASES = {
    f"test_original_pair_current_compatibility[{route}]": route
    for route in bank_capture_gate.ADMITTED_ROUTES
}


def approval_path(root, fixture_id):
    # fixture_id is derived only from A-validated route + canonical UUID4.
    return Path(root) / "approvals" / (fixture_id.replace("/", "__") + ".json")


def hold_present(route, root=None):
    root = bank_capture_gate.FIXTURE_ROOT if root is None else root
    path = Path(root) / route / "hold.json"
    return path.exists() or path.is_symlink()


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


class HoldSession:
    def __init__(self, root=None, *, reviews_root=None, admitted=None, compatibility_path=COMPATIBILITY_PATH):
        self.root = Path(bank_capture_gate.FIXTURE_ROOT if root is None else root)
        self.reviews_root = Path(bank_capture_gate.REVIEWS_ROOT if reviews_root is None else reviews_root)
        self.admitted = bank_capture_gate.ADMITTED_ROUTES if admitted is None else admitted
        self.compatibility_path = Path(compatibility_path).resolve()
        self.reports = {}
        self.held_routes = set()

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

    def pytest_runtest_logreport(self, report):
        self.reports.setdefault(report.nodeid, []).append(
            (report.when, report.outcome, hasattr(report, "wasxfail")))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        failures = []
        for route in sorted(self.held_routes):
            try:
                # Holds NEVER suppress A, D1 or C. Once used, removing hold.json
                # before session finish must fail, not erase the obligation.
                evidence = bank_capture_gate.admitted_evidence(
                    route, root=self.root, reviews_root=self.reviews_root, admitted=self.admitted)
                current = validate_contract_evidence(evidence, Registry())
                validate_hold(evidence, current, (self.root / route / "hold.json").read_bytes(),
                              approval_path(self.root, evidence.fixture_id).read_bytes(), self.reports)
            except CaptureError as error:
                failures.append(f"{route} ({error.location}: {error.rule})")
            except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
                # Do not echo page data, approval statements or arbitrary exception text.
                failures.append(route)
        if failures:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter:
                reporter.write_sep("=", "bank capture hold FAILED: " + ", ".join(failures), red=True)
