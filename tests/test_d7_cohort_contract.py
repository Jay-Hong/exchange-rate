"""D7 네 번째 조각 계약 — 시작 cohort 두 보존식·초기화 실패·래퍼 종료·overdue·늦은 종료(운영 경로 무접촉).

정본: SOURCE_HEALTH_D7_AGGREGATION.md §1·§2·§4. 세부 계약은 Claude·Codex 합의본
`design/d7-aggregation/slice4_contract_r2.md`(Codex 작성 — r1 과 Claude 검토 K1 종료 상한·K2 비용 게이트 반영, APPROVE_CONTRACT_DRAFT).
벡터 이름 V1~V15 는 그 문서 S4.7 표를 따른다. 계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.
잠긴 기존 시험의 변경(S4.6)은 tests/test_d7_round_ledger_contract.py·tests/test_d7_aggregation_contract.py 에 함께 반영한다.
"""
from __future__ import annotations

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg

P = ("usd-krw", "jpy-krw", "eur-krw")
S1 = 10 ** 6
MIN = 60 * S1
T = 1789984800000000                      # 2026-09-21T10:00:00Z
OFF = T - 10 ** 12
OVERDUE = 900_000_000                     # 15분(mono µs)
CONTRACT = ax.REGISTRY["bs"]


def mono(w):
    return w - OFF


def hm(hh, mm, ss=0, us=0):
    return T + ((hh - 10) * 60 + mm) * MIN + ss * S1 + us


def a(status, reason):
    return {"status": status, "reason": reason}


def S(kind="V"):
    col = a("valid", "validated") if kind == "V" else a("missing", "no_value")
    return {"collection": {p: dict(col) for p in P}, "writing": {p: a("performed", "committed") for p in P},
            "final_db": "not_checked"}


def D(kind="V"):
    s = S(kind)
    clean = {ax_: {p: {"status": s[ax_][p]["status"], "reason": s[ax_][p]["reason"]} for p in P}
             for ax_ in ("collection", "writing")}
    clean["final_db"] = "not_checked"
    return ax.normalize_round_axes("bs", CONTRACT[0], CONTRACT[1], clean, False)


class Co:
    """한 ledger 와 비감소 수신 시계. 시각 인자는 모두 wall(µs), mono 는 mono(wall) 로 맞춘다(별도 지정 제외)."""

    def __init__(self, origin=None):
        self.origin = hm(9, 59) if origin is None else origin
        self.ld = lg.RoundLedger("E1", aggregation_started_at=self.origin)
        self.now = self.origin

    def _rx(self, w):
        assert w >= self.now, "시험 작성 오류: 수신 시계가 역행"
        self.now = w
        return {"received_at": w, "received_mono": mono(w)}

    def reg(self, inv, start=None, at=None, job_id=None, serial=False, src="bs", sm=None):
        # r3 §6 새 ID의 오래된 시작: 기본 fixture 는 현재 수신 시각에 시작한다.
        if start is None:
            prior = self.ld.record(inv)
            start = prior["started_wall"] if prior is not None else max(T, self.now)
        return self.ld.register(epoch="E1", invocation_id=inv, source=src, started_wall=start,
                                started_mono=mono(start) if sm is None else sm, job_id=job_id, serial_job=serial,
                                **self._rx(max(self.now, start) if at is None else at))

    def link(self, inv, at=None):
        return self.ld.link_round(epoch="E1", invocation_id=inv, round_id="r" + inv, report_schema=CONTRACT[0],
                                  validity_contract=CONTRACT[1], **self._rx(self.now if at is None else at))

    def fin(self, inv, fw, at, fm=None, kind="V", contract=None):
        return self.ld.finish(epoch="E1", invocation_id=inv, round_id="r" + inv, report_schema=CONTRACT[0],
                              validity_contract=CONTRACT[1] if contract is None else contract,
                              finished_wall=fw, finished_mono=mono(fw) if fm is None else fm,
                              selected_summary=S(kind), telemetry_error_present=False, **self._rx(at))

    def init_fail(self, inv, fw, at, fm=None):
        return self.ld.report_init_failed(epoch="E1", invocation_id=inv, failed_wall=fw,
                                          failed_mono=mono(fw) if fm is None else fm,
                                          **self._rx(at))

    def exit(self, inv, ew, at, em=None):
        return self.ld.wrapper_exited(epoch="E1", invocation_id=inv, exited_wall=ew,
                                      exited_mono=mono(ew) if em is None else em, **self._rx(at))

    def cohort(self, at, lo=None, hi=None, src="bs"):
        self._rx(at)
        return self.ld.cohort_snapshot(source=src, cohort_start=hm(9, 59) if lo is None else lo,
                                       cohort_end=hm(10, 1) if hi is None else hi, as_of=at, as_of_mono=mono(at))

    def snap(self, at):
        self._rx(at)
        return self.ld.aggregation_snapshot(as_of=at, as_of_mono=mono(at))

    def rec(self, inv):
        return self.ld.record(inv)


def counts(c):
    k, l = c["connection_counts"], c["lifecycle_counts"]
    return (k["started"], k["init_failed"], k["unbound"],
            l["awaiting_report"], l["in_flight"], l["overdue"], l["report_unavailable"], l["finalized"],
            l["conflicting"], l["contract_mixed"])


def ok_equations(c):
    assert c["classification"] == "snapshot"
    assert c["equations_hold"] == {"connection": True, "lifecycle": True}
    k = c["connection_counts"]
    assert sum(k.values()) == sum(c["lifecycle_counts"].values()) == c["registered_invocations"]


def cum(co, at):
    s = co.snap(at)
    hit = [r for r in s["cumulative"] if r["source"] == "bs" and r["pair"] == "usd-krw"]
    return hit[0]["collection"], s


# ═════════ 형태 ═════════

COHORT_KEYS = {"classification", "as_of", "as_of_mono", "process_epoch", "source", "expected_report_schema",
               "validity_contract", "cohort_start", "cohort_end", "registered_invocations", "connection_counts",
               "lifecycle_counts", "ever_overdue", "ever_unavailable", "late_finish_accepted", "equations_hold",
               "diagnostics", "health"}
NEW_RECORD_KEYS = {"job_id", "serial_job", "init_failed_wall", "init_failed_mono", "exit_evidence",
                   "exit_evidence_wall", "exit_evidence_mono", "overdue_first_observed_at",
                   "overdue_first_observed_mono"}


def test_cohort_snapshot_shape_and_empty_cohort():
    co = Co()
    c = co.cohort(hm(10, 5))
    assert set(c) == COHORT_KEYS
    assert (c["source"], c["expected_report_schema"], c["validity_contract"]) == ("bs",) + CONTRACT
    assert (c["cohort_start"], c["cohort_end"], c["registered_invocations"]) == (hm(9, 59), hm(10, 1), 0)
    assert counts(c) == (0,) * 10
    assert (c["ever_overdue"], c["ever_unavailable"], c["late_finish_accepted"]) == (0, 0, 0)
    ok_equations(c)
    assert set(c["connection_counts"]) == {"started", "init_failed", "unbound"}
    assert set(c["lifecycle_counts"]) == {"awaiting_report", "in_flight", "overdue", "report_unavailable",
                                          "finalized", "conflicting", "contract_mixed"}


def test_new_record_keys_start_null():
    co = Co()
    r = co.reg("A")
    rec = r["records"][0]
    assert NEW_RECORD_KEYS <= set(rec)
    assert rec["job_id"] is None and rec["serial_job"] is False
    assert all(rec[k] is None for k in NEW_RECORD_KEYS - {"job_id", "serial_job"})
    assert r["health"]["unsupported_invocations"] == 0 and r["health"]["registration_errors"] == 0


# ═════════ V1~V3 · 15분 경계 ═════════

def test_V1_V2_V3_overdue_boundary_then_late_finish():
    co = Co()
    co.reg("A")
    assert co.link("A")["classification"] == "linked"
    c = co.cohort(T + OVERDUE - 1)                                  # 14:59.999999
    assert counts(c) == (1, 0, 0, 0, 1, 0, 0, 0, 0, 0) and c["ever_overdue"] == 0
    ok_equations(c)
    c = co.cohort(T + OVERDUE)                                      # 정확히 15:00
    assert counts(c) == (1, 0, 0, 0, 0, 1, 0, 0, 0, 0) and c["ever_overdue"] == 1
    ok_equations(c)
    rec = co.rec("A")
    assert rec["lifecycle"] == "overdue" and "ever_overdue" in rec["diagnostics"]["codes"]
    assert (rec["overdue_first_observed_at"], rec["overdue_first_observed_mono"]) == (T + OVERDUE, mono(T + OVERDUE))
    c = co.cohort(T + OVERDUE)                                      # 같은 시각 재조회 증분 0
    assert c["ever_overdue"] == 1 and counts(c)[5] == 1
    col, _ = cum(co, T + OVERDUE)
    assert col == {"V": 0, "M": 0, "U": 0, "N": 0}                  # overdue 는 수집 계수를 만들지 않는다
    r = co.fin("A", hm(10, 16), hm(10, 16))
    assert r["classification"] == "finalized" and r["changes"][0]["action"] == "add"
    assert "late_finish_accepted" in r["diagnostics"]["codes"]
    c = co.cohort(hm(10, 16))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 0, 1, 0, 0)
    assert (c["ever_overdue"], c["late_finish_accepted"]) == (1, 1)
    ok_equations(c)
    rec = co.rec("A")
    assert rec["bucket_start"] == hm(10, 16) and rec["inclusion"] == "open_included"
    assert (rec["overdue_first_observed_at"], rec["overdue_first_observed_mono"]) == (T + OVERDUE, mono(T + OVERDUE))


def test_overdue_uses_mono_not_wall():
    co = Co()
    co.ld.register(epoch="E1", invocation_id="A", source="bs", started_wall=T, started_mono=mono(T),
                   received_at=T, received_mono=mono(T))
    q = co.ld.cohort_snapshot(source="bs", cohort_start=hm(9, 59), cohort_end=hm(10, 1),
                              as_of=T + OVERDUE, as_of_mono=mono(T) + OVERDUE - 1)   # wall 은 15분, mono 는 1µs 모자람
    assert counts(q)[3] == 1 and q["ever_overdue"] == 0


def test_late_registered_old_start_is_overdue_at_registration():
    co = Co()
    r = co.reg("A", start=T, at=T + OVERDUE)
    # r3 §6 새 ID의 오래된 시작: 15분 늦은 최초 등록은 무삽입으로 제외한다.
    assert r["classification"] == "late_start_excluded" and r["records"] == []
    assert co.rec("A") is None and r["health"]["N_total"] == 0


def test_overdue_marks_other_records_but_not_event_diagnostics():
    co = Co()
    co.reg("A")
    co.link("A")
    r = co.reg("B", start=T + OVERDUE, at=T + OVERDUE)
    assert r["diagnostics"]["codes"] == [] and [x["invocation_id"] for x in r["records"]] == ["B"]
    assert co.rec("A")["lifecycle"] == "overdue"


# ═════════ V4 · V6 · 래퍼 종료 ═════════

def test_V4_wrapper_exit_then_late_finish():
    co = Co()
    co.reg("A")
    co.link("A")
    r = co.exit("A", hm(10, 10), hm(10, 10))
    assert r["classification"] == "wrapper_exited" and r["changes"] == []
    assert r["diagnostics"]["codes"] == ["ever_unavailable", "report_unavailable", "wrapper_exited"]
    assert r["diagnostics"]["coverage_error"] is True
    rec = co.rec("A")
    assert (rec["lifecycle"], rec["unavailable_reason"], rec["exit_evidence"]) == \
        ("report_unavailable", "wrapper_exited", "wrapper_exited")
    assert (rec["exit_evidence_wall"], rec["exit_evidence_mono"]) == (hm(10, 10), mono(hm(10, 10)))
    c = co.cohort(hm(10, 10))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 1, 0, 0, 0) and c["ever_unavailable"] == 1
    r = co.fin("A", hm(10, 9), hm(10, 11))
    assert r["classification"] == "finalized" and "late_finish_accepted" in r["diagnostics"]["codes"]
    c = co.cohort(hm(10, 11))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 0, 1, 0, 0)
    assert (c["ever_unavailable"], c["ever_overdue"], c["late_finish_accepted"]) == (1, 0, 1)
    assert co.rec("A")["bucket_start"] == hm(10, 9)


def test_V6_overdue_then_exit_then_late_finish():
    co = Co()
    co.reg("A")
    co.link("A")
    assert counts(co.cohort(T + OVERDUE))[5] == 1
    co.exit("A", hm(10, 16), hm(10, 16))
    c = co.cohort(hm(10, 16))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 1, 0, 0, 0)
    r = co.fin("A", hm(10, 14), hm(10, 17))
    assert r["classification"] == "finalized" and r["changes"][0]["action"] == "add"
    c = co.cohort(hm(10, 17))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 0, 1, 0, 0)
    assert (c["ever_overdue"], c["ever_unavailable"], c["late_finish_accepted"]) == (1, 1, 1)
    assert co.rec("A")["inclusion"] == "open_included" and co.rec("A")["bucket_start"] == hm(10, 14)


def test_duplicate_and_conflicting_wrapper_exit():
    co = Co()
    co.reg("A")
    co.link("A")
    co.exit("A", hm(10, 5), hm(10, 5))
    r = co.exit("A", hm(10, 5), hm(10, 6))
    assert r["classification"] == "duplicate_wrapper_exit" and r["diagnostics"]["codes"] == ["duplicate_wrapper_exit"]
    r = co.exit("A", hm(10, 4), hm(10, 7))
    assert "wrapper_exit_conflict" in r["diagnostics"]["codes"] and r["diagnostics"]["coverage_error"] is True
    rec = co.rec("A")
    assert rec["exit_evidence_wall"] == hm(10, 5) and rec["lifecycle"] == "report_unavailable"
    assert counts(co.cohort(hm(10, 7))) == (1, 0, 0, 0, 0, 0, 1, 0, 0, 0)


def test_wrapper_exit_after_finalized_keeps_state_without_G():
    co = Co()
    co.reg("A")
    co.link("A")
    co.fin("A", hm(10, 1), hm(10, 1))
    r = co.exit("A", hm(10, 2), hm(10, 2))
    assert r["diagnostics"]["codes"] == ["wrapper_exited"] and r["diagnostics"]["coverage_error"] is False
    rec = co.rec("A")
    assert rec["lifecycle"] == "finalized" and rec["exit_evidence"] == "wrapper_exited"


@pytest.mark.parametrize("ew,em,at", [
    (T - S1 - 1, None, T), (hm(10, 5) + 1, None, hm(10, 5)),                       # 두 축 함께
    (T, mono(T - S1) - 1, T), (hm(10, 5), mono(hm(10, 5)) + 1, hm(10, 5))],           # mono 만: 시작 이전 / 수신 이후
    ids=["both_before_start", "both_after_receipt", "mono_before_start", "mono_after_receipt"])
def test_wrapper_exit_time_integrity_rejected(ew, em, at):
    co = Co()
    co.reg("A", start=T - S1, at=T - S1)
    accepted = co.link("A", at=T - S1)
    before = co.rec("A")
    r = co.exit("A", ew, at, em=em)
    assert r["classification"] == "time_integrity_error" and r["changes"] == []
    assert r["diagnostics"]["coverage_error"] is True
    assert co.rec("A") == before
    assert (r["health"]["last_received_at"], r["health"]["last_received_mono"]) == \
        (accepted["health"]["last_received_at"], accepted["health"]["last_received_mono"])


# ═════════ V5 · V12 · 초기화 실패 ═════════

def test_V5_init_failed_and_duplicate():
    co = Co()
    co.reg("A")
    r = co.init_fail("A", hm(10, 1), hm(10, 1))
    assert r["classification"] == "report_init_failed" and r["changes"] == []
    assert r["diagnostics"]["codes"] == ["ever_unavailable", "report_init_failed", "report_unavailable"]
    rec = co.rec("A")
    assert (rec["connection"], rec["lifecycle"], rec["unavailable_reason"], rec["round_id"]) == \
        ("init_failed", "report_unavailable", "report_init_failed", None)
    assert (rec["init_failed_wall"], rec["init_failed_mono"]) == (hm(10, 1), mono(hm(10, 1)))
    r = co.init_fail("A", hm(10, 1), hm(10, 2))
    assert r["classification"] == "duplicate_init_failure" and r["diagnostics"]["codes"] == ["duplicate_init_failure"]
    c = co.cohort(hm(10, 2))
    assert counts(c) == (0, 1, 0, 0, 0, 0, 1, 0, 0, 0) and c["ever_unavailable"] == 1
    ok_equations(c)
    col, _ = cum(co, hm(11, 30))
    assert col == {"V": 0, "M": 0, "U": 0, "N": 0}


def test_init_failure_conflict_keeps_first_times():
    co = Co()
    co.reg("A")
    co.init_fail("A", hm(10, 1), hm(10, 1))
    r = co.init_fail("A", hm(10, 1, 30), hm(10, 2))
    assert "init_failure_conflict" in r["diagnostics"]["codes"]
    assert co.rec("A")["init_failed_wall"] == hm(10, 1)


def test_V12_link_after_init_failed_is_conflict_and_keeps_connection():
    co = Co()
    co.reg("A")
    co.init_fail("A", hm(10, 1), hm(10, 1))
    r = co.link("A", at=hm(10, 2))
    assert r["classification"] == "identity_conflict"
    rec = co.rec("A")
    assert (rec["connection"], rec["lifecycle"]) == ("init_failed", "conflicting")
    c = co.cohort(hm(10, 2))
    assert counts(c) == (0, 1, 0, 0, 0, 0, 0, 0, 1, 0)
    ok_equations(c)


def test_V12_init_failure_after_started_is_conflict():
    co = Co()
    co.reg("A")
    co.link("A")
    co.fin("A", hm(10, 1), hm(10, 1))
    r = co.init_fail("A", hm(10, 1), hm(10, 2))
    assert r["classification"] == "identity_conflict"
    assert r["changes"][0]["action"] == "remove"
    rec = co.rec("A")
    assert (rec["connection"], rec["lifecycle"]) == ("started", "conflicting")
    r = co.fin("A", hm(10, 1), hm(10, 3))
    assert r["classification"] == "after_conflict_redelivery"         # 복권 없음


# ═════════ V7 · V8 · 닫힌 버킷 ═════════

def test_V7_first_finish_exactly_at_close_after_overdue():
    co = Co()
    co.reg("A")
    co.link("A")
    r = co.fin("A", T, hm(11, 11))
    assert r["classification"] == "post_close_finish" and r["changes"] == []
    assert r["diagnostics"]["codes"] == ["late_finish_accepted", "post_close_finish"]
    assert co.rec("A") is None and co.ld.identity_status("A") == "tombstoned"  # r3 §6 첫 퇴출
    col, s = cum(co, hm(11, 11))
    assert col == {"V": 0, "M": 0, "U": 0, "N": 0}
    assert s["cumulative_end"] == hm(10, 1) and s["post_close_diagnostics"]["post_close_finish"] == 1
    c = co.ld.epoch_cohort_totals(as_of=hm(11, 11), as_of_mono=mono(hm(11, 11)))
    row = next(x for x in c["sources"] if x["source"] == "bs")
    assert row["lifecycle_counts"]["finalized"] == 1 and (row["ever_overdue"], row["late_finish_accepted"]) == (1, 1)  # r3 §6 epoch totals


@pytest.mark.parametrize("change", ["content", "contract"])
def test_V8_closed_conflict_keeps_cumulative(change):
    co = Co()
    co.reg("A")
    co.link("A")
    assert co.fin("A", T, hm(10, 1))["classification"] == "finalized"
    col, _ = cum(co, hm(11, 11))
    assert col["V"] == 1
    r = (co.fin("A", T, hm(11, 12), kind="M") if change == "content" else
         co.fin("A", T, hm(11, 12), contract="bank_v2_evidence/9"))
    assert r["classification"] == "post_close_conflict"  # r3 §6 tombstone 충돌은 최초 digest 로 판정
    assert r["changes"] == [] and r["diagnostics"]["cumulative_evidence_uncertain"] is True
    assert co.rec("A") is None and co.ld.identity_status("A") == "tombstoned"  # r3 §6 첫 퇴출
    col, _ = cum(co, hm(11, 12))
    assert col["V"] == 1
    assert co.fin("A", T, hm(11, 13))["classification"] == "post_close_duplicate"  # r3 §6 tombstone 재전달
    c = co.ld.epoch_cohort_totals(as_of=hm(11, 13), as_of_mono=mono(hm(11, 13)))
    row = next(x for x in c["sources"] if x["source"] == "bs")
    assert row["lifecycle_counts"]["finalized"] == 1 and row["equations_hold"] == {"connection": True, "lifecycle": True}  # r3 §6 동결 lifecycle


# ═════════ V9 · next_entry ═════════

def test_V9_next_entry_marks_previous_serial_call():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    r = co.reg("B", start=hm(10, 0, 30), job_id="j", serial=True)
    assert r["classification"] == "registered"
    assert [x["invocation_id"] for x in r["records"]] == ["A", "B"]
    ra = co.rec("A")
    assert (ra["lifecycle"], ra["unavailable_reason"], ra["exit_evidence"]) == \
        ("report_unavailable", "next_entry", "next_entry")
    assert (ra["exit_evidence_wall"], ra["exit_evidence_mono"]) == (hm(10, 0, 30), mono(hm(10, 0, 30)))
    assert r["diagnostics"]["codes"] == ["ever_unavailable", "next_entry", "report_unavailable"]
    c = co.cohort(hm(10, 1))
    assert counts(c) == (0, 0, 2, 1, 0, 0, 1, 0, 0, 0) and c["registered_invocations"] == 2
    ok_equations(c)


@pytest.mark.parametrize("kw", [{"job_id": "k", "serial": True}, {"job_id": "j", "serial": False},
                                {"job_id": None, "serial": False}])
def test_next_entry_not_inferred_without_same_serial_job(kw):
    co = Co()
    co.reg("A", job_id="j", serial=True)
    r = co.reg("B", start=hm(10, 0, 30), **kw)
    assert [x["invocation_id"] for x in r["records"]] == ["B"]
    assert co.rec("A")["lifecycle"] == "awaiting_report" and co.rec("A")["exit_evidence"] is None


def test_next_entry_not_inferred_for_other_source():
    co = Co()
    co.reg("A", job_id="j", serial=True, src="bs")
    co.reg("B", start=hm(10, 0, 30), job_id="j", serial=True, src="citi")
    assert co.rec("A")["lifecycle"] == "awaiting_report"


def test_serial_job_requires_job_id():
    co = Co()
    h0 = co.cohort(T)["health"]
    r = co.reg("A", job_id=None, serial=True, at=T)
    assert r["classification"] == "invalid_argument" and co.rec("A") is None
    for k in ("registered_records", "untracked_invocations", "unsupported_invocations", "registration_errors",
              "admission_stopped"):
        assert r["health"][k] == h0[k], k                          # 보완 C1: 슬롯·계수 불변


def test_registration_conflict_includes_job_metadata():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    r = co.reg("A", job_id="k", serial=True, at=T)
    assert r["classification"] == "registration_conflict"


# ═════════ V10 · V11 · 늦은 시작 연결 ═════════

def test_V10_late_link_after_overdue_then_finish():
    co = Co()
    co.reg("A")
    co.cohort(T + OVERDUE)
    assert co.rec("A")["lifecycle"] == "overdue"
    r = co.link("A", at=hm(10, 16))
    assert r["classification"] == "linked" and co.rec("A")["lifecycle"] == "overdue"   # link 는 overdue 를 풀지 않는다
    c = co.cohort(hm(10, 16))
    assert counts(c) == (1, 0, 0, 0, 0, 1, 0, 0, 0, 0) and c["registered_invocations"] == 1
    r = co.fin("A", hm(10, 16, 30), hm(10, 17))
    assert r["classification"] == "finalized" and "late_finish_accepted" in r["diagnostics"]["codes"]
    c = co.cohort(hm(10, 17))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 0, 1, 0, 0) and c["ever_overdue"] == 1


def test_V11_start_unrecorded_then_link_then_finish():
    co = Co()
    co.reg("A")
    assert co.fin("A", hm(10, 1), hm(10, 1))["classification"] == "start_unrecorded"
    c = co.cohort(hm(10, 1))
    assert counts(c) == (0, 0, 1, 0, 0, 0, 1, 0, 0, 0)
    co.link("A", at=hm(10, 2))
    assert co.rec("A")["connection"] == "started" and co.rec("A")["lifecycle"] == "report_unavailable"
    r = co.fin("A", hm(10, 3), hm(10, 3))
    assert r["classification"] == "finalized" and r["changes"][0]["action"] == "add"
    c = co.cohort(hm(10, 3))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 0, 1, 0, 0)
    assert (c["ever_unavailable"], c["late_finish_accepted"]) == (1, 1)


# ═════════ V13 · V14 · V15 · 종료 상한 ═════════

def test_V13_finish_mono_after_explicit_exit():
    co = Co()
    co.reg("A")
    co.link("A")
    co.exit("A", hm(10, 10), hm(10, 10))
    r = co.fin("A", hm(10, 9), hm(10, 11), fm=mono(hm(10, 10)) + 1)
    assert r["classification"] == "time_integrity_error" and r["changes"] == []
    assert {"finish_after_exit", "time_integrity_error"} <= set(r["diagnostics"]["codes"])
    assert "late_finish_accepted" not in r["diagnostics"]["codes"]
    rec = co.rec("A")
    assert rec["lifecycle"] == "report_unavailable" and rec["first_digest"] is None and rec["bucket_start"] is None
    c = co.cohort(hm(10, 11))
    assert counts(c) == (1, 0, 0, 0, 0, 0, 1, 0, 0, 0) and c["late_finish_accepted"] == 0


def test_finish_exactly_at_exit_bound_is_allowed():
    co = Co()
    co.reg("A")
    co.link("A")
    co.exit("A", hm(10, 10), hm(10, 10))
    r = co.fin("A", hm(10, 10), hm(10, 11))
    assert r["classification"] == "finalized"


def test_V14_finish_wall_after_next_entry():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    co.link("A")
    co.reg("B", start=hm(10, 0, 30), job_id="j", serial=True)
    r = co.fin("A", hm(10, 0, 30) + 1, hm(10, 0, 31), fm=mono(hm(10, 0, 29)))
    assert r["classification"] == "time_integrity_error" and "finish_after_exit" in r["diagnostics"]["codes"]
    ra = co.rec("A")
    assert ra["lifecycle"] == "report_unavailable" and ra["exit_evidence"] == "next_entry" and ra["first_digest"] is None
    c = co.cohort(hm(10, 1))
    assert counts(c) == (1, 0, 1, 1, 0, 0, 1, 0, 0, 0) and c["late_finish_accepted"] == 0


def test_V15a_earlier_exit_after_finish_is_conflict_record_only():
    co = Co()
    co.reg("A")
    co.link("A")
    co.fin("A", hm(10, 9), hm(10, 9))
    before = co.rec("A")
    r = co.exit("A", hm(10, 8), hm(10, 11))
    assert r["classification"] == "wrapper_exit_conflict" and r["changes"] == []
    assert "wrapper_exit_conflict" in r["diagnostics"]["codes"] and r["diagnostics"]["coverage_error"] is True
    rec = co.rec("A")
    assert rec["exit_evidence"] is None and rec["lifecycle"] == "finalized"
    assert rec["first_digest"] == before["first_digest"] and rec["inclusion"] == before["inclusion"]
    assert "finish_after_exit" not in rec["diagnostics"]["codes"]
    assert counts(co.cohort(hm(10, 11))) == (1, 0, 0, 0, 0, 0, 0, 1, 0, 0)


def test_V15b_earlier_next_entry_after_finish_is_conflict_record_only():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    co.link("A")
    co.fin("A", hm(10, 9), hm(10, 9))
    r = co.reg("B", start=hm(10, 8, 59), at=hm(10, 9, 30), job_id="j", serial=True)  # r3 §6 신선도 안에서 순서 역전
    assert r["classification"] == "registered"
    assert "wrapper_exit_conflict" in r["diagnostics"]["codes"] and r["diagnostics"]["coverage_error"] is True
    rec = co.rec("A")
    assert rec["exit_evidence"] is None and rec["lifecycle"] == "finalized"
    assert "wrapper_exit_conflict" in rec["diagnostics"]["codes"]
    assert counts(co.cohort(hm(10, 11), hi=hm(10, 9))) == (1, 0, 1, 1, 0, 0, 0, 1, 0, 0)


# ═════════ 등록 밖 계수 · 조회 인자 · 수신 게이트 ═════════

def test_unsupported_and_registration_error_counters_stay_outside_cohort():
    co = Co()
    r = co.reg("X", src="kb")
    assert r["classification"] == "unsupported_source" and r["health"]["unsupported_invocations"] == 1
    r = co.ld.note_registration_error(epoch="E1", source="bs", **co._rx(T))
    assert r["classification"] == "registration_error_recorded" and r["records"] == [] and r["changes"] == []
    assert r["diagnostics"]["codes"] == ["registration_error"] and r["diagnostics"]["coverage_error"] is True
    assert r["health"]["registration_errors"] == 1 and r["health"]["coverage_complete"] is False
    c = co.cohort(hm(10, 1))
    assert c["registered_invocations"] == 0 and counts(c) == (0,) * 10


def test_cohort_boundaries_left_inclusive_right_exclusive():
    co = Co()
    co.reg("A", start=hm(9, 59))
    co.reg("B", start=hm(10, 1))
    c = co.cohort(hm(10, 2), lo=hm(9, 59), hi=hm(10, 1))
    assert c["registered_invocations"] == 1
    c = co.cohort(hm(10, 2), lo=hm(9, 59), hi=hm(10, 1) + 1)
    assert c["registered_invocations"] == 2


@pytest.mark.parametrize("lo,hi,src", [(hm(10, 1), hm(10, 1), "bs"), (hm(10, 1), hm(10, 0), "bs"),
                                       (hm(9, 59), hm(10, 30), "bs"), (hm(9, 59), hm(10, 1), "kb")])
def test_cohort_snapshot_invalid_arguments(lo, hi, src):
    co = Co()
    co.reg("A")
    h0 = co.cohort(hm(10, 5))["health"]
    q = co.ld.cohort_snapshot(source=src, cohort_start=lo, cohort_end=hi, as_of=hm(10, 5), as_of_mono=mono(hm(10, 5)))
    assert q["classification"] == "invalid_argument"
    assert set(q) == {"classification", "as_of", "as_of_mono", "diagnostics", "health"}
    assert q["health"] == h0


def test_regressed_receipt_does_not_advance_overdue_then_retry():
    co = Co()
    co.reg("A")
    co.link("A")
    co.cohort(hm(10, 5))
    q = co.ld.cohort_snapshot(source="bs", cohort_start=hm(9, 59), cohort_end=hm(10, 1),
                              as_of=T + OVERDUE, as_of_mono=mono(hm(10, 5)) - 1)
    assert q["classification"] == "time_integrity_error"
    assert co.rec("A")["lifecycle"] == "in_flight"
    c = co.cohort(T + OVERDUE)
    assert counts(c)[5] == 1


def test_duplicate_finish_does_not_double_count_late_finish():
    co = Co()
    co.reg("A")
    co.link("A")
    co.cohort(T + OVERDUE)
    co.fin("A", hm(10, 16), hm(10, 16))
    r = co.fin("A", hm(10, 16), hm(10, 17))
    assert r["classification"] == "duplicate_finish" and "late_finish_accepted" not in r["diagnostics"]["codes"]
    c = co.cohort(hm(10, 17))
    assert c["late_finish_accepted"] == 1 and counts(c)[7] == 1


def test_new_public_methods_are_type_checked():
    co = Co()
    co.reg("A")
    with pytest.raises(TypeError):
        co.ld.report_init_failed(epoch="E1", invocation_id="A", failed_wall=1.0, failed_mono=mono(T),
                                 received_at=T, received_mono=mono(T))
    with pytest.raises(TypeError):
        co.ld.cohort_snapshot(source="bs", cohort_start=hm(9, 59), cohort_end=hm(10, 1), as_of=True,
                              as_of_mono=mono(T))
    with pytest.raises(TypeError):
        co.ld.register(epoch="E1", invocation_id="B", source="bs", started_wall=T, started_mono=mono(T),
                       received_at=T, received_mono=mono(T), job_id="j", serial_job=1)


def test_serial_entry_earlier_than_previous_start_is_conflict_without_next_entry():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    r = co.reg("B", start=hm(9, 59, 30), at=T, job_id="j", serial=True)       # 직전 시작(T)보다 이른 새 진입
    assert r["classification"] == "registered"
    assert "wrapper_exit_conflict" in r["diagnostics"]["codes"] and r["diagnostics"]["coverage_error"] is True
    ra = co.rec("A")
    assert ra["exit_evidence"] is None and ra["lifecycle"] == "awaiting_report"
    assert "wrapper_exit_conflict" in ra["diagnostics"]["codes"] and co.rec("B") is not None


def test_next_entry_refined_by_explicit_exit_within_bound():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    co.reg("B", start=hm(10, 5), job_id="j", serial=True)
    assert co.rec("A")["exit_evidence"] == "next_entry"
    r = co.exit("A", hm(10, 4), hm(10, 6))                                   # 실제 반환 ≤ 다음 진입 상한
    assert r["classification"] == "wrapper_exited"
    ra = co.rec("A")
    assert (ra["exit_evidence"], ra["exit_evidence_wall"], ra["exit_evidence_mono"]) == \
        ("wrapper_exited", hm(10, 4), mono(hm(10, 4)))
    assert ra["lifecycle"] == "report_unavailable" and "ever_unavailable" in ra["diagnostics"]["codes"]


def test_next_entry_refinement_later_than_bound_is_rejected():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    co.reg("B", start=hm(10, 5), job_id="j", serial=True)
    before = co.rec("A")
    r = co.exit("A", hm(10, 5) + 1, hm(10, 6))                               # 실제 반환 > 다음 진입 상한
    assert r["classification"] == "time_integrity_error" and r["changes"] == []
    assert co.rec("A") == before


@pytest.mark.parametrize("fw,fm,at", [
    (T - S1 - 1, None, T), (hm(10, 5) + 1, None, hm(10, 5)),
    (T, mono(T - S1) - 1, T), (hm(10, 5), mono(hm(10, 5)) + 1, hm(10, 5))],
    ids=["both_before_start", "both_after_receipt", "mono_before_start", "mono_after_receipt"])
def test_init_failed_time_integrity_rejected(fw, fm, at):
    co = Co()
    accepted = co.reg("A", start=T - S1, at=T - S1)
    before = co.rec("A")
    r = co.init_fail("A", fw, at, fm=fm)
    assert r["classification"] == "time_integrity_error" and r["changes"] == []
    assert r["diagnostics"]["coverage_error"] is True
    assert co.rec("A") == before
    assert (r["health"]["last_received_at"], r["health"]["last_received_mono"]) == \
        (accepted["health"]["last_received_at"], accepted["health"]["last_received_mono"])


@pytest.mark.parametrize("start,sm", [(hm(9, 59, 30), mono(T) + 1), (T + 1, mono(hm(9, 59, 30)))],
                         ids=["wall_only_earlier", "mono_only_earlier"])
def test_serial_entry_earlier_on_one_axis_is_conflict(start, sm):
    co = Co()
    co.reg("A", job_id="j", serial=True)
    r = co.reg("B", start=start, sm=sm, at=T + S1, job_id="j", serial=True)
    assert r["classification"] == "registered" and "wrapper_exit_conflict" in r["diagnostics"]["codes"]
    ra = co.rec("A")
    assert ra["exit_evidence"] is None and ra["lifecycle"] == "awaiting_report"


def test_serial_entry_after_explicit_exit_keeps_exit_evidence():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    co.exit("A", hm(10, 3), hm(10, 3))
    r = co.reg("B", start=hm(10, 5), job_id="j", serial=True)
    assert r["classification"] == "registered" and "wrapper_exit_conflict" not in r["diagnostics"]["codes"]
    ra = co.rec("A")
    assert (ra["exit_evidence"], ra["exit_evidence_wall"], ra["exit_evidence_mono"]) == \
        ("wrapper_exited", hm(10, 3), mono(hm(10, 3)))   # 더 정확한 반환시각 보존
    assert ra["unavailable_reason"] == "wrapper_exited"


@pytest.mark.parametrize("start,sm", [
    (hm(10, 2, 59), mono(hm(10, 3, 30))), (hm(10, 3, 30), mono(hm(10, 2, 59))),
    (hm(10, 2, 59), mono(hm(10, 2, 59)))],
    ids=["wall_only_earlier", "mono_only_earlier", "both_earlier"])
def test_serial_entry_earlier_than_explicit_exit_is_conflict(start, sm):
    co = Co()
    co.reg("A", job_id="j", serial=True)
    co.exit("A", hm(10, 3), hm(10, 3))
    r = co.reg("B", start=start, sm=sm, at=hm(10, 3, 30), job_id="j", serial=True)  # r3 §6 신선도 안에서 반환 이전 새 진입
    assert r["classification"] == "registered"
    assert "wrapper_exit_conflict" in r["diagnostics"]["codes"] and r["diagnostics"]["coverage_error"] is True
    ra = co.rec("A")
    assert (ra["exit_evidence"], ra["exit_evidence_wall"]) == ("wrapper_exited", hm(10, 3))
    assert "wrapper_exit_conflict" in ra["diagnostics"]["codes"]


# ───────── 변이 배터리 생존 보강(시험 잠금 뒤 추가 — Codex 재승인 대상) ─────────

def test_next_entry_requires_previous_call_to_be_serial_too():
    co = Co()
    co.reg("A", job_id="j", serial=False)                            # 같은 job 이지만 직렬 선언 없는 직전 호출
    r = co.reg("B", start=hm(10, 0, 30), job_id="j", serial=True)
    assert [x["invocation_id"] for x in r["records"]] == ["B"]
    assert co.rec("A")["lifecycle"] == "awaiting_report" and co.rec("A")["exit_evidence"] is None


def test_next_entry_not_inferred_across_non_serial_call_of_same_job():
    co = Co()
    co.reg("A", job_id="j", serial=True)
    co.reg("B", start=hm(10, 0, 20), job_id="j", serial=False)      # 같은 job 의 비직렬 호출이 사이에 있음
    r = co.reg("C", start=hm(10, 0, 40), job_id="j", serial=True)  # C 의 직전 성공 등록 호출은 B(비직렬) → 추론 없음
    assert [x["invocation_id"] for x in r["records"]] == ["C"]
    assert co.rec("A")["lifecycle"] == "awaiting_report" and co.rec("A")["exit_evidence"] is None
    assert co.rec("B")["exit_evidence"] is None
