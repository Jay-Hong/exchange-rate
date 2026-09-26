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
import hashlib
import importlib
from itertools import islice
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from collections import Counter
from contextlib import contextmanager
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
    from app.d7_round_ledger import RoundLedger  # noqa: E402

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


def new_ledger(limit):
    return RoundLedger(EPOCH, aggregation_started_at=T - MINUTE,
                       limits={"max_records": limit, "max_retained_details": min(DETAIL_CAP, limit),
                               "max_detail_bytes": 4096})


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
    for i in range(stop if stop is not None else limit):
        result = ledger.register(**register_args(i))
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
        elif unavailable_rest:
            result = ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i),
                                               failed_wall=T, failed_mono=T,
                                               received_at=T, received_mono=T)
            if result["classification"] != "report_init_failed":
                raise RuntimeError(f"fixture unavailable {i}: {result['classification']}")
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
    scalar = (str, int, float, bool, bytes, type(None), lock_type)
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
             if key not in ("_lock", "_close_index", "_overdue_index", "_cohort_index")
             and not key.startswith("_gate_")}
    state["_close_index"] = (ledger._close_index.size, ledger._close_index.data)
    state["_overdue_index"] = (ledger._overdue_index.size, ledger._overdue_index.data)
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
    clone._close_index = copy.copy(base._close_index)
    clone._close_index.data = base._close_index.data.copy()
    clone._overdue_index = copy.copy(base._overdue_index)
    clone._overdue_index.data = base._overdue_index.data.copy()
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
    ledger = fill(limit, finalized=detail_count)
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
        ledger = new_ledger(limit)
        for i in range(limit):
            result = ledger.register(**register_args(i))
            if result["classification"] != "registered":
                raise RuntimeError("adapter maximum-slot fixture registration failed")
            if i < demand:
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

        rows.append(measure_case("adapter_bank_to_finish", ledger, "combined",
                                 list(enumerate(reports)), expected="finalized",
                                 invoke=lambda pair: combined_on(ledger, pair),
                                 gc_invoke=lambda pair: combined_on(ledger_gc, pair),
                                 quick=quick, warmup=warmup, samples=samples, clock=clock))
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
    return [row for row in rows if not row["name"].startswith("resident_")
            and row["name"] != "record_baseline" and row.get("status") != "baseline"]


def overall_status(rows, *, adapter_status, mode):
    gated = [row for row in rows if row["name"] != "record_baseline"
             and row.get("status") != "baseline"]
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
        return ledger._records.get(ledger._seq[seq - 1])
    return (sum(not was_closed and current(seq) is not None and current(seq)["closed"]
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
                                    required_n=required, full_cohort=full_cohort)
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


def _close_base(limit, n=1):
    ledger = new_ledger(limit)
    for i in range(limit):
        if ledger.register(**register_args(i))["classification"] != "registered":
            raise RuntimeError("close fixture registration")
    for i in range(n):
        if ledger.link_round(**link_args(i))["classification"] != "linked":
            raise RuntimeError("close fixture link")
        if ledger.finish(**finish_args(i))["classification"] != "finalized":
            raise RuntimeError("close fixture finish")
    for i in range(n, limit):
        ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i), failed_wall=T,
                                  failed_mono=T, received_at=T, received_mono=T)
    return ledger


def _multi_bucket_base(limit):
    n = min(DETAIL_CAP, limit)
    ledger = new_ledger(limit)
    for i in range(limit):
        ledger.register(**register_args(i))
    for i in range(n):
        ledger.link_round(**link_args(i))
    for i in range(n, limit):
        ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i), failed_wall=T,
                                  failed_mono=T, received_at=T, received_mono=T)
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
            ledger = new_ledger(limit)
            for i in range(limit):
                ledger.register(**register_args(i))
            for i in range(count):
                ledger.link_round(**link_args(i))
            for i in range(count, limit):
                ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i), failed_wall=T,
                                          failed_mono=T, received_at=T, received_mono=T)
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
    for i in range(limit if stop is None else stop):
        kwargs = register_args(i, source=_reverse_source(i))
        kwargs["started_wall"] = T - i
        result = ledger.register(**kwargs)
        if result["classification"] != "registered":
            raise RuntimeError(f"reverse registration {i}: {result['classification']}")
    return ledger


def _reverse_source(i):
    sources = tuple(REGISTRY)
    return sources[i % len(sources)]


def _stale_base(limit):
    ledger = new_ledger(limit)
    for i in range(limit):
        kwargs = register_args(i)
        if i >= 2 * limit // 3:
            kwargs.update(job_id="serial", serial_job=True)
        ledger.register(**kwargs)
        if i < limit // 3:
            ledger.link_round(**link_args(i))
            ledger.finish(**finish_args(i))
        elif i < 2 * limit // 3:
            ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i), failed_wall=T,
                                      failed_mono=T, received_at=T, received_mono=T)
    # The final serial successor has no next entry, so remove its B deadline too.
    ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(limit - 1), failed_wall=T,
                              failed_mono=T, received_at=T, received_mono=T)
    return ledger


def _recent_base(limit, one_inside=False):
    n = min(DETAIL_CAP, limit)
    ledger = new_ledger(limit)
    for i in range(limit):
        ledger.register(**register_args(i))
    for i in range(n):
        ledger.link_round(**link_args(i))
    for i in range(n, limit):
        ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i), failed_wall=T,
                                  failed_mono=T, received_at=T, received_mono=T)
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
    ledger = fill(limit)
    for i in (0, 1):
        if ledger.link_round(**link_args(i))["classification"] != "linked":
            raise RuntimeError(f"recent order fixture link {i}")
    for i in range(2, limit):
        ledger.report_init_failed(epoch=EPOCH, invocation_id=rid(i), failed_wall=T,
                                  failed_mono=T, received_at=T, received_mono=T)
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
    ledger = new_ledger(limit)
    for i in range(limit):
        ledger.register(**register_args(i))
        ledger.link_round(**link_args(i))
        outcome = ledger.finish(**finish_args(i))
        if outcome["classification"] not in ("finalized", "report_unavailable"):
            raise RuntimeError(f"wait fixture finish {i}: {outcome['classification']}")
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
                ledger = (_reverse_base(limit, limit-probe_count) if reverse
                          else fill(limit, stop=limit-probe_count))
            target = limit-probe_count+offset
        else:
            offset = (index-probe_count) % samples
            if offset == 0:
                ledger = (_reverse_base(limit, limit-samples) if reverse
                          else fill(limit, stop=limit-samples))
            target = limit-samples+offset
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
            ledger = fill(limit, linked=min(total, detail_cap))
        return ledger, "finish", finish_args(target, summary=summary), rid(target)
    return provide


def _scenario_specs(limit, samples, warmup):
    total = 2 + warmup + 2 * samples
    close_n = min(DETAIL_CAP, limit)
    specs = []
    def add(name, provider, plan, classification="snapshot", d=0, k=0, validate=None,
            exception=None, full_cohort=False):
        specs.append((name, provider, plan, classification, d, k, validate, exception, full_cohort))

    add("link_round_accept", _repeat_provider(lambda: fill(limit), "link_round",
        lambda i: link_args(i)), "sequential", "linked")
    add("report_init_failed_accept", _repeat_provider(lambda: fill(limit), "report_init_failed",
        lambda i: dict(epoch=EPOCH, invocation_id=rid(i), failed_wall=T, failed_mono=T,
                       received_at=T, received_mono=T)), "sequential", "report_init_failed")
    add("wrapper_exited_accept", _repeat_provider(lambda: fill(limit), "wrapper_exited",
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
    add("mass_close_boundary", _repeat_provider(lambda: _close_base(limit, close_n),
        "aggregation_snapshot", _at(T + 71 * MINUTE), independent=True, split=True), "independent_split_copy", d=close_n,
        validate=lambda ledger, outcome, error, before: (
            "detail not released or cumulative count incorrect"
            if error or ledger._health["retained_details"] != 0
            or ledger._cumulative_rounds[SOURCE]["rounds"] != close_n else None))
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
    multi = lambda: _multi_bucket_base(limit)
    add("multi_bucket_close", _repeat_provider(multi, "aggregation_snapshot",
        _at(T + 73 * MINUTE), independent=True, split=True), "independent_split_copy", d=close_n,
        validate=lambda ledger, outcome, error, before: (
            "multi-bucket close did not release/count once"
            if error or ledger._health["retained_details"] != 0
            or ledger._cumulative_rounds[SOURCE]["rounds"] != close_n else None))
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
            "candidate_records": {rid(i): copy.deepcopy(ledger._records[rid(i)]) for i in range(close_n)},
        }
    def check_atomic(ledger, outcome, error, before):
        saved = ledger._gate_atomic_before
        now = {"close": tuple(ledger._close_index.due(T + 3*MINUTE)),
               "open": tuple(ledger._open_seq), "recent": ledger._recent_buckets,
               "health": ledger._health, "cumulative_end": ledger._cumulative_end,
               "cumulative_rounds": ledger._cumulative_rounds, "cumulative_rows": ledger._cumulative_rows,
               "candidate_records": {rid(i): ledger._records[rid(i)] for i in range(close_n)}}
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
        _at(T + 73 * MINUTE), consume_failure), "full_deepcopy", d=close_n,
        validate=lambda ledger, outcome, error, before: (
            "retry did not close once"
            if error or ledger._health["retained_details"] != 0
            or ledger._cumulative_rounds[SOURCE]["rounds"] != close_n else None))
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
    narrow_indices = {source: max((i for i in range(limit) if _reverse_source(i) == source),
                                  default=None) for source in cohort_sources}
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
    add("reverse_cohort_empty", _repeat_provider(lambda: _reverse_base(limit), "cohort_snapshot",
        empty_cohort_args), "repeat")
    add("reverse_cohort_narrow", _repeat_provider(lambda: _reverse_base(limit), "cohort_snapshot",
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
    add("open_end_cursor", _repeat_provider(lambda: fill(limit, finalized=close_n),
        "contributions_open", dict(as_of=T, as_of_mono=T, after_seq=limit, limit=16)), "repeat",
        validate=lambda ledger, outcome, error, before: (
            "nonempty end cursor" if error or outcome["entries"] or outcome["next_seq"] is not None else None))
    cursor = {}
    def open_last_base():
        ledger = fill(limit, finalized=min(DETAIL_CAP, limit - 1), unavailable_rest=True)
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
                or len(ledger._gate_wait_candidates) != limit):
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


def run_gate(*, limit=CAP, samples=1000, warmup=100, quick=False,
             budget_seconds=14400, clock=time.perf_counter_ns, wall=time.monotonic,
             progress=sys.stderr, rows=None):
    """Measure the plan; selected scenario rows form a small, non-accepting run."""
    if not 1 <= limit <= CAP or samples < 1 or warmup < 0 or budget_seconds < 0:
        raise ValueError("invalid gate parameter")
    chosen = None if rows is None else tuple(rows)
    if chosen is not None and (not chosen or len(chosen) != len(set(chosen))):
        raise ValueError("rows must be distinct scenario names")
    if chosen is not None and not quick and (limit, samples, warmup) == (CAP, 1000, 100):
        raise ValueError("selected rows require small gate parameters")
    if quick and (limit, samples, warmup) == (CAP, 1000, 100):
        limit, samples, warmup = 96, 8, 2
    mode = ("full_small" if chosen is not None else "quick" if quick else "full"
            if (limit, samples, warmup) == (CAP, 1000, 100) else "full_small")
    started = wall()
    deadline = started + budget_seconds
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
        row = _measure_row(name, provider, plan=plan, expected=classification, expected_d=d,
                           expected_k=k, samples=n, warmup=warmup, clock=clock,
                           wall=wall, deadline=deadline, full_cohort=full_cohort,
                           sample_exception=exception if mode == "full" else None,
                           validate=validate, progress=progress)
        for field in ("cursor", "order_probe"):
            if hasattr(provider, field):
                row[field] = getattr(provider, field).copy()
        rows.append(row)
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
            try:
                row = memory_state(name, limit, detail_count=detail_count,
                                   release=release, reject=reject, quick=quick)
                row.update({key: value for key, value in _empty_row(name).items()
                            if key not in row})
            except Exception as exc:
                row = _empty_row(name, "FAIL")
                row["reason"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            print(f"end {name} {row['status']}", file=progress)
    adapter = {"status": "UNVERIFIED", "reason": "not started"}
    if chosen is None and aborted is None and wall() < deadline:
        for name in adapter_names:
            print(f"start {name}", file=progress)
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
    elif chosen is None and aborted is None:
        aborted = "budget"
    if len(rows) < len(names):
        existing = {row["name"] for row in rows}
        rows.extend(_empty_row(name) for name in names if name not in existing)
    if mode == "full" and not all(prereq.values()) and adapter["status"] == "PASS":
        adapter = {**adapter, "status": "UNVERIFIED", "reason": "environment prerequisites"}
    overall = overall_status(rows, adapter_status=adapter["status"], mode=mode)
    partial = partial_acceptance(rows, adapter_status=adapter["status"], mode=mode)
    if aborted is not None and overall != "FAIL":
        overall = "UNVERIFIED"
    env["load_after"] = _load_average()
    elapsed = wall() - started
    return {"contract": "slice5a_contract_r2.md S5a.4 + addendumB B1-B7",
            "mode": mode, "complete": aborted is None, "aborted_reason": aborted,
            "overall": overall, "summary": summarize(rows), "scenarios": rows,
            "adapter": adapter, "partial_acceptance": partial,
            "limits": {"max_records": CAP, "max_retained_details": DETAIL_CAP,
                       "max_detail_bytes": 4096, "owned_bytes": OWNED_LIMIT,
                       "temporary_bytes": TEMP_LIMIT},
            "environment": env, "prerequisites": prereq,
            "record_access_audit_findings": audit,
            "total_elapsed_seconds": round(elapsed, 6),
            "exit_code": 0, "reproduce": " ".join(sys.argv)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--limit", type=int, default=CAP)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--budget-seconds", type=float, default=14400)
    args = parser.parse_args()
    report = run_gate(limit=args.limit, samples=args.samples, warmup=args.warmup,
                      quick=args.quick, budget_seconds=args.budget_seconds)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
