"""USDT WS Bithumb canary 단위 테스트 — Phase B.3 Stage U2 lifecycle skeleton.

USDT_WS_DESIGN_PLAN §12.5 Phase B.3 Stage U2 (2026-05-17).
Upbit Phase B.1 PR1 skeleton 테스트 패턴 작은 복제.

U2 핵심 acceptance (Codex 강조, plan §12.5.2):
    USDT_WS_BITHUMB_ENABLED=false 시 scheduler start 함수 즉시 return +
    BithumbWsClient 생성 X + network connect X + Redis/DB writer X.
    이 invariant가 U2부터 들어가야 후속 U3-U6 운영 영향 0 주장 유지.

U2 client behavior:
    - __init__: stop_event + running flag만 (network/Redis/DB import 없음)
    - start: stop_event 대기만 (중복 start 방지)
    - stop: stop_event set (idempotent)

실행:
    python -m unittest tests.test_usdt_ws_bithumb_skeleton -v
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import config, scheduler
from app.crawlers.usdt_ws.bithumb import BithumbWsClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_scheduler_usdt_ws_bithumb_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_bithumb_client = None
    scheduler.usdt_ws_bithumb_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_bithumb_client` mock — client.stop()까지 stop_event 대기."""
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# Stage U2 acceptance — flag=false invariant 핵심 검증 (Codex 강조)
# ---------------------------------------------------------------------------

class TestFlagFalseInvariant(unittest.IsolatedAsyncioTestCase):
    """Stage U2 핵심 acceptance — flag=false 시 운영 영향 0 검증.

    이 invariant가 보장되어야 U3-U6 stage가 누적되어도 운영 영향 0 유지.
    """

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_start_returns_immediately_when_flag_false(self):
        """flag=false → start 함수 즉시 return + client/task 생성 X (skip log only)."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler.start_usdt_ws_bithumb_client()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)
        # skip log emit 확인
        self.assertTrue(any("USDT_WS_BITHUMB_ENABLED=false" in m for m in cm.output))

    async def test_no_bithumb_client_constructed_when_flag_false(self):
        """flag=false → BithumbWsClient.__init__이 호출되지 않음 (생성 X invariant)."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             patch("app.crawlers.usdt_ws.bithumb.BithumbWsClient") as mock_client_cls:
            await scheduler.start_usdt_ws_bithumb_client()
        # BithumbWsClient 생성자가 호출되지 않음 — import 자체도 발생 안 함이 이상이지만
        # 함수 내부 import라 module load는 가능. 인스턴스 생성 0이 핵심 invariant.
        mock_client_cls.assert_not_called()

    async def test_no_network_redis_db_when_flag_false(self):
        """flag=false → network connect / Redis SET / DB write 0건 invariant.

        scheduler.start_usdt_ws_bithumb_client()가 즉시 return하므로 후속 stage에서
        추가될 BithumbRedisWriter / BithumbDbWriter / WS connect 호출 path 미진입.

        본 test는 U2 단계에서 client 생성 자체가 안 됨을 보장하면 충분 (writers는 client
        instance 안 attribute라 client 미생성 = writers 미생성 = 외부 IO 0).
        """
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False):
            await scheduler.start_usdt_ws_bithumb_client()
        # globals 모두 None — 어떤 client/writer/task도 생성 X
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)

    async def test_shutdown_safe_when_never_started(self):
        """start 호출 안 한 상태에서 shutdown → no-op (globals 그대로 None)."""
        # globals already None (setUp)
        await scheduler.shutdown_usdt_ws_bithumb_client()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)


# ---------------------------------------------------------------------------
# Stage U2 lifecycle — flag=true 경로 (network 무관, _run_* mock)
# ---------------------------------------------------------------------------

class TestStartUsdtWsBithumbClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_creates_task_when_enabled(self):
        """flag=true → BithumbWsClient + task 생성."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_bithumb_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_bithumb_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_bithumb_client)
                self.assertIsNotNone(scheduler.usdt_ws_bithumb_task)
                self.assertFalse(scheduler.usdt_ws_bithumb_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_bithumb_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_bithumb_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_bithumb_client()
            first_task = scheduler.usdt_ws_bithumb_task
            first_client = scheduler.usdt_ws_bithumb_client
            self.assertIsNotNone(first_task)

            try:
                await scheduler.start_usdt_ws_bithumb_client()
                self.assertIs(scheduler.usdt_ws_bithumb_task, first_task)
                self.assertIs(scheduler.usdt_ws_bithumb_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_bithumb_client()


class TestShutdownUsdtWsBithumbClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_bithumb_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_bithumb_client()
            task = scheduler.usdt_ws_bithumb_task
            self.assertIsNotNone(task)

            await scheduler.shutdown_usdt_ws_bithumb_client()
            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_bithumb_client)
            self.assertIsNone(scheduler.usdt_ws_bithumb_task)


# ---------------------------------------------------------------------------
# BithumbWsClient skeleton behavior
# ---------------------------------------------------------------------------

class TestBithumbWsClientSkeleton(unittest.IsolatedAsyncioTestCase):
    """U2 skeleton: __init__ + start (stop_event 대기) + stop (set)."""

    async def test_init_state(self):
        """__init__ → stop_event (not set) + running False."""
        client = BithumbWsClient()
        self.assertFalse(client._stop_event.is_set())
        self.assertFalse(client._running)

    async def test_start_waits_for_stop_event(self):
        """start → stop_event 대기 (running=True, network X)."""
        client = BithumbWsClient()
        task = asyncio.create_task(client.start())
        # 짧은 yield로 start loop 진입 보장
        await asyncio.sleep(0.01)
        self.assertTrue(client._running)
        # stop() 호출 → start loop 종료
        await client.stop()
        await asyncio.wait_for(task, timeout=1.0)
        self.assertFalse(client._running)

    async def test_double_start_skip(self):
        """이미 _running 시 두 번째 start 즉시 return (중복 방지)."""
        client = BithumbWsClient()
        task1 = asyncio.create_task(client.start())
        await asyncio.sleep(0.01)
        self.assertTrue(client._running)

        # 두 번째 start → 즉시 return (debug log)
        await client.start()
        # task1은 여전히 running
        self.assertFalse(task1.done())

        await client.stop()
        await asyncio.wait_for(task1, timeout=1.0)

    async def test_stop_idempotent(self):
        """stop 두 번 호출 → 두 번째도 안전 (이미 set인 event는 set 가능)."""
        client = BithumbWsClient()
        await client.stop()
        await client.stop()  # idempotent
        self.assertTrue(client._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
