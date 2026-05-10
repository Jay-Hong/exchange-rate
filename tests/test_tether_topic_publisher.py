"""테더 탭 topic publish wrapper 단위 테스트 (PR Z-2b Stage 3 Level 1).

검증:
    - guard 동작 (FF=false / subscriber 0 → builder/publish 호출 0회)
    - 정상 path (FF=true + subscriber → builder + publish 1회)
    - include_krx=False 고정 (호출 인자 검증)
    - publish_topic sent=0 시 wrapper False 반환
    - wrapper가 builder 반환 payload를 변형하지 않음 (Codex 권고: 무변형 계약)
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, tether_topic_publisher, topic_dispatcher
from app.tether_topic_publisher import TETHER_TOPIC, publish_tether_tab_snapshot


class TestPublishTetherTabSnapshot(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # 모듈 싱글톤 registry를 격리 인스턴스로 swap
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    # 1) FF=false → builder/publish 호출 0회, False
    async def test_returns_false_when_flag_disabled(self):
        db = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await publish_tether_tab_snapshot(db)
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    # 2) subscriber 0 → builder/publish 호출 0회, False
    async def test_returns_false_when_no_subscribers(self):
        db = MagicMock()
        # registry 비어 있음 (asyncSetUp에서 swap)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await publish_tether_tab_snapshot(db)
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    # 3) FF=true + subscriber 1 + sent=1 → True
    async def test_returns_true_when_send_succeeds(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        fake_payload = {"type": "snapshot", "version": 1, "data": {}}
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value=fake_payload) as mock_build, \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            result = await publish_tether_tab_snapshot(db)
        self.assertTrue(result)
        mock_build.assert_called_once()
        mock_pub.assert_awaited_once()

    # 4) builder 호출 인자 include_krx=False
    async def test_calls_builder_with_include_krx_false(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}) as mock_build, \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            await publish_tether_tab_snapshot(db)

        # include_krx=False keyword로 호출됨
        mock_build.assert_called_once()
        call_args = mock_build.call_args
        self.assertEqual(call_args.kwargs.get("include_krx"), False)
        # db도 그대로 전달
        self.assertIs(call_args.args[0], db)

    # 5) publish_topic이 0 반환 (모든 send 실패) → wrapper False 반환
    async def test_returns_false_when_publish_sends_zero(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=0)):
            result = await publish_tether_tab_snapshot(db)
        self.assertFalse(result)

    # 6) publish 호출 payload == builder 반환 payload (무변형 계약, Codex 권고)
    async def test_publish_payload_matches_builder_output(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        builder_payload = {
            "type": "snapshot",
            "version": 1,
            "data": {
                "usdt_krw": [{"source": "upbit", "rate": 1485.0}],
                "usd_krw_banks": [{"source": "kb", "rate": 1380.0}],
            },
        }
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value=builder_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            await publish_tether_tab_snapshot(db)

        # publish_topic 호출 인자 검증
        mock_pub.assert_awaited_once()
        call_args = mock_pub.call_args
        # positional: (topic, payload)
        self.assertEqual(call_args.args[0], TETHER_TOPIC)
        # wrapper가 payload 변형 X — builder 반환 그대로 전달
        self.assertIs(call_args.args[1], builder_payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
