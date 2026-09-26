"""D7 5a 측정 게이트 도구(scripts/d7_ledger_measure_gate.py)의 판정 계약.

게이트가 유효한 구현을 합격시킬 수 있고, 판정 허점으로 거짓 합격을 내지 않으며,
외부 I/O·logger 없이 오프라인으로 돈다는 것만 잠근다. 측정 수치 자체는 여기서 보지 않는다.
"""
import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "d7_ledger_measure_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("d7_ledger_measure_gate_under_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


# ───────── G1 어댑터 검사는 source 마다 그 source 로 등록한다 ─────────

def test_adapter_status_accepts_real_adapter_for_every_checked_source(gate):
    status, rows = gate.adapter_status(limit=16, demand=16, warmup=1, samples=3, quick=True)
    assert status["status"] in {"PASS", "UNVERIFIED"}, status
    assert "observations" in status, status  # 예외 경로(UNVERIFIED + reason)는 합격이 아니다
    observations = status["observations"]
    assert {obs["source"] for obs in observations} == {"investing", "bs", "citi"}
    for obs in observations:
        assert obs["link"] == "linked", obs
        assert obs["finish"] == "finalized", obs
        assert obs["status"] == "PASS", obs
    assert all(row["status"] != "FAIL" for row in rows), rows


def test_register_args_can_register_a_non_default_source(gate):
    args = gate.register_args(0, source="investing")
    assert args["source"] == "investing"
    ledger = gate.new_ledger(1)
    assert ledger.register(**args)["classification"] == "registered"


# ───────── G2 오프라인: 파일 로깅을 초기화하지 않는다 ─────────

_OFFLINE_PROBE = r"""
import importlib.util, logging, sys
spec = importlib.util.spec_from_file_location("g", sys.argv[1])
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
g.adapter_status(limit=16, demand=16, warmup=1, samples=2, quick=True)
loggers = [logging.getLogger()] + [lg for lg in logging.root.manager.loggerDict.values()
                                   if isinstance(lg, logging.Logger)]
file_handlers = [h for lg in loggers for h in lg.handlers if isinstance(h, logging.FileHandler)]
print("FILE_HANDLERS", len(file_handlers))
"""


def test_gate_import_and_adapter_check_install_no_file_logging():
    done = subprocess.run([sys.executable, "-c", _OFFLINE_PROBE, str(GATE_PATH)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "FILE_HANDLERS 0" in done.stdout, done.stdout[-2000:]


# ───────── G3 AST 감사: 비교 면제는 키 membership 만 ─────────

_AUDIT_SAMPLE = '''
class L:
    def ok_membership(self, k):
        return k in self._records

    def ok_not_membership(self, k):
        return k not in self._records

    def bad_equality(self, other):
        return other == self._records

    def bad_equality_left(self, other):
        return self._records != other
'''


def test_audit_exempts_only_membership_compares(gate):
    uses = gate.audit_record_access(source_text=_AUDIT_SAMPLE)
    flagged = sorted(line for line, _kind in uses)
    tree = ast.parse(_AUDIT_SAMPLE)
    bad = sorted(node.lineno for node in ast.walk(tree)
                 if isinstance(node, ast.Compare)
                 and not all(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops))
    assert flagged == bad


def test_audit_default_reads_ledger_file(gate):
    assert gate.audit_record_access() == gate.audit_record_access(
        source_text=(ROOT / "app/d7_round_ledger.py").read_text())


# ───────── G4 시나리오 판정: 미검증 게이트는 합격이 아니다 ─────────

@pytest.mark.parametrize("visit,temp,time_,correct,expected", [
    ("PASS", "PASS", "PASS", True, "PASS"),
    ("PASS", "PASS", "UNVERIFIED", True, "UNVERIFIED"),
    ("UNVERIFIED", "PASS", "PASS", True, "UNVERIFIED"),
    ("PASS", "UNVERIFIED", "PASS", True, "UNVERIFIED"),
    ("FAIL", "PASS", "UNVERIFIED", True, "FAIL"),
    ("PASS", "FAIL", "PASS", True, "FAIL"),
    ("PASS", "PASS", "FAIL", True, "FAIL"),
    ("PASS", "PASS", "PASS", False, "FAIL"),
    ("PASS", "PASS", "UNVERIFIED", False, "FAIL"),
])
def test_scenario_status_table(gate, visit, temp, time_, correct, expected):
    assert gate.scenario_status(visit_gate=visit, temp_gate=temp, time_gate=time_,
                                correct=correct) == expected


def test_one_shot_derives_status_only_through_scenario_status(gate):
    tree = ast.parse(GATE_PATH.read_text())
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "one_shot")
    calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "scenario_status"]
    assert calls, "one_shot must compute its status via scenario_status"
    status_values = [value for node in ast.walk(fn) if isinstance(node, ast.Dict)
                     for key, value in zip(node.keys, node.values)
                     if isinstance(key, ast.Constant) and key.value == "status"]
    assert status_values, "one_shot result must carry a status"
    derived = {target.id for node in ast.walk(fn) if isinstance(node, ast.Assign)
               and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
               and node.value.func.id == "scenario_status"
               for target in node.targets if isinstance(target, ast.Name)}
    for value in status_values:
        if isinstance(value, ast.Name):
            assert value.id in derived, ast.dump(value)
        else:
            assert (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id == "scenario_status"), ast.dump(value)
