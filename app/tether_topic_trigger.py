"""Tether topic publish trigger controller (Phase B.2 PR1+PR2).

This module decides *when* the tether topic should be published. The existing
`tether_topic_publisher` module still owns *how* to build and publish the
snapshot.

Modes:
    - legacy_piggyback: noop. Existing `main.py` legacy broadcast hook remains
      the only publisher.
    - dual_shadow: collect direct triggers and run the same coalesce timer as
      direct mode, but skip the publish call. This gives realistic publish
      frequency telemetry before switching production traffic.
    - direct_coalesced: coalesce triggers per topic and call the existing safe
      publisher once per flush window.

PR2 add (2026-05-15):
    - Reason 상수 centralization (호출자가 string literal 직접 사용 금지).
    - Redis-backed telemetry — `topic:tether:stats` hash에 `trigger_*` prefix
      10 counter + 7 last/HSET fields 기록. circuit_breaker.record_failure() 호출
      X (telemetry 실패가 broadcast/latest mirror 같은 core Redis 경로에 영향
      미치지 않게 격리).
    - Hook: `UpbitRedisWriter._write_async` (PR2 callsite, lock 밖).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from pytz import timezone as pytz_timezone

from app import config
from app.cache import redis_cache
from app.tether_topic_publisher import TETHER_TOPIC

logger = logging.getLogger("exchange_rate.tether_topic_trigger")
_KST = pytz_timezone("Asia/Seoul")

MODE_LEGACY_PIGGYBACK = "legacy_piggyback"
MODE_DUAL_SHADOW = "dual_shadow"
MODE_DIRECT_COALESCED = "direct_coalesced"

DEFAULT_CLOSE_TIMEOUT_SEC = 1.0

# Reason 상수 — 호출자가 string literal 대신 본 상수를 import해 사용.
# Redis telemetry `last_reason` field에 기록되며 검색/집계 일관성 보장.
TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS = "usdt_ws_redis_write_success"
# KRX는 "ws" prefix 미포함 — REST fallback이 동일 DB path 공유 가능 +
# 미래 KRX tick-level Redis writer 도입 시에도 같은 reason 유지 (hook 위치만 이동).
TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS = "krx_redis_write_success"

# Redis-backed telemetry — `tether_topic_publisher`와 동일 hash 재사용
# (운영 admin 단일 조회 지점 유지). `trigger_*` prefix로 publish counter와 분리.
_TELEMETRY_KEY: str = "topic:tether:stats"
_TRIGGER_PREFIX: str = "trigger_"
_LAST_ERROR_MAX_LEN: int = 500

# `trigger_*` counter 필드 — 10개 (publish 측은 prefix 없이 분리).
# Last/HSET fields 7개는 _record_trigger_event 내부에서 별도 set (last_result,
# last_reason, last_source, last_asset, last_window_ms, last_error, last_at_kst).
TRIGGER_COUNTER_FIELDS = (
    "request",
    "skipped_legacy",
    "coalesced",
    "flush_dual_shadow",
    "flush_direct",
    "publish_called",
    "publish_success",
    "publish_skipped_shadow",
    "no_loop",
    "error",
)

# Fire-and-forget telemetry task strong reference set — Python asyncio docs:
# "Save a reference to the result of [create_task], to avoid a task
# disappearing mid-execution." close() drain 대상 아님 (best-effort + shutdown
# latency 영향 X). add_done_callback(discard)로 즉시 cleanup.
_telemetry_tasks: "set[asyncio.Task]" = set()

PublishCallable = Callable[[], Awaitable[bool]]


@dataclass
class TetherTopicTriggerStats:
    """In-process PR1 telemetry snapshot.

    Redis-backed counters can be added after the direct hook is connected and
    operational questions are clearer. PR1 keeps telemetry local and testable.
    """

    trigger_count: int = 0
    flush_count_dual_shadow: int = 0
    flush_count_direct: int = 0
    publish_called: int = 0
    publish_success: int = 0
    publish_skipped_shadow: int = 0
    publish_skipped_legacy: int = 0
    coalesced_trigger_count: int = 0
    last_mode: Optional[str] = None
    last_topic: Optional[str] = None
    last_reason: Optional[str] = None
    last_source: Optional[str] = None
    last_asset: Optional[str] = None
    last_window_ms: Optional[float] = None
    last_error: Optional[str] = None


@dataclass
class _PendingTopic:
    task: asyncio.Task
    trigger_count: int = 1
    first_trigger_at: float = field(default_factory=time.monotonic)
    last_source: str = ""
    last_asset: str = ""
    last_reason: str = ""


async def _record_trigger_event(
    *,
    counter: Optional[str] = None,
    last_result: Optional[str] = None,
    last_reason: Optional[str] = None,
    last_source: Optional[str] = None,
    last_asset: Optional[str] = None,
    last_window_ms: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """best-effort Redis telemetry — circuit_breaker 오염 차단.

    원칙 (publisher `_record_topic_event` mirror):
        - redis_cache.client raw 직접 사용 (circuit wrapper 우회)
        - circuit.can_attempt() 체크만 — record_failure() 호출 X
        - 실패 시 logger.debug + 조용히 skip
        - telemetry 실패가 trigger/publish/broadcast 정상 경로에 영향 X

    Args:
        counter: `TRIGGER_COUNTER_FIELDS` 중 하나 — hincrby 적용.
        last_result: `trigger_last_result` 에 기록 (예: "publish_success" /
            "publish_skipped_shadow" / "skipped_legacy" / "error").
        last_reason/source/asset/window_ms: 보조 진단 필드. None이면 skip.
        error: 예외 문자열 (호출자가 길이 제한, 본 함수가 추가 cap).
    """
    client = redis_cache.client
    if client is None:
        return
    try:
        if not await redis_cache.circuit.can_attempt():
            return
    except Exception:
        return

    now_iso = datetime.now(_KST).isoformat()

    try:
        if counter is not None and counter in TRIGGER_COUNTER_FIELDS:
            await client.hincrby(_TELEMETRY_KEY, f"{_TRIGGER_PREFIX}{counter}", 1)
        if last_result is not None:
            await client.hset(
                _TELEMETRY_KEY, f"{_TRIGGER_PREFIX}last_result", last_result
            )
        if last_reason is not None:
            await client.hset(
                _TELEMETRY_KEY, f"{_TRIGGER_PREFIX}last_reason", last_reason
            )
        if last_source is not None:
            await client.hset(
                _TELEMETRY_KEY, f"{_TRIGGER_PREFIX}last_source", last_source
            )
        if last_asset is not None:
            await client.hset(
                _TELEMETRY_KEY, f"{_TRIGGER_PREFIX}last_asset", last_asset
            )
        if last_window_ms is not None:
            await client.hset(
                _TELEMETRY_KEY,
                f"{_TRIGGER_PREFIX}last_window_ms",
                f"{last_window_ms:.2f}",
            )
        if error is not None:
            await client.hset(
                _TELEMETRY_KEY,
                f"{_TRIGGER_PREFIX}last_error",
                error[:_LAST_ERROR_MAX_LEN],
            )
        await client.hset(
            _TELEMETRY_KEY, f"{_TRIGGER_PREFIX}last_at_kst", now_iso
        )
    except Exception:
        # circuit_breaker.record_failure() 호출 X — telemetry 실패 격리
        logger.debug(
            "trigger telemetry 기록 실패 (격리, broadcast/trigger 영향 X)",
            exc_info=True,
        )


class TetherTopicTriggerController:
    """Coalesces tether topic publish triggers.

    The controller is intentionally source-agnostic at the publish level:
    trigger metadata keeps `(source, asset, reason)` for diagnostics, but the
    actual flush is per topic because `usdt:krw` is a multi-source snapshot.
    """

    def __init__(
        self,
        *,
        mode: Optional[str] = None,
        coalesce_ms: Optional[int] = None,
        publish_func: Optional[PublishCallable] = None,
    ) -> None:
        self.mode = mode if mode is not None else config.TETHER_TOPIC_TRIGGER_MODE
        self.coalesce_ms = (
            coalesce_ms
            if coalesce_ms is not None
            else config.TETHER_TOPIC_TRIGGER_COALESCE_MS
        )
        self._validate()
        self._publish_func = publish_func or _default_publish_tether_snapshot
        self._pending: dict[str, _PendingTopic] = {}
        self.stats = TetherTopicTriggerStats()

    def _validate(self) -> None:
        if self.mode not in config.TETHER_TOPIC_TRIGGER_ALLOWED_MODES:
            raise ValueError(
                "mode must be one of "
                f"{config.TETHER_TOPIC_TRIGGER_ALLOWED_MODES} (got {self.mode!r})"
            )
        if self.coalesce_ms < 1:
            raise ValueError(f"coalesce_ms must be >= 1 (got {self.coalesce_ms})")

    def request_trigger(self, source: str, asset: str, reason: str) -> None:
        """Request a future tether topic publish.

        Synchronous by design so writers can call it from their hot path. In
        `legacy_piggyback` mode the controller is a strict noop and starts no
        timer.
        """
        self.stats.last_mode = self.mode
        self.stats.last_source = source
        self.stats.last_asset = asset
        self.stats.last_reason = reason
        self.stats.last_topic = TETHER_TOPIC

        if self.mode == MODE_LEGACY_PIGGYBACK:
            self.stats.publish_skipped_legacy += 1
            self._fire_telemetry(
                counter="skipped_legacy",
                last_result="skipped_legacy",
                last_reason=reason,
                last_source=source,
                last_asset=asset,
            )
            return

        self.stats.trigger_count += 1
        pending = self._pending.get(TETHER_TOPIC)
        if pending is not None and not pending.task.done():
            pending.trigger_count += 1
            pending.last_source = source
            pending.last_asset = asset
            pending.last_reason = reason
            self.stats.coalesced_trigger_count += 1
            self._fire_telemetry(
                counter="coalesced",
                last_reason=reason,
                last_source=source,
                last_asset=asset,
            )
            # request counter도 함께 갱신 (호출 빈도 baseline 유지)
            self._fire_telemetry(counter="request")
            return

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # request_trigger may be exercised in sync tests or startup paths.
            # Without a loop there is no safe place to run the timer, so skip
            # without falling back to sync publish.
            logger.debug(
                "[tether_topic_trigger] no running event loop, skip trigger",
                extra={"source": source, "asset": asset, "reason": reason},
            )
            # no_loop counter는 fire-and-forget create_task가 불가능한 상황.
            # _fire_telemetry는 loop가 있어야 하므로 본 분기에서도 skip.
            self.stats.no_loop_skipped = getattr(
                self.stats, "no_loop_skipped", 0
            ) + 1
            return

        self._fire_telemetry(
            counter="request",
            last_reason=reason,
            last_source=source,
            last_asset=asset,
        )
        task = asyncio.create_task(self._flush_after_window(TETHER_TOPIC))
        self._pending[TETHER_TOPIC] = _PendingTopic(
            task=task,
            last_source=source,
            last_asset=asset,
            last_reason=reason,
        )

    def _fire_telemetry(
        self,
        *,
        counter: Optional[str] = None,
        last_result: Optional[str] = None,
        last_reason: Optional[str] = None,
        last_source: Optional[str] = None,
        last_asset: Optional[str] = None,
        last_window_ms: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """fire-and-forget telemetry — running loop 없으면 silent skip.

        request_trigger의 hot path를 막지 않기 위해 create_task로 분리.
        예외는 _record_trigger_event 내부에서 격리되므로 task 결과는 무시.

        Strong reference: `_telemetry_tasks` set에 add + done callback에서
        discard. asyncio docs 권고 — 미보유 task는 GC로 사라질 수 있음.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            _record_trigger_event(
                counter=counter,
                last_result=last_result,
                last_reason=last_reason,
                last_source=last_source,
                last_asset=last_asset,
                last_window_ms=last_window_ms,
                error=error,
            )
        )
        _telemetry_tasks.add(task)
        task.add_done_callback(_telemetry_tasks.discard)

    async def _flush_after_window(self, topic: str) -> None:
        await asyncio.sleep(self.coalesce_ms / 1000)
        pending = self._pending.get(topic)
        if pending is None:
            return

        window_ms = (time.monotonic() - pending.first_trigger_at) * 1000
        self.stats.last_window_ms = window_ms
        self.stats.last_source = pending.last_source
        self.stats.last_asset = pending.last_asset
        self.stats.last_reason = pending.last_reason

        try:
            if self.mode == MODE_DUAL_SHADOW:
                self.stats.flush_count_dual_shadow += 1
                self.stats.publish_skipped_shadow += 1
                self._fire_telemetry(
                    counter="flush_dual_shadow",
                    last_result="publish_skipped_shadow",
                    last_reason=pending.last_reason,
                    last_source=pending.last_source,
                    last_asset=pending.last_asset,
                    last_window_ms=window_ms,
                )
                self._fire_telemetry(counter="publish_skipped_shadow")
                return

            if self.mode == MODE_DIRECT_COALESCED:
                self.stats.flush_count_direct += 1
                self.stats.publish_called += 1
                self._fire_telemetry(
                    counter="flush_direct",
                    last_reason=pending.last_reason,
                    last_source=pending.last_source,
                    last_asset=pending.last_asset,
                    last_window_ms=window_ms,
                )
                self._fire_telemetry(counter="publish_called")
                if await self._publish_func():
                    self.stats.publish_success += 1
                    self._fire_telemetry(
                        counter="publish_success",
                        last_result="publish_success",
                    )
                else:
                    self._fire_telemetry(last_result="publish_zero")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[tether_topic_trigger] flush failed (isolated)")
            self._fire_telemetry(
                counter="error",
                last_result="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if self._pending.get(topic) is pending:
                self._pending.pop(topic, None)

    async def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_SEC) -> None:
        """Drain pending flush tasks, then cancel on timeout."""
        tasks = [pending.task for pending in self._pending.values()]
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[tether_topic_trigger] close timeout (%ss), cancel pending",
                timeout,
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)


async def _default_publish_tether_snapshot() -> bool:
    """Default direct publish path.

    PR1 keeps this behind `direct_coalesced` mode only. It opens a short DB
    session and delegates all guard/build/publish behavior to the existing safe
    publisher.
    """
    from app import config as app_config
    from app.database import get_db_context
    from app.tether_topic_publisher import safe_publish_tether_tab_snapshot

    with get_db_context() as db:
        return await safe_publish_tether_tab_snapshot(
            db,
            include_krx=app_config.KRX_TOPIC_INCLUDE,
        )


_default_controller: Optional[TetherTopicTriggerController] = None


def get_tether_topic_trigger_controller() -> TetherTopicTriggerController:
    """Process-wide controller singleton."""
    global _default_controller
    if _default_controller is None:
        _default_controller = TetherTopicTriggerController()
    return _default_controller


def request_tether_topic_trigger(source: str, asset: str, reason: str) -> None:
    get_tether_topic_trigger_controller().request_trigger(source, asset, reason)


async def shutdown_tether_topic_trigger() -> None:
    controller = get_tether_topic_trigger_controller()
    await controller.close()


def reset_tether_topic_trigger_for_tests(
    controller: Optional[TetherTopicTriggerController] = None,
) -> None:
    """Replace the module singleton in tests."""
    global _default_controller
    _default_controller = controller
