"""Process-local configuration evidence; never a control or persistence layer.

Only a stable baseline and acknowledged commits establish policy. Cache writes
are diagnostic evidence. All hooks are isolated from the operations they watch.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps

from app.collection_policy import POLICY
from app.collection_slots import ConfigState


CRAWLERS = tuple(sorted({job.crawler for jobs in POLICY.values() for job in jobs.values()}))
_UNKNOWN = ConfigState(None, None)
_COUNTERS = ("ack", "not_entered", "result_unknown", "duplicate_ack",
             "stale_ack_ignored", "hook_failure", "cache_applied")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return value.astimezone(timezone.utc)


def new_call_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class _Point:
    at: datetime
    enabled: bool | None
    revision: str | None
    reason: str | None


@dataclass(frozen=True)
class _Call:
    crawler: str
    requested: bool
    started_at: datetime


@dataclass
class _Crawler:
    generation: int = 0
    baseline_id: str | None = None
    revision: int = 0
    blocked: str | None = "no_baseline"
    history: deque = field(default_factory=deque)
    truncated_before: datetime | None = None
    cache: deque = field(default_factory=lambda: deque(maxlen=8))


def _hook(method):
    """Serialize small memory updates and invalidate evidence on partial failure."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        crawler = None
        try:
            with self._lock:
                try:
                    if method.__name__ != "start_epoch":
                        self._check_tracking()
                    if method.__name__ in {"commit_started", "baseline_row", "cache_applied"}:
                        crawler = args[0] if args else kwargs.get("crawler")
                    elif "call_id" in kwargs:
                        call = self._inflight.get(kwargs["call_id"])
                        crawler = call.crawler if call is not None else None
                    return method(self, *args, **kwargs)
                except BaseException:
                    self._failure(method.__name__, crawler)
        except BaseException:
            # Even damaged tracking/clock state must not turn into a business error.
            # Assignment is the last-resort latch; only start_epoch can clear it.
            self._tracking_degraded = True
            self._degraded_at = self._started_at
        return "" if method.__name__ == "baseline_begin" else None
    return guarded


class ConfigObserver:
    INFLIGHT_CAP = 256
    HISTORY_CAP = 1024
    COMPLETED_IDS_CAP = 4096

    def __init__(self):
        self._lock = threading.Lock()
        self._process_instance_id = None
        self._started_at = None
        self._tracking_degraded = False
        self._degraded_at = None
        self._crawlers = {name: _Crawler() for name in CRAWLERS}
        self._inflight = {}
        self._inflight_count = 0
        self._completed = OrderedDict()
        self._baseline = None
        self._counters = dict.fromkeys(_COUNTERS, 0)

    def _failure_time(self):
        try:
            return _utc(_now())
        except BaseException:
            # Without a timestamp, invalidate conservatively from the epoch start.
            return self._started_at or datetime.min.replace(tzinfo=timezone.utc)

    def _degrade(self, at):
        self._tracking_degraded = True
        if self._degraded_at is None or at < self._degraded_at:
            self._degraded_at = at

    def _check_tracking(self):
        try:
            if (len(self._inflight) != self._inflight_count
                    or any(not isinstance(call, _Call) for call in self._inflight.values())):
                raise ValueError("damaged unfinished-call tracking")
        except BaseException:
            self._degrade(self._failure_time())
            raise

    def _append(self, state, at, enabled, revision, reason):
        if state.history and at < state.history[-1].at:
            # Clock regression cannot establish an ordered policy timeline.
            self._degrade(at)
            return
        state.history.append(_Point(at, enabled, revision, reason))
        if len(state.history) > self.HISTORY_CAP:
            state.history.popleft()
            state.truncated_before = state.history[0].at

    def _unknown(self, crawler, at, reason):
        state = self._crawlers[crawler]
        state.generation += 1
        state.blocked = reason
        self._append(state, at, None, None, reason)

    def _failure(self, hook, crawler):
        self._counters["hook_failure"] += 1
        at = self._failure_time()
        if hook in {"commit_started", "start_epoch"}:
            self._degrade(at)
        elif type(crawler) is str and crawler in self._crawlers:
            self._unknown(crawler, at, "hook_failure")
        else:
            for name in self._crawlers:
                self._unknown(name, at, "hook_failure")

    @_hook
    def hook_failed(self, hook, *, crawler=None, call_id=None):
        """Also handle failures in caller-side ID/time acquisition or patched hooks."""
        if crawler is None and call_id in self._inflight:
            crawler = self._inflight[call_id].crawler
        self._failure(hook, crawler)

    @_hook
    def start_epoch(self, process_instance_id: str) -> None:
        at = _utc(_now())
        if type(process_instance_id) is not str or not process_instance_id:
            raise ValueError("process instance ID is required")
        self._process_instance_id = process_instance_id
        self._started_at = at
        self._crawlers = {name: _Crawler() for name in CRAWLERS}
        self._inflight = {}
        self._inflight_count = 0
        self._completed = OrderedDict()
        self._baseline = None
        self._counters = dict.fromkeys(_COUNTERS, 0)
        self._degraded_at = None
        self._tracking_degraded = False

    @_hook
    def commit_started(self, crawler: str | None, requested: bool | None, *, call_id: str) -> None:
        at = _utc(_now())
        if (type(crawler) is not str or crawler not in self._crawlers
                or type(requested) is not bool or type(call_id) is not str or not call_id):
            raise ValueError("invalid commit observation")
        if self._tracking_degraded or self._started_at is None:
            return
        if call_id in self._inflight or call_id in self._completed:
            raise ValueError("call IDs cannot be reused")
        if len(self._inflight) >= self.INFLIGHT_CAP:
            self._degrade(at)
            return
        if any(call.crawler == crawler for call in self._inflight.values()):
            self._unknown(crawler, at, "overlapping_calls")
        self._crawlers[crawler].generation += 1
        self._inflight[call_id] = _Call(crawler, requested, at)
        self._inflight_count += 1

    def _finish(self, call_id):
        # Call only after the terminal evidence has been recorded successfully.
        # A failure must leave the unfinished record available, never silently drop it.
        try:
            self._completed[call_id] = None
            if len(self._completed) > self.COMPLETED_IDS_CAP:
                self._completed.popitem(last=False)
            del self._inflight[call_id]
            self._inflight_count -= 1
        except BaseException:
            self._degrade(self._failure_time())
            raise

    @_hook
    def commit_not_entered(self, *, call_id: str, error_type: str) -> None:
        call = self._inflight.get(call_id)
        if call is None:
            return
        self._crawlers[call.crawler].generation += 1
        self._counters["not_entered"] += 1
        self._finish(call_id)

    @_hook
    def commit_result_unknown(self, *, call_id: str, error_type: str) -> None:
        call = self._inflight.get(call_id)
        if call is None:
            return
        self._unknown(call.crawler, _utc(_now()), "commit_result_unknown")
        self._counters["result_unknown"] += 1
        self._finish(call_id)

    @_hook
    def commit_ack(self, *, call_id: str, ack_at: datetime) -> None:
        # An old, still unfinished call takes precedence over the completed FIFO.
        call = self._inflight.get(call_id)
        if call is None:
            counter = "duplicate_ack" if call_id in self._completed else "stale_ack_ignored"
            self._counters[counter] += 1
            return
        at = _utc(ack_at)
        if at < call.started_at:
            self._degrade(at)
        state = self._crawlers[call.crawler]
        state.generation += 1
        state.revision += 1
        revision = f"{state.baseline_id}:{call.crawler}:{state.revision}"
        self._append(state, at, call.requested if state.blocked is None else None,
                     revision if state.blocked is None else None, state.blocked)
        self._counters["ack"] += 1
        self._finish(call_id)

    @_hook
    def cache_applied(self, crawler: str, enabled: bool, at: datetime) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a bool")
        self._crawlers[crawler].cache.append((_utc(at), enabled))
        self._counters["cache_applied"] += 1

    @_hook
    def baseline_begin(self) -> str:
        baseline_id = uuid.uuid4().hex
        self._baseline = {
            "id": baseline_id, "started_at": _utc(_now()), "ended_at": None,
            "ok": False, "known": (), "unknown": CRAWLERS, "missing": (),
            "rows": {},
            "generations": {name: state.generation for name, state in self._crawlers.items()},
            "busy": {call.crawler for call in self._inflight.values()},
        }
        return baseline_id

    def _active_baseline(self, baseline_id):
        return (self._baseline is not None and self._baseline["id"] == baseline_id
                and self._baseline["ended_at"] is None)

    @_hook
    def baseline_row(self, crawler: str, enabled: bool, *, baseline_id: str) -> None:
        if not self._active_baseline(baseline_id):
            return
        if crawler not in self._crawlers or type(enabled) is not bool:
            raise ValueError("invalid baseline row")
        rows = self._baseline["rows"]
        if crawler in rows:
            raise ValueError("duplicate baseline row")
        rows[crawler] = enabled

    @_hook
    def baseline_end(self, *, baseline_id: str, ok: bool, missing: tuple[str, ...] = ()) -> None:
        if not self._active_baseline(baseline_id):
            return
        at = _utc(_now())
        baseline = self._baseline
        rows = baseline["rows"]
        absent = set(CRAWLERS).difference(rows).union(missing)
        busy = {call.crawler for call in self._inflight.values()}
        known = []
        for name, state in self._crawlers.items():
            if (ok is True and name not in absent and not self._tracking_degraded
                    and self._started_at is not None
                    and state.generation == baseline["generations"][name]
                    and name not in baseline["busy"] and name not in busy):
                state.baseline_id = baseline_id
                state.revision = 0
                state.blocked = None
                self._append(state, at, rows[name], f"{baseline_id}:{name}:0", None)
                known.append(name)
            else:
                reason = "baseline_failed" if ok is not True else (
                    "baseline_missing" if name in absent else "baseline_unstable")
                self._unknown(name, at, reason)
        baseline.update(ended_at=at, ok=ok is True, known=tuple(known),
                        unknown=tuple(name for name in CRAWLERS if name not in known),
                        missing=tuple(sorted(absent)))
        # Only the most recent baseline is retained; history points own revisions.
        for key in ("rows", "generations", "busy"):
            baseline.pop(key)

    def _point_at(self, crawler, at):
        if self._started_at is None or at < self._started_at:
            return _Point(at, None, None, "outside_epoch")
        if self._tracking_degraded and (self._degraded_at is None or at >= self._degraded_at):
            return _Point(at, None, None, "tracking_degraded")
        state = self._crawlers.get(crawler)
        if state is None:
            return _Point(at, None, None, "unknown_crawler")
        if state.truncated_before is not None and at < state.truncated_before:
            return _Point(at, None, None, "history_truncated")
        if any(call.crawler == crawler and at >= call.started_at for call in self._inflight.values()):
            return _Point(at, None, None, "commit_inflight")
        for point in reversed(state.history):
            if point.at <= at:
                return point
        return _Point(at, None, None, "no_baseline")

    def config_at(self, crawler: str, at_utc: datetime) -> ConfigState:
        try:
            at = _utc(at_utc)
            with self._lock:
                self._check_tracking()
                point = self._point_at(crawler, at)
                if type(point.enabled) is bool and type(point.revision) is str:
                    return ConfigState(point.enabled, point.revision)
        except BaseException:
            # Invalid lookup input/internal state is never affirmative evidence.
            return _UNKNOWN
        return _UNKNOWN

    def snapshot(self) -> dict:
        with self._lock:
            at = self._failure_time()
            crawlers = {}
            for name, state in self._crawlers.items():
                point = self._point_at(name, at)
                crawlers[name] = {
                    "enabled": point.enabled, "config_revision": point.revision,
                    "effective_at": point.at if point.enabled is not None else None,
                    "unknown_reason": point.reason, "history_len": len(state.history),
                    "truncated_before": state.truncated_before,
                    "cache_applied": tuple(state.cache),
                }
            baseline = self._baseline or {
                "id": None, "started_at": None, "ended_at": None, "ok": False,
                "known": (), "unknown": CRAWLERS, "missing": (),
            }
            reason = "tracking_degraded" if self._tracking_degraded else (
                "baseline_unverified" if not baseline["ok"] else (
                    "crawler_unknown" if any(row["enabled"] is None for row in crawlers.values()) else None))
            return {
                "epoch": {"process_instance_id": self._process_instance_id, "started_at": self._started_at},
                "readiness": {"state": "degraded" if reason else "ok", "reason": reason},
                "baseline": {key: baseline[key] for key in
                             ("id", "started_at", "ended_at", "ok", "known", "unknown", "missing")},
                "crawlers": crawlers, "counters": dict(self._counters),
                "limits": {"inflight_cap": self.INFLIGHT_CAP, "inflight_len": len(self._inflight),
                           "history_cap": self.HISTORY_CAP, "completed_ids_cap": self.COMPLETED_IDS_CAP,
                           "completed_ids_len": len(self._completed)},
            }


observer = ConfigObserver()


def observation_failed(hook, *, crawler=None, call_id=None):
    """Caller-side isolation must invalidate evidence even when a hook is replaced."""
    try:
        observer.hook_failed(hook, crawler=crawler, call_id=call_id)
    except BaseException:
        # The real observer has its own last-resort latch; an injected observer
        # must likewise never replace the original operation's result/exception.
        if isinstance(observer, ConfigObserver):
            observer._tracking_degraded = True
            observer._degraded_at = observer._started_at
