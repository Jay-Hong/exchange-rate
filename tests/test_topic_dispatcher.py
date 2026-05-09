"""topic_dispatcher 단위 테스트 (PR Z-2b Stage 1).

TopicRegistry register/unregister/remove + publish_topic FF 분기 + 실패 격리.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, topic_dispatcher


def _fresh_registry() -> topic_dispatcher.TopicRegistry:
    """test isolation — 모듈 싱글톤 대신 fresh 인스턴스."""
    return topic_dispatcher.TopicRegistry()


# ---------------------------------------------------------------------------
# TopicRegistry — synchronous registry ops
# ---------------------------------------------------------------------------

class TestTopicRegistry(unittest.TestCase):

    def test_register_creates_entry(self):
        reg = _fresh_registry()
        ws = MagicMock()
        result = reg.register(ws, ["usdt:krw"])
        self.assertEqual(result, {"usdt:krw"})
        self.assertEqual(reg.subscribed_connection_count, 1)

    def test_register_idempotent_union(self):
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw"])
        result = reg.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        # 합집합 — 중복 추가는 그대로
        self.assertEqual(result, {"usdt:krw", "krx:usd-krw-futures"})
        self.assertEqual(reg.subscribed_connection_count, 1)

    def test_unregister_partial(self):
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        result = reg.unregister(ws, ["usdt:krw"])
        self.assertEqual(result, {"krx:usd-krw-futures"})
        self.assertEqual(reg.subscribed_connection_count, 1)

    def test_unregister_clears_entry_when_empty(self):
        """모든 topic 해제 시 entry 제거 — 메모리 누수 방지."""
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw"])
        result = reg.unregister(ws, ["usdt:krw"])
        self.assertEqual(result, set())
        self.assertEqual(reg.subscribed_connection_count, 0)

    def test_unregister_unknown_websocket_safe(self):
        reg = _fresh_registry()
        ws = MagicMock()
        # 등록 X 상태에서 unregister — 예외 X, 빈 set
        result = reg.unregister(ws, ["usdt:krw"])
        self.assertEqual(result, set())
        self.assertEqual(reg.subscribed_connection_count, 0)

    def test_remove_websocket_clears_all_subscriptions(self):
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        reg.remove_websocket(ws)
        self.assertEqual(reg.subscribed_connection_count, 0)
        self.assertEqual(reg.get_subscriptions(ws), set())

    def test_get_subscribers_returns_only_topic_subscribers(self):
        reg = _fresh_registry()
        ws_a = MagicMock(name="ws_a")
        ws_b = MagicMock(name="ws_b")
        ws_c = MagicMock(name="ws_c")
        reg.register(ws_a, ["usdt:krw"])
        reg.register(ws_b, ["usdt:krw", "krx:usd-krw-futures"])
        reg.register(ws_c, ["krx:usd-krw-futures"])

        usdt_subs = reg.get_subscribers("usdt:krw")
        self.assertEqual(usdt_subs, {ws_a, ws_b})

        krx_subs = reg.get_subscribers("krx:usd-krw-futures")
        self.assertEqual(krx_subs, {ws_b, ws_c})

        # 미존재 topic — 빈 set
        self.assertEqual(reg.get_subscribers("non-existent"), set())

    def test_get_subscriptions_returns_snapshot_copy(self):
        """반환된 set 수정이 registry 내부에 영향 없는지 (snapshot)."""
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw"])
        snap = reg.get_subscriptions(ws)
        snap.add("hacked")
        self.assertEqual(reg.get_subscriptions(ws), {"usdt:krw"})


# ---------------------------------------------------------------------------
# publish_topic — FF 분기 + 실패 격리
# ---------------------------------------------------------------------------

class TestPublishTopic(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # 모듈 싱글톤 registry를 격리된 인스턴스로 교체 — 다른 테스트와 분리
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_no_op_when_flag_disabled(self):
        """FF=false → subscribers 있어도 send 호출 X."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        topic_dispatcher.registry.register(ws, ["usdt:krw"])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            sent = await topic_dispatcher.publish_topic(
                "usdt:krw", {"type": "tick", "rate": 1450.0}
            )
        self.assertEqual(sent, 0)
        ws.send_json.assert_not_called()

    async def test_no_op_when_no_subscribers(self):
        """FF=true이지만 구독자 없으면 0 반환 (빈 loop)."""
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            sent = await topic_dispatcher.publish_topic(
                "usdt:krw", {"type": "tick"}
            )
        self.assertEqual(sent, 0)

    async def test_sends_to_topic_subscribers_only(self):
        """FF=true + 구독자 있음 → topic 일치하는 ws에만 send."""
        ws_a = MagicMock(name="ws_a")
        ws_a.send_json = AsyncMock()
        ws_b = MagicMock(name="ws_b")
        ws_b.send_json = AsyncMock()
        ws_c = MagicMock(name="ws_c")
        ws_c.send_json = AsyncMock()

        topic_dispatcher.registry.register(ws_a, ["usdt:krw"])
        topic_dispatcher.registry.register(ws_b, ["usdt:krw"])
        topic_dispatcher.registry.register(ws_c, ["krx:usd-krw-futures"])

        payload = {"type": "tick", "rate": 1450.0}
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            sent = await topic_dispatcher.publish_topic("usdt:krw", payload)

        self.assertEqual(sent, 2)
        ws_a.send_json.assert_awaited_once_with(payload)
        ws_b.send_json.assert_awaited_once_with(payload)
        ws_c.send_json.assert_not_called()

    async def test_send_failure_isolated_per_subscriber(self):
        """한 구독자 send 실패 → 격리 + registry에서 stale ws 즉시 제거."""
        ws_ok = MagicMock(name="ws_ok")
        ws_ok.send_json = AsyncMock()
        ws_fail = MagicMock(name="ws_fail")
        ws_fail.send_json = AsyncMock(side_effect=ConnectionError("closed"))

        topic_dispatcher.registry.register(ws_ok, ["usdt:krw"])
        topic_dispatcher.registry.register(ws_fail, ["usdt:krw"])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING") as cm:
            sent = await topic_dispatcher.publish_topic("usdt:krw", {})

        # 실패한 ws는 sent count 미반영
        self.assertEqual(sent, 1)
        ws_ok.send_json.assert_awaited_once()
        ws_fail.send_json.assert_awaited_once()
        self.assertTrue(any("topic publish 실패" in m for m in cm.output))

        # Codex 권고: 실패한 ws는 registry에서 즉시 제거 (stale entry 방지)
        subs_after = topic_dispatcher.registry.get_subscribers("usdt:krw")
        self.assertNotIn(ws_fail, subs_after)
        self.assertIn(ws_ok, subs_after)

    async def test_send_failure_remove_is_idempotent_with_disconnect_hook(self):
        """publish 실패 정리 + Stage 2 disconnect hook 중복 호출 안전성 (idempotent).

        실제 운영 시나리오: publish_topic이 ws_fail을 정리한 직후 main.py
        disconnect hook이 같은 ws를 remove_websocket() 호출 — 예외 없이 통과해야 함.
        """
        ws_fail = MagicMock(name="ws_fail")
        ws_fail.send_json = AsyncMock(side_effect=ConnectionError("closed"))
        topic_dispatcher.registry.register(ws_fail, ["usdt:krw", "krx:usd-krw-futures"])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING"):
            await topic_dispatcher.publish_topic("usdt:krw", {})

        # publish 실패 시 모든 구독에서 정리 (ws 자체가 끊겼으므로)
        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws_fail), set())

        # disconnect hook이 뒤늦게 호출돼도 예외 없음 (idempotent)
        topic_dispatcher.registry.remove_websocket(ws_fail)  # 무동작
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
