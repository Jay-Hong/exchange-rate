"""KRX 독립 topic publisher 단위 테스트 (ADR-038 Decision 2 — krx:usd-krw-futures).

검증:
    - build_krx_topic_payload schema (type/topic/version/data.usd_krw_futures)
    - load_krx_topic_entry: Redis-first + normalize 검증 (wrong source/asset → None)
      + rate_changed_at carry (Stage E tick 5-field → 정밀 변경시각 노출)
      (Redis hit/miss + DB fallback 상세 계약은 tests/test_krx_redis_integration.py
       TestKrxTopicEntryRedisFirst가 커버 — 여기선 normalize/게이트 축)
    - publish_krx_topic_snapshot guard 순서:
      dispatcher off → G2/G3(EFFECTIVE) off → subscriber 0 → entry None → publish
    - request_krx_topic_publish: gate off 시 marshal 미호출 / gate on 시 schedule_on_loop
      호출 / schedule 예외 격리 (writer hot path 영향 0)
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, krx_topic_publisher, topic_dispatcher
from app.krx_topic_publisher import (
    KRX_ASSET,
    KRX_TOPIC,
    build_krx_topic_payload,
    load_krx_topic_entry,
    publish_krx_topic_snapshot,
    request_krx_topic_publish,
)

_ENTRY = {
    "source": "krx", "asset": KRX_ASSET,
    "rate": 1382.0, "timestamp": "2026-07-08T10:00:05+09:00",
}


class TestBuildKrxTopicPayload(unittest.TestCase):

    def test_schema(self):
        payload = build_krx_topic_payload(dict(_ENTRY))
        self.assertEqual(payload["type"], "snapshot")
        self.assertEqual(payload["topic"], KRX_TOPIC)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["data"], {"usd_krw_futures": _ENTRY})

    def test_topic_constant(self):
        """iOS 구독 문자열 계약 잠금."""
        self.assertEqual(KRX_TOPIC, "krx:usd-krw-futures")


def krx_topic_publisher_lrc():
    """load_krx_topic_entry는 함수 내부 lazy import — patch 대상은 원 모듈."""
    from app import latest_rates_cache
    return latest_rates_cache


class TestLoadKrxTopicEntry(unittest.TestCase):
    """normalize/검증 축 — Redis/DB dispatch 상세는 test_krx_redis_integration.py."""

    def _load_with_redis(self, raw):
        with patch.object(
            krx_topic_publisher_lrc(), "get_latest_krx_rate_from_sync_job",
            return_value=raw,
        ):
            return load_krx_topic_entry()

    def test_redis_hit_returns_normalized_entry(self):
        raw = {"source": "krx", "asset": KRX_ASSET,
               "rate": 1382.0, "timestamp": "2026-07-08T10:00:05+09:00"}
        entry = self._load_with_redis(raw)
        self.assertEqual(entry["source"], "krx")
        self.assertEqual(entry["asset"], KRX_ASSET)
        self.assertEqual(entry["rate"], 1382.0)

    def test_rate_changed_at_carried_from_stage_e_tick(self):
        """Stage E 5-field Redis value → rate_changed_at 정밀 시각 carry (USDT 대칭)."""
        raw = {"source": "krx", "asset": KRX_ASSET,
               "rate": 1382.0, "timestamp": "2026-07-08T10:00:05+09:00",
               "rate_changed_at": "2026-07-08T10:00:03+09:00"}
        entry = self._load_with_redis(raw)
        self.assertEqual(entry["rate_changed_at"], "2026-07-08T10:00:03+09:00")

    def test_wrong_source_rejected(self):
        raw = {"source": "upbit", "asset": KRX_ASSET,
               "rate": 1.0, "timestamp": "2026-07-08T10:00:05+09:00"}
        self.assertIsNone(self._load_with_redis(raw))

    def test_wrong_asset_rejected(self):
        raw = {"source": "krx", "asset": "usdt-krw",
               "rate": 1.0, "timestamp": "2026-07-08T10:00:05+09:00"}
        self.assertIsNone(self._load_with_redis(raw))


class TestPublishKrxTopicSnapshot(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    def _register_subscriber(self):
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [KRX_TOPIC])
        return ws

    # 1) dispatcher off → False, load/publish 미호출
    async def test_dispatcher_off_returns_false(self):
        self._register_subscriber()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher, "load_krx_topic_entry") as mock_load, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await publish_krx_topic_snapshot()
        self.assertFalse(result)
        mock_load.assert_not_called()
        mock_pub.assert_not_called()

    # 2) G2/G3(EFFECTIVE) off → False (ADR-038 — 발행 자체 중단)
    async def test_distribution_gate_off_returns_false(self):
        self._register_subscriber()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", False), \
             patch.object(krx_topic_publisher, "load_krx_topic_entry") as mock_load, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await publish_krx_topic_snapshot()
        self.assertFalse(result)
        mock_load.assert_not_called()
        mock_pub.assert_not_called()

    # 3) subscriber 0 → False, load 미호출 (builder 비용 차단)
    async def test_no_subscribers_returns_false(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher, "load_krx_topic_entry") as mock_load, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await publish_krx_topic_snapshot()
        self.assertFalse(result)
        mock_load.assert_not_called()
        mock_pub.assert_not_called()

    # 4) entry None (Redis miss + db 미제공) → False, publish 미호출
    async def test_entry_none_returns_false(self):
        self._register_subscriber()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher, "load_krx_topic_entry", return_value=None), \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await publish_krx_topic_snapshot()
        self.assertFalse(result)
        mock_pub.assert_not_called()

    # 5) 정상 path → publish_topic(KRX_TOPIC, payload) + True
    async def test_success_publishes_snapshot(self):
        self._register_subscriber()
        mock_pub = AsyncMock(return_value=1)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher, "load_krx_topic_entry",
                          return_value=dict(_ENTRY)), \
             patch.object(topic_dispatcher, "publish_topic", new=mock_pub):
            result = await publish_krx_topic_snapshot()
        self.assertTrue(result)
        mock_pub.assert_awaited_once()
        topic_arg, payload_arg = mock_pub.await_args.args
        self.assertEqual(topic_arg, KRX_TOPIC)
        self.assertEqual(payload_arg, build_krx_topic_payload(_ENTRY))

    # 6) sent=0 (전송 실패) → False
    async def test_sent_zero_returns_false(self):
        self._register_subscriber()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher, "load_krx_topic_entry",
                          return_value=dict(_ENTRY)), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=0)):
            result = await publish_krx_topic_snapshot()
        self.assertFalse(result)


class TestRequestKrxTopicPublish(unittest.TestCase):
    """sync trigger 진입점 — writer hot path 계약 (조기 skip + marshal + 예외 격리)."""

    def test_gate_off_skips_marshal(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", False), \
             patch.object(krx_topic_publisher.topic_trigger_bridge,
                          "schedule_on_loop") as mock_sched:
            request_krx_topic_publish(reason="krx_redis_write_success")
        mock_sched.assert_not_called()

    def test_dispatcher_off_skips_marshal(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher.topic_trigger_bridge,
                          "schedule_on_loop") as mock_sched:
            request_krx_topic_publish(reason="krx_redis_write_success")
        mock_sched.assert_not_called()

    def test_gates_on_schedules_publish(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher.topic_trigger_bridge,
                          "schedule_on_loop") as mock_sched:
            request_krx_topic_publish(reason="krx_redis_write_success")
        mock_sched.assert_called_once_with(krx_topic_publisher._schedule_publish)

    def test_schedule_exception_isolated(self):
        """marshal 실패해도 writer hot path에 예외 전파 0."""
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch.object(krx_topic_publisher.topic_trigger_bridge,
                          "schedule_on_loop", side_effect=RuntimeError("boom")):
            request_krx_topic_publish(reason="krx_close_finalizer")  # no raise


if __name__ == "__main__":
    unittest.main()
