"""D7 5a-3b 계약 — 필드별 상주 선불(E = F + Q(N) + D + Σ(A+R)), 등록·상세 선검사, 첫 거절 원자성(운영 경로 무접촉).

세부 계약: `design/d7-aggregation/slice5a3b_v2/slice5a3b_contract_r2.md` (Codex 작성, Claude 검토 반영·합의) — 근거 계약은
`slice5a3_contract_r3.md`. 이 시험이 고정하는 공개 이름:
  - `RoundLedger.budget_state()` → {"F","Q","D","AR","E","B","N"} (E = F+Q+D+AR, B 기본 62,914,560)
  - `RoundLedger.record_charge(invocation_id)` → {"A","R"} (없으면 None)
  - 생성자 `limits={"max_resident_bytes": …}` 로 B 를 줄여 작은 규모에서 byte 선중단을 재현한다(기본 62,914,560).
실측 상주 G 는 측정 게이트의 owned_graph 로 잰다. 계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg
from tests.test_d7_ledger_index_contract import MIN, OVERDUE, S1, hm, mono, summary

B_DEFAULT = 62_914_560
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("d7_gate_for_budget", ROOT / "scripts" / "d7_ledger_measure_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class L:
    """한 ledger 와 비감소 수신 시계, 전이마다 기존 Record 의 A+R 비증가를 검사한다."""

    def __init__(self, **limits):
        self.origin = hm(9, 59)
        self.ld = lg.RoundLedger("E1", aggregation_started_at=self.origin, limits=limits or None)
        self.now = self.origin
        self.charges = {}

    def _rx(self, at):
        at = self.now if at is None else at
        assert at >= self.now
        self.now = at
        return {"received_at": at, "received_mono": mono(at)}

    def _check(self):
        state = self.ld.budget_state()
        # r3 §6 budget 계상: tombstone TR 를 새 항으로 포함한다.
        assert state["E"] == state["F"] + state["Q"] + state["D"] + state["AR"] + state["TR"], state
        assert state["E"] <= state["B"], state
        recomputed = sum(sum(self.ld.record_charge(inv).values()) for inv in dict.keys(self.ld._records))
        assert state["AR"] == recomputed, (state["AR"], recomputed)       # 증분 합계가 Record 별 재계산과 어긋나지 않는다(구현 검토 보강 — Codex 재승인 대상)
        for inv, before in list(self.charges.items()):
            now = self.ld.record_charge(inv)
            if now is None:
                continue
            assert now["A"] + now["R"] <= before, (inv, before, now)
            self.charges[inv] = now["A"] + now["R"]
        for inv in dict.keys(self.ld._records):
            if inv not in self.charges:
                c = self.ld.record_charge(inv)
                self.charges[inv] = c["A"] + c["R"]
        return state

    def call(self, method, **kw):
        result = getattr(self.ld, method)(**kw)
        self._check()
        return result

    def reg(self, inv, start=None, at=None, src="bs", job_id=None, serial=False):
        start = self.now if start is None else start
        return self.call("register", epoch="E1", invocation_id=inv, source=src, started_wall=start,
                         started_mono=mono(start), job_id=job_id, serial_job=serial, **self._rx(at))

    def link(self, inv, at=None, src="bs", rid=None):
        schema, contract = ax.REGISTRY[src]
        return self.call("link_round", epoch="E1", invocation_id=inv, round_id=rid or "r" + inv, report_schema=schema,
                         validity_contract=contract, **self._rx(at))

    def fin(self, inv, fw=None, at=None, src="bs", s=None, rid=None):
        schema, contract = ax.REGISTRY[src]
        fw = self.now if fw is None else fw
        return self.call("finish", epoch="E1", invocation_id=inv, round_id=rid or "r" + inv, report_schema=schema,
                         validity_contract=contract, finished_wall=fw, finished_mono=mono(fw),
                         selected_summary=s if s is not None else summary(), telemetry_error_present=False,
                         **self._rx(at))

    def init_fail(self, inv, at=None):
        return self.call("report_init_failed", epoch="E1", invocation_id=inv, failed_wall=self.now, failed_mono=mono(self.now),
                         **self._rx(at))

    def exit(self, inv, at=None):
        return self.call("wrapper_exited", epoch="E1", invocation_id=inv, exited_wall=self.now, exited_mono=mono(self.now),
                         **self._rx(at))

    def snap(self, at):
        self._rx(at)
        result = self.ld.aggregation_snapshot(as_of=at, as_of_mono=mono(at))
        self._check()
        return result


def _all_transitions(led, n=12):
    ids = [f"i{j:03d}" for j in range(n)]
    for j, inv in enumerate(ids):
        led.reg(inv, job_id="J" if j % 3 == 0 else None, serial=j % 3 == 0)
    for j, inv in enumerate(ids):
        k = j % 6
        if k == 0:
            led.link(inv)
            led.fin(inv)
        elif k == 1:
            led.link(inv)
            led.fin(inv)
            led.fin(inv, s=summary("M"))                              # 충돌 격리
        elif k == 2:
            led.init_fail(inv)
        elif k == 3:
            led.exit(inv)
        elif k == 4:
            led.link(inv)                                             # 미종료로 남는다 → overdue
    led.reg("next", job_id="J", serial=True)                          # serial next_entry 가 기존 Record 를 바꾼다
    led.snap(hm(9, 59) + OVERDUE + MIN)                               # overdue
    led.fin(ids[4])                                                   # 늦은 종료
    led.snap(hm(12, 0))                                               # 닫힘·상세 해제
    led.fin(ids[0], at=led.now)                                       # post-close 중복
    led.fin(ids[0], at=led.now, s=summary("M"))                       # post-close 충돌
    return ids


# ───────── §2 계상식·기본 B ─────────

def test_budget_state_shape_and_default_b():
    led = L()
    s = led.ld.budget_state()
    # r3 §6 budget 계상: 기존 키와 새 F₄/Q₄/T/Rᵀ·상주 계수를 모두 요구한다.
    assert set(s) == {"F", "Q", "D", "AR", "E", "B", "N", "F_4", "Q_4", "A", "R", "T", "R_T",
                      "TR", "N_total", "N_live", "N_tomb", "N_res", "capacity", "rebuild_count"}
    assert s["B"] == B_DEFAULT and s["N"] == 0 and s["D"] == 0 and s["AR"] == 0
    assert s["E"] == s["F"] + s["Q"] + s["TR"] and s["F"] > 0
    assert led.ld.record_charge("nope") is None


# ───────── §3 모든 전이에서 기존 Record 의 A+R 비증가 · E ≤ B ─────────

def test_every_transition_is_non_increasing_per_existing_record():
    led = L()
    _all_transitions(led)
    assert led.ld.budget_state()["N"] == 13


# ───────── §2 실측 G ≤ E (계상 누락 검출) ─────────

def test_owned_graph_never_exceeds_accounted(gate):
    led = L()
    checkpoints = []
    original_check = led._check

    def check_and_measure():
        state = original_check()
        g = gate.owned_graph(led.ld)
        assert g["unknown_types"] == [], g["unknown_types"]
        checkpoints.append((g["bytes"], state["E"]))
        assert g["bytes"] <= state["E"], (g["bytes"], state)
        return state

    led._check = check_and_measure
    _all_transitions(led)
    assert len(checkpoints) > 20


def test_detail_charge_released_on_close():
    led = L()
    led.reg("a")
    led.link("a")
    led.fin("a")
    with_detail = led.ld.budget_state()["D"]
    assert with_detail > 0
    led.snap(hm(12, 0))
    assert led.ld.budget_state()["D"] == 0


# ───────── §3 ID 요금은 실제 문자열 객체 크기 ─────────

def test_id_charge_uses_actual_string_size():
    short, long_ascii, long_emoji = L(), L(), L()
    short.reg("a" * 10)
    long_ascii.reg("a" * 128)
    long_emoji.reg("😀" + "a" * 124)                                  # UTF-8 128 B, PEP-393 4바이트 폭
    a_short = short.ld.record_charge("a" * 10)["A"]
    a_long = long_ascii.ld.record_charge("a" * 128)["A"]
    a_emoji = long_emoji.ld.record_charge("😀" + "a" * 124)["A"]
    assert a_short < a_long < a_emoji


# ───────── §4 byte 선중단과 첫 거절 원자성 ─────────

def _b_after(k, prefix="b"):
    """같은 ID 로 k 건 등록한 probe 의 E — 이 값을 B 로 두면 정확히 k 건 뒤 첫 거절이 난다(시험 검토 REVISE 반영: B 는 F 보다 커야 한다)."""
    probe = L()
    for j in range(k):
        assert probe.reg(f"{prefix}{j:06d}")["classification"] == "registered"
    return probe.ld.budget_state()["E"]


def _fill_until_reject(led, prefix="b", limit=100_000):
    for j in range(limit):
        inv = f"{prefix}{j:06d}"
        r = led.reg(inv)
        if r["classification"] == "admission_stopped":
            return j, inv, r
    raise AssertionError("never rejected")


def test_byte_first_stop_is_atomic_and_latched():
    k = 40
    led = L(max_resident_bytes=_b_after(k))
    n, rejected, r = _fill_until_reject(led)
    assert n == k                                                    # 같은 순서면 정확히 k 건 뒤 첫 거절
    assert r["records"] == [] and r["diagnostics"]["codes"] == ["admission_stopped", "aggregation_capacity"]
    h = r["health"]
    assert h["admission_stopped"] is True and h["admission_stopped_at"] == led.now
    assert h["uncertain_sources"] == list(ax.REGISTRY) and h["coverage_complete"] is False
    ld = led.ld
    assert rejected not in dict.keys(ld._records) and rejected not in ld._seq
    stop_at = led.now
    led.reg("late-1", at=led.now + S1)
    led.reg("late-2", at=led.now + S1)
    h = led.ld.aggregation_snapshot(as_of=led.now, as_of_mono=mono(led.now))["health"]
    assert h["admission_stopped_at"] == stop_at and h["untracked_invocations"] == 3
    assert ld.budget_state()["N"] == n
    assert "late-1" not in dict.keys(ld._records) and "late-2" not in dict.keys(ld._records)


def test_existing_records_keep_working_after_byte_stop():
    led = L(max_resident_bytes=_b_after(40))
    n, _, _ = _fill_until_reject(led)
    assert n == 40
    first = "b000000"
    assert led.link(first)["classification"] == "linked"            # 필드 예약 안의 전이는 byte 로 거절되지 않는다
    r = led.fin(first)
    assert r["classification"] in {"finalized", "report_unavailable"}  # 상세 byte 가 모자라면 상세만 못 남긴다
    assert led.ld.record(first)["first_digest"] is not None
    assert led.init_fail("b000001")["classification"] != "admission_stopped"
    led.snap(hm(12, 0))                                               # 상세 해제로 D 가 줄어도
    assert led.reg("again", at=led.now)["classification"] == "admission_stopped"   # 재개 없음


def test_slot_first_stop_marks_all_sources_uncertain():
    led = L(max_records=3)
    for inv in ("a", "b", "c"):
        assert led.reg(inv)["classification"] == "registered"
    r = led.reg("d")
    assert r["classification"] == "admission_stopped"
    assert r["diagnostics"]["codes"] == ["admission_stopped", "aggregation_capacity"]
    assert r["health"]["uncertain_sources"] == list(ax.REGISTRY)
    assert "d" not in dict.keys(led.ld._records)


def test_detail_byte_shortage_is_atomic_and_latches():
    """상세 저장 전 실측으로 byte 가 모자라면 상세·열린 색인 무게시, 최초 증거는 남고 latch 가 선다."""
    probe = L()
    probe.reg("p")
    probe.link("p")
    probe.fin("p")
    after = probe.ld.budget_state()
    assert after["D"] > 0
    led = L(max_resident_bytes=after["E"] - 1)                       # 같은 순서를 밟으면 상세를 더하는 순간 1 B 모자란다
    led.reg("p")
    led.link("p")
    r = led.fin("p")
    assert r["classification"] == "report_unavailable", r["classification"]
    rec = led.ld.record("p")
    assert rec["detail"] is None and rec["first_digest"] is not None and rec["first_finished_wall"] is not None
    assert rec["unavailable_reason"] == "aggregation_capacity"
    assert led.ld.budget_state()["D"] == 0
    h = led.ld.aggregation_snapshot(as_of=led.now, as_of_mono=mono(led.now))["health"]
    assert h["admission_stopped"] is True
    assert led.reg("q", at=led.now)["classification"] == "admission_stopped"


def test_detail_count_limit_does_not_latch():
    led = L(max_retained_details=1)
    led.reg("a")
    led.link("a")
    led.fin("a")
    led.reg("b")
    led.link("b")
    r = led.fin("b")
    assert r["classification"] == "report_unavailable"
    h = led.ld.aggregation_snapshot(as_of=led.now, as_of_mono=mono(led.now))["health"]
    assert h["admission_stopped"] is False
    assert led.reg("c")["classification"] == "registered"


# ───────── §2 Q 는 high-water: 삭제·재삽입 뒤에도 줄지 않는다 ─────────

def test_q_is_monotone_high_water():
    led = L()
    qs = []
    for j in range(300):
        led.reg(f"q{j:04d}")
        qs.append(led.ld.budget_state()["Q"])
    assert all(b >= a for a, b in zip(qs, qs[1:]))
    led.link("q0000")
    led.fin("q0000")
    before = led.ld.budget_state()["Q"]
    led.snap(hm(12, 0))
    assert led.ld.budget_state()["Q"] >= before


# ───────── 변이 배터리 보강(시험 잠금 뒤 추가 — Codex 재승인 대상) ─────────

def test_admission_check_prepays_next_container_growth():
    """등록 선검사는 새 Record 를 넣은 뒤의 Q(N+1) 로 해야 한다. Q 가 커지는 지점 k 를 골라 B = E(k+1) - 1 로 두면
    정확히 k 건 뒤 거절이어야 한다(Q(N) 로 검사하면 k+1 번째가 들어가 E > B 가 된다)."""
    probe = L()
    states = [probe.ld.budget_state()]
    for j in range(200):
        assert probe.reg(f"b{j:06d}")["classification"] == "registered"
        states.append(probe.ld.budget_state())
    k = next(n for n in range(30, 200) if states[n + 1]["Q"] > states[n]["Q"])
    led = L(max_resident_bytes=states[k + 1]["E"] - 1)
    n, _, _ = _fill_until_reject(led)
    assert n == k, (n, k)
