"""KRX Redis 통합 단위 테스트 (PR ADR-031).

ADR-031 핵심 계약:
1. KrxDbWriter DB insert 성공 시 set_latest_krx_rate_from_sync_job 호출
2. insert_source_rate_if_changed False면 Redis write 미호출
3. Redis write 예외가 DB writer loop에 전파되지 않음 (best-effort 격리)
4. builder Redis-first read: hit이면 DB query 미호출
5. builder Redis miss: DB fallback 호출
6. KRX helper는 usdt_redis_stats 미호출 (telemetry 분리 ★)
7. 오래된 timestamp Redis hit 그대로 채택 (stale guard 없음 ★)
8. Redis hit 값 topic-native shape 정상 normalize

helper layer 단위 (4):
- writer key/value 정확
- writer init fail → False
- writer exception → False + async circuit 미터치 + usdt_redis_stats 미호출
- reader hit/miss/parse fail
"""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import latest_rates_cache
from app.latest_rates_cache import (
    _KST,
    get_latest_krx_rate_from_sync_job,
    serialize_value,
    set_latest_krx_rate_from_sync_job,
)


# ---------------------------------------------------------------------------
# Group 1 — set_latest_krx_rate_from_sync_job (writer) 단위
# ---------------------------------------------------------------------------

class TestSetLatestKrxRateFromSyncJob(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def test_calls_set_with_correct_key_and_value(self):
        """latest_key_source("krx", asset) + serialize_value 정확 사용."""
        fake_client = MagicMock()
        latest_rates_cache._sync_client = fake_client
        result = set_latest_krx_rate_from_sync_job(
            asset="usd-krw-futures",
            rate=1485.0,
            timestamp="2026-05-13T15:00:00+09:00",
        )
        self.assertTrue(result)
        fake_client.set.assert_called_once()
        called_args = fake_client.set.call_args.args
        self.assertEqual(called_args[0], "latest:source:krx:usd-krw-futures")
        parsed = json.loads(called_args[1])
        self.assertEqual(parsed["rate"], 1485.0)
        self.assertEqual(parsed["timestamp"], "2026-05-13T15:00:00+09:00")
        self.assertIn("mirrored_at", parsed)

    def test_returns_false_when_client_init_fails(self):
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning"):
            result = set_latest_krx_rate_from_sync_job(
                "usd-krw-futures", 1485.0, "2026-05-13T15:00:00+09:00"
            )
        self.assertFalse(result)

    def test_exception_skips_async_circuit_and_usdt_stats(self):
        """★ Redis 예외 시 async circuit 미터치 + usdt_redis_stats 미호출.

        KRX는 USDT telemetry layer 분리 — 운영자 시맨틱 보호.
        """
        fake_client = MagicMock()
        fake_client.set.side_effect = ConnectionError("redis fault")
        latest_rates_cache._sync_client = fake_client

        with patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_success") as mock_circuit_success, \
             patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_failure") as mock_circuit_failure, \
             patch("app.usdt_redis_stats.record_direct_write_success") as mock_usdt_succ, \
             patch("app.usdt_redis_stats.record_direct_write_failure") as mock_usdt_fail, \
             patch.object(latest_rates_cache.logger, "warning"):
            result = set_latest_krx_rate_from_sync_job(
                "usd-krw-futures", 1485.0, "2026-05-13T15:00:00+09:00"
            )

        self.assertFalse(result)
        # ★ 핵심 — async circuit 미터치 (broadcast/mirror path 보호)
        mock_circuit_success.assert_not_called()
        mock_circuit_failure.assert_not_called()
        # ★ 핵심 — usdt_redis_stats 미호출 (telemetry 시맨틱 보호)
        mock_usdt_succ.assert_not_called()
        mock_usdt_fail.assert_not_called()


# ---------------------------------------------------------------------------
# Group 2 — get_latest_krx_rate_from_sync_job (reader) 단위
# ---------------------------------------------------------------------------

class TestGetLatestKrxRateFromSyncJob(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def _make_value(self, rate: float, ts: str, mirrored_at: datetime) -> bytes:
        return serialize_value(rate, ts, mirrored_at).encode("utf-8")

    def test_hit_returns_topic_native_shape(self):
        """Redis hit → {source="krx", asset, rate, timestamp} shape."""
        fake_client = MagicMock()
        fake_client.get.return_value = self._make_value(
            1485.0,
            "2026-05-13T15:00:00+09:00",
            datetime.now(_KST),
        )
        latest_rates_cache._sync_client = fake_client

        result = get_latest_krx_rate_from_sync_job("usd-krw-futures")

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "krx")
        self.assertEqual(result["asset"], "usd-krw-futures")
        self.assertEqual(result["rate"], 1485.0)
        self.assertEqual(result["timestamp"], "2026-05-13T15:00:00+09:00")
        fake_client.get.assert_called_once_with("latest:source:krx:usd-krw-futures")

    def test_miss_returns_none(self):
        fake_client = MagicMock()
        fake_client.get.return_value = None
        latest_rates_cache._sync_client = fake_client
        self.assertIsNone(get_latest_krx_rate_from_sync_job("usd-krw-futures"))

    def test_old_timestamp_redis_hit_returned_as_is(self):
        """★ stale guard 없음 회귀 가드: 오래된 timestamp Redis hit 그대로 채택.

        ADR-031 1차: insert_source_rate_if_changed라 가격 stagnant 시 timestamp가
        오래된 채로 stuck. DB row gap ≠ raw frame gap 원칙에 따라 timestamp age로
        stale 판단 X. 본 테스트는 reader가 oldness와 무관하게 값을 반환함을 잠금.
        """
        fake_client = MagicMock()
        # 1시간 + 1초 전 — 일반적인 stale threshold(6s) 한참 초과
        old_mirrored_at = datetime.now(_KST) - timedelta(seconds=3601)
        old_ts = "2026-05-13T13:00:00+09:00"  # 가짜 오래된 timestamp
        fake_client.get.return_value = self._make_value(
            1485.0, old_ts, old_mirrored_at,
        )
        latest_rates_cache._sync_client = fake_client

        result = get_latest_krx_rate_from_sync_job("usd-krw-futures")

        # 오래된 값이라도 reader는 그대로 반환 — caller 정책 결정 영역
        self.assertIsNotNone(result)
        self.assertEqual(result["rate"], 1485.0)
        self.assertEqual(result["timestamp"], old_ts)

    def test_reader_does_not_touch_usdt_stats(self):
        """★ reader가 usdt_redis_stats 미호출 (telemetry 시맨틱 분리)."""
        fake_client = MagicMock()
        fake_client.get.return_value = self._make_value(
            1485.0, "2026-05-13T15:00:00+09:00", datetime.now(_KST),
        )
        latest_rates_cache._sync_client = fake_client

        with patch("app.usdt_redis_stats.record_redis_read_hit") as mock_hit, \
             patch("app.usdt_redis_stats.record_redis_read_miss") as mock_miss, \
             patch("app.usdt_redis_stats.record_redis_read_parse_fail") as mock_parse, \
             patch("app.usdt_redis_stats.record_redis_read_error") as mock_err:
            get_latest_krx_rate_from_sync_job("usd-krw-futures")

        mock_hit.assert_not_called()
        mock_miss.assert_not_called()
        mock_parse.assert_not_called()
        mock_err.assert_not_called()


# ---------------------------------------------------------------------------
# Group 3 — KrxDbWriter integration (insert + Redis write)
# ---------------------------------------------------------------------------

class TestKrxDbWriterRedisIntegration(unittest.TestCase):
    """KrxDbWriter._sync_db_write가 ADR-031 spec대로 동작."""

    SAMPLE_TICK = {
        "source": "krx",
        "asset": "usd-krw-futures",
        "price": "1485.10",
    }

    def test_calls_redis_writer_when_inserted(self):
        """insert_source_rate_if_changed True → set_latest_krx_rate_from_sync_job 호출."""
        from app.crawlers.krx_kis import KrxDbWriter

        fake_latest = {
            "rate": 1485.1,
            "timestamp": "2026-05-13T15:00:00+09:00",
        }
        # get_db_context는 context manager mock 필요
        fake_db = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.__enter__ = MagicMock(return_value=fake_db)
        fake_ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.database.get_db_context", return_value=fake_ctx), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch("app.crud.get_latest_source_rate", return_value=fake_latest), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
                   return_value=True) as mock_redis:
            KrxDbWriter._sync_db_write(self.SAMPLE_TICK)

        mock_redis.assert_called_once()
        call_kwargs = mock_redis.call_args.kwargs
        self.assertEqual(call_kwargs["asset"], "usd-krw-futures")
        self.assertEqual(call_kwargs["rate"], 1485.1)
        self.assertEqual(call_kwargs["timestamp"], "2026-05-13T15:00:00+09:00")

    def test_skips_redis_when_not_inserted(self):
        """insert_source_rate_if_changed False (unchanged) → Redis write 미호출."""
        from app.crawlers.krx_kis import KrxDbWriter

        fake_db = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.__enter__ = MagicMock(return_value=fake_db)
        fake_ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.database.get_db_context", return_value=fake_ctx), \
             patch("app.crud.insert_source_rate_if_changed", return_value=False), \
             patch("app.crud.get_latest_source_rate") as mock_get_latest, \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job") as mock_redis:
            KrxDbWriter._sync_db_write(self.SAMPLE_TICK)

        mock_redis.assert_not_called()
        # get_latest_source_rate도 호출 안 함 — early return
        mock_get_latest.assert_not_called()

    def test_redis_exception_isolated_from_writer_loop(self):
        """★ Redis write 예외가 _sync_db_write에서 raise되지 않음 (best-effort 격리)."""
        from app.crawlers.krx_kis import KrxDbWriter

        fake_latest = {"rate": 1485.1, "timestamp": "2026-05-13T15:00:00+09:00"}
        fake_db = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.__enter__ = MagicMock(return_value=fake_db)
        fake_ctx.__exit__ = MagicMock(return_value=False)

        with patch("app.database.get_db_context", return_value=fake_ctx), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch("app.crud.get_latest_source_rate", return_value=fake_latest), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
                   side_effect=RuntimeError("redis dead")), \
             patch("app.crawlers.krx_kis.logger.exception") as mock_log:
            # raise X — 정상 return
            try:
                KrxDbWriter._sync_db_write(self.SAMPLE_TICK)
            except Exception:
                self.fail("Redis 예외가 _sync_db_write에서 전파됨 — 격리 실패")

        mock_log.assert_called_once()


# ---------------------------------------------------------------------------
# Group 4 — builder Redis-first read
# ---------------------------------------------------------------------------

class TestKrxTopicEntryRedisFirst(unittest.TestCase):
    """load_krx_topic_entry의 Redis-first + DB fallback (ADR-038 D2 — 구 usdt:krw builder
    KRX Redis-first 계약을 krx_topic_publisher로 이관, 동일 계약)."""

    def test_redis_hit_skips_db_query(self):
        """KRX Redis hit → DB get_latest_source_rate 미호출."""
        from app.krx_topic_publisher import load_krx_topic_entry

        krx_redis = {
            "source": "krx", "asset": "usd-krw-futures",
            "rate": 1485.0, "timestamp": "2026-05-13T15:00:00+09:00",
        }
        fake_db = MagicMock()
        with patch("app.latest_rates_cache.get_latest_krx_rate_from_sync_job",
                   return_value=krx_redis), \
             patch("app.crud.get_latest_source_rate") as mock_db:
            entry = load_krx_topic_entry(fake_db)

        mock_db.assert_not_called()
        self.assertIsNotNone(entry)
        self.assertEqual(entry["rate"], 1485.0)
        self.assertEqual((entry["source"], entry["asset"]), ("krx", "usd-krw-futures"))

    def test_redis_miss_falls_back_to_db(self):
        """KRX Redis miss → DB get_latest_source_rate 호출 (db 세션 제공 시)."""
        from app.krx_topic_publisher import load_krx_topic_entry

        # crud.get_latest_source_rate 실제 반환 shape = legacy dict (bank/currency 키) —
        # MagicMock attribute row로 잠그면 회귀를 못 잡음 (codex blocker 019f4117)
        db_row = {
            "currency": "usd-krw-futures", "bank": "krx",
            "rate": 1486.0, "timestamp": "2026-05-13T15:00:00+09:00",
        }
        fake_db = MagicMock()
        with patch("app.latest_rates_cache.get_latest_krx_rate_from_sync_job",
                   return_value=None), \
             patch("app.crud.get_latest_source_rate", return_value=db_row) as mock_db:
            entry = load_krx_topic_entry(fake_db)

        mock_db.assert_called_once_with(fake_db, "krx", "usd-krw-futures")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["rate"], 1486.0)

    def test_no_db_session_skips_fallback(self):
        """db=None(publish hot path) → Redis miss 시 DB fallback 생략, None 반환."""
        from app.krx_topic_publisher import load_krx_topic_entry

        with patch("app.latest_rates_cache.get_latest_krx_rate_from_sync_job",
                   return_value=None), \
             patch("app.crud.get_latest_source_rate") as mock_db:
            entry = load_krx_topic_entry(None)

        mock_db.assert_not_called()
        self.assertIsNone(entry)


# ---------------------------------------------------------------------------
# Group 5 — KrxRedisLatestWriter 단위 (PR Next-C, 책임 분리 behavior-change-0)
# ---------------------------------------------------------------------------


class TestKrxRedisLatestWriter(unittest.TestCase):
    """KRX_FANOUT_REFACTOR_PLAN 5.1.C — KrxDbWriter에서 Redis write-through 책임 분리.

    ADR-031 timing 시맨틱 그대로 유지 (DB insert 성공 후, 같은 sync 컨텍스트).
    tick-level write가 *아님*. 단위 테스트 3개로 behavior-change-0 잠금.
    """

    SOURCE = "krx"
    ASSET = "usd-krw-futures"

    def test_calls_redis_helper_when_latest_found(self):
        """latest row 있음 → set_latest_krx_rate_from_sync_job 정확 호출."""
        from app.crawlers.krx_kis import KrxRedisLatestWriter

        fake_latest = {
            "rate": 1485.1,
            "timestamp": "2026-05-13T15:00:00+09:00",
        }
        fake_db = MagicMock()

        with patch("app.crud.get_latest_source_rate", return_value=fake_latest), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
                   return_value=True) as mock_redis:
            KrxRedisLatestWriter.write_after_db_insert(fake_db, self.SOURCE, self.ASSET)

        mock_redis.assert_called_once()
        kwargs = mock_redis.call_args.kwargs
        self.assertEqual(kwargs["asset"], self.ASSET)
        self.assertEqual(kwargs["rate"], 1485.1)
        self.assertEqual(kwargs["timestamp"], "2026-05-13T15:00:00+09:00")

    def test_skips_redis_when_latest_is_none(self):
        """latest row 없음 (get_latest_source_rate None 반환) → Redis helper 미호출."""
        from app.crawlers.krx_kis import KrxRedisLatestWriter

        fake_db = MagicMock()
        with patch("app.crud.get_latest_source_rate", return_value=None), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job") as mock_redis:
            KrxRedisLatestWriter.write_after_db_insert(fake_db, self.SOURCE, self.ASSET)

        mock_redis.assert_not_called()

    def test_isolates_redis_helper_exception(self):
        """★ Redis helper raise → 호출자로 전파 X + logger.exception 호출.

        ADR-031 격리 시맨틱 유지 — DB writer loop / 호출자 흐름 영향 X.
        """
        from app.crawlers import krx_kis as krx_module
        from app.crawlers.krx_kis import KrxRedisLatestWriter

        fake_latest = {"rate": 1485.1, "timestamp": "2026-05-13T15:00:00+09:00"}
        fake_db = MagicMock()

        with patch("app.crud.get_latest_source_rate", return_value=fake_latest), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job",
                   side_effect=RuntimeError("redis dead")), \
             patch.object(krx_module.logger, "exception") as mock_log:
            try:
                KrxRedisLatestWriter.write_after_db_insert(fake_db, self.SOURCE, self.ASSET)
            except Exception:
                self.fail("Redis helper 예외가 KrxRedisLatestWriter에서 전파됨 — 격리 실패")

        mock_log.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
