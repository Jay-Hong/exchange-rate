"""Tether topic trigger controller tests (Phase B.2 PR1 + PR2)."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config
from app.tether_topic_publisher import TETHER_TOPIC
from app.tether_topic_trigger import (
    MODE_DIRECT_COALESCED,
    MODE_DUAL_SHADOW,
    MODE_LEGACY_PIGGYBACK,
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
    TRIGGER_COUNTER_FIELDS,
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

    def test_reason_constant_value(self):
        """reason 상수는 'usdt_ws_redis_write_success' 정확 일치."""
        self.assertEqual(
            TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
            "usdt_ws_redis_write_success",
        )


class TestTetherTriggerTelemetry(unittest.IsolatedAsyncioTestCase):
    """Phase B.2 PR2 — Redis-backed telemetry 기록 검증.

    `topic:tether:stats` hash에 `trigger_*` prefix 12 fields 기록.
    `_record_trigger_event`을 직접 호출해 hincrby/hset 인자 정확성 검증.
    """

    async def test_record_trigger_event_writes_counter_and_last_fields(self):
        from app import tether_topic_trigger as ttt

        # redis_cache.client mock + circuit.can_attempt() True
        mock_client = MagicMock()
        mock_client.hincrby = AsyncMock(return_value=1)
        mock_client.hset = AsyncMock(return_value=1)
        mock_circuit = MagicMock()
        mock_circuit.can_attempt = AsyncMock(return_value=True)

        with patch.object(ttt.redis_cache, "client", mock_client), \
             patch.object(ttt.redis_cache, "circuit", mock_circuit):
            await ttt._record_trigger_event(
                counter="publish_success",
                last_result="publish_success",
                last_reason="usdt_ws_redis_write_success",
                last_source="upbit",
                last_asset="usdt-krw",
                last_window_ms=512.5,
            )

        # counter increment
        mock_client.hincrby.assert_awaited_once()
        hincrby_args = mock_client.hincrby.await_args.args
        self.assertEqual(hincrby_args[0], "topic:tether:stats")
        self.assertEqual(hincrby_args[1], "trigger_publish_success")
        self.assertEqual(hincrby_args[2], 1)

        # last_* fields all written with trigger_ prefix
        hset_keys = {call.args[1] for call in mock_client.hset.await_args_list}
        self.assertIn("trigger_last_result", hset_keys)
        self.assertIn("trigger_last_reason", hset_keys)
        self.assertIn("trigger_last_source", hset_keys)
        self.assertIn("trigger_last_asset", hset_keys)
        self.assertIn("trigger_last_window_ms", hset_keys)
        self.assertIn("trigger_last_at_kst", hset_keys)

    async def test_record_trigger_event_silent_skip_when_client_none(self):
        from app import tether_topic_trigger as ttt

        with patch.object(ttt.redis_cache, "client", None):
            # should not raise
            await ttt._record_trigger_event(counter="request")

    async def test_record_trigger_event_silent_skip_when_circuit_open(self):
        from app import tether_topic_trigger as ttt

        mock_client = MagicMock()
        mock_client.hincrby = AsyncMock()
        mock_client.hset = AsyncMock()
        mock_circuit = MagicMock()
        mock_circuit.can_attempt = AsyncMock(return_value=False)

        with patch.object(ttt.redis_cache, "client", mock_client), \
             patch.object(ttt.redis_cache, "circuit", mock_circuit):
            await ttt._record_trigger_event(counter="request")

        mock_client.hincrby.assert_not_awaited()
        mock_client.hset.assert_not_awaited()

    async def test_record_trigger_event_isolates_redis_exception(self):
        """Redis 예외 → 격리, broadcast/trigger 영향 X."""
        from app import tether_topic_trigger as ttt

        mock_client = MagicMock()
        mock_client.hincrby = AsyncMock(side_effect=RuntimeError("redis down"))
        mock_client.hset = AsyncMock()
        mock_circuit = MagicMock()
        mock_circuit.can_attempt = AsyncMock(return_value=True)

        with patch.object(ttt.redis_cache, "client", mock_client), \
             patch.object(ttt.redis_cache, "circuit", mock_circuit):
            # should not raise
            await ttt._record_trigger_event(counter="request")

    def test_trigger_counter_fields_cover_all_modes(self):
        """필수 counter 12개 모두 포함 — admin telemetry 일관성."""
        required = {
            "request", "skipped_legacy", "coalesced",
            "flush_dual_shadow", "flush_direct",
            "publish_called", "publish_success", "publish_skipped_shadow",
            "no_loop", "error",
        }
        self.assertTrue(required.issubset(set(TRIGGER_COUNTER_FIELDS)))
