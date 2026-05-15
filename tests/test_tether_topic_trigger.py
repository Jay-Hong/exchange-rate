"""Tether topic trigger controller tests (Phase B.2 PR1)."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import config
from app.tether_topic_publisher import TETHER_TOPIC
from app.tether_topic_trigger import (
    MODE_DIRECT_COALESCED,
    MODE_DUAL_SHADOW,
    MODE_LEGACY_PIGGYBACK,
    TetherTopicTriggerController,
    get_tether_topic_trigger_controller,
    reset_tether_topic_trigger_for_tests,
)


class TestTetherTopicTriggerController(unittest.IsolatedAsyncioTestCase):

    async def asyncTearDown(self):
        controller = get_tether_topic_trigger_controller()
        await controller.close(timeout=0.01)
        reset_tether_topic_trigger_for_tests(None)

    async def test_legacy_piggyback_is_strict_noop(self):
        publish = AsyncMock(return_value=True)
        controller = TetherTopicTriggerController(
            mode=MODE_LEGACY_PIGGYBACK,
            coalesce_ms=5,
            publish_func=publish,
        )

        controller.request_trigger("upbit", "usdt-krw", "redis_write_success")
        await asyncio.sleep(0.02)

        self.assertEqual(controller.pending_count, 0)
        self.assertEqual(controller.stats.trigger_count, 0)
        self.assertEqual(controller.stats.publish_skipped_legacy, 1)
        publish.assert_not_awaited()

    async def test_dual_shadow_coalesces_triggers_but_skips_publish(self):
        publish = AsyncMock(return_value=True)
        controller = TetherTopicTriggerController(
            mode=MODE_DUAL_SHADOW,
            coalesce_ms=10,
            publish_func=publish,
        )

        controller.request_trigger("upbit", "usdt-krw", "redis_write_success")
        controller.request_trigger("krx", "usd-krw-futures", "redis_write_success")
        controller.request_trigger("upbit", "usdt-krw", "redis_write_success")
        await asyncio.sleep(0.04)

        self.assertEqual(controller.pending_count, 0)
        self.assertEqual(controller.stats.trigger_count, 3)
        self.assertEqual(controller.stats.coalesced_trigger_count, 2)
        self.assertEqual(controller.stats.flush_count_dual_shadow, 1)
        self.assertEqual(controller.stats.publish_skipped_shadow, 1)
        self.assertEqual(controller.stats.last_topic, TETHER_TOPIC)
        publish.assert_not_awaited()

    async def test_direct_coalesced_publishes_once_for_many_triggers(self):
        publish = AsyncMock(return_value=True)
        controller = TetherTopicTriggerController(
            mode=MODE_DIRECT_COALESCED,
            coalesce_ms=10,
            publish_func=publish,
        )

        controller.request_trigger("upbit", "usdt-krw", "redis_write_success")
        controller.request_trigger("upbit", "usdt-krw", "redis_write_success")
        controller.request_trigger("krx", "usd-krw-futures", "redis_write_success")
        await asyncio.sleep(0.04)

        self.assertEqual(controller.pending_count, 0)
        self.assertEqual(controller.stats.trigger_count, 3)
        self.assertEqual(controller.stats.coalesced_trigger_count, 2)
        self.assertEqual(controller.stats.flush_count_direct, 1)
        self.assertEqual(controller.stats.publish_called, 1)
        self.assertEqual(controller.stats.publish_success, 1)
        publish.assert_awaited_once()

    async def test_close_drains_pending_flush(self):
        publish = AsyncMock(return_value=True)
        controller = TetherTopicTriggerController(
            mode=MODE_DIRECT_COALESCED,
            coalesce_ms=10,
            publish_func=publish,
        )

        controller.request_trigger("upbit", "usdt-krw", "redis_write_success")
        await controller.close(timeout=0.1)

        self.assertEqual(controller.pending_count, 0)
        self.assertEqual(controller.stats.flush_count_direct, 1)
        publish.assert_awaited_once()

    async def test_close_cancels_pending_flush_after_timeout(self):
        publish = AsyncMock(return_value=True)
        controller = TetherTopicTriggerController(
            mode=MODE_DIRECT_COALESCED,
            coalesce_ms=500,
            publish_func=publish,
        )

        controller.request_trigger("upbit", "usdt-krw", "redis_write_success")
        await controller.close(timeout=0.01)

        self.assertEqual(controller.pending_count, 0)
        publish.assert_not_awaited()

    def test_invalid_mode_and_coalesce_are_rejected(self):
        with self.assertRaises(ValueError):
            TetherTopicTriggerController(mode="bad_mode", coalesce_ms=500)
        with self.assertRaises(ValueError):
            TetherTopicTriggerController(mode=MODE_DUAL_SHADOW, coalesce_ms=0)

    def test_controller_uses_config_values_by_default(self):
        with patch.object(config, "TETHER_TOPIC_TRIGGER_MODE", MODE_DUAL_SHADOW), \
             patch.object(config, "TETHER_TOPIC_TRIGGER_COALESCE_MS", 25):
            controller = TetherTopicTriggerController()

        self.assertEqual(controller.mode, MODE_DUAL_SHADOW)
        self.assertEqual(controller.coalesce_ms, 25)
