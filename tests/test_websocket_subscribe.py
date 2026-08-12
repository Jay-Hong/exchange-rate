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
from app import topic_wire
from app.topic_auth_rollout import TopicAuthRollout, TopicAuthStage
from app.topic_dispatcher import handle_client_message as _real_handle_client_message

# 이 파일의 주제는 **무토큰(legacy) 경로**다 — 인증 seam은 그 경로를 타지 않는다.
# `authorize_subscribe`가 필수 kwarg가 됐으므로(fail-open 방지) 여기서 한 번만 채운다.
# ⛔ 인증 경로의 계약은 이 래퍼가 아니라 실제 `/ws` 경계 테스트가 검증한다
#    (tests/test_topic_initial_snapshot_e2e.py — TestAuthenticatedSubscribeIsAcknowledged).
_UNUSED_AUTHZ = AsyncMock(side_effect=AssertionError("무토큰 경로는 인증자를 부르지 않아야 한다"))
_FX_TOPICS = ("fx:usd-krw", "fx:jpy-krw", "fx:eur-krw")
_USDT_TOPIC = "usdt:krw"


def _rollout(stage=TopicAuthStage.COMPATIBILITY):
    return TopicAuthRollout(
        stage=stage,
        fx_topics=_FX_TOPICS,
        usdt_topic=_USDT_TOPIC,
        started_at_epoch_seconds=0,
    )


async def handle_client_message(ws, raw_text, **kwargs):
    kwargs.setdefault("authorize_subscribe", _UNUSED_AUTHZ)
    return await _real_handle_client_message(ws, raw_text, **kwargs)


def _fresh_registry_swap():
    """모듈 싱글톤을 격리된 인스턴스로 교체. 호출자가 setUp/tearDown에서 사용."""
    original = topic_dispatcher.registry
    topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
    return original


async def _dispatch(ws, raw_text, **kwargs):
    """`handle_client_message` 를 단위 테스트용 기본값과 함께 부른다.

    ⚠️ `identity` 와 `topic_auth_rollout` 은 **필수 keyword-only** 다. 테스트마다 새 identity
    holder 를 만들면 cross-UID 를 영영 관측할 수 없으므로 같은 연결 테스트는 직접 넘긴다.
    rollout 계수의 연속성이 필요한 테스트도 같은 객체를 직접 넘긴다.
    """
    kwargs.setdefault("identity", topic_wire.ConnectionIdentity())
    kwargs.setdefault("topic_auth_rollout", _rollout())
    return await handle_client_message(ws, raw_text, **kwargs)


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
        await _dispatch(ws, "ping")
        ws.send_json.assert_awaited_once_with({"type": "pong"})

    async def test_ping_does_not_register_topics(self):
        """ping은 registry에 영향 X."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await _dispatch(ws, "ping")
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    # ─────────────────────────────────────────────────────────────
    # FF=false: subscribe/unsubscribe 무시
    # ─────────────────────────────────────────────────────────────

    async def test_subscribe_ignored_when_flag_disabled(self):
        """FF=false이면 subscribe 메시지 받아도 registry 변경 X (silent)."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            await _dispatch(
                ws, '{"type": "subscribe", "topics": ["usdt:krw"]}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)
        ws.send_json.assert_not_called()

    async def test_anonymous_subscribe_is_observed_before_disabled_gate(self):
        """dispatcher off 는 등록을 막지만 전환 영향 계측까지 막으면 안 된다."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        rollout = _rollout()

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            await _dispatch(
                ws,
                '{"type": "subscribe", "topics": ["fx:usd-krw", "usdt:krw"]}',
                topic_auth_rollout=rollout,
            )

        snapshot = rollout.snapshot()
        self.assertEqual(snapshot["anonymous_subscribe_attempts_total"], 1)
        self.assertEqual(snapshot["anonymous_fx_attempts_total"], 1)
        self.assertEqual(snapshot["per_topic_attempts"]["usdt:krw"], 1)
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)
        ws.send_json.assert_not_called()

    async def test_unsubscribe_ignored_when_flag_disabled(self):
        """FF=false이면 unsubscribe도 무시."""
        ws = MagicMock()
        # 사전 등록 — FF=true 컨텍스트에서 등록됐다고 가정
        topic_dispatcher.registry.register(ws, ["usdt:krw"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            await _dispatch(
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
        # subscribe(FF on)는 이제 snapshot-on-subscribe도 트리거 → routing 검증만 격리하려고
        # send_initial_snapshots를 no-op으로 patch(snapshot 로직은 test_topic_initial_snapshot.py).
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.topic_initial_snapshot.send_initial_snapshots", new=AsyncMock()):
            await _dispatch(
                ws,
                '{"type": "subscribe", "topics": ["usdt:krw", "krx:usd-krw-futures"]}',
            )
        # ⛔ **구 기대값은 취약점이었다**: 무토큰 subscribe 가 `krx:usd-krw-futures` 까지
        #    등록하면, publish 가 lease 부재를 "무제한"으로 취급하므로 **무인증 유료 데이터
        #    우회**가 된다. 무토큰 경로는 이제 **무료 topic 만** 등록한다(§E1 은 무료 범위
        #    안에서의 기존 동작 보존이다).
        self.assertEqual(
            topic_dispatcher.registry.get_subscriptions(ws),
            {"usdt:krw"},
        )

    async def test_subscribe_triggers_initial_snapshot(self):
        """subscribe(FF on) → register 후 send_initial_snapshots 호출 (snapshot-on-subscribe wiring)."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch(
                 "app.topic_initial_snapshot.send_initial_snapshots", new=AsyncMock()
             ) as mock_snap:
            await _dispatch(
                ws, '{"type": "subscribe", "topics": ["fx:usd-krw"]}'
            )
        mock_snap.assert_awaited_once_with(ws, ["fx:usd-krw"])
        # register는 snapshot 전에 완료 (구독 등록 보장)
        self.assertEqual(
            topic_dispatcher.registry.get_subscriptions(ws), {"fx:usd-krw"}
        )

    async def test_reject_stage_filters_only_free_topics_in_a_mixed_request(self):
        """KRX·unknown 을 제거한 뒤 FX 만 거부해야 하며 USDT 는 유지한다."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        rollout = _rollout(TopicAuthStage.REJECT_ANONYMOUS_FX)

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch(
                 "app.topic_initial_snapshot.supported_snapshot_topics",
                 return_value=_FX_TOPICS + (_USDT_TOPIC, "krx:usd-krw-futures"),
             ), \
             patch(
                 "app.topic_initial_snapshot.per_user_gated_snapshot_topics",
                 return_value=frozenset({"krx:usd-krw-futures"}),
             ), \
             patch(
                 "app.topic_initial_snapshot.send_initial_snapshots", new=AsyncMock()
             ) as mock_snap:
            await _dispatch(
                ws,
                '{"type":"subscribe","topics":["fx:usd-krw",'
                '"krx:usd-krw-futures","unknown:topic","usdt:krw"]}',
                topic_auth_rollout=rollout,
            )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), {_USDT_TOPIC})
        mock_snap.assert_awaited_once_with(ws, [_USDT_TOPIC])
        ws.send_json.assert_not_called()

    async def test_reject_stage_fx_only_is_silent_and_registers_nothing(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        rollout = _rollout(TopicAuthStage.REJECT_ANONYMOUS_FX)

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch(
                 "app.topic_initial_snapshot.send_initial_snapshots", new=AsyncMock()
             ) as mock_snap:
            await _dispatch(
                ws,
                '{"type":"subscribe","topics":["fx:usd-krw","fx:jpy-krw"]}',
                topic_auth_rollout=rollout,
            )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), set())
        mock_snap.assert_not_awaited()
        ws.send_json.assert_not_called()

    async def test_enforcement_stage_silently_rejects_all_anonymous_topics(self):
        """최종 stage는 UID가 없는 요청을 premium 판정으로 보내지 않고 전부 제외한다."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        rollout = _rollout(TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM)

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch(
            "app.topic_initial_snapshot.send_initial_snapshots", new=AsyncMock()
        ) as mock_snap:
            await _dispatch(
                ws,
                '{"type":"subscribe","topics":["fx:usd-krw","usdt:krw"]}',
                topic_auth_rollout=rollout,
            )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), set())
        mock_snap.assert_not_awaited()
        ws.send_json.assert_not_called()

    async def test_observation_failure_does_not_change_compatibility_behavior(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        rollout = _rollout()

        with patch.object(
            rollout, "observe_anonymous_subscribe", side_effect=RuntimeError("metrics down")
        ), patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch(
            "app.topic_initial_snapshot.send_initial_snapshots", new=AsyncMock()
        ) as mock_snap:
            await _dispatch(
                ws,
                '{"type":"subscribe","topics":["usdt:krw"]}',
                topic_auth_rollout=rollout,
            )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), {_USDT_TOPIC})
        mock_snap.assert_awaited_once_with(ws, [_USDT_TOPIC])

    async def test_policy_failure_propagates_before_registration(self):
        ws = MagicMock()
        rollout = _rollout()

        with patch.object(
            rollout, "filter_anonymous_topics", side_effect=RuntimeError("policy broken")
        ), patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch(
            "app.topic_initial_snapshot.send_initial_snapshots", new=AsyncMock()
        ) as mock_snap:
            with self.assertRaisesRegex(RuntimeError, "policy broken"):
                await _dispatch(
                    ws,
                    '{"type":"subscribe","topics":["usdt:krw"]}',
                    topic_auth_rollout=rollout,
                )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), set())
        mock_snap.assert_not_awaited()

    async def test_authenticated_result_for_a_different_plan_is_rejected(self):
        """coordinator 결과를 요청 plan과 대조하지 않으면 다른 topic의 권한을 재사용할 수 있다."""
        from app import topic_authorization, topic_policy

        ws = MagicMock()
        ws.send_json = AsyncMock()
        wrong_plan = topic_policy.AuthorizationPlan(
            uid="u1",
            identity_only=("fx:usd-krw",),
            premium_only=(),
            premium_and_entitlement=(),
        )
        wrong_outcome = topic_authorization.AuthorizationOutcome(
            plan=wrong_plan, premium=None, entitlement=None
        )

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch.object(
            topic_dispatcher,
            "authorize_subscription_plan",
            new=AsyncMock(return_value=wrong_outcome),
        ), self.assertRaisesRegex(ValueError, "다른 authorization plan"):
            await _dispatch(
                ws,
                '{"type":"subscribe","request_id":"plan-mismatch",'
                '"id_token":"tok","topics":["usdt:krw"]}',
                authorize_subscribe=AsyncMock(return_value="u1"),
                identity=topic_wire.ConnectionIdentity(),
            )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), set())
        ws.send_json.assert_not_called()

    async def test_unsubscribe_with_flag_enabled_removes_topics(self):
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(
                ws, '{"type": "unsubscribe", "topics": ["usdt:krw"]}'
            )
        self.assertEqual(
            topic_dispatcher.registry.get_subscriptions(ws),
            {"krx:usd-krw-futures"},
        )

    async def test_reject_stage_does_not_block_anonymous_unsubscribe(self):
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw", _USDT_TOPIC])
        rollout = _rollout(TopicAuthStage.REJECT_ANONYMOUS_FX)

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(
                ws,
                '{"type":"unsubscribe","topics":["fx:usd-krw"]}',
                topic_auth_rollout=rollout,
            )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), {_USDT_TOPIC})
        self.assertEqual(rollout.snapshot()["anonymous_subscribe_attempts_total"], 0)

    async def test_enforcement_stage_still_allows_anonymous_unsubscribe(self):
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw", _USDT_TOPIC])
        rollout = _rollout(TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM)

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(
                ws,
                '{"type":"unsubscribe","topics":["fx:usd-krw","usdt:krw"]}',
                topic_auth_rollout=rollout,
            )

        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws), set())

    # ─────────────────────────────────────────────────────────────
    # 입력 격리: JSON 파싱 실패 / non-dict / 잘못된 payload
    # ─────────────────────────────────────────────────────────────

    async def test_invalid_json_silently_ignored(self):
        """non-JSON / non-ping text는 무시 (legacy 호환)."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(ws, "garbage{")
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)
        ws.send_json.assert_not_called()

    async def test_non_dict_json_ignored(self):
        """JSON array / scalar 등 dict 아닌 페이로드 무시."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(ws, '["subscribe", "usdt:krw"]')
            await _dispatch(ws, '"hello"')
            await _dispatch(ws, "42")
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_unknown_type_ignored(self):
        """forward-compat: 모르는 type은 조용히 무시 (예외 X, registry 무변경)."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(
                ws, '{"type": "future_feature", "topics": ["x"]}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_subscribe_without_topics_field_ignored(self):
        """topics 필드 누락 → debug 로그 + 무시."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(ws, '{"type": "subscribe"}')
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_subscribe_with_non_list_topics_ignored(self):
        """topics가 string 등 list가 아니면 무시."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(
                ws, '{"type": "subscribe", "topics": "usdt:krw"}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_subscribe_with_non_string_topic_items_ignored(self):
        """topics 안에 string 아닌 element (int 등) 섞여 있으면 전체 무시."""
        ws = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await _dispatch(
                ws, '{"type": "subscribe", "topics": ["usdt:krw", 42]}'
            )
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_only_valid_anonymous_subscribe_is_observed(self):
        rollout = MagicMock(spec=TopicAuthRollout)
        ws = MagicMock()
        ws.send_json = AsyncMock()

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            await _dispatch(ws, "ping", topic_auth_rollout=rollout)
            await _dispatch(ws, "not-json", topic_auth_rollout=rollout)
            await _dispatch(
                ws,
                '{"type":"subscribe","topics":[]}',
                topic_auth_rollout=rollout,
            )
            await _dispatch(
                ws,
                '{"type":"unsubscribe","topics":["fx:usd-krw"]}',
                topic_auth_rollout=rollout,
            )
            await _dispatch(
                ws,
                '{"type":"subscribe","request_id":"r1","id_token":"token",'
                '"topics":["fx:usd-krw"]}',
                topic_auth_rollout=rollout,
            )

        rollout.observe_anonymous_subscribe.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
