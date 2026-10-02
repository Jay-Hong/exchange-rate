"""Redis-only FX collection stall monitor.

The in-process runtime and one-minute job assume a single app worker (--workers 1).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.collection_slots import generate_candidates
from app.crawlers.constants import MIBANK_REQUIRED_PAIRS

TARGET_SOURCES = ("investing", "kb", "hana", "woori", "shinhan", "nh", "sc", "ibk", "bs", "citi")
QUEUE_SOURCES = frozenset({"shinhan", "nh", "sc", "ibk"})
STATE_KEY_PREFIX = "source_health:stall_state:"
_UTC = timezone.utc
_KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("exchange_rate.source_stall_monitor")


@dataclass(frozen=True)
class StallConfig:
    min_gap_s: int = 600
    lag_request_s: int = 120
    lag_queue_s: int = 300
    lookback_s: int = 2700


@dataclass
class MonitorRuntime:
    started_utc: datetime
    enabled_since: dict[str, datetime | None] = field(default_factory=dict)
    redis_failure_since: datetime | None = None
    read_outage: bool = False
    # Delivered transitions whose final state write has not succeeded yet: never resent,
    # only the write is retried (single process, see module docstring).
    unpersisted: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    unavailable_notified: bool = False
    resumed_after: datetime | None = None
    last_warning_at: datetime | None = None
    last_ages_hour: int | None = None
    startup_sent: bool = False
    message_attempted_at: datetime | None = None


def _warn(runtime: MonitorRuntime, now: datetime, operation: str, exc: Exception) -> None:
    if runtime.last_warning_at is None or (now - runtime.last_warning_at).total_seconds() >= 3600:
        logger.warning("source_stall_monitor %s failed: %s", operation, type(exc).__name__)
        runtime.last_warning_at = now


def _decode_hash(raw: dict) -> dict[str, str]:
    return {
        (key.decode() if isinstance(key, bytes) else key):
        (value.decode() if isinstance(value, bytes) else value)
        for key, value in raw.items()
    }


def _transition_log(kind: str, source: str, pairs: list[str], last_seen, span_s, slots, mode) -> None:
    logger.info("source_stall_transition %s", json.dumps({
        "kind": kind, "source": source, "pairs": pairs, "last_seen": last_seen,
        "span_s": span_s, "slots": slots, "mode": mode, "sent": False,
    }, ensure_ascii=False, separators=(",", ":")))


def _message(events: list[tuple[str, str, str, int | None]]) -> str:
    """One plain-text message; stall lines name the oldest receipt, recovery the longest gap."""
    groups: dict[tuple[str, str], list[tuple[str, int | None]]] = defaultdict(list)
    for kind, source, pair, value_ms in events:
        groups[(kind, source)].append((pair.split("-")[0].upper(), value_ms))
    lines = []
    for (kind, source), items in groups.items():
        names = "·".join(name for name, _ in items)
        if kind == "stalled":
            oldest = min(value for _, value in items)
            detail = ("수신 기록 없음" if oldest <= 0 else "마지막 수신 " +
                      datetime.fromtimestamp(oldest / 1000, _KST).strftime("%m-%d %H:%M KST"))
            lines.append(f"{source} {names}: 멈춤 ({detail})")
        else:
            gaps = [value for _, value in items if value is not None]
            detail = f"멈춘 시간 약 {max(gaps) // 60000}분" if gaps else "첫 수신"
            lines.append(f"{source} {names}: 복구 ({detail})")
    return "수집 멈춤 감시\n" + "\n".join(lines)


def _send_safe(runtime: MonitorRuntime, now: datetime, send, message: str) -> bool:
    if runtime.message_attempted_at == now:
        return False
    runtime.message_attempted_at = now
    try:
        ok = send(message) is True
    except Exception as exc:
        _warn(runtime, now, "send", exc)
        return False
    if not ok:
        _warn(runtime, now, "send", RuntimeError("delivery returned false"))
    return ok


def _redis_failed(runtime: MonitorRuntime, now: datetime, operation: str, exc: Exception) -> None:
    """Any Redis read or state-write failure counts toward the 30-minute outage notice."""
    if runtime.redis_failure_since is None:
        runtime.redis_failure_since = now
    _warn(runtime, now, operation, exc)


def _monitor_notice(runtime: MonitorRuntime, now: datetime, send, send_enabled: bool,
                    *, redis_healthy: bool) -> bool | None:
    """Select a monitor notice before transitions; None means no notice is due."""
    if redis_healthy:
        if not runtime.unavailable_notified:
            return None
        # Reads, intents and outstanding final writes succeeded. No transition is sent
        # on this tick, so there are no further Redis writes before it is healthy.
        runtime.redis_failure_since = None
        message, kind = "수집 감시 재개", "monitor_resumed"
    else:
        if (runtime.redis_failure_since is None or runtime.unavailable_notified
                or (now - runtime.redis_failure_since).total_seconds() < 1800):
            return None
        message, kind = "수집 감시 불가 (Redis 실패)", "monitor_unavailable"
    delivered = not send_enabled or _send_safe(runtime, now, send, message)
    if delivered:
        runtime.unavailable_notified = not redis_healthy
        if not send_enabled:
            _transition_log(kind, "monitor", [], None, None, 0, None)
    return delivered


def _persist_final(runtime: MonitorRuntime, client, state_key: str) -> None:
    """Retry delivered transitions without sending them again; writes are idempotent."""
    notified = {key: value for key, (kind, value) in runtime.unpersisted.items()
                if kind == "stalled"}
    recovered = [key for key, (kind, _) in runtime.unpersisted.items() if kind == "recovered"]
    if notified:
        client.hset(state_key, mapping=notified)
    if recovered:
        client.hdel(state_key, *recovered)
    runtime.unpersisted.clear()


def run_tick(runtime: MonitorRuntime, *, now_utc: datetime, client, is_enabled, send,
             send_enabled: bool, config: StallConfig = StallConfig(),
             candidates_fn=generate_candidates) -> bool:
    """Evaluate scheduled slots once, persist intent, then deliver at most one message.

    Returns True only when the tick read and wrote Redis without failure (and its message,
    if any, was delivered); the startup notice depends on it.
    """
    now = now_utc.astimezone(_UTC)
    namespace = "live" if send_enabled else "shadow"
    state_key = STATE_KEY_PREFIX + namespace
    try:
        seen = {key: int(value) for key, value in
                _decode_hash(client.hgetall("source_health:last_valid_seen")).items()}
        state = {key: json.loads(value) for key, value in
                 _decode_hash(client.hgetall(state_key)).items()}
    except Exception as exc:
        runtime.read_outage = True
        _redis_failed(runtime, now, "read", exc)
        _monitor_notice(runtime, now, send, send_enabled, redis_healthy=False)
        return False

    if runtime.read_outage:
        # Slots that fell inside a read outage were never judged on fresh data: start every
        # source at now. Write-only failures keep judging fresh reads, so they need no floor.
        runtime.resumed_after = now
        runtime.read_outage = False

    enabled: dict[str, bool] = {}
    try:
        for source in TARGET_SOURCES:
            active = bool(is_enabled(source))
            enabled[source] = active
            if not active:
                runtime.enabled_since[source] = None
            elif runtime.enabled_since.get(source) is None:
                runtime.enabled_since[source] = (now if source in runtime.enabled_since
                                                 else runtime.started_utc)
        start_window = now - timedelta(seconds=config.lookback_s)
        end_window = now - timedelta(seconds=min(config.lag_request_s, config.lag_queue_s))
        candidates = candidates_fn(start_window, end_window + timedelta(microseconds=1))
    except Exception as exc:
        _warn(runtime, now, "candidates", exc)
        return False

    due_by_source: dict[str, list] = defaultdict(list)
    for candidate in candidates:
        if candidate.crawler in TARGET_SOURCES:
            due_by_source[candidate.crawler].append(candidate)

    intent: dict[str, str] = {}
    events: list[tuple[str, str, str, int | None]] = []
    event_logs: list[tuple[str, str, str, int, int | None, int, str | None]] = []
    ages = {}
    for source in TARGET_SOURCES:
        for pair in MIBANK_REQUIRED_PAIRS:
            key = f"{source}:{pair}"
            last_ms = seen.get(key, 0)
            ages[key] = {"age_s": round((now.timestamp() * 1000 - last_ms) / 1000)
                         if last_ms else None, "held": not enabled[source]}
            if not enabled[source]:
                continue
            prior = state.get(key)
            if key in runtime.unpersisted:
                continue
            if prior is not None:
                if not prior.get("notified", False) and not prior.get("recovered"):
                    # Deliver a pending stall before its recovery if receipt returned
                    # while Telegram was unavailable on the previous tick.
                    events.append(("stalled", source, pair, prior.get("seen_at_stall_ms", 0)))
                    event_logs.append(("stalled", source, pair,
                                       prior.get("seen_at_stall_ms", 0), None, 0, None))
                elif prior.get("recovered") or last_ms > prior.get("seen_at_stall_ms", 0):
                    if not prior.get("recovered"):
                        # The gap is fixed at first detection so retries report the same event;
                        # a stall that began with no receipt at all has no gap (first receipt).
                        seen_at_stall = prior.get("seen_at_stall_ms", 0)
                        prior = {**prior, "recovered": True,
                                 "recovered_gap_ms": (last_ms - seen_at_stall
                                                      if seen_at_stall > 0 else None)}
                        intent[key] = json.dumps(prior, separators=(",", ":"))
                    events.append(("recovered", source, pair, prior.get("recovered_gap_ms")))
                    event_logs.append(("recovered", source, pair, last_ms, None, 0, None))
                continue

            lag = config.lag_queue_s if source in QUEUE_SOURCES else config.lag_request_s
            lower = max(runtime.started_utc, runtime.enabled_since[source], start_window,
                        runtime.resumed_after or runtime.started_utc,
                        datetime.fromtimestamp(last_ms / 1000, _UTC))
            eligible = [slot for slot in due_by_source[source]
                        if lower < slot.due_at_utc <= now - timedelta(seconds=lag)]
            if len(eligible) < 2:
                ages[key]["held"] = not eligible
                continue
            span = (eligible[-1].due_at_utc - eligible[0].due_at_utc).total_seconds()
            if span < config.min_gap_s:
                continue
            entry = {"stalled_since_ms": int(now.timestamp() * 1000),
                     "seen_at_stall_ms": last_ms, "notified": False}
            intent[key] = json.dumps(entry, separators=(",", ":"))
            state[key] = entry
            events.append(("stalled", source, pair, last_ms))
            event_logs.append(("stalled", source, pair, last_ms, int(span),
                               len(eligible), eligible[-1].mode))

    try:
        if intent:
            client.hset(state_key, mapping=intent)
    except Exception as exc:
        _redis_failed(runtime, now, "intent write", exc)
        _monitor_notice(runtime, now, send, send_enabled, redis_healthy=False)
        return False

    # Settle earlier deliveries before choosing this tick's notice. A write failure
    # keeps the outage active but need not block transitions for other keys forever.
    redis_healthy = True
    try:
        _persist_final(runtime, client, state_key)
    except Exception as exc:
        redis_healthy = False
        _redis_failed(runtime, now, "final state write", exc)

    hour = int(now.timestamp() // 3600)
    if not send_enabled and runtime.last_ages_hour != hour:
        logger.info("source_stall_ages %s", json.dumps(ages, ensure_ascii=False, separators=(",", ":")))
        runtime.last_ages_hour = hour

    notice = _monitor_notice(runtime, now, send, send_enabled, redis_healthy=redis_healthy)
    if notice is not None:
        # A monitor notice, including a failed attempt, takes the tick's sole slot.
        return redis_healthy and notice

    delivered = True
    if events:
        if not send_enabled:
            # Include intents deferred by a monitor notice, logging only on delivery.
            for kind, source, pair, last_ms, span, slots, mode in event_logs:
                _transition_log(kind, source, [pair], last_ms, span, slots, mode)
        else:
            delivered = _send_safe(runtime, now, send, _message(events))
        if delivered:
            for kind, source, pair, _ in events:
                key = f"{source}:{pair}"
                runtime.unpersisted[key] = (
                    (kind, None) if kind == "recovered" else
                    (kind, json.dumps({**state[key], "notified": True}, separators=(",", ":"))))

    if not redis_healthy:
        return False
    try:
        _persist_final(runtime, client, state_key)
    except Exception as exc:
        _redis_failed(runtime, now, "final state write", exc)
        # The transition already took this tick's slot; an outage notice waits.
        return False
    # Every Redis read and write this tick succeeded.
    runtime.redis_failure_since = None
    return delivered


def default_send(text: str) -> bool:
    from app.notifications.telegram import telegram_handler
    return telegram_handler.send_message(text, parse_mode="")


def register(scheduler) -> None:
    """Register one process-local tick (the deployment runs with --workers 1)."""
    from app import config
    from app import latest_rates_cache
    from app.scheduler import crawler_manager

    runtime = MonitorRuntime(datetime.now(_UTC))

    def job() -> None:
        try:
            now = datetime.now(_UTC)
            try:
                client = latest_rates_cache._get_sync_client()
            except Exception:
                client = None  # run_tick accounts for this as a Redis read outage.
            send_enabled = config.SOURCE_STALL_ALERT_SEND
            healthy = run_tick(runtime, now_utc=now, client=client,
                               is_enabled=crawler_manager.is_enabled, send=default_send,
                               send_enabled=send_enabled,
                               config=StallConfig(config.SOURCE_STALL_MIN_GAP_S,
                                                  config.SOURCE_STALL_LAG_REQUEST_S,
                                                  config.SOURCE_STALL_LAG_QUEUE_S,
                                                  config.SOURCE_STALL_LOOKBACK_S))
            if (healthy and send_enabled and not runtime.startup_sent
                    and runtime.redis_failure_since is None
                    and runtime.message_attempted_at != now):
                raw = client.hgetall(STATE_KEY_PREFIX + "live")
                current_stalls = len(raw)
                if _send_safe(runtime, now, default_send,
                              f"수집 멈춤 감시 시작 (10개 소스·30항목, 현재 멈춤 {current_stalls})"):
                    runtime.startup_sent = True
        except Exception as exc:
            _warn(runtime, datetime.now(_UTC), "job", exc)

    # :25 avoids every fixed crawler second in the current four-mode schedule.
    scheduler.add_job(job, CronTrigger(second=25, timezone="Asia/Seoul"),
                      id="source_stall_check", coalesce=True, max_instances=1)
