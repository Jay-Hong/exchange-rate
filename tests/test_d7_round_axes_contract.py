"""D7 Tier 1 첫 슬라이스 계약 — 종료보고 요약의 축별 정규화와 단일 회차 기여(순수 함수, 운영 경로 무접촉).

설계: SOURCE_HEALTH_D7_AGGREGATION.md §3·§3.1(축별 검증·report_malformed·reason_unrecorded·계측 오류 독립),
SOURCE_HEALTH_PLAN.md §7.2(5축)·310행(`final_db=not_checked` = 모든 통화 `unknown/not_checked`), 첫 슬라이스 범위는
Claude·Codex 합의(2026-09-26 — 호출 등록·dedup·시간 버킷·어댑터·배선은 제외).
계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현(`app/d7_round_axes.py`)은 Codex.

## 인터페이스
- `PAIRS == ("usd-krw", "jpy-krw", "eur-krw")` — payload 와 독립인 필수 통화.
- `REGISTRY == {"investing": (3, "investing_range_checked/2"), "bs": (1, "bank_v2_evidence/1"), "citi": (1, "bank_v2_evidence/1")}`
  — (report_schema, validity_contract). 값은 `app/crawlers/investing_report.py` SCHEMA_VERSION·VALIDITY_CONTRACT 와
  `app/crawlers/bank_report.py` 의 것과 같다(이 시험이 대조한다).
- `normalize_round_axes(source, report_schema, validity_contract, selected_summary, telemetry_error_present) -> dict`
  - 타입 오류는 `TypeError`: source·validity_contract 가 str 아님, report_schema 가 int 아님(bool 포함), selected_summary 가 dict 아님,
    telemetry_error_present 가 bool 아님.
  - 지원 밖 소스 → `{"accepted": False, "reason": "unsupported_source"}`, 등록된 (schema, 계약) 과 다름 → `{"accepted": False, "reason": "unregistered_contract"}`.
    거부에는 기여·축이 없다.
  - selected_summary = `{"collection": {pair: 축}, "writing": {pair: 축}, "final_db": "not_checked"}` (이미 선택된 요약. 원문 재판단 없음).
  - 축 검증(통화×축마다): 축이 dict 가 아니거나 status 가 없거나 str 이 아니거나 허용 enum 밖 → 그 축만 `{"status": "unknown", "reason": "report_malformed"}`.
    허용 enum — collection: valid·missing·not_attempted·unknown / writing: investing = unknown·not_attempted,
    bs·citi = performed·no_change_needed·policy_blocked·not_attempted·unknown. reason 이 없거나 str 아님·빈 문자열이면 status 유지, reason = `"reason_unrecorded"`.
    출력 축은 `{"status", "reason"}` 두 키뿐(증거 필드는 싣지 않는다).
  - final_db: 요약의 `final_db` 가 정확히 `"not_checked"` 이면 모든 통화 `{"status": "unknown", "reason": "not_checked"}`, 그 밖(없음 포함)은 모든 통화 report_malformed.
- 반환(수락): `{"accepted": True, "source", "report_schema", "validity_contract",
  "pairs": {pair: {"collection", "writing", "final_db"}},
  "contribution": {"collection": {"V","M","U","N"}, "collection_unknown_reasons": {사유: 수}, "collection_not_attempted_reasons": {사유: 수},
                   "writing": {상태: 수}, "final_db": {상태: 수}},        # 상태별 수는 1 이상인 것만
  "diagnostics": {"malformed_axis_items": 수, "malformed": [[pair, axis], ...], "reason_unrecorded": [[pair, axis], ...],
                  "telemetry_error": bool, "unexpected_pairs": [...]}}`
  목록은 PAIRS 순서 → 축 순서(collection, writing, final_db). 필수 통화 밖의 키는 세지 않고 unexpected_pairs 로만 보고.
  unexpected_pairs 는 늘 문자열 목록이다 — str 키는 그대로, str 이 아닌 키는 repr(키) 로 적고, str 키 묶음(정렬) 뒤에 비 str 묶음(정렬)을 둔다.
  키 종류가 섞여도 예외를 내지 않는다(보고 결함은 진단으로, Codex 커밋 검토 2026-09-26 지적).
- 순수: 같은 입력 → 같은 출력, 입력을 바꾸지 않고, 반환값이 입력의 뒤 변경에 영향받지 않는다. 같은 보고를 두 번 넣으면 같은 기여가 두 번 나온다 —
  누적은 다음 슬라이스(식별 ledger)의 일이다.
"""
from __future__ import annotations

import copy
import datetime

import pytest

from app import d7_round_axes as ax
from app.crawlers import bank_report, investing_report

P = ("usd-krw", "jpy-krw", "eur-krw")


def a(status, reason="r", **extra):
    d = {"status": status, "reason": reason}
    d.update(extra)
    return d


def bs_summary(col=None, wr=None, final_db="not_checked"):
    col = col or {p: a("valid", "range_checked", attempt_id=1, normalized_rate=1.0) for p in P}
    wr = wr or {p: a("performed", "committed", writer_call_id=3) for p in P}
    return {"collection": col, "writing": wr, "final_db": final_db}


def run(summary, source="bs", tel=False):
    schema, contract = ax.REGISTRY[source]
    return ax.normalize_round_axes(source, schema, contract, summary, tel)


def test_constants_match_report_producers():
    assert ax.PAIRS == P
    assert ax.REGISTRY == {"investing": (3, "investing_range_checked/2"), "bs": (1, "bank_v2_evidence/1"),
                           "citi": (1, "bank_v2_evidence/1")}
    assert (investing_report.SCHEMA_VERSION, investing_report.VALIDITY_CONTRACT) == ax.REGISTRY["investing"]
    assert (bank_report.SCHEMA_VERSION, bank_report.VALIDITY_CONTRACT) == ax.REGISTRY["bs"]


@pytest.mark.parametrize("args,reason", [
    (("kb", 1, "bank_v2_evidence/1"), "unsupported_source"),
    (("ibk", 1, "bank_v2_evidence/1"), "unsupported_source"),
    (("kb", 9, "unknown_contract/9"), "unsupported_source"),       # 소스 판정이 계약 판정보다 먼저
    (("investing", 2, "investing_range_checked/2"), "unregistered_contract"),
    (("investing", 3, "investing_range_checked/1"), "unregistered_contract"),
    (("bs", 1, "investing_range_checked/2"), "unregistered_contract"),
])
def test_rejections(args, reason):
    r = ax.normalize_round_axes(*args, bs_summary(), False)
    assert r == {"accepted": False, "reason": reason}


def test_clean_bs_round():
    wr = {"usd-krw": a("performed", "committed"), "jpy-krw": a("no_change_needed", "equal_to_last_record"),
          "eur-krw": a("performed", "committed")}
    r = run(bs_summary(wr=wr))
    assert r["accepted"] is True and r["source"] == "bs" and r["report_schema"] == 1 and r["validity_contract"] == "bank_v2_evidence/1"
    assert r["contribution"]["collection"] == {"V": 3, "M": 0, "U": 0, "N": 0}
    assert r["contribution"]["writing"] == {"performed": 2, "no_change_needed": 1}
    assert r["contribution"]["final_db"] == {"unknown": 3}
    assert r["contribution"]["collection_unknown_reasons"] == {} and r["contribution"]["collection_not_attempted_reasons"] == {}
    assert r["pairs"]["usd-krw"] == {"collection": {"status": "valid", "reason": "range_checked"},
                                     "writing": {"status": "performed", "reason": "committed"},
                                     "final_db": {"status": "unknown", "reason": "not_checked"}}
    assert r["diagnostics"] == {"malformed_axis_items": 0, "malformed": [], "reason_unrecorded": [],
                                "telemetry_error": False, "unexpected_pairs": []}


@pytest.mark.parametrize("bad", ["empty", "absent", "none", "no_status", "bad_enum", "status_not_str", "not_dict"])
def test_single_axis_malformed_keeps_others(bad):
    col = {p: a("valid", "range_checked") for p in P}
    if bad == "absent":
        del col["eur-krw"]
    else:
        col["eur-krw"] = {"empty": {}, "none": None, "no_status": {"reason": "x"}, "bad_enum": a("preserved", "x"),
                          "status_not_str": {"status": 1, "reason": "x"}, "not_dict": "valid"}[bad]
    r = run(bs_summary(col=col))
    assert r["pairs"]["eur-krw"]["collection"] == {"status": "unknown", "reason": "report_malformed"}
    assert r["pairs"]["usd-krw"]["collection"]["status"] == "valid"
    assert r["pairs"]["eur-krw"]["writing"] == {"status": "performed", "reason": "committed"}   # 수집 오류가 쓰기 증거를 지우지 않는다
    assert r["contribution"]["collection"] == {"V": 2, "M": 0, "U": 1, "N": 0}
    assert r["contribution"]["collection_unknown_reasons"] == {"report_malformed": 1}
    assert r["diagnostics"]["malformed"] == [["eur-krw", "collection"]] and r["diagnostics"]["malformed_axis_items"] == 1


@pytest.mark.parametrize("reason", ["__absent__", None, "", 7])
def test_reason_unrecorded(reason):
    item = {"status": "valid"} if reason == "__absent__" else {"status": "valid", "reason": reason}
    col = {p: a("valid", "range_checked") for p in P}
    col["usd-krw"] = item
    r = run(bs_summary(col=col))
    assert r["pairs"]["usd-krw"]["collection"] == {"status": "valid", "reason": "reason_unrecorded"}
    assert r["contribution"]["collection"]["V"] == 3
    assert r["diagnostics"]["reason_unrecorded"] == [["usd-krw", "collection"]] and r["diagnostics"]["malformed_axis_items"] == 0


def test_unknown_collection_with_performed_write_and_unchecked_db():
    col = {p: a("unknown", "v2_evidence_unconfirmed") for p in P}
    r = run(bs_summary(col=col))
    assert r["contribution"]["collection"] == {"V": 0, "M": 0, "U": 3, "N": 0}
    assert r["contribution"]["collection_unknown_reasons"] == {"v2_evidence_unconfirmed": 3}
    assert r["contribution"]["writing"] == {"performed": 3} and r["contribution"]["final_db"] == {"unknown": 3}


def test_missing_counts_as_m():
    col = {"usd-krw": a("missing", "no_value"), "jpy-krw": a("missing", "validation_rejected"), "eur-krw": a("valid", "range_checked")}
    r = run(bs_summary(col=col))
    assert r["contribution"]["collection"] == {"V": 1, "M": 2, "U": 0, "N": 0}


def test_investing_cooldown():
    s = {"collection": {p: a("not_attempted", "cooldown") for p in P},
         "writing": {p: a("not_attempted", "not_submitted_to_writer") for p in P}, "final_db": "not_checked"}
    r = run(s, source="investing")
    assert r["contribution"]["collection"] == {"V": 0, "M": 0, "U": 0, "N": 3}
    assert r["contribution"]["collection_not_attempted_reasons"] == {"cooldown": 3}
    assert r["contribution"]["writing"] == {"not_attempted": 3}


def test_investing_writer_submission_and_telemetry():
    s = {"collection": {p: a("valid", "range_checked") for p in P},
         "writing": {"usd-krw": a("unknown", "per_currency_write_unverified", attempt_ids=[1]),
                     "jpy-krw": a("unknown", "telemetry_error", attempt_ids=[]),
                     "eur-krw": a("not_attempted", "not_submitted_to_writer", attempt_ids=[])}, "final_db": "not_checked"}
    r = run(s, source="investing")
    assert r["contribution"]["writing"] == {"unknown": 2, "not_attempted": 1}
    assert r["pairs"]["jpy-krw"]["writing"] == {"status": "unknown", "reason": "telemetry_error"}


@pytest.mark.parametrize("status", ["performed", "no_change_needed", "policy_blocked", "failed"])
def test_investing_writing_enum_is_restricted(status):
    s = {"collection": {p: a("valid", "range_checked") for p in P},
         "writing": {p: a("not_attempted", "not_submitted_to_writer") for p in P}, "final_db": "not_checked"}
    s["writing"]["usd-krw"] = a(status, "x")
    r = run(s, source="investing")
    assert r["pairs"]["usd-krw"]["writing"] == {"status": "unknown", "reason": "report_malformed"}
    assert r["diagnostics"]["malformed"] == [["usd-krw", "writing"]]


def test_bank_failed_is_not_allowed():
    wr = {p: a("performed", "committed") for p in P}
    wr["jpy-krw"] = a("failed", "x")
    r = run(bs_summary(wr=wr), source="citi")
    assert r["pairs"]["jpy-krw"]["writing"] == {"status": "unknown", "reason": "report_malformed"}
    assert r["contribution"]["writing"] == {"performed": 2, "unknown": 1}


def test_telemetry_error_independent_of_valid():
    r = run(bs_summary(), tel=True)
    assert r["contribution"]["collection"]["V"] == 3 and r["diagnostics"]["telemetry_error"] is True


def test_empty_summary():
    r = run({})
    for p in P:
        assert r["pairs"][p] == {ax_: {"status": "unknown", "reason": "report_malformed"} for ax_ in ("collection", "writing", "final_db")}
    assert r["contribution"]["collection"] == {"V": 0, "M": 0, "U": 3, "N": 0}
    assert r["diagnostics"]["malformed_axis_items"] == 9
    assert r["diagnostics"]["malformed"] == [[p, x] for p in P for x in ("collection", "writing", "final_db")]


@pytest.mark.parametrize("fdb", ["matched", None, "__absent__", {"usd-krw": "not_checked"}])
def test_final_db_other_values(fdb):
    s = bs_summary()
    if fdb == "__absent__":
        del s["final_db"]
    else:
        s["final_db"] = fdb
    r = run(s)
    assert all(r["pairs"][p]["final_db"] == {"status": "unknown", "reason": "report_malformed"} for p in P)
    assert r["diagnostics"]["malformed"] == [[p, "final_db"] for p in P] and r["contribution"]["final_db"] == {"unknown": 3}


def test_unexpected_pair_reported_not_counted():
    s = bs_summary()
    s["collection"]["cny-krw"] = a("valid", "x")
    s["writing"]["aud-krw"] = a("performed", "committed")
    r = run(s)
    assert r["contribution"]["collection"]["V"] == 3 and set(r["pairs"]) == set(P)
    assert r["diagnostics"]["unexpected_pairs"] == ["aud-krw", "cny-krw"]


def test_unexpected_keys_of_mixed_types_do_not_raise():
    s = bs_summary()
    s["collection"]["cny-krw"] = a("valid", "x")
    s["collection"][1] = a("valid", "x")
    s["writing"][("a",)] = a("performed", "committed")
    s["writing"]["aud-krw"] = a("performed", "committed")
    s["writing"][datetime.date(2026, 9, 26)] = a("performed", "committed")   # str ≠ repr 인 키
    r = run(s)
    assert r["contribution"]["collection"]["V"] == 3 and set(r["pairs"]) == set(P)
    assert r["diagnostics"]["unexpected_pairs"] == ["aud-krw", "cny-krw", "('a',)", "1", "datetime.date(2026, 9, 26)"]

def test_purity_order_and_immutability():
    s = bs_summary()
    before = copy.deepcopy(s)
    r1 = run(s)
    assert s == before
    rev = {"final_db": s["final_db"], "writing": dict(reversed(list(s["writing"].items()))),
           "collection": dict(reversed(list(s["collection"].items())))}
    assert run(rev) == r1
    snap = copy.deepcopy(r1)
    s["collection"]["usd-krw"]["status"] = "missing"
    s["writing"]["usd-krw"]["reason"] = "changed"
    assert r1 == snap
    assert run(copy.deepcopy(before)) == r1   # 같은 입력 → 같은 출력(두 번 넣으면 두 번 같은 기여)


@pytest.mark.parametrize("bad", ["tel_str", "schema_bool", "schema_str", "summary_list", "source_none", "contract_none"])
def test_type_errors(bad):
    args = ["bs", 1, "bank_v2_evidence/1", bs_summary(), False]
    if bad == "tel_str":
        args[4] = "yes"
    elif bad == "schema_bool":
        args[1] = True
    elif bad == "schema_str":
        args[1] = "1"
    elif bad == "summary_list":
        args[3] = []
    elif bad == "source_none":
        args[0] = None
    else:
        args[2] = None
    with pytest.raises(TypeError):
        ax.normalize_round_axes(*args)
