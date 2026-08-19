"""LOAD-S3 — 요청 absolute deadline과 sync worker 협력 중단 행동 검증."""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import subprocess
import sys
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app import config, subscribe_load_metrics, topic_dispatcher
from app.topic_initial_snapshot import (
    SnapshotDeadlineExceeded,
    SnapshotFailureCooldownActive,
    SnapshotRequestBudget,
    SnapshotWorkerStopped,
    _run_snapshot_worker,
    build_snapshot_observed,
    send_initial_snapshots,
)
from app.topic_wire import (
    InitialSnapshotDeadlineExceeded,
    InitialSnapshotFatalFailure,
    InitialSnapshotTransientFailure,
)


class _Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def _budget(clock, *, snapshot=1.0, request=5.0):
    return SnapshotRequestBudget(
        started_at_mono=0.0,
        ack_deadline_seconds=2.0,
        snapshot_budget_seconds=snapshot,
        request_deadline_seconds=request,
        clock=clock,
    )


class TestSnapshotRequestBudget(unittest.TestCase):
    def test_snapshot_deadline_starts_once_and_is_clamped_by_request(self):
        clock = _Clock(1.0)
        budget = _budget(clock, snapshot=2.0, request=10.0)
        first = budget.start_snapshot_phase()
        clock.value = 2.0
        self.assertEqual(first, 3.0)
        self.assertEqual(budget.start_snapshot_phase(), first, "topic마다 예산이 리셋됐다")
        self.assertEqual(budget.remaining_snapshot_seconds(), 1.0)

    def test_invalid_or_inverted_budgets_fail_closed(self):
        for kwargs in (
            {"ack_deadline_seconds": 0.0},
            {"snapshot_budget_seconds": float("inf")},
            {"ack_deadline_seconds": 5.0, "request_deadline_seconds": 5.0},
            {"snapshot_budget_seconds": 6.0, "request_deadline_seconds": 5.0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SnapshotRequestBudget(started_at_mono=0.0, clock=_Clock(), **kwargs)

    def test_auth_budget_equal_to_ack_budget_is_rejected_at_config_load(self):
        env = os.environ.copy()
        env["WS_TOPIC_ACK_DEADLINE_SECONDS"] = str(
            config.WS_AUTH_WIRE_DEADLINE_SECONDS
        )
        proc = subprocess.run(
            [sys.executable, "-c", "import app.config"],
            cwd=pathlib.Path(__file__).resolve().parent.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "auth deadline must be less than the topic ACK deadline",
            proc.stderr,
        )

    def test_queue_expiry_prevents_session_creation(self):
        clock = _Clock()
        budget = _budget(clock)
        budget.start_snapshot_phase()
        clock.value = 2.0
        with patch("app.database.SessionLocal") as session:
            with self.assertRaises(SnapshotWorkerStopped):
                _run_snapshot_worker("fx:usd-krw", budget)
        session.assert_not_called()


class TestSnapshotWorkerCooperativeStop(unittest.TestCase):
    def test_deadline_after_first_redis_call_prevents_the_second(self):
        clock = _Clock()
        budget = _budget(clock)
        budget.start_snapshot_phase()
        db = MagicMock()

        def first_call_moves_past_deadline(*args, **kwargs):
            clock.value = 2.0
            return {
                "bank": "kb", "currency": "usd-krw", "rate": 1400.0,
                "timestamp": "2026-08-19T00:00:00Z",
            }

        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch("app.database.SessionLocal", return_value=db), \
             patch(
                 "app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                 side_effect=first_call_moves_past_deadline,
             ) as redis_get:
            with self.assertRaises(SnapshotWorkerStopped):
                _run_snapshot_worker("fx:usd-krw", budget)

        self.assertEqual(redis_get.call_count, 1, "deadline 뒤 두 번째 Redis GET을 시작했다")
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    def test_usdt_deadline_prevents_second_exchange_redis_call(self):
        from app import usdt_topic_payload

        clock = _Clock()
        budget = _budget(clock)
        budget.start_snapshot_phase()

        def first_call_moves_past_deadline(*args, **kwargs):
            clock.value = 2.0
            return {
                "source": "upbit", "asset": "usdt-krw", "rate": 1400.0,
                "timestamp": "2026-08-19T00:00:00Z",
            }

        with patch.object(
            usdt_topic_payload,
            "get_latest_usdt_rate_from_sync_job",
            side_effect=first_call_moves_past_deadline,
        ) as redis_get, patch.object(
            usdt_topic_payload, "get_latest_source_rates_for_topic"
        ) as db_get:
            with self.assertRaises(SnapshotWorkerStopped):
                usdt_topic_payload.load_and_build_tether_tab_payload(
                    MagicMock(), checkpoint=budget.checkpoint
                )
        self.assertEqual(redis_get.call_count, 1)
        db_get.assert_not_called()

    def test_krx_deadline_prevents_db_fallback_after_redis_miss(self):
        from app.krx_topic_publisher import load_krx_topic_entry

        clock = _Clock()
        budget = _budget(clock)
        budget.start_snapshot_phase()

        def miss_moves_past_deadline(*args, **kwargs):
            clock.value = 2.0
            return None

        with patch(
            "app.latest_rates_cache.get_latest_krx_rate_from_sync_job",
            side_effect=miss_moves_past_deadline,
        ), patch("app.crud.get_latest_source_rate") as db_get:
            with self.assertRaises(SnapshotWorkerStopped):
                load_krx_topic_entry(MagicMock(), checkpoint=budget.checkpoint)
        db_get.assert_not_called()


class TestSnapshotCallerCancellation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with subscribe_load_metrics._lock:
            subscribe_load_metrics._metrics.clear()
            subscribe_load_metrics._metrics.update(subscribe_load_metrics._blank())

    async def test_cancelled_caller_does_not_claim_early_session_release(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        db = MagicMock()
        db.close.side_effect = closed.set
        stop_seen_before_observe_exit = []
        budget = SnapshotRequestBudget(
            ack_deadline_seconds=5.0,
            snapshot_budget_seconds=10.0,
            request_deadline_seconds=20.0,
        )

        def stalled_first_redis(*args, **kwargs):
            entered.set()
            release.wait(timeout=2.0)
            return {
                "bank": "kb", "currency": "usd-krw", "rate": 1400.0,
                "timestamp": "2026-08-19T00:00:00Z",
            }

        real_observe = subscribe_load_metrics.observe

        @contextlib.asynccontextmanager
        async def recording_observe(axis):
            async with real_observe(axis) as handle:
                try:
                    yield handle
                finally:
                    stop_seen_before_observe_exit.append(budget.stop_reason)

        with patch.object(config, "FX_TOPIC_ENABLED", True), \
             patch("app.database.SessionLocal", return_value=db), \
             patch.object(
                 subscribe_load_metrics, "observe", new=recording_observe
             ), \
             patch(
                 "app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                 side_effect=stalled_first_redis,
             ):
            task = asyncio.create_task(
                build_snapshot_observed("fx:usd-krw", budget=budget)
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(budget.stop_reason, "caller_cancelled")
            self.assertEqual(
                stop_seen_before_observe_exit,
                ["caller_cancelled"],
                "worker stop 신호가 계측 context 정리보다 늦게 전달됐다",
            )
            self.assertFalse(
                closed.is_set(),
                "caller 취소 직후 worker Session이 반환됐다고 거짓 관측했다",
            )
            release.set()
            self.assertTrue(await asyncio.to_thread(closed.wait, 1.0))

        db.rollback.assert_called_once()
        db.close.assert_called_once()
        for _ in range(100):
            snap = subscribe_load_metrics.subscribe_load_metrics()["snapshot_build"]
            if snap["worker_started_total"] == snap["worker_finished_total"]:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(snap["callers_awaiting"], 0)
        self.assertEqual(snap["worker_started_total"], snap["worker_finished_total"])

    async def test_cancelled_worker_returns_real_db_pool_checkout_after_phase(self):
        engine = create_engine(
            "sqlite://",
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            connect_args={"check_same_thread": False},
        )
        session_factory = sessionmaker(bind=engine)
        entered = threading.Event()
        release = threading.Event()
        worker_finished = threading.Event()
        budget = SnapshotRequestBudget(
            ack_deadline_seconds=5.0,
            snapshot_budget_seconds=10.0,
            request_deadline_seconds=20.0,
        )

        def stalled_statement(db, _asset):
            db.execute(text("SELECT 1")).scalar_one()
            entered.set()
            release.wait(timeout=2.0)
            return []

        try:
            with patch.object(config, "FX_TOPIC_ENABLED", True), \
                 patch("app.database.SessionLocal", side_effect=session_factory), \
                 patch(
                     "app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                     return_value=None,
                 ), \
                 patch(
                     "app.fx_topic_payload.select_latest_bank_rates_from_db",
                     side_effect=stalled_statement,
                 ):
                task = asyncio.create_task(
                    build_snapshot_observed("fx:usd-krw", budget=budget)
                )
                self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
                self.assertEqual(engine.pool.checkedout(), 1)

                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(
                    engine.pool.checkedout(), 1,
                    "caller 취소를 worker connection 반환으로 오인했다",
                )

                release.set()
                for _ in range(200):
                    snap = subscribe_load_metrics.subscribe_load_metrics()["snapshot_build"]
                    if (
                        engine.pool.checkedout() == 0
                        and snap["worker_started_total"] == snap["worker_finished_total"]
                    ):
                        worker_finished.set()
                        break
                    await asyncio.sleep(0.005)

            self.assertTrue(worker_finished.is_set())
            self.assertEqual(engine.pool.checkedout(), 0)
            self.assertEqual(snap["callers_awaiting"], 0)
        finally:
            release.set()
            engine.dispose()


class TestSnapshotWireTermination(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
        self.ws = MagicMock()
        self.ws.send_json = AsyncMock()
        self.ws.close = AsyncMock()
        topic_dispatcher.registry.register(self.ws, ["fx:usd-krw", "usdt:krw"])

    async def asyncTearDown(self):
        topic_dispatcher.registry = self.original_registry

    async def test_topic_between_expiry_uses_same_deadline_and_closes_once(self):
        clock = _Clock()
        budget = _budget(clock)
        built = []

        def build(topic):
            built.append(topic)
            return {"type": "snapshot", "topic": topic, "data": {}}

        async def send_then_expire(payload):
            clock.value = 2.0

        self.ws.send_json.side_effect = send_then_expire
        with patch("app.topic_initial_snapshot._build_snapshot_sync", side_effect=build):
            with self.assertRaises(InitialSnapshotDeadlineExceeded):
                await send_initial_snapshots(
                    self.ws,
                    ["fx:usd-krw", "usdt:krw"],
                    channel="token_bearing",
                    budget=budget,
                )

        self.assertEqual(built, ["fx:usd-krw"], "두 번째 topic build가 시작됐다")
        self.ws.close.assert_awaited_once_with(code=1013)
        self.assertEqual(self.ws.send_json.await_count, 1, "terminal frame이 추가 전송됐다")
        self.assertEqual(topic_dispatcher.registry.get_subscriptions(self.ws), set())

    async def test_transient_build_failure_is_1013_without_terminal_frame(self):
        exc = OperationalError("SELECT 1", {}, Exception("connection lost"))
        with patch("app.topic_initial_snapshot._build_snapshot_sync", side_effect=exc):
            with self.assertRaises(InitialSnapshotTransientFailure):
                await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        self.ws.close.assert_awaited_once_with(code=1013)
        self.ws.send_json.assert_not_awaited()

    async def test_failure_cooldown_hit_is_1013_without_starting_a_build(self):
        cooldown = SnapshotFailureCooldownActive(
            "fx:usd-krw",
            failure_class="transient_db",
            remaining_seconds=0.75,
            suppressed_builds=4,
        )
        with patch(
            "app.topic_initial_snapshot.build_snapshot_observed",
            new=AsyncMock(side_effect=cooldown),
        ):
            with self.assertRaises(InitialSnapshotTransientFailure):
                await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        self.ws.close.assert_awaited_once_with(code=1013)
        self.ws.send_json.assert_not_awaited()

    async def test_fatal_build_failure_is_1011_without_terminal_frame(self):
        with patch(
            "app.topic_initial_snapshot._build_snapshot_sync",
            side_effect=ValueError("bad payload"),
        ):
            with self.assertRaises(InitialSnapshotFatalFailure):
                await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        self.ws.close.assert_awaited_once_with(code=1011)
        self.ws.send_json.assert_not_awaited()


class TestAckDeadlineClassification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
        self.ws = MagicMock()
        self.ws.send_json = AsyncMock()
        self.ws.close = AsyncMock()
        topic_dispatcher.registry.register(self.ws, ["fx:usd-krw"])

    async def asyncTearDown(self):
        topic_dispatcher.registry = self.original_registry

    async def test_expired_ack_budget_closes_1013_without_frame(self):
        from app.topic_dispatcher import _send_json_with_topic_deadline
        from app.topic_wire import TopicRequestDeadlineExceeded

        clock = _Clock(2.0)
        budget = _budget(clock)
        with self.assertRaises(TopicRequestDeadlineExceeded):
            await _send_json_with_topic_deadline(
                self.ws,
                {"type": "subscription_ack"},
                budget=budget,
                deadline_at_mono=budget.ack_deadline_at_mono,
            )
        self.ws.send_json.assert_not_awaited()
        self.ws.close.assert_awaited_once_with(code=1013)
        self.assertEqual(topic_dispatcher.registry.get_subscriptions(self.ws), set())

    async def test_send_internal_timeout_is_not_misclassified_as_deadline(self):
        from app.topic_dispatcher import _send_json_with_topic_deadline

        budget = _budget(_Clock())
        self.ws.send_json.side_effect = TimeoutError("serializer/socket internal")
        with self.assertRaisesRegex(TimeoutError, "internal"):
            await _send_json_with_topic_deadline(
                self.ws,
                {"type": "subscription_ack"},
                budget=budget,
                deadline_at_mono=budget.ack_deadline_at_mono,
            )
        self.ws.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
