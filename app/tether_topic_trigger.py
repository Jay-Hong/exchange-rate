"""Tether topic publish trigger controller (Phase B.2 PR1).

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
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app import config
from app.tether_topic_publisher import TETHER_TOPIC

logger = logging.getLogger("exchange_rate.tether_topic_trigger")

MODE_LEGACY_PIGGYBACK = "legacy_piggyback"
MODE_DUAL_SHADOW = "dual_shadow"
MODE_DIRECT_COALESCED = "direct_coalesced"

DEFAULT_CLOSE_TIMEOUT_SEC = 1.0

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
            return

        self.stats.trigger_count += 1
        pending = self._pending.get(TETHER_TOPIC)
        if pending is not None and not pending.task.done():
            pending.trigger_count += 1
            pending.last_source = source
            pending.last_asset = asset
            pending.last_reason = reason
            self.stats.coalesced_trigger_count += 1
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
            return

        task = asyncio.create_task(self._flush_after_window(TETHER_TOPIC))
        self._pending[TETHER_TOPIC] = _PendingTopic(
            task=task,
            last_source=source,
            last_asset=asset,
            last_reason=reason,
        )

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
                return

            if self.mode == MODE_DIRECT_COALESCED:
                self.stats.flush_count_direct += 1
                self.stats.publish_called += 1
                if await self._publish_func():
                    self.stats.publish_success += 1
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[tether_topic_trigger] flush failed (isolated)")
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
