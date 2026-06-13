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

    async def test_drain_barrier_runs_queued_callback_and_lock_blocks_new(self):
        """shutdown lifecycle: 큐된 callback은 barrier로 flush, 신규는 lock으로 차단.

        orphan 방지 핵심 — drain_loop_callbacks가 이미 큐된 callback을 fx drain
        전에 실행시키고, signal_shutdown 후 schedule은 enqueue 자체가 안 됨.
        """
        bridge.register_main_loop(asyncio.get_running_loop())
        ran = []
        # 1) shutdown 전 callback enqueue (큐 대기 상태)
        self.assertTrue(bridge.schedule_on_loop(ran.append, "queued"))
        # 2) shutdown signal → lock으로 신규 enqueue 차단
        bridge.signal_shutdown()
        self.assertFalse(bridge.schedule_on_loop(ran.append, "after"))  # 차단됨
        # 3) barrier → 큐된 callback 실행 완료 보장 (fx drain 전)
        await bridge.drain_loop_callbacks()
        self.assertEqual(ran, ["queued"])  # queued 실행 / after 미실행


class TestShutdownOrphanRegression(unittest.IsolatedAsyncioTestCase):
    """원래 orphan race를 end-to-end로 재현 — main.py shutdown 순서 회귀 탐지.

    시퀀스: bridge로 큐된 emission callback이 fx flush task를 만들고,
    signal → barrier → fx close 순서가 그 task를 orphan 없이 drain하는지.
    barrier가 fx close 앞이 아니면(순서 회귀) task가 close 뒤 생성 → orphan → 실패.
    """

    def setUp(self):
        bridge.reset_for_tests()

    def tearDown(self):
        bridge.reset_for_tests()

    async def test_shutdown_sequence_drains_bridge_queued_fx_task(self):
        import app.fx_topic_trigger as fxt

        bridge.register_main_loop(asyncio.get_running_loop())
        published = []

        async def slow_publish(asset):
            published.append(asset)
            return True

        controller = fxt.FxTopicTriggerController(
            mode="direct_coalesced", coalesce_ms=5, publish_func=slow_publish
        )
        fxt.reset_fx_topic_trigger_for_tests(controller)
        try:
            # emission callback을 bridge로 큐 (아직 실행 X = 원래 race 진입점)
            def emit():
                fxt.request_fx_topic_trigger(
                    "kb", "usd-krw", fxt.FX_TRIGGER_REASON_BANK_CHANGE
                )

            self.assertTrue(bridge.schedule_on_loop(emit))

            # main.py shutdown 순서 재현: signal → barrier → fx close
            bridge.signal_shutdown()
            await bridge.drain_loop_callbacks()
            # barrier 후 emit 실행됨 → fx flush task 존재 (race였다면 close가 못 봄)
            self.assertEqual(controller.pending_count, 1)

            await fxt.shutdown_fx_topic_trigger()  # close → drain
            await asyncio.sleep(0.01)  # orphan callback이 더 없는지 yield

            self.assertEqual(controller.pending_count, 0)
            self.assertEqual(published, ["usd-krw"])  # orphan 아닌 정상 drain
        finally:
            fxt.reset_fx_topic_trigger_for_tests(None)


if __name__ == "__main__":
    unittest.main()
