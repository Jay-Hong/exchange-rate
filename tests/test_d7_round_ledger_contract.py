"""D7 두 번째 조각 계약 — 최소 등록·종료 ledger(순수 객체, 운영 경로 무접촉).

정본: SOURCE_HEALTH_D7_AGGREGATION.md §1·§5·§6(close_at 만)·§7. 세부 계약은 Claude·Codex 합의본
`design/d7-aggregation/ledger_contract_r3.md`(sha256 8b6c5fd67177a4da2dead636c1ab7cfba2a5a193cb05a406a6bb01e504b0ac49, Codex 작성·
Claude 검토 R1~R3 반영). 시험 번호 L9.xx 는 그 문서의 벡터 번호다. 계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.

## 이 시험이 고정하는 모듈 경계(계약 문서가 이름을 정하지 않은 부분)
- 모듈 `app/d7_round_ledger.py` — `RoundLedger`, `REASON_ENUM`(frozenset, 계약 L6.2 고정 reason 목록).
- Detail 은 L6.2 의 소형 사본을 첫 조각 `app.d7_round_axes.normalize_round_axes` 로 정규화한 결과다 — 시험은 기대 Detail 을
  같은 함수로 만든다(첫 조각은 이미 해시로 고정돼 있다).
- 모든 시각은 정수 µs. 벽시계·단조시계는 시험이 주입한다.
모듈 제약은 (b)를 택한다: 주입 정수만 쓰므로 time·datetime import와 모든 open 호출을 금지해 스코프별 별칭 해석의 누락을 피한다.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg

P = ("usd-krw", "jpy-krw", "eur-krw")
SRC3 = ["investing", "bs", "citi"]
REPO = Path(__file__).resolve().parents[1]

S1 = 10 ** 6                       # 1초(µs)
MIN = 60 * S1
T = 1789984800000000               # 2026-09-21T10:00:00Z
OFF = T - 10 ** 12                 # mono = wall - OFF (두 시계를 같은 속도로 움직이는 시험용 대응)
CLOSE = T + 71 * MIN               # [10:00,10:01) 의 close_at = 11:11


def mono(w):
    return w - OFF


EXPECTED_ENUM = frozenset("""
validated not_started not_observed empty_or_placeholder nan_value out_of_range parse_failed selector_missing http_403
attempt_failed attempt_interrupted cooldown mibank_untrusted_window per_currency_write_unverified not_submitted_to_writer
telemetry_error validation_rejected not_instrumented evidence_incomplete v2_evidence_unconfirmed no_value unobserved
previous_attempt_succeeded value_none equal_to_last_record committed commit_outcome_unknown commit_not_reached
guard_unrecorded staging_incomplete withheld_before_writer write_mode_uninitialized write_mode_halt reason_unrecorded
report_malformed other
""".split())

REG = {"investing": (3, "investing_range_checked/2"), "bs": (1, "bank_v2_evidence/1"), "citi": (1, "bank_v2_evidence/1")}

RECORD_KEYS = {
    "epoch", "invocation_id", "source", "seq", "started_wall", "started_mono", "expected_report_schema",
    "expected_validity_contract", "round_id", "linked_report_schema", "linked_validity_contract", "connection",
    "lifecycle", "unavailable_reason", "first_finished_wall", "first_finished_mono", "first_digest", "bucket_start",
    "bucket_end", "close_at", "closed", "inclusion", "diagnostics", "detail",
}
HEALTH_KEYS = {
    "registered_records", "retained_details", "admission_stopped", "admission_stopped_at", "untracked_invocations",
    "coverage_complete", "uncertain_sources", "clock_error", "index_error", "counter_saturated", "last_received_at",
    "last_received_mono",
}
DIAG_KEYS = {"codes", "baseline_invalidated", "coverage_error", "uncertain_pairs", "cumulative_evidence_uncertain"}


def diag(codes=(), level=None, global_=False):
    """level: None | 'G' | 'B' | 'F'. global_ 이면 uncertain_pairs=[]."""
    d = {"codes": sorted(set(codes)), "baseline_invalidated": False, "coverage_error": False,
         "uncertain_pairs": [], "cumulative_evidence_uncertain": False}
    if level:
        d["coverage_error"] = True
        d["uncertain_pairs"] = [] if global_ else list(P)
    if level in ("B", "F"):
        d["baseline_invalidated"] = True
    if level == "F":
        d["cumulative_evidence_uncertain"] = True
    return d


# ───────── fixture ─────────

def a(status, reason, **ev):
    d = {"status": status, "reason": reason}
    d.update(ev)
    return d


def S(src="bs", col=None, wr=None, final_db="not_checked"):
    if src == "investing":
        col = col or {p: a("valid", "validated") for p in P}
        wr = wr or {p: a("unknown", "per_currency_write_unverified") for p in P}
    else:
        col = col or {p: a("valid", "validated") for p in P}
        wr = wr or {p: a("performed", "committed") for p in P}
    return {"collection": col, "writing": wr, "final_db": final_db}


def clean(summary):
    """L6.2 소형 사본(시험 기대값용): 세 pair × 두 축 status/reason + final_db. 정상 fixture 에서만 쓴다."""
    out = {}
    for axis in ("collection", "writing"):
        out[axis] = {}
        for p in P:
            item = summary[axis][p]
            out[axis][p] = {"status": item["status"], "reason": item["reason"]}
    out["final_db"] = summary["final_db"]
    return out


def D(summary, src="bs", tel=False):
    schema, contract = REG[src]
    return ax.normalize_round_axes(src, schema, contract, clean(summary), tel)


class Env:
    """한 ledger 와 비감소 수신 시계. 각 호출은 명시 시각(기본: 직전 수락값 이상)을 쓴다."""

    def __init__(self, epoch="E1", **limits):
        self.epoch = epoch
        self.ld = lg.RoundLedger(epoch, limits=limits or None)
        self.now = T - 2 * S1

    def at(self, w):
        self.now = w
        return w, mono(w)

    def register(self, inv="A", src="bs", sw=T - S1, sm=None, at=None, epoch=None, rm=None):
        ra, rmm = self.at(at if at is not None else max(self.now, T - S1))
        return self.ld.register(epoch=epoch or self.epoch, invocation_id=inv, source=src, started_wall=sw,
                                started_mono=mono(sw) if sm is None else sm, received_at=ra,
                                received_mono=rmm if rm is None else rm)

    def link(self, inv="A", rid="r1", src="bs", schema=None, contract=None, at=None, epoch=None, rm=None):
        s, c = REG[src]
        ra, rmm = self.at(at if at is not None else max(self.now, T - S1 // 2))
        return self.ld.link_round(epoch=epoch or self.epoch, invocation_id=inv, round_id=rid,
                                  report_schema=s if schema is None else schema,
                                  validity_contract=c if contract is None else contract,
                                  received_at=ra, received_mono=rmm if rm is None else rm)

    def finish(self, inv="A", rid="r1", src="bs", summary=None, fw=T, fm=None, tel=False, at=None,
               schema=None, contract=None, epoch=None, rm=None, rw=None):
        s, c = REG[src]
        if at is None:
            at = max(self.now, fw)
        ra, rmm = self.at(at)
        return self.ld.finish(epoch=epoch or self.epoch, invocation_id=inv, round_id=rid,
                              report_schema=s if schema is None else schema,
                              validity_contract=c if contract is None else contract,
                              finished_wall=fw, finished_mono=mono(fw) if fm is None else fm,
                              selected_summary=S(src) if summary is None else summary,
                              telemetry_error_present=tel,
                              received_at=ra if rw is None else rw, received_mono=rmm if rm is None else rm)

    def query(self, at=None, after_seq=0, limit=16, as_of_mono=None):
        w = self.now if at is None else at
        if at is not None:
            self.now = at
        return self.ld.contributions_open(as_of=w, as_of_mono=mono(w) if as_of_mono is None else as_of_mono,
                                          after_seq=after_seq, limit=limit)

    def rec(self, inv="A"):
        return self.ld.record(inv)

    def ready(self, inv="A", rid="r1", src="bs"):
        self.register(inv, src)
        self.link(inv, rid, src)


def finalized_env(src="bs", summary=None, **limits):
    e = Env(**limits)
    e.ready(src=src)
    r = e.finish(src=src, summary=summary)
    assert r["classification"] == "finalized", r
    return e, r


def shape(r):
    assert set(r) == {"classification", "records", "changes", "diagnostics", "health"}
    assert set(r["diagnostics"]) == DIAG_KEYS and set(r["health"]) == HEALTH_KEYS
    for rec in r["records"]:
        assert set(rec) == RECORD_KEYS and set(rec["diagnostics"]) == DIAG_KEYS
    return r


def add(inv, detail):
    return {"invocation_id": inv, "action": "add", "detail": detail}


def rm(inv, detail):
    return {"invocation_id": inv, "action": "remove", "detail": detail}


# ───────── 독립 digest(L5.2·L5.3) — 시험이 스스로 계산한다 ─────────

ABSENT = object()
FIELDS = {
    ("investing", "collection"): ("status", "reason", "normalized_rate", "attempt_id", "error_type"),
    ("investing", "writing"): ("status", "reason", "attempt_ids"),
    ("bank", "collection"): ("status", "reason", "detail", "path", "attempt_id", "observation_sequences",
                             "miss_sequences", "error_type"),
    ("bank", "writing"): ("status", "reason", "detail", "path", "writer_call_id", "writer_call_ids",
                          "lost_writer_calls"),
}
ALLOWED = {
    "collection": {"valid", "missing", "unknown", "not_attempted"},
    ("investing", "writing"): {"unknown", "not_attempted"},
    ("bank", "writing"): {"performed", "no_change_needed", "policy_blocked", "not_attempted", "unknown"},
}


def C(v):
    if v is ABSENT:
        return ["absent"]
    if v is None:
        return ["null"]
    if type(v) is bool:
        return ["bool", v]
    if type(v) is int:
        return ["int", str(v)]
    if type(v) is float:
        if v != v:
            return ["float_nonfinite", "nan"]
        if v in (float("inf"), float("-inf")):
            return ["float_nonfinite", "+inf" if v > 0 else "-inf"]
        return ["float", v.hex()]
    if type(v) is str:
        b = v.encode("utf-8")
        return ["str", v] if len(b) <= 256 else ["str_hash", str(len(b)), hashlib.sha256(b).hexdigest()]
    if type(v) is list:
        return ["list", [C(x) for x in v]]
    if type(v) is dict:
        return ["dict", [[k, C(v[k])] for k in sorted(v)]]
    return ["unsupported"]


def extras(d, allowed):
    return sorted(k for k in d if k not in allowed)


def item_repr(src, axis, v):
    fam = "investing" if src == "investing" else "bank"
    fields = FIELDS[(fam, axis)]
    allowed = ALLOWED["collection"] if axis == "collection" else ALLOWED[(fam, axis)]
    if v is ABSENT or v is None or type(v) is not dict:
        return {"kind": "absent" if v is ABSENT else "null" if v is None else "not_dict", "raw": C(v)}
    st = v.get("status", ABSENT)
    if len(v) == 0:
        k = "empty_dict"
    elif st is ABSENT:
        k = "status_missing"
    elif type(st) is not str:
        k = "status_not_str"
    elif st not in allowed:
        k = "status_not_allowed"
    else:
        k = "ok"
    return {"kind": k, "fields": {f: C(v.get(f, ABSENT)) for f in fields}, "unexpected": extras(v, fields)}


def axis_repr(src, axis, m):
    if m is ABSENT or m is None or type(m) is not dict:
        return {"kind": "absent" if m is ABSENT else "null" if m is None else "not_dict", "raw": C(m)}
    return {"kind": "dict", "pairs": {p: item_repr(src, axis, m.get(p, ABSENT)) for p in P}, "unexpected": extras(m, P)}


def digest(src, rid, summary, fw=T, fm=None, tel=False):
    schema, contract = REG[src]
    obj = {
        "digest_version": "d7-ledger/2", "source": src, "round_id": rid,
        "report_schema": C(schema), "validity_contract": C(contract),
        "finished_wall": C(fw), "finished_mono": C(mono(fw) if fm is None else fm),
        "telemetry_error_present": tel,
        "summary": {"collection": axis_repr(src, "collection", summary.get("collection", ABSENT)),
                    "writing": axis_repr(src, "writing", summary.get("writing", ABSENT)),
                    "final_db": C(summary.get("final_db", ABSENT)),
                    "unexpected": extras(summary, ("collection", "writing", "final_db"))},
    }
    b = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


# ═════════ L2 — 경계·자료형 ═════════

def test_enum_is_exact():
    assert type(lg.REASON_ENUM) is frozenset and lg.REASON_ENUM == EXPECTED_ENUM


@pytest.mark.parametrize("limits", [{"max_records": 0}, {"max_records": 131073}, {"max_retained_details": 2049},
                                    {"max_detail_bytes": 4097}, {"bogus": 1}, {"max_records": True},
                                    {"max_detail_bytes": 1.0}])
def test_constructor_limit_values(limits):
    with pytest.raises(ValueError):
        lg.RoundLedger("E1", limits=limits)


def test_constructor_limit_types():
    with pytest.raises(TypeError):
        lg.RoundLedger("E1", limits=[("max_records", 1)])
    with pytest.raises(TypeError):
        lg.RoundLedger(1)


def test_constructor_limits_at_bounds_are_accepted():
    lg.RoundLedger("E1", limits={"max_records": 131072, "max_retained_details": 2048, "max_detail_bytes": 4096})
    lg.RoundLedger("E1", limits={"max_records": 1, "max_retained_details": 1, "max_detail_bytes": 1})


def test_constructor_epoch_value():
    with pytest.raises(ValueError):
        lg.RoundLedger("")


@pytest.mark.parametrize("kw", [{"started_wall": True}, {"started_wall": 1.0}, {"source": 1}, {"received_mono": None}])
def test_register_type_errors_do_not_change_state(kw):
    e = Env()
    args = dict(epoch="E1", invocation_id="A", source="bs", started_wall=T - S1, started_mono=mono(T - S1),
                received_at=T, received_mono=mono(T))
    args.update(kw)
    with pytest.raises(TypeError):
        e.ld.register(**args)
    assert e.rec("A") is None
    assert e.query(at=T)["health"]["registered_records"] == 0


def test_finish_summary_root_must_be_exact_dict():
    e = Env()
    e.ready()

    class Sub(dict):
        pass
    with pytest.raises(TypeError):
        e.finish(summary=Sub(S()))
    assert e.rec()["lifecycle"] == "in_flight"


# ═════════ L9 벡터 ═════════

def test_L9_01_register_link_finish():
    e = Env()
    r = shape(e.register())
    assert r["classification"] == "registered" and r["changes"] == [] and r["diagnostics"] == diag()
    rec = r["records"][0]
    assert (rec["lifecycle"], rec["connection"], rec["seq"], rec["round_id"], rec["inclusion"], rec["closed"]) == \
        ("awaiting_report", "unbound", 1, None, "none", False)
    assert (rec["expected_report_schema"], rec["expected_validity_contract"]) == REG["bs"]
    assert rec["detail"] is None and rec["first_digest"] is None and rec["unavailable_reason"] is None
    r = shape(e.link())
    assert r["classification"] == "linked" and r["changes"] == []
    rec = r["records"][0]
    assert (rec["lifecycle"], rec["connection"], rec["round_id"], rec["linked_report_schema"],
            rec["linked_validity_contract"]) == ("in_flight", "started", "r1", 1, "bank_v2_evidence/1")
    r = shape(e.finish())
    d = D(S())
    assert r["classification"] == "finalized" and r["changes"] == [add("A", d)] and r["diagnostics"] == diag()
    rec = r["records"][0]
    assert (rec["lifecycle"], rec["inclusion"], rec["closed"]) == ("finalized", "open_included", False)
    assert (rec["first_finished_wall"], rec["first_finished_mono"]) == (T, mono(T))
    assert (rec["bucket_start"], rec["bucket_end"], rec["close_at"]) == (T, T + MIN, CLOSE)
    assert rec["first_digest"] == digest("bs", "r1", S())
    assert rec["detail"] == d
    assert r["health"]["registered_records"] == 1 and r["health"]["retained_details"] == 1


def test_detail_shape_matches_first_slice():
    _, r = finalized_env()
    d = r["changes"][0]["detail"]
    assert set(d) == {"accepted", "source", "report_schema", "validity_contract", "pairs", "contribution", "diagnostics"}
    assert d["contribution"]["collection"] == {"V": 3, "M": 0, "U": 0, "N": 0}
    assert d["contribution"]["writing"] == {"performed": 3} and d["contribution"]["final_db"] == {"unknown": 3}
    assert d["diagnostics"]["unexpected_pairs"] == []


def test_L9_02_duplicate_and_conflicting_registration():
    e, _ = finalized_env()
    r = shape(e.register())
    assert r["classification"] == "duplicate_invocation" and r["changes"] == []
    assert r["diagnostics"] == diag(["duplicate_invocation"])
    r = shape(e.register(sm=mono(T - S1) + 1))
    assert r["classification"] == "registration_conflict"
    assert r["changes"] == [rm("A", D(S()))]
    assert r["diagnostics"] == diag(["identity_conflict", "registration_conflict"], "B")
    rec = e.rec()
    assert rec["lifecycle"] == "conflicting" and rec["started_mono"] == mono(T - S1)
    assert rec["inclusion"] == "open_excluded"


def test_L9_03_identity_conflict_both_directions():
    e = Env()
    e.ready("A", "r1")
    r = shape(e.link("A", "r2"))
    assert r["classification"] == "identity_conflict"
    assert e.rec("A")["round_id"] == "r1" and e.rec("A")["lifecycle"] == "conflicting"
    e2 = Env()
    e2.ready("A", "r1")
    e2.finish("A", "r1")
    e2.register("B")
    r = shape(e2.link("B", "r1"))
    assert r["classification"] == "identity_conflict"
    assert [x["invocation_id"] for x in r["records"]] == ["A", "B"]
    assert r["changes"] == [rm("A", D(S()))]
    assert e2.rec("A")["lifecycle"] == "conflicting" and e2.rec("B")["lifecycle"] == "conflicting"
    assert e2.rec("B")["round_id"] is None
    assert "identity_conflict" in r["diagnostics"]["codes"] and r["diagnostics"]["baseline_invalidated"]


def test_L9_03_both_finalized_then_cross_link_removes_both():
    e = Env()
    e.ready("A", "r1")
    e.ready("B", "r2")
    e.finish("A", "r1")
    e.finish("B", "r2")
    r = shape(e.link("B", "r1", at=T + S1))
    assert r["classification"] == "identity_conflict"
    assert [x["invocation_id"] for x in r["records"]] == ["A", "B"]
    assert r["changes"] == [rm("A", D(S())), rm("B", D(S()))]
    assert r["diagnostics"] == diag(["identity_conflict"], "B")
    assert (e.rec("A")["round_id"], e.rec("B")["round_id"]) == ("r1", "r2")


def test_L9_03_one_closed_record_gets_F_other_B():
    e = Env()
    e.ready("A", "r1")
    e.finish("A", "r1")
    e.query(at=CLOSE)                                            # A 닫힘
    e.ready("B", "r2")
    e.finish("B", "r2", fw=CLOSE, at=CLOSE)                      # B 는 다음 버킷에서 열림
    r = shape(e.link("B", "r1", at=CLOSE + S1))
    assert r["classification"] == "identity_conflict"
    assert r["changes"] == [rm("B", D(S()))]
    assert r["diagnostics"] == diag(["identity_conflict"], "F")  # 이벤트 = 두 레코드 진단의 합집합
    ra, rb = e.rec("A"), e.rec("B")
    assert ra["diagnostics"] == diag(["identity_conflict"], "F") and ra["inclusion"] == "frozen_included"
    assert rb["diagnostics"] == diag(["identity_conflict"], "B") and rb["inclusion"] == "open_excluded"


def test_L9_03_same_round_string_other_source_is_separate():
    e = Env()
    e.ready("A", "r1", "bs")
    e.register("B", "citi")
    r = e.link("B", "r1", "citi")
    assert r["classification"] == "linked"


def test_L9_04_relink_same_and_contract_change():
    e, _ = finalized_env()
    r = shape(e.link())
    assert r["classification"] == "relinked_same" and r["changes"] == [] and r["diagnostics"] == diag(["relinked_same"])
    r = shape(e.link(contract="bank_v2_evidence/9"))
    assert r["classification"] == "contract_mixed"
    assert r["changes"] == [rm("A", D(S()))]
    assert r["diagnostics"] == diag(["contract_mixed", "unregistered_contract"], "B")
    rec = e.rec()
    assert rec["lifecycle"] == "contract_mixed" and rec["linked_validity_contract"] == "bank_v2_evidence/1"


def test_L9_05_unregistered_contract_on_unbound():
    e = Env()
    e.register()
    r = shape(e.link(schema=99))
    assert r["classification"] == "unregistered_contract" and r["changes"] == []
    assert r["diagnostics"] == diag(["unregistered_contract", "ever_unavailable"], "G")
    rec = e.rec()
    assert (rec["lifecycle"], rec["unavailable_reason"], rec["connection"], rec["round_id"]) == \
        ("report_unavailable", "unregistered_contract", "unbound", None)


def test_L9_06_orphan_and_start_unrecorded():
    e = Env()
    r = shape(e.finish("Z", "r1"))
    assert r["classification"] == "orphan" and r["records"] == [] and r["changes"] == []
    assert r["diagnostics"] == diag(["orphan_finish"], "G", global_=True)
    e.register()
    r = shape(e.finish())
    assert r["classification"] == "start_unrecorded" and r["changes"] == []
    assert r["diagnostics"] == diag(["report_unavailable", "start_unrecorded", "ever_unavailable"], "G")
    rec = e.rec()
    assert (rec["lifecycle"], rec["unavailable_reason"], rec["first_digest"], rec["bucket_start"]) == \
        ("report_unavailable", "start_unrecorded", None, None)


def test_L9_06_orphan_link():
    e = Env()
    r = shape(e.link("Z"))
    assert r["classification"] == "orphan" and r["records"] == []
    assert r["diagnostics"] == diag(["orphan_start"], "G", global_=True)


def test_L9_07_late_link_then_finish_recovers():
    e = Env()
    e.register()
    e.finish()
    r = e.link(at=T)
    assert r["classification"] == "linked"
    rec = e.rec()
    assert rec["lifecycle"] == "report_unavailable" and rec["connection"] == "started"
    r = shape(e.finish(at=T + S1))
    assert r["classification"] == "finalized" and r["changes"] == [add("A", D(S()))]
    assert "late_finish_accepted" in r["diagnostics"]["codes"]
    rec = e.rec()
    assert rec["lifecycle"] == "finalized"
    assert {"ever_unavailable", "late_finish_accepted"} <= set(rec["diagnostics"]["codes"])


def test_L9_08_foreign_epoch():
    e, _ = finalized_env()
    before = e.rec()
    h = e.query(at=T)["health"]
    r = shape(e.finish(epoch="E2", at=T + S1))
    assert r["classification"] == "foreign_epoch" and r["records"] == [] and r["changes"] == []
    assert "foreign_epoch" in r["diagnostics"]["codes"]
    assert e.rec() == before and r["health"] == h


def test_L9_09_minute_boundary_goes_to_next_bucket():
    e = Env()
    e.ready()
    e.finish(fw=T + MIN, at=T + MIN)
    rec = e.rec()
    assert (rec["bucket_start"], rec["bucket_end"], rec["close_at"]) == (T + MIN, T + 2 * MIN, T + 72 * MIN)


def test_L9_10_first_receipt_just_before_close():
    e = Env()
    e.ready()
    r = e.finish(at=CLOSE - 1)
    assert r["classification"] == "finalized" and r["changes"] == [add("A", D(S()))]


def test_L9_10_first_receipt_at_close():
    e = Env()
    e.ready()
    r = shape(e.finish(at=CLOSE))
    assert r["classification"] == "post_close_finish" and r["changes"] == []
    assert r["diagnostics"] == diag(["post_close_finish"], "G")
    rec = e.rec()
    assert (rec["lifecycle"], rec["inclusion"], rec["closed"], rec["detail"]) == \
        ("finalized", "post_close_excluded", True, None)
    assert rec["first_digest"] == digest("bs", "r1", S())


def test_L9_11_open_duplicate():
    e, _ = finalized_env()
    r = shape(e.finish(at=T + 5 * S1))
    assert r["classification"] == "duplicate_finish" and r["changes"] == []
    assert r["diagnostics"] == diag(["duplicate_finish"])
    assert e.rec()["lifecycle"] == "finalized" and e.rec()["inclusion"] == "open_included"


def test_L9_12_post_close_duplicate():
    e, _ = finalized_env()
    r = shape(e.finish(at=CLOSE))
    assert r["classification"] == "post_close_duplicate" and r["changes"] == []
    assert r["diagnostics"] == diag(["duplicate_finish", "post_close_duplicate"])
    rec = e.rec()
    assert rec["inclusion"] == "frozen_included" and rec["closed"] and rec["detail"] == D(S())


def _bank_evidence_variants():
    base = S()
    out = []
    for axis, field, val in [("collection", "detail", "non_finite"), ("collection", "path", "mibank"),
                             ("collection", "attempt_id", 2), ("writing", "writer_call_id", "r1:2"),
                             ("writing", "lost_writer_calls", 1)]:
        s = copy.deepcopy(base)
        s[axis]["usd-krw"][field] = val
        out.append(pytest.param(s, id=f"{axis}.{field}"))
    return out


@pytest.mark.parametrize("changed", _bank_evidence_variants())
def test_L9_13_evidence_change_is_conflict(changed):
    e, _ = finalized_env()
    assert D(changed) == D(S())                                      # 기여는 같다
    r = shape(e.finish(summary=changed, at=T + S1))
    assert r["classification"] == "conflicting_finish" and r["changes"] == [rm("A", D(S()))]
    assert r["diagnostics"] == diag(["conflicting_finish"], "B")
    assert e.rec()["lifecycle"] == "conflicting" and e.rec()["inclusion"] == "open_excluded"


def test_L9_13_investing_normalized_rate_change_is_conflict():
    s0 = S("investing")
    s0["collection"]["usd-krw"]["normalized_rate"] = 1390.5
    e, _ = finalized_env("investing", s0)
    s1 = copy.deepcopy(s0)
    s1["collection"]["usd-krw"]["normalized_rate"] = 1390.6
    r = e.finish(src="investing", summary=s1, at=T + S1)
    assert r["classification"] == "conflicting_finish"


@pytest.mark.parametrize("fw,fm", [(T + 1, mono(T)), (T, mono(T) + 1)], ids=["wall_only", "mono_only"])
def test_L9_14_time_only_change_is_conflict(fw, fm):
    e, _ = finalized_env()
    r = shape(e.finish(fw=fw, fm=fm, at=T + S1))
    assert r["classification"] == "conflicting_finish" and r["changes"] == [rm("A", D(S()))]
    rec = e.rec()
    assert (rec["first_finished_wall"], rec["first_finished_mono"], rec["bucket_start"], rec["close_at"]) == \
        (T, mono(T), T, CLOSE)


def _changed():
    s = S()
    s["collection"]["usd-krw"] = a("missing", "no_value")
    return s


def test_L9_15_closed_conflict_then_original():
    e, _ = finalized_env()
    r = shape(e.finish(summary=_changed(), at=CLOSE))
    assert r["classification"] == "post_close_conflict" and r["changes"] == []
    assert r["diagnostics"] == diag(["post_close_conflict"], "F")
    rec = e.rec()
    assert rec["lifecycle"] == "conflicting" and rec["inclusion"] == "frozen_included" and rec["detail"] == D(S())
    r = shape(e.finish(at=CLOSE + S1))
    assert r["classification"] == "after_conflict_redelivery" and r["changes"] == []
    assert r["diagnostics"] == diag(["after_conflict_redelivery"], "F")
    assert e.rec()["inclusion"] == "frozen_included" and e.rec()["detail"] == D(S())


def test_L9_16_open_conflict_then_original_no_reinstatement():
    e, _ = finalized_env()
    e.finish(summary=_changed(), at=T + S1)
    r = shape(e.finish(at=T + 2 * S1))
    assert r["classification"] == "after_conflict_redelivery" and r["changes"] == []
    assert r["diagnostics"] == diag(["after_conflict_redelivery"], "B")
    rec = e.rec()
    assert rec["lifecycle"] == "conflicting" and rec["inclusion"] == "open_excluded"


def test_L9_17_closed_contract_change():
    e, _ = finalized_env()
    r = shape(e.finish(contract="bank_v2_evidence/9", at=CLOSE))
    assert r["classification"] == "contract_mixed" and r["changes"] == []
    assert r["diagnostics"] == diag(["contract_mixed", "post_close_conflict", "unregistered_contract"], "F")
    rec = e.rec()
    assert rec["lifecycle"] == "contract_mixed" and rec["inclusion"] == "frozen_included" and rec["detail"] == D(S())


def test_L9_18_open_contract_change():
    e, _ = finalized_env()
    r = shape(e.finish(contract="bank_v2_evidence/9", at=T + S1))
    assert r["classification"] == "contract_mixed" and r["changes"] == [rm("A", D(S()))]
    assert r["diagnostics"] == diag(["conflicting_finish", "contract_mixed", "unregistered_contract"], "B")
    assert e.rec()["lifecycle"] == "contract_mixed"


def test_L9_19_first_finish_mono_before_start():
    e = Env()
    e.ready()
    r = shape(e.finish(fm=mono(T - S1) - 1))
    assert r["classification"] == "time_integrity_error" and r["changes"] == []
    assert r["diagnostics"] == diag(["time_integrity_error", "finish_mono_before_start", "ever_unavailable"], "G")
    rec = e.rec()
    assert (rec["lifecycle"], rec["unavailable_reason"], rec["first_digest"]) == \
        ("report_unavailable", "time_integrity_error", None)


def test_L9_19_first_finish_wall_in_future():
    e = Env()
    e.ready()
    r = shape(e.finish(fw=T + S1, fm=mono(T), at=T))                 # mono 는 정상, wall 만 미래
    assert r["classification"] == "time_integrity_error"
    assert r["diagnostics"] == diag(["time_integrity_error", "finish_wall_in_future", "ever_unavailable"], "G")
    assert e.rec()["first_digest"] is None


def test_first_finish_both_clocks_in_future_reports_both():
    e = Env()
    e.ready()
    r = e.finish(fw=T + S1, at=T)
    assert r["diagnostics"]["codes"] == sorted(["ever_unavailable", "finish_mono_in_future", "finish_wall_in_future",
                                                "time_integrity_error"])


def test_L9_19_first_finish_receipt_mono_regressed_is_gate_rejected():
    e = Env()
    e.ready()
    before = e.rec()
    h0 = e.query()["health"]
    r = shape(e.finish(at=T, rm=mono(T - S1) - 1))
    assert r["classification"] == "time_integrity_error" and r["records"] == [] and r["changes"] == []
    assert r["diagnostics"] == diag(["receipt_mono_regressed", "time_integrity_error"], "G", global_=True)
    assert r["health"]["clock_error"] is True and r["health"]["coverage_complete"] is False
    assert r["health"]["uncertain_sources"] == SRC3
    assert (r["health"]["last_received_at"], r["health"]["last_received_mono"]) == \
        (h0["last_received_at"], h0["last_received_mono"])
    assert e.rec() == before


def test_L9_20_changed_finish_mono_before_start():
    e, _ = finalized_env()
    r = shape(e.finish(fm=mono(T - S1) - 1, at=T + S1))
    assert r["classification"] == "conflicting_finish" and r["changes"] == [rm("A", D(S()))]
    assert r["diagnostics"] == diag(["conflicting_finish", "finish_mono_before_start", "time_integrity_error"], "B")
    assert e.rec()["lifecycle"] == "conflicting"


def _state_env(state):
    if state == "capacity":
        e = Env(max_detail_bytes=1)
        e.ready()
        e.finish()
    else:
        e, _ = finalized_env()
        if state == "conflicting":
            e.finish(summary=_changed(), at=T + S1)
        elif state == "contract_mixed":
            e.finish(contract="bank_v2_evidence/9", at=T + S1)
    return e


@pytest.mark.parametrize("state", ["finalized", "conflicting", "contract_mixed", "capacity"])
@pytest.mark.parametrize("which", ["wall", "mono", "both"])
def test_L9_21_closed_then_regressed_receipt_redelivery(which, state):
    e = _state_env(state)
    e.query(at=CLOSE)
    before = e.rec()
    h0 = e.query()["health"]
    rw = CLOSE - S1 if which in ("wall", "both") else CLOSE
    rmo = mono(CLOSE) - S1 if which in ("mono", "both") else mono(CLOSE)
    r = shape(e.ld.finish(epoch="E1", invocation_id="A", round_id="r1", report_schema=1,
                          validity_contract="bank_v2_evidence/1", finished_wall=T, finished_mono=mono(T),
                          selected_summary=S(), telemetry_error_present=False, received_at=rw, received_mono=rmo))
    codes = {"wall": ["receipt_wall_regressed"], "mono": ["receipt_mono_regressed"],
             "both": ["receipt_mono_regressed", "receipt_wall_regressed"]}[which]
    assert r["classification"] == "time_integrity_error" and r["records"] == [] and r["changes"] == []
    assert r["diagnostics"] == diag(codes + ["time_integrity_error"], "G", global_=True)
    assert e.rec() == before and before["closed"] is True
    assert (r["health"]["last_received_at"], r["health"]["last_received_mono"]) == \
        (h0["last_received_at"], h0["last_received_mono"])
    expect = {"finalized": "finalized", "conflicting": "conflicting", "contract_mixed": "contract_mixed",
              "capacity": "report_unavailable"}[state]
    assert before["lifecycle"] == expect


def test_L9_22_other_invocation_older_finish_is_fine():
    e = Env()
    e.ready("A", "r1")
    e.ready("B", "r2")
    ra = e.finish("A", "r1", fw=T + 30 * S1, at=T + 40 * S1)
    rb = e.finish("B", "r2", fw=T, at=T + 41 * S1)
    assert ra["classification"] == rb["classification"] == "finalized"
    assert "time_integrity_error" not in rb["diagnostics"]["codes"]
    assert e.rec("B")["bucket_start"] == T


def test_L9_23_query_closes_without_event():
    e, _ = finalized_env()
    q = e.query(at=CLOSE)
    assert set(q) == {"classification", "as_of", "as_of_mono", "entries", "next_seq", "diagnostics", "health"}
    assert q["classification"] == "snapshot" and q["entries"] == [] and q["next_seq"] is None
    assert (q["as_of"], q["as_of_mono"]) == (CLOSE, mono(CLOSE))
    rec = e.rec()
    assert rec["closed"] and rec["inclusion"] == "frozen_included" and q["health"]["retained_details"] == 1


def test_query_open_entry_before_close():
    e, _ = finalized_env()
    q = e.query(at=CLOSE - 1)
    assert q["entries"] == [{"seq": 1, "invocation_id": "A", "detail": D(S())}]


def test_L9_24_single_axis_malformed():
    s = S()
    s["collection"]["eur-krw"] = {}
    e, r = finalized_env(summary=s)
    d = r["changes"][0]["detail"]
    assert d["contribution"]["collection"] == {"V": 2, "M": 0, "U": 1, "N": 0}
    assert d["contribution"]["writing"] == {"performed": 3} and d["contribution"]["final_db"] == {"unknown": 3}
    assert d["diagnostics"]["malformed"] == [["eur-krw", "collection"]]
    assert "report_malformed" in r["diagnostics"]["codes"]
    assert r["diagnostics"]["coverage_error"] is False


def test_L9_25_long_reason_keeps_status():
    s = S()
    s["collection"]["eur-krw"]["reason"] = "x" * 257
    _, r = finalized_env(summary=s)
    d = r["changes"][0]["detail"]
    assert d["pairs"]["eur-krw"]["collection"] == {"status": "valid", "reason": "other"}
    assert d["contribution"]["collection"]["V"] == 3 and d["diagnostics"]["malformed_axis_items"] == 0
    assert r["diagnostics"]["codes"] == ["reason_other", "reason_oversize"]


def test_reason_outside_enum_becomes_other_without_oversize():
    s = S()
    s["collection"]["eur-krw"] = a("missing", "brand_new_reason")
    _, r = finalized_env(summary=s)
    assert r["changes"][0]["detail"]["pairs"]["eur-krw"]["collection"] == {"status": "missing", "reason": "other"}
    assert r["diagnostics"]["codes"] == ["reason_other"]


@pytest.mark.parametrize("reason,out_reason,unrec", [
    (ABSENT, "reason_unrecorded", True), (None, "reason_unrecorded", True), ("", "reason_unrecorded", True),
    ("reason_unrecorded", "reason_unrecorded", False)])
def test_L9_26_reason_unrecorded_variants(reason, out_reason, unrec):
    s = S()
    item = {"status": "valid"} if reason is ABSENT else a("valid", reason)
    s["collection"]["usd-krw"] = item
    _, r = finalized_env(summary=s)
    d = r["changes"][0]["detail"]
    assert d["pairs"]["usd-krw"]["collection"] == {"status": "valid", "reason": out_reason}
    assert (d["diagnostics"]["reason_unrecorded"] == [["usd-krw", "collection"]]) is unrec
    assert ("reason_unrecorded" in r["diagnostics"]["codes"]) is unrec


def test_L9_26_reason_unrecorded_variants_have_distinct_digests():
    vs = []
    for reason in (ABSENT, None, "", "reason_unrecorded"):
        s = S()
        s["collection"]["usd-krw"] = {"status": "valid"} if reason is ABSENT else a("valid", reason)
        vs.append(_digest_via_ledger(s))
    assert len(set(vs)) == 4


# L9.27 — L5.4 동치 표를 실제 ledger 의 first_digest 로 확인한다(시험 digest 와도 일치해야 한다).

def _digest_via_ledger(summary, src="bs"):
    e = Env()
    e.ready(src=src)
    r = e.finish(src=src, summary=summary)
    assert r["classification"] == "finalized", r
    got = e.rec()["first_digest"]
    assert got == digest(src, "r1", summary)
    return got


def _with(item_value, axis="collection", pair="usd-krw", src="bs"):
    s = S(src)
    if item_value is ABSENT:
        del s[axis][pair]
    else:
        s[axis][pair] = item_value
    return s


def test_L9_27_malformed_item_kinds_all_distinct():
    ds = [_digest_via_ledger(_with(v)) for v in (ABSENT, None, {}, {"reason": "x"}, {"reason": "y"})]
    assert len(set(ds)) == 5


@pytest.mark.parametrize("v", [ABSENT, None, {}, {"reason": "x"}, {"status": 1, "reason": "r"},
                               {"status": "bad-a", "reason": "r"}, "str"], ids=repr)
def test_L9_27_malformed_item_contribution_and_diagnostics(v):
    e = Env()
    e.ready()
    r = e.finish(summary=_with(v))
    d = r["changes"][0]["detail"]
    assert d["pairs"]["usd-krw"]["collection"] == {"status": "unknown", "reason": "report_malformed"}
    assert d["contribution"]["collection"] == {"V": 2, "M": 0, "U": 1, "N": 0}
    assert d["contribution"]["collection_unknown_reasons"] == {"report_malformed": 1}
    assert d["diagnostics"]["malformed"] == [["usd-krw", "collection"]]
    assert r["diagnostics"] == diag(["report_malformed"])


def test_L9_27_long_reasons_differ_in_digest_same_contribution():
    s1, s2 = S(), S()
    s1["collection"]["usd-krw"]["reason"] = "x" * 300
    s2["collection"]["usd-krw"]["reason"] = "y" * 300
    assert _digest_via_ledger(s1) != _digest_via_ledger(s2)
    e = Env()
    e.ready()
    d = e.finish(summary=s1)["changes"][0]["detail"]
    assert d["pairs"]["usd-krw"]["collection"] == {"status": "valid", "reason": "other"}


@pytest.mark.parametrize("field", ["observation_sequences", "writer_call_ids"])
def test_L9_27_evidence_absent_null_empty_distinct(field):
    axis = "collection" if field == "observation_sequences" else "writing"
    ds = []
    for v in (ABSENT, None, []):
        s = S()
        if v is not ABSENT:
            s[axis]["usd-krw"][field] = v
        ds.append(_digest_via_ledger(s))
    assert len(set(ds)) == 3


def test_L9_27_status_types_and_values_distinct():
    ds = [_digest_via_ledger(_with({"status": v, "reason": "r"})) for v in (1, "1", True, "bad-a", "bad-b")]
    assert len(set(ds)) == 5
    assert _digest_via_ledger(_with({"status": "bad-a", "reason": "r"})) == ds[3]


def test_L9_27_final_db_variants():
    ds = []
    for v in (ABSENT, None, "other", "not_checked"):
        s = S()
        if v is ABSENT:
            del s["final_db"]
        else:
            s["final_db"] = v
        ds.append(_digest_via_ledger(s))
    assert len(set(ds)) == 4


def test_L9_27_evidence_and_numbers():
    base = S("investing")
    vals = []
    for v in (ABSENT, None, 1, 1.0, "1", True, float("nan"), float("inf"), -0.0, 0.0):
        s = copy.deepcopy(base)
        if v is not ABSENT:
            s["collection"]["usd-krw"]["normalized_rate"] = v
        vals.append(_digest_via_ledger(s, "investing"))
    assert len(set(vals)) == len(vals)
    s1, s2 = copy.deepcopy(base), copy.deepcopy(base)
    s1["collection"]["usd-krw"]["normalized_rate"] = float("nan")
    s2["collection"]["usd-krw"]["normalized_rate"] = -float("nan")
    assert _digest_via_ledger(s1, "investing") == _digest_via_ledger(s2, "investing")


def test_L9_27_lists_order_and_dict_order():
    ds = []
    for v in ([1, 2], [2, 1], [1, 1, 2], []):
        s = S()
        s["collection"]["usd-krw"]["observation_sequences"] = v
        ds.append(_digest_via_ledger(s))
    assert len(set(ds)) == 4
    s1 = S()
    s2 = {"final_db": "not_checked", "writing": S()["writing"],
          "collection": {p: {"reason": "validated", "status": "valid"} for p in reversed(P)}}
    assert _digest_via_ledger(s1) == _digest_via_ledger(s2)


def test_L9_27_unexpected_keys():
    base = _digest_via_ledger(S())
    s = S()
    s["collection"]["usd-krw"]["zzz"] = 1
    d1 = _digest_via_ledger(s)
    s["collection"]["usd-krw"]["zzz"] = "완전히 다른 값"
    assert _digest_via_ledger(s) == d1 != base
    s2 = S()
    s2["collection"]["usd-krw"]["yyy"] = 1
    assert _digest_via_ledger(s2) not in (d1, base)
    s3 = S()
    s3["extra_root"] = 1
    e = Env()
    e.ready()
    r = e.finish(summary=s3)
    assert r["classification"] == "finalized" and "unexpected_keys" in r["diagnostics"]["codes"]
    assert r["changes"][0]["detail"] == D(S())


def test_L9_27_unsupported_values_are_equal():
    class X:
        def __repr__(self):
            raise AssertionError("repr 호출 금지")

        def __str__(self):
            raise AssertionError("str 호출 금지")
    s1, s2 = S(), S()
    s1["collection"]["usd-krw"]["detail"] = X()
    s2["collection"]["usd-krw"]["detail"] = (1, 2)
    d1 = _digest_via_ledger(s1)
    assert d1 == _digest_via_ledger(s2)
    e = Env()
    e.ready()
    r = e.finish(summary=s1)
    assert "evidence_malformed" in r["diagnostics"]["codes"]
    assert r["changes"][0]["detail"]["pairs"]["usd-krw"]["collection"]["status"] == "valid"


def test_L9_27_different_digest_on_open_redelivery_is_conflict():
    e, _ = finalized_env()
    s = S()
    s["collection"]["usd-krw"]["zzz"] = 1                          # 이름 추가 → digest 다름
    assert e.finish(summary=s, at=T + S1)["classification"] == "conflicting_finish"


def test_digest_is_exact_for_basic_fixture_and_investing():
    assert _digest_via_ledger(S()) == digest("bs", "r1", S())
    s = S("investing")
    s["collection"]["usd-krw"].update(normalized_rate=1390.25, attempt_id=1)
    s["writing"]["usd-krw"]["attempt_ids"] = [1, 2]
    _digest_via_ledger(s, "investing")


def test_digest_uses_candidate_time_on_redelivery():
    e, _ = finalized_env()
    e.finish(fw=T + 1, at=T + S1)
    assert e.rec()["first_digest"] == digest("bs", "r1", S())


# ───────── L6 사전 입력 상한(한도 정확히 = 통과, +1 = 격리) ─────────

def _limit_case(summary, code, kind="input_limit_exceeded"):
    e = Env()
    e.ready()
    r = shape(e.finish(summary=summary))
    assert r["classification"] == kind, r["diagnostics"]
    reason = "input_limit" if kind == "input_limit_exceeded" else "input_shape"
    assert r["changes"] == []
    assert r["diagnostics"] == diag([kind, code, "report_unavailable", "ever_unavailable"], "G")
    rec = e.rec()
    assert (rec["lifecycle"], rec["unavailable_reason"], rec["first_digest"], rec["detail"]) == \
        ("report_unavailable", reason, None, None)
    return e


def _ok(summary):
    e = Env()
    e.ready()
    r = e.finish(summary=summary)
    assert r["classification"] == "finalized", r["diagnostics"]


def _keys(n):
    s = S()
    item = s["collection"]["usd-krw"]
    for i in range(n - 2):
        item[f"k{i:02d}"] = 0
    return s


def test_L9_28_dict_keys_limit():
    _ok(_keys(32))
    _limit_case(_keys(33), "dict_keys_limit")


def test_L9_28_key_bytes_limit():
    s = S()
    s["collection"]["usd-krw"]["k" * 128] = 0
    _ok(s)
    s = S()
    s["collection"]["usd-krw"]["k" * 129] = 0
    _limit_case(s, "key_bytes_limit")


def test_L9_28_string_bytes_limit():
    s = S()
    s["collection"]["usd-krw"]["detail"] = "가" * 1365 + "x"     # 4096 bytes
    _ok(s)
    s["collection"]["usd-krw"]["detail"] = "가" * 1365 + "xy"    # 4097 bytes
    _limit_case(s, "string_bytes_limit")


def test_L9_28_reason_4097_bytes_isolates_report():
    s = S()
    s["collection"]["eur-krw"]["reason"] = "x" * 4097
    _limit_case(s, "string_bytes_limit")


def test_L9_28_list_length_limit():
    s = S()
    s["collection"]["usd-krw"]["observation_sequences"] = list(range(1, 33))
    _ok(s)
    s["collection"]["usd-krw"]["observation_sequences"] = list(range(1, 34))
    _limit_case(s, "list_length_limit")


def test_L9_28_total_list_items_limit():
    s = S()
    for i, p in enumerate(P):
        s["collection"][p]["observation_sequences"] = list(range(1, 33))
        s["collection"][p]["miss_sequences"] = list(range(1, 33)) if i < 1 else []
    _ok(s)                                                          # 32*3 + 32 = 128
    s["collection"]["jpy-krw"]["miss_sequences"] = [1]
    _limit_case(s, "total_list_items_limit")


def _nest(levels):
    v = 1
    for _ in range(levels):
        v = {"a": v}
    return v


def test_L9_28_depth_limit():
    s = S()
    s["collection"]["usd-krw"]["zz"] = _nest(3)                    # 1 이 depth 6
    _ok(s)
    s["collection"]["usd-krw"]["zz"] = _nest(4)                    # depth 7
    _limit_case(s, "depth_limit")


def _nodes(extra_keys):
    """기본 S 는 22 node·21 key. 목록 4개(각 32 원소)는 128 node, 나머지 extra_keys-4 개는 스칼라 값."""
    s = S()
    targets = [s, s["collection"], s["writing"], s["collection"]["usd-krw"]]
    room = [29, 29, 29, 30]
    lists = 4
    n = 0
    for t, cap in zip(targets, room):
        for _ in range(cap):
            if n == extra_keys:
                return s
            t[f"q{n:03d}"] = list(range(32)) if lists > 0 else 0
            lists -= 1
            n += 1
    assert n == extra_keys
    return s


def test_L9_28_nodes_limit():
    _ok(_nodes(106))                                               # 22 + 106 + 128 = 256 node, 127 key
    _limit_case(_nodes(107), "nodes_limit")                        # 257 node, 128 key


def test_total_keys_limit():
    s = S()
    targets = [s, s["collection"], s["writing"], s["collection"]["usd-krw"], s["collection"]["jpy-krw"]]
    n = 0
    for t, cap in zip(targets, [29, 29, 29, 30, 30]):
        for _ in range(cap):
            if n < 107:
                t[f"q{n:03d}"] = 0
                n += 1
    _ok(s)                                                         # 21 + 107 = 128
    s["collection"]["jpy-krw"]["q999"] = 0
    _limit_case(s, "total_keys_limit")


def test_total_string_bytes_limit():
    s = S()
    base = sum(len(k.encode()) for k in ("collection", "writing", "final_db")) + len("not_checked")
    for axis in ("collection", "writing"):
        base += sum(len(p) for p in P)
        for p in P:
            base += len("status") + len("reason") + len(s[axis][p]["status"]) + len(s[axis][p]["reason"])
    budget = 16384 - base
    fill = []
    for i, p in enumerate(P):
        room = min(4096, budget - sum(fill) - len("detail"))
        if room <= 0:
            break
        s["collection"][p]["detail"] = "d" * room
        fill.append(room + len("detail"))
    s["writing"]["usd-krw"]["detail"] = "e" * (budget - sum(fill) - len("detail"))
    _ok(s)                                                         # 정확히 16384
    s["writing"]["usd-krw"]["detail"] += "e"
    _limit_case(s, "total_string_bytes_limit")


def test_integer_bits_limit():
    s = S()
    s["collection"]["usd-krw"]["attempt_id"] = 2 ** 63
    _ok(s)                                                         # bit_length 64
    s["collection"]["usd-krw"]["attempt_id"] = 2 ** 64
    _limit_case(s, "integer_bits_limit")


def _canonical_size(summary):
    src, rid = "bs", "r1"
    schema, contract = REG[src]
    obj = {"digest_version": "d7-ledger/2", "source": src, "round_id": rid, "report_schema": C(schema),
           "validity_contract": C(contract), "finished_wall": C(T), "finished_mono": C(mono(T)),
           "telemetry_error_present": False,
           "summary": {"collection": axis_repr(src, "collection", summary["collection"]),
                       "writing": axis_repr(src, "writing", summary["writing"]),
                       "final_db": C(summary["final_db"]), "unexpected": extras(summary, ("collection", "writing",
                                                                                        "final_db"))}}
    return len(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8"))


def _canonical_sized(target):
    """writer_call_ids 96 개 문자열을 ≤256 bytes 로 채워 digest 객체 JSON 을 정확히 target bytes 로 만든다."""
    s = S()
    for p in P:
        s["writing"][p]["writer_call_ids"] = ["x"] * 32
    need = target - _canonical_size(s)
    ctrl, ascii_ = divmod(need, 6)                                 # \x01 은 JSON 에서 6 bytes, ASCII 는 1 byte
    slots = [(p, i) for p in P for i in range(32)]
    for k, (p, i) in enumerate(slots):
        add_c = ctrl // len(slots) + (1 if k < ctrl % len(slots) else 0)
        s["writing"][p]["writer_call_ids"][i] = "x" + "\x01" * add_c
    s["writing"]["usd-krw"]["writer_call_ids"][0] += "a" * ascii_
    assert _canonical_size(s) == target
    return s


def test_canonical_bytes_limit():
    _ok(_canonical_sized(65536))
    _limit_case(_canonical_sized(65537), "canonical_bytes_limit")


def test_L9_29_shape_cycle():
    s = S()
    loop = []
    loop.append(loop)
    s["collection"]["usd-krw"]["zz"] = loop
    _limit_case(s, "cyclic_input", "input_shape_error")


def test_shared_reference_is_not_cycle():
    s = S()
    shared = [1, 2]
    s["collection"]["usd-krw"]["observation_sequences"] = shared
    s["collection"]["jpy-krw"]["observation_sequences"] = shared
    _ok(s)


def test_L9_29_shape_non_string_key():
    s = S()
    s["collection"]["usd-krw"][1] = "v"
    _limit_case(s, "non_string_key", "input_shape_error")


def test_L9_29_shape_surrogate():
    s = S()
    s["collection"]["usd-krw"]["detail"] = "\ud800"
    _limit_case(s, "invalid_unicode", "input_shape_error")


def test_L9_29_limit_redelivery_is_not_duplicate():
    s = _keys(33)
    e = _limit_case(s, "dict_keys_limit")
    r = e.finish(summary=s, at=T + S1)
    assert r["classification"] == "input_limit_exceeded"


@pytest.mark.parametrize("field,val", [("invocation_id", "i" * 129), ("round_id", "r" * 129), ("source", "s" * 129)])
def test_envelope_string_limit(field, val):
    e = Env()
    e.ready()
    kw = dict(epoch="E1", invocation_id="A", round_id="r1", report_schema=1, validity_contract="bank_v2_evidence/1",
              finished_wall=T, finished_mono=mono(T), selected_summary=S(), telemetry_error_present=False,
              received_at=T, received_mono=mono(T))
    if field == "source":
        r = e.ld.register(epoch="E1", invocation_id="B", source=val, started_wall=T - S1, started_mono=mono(T - S1),
                          received_at=T, received_mono=mono(T))
    else:
        kw[field] = val
        r = e.ld.finish(**kw)
    shape(r)
    assert r["classification"] == "input_limit_exceeded" and r["records"] == [] and r["changes"] == []
    assert r["diagnostics"] == diag(["envelope_string_limit", "input_limit_exceeded"], "G", global_=True)
    assert e.rec()["lifecycle"] == "in_flight"


def test_envelope_empty_and_range_are_invalid_argument():
    e = Env()
    r = e.ld.register(epoch="E1", invocation_id="", source="bs", started_wall=T - S1, started_mono=mono(T - S1),
                      received_at=T, received_mono=mono(T))
    assert r["classification"] == "invalid_argument" and r["records"] == []
    assert r["diagnostics"] == diag(["invalid_argument"], "G", global_=True)
    r = e.ld.register(epoch="E1", invocation_id="A", source="bs", started_wall=-1, started_mono=mono(T - S1),
                      received_at=T, received_mono=mono(T))
    assert r["classification"] == "invalid_argument"
    assert e.rec("A") is None


def test_envelope_string_at_limit_is_accepted():
    e = Env()
    inv, rid = "i" * 128, "r" * 128
    e.register(inv)
    assert e.link(inv, rid)["classification"] == "linked"
    assert e.finish(inv, rid)["classification"] == "finalized"


MAXT = 2 ** 63 - 1 - 4260000000


def test_time_range_upper_bound():
    e = Env()
    r = e.ld.register(epoch="E1", invocation_id="A", source="bs", started_wall=MAXT, started_mono=MAXT,
                      received_at=MAXT, received_mono=MAXT)
    assert r["classification"] == "registered"
    r = e.ld.register(epoch="E1", invocation_id="B", source="bs", started_wall=MAXT, started_mono=MAXT,
                      received_at=MAXT + 1, received_mono=MAXT)
    assert r["classification"] == "invalid_argument" and e.rec("B") is None


def test_schema_range_upper_bound():
    e = Env()
    e.register()
    assert e.link(schema=2 ** 31 - 1)["classification"] == "unregistered_contract"   # 범위 안, 등록표 밖
    e2 = Env()
    e2.register()
    r = e2.link(schema=2 ** 31)
    assert r["classification"] == "invalid_argument" and e2.rec()["lifecycle"] == "awaiting_report"


def test_reason_256_bytes_is_other_without_oversize():
    s = S()
    s["collection"]["eur-krw"]["reason"] = "x" * 256
    _, r = finalized_env(summary=s)
    assert r["diagnostics"]["codes"] == ["reason_other"]


def test_first_limit_in_fixed_order_wins():
    s = _keys(33)
    s["collection"]["usd-krw"]["k" * 129] = 0                     # 길이 검사가 키 조사보다 먼저
    _limit_case(s, "dict_keys_limit")
    s = S()
    s["collection"]["usd-krw"][1] = 0
    s["collection"]["usd-krw"]["k" * 129] = 0                     # non_string_key → key_bytes_limit 순서
    _limit_case(s, "non_string_key", "input_shape_error")


def _detail_bytes(d):
    return len(json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())


def test_detail_bytes_exact_bound():
    size = _detail_bytes(D(S()))
    e = Env(max_detail_bytes=size)
    e.ready()
    assert e.finish()["classification"] == "finalized"
    e = Env(max_detail_bytes=size - 1)
    e.ready()
    assert e.finish()["classification"] == "report_unavailable"


def test_anchored_record_input_limit_isolates():
    e, _ = finalized_env()
    r = shape(e.finish(summary=_keys(33), at=T + S1))
    assert r["classification"] == "input_limit_exceeded" and r["changes"] == [rm("A", D(S()))]
    assert r["diagnostics"] == diag(["dict_keys_limit", "ever_unavailable", "input_limit_exceeded",
                                     "report_unavailable"], "B")
    rec = e.rec()
    assert (rec["lifecycle"], rec["unavailable_reason"], rec["inclusion"]) == \
        ("report_unavailable", "input_limit", "open_excluded")
    assert rec["first_digest"] == digest("bs", "r1", S())


def test_contract_mixed_with_input_limit():
    e, _ = finalized_env()
    r = e.finish(summary=_keys(33), contract="bank_v2_evidence/9", at=T + S1)
    assert r["classification"] == "contract_mixed" and r["changes"] == [rm("A", D(S()))]
    codes = r["diagnostics"]["codes"]
    assert {"contract_mixed", "unregistered_contract", "dict_keys_limit", "input_limit_exceeded"} <= set(codes)
    assert "conflicting_finish" not in codes and r["diagnostics"]["baseline_invalidated"] is True
    assert e.rec()["lifecycle"] == "contract_mixed"


def test_anchorless_unavailable_recovers_on_valid_finish():
    e = Env()
    e.ready()
    assert e.finish(fm=mono(T - S1) - 1)["classification"] == "time_integrity_error"
    r = e.finish(at=T + S1)
    assert r["classification"] == "finalized" and r["changes"] == [add("A", D(S()))]
    assert "late_finish_accepted" in r["diagnostics"]["codes"]
    assert e.rec()["first_digest"] == digest("bs", "r1", S())


# ───────── L8 용량 ─────────

def test_L9_30_admission_stopped():
    e = Env(max_records=1)
    e.ready("A")
    r = shape(e.register("B", at=T - S1 // 4))
    assert r["classification"] == "admission_stopped" and r["records"] == []
    assert r["diagnostics"] == diag(["admission_stopped", "aggregation_capacity"], "G")   # source 를 아는 거절 — 전역 아님
    h = r["health"]
    assert "bs" in h["uncertain_sources"] and h["coverage_complete"] is False
    assert h["admission_stopped"] is True and h["admission_stopped_at"] == T - S1 // 4 and h["untracked_invocations"] == 1
    r = e.register("C", at=T - S1 // 5)
    assert e.query(at=T - S1 // 5)["health"]["untracked_invocations"] == 2
    assert e.query()["health"]["admission_stopped_at"] == T - S1 // 4
    assert e.finish("A")["classification"] == "finalized"
    assert e.finish("B", "rB")["classification"] == "orphan"


def test_L9_30_duplicates_do_not_count_as_untracked():
    e = Env(max_records=1)
    e.ready("A")
    e.register("B")
    e.register("A")
    e.register("X", "unknown_source")
    assert e.query()["health"]["untracked_invocations"] == 1


def test_L9_31_retained_detail_capacity():
    e = Env(max_retained_details=1)
    e.ready("A", "r1")
    e.finish("A", "r1")
    e.query(at=CLOSE)
    e.ready("B", "r2")
    r = shape(e.finish("B", "r2", fw=CLOSE, at=CLOSE))
    assert r["classification"] == "report_unavailable" and r["changes"] == []
    assert r["diagnostics"] == diag(["aggregation_capacity", "report_unavailable", "ever_unavailable"], "G")
    rec = e.rec("B")
    assert (rec["lifecycle"], rec["unavailable_reason"], rec["inclusion"], rec["detail"]) == \
        ("report_unavailable", "aggregation_capacity", "open_excluded", None)
    assert rec["first_digest"] == digest("bs", "r2", S(), fw=CLOSE)
    assert r["health"]["retained_details"] == 1 and e.rec("A")["detail"] == D(S())


def test_L9_32_detail_bytes_capacity_then_duplicate():
    e = Env(max_detail_bytes=1)
    e.ready()
    r = e.finish()
    assert r["classification"] == "report_unavailable" and e.rec()["first_digest"] is not None
    r = e.finish(at=T + S1)
    assert r["classification"] == "duplicate_finish" and r["changes"] == []
    assert e.rec()["lifecycle"] == "report_unavailable"


def test_L9_33_saturated_late_first_finish_is_post_close():
    e = Env(max_retained_details=1)
    e.ready("A", "r1")
    e.finish("A", "r1")
    e.ready("B", "r2")
    r = e.finish("B", "r2", at=CLOSE)
    assert r["classification"] == "post_close_finish" and e.rec("B")["inclusion"] == "post_close_excluded"


def test_L9_37_capacity_unavailable_then_different_digest():
    e = Env(max_retained_details=1)
    e.ready("A", "r1")
    e.finish("A", "r1")
    e.ready("B", "r2")
    e.finish("B", "r2", at=T + S1)
    assert e.finish("B", "r2", at=T + 2 * S1)["classification"] == "duplicate_finish"
    r = e.finish("B", "r2", summary=_changed(), at=T + 3 * S1)
    assert r["classification"] == "conflicting_finish" and r["changes"] == []
    assert r["diagnostics"]["baseline_invalidated"] is True
    assert e.rec("B")["lifecycle"] == "conflicting"


# ───────── L3.4 색인 손상 ─────────

def test_L9_34_missing_record():
    e = Env()
    e.ready()
    assert e.ld._inject_identity_fault_for_test(invocation_id="A", fault="missing_record") is None
    r = shape(e.finish(at=T))
    assert r["classification"] == "post_close_unverified" and r["records"] == [] and r["changes"] == []
    assert r["diagnostics"] == diag(["identity_unverified"], "B", global_=True)
    assert r["health"]["index_error"] is True
    e.register("B")
    r = e.finish("B", "r2", at=T + S1)
    assert r["classification"] == "post_close_unverified" and r["records"] == []
    q = e.query(at=T + S1)
    assert q["classification"] == "post_close_unverified" and q["entries"] == [] and q["next_seq"] is None


@pytest.mark.parametrize("closed", [False, True])
def test_L9_34_missing_owner(closed):
    e, _ = finalized_env()
    if closed:
        e.query(at=CLOSE)                                        # 정상 조회로 먼저 닫는다(색인 오류는 수신 게이트보다 앞서 끝난다)
        assert e.rec()["closed"] is True
    at = CLOSE if closed else T + S1
    pair_before = (e.query()["health"]["last_received_at"], e.query()["health"]["last_received_mono"])
    e.ld._inject_identity_fault_for_test(invocation_id="A", fault="missing_owner")
    r = shape(e.finish(at=at))
    assert (r["health"]["last_received_at"], r["health"]["last_received_mono"]) == pair_before
    assert r["classification"] == "post_close_unverified"
    assert r["changes"] == ([] if closed else [rm("A", D(S()))])
    assert "identity_unverified" in r["diagnostics"]["codes"]
    assert r["diagnostics"]["cumulative_evidence_uncertain"] is closed
    rec = e.rec()
    assert (rec["lifecycle"], rec["unavailable_reason"]) == ("report_unavailable", "identity_unverified")
    assert rec["inclusion"] == ("frozen_included" if closed else "open_excluded")
    assert e.finish(at=at + S1)["classification"] == "post_close_unverified"


def test_fault_hook_validation():
    e = Env()
    e.register()
    with pytest.raises(ValueError):
        e.ld._inject_identity_fault_for_test(invocation_id="A", fault="missing_record")   # 미연결
    e.link()
    with pytest.raises(ValueError):
        e.ld._inject_identity_fault_for_test(invocation_id="A", fault="nope")
    with pytest.raises(TypeError):
        e.ld._inject_identity_fault_for_test(invocation_id="A", fault=1)


# ───────── 순수성·사본 ─────────

def test_L9_35_inputs_and_returns_are_isolated():
    s = S()
    frozen = copy.deepcopy(s)
    e = Env()
    e.ready()
    r = e.finish(summary=s)
    assert s == frozen
    r["records"][0]["detail"]["pairs"]["usd-krw"]["collection"]["status"] = "hacked"
    r["changes"][0]["detail"]["contribution"]["collection"]["V"] = 99
    s["collection"]["usd-krw"]["status"] = "missing"
    rec = e.rec()
    rec["lifecycle"] = "hacked"
    assert e.rec()["detail"] == D(frozen) and e.rec()["lifecycle"] == "finalized"
    assert e.rec()["first_digest"] == digest("bs", "r1", frozen)
    assert e.finish(summary=frozen, at=T + S1)["classification"] == "duplicate_finish"


def test_L9_36_query_paging():
    e = Env()
    q = e.query(at=T)
    assert q["classification"] == "snapshot" and q["entries"] == [] and q["next_seq"] is None
    e.ready("A", "r1")
    e.ready("B", "r2")
    e.finish("A", "r1")
    e.finish("B", "r2")
    p1 = e.query(limit=1)
    assert [x["invocation_id"] for x in p1["entries"]] == ["A"] and p1["next_seq"] == 1
    p2 = e.query(after_seq=p1["next_seq"], limit=1)
    assert [x["invocation_id"] for x in p2["entries"]] == ["B"] and p2["next_seq"] is None


@pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": 17}, {"after_seq": -1}])
def test_query_invalid_argument(kw):
    e, _ = finalized_env()
    q = e.query(at=T, **kw)
    assert q["classification"] == "invalid_argument" and q["entries"] == [] and q["next_seq"] is None
    assert q["diagnostics"] == diag(["invalid_argument"])


def test_L9_38_telemetry_and_evidence_malformed():
    e, r = finalized_env()
    e2 = Env()
    e2.ready()
    r = e2.finish(tel=True)
    assert r["classification"] == "finalized" and "telemetry_error" in r["diagnostics"]["codes"]
    assert r["changes"][0]["detail"]["diagnostics"]["telemetry_error"] is True
    assert e2.rec()["first_digest"] == digest("bs", "r1", S(), tel=True) != digest("bs", "r1", S())
    s = S()
    s["writing"]["usd-krw"]["lost_writer_calls"] = -1
    e3 = Env()
    e3.ready()
    r = e3.finish(summary=s)
    assert r["classification"] == "finalized" and "evidence_malformed" in r["diagnostics"]["codes"]
    assert r["changes"][0]["detail"] == D(S())


def test_L9_39_selected_producer_reasons_are_kept():
    s = S("investing")
    s["collection"] = {p: a("missing", "selector_missing") for p in P}
    _, r = finalized_env("investing", s)
    d = r["changes"][0]["detail"]
    assert d["contribution"]["collection"]["M"] == 3 and "reason_other" not in r["diagnostics"]["codes"]
    assert all(d["pairs"][p]["collection"]["reason"] == "selector_missing" for p in P)
    s = S()
    s["collection"] = {p: a("not_attempted", "mibank_untrusted_window") for p in P}
    _, r = finalized_env("bs", s)
    d = r["changes"][0]["detail"]
    assert d["contribution"]["collection"]["N"] == 3
    assert d["contribution"]["collection_not_attempted_reasons"] == {"mibank_untrusted_window": 3}
    assert "reason_other" not in r["diagnostics"]["codes"]


CLOCK_HEALTH = ("clock_error", "coverage_complete", "uncertain_sources")


def test_L9_40_open_regressed_redelivery_then_equal_receipt():
    e, _ = finalized_env()
    h0 = e.query()["health"]
    rec0 = e.rec()
    for rw, rmo, codes in ((T - 1, mono(T), ["receipt_wall_regressed"]), (T, mono(T) - 1, ["receipt_mono_regressed"]),
                           (T - 1, mono(T) - 1, ["receipt_mono_regressed", "receipt_wall_regressed"])):
        r = shape(e.ld.finish(epoch="E1", invocation_id="A", round_id="r1", report_schema=1,
                              validity_contract="bank_v2_evidence/1", finished_wall=T, finished_mono=mono(T),
                              selected_summary=S(), telemetry_error_present=False, received_at=rw, received_mono=rmo))
        assert r["classification"] == "time_integrity_error" and r["changes"] == [] and r["records"] == []
        assert r["diagnostics"] == diag(codes + ["time_integrity_error"], "G", global_=True)
        assert {k: v for k, v in r["health"].items() if k not in CLOCK_HEALTH} == \
            {k: v for k, v in h0.items() if k not in CLOCK_HEALTH}
        assert (r["health"]["clock_error"], r["health"]["coverage_complete"], r["health"]["uncertain_sources"]) == \
            (True, False, SRC3)
        assert e.rec() == rec0
    rec = e.rec()
    assert (rec["lifecycle"], rec["inclusion"], rec["detail"]) == ("finalized", "open_included", D(S()))
    r = e.finish(at=h0["last_received_at"])
    assert r["classification"] == "duplicate_finish" and r["diagnostics"] == diag(["duplicate_finish"])
    assert r["health"]["clock_error"] is True and r["health"]["coverage_complete"] is False


def _gate_env():
    e, _ = finalized_env()                                        # A: 열린 finalized
    e.register("B")                                               # B: unbound
    return e


def _call(e, kind, rw, rmo):
    base = dict(epoch="E1", received_at=rw, received_mono=rmo)
    fin = dict(base, report_schema=1, validity_contract="bank_v2_evidence/1", finished_wall=T,
               finished_mono=mono(T), selected_summary=S(), telemetry_error_present=False)
    if kind == "new_register":
        return e.ld.register(invocation_id="N", source="bs", started_wall=T - S1, started_mono=mono(T - S1), **base)
    if kind == "dup_register":
        return e.ld.register(invocation_id="A", source="bs", started_wall=T - S1, started_mono=mono(T - S1), **base)
    if kind == "conflict_register":
        return e.ld.register(invocation_id="A", source="citi", started_wall=T - S1, started_mono=mono(T - S1), **base)
    if kind == "identity_link":
        return e.ld.link_round(invocation_id="B", round_id="r1", report_schema=1,
                               validity_contract="bank_v2_evidence/1", **base)
    if kind == "unbound_finish":
        return e.ld.finish(**dict(fin, invocation_id="B", round_id="rB"))
    if kind == "contract_finish":
        return e.ld.finish(**dict(fin, invocation_id="A", round_id="r1", validity_contract="bank_v2_evidence/9"))
    if kind == "digest_finish":
        return e.ld.finish(**dict(fin, invocation_id="A", round_id="r1", selected_summary=_changed()))
    if kind == "limit_finish":
        return e.ld.finish(**dict(fin, invocation_id="A", round_id="r1", selected_summary=_keys(33)))
    if kind == "identity_finish":
        return e.ld.finish(**dict(fin, invocation_id="A", round_id="r9"))
    raise AssertionError(kind)


GATE_CASES = [("new_register", "registered"), ("dup_register", "duplicate_invocation"),
              ("conflict_register", "registration_conflict"), ("identity_link", "identity_conflict"),
              ("unbound_finish", "start_unrecorded"), ("contract_finish", "contract_mixed"),
              ("digest_finish", "conflicting_finish"), ("limit_finish", "input_limit_exceeded"),
              ("identity_finish", "identity_conflict")]


@pytest.mark.parametrize("kind,normal", GATE_CASES)
@pytest.mark.parametrize("which", ["wall", "mono", "both"])
def test_L9_41_regressed_receipt_overrides_everything(kind, normal, which):
    e = _gate_env()
    h0 = e.query()["health"]
    lw, lm = h0["last_received_at"], h0["last_received_mono"]
    state = {i: e.rec(i) for i in ("A", "B")}
    rw = lw - 1 if which in ("wall", "both") else lw
    rmo = lm - 1 if which in ("mono", "both") else lm
    codes = {"wall": ["receipt_wall_regressed"], "mono": ["receipt_mono_regressed"],
             "both": ["receipt_mono_regressed", "receipt_wall_regressed"]}[which]
    r = shape(_call(e, kind, rw, rmo))
    assert r["classification"] == "time_integrity_error" and r["records"] == [] and r["changes"] == []
    assert r["diagnostics"] == diag(codes + ["time_integrity_error"], "G", global_=True)
    assert {i: e.rec(i) for i in ("A", "B")} == state and e.rec("N") is None
    h = r["health"]
    assert {k: v for k, v in h.items() if k not in CLOCK_HEALTH} == {k: v for k, v in h0.items() if k not in CLOCK_HEALTH}
    assert _call(e, kind, lw, lm)["classification"] == normal      # 정상 수신 쌍으로 다시 보내면 원래 판정


def test_L9_42_query_receipt_gate():
    e = Env()
    q = e.query(at=T)
    assert q["classification"] == "snapshot" and (q["as_of"], q["as_of_mono"]) == (T, mono(T))
    e2, _ = finalized_env()
    q = e2.ld.contributions_open(as_of=CLOSE, as_of_mono=mono(T) - 1, after_seq=0, limit=16)
    assert q["classification"] == "time_integrity_error" and q["entries"] == [] and q["next_seq"] is None
    assert (q["as_of"], q["as_of_mono"]) == (T, mono(T))
    assert q["diagnostics"] == diag(["receipt_mono_regressed", "time_integrity_error"], "G", global_=True)
    assert e2.rec()["closed"] is False
    assert (q["health"]["last_received_at"], q["health"]["last_received_mono"]) == (T, mono(T))
    q = e2.ld.contributions_open(as_of=T, as_of_mono=mono(T), after_seq=0, limit=16)
    assert q["classification"] == "snapshot" and len(q["entries"]) == 1


def test_query_after_closed_regressed_does_not_reopen():
    e, _ = finalized_env()
    e.query(at=CLOSE)
    q = e.ld.contributions_open(as_of=CLOSE - 1, as_of_mono=mono(CLOSE), after_seq=0, limit=16)
    assert q["classification"] == "time_integrity_error" and e.rec()["closed"] is True


# ───────── §5 반례 ─────────

def test_canonical_counterexample_conflict_removes_whole_round():
    e = Env()
    other = {p: a("not_attempted", "not_started") for p in P[1:]}
    rounds = [("A", "valid", "validated"), ("B", "missing", "no_value"),
              ("C", "valid", "validated"), ("D", "missing", "no_value")]
    for i, (inv, st, reason) in enumerate(rounds):
        e.ready(inv, f"r{inv}")
    summaries = {}
    for i, (inv, st, reason) in enumerate(rounds):
        s = S(col={"usd-krw": a(st, reason), **other})
        summaries[inv] = s
        assert e.finish(inv, f"r{inv}", summary=s, fw=T + i * S1, at=T + i * S1)["classification"] == "finalized"
    c2 = copy.deepcopy(summaries["C"])
    c2["collection"]["usd-krw"] = a("missing", "no_value")
    r = e.finish("C", "rC", summary=c2, fw=T + 2 * S1, at=T + 10 * S1)
    assert r["classification"] == "conflicting_finish"
    assert r["changes"] == [rm("C", D(summaries["C"]))]
    assert r["diagnostics"]["baseline_invalidated"] is True
    q = e.query()
    kept = [x["detail"] for x in q["entries"]]
    usd = [d["pairs"]["usd-krw"]["collection"]["status"] for d in kept]
    assert sorted(usd) == ["missing", "missing", "valid"]
    total = {"V": 0, "M": 0, "U": 0, "N": 0}
    for d in kept:
        for k, v in d["contribution"]["collection"].items():
            total[k] += v
    assert total == {"V": 1, "M": 2, "U": 0, "N": 6}


def test_record_equation_holds():
    e = Env()
    e.ready("A", "r1")
    e.finish("A", "r1")
    e.register("B")
    e.ready("C", "r3")
    e.finish("C", "r3", summary=_keys(33))
    e.register("D")
    e.finish("D", "r4")
    lif = [e.rec(i)["lifecycle"] for i in "ABCD"]
    assert sorted(lif) == ["awaiting_report", "finalized", "report_unavailable", "report_unavailable"]
    con = [e.rec(i)["connection"] for i in "ABCD"]
    assert con.count("unbound") + con.count("started") == 4


# ───────── 모듈 제약(L1.3 + 보완 A7) ─────────
# L9.39 생산자 reason 정적 대조는 보완 A6 에 따라 tests/test_d7_reason_producer_drift.py 에 있다.
# 나머지 금지 호출은 import 별칭·상대 import·from-import 이름을 바인딩으로 풀어서 판정한다(Codex 시험 r2 검토 1번).
# 구현 제약(b): 시각은 주입 정수만 사용하므로 time·datetime import 자체를 금지한다.
# 별칭의 스코프 해석 없이도 로컬 바인딩에 가려진 시계 읽기를 막고, 모든 스코프의 open 호출을 금지한다.
# 따라서 로컬 def open() 호출도 허용하지 않는다(보완 A7 의 time/datetime import 허용을 강화).

APP = REPO / "app"
BANNED_MODULES = ("app.crawlers", "app.crud", "app.database", "app.models", "app.main", "app.scheduler",
                  "sqlalchemy", "requests", "urllib", "http", "socket", "redis", "os", "io", "pathlib", "shutil",
                  "tempfile", "subprocess", "importlib", "time", "datetime")
FORBIDDEN_CALLS = {"time.time", "time.time_ns", "time.monotonic", "time.monotonic_ns", "time.perf_counter",
                   "time.perf_counter_ns", "time.process_time", "datetime.datetime.now", "datetime.datetime.utcnow",
                   "datetime.datetime.today", "datetime.date.today", "builtins.open", "builtins.__import__",
                   "builtins.exec", "builtins.eval", "builtins.compile"}
FILE_METHODS = {"read_text", "write_text", "read_bytes", "write_bytes", "open", "unlink", "touch", "mkdir", "rmdir"}


def _resolve_from(node, package="app"):
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    return ".".join(parts + ([node.module] if node.module else []))


def _module_violations(src, package="app"):
    tree = ast.parse(src)
    bind, local, bad = {}, set(), []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(n.name)
        elif isinstance(n, ast.Import):
            for x in n.names:
                bind[(x.asname or x.name).split(".")[0]] = x.name if x.asname else x.name.split(".")[0]
                mods = [x.name]
                bad += [f"import {m}" for m in mods if any(m == b or m.startswith(b + ".") for b in BANNED_MODULES)]
        elif isinstance(n, ast.ImportFrom):
            base = _resolve_from(n, package)
            for x in n.names:
                full = f"{base}.{x.name}" if base else x.name
                bind[x.asname or x.name] = full
                for m in (base, full):
                    if any(m == b or m.startswith(b + ".") for b in BANNED_MODULES):
                        bad.append(f"from-import {full}")
                        break
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f, attrs = n.func, []
        while isinstance(f, ast.Attribute):
            attrs.insert(0, f.attr)
            f = f.value
        if isinstance(n.func, ast.Name) and n.func.id == "open":
            bad.append("call open")
            continue
        if isinstance(n.func, ast.Attribute) and n.func.attr == "open":
            bad.append("call <expr>.open")
            continue
        if isinstance(f, ast.Name):
            head = bind.get(f.id) or (None if f.id in local else f"builtins.{f.id}")
            if head is None:
                continue
            q = ".".join([head] + attrs)
            if q in FORBIDDEN_CALLS or any(q == b or q.startswith(b + ".") for b in BANNED_MODULES):
                bad.append(f"call {q}")
        elif attrs and attrs[-1] in FILE_METHODS:
            bad.append(f"call <expr>.{attrs[-1]}")
    return bad


@pytest.mark.parametrize("snippet", [
    "from time import time\ntime()",
    "import time as t\nt.monotonic()",
    "from datetime import datetime as dt\ndt.now()",
    "from io import open as file_open\nfile_open('x')",
    "from pathlib import Path\nPath('x').read_text()",
    "from app import crawlers",
    "from . import crawlers",
    "from .database import SessionLocal",
    "open('x')",
    "import datetime\ndatetime.date.today()",
    "import os.path",
    "def local():\n    def open():\n        return 1\n    return open()\nopen('x')",
    "import time as t\ndef local():\n    import math as t\nt.time()",
    "import time\ntime.gmtime()",
    "import time\ntime.localtime()",
    "import time\ntime.ctime()",
    "import time\ntime.asctime()",
    "from time import gmtime\ngmtime()",
    "from datetime import date\ndate.today()",
    "def open():\n    return 1\nopen()",
    "stream.open('x')",
    "factory().open('x')",
])
def test_module_violation_checker_catches(snippet):
    assert _module_violations(snippet) != []


@pytest.mark.parametrize("snippet", [
    "import hashlib, json\nhashlib.sha256(json.dumps({}).encode()).hexdigest()",
    "s = 'a'\ns.replace('a', 'b')",
    "from . import d7_round_axes",
    "def local():\n    return 1\nlocal()",
])
def test_module_violation_checker_allows(snippet):
    assert _module_violations(snippet) == []


def test_ledger_imports_no_producer_or_io():
    assert _module_violations((APP / "d7_round_ledger.py").read_text(encoding="utf-8")) == []
