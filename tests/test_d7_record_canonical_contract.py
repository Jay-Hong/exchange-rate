"""D7 5a-3a′ 계약 — 공유 상수의 정본 객체 참조 · 정수 표현의 None/0·극값 보존(운영 경로 무접촉).

세부 계약: `design/d7-aggregation/slice5a3_contract_r3.md` §2 (Codex 작성, Claude 합의 — r2 의 Record 당 최악 선불 철회 뒤 개정).
공개 결과는 52c0c44 동결 사본과 계속 같아야 한다(5a-2 색인 계약 시험). 메모리 수치 목표는 여기서 두지 않는다 — 3b 계상표에서 잠근다.
계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.
"""
from __future__ import annotations

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg
from tests.test_d7_ledger_index_contract import MIN, OVERDUE, S1, Dual, hm, mono, populate, summary


def _fresh(text):
    """값은 같지만 등록표 객체와 다른 새 str 객체."""
    built = "".join(list(text))
    assert built == text and built is not text or len(text) <= 1
    return built


def _canon_source(name):
    return next(key for key in ax.REGISTRY if key == name)


def _internal(ld, inv):
    return dict.__getitem__(ld._records, inv)


# ───────── §2 공유 상수: 등록표 정본 객체 참조 ─────────

@pytest.mark.parametrize("src", ["investing", "bs", "citi"])
def test_source_and_contracts_are_registry_objects(src):
    ld = lg.RoundLedger("E1", aggregation_started_at=hm(9, 59))
    at = hm(10, 0)
    schema, contract = ax.REGISTRY[src]
    r = ld.register(epoch="E1", invocation_id="i1", source=_fresh(src), started_wall=at, started_mono=mono(at),
                    received_at=at, received_mono=mono(at))
    assert r["classification"] == "registered"
    rec = _internal(ld, "i1")
    assert rec["source"] is _canon_source(src)
    assert rec["expected_validity_contract"] is contract
    r = ld.link_round(epoch="E1", invocation_id="i1", round_id="r1", report_schema=schema,
                      validity_contract=_fresh(contract), received_at=at, received_mono=mono(at))
    assert r["classification"] == "linked"
    assert rec["linked_validity_contract"] is contract


def test_enum_fields_share_objects_across_records():
    g = Dual()
    ids, _ = populate(g, n=6, finish=False)
    g.fin(ids[0], g.now)
    g.fin(ids[1], g.now)
    g.init_fail(ids[2], g.now)
    g.init_fail(ids[3], g.now)
    g.exit(ids[4], g.now)
    ld = g.new
    for field in ("connection", "lifecycle", "inclusion", "unavailable_reason", "exit_evidence"):
        pairs = [(ids[0], ids[1]), (ids[2], ids[3])]
        for a, b in pairs:
            va, vb = _internal(ld, a)[field], _internal(ld, b)[field]
            if isinstance(va, str) and va == vb:
                assert va is vb, (field, va)


def test_unregistered_source_is_not_interned():
    ld = lg.RoundLedger("E1", aggregation_started_at=hm(9, 59))
    at = hm(10, 0)
    r = ld.register(epoch="E1", invocation_id="i1", source="not-a-source", started_wall=at, started_mono=mono(at),
                    received_at=at, received_mono=mono(at))
    assert r["classification"] == "unsupported_source"
    assert "not-a-source" not in {getattr(v, "source", None) for v in dict.values(ld._records)}


# ───────── §2 정수 표현: None 과 0, 극값, 복원 ─────────

def test_zero_times_are_not_confused_with_none():
    ld = lg.RoundLedger("E1", aggregation_started_at=0)
    r = ld.register(epoch="E1", invocation_id="z", source="bs", started_wall=0, started_mono=0,
                    received_at=0, received_mono=0)
    assert r["classification"] == "registered"
    rec = ld.record("z")
    assert rec["started_wall"] == 0 and rec["started_mono"] == 0
    assert rec["started_wall"] is not None and type(rec["started_wall"]) is int
    for key in ("init_failed_wall", "init_failed_mono", "exit_evidence_wall", "exit_evidence_mono",
                "overdue_first_observed_at", "overdue_first_observed_mono", "first_finished_wall",
                "first_finished_mono", "bucket_start", "bucket_end", "close_at"):
        assert rec[key] is None, key


def test_zero_and_extreme_times_match_baseline():
    """0 근처와 큰 시각에서 동결 사본과 같은 결과(정수 압축의 폭·부호·복원)."""
    for origin in (0, lg._MAX_TIME - 200 * MIN):
        g = Dual(origin=origin)
        g.now = origin
        ids, _ = populate(g, n=5, finish=False)
        g.fin(ids[0], g.now)
        g.init_fail(ids[1], g.now)
        g.exit(ids[2], g.now)
        g.snap(origin + OVERDUE + MIN)
        g.fin(ids[3], g.now)
        g.snap(origin + 75 * MIN)
        g.same_records()
        g.same_internals()


def test_record_values_are_plain_python_types():
    g = Dual()
    ids, _ = populate(g, n=3, fw_of=lambda j: hm(10, 0, 1) + j)
    for inv in ids:
        new, old = g.new.record(inv), g.old.record(inv)
        for key in old:
            assert type(new[key]) is type(old[key]), (key, type(new[key]), type(old[key]))


# ───────── §2 ID: 비ASCII·독립 문자열 ─────────

@pytest.mark.parametrize("inv", ["가나다" * 14, "é" * 64, "mix-가-é-" + "x" * 100])
def test_non_ascii_ids_roundtrip_against_baseline(inv):
    g = Dual()
    populate(g, n=3, finish=False)
    g.reg(_fresh(inv), start=g.now, job_id=_fresh(inv), serial=True)
    schema, contract = ax.REGISTRY["bs"]
    g.call("link_round", epoch="E1", invocation_id=_fresh(inv), round_id=_fresh(inv), report_schema=schema,
           validity_contract=contract, **g._rx(g.now))
    g.fin(_fresh(inv), g.now, rid=_fresh(inv))
    g.same_records()
    assert g.new.record(inv)["invocation_id"] == inv
