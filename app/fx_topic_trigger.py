"""FX topic publish trigger controller (§6.6.2 Bank/Investing β PR C — C1).

`tether_topic_trigger.TetherTopicTriggerController` 복제 + multi-topic 적응.
tether는 단일 `usdt:krw` topic이지만 fx는 3개(`fx:usd-krw`/`fx:jpy-krw`/
`fx:eur-krw`)이므로 `_pending`을 topic별로 coalesce하고 flush도 **per-topic**
(`safe_publish_fx_snapshot(asset)`)으로 한다 — fx:usd-krw 변경이 jpy/eur를
재발행하지 않게.

mode (config.BANK_INVESTING_TOPIC_TRIGGER_MODE):
    - legacy_piggyback: noop. 기존 main.py legacy hook이 유일 publisher.
    - dual_shadow: coalesce timer는 돌되 publish call skip (발화 빈도 telemetry용).
    - direct_coalesced: flush window 뒤 safe_publish_fx_snapshot(asset) 호출.

호출 경로: crud worker thread → topic_trigger_bridge.schedule_on_loop →
(main loop) request_trigger. worker thread 직접 호출이 아니므로 request_trigger
자체는 running loop을 전제할 수 있으나, sync 테스트/startup 방어로 no_loop 분기
유지 (tether 패턴).

NOTE (Increment 1 scaffolding): 본 모듈은 호출자가 아직 없다 (crud emission +
lifespan register는 Increment 2). telemetry는 in-process stats만 — Redis
`topic:fx:<asset>:stats` `trigger_*` counter + cross-route `tether_route_shadow`는
Increment 2 (canary 계측)에서 추가.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app import config
# FX_TOPICS(asset→topic 매핑)는 fx_topic_publisher를 단일 진실 소스로 재사용
# (`f"fx:{asset}"` format 중복 제거, Codex review). publisher가 config/dispatcher/
# cache를 끌어오나 trigger와 cycle 없음 (publisher는 fx_topic_trigger를 import 안 함).
from app.fx_topic_publisher import FX_TOPICS

logger = logging.getLogger("exchange_rate.fx_topic_trigger")

MODE_LEGACY_PIGGYBACK = "legacy_piggyback"
MODE_DUAL_SHADOW = "dual_shadow"
MODE_DIRECT_COALESCED = "direct_coalesced"

# Reason 상수 — 호출자가 string literal 대신 import해 사용 (telemetry 일관성).
FX_TRIGGER_REASON_BANK_CHANGE = "bank_change"
FX_TRIGGER_REASON_INVESTING_CHANGE = "investing_change"

# publish_func: per-topic asset 1개를 받아 publish (tether는 인자 없는 snapshot,
# fx는 multi-topic이라 asset 파라미터).
PublishCallable = Callable[[str], Awaitable[bool]]


@dataclass
class FxTopicTriggerStats:
    """In-process telemetry snapshot (Increment 1). Redis counter는 Increment 2."""

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
        ignore (fx topic이 아닌 통화쌍 방어). legacy_piggyback은 strict noop.
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
            return

        self.stats.trigger_count += 1
        pending = self._pending.get(topic)
        if pending is not None and not pending.task.done():
            pending.trigger_count += 1
            pending.last_source = source
            pending.last_reason = reason
            self.stats.coalesced_trigger_count += 1
            return

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # running loop 없음 (sync 테스트/startup) — timer 돌릴 곳 없어 skip.
            # sync publish fallback 안 함 (tether 정책 일관).
            self.stats.no_loop_skipped += 1
            return

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
                return

            if self.mode == MODE_DIRECT_COALESCED:
                self.stats.flush_count_direct += 1
                self.stats.publish_called += 1
                if await self._publish_func(asset):
                    self.stats.publish_success += 1
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[fx_topic_trigger] flush failed (isolated)", extra={"topic": topic})
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
