"""snapshot-on-subscribe e2e wire 테스트 (realtime topic release readiness smoke harness 1).

목적: subscriber=0이라 그동안 미검증이던 **실 WebSocket wire 경로**를 자동 회귀로 잠근다 —
TestClient ws connect → subscribe → send_initial_snapshots → to_thread → ws.send_json →
client 수신. 즉 "구독하면 snapshot이 실제로 날아간다"를 ASGI transport 레벨로 검증.

flakiness 방어 (feedback_flaky_sleep_async_tests):
- `_build_snapshot_sync`를 canned로 patch → wire를 builder/Redis/DB와 분리(builder 로직은
  test_topic_initial_snapshot.py 단위 테스트).
- 모든 receive는 thread + join **timeout** 경유(`_receive_json`) → snapshot 미전달 regression
  시 hang이 아니라 fast assertion fail (codex 019efdd5 blocker). green path는 patch로 보장돼
  즉시 도착(no sleep).
- redis_cache.get을 None으로 patch → connect 시 Redis 연결 지연/불확정 제거(→ DB fallback
  legacy payload, 빈 테이블).
- conftest: firebase stub + file-backed sqlite. lifespan(scheduler)은 TestClient context
  manager 미사용으로 미진입.

scope: connect→subscribe→snapshot 수신(fx + usdt) + reconnect→재수신. send-failure cleanup은
단위 테스트(test_topic_initial_snapshot.py), live/synthetic publish 수신은 별도 follow-up.
"""
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
from app import config, models
from app.database import engine
from app.main import app

models.Base.metadata.create_all(engine)  # 빈 테이블 (connect 초기 legacy payload용)


def _canned_snapshot(topic):
    return {"type": "snapshot", "version": 1, "topic": topic, "data": {"_canned": topic}}


class TestSnapshotOnSubscribeE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)  # context manager 미사용 → lifespan 미진입

    def _ctx(self):
        """FF on + canned builder + redis None patch 컨텍스트."""
        return (
            patch.object(config, "TOPIC_DISPATCHER_ENABLED", True),
            patch.object(config, "FX_TOPIC_ENABLED", True),
            patch(
                "app.topic_initial_snapshot._build_snapshot_sync",
                side_effect=_canned_snapshot,
            ),
            patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)),
        )

    def _receive_json(self, ws, timeout=5.0):
        """ws.receive_json()을 thread+join timeout으로 감쌈 — regression(미전달) 시 hang 대신
        fast assertion fail (codex 019efdd5: TestClient receive_json 자체엔 timeout 없음).
        green path는 patch로 보장돼 즉시 반환."""
        box = {}

        def _recv():
            try:
                box["msg"] = ws.receive_json()
            except Exception as exc:  # noqa: BLE001 — 그대로 재전파
                box["err"] = exc

        t = threading.Thread(target=_recv, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.fail(f"ws.receive_json {timeout}s timeout — 메시지 미전달(regression)")
        if "err" in box:
            raise box["err"]
        return box["msg"]

    def _recv_snapshot(self, ws, topic, max_msgs=4):
        """초기 legacy payload(type=rates 등)를 건너뛰고 해당 topic snapshot을 찾음.

        각 receive는 timeout 경유 → snapshot 미전달 시 max_msgs 도달 전이라도 fast fail.
        """
        for _ in range(max_msgs):
            msg = self._receive_json(ws)
            if msg.get("type") == "snapshot" and msg.get("topic") == topic:
                return msg
        self.fail(f"snapshot for {topic} not received within {max_msgs} messages")

    def test_subscribe_fx_receives_snapshot(self):
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                snap = self._recv_snapshot(ws, "fx:usd-krw")
        self.assertEqual(snap["version"], 1)
        self.assertEqual(snap["data"], {"_canned": "fx:usd-krw"})

    def test_subscribe_tether_receives_snapshot(self):
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "topics": ["usdt:krw"]})
                snap = self._recv_snapshot(ws, "usdt:krw")
        self.assertEqual(snap["topic"], "usdt:krw")

    def test_subscribe_multiple_topics_receives_each_snapshot(self):
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json(
                    {"type": "subscribe", "topics": ["fx:usd-krw", "usdt:krw"]}
                )
                # 두 snapshot 모두 도착 (순서 무관 — topic으로 식별)
                got = set()
                for _ in range(6):
                    msg = self._receive_json(ws)
                    if msg.get("type") == "snapshot":
                        got.add(msg.get("topic"))
                    if {"fx:usd-krw", "usdt:krw"} <= got:
                        break
        self.assertEqual(got & {"fx:usd-krw", "usdt:krw"}, {"fx:usd-krw", "usdt:krw"})

    def test_reconnect_resubscribe_receives_snapshot_again(self):
        """재연결(새 connection) 후 subscribe → snapshot 재수신 (resync)."""
        p1, p2, p3, p4 = self._ctx()
        with p1, p2, p3, p4:
            with self.client.websocket_connect("/ws") as ws:
                ws.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                self._recv_snapshot(ws, "fx:usd-krw")
            # 새 connection
            with self.client.websocket_connect("/ws") as ws2:
                ws2.send_json({"type": "subscribe", "topics": ["fx:usd-krw"]})
                snap = self._recv_snapshot(ws2, "fx:usd-krw")
        self.assertEqual(snap["topic"], "fx:usd-krw")


class TestTopicSnapshotRestBootstrap(unittest.TestCase):
    """GET /api/v2/topics/snapshot — REST bootstrap (OPEN 1 해소). _build_snapshot_sync patch로 격리."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_dormant_when_dispatcher_disabled(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "usdt:krw"})
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"], "topics_disabled")
        self.assertNotIn("supported_topics", body)  # dormant 시 미노출 (codex 019efe2d)

    def test_unknown_topic_404(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "dxy"})
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"], "unknown_topic")
        self.assertIn("usdt:krw", body["supported_topics"])

    def test_supported_topic_returns_snapshot_with_no_store(self):
        canned = {"type": "snapshot", "version": 1, "topic": "usdt:krw",
                  "data": {"_canned": True}}
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=canned):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "usdt:krw"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), canned)  # WS snapshot과 동일 contract
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_topic_unavailable_when_build_none(self):
        """지원 topic이나 _build_snapshot_sync None(예: fx FX_TOPIC_ENABLED off) → 404 topic_unavailable."""
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.topic_initial_snapshot._build_snapshot_sync", return_value=None):
            r = self.client.get("/api/v2/topics/snapshot", params={"topic": "fx:usd-krw"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "topic_unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
