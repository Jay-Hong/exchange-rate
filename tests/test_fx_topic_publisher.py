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


# ---------------------------------------------------------------------------
# get_fx_topic_telemetry — C1 trigger_* 필드 surface (Item 1, admin observability)
# ---------------------------------------------------------------------------

class TestGetFxTopicTelemetryTriggerFields(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    def test_trigger_counter_fields_match_redis_persisted_set(self):
        """drift guard: publisher surface == fx_topic_trigger의 Redis-영속 counter
        (= TRIGGER_COUNTER_FIELDS − process-local). no_loop은 loop 부재라 Redis 미기록 → 제외."""
        from app import fx_topic_trigger
        self.assertEqual(
            set(fx_topic_publisher._TRIGGER_COUNTER_FIELDS),
            set(fx_topic_trigger.TRIGGER_COUNTER_FIELDS)
            - fx_topic_publisher._TRIGGER_PROCESS_LOCAL_FIELDS,
        )
        self.assertNotIn("no_loop", fx_topic_publisher._TRIGGER_COUNTER_FIELDS)

    def _mock_cache(self, mock_cache, raw):
        mock_cache.client = MagicMock()
        mock_cache.circuit = MagicMock()
        mock_cache.circuit.can_attempt = AsyncMock(return_value=True)
        mock_cache.client.hgetall = AsyncMock(return_value=raw)

    async def test_trigger_fields_default_zero_none_when_absent(self):
        """hash에 trigger_ 필드 없으면 counter=0 / last_=None 계약."""
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            self._mock_cache(mock_cache, {})  # 빈 hash
            out = await fx_topic_publisher.get_fx_topic_telemetry()
        snap = out["fx:usd-krw"]
        for c in fx_topic_publisher._TRIGGER_COUNTER_FIELDS:
            self.assertEqual(snap[f"trigger_{c}"], 0)
        for lf in fx_topic_publisher._TRIGGER_LAST_FIELDS:
            self.assertIsNone(snap[f"trigger_{lf}"])
        # no_loop은 Redis 미기록(process-local) → surface 안 함 (항상 0 오해 방지)
        self.assertNotIn("trigger_no_loop", snap)

    async def test_trigger_fields_surfaced_and_separated_from_publisher(self):
        """hash trigger_ 값 반영 + publisher 동명 필드(error/publish_called)와 충돌 분리."""
        raw = {
            "trigger_request": "42",
            "trigger_flush_direct": "7",
            "trigger_publish_success": "3",
            "trigger_error": "0",
            "trigger_tether_route_shadow": "12",
            "trigger_last_result": "publish_success",
            "trigger_last_reason": "bank_change",
            "publish_called": "100",  # publisher 측
            "error": "5",             # publisher 측
        }
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            self._mock_cache(mock_cache, raw)
            out = await fx_topic_publisher.get_fx_topic_telemetry()
        snap = out["fx:usd-krw"]
        self.assertEqual(snap["trigger_request"], 42)
        self.assertEqual(snap["trigger_flush_direct"], 7)
        self.assertEqual(snap["trigger_publish_success"], 3)
        self.assertEqual(snap["trigger_tether_route_shadow"], 12)
        self.assertEqual(snap["trigger_last_result"], "publish_success")
        self.assertEqual(snap["trigger_last_reason"], "bank_change")
        # 충돌 분리 — publisher 측과 trigger 측이 별개 키
        self.assertEqual(snap["error"], 5)
        self.assertEqual(snap["trigger_error"], 0)
        self.assertEqual(snap["publish_called"], 100)
        self.assertEqual(snap["trigger_publish_called"], 0)  # hash에 없음 → 0

    async def test_existing_publisher_fields_preserved(self):
        """회귀: 기존 publisher-side/메타 필드 그대로 노출 (호출자 영향 0)."""
        raw = {"publish_sent_total": "9", "hook_called": "11", "last_result": "sent"}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            self._mock_cache(mock_cache, raw)
            out = await fx_topic_publisher.get_fx_topic_telemetry()
        snap = out["fx:usd-krw"]
        for f in ("enabled", "fx_topic_enabled", "topic_dispatcher_enabled",
                  "topic", "asset", "subscriber_count",
                  "hook_called", "publish_sent_total", "publish_zero", "error",
                  "last_result", "last_at_kst", "last_error"):
            self.assertIn(f, snap)
        self.assertEqual(snap["publish_sent_total"], 9)
        self.assertEqual(snap["hook_called"], 11)
        self.assertEqual(snap["last_result"], "sent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
