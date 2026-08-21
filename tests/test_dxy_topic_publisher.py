"""DXY ``dxy:spot`` publisher 수직 경로 회귀 테스트."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, dxy_topic_publisher, topic_dispatcher
from app.dxy_topic_publisher import (
    DXY_TOPIC,
    build_dxy_topic_payload,
    load_dxy_topic_entry,
    normalize_dxy_topic_entry,
    publish_dxy_topic_snapshot,
    request_dxy_topic_publish,
)

ENTRY = {
    "instrument": "dxy",
    "rate": 104.52,
    "timestamp": "2026-08-21T14:30:00+09:00",
    "source": "investing",
}
WIRE_ENTRY = {key: ENTRY[key] for key in ("rate", "timestamp", "source")}


class TestDxyPayload(unittest.TestCase):
    def test_topic_and_schema(self):
        self.assertEqual(DXY_TOPIC, "dxy:spot")
        self.assertEqual(
            build_dxy_topic_payload(ENTRY),
            {
                "type": "snapshot",
                "topic": "dxy:spot",
                "version": 1,
                "data": {"dxy": WIRE_ENTRY},
            },
        )

    def test_timezone_aware_datetime_is_normalized(self):
        raw = dict(ENTRY, timestamp=datetime(2026, 8, 21, tzinfo=timezone.utc))
        self.assertEqual(
            normalize_dxy_topic_entry(raw)["timestamp"],
            "2026-08-21T00:00:00+00:00",
        )

    def test_malformed_entries_are_rejected(self):
        cases = (
            dict(ENTRY, instrument="dxy_futures"),
            dict(ENTRY, source="unknown"),
            dict(ENTRY, rate=True),
            dict(ENTRY, rate=float("inf")),
            dict(ENTRY, rate=0),
            dict(ENTRY, timestamp="2026-08-21T14:30:00"),
            dict(ENTRY, timestamp="not-a-date"),
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_dxy_topic_entry(raw))
                with self.assertRaises(ValueError):
                    build_dxy_topic_payload(raw)


class TestLoadDxyTopicEntry(unittest.TestCase):
    def test_redis_hit_skips_db(self):
        db = MagicMock()
        with patch(
            "app.latest_rates_cache.get_latest_dxy_rate_from_sync_job",
            return_value=dict(ENTRY),
        ), patch("app.crud.get_latest_dxy_rate") as db_load:
            got = load_dxy_topic_entry(db)
        self.assertEqual(got, WIRE_ENTRY)
        db_load.assert_not_called()

    def test_redis_miss_falls_back_to_db(self):
        db = MagicMock()
        with patch(
            "app.latest_rates_cache.get_latest_dxy_rate_from_sync_job",
            return_value=None,
        ), patch("app.crud.get_latest_dxy_rate", return_value=dict(ENTRY)) as db_load:
            got = load_dxy_topic_entry(db)
        self.assertEqual(got, WIRE_ENTRY)
        db_load.assert_called_once_with(db)

    def test_live_trigger_prefers_just_committed_db_value(self):
        db = MagicMock()
        with patch(
            "app.latest_rates_cache.get_latest_dxy_rate_from_sync_job"
        ) as redis_load, patch(
            "app.crud.get_latest_dxy_rate", return_value=dict(ENTRY)
        ) as db_load:
            got = load_dxy_topic_entry(db, prefer_db=True)
        self.assertEqual(got, WIRE_ENTRY)
        redis_load.assert_not_called()
        db_load.assert_called_once_with(db)


class TestPublishDxyTopicSnapshot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self.original_registry

    def register(self):
        websocket = MagicMock()
        topic_dispatcher.registry.register(websocket, [DXY_TOPIC])
        return websocket

    async def test_dispatcher_off_skips_load(self):
        self.register()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), patch.object(
            dxy_topic_publisher, "load_dxy_topic_entry"
        ) as load, patch.object(
            topic_dispatcher, "publish_topic", new=AsyncMock()
        ) as publish:
            result = await publish_dxy_topic_snapshot()
        self.assertFalse(result)
        load.assert_not_called()
        publish.assert_not_called()

    async def test_no_subscriber_skips_load(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch.object(
            dxy_topic_publisher, "load_dxy_topic_entry"
        ) as load:
            result = await publish_dxy_topic_snapshot()
        self.assertFalse(result)
        load.assert_not_called()

    async def test_valid_entry_is_published(self):
        self.register()
        publish = AsyncMock(return_value=1)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch.object(
            topic_dispatcher, "publish_topic", new=publish
        ):
            result = await publish_dxy_topic_snapshot(dict(ENTRY))
        self.assertTrue(result)
        publish.assert_awaited_once_with(DXY_TOPIC, build_dxy_topic_payload(ENTRY))

    async def test_invalid_entry_never_publishes(self):
        self.register()
        publish = AsyncMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch.object(
            topic_dispatcher, "publish_topic", new=publish
        ):
            result = await publish_dxy_topic_snapshot(dict(ENTRY, source="bad"))
        self.assertFalse(result)
        publish.assert_not_called()


class TestRequestDxyTopicPublish(unittest.TestCase):
    def setUp(self):
        self.original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    def tearDown(self):
        topic_dispatcher.registry = self.original_registry

    def register(self):
        websocket = MagicMock()
        topic_dispatcher.registry.register(websocket, [DXY_TOPIC])
        return websocket

    def test_dispatcher_off_skips_db_and_marshal(self):
        self.register()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False), patch.object(
            dxy_topic_publisher, "load_dxy_topic_entry"
        ) as load, patch.object(
            dxy_topic_publisher.topic_trigger_bridge, "schedule_on_loop"
        ) as schedule:
            request_dxy_topic_publish(MagicMock(), reason="changed")
        load.assert_not_called()
        schedule.assert_not_called()

    def test_worker_thread_never_reads_event_loop_registry(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch.object(
            topic_dispatcher.registry,
            "subscriber_count",
            side_effect=AssertionError("worker thread registry read"),
        ), patch.object(
            dxy_topic_publisher, "load_dxy_topic_entry", return_value=dict(WIRE_ENTRY)
        ) as load, patch.object(
            dxy_topic_publisher.topic_trigger_bridge, "schedule_on_loop"
        ) as schedule:
            db = MagicMock()
            request_dxy_topic_publish(db, reason="changed")
        load.assert_called_once_with(db, prefer_db=True)
        schedule.assert_called_once()

    def test_changed_value_is_frozen_before_loop_marshal(self):
        self.register()
        db = MagicMock()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch.object(
            dxy_topic_publisher, "load_dxy_topic_entry", return_value=dict(WIRE_ENTRY)
        ) as load, patch.object(
            dxy_topic_publisher.topic_trigger_bridge, "schedule_on_loop"
        ) as schedule:
            request_dxy_topic_publish(db, reason="changed")
        load.assert_called_once_with(db, prefer_db=True)
        callback = schedule.call_args.args[0]
        self.assertIsInstance(callback, partial)
        self.assertIs(callback.func, dxy_topic_publisher._schedule_publish)
        self.assertEqual(callback.args, (WIRE_ENTRY,))

    def test_marshal_exception_isolated(self):
        self.register()
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), patch.object(
            dxy_topic_publisher, "load_dxy_topic_entry", return_value=dict(WIRE_ENTRY)
        ), patch.object(
            dxy_topic_publisher.topic_trigger_bridge,
            "schedule_on_loop",
            side_effect=RuntimeError("boom"),
        ):
            request_dxy_topic_publish(MagicMock(), reason="changed")


class TestScheduleDxyTopicPublish(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        tasks = list(dxy_topic_publisher._publish_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        dxy_topic_publisher._publish_tasks.clear()

    async def test_task_is_retained_until_publish_finishes(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_publish(_entry):
            started.set()
            await release.wait()

        with patch.object(dxy_topic_publisher, "_run_publish", new=blocked_publish):
            dxy_topic_publisher._schedule_publish(dict(WIRE_ENTRY))
            await started.wait()
            self.assertEqual(len(dxy_topic_publisher._publish_tasks), 1)
            task = next(iter(dxy_topic_publisher._publish_tasks))
            release.set()
            await task
            await asyncio.sleep(0)

        self.assertFalse(dxy_topic_publisher._publish_tasks)


if __name__ == "__main__":
    unittest.main()
