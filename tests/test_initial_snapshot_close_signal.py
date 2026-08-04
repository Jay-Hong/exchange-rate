"""initial snapshot 중 연결 종료 — **닫힌 소켓에 다시 읽지 않는가**.

⛔ **운영 canary 를 실제로 중단시킨 결함이다.** `send_initial_snapshots` 가 send 실패를 잡아
   registry 만 정리하고 **정상 반환**했고, 그러면 endpoint 의 `while True` 가 **닫힌 소켓에
   `receive_text()` 를 다시 호출**해 `RuntimeError: WebSocket is not connected` → 상위
   `except Exception` → **ERROR + traceback** 이 남았다.
⚠️ **로그 레벨을 낮추는 것은 답이 아니다** — 그건 제어흐름 결함을 덮는다. 끊긴 연결에 계속
   읽기를 시도하는 것 자체가 문제이고, 정상적인 클라 종료가 ERROR 채널을 오염시킨다
   (실사용자 단말은 백그라운드 전환·네트워크 전환으로 수시로 끊는다).
"""

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import auth_executor


def setUpModule():
    auth_executor.start_auth_executor(2)


def tearDownModule():
    auth_executor.shutdown_auth_executor()


from app import main as app_main            # noqa: E402
from app import topic_dispatcher            # noqa: E402
from app.topic_wire import InitialSnapshotConnectionClosed  # noqa: E402


class _ClosedDuringSnapshot:
    """ack 은 통과시키고 **snapshot 전송에서** 연결이 닫힌 소켓."""

    def __init__(self):
        self.headers = {"x-real-ip": "203.0.113.7"}
        self.receive_calls = 0
        self.sent_types: list = []
        self._closed = False

    async def accept(self):
        return None

    async def send_json(self, message):
        kind = message.get("type") if isinstance(message, dict) else "?"
        if kind == "snapshot":
            self._closed = True
            raise RuntimeError("connection closed during snapshot")   # ConnectionClosedOK 대역
        self.sent_types.append(kind)

    async def receive_text(self):
        self.receive_calls += 1
        if self._closed:
            # ⛔ 실제 starlette 동작 — 닫힌 소켓을 다시 읽으면 이 예외가 난다.
            raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')
        if self.receive_calls == 1:
            import json
            return json.dumps({"type": "subscribe", "topics": ["fx:usd-krw"]})
        await asyncio.sleep(3600)         # 두 번째 호출이 오면 테스트가 잡는다

    async def close(self, code=1000):
        self._closed = True


class TestInitialSnapshotCloseSignal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        topic_dispatcher.registry._subscriptions.clear()
        app_main.manager.active_connections.clear()

    async def _run_endpoint(self, websocket):
        with patch.object(app_main, "redis_cache", MagicMock(get=AsyncMock(return_value=None),
                                                             set=AsyncMock())), \
             patch.object(app_main, "build_rates_payload",
                          return_value={"type": "rates", "data": {"rates": []}}), \
             patch.object(app_main, "SessionLocal", MagicMock()), \
             patch("app.topic_initial_snapshot._build_snapshot_sync",
                   side_effect=lambda topic: {"type": "snapshot", "topic": topic, "data": {}}), \
             patch.object(app_main.config, "TOPIC_DISPATCHER_ENABLED", True):
            await app_main.websocket_endpoint(websocket)

    async def test_closed_socket_is_not_read_again_and_leaves_no_error(self):
        """⛔ **핵심**: snapshot 실패 뒤 `receive_text()` 가 다시 불리면 안 된다."""
        websocket = _ClosedDuringSnapshot()

        with self.assertLogs("exchange_rate", level=logging.INFO) as captured:
            await asyncio.wait_for(self._run_endpoint(websocket), timeout=10)

        self.assertEqual(websocket.receive_calls, 1,
                         "닫힌 소켓에 다시 읽었다 — 그 호출이 ERROR + traceback 을 만든다")

        errors = [r for r in captured.records if r.levelno >= logging.ERROR]
        self.assertEqual(errors, [], f"정상 종료인데 ERROR 가 남았다: {[r.message for r in errors]}")
        self.assertTrue(
            any("initial snapshot 중 연결 종료" in r.getMessage() for r in captured.records),
            "정상 종료를 INFO 로 남기지 않았다",
        )

    async def test_registry_and_manager_are_cleaned_up(self):
        """⚠️ 신호로 바꿔도 `finally` 의 정리는 그대로 돌아야 한다."""
        websocket = _ClosedDuringSnapshot()
        await asyncio.wait_for(self._run_endpoint(websocket), timeout=10)

        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0,
                         "registry 에 죽은 연결이 남았다")
        self.assertNotIn(websocket, app_main.manager.active_connections,
                         "manager gauge 가 줄지 않았다")

    async def test_traffic_before_the_close_is_unaffected(self):
        """⚠️ 종료 신호는 **snapshot 실패 지점부터**다 — 그 전 정상 전송은 그대로 나가야 한다.
        (이 경로는 무토큰 subscribe = §E1 free-topic 등록이라 ack 이 없다. ack 순서 계약은
        인증 경로 테스트가 따로 본다.)"""
        websocket = _ClosedDuringSnapshot()
        await asyncio.wait_for(self._run_endpoint(websocket), timeout=10)
        self.assertIn("rates", websocket.sent_types, "종료 전 정상 전송까지 사라졌다")

    def test_signal_type_is_a_lifecycle_signal_not_a_failure(self):
        """⚠️ `SubscribeIdentityConflict` 와 **같은 축**이다 — 연결을 닫으라는 신호."""
        self.assertTrue(issubclass(InitialSnapshotConnectionClosed, Exception))
        source = __import__("inspect").getsource(app_main.websocket_endpoint)
        self.assertIn("InitialSnapshotConnectionClosed", source,
                      "endpoint 가 이 신호를 처리하지 않는다")
