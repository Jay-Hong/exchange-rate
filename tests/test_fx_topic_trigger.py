"""FxTopicTriggerController tests (§6.6.2 C1 — source-routed fx topic trigger).

tether_topic_trigger 테스트 구조 mirror + multi-topic 검증 (per-topic coalesce /
per-topic flush isolation). Increment 1 scaffolding — control flow only.
"""
from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app import config
from app.fx_topic_trigger import (
    FX_TOPICS,
    FX_TRIGGER_REASON_BANK_CHANGE,
    FX_TRIGGER_REASON_INVESTING_CHANGE,
    MODE_DIRECT_COALESCED,
    MODE_DUAL_SHADOW,
    MODE_LEGACY_PIGGYBACK,
    FxTopicTriggerController,
    get_fx_topic_trigger_controller,
    reset_fx_topic_trigger_for_tests,
)
from app.fx_topic_publisher import safe_publish_fx_snapshot


class TestFxTopicTriggerController(unittest.IsolatedAsyncioTestCase):

    async def asyncTearDown(self):
        controller = get_fx_topic_trigger_controller()
        await controller.close(timeout=0.01)
        reset_fx_topic_trigger_for_tests(None)

    async def test_legacy_piggyback_is_strict_noop(self):
        publish = AsyncMock(return_value=True)
        c = FxTopicTriggerController(
            mode=MODE_LEGACY_PIGGYBACK, coalesce_ms=5, publish_func=publish
        )
        c.request_trigger("kb", "usd-krw", FX_TRIGGER_REASON_BANK_CHANGE)
        await asyncio.sleep(0.02)
        self.assertEqual(c.pending_count, 0)
        self.assertEqual(c.stats.trigger_count, 0)
        self.assertEqual(c.stats.publish_skipped_legacy, 1)
        publish.assert_not_awaited()

    async def test_dual_shadow_coalesces_but_skips_publish(self):
        publish = AsyncMock(return_value=True)
        c = FxTopicTriggerController(
            mode=MODE_DUAL_SHADOW, coalesce_ms=20, publish_func=publish
        )
        for _ in range(3):
            c.request_trigger("kb", "usd-krw", FX_TRIGGER_REASON_BANK_CHANGE)
        await asyncio.sleep(0.05)
        self.assertEqual(c.stats.flush_count_dual_shadow, 1)
        self.assertEqual(c.stats.publish_skipped_shadow, 1)
        self.assertGreaterEqual(c.stats.coalesced_trigger_count, 2)
        publish.assert_not_awaited()

    async def test_direct_coalesced_publishes_once_per_topic(self):
        publish = AsyncMock(return_value=True)
        c = FxTopicTriggerController(
            mode=MODE_DIRECT_COALESCED, coalesce_ms=20, publish_func=publish
        )
        for _ in range(3):
            c.request_trigger("kb", "usd-krw", FX_TRIGGER_REASON_BANK_CHANGE)
        await asyncio.sleep(0.05)
        publish.assert_awaited_once_with("usd-krw")
        self.assertEqual(c.stats.publish_success, 1)

    async def test_per_topic_isolation(self):
        """usd-krw / jpy-krw 변경은 각자 topic만 발행 (cross 재발행 X)."""
        publish = AsyncMock(return_value=True)
        c = FxTopicTriggerController(
            mode=MODE_DIRECT_COALESCED, coalesce_ms=20, publish_func=publish
        )
        c.request_trigger("kb", "usd-krw", FX_TRIGGER_REASON_BANK_CHANGE)
        c.request_trigger("investing", "jpy-krw", FX_TRIGGER_REASON_INVESTING_CHANGE)
        await asyncio.sleep(0.05)
        assets = sorted(call.args[0] for call in publish.await_args_list)
        self.assertEqual(assets, ["jpy-krw", "usd-krw"])

    async def test_unknown_asset_ignored(self):
        publish = AsyncMock(return_value=True)
        c = FxTopicTriggerController(
            mode=MODE_DIRECT_COALESCED, coalesce_ms=5, publish_func=publish
        )
        c.request_trigger("upbit", "usdt-krw", "x")  # fx asset 아님
        await asyncio.sleep(0.02)
        self.assertEqual(c.pending_count, 0)
        publish.assert_not_awaited()

    async def test_close_drains_pending_flush(self):
        published = []

        async def slow_publish(asset):
            published.append(asset)
            return True

        c = FxTopicTriggerController(
            mode=MODE_DIRECT_COALESCED, coalesce_ms=10, publish_func=slow_publish
        )
        c.request_trigger("kb", "usd-krw", FX_TRIGGER_REASON_BANK_CHANGE)
        await c.close(timeout=1.0)
        self.assertEqual(c.pending_count, 0)
        self.assertEqual(published, ["usd-krw"])

    async def test_close_cancels_pending_after_timeout(self):
        """느린 publish가 pending인 상태에서 close(timeout) → cancel + _pending 비움."""
        started = asyncio.Event()

        async def hang_publish(asset):
            started.set()
            await asyncio.sleep(10)  # close timeout보다 김
            return True

        c = FxTopicTriggerController(
            mode=MODE_DIRECT_COALESCED, coalesce_ms=1, publish_func=hang_publish
        )
        c.request_trigger("kb", "usd-krw", FX_TRIGGER_REASON_BANK_CHANGE)
        await asyncio.wait_for(started.wait(), timeout=1.0)  # flush가 publish 진입
        task = next(iter(c._pending.values())).task  # close 전 task 보관
        await c.close(timeout=0.02)  # drain 실패 → cancel
        self.assertEqual(c.pending_count, 0)
        # finally: _pending.clear()가 무조건 비우므로 pending_count로는 cancel을
        # 검증 못 함 → task.cancelled()로 실제 취소 직접 단언 (Codex review).
        self.assertTrue(task.cancelled())

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            FxTopicTriggerController(mode="bogus", coalesce_ms=5)

    def test_invalid_coalesce_rejected(self):
        with self.assertRaises(ValueError):
            FxTopicTriggerController(mode=MODE_DIRECT_COALESCED, coalesce_ms=0)

    def test_defaults_from_config(self):
        c = FxTopicTriggerController()
        self.assertEqual(c.mode, config.BANK_INVESTING_TOPIC_TRIGGER_MODE)
        self.assertEqual(
            c.coalesce_ms, config.BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS
        )

    def test_fx_topics_mapping(self):
        self.assertEqual(
            FX_TOPICS,
            {
                "usd-krw": "fx:usd-krw",
                "jpy-krw": "fx:jpy-krw",
                "eur-krw": "fx:eur-krw",
            },
        )


class TestSafePublishFxSnapshotContract(unittest.IsolatedAsyncioTestCase):
    """flush 전용 entry — own-session 위임 / 예외 격리 / hook_entered 계약 잠금."""

    async def test_invalid_asset_returns_false_without_db(self):
        with patch("app.database.get_db_context") as gdc:
            result = await safe_publish_fx_snapshot("usdt-krw")  # fx asset 아님
            self.assertFalse(result)
            gdc.assert_not_called()  # invalid면 session 안 엶

    async def test_delegates_to_publish_with_own_session(self):
        fake_db = MagicMock()
        events = []  # session enter/exit 추적 (context 종료 계약 잠금)

        @contextmanager
        def fake_ctx():
            events.append("enter")
            try:
                yield fake_db
            finally:
                events.append("exit")

        with patch("app.database.get_db_context", fake_ctx), patch(
            "app.fx_topic_publisher._publish_fx_snapshot",
            new=AsyncMock(return_value=True),
        ) as pub, patch(
            "app.fx_topic_publisher._record_topic_event", new=AsyncMock()
        ) as rec:
            result = await safe_publish_fx_snapshot("usd-krw")
            self.assertTrue(result)
            pub.assert_awaited_once_with(fake_db, "usd-krw")  # 자체 session 위임
            self.assertEqual(events, ["enter", "exit"])  # session 열고 닫음 (exit 계약)
            hook_calls = [
                c for c in rec.await_args_list if c.kwargs.get("result") == "hook_entered"
            ]
            self.assertEqual(len(hook_calls), 1)  # hook_entered 정확히 1회

    async def test_exception_returns_false_and_records_error(self):
        @contextmanager
        def fake_ctx():
            yield MagicMock()

        with patch("app.database.get_db_context", fake_ctx), patch(
            "app.fx_topic_publisher._publish_fx_snapshot",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ), patch(
            "app.fx_topic_publisher._record_topic_event", new=AsyncMock()
        ) as rec:
            result = await safe_publish_fx_snapshot("usd-krw")  # raise 전파 X
            self.assertFalse(result)
            error_calls = [
                c for c in rec.await_args_list if c.kwargs.get("result") == "error"
            ]
            self.assertEqual(len(error_calls), 1)


if __name__ == "__main__":
    unittest.main()
