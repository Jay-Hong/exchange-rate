"""Phase B.6 Stage G1 — Gopax USDT/KRW WebSocket skeleton tests.

USDT_WS_DESIGN_PLAN §12.9 (2026-05-21).

G1 scope: flag + scheduler + lifecycle skeleton만. Subscribe/Parse/Primus
heartbeat/Liveness/Writers/Telemetry는 G2~ 별도 stage이므로 본 파일에서 검증 X.

G1 acceptance (테스트 핵심):
    - flag=false → GopaxWsClient 생성 X + task 생성 X + network connect X
    - flag=true → client/task 생성 + 중복 start 방지
    - shutdown → stop_event set + task 종료 + globals 초기화
    - GopaxWsClient minimal state (_stop_event, _running만 보유 — Codex 최종 권고)
    - start-stop lifecycle placeholder (no network)
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import config, scheduler
from app.crawlers.usdt_ws.gopax import (
    GOPAX_WS_URL,
    GopaxWsClient,
)


def _reset_scheduler_usdt_ws_gopax_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_gopax_client = None
    scheduler.usdt_ws_gopax_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_gopax_client` mock — client.stop()까지 stop_event 대기."""
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# G1 Scenario 1: flag=false → GopaxWsClient 생성 안 함 (G1 핵심 acceptance)
# ---------------------------------------------------------------------------


class TestFlagFalseInvariant(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    async def test_no_gopax_client_constructed_when_flag_false(self):
        """flag=false → start 함수 즉시 return + GopaxWsClient 생성 X + task 생성 X."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", False), \
             patch("app.crawlers.usdt_ws.gopax.GopaxWsClient") as mock_client_cls, \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler.start_usdt_ws_gopax_client()
        mock_client_cls.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_gopax_client)
        self.assertIsNone(scheduler.usdt_ws_gopax_task)
        self.assertTrue(any("USDT_WS_GOPAX_ENABLED=false" in m for m in cm.output))


# ---------------------------------------------------------------------------
# G1 Scenario 2-3: flag=true → task 생성, 중복 start 방지
# ---------------------------------------------------------------------------


class TestStartUsdtWsGopaxClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    async def test_creates_task_when_enabled(self):
        """flag=true → GopaxWsClient + task 생성."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_gopax_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_gopax_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_gopax_client)
                self.assertIsNotNone(scheduler.usdt_ws_gopax_task)
                self.assertFalse(scheduler.usdt_ws_gopax_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_gopax_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_gopax_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_gopax_client()
            first_task = scheduler.usdt_ws_gopax_task
            first_client = scheduler.usdt_ws_gopax_client
            self.assertIsNotNone(first_task)
            try:
                # 2번째 start 호출 — 동일 task 유지
                await scheduler.start_usdt_ws_gopax_client()
                self.assertIs(scheduler.usdt_ws_gopax_task, first_task)
                self.assertIs(scheduler.usdt_ws_gopax_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_gopax_client()


# ---------------------------------------------------------------------------
# G1 Shutdown: stop_event set + task 종료 + globals 초기화
# ---------------------------------------------------------------------------


class TestShutdownUsdtWsGopaxClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_gopax_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_gopax_client()
            task = scheduler.usdt_ws_gopax_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())

            await scheduler.shutdown_usdt_ws_gopax_client()

            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_gopax_client)
            self.assertIsNone(scheduler.usdt_ws_gopax_task)


# ---------------------------------------------------------------------------
# G1 Client skeleton: minimal state + start/stop lifecycle
# ---------------------------------------------------------------------------


class TestGopaxWsClientSkeleton(unittest.IsolatedAsyncioTestCase):
    """G1 minimal client — Codex 최종 권고대로 _stop_event / _running 만 보유."""

    async def test_init_minimal_state(self):
        """__init__ 직후: _stop_event 미설정 + _running=False. G4 attribute 부재."""
        client = GopaxWsClient()
        self.assertFalse(client._running)
        self.assertFalse(client._stop_event.is_set())
        # Codex 최종 권고 — G4 선반영 attribute 부재 검증
        self.assertFalse(hasattr(client, "_connection_status"))
        self.assertFalse(hasattr(client, "_ticker_freshness_status"))
        self.assertFalse(hasattr(client, "_reconnect_attempt_count"))
        self.assertFalse(hasattr(client, "_ws"))

    async def test_start_stop_lifecycle(self):
        """start() → _running=True → stop() → _stop_event.wait() 풀림 → _running=False."""
        client = GopaxWsClient()

        async def stop_after_start():
            # start()의 stop_event.wait() 진입 보장
            await asyncio.sleep(0.01)
            self.assertTrue(client._running)
            await client.stop()

        await asyncio.gather(client.start(), stop_after_start())
        self.assertFalse(client._running)
        self.assertTrue(client._stop_event.is_set())

    async def test_double_start_idempotent(self):
        """start() 호출 중 중복 start → 즉시 return (no duplicate lifecycle entry)."""
        client = GopaxWsClient()

        start_task = asyncio.create_task(client.start())
        await asyncio.sleep(0.01)  # 첫 start lifecycle 진입 대기

        # 중복 start — 즉시 return
        await client.start()
        self.assertTrue(client._running)  # 첫 start 그대로 유지

        await client.stop()
        await start_task
        self.assertFalse(client._running)


# ---------------------------------------------------------------------------
# G1 Module scope guard: GOPAX_WS_URL 노출 + G2~ symbol 부재
# ---------------------------------------------------------------------------


class TestScopeGuard(unittest.TestCase):
    """G1 module-level scope guard — 미구현 stage symbol 부재 검증."""

    def test_gopax_ws_url_exported(self):
        """G1: GOPAX_WS_URL constant module-level 노출."""
        self.assertEqual(GOPAX_WS_URL, "wss://wsapi.gopax.co.kr")

    def test_no_g2_g7_symbols_at_module_level(self):
        """G1: G2~G7에서 추가될 symbol module-level 부재 검증.

        G2 (Subscribe/Parse), G3 (Primus), G4 (Liveness), G5-G7 (writers/fallback/alert).
        본 G1 단계에서는 부재해야 함 (선제 작성 회피).
        """
        from app.crawlers.usdt_ws import gopax as gopax_module

        forbidden_attrs = [
            "GOPAX_SUBSCRIBE_PAYLOAD",   # G2
            "parse_gopax_ticker",        # G2
            "GopaxRedisWriter",          # G5
            "GopaxDbWriter",             # G6a
            "GopaxRestFallbackController",  # G6b
            "fetch_gopax_usdt_tick",     # G6b
        ]
        for attr in forbidden_attrs:
            self.assertFalse(
                hasattr(gopax_module, attr),
                f"G1 scope 위반: {attr} symbol이 module-level에 존재함 — G2~ stage에서 추가 예정",
            )


if __name__ == "__main__":
    unittest.main()
