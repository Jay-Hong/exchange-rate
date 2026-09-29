"""D7 5a-4c 최종 판정 단계(X1) 계약 시험.

근거(리포 밖 설계 폴더 design/d7-aggregation/):
- 최종 판정 단계 명세 r2 `codex_5a4c_finalize_spec_r2.md` sha256 83edf0324a17a0e93552d49e4b51d33358e9d6792b348d6c7e39df61a475602b
- 행별 판정식 명세 r2 `codex_5a4c_row_predicates_r2.md` sha256 d5114413a9d14af11741c7950cfb3055892a342951b8e34bdf6c8d2670116047
- 50행 표 `slice5a4c_final_checklist_r1.md`

시험 입력의 기준은 행별 명세의 `min_pass_example` 45개를 기계적으로 옮긴 `tests/fixtures/d7_5a4c/row_examples.json` 이다.
행마다 그 예시에 대해 PASS, 한 필수 경로를 지운 UNVERIFIED, 완전한 입력에서 판정식 하나를 어긴 FAIL 을 단언한다.
변형이 예시에 실제로 닿는지(지울 경로가 있는지, 바꾼 값이 원래 값과 다른지)도 함께 검사해 공허한 변형을 막는다.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "d7_ledger_measure_gate.py"
EXAMPLES = json.loads((ROOT / "tests" / "fixtures" / "d7_5a4c" / "row_examples.json").read_text(encoding="utf-8"))


def _load_gate():
    spec = importlib.util.spec_from_file_location("d7_gate_5a4c_finalize_under_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

EVIDENCE = ([f"A{i:02d}" for i in range(1, 13)] + [f"B{i:02d}" for i in range(1, 24)]
            + [f"C{i:02d}" for i in range(1, 11)])
ALL_ROWS = EVIDENCE + [f"D{i:02d}" for i in range(1, 6)]
STATUSES = {"PASS", "FAIL", "UNVERIFIED", "N/A"}

INDEX_PATHS = ["close", "cohort", "expiry", "live", "open", "overdue", "owned_id", "owner", "previous_job",
               "prune", "recent", "seq", "tomb"]


# ───────── 경로 조작(변형이 예시에 실제로 닿는지 검사) ─────────

def _walk(doc, path):
    node = doc
    for key in path[:-1]:
        node = node[key]
    return node, path[-1]


def _apply(doc, op):
    doc = copy.deepcopy(doc)
    kind, path = op[0], op[1]
    parent, last = _walk(doc, path)
    if kind == "del":
        if isinstance(parent, list):
            assert 0 <= last < len(parent), f"공허한 변형: {path} 없음"
        else:
            assert last in parent, f"공허한 변형: {path} 없음"
        del parent[last]
    else:
        value = op[2]
        assert (last in parent) if isinstance(parent, dict) else (0 <= last < len(parent)), f"공허한 변형: {path}"
        assert (type(parent[last]), parent[last]) != (type(value), value), f"공허한 변형: {path} 이미 {value!r}"
        parent[last] = value
    return doc


def _ex(row):
    return copy.deepcopy(EXAMPLES[row])


P0 = ["pressure_fixtures", 0]
CH0 = ["churn_fixtures", 0]
HW = CH0 + ["highwater_checks"]

# 행별 (UNVERIFIED 변형, FAIL 변형). 변형은 명세의 unverified / fail 조항 하나에 대응한다.
MUTATIONS = {
    "A01": (("del", ["limits", "max_records"]), ("set", ["limits", "max_records"], 131071)),
    "A02": (("del", P0 + ["fixture_provenance", "api_trace"]), ("set", P0 + ["id_getsizeof_bytes"], 50)),
    "A03": (("del", P0 + ["receipt_invariants", "checked_steps"]),
            ("set", P0 + ["receipt_invariants", "checked_steps", 0, "retire_eligible"], 1)),
    "A04": (("del", ["scenarios"]), ("set", P0 + ["status"], "FAIL")),
    "A05": (("del", P0 + ["transition_trace"]), ("set", P0 + ["transition_trace", 2, "detail_charge_bytes"], 0)),
    "A06": (("del", P0 + ["first_rejection", "byte_precheck"]), ("set", P0 + ["first_rejection", "cause"], "slot")),
    "A07": (("del", P0 + ["first_rejection", "index_audit", "after"]),
            ("set", P0 + ["first_rejection", "index_audit", "after", "paths", "live"], True)),
    "A08": (("del", P0 + ["post_latch", "coverage_by_source"]),
            ("set", P0 + ["post_latch", "resumed_after_release"], True)),
    "A09": (("del", P0 + ["post_latch", "existing_id_budget"]),
            ("set", P0 + ["post_latch", "existing_id_budget", 0, "AR_after"], 5)),
    "A10": (("del", ["unreachable_rows"]), ("set", ["131072_slots", "by_fixture", 0, "status"], "PASS")),
    "A11": (("del", ["scenarios", 0, "timing", "gc_disabled", "raw_ns"]), ("set", ["scenarios", 0, "visits"], 65)),
    "A12": (("del", ["scenarios", 0, "tracemalloc", "current_before"]), None),     # FAIL 은 아래 복합 변형
    "B01": (("del", CH0 + ["fixture_provenance", "api_trace"]),
            ("set", CH0 + ["fixture_provenance", "api_trace", 1, "classification"], "rejected")),
    "B02": (("del", CH0 + ["state_cycle"]), ("set", CH0 + ["state_cycle", 0, "action"], "unbound")),
    "B03": (("del", CH0 + ["fixture_provenance", "api_trace"]), ("set", CH0 + ["auxiliary_registered"], 1)),
    "B04": (("del", CH0 + ["checkpoints", 3]), ("set", CH0 + ["checkpoints", 2, "received_at"], 3600000001)),
    "B05": (("del", CH0 + ["fixture_provenance", "api_trace"]), ("set", CH0 + ["checkpoints", 1, "checkpoint_id"], "x:b")),
    "B06": (("del", CH0 + ["checkpoints", 0, "source_registered"]), ("set", CH0 + ["checkpoints", 0, "last_seq"], 2)),
    "B07": (("del", CH0 + ["checkpoints", 2, "identity_index_audit"]),
            ("set", CH0 + ["checkpoints", 2, "identity_index_audit", "samples", 0, "tomb"], True)),
    "B08": (("del", CH0 + ["checkpoints", 1, "frozen_totals_before_after"]),
            ("set", CH0 + ["checkpoints", 1, "frozen_totals_before_after", 0, "requery", "finished"], 2)),
    "B09": (("del", CH0 + ["checkpoints", 1, "diagnostic_counters"]),
            ("set", CH0 + ["checkpoints", 1, "diagnostic_counters", "retention_expired"], 2)),
    "B10": (("del", CH0 + ["tomb_due_ledger"]), ("set", CH0 + ["checkpoints", 0, "pruned_since_previous_hour"], 1)),
    "B11": (("del", CH0 + ["checkpoints", 1, "identity_index_audit"]),
            ("set", CH0 + ["checkpoints", 1, "prune_witness", "pruned_count"], 2)),
    "B12": (("del", CH0 + ["checkpoints", 0, "recent_expected_counts"]),
            ("set", CH0 + ["checkpoints", 1, "recent_cohort_counts", "investing", "registered"], 1)),  # X2 K11: recent_rows 는 유효성 배열
    "B13": (("del", CH0 + ["checkpoints", 0, "budget", "unknown_types"]),
            ("set", CH0 + ["checkpoints", 0, "budget", "AR"], 3)),
    "B14": (("del", CH0 + ["checkpoints", 1, "prune_witness"]),
            ("set", CH0 + ["checkpoints", 1, "last_received_at"], 11)),
    "B15": (("del", CH0 + ["checkpoints", 0, "oldest_retained_seq"]),
            ("set", CH0 + ["checkpoints", 0, "cursor_probes", 0, "entry_seqs"], [1])),
    "B16": (("del", CH0 + ["checkpoints", 0, "passive_cohort_projection"]),
            ("set", CH0 + ["checkpoints", 1, "recent_cohorts", "investing", "counts", "registered"], 2)),
    "B17": (("del", CH0 + ["checkpoints", 0, "passive_epoch_projection"]), ("set", CH0 + ["checkpoints", 1, "N_total"], 2)),
    "B18": (("del", CH0 + ["checkpoints", 0, "clock_isolation"]),
            ("set", CH0 + ["checkpoints", 0, "clock_isolation", "active"], True)),
    "B19": (("del", CH0 + ["checkpoints", 0, "measurement_refs"]), ("set", ["scenarios", 0, "visits"], 65)),
    "B20": (("del", ["scenarios", 0, "fault_evidence", "watermark_after"]),
            ("set", ["scenarios", 0, "fault_evidence", "normal_schedule_counted"], True)),
    "B21": (("del", CH0 + ["max_resident_observed"]), ("set", CH0 + ["max_resident_observed"], 0)),
    "B22": (("del", ["capacity_proof", "budget_q_size_steps"]), ("set", ["capacity_proof", "Q_cap_bytes"], 5703)),
    "B23": (("del", P0 + ["first_rejection", "index_audit"]), ("set", P0 + ["post_latch", "resumed_after_release"], True)),
    "C01": (("del", HW + ["register_attempts"]), ("set", HW + ["events"], 2)),
    "C02": (("del", ["capacity_proof", "checkpoints", 0]), ("set", HW + ["checks", 0, "preserved"], False)),
    "C03": (("del", ["capacity_proof", "checkpoints"]), ("set", CH0 + ["checkpoints", 1, "observation"], "public_advance")),
    "C04": (("del", HW + ["register_attempts"]), ("set", HW + ["register_attempts", 0, "after", "G"], 11)),
    "C05": (("del", HW + ["replay", "comparison_digest_by_seq"]), None),         # 명세상 FAIL 은 별도 Q/E/G 측정 위반뿐
    "C06": (("del", ["capacity_proof", "checkpoints", 0, "checkpoint_id"]), None),  # FAIL 은 아래 복합 변형
    "C07": (("del", ["scenarios", 0, "reason"]), ("set", CH0 + ["checkpoints", 0, "rebuild_count"], 1)),
    "C08": (("del", ["rebuild_events", 0, "during"]), ("set", ["rebuild_events", 0, "during", "E"], 11)),
    "C09": (("del", ["scenarios", 0, "tracemalloc", "current_before"]), None),  # FAIL 은 아래 복합 변형
    "C10": (("del", ["calibration", "observations"]), None),                    # FAIL 은 아래 복합 변형
}


def _compound_fail(row):
    """값 하나만 바꾸면 다른 식이 먼저 깨져 원인이 섞이는 행은, 관련 값을 함께 바꿔 한 한도만 어기게 한다."""
    doc = _ex(row)
    if row == "A12":                                    # peak−current_before = 262145 > 262144, 보고 delta 는 정확
        doc = _apply(doc, ("set", ["scenarios", 0, "tracemalloc", "peak"], 100 + 262145))
        return _apply(doc, ("set", ["scenarios", 0, "temporary_breakdown", "peak_delta"], 262145))
    if row == "C06":                                    # 고유 소유 합 = Q_actual = 3 > Q_4 = 2
        doc = _apply(doc, ("set", ["capacity_proof", "checkpoints", 0, "container_backings", 0, "Q_attributed_bytes"], 3))
        return _apply(doc, ("set", ["capacity_proof", "checkpoints", 0, "Q_actual_bytes"], 3))
    if row == "C09":                                    # 임시 peak 초과
        return _apply(doc, ("set", ["scenarios", 0, "tracemalloc", "peak"], 100 + 262145))
    if row == "C10":                                    # 합의 없이 ETA 가 기본 예산 초과
        doc = _apply(doc, ("set", ["eta", "seconds"], 14401))
        return doc
    raise KeyError(row)


def _fail_input(row):
    op = MUTATIONS[row][1]
    return _apply(_ex(row), op) if op is not None else _compound_fail(row)


FAIL_ROWS = [r for r in EVIDENCE if r != "C05"]


def _check_result(result, row, status):
    assert set(result) >= {"row_id", "status", "reason", "evidence_refs"}
    assert result["row_id"] == row
    assert result["status"] == status, (row, result)
    assert isinstance(result["evidence_refs"], list) and all(isinstance(x, str) for x in result["evidence_refs"])
    if status in ("FAIL", "UNVERIFIED"):
        assert isinstance(result["reason"], str) and result["reason"]


# ───────── 1. 공개 상수 ─────────

def test_row_id_constants_follow_table_order():
    assert tuple(gate.ROW_IDS) == tuple(ALL_ROWS)
    assert tuple(gate.EVIDENCE_ROW_IDS) == tuple(EVIDENCE)


@pytest.mark.parametrize("row", ["D01", "D02", "D03", "D04", "D05", "E01", "A13"])
def test_evaluate_row_rejects_non_evidence_rows(row):
    with pytest.raises(ValueError):
        gate.evaluate_row(row, {}, attachments={})


def test_examples_cover_exactly_the_evidence_rows():
    assert sorted(k for k in EXAMPLES if not k.startswith("_")) == sorted(EVIDENCE)
    assert sorted(MUTATIONS) == sorted(EVIDENCE)


# ───────── 2. 행별 PASS / UNVERIFIED / FAIL ─────────

@pytest.mark.parametrize("row", [r for r in EVIDENCE if r != "C08"])
def test_row_min_example_passes(row):
    _check_result(gate.evaluate_row(row, _ex(row), attachments={}), row, "PASS")


def test_c08_rebuild_example_passes():
    """행별 명세 r2: C08 예시는 rebuild 가 실제 있는 경우의 PASS 다."""
    _check_result(gate.evaluate_row("C08", _ex("C08"), attachments={}), "C08", "PASS")


@pytest.mark.parametrize("row", EVIDENCE)
def test_row_missing_required_path_is_unverified(row):
    doc = _apply(_ex(row), MUTATIONS[row][0])
    _check_result(gate.evaluate_row(row, doc, attachments={}), row, "UNVERIFIED")


@pytest.mark.parametrize("row", FAIL_ROWS)
def test_row_complete_violation_is_fail(row):
    _check_result(gate.evaluate_row(row, _fail_input(row), attachments={}), row, "FAIL")


def test_empty_report_is_unverified_for_every_evidence_row():
    for row in EVIDENCE:
        result = gate.evaluate_row(row, {}, attachments={})
        assert result["status"] == "UNVERIFIED", (row, result)


def test_bool_is_not_an_integer():
    """행별 명세 §0: JSON bool 은 정수로 인정하지 않는다."""
    doc = _apply(_ex("B06"), ("set", CH0 + ["checkpoints", 0, "N_tomb"], False))
    assert gate.evaluate_row("B06", doc, attachments={})["status"] == "UNVERIFIED"


def test_summary_flags_do_not_replace_raw_evidence():
    """요약 bool 이 참이어도 원자료 식이 거짓이면 PASS 가 아니다(A07 no_insertion 만 참, 감사는 삽입)."""
    doc = _apply(_ex("A07"), ("set", P0 + ["first_rejection", "index_audit", "before", "paths", "seq"], True))
    assert gate.evaluate_row("A07", doc, attachments={})["status"] == "FAIL"


# ───────── 3. N/A ─────────

def test_c08_without_rebuild_is_verified_na():
    """C07 조건이 검증되면 C08 은 `N/A (no rebuild in this implementation)`."""
    result = gate.evaluate_row("C08", _ex("C07"), attachments={})
    assert result["status"] == "N/A"
    assert "no rebuild" in result["reason"]


def test_c08_na_without_reason_is_unverified():
    doc = _apply(_ex("C07"), ("del", ["scenarios", 0, "reason"]))
    assert gate.evaluate_row("C08", doc, attachments={})["status"] == "UNVERIFIED"


def test_c07_row_itself_is_never_na():
    assert gate.evaluate_row("C07", _ex("C07"), attachments={})["status"] == "PASS"


# ───────── 4. C04 부속 파일 ─────────

REF_PATH = "attempts/churn_x.jsonl.gz"


def _attempts_with_ref(rows=None, *, path=REF_PATH, ref_overrides=None):
    doc = _ex("C04")
    hw = doc["churn_fixtures"][0]["highwater_checks"]
    lines = rows if rows is not None else hw["register_attempts"]
    raw = b"".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                   for r in lines)
    blob = gzip.compress(raw, mtime=0)
    del hw["register_attempts"]
    ref = {"path": path, "sha256": hashlib.sha256(blob).hexdigest(), "rows": len(lines),
           "first_seq": lines[0]["register_seq"], "last_seq": lines[-1]["register_seq"]}
    ref.update(ref_overrides or {})
    hw["register_attempts_ref"] = ref
    return doc, {path: blob}


def test_c04_reference_verified_passes():
    doc, att = _attempts_with_ref()
    assert gate.evaluate_row("C04", doc, attachments=att)["status"] == "PASS"


@pytest.mark.parametrize("case", ["missing", "hash", "rows", "seq_gap", "abs_path", "dotdot", "basename_key",
                                  "selected_layout_missing"])
def test_c04_reference_defects_are_unverified(case):
    if case == "seq_gap":
        row = copy.deepcopy(_ex("C04")["churn_fixtures"][0]["highwater_checks"]["register_attempts"][0])
        row["register_seq"] = 1
        doc, att = _attempts_with_ref([row])
    elif case == "selected_layout_missing":                     # 선택 사건의 layout 원자료 없이는 결합을 검증할 수 없다
        row = copy.deepcopy(_ex("C04")["churn_fixtures"][0]["highwater_checks"]["register_attempts"][0])
        assert row["selected"] is True
        del row["normalized_layout"]
        doc, att = _attempts_with_ref([row])
    elif case == "abs_path":
        doc, att = _attempts_with_ref(path="/tmp/attempts.jsonl.gz")
    elif case == "dotdot":
        doc, att = _attempts_with_ref(path="attempts/../../x.jsonl.gz")
    else:
        doc, att = _attempts_with_ref()
    if case == "missing":
        att = {}
    elif case == "hash":
        doc["churn_fixtures"][0]["highwater_checks"]["register_attempts_ref"]["sha256"] = "0" * 64
    elif case == "rows":
        doc["churn_fixtures"][0]["highwater_checks"]["register_attempts_ref"]["rows"] = 2
    elif case == "basename_key":
        att = {Path(REF_PATH).name: att[REF_PATH]}
    assert gate.evaluate_row("C04", doc, attachments=att)["status"] == "UNVERIFIED", case


def test_c04_verified_reference_with_budget_violation_is_fail():
    row = copy.deepcopy(_ex("C04")["churn_fixtures"][0]["highwater_checks"]["register_attempts"][0])
    row["after"]["G"] = 11
    doc, att = _attempts_with_ref([row])
    assert gate.evaluate_row("C04", doc, attachments=att)["status"] == "FAIL"


# ───────── 5. finalize: stdout / stderr / 종료 코드 (D05) ─────────

GOOD_STDERR = b"start record_baseline\nend record_baseline PASS\n"


def _report(**extra):
    base = {"mode": "full", "complete": True, "aborted_reason": None, "exit_code": 0,
            "overall": "PASS", "verdicts": {"slice5a2_partial": "PASS", "slice5a3": "PASS", "slice5a4": "PASS"},
            "acceptance_stage": "provisional"}
    base.update(extra)
    return base


def _stdout(report):
    return json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def _finalize(stdout=None, stderr=GOOD_STDERR, exit_code=0, attachments=None):
    return gate.finalize_acceptance(stdout_bytes=_stdout(_report()) if stdout is None else stdout,
                                    stderr_bytes=stderr, exit_code=exit_code, attachments=attachments or {})


def _rows(out):
    return {r["row_id"]: r for r in out["acceptance_checklist"]}


def test_finalize_output_shape():
    out = _finalize()
    assert [r["row_id"] for r in out["acceptance_checklist"]] == ALL_ROWS
    assert all(r["status"] in STATUSES for r in out["acceptance_checklist"])
    assert set(out["verdicts"]) == {"slice5a2_partial", "slice5a3", "slice5a4"}
    assert set(out["verdict_reasons"]) >= {"slice5a2_partial", "slice5a3", "slice5a4"}
    assert out["overall"] == _rows(out)["D01"]["status"]
    assert set(out["provisional_consistency"]) >= {"matches", "diffs"}


def test_d05_pass_on_clean_output():
    assert _rows(_finalize())["D05"]["status"] == "PASS"


@pytest.mark.parametrize("stdout", [b"", b"   \n", _stdout(_report()) * 2, b"{not json\n", b"[1,2]\n", b"42\n"],
                         ids=["empty", "blank", "two", "invalid", "array", "number"])
def test_no_usable_report_yields_failure_output(stdout):
    out = _finalize(stdout=stdout)
    rows = _rows(out)
    assert rows["D05"]["status"] == "FAIL" and rows["D01"]["status"] == "FAIL" and out["overall"] == "FAIL"
    assert all(rows[r]["status"] == "UNVERIFIED" and rows[r]["reason"] for r in EVIDENCE)
    for r in ("D02", "D03", "D04"):
        assert rows[r]["status"] == "UNVERIFIED"
    assert set(out["verdicts"].values()) == {"UNVERIFIED"}


def test_extra_stdout_text_fails_d05_but_report_is_still_evaluated():
    report = _report(limits=EXAMPLES["A01"]["limits"])
    out = _finalize(stdout=b"hello\n" + _stdout(report))
    rows = _rows(out)
    assert rows["D05"]["status"] == "FAIL"
    assert rows["A01"]["status"] == "PASS"                     # report 는 평가됐다


@pytest.mark.parametrize("stderr", [
    b"progress record_baseline 1/2\nstart record_baseline\nend record_baseline PASS\n",   # 첫 기록이 start 가 아님
    b"start record_baseline\nend record_baseline PASS\nprogress x 1/2\n",               # 마지막 기록이 end 가 아님
    b"start record_baseline\nstart link_round_accept\nprogress link_round_accept 100/1000\nend record_baseline PASS\n",
    b"start link_round_accept\nend link_round_accept PASS\n",                          # 반복 측정인데 progress 없음
    b"start pressure_p0_short_ascii\nend pressure_p0_short_ascii PASS\n",              # 압력 원본인데 progress 없음
    b"start churn_most_finished_short_ascii\nend churn_most_finished_short_ascii PASS\n",
    b"start link_round_accept\nend link_round_accept PASS\nprogress link_round_accept 100/1000\n",  # 진행이 짝 밖
], ids=["first_not_start", "last_not_end", "unmatched", "no_progress_measured", "no_progress_pressure",
        "no_progress_churn", "progress_outside_pair"])
def test_d05_stderr_contract_violations_fail(stderr):
    assert _rows(_finalize(stderr=stderr))["D05"]["status"] == "FAIL"


@pytest.mark.parametrize("stderr", [
    b"start link_round_accept 0/1000 gc=setup\nprogress link_round_accept 100/1000 gc=off\nend link_round_accept PASS\n",
    b"start pressure_p0_short_ascii\nprogress pressure_p0_short_ascii accepted=1000\nend pressure_p0_short_ascii PASS\n",
    b"start churn_x\nprogress churn_x batches=60\nend churn_x PASS\n",
    b"start resident_unfinished\nend resident_unfinished PASS\nstart adapter_bs_link\nend adapter_bs_link PASS\n"
    b"start capacity_highwater\nend capacity_highwater PASS\nstart rebuild_double_backing\nend rebuild_double_backing N/A\n",
])
def test_d05_accepts_documented_progress_shapes(stderr):
    assert _rows(_finalize(stderr=stderr))["D05"]["status"] == "PASS"


def test_d05_empty_stderr_is_unverified():
    assert _rows(_finalize(stderr=b""))["D05"]["status"] == "UNVERIFIED"


def test_d05_nonzero_exit_fails():
    assert _rows(_finalize(exit_code=1))["D05"]["status"] == "FAIL"


def test_d05_report_exit_code_must_match_actual():
    assert _rows(_finalize(stdout=_stdout(_report(exit_code=1))))["D05"]["status"] == "FAIL"


def test_d05_contradictory_abort_marking_fails():
    assert _rows(_finalize(stdout=_stdout(_report(complete=False, aborted_reason=None))))["D05"]["status"] == "FAIL"
    assert _rows(_finalize(stdout=_stdout(_report(complete=True, aborted_reason="budget"))))["D05"]["status"] == "FAIL"


def test_properly_marked_abort_is_not_accepted_but_not_an_output_failure():
    stderr = b"start link_round_accept\nprogress link_round_accept 100/1000\n"
    out = _finalize(stdout=_stdout(_report(complete=False, aborted_reason="budget")), stderr=stderr)
    assert _rows(out)["D05"]["status"] == "UNVERIFIED"
    assert out["overall"] == "UNVERIFIED"


# ───────── 6. 집계 순서 (D01 / D02–D04) — evaluate_row 를 바꿔 끼운 시험 ─────────

D02_SCENARIOS = [
    "link_round_accept", "report_init_failed_accept", "wrapper_exited_accept", "contributions_tail_empty",
    "aggregation_empty", "cohort_empty", "finish_accept", "link_round_duplicate", "finish_near_valid_limit",
    "finish_over_input_limit", "finish_duplicate", "contributions_first_page", "contributions_after_cursor",
    "aggregation_recent", "cohort_all", "register_accept", "register_capacity_reject", "close_boundary_before",
    "close_boundary_exact", "large_clock_jump", "close_same_time_requery", "mass_close_boundary", "mass_overdue",
    "stale_overdue_end_cursor", "stale_overdue_requery", "multi_bucket_close", "multi_bucket_close_requery",
    "multi_bucket_close_merge_failure", "multi_bucket_close_retry", "multi_bucket_close_retry_requery",
    "reverse_cohort_register_tail", "reverse_cohort_empty", "reverse_cohort_narrow", "identity_absent_link",
    "identity_absent_finish", "missing_record_direct", "missing_record_first_query_cohort",
    "missing_record_first_query_contributions", "missing_record_first_query_aggregation", "open_end_cursor",
    "open_last_seq_cursor", "recent_window_seq_order", "recent_window_boundary", "recent_window_before_exclusion",
    "recent_window_after_exclusion", "recent_window_before_close", "recent_window_exact_close",
    "close_wait_target_advance", "recent_outside_open", "recent_outside_open_one_inside",
]
ADAPTERS = ["adapter_investing_link", "adapter_investing_finish", "adapter_bs_link", "adapter_bs_finish",
            "adapter_citi_link", "adapter_citi_finish", "adapter_bank_to_finish"]
COMMIT = "a" * 40
# run 6 보고서의 실제 adapter 관측 3건(run6_projection.json.gz 에서 그대로 읽는다 — 손으로 만든 빈 객체는 D02.P2 내용 검증을 통과할 수 없다)
RUN6_ADAPTER_OBS = json.loads(gzip.decompress(
    (ROOT / "tests" / "fixtures" / "d7_5a4c" / "run6_projection.json.gz").read_bytes()))["adapter"]["observations"]
LOCK_KEYS = ("d7", "index", "slice5a3", "slice5a4a", "slice5a4b")


def _locked(keys=LOCK_KEYS):
    tests, att = {}, {}
    for key in keys:
        blob = json.dumps({"suite": key, "passed": 10, "failed": 0, "commit_sha": COMMIT}).encode()
        ref = f"locked/{key}.txt"
        att[ref] = blob
        tests[key] = {"passed": 10, "failed": 0, "commit_sha": COMMIT, "result_ref": ref,
                      "result_sha256": hashlib.sha256(blob).hexdigest()}
    return tests, att


def _d02_report(**extra):
    scen = [{"name": n, "status": "PASS", "visit_gate": "PASS", "temporary_gate": "PASS", "time_gate": "PASS"}
            for n in D02_SCENARIOS + ADAPTERS]
    for s in scen[:len(D02_SCENARIOS)]:                  # 명세 D02.P2: 적용 S 의 측정 원자료(GC 양쪽 1000 표본)를 자체 조건으로 확인
        s.update(D=0, K=0, visits=0, visit_limit=64,
                 timing={kind: {"n": 1000, "raw_ns": [1000] * 1000, "p99_us": 1, "max_us": 1}
                         for kind in ("gc_disabled", "gc_enabled")})
    scen += [{"name": n, "status": "PASS"} for n in ("resident_unfinished", "resident_2048_details",
                                                     "resident_released_identity", "resident_capacity_stop")]
    locked, att = _locked()
    report = _report(scenarios=scen, adapter={"status": "PASS", "observations": copy.deepcopy(RUN6_ADAPTER_OBS)},
                     locked_tests=locked, environment={"commit_sha": COMMIT},
                     record_access_audit_findings=[], prerequisites={"record_access_audit": True})
    report.update(extra)
    return report, att


def _stub(monkeypatch, statuses, default="PASS"):
    def fake(row, report, *, attachments):
        if row not in EVIDENCE:
            raise ValueError(row)
        status = statuses.get(row, default)
        return {"row_id": row, "status": status, "reason": None if status == "PASS" else f"stub {status}",
                "evidence_refs": []}
    monkeypatch.setattr(gate, "evaluate_row", fake)


def _fin_d02(monkeypatch, statuses, default="PASS", **extra):
    _stub(monkeypatch, statuses, default)
    report, att = _d02_report(**extra)
    return _finalize(stdout=_stdout(report), attachments=att)


def test_all_pass_yields_overall_pass(monkeypatch):
    out = _fin_d02(monkeypatch, {})
    assert out["overall"] == "PASS"
    assert out["verdicts"] == {"slice5a2_partial": "PASS", "slice5a3": "PASS", "slice5a4": "PASS"}


def test_fail_has_priority_over_unverified(monkeypatch):
    out = _fin_d02(monkeypatch, {"B01": "UNVERIFIED", "C06": "FAIL"})
    assert out["overall"] == "FAIL" and _rows(out)["D01"]["status"] == "FAIL"


def test_unverified_without_fail(monkeypatch):
    assert _fin_d02(monkeypatch, {"B19": "UNVERIFIED"})["overall"] == "UNVERIFIED"


def test_d05_participates_in_d01(monkeypatch):
    _stub(monkeypatch, {})
    report, att = _d02_report()
    out = _finalize(stdout=_stdout(report), attachments=att, exit_code=1)
    assert _rows(out)["D05"]["status"] == "FAIL" and out["overall"] == "FAIL"


def test_unpermitted_na_counts_as_unverified(monkeypatch):
    assert _fin_d02(monkeypatch, {"A01": "N/A"})["overall"] == "UNVERIFIED"


def _with_no_rebuild_evidence(report):
    """C07 예시의 no-rebuild 근거(모든 C 의 rebuild 0·old/new 0, 사건 배열 공백, 이유 있는 scenario)를 보고서에 싣는다."""
    c07 = _ex("C07")
    report["churn_fixtures"] = c07["churn_fixtures"]
    report["rebuild_events"] = c07["rebuild_events"]
    report["scenarios"] = report["scenarios"] + c07["scenarios"]
    return report


def test_permitted_c08_na_with_evidence_is_excluded(monkeypatch):
    """최종 판정 명세 r2 §2-4: C08 N/A 는 비적용 조건과 근거 필드를 확인했을 때만 제외한다."""
    _stub(monkeypatch, {"C08": "N/A"})
    report, att = _d02_report()
    out = _finalize(stdout=_stdout(_with_no_rebuild_evidence(report)), attachments=att)
    assert out["overall"] == "PASS" and _rows(out)["C08"]["status"] == "N/A"


def test_c08_na_without_evidence_counts_as_unverified(monkeypatch):
    """행 평가가 N/A 를 내도 근거 필드가 보고서에 없으면 제외하지 않는다."""
    out = _fin_d02(monkeypatch, {"C08": "N/A"})
    assert out["overall"] == "UNVERIFIED"


def test_d02_is_independent_of_other_rows(monkeypatch):
    """D02 는 자기 대상만 본다. 대상 밖 행이 UNVERIFIED 여도 PASS."""
    out = _fin_d02(monkeypatch, {"A01": "PASS"}, default="UNVERIFIED")
    assert out["overall"] == "UNVERIFIED"
    assert out["verdicts"]["slice5a2_partial"] == "PASS" and _rows(out)["D02"]["status"] == "PASS"
    assert out["verdicts"]["slice5a3"] != "PASS" and out["verdicts"]["slice5a4"] != "PASS"


@pytest.mark.parametrize("breaker", ["scenario_gate", "adapter_scenario", "adapter_obs", "locked_failed",
                                     "locked_commit", "locked_hash", "locked_missing_file", "audit_findings",
                                     "locked_file_not_json", "locked_file_wrong_suite", "locked_file_value_mismatch"])
def test_d02_own_conditions(monkeypatch, breaker):
    _stub(monkeypatch, {})
    report, att = _d02_report()
    if breaker == "scenario_gate":
        report["scenarios"][0]["visit_gate"] = "FAIL"
    elif breaker == "adapter_scenario":
        next(s for s in report["scenarios"] if s["name"] == "adapter_bs_link")["status"] = "FAIL"
    elif breaker == "adapter_obs":
        report["adapter"]["observations"] = [{}, {}]
    elif breaker == "locked_failed":
        report["locked_tests"]["d7"]["failed"] = 1
    elif breaker == "locked_commit":
        report["locked_tests"]["index"]["commit_sha"] = "b" * 40
    elif breaker == "locked_hash":
        report["locked_tests"]["d7"]["result_sha256"] = "0" * 64
    elif breaker == "locked_missing_file":
        del att["locked/d7.txt"]
    elif breaker == "audit_findings":
        report["record_access_audit_findings"] = [{"path": "x"}]
    elif breaker.startswith("locked_file_"):
        blob = {"locked_file_not_json": b"d7 10 passed\n",
                "locked_file_wrong_suite": json.dumps({"suite": "index", "passed": 10, "failed": 0,
                                                        "commit_sha": COMMIT}).encode(),
                "locked_file_value_mismatch": json.dumps({"suite": "d7", "passed": 9, "failed": 0,
                                                           "commit_sha": COMMIT}).encode()}[breaker]
        att["locked/d7.txt"] = blob
        report["locked_tests"]["d7"]["result_sha256"] = hashlib.sha256(blob).hexdigest()
    out = _finalize(stdout=_stdout(report), attachments=att)
    assert out["verdicts"]["slice5a2_partial"] != "PASS", breaker
    assert out["overall"] == "PASS"                             # D02 는 overall 로 되먹이지 않는다


def test_d02_requires_applied_scenario_timing_raw(monkeypatch):
    """D02.P2: 적용 S 의 시간 원자료가 없으면 5a-2 부분 수락을 PASS 로 내지 않는다."""
    _stub(monkeypatch, {})
    report, att = _d02_report()
    del report["scenarios"][0]["timing"]
    assert _finalize(stdout=_stdout(report), attachments=att)["verdicts"]["slice5a2_partial"] != "PASS"


def test_d02_applied_scenario_time_limit_violation_is_fail(monkeypatch):
    _stub(monkeypatch, {})
    report, att = _d02_report()
    gd = report["scenarios"][0]["timing"]["gc_disabled"]
    gd.update(raw_ns=[30_000_000] * 1000, p99_us=30000, max_us=30000)
    assert _finalize(stdout=_stdout(report), attachments=att)["verdicts"]["slice5a2_partial"] == "FAIL"


def test_d02_measured_violation_is_fail(monkeypatch):
    _stub(monkeypatch, {})
    report, att = _d02_report()
    report["scenarios"][0]["status"] = "FAIL"
    assert _finalize(stdout=_stdout(report), attachments=att)["verdicts"]["slice5a2_partial"] == "FAIL"


def test_d03_d04_require_overall_pass(monkeypatch):
    out = _fin_d02(monkeypatch, {"B01": "UNVERIFIED"})
    assert out["verdicts"]["slice5a2_partial"] == "PASS"
    assert out["verdicts"]["slice5a3"] == "UNVERIFIED" and out["verdicts"]["slice5a4"] == "UNVERIFIED"
    assert "B01" in json.dumps(out["verdict_reasons"]["slice5a4"])


def test_d03_d04_need_their_own_locked_tests(monkeypatch):
    _stub(monkeypatch, {})
    report, att = _d02_report()
    del report["locked_tests"]["slice5a4b"]
    out = _finalize(stdout=_stdout(report), attachments=att)
    assert out["overall"] == "PASS"
    assert out["verdicts"]["slice5a3"] == "PASS" and out["verdicts"]["slice5a4"] == "UNVERIFIED"


def test_verdicts_do_not_feed_back_into_overall(monkeypatch):
    _stub(monkeypatch, {})
    report, att = _d02_report()
    del report["locked_tests"]
    out = _finalize(stdout=_stdout(report), attachments=att)
    assert out["overall"] == "PASS" and _rows(out)["D01"]["status"] == "PASS"
    assert set(out["verdicts"].values()) == {"UNVERIFIED"}


def test_provisional_values_are_not_inputs(monkeypatch):
    _stub(monkeypatch, {"B01": "UNVERIFIED"})
    report, att = _d02_report(overall="PASS")
    out = _finalize(stdout=_stdout(report), attachments=att)
    assert out["overall"] == "UNVERIFIED"
    assert out["provisional_consistency"]["matches"] is False and out["provisional_consistency"]["diffs"]


# ───────── 7. 스텁 없는 소형 왕복 ─────────

def test_round_trip_without_stubs():
    report = _report(limits=EXAMPLES["A01"]["limits"])
    out = _finalize(stdout=_stdout(report))
    rows = _rows(out)
    assert rows["A01"]["status"] == "PASS"
    assert rows["B01"]["status"] == "UNVERIFIED"
    assert rows["D05"]["status"] == "PASS"
    assert out["overall"] == "UNVERIFIED"
    assert out["provisional_consistency"]["matches"] is False




# ───────── 8. CLI ─────────

def _cli(tmp_path, stdout_bytes, *, exit_code=0, attachments=None):
    (tmp_path / "out.stdout").write_bytes(stdout_bytes)
    (tmp_path / "out.stderr").write_bytes(GOOD_STDERR)
    att_dir = tmp_path / "att"
    att_dir.mkdir(exist_ok=True)
    for rel, blob in (attachments or {}).items():
        p = att_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    out = tmp_path / "final.json"
    proc = subprocess.run([sys.executable, str(GATE_PATH), "finalize", "--stdout-log", str(tmp_path / "out.stdout"),
                           "--stderr-log", str(tmp_path / "out.stderr"), "--exit-code", str(exit_code),
                           "--attachments-dir", str(att_dir), "--out", str(out)],
                          capture_output=True, cwd=ROOT, timeout=120)
    return proc, out


def test_cli_normal_writes_output(tmp_path):
    proc, out = _cli(tmp_path, _stdout(_report()))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [r["row_id"] for r in data["acceptance_checklist"]] == ALL_ROWS


@pytest.mark.parametrize("stdout", [b"", _stdout(_report()) * 2, b"{oops\n"], ids=["none", "duplicate", "invalid"])
def test_cli_writes_failure_output_and_nonzero_exit(tmp_path, stdout):
    proc, out = _cli(tmp_path, stdout)
    assert proc.returncode != 0
    data = json.loads(out.read_text(encoding="utf-8"))
    rows = _rows(data)
    assert rows["D05"]["status"] == "FAIL" and data["overall"] == "FAIL"


def _c04_unit_report(doc):
    """C04 단위 입력(mode=predicate_unit)을 D05 가 읽는 필드와 합친다. C04 는 mode 와 무관하게 참조를 검증한다."""
    report = _report()
    report.update(doc)
    return report


def test_cli_reads_attachments_by_exact_relative_path(tmp_path):
    doc, att = _attempts_with_ref()
    proc, out = _cli(tmp_path, _stdout(_c04_unit_report(doc)), attachments=att)
    assert proc.returncode == 0, proc.stderr
    assert _rows(json.loads(out.read_text(encoding="utf-8")))["C04"]["status"] == "PASS"


def test_cli_attachment_under_other_path_is_unverified(tmp_path):
    doc, att = _attempts_with_ref()
    proc, out = _cli(tmp_path, _stdout(_c04_unit_report(doc)), attachments={"attempts/elsewhere.jsonl.gz": att[REF_PATH]})
    assert _rows(json.loads(out.read_text(encoding="utf-8")))["C04"]["status"] == "UNVERIFIED"


def test_cli_rejects_symlink_escaping_attachments_dir(tmp_path):
    outside = tmp_path / "outside.jsonl.gz"
    doc, att = _attempts_with_ref()
    outside.write_bytes(att[REF_PATH])
    att_dir = tmp_path / "att"
    (att_dir / "attempts").mkdir(parents=True)
    (att_dir / REF_PATH).symlink_to(outside)
    proc, out = _cli(tmp_path, _stdout(_c04_unit_report(doc)))
    assert _rows(json.loads(out.read_text(encoding="utf-8")))["C04"]["status"] == "UNVERIFIED"


def test_measurement_cli_unchanged_without_subcommand():
    proc = subprocess.run([sys.executable, str(GATE_PATH), "--help"], capture_output=True, cwd=ROOT, timeout=120)
    assert proc.returncode == 0 and b"--budget-seconds" in proc.stdout


# ───────── 9. 게이트 본 실행의 잠정 표시(2a) ─────────

def test_gate_report_marks_acceptance_as_provisional():
    report = gate.run_gate(limit=64, samples=2, warmup=0, rows=["aggregation_empty"], max_resident_bytes=62_914_560,
                           churn_minutes=25, churn_stride_minutes=60, fixture_detail_divisor=256)
    assert report["acceptance_stage"] == "provisional"


# ───────── 10. 실제 full 보고서 형태(run 6 투영) — full 분기 회귀 잠금 ─────────
# 위 시험은 전부 mode=predicate_unit 이라 full 분기(규모 상수·원본 이름·track 표기·kind 집합)를 실행하지 않는다.
# run 6 은 계약 검토에서 한도 위반이 없고 신규 필드만 없는 보고서로 판정됐다(slice5a4c_final_checklist_r1.md ⑤열).
# 따라서 그 투영에서 어떤 행도 FAIL 이면 안 되고, 표가 run 6 원자료만으로 '충족'이라 한 A01·A04·A10 은 PASS 여야 한다.
# (투영이 churn checkpoints 를 뺐으므로 그것이 필요한 행은 UNVERIFIED 가 정상이다.)

RUN6 = json.loads(gzip.decompress((ROOT / "tests" / "fixtures" / "d7_5a4c" / "run6_projection.json.gz").read_bytes()))


def _run6():
    doc = copy.deepcopy(RUN6)
    doc.pop("_projection")
    return doc


def test_run6_projection_is_a_real_full_report():
    doc = _run6()
    assert doc["mode"] == "full" and doc["complete"] is True
    assert sorted({p["track"] for p in doc["pressure_fixtures"] if p["track"].startswith("p")}) == ["p0", "p1", "p2", "p3"]
    assert {p["id_kind"] for p in doc["pressure_fixtures"]} == {"short_ascii", "ascii128", "unicode128"}


@pytest.mark.parametrize("row", EVIDENCE)
def test_run6_projection_has_no_false_fail(row):
    result = gate.evaluate_row(row, _run6(), attachments={})
    assert result["status"] in ("PASS", "UNVERIFIED", "N/A"), (row, result)


@pytest.mark.parametrize("row", ["A01", "A04", "A10"])
def test_run6_projection_rows_the_checklist_marked_satisfied_pass(row):
    _check_result(gate.evaluate_row(row, _run6(), attachments={}), row, "PASS")


def test_run6_projection_finalize_is_unverified_not_fail():
    doc = _run6()
    stderr = b"start record_baseline\nend record_baseline PASS\n"
    out = gate.finalize_acceptance(stdout_bytes=_stdout(doc), stderr_bytes=stderr, exit_code=0, attachments={})
    assert out["overall"] == "UNVERIFIED"
    assert not [r["row_id"] for r in out["acceptance_checklist"] if r["status"] == "FAIL"]


# ───────── 11. 조항 단위 위반(명세 조항 → 판정 변화 실측) ─────────
# clauses_M*.json 은 Codex 가 대응표(codex_x1_coverage_verdict.md)의 조항마다 낸 '그 조항 하나만 어기는 입력'을
# 기계적으로 옮긴 것이다. 기준 예시가 PASS 이고, 변형 뒤 기대 상태가 나와야 한다(조항별 판정 변화 실측).
# 격리 불가(not_isolatable) 항목은 사유와 함께 건너뛰고, 그 조항은 다른 절의 시험이 다룬다(예: C04 부속 참조 → §4).

CLAUSE_FILES = sorted((ROOT / "tests" / "fixtures" / "d7_5a4c").glob("clauses_M*.json"))
CLAUSES = [c for f in CLAUSE_FILES for c in json.loads(f.read_text(encoding="utf-8"))]


def test_clause_cases_are_loaded():
    assert CLAUSE_FILES and CLAUSES
    assert {c["base"] for c in CLAUSES} <= {"example", "report"}
    assert {c.get("variant") for c in CLAUSES} <= {None, "extended", "attachment", "selected_ref", "sync_digest", "sync_locked"}


# 아래 적용기는 M4 결과(codex_x1_M4_report.md)의 재현 스크립트 규칙을 그대로 옮긴 것이다.
# base=example: 행 예시에 ops 적용 후 evaluate_row. base=report: 시험의 _d02_report() + 행 평가 stub(기본 PASS)으로
# finalize 를 돌려 해당 D행 상태를 읽는다. ops 경로 첫 요소가 report/attachments/stub/stdout/stderr/exit_code 이면
# 그 상태 칸을, 아니면 보고서를 가리킨다. variant 는 부속 bytes·digest·잠금 결과를 스크립트 안에서 만든다.

_CLAUSE_STATE_KEYS = ("report", "attachments", "stub", "stdout", "stderr", "exit_code")


def _clause_digest(row):
    fields = ("input", "classification", "before", "after", "normalized_layout", "selected")
    raw = json.dumps({k: row[k] for k in fields}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _clause_state(case):
    row = case["row"]
    if case["base"] == "example":
        report, att = _ex(row), {}
    else:
        report, att = _d02_report()
    variant = case.get("variant")
    if variant == "extended":
        report["eta"].update(seconds=14500, budget_seconds=15000, decision="agreement_required")
        report["budget_agreement_ref"] = "agreement/1"
        report["run_started_at"] = 5
        report["budget_agreement"] = {"ref": "agreement/1", "approved_budget_seconds": 15000,
                                      "signature": "signed", "signed_at": 4}
    if variant == "selected_ref":
        attempt = copy.deepcopy(EXAMPLES["C04"]["churn_fixtures"][0]["highwater_checks"]["register_attempts"][0])
        fixture = report["churn_fixtures"][0]
        h = fixture["highwater_checks"]
        h["register_attempts"] = [attempt]
        h["checks"] = [{"register_seq": 0, "preserved": True,
                        "before": copy.deepcopy(attempt["before"]), "after": copy.deepcopy(attempt["after"])}]
        fixture["checkpoints"] = [
            {"checkpoint_id": "m4:b", "register_seq": 0, "kind": "highwater_before", "capacity_ref": "m4:b"},
            {"checkpoint_id": "m4:a", "register_seq": 0, "kind": "highwater_after", "capacity_ref": "m4:a"}]
        report["capacity_proof"] = {"checkpoints": [{"checkpoint_id": "m4:b"}, {"checkpoint_id": "m4:a"}]}
        value = _clause_digest(attempt)
        h["replay"].update(performed=True, stopped_after_seq=0, compared_steps=1,
                           comparison_digest_by_seq=[{"register_seq": 0, "pass1_sha256": value,
                                                      "pass2_sha256": value, "selected": True}])
    if variant in ("attachment", "selected_ref"):
        h = report["churn_fixtures"][0]["highwater_checks"]
        rows = h.pop("register_attempts")
        blob = gzip.compress("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode(), mtime=0)
        ref = "attempts/m4.jsonl.gz"
        att[ref] = blob
        h["register_attempts_ref"] = {"path": ref, "sha256": hashlib.sha256(blob).hexdigest(),
                                      "rows": len(rows), "first_seq": 0, "last_seq": len(rows) - 1}
    if case["base"] == "example":
        assert gate.evaluate_row(row, report, attachments=att)["status"] == "PASS", case["clause"]
    return {"report": report, "attachments": att, "stub": {}, "stdout": "normal", "stderr": "normal", "exit_code": 0}


def _clause_apply_op(state, op):
    kind, path = op[0], op[1]
    node = state if path[0] in _CLAUSE_STATE_KEYS else state["report"]
    for key in path[:-1]:
        node = node[key]
    if kind == "set":
        node[path[-1]] = op[2]
    elif kind == "del":
        del node[path[-1]]
    elif kind == "append":
        node[path[-1]].append(op[2])
    else:
        raise ValueError(kind)


def _clause_result(case, monkeypatch, tmp_path):
    state = _clause_state(case)
    before = copy.deepcopy(state)
    assert case["ops"], case["clause"]
    for op in case["ops"]:
        _clause_apply_op(state, op)
    assert state != before, f"공허한 변형: {case['clause']}"
    report, att = state["report"], state["attachments"]
    variant = case.get("variant")
    if variant == "sync_digest":
        h = report["churn_fixtures"][0]["highwater_checks"]
        value = _clause_digest(h["register_attempts"][0])
        h["replay"]["comparison_digest_by_seq"][0].update(pass1_sha256=value, pass2_sha256=value)
    if variant == "sync_locked":
        entry = report["locked_tests"]["slice5a4b"]
        blob = json.dumps({"suite": "slice5a4b", "passed": entry["passed"], "failed": entry["failed"],
                           "commit_sha": entry["commit_sha"]}).encode()
        att[entry["result_ref"]] = blob
        entry["result_sha256"] = hashlib.sha256(blob).hexdigest()
    row = case["row"]
    if case["base"] == "example":
        return gate.evaluate_row(row, report, attachments=att)["status"]
    _stub(monkeypatch, state["stub"])
    stdout = json.dumps(report).encode()
    if state["stdout"] == "malformed_outer":
        stdout = b'{"bad": ' + stdout + b" trailing}"
    elif state["stdout"] == "invalid":
        stdout = b"{invalid"
    stderr = GOOD_STDERR if state["stderr"] == "normal" else b""
    if row == "CLI":
        (tmp_path / "stdout").write_bytes(stdout)
        (tmp_path / "stderr").write_bytes(stderr)
        (tmp_path / "att").mkdir()
        out = tmp_path / "final.json"
        proc = subprocess.run([sys.executable, str(GATE_PATH), "finalize", "--stdout-log", str(tmp_path / "stdout"),
                               "--stderr-log", str(tmp_path / "stderr"), "--exit-code", str(state["exit_code"]),
                               "--attachments-dir", str(tmp_path / "att"), "--out", str(out)],
                              cwd=ROOT, capture_output=True, timeout=120)
        assert proc.returncode != 0 and out.is_file(), (proc.returncode, proc.stderr)
        return _rows(json.loads(out.read_text(encoding="utf-8")))["D05"]["status"]
    data = gate.finalize_acceptance(stdout_bytes=stdout, stderr_bytes=stderr, exit_code=state["exit_code"],
                                    attachments=att)
    return _rows(data)[row]["status"]


@pytest.mark.parametrize("case", [c for c in CLAUSES if "not_isolatable" not in c],
                         ids=lambda c: f"{c['row']}-{c['clause']}")
def test_clause_violation_changes_verdict(case, monkeypatch, tmp_path):
    assert _clause_result(case, monkeypatch, tmp_path) == case["expect"]


# Claude 독립 추가: 위반 입력 표의 항목이 조항 문장의 일부만 겨냥한 곳(A03.P2 mono·N_tomb, A06.P2 후보 source·job·health).
EXTRA_CLAUSE_CASES = [
    ("A03", "A03.P2 첫 거절 mono=T", [("set", P0 + ["first_rejection", "received_mono"], 11)], "FAIL"),
    ("A03", "A03.P2 거절 전 N_tomb=0", [("set", P0 + ["before", "N_tomb"], 1)], "FAIL"),
    ("A03", "A03.P2 거절 후 N_tomb=0", [("set", P0 + ["after", "N_tomb"], 1)], "FAIL"),
    ("A06", "A06.P2 후보 source 대조", [("set", P0 + ["first_rejection", "candidate_source"], "citi")], "FAIL"),
    ("A06", "A06.P2 후보 job 대조", [("set", P0 + ["first_rejection", "candidate_job_id"], "k")], "FAIL"),
    ("A06", "A06.P2 거절 전 health 는 latch 전", [("set", P0 + ["first_rejection", "health_before", "admission_stopped"], True)], "FAIL"),
    # M3: B18.P2·B19.P2 는 표 항목이 필드 삭제(U)만 겨냥 — 완전 관측의 FAIL 쪽을 독립 추가
    ("B18", "B18.P2 원인 없는 counter 증가", [("set", CH0 + ["checkpoints", 0, "diagnostic_counters", "init_failed"], 1)], "FAIL"),
    ("B18", "B18.P2 원인 없는 불확실 source", [("set", CH0 + ["checkpoints", 0, "uncertain_sources"], ["investing"])], "FAIL"),
    ("B19", "B19.P2 참조 S 의 시간 한도 초과", [
        ("set", ["scenarios", 0, "timing", "gc_disabled", "raw_ns"], [30_000_000]),
        ("set", ["scenarios", 0, "timing", "gc_disabled", "p99_us"], 30000),
        ("set", ["scenarios", 0, "timing", "gc_disabled", "max_us"], 30000)], "FAIL"),
    ("B19", "B19.P2 참조 S 의 p99 재계산 불일치", [("set", ["scenarios", 0, "timing", "gc_disabled", "p99_us"], 2)], "FAIL"),
]


@pytest.mark.parametrize("row,label,ops,expect", EXTRA_CLAUSE_CASES, ids=[c[1] for c in EXTRA_CLAUSE_CASES])
def test_extra_clause_violation(row, label, ops, expect):
    doc = _ex(row)
    for op in ops:
        doc = _apply(doc, op)
    _check_result(gate.evaluate_row(row, doc, attachments={}), row, expect)


# ───────── 12. 커밋 전 검토(codex_x1_final_report.md)가 드러낸 시험 빈틈 ─────────

def test_clause_table_size_is_locked():
    """clauses_M1–M4 는 대응표 210 조항(격리 불가 6)을 담는다. 파일이 줄면 조항 시험이 조용히 빠진다."""
    assert len(CLAUSES) == 210
    assert sum("not_isolatable" in c for c in CLAUSES) == 6


def test_duplicate_json_is_no_report_even_when_first_object_is_valid():
    """중복 JSON 이면 첫 객체가 유효해도 report 없음 — A01 이 평가될 수 있는 보고서로 확인(이전 시험은 limits 가 없어 공허)."""
    report = _report(limits=EXAMPLES["A01"]["limits"])
    rows = _rows(_finalize(stdout=_stdout(report) * 2))
    assert rows["A01"]["status"] == "UNVERIFIED" and rows["D05"]["status"] == "FAIL"


def _b19_two_checkpoints(second_count):
    doc = _ex("B19")
    fx = doc["churn_fixtures"][0]
    fx["fixture_provenance"]["api_trace"] = [{"classification": "prune", "event_order": 0},
                                             {"classification": "prune", "event_order": 2}]
    fx["checkpoints"] = [{"event_order": 1, "measurement_refs": ["prune_transition"], "transition_counts": {"prune": 1}},
                         {"event_order": 3, "measurement_refs": ["prune_transition"],
                          "transition_counts": {"prune": second_count}}]
    return doc


def test_b19_counts_are_checked_per_checkpoint_prefix():
    """B19: checkpoint 의 전이 건수는 그 시점까지의 trace 접두와 같아야 한다(전체 합계 비교 아님)."""
    assert gate.evaluate_row("B19", _b19_two_checkpoints(2), attachments={})["status"] == "PASS"
    early = _b19_two_checkpoints(2)
    early["churn_fixtures"][0]["checkpoints"][0]["transition_counts"]["prune"] = 2   # 최종 합계를 앞 지점에 복사
    assert gate.evaluate_row("B19", early, attachments={})["status"] == "FAIL"


def test_c07_reason_must_match_exactly():
    doc = _apply(_ex("C07"), ("set", ["scenarios", 0, "reason"], "not no rebuild in this implementation"))
    assert gate.evaluate_row("C07", doc, attachments={})["status"] != "PASS"


def test_d05_report_exit_code_bool_is_not_zero():
    assert _rows(_finalize(stdout=_stdout(_report(exit_code=False))))["D05"]["status"] != "PASS"


def test_d02_on_properly_marked_abort_is_unverified_not_fail(monkeypatch):
    _stub(monkeypatch, {})
    report, att = _d02_report(complete=False, aborted_reason="budget")
    out = _finalize(stdout=_stdout(report), attachments=att)
    assert out["verdicts"]["slice5a2_partial"] == "UNVERIFIED"


def test_b04_schedule_start_is_first_normal_register_not_call_zero():
    """B04: 일정 시작은 '첫 정상 register'. 보조 호출이 앞서도 정상 호출 기준으로 경계를 계산한다."""
    doc = _ex("B04")
    trace = doc["churn_fixtures"][0]["fixture_provenance"]["api_trace"]
    for t in trace:
        t["call_index"] += 1
    trace.insert(0, {"action": "register", "auxiliary": True, "call_index": 0, "received_at": 0, "received_mono": 0})
    assert gate.evaluate_row("B04", doc, attachments={})["status"] == "PASS"


# ───────── 13. B22 CPython 잠금은 '보고서를 만든 인터프리터 == 판정하는 인터프리터' ─────────
# size-step 은 판정 시 실행 중인 인터프리터에서 재계산한다. 특정 패치 버전(3.13.5)을 박아 두면
# CI(3.13.x 다른 패치)에서 단위 예시까지 UNVERIFIED 가 된다(d2fc628 CI 실패). full 보고서는
# environment.python 의 버전이 실행 중 버전과 같을 때만 비교하고, 다르면 UNVERIFIED.

import platform  # noqa: E402  (이 절 전용)


def test_b22_unit_example_does_not_depend_on_patch_version(monkeypatch):
    monkeypatch.setattr(gate.sys, "version_info", (3, 13, 99, "final", 0), raising=False)
    assert gate.evaluate_row("B22", _ex("B22"), attachments={})["status"] == "PASS"


def _b22_full(python_version):
    doc = _ex("B22")
    doc["mode"] = "full"
    doc.pop("test_scale")
    doc["environment"] = {"python": f"{python_version} (main, Jan 1 2026, 00:00:00) [Clang]",
                          "implementation": platform.python_implementation(), "python_hash_seed": "0"}
    doc["churn_fixtures"] = [{"name": n, "job_key_counts": {"investing:j": 1}} for n in gate.CHURN_NAMES]  # full 범위 검사 통과용
    return doc


def test_b22_full_report_from_other_interpreter_is_unverified():
    result = gate.evaluate_row("B22", _b22_full("3.12.0"), attachments={})
    assert result["status"] == "UNVERIFIED" and "CPython" in result["reason"]


def test_b22_full_report_from_same_interpreter_passes_version_gate():
    result = gate.evaluate_row("B22", _b22_full(platform.python_version()), attachments={})
    assert "CPython" not in (result["reason"] or "")


@pytest.mark.parametrize("field,value", [("implementation", "PyPy"), ("python_hash_seed", "1"), ("python_hash_seed", None)])
def test_b22_full_report_environment_lock(field, value):
    """명세 B22: 환경 CPython/hash 잠금 부족은 U — 구현체가 다르거나 hash seed 가 0 이 아니면(또는 없으면) UNVERIFIED."""
    doc = _b22_full(platform.python_version())
    if value is None:
        del doc["environment"][field]
    else:
        doc["environment"][field] = value
    result = gate.evaluate_row("B22", doc, attachments={})
    assert result["status"] == "UNVERIFIED" and ("CPython" in result["reason"] or field in result["reason"])



# ───────── 14. X2 0단위 계약 정합(게이트 산출 형식 ↔ 판정기) ─────────
# conflicts_K.json 은 Codex 충돌 표(codex_x2_u0_conflicts.md)의 (b)·(c) 12건마다 PASS 입력과 FAIL/U 입력을
# 담는다. 형식은 clauses 와 같아 §11 적용기로 돌린다. 명세 정정 3건(K03·K11·K14)은
# design/.../codex_5a4c_row_predicates_r2_amend1.md.

CONFLICTS = json.loads((ROOT / "tests" / "fixtures" / "d7_5a4c" / "conflicts_K.json").read_text(encoding="utf-8"))


def test_conflict_cases_cover_the_twelve_contract_fixes():
    ids = {c["clause"].split(".")[0] for c in CONFLICTS}
    assert ids == {"K01", "K02", "K03", "K04", "K05", "K06", "K08", "K10", "K11", "K12", "K13", "K14"}
    assert len(CONFLICTS) == 24
    for k in ids:
        expects = {c["expect"] for c in CONFLICTS if c["clause"].split(".")[0] == k}
        assert "PASS" in expects and expects & {"FAIL", "UNVERIFIED"}, k


@pytest.mark.parametrize("case", CONFLICTS, ids=lambda c: f"{c['row']}-{c['clause']}")
def test_conflict_contract_case(case, monkeypatch, tmp_path):
    if case["expect"] == "PASS" and not case["ops"]:
        assert gate.evaluate_row(case["row"], _ex(case["row"]), attachments={})["status"] == "PASS"
        return
    assert _clause_result(case, monkeypatch, tmp_path) == case["expect"]


def test_b23_existing_key_is_matched_by_source_and_job_pair():
    """K14 보강(Codex X2 0단위 커밋 검토 반례): 기존 key 는 (source, job_id) 쌍이다. 등록된 key 는 (bs, j0) 인데
    clone 대조와 clone trace 의 source 를 함께 investing 으로 바꾸면 존재하지 않는 (investing, j0) 이므로 PASS 가 아니다."""
    doc = _ex("B23")
    p = doc["pressure_fixtures"][0]
    assert p["job_key_counts"] == [{"job_id": "j0", "registrations": 1, "source": "bs"}]
    p["control_existing_key"]["source"] = "investing"
    p["fixture_provenance"]["clone_api_trace"][0]["source"] = "investing"
    assert gate.evaluate_row("B23", doc, attachments={})["status"] == "FAIL"


# ───────── 15. X2 A단위: 실제 압력 원본 → 행별 투영 → 판정 ─────────
# 작은 한도로 게이트의 run_pressure_fixture 를 실제로 돌리고, 결과 원본을 predicate_unit 행 입력으로 옮겨
# A02·A03·A05–A09·B23 이 PASS 인지 본다. 투영은 관측값을 바꾸지 않는다(구간 절단·표기·규모 표시만).
# 투영은 Codex X2 A 보고(codex_x2_a_report.md)의 project() 에서 구간 절단·표기 변환을 뺀 것(판정기 몫).

from app import d7_round_ledger as _lg  # noqa: E402

_A_F4 = _lg.RoundLedger("E1", aggregation_started_at=0).budget_state()["F_4"]
_A_KW = dict(limit=4096, max_resident_bytes=_A_F4 + 1_000_000)


def _project_pressure(row, pressure):
    """관측값·구간·표기를 바꾸지 않는다. predicate_unit 이 요구하는 규모 표시(test_scale)만 원본에서 읽어 붙인다.
    A03·A06 의 '첫 거절까지' 범위와 track 표기(p0 등)는 판정기가 스스로 다뤄야 한다(full 보고서엔 투영이 없다)."""
    fixture = copy.deepcopy(pressure)
    scale = {}
    if row == "A05":
        scale["detail_target"] = fixture["effective_detail_target"]
    if row == "A08":
        scale["sources"] = list(fixture["post_latch"]["coverage_by_source"])
    if row == "A09":
        scale["existing_transitions"] = list(fixture["post_latch"]["existing_id_transitions"])
    return {"mode": "predicate_unit", "pressure_fixtures": [fixture],
            "limits": {"max_records": fixture["fixture_limit"]}, "test_scale": scale}


_A_CASES = [
    ("pressure_p0_short_ascii", ("A02", "A03", "A06", "A07", "A08", "A09")),
    ("pressure_p2_short_ascii", ("A05",)),
    ("pressure_unique_job_keys_byte_stop", ("B23",)),
]


@pytest.mark.parametrize("name,rows", _A_CASES, ids=[c[0] for c in _A_CASES])
def test_real_pressure_fixture_projects_to_pass(name, rows):
    pressure = gate.run_pressure_fixture(name, **_A_KW)
    for row in rows:
        result = gate.evaluate_row(row, _project_pressure(row, pressure), attachments={})
        assert result["status"] == "PASS", (name, row, result)


# ───────── 16. X2 B단위: 실제 churn 원본 → 행별 투영 → 판정 ─────────
# 소형 run_churn_fixture 결과를 predicate_unit 으로 옮겨 B 행이 PASS 인지 본다. 관측값은 바꾸지 않는다.
# 투영이 하는 일은 둘뿐이다: (1) 소형 tail 표기 tail_before/tail_after → full 표기 tail_80640/tail_80641
# (full 에선 게이트가 직접 full 표기를 낸다), (2) test_scale 규모 표시. 출처: codex_x2_b_report.md(8b592fcc).
# B11 은 unit 모드가 경계쌍 1개를 요구하므로 범주마다 따로 투영해 세 범주(finished·linked·unbound) 모두 본다
# — full(36쌍)에선 판정기가 전 범주를 한 번에 다뤄야 하므로, 한 범주만 보면 결함을 가린다.

_B_Q = ("churn_most_finished_short_ascii", dict(limit=4096, churn_minutes=25, churn_stride_minutes=1,
                                                 fixture_detail_divisor=256))
_B_D = ("churn_burst_ascii128", dict(limit=128, churn_minutes=25, churn_stride_minutes=60,
                                      fixture_detail_divisor=256))
_B_S = ("churn_most_finished_short_ascii", dict(limit=128, churn_minutes=25, churn_stride_minutes=60,
                                                 fixture_detail_divisor=256))
_B_SCALE = {
    "B01": {"normal_calls": 201, "minutes": 25, "sources": ["investing", "bs", "citi"]},
    "B02": {"cycle_length": 20, "finished": 16, "unbound": 2, "cycles": 10},
    "B03": {"burst_details": 8, "normal_calls": 201},
    "B04": {"normal_calls": 201, "hours": 25, "days": 1},
    "B13": {"required_budget_points": ["hour", "day", "tail_before", "tail_after"]},
}
_B_TAIL_NAMES = {"tail_before": "tail_80640", "tail_after": "tail_80641"}
_B_CACHE: dict = {}


def _b_fixture(spec):
    name, kwargs = spec
    key = (name, tuple(sorted(kwargs.items())))
    if key not in _B_CACHE:
        _B_CACHE[key] = gate.run_churn_fixture(name, **kwargs)
    return copy.deepcopy(_B_CACHE[key])


def _project_churn(row, fixture, *, b11_category=None):
    f = copy.deepcopy(fixture)
    scale = copy.deepcopy(_B_SCALE.get(row, {}))
    if row == "B02":
        scale["churn_names"] = [f["name"]]
    if row in ("B01", "B03", "B04"):
        for cp in f["checkpoints"]:
            cp["kind"] = _B_TAIL_NAMES.get(cp["kind"], cp["kind"])
    if row == "B11":
        f["checkpoints"] = [cp for cp in f["checkpoints"]
                            if cp["kind"] in ("probe_prune_before", "probe_prune_at")
                            and cp["identity_samples"][0]["category"] == b11_category]
    return {"mode": "predicate_unit", "churn_fixtures": [f], "test_scale": scale}


_B_CASES = [(_B_Q, ("B01",)), (_B_D, ("B03",)), (_B_S, ("B02", "B04", "B05", "B06", "B07", "B13"))]


@pytest.mark.parametrize("spec,rows", _B_CASES, ids=[f"{c[0][0]}-{'-'.join(c[1])}" for c in _B_CASES])
def test_real_churn_fixture_projects_to_pass(spec, rows):
    fixture = _b_fixture(spec)
    for row in rows:
        result = gate.evaluate_row(row, _project_churn(row, fixture), attachments={})
        assert result["status"] == "PASS", (spec[0], row, result)


@pytest.mark.parametrize("category", ["finished", "linked", "unbound"])
def test_real_churn_b11_every_category_passes(category):
    fixture = _b_fixture(_B_S)
    projected = _project_churn("B11", fixture, b11_category=category)
    assert len(projected["churn_fixtures"][0]["checkpoints"]) == 2, category
    result = gate.evaluate_row("B11", projected, attachments={})
    assert result["status"] == "PASS", (category, result)


def test_real_churn_raw_bool_fields_do_not_block_rows():
    # 게이트의 진짜 bool 필드(insufficient_evidence·expected_in_recent·observed_in_recent)가
    # 정수 증거 거부 검사에 걸려 전 행을 UNVERIFIED 로 만들지 않는다.
    fixture = _b_fixture(_B_S)
    result = gate.evaluate_row("B05", _project_churn("B05", fixture), attachments={})
    assert "JSON bool cannot stand" not in (result["reason"] or ""), result


def _b_attachments(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


@pytest.mark.parametrize("row", ["B12", "B15"])
def test_real_churn_streamed_evidence_passes_and_fails_closed(row, tmp_path):
    # 40분 초과 원본은 상세 증거를 gzip JSONL 부속으로 흘려 쓴다(full 과 같은 경로). 부속이 온전하면 PASS,
    # 없거나 한 바이트라도 바뀌면 UNVERIFIED 여야 한다 — 부속 경로가 판정을 조용히 약화하지 않는다.
    fixture = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, churn_minutes=41,
                                     churn_stride_minutes=60, fixture_detail_divisor=256,
                                     attachment_dir=str(tmp_path))
    attachments = _b_attachments(tmp_path)
    assert attachments, "streamed evidence produced no attachment"
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "test_scale": {"hours": 41, "pre_prune_hours": 3}}
    assert gate.evaluate_row(row, report, attachments=attachments)["status"] == "PASS"
    assert gate.evaluate_row(row, report, attachments={})["status"] == "UNVERIFIED"
    tampered = {k: v[:-1] + bytes([v[-1] ^ 1]) for k, v in attachments.items()}
    assert gate.evaluate_row(row, report, attachments=tampered)["status"] == "UNVERIFIED"


@pytest.mark.parametrize("index", range(5))
def test_real_fault_scenario_without_observed_coverage_is_unverified(index):
    # Codex 커밋 검토 REJECT(codex_x2b_commit_verdict1.md): B20 이 observed_coverage 가 있을 때만 실제
    # coverage 를 봐서, 필드를 지워도 PASS 했다. 실제 fault 원본 다섯 개 각각에서 필드를 지우면 UNVERIFIED.
    scenarios = gate.run_churn_fault_scenarios()
    assert len(scenarios) == 5
    doc = copy.deepcopy(_ex("B20"))
    doc["scenarios"] = copy.deepcopy(scenarios)
    doc["test_scale"] = {"fault_kinds": [s["fault_evidence"]["fault_kind"] for s in scenarios]}
    assert gate.evaluate_row("B20", doc, attachments={})["status"] == "PASS"
    del doc["scenarios"][index]["fault_evidence"]["observed_coverage"]
    result = gate.evaluate_row("B20", doc, attachments={})
    assert result["status"] == "UNVERIFIED", (index, result)


@pytest.mark.parametrize("which", ["first_hour_after_close", "last"])
def test_real_churn_b14_close_through_cannot_vanish_after_close(which):
    # Codex 커밋 재검토 REJECT(codex_x2b_commit_verdict2.md): B14 가 이전·현재 모두 close_through 가 있을
    # 때만 단조성을 봐서, 닫힌 뒤 체크포인트의 close_through·cumulative_end 를 None 으로 바꿔도 PASS 했다.
    # None 은 첫 닫힘 전에만 정상이다.
    fixture = _b_fixture(_B_S)
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture], "test_scale": {}}
    assert gate.evaluate_row("B14", report, attachments={})["status"] == "PASS"
    cps = report["churn_fixtures"][0]["checkpoints"]
    first = next(i for i, cp in enumerate(cps) if cp.get("close_through") is not None)
    assert first + 1 < len(cps)
    # probe_close_at 은 다른 검사(close_through >= bucket_end)가 이미 잡으므로 hour 체크포인트를 고른다.
    hour = next(i for i in range(first + 1, len(cps)) if cps[i]["kind"] == "hour")
    target = cps[hour] if which == "first_hour_after_close" else cps[-1]
    assert target["kind"] != "probe_close_at", which
    target["close_through"] = None
    target["cumulative_end"] = None
    assert gate.evaluate_row("B14", report, attachments={})["status"] != "PASS"


@pytest.mark.parametrize("kind,pick", [("probe_prune_at", 0), ("probe_prune_at", -1), ("tail_after", 0)])
def test_real_churn_b15_identity_index_audit_cannot_vanish(kind, pick):
    # Codex 커밋 재검토 REJECT(codex_x2b_commit_verdict3.md): 게이트 원본에서 체크포인트별 색인 감사가 없으면
    # 빈 목록으로 처리해, prune 체크포인트 하나의 감사를 지워도 B15 가 PASS 했다. 게이트는 probe_prune_at 과
    # 두 번째 tail 에 감사를 늘 붙이므로 그 자리의 소실은 UNVERIFIED.
    fixture = _b_fixture(_B_S)
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture], "test_scale": {}}
    assert gate.evaluate_row("B15", report, attachments={})["status"] == "PASS"
    cps = report["churn_fixtures"][0]["checkpoints"]
    index = [i for i, cp in enumerate(cps) if cp["kind"] == kind][pick]
    del cps[index]["identity_index_audit"]
    assert gate.evaluate_row("B15", report, attachments={})["status"] == "UNVERIFIED"


# Codex 커밋 재검토 REJECT(codex_x2b_commit_verdict4.md): 보조 호출 증거(register 의 auxiliary,
# 원본의 auxiliary_registered)가 없으면 기본값 False·0 으로 처리해 B01·B02·B04·B10·B21 이 PASS 했다.
# 게이트는 두 필드를 늘 낸다 — 게이트 원본에서의 누락·불일치는 PASS 불가.

def _b_row_report(row, spec):
    report = _project_churn(row, _b_fixture(spec))
    if row == "B10":
        report["test_scale"] = {"hours": 25, "pre_prune_hours": 3}
    if row == "B21":
        for cp in report["churn_fixtures"][0]["checkpoints"]:
            cp["kind"] = _B_TAIL_NAMES.get(cp["kind"], cp["kind"])
    return report


def _b_normal_registers(report):
    trace = report["churn_fixtures"][0]["fixture_provenance"]["api_trace"]
    return [t for t in trace if t.get("action") == "register" and t.get("auxiliary") is False]


_B_AUX_CASES = [
    ("B01", _B_Q, "drop_fixture_count"), ("B01", _B_Q, "drop_first_normal"),
    ("B02", _B_S, "drop_first_normal"), ("B02", _B_S, "flip_middle_normal"),
    ("B04", _B_S, "drop_first_normal"), ("B10", _B_S, "drop_first_normal"),
    ("B21", _B_S, "drop_fixture_count"), ("B21", _B_D, "drop_fixture_count"),
]


@pytest.mark.parametrize("row,spec,mutation", _B_AUX_CASES,
                         ids=[f"{r}-{s[0]}-{m}" for r, s, m in _B_AUX_CASES])
def test_real_churn_auxiliary_evidence_cannot_default(row, spec, mutation):
    report = _b_row_report(row, spec)
    assert gate.evaluate_row(row, report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    normal = _b_normal_registers(report)
    assert len(normal) > 2
    if mutation == "drop_fixture_count":
        del fixture["auxiliary_registered"]
    elif mutation == "drop_first_normal":
        del normal[0]["auxiliary"]
    elif mutation == "flip_middle_normal":
        normal[len(normal) // 2]["auxiliary"] = True
    assert gate.evaluate_row(row, report, attachments={})["status"] != "PASS", (row, mutation)


# Codex 커밋 재검토 REJECT(codex_x2b_commit_verdict5.md) 1·2번: 보조 호출 증거의 **값**이 trace 와 어긋나도 PASS.
# (3번 — predicate_unit 에서 name 을 지워 축약 예시로 오인시키는 우회 — 는 최종 수락이 full 만이라 범위 밖으로
#  Codex 와 합의 대상. 여기서는 잠그지 않는다.)

@pytest.mark.parametrize("row", ["B02", "B04", "B10", "B21"])
def test_real_churn_auxiliary_registered_must_match_trace(row):
    report = _b_row_report(row, _B_S)
    fixture = report["churn_fixtures"][0]
    assert fixture["auxiliary_registered"] == 0
    assert gate.evaluate_row(row, report, attachments={})["status"] == "PASS"
    fixture["auxiliary_registered"] = 1
    assert gate.evaluate_row(row, report, attachments={})["status"] != "PASS", row


@pytest.mark.parametrize("flip", ["burst_to_normal", "normal_to_burst"])
def test_real_churn_b03_auxiliary_flip_is_not_pass(flip):
    report = _project_churn("B03", _b_fixture(_B_D))
    assert gate.evaluate_row("B03", report, attachments={})["status"] == "PASS"
    trace = report["churn_fixtures"][0]["fixture_provenance"]["api_trace"]
    registers = [t for t in trace if t.get("action") == "register"]
    burst = [t for t in registers if t["auxiliary"] is True]
    normal = [t for t in registers if t["auxiliary"] is False]
    assert burst and normal
    if flip == "burst_to_normal":
        burst[len(burst) // 2]["auxiliary"] = False
    else:
        normal[len(normal) // 2]["auxiliary"] = True
    assert gate.evaluate_row("B03", report, attachments={})["status"] != "PASS", flip


@pytest.mark.parametrize("field", ["rows", "last_seq", "first_seq"])
def test_real_churn_streamed_ref_metadata_rechecked_per_reference(field, tmp_path):
    # Codex 커밋 재검토 REJECT(codex_x2b_commit_verdict6.md): 부속 뷰를 (path, sha256) 으로만 캐시해,
    # 첫 체크포인트가 정상 참조를 읽은 뒤 두 번째 체크포인트의 같은 부속 참조 메타데이터를 바꿔도 통과했다.
    fixture = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, churn_minutes=41,
                                     churn_stride_minutes=60, fixture_detail_divisor=256,
                                     attachment_dir=str(tmp_path))
    attachments = _b_attachments(tmp_path)
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "test_scale": {"hours": 41, "pre_prune_hours": 3}}
    assert gate.evaluate_row("B15", report, attachments=attachments)["status"] == "PASS"
    refs = [cp["seq_evidence_ref"] for cp in fixture["checkpoints"] if "seq_evidence_ref" in cp]
    assert len(refs) >= 2 and refs[0]["path"] == refs[1]["path"]
    second = [cp for cp in fixture["checkpoints"] if "seq_evidence_ref" in cp][1]
    second["seq_evidence_ref"] = dict(second["seq_evidence_ref"])
    second["seq_evidence_ref"][field] = second["seq_evidence_ref"][field] + 1
    assert gate.evaluate_row("B15", report, attachments=attachments)["status"] == "UNVERIFIED", field


# Codex 커밋 재검토 REJECT(codex_x2b_commit_verdict7.md): B14 게이트 원본 분기가 기존 대조(단조성·닫힘 입력·watermark)를
# 건너뛰어, 아래 세 변조가 모두 PASS 했다. 게이트 원본 분기에서도 같은 의미의 대조가 있어야 한다.

@pytest.mark.parametrize("mutation", ["last_received_regress", "close_through_future", "prune_watermark_zero"])
def test_real_churn_b14_gate_branch_keeps_old_contracts(mutation):
    fixture = _b_fixture(_B_S)
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture], "test_scale": {}}
    assert gate.evaluate_row("B14", report, attachments={})["status"] == "PASS"
    cps = report["churn_fixtures"][0]["checkpoints"]
    if mutation == "last_received_regress":
        target = cps[len(cps) // 2]
        target["last_received_at"] = 0
        target["last_received_mono"] = 0
    elif mutation == "close_through_future":
        last = cps[-1]
        assert last["close_through"] is not None
        future = max(last["received_at"], last["received_mono"]) + 10 ** 9
        last["close_through"] = future
        last["cumulative_end"] = future
    else:
        prune_at = [cp for cp in cps if cp["kind"] == "probe_prune_at"]
        assert prune_at and prune_at[-1]["prune_observed_through"] not in (None, 0)
        prune_at[-1]["prune_observed_through"] = 0
    assert gate.evaluate_row("B14", report, attachments={})["status"] != "PASS", mutation
