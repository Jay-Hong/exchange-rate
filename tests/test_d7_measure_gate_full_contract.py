"""D7 5a-2 부록 B 계약 — 측정 게이트의 full 표본 설계·판정 (작은 full 경로로 CI 에서 확인).

세부 계약: `design/d7-aggregation/slice5a2_contract_r2_addendumB.md` (Codex 작성, Claude 동의 + B7 인터페이스).
실제 최대 슬롯 성능은 여기서 재지 않는다. 같은 계획·판정 함수가 작은 limit/samples 로도 돌며
관찰한 D·K·분류로 표본을 검사하고, 시간 판정(N/A 와 UNVERIFIED 구분)·overall·부분 수락 계산이 계약대로인지만 잠근다.
계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "d7_ledger_measure_gate.py"

TIME_VALUES = {"PASS", "FAIL", "UNVERIFIED", "N/A"}
PLANS = {"repeat", "sequential", "sequential_reprepared", "independent", "independent_split_copy", "full_deepcopy"}
ROW_FIELDS = {"name", "status", "visit_gate", "temporary_gate", "time_gate", "sample_plan", "samples", "sample_checks",
              "D_observed", "K_observed", "sample_exception", "prepare_seconds", "timed_calls"}
SMALL = dict(limit=128, samples=3, warmup=1)

# 부록 B4 (a)~(g) 와 B1 표의 행 이름(B8 — 시험이 고정한다)
ADDED_ROWS = {
    "stale_overdue_end_cursor": ((0, 0), (0, 0)),                   # (a)
    "stale_overdue_requery": ((0, 0), (0, 0)),                      # (a) 같은 시각 재조회(B4 재대조 보강)
    "multi_bucket_close": (None, (0, 0)),                           # (b) 정상
    "multi_bucket_close_merge_failure": (None, (0, 0)),             # (b) 실패 주입
    "multi_bucket_close_retry": (None, (0, 0)),                     # (b) 재시도
    "reverse_cohort_register_tail": ((0, 0), (0, 0)),               # (c)
    "reverse_cohort_empty": ((0, 0), (0, 0)),                       # (c)
    "reverse_cohort_narrow": ((0, 0), None),                        # (c) K 1..16
    "identity_absent_link": ((0, 0), (0, 0)),                       # (d)
    "identity_absent_finish": ((0, 0), (0, 0)),                     # (d)
    "missing_record_direct": ((0, 0), (0, 0)),                      # (d)
    "missing_record_first_query_cohort": ((0, 0), (0, 0)),          # (d)
    "missing_record_first_query_contributions": ((0, 0), (0, 0)),   # (d)
    "missing_record_first_query_aggregation": ((0, 0), (0, 0)),     # (d)
    "open_end_cursor": ((0, 0), (0, 0)),                            # (e) 마지막 seq 뒤
    "open_last_seq_cursor": ((0, 0), (0, 0)),                       # (e) 마지막 열린 seq 자체(B4(e) 보강)
    "recent_window_seq_order": ((0, 0), None),                      # (e) 등록 seq 와 bucket 순서 역전(B4(e) 보강)
    "recent_window_boundary": ((0, 0), None),                       # (e) K 실제값
    "close_wait_target_advance": ((0, 0), (0, 0)),                  # (f)
    "recent_outside_open": ((0, 0), (0, 0)),                        # (g)
    "recent_outside_open_one_inside": ((0, 0), (1, 1)),             # (g)
}


def _load_gate():
    spec = importlib.util.spec_from_file_location("d7_gate_full_under_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


class SpyClock:
    def __init__(self):
        self.calls = 0
        self.now = 0

    def __call__(self):
        self.calls += 1
        self.now += 1000
        return self.now


@pytest.fixture(scope="module")
def small(gate):
    spy = SpyClock()
    progress = io.StringIO()
    report = gate.run_gate(quick=False, budget_seconds=3600, clock=spy, progress=progress, **SMALL)
    return report, spy, progress.getvalue()


def rows_by_name(report):
    return {row["name"]: row for row in report["scenarios"]}


# ───────── B3 시간 판정 (순수 함수) ─────────

@pytest.mark.parametrize("d,k,p99,mx,n,full_cohort,expected", [
    (0, 0, 20_000, 100_000, 1000, False, "PASS"),
    (0, 0, 20_001, 100_000, 1000, False, "FAIL"),
    (0, 0, 20_000, 100_001, 1000, False, "FAIL"),
    (1, 0, 250_000, 1_000_000, 1000, False, "PASS"),
    (0, 2048, 250_000, 1_000_000, 1000, False, "PASS"),
    (0, 2048, 250_001, 1_000_000, 1000, False, "FAIL"),
    (2048, 1, 9_999_999, 99_999_999, 1000, False, "N/A"),
    (131072, 0, 9_999_999, 99_999_999, 100, False, "N/A"),
    (0, 131072, 2_000_000, 5_000_000, 1000, True, "PASS"),
    (0, 131072, 2_000_001, 5_000_000, 1000, True, "FAIL"),
    (0, 0, 1, 1, 999, False, "UNVERIFIED"),                 # 표본 부족
    (131072, 0, 1, 1, 99, False, "UNVERIFIED"),              # 예외 행도 100 미만이면 미검증
])
def test_time_verdict_table(gate, d, k, p99, mx, n, full_cohort, expected):
    dist = {"n": n, "p99_us": p99, "max_us": mx}
    required = 100 if d + k > 2048 and not full_cohort else 1000
    assert gate.time_verdict(d=d, k=k, gc_disabled=dist, required_n=required, full_cohort=full_cohort) == expected


def test_time_verdict_without_distribution_is_unverified_not_na(gate):
    assert gate.time_verdict(d=131072, k=0, gc_disabled=None, required_n=100, full_cohort=False) == "UNVERIFIED"
    assert gate.time_verdict(d=0, k=0, gc_disabled=None, required_n=1000, full_cohort=False) == "UNVERIFIED"


@pytest.mark.parametrize("visit,temp,time_,correct,expected", [
    ("PASS", "PASS", "N/A", True, "PASS"),
    ("FAIL", "PASS", "N/A", True, "FAIL"),
    ("PASS", "UNVERIFIED", "N/A", True, "UNVERIFIED"),
    ("PASS", "PASS", "N/A", False, "FAIL"),
])
def test_scenario_status_accepts_na(gate, visit, temp, time_, correct, expected):
    assert gate.scenario_status(visit_gate=visit, temp_gate=temp, time_gate=time_, correct=correct) == expected


# ───────── B3 overall 과 부록 A2 부분 수락 (순수 함수) ─────────

def _row(name, status="PASS", visit="PASS", temp="PASS", time_="PASS"):
    return {"name": name, "status": status, "visit_gate": visit, "temporary_gate": temp, "time_gate": time_}


def _resident(name, status):
    return {"name": name, "status": status, "visit_gate": "not_applicable", "temporary_gate": "not_applicable",
            "time_gate": "not_applicable", "owned_graph": {"bytes": 1}}


def test_overall_and_partial_acceptance_with_resident_fail(gate):
    rows = [_row("a"), _row("b", time_="N/A"), _resident("resident_unfinished", "FAIL"),
            {"name": "record_baseline", "status": "baseline"}]
    assert gate.overall_status(rows, adapter_status="PASS", mode="full") == "FAIL"
    pa = gate.partial_acceptance(rows, adapter_status="PASS", mode="full")
    assert pa == {"eligible": True, "failing_rows": [], "unverified_rows": []}


def test_partial_acceptance_blocked_by_measured_row(gate):
    rows = [_row("a"), _row("slow", status="FAIL", time_="FAIL"), _row("unsure", status="UNVERIFIED", time_="UNVERIFIED"),
            _resident("resident_unfinished", "FAIL")]
    pa = gate.partial_acceptance(rows, adapter_status="PASS", mode="full")
    assert pa == {"eligible": False, "failing_rows": ["slow"], "unverified_rows": ["unsure"]}
    assert gate.overall_status(rows, adapter_status="PASS", mode="full") == "FAIL"


def test_overall_unverified_without_fail(gate):
    rows = [_row("a"), _row("b", status="UNVERIFIED", time_="UNVERIFIED"), {"name": "record_baseline", "status": "baseline"}]
    assert gate.overall_status(rows, adapter_status="PASS", mode="full") == "UNVERIFIED"
    assert gate.overall_status([_row("a")], adapter_status="UNVERIFIED", mode="full") == "UNVERIFIED"
    assert gate.overall_status([_row("a")], adapter_status="FAIL", mode="full") == "FAIL"
    assert gate.overall_status([_row("a")], adapter_status="PASS", mode="full") == "PASS"


@pytest.mark.parametrize("mode", ["quick", "full_small"])
def test_non_full_mode_never_passes(gate, mode):
    rows = [_row("a")]
    assert gate.overall_status(rows, adapter_status="PASS", mode=mode) == "UNVERIFIED"
    assert gate.partial_acceptance(rows, adapter_status="PASS", mode=mode)["eligible"] is False
    assert gate.overall_status([_row("a", status="FAIL")], adapter_status="PASS", mode=mode) == "FAIL"


def test_partial_acceptance_requires_adapter(gate):
    rows = [_row("a")]
    assert gate.partial_acceptance(rows, adapter_status="FAIL", mode="full")["eligible"] is False
    assert gate.partial_acceptance(rows, adapter_status="UNVERIFIED", mode="full")["eligible"] is False


# ───────── B7 작은 full 경로 전체 실행 ─────────

def test_small_full_report_shape(small):
    report, _, _ = small
    assert report["mode"] == "full_small"
    assert report["complete"] is True and report["aborted_reason"] is None
    assert report["overall"] != "PASS"
    assert report["partial_acceptance"]["eligible"] is False
    for key in ("summary", "scenarios", "limits", "environment", "total_elapsed_seconds"):
        assert key in report, key
    for row in report["scenarios"]:
        if row["name"] == "record_baseline" or row["name"].startswith("resident_"):
            continue
        assert ROW_FIELDS <= set(row), (row["name"], ROW_FIELDS - set(row))
        assert row["time_gate"] in TIME_VALUES, row["name"]
        assert row["sample_plan"] in PLANS, row["name"]
        assert row["sample_checks"]["failures"] == [], (row["name"], row["sample_checks"])
        assert row["sample_checks"]["checked"] >= 1, row["name"]


def test_small_full_rows_cover_one_shot_and_added_scenarios(small):
    report, _, _ = small
    rows = rows_by_name(report)
    for name in ("close_boundary_before", "close_boundary_exact", "large_clock_jump", "mass_close_boundary", "mass_overdue"):
        assert name in rows, name
    missing = set(ADDED_ROWS) - set(rows)
    assert not missing, sorted(missing)


def test_small_full_observed_d_k(small):
    report, _, _ = small
    rows = rows_by_name(report)
    limit = SMALL["limit"]
    expect = {
        "close_boundary_before": ((0, 0), (0, 0)),
        "close_boundary_exact": ((1, 1), None),
        "large_clock_jump": ((1, 1), (0, 0)),
        "mass_close_boundary": ((min(2048, limit), min(2048, limit)), (0, 0)),
        "mass_overdue": ((limit, limit), (0, 0)),
        **ADDED_ROWS,
    }
    for name, (d, k) in expect.items():
        row = rows[name]
        if d is not None:
            assert tuple(row["D_observed"]) == d, (name, row["D_observed"])
        if k is not None:
            assert tuple(row["K_observed"]) == k, (name, row["K_observed"])
    lo, hi = rows["reverse_cohort_narrow"]["K_observed"]
    assert 1 <= lo <= hi <= 16
    d_lo, d_hi = rows["multi_bucket_close"]["D_observed"]
    assert d_lo == d_hi == min(2048, limit)


def test_small_full_sample_plans(small):
    report, _, _ = small
    rows = rows_by_name(report)
    assert rows["close_boundary_before"]["sample_plan"] == "repeat"
    assert rows["close_boundary_exact"]["sample_plan"] in {"sequential", "sequential_reprepared"}
    for name in ("large_clock_jump", "mass_close_boundary", "mass_overdue"):
        assert rows[name]["sample_plan"] in {"independent", "independent_split_copy", "full_deepcopy"}, name
    for row in report["scenarios"]:
        if row.get("sample_plan") == "independent_split_copy":
            assert row["copy_equivalence"] == {"checked": True, "equal": True, "original_unchanged": True}, row["name"]


def test_small_full_sample_exception_only_on_mass_overdue(small):
    report, _, _ = small
    for row in report["scenarios"]:
        if row["name"] == "mass_overdue":
            continue
        assert row.get("sample_exception") is None, row["name"]


def test_clock_is_used_only_around_timed_calls(small):
    report, spy, _ = small
    timed = sum(row["timed_calls"] for row in report["scenarios"] if isinstance(row.get("timed_calls"), int))
    assert timed > 0
    assert spy.calls == 2 * timed


def test_progress_lines_bracket_every_row(small):
    report, _, text = small
    lines = text.splitlines()
    starts = [l.split()[1] for l in lines if l.startswith("start ")]
    ends = [l.split()[1] for l in lines if l.startswith("end ")]
    names = [row["name"] for row in report["scenarios"]]
    assert set(names) <= set(starts) and set(names) <= set(ends)
    for name in names:
        assert lines.index(next(l for l in lines if l.startswith(f"start {name}"))) < \
            lines.index(next(l for l in lines if l.startswith(f"end {name}"))), name


def test_budget_exhaustion_reports_incomplete(gate):
    report = gate.run_gate(quick=False, budget_seconds=0, progress=io.StringIO(), **SMALL)
    assert report["complete"] is False and report["aborted_reason"] == "budget"
    assert report["overall"] in {"UNVERIFIED", "FAIL"}
    assert report["partial_acceptance"]["eligible"] is False
    unfinished_pass = [row["name"] for row in report["scenarios"]
                       if row.get("status") == "PASS" and row.get("sample_checks", {}).get("checked", 0) == 0]
    assert unfinished_pass == []                                   # 돌지 않은 행을 PASS 로 두지 않는다


def test_gate_observes_d_from_ledger_not_declaration(gate, monkeypatch):
    """닫힘이 일어나지 않는 ledger 로 돌리면 exact 행은 관찰 D=0 으로 표본 실패가 되어야 한다."""
    base = gate.RoundLedger

    class NoClose(base):
        def _advance_and_close(self, wall, mono):
            self._health["last_received_at"] = wall
            self._health["last_received_mono"] = mono

    NoClose.__module__ = base.__module__
    monkeypatch.setattr(gate, "RoundLedger", NoClose)
    report = gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(), **SMALL)
    row = rows_by_name(report)["close_boundary_exact"]
    assert row["status"] == "FAIL"
    assert row["sample_checks"]["failures"], row["sample_checks"]
    assert row["D_observed"][0] == 0
    assert report["overall"] == "FAIL"


# ───────── B7 CLI: stdout 은 JSON 하나 ─────────

def test_cli_small_full_writes_single_json_object():
    done = subprocess.run([sys.executable, str(GATE_PATH), "--limit", "64", "--samples", "2", "--warmup", "1"],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=600,
                          env={"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout)
    assert isinstance(report, dict) and report["mode"] == "full_small"
    assert any(l.startswith("start ") for l in done.stderr.splitlines())


# ───────── 변이 배터리 생존 보강(시험 잠금 뒤 추가 — Codex 재승인 대상) ─────────

def test_partial_acceptance_rejects_unverified_time_even_if_status_pass(gate):
    """G3: 행 상태가 PASS 로 적혀 있어도 시간 게이트가 UNVERIFIED 면 부분 수락 대상이 아니다."""
    rows = [_row("a"), _row("odd", status="PASS", time_="UNVERIFIED")]
    pa = gate.partial_acceptance(rows, adapter_status="PASS", mode="full")
    assert pa["eligible"] is False and pa["unverified_rows"] == ["odd"]


def test_gate_checks_k_against_ledger_output(gate, monkeypatch):
    """G5: 조회가 결과를 덜 돌려주면 관찰 K 가 달라져 그 행은 표본 실패다."""
    base = gate.RoundLedger

    class DropEntries(base):
        def contributions_open(self, **kw):
            result = super().contributions_open(**kw)
            if result.get("classification") == "snapshot" and result["entries"]:
                result = dict(result, entries=result["entries"][:1])
            return result

    DropEntries.__module__ = base.__module__
    monkeypatch.setattr(gate, "RoundLedger", DropEntries)
    report = gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(), **SMALL)
    row = rows_by_name(report)["contributions_first_page"]
    assert row["status"] == "FAIL"
    assert any("K:" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


def test_gate_checks_classification(gate, monkeypatch):
    """G9: 분류가 기대와 다르면 표본 실패다."""
    base = gate.RoundLedger

    class OddLink(base):
        def link_round(self, **kw):
            result = super().link_round(**kw)
            return dict(result, classification="odd") if result.get("classification") == "linked" else result

    OddLink.__module__ = base.__module__
    monkeypatch.setattr(gate, "RoundLedger", OddLink)
    report = gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(), **SMALL)
    row = rows_by_name(report)["link_round_accept"]
    assert row["status"] == "FAIL"
    assert any("classification:" in f["reason"] for f in row["sample_checks"]["failures"])


def test_split_copy_that_later_mutates_original_is_caught(gate, monkeypatch):
    """G6: 첫 증명은 통과하고 뒤 표본에서 원본을 건드리는 분리 복제는 표본 실패로 드러나야 한다."""
    real = gate._split_clone
    calls = {"n": 0}

    def flaky(base, mutable_ids):
        calls["n"] += 1
        return real(base, mutable_ids) if calls["n"] == 1 else base

    monkeypatch.setattr(gate, "_split_clone", flaky)
    report = gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(), **SMALL)
    caught = [row["name"] for row in report["scenarios"]
              if any("mutated" in f["reason"] for f in row.get("sample_checks", {}).get("failures", []))]
    assert caught, "원본 변이가 어느 행에서도 드러나지 않았다"
    for name in caught:
        assert rows_by_name(report)[name]["status"] == "FAIL", name


class FakeWall:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


class MarkProgress(io.StringIO):
    def __init__(self, wall):
        super().__init__()
        self.wall = wall
        self.first_start = None

    def write(self, text):
        if self.first_start is None and text.startswith("start "):
            self.first_start = self.wall.t
        return super().write(text)


def test_budget_stops_inside_a_row(gate):
    """G7: 예산은 행 사이만이 아니라 행 안의 표본 경계에서도 확인한다."""
    probe_wall = FakeWall()
    probe = MarkProgress(probe_wall)
    gate.run_gate(quick=False, budget_seconds=10 ** 9, wall=probe_wall, progress=probe, **SMALL)
    assert probe.first_start is not None
    wall = FakeWall()
    report = gate.run_gate(quick=False, budget_seconds=probe.first_start + 4, wall=wall,
                           progress=io.StringIO(), **SMALL)
    assert report["complete"] is False and report["aborted_reason"] == "budget"
    planned = 2 + SMALL["warmup"] + 2 * SMALL["samples"]
    started = [row for row in report["scenarios"] if row.get("sample_checks", {}).get("checked", 0) > 0]
    assert started, "첫 행이 시작되지 않았다(시험 설계 확인 필요)"
    assert started[0]["sample_checks"]["checked"] < planned, started[0]["sample_checks"]
    assert started[0]["status"] != "PASS"


@pytest.mark.parametrize("container", ["_owned_ids", "_previous_job", "_seq_tail", "_records_add"])
def test_split_copy_shared_container_mutation_caught_on_large_ledger(gate, monkeypatch, container):
    """분리 복제가 공유하는 컨테이너(원본과 같은 객체)에 뒤 표본이 추가해도, 1,024 슬롯 초과 ledger 의 원본 불변 검사가 잡아야 한다."""
    real = gate._split_clone
    calls = {"n": 0}

    def sneaky(base, mutable_ids):
        calls["n"] += 1
        clone = real(base, mutable_ids)
        if calls["n"] == 3:
            if container == "_owned_ids":
                base._owned_ids.add("ghost-owner")
            elif container == "_previous_job":
                base._previous_job[("ghost", "job")] = 1
            elif container == "_seq_tail":
                base._seq.append(base._seq[-1])          # 길이만 늘고 _records 는 그대로
            else:
                base._records["ghost-record"] = dict(next(iter(base._records.values())))   # 원본 Record 표에 직접 추가(G14)
        return clone

    monkeypatch.setattr(gate, "_split_clone", sneaky)
    report = gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(), limit=1030, samples=1, warmup=0,
                           rows=["large_clock_jump"])               # B7 보충: rows 로 실행 행을 고른다(작은 경로 전용)
    assert [row["name"] for row in report["scenarios"] if row.get("sample_checks", {}).get("checked", 0)] == ["large_clock_jump"]
    assert report["mode"] == "full_small" and report["partial_acceptance"]["eligible"] is False
    caught = [row["name"] for row in report["scenarios"]
              if any("mutated" in f["reason"] for f in row.get("sample_checks", {}).get("failures", []))]
    assert caught, container



# ───────── B4(e) 보강(커밋 검토 REVISE 반영 — Codex 재승인 대상) ─────────

def test_seq_order_row_reverses_seq_and_bucket_order_with_distinct_reasons(small):
    """recent_window_seq_order 는 seq 가 늘수록 이른 버킷이고, 동적 사유 키가 둘 이상이어야 순서 규칙을 건드린다."""
    report, _, _ = small
    row = rows_by_name(report)["recent_window_seq_order"]
    assert row["sample_checks"]["failures"] == []
    lo, hi = row["K_observed"]
    assert lo >= 2 and hi >= 2
    assert row.get("order_probe", {}).get("distinct_reasons", 0) >= 2, row.get("order_probe")
    assert row["order_probe"].get("seq_bucket_reversed") is True, row.get("order_probe")


def test_gate_checks_recent_key_order(gate, monkeypatch):
    """최근 창 동적 키 순서를 뒤집는 ledger 는 recent_window_seq_order 표본 실패여야 한다."""
    base = gate.RoundLedger

    class ReversedKeys(base):
        def aggregation_snapshot(self, **kw):
            result = super().aggregation_snapshot(**kw)
            if result.get("classification") == "snapshot":
                for r in result["recent"]:
                    reasons = r["collection_unknown_reasons"]
                    r["collection_unknown_reasons"] = dict(reversed(list(reasons.items())))
            return result

    ReversedKeys.__module__ = base.__module__
    monkeypatch.setattr(gate, "RoundLedger", ReversedKeys)
    report = gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(), rows=["recent_window_seq_order"],
                           **SMALL)
    row = rows_by_name(report)["recent_window_seq_order"]
    assert row["status"] == "FAIL"
    assert any("order" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


def test_open_last_seq_cursor_uses_last_open_seq(small):
    report, _, _ = small
    row = rows_by_name(report)["open_last_seq_cursor"]
    first = row["first_call"]
    assert first["classification"] == "snapshot"
    assert row.get("cursor", {}).get("after_seq") == row.get("cursor", {}).get("last_open_seq"), row.get("cursor")
    assert row["cursor"]["last_open_seq"] < SMALL["limit"]



# ───────── B4 재대조 보강 (a)(c)(d)(e)(g)(f) — Codex 가 찾은 측정 공백, Codex 재승인 대상 ─────────

def _run_rows(gate, monkeypatch, cls, rows):
    cls.__module__ = gate.RoundLedger.__module__
    monkeypatch.setattr(gate, "RoundLedger", cls)
    return rows_by_name(gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(), rows=rows, **SMALL))


def test_boundary_k_expectations(small):
    """(e) 경계 K 를 시험이 직접 고정한다: T 버킷 limit-1 건 + T+60분 버킷 1건 fixture."""
    report, _, _ = small
    rows = rows_by_name(report)
    n = min(2048, SMALL["limit"])
    assert tuple(rows["recent_window_boundary"]["K_observed"]) == (n - 1, n - 1)
    assert tuple(rows["recent_window_before_exclusion"]["K_observed"]) == (n - 1, n - 1)
    assert tuple(rows["recent_window_after_exclusion"]["K_observed"]) == (1, 1)


def test_requery_row_is_measured(small):
    """(a) 같은 시각 재조회는 방문·임시량·시간을 따로 잰 행이다."""
    report, _, _ = small
    row = rows_by_name(report)["stale_overdue_requery"]
    assert row["sample_plan"] in {"repeat", "independent", "independent_split_copy", "full_deepcopy"}
    assert row["visit_gate"] in {"PASS", "FAIL"} and row["temporary_gate"] in {"PASS", "FAIL"}
    assert row["samples"]["gc_disabled"] >= SMALL["samples"]


def test_narrow_cohort_out_of_range_reads_are_caught(gate, monkeypatch):
    """(c) 방문 수가 상한 안이어도 범위 밖 Record 를 읽으면 실패다(방문 ID 추적)."""
    base = gate.RoundLedger

    class Peek(base):
        def cohort_snapshot(self, **kw):
            for invocation_id in list(dict.keys(self._records))[:3]:
                self._records[invocation_id]                            # 범위 밖 3건 읽기
            return super().cohort_snapshot(**kw)

    rows = _run_rows(gate, monkeypatch, Peek, ["reverse_cohort_narrow"])
    row = rows["reverse_cohort_narrow"]
    assert row["status"] == "FAIL"
    assert row.get("out_of_range_visits", 0) > 0, row.get("out_of_range_visits")


def test_narrow_cohort_reports_zero_out_of_range(small):
    report, _, _ = small
    rows = rows_by_name(report)
    for name in ("reverse_cohort_narrow", "reverse_cohort_empty"):
        assert rows[name]["out_of_range_visits"] == 0, name


def test_identity_rows_check_b_global_diagnostics(gate, monkeypatch):
    """(d) 삭제 주입 뒤 첫 latch 는 index_error 뿐 아니라 B·global 진단이어야 한다."""
    base = gate.RoundLedger

    class LocalDiag(base):
        def contributions_open(self, **kw):
            result = super().contributions_open(**kw)
            if result.get("classification") == "post_close_unverified":
                result = dict(result, diagnostics=dict(result["diagnostics"], uncertain_pairs=["usd-krw"]))
            return result

    rows = _run_rows(gate, monkeypatch, LocalDiag, ["missing_record_first_query_contributions"])
    row = rows["missing_record_first_query_contributions"]
    assert row["status"] == "FAIL"
    assert any("diagnostic" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


@pytest.mark.parametrize("name", ["recent_window_boundary", "recent_outside_open_one_inside"])
def test_recent_k_cross_checked_with_returned_aggregate(gate, monkeypatch, name):
    """(e)(g) 내부 버킷으로 센 K 와 반환 집계(recent_rounds 의 rounds 합)가 다르면 실패다."""
    base = gate.RoundLedger

    class DropOne(base):
        def aggregation_snapshot(self, **kw):
            result = super().aggregation_snapshot(**kw)
            if result.get("classification") == "snapshot":
                for r in result["recent_rounds"]:
                    if r["rounds"]:
                        r["rounds"] -= 1
                        break
            return result

    rows = _run_rows(gate, monkeypatch, DropOne, [name])
    row = rows[name]
    assert row["status"] == "FAIL"
    assert any("aggregate" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


def test_close_wait_candidates_identity_is_checked(gate, monkeypatch):
    """(f) 빈 전진 호출 전후 닫힘 대기 후보 집합과 보유 상세가 그대로여야 한다(개수만이 아니라 동일성)."""
    base = gate.RoundLedger

    class Swap(base):
        def aggregation_snapshot(self, **kw):
            result = super().aggregation_snapshot(**kw)
            due = list(self._close_index.due(2 ** 62))
            if len(due) >= 2:                                           # 개수는 그대로 두고 후보 하나를 바꾼다
                self._close_index.set(due[0], None)
                for seq in range(1, len(self._seq) + 1):
                    if seq not in due:
                        self._close_index.set(seq, 1)
                        break
            return result

    rows = _run_rows(gate, monkeypatch, Swap, ["close_wait_target_advance"])
    row = rows["close_wait_target_advance"]
    assert row["status"] == "FAIL"
    assert any("candidate" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


# ───────── B4 마지막 보강 (c) 모든 source · (f) 상세 내용 · (g) 상세 수 — Codex 재승인 대상 ─────────

def test_cohort_rows_cover_every_source(small):
    """(c) '소스별' 빈·좁은 cohort 는 등록된 모든 source 에서 잰다."""
    from app.d7_round_axes import REGISTRY
    report, _, _ = small
    rows = rows_by_name(report)
    for name in ("reverse_cohort_narrow", "reverse_cohort_empty"):
        assert rows[name]["cohort_sources"] == list(REGISTRY), (name, rows[name].get("cohort_sources"))
        assert rows[name]["out_of_range_visits"] == 0


def test_close_wait_retained_detail_content_is_checked(gate, monkeypatch):
    """(f) 개수·ID 는 그대로 두고 보유 상세 내용만 바꿔도 실패다."""
    base = gate.RoundLedger

    class Tamper(base):
        def aggregation_snapshot(self, **kw):
            result = super().aggregation_snapshot(**kw)
            for rec in dict.values(self._records):
                if rec["detail"] is not None:
                    rec["detail"]["diagnostics"]["telemetry_error"] = not rec["detail"]["diagnostics"]["telemetry_error"]
                    break
            return result

    rows = _run_rows(gate, monkeypatch, Tamper, ["close_wait_target_advance"])
    row = rows["close_wait_target_advance"]
    assert row["status"] == "FAIL"
    assert any("detail" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


def test_recent_outside_open_detail_count_is_checked(gate, monkeypatch):
    """(g) 호출 뒤 보유 상세 수가 기대와 다르면 실패다."""
    base = gate.RoundLedger

    class DropDetail(base):
        def aggregation_snapshot(self, **kw):
            result = super().aggregation_snapshot(**kw)
            for rec in dict.values(self._records):
                if rec["detail"] is not None:
                    rec["detail"] = None
                    self._health["retained_details"] -= 1
                    break
            return result

    rows = _run_rows(gate, monkeypatch, DropDetail, ["recent_outside_open"])
    row = rows["recent_outside_open"]
    assert row["status"] == "FAIL"
    assert any("detail" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


# ───────── 커밋 검토 2회차 REVISE 반영 (b) 재조회 누적 · (f) cumulative_end 전진 · (g) 두 번째 변형 — Codex 재승인 대상 ─────────

@pytest.mark.parametrize("name", ["multi_bucket_close_requery", "multi_bucket_close_retry_requery"])
def test_requery_does_not_add_to_cumulative(gate, monkeypatch, name):
    """(b) 같은 시각 재조회가 누적 회차를 다시 더하면 실패다(누적 정확히 한 번)."""
    base = gate.RoundLedger

    class DoubleCount(base):
        def aggregation_snapshot(self, **kw):
            result = super().aggregation_snapshot(**kw)
            first = next(iter(self._cumulative_rounds))
            self._cumulative_rounds[first]["rounds"] += 1
            return result

    rows = _run_rows(gate, monkeypatch, DoubleCount, [name])
    row = rows[name]
    assert row["status"] == "FAIL"
    assert any("cumulative" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


def test_close_wait_requires_cumulative_end_advance(gate, monkeypatch):
    """(f) 빈 전진 호출은 cumulative_end 를 목표 경계로 옮겨야 한다."""
    base = gate.RoundLedger

    class NoAdvance(base):
        def aggregation_snapshot(self, **kw):
            before = self._cumulative_end
            result = super().aggregation_snapshot(**kw)
            self._cumulative_end = before
            return result

    rows = _run_rows(gate, monkeypatch, NoAdvance, ["close_wait_target_advance"])
    row = rows["close_wait_target_advance"]
    assert row["status"] == "FAIL"
    assert any("cumulative_end" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


def test_recent_outside_one_inside_detail_count_is_checked(gate, monkeypatch):
    """(g) 두 번째 변형(한 건만 창 안)도 호출 뒤 보유 상세 수를 확인한다."""
    base = gate.RoundLedger

    class DropDetail(base):
        def aggregation_snapshot(self, **kw):
            result = super().aggregation_snapshot(**kw)
            for rec in dict.values(self._records):
                if rec["detail"] is not None:
                    rec["detail"] = None
                    self._health["retained_details"] -= 1
                    break
            return result

    rows = _run_rows(gate, monkeypatch, DropDetail, ["recent_outside_open_one_inside"])
    row = rows["recent_outside_open_one_inside"]
    assert row["status"] == "FAIL"
    assert any("detail" in f["reason"] for f in row["sample_checks"]["failures"]), row["sample_checks"]


# ───────── full 1회차(87f93a0) 결과 반영 — finish 재준비 · 임시량 분해 (Codex 재승인 대상) ─────────

def test_finish_rows_reprepare_before_detail_cap(gate):
    """full 1회차에서 finish 두 행은 2,048 상세 한도를 넘는 종료 54건이 격리돼 실패했다. 한도 전에 재준비해야 한다."""
    small_cap = dict(limit=128, samples=70, warmup=1)            # 2 + 1 + 140 = 143 > 상세 한도 128
    report = gate.run_gate(quick=False, budget_seconds=3600, progress=io.StringIO(),
                           rows=["finish_accept", "finish_near_valid_limit"], **small_cap)
    rows = rows_by_name(report)
    for name in ("finish_accept", "finish_near_valid_limit"):
        row = rows[name]
        assert row["sample_plan"] == "sequential_reprepared", (name, row["sample_plan"])
        assert row["sample_checks"]["failures"] == [], (name, row["sample_checks"]["failures"][:3])
        assert row["sample_checks"]["checked"] == 2 + 1 + 2 * 70


class _Persist(list):
    pass


def test_temporary_breakdown_separates_persistent_growth(gate, monkeypatch):
    """임시량 FAIL 행은 호출 뒤 남은 증가분과 순수 임시분을 나눠 보고한다(판정식은 그대로 peak-current_before)."""
    base = gate.RoundLedger

    class Keeps(base):
        def aggregation_snapshot(self, **kw):
            self.__dict__.setdefault("_probe_keep", _Persist()).append(bytearray(400_000))
            return super().aggregation_snapshot(**kw)

    rows = _run_rows(gate, monkeypatch, Keeps, ["aggregation_empty"])
    row = rows["aggregation_empty"]
    assert row["temporary_gate"] == "FAIL"
    b = row["temporary_breakdown"]
    assert b["peak_delta"] >= 400_000 and b["current_after_delta"] >= 400_000


def test_temporary_breakdown_marks_transient_peak(gate, monkeypatch):
    base = gate.RoundLedger

    class Spikes(base):
        def aggregation_snapshot(self, **kw):
            scratch = bytearray(400_000)
            del scratch
            return super().aggregation_snapshot(**kw)

    rows = _run_rows(gate, monkeypatch, Spikes, ["aggregation_empty"])
    row = rows["aggregation_empty"]
    assert row["temporary_gate"] == "FAIL"
    b = row["temporary_breakdown"]
    assert b["peak_delta"] >= 400_000 and b["current_after_delta"] < 100_000
