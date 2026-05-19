"""USDT WS Korbit canary 단위 테스트 — Phase B.5 Stage K2 skeleton.

USDT_WS_DESIGN_PLAN §12.7 Phase B.5 Stage K2 (2026-05-19).
Coinone Stage C2 테스트 패턴 minimal 복제 (K2 skeleton 테스트는 이후 K3~K7
stage 진입 시 깨질 가능성 최소화 위해 핵심 5 시나리오만).

K2 핵심 acceptance (USDT_WS_DESIGN_PLAN §12.7.4):
    USDT_WS_KORBIT_ENABLED=false 시 scheduler start 함수 즉시 return +
    KorbitWsClient 생성 X + network connect X + Redis/DB writer X.
    이 invariant가 K2부터 들어가야 후속 K3-K7 운영 영향 0 주장 유지.

K2 client behavior:
    - __init__: stop_event + running flag만 (network/Redis/DB import 없음)
    - start: stop_event 대기만 (중복 start 방지)
    - stop: stop_event set (idempotent)

테스트 시나리오 (Coinone C2 패턴 mirror 6개):
    1. flag=false → KorbitWsClient 생성 안 함
    2. flag=true → task 생성됨
    3. 중복 start 방지
    4. shutdown 시 client/task 정리
    5. KorbitWsClient.start()가 stop()까지 대기
    6. (TestKorbitWsClientSkeleton init state + start wait)

실행:
    python -m unittest tests.test_usdt_ws_korbit_skeleton -v
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import config, scheduler
from app.crawlers.usdt_ws.korbit import KorbitWsClient


def _reset_scheduler_usdt_ws_korbit_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_korbit_client = None
    scheduler.usdt_ws_korbit_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_korbit_client` mock — client.stop()까지 stop_event 대기."""
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# Scenario 1: flag=false → KorbitWsClient 생성 안 함 (K2 핵심 acceptance)
# ---------------------------------------------------------------------------

class TestFlagFalseInvariant(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    async def test_no_korbit_client_constructed_when_flag_false(self):
        """flag=false → start 함수 즉시 return + KorbitWsClient 생성 X + task 생성 X."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", False), \
             patch("app.crawlers.usdt_ws.korbit.KorbitWsClient") as mock_client_cls, \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler.start_usdt_ws_korbit_client()
        mock_client_cls.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_korbit_client)
        self.assertIsNone(scheduler.usdt_ws_korbit_task)
        self.assertTrue(any("USDT_WS_KORBIT_ENABLED=false" in m for m in cm.output))


# ---------------------------------------------------------------------------
# Scenario 2-3: flag=true → task 생성, 중복 start 방지
# ---------------------------------------------------------------------------

class TestStartUsdtWsKorbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    async def test_creates_task_when_enabled(self):
        """flag=true → KorbitWsClient + task 생성."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_korbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_korbit_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_korbit_client)
                self.assertIsNotNone(scheduler.usdt_ws_korbit_task)
                self.assertFalse(scheduler.usdt_ws_korbit_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_korbit_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_korbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_korbit_client()
            first_task = scheduler.usdt_ws_korbit_task
            first_client = scheduler.usdt_ws_korbit_client
            self.assertIsNotNone(first_task)

            try:
                await scheduler.start_usdt_ws_korbit_client()
                self.assertIs(scheduler.usdt_ws_korbit_task, first_task)
                self.assertIs(scheduler.usdt_ws_korbit_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_korbit_client()


# ---------------------------------------------------------------------------
# Scenario 4: shutdown 시 client/task 정리
# ---------------------------------------------------------------------------

class TestShutdownUsdtWsKorbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_korbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_korbit_client()
            task = scheduler.usdt_ws_korbit_task
            self.assertIsNotNone(task)

            await scheduler.shutdown_usdt_ws_korbit_client()
            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_korbit_client)
            self.assertIsNone(scheduler.usdt_ws_korbit_task)


# ---------------------------------------------------------------------------
# Scenario 5-6: KorbitWsClient skeleton behavior
# ---------------------------------------------------------------------------

class TestKorbitWsClientSkeleton(unittest.IsolatedAsyncioTestCase):
    """K2 skeleton behavior: __init__ + start (stop_event 대기) + stop (set)."""

    async def test_init_state(self):
        """__init__ → stop_event (not set) + running False."""
        client = KorbitWsClient()
        self.assertFalse(client._stop_event.is_set())
        self.assertFalse(client._running)

    async def test_start_waits_until_stop(self):
        """start() → stop_event 대기. stop() 호출 시 종료."""
        client = KorbitWsClient()
        task = asyncio.create_task(client.start())
        # start가 무한 대기 (skeleton): 짧은 sleep 후 task 진행 중 확인
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        self.assertTrue(client._running)

        # stop() 호출 → task 종료
        await client.stop()
        await asyncio.wait_for(task, timeout=1.0)
        self.assertTrue(task.done())
        self.assertFalse(client._running)


if __name__ == "__main__":
    unittest.main()
