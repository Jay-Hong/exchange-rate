"""KRX close finalizer Stage 4 — KrxCloseSnapshotController._retry_sequence env-gated.

KRX_CLOSE_SNAPSHOT_PLAN §5.3 (2026-05-17). 6 tests:
    - env true entry captured → _attempt_once 호출 0, counter unchanged
    - env true not captured → _attempt_once 1회, counter = 1
    - env true captured during wait → _attempt_once 호출 0, counter unchanged
    - env true REST 실패 → _attempt_once 1회 (3 X), counter = 1
    - env false → legacy 3 retry, flag GET 호출 없음, counter unchanged
    - cross-cutting: counter == _attempt_once.call_count invariant

Codex invariant (Stage 4 핵심):
    close_rest_fallback_used == _attempt_once.call_count (env true)
    close_rest_fallback_used 미증가 (env false)

시간 fixture: 과거 boundary (즉시 실행) + asyncio.sleep mock + flag side_effect 분리.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from app.sources.kis_master import ContractInfo

KST = ZoneInfo("Asia/Seoul")


def _make_contract():
    return ContractInfo(
        short_code="A75605",
        standard_code="KR4A75650007",
        name="미국달러 F 202605",
        contract_month="202605",
        expiry_date=date(2026, 5, 18),
    )


def _make_past_boundary(seconds_ago: int = 120) -> datetime:
    """과거 boundary — _retry_sequence 진입 시 elapsed > delay → sleep skip 분기."""
    return datetime.now(KST) - timedelta(seconds=seconds_ago)


class TestRetrySequenceEnvTrue(unittest.IsolatedAsyncioTestCase):
    """Stage 4 acceptance — env true 경로 invariant 검증."""

    def setUp(self):
        from app.crawlers.krx_kis import (
            KrxCloseSnapshotController,
            KisAccessTokenManager,
            reset_krx_close_finalizer_stats_for_tests,
        )
        reset_krx_close_finalizer_stats_for_tests()
        self._token_mgr = MagicMock(spec=KisAccessTokenManager)
        self._controller = KrxCloseSnapshotController(token_manager=self._token_mgr)
        # env true patcher (default — 각 test에서 override 가능)
        self._env_patcher = patch.object(
            __import__("app.config", fromlist=["KRX_CLOSE_FINALIZER_ENABLED"]),
            "KRX_CLOSE_FINALIZER_ENABLED", True,
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    async def test_skipped_when_captured_at_entry(self):
        """env true + entry flag SET → _attempt_once 호출 0 + counter 0."""
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats

        contract = _make_contract()
        boundary = _make_past_boundary()

        with patch(
            "app.latest_rates_cache.get_krx_close_captured_flag",
            return_value=True,
        ) as flag_mock, patch.object(
            self._controller, "_attempt_once",
        ) as attempt_mock:
            await self._controller._retry_sequence(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )

        # Entry flag GET 1회만 호출 (sleep 후 재확인 없음, return)
        flag_mock.assert_called_once_with("CF", boundary.astimezone(KST).date().isoformat())
        # _attempt_once 호출 0
        attempt_mock.assert_not_called()
        # Invariant: counter == call_count
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_rest_fallback_used, attempt_mock.call_count)
        self.assertEqual(stats.close_rest_fallback_used, 0)

    async def test_used_when_not_captured(self):
        """env true + flag absent → _attempt_once 1회 + counter 1."""
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats

        contract = _make_contract()
        boundary = _make_past_boundary()

        attempt_mock = AsyncMock(return_value=True)
        with patch(
            "app.latest_rates_cache.get_krx_close_captured_flag",
            return_value=False,
        ), patch.object(self._controller, "_attempt_once", attempt_mock):
            await self._controller._retry_sequence(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )

        self.assertEqual(attempt_mock.await_count, 1)
        stats = get_krx_close_finalizer_stats()
        # Invariant: counter == call_count
        self.assertEqual(stats.close_rest_fallback_used, attempt_mock.await_count)
        self.assertEqual(stats.close_rest_fallback_used, 1)

    async def test_skipped_when_captured_during_wait(self):
        """env true + entry False + sleep 후 True → _attempt_once 호출 0 + counter 0.

        sleep mock + flag side_effect [False, True] — captured during wait 케이스.
        """
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats

        contract = _make_contract()
        # 미래 boundary로 sleep 진입 유도 (sleep mock으로 즉시 진행)
        boundary = datetime.now(KST) + timedelta(seconds=30)

        sleep_mock = AsyncMock()
        attempt_mock = AsyncMock(return_value=True)
        with patch("asyncio.sleep", sleep_mock), patch(
            "app.latest_rates_cache.get_krx_close_captured_flag",
            side_effect=[False, True],  # entry False, sleep 후 True
        ) as flag_mock, patch.object(
            self._controller, "_attempt_once", attempt_mock,
        ):
            await self._controller._retry_sequence(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )

        # Flag GET 2회 (entry + sleep 후)
        self.assertEqual(flag_mock.call_count, 2)
        # sleep 호출 (mock으로 즉시 진행)
        sleep_mock.assert_awaited()
        # _attempt_once 호출 0 (captured during wait)
        attempt_mock.assert_not_called()
        # Invariant: counter == call_count
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_rest_fallback_used, attempt_mock.await_count)
        self.assertEqual(stats.close_rest_fallback_used, 0)

    async def test_env_on_single_retry_then_exhausted(self):
        """env true + REST 실패 → _attempt_once 1회만 (3 X) + exhausted + counter 1."""
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats

        contract = _make_contract()
        boundary = _make_past_boundary()

        attempt_mock = AsyncMock(return_value=False)  # 실패 시뮬레이션
        with patch(
            "app.latest_rates_cache.get_krx_close_captured_flag",
            return_value=False,
        ), patch.object(self._controller, "_attempt_once", attempt_mock):
            await self._controller._retry_sequence(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )

        # 1회만 시도 (env true는 delays[:1])
        self.assertEqual(attempt_mock.await_count, 1)
        # exhausted counter ++
        self.assertEqual(self._controller.counters["exhausted"], 1)
        # Invariant: counter == call_count (실패도 호출 1회)
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_rest_fallback_used, attempt_mock.await_count)
        self.assertEqual(stats.close_rest_fallback_used, 1)


class TestRetrySequenceEnvFalse(unittest.IsolatedAsyncioTestCase):
    """Stage 4 acceptance — env false 경로 (legacy rollback)."""

    def setUp(self):
        from app.crawlers.krx_kis import (
            KrxCloseSnapshotController,
            KisAccessTokenManager,
            reset_krx_close_finalizer_stats_for_tests,
        )
        reset_krx_close_finalizer_stats_for_tests()
        token_mgr = MagicMock(spec=KisAccessTokenManager)
        self._controller = KrxCloseSnapshotController(
            token_manager=token_mgr,
            retry_delays_sec={"CF": [0.0, 0.0, 0.0], "CM": [0.0, 0.0, 0.0]},
        )

    async def test_env_off_keeps_legacy_3retry_no_flag_get(self):
        """env false → 3 retry 유지 + flag GET 호출 없음 + counter 미증가 (legacy rollback)."""
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats
        from app import config

        contract = _make_contract()
        boundary = _make_past_boundary()

        attempt_mock = AsyncMock(return_value=False)  # 모든 attempt 실패 → 3 retry 다 시도
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False), patch(
            "app.latest_rates_cache.get_krx_close_captured_flag",
        ) as flag_mock, patch.object(self._controller, "_attempt_once", attempt_mock):
            await self._controller._retry_sequence(
                contract=contract, session="CF", boundary_at_kst=boundary,
            )

        # env false → flag GET 호출 안 함 (legacy path)
        flag_mock.assert_not_called()
        # 3 retry 다 시도
        self.assertEqual(attempt_mock.await_count, 3)
        # exhausted counter ++
        self.assertEqual(self._controller.counters["exhausted"], 1)
        # env false: close_rest_fallback_used 미증가 (legacy rollback path)
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_rest_fallback_used, 0)


class TestCounterInvariantCrossCutting(unittest.IsolatedAsyncioTestCase):
    """Stage 4 핵심 invariant cross-cutting 검증.

    close_rest_fallback_used == _attempt_once.call_count (env true 모든 경로)
    """

    def setUp(self):
        from app.crawlers.krx_kis import (
            KrxCloseSnapshotController,
            KisAccessTokenManager,
            reset_krx_close_finalizer_stats_for_tests,
        )
        reset_krx_close_finalizer_stats_for_tests()
        self._token_mgr = MagicMock(spec=KisAccessTokenManager)
        self._controller = KrxCloseSnapshotController(token_manager=self._token_mgr)
        self._env_patcher = patch.object(
            __import__("app.config", fromlist=["KRX_CLOSE_FINALIZER_ENABLED"]),
            "KRX_CLOSE_FINALIZER_ENABLED", True,
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    async def test_counter_increment_only_on_actual_rest_call(self):
        """3개 시나리오에서 counter == attempt call count 보장.

        시나리오:
          (a) entry captured: 0 == 0
          (b) not captured + success: 1 == 1
          (c) entry False + during-wait True: 0 == 0
        """
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats, reset_krx_close_finalizer_stats_for_tests

        contract = _make_contract()

        # (a) entry captured
        boundary_a = _make_past_boundary()
        with patch("app.latest_rates_cache.get_krx_close_captured_flag", return_value=True), \
             patch.object(self._controller, "_attempt_once") as attempt_a:
            await self._controller._retry_sequence(
                contract=contract, session="CF", boundary_at_kst=boundary_a,
            )
        stats_a = get_krx_close_finalizer_stats()
        self.assertEqual(stats_a.close_rest_fallback_used, attempt_a.call_count)
        self.assertEqual(stats_a.close_rest_fallback_used, 0)

        # (b) not captured + success
        reset_krx_close_finalizer_stats_for_tests()
        boundary_b = _make_past_boundary()
        attempt_b = AsyncMock(return_value=True)
        with patch("app.latest_rates_cache.get_krx_close_captured_flag", return_value=False), \
             patch.object(self._controller, "_attempt_once", attempt_b):
            await self._controller._retry_sequence(
                contract=contract, session="CM", boundary_at_kst=boundary_b,
            )
        stats_b = get_krx_close_finalizer_stats()
        self.assertEqual(stats_b.close_rest_fallback_used, attempt_b.await_count)
        self.assertEqual(stats_b.close_rest_fallback_used, 1)

        # (c) captured during wait
        reset_krx_close_finalizer_stats_for_tests()
        boundary_c = datetime.now(KST) + timedelta(seconds=30)
        attempt_c = AsyncMock(return_value=True)
        with patch("asyncio.sleep", AsyncMock()), \
             patch("app.latest_rates_cache.get_krx_close_captured_flag",
                   side_effect=[False, True]), \
             patch.object(self._controller, "_attempt_once", attempt_c):
            await self._controller._retry_sequence(
                contract=contract, session="CF", boundary_at_kst=boundary_c,
            )
        stats_c = get_krx_close_finalizer_stats()
        self.assertEqual(stats_c.close_rest_fallback_used, attempt_c.await_count)
        self.assertEqual(stats_c.close_rest_fallback_used, 0)


if __name__ == "__main__":
    unittest.main()
