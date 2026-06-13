"""topic_trigger_bridge tests (§6.6.2 C1 — worker thread → main loop bridge).

verification axis #1: main loop capture / shutdown·closed best-effort skip /
call_soon_threadsafe callback 예외 격리.
"""
from __future__ import annotations

import asyncio
import unittest

from app import topic_trigger_bridge as bridge


class TestScheduleGuards(unittest.TestCase):
    """순수 guard short-circuit (running loop 불필요)."""

    def setUp(self):
        bridge.reset_for_tests()

    def tearDown(self):
        bridge.reset_for_tests()

    def test_skip_when_no_loop_registered(self):
        self.assertFalse(bridge.schedule_on_loop(lambda: None))

    def test_skip_when_shutting_down(self):
        loop = asyncio.new_event_loop()
        try:
            bridge.register_main_loop(loop)
            bridge.signal_shutdown()
            self.assertFalse(bridge.schedule_on_loop(lambda: None))
        finally:
            loop.close()

    def test_skip_when_loop_closed(self):
        loop = asyncio.new_event_loop()
        loop.close()
        bridge.register_main_loop(loop)
        self.assertFalse(bridge.schedule_on_loop(lambda: None))


class TestScheduleOnRunningLoop(unittest.IsolatedAsyncioTestCase):
    """running loop 위에서 실제 마샬링 + 실행 + 예외 격리."""

    def setUp(self):
        bridge.reset_for_tests()

    def tearDown(self):
        bridge.reset_for_tests()

    async def test_callback_runs_on_registered_loop(self):
        bridge.register_main_loop(asyncio.get_running_loop())
        ran = []
        self.assertTrue(bridge.schedule_on_loop(ran.append, "x"))
        await asyncio.sleep(0.01)
        self.assertEqual(ran, ["x"])

    async def test_callback_exception_is_isolated(self):
        bridge.register_main_loop(asyncio.get_running_loop())
        ran = []

        def boom(v):
            ran.append(v)
            raise RuntimeError("boom")

        # schedule 자체는 성공, callback 예외는 _run_guarded가 흡수 → 테스트 무사.
        self.assertTrue(bridge.schedule_on_loop(boom, "y"))
        await asyncio.sleep(0.01)
        self.assertEqual(ran, ["y"])

    async def test_register_clears_shutdown_flag(self):
        bridge.signal_shutdown()
        bridge.register_main_loop(asyncio.get_running_loop())  # shutdown 해제
        ran = []
        self.assertTrue(bridge.schedule_on_loop(ran.append, "z"))
        await asyncio.sleep(0.01)
        self.assertEqual(ran, ["z"])

    async def test_skip_after_shutdown_signal(self):
        bridge.register_main_loop(asyncio.get_running_loop())
        bridge.signal_shutdown()
        ran = []
        self.assertFalse(bridge.schedule_on_loop(ran.append, "w"))
        await asyncio.sleep(0.01)
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
