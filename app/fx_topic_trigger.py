"""FX topic publish trigger controller (§6.6.2 Bank/Investing β PR C — C1).

`tether_topic_trigger.TetherTopicTriggerController` 복제 + multi-topic 적응.
tether는 단일 `usdt:krw` topic이지만 fx는 3개(`fx:usd-krw`/`fx:jpy-krw`/
`fx:eur-krw`)이므로 `_pending`을 topic별로 coalesce하고 flush도 **per-topic**
(`safe_publish_fx_snapshot(asset)`)으로 한다 — fx:usd-krw 변경이 jpy/eur를
재발행하지 않게.

mode (config.BANK_INVESTING_TOPIC_TRIGGER_MODE):
    - legacy_piggyback: publish/coalesce noop (기존 main.py legacy hook이 유일
      publisher). 단 controller 직접 호출 시 `skipped_legacy` telemetry는 발화 —
      운영 land에선 crud `_emit_topic_triggers`가 legacy일 때 controller 자체를
      미호출하므로 telemetry도 안 남 (behavior-change-0).
    - dual_shadow: coalesce timer는 돌되 publish call skip (발화 빈도 telemetry용).
    - direct_coalesced: flush window 뒤 safe_publish_fx_snapshot(asset) 호출.

호출 경로: crud worker thread → topic_trigger_bridge.schedule_on_loop →
(main loop) request_trigger. worker thread 직접 호출이 아니므로 request_trigger
자체는 running loop을 전제할 수 있으나, sync 테스트/startup 방어로 no_loop 분기
유지 (tether 패턴).

telemetry: Redis `topic:fx:<asset>:stats`에 `trigger_*` prefix counter +
cross-route `tether_route_shadow` (fire-and-forget, circuit 오염 차단). in-process
`FxTopicTriggerStats`는 단위 테스트/즉시 진단용으로 병행 유지.
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
# FX_TOPICS(asset→topic 매핑)는 fx_topic_publisher를 단일 진실 소스로 재사용
# (`f"fx:{asset}"` format 중복 제거, Codex review). publisher가 config/dispatcher/
# cache를 끌어오나 trigger와 cycle 없음 (publisher는 fx_topic_trigger를 import 안 함).
from app.fx_topic_publisher import FX_TOPICS

logger = logging.getLogger("exchange_rate.fx_topic_trigger")
_KST = pytz_timezone("Asia/Seoul")

MODE_LEGACY_PIGGYBACK = "legacy_piggyback"
MODE_DUAL_SHADOW = "dual_shadow"
MODE_DIRECT_COALESCED = "direct_coalesced"

# Reason 상수 — 호출자가 string literal 대신 import해 사용 (telemetry 일관성).
FX_TRIGGER_REASON_BANK_CHANGE = "bank_change"
FX_TRIGGER_REASON_INVESTING_CHANGE = "investing_change"

# publish_func: per-topic asset 1개를 받아 publish (tether는 인자 없는 snapshot,
# fx는 multi-topic이라 asset 파라미터).
PublishCallable = Callable[[str], Awaitable[bool]]

# ── Redis-backed telemetry (Increment 3) — tether _record_trigger_event mirror ──
# fx_topic_publisher와 동일 hash(topic:fx:<asset>:stats)에 trigger_ prefix로 기록
# (publish 측 counter[prefix 없음]와 분리). circuit_breaker.record_failure() 호출 X
# — telemetry 실패가 broadcast/trigger 정상 경로를 오염시키지 않게 (tether 패턴).
_TRIGGER_PREFIX = "trigger_"
_LAST_ERROR_MAX_LEN = 500
TRIGGER_COUNTER_FIELDS = (
    "request",
    "skipped_legacy",
    "coalesced",
    "no_loop",
    "flush_dual_shadow",
    "flush_direct",
    "publish_called",
    "publish_success",
    "publish_skipped_shadow",
    "error",
    "tether_route_shadow",
)
# fire-and-forget task strong ref (asyncio docs — 미보유 task는 GC로 사라질 수 있음).
_telemetry_tasks: "set[asyncio.Task]" = set()


def _telemetry_key(asset: str) -> str:
    """asset별 Redis hash — fx_topic_publisher._telemetry_key와 동일 (trigger_ prefix로 분리)."""
    return f"topic:fx:{asset}:stats"


async def _record_trigger_event(
    asset: str,
    *,
    counter: Optional[str] = None,
    last_result: Optional[str] = None,
    last_reason: Optional[str] = None,
    last_source: Optional[str] = None,
    last_window_ms: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """best-effort Redis telemetry — circuit_breaker 오염 차단 (tether mirror).

    redis_cache.client raw 직접 사용 + circuit.can_attempt() 체크만 (record_failure
    호출 X). 실패 시 logger.debug + 조용히 skip.
    """
    client = redis_cache.client
    if client is None:
        return
    try:
        if not await redis_cache.circuit.can_attempt():
            return
    except Exception:
        return

    key = _telemetry_key(asset)
    now_iso = datetime.now(_KST).isoformat()
    try:
        if counter is not None and counter in TRIGGER_COUNTER_FIELDS:
            await client.hincrby(key, f"{_TRIGGER_PREFIX}{counter}", 1)
        if last_result is not None:
            await client.hset(key, f"{_TRIGGER_PREFIX}last_result", last_result)
        if last_reason is not None:
            await client.hset(key, f"{_TRIGGER_PREFIX}last_reason", last_reason)
        if last_source is not None:
            await client.hset(key, f"{_TRIGGER_PREFIX}last_source", last_source)
        if last_window_ms is not None:
            await client.hset(
                key, f"{_TRIGGER_PREFIX}last_window_ms", f"{last_window_ms:.2f}"
            )
        if error is not None:
            await client.hset(
                key, f"{_TRIGGER_PREFIX}last_error", error[:_LAST_ERROR_MAX_LEN]
            )
        await client.hset(key, f"{_TRIGGER_PREFIX}last_at_kst", now_iso)
    except Exception:
        logger.debug("fx trigger telemetry 기록 실패 (격리)", exc_info=True)


def _fire_telemetry(asset: str, **kwargs) -> None:
    """fire-and-forget telemetry — running loop 없으면 silent skip (hot path 비차단)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_record_trigger_event(asset, **kwargs))
    _telemetry_tasks.add(task)
    task.add_done_callback(_telemetry_tasks.discard)


def record_tether_route_shadow(asset: str) -> None:
    """dual_shadow에서 bank/investing usd-krw → usdt:krw cross-route 예상 발화량 기록.

    실제 tether publish는 안 하고(live tether 발행 방지) counter만 — direct 전환 시
    usdt:krw에 추가될 발화량 사전 측정. **request-count proxy**: 실제 publish 증가분은
    tether가 USDT/KRX와 live coalesce라 더 작음 (§6.6.2). topic:fx:<asset>:stats 기록.
    """
    _fire_telemetry(asset, counter="tether_route_shadow")


@dataclass
class FxTopicTriggerStats:
    """In-process telemetry snapshot (단위 테스트/즉시 진단용).

    운영 관측은 Redis `topic:fx:<asset>:stats` `trigger_*` counter
    (`_record_trigger_event`) — 재시작 보존 + admin 조회. 둘 병행.
    """

    trigger_count: int = 0
    coalesced_trigger_count: int = 0
    flush_count_dual_shadow: int = 0
    flush_count_direct: int = 0
    publish_called: int = 0
    publish_success: int = 0
    publish_skipped_shadow: int = 0
    publish_skipped_legacy: int = 0
    no_loop_skipped: int = 0
    last_mode: Optional[str] = None
    last_topic: Optional[str] = None
    last_asset: Optional[str] = None
    last_source: Optional[str] = None
    last_reason: Optional[str] = None
    last_window_ms: Optional[float] = None
    last_error: Optional[str] = None


@dataclass
class _PendingTopic:
    task: asyncio.Task
    asset: str
    trigger_count: int = 1
    first_trigger_at: float = field(default_factory=time.monotonic)
    last_source: str = ""
    last_reason: str = ""


async def _default_publish_fx_snapshot(asset: str) -> bool:
    """Default per-topic publish — fx_topic_publisher 위임 (lazy import).

    safe_publish_fx_snapshot이 자체 DB session을 연다 (flush는 caller db 없음).
    """
    from app.fx_topic_publisher import safe_publish_fx_snapshot

    return await safe_publish_fx_snapshot(asset)


class FxTopicTriggerController:
    """Coalesces FX topic publish triggers, per-topic (3 fx:* topics)."""

    def __init__(
        self,
        *,
        mode: Optional[str] = None,
        coalesce_ms: Optional[int] = None,
        publish_func: Optional[PublishCallable] = None,
    ) -> None:
        self.mode = (
            mode if mode is not None else config.BANK_INVESTING_TOPIC_TRIGGER_MODE
        )
        self.coalesce_ms = (
            coalesce_ms
            if coalesce_ms is not None
            else config.BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS
        )
        self._validate()
        self._publish_func = publish_func or _default_publish_fx_snapshot
        self._pending: dict[str, _PendingTopic] = {}
        self.stats = FxTopicTriggerStats()

    def _validate(self) -> None:
        if self.mode not in config.BANK_INVESTING_TOPIC_TRIGGER_ALLOWED_MODES:
            raise ValueError(
                "mode must be one of "
                f"{config.BANK_INVESTING_TOPIC_TRIGGER_ALLOWED_MODES} "
                f"(got {self.mode!r})"
            )
        if self.coalesce_ms < 1:
            raise ValueError(f"coalesce_ms must be >= 1 (got {self.coalesce_ms})")

    def request_trigger(self, source: str, asset: str, reason: str) -> None:
        """Request a future fx:<asset> topic publish (per-topic coalesce).

        Synchronous — bridge가 main loop 위에서 호출. unknown asset은 silent
        ignore (fx topic이 아닌 통화쌍 방어). legacy_piggyback은 publish/coalesce
        noop이나 `skipped_legacy` telemetry는 발화 (tether mirror — 직접 호출 시
        관측용). 단 운영 land에선 CRUD `_emit_topic_triggers`가 legacy일 때 controller
        자체를 호출하지 않으므로 behavior-change-0 (telemetry도 안 남).
        """
        if asset not in FX_TOPICS:
            # fx topic이 아닌 asset (방어) — 호출자(emission)가 이미 거르지만 이중.
            return

        topic = FX_TOPICS[asset]
        self.stats.last_mode = self.mode
        self.stats.last_source = source
        self.stats.last_asset = asset
        self.stats.last_reason = reason
        self.stats.last_topic = topic

        if self.mode == MODE_LEGACY_PIGGYBACK:
            self.stats.publish_skipped_legacy += 1
            _fire_telemetry(
                asset, counter="skipped_legacy", last_result="skipped_legacy",
                last_reason=reason, last_source=source,
            )
            return

        self.stats.trigger_count += 1
        pending = self._pending.get(topic)
        if pending is not None and not pending.task.done():
            pending.trigger_count += 1
            pending.last_source = source
            pending.last_reason = reason
            self.stats.coalesced_trigger_count += 1
            _fire_telemetry(
                asset, counter="coalesced", last_reason=reason, last_source=source,
            )
            _fire_telemetry(asset, counter="request")  # 호출 빈도 baseline 유지
            return

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # running loop 없음 (sync 테스트/startup) — timer 돌릴 곳 없어 skip.
            # sync publish fallback 안 함 (tether 정책 일관). loop 없어 telemetry도 skip.
            self.stats.no_loop_skipped += 1
            return

        _fire_telemetry(asset, counter="request", last_reason=reason, last_source=source)
        task = asyncio.create_task(self._flush_after_window(topic, asset))
        self._pending[topic] = _PendingTopic(
            task=task,
            asset=asset,
            last_source=source,
            last_reason=reason,
        )

    async def _flush_after_window(self, topic: str, asset: str) -> None:
        await asyncio.sleep(self.coalesce_ms / 1000)
        pending = self._pending.get(topic)
        if pending is None:
            return

        window_ms = (time.monotonic() - pending.first_trigger_at) * 1000
        self.stats.last_window_ms = window_ms
        self.stats.last_source = pending.last_source
        self.stats.last_reason = pending.last_reason
        self.stats.last_topic = topic
        self.stats.last_asset = asset

        try:
            if self.mode == MODE_DUAL_SHADOW:
                self.stats.flush_count_dual_shadow += 1
                self.stats.publish_skipped_shadow += 1
                _fire_telemetry(
                    asset, counter="flush_dual_shadow",
                    last_result="publish_skipped_shadow",
                    last_reason=pending.last_reason, last_source=pending.last_source,
                    last_window_ms=window_ms,
                )
                _fire_telemetry(asset, counter="publish_skipped_shadow")
                return

            if self.mode == MODE_DIRECT_COALESCED:
                self.stats.flush_count_direct += 1
                self.stats.publish_called += 1
                _fire_telemetry(
                    asset, counter="flush_direct", last_reason=pending.last_reason,
                    last_source=pending.last_source, last_window_ms=window_ms,
                )
                _fire_telemetry(asset, counter="publish_called")
                if await self._publish_func(asset):
                    self.stats.publish_success += 1
                    _fire_telemetry(
                        asset, counter="publish_success", last_result="publish_success",
                    )
                else:
                    # publish zero (subscriber 0 등) — last_result 갱신 (canary 오독
                    # 방지, tether 대칭). count는 publish_called - publish_success로 도출.
                    _fire_telemetry(asset, last_result="publish_zero")
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[fx_topic_trigger] flush failed (isolated)", extra={"topic": topic})
            _fire_telemetry(
                asset, counter="error", last_result="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if self._pending.get(topic) is pending:
                self._pending.pop(topic, None)

    async def close(self, timeout: float = 1.0) -> None:
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
            logger.warning("[fx_topic_trigger] close timeout (%ss), cancel pending", timeout)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)


_default_controller: Optional[FxTopicTriggerController] = None


def get_fx_topic_trigger_controller() -> FxTopicTriggerController:
    """Process-wide controller singleton."""
    global _default_controller
    if _default_controller is None:
        _default_controller = FxTopicTriggerController()
    return _default_controller


def request_fx_topic_trigger(source: str, asset: str, reason: str) -> None:
    get_fx_topic_trigger_controller().request_trigger(source, asset, reason)


async def shutdown_fx_topic_trigger() -> None:
    controller = get_fx_topic_trigger_controller()
    await controller.close()


def reset_fx_topic_trigger_for_tests(
    controller: Optional[FxTopicTriggerController] = None,
) -> None:
    """Replace the module singleton in tests."""
    global _default_controller
    _default_controller = controller
