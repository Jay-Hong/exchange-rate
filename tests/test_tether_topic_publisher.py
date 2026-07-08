"""테더 탭 topic publish wrapper 단위 테스트 (PR Z-2b Stage 3 Level 1).

검증:
    - guard 동작 (FF=false / subscriber 0 → builder/publish 호출 0회)
    - 정상 path (FF=true + subscriber → builder + publish 1회)
    - builder 호출에 include_krx kwargs 부재 (ADR-038 D2 — KRX 독립 topic)
    - publish_topic sent=0 시 wrapper False 반환
    - wrapper가 builder 반환 payload를 변형하지 않음 (Codex 권고: 무변형 계약)
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, tether_topic_publisher, topic_dispatcher
from app.tether_topic_publisher import (
    TETHER_TOPIC,
    publish_tether_tab_snapshot,
    safe_publish_tether_tab_snapshot,
)


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

    # 4) builder 호출 인자 — include_krx kwargs 부재
    async def test_calls_builder_without_krx_kwargs(self):
        """ADR-038 D2 — builder 호출에 include_krx kwargs 자체가 없음 (KRX는 독립 topic)."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}) as mock_build, \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            await publish_tether_tab_snapshot(db)

        mock_build.assert_called_once()
        call_args = mock_build.call_args
        self.assertNotIn("include_krx", call_args.kwargs)
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


class TestSafePublishTetherTabSnapshot(unittest.IsolatedAsyncioTestCase):
    """safe_publish_tether_tab_snapshot — 예외 격리 wrapper (Level 2 hot path 호출용).

    핵심 계약:
      - 정상 path는 publish_tether_tab_snapshot 결과 그대로 반환
      - 어떤 예외 발생해도 호출자에 propagate 안 함, False 반환
      - 예외 시 logger.exception 호출 (운영 가시성)
    """

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_returns_inner_result_on_success(self):
        """정상 path — publish_tether_tab_snapshot의 True/False 그대로 반환."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            result = await safe_publish_tether_tab_snapshot(db)
        self.assertTrue(result)

    async def test_returns_false_when_inner_returns_false(self):
        """guard 차단 시 publish_tether_tab_snapshot이 False — safe wrapper도 False."""
        db = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            result = await safe_publish_tether_tab_snapshot(db)
        self.assertFalse(result)

    async def test_isolates_exception_from_caller(self):
        """build/publish 단계에서 예외 발생 → False 반환 + logger.exception, 호출자 영향 X."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        # builder가 예외 발생 시뮬레이션
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          side_effect=RuntimeError("DB connection lost")), \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()), \
             self.assertLogs("exchange_rate.tether_topic_publisher", level="ERROR") as cm:
            # 호출자가 try/except 안 써도 예외 propagate 안 됨 — 핵심 계약
            result = await safe_publish_tether_tab_snapshot(db)

        self.assertFalse(result)
        self.assertTrue(any("실패" in m for m in cm.output))

    async def test_isolates_publish_topic_exception(self):
        """publish_topic 단계 예외도 격리."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(side_effect=RuntimeError("send failed"))), \
             self.assertLogs("exchange_rate.tether_topic_publisher", level="ERROR"):
            result = await safe_publish_tether_tab_snapshot(db)

        self.assertFalse(result)


class TestTopicTelemetry(unittest.IsolatedAsyncioTestCase):
    """Redis-backed telemetry — best-effort 기록 + circuit_breaker 오염 회피.

    핵심 계약:
      - redis_cache.client raw 사용 (circuit wrapper 우회)
      - circuit.can_attempt() 체크만, record_failure() 호출 X
      - Redis 미가용 / circuit open / 예외 → broadcast 영향 X
      - last_error 길이 제한 (500자)
      - reset은 DEL key (HDEL 아님, 필드 추가 자동 적용)
    """

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    def _make_redis_mock(self, can_attempt: bool = True):
        """redis_cache.client + circuit mock helper."""
        client = MagicMock()
        client.hincrby = AsyncMock()
        client.hset = AsyncMock()
        client.hgetall = AsyncMock(return_value={})
        client.delete = AsyncMock()
        circuit_mock = MagicMock()
        circuit_mock.can_attempt = AsyncMock(return_value=can_attempt)
        # record_failure은 telemetry가 호출하지 않아야 — mock으로 호출 검증
        circuit_mock.record_failure = AsyncMock()
        return client, circuit_mock

    # 1) FF=false → hook_called + skipped_disabled
    async def test_ff_false_records_skipped_disabled(self):
        client, circuit = self._make_redis_mock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock:
            redis_mock.client = client
            redis_mock.circuit = circuit
            await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())

        # hook_called +1 (safe wrapper 진입), skipped_disabled +1
        increments = [c.args[1] for c in client.hincrby.call_args_list]
        self.assertIn("hook_called", increments)
        self.assertIn("skipped_disabled", increments)
        # circuit.record_failure 호출되면 안 됨 (telemetry 격리)
        circuit.record_failure.assert_not_called()

    # 2) subscriber 0 → skipped_no_subscribers
    async def test_no_subscribers_records_skipped_no_subscribers(self):
        client, circuit = self._make_redis_mock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock:
            redis_mock.client = client
            redis_mock.circuit = circuit
            await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())
        increments = [c.args[1] for c in client.hincrby.call_args_list]
        self.assertIn("hook_called", increments)
        self.assertIn("skipped_no_subscribers", increments)

    # 3) sent > 0 → built + publish_called + publish_sent_total
    async def test_send_success_records_built_and_publish_total(self):
        client, circuit = self._make_redis_mock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [tether_topic_publisher.TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock, \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=2)):  # 2명 send 성공
            redis_mock.client = client
            redis_mock.circuit = circuit
            result = await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())
        self.assertTrue(result)

        # built / publish_called counter + publish_sent_total += 2
        increments = [(c.args[1], c.args[2] if len(c.args) > 2 else 1)
                       for c in client.hincrby.call_args_list]
        increment_fields = [i[0] for i in increments]
        self.assertIn("built", increment_fields)
        self.assertIn("publish_called", increment_fields)
        # publish_sent_total은 sent=2로 증가
        sent_increments = [i[1] for i in increments if i[0] == "publish_sent_total"]
        self.assertEqual(sum(sent_increments), 2)

    # 4) sent == 0 → publish_zero
    async def test_send_zero_records_publish_zero(self):
        client, circuit = self._make_redis_mock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [tether_topic_publisher.TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock, \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=0)):  # 모든 send 실패
            redis_mock.client = client
            redis_mock.circuit = circuit
            result = await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())
        self.assertFalse(result)

        increments = [c.args[1] for c in client.hincrby.call_args_list]
        self.assertIn("publish_zero", increments)
        self.assertIn("publish_called", increments)

    # 5) exception → error + last_error 기록
    async def test_exception_records_error_and_last_error(self):
        client, circuit = self._make_redis_mock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [tether_topic_publisher.TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock, \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          side_effect=RuntimeError("DB connection lost")), \
             self.assertLogs("exchange_rate.tether_topic_publisher", level="ERROR"):
            redis_mock.client = client
            redis_mock.circuit = circuit
            result = await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())
        self.assertFalse(result)

        increments = [c.args[1] for c in client.hincrby.call_args_list]
        self.assertIn("error", increments)
        # last_error HSET 호출 검증 + 길이 500 이하
        last_error_calls = [
            c for c in client.hset.call_args_list
            if len(c.args) >= 2 and c.args[1] == "last_error"
        ]
        self.assertTrue(len(last_error_calls) >= 1)
        last_err_value = last_error_calls[0].args[2]
        self.assertIn("RuntimeError", last_err_value)
        self.assertIn("DB connection lost", last_err_value)
        self.assertLessEqual(len(last_err_value), 500)

    # 6) Redis 미가용 (client=None) → 예외 전파 X
    async def test_redis_unavailable_no_propagation(self):
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [tether_topic_publisher.TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock, \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            redis_mock.client = None  # Redis 미가용
            # 예외 발생 안 해야 함, 정상 결과 반환
            result = await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())
        self.assertTrue(result)  # publish는 성공, telemetry만 skip

    # 7) circuit open → 예외 전파 X + record_failure 호출 X
    async def test_circuit_open_no_propagation(self):
        client, circuit = self._make_redis_mock(can_attempt=False)  # circuit open
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [tether_topic_publisher.TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock, \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            redis_mock.client = client
            redis_mock.circuit = circuit
            result = await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())
        self.assertTrue(result)
        # circuit open이라 hincrby 호출 0회 (telemetry skip)
        client.hincrby.assert_not_called()
        # record_failure 절대 호출 X (Codex 권고)
        circuit.record_failure.assert_not_called()

    # 8) Redis hincrby 예외 → broadcast 영향 X + record_failure 호출 X
    async def test_redis_exception_no_propagation_no_record_failure(self):
        client, circuit = self._make_redis_mock()
        client.hincrby.side_effect = ConnectionError("Redis disconnected")
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, [tether_topic_publisher.TETHER_TOPIC])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock, \
             patch.object(tether_topic_publisher, "load_and_build_tether_tab_payload",
                          return_value={"type": "snapshot", "version": 1, "data": {}}), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            redis_mock.client = client
            redis_mock.circuit = circuit
            # telemetry 실패해도 broadcast 영향 X
            result = await tether_topic_publisher.safe_publish_tether_tab_snapshot(MagicMock())
        # publish는 성공했으나 telemetry 일부 실패 — wrapper는 정상 흐름
        self.assertTrue(result)
        # circuit.record_failure 호출 X (telemetry 격리, Codex 권고)
        circuit.record_failure.assert_not_called()

    # 9) reset → DEL key (HDEL 아님)
    async def test_reset_uses_del_not_hdel(self):
        client, circuit = self._make_redis_mock()
        with patch("app.tether_topic_publisher.redis_cache") as redis_mock:
            redis_mock.client = client
            redis_mock.circuit = circuit
            success = await tether_topic_publisher.reset_topic_telemetry()
        self.assertTrue(success)
        # DEL topic:tether:stats 호출 (HDEL 아님)
        client.delete.assert_called_once_with("topic:tether:stats")

    # 10) get_topic_telemetry — Redis 값 + base 필드 통합
    async def test_get_telemetry_returns_combined_shape(self):
        client, circuit = self._make_redis_mock()
        client.hgetall = AsyncMock(return_value={
            b"hook_called": b"123",
            b"skipped_disabled": b"100",
            b"built": b"23",
            b"publish_called": b"23",
            b"publish_sent_total": b"50",
            b"last_result": b"sent",
            b"last_at_kst": b"2026-05-10T20:00:00+09:00",
        })
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.tether_topic_publisher.redis_cache") as redis_mock:
            redis_mock.client = client
            redis_mock.circuit = circuit
            telemetry = await tether_topic_publisher.get_topic_telemetry()

        # base 필드
        self.assertEqual(telemetry["enabled"], True)
        self.assertEqual(telemetry["topic"], tether_topic_publisher.TETHER_TOPIC)
        # Redis 값
        self.assertEqual(telemetry["hook_called"], 123)
        self.assertEqual(telemetry["skipped_disabled"], 100)
        self.assertEqual(telemetry["built"], 23)
        self.assertEqual(telemetry["publish_called"], 23)
        self.assertEqual(telemetry["publish_sent_total"], 50)
        self.assertEqual(telemetry["last_result"], "sent")
        # 미존재 필드는 base default
        self.assertEqual(telemetry["error"], 0)


# (ADR-038 Decision 2) TestIncludeKrxParameter 클래스 제거 — usdt:krw의 include_krx
# wiring 계약 소멸 (KRX는 krx:usd-krw-futures 독립 topic, tests/test_krx_topic_publisher.py).


if __name__ == "__main__":
    unittest.main(verbosity=2)
