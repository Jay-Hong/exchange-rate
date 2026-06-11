"""§12.9.8 ② — 5-source USDT WS collector task supervisor 단위 테스트.

①(silent-stale reconnect)이 살아있는 task 안의 reconnect라 미커버하는 "task 자체가
죽는" 경우를 메우는 watchdog. 죽은 task(done) 감지 → shutdown_*(teardown) → start_*
(fresh) 재시작. shutdown race / backoff / reset / env gate 검증.

테스트 매트릭스:
  1. crash(done)+enabled → shutdown_*→start_* 재시작 (+ #8 순서)
  2. 정상 running(not done) → skip (중복 방지)
  3. disabled → skip
  4. task None(미시작/정상종료) → skip
  5. shutting_down flag → 전체 skip (shutdown race 차단)
  6. backoff: 연속 crash → consecutive escalate + throttle
  7. backoff cap → 영구 포기 X (300s 상한)
  8. reset: 마지막 restart 이후 5분+ 생존 → counter reset
  9. reset 무력화 방지: 5분 미생존(crash-loop) → reset 안 함
  10. per-source 격리: 한 source 실패가 다른 source 막지 않음
  11. env gate default off (배포 ≠ 동작 변화)
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import AsyncMock, patch

from app import config, scheduler


def _mk_task(done: bool):
    class _T:
        def done(self_inner):
            return done
    return _T()


def _registry(name="upbit", *, enabled=True, task=None, shutdown_fn=None, start_fn=None):
    """단일 source 테스트 registry — closure로 flag/task 고정."""
    return [(name, lambda: enabled, shutdown_fn, start_fn, lambda: task)]


class TestSupervisorTick(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        scheduler.reset_usdt_ws_supervisor_state()

    async def test_dead_task_restarts_via_shutdown_then_start(self):
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=_mk_task(done=True), shutdown_fn=shutdown_fn, start_fn=start_fn)
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        shutdown_fn.assert_awaited_once()
        start_fn.assert_awaited_once()
        st = scheduler._usdt_ws_supervisor_state["upbit"]
        self.assertEqual(st["consecutive"], 1)
        self.assertIsNotNone(st["last_restart_at"])

    async def test_restart_calls_shutdown_before_start(self):
        # #8 — teardown 먼저, 그 후 fresh start (잔여물 정리)
        order = []

        async def sd():
            order.append("shutdown")

        async def st():
            order.append("start")

        reg = _registry(task=_mk_task(done=True), shutdown_fn=sd, start_fn=st)
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        self.assertEqual(order, ["shutdown", "start"])

    async def test_running_task_skipped(self):
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=_mk_task(done=False), shutdown_fn=shutdown_fn, start_fn=start_fn)
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        shutdown_fn.assert_not_awaited()
        start_fn.assert_not_awaited()

    async def test_disabled_source_skipped(self):
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(enabled=False, task=_mk_task(done=True),
                        shutdown_fn=shutdown_fn, start_fn=start_fn)
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        shutdown_fn.assert_not_awaited()
        start_fn.assert_not_awaited()

    async def test_none_task_skipped(self):
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=None, shutdown_fn=shutdown_fn, start_fn=start_fn)
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        shutdown_fn.assert_not_awaited()
        start_fn.assert_not_awaited()

    async def test_shutting_down_skips_restart(self):
        # shutdown race 차단 — flag set 시 done task여도 재시작 안 함
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=_mk_task(done=True), shutdown_fn=shutdown_fn, start_fn=start_fn)
        scheduler.signal_collector_shutdown_initiated()
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        shutdown_fn.assert_not_awaited()
        start_fn.assert_not_awaited()

    async def test_backoff_throttle_blocks_restart(self):
        # next_allowed_at 미래 → done task여도 throttle (restart 안 함)
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=_mk_task(done=True), shutdown_fn=shutdown_fn, start_fn=start_fn)
        now = time.time()
        scheduler._usdt_ws_supervisor_state["upbit"] = {
            "consecutive": 2, "last_restart_at": now, "next_allowed_at": now + 1000,
        }
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        shutdown_fn.assert_not_awaited()
        self.assertEqual(scheduler._usdt_ws_supervisor_state["upbit"]["consecutive"], 2)

    async def test_consecutive_escalates_then_throttles(self):
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=_mk_task(done=True), shutdown_fn=shutdown_fn, start_fn=start_fn)
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()  # consecutive 1, backoff(1)=0
            self.assertEqual(scheduler._usdt_ws_supervisor_state["upbit"]["consecutive"], 1)
            await scheduler._usdt_ws_supervisor_tick()  # now>=next_allowed → consecutive 2, backoff 60
            self.assertEqual(scheduler._usdt_ws_supervisor_state["upbit"]["consecutive"], 2)
            await scheduler._usdt_ws_supervisor_tick()  # now<next_allowed(+60) → throttle
            self.assertEqual(scheduler._usdt_ws_supervisor_state["upbit"]["consecutive"], 2)
        self.assertEqual(start_fn.await_count, 2)  # 3번째는 throttle

    async def test_reset_after_5min_survival(self):
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=_mk_task(done=False), shutdown_fn=shutdown_fn, start_fn=start_fn)
        now = time.time()
        scheduler._usdt_ws_supervisor_state["upbit"] = {
            "consecutive": 3, "last_restart_at": now - 301, "next_allowed_at": now - 200,
        }
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        self.assertEqual(scheduler._usdt_ws_supervisor_state["upbit"]["consecutive"], 0)
        self.assertIsNone(scheduler._usdt_ws_supervisor_state["upbit"]["last_restart_at"])

    async def test_no_reset_before_5min_crashloop(self):
        # 45s-crash-loop 방어: 5분 미생존이면 alive 관측해도 counter 유지
        shutdown_fn, start_fn = AsyncMock(), AsyncMock()
        reg = _registry(task=_mk_task(done=False), shutdown_fn=shutdown_fn, start_fn=start_fn)
        now = time.time()
        scheduler._usdt_ws_supervisor_state["upbit"] = {
            "consecutive": 3, "last_restart_at": now - 30, "next_allowed_at": now - 10,
        }
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        self.assertEqual(scheduler._usdt_ws_supervisor_state["upbit"]["consecutive"], 3)

    async def test_per_source_isolation(self):
        # upbit shutdown 실패 → gopax는 정상 처리
        bad_sd = AsyncMock(side_effect=RuntimeError("boom"))
        good_sd, good_st = AsyncMock(), AsyncMock()
        reg = [
            ("upbit", lambda: True, bad_sd, AsyncMock(), lambda: _mk_task(done=True)),
            ("gopax", lambda: True, good_sd, good_st, lambda: _mk_task(done=True)),
        ]
        with patch.object(scheduler, "_USDT_WS_SUPERVISOR_REGISTRY", reg):
            await scheduler._usdt_ws_supervisor_tick()
        good_sd.assert_awaited_once()
        good_st.assert_awaited_once()


class TestSupervisorHelpers(unittest.TestCase):

    def setUp(self):
        scheduler.reset_usdt_ws_supervisor_state()

    def test_compute_backoff_values(self):
        self.assertEqual(scheduler._compute_supervisor_backoff(1), 0.0)
        self.assertEqual(scheduler._compute_supervisor_backoff(2), 60.0)
        self.assertEqual(scheduler._compute_supervisor_backoff(3), 120.0)
        self.assertEqual(scheduler._compute_supervisor_backoff(4), 240.0)

    def test_compute_backoff_caps_no_giveup(self):
        # cap 300s — 영구 포기 X (큰 consecutive에도 유한 + 재시도 유지)
        self.assertEqual(scheduler._compute_supervisor_backoff(5), 300.0)
        self.assertEqual(scheduler._compute_supervisor_backoff(100), 300.0)

    def test_signal_shutdown_sets_flag(self):
        self.assertFalse(scheduler._collector_shutdown_initiated)
        scheduler.signal_collector_shutdown_initiated()
        self.assertTrue(scheduler._collector_shutdown_initiated)

    def test_registry_has_five_usdt_sources(self):
        names = [e[0] for e in scheduler._USDT_WS_SUPERVISOR_REGISTRY]
        self.assertEqual(names, ["upbit", "bithumb", "coinone", "korbit", "gopax"])

    def test_env_gate_default_off(self):
        # 배포 ≠ 동작 변화 — default false
        self.assertFalse(config.USDT_WS_SUPERVISOR_ENABLED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
