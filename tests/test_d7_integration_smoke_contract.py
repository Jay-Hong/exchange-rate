"""One bounded measure CLI → finalize CLI run, followed by evidence mutations."""

import importlib.util
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "d7_ledger_measure_gate.py"


def _gate():
    spec = importlib.util.spec_from_file_location("d7_smoke_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replace_fixture(report, index, **fields):
    changed = {**report}
    fixtures = list(report["pressure_fixtures"])
    fixtures[index] = {**fixtures[index], **fields}
    changed["pressure_fixtures"] = fixtures
    return changed


def _replace_scenario(report, name, **fields):
    changed = {**report}
    scenarios = list(report["scenarios"])
    index = next(i for i, row in enumerate(scenarios) if row["name"] == name)
    scenarios[index] = {**scenarios[index], **fields}
    changed["scenarios"] = scenarios
    return changed


def test_measure_to_finalize_integration_smoke_and_mutations(tmp_path):
    attachment_dir = tmp_path / "attachments"
    attachment_dir.mkdir()
    stdout_path = tmp_path / "measure.stdout"
    stderr_path = tmp_path / "measure.stderr"
    final_path = tmp_path / "final.json"
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    command = [sys.executable, str(GATE_PATH), "--integration-smoke", "--limit", "128",
               "--samples", "2", "--warmup", "0", "--churn-minutes", "41",
               "--churn-stride-minutes", "60", "--fixture-detail-divisor", "256",
               "--budget-seconds", "900", "--attachments-dir", str(attachment_dir)]
    started = time.perf_counter()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        measured = subprocess.run(command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr,
                                  timeout=900, check=False)
    elapsed = time.perf_counter() - started
    assert measured.returncode == 0
    assert elapsed < 900

    finalized = subprocess.run(
        [sys.executable, str(GATE_PATH), "finalize", "--stdout-log", str(stdout_path),
         "--stderr-log", str(stderr_path), "--exit-code", "0", "--attachments-dir",
         str(attachment_dir), "--out", str(final_path)],
        cwd=ROOT, env=env, capture_output=True, timeout=120, check=False)
    assert finalized.returncode == 0, finalized.stderr.decode(errors="replace")
    result = json.loads(final_path.read_text())
    assert result["acceptance_scope"] == "integration_smoke"
    assert result["overall"] == "PASS", result["acceptance_checklist"]
    assert {entry["row_id"]: entry["status"] for entry in result["acceptance_checklist"]
            if entry["row_id"].startswith("D0")} == {f"D0{i}": "PASS" for i in range(1, 6)}
    assert result["provisional_consistency"] == {"matches": True, "diffs": {}}
    gate = _gate()
    evidence = {entry["row_id"]: entry for entry in result["acceptance_checklist"]}
    assert set(gate.EVIDENCE_ROW_IDS) <= set(evidence)
    assert all(evidence[row]["status"] == "PASS" for row in gate.EVIDENCE_ROW_IDS
               if row not in {"A01", "A10", "C08", "C10"})
    assert {entry["row_id"] for entry in result["excluded_rows"]} == {"A01", "A10", "C10"}
    assert all(evidence[row]["status"] == "N/A" for row in ("A01", "A10", "C08", "C10"))

    report = json.loads(stdout_path.read_bytes())
    stderr = stderr_path.read_bytes()
    assert report["acceptance_scope"] == "integration_smoke"
    assert len(report["pressure_fixtures"]) == 14 and len(report["churn_fixtures"]) == 10
    assert all("api_trace_ref" in f["fixture_provenance"] for f in report["churn_fixtures"])
    attachments = {p.relative_to(attachment_dir).as_posix(): p
                   for p in attachment_dir.rglob("*.gz")}
    lines = stderr.decode().splitlines()
    for name, expected in (("pressure_slot_first_small_limit", "accepted=1"),
                           ("churn_most_finished_short_ascii", "batches=1"),
                           ("churn_fault_clock_step", "checked=1")):
        start = next(i for i, line in enumerate(lines) if line.startswith(f"start {name}"))
        end = next(i for i, line in enumerate(lines) if line.startswith(f"end {name} "))
        progress = [line for line in lines[start + 1:end]
                    if line.startswith(f"progress {name} ")]
        assert progress == [f"progress {name} {expected}"]

    def reject(mutant, stderr_bytes, row_id):
        verdict = gate._finalize_acceptance_parsed(mutant, False, stderr_bytes, 0, attachments)
        rows = {entry["row_id"]: entry for entry in verdict["acceptance_checklist"]}
        assert rows[row_id]["status"] != "PASS", (row_id, rows[row_id])
        assert rows["D01"]["status"] != "PASS"
        assert rows["D04"]["status"] != "PASS"
        assert verdict["provisional_consistency"]["matches"] is False

    # A02: both a wrong byte charge and the original cross-fixture ID collision.
    reject(_replace_fixture(report, 0,
                            id_utf8_bytes=report["pressure_fixtures"][0]["id_utf8_bytes"] + 1),
           stderr, "A02")
    first_id = next(step["id"] for step in report["pressure_fixtures"][0]
                    ["fixture_provenance"]["api_trace"] if step["action"] == "register"
                    and step["classification"] == "registered")
    second = report["pressure_fixtures"][1]
    trace = list(second["fixture_provenance"]["api_trace"])
    register_index = next(i for i, step in enumerate(trace) if step["action"] == "register"
                          and step["classification"] == "registered")
    trace[register_index] = {**trace[register_index], "id": first_id}
    reject(_replace_fixture(report, 1, fixture_provenance={**second["fixture_provenance"],
                                                          "api_trace": trace}), stderr, "A02")

    finish = next(row for row in report["scenarios"] if row["name"] == "finish_accept")
    observations = list(finish["sample_observations"])
    timed_index = next(i for i, row in enumerate(observations) if row["phase"] == "gc_disabled")
    observations[timed_index] = {key: value for key, value in observations[timed_index].items()
                                 if key != "prepared_at"}
    reject(_replace_scenario(report, "finish_accept", sample_observations=observations), stderr, "A11")

    temporary = next(row for row in report["scenarios"] if row["name"] == "link_round_accept")
    tm = {key: value for key, value in temporary["tracemalloc"].items()
          if key != "current_before"}
    reject(_replace_scenario(report, "link_round_accept", tracemalloc=tm), stderr, "A12")

    proof = {key: value for key, value in report["capacity_proof"].items()
             if key != "budget_q_size_steps"}
    reject({**report, "capacity_proof": proof}, stderr, "B22")

    for name in ("pressure_slot_first_small_limit", "churn_fault_clock_step"):
        without_progress = b"\n".join(line for line in stderr.splitlines()
                                      if not line.startswith(f"progress {name} ".encode())) + b"\n"
        reject(report, without_progress, "D05")

    # A smoke marker cannot be reinterpreted as full acceptance.
    reject({**report, "mode": "full"}, stderr, "SMOKE_SCOPE")

    # Restore two judge bugs seen in the prior full run. The same measured
    # smoke evidence must now fail the corresponding row and D04.
    for function_name, row_id, old, mutant in (
            ("_evaluate_b", "B03",
             'applicable = _items([f for f in fixtures if f.get("name") in CHURN_NAMES[-2:]])',
             "applicable = fixtures"),
            ("_evaluate_b_rest", "B18",
             'counter_key = "init_failed" if classification == "report_init_failed" else classification',
             "counter_key = classification")):
        original = getattr(gate, function_name)
        source = inspect.getsource(original)
        assert source.count(old) == 1
        namespace = {}
        exec(source.replace(old, mutant), gate.__dict__, namespace)
        setattr(gate, function_name, namespace[function_name])
        try:
            reject(report, stderr, row_id)
        finally:
            setattr(gate, function_name, original)
