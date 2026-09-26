"""D7 5a-3a 계약 — Record 압축 표현과 진단 비트 표현(운영 경로 무접촉).

세부 계약: `design/d7-aggregation/slice5a3_contract_r2.md` (Codex 작성, Claude 검토 R1~R3 반영·합의) §2, §6 의 1·2·3·7.
공개 결과는 52c0c44 동결 사본과 값·키 순서까지 같아야 하고(5a-2 색인 계약 시험이 계속 대조한다), 내부 상주 표현은 33키 dict 를 버린다.
바이트 예산·선중단(5a-3b)은 이 파일의 범위가 아니다. 계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.
"""
from __future__ import annotations

import gc
import tracemalloc

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg
from tests.test_d7_ledger_index_contract import OVERDUE, S1, MIN, Dual, canon, hm, mono, populate, summary

PER_SLOT_5A2 = 1366       # 부록 A1 실측(4,096→8,192 미종료 등록, owned_graph 기울기) — 압축 뒤 이보다 작아야 한다


# ───────── §2.1-3 공개 결과는 매번 새 사본 ─────────

def test_record_returns_fresh_independent_dict():
    g = Dual()
    ids, _ = populate(g, n=4, fw_of=lambda j: hm(10, 0, 1) + j)
    first = g.new.record(ids[0])
    second = g.new.record(ids[0])
    assert first == second and first is not second
    first["lifecycle"] = "tampered"
    first["diagnostics"]["codes"].append("tampered")
    assert g.new.record(ids[0]) == second
    assert list(second) == list(g.old.record(ids[0]))               # 33키 선언 순서


def test_results_do_not_alias_internal_state():
    g = Dual()
    ids, _ = populate(g, n=3, fw_of=lambda j: hm(10, 0, 1) + j)
    r = g.fin(ids[0], hm(10, 0, 1), at=g.now)                      # 중복 종료 결과
    for rec in r["records"]:
        rec["diagnostics"]["codes"].append("tampered")
        rec["lifecycle"] = "tampered"
    assert canon(g.new.record(ids[0])) == canon(g.old.record(ids[0]))


# ───────── §2.1-1 33키 dict 를 상주 정본으로 두지 않는다 ─────────

def test_resident_record_is_not_a_wide_dict():
    g = Dual()
    populate(g, n=8, fw_of=lambda j: hm(10, 0, 1) + j)
    for value in dict.values(g.new._records):
        assert not (isinstance(value, dict) and len(value) >= 20), type(value)


def test_per_slot_resident_growth_is_below_5a2():
    spec_gate = __import__("importlib.util").util.spec_from_file_location(
        "d7_gate_for_compact", __import__("pathlib").Path(__file__).resolve().parents[1] / "scripts" / "d7_ledger_measure_gate.py")
    gate = __import__("importlib.util").util.module_from_spec(spec_gate)
    spec_gate.loader.exec_module(gate)
    small, large = gate.owned_graph(gate.fill(4096)), gate.owned_graph(gate.fill(8192))
    assert small["unknown_types"] == [] and large["unknown_types"] == []
    per_slot = (large["bytes"] - small["bytes"]) / 4096
    assert per_slot < PER_SLOT_5A2, per_slot


# ───────── §2.2 진단 비트 표현 ─────────

def test_diag_codes_registry_roundtrip():
    codes = lg.DIAG_CODES
    assert isinstance(codes, tuple) and len(codes) == len(set(codes)) and codes
    for code in codes:
        for level in (None, "G", "B", "F"):
            for global_ in (False, True):
                d = lg._diag((code,), level, global_)
                assert lg.decode_diag(lg.encode_diag(d)) == d, (code, level, global_)


def test_diag_encode_merge_matches_dict_merge():
    codes = lg.DIAG_CODES
    a = lg._diag(codes[:3], "G")
    b = lg._diag(codes[2:6], "B", True)
    merged = lg._diag()
    lg._merge_diag(merged, a)
    lg._merge_diag(merged, b)
    assert lg.decode_diag(lg.merge_encoded_diag(lg.encode_diag(a), lg.encode_diag(b))) == merged


def test_unknown_diag_code_is_not_silently_dropped():
    with pytest.raises(ValueError):
        lg.encode_diag(lg._diag(("definitely_not_a_registered_code",)))


# ───────── §2.2 대량 overdue: 임시 256 KiB · Record 수 비례 새 객체 없음 ─────────

def _registered(n):
    ld = lg.RoundLedger("E1", aggregation_started_at=hm(9, 59))
    for j in range(n):
        ld.register(epoch="E1", invocation_id=f"i{j:06d}", source="bs", started_wall=hm(9, 59), started_mono=mono(hm(9, 59)),
                    received_at=hm(9, 59), received_mono=mono(hm(9, 59)))
    return ld


def test_mass_overdue_temporary_and_persistent_growth():
    n = 20_000
    ld = _registered(n)
    at = hm(9, 59) + OVERDUE + 10 * S1
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before, _ = tracemalloc.get_traced_memory()
        result = ld.aggregation_snapshot(as_of=at, as_of_mono=mono(at))
        after, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["classification"] == "snapshot"
    assert all(ld.record(f"i{j:06d}")["lifecycle"] == "overdue" for j in range(0, n, 997))
    assert "ever_overdue" in ld.record("i000000")["diagnostics"]["codes"]
    assert ld.record("i000000")["overdue_first_observed_at"] == at
    assert peak - before <= 262_144, peak - before
    assert after - before <= 65_536, after - before                # Record 수에 비례한 상주 증가 없음


# ───────── §6-2 최장 ID·비ASCII·닫힘 compaction 뒤 식별 판정 ─────────

LONG_ASCII = "a" * 128
LONG_KOREAN = "가" * 42                                             # 126 bytes UTF-8


@pytest.mark.parametrize("inv", [LONG_ASCII, LONG_KOREAN])
def test_longest_ids_survive_close_compaction(inv):
    g = Dual()
    populate(g, n=5, fw_of=lambda j: hm(10, 0, 1) + j)
    g.reg(inv, start=g.now, job_id=inv, serial=True)
    schema, contract = ax.REGISTRY["bs"]
    g.call("link_round", epoch="E1", invocation_id=inv, round_id=inv, report_schema=schema, validity_contract=contract,
           **g._rx(g.now))
    fw = g.now
    g.fin(inv, fw, rid=inv)
    g.snap(hm(11, 11))                                              # 닫힘 → 상세 해제·compaction
    g.same_records()
    g.fin(inv, fw, at=g.now, rid=inv)                              # 늦은 같은 재전달 → post_close_duplicate
    g.fin(inv, fw, at=g.now, rid=inv, s=summary("M"))              # 다른 내용 → post_close_conflict
    g.fin(inv, fw + S1, at=g.now, rid=inv)                         # 종료 시각을 바꿔도 같은 ID 로 판정
    g.same_records()
    g.same_internals()
    rec = g.new.record(inv)
    assert rec["first_finished_wall"] == fw and rec["first_digest"] is not None and rec["closed"] is True


def test_state_mix_then_close_matches_baseline():
    g = Dual()
    ids, _ = populate(g, n=30, finish=False)
    for j, inv in enumerate(ids):
        k = j % 6
        if k == 0:
            g.fin(inv, g.now)
        elif k == 1:
            g.init_fail(inv, g.now)
        elif k == 2:
            g.exit(inv, g.now)
        elif k == 3:
            g.fin(inv, g.now)
            g.fin(inv, g.now, s=summary("M"))                       # 충돌 격리
    g.snap(hm(9, 59) + OVERDUE + MIN)                              # 남은 호출 overdue
    g.fin(ids[4], g.now)                                            # 늦은 종료
    g.snap(hm(12, 0))                                               # 닫힘
    g.same_records()
    g.same_internals()


# ───────── 구현 검토 보강(시험 잠금 뒤 추가 — Codex 재승인 대상) ─────────

def test_cohort_counts_diag_bits_without_full_decode(monkeypatch):
    """cohort 는 선택 Record 마다 진단 전체를 복원하지 않고 비트로 센다.
    (구현 검토 실측: 131,072 cohort 가 복원 방식으로 약 1.58 초 — full 1회차 p99 76 ms 의 약 20배, 2 초 문턱에 근접)"""
    n = 500
    ld = _registered(n)
    at = hm(9, 59) + OVERDUE + 10 * S1
    ld.aggregation_snapshot(as_of=at, as_of_mono=mono(at))
    calls = {"n": 0}
    real = lg.decode_diag

    def counting(encoded):
        calls["n"] += 1
        return real(encoded)

    monkeypatch.setattr(lg, "decode_diag", counting)
    r = ld.cohort_snapshot(source="bs", cohort_start=hm(9, 58), cohort_end=hm(10, 0), as_of=at, as_of_mono=mono(at))
    assert r["classification"] == "snapshot" and r["registered_invocations"] == n and r["ever_overdue"] == n
    assert calls["n"] <= 4, calls["n"]
