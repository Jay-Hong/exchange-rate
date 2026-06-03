"""Gopax silent-session reconnect fix + USDT Redis regression guard 회귀 테스트.

대응 사고: 2026-05-28 15:52 Gopax WS가 frame 완전 정지(tick·heartbeat 모두)인데
ConnectionClosed 미발생 → `connection_status=stale`은 감지됐으나 no-op이라 영구 고착
(5일). 다른 4 source는 _ping_loop로 능동 close하지만 Gopax만 그 경로가 없었음.

이 파일이 잠그는 invariant:
    1. mid-session stale (tick·heartbeat 모두 silent) → _SilentSessionError → reconnect.
    2. no-first-tick (heartbeat만 와도 valid USDT-KRW tick 없음) → _SilentSessionError.
    3. 첫 valid tick → consecutive backoff attempt 리셋.
    4. backoff probe: cancel 시 cooldown 미소비 / 완료 시 cooldown 적용 (독립 lifecycle).
    5. USDT Redis writer 역행 가드: older bucket → SKIPPED_REGRESSION, same/newer → SET,
       복구 비차단, counter 증가.
"""
import asyncio
import json
import time
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app import latest_rates_cache, usdt_redis_stats
from app.crawlers.usdt_ws import gopax
from app.crawlers.usdt_ws.gopax import (
    GopaxRestFallbackController,
    GopaxWsClient,
    _SilentSessionError,
)
from app.latest_rates_cache import (
    UsdtLatestWriteOutcome,
    set_latest_usdt_rate_from_sync_job,
)


def _mock_connect(mock_ws):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_ws)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("app.crawlers.usdt_ws.gopax.websockets.connect", return_value=ctx)


def _valid_ticker_event(rate="1480", last_traded_ms=1780000000000):
    return json.dumps(
        {"n": "TickerEvent", "o": {"USDT-KRW": {"last": rate, "lastTraded": last_traded_ms}}}
    )


# ---------------------------------------------------------------------------
# 1·2. _run_one_session silent-session guards → _SilentSessionError
# ---------------------------------------------------------------------------


class TestSilentSessionGuards(unittest.IsolatedAsyncioTestCase):

    async def test_mid_session_stale_raises_silent_session_error(self):
        """tick·heartbeat 모두 silent(is_stale True) → _SilentSessionError('stale').

        기존엔 status 라벨만 바꾸는 no-op이라 영구 고착 = 5/28 사고. 이제 raise → reconnect.
        """
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(return_value="primus::ping::x")  # 가드가 recv 전에 raise
        # last_tick_at None + session 막 시작 → no-first-tick 가드는 안 뜸(0 < 30).
        # is_stale만 True로 만들어 mid-session stale 경로 격리.
        with _mock_connect(mock_ws), patch.object(
            client._liveness, "is_stale", return_value=True
        ):
            with self.assertRaises(_SilentSessionError) as cm:
                await asyncio.wait_for(client._run_one_session(), timeout=2.0)
        self.assertEqual(str(cm.exception), "stale")
        self.assertEqual(client._connection_status, "stale")

    async def test_no_first_tick_raises_silent_session_error(self):
        """heartbeat만 오고 valid tick 없음(last_tick_at None) → _SilentSessionError.

        FIRST_TICK_TIMEOUT 초과 시 — heartbeat-only 세션은 is_stale가 not-stale로
        두므로 (Codex) 이 가드가 따로 필요.
        """
        client = GopaxWsClient()
        mock_ws = AsyncMock()

        async def slow_send(*_a):
            await asyncio.sleep(0.05)  # now - session_started > timeout 보장

        mock_ws.send = AsyncMock(side_effect=slow_send)
        mock_ws.recv = AsyncMock(return_value="primus::ping::x")
        with _mock_connect(mock_ws), patch.object(gopax, "FIRST_TICK_TIMEOUT_SEC", 0.01):
            with self.assertRaises(_SilentSessionError) as cm:
                await asyncio.wait_for(client._run_one_session(), timeout=2.0)
        self.assertEqual(str(cm.exception), "no_first_tick")

    async def test_silent_session_error_caught_by_start_triggers_reconnect(self):
        """start()가 _SilentSessionError를 except Exception으로 잡아 reconnect (backoff)."""
        client = GopaxWsClient()
        calls = {"n": 0}

        async def fake_session():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _SilentSessionError("stale")
            client._stop_event.set()  # 2번째엔 정상 종료로 loop 탈출

        with patch.object(client, "_run_one_session", side_effect=fake_session), \
             patch.object(client, "_compute_backoff", return_value=0.0):
            await asyncio.wait_for(client.start(), timeout=2.0)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(client._reconnect_attempt_count, 1)


# ---------------------------------------------------------------------------
# 3. 첫 valid tick → consecutive backoff attempt 리셋
# ---------------------------------------------------------------------------


class TestBackoffResetOnFirstTick(unittest.IsolatedAsyncioTestCase):

    def test_handle_message_valid_tick_resets_consecutive_attempts(self):
        client = GopaxWsClient()
        client._consecutive_reconnect_attempts = 5
        tick = client._handle_message(_valid_ticker_event())
        self.assertIsNotNone(tick)
        self.assertEqual(client._consecutive_reconnect_attempts, 0)

    def test_handle_message_invalid_frame_keeps_attempts(self):
        """Primus/invalid frame은 tick None → 리셋 안 함 (valid target tick 기준)."""
        client = GopaxWsClient()
        client._consecutive_reconnect_attempts = 3
        tick = client._handle_message("primus::ping::x")
        self.assertIsNone(tick)
        self.assertEqual(client._consecutive_reconnect_attempts, 3)


# ---------------------------------------------------------------------------
# 4. backoff probe — cancel 시 cooldown 미소비 / 완료 시 cooldown 적용
# ---------------------------------------------------------------------------


def _make_controller():
    return GopaxRestFallbackController(
        redis_writer=MagicMock(),
        db_writer=MagicMock(),
        alert_evaluator=MagicMock(),
    )


class TestBackoffProbeLifecycle(unittest.IsolatedAsyncioTestCase):

    async def test_completion_sets_cooldown_and_fans_out(self):
        controller = _make_controller()
        tick = {"source": "gopax", "asset": "usdt-krw", "rate": 1480.0,
                "timestamp_ms": 1780000000000}
        with patch.object(controller, "_fetch_gopax_tick", return_value=tick):
            await controller.run_backoff_probe("test")
        self.assertGreater(controller._backoff_cooldown_until, 0.0)
        self.assertFalse(controller._backoff_in_flight)
        controller._redis_writer.schedule.assert_called_once_with(tick)
        controller._db_writer.schedule.assert_called_once_with(tick)
        controller._alert_evaluator.schedule.assert_called_once()

    async def test_cancel_does_not_consume_cooldown(self):
        """backoff 종료 cancel → cooldown 미소비 (REST 결과 없이 cooldown 먹기 방지, Codex)."""
        controller = _make_controller()

        def slow_fetch():
            time.sleep(0.5)  # to_thread 안에서 block, cancel 대상
            return None

        with patch.object(controller, "_fetch_gopax_tick", side_effect=slow_fetch):
            task = asyncio.create_task(controller.run_backoff_probe("test"))
            await asyncio.sleep(0.05)
            self.assertTrue(controller._backoff_in_flight)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(controller._backoff_cooldown_until, 0.0)
        self.assertFalse(controller._backoff_in_flight)
        controller._redis_writer.schedule.assert_not_called()

    async def test_cooldown_skips_second_probe(self):
        controller = _make_controller()
        tick = {"source": "gopax", "asset": "usdt-krw", "rate": 1480.0,
                "timestamp_ms": 1780000000000}
        with patch.object(controller, "_fetch_gopax_tick", return_value=tick) as mock_fetch:
            await controller.run_backoff_probe("first")
            await controller.run_backoff_probe("second")  # cooldown → skip
        mock_fetch.assert_called_once()  # 두 번째는 cooldown으로 fetch 안 함

    async def test_separate_from_session_scoped_cooldown(self):
        """backoff cooldown은 session-scoped _cooldown_until과 독립."""
        controller = _make_controller()
        controller._cooldown_until = time.time() + 999  # session probe cooldown 진행 중
        tick = {"source": "gopax", "asset": "usdt-krw", "rate": 1480.0,
                "timestamp_ms": 1780000000000}
        with patch.object(controller, "_fetch_gopax_tick", return_value=tick) as mock_fetch:
            await controller.run_backoff_probe("test")  # session cooldown 무관하게 실행
        mock_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# 5. USDT Redis writer — timestamp 역행 가드 (SKIPPED_REGRESSION)
# ---------------------------------------------------------------------------


class TestRedisRegressionGuard(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = MagicMock()
        latest_rates_cache._last_written_usdt_state.clear()
        latest_rates_cache._last_regression_warn_at.clear()
        usdt_redis_stats.reset_stats()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()
        latest_rates_cache._last_regression_warn_at.clear()

    def _seed_stored(self, rate, iso):
        ts = latest_rates_cache._parse_kst(iso)
        floor = latest_rates_cache._floor_5s(ts)
        latest_rates_cache._last_written_usdt_state[("gopax", "usdt-krw")] = {
            "rate": Decimal(str(rate)),
            "seen_at": floor,
            "rate_changed_at": floor,
        }

    def _write(self, rate, iso):
        return set_latest_usdt_rate_from_sync_job("gopax", "usdt-krw", rate, iso)

    def test_older_bucket_different_rate_skipped_regression(self):
        self._seed_stored(1480.0, "2026-06-03T20:00:00+09:00")
        with patch.object(latest_rates_cache.logger, "warning"):
            out = self._write(1475.0, "2026-06-03T19:50:00+09:00")
        self.assertIs(out, UsdtLatestWriteOutcome.SKIPPED_REGRESSION)
        latest_rates_cache._sync_client.set.assert_not_called()

    def test_same_bucket_different_rate_sets(self):
        """동일 5s bucket + 다른 rate → SET (< strict라 == bucket은 가드 미발동)."""
        self._seed_stored(1480.0, "2026-06-03T20:00:00+09:00")
        out = self._write(1485.0, "2026-06-03T20:00:02+09:00")
        self.assertIs(out, UsdtLatestWriteOutcome.SET)

    def test_newer_bucket_sets(self):
        self._seed_stored(1480.0, "2026-06-03T20:00:00+09:00")
        out = self._write(1481.0, "2026-06-03T20:05:00+09:00")
        self.assertIs(out, UsdtLatestWriteOutcome.SET)

    def test_recovery_not_blocked(self):
        """5/28 frozen stored + fresh reconnect snapshot → SET (가드가 복구를 막지 않음)."""
        self._seed_stored(1475.0, "2026-05-28T15:52:50+09:00")
        out = self._write(1479.0, "2026-06-03T20:12:35+09:00")
        self.assertIs(out, UsdtLatestWriteOutcome.SET)

    def test_regression_increments_counter(self):
        self._seed_stored(1480.0, "2026-06-03T20:00:00+09:00")
        with patch.object(latest_rates_cache.logger, "warning"):
            self._write(1475.0, "2026-06-03T19:50:00+09:00")
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(
            stats["per_source"]["gopax"]["direct_write_regression_skipped"], 1
        )

    def test_regression_warning_throttled(self):
        """counter는 매번, warning은 throttle (반복 stale snapshot 로그 폭주 회피)."""
        self._seed_stored(1480.0, "2026-06-03T20:00:00+09:00")
        with patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            for _ in range(5):
                self._write(1475.0, "2026-06-03T19:50:00+09:00")
        self.assertEqual(mock_warn.call_count, 1)  # 5회 중 1회만 warning
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(
            stats["per_source"]["gopax"]["direct_write_regression_skipped"], 5
        )


# ---------------------------------------------------------------------------
# 6. legacy polling adapter — SKIPPED_REGRESSION을 success로 취급
# ---------------------------------------------------------------------------


class TestLegacyAdapterOutcome(unittest.TestCase):

    def test_skipped_regression_treated_as_success(self):
        """adapter는 FAILED만 False — regression skip은 기존 latest 보존이라 success."""
        from app.crawlers import usdt_sources

        with patch.object(usdt_sources, "crud") as mock_crud, \
             patch.object(
                 usdt_sources.latest_rates_cache,
                 "set_latest_usdt_rate_from_sync_job",
                 return_value=UsdtLatestWriteOutcome.SKIPPED_REGRESSION,
             ):
            mock_crud.get_latest_source_rate.return_value = {
                "rate": 1480.0, "timestamp": "2026-06-03T20:00:00+09:00",
            }
            result = usdt_sources._mirror_changed_source_to_redis(
                MagicMock(), "gopax", "usdt-krw"
            )
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
