"""topic 구독자 lifecycle 통합 테스트 (realtime topic readiness — live-publish 검증).

조각별 커버리지는 이미 존재:
  - publish_topic → subscriber 전달 / 실패 격리: test_topic_dispatcher.py
  - publisher SET-only/gate: test_fx_topic_publisher.py / test_tether_topic_publisher.py
  - snapshot-on-subscribe ASGI wire: test_topic_initial_snapshot_e2e.py

본 파일은 그 조각들이 **하나의 구독자 lifecycle 흐름**으로 맞물리는지 검증하는 통합/회귀 가드:
  subscribe(register) → snapshot-on-subscribe → live publish 수신 → reconnect 후 수신 유지
  → unsubscribe 후 미수신.

deterministic (feedback_flaky_sleep_async_tests): 실 send_initial_snapshots/publish_topic/registry +
synthetic subscriber(async send_json capture). _build_snapshot_sync만 canned patch(builder 격리,
ASGI transport는 e2e가 별도 증명). no sleep.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import config, topic_dispatcher
from app.topic_dispatcher import publish_topic
from app.topic_initial_snapshot import send_initial_snapshots

_TOPIC = "usdt:krw"


class _FakeSubscriber:
    """async send_json capture — registry에 등록 가능한 synthetic ws."""

    def __init__(self):
        self.received: list = []

    async def send_json(self, payload):
        self.received.append(payload)


def _canned_snapshot(topic):
    return {"type": "snapshot", "version": 1, "topic": topic, "data": {"_snap": topic}}


class TestSubscriberLifecycle(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
        # publish_topic FF gate 통과 (live 활성 상황 시뮬)
        self._ff = patch.object(config, "TOPIC_DISPATCHER_ENABLED", True)
        self._ff.start()
        self._build = patch(
            "app.topic_initial_snapshot._build_snapshot_sync", side_effect=_canned_snapshot
        )
        self._build.start()

    async def asyncTearDown(self):
        self._build.stop()
        self._ff.stop()
        topic_dispatcher.registry = self._original_registry

    async def test_subscribe_snapshot_then_live_publish(self):
        """register → snapshot-on-subscribe → live publish가 같은 구독자에게 순서대로 도달."""
        ws = _FakeSubscriber()
        topic_dispatcher.registry.register(ws, [_TOPIC])
        await send_initial_snapshots(ws, [_TOPIC])
        # snapshot 수신
        self.assertEqual(len(ws.received), 1)
        self.assertEqual(ws.received[0]["type"], "snapshot")

        live = {"type": "snapshot", "version": 1, "topic": _TOPIC, "data": {"_live": 1}}
        sent = await publish_topic(_TOPIC, live)
        self.assertEqual(sent, 1)
        # snapshot 다음에 live 수신 (순서 유지)
        self.assertEqual(len(ws.received), 2)
        self.assertEqual(ws.received[1]["data"], {"_live": 1})

    async def test_reconnect_keeps_receiving_publish(self):
        """disconnect(remove) 후 옛 ws 미수신, reconnect(새 ws register) 후 publish 수신 유지."""
        ws1 = _FakeSubscriber()
        topic_dispatcher.registry.register(ws1, [_TOPIC])
        await publish_topic(_TOPIC, {"data": {"n": 1}})
        self.assertEqual(len(ws1.received), 1)

        # disconnect (main.py finally의 remove_websocket 시뮬)
        topic_dispatcher.registry.remove_websocket(ws1)
        await publish_topic(_TOPIC, {"data": {"n": 2}})
        self.assertEqual(len(ws1.received), 1)  # 옛 ws엔 더 안 옴

        # reconnect — 새 connection register
        ws2 = _FakeSubscriber()
        topic_dispatcher.registry.register(ws2, [_TOPIC])
        await send_initial_snapshots(ws2, [_TOPIC])  # 재구독 snapshot
        await publish_topic(_TOPIC, {"data": {"n": 3}})
        # ws2 = snapshot + publish(n=3)
        self.assertEqual([m.get("data") for m in ws2.received], [{"_snap": _TOPIC}, {"n": 3}])

    async def test_unsubscribe_stops_publish(self):
        """unsubscribe 후 publish 미수신."""
        ws = _FakeSubscriber()
        topic_dispatcher.registry.register(ws, [_TOPIC])
        await publish_topic(_TOPIC, {"data": {"n": 1}})
        self.assertEqual(len(ws.received), 1)

        topic_dispatcher.registry.unregister(ws, [_TOPIC])
        sent = await publish_topic(_TOPIC, {"data": {"n": 2}})
        self.assertEqual(sent, 0)
        self.assertEqual(len(ws.received), 1)  # 변화 없음


if __name__ == "__main__":
    unittest.main(verbosity=2)
