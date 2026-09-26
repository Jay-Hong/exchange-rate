"""D7 세 번째 조각 계약 — 1분 버킷·최근 창·확정 누적·상세 해제(운영 경로 무접촉).

정본: SOURCE_HEALTH_D7_AGGREGATION.md §1·§3·§6·§7. 세부 계약은 Claude·Codex 합의본
`design/d7-aggregation/slice3_contract_r2.md`(sha256 48f5f9b019a93025faaa9d11a2daea88dcab005d3d936e03d71d8d0db7f7d11a, Codex 작성 —
r1 과 Claude 보완 C1~C5 통합, APPROVE_CONTRACT_DRAFT). 벡터 이름(E0·R1·M1·B1·P1·F1·X1…)은 그 문서의 S3.5·C1 표를 따른다.
계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex. 잠긴 ledger 시험의 기대값 변경(S3.4)은
tests/test_d7_round_ledger_contract.py 에 함께 반영한다.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg

P = ("usd-krw", "jpy-krw", "eur-krw")
SRC3 = ("investing", "bs", "citi")
S1 = 10 ** 6
MIN = 60 * S1
T = 1789984800000000                      # 2026-09-21T10:00:00Z
OFF = T - 10 ** 12


def mono(w):
    return w - OFF


def at(hh, mm, ss=0, us=0):
    return T + ((hh - 10) * 60 + mm) * MIN + ss * S1 + us


def a(status, reason):
    return {"status": status, "reason": reason}


def summary(kind="V"):
    col = a("valid", "validated") if kind == "V" else a("missing", "no_value")
    return {"collection": {p: dict(col) for p in P}, "writing": {p: a("performed", "committed") for p in P},
            "final_db": "not_checked"}


class Agg:
    def __init__(self, origin=None, **limits):
        self.origin = at(9, 59, 30) if origin is None else origin
        self.ld = lg.RoundLedger("E1", aggregation_started_at=self.origin, limits=limits or None)

    def run(self, inv, fw, rw, s=None, src="bs", rid=None, tel=False):
        rid = rid or f"r{inv}"
        schema, contract = ax.REGISTRY[src]
        start = max(self.origin, fw - S1)
        self.ld.register(epoch="E1", invocation_id=inv, source=src, started_wall=start, started_mono=mono(start),
                         received_at=rw, received_mono=mono(rw))
        self.ld.link_round(epoch="E1", invocation_id=inv, round_id=rid, report_schema=schema,
                           validity_contract=contract, received_at=rw, received_mono=mono(rw))
        return self.ld.finish(epoch="E1", invocation_id=inv, round_id=rid, report_schema=schema,
                              validity_contract=contract, finished_wall=fw, finished_mono=mono(fw),
                              selected_summary=s if s is not None else summary(), telemetry_error_present=tel,
                              received_at=rw, received_mono=mono(rw))

    def snap(self, w):
        return self.ld.aggregation_snapshot(as_of=w, as_of_mono=mono(w))


def row(rows, source="bs", pair="usd-krw"):
    hit = [r for r in rows if r["source"] == source and r["pair"] == pair]
    assert len(hit) == 1
    return hit[0]


def rrow(rows, source="bs"):
    hit = [r for r in rows if r["source"] == source]
    assert len(hit) == 1
    return hit[0]


def vmun(r):
    c = r["collection"]
    return (c["V"], c["M"], c["U"], c["N"], r["collection_rate"])


def each_pair(snap_part, expect, source="bs"):
    for p in P:
        assert vmun(row(snap_part, source, p)) == expect, p


# ───────── 출력 형태 ─────────

def test_snapshot_shape_and_rows():
    g = Agg()
    s = g.snap(at(10, 0))
    for k in ("thresholds", "classification", "as_of", "as_of_mono", "process_epoch", "aggregation_rule_version",
              "aggregation_started_at", "first_bucket_start", "first_bucket_partial", "window_start", "window_end",
              "recent", "cumulative", "recent_rounds", "cumulative_rounds", "recent_first_finished_at",
              "recent_last_finished_at", "cumulative_first_finished_at", "cumulative_last_finished_at",
              "cumulative_end", "warming_up", "coverage", "close_policy", "post_close_diagnostics", "health"):
        assert k in s, k
    assert s["classification"] == "snapshot" and s["aggregation_rule_version"] == "d7-aggregation/1"
    assert [(r["source"], r["pair"]) for r in s["recent"]] == [(x, p) for x in SRC3 for p in P]
    assert [(r["source"], r["pair"]) for r in s["cumulative"]] == [(x, p) for x in SRC3 for p in P]
    assert [r["source"] for r in s["cumulative_rounds"]] == list(SRC3)
    assert [r["source"] for r in s["recent_rounds"]] == list(SRC3)
    assert s["post_close_diagnostics"] == {"post_close_duplicate": 0, "post_close_conflict": 0, "post_close_finish": 0,
                                           "post_close_unverified": 0, "cumulative_evidence_uncertain": False}
    assert s["coverage"]["ledger_health_complete"] is True and s["coverage"]["uncertain_sources"] == []
    assert s["close_policy"] == {"window_minutes": 60, "grace_minutes": 10, "bucket_minutes": 1}
    assert s["thresholds"] == {"insufficient_evidence": 0.9}
    assert s["coverage"]["external_observation_verified"] is False and s["coverage"]["complete"] is False
    r = row(s["recent"])
    assert r["round_kind"] == "primary" and r["validity_contract"] == "bank_v2_evidence/1"
    assert r["collection_unknown_reasons"] == {} and r["writing"] == {} and r["final_db"] == {}
    assert r["determinable_ratio"] is None and r["insufficient_evidence"] is None
    assert (r["malformed_axis_items"], r["reason_unrecorded_items"]) == (0, 0)
    assert set(r) == {"source", "pair", "validity_contract", "aggregation_rule_version", "process_epoch", "round_kind",
                      "collection", "collection_rate", "determinable_ratio", "insufficient_evidence",
                      "collection_unknown_reasons", "collection_not_attempted_reasons", "writing", "final_db",
                      "malformed_axis_items", "reason_unrecorded_items"}
    rr = rrow(s["recent_rounds"])
    assert (rr["rounds"], rr["malformed_rounds"], rr["telemetry_error_rounds"], rr["partial_rounds"]) == (0, 0, 0, 0)


# ───────── S3.5 벡터 ─────────

def test_E0_empty_epoch_partial_origin():
    g = Agg(origin=at(10, 0, 30))
    s = g.snap(at(10, 0, 30))
    assert (s["window_start"], s["window_end"]) == (at(9, 0), at(10, 0))
    assert s["first_bucket_start"] == at(10, 0) and s["first_bucket_partial"] is True
    assert s["cumulative_end"] is None and s["warming_up"] is True
    each_pair(s["recent"], (0, 0, 0, 0, None))
    assert s["recent_first_finished_at"] is None and s["cumulative_first_finished_at"] is None
    assert s["health"]["retained_details"] == 0


def test_E1_empty_first_minute_closes():
    g = Agg(origin=at(10, 0, 30))
    g.snap(at(10, 0, 30))
    s = g.snap(at(11, 11))
    assert s["cumulative_end"] == at(10, 1)
    each_pair(s["cumulative"], (0, 0, 0, 0, None))


def _r1():
    g = Agg()
    assert g.run("A", at(10, 0), at(10, 0))["classification"] == "finalized"
    assert g.run("B", at(10, 30), at(10, 30), summary("M"))["classification"] == "finalized"
    return g


def test_R1_recent_before_any_close():
    s = _r1().snap(at(11, 0))
    assert (s["window_start"], s["window_end"]) == (at(10, 0), at(11, 0))
    each_pair(s["recent"], (1, 1, 0, 0, 0.5))
    assert (s["recent_first_finished_at"], s["recent_last_finished_at"]) == (at(10, 0), at(10, 30))
    assert s["cumulative_end"] is None and s["warming_up"] is False
    each_pair(s["cumulative"], (0, 0, 0, 0, None))
    assert s["health"]["retained_details"] == 2
    assert rrow(s["recent_rounds"])["rounds"] == 2 and rrow(s["cumulative_rounds"])["rounds"] == 0


def test_R2_sliding_window_does_not_accumulate():
    g = _r1()
    g.snap(at(11, 0))
    s = g.snap(at(11, 1))
    each_pair(s["recent"], (0, 1, 0, 0, 0.0))
    assert s["recent_first_finished_at"] == s["recent_last_finished_at"] == at(10, 30)
    assert rrow(s["recent_rounds"])["rounds"] == 1                # 창 이동으로 A 가 빠졌다
    assert s["cumulative_end"] is None
    each_pair(s["cumulative"], (0, 0, 0, 0, None))


def test_R3_partial_origin_bucket_closes_first():
    s = _r1().snap(at(11, 10))
    assert s["cumulative_end"] == at(10, 0)
    each_pair(s["recent"], (0, 1, 0, 0, 0.0))
    each_pair(s["cumulative"], (0, 0, 0, 0, None))
    assert s["health"]["retained_details"] == 2


def test_R4_first_result_bucket_closes_once_and_releases():
    g = _r1()
    s = g.snap(at(11, 11))
    assert s["cumulative_end"] == at(10, 1)
    each_pair(s["cumulative"], (1, 0, 0, 0, 1.0))
    each_pair(s["recent"], (0, 1, 0, 0, 0.0))
    assert s["cumulative_first_finished_at"] == s["cumulative_last_finished_at"] == at(10, 0)
    assert s["recent_first_finished_at"] == s["recent_last_finished_at"] == at(10, 30)   # A 는 창에서 빠진다
    ra, rb = g.ld.record("A"), g.ld.record("B")
    assert (ra["inclusion"], ra["detail"]) == ("frozen_included", None)
    assert rb["inclusion"] == "open_included" and rb["detail"] is not None
    assert s["health"]["retained_details"] == 1


def test_R5_both_closed_and_idempotent_requery():
    g = _r1()
    s = g.snap(at(11, 41))
    assert s["cumulative_end"] == at(10, 31)
    each_pair(s["cumulative"], (1, 1, 0, 0, 0.5))
    each_pair(s["recent"], (0, 0, 0, 0, None))
    assert (s["cumulative_first_finished_at"], s["cumulative_last_finished_at"]) == (at(10, 0), at(10, 30))
    assert s["recent_first_finished_at"] is None and s["recent_last_finished_at"] is None
    assert s["health"]["retained_details"] == 0
    assert g.ld.record("A")["detail"] is None and g.ld.record("B")["detail"] is None
    s2 = g.snap(at(11, 41))
    assert s2["cumulative"] == s["cumulative"] and s2["cumulative_end"] == s["cumulative_end"]
    assert s2["health"]["retained_details"] == 0


def test_M1_same_minute_merged_once():
    g = Agg()
    g.run("A", at(10, 0, 10), at(10, 0, 10))
    g.run("B", at(10, 0, 20), at(10, 0, 20), summary("M"))
    assert g.ld.aggregation_snapshot(as_of=at(10, 0, 20), as_of_mono=mono(at(10, 0, 20)))["health"][
        "retained_details"] == 2
    s = g.snap(at(11, 11))
    assert s["cumulative_end"] == at(10, 1)
    each_pair(s["cumulative"], (1, 1, 0, 0, 0.5))
    for p in P:
        r = row(s["cumulative"], "bs", p)
        assert r["writing"] == {"performed": 2} and r["final_db"] == {"unknown": 2}
        assert r["determinable_ratio"] == 1.0 and r["insufficient_evidence"] is False
    rr = rrow(s["cumulative_rounds"])
    assert (rr["rounds"], rr["malformed_rounds"], rr["telemetry_error_rounds"], rr["partial_rounds"]) == (2, 0, 0, 0)
    assert s["health"]["retained_details"] == 0


def test_B1_first_receipt_just_before_close():
    g = Agg()
    r = g.run("A", at(10, 0), at(11, 10, 59, 999999))
    assert r["classification"] == "finalized" and r["changes"][0]["action"] == "add"
    assert g.ld.record("A")["inclusion"] == "open_included" and r["health"]["retained_details"] == 1
    assert "late_finish_accepted" in r["diagnostics"]["codes"]                 # 등록 시작 09:59:59 → 15분 경과(S4.6)
    assert "ever_overdue" in g.ld.record("A")["diagnostics"]["codes"]
    s = g.snap(at(11, 11))
    each_pair(s["cumulative"], (1, 0, 0, 0, 1.0))
    assert g.ld.record("A")["detail"] is None and s["health"]["retained_details"] == 0


def test_B2_first_receipt_exactly_at_close():
    g = Agg()
    r = g.run("A", at(10, 0), at(11, 11))
    assert r["classification"] == "post_close_finish" and r["changes"] == []
    assert r["diagnostics"]["codes"] == ["late_finish_accepted", "post_close_finish"]    # S4.6
    assert r["diagnostics"]["coverage_error"] is True
    assert "ever_overdue" in g.ld.record("A")["diagnostics"]["codes"]
    assert r["diagnostics"]["baseline_invalidated"] is False
    assert r["health"]["coverage_complete"] is False and "bs" in r["health"]["uncertain_sources"]
    s = g.snap(at(11, 11))
    assert s["cumulative_end"] == at(10, 1)
    each_pair(s["cumulative"], (0, 0, 0, 0, None))
    assert s["post_close_diagnostics"]["post_close_finish"] == 1
    rec = g.ld.record("A")
    assert rec["inclusion"] == "post_close_excluded" and rec["closed"] and rec["detail"] is None
    assert rec["first_digest"] is not None and rec["bucket_start"] == at(10, 0)


def test_B3_minute_boundary_result_closes_one_minute_later():
    g = Agg()
    g.run("A", at(10, 1), at(10, 1))
    rec = g.ld.record("A")
    assert (rec["bucket_start"], rec["bucket_end"], rec["close_at"]) == (at(10, 1), at(10, 2), at(11, 12))
    s = g.snap(at(11, 11))
    assert s["cumulative_end"] == at(10, 1)
    each_pair(s["cumulative"], (0, 0, 0, 0, None))
    assert g.ld.record("A")["inclusion"] == "open_included" and s["health"]["retained_details"] == 1
    s = g.snap(at(11, 12))
    assert s["cumulative_end"] == at(10, 2)
    each_pair(s["cumulative"], (1, 0, 0, 0, 1.0))
    assert g.ld.record("A")["inclusion"] == "frozen_included" and s["health"]["retained_details"] == 0


def test_P1_post_close_redelivery_after_release():
    g = _r1()
    g.snap(at(11, 11))
    schema, contract = ax.REGISTRY["bs"]
    kw = dict(epoch="E1", invocation_id="A", round_id="rA", report_schema=schema, validity_contract=contract,
              finished_wall=at(10, 0), finished_mono=mono(at(10, 0)), telemetry_error_present=False,
              received_at=at(11, 12), received_mono=mono(at(11, 12)))
    r = g.ld.finish(selected_summary=summary(), **kw)
    assert r["classification"] == "post_close_duplicate" and r["changes"] == []
    kw.update(received_at=at(11, 13), received_mono=mono(at(11, 13)))
    r = g.ld.finish(selected_summary=summary("M"), **kw)
    assert r["classification"] == "post_close_conflict" and r["changes"] == []
    assert r["diagnostics"]["cumulative_evidence_uncertain"] is True
    s = g.snap(at(11, 13))
    each_pair(s["cumulative"], (1, 0, 0, 0, 1.0))
    assert s["cumulative_end"] == at(10, 3) and s["health"]["retained_details"] == 1
    assert s["post_close_diagnostics"] == {"post_close_duplicate": 1, "post_close_conflict": 1, "post_close_finish": 0,
                                           "post_close_unverified": 0, "cumulative_evidence_uncertain": True}
    assert g.ld.record("A")["detail"] is None and g.ld.record("A")["first_digest"] is not None


def test_F1_merge_failure_is_atomic_then_retry():
    g = Agg()
    g.run("A", at(10, 0), at(10, 0))
    g.snap(at(11, 10))
    before_rec = g.ld.record("A")
    before = g.snap(at(11, 10))
    before_open = g.ld.contributions_open(as_of=at(11, 10), as_of_mono=mono(at(11, 10)))
    assert before["cumulative_end"] == at(10, 0)
    g.ld._inject_cumulative_merge_failure_for_test(bucket_end=at(10, 1))
    with pytest.raises(lg.CumulativeMergeFailureForTest):
        g.snap(at(11, 11))
    assert g.ld.record("A") == before_rec
    same = g.snap(at(11, 10))
    assert same == before                                          # 실패 전후 정상 snapshot 전체가 같다
    assert g.ld.contributions_open(as_of=at(11, 10), as_of_mono=mono(at(11, 10)))["entries"] == before_open["entries"]
    assert (same["as_of"], same["health"]["retained_details"]) == (at(11, 10), 1)
    s = g.snap(at(11, 11))
    assert s["cumulative_end"] == at(10, 1)
    each_pair(s["cumulative"], (1, 0, 0, 0, 1.0))
    assert g.ld.record("A")["detail"] is None and s["health"]["retained_details"] == 0
    assert g.snap(at(11, 11))["cumulative"] == s["cumulative"]


# ───────── 보완 C1 벡터 ─────────

def _eur_malformed():
    s = summary()
    s["collection"]["eur-krw"] = {}
    return s


def test_X1_malformed_eur_integrity_rows():
    g = Agg()
    g.run("A", at(10, 0), at(10, 0), _eur_malformed())
    s = g.snap(at(11, 11))
    eur = row(s["cumulative"], "bs", "eur-krw")
    assert vmun(eur) == (0, 0, 1, 0, None)
    assert eur["determinable_ratio"] == 0.0 and eur["insufficient_evidence"] is True   # (0+0)/(0+0+1)
    assert eur["malformed_axis_items"] == 1 and eur["reason_unrecorded_items"] == 0
    assert eur["collection_unknown_reasons"] == {"report_malformed": 1}
    for p in P[:2]:
        r = row(s["cumulative"], "bs", p)
        assert vmun(r) == (1, 0, 0, 0, 1.0) and r["determinable_ratio"] == 1.0
        assert r["insufficient_evidence"] is False and r["malformed_axis_items"] == 0
    rr = rrow(s["cumulative_rounds"])
    assert (rr["rounds"], rr["malformed_rounds"], rr["partial_rounds"]) == (1, 1, 1)


def test_X2_insufficient_evidence():
    g = Agg()
    g.run("A", at(10, 0, 10), at(10, 0, 10), _eur_malformed())
    g.run("B", at(10, 0, 20), at(10, 0, 20))
    s = g.snap(at(11, 11))
    eur = row(s["cumulative"], "bs", "eur-krw")
    assert vmun(eur) == (1, 0, 1, 0, 1.0)
    assert eur["determinable_ratio"] == 0.5 and eur["insufficient_evidence"] is True
    assert eur["collection_unknown_reasons"] == {"report_malformed": 1}
    rr = rrow(s["cumulative_rounds"])
    assert (rr["rounds"], rr["malformed_rounds"], rr["partial_rounds"]) == (2, 1, 1)


def test_X3_telemetry_round_independent_of_contribution():
    g = Agg()
    g.run("A", at(10, 0), at(10, 0), tel=True)
    s = g.snap(at(11, 11))
    each_pair(s["cumulative"], (1, 0, 0, 0, 1.0))
    assert rrow(s["cumulative_rounds"])["telemetry_error_rounds"] == 1


# ───────── 원점·잠금·실패 훅 경계(C2~C4) ─────────

def test_before_aggregation_start_register_and_first_receipt():
    g = Agg(origin=at(10, 0))
    r = g.ld.register(epoch="E1", invocation_id="A", source="bs", started_wall=at(9, 59, 59),
                      started_mono=mono(at(9, 59, 59)), received_at=at(10, 0), received_mono=mono(at(10, 0)))
    assert r["classification"] == "invalid_argument"
    assert set(r["diagnostics"]["codes"]) == {"before_aggregation_start", "invalid_argument"}
    assert g.ld.record("A") is None
    g3 = Agg(origin=at(10, 0))
    r = g3.ld.register(epoch="E1", invocation_id="A", source="bs", started_wall=at(10, 0),     # 시작은 원점 — 수신만 이전
                       started_mono=mono(at(10, 0)), received_at=at(10, 0) - 1, received_mono=mono(at(10, 0) - 1))
    assert r["classification"] == "invalid_argument" and "before_aggregation_start" in r["diagnostics"]["codes"]
    assert r["health"]["last_received_at"] is None and g3.ld.record("A") is None                 # 원점 이전 최초 수신은 수락되지 않는다
    g2 = Agg(origin=at(10, 0))
    q = g2.ld.aggregation_snapshot(as_of=at(9, 59), as_of_mono=mono(at(9, 59)))
    assert q["classification"] == "invalid_argument"
    assert q["health"]["coverage_complete"] is True and q["health"]["last_received_at"] is None   # 최초 조회 거절은 Health 불변
    assert g2.snap(at(10, 0))["classification"] == "snapshot"
    q = g2.ld.aggregation_snapshot(as_of=at(9, 59), as_of_mono=mono(at(9, 59)))
    assert q["classification"] == "time_integrity_error"
    assert "before_aggregation_start" not in q["diagnostics"]["codes"]


@pytest.mark.parametrize("bad", [True, 1.0, None])
def test_origin_type(bad):
    with pytest.raises(TypeError):
        lg.RoundLedger("E1", aggregation_started_at=bad)


@pytest.mark.parametrize("bad", [-1, 2 ** 63 - 1 - 4260000000 + 1])
def test_origin_range(bad):
    with pytest.raises(ValueError):
        lg.RoundLedger("E1", aggregation_started_at=bad)


def test_large_time_jump_is_arithmetic():
    import time as _t
    g = Agg()
    g.run("A", at(10, 0), at(10, 0))
    big = 2 ** 62
    t0 = _t.perf_counter()
    s = g.snap(big)
    assert _t.perf_counter() - t0 < 5.0                            # 보완 B2: 분 단위 반복이면 끝나지 않는다
    assert s["cumulative_end"] == ((big - 70 * MIN) // MIN) * MIN
    each_pair(s["cumulative"], (1, 0, 0, 0, 1.0))


def test_merge_failure_hook_validation():
    g = Agg()
    with pytest.raises(TypeError):
        g.ld._inject_cumulative_merge_failure_for_test(bucket_end=1.0)
    with pytest.raises(ValueError):
        g.ld._inject_cumulative_merge_failure_for_test(bucket_end=at(10, 0, 30))      # 분 경계 아님
    with pytest.raises(ValueError):
        g.ld._inject_cumulative_merge_failure_for_test(bucket_end=at(9, 59))           # 원점 버킷 end 이전
    g.snap(at(11, 11))                                                                  # 10:00 버킷까지 닫힘(end=10:01)
    with pytest.raises(ValueError):
        g.ld._inject_cumulative_merge_failure_for_test(bucket_end=at(10, 1))            # 이미 닫힌 버킷(arm 없음 상태)
    g.ld._inject_cumulative_merge_failure_for_test(bucket_end=at(10, 5))
    with pytest.raises(ValueError):
        g.ld._inject_cumulative_merge_failure_for_test(bucket_end=at(10, 5))            # 중복 arm
    g.snap(at(11, 12))                                                                  # 10:05 버킷 미도달 — arm 유지
    with pytest.raises(lg.CumulativeMergeFailureForTest):
        g.snap(at(11, 15))                                                              # 10:05 버킷 닫힘에서 소비


def test_public_methods_run_under_lock():
    tree = ast.parse(Path(lg.__file__).read_text(encoding="utf-8"))
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RoundLedger"][0]
    public = {"register", "link_round", "finish", "record", "contributions_open", "aggregation_snapshot",
              "_inject_identity_fault_for_test", "_inject_cumulative_merge_failure_for_test",
              "report_init_failed", "wrapper_exited", "note_registration_error", "cohort_snapshot"}   # 네 번째 조각
    seen = set()
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in public:
            seen.add(fn.name)
            body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                                   and isinstance(fn.body[0].value, ast.Constant)
                                   and isinstance(fn.body[0].value.value, str)) else fn.body
            assert len(body) == 1, fn.name                                # 잠금 밖 처리 없음
            first = body[0]
            assert isinstance(first, ast.With) and len(first.items) == 1, fn.name
            assert ast.unparse(first.items[0].context_expr) == "self._lock" and first.items[0].optional_vars is None, fn.name
    assert seen == public
    init = [fn for fn in cls.body if isinstance(fn, ast.FunctionDef) and fn.name == "__init__"][0]
    assigns = [ast.unparse(n.value) for n in init.body if isinstance(n, ast.Assign)          # __init__ 최상위 문장만
               and any(ast.unparse(x) == "self._lock" for x in n.targets)]
    assert assigns == ["threading.RLock()"]


def test_snapshot_under_index_latch_is_rejected():
    g = Agg()
    g.run("A", at(10, 0), at(10, 0))
    g.ld._inject_identity_fault_for_test(invocation_id="A", fault="missing_owner")
    schema, contract = ax.REGISTRY["bs"]
    g.ld.finish(epoch="E1", invocation_id="A", round_id="rA", report_schema=schema, validity_contract=contract,
                finished_wall=at(10, 0), finished_mono=mono(at(10, 0)), selected_summary=summary(),
                telemetry_error_present=False, received_at=at(10, 1), received_mono=mono(at(10, 1)))
    q = g.ld.aggregation_snapshot(as_of=at(11, 11), as_of_mono=mono(at(11, 11)))
    assert set(q) == {"classification", "as_of", "as_of_mono", "diagnostics", "health"}
    assert q["classification"] == "post_close_unverified" and q["health"]["index_error"] is True
    assert "identity_unverified" in q["diagnostics"]["codes"] and q["diagnostics"]["baseline_invalidated"] is True
    assert q["diagnostics"]["uncertain_pairs"] == []
    assert (q["as_of"], q["as_of_mono"]) == (at(10, 0), mono(at(10, 0)))   # 마지막 수락 쌍(색인 오류 finish 는 수신을 진행하지 않음)


# ───────── 변이 배터리 생존 보강(시험 잠금 뒤 추가 — Codex 재승인 대상) ─────────

def test_determinable_ratio_excludes_not_attempted():
    g = Agg()
    s = summary()
    s["collection"]["usd-krw"] = a("not_attempted", "not_started")
    g.run("A", at(10, 0, 10), at(10, 0, 10), s)
    g.run("B", at(10, 0, 20), at(10, 0, 20))
    usd = row(g.snap(at(11, 11))["cumulative"], "bs", "usd-krw")
    assert vmun(usd) == (1, 0, 0, 1, 1.0)
    assert usd["determinable_ratio"] == 1.0                        # N 은 (V+M)/(V+M+U) 분모 밖
    assert usd["collection_not_attempted_reasons"] == {"not_started": 1}


def test_insufficient_evidence_boundary_exactly_threshold():
    g = Agg()
    for i in range(9):
        g.run(f"V{i}", at(10, 0, i + 1), at(10, 0, i + 1))
    g.run("U", at(10, 0, 30), at(10, 0, 30), _eur_malformed())
    eur = row(g.snap(at(11, 11))["cumulative"], "bs", "eur-krw")
    assert vmun(eur)[:4] == (9, 0, 1, 0)
    assert eur["determinable_ratio"] == 0.9 and eur["insufficient_evidence"] is False   # 0.9 미만만 true


def test_origin_on_minute_boundary_is_not_partial_nor_warming_at_60min():
    g = Agg(origin=at(10, 0))
    s = g.snap(at(11, 0))
    assert s["first_bucket_partial"] is False and s["first_bucket_start"] == at(10, 0)
    assert s["window_start"] == at(10, 0) and s["warming_up"] is False   # window_start == 원점 → 예열 아님


def test_cumulative_first_last_across_separate_closes():
    g = _r1()
    s = g.snap(at(11, 11))
    assert s["cumulative_first_finished_at"] == s["cumulative_last_finished_at"] == at(10, 0)
    s = g.snap(at(11, 41))
    assert (s["cumulative_first_finished_at"], s["cumulative_last_finished_at"]) == (at(10, 0), at(10, 30))
