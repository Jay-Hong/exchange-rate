"""D7 5a-4c 계약 — 측정 게이트의 압력·7일 churn·high-water capacity·수락 verdict (작은 경로로 CI 에서 확인).

세부 계약: `design/d7-aggregation/slice5a4c_contract_r2.md` (sha256 f184d631c3f7ebccfbd3bc1892791fdb029df0de2a278e145f3cc302e5d3fd28,
Codex 작성, Claude 검토 R1~R4 반영·합의). 이름·모양: `slice5a4c_interface_r2.md`
(sha256 188b07e646b0649604952562d03e10ac6b376953a3c3295affbc22626a3c2cd3 — Codex 가 r1 을 rebuild_events·후보 source/job_id 로 정정한 판).
+ 부록 `slice5a4c_interface_r2_addendum_control.md`(control_existing_key, Codex 문구). 기본 full 규모는 여기서 재지 않는다 —
같은 로직의 소형 경로가 원인·무삽입·latch·분해·모드 강등을 계약대로 판정하는지만 잠근다.
계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.
"""
from __future__ import annotations

import copy
import gc
import gzip
import hashlib
import importlib.util
import io
import json
import math
import sys
from pathlib import Path

import pytest

from app import d7_round_ledger as lg


@pytest.fixture(scope="module", autouse=True)
def _freeze_preexisting_heap():
    """게이트는 체크포인트마다 gc.collect() 를 부른다. 스위트 전체를 한 프로세스로 돌리면 앞선 시험이 남긴
    객체까지 매번 훑어 CI 가 20분을 넘겼다(2751c84 CI 취소, 같은 소형 churn 이 힙 300만 객체에서 0.27 s→19 s).
    이 모듈이 시작될 때 이미 있는 객체만 영구 세대로 옮기고 끝나면 되돌린다. 측정 대상 원장은 그 뒤에 생성된다."""
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


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
MIN = 60 * 10 ** 6                                                                       # 1분(µs)
F4 = lg.RoundLedger("E1", aggregation_started_at=0).budget_state()["F_4"]      # 공개 관측으로만
B_DEFAULT = 62_914_560
SMALL_CHURN = dict(churn_minutes=25, churn_stride_minutes=60, fixture_detail_divisor=256)


_DEFAULT_CHURN = []


def _default_churn():
    """패치 없는 기본 소형 churn 은 한 번만 돌리고 사본을 준다(CI 비용). 패치·hook 을 쓰는 시험은 직접 돌린다."""
    if not _DEFAULT_CHURN:
        _DEFAULT_CHURN.append(gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN))
    return copy.deepcopy(_DEFAULT_CHURN[0])
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
              "tail_classification", "checkpoints", "classification_counts", "first_failure", "reason",
              "highwater_checks", "evidence_gaps", "state_cycle", "fixture_provenance",
              "max_resident_observed", "tomb_due_ledger", "recent_input_ledger", "attachment_root"}
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
    # X2 A단위가 판정기용 원자료 필드를 더한다 — 기존 필드의 유실만 막고 추가는 허용한다.
    assert set(p) >= PRESSURE_KEYS
    assert set(p["first_rejection"]) >= FIRST_REJECTION_KEYS
    assert set(p["post_latch"]) >= POST_LATCH_KEYS
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
    assert set(p) >= PRESSURE_KEYS | {"control_existing_key"}
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
        assert CHURN_CP_KEYS <= set(cp)                                             # C′2 필드가 더해진다
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
    c = _default_churn()
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
    # 일곱 측정 유형을 모두 관측하는 최소 규모(CI 비용 — Codex 제안, 같은 단언 유지)
    r = gate.run_gate(calibration=True, limit=32, samples=1, warmup=0, max_resident_bytes=F4 + 1_000_000,
                      churn_minutes=1, churn_stride_minutes=60, fixture_detail_divisor=4096)
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


# ───────── full 1회차 중단 원인(09-27): 원본 구축이 등록마다 budget_state() 를 불러 O(N²) ─────────

def _count_budget_state(monkeypatch):
    calls = {"n": 0}
    original = lg.RoundLedger.budget_state

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(lg.RoundLedger, "budget_state", counting)
    return calls


def test_headroom_fixture_does_not_call_budget_state_per_registration(monkeypatch):
    calls = _count_budget_state(monkeypatch)
    ledger = gate.fill_with_headroom(4096, accepts=1)
    n = ledger._gate_fixture_n
    assert n > 1000                                                                          # 실제로 많이 쌓였다
    assert calls["n"] <= 16, calls["n"]                                                      # 건수에 비례하지 않는다
    b = ledger.budget_state()
    assert b["B"] - b["E"] == ledger._gate_headroom_bytes or ledger._gate_fixture_stop == "limit"


def test_pressure_fixture_does_not_call_budget_state_per_registration(monkeypatch):
    calls = _count_budget_state(monkeypatch)
    p = gate.run_pressure_fixture("pressure_p0_short_ascii", limit=4096, max_resident_bytes=F4 + 1_000_000)
    assert p["N_last_accepted"] > 200 and p["status"] == "PASS"
    assert calls["n"] <= 64, calls["n"]


# ───────── full 2회차 HOLD(09-27): churn 경계 체크포인트가 시작 기준이 아니고 day 하나가 빠짐 ─────────

@pytest.mark.parametrize("name", ["churn_most_finished_short_ascii", "churn_burst_unicode128"])
def test_churn_boundaries_are_exact_from_schedule_start(name):
    c = gate.run_churn_fixture(name, limit=128, **SMALL_CHURN)
    cps = c["checkpoints"]
    tail = next(cp for cp in cps if cp["kind"] == "tail_before")["received_at"]
    span = 25 * 60 * MIN
    start = tail - (24 * 60 * MIN + 52_500_000)
    hours = sorted(cp["received_at"] for cp in cps if cp["kind"] == "hour")
    days = sorted(cp["received_at"] for cp in cps if cp["kind"] == "day")
    assert hours == [start + k * 60 * MIN for k in range(1, 26)]                             # 시작 기준, tail 과 같은 경계 포함
    assert days == [start + 24 * 60 * MIN]
    after = next(cp for cp in cps if cp["kind"] == "tail_after")
    assert after["received_at"] == start + span
    assert gate.churn_checkpoint_gaps(cps, minute_batches=25, stride_minutes=60) == []
    assert c["status"] == "PASS"


def test_churn_checkpoint_gaps_detects_missing_and_shifted():
    c = _default_churn()
    cps = c["checkpoints"]
    without_day = [cp for cp in cps if cp["kind"] != "day"]
    assert gate.churn_checkpoint_gaps(without_day, minute_batches=25, stride_minutes=60)
    without_tail = [cp for cp in cps if cp["kind"] != "tail_after"]
    assert gate.churn_checkpoint_gaps(without_tail, minute_batches=25, stride_minutes=60)
    shifted = [dict(cp, received_at=cp["received_at"] + 1) if cp["kind"] == "hour" else cp for cp in cps]
    assert gate.churn_checkpoint_gaps(shifted, minute_batches=25, stride_minutes=60)
    one_hour_less = list(cps)
    one_hour_less.remove(next(cp for cp in cps if cp["kind"] == "hour"))
    assert gate.churn_checkpoint_gaps(one_hour_less, minute_batches=25, stride_minutes=60)
    assert gate.churn_checkpoint_gaps(cps, minute_batches=25, stride_minutes=60) == []
    duplicated_hour = cps + [next(cp for cp in cps if cp["kind"] == "hour")]
    gaps = gate.churn_checkpoint_gaps(duplicated_hour, minute_batches=25, stride_minutes=60)
    assert any(g.startswith("hour unexpected") for g in gaps)
    shifted_after_tail = [dict(cp, received_at=cp["received_at"] + 1) if cp["kind"] == "tail_after" else cp
                          for cp in cps]
    gaps = gate.churn_checkpoint_gaps(shifted_after_tail, minute_batches=25, stride_minutes=60)
    assert any(g.startswith("tail_after: expected received_at") for g in gaps)


def test_churn_status_requires_complete_checkpoints(monkeypatch):
    original = gate.churn_checkpoint_gaps
    monkeypatch.setattr(gate, "churn_checkpoint_gaps", lambda *a, **k: ["day 1 missing"])
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    assert c["status"] == "UNVERIFIED" and "day 1 missing" in (c["reason"] or "")
    monkeypatch.setattr(gate, "churn_checkpoint_gaps", original)


# --- highwater 선택 보존 (addendum slice5a4c_addendum_highwater.md, C′2 교체) ---------------------------
HW_KEYS = {"events", "resident_events", "job_events", "both_events", "backing_changes", "preserved_pairs",
           "violations", "first_violation", "unverified_events", "first_unverified", "checks", "replay",
           "backing_checks", "register_attempts_ref", "first_pass_complete"}
REPLAY_KEYS = {"performed", "stopped_after_seq", "compared_steps", "mismatch", "pass1_seconds", "pass2_seconds",
               "stop_reason", "comparison_digest_by_seq"}
HW_SIDE_KEYS = {"H_res", "H_job", "Q_4", "E", "B", "Q_actual"}


def _run_with_register_spy(monkeypatch, name, **kw):
    """게이트와 무관하게 register 호출 전후 H_res/H_job 증가를 직접 센다."""
    seen = []
    original = lg.RoundLedger.register

    def spy(self, **kwargs):
        before = (self._q_highwater, self._job_highwater)
        out = original(self, **kwargs)
        after = (self._q_highwater, self._job_highwater)
        if out.get("classification") == "registered" and after != before:
            seen.append((id(self), kwargs["received_at"], after[0] > before[0], after[1] > before[1]))
        return out

    monkeypatch.setattr(lg.RoundLedger, "register", spy)
    points = []
    c = gate.run_churn_fixture(name, limit=128, _capacity_observer=lambda led, fx, kind, m, *, budget=None:
                               points.append((kind, m)), **SMALL_CHURN, **kw)
    first = seen[0][0] if seen else None                              # 1차 원장만 센다(2차 재실행은 제외)
    return c, [row[1:] for row in seen if row[0] == first], points


@pytest.mark.parametrize("name", ["churn_most_finished_short_ascii", "churn_burst_ascii128"])
def test_highwater_checks_cover_every_register_increase(monkeypatch, name):
    c, seen, _ = _run_with_register_spy(monkeypatch, name)
    hw = c["highwater_checks"]
    assert set(hw) == HW_KEYS
    assert hw["events"] == len(hw["checks"]) == len(seen) > 0
    assert hw["resident_events"] == sum(r for _, r, _ in seen)
    assert hw["job_events"] == sum(j for _, _, j in seen)
    assert hw["both_events"] == sum(r and j for _, r, j in seen)
    assert [ch["received_at"] for ch in hw["checks"]] == [at for at, _, _ in seen]
    assert hw["violations"] == 0 and hw["first_violation"] is None
    assert hw["unverified_events"] == 0 and hw["first_unverified"] is None
    assert c["status"] == "PASS"


def test_highwater_check_records_before_after_and_limits():
    c = _default_churn()
    for ch in c["highwater_checks"]["checks"]:
        assert {"minute_index", "received_at", "received_mono", "N_total_before", "N_total_after",
                "before", "after", "preserved"} <= set(ch)
        assert ch["N_total_after"] == ch["N_total_before"] + 1
        assert set(ch["before"]) >= HW_SIDE_KEYS and set(ch["after"]) >= HW_SIDE_KEYS
        b, a = ch["before"], ch["after"]
        assert a["H_res"] > b["H_res"] or a["H_job"] > b["H_job"]
        for side in (b, a):
            assert side["Q_actual"] <= side["Q_4"] and side["E"] <= side["B"]


def test_highwater_preserves_pairs_only_on_backing_or_job_change():
    c = _default_churn()
    hw = c["highwater_checks"]
    for ch in hw["checks"]:
        b, a = ch["before"], ch["after"]
        if a["H_job"] > b["H_job"] or a["Q_actual"] != b["Q_actual"]:
            assert ch["preserved"] is True
        if not ch["preserved"]:
            assert a["Q_actual"] == b["Q_actual"] and a["H_job"] == b["H_job"]
    preserved = [ch for ch in hw["checks"] if ch["preserved"]]
    assert hw["preserved_pairs"] == len(preserved) >= hw["job_events"]
    assert hw["backing_changes"] <= hw["preserved_pairs"] <= hw["events"]
    befores = [cp for cp in c["checkpoints"] if cp["kind"] == "highwater_before"]
    afters = [cp for cp in c["checkpoints"] if cp["kind"] == "highwater_after"]
    assert len(befores) == len(afters) == len(preserved)
    for ch, cb, ca in zip(preserved, befores, afters):
        assert cb["received_at"] == ca["received_at"] == ch["received_at"]
        assert cb["minute_index"] == ca["minute_index"] == ch["minute_index"]
        assert cb["N_total"] == ch["N_total_before"] and ca["N_total"] == ch["N_total_after"]
    assert gate.highwater_pair_gaps(hw, c["checkpoints"]) == []


def test_highwater_capacity_observer_gets_same_pairs():
    points = []
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128,
                               _capacity_observer=lambda led, fx, kind, m, *, budget=None: points.append((kind, m)),
                               **SMALL_CHURN)
    churn_pairs = [(cp["kind"], cp["minute_index"]) for cp in c["checkpoints"]
                   if cp["kind"] in ("highwater_before", "highwater_after")]
    observed = [p for p in points if p[0] in ("highwater_before", "highwater_after")]
    assert observed == churn_pairs and len(observed) == 2 * c["highwater_checks"]["preserved_pairs"]


def test_highwater_pair_gaps_detect_mismatch():
    c = _default_churn()
    hw, cps = c["highwater_checks"], c["checkpoints"]
    drop_after = list(cps)
    drop_after.remove(next(cp for cp in cps if cp["kind"] == "highwater_after"))
    assert gate.highwater_pair_gaps(hw, drop_after)
    shifted = [dict(cp, received_at=cp["received_at"] + 1) if cp["kind"] == "highwater_before" else cp
               for cp in cps]
    assert gate.highwater_pair_gaps(hw, shifted)
    fewer_checks = dict(hw, checks=hw["checks"][:-1])
    assert gate.highwater_pair_gaps(fewer_checks, cps)
    unpreserved = dict(hw, checks=[dict(ch, preserved=False) for ch in hw["checks"]])
    assert gate.highwater_pair_gaps(unpreserved, cps)


def test_highwater_gaps_make_fixture_unverified(monkeypatch):
    monkeypatch.setattr(gate, "highwater_pair_gaps", lambda *a, **k: ["pair 3 missing"])
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    assert c["status"] == "UNVERIFIED" and "pair 3 missing" in (c["reason"] or "")


def test_highwater_violation_fails_fixture(monkeypatch):
    monkeypatch.setattr(lg.RoundLedger, "_q_charge", lambda self, resident=None, jobs=None: 0)
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    hw = c["highwater_checks"]
    assert hw["violations"] > 0 and hw["first_violation"] is not None
    assert hw["first_violation"]["after"]["Q_actual"] > hw["first_violation"]["after"]["Q_4"] or \
        hw["first_violation"]["before"]["Q_actual"] > hw["first_violation"]["before"]["Q_4"]
    assert c["status"] == "FAIL"


# --- 2-pass 재실행 (addendum slice5a4c_addendum_highwater_2pass.md) -------------------------------------
def test_replay_stops_after_last_selected_event_and_matches():
    calls = []
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN,
                               _replay_hook=lambda seq, led, wall: calls.append(seq))
    hw = c["highwater_checks"]
    rp = hw["replay"]
    assert set(rp) == REPLAY_KEYS
    seqs = [ch["register_seq"] for ch in hw["checks"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and min(seqs) >= 0
    selected = ([ch["register_seq"] for ch in hw["checks"] if ch["preserved"]] +
                [ch["register_seq"] for ch in hw["backing_checks"]])
    assert selected and rp["performed"] is True and rp["mismatch"] is None
    assert rp["stopped_after_seq"] == max(selected)
    assert rp["stop_reason"] == "last_selected"
    assert rp["compared_steps"] == rp["stopped_after_seq"] + 1
    assert calls == list(range(rp["stopped_after_seq"] + 1))        # 2차는 마지막 선택 순번에서 멈춘다
    assert rp["pass1_seconds"] >= 0 and rp["pass2_seconds"] >= 0
    assert c["status"] == "PASS"


def test_replay_hook_is_pass2_only(monkeypatch):
    seen = []
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN,
                               _replay_hook=lambda seq, led, wall: seen.append(id(led)))
    assert len(set(seen)) == 1                                         # 한 원장(2차)에서만 불린다
    assert c["highwater_checks"]["replay"]["mismatch"] is None


def _perturb_at(target, action):
    def hook(seq, led, wall):
        if seq == target:
            action(led, wall)
    return hook


def test_replay_detects_extra_registration_before_last_selected():
    c = gate.run_churn_fixture(
        "churn_most_finished_short_ascii", limit=128, **SMALL_CHURN,
        _replay_hook=_perturb_at(1, lambda led, wall: gate._register_fixture(led, "zz-perturb", "investing", wall)))
    rp = c["highwater_checks"]["replay"]
    assert rp["mismatch"] and c["status"] == "UNVERIFIED" and rp["mismatch"] in (c["reason"] or "")


def test_replay_detects_state_change_without_new_record():
    def bump(led, wall):
        led._job_highwater += 1
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN,
                               _replay_hook=_perturb_at(0, bump))
    assert c["highwater_checks"]["replay"]["mismatch"] and c["status"] == "UNVERIFIED"


def test_replay_ignores_changes_after_stop():
    c0 = _default_churn()
    stop = c0["highwater_checks"]["replay"]["stopped_after_seq"]
    c = gate.run_churn_fixture(
        "churn_most_finished_short_ascii", limit=128, **SMALL_CHURN,
        _replay_hook=_perturb_at(stop + 1, lambda led, wall: gate._register_fixture(led, "zz-late", "investing", wall)))
    assert c["highwater_checks"]["replay"]["mismatch"] is None and c["status"] == "PASS"


def test_full_graph_walks_not_proportional_to_events(monkeypatch):
    walks = []
    original = gate.owned_graph
    monkeypatch.setattr(gate, "owned_graph", lambda root: walks.append(1) or original(root))
    c = gate.run_churn_fixture("churn_most_unfinished_ascii128", limit=4096,
                               churn_minutes=40, churn_stride_minutes=1, fixture_detail_divisor=256)
    hw = c["highwater_checks"]
    assert hw["events"] > 2 * len(c["checkpoints"])                  # 비공허: 사건이 체크포인트보다 훨씬 많다
    assert len(walks) <= len(c["checkpoints"])                        # 저장된 체크포인트마다 많아야 1회
    assert c["status"] == "PASS"


# --- 배터리 생존 변이 보강(G19·G22·G23): 조건 하나만 다르게 만드는 주입 ----------------------------------
def test_job_highwater_preserved_without_backing_change(monkeypatch):
    monkeypatch.setattr(gate, "_capacity_containers", lambda led: [])   # backing 관측을 비워 H_job 규칙만 남긴다
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    job_checks = [ch for ch in c["highwater_checks"]["checks"] if ch["after"]["H_job"] > ch["before"]["H_job"]]
    assert any(not ch["backing_changed"] for ch in job_checks)       # 비공허: backing 변화 없는 H_job 경신이 있다
    assert all(ch["preserved"] for ch in job_checks)


def test_replay_detects_after_layout_only_difference(monkeypatch):
    c0 = _default_churn()
    stop = c0["highwater_checks"]["replay"]["stopped_after_seq"]
    original = gate._capacity_containers
    monkeypatch.setattr(gate, "_capacity_containers", lambda led: original(led) + (
        [("alias.records", led._records)] if getattr(led, "_test_alias", False) else []))

    def arm(seq, led, wall):                                          # 마지막 선택 순번의 register 직후부터만 경로가 늘어난다
        if seq == stop:
            real = led.register
            def register(**kw):
                out = real(**kw)
                led._test_alias = True
                return out
            led.register = register
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN, _replay_hook=arm)
    rp = c["highwater_checks"]["replay"]
    assert rp["mismatch"] and f"seq {stop}" in rp["mismatch"] and c["status"] == "UNVERIFIED"


@pytest.mark.parametrize("kind,label", [("highwater_before", "pre-capture"), ("highwater_after", "post-capture")])
def test_replay_detects_capture_that_changes_state(monkeypatch, kind, label):
    original = gate._churn_checkpoint

    def capture(ledger, k, *a, **kw):
        out = original(ledger, k, *a, **kw)
        if k == kind:
            ledger._job_highwater += 1                                # 캡처가 원장 상태를 바꾸는 결함
        return out
    monkeypatch.setattr(gate, "_churn_checkpoint", capture)
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    rp = c["highwater_checks"]["replay"]
    assert rp["mismatch"] and label in rp["mismatch"] and c["status"] == "UNVERIFIED"



# --- 경신이 아닌 호출의 첫 위반도 전후 원자료를 보존한다 (addendum_highwater_2pass §1 '첫 위반') ----------------
def test_first_violation_on_non_event_call_is_preserved(monkeypatch):
    original = gate._highwater_measure

    def inflated(ledger, baseline_sizes, fixed):                     # 상주가 high-water 아래일 때만 위반을 만든다
        side, layout = original(ledger, baseline_sizes, fixed)
        if ledger._health["N_res"] < ledger._q_highwater:
            side = dict(side, Q_actual=side["Q_4"] + 1)
        return side, layout
    monkeypatch.setattr(gate, "_highwater_measure", inflated)
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    hw = c["highwater_checks"]
    fv = hw["first_violation"]
    assert hw["violations"] > 0 and fv is not None and c["status"] == "FAIL"
    assert fv["register_seq"] not in {ch["register_seq"] for ch in hw["checks"]}   # 비공허: 경신이 아닌 호출
    assert fv["preserved"] is True
    pair = [cp for cp in c["checkpoints"] if cp["kind"] in ("violation_before", "violation_after")]
    assert [cp["kind"] for cp in pair] == ["violation_before", "violation_after"]
    assert all(cp["received_at"] == fv["received_at"] for cp in pair)
    assert pair[0]["N_total"] == fv["N_total_before"] and pair[1]["N_total"] == fv["N_total_after"]
    assert gate.highwater_pair_gaps(hw, c["checkpoints"]) == []
    no_after = [cp for cp in c["checkpoints"] if cp["kind"] != "violation_after"]
    assert gate.highwater_pair_gaps(hw, no_after)
    shifted = [dict(cp, received_at=cp["received_at"] + 1) if cp["kind"] == "violation_before" else cp
               for cp in c["checkpoints"]]
    assert gate.highwater_pair_gaps(hw, shifted)



# --- full run 3 에서 드러난 결함 D1~D4 (codex_5a4c_run3_review_req.md + Codex 판정) -----------------------
@pytest.mark.parametrize("name", ["churn_most_finished_short_ascii", "churn_burst_ascii128"])
def test_budget_stop_during_replay_is_budget_not_mismatch(name):
    state = {"stop": False}

    def hook(seq, led, wall):                                         # 2차에서만 불린다 → 2차 도중 예산 소진
        if seq == 3:
            state["stop"] = True
    c = gate.run_churn_fixture(name, limit=128, **SMALL_CHURN, _replay_hook=hook,
                               _stop_requested=lambda: state["stop"])
    hw = c["highwater_checks"]
    rp = hw["replay"]
    selected = [ch["register_seq"] for ch in hw["checks"] if ch["preserved"]]
    assert rp["stopped_after_seq"] < max(selected)                     # 비공허: 마지막 선택 전에 멈췄다
    assert rp["mismatch"] is None and rp["stop_reason"] == "budget"    # D1: 예산 정지는 불일치가 아니다
    assert c["status"] == "UNVERIFIED" and (c["first_failure"] or {}).get("phase") == "budget"


def _replay_budget_run(monkeypatch):
    state = {"pass2": False}
    original = gate.run_churn_fixture

    def spy(*a, **kw):
        if kw.get("_replay_expected") is not None:
            state["pass2"] = True
        return original(*a, **kw)
    monkeypatch.setattr(gate, "run_churn_fixture", spy)
    tick = [0.0]

    def wall():                                                       # 2차가 시작되면 시계가 예산을 넘긴다
        tick[0] += 1e-3
        return tick[0] + (10 ** 7 if state["pass2"] else 0.0)
    return gate.run_gate(limit=128, samples=3, warmup=1, churn_names=["churn_burst_ascii128"], wall=wall,
                         budget_seconds=3600, progress=io.StringIO(), **SMALL_CHURN)


def test_budget_stop_inside_final_fixture_marks_run_incomplete(monkeypatch):
    r = _replay_budget_run(monkeypatch)
    assert r["complete"] is False and r["aborted_reason"] == "budget"   # D2


def test_capacity_not_pass_when_required_pairs_missing(monkeypatch):
    r = _replay_budget_run(monkeypatch)
    fx = r["churn_fixtures"][0]
    assert fx["highwater_checks"]["preserved_pairs"] < sum(ch["preserved"] for ch in fx["highwater_checks"]["checks"])
    assert r["capacity_proof"]["status"] != "PASS"                     # D3: 필수 관측점 누락은 PASS 불가
    row = next(x for x in r["scenarios"] if x["name"] == "capacity_highwater")
    assert row["status"] != "PASS"


def test_fail_wins_over_replay_mismatch(monkeypatch):
    monkeypatch.setattr(lg.RoundLedger, "_q_charge", lambda self, resident=None, jobs=None: 0)
    c = gate.run_churn_fixture(
        "churn_most_finished_short_ascii", limit=128, **SMALL_CHURN,
        _replay_hook=_perturb_at(1, lambda led, wall: gate._register_fixture(led, "zz-perturb", "investing", wall)))
    hw = c["highwater_checks"]
    assert hw["violations"] > 0 and hw["replay"]["mismatch"]           # 비공허: 둘 다 있다
    assert c["status"] == "FAIL"                                       # D4: 발견한 FAIL 이 우선


def test_capacity_proof_requires_each_preserved_pair_point():
    # 중단 없이도 필수 관측점 하나가 빠지면 PASS 가 아니다(D3 의 missing 조건을 interrupted 와 분리해 잠근다).
    r = gate.run_gate(limit=128, samples=3, warmup=1, churn_names=["churn_most_finished_short_ascii"],
                      **SMALL_CHURN)
    points, fixtures = r["capacity_proof"]["checkpoints"], r["churn_fixtures"]
    assert r["capacity_proof"]["status"] == "PASS"                     # 비공허: 완전하면 PASS
    whole = gate._capacity_proof(points, fixtures, ["churn_most_finished_short_ascii"])
    assert whole["status"] == "PASS"
    drop = next(i for i, p in enumerate(points) if p["kind"] == "highwater_after")
    partial = gate._capacity_proof(points[:drop] + points[drop + 1:], fixtures, ["churn_most_finished_short_ascii"])
    assert partial["status"] == "UNVERIFIED" and "missing" in (partial["reason"] or "")


# --- C′2 체크포인트 원자료 확장 (slice5a4c_addendum_c2_evidence.md, Codex 설계 codex_c2_checkpoint_design_r1.md) ----
C2_COMMON = {"checkpoint_id", "observation", "admission_counts", "source_counts", "last_seq", "registered_records",
             "diagnostic_counters"}
C2_PUBLIC = {"epoch_sources", "recent_cohorts", "cursor_probes"}
C2_DIAG = {"retention_expired", "expired_start", "expired_finish", "expired_wrapper", "expired_identity_unverified",
           "init_failed"}
PROBE_PAIRS = [("probe_close_before", "probe_close_at"), ("probe_expire_before", "probe_expire_at"),
               ("probe_prune_before", "probe_prune_at"), ("probe_recent_before", "probe_recent_at")]


def _by_kind(c, kind):
    return [cp for cp in c["checkpoints"] if cp["kind"] == kind]


def test_c2_checkpoint_fields_present():
    c = _default_churn()
    ids = [cp["checkpoint_id"] for cp in c["checkpoints"]]
    assert len(ids) == len(set(ids)) and all(isinstance(i, str) and i for i in ids)
    for cp in c["checkpoints"]:
        assert C2_COMMON <= set(cp), cp["kind"]
        assert cp["observation"] in ("public_advance", "passive")
        assert C2_DIAG <= set(cp["diagnostic_counters"])
        if cp["kind"] in ("hour", "day", "tail_before", "tail_after"):
            assert C2_PUBLIC <= set(cp), cp["kind"]
        if cp["kind"] in ("highwater_before", "highwater_after"):
            assert cp["observation"] == "passive"
    for cp in _by_kind(c, "hour"):
        assert type(cp["pruned_since_previous_hour"]) is int


def test_c2_invariants_hold_on_normal_run():
    c = _default_churn()
    for cp in c["checkpoints"]:
        ac = cp["admission_counts"]
        assert ac["scheduled"] == ac["registered"] + ac["rejected"] and ac["rejected"] == 0
        assert cp["last_seq"] == cp["N_total"] == cp["registered_records"]
        assert sum(v["registered"] for v in cp["source_counts"].values()) == cp["N_total"]
        if "epoch_sources" in cp:
            srcs = cp["epoch_sources"]
            assert sum(s["registered_invocations"] for s in srcs) == cp["N_total"]
            assert sum(s["live_invocations"] for s in srcs) == cp["N_live"]
            for s in srcs:
                assert s["registered_invocations"] == s["frozen_invocations"] + s["live_invocations"]
                assert s["equations_hold"] == {"connection": True, "lifecycle": True}
            for rc in cp["recent_cohorts"].values():
                assert rc["equations_hold"] == {"connection": True, "lifecycle": True}
            for probe in cp["cursor_probes"]:
                seqs = probe["entry_seqs"]
                assert all(s > probe["after_seq"] for s in seqs) and seqs == sorted(set(seqs))


def test_c2_prune_counts_from_resident_ledger():
    c = _default_churn()
    hours = _by_kind(c, "hour")
    assert [h["pruned_since_previous_hour"] for h in hours[:3]] == [0, 0, 0]
    assert sum(h["pruned_since_previous_hour"] for h in hours) > 0              # 비공허: 실제 prune 이 있다
    for a, b in zip(hours, hours[1:]):
        accepted = b["N_total"] - a["N_total"]
        assert b["pruned_since_previous_hour"] == a["N_res"] + accepted - b["N_res"]


@pytest.mark.parametrize("name", ["churn_most_finished_short_ascii", "churn_init_failed_most_finished_unicode128",
                                  "churn_burst_ascii128"])
def test_c2_boundary_probes_observe_expected_states(name):
    c = _default_churn() if name == "churn_most_finished_short_ascii" else \
        gate.run_churn_fixture(name, limit=128, **SMALL_CHURN)
    expected = {"probe_close_before": "live", "probe_close_at": "tombstoned",
                "probe_expire_before": "live", "probe_expire_at": "tombstoned",
                "probe_prune_before": "tombstoned", "probe_prune_at": "expired_or_untracked"}
    for before, at in PROBE_PAIRS:
        b, a = _by_kind(c, before), _by_kind(c, at)
        assert b and len(b) == len(a), (name, before)
        for cp in b + a:
            for s in cp["identity_samples"]:
                assert s["observed"] == s["expected"], (cp["kind"], s)
                if cp["kind"] in expected:
                    assert s["expected"] == expected[cp["kind"]]
    cats = {s["category"] for cp in _by_kind(c, "probe_prune_at") for s in cp["identity_samples"]}
    assert {"finished", "linked", "unbound"} <= cats
    if "init_failed" in name:
        assert "init_failed" in cats
        tail = _by_kind(c, "tail_after")[0]
        assert tail["diagnostic_counters"]["init_failed"] > 0
    if "burst" in name:
        assert {"burst_finished", "burst_probe"} <= cats


def test_c2_evidence_gaps_detect_missing_data():
    c = _default_churn()
    assert gate.churn_evidence_gaps(c) == [] and c["evidence_gaps"] == []
    no_epoch = copy.deepcopy(c)
    del next(cp for cp in no_epoch["checkpoints"] if cp["kind"] == "hour")["epoch_sources"]
    assert gate.churn_evidence_gaps(no_epoch)
    no_prune_probe = copy.deepcopy(c)
    no_prune_probe["checkpoints"] = [cp for cp in c["checkpoints"] if cp["kind"] != "probe_prune_at"]
    assert gate.churn_evidence_gaps(no_prune_probe)
    no_diag = copy.deepcopy(c)
    del next(cp for cp in no_diag["checkpoints"] if cp["kind"] == "day")["diagnostic_counters"]["init_failed"]
    assert gate.churn_evidence_gaps(no_diag)


def test_c2_evidence_gaps_make_fixture_unverified(monkeypatch):
    monkeypatch.setattr(gate, "churn_evidence_gaps", lambda *a, **k: ["epoch_sources missing at hour 3"])
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    assert c["status"] == "UNVERIFIED" and "epoch_sources missing at hour 3" in (c["reason"] or "")


def test_c2_broken_epoch_equation_fails(monkeypatch):
    original = lg.RoundLedger.epoch_cohort_totals

    def broken(self, **kw):
        out = original(self, **kw)
        if out.get("sources"):
            out["sources"][0]["live_invocations"] += 1                       # 완전 관측의 등식 위반
        return out
    monkeypatch.setattr(lg.RoundLedger, "epoch_cohort_totals", broken)
    c = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128, **SMALL_CHURN)
    assert c["status"] == "FAIL"


def test_c2_probes_keep_replay_consistent():
    c = _default_churn()
    rp = c["highwater_checks"]["replay"]
    assert rp["mismatch"] is None and rp["stop_reason"] == "last_selected"
    assert _by_kind(c, "probe_prune_at")                                     # 탐침이 있는 상태에서 재실행이 일치



def test_result_keys_are_not_hidden_from_iteration():
    # 반복으로 보이는 키와 JSON 에 실리는 키가 같아야 한다(dict 하위 클래스로 키를 숨기는 우회 금지).
    c = _default_churn()
    assert type(c) is dict and set(c) == set(json.loads(json.dumps(c)))
    for cp in c["checkpoints"]:
        assert type(cp) is dict and set(cp) == set(json.loads(json.dumps(cp))), cp["kind"]


# --- 배터리 생존 보강(G33~G36·G39): 각 검사만 건드리는 입력 --------------------------------------------
def test_c2_gaps_detect_single_missing_prune_category():
    c = copy.deepcopy(_default_churn())
    relabeled = 0
    for cp in _by_kind(c, "probe_prune_at"):
        for sample in cp["identity_samples"]:
            if sample["category"] == "linked":
                sample["category"] = "finished"                               # 표본은 남기고 범주만 빠진다
                relabeled += 1
    assert relabeled > 0 and all(cp["identity_samples"] for cp in _by_kind(c, "probe_prune_at"))
    assert any("prune categories" in g for g in gate.churn_evidence_gaps(c))


def test_c2_gaps_detect_empty_probe_samples():
    c = copy.deepcopy(_default_churn())
    _by_kind(c, "probe_close_before")[0]["identity_samples"] = []
    assert any("identity_samples missing" in g for g in gate.churn_evidence_gaps(c))


def test_c2_violations_detect_epoch_total_with_consistent_rows():
    c = copy.deepcopy(_default_churn())
    cp = next(cp for cp in _by_kind(c, "hour") if len(cp["epoch_sources"]) > 1 and
              cp["epoch_sources"][0]["registered_invocations"] > 0)
    cp["epoch_sources"] = cp["epoch_sources"][1:]                           # 남은 행은 각자 등식을 지킨다
    assert gate._churn_evidence_violations([cp])


def test_c2_violations_detect_identity_mismatch():
    c = copy.deepcopy(_default_churn())
    cp = _by_kind(c, "probe_prune_at")[0]
    cp["identity_samples"][0]["observed"] = "live"
    assert gate._churn_evidence_violations([cp])


def test_c2_violations_detect_old_cohort_not_rejected():
    c = copy.deepcopy(_default_churn())
    cp = next(cp for cp in c["checkpoints"] if cp.get("old_cohort_probe"))  # 비공허: 오래된 cohort 탐침이 있다
    assert cp["old_cohort_probe"]["classification"] == "cohort_expired"
    cp["old_cohort_probe"]["classification"] = "snapshot"
    assert gate._churn_evidence_violations([cp])


# --- C′2 커밋 검토 REVISE 반영(Codex): W 경계 기대값·source별 대조·탐침별 범주 완전성 -----------------------
PROBE_CATEGORIES = {"close": {"finished"}, "expire": {"linked", "unbound"}, "prune": {"finished", "linked", "unbound"},
                    "recent": {"finished"}}


def _required_categories(name, kind):
    need = set(PROBE_CATEGORIES[kind])
    if "init_failed" in name and kind in ("expire", "prune"):
        need.add("init_failed")
    if "burst" in name:
        need |= {"close": {"burst_finished"}, "expire": {"burst_probe"},
                 "prune": {"burst_finished", "burst_probe"}, "recent": set()}[kind]
    return need


@pytest.mark.parametrize("name", ["churn_most_finished_short_ascii", "churn_init_failed_most_finished_unicode128",
                                  "churn_burst_ascii128"])
def test_c2_every_probe_kind_has_required_categories(name):
    c = _default_churn() if name == "churn_most_finished_short_ascii" else \
        gate.run_churn_fixture(name, limit=128, **SMALL_CHURN)
    for kind in PROBE_CATEGORIES:
        for side in ("before", "at"):
            cats = {s["category"] for cp in _by_kind(c, f"probe_{kind}_{side}") for s in cp["identity_samples"]}
            assert _required_categories(name, kind) <= cats, (name, kind, side, cats)


def test_c2_gaps_detect_missing_burst_close_samples():
    c = gate.run_churn_fixture("churn_burst_ascii128", limit=128, **SMALL_CHURN)
    assert gate.churn_evidence_gaps(c) == []
    for cp in c["checkpoints"]:
        if cp["kind"] in ("probe_close_before", "probe_close_at"):
            cp["identity_samples"] = [s for s in cp["identity_samples"] if s["category"] != "burst_finished"]
    c["checkpoints"] = [cp for cp in c["checkpoints"]
                        if not (cp["kind"].startswith("probe_close") and not cp["identity_samples"])]
    assert any(cp["kind"] == "probe_close_at" for cp in c["checkpoints"])      # 비공허: finished close 는 남는다
    assert gate.churn_evidence_gaps(c)


def test_c2_recent_window_probe_distinguishes_boundary():
    c = _default_churn()
    before, at = _by_kind(c, "probe_recent_before"), _by_kind(c, "probe_recent_at")
    assert before and len(before) == len(at)
    for b, a in zip(before, at):
        assert "recent_rows" in b and "recent_rows" in a
        sb = {s["id"]: s for s in b["identity_samples"]}
        for s in a["identity_samples"]:
            for side in (sb[s["id"]], s):
                assert side["observed_in_recent"] == side["expected_in_recent"]
            assert sb[s["id"]]["expected_in_recent"] != s["expected_in_recent"]   # 경계 전후 기대가 다르다


def test_c2_source_counts_match_epoch_rows_per_source():
    c = _default_churn()
    for cp in c["checkpoints"]:
        if "epoch_sources" in cp:
            for row in cp["epoch_sources"]:
                assert row["registered_invocations"] == cp["source_counts"].get(row["source"], {}).get("registered", 0)
    cp = copy.deepcopy(next(cp for cp in _by_kind(c, "hour")
                            if sum(1 for v in cp["source_counts"].values() if v["registered"]) >= 2))
    srcs = [s for s, v in cp["source_counts"].items() if v["registered"]]
    a, b = srcs[0], srcs[1]
    if cp["source_counts"][a]["registered"] == cp["source_counts"][b]["registered"]:
        cp["source_counts"][a]["registered"] += 1
        cp["source_counts"][b]["registered"] -= 1
    else:
        cp["source_counts"][a]["registered"], cp["source_counts"][b]["registered"] = \
            cp["source_counts"][b]["registered"], cp["source_counts"][a]["registered"]
    assert gate._churn_evidence_violations([cp])                             # 합계는 같고 source 별만 어긋남


def test_c2_violations_detect_recent_window_mismatch():
    c = copy.deepcopy(_default_churn())
    cp = next(cp for cp in _by_kind(c, "probe_recent_before") + _by_kind(c, "probe_recent_at")
              if cp["identity_samples"][0]["expected_in_recent"])                # 비공허: 포함이 기대되는 쪽
    assert not gate._churn_evidence_violations([cp])
    sample = cp["identity_samples"][0]
    sample["observed_recent_marker_count"] = 0
    sample["observed_in_recent"] = False
    assert gate._churn_evidence_violations([cp])


# X2 B second pass: exercise the rows against the gate's own small raw fixture.
def _project_b_row(row, fixture):
    projected = copy.deepcopy(fixture)
    if row == "B21":
        names = {"tail_before": "tail_80640", "tail_after": "tail_80641"}
        for cp in projected["checkpoints"]:
            cp["kind"] = names.get(cp["kind"], cp["kind"])
    scale = ({"cycle_length": 20, "finished": 16, "unbound": 2,
              "cycles": 10, "churn_names": [projected["name"]]} if row == "B02" else
             {"hours": 25, "pre_prune_hours": 3} if row == "B10" else {})
    return {"mode": "predicate_unit", "churn_fixtures": [projected], "test_scale": scale}


@pytest.mark.parametrize("row", ("B08", "B09", "B10", "B12", "B14", "B15",
                                     "B16", "B17", "B18", "B21"))
def test_x2_b_remaining_rows_pass_on_small_raw_fixture(row):
    report = _project_b_row(row, _default_churn())
    assert gate.evaluate_row(row, report, attachments={})["status"] == "PASS"


@pytest.mark.parametrize("row", ("B02", "B07", "B08", "B09", "B10", "B12",
                                 "B14", "B15", "B16", "B18", "B21"))
def test_x2_b_gate_shaped_rows_require_minute_batches(row):
    report = _project_b_row(row, _default_churn())
    assert gate.evaluate_row(row, report, attachments={})["status"] == "PASS", row
    del report["churn_fixtures"][0]["minute_batches"]
    result = gate.evaluate_row(row, report, attachments={})
    assert result["status"] == "UNVERIFIED" and "minute_batches" in result["reason"], (row, result)


def test_x2_b_b14_close_input_mono_deadline_must_have_passed():
    report = _project_b_row("B14", _default_churn())
    assert gate.evaluate_row("B14", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    first_finish = next(t for t in fixture["fixture_provenance"]["api_trace"]
                        if t["action"] == "finish")
    first_finish["close_mono"] = fixture["checkpoints"][-1]["received_mono"] + 1
    assert gate.evaluate_row("B14", report, attachments={})["status"] != "PASS"


def test_x2_b_b14_prune_watermark_uses_all_active_tombs():
    report = _project_b_row("B14", _default_churn())
    assert gate.evaluate_row("B14", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    checkpoint = next(cp for cp in fixture["checkpoints"] if cp["kind"] == "probe_prune_at")
    active = {}
    for event in fixture["fixture_provenance"]["api_trace"]:
        if event["event_order"] > checkpoint["event_order"]:
            break
        if event["action"] == "tomb":
            active[event["id"]] = event
        elif event["action"] == "prune":
            active.pop(event["id"], None)
    future = next(tomb for tomb in active.values()
                  if tomb["prune_due_at"] > checkpoint["received_at"])
    future["prune_due_at"] = checkpoint["received_at"]
    assert gate.evaluate_row("B14", report, attachments={})["status"] != "PASS"


def test_x2_b_b02_requires_stride_on_gate_shaped_fixture():
    report = _project_b_row("B02", _default_churn())
    del report["churn_fixtures"][0]["stride_minutes"]
    assert gate.evaluate_row("B02", report, attachments={})["status"] == "UNVERIFIED"


def test_x2_b_b02_tail_must_be_observed_as_non_auxiliary():
    report = _project_b_row("B02", _default_churn())
    registers = [t for t in report["churn_fixtures"][0]["fixture_provenance"]["api_trace"]
                 if t["action"] == "register"]
    registers[-1]["auxiliary"] = True
    assert gate.evaluate_row("B02", report, attachments={})["status"] == "FAIL"


def test_x2_b_b02_tail_missing_trace_cannot_match_adjusted_count():
    report = _project_b_row("B02", _default_churn())
    assert gate.evaluate_row("B02", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    trace = fixture["fixture_provenance"]["api_trace"]
    tail = next(t for t in reversed(trace) if t["action"] == "register")
    assert tail["registered_seq"] == fixture["N_total_at_tail"]
    trace.remove(tail)
    fixture["classification_counts"]["registered"] -= 1
    assert gate.evaluate_row("B02", report, attachments={})["status"] != "PASS"


@pytest.fixture(scope="module")
def x2_c_small_raw(tmp_path_factory):
    root = tmp_path_factory.mktemp("x2-c-attempts")
    points = []
    baseline = {name: sys.getsizeof(obj) for name, obj in
                gate._capacity_containers(gate.new_ledger(128))}

    def observe(ledger, name, kind, minute_index, *, budget=None):
        if kind.endswith("_before") and hasattr(ledger, "_gate_capacity_pre_point"):
            point = dict(ledger._gate_capacity_pre_point)
        else:
            point = gate._capacity_point(ledger, baseline, name, kind, minute_index,
                                         budget=budget or gate._budget_checkpoint(ledger))
        point["checkpoint_id"] = f"observed:{len(points)}"
        points.append(point)
        return point["checkpoint_id"]

    fixture = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128,
                                     churn_minutes=25, churn_stride_minutes=60,
                                     fixture_detail_divisor=256, attachment_dir=root,
                                     _capacity_observer=observe)
    ref = fixture["highwater_checks"]["register_attempts_ref"]
    blob = (root / ref["path"]).read_bytes()
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "capacity_proof": {"checkpoints": points}}
    return report, {ref["path"]: blob}


def test_x2_c_real_small_attempts_and_replay_pass(x2_c_small_raw):
    report, attachments = x2_c_small_raw
    fixture = report["churn_fixtures"][0]
    highwater = fixture["highwater_checks"]
    assert highwater["register_attempts_ref"]["rows"] == 201
    assert highwater["backing_checks"]
    assert all("normalized_layout_ref" in check for check in
               highwater["checks"] + highwater["backing_checks"])
    assert {row: gate.evaluate_row(row, report, attachments=attachments)["status"]
            for row in ("C01", "C02", "C03", "C04", "C05", "C06")} == dict.fromkeys(
                ("C01", "C02", "C03", "C04", "C05", "C06"), "PASS")


def test_x2_c_attempts_keep_exact_g_at_selected_capacity_points(x2_c_small_raw):
    report, attachments = x2_c_small_raw
    fixture = report["churn_fixtures"][0]
    ref = fixture["highwater_checks"]["register_attempts_ref"]["path"]
    attempts = [json.loads(line) for line in gzip.decompress(attachments[ref]).splitlines()]
    assert all("G" not in attempt[side] for attempt in attempts for side in ("before", "after"))
    assert all("G" in check[side] for check in fixture["highwater_checks"]["checks"]
               if check["preserved"] for side in ("before", "after"))
    assert gate.evaluate_row("C04", report, attachments=attachments)["status"] == "PASS"


def test_x2_c_missing_selected_before_q_is_unverified(x2_c_small_raw):
    original, attachments = x2_c_small_raw
    report = copy.deepcopy(original)
    cp = next(cp for cp in report["churn_fixtures"][0]["checkpoints"]
              if cp["kind"] == "highwater_before")
    cp["capacity_ref"] = None
    assert gate.evaluate_row("C02", report, attachments=attachments)["status"] == "UNVERIFIED"
    assert gate.evaluate_row("C06", report, attachments=attachments)["status"] == "UNVERIFIED"


def test_x2_c_measured_q_over_limit_is_fail(x2_c_small_raw):
    original, attachments = x2_c_small_raw
    report = copy.deepcopy(original)
    point = next(point for point in report["capacity_proof"]["checkpoints"]
                 if 1 < point["Q_actual_bytes"] < point["Q_4_bytes"])
    point["Q_4_bytes"] = point["Q_actual_bytes"] - 1
    for backing in point["container_backings"]:
        backing["charged_limit_bytes"] = point["Q_4_bytes"]
    assert gate.evaluate_row("C06", report, attachments=attachments)["status"] == "FAIL"


def test_x2_c_no_rebuild_is_evidenced_and_missing_rebuild_witness_is_unverified(x2_c_small_raw):
    original, attachments = x2_c_small_raw
    report = copy.deepcopy(original)
    report["rebuild_events"] = []
    report["scenarios"] = [{"name": "rebuild_double_backing", "status": "N/A",
                            "reason": "N/A (no rebuild in this implementation)"}]
    assert gate.evaluate_row("C07", report, attachments=attachments)["status"] == "PASS"
    assert gate.evaluate_row("C08", report, attachments=attachments)["status"] == "N/A"
    report["churn_fixtures"][0]["checkpoints"][0]["rebuild_count"] = 1
    assert gate.evaluate_row("C08", report, attachments=attachments)["status"] == "UNVERIFIED"


def test_x2_c_gate_capacity_kinds_and_no_rebuild_projection():
    report = gate.run_gate(limit=128, samples=3, warmup=1,
                           churn_names=["churn_most_unfinished_short_ascii"],
                           **SMALL_CHURN, progress=io.StringIO())
    assert report["capacity_proof"]["status"] == "PASS"
    assert all(point["Q_actual_bytes"] <= point["Q_4_bytes"]
               for point in report["capacity_proof"]["checkpoints"])
    report["mode"] = "predicate_unit"
    assert {row: gate.evaluate_row(row, report, attachments={})["status"]
            for row in ("C06", "C07", "C08", "C09", "C10")} == {
                "C06": "PASS", "C07": "PASS", "C08": "N/A", "C09": "PASS",
                "C10": "UNVERIFIED"}


@pytest.mark.parametrize("mutation", ("missing", "corrupt", "source", "layout_ref", "inline_only"))
def test_x2_c_attempt_source_or_attachment_damage_cannot_pass(x2_c_small_raw, mutation):
    original, original_attachments = x2_c_small_raw
    report = copy.deepcopy(original)
    attachments = dict(original_attachments)
    highwater = report["churn_fixtures"][0]["highwater_checks"]
    ref = highwater["register_attempts_ref"]
    if mutation == "missing":
        attachments.clear()
    elif mutation == "corrupt":
        attachments[ref["path"]] = b"corrupt"
    elif mutation == "source":
        rows = [json.loads(line) for line in gzip.decompress(attachments[ref["path"]]).splitlines()]
        selected = next(row for row in rows if row["selected"])
        selected["after"]["H_res"] += 1
        blob = gzip.compress("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode(),
                             mtime=0)
        attachments[ref["path"]] = blob
        ref["sha256"] = hashlib.sha256(blob).hexdigest()
    elif mutation == "layout_ref":
        highwater["checks"][0]["normalized_layout_ref"]["layout_sha256"] = "0" * 64
    else:
        highwater["register_attempts"] = [json.loads(line) for line in
                                          gzip.decompress(attachments[ref["path"]]).splitlines()]
        del highwater["register_attempts_ref"]
        attachments.clear()
    result = gate.evaluate_row("C04", report, attachments=attachments)
    assert result["status"] != "PASS", (mutation, result)
    if mutation in ("missing", "corrupt", "layout_ref", "inline_only"):
        assert result["status"] == "UNVERIFIED", (mutation, result)


def test_x2_c_replay_digest_damage_is_unverified(x2_c_small_raw):
    original, attachments = x2_c_small_raw
    report = copy.deepcopy(original)
    report["churn_fixtures"][0]["highwater_checks"]["replay"]["comparison_digest_by_seq"][0][
        "pass2_sha256"] = "0" * 64
    assert gate.evaluate_row("C05", report, attachments=attachments)["status"] == "UNVERIFIED"


def test_x2_c_capacity_refs_exist_only_for_observed_points(x2_c_small_raw):
    report, _ = x2_c_small_raw
    fixture = report["churn_fixtures"][0]
    points = {p["checkpoint_id"]: p for p in report["capacity_proof"]["checkpoints"]}
    assert points and all(ref in points for cp in fixture["checkpoints"]
                          if (ref := cp["capacity_ref"]) is not None)
    assert any(cp["capacity_ref"] is None for cp in fixture["checkpoints"])
    assert any(cp["kind"] == "probe_prune_at" and cp["capacity_ref"] in points
               for cp in fixture["checkpoints"])


def test_x2_c_calibration_and_locked_input_shape(tmp_path):
    observations = [{"class_name": name, "completed_units": 1,
                     "projected_full_seconds": 2, "prepare_seconds": 0,
                     "clone_seconds": 0, "call_seconds": 1,
                     "pass1_seconds": 1, "pass2_seconds": 0,
                     "selected_pairs": 0, "error_seconds": 0}
                    for name in ("legacy_repeat", "pressure", "churn_normal",
                                 "churn_burst", "capacity", "resident", "adapter")]
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"status": "complete", "run_id": "cal-1",
                                "observations": observations}), encoding="utf-8")
    loaded = gate._read_evidence_input(path, "calibration")
    assert gate._calibration_eta(loaded["observations"], 14400)["seconds"] == 14
    assert gate._calibration_eta([], 14400)["decision"] == "unknown"
    locked = {name: {"passed": 1, "failed": 0, "commit_sha": "head",
                     "result_ref": f"locked/{name}.json", "result_sha256": "0" * 64}
              for name in ("d7", "index", "slice5a3", "slice5a4a", "slice5a4b")}
    path.write_text(json.dumps(locked), encoding="utf-8")
    assert gate._read_evidence_input(path, "locked tests") == locked


@pytest.mark.parametrize("future_axis", ("both", "received_mono"))
def test_x2_b_b16_future_link_cannot_change_published_lifecycle(future_axis):
    report = _project_b_row("B16", _default_churn())
    assert gate.evaluate_row("B16", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    trace = fixture["fixture_provenance"]["api_trace"]
    pub = next(cp for cp in fixture["checkpoints"] if cp["kind"] == "tail_after")
    projections = next(cp["passive_cohort_projection"] for cp in fixture["checkpoints"]
                       if "passive_cohort_projection" in cp)
    registration = next(t for t in trace if t["action"] == "register" and
                        t["source"] == "investing" and t["cycle_position"] == 17 and
                        pub["recent_cohorts"]["investing"]["range"][0] <= t["received_at"] < pub["received_at"])
    link = next(t for t in trace if t["action"] == "link_round" and t["id"] == registration["id"])
    assert not any(t["action"] == "finish" and t["id"] == registration["id"] for t in trace)
    assert pub["received_mono"] - registration["received_mono"] >= 15 * MIN
    registration["received_at"] = pub["received_at"] - MIN
    registration["received_mono"] = pub["received_mono"] - MIN
    if future_axis == "both":
        link["received_at"] = pub["received_at"] + MIN
    link["received_mono"] = pub["received_mono"] + MIN
    counts = pub["recent_cohorts"][registration["source"]]["counts"]
    if future_axis == "both":
        counts["connection"]["started"] -= 1
        counts["connection"]["unbound"] += 1
    counts["lifecycle"]["overdue"] -= 1
    counts["lifecycle"]["in_flight"] += 1
    next(p for p in projections if p["source"] == registration["source"])["counts"] = copy.deepcopy(counts)
    assert gate.evaluate_row("B16", report, attachments={})["status"] != "PASS"


def test_x2_b_b09_future_expiry_cannot_change_prior_coverage():
    report = _project_b_row("B09", _default_churn())
    assert gate.evaluate_row("B09", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    before = next(cp for cp in fixture["checkpoints"] if cp["kind"] == "probe_expire_before")
    future = next(t for t in fixture["fixture_provenance"]["api_trace"]
                  if t["classification"] == "retention_expired" and
                  t["id"] != before["identity_samples"][0]["id"])
    assert future["received_mono"] > before["received_mono"]
    future["received_at"] = before["received_at"]
    before["coverage_complete"] = False
    assert gate.evaluate_row("B09", report, attachments={})["status"] != "PASS"


def test_x2_b08_future_finish_backdated_cannot_inflate_frozen_count():
    report = _project_b_row("B08", _default_churn())
    assert gate.evaluate_row("B08", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    before, at = (next(cp for cp in fixture["checkpoints"] if cp["kind"] == kind)
                  for kind in ("probe_close_before", "probe_close_at"))
    item = at["frozen_totals_before_after"][0]
    future = next(t for t in fixture["fixture_provenance"]["api_trace"]
                  if t["action"] == "finish" and t["source"] == item["source"]
                  and t["event_order"] > at["event_order"])
    future["received_at"] = before["received_at"]
    future["received_mono"] = before["received_mono"]
    future["close_at"] = at["received_at"]
    future["close_mono"] = at["received_mono"]
    item["at"]["finished"] += 1
    item["requery"]["finished"] += 1
    assert gate.evaluate_row("B08", report, attachments={})["status"] != "PASS"


def test_x2_b09_future_expiry_backdated_cannot_change_prior_coverage():
    report = _project_b_row("B09", _default_churn())
    assert gate.evaluate_row("B09", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    before = next(cp for cp in fixture["checkpoints"] if cp["kind"] == "probe_expire_before")
    future = next(t for t in fixture["fixture_provenance"]["api_trace"]
                  if t["classification"] == "retention_expired" and
                  t["event_order"] > before["event_order"] and
                  t["id"] != before["identity_samples"][0]["id"])
    future["received_at"] = before["received_at"]
    future["received_mono"] = before["received_mono"]
    before["coverage_complete"] = False
    assert gate.evaluate_row("B09", report, attachments={})["status"] != "PASS"


def test_x2_b12_future_register_backdated_cannot_enter_recent_window():
    report = _project_b_row("B12", _default_churn())
    assert gate.evaluate_row("B12", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    before, at = (next(cp for cp in fixture["checkpoints"] if cp["kind"] == kind)
                  for kind in ("probe_recent_before", "probe_recent_at"))
    trace = fixture["fixture_provenance"]["api_trace"]
    entry = next(e for e in fixture["recent_input_ledger"]
                 if next(t for t in trace if t["action"] == "register" and t["id"] == e["id"])["event_order"]
                 > at["event_order"])
    entry["bucket_start"] = before["received_at"] // MIN * MIN - MIN
    for cp, part in ((before, "before"), (at, "at")):
        for key in (cp["recent_expected_counts"][entry["source"]][part],
                    cp["recent_cohort_counts"][entry["source"]]):
            key["registered"] += 1
            key["connection"][entry["connection"]] = key["connection"].get(entry["connection"], 0) + 1
            key["lifecycle"][entry["lifecycle"]] = key["lifecycle"].get(entry["lifecycle"], 0) + 1
    assert gate.evaluate_row("B12", report, attachments={})["status"] != "PASS"


@pytest.mark.parametrize("row", ("B03", "B08", "B10", "B12", "B15", "B19", "B21"))
def test_x2_b_checkpoint_future_event_cannot_enter_published_state(row):
    if row == "B03":
        fixture = gate.run_churn_fixture("churn_burst_ascii128", limit=128, **SMALL_CHURN)
    elif row == "B19":
        fixture = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128,
                                        measure_transitions=True, **SMALL_CHURN)
    else:
        fixture = _default_churn()
    report = _project_b_row(row, fixture)
    fixture = report["churn_fixtures"][0]
    if row == "B03":
        for cp in fixture["checkpoints"]:
            cp["kind"] = {"tail_before": "tail_80640", "tail_after": "tail_80641"}.get(cp["kind"], cp["kind"])
        report["test_scale"] = {"burst_details": 8, "normal_calls": 201}
    elif row == "B10":
        report["test_scale"] = {"hours": 25, "pre_prune_hours": 3}
    elif row == "B19":
        report["scenarios"] = fixture["transition_measurements"]
    assert gate.evaluate_row(row, report, attachments={})["status"] == "PASS"
    trace = fixture["fixture_provenance"]["api_trace"]
    if row in ("B08", "B12"):
        kind = "probe_close_at" if row == "B08" else "probe_recent_before"
        cp = next(cp for cp in fixture["checkpoints"] if cp["kind"] == kind)
        identity = cp["identity_samples"][0]["id"]
        event = next(t for t in trace if t["action"] == "finish" and t["id"] == identity)
    else:
        event = next(t for t in trace if t["action"] == ("tomb" if row == "B10" else "register"))
        cp = next(cp for cp in fixture["checkpoints"] if cp["event_order"] >= event["event_order"])
    event["received_at"] = cp["received_at"] + 1
    event["received_mono"] = cp["received_mono"] + 1
    assert gate.evaluate_row(row, report, attachments={})["status"] != "PASS"


@pytest.mark.parametrize("auxiliary", (None, 0, "false"))
def test_x2_b_b02_gate_register_requires_boolean_auxiliary(auxiliary):
    report = _project_b_row("B02", _default_churn())
    registers = [t for t in report["churn_fixtures"][0]["fixture_provenance"]["api_trace"]
                 if t["action"] == "register"]
    if auxiliary is None:
        del registers[len(registers) // 2]["auxiliary"]
    else:
        registers[len(registers) // 2]["auxiliary"] = auxiliary
    assert gate.evaluate_row("B02", report, attachments={})["status"] == "UNVERIFIED"


def test_x2_b_b02_gate_requires_integer_auxiliary_count():
    report = _project_b_row("B02", _default_churn())
    report["churn_fixtures"][0]["auxiliary_registered"] = False
    assert gate.evaluate_row("B02", report, attachments={})["status"] == "UNVERIFIED"


def test_x2_b_b03_missing_actual_charge_is_unverified():
    fixture = gate.run_churn_fixture("churn_burst_ascii128", limit=128, **SMALL_CHURN)
    for cp in fixture["checkpoints"]:
        cp["kind"] = {"tail_before": "tail_80640", "tail_after": "tail_80641"}.get(cp["kind"], cp["kind"])
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "test_scale": {"burst_details": 8, "normal_calls": 201}}
    assert gate.evaluate_row("B03", report, attachments={})["status"] == "PASS"
    first_finish = next(t for t in fixture["fixture_provenance"]["api_trace"] if t["action"] == "finish")
    del first_finish["detail_charge_bytes"]
    assert gate.evaluate_row("B03", report, attachments={})["status"] == "UNVERIFIED"


def test_x2_b_b03_rejects_swapped_auxiliary_positions_with_same_count():
    fixture = gate.run_churn_fixture("churn_burst_ascii128", limit=128, **SMALL_CHURN)
    for cp in fixture["checkpoints"]:
        cp["kind"] = {"tail_before": "tail_80640", "tail_after": "tail_80641"}.get(cp["kind"], cp["kind"])
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "test_scale": {"burst_details": 8, "normal_calls": 201}}
    assert gate.evaluate_row("B03", report, attachments={})["status"] == "PASS"
    registers = [t for t in fixture["fixture_provenance"]["api_trace"]
                 if t["action"] == "register"]
    registers[0]["auxiliary"] = False
    registers[9]["auxiliary"] = True
    assert sum(t["auxiliary"] for t in registers) == fixture["auxiliary_registered"]
    assert gate.evaluate_row("B03", report, attachments={})["status"] == "FAIL"


def test_x2_b_b19_independent_transition_measurements_pass():
    fixture = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128,
                                    measure_transitions=True, **SMALL_CHURN)
    report = {"mode": "predicate_unit", "churn_fixtures": [fixture],
              "scenarios": fixture["transition_measurements"]}
    assert gate.evaluate_row("B19", report, attachments={})["status"] == "PASS"
    del report["churn_fixtures"][0]["minute_batches"]
    result = gate.evaluate_row("B19", report, attachments={})
    assert result["status"] == "UNVERIFIED" and "minute_batches" in result["reason"]


def test_x2_b_b20_independent_fault_scenarios_pass():
    scenarios = gate.run_churn_fault_scenarios()
    report = {"mode": "predicate_unit", "scenarios": scenarios,
              "test_scale": {"fault_kinds": [s["fault_evidence"]["fault_kind"] for s in scenarios]}}
    assert gate.evaluate_row("B20", report, attachments={})["status"] == "PASS"


@pytest.mark.parametrize("clock", ("at", "mono"))
def test_gap_b14_checkpoint_last_receipt_matches_last_trace_event(clock):
    report = _project_b_row("B14", _default_churn())
    assert gate.evaluate_row("B14", report, attachments={})["status"] == "PASS"
    checkpoint = report["churn_fixtures"][0]["checkpoints"][0]
    checkpoint["last_received_" + clock] -= 1
    assert gate.evaluate_row("B14", report, attachments={})["status"] == "FAIL"


def test_gap_b14_missing_receipt_event_is_unverified():
    report = _project_b_row("B14", _default_churn())
    del report["churn_fixtures"][0]["checkpoints"][0]["receipt_event"]
    assert gate.evaluate_row("B14", report, attachments={})["status"] == "UNVERIFIED"


@pytest.mark.parametrize("field, donor, recipient", (("connection_counts", "started", "unbound"),
                                                  ("lifecycle_counts", "finalized", "awaiting_report")))
def test_gap_b17_epoch_distribution_matches_trace(field, donor, recipient):
    report = _project_b_row("B17", _default_churn())
    assert gate.evaluate_row("B17", report, attachments={})["status"] == "PASS"
    fixture = report["churn_fixtures"][0]
    projection = next(p for cp in fixture["checkpoints"]
                      for p in cp.get("passive_epoch_projection", [])
                      if p[field][donor])
    public = next(cp for cp in fixture["checkpoints"]
                  if cp["checkpoint_id"] == projection["public_checkpoint_id"])
    entry = next(e for e in public["epoch_sources"] if e["source"] == projection["source"])
    for counts in (projection[field], entry[field]):
        counts[donor] -= 1
        counts[recipient] += 1
    assert gate.evaluate_row("B17", report, attachments={})["status"] == "FAIL"


def test_gap_b17_missing_epoch_bucket_is_unverified():
    report = _project_b_row("B17", _default_churn())
    projection = next(p for cp in report["churn_fixtures"][0]["checkpoints"]
                      for p in cp.get("passive_epoch_projection", []))
    del projection["lifecycle_counts"]["contract_mixed"]
    assert gate.evaluate_row("B17", report, attachments={})["status"] == "UNVERIFIED"


@pytest.mark.parametrize("change", ("extra", "omitted"))
def test_gap_b20_affected_sources_match_fault_trace(change):
    scenarios = gate.run_churn_fault_scenarios()
    report = {"mode": "predicate_unit", "scenarios": scenarios,
              "test_scale": {"fault_kinds": [s["fault_evidence"]["fault_kind"] for s in scenarios]}}
    assert gate.evaluate_row("B20", report, attachments={})["status"] == "PASS"
    kind = "merge_failure" if change == "extra" else "clock_step"
    evidence = next(s["fault_evidence"] for s in scenarios if s["fault_evidence"]["fault_kind"] == kind)
    if change == "extra":
        evidence["affected_sources"].append("unobserved")
    else:
        removed = evidence["affected_sources"].pop()
        evidence["observed_coverage"]["uncertain_sources"].remove(removed)
    assert gate.evaluate_row("B20", report, attachments={})["status"] == "FAIL"


def test_gap_b20_missing_fault_trace_source_is_unverified():
    scenarios = gate.run_churn_fault_scenarios()
    report = {"mode": "predicate_unit", "scenarios": scenarios,
              "test_scale": {"fault_kinds": [s["fault_evidence"]["fault_kind"] for s in scenarios]}}
    del scenarios[0]["fault_evidence"]["fault_trace"][0]["source"]
    assert gate.evaluate_row("B20", report, attachments={})["status"] == "UNVERIFIED"


@pytest.fixture(scope="module")
def _gap_b22_small_report():
    report = gate.run_gate(limit=128, samples=1, warmup=0,
                           churn_names=["churn_most_finished_short_ascii"],
                           progress=io.StringIO(), **SMALL_CHURN)
    proof = report["capacity_proof"]
    assert proof["status"] == "PASS" and proof["checkpoints"]
    dict_steps, set_steps = gate._runtime_budget_steps(128)
    steps = {"at_count": 128, "list_header_bytes": sys.getsizeof([]),
             "list_slot_bytes": sys.getsizeof([None]) - sys.getsizeof([]),
             "block_width": 257, "detail_cap": 128,
             "dict_steps": dict_steps, "set_steps": set_steps}
    steps["computed_q_at_limit"] = gate._actual_budget_q(128, steps)
    proof["budget_q_size_steps"] = steps
    proof["Q_cap_bytes"] = steps["computed_q_at_limit"] + 512 * 128 + 1536 * 12
    proof["F4_ownership"] = {"total_bytes": 18_874_368,
                             "items": [{"path": "fixed", "bytes": 18_874_368, "owner": "F_4"}]}
    report["mode"] = "predicate_unit"
    report["test_scale"] = {"max_records": 128, "job_key_limit": 12}
    assert gate.evaluate_row("B22", report, attachments={})["status"] == "PASS"
    return report


@pytest.mark.parametrize("summary", ("H_res", "Q_obs_bytes"))
def test_gap_b22_capacity_summary_matches_observation_maximum(_gap_b22_small_report, summary):
    report = copy.deepcopy(_gap_b22_small_report)
    proof = report["capacity_proof"]
    assert proof[summary] > 0
    proof[summary] -= 1
    assert gate.evaluate_row("B22", report, attachments={})["status"] == "FAIL"


def test_gap_b22_missing_capacity_observation_is_unverified(_gap_b22_small_report):
    report = copy.deepcopy(_gap_b22_small_report)
    del report["capacity_proof"]["checkpoints"][0]["Q_4_bytes"]
    assert gate.evaluate_row("B22", report, attachments={})["status"] == "UNVERIFIED"


def test_x2_b_b18_does_not_claim_complete_counters_without_transition_trace():
    report = _project_b_row("B18", _default_churn())
    del report["churn_fixtures"][0]["max_resident_observed"]
    assert gate.evaluate_row("B18", report, attachments={})["status"] == "UNVERIFIED"


@pytest.fixture(scope="module")
def _stream_churn(tmp_path_factory):
    root = tmp_path_factory.mktemp("d7_churn_attachments")
    fixture = gate.run_churn_fixture("churn_most_finished_short_ascii", limit=128,
                                    churn_minutes=41, churn_stride_minutes=60,
                                    fixture_detail_divisor=256, measure_transitions=True,
                                    attachment_dir=root)
    refs = [fixture["fixture_provenance"]["api_trace_ref"],
            fixture["tomb_due_ledger_ref"], fixture["recent_input_ledger_ref"],
            fixture["checkpoints"][0]["seq_evidence_ref"]]
    return fixture, {ref["path"]: root / ref["path"] for ref in refs}


def test_full_shape_churn_keeps_detailed_evidence_in_attachments(_stream_churn):
    fixture, attachments = _stream_churn
    assert fixture["status"] == "PASS"
    assert fixture["max_resident_observed"] > 0
    assert "api_trace_ref" in fixture["fixture_provenance"]
    assert "tomb_due_ledger_ref" in fixture and "recent_input_ledger_ref" in fixture
    assert all("seq_evidence_ref" in cp and "retained_seqs" not in cp
               for cp in fixture["checkpoints"])
    for row in ("B10", "B12", "B14", "B15", "B17", "B18", "B19", "B21"):
        report = _project_b_row(row, fixture)
        report["scenarios"] = fixture["transition_measurements"]
        if row == "B10":
            report["test_scale"] = {"hours": 41, "pre_prune_hours": 3}
        assert gate.evaluate_row(row, report, attachments=attachments)["status"] == "PASS", row


@pytest.mark.parametrize("damage", ("missing", "hash", "rows", "sequence"))
def test_streamed_churn_trace_fails_closed(_stream_churn, damage):
    fixture, attachments = _stream_churn
    report = _project_b_row("B18", fixture)
    attachments = dict(attachments)
    ref = report["churn_fixtures"][0]["fixture_provenance"]["api_trace_ref"]
    path = ref["path"]
    if damage == "missing":
        attachments.pop(path)
    elif damage == "hash":
        ref["sha256"] = "0" * 64
    elif damage == "rows":
        ref["rows"] += 1
    else:
        rows = [json.loads(line) for line in gzip.decompress(attachments[path].read_bytes()).splitlines()]
        rows[0]["attachment_seq"] = 1
        blob = gzip.compress(("\n".join(json.dumps(row) for row in rows) + "\n").encode(), mtime=0)
        ref["sha256"] = hashlib.sha256(blob).hexdigest()
        attachments[path] = blob
    assert gate.evaluate_row("B18", report, attachments=attachments)["status"] == "UNVERIFIED"


def test_gate_records_independent_b19_b20_scenarios(tmp_path):
    report = gate.run_gate(limit=128, samples=1, warmup=0,
                           churn_names=["churn_most_finished_short_ascii"],
                           churn_minutes=41, churn_stride_minutes=60,
                           fixture_detail_divisor=256, attachment_dir=tmp_path,
                           progress=io.StringIO())
    names = {row["name"] for row in report["scenarios"]}
    assert {"churn_fault_clock_step", "churn_fault_merge_failure",
            "churn_fault_late_after_expiry", "churn_fault_id_collision",
            "churn_fault_uuid_uniqueness"} <= names
    assert "churn_registered_independent" in names
    assert any(cp["measurement_refs"] for cp in report["churn_fixtures"][0]["checkpoints"])


def test_independent_transition_full_sample_count():
    measured = gate._churn_transition_measurement("registered", samples=1000)
    assert measured["timing"]["gc_disabled"]["n"] == 1000
    gate._observed_timing(measured)
    gate._measured_time_gate(measured, required_n=1000)


def test_independent_faults_are_present_in_full_branch():
    report = {"mode": "full", "scenarios": gate.run_churn_fault_scenarios()}
    assert gate.evaluate_row("B20", report, attachments={})["status"] == "PASS"
