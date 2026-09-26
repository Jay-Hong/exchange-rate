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

    def __init__(self, original):
        super().__init__(original)
        self.visits = 0

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.visits += 1
        return value

    def get(self, key, default=None):
        value = super().get(key, default)
        if key in self:
            self.visits += 1
        return value

    def values(self):
        for value in super().values():
            self.visits += 1
            yield value

    def items(self):
        for key, value in super().items():
            self.visits += 1
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
        if isinstance(obj, RoundLedger):
            pending.append(obj.__dict__)
        elif type(obj) is dict:
            for key, value in obj.items():
                pending.extend((key, value))
        elif type(obj) in (list, tuple, set, frozenset):
            pending.extend(obj)
        elif not isinstance(obj, scalar):
            unknown.add(f"{type(obj).__module__}.{type(obj).__qualname__}")
    return {"bytes": total, "objects": len(seen), "unknown_types": sorted(unknown),
            "conservative_shared_strings": True}


def pct(samples, percent):
    ordered = sorted(samples)
    return round(ordered[max(0, (len(ordered) * percent + 99) // 100 - 1)] / 1000, 3)


def timing(call, args, warmup, samples, gc_call=None):
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
                start = time.perf_counter_ns()
                outcome = (gc_call if label == "gc_enabled" and gc_call is not None else call)(arg)
                durations.append(time.perf_counter_ns() - start)
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
                 invoke=None, gc_invoke=None):
    call = invoke if invoke is not None else lambda arg: getattr(ledger, method)(**arg)
    if len(arguments) < 2 + warmup + 2 * samples:
        raise RuntimeError(f"insufficient independent arguments: {name}")
    ledger._records = CountedRecords(ledger._records)
    first = call(arguments[0])
    visits = ledger._records.visits
    ledger._records = dict(ledger._records)
    temp = peak_call(call, arguments[1])
    dist = timing(call, arguments[2:], warmup, samples, gc_call=gc_invoke)
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
    clone = object.__new__(RoundLedger)
    clone.__dict__ = copy.deepcopy({k: v for k, v in base.__dict__.items() if k != "_lock"})
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


def adapter_status(*, limit, demand, warmup, samples, quick):
    with offline_app_import():
        return _adapter_status(limit=limit, demand=demand, warmup=warmup,
                               samples=samples, quick=quick)


def _adapter_status(*, limit, demand, warmup, samples, quick):
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
                dist = timing(projection, [None] * (warmup + 2 * samples), warmup, samples)
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
                                 quick=quick, warmup=warmup, samples=samples))
        adapter_state = ("FAIL" if any(x["status"] == "FAIL" for x in observations + rows)
                         else "UNVERIFIED" if quick or any(x["status"] != "PASS"
                                                             for x in observations + rows) else "PASS")
        return {"status": adapter_state, "observations": observations}, rows
    except Exception as exc:
        return {"status": "UNVERIFIED", "reason": f"adapter path could not run: {type(exc).__name__}: {exc}"}, []


def summarize(rows):
    return dict(Counter(row["status"] for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="small fixture path check; never a maximum-slot pass")
    args = parser.parse_args()
    if platform.python_implementation() != "CPython":
        raise SystemExit("CPython required")
    quick = args.quick
    limit = 96 if quick else CAP
    samples = 8 if quick else 1000
    warmup = 2 if quick else 100
    demand = 2 + warmup + 2 * samples
    audit = audit_record_access()
    equivalent_visits = equivalence_check()
    rows = []
    env = environment()
    prereq = {"python_3_13": sys.version_info[:2] == (3, 13),
              "hash_seed_zero": os.environ.get("PYTHONHASHSEED") == "0",
              "no_bytecode": sys.dont_write_bytecode, "record_access_audit": not audit,
              "min_4_cores": (os.cpu_count() or 0) >= 4,
              "min_8gib_ram": (env["ram_bytes"] or 0) >= 8 * 1024**3}
    if not quick and not all(prereq.values()):
        raise SystemExit("full mode requires CPython 3.13, PYTHONHASHSEED=0, no bytecode, "
                         "audited Record access, and >=4 cores/8 GiB")

    # One full unbound fixture supplies disjoint Records to each mutating case.
    unbound = fill(limit)
    rows.append(measure_case("link_round_accept", unbound, "link_round",
                             [link_args(i) for i in range(demand)], expected="linked",
                             quick=quick, warmup=warmup, samples=samples))
    offset = demand
    rows.append(measure_case("report_init_failed_accept", unbound, "report_init_failed",
                             [dict(epoch=EPOCH, invocation_id=rid(offset + i), failed_wall=T,
                                   failed_mono=T, received_at=T, received_mono=T)
                              for i in range(demand)], expected="report_init_failed",
                             quick=quick, warmup=warmup, samples=samples))
    offset += demand
    rows.append(measure_case("wrapper_exited_accept", unbound, "wrapper_exited",
                             [dict(epoch=EPOCH, invocation_id=rid(offset + i), exited_wall=T,
                                   exited_mono=T, received_at=T, received_mono=T)
                              for i in range(demand)], expected="wrapper_exited",
                             quick=quick, warmup=warmup, samples=samples))
    same = lambda arg: arg
    count = demand
    rows.append(measure_case("contributions_tail_empty", unbound, "contributions_open",
                             [dict(as_of=T, as_of_mono=T, after_seq=limit, limit=16) for _ in range(count)],
                             expected="snapshot", quick=quick, warmup=warmup, samples=samples))
    rows.append(measure_case("aggregation_empty", unbound, "aggregation_snapshot",
                             [dict(as_of=T, as_of_mono=T) for _ in range(count)],
                             expected="snapshot", quick=quick, warmup=warmup, samples=samples))
    empty_cohort = dict(source=SOURCE, cohort_start=T + 1, cohort_end=T + 2,
                        as_of=T + 2, as_of_mono=T + 2)
    rows.append(measure_case("cohort_empty", unbound, "cohort_snapshot",
                             [empty_cohort for _ in range(count)], expected="snapshot",
                             quick=quick, warmup=warmup, samples=samples))
    del unbound

    linked = fill(limit, linked=demand * 4)
    linked_gc = fill(limit, linked=demand * 4)
    rows.append(measure_case("finish_accept", linked, "finish",
                             [finish_args(i) for i in range(demand)], expected="finalized",
                             quick=quick, warmup=warmup, samples=samples,
                             gc_invoke=lambda kw: linked_gc.finish(**kw)))
    rows.append(measure_case("link_round_duplicate", linked, "link_round",
                             [link_args(demand + i) for i in range(demand)], expected="relinked_same",
                             quick=quick, warmup=warmup, samples=samples))
    near = large_summary(3900)
    over = large_summary(4097)
    linked_near = fill(limit, linked=demand)
    linked_near_gc = fill(limit, linked=demand)
    rows.append(measure_case("finish_near_valid_limit", linked_near, "finish",
                             [finish_args(i, summary=near) for i in range(demand)],
                             expected="finalized", quick=quick, warmup=warmup, samples=samples,
                             gc_invoke=lambda kw: linked_near_gc.finish(**kw)))
    rows.append(measure_case("finish_over_input_limit", linked, "finish",
                             [finish_args(demand * 3 + i, summary=over) for i in range(demand)],
                             expected="input_limit_exceeded", quick=quick, warmup=warmup, samples=samples))
    del linked, linked_gc, linked_near, linked_near_gc

    final = fill(limit, finalized=16)
    rows.append(measure_case("finish_duplicate", final, "finish",
                             [finish_args(0) for _ in range(count)], expected="duplicate_finish",
                             quick=quick, warmup=warmup, samples=samples, details=16))
    rows.append(measure_case("contributions_first_page", final, "contributions_open",
                             [dict(as_of=T, as_of_mono=T, after_seq=0, limit=16) for _ in range(count)],
                             k=16, expected="snapshot", quick=quick, warmup=warmup, samples=samples, details=16))
    rows.append(measure_case("contributions_after_cursor", final, "contributions_open",
                             [dict(as_of=T, as_of_mono=T, after_seq=15, limit=16) for _ in range(count)],
                             k=1, expected="snapshot", quick=quick, warmup=warmup, samples=samples, details=16))
    rows.append(measure_case("aggregation_recent", final, "aggregation_snapshot",
                             [dict(as_of=T, as_of_mono=T) for _ in range(count)],
                             k=16, expected="snapshot", quick=quick, warmup=warmup, samples=samples, details=16))
    broad = dict(source=SOURCE, cohort_start=T, cohort_end=T + 1,
                 as_of=T + 1, as_of_mono=T + 1)
    rows.append(measure_case("cohort_all", final, "cohort_snapshot",
                             [broad for _ in range(count)], k=limit, expected="snapshot",
                             quick=quick, warmup=warmup, samples=samples, details=16))
    record_samples = []
    for _ in range(samples):
        start = time.perf_counter_ns()
        value = final.record(rid(0))
        record_samples.append(time.perf_counter_ns() - start)
        if value is None:
            raise RuntimeError("record baseline missing")
    rows.append({"name": "record_baseline", "slots": limit, "retained_details": 16,
                 "D": 0, "K": 1, "visits": None, "timing": {
                     "gc_default": {"n": samples, "p50_us": pct(record_samples, 50),
                                    "p95_us": pct(record_samples, 95),
                                    "p99_us": pct(record_samples, 99),
                                    "max_us": round(max(record_samples) / 1000, 3)}},
                 "owned_graph": None, "tracemalloc": None, "rss_peak_bytes": rss_bytes(),
                 "rss_current_bytes": current_rss_bytes(),
                 "status": "UNVERIFIED",
                 "note": "non-progressing lookup baseline; no gate threshold"})
    del final

    # Registration timing is exactly the last 1000 accepts in full mode.
    prefill = limit - samples
    admit = fill(limit, stop=prefill - warmup)
    for i in range(prefill - warmup, prefill):
        result = admit.register(**register_args(i))
        if result["classification"] != "registered":
            raise RuntimeError("registration warmup changed classification")
    reg_args = [register_args(i) for i in range(prefill, limit)]
    reg_timing = {}
    def register_time(ledger, disable):
        durations = []
        classes = Counter()
        prior = gc.isenabled()
        gc.disable() if disable else gc.enable()
        try:
            for kw in reg_args:
                start = time.perf_counter_ns()
                result = ledger.register(**kw)
                durations.append(time.perf_counter_ns() - start)
                classes[result["classification"]] += 1
        finally:
            gc.enable() if prior else gc.disable()
        return {"n": len(durations), "p50_us": pct(durations, 50),
                "p95_us": pct(durations, 95), "p99_us": pct(durations, 99),
                "max_us": round(max(durations) / 1000, 3),
                "classifications": dict(classes),
                "slots_before_each": list(range(prefill, limit))}
    reg_timing["gc_disabled"] = register_time(admit, True)
    gc_admit = fill(limit, stop=prefill - warmup)
    for i in range(prefill - warmup, prefill):
        gc_admit.register(**register_args(i))
    reg_timing["gc_enabled"] = register_time(gc_admit, False)
    reg_visit_fixture = fill(limit, stop=limit - 1)
    reg_visit_fixture._records = CountedRecords(reg_visit_fixture._records)
    reg_first = reg_visit_fixture.register(**register_args(limit - 1))
    reg_visits = reg_visit_fixture._records.visits
    reg_temp_fixture = fill(limit, stop=limit - 1)
    reg_temp = peak_call(lambda kw: reg_temp_fixture.register(**kw), register_args(limit - 1))
    reg_clock_ok = reg_timing["gc_disabled"]["p99_us"] <= 20_000 and reg_timing["gc_disabled"]["max_us"] <= 100_000
    reg_classes_ok = (reg_first["classification"] == "registered" and
                      reg_temp["classification"] == "registered" and
                      all(dist["classifications"] == {"registered": dist["n"]}
                          for dist in reg_timing.values()))
    reg_visit_gate = "FAIL" if reg_visits > 64 else "PASS"
    reg_temp_gate = "FAIL" if reg_temp["peak_delta"] > TEMP_LIMIT else "PASS"
    reg_time_gate = "UNVERIFIED" if quick else "PASS" if reg_clock_ok else "FAIL"
    rows.append({"name": "register_accept", "slots": limit, "retained_details": 0,
                 "D": 0, "K": 0, "visits": reg_visits, "visit_limit": 64,
                 "visit_gate": reg_visit_gate,
                 "temporary_gate": reg_temp_gate,
                 "time_gate": reg_time_gate,
                 "timing": reg_timing, "tracemalloc": reg_temp,
                 "first_classification": reg_first["classification"],
                 "expected_classification": "registered",
                 "classification_ok": reg_classes_ok,
                 "owned_graph": None,
                 "status": scenario_status(visit_gate=reg_visit_gate, temp_gate=reg_temp_gate,
                                           time_gate=reg_time_gate, correct=reg_classes_ok)})
    del admit, gc_admit, reg_visit_fixture, reg_temp_fixture

    cap_fixture = fill(limit)
    rows.append(measure_case("register_capacity_reject", cap_fixture, "register",
                             [register_args(limit + i) for i in range(count)], expected="admission_stopped",
                             quick=quick, warmup=warmup, samples=samples))
    del cap_fixture

    edge_factory = lambda: fill(limit, finalized=1, unavailable_rest=True)
    rows.append(one_shot("close_boundary_before", edge_factory, "aggregation_snapshot",
                         dict(as_of=T + 71 * MINUTE - 1, as_of_mono=T + 71 * MINUTE - 1),
                         d=0, k=0, expected="snapshot", quick=quick,
                         warmup=warmup, samples=samples))
    rows.append(one_shot("close_boundary_exact", edge_factory, "aggregation_snapshot",
                         dict(as_of=T + 71 * MINUTE, as_of_mono=T + 71 * MINUTE),
                         d=1, k=0, expected="snapshot", quick=quick,
                         warmup=warmup, samples=samples))
    rows.append(one_shot("large_clock_jump", edge_factory, "aggregation_snapshot",
                         dict(as_of=T + 180 * MINUTE, as_of_mono=T + 180 * MINUTE),
                         d=1, k=0, expected="snapshot", quick=quick,
                         warmup=warmup, samples=samples))
    close_ledger = edge_factory()
    close_ledger.aggregation_snapshot(as_of=T + 71 * MINUTE, as_of_mono=T + 71 * MINUTE)
    rows.append(measure_case("close_same_time_requery", close_ledger, "aggregation_snapshot",
                             [dict(as_of=T + 71 * MINUTE, as_of_mono=T + 71 * MINUTE)
                              for _ in range(count)], expected="snapshot",
                             quick=quick, warmup=warmup, samples=samples, details=0))
    del close_ledger
    rows.append(one_shot("mass_close_boundary", lambda: fill(
        limit, finalized=min(DETAIL_CAP, limit), unavailable_rest=True),
                         "aggregation_snapshot",
                         dict(as_of=T + 71 * MINUTE, as_of_mono=T + 71 * MINUTE),
                         d=min(DETAIL_CAP, limit), k=0, expected="snapshot", quick=quick,
                         warmup=warmup, samples=samples))
    rows.append(one_shot("mass_overdue", lambda: fill(limit), "aggregation_snapshot",
                         dict(as_of=T + 15 * MINUTE, as_of_mono=T + 15 * MINUTE),
                         d=limit, k=0, expected="snapshot", quick=quick,
                         warmup=warmup, samples=samples))

    for name, n, release, reject in (
        ("resident_unfinished", 0, False, False),
        ("resident_2048_details", min(DETAIL_CAP, limit), False, False),
        ("resident_released_identity", min(DETAIL_CAP, limit), True, False),
        ("resident_capacity_stop", 0, False, True),
    ):
        rows.append(memory_state(name, limit, detail_count=n, release=release,
                                 reject=reject, quick=quick))

    adapter, adapter_rows = adapter_status(limit=limit, demand=demand, warmup=warmup,
                                           samples=samples, quick=quick)
    rows.extend(adapter_rows)
    statuses = summarize(rows)
    gated = [row for row in rows if row["name"] != "record_baseline"]
    overall = ("FAIL" if any(row["status"] == "FAIL" for row in gated) or
               adapter["status"] == "FAIL" else
               "UNVERIFIED" if quick or any(row["status"] != "PASS" for row in gated) or
               adapter["status"] != "PASS" else "PASS")
    report = {"contract": "slice5a_contract_r2.md S5a.4", "mode": "quick" if quick else "full",
              "overall": overall, "environment": env, "prerequisites": prereq,
              "wrapper_equivalence_visits": equivalent_visits, "record_access_audit_findings": audit,
              "adapter": adapter, "limits": {"max_records": CAP, "max_retained_details": DETAIL_CAP,
                                          "max_detail_bytes": 4096, "owned_bytes": OWNED_LIMIT,
                                          "temporary_bytes": TEMP_LIMIT},
              "scenarios": rows, "summary": statuses,
              "reproduce": "PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python3.13 scripts/d7_ledger_measure_gate.py"
                           + (" --quick" if quick else "")}
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    print(f"D7 measure {report['mode']}: {overall}; "
          f"PASS={statuses.get('PASS', 0)} FAIL={statuses.get('FAIL', 0)} "
          f"UNVERIFIED={statuses.get('UNVERIFIED', 0)}", file=sys.stderr)
    for row in rows:
        trace = row.get("tracemalloc") or {}
        print(f"  {row['name']}: {row['status']} D={row['D']} K={row['K']} "
              f"visits={row['visits']} peak_delta={trace.get('peak_delta')}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
