"""FX topic publish wrapper 단위 테스트 (PR Z-2c Step 2).

검증:
    - guard 동작 (FF=false / TOPIC_DISPATCHER=false / subscriber 0)
    - 정상 path (FF=true + subscriber → builder + publish 1회)
    - payload에 topic 필드 inject (publisher 책임)
    - 3 topic 독립 처리 (1개 실패 시 다른 topic 계속)
    - per-topic subscriber_count guard (multi-topic 환경 정확성)
    - per-topic telemetry key 분리 (`topic:fx:<asset>:stats`)
    - safe wrapper 예외 격리
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, fx_topic_publisher, topic_dispatcher
from app.fx_topic_publisher import (
    FX_TOPICS,
    FX_TOPIC_ASSETS,
    _publish_fx_snapshot,
    _telemetry_key,
    safe_publish_all_fx_snapshots,
)


# ---------------------------------------------------------------------------
# Guard — _publish_fx_snapshot
# ---------------------------------------------------------------------------

class TestPublishFxSnapshotGuards(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_returns_false_when_fx_topic_disabled(self):
        db = MagicMock()
        with patch.object(config, "FX_TOPIC_ENABLED", False), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    async def test_returns_false_when_topic_dispatcher_disabled(self):
        db = MagicMock()
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    async def test_returns_false_when_no_subscribers_for_topic(self):
        db = MagicMock()
        # registry 비어 있음
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    async def test_other_topic_subscriber_does_not_trigger_this_topic(self):
        """multi-topic 정확성: fx:jpy-krw 구독자 있어도 fx:usd-krw publisher는 skip."""
        db = MagicMock()
        ws = MagicMock()
        # 다른 topic만 구독
        topic_dispatcher.registry.register(ws, ["fx:jpy-krw"])

        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        # subscribed_connection_count=1이지만 fx:usd-krw subscriber=0 → skip
        mock_build.assert_not_called()
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# Normal path — _publish_fx_snapshot
# ---------------------------------------------------------------------------

class TestPublishFxSnapshotNormal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_publishes_and_returns_true_when_send_succeeds(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload) as mock_build, \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")

        self.assertTrue(result)
        mock_build.assert_called_once_with(db, "usd-krw")
        mock_pub.assert_awaited_once()

    async def test_payload_topic_field_injected_by_publisher(self):
        """builder는 topic-agnostic, publisher가 payload['topic'] inject."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:jpy-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            await _publish_fx_snapshot(db, "jpy-krw")

        sent_payload = mock_pub.await_args.args[1]
        self.assertEqual(sent_payload["topic"], "fx:jpy-krw")

    async def test_publish_zero_returns_false(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=0)):
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# safe_publish_all_fx_snapshots — 3 topic 일괄 + 예외 격리
# ---------------------------------------------------------------------------

class TestSafePublishAllFxSnapshots(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_all_three_assets_processed(self):
        db = MagicMock()
        # 3 topic 모두 구독자 있음
        for asset, topic in FX_TOPICS.items():
            ws = MagicMock(name=f"ws_{asset}")
            topic_dispatcher.registry.register(ws, [topic])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload) as mock_build, \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            results = await safe_publish_all_fx_snapshots(db)

        self.assertEqual(set(results.keys()), set(FX_TOPIC_ASSETS))
        self.assertTrue(all(results.values()))
        # builder는 3번 호출 (asset별)
        self.assertEqual(mock_build.call_count, 3)
        self.assertEqual(mock_pub.await_count, 3)

    async def test_one_asset_failure_does_not_block_others(self):
        """1개 topic의 builder 예외가 나머지 publish를 막지 않음 (격리).

        logger.exception을 patch 캡처 — 회귀 출력에 traceback 노출 차단 +
        예외 격리 시 실제로 로그가 남는다는 contract 명시 검증.
        """
        db = MagicMock()
        # 3 topic 모두 구독자 있음
        for asset, topic in FX_TOPICS.items():
            ws = MagicMock(name=f"ws_{asset}")
            topic_dispatcher.registry.register(ws, [topic])

        def builder_side_effect(db, asset):
            if asset == "jpy-krw":
                raise RuntimeError("simulated builder failure")
            return {"type": "snapshot", "version": 1, "data": {"banks": []}}

        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          side_effect=builder_side_effect), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)), \
             patch.object(fx_topic_publisher.logger, "exception") as mock_log:
            results = await safe_publish_all_fx_snapshots(db)

        self.assertTrue(results["usd-krw"])
        self.assertFalse(results["jpy-krw"])  # 예외 격리 — False
        self.assertTrue(results["eur-krw"])
        # logger.exception이 정확히 1회 호출 (jpy-krw 격리)
        self.assertEqual(mock_log.call_count, 1)

    async def test_all_disabled_returns_all_false(self):
        db = MagicMock()
        with patch.object(config, "FX_TOPIC_ENABLED", False), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            results = await safe_publish_all_fx_snapshots(db)

        self.assertEqual(results, {"usd-krw": False, "jpy-krw": False, "eur-krw": False})
        mock_build.assert_not_called()
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# Telemetry key separation — per topic Redis key
# ---------------------------------------------------------------------------

class TestTelemetryKeySeparation(unittest.TestCase):

    def test_telemetry_key_per_asset(self):
        self.assertEqual(_telemetry_key("usd-krw"), "topic:fx:usd-krw:stats")
        self.assertEqual(_telemetry_key("jpy-krw"), "topic:fx:jpy-krw:stats")
        self.assertEqual(_telemetry_key("eur-krw"), "topic:fx:eur-krw:stats")

    def test_telemetry_key_differs_from_tether(self):
        """tether와 namespace 분리."""
        for asset in FX_TOPIC_ASSETS:
            self.assertNotEqual(_telemetry_key(asset), "topic:tether:stats")

    def test_fx_topics_mapping_derived_from_assets(self):
        """FX_TOPICS는 FX_TOPIC_ASSETS에서 파생 — 변경 시 자동 동기화."""
        self.assertEqual(
            FX_TOPICS,
            {"usd-krw": "fx:usd-krw", "jpy-krw": "fx:jpy-krw", "eur-krw": "fx:eur-krw"},
        )


# ---------------------------------------------------------------------------
# Telemetry recording — best-effort, no circuit pollution
# ---------------------------------------------------------------------------

class TestTelemetryBestEffort(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_publish_succeeds_when_redis_unavailable(self):
        """Redis client=None이면 telemetry skip — publish 정상 동작."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache, \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            mock_cache.client = None
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertTrue(result)

    async def test_publish_succeeds_when_circuit_open(self):
        """circuit.can_attempt()=False여도 publish 정상 — telemetry만 skip."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache, \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            mock_cache.client = MagicMock()
            mock_cache.circuit = MagicMock()
            mock_cache.circuit.can_attempt = AsyncMock(return_value=False)
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertTrue(result)
        # circuit.record_failure는 호출되면 안 됨 (오염 차단)
        if hasattr(mock_cache.circuit, "record_failure"):
            mock_cache.circuit.record_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
