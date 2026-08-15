# tests/test_subscribe_load_terminal_wiring.py
"""S4 — dispatcher terminal counter 배선 계약.

설계 §7 S4:
  - `auth_wire_deadline_expired_by_stage` 는 **expired() True 분기에서만** 오른다 —
    검증자 내부 TimeoutError(미만료 재전파)는 세지 않는다.
  - 두 stage(identity/authorization)는 하나의 절대 deadline 을 공유한다. ⚠️ 버킷은
    **만료가 관측된 단계**이지 예산을 소비한 단계가 아니다(적대 probe 반례: identity 가
    예산 93% 를 쓰고 성공하면 만료는 authorization 에 계상) — 서로의 버킷 0 단언은
    "이 통제 시나리오에서 관측 지점이 갈린다" 를 잠그는 것이다.
  - `subscribe_auth_failed_by_error` 는 SubscribeAuthFailed(전체-요청, Firebase 전이 등)
    의 wire 코드를 센다 — 기존엔 어느 축에도 안 잡히던 gap.
  - wire 계약(오류 프레임·연결/registry 불변·재전파)은 배선 전과 동일해야 한다.
"""

# 표준 라이브러리
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 로컬
from app import config, topic_dispatcher, topic_policy, topic_wire
from app import subscribe_load_metrics as slm
from app import topic_initial_snapshot as _tis
from app.topic_auth_rollout import TopicAuthRollout, TopicAuthStage
from app.topic_dispatcher import handle_client_message

_FX_TOPICS = ("fx:usd-krw", "fx:jpy-krw", "fx:eur-krw")


def _rollout(stage=TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM):
    return TopicAuthRollout(
        stage=stage,
        fx_topics=_FX_TOPICS,
        usdt_topic="usdt:krw",
        policy_topics=tuple(sorted(topic_policy.TOPIC_POLICY)),
        final_stage_rc_candidate_topics=tuple(
            t for t in _tis.supported_snapshot_topics() if _tis.is_snapshot_topic_enabled(t)
        ),
        started_at_epoch_seconds=0,
    )


class TerminalWiringTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._hard_reset()
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
        self.ws = MagicMock()
        self.ws.send_json = AsyncMock()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry
        self._hard_reset()

    @staticmethod
    def _hard_reset():
        with slm._lock:
            slm._metrics.clear()
            slm._metrics.update(slm._blank())

    @staticmethod
    def _terminal():
        return slm.subscribe_load_metrics()["terminal"]

    async def _subscribe(self, *, authorize_subscribe, topics=("fx:usd-krw",)):
        raw = json.dumps({"type": "subscribe", "request_id": "r1",
                          "id_token": "tok", "topics": list(topics)})
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            await handle_client_message(
                self.ws, raw,
                identity=topic_wire.ConnectionIdentity(),
                topic_auth_rollout=_rollout(),
                authorize_subscribe=authorize_subscribe,
            )


class TestDeadlineStageCounters(TerminalWiringTestCase):
    async def test_identity_deadline_expiry_counts_identity_only(self):
        """실제 만료 → identity+1, authorization 0, 오류 프레임(wire 불변)."""
        async def slow_authz(token):
            await asyncio.sleep(0.5)
            return "u1"
        with patch.object(config, "WS_AUTH_WIRE_DEADLINE_SECONDS", 0.02):
            await self._subscribe(authorize_subscribe=slow_authz)
        stages = self._terminal()["auth_wire_deadline_expired_by_stage"]
        self.assertEqual(stages["identity"], 1)
        self.assertEqual(stages["authorization"], 0, "다른 stage 버킷이 오염됐다")
        frame = self.ws.send_json.await_args_list[-1].args[0]
        self.assertEqual(frame["error"], "temporarily_unavailable")

    async def test_validator_internal_timeout_is_reraised_and_not_counted(self):
        """미만료 TimeoutError 는 분류 불가 재전파 — **세지 않는다**(계약)."""
        async def authz_raises_its_own_timeout(token):
            raise asyncio.TimeoutError("validator internal")
        with self.assertRaises(asyncio.TimeoutError):
            await self._subscribe(authorize_subscribe=authz_raises_its_own_timeout)
        stages = self._terminal()["auth_wire_deadline_expired_by_stage"]
        self.assertEqual(sum(stages.values()), 0,
                         "미만료 재전파가 deadline 만료로 계상됐다")

    async def test_authorization_deadline_expiry_counts_authorization_only(self):
        """gate 단계 만료 → authorization+1, identity 0 (identity 는 정상 통과)."""
        async def fast_authz(token):
            return "u1"

        async def slow_plan(plan, *, mono):
            # ⛔ gate 경로 검증 — plan 이 premium 을 요구해야 timeout_at 아래로 들어온다.
            self.assertTrue(plan.requires_premium(), "premium plan 이 아니면 gate 가 없다")
            await asyncio.sleep(0.5)
            raise AssertionError("도달하면 안 된다")
        with patch.object(config, "WS_AUTH_WIRE_DEADLINE_SECONDS", 0.05), \
             patch.object(_tis, "supported_snapshot_topics",
                          new=lambda: frozenset({"fx:usd-krw"})), \
             patch.object(_tis, "is_snapshot_topic_enabled", new=lambda t: True), \
             patch.object(topic_dispatcher, "authorize_subscription_plan", new=slow_plan):
            await self._subscribe(authorize_subscribe=fast_authz)
        stages = self._terminal()["auth_wire_deadline_expired_by_stage"]
        self.assertEqual(stages["authorization"], 1)
        self.assertEqual(stages["identity"], 0, "다른 stage 버킷이 오염됐다")
        # wire 불변 — deadline 은 전체-요청 transient 로 접힌다 (연결 유지)
        frame = self.ws.send_json.await_args_list[-1].args[0]
        self.assertEqual(frame.get("error"), "temporarily_unavailable")


class TestAuthorizationInternalTimeout(TerminalWiringTestCase):
    async def test_gate_internal_timeout_is_reraised_and_not_counted(self):
        """[WF-M4] 미만료 TimeoutError 는 authorization 쪽에서도 재전파·미계상 — identity
        쪽만 잠그면 gate_cm 가드 앞 이동 변이가 생존했다(적대 probe 실측)."""
        async def fast_authz(token):
            return "u1"

        async def plan_raises_its_own_timeout(plan, *, mono):
            raise asyncio.TimeoutError("gate internal")
        with patch.object(_tis, "supported_snapshot_topics",
                          new=lambda: frozenset({"fx:usd-krw"})), \
             patch.object(_tis, "is_snapshot_topic_enabled", new=lambda t: True), \
             patch.object(topic_dispatcher, "authorize_subscription_plan",
                          new=plan_raises_its_own_timeout):
            with self.assertRaises(asyncio.TimeoutError):
                await self._subscribe(authorize_subscribe=fast_authz)
        stages = self._terminal()["auth_wire_deadline_expired_by_stage"]
        self.assertEqual(sum(stages.values()), 0, "미만료 재전파가 계상됐다")


class TestAuthFailedCounter(TerminalWiringTestCase):
    async def test_wire_code_lands_and_the_frame_contract_is_unchanged(self):
        async def failing_authz(token):
            raise topic_wire.SubscribeAuthFailed("invalid_token", retry_after_seconds=None)
        await self._subscribe(authorize_subscribe=failing_authz)
        by_error = self._terminal()["subscribe_auth_failed_by_error"]
        self.assertEqual(by_error["invalid_token"], 1)
        self.assertEqual(by_error["other"], 0)
        frame = self.ws.send_json.await_args_list[-1].args[0]
        self.assertEqual(frame["error"], "invalid_token")
        # 연결·registry 불변 계약
        self.assertEqual(topic_dispatcher.registry.get_subscriptions(self.ws), set())

    async def test_unknown_wire_code_records_other_before_the_frame_builder_rejects(self):
        """미지 코드는 생성·raise 는 되지만 **frame builder 가 ValueError 로 거부**한다
        (pre-S4 와 동일 wire). 기록은 frame 시도 **전**이라 other 로 남는다 — 어휘 사본이
        topic_wire 보다 뒤처진 미래의 방어이고, drift-lock 테스트가 동기화를 잠근다."""
        async def failing_authz(token):
            raise topic_wire.SubscribeAuthFailed("future_code", retry_after_seconds=None)
        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
            with self.assertRaises(ValueError):
                await self._subscribe(authorize_subscribe=failing_authz)
        snap = slm.subscribe_load_metrics()
        self.assertEqual(snap["terminal"]["subscribe_auth_failed_by_error"]["other"], 1)
        self.assertEqual(snap["metrics_internal_errors_total"], 0, "관측 fold 는 결함이 아니다")


if __name__ == "__main__":
    unittest.main()
