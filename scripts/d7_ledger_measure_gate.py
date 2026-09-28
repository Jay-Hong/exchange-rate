#!/usr/bin/env python3
"""Offline D7 S5a.4 measurement gate. Emits one JSON object on stdout.

Run with CPython 3.13, PYTHONHASHSEED=0 and PYTHONDONTWRITEBYTECODE=1.
The quick mode checks the harness path; it cannot establish a maximum-slot pass.
"""

from __future__ import annotations

import argparse
import ast
import copy
import gc
import gzip
import hashlib
import heapq
import importlib
from itertools import islice
import json
import os
import platform
import resource
import math
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextmanager
def offline_app_import():
    """Load only the D7 modules without running app's logging initializer.

    Restore the import table afterward so an in-process caller can still import
    the real app package. Existing app imports are left alone.
    """
    if "app" in sys.modules:
        yield
        return
    existing = set(sys.modules)
    package = ModuleType("app")
    package.__path__ = [str(ROOT / "app")]
    package.__package__ = "app"
    sys.modules["app"] = package
    try:
        yield
    finally:
        for name in set(sys.modules) - existing:
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)


with offline_app_import():
    from app.d7_round_axes import PAIRS, REGISTRY  # noqa: E402
    from app.d7_round_ledger import RoundLedger, _budget_q  # noqa: E402

CAP = 131072
DETAIL_CAP = 2048
OWNED_LIMIT = 64 * 1024 * 1024
TEMP_LIMIT = 256 * 1024
MINUTE = 60_000_000
T = 1_790_000_000_000_000 // MINUTE * MINUTE
EPOCH = "d7-measure"
SOURCE = "bs"
SCHEMA, CONTRACT = REGISTRY[SOURCE]
SUMMARY = {
    "collection": {p: {"status": "unknown", "reason": "unobserved"} for p in PAIRS},
    "writing": {p: {"status": "not_attempted", "reason": "not_submitted_to_writer"} for p in PAIRS},
    "final_db": "not_checked",
}
# One valid collection distinguishes the witness ID in otherwise aggregate recent rows.
RECENT_WITNESS_SUMMARY = copy.deepcopy(SUMMARY)
RECENT_WITNESS_SUMMARY["collection"][PAIRS[0]] = {"status": "valid", "reason": "validated"}
_gate_resident_budget = ContextVar("d7_gate_resident_budget", default=62_914_560)


class HeadroomUnverified(RuntimeError):
    """A public-API fixture could not establish the planned byte reserve."""


class CountedRecords(dict):
    """Count each successful Record fetch, including repeated fetches."""

    def __init__(self, original, allowed_ids=None):
        super().__init__(original)
        self.visits = 0
        self.allowed_ids = allowed_ids
        self.out_of_range_visits = 0

    def _count(self, key):
        self.visits += 1
        if self.allowed_ids is not None and key not in self.allowed_ids:
            self.out_of_range_visits += 1

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self._count(key)
        return value

    def get(self, key, default=None):
        value = super().get(key, default)
        if key in self:
            self._count(key)
        return value

    def values(self):
        for key, value in super().items():
            self._count(key)
            yield value

    def items(self):
        for key, value in super().items():
            self._count(key)
            yield key, value


def audit_record_access(source_text=None):
    """Fail closed if ledger starts fetching Records by an uncounted route."""
    tree = ast.parse(source_text if source_text is not None else
                     (ROOT / "app/d7_round_ledger.py").read_text())
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    uses = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_records":
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.attr in {"values", "items", "get"}:
                continue
            if isinstance(parent, ast.Subscript):
                continue  # __getitem__, __setitem__, __delitem__
            if isinstance(parent, ast.Assign) and node in parent.targets:
                continue  # constructing the storage dict
            if (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                    and parent.func.id == "len"):
                continue
            if (isinstance(parent, ast.Compare) and len(parent.ops) == 1
                    and isinstance(parent.ops[0], (ast.In, ast.NotIn))
                    and parent.comparators[0] is node):
                continue  # only a key lookup with _records on the right
            uses.append((node.lineno, type(parent).__name__ if parent is not None else "root"))
    return uses


def scenario_status(*, visit_gate, temp_gate, time_gate, correct):
    gates = (visit_gate, temp_gate, time_gate)
    if not correct or "FAIL" in gates:
        return "FAIL"
    if "UNVERIFIED" in gates:
        return "UNVERIFIED"
    return "PASS"


def rid(i):
    return f"m{i:06d}"


def new_ledger(limit, max_resident_bytes=None):
    return RoundLedger(EPOCH, aggregation_started_at=T - MINUTE,
                       limits={"max_records": limit, "max_retained_details": min(DETAIL_CAP, limit),
                               "max_detail_bytes": 4096,
                               "max_resident_bytes": (max_resident_bytes if max_resident_bytes is not None
                                                      else _gate_resident_budget.get())})


def register_args(i, wall=T, *, source=SOURCE):
    return dict(epoch=EPOCH, invocation_id=rid(i), source=source,
                started_wall=T, started_mono=T, received_at=wall, received_mono=wall)


def link_args(i, wall=T):
    return dict(epoch=EPOCH, invocation_id=rid(i), round_id=f"r{i}",
                report_schema=SCHEMA, validity_contract=CONTRACT,
                received_at=wall, received_mono=wall)


def finish_args(i, wall=T, summary=SUMMARY):
    return dict(epoch=EPOCH, invocation_id=rid(i), round_id=f"r{i}",
                report_schema=SCHEMA, validity_contract=CONTRACT,
                finished_wall=T, finished_mono=T, selected_summary=summary,
                telemetry_error_present=False, received_at=wall, received_mono=wall)


def large_summary(chars):
    return {"collection": {p: {"status": "unknown", "reason": "unobserved",
                               "error_type": "x" * chars} for p in PAIRS},
            "writing": {p: {"status": "not_attempted", "reason": "not_submitted_to_writer"}
                        for p in PAIRS}, "final_db": "not_checked"}


def fill(limit, *, linked=0, finalized=0, stop=None, unavailable_rest=False):
    ledger = new_ledger(limit)
    target = stop if stop is not None else limit
    for i in range(target):
        result = ledger.register(**register_args(i))
        if result["classification"] == "admission_stopped":
            if ledger._health["N_res"] >= limit:
                raise RuntimeError(f"fixture slot stop before target {i}")
            ledger._gate_fixture_n = i
            ledger._gate_fixture_stop = "byte"
            return ledger
        if result["classification"] != "registered":
            raise RuntimeError(f"fixture register {i}: {result['classification']}")
        if i < linked or i < finalized:
            result = ledger.link_round(**link_args(i))
            if result["classification"] != "linked":
                raise RuntimeError(f"fixture link {i}: {result['classification']}")
        if i < finalized:
            result = ledger.finish(**finish_args(i))
            if result["classification"] != "finalized":
                raise RuntimeError(f"fixture finish {i}: {result['classification']}")
        elif unavailable_rest and i >= max(linked, finalized):
            result = ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i),
                                               failed_wall=T, failed_mono=T,
                                               received_at=T, received_mono=T)
            if result["classification"] != "report_init_failed":
                raise RuntimeError(f"fixture unavailable {i}: {result['classification']}")
    ledger._gate_fixture_n = target
    ledger._gate_fixture_stop = "limit"
    return ledger


def _headroom_tariffs(summary=SUMMARY, *, reverse=False, need_detail=False):
    """Measure one public registration and normalized detail on a small origin."""
    record_charges = []
    for source in (REGISTRY if reverse else (SOURCE,)):
        candidate = new_ledger(1)
        before = candidate.budget_state()
        if candidate.register(**register_args(0, source=source))["classification"] != "registered":
            raise HeadroomUnverified("headroom tariff registration unavailable")
        registered = candidate.budget_state()
        record_charges.append((registered["E"] - before["E"])
                              - (registered["Q_4"] - before["Q_4"]))
    record_charge = max(record_charges)
    if not need_detail:
        return record_charge, 4096
    probe = new_ledger(1)
    if probe.register(**register_args(0))["classification"] != "registered":
        raise HeadroomUnverified("headroom tariff detail registration unavailable")
    if probe.link_round(**link_args(0))["classification"] != "linked":
        raise HeadroomUnverified("headroom tariff link unavailable")
    if probe.finish(**finish_args(0, summary=summary))["classification"] != "finalized":
        raise HeadroomUnverified("headroom tariff detail unavailable")
    detail_charge = max(4096, probe.budget_state()["D"])
    if record_charge <= 0 or detail_charge <= 0:
        raise HeadroomUnverified("headroom tariff nonpositive")
    return record_charge, detail_charge


def _fixture_budget_e(ledger, fixed):
    """Use the ledger's incremental admission charges between public checkpoints."""
    return fixed + ledger._q_charge() + ledger._budget_d + ledger._budget_ar + ledger._budget_t


def _check_fixture_budget_e(ledger, fixed):
    budget = ledger.budget_state()
    if budget["E"] != _fixture_budget_e(ledger, fixed):
        raise RuntimeError("fixture incremental budget disagrees with public budget")
    return budget


def fill_with_headroom(limit, *, accepts, detail_accepts=0, linked=0,
                       finalized=0, unavailable_rest=False, summary=SUMMARY,
                       reserve_slots=0, reverse=False, min_existing=0):
    """Build an API origin that leaves charged room for its measured accepts."""
    if accepts < 1 or detail_accepts < 0 or reserve_slots < 0 or min_existing < 0:
        raise ValueError("invalid headroom plan")
    record_charge, detail_charge = _headroom_tariffs(
        summary, reverse=reverse, need_detail=bool(detail_accepts or finalized))
    ledger = new_ledger(limit)
    fixed = ledger.budget_state()["F_4"]
    budget_limit = ledger._limits["max_resident_bytes"]
    target = max(0, limit - reserve_slots)
    for i in range(target):
        charge = _fixture_budget_e(ledger, fixed)
        # Include the next fixture insertion as well as every later measured
        # acceptance; otherwise the last fixture insertion spends a probe's room.
        planned = min(limit, i + accepts + 1)
        future_q = _budget_q(planned) + 512 * planned - ledger._q_charge()
        required = (record_charge * (accepts + 1) + max(0, future_q)
                    + detail_charge * detail_accepts)
        if budget_limit - charge < required:
            budget = _check_fixture_budget_e(ledger, fixed)
            ledger._gate_fixture_n = i
            ledger._gate_fixture_stop = "byte_headroom"
            ledger._gate_headroom_bytes = budget["B"] - budget["E"]
            if i < min_existing:
                ledger._gate_headroom_error = "insufficient room for required existing identities"
            return ledger
        kwargs = register_args(i, source=_reverse_source(i) if reverse else SOURCE)
        if reverse:
            kwargs["started_wall"] = T - i
        result = ledger.register(**kwargs)
        if result["classification"] == "admission_stopped":
            _check_fixture_budget_e(ledger, fixed)
            ledger._gate_fixture_n = i
            ledger._gate_fixture_stop = "byte_headroom"
            ledger._gate_headroom_bytes = budget_limit - charge
            ledger._gate_headroom_error = "registration rejected before planned headroom stop"
            return ledger
        if result["classification"] != "registered":
            raise RuntimeError(f"headroom fixture register {i}: {result['classification']}")
        if i < linked or i < finalized:
            result = ledger.link_round(**link_args(i))
            if result["classification"] != "linked":
                raise RuntimeError(f"headroom fixture link {i}: {result['classification']}")
        if i < finalized:
            result = ledger.finish(**finish_args(i))
            if result["classification"] != "finalized":
                if result["classification"] == "report_unavailable":
                    raise HeadroomUnverified(f"headroom fixture finish {i}: report_unavailable")
                raise RuntimeError(f"headroom fixture finish {i}: {result['classification']}")
        elif unavailable_rest and i >= max(linked, finalized):
            result = ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i),
                                               failed_wall=T, failed_mono=T,
                                               received_at=T, received_mono=T)
            if result["classification"] != "report_init_failed":
                raise RuntimeError(f"headroom fixture unavailable {i}: {result['classification']}")
    budget = _check_fixture_budget_e(ledger, fixed)
    ledger._gate_fixture_n = target
    ledger._gate_fixture_stop = "limit"
    ledger._gate_headroom_bytes = budget["B"] - budget["E"]
    if target < min_existing:
        raise HeadroomUnverified("slot limit below required existing identities")
    return ledger


def equivalence_check():
    left, right = fill(4, stop=2), fill(4, stop=2)
    right._records = CountedRecords(right._records)
    calls = [
        ("link_round", link_args(0)),
        ("finish", finish_args(0)),
        ("contributions_open", dict(as_of=T, as_of_mono=T, after_seq=0, limit=16)),
        ("aggregation_snapshot", dict(as_of=T, as_of_mono=T)),
        ("cohort_snapshot", dict(source=SOURCE, cohort_start=T, cohort_end=T + 1,
                                 as_of=T + 1, as_of_mono=T + 1)),
        ("report_init_failed", dict(epoch=EPOCH, invocation_id=rid(1), failed_wall=T,
                                     failed_mono=T, received_at=T + 1, received_mono=T + 1)),
    ]
    for method, kwargs in calls:
        a = getattr(left, method)(**kwargs)
        b = getattr(right, method)(**kwargs)
        if a != b or left.record(rid(0)) != right.record(rid(0)) or left._health != right._health:
            raise RuntimeError(f"counting wrapper changes semantics: {method}")
    return right._records.visits


def rss_bytes():
    # ru_maxrss is a peak and uses bytes on Darwin, KiB on Linux.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def current_rss_bytes():
    if sys.platform == "linux":
        try:
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        value = command_output(["ps", "-o", "rss=", "-p", str(os.getpid())])
        return int(value) * 1024 if value and value.isdecimal() else None
    return None


def owned_graph(root):
    seen, unknown, pending = set(), set(), [root]
    total = 0
    lock_type = type(threading.RLock())
    scalar = (str, int, float, bool, bytes, bytearray, type(None), lock_type)
    while pending:
        obj = pending.pop()
        marker = id(obj)
        if marker in seen:
            continue
        seen.add(marker)
        total += sys.getsizeof(obj)
        if type(obj) is dict:
            for key, value in obj.items():
                pending.extend((key, value))
        elif type(obj) in (list, tuple, set, frozenset):
            pending.extend(obj)
        elif type(obj).__module__ == RoundLedger.__module__:
            if hasattr(obj, "__dict__"):
                pending.append(vars(obj))
            for cls in type(obj).__mro__:
                slots = cls.__dict__.get("__slots__", ())
                if isinstance(slots, str):
                    slots = (slots,)
                for name in slots:
                    if name in ("__dict__", "__weakref__"):
                        continue
                    if name.startswith("__") and not name.endswith("__"):
                        name = f"_{cls.__name__.lstrip('_')}{name}"
                    if hasattr(obj, name):
                        pending.append(getattr(obj, name))
        elif not isinstance(obj, scalar):
            unknown.add(f"{type(obj).__module__}.{type(obj).__qualname__}")
    return {"bytes": total, "objects": len(seen), "unknown_types": sorted(unknown),
            "conservative_shared_strings": True}


def pct(samples, percent):
    ordered = sorted(samples)
    return round(ordered[max(0, (len(ordered) * percent + 99) // 100 - 1)] / 1000, 3)


def timing(call, args, warmup, samples, gc_call=None, clock=None):
    clock = clock or time.perf_counter_ns
    if tracemalloc.is_tracing():
        raise RuntimeError("timing with tracemalloc enabled")
    for arg in args[:warmup]:
        call(arg)
    result = {}
    for label, sequence, disable in (
        ("gc_disabled", args[warmup:warmup + samples], True),
        ("gc_enabled", args[warmup + samples:warmup + 2 * samples], False),
    ):
        prior = gc.isenabled()
        if disable:
            gc.disable()
        else:
            gc.enable()
        durations, classes = [], Counter()
        try:
            for arg in sequence:
                start = clock()
                outcome = (gc_call if label == "gc_enabled" and gc_call is not None else call)(arg)
                durations.append(clock() - start)
                classes[outcome["classification"]] += 1
        finally:
            gc.enable() if prior else gc.disable()
        result[label] = {"n": len(durations), "p50_us": pct(durations, 50),
                         "p95_us": pct(durations, 95), "p99_us": pct(durations, 99),
                         "max_us": round(max(durations) / 1000, 3),
                         "classifications": dict(classes)}
    return result


def peak_call(call, arg):
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    before, _ = tracemalloc.get_traced_memory()
    outcome = call(arg)
    after, peak = tracemalloc.get_traced_memory()
    rss = rss_bytes()
    data = {"current_before": before, "current_after": after, "peak": peak,
            "peak_delta": peak - before, "rss_peak_bytes": rss,
            "rss_current_bytes": current_rss_bytes(),
            "classification": outcome["classification"]}
    tracemalloc.stop()
    return data


def measure_case(name, ledger, method, arguments, *, d=0, k=0, expected=None,
                 quick=False, warmup=100, samples=1000, slots=None, details=None,
                 invoke=None, gc_invoke=None, clock=None):
    call = invoke if invoke is not None else lambda arg: getattr(ledger, method)(**arg)
    if len(arguments) < 2 + warmup + 2 * samples:
        raise RuntimeError(f"insufficient independent arguments: {name}")
    ledger._records = CountedRecords(ledger._records)
    first = call(arguments[0])
    visits = ledger._records.visits
    ledger._records = dict(ledger._records)
    temp = peak_call(call, arguments[1])
    dist = timing(call, arguments[2:], warmup, samples, gc_call=gc_invoke, clock=clock)
    classes = dist["gc_disabled"]["classifications"]
    bound = 64 + 3 * (d + k)
    visit_status = "PASS" if visits <= bound else "FAIL"
    temp_status = "PASS" if temp["peak_delta"] <= TEMP_LIMIT else "FAIL"
    if k >= CAP:
        p99_limit, max_limit = 2_000_000, 5_000_000
    elif d + k:
        p99_limit, max_limit = 250_000, 1_000_000
    else:
        p99_limit, max_limit = 20_000, 100_000
    clock_ok = (dist["gc_disabled"]["p99_us"] <= p99_limit
                and dist["gc_disabled"]["max_us"] <= max_limit)
    classification_ok = (expected is None or
                         (set(classes) == {expected} and
                          set(dist["gc_enabled"]["classifications"]) == {expected} and
                          first["classification"] == expected and temp["classification"] == expected))
    time_gate = "UNVERIFIED" if quick else "PASS" if clock_ok else "FAIL"
    status = scenario_status(visit_gate=visit_status, temp_gate=temp_status,
                             time_gate=time_gate, correct=classification_ok)
    return {"name": name, "slots": slots if slots is not None else len(ledger._records),
            "retained_details": details if details is not None else ledger._health["retained_details"],
            "D": d, "K": k, "visits": visits, "visit_limit": bound,
            "visit_gate": visit_status, "temporary_gate": temp_status,
            "time_gate": time_gate,
            "time_limits_us": {"p99": p99_limit, "max": max_limit},
            "timing": dist, "tracemalloc": temp, "first_classification": first["classification"],
            "expected_classification": expected, "classification_ok": classification_ok,
            "owned_graph": None, "status": status}


def clone_ledger(base):
    clone = object.__new__(type(base))
    clone.__dict__ = copy.deepcopy({k: v for k, v in base.__dict__.items() if k != "_lock"})
    clone._lock = threading.RLock()
    return clone


def _structural_state(ledger):
    """Comparable Record, A-G index, Health and cumulative values."""
    state = {key: value for key, value in ledger.__dict__.items()
             if key not in ("_lock", "_close_index", "_overdue_index", "_expiry_index", "_prune_index", "_cohort_index")
             and not key.startswith("_gate_")}
    # r3 §6 첫 퇴출 뒤: 새 deadline 색인의 slot/seq backing 도 분리 복제 동등성에 포함한다.
    for name in ("_close_index", "_overdue_index", "_expiry_index", "_prune_index"):
        index = getattr(ledger, name)
        state[name] = (index.size, index.data, index.seqs, index.slots, index.free_count)
    state["_cohort_index"] = {source: (index.blocks, index.maxes)
                              for source, index in ledger._cohort_index.items()}
    return state


def _state_digest(ledger):
    """Hash the API-built original before and after an independent call."""
    state = _structural_state(ledger)
    return hashlib.sha256(repr(sorted(state.items())).encode("utf-8")).hexdigest()


def _anchor_metric(value, depth=4):
    """Bounded, detached probes for a potentially large shared attribute."""
    if value is None or type(value) in (bool, int, float, str, bytes):
        return (type(value), value)
    if depth == 0:
        return (type(value), id(value), len(value) if hasattr(value, "__len__") else None)
    if isinstance(value, dict):
        keys = (list(value) if len(value) <= 8 else
                list(islice(value, 4)) + list(islice(reversed(value), 4)))
        return (type(value), id(value), len(value),
                tuple((_anchor_metric(key, depth - 1), _anchor_metric(value[key], depth - 1))
                      for key in keys))
    if isinstance(value, (list, tuple)):
        positions = (range(len(value)) if len(value) <= 8 else
                     (0, 1, 2, 3, len(value) - 4, len(value) - 3, len(value) - 2, len(value) - 1))
        return (type(value), id(value), len(value),
                tuple(_anchor_metric(value[index], depth - 1) for index in positions))
    if isinstance(value, (set, frozenset)):
        return (type(value), id(value), len(value),
                _anchor_metric(next(iter(value)), depth - 1) if value else None)
    if hasattr(value, "__dict__"):
        return (type(value), id(value), _anchor_metric(vars(value), depth - 1))
    return (type(value), id(value))


def _split_clone(base, mutable_ids):
    clone = object.__new__(type(base))
    clone.__dict__ = base.__dict__.copy()
    clone._records = base._records.copy()
    for invocation_id in mutable_ids:
        if invocation_id in clone._records:
            clone._records[invocation_id] = copy.deepcopy(clone._records[invocation_id])
    # r3 §6 첫 퇴출 뒤: close/expiry/prune 는 동일 호출에서 변하므로 관련 backing 을 분리한다.
    for name in ("_close_index", "_overdue_index", "_expiry_index", "_prune_index"):
        source = getattr(base, name)
        index = copy.copy(source)
        index.data = source.data.copy()
        index.seqs = source.seqs.copy()
        index.slots = source.slots.copy()
        index.free = source.free.copy()
        setattr(clone, name, index)
    clone._seq = copy.copy(base._seq)
    clone._seq.positions = base._seq.positions.copy()
    clone._tombs = base._tombs.copy()
    clone._frozen = copy.deepcopy(base._frozen)
    clone._cohort_index = copy.deepcopy(base._cohort_index)
    clone._owners = base._owners.copy()
    clone._owned_ids = base._owned_ids.copy()
    clone._open_seq = base._open_seq.copy()
    clone._recent_buckets = copy.deepcopy(base._recent_buckets)
    clone._health = copy.deepcopy(base._health)
    clone._post_close = base._post_close.copy()
    clone._lock = threading.RLock()
    return clone


def one_shot(name, make_fixture, method, kwargs, *, d, k, expected, quick,
             warmup=100, samples=1000):
    base = make_fixture()
    before = base._health["retained_details"]
    ledger = clone_ledger(base)
    ledger._records = CountedRecords(ledger._records)
    outcome = getattr(ledger, method)(**kwargs)
    visits = ledger._records.visits
    bound = 64 + 3 * (d + k)
    del ledger
    ledger = clone_ledger(base)
    temp = peak_call(lambda arg: getattr(ledger, method)(**arg), kwargs)
    del ledger
    distributions = None
    time_gate = "UNVERIFIED"
    if not quick:
        def run_samples(count, gc_disabled):
            durations, classes = [], Counter()
            for _ in range(count):
                ledger = clone_ledger(base)
                prior = gc.isenabled()
                if gc_disabled:
                    gc.disable()
                else:
                    gc.enable()
                try:
                    start = time.perf_counter_ns()
                    result = getattr(ledger, method)(**kwargs)
                    durations.append(time.perf_counter_ns() - start)
                    classes[result["classification"]] += 1
                finally:
                    gc.enable() if prior else gc.disable()
                del ledger
            return durations, classes

        run_samples(warmup, True)
        distributions = {}
        for label, disabled in (("gc_disabled", True), ("gc_enabled", False)):
            durations, classes = run_samples(samples, disabled)
            distributions[label] = {"n": samples, "p50_us": pct(durations, 50),
                                    "p95_us": pct(durations, 95), "p99_us": pct(durations, 99),
                                    "max_us": round(max(durations) / 1000, 3),
                                    "classifications": dict(classes)}
        if d + k <= 2048:
            p99_limit, max_limit = (20_000, 100_000) if d + k == 0 else (250_000, 1_000_000)
            time_gate = "PASS" if (distributions["gc_disabled"]["p99_us"] <= p99_limit and
                                   distributions["gc_disabled"]["max_us"] <= max_limit) else "FAIL"
    visit_gate = "PASS" if visits <= bound else "FAIL"
    temp_gate = "PASS" if temp["peak_delta"] <= TEMP_LIMIT else "FAIL"
    correct = (outcome["classification"] == expected and temp["classification"] == expected and
               (distributions is None or all(set(x["classifications"]) == {expected}
                                             for x in distributions.values())))
    status = scenario_status(visit_gate=visit_gate, temp_gate=temp_gate,
                             time_gate=time_gate, correct=correct)
    return {"name": name, "slots": len(base._records), "retained_details": before,
            "D": d, "K": k, "visits": visits, "visit_limit": bound,
            "visit_gate": visit_gate, "temporary_gate": temp_gate,
            "time_gate": time_gate, "timing": distributions, "tracemalloc": temp,
            "first_classification": outcome["classification"], "expected_classification": expected,
            "classification_ok": correct, "owned_graph": None,
            "status": status,
            "note": "timing uses independently cloned API-built fixtures; cloning is outside call boundary"}


def memory_state(name, limit, *, detail_count=0, release=False, reject=False, quick=False):
    gc.collect()
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    baseline = tracemalloc.take_snapshot()
    ledger = (fill_with_headroom(limit, accepts=1, detail_accepts=detail_count,
                                 finalized=detail_count, min_existing=detail_count)
              if detail_count else fill(limit))
    if release:
        result = ledger.aggregation_snapshot(as_of=T + 71 * MINUTE,
                                             as_of_mono=T + 71 * MINUTE)
        if result["classification"] != "snapshot":
            raise RuntimeError("release fixture did not snapshot")
        del result
    if reject:
        result = ledger.register(**register_args(limit))
        if result["classification"] != "admission_stopped":
            raise RuntimeError("capacity fixture did not reject")
        del result
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    snap = tracemalloc.take_snapshot()
    ledger_file = str(ROOT / "app/d7_round_ledger.py")
    ledger_trace_diff = sum(item.size_diff for item in snap.compare_to(baseline, "filename")
                            if item.traceback[0].filename == ledger_file)
    current_rss = current_rss_bytes()
    owned = owned_graph(ledger)
    outcome = "FAIL" if owned["bytes"] > OWNED_LIMIT else "UNVERIFIED" if owned["unknown_types"] or quick else "PASS"
    data = {"name": name, "slots": len(ledger._records),
            "fixture_n": ledger._gate_fixture_n,
            "fixture_stop": ledger._gate_fixture_stop,
            "retained_details": ledger._health["retained_details"], "D": 0, "K": 0,
            "visits": None, "timing": None, "owned_graph": owned,
            "tracemalloc": {"current_before": before, "current_after": current,
                            "current_delta": current - before, "peak": peak,
                            "ledger_file_traceback_delta": ledger_trace_diff},
            "rss_peak_bytes": rss_bytes(), "rss_current_bytes": current_rss,
            "status": outcome}
    del ledger
    tracemalloc.stop()
    return data


def command_output(args):
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def environment():
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        cpu = command_output(["sysctl", "-n", "machdep.cpu.brand_string"]) or cpu
        ram = command_output(["sysctl", "-n", "hw.memsize"])
        ram_bytes = int(ram) if ram and ram.isdecimal() else None
    else:
        try:
            cpu = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                       if line.startswith("model name"))
        except (OSError, StopIteration):
            pass
        try:
            ram_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError):
            ram_bytes = None
    container = "unknown"
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        container = "likely" if any(x in cgroup for x in ("docker", "kubepods", "containerd")) else "not_detected"
    except OSError:
        container = "not_detected"
    return {"cpu_model": cpu, "logical_cores": os.cpu_count(), "ram_bytes": ram_bytes,
            "os": platform.platform(), "container": container,
            "python": sys.version, "implementation": platform.python_implementation(),
            "allocator": "default_requested" if "PYTHONMALLOC" not in os.environ else "environment_override",
            "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
            "gc_thresholds": gc.get_threshold(), "gc_enabled": gc.isenabled(),
            "commit_sha": command_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "command": " ".join(sys.argv), "rss_note": "process peak; not ledger ownership",
            "host_representation": "운영 호스트 대표성 없음",
            "auxiliary_2cpu_2gib": {"status": "not_run", "reason": "offline harness does not launch a container"}}


def adapter_status(*, limit, demand, warmup, samples, quick, clock=None):
    with offline_app_import():
        return _adapter_status(limit=limit, demand=demand, warmup=warmup,
                               samples=samples, quick=quick, clock=clock)


def _adapter_status(*, limit, demand, warmup, samples, quick, clock=None):
    path = ROOT / "app/d7_report_adapter.py"
    if not path.exists():
        return {"status": "UNVERIFIED", "reason": "adapter module absent"}, []
    try:
        adapter = importlib.import_module("app.d7_report_adapter")
        from app.crawlers.bank_report import BankReport
        from app.crawlers.investing_report import InvestingReport

        class NullLogger:
            def info(self, _message):
                pass

        observations, rows = [], []
        for source in ("investing", "bs", "citi"):
            report = (InvestingReport(NullLogger(), PAIRS) if source == "investing" else
                      BankReport(NullLogger(), source, PAIRS, ("official_primary",)))
            link = adapter.report_link_args(report, expected_source=source,
                                            received_at=T, received_mono=T)
            report.finish()
            end = adapter.report_finish_args(report, expected_source=source,
                                             finished_wall=T, finished_mono=T,
                                             received_at=T, received_mono=T)
            ledger = new_ledger(1)
            registration = ledger.register(**register_args(0, source=source))
            if link.get("ok") and end.get("ok"):
                linked = ledger.link_round(epoch=EPOCH, invocation_id=rid(0),
                                           round_id=link["round_id"],
                                           report_schema=link["report_schema"],
                                           validity_contract=link["validity_contract"],
                                           received_at=T, received_mono=T)
                finished = ledger.finish(epoch=EPOCH, invocation_id=rid(0),
                                         round_id=end["round_id"],
                                         report_schema=end["report_schema"],
                                         validity_contract=end["validity_contract"],
                                         finished_wall=end["finished_wall"],
                                         finished_mono=end["finished_mono"],
                                         selected_summary=end["selected_summary"],
                                         telemetry_error_present=end["telemetry_error_present"],
                                         received_at=end["received_at"],
                                         received_mono=end["received_mono"])
                status = "PASS" if (registration["classification"] == "registered" and
                                    linked["classification"] == "linked" and
                                    finished["classification"] == "finalized") else "FAIL"
                observations.append({"source": source, "link": linked["classification"],
                                     "finish": finished["classification"], "status": status})
            else:
                observations.append({"source": source, "link_ok": link.get("ok"),
                                     "finish_ok": end.get("ok"), "status": "FAIL"})
            for stage, fn, kw in (
                ("link", adapter.report_link_args,
                 dict(expected_source=source, received_at=T, received_mono=T)),
                ("finish", adapter.report_finish_args,
                 dict(expected_source=source, finished_wall=T, finished_mono=T,
                      received_at=T, received_mono=T)),
            ):
                def projection(_unused, fn=fn, kw=kw, report=report):
                    value = fn(report, **kw)
                    return {"classification": "projected" if value.get("ok") else value.get("reason"),
                            "projection": value}

                temp = peak_call(projection, None)
                dist = timing(projection, [None] * (warmup + 2 * samples), warmup, samples, clock=clock)
                classes = dist["gc_disabled"]["classifications"]
                time_ok = dist["gc_disabled"]["p99_us"] <= 20_000 and dist["gc_disabled"]["max_us"] <= 100_000
                correct = (temp["classification"] == "projected"
                           and classes == {"projected": samples}
                           and dist["gc_enabled"]["classifications"] == {"projected": samples})
                temp_gate = "FAIL" if temp["peak_delta"] > TEMP_LIMIT else "PASS"
                time_gate = "UNVERIFIED" if quick else "PASS" if time_ok else "FAIL"
                rows.append({"name": f"adapter_{source}_{stage}", "slots": 0,
                             "retained_details": 0, "D": 0, "K": 0, "visits": None,
                             "visit_gate": "not_applicable", "temporary_gate": temp_gate,
                             "time_gate": time_gate,
                             "timing": dist, "tracemalloc": temp, "owned_graph": None,
                             "classification_ok": correct,
                             "status": scenario_status(visit_gate="PASS", temp_gate=temp_gate,
                                                       time_gate=time_gate, correct=correct)})

        reports = []
        ledger = fill_with_headroom(limit, accepts=1, detail_accepts=demand,
                                    min_existing=demand)
        if ledger._gate_fixture_n < demand or hasattr(ledger, "_gate_headroom_error"):
            raise HeadroomUnverified("adapter combined origin lacks finish headroom")
        for i in range(demand):
            report = BankReport(NullLogger(), "bs", PAIRS, ("official_primary",))
            report.finish()
            projected = adapter.report_link_args(report, expected_source="bs",
                                                 received_at=T, received_mono=T)
            if not projected.get("ok"):
                raise RuntimeError("adapter link projection failed")
            linked = ledger.link_round(epoch=EPOCH, invocation_id=rid(i),
                                       round_id=projected["round_id"],
                                       report_schema=projected["report_schema"],
                                       validity_contract=projected["validity_contract"],
                                       received_at=T, received_mono=T)
            if linked["classification"] != "linked":
                raise RuntimeError("adapter combined fixture link failed")
            reports.append(report)

        ledger_gc = clone_ledger(ledger)

        def combined_on(target, pair):
            i, report = pair
            projection = adapter.report_finish_args(report, expected_source="bs",
                                                    finished_wall=T, finished_mono=T,
                                                    received_at=T, received_mono=T)
            if not projection.get("ok"):
                return {"classification": projection.get("reason"), "projection": projection}
            outcome = target.finish(epoch=EPOCH, invocation_id=rid(i),
                                    round_id=projection["round_id"],
                                    report_schema=projection["report_schema"],
                                    validity_contract=projection["validity_contract"],
                                    finished_wall=projection["finished_wall"],
                                    finished_mono=projection["finished_mono"],
                                    selected_summary=projection["selected_summary"],
                                    telemetry_error_present=projection["telemetry_error_present"],
                                    received_at=projection["received_at"],
                                    received_mono=projection["received_mono"])
            return {"classification": outcome["classification"], "projection": projection,
                    "ledger_result": outcome}

        combined = measure_case("adapter_bank_to_finish", ledger, "combined",
                                list(enumerate(reports)), expected="finalized",
                                invoke=lambda pair: combined_on(ledger, pair),
                                gc_invoke=lambda pair: combined_on(ledger_gc, pair),
                                quick=quick, warmup=warmup, samples=samples, clock=clock)
        combined["fixture_n"] = ledger._gate_fixture_n
        combined["fixture_stop"] = ledger._gate_fixture_stop
        if ledger._gate_fixture_stop == "byte_headroom":
            combined["headroom_bytes"] = ledger._gate_headroom_bytes
        rows.append(combined)
        adapter_state = ("FAIL" if any(x["status"] == "FAIL" for x in observations + rows)
                         else "UNVERIFIED" if quick or any(x["status"] != "PASS"
                                                             for x in observations + rows) else "PASS")
        return {"status": adapter_state, "observations": observations}, rows
    except Exception as exc:
        return {"status": "UNVERIFIED", "reason": f"adapter path could not run: {type(exc).__name__}: {exc}"}, []


def summarize(rows):
    return dict(Counter(row["status"] for row in rows))



def time_verdict(*, d, k, gc_disabled, required_n, full_cohort):
    """Apply the elapsed-time contract only to a complete distribution."""
    if (not gc_disabled or gc_disabled.get("n", 0) < required_n
            or gc_disabled.get("p99_us") is None or gc_disabled.get("max_us") is None):
        return "UNVERIFIED"
    if full_cohort:
        p99, maximum = 2_000_000, 5_000_000
    elif d + k > DETAIL_CAP:
        return "N/A"
    elif d + k:
        p99, maximum = 250_000, 1_000_000
    else:
        p99, maximum = 20_000, 100_000
    return "PASS" if gc_disabled["p99_us"] <= p99 and gc_disabled["max_us"] <= maximum else "FAIL"


def _gated_rows(rows):
    return [row for row in rows if row.get("partial_acceptance_applicable", True)
            and not row["name"].startswith("resident_")
            and row["name"] != "record_baseline" and row.get("status") != "baseline"]


def overall_status(rows, *, adapter_status, mode):
    gated = [row for row in rows if row["name"] != "record_baseline"
             and row.get("status") != "baseline"
             and not (row.get("kind") == "rebuild" and row.get("status") == "N/A"
                      and row.get("reason") == "N/A (no rebuild in this implementation)")]
    if adapter_status == "FAIL" or any(row["status"] == "FAIL" for row in gated):
        return "FAIL"
    if (mode != "full" or not gated or adapter_status != "PASS"
            or any(row["status"] != "PASS" for row in gated)):
        return "UNVERIFIED"
    return "PASS"


def partial_acceptance(rows, *, adapter_status, mode):
    failing, unverified = [], []
    measured = _gated_rows(rows)
    for row in measured:
        gates = (row.get("visit_gate"), row.get("temporary_gate"), row.get("time_gate"))
        if row.get("status") == "FAIL" or "FAIL" in gates:
            failing.append(row["name"])
        elif (row.get("status") != "PASS" or gates[0] != "PASS" or gates[1] != "PASS"
              or gates[2] not in ("PASS", "N/A")):
            unverified.append(row["name"])
    return {"eligible": mode == "full" and bool(measured) and adapter_status == "PASS"
            and not failing and not unverified,
            "failing_rows": failing, "unverified_rows": unverified}


def _empty_row(name, status="UNVERIFIED"):
    return {"name": name, "status": status, "visit_gate": "UNVERIFIED",
            "temporary_gate": "UNVERIFIED", "time_gate": "UNVERIFIED", "sample_plan": "N/A",
            "temporary_breakdown": None,
            "samples": {"gc_disabled": 0, "gc_enabled": 0},
            "sample_checks": {"checked": 0, "failures": []}, "D_observed": [None, None],
            "K_observed": [None, None], "sample_exception": None, "prepare_seconds": 0.0,
            "timed_calls": 0}


def _distribution(durations, classes):
    if not durations:
        return None
    return {"n": len(durations), "p50_us": pct(durations, 50), "p95_us": pct(durations, 95),
            "p99_us": pct(durations, 99), "max_us": round(max(durations) / 1000, 3),
            "classifications": dict(classes), "raw_ns": durations}


def _candidate_state(ledger, kwargs):
    wall = kwargs.get("as_of", kwargs.get("received_at"))
    mono = kwargs.get("as_of_mono", kwargs.get("received_mono"))
    if wall is None or mono is None:
        return [], []
    end = ((wall - 70 * MINUTE) // MINUTE) * MINUTE
    closed = [(seq, ledger._records[ledger._seq[seq - 1]]["closed"])
              for seq in ledger._close_index.due(end)
              if ledger._seq[seq - 1] in ledger._records]
    overdue = [(seq, ledger._records[ledger._seq[seq - 1]]["lifecycle"])
               for seq in ledger._overdue_index.due(mono)
               if ledger._seq[seq - 1] in ledger._records]
    return closed, overdue


def _observed_d(ledger, before):
    close, overdue = before
    def current(seq):
        return ledger._records.get(ledger._seq.positions.get(seq - 1))
    # r3 §6 첫 퇴출 뒤: 닫힌 Record 가 tombstone/prune 되어도 그 호출의 D 전이는 센다.
    return (sum(not was_closed and (current(seq) is None or current(seq)["closed"])
                for seq, was_closed in close)
            + sum(old != "overdue" and current(seq) is not None
                  and current(seq)["lifecycle"] == "overdue" for seq, old in overdue))


def _observed_k(ledger, method, kwargs, outcome):
    if method == "contributions_open":
        return len(outcome.get("entries", ()))
    if method == "cohort_snapshot":
        return outcome.get("registered_invocations", 0)
    if method == "aggregation_snapshot":
        end = kwargs["as_of"] // MINUTE * MINUTE
        return sum(len(ledger._recent_buckets.get(bucket, ()))
                   for bucket in range(end - 60 * MINUTE, end, MINUTE))
    return 0


def _cohort_allowed_ids(ledger, kwargs):
    source = kwargs["source"]
    lower = (kwargs["cohort_start"], 0)
    upper = (kwargs["cohort_end"], 0)
    return {ledger._seq[seq - 1] for _, seq in ledger._cohort_index[source].range(lower, upper)}


def _snapshot_call(ledger, method, kwargs):
    try:
        return getattr(ledger, method)(**kwargs), None
    except Exception as exc:
        return None, exc


def _measure_row(name, provider, *, plan, expected, expected_d, expected_k,
                 samples, warmup, clock, wall, deadline, full_cohort=False,
                 sample_exception=None, validate=None, progress=None):
    row = _empty_row(name)
    row["sample_plan"] = plan
    row["sample_exception"] = sample_exception
    cohort_range_audit = name in ("reverse_cohort_narrow", "reverse_cohort_empty")
    row["out_of_range_visits"] = 0 if cohort_range_audit else None
    if cohort_range_audit:
        row["cohort_sources"] = []
        samples = max(samples, len(REGISTRY))
    row["timing"] = {}
    row["classification_ok"] = True
    row["sample_observations"] = []
    row["D"] = expected_d if isinstance(expected_d, int) else None
    row["K"] = expected_k if isinstance(expected_k, int) else None
    d_values, k_values, candidate_values, visits, peak_delta = [], [], [], None, None
    setup_seconds = 0.0
    clone_seconds = 0.0
    call_seconds = 0.0
    durations = {"gc_disabled": [], "gc_enabled": []}
    classes = {"gc_disabled": Counter(), "gc_enabled": Counter()}
    source_count = len(REGISTRY) if cohort_range_audit else 1
    phases = [("visit", source_count), ("temporary", source_count), ("warmup", warmup),
              ("gc_disabled", samples), ("gc_enabled", samples)]
    if hasattr(provider, "set_wall"):
        provider.set_wall(wall)
    index = 0
    preparation_failed = False
    visit_over_limit = False
    for phase, count in phases:
        for _ in range(count):
            if wall() >= deadline:
                row["aborted"] = "budget"
                break
            started = wall()
            try:
                ledger, method, kwargs, target = provider(index)
            except Exception as exc:
                if isinstance(exc, HeadroomUnverified):
                    row["reason"] = f"headroom: {exc}"
                row["sample_checks"]["failures"].append(
                    {"index": index, "reason": f"prepare:{type(exc).__name__}:{exc}",
                     "D": None, "K": None, "target": None})
                row["classification_ok"] = False
                preparation_failed = True
                break
            setup_seconds += wall() - started
            clone_seconds += getattr(provider, "last_clone_seconds", 0.0)
            index += 1
            if cohort_range_audit and kwargs["source"] not in row["cohort_sources"]:
                row["cohort_sources"].append(kwargs["source"])
            old = _candidate_state(ledger, kwargs)
            candidate_values.append(sum(len(part) for part in old))
            old_health = ledger._health["retained_details"]
            slots_before = len(ledger._records)
            if index == 1:
                row["slots"] = slots_before
                row["retained_details"] = old_health
                if hasattr(ledger, "_gate_fixture_n"):
                    row["fixture_n"] = ledger._health["N_total"]
                    row["fixture_stop"] = ledger._gate_fixture_stop
                    if row["fixture_stop"] == "byte_headroom":
                        row["headroom_bytes"] = ledger._gate_headroom_bytes
                        if hasattr(ledger, "_gate_headroom_error"):
                            row["reason"] = ledger._gate_headroom_error
                    if row["fixture_stop"] == "byte":
                        if expected_d == ledger._limits["max_records"]:
                            row["D"] = row["fixture_n"]
                        if expected_k == ledger._limits["max_records"]:
                            row["K"] = row["fixture_n"]
            old_counted = None
            if phase == "visit":
                old_counted = ledger._records
                allowed = _cohort_allowed_ids(ledger, kwargs) if cohort_range_audit else None
                ledger._records = CountedRecords(old_counted, allowed)
            if phase == "temporary":
                gc.collect()
                tracemalloc.start()
                tracemalloc.reset_peak()
                before_bytes, _ = tracemalloc.get_traced_memory()
            call_started = wall() if phase not in ("gc_disabled", "gc_enabled") else None
            if phase in ("gc_disabled", "gc_enabled"):
                prior_gc = gc.isenabled()
                gc.disable() if phase == "gc_disabled" else gc.enable()
                t0 = clock()
            try:
                outcome, error = _snapshot_call(ledger, method, kwargs)
            finally:
                if phase in ("gc_disabled", "gc_enabled"):
                    elapsed = clock() - t0
                    call_seconds += elapsed / 1_000_000_000
                    gc.enable() if prior_gc else gc.disable()
                    durations[phase].append(elapsed)
                    row["timed_calls"] += 1
                else:
                    call_seconds += wall() - call_started
                if phase == "temporary":
                    after_bytes, peak = tracemalloc.get_traced_memory()
                    peak_delta = peak - before_bytes
                    tracemalloc.stop()
                    row["temporary_breakdown"] = {
                        "peak_delta": peak_delta,
                        "current_after_delta": after_bytes - before_bytes,
                        "peak_over_current_after": peak - after_bytes,
                    }
                if phase == "visit":
                    visits = ledger._records.visits
                    if cohort_range_audit:
                        row["out_of_range_visits"] += ledger._records.out_of_range_visits
                    # Retirement removes Record entries. Keep those mutations when
                    # the counting wrapper is detached from a sequential fixture.
                    old_counted.clear()
                    old_counted.update(ledger._records)
                    ledger._records = old_counted
            observed_d = _observed_d(ledger, old)
            observed_k = _observed_k(ledger, method, kwargs, outcome or {})
            d_values.append(observed_d)
            k_values.append(observed_k)
            classification = type(error).__name__ if error else outcome.get("classification")
            if phase in classes:
                classes[phase][classification] += 1
            observation = {"index": index - 1, "phase": phase, "slots": slots_before,
                           "retained_details_before": old_health, "target": target,
                           "receipt_wall": kwargs.get("as_of", kwargs.get("received_at")),
                           "receipt_mono": kwargs.get("as_of_mono", kwargs.get("received_mono")),
                           "D_candidate": candidate_values[-1], "D": observed_d, "K": observed_k,
                           "classification": classification}
            row["sample_observations"].append(observation)
            if index == 1:
                row["first_call"] = observation.copy()
            reasons = []
            if expected is not None and classification != expected:
                reasons.append(f"classification:{classification} expected:{expected}")
            want_d = expected_d(index - 1) if callable(expected_d) else expected_d
            want_k = expected_k(index - 1) if callable(expected_k) else expected_k
            if row.get("fixture_stop") == "byte":
                if expected_d == ledger._limits["max_records"]:
                    want_d = row["fixture_n"]
                if expected_k == ledger._limits["max_records"]:
                    want_k = row["fixture_n"]
            if want_d is not None and observed_d != want_d:
                reasons.append(f"D:{observed_d} expected:{want_d}")
            if want_k is not None and observed_k != want_k:
                reasons.append(f"K:{observed_k} expected:{want_k}")
            if (method == "aggregation_snapshot" and error is None
                    and classification == "snapshot"):
                returned_k = sum(part["rounds"] for part in outcome["recent_rounds"])
                if observed_k != returned_k:
                    reasons.append(f"aggregate K:{observed_k} returned:{returned_k}")
            if phase == "visit" and cohort_range_audit and row["out_of_range_visits"]:
                reasons.append(f"out_of_range_visits:{row['out_of_range_visits']}")
            if validate is not None:
                try:
                    extra = validate(ledger, outcome, error, old_health)
                    if extra:
                        reasons.append(extra)
                except Exception as exc:
                    reasons.append(f"validate:{type(exc).__name__}:{exc}")
            if (getattr(provider, "use_split", False)
                    and not provider.original_unchanged()):
                reasons.append("split clone mutated API-built original")
                row["copy_mutation"] = True
            row["sample_checks"]["checked"] += 1
            if reasons:
                row["sample_checks"]["failures"].append(
                    {"index": index - 1, "reason": ";".join(reasons), "D": observed_d,
                     "K": observed_k, "target": target})
            if phase == "visit":
                charged_d = candidate_values[-1] if expected == "CumulativeMergeFailureForTest" else observed_d
                visit_limit = 64 + 3 * (charged_d + observed_k)
                visit_over_limit |= visits > visit_limit
                row["visits"] = max(row.get("visits", 0), visits)
                row["visit_limit"] = max(row.get("visit_limit", 0), visit_limit)
            if phase == "temporary":
                row["tracemalloc"] = {"peak_delta": peak_delta}
            if progress and index % 100 == 0:
                remaining = sum(n for _, n in phases) - index
                eta = (setup_seconds + call_seconds) / index * remaining
                print(f"progress {name} {index}/{sum(n for _, n in phases)} gc={phase} "
                      f"prepare={setup_seconds-clone_seconds:.2f}s clone={clone_seconds:.2f}s "
                      f"call={call_seconds:.2f}s eta={eta:.1f}s", file=progress)
        if row.get("aborted") or preparation_failed:
            break
    row["prepare_seconds"] = round(setup_seconds-clone_seconds, 6)
    row["clone_seconds"] = round(clone_seconds, 6)
    row["call_seconds"] = round(call_seconds, 6)
    row["D_observed"] = [min(d_values), max(d_values)] if d_values else [None, None]
    row["K_observed"] = [min(k_values), max(k_values)] if k_values else [None, None]
    row["D_candidate"] = [min(candidate_values), max(candidate_values)] if candidate_values else [None, None]
    row["D_committed"] = row["D_observed"]
    row["samples"] = {key: len(value) for key, value in durations.items()}
    row["timing"] = {key: _distribution(durations[key], classes[key]) for key in durations}
    required = samples
    if sample_exception == "mass_overdue_report_only_100_per_gc" and samples >= 100:
        required = 100
        row["original_required_n"] = 1000
        row["sample_exception_reason"] = "D+K exceeds 2048; elapsed time is report-only"
    d_for_time = (max(candidate_values) if candidate_values else 0) if expected == "CumulativeMergeFailureForTest" else (max(d_values) if d_values else 0)
    k_for_time = max(k_values) if k_values else 0
    row["time_gate"] = time_verdict(d=d_for_time, k=k_for_time,
                                    gc_disabled=row["timing"]["gc_disabled"],
                                    required_n=required,
                                    full_cohort=full_cohort and row.get("fixture_stop") != "byte")
    if len(durations["gc_enabled"]) < required:
        row["time_gate"] = "UNVERIFIED"
    row["visit_gate"] = "UNVERIFIED" if visits is None else "FAIL" if visit_over_limit else "PASS"
    row["temporary_gate"] = "UNVERIFIED" if peak_delta is None else "PASS" if peak_delta <= TEMP_LIMIT else "FAIL"
    row["classification_ok"] = not row["sample_checks"]["failures"]
    if hasattr(provider, "proof"):
        row["copy_equivalence"] = provider.proof
        row["sample_plan"] = "independent_split_copy" if provider.use_split else "full_deepcopy"
        if row.get("copy_mutation"):
            row["copy_equivalence"] = {**provider.proof, "original_unchanged": False}
    row["status"] = scenario_status(visit_gate=row["visit_gate"], temp_gate=row["temporary_gate"],
                                    time_gate=row["time_gate"], correct=row["classification_ok"])
    if row.get("reason", "").startswith("headroom:") or row.get("fixture_stop") == "byte_headroom" and (
            row.get("reason") or any(
                item["classification"] in ("admission_stopped", "report_unavailable")
                for item in row["sample_observations"])):
        row["status"] = "UNVERIFIED"
        row["reason"] = row.get("reason") or "planned headroom did not admit an acceptance"
    if row.get("copy_mutation") and not any(
            failure["reason"] != "split clone mutated API-built original"
            for failure in row["sample_checks"]["failures"]) and all(
            row[gate] != "FAIL" for gate in ("visit_gate", "temporary_gate", "time_gate")):
        row["status"] = "UNVERIFIED"
    if row.get("aborted") and row["status"] != "FAIL":
        row["status"] = "UNVERIFIED"
    return row


def _repeat_provider(factory, method, arguments, *, independent=False, prepare=None, split=False):
    base = None
    timer = time.monotonic
    def provide(index):
        nonlocal base
        if base is None:
            base = factory()
        kwargs = arguments(index) if callable(arguments) else arguments
        clone_started = timer()
        if split and independent:
            close, overdue = _candidate_state(base, kwargs)
            mutable = {base._seq[seq - 1] for seq, _ in close + overdue}
            if "invocation_id" in kwargs:
                mutable.add(kwargs["invocation_id"])
            if not hasattr(provide, "proof"):
                original = _state_digest(base)
                full = clone_ledger(base)
                separated = _split_clone(base, mutable)
                # Derive the shared fields from this clone's actual identities.
                # A future ledger attribute is covered without updating a name list.
                provide.shared_names = tuple(name for name, value in base.__dict__.items()
                                             if name != "_lock" and separated.__dict__.get(name) is value)
                distinct = all(id(separated._records[key]) != id(base._records[key])
                               for key in mutable if key in base._records)
                for candidate in (full, separated):
                    if prepare is not None:
                        prepare(candidate, index)
                left, left_error = _snapshot_call(full, method, kwargs)
                right, right_error = _snapshot_call(separated, method, kwargs)
                equal = (left == right and type(left_error) is type(right_error)
                         and _structural_state(full) == _structural_state(separated) and distinct)
                unchanged = _state_digest(base) == original
                provide.proof = {"checked": True, "equal": equal,
                                 "original_unchanged": unchanged}
                provide.use_split = equal and unchanged
                provide.original_digest = original
            provide.anchor = {
                "attributes": frozenset(base.__dict__),
                "shared": {name: _anchor_metric(base.__dict__[name])
                           for name in provide.shared_names},
                "health": copy.deepcopy(base._health),
                "cumulative_end": base._cumulative_end,
                "close_root": base._close_index.data[1],
                "overdue_root": base._overdue_index.data[1],
                "open_seq": _anchor_metric(base._open_seq),
                "records_size": len(base._records),
                "owned_ids": _anchor_metric(base._owned_ids),
                "previous_job": _anchor_metric(base._previous_job),
                "seq": _anchor_metric(base._seq),
                "records": {key: copy.deepcopy(base._records[key])
                            for key in mutable if key in base._records},
            }
            ledger = _split_clone(base, mutable) if provide.use_split else clone_ledger(base)
        else:
            ledger = clone_ledger(base) if independent else base
        provide.last_clone_seconds = timer() - clone_started if independent else 0.0
        if prepare is not None:
            prepare(ledger, index)
        return ledger, method, kwargs, kwargs.get("invocation_id", kwargs.get("after_seq", index))
    if split:
        def original_unchanged():
            if len(base._records) <= 1024:
                return _state_digest(base) == provide.original_digest
            anchor = provide.anchor
            return (frozenset(base.__dict__) == anchor["attributes"]
                    and all(_anchor_metric(base.__dict__[name]) == metric
                            for name, metric in anchor["shared"].items())
                    and base._health == anchor["health"]
                    and base._cumulative_end == anchor["cumulative_end"]
                    and base._close_index.data[1] == anchor["close_root"]
                    and base._overdue_index.data[1] == anchor["overdue_root"]
                    and _anchor_metric(base._open_seq) == anchor["open_seq"]
                    and len(base._records) == anchor["records_size"]
                    and _anchor_metric(base._owned_ids) == anchor["owned_ids"]
                    and _anchor_metric(base._previous_job) == anchor["previous_job"]
                    and _anchor_metric(base._seq) == anchor["seq"]
                    and all(base._records.get(key) == value
                            for key, value in anchor["records"].items()))
        provide.original_unchanged = original_unchanged
    def set_wall(value):
        nonlocal timer
        timer = value
    provide.set_wall = set_wall
    return provide


def _at(wall):
    return {"as_of": wall, "as_of_mono": wall}


def _detail_origin_target(limit, requested):
    """Keep a small injected byte budget from hiding an origin's actual stop."""
    record, detail = _headroom_tariffs(need_detail=True)
    available = _gate_resident_budget.get() - new_ledger(1).budget_state()["F_4"]
    # Q growth and the last registration need room in addition to the detail.
    feasible = max(1, available // (record + detail + 2048))
    return min(requested, limit, feasible)


def _close_base(limit, n=1):
    actual_target = _detail_origin_target(limit, n)
    ledger = fill(limit, finalized=actual_target, unavailable_rest=True)
    ledger._gate_detail_target_requested = n
    ledger._gate_detail_target_actual = min(actual_target, ledger._gate_fixture_n)
    return ledger


def _multi_bucket_base(limit):
    requested = min(DETAIL_CAP, limit)
    n = _detail_origin_target(limit, requested)
    ledger = fill_with_headroom(limit, accepts=1, detail_accepts=n, linked=n,
                                unavailable_rest=True, min_existing=n)
    if ledger._gate_fixture_n < n:
        raise HeadroomUnverified("multi-bucket detail identities unavailable")
    ledger._gate_detail_target_requested = requested
    ledger._gate_detail_target_actual = n
    for i in range(n):
        bucket = T + (i * 3 // n) * MINUTE
        kwargs = finish_args(i, wall=bucket)
        kwargs["finished_wall"] = bucket
        kwargs["finished_mono"] = bucket
        result = ledger.finish(**kwargs)
        if result["classification"] != "finalized":
            raise RuntimeError(f"multi bucket fixture finish {i}: {result['classification']}")
    return ledger


def _exact_provider(limit):
    ledger = None
    count = min(limit, 70)
    def provide(index):
        nonlocal ledger
        offset = index % count
        if offset == 0:
            ledger = fill_with_headroom(limit, accepts=1, detail_accepts=count,
                                        linked=count, unavailable_rest=True,
                                        min_existing=count)
            if ledger._gate_fixture_n < count:
                raise HeadroomUnverified("exact boundary identities unavailable")
            for i in range(count):
                instant = T + i * MINUTE
                kwargs = finish_args(i, wall=instant)
                kwargs["finished_wall"] = instant
                kwargs["finished_mono"] = instant
                result = ledger.finish(**kwargs)
                if result["classification"] != "finalized":
                    raise RuntimeError(f"exact fixture finish {i}: {result['classification']}")
        instant = T + (71 + offset) * MINUTE
        return ledger, "aggregation_snapshot", _at(instant), rid(offset)
    return provide


def _reverse_base(limit, stop=None):
    ledger = new_ledger(limit)
    target = limit if stop is None else stop
    for i in range(target):
        kwargs = register_args(i, source=_reverse_source(i))
        kwargs["started_wall"] = T - i
        result = ledger.register(**kwargs)
        if result["classification"] == "admission_stopped" and ledger._health["N_res"] < limit:
            ledger._gate_fixture_n = i
            ledger._gate_fixture_stop = "byte"
            return ledger
        if result["classification"] != "registered":
            raise RuntimeError(f"reverse registration {i}: {result['classification']}")
    ledger._gate_fixture_n = target
    ledger._gate_fixture_stop = "limit"
    return ledger


def _reverse_source(i):
    sources = tuple(REGISTRY)
    return sources[i % len(sources)]


def _stale_base(limit):
    third = min(limit // 3, _detail_origin_target(limit, min(DETAIL_CAP, max(1, limit // 3))))
    ledger = new_ledger(limit)
    for i in range(limit):
        kwargs = register_args(i)
        if i >= 2 * third:
            kwargs.update(job_id="serial", serial_job=True)
        result = ledger.register(**kwargs)
        if result["classification"] == "admission_stopped" and ledger._health["N_res"] < limit:
            ledger._gate_fixture_n = i
            ledger._gate_fixture_stop = "byte"
            break
        if result["classification"] != "registered":
            raise RuntimeError(f"stale fixture register {i}: {result['classification']}")
        if i < third:
            if ledger.link_round(**link_args(i))["classification"] != "linked":
                raise RuntimeError("stale fixture link")
            if ledger.finish(**finish_args(i))["classification"] != "finalized":
                raise HeadroomUnverified("stale fixture detail unavailable")
        elif i < 2 * third:
            ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i), failed_wall=T,
                                      failed_mono=T, received_at=T, received_mono=T)
    else:
        ledger._gate_fixture_n = limit
        ledger._gate_fixture_stop = "limit"
    # The final serial successor has no next entry, so remove its B deadline too.
    if ledger._gate_fixture_n > 2 * third:
        last = ledger._gate_fixture_n - 1
        ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(last), failed_wall=T,
                                  failed_mono=T, received_at=T, received_mono=T)
    return ledger


def _recent_base(limit, one_inside=False):
    requested = min(DETAIL_CAP, limit)
    n = _detail_origin_target(limit, requested)
    ledger = fill_with_headroom(limit, accepts=1, detail_accepts=n,
                                linked=n, unavailable_rest=True, min_existing=n)
    if ledger._gate_fixture_n < n:
        raise HeadroomUnverified("recent detail identities unavailable")
    ledger._gate_detail_target_requested = requested
    ledger._gate_detail_target_actual = n
    for i in range(n):
        instant = T + 60 * MINUTE if one_inside and i == n - 1 else T
        kwargs = finish_args(i, wall=instant)
        kwargs["finished_wall"] = instant
        kwargs["finished_mono"] = instant
        outcome = ledger.finish(**kwargs)
        if outcome["classification"] != "finalized":
            raise RuntimeError(f"recent fixture finish {i}: {outcome['classification']}")
    return ledger


def _recent_seq_order_base(limit):
    """Finish two records in bucket order opposite to registration order."""
    ledger = fill_with_headroom(limit, accepts=1, detail_accepts=2,
                                linked=2, unavailable_rest=True, min_existing=2)
    if ledger._gate_fixture_n < 2:
        raise HeadroomUnverified("recent order identities unavailable")
    earlier = copy.deepcopy(SUMMARY)
    earlier["collection"][PAIRS[0]]["reason"] = "parse_failed"
    for i, instant, summary in ((1, T, earlier), (0, T + MINUTE, SUMMARY)):
        kwargs = finish_args(i, wall=instant, summary=summary)
        kwargs["finished_wall"] = instant
        kwargs["finished_mono"] = instant
        if ledger.finish(**kwargs)["classification"] != "finalized":
            raise RuntimeError(f"recent order fixture finish {i}")
    return ledger


def _wait_base(limit):
    detail_target = _detail_origin_target(limit, min(DETAIL_CAP, limit))
    ledger = fill_with_headroom(limit, accepts=1, detail_accepts=detail_target,
                                linked=limit, min_existing=detail_target)
    for i in range(ledger._gate_fixture_n):
        outcome = ledger.finish(**finish_args(i))
        if outcome["classification"] not in ("finalized", "report_unavailable"):
            raise RuntimeError(f"wait fixture finish {i}: {outcome['classification']}")
    ledger._gate_detail_target_requested = min(DETAIL_CAP, limit)
    ledger._gate_detail_target_actual = ledger._health["retained_details"]
    return ledger


def _failure_provider(factory, method, kwargs, prepare):
    return _repeat_provider(factory, method, kwargs, independent=True, prepare=prepare, split=True)


def _tail_register_provider(limit, samples, warmup, *, reverse=False):
    """Give both GC distributions the same final acceptance range."""
    ledger = None
    probe_count = 2 + warmup
    def provide(index):
        nonlocal ledger
        if index < probe_count:
            offset = index
            if index == 0:
                ledger = fill_with_headroom(limit, accepts=probe_count,
                                            reserve_slots=probe_count, reverse=reverse)
            target = ledger._gate_fixture_n + offset
        else:
            offset = (index-probe_count) % samples
            if offset == 0:
                ledger = fill_with_headroom(limit, accepts=samples,
                                            reserve_slots=samples, reverse=reverse)
            target = ledger._gate_fixture_n + offset
        kwargs = register_args(target, source=_reverse_source(target) if reverse else SOURCE)
        if reverse:
            kwargs["started_wall"] = T-target
        return ledger, "register", kwargs, rid(target)
    return provide


def _finish_reprepared_provider(limit, total, summary=SUMMARY):
    """Keep finish calls sequential while resetting before the detail cap is reached."""
    detail_cap = min(DETAIL_CAP, limit)
    ledger = None
    def provide(index):
        nonlocal ledger
        target = index % detail_cap
        if ledger is None or target == 0:
            group_count = min(total, detail_cap)
            ledger = fill_with_headroom(limit, accepts=group_count,
                                        detail_accepts=group_count, linked=group_count,
                                        summary=summary, min_existing=group_count)
        return ledger, "finish", finish_args(target, summary=summary), rid(target)
    return provide


def _scenario_specs(limit, samples, warmup):
    total = 2 + warmup + 2 * samples
    close_n = min(DETAIL_CAP, limit)
    specs = []
    def add(name, provider, plan, classification="snapshot", d=0, k=0, validate=None,
            exception=None, full_cohort=False):
        specs.append((name, provider, plan, classification, d, k, validate, exception, full_cohort))

    add("link_round_accept", _repeat_provider(lambda: fill_with_headroom(limit, accepts=total,
        min_existing=total), "link_round",
        lambda i: link_args(i)), "sequential", "linked")
    add("report_init_failed_accept", _repeat_provider(lambda: fill_with_headroom(limit, accepts=total,
        min_existing=total), "report_init_failed",
        lambda i: dict(epoch=EPOCH, invocation_id=rid(i), failed_wall=T, failed_mono=T,
                       received_at=T, received_mono=T)), "sequential", "report_init_failed")
    add("wrapper_exited_accept", _repeat_provider(lambda: fill_with_headroom(limit, accepts=total,
        min_existing=total), "wrapper_exited",
        lambda i: dict(epoch=EPOCH, invocation_id=rid(i), exited_wall=T, exited_mono=T,
                       received_at=T, received_mono=T)), "sequential", "wrapper_exited")
    add("contributions_tail_empty", _repeat_provider(lambda: fill(limit), "contributions_open",
        dict(as_of=T, as_of_mono=T, after_seq=limit, limit=16)), "repeat")
    add("aggregation_empty", _repeat_provider(lambda: fill(limit), "aggregation_snapshot", _at(T)), "repeat")
    add("cohort_empty", _repeat_provider(lambda: fill(limit), "cohort_snapshot",
        dict(source=SOURCE, cohort_start=T + 1, cohort_end=T + 2, as_of=T + 2,
             as_of_mono=T + 2)), "repeat")
    add("finish_accept", _finish_reprepared_provider(limit, total),
        "sequential_reprepared", "finalized")
    add("link_round_duplicate", _repeat_provider(lambda: fill(limit, linked=1), "link_round",
        link_args(0)), "repeat", "relinked_same")
    near, over = large_summary(3900), large_summary(4097)
    add("finish_near_valid_limit", _finish_reprepared_provider(limit, total, near),
        "sequential_reprepared", "finalized")
    add("finish_over_input_limit", _repeat_provider(lambda: fill(limit, linked=total), "finish",
        lambda i: finish_args(i, summary=over)), "sequential", "input_limit_exceeded")
    add("finish_duplicate", _repeat_provider(lambda: fill(limit, finalized=1), "finish",
        finish_args(0)), "repeat", "duplicate_finish")
    add("contributions_first_page", _repeat_provider(lambda: fill(limit, finalized=min(16, limit)),
        "contributions_open", dict(as_of=T, as_of_mono=T, after_seq=0, limit=16)),
        "repeat", k=min(16, limit))
    add("contributions_after_cursor", _repeat_provider(lambda: fill(limit, finalized=min(16, limit)),
        "contributions_open", dict(as_of=T, as_of_mono=T, after_seq=min(15, limit - 1), limit=16)),
        "repeat", k=1)
    add("aggregation_recent", _repeat_provider(lambda: fill(limit, finalized=min(16, limit)),
        "aggregation_snapshot", _at(T + MINUTE)), "repeat", k=min(16, limit))
    add("cohort_all", _repeat_provider(lambda: fill(limit), "cohort_snapshot",
        dict(source=SOURCE, cohort_start=T, cohort_end=T + 1, as_of=T + 1, as_of_mono=T + 1)),
        "repeat", k=limit, full_cohort=limit == CAP)
    add("register_accept", _tail_register_provider(limit, samples, warmup),
        "sequential_reprepared", "registered")
    add("register_capacity_reject", _repeat_provider(lambda: fill(limit), "register",
        lambda i: register_args(limit+i)), "repeat", "admission_stopped")

    before_ledger = None
    def before_provider(index):
        nonlocal before_ledger
        if before_ledger is None:
            before_ledger = _close_base(limit)
        instant = T + 71 * MINUTE - 1
        return before_ledger, "aggregation_snapshot", _at(instant), rid(0)
    add("close_boundary_before", before_provider, "repeat")
    add("close_boundary_exact", _exact_provider(limit), "sequential_reprepared", d=1, k=None)
    add("large_clock_jump", _repeat_provider(lambda: _close_base(limit), "aggregation_snapshot",
        _at(T + 180 * MINUTE), independent=True, split=True), "independent_split_copy", d=1)
    def closed_base():
        ledger = _close_base(limit)
        ledger.aggregation_snapshot(**_at(T + 71 * MINUTE))
        return ledger
    add("close_same_time_requery", _repeat_provider(closed_base, "aggregation_snapshot",
        _at(T + 71 * MINUTE)), "repeat")
    mass_close_actual = {"n": close_n}
    def mass_close_base():
        ledger = _close_base(limit, close_n)
        mass_close_actual["n"] = ledger._gate_detail_target_actual
        return ledger
    add("mass_close_boundary", _repeat_provider(mass_close_base,
        "aggregation_snapshot", _at(T + 71 * MINUTE), independent=True, split=True),
        "independent_split_copy", d=lambda _index: mass_close_actual["n"],
        validate=lambda ledger, outcome, error, before: (
            "detail not released or cumulative count incorrect"
            if error or ledger._health["retained_details"] != 0
            or ledger._cumulative_rounds[SOURCE]["rounds"] != mass_close_actual["n"] else None))
    add("mass_overdue", _repeat_provider(lambda: fill(limit), "aggregation_snapshot",
        _at(T + 15 * MINUTE), independent=True), "full_deepcopy", d=limit,
        exception="mass_overdue_report_only_100_per_gc")

    # B4(a): all overdue entries are stale, including the final serial successor.
    add("stale_overdue_end_cursor", _repeat_provider(lambda: _stale_base(limit),
        "contributions_open", dict(as_of=T + 15 * MINUTE, as_of_mono=T + 15 * MINUTE,
                                   after_seq=limit, limit=16), independent=True, split=True), "independent_split_copy",
        validate=lambda ledger, outcome, error, before: (
            "stale B or nonempty tail"
            if error or ledger._overdue_index.data[1] is not None
            or outcome["entries"] or outcome["next_seq"] is not None
            or ledger.contributions_open(as_of=T+15*MINUTE, as_of_mono=T+15*MINUTE,
                                         after_seq=limit, limit=16)["entries"] else None))
    def stale_advanced():
        ledger = _stale_base(limit)
        first = ledger.contributions_open(as_of=T+15*MINUTE, as_of_mono=T+15*MINUTE,
                                          after_seq=limit, limit=16)
        if first["classification"] != "snapshot" or first["entries"]:
            raise RuntimeError("stale fixture first query failed")
        return ledger
    add("stale_overdue_requery", _repeat_provider(stale_advanced, "contributions_open",
        dict(as_of=T+15*MINUTE, as_of_mono=T+15*MINUTE, after_seq=limit, limit=16)), "repeat",
        validate=lambda ledger, outcome, error, before: (
            "stale same-time requery changed tail or B"
            if error or outcome["entries"] or outcome["next_seq"] is not None
            or ledger._overdue_index.data[1] is not None else None))
    multi_actual = {"n": close_n}
    def multi():
        ledger = _multi_bucket_base(limit)
        multi_actual["n"] = ledger._gate_detail_target_actual
        return ledger
    add("multi_bucket_close", _repeat_provider(multi, "aggregation_snapshot",
        _at(T + 73 * MINUTE), independent=True, split=True), "independent_split_copy",
        d=lambda _index: multi_actual["n"],
        validate=lambda ledger, outcome, error, before: (
            "multi-bucket close did not release/count once"
            if error or ledger._health["retained_details"] != 0
            or ledger._cumulative_rounds[SOURCE]["rounds"] != multi_actual["n"] else None))
    def multi_closed():
        ledger = multi()
        ledger.aggregation_snapshot(**_at(T+73*MINUTE))
        return ledger
    def mark_cumulative(ledger, _index):
        ledger._gate_cumulative_before = {
            key: copy.deepcopy(value) for key, value in ledger.__dict__.items()
            if key.startswith("_cumulative_")}
    def check_cumulative(ledger, outcome, error, before):
        current = {key: value for key, value in ledger.__dict__.items()
                   if key.startswith("_cumulative_")}
        return ("cumulative changed on same-time requery"
                if error or current != ledger._gate_cumulative_before else None)
    add("multi_bucket_close_requery", _repeat_provider(multi_closed,
        "aggregation_snapshot", _at(T+73*MINUTE), prepare=mark_cumulative),
        "repeat", validate=check_cumulative)
    def arm_failure(ledger, _index):
        ledger._inject_cumulative_merge_failure_for_test(bucket_end=T + MINUTE)
        ledger._gate_atomic_before = {
            "close": tuple(ledger._close_index.due(T + 3*MINUTE)),
            "open": tuple(ledger._open_seq),
            "recent": copy.deepcopy(ledger._recent_buckets),
            "health": copy.deepcopy(ledger._health),
            "cumulative_end": ledger._cumulative_end,
            "cumulative_rounds": copy.deepcopy(ledger._cumulative_rounds),
            "cumulative_rows": copy.deepcopy(ledger._cumulative_rows),
            "candidate_records": {rid(i): copy.deepcopy(ledger._records[rid(i)])
                                  for i in range(ledger._gate_detail_target_actual)},
        }
    def check_atomic(ledger, outcome, error, before):
        saved = ledger._gate_atomic_before
        now = {"close": tuple(ledger._close_index.due(T + 3*MINUTE)),
               "open": tuple(ledger._open_seq), "recent": ledger._recent_buckets,
               "health": ledger._health, "cumulative_end": ledger._cumulative_end,
               "cumulative_rounds": ledger._cumulative_rounds, "cumulative_rows": ledger._cumulative_rows,
               "candidate_records": {rid(i): ledger._records[rid(i)]
                                     for i in range(ledger._gate_detail_target_actual)}}
        return None if type(error).__name__ == "CumulativeMergeFailureForTest" and saved == now else "merge failure changed published state"
    add("multi_bucket_close_merge_failure", _failure_provider(multi, "aggregation_snapshot",
        _at(T + 73 * MINUTE), arm_failure), "full_deepcopy",
        "CumulativeMergeFailureForTest", d=0, validate=check_atomic)
    def consume_failure(ledger, _index):
        arm_failure(ledger, _index)
        try:
            ledger.aggregation_snapshot(**_at(T + 73 * MINUTE))
        except Exception as exc:
            if type(exc).__name__ != "CumulativeMergeFailureForTest":
                raise
        else:
            raise RuntimeError("merge failure injection did not fail")
    add("multi_bucket_close_retry", _failure_provider(multi, "aggregation_snapshot",
        _at(T + 73 * MINUTE), consume_failure), "full_deepcopy",
        d=lambda _index: multi_actual["n"],
        validate=lambda ledger, outcome, error, before: (
            "retry did not close once"
            if error or ledger._health["retained_details"] != 0
            or ledger._cumulative_rounds[SOURCE]["rounds"] != multi_actual["n"] else None))
    def multi_retried():
        ledger = multi()
        consume_failure(ledger, 0)
        ledger.aggregation_snapshot(**_at(T+73*MINUTE))
        return ledger
    add("multi_bucket_close_retry_requery", _repeat_provider(multi_retried,
        "aggregation_snapshot", _at(T+73*MINUTE), prepare=mark_cumulative),
        "repeat", validate=check_cumulative)
    add("reverse_cohort_register_tail", _tail_register_provider(limit, samples, warmup, reverse=True),
        "sequential_reprepared", "registered")
    cohort_sources = tuple(REGISTRY)
    narrow_indices = {}
    def reverse_base():
        ledger = _reverse_base(limit)
        actual = ledger._gate_fixture_n
        narrow_indices.update({source: max((i for i in range(actual)
                                            if _reverse_source(i) == source), default=None)
                               for source in cohort_sources})
        return ledger
    def empty_cohort_args(index):
        return dict(source=cohort_sources[index % len(cohort_sources)],
                    cohort_start=T+1, cohort_end=T+2, as_of=T+2, as_of_mono=T+2)
    def narrow_cohort_args(index):
        source = cohort_sources[index % len(cohort_sources)]
        last = narrow_indices[source]
        start = T-last if last is not None else T+1
        return dict(source=source, cohort_start=start, cohort_end=start+1,
                    as_of=T+1, as_of_mono=T+1)
    def check_narrow_cohort(ledger, outcome, error, before):
        if error or outcome.get("source") not in narrow_indices:
            return "narrow cohort count"
        expected_count = int(narrow_indices[outcome["source"]] is not None)
        return ("narrow cohort count" if outcome["registered_invocations"] != expected_count
                else None)
    add("reverse_cohort_empty", _repeat_provider(reverse_base, "cohort_snapshot",
        empty_cohort_args), "repeat")
    add("reverse_cohort_narrow", _repeat_provider(reverse_base, "cohort_snapshot",
        narrow_cohort_args), "repeat", k=lambda index: 1 if narrow_indices[
            cohort_sources[index % len(cohort_sources)]] is not None else 0,
        validate=check_narrow_cohort)
    add("identity_absent_link", _repeat_provider(lambda: fill(limit, linked=limit), "link_round",
        link_args(limit)), "repeat", "orphan")
    add("identity_absent_finish", _repeat_provider(lambda: fill(limit, linked=limit), "finish",
        finish_args(limit)), "repeat", "orphan")
    def missing(ledger, _index):
        ledger._inject_identity_fault_for_test(invocation_id=rid(0), fault="missing_record")
    expected_global_diag = {"codes": ["identity_unverified"], "baseline_invalidated": True,
                            "coverage_error": True, "uncertain_pairs": [],
                            "cumulative_evidence_uncertain": False}
    def check_first_query_diagnostics(ledger, outcome, error, before):
        if error or not ledger._health["index_error"]:
            return "first query latch missing"
        if (outcome.get("diagnostics") != expected_global_diag
                or not outcome.get("health", {}).get("index_error")):
            return "first query diagnostic B/global mismatch"
        return None
    add("missing_record_direct", _failure_provider(lambda: fill(limit, linked=1),
        "link_round", link_args(0), missing), "full_deepcopy", "post_close_unverified",
        validate=check_first_query_diagnostics)
    for suffix, method, kwargs in (
        ("cohort", "cohort_snapshot", dict(source=SOURCE, cohort_start=T,
         cohort_end=T+1, as_of=T+1, as_of_mono=T+1)),
        ("contributions", "contributions_open", dict(as_of=T, as_of_mono=T, after_seq=limit, limit=16)),
        ("aggregation", "aggregation_snapshot", _at(T))):
        add("missing_record_first_query_"+suffix,
            _failure_provider(lambda: fill(limit, linked=1), method, kwargs, missing),
            "full_deepcopy", "post_close_unverified",
            validate=check_first_query_diagnostics)
    add("open_end_cursor", _repeat_provider(lambda: fill(limit),
        "contributions_open", dict(as_of=T, as_of_mono=T, after_seq=limit, limit=16)), "repeat",
        validate=lambda ledger, outcome, error, before: (
            "nonempty end cursor" if error or outcome["entries"] or outcome["next_seq"] is not None else None))
    cursor = {}
    def open_last_base():
        desired = min(DETAIL_CAP, limit - 1)
        record_charge, detail_charge = _headroom_tariffs(need_detail=True)
        available = _gate_resident_budget.get() - new_ledger(1).budget_state()["E"]
        # Keep the original large-detail origin when its charges fit; a tight
        # byte budget still needs one open entry to test the cursor boundary.
        if desired * (record_charge + detail_charge + 1024) + detail_charge > available:
            desired = 1
        ledger = fill_with_headroom(limit, accepts=1, detail_accepts=1,
                                    finalized=desired, unavailable_rest=True,
                                    min_existing=desired)
        if hasattr(ledger, "_gate_headroom_error"):
            raise HeadroomUnverified(ledger._gate_headroom_error)
        last_open_seq = ledger._open_seq[-1]
        cursor.update(after_seq=last_open_seq, last_open_seq=last_open_seq)
        return ledger
    open_last = _repeat_provider(open_last_base, "contributions_open",
        lambda _index: dict(as_of=T, as_of_mono=T, after_seq=cursor["after_seq"], limit=16))
    open_last.cursor = cursor
    add("open_last_seq_cursor", open_last, "repeat",
        validate=lambda ledger, outcome, error, before: (
            "last open cursor returned entries or changed"
            if error or not ledger._open_seq or cursor["last_open_seq"] != ledger._open_seq[-1]
            or cursor["after_seq"] != cursor["last_open_seq"] or cursor["last_open_seq"] >= limit
            or outcome["entries"] or outcome["next_seq"] is not None else None))
    order_probe = {}
    def order_base():
        ledger = _recent_seq_order_base(limit)
        first, second = (ledger._records[rid(i)] for i in (0, 1))
        reasons = [rec["detail"]["pairs"][PAIRS[0]]["collection"]["reason"]
                   for rec in (first, second)]
        order_probe.update(distinct_reasons=len(set(reasons)),
                           seq_bucket_reversed=first["bucket_start"] > second["bucket_start"])
        return ledger
    def check_order(ledger, outcome, error, before):
        if error or outcome.get("classification") != "snapshot":
            return "order snapshot missing"
        records = (ledger._records[rid(i)] for i in (0, 1))
        expected = list(dict.fromkeys(
            rec["detail"]["pairs"][PAIRS[0]]["collection"]["reason"] for rec in records))
        matches = [row for row in outcome["recent"]
                   if row["source"] == SOURCE and row["pair"] == PAIRS[0]]
        if (order_probe["distinct_reasons"] < 2 or not order_probe["seq_bucket_reversed"]
                or "other" in expected or len(matches) != 1
                or list(matches[0]["collection_unknown_reasons"]) != expected):
            return "recent dynamic reason order differs from seq first appearance"
        return None
    recent_order = _repeat_provider(order_base, "aggregation_snapshot", _at(T + 2 * MINUTE))
    recent_order.order_probe = order_probe
    add("recent_window_seq_order", recent_order, "repeat", k=2, validate=check_order)
    add("recent_window_boundary", _repeat_provider(lambda: _recent_base(limit, True),
        "aggregation_snapshot", _at(T+60*MINUTE), independent=True, split=True),
        "independent_split_copy", k=close_n-1)
    add("recent_window_before_exclusion", _repeat_provider(lambda: _recent_base(limit, True),
        "aggregation_snapshot", _at(T+61*MINUTE-1), independent=True, split=True),
        "independent_split_copy", k=close_n-1)
    add("recent_window_after_exclusion", _repeat_provider(lambda: _recent_base(limit, True),
        "aggregation_snapshot", _at(T+61*MINUTE), independent=True, split=True),
        "independent_split_copy", k=1)
    add("recent_window_before_close", _repeat_provider(lambda: _recent_base(limit, True),
        "aggregation_snapshot", _at(T+71*MINUTE-1), independent=True, split=True),
        "independent_split_copy", k=1)
    add("recent_window_exact_close", _repeat_provider(lambda: _recent_base(limit, True),
        "aggregation_snapshot", _at(T+71*MINUTE), independent=True, split=True),
        "independent_split_copy", d=close_n-1, k=1)
    def mark_wait_candidates(ledger, _index):
        ledger._gate_wait_candidates = tuple(ledger._close_index.due(T+MINUTE))
        ledger._gate_wait_details = tuple(ledger._open_seq)
        ledger._gate_wait_detail_content = {
            key: copy.deepcopy(rec["detail"]) for key, rec in dict.items(ledger._records)
            if rec["detail"] is not None}
    def check_wait_candidates(ledger, outcome, error, before):
        if (error or tuple(ledger._close_index.due(T+MINUTE)) != ledger._gate_wait_candidates
                or len(ledger._gate_wait_candidates) != ledger._gate_fixture_n):
            return "A pending candidates changed"
        target_end = ((wait_as_of - 70 * MINUTE) // MINUTE) * MINUTE
        if ledger._cumulative_end != target_end:
            return "cumulative_end did not advance to target"
        details = {key: rec["detail"] for key, rec in dict.items(ledger._records)
                   if rec["detail"] is not None}
        if (ledger._health["retained_details"] != before
                or tuple(ledger._open_seq) != ledger._gate_wait_details
                or details != ledger._gate_wait_detail_content):
            return "A pending detail changed"
        return None
    wait_as_of = T + 70 * MINUTE
    add("close_wait_target_advance", _repeat_provider(lambda: _wait_base(limit),
        "aggregation_snapshot", _at(wait_as_of), independent=True, split=True,
        prepare=mark_wait_candidates), "independent_split_copy",
        validate=check_wait_candidates)
    def check_outside_details(ledger, outcome, error, before):
        retained = sum(rec["detail"] is not None for rec in dict.values(ledger._records))
        if error or ledger._health["retained_details"] != before or retained != before:
            return "recent outside detail count changed"
        return None
    add("recent_outside_open", _repeat_provider(lambda: _recent_base(limit),
        "aggregation_snapshot", _at(T+61*MINUTE), independent=True, split=True),
        "independent_split_copy", validate=check_outside_details)
    add("recent_outside_open_one_inside", _repeat_provider(lambda: _recent_base(limit, True),
        "aggregation_snapshot", _at(T+61*MINUTE), independent=True, split=True),
        "independent_split_copy", k=1, validate=check_outside_details)
    return specs


def _load_average():
    observed = time.time()
    try:
        return {"observed_at": observed, "values": list(os.getloadavg()), "reason": None}
    except (OSError, AttributeError) as exc:
        return {"observed_at": observed, "values": None, "reason": type(exc).__name__}


def _normalize_adapter_row(row, samples, warmup):
    row = dict(row)
    row["sample_plan"] = "sequential" if row["name"] == "adapter_bank_to_finish" else "repeat"
    row["samples"] = {key: (row.get("timing") or {}).get(key, {}).get("n", 0)
                      for key in ("gc_disabled", "gc_enabled")}
    checked = warmup + sum(row["samples"].values()) + (2 if row["name"] == "adapter_bank_to_finish" else 1)
    row["sample_checks"] = {"checked": checked, "failures": []}
    row["D_observed"] = [0, 0]
    row["K_observed"] = [0, 0]
    row["sample_exception"] = None
    row["prepare_seconds"] = 0.0
    row["timed_calls"] = sum(row["samples"].values())
    if row.get("visit_gate") == "not_applicable":
        row["visit_gate"] = "PASS"
    return row


# S5a-4c fixtures use public transitions. Private storage is read only for the
# identity and capacity audits; no fixture is manufactured by assigning fields.
PRESSURE_NAMES = tuple(f"pressure_{track}_{kind}" for track in ("p0", "p1", "p2", "p3")
                       for kind in ("short_ascii", "ascii128", "unicode128"))
PRESSURE_AUX = ("pressure_unique_job_keys_byte_stop", "pressure_slot_first_small_limit")
CHURN_NAMES = tuple(f"churn_{track}_{kind}" for track in ("most_finished", "most_unfinished")
                    for kind in ("short_ascii", "ascii128", "unicode128")) + (
    "churn_init_failed_most_finished_unicode128", "churn_init_failed_most_unfinished_unicode128",
    "churn_burst_ascii128", "churn_burst_unicode128")


def _fixture_id(index, kind, prefix="p"):
    stem = f"{prefix}{index:08d}"
    if kind == "short_ascii":
        return stem
    if kind == "ascii128":
        return stem + "a" * (128 - len(stem))
    # Mixed non-ASCII, exactly 128 UTF-8 bytes; its Python size differs from ASCII.
    return stem + "한" + "a" * (125 - len(stem))


def _register_fixture(ledger, invocation_id, source, wall, job_id=None, serial_job=False):
    return ledger.register(epoch=EPOCH, invocation_id=invocation_id, source=source,
                           started_wall=wall, started_mono=wall, received_at=wall,
                           received_mono=wall, job_id=job_id, serial_job=serial_job)


def _link_fixture(ledger, invocation_id, source, wall):
    schema, contract = REGISTRY[source]
    return ledger.link_round(epoch=EPOCH, invocation_id=invocation_id,
                             round_id="round-" + invocation_id[:9],
                             report_schema=schema, validity_contract=contract,
                             received_at=wall, received_mono=wall)


def _finish_fixture(ledger, invocation_id, source, wall, *, summary=SUMMARY):
    schema, contract = REGISTRY[source]
    return ledger.finish(epoch=EPOCH, invocation_id=invocation_id,
                         round_id="round-" + invocation_id[:9],
                         report_schema=schema, validity_contract=contract,
                         finished_wall=wall, finished_mono=wall, selected_summary=summary,
                         telemetry_error_present=False, received_at=wall, received_mono=wall)


def _init_failed_fixture(ledger, invocation_id, wall):
    return ledger.report_init_failed(epoch=EPOCH, invocation_id=invocation_id,
                                     failed_wall=wall, failed_mono=wall,
                                     received_at=wall, received_mono=wall)


def _budget_checkpoint(ledger):
    gc.collect()
    budget = ledger.budget_state()
    graph = owned_graph(ledger)
    health = ledger._health
    return {"F_4": budget["F_4"], "Q_4": budget["Q_4"], "D": budget["D"],
            "A": budget["A"], "R": budget["R"], "T": budget["T"], "R_T": budget["R_T"],
            "AR": budget["AR"], "TR": budget["TR"], "E": budget["E"], "B": budget["B"],
            "G": graph["bytes"] if not graph["unknown_types"] else None,
            "B_minus_E": budget["B"] - budget["E"],
            "N_total": budget["N_total"], "N_live": budget["N_live"],
            "N_tomb": budget["N_tomb"], "N_res": budget["N_res"],
            "capacity": budget["capacity"], "rebuild_count": budget["rebuild_count"],
            "unknown_types": graph["unknown_types"]}


def _budget_checkpoint_without_graph(budget):
    """Keep a pre-call public budget when no pre-call graph was captured."""
    return {"F_4": budget["F_4"], "Q_4": budget["Q_4"], "D": budget["D"],
            "A": budget["A"], "R": budget["R"], "T": budget["T"], "R_T": budget["R_T"],
            "AR": budget["AR"], "TR": budget["TR"], "E": budget["E"], "B": budget["B"],
            "G": None, "B_minus_E": budget["B"] - budget["E"],
            "N_total": budget["N_total"], "N_live": budget["N_live"],
            "N_tomb": budget["N_tomb"], "N_res": budget["N_res"],
            "capacity": budget["capacity"], "rebuild_count": budget["rebuild_count"],
            "unknown_types": ["pre_rejection_graph_not_captured"]}


def _job_counts(counter):
    return [{"source": s, "job_id": j, "registrations": n}
            for (s, j), n in sorted(counter.items())]


def _identity_absent(ledger, invocation_id):
    if invocation_id in ledger._records or invocation_id in ledger._tombs or invocation_id in ledger._owned_ids:
        return False
    if invocation_id in ledger._seq.positions.values():
        return False
    if any(invocation_id == owner for owner in ledger._owners.values()):
        return False
    seq = ledger._health["N_total"] + 1
    if any(seq in getattr(ledger, "_" + name + "_index").slots
           for name in ("close", "overdue", "expiry", "prune")):
        return False
    if any(seq == pair[1] for index in ledger._cohort_index.values()
           for block in index.blocks for pair in block):
        return False
    return True


def _pressure_index_point(ledger, invocation_id, source, job_id, seq):
    """Read every candidate identity path without changing the ledger."""
    paths = {
        "live": invocation_id in ledger._records,
        "tomb": invocation_id in ledger._tombs,
        "seq": invocation_id in ledger._seq.positions.values(),
        "owner": invocation_id in ledger._owners.values(),
        "owned_id": invocation_id in ledger._owned_ids,
        "previous_job": job_id is not None and (source, job_id) in ledger._previous_job,
        "cohort": any(pair[1] == seq for index in ledger._cohort_index.values()
                      for block in index.blocks for pair in block),
        "open": seq in ledger._open_seq,
        "recent": any(seq in bucket for bucket in ledger._recent_buckets.values()),
    }
    for name in ("close", "overdue", "expiry", "prune"):
        paths[name] = seq in getattr(ledger, "_" + name + "_index").slots
    return {"N_total": ledger._health["N_total"], "last_seq": ledger._health["N_total"],
            "paths": paths}


class _PressureTrace:
    """Keep a compact witness of calls made by one public-API pressure fixture."""

    def __init__(self):
        self.api = []
        self.receipts = []
        self.existing_budgets = []

    def call(self, ledger, action, invocation_id, source, wall, operation, *,
             job_id=None, started=None, existing=False):
        before_health = ledger._health
        before_state = ("live" if invocation_id in ledger._records else
                        "tombstoned" if invocation_id in ledger._tombs else "absent")
        before_seq = (ledger._records[invocation_id]["seq"] if before_state == "live" else
                      ledger._health["N_total"])
        before_d = ledger._budget_d
        budget_before = _budget_checkpoint(ledger) if existing else None
        receipt = {"call_index": len(self.api), "received_at": wall, "received_mono": wall,
                   "started_at": wall if started is None else started,
                   "started_mono": wall if started is None else started,
                   "N_total": before_health["N_total"], "N_res": before_health["N_res"],
                   "N_tomb": before_health["N_tomb"],
                   "retire_eligible": sum(1 for _ in ledger._prune_index.due(wall))}
        outcome = operation()
        classification = outcome["classification"] if isinstance(outcome, dict) else outcome
        after_state = ("live" if invocation_id in ledger._records else
                       "tombstoned" if invocation_id in ledger._tombs else "absent")
        event = {"call_index": len(self.api), "action": action, "id": invocation_id,
                 "source": source, "job_id": job_id, "received_at": wall,
                 "received_mono": wall, "started_at": receipt["started_at"],
                 "started_mono": receipt["started_mono"], "classification": classification,
                 "registered_seq": (ledger._records[invocation_id]["seq"]
                                    if after_state == "live" else before_seq),
                 "detail_charge_bytes": ledger._budget_d - before_d,
                 "state_before": before_state, "state_after": after_state}
        self.api.append(event)
        self.receipts.append(receipt)
        if existing:
            budget_after = _budget_checkpoint(ledger)
            self.existing_budgets.append({"action": action, "id": invocation_id,
                                          "classification": classification,
                                          "state_before": before_state,
                                          "AR_before": budget_before["AR"],
                                          "AR_after": budget_after["AR"],
                                          "before": budget_before, "after": budget_after})
        return outcome


def _pressure_candidate_e(ledger, kwargs, before, next_q, template):
    """Recompute the register precheck tariff from a fresh unbound record."""
    if template is None:
        probe = new_ledger(1)
        if probe.register(**kwargs)["classification"] != "registered":
            raise RuntimeError("candidate tariff probe unavailable")
        template = probe._records[kwargs["invocation_id"]]
    candidate = copy.deepcopy(template)
    delta_wall = kwargs["started_wall"] - candidate["started_wall"]
    delta_mono = kwargs["started_mono"] - candidate["started_mono"]
    candidate["invocation_id"] = kwargs["invocation_id"]
    candidate["source"] = kwargs["source"]
    candidate["seq"] = ledger._health["N_total"] + 1
    candidate["started_wall"] = kwargs["started_wall"]
    candidate["started_mono"] = kwargs["started_mono"]
    candidate["job_id"] = kwargs["job_id"]
    candidate["serial_job"] = kwargs["serial_job"]
    candidate.retire_at += delta_wall
    candidate.retire_mono += delta_mono
    candidate.prune_at += delta_wall
    a, r = ledger._record_tariff(candidate, admitted=False)
    return before["F_4"] + next_q + before["D"] + before["AR"] + before["TR"] + a + r


def _post_latch_existing_transitions(ledger, *, kind, n_last, wall, predecessor_id,
                                     predecessor_job, candidate_index, trace):
    """Exercise existing identities on the latched origin through public APIs."""
    transitions = {}
    before_record = ledger.record(predecessor_id)
    before_job = copy.deepcopy(ledger._previous_job.get((SOURCE, predecessor_job)))
    serial_candidate = _fixture_id(candidate_index, kind)
    serial_result = trace.call(ledger, "register", serial_candidate, SOURCE, wall,
                               lambda: _register_fixture(ledger, serial_candidate, SOURCE, wall,
                                                         job_id=predecessor_job, serial_job=True),
                               job_id=predecessor_job)
    after_record = ledger.record(predecessor_id)
    after_job = ledger._previous_job.get((SOURCE, predecessor_job))
    next_entry = (
        "predecessor_unchanged" if before_record is not None and before_job is not None
        and serial_result["classification"] == "admission_stopped"
        and _identity_absent(ledger, serial_candidate)
        and before_record["lifecycle"] == after_record["lifecycle"]
        and before_record["exit_evidence"] == after_record["exit_evidence"]
        and before_job == after_job else "predecessor_changed")
    transitions["next_entry"] = trace.call(
        ledger, "next_entry", predecessor_id, SOURCE, wall, lambda: next_entry,
        existing=True)

    available = [invocation_id for invocation_id in reversed(tuple(ledger._records))
                 if (rec := ledger.record(invocation_id)) is not None
                 and rec["connection"] == "unbound"
                 and rec["lifecycle"] in ("awaiting_report", "in_flight")]
    if len(available) < 4:
        return {key: transitions.get(key, "existing_id_unavailable") for key in (
            "link_round", "report_init_failed", "wrapper_exited", "next_entry",
            "overdue", "finish", "late_finish", "close", "post_close_duplicate")}
    first_id, late_id, init_id, exit_id = available[:4]
    transitions["link_round"] = trace.call(
        ledger, "link_round", first_id, SOURCE, wall,
        lambda: _link_fixture(ledger, first_id, SOURCE, wall), existing=True)["classification"]
    trace.call(ledger, "link_round", late_id, SOURCE, wall,
               lambda: _link_fixture(ledger, late_id, SOURCE, wall))
    transitions["report_init_failed"] = trace.call(
        ledger, "report_init_failed", init_id, SOURCE, wall,
        lambda: _init_failed_fixture(ledger, init_id, wall), existing=True)["classification"]
    transitions["wrapper_exited"] = trace.call(
        ledger, "wrapper_exited", exit_id, SOURCE, wall,
        lambda: ledger.wrapper_exited(epoch=EPOCH, invocation_id=exit_id,
                                     exited_wall=wall, exited_mono=wall,
                                     received_at=wall, received_mono=wall),
        existing=True)["classification"]

    overdue_at = wall + 15 * MINUTE
    def overdue():
        ledger.aggregation_snapshot(as_of=overdue_at, as_of_mono=overdue_at)
        record = ledger.record(late_id)
        return record["lifecycle"] if record is not None else "existing_id_unavailable"
    transitions["overdue"] = trace.call(ledger, "overdue", late_id, SOURCE,
                                        overdue_at, overdue, existing=True)
    finish_at = wall + 71 * MINUTE
    ledger.aggregation_snapshot(as_of=finish_at, as_of_mono=finish_at)
    # An admission stop can leave less than a detail charge of free capacity.
    # Existing unavailable transitions release their unused record reserve.
    for invocation_id in available[4:]:
        budget = ledger.budget_state()
        if budget["B"] - budget["E"] >= 7_000:
            break
        trace.call(ledger, "report_init_failed", invocation_id, SOURCE, finish_at,
                   lambda invocation_id=invocation_id: _init_failed_fixture(
                       ledger, invocation_id, finish_at))
    transitions["finish"] = trace.call(
        ledger, "finish", first_id, SOURCE, finish_at,
        lambda: _finish_fixture(ledger, first_id, SOURCE, finish_at),
        existing=True)["classification"]

    close_at = finish_at + 71 * MINUTE
    def close():
        ledger.aggregation_snapshot(as_of=close_at, as_of_mono=close_at)
        closed_record = ledger.record(first_id)
        closed_tomb = ledger._tombs.get(first_id)
        return ("closed" if (closed_record is not None and closed_record["closed"])
                or (closed_tomb is not None and not closed_tomb.expired) else "not_closed")
    transitions["close"] = trace.call(ledger, "close", first_id, SOURCE,
                                      close_at, close, existing=True)
    schema, contract = REGISTRY[SOURCE]
    transitions["post_close_duplicate"] = trace.call(
        ledger, "post_close_duplicate", first_id, SOURCE, close_at,
        lambda: ledger.finish(
            epoch=EPOCH, invocation_id=first_id, round_id="round-" + first_id[:9],
            report_schema=schema, validity_contract=contract,
            finished_wall=finish_at, finished_mono=finish_at, selected_summary=SUMMARY,
            telemetry_error_present=False, received_at=close_at, received_mono=close_at),
        existing=True)["classification"]
    transitions["late_finish"] = trace.call(
        ledger, "late_finish", late_id, SOURCE, close_at,
        lambda: _finish_fixture(ledger, late_id, SOURCE, close_at),
        existing=True)["classification"]
    return transitions


def _post_latch_job_predecessor(ledger, *, kind, n_last, wall, candidate_index, trace):
    """The byte-limited unique-key control preserves its previous job summary."""
    predecessor_job = f"job{n_last - 1:08d}"
    before = copy.deepcopy(ledger._previous_job.get((SOURCE, predecessor_job)))
    candidate_id = _fixture_id(candidate_index, kind)
    result = trace.call(ledger, "register", candidate_id, SOURCE, wall,
                        lambda: _register_fixture(ledger, candidate_id, SOURCE, wall,
                                                  job_id=predecessor_job, serial_job=True),
                        job_id=predecessor_job)
    unchanged = (before is not None and result["classification"] == "admission_stopped"
                 and _identity_absent(ledger, candidate_id)
                 and before == ledger._previous_job.get((SOURCE, predecessor_job)))
    return {"next_entry": "predecessor_unchanged" if unchanged else "predecessor_changed"}


def run_pressure_fixture(name, *, limit=CAP, max_resident_bytes=62_914_560,
                         fixture_detail_divisor=1, _capacity_observer=None,
                         _stop_requested=None, _progress=None):
    if name not in PRESSURE_NAMES + PRESSURE_AUX:
        raise ValueError("unknown pressure fixture")
    if not 1 <= limit <= CAP or fixture_detail_divisor < 1:
        raise ValueError("invalid pressure fixture scale")
    if name == "pressure_slot_first_small_limit":
        fixture_limit, kind, track = min(limit, 128), "short_ascii", "slot_aux"
        max_resident_bytes = 62_914_560
    elif name == "pressure_unique_job_keys_byte_stop":
        fixture_limit, kind, track = limit, "short_ascii", "job_aux"
    else:
        fixture_limit = limit
        _, track, kind = name.split("_", 2)
    requested = 512 if track == "p2" else 2048 if track == "p3" else 0
    if requested and fixture_limit < 2:
        raise ValueError("detail fixture needs a probe slot")
    target = min((requested + fixture_detail_divisor - 1) // fixture_detail_divisor,
                 DETAIL_CAP, fixture_limit - 1) if requested else 0
    ledger = new_ledger(fixture_limit, max_resident_bytes)
    fixed = ledger.budget_state()["F_4"]
    states, sources, jobs = Counter(), Counter(), Counter()
    trace = _PressureTrace()
    clone_equivalence = {"used": False, "original_before_sha256": None,
                         "original_after_sha256": None, "clone_replay_sha256": None}
    clone_api_trace, pre_rejection_digest = [], None
    candidate_template = None
    failures = []
    first, before, after, n_last, control = None, None, None, None, None
    interrupted = False
    # The unique-key input advances both clocks so retired identity does not
    # masquerade as a slot limit. Each key is new, while the control reuses one.
    for i in range(max(fixture_limit + 1, 100_000) if track == "job_aux" else fixture_limit + 1):
        if _stop_requested is not None and _stop_requested():
            interrupted = True
            break
        if _progress is not None and i and i % 1000 == 0:
            _progress(i)
        wall = T + i * 6 * 60 * MINUTE if track == "job_aux" else T
        invocation_id = _fixture_id(i, kind)
        job_id = f"job{i:08d}" if track == "job_aux" else "post_latch_probe" if i == 0 else None
        kwargs = dict(epoch=EPOCH, invocation_id=invocation_id, source=SOURCE,
                      started_wall=wall, started_mono=wall, received_at=wall,
                      received_mono=wall, job_id=job_id, serial_job=job_id is not None)
        # Capture the precise pre-rejection state; the control is a detached
        # clone of the public-API-built history and does not alter the original.
        pre_health = ledger._health.copy()
        pre_job_absent = job_id is None or (SOURCE, job_id) not in ledger._previous_job
        next_jobs = len(ledger._previous_job) + int(pre_job_absent and job_id is not None)
        current_q = ledger._q_charge()
        next_q = ledger._q_charge(pre_health["N_res"] + 1, next_jobs)
        possible_rejection = (pre_health["N_res"] >= fixture_limit or
                              max_resident_bytes - _fixture_budget_e(ledger, fixed) <=
                              max(0, next_q - current_q) + 64_000)
        pre_identity_absent = _identity_absent(ledger, invocation_id) if possible_rejection else False
        pre_index = (_pressure_index_point(ledger, invocation_id, SOURCE, job_id,
                                           pre_health["N_total"] + 1)
                     if possible_rejection else None)
        alternate = (clone_ledger(ledger) if track == "job_aux" and i > 0
                     and possible_rejection else None)
        if alternate is not None:
            alternate.aggregation_snapshot(as_of=wall, as_of_mono=wall)
            pre_health = alternate._health.copy()
        pre_checkpoint = (_budget_checkpoint(alternate if alternate is not None else ledger)
                          if possible_rejection else None)
        result = trace.call(ledger, "register", invocation_id, SOURCE, wall,
                            lambda: ledger.register(**kwargs), job_id=job_id)
        if result["classification"] == "admission_stopped":
            n_last = pre_health["N_total"]
            # The rejection changes health diagnostics, but not any identity index.
            before = (pre_checkpoint if pre_checkpoint is not None else
                      _budget_checkpoint_without_graph(ledger.budget_state()))
            if pre_checkpoint is None:
                failures.append({"index": i, "reason": "pre-rejection checkpoint not captured"})
            after = _budget_checkpoint(ledger)
            if _capacity_observer is not None and track != "job_aux":
                _capacity_observer(ledger, name, "highwater", i, budget=after)
            slot = pre_health["N_res"] >= fixture_limit
            cause = "slot" if slot else "byte"
            if track == "job_aux" and alternate is not None:
                old_job = "job00000000"
                pre_rejection_digest = _state_digest(alternate)
                original_digest = _state_digest(ledger)
                clone_call = {"call_index": len(trace.api), "action": "register",
                              "branch": "detached_clone", "source": SOURCE,
                              "job_id": old_job}
                same = alternate.register(**dict(kwargs, job_id=old_job))
                clone_call["classification"] = same["classification"]
                clone_api_trace.append(clone_call)
                clone_equivalence = {"used": True, "original_before_sha256": original_digest,
                                     "original_after_sha256": _state_digest(ledger),
                                     "clone_replay_sha256": original_digest}
                control = {"source": SOURCE, "job_id": old_job,
                           "classification": same["classification"],
                           "branch": "detached_clone",
                           "forked_before_call_index": trace.api[-1]["call_index"],
                           "trace_call_index": clone_call["call_index"],
                           "pre_rejection_sha256": pre_rejection_digest,
                           "clone_base_sha256": pre_rejection_digest}
            # The precheck uses the same record tariff that register reserves.
            candidate_e = _pressure_candidate_e(ledger, kwargs, before, next_q,
                                                candidate_template)
            after_index = _pressure_index_point(ledger, invocation_id, SOURCE, job_id,
                                                pre_health["N_total"] + 1)
            first = {"candidate_id": invocation_id, "candidate_source": SOURCE,
                     "candidate_job_id": job_id, "candidate_seq": n_last + 1,
                     "received_at": wall, "received_mono": wall,
                     "classification": result["classification"], "cause": cause,
                     "simultaneous_causes": (["slot"] if slot else []) +
                     (["byte"] if candidate_e > before["B"] else []),
                     "precedence": "slot_then_byte",
                     "slot_test": {"candidate_N_res": pre_health["N_res"] + 1,
                                   "would_exceed": slot},
                     "byte_precheck": {"candidate_E": candidate_e,
                                       "would_exceed": candidate_e > before["B"]},
                     "trace_call_index": trace.api[-1]["call_index"],
                     "health_before": {key: pre_health[key] for key in
                                       ("N_total", "admission_stopped", "admission_stopped_at")}
                     | {"last_seq": pre_health["N_total"]},
                     "health_after": {key: ledger._health[key] for key in
                                      ("N_total", "admission_stopped", "admission_stopped_at")}
                     | {"last_seq": ledger._health["N_total"]},
                     "index_audit": {"before": pre_index, "after": after_index,
                                     "absent_paths": sorted(_INDEX_PATHS),
                                     "owned_bytes": {"candidate_before": 0 if pre_identity_absent else
                                                     sys.getsizeof(invocation_id),
                                                     "candidate_after": 0 if _identity_absent(
                                                         ledger, invocation_id) else sys.getsizeof(invocation_id)}},
                     "diagnostics_codes": result["diagnostics"]["codes"],
                     "admission_stopped_at": ledger._health["admission_stopped_at"],
                    "no_insertion": (pre_checkpoint is not None and pre_identity_absent and pre_job_absent and
                                     _identity_absent(ledger, invocation_id) and
                                     (job_id is None or (SOURCE, job_id) not in ledger._previous_job) and
                                      all(before[key] == after[key] for key in
                                          ("F_4", "Q_4", "D", "A", "R", "T", "R_T", "AR", "TR", "E")))}
            break
        if result["classification"] != "registered":
            failures.append({"index": i, "classification": result["classification"]})
            break
        if candidate_template is None:
            candidate_template = copy.deepcopy(ledger._records[invocation_id])
        states["unbound"] += 1
        sources[SOURCE] += 1
        if job_id is not None:
            jobs[(SOURCE, job_id)] += 1
        action = ("finished" if i < target else
                  "linked" if track in ("p1", "p2", "p3") and i % 20 < 4 else
                  "init_failed" if track in ("p1", "p2", "p3") and 4 <= i % 20 < 8 else None)
        if action == "finished":
            a = trace.call(ledger, "link_round", invocation_id, SOURCE, wall,
                           lambda: _link_fixture(ledger, invocation_id, SOURCE, wall))
            b = (trace.call(ledger, "finish", invocation_id, SOURCE, wall,
                            lambda: _finish_fixture(ledger, invocation_id, SOURCE, wall))
                 if a["classification"] == "linked" else a)
            good = a["classification"] == "linked" and b["classification"] == "finalized"
        elif action == "linked":
            good = trace.call(ledger, "link_round", invocation_id, SOURCE, wall,
                              lambda: _link_fixture(ledger, invocation_id, SOURCE, wall))["classification"] == "linked"
        elif action == "init_failed":
            good = trace.call(ledger, "report_init_failed", invocation_id, SOURCE, wall,
                              lambda: _init_failed_fixture(ledger, invocation_id, wall))["classification"] == "report_init_failed"
        else:
            good = True
        if not good:
            failures.append({"index": i, "action": action})
            break
        if action:
            states["unbound"] -= 1
            states[action] += 1
    _check_fixture_budget_e(ledger, fixed)
    if first is None and not interrupted:
        failures.append({"reason": "no first admission stop"})
    retained_at_stop = ledger._health["retained_details"]
    stop_time = ledger._health["admission_stopped_at"]
    post_results = []
    if first:
        for offset, source in ((1, SOURCE), (4, "investing"), (5, "citi")):
            new_id = _fixture_id(i + offset, kind)
            call_kwargs = dict(kwargs, invocation_id=new_id, source=source)
            outcome = trace.call(ledger, "register", new_id, source, wall,
                                 lambda call_kwargs=call_kwargs: ledger.register(**call_kwargs),
                                 job_id=job_id)
            post_results.append((source, outcome))
    predecessor_id = _fixture_id(0, kind)
    predecessor_job = "post_latch_probe"
    transitions = (_post_latch_existing_transitions(
        ledger, kind=kind, n_last=n_last, wall=wall, predecessor_id=predecessor_id,
        predecessor_job=predecessor_job, candidate_index=i + 3, trace=trace)
        if first and n_last and track != "job_aux" else
        _post_latch_job_predecessor(ledger, kind=kind, n_last=n_last, wall=wall,
                                    candidate_index=i + 3, trace=trace)
        if first and n_last else {})
    released = False
    prune_before = ledger._health["N_res"]
    prune_after = prune_before
    if first:
        later = wall + 7 * 60 * MINUTE
        ledger.aggregation_snapshot(as_of=later, as_of_mono=later)
        prune_after = ledger._health["N_res"]
        release_id = _fixture_id(i + 2, kind)
        release_kwargs = dict(kwargs, invocation_id=release_id, started_wall=later,
                              started_mono=later, received_at=later, received_mono=later)
        outcome = trace.call(ledger, "register", release_id, SOURCE, later,
                             lambda: ledger.register(**release_kwargs), job_id=job_id)
        post_results.append((SOURCE, outcome))
        released = outcome["classification"] == "registered"
    post_trace = [call for call in trace.api if first is not None and
                  call["call_index"] > first["trace_call_index"] and
                  call["action"] == "register"]
    coverage = {source: {"uncertain": source in ledger._health["uncertain_sources"],
                         "diagnostic_codes": outcome["diagnostics"]["codes"]}
                for source, outcome in post_results}
    post_budget = _budget_checkpoint(ledger) if first else None
    post = {"attempted": len(post_trace),
            "all_rejected": bool(post_trace) and all(
                call["classification"] == "admission_stopped" for call in post_trace),
            "first_stop_time_unchanged": ledger._health["admission_stopped_at"] == stop_time,
            "resumed_after_release": released, "existing_id_transitions": transitions,
            "api_trace": post_trace, "coverage_by_source": coverage,
            "g_diagnostic": {"measured": post_budget is not None,
                             "G": post_budget["G"] if post_budget else None,
                             "E": post_budget["E"] if post_budget else None},
            "prune_after_release": {"N_res_before": prune_before,
                                    "N_res_after": prune_after,
                                    "attempt_classification": post_results[-1][1]["classification"]
                                    if post_results else None, "stop_time": stop_time},
            "existing_id_budget": trace.existing_budgets}
    transition_ok = (transitions == {"next_entry": "predecessor_unchanged"}
                     if track == "job_aux" else transitions == {
        "link_round": "linked", "report_init_failed": "report_init_failed",
        "wrapper_exited": "wrapper_exited", "next_entry": "predecessor_unchanged",
        "overdue": "overdue", "finish": "finalized", "late_finish": "finalized",
        "close": "closed", "post_close_duplicate": "post_close_duplicate"})
    residency_ok = (before is not None and after is not None and
                    before["N_total"] == after["N_total"] == n_last and
                    (before["N_res"] < fixture_limit and after["N_res"] < fixture_limit
                     if track == "job_aux" else
                     before["N_res"] == after["N_res"] == n_last and
                     before["N_tomb"] == after["N_tomb"] == 0))
    valid = (not failures and first is not None and first["no_insertion"] and residency_ok and
             sum(states.values()) == n_last and
             not ledger._health["coverage_complete"] and
             set(ledger._health["uncertain_sources"]) == set(REGISTRY) and
             before["E"] <= before["B"] and before["G"] is not None and before["G"] <= before["E"] and
             after["G"] is not None and after["G"] <= after["E"] <= after["B"] and
             post["all_rejected"] and not post["resumed_after_release"] and post["first_stop_time_unchanged"] and
             transition_ok and
             (control is None or control["classification"] == "registered") and
             (not requested or states["finished"] >= target))
    detail_shortfall = bool(requested and states["finished"] < target)
    result = {"name": name, "status": "UNVERIFIED" if interrupted else "PASS" if valid else "UNVERIFIED" if detail_shortfall else
              "FAIL" if failures or first else "UNVERIFIED",
              "id_kind": kind, "track": track, "fixture_limit": fixture_limit,
              "source_registered": dict(sources), "job_key_counts": _job_counts(jobs),
              "requested_detail_target": requested,
              "effective_detail_target": min(target, states["finished"]) if requested else 0,
              "retained_details": retained_at_stop,
              "state_counts": dict(states), "id_utf8_bytes": len(_fixture_id(0, kind).encode()),
              "id_getsizeof_bytes": sys.getsizeof(_fixture_id(0, kind)),
              "N_last_accepted": n_last, "first_rejection": first, "before": before,
              "after": after, "post_latch": post, "sample_failures": failures,
              "reason": "budget" if interrupted else None if valid else
              "pressure fixture incomplete or invariant failed"}
    pressure_end = first["trace_call_index"] + 1 if first is not None else len(trace.api)
    accepted_receipts = trace.receipts[:pressure_end]
    result["state_registration_counts"] = dict(sources)
    result["fixture_provenance"] = {
        "api_trace": trace.api, "detail_charged_bytes": sum(
            call["detail_charge_bytes"] for call in trace.api),
        "clone_equivalence": clone_equivalence,
        "clone_api_trace": clone_api_trace,
        "pre_rejection_state_sha256": pre_rejection_digest}
    result["transition_trace"] = [{"action": call["action"],
                                    "classification": call["classification"],
                                    "detail_charge_bytes": call["detail_charge_bytes"],
                                    "seq": call["registered_seq"]} for call in trace.api]
    result["receipt_invariants"] = {
        "checked_steps": accepted_receipts,
        "same_pair": len({(step["received_at"], step["received_mono"])
                          for step in accepted_receipts}) == 1,
        "fresh_starts": all(step["received_at"] == step["started_at"] and
                            step["received_mono"] == step["started_mono"]
                            for step in accepted_receipts),
        "no_retirement": all(step["N_total"] == step["N_res"] and
                             step["N_tomb"] == step["retire_eligible"] == 0
                             for step in accepted_receipts)}
    if track == "job_aux":
        result["control_existing_key"] = control
    return result


def _churn_checkpoint(ledger, kind, minute_index, received_at, scheduled, auxiliary,
                      *, readonly=False, graph=True, sources=(), attempts=0,
                      source_attempts=(), init_failed=0):
    if readonly:
        # A pre-register witness must not advance the ledger to the call's time.
        cumulative_end = ledger._cumulative_end
        cursor_next_seq = ledger._open_seq[0] if len(ledger._open_seq) > 1 else None
    else:
        snapshot = ledger.aggregation_snapshot(as_of=received_at, as_of_mono=received_at)
        cursor = ledger.contributions_open(as_of=received_at, as_of_mono=received_at,
                                           after_seq=0, limit=1)
        cumulative_end = snapshot.get("cumulative_end")
        cursor_next_seq = cursor.get("next_seq")
    public = kind in ("hour", "day", "tail_before", "tail_after",
                      "tail_80640", "tail_80641")
    evidence = {}
    if public and not readonly:
        epoch = ledger.epoch_cohort_totals(as_of=received_at, as_of_mono=received_at)
        evidence["epoch_sources"] = epoch.get("sources", [])
        recent = {}
        start = max(ledger._health["cohort_exact_from"], received_at - 60 * MINUTE)
        if start >= received_at:
            start = received_at - 1
        for source in REGISTRY:
            cohort = ledger.cohort_snapshot(source=source, cohort_start=start,
                                            cohort_end=received_at, as_of=received_at,
                                            as_of_mono=received_at)
            recent[source] = {"range": [start, received_at],
                              "classification": cohort["classification"],
                              "equations_hold": cohort.get("equations_hold")}
        evidence["recent_cohorts"] = recent
        latest = ledger._open_seq[-1] if ledger._open_seq else 0
        probes = []
        for after_seq in (0, latest):
            page = ledger.contributions_open(as_of=received_at, as_of_mono=received_at,
                                             after_seq=after_seq, limit=16)
            probes.append({"after_seq": after_seq,
                           "entry_seqs": [entry["seq"] for entry in page["entries"]],
                           "next_seq": page["next_seq"],
                           "classification": page["classification"]})
        evidence["cursor_probes"] = probes
        if kind.startswith("tail") and ledger._health["cohort_exact_from"] > 0:
            old = ledger.cohort_snapshot(source=SOURCE, cohort_start=0,
                                         cohort_end=ledger._health["cohort_exact_from"],
                                         as_of=received_at, as_of_mono=received_at)
            evidence["old_cohort_probe"] = {"range": [0, ledger._health["cohort_exact_from"]],
                                            "classification": old["classification"]}
    if kind in ("probe_recent_before", "probe_recent_at") and not readonly:
        evidence["recent_rows"] = snapshot["recent"]
    h = ledger._health
    budget = _budget_checkpoint(ledger) if graph else _budget_checkpoint_without_graph(ledger.budget_state())
    source_counts = {source: {"scheduled": source_attempts.get(source, 0),
                              "registered": sources.get(source, 0),
                              "rejected": source_attempts.get(source, 0) - sources.get(source, 0)}
                     for source in REGISTRY}
    return {"kind": kind, "minute_index": minute_index, "received_at": received_at,
            "received_mono": received_at, "scheduled_registered": scheduled,
            "auxiliary_registered": auxiliary, "N_total": h["N_total"],
            "N_live": h["N_live"], "N_tomb": h["N_tomb"], "N_res": h["N_res"],
            "retained_details": h["retained_details"], "budget": budget,
            "last_received_at": h["last_received_at"], "last_received_mono": h["last_received_mono"],
            "cumulative_end": cumulative_end, "cursor_after_seq": 0,
            "close_through": cumulative_end,
            "cursor_next_seq": cursor_next_seq,
            "cohort_exact_from": h["cohort_exact_from"], "frozen_through": h["frozen_through"],
            "coverage_complete": h["coverage_complete"], "uncertain_sources": list(h["uncertain_sources"]),
            "admission_stopped": h["admission_stopped"], "admission_stopped_at": h["admission_stopped_at"],
            "rebuild_count": budget["rebuild_count"],
            "capacity_charged_bytes": budget["capacity"]["charged_bytes"],
            "checkpoint_id": None,
            "observation": "passive" if readonly else "public_advance",
            "admission_counts": {"scheduled": attempts, "registered": h["N_total"],
                                 "rejected": attempts - h["N_total"]},
            "source_counts": source_counts, "last_seq": h["N_total"],
            "registered_records": h["registered_records"],
            "diagnostic_counters": {key: h[key] for key in
                                    ("retention_expired", "expired_start", "expired_finish",
                                     "expired_wrapper", "expired_identity_unverified")} |
                                   {"init_failed": init_failed},
            **evidence}


def churn_checkpoint_gaps(checkpoints, *, minute_batches, stride_minutes) -> list[str]:
    """Check schedule-relative hour/day boundaries and both tail observations."""
    if min(minute_batches, stride_minutes) < 1:
        raise ValueError("invalid churn scale")
    span_minutes = minute_batches * stride_minutes
    full = minute_batches == 10_080 and stride_minutes == 1
    before_kind, after_kind = (("tail_80640", "tail_80641") if full else
                               ("tail_before", "tail_after"))
    gaps = []
    tails = {kind: [cp.get("received_at") for cp in checkpoints if cp.get("kind") == kind]
             for kind in (before_kind, after_kind)}
    for kind, times in tails.items():
        if len(times) != 1:
            gaps.append(f"{kind}: expected one checkpoint, found {len(times)}")
    anchor = next((times[0] for times in tails.values() if times), None)
    if not isinstance(anchor, int):
        return gaps + ["tail time missing; hour/day boundaries cannot be verified"]
    if tails[after_kind] and tails[after_kind][0] != anchor:
        gaps.append(f"{after_kind}: expected received_at {anchor}, found {tails[after_kind][0]}")

    schedule_start = anchor - span_minutes * MINUTE
    for kind, step_minutes in (("hour", 60), ("day", 1440)):
        expected = Counter(schedule_start + k * step_minutes * MINUTE
                           for k in range(1, span_minutes // step_minutes + 1))
        actual = Counter(cp.get("received_at") for cp in checkpoints if cp.get("kind") == kind)
        for label, difference in (("missing", expected - actual), ("unexpected", actual - expected)):
            if difference:
                examples = ", ".join(f"{at!r} ({count})" for at, count in
                                     sorted(difference.items(), key=lambda item: str(item[0]))[:3])
                gaps.append(f"{kind} {label}: {sum(difference.values())} checkpoint(s); "
                            f"received_at {examples}")
    return gaps


_C2_COMMON = {"checkpoint_id", "observation", "admission_counts", "source_counts",
              "last_seq", "registered_records", "diagnostic_counters"}
_C2_PUBLIC = {"epoch_sources", "recent_cohorts", "cursor_probes"}
_C2_DIAG = {"retention_expired", "expired_start", "expired_finish",
            "expired_wrapper", "expired_identity_unverified", "init_failed"}
_C2_PAIRS = (("probe_close_before", "probe_close_at"),
             ("probe_expire_before", "probe_expire_at"),
             ("probe_prune_before", "probe_prune_at"),
             ("probe_recent_before", "probe_recent_at"))
_C2_PROBE_CATEGORIES = {"close": {"finished"},
                        "expire": {"linked", "unbound"},
                        "prune": {"finished", "linked", "unbound"},
                        "recent": {"finished"}}


def churn_evidence_gaps(fixture_result) -> list[str]:
    """Find absent C′2 witnesses separately from fully observed violations."""
    gaps = []
    checkpoints = fixture_result.get("checkpoints", [])
    ids = [cp.get("checkpoint_id") for cp in checkpoints]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        gaps.append("checkpoint_id missing or duplicated")
    for index, cp in enumerate(checkpoints):
        missing = _C2_COMMON - cp.keys()
        if missing:
            gaps.append(f"checkpoint {index} {cp.get('kind')}: missing {sorted(missing)}")
        diag = cp.get("diagnostic_counters")
        if not isinstance(diag, dict) or _C2_DIAG - diag.keys():
            gaps.append(f"checkpoint {index} {cp.get('kind')}: diagnostic_counters missing")
        if cp.get("kind") in ("hour", "day", "tail_before", "tail_after",
                              "tail_80640", "tail_80641"):
            missing = _C2_PUBLIC - cp.keys()
            if missing:
                gaps.append(f"checkpoint {index} {cp['kind']}: missing {sorted(missing)}")
            if not cp.get("epoch_sources") or not isinstance(cp.get("recent_cohorts"), dict):
                gaps.append(f"checkpoint {index} {cp['kind']}: epoch/cohort rows missing")
            elif set(cp["recent_cohorts"]) != set(REGISTRY):
                gaps.append(f"checkpoint {index} {cp['kind']}: cohort source missing")
            if len(cp.get("cursor_probes", [])) < 2:
                gaps.append(f"checkpoint {index} {cp['kind']}: cursor probes missing")
            if cp["kind"].startswith("tail") and cp.get("cohort_exact_from", 0) > 0 and \
                    "old_cohort_probe" not in cp:
                gaps.append(f"checkpoint {index} {cp['kind']}: old cohort probe missing")
        if cp.get("kind") == "hour" and type(cp.get("pruned_since_previous_hour")) is not int:
            gaps.append(f"checkpoint {index}: pruned_since_previous_hour missing")
        if cp.get("kind", "").startswith("probe_") and not cp.get("identity_samples"):
            gaps.append(f"checkpoint {index} {cp['kind']}: identity_samples missing")
        if cp.get("kind", "").startswith("probe_"):
            for sample in cp.get("identity_samples", []):
                if {"id", "category", "registered_seq", "expected", "observed"} - sample.keys():
                    gaps.append(f"checkpoint {index} {cp['kind']}: identity sample incomplete")
        if cp.get("kind") in ("probe_recent_before", "probe_recent_at"):
            if not isinstance(cp.get("recent_rows"), list):
                gaps.append(f"checkpoint {index} {cp['kind']}: recent_rows missing")
            for sample in cp.get("identity_samples", []):
                if {"expected_in_recent", "observed_in_recent", "observed_recent_marker_count"} - sample.keys():
                    gaps.append(f"checkpoint {index} {cp['kind']}: recent sample incomplete")
    scale = fixture_result.get("minute_batches", 10_080) * fixture_result.get("stride_minutes", 1)
    if scale >= 360:
        for before, at in _C2_PAIRS:
            b = [cp for cp in checkpoints if cp.get("kind") == before]
            a = [cp for cp in checkpoints if cp.get("kind") == at]
            if not b or len(b) != len(a):
                gaps.append(f"{before}/{at}: missing probe pair")
        name = fixture_result.get("name", "")
        for boundary in _C2_PROBE_CATEGORIES:
            required = set(_C2_PROBE_CATEGORIES[boundary])
            if "init_failed" in name and boundary in ("expire", "prune"):
                required.add("init_failed")
            if "burst" in name:
                required.update({"close": {"burst_finished"}, "expire": {"burst_probe"},
                                 "prune": {"burst_finished", "burst_probe"},
                                 "recent": set()}[boundary])
            for side in ("before", "at"):
                probe_kind = f"probe_{boundary}_{side}"
                observed = {sample.get("category") for cp in checkpoints
                            if cp.get("kind") == probe_kind
                            for sample in cp.get("identity_samples", [])}
                if not required <= observed:
                    gaps.append(f"{boundary} categories missing at {side}: {sorted(required - observed)}")
    return gaps


def _churn_evidence_violations(checkpoints) -> list[str]:
    violations = []
    for cp in checkpoints:
        counts = cp.get("admission_counts", {})
        if (counts.get("scheduled") != counts.get("registered", -1) + counts.get("rejected", -1)
                or cp.get("last_seq") != cp.get("N_total")
                or cp.get("registered_records") != cp.get("N_total")):
            violations.append(f"{cp.get('kind')}: admission or sequence equation")
        if sum(row.get("registered", 0) for row in cp.get("source_counts", {}).values()) != cp.get("N_total"):
            violations.append(f"{cp.get('kind')}: source total")
        if "epoch_sources" in cp:
            sources = cp["epoch_sources"]
            if (sum(row.get("registered_invocations", 0) for row in sources) != cp["N_total"]
                    or sum(row.get("live_invocations", 0) for row in sources) != cp["N_live"]):
                violations.append(f"{cp['kind']}: epoch total")
            for row in sources:
                total = row.get("registered_invocations")
                if total != cp.get("source_counts", {}).get(row.get("source"), {}).get("registered"):
                    violations.append(f"{cp['kind']}: epoch source registered count")
                if (total != row.get("frozen_invocations", 0) + row.get("live_invocations", 0)
                        or row.get("equations_hold") != {"connection": True, "lifecycle": True}
                        or sum(row.get("connection_counts", {}).values()) != total
                        or sum(row.get("lifecycle_counts", {}).values()) != total):
                    violations.append(f"{cp['kind']}: epoch source equation")
            for row in cp.get("recent_cohorts", {}).values():
                if row.get("classification") != "snapshot" or row.get("equations_hold") != {
                        "connection": True, "lifecycle": True}:
                    violations.append(f"{cp['kind']}: recent cohort equation")
            for probe in cp.get("cursor_probes", []):
                seqs = probe.get("entry_seqs", [])
                if (probe.get("classification") != "snapshot"
                        or seqs != sorted(set(seqs))
                        or any(seq <= probe.get("after_seq", -1) for seq in seqs)):
                    violations.append(f"{cp['kind']}: cursor sequence")
        for sample in cp.get("identity_samples", []):
            if sample.get("observed") != sample.get("expected"):
                violations.append(f"{cp['kind']}: identity {sample.get('category')}")
            if cp.get("kind") in ("probe_recent_before", "probe_recent_at"):
                expected_in_recent = sample.get("expected_in_recent")
                if (type(expected_in_recent) is not bool or
                        sample.get("observed_in_recent") != expected_in_recent or
                        sample.get("observed_recent_marker_count") != int(expected_in_recent)):
                    violations.append(f"{cp['kind']}: recent window {sample.get('category')}")
        if cp.get("old_cohort_probe") and cp["old_cohort_probe"].get("classification") != "cohort_expired":
            violations.append(f"{cp['kind']}: old cohort was not rejected")
        if cp.get("kind") == "probe_prune_at" and cp.get("prune_witness"):
            witness = cp["prune_witness"]
            if witness.get("pruned_count") is None or witness["pruned_count"] < 0:
                violations.append("prune resident count increased")
    return violations


def _highwater_measure(ledger, baseline_sizes, fixed):
    """Read the charged backing layout without a full budget or owned graph walk."""
    seen = {}
    layout = []
    actual = 0
    for name, obj in _capacity_containers(ledger):
        size = sys.getsizeof(obj)
        marker = id(obj)
        owner = seen.get(marker)
        if owner is None:
            seen[marker] = name
            actual += max(0, size - baseline_sizes.get(name, 0))
            owner = name
        # The owner path represents sharing; an identity replacement by itself
        # does not constitute a backing change.
        layout.append((name, size, owner))
    return ({"H_res": ledger._q_highwater, "H_job": ledger._job_highwater,
             "Q_4": ledger._q_charge(), "E": _fixture_budget_e(ledger, fixed),
             "B": ledger._limits["max_resident_bytes"], "Q_actual": actual},
            tuple(layout))


def _replay_step_digest(step):
    """Hash the ordered observation without retaining its two backing layouts."""
    canonical = json.dumps(step, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=16).digest()


def highwater_pair_gaps(highwater_checks, checkpoints) -> list[str]:
    """Reconcile selected high-water and first-violation checkpoint pairs."""
    gaps = []
    checks = highwater_checks.get("checks", [])
    events = highwater_checks.get("events")
    if events != len(checks):
        gaps.append(f"highwater events: {events} != checks {len(checks)}")
    resident = sum(ch.get("after", {}).get("H_res", -1) >
                   ch.get("before", {}).get("H_res", -1) for ch in checks)
    jobs = sum(ch.get("after", {}).get("H_job", -1) >
               ch.get("before", {}).get("H_job", -1) for ch in checks)
    both = sum(ch.get("after", {}).get("H_res", -1) >
               ch.get("before", {}).get("H_res", -1) and
               ch.get("after", {}).get("H_job", -1) >
               ch.get("before", {}).get("H_job", -1) for ch in checks)
    for key, actual in (("resident_events", resident), ("job_events", jobs),
                        ("both_events", both), ("backing_changes",
                         sum(bool(ch.get("backing_changed")) for ch in checks)),
                        ("preserved_pairs", sum(ch.get("preserved") is True for ch in checks))):
        if highwater_checks.get(key) != actual:
            gaps.append(f"highwater {key}: {highwater_checks.get(key)} != {actual}")
    if resident + jobs - both != len(checks):
        gaps.append("highwater category sum does not cover events")
    pairs = [cp for cp in checkpoints if cp.get("kind") in
             ("highwater_before", "highwater_after")]
    selected = [ch for ch in checks if ch.get("preserved")]
    if len(pairs) != 2 * len(selected):
        gaps.append(f"highwater pairs: {len(pairs)} checkpoint(s) for {len(selected)} selected event(s)")
    for index, ch in enumerate(selected):
        pair = pairs[2 * index:2 * index + 2]
        if len(pair) != 2 or [cp.get("kind") for cp in pair] != ["highwater_before", "highwater_after"]:
            gaps.append(f"highwater pair {index}: missing or out of order")
            continue
        for cp, expected_n in zip(pair, (ch.get("N_total_before"), ch.get("N_total_after"))):
            if (cp.get("received_at") != ch.get("received_at") or
                    cp.get("received_mono") != ch.get("received_mono") or
                    cp.get("minute_index") != ch.get("minute_index") or
                    cp.get("N_total") != expected_n):
                gaps.append(f"highwater pair {index}: time, minute, or N_total mismatch")
    violation = highwater_checks.get("first_violation")
    violation_pairs = [cp for cp in checkpoints if cp.get("kind") in
                       ("violation_before", "violation_after")]
    if violation is None or any(ch.get("register_seq") == violation.get("register_seq")
                                for ch in checks):
        if violation_pairs:
            gaps.append(f"violation pair: unexpected {len(violation_pairs)} checkpoint(s)")
        if violation is not None and violation.get("preserved") is not True:
            gaps.append("first violation: selected highwater event not preserved")
    else:
        if (len(violation_pairs) != 2 or
                [cp.get("kind") for cp in violation_pairs] !=
                ["violation_before", "violation_after"]):
            gaps.append("violation pair: missing or out of order")
        else:
            for cp, expected_n in zip(violation_pairs,
                                      (violation.get("N_total_before"),
                                       violation.get("N_total_after"))):
                if (cp.get("received_at") != violation.get("received_at") or
                        cp.get("received_mono") != violation.get("received_mono") or
                        cp.get("minute_index") != violation.get("minute_index") or
                        cp.get("N_total") != expected_n):
                    gaps.append("violation pair: time, minute, or N_total mismatch")
                    break
        if violation.get("preserved") is not True:
            gaps.append("first violation: not preserved")
    return gaps


def run_churn_fixture(name, *, limit=CAP, max_resident_bytes=62_914_560,
                      churn_minutes=10_080, churn_stride_minutes=1,
                      fixture_detail_divisor=1, _capacity_observer=None,
                      _stop_requested=None, _progress=None, _replay_hook=None,
                      _replay_expected=None, _replay_stop=None, _replay_selected=None):
    if name not in CHURN_NAMES or not 1 <= limit <= CAP:
        raise ValueError("unknown churn fixture or invalid limit")
    if min(churn_minutes, churn_stride_minutes, fixture_detail_divisor) < 1:
        raise ValueError("invalid churn scale")
    burst = name.startswith("churn_burst_")
    kind = "unicode128" if name.endswith("unicode128") else "ascii128" if name.endswith("ascii128") else "short_ascii"
    track = "burst" if burst else "most_unfinished" if "most_unfinished" in name else "most_finished"
    init_variant = name.startswith("churn_init_failed_")
    if burst and limit < 2:
        raise ValueError("burst needs a probe slot")
    target = min((2048 + fixture_detail_divisor - 1) // fixture_detail_divisor,
                 DETAIL_CAP, limit - 1) if burst else 0
    ledger = new_ledger(limit, max_resident_bytes)
    baseline = new_ledger(limit, max_resident_bytes)
    baseline_sizes = {path: sys.getsizeof(obj) for path, obj in _capacity_containers(baseline)}
    fixed = baseline.budget_state()["F_4"]
    replaying = _replay_expected is not None
    pass_start = time.perf_counter()
    sources, jobs, classes = Counter(), Counter(), Counter()
    source_attempts = Counter()
    attempts = init_failed_count = 0
    checkpoints = []
    probe_events = []
    probe_serial = 0
    witness_categories = set()
    witness_seq = {}
    witness_ids = {}
    witness_sources = {}
    witness_buckets = {}
    prune_before = {}
    previous_hour = None
    steps = []
    compared_steps = 0
    replay_mismatch = None
    pending = None
    highwater = {"events": 0, "resident_events": 0, "job_events": 0,
                 "both_events": 0, "backing_changes": 0, "preserved_pairs": 0,
                 "violations": 0, "first_violation": None, "unverified_events": 0,
                 "first_unverified": None, "checks": []}
    def add_checkpoint(kind, minute_index, received_at, *, probe_sample=None):
        nonlocal previous_hour
        cp = _churn_checkpoint(ledger, kind, minute_index, received_at, scheduled, auxiliary,
                               graph=not replaying,
                               sources=sources, attempts=attempts,
                               source_attempts=source_attempts, init_failed=init_failed_count)
        if probe_sample is not None:
            invocation_id, category, expected = probe_sample
            cp["identity_samples"] = [{"id": invocation_id, "category": category,
                                       "registered_seq": witness_seq[category],
                                       "expected": expected,
                                       "observed": ledger.identity_status(invocation_id)}]
            if kind in ("probe_recent_before", "probe_recent_at"):
                source = witness_sources[category]
                # W contains finished buckets in [floor(now)-60min, floor(now)); the old bucket leaves at the boundary.
                window_end = received_at // MINUTE * MINUTE
                window_start = window_end - 60 * MINUTE
                expected_in_recent = window_start <= witness_buckets[category] < window_end
                row = next((row for row in cp["recent_rows"]
                            if row["source"] == source and row["pair"] == PAIRS[0]), None)
                marker_count = row["collection"]["V"] if row is not None else None
                sample = cp["identity_samples"][0]
                sample["expected_in_recent"] = expected_in_recent
                sample["observed_in_recent"] = marker_count is not None and marker_count > 0
                sample["observed_recent_marker_count"] = marker_count
            if kind == "probe_prune_before":
                prune_before[invocation_id] = cp["N_res"]
            elif kind == "probe_prune_at":
                before_res = prune_before.get(invocation_id)
                cp["prune_witness"] = {"before_N_res": before_res, "after_N_res": cp["N_res"],
                                       "accepted_between": 0,
                                       "pruned_count": before_res - cp["N_res"] if before_res is not None else None,
                                       "as_of": received_at, "as_of_mono": received_at,
                                       "sample_ids": [invocation_id]}
                cp["prune_observed_through"] = (received_at if all(
                    tomb.prune_at > received_at for tomb in ledger._tombs.values()) else None)
        if kind == "hour":
            cp["pruned_since_previous_hour"] = (0 if previous_hour is None else
                previous_hour["N_res"] + cp["N_total"] - previous_hour["N_total"] - cp["N_res"])
            previous_hour = cp
        if not replaying:
            checkpoints.append(cp)
        if not replaying and _capacity_observer is not None and probe_sample is None:
            _capacity_observer(ledger, name, kind, minute_index, budget=cp["budget"])
        return cp

    def schedule_witness(category, invocation_id, wall):
        nonlocal probe_serial
        if category in witness_categories:
            return
        witness_categories.add(category)
        witness_seq[category] = ledger._records[invocation_id]["seq"]
        witness_ids[category] = invocation_id
        witness_sources[category] = ledger._records[invocation_id]["source"]
        witness_buckets[category] = wall // MINUTE * MINUTE
        events = []
        if category in ("finished", "burst_finished"):
            close_at = (wall // MINUTE + 1) * MINUTE + 70 * MINUTE
            events.extend((("close", close_at, "live", "tombstoned"),
                           ("prune", close_at + 120 * MINUTE,
                            "tombstoned", "expired_or_untracked")))
            if category == "finished":
                events.append(("recent", (wall // MINUTE + 61) * MINUTE,
                               "live", "live"))
        else:
            expire_at = wall + 240 * MINUTE
            events.extend((("expire", expire_at, "live", "tombstoned"),
                           ("prune", expire_at + 120 * MINUTE,
                            "tombstoned", "expired_or_untracked")))
        for boundary, at, before_state, at_state in events:
            for point, when, expected in (("before", at - 1, before_state),
                                          ("at", at, at_state)):
                heapq.heappush(probe_events, (when, probe_serial,
                                              f"probe_{boundary}_{point}",
                                              invocation_id, category, expected))
                probe_serial += 1

    def flush_probes(through, minute_index):
        while probe_events and probe_events[0][0] <= through:
            when, _, probe_kind, invocation_id, category, expected = heapq.heappop(probe_events)
            add_checkpoint(probe_kind, minute_index, when,
                           probe_sample=(invocation_id, category, expected))
    auxiliary = scheduled = max_details = 0
    def register_checked(invocation_id, source, wall, minute_index, job_id=None, *, auxiliary_call=False):
        nonlocal pending, replay_mismatch, attempts
        seq = compared_steps if replaying else len(steps)
        if replaying and _replay_hook is not None:
            _replay_hook(seq, ledger, wall)
        before, before_layout = _highwater_measure(ledger, baseline_sizes, fixed)
        before_counts = tuple(ledger._health[key] for key in ("N_total", "N_live", "N_tomb", "N_res"))
        selected_kind = _replay_selected.get(seq) if replaying else None
        pre_cp = pre_point = None
        if selected_kind:
            before_kind = f"{selected_kind}_before"
            pre_cp = _churn_checkpoint(ledger, before_kind, minute_index, wall,
                                       scheduled, auxiliary, readonly=True,
                                       sources=sources, attempts=attempts,
                                       source_attempts=source_attempts,
                                       init_failed=init_failed_count)
            pre_point = _capacity_point(ledger, baseline_sizes, name,
                                        before_kind, minute_index, budget=pre_cp["budget"])
            if (before, before_layout) != _highwater_measure(ledger, baseline_sizes, fixed):
                replay_mismatch = replay_mismatch or f"replay seq {seq}: pre-capture changed trajectory"
        attempts += 1
        source_attempts[source] += 1
        result = _register_fixture(ledger, invocation_id, source, wall, job_id)
        pending = {"seq": seq, "input": (invocation_id, source, wall, minute_index, job_id,
                                           auxiliary_call), "classification": result["classification"],
                   "before": before, "before_layout": before_layout,
                   "before_counts": before_counts, "pre_cp": pre_cp, "pre_point": pre_point}
        return result

    def finish_step():
        nonlocal pending, compared_steps, replay_mismatch
        p = pending
        pending = None
        seq = p["seq"]
        after, after_layout = _highwater_measure(ledger, baseline_sizes, fixed)
        after_counts = tuple(ledger._health[key] for key in ("N_total", "N_live", "N_tomb", "N_res"))
        before, before_layout = p["before"], p["before_layout"]
        res_up = after["H_res"] > before["H_res"]
        job_up = after["H_job"] > before["H_job"]
        backing_changed = before_layout != after_layout or before["Q_actual"] != after["Q_actual"]
        bad = any(side["Q_actual"] > side["Q_4"] or side["E"] > side["B"]
                  for side in (before, after))
        event = p["classification"] == "registered" and (res_up or job_up)
        preserved = event and (backing_changed or job_up or bad)
        step = {"input": p["input"], "classification": p["classification"],
                "before_counts": p["before_counts"], "after_counts": after_counts,
                "before": before, "after": after, "before_layout": before_layout,
                "after_layout": after_layout, "event": event, "preserved": preserved}
        if replaying:
            if seq >= len(_replay_expected) or _replay_step_digest(step) != _replay_expected[seq]:
                replay_mismatch = replay_mismatch or f"replay seq {seq}: input or state differs"
            compared_steps += 1
            if p["pre_cp"] is not None:
                before_kind = p["pre_cp"]["kind"]
                after_kind = before_kind.removesuffix("_before") + "_after"
                post_cp = _churn_checkpoint(ledger, after_kind, p["input"][3],
                                            p["input"][2], scheduled, auxiliary, readonly=True,
                                            sources=sources, attempts=attempts,
                                            source_attempts=source_attempts,
                                            init_failed=init_failed_count)
                if (after, after_layout) != _highwater_measure(ledger, baseline_sizes, fixed):
                    replay_mismatch = replay_mismatch or f"replay seq {seq}: post-capture changed trajectory"
                checkpoints.extend((p["pre_cp"], post_cp))
                if _capacity_observer is not None:
                    ledger._gate_capacity_pre_point = p["pre_point"]
                    try:
                        _capacity_observer(ledger, name, before_kind, p["input"][3],
                                           budget=p["pre_cp"]["budget"])
                    finally:
                        del ledger._gate_capacity_pre_point
                    _capacity_observer(ledger, name, after_kind, p["input"][3],
                                       budget=post_cp["budget"])
            return replay_mismatch is not None or seq == _replay_stop
        steps.append(_replay_step_digest(step))
        if not event:
            if bad:
                highwater["violations"] += 1
                if highwater["first_violation"] is None:
                    highwater["first_violation"] = {
                        "register_seq": seq, "minute_index": p["input"][3],
                        "received_at": p["input"][2], "received_mono": p["input"][2],
                        "N_total_before": p["before_counts"][0],
                        "N_total_after": after_counts[0], "before": before,
                        "after": after, "preserved": True}
            return False
        minute_index, wall = p["input"][3], p["input"][2]
        check = {"register_seq": seq, "minute_index": minute_index,
                 "received_at": wall, "received_mono": wall,
                 "N_total_before": p["before_counts"][0], "N_total_after": after_counts[0],
                 "before": before, "after": after, "backing_changed": backing_changed,
                 "preserved": preserved}
        highwater["checks"].append(check)
        highwater["events"] += 1
        highwater["resident_events"] += int(res_up)
        highwater["job_events"] += int(job_up)
        highwater["both_events"] += int(res_up and job_up)
        highwater["backing_changes"] += int(backing_changed)
        if bad:
            highwater["violations"] += 1
            if highwater["first_violation"] is None:
                highwater["first_violation"] = check
        return False

    def replay_result():
        if failure and failure.get("phase") == "budget":
            stop_reason, mismatch = "budget", None
        elif replay_mismatch:
            stop_reason, mismatch = "mismatch", replay_mismatch
        elif compared_steps - 1 == _replay_stop:
            stop_reason, mismatch = "last_selected", None
        else:
            stop_reason = "mismatch"
            mismatch = ("replay: burst ended early" if burst and scheduled == 0 else
                        "replay: stopped before selected seq")
        return {"compared_steps": compared_steps, "mismatch": mismatch,
                "stop_reason": stop_reason,
                "stopped_after_seq": compared_steps - 1,
                "seconds": time.perf_counter() - pass_start, "checkpoints": checkpoints}

    def publish_replay_pairs(replayed):
        pairs = iter(replayed["checkpoints"])
        checks_by_seq = {ch["register_seq"]: ch for ch in highwater["checks"]}
        for seq in selected_seqs:
            pre_cp, post_cp = next(pairs, None), next(pairs, None)
            if pre_cp is None or post_cp is None:
                if seq in checks_by_seq:
                    highwater["unverified_events"] += 1
                    if highwater["first_unverified"] is None:
                        highwater["first_unverified"] = checks_by_seq[seq]
                continue
            checkpoints.extend((pre_cp, post_cp))
            if seq in checks_by_seq:
                highwater["preserved_pairs"] += 1

    failure = None
    schedule_start = T
    if burst:
        for i in range(target):
            if _stop_requested is not None and _stop_requested():
                failure = {"phase": "budget"}
                break
            invocation_id = _fixture_id(i, kind, "b")
            r = register_checked(invocation_id, SOURCE, T, -1, auxiliary_call=True)
            if r["classification"] != "registered":
                failure = {"phase": "burst", "index": i, "classification": r["classification"]}
                if finish_step() and replaying:
                    return replay_result()
                break
            auxiliary += 1
            sources[SOURCE] += 1
            if finish_step() and replaying:
                return replay_result()
            a = _link_fixture(ledger, invocation_id, SOURCE, T)
            b = _finish_fixture(ledger, invocation_id, SOURCE, T) if a["classification"] == "linked" else a
            if b["classification"] != "finalized":
                failure = {"phase": "burst_finish", "index": i, "classification": b["classification"]}
            elif i == 0:
                schedule_witness("burst_finished", invocation_id, T)
            if failure:
                break
        if replaying and failure:
            return replay_result()
        max_details = ledger._health["retained_details"]
        add_checkpoint("detail_open", -1, T)
        if failure is None:
            probe = register_checked(_fixture_id(target, kind, "b"), SOURCE, T,
                                     -1, auxiliary_call=True)
            if probe["classification"] == "registered":
                auxiliary += 1
                sources[SOURCE] += 1
            else:
                failure = {"phase": "probe", "classification": probe["classification"]}
            if finish_step() and replaying:
                return replay_result()
            if failure is None:
                schedule_witness("burst_probe", _fixture_id(target, kind, "b"), T)
                add_checkpoint("highwater", -1, T)
        release_at = T + 71 * MINUTE
        flush_probes(release_at, -1)
        add_checkpoint("detail_released", -1, release_at)
        schedule_start = T + 7 * 60 * MINUTE
        flush_probes(schedule_start, -1)
        ledger.aggregation_snapshot(as_of=schedule_start, as_of_mono=schedule_start)
    offsets = (0, 7_500_000, 15_000_000, 22_500_000, 30_000_000,
               37_500_000, 45_000_000, 52_500_000)
    source_order = ("investing",) * 6 + ("bs", "citi")
    job_by_source = {"investing": "task_investing", "bs": "task_bs", "citi": "task_citi"}
    tail = None
    last_observed_q = ledger._q_charge()
    for minute in range(churn_minutes):
        if failure:
            break
        if _stop_requested is not None and _stop_requested():
            failure = {"phase": "budget", "minute": minute}
            break
        if _progress is not None and minute and minute % 60 == 0:
            _progress(minute)
        base = schedule_start + minute * churn_stride_minutes * MINUTE
        flush_probes(base, minute)
        ledger.aggregation_snapshot(as_of=base, as_of_mono=base)
        for offset_index, (offset, source) in enumerate(zip(offsets, source_order)):
            if _stop_requested is not None and _stop_requested():
                failure = {"phase": "budget", "minute": minute}
                break
            wall = base + offset
            flush_probes(wall, minute)
            invocation_id = _fixture_id(scheduled, kind, "c")
            job = job_by_source[source]
            r = register_checked(invocation_id, source, wall, minute, job)
            classes[r["classification"]] += 1
            if r["classification"] != "registered":
                failure = {"minute": minute, "offset_index": offset_index,
                           "classification": r["classification"]}
                if finish_step() and replaying:
                    return replay_result()
                break
            sources[source] += 1
            jobs[(source, job)] += 1
            position = scheduled % 20
            scheduled += 1
            if finish_step() and replaying:
                return replay_result()
            finish_count = 4 if track == "most_unfinished" else 16
            linked_count = 4 if track == "most_unfinished" else 2
            if position < finish_count:
                a = _link_fixture(ledger, invocation_id, source, wall)
                recent_witness = position == 0 and "finished" not in witness_categories
                b = (_finish_fixture(ledger, invocation_id, source, wall,
                                     summary=RECENT_WITNESS_SUMMARY if recent_witness else SUMMARY)
                     if a["classification"] == "linked" else a)
                if b["classification"] != "finalized":
                    failure = {"minute": minute, "phase": "finish", "classification": b["classification"]}
                elif position == 0:
                    schedule_witness("finished", invocation_id, wall)
            elif position < finish_count + linked_count:
                a = _link_fixture(ledger, invocation_id, source, wall)
                if a["classification"] != "linked":
                    failure = {"minute": minute, "phase": "link", "classification": a["classification"]}
                elif position == finish_count:
                    schedule_witness("linked", invocation_id, wall)
            elif init_variant and position == finish_count + linked_count:
                a = _init_failed_fixture(ledger, invocation_id, wall)
                if a["classification"] != "report_init_failed":
                    failure = {"minute": minute, "phase": "init_failed", "classification": a["classification"]}
                else:
                    init_failed_count += 1
                    schedule_witness("init_failed", invocation_id, wall)
            elif position == finish_count + linked_count + int(init_variant):
                schedule_witness("unbound", invocation_id, wall)
            if failure:
                break
        if failure is None and ledger._q_charge() > last_observed_q:
            add_checkpoint("highwater", minute, base + offsets[-1])
            last_observed_q = ledger._q_charge()
        first_hour = minute * churn_stride_minutes // 60 + 1
        last_hour = (minute + 1) * churn_stride_minutes // 60
        for hour_index in range(first_hour, last_hour + 1):
            boundary = schedule_start + hour_index * 60 * MINUTE
            flush_probes(boundary, minute)
            add_checkpoint("hour", minute, boundary)
            if hour_index % 24 == 0:
                add_checkpoint("day", minute, boundary)
        max_details = max(max_details, ledger._health["retained_details"])
    if failure is None:
        tail_time = schedule_start + churn_minutes * churn_stride_minutes * MINUTE
        flush_probes(tail_time, churn_minutes)
        full = churn_minutes == 10_080 and churn_stride_minutes == 1
        add_checkpoint("tail_80640" if full else "tail_before", churn_minutes, tail_time)
        tail = register_checked(_fixture_id(scheduled, kind, "c"), "investing",
                                tail_time, churn_minutes, job_by_source["investing"])
        classes[tail["classification"]] += 1
        if tail["classification"] == "registered":
            scheduled += 1
            sources["investing"] += 1
            jobs[("investing", job_by_source["investing"])] += 1
        else:
            failure = {"phase": "tail", "classification": tail["classification"]}
        if finish_step() and replaying:
            return replay_result()
        tail_cp = add_checkpoint("tail_80641" if full else "tail_after", churn_minutes, tail_time)
        if churn_minutes * churn_stride_minutes >= 360:
            tail_cp["identity_samples"] = [
                {"id": witness_ids[category],
                 "category": category, "registered_seq": witness_seq[category],
                 "expected": "expired_or_untracked", "observed": ledger.identity_status(
                     witness_ids[category])}
                for category in sorted(witness_categories)]
    if replaying:
        return replay_result()
    pass1_seconds = time.perf_counter() - pass_start
    selected_kinds = {ch["register_seq"]: "highwater" for ch in highwater["checks"]
                      if ch["preserved"]}
    violation = highwater["first_violation"]
    if violation is not None and violation["register_seq"] not in selected_kinds:
        selected_kinds[violation["register_seq"]] = "violation"
    selected_seqs = sorted(selected_kinds)
    replay = {"performed": False, "stopped_after_seq": None, "compared_steps": 0,
              "mismatch": None, "stop_reason": None,
              "pass1_seconds": pass1_seconds, "pass2_seconds": 0.0}
    if selected_seqs and failure is None:
        replayed = run_churn_fixture(
            name, limit=limit, max_resident_bytes=max_resident_bytes,
            churn_minutes=churn_minutes, churn_stride_minutes=churn_stride_minutes,
            fixture_detail_divisor=fixture_detail_divisor, _capacity_observer=_capacity_observer,
            _stop_requested=_stop_requested, _progress=_progress, _replay_hook=_replay_hook,
            _replay_expected=steps, _replay_stop=selected_seqs[-1],
            _replay_selected=selected_kinds)
        publish_replay_pairs(replayed)
        replay.update(performed=True, stopped_after_seq=replayed["stopped_after_seq"],
                      compared_steps=replayed["compared_steps"], mismatch=replayed["mismatch"],
                      stop_reason=replayed["stop_reason"],
                      pass2_seconds=replayed["seconds"])
        if replayed["stop_reason"] == "budget":
            failure = {"phase": "budget", "pass": 2}
    highwater["replay"] = replay
    for index, cp in enumerate(checkpoints):
        cp["checkpoint_id"] = f"{name}:{index}"
        cp["capacity_ref"] = cp["checkpoint_id"]
    evidence_result = {"name": name, "checkpoints": checkpoints, "minute_batches": churn_minutes,
                       "stride_minutes": churn_stride_minutes}
    evidence_gaps = churn_evidence_gaps(evidence_result) if failure is None else []
    evidence_violations = (_churn_evidence_violations(checkpoints)
                           if failure is None and replay["mismatch"] is None else [])
    checkpoint_gaps = churn_checkpoint_gaps(checkpoints, minute_batches=churn_minutes,
                                            stride_minutes=churn_stride_minutes)
    highwater_gaps = highwater_pair_gaps(highwater, checkpoints)
    unknown_ownership = any(cp["budget"]["G"] is None or cp["budget"]["unknown_types"]
                            for cp in checkpoints)
    valid = (failure is None and replay["mismatch"] is None and
             (not selected_seqs or replay["performed"]) and
             not checkpoint_gaps and not highwater_gaps and not evidence_gaps and
             not evidence_violations and
             not highwater["violations"] and not highwater["unverified_events"] and
             scheduled == 8 * churn_minutes + 1 and
             (not burst or max_details >= target) and
             ledger._health["registered_records"] == ledger._health["N_total"] == ledger._seq.total and
             all(cp["N_res"] == cp["N_live"] + cp["N_tomb"] <= limit and
                 cp["N_total"] == cp["scheduled_registered"] + cp["auxiliary_registered"] and
                 (churn_stride_minutes != 1 or cp["N_res"] <= 2880 + auxiliary) and
                 not cp["admission_stopped"] and cp["budget"]["G"] is not None and
                 cp["budget"]["G"] <= cp["budget"]["E"] <= cp["budget"]["B"]
                 for cp in checkpoints))
    reason = (None if valid else "budget" if failure and failure.get("phase") == "budget"
              else "churn admission or checkpoint invariant failed")
    if checkpoint_gaps:
        reason = (f"{reason}; " if reason else "") + "checkpoint gaps: " + "; ".join(checkpoint_gaps)
    if highwater_gaps or highwater["unverified_events"]:
        reason = (f"{reason}; " if reason else "") + "highwater gaps: " + "; ".join(
            highwater_gaps or [f"{highwater['unverified_events']} unverified event(s)"])
    if highwater["violations"]:
        reason = (f"{reason}; " if reason else "") + "highwater Q_actual or E limit exceeded"
    if evidence_gaps:
        reason = (f"{reason}; " if reason else "") + "evidence gaps: " + "; ".join(evidence_gaps[:8])
    if evidence_violations:
        reason = (f"{reason}; " if reason else "") + "evidence violations: " + "; ".join(evidence_violations[:8])
    if unknown_ownership:
        reason = (f"{reason}; " if reason else "") + "owned graph or baseline unverified"
    if replay["mismatch"]:
        reason = (f"{reason}; " if reason else "") + replay["mismatch"]
    status = ("PASS" if valid else "UNVERIFIED" if checkpoint_gaps or evidence_gaps or
              highwater_gaps or highwater["unverified_events"] or
              replay["mismatch"] or (failure and failure.get("phase") == "budget") else "FAIL")
    if highwater["violations"]:
        status = "FAIL"
    elif evidence_violations:
        status = "FAIL"
    elif replay["mismatch"] or unknown_ownership:
        status = "UNVERIFIED"
    return {"name": name, "status": status, "id_kind": kind,
            "state_track": track + ("_init_failed" if init_variant else ""),
            "job_key_counts": _job_counts(jobs), "id_utf8_bytes": len(_fixture_id(0, kind, "c").encode()),
            "id_getsizeof_bytes": sys.getsizeof(_fixture_id(0, kind, "c")),
            "minute_batches": churn_minutes, "stride_minutes": churn_stride_minutes,
            "scheduled_target": 8 * churn_minutes + 1, "scheduled_registered": scheduled,
            "auxiliary_registered": auxiliary, "N_total_at_tail": ledger._health["N_total"] if tail else None,
            "source_registered": dict(sources), "requested_detail_target": 2048 if burst else 0,
            "effective_detail_target": target, "max_retained_details_observed": max_details,
            "tail_classification": tail["classification"] if tail else None,
            "checkpoints": checkpoints, "highwater_checks": highwater,
            "evidence_gaps": evidence_gaps,
            "classification_counts": dict(classes),
            "first_failure": failure, "reason": reason}


def _capacity_containers(ledger):
    items = [("records", ledger._records), ("tombs", ledger._tombs),
             ("seq.positions", ledger._seq.positions), ("owners", ledger._owners),
             ("owned_ids", ledger._owned_ids), ("previous_job", ledger._previous_job),
             ("open_seq", ledger._open_seq), ("recent_buckets", ledger._recent_buckets)]
    for name in ("close", "overdue", "expiry", "prune"):
        index = getattr(ledger, "_" + name + "_index")
        for field in ("data", "seqs", "slots", "free"):
            items.append((name + "." + field, getattr(index, field)))
    for source, index in ledger._cohort_index.items():
        items.extend((("cohort." + source + ".blocks", index.blocks),
                      ("cohort." + source + ".maxes", index.maxes)))
        items.extend((f"cohort.{source}.block.{i}", block) for i, block in enumerate(index.blocks))
    for key, value in ledger._recent_buckets.items():
        if isinstance(value, (list, dict, set)):
            items.append((f"recent.{key}", value))
    for i, (key, summary) in enumerate(ledger._previous_job.items()):
        items.append((f"previous_job.key.{i}", key))
        items.append((f"previous_job.summary.{i}", summary))
    return items


def _capacity_point(ledger, baseline_sizes, fixture_name, kind, minute_index, *, budget=None):
    if budget is None:
        budget = _budget_checkpoint(ledger)
    seen = set()
    backings = []
    actual = 0
    for name, obj in _capacity_containers(ledger):
        marker = id(obj)
        if marker in seen:
            continue
        seen.add(marker)
        size = sys.getsizeof(obj)
        # Initial fixed allocations belong to F_4. Dynamic growth belongs to
        # Q_4, including dict dummy slots that persist after deletion.
        fixed = baseline_sizes.get(name, 0)
        attributed = max(0, size - fixed)
        actual += attributed
        backings.append({"name": name, "object_id": marker, "getsizeof_bytes": size,
                         "header_accounted_elsewhere_bytes": fixed,
                         "Q_attributed_bytes": attributed,
                         "charged_limit_bytes": budget["Q_4"],
                         "includes_deleted_dummy": isinstance(obj, dict)})
    h = ledger._health
    jobs = len(ledger._previous_job)
    covered = (actual <= budget["Q_4"] and budget["G"] is not None
               and budget["G"] <= budget["E"] <= budget["B"])
    return {"fixture_name": fixture_name, "kind": kind, "minute_index": minute_index,
            "N_total": h["N_total"], "N_res": h["N_res"],
            "H_res": ledger._q_highwater, "H_job": ledger._job_highwater,
            "Q_4_bytes": budget["Q_4"], "Q_actual_bytes": actual,
            "G_bytes": budget["G"], "E_bytes": budget["E"], "B_bytes": budget["B"],
            "capacity_covered": covered, "resident_covered": budget["G"] is not None and
            budget["G"] <= budget["E"] <= budget["B"],
            "container_backings": backings}


def _capacity_proof(points, churn_fixtures=(), required_churn_names=()):
    qcap = _budget_q(CAP) + 512 * CAP + 1536 * 12
    observed = max((p["Q_4_bytes"] for p in points), default=None)
    actual = max((p["Q_actual_bytes"] for p in points), default=None)
    required = Counter()
    for fixture in churn_fixtures:
        highwater = fixture["highwater_checks"]
        selected = [(check, "highwater") for check in highwater["checks"] if check["preserved"]]
        violation = highwater["first_violation"]
        if violation is not None and violation["register_seq"] not in {
                check["register_seq"] for check, _ in selected}:
            selected.append((violation, "violation"))
        for check, kind in selected:
            for side, n_key in (("before", "N_total_before"), ("after", "N_total_after")):
                required[(fixture["name"], f"{kind}_{side}", check["minute_index"],
                          check[n_key])] += 1
    seen = Counter((point["fixture_name"], point["kind"], point["minute_index"],
                    point["N_total"]) for point in points)
    missing = required - seen
    unrun = set(required_churn_names) - {fixture["name"] for fixture in churn_fixtures}
    interrupted = any((fixture["first_failure"] or {}).get("phase") == "budget"
                      for fixture in churn_fixtures)
    unknown_ownership = any(point["G_bytes"] is None for point in points)
    observed_failure = bool(points) and (
        any(point["Q_actual_bytes"] > point["Q_4_bytes"] or
            point["E_bytes"] > point["B_bytes"] or
            (point["G_bytes"] is not None and point["G_bytes"] > point["E_bytes"]) or
            point["H_job"] > 12 for point in points)
        or observed > qcap)
    status = ("FAIL" if observed_failure else "UNVERIFIED" if
              not points or missing or unrun or interrupted or unknown_ownership else "PASS")
    reasons = []
    if observed_failure:
        reasons.append("capacity witness exceeded")
    if not points:
        reasons.append("capacity witness missing")
    if missing:
        reasons.append(f"{sum(missing.values())} required capacity observation(s) missing")
    if unrun:
        reasons.append("churn fixture(s) not run: " + ", ".join(sorted(unrun)))
    if interrupted:
        reasons.append("churn budget interrupted capacity evidence")
    if unknown_ownership:
        reasons.append("owned graph unverified")
    return {"status": status, "formula": "_budget_q(131072)+512*131072+1536*12",
            "job_key_limit": 12, "H_res": max((p["N_res"] for p in points), default=None),
            "H_job": max((p["H_job"] for p in points), default=None),
            "Q_cap_bytes": qcap, "Q_obs_bytes": observed,
            "Q_actual_max_bytes": actual, "checkpoints": points,
            "unknown_ownership": ["owned_graph"] if unknown_ownership else [],
            "reason": "; ".join(reasons) or None}


def _calibration_eta(observations, budget_seconds):
    required = {"legacy_repeat", "pressure", "churn_normal", "churn_burst",
                "capacity", "resident", "adapter"}
    available = {item["class_name"] for item in observations
                 if item["completed_units"] > 0 and item["projected_full_seconds"] > 0}
    result = {"seconds": None, "uncertainty_seconds": None, "basis": "none",
              "budget_seconds": budget_seconds, "decision": "unknown", "assumptions": []}
    if not required <= available:
        return result
    seconds = sum(item["projected_full_seconds"] for item in observations)
    # The reduced fixture's rate is a rough extrapolation. Capacity calls are
    # also inside pressure/churn elapsed time, so retaining them is conservative.
    uncertainty = seconds * 0.5
    result.update(seconds=seconds, uncertainty_seconds=uncertainty,
                  basis="calibration",
                  decision="agreement_required" if seconds > 14_400 else "within_budget",
                  assumptions=["linear projection of each completed small-run fixture to its full unit count",
                               "churn pass 1 scales with scheduled calls; pass 2 uses the observed selected prefix and resident growth, with quadratic allowance for larger graph captures",
                               "capacity observation time is counted separately and within pressure/churn elapsed time",
                               "50% model uncertainty; same host and runtime are assumed"])
    return result


def run_gate(*, limit=CAP, samples=1000, warmup=100, quick=False,
             budget_seconds=14400, clock=time.perf_counter_ns, wall=time.monotonic,
             progress=sys.stderr, rows=None, max_resident_bytes=None,
             pressure_names=None, churn_names=None, churn_minutes=10_080,
             churn_stride_minutes=1, fixture_detail_divisor=1, calibration=False,
             budget_agreement_ref=None):
    """Measure the plan; selected scenario rows form a small, non-accepting run."""
    if not 1 <= limit <= CAP or samples < 1 or warmup < 0 or budget_seconds < 0:
        raise ValueError("invalid gate parameter")
    if quick and calibration or min(churn_minutes, churn_stride_minutes, fixture_detail_divisor) < 1:
        raise ValueError("invalid mode or fixture scale")
    default_b = 62_914_560
    resident_b = default_b if max_resident_bytes is None else max_resident_bytes
    fixed = new_ledger(1).budget_state()["F_4"]
    if type(resident_b) is not int or not fixed <= resident_b <= default_b:
        raise ValueError("invalid resident budget")
    chosen = None if rows is None else tuple(rows)
    if chosen is not None and (not chosen or len(chosen) != len(set(chosen))):
        raise ValueError("rows must be distinct scenario names")
    selected_pressure = None if pressure_names is None else tuple(pressure_names)
    selected_churn = None if churn_names is None else tuple(churn_names)
    for selected, available in ((selected_pressure, PRESSURE_NAMES + PRESSURE_AUX),
                                (selected_churn, CHURN_NAMES)):
        if selected is not None and (not selected or len(selected) != len(set(selected))
                                     or set(selected) - set(available)):
            raise ValueError("invalid fixture selection")
    selected_any = any(x is not None for x in (chosen, selected_pressure, selected_churn))
    if quick and (limit, samples, warmup) == (CAP, 1000, 100):
        limit, samples, warmup = 96, 8, 2
    if quick and (churn_minutes, churn_stride_minutes, fixture_detail_divisor) == (10_080, 1, 1):
        churn_minutes, churn_stride_minutes, fixture_detail_divisor = 25, 60, 256
    default_scale = ((limit, samples, warmup, resident_b, churn_minutes, churn_stride_minutes,
                      fixture_detail_divisor) == (CAP, 1000, 100, default_b, 10_080, 1, 1))
    if selected_any and default_scale and not quick and not calibration:
        raise ValueError("selected full run requires a reduced scale")
    if calibration and default_scale and not selected_any:
        raise ValueError("calibration requires a selection or reduced scale")
    mode = "quick" if quick else "calibration" if calibration else "full" if default_scale and not selected_any else "full_small"
    if mode == "full" and budget_seconds > 14_400 and (
            not isinstance(budget_agreement_ref, str) or not budget_agreement_ref.strip()):
        raise ValueError("extended full budget requires agreement reference")
    budget_token = _gate_resident_budget.set(resident_b)
    started = wall()
    deadline = started + budget_seconds
    calibration_observations = []

    def note_calibration(class_name, fixture_name, completed_units, projected_full_units,
                         elapsed_seconds, *, prepare_seconds=0.0, clone_seconds=0.0,
                         call_seconds=0.0, gc_seconds=0.0, owned_graph_seconds=0.0,
                         projected_override=None, replay_projection=None):
        if not calibration or completed_units <= 0:
            return
        projected_full_units = max(completed_units, projected_full_units)
        projected = (projected_override if projected_override is not None else
                     elapsed_seconds * projected_full_units / completed_units)
        calibration_observations.append({"class_name": class_name, "fixture_name": fixture_name,
                                         "completed_units": completed_units,
                                         "elapsed_seconds": elapsed_seconds,
                                         "prepare_seconds": prepare_seconds,
                                         "clone_seconds": clone_seconds,
                                         "call_seconds": call_seconds,
                                         "gc_seconds": gc_seconds,
                                         "owned_graph_seconds": owned_graph_seconds,
                                         "replay_projection": replay_projection,
                                         "projected_full_units": projected_full_units,
                                         "projected_full_seconds": projected})
    env = environment()
    env["load_before"] = _load_average()
    audit = audit_record_access()
    prereq = {"python_3_13": sys.version_info[:2] == (3, 13),
              "hash_seed_zero": os.environ.get("PYTHONHASHSEED") == "0",
              "no_bytecode": sys.dont_write_bytecode,
              "record_access_audit": not audit,
              "min_4_cores": (os.cpu_count() or 0) >= 4,
              "min_8gib_ram": (env["ram_bytes"] or 0) >= 8 * 1024**3}
    specs = _scenario_specs(limit, samples, warmup)
    names = [item[0] for item in specs] + ["record_baseline",
        "resident_unfinished", "resident_2048_details", "resident_released_identity",
        "resident_capacity_stop"]
    adapter_names = [f"adapter_{source}_{stage}" for source in ("investing", "bs", "citi")
                     for stage in ("link", "finish")] + ["adapter_bank_to_finish"]
    names += adapter_names
    if chosen is not None:
        available = {item[0] for item in specs}
        if set(chosen) - available:
            raise ValueError("rows must name scenario measurements")
        specs = [item for item in specs if item[0] in chosen]
        names = [item[0] for item in specs]
    elif selected_any:
        specs = []
        names = []
    pressure_plan = (PRESSURE_NAMES + PRESSURE_AUX if not selected_any else
                     selected_pressure or ())
    churn_plan = CHURN_NAMES if not selected_any else selected_churn or ()
    rows = []
    aborted = "prerequisite" if mode == "full" and not all(prereq.values()) else None
    for name, provider, plan, classification, d, k, validate, exception, full_cohort in specs:
        if aborted is not None:
            break
        if wall() >= deadline:
            aborted = "budget"
            break
        n = min(samples, 100) if name == "mass_overdue" and mode == "full" else samples
        if name in ("reverse_cohort_narrow", "reverse_cohort_empty"):
            target_count = 2*len(REGISTRY) + warmup + 2*max(n, len(REGISTRY))
        else:
            target_count = 2 + warmup + 2*n
        print(f"start {name} 0/{target_count} gc=setup prepare=0s clone=0s call=0s eta=unknown", file=progress)
        calibration_start = wall() if calibration else None
        row = _measure_row(name, provider, plan=plan, expected=classification, expected_d=d,
                           expected_k=k, samples=n, warmup=warmup, clock=clock,
                           wall=wall, deadline=deadline, full_cohort=full_cohort,
                           sample_exception=exception if mode == "full" else None,
                           validate=validate, progress=progress)
        for field in ("cursor", "order_probe"):
            if hasattr(provider, field):
                row[field] = getattr(provider, field).copy()
        rows.append(row)
        if calibration:
            note_calibration("legacy_repeat", name, row["sample_checks"]["checked"],
                             2 + 100 + 2 * (100 if name == "mass_overdue" else 1000),
                             wall() - calibration_start,
                             prepare_seconds=row.get("prepare_seconds", 0.0),
                             clone_seconds=row.get("clone_seconds", 0.0),
                             call_seconds=row.get("call_seconds", 0.0))
        print(f"end {name} {row['status']} {row['sample_checks']['checked']}/{target_count} "
              f"gc=done prepare={row['prepare_seconds']:.3f}s clone={row['clone_seconds']:.3f}s "
              f"call={row['call_seconds']:.3f}s eta=0s", file=progress)
        if row.get("aborted"):
            aborted = "budget"
            break
    if chosen is None and aborted is None and wall() < deadline:
        name = "record_baseline"
        print(f"start {name}", file=progress)
        try:
            ledger = fill(limit, finalized=1)
            durations = []
            for _ in range(samples):
                before = clock()
                rec = ledger.record(rid(0))
                durations.append(clock() - before)
                if rec is None:
                    raise RuntimeError("record baseline missing")
            row = _empty_row(name, "baseline")
            row.update({"slots": limit, "timed_calls": samples,
                        "timing": {"gc_default": _distribution(durations, Counter({"record": samples}))}})
        except Exception as exc:
            row = _empty_row(name, "baseline")
            row["reason"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
        print(f"end {name} {row['status']}", file=progress)
    if chosen is None and aborted is None:
        for name, detail_count, release, reject in (
            ("resident_unfinished", 0, False, False),
            ("resident_2048_details", min(DETAIL_CAP, limit), False, False),
            ("resident_released_identity", min(DETAIL_CAP, limit), True, False),
            ("resident_capacity_stop", 0, False, True),
        ):
            if wall() >= deadline:
                aborted = "budget"
                break
            print(f"start {name}", file=progress)
            resident_start = wall() if calibration else None
            try:
                row = memory_state(name, limit, detail_count=detail_count,
                                   release=release, reject=reject, quick=quick)
                row.update({key: value for key, value in _empty_row(name).items()
                            if key not in row})
            except Exception as exc:
                row = _empty_row(name, "FAIL")
                row["reason"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            if calibration and row.get("fixture_n", 0):
                note_calibration("resident", name, row["fixture_n"], CAP,
                                 wall() - resident_start)
            print(f"end {name} {row['status']}", file=progress)
    adapter = {"status": "UNVERIFIED", "reason": "not started"}
    if chosen is None and aborted is None and wall() < deadline:
        for name in adapter_names:
            print(f"start {name}", file=progress)
        adapter_start = wall() if calibration else None
        try:
            adapter, adapter_rows = adapter_status(limit=limit, demand=2 + warmup + 2*samples,
                                                   warmup=warmup, samples=samples,
                                                   quick=quick, clock=clock)
            indexed = {row["name"]: _normalize_adapter_row(row, samples, warmup)
                       for row in adapter_rows}
        except Exception as exc:
            adapter = {"status": "UNVERIFIED", "reason": f"{type(exc).__name__}: {exc}"}
            indexed = {}
        for name in adapter_names:
            row = indexed.get(name, _empty_row(name))
            rows.append(row)
            print(f"end {name} {row['status']}", file=progress)
        if calibration:
            measured = sum(row.get("timed_calls", 0) for row in indexed.values())
            note_calibration("adapter", "adapter_all", measured,
                             len(adapter_names) * (2 + 100 + 2 * 1000),
                             wall() - adapter_start)
    elif chosen is None and aborted is None:
        aborted = "budget"
    pressure_fixtures, churn_fixtures, capacity_points = [], [], []
    rebuild_seen = False
    rebuild_events = []
    capacity_seconds = 0.0
    baseline_sizes = {name: sys.getsizeof(obj) for name, obj in
                      _capacity_containers(new_ledger(limit))}

    def observe_capacity(ledger, fixture_name, kind, minute_index, *, budget=None):
        nonlocal rebuild_seen, capacity_seconds
        observed_at = wall() if calibration else None
        rebuild_seen |= bool(ledger._rebuild_count)
        if ledger._rebuild_count:
            b = _budget_checkpoint(ledger)
            rebuild_events.append({"checkpoint_kind": "after", "rebuild_count": ledger._rebuild_count,
                                   "old_bytes": b["capacity"]["rebuild_old_bytes"],
                                   "new_bytes": b["capacity"]["rebuild_new_bytes"],
                                   "budget": b, "temporary_peak_minus_current_before_bytes": None,
                                   "status": "UNVERIFIED", "reason": "during-backings witness missing"})
        if kind in ("highwater_before", "violation_before") and hasattr(ledger, "_gate_capacity_pre_point"):
            capacity_points.append(ledger._gate_capacity_pre_point)
        else:
            capacity_points.append(_capacity_point(ledger, baseline_sizes, fixture_name,
                                                   kind if kind in ("hour", "day", "prune", "highwater",
                                                                    "highwater_after", "violation_after") else "tail",
                                                   minute_index, budget=budget))
        if calibration:
            capacity_seconds += wall() - observed_at

    def summary_row(name, kind, status, reason):
        return {"name": name, "kind": kind, "status": status, "reason": reason,
                "partial_acceptance_applicable": False}

    for name in pressure_plan:
        if aborted is not None or wall() >= deadline:
            aborted = aborted or "budget"
            break
        print(f"start {name}", file=progress)
        calibration_start = wall() if calibration else None
        fixture = run_pressure_fixture(name, limit=limit, max_resident_bytes=resident_b,
                                       fixture_detail_divisor=fixture_detail_divisor,
                                       _capacity_observer=observe_capacity,
                                       _stop_requested=lambda: wall() >= deadline,
                                       _progress=lambda count: print(f"progress {name} accepted={count}", file=progress))
        pressure_fixtures.append(fixture)
        if calibration:
            note_calibration("pressure", name, fixture["N_last_accepted"] or 0,
                             CAP, wall() - calibration_start)
        rows.append(summary_row(name, "pressure", fixture["status"], fixture["reason"]))
        print(f"end {name} {fixture['status']}", file=progress)
        if fixture["reason"] == "budget":
            aborted = "budget"
    for name in churn_plan:
        if aborted is not None or wall() >= deadline:
            aborted = aborted or "budget"
            break
        print(f"start {name}", file=progress)
        calibration_start = wall() if calibration else None
        fixture = run_churn_fixture(name, limit=limit, max_resident_bytes=resident_b,
                                    churn_minutes=churn_minutes,
                                    churn_stride_minutes=churn_stride_minutes,
                                    fixture_detail_divisor=fixture_detail_divisor,
                                    _capacity_observer=observe_capacity,
                                    _stop_requested=lambda: wall() >= deadline,
                                    _progress=lambda count: print(f"progress {name} batches={count}", file=progress))
        churn_fixtures.append(fixture)
        if calibration:
            rp = fixture["highwater_checks"]["replay"]
            full_ratio = 80_641 / max(1, fixture["scheduled_registered"])
            observed_hres = max((ch["after"]["H_res"] for ch in
                                 fixture["highwater_checks"]["checks"]), default=1)
            resident_ratio = min(full_ratio, max(1, (2880 + fixture["auxiliary_registered"]) /
                                                max(1, observed_hres)))
            # High-water selection is concentrated while resident backing grows;
            # the replay ends at the last selected step, not at day seven.
            projected_pass1 = rp["pass1_seconds"] * full_ratio
            projected_pass2 = rp["pass2_seconds"] * resident_ratio**2
            replay_projection = {"pass1_seconds": rp["pass1_seconds"],
                                 "pass2_seconds": rp["pass2_seconds"],
                                 "selected_pairs": fixture["highwater_checks"]["preserved_pairs"],
                                 "compared_steps": rp["compared_steps"],
                                 "resident_scale": resident_ratio,
                                 "projected_pass1_seconds": projected_pass1,
                                 "projected_pass2_seconds": projected_pass2}
            note_calibration("churn_burst" if name.startswith("churn_burst") else "churn_normal",
                             name, fixture["scheduled_registered"], 80_641,
                             wall() - calibration_start,
                             projected_override=projected_pass1 + projected_pass2,
                             replay_projection=replay_projection)
        rows.append(summary_row(name, "churn", fixture["status"], fixture["reason"]))
        print(f"end {name} {fixture['status']}", file=progress)
        if fixture["first_failure"] and fixture["first_failure"].get("phase") == "budget":
            aborted = "budget"
    capacity = _capacity_proof(capacity_points, churn_fixtures, churn_plan)
    if calibration and capacity_points:
        note_calibration("capacity", "capacity_all", len(capacity_points),
                         len(CHURN_NAMES) * (168 + 7 + 2) + len(PRESSURE_NAMES),
                         capacity_seconds, owned_graph_seconds=capacity_seconds)
    if rebuild_seen:
        capacity["status"] = "UNVERIFIED"
        capacity["reason"] = "rebuild observed without during-backings witness"
    if churn_plan:
        rows.append(summary_row("capacity_highwater", "capacity", capacity["status"], capacity["reason"]))
        rows.append(summary_row("rebuild_double_backing", "rebuild",
                                "UNVERIFIED" if rebuild_seen else "N/A",
                                "during-backings witness missing" if rebuild_seen else
                                "N/A (no rebuild in this implementation)"))
        print("start capacity_highwater", file=progress)
        print(f"end capacity_highwater {capacity['status']}", file=progress)
        print("start rebuild_double_backing", file=progress)
        print(f"end rebuild_double_backing {'UNVERIFIED' if rebuild_seen else 'N/A'}", file=progress)
    if len(rows) < len(names):
        existing = {row["name"] for row in rows}
        rows.extend(_empty_row(name) for name in names if name not in existing)
    if mode == "full" and not all(prereq.values()) and adapter["status"] == "PASS":
        adapter = {**adapter, "status": "UNVERIFIED", "reason": "environment prerequisites"}
    overall = overall_status(rows, adapter_status=adapter["status"], mode=mode)
    partial = partial_acceptance(rows, adapter_status=adapter["status"], mode=mode)
    if aborted is not None:
        partial["eligible"] = False
    if aborted is not None and overall != "FAIL":
        overall = "UNVERIFIED"
    env["load_after"] = _load_average()
    elapsed = wall() - started
    slot_rows, unreachable = [], []
    for row in rows:
        if (limit == CAP and row.get("fixture_stop") in ("byte", "byte_headroom")
                and row.get("fixture_n") is not None):
            unreachable.append({"original_name": row["name"] + "_131072_slots",
                                "fixture_name": row["name"],
                                "reason": "N/A (unreachable by byte policy)",
                                "observed_N": row["fixture_n"],
                                "replacement_name": row["name"]})
    for fixture in pressure_fixtures:
        if fixture["name"] in PRESSURE_AUX:
            continue
        rejection = fixture["first_rejection"]
        cause = rejection["cause"] if rejection else None
        status = "PASS" if cause == "slot" and limit == CAP else "N/A" if cause == "byte" else "UNVERIFIED"
        slot_rows.append({"name": fixture["name"], "status": status,
                          "N_stop": fixture["N_last_accepted"],
                          "reason": "N/A (byte stop)" if status == "N/A" else None})
        if status == "N/A":
            unreachable.append({"original_name": fixture["name"] + "_131072_slots",
                                "fixture_name": fixture["name"],
                                "reason": "N/A (unreachable by byte policy)",
                                "observed_N": fixture["N_last_accepted"],
                                "replacement_name": fixture["name"]})
    verdicts = {key: "UNVERIFIED" for key in ("slice5a2_partial", "slice5a3", "slice5a4")}
    reasons = {key: ["full evidence incomplete"] for key in verdicts}
    if mode == "full":
        legacy_fail = bool(partial["failing_rows"] or adapter["status"] == "FAIL")
        resident_fail = any(r["status"] == "FAIL" for r in rows if r["name"].startswith("resident_"))
        pressure_fail = any(p["status"] == "FAIL" for p in pressure_fixtures)
        scale_fail = any(c["status"] == "FAIL" for c in churn_fixtures) or capacity["status"] == "FAIL"
        if legacy_fail:
            verdicts["slice5a2_partial"] = "FAIL"
            reasons["slice5a2_partial"] = ["legacy measured row or adapter failed"]
        elif partial["eligible"]:
            verdicts["slice5a2_partial"] = "PASS"
            reasons["slice5a2_partial"] = []
        if legacy_fail or resident_fail or pressure_fail:
            verdicts["slice5a3"] = "FAIL"
            reasons["slice5a3"] = ["own pressure, resident, or legacy condition failed"]
        elif overall == "PASS" and aborted is None:
            verdicts["slice5a3"] = "PASS"
            reasons["slice5a3"] = []
        elif overall == "FAIL":
            reasons["slice5a3"] = ["global overall failed outside 5a-3 own conditions"]
        if legacy_fail or resident_fail or pressure_fail or scale_fail:
            verdicts["slice5a4"] = "FAIL"
            reasons["slice5a4"] = ["own pressure, churn, capacity, resident, or legacy condition failed"]
        elif overall == "PASS" and aborted is None:
            verdicts["slice5a4"] = "PASS"
            reasons["slice5a4"] = []
    calibration_result = {"status": "not_run", "observations": [], "reason": None}
    if calibration:
        calibration_result = {"status": "complete" if aborted is None else "incomplete",
                              "observations": calibration_observations, "reason": aborted}
    eta = (_calibration_eta(calibration_observations, budget_seconds)
           if calibration and aborted is None else
           {"seconds": None, "uncertainty_seconds": None, "basis": "none",
            "budget_seconds": budget_seconds, "decision": "unknown", "assumptions": []})
    _gate_resident_budget.reset(budget_token)
    return {"contract": "slice5a4c_contract_r2.md C-prime + slice5a_contract_r2.md S5a.4 B1-B7",
            "acceptance_stage": "provisional",
            "mode": mode, "complete": aborted is None, "aborted_reason": aborted,
            "overall": overall, "summary": summarize(rows), "scenarios": rows,
            "adapter": adapter, "partial_acceptance": partial,
            "limits": {"max_records": limit, "contract_max_records": CAP,
                       "max_resident_bytes": resident_b, "churn_minutes": churn_minutes,
                       "churn_stride_minutes": churn_stride_minutes,
                       "fixture_detail_divisor": fixture_detail_divisor,
                       "max_retained_details": min(DETAIL_CAP, limit),
                       "max_detail_bytes": 4096, "owned_bytes": OWNED_LIMIT,
                       "temporary_bytes": TEMP_LIMIT},
            "pressure_fixtures": pressure_fixtures, "churn_fixtures": churn_fixtures,
            "capacity_proof": capacity, "rebuild_events": rebuild_events,
            "131072_slots": {"by_fixture": slot_rows}, "unreachable_rows": unreachable,
            "verdicts": verdicts, "verdict_reasons": reasons,
            "calibration": calibration_result, "eta": eta,
            "environment": env, "prerequisites": prereq,
            "record_access_audit_findings": audit,
            "total_elapsed_seconds": round(elapsed, 6),
            "exit_code": 0, "reproduce": " ".join(sys.argv)}


EVIDENCE_ROW_IDS = (tuple(f"A{i:02d}" for i in range(1, 13))
                    + tuple(f"B{i:02d}" for i in range(1, 24))
                    + tuple(f"C{i:02d}" for i in range(1, 11)))
ROW_IDS = EVIDENCE_ROW_IDS + tuple(f"D{i:02d}" for i in range(1, 6))
_finalize_cache = ContextVar("d7_finalize_cache", default=None)
_INDEX_PATHS = frozenset(("live", "tomb", "seq", "owner", "owned_id", "previous_job",
                          "cohort", "open", "recent", "close", "overdue", "expiry", "prune"))
_EXISTING_TRANSITION_CLASSES = {
    "link_round": "linked", "report_init_failed": "report_init_failed",
    "wrapper_exited": "wrapper_exited", "next_entry": "predecessor_unchanged",
    "overdue": "overdue", "finish": "finalized", "late_finish": "finalized",
    "close": "closed", "post_close_duplicate": "post_close_duplicate",
}


class _EvidenceMissing(Exception):
    pass


class _EvidenceViolation(Exception):
    pass


def _need(value, *path):
    for key in path:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise _EvidenceMissing("missing " + ".".join(map(str, path))) from exc
    return value


def _integer(value):
    if type(value) is not int:
        raise _EvidenceMissing("integer evidence missing or malformed")
    return value


def _items(value, minimum=1):
    if not isinstance(value, list) or len(value) < minimum:
        raise _EvidenceMissing("event list missing or incomplete")
    return value


def _names(value, label):
    names = _items(value)
    if not all(isinstance(name, str) and name for name in names):
        raise _EvidenceMissing(f"{label} name missing or malformed")
    _assert(len(set(names)) == len(names), f"{label} names duplicated")
    return names


def _unique_named(rows, key, label):
    names = [_need(row, key) for row in _items(rows)]
    return _names(names, label)


def _trace_projection(trace, transitions):
    _eq(len(trace), len(transitions), "transition trace length differs")
    for call, item in zip(trace, transitions):
        for a, b in (("action", "action"), ("classification", "classification"),
                     ("detail_charge_bytes", "detail_charge_bytes"), ("registered_seq", "seq")):
            _eq(_need(call, a), _need(item, b))


def _registered_calls(trace):
    return [step for step in trace if _need(step, "action") == "register" and
            _need(step, "classification") == "registered"]


def _check_job_key_counts(recorded, calls):
    """Compare the public list with successful registrations having a job ID."""
    entries = _items(recorded, 0)
    actual = Counter()
    for call in calls:
        job = _need(call, "job_id")
        if job is not None:
            actual[(_need(call, "source"), job)] += 1
    observed = {}
    for entry in entries:
        key = (_need(entry, "source"), _need(entry, "job_id"))
        if not all(isinstance(part, str) and part for part in key):
            raise _EvidenceMissing("job key missing or malformed")
        _assert(key not in observed, "duplicate job key")
        count = _integer(_need(entry, "registrations"))
        _assert(count > 0, "job registration count must be positive")
        observed[key] = count
    _eq(observed, dict(actual), "job key count differs from registration trace")


def _eq(actual, expected, reason="recorded value disagrees with source evidence"):
    def bool_for_integer(a, b):
        if (type(a) is bool and type(b) is int) or (type(a) is int and type(b) is bool):
            return True
        if isinstance(a, dict) and isinstance(b, dict):
            return any(key in b and bool_for_integer(value, b[key]) for key, value in a.items())
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return any(bool_for_integer(x, y) for x, y in zip(a, b))
        return False
    if bool_for_integer(actual, expected):
        raise _EvidenceMissing("JSON bool cannot stand for integer evidence")
    if actual != expected:
        raise _EvidenceViolation(reason)


def _assert(condition, reason="measured predicate violated"):
    if not condition:
        raise _EvidenceViolation(reason)


def _first_fixture(report, key):
    return _items(_need(report, key))[0]


def _index_audit(audit):
    before, after = _need(audit, "before"), _need(audit, "after")
    for point in (before, after):
        paths = _need(point, "paths")
        if not isinstance(paths, dict) or set(paths) != _INDEX_PATHS:
            raise _EvidenceMissing("candidate index path audit incomplete")
        _assert(all(v is False for v in paths.values()), "candidate inserted into an index")
    _eq(_need(audit, "absent_paths"), sorted(_INDEX_PATHS))
    for key in ("N_total", "last_seq"):
        _eq(_integer(_need(before, key)), _integer(_need(after, key)))
    owned = _need(audit, "owned_bytes")
    _eq(_need(owned, "candidate_before"), 0)
    _eq(_need(owned, "candidate_after"), 0)


def _budget_order(point):
    g, e, b = (_integer(_need(point, key)) for key in ("G", "E", "B"))
    _assert(0 <= g <= e <= b, "G≤E≤B violated")


def _observed_timing(scenario):
    d, k = _integer(_need(scenario, "D")), _integer(_need(scenario, "K"))
    visits = _integer(_need(scenario, "visits"))
    limit = 64 + 3 * (d + k)
    _eq(_integer(_need(scenario, "visit_limit")), limit)
    _assert(visits <= limit, "visit bound exceeded")
    timing = _need(scenario, "timing")
    for kind in ("gc_disabled", "gc_enabled"):
        sample = _need(timing, kind)
        raw = _items(_need(sample, "raw_ns"))
        _eq(_integer(_need(sample, "n")), len(raw))
        _assert(all(type(n) is int and n >= 0 for n in raw), "invalid duration")
        ordered = sorted(raw)
        rank = max(1, math.ceil(len(raw) * .99))
        _eq(_need(sample, "p99_us"), round(ordered[rank - 1] / 1000, 3))
        _eq(_need(sample, "max_us"), round(ordered[-1] / 1000, 3))
        full_cohort = (scenario.get("name") == "cohort_all" and
                       scenario.get("fixture_stop") != "byte" and d + k == CAP)
        if kind == "gc_disabled" and (d + k <= DETAIL_CAP or full_cohort):
            p_limit, max_limit = ((20_000, 100_000) if d + k == 0 else
                                  (250_000, 1_000_000) if d + k <= DETAIL_CAP else
                                  (2_000_000, 5_000_000))
            _assert(_need(sample, "p99_us") <= p_limit and
                    _need(sample, "max_us") <= max_limit, "time limit exceeded")


def _measured_time_gate(scenario, *, required_n):
    """Check a time N/A against the measured D/K and both GC distributions."""
    d, k = _integer(_need(scenario, "D")), _integer(_need(scenario, "K"))
    gate = _need(scenario, "time_gate")
    full_cohort = (scenario.get("name") == "cohort_all" and
                   scenario.get("fixture_stop") != "byte" and d + k == CAP)
    if gate == "N/A":
        if d + k <= DETAIL_CAP or full_cohort:
            raise _EvidenceMissing("time N/A lacks an over-2048 D/K reason")
        for key in ("D_observed", "K_observed"):
            values = _items(_need(scenario, key), 2)
            _eq(len(values), 2)
            _eq(max(_integer(v) for v in values), d if key == "D_observed" else k)
        exception = scenario.get("sample_exception")
        if exception is not None:
            _eq(_need(scenario, "name"), "mass_overdue")
            _eq(exception, "mass_overdue_report_only_100_per_gc")
            _eq(_need(scenario, "original_required_n"), 1000)
            _assert("exceeds 2048" in _need(scenario, "sample_exception_reason"))
            required_n = 100
        for kind in ("gc_disabled", "gc_enabled"):
            if _integer(_need(scenario, "timing", kind, "n")) < required_n:
                raise _EvidenceMissing("GC distribution too small")
        return
    if gate != "PASS":
        raise _EvidenceMissing("time gate has no supported verdict")
    if d + k > DETAIL_CAP and not full_cohort:
        raise _EvidenceMissing("over-2048 time PASS lacks a full cohort reason")
    for kind in ("gc_disabled", "gc_enabled"):
        if _integer(_need(scenario, "timing", kind, "n")) < required_n:
            raise _EvidenceMissing("GC distribution too small")


def _temporary(scenario):
    tm = _need(scenario, "tracemalloc")
    before, peak = _integer(_need(tm, "current_before")), _integer(_need(tm, "peak"))
    _integer(_need(tm, "current_after"))
    _assert(peak >= before, "tracemalloc peak before current")
    delta = peak - before
    _assert(delta <= 262144, "temporary allocation limit exceeded")
    return delta


def _evaluate_a(row, report):
    unit = report.get("mode") == "predicate_unit"
    if row == "A01":
        limits = _need(report, "limits")
        names = ("max_records", "max_retained_details", "max_detail_bytes",
                 "max_resident_bytes", "owned_bytes", "temporary_bytes",
                 "churn_minutes", "churn_stride_minutes", "fixture_detail_divisor")
        expected = (131072, 2048, 4096, 62914560, 67108864, 262144, 10080, 1, 1)
        for name, value in zip(names, expected):
            _eq(_integer(_need(limits, name)), value, f"default limit {name} changed")
        if not unit:
            _assert(_need(report, "complete") is True and
                    _need(report, "aborted_reason") is None, "full run not complete")
    elif row == "A02":
        seen = set()
        for fixture in _items(_need(report, "pressure_fixtures")) + _need(report, "churn_fixtures") if not unit else _items(_need(report, "pressure_fixtures")):
            trace = _items(_need(fixture, "fixture_provenance", "api_trace"))
            registers = _registered_calls(trace)
            _eq([_integer(_need(step, "call_index")) for step in trace], list(range(len(trace))))
            ids = [_need(step, "id") for step in registers]
            if not all(isinstance(i, str) for i in ids):
                raise _EvidenceMissing("registered ID missing or malformed")
            _assert(len(set(ids)) == len(ids) and not seen.intersection(ids), "ID collision")
            seen.update(ids)
            for identity in ids:
                _eq(_need(fixture, "id_utf8_bytes"), len(identity.encode()))
                _eq(_need(fixture, "id_getsizeof_bytes"), sys.getsizeof(identity))
            _eq(_need(fixture, "source_registered"),
                dict(Counter(_need(step, "source") for step in registers)))
            _check_job_key_counts(_need(fixture, "job_key_counts"), registers)
            _eq(_need(fixture, "state_registration_counts"),
                dict(Counter(_need(step, "source") for step in registers)))
            for step in trace:
                _integer(_need(step, "registered_seq"))
                _integer(_need(step, "detail_charge_bytes"))
                if _need(step, "action") == "register":
                    _assert(_need(step, "classification") in ("registered", "admission_stopped"),
                            "register classification invalid")
                    if step["classification"] == "registered":
                        _eq(_need(step, "state_before"), "absent")
                        _eq(_need(step, "state_after"), "live")
            if not unit:
                required = ("call_index", "action", "id", "source", "job_id", "received_at",
                            "received_mono", "started_at", "started_mono", "classification",
                            "registered_seq", "detail_charge_bytes", "state_before", "state_after")
                for step in trace:
                    for key in required: _need(step, key)
                _trace_projection(trace, _items(_need(fixture, "transition_trace")))
            _eq(_need(fixture, "fixture_provenance", "detail_charged_bytes"),
                sum(step.get("detail_charge_bytes", 0) for step in trace))
            clone = _need(fixture, "fixture_provenance", "clone_equivalence")
            if _need(clone, "used"):
                for key in ("original_before_sha256", "original_after_sha256", "clone_replay_sha256"):
                    digest = _need(clone, key)
                    if not isinstance(digest, str) or len(digest) != 64:
                        raise _EvidenceMissing("clone digest missing or malformed")
                _eq(_need(clone, "original_before_sha256"), _need(clone, "original_after_sha256"))
                _eq(_need(clone, "original_before_sha256"), _need(clone, "clone_replay_sha256"))
            else:
                for key in ("original_before_sha256", "original_after_sha256", "clone_replay_sha256"):
                    _eq(_need(clone, key), None)
    elif row == "A03":
        fixtures = _items(_need(report, "pressure_fixtures"))
        for fixture in (fixtures if unit else
                        [p for p in fixtures if p.get("name") in PRESSURE_NAMES +
                         ("pressure_slot_first_small_limit",)]):
            inv = _need(fixture, "receipt_invariants")
            trace = _items(_need(fixture, "fixture_provenance", "api_trace"))
            steps = _items(_need(inv, "checked_steps"))
            end = _integer(_need(fixture, "first_rejection", "trace_call_index"))
            _assert(0 <= end < len(trace), "first rejection index outside trace")
            _eq(len(steps), end + 1)
            trace = trace[:end + 1]
            clock_pairs = set()
            for receipt, call in zip(steps, trace):
                for key in ("call_index", "received_at", "received_mono", "started_at", "started_mono"):
                    _eq(_need(receipt, key), _need(call, key))
                _assert(_need(receipt, "received_at") == _need(receipt, "received_mono") ==
                        _need(receipt, "started_at") == _need(receipt, "started_mono"), "stale receipt clock")
                _eq(_need(receipt, "N_res"), _need(receipt, "N_total"))
                _eq(_need(receipt, "N_tomb"), 0)
                _eq(_need(receipt, "retire_eligible"), 0)
                clock_pairs.add((_need(receipt, "received_at"), _need(receipt, "received_mono")))
            _eq(len(clock_pairs), 1)
            _assert(all(_need(inv, key) is True for key in
                        ("same_pair", "fresh_starts", "no_retirement")))
            _eq(_need(fixture, "first_rejection", "received_at"), _need(steps[0], "received_at"))
            _eq(_need(fixture, "first_rejection", "received_mono"), _need(steps[0], "received_mono"))
            _eq(_need(fixture, "before", "N_total"), _need(fixture, "before", "N_res"))
            _eq(_need(fixture, "after", "N_total"), _need(fixture, "after", "N_res"))
            _eq(_integer(_need(fixture, "before", "N_tomb")), 0)
            _eq(_integer(_need(fixture, "after", "N_tomb")), 0)
            rejection = _need(fixture, "first_rejection")
            rejected = [call for call in trace if call.get("action") == "register" and
                        call.get("classification") == "admission_stopped"]
            if not rejected:
                raise _EvidenceMissing("first rejection trace missing")
            _eq(_integer(_need(rejection, "trace_call_index")),
                _integer(_need(rejected[0], "call_index")))
            _eq(_need(rejection, "received_at"), _need(rejected[0], "received_at"))
            _eq(_need(rejection, "received_mono"), _need(rejected[0], "received_mono"))
            _eq(_need(rejected[0], "received_mono"), _need(steps[0], "received_mono"))
            _eq(_integer(_need(steps[trace.index(rejected[0])], "N_tomb")), 0)
    elif row == "A04":
        fixtures = _items(_need(report, "pressure_fixtures"))
        scenarios = {s.get("name"): s for s in _items(_need(report, "scenarios"))}
        kinds = _need(report, "test_scale", "id_kinds") if unit else ("short_ascii", "ascii128", "unicode128")
        tracks = _need(report, "test_scale", "tracks") if unit else ("p0", "p1", "p2", "p3")
        primary = fixtures if unit else [p for p in fixtures if p.get("name") in PRESSURE_NAMES]
        if not unit:
            _eq(len(primary), len(PRESSURE_NAMES))
            _eq({p.get("name") for p in primary}, set(PRESSURE_NAMES))
        _eq({(p.get("id_kind"), p.get("track")) for p in primary},
            {(kind, track) for kind in kinds for track in tracks})
        for p in primary:
            if not unit:
                _eq(_need(p, "name"), f"pressure_{_need(p, 'track')}_{_need(p, 'id_kind')}")
            target = (0 if p["track"].lower() in ("p0", "p1") else
                      512 if p["track"].lower() == "p2" else 2048)
            _eq(_need(p, "requested_detail_target"), target)
            _eq(_need(p, "effective_detail_target"), target)
            _eq(_need(p, "status"), "PASS")
            _eq(_need(scenarios, _need(p, "name"), "status"), "PASS")
    elif row == "A05":
        fixtures = _items(_need(report, "pressure_fixtures"))
        for p in (fixtures if unit else [p for p in fixtures if p.get("name") in PRESSURE_NAMES]):
            trace = _items(_need(p, "fixture_provenance", "api_trace"))
            trans = _items(_need(p, "transition_trace"))
            _trace_projection(trace, trans)
            _eq(_need(p, "fixture_provenance", "detail_charged_bytes"),
                sum(_need(s, "detail_charge_bytes") for s in trans))
            target = _need(report, "test_scale", "detail_target") if unit else _need(p, "requested_detail_target")
            _eq(_need(p, "retained_details"), target)
            _eq(_need(p, "effective_detail_target"), target)
            tracks = ("p0", "p1", "p2", "p3", "P0", "P1", "P2", "P3") if unit else ("p0", "p1", "p2", "p3")
            _assert(_need(p, "track") in tracks)
            track = p["track"].lower()
            observed_d = _need(p, "before", "D") if "before" in p else None
            detail_charge = (observed_d // target if target and observed_d is not None
                             else sum(_need(call, "detail_charge_bytes") for call in trace
                                      if call.get("action") == "finish" and
                                      call.get("classification") == "finalized") // target
                             if target else 0)
            if target:
                _assert(type(detail_charge) is int and detail_charge > 0)
                if observed_d is not None:
                    _eq(detail_charge * target, observed_d)
            register_positions = [i for i, call in enumerate(trace)
                                  if call.get("action") == "register" and call.get("classification") == "registered"]
            all_register_positions = [i for i, call in enumerate(trace) if call.get("action") == "register"]
            if not register_positions:
                raise _EvidenceMissing("pressure register trace missing")
            for index, start in enumerate(register_positions):
                stop = next((pos for pos in all_register_positions if pos > start), len(trace))
                calls = trace[start:stop]
                seq = _integer(_need(calls[0], "registered_seq"))
                _eq(_need(calls[0], "classification"), "registered")
                _eq(_integer(_need(calls[0], "detail_charge_bytes")), 0)
                prefix = track in ("p2", "p3") and index < target
                if prefix:
                    expected = (("register", "registered", 0), ("link_round", "linked", 0),
                                ("finish", "finalized", detail_charge))
                elif track == "p0":
                    expected = (("register", "registered", 0),)
                else:
                    phase = index % 20
                    suffix = (("link_round", "linked", 0),) if phase < 4 else (
                        (("report_init_failed", "report_init_failed", 0),) if phase < 8 else ())
                    expected = (("register", "registered", 0),) + suffix
                for call, (action, classification, charge) in zip(calls, expected):
                    _eq(_need(call, "registered_seq"), seq)
                    _eq(_need(call, "action"), action)
                    _eq(_need(call, "classification"), classification)
                    _eq(_integer(_need(call, "detail_charge_bytes")), charge)
                if len(calls) < len(expected):
                    raise _EvidenceMissing("pressure cycle trace incomplete")
                _eq(len(calls), len(expected), "unexpected pressure cycle transition")
    elif row == "A06":
        for p in _items(_need(report, "pressure_fixtures")):
            r = _need(p, "first_rejection")
            before, after = _need(p, "before"), _need(p, "after")
            slot, byte = _need(r, "slot_test"), _need(r, "byte_precheck")
            _eq(_integer(_need(r, "candidate_seq")), _integer(_need(p, "N_last_accepted")) + 1)
            _eq(_need(slot, "candidate_N_res"), _integer(_need(before, "N_res")) + 1)
            causes = []
            if _need(slot, "would_exceed") is True: causes.append("slot")
            if _need(byte, "would_exceed") is True: causes.append("byte")
            fixture_limit = (_need(p, "fixture_limit") if not unit else
                             p.get("fixture_limit", _need(report, "limits", "max_records")))
            _eq(_need(slot, "would_exceed"), _need(slot, "candidate_N_res") >
                _integer(fixture_limit))
            _eq(_need(byte, "would_exceed"), _need(byte, "candidate_E") > _need(before, "B"))
            _eq(_need(r, "precedence"), "slot_then_byte")
            _eq(_need(r, "simultaneous_causes"), causes)
            _eq(_need(r, "cause"), causes[0] if causes else None)
            _eq(_need(r, "classification"), "admission_stopped")
            _eq(_need(r, "admission_stopped_at"), _need(r, "received_at"))
            _eq(_need(after, "N_total"), _need(before, "N_total"))
            _eq(_need(after, "N_res"), _need(before, "N_res"))
            _eq(_need(r, "health_after", "admission_stopped_at"), _need(r, "received_at"))
            hb, ha = _need(r, "health_before"), _need(r, "health_after")
            for key in ("N_total", "last_seq"):
                _eq(_integer(_need(hb, key)), _integer(_need(ha, key)))
            _eq(_need(hb, "N_total"), _need(before, "N_total"))
            _eq(_need(ha, "N_total"), _need(after, "N_total"))
            _eq(_need(hb, "last_seq"), _need(p, "N_last_accepted"))
            _assert(_need(hb, "admission_stopped") is False)
            _assert(_need(ha, "admission_stopped") is True)
            trace = _items(_need(p, "fixture_provenance", "api_trace"))
            end = _integer(_need(r, "trace_call_index"))
            _assert(0 <= end < len(trace), "first rejection index outside trace")
            rejected = [call for call in trace[:end + 1]
                        if call.get("action") == "register" and
                        call.get("classification") == "admission_stopped"]
            if len(rejected) != 1:
                raise _EvidenceMissing("first rejection API call missing or ambiguous")
            call = rejected[0]
            _eq(_integer(_need(call, "call_index")), end)
            for left, right in (("candidate_id", "id"), ("candidate_source", "source"),
                                ("candidate_job_id", "job_id"), ("received_at", "received_at"),
                                ("received_mono", "received_mono")):
                _eq(_need(r, left), _need(call, right))
    elif row == "A07":
        for p in _items(_need(report, "pressure_fixtures")):
            r = _need(p, "first_rejection")
            _assert(_need(r, "no_insertion") is True)
            _index_audit(_need(r, "index_audit"))
    elif row == "A08":
        sources = _need(report, "test_scale", "sources") if unit else ("investing", "bs", "citi")
        fixtures = _items(_need(report, "pressure_fixtures"))
        for p in (fixtures if unit else [p for p in fixtures if p.get("name") !=
                                   "pressure_unique_job_keys_byte_stop"]):
            r = _need(p, "first_rejection")
            post = _need(p, "post_latch")
            _index_audit(_need(r, "index_audit"))
            _assert(_integer(_need(post, "attempted")) >= len(sources))
            _assert(_need(post, "all_rejected") is True)
            _assert(_need(post, "first_stop_time_unchanged") is True)
            _assert(_need(post, "resumed_after_release") is False, "admission latch reopened")
            for source in sources:
                coverage = _need(post, "coverage_by_source", source)
                _assert(_need(coverage, "uncertain") is True)
                _assert("aggregation_capacity" in _need(coverage, "diagnostic_codes"))
            diag = _need(post, "g_diagnostic")
            _assert(_need(diag, "measured") is True)
            _assert(_need(diag, "G") <= _need(diag, "E"))
            prune = _need(post, "prune_after_release")
            _assert(_need(prune, "N_res_after") < _need(prune, "N_res_before"))
            _eq(_need(prune, "attempt_classification"), "admission_stopped")
            _eq(_need(prune, "stop_time"), _need(r, "admission_stopped_at"))
            calls = _items(_need(post, "api_trace"))
            _eq(_integer(_need(post, "attempted")), len(calls))
            _assert(all(_need(call, "classification") == "admission_stopped" for call in calls),
                    "post-latch call accepted")
            _assert(set(_need(call, "source") for call in calls) >= set(sources),
                    "post-latch source not attempted")
    elif row == "A09":
        kinds = _need(report, "test_scale", "existing_transitions") if unit else None
        fixtures = _items(_need(report, "pressure_fixtures"))
        for p in (fixtures if unit else [p for p in fixtures if p.get("name") !=
                                   "pressure_unique_job_keys_byte_stop"]):
            post = _need(p, "post_latch")
            transitions = _need(post, "existing_id_transitions")
            budgets = _items(_need(post, "existing_id_budget"))
            trace = _items(_need(p, "fixture_provenance", "api_trace"))
            for kind in kinds or transitions:
                expected = _need(transitions, kind)
                if kind not in _EXISTING_TRANSITION_CLASSES:
                    raise _EvidenceMissing("existing transition kind unknown")
                _eq(expected, _EXISTING_TRANSITION_CLASSES[kind])
                _assert(any(t.get("action") == kind and t.get("classification") == expected for t in trace))
            for b in budgets:
                _assert(_need(b, "AR_after") <= _need(b, "AR_before"), "existing ID reserve grew")
                _assert(_need(b, "classification") != "admission_stopped")
                action = _need(b, "action")
                expected = _need(transitions, action)
                _eq(_need(b, "classification"), expected)
                _assert(any(_need(t, "id") == _need(b, "id") and
                            _need(t, "action") == action and
                            _need(t, "classification") == expected for t in trace),
                        "existing ID budget has no matching API event")
                matching = [t for t in trace if t.get("id") == b["id"] and
                            t.get("action") == action and
                            t.get("classification") == expected]
                _eq(_need(b, "state_before"), _need(matching[0], "state_before"))
                for stage in ("before", "after"):
                    _budget_order(_need(b, stage))
            _assert(set(_need(b, "action") for b in budgets) >= set(kinds or transitions),
                    "existing transition has no budget observation")
            _budget_order(_need(p, "before")); _budget_order(_need(p, "after"))
    elif row == "A10":
        fixtures = _items(_need(report, "pressure_fixtures"))
        slot_rows = _items(_need(report, "131072_slots", "by_fixture"))
        slots = {r.get("name"): r for r in slot_rows}
        unreachable = _items(_need(report, "unreachable_rows"))
        scenarios = {s.get("name"): s for s in _items(_need(report, "scenarios"))}
        auxiliary = [p for p in fixtures if p.get("name") == "pressure_slot_first_small_limit"]
        if len(auxiliary) != 1:
            raise _EvidenceMissing("small-limit slot control missing or duplicated")
        control = auxiliary[0]
        _assert(_need(control, "fixture_limit") < 131072)
        _eq(_need(control, "before", "B"), 62914560)
        _eq(_need(control, "first_rejection", "cause"), "slot")
        if not unit:
            _eq(_need(control, "fixture_limit"), 128)
            _eq(_need(control, "status"), "PASS")
            _eq(len(slot_rows), len(PRESSURE_NAMES))
            _eq(set(slots), set(PRESSURE_NAMES))
        primary = ([p for p in fixtures if p.get("name") in PRESSURE_NAMES] if not unit else
                   [p for p in fixtures if p is not control])
        if not unit:
            _eq(len(primary), len(PRESSURE_NAMES))
        for p in primary:
            _eq(_need(p, "before", "B"), 62914560)
            if not unit: _eq(_need(p, "fixture_limit"), 131072)
            n = _integer(_need(p, "N_last_accepted"))
            cause = _need(p, "first_rejection", "cause")
            slot = _need(slots, _need(p, "name"))
            _eq(_need(slot, "N_stop"), n)
            if cause == "byte":
                _assert(0 < n < 131072)
                _eq(_need(slot, "status"), "N/A")
                _eq(_need(slot, "reason"), "N/A (byte stop)")
                _assert(any(u.get("fixture_name") == p["name"] and u.get("observed_N") == n
                            and u.get("replacement_name") == p["name"] and
                            u.get("original_name") == p["name"] + "_131072_slots" and
                            u.get("reason") == "N/A (unreachable by byte policy)"
                            for u in unreachable))
            else:
                _eq(cause, "slot"); _eq(n, 131072); _eq(_need(slot, "status"), "PASS")
            _eq(_need(scenarios, p["name"], "status"), "PASS")
    elif row == "A11":
        names = (_need(report, "test_scale", "measured_scenarios") if unit else
                 _D02_SCENARIOS + ("prune_transition",))
        if unit: names = _names(names, "measured_scenarios")
        scenarios = {s.get("name"): s for s in _items(_need(report, "scenarios"))}
        required_n = _need(report, "test_scale", "samples_per_gc") if unit else 1000
        for name in names:
            s = _need(scenarios, name)
            _observed_timing(s)
            _eq(_need(s, "visit_gate"), "PASS")
            _measured_time_gate(s, required_n=required_n)
            plan = _need(s, "sample_plan")
            if not isinstance(plan, str) or plan not in {
                    "repeat", "sequential", "sequential_reprepared", "full_deepcopy",
                    "independent_split_copy"}:
                raise _EvidenceMissing("sample plan missing or unsupported")
            checks = _need(s, "sample_checks")
            _eq(_need(checks, "failures"), [])
            _assert(_integer(_need(checks, "checked")) >= 2 * required_n,
                    "insufficient independently checked calls")
            if not unit and name in ("register_accept", "finish_accept"):
                observations = _items(_need(s, "sample_observations"))
                for phase in ("gc_disabled", "gc_enabled"):
                    measured = [o for o in observations if o.get("phase") == phase]
                    if len(measured) < required_n:
                        raise _EvidenceMissing("independent timed call samples missing")
                    _assert(all(_need(o, "classification") ==
                                ("registered" if name == "register_accept" else "finalized")
                                for o in measured), "timed API call did not succeed")
                    indices = [_integer(_need(o, "index")) for o in measured]
                    _assert(indices == sorted(set(indices)), "timed call indices reused or unordered")
                    targets = [_need(o, "target") for o in measured]
                    if not all(isinstance(target, str) and target for target in targets):
                        raise _EvidenceMissing("timed call target missing")
                    _assert(len(set(targets)) == len(targets), "timed call target reused")
                    if name == "finish_accept":
                        for observation in measured:
                            prepared = _integer(_need(observation, "prepared_at"))
                            called = _integer(_need(observation, "called_at"))
                            _assert(prepared <= called, "finish measured before preparation")
                _eq(plan, "sequential_reprepared")
    elif row == "A12":
        names = (_need(report, "test_scale", "temporary_scenarios") if unit else
                 _D02_SCENARIOS + ("prune_transition", "normalization"))
        scenarios = {s.get("name"): s for s in _items(_need(report, "scenarios"))}
        origins = set()
        for name in names:
            s = _need(scenarios, name)
            delta = _temporary(s)
            _eq(_need(s, "temporary_breakdown", "peak_delta"), delta)
            _eq(_need(s, "temporary_gate"), "PASS")
            _items(_need(s, "timing", "gc_disabled", "raw_ns"))
            _items(_need(s, "timing", "gc_enabled", "raw_ns"))
            if name in ("prune_transition", "close_boundary_exact", "normalization"):
                origin = _need(s, "temporary_sample_origin")
                if not isinstance(origin, str) or not origin:
                    raise _EvidenceMissing("temporary measurement origin missing")
                _eq(_need(s, "tracemalloc", "sample_origin"), origin)
                _assert(origin not in origins, "temporary measurements share one sample")
                origins.add(origin)


def _identity_absence(sample):
    _eq(_need(sample, "seq_positions"), [])
    _assert(_need(sample, "tomb") is False and _need(sample, "owner") is False)
    other = _need(sample, "other_indexes_absent")
    if set(other) != _INDEX_PATHS - {"seq", "tomb", "owner"}:
        raise _EvidenceMissing("identity index audit incomplete")
    _assert(all(v is True for v in other.values()))


def _checkpoint_pair(fixture, before_kind, after_kind):
    checkpoints = _items(_need(fixture, "checkpoints"), 2)
    before = _items([p for p in checkpoints if p.get("kind") == before_kind])[0]
    after = _items([p for p in checkpoints if p.get("kind") == after_kind])[0]
    return before, after


def _boundary_pairs(fixture, before_kind, after_kind, expected):
    """Pair every boundary observation by its witnessed identity, not list position."""
    checkpoints = _items(_need(fixture, "checkpoints"), 2 * expected)
    sides = []
    for kind in (before_kind, after_kind):
        selected = [p for p in checkpoints if p.get("kind") == kind]
        if len(selected) < expected:
            raise _EvidenceMissing(f"{kind} boundary observations incomplete")
        _eq(len(selected), expected, "boundary observation count changed")
        indexed = {}
        for cp in selected:
            identity = _need(cp, "identity_samples", 0, "id")
            if identity in indexed:
                raise _EvidenceViolation("boundary identity repeated")
            indexed[identity] = cp
        sides.append(indexed)
    _eq(set(sides[0]), set(sides[1]), "boundary identity pairs differ")
    return [(identity, sides[0][identity], sides[1][identity]) for identity in sides[0]]


def _b_followup(row, report, fixtures, unit):
    """Independent inputs for the B01–B12 clauses beyond the row's local equations."""
    if row == "B02":
        applicable = fixtures if unit else [f for f in fixtures if f.get("name") in CHURN_NAMES[:-2]]
        for f in applicable:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            cycle = _items(_need(f, "state_cycle"))
            registers = [t for t in trace if t.get("action") == "register"]
            events_by_seq = defaultdict(list)
            for event in trace:
                if "registered_seq" in event:
                    events_by_seq[event["registered_seq"]].append(event)
            first_unbound = next((i for i, e in enumerate(cycle) if e.get("action") == "unbound"), None)
            limit = len(registers) if unit else len(registers) - 1
            for i, reg in enumerate(registers[:limit]):
                position = i % len(cycle)
                entry = cycle[position]
                seq = _need(reg, "registered_seq")
                events = events_by_seq[seq]
                action = _need(entry, "action")
                if action == "finish":
                    _eq([_need(t, "action") for t in events], ["register", "link_round", "finish"])
                    _eq([_need(t, "classification") for t in events[1:]], ["linked", "finalized"])
                elif action == "link":
                    _eq([_need(t, "action") for t in events], ["register", "link_round"])
                    _eq(_need(events[1], "classification"), "linked")
                elif action == "unbound":
                    if ((_need(f, "state_track").endswith("_init_failed") or
                         _need(f, "name").startswith("churn_init_failed_"))
                            and position == first_unbound):
                        _eq([_need(t, "action") for t in events], ["register", "init_failed"])
                        _eq(_need(events[1], "classification"), "report_init_failed")
                    else:
                        _eq([_need(t, "action") for t in events], ["register"])
                else:
                    raise _EvidenceMissing(f"cycle action at {position} has no trace rule")
            if (_need(f, "state_track").endswith("_init_failed") or
                    _need(f, "name").startswith("churn_init_failed_")):
                if first_unbound is None: raise _EvidenceMissing("Unicode unbound target absent")
                failures_by_seq = Counter(t.get("registered_seq") for t in trace
                                          if t.get("classification") == "report_init_failed")
                for reg in registers[:limit]:
                    seq = _need(reg, "registered_seq")
                    _eq(failures_by_seq[seq],
                        int(_need(reg, "cycle_position") == first_unbound))
    elif row == "B03":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            d = _integer(_need(report, "test_scale", "burst_details")) if unit else 2048
            n = _integer(_need(report, "test_scale", "normal_calls")) if unit else 80641
            registers = [t for t in trace if t.get("action") == "register"]
            _eq(len(registers), d + 1 + n, "burst and normal input ledger incomplete")
            _eq(len({t.get("registered_seq") for t in registers}), len(registers),
                "burst and normal registrations overlap")
            probe = registers[d]
            _eq(_need(probe, "classification"), "registered")
            open_cp, release_cp = _checkpoint_pair(f, "detail_open", "detail_released")
            for axis in ("received_at", "received_mono"):
                _assert(_need(open_cp, axis) <= _need(probe, axis) < _need(release_cp, axis),
                        "probe was not accepted while details were held")
    elif row == "B04":
        n = _integer(_need(report, "test_scale", "normal_calls")) if unit else 80641
        if n < 2: raise _EvidenceMissing("normal_calls must be at least two")
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"), n)
            normal = [t for t in trace if t.get("action") == "register" and
                      not t.get("auxiliary", False)]
            _eq([_integer(_need(t, "call_index")) for t in trace], list(range(len(trace))))
            _assert(len(normal) >= n)
            cps = _items(_need(f, "checkpoints"))
            _eq(len([c for c in cps if c.get("kind") == "tail_80640"]), 1)
            _eq(len([c for c in cps if c.get("kind") == "tail_80641"]), 1)
            _eq(len({_need(c, "checkpoint_id") for c in cps}), len(cps),
                "simultaneous checkpoints reused an identity")
    elif row == "B05":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            cps = _items(_need(f, "checkpoints"))
            public = [c for c in cps if c.get("observation") == "public_advance"]
            queries = _need(f, "fixture_provenance", "public_query_trace") if public or not unit else []
            _eq(len(queries), len(public), "public query evidence incomplete")
            by_id = {_need(q, "checkpoint_id"): q for q in queries}
            _eq(len(by_id), len(queries), "public query reused checkpoint ID")
            for cp in public:
                query = _need(by_id, _need(cp, "checkpoint_id"))
                _eq((_need(query, "received_at"), _need(query, "received_mono")),
                    (_need(cp, "received_at"), _need(cp, "received_mono")))
                _eq(_need(query, "event_order"), _need(cp, "event_order"))
            before, after = _checkpoint_pair(f, "highwater_before", "highwater_after")
            low, high = _need(before, "event_order"), _need(after, "event_order")
            _assert(not any(low < _need(c, "event_order") < high for c in public),
                    "public advance occurred inside a passive highwater pair")
            _assert(not any(low < _need(q, "event_order") < high for q in queries),
                    "public query occurred inside a passive highwater pair")
            _assert(any(low < _need(t, "event_order") < high and t.get("action") == "register"
                        for t in trace))
    elif row == "B06":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            if not unit:
                previous_event = -1
                for step in trace:
                    event = _integer(_need(step, "event_order"))
                    _assert(event >= previous_event, "API event order regressed")
                    previous_event = event
            cps = _items(_need(f, "checkpoints"))
            cursor = 0
            scheduled = Counter()
            registered = Counter()
            previous_order = -1
            for cp in cps:
                total = _integer(_need(cp, "N_total"))
                order = total if unit else _integer(_need(cp, "event_order"))
                _assert(order >= previous_order, "checkpoint order regressed")
                previous_order = order
                while cursor < len(trace):
                    step = trace[cursor]
                    if step.get("action") == "register":
                        step_order = _integer(_need(step, "registered_seq" if unit else "event_order"))
                    else:
                        step_order = _integer(_need(step, "event_order")) if not unit else -1
                    if step_order > order: break
                    if step.get("action") == "register":
                        source = _need(step, "source")
                        scheduled[source] += 1
                        if _need(step, "classification") == "registered":
                            registered[source] += 1
                    cursor += 1
                rejected = scheduled - registered
                _eq(_need(cp, "admission_counts"),
                    {"scheduled": sum(scheduled.values()), "registered": sum(registered.values()),
                     "rejected": sum(rejected.values())})
                _eq(_need(cp, "source_counts"),
                    {source: {"scheduled": qty, "registered": registered[source],
                              "rejected": rejected[source]} for source, qty in scheduled.items()})
    elif row == "B07":
        for f in fixtures:
            cps = _items(_need(f, "checkpoints"), 3)
            observed = [(cp, s) for cp in cps for s in cp.get("identity_samples", [])
                        if s.get("phase") in ("live", "tomb", "pruned")]
            live = _items([(cp, s) for cp, s in observed if s.get("phase") == "live"])[0][1]
            identity = tuple(_need(live, key) for key in ("id", "registered_seq", "source"))
            for phase, state in (("live", "live"), ("tomb", "tombstoned"),
                                 ("pruned", "expired_or_untracked")):
                phase_rows = [(cp, s) for cp, s in observed if s.get("phase") == phase]
                if not phase_rows: raise _EvidenceMissing(f"{phase} identity phase missing")
                matches = [(cp, s) for cp, s in phase_rows if
                           tuple(s.get(key) for key in ("id", "registered_seq", "source")) == identity]
                _assert(bool(matches), "identity changed across live, tomb, and pruned phases")
                _eq(_need(matches[0][1], "observed"), state)
            tail = cps[-1] if unit else _items([c for c in cps if c.get("kind") == "tail_80641"])[0]
            audit = _items(_need(tail, "identity_index_audit", "samples"))
            matched = [a for a in audit if a.get("id") == identity[0]]
            if not matched: raise _EvidenceMissing("initial identity absent from tail index audit")
            _identity_absence(matched[0])
    elif row in ("B08", "B09", "B11"):
        kinds = {"B08": ("probe_close_before", "probe_close_at", 12),
                 "B09": ("probe_expire_before", "probe_expire_at", 24),
                 "B11": ("probe_prune_before", "probe_prune_at", 36)}
        before_kind, after_kind, full_count = kinds[row]
        for f in fixtures:
            pairs = _boundary_pairs(f, before_kind, after_kind, 1 if unit else full_count)
            trace = _items(_need(f, "fixture_provenance", "api_trace")) if row != "B11" or not unit else []
            for identity, before, at in pairs:
                if row == "B08":
                    finish = _items([t for t in trace if t.get("action") == "finish" and
                                     t.get("id") == identity])[0]
                    for axis, due in (("received_at", "close_at"), ("received_mono", "close_mono")):
                        _eq(_need(before, axis), _integer(_need(finish, due)) - 1)
                        _eq(_need(at, axis), _need(finish, due))
                    _eq(_need(before, "identity_samples", 0, "observed"), "live")
                    _eq(_need(at, "identity_samples", 0, "observed"), "tombstoned")
                    if unit:
                        _eq(_need(before, "retained_details"), 1)
                        _eq(_need(at, "retained_details"), 0)
                    _eq(_need(before, "retained_details") - _need(at, "retained_details"), 1)
                    _eq(_need(at, "cumulative_end"), _need(at, "close_through"))
                    totals = _items(_need(at, "frozen_totals_before_after"))
                    matching = [x for x in totals if x.get("id") == identity]
                    if not matching: raise _EvidenceMissing("close frozen input missing")
                    for item in matching:
                        _eq(_need(item, "requery"), _need(item, "at"))
                        _eq(_need(item, "at", "finished") - _need(item, "before", "finished"), 1)
                        source = _need(item, "source")
                        for cp, part in ((before, "before"), (at, "at")):
                            expected_finished = sum(
                                t.get("action") == "finish" and t.get("source") == source and
                                _integer(_need(t, "close_at")) <= _integer(_need(cp, "received_at")) and
                                _integer(_need(t, "close_mono")) <= _integer(_need(cp, "received_mono"))
                                for t in trace if t.get("action") == "finish" and t.get("source") == source)
                            _eq(_need(item, part, "finished"), expected_finished,
                                "frozen completion count differs from completed input ledger")
                elif row == "B09":
                    reg = _items([t for t in trace if t.get("action") == "register" and
                                  t.get("id") == identity])[0]
                    for axis in ("received_at", "received_mono"):
                        _eq(_need(before, axis), _integer(_need(reg, axis)) + 14_400_000_000 - 1)
                        _eq(_need(at, axis), _integer(_need(reg, axis)) + 14_400_000_000)
                    _eq(_need(before, "identity_samples", 0, "observed"), "live")
                    _eq(_need(at, "identity_samples", 0, "observed"), "tombstoned")
                    _assert(_need(before, "coverage_complete") is True and
                            _need(at, "coverage_complete") is False)
                    _eq(_need(at, "diagnostic_counters", "retention_expired") -
                        _need(before, "diagnostic_counters", "retention_expired"), 1)
                    causes = ("expired_start", "expired_finish", "expired_wrapper",
                              "expired_identity_unverified")
                    observed_causes = Counter(_need(t, "classification") for t in trace
                                              if t.get("id") == identity and t.get("classification") in causes and
                                              _integer(_need(before, "received_at")) <
                                              _integer(_need(t, "received_at")) <= _integer(_need(at, "received_at")) and
                                              _integer(_need(before, "received_mono")) <
                                              _integer(_need(t, "received_mono")) <= _integer(_need(at, "received_mono")))
                    if not observed_causes:
                        raise _EvidenceMissing("expiry cause input trace absent")
                    for cause in causes:
                        if cause in _need(before, "diagnostic_counters") or cause in _need(at, "diagnostic_counters"):
                            _eq(_need(at, "diagnostic_counters", cause) -
                                _need(before, "diagnostic_counters", cause), observed_causes[cause])
                        elif observed_causes[cause]:
                            raise _EvidenceMissing("expiry cause counter absent")
                    _eq(_need(at, "uncertain_sources"), [_need(reg, "source")])
                else:
                    for axis in ("received_at", "received_mono"):
                        _eq(_need(before, axis), _integer(_need(at, axis)) - 1)
                    _eq(_need(before, "identity_samples", 0, "observed"), "tombstoned")
                    _eq(_need(at, "identity_samples", 0, "observed"), "expired_or_untracked")
                    witness = _need(at, "prune_witness")
                    _eq(_need(witness, "before_N_res"), _need(before, "N_res"))
                    _eq(_need(witness, "after_N_res"), _need(at, "N_res"))
                    _eq(_need(witness, "pruned_count"), _need(witness, "before_N_res") +
                        _need(witness, "accepted_between") - _need(witness, "after_N_res"))
                    _assert(identity in _need(witness, "sample_ids"))
                    audit = _items(_need(at, "identity_index_audit", "samples"))
                    matched = [sample for sample in audit if sample.get("id") == identity]
                    if not matched: raise _EvidenceMissing("pruned identity index audit absent")
                    _identity_absence(matched[0])
                    _assert(_integer(_need(at, "prune_observed_through")) <=
                            _integer(_need(witness, "as_of")))
                    _eq(_need(witness, "unprocessed_tomb_count"), 0,
                        "prune watermark advanced with unprocessed tombs")
                    if not unit:
                        tomb = _items([t for t in trace if t.get("action") == "tomb" and
                                       t.get("id") == identity])[0]
                        for axis, due in (("received_at", "prune_due_at"),
                                          ("received_mono", "prune_due_mono")):
                            _eq(_need(at, axis), _need(tomb, due))
            if row == "B11" and not unit:
                watermarks = [_need(cp, "prune_observed_through") for cp in
                              _items(_need(f, "checkpoints")) if cp.get("kind") == after_kind]
                _eq(watermarks, sorted(watermarks), "prune watermark regressed")
    elif row == "B10" and not unit:
        hour_us = 3_600_000_000
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            normal = [t for t in trace if t.get("action") == "register" and not t.get("auxiliary", False)]
            start = _items(normal)[0]
            tombs = [t for t in trace if t.get("action") == "tomb"]
            ledger = _need(f, "tomb_due_ledger")
            _eq(len(ledger), len(tombs), "due ledger omitted a tomb transition")
            by_id = {_need(t, "id"): t for t in tombs}
            _eq(len(by_id), len(tombs), "tomb identity repeated")
            for entry in ledger:
                tomb = _need(by_id, _need(entry, "id"))
                for key in ("registered_seq", "source"):
                    _eq(_need(entry, key), _need(tomb, key))
                due = max((_integer(_need(tomb, "prune_due_at")) - _integer(_need(start, "received_at")) + hour_us - 1) // hour_us,
                          (_integer(_need(tomb, "prune_due_mono")) - _integer(_need(start, "received_mono")) + hour_us - 1) // hour_us)
                _eq(_need(entry, "due_hour"), due)
    elif row == "B12":
        for f in fixtures:
            pairs = _boundary_pairs(f, "probe_recent_before", "probe_recent_at", 1 if unit else 10)
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            ledger = _items(_need(f, "recent_input_ledger"))
            _eq(len({e.get("id") for e in ledger}), len(ledger), "recent input identity repeated")
            for entry in ledger:
                identity = _need(entry, "id")
                reg = _items([t for t in trace if t.get("action") == "register" and
                              t.get("id") == identity and t.get("source") == entry.get("source")])[0]
                finish = _items([t for t in trace if t.get("action") == "finish" and
                                 t.get("id") == identity])[0]
                _eq(_need(entry, "connection"), _need(reg, "connection"))
                _eq(_need(entry, "lifecycle"), _need(finish, "classification"))
                for axis, start in (("received_at", "registered_at"),
                                    ("received_mono", "registered_mono")):
                    _eq(_need(entry, start), _need(reg, axis))
                for end in ("bucket_end", "bucket_end_mono"):
                    _eq(_need(entry, end), _need(finish, end))
            for identity, before, at in pairs:
                target = _items([e for e in ledger if e.get("id") == identity])[0]
                _eq(_need(before, "identity_samples", 0, "observed"), "included")
                _eq(_need(at, "identity_samples", 0, "observed"), "excluded")
                for axis, end in (("received_at", "bucket_end"), ("received_mono", "bucket_end_mono")):
                    _eq(_need(before, axis), _integer(_need(target, end)) - 1)
                    _eq(_need(at, axis), _need(target, end))
                for cp, part in ((before, "before"), (at, "at")):
                    counts = {}
                    for entry in ledger:
                        if not (all(_need(entry, start) <= _need(cp, axis) < _need(entry, end)
                                    for start, axis, end in (("registered_at", "received_at", "bucket_end"),
                                                             ("registered_mono", "received_mono", "bucket_end_mono")))):
                            continue
                        source = _need(entry, "source")
                        bucket = counts.setdefault(source, {"registered": 0, "connection": {}, "lifecycle": {}})
                        bucket["registered"] += 1
                        for axis in ("connection", "lifecycle"):
                            classification = _need(entry, axis)
                            bucket[axis][classification] = bucket[axis].get(classification, 0) + 1
                    sources = {e["source"] for e in ledger}
                    for source in sources:
                        expected = counts.get(source, {"registered": 0, "connection": {}, "lifecycle": {}})
                        _eq(_need(cp, "recent_expected_counts", source, part), expected)
                        _eq(_need(cp, "recent_cohort_counts", source), expected)


def _evaluate_b(row, report):
    unit = report.get("mode") == "predicate_unit"
    fixtures = _items(_need(report, "churn_fixtures"))
    if row == "B01":
        n = _integer(_need(report, "test_scale", "normal_calls")) if unit else 80641
        if unit and n < 2:
            raise _EvidenceMissing("normal_calls must be at least two")
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"), n)
            _eq([_integer(_need(t, "call_index")) for t in trace], list(range(len(trace))))
            normal = [t for t in trace if t.get("action") == "register" and
                      not t.get("auxiliary", False)][:n]
            _eq(len(normal), n)
            _assert(all(t.get("classification") == "registered" for t in normal))
            start = normal[0]
            for i, t in enumerate(normal):
                offset = (i % 8) * 7_500_000 + (i // 8) * 60_000_000
                _eq(_need(t, "received_at"), _need(start, "received_at") + offset)
                _eq(_need(t, "received_mono"), _need(start, "received_mono") + offset)
                _eq(_need(t, "source"), ("investing" if i % 8 < 6 else "bs" if i % 8 == 6 else "citi"))
            _eq(_need(f, "scheduled_registered"), n)
            _eq(_need(f, "scheduled_target"), n)
            _eq(_need(f, "N_total_at_tail"), n + f.get("auxiliary_registered", 0))
            registered = [t for t in trace if t.get("action") == "register" and
                          t.get("classification") == "registered"]
            _eq(_need(f, "source_registered"), dict(Counter(_need(t, "source") for t in registered)))
            _eq(_need(f, "classification_counts", "registered"), len(registered))
            _eq(_need(f, "tail_classification"), "registered")
            b, a = _checkpoint_pair(f, "tail_80640", "tail_80641")
            for cp, count in ((b, n - 1), (a, n)):
                _eq((_need(cp, "received_at"), _need(cp, "received_mono")),
                    (_need(normal[count - 1], "received_at"), _need(normal[count - 1], "received_mono")))
                prefix = [t for t in trace if t.get("action") == "register" and
                          _integer(_need(t, "call_index")) <= _need(normal[count - 1], "call_index")]
                _eq(len([t for t in prefix if not t.get("auxiliary", False)]), count)
                _eq(len(prefix), count + f.get("auxiliary_registered", 0))
                accepted = [t for t in prefix if t.get("classification") == "registered"]
                admission = _need(cp, "admission_counts")
                _eq((_need(admission, "scheduled"), _need(admission, "registered"), _need(admission, "rejected")),
                    (len(prefix), len(accepted), len(prefix) - len(accepted)))
                source_counts = _need(cp, "source_counts")
                _eq(set(source_counts), set(REGISTRY) if not unit else
                    {_need(t, "source") for t in prefix})
                for source, counts in source_counts.items():
                    scheduled = sum(t.get("source") == source for t in prefix)
                    registered_count = sum(t.get("source") == source and
                                           t.get("classification") == "registered" for t in prefix)
                    _eq(counts, {"scheduled": scheduled, "registered": registered_count,
                                 "rejected": scheduled - registered_count})
    elif row == "B02":
        scale = _need(report, "test_scale") if unit else {}
        names = _need(scale, "churn_names") if unit else None
        applicable = fixtures if unit else [f for f in fixtures if f.get("name") in CHURN_NAMES[:-2]]
        if not unit:
            _eq(len(applicable), 8)
        for f in applicable:
            if names is not None and _need(f, "name") not in names: continue
            cycle = _items(_need(f, "state_cycle"))
            length = _need(scale, "cycle_length") if unit else 20
            _eq(len(cycle), length)
            _eq([_need(c, "position") for c in cycle], list(range(length)))
            actions = Counter(_need(c, "action") for c in cycle)
            if unit:
                _eq(actions.get("finish", 0), _need(scale, "finished"))
                _eq(actions.get("unbound", 0), _need(scale, "unbound"))
                if length == 20:
                    track = _need(f, "state_track")
                    _assert(track in ("most_finished", "most_finished_init_failed",
                                      "most_unfinished", "most_unfinished_init_failed"),
                            "unknown state track")
                    _eq(dict(actions), {"finish": 16, "link": 2, "unbound": 2}
                        if track.startswith("most_finished") else
                        {"finish": 4, "link": 4, "unbound": 12},
                        "state track distribution changed")
            else:
                track = _need(f, "state_track")
                _assert(track in ("most_finished", "most_finished_init_failed",
                                  "most_unfinished", "most_unfinished_init_failed"),
                        "unknown state track")
                expected = ({"finish": 16, "link": 2, "unbound": 2}
                            if track.startswith("most_finished") else
                            {"finish": 4, "link": 4, "unbound": 12})
                _eq(dict(actions), expected, "full state cycle distribution changed")
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            registers = [t for t in trace if t.get("action") == "register"]
            _eq(len(registers), length * _need(scale, "cycles") if unit and "cycles" in scale else
                length if unit else 80641)
            finished_seqs = {t.get("registered_seq") for t in trace
                             if t.get("action") == "finish" and t.get("classification") == "finalized"}
            for i, t in enumerate(registers if unit else registers[:80640]):
                position = i % length
                _eq(_need(t, "cycle_position"), position)
                _eq(_need(t, "source"), _need(cycle[position], "source"))
                if cycle[position]["action"] == "finish":
                    seq = _need(t, "registered_seq")
                    _assert(seq in finished_seqs)
            if (_need(f, "state_track").endswith("_init_failed") or
                    _need(f, "name").startswith("churn_init_failed_")):
                first_unbound = next((i for i, item in enumerate(cycle)
                                      if item.get("action") == "unbound"), None)
                if first_unbound is None: raise _EvidenceMissing("init failure target absent")
                failures_by_seq = Counter(t.get("registered_seq") for t in trace
                                          if t.get("classification") == "report_init_failed")
                for reg in (registers if unit else registers[:80640]):
                    seq = _need(reg, "registered_seq")
                    _eq(failures_by_seq[seq],
                        int(_need(reg, "cycle_position") == first_unbound),
                        "periodic first unbound failure differs")
            _eq(_need(f, "classification_counts", "registered"), len(registers))
            # classification_counts records register outcomes; completion evidence is in the API trace.
            ids = [_need(t, "id") for t in registers]
            for identity in ids:
                _eq(_need(f, "id_utf8_bytes"), len(identity.encode()))
                _eq(_need(f, "id_getsizeof_bytes"), sys.getsizeof(identity))
    elif row == "B03":
        d = _need(report, "test_scale", "burst_details") if unit else 2048
        n = _need(report, "test_scale", "normal_calls") if unit else 80641
        applicable = fixtures if unit else [f for f in fixtures if f.get("name") in CHURN_NAMES[-2:]]
        if not unit:
            _eq(len(applicable), 2)
            _eq({f.get("name") for f in applicable}, set(CHURN_NAMES[-2:]))
            _assert(all(_need(f, "id_kind") == f["name"].removeprefix("churn_burst_")
                        and _need(f, "state_track") == "burst" for f in applicable),
                    "burst fixture identity changed")
        for f in applicable:
            _eq(_need(f, "auxiliary_registered"), d + 1)
            _eq(_need(f, "N_total_at_tail"), n + d + 1)
            _eq(_need(f, "scheduled_registered"), n)
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            probe = _need([t for t in trace if t.get("action") == "register"], d)
            _eq(_need(probe, "classification"), "registered")
            probe_position = trace.index(probe)
            close_position = next((i for i, t in enumerate(trace) if t.get("action") == "close"), None)
            if close_position is None: raise _EvidenceMissing("detail release input missing")
            first_normal = _need([t for t in trace if t.get("action") == "register"], d + 1)
            _assert(probe_position < close_position < trace.index(first_normal),
                    "probe, close, and normal schedule order violated")
            completed_before_probe = [t for t in trace[:probe_position]
                                      if t.get("action") == "finish" and
                                      t.get("classification") == "finalized"]
            _eq(len(completed_before_probe), d, "burst details not complete at probe")
            registers = [t for t in trace if t.get("action") == "register"]
            _assert(len(registers) >= d + 1 + n, "normal calls absent after probe")
            normal = registers[d + 1:d + 1 + n]
            _assert(all(trace.index(t) > close_position and t.get("classification") == "registered"
                        for t in normal), "normal calls not independently accepted")
            if not unit:
                _assert(all(t.get("auxiliary") is True for t in registers[:d + 1]))
                _assert(all(t.get("auxiliary") is False for t in normal))
            open_cp, released = _checkpoint_pair(f, "detail_open", "detail_released")
            _eq(_need(open_cp, "retained_details"), d)
            _eq(_need(open_cp, "budget", "D"), d * 4096)
            _eq(_need(released, "retained_details"), 0)
            _eq(_need(released, "budget", "D"), 0)
            _eq(_need(f, "max_retained_details_observed"), d)
            expire = _items([p for p in f["checkpoints"] if p.get("kind") == "probe_expire_at"])[0]
            _eq(_need(expire, "received_at"), _need(probe, "received_at") + 14_400_000_000)
            _eq(_need(expire, "received_mono"), _need(probe, "received_mono") + 14_400_000_000)
            _assert(any(s.get("id") == probe["id"] and s.get("observed") == "tombstoned"
                        for s in _need(expire, "identity_samples")))
            _eq(_need(_items([p for p in f["checkpoints"] if p.get("kind") == "tail_80641"])[0], "N_total"), n + d + 1)
    elif row == "B04":
        hours = _need(report, "test_scale", "hours") if unit else 168
        days = _need(report, "test_scale", "days") if unit else 7
        n = _need(report, "test_scale", "normal_calls") if unit else 80641
        for f in fixtures:
            _eq(_need(f, "evidence_gaps"), [])
            trace = _items(_need(f, "fixture_provenance", "api_trace"), n)
            normal = [t for t in trace if t.get("action") == "register" and
                      not t.get("auxiliary", False)]
            _assert(len(normal) >= n)
            start = normal[0]
            checkpoints = _items(_need(f, "checkpoints"))
            _eq(len({c.get("checkpoint_id") for c in checkpoints}), len(checkpoints))
            for kind, total, increment in (("hour", hours, 3_600_000_000), ("day", days, 86_400_000_000)):
                selected = [c for c in checkpoints if c.get("kind") == kind]
                if len(selected) < total: raise _EvidenceMissing("scheduled checkpoint missing")
                _eq(len(selected), total)
                for i, cp in enumerate(selected, 1):
                    _eq(_need(cp, "received_at"), _need(start, "received_at") + i * increment)
                    _eq(_need(cp, "received_mono"), _need(start, "received_mono") + i * increment)
            for kind, index in (("tail_80640", n - 2), ("tail_80641", n - 1)):
                cp = _items([c for c in checkpoints if c.get("kind") == kind])[0]
                for key in ("received_at", "received_mono"):
                    _eq(_need(cp, key), _need(normal[index], key))
    elif row == "B05":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            checkpoints = _items(_need(f, "checkpoints"), 2)
            _eq(len({c.get("checkpoint_id") for c in checkpoints}), len(checkpoints))
            register = _items([x for x in trace if x.get("action") == "register"])[0]
            before, after = _checkpoint_pair(f, "highwater_before", "highwater_after")
            _assert(_need(before, "observation") == _need(after, "observation") == "passive")
            _assert(_need(before, "event_order") < _need(register, "event_order") < _need(after, "event_order"))
            for key in ("received_at", "received_mono"):
                _eq(_need(before, key), _need(register, key))
                _eq(_need(after, key), _need(register, key))
    elif row == "B06":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            registered_trace = (t for t in trace if t.get("classification") == "registered")
            next_registered = next(registered_trace, None)
            registered_count = 0
            for cp in _items(_need(f, "checkpoints")):
                admission = _need(cp, "admission_counts")
                _eq(_need(admission, "scheduled"), _need(admission, "registered") + _need(admission, "rejected"))
                source_counts = _need(cp, "source_counts")
                for counts in source_counts.values():
                    _eq(_need(counts, "scheduled"), _need(counts, "registered") + _need(counts, "rejected"))
                _eq(_need(cp, "source_registered"), {k: v["registered"] for k, v in source_counts.items()})
                _eq(sum(v["registered"] for v in source_counts.values()), _need(admission, "registered"))
                total = _integer(_need(cp, "N_total"))
                _eq(_need(cp, "last_seq"), total)
                _eq(_need(cp, "registered_records"), total)
                _eq(_need(cp, "N_res"), _integer(_need(cp, "N_live")) + _integer(_need(cp, "N_tomb")))
                _assert(_need(cp, "N_res") <= 131072)
                _eq(_need(admission, "rejected"), 0)
                while next_registered is not None and _integer(_need(next_registered, "registered_seq")) <= total:
                    registered_count += 1
                    _eq(_need(next_registered, "registered_seq"), registered_count,
                        "registration sequence gap")
                    next_registered = next(registered_trace, None)
                _eq(registered_count, total, "registration sequence gap")
    elif row == "B07":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            cps = _items(_need(f, "checkpoints"), 3)
            phases = ("live", "tomb", "pruned")
            states = ("live", "tombstoned", "expired_or_untracked")
            samples = [(cp, sample) for cp in cps for sample in cp.get("identity_samples", [])]
            initial = _items([(cp, sample) for cp, sample in samples
                              if sample.get("phase") == "live"])[0][1]
            identity = tuple(_need(initial, key) for key in ("id", "registered_seq", "source"))
            for phase, state in zip(phases, states):
                phase_rows = [(cp, sample) for cp, sample in samples if sample.get("phase") == phase]
                if not phase_rows: raise _EvidenceMissing(f"{phase} identity phase missing")
                selected = [(cp, sample) for cp, sample in phase_rows if
                            tuple(sample.get(key) for key in ("id", "registered_seq", "source")) == identity]
                _assert(bool(selected), "identity changed across live, tomb, and pruned phases")
                cp, sample = selected[0]
                _eq(_need(sample, "phase"), phase)
                _eq(_need(sample, "observed"), state)
                _eq(_need(sample, "expected"), state)
                _assert(any(t.get("id") == sample["id"] and t.get("registered_seq") == sample["registered_seq"]
                            and t.get("source") == sample["source"] for t in trace))
            tail = cps[-1] if unit else _items([cp for cp in cps if cp.get("kind") == "tail_80641"])[0]
            final = _items([sample for sample in _need(tail, "identity_index_audit", "samples")
                            if sample.get("id") == identity[0]])[0]
            _eq(_need(final, "checkpoint_id"), _need(tail, "checkpoint_id"))
            _identity_absence(final)
            _assert(_need(tail, "N_total") > 0)
    elif row == "B08":
        for f in fixtures:
            b, a = _checkpoint_pair(f, "probe_close_before", "probe_close_at")
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            finished = _items([t for t in trace if t.get("action") == "finish"])[0]
            for axis, end_key in (("received_at", "close_at"), ("received_mono", "close_mono")):
                _eq(_need(b, axis), _need(finished, end_key) - 1)
                _eq(_need(a, axis), _need(finished, end_key))
            if unit:
                _eq(_need(b, "retained_details"), 1)
                _eq(_need(a, "retained_details"), 0)
            else:
                _eq(_need(b, "retained_details") - _need(a, "retained_details"), 1)
            for cp, state in ((b, "live"), (a, "tombstoned")):
                sample = _items(_need(cp, "identity_samples"))[0]
                _eq(_need(sample, "id"), _need(finished, "id"))
                _eq(_need(sample, "observed"), state)
            _eq(_need(a, "cumulative_end"), _need(a, "close_through"))
            for totals in _items(_need(a, "frozen_totals_before_after")):
                _eq(_need(totals, "id"), _need(finished, "id"))
                _eq(_need(totals, "requery"), _need(totals, "at"))
                _assert(_need(totals, "at", "finished") > _need(totals, "before", "finished"))
    elif row == "B09":
        for f in fixtures:
            b, a = _checkpoint_pair(f, "probe_expire_before", "probe_expire_at")
            reg = _items(_need(f, "fixture_provenance", "api_trace"))[0]
            for axis in ("received_at", "received_mono"):
                _eq(_need(b, axis), _need(reg, axis) + 14_400_000_000 - 1)
                _eq(_need(a, axis), _need(reg, axis) + 14_400_000_000)
            for cp, state in ((b, "live"), (a, "tombstoned")):
                sample = _items(_need(cp, "identity_samples"))[0]
                _eq(_need(sample, "id"), _need(reg, "id"))
                _eq(_need(sample, "observed"), state)
            _eq(_need(a, "diagnostic_counters", "retention_expired") -
                _need(b, "diagnostic_counters", "retention_expired"), 1)
            _eq(_need(a, "diagnostic_counters", "expired_start") -
                _need(b, "diagnostic_counters", "expired_start"), 1)
            _assert(_need(b, "coverage_complete") is True and _need(a, "coverage_complete") is False)
            _eq(_need(a, "uncertain_sources"), [_need(reg, "source")])
    elif row == "B10":
        hours = _need(report, "test_scale", "hours") if unit else 168
        pre = _need(report, "test_scale", "pre_prune_hours") if unit else 3
        for f in fixtures:
            ledger = _need(f, "tomb_due_ledger")
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            due = Counter(_need(e, "due_hour") for e in ledger)
            identities = {(t.get("id"), t.get("registered_seq"), t.get("source"))
                          for t in trace}
            for entry in ledger:
                _assert((entry.get("id"), entry.get("registered_seq"),
                         entry.get("source")) in identities)
            checkpoints = [c for c in _items(_need(f, "checkpoints")) if c.get("kind") == "hour"]
            _eq(len(checkpoints), hours)
            first = _items([t for t in trace if t.get("action") == "register"])[0]
            previous_res, previous_total = 0, 0
            for hour, cp in enumerate(checkpoints, 1):
                _eq(_need(cp, "minute_index"), 60 * hour - 1)
                for axis in ("received_at", "received_mono"):
                    _eq(_need(cp, axis), _integer(_need(first, axis)) + hour * 60 * MINUTE)
                pruned = _integer(_need(cp, "pruned_since_previous_hour"))
                _eq(pruned, due[hour])
                _assert(pruned == 0 if hour <= pre else pruned > 0)
                accepted = _integer(_need(cp, "N_total")) - previous_total
                other = _integer(_need(cp, "other_recorded_resident_removals"))
                _assert(other >= 0)
                _eq(pruned + other, previous_res + accepted - _integer(_need(cp, "N_res")))
                previous_res, previous_total = cp["N_res"], cp["N_total"]
    elif row == "B11":
        for f in fixtures:
            b, a = _checkpoint_pair(f, "probe_prune_before", "probe_prune_at")
            for axis in ("received_at", "received_mono"):
                _eq(_need(b, axis), _need(a, axis) - 1)
            sample_b = _items(_need(b, "identity_samples"))[0]
            sample_a = _items(_need(a, "identity_samples"))[0]
            _eq(_need(sample_b, "id"), _need(sample_a, "id"))
            _eq(_need(sample_b, "observed"), "tombstoned")
            _eq(_need(sample_a, "observed"), "expired_or_untracked")
            witness = _need(a, "prune_witness")
            count = _need(witness, "before_N_res") + _need(witness, "accepted_between") - _need(witness, "after_N_res")
            _eq(_need(witness, "pruned_count"), count)
            _assert(count > 0)
            _eq(_need(witness, "before_N_res"), _need(b, "N_res"))
            _eq(_need(witness, "after_N_res"), _need(a, "N_res"))
            _assert(_need(sample_a, "id") in _need(witness, "sample_ids"))
            _assert(_need(a, "prune_observed_through") <= _need(witness, "as_of"))
            audit = _items(_need(a, "identity_index_audit", "samples"))[0]
            _eq(_need(audit, "id"), _need(sample_a, "id"))
            _identity_absence(audit)
    elif row == "B12":
        for f in fixtures:
            b, a = _checkpoint_pair(f, "probe_recent_before", "probe_recent_at")
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            finish = _items([t for t in trace if t.get("action") == "finish"])[0]
            reg = _items([t for t in trace if t.get("action") == "register" and t.get("id") == finish.get("id")])[0]
            _eq(_need(b, "received_at"), _need(finish, "bucket_end") - 1)
            _eq(_need(a, "received_at"), _need(finish, "bucket_end"))
            for cp, state, part in ((b, "included", "before"), (a, "excluded", "at")):
                rows = _items(_need(cp, "recent_rows"))
                row_keys = []
                for item in rows:
                    row_keys.append((_need(item, "source"), _need(item, "pair")))
                    _assert(isinstance(_need(item, "collection"), dict) and
                            isinstance(_need(item, "writing"), dict),
                            "recent validity row malformed")
                _eq(len(set(row_keys)), len(row_keys), "recent validity row duplicated")
                _eq(_need(cp, "identity_samples", 0, "id"), _need(finish, "id"))
                _eq(_need(cp, "identity_samples", 0, "observed"), state)
                source = _need(finish, "source")
                expected = _need(cp, "recent_expected_counts", source, part)
                _eq(_need(cp, "recent_cohort_counts", source), expected)
            source = _need(finish, "source")
            for path in (("registered",), ("connection", _need(reg, "connection")),
                         ("lifecycle", _need(finish, "classification"))):
                before = _need(b, "recent_cohort_counts", source)
                at = _need(a, "recent_cohort_counts", source)
                before_count = before
                at_count = at
                for key in path:
                    before_count = before_count.get(key, 0)
                    at_count = at_count.get(key, 0)
                _eq(_integer(before_count) - _integer(at_count), 1,
                    "recent boundary cohort difference is not one")

    _b_followup(row, report, fixtures, unit)


def _validate_budget(budget, unit):
    for key in ("F_4", "Q_4", "D", "A", "R", "T", "R_T", "AR", "TR", "E", "B", "G", "B_minus_E"):
        _integer(_need(budget, key))
    _eq(budget["AR"], budget["A"] + budget["R"])
    _eq(budget["TR"], budget["T"] + budget["R_T"])
    _eq(budget["E"], sum(budget[k] for k in ("F_4", "Q_4", "D", "AR", "TR")))
    _eq(budget["Q_4"], _need(budget, "capacity", "charged_bytes"))
    _eq(budget["B_minus_E"], budget["B"] - budget["E"])
    _budget_order(budget)
    _eq(_need(budget, "unknown_types"), [])
    if not unit: _eq(budget["B"], 62_914_560)


def _container_sum(point):
    seen = set()
    total = 0
    for backing in _need(point, "container_backings"):
        marker = _need(backing, "object_id")
        if marker in seen: continue
        seen.add(marker)
        attributed = _integer(_need(backing, "Q_attributed_bytes"))
        _assert(attributed <= _integer(_need(backing, "charged_limit_bytes")))
        _assert(attributed == max(0, _integer(_need(backing, "getsizeof_bytes")) -
                                   _integer(_need(backing, "header_accounted_elsewhere_bytes"))))
        _need(backing, "includes_deleted_dummy")
        total += attributed
    _eq(total, _integer(_need(point, "Q_actual_bytes")))
    _assert(total <= _integer(_need(point, "Q_4_bytes")), "Q actual exceeds Q4")


def _evaluate_b_rest(row, report):
    unit = report.get("mode") == "predicate_unit"
    if row not in ("B20", "B22", "B23"):
        fixtures = _items(_need(report, "churn_fixtures"))
    if row == "B13":
        for f in fixtures:
            checkpoints = _items(_need(f, "checkpoints"))
            kinds = Counter(_need(cp, "kind") for cp in checkpoints)
            required = (Counter(_names(_need(report, "test_scale", "required_budget_points"),
                                       "required_budget_points")) if unit else
                        Counter({"hour": 168, "day": 7, "tail_80640": 1, "tail_80641": 1,
                                 "highwater_before": 1, "highwater_after": 1,
                                 "probe_close_before": 12, "probe_close_at": 12,
                                 "probe_expire_before": 24, "probe_expire_at": 24,
                                 "probe_prune_before": 36, "probe_prune_at": 36,
                                 "probe_recent_before": 10, "probe_recent_at": 10}))
            for kind, minimum in required.items():
                if kinds[kind] < minimum:
                    raise _EvidenceMissing(f"required budget checkpoint absent: {kind}")
            _eq(len({_need(cp, "checkpoint_id") for cp in checkpoints}), len(checkpoints))
            for cp in checkpoints:
                _validate_budget(_need(cp, "budget"), unit)
    elif row == "B14":
        for f in fixtures:
            cps = _items(_need(f, "checkpoints"), 2)
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            last = None
            for cp in cps:
                for axis, received, last_received in (("wall", "received_at", "last_received_at"),
                                                      ("mono", "received_mono", "last_received_mono")):
                    _assert(_need(cp, last_received) <= _need(cp, received))
                    if last:
                        _assert(_need(cp, received) >= _need(last, received))
                        _assert(_need(cp, last_received) >= _need(last, last_received))
                _eq(_need(cp, "cumulative_end"), _need(cp, "close_through"))
                if last and last.get("close_through") is not None and cp.get("close_through") is not None:
                    _assert(cp["close_through"] >= last["close_through"])
                if last and last.get("prune_observed_through") is not None and cp.get("prune_observed_through") is not None:
                    _assert(cp["prune_observed_through"] >= last["prune_observed_through"])
                last = cp
            closed = [t for t in trace if t.get("action") in ("finish", "close")]
            _assert(any(t.get("close_at") == _need(cps[-1], "close_through") for t in closed))
            _assert(_need(cps[-1], "prune_observed_through") <= _need(cps[-1], "prune_witness", "as_of"))
            for totals in _items(_need(cps[-1], "frozen_totals_before_after")):
                _eq(_need(totals, "at"), _need(totals, "requery"))
            for cp in cps:
                close = _need(cp, "close_through")
                if close is not None:
                    matches = [t for t in closed if t.get("close_at") == close]
                    if not matches:
                        raise _EvidenceMissing("close deadline input absent")
                    _assert(any(_integer(_need(t, "close_at")) <= _integer(_need(cp, "received_at"))
                                and _integer(_need(t, "close_mono")) <= _integer(_need(cp, "received_mono"))
                                for t in matches), "close posted before both deadlines")
                watermark = _need(cp, "prune_observed_through")
                if watermark is not None:
                    witness = _need(cp, "prune_witness")
                    _eq(_need(witness, "unprocessed_tomb_count"), 0)
                    _assert(_integer(_need(witness, "as_of")) <= _integer(_need(cp, "received_at"))
                            and _integer(_need(witness, "as_of_mono")) <= _integer(_need(cp, "received_mono")))
                    _assert(_integer(watermark) <= _integer(_need(witness, "as_of")))
                    tombs = [t for t in trace if t.get("action") == "tomb"]
                    if not tombs:
                        raise _EvidenceMissing("prune deadline input absent")
                    due = {t.get("id") for t in tombs if
                           _integer(_need(t, "prune_due_at")) <= watermark and
                           _integer(_need(t, "prune_due_mono")) <= _integer(_need(witness, "as_of_mono"))}
                    pruned = {t.get("id") for t in trace if t.get("action") == "prune" and
                              _integer(_need(t, "received_at")) <= _integer(_need(witness, "as_of")) and
                              _integer(_need(t, "received_mono")) <= _integer(_need(witness, "as_of_mono"))}
                    _eq(pruned, due, "prune watermark lacks completed tomb transitions")
    elif row == "B15":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            for cp in _items(_need(f, "checkpoints")):
                retained, open_seqs = _need(cp, "retained_seqs"), _need(cp, "open_seqs")
                if not isinstance(retained, list) or not isinstance(open_seqs, list):
                    raise _EvidenceMissing("retained/open seq input malformed")
                _assert(all(type(seq) is int and seq >= 0 for seq in retained + open_seqs))
                _eq(len(retained), len(set(retained)))
                _eq(len(open_seqs), len(set(open_seqs)))
                _eq(_need(cp, "oldest_retained_seq"), min(retained) if retained else None)
                _eq(_need(cp, "latest_open_seq"), max(open_seqs) if open_seqs else None)
                for probe in _items(_need(cp, "cursor_probes")):
                    entries = _need(probe, "entry_seqs")
                    _assert(all(type(s) is int and s > _need(probe, "after_seq") for s in entries))
                    _eq(entries, sorted(set(entries)))
                    _assert(set(entries) <= set(open_seqs))
                    _eq(_need(probe, "next_seq"), entries[-1] if entries and
                        _need(probe, "has_more") is True else None)
                    _eq(_need(probe, "has_more"), bool([s for s in open_seqs if s >
                        (entries[-1] if entries else probe["after_seq"])]))
                for audit in _items(_need(cp, "identity_index_audit", "samples")):
                    _identity_absence(audit)
                order = _integer(_need(cp, "event_order"))
                prefix = [t for t in trace if _integer(_need(t, "event_order")) <= order]
                registered = {_integer(_need(t, "registered_seq")) for t in prefix
                              if t.get("action") == "register" and t.get("classification") == "registered"}
                retired = {_integer(_need(t, "registered_seq")) for t in prefix
                           if t.get("action") == "prune"}
                open_removed = {_integer(_need(t, "registered_seq")) for t in prefix
                                if t.get("action") in ("finish", "close", "prune")}
                _eq(set(retained), registered - retired, "retained seq differs from transition trace")
                _eq(set(open_seqs), registered - open_removed, "open seq differs from transition trace")
                probes = _need(cp, "cursor_probes")
                if retained:
                    _assert(any(_need(p, "after_seq") < min(retained) for p in probes),
                            "stale cursor probe absent")
                if open_seqs:
                    _assert(any(_need(p, "after_seq") >= max(open_seqs) for p in probes),
                            "latest cursor probe absent")
                else:
                    _assert(any(_need(p, "entry_seqs") == [] for p in probes),
                            "empty latest cursor probe absent")
    elif row == "B16":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            cps = _items(_need(f, "checkpoints"), 2)
            if not any("passive_cohort_projection" in cp for cp in cps):
                raise _EvidenceMissing("passive cohort projection missing")
            public = {c.get("checkpoint_id"): c for c in cps}
            required_sources = (set(REGISTRY) if not unit else
                                {_need(t, "source") for t in trace if t.get("action") == "register"})
            for cp in cps:
                if "passive_cohort_projection" not in cp: continue
                projections = _items(cp["passive_cohort_projection"])
                _eq({_need(p, "source") for p in projections}, required_sources,
                    "recent cohort passive projection omitted a source")
                _eq(len(projections), len(required_sources))
                for projection in projections:
                    _eq(_need(cp, "observation"), "passive")
                    pub = _need(public, _need(projection, "public_checkpoint_id"))
                    source = _need(projection, "source")
                    _eq(set(_need(pub, "recent_cohorts")), required_sources,
                        "recent cohort public response omitted a source")
                    actual = _need(pub, "recent_cohorts", source)
                    _eq(_need(projection, "counts"), _need(actual, "counts"))
                    _eq(_need(projection, "range"), _need(actual, "range"))
                    counts = _need(actual, "counts")
                    registered = _integer(_need(counts, "registered"))
                    _eq(registered, sum(_need(counts, "connection").values()))
                    _eq(registered, sum(_need(counts, "lifecycle").values()))
                    _assert(all(_need(actual, "equations_hold", key) is True for key in ("connection", "lifecycle")))
                    probe = _need(pub, "old_cohort_probe")
                    probe_range = _items(_need(probe, "range"), 2)
                    _eq(len(probe_range), 2)
                    _assert(_integer(probe_range[0]) < _need(pub, "cohort_exact_from"))
                    _assert(_integer(probe_range[0]) < _integer(probe_range[1]))
                    _eq(_integer(probe_range[1]), _integer(_need(pub, "cohort_exact_from")))
                    _eq(_need(probe, "classification"), "cohort_expired")
                    _assert("cohort_expired" in _need(probe, "diagnostics", "codes"))
                    _assert(not any(key in probe for key in
                                    ("counts", "registered", "connection", "lifecycle", "equations_hold")),
                            "expired cohort returned a partial denominator")
                    range_pair = _items(_need(actual, "range"), 2)
                    _eq(len(range_pair), 2)
                    start, end = map(_integer, range_pair)
                    _eq(end, _integer(_need(pub, "received_at")))
                    _assert(start < end and start >= _integer(_need(pub, "cohort_exact_from")))
                    if not unit:
                        _eq(start, max(_integer(_need(pub, "cohort_exact_from")), end - 60 * MINUTE))
                    cohort = [t for t in trace if t.get("action") == "register" and
                              t.get("source") == source and
                              start <= _integer(_need(t, "received_at")) < end and
                              start <= _integer(_need(t, "received_mono")) <
                              _integer(_need(pub, "received_mono"))]
                    _eq(counts["registered"], len(cohort), "cohort range input count differs")
                    _eq(counts["connection"], dict(Counter(_need(t, "connection") for t in cohort)))
                    lifecycle = Counter()
                    for registration in cohort:
                        completions = [t for t in trace if t.get("action") == "finish" and
                                       t.get("id") == _need(registration, "id") and
                                       _integer(_need(t, "received_at")) <= _integer(_need(pub, "received_at")) and
                                       _integer(_need(t, "received_mono")) <= _integer(_need(pub, "received_mono"))]
                        state = _need(completions[-1], "classification") if completions else _need(registration, "lifecycle")
                        lifecycle[state] += 1
                    _eq(counts["lifecycle"], dict(lifecycle))
    elif row == "B17":
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            cps = _items(_need(f, "checkpoints"), 2)
            if not any("passive_epoch_projection" in cp for cp in cps):
                raise _EvidenceMissing("passive epoch projection missing")
            public = {c.get("checkpoint_id"): c for c in cps}
            sources = set(_need(f, "source_registered"))
            _eq(sources, {_need(t, "source") for t in trace if t.get("action") == "register"},
                "source input coverage incomplete")
            for cp in cps:
                if "passive_epoch_projection" not in cp: continue
                projections = _items(cp["passive_epoch_projection"])
                _eq({_need(p, "source") for p in projections}, sources,
                    "passive epoch omitted a source")
                _eq(len(projections), len(sources), "passive epoch duplicated a source")
                for projection in projections:
                    pub = _need(public, _need(projection, "public_checkpoint_id"))
                    source = _need(projection, "source")
                    entries = _items(_need(pub, "epoch_sources"))
                    _eq({_need(e, "source") for e in entries}, sources,
                        "public epoch omitted a source")
                    _eq(len(entries), len(sources), "public epoch duplicated a source")
                    entry = _items([e for e in entries if e.get("source") == source])[0]
                    for key in ("registered_invocations", "frozen_invocations", "live_invocations",
                                "connection_counts", "lifecycle_counts"):
                        _eq(_need(projection, key), _need(entry, key))
                    registered = _integer(_need(entry, "registered_invocations"))
                    _eq(registered, _need(entry, "frozen_invocations") + _need(entry, "live_invocations"))
                    _eq(registered, sum(_need(entry, "connection_counts").values()))
                    _eq(registered, sum(_need(entry, "lifecycle_counts").values()))
                    _eq(registered, _need(f, "source_registered", source))
                    _eq(registered, sum(t.get("action") == "register" and t.get("source") == source for t in trace))
                    _assert(all(_need(entry, "equations_hold", key) is True for key in ("connection", "lifecycle")))
                    _eq(_need(pub, "N_total"), sum(e["registered_invocations"] for e in pub["epoch_sources"]))
                    _eq(_need(pub, "N_live"), sum(e["live_invocations"] for e in pub["epoch_sources"]))
    elif row == "B18":
        for f in fixtures:
            trace = _need(f, "fixture_provenance", "api_trace")
            previous_event = -1
            for step in trace:
                event = _integer(_need(step, "event_order"))
                _assert(event >= previous_event, "API event order regressed")
                previous_event = event
            cursor = 0
            previous_order = -1
            causes = ("retention_expired", "expired_start", "expired_finish", "expired_wrapper",
                      "expired_identity_unverified", "init_failed")
            totals = Counter()
            affected = set()
            for cp in _items(_need(f, "checkpoints")):
                counters = _need(cp, "diagnostic_counters")
                order = _integer(_need(cp, "event_order"))
                _assert(order >= previous_order, "checkpoint order regressed")
                previous_order = order
                while cursor < len(trace) and _integer(_need(trace[cursor], "event_order")) <= order:
                    step = trace[cursor]
                    classification = step.get("classification")
                    if classification in causes:
                        totals[classification] += 1
                        affected.add(_need(step, "source"))
                    cursor += 1
                for key in (*causes, "clock_unverified"):
                    _integer(_need(counters, key))
                _eq(_need(counters, "clock_unverified"), 0)
                isolation = _need(cp, "clock_isolation")
                _assert(_need(isolation, "active") is False and _need(isolation, "candidate_pairs") == 0,
                        "normal trace entered clock isolation")
                _assert(_need(cp, "admission_stopped") is False)
                _eq(set(_need(cp, "uncertain_sources")), affected,
                    "uncertain source differs from cause input")
                _eq(_need(cp, "coverage_complete"), not affected)
                for cause in causes:
                    _eq(_need(counters, cause), totals[cause])
    elif row == "B19":
        scenarios = {s.get("name"): s for s in _items(_need(report, "scenarios"))}
        for f in fixtures:
            trace = _items(_need(f, "fixture_provenance", "api_trace"))
            counts = Counter()
            cursor = 0
            cps = _items(_need(f, "checkpoints"))
            legacy_unit = (unit and len(cps) == 1 and "event_order" not in cps[0] and
                           all("event_order" not in t for t in trace))
            if legacy_unit:
                counts.update(_need(t, "classification") for t in trace)
            else:
                previous_event = -1
                for step in trace:
                    event = _integer(_need(step, "event_order"))
                    _assert(event >= previous_event, "API event order regressed")
                    previous_event = event
            previous_order = -1
            for cp in cps:
                if not legacy_unit:
                    order = _integer(_need(cp, "event_order"))
                    _assert(order >= previous_order, "checkpoint order regressed")
                    previous_order = order
                    while cursor < len(trace) and _integer(_need(trace[cursor], "event_order")) <= order:
                        counts[_need(trace[cursor], "classification")] += 1
                        cursor += 1
                _eq(_need(cp, "transition_counts"), dict(counts))
                refs = _items(_need(cp, "measurement_refs"))
                _eq(len(set(refs)), len(refs))
                for classification, count in counts.items():
                    if not count: continue
                    _assert(any(_need(scenarios, ref, "measured_transition") == classification for ref in refs))
                for ref in refs:
                    scenario = _need(scenarios, ref)
                    _observed_timing(scenario)
                    _measured_time_gate(scenario, required_n=1 if unit else 1000)
                    _temporary(scenario)
                    _eq(_need(scenario, "status"), "PASS")
                if counts.get("prune", 0):
                    _assert(any(_need(scenarios, ref, "measured_transition") == "prune"
                                for ref in refs), "prune requires independent measurement")
    elif row == "B20":
        kinds = _need(report, "test_scale", "fault_kinds") if unit else None
        scenarios = _items(_need(report, "scenarios"))
        faults = [s for s in scenarios if "fault_evidence" in s]
        if kinds is not None: _eq({s["fault_evidence"]["fault_kind"] for s in faults}, set(kinds))
        expected_classes = {"clock_step": "clock_unverified",
                            "merge_failure": "CumulativeMergeFailureForTest",
                            "late_after_expiry": "expired_finish",
                            "id_collision": "registration_conflict",
                            "uuid_uniqueness": "registered"}
        for s in _items(faults):
            _assert(_need(s, "classification_ok") is True)
            _assert(_need(s, "sample_checks", "checked") > 0)
            _eq(_need(s, "sample_checks", "failures"), [])
            _need(s, "first_call", "classification")
            evidence = _need(s, "fault_evidence")
            kind = _need(evidence, "fault_kind")
            if kind not in expected_classes:
                raise _EvidenceMissing("unknown fault kind")
            _eq(_need(s, "first_call", "classification"), expected_classes[kind],
                "fault classification differs from fault kind")
            coverage = _need(evidence, "expected_coverage")
            affected = _items(_need(evidence, "affected_sources"))
            _eq(_need(coverage, "uncertain_sources"), affected)
            _eq(_need(coverage, "complete"), False)
            _eq(_need(evidence, "watermark_before"), _need(evidence, "watermark_after"))
            _eq(_need(evidence, "normal_schedule_before"),
                _need(evidence, "normal_schedule_after"),
                "fault entered the normal schedule")
            fault_trace = _items(_need(evidence, "fault_trace"))
            _eq(_need(fault_trace[0], "classification"), expected_classes[kind])
            _eq(_need(fault_trace[0], "source"), affected[0])
            _eq(_need(fault_trace[0], "watermark_after"), _need(evidence, "watermark_before"),
                "fault published a watermark")
            retry = _need(evidence, "retry_admission")
            _eq(_need(retry, "attempted"), _need(retry, "registered") + _need(retry, "rejected"))
            _eq(_need(retry, "rejected"), 0)
            registered = [t for t in fault_trace[1:] if t.get("action") == "register" and
                          t.get("classification") == "registered"]
            _eq(len(registered), _need(retry, "registered"),
                "retry/new admission trace differs from summary")
            _assert(all(_need(t, "source") in affected for t in registered))
            if kind == "merge_failure":
                _assert(any(t.get("action") == "retry" and
                            t.get("classification") == "published" and
                            _need(t, "watermark_after") != _need(evidence, "watermark_before")
                            for t in fault_trace[1:]), "merge retry did not publish")
            if kind == "uuid_uniqueness":
                ids = [_need(t, "id") for t in registered]
                _eq(len(ids), len(set(ids)), "UUID uniqueness counterexample reused an ID")
            _assert(_need(evidence, "normal_schedule_counted") is False)
    elif row == "B21":
        for f in fixtures:
            trace = _items(_need(f, "resident_trace"))
            maximum = max(trace)
            _eq(_need(f, "max_resident_observed"), maximum)
            _assert(maximum > 0 and maximum <= (4929 if f.get("auxiliary_registered", 0) else 2880))
            cps = _items(_need(f, "checkpoints"))
            _assert(all(_need(cp, "N_res") <= maximum for cp in cps))
            _assert(any(_need(cp, "pruned_since_previous_hour") > 0 for cp in cps if cp.get("kind") == "hour"))
            tail = _items([cp for cp in cps if cp.get("kind") == "tail_80641"])[0]
            _assert(any(s.get("observed") == "expired_or_untracked" for s in _need(tail, "identity_samples")))
            events = _items(_need(f, "resident_events"))
            api = _items(_need(f, "fixture_provenance", "api_trace"))
            orders = [_integer(_need(e, "event_order")) for e in events]
            _assert(all(previous < current for previous, current in zip(orders, orders[1:])),
                    "resident events omitted or reused an order")
            _eq([_integer(_need(e, "N_res")) for e in events], trace,
                "resident trace differs from every event")
            _eq([(_need(e, "event_order"), _need(e, "action"), _need(e, "id")) for e in events],
                [(_need(t, "event_order"), _need(t, "action"), _need(t, "id")) for t in api],
                "resident event omitted an API transition")
            cursor = 0
            previous_order = -1
            for cp in cps:
                order = _integer(_need(cp, "event_order"))
                _assert(order >= previous_order, "checkpoint order regressed")
                previous_order = order
                while cursor < len(events) and orders[cursor] <= order:
                    cursor += 1
                if cursor == 0:
                    raise _EvidenceMissing("checkpoint has no preceding resident event")
                _eq(_need(cp, "N_res"), _need(events[cursor - 1], "N_res"))
            initial = {_need(s, "id") for s in _need(tail, "identity_samples")
                       if s.get("observed") == "expired_or_untracked"}
            _assert(any(t.get("action") == "expire" and t.get("id") in initial for t in api),
                    "initial identity never expired")
            _assert(any(t.get("action") == "prune" and t.get("id") in initial for t in api),
                    "initial identity never pruned")
    elif row == "B22":
        if not unit:                       # size-step 은 판정하는 인터프리터에서 재계산하므로 보고서와 같은 CPython 이어야 한다
            env = _need(report, "environment")
            produced = str(_need(env, "python")).split()[0]
            if (produced != platform.python_version()
                    or _need(env, "implementation") != platform.python_implementation()):
                raise _EvidenceMissing(f"report CPython {produced} differs from judging CPython "
                                       f"{platform.python_version()}")
            if _need(env, "python_hash_seed") != "0":
                raise _EvidenceMissing("report CPython hash seed lock is not 0")
        proof = _need(report, "capacity_proof")
        m = _need(report, "test_scale", "max_records") if unit else 131072
        j = _need(report, "test_scale", "job_key_limit") if unit else 12
        steps = _need(proof, "budget_q_size_steps")
        _eq(_need(steps, "at_count"), m)
        _eq(_need(steps, "list_header_bytes"), sys.getsizeof([]))
        _eq(_need(steps, "list_slot_bytes"), sys.getsizeof([None]) - sys.getsizeof([]))
        _eq(_need(steps, "block_width"), 257)
        _eq(_need(steps, "detail_cap"), min(m, 2048))
        actual_dict, actual_set = _runtime_budget_steps(m)
        _eq(_need(steps, "dict_steps"), actual_dict)
        _eq(_need(steps, "set_steps"), actual_set)
        _eq(_need(steps, "computed_q_at_limit"), _actual_budget_q(m, steps))
        _assert(_need(proof, "H_res") <= m and _need(proof, "H_job") <= j)
        _eq(_need(proof, "job_key_limit"), j)
        _eq(_need(proof, "Q_cap_bytes"), _actual_budget_q(m, steps) + 512 * m + 1536 * j)
        _assert(_need(proof, "Q_obs_bytes") <= _need(proof, "Q_cap_bytes"))
        ownership = _need(proof, "F4_ownership")
        _eq(_need(ownership, "total_bytes"), 18_874_368)
        _eq(sum(_need(i, "bytes") for i in _items(_need(ownership, "items"))), 18_874_368)
        _assert(all(_need(i, "owner") == "F_4" for i in ownership["items"]))
        paths = [_need(i, "path") for i in ownership["items"]]
        _eq(len(paths), len(set(paths)), "F4 ownership path duplicated")
        fixtures = _items(_need(report, "churn_fixtures"))
        observed_keys = set()
        for f in fixtures:
            counts = _need(f, "job_key_counts")
            registered = [t for t in _items(_need(f, "fixture_provenance", "api_trace"))
                          if t.get("action") == "register" and t.get("classification") == "registered"]
            _check_job_key_counts(counts, registered)
            observed_keys.update((_need(entry, "source"), _need(entry, "job_id")) for entry in counts)
        _assert(_need(proof, "H_job") <= len(observed_keys), "H_job lacks job-key input")
    elif row == "B23":
        fixtures = _items(_need(report, "pressure_fixtures"))
        applicable = fixtures if unit else [p for p in fixtures
                                             if p.get("name") == "pressure_unique_job_keys_byte_stop"]
        if not unit:
            _eq(len(applicable), 1)
        for p in applicable:
            r = _need(p, "first_rejection")
            before = _need(p, "before")
            _assert(_need(before, "N_res") + 1 < 131072)
            _budget_order(before)
            _assert(_need(r, "slot_test", "would_exceed") is False)
            _eq(_need(r, "slot_test", "candidate_N_res"), _need(before, "N_res") + 1)
            _eq(_need(r, "byte_precheck", "would_exceed"), _need(r, "byte_precheck", "candidate_E") > _need(before, "B"))
            _eq(_need(r, "cause"), "byte")
            _assert(_need(r, "no_insertion") is True)
            _index_audit(_need(r, "index_audit"))
            _assert(_need(p, "post_latch", "resumed_after_release") is False, "latch reopened")
            control = _need(p, "control_existing_key")
            _eq(_need(control, "classification"), "registered")
            _eq(_need(control, "branch"), "detached_clone")
            digest = _need(p, "fixture_provenance", "pre_rejection_state_sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise _EvidenceMissing("pre-rejection state digest absent or malformed")
            _eq(_need(control, "pre_rejection_sha256"), digest)
            _eq(_need(control, "pre_rejection_sha256"), _need(control, "clone_base_sha256"))
            registered_keys = {(_need(key, "source"), _need(key, "job_id")) for key in
                               _items(_need(p, "job_key_counts"))}
            _assert((_need(control, "source"), _need(control, "job_id")) in registered_keys,
                    "control key was not registered before the rejection")
            trace = _items(_need(p, "fixture_provenance", "api_trace"))
            rejected_at = _integer(_need(r, "trace_call_index"))
            _eq(_need(control, "forked_before_call_index"), rejected_at)
            pressure = [t for t in trace if _integer(_need(t, "call_index")) <= rejected_at]
            keys = [(_need(t, "source"), _need(t, "job_id")) for t in pressure]
            _eq(len(keys), len(set(keys)), "unique-key pressure reused a key")
            rejection = _items([t for t in pressure if _need(t, "call_index") == rejected_at])[0]
            _eq(_need(rejection, "job_id"), _need(r, "candidate_job_id"))
            _assert((_need(rejection, "source"), _need(rejection, "job_id")) not in registered_keys,
                    "rejected candidate key was already registered")
            _eq(_need(rejection, "classification"), "admission_stopped")
            _assert(all(_need(t, "classification") == "registered" for t in pressure[:-1]))
            _check_job_key_counts(_need(p, "job_key_counts"),
                                  [t for t in pressure if t.get("classification") == "registered"])
            clone_trace = _items(_need(p, "fixture_provenance", "clone_api_trace"))
            control_call = _items([t for t in clone_trace if t.get("call_index") ==
                                   _need(control, "trace_call_index")])[0]
            _eq(_need(control_call, "job_id"), _need(control, "job_id"))
            _eq(_need(control_call, "source"), _need(control, "source"))
            _eq(_need(control_call, "classification"), "registered")
            _eq(_need(control_call, "branch"), "detached_clone")
            for point in (before, _need(p, "after")):
                _budget_order(point)
            _assert(all(_need(t, "classification") == "admission_stopped" for t in
                        trace if t.get("action") == "register" and
                        _need(t, "call_index") > rejected_at),
                    "byte latch resumed a new-key admission")


def _actual_budget_q(count, steps):
    def stepped(table, n):
        return max((row for row in table if row[0] <= n), key=lambda row: row[0])
    def backing(n):
        return 0 if n == 0 else ((n + (n >> 3) + 9) & ~3) * steps["list_slot_bytes"]
    d = stepped(_need(steps, "dict_steps"), count)
    s = stepped(_need(steps, "set_steps"), count)
    blocks = min(3, count) + count // 128
    details = min(count, 2048)
    return (d[2] - 64 + backing(count) + 2 * (d[1] - 64) + s[1] - 216
            + 6 * backing(blocks) + blocks * (steps["list_header_bytes"] + backing(257))
            + backing(details) + d[1] - 64 +
            details * (steps["list_header_bytes"] + backing(1)))


def _runtime_budget_steps(count):
    if type(count) is not int or count < 0 or count > 131072:
        raise _EvidenceMissing("invalid budget count")
    generic, unicode, entries = {}, {}, set()
    dict_steps = [[0, sys.getsizeof(generic), sys.getsizeof(unicode)]]
    set_steps = [[0, sys.getsizeof(entries)]]
    for n in range(1, count + 1):
        key = n - 1
        generic[key] = key
        unicode[str(key)] = key
        entries.add(key)
        size = [n, sys.getsizeof(generic), sys.getsizeof(unicode)]
        if size[1:] != dict_steps[-1][1:]: dict_steps.append(size)
        set_size = [n, sys.getsizeof(entries)]
        if set_size[1] != set_steps[-1][1]: set_steps.append(set_size)
    return dict_steps, set_steps


def _attempts(fixture, attachments, unit):
    h = _need(fixture, "highwater_checks")
    if unit and "register_attempts" in h:
        return _items(h["register_attempts"])
    cache = _finalize_cache.get()
    key = id(fixture)
    if cache is not None and key in cache["attempts"]:
        return cache["attempts"][key]
    ref = _need(h, "register_attempts_ref")
    path = _need(ref, "path")
    if not _safe_ref(path): raise _EvidenceMissing("unsafe attachment path")
    blob = _need(attachments, path)
    if not isinstance(blob, bytes): raise _EvidenceMissing("attachment bytes missing")
    if hashlib.sha256(blob).hexdigest() != _need(ref, "sha256"):
        raise _EvidenceMissing("attachment hash mismatch")
    try:
        lines = gzip.decompress(blob).splitlines()
        rows = [json.loads(line) for line in lines]
    except (OSError, ValueError, UnicodeError) as exc:
        raise _EvidenceMissing("invalid gzip JSONL attachment") from exc
    if len(rows) != _need(ref, "rows"):
        raise _EvidenceMissing("attachment row count mismatch")
    _items(rows)
    if rows[0].get("register_seq") != _need(ref, "first_seq") or rows[-1].get("register_seq") != _need(ref, "last_seq"):
        raise _EvidenceMissing("attachment sequence bounds mismatch")
    if cache is not None:
        cache["attempts"][key] = rows
    return rows


def _safe_ref(path):
    return (isinstance(path, str) and path and not Path(path).is_absolute() and
            all(part not in ("..", "") for part in Path(path).parts))


def _validate_attempts(rows, *, require_budget=False, require_complete=True):
    if [r.get("register_seq") for r in rows] != list(range(len(rows))):
        raise _EvidenceMissing("register sequence gap")
    for r in rows:
        _need(r, "input")
        _need(r, "classification")
        _need(r, "before")
        _need(r, "after")
        if require_complete:
            _need(r, "selected")
            layout = _need(r, "normalized_layout")
            _need(layout, "before")
            _need(layout, "after")
        if require_budget:
            _budget_order(r["before"])
            _budget_order(r["after"])


def _validate_attempt_stream(rows, *, unit, highwater):
    """Check the register ledger against its own input and adjacent registrations."""
    previous = None
    phases = Counter()
    for row in rows:
        input_row = _need(row, "input")
        for key in ("invocation_id", "source", "job_id", "received_at", "received_mono",
                    "started_at", "started_mono", "minute_index", "auxiliary"):
            _need(input_row, key)
        before, after = _need(row, "before"), _need(row, "after")
        for side in (before, after):
            for key in ("N_total", "N_res", "H_res", "H_job"):
                _integer(_need(side, key))
            _budget_order(side)
        classification = _need(row, "classification")
        if classification == "registered":
            _eq(after["N_total"], before["N_total"] + 1)
        elif classification == "rejected":
            _eq(after["N_total"], before["N_total"])
        else:
            raise _EvidenceViolation("register attempt classification changed")
        _assert(after["H_res"] >= before["H_res"] and after["H_job"] >= before["H_job"])
        if previous is not None:
            _eq(before["N_total"], previous["N_total"], "register sequence N_total discontinuity")
            _assert(before["H_res"] >= previous["H_res"] and before["H_job"] >= previous["H_job"])
        previous = after
        if not unit:
            phase = _need(input_row, "phase")
            _assert(phase in ("burst", "probe", "schedule", "tail"))
            phases[phase] += 1
    if not unit:
        _assert(_need(highwater, "first_pass_complete") is True,
                "register attempt first pass did not complete")
        _eq(phases["schedule"] + phases["tail"], 80641)
        _eq(phases["tail"], 1)
        _assert(all(row["classification"] == "registered" for row in rows
                    if row["input"]["phase"] in ("burst", "probe", "schedule", "tail")))


def _layout_changed(layout):
    before, after = _need(layout, "before"), _need(layout, "after")
    for side in (before, after):
        for item in side:
            for key in ("path", "size_bytes", "share_group", "q_attributed_bytes", "includes_deleted_dummy"):
                _need(item, key)
    return before != after


def _no_rebuild(report):
    events = _need(report, "rebuild_events")
    if events != []: return False
    for f in _items(_need(report, "churn_fixtures")):
        for cp in _items(_need(f, "checkpoints")):
            cap = _need(cp, "budget", "capacity")
            if (_need(cp, "rebuild_count") != 0 or _need(cap, "rebuild_old_bytes") != 0
                    or _need(cap, "rebuild_new_bytes") != 0): return False
    scenarios = _items(_need(report, "scenarios"))
    rebuild = _items([s for s in scenarios if s.get("name") == "rebuild_double_backing"])[0]
    _eq(_need(rebuild, "status"), "N/A")
    return _need(rebuild, "reason") == "N/A (no rebuild in this implementation)"


def _evaluate_c(row, report, attachments):
    unit = report.get("mode") == "predicate_unit"
    if row in ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08"):
        fixtures = _items(_need(report, "churn_fixtures"))
    if row == "C01":
        for f in fixtures:
            h = _need(f, "highwater_checks")
            attempts = _attempts(f, attachments, unit)
            _validate_attempts(attempts, require_complete=False)
            selected = _items(_need(h, "checks"))
            actual_events = [step["register_seq"] for step in attempts
                             if step["classification"] == "registered" and
                             (step["after"]["H_res"] > step["before"]["H_res"] or
                              step["after"]["H_job"] > step["before"]["H_job"])]
            _eq([_need(check, "register_seq") for check in selected], actual_events)
            res_count = job_count = both_count = 0
            for check in selected:
                seq = _integer(_need(check, "register_seq"))
                attempt = _need(attempts, seq)
                for side in ("before", "after"):
                    for key in ("H_res", "H_job"):
                        _eq(_need(check, side, key), _need(attempt, side, key))
                res = check["after"]["H_res"] > check["before"]["H_res"]
                job = check["after"]["H_job"] > check["before"]["H_job"]
                res_count += res; job_count += job; both_count += res and job
                _assert(res or job)
                _eq(_need(check, "N_total_before"), _need(attempt, "before", "N_total"))
                _eq(_need(check, "N_total_after"), _need(attempt, "after", "N_total"))
                for key in ("received_at", "received_mono", "minute_index"):
                    _eq(_need(check, key), _need(attempt, "input", key))
            _eq(_need(h, "resident_events"), res_count)
            _eq(_need(h, "job_events"), job_count)
            _eq(_need(h, "both_events"), both_count)
            _eq(_need(h, "events"), len(selected))
    elif row == "C02":
        for f in fixtures:
            h = _need(f, "highwater_checks")
            attempts = _attempts(f, attachments, unit)
            checks = _items(_need(h, "checks"))
            point_rows = _items(_need(report, "capacity_proof", "checkpoints"), 2)
            points = {_need(p, "checkpoint_id"): p for p in point_rows}
            _eq(len(points), len(point_rows), "capacity checkpoint ID reused")
            cps = _items(_need(f, "checkpoints"), 2)
            check_seqs = [_integer(_need(check, "register_seq")) for check in checks]
            _eq(len(set(check_seqs)), len(check_seqs), "highwater sequence reused")
            check_by_seq = dict(zip(check_seqs, checks))
            first_violation = h.get("first_violation")
            if isinstance(first_violation, dict):
                violation_seq = _integer(_need(first_violation, "register_seq"))
                if violation_seq not in check_by_seq: check_by_seq[violation_seq] = first_violation
            changed = preserved = violations = 0
            paired_refs = set()
            paired_cps = defaultdict(list)
            for cp in cps:
                if cp.get("kind") in ("highwater_before", "highwater_after"):
                    paired_cps[(cp.get("register_seq"), cp["kind"])].append(cp)
            for seq, attempt in enumerate(attempts):
                _eq(_need(attempt, "register_seq"), seq)
                check = check_by_seq.get(seq)
                layout = _need(attempt, "normalized_layout")
                if check is not None and "normalized_layout" in check:
                    _eq(_need(check, "normalized_layout"), layout)
                backing = _layout_changed(layout)
                if seq in check_seqs: _eq(_need(check, "backing_changed"), backing)
                before = attempt.get("before") if "before" in attempt else _need(check, "before")
                after = attempt.get("after") if "after" in attempt else _need(check, "after")
                if check is not None and "before" in attempt and "after" in attempt:
                    for side, measured in (("before", before), ("after", after)):
                        for key in ("H_job", "Q_actual", "Q_4", "G", "E", "B"):
                            _eq(_need(check, side, key), _need(measured, key))
                violation = any(
                    _need(point, "G") > _need(point, "E") or
                    _need(point, "E") > _need(point, "B") or
                    _need(point, "Q_actual") > _need(point, "Q_4")
                    for point in (before, after)
                )
                selected = (backing or _need(after, "H_job") > _need(before, "H_job") or
                            (violation and violations == 0))
                _eq(_need(attempt, "selected"), selected)
                if selected:
                    if check is None: raise _EvidenceMissing("selected highwater check missing")
                if seq in check_seqs:
                    changed += backing
                    preserved += bool(_need(check, "preserved"))
                    _eq(_need(check, "preserved"), selected)
                violations += violation
                if selected:
                    def paired(kind):
                        matching = paired_cps[(seq, kind)]
                        if not matching:
                            raise _EvidenceMissing(f"{kind} pair for register_seq {seq} missing")
                        _eq(len(matching), 1, f"{kind} pair for register_seq {seq} duplicated")
                        return matching[0]
                    before_cp = paired("highwater_before")
                    after_cp = paired("highwater_after")
                    for side, cp in (("before", before_cp), ("after", after_cp)):
                        measured = _need(check, side)
                        _budget_order(measured)
                        ref = _need(cp, "capacity_ref")
                        _assert(ref not in paired_refs, "selected capacity point reused")
                        paired_refs.add(ref)
                        point = _need(points, ref)
                        _container_sum(point)
                        _eq(_need(measured, "Q_actual"), _need(point, "Q_actual_bytes"))
                        _eq(_need(measured, "Q_4"), _need(point, "Q_4_bytes"))
            _eq(_need(h, "backing_changes"), changed)
            _eq(_need(h, "preserved_pairs"), preserved)
            _eq(_need(h, "violations"), violations)
    elif row == "C03":
        for f in fixtures:
            attempts = _attempts(f, attachments, unit)
            cps = _items(_need(f, "checkpoints"), 2)
            paired_cps = defaultdict(list)
            for cp in cps:
                if cp.get("kind") in ("highwater_before", "highwater_after"):
                    paired_cps[(cp.get("register_seq"), cp["kind"])].append(cp)
            point_rows = _items(_need(report, "capacity_proof", "checkpoints"), 2)
            points = {p.get("checkpoint_id") for p in point_rows}
            _eq(len(points), len(point_rows))
            refs = set()
            trace = _need(f, "fixture_provenance", "api_trace")
            public = _need(f, "fixture_provenance", "public_query_trace")
            trace_blocked = [_integer(_need(event, "event_order")) for event in trace
                             if event.get("action") in ("link_round", "finish")]
            public_blocked = [_integer(_need(event, "event_order")) for event in public]
            for orders in (trace_blocked, public_blocked):
                _assert(all(a <= b for a, b in zip(orders, orders[1:])),
                        "event order regressed")
            blocked = list(heapq.merge(trace_blocked, public_blocked))
            blocked_cursor = 0
            previous_order = -1
            for attempt in attempts:
                if not _need(attempt, "selected"): continue
                seq = _need(attempt, "register_seq")
                before_rows = paired_cps[(seq, "highwater_before")]
                after_rows = paired_cps[(seq, "highwater_after")]
                before = _items(before_rows)[0]
                after = _items(after_rows)[0]
                _eq(len(before_rows), 1)
                _eq(len(after_rows), 1)
                order = _integer(_need(attempt, "event_order"))
                _assert(order >= previous_order, "selected attempt order regressed")
                previous_order = order
                after_order = _integer(_need(after, "event_order"))
                _assert(_need(before, "event_order") < order < after_order)
                while blocked_cursor < len(blocked) and blocked[blocked_cursor] <= order:
                    blocked_cursor += 1
                _assert(blocked_cursor == len(blocked) or blocked[blocked_cursor] > after_order,
                        "highwater pair followed a public query, link, or finish")
                for cp in (before, after):
                    _eq(_need(cp, "observation"), "passive")
                    for key in ("received_at", "received_mono", "minute_index"):
                        _eq(_need(cp, key), _need(attempt, "input", key))
                    ref = _need(cp, "capacity_ref")
                    _assert(ref in points)
                    _assert(ref not in refs, "capacity point reused")
                    refs.add(ref)
    elif row == "C04":
        for f in fixtures:
            h = _need(f, "highwater_checks")
            attempts = _attempts(f, attachments, unit)
            _validate_attempts(attempts, require_budget=True)
            _validate_attempt_stream(attempts, unit=unit, highwater=h)
            replay = _need(h, "replay")
            selected = [r["register_seq"] for r in attempts if r["selected"]]
            if selected:
                _assert(_need(replay, "performed") is True)
                _eq(_need(replay, "stopped_after_seq"), max(selected))
                _eq(_need(replay, "compared_steps"), max(selected) + 1)
            else:
                _assert(_need(replay, "performed") is False)
                _eq(_need(replay, "compared_steps"), 0)
            _eq(_need(replay, "mismatch"), None)
            if selected:
                digest = _need(replay, "comparison_digest_by_seq")
                for seq in selected:
                    record = _need(digest, seq)
                    _eq(_need(record, "register_seq"), seq)
                    source = {key: _need(attempts[seq], key) for key in
                              ("input", "classification", "before", "after", "normalized_layout", "selected")}
                    raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    _eq(_need(record, "pass1_sha256"), hashlib.sha256(raw).hexdigest())
    elif row == "C05":
        for f in fixtures:
            h = _need(f, "highwater_checks")
            attempts = _attempts(f, attachments, unit)
            replay = _need(h, "replay")
            digest = _need(replay, "comparison_digest_by_seq")
            selected = [r["register_seq"] for r in attempts if _need(r, "selected")]
            if not selected:
                _assert(_need(replay, "performed") is False)
                _eq(_need(replay, "compared_steps"), 0)
                _eq(digest, [])
            else:
                final = max(selected)
                _assert(_need(replay, "performed") is True)
                _eq(_need(replay, "stopped_after_seq"), final)
                _eq(_need(replay, "compared_steps"), final + 1)
                if len(digest) != final + 1: raise _EvidenceMissing("replay digest prefix incomplete")
                if not unit:
                    _assert(_need(h, "first_pass_complete") is True,
                            "first pass did not finish independently of replay")
                checks = {_need(c, "register_seq"): c for c in _items(_need(h, "checks"))}
                cps = _items(_need(f, "checkpoints"), 2)
                paired_cps = defaultdict(list)
                for cp in cps:
                    if cp.get("kind") in ("highwater_before", "highwater_after"):
                        paired_cps[(cp.get("register_seq"), cp["kind"])].append(cp)
                point_rows = _items(_need(report, "capacity_proof", "checkpoints"), 2)
                point_ids = {_need(p, "checkpoint_id") for p in point_rows}
                _eq(len(point_ids), len(point_rows))
                for seq in selected:
                    check = _need(checks, seq)
                    _assert(_need(check, "preserved") is True,
                            "selected before/after pair not preserved")
                    for side, kind in (("before", "highwater_before"), ("after", "highwater_after")):
                        _eq(_need(check, side), _need(attempts[seq], side))
                        matched = paired_cps[(seq, kind)]
                        if not matched: raise _EvidenceMissing("selected replay capacity pair absent")
                        _eq(len(matched), 1)
                        _assert(_need(matched[0], "capacity_ref") in point_ids)
                for i, record in enumerate(digest):
                    _eq(_need(record, "register_seq"), i)
                    source = {k: _need(attempts[i], k) for k in
                              ("input", "classification", "before", "after", "normalized_layout", "selected")}
                    raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    _eq(_need(record, "pass1_sha256"), hashlib.sha256(raw).hexdigest())
                    if _need(record, "pass1_sha256") != _need(record, "pass2_sha256"):
                        raise _EvidenceMissing("replay prefix mismatch")
            _eq(_need(replay, "mismatch"), None)
    elif row == "C06":
        point_rows = _items(_need(report, "capacity_proof", "checkpoints"))
        points = {_need(p, "checkpoint_id"): p for p in point_rows}
        _eq(len(points), len(point_rows), "capacity checkpoint ID reused")
        required_kinds = {"hour", "day", "highwater_before", "highwater_after",
                          "probe_prune_at", "prune", "tail_80640", "tail_80641"}
        used_refs = set()
        for f in fixtures:
            for cp in _items(_need(f, "checkpoints")):
                ref = _need(cp, "capacity_ref")
                if ref is None:
                    if cp.get("kind") in required_kinds:
                        raise _EvidenceMissing("required capacity observation missing")
                    continue
                _assert(ref not in used_refs, "capacity point reused")
                used_refs.add(ref)
                point = _need(points, ref)
                _eq(_need(point, "fixture_name"), _need(f, "name"))
                _eq(_need(point, "kind"), _need(cp, "kind"))
                _eq(_need(point, "minute_index"), _need(cp, "minute_index"))
                _container_sum(point)
                _assert(_need(point, "G_bytes") <= _need(point, "E_bytes") <= _need(point, "B_bytes"))
                _assert(_need(point, "capacity_covered") is True and _need(point, "resident_covered") is True)
    elif row == "C07":
        _assert(_no_rebuild(report), "rebuild cannot be classified no-rebuild")
    elif row == "C08":
        if _no_rebuild(report):
            raise _NoRebuild()
        events = _items(_need(report, "rebuild_events"))
        observed_rebuild = False
        for f in fixtures:
            for cp in _items(_need(f, "checkpoints")):
                count = _integer(_need(cp, "rebuild_count"))
                old = _integer(_need(cp, "budget", "capacity", "rebuild_old_bytes"))
                new = _integer(_need(cp, "budget", "capacity", "rebuild_new_bytes"))
                _assert(count >= 0 and old >= 0 and new >= 0)
                if count == 0: _eq((old, new), (0, 0))
                observed_rebuild |= count > 0
        _assert(observed_rebuild, "rebuild event has no checkpoint")
        event_counts = Counter(_need(event, "checkpoint_id") for event in events)
        for f in fixtures:
            for cp in _items(_need(f, "checkpoints")):
                _eq(event_counts[_need(cp, "checkpoint_id")], _need(cp, "rebuild_count"),
                    "rebuild event coverage differs from checkpoint")
        for event in events:
            for phase in ("before", "during", "after"):
                point = _need(event, phase)
                _assert(_need(point, "Q_actual") <= _need(point, "Q_4"))
                _budget_order(point)
            _assert(_need(event, "during", "Q_actual") >= _need(event, "old_bytes") +
                    _need(event, "new_bytes"), "old/new backing absent from physical Q")
            base_charges = [_need(event, phase, "E") - _need(event, phase, "Q_actual")
                            for phase in ("before", "during", "after")]
            _eq(base_charges, [base_charges[0]] * 3,
                "old/new backing charged twice or omitted from E")
            _assert(any(_need(cp, "checkpoint_id") == _need(event, "checkpoint_id") and
                        _need(cp, "rebuild_count") > 0 for f in fixtures for cp in f["checkpoints"]),
                    "rebuild event has no measured checkpoint")
            _assert(_need(event, "failure_retry", "atomic") is True and
                    _need(event, "failure_retry", "retry_succeeded") is True)
            temporary = _need(event, "temporary")
            delta = _need(temporary, "peak") - _need(temporary, "current_before")
            _eq(_need(temporary, "peak_delta"), delta)
            _assert(delta <= 262144)
    elif row == "C09":
        proof = _need(report, "capacity_proof")
        _eq(_need(proof, "unknown_ownership"), [])
        points = _items(_need(proof, "checkpoints"))
        for point in points:
            _container_sum(point)
            _need(point, "kind")
            if point.get("kind") == "prune":
                backings = _need(point, "container_backings")
                deleted = {_need(backing, "object_id") for backing in backings
                           if _need(backing, "includes_deleted_dummy") is True}
                _eq(set(_need(point, "deleted_dummy_object_ids")), deleted,
                    "deleted dummy lacks physical Q ownership")
        if not unit and not any(p.get("kind") in ("highwater", "highwater_before", "highwater_after")
                                for p in points):
            raise _EvidenceMissing("high-water capacity observation missing")
        if not any(p.get("kind") == "prune" for p in points):
            raise _EvidenceMissing("prune capacity observation missing")
        scenarios = {s.get("name"): s for s in _items(_need(report, "scenarios"))}
        temporary_refs = []
        for point in points:
            if point.get("kind") == "prune":
                ref = _need(point, "temporary_ref")
                temporary_refs.append(ref)
                _temporary(_need(scenarios, ref))
        _eq(len(temporary_refs), len(set(temporary_refs)), "prune peak sample reused")
    elif row == "C10":
        calibration = _need(report, "calibration")
        if _need(calibration, "status") != "complete":
            raise _EvidenceMissing("pre-full calibration incomplete or unlinked")
        observations = _items(_need(calibration, "observations"))
        for obs in observations:
            for key in ("prepare_seconds", "clone_seconds", "call_seconds", "pass1_seconds",
                        "pass2_seconds", "selected_pairs", "error_seconds"):
                _need(obs, key)
        eta = _need(report, "eta")
        seconds = _need(eta, "seconds")
        budget = _need(eta, "budget_seconds")
        if (type(seconds) not in (int, float) or type(budget) not in (int, float) or
                not math.isfinite(seconds) or not math.isfinite(budget)):
            raise _EvidenceMissing("calibration ETA or budget missing")
        if seconds > 14_400 or budget > 14_400:
            reference = report.get("budget_agreement_ref")
            if not isinstance(reference, str) or not reference.strip():
                raise _EvidenceViolation("extended full budget lacks prior agreement")
            _eq(_need(eta, "decision"), "agreement_required")
            agreement = _need(report, "budget_agreement")
            _eq(_need(agreement, "ref"), reference)
            _eq(_need(agreement, "approved_budget_seconds"), budget)
            _assert(bool(_need(agreement, "signature")))
            _assert(_need(agreement, "signed_at") < _need(report, "run_started_at"),
                    "extended budget was not signed before full run")
        else:
            _eq(_need(eta, "decision"), "within_budget")
        _assert(seconds <= budget, "ETA exceeds agreed budget")
        _assert(_need(report, "total_elapsed_seconds") <= budget)
        _need(report, "environment", "command")
        if not unit:
            _assert(_need(report, "complete") is True and _need(report, "aborted_reason") is None)
            _assert(_need(report, "calibration", "run_id") != _need(report, "environment", "run_id"),
                    "calibration and full run are the same execution")


class _NoRebuild(Exception):
    pass


def _full_scope(row_id, report):
    if report.get("mode") == "predicate_unit": return
    if report.get("mode") != "full": raise _EvidenceMissing("unsupported evidence mode")
    if row_id in ("A01", "C10"): return
    if row_id[0] == "A" and row_id not in ("A11", "A12") or row_id == "B23":
        pressure = _items(_need(report, "pressure_fixtures"), len(PRESSURE_NAMES + PRESSURE_AUX))
        if any("name" not in p for p in pressure) or len({p["name"] for p in pressure}) != len(pressure):
            raise _EvidenceMissing("pressure fixture names incomplete or duplicated")
        actual = {p["name"] for p in pressure}
        if set(PRESSURE_NAMES + PRESSURE_AUX) - actual:
            raise _EvidenceMissing("required pressure fixture missing")
        _eq(actual, set(PRESSURE_NAMES + PRESSURE_AUX), "unexpected pressure fixture replaced the full plan")
    if (row_id[0] in ("B", "C") and row_id not in ("B20", "B23", "C10")) or row_id == "A02":
        churn = _items(_need(report, "churn_fixtures"), len(CHURN_NAMES))
        if any("name" not in f for f in churn) or len({f["name"] for f in churn}) != len(churn):
            raise _EvidenceMissing("churn fixture names incomplete or duplicated")
        actual = {f["name"] for f in churn}
        if set(CHURN_NAMES) - actual:
            raise _EvidenceMissing("required churn fixture missing")
        _eq(actual, set(CHURN_NAMES), "unexpected churn fixture replaced the full plan")
    if row_id in ("A11", "A12"):
        scenarios = _items(_need(report, "scenarios"), len(_D02_SCENARIOS))
        if not set(_D02_SCENARIOS) <= {s.get("name") for s in scenarios}:
            raise _EvidenceMissing("applied measurement scenario missing")
    if row_id == "B20":
        fault_kinds = {s.get("fault_evidence", {}).get("fault_kind") for s in
                       _items(_need(report, "scenarios"))}
        if not {"clock_step", "merge_failure", "late_after_expiry", "id_collision",
                "uuid_uniqueness"} <= fault_kinds:
            raise _EvidenceMissing("independent fault scenario missing")


def _validate_unit_scale(report):
    scale = report.get("test_scale", {})
    if not isinstance(scale, dict):
        raise _EvidenceMissing("test scale malformed")
    name_lists = {"id_kinds", "tracks", "sources", "existing_transitions", "measured_scenarios",
                  "temporary_scenarios", "fault_kinds", "churn_names"}
    for key, value in scale.items():
        if key in name_lists:
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
                raise _EvidenceMissing(f"{key} name list missing")
            if len(set(value)) != len(value):
                raise _EvidenceViolation(f"{key} names duplicated")
        elif key in {"normal_calls", "minutes", "hours", "days", "burst_details", "max_records",
                     "job_key_limit", "samples_per_gc", "cycle_length", "finished", "unbound",
                     "pre_prune_hours", "detail_target"}:
            if type(value) is not int or value <= 0:
                raise _EvidenceMissing(f"{key} scale must be a positive integer")


def _validate_full_keys(report, row_id):
    for field in ("pressure_fixtures", "churn_fixtures", "scenarios"):
        if field not in report: continue
        rows = _items(report[field])
        names = [_need(item, "name") for item in rows]
        if not all(isinstance(name, str) and name for name in names):
            raise _EvidenceMissing(f"{field} name missing")
        _assert(len(set(names)) == len(names), f"{field} name reused")
    for fixture in report.get("churn_fixtures", []) if row_id[0] in ("B", "C") else []:
        if "checkpoints" in fixture:
            _unique_named(fixture["checkpoints"], "checkpoint_id", "checkpoint")
    references = []
    for fixture in report.get("churn_fixtures", []) if row_id[0] in ("B", "C") else []:
        if "checkpoints" in fixture:
            ids = [cp["checkpoint_id"] for cp in _items(fixture["checkpoints"])
                   if "checkpoint_id" in cp]
            _assert(len(set(ids)) == len(ids), "checkpoint ID reused")
        ref = fixture.get("highwater_checks", {}).get("register_attempts_ref")
        if ref is not None: references.append(_need(ref, "path"))
    if references:
        _assert(len(set(references)) == len(references), "attachment reference reused")
    proof = report.get("capacity_proof")
    if row_id[0] == "C" and isinstance(proof, dict) and "checkpoints" in proof:
        ids = [point["checkpoint_id"] for point in _items(proof["checkpoints"])
               if "checkpoint_id" in point]
        _assert(len(set(ids)) == len(ids), "capacity checkpoint ID reused")


def _validate_common_keys(report, row_id):
    fields = (("pressure_fixtures",) if row_id[0] == "A" else
              ("churn_fixtures",) if row_id[0] in ("B", "C") else ())
    if row_id in ("A04", "A10", "A11", "A12") and "scenarios" in report:
        _unique_named(report["scenarios"], "name", "scenario")
    for field in fields:
        if field not in report:
            continue
        rows = _items(report[field])
        if all(isinstance(row, dict) and "name" in row for row in rows):
            _unique_named(rows, "name", field)
        if field == "churn_fixtures":
            ids = []
            for fixture in rows:
                if "checkpoints" in fixture:
                    ids.extend(cp["checkpoint_id"] for cp in _items(fixture["checkpoints"])
                               if "checkpoint_id" in cp)
            if ids: _names(ids, "checkpoint")
            refs = []
            for fixture in rows:
                hw = fixture.get("highwater_checks", {})
                if isinstance(hw, dict) and "register_attempts_ref" in hw:
                    refs.append(_need(hw["register_attempts_ref"], "path"))
            if refs: _names(refs, "attachment reference")


_INTEGER_EVIDENCE_KEYS = frozenset({
    "N_total", "N_res", "N_tomb", "N_live", "D", "K", "A", "R", "T", "R_T",
    "AR", "TR", "E", "B", "G", "F_4", "Q_4", "Q_actual", "last_seq",
    "registered_seq", "call_index", "seq", "candidate_seq", "candidate_N_res",
    "candidate_E", "attempted", "visits", "visit_limit", "n",
    "detail_charge_bytes", "detail_charged_bytes", "id_utf8_bytes",
    "id_getsizeof_bytes", "retained_details", "requested_detail_target",
    "effective_detail_target", "received_at", "received_mono", "started_at",
    "started_mono", "admission_stopped_at", "current_before", "current_after",
    "peak", "peak_delta", "fixture_limit", "N_last_accepted", "AR_before",
    "AR_after", "pruned_count", "registered", "finished", "last_seq",
    "N_stop", "observed_N", "candidate_before", "candidate_after",
    "minute_index", "samples_per_gc", "detail_target", "normal_calls",
})
_INTEGER_EVIDENCE_ARRAYS = frozenset({"raw_ns", "D_observed", "K_observed", "entry_seqs"})
_INTEGER_EVIDENCE_MAPS = frozenset({"source_registered", "job_key_counts", "state_counts",
                                    "state_registration_counts",
                                    "classification_counts", "transition_counts", "owned_bytes"})
_BOOLEAN_EVIDENCE_KEYS = frozenset({
    "active", "admission_stopped", "all_rejected", "atomic", "auxiliary",
    "backing_changed", "capacity_covered", "checked", "classification_ok",
    "complete", "connection", "conservative_shared_strings", "coverage_complete",
    "eligible", "equal", "first_stop_time_unchanged", "fresh_starts",
    "gc_enabled", "has_more", "hash_seed_zero", "includes_deleted_dummy",
    "lifecycle", "live", "measured", "min_4_cores", "min_8gib_ram",
    "no_bytecode", "no_insertion", "no_retirement", "normal_schedule_counted",
    "open", "original_unchanged", "overdue", "owned_id", "owner",
    "partial_acceptance_applicable", "performed", "preserved", "previous_job",
    "prune", "python_3_13", "recent", "record_access_audit", "resident_covered",
    "resumed_after_release", "retry_succeeded", "same_pair", "selected", "seq",
    "seq_bucket_reversed", "tomb", "uncertain", "used", "would_exceed",
    "expiry", "cohort", "close",
})


def _reject_integer_bools(value, key=None):
    if type(value) is bool and (key in _INTEGER_EVIDENCE_KEYS or
                                key in _INTEGER_EVIDENCE_ARRAYS or
                                key in _INTEGER_EVIDENCE_MAPS or
                                (key not in _BOOLEAN_EVIDENCE_KEYS and key is not None) or
                                isinstance(key, str) and (key.startswith("max_") or key.endswith("_bytes"))):
        raise _EvidenceMissing("JSON bool cannot stand for integer evidence")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _reject_integer_bools(child, None if key in ("paths", "other_indexes_absent") else
                                  key if key in _INTEGER_EVIDENCE_MAPS else child_key)
    elif isinstance(value, list):
        for child in value:
            _reject_integer_bools(child, key if key in _INTEGER_EVIDENCE_ARRAYS else None)


def evaluate_row(row_id, report, *, attachments):
    if row_id not in EVIDENCE_ROW_IDS:
        raise ValueError(row_id)
    try:
        _need(report, "mode")
        cache = _finalize_cache.get()
        if cache is None or not cache["bool_checked"] or cache["report_id"] != id(report):
            _reject_integer_bools(report)
            if cache is not None and cache["report_id"] == id(report):
                cache["bool_checked"] = True
        if report.get("mode") == "predicate_unit": _validate_unit_scale(report)
        elif report.get("mode") == "full": _validate_full_keys(report, row_id)
        _validate_common_keys(report, row_id)
        _full_scope(row_id, report)
        if row_id[0] == "A": _evaluate_a(row_id, report)
        elif row_id in tuple(f"B{i:02d}" for i in range(1, 13)): _evaluate_b(row_id, report)
        elif row_id[0] == "B": _evaluate_b_rest(row_id, report)
        else: _evaluate_c(row_id, report, attachments)
    except _NoRebuild:
        return {"row_id": row_id, "status": "N/A", "reason": "N/A (no rebuild in this implementation)",
                "evidence_refs": []}
    except _EvidenceMissing as exc:
        return {"row_id": row_id, "status": "UNVERIFIED", "reason": str(exc), "evidence_refs": []}
    except _EvidenceViolation as exc:
        return {"row_id": row_id, "status": "FAIL", "reason": str(exc), "evidence_refs": []}
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, StopIteration,
            OverflowError, UnicodeError, ZeroDivisionError) as exc:
        return {"row_id": row_id, "status": "UNVERIFIED",
                "reason": f"malformed nested evidence: {type(exc).__name__}", "evidence_refs": []}
    return {"row_id": row_id, "status": "PASS", "reason": None, "evidence_refs": []}


def _result(row_id, status, reason=None):
    return {"row_id": row_id, "status": status, "reason": reason, "evidence_refs": []}


def _parse_stdout(stdout_bytes):
    try:
        source = stdout_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, False
    start = len(source) - len(source.lstrip())
    if start == len(source): return None, False
    if source[start] == "[": return None, False
    if source[start] != "{":
        # Diagnostic text before a complete report is still a D05 failure, but
        # the report remains available to the evidence rows.
        start = source.find("{", start)
        if start < 0: return None, False
    try:
        report, end = json.JSONDecoder().raw_decode(source, start)
    except ValueError:
        # In particular, never scan into a malformed outer JSON object for an
        # apparently valid nested object.
        return None, False
    if not isinstance(report, dict): return None, False
    trailing = source[end:].lstrip()
    if trailing:
        try:
            json.JSONDecoder().raw_decode(trailing)
        except ValueError:
            pass
        else:
            return None, False
    return report, bool(source[:start].strip() or trailing)


def _d05(report, stderr_bytes, exit_code, extra_stdout):
    if report is None: return _result("D05", "FAIL", "usable JSON report absent or duplicated")
    if extra_stdout: return _result("D05", "FAIL", "non-JSON stdout content")
    if type(exit_code) is not int or exit_code != 0 or type(report.get("exit_code")) is not int or report["exit_code"] != exit_code:
        return _result("D05", "FAIL", "measurement exit code mismatch")
    complete, reason = report.get("complete"), report.get("aborted_reason")
    if (complete is True and reason is not None) or (complete is False and not reason):
        return _result("D05", "FAIL", "contradictory completion marking")
    if complete not in (True, False): return _result("D05", "UNVERIFIED", "completion marking missing")
    if not stderr_bytes.strip(): return _result("D05", "UNVERIFIED", "stderr progress log missing")
    try: lines = stderr_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError: return _result("D05", "FAIL", "invalid stderr encoding")
    lines = [line.strip() for line in lines if line.strip()]
    open_jobs, progress = [], set()
    if not lines or not lines[0].startswith("start "):
        return _result("D05", "FAIL", "stderr must begin with start")
    for line in lines:
        parts = line.split()
        if len(parts) < 2: return _result("D05", "FAIL", "malformed stderr record")
        action, name = parts[:2]
        if action == "start": open_jobs.append(name)
        elif action == "progress":
            if name not in open_jobs: return _result("D05", "FAIL", "progress outside start/end")
            progress.add(name)
        elif action == "end":
            if name not in open_jobs: return _result("D05", "FAIL", "end without matching start")
            if (name.startswith("pressure_") or name.startswith("churn_") or
                    name in _D02_SCENARIOS) and name not in progress:
                return _result("D05", "FAIL", "required progress missing")
            open_jobs.remove(name)
        else: return _result("D05", "FAIL", "unknown stderr record")
    if complete and (open_jobs or not lines[-1].startswith("end ")):
        return _result("D05", "FAIL", "unclosed stderr task")
    if not complete: return _result("D05", "UNVERIFIED", "run aborted before complete evidence")
    return _result("D05", "PASS")


_D02_SCENARIOS = (
    "link_round_accept", "report_init_failed_accept", "wrapper_exited_accept", "contributions_tail_empty",
    "aggregation_empty", "cohort_empty", "finish_accept", "link_round_duplicate", "finish_near_valid_limit",
    "finish_over_input_limit", "finish_duplicate", "contributions_first_page", "contributions_after_cursor",
    "aggregation_recent", "cohort_all", "register_accept", "register_capacity_reject", "close_boundary_before",
    "close_boundary_exact", "large_clock_jump", "close_same_time_requery", "mass_close_boundary", "mass_overdue",
    "stale_overdue_end_cursor", "stale_overdue_requery", "multi_bucket_close", "multi_bucket_close_requery",
    "multi_bucket_close_merge_failure", "multi_bucket_close_retry", "multi_bucket_close_retry_requery",
    "reverse_cohort_register_tail", "reverse_cohort_empty", "reverse_cohort_narrow", "identity_absent_link",
    "identity_absent_finish", "missing_record_direct", "missing_record_first_query_cohort",
    "missing_record_first_query_contributions", "missing_record_first_query_aggregation", "open_end_cursor",
    "open_last_seq_cursor", "recent_window_seq_order", "recent_window_boundary", "recent_window_before_exclusion",
    "recent_window_after_exclusion", "recent_window_before_close", "recent_window_exact_close",
    "close_wait_target_advance", "recent_outside_open", "recent_outside_open_one_inside",
)
_ADAPTER_SCENARIOS = ("adapter_investing_link", "adapter_investing_finish", "adapter_bs_link",
                      "adapter_bs_finish", "adapter_citi_link", "adapter_citi_finish", "adapter_bank_to_finish")


def _locked_result(report, attachments, key):
    entry = _need(report, "locked_tests", key)
    reference = _need(entry, "result_ref")
    if not _safe_ref(reference): raise _EvidenceMissing(f"{key} unsafe locked test reference")
    raw = _need(attachments, reference)
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != _need(entry, "result_sha256"):
        raise _EvidenceMissing(f"{key} locked test attachment missing or changed")
    try: observed = json.loads(raw)
    except (ValueError, UnicodeError) as exc: raise _EvidenceMissing(f"{key} locked result invalid") from exc
    if not isinstance(observed, dict): raise _EvidenceMissing(f"{key} locked result invalid")
    if observed != {"suite": key, "passed": _need(entry, "passed"), "failed": _need(entry, "failed"),
                    "commit_sha": _need(entry, "commit_sha")}:
        raise _EvidenceMissing(f"{key} locked result disagrees with report")
    _eq(_need(entry, "commit_sha"), _need(report, "environment", "commit_sha"))
    _assert(_integer(_need(entry, "passed")) > 0 and _integer(_need(entry, "failed")) == 0,
            f"{key} locked test failed")


def _require_row_pass(rows, row_id):
    status = _need(rows, row_id, "status")
    if status == "FAIL": raise _EvidenceViolation(f"{row_id} failed")
    if status not in ("PASS", "N/A"): raise _EvidenceMissing(f"{row_id} unverified")


def _d02_own(report, attachments, rows):
    if report.get("mode") != "full" or report.get("complete") is not True:
        raise _EvidenceMissing("full run did not complete")
    _require_row_pass(rows, "A01")
    _require_row_pass(rows, "D05")
    scenarios = {s.get("name"): s for s in _items(_need(report, "scenarios"))}
    for name in _D02_SCENARIOS + _ADAPTER_SCENARIOS:
        scenario = _need(scenarios, name)
        for field in ("status", "visit_gate", "temporary_gate"):
            _eq(_need(scenario, field), "PASS")
        if name in _D02_SCENARIOS and scenario.get("time_gate") == "N/A":
            _observed_timing(scenario)
            _measured_time_gate(scenario, required_n=1000)
        else:
            _eq(_need(scenario, "time_gate"), "PASS")
            if name in _D02_SCENARIOS:
                _observed_timing(scenario)
                _measured_time_gate(scenario, required_n=1000)
    _eq(_need(report, "adapter", "status"), "PASS")
    observations = _items(_need(report, "adapter", "observations"), 3)
    _eq(len(observations), 3)
    _eq({_need(obs, "source") for obs in observations}, {"investing", "bs", "citi"})
    for obs in observations:
        _eq(_need(obs, "link"), "linked")
        _eq(_need(obs, "finish"), "finalized")
        _eq(_need(obs, "status"), "PASS")
    _eq(_need(report, "record_access_audit_findings"), [])
    _assert(_need(report, "prerequisites", "record_access_audit") is True)
    for key in ("d7", "index"): _locked_result(report, attachments, key)


def _d_verdict(report, attachments, rows, stage):
    try:
        _d02_own(report, attachments, rows)
        if stage >= 3:
            _locked_result(report, attachments, "slice5a3")
            for row_id in tuple(f"A{i:02d}" for i in range(1, 13)) + ("B13",):
                _require_row_pass(rows, row_id)
            for name in ("resident_unfinished", "resident_2048_details",
                         "resident_released_identity", "resident_capacity_stop"):
                _eq(_need({s.get("name"): s for s in report["scenarios"]}, name, "status"), "PASS")
        if stage >= 4:
            for key in ("slice5a4a", "slice5a4b"):
                _locked_result(report, attachments, key)
            for row_id in EVIDENCE_ROW_IDS: _require_row_pass(rows, row_id)
        if stage >= 3 and rows["D01"]["status"] != "PASS":
            bad = [r for r in EVIDENCE_ROW_IDS if rows[r]["status"] != "PASS" and rows[r]["status"] != "N/A"]
            raise _EvidenceMissing("overall not PASS: " + ", ".join(bad))
    except _EvidenceMissing as exc:
        return _result(f"D0{stage if stage != 2 else 2}", "UNVERIFIED", str(exc))
    except _EvidenceViolation as exc:
        return _result(f"D0{stage if stage != 2 else 2}", "FAIL", str(exc))
    return _result(f"D0{stage if stage != 2 else 2}", "PASS")


def finalize_acceptance(*, stdout_bytes: bytes, stderr_bytes: bytes,
                        exit_code: int, attachments: dict[str, bytes]) -> dict:
    report, extra = _parse_stdout(stdout_bytes)
    if report is None:
        entries = [_result(r, "UNVERIFIED", "usable JSON report absent") for r in EVIDENCE_ROW_IDS]
    else:
        token = _finalize_cache.set({"report_id": id(report), "bool_checked": False, "attempts": {}})
        try:
            entries = [evaluate_row(r, report, attachments=attachments) for r in EVIDENCE_ROW_IDS]
        finally:
            _finalize_cache.reset(token)
    d05 = _d05(report, stderr_bytes, exit_code, extra)
    entries.append(d05)
    by_id = {entry["row_id"]: entry for entry in entries}
    if report is None:
        d01 = _result("D01", "FAIL", "usable JSON report absent")
    else:
        for entry in entries:
            if entry["status"] == "N/A":
                try:
                    allowed = entry["row_id"] == "C08" and _no_rebuild(report)
                except (_EvidenceMissing, _EvidenceViolation):
                    allowed = False
                if not allowed: entry.update(status="UNVERIFIED", reason="N/A not supported by evidence")
        statuses = {entry["status"] for entry in entries}
        overall = "FAIL" if "FAIL" in statuses else "UNVERIFIED" if "UNVERIFIED" in statuses else "PASS"
        d01 = _result("D01", overall, None if overall == "PASS" else "evidence rows or D05 incomplete")
    by_id["D01"] = d01
    verdicts = []
    for stage in (2, 3, 4):
        if report is None: verdict = _result(f"D0{stage}", "UNVERIFIED", "usable JSON report absent")
        else: verdict = _d_verdict(report, attachments, by_id, stage)
        verdicts.append(verdict)
    by_id.update((v["row_id"], v) for v in verdicts)
    final = {"slice5a2_partial": verdicts[0]["status"], "slice5a3": verdicts[1]["status"],
             "slice5a4": verdicts[2]["status"]}
    provisional = report.get("verdicts", {}) if report else {}
    diffs = {}
    if report and report.get("overall") != d01["status"]:
        diffs["overall"] = {"provisional": report.get("overall"), "final": d01["status"]}
    for name, status in final.items():
        if provisional.get(name) != status:
            diffs[name] = {"provisional": provisional.get(name), "final": status}
    return {"acceptance_checklist": [by_id[r] for r in ROW_IDS], "overall": d01["status"],
            "verdicts": final,
            "verdict_reasons": {name: verdicts[i]["reason"] for i, name in
                                enumerate(("slice5a2_partial", "slice5a3", "slice5a4"))},
            "provisional_consistency": {"matches": not diffs, "diffs": diffs}}


def _finalize_cli(argv):
    parser = argparse.ArgumentParser(description="Finalize D7 evidence from captured output")
    parser.add_argument("--stdout-log", required=True)
    parser.add_argument("--stderr-log", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--attachments-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    stdout = Path(args.stdout_log).read_bytes()
    stderr = Path(args.stderr_log).read_bytes()
    report, extra_stdout = _parse_stdout(stdout)
    attachments = {}
    if report:
        references = []
        for fixture in report.get("churn_fixtures", []):
            ref = fixture.get("highwater_checks", {}).get("register_attempts_ref", {})
            if isinstance(ref, dict) and "path" in ref: references.append(ref["path"])
        for entry in report.get("locked_tests", {}).values():
            if isinstance(entry, dict) and "result_ref" in entry: references.append(entry["result_ref"])
        root = Path(args.attachments_dir).resolve()
        for reference in references:
            if not _safe_ref(reference): continue
            target = (root / reference).resolve()
            if not target.is_relative_to(root) or not target.is_file(): continue
            attachments[reference] = target.read_bytes()
    result = finalize_acceptance(stdout_bytes=stdout, stderr_bytes=stderr,
                                 exit_code=args.exit_code, attachments=attachments)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report is not None and not extra_stdout else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "finalize":
        return _finalize_cli(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--limit", type=int, default=CAP)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--budget-seconds", type=float, default=14400)
    parser.add_argument("--max-resident-bytes", type=int)
    parser.add_argument("--row", action="append", dest="rows")
    parser.add_argument("--pressure-name", action="append", dest="pressure_names")
    parser.add_argument("--churn-name", action="append", dest="churn_names")
    parser.add_argument("--churn-minutes", type=int, default=10_080)
    parser.add_argument("--churn-stride-minutes", type=int, default=1)
    parser.add_argument("--fixture-detail-divisor", type=int, default=1)
    parser.add_argument("--calibration", action="store_true")
    parser.add_argument("--budget-agreement-ref")
    args = parser.parse_args()
    report = run_gate(limit=args.limit, samples=args.samples, warmup=args.warmup,
                      quick=args.quick, budget_seconds=args.budget_seconds,
                      rows=args.rows,
                      max_resident_bytes=args.max_resident_bytes,
                      pressure_names=args.pressure_names, churn_names=args.churn_names,
                      churn_minutes=args.churn_minutes,
                      churn_stride_minutes=args.churn_stride_minutes,
                      fixture_detail_divisor=args.fixture_detail_divisor,
                      calibration=args.calibration,
                      budget_agreement_ref=args.budget_agreement_ref)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    sys.exit(main())
