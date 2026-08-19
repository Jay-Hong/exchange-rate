"""LOAD-S5 — topic snapshot single-flight/cache 행동 계약."""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app import config, subscribe_load_metrics as slm, topic_dispatcher
from app import topic_initial_snapshot as snapshot
from app.topic_initial_snapshot import (
    SnapshotDeadlineExceeded,
    SnapshotFailureCooldownActive,
    SnapshotRequestBudget,
    build_snapshot_observed,
)


def _budget(*, snapshot_seconds: float = 10.0) -> SnapshotRequestBudget:
    return SnapshotRequestBudget(
        ack_deadline_seconds=5.0,
        snapshot_budget_seconds=snapshot_seconds,
        request_deadline_seconds=20.0,
    )


async def _wait_until(predicate, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not become true")


class TestSnapshotSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        topic_dispatcher._topic_payload_generations.clear()
        with slm._lock:
            slm._metrics.clear()
            slm._metrics.update(slm._blank())

    async def asyncTearDown(self) -> None:
        topic_dispatcher._topic_payload_generations.clear()

    async def test_concurrent_waiters_build_once_and_receive_isolated_payloads(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def build(topic, *, budget):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {
                "type": "snapshot",
                "version": 1,
                "topic": topic,
                "data": {"rates": [{"rate": 1400.0}]},
            }

        with patch.object(config, "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS", 1), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=build,
        ):
            tasks = [
                asyncio.create_task(build_snapshot_observed("usdt:krw", budget=_budget()))
                for _ in range(20)
            ]
            await entered.wait()
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_singleflight"][
                    "requests_total"
                ] == 20
            )
            self.assertEqual(calls, 1)
            release.set()
            results = await asyncio.gather(*tasks)

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len({id(result) for result in results}), 20)
        self.assertEqual(len({id(result["data"]) for result in results}), 20)
        results[0]["data"]["rates"][0]["rate"] = -1
        self.assertEqual(results[1]["data"]["rates"][0]["rate"], 1400.0)

        metrics = slm.subscribe_load_metrics()["snapshot_singleflight"]
        self.assertEqual(metrics["leaders_total"], 1)
        self.assertEqual(metrics["joined_total"], 19)
        self.assertEqual(metrics["successes_cached_total"], 1)
        self.assertEqual(metrics["waiters_max"], 20)
        self.assertEqual(
            slm.subscribe_load_metrics()["snapshot_admission"]["queued_max"],
            0,
            "기존 flight join이 admission queue를 다시 탔다",
        )

        cached = await build_snapshot_observed("usdt:krw", budget=_budget())
        self.assertEqual(cached["data"]["rates"][0]["rate"], 1400.0)
        self.assertIsNot(cached, results[1])
        self.assertEqual(
            slm.subscribe_load_metrics()["snapshot_singleflight"]["cache_hits_total"],
            1,
        )

    async def test_leader_cancellation_does_not_cancel_shared_build_or_follower(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        shared_budgets = []

        async def build(topic, *, budget):
            shared_budgets.append(budget)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {"type": "snapshot", "topic": topic, "data": {}}

        leader_budget = _budget()
        follower_budget = _budget()
        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=build,
        ):
            leader = asyncio.create_task(
                build_snapshot_observed("usdt:krw", budget=leader_budget)
            )
            await entered.wait()
            follower = asyncio.create_task(
                build_snapshot_observed("usdt:krw", budget=follower_budget)
            )
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_singleflight"][
                    "joined_total"
                ] == 1
            )
            leader.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await leader
            self.assertEqual(leader_budget.stop_reason, "caller_cancelled")
            self.assertFalse(cancelled.is_set(), "leader 취소가 shared task까지 전파됐다")
            self.assertEqual(
                slm.subscribe_load_metrics()["snapshot_admission"]["in_flight"],
                1,
                "leader caller 취소가 shared task의 permit을 조기 반환했다",
            )
            self.assertEqual(
                slm.subscribe_load_metrics()["snapshot_singleflight"]["waiters_now"], 1
            )
            self.assertIsNot(shared_budgets[0], leader_budget)
            self.assertIsNot(shared_budgets[0], follower_budget)

            release.set()
            self.assertEqual(
                await follower,
                {"type": "snapshot", "topic": "usdt:krw", "data": {}},
            )
            self.assertIsNone(shared_budgets[0].stop_reason)
            self.assertEqual(
                slm.subscribe_load_metrics()["snapshot_admission"]["in_flight"], 0
            )

    async def test_failure_and_last_waiter_cancellation_remove_flight_for_retry(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fail_once(topic, *, budget):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            if calls == 1:
                raise RuntimeError("boom")
            return {"type": "snapshot", "topic": topic, "data": {"try": calls}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=fail_once,
        ):
            first = asyncio.create_task(build_snapshot_observed("usdt:krw", budget=_budget()))
            second = asyncio.create_task(build_snapshot_observed("usdt:krw", budget=_budget()))
            await entered.wait()
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_singleflight"][
                    "joined_total"
                ] == 1
            )
            release.set()
            failures = await asyncio.gather(first, second, return_exceptions=True)
            self.assertTrue(all(isinstance(exc, RuntimeError) for exc in failures))
            self.assertEqual(calls, 1)
            self.assertEqual(snapshot._snapshot_state().flights, {})

            retried = await build_snapshot_observed("usdt:krw", budget=_budget())
            self.assertEqual(retried["data"]["try"], 2)

        cancel_entered = asyncio.Event()
        cancel_seen = asyncio.Event()

        async def cancellable(topic, *, budget):
            cancel_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_seen.set()
                raise

        # generation을 바꿔 위 성공 cache와 다른 key를 만든다.
        topic_dispatcher._advance_topic_payload_generation("usdt:krw")
        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=cancellable,
        ):
            only = asyncio.create_task(build_snapshot_observed("usdt:krw", budget=_budget()))
            await cancel_entered.wait()
            only.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await only
            await _wait_until(cancel_seen.is_set)
            await _wait_until(lambda: not snapshot._snapshot_state().flights)

        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            return_value={"type": "snapshot", "topic": "usdt:krw", "data": {}},
        ) as retry:
            await build_snapshot_observed("usdt:krw", budget=_budget())
        retry.assert_awaited_once()

    async def test_transient_failure_cooldown_suppresses_rebuilds_then_recovers(self):
        calls = 0
        transient = OperationalError("SELECT 1", {}, Exception("connection lost"))

        async def fail_then_succeed(topic, *, budget):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise transient
            return {"type": "snapshot", "topic": topic, "data": {"call": calls}}

        with patch.object(
            config, "WS_TOPIC_SNAPSHOT_FAILURE_COOLDOWN_SECONDS", 0.04
        ), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=fail_then_succeed,
        ):
            with self.assertRaises(OperationalError):
                await build_snapshot_observed("usdt:krw", budget=_budget())

            retries = await asyncio.gather(*[
                build_snapshot_observed("usdt:krw", budget=_budget())
                for _ in range(20)
            ], return_exceptions=True)
            self.assertTrue(
                all(isinstance(exc, SnapshotFailureCooldownActive) for exc in retries)
            )
            self.assertEqual(calls, 1, "cooldown 중 새 builder가 시작됐다")

            metric = slm.subscribe_load_metrics()["snapshot_failure_cooldown"]
            self.assertEqual(metric["armed_total"], 1)
            self.assertEqual(metric["suppressed_total"], 20)
            self.assertEqual(metric["by_failure_class"]["transient_db"], 1)
            self.assertEqual(metric["last_suppression"]["topic"], "usdt:krw")
            self.assertEqual(metric["last_suppression"]["suppressed_builds"], 20)
            self.assertGreater(metric["last_suppression"]["cooldown_remaining_ms"], 0)
            self.assertNotIn("sql", metric["last_suppression"])
            self.assertNotIn("token", metric["last_suppression"])

            await asyncio.sleep(0.05)
            recovered = await build_snapshot_observed("usdt:krw", budget=_budget())

        self.assertEqual(recovered["data"]["call"], 2)
        self.assertEqual(calls, 2)
        self.assertEqual(snapshot._snapshot_state().failure_cooldowns, {})
        self.assertEqual(
            slm.subscribe_load_metrics()["snapshot_failure_cooldown"]["expired_total"],
            1,
        )

    async def test_deadline_and_redis_failures_are_cooldown_eligible(self):
        from redis.exceptions import TimeoutError as RedisTimeoutError

        for label, failure, expected_class in (
            ("deadline", SnapshotDeadlineExceeded("usdt:krw"), "deadline"),
            ("redis", RedisTimeoutError("stalled"), "redis"),
        ):
            with self.subTest(failure=label), patch(
                "app.topic_initial_snapshot._build_snapshot_once_observed",
                side_effect=failure,
            ):
                topic_dispatcher._advance_topic_payload_generation("usdt:krw")
                with self.assertRaises(type(failure)):
                    await build_snapshot_observed("usdt:krw", budget=_budget())
                with self.assertRaises(SnapshotFailureCooldownActive) as caught:
                    await build_snapshot_observed("usdt:krw", budget=_budget())
                self.assertEqual(caught.exception.failure_class, expected_class)

    async def test_new_generation_does_not_reuse_previous_failure_cooldown(self):
        calls = 0
        transient = OperationalError("SELECT 1", {}, Exception("connection lost"))

        async def fail_then_succeed(topic, *, budget):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise transient
            return {"type": "snapshot", "topic": topic, "data": {"call": calls}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=fail_then_succeed,
        ):
            with self.assertRaises(OperationalError):
                await build_snapshot_observed("usdt:krw", budget=_budget())
            topic_dispatcher._advance_topic_payload_generation("usdt:krw")
            result = await build_snapshot_observed("usdt:krw", budget=_budget())

        self.assertEqual(result["data"]["call"], 2)
        self.assertEqual(calls, 2)

    async def test_fatal_and_permanent_db_failures_never_enter_cooldown(self):
        class _Orig:
            sqlstate = "28P01"

        failures = (
            RuntimeError("programming bug"),
            OperationalError("connect", {}, _Orig()),
        )
        for failure in failures:
            calls = 0

            async def fail_then_succeed(topic, *, budget):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise failure
                return {"type": "snapshot", "topic": topic, "data": {}}

            with self.subTest(failure=type(failure).__name__), patch(
                "app.topic_initial_snapshot._build_snapshot_once_observed",
                side_effect=fail_then_succeed,
            ):
                topic_dispatcher._advance_topic_payload_generation("usdt:krw")
                with self.assertRaises(type(failure)):
                    await build_snapshot_observed("usdt:krw", budget=_budget())
                await build_snapshot_observed("usdt:krw", budget=_budget())
                self.assertEqual(calls, 2)
                self.assertEqual(snapshot._snapshot_state().failure_cooldowns, {})

    async def test_topic_generation_and_gate_are_distinct_keys(self):
        calls = []

        async def build(topic, *, budget):
            calls.append((topic, config.FX_TOPIC_ENABLED))
            return {"type": "snapshot", "topic": topic, "data": {"call": len(calls)}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=build,
        ), patch.object(config, "FX_TOPIC_ENABLED", True):
            first = await build_snapshot_observed("fx:usd-krw", budget=_budget())
            await build_snapshot_observed("usdt:krw", budget=_budget())
            self.assertEqual(len(calls), 2, "서로 다른 topic이 합쳐졌다")

            with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
                before = topic_dispatcher.topic_payload_generation("fx:usd-krw")
                self.assertEqual(
                    await topic_dispatcher.publish_topic("fx:usd-krw", first), 0
                )
                self.assertEqual(
                    topic_dispatcher.topic_payload_generation("fx:usd-krw"),
                    before + 1,
                )
            await build_snapshot_observed("fx:usd-krw", budget=_budget())
            self.assertEqual(len(calls), 3, "새 generation이 이전 cache를 재사용했다")

            with patch.object(config, "FX_TOPIC_ENABLED", False):
                await build_snapshot_observed("fx:usd-krw", budget=_budget())
            self.assertEqual(len(calls), 4, "availability gate 변화가 같은 key로 합쳐졌다")

    async def test_none_is_not_success_cached(self):
        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            return_value=None,
        ) as build:
            self.assertIsNone(await build_snapshot_observed("usdt:krw", budget=_budget()))
            self.assertIsNone(await build_snapshot_observed("usdt:krw", budget=_budget()))
        self.assertEqual(build.await_count, 2)
        self.assertEqual(
            slm.subscribe_load_metrics()["snapshot_singleflight"][
                "successes_cached_total"
            ],
            0,
        )

    async def test_cancelling_flight_is_not_reused_by_immediate_retry(self):
        entered = asyncio.Event()
        first_cancelled = asyncio.Event()
        calls = 0

        async def build(topic, *, budget):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise
            return {"type": "snapshot", "topic": topic, "data": {"call": calls}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=build,
        ):
            first = asyncio.create_task(build_snapshot_observed("usdt:krw", budget=_budget()))
            await entered.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            # shared task cleanup 완료를 기다리지 않고 즉시 재진입한다.
            retried = await build_snapshot_observed("usdt:krw", budget=_budget())

        self.assertEqual(retried["data"]["call"], 2)
        self.assertTrue(first_cancelled.is_set())

    async def test_success_cache_expires_at_the_configured_ceiling(self):
        calls = 0

        async def build(topic, *, budget):
            nonlocal calls
            calls += 1
            return {"type": "snapshot", "topic": topic, "data": {"call": calls}}

        with patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=build,
        ), patch.object(config, "WS_TOPIC_SNAPSHOT_CACHE_TTL_SECONDS", 0.01):
            first = await build_snapshot_observed("usdt:krw", budget=_budget())
            await asyncio.sleep(0.02)
            second = await build_snapshot_observed("usdt:krw", budget=_budget())
        self.assertEqual((first["data"]["call"], second["data"]["call"]), (1, 2))

    async def test_unknown_metric_disposition_cannot_create_dynamic_keys(self):
        before = slm.subscribe_load_metrics()["snapshot_singleflight"]
        errors_before = slm.subscribe_load_metrics()["metrics_internal_errors_total"]
        slm.record_snapshot_singleflight("typo")
        after = slm.subscribe_load_metrics()["snapshot_singleflight"]
        self.assertEqual(after, before)
        self.assertEqual(
            slm.subscribe_load_metrics()["metrics_internal_errors_total"],
            errors_before + 1,
        )


class TestSnapshotCacheConfig(unittest.TestCase):
    def test_failure_class_copies_do_not_drift(self):
        self.assertEqual(
            tuple(snapshot.SNAPSHOT_FAILURE_CLASSES),
            tuple(slm.SNAPSHOT_FAILURE_CLASSES),
        )

    def test_admission_capacity_has_a_finite_dormant_default(self):
        self.assertEqual(config.WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS, 4)

    def test_failure_cooldown_has_a_finite_dormant_default(self):
        self.assertEqual(config.WS_TOPIC_SNAPSHOT_FAILURE_COOLDOWN_SECONDS, 1.0)
        self.assertLessEqual(
            config.WS_TOPIC_SNAPSHOT_FAILURE_COOLDOWN_SECONDS,
            config.WS_TOPIC_REQUEST_DEADLINE_SECONDS,
        )

    def test_cache_ttl_is_positive_and_no_more_than_live_freshness_contract(self):
        self.assertGreater(config.WS_TOPIC_SNAPSHOT_CACHE_TTL_SECONDS, 0)
        self.assertLessEqual(config.WS_TOPIC_SNAPSHOT_CACHE_TTL_SECONDS, 1.0)

    def test_invalid_cache_ttl_fails_at_config_import(self):
        repo = pathlib.Path(__file__).resolve().parent.parent
        for value in ("0", "1.0001", "nan", "inf"):
            with self.subTest(value=value):
                env = os.environ.copy()
                env["WS_TOPIC_SNAPSHOT_CACHE_TTL_SECONDS"] = value
                proc = subprocess.run(
                    [sys.executable, "-c", "import app.config"],
                    cwd=repo,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("TTL", proc.stderr)

    def test_invalid_admission_capacity_fails_at_config_import(self):
        repo = pathlib.Path(__file__).resolve().parent.parent
        for value in ("0", "33", "not-an-int"):
            with self.subTest(value=value):
                env = os.environ.copy()
                env["WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS"] = value
                proc = subprocess.run(
                    [sys.executable, "-c", "import app.config"],
                    cwd=repo,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(proc.returncode, 0)

    def test_invalid_failure_cooldown_fails_at_config_import(self):
        repo = pathlib.Path(__file__).resolve().parent.parent
        for value in ("0", "nan", "inf", "26"):
            with self.subTest(value=value):
                env = os.environ.copy()
                env["WS_TOPIC_SNAPSHOT_FAILURE_COOLDOWN_SECONDS"] = value
                proc = subprocess.run(
                    [sys.executable, "-c", "import app.config"],
                    cwd=repo,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("cooldown", proc.stderr.lower())


class TestSnapshotAdmission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        topic_dispatcher._topic_payload_generations.clear()
        with slm._lock:
            slm._metrics.clear()
            slm._metrics.update(slm._blank())

    async def test_fifo_queue_absorbs_short_predecessors_without_rejection(self):
        entered = {topic: asyncio.Event() for topic in ("test:a", "test:b", "test:c")}
        release = {topic: asyncio.Event() for topic in entered}
        order = []

        async def build(topic, *, budget):
            order.append(topic)
            entered[topic].set()
            await release[topic].wait()
            return {"type": "snapshot", "topic": topic, "data": {}}

        with patch.object(config, "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS", 1), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed", side_effect=build
        ):
            first = asyncio.create_task(build_snapshot_observed("test:a", budget=_budget()))
            await entered["test:a"].wait()
            second = asyncio.create_task(build_snapshot_observed("test:b", budget=_budget()))
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_admission"]["queued_now"] == 1
            )
            third = asyncio.create_task(build_snapshot_observed("test:c", budget=_budget()))
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_admission"]["queued_now"] == 2
            )

            release["test:a"].set()
            await entered["test:b"].wait()
            self.assertFalse(entered["test:c"].is_set(), "FIFO 앞의 request를 건너뛰었다")
            release["test:b"].set()
            await entered["test:c"].wait()
            release["test:c"].set()
            await asyncio.gather(first, second, third)

        self.assertEqual(order, ["test:a", "test:b", "test:c"])
        admission = slm.subscribe_load_metrics()["snapshot_admission"]
        self.assertEqual(admission["queued_now"], 0)
        self.assertEqual(admission["in_flight"], 0)
        self.assertEqual(admission["queued_max"], 2)
        self.assertEqual(admission["wait_observed_total"], 2)
        self.assertEqual(
            slm.subscribe_load_metrics()["snapshot_singleflight"]["waiters_now"], 0
        )

    async def test_queued_deadline_never_starts_builder_and_releases_gauges(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def build(topic, *, budget):
            calls.append(topic)
            entered.set()
            await release.wait()
            return {"type": "snapshot", "topic": topic, "data": {}}

        waiting_budget = _budget(snapshot_seconds=0.05)
        with patch.object(config, "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS", 1), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed", side_effect=build
        ):
            holder = asyncio.create_task(build_snapshot_observed("test:holder", budget=_budget()))
            await entered.wait()
            with self.assertRaises(SnapshotDeadlineExceeded):
                await build_snapshot_observed("test:expired", budget=waiting_budget)
            self.assertEqual(calls, ["test:holder"])
            self.assertEqual(waiting_budget.stop_reason, "deadline")
            self.assertEqual(
                snapshot._snapshot_state().failure_cooldowns, {},
                "admission queue deadline이 build failure cooldown으로 오분류됐다",
            )
            release.set()
            await holder
            await build_snapshot_observed("test:expired", budget=_budget())

        metrics = slm.subscribe_load_metrics()
        self.assertEqual(metrics["snapshot_admission"]["queued_now"], 0)
        self.assertEqual(metrics["snapshot_admission"]["in_flight"], 0)
        self.assertEqual(metrics["snapshot_singleflight"]["waiters_now"], 0)

    async def test_same_key_joins_a_flight_that_is_still_waiting_for_admission(self):
        holder_entered = asyncio.Event()
        holder_release = asyncio.Event()
        same_entered = asyncio.Event()
        calls = []

        async def build(topic, *, budget):
            calls.append(topic)
            if topic == "test:holder":
                holder_entered.set()
                await holder_release.wait()
            else:
                same_entered.set()
            return {"type": "snapshot", "topic": topic, "data": {}}

        with patch.object(config, "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS", 1), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed", side_effect=build
        ):
            holder = asyncio.create_task(build_snapshot_observed("test:holder", budget=_budget()))
            await holder_entered.wait()
            leader = asyncio.create_task(build_snapshot_observed("test:same", budget=_budget()))
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_admission"]["queued_now"] == 1
            )
            follower = asyncio.create_task(build_snapshot_observed("test:same", budget=_budget()))
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_singleflight"]["joined_total"] == 1
            )
            self.assertEqual(
                slm.subscribe_load_metrics()["snapshot_admission"]["queued_now"], 1,
                "같은 key follower가 기존 queued flight를 join하지 않고 admission에 중복 진입했다",
            )
            holder_release.set()
            await same_entered.wait()
            await asyncio.gather(holder, leader, follower)

        self.assertEqual(calls.count("test:same"), 1)
        admission = slm.subscribe_load_metrics()["snapshot_admission"]
        self.assertEqual((admission["queued_now"], admission["in_flight"]), (0, 0))
        self.assertEqual(
            slm.subscribe_load_metrics()["snapshot_singleflight"]["waiters_now"], 0
        )

    async def test_queued_cancel_and_builder_error_both_release_admission_state(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold(topic, *, budget):
            entered.set()
            await release.wait()
            return {"type": "snapshot", "topic": topic, "data": {}}

        with patch.object(config, "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS", 1), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed", side_effect=hold
        ):
            holder = asyncio.create_task(build_snapshot_observed("test:holder", budget=_budget()))
            await entered.wait()
            queued = asyncio.create_task(build_snapshot_observed("test:cancel", budget=_budget()))
            await _wait_until(
                lambda: slm.subscribe_load_metrics()["snapshot_admission"]["queued_now"] == 1
            )
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            release.set()
            await holder

        with patch.object(config, "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS", 1), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await build_snapshot_observed("test:error", budget=_budget())

        metrics = slm.subscribe_load_metrics()
        self.assertEqual(metrics["snapshot_admission"]["queued_now"], 0)
        self.assertEqual(metrics["snapshot_admission"]["in_flight"], 0)
        self.assertEqual(metrics["snapshot_singleflight"]["waiters_now"], 0)

    async def test_different_topics_are_not_serialized_by_registry_lock(self):
        entered = {"test:slow": asyncio.Event(), "test:fast": asyncio.Event()}
        release = asyncio.Event()

        async def build(topic, *, budget):
            entered[topic].set()
            if topic == "test:slow":
                await release.wait()
            return {"type": "snapshot", "topic": topic, "data": {}}

        with patch.object(config, "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS", 2), patch(
            "app.topic_initial_snapshot._build_snapshot_once_observed", side_effect=build
        ):
            slow = asyncio.create_task(build_snapshot_observed("test:slow", budget=_budget()))
            await entered["test:slow"].wait()
            fast = asyncio.create_task(build_snapshot_observed("test:fast", budget=_budget()))
            await asyncio.wait_for(entered["test:fast"].wait(), timeout=0.2)
            self.assertTrue(slow.done() is False)
            self.assertEqual((await fast)["topic"], "test:fast")
            release.set()
            await slow


if __name__ == "__main__":
    unittest.main()
