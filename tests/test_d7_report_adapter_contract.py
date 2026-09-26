"""D7 조각 5a 계약 — 순수 보고 어댑터(보고 객체 → ledger 인자). 운영 경로 무접촉.

세부 계약: `design/d7-aggregation/slice5a_contract_r2.md`(Codex 작성·Claude 검토 Q1~Q4 반영, APPROVE_CONTRACT_DRAFT).
벡터 이름(I1~I5·B1~B3·M1·F1~F5·F3a/b·D1)은 그 문서 S5a.3 을 따른다. 계약 시험은 Claude 가 먼저 쓰고 해시로 고정,
어댑터 구현(`app/d7_report_adapter.py`)은 Codex. 측정 게이트(S5a.4)는 scripts/d7_ledger_measure_gate.py(Codex) 가 따로 판정한다.
시험은 mock payload 가 아니라 실제 InvestingReport·BankReport 인스턴스를 쓰고, 외부 요청·DB 는 호출하지 않는다.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from app import d7_report_adapter as ad
from app import d7_round_axes as ax
from app import d7_round_ledger as lg
from app.crawlers import bank_report as br
from app.crawlers import investing_report as ir

P = ("usd-krw", "jpy-krw", "eur-krw")
S1 = 10 ** 6
T = 1789984800000000
OFF = T - 10 ** 12
REPO = Path(__file__).resolve().parents[1]


def mono(w):
    return w - OFF


class CaptureLogger:
    def __init__(self, fail=False):
        self.lines, self.fail = [], fail

    def info(self, line):
        if self.fail:
            raise RuntimeError("logger down")
        self.lines.append(json.loads(line))

    def last(self, event):
        hits = [x for x in self.lines if x["event"] == event]
        assert hits, event
        return hits[-1]


RATES = {"usd-krw": 1300.0, "jpy-krw": 900.0, "eur-krw": 1500.0}


def investing_i1(logger=None):
    lg_ = logger or CaptureLogger()
    r = ir.InvestingReport(lg_, P)
    r.start_attempt(1)
    for p in P:
        r.observation(1, p, text=str(int(RATES[p])), rate=RATES[p])
    r.writer_started(1, {"usd-krw": RATES["usd-krw"]})
    r.finish_attempt(1)
    r.finish()
    return r, lg_


def bank_b1(bank="bs", logger=None):
    lg_ = logger or CaptureLogger()
    r = br.BankReport(lg_, bank, P, (br.OFFICIAL_PRIMARY,))
    r.start_attempt(br.OFFICIAL_PRIMARY)
    r.observed(br.OFFICIAL_PRIMARY, "usd-krw", rate_text="1200", rate=1200.0)
    r.finish_attempt(br.OFFICIAL_PRIMARY)
    r.finish()
    return r, lg_


def rx(w=T):
    return {"received_at": w, "received_mono": mono(w)}


def fin_args(report, source, fw=T, at=T):
    return ad.report_finish_args(report, expected_source=source, finished_wall=fw, finished_mono=mono(fw), **rx(at))


def link_args(report, source, at=T):
    return ad.report_link_args(report, expected_source=source, **rx(at))


def status_reason(axis_map):
    return {p: (axis_map[p]["status"], axis_map[p]["reason"]) for p in P}


def through_ledger(source, link, fin, start=T - S1):
    """register → link_round → finish 를 주입 시각으로 실행하고 finish 결과를 돌려준다."""
    ld = lg.RoundLedger("E1", aggregation_started_at=T - 2 * S1)
    ld.register(epoch="E1", invocation_id="A", source=source, started_wall=start, started_mono=mono(start),
                received_at=start, received_mono=mono(start))
    rl = ld.link_round(epoch="E1", invocation_id="A", round_id=link["round_id"], report_schema=link["report_schema"],
                       validity_contract=link["validity_contract"], received_at=link["received_at"],
                       received_mono=link["received_mono"])
    rf = None
    if fin is not None:
        rf = ld.finish(epoch="E1", invocation_id="A", round_id=fin["round_id"], report_schema=fin["report_schema"],
                       validity_contract=fin["validity_contract"], finished_wall=fin["finished_wall"],
                       finished_mono=fin["finished_mono"], selected_summary=fin["selected_summary"],
                       telemetry_error_present=fin["telemetry_error_present"], received_at=fin["received_at"],
                       received_mono=fin["received_mono"])
    return ld, rl, rf


LINK_KEYS = {"ok", "source", "round_id", "report_schema", "validity_contract", "selected_summary",
             "telemetry_error_present", "received_at", "received_mono"}
FIN_KEYS = LINK_KEYS | {"finished_wall", "finished_mono"}


# ═════════ 형태·상수 대조 ═════════

def test_registry_matches_producer_constants():
    assert (ir.SCHEMA_VERSION, ir.VALIDITY_CONTRACT) == ax.REGISTRY["investing"]
    assert (br.SCHEMA_VERSION, br.VALIDITY_CONTRACT) == ax.REGISTRY["bs"] == ax.REGISTRY["citi"]


def test_link_projection_shape():
    r, _ = investing_i1()
    a = link_args(r, "investing", at=T - S1 // 2)
    assert set(a) == LINK_KEYS and a["ok"] is True
    assert (a["source"], a["round_id"], a["report_schema"], a["validity_contract"]) == \
        ("investing", r.round_id, 3, "investing_range_checked/2")
    assert a["selected_summary"] is None and a["telemetry_error_present"] is None
    assert (a["received_at"], a["received_mono"]) == (T - S1 // 2, mono(T - S1 // 2))


def test_link_projection_right_after_creation_before_any_attempt():
    r = ir.InvestingReport(CaptureLogger(), P)                        # 생성 직후(최종화 전)도 신원 투영 가능
    a = link_args(r, "investing")
    assert a["ok"] is True and a["round_id"] == r.round_id


# ═════════ Investing I1~I5 ═════════

def test_I1_investing_normal_projection_and_ledger():
    r, _ = investing_i1()
    f = fin_args(r, "investing")
    assert set(f) == FIN_KEYS and f["ok"] is True
    assert (f["source"], f["round_id"], f["report_schema"], f["validity_contract"]) == \
        ("investing", r.round_id, 3, "investing_range_checked/2")
    s = f["selected_summary"]
    assert set(s) == {"collection", "writing", "final_db"} and s["final_db"] == "not_checked"
    assert status_reason(s["collection"]) == {p: ("valid", "validated") for p in P}
    assert status_reason(s["writing"]) == {"usd-krw": ("unknown", "per_currency_write_unverified"),
                                           "jpy-krw": ("not_attempted", "not_submitted_to_writer"),
                                           "eur-krw": ("not_attempted", "not_submitted_to_writer")}
    assert s["collection"] == r._collection() and s["writing"] == r._writing()   # 생산자 선택 그대로(재판정 없음)
    assert f["telemetry_error_present"] is False
    assert (f["finished_wall"], f["finished_mono"]) == (T, mono(T))
    ld, rl, rf = through_ledger("investing", link_args(r, "investing", at=T - S1 // 2), f)
    assert rl["classification"] == "linked" and rf["classification"] == "finalized"
    d = rf["changes"][0]["detail"]
    assert d["contribution"]["collection"] == {"V": 3, "M": 0, "U": 0, "N": 0}
    assert d["contribution"]["writing"] == {"unknown": 1, "not_attempted": 2}
    assert d["contribution"]["final_db"] == {"unknown": 3}


def test_I2_investing_cooldown():
    r = ir.InvestingReport(CaptureLogger(), P)
    r.cooldown()
    r.finish()
    f = fin_args(r, "investing")
    assert status_reason(f["selected_summary"]["collection"]) == {p: ("not_attempted", "cooldown") for p in P}
    assert status_reason(f["selected_summary"]["writing"]) == {p: ("not_attempted", "not_submitted_to_writer") for p in P}
    assert f["telemetry_error_present"] is False
    _, _, rf = through_ledger("investing", link_args(r, "investing", at=T - S1 // 2), f)
    assert rf["classification"] == "finalized"
    assert rf["changes"][0]["detail"]["contribution"]["collection"] == {"V": 0, "M": 0, "U": 0, "N": 3}


def test_I3_investing_writer_telemetry_failure():
    r = ir.InvestingReport(CaptureLogger(), P)
    r.telemetry_failed("writer_started", 1)
    r.finish()
    f = fin_args(r, "investing")
    assert status_reason(f["selected_summary"]["writing"]) == {p: ("unknown", "telemetry_error") for p in P}
    assert f["selected_summary"]["collection"] == r._collection()
    assert f["telemetry_error_present"] is True
    ld, _, rf = through_ledger("investing", link_args(r, "investing", at=T - S1 // 2), f)
    assert rf["classification"] == "finalized" and "telemetry_error" in rf["diagnostics"]["codes"]
    s = ld.aggregation_snapshot(as_of=T + 71 * 60 * S1, as_of_mono=mono(T + 71 * 60 * S1))
    assert [x for x in s["cumulative_rounds"] if x["source"] == "investing"][0]["telemetry_error_rounds"] == 1


def test_I4_investing_detail_payload_drift():
    r, cap = investing_i1()
    payload = cap.last("investing_round_finished")
    assert payload["format"] == "detail"                              # USD 만 writer 제출 → detail
    f = fin_args(r, "investing")
    s = f["selected_summary"]
    assert s["writing"] == payload["writing"] and s["final_db"] == payload["final_db"]
    attempts = {a["attempt_id"]: a for a in payload["attempts"]}
    for p in P:
        aid = payload["collection_attempts"][p]
        assert s["collection"][p] == {**attempts[aid]["collection"][p], "attempt_id": aid}
    assert f["telemetry_error_present"] is bool(payload["telemetry_errors"])


def test_I5_investing_compact_payload_drift():
    cap = CaptureLogger()
    r = ir.InvestingReport(cap, P)
    r.session_state("open")
    r.start_attempt(1)
    for p in P:
        r.observation(1, p, text=str(int(RATES[p])), rate=RATES[p])
    r.writer_started(1, dict(RATES))
    r.writer_finished(1, count=3)
    r.finish_attempt(1)
    r.session_state("closed")
    r.finish()
    payload = cap.last("investing_round_finished")
    assert (payload["format"], payload["outcome"], payload["fx_attempt_id"]) == ("compact", "all_valid", 1)
    f = fin_args(r, "investing")
    s = f["selected_summary"]
    assert status_reason(s["collection"]) == {p: ("valid", "validated") for p in P}
    assert {p: s["collection"][p]["normalized_rate"] for p in P} == payload["rates"]
    assert status_reason(s["writing"]) == {p: ("unknown", "per_currency_write_unverified") for p in P}
    assert s["final_db"] == payload["final_db"] == "not_checked" and f["telemetry_error_present"] is False


# ═════════ Bank B1~B3 ═════════

def test_B1_bs_unconfirmed_evidence_not_upgraded():
    r, cap = bank_b1("bs")
    f = fin_args(r, "bs")
    assert (f["source"], f["report_schema"], f["validity_contract"]) == ("bs", 1, "bank_v2_evidence/1")
    s = f["selected_summary"]
    assert status_reason(s["collection"]) == {"usd-krw": ("unknown", "v2_evidence_unconfirmed"),
                                              "jpy-krw": ("unknown", "unobserved"),
                                              "eur-krw": ("unknown", "unobserved")}
    assert status_reason(s["writing"]) == {p: ("not_attempted", "not_submitted_to_writer") for p in P}
    assert s["final_db"] == "not_checked" and f["telemetry_error_present"] is False
    summary = cap.last("bank_round_finished")["summary"]
    assert s == summary                                               # emit detail 요약과 같은 값
    _, rl, rf = through_ledger("bs", link_args(r, "bs", at=T - S1 // 2), f)
    assert rf["classification"] == "finalized"
    assert rf["changes"][0]["detail"]["contribution"]["collection"] == {"V": 0, "M": 0, "U": 3, "N": 0}


def test_B2_citi_missing_eur():
    cap = CaptureLogger()
    r = br.BankReport(cap, "citi", P, (br.OFFICIAL_PRIMARY,))
    r.start_attempt(br.OFFICIAL_PRIMARY)
    r.missed(br.OFFICIAL_PRIMARY, "eur-krw", "not_matched")
    r.finish_attempt(br.OFFICIAL_PRIMARY)
    r.finish()
    f = fin_args(r, "citi")
    s = f["selected_summary"]
    assert status_reason(s["collection"]) == {"usd-krw": ("unknown", "unobserved"),
                                              "jpy-krw": ("unknown", "unobserved"),
                                              "eur-krw": ("missing", "no_value")}
    assert f["source"] == "citi" and s == cap.last("bank_round_finished")["summary"]
    _, _, rf = through_ledger("citi", link_args(r, "citi", at=T - S1 // 2), f)
    assert rf["changes"][0]["detail"]["contribution"]["collection"] == {"V": 0, "M": 1, "U": 2, "N": 0}


def test_B3_bank_writer_telemetry_failure():
    cap = CaptureLogger()
    r = br.BankReport(cap, "bs", P, (br.OFFICIAL_PRIMARY,))
    r.telemetry_failed("writer_started", br.OFFICIAL_PRIMARY)
    r.finish()
    f = fin_args(r, "bs")
    assert status_reason(f["selected_summary"]["writing"]) == {p: ("unknown", "telemetry_error") for p in P}
    assert f["telemetry_error_present"] is True
    assert bool(r.telemetry_errors) == bool(r.telemetry_error_counts)        # 두 구조 드리프트 검출(S5a.2)
    assert set(r.telemetry_errors) == set(r.telemetry_error_counts)


def test_B3b_bank_telemetry_counts_alone_still_flag():
    """OR 규칙 구별 입력: 목록이 비어도 계수에 기록이 있으면 telemetry=true(S5a.2)."""
    r, _ = bank_b1("bs")
    r.telemetry_error_counts["observed"] = 1                        # 목록에는 없고 계수에만 있는 드리프트 상태
    assert r.telemetry_errors == []
    assert fin_args(r, "bs")["telemetry_error_present"] is True


# ═════════ M1 축 결함 보존 ═════════

_ABSENT = object()


def _put(mapping, key, bad):
    if bad is _ABSENT:
        del mapping[key]
    else:
        mapping[key] = bad


@pytest.mark.parametrize("bad", [_ABSENT, {}, None, {"status": "exploded", "reason": "x"}],
                         ids=["absent", "empty", "null", "enum"])
@pytest.mark.parametrize("axis", ["collection", "writing"])
@pytest.mark.parametrize("kind", ["investing", "bank"])
def test_M1_malformed_item_preserved_and_isolated_by_ledger(kind, axis, bad, monkeypatch):
    if kind == "investing":
        r, _ = investing_i1()
        method = "_collection" if axis == "collection" else "_writing"
        orig = getattr(r, method)

        def patched():
            c = orig()
            _put(c, "eur-krw", bad)
            return c
        monkeypatch.setattr(r, method, patched)
        src = "investing"
    else:
        r, _ = bank_b1("bs")
        orig = r._summary

        def patched(attempts):
            s = orig(attempts)
            _put(s[axis], "eur-krw", bad)
            return s
        monkeypatch.setattr(r, "_summary", patched)
        src = "bs"
    f = fin_args(r, src)
    assert f["ok"] is True
    base_r = investing_i1()[0] if kind == "investing" else bank_b1("bs")[0]   # 같은 입력의 무결함 기준
    _, _, base = through_ledger(src, link_args(base_r, src, at=T - S1 // 2), fin_args(base_r, src))
    base_pairs = base["changes"][0]["detail"]["pairs"]
    if bad is _ABSENT:
        assert "eur-krw" not in f["selected_summary"][axis]
    else:
        assert f["selected_summary"][axis]["eur-krw"] == bad
    _, _, rf = through_ledger(src, link_args(r, src, at=T - S1 // 2), f)
    d = rf["changes"][0]["detail"]
    assert d["pairs"]["eur-krw"][axis] == {"status": "unknown", "reason": "report_malformed"}
    other = "writing" if axis == "collection" else "collection"
    assert d["pairs"]["eur-krw"][other] == base_pairs["eur-krw"][other]      # 다른 축은 정확히 유지
    for p in ("usd-krw", "jpy-krw"):
        assert d["pairs"][p] == base_pairs[p]
    expected_usd = ("valid", "validated") if kind == "investing" else ("unknown", "v2_evidence_unconfirmed")
    assert (d["pairs"]["usd-krw"]["collection"]["status"], d["pairs"]["usd-krw"]["collection"]["reason"]) == expected_usd


# ═════════ F 실패 벡터 ═════════

FAIL_KEYS = {"ok", "reason"}


def test_F1_report_missing():
    for fn in (lambda: link_args(None, "investing"), lambda: fin_args(None, "investing")):
        a = fn()
        assert a == {"ok": False, "reason": "report_missing"}


def test_F2_source_mismatch():
    r, _ = bank_b1("bs")
    assert link_args(r, "citi") == {"ok": False, "reason": "source_mismatch"}
    assert fin_args(r, "citi") == {"ok": False, "reason": "source_mismatch"}
    ri, _ = investing_i1()
    assert fin_args(ri, "bs") == {"ok": False, "reason": "source_mismatch"}


def test_unsupported_report_objects():
    class Other:
        round_id = "x"
    assert fin_args(Other(), "investing") == {"ok": False, "reason": "unsupported_report"}
    kb = br.BankReport(CaptureLogger(), "kb", P, (br.OFFICIAL_PRIMARY,))   # D7 Tier 1 지원 밖 은행
    assert link_args(kb, "kb") == {"ok": False, "reason": "unsupported_report"}


def test_F4_not_finalized():
    r = ir.InvestingReport(CaptureLogger(), P)
    r.start_attempt(1)
    assert fin_args(r, "investing") == {"ok": False, "reason": "report_not_finalized"}
    b = br.BankReport(CaptureLogger(), "bs", P, (br.OFFICIAL_PRIMARY,))
    assert fin_args(b, "bs") == {"ok": False, "reason": "report_not_finalized"}


@pytest.mark.parametrize("rid", ["", None, 123])
def test_F5_invalid_identity(rid):
    r, _ = investing_i1()
    r.round_id = rid
    assert link_args(r, "investing") == {"ok": False, "reason": "invalid_identity"}
    assert fin_args(r, "investing") == {"ok": False, "reason": "invalid_identity"}


def test_projection_failure_is_reported_not_synthesized(monkeypatch):
    r, _ = bank_b1("bs")

    def boom(*_a, **_k):
        raise RuntimeError("summary broke")
    monkeypatch.setattr(r, "_summary", boom)
    assert fin_args(r, "bs") == {"ok": False, "reason": "projection_failed"}


def test_F3a_unregistered_contract_value_preserved(monkeypatch):
    monkeypatch.setattr(br, "VALIDITY_CONTRACT", "bank_v2_evidence/9")
    r, _ = bank_b1("bs")
    a = link_args(r, "bs")
    assert a["ok"] is True and a["validity_contract"] == "bank_v2_evidence/9"
    ld = lg.RoundLedger("E1", aggregation_started_at=T - 2 * S1)
    ld.register(epoch="E1", invocation_id="A", source="bs", started_wall=T - S1, started_mono=mono(T - S1),
                received_at=T - S1, received_mono=mono(T - S1))
    rl = ld.link_round(epoch="E1", invocation_id="A", round_id=a["round_id"], report_schema=a["report_schema"],
                       validity_contract=a["validity_contract"], received_at=T, received_mono=mono(T))
    assert rl["classification"] == "unregistered_contract"


def test_F3b_contract_changes_after_link_is_mixed(monkeypatch):
    cap = CaptureLogger()
    r = br.BankReport(cap, "bs", P, (br.OFFICIAL_PRIMARY,))
    link = link_args(r, "bs", at=T - S1 // 2)                        # 링크 → 상수 변경 → finish() → 종료 투영
    monkeypatch.setattr(br, "VALIDITY_CONTRACT", "bank_v2_evidence/9")
    r.start_attempt(br.OFFICIAL_PRIMARY)
    r.observed(br.OFFICIAL_PRIMARY, "usd-krw", rate_text="1200", rate=1200.0)
    r.finish_attempt(br.OFFICIAL_PRIMARY)
    r.finish()
    assert cap.last("bank_round_finished")["validity_contract"] == "bank_v2_evidence/9"   # 종료 보고가 실제로 쓴 값
    f = fin_args(r, "bs")
    assert f["ok"] is True and f["validity_contract"] == "bank_v2_evidence/9"
    _, rl, rf = through_ledger("bs", link, f)
    assert rl["classification"] == "linked" and rf["classification"] == "contract_mixed"
    assert rf["changes"] == []


# ═════════ D1 사본 독립·재전달 ═════════

def test_D1_projection_is_deep_copy_and_redelivery():
    r, _ = investing_i1()
    f = fin_args(r, "investing")
    frozen = copy.deepcopy(f)
    r.attempts[1]["collection"]["usd-krw"]["status"] = "missing"      # 원자료 변경
    r.attempts[1]["writer"]["input_pairs"].append("jpy-krw")
    assert f == frozen                                                # 보관 결과 불변
    f["selected_summary"]["collection"]["usd-krw"]["status"] = "hacked"
    assert r.attempts[1]["collection"]["usd-krw"]["status"] == "missing"   # 역방향도 공유 없음
    f = copy.deepcopy(frozen)
    ld, _, rf = through_ledger("investing", link_args(r, "investing", at=T - S1 // 2), f)
    assert rf["classification"] == "finalized"
    again = ld.finish(epoch="E1", invocation_id="A", round_id=f["round_id"], report_schema=3,
                      validity_contract="investing_range_checked/2", finished_wall=T, finished_mono=mono(T),
                      selected_summary=frozen["selected_summary"], telemetry_error_present=False,
                      received_at=T + S1, received_mono=mono(T + S1))
    assert again["classification"] == "duplicate_finish"
    newer = fin_args(r, "investing", at=T + 2 * S1)                   # 바뀐 원자료의 새 투영 → digest 다름
    conflict = ld.finish(epoch="E1", invocation_id="A", round_id=newer["round_id"], report_schema=3,
                         validity_contract="investing_range_checked/2", finished_wall=T, finished_mono=mono(T),
                         selected_summary=newer["selected_summary"], telemetry_error_present=False,
                         received_at=T + 2 * S1, received_mono=mono(T + 2 * S1))
    assert conflict["classification"] == "conflicting_finish"


def test_same_state_same_result_and_report_not_mutated():
    r, cap = bank_b1("bs")
    before_attempts = copy.deepcopy(r.attempts)
    n_lines = len(cap.lines)
    a, b = fin_args(r, "bs"), fin_args(r, "bs")
    assert a == b
    assert r.attempts == before_attempts and len(cap.lines) == n_lines   # 객체 불변, emit 재호출 없음


def test_projection_after_logger_failure_on_finish():
    bad = CaptureLogger(fail=True)
    rep = ir.InvestingReport(bad, P)
    rep.start_attempt(1)
    for p in P:
        rep.observation(1, p, text=str(int(RATES[p])), rate=RATES[p])
    rep.finish_attempt(1)
    ir.safely_report(rep, "finish")                                  # logger 가 실패해도 최종화는 됐다
    f = fin_args(rep, "investing")
    assert f["ok"] is True
    assert status_reason(f["selected_summary"]["collection"]) == {p: ("valid", "validated") for p in P}


def test_times_are_passed_through_unchanged():
    r, _ = investing_i1()
    f = ad.report_finish_args(r, expected_source="investing", finished_wall=7, finished_mono=8,
                              received_at=9, received_mono=10)
    assert (f["finished_wall"], f["finished_mono"], f["received_at"], f["received_mono"]) == (7, 8, 9, 10)


# ═════════ 모듈 제약 ═════════

def _resolve(mod, level, pkg="app"):
    if level == 0:
        return mod or ""
    base = pkg.split(".")[: len(pkg.split(".")) - (level - 1)]
    return ".".join(base + ([mod] if mod else []))


def _adapter_violations(source):
    """계약 S5a.1: 어댑터는 ledger·logger·파일·DB·네트워크·시계를 읽거나 쓰지 않는다(쓰지 않는 import 자체는 막지 않는다)."""
    tree = ast.parse(source)
    bind, imported = {}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for x in n.names:
                imported.add(x.name)                                   # import a.b 는 a 만 바인딩하지만 a.b 를 적재한다
                bind[(x.asname or x.name).split(".")[0]] = x.name if x.asname else x.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom):
            base = _resolve(n.module, n.level)
            for x in n.names:
                bind[x.asname or x.name] = f"{base}.{x.name}" if base else x.name
    targets = set(bind.values()) | imported
    banned_mods = ("app.d7_round_ledger", "app.database", "app.crud", "app.scheduler", "sqlalchemy", "requests",
                   "socket", "urllib", "http")
    bad = ["import " + m for m in targets if any(m == b or m.startswith(b + ".") for b in banned_mods)]
    for c in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        f, attrs = c.func, []
        while isinstance(f, ast.Attribute):
            attrs.insert(0, f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            q = ".".join([bind.get(f.id, f.id)] + attrs)
            if q.startswith(("time.", "datetime.", "os.", "io.", "pathlib.", "logging.")) or q in ("open", "print"):
                bad.append(q)
        if attrs and attrs[-1] in ("emit", "info", "debug", "warning", "error", "exception", "critical"):
            bad.append("." + attrs[-1])
    return bad


def test_adapter_module_has_no_clock_io_or_ledger_dependency():
    assert _adapter_violations((REPO / "app" / "d7_report_adapter.py").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("src", [
    "from app import d7_round_ledger", "from . import d7_round_ledger", "import app.d7_round_ledger",
    "from .d7_round_ledger import RoundLedger", "import time\ntime.monotonic_ns()", "import time as t\nt.time()",
    "from datetime import datetime\ndatetime.now()", "open('x')", "import logging\nlogging.getLogger().info('x')",
    "def f(r):\n    r.emit('e')"])
def test_module_check_catches_forbidden_forms(src):
    assert _adapter_violations(src) != []


@pytest.mark.parametrize("src", ["import time", "import copy\ncopy.deepcopy({})",
                                 "from app.crawlers import bank_report as br\nbr.BankReport"])
def test_module_check_allows_harmless_forms(src):
    assert _adapter_violations(src) == []


@pytest.mark.parametrize("kind", ["investing", "bank"])
def test_adapter_makes_its_own_deep_copy_even_if_producer_shares_objects(kind, monkeypatch):
    """생산자가 내부 객체를 그대로 돌려주더라도 어댑터 결과는 독립 사본이어야 한다(S5a.1)."""
    if kind == "investing":
        r, _ = investing_i1()
        shared = r._collection()
        monkeypatch.setattr(r, "_collection", lambda: shared)
        src = "investing"
    else:
        r, _ = bank_b1("bs")
        whole = r._summary(r._attempt_payloads())
        monkeypatch.setattr(r, "_summary", lambda _attempts: whole)
        shared = whole["collection"]
        src = "bs"
    f = fin_args(r, src)
    before = copy.deepcopy(f["selected_summary"]["collection"])
    shared["usd-krw"]["status"] = "mutated"
    shared["usd-krw"].setdefault("observation_sequences", []).append(999)
    assert f["selected_summary"]["collection"] == before


# ───────── 변이 배터리 생존 보강(시험 잠금 뒤 추가 — Codex 재승인 대상) ─────────

def test_unsupported_bank_object_is_unsupported_even_with_supported_expected_source():
    """kb 보고 객체를 지원 source 로 투영해도 미지원 객체가 먼저다(S5a.1 — source_mismatch 가 아님)."""
    kb = br.BankReport(CaptureLogger(), "kb", P, (br.OFFICIAL_PRIMARY,))
    kb.finish()
    assert link_args(kb, "bs") == {"ok": False, "reason": "unsupported_report"}
    assert fin_args(kb, "bs") == {"ok": False, "reason": "unsupported_report"}
