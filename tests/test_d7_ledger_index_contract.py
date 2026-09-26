"""D7 5a-2 계약 — RoundLedger 색인: 방문 상한·출력 동등성·시험 주입 뒤 동작(운영 경로 무접촉).

세부 계약은 Claude·Codex 합의본 `design/d7-aggregation/slice5a2_contract_r2.md`
(sha256 9fcdb4155b35791b209e1153be821a5b0c593db78785fe950c0f1351c5b54383, Codex 작성 — r1 과 Claude 검토 R1~R4 반영).
벡터 이름 V0~V8 은 그 문서 §4 표를 따른다. 계약 시험은 Claude 가 먼저 쓰고 해시로 고정, 구현은 Codex.

정상 경로는 52c0c44 의 ledger 동결 사본(tests/fixtures/d7_round_ledger_52c0c44.py)과 **같은 입력을 같은 순서로** 넣어
반환·Record·Health 를 dict 키 순서까지 대조한다. 방문 수는 새 ledger 의 `_records` 를 계수 dict 로 바꿔 세고,
D(그 호출에서 실제로 닫히거나 overdue 가 된 Record 수)는 동결 사본의 호출 전후 상태 차이로 구한다.
§3.3 의 의도적 변경(missing_record 뒤 latch 전 contributions_open·aggregation_snapshot)은 V7 의 명시 판정으로 잠근다.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

from app import d7_round_axes as ax
from app import d7_round_ledger as lg

ROOT = Path(__file__).resolve().parents[1]
FROZEN_PATH = ROOT / "tests" / "fixtures" / "d7_round_ledger_52c0c44.py"
FROZEN_SHA256 = "50eca3e21b9a1e9cea559429f02e8b14bdd1d9b2b6f6fa9c817a88af3f4dee4c"

P = ("usd-krw", "jpy-krw", "eur-krw")
S1 = 10 ** 6
MIN = 60 * S1
T = 1789984800000000                      # 2026-09-21T10:00:00Z
OFF = T - 10 ** 12
OVERDUE = 900_000_000
N = 240                                   # 전수 순회면 64+3(D+K) 를 넘도록 잡은 작은 N


def _load_frozen():
    spec = importlib.util.spec_from_file_location("app._frozen_d7_round_ledger_52c0c44", FROZEN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


old = _load_frozen()


def test_frozen_baseline_is_byte_identical_to_52c0c44():
    assert hashlib.sha256(FROZEN_PATH.read_bytes()).hexdigest() == FROZEN_SHA256


def mono(w):
    return w - OFF


def hm(hh, mm, ss=0, us=0):
    return T + ((hh - 10) * 60 + mm) * MIN + ss * S1 + us


def a(status, reason):
    return {"status": status, "reason": reason}


def summary(kind="V", reason=None):
    if kind == "V":
        col = a("valid", "validated")
    elif kind == "M":
        col = a("missing", "no_value")
    else:
        col = a("unknown", reason or "probe_unknown")
    return {"collection": {p: dict(col) for p in P}, "writing": {p: a("performed", "committed") for p in P},
            "final_db": "not_checked"}


def row(rows, source="bs", pair="usd-krw"):
    hit = [r for r in rows if r["source"] == source and r["pair"] == pair]
    assert len(hit) == 1
    return hit[0]


def canon(value):
    """dict 키 순서까지 드러내는 비교용 형태."""
    if isinstance(value, dict):
        return ("dict", [(k, canon(v)) for k, v in value.items()])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [canon(v) for v in value])
    return value


class Counted(dict):
    """계약 S5a.4 의 계수 래퍼: Record 를 꺼내는 values/items/get/__getitem__ 을 센다."""

    visits = 0

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.visits += 1
        return value

    def get(self, key, default=None):
        if key in self:
            self.visits += 1
        return super().get(key, default)

    def values(self):
        for value in super().values():
            self.visits += 1
            yield value

    def items(self):
        for key, value in super().items():
            self.visits += 1
            yield key, value


def _state(ld):
    return {inv: (rec["closed"], rec["lifecycle"]) for inv, rec in dict.items(ld._records)}


def _transitions(before, after):
    d = 0
    for inv, (closed, life) in after.items():
        b_closed, b_life = before.get(inv, (closed, life))
        if closed and not b_closed:
            d += 1
        elif life == "overdue" and b_life != "overdue" and inv in before:
            d += 1
    return d


def _k(method, result):
    if not isinstance(result, dict) or result.get("classification") != "snapshot":
        return 0
    if method == "cohort_snapshot":
        return result["registered_invocations"]
    if method == "aggregation_snapshot":
        return sum(r["rounds"] for r in result["recent_rounds"])
    if method == "contributions_open":
        return len(result["entries"])
    return 0


class Dual:
    """같은 입력을 동결 사본(old)과 현재 ledger(new)에 넣고 결과·방문 수를 잰다."""

    def __init__(self, origin=None, **limits):
        self.origin = hm(9, 59) if origin is None else origin
        self.old = old.RoundLedger("E1", aggregation_started_at=self.origin, limits=limits or None)
        self.new = lg.RoundLedger("E1", aggregation_started_at=self.origin, limits=limits or None)
        assert type(self.new._records) is dict and not self.new._records
        self.new._records = Counted()
        self.now = self.origin
        self.last = None

    def call(self, method, *, compare=True, **kw):
        before = _state(self.old)
        err_old = err_new = None
        try:
            r_old = getattr(self.old, method)(**copy.deepcopy(kw))
        except Exception as exc:      # 동결 사본의 예외도 대조 대상
            r_old, err_old = None, exc
        d = _transitions(before, _state(self.old))
        self.new._records.visits = 0
        try:
            r_new = getattr(self.new, method)(**copy.deepcopy(kw))
        except Exception as exc:
            r_new, err_new = None, exc
        visits = self.new._records.visits
        if compare:
            assert type(err_old).__name__ == type(err_new).__name__, (method, err_old, err_new)
            assert canon(r_old) == canon(r_new), method
        k = _k(method, r_old if compare else r_new)
        self.last = {"method": method, "visits": visits, "D": d, "K": k}
        if err_new is not None:
            raise err_new
        return r_new

    def bound_ok(self):
        x = self.last
        assert x["visits"] <= 64 + 3 * (x["D"] + x["K"]), x

    def _rx(self, w):
        assert w >= self.now, "시험 작성 오류: 수신 시계가 역행"
        self.now = w
        return {"received_at": w, "received_mono": mono(w)}

    def reg(self, inv, start=None, at=None, src="bs", job_id=None, serial=False, **kw):
        start = self.now if start is None else start
        return self.call("register", epoch="E1", invocation_id=inv, source=src, started_wall=start,
                         started_mono=mono(start), job_id=job_id, serial_job=serial,
                         **self._rx(max(self.now, start) if at is None else at), **kw)

    def link(self, inv, at=None, src="bs", **kw):
        schema, contract = ax.REGISTRY[src]
        return self.call("link_round", epoch="E1", invocation_id=inv, round_id="r" + inv, report_schema=schema,
                         validity_contract=contract, **self._rx(self.now if at is None else at), **kw)

    def fin(self, inv, fw, at=None, src="bs", s=None, rid=None, **kw):
        schema, contract = ax.REGISTRY[src]
        return self.call("finish", epoch="E1", invocation_id=inv, round_id=rid or "r" + inv, report_schema=schema,
                         validity_contract=contract, finished_wall=fw, finished_mono=mono(fw),
                         selected_summary=s if s is not None else summary(), telemetry_error_present=False,
                         **self._rx(self.now if at is None else at), **kw)

    def init_fail(self, inv, fw, at=None):
        return self.call("report_init_failed", epoch="E1", invocation_id=inv, failed_wall=fw,
                         failed_mono=mono(fw), **self._rx(self.now if at is None else at))

    def exit(self, inv, ew, at=None):
        return self.call("wrapper_exited", epoch="E1", invocation_id=inv, exited_wall=ew,
                         exited_mono=mono(ew), **self._rx(self.now if at is None else at))

    def cohort(self, at, lo, hi, src="bs", **kw):
        self._rx(at)
        return self.call("cohort_snapshot", source=src, cohort_start=lo, cohort_end=hi,
                         as_of=at, as_of_mono=mono(at), **kw)

    def snap(self, at, **kw):
        self._rx(at)
        return self.call("aggregation_snapshot", as_of=at, as_of_mono=mono(at), **kw)

    def contrib(self, at, after_seq=0, limit=16, **kw):
        self._rx(at)
        return self.call("contributions_open", as_of=at, as_of_mono=mono(at), after_seq=after_seq, limit=limit,
                         **kw)

    def inject(self, inv, fault):
        self.old._inject_identity_fault_for_test(invocation_id=inv, fault=fault)
        self.new._inject_identity_fault_for_test(invocation_id=inv, fault=fault)

    def arm_merge_failure(self, bucket_end):
        self.old._inject_cumulative_merge_failure_for_test(bucket_end=bucket_end)
        self.new._inject_cumulative_merge_failure_for_test(bucket_end=bucket_end)

    def same_records(self):
        ids = list(dict.keys(self.old._records))
        assert ids == list(dict.keys(self.new._records))
        for inv in ids:
            assert canon(self.old.record(inv)) == canon(self.new.record(inv)), inv

    def same_internals(self):
        for name in ("_cumulative_rows", "_cumulative_rounds", "_cumulative_end", "_cumulative_first",
                     "_cumulative_last", "_post_close", "_cumulative_evidence_uncertain", "_health"):
            assert canon(getattr(self.old, name)) == canon(getattr(self.new, name)), name


def populate(g, n=N, *, src_cycle=("bs",), finish=True, fw_of=None, s_of=None, order=None, at_fin=None):
    """n 개 호출을 등록·연결하고(seq 순), 요청하면 주어진 순서로 종료한다."""
    ids = [f"i{j:04d}" for j in range(n)]
    srcs = {inv: src_cycle[j % len(src_cycle)] for j, inv in enumerate(ids)}
    start = g.now
    for j, inv in enumerate(ids):
        g.reg(inv, start=start + j, src=srcs[inv])
        g.link(inv, src=srcs[inv])
    if finish:
        for j in (order if order is not None else range(n)):
            inv = ids[j]
            fw = fw_of(j) if fw_of else g.now
            g.fin(inv, fw, at=max(g.now, fw) if at_fin is None else at_fin, src=srcs[inv],
                  s=s_of(j) if s_of else None)
    return ids, srcs


# ───────── V0 빈 후보 ─────────

def test_V0_empty_candidates_visit_at_most_64():
    g = Dual()
    ids, _ = populate(g, fw_of=lambda j: hm(10, 0, 1) + j)
    settle = hm(10, 5)
    g.snap(settle)                                           # 첫 조회(전이 없음)로 수신 쌍을 맞춘다
    g.bound_ok()
    g.snap(settle)                                           # 같은 수신 쌍 재조회: 최근 창 K=N 이지만 D=0
    g.bound_ok()
    r = g.cohort(settle, lo=hm(10, 1), hi=hm(10, 2))          # 빈 cohort(as_of 이전 구간)
    assert r["classification"] == "snapshot" and g.last["K"] == 0
    g.bound_ok()
    r = g.cohort(settle, lo=hm(9, 59), hi=hm(10, 5), src="citi")  # 다른 source: 빈 cohort
    assert r["classification"] == "snapshot" and g.last["K"] == 0
    g.bound_ok()
    g.contrib(settle, after_seq=N)                            # 끝 cursor
    assert g.last["K"] == 0
    g.bound_ok()
    g.contrib(settle, after_seq=N - 1, limit=1)               # 마지막 한 건
    assert g.last["K"] == 1
    g.bound_ok()
    g.reg("late", start=settle)                              # 일반 수락 변경
    assert g.last["D"] == 0
    g.bound_ok()
    g.link("late")
    g.bound_ok()
    g.same_records()
    g.same_internals()


def test_V0_empty_recent_window_after_everything_closed():
    g = Dual()
    populate(g, fw_of=lambda j: hm(10, 0, 1) + j)
    g.snap(hm(12, 0))                                        # 모두 닫힘(D=N) — 상한은 D 로 넉넉하다
    g.bound_ok()
    g.snap(hm(12, 0))                                        # 같은 쌍 재조회: D=K=0
    assert (g.last["D"], g.last["K"]) == (0, 0)
    g.bound_ok()
    g.contrib(hm(12, 0))                                     # 열린 기여 없음
    g.bound_ok()
    g.same_records()
    g.same_internals()


# ───────── V1 닫힘 ─────────

def _mixed_close_fixture():
    g = Dual()
    order = list(range(N))[::-1]                             # 종료 순서 = seq 역순
    fw_of = lambda j: hm(10, j % 10, 1) + j                 # 10 개 버킷에 흩어진다
    s_of = lambda j: summary("V" if j % 3 else "M")
    ids, srcs = populate(g, src_cycle=("bs", "investing", "citi"), fw_of=fw_of, s_of=s_of, order=order,
                         at_fin=hm(10, 10))
    return g, ids


def test_V1_close_boundary_exact_and_jump():
    g, ids = _mixed_close_fixture()
    g.snap(hm(11, 10, 59, 999_999))                          # 경계 직전: target_end=10:00 → 아무것도 안 닫힘
    assert g.last["D"] == 0
    g.bound_ok()
    g.snap(hm(11, 11))                                       # 정확히 경계: 10:00 버킷만 닫힘
    assert g.last["D"] == sum(1 for j in range(N) if j % 10 == 0)
    g.bound_ok()
    g.snap(hm(11, 11))                                       # 같은 쌍 재조회
    assert g.last["D"] == 0
    g.bound_ok()
    g.contrib(hm(11, 11), after_seq=0, limit=16)
    g.bound_ok()
    g.snap(hm(11, 40))                                       # 큰 도약: 나머지 전부
    g.bound_ok()
    g.snap(hm(11, 40))
    assert (g.last["D"], g.last["K"]) == (0, 0)
    g.bound_ok()
    g.same_records()
    g.same_internals()


def test_V1_immediate_post_close_finish_is_not_left_waiting():
    g = Dual()
    ids, _ = populate(g, finish=False)
    g.snap(hm(12, 0))
    for inv in ids[:5]:                                      # close_at 이 이미 지난 종료 → 즉시 closed
        r = g.fin(inv, hm(10, 0, 30), at=hm(12, 0))
        assert r["classification"] == "post_close_finish"
        g.bound_ok()
    g.snap(hm(12, 1))
    assert g.last["D"] == 0
    g.bound_ok()
    g.contrib(hm(12, 1))
    assert g.last["K"] == 0
    g.bound_ok()
    g.same_records()
    g.same_internals()


def test_V1_isolated_open_record_leaves_open_and_recent_sets():
    g = Dual()
    ids, _ = populate(g, fw_of=lambda j: hm(10, 0, 1) + j)
    g.fin(ids[7], hm(10, 0, 30), at=hm(10, 5), s=summary("M"))   # 다른 내용 재종료 → 충돌 격리
    g.bound_ok()
    g.contrib(hm(10, 5))
    g.bound_ok()
    g.snap(hm(10, 5))
    g.bound_ok()
    g.snap(hm(11, 11))
    g.bound_ok()
    g.same_records()
    g.same_internals()


# ───────── V2 overdue ─────────

def test_V2_overdue_boundary_and_stale_non_targets():
    g = Dual()
    ids, _ = populate(g, finish=False)
    for j, inv in enumerate(ids):                            # 대부분을 비대상으로 만든다
        if j % 4 == 0:
            g.fin(inv, g.now)
        elif j % 4 == 1:
            g.init_fail(inv, g.now)
        elif j % 4 == 2:
            g.exit(inv, g.now)
    targets = [inv for j, inv in enumerate(ids) if j % 4 == 3]
    start = hm(9, 59)
    g.snap(start + OVERDUE - 1)                              # 경계 직전
    assert g.last["D"] == 0
    g.bound_ok()
    g.snap(start + OVERDUE + N)                              # 경계를 넘는다: 살아 있는 대상만 overdue
    assert g.last["D"] == len(targets)
    g.bound_ok()
    g.snap(start + OVERDUE + N)
    assert g.last["D"] == 0
    g.bound_ok()
    g.fin(targets[0], start + OVERDUE + N)                   # 늦은 종료
    g.bound_ok()
    g.same_records()
    g.same_internals()


def test_V2_all_non_targets_crossing_is_d_zero():
    g = Dual()
    ids, _ = populate(g, finish=False)
    for inv in ids:
        g.fin(inv, g.now)
    g.snap(hm(9, 59) + OVERDUE + N)
    assert g.last["D"] == 0
    g.bound_ok()
    g.same_records()


def test_V2_already_old_start_registers_overdue_immediately():
    g = Dual()
    populate(g, finish=False)
    g.reg("old", start=hm(9, 59), at=hm(9, 59) + OVERDUE + 5 * S1)
    g.bound_ok()
    g.reg("fresh", start=g.now)
    g.bound_ok()
    g.same_records()


def test_V2_wall_and_mono_disagree_uses_mono():
    g = Dual()
    ids, _ = populate(g, n=40, finish=False)
    w = hm(10, 30)
    g.now = w
    g.call("aggregation_snapshot", as_of=w, as_of_mono=mono(hm(9, 59)) + OVERDUE - 1)
    assert g.last["D"] == 0
    g.call("aggregation_snapshot", as_of=w, as_of_mono=mono(hm(9, 59)) + OVERDUE + 40)
    g.bound_ok()
    g.same_records()


# ───────── V3 직전 호출 ─────────

def test_V3_previous_same_job_with_many_other_jobs():
    g = Dual()
    populate(g, finish=False)                                # job_id 없는 다른 호출이 N 개
    g.reg("s1", start=g.now, job_id="J", serial=True)
    g.bound_ok()
    for j in range(N):
        g.reg(f"o{j}", start=g.now, job_id=f"other{j}", serial=True)
    g.reg("n1", start=g.now, job_id="J", serial=False)
    g.bound_ok()
    g.reg("s2", start=g.now, job_id="J", serial=True)       # 직전은 비직렬 n1 → 추론 없음
    g.bound_ok()
    g.reg("s3", start=g.now, job_id="J", serial=True)       # 직전은 직렬 s2 → next_entry
    g.bound_ok()
    g.reg("x1", start=g.now, src="citi", job_id="J", serial=True)
    g.bound_ok()
    g.same_records()


def test_V3_previous_finish_exit_and_backward_start():
    g = Dual()
    populate(g, n=60, finish=False)
    g.reg("s1", start=g.now, job_id="J", serial=True)
    g.link("s1")
    g.fin("s1", g.now + 5)
    g.reg("s2", start=g.now + 10, job_id="J", serial=True)
    g.bound_ok()
    g.exit("s2", g.now + 3)
    g.reg("s3", start=g.now - 1, job_id="J", serial=True)   # 이전 종료 증거보다 이른 시작 → 충돌 진단
    g.bound_ok()
    g.same_records()


def test_V3_deleted_previous_is_not_treated_as_live():
    g = Dual()
    populate(g, n=60, finish=False)
    g.reg("s1", start=g.now, job_id="J", serial=True)
    g.link("s1")
    g.inject("s1", "missing_record")
    g.reg("s2", start=g.now, job_id="J", serial=True)
    g.same_records()
    g.same_internals()


# ───────── V4 cohort ─────────

def test_V4_reverse_started_wall_ranges():
    g = Dual()
    base = hm(10, 0)
    g.now = hm(10, 30)
    for j in range(N):                                        # 등록 순서와 started_wall 역순
        g.reg(f"c{j:04d}", start=base + (N - j) // 2 * S1, at=g.now)
    for j in range(0, N, 3):
        g.link(f"c{j:04d}")
    for j in range(0, N, 6):
        g.fin(f"c{j:04d}", g.now)
    at = g.now
    for lo, hi in ((base, base + N * S1), (base + 10 * S1, base + 11 * S1), (base + 10 * S1, base + 10 * S1 + 1),
                   (base - MIN, base), (base + N * S1, base + N * S1 + MIN), (base + 1, base + 50 * S1)):
        r = g.cohort(at, lo, hi)
        assert r["classification"] == "snapshot", (lo, hi)
        g.bound_ok()
    g.same_records()


# ───────── V5 열린 페이지 ─────────

def test_V5_contributions_paging_matches_baseline():
    g = Dual()
    ids, _ = populate(g, fw_of=lambda j: hm(10, j % 3, 1) + j, order=list(range(N))[::-1], at_fin=hm(10, 5))
    g.fin(ids[10], hm(10, 0, 30), at=hm(10, 5), s=summary("M"))   # 격리
    at = hm(11, 11)                                              # 10:00 버킷 닫힘
    g.snap(at)
    for after in (0, 1, 9, 10, 11, N // 2, N - 17, N - 16, N - 2, N - 1, N):
        for limit in (1, 16):
            g.contrib(at, after_seq=after, limit=limit)
            g.bound_ok()
    g.same_records()


# ───────── V6 최근 창의 키 순서 ─────────

def test_V6_recent_dynamic_keys_follow_seq_order():
    g = Dual()
    n = 60
    fw_of = lambda j: hm(10, 20, 0) - j * S1                  # seq 가 늘수록 이른 버킷
    reasons = ("not_observed", "parse_failed", "selector_missing", "http_403", "attempt_failed", "cooldown",
               "nan_value")                                   # REASON_ENUM 안 값이어야 'other' 로 접히지 않는다
    s_of = lambda j: summary("U", reason=reasons[(n - j) % 7])
    populate(g, n=n, fw_of=fw_of, s_of=s_of, at_fin=hm(10, 21))
    first = g.snap(hm(10, 21))                                      # 창에 10:19·10:20 두 버킷
    g.bound_ok()
    keys = list(row(first["recent"])["collection_unknown_reasons"])
    assert len(keys) == 7 and keys[0] == reasons[n % 7]            # 시험 입력이 동적 키 순서 규칙을 실제로 건드린다
    for at in (hm(11, 0), hm(11, 19, 30), hm(11, 20)):
        g.snap(at)
        g.bound_ok()
    g.snap(hm(12, 0))
    g.same_internals()
    g.same_records()


# ───────── V7 손상·예외 (§3.3 새 계약) ─────────

def _missing_fixture():
    g = Dual()
    ids, _ = populate(g, fw_of=lambda j: hm(10, 0, 1) + j, at_fin=hm(10, 5))
    return g, ids


def _latched_query_is_stable(g, method, **kw):
    first = g.call(method, compare=False, **kw)
    second = g.call(method, compare=False, **kw)
    assert first["classification"] == "post_close_unverified"
    assert canon(first) == canon(second)
    assert first["health"]["index_error"] is True
    assert first["diagnostics"] == {"codes": ["identity_unverified"], "baseline_invalidated": True,
                                    "coverage_error": True, "uncertain_pairs": [],
                                    "cumulative_evidence_uncertain": False}      # B·global
    return first


@pytest.mark.parametrize("victim,after", [(0, 0), (5, 3), (5, 6), (N - 1, 0), (N - 1, N - 1)])
def test_V7_contributions_open_latches_instead_of_keyerror(victim, after):
    g, ids = _missing_fixture()
    g.inject(ids[victim], "missing_record")
    at = hm(10, 6)
    g.now = at
    r = _latched_query_is_stable(g, "contributions_open", as_of=at, as_of_mono=mono(at), after_seq=after, limit=16)
    assert r["entries"] == [] and r["next_seq"] is None
    assert (r["as_of"], r["as_of_mono"]) == (at, mono(at))          # 수신 쌍은 latch 보다 먼저 진행
    assert r["health"]["coverage_complete"] is False
    assert r["health"]["uncertain_sources"] == list(ax.REGISTRY)


@pytest.mark.parametrize("victim", [0, 7, N - 1])
def test_V7_aggregation_snapshot_latches_even_outside_window(victim):
    g, ids = _missing_fixture()
    g.inject(ids[victim], "missing_record")
    at = hm(10, 6)
    g.now = at
    r = _latched_query_is_stable(g, "aggregation_snapshot", as_of=at, as_of_mono=mono(at))
    assert (r["as_of"], r["as_of_mono"]) == (at, mono(at))
    assert r["health"]["last_received_at"] == at


def test_V7_cohort_out_of_range_deletion_latches_like_baseline():
    g, ids = _missing_fixture()
    g.inject(ids[3], "missing_record")
    at = hm(10, 6)
    r = g.cohort(at, hm(10, 2), hm(10, 3))                          # 범위 밖 삭제 — 동결 사본과 동일
    assert r["classification"] == "post_close_unverified"
    g.same_internals()


def test_V7_advance_skips_deleted_close_and_overdue_candidates():
    g = Dual()
    ids, _ = populate(g, n=80, finish=False)
    for inv in ids[:40]:
        g.fin(inv, hm(10, 0, 30), at=hm(10, 1))
    g.inject(ids[3], "missing_record")                              # 닫힘 대기 중인 열린 기여
    g.inject(ids[50], "missing_record")                             # overdue 대기 중인 in_flight
    g.reg("z1", start=hm(10, 2), at=hm(9, 59) + OVERDUE + 100)      # 변경 API 가 overdue 경계를 넘긴다
    g.reg("z2", start=hm(10, 2), at=hm(11, 11))                     # 변경 API 가 닫힘 경계를 넘긴다
    g.same_records()
    g.same_internals()


def test_V7_missing_owner_open_and_closed_isolation():
    for closed in (False, True):
        g = Dual()
        ids, _ = populate(g, n=40, fw_of=lambda j: hm(10, 0, 1) + j, at_fin=hm(10, 5))
        if closed:
            g.snap(hm(11, 11))
        g.inject(ids[4], "missing_owner")
        g.fin(ids[4], hm(10, 0, 5), at=g.now)
        g.fin(ids[5], hm(10, 0, 6), at=g.now)
        g.contrib(g.now)
        g.snap(g.now)
        g.same_records()
        g.same_internals()


# ───────── V8 merge 실패 뒤 재시도 ─────────

def test_V8_merge_failure_is_atomic_and_retry_counts_once():
    g = Dual()
    populate(g, src_cycle=("bs", "investing"), fw_of=lambda j: hm(10, j % 5, 1) + j,
             order=list(range(N))[::-1], at_fin=hm(10, 6))
    g.arm_merge_failure(hm(10, 3))
    before_old = canon(g.old.aggregation_snapshot(as_of=hm(10, 6), as_of_mono=mono(hm(10, 6))))
    with pytest.raises(Exception) as caught:
        g.snap(hm(11, 20))
    assert type(caught.value).__name__ == "CumulativeMergeFailureForTest"
    g.same_records()
    g.same_internals()
    assert canon(g.new.aggregation_snapshot(as_of=hm(10, 6), as_of_mono=mono(hm(10, 6)))) == before_old
    g.snap(hm(11, 20))                                              # fault 소모 뒤 같은 시각 재시도
    g.bound_ok()
    g.snap(hm(11, 20))
    assert g.last["D"] == 0
    g.bound_ok()
    g.same_records()
    g.same_internals()


# ───────── 계수 래퍼 우회 금지(§4) ─────────

def _walk(value, seen, depth=0):
    if depth > 8 or id(value) in seen:
        return
    seen.add(id(value))
    yield value
    if isinstance(value, dict):
        for k, v in dict.items(value):
            yield from _walk(k, seen, depth + 1)
            yield from _walk(v, seen, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _walk(v, seen, depth + 1)
    elif hasattr(value, "__dict__") and not isinstance(value, type):
        for v in vars(value).values():
            yield from _walk(v, seen, depth + 1)


def test_indexes_hold_no_record_objects():
    g, _ = _mixed_close_fixture()
    for j in range(5):
        g.reg(f"late{j}", start=g.now)
        if j % 2:
            g.link(f"late{j}")
    g.snap(g.now + OVERDUE + 1)                                # 늦은 호출 5개 overdue
    g.snap(hm(11, 11))                                         # 일부 닫힘
    ld = g.new
    records = {id(rec) for rec in dict.values(ld._records)}
    assert records
    seen = {id(ld._records)}
    for name, value in vars(ld).items():
        if name in ("_records", "_lock"):
            continue
        for item in _walk(value, seen):
            assert id(item) not in records, name


def test_gate_record_access_audit_is_clean():
    spec = importlib.util.spec_from_file_location("d7_gate_for_index", ROOT / "scripts" / "d7_ledger_measure_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert gate.audit_record_access() == []


# ───────── 변이 배터리 생존 보강(시험 잠금 뒤 추가 — Codex 재승인 대상) ─────────

def test_V2_next_entry_non_targets_crossing_is_d_zero():
    """next_entry 로 report_unavailable 이 된 직전 호출도 overdue 대기에서 빠져야 한다(M15)."""
    g = Dual()
    n = N // 2
    for j in range(n):
        g.reg(f"a{j:04d}", start=g.now, job_id=f"J{j}", serial=True)
    for j in range(n):
        g.reg(f"b{j:04d}", start=g.now, job_id=f"J{j}", serial=True)   # a{j} → next_entry
        g.link(f"b{j:04d}")
        g.fin(f"b{j:04d}", g.now)
    assert g.new.record("a0000")["unavailable_reason"] == "next_entry"
    at = hm(9, 59) + OVERDUE + 10 * S1
    r = g.cohort(at, lo=hm(10, 1), hi=hm(10, 2))                    # K=0 조회라야 남은 대기 항목 읽기가 드러난다
    assert r["classification"] == "snapshot"
    assert (g.last["D"], g.last["K"]) == (0, 0)
    g.bound_ok()
    g.same_records()
