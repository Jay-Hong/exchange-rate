"""D7 5a-4c 계약 — 측정 게이트의 압력·7일 churn·high-water capacity·수락 verdict (작은 경로로 CI 에서 확인).

세부 계약: `design/d7-aggregation/slice5a4c_contract_r2.md` (sha256 f184d631c3f7ebccfbd3bc1892791fdb029df0de2a278e145f3cc302e5d3fd28,
Codex 작성, Claude 검토 R1~R4 반영·합의). 이름·모양: `slice5a4c_interface_r2.md`
(sha256 188b07e646b0649604952562d03e10ac6b376953a3c3295affbc22626a3c2cd3 — Codex 가 r1 을 rebuild_events·후보 source/job_id 로 정정한 판).
+ 부록 `slice5a4c_interface_r2_addendum_control.md`(control_existing_key, Codex 문구). 기본 full 규모는 여기서 재지 않는다 —
같은 로직의 소형 경로가 원인·무삽입·latch·분해·모드 강등을 계약대로 판정하는지만 잠근다.
계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

from app import d7_round_ledger as lg

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "d7_ledger_measure_gate.py"

PRESSURE = [f"pressure_{t}_{k}" for t in ("p0", "p1", "p2", "p3") for k in ("short_ascii", "ascii128", "unicode128")]
AUX = ["pressure_unique_job_keys_byte_stop", "pressure_slot_first_small_limit"]
CHURN = [
    "churn_most_finished_short_ascii", "churn_most_finished_ascii128", "churn_most_finished_unicode128",
    "churn_most_unfinished_short_ascii", "churn_most_unfinished_ascii128", "churn_most_unfinished_unicode128",
    "churn_init_failed_most_finished_unicode128", "churn_init_failed_most_unfinished_unicode128",
    "churn_burst_ascii128", "churn_burst_unicode128",
]
F4 = lg.RoundLedger("E1", aggregation_started_at=0).budget_state()["F_4"]      # 공개 관측으로만
B_DEFAULT = 62_914_560
SMALL_CHURN = dict(churn_minutes=25, churn_stride_minutes=60, fixture_detail_divisor=256)
CHECKPOINT_KEYS = {"F_4", "Q_4", "D", "A", "R", "T", "R_T", "AR", "TR", "E", "B", "G", "B_minus_E", "N_total", "N_live",
                   "N_tomb", "N_res", "capacity", "rebuild_count", "unknown_types"}
FIRST_REJECTION_KEYS = {"candidate_id", "candidate_source", "candidate_job_id", "candidate_seq", "received_at", "received_mono", "classification", "cause",
                        "simultaneous_causes", "diagnostics_codes", "admission_stopped_at", "no_insertion"}
POST_LATCH_KEYS = {"attempted", "all_rejected", "first_stop_time_unchanged", "resumed_after_release",
                   "existing_id_transitions"}
PRESSURE_KEYS = {"name", "status", "id_kind", "track", "fixture_limit", "source_registered", "job_key_counts",
                 "requested_detail_target", "effective_detail_target",
                 "retained_details", "state_counts", "id_utf8_bytes", "id_getsizeof_bytes", "N_last_accepted",
                 "first_rejection", "before", "after", "post_latch", "sample_failures", "reason"}
CHURN_KEYS = {"name", "status", "id_kind", "state_track", "job_key_counts", "id_utf8_bytes", "id_getsizeof_bytes",
              "minute_batches", "stride_minutes", "scheduled_target",
              "scheduled_registered", "auxiliary_registered", "N_total_at_tail", "source_registered",
              "requested_detail_target", "effective_detail_target", "max_retained_details_observed",
              "tail_classification", "checkpoints", "classification_counts", "first_failure", "reason"}
CHURN_CP_KEYS = {"kind", "minute_index", "received_at", "received_mono", "scheduled_registered", "auxiliary_registered",
                 "N_total", "N_live", "N_tomb", "N_res", "retained_details", "budget", "last_received_at",
                 "last_received_mono", "cumulative_end", "cursor_after_seq", "cursor_next_seq", "cohort_exact_from",
                 "frozen_through", "coverage_complete", "uncertain_sources", "admission_stopped",
                 "admission_stopped_at", "rebuild_count", "capacity_charged_bytes"}
NEW_TOP_KEYS = {"pressure_fixtures", "churn_fixtures", "capacity_proof", "rebuild_events", "131072_slots",
                "unreachable_rows", "verdicts", "verdict_reasons", "calibration", "eta"}


def _load_gate():
    spec = importlib.util.spec_from_file_location("d7_gate_5a4c_under_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


# ───────── I1 서명·모드 강등·인자 검증 ─────────

def test_run_gate_signature_defaults():
    import inspect
    params = inspect.signature(gate.run_gate).parameters
    expected = {"limit": 131_072, "samples": 1000, "warmup": 100, "quick": False, "budget_seconds": 14400,
                "rows": None, "max_resident_bytes": None, "pressure_names": None, "churn_names": None,
                "churn_minutes": 10_080, "churn_stride_minutes": 1, "fixture_detail_divisor": 1,
                "calibration": False, "budget_agreement_ref": None}
    for name, default in expected.items():
        assert params[name].default == default, name
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


@pytest.mark.parametrize("kwargs", [
    dict(pressure_names=[]), dict(churn_names=[]), dict(pressure_names=["pressure_p0_short_ascii"] * 2),
    dict(pressure_names=["pressure_p9_short_ascii"]), dict(churn_names=["churn_nope"]),
    dict(quick=True, calibration=True), dict(calibration=True),                        # 축소·선택 없는 calibration
    dict(churn_minutes=0), dict(churn_stride_minutes=0), dict(fixture_detail_divisor=0),
    dict(max_resident_bytes=F4 - 1), dict(max_resident_bytes=B_DEFAULT + 1),
    dict(budget_seconds=14_401),                                                         # full 인데 합의 참조 없음
    dict(budget_seconds=14_401, budget_agreement_ref=""),
])
def test_invalid_combinations_raise_before_running(kwargs):
    with pytest.raises(ValueError):
        gate.run_gate(**kwargs)


def test_selected_pressure_run_is_full_small_and_never_accepts():
    r = gate.run_gate(limit=128, samples=3, warmup=1, pressure_names=["pressure_p0_short_ascii"],
                      max_resident_bytes=F4 + 1_000_000, **SMALL_CHURN)
    assert r["mode"] == "full_small"
    assert NEW_TOP_KEYS <= set(r)
    assert [p["name"] for p in r["pressure_fixtures"]] == ["pressure_p0_short_ascii"]
    assert r["churn_fixtures"] == []                                                     # 미선택 범주는 빈 배열
    names = {row["name"] for row in r["scenarios"]}
    assert "pressure_p0_short_ascii" in names and not any(n.startswith("churn_") for n in names)
    assert r["overall"] != "PASS" and r["partial_acceptance"]["eligible"] is False
    assert set(r["verdicts"]) == {"slice5a2_partial", "slice5a3", "slice5a4"}
    assert set(r["verdict_reasons"]) == set(r["verdicts"])
    assert "PASS" not in r["verdicts"].values()
    assert r["limits"]["max_records"] == 128 and r["limits"]["contract_max_records"] == 131072
    assert r["limits"]["max_resident_bytes"] == F4 + 1_000_000
    assert (r["limits"]["churn_minutes"], r["limits"]["churn_stride_minutes"], r["limits"]["fixture_detail_divisor"]) \
        == (25, 60, 256)
    assert r["calibration"]["status"] == "not_run" and r["calibration"]["observations"] == []
    assert r["eta"]["basis"] == "none" and r["eta"]["decision"] == "unknown" and r["eta"]["seconds"] is None


# ───────── C′1 압력: 원인·무삽입·latch ─────────

def _check_pressure_shape(p):
    assert set(p) == PRESSURE_KEYS
    assert set(p["first_rejection"]) == FIRST_REJECTION_KEYS
    assert set(p["post_latch"]) == POST_LATCH_KEYS
    for key in ("before", "after"):
        assert set(p[key]) == CHECKPOINT_KEYS, key
        assert set(p[key]["capacity"]) == {"charged_bytes", "rebuild_old_bytes", "rebuild_new_bytes"}


def test_pressure_byte_first_under_small_budget():
    p = gate.run_pressure_fixture("pressure_p0_short_ascii", limit=4096, max_resident_bytes=F4 + 1_000_000)
    _check_pressure_shape(p)
    fr = p["first_rejection"]
    assert fr["classification"] == "admission_stopped" and fr["cause"] == "byte" and fr["no_insertion"] is True
    assert 0 < p["N_last_accepted"] < 4096
    assert fr["candidate_seq"] == p["N_last_accepted"] + 1
    b = p["before"]
    assert b["N_total"] == b["N_res"] == p["N_last_accepted"] and b["N_tomb"] == 0             # 퇴출 자격 없는 압력
    assert b["E"] == b["F_4"] + b["Q_4"] + b["D"] + b["AR"] + b["TR"] <= b["B"] == F4 + 1_000_000
    assert b["G"] is not None and b["G"] <= b["E"]
    pl = p["post_latch"]
    assert pl["all_rejected"] is True and pl["resumed_after_release"] is False
    assert pl["first_stop_time_unchanged"] is True and pl["attempted"] >= 1
    # 계약 r2 C′1: latch 뒤 기존 ID 의 필수 전이는 byte 거절로 숨지 않는다(부록 slice5a4c_interface_r2_addendum_full.md)
    transitions = pl["existing_id_transitions"]
    assert set(transitions) == {"link_round", "report_init_failed", "wrapper_exited", "next_entry", "overdue",
                                "finish", "late_finish", "close", "post_close_duplicate"}
    assert all(type(v) is str and v != "admission_stopped" for v in transitions.values()), transitions
    assert transitions["finish"] == "finalized" and transitions["post_close_duplicate"] == "post_close_duplicate"
    assert p["status"] == "PASS"


def test_pressure_slot_first_small_limit_aux():
    p = gate.run_pressure_fixture("pressure_slot_first_small_limit", limit=8)
    _check_pressure_shape(p)
    fr = p["first_rejection"]
    assert fr["cause"] == "slot" and fr["no_insertion"] is True
    assert p["fixture_limit"] == 8 and p["N_last_accepted"] == 8 and fr["candidate_seq"] == 9
    assert p["before"]["B"] == B_DEFAULT                                                     # 작은 B 로 slot 을 유도하지 않는다
    assert p["status"] == "PASS"


def test_unique_job_keys_stop_by_bytes_not_slots():
    p = gate.run_pressure_fixture("pressure_unique_job_keys_byte_stop", limit=128, max_resident_bytes=F4 + 300_000)
    fr = p["first_rejection"]
    assert fr["cause"] == "byte" and fr["no_insertion"] is True
    assert p["before"]["N_res"] < 128                                                       # prune 으로 슬롯 거절과 구분
    keys = {(k["source"], k["job_id"]) for k in p["job_key_counts"]}
    assert len(keys) > 12 and all(k["registrations"] >= 1 for k in p["job_key_counts"])    # 고정 12-key 가정 위반 입력
    assert (fr["candidate_source"], fr["candidate_job_id"]) not in keys                      # 거절 원인은 새 key 요금
    assert set(p) == PRESSURE_KEYS | {"control_existing_key"}
    ctl = p["control_existing_key"]                                                          # 같은 상태 재현본의 기존 key 후보
    assert ctl["classification"] == "registered" and (ctl["source"], ctl["job_id"]) in keys
    assert ctl["source"] == fr["candidate_source"]
    assert sys.getsizeof(ctl["job_id"]) == sys.getsizeof(fr["candidate_job_id"])            # 새 key 요금만 다르게
    assert p["status"] == "PASS"


@pytest.mark.parametrize("name", ["pressure_p2_unicode128", "pressure_p3_ascii128"])
def test_pressure_detail_tracks_scale_targets_and_report_actual(name):
    p = gate.run_pressure_fixture(name, limit=128, max_resident_bytes=F4 + 1_000_000, fixture_detail_divisor=256)
    full = 512 if "_p2_" in name else 2048
    assert p["requested_detail_target"] == full
    assert p["effective_detail_target"] == min(math.ceil(full / 256), 2048, 127)
    assert p["retained_details"] == p["effective_detail_target"]
    assert p["id_utf8_bytes"] <= 128 and p["id_getsizeof_bytes"] > 0
    assert sum(p["state_counts"].values()) == p["N_last_accepted"]


def test_unknown_pressure_name_raises():
    with pytest.raises(ValueError):
        gate.run_pressure_fixture("pressure_p4_short_ascii", limit=8)


# ───────── C′2 churn: 소형 동형 경로 ─────────

@pytest.mark.parametrize("name", ["churn_most_finished_short_ascii", "churn_most_unfinished_unicode128",
                                  "churn_init_failed_most_unfinished_unicode128"])
def test_small_churn_registers_every_scheduled_call(name):
    c = gate.run_churn_fixture(name, limit=128, **SMALL_CHURN)
    assert set(c) == CHURN_KEYS
    assert c["minute_batches"] == 25 and c["stride_minutes"] == 60
    assert c["scheduled_target"] == c["scheduled_registered"] == 8 * 25 + 1
    assert c["auxiliary_registered"] == 0
    assert c["tail_classification"] == "registered" and c["first_failure"] is None
    assert sum(c["source_registered"].values()) == c["scheduled_registered"]
    assert c["source_registered"]["investing"] == 6 * 25 + 1
    assert c["source_registered"]["bs"] == c["source_registered"]["citi"] == 25
    kinds = [cp["kind"] for cp in c["checkpoints"]]
    assert "tail_before" in kinds and "tail_after" in kinds
    assert "tail_80640" not in kinds and "tail_80641" not in kinds                           # full 전용 표기
    assert kinds.count("hour") >= 24 and kinds.count("day") >= 1
    for cp in c["checkpoints"]:
        assert set(cp) == CHURN_CP_KEYS
        assert cp["admission_stopped"] is False and cp["rebuild_count"] == 0
        assert cp["N_res"] == cp["N_live"] + cp["N_tomb"] <= 128
        assert cp["budget"]["E"] <= cp["budget"]["B"]
    before = next(cp for cp in c["checkpoints"] if cp["kind"] == "tail_before")
    after = next(cp for cp in c["checkpoints"] if cp["kind"] == "tail_after")
    assert before["scheduled_registered"] == 8 * 25 and after["scheduled_registered"] == 8 * 25 + 1
    assert c["N_total_at_tail"] == after["N_total"] == c["scheduled_registered"] + c["auxiliary_registered"]
    assert after["cursor_after_seq"] <= after["N_total"]
    assert after["cursor_next_seq"] is None or after["cursor_next_seq"] > after["cursor_after_seq"]
    assert after["last_received_at"] == after["received_at"] and after["last_received_mono"] == after["received_mono"]
    assert {(k["source"], k["job_id"]) for k in c["job_key_counts"]} and len(c["job_key_counts"]) <= 12   # 고정 job key
    assert c["status"] == "PASS"


def test_small_churn_actually_retires_and_prunes():
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    last = c["checkpoints"][-1]
    assert last["N_total"] > last["N_res"]                                                   # 누적 > 상주
    assert last["frozen_through"] is not None and last["cohort_exact_from"] > 0
    assert any(cp["N_tomb"] > 0 for cp in c["checkpoints"])


@pytest.mark.parametrize("name", ["churn_burst_ascii128", "churn_burst_unicode128"])
def test_small_burst_overlay_separates_auxiliary_calls(name):
    c = gate.run_churn_fixture(name, limit=128, **SMALL_CHURN)
    target = math.ceil(2048 / 256)
    assert c["requested_detail_target"] == 2048 and c["effective_detail_target"] == target
    assert c["max_retained_details_observed"] >= target
    assert c["auxiliary_registered"] == target + 1                                           # burst + probe
    assert c["scheduled_registered"] == 8 * 25 + 1
    assert c["N_total_at_tail"] == c["scheduled_registered"] + c["auxiliary_registered"] == 8 * 25 + 1 + target + 1
    kinds = [cp["kind"] for cp in c["checkpoints"]]
    opened, released = kinds.index("detail_open"), kinds.index("detail_released")
    assert opened < released
    assert c["checkpoints"][opened]["retained_details"] == target
    assert c["checkpoints"][released]["retained_details"] == 0
    assert c["checkpoints"][released]["scheduled_registered"] == 0                           # 정상 일정 전 해제
    assert c["status"] == "PASS"


def test_normal_churn_never_claims_2048_details():
    c = gate.run_churn_fixture("churn_most_finished_ascii128", limit=128, **SMALL_CHURN)
    assert c["requested_detail_target"] == 0 and c["effective_detail_target"] == 0


# ───────── C′3 capacity·rebuild ─────────

def test_small_run_capacity_proof_and_rebuild_na():
    r = gate.run_gate(limit=128, samples=3, warmup=1, churn_names=["churn_most_unfinished_short_ascii"],
                      **SMALL_CHURN)
    cp = r["capacity_proof"]
    assert set(cp) >= {"status", "formula", "job_key_limit", "H_res", "H_job", "Q_cap_bytes", "Q_obs_bytes",
                       "Q_actual_max_bytes", "checkpoints", "unknown_ownership", "reason"}
    assert cp["job_key_limit"] == 12
    assert cp["formula"] == "_budget_q(131072)+512*131072+1536*12"
    assert cp["Q_obs_bytes"] is None or cp["Q_cap_bytes"] is None or cp["Q_obs_bytes"] <= cp["Q_cap_bytes"]
    for point in cp["checkpoints"]:
        if point["capacity_covered"] is not None:
            assert point["capacity_covered"] == (point["Q_actual_bytes"] <= point["Q_4_bytes"])
        for backing in point["container_backings"]:
            assert set(backing) == {"name", "object_id", "getsizeof_bytes", "header_accounted_elsewhere_bytes",
                                    "Q_attributed_bytes", "charged_limit_bytes", "includes_deleted_dummy"}
    assert cp["status"] == "PASS"
    assert None not in (cp["H_res"], cp["H_job"], cp["Q_cap_bytes"], cp["Q_obs_bytes"], cp["Q_actual_max_bytes"])
    assert cp["H_job"] <= 12 and cp["Q_obs_bytes"] <= cp["Q_cap_bytes"]
    assert cp["Q_cap_bytes"] == lg._budget_q(131072) + 512 * 131072 + 1536 * 12          # 구조식의 계산값 자체
    assert cp["H_res"] == max(point["N_res"] for point in cp["checkpoints"])
    assert cp["Q_obs_bytes"] == max(point["Q_4_bytes"] for point in cp["checkpoints"])
    assert cp["Q_actual_max_bytes"] == max(point["Q_actual_bytes"] for point in cp["checkpoints"])
    assert cp["checkpoints"] and all(point["capacity_covered"] is True for point in cp["checkpoints"])
    assert all(point["container_backings"] for point in cp["checkpoints"])
    for point in cp["checkpoints"]:
        assert point["G_bytes"] is not None and point["G_bytes"] <= point["E_bytes"] <= point["B_bytes"]
    assert r["rebuild_events"] == []                                                         # 현 구현은 rebuild 없음


# ───────── 판정 함수: overall·부분 수락·verdict ─────────

def _row(name, status, kind=None, reason=None, applicable=True):
    row = {"name": name, "status": status, "visit_gate": "PASS", "temporary_gate": "PASS", "time_gate": "PASS"}
    if kind is not None:
        row.update(kind=kind, reason=reason, partial_acceptance_applicable=applicable)
    return row


def test_overall_excludes_only_exact_rebuild_na():
    base = [_row("aggregation_empty", "PASS"), _row("pressure_p0_short_ascii", "PASS", "pressure", applicable=False)]
    ok = base + [_row("rebuild_double_backing", "N/A", "rebuild", "N/A (no rebuild in this implementation)", False)]
    assert gate.overall_status(ok, adapter_status="PASS", mode="full") == "PASS"
    wrong = base + [_row("rebuild_double_backing", "N/A", "rebuild", "not_measured", False)]
    assert gate.overall_status(wrong, adapter_status="PASS", mode="full") != "PASS"


def test_new_kind_fail_makes_overall_fail_but_not_partial_eligibility():
    rows = [_row("aggregation_empty", "PASS"), _row("churn_most_finished_short_ascii", "FAIL", "churn", applicable=False),
            _row("rebuild_double_backing", "N/A", "rebuild", "N/A (no rebuild in this implementation)", False)]
    assert gate.overall_status(rows, adapter_status="PASS", mode="full") == "FAIL"
    pa = gate.partial_acceptance(rows, adapter_status="PASS", mode="full")
    assert pa["eligible"] is True and pa["failing_rows"] == []


def test_new_kind_unverified_blocks_overall_pass():
    rows = [_row("aggregation_empty", "PASS"), _row("capacity_highwater", "UNVERIFIED", "capacity", applicable=False)]
    assert gate.overall_status(rows, adapter_status="PASS", mode="full") == "UNVERIFIED"


@pytest.mark.parametrize("mode", ["full_small", "quick", "calibration"])
def test_non_full_modes_never_pass_even_if_all_rows_pass(mode):
    rows = [_row("aggregation_empty", "PASS"), _row("pressure_p0_short_ascii", "PASS", "pressure", applicable=False)]
    assert gate.overall_status(rows, adapter_status="PASS", mode=mode) != "PASS"
    assert gate.partial_acceptance(rows, adapter_status="PASS", mode=mode)["eligible"] is False


# ───────── C′4 기존 fill 행: 기본 한도에서 byte-stop N 으로 이관 ─────────

@pytest.mark.parametrize("B,limit,stop", [(F4 + 1_000_000, 4096, "byte"), (B_DEFAULT, 64, "limit")])
def test_legacy_fill_rows_use_actual_fixture_n(B, limit, stop):
    r = gate.run_gate(limit=limit, samples=2, warmup=0, rows=["aggregation_empty"], max_resident_bytes=B,
                      **SMALL_CHURN)
    assert r["complete"] is True and r["mode"] == "full_small"
    row = next(x for x in r["scenarios"] if x["name"] == "aggregation_empty")
    assert row["fixture_stop"] == stop
    assert row["fixture_n"] == (limit if stop == "limit" else row["fixture_n"])
    assert 0 < row["fixture_n"] <= limit and (stop == "limit" or row["fixture_n"] < limit)
    assert row["status"] in ("PASS", "FAIL")                                                  # fixture 오류로 UNVERIFIED 가 아니다


@pytest.mark.parametrize("name", ["register_accept", "finish_accept", "open_last_seq_cursor"])
def test_accept_rows_use_byte_headroom_fixture_under_byte_limit(name):
    """수락을 재는 행은 byte-stop N 원본이 아니라 필요한 수락 수만큼 바이트 여유를 남긴 대체 원본(부록 addendum_full)."""
    r = gate.run_gate(limit=4096, samples=2, warmup=0, rows=[name], max_resident_bytes=F4 + 1_000_000, **SMALL_CHURN)
    assert r["complete"] is True
    row = next(x for x in r["scenarios"] if x["name"] == name)
    assert row["fixture_stop"] == "byte_headroom" and row["headroom_bytes"] > 0
    assert 0 < row["fixture_n"] < 4096
    assert row["status"] in ("PASS", "FAIL"), row.get("reason")                               # 여유 원본이면 fixture 오류가 아니다


# ───────── 고장 난 ledger 를 주입해 게이트의 검출 능력을 잠근다(정상 ledger 에서는 드러나지 않는 판정) ─────────

def test_gate_detects_rejected_candidate_leaving_a_trace(monkeypatch):
    original = lg.RoundLedger.register

    def leaky(self, **kw):
        result = original(self, **kw)
        if result["classification"] == "admission_stopped":
            self._budget_d += 64                                                               # 거절 후보가 바이트 흔적을 남김
        return result

    monkeypatch.setattr(lg.RoundLedger, "register", leaky)
    p = gate.run_pressure_fixture("pressure_p0_short_ascii", limit=4096, max_resident_bytes=F4 + 1_000_000)
    assert p["first_rejection"]["no_insertion"] is False
    assert p["status"] != "PASS"


def test_gate_control_detects_ledger_that_charges_existing_keys_as_new(monkeypatch):
    original = lg.RoundLedger._q_charge

    def every_key_new(self, resident=None, jobs=None):
        if jobs is not None:                                                                  # 후보 검사: 기존 key 도 새 key 가격
            jobs = max(jobs, len(self._previous_job) + 1)
        return original(self, resident, jobs)

    monkeypatch.setattr(lg.RoundLedger, "_q_charge", every_key_new)
    p = gate.run_pressure_fixture("pressure_unique_job_keys_byte_stop", limit=128, max_resident_bytes=F4 + 300_000)
    assert p["control_existing_key"]["classification"] != "registered"
    assert p["status"] != "PASS"


def test_gate_capacity_detects_under_charged_backing(monkeypatch):
    original = lg.RoundLedger._q_charge
    monkeypatch.setattr(lg.RoundLedger, "_q_charge", lambda self, resident=None, jobs=None:
                        original(self, resident, jobs) // 8)                                 # 실제 backing 보다 적게 청구
    r = gate.run_gate(limit=128, samples=3, warmup=1, churn_names=["churn_most_unfinished_short_ascii"],
                      **SMALL_CHURN)
    cp = r["capacity_proof"]
    assert cp["status"] != "PASS"
    assert any(point["capacity_covered"] is False for point in cp["checkpoints"])


# ───────── Codex commit 검토 REVISE 대응: 기본 한도 fixture 와 ETA ─────────

@pytest.mark.parametrize("name", ["close_boundary_exact", "large_clock_jump", "mass_close_boundary",
                                  "reverse_cohort_empty", "reverse_cohort_narrow"])
def test_close_and_reverse_bases_do_not_crash_at_byte_stop(name):
    """_close_base·_reverse_base 도 byte-stop/여유 원본으로 전환돼야 한다(기본 B 에서 fill 은 약 15k 에서 멈춘다)."""
    r = gate.run_gate(limit=4096, samples=2, warmup=0, rows=[name], max_resident_bytes=F4 + 1_000_000, **SMALL_CHURN)
    assert r["complete"] is True
    row = next(x for x in r["scenarios"] if x["name"] == name)
    assert row["fixture_stop"] in ("byte", "byte_headroom") and 0 < row["fixture_n"] < 4096
    assert row["status"] in ("PASS", "FAIL"), row.get("reason")


def test_calibration_over_all_categories_produces_eta():
    r = gate.run_gate(calibration=True, limit=128, samples=2, warmup=0, **SMALL_CHURN)
    assert r["mode"] == "calibration" and r["overall"] != "PASS"
    cal, eta = r["calibration"], r["eta"]
    assert cal["status"] == "complete"
    assert {o["class_name"] for o in cal["observations"]} == {"legacy_repeat", "pressure", "churn_normal", "churn_burst",
                                                             "capacity", "resident", "adapter"}
    for o in cal["observations"]:
        assert o["completed_units"] > 0 and o["elapsed_seconds"] >= 0 and o["projected_full_units"] >= o["completed_units"]
    assert eta["basis"] == "calibration" and eta["seconds"] is not None and eta["seconds"] > 0
    assert eta["decision"] == ("agreement_required" if eta["seconds"] > 14_400 else "within_budget")
    assert eta["budget_seconds"] == 14400 and eta["assumptions"]
