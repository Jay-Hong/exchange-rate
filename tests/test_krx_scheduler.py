"""KRX scheduler lifecycle 단위 테스트.

start_krx_futures_client / _bootstrap_krx_futures_client /
shutdown_krx_futures_client + 모듈 globals (krx_bootstrap_task /
krx_futures_client / krx_futures_task) 검증.

운영 미연결 — 실제 KIS API / WebSocket 호출 X (mock).

실행:
    python -m unittest tests.test_krx_scheduler -v
"""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.sources.kis_master import ContractInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_scheduler_krx_globals():
    """Test isolation — module globals 초기화."""
    scheduler.krx_bootstrap_task = None
    scheduler.krx_futures_client = None
    scheduler.krx_futures_task = None


def _make_resolved_contract():
    return ContractInfo(
        short_code="A75605",
        standard_code="KR4A75650007",
        name="미국달러 F 202605",
        contract_month="202605",
        expiry_date=date(2026, 5, 18),
    )


# ---------------------------------------------------------------------------
# start_krx_futures_client — KRX_FUTURES_ENABLED gate + 중복 방지
# ---------------------------------------------------------------------------

class TestStartKrxFuturesClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_krx_globals()

    def tearDown(self):
        _reset_scheduler_krx_globals()

    async def test_skips_when_disabled(self):
        """KRX_FUTURES_ENABLED=false → bootstrap task 미생성."""
        with patch.object(config, "KRX_FUTURES_ENABLED", False), \
             self.assertLogs("exchange_rate.scheduler", level="INFO"):
            await scheduler.start_krx_futures_client()
        self.assertIsNone(scheduler.krx_bootstrap_task)
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertIsNone(scheduler.krx_futures_task)

    async def test_creates_bootstrap_task_when_enabled(self):
        """KRX_FUTURES_ENABLED=true → bootstrap task 생성 (background)."""
        # bootstrap 자체는 mock — 즉시 종료
        async def fake_bootstrap():
            return

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "_bootstrap_krx_futures_client", side_effect=fake_bootstrap):
            await scheduler.start_krx_futures_client()
            self.assertIsNotNone(scheduler.krx_bootstrap_task)
            await scheduler.krx_bootstrap_task  # bootstrap 완료 대기
            self.assertTrue(scheduler.krx_bootstrap_task.done())

    async def test_double_start_skipped_during_bootstrap(self):
        """start 두 번 호출 → bootstrap task 1개만 (중복 방지)."""
        # bootstrap이 길게 걸리는 상태 시뮬레이션
        bootstrap_event = asyncio.Event()

        async def slow_bootstrap():
            await bootstrap_event.wait()

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "_bootstrap_krx_futures_client", side_effect=slow_bootstrap):
            await scheduler.start_krx_futures_client()
            first_task = scheduler.krx_bootstrap_task
            self.assertIsNotNone(first_task)

            # 두 번째 start — bootstrap 진행 중이라 skip
            await scheduler.start_krx_futures_client()
            self.assertIs(scheduler.krx_bootstrap_task, first_task)  # 같은 task

            # cleanup — bootstrap 종료
            bootstrap_event.set()
            await first_task

    async def test_double_start_skipped_when_client_running(self):
        """client task 진행 중에 start 호출 → skip."""
        # client task 진행 중 시뮬레이션
        async def run_forever():
            await asyncio.sleep(10)

        scheduler.krx_futures_task = asyncio.create_task(run_forever())

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "_bootstrap_krx_futures_client") as mock_bootstrap:
            await scheduler.start_krx_futures_client()
            # bootstrap 안 호출됨 (skip)
            mock_bootstrap.assert_not_called()
            self.assertIsNone(scheduler.krx_bootstrap_task)

        # cleanup
        scheduler.krx_futures_task.cancel()
        try:
            await scheduler.krx_futures_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# _bootstrap_krx_futures_client — failure isolation
# ---------------------------------------------------------------------------

class TestBootstrapKrxFuturesClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_krx_globals()

    def tearDown(self):
        _reset_scheduler_krx_globals()

    async def test_fetch_failure_isolated(self):
        """fetch 실패 → logger.exception (resolve 실패) + globals None 유지.

        PR6c-2c refactor 후: resolve_active_krx_futures_contract가 raise
        하면 _bootstrap의 inner try/except가 잡고 "active contract resolve
        실패 (격리)" 로깅 — globals 정리.
        """
        with patch(
            "app.crawlers.krx_kis.KisApprovalManager"
        ), patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            side_effect=RuntimeError("network error"),
        ), self.assertLogs("exchange_rate.scheduler", level="ERROR") as cm:
            await scheduler._bootstrap_krx_futures_client()
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertIsNone(scheduler.krx_futures_task)
        self.assertTrue(any("resolve 실패" in m for m in cm.output))

    async def test_no_active_contract_returns_warning(self):
        """select 결과 None → warning + globals None."""
        with patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            return_value=b"dummy",
        ), patch(
            "app.sources.kis_master.parse_commodity_future_master",
            return_value=[],
        ), patch(
            "app.sources.kis_master.select_active_usd_futures_contract",
            return_value=None,
        ), self.assertLogs("exchange_rate.scheduler", level="WARNING") as cm:
            await scheduler._bootstrap_krx_futures_client()
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertTrue(any("active USD futures contract 없음" in m for m in cm.output))

    async def test_kis_keys_missing_returns_warning(self):
        """KIS_APP_KEY/SECRET 없음 → warning + globals None."""
        resolved = _make_resolved_contract()
        env_no_keys = {
            k: v for k, v in os.environ.items()
            if k not in ("KIS_APP_KEY", "KIS_APP_SECRET")
        }
        with patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            return_value=b"dummy",
        ), patch(
            "app.sources.kis_master.parse_commodity_future_master",
            return_value=[resolved],
        ), patch(
            "app.sources.kis_master.select_active_usd_futures_contract",
            return_value=resolved,
        ), patch.dict(os.environ, env_no_keys, clear=True), \
             self.assertLogs("exchange_rate.scheduler", level="WARNING") as cm:
            await scheduler._bootstrap_krx_futures_client()
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertTrue(any("KIS_APP_KEY/SECRET 미설정" in m for m in cm.output))

    async def test_normal_bootstrap_creates_client_and_task(self):
        """정상 bootstrap → globals에 client/task 할당 + start 로그."""
        resolved = _make_resolved_contract()

        # KisFuturesClient.start()를 mock — 즉시 끝나는 coroutine으로
        async def fake_start(*args, **kwargs):
            return

        mock_client_instance = MagicMock()
        mock_client_instance.start = AsyncMock(side_effect=fake_start)
        mock_client_instance.add_tick_handler = MagicMock()

        with patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            return_value=b"dummy",
        ), patch(
            "app.sources.kis_master.parse_commodity_future_master",
            return_value=[resolved],
        ), patch(
            "app.sources.kis_master.select_active_usd_futures_contract",
            return_value=resolved,
        ), patch.dict(
            os.environ,
            {"KIS_APP_KEY": "fake_key", "KIS_APP_SECRET": "fake_secret"},
        ), patch(
            "app.crawlers.krx_kis.KisApprovalManager"
        ), patch(
            "app.crawlers.krx_kis.KisFuturesClient",
            return_value=mock_client_instance,
        ), patch(
            "app.crawlers.krx_kis.KrxDbWriter"
        ), self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler._bootstrap_krx_futures_client()
            # task 완료 대기
            if scheduler.krx_futures_task is not None:
                await scheduler.krx_futures_task

        self.assertIsNotNone(scheduler.krx_futures_client)
        self.assertIsNotNone(scheduler.krx_futures_task)
        self.assertTrue(any("KisFuturesClient 시작" in m for m in cm.output))
        # add_tick_handler가 KrxDbWriter로 호출됨
        mock_client_instance.add_tick_handler.assert_called_once()


# ---------------------------------------------------------------------------
# resolve_active_krx_futures_contract — side-effect free helper (PR6c-2c)
# ---------------------------------------------------------------------------

class TestResolveActiveContract(unittest.IsolatedAsyncioTestCase):
    """side-effect free contract resolver — globals 변경 X, logging X.

    raise on failure — caller가 try/except + 로깅 책임.
    """

    def setUp(self):
        _reset_scheduler_krx_globals()

    def tearDown(self):
        _reset_scheduler_krx_globals()

    async def test_normal_returns_contract(self):
        """contracts에 USD futures 있으면 ContractInfo 반환."""
        resolved = _make_resolved_contract()
        with patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            return_value=b"dummy",
        ), patch(
            "app.sources.kis_master.parse_commodity_future_master",
            return_value=[resolved],
        ), patch(
            "app.sources.kis_master.select_active_usd_futures_contract",
            return_value=resolved,
        ):
            result = await scheduler.resolve_active_krx_futures_contract()
        self.assertEqual(result.short_code, "A75605")

    async def test_returns_none_when_no_active_contract(self):
        """select 결과 None → None 반환 (raise X)."""
        with patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            return_value=b"dummy",
        ), patch(
            "app.sources.kis_master.parse_commodity_future_master",
            return_value=[],
        ), patch(
            "app.sources.kis_master.select_active_usd_futures_contract",
            return_value=None,
        ):
            result = await scheduler.resolve_active_krx_futures_contract()
        self.assertIsNone(result)

    async def test_raises_on_fetch_failure(self):
        """fetch 실패 → raise (caller가 try/except + logging)."""
        with patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            side_effect=RuntimeError("network error"),
        ):
            with self.assertRaises(RuntimeError):
                await scheduler.resolve_active_krx_futures_contract()

    async def test_no_globals_side_effect(self):
        """resolve는 side-effect free — globals 변경 X."""
        resolved = _make_resolved_contract()
        with patch(
            "app.sources.kis_master.fetch_commodity_future_master",
            return_value=b"dummy",
        ), patch(
            "app.sources.kis_master.parse_commodity_future_master",
            return_value=[resolved],
        ), patch(
            "app.sources.kis_master.select_active_usd_futures_contract",
            return_value=resolved,
        ):
            await scheduler.resolve_active_krx_futures_contract()
        # globals 그대로
        self.assertIsNone(scheduler.krx_bootstrap_task)
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertIsNone(scheduler.krx_futures_task)


# ---------------------------------------------------------------------------
# bootstrap finally cleanup — current task 일치 시 globals None
# ---------------------------------------------------------------------------

class TestBootstrapFinallyCleanup(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_krx_globals()

    def tearDown(self):
        _reset_scheduler_krx_globals()

    async def test_cleanup_when_current_task_matches(self):
        """bootstrap이 background task로 실행 → 종료 시 globals 정리."""
        # bootstrap 실패 시나리오 (resolve 실패) — finally 블록 도달
        with patch(
            "app.scheduler.resolve_active_krx_futures_contract",
            side_effect=RuntimeError("network error"),
        ), self.assertLogs("exchange_rate.scheduler", level="ERROR"):
            # start_krx_futures_client → background task 생성
            with patch.object(config, "KRX_FUTURES_ENABLED", True):
                await scheduler.start_krx_futures_client()
                bootstrap_task = scheduler.krx_bootstrap_task
                self.assertIsNotNone(bootstrap_task)
                # bootstrap 완료 대기
                await bootstrap_task
            # finally에서 current_task() is krx_bootstrap_task 매칭 → None
            self.assertIsNone(scheduler.krx_bootstrap_task)

    async def test_no_cleanup_when_called_directly(self):
        """_bootstrap을 직접 호출 (current_task != krx_bootstrap_task) →
        global 변경 X (다른 task의 bootstrap 참조 보존)."""
        # 다른 task가 globals 점유 중
        async def fake_bootstrap_other():
            await asyncio.sleep(10)

        other_task = asyncio.create_task(fake_bootstrap_other())
        scheduler.krx_bootstrap_task = other_task

        # _bootstrap을 직접 호출 (run as current task, but krx_bootstrap_task is `other_task`)
        with patch(
            "app.scheduler.resolve_active_krx_futures_contract",
            side_effect=RuntimeError("network error"),
        ), self.assertLogs("exchange_rate.scheduler", level="ERROR"):
            await scheduler._bootstrap_krx_futures_client()

        # current_task != krx_bootstrap_task (other_task) → 정리 X
        self.assertIs(scheduler.krx_bootstrap_task, other_task)

        # cleanup
        other_task.cancel()
        try:
            await other_task
        except asyncio.CancelledError:
            pass
        scheduler.krx_bootstrap_task = None


# ---------------------------------------------------------------------------
# shutdown_krx_futures_client — bootstrap + client + task cleanup
# ---------------------------------------------------------------------------

class TestShutdownKrxFuturesClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_krx_globals()

    def tearDown(self):
        _reset_scheduler_krx_globals()

    async def test_shutdown_when_nothing_started(self):
        """globals 모두 None → 무동작 + 예외 X."""
        await scheduler.shutdown_krx_futures_client()
        self.assertIsNone(scheduler.krx_bootstrap_task)
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertIsNone(scheduler.krx_futures_task)

    async def test_shutdown_cancels_bootstrap_task(self):
        """bootstrap 진행 중 shutdown → bootstrap task cancel + globals None."""
        bootstrap_event = asyncio.Event()

        async def slow_bootstrap():
            await bootstrap_event.wait()

        scheduler.krx_bootstrap_task = asyncio.create_task(slow_bootstrap())

        await scheduler.shutdown_krx_futures_client()
        self.assertIsNone(scheduler.krx_bootstrap_task)
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertIsNone(scheduler.krx_futures_task)

    async def test_shutdown_stops_client_and_cancels_task(self):
        """client + task 진행 중 shutdown → stop() + cancel + globals None."""
        mock_client = MagicMock()
        mock_client.stop = AsyncMock()

        async def run_forever():
            await asyncio.sleep(10)

        scheduler.krx_futures_client = mock_client
        scheduler.krx_futures_task = asyncio.create_task(run_forever())

        await scheduler.shutdown_krx_futures_client()
        mock_client.stop.assert_awaited_once()
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertIsNone(scheduler.krx_futures_task)


if __name__ == "__main__":
    unittest.main(verbosity=2)
