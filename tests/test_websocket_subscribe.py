"""WebSocket subscribe/unsubscribe 메시지 dispatcher 단위 테스트 (PR Z-2b Stage 2).

`app.topic_dispatcher.handle_client_message` 검증:
  - ping → pong (legacy 보존)
  - subscribe/unsubscribe FF gate
  - JSON 파싱 실패 / non-dict / unknown type / invalid topics 격리
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, topic_dispatcher
from app.topic_dispatcher import handle_client_message


def _fresh_registry_swap():
    """모듈 싱글톤을 격리된 인스턴스로 교체. 호출자가 setUp/tearDown에서 사용."""
    original = topic_dispatcher.registry
    topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
    return original


class TestHandleClientMessage(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = _fresh_registry_swap()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    # ─────────────────────────────────────────────────────────────
    # ping/pong (legacy 보존)
    # ─────────────────────────────────────────────────────────────

    async def test_ping_returns_pong(self):
        """legacy ping text → pong JSON. 기존 동작 보존."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await handle_client_message(ws, "ping")
        ws.send_json.assert_awaited_once_with({"type": "pong"})

    async def test_ping_does_not_register_topics(self):
        """ping은 registry에 영향 X."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await handle_client_message(ws, "ping")
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    # ─────────────────────────────────────────────────────────────
    # FF=false: subscribe/unsubscribe 무시
    # ─────────────────────────────────────────────────────────────

    async def test_subscribe_ignored_when_flag_disabled(self):
        """FF=false이면 subscribe 메시지 받아도 registry 변경 X (silent)."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            await handle_client_message(
                ws, '{"type": "subscribe", "topics": ["usdt:krw"]}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)
        ws.send_json.assert_not_called()

    async def test_unsubscribe_ignored_when_flag_disabled(self):
        """FF=false이면 unsubscribe도 무시."""
        ws = MagicMock()
        # 사전 등록 — FF=true 컨텍스트에서 등록됐다고 가정
        topic_dispatcher.registry.register(ws, ["usdt:krw"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            await handle_client_message(
                ws, '{"type": "unsubscribe", "topics": ["usdt:krw"]}'
            )
        # 변경 X
        self.assertEqual(
            topic_dispatcher.registry.get_subscriptions(ws), {"usdt:krw"}
        )

    # ─────────────────────────────────────────────────────────────
    # FF=true: subscribe/unsubscribe 실제 동작
    # ─────────────────────────────────────────────────────────────

    async def test_subscribe_with_flag_enabled_registers_topics(self):
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(
                ws,
                '{"type": "subscribe", "topics": ["usdt:krw", "krx:usd-krw-futures"]}',
            )
        self.assertEqual(
            topic_dispatcher.registry.get_subscriptions(ws),
            {"usdt:krw", "krx:usd-krw-futures"},
        )

    async def test_unsubscribe_with_flag_enabled_removes_topics(self):
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(
                ws, '{"type": "unsubscribe", "topics": ["usdt:krw"]}'
            )
        self.assertEqual(
            topic_dispatcher.registry.get_subscriptions(ws),
            {"krx:usd-krw-futures"},
        )

    # ─────────────────────────────────────────────────────────────
    # 입력 격리: JSON 파싱 실패 / non-dict / 잘못된 payload
    # ─────────────────────────────────────────────────────────────

    async def test_invalid_json_silently_ignored(self):
        """non-JSON / non-ping text는 무시 (legacy 호환)."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(ws, "garbage{")
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)
        ws.send_json.assert_not_called()

    async def test_non_dict_json_ignored(self):
        """JSON array / scalar 등 dict 아닌 페이로드 무시."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(ws, '["subscribe", "usdt:krw"]')
            await handle_client_message(ws, '"hello"')
            await handle_client_message(ws, "42")
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_unknown_type_ignored(self):
        """forward-compat: 모르는 type은 조용히 무시 (예외 X, registry 무변경)."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(
                ws, '{"type": "future_feature", "topics": ["x"]}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_subscribe_without_topics_field_ignored(self):
        """topics 필드 누락 → debug 로그 + 무시."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(ws, '{"type": "subscribe"}')
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_subscribe_with_non_list_topics_ignored(self):
        """topics가 string 등 list가 아니면 무시."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(
                ws, '{"type": "subscribe", "topics": "usdt:krw"}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_subscribe_with_non_string_topic_items_ignored(self):
        """topics 안에 string 아닌 element (int 등) 섞여 있으면 전체 무시."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(
                ws, '{"type": "subscribe", "topics": ["usdt:krw", 42]}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
