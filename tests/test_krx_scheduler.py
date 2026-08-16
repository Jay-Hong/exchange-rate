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
    # PR6c-2d-2 — reconcile state 초기화 (테스트 isolation)
    scheduler._krx_reconcile_state = {
        "last_run_at_kst": None,
        "last_result": None,
        "last_current_contract": None,
        "last_resolved_contract": None,
        "rollover_count": 0,
    }


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
        # add_tick_handler가 KrxDbWriter + KrxCloseWindowWriter로 호출됨
        # (Stage 5, 2026-05-17): KRX_CLOSE_FINALIZER_ENABLED default true → 2 handlers.
        self.assertEqual(mock_client_instance.add_tick_handler.call_count, 2)


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


# ---------------------------------------------------------------------------
# _reconcile_krx_futures_contract — PR6c-2d-1 5분 reconcile
# ---------------------------------------------------------------------------

class TestReconcileKrxFuturesContract(unittest.IsolatedAsyncioTestCase):
    """PR6c-2d-1 — 5분 contract reconcile 단위 테스트.

    동작 검증:
      - KRX_FUTURES_ENABLED=false → 즉시 return
      - resolve 실패 → 격리 (예외 propagate X)
      - resolve None → warning + no-op
      - client None + bootstrap_task 없음 → _bootstrap_krx_futures_client(resolved_override=)
      - client None + bootstrap_task 진행 중 → skip (race guard, Codex 3회차 권고)
      - 같은 contract → no-op
      - 점프 의심 (인접 월물 아님 — 결번/역행/형식오류) → 보류
      - 정상 rollover → shutdown + _bootstrap_krx_futures_client(resolved_override=)
        (Codex Issue 2: 두 번째 resolve 회피)
    """

    def setUp(self):
        _reset_scheduler_krx_globals()

    def tearDown(self):
        _reset_scheduler_krx_globals()

    async def test_skips_when_disabled(self):
        """KRX_FUTURES_ENABLED=false → 아무 호출 X."""
        with patch.object(config, "KRX_FUTURES_ENABLED", False), \
             patch.object(scheduler, "resolve_active_krx_futures_contract", new=AsyncMock()) as mock_resolve, \
             patch.object(scheduler, "start_krx_futures_client", new=AsyncMock()) as mock_start, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown:
            await scheduler._reconcile_krx_futures_contract()
        mock_resolve.assert_not_called()
        mock_start.assert_not_called()
        mock_shutdown.assert_not_called()

    async def test_resolve_failure_isolated(self):
        """resolve 예외 → exception 캐치, 기존 client 유지 + state=resolve_error."""
        existing = MagicMock()
        existing._contract = _make_resolved_contract()
        scheduler.krx_futures_client = existing

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(side_effect=RuntimeError("master fetch fail"))), \
             patch.object(scheduler, "start_krx_futures_client", new=AsyncMock()) as mock_start, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown, \
             self.assertLogs("exchange_rate.scheduler", level="ERROR"):
            await scheduler._reconcile_krx_futures_contract()

        # 기존 client 유지
        self.assertIs(scheduler.krx_futures_client, existing)
        mock_start.assert_not_called()
        mock_shutdown.assert_not_called()
        # PR6c-2d-2 — reconcile state 검증
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "resolve_error")

    async def test_resolved_none_keeps_client(self):
        """resolve None → warning + 기존 client 유지 + state=resolved_none."""
        existing = MagicMock()
        existing._contract = _make_resolved_contract()
        scheduler.krx_futures_client = existing

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=None)), \
             patch.object(scheduler, "start_krx_futures_client", new=AsyncMock()) as mock_start, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown, \
             self.assertLogs("exchange_rate.scheduler", level="WARNING"):
            await scheduler._reconcile_krx_futures_contract()

        self.assertIs(scheduler.krx_futures_client, existing)
        mock_start.assert_not_called()
        mock_shutdown.assert_not_called()
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "resolved_none")

    async def test_no_op_when_same_contract(self):
        """resolved == current → no-op (대부분 케이스) + state=no_op."""
        existing = MagicMock()
        existing._contract = _make_resolved_contract()  # A75605
        scheduler.krx_futures_client = existing

        same = _make_resolved_contract()
        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=same)), \
             patch.object(scheduler, "start_krx_futures_client", new=AsyncMock()) as mock_start, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown:
            await scheduler._reconcile_krx_futures_contract()

        mock_start.assert_not_called()
        mock_shutdown.assert_not_called()
        # PR6c-2d-2 — no_op도 state 기록 (5/18 admin 검증용)
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "no_op")
        self.assertEqual(
            scheduler._krx_reconcile_state["last_current_contract"]["code"], "A75605"
        )

    async def test_bootstrap_when_client_is_none(self):
        """client None + resolved 정상 + bootstrap 성공 (globals set) → bootstrap_started."""
        scheduler.krx_futures_client = None
        resolved = _make_resolved_contract()

        # PR6c-2d-2 fix: bootstrap mock이 globals를 set해야 post-condition 통과.
        # 실제 _bootstrap_krx_futures_client는 globals 갱신 후 return.
        async def fake_bootstrap(*args, **kwargs):
            mock_client = MagicMock()
            mock_client._contract = resolved
            scheduler.krx_futures_client = mock_client

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock(side_effect=fake_bootstrap)) as mock_boot, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown:
            await scheduler._reconcile_krx_futures_contract()

        # PR6c-2d-1 amend: bootstrap을 resolved_override와 함께 직접 호출 (Codex Issue 2)
        mock_boot.assert_awaited_once_with(resolved_override=resolved)
        mock_shutdown.assert_not_called()
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "bootstrap_started")

    async def test_bootstrap_failure_when_client_is_none(self):
        """client None + bootstrap 실패 (globals=None 유지) → bootstrap_error.

        PR6c-2d-2 fix (Codex BLOCKING): _bootstrap_krx_futures_client 내부 예외 catch라
        raise 안 됨. post-condition globals=None 검증으로 실패 분류.
        """
        scheduler.krx_futures_client = None
        resolved = _make_resolved_contract()

        # bootstrap mock이 globals 그대로 (None) — 실패 시뮬레이션
        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock()), \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()):
            await scheduler._reconcile_krx_futures_contract()

        # globals 변경 없음 → bootstrap_error 분류
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "bootstrap_error")
        self.assertEqual(scheduler._krx_reconcile_state["rollover_count"], 0)

    async def test_skips_when_bootstrap_in_progress(self):
        """client None인데 lifespan bootstrap_task 진행 중 → skip (Codex 3회차 가드).

        main.py lifespan에서 만든 bootstrap_task가 아직 완료 전인데 5분 reconcile이
        발화하면 중복 client 생성 위험. krx_bootstrap_task가 not done이면 skip.
        """
        scheduler.krx_futures_client = None

        # 진행 중 bootstrap task 시뮬레이션 — 영원히 안 끝나는 task
        async def slow_bootstrap():
            await asyncio.sleep(60)

        scheduler.krx_bootstrap_task = asyncio.create_task(slow_bootstrap())

        resolved = _make_resolved_contract()
        try:
            with patch.object(config, "KRX_FUTURES_ENABLED", True), \
                 patch.object(scheduler, "resolve_active_krx_futures_contract",
                              new=AsyncMock(return_value=resolved)), \
                 patch.object(scheduler, "_bootstrap_krx_futures_client", new=AsyncMock()) as mock_boot, \
                 patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown:
                await scheduler._reconcile_krx_futures_contract()

            # bootstrap 진행 중이라 reconcile은 skip — 새 bootstrap 안 띄움
            mock_boot.assert_not_called()
            mock_shutdown.assert_not_called()
            self.assertEqual(scheduler._krx_reconcile_state["last_result"], "bootstrap_skipped")
        finally:
            scheduler.krx_bootstrap_task.cancel()
            try:
                await scheduler.krx_bootstrap_task
            except asyncio.CancelledError:
                pass

    async def test_normal_rollover_calls_shutdown_then_bootstrap(self):
        """resolved != current + 만기 1개월 차이 → shutdown + bootstrap(resolved_override=)."""
        existing = MagicMock()
        existing._contract = _make_resolved_contract()  # A75605, 5/18 만기
        scheduler.krx_futures_client = existing

        next_month = ContractInfo(
            short_code="A75606",
            standard_code="KR4A75660006",
            name="미국달러 F 202606",
            contract_month="202606",
            expiry_date=date(2026, 6, 15),  # 28일 차이
        )

        # PR6c-2d-2 fix: bootstrap mock이 globals를 새 contract로 갱신해야
        # post-condition (krx_futures_client._contract == resolved) 통과.
        # 실제 _bootstrap_krx_futures_client는 새 KisFuturesClient를 globals에 설정.
        async def fake_bootstrap(*args, **kwargs):
            new_client = MagicMock()
            new_client._contract = next_month
            scheduler.krx_futures_client = new_client

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=next_month)), \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock(side_effect=fake_bootstrap)) as mock_boot, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown, \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as log_cm:
            await scheduler._reconcile_krx_futures_contract()

        mock_shutdown.assert_awaited_once()
        # PR6c-2d-1 amend: 두 번째 resolve 회피 — resolved를 직접 넘김 (Codex Issue 2)
        mock_boot.assert_awaited_once_with(resolved_override=next_month)
        # rollover 로그 검증
        self.assertTrue(
            any("rollover A75605/202605 → A75606/202606" in m for m in log_cm.output),
            f"rollover info log 부재: {log_cm.output}",
        )
        # PR6c-2d-2 — 정상 rollover state + count 증가
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "rollover")
        self.assertEqual(scheduler._krx_reconcile_state["rollover_count"], 1)
        self.assertEqual(
            scheduler._krx_reconcile_state["last_resolved_contract"]["code"], "A75606"
        )

    async def test_rollover_bootstrap_failure_records_bootstrap_error(self):
        """rollover 시 shutdown은 성공했지만 새 bootstrap 실패 (globals=None) → bootstrap_error.

        PR6c-2d-2 (Codex BLOCKING): _bootstrap_krx_futures_client 내부 예외 catch라
        raise 안 됨. 외부 try/except로 실패 감지 불가 → post-condition globals 검증.
        잘못 rollover로 기록되면 rollover_count 부풀어 운영 metric 신뢰도 ↓.
        """
        existing = MagicMock()
        existing._contract = _make_resolved_contract()  # A75605, 5/18 만기
        scheduler.krx_futures_client = existing

        next_month = ContractInfo(
            short_code="A75606",
            standard_code="KR4A75660006",
            name="미국달러 F 202606",
            contract_month="202606",
            expiry_date=date(2026, 6, 15),
        )

        # shutdown은 globals 정리 시뮬레이션 (실제 동작 모사)
        async def fake_shutdown():
            scheduler.krx_futures_client = None
            scheduler.krx_futures_task = None

        # bootstrap mock은 globals 그대로 (None) — 실패 시뮬레이션
        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=next_month)), \
             patch.object(scheduler, "_bootstrap_krx_futures_client", new=AsyncMock()), \
             patch.object(scheduler, "shutdown_krx_futures_client",
                          new=AsyncMock(side_effect=fake_shutdown)), \
             self.assertLogs("exchange_rate.scheduler", level="ERROR"):
            await scheduler._reconcile_krx_futures_contract()

        # globals는 None (shutdown 후 bootstrap 실패) → bootstrap_error 기록
        self.assertIsNone(scheduler.krx_futures_client)
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "bootstrap_error")
        # rollover로 잘못 기록되지 않음
        self.assertEqual(scheduler._krx_reconcile_state["rollover_count"], 0)

    async def test_jump_protection_skips_rollover(self):
        """resolved가 current의 인접 월물이 아님(202605 → 202607, 건너뜀) → 보류."""
        existing = MagicMock()
        existing._contract = _make_resolved_contract()  # A75605, 월물 202605
        scheduler.krx_futures_client = existing

        far_future = ContractInfo(
            short_code="A75607",
            standard_code="KR4A75670005",
            name="미국달러 F 202607",
            contract_month="202607",  # 202606을 건너뜀
            expiry_date=date(2026, 7, 20),
        )

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=far_future)), \
             patch.object(scheduler, "_bootstrap_krx_futures_client", new=AsyncMock()) as mock_boot, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_shutdown, \
             self.assertLogs("exchange_rate.scheduler", level="WARNING") as log_cm:
            await scheduler._reconcile_krx_futures_contract()

        # rollover 보류 — shutdown / bootstrap 호출 X
        mock_shutdown.assert_not_called()
        mock_boot.assert_not_called()
        self.assertTrue(
            any("점프 의심" in m for m in log_cm.output),
            f"점프 의심 warning 부재: {log_cm.output}",
        )
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "jump_suppressed")
        self.assertEqual(scheduler._krx_reconcile_state["rollover_count"], 0)

    async def test_jump_suppressed_persists_and_only_restart_recovers(self):
        """skipped-month 보류는 **살아 있는 collector 위에서 자체 복구되지 않는다**.

        `_is_next_contract_month`를 반복 호출하는 것만으로는 계약이 잠기지 않는다
        (순수 함수라 항상 같은 답) — reconcile **상태 머신을 실제로 돌려야** 한다.
        그리고 **live task를 명시로 세워야** 한다: reconcile은 jump guard보다 앞서
        `task.done()`을 별도 복구 분기로 처리하므로, task=None으로 두면 "살아 있는
        collector에서의 반복 억제"를 증명하지 못한다(codex 리뷰 지적).

        복구 경로는 두 가지이고 **둘 다 collector가 사라졌을 때**다:
          (a) client 소실(프로세스 재시작 등) → bootstrap
          (b) client 잔존 + task 사망 → task-death 재시작 분기
        즉 정상 실행 중에는 자동 해소가 없다 — optional source의 fail-closed
        정책으로 수용하되, 그 사실을 운영 계약으로 박제한다.
        """
        existing = MagicMock()
        existing._contract = _make_resolved_contract()  # 월물 202605
        scheduler.krx_futures_client = existing
        scheduler.krx_futures_task = _task_mock(done=False)  # ★ 살아 있는 collector

        skipped = ContractInfo(
            short_code="A75607",
            standard_code="KR4A75670005",
            name="미국달러 F 202607",
            contract_month="202607",  # 202606 건너뜀
            expiry_date=date(2026, 7, 20),
        )

        async def fake_bootstrap(*args, **kwargs):
            mock_client = MagicMock()
            mock_client._contract = skipped
            scheduler.krx_futures_client = mock_client

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=skipped)), \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock(side_effect=fake_bootstrap)) as mock_boot, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()):
            # (1) client 생존 중에는 몇 번을 돌려도 보류 — 자체 복구 없음
            for _ in range(3):
                await scheduler._reconcile_krx_futures_contract()
                self.assertEqual(
                    scheduler._krx_reconcile_state["last_result"], "jump_suppressed"
                )
            mock_boot.assert_not_called()
            self.assertIs(scheduler.krx_futures_client, existing)

            # (2) 재시작/소실로 collector가 없어지면 bootstrap이 resolved를 직접 사용
            scheduler.krx_futures_client = None
            scheduler.krx_futures_task = None
            await scheduler._reconcile_krx_futures_contract()

        mock_boot.assert_awaited_once_with(resolved_override=skipped)
        self.assertEqual(
            scheduler._krx_reconcile_state["last_result"], "bootstrap_started"
        )
        self.assertEqual(scheduler._krx_reconcile_state["rollover_count"], 0)


class TestIsNextContractMonth(unittest.TestCase):
    """월물 인접성 판정 (구 "만기 45일 차이" 날짜 산술 대체, 2026-08-16).

    날짜 산술을 쓰면 만기 계산의 walk-back 한도와 결합돼, 이론상 정상 rollover가
    가드에 막힐 수 있다. 월물 인접성은 같은 의도를 결합 없이 표현한다.
    """

    def test_normal_next_month(self):
        self.assertTrue(scheduler._is_next_contract_month("202608", "202609"))

    def test_year_wrap_dec_to_jan(self):
        self.assertTrue(scheduler._is_next_contract_month("202612", "202701"))

    def test_same_month_rejected(self):
        self.assertFalse(scheduler._is_next_contract_month("202608", "202608"))

    def test_backward_rejected(self):
        self.assertFalse(scheduler._is_next_contract_month("202609", "202608"))
        self.assertFalse(scheduler._is_next_contract_month("202701", "202612"))

    def test_skipped_month_rejected(self):
        self.assertFalse(scheduler._is_next_contract_month("202608", "202610"))

    def test_year_wrap_skip_rejected(self):
        self.assertFalse(scheduler._is_next_contract_month("202612", "202702"))

    def test_malformed_input_fails_closed(self):
        """형식 오류는 fail-closed — 통과시키면 엉뚱한 월물을 구독한다."""
        for cur, res in [
            ("2026", "202609"), ("202608", "20269"), ("20260X", "202609"),
            ("202613", "202614"), ("202600", "202601"), ("", "202609"),
        ]:
            with self.subTest(cur=cur, res=res):
                self.assertFalse(scheduler._is_next_contract_month(cur, res))

    def test_out_of_range_year_fails_closed(self):
        """year 0 등 범위 밖 → False.

        구 버전은 `("000001", "000002")`를 인접으로 통과시켜 docstring의
        "형식 오류 fail-closed"와 반대였다 (codex 리뷰 발견).
        """
        for cur, res in [("000001", "000002"), ("000012", "000101")]:
            with self.subTest(cur=cur, res=res):
                self.assertFalse(scheduler._is_next_contract_month(cur, res))


# ---------------------------------------------------------------------------
# PR6c-2d-2 — get_krx_reconcile_status() helper
# ---------------------------------------------------------------------------


class TestGetKrxReconcileStatus(unittest.IsolatedAsyncioTestCase):
    """admin endpoint 노출 helper. shape는 항상 동일 (Codex 권고)."""

    def setUp(self):
        _reset_scheduler_krx_globals()

    def tearDown(self):
        _reset_scheduler_krx_globals()

    def test_shape_consistency_when_no_job(self):
        """job 미등록 — job_registered=false + last_*=null. shape는 동일."""
        # scheduler에 krx_contract_reconcile job이 없다고 가정 (test 환경)
        # 실제 운영 시 KRX_FUTURES_ENABLED=false 또는 scheduler 초기화 전 케이스
        with patch.object(scheduler.scheduler, "get_job", return_value=None):
            status = scheduler.get_krx_reconcile_status()
        # 필수 key 존재 + null
        self.assertIn("job_registered", status)
        self.assertIn("next_run_at_kst", status)
        self.assertIn("last_run_at_kst", status)
        self.assertIn("last_result", status)
        self.assertIn("last_current_contract", status)
        self.assertIn("last_resolved_contract", status)
        self.assertIn("rollover_count", status)
        self.assertFalse(status["job_registered"])
        self.assertIsNone(status["next_run_at_kst"])
        self.assertIsNone(status["last_result"])
        self.assertEqual(status["rollover_count"], 0)

    def test_shape_with_state_recorded(self):
        """state 기록 후 last_* 반영."""
        contract = _make_resolved_contract()
        scheduler._record_krx_reconcile(
            result="no_op", current=contract, resolved=contract,
        )
        with patch.object(scheduler.scheduler, "get_job", return_value=None):
            status = scheduler.get_krx_reconcile_status()
        self.assertEqual(status["last_result"], "no_op")
        self.assertEqual(status["last_current_contract"]["code"], "A75605")
        self.assertEqual(status["last_current_contract"]["month"], "202605")
        self.assertEqual(status["last_current_contract"]["expires_on"], "2026-05-18")
        self.assertIsNotNone(status["last_run_at_kst"])


# ---------------------------------------------------------------------------
# KRX task-death gap (§12.9.8 ② KRX-side) — client 객체 잔존 + task.done()이면 부활
# ---------------------------------------------------------------------------


def _task_mock(done: bool):
    """krx_futures_task stub — done() 제어."""
    t = MagicMock()
    t.done.return_value = done
    return t


class TestReconcileKrxTaskDeath(unittest.IsolatedAsyncioTestCase):
    """reconcile이 'client 살아있는 척하지만 task만 죽은' 상태를 부활시키는지 검증."""

    def setUp(self):
        _reset_scheduler_krx_globals()
        scheduler._collector_shutdown_initiated = False

    async def test_dead_task_restarts_via_shutdown_then_bootstrap(self):
        # client 비None + task done + 만기 동일 → no_op 안 가고 shutdown→bootstrap 재시작
        existing = MagicMock()
        existing._contract = _make_resolved_contract()  # A75605
        scheduler.krx_futures_client = existing
        scheduler.krx_futures_task = _task_mock(done=True)
        resolved = _make_resolved_contract()  # A75605 (same)

        async def fake_bootstrap(*a, **k):
            mc = MagicMock()
            mc._contract = resolved
            scheduler.krx_futures_client = mc

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_sd, \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock(side_effect=fake_bootstrap)) as mock_boot:
            await scheduler._reconcile_krx_futures_contract()

        mock_sd.assert_awaited_once()  # teardown 먼저
        mock_boot.assert_awaited_once_with(resolved_override=resolved)  # resolved 재사용
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "task_dead_restarted")

    async def test_dead_task_restart_bootstrap_failure(self):
        # task done + bootstrap 실패(client None 유지) → post-condition으로 task_dead_restart_error
        existing = MagicMock()
        existing._contract = _make_resolved_contract()
        scheduler.krx_futures_client = existing
        scheduler.krx_futures_task = _task_mock(done=True)
        resolved = _make_resolved_contract()

        async def fail_bootstrap(*a, **k):
            scheduler.krx_futures_client = None  # 실패

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()), \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock(side_effect=fail_bootstrap)):
            await scheduler._reconcile_krx_futures_contract()

        self.assertEqual(
            scheduler._krx_reconcile_state["last_result"], "task_dead_restart_error"
        )

    async def test_live_task_same_contract_noop(self):
        # task 살아있음(done False) + 만기 동일 → 신규 분기 skip → no_op (회귀)
        existing = MagicMock()
        existing._contract = _make_resolved_contract()
        scheduler.krx_futures_client = existing
        scheduler.krx_futures_task = _task_mock(done=False)
        resolved = _make_resolved_contract()

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_sd, \
             patch.object(scheduler, "_bootstrap_krx_futures_client", new=AsyncMock()) as mock_boot:
            await scheduler._reconcile_krx_futures_contract()

        mock_sd.assert_not_called()
        mock_boot.assert_not_called()
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "no_op")

    async def test_live_task_contract_mismatch_rollover(self):
        # task 살아있음 + 만기 다름 → 신규 분기 skip → 기존 rollover (회귀)
        existing = MagicMock()
        existing._contract = _make_resolved_contract()  # A75605, expiry 5/18
        scheduler.krx_futures_client = existing
        scheduler.krx_futures_task = _task_mock(done=False)
        resolved = ContractInfo(
            short_code="A75606", standard_code="KR4A75660006", name="미국달러 F 202606",
            contract_month="202606", expiry_date=date(2026, 6, 15),
        )

        async def fake_bootstrap(*a, **k):
            mc = MagicMock()
            mc._contract = resolved
            scheduler.krx_futures_client = mc

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_sd, \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock(side_effect=fake_bootstrap)):
            await scheduler._reconcile_krx_futures_contract()

        mock_sd.assert_awaited_once()
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "rollover")

    async def test_shutdown_flag_blocks_restart(self):
        # _collector_shutdown_initiated=True → top에서 즉시 return (resolve도 안 함, race guard)
        existing = MagicMock()
        existing._contract = _make_resolved_contract()
        scheduler.krx_futures_client = existing
        scheduler.krx_futures_task = _task_mock(done=True)
        scheduler._collector_shutdown_initiated = True

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock()) as mock_resolve, \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_sd, \
             patch.object(scheduler, "_bootstrap_krx_futures_client", new=AsyncMock()) as mock_boot:
            await scheduler._reconcile_krx_futures_contract()

        mock_resolve.assert_not_called()  # top flag check → resolve 전 return
        mock_sd.assert_not_called()
        mock_boot.assert_not_called()

    async def test_client_none_with_dead_task_uses_bootstrap_path(self):
        # client None이면 task done이어도 신규 분기 미도달 — 기존 client-None bootstrap path 우선
        scheduler.krx_futures_client = None
        scheduler.krx_futures_task = _task_mock(done=True)  # 무관 (client None이 먼저)
        resolved = _make_resolved_contract()

        async def fake_bootstrap(*a, **k):
            mc = MagicMock()
            mc._contract = resolved
            scheduler.krx_futures_client = mc

        with patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.object(scheduler, "resolve_active_krx_futures_contract",
                          new=AsyncMock(return_value=resolved)), \
             patch.object(scheduler, "shutdown_krx_futures_client", new=AsyncMock()) as mock_sd, \
             patch.object(scheduler, "_bootstrap_krx_futures_client",
                          new=AsyncMock(side_effect=fake_bootstrap)):
            await scheduler._reconcile_krx_futures_contract()

        mock_sd.assert_not_called()  # client-None path는 shutdown 안 함 (task-death 분기 미도달)
        self.assertEqual(scheduler._krx_reconcile_state["last_result"], "bootstrap_started")


if __name__ == "__main__":
    unittest.main(verbosity=2)
