"""FX topic publish wrapper 단위 테스트 (PR Z-2c Step 2).

검증:
    - guard 동작 (FF=false / TOPIC_DISPATCHER=false / subscriber 0)
    - 정상 path (FF=true + subscriber → builder + publish 1회)
    - payload에 topic 필드 inject (publisher 책임)
    - 3 topic 독립 처리 (1개 실패 시 다른 topic 계속)
    - per-topic subscriber_count guard (multi-topic 환경 정확성)
    - per-topic telemetry key 분리 (`topic:fx:<asset>:stats`)
    - safe wrapper 예외 격리
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import atomic_cutover_runtime, config, fx_topic_publisher, topic_dispatcher
from app.atomic_cutover import PublisherGateDisposition
from app.fx_topic_publisher import (
    FX_TOPICS,
    FX_TOPIC_ASSETS,
    _publish_fx_snapshot,
    _read_fx_publisher_gate_disposition,
    _telemetry_key,
    safe_publish_all_fx_snapshots,
)


# ---------------------------------------------------------------------------
# Guard — _publish_fx_snapshot
# ---------------------------------------------------------------------------

class TestPublishFxSnapshotGuards(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_returns_false_when_fx_topic_disabled(self):
        db = MagicMock()
        with patch.object(config, "FX_TOPIC_ENABLED", False), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    async def test_returns_false_when_topic_dispatcher_disabled(self):
        db = MagicMock()
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    async def test_returns_false_when_no_subscribers_for_topic(self):
        db = MagicMock()
        # registry 비어 있음
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        mock_build.assert_not_called()
        mock_pub.assert_not_called()

    async def test_other_topic_subscriber_does_not_trigger_this_topic(self):
        """multi-topic 정확성: fx:jpy-krw 구독자 있어도 fx:usd-krw publisher는 skip."""
        db = MagicMock()
        ws = MagicMock()
        # 다른 topic만 구독
        topic_dispatcher.registry.register(ws, ["fx:jpy-krw"])

        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)
        # subscribed_connection_count=1이지만 fx:usd-krw subscriber=0 → skip
        mock_build.assert_not_called()
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# Normal path — _publish_fx_snapshot
# ---------------------------------------------------------------------------

class TestPublishFxSnapshotNormal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_publishes_and_returns_true_when_send_succeeds(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload) as mock_build, \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            result = await _publish_fx_snapshot(db, "usd-krw")

        self.assertTrue(result)
        mock_build.assert_called_once_with(db, "usd-krw")
        mock_pub.assert_awaited_once()

    async def test_payload_topic_field_injected_by_publisher(self):
        """builder는 topic-agnostic, publisher가 payload['topic'] inject."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:jpy-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            await _publish_fx_snapshot(db, "jpy-krw")

        sent_payload = mock_pub.await_args.args[1]
        self.assertEqual(sent_payload["topic"], "fx:jpy-krw")

    async def test_publish_zero_returns_false(self):
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=0)):
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# safe_publish_all_fx_snapshots — 3 topic 일괄 + 예외 격리
# ---------------------------------------------------------------------------

class TestSafePublishAllFxSnapshots(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_all_three_assets_processed(self):
        db = MagicMock()
        # 3 topic 모두 구독자 있음
        for asset, topic in FX_TOPICS.items():
            ws = MagicMock(name=f"ws_{asset}")
            topic_dispatcher.registry.register(ws, [topic])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload) as mock_build, \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            results = await safe_publish_all_fx_snapshots(db)

        self.assertEqual(set(results.keys()), set(FX_TOPIC_ASSETS))
        self.assertTrue(all(results.values()))
        # builder는 3번 호출 (asset별)
        self.assertEqual(mock_build.call_count, 3)
        self.assertEqual(mock_pub.await_count, 3)

    async def test_one_asset_failure_does_not_block_others(self):
        """1개 topic의 builder 예외가 나머지 publish를 막지 않음 (격리).

        logger.exception을 patch 캡처 — 회귀 출력에 traceback 노출 차단 +
        예외 격리 시 실제로 로그가 남는다는 contract 명시 검증.
        """
        db = MagicMock()
        # 3 topic 모두 구독자 있음
        for asset, topic in FX_TOPICS.items():
            ws = MagicMock(name=f"ws_{asset}")
            topic_dispatcher.registry.register(ws, [topic])

        def builder_side_effect(db, asset):
            if asset == "jpy-krw":
                raise RuntimeError("simulated builder failure")
            return {"type": "snapshot", "version": 1, "data": {"banks": []}}

        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          side_effect=builder_side_effect), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)), \
             patch.object(fx_topic_publisher.logger, "exception") as mock_log:
            results = await safe_publish_all_fx_snapshots(db)

        self.assertTrue(results["usd-krw"])
        self.assertFalse(results["jpy-krw"])  # 예외 격리 — False
        self.assertTrue(results["eur-krw"])
        # logger.exception이 정확히 1회 호출 (jpy-krw 격리)
        self.assertEqual(mock_log.call_count, 1)

    async def test_all_disabled_returns_all_false(self):
        db = MagicMock()
        with patch.object(config, "FX_TOPIC_ENABLED", False), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload") as mock_build, \
             patch.object(topic_dispatcher, "publish_topic", new=AsyncMock()) as mock_pub:
            results = await safe_publish_all_fx_snapshots(db)

        self.assertEqual(results, {"usd-krw": False, "jpy-krw": False, "eur-krw": False})
        mock_build.assert_not_called()
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# Telemetry key separation — per topic Redis key
# ---------------------------------------------------------------------------

class TestTelemetryKeySeparation(unittest.TestCase):

    def test_telemetry_key_per_asset(self):
        self.assertEqual(_telemetry_key("usd-krw"), "topic:fx:usd-krw:stats")
        self.assertEqual(_telemetry_key("jpy-krw"), "topic:fx:jpy-krw:stats")
        self.assertEqual(_telemetry_key("eur-krw"), "topic:fx:eur-krw:stats")

    def test_telemetry_key_differs_from_tether(self):
        """tether와 namespace 분리."""
        for asset in FX_TOPIC_ASSETS:
            self.assertNotEqual(_telemetry_key(asset), "topic:tether:stats")

    def test_fx_topics_mapping_derived_from_assets(self):
        """FX_TOPICS는 FX_TOPIC_ASSETS에서 파생 — 변경 시 자동 동기화."""
        self.assertEqual(
            FX_TOPICS,
            {"usd-krw": "fx:usd-krw", "jpy-krw": "fx:jpy-krw", "eur-krw": "fx:eur-krw"},
        )


# ---------------------------------------------------------------------------
# Telemetry recording — best-effort, no circuit pollution
# ---------------------------------------------------------------------------

class TestTelemetryBestEffort(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_publish_succeeds_when_redis_unavailable(self):
        """Redis client=None이면 telemetry skip — publish 정상 동작."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache, \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            mock_cache.client = None
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertTrue(result)

    async def test_publish_succeeds_when_circuit_open(self):
        """circuit.can_attempt()=False여도 publish 정상 — telemetry만 skip."""
        db = MagicMock()
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])

        fake_payload = {"type": "snapshot", "version": 1, "data": {"banks": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache, \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          return_value=fake_payload), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)):
            mock_cache.client = MagicMock()
            mock_cache.circuit = MagicMock()
            mock_cache.circuit.can_attempt = AsyncMock(return_value=False)
            result = await _publish_fx_snapshot(db, "usd-krw")
        self.assertTrue(result)
        # circuit.record_failure는 호출되면 안 됨 (오염 차단)
        if hasattr(mock_cache.circuit, "record_failure"):
            mock_cache.circuit.record_failure.assert_not_called()


# ---------------------------------------------------------------------------
# get_fx_topic_telemetry — C1 trigger_* 필드 surface (Item 1, admin observability)
# ---------------------------------------------------------------------------

class TestGetFxTopicTelemetryTriggerFields(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    def test_trigger_counter_fields_match_redis_persisted_set(self):
        """drift guard: publisher surface == fx_topic_trigger의 Redis-영속 counter
        (= TRIGGER_COUNTER_FIELDS − process-local). no_loop은 loop 부재라 Redis 미기록 → 제외."""
        from app import fx_topic_trigger
        self.assertEqual(
            set(fx_topic_publisher._TRIGGER_COUNTER_FIELDS),
            set(fx_topic_trigger.TRIGGER_COUNTER_FIELDS)
            - fx_topic_publisher._TRIGGER_PROCESS_LOCAL_FIELDS,
        )
        self.assertNotIn("no_loop", fx_topic_publisher._TRIGGER_COUNTER_FIELDS)

    def _mock_cache(self, mock_cache, raw):
        mock_cache.client = MagicMock()
        mock_cache.circuit = MagicMock()
        mock_cache.circuit.can_attempt = AsyncMock(return_value=True)
        mock_cache.client.hgetall = AsyncMock(return_value=raw)

    async def test_trigger_fields_default_zero_none_when_absent(self):
        """hash에 trigger_ 필드 없으면 counter=0 / last_=None 계약."""
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            self._mock_cache(mock_cache, {})  # 빈 hash
            out = await fx_topic_publisher.get_fx_topic_telemetry()
        snap = out["fx:usd-krw"]
        for c in fx_topic_publisher._TRIGGER_COUNTER_FIELDS:
            self.assertEqual(snap[f"trigger_{c}"], 0)
        for lf in fx_topic_publisher._TRIGGER_LAST_FIELDS:
            self.assertIsNone(snap[f"trigger_{lf}"])
        # no_loop은 Redis 미기록(process-local) → surface 안 함 (항상 0 오해 방지)
        self.assertNotIn("trigger_no_loop", snap)

    async def test_trigger_fields_surfaced_and_separated_from_publisher(self):
        """hash trigger_ 값 반영 + publisher 동명 필드(error/publish_called)와 충돌 분리."""
        raw = {
            "trigger_request": "42",
            "trigger_flush_direct": "7",
            "trigger_publish_success": "3",
            "trigger_error": "0",
            "trigger_tether_route_shadow": "12",
            "trigger_last_result": "publish_success",
            "trigger_last_reason": "bank_change",
            "publish_called": "100",  # publisher 측
            "error": "5",             # publisher 측
        }
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            self._mock_cache(mock_cache, raw)
            out = await fx_topic_publisher.get_fx_topic_telemetry()
        snap = out["fx:usd-krw"]
        self.assertEqual(snap["trigger_request"], 42)
        self.assertEqual(snap["trigger_flush_direct"], 7)
        self.assertEqual(snap["trigger_publish_success"], 3)
        self.assertEqual(snap["trigger_tether_route_shadow"], 12)
        self.assertEqual(snap["trigger_last_result"], "publish_success")
        self.assertEqual(snap["trigger_last_reason"], "bank_change")
        # 충돌 분리 — publisher 측과 trigger 측이 별개 키
        self.assertEqual(snap["error"], 5)
        self.assertEqual(snap["trigger_error"], 0)
        self.assertEqual(snap["publish_called"], 100)
        self.assertEqual(snap["trigger_publish_called"], 0)  # hash에 없음 → 0

    async def test_existing_publisher_fields_preserved(self):
        """회귀: 기존 publisher-side/메타 필드 그대로 노출 (호출자 영향 0)."""
        raw = {"publish_sent_total": "9", "hook_called": "11", "last_result": "sent"}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            self._mock_cache(mock_cache, raw)
            out = await fx_topic_publisher.get_fx_topic_telemetry()
        snap = out["fx:usd-krw"]
        for f in ("enabled", "fx_topic_enabled", "topic_dispatcher_enabled",
                  "topic", "asset", "subscriber_count",
                  "hook_called", "publish_sent_total", "publish_zero", "error",
                  "last_result", "last_at_kst", "last_error"):
            self.assertIn(f, snap)
        self.assertEqual(snap["publish_sent_total"], 9)
        self.assertEqual(snap["hook_called"], 11)
        self.assertEqual(snap["last_result"], "sent")


# ---------------------------------------------------------------------------
# item 2: direct↔legacy wrapper payload equality (공통 core regression lock)
# ---------------------------------------------------------------------------

class TestDirectLegacyPayloadEquality(unittest.IsolatedAsyncioTestCase):
    """safe_publish_fx_snapshot(direct flush) vs safe_publish_all_fx_snapshots(legacy hook)가
    동일 asset에 동일 topic+payload를 publish_topic으로 전달함을 잠근다. 둘 다 공통
    _publish_fx_snapshot→load_and_build_fx_topic_payload core라 by-construction 동치지만,
    wrapper 분기(legacy asset 누락 / 다른 builder·topic 매핑 등)를 회귀로 catch."""

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
        for asset in fx_topic_publisher.FX_TOPIC_ASSETS:
            topic_dispatcher.registry.register(MagicMock(), [f"fx:{asset}"])

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_direct_and_legacy_emit_equal_payload_per_asset(self):
        from contextlib import contextmanager

        def fake_build(db, asset):
            # asset별 결정적 payload (db 무관 = 입력 고정 효과). 매 호출 새 dict
            # (publisher가 payload["topic"] 주입 mutate해도 경로 간 독립).
            return {
                "type": "snapshot", "version": 1,
                "data": {
                    "banks": [{"source": "kb", "asset": asset, "rate": 1.0, "timestamp": "t"}],
                    "reference": {"source": "investing", "asset": asset, "rate": 2.0, "timestamp": "t"},
                },
            }

        @contextmanager
        def fake_db_context():
            yield MagicMock()

        legacy_db = MagicMock()
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload",
                          side_effect=fake_build), \
             patch("app.database.get_db_context", fake_db_context), \
             patch.object(topic_dispatcher, "publish_topic",
                          new=AsyncMock(return_value=1)) as mock_pub:
            # direct: asset별 단건 호출 (자체 get_db_context)
            direct = {}
            for asset in fx_topic_publisher.FX_TOPIC_ASSETS:
                mock_pub.reset_mock()
                d_ok = await fx_topic_publisher.safe_publish_fx_snapshot(asset)
                self.assertTrue(d_ok)  # wrapper 성공 반환
                self.assertEqual(mock_pub.call_count, 1)
                direct[asset] = mock_pub.call_args.args  # (topic, payload)
            # legacy: 일괄 호출 (caller db)
            mock_pub.reset_mock()
            legacy_results = await fx_topic_publisher.safe_publish_all_fx_snapshots(legacy_db)
            # 각 asset 정확히 1회 발행 — dict 변환이 중복 발행을 가리는 것 방지 (Codex)
            self.assertEqual(mock_pub.call_count, len(fx_topic_publisher.FX_TOPIC_ASSETS))
            self.assertTrue(all(legacy_results.values()))  # 모든 asset 성공 반환
            legacy = {c.args[0]: c.args[1] for c in mock_pub.call_args_list}

        # legacy가 3 asset 모두 발행 + direct와 topic·payload deep-equal
        self.assertEqual(
            set(legacy.keys()),
            {f"fx:{a}" for a in fx_topic_publisher.FX_TOPIC_ASSETS},
        )
        for asset in fx_topic_publisher.FX_TOPIC_ASSETS:
            topic = f"fx:{asset}"
            d_topic, d_payload = direct[asset]
            self.assertEqual(d_topic, topic)
            self.assertEqual(d_payload, legacy[topic])


# ---------------------------------------------------------------------------
# P1b C6-7 — cutover gate SHADOW (dry-run observe, characterization-locked behavior-change-0)
# ---------------------------------------------------------------------------

class _FakeSnap:
    def __init__(self, gate_open):
        self.publisher_gate_open = gate_open


class TestC6_7CutoverGateShadow(unittest.IsolatedAsyncioTestCase):
    """gate는 dry-run shadow: PASS_THROUGH/WOULD_BLOCK 둘 다 legacy publish 진행(real-block 없음).
    flag-off=zero work, fail-OPEN, pre-publish placement(early-return/builder-raise는 gate 미도달).

    **single-core equivalence**: 3 entry(safe_publish_fx_snapshot·safe_publish_all_fx_snapshots·trigger)가
    전부 _publish_fx_snapshot 경유 → gate는 그 single core에 1곳. _publish_fx_snapshot characterization이
    by construction 모든 entry를 cover."""

    async def asyncSetUp(self):
        self._orig = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._orig

    def _payload(self):
        return {"type": "snapshot", "version": 1, "data": {"banks": []}}

    def _publishable_ctx(self, *, flag, snap_gate_open=True, snap_side_effect=None, sent=1):
        # FF on + 구독자 → publishable. gate flag + snapshot 제어.
        ws = MagicMock()
        topic_dispatcher.registry.register(ws, ["fx:usd-krw"])
        snap_mock = (MagicMock(side_effect=snap_side_effect) if snap_side_effect
                     else MagicMock(return_value=_FakeSnap(snap_gate_open)))
        return snap_mock, (
            patch.object(config, "FX_CUTOVER_GATE_OBSERVE_ENABLED", flag),
            patch.object(config, "FX_TOPIC_ENABLED", True),
            patch.object(config, "TOPIC_DISPATCHER_ENABLED", True),
            patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload", return_value=self._payload()),
            patch.object(topic_dispatcher, "publish_topic", new=AsyncMock(return_value=sent)),
            patch.object(atomic_cutover_runtime, "snapshot", snap_mock),
        )

    async def test_flag_off_gate_not_invoked_legacy_identical(self):
        snap, ctx = self._publishable_ctx(flag=False)
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], \
             patch.object(fx_topic_publisher, "_record_gate_shadow_event", new=AsyncMock()) as shadow:
            result = await _publish_fx_snapshot(MagicMock(), "usd-krw")
        self.assertTrue(result)                # legacy 발행 그대로
        snap.assert_not_called()               # flag-off → snapshot 미호출 (zero hot-path)
        shadow.assert_not_called()

    async def test_flag_on_pass_through_publishes_and_records(self):
        snap, ctx = self._publishable_ctx(flag=True, snap_gate_open=True)
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], \
             patch.object(fx_topic_publisher, "_record_gate_shadow_event", new=AsyncMock()) as shadow:
            result = await _publish_fx_snapshot(MagicMock(), "usd-krw")
        self.assertTrue(result)                # legacy 발행
        shadow.assert_awaited_once()
        self.assertIs(shadow.await_args.args[1], PublisherGateDisposition.PASS_THROUGH)

    async def test_flag_on_would_block_STILL_publishes(self):
        # behavior-change-0 핵심: WOULD_BLOCK이어도 legacy publish 진행(real-block 없음)
        snap, ctx = self._publishable_ctx(flag=True, snap_gate_open=False)
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4] as pub, ctx[5], \
             patch.object(fx_topic_publisher, "_record_gate_shadow_event", new=AsyncMock()) as shadow:
            result = await _publish_fx_snapshot(MagicMock(), "usd-krw")
        self.assertTrue(result)                # 차단 안 됨
        pub.assert_awaited_once()              # publish_topic 실제 호출됨
        self.assertIs(shadow.await_args.args[1], PublisherGateDisposition.WOULD_BLOCK_DRY_RUN)

    async def test_gate_read_fail_open_on_snapshot_exception(self):
        # snapshot() raise → _read_..._disposition fail-OPEN PASS_THROUGH → legacy publish 진행
        snap, ctx = self._publishable_ctx(flag=True, snap_side_effect=RuntimeError("runtime down"))
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4] as pub, ctx[5]:
            result = await _publish_fx_snapshot(MagicMock(), "usd-krw")
        self.assertTrue(result)
        pub.assert_awaited_once()

    def test_read_disposition_fail_open_unit(self):
        with patch.object(atomic_cutover_runtime, "snapshot", side_effect=RuntimeError("x")):
            self.assertIs(_read_fx_publisher_gate_disposition(), PublisherGateDisposition.PASS_THROUGH)
        with patch.object(atomic_cutover_runtime, "snapshot", return_value=_FakeSnap(False)):
            self.assertIs(_read_fx_publisher_gate_disposition(), PublisherGateDisposition.WOULD_BLOCK_DRY_RUN)

    async def test_gate_not_invoked_on_ff_off(self):
        # pre-publish placement: FF-off early-return은 gate 도달 전 → snapshot 미호출
        snap = MagicMock(return_value=_FakeSnap(True))
        with patch.object(config, "FX_CUTOVER_GATE_OBSERVE_ENABLED", True), \
             patch.object(config, "FX_TOPIC_ENABLED", False), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(atomic_cutover_runtime, "snapshot", snap):
            result = await _publish_fx_snapshot(MagicMock(), "usd-krw")
        self.assertFalse(result)
        snap.assert_not_called()

    async def test_gate_not_invoked_on_ff2_off(self):
        # pre-publish: TOPIC_DISPATCHER_ENABLED=false 두 번째 early-return도 gate 미도달
        snap = MagicMock(return_value=_FakeSnap(True))
        with patch.object(config, "FX_CUTOVER_GATE_OBSERVE_ENABLED", True), \
             patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), \
             patch.object(atomic_cutover_runtime, "snapshot", snap):
            result = await _publish_fx_snapshot(MagicMock(), "usd-krw")
        self.assertFalse(result)
        snap.assert_not_called()

    async def test_gate_not_invoked_on_no_subscribers(self):
        snap = MagicMock(return_value=_FakeSnap(True))
        with patch.object(config, "FX_CUTOVER_GATE_OBSERVE_ENABLED", True), \
             patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(atomic_cutover_runtime, "snapshot", snap):
            result = await _publish_fx_snapshot(MagicMock(), "usd-krw")  # 구독자 0
        self.assertFalse(result)
        snap.assert_not_called()

    async def test_gate_not_invoked_on_builder_raise(self):
        snap = MagicMock(return_value=_FakeSnap(True))
        ws = MagicMock(); topic_dispatcher.registry.register(ws, ["fx:usd-krw"])
        with patch.object(config, "FX_CUTOVER_GATE_OBSERVE_ENABLED", True), \
             patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(fx_topic_publisher, "load_and_build_fx_topic_payload", side_effect=RuntimeError("build")), \
             patch.object(atomic_cutover_runtime, "snapshot", snap):
            with self.assertRaises(RuntimeError):
                await _publish_fx_snapshot(MagicMock(), "usd-krw")  # legacy: builder 예외 전파
        snap.assert_not_called()                # builder(line 194) 후 gate라 미도달

    async def test_record_topic_event_sequence_unchanged_by_gate(self):
        # gate(flag-on PASS_THROUGH)가 legacy _record_topic_event result 시퀀스를 perturb 안 함
        results = []
        async def _capture(asset, *, result, **kw):
            results.append(result)
        snap, ctx = self._publishable_ctx(flag=True, snap_gate_open=True, sent=1)
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], \
             patch.object(fx_topic_publisher, "_record_topic_event", new=_capture):
            await _publish_fx_snapshot(MagicMock(), "usd-krw")
        # legacy sent>0 시퀀스: built → publish_called (gate result는 _record_topic_event에 없음)
        self.assertEqual(results, ["built", "publish_called"])

    async def test_shadow_helper_writes_only_gate_fields_would_block(self):
        # MEDIUM(Workflow): disjoint-field invariant **실행 검증** — _record_gate_shadow_event가 gate_* 만
        # 기록하고 legacy last_result/last_at_kst/last_error 무접촉(_record_topic_event corruption 회피의 핵심).
        with patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            mock_cache.client = MagicMock()
            mock_cache.client.hincrby = AsyncMock()
            mock_cache.client.hset = AsyncMock()
            mock_cache.circuit = MagicMock()
            mock_cache.circuit.can_attempt = AsyncMock(return_value=True)
            await fx_topic_publisher._record_gate_shadow_event(
                "usd-krw", PublisherGateDisposition.WOULD_BLOCK_DRY_RUN)
        key = _telemetry_key("usd-krw")
        mock_cache.client.hincrby.assert_awaited_once_with(key, "gate_would_block_dry_run", 1)
        hset_fields = [c.args[1] for c in mock_cache.client.hset.await_args_list]
        self.assertIn("gate_last_disposition", hset_fields)
        self.assertIn("gate_last_at_kst", hset_fields)
        # CRITICAL: legacy ordering field 무접촉
        self.assertNotIn("last_result", hset_fields)
        self.assertNotIn("last_at_kst", hset_fields)
        self.assertNotIn("last_error", hset_fields)
        mock_cache.circuit.record_failure.assert_not_called()  # circuit 오염 차단(_record_topic_event 정합)

    async def test_shadow_helper_pass_through_no_counter(self):
        with patch("app.fx_topic_publisher.redis_cache") as mock_cache:
            mock_cache.client = MagicMock()
            mock_cache.client.hincrby = AsyncMock()
            mock_cache.client.hset = AsyncMock()
            mock_cache.circuit = MagicMock()
            mock_cache.circuit.can_attempt = AsyncMock(return_value=True)
            await fx_topic_publisher._record_gate_shadow_event(
                "usd-krw", PublisherGateDisposition.PASS_THROUGH)
        mock_cache.client.hincrby.assert_not_called()  # PASS_THROUGH → would_block counter 미증가
        hset_fields = [c.args[1] for c in mock_cache.client.hset.await_args_list]
        self.assertEqual(set(hset_fields), {"gate_last_disposition", "gate_last_at_kst"})
        self.assertNotIn("last_result", hset_fields)


# gate-shell은 atomic_cutover(enum) + atomic_cutover_runtime(snapshot)만 sanctioned. coordinator/adapter
# (atomic_coordinator·atomic_fx_live) import + activation(publish_asset/refresh_from_db/AtomicFxCoordinator)
# call은 FLIP 영역. AST 기반(주석 제외) — FLIP seam 주석의 AtomicFxCoordinator 언급에 false-trip 안 함.
_C6_7_FORBIDDEN_MODULES = frozenset({"atomic_fx_live", "atomic_coordinator"})
_C6_7_FORBIDDEN_CALLS = frozenset({"publish_asset", "refresh_from_db", "AtomicFxCoordinator"})


def _scan_fx_publisher_scope_violations(src):
    """C6-7 scope 위반(coordinator/adapter import + activation call) 목록 (real test + self-arm 공유)."""
    hits = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            tail = (node.module or "").split(".")[-1]
            if tail in _C6_7_FORBIDDEN_MODULES:
                hits.append(f"from {node.module} import")
            if node.module == "app" and any(a.name in _C6_7_FORBIDDEN_MODULES for a in node.names):
                hits.append("from app import <coordinator/adapter>")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] in _C6_7_FORBIDDEN_MODULES:
                    hits.append(f"import {a.name}")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _C6_7_FORBIDDEN_CALLS:
                hits.append(f"{name}()")
    return hits


class TestC6_7Dormancy(unittest.TestCase):
    """C6-7 scope: gate-shell only — coordinator/activation/refresh는 FLIP. fx_topic_publisher가
    coordinator/adapter import·publish_asset·refresh_from_db·AtomicFxCoordinator call 0 (AST, 주석 제외)."""

    def test_publisher_no_coordinator_or_activation(self):
        src = pathlib.Path(fx_topic_publisher.__file__).read_text(encoding="utf-8")
        self.assertEqual(_scan_fx_publisher_scope_violations(src), [],
                         "fx_topic_publisher가 coordinator/activation 참조 — C6-7 scope 위반(FLIP 영역)")

    def test_trip_wire_self_arms(self):
        # real test와 동일 predicate를 forbidden form 전수 plant로 검증 (vacuous pass 방지)
        for planted in (
            "from app.atomic_fx_live import X\n",
            "from app.atomic_coordinator import AtomicFxCoordinator\n",  # import-without-call도 검출
            "import app.atomic_fx_live\n",
            "from app import atomic_coordinator\n",
            "def f():\n    publish_asset()\n",
            "def f():\n    AtomicFxCoordinator()\n",
            "def f():\n    refresh_from_db()\n",
        ):
            self.assertTrue(_scan_fx_publisher_scope_violations(planted), f"planted 미검출: {planted!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
