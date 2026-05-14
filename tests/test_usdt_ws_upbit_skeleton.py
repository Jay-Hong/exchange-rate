"""USDT WS Upbit canary skeleton 단위 테스트 (PR1).

USDT_WS_DESIGN_PLAN §12 PR1 검증 — feature flag + lifecycle scaffolding only.
외부 network 호출 없음을 코드 경로 자체에서 보장.

검증 항목 (Codex 5):
    1. USDT_WS_UPBIT_ENABLED=false → start no-op
    2. USDT_WS_UPBIT_ENABLED=true → task 생성
    3. shutdown → task/client cleanup
    4. 중복 start → 동일 task 유지 (duplicate task 없음)
    5. no-network 보장 — upbit.py source에 websockets/wss/endpoint 미포함

실행:
    python -m unittest tests.test_usdt_ws_upbit_skeleton -v
"""
from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest.mock import patch

from app import config, scheduler
from app.crawlers.usdt_ws import upbit as upbit_mod


def _reset_scheduler_usdt_ws_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_upbit_client = None
    scheduler.usdt_ws_upbit_task = None


class TestStartUsdtWsUpbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_globals()

    async def test_skips_when_disabled(self):
        """USDT_WS_UPBIT_ENABLED=false → client/task 미생성."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", False), \
             self.assertLogs("exchange_rate.scheduler", level="INFO"):
            await scheduler.start_usdt_ws_upbit_client()
        self.assertIsNone(scheduler.usdt_ws_upbit_client)
        self.assertIsNone(scheduler.usdt_ws_upbit_task)

    async def test_creates_task_when_enabled(self):
        """USDT_WS_UPBIT_ENABLED=true → UpbitWsClient + task 생성."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", True):
            await scheduler.start_usdt_ws_upbit_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_upbit_client)
                self.assertIsNotNone(scheduler.usdt_ws_upbit_task)
                self.assertFalse(scheduler.usdt_ws_upbit_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_upbit_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", True):
            await scheduler.start_usdt_ws_upbit_client()
            first_task = scheduler.usdt_ws_upbit_task
            first_client = scheduler.usdt_ws_upbit_client
            self.assertIsNotNone(first_task)

            try:
                # 두 번째 start — task 진행 중이라 skip, 동일 reference 유지
                await scheduler.start_usdt_ws_upbit_client()
                self.assertIs(scheduler.usdt_ws_upbit_task, first_task)
                self.assertIs(scheduler.usdt_ws_upbit_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_upbit_client()


class TestShutdownUsdtWsUpbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", True):
            await scheduler.start_usdt_ws_upbit_client()
            task = scheduler.usdt_ws_upbit_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())

            await scheduler.shutdown_usdt_ws_upbit_client()

            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_upbit_client)
            self.assertIsNone(scheduler.usdt_ws_upbit_task)

    async def test_shutdown_is_noop_when_not_started(self):
        """start 안 한 상태에서 shutdown → exception 없음, globals 그대로 None."""
        await scheduler.shutdown_usdt_ws_upbit_client()
        self.assertIsNone(scheduler.usdt_ws_upbit_client)
        self.assertIsNone(scheduler.usdt_ws_upbit_task)


class TestNoNetworkGuarantee(unittest.TestCase):
    """PR1 strict 'no network' 보장 — upbit.py source 자체에 network 코드 없음.

    PR2에서 connect 추가 시 본 테스트는 제거되고 실제 mock 기반 테스트로 교체.
    """

    def test_upbit_module_source_has_no_network_symbols(self):
        source = inspect.getsource(upbit_mod)
        # WebSocket library import 금지
        self.assertNotIn("import websockets", source)
        self.assertNotIn("from websockets", source)
        # endpoint URL 금지 (wss:// / https://api.upbit.com 등)
        self.assertNotIn("wss://", source)
        self.assertNotIn("api.upbit.com", source)
        # HTTP 클라이언트 import 금지 (skeleton 단계에서는 아예 없음)
        self.assertNotIn("import aiohttp", source)
        self.assertNotIn("import requests", source)
        self.assertNotIn("import httpx", source)


if __name__ == "__main__":
    unittest.main()
