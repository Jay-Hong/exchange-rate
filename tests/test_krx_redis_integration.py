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

class TestTetherTabPayloadKrxRedisFirst(unittest.TestCase):
    """load_and_build_tether_tab_payload의 KRX Redis-first 분기."""

    def _build_helper_mocks(self, krx_redis_value):
        """common patches for builder context."""
        # USDT 5거래소 / 은행 2개 / Investing 등 다른 source는 정상 동작 가정
        # KRX만 점검
        from app import usdt_topic_payload as utp

        usdt_value = {
            "source": "upbit", "asset": "usdt-krw",
            "rate": 1400.0, "timestamp": "2026-05-13T15:00:00+09:00",
        }
        bank_value = {
            "source": "kb", "asset": "usd-krw",
            "rate": 1485.0, "timestamp": "2026-05-13T15:00:00+09:00",
        }
        inv_value = {
            "source": "investing", "asset": "usd-krw",
            "rate": 1485.5, "timestamp": "2026-05-13T15:00:00+09:00",
        }

        return {
            "krx_redis": patch.object(utp, "get_latest_krx_rate_from_sync_job",
                                      return_value=krx_redis_value),
            "krx_db": patch("app.usdt_topic_payload.get_latest_source_rate"),
            "usdt": patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                                 return_value=usdt_value),
            "bank": patch.object(utp, "get_latest_bank_rate_from_sync_job",
                                 return_value=bank_value),
            "inv": patch.object(utp, "get_latest_investing_rate_from_sync_job",
                                return_value=inv_value),
        }

    def test_redis_hit_skips_db_query(self):
        """KRX Redis hit → DB get_latest_source_rate 미호출."""
        from app.usdt_topic_payload import load_and_build_tether_tab_payload

        krx_redis = {
            "source": "krx", "asset": "usd-krw-futures",
            "rate": 1485.0, "timestamp": "2026-05-13T15:00:00+09:00",
        }
        mocks = self._build_helper_mocks(krx_redis)
        fake_db = MagicMock()

        with mocks["krx_redis"], mocks["krx_db"] as mock_db, \
             mocks["usdt"], mocks["bank"], mocks["inv"]:
            payload = load_and_build_tether_tab_payload(fake_db, include_krx=True)

        # ★ KRX DB query 미호출 (Redis hit 시)
        mock_db.assert_not_called()
        # KRX 데이터 포함 확인
        self.assertIn("usd_krw_futures", payload["data"])
        self.assertEqual(payload["data"]["usd_krw_futures"]["rate"], 1485.0)

    def test_redis_miss_falls_back_to_db(self):
        """KRX Redis miss → DB get_latest_source_rate 호출."""
        from app.usdt_topic_payload import load_and_build_tether_tab_payload

        krx_db_value = {
            "source": "krx", "currency": "usd-krw-futures",
            "rate": 1486.0, "timestamp": "2026-05-13T15:00:00+09:00",
            "bank": "krx",
        }
        mocks = self._build_helper_mocks(None)  # Redis miss
        fake_db = MagicMock()

        with mocks["krx_redis"], \
             patch("app.usdt_topic_payload.get_latest_source_rate",
                   return_value=krx_db_value) as mock_db, \
             mocks["usdt"], mocks["bank"], mocks["inv"]:
            payload = load_and_build_tether_tab_payload(fake_db, include_krx=True)

        # DB fallback 호출
        mock_db.assert_called_once_with(fake_db, "krx", "usd-krw-futures")
        # KRX 데이터 DB 값으로 포함
        self.assertIn("usd_krw_futures", payload["data"])
        self.assertEqual(payload["data"]["usd_krw_futures"]["rate"], 1486.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
