"""D7 5a-4 계약 — 닫힌 식별 기록 퇴출·무종료 만료·시계 격리·조회 경계(운영 경로 무접촉).

정본: SOURCE_HEALTH_D7_AGGREGATION.md §1·§5·§6·§7 (5a-4 개정, 5c75f49).
세부 계약: `design/d7-aggregation/slice5a4_contract_r2.md` (Codex 작성, Claude 검토 반영·합의).
공개 이름·반환 모양: `design/d7-aggregation/slice5a4a_interface_r3.md` (sha256 f5c7ef7cd5df2fb1aa14b1edd8fb63f81fa03d06d026285af6928c9038c48f1f, Codex 작성, Claude 검토 P1~P6 반영).
계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex(5a-4b).
"""
from __future__ import annotations

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg

S1 = 10 ** 6
MIN = 60 * S1
HOUR = 60 * MIN
T = 1789984800000000                      # 2026-09-21T10:00:00Z
OFF = T - 10 ** 12
W_LATE = 120 * MIN
EXPIRY = 4 * HOUR


def mono(w):
    return w - OFF


def hm(hh, mm, ss=0, us=0):
    return T + ((hh - 10) * 60 + mm) * MIN + ss * S1 + us


P = ("usd-krw", "jpy-krw", "eur-krw")


def summary(kind="V"):
    col = {"status": "valid", "reason": "validated"} if kind == "V" else {"status": "missing", "reason": "no_value"}
    return {"collection": {p: dict(col) for p in P},
            "writing": {p: {"status": "performed", "reason": "committed"} for p in P}, "final_db": "not_checked"}


EXPIRED = {"expired_finish", "expired_start", "expired_wrapper", "expired_identity_unverified"}


class L:
    """새 ledger 하나. 수신 쌍은 기본적으로 (w, mono(w)) — 두 시계 같은 속도."""

    def __init__(self, origin=None, **limits):
        self.origin = hm(9, 59) if origin is None else origin
        self.ld = lg.RoundLedger("E1", aggregation_started_at=self.origin, limits=limits or None)

    @staticmethod
    def rx(at, at_mono=None):
        return {"received_at": at, "received_mono": mono(at) if at_mono is None else at_mono}

    def reg(self, inv, start, at=None, src="bs", start_mono=None, at_mono=None, **kw):
        return self.ld.register(epoch="E1", invocation_id=inv, source=src, started_wall=start,
                                started_mono=mono(start) if start_mono is None else start_mono,
                                **self.rx(start if at is None else at, at_mono), **kw)

    def link(self, inv, at, src="bs", rid=None, at_mono=None):
        schema, contract = ax.REGISTRY[src]
        return self.ld.link_round(epoch="E1", invocation_id=inv, round_id=rid or "r" + inv, report_schema=schema,
                                  validity_contract=contract, **self.rx(at, at_mono))

    def fin(self, inv, fw, at=None, src="bs", s=None, rid=None, at_mono=None):
        schema, contract = ax.REGISTRY[src]
        return self.ld.finish(epoch="E1", invocation_id=inv, round_id=rid or "r" + inv, report_schema=schema,
                              validity_contract=contract, finished_wall=fw, finished_mono=mono(fw),
                              selected_summary=s if s is not None else summary(), telemetry_error_present=False,
                              **self.rx(fw if at is None else at, at_mono))

    def init_fail(self, inv, fw, at):
        return self.ld.report_init_failed(epoch="E1", invocation_id=inv, failed_wall=fw, failed_mono=mono(fw),
                                          **self.rx(at))

    def exit(self, inv, ew, at):
        return self.ld.wrapper_exited(epoch="E1", invocation_id=inv, exited_wall=ew, exited_mono=mono(ew),
                                      **self.rx(at))

    def snap(self, at, at_mono=None):
        return self.ld.aggregation_snapshot(as_of=at, as_of_mono=mono(at) if at_mono is None else at_mono)

    def cohort(self, at, lo, hi, src="bs"):
        return self.ld.cohort_snapshot(source=src, cohort_start=lo, cohort_end=hi, as_of=at, as_of_mono=mono(at))

    def totals(self, at):
        return self.ld.epoch_cohort_totals(as_of=at, as_of_mono=mono(at))

    @property
    def status(self):
        return self.ld.identity_status

    def done(self, inv, start=None, fw=None, src="bs"):
        """등록·연결·종료(열린 버킷)까지 한 번에."""
        start = hm(10, 0) if start is None else start
        fw = start + S1 if fw is None else fw
        assert self.reg(inv, start, src=src)["classification"] == "registered"
        assert self.link(inv, start, src=src)["classification"] == "linked"
        assert self.fin(inv, fw, src=src)["classification"] == "finalized"


def H(result):
    return result["health"]


# ───────── 1. 상수·초기 health ─────────

def test_constants_match_contract():
    assert lg._W_LATE == 7_200_000_000
    assert lg._UNFINISHED_EXPIRY == 14_400_000_000
    assert lg._REGISTRATION_FRESHNESS == 60_000_000
    assert lg._MAX_CLOCK_SKEW == 60_000_000
    assert lg._REANCHOR_STEP_SKEW == 1_000_000
    assert lg._REANCHOR_PAIRS == 3
    assert lg._REANCHOR_MIN_SPAN == 10_000_000
    assert lg._CLOSE_DELAY == 4_200_000_000 and lg._OVERDUE == 900_000_000


NEW_HEALTH = {
    "N_total": 0, "N_live": 0, "N_tomb": 0, "N_res": 0, "cohort_exact_from": 0, "frozen_through": None,
    "identity_pruned": False, "late_start_excluded": 0, "expired_finish": 0, "expired_start": 0,
    "expired_wrapper": 0, "expired_identity_unverified": 0, "retention_expired": 0, "clock_unverified": 0,
    "clock_isolation": {"active": False, "candidate_pairs": 0, "since_at": None, "since_mono": None,
                        "unverified_from_at": None, "unverified_through_at": None, "reanchor_count": 0},
}


def test_initial_health_has_new_keys_with_exact_values():
    e = L()
    h = H(e.snap(hm(9, 59)))
    for key, value in NEW_HEALTH.items():
        assert h[key] == value, key
        assert type(h[key]) is type(value), key
    assert h["registered_records"] == 0


def test_limits_do_not_accept_time_constants():
    for key in ("w_late", "unfinished_expiry", "registration_freshness"):
        with pytest.raises(ValueError):
            lg.RoundLedger("E1", aggregation_started_at=hm(9, 59), limits={key: 1})


# ───────── 2. 등록 신선도(두 시계 각각 0..60초 포함) ─────────

def test_registration_freshness_boundaries_each_clock():
    e = L()
    start = hm(10, 0)
    r = e.reg("ok", start, at=start + MIN)                                   # 정확히 60초: 접수
    assert r["classification"] == "registered"
    r = e.reg("lw", start, at=start + MIN + 1, start_mono=mono(start) + 1)    # wall 만 60초+1 (mono 는 정확히 60초)
    assert r["classification"] == "late_start_excluded"
    assert r["records"] == [] and r["changes"] == [] and "late_start_excluded" in r["diagnostics"]["codes"]
    r = e.reg("lm", start + 1, at=start + MIN + 1, start_mono=mono(start + 1) - 1)   # mono 만 60초+1
    assert r["classification"] == "late_start_excluded"
    h = H(r)
    assert h["late_start_excluded"] == 2 and h["N_total"] == 1 and h["registered_records"] == 1
    assert h["coverage_complete"] is False and "bs" in h["uncertain_sources"]
    for inv in ("lw", "lm"):
        assert e.ld.record(inv) is None and e.status(inv) == "expired_or_untracked"
    assert e.status("ok") == "live"


def test_future_start_remains_time_integrity_error():
    e = L()
    r = e.reg("f", hm(10, 0, 1), at=hm(10, 0))
    assert r["classification"] == "time_integrity_error"
    assert H(r)["late_start_excluded"] == 0


def test_held_id_redelivery_is_not_rechecked_for_freshness():
    e = L()
    e.reg("a", hm(10, 0))
    r = e.reg("a", hm(10, 0), at=hm(10, 30))                                  # 같은 등록 재전달, 30분 뒤
    assert r["classification"] == "duplicate_invocation"
    assert H(r)["late_start_excluded"] == 0


# ───────── 3. 닫힘 = 퇴출, tombstone 120분, 첫 prune 경계 ─────────

def test_retire_exactly_at_close_and_prune_after_w_late():
    e = L()
    e.done("a")                                                               # [10:00,10:01) → close_at 11:11
    close_at = hm(11, 11)
    h = H(e.snap(close_at - 1))
    assert e.status("a") == "live" and e.ld.record("a") is not None and e.ld.record_charge("a") is not None
    assert (h["N_live"], h["N_tomb"], h["N_res"], h["N_total"]) == (1, 0, 1, 1)
    h = H(e.snap(close_at))
    assert e.status("a") == "tombstoned"
    assert e.ld.record("a") is None and e.ld.record_charge("a") is None
    assert (h["N_live"], h["N_tomb"], h["N_res"], h["N_total"]) == (0, 1, 1, 1)
    assert h["identity_pruned"] is False and h["registered_records"] == 1
    h = H(e.snap(close_at + W_LATE - 1))
    assert e.status("a") == "tombstoned" and h["N_tomb"] == 1
    h = H(e.snap(close_at + W_LATE))
    assert e.status("a") == "expired_or_untracked"
    assert (h["N_live"], h["N_tomb"], h["N_res"], h["N_total"]) == (0, 0, 0, 1)
    assert h["identity_pruned"] is True


def test_retire_waits_for_late_first_finish_receipt():
    """retire_at = max(close_at, 최초 종료 수신시각). 닫힌 뒤 늦은 첫 종료는 그 수신시각부터 120분 보유."""
    e = L()
    e.reg("a", hm(10, 0))
    e.link("a", hm(10, 0))
    r = e.fin("a", hm(10, 0, 30), at=hm(11, 20))                             # 버킷 11:11 닫힌 뒤 첫 종료
    assert r["classification"] == "post_close_finish"
    assert e.status("a") == "tombstoned"                                      # 그 event 안에서 즉시 퇴출(인터페이스 r3)
    assert r["records"] == [] and r["changes"] == []
    e.snap(hm(13, 19, 59, 999_999))
    assert e.status("a") == "tombstoned"
    e.snap(hm(13, 20))
    assert e.status("a") == "expired_or_untracked"


def test_orphan_before_first_prune_then_expired_identity_unverified():
    e = L()
    e.done("a")
    r = e.fin("zz", hm(10, 5), at=hm(10, 5))
    assert r["classification"] == "orphan"
    assert r["diagnostics"]["codes"] == ["orphan_finish"]
    e.snap(hm(11, 11))                                                        # 퇴출만(보유 중): 여전히 orphan
    r = e.link("zz", hm(11, 12))
    assert r["classification"] == "orphan" and H(r)["identity_pruned"] is False
    e.snap(hm(13, 11))                                                        # 첫 prune
    for call in (lambda: e.fin("zz", hm(13, 12)), lambda: e.link("zz", hm(13, 12)),
                 lambda: e.fin("a", hm(10, 0, 1), at=hm(13, 12)),
                 lambda: e.exit("a", hm(13, 12), hm(13, 12)), lambda: e.init_fail("a", hm(13, 12), hm(13, 12))):
        r = call()
        assert r["classification"] == "expired_identity_unverified", r["classification"]
        assert r["records"] == [] and r["changes"] == []
        assert "expired_identity_unverified" in r["diagnostics"]["codes"]
    h = H(r)
    assert h["expired_identity_unverified"] == 5 and h["coverage_complete"] is False
    assert e.ld.record("a") is None


def test_pruned_id_reregistration_is_late_start():
    e = L()
    e.done("a")
    e.snap(hm(13, 11))
    r = e.reg("a", hm(10, 0), at=hm(13, 12))
    assert r["classification"] == "late_start_excluded"
    assert H(r)["N_total"] == 1


def test_tombstone_redelivery_duplicate_and_conflict_do_not_change_totals():
    e = L()
    e.done("a", fw=hm(10, 0, 1))
    e.done("b", start=hm(10, 0, 2), fw=hm(10, 0, 3))
    before = e.snap(hm(11, 11))
    assert e.status("a") == "tombstoned"
    r = e.fin("a", hm(10, 0, 1), at=hm(11, 12))
    assert r["classification"] == "post_close_duplicate"
    assert r["records"] == [] and r["changes"] == []                          # tombstone 단독 적중
    r = e.fin("a", hm(10, 0, 1), at=hm(11, 13), s=summary("M"))
    assert r["classification"] == "post_close_conflict"
    r = e.fin("a", hm(11, 13), at=hm(11, 14))                                 # 종료 시각을 바꿔도 같은 ID 로 판정
    assert r["classification"] == "post_close_conflict"
    after = e.snap(hm(11, 15))
    assert after["cumulative"] == before["cumulative"]
    pcd = after["post_close_diagnostics"]
    assert pcd["post_close_duplicate"] == 1 and pcd["post_close_conflict"] == 2
    assert pcd["cumulative_evidence_uncertain"] is True
    assert pcd["first_uncertain_event"] == {"code": "post_close_conflict", "received_at": hm(11, 13),
                                            "received_mono": mono(hm(11, 13))}
    assert e.status("a") == "tombstoned"


def test_first_uncertain_event_is_none_until_set_and_not_by_post_close_finish():
    e = L()
    e.reg("a", hm(10, 0))
    e.link("a", hm(10, 0))
    assert e.snap(hm(10, 1))["post_close_diagnostics"]["first_uncertain_event"] is None
    assert e.fin("a", hm(10, 0, 30), at=hm(11, 20))["classification"] == "post_close_finish"
    assert e.snap(hm(11, 21))["post_close_diagnostics"]["first_uncertain_event"] is None


def test_round_owned_by_tombstone_cannot_be_linked_to_new_invocation():
    e = L()
    e.done("a")
    e.snap(hm(11, 11))
    e.reg("b", hm(11, 12))
    r = e.link("b", hm(11, 12), rid="ra")                                     # a 의 round 를 b 에 연결 시도
    assert r["classification"] == "identity_conflict"
    assert [rec["invocation_id"] for rec in r["records"]] == ["b"] and r["changes"] == []   # tombstone 은 싣지 않음
    assert e.status("a") == "tombstoned"


def test_prune_removes_round_ownership_producer_assumption_counterexample():
    """prune 뒤에는 옛 round 소유권이 남지 않는다. 생산자가 round ID 를 재사용하면 ledger 는 검출하지 못한다
    (계약 r2 §3 '가정의 경계' — 반례로 기록, 정상 UUID4 경로의 유일성은 생산자 검증 항목)."""
    e = L()
    e.done("a")
    e.snap(hm(13, 11))
    assert H(e.snap(hm(13, 11)))["identity_pruned"] is True
    e.reg("b", hm(13, 12))
    r = e.link("b", hm(13, 12), rid="ra")
    assert r["classification"] == "linked"


# ───────── 4. 무종료 4시간 만료 ─────────

def test_unfinished_expires_after_four_hours_both_clocks():
    e = L()
    e.reg("a", hm(10, 0))
    e.link("a", hm(10, 0))
    e.snap(hm(10, 16))                                                        # overdue 는 진단일 뿐
    assert e.status("a") == "live" and e.ld.record("a")["lifecycle"] == "overdue"
    e.snap(hm(10, 0) + EXPIRY - 1)
    assert e.status("a") == "live"
    h = H(e.snap(hm(10, 0) + EXPIRY))
    assert e.status("a") == "tombstoned" and h["retention_expired"] == 1
    at = hm(14, 1)
    r = e.fin("a", hm(13, 0), at=at)
    assert r["classification"] == "expired_finish" and r["records"] == [] and r["changes"] == []
    r = e.link("a", at)
    assert r["classification"] == "expired_start"
    r = e.exit("a", at, at)
    assert r["classification"] == "expired_wrapper"
    r = e.init_fail("a", at, at)
    assert r["classification"] == "expired_wrapper"
    h = H(r)
    assert (h["expired_finish"], h["expired_start"], h["expired_wrapper"]) == (1, 1, 2)
    snap = e.snap(at)
    assert all(row["rounds"] == 0 for row in snap["recent_rounds"] + snap["cumulative_rounds"])
    assert all(sum(row["collection"].values()) == 0 for row in snap["recent"] + snap["cumulative"])
    e.snap(hm(10, 0) + EXPIRY + W_LATE - 1)
    assert e.status("a") == "tombstoned"
    e.snap(hm(10, 0) + EXPIRY + W_LATE)
    assert e.status("a") == "expired_or_untracked"


@pytest.mark.parametrize("pre", ["wrapper", "init_failed", "next_entry"])
def test_exit_evidence_does_not_shorten_unfinished_expiry(pre):
    e = L()
    e.reg("a", hm(10, 0), job_id="j", serial_job=True)
    if pre == "wrapper":
        e.exit("a", hm(10, 1), hm(10, 1))
    elif pre == "init_failed":
        e.init_fail("a", hm(10, 1), hm(10, 1))
    else:
        e.reg("b", hm(10, 1), job_id="j", serial_job=True)
    e.snap(hm(13, 59, 59, 999_999))
    assert e.status("a") == "live"
    e.snap(hm(14, 0))
    assert e.status("a") == "tombstoned"


def test_late_first_finish_before_expiry_is_accepted():
    e = L()
    e.reg("a", hm(10, 0))
    e.link("a", hm(10, 0))
    fw = hm(13, 59, 59)
    r = e.fin("a", fw, at=fw)
    assert r["classification"] == "finalized"
    assert e.ld.record("a")["first_finished_wall"] == fw


# ───────── 5. 시계: wall 역행·skew → 격리, mono 역행 → 새 epoch ─────────

def _anchored():
    e = L()
    assert e.reg("a", hm(10, 0))["classification"] == "registered"
    return e


def test_wall_regression_is_clock_unverified_not_time_integrity():
    e = _anchored()
    r = e.reg("b", hm(9, 59, 59), at=hm(9, 59, 59), at_mono=mono(hm(10, 0)) + S1)
    assert r["classification"] == "clock_unverified" and r["records"] == []
    h = H(r)
    assert h["clock_unverified"] == 1 and h["clock_isolation"]["active"] is True
    assert h["coverage_complete"] is False
    assert e.ld.record("b") is None
    assert h["last_received_at"] == hm(10, 0)                                 # 성공 수신 쌍 불변


def test_mono_regression_stays_time_integrity_error():
    e = _anchored()
    r = e.reg("b", hm(10, 0, 1), at=hm(10, 0, 1), at_mono=mono(hm(10, 0)) - 1)
    assert r["classification"] == "time_integrity_error"


def test_skew_over_sixty_seconds_is_isolated():
    e = _anchored()
    at = hm(10, 2)
    r = e.snap(at, at_mono=mono(hm(10, 0)) + MIN - 1)                         # wall +120s, mono +60s−1µs → 차 >60s
    assert r["classification"] == "clock_unverified"
    e2 = _anchored()
    r = e2.snap(hm(10, 1), at_mono=mono(hm(10, 0)))                          # 차 정확히 60초: 정상
    assert r["classification"] == "snapshot"


def _step(e, n, step=5 * S1, offset=5 * MIN, base=None):
    """wall 을 offset 만큼 계단 이동한 뒤 두 시계를 step 씩 함께 전진시키는 탐침 n 개."""
    base = hm(10, 0) if base is None else base
    out = []
    for k in range(1, n + 1):
        w = base + offset + k * step
        out.append(e.snap(w, at_mono=mono(base) + k * step))
    return out


def test_reanchor_after_three_consistent_probes_spanning_ten_seconds():
    e = _anchored()
    r1, r2, r3 = _step(e, 3)                                                  # 후보 t, t+5s, t+10s → span 10s
    assert [r["classification"] for r in (r1, r2, r3)] == ["clock_unverified"] * 3
    assert H(r1)["clock_isolation"]["candidate_pairs"] == 1
    assert H(r2)["clock_isolation"]["candidate_pairs"] == 2
    iso = H(r3)["clock_isolation"]
    assert iso["active"] is False and iso["reanchor_count"] == 1 and iso["candidate_pairs"] == 0
    w = hm(10, 0) + 5 * MIN + 20 * S1
    m = mono(hm(10, 0)) + 20 * S1
    r = e.reg("b", w, at=w, start_mono=m, at_mono=m)                          # 다음 입력부터 정상 접수(시작 mono = 수신 mono)
    assert r["classification"] == "registered"


def test_reanchor_needs_ten_seconds_even_with_three_pairs():
    e = _anchored()
    base = hm(10, 0)
    rs = _step(e, 3, step=4 * S1)                                            # 후보 4s·8s·12s → span 8s
    iso = H(rs[-1])["clock_isolation"]
    assert iso["active"] is True and iso["candidate_pairs"] == 3
    r = e.snap(base + 5 * MIN + 12 * S1, at_mono=mono(base) + 12 * S1)       # 같은 시각 재탐침은 세지 않음
    assert H(r)["clock_isolation"]["active"] is True and H(r)["clock_isolation"]["candidate_pairs"] == 3
    r = e.snap(base + 5 * MIN + 16 * S1, at_mono=mono(base) + 16 * S1)       # span 12s → 재앵커
    assert H(r)["clock_isolation"]["active"] is False and H(r)["clock_isolation"]["reanchor_count"] == 1


def test_two_pairs_spanning_ten_seconds_do_not_reanchor():
    """재앵커는 3쌍 이상 **그리고** 10초 이상 — 2쌍만으로 10초를 채워도 격리 유지."""
    e = _anchored()
    rs = _step(e, 2, step=10 * S1)                                           # 후보 10s·20s → 2쌍, span 10s
    iso = H(rs[-1])["clock_isolation"]
    assert iso["active"] is True and iso["candidate_pairs"] == 2
    r = _step(e, 3, step=10 * S1)[2]                                          # 세 번째(30s) → 재앵커
    assert H(r)["clock_isolation"]["active"] is False


def test_candidate_run_restarts_on_inconsistent_probe():
    e = _anchored()
    base = hm(10, 0)
    e.snap(base + 5 * MIN + 5 * S1, at_mono=mono(base) + 5 * S1)
    e.snap(base + 5 * MIN + 10 * S1, at_mono=mono(base) + 10 * S1)
    r = e.snap(base + 5 * MIN + 17 * S1, at_mono=mono(base) + 15 * S1)       # 인접 증분 차 2초 > 1초 → 재시작
    assert H(r)["clock_isolation"]["active"] is True and H(r)["clock_isolation"]["candidate_pairs"] == 1


def test_isolated_inputs_are_not_admitted_retroactively():
    e = _anchored()
    base = hm(10, 0)
    r = e.reg("x", base + 5 * MIN + 5 * S1, at=base + 5 * MIN + 5 * S1, start_mono=mono(base) + 5 * S1,
              at_mono=mono(base) + 5 * S1)
    assert r["classification"] == "clock_unverified"
    _step(e, 3, base=base)
    assert e.ld.record("x") is None and e.status("x") == "expired_or_untracked"


def test_forward_wall_step_does_not_close_before_mono_deadline():
    e = L()
    e.done("a")
    base = hm(10, 0, 1)
    jump = 2 * HOUR                                                          # wall 만 2시간 앞으로 계단
    for k in range(1, 4):
        e.snap(base + jump + k * 5 * S1, at_mono=mono(base) + k * 5 * S1)
    assert H(e.snap(base + jump + 20 * S1, at_mono=mono(base) + 20 * S1))["clock_isolation"]["active"] is False
    assert e.status("a") == "live"                                           # mono 기준 close 하한 전
    snap = e.snap(base + jump + 71 * MIN, at_mono=mono(base) + 71 * MIN)
    assert snap["classification"] == "snapshot" and e.status("a") == "tombstoned"


def test_merge_failure_does_not_publish_retirement_then_retry():
    e = L()
    e.done("a")
    e.ld._inject_cumulative_merge_failure_for_test(bucket_end=hm(10, 1))
    with pytest.raises(lg.CumulativeMergeFailureForTest):
        e.snap(hm(11, 11))
    assert e.status("a") == "live" and e.ld.record("a") is not None
    assert H(e.snap(hm(11, 11)))["N_tomb"] == 1
    assert e.status("a") == "tombstoned"


# ───────── 6. cohort 동결·epoch totals ─────────

def test_cohort_expired_before_exact_horizon_and_snapshot_after():
    e = L()
    e.done("a", start=hm(10, 0))
    e.reg("b", hm(11, 10))
    e.snap(hm(11, 11))
    h = H(e.snap(hm(11, 11)))
    assert h["frozen_through"] == hm(10, 0) and h["cohort_exact_from"] == hm(10, 0) + 1
    r = e.cohort(hm(11, 12), hm(9, 59), hm(11, 12))
    assert r["classification"] == "cohort_expired"
    assert set(r) == {"classification", "as_of", "as_of_mono", "diagnostics", "health"}
    assert r["diagnostics"]["codes"] == ["cohort_expired"] and r["diagnostics"]["coverage_error"] is True
    r = e.cohort(hm(11, 12), hm(10, 0) + 1, hm(11, 12))
    assert r["classification"] == "snapshot" and r["registered_invocations"] == 1
    assert r["equations_hold"] == {"connection": True, "lifecycle": True}


def test_recent_sixty_minute_cohort_is_always_exact_in_normal_flow():
    e = L()
    for k in range(0, 300, 5):                                                # 5시간, 5분마다 한 호출
        start = hm(10, 0) + k * MIN
        e.done(f"i{k:03d}", start=start)
        at = start + 2 * S1
        r = e.cohort(at, at - 60 * MIN, at)
        assert r["classification"] == "snapshot", k
        assert r["registered_invocations"] == min(k // 5 + 1, 12), k        # 창 [at−60분, at) 의 시작 수
        assert r["equations_hold"] == {"connection": True, "lifecycle": True}
    h = H(r)
    assert h["N_tomb"] > 0 and h["cohort_exact_from"] > hm(10, 0)             # 실제로 퇴출이 일어난 흐름
    assert h["cohort_exact_from"] <= at - 60 * MIN


def test_epoch_totals_frozen_plus_live():
    e = L()
    e.done("a", start=hm(10, 0))
    e.reg("b", hm(10, 0, 5))                                                  # 무종료
    e.done("c", start=hm(11, 5), src="citi")
    at = hm(11, 11)
    e.snap(at)
    r = e.totals(at)
    assert r["classification"] == "snapshot"
    assert [row["source"] for row in r["sources"]] == list(ax.REGISTRY)
    assert r["cohort_exact_from"] == H(r)["cohort_exact_from"] and r["frozen_through"] == H(r)["frozen_through"]
    bs = next(row for row in r["sources"] if row["source"] == "bs")
    assert (bs["registered_invocations"], bs["frozen_invocations"], bs["live_invocations"]) == (2, 1, 1)
    for row in r["sources"]:
        n = row["registered_invocations"]
        assert n == row["frozen_invocations"] + row["live_invocations"]
        assert sum(row["connection_counts"].values()) == n and sum(row["lifecycle_counts"].values()) == n
        assert row["equations_hold"] == {"connection": True, "lifecycle": True}
    assert sum(row["registered_invocations"] for row in r["sources"]) == H(r)["N_total"] == 3
    assert bs["lifecycle_counts"]["finalized"] == 1
    e.snap(hm(14, 0, 5))                                                      # b 무종료 만료 → frozen report_unavailable
    r = e.totals(hm(14, 0, 5))
    bs = next(row for row in r["sources"] if row["source"] == "bs")
    assert (bs["frozen_invocations"], bs["live_invocations"]) == (2, 0)
    assert bs["lifecycle_counts"]["report_unavailable"] == 1
    e.snap(hm(16, 1))                                                         # 전부 prune 뒤에도 totals 유지
    r2 = e.totals(hm(16, 1))
    assert [row["registered_invocations"] for row in r2["sources"]] == \
        [row["registered_invocations"] for row in r["sources"]]


def test_tombstone_conflict_does_not_change_frozen_lifecycle():
    e = L()
    e.done("a")
    e.snap(hm(11, 11))
    before = e.totals(hm(11, 11))
    e.fin("a", hm(10, 0, 1), at=hm(11, 12), s=summary("M"))
    after = e.totals(hm(11, 12))
    assert [(row["lifecycle_counts"], row["connection_counts"]) for row in after["sources"]] == \
        [(row["lifecycle_counts"], row["connection_counts"]) for row in before["sources"]]


# ───────── 7. 상주 한도는 동시 N_res, seq·cursor 는 누적 ─────────

def test_n_res_limit_allows_cumulative_admission_beyond_max_records():
    e = L(max_records=4)
    for k in range(10):                                                       # 60분 간격, 각 191분 상주 → 동시 ≤4
        start = hm(10, 0) + k * HOUR
        e.done(f"i{k}", start=start)
        h = H(e.snap(start + 2 * S1))
        assert h["N_res"] <= 4 and h["admission_stopped"] is False, k
    assert h["N_total"] == 10
    rec = e.ld.record("i9")
    assert rec["seq"] == 10
    r = e.ld.contributions_open(as_of=hm(19, 0, 3), as_of_mono=mono(hm(19, 0, 3)), after_seq=9, limit=16)
    assert r["classification"] == "snapshot" and [x["seq"] for x in r["entries"]] == [10]
    r = e.ld.contributions_open(as_of=hm(19, 0, 3), as_of_mono=mono(hm(19, 0, 3)), after_seq=0, limit=16)
    assert [x["seq"] for x in r["entries"]] == [9, 10]                        # 퇴출된 seq 1..8 은 건너뜀(i8 은 19:11 닫힘 전)


def test_admission_latch_is_not_released_by_retirement():
    e = L(max_records=2)
    e.done("a", start=hm(10, 0))
    e.done("b", start=hm(10, 0, 2))
    r = e.reg("c", hm(10, 0, 4))
    assert r["classification"] == "admission_stopped"
    e.snap(hm(13, 11))                                                        # 모두 prune — N_res 0
    h = H(e.snap(hm(13, 11)))
    assert h["N_res"] == 0 and h["admission_stopped"] is True
    r = e.reg("d", hm(13, 12))
    assert r["classification"] == "admission_stopped"


# ───────── 8. 바이트 계상 ─────────

def test_budget_state_keys_and_identities_through_retirement():
    e = L()
    e.done("a")
    e.reg("b", hm(10, 0, 5))

    def check():
        b = e.ld.budget_state()
        for key in ("F", "Q", "D", "AR", "E", "B", "N", "F_4", "Q_4", "A", "R", "T", "R_T", "TR",
                    "N_total", "N_live", "N_tomb", "N_res", "capacity", "rebuild_count"):
            assert key in b, key
        assert b["F"] == b["F_4"] and b["Q"] == b["Q_4"] == b["capacity"]["charged_bytes"]
        assert b["AR"] == b["A"] + b["R"] and b["TR"] == b["T"] + b["R_T"] and b["N"] == b["N_res"]
        assert b["E"] == b["F_4"] + b["Q_4"] + b["D"] + b["AR"] + b["TR"] <= b["B"]
        live = [inv for inv in ("a", "b") if e.status(inv) == "live"]
        assert b["AR"] == sum(sum(e.ld.record_charge(inv).values()) for inv in live)   # 퇴출된 요금은 AR 에서 빠진다
        return b

    b0 = check()
    assert b0["TR"] == 0 and b0["N_res"] == 2
    e.snap(hm(11, 11))
    b1 = check()
    assert b1["T"] > 0 and b1["N_tomb"] == 1 and b1["N_live"] == 1 and b1["AR"] < b0["AR"]
    e.snap(hm(13, 11))
    b2 = check()
    assert b2["N_tomb"] == 0 and b2["TR"] == 0
    assert b2["Q_4"] >= 0 and b2["capacity"]["rebuild_old_bytes"] >= 0


def test_backward_wall_step_cannot_reopen_frozen_cohort():
    """wall 이 뒤로 계단 이동해 재앵커된 뒤에도, 신선한 새 시작이 cohort_exact_from 이전이면 무삽입."""
    back2 = 80 * MIN
    e2 = L()
    e2.done("a", start=hm(10, 0))
    e2.done("z", start=hm(10, 30))
    e2.snap(hm(11, 41))                                                      # a·z 퇴출 → exact_from 10:30+1
    base2 = hm(11, 41)
    for k in range(1, 4):
        e2.snap(base2 - back2 + k * 5 * S1, at_mono=mono(base2) + k * 5 * S1)
    w2 = base2 - back2 + 20 * S1                                             # 10:21:20 < 10:30
    r = e2.reg("fresh", w2, at=w2, start_mono=mono(base2) + 20 * S1, at_mono=mono(base2) + 20 * S1)
    assert r["classification"] == "clock_unverified" and e2.ld.record("fresh") is None
    w3 = hm(10, 31)                                                          # 동결 범위를 지난 뒤 정상 접수
    m3 = mono(base2) + (w3 - (base2 - back2))
    r = e2.reg("later", w3, at=w3, start_mono=m3, at_mono=m3)
    assert r["classification"] == "registered"


def test_normal_retirement_is_not_index_damage_but_injected_fault_is():
    e = L()
    e.done("a")
    e.reg("b", hm(11, 10))
    e.link("b", hm(11, 10))
    e.snap(hm(11, 11))
    assert e.status("a") == "tombstoned"
    r = e.ld.contributions_open(as_of=hm(11, 11), as_of_mono=mono(hm(11, 11)), after_seq=0, limit=16)
    assert r["classification"] == "snapshot" and H(r)["index_error"] is False
    assert e.cohort(hm(11, 12), hm(10, 0) + 1, hm(11, 12))["classification"] == "snapshot"
    e.ld._inject_identity_fault_for_test(invocation_id="b", fault="missing_record")
    r = e.ld.contributions_open(as_of=hm(11, 12), as_of_mono=mono(hm(11, 12)), after_seq=0, limit=16)
    assert r["classification"] == "post_close_unverified"


@pytest.mark.parametrize("kind", ["closed", "unfinished"])
def test_mass_retirement_temporary_peak(kind):
    import gc
    import tracemalloc
    n = 2_000 if kind == "closed" else 5_000                                 # closed 는 열린 상세 한도 2,048 안에서
    e = L()
    start = hm(10, 0)
    ids = [f"m{j:05d}" for j in range(n)]
    for inv in ids:
        assert e.reg(inv, start)["classification"] == "registered"
    if kind == "closed":
        for inv in ids:
            assert e.link(inv, start)["classification"] == "linked"
        for inv in ids:
            assert e.fin(inv, start + S1, at=start + S1)["classification"] == "finalized"
    at = hm(11, 11) if kind == "closed" else start + EXPIRY
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before, _ = tracemalloc.get_traced_memory()
        h = H(e.snap(at))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert h["N_tomb"] == n and h["N_live"] == 0
    assert peak - before <= 262_144, peak - before


# ───────── 9. serial 직전 호출이 tombstone 일 때(인터페이스 r3 P6) ─────────

def test_serial_predecessor_tombstone_after_finish_returns_only_new_record():
    e = L()
    e.reg("a", hm(10, 0), job_id="j", serial_job=True)
    e.link("a", hm(10, 0))
    e.fin("a", hm(10, 0, 1))
    e.snap(hm(11, 11))
    assert e.status("a") == "tombstoned"
    r = e.reg("b", hm(11, 12), job_id="j", serial_job=True)
    assert r["classification"] == "registered" and r["changes"] == []
    assert [rec["invocation_id"] for rec in r["records"]] == ["b"]
    assert r["diagnostics"]["codes"] == []
    assert e.status("a") == "tombstoned"


def test_serial_predecessor_expired_without_digest_yields_next_entry_once():
    e = L()
    e.reg("a", hm(10, 0), job_id="j", serial_job=True)
    h = H(e.snap(hm(14, 0)))
    assert e.status("a") == "tombstoned" and h["retention_expired"] == 1
    before = e.totals(hm(14, 0))
    r = e.reg("b", hm(14, 0, 30), job_id="j", serial_job=True)
    assert r["classification"] == "registered"
    assert [rec["invocation_id"] for rec in r["records"]] == ["b"] and r["changes"] == []
    assert r["diagnostics"]["codes"] == ["next_entry"] and r["diagnostics"]["coverage_error"] is True
    assert H(r)["retention_expired"] == 1
    after = e.totals(hm(14, 0, 30))
    bs_before = next(row for row in before["sources"] if row["source"] == "bs")
    bs_after = next(row for row in after["sources"] if row["source"] == "bs")
    assert bs_after["frozen_invocations"] == bs_before["frozen_invocations"] == 1
    assert bs_after["lifecycle_counts"]["report_unavailable"] == bs_before["lifecycle_counts"]["report_unavailable"]


def test_serial_predecessor_pruned_does_not_resurrect():
    e = L()
    e.reg("a", hm(10, 0), job_id="j", serial_job=True)
    e.link("a", hm(10, 0))
    e.fin("a", hm(10, 0, 1))
    e.snap(hm(13, 11))
    assert e.status("a") == "expired_or_untracked"
    r = e.reg("b", hm(13, 12), job_id="j", serial_job=True)
    assert r["classification"] == "registered" and [rec["invocation_id"] for rec in r["records"]] == ["b"]
    assert e.ld.record("a") is None and e.status("a") == "expired_or_untracked"


# ───────── 10. 입력 사건의 경계(snapshot 이 아닌 event 가 기한에 걸릴 때) ─────────

def test_redelivery_events_at_close_and_prune_boundaries():
    e = L()
    e.done("a", fw=hm(10, 0, 1))
    close_at = hm(11, 11)
    r = e.fin("a", hm(10, 0, 1), at=close_at - 1)                            # 닫힘 직전: live 중복
    assert r["classification"] == "duplicate_finish" and e.status("a") == "live"
    r = e.fin("a", hm(10, 0, 1), at=close_at)                                # 정확히 닫힘: 같은 event 안에서 퇴출
    assert r["classification"] == "post_close_duplicate" and r["records"] == [] and r["changes"] == []
    assert e.status("a") == "tombstoned"
    r = e.fin("a", hm(10, 0, 1), at=close_at + W_LATE - 1)
    assert r["classification"] == "post_close_duplicate" and e.status("a") == "tombstoned"
    r = e.fin("a", hm(10, 0, 1), at=close_at + W_LATE)                       # 정확히 prune: 선행 prune 뒤 판정
    assert r["classification"] == "expired_identity_unverified"
    assert e.status("a") == "expired_or_untracked"


def test_unfinished_expiry_requires_both_clocks():
    e = L()
    e.reg("a", hm(10, 0))
    deadline = hm(10, 0) + EXPIRY
    e.snap(deadline, at_mono=mono(deadline) - 30 * S1)                       # wall 만 도달(skew 30초 ≤ 60초)
    assert e.status("a") == "live"
    e.snap(deadline + 30 * S1, at_mono=mono(deadline))                       # mono 도달
    assert e.status("a") == "tombstoned"
    prune = deadline + W_LATE
    e.snap(prune + 30 * S1, at_mono=mono(prune) - 1)                         # wall 은 지났지만 mono 1µs 전
    assert e.status("a") == "tombstoned"
    e.snap(prune + 30 * S1, at_mono=mono(prune))
    assert e.status("a") == "expired_or_untracked"


def test_retirement_requires_both_clocks_at_close():
    e = L()
    e.done("a", fw=hm(10, 0, 1))
    close_at = hm(11, 11)
    e.snap(close_at + 30 * S1, at_mono=mono(close_at) - 1)
    assert e.status("a") == "live"
    e.snap(close_at + 30 * S1, at_mono=mono(close_at))
    assert e.status("a") == "tombstoned"


# ───────── 11. 누적 seq·cursor·슬롯 재사용과 소유 그래프 G ≤ E ─────────

def _gate():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "d7_gate_for_retention", Path(__file__).resolve().parents[1] / "scripts" / "d7_ledger_measure_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_churn_small_limit_seq_cursor_and_owned_graph():
    gate = _gate()
    e = L(max_records=4)
    seen = []
    for k in range(40):                                                       # 60분 간격 40회 — 누적 10배
        start = hm(10, 0) + k * HOUR
        e.done(f"c{k:02d}", start=start)
        seen.append(e.ld.record(f"c{k:02d}")["seq"])
        at = start + 2 * S1
        h = H(e.snap(at))
        assert h["N_res"] <= 4 and h["admission_stopped"] is False and h["N_total"] == k + 1
        b = e.ld.budget_state()
        graph = gate.owned_graph(e.ld)
        assert graph["unknown_types"] == []
        assert graph["bytes"] <= b["E"] <= b["B"], (k, graph["bytes"], b["E"])
        r = e.ld.contributions_open(as_of=at, as_of_mono=mono(at), after_seq=k, limit=16)
        assert [x["seq"] for x in r["entries"]] == [k + 1]
    assert seen == list(range(1, 41))                                         # 슬롯을 재사용해도 seq 는 누적
    at = hm(10, 0) + 39 * HOUR + 3 * S1
    r = e.ld.contributions_open(as_of=at, as_of_mono=mono(at), after_seq=40, limit=16)
    assert r["entries"] == []


def test_conflict_and_late_first_finish_at_close_boundary():
    e = L()
    e.done("a", fw=hm(10, 0, 1))
    close_at = hm(11, 11)
    r = e.fin("a", hm(10, 0, 1), at=close_at - 1, s=summary("M"))            # 닫힘 직전 충돌: live 격리
    assert r["classification"] == "conflicting_finish" and e.status("a") == "live"
    e2 = L()
    e2.done("a", fw=hm(10, 0, 1))
    r = e2.fin("a", hm(10, 0, 1), at=close_at, s=summary("M"))               # 정확히 닫힘: tombstone 충돌
    assert r["classification"] == "post_close_conflict" and r["records"] == [] and e2.status("a") == "tombstoned"
    for at, expect, status in ((close_at - 1, "finalized", "live"), (close_at, "post_close_finish", "tombstoned")):
        e3 = L()
        e3.reg("a", hm(10, 0))
        e3.link("a", hm(10, 0))
        r = e3.fin("a", hm(10, 0, 30), at=at)                                 # 결과 버킷 [10:00,10:01) 의 늦은 첫 종료
        assert r["classification"] == expect and e3.status("a") == status, at


def test_malformed_first_finish_retires_and_is_judged_by_digest():
    e = L()
    e.reg("a", hm(10, 0))
    e.link("a", hm(10, 0))
    r = e.fin("a", hm(10, 0, 1), s={})
    assert r["classification"] == "finalized" and "report_malformed" in r["diagnostics"]["codes"]
    e.snap(hm(11, 11))
    assert e.status("a") == "tombstoned"
    assert e.fin("a", hm(10, 0, 1), at=hm(11, 12), s={})["classification"] == "post_close_duplicate"
    assert e.fin("a", hm(10, 0, 1), at=hm(11, 13))["classification"] == "post_close_conflict"


def test_format_rejects_on_tombstone_do_not_touch_identity():
    e = L()
    e.done("a")
    e.snap(hm(11, 11))
    h0 = H(e.snap(hm(11, 12)))
    schema, contract = ax.REGISTRY["bs"]
    r = e.ld.finish(epoch="E1", invocation_id="a", round_id="", report_schema=schema, validity_contract=contract,
                    finished_wall=hm(10, 0, 1), finished_mono=mono(hm(10, 0, 1)), selected_summary=summary(),
                    telemetry_error_present=False, received_at=hm(11, 13), received_mono=mono(hm(11, 13)))
    assert r["classification"] == "invalid_argument"
    with pytest.raises(TypeError):
        e.ld.finish(epoch="E1", invocation_id="a", round_id="ra", report_schema=schema, validity_contract=contract,
                    finished_wall=hm(10, 0, 1), finished_mono=mono(hm(10, 0, 1)), selected_summary="x",
                    telemetry_error_present=False, received_at=hm(11, 13), received_mono=mono(hm(11, 13)))
    h = H(e.snap(hm(11, 14)))
    assert e.status("a") == "tombstoned"
    for key in ("expired_finish", "expired_identity_unverified", "N_tomb", "N_total"):
        assert h[key] == h0[key], key
    assert e.snap(hm(11, 14))["post_close_diagnostics"]["post_close_conflict"] == 0
