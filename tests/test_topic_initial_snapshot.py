"""topic snapshot-on-subscribe 단위 테스트 (realtime topic release readiness 1st slice).

app.topic_initial_snapshot 검증:
  - _build_snapshot_sync: topic→builder 매핑 / enable gate(FX_TOPIC_ENABLED) / topic 필드 inject /
    미지원 topic None
  - send_initial_snapshots: per-topic 격리(build 실패 continue) / send 실패 시 registry 정리+중단 /
    요청 내 dedupe / 빈 payload도 전송 / 미지원 skip

builder/SessionLocal은 patch로 격리(실 DB 접근 0 — deterministic). 실 wire e2e(TestClient ws)는
별도 smoke harness 슬라이스.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, topic_dispatcher
from app.topic_initial_snapshot import _build_snapshot_sync, send_initial_snapshots


class TestBuildSnapshotSync(unittest.TestCase):
    """_build_snapshot_sync — to_thread 내부 sync 빌더 (SessionLocal/builder patch)."""

    def test_fx_topic_builds_with_topic_field(self):
        built = {"type": "snapshot", "version": 1, "data": {"rates": []}}
        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch("app.database.SessionLocal", MagicMock()), \
             patch(
                 "app.fx_topic_payload.load_and_build_fx_topic_payload",
                 return_value=dict(built),
             ) as mock_build:
            payload = _build_snapshot_sync("fx:usd-krw")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["topic"], "fx:usd-krw")
        self.assertEqual(payload["type"], "snapshot")
        # topic→asset 파생 정확성: builder가 "usd-krw"로 호출됨
        mock_build.assert_called_once()
        self.assertEqual(mock_build.call_args.args[1], "usd-krw")

    def test_fx_topic_returns_none_when_fx_disabled(self):
        with patch.object(config, "FX_TOPIC_ENABLED", False), \
             patch("app.fx_topic_payload.load_and_build_fx_topic_payload") as mock_build:
            payload = _build_snapshot_sync("fx:jpy-krw")
        self.assertIsNone(payload)
        mock_build.assert_not_called()  # gate off → builder 비용 미발생

    def test_tether_topic_builds_without_krx_kwargs(self):
        """ADR-038 D2 — tether snapshot은 KRX 무관 (include_krx kwargs 자체가 없음)."""
        built = {"type": "snapshot", "version": 1, "data": {}}
        with patch("app.database.SessionLocal", MagicMock()), \
             patch(
                 "app.usdt_topic_payload.load_and_build_tether_tab_payload",
                 return_value=dict(built),
             ) as mock_build:
            payload = _build_snapshot_sync("usdt:krw")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["topic"], "usdt:krw")
        self.assertNotIn("include_krx", mock_build.call_args.kwargs)

    def test_unsupported_topics_return_none(self):
        with patch("app.fx_topic_payload.load_and_build_fx_topic_payload") as fx_b, \
             patch("app.usdt_topic_payload.load_and_build_tether_tab_payload") as t_b:
            self.assertIsNone(_build_snapshot_sync("dxy"))
            self.assertIsNone(_build_snapshot_sync("graph:usd-krw"))
            self.assertIsNone(_build_snapshot_sync("news"))
        fx_b.assert_not_called()
        t_b.assert_not_called()


class TestSupportedSnapshotTopics(unittest.TestCase):
    """supported_snapshot_topics() = REST bootstrap unknown_topic 검증 단일 소스.

    _build_snapshot_sync dispatch와 drift 잠금 (codex 019efe32).
    """

    def test_exact_supported_set_gate_off(self):
        """KRX_CLIENT_DISTRIBUTION_EFFECTIVE=false(기본) → krx topic 미포함."""
        from app import config
        from app.topic_initial_snapshot import supported_snapshot_topics
        with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", False):
            self.assertEqual(
                set(supported_snapshot_topics()),
                {"fx:usd-krw", "fx:jpy-krw", "fx:eur-krw", "usdt:krw"},
            )

    def test_supported_set_includes_krx_when_gate_on(self):
        """G2∧G3 on → krx:usd-krw-futures supported 포함 (ADR-038 D2)."""
        from app import config
        from app.topic_initial_snapshot import supported_snapshot_topics
        with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True):
            self.assertEqual(
                set(supported_snapshot_topics()),
                {"fx:usd-krw", "fx:jpy-krw", "fx:eur-krw", "usdt:krw",
                 "krx:usd-krw-futures"},
            )


class TestKrxSnapshotBranch(unittest.TestCase):
    """_build_snapshot_sync krx:usd-krw-futures branch (ADR-038 D2 positive coverage).

    gate-on: SessionLocal 열어 load_krx_topic_entry(db) — DB fallback 포함 경로.
    gate-off: load 자체 미호출 + None (supported 목록 제외와 동일 계약).
    """

    _TOPIC = "krx:usd-krw-futures"
    _ENTRY = {"source": "krx", "asset": "usd-krw-futures",
              "rate": 1382.0, "timestamp": "2026-07-08T10:00:05+09:00"}

    def test_gate_on_builds_krx_payload(self):
        from app import config
        fake_db = MagicMock()
        with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.database.SessionLocal", return_value=fake_db), \
             patch("app.krx_topic_publisher.load_krx_topic_entry",
                   return_value=dict(self._ENTRY)) as mock_load:
            payload = _build_snapshot_sync(self._TOPIC)
        mock_load.assert_called_once_with(fake_db)
        fake_db.close.assert_called_once()
        self.assertEqual(payload["type"], "snapshot")
        self.assertEqual(payload["topic"], self._TOPIC)
        self.assertEqual(payload["data"], {"usd_krw_futures": self._ENTRY})

    def test_gate_on_entry_none_returns_none(self):
        from app import config
        fake_db = MagicMock()
        with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.database.SessionLocal", return_value=fake_db), \
             patch("app.krx_topic_publisher.load_krx_topic_entry", return_value=None):
            self.assertIsNone(_build_snapshot_sync(self._TOPIC))
        fake_db.close.assert_called_once()

    def test_gate_off_returns_none_without_load(self):
        from app import config
        with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", False), \
             patch("app.krx_topic_publisher.load_krx_topic_entry") as mock_load:
            self.assertIsNone(_build_snapshot_sync(self._TOPIC))
        mock_load.assert_not_called()


class TestSendInitialSnapshots(unittest.IsolatedAsyncioTestCase):
    """send_initial_snapshots — _build_snapshot_sync patch로 격리(실 DB 0)."""

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_sends_snapshot_per_supported_topic(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()

        def fake_build(topic):
            return {"type": "snapshot", "version": 1, "topic": topic, "data": {}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_sync", side_effect=fake_build
        ):
            sent = await send_initial_snapshots(ws, ["fx:usd-krw", "usdt:krw"])
        self.assertEqual(sent, 2)
        self.assertEqual(ws.send_json.await_count, 2)

    async def test_unsupported_topic_skipped_no_send(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()

        with patch(
            "app.topic_initial_snapshot._build_snapshot_sync", side_effect=lambda t: None
        ):
            sent = await send_initial_snapshots(ws, ["dxy", "graph:x"])
        self.assertEqual(sent, 0)
        ws.send_json.assert_not_called()

    async def test_build_failure_isolated_continues(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()

        def fake_build(topic):
            if topic == "fx:usd-krw":
                raise RuntimeError("build boom")
            return {"type": "snapshot", "topic": topic, "data": {}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_sync", side_effect=fake_build
        ):
            sent = await send_initial_snapshots(ws, ["fx:usd-krw", "usdt:krw"])
        self.assertEqual(sent, 1)  # fx build 실패 격리 → usdt 계속
        ws.send_json.assert_awaited_once()

    async def test_send_failure_removes_ws_and_aborts_remaining(self):
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=Exception("send boom"))
        topic_dispatcher.registry.register(ws, ["fx:usd-krw", "usdt:krw"])

        def fake_build(topic):
            return {"type": "snapshot", "topic": topic, "data": {}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_sync", side_effect=fake_build
        ):
            sent = await send_initial_snapshots(ws, ["fx:usd-krw", "usdt:krw"])
        self.assertEqual(sent, 0)
        # send 실패 = connection 실패 → 1회만 시도하고 중단 + registry 정리
        self.assertEqual(ws.send_json.await_count, 1)
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)

    async def test_dedupe_within_request(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        built_topics = []

        def fake_build(topic):
            built_topics.append(topic)
            return {"type": "snapshot", "topic": topic, "data": {}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_sync", side_effect=fake_build
        ):
            sent = await send_initial_snapshots(ws, ["fx:usd-krw", "fx:usd-krw"])
        self.assertEqual(sent, 1)
        self.assertEqual(built_topics, ["fx:usd-krw"])  # 같은 요청 내 1회만 build

    async def test_empty_payload_still_sent(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()

        def fake_build(topic):
            # 빈 data여도 schema-valid → 전송(빈 화면 방지)
            return {"type": "snapshot", "version": 1, "topic": topic, "data": {"rates": []}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_sync", side_effect=fake_build
        ):
            sent = await send_initial_snapshots(ws, ["fx:usd-krw"])
        self.assertEqual(sent, 1)
        ws.send_json.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
