"""USDT direct Redis write Foundation 단위 테스트 (PR Z-2e B-Step 1 재시도).

이번 spec은 sync `redis.Redis` client 사용 — 직전 실패(asyncio.run + main loop
async client mismatch + circuit_breaker 오염) 정면 해결.

검증 4 group:
    1. _get_sync_client — lazy init + 재호출 시 cache, env reset 동작
    2. set_latest_source_rate_from_sync_job — key/value sync `.set()` 호출 정확
    3. set_latest_source_rate_from_sync_job — Redis 예외 시 False + warning
    4. _mirror_changed_source_to_redis — inserted=True 호출, latest=None skip

검증 안 됨 (단위 mock 한계 — D1 dry-run에서 실제 검증):
    - 진짜 sync `redis.Redis` client가 scheduler thread에서 정상 동작하는지
    - mirrored_at - timestamp가 ms 단위인지

운영 영향:
- 사용자-facing 영향 0
- hot path 소비자 변화 0
- 변경 INSERT 시 best-effort sync Redis SET 추가
- **async circuit_breaker 미사용** — broadcast/mirror Redis path 격리 보장
- 실패 시 read path DB fallback이 안전망 (USDT는 Z-2d allowlist 미통과로
  mirror cycle skip — ADR-029)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import latest_rates_cache
from app.latest_rates_cache import (
    _get_sync_client,
    set_latest_source_rate_from_sync_job,
)


# ---------------------------------------------------------------------------
# Group 1 — _get_sync_client lazy init + reset 동작
# ---------------------------------------------------------------------------

class TestGetSyncClient(unittest.TestCase):

    def setUp(self):
        # 매 테스트 시작 시 module-level cache reset (Codex 권고)
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def test_lazy_init_creates_client(self):
        """첫 호출 시 redis_sync.from_url 호출 + client 캐싱."""
        fake_client = MagicMock(name="fake_redis_client")
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          return_value=fake_client) as mock_from_url:
            result = _get_sync_client()
        self.assertIs(result, fake_client)
        mock_from_url.assert_called_once()
        # 두 번째 호출은 cache 사용 — from_url 추가 호출 X
        with patch.object(latest_rates_cache.redis_sync, "from_url") as mock_again:
            result2 = _get_sync_client()
        self.assertIs(result2, fake_client)
        mock_again.assert_not_called()

    def test_reset_recreates_client(self):
        """_sync_client = None 후 호출 → 재생성 (Codex 권고: env 변경 테스트 친화)."""
        fake_client_1 = MagicMock(name="client_1")
        fake_client_2 = MagicMock(name="client_2")
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          return_value=fake_client_1):
            first = _get_sync_client()
        self.assertIs(first, fake_client_1)
        # reset
        latest_rates_cache._sync_client = None
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          return_value=fake_client_2):
            second = _get_sync_client()
        self.assertIs(second, fake_client_2)
        self.assertIsNot(second, fake_client_1)

    def test_init_failure_returns_none_and_logs(self):
        """from_url 예외 시 None 반환 + warning 로그 (best-effort).

        logger.warning patch — traceback 회귀 stderr 노출 차단 + contract 검증.
        """
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("connect fail")), \
             patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            result = _get_sync_client()
        self.assertIsNone(result)
        mock_warn.assert_called_once()

    def test_reads_config_at_call_time(self):
        """env는 호출 시점에 config에서 read (테스트 친화)."""
        from app import config
        fake_client = MagicMock()
        with patch.object(config, "REDIS_URL", "redis://test-host:1234"), \
             patch.object(config, "REDIS_PASSWORD", "test-pw"), \
             patch.object(latest_rates_cache.redis_sync, "from_url",
                          return_value=fake_client) as mock_from_url:
            _get_sync_client()
        call_args = mock_from_url.call_args
        self.assertEqual(call_args.args[0], "redis://test-host:1234")
        self.assertEqual(call_args.kwargs["password"], "test-pw")
        # socket_timeout 짧게 설정 검증
        self.assertEqual(call_args.kwargs["socket_timeout"], 1.0)
        self.assertEqual(call_args.kwargs["socket_connect_timeout"], 1.0)


# ---------------------------------------------------------------------------
# Group 2 — set_latest_source_rate_from_sync_job key/value 검증
# ---------------------------------------------------------------------------

class TestSetLatestSourceRateFromSyncJob(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def test_calls_set_with_correct_key_and_value(self):
        """latest_key_source + serialize_value 정확 사용."""
        fake_client = MagicMock()
        latest_rates_cache._sync_client = fake_client  # 직접 주입
        result = set_latest_source_rate_from_sync_job(
            source="upbit",
            asset="usdt-krw",
            rate=1485.5,
            timestamp="2026-05-12T15:00:00+09:00",
        )
        self.assertTrue(result)
        fake_client.set.assert_called_once()
        called_args = fake_client.set.call_args.args
        self.assertEqual(called_args[0], "latest:source:upbit:usdt-krw")
        # value JSON에 rate, timestamp, mirrored_at 포함
        import json
        parsed = json.loads(called_args[1])
        self.assertEqual(parsed["rate"], 1485.5)
        self.assertEqual(parsed["timestamp"], "2026-05-12T15:00:00+09:00")
        self.assertIn("mirrored_at", parsed)

    def test_returns_false_when_client_init_fails(self):
        """init 실패 → False (best-effort skip).

        logger.warning patch — _get_sync_client init 실패 시 발생하는
        warning(exc_info=True) traceback이 회귀 stderr 노출되지 않도록 차단 +
        warning 호출 contract 검증.
        """
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            result = set_latest_source_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        self.assertFalse(result)
        mock_warn.assert_called_once()


# ---------------------------------------------------------------------------
# Group 3 — Redis 예외 시 False + warning
# ---------------------------------------------------------------------------

class TestSyncRedisExceptionIsolation(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def test_set_exception_returns_false_with_warning(self):
        """sync client .set() 예외 → False + warning (async circuit 미호출)."""
        fake_client = MagicMock()
        fake_client.set.side_effect = ConnectionError("simulated redis fault")
        latest_rates_cache._sync_client = fake_client
        with patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            result = set_latest_source_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        self.assertFalse(result)
        mock_warn.assert_called_once()

    def test_does_not_touch_async_circuit_breaker(self):
        """sync helper는 async circuit_breaker(record_success/failure) 미호출.

        이번 실패의 핵심 원인 회귀 차단 — broadcast/mirror Redis path 격리.
        logger.warning patch — ConnectionError traceback이 회귀 stderr 노출 차단.
        """
        fake_client = MagicMock()
        fake_client.set.side_effect = ConnectionError("fault")
        latest_rates_cache._sync_client = fake_client

        # async circuit_breaker mock — 어떤 호출도 없어야 함
        with patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_success") as mock_success, \
             patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_failure") as mock_failure, \
             patch.object(latest_rates_cache.logger, "warning"):
            set_latest_source_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        mock_success.assert_not_called()
        mock_failure.assert_not_called()


# ---------------------------------------------------------------------------
# Group 4 — _mirror_changed_source_to_redis helper
# ---------------------------------------------------------------------------

class TestMirrorChangedSourceToRedis(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def test_calls_sync_helper_when_latest_found(self):
        from app.crawlers import usdt_sources
        fake_latest = {
            "bank": "upbit",
            "currency": "usdt-krw",
            "rate": 1485.5,
            "timestamp": "2026-05-12T15:00:00+09:00",
        }
        db = MagicMock()
        with patch("app.crawlers.usdt_sources.crud.get_latest_source_rate",
                   return_value=fake_latest) as mock_get, \
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_source_rate_from_sync_job",
                   return_value=True) as mock_set:
            result = usdt_sources._mirror_changed_source_to_redis(
                db=db, source="upbit", asset="usdt-krw"
            )
        self.assertTrue(result)
        mock_get.assert_called_once_with(db, "upbit", "usdt-krw")
        mock_set.assert_called_once_with(
            source="upbit",
            asset="usdt-krw",
            rate=1485.5,
            timestamp="2026-05-12T15:00:00+09:00",
        )

    def test_skips_when_latest_none(self):
        """latest 재조회 None → helper 호출 X + warning."""
        from app.crawlers import usdt_sources
        db = MagicMock()
        with patch("app.crawlers.usdt_sources.crud.get_latest_source_rate",
                   return_value=None) as mock_get, \
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_source_rate_from_sync_job",
                   return_value=True) as mock_set, \
             patch("app.crawlers.usdt_sources.logger.warning") as mock_warn:
            result = usdt_sources._mirror_changed_source_to_redis(
                db=db, source="upbit", asset="usdt-krw"
            )
        self.assertFalse(result)
        mock_get.assert_called_once_with(db, "upbit", "usdt-krw")
        mock_set.assert_not_called()
        mock_warn.assert_called_once()

    def test_isolates_redis_exception(self):
        """sync helper 예외 → False, 호출자에 전파 X."""
        from app.crawlers import usdt_sources
        fake_latest = {
            "bank": "upbit",
            "currency": "usdt-krw",
            "rate": 1485.5,
            "timestamp": "2026-05-12T15:00:00+09:00",
        }
        db = MagicMock()
        with patch("app.crawlers.usdt_sources.crud.get_latest_source_rate",
                   return_value=fake_latest), \
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_source_rate_from_sync_job",
                   side_effect=RuntimeError("simulated")), \
             patch("app.crawlers.usdt_sources.logger.exception") as mock_log:
            result = usdt_sources._mirror_changed_source_to_redis(
                db=db, source="upbit", asset="usdt-krw"
            )
        self.assertFalse(result)
        mock_log.assert_called_once()


# ---------------------------------------------------------------------------
# PR Z-2e B-Step Telemetry: 호출 site counter 증가 검증
# ---------------------------------------------------------------------------

class TestDirectWriteStatsCounters(unittest.TestCase):
    """set_latest_source_rate_from_sync_job 호출 site에서 stats counter가 정확히
    증가하는지 잠금. 모듈 단위 test(test_usdt_redis_stats.py)는 record_* 자체만
    검증 — 호출 site에서 record_*가 정말 호출되는지는 별도 검증 필요.
    """

    def setUp(self):
        latest_rates_cache._sync_client = None
        from app import usdt_redis_stats
        usdt_redis_stats.reset_stats()

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def test_success_increments_direct_write_success(self):
        from app import usdt_redis_stats
        fake_client = MagicMock()
        latest_rates_cache._sync_client = fake_client
        set_latest_source_rate_from_sync_job(
            "upbit", "usdt-krw", 1485.5, "2026-05-12T15:00:00+09:00"
        )
        stats = usdt_redis_stats.get_stats()
        upbit = stats["per_source"]["upbit"]
        self.assertEqual(upbit["direct_write_success"], 1)
        self.assertEqual(upbit["direct_write_failure"], 0)

    def test_client_init_failure_increments_direct_write_failure(self):
        from app import usdt_redis_stats
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning"):
            set_latest_source_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        stats = usdt_redis_stats.get_stats()
        upbit = stats["per_source"]["upbit"]
        self.assertEqual(upbit["direct_write_failure"], 1)
        self.assertEqual(upbit["direct_write_success"], 0)

    def test_set_exception_increments_direct_write_failure(self):
        from app import usdt_redis_stats
        fake_client = MagicMock()
        fake_client.set.side_effect = ConnectionError("fault")
        latest_rates_cache._sync_client = fake_client
        with patch.object(latest_rates_cache.logger, "warning"):
            set_latest_source_rate_from_sync_job(
                "bithumb", "usdt-krw", 1486.0, "2026-05-12T15:00:00+09:00"
            )
        stats = usdt_redis_stats.get_stats()
        bithumb = stats["per_source"]["bithumb"]
        self.assertEqual(bithumb["direct_write_failure"], 1)


class TestRedisReadStatsCounters(unittest.TestCase):
    """get_latest_source_rate_from_sync_job 호출 site counter 검증."""

    def setUp(self):
        latest_rates_cache._sync_client = None
        from app import usdt_redis_stats
        usdt_redis_stats.reset_stats()

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def test_hit_increments_redis_read_hit(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_source_rate_from_sync_job
        import json
        from datetime import datetime, timezone, timedelta

        kst = timezone(timedelta(hours=9))
        fake_value = json.dumps({
            "rate": 1485.0,
            "timestamp": "2026-05-12T15:00:00+09:00",
            "mirrored_at": datetime.now(kst).isoformat(),
        })
        fake_client = MagicMock()
        fake_client.get.return_value = fake_value.encode("utf-8")
        latest_rates_cache._sync_client = fake_client

        result = get_latest_source_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNotNone(result)
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_hit"], 1)
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_miss"], 0)

    def test_miss_increments_redis_read_miss(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_source_rate_from_sync_job

        fake_client = MagicMock()
        fake_client.get.return_value = None
        latest_rates_cache._sync_client = fake_client

        result = get_latest_source_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNone(result)
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_miss"], 1)

    def test_parse_fail_increments_redis_read_parse_fail(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_source_rate_from_sync_job

        # naive datetime → deserialize_value None 반환
        import json
        bad_value = json.dumps({
            "rate": 1485.0,
            "timestamp": "...",
            "mirrored_at": "2026-05-12T15:00:00",  # naive (no tz)
        })
        fake_client = MagicMock()
        fake_client.get.return_value = bad_value.encode("utf-8")
        latest_rates_cache._sync_client = fake_client

        result = get_latest_source_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNone(result)
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_parse_fail"], 1)

    def test_get_exception_increments_redis_read_error(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_source_rate_from_sync_job

        fake_client = MagicMock()
        fake_client.get.side_effect = ConnectionError("fault")
        latest_rates_cache._sync_client = fake_client

        with patch.object(latest_rates_cache.logger, "warning"):
            result = get_latest_source_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNone(result)
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_error"], 1)


# ---------------------------------------------------------------------------
# PR Z-2e Step 3a: bank/investing Redis-first read helper
# ---------------------------------------------------------------------------

class TestBankRedisReadHelper(unittest.TestCase):
    """get_latest_bank_rate_from_sync_job — mirror cycle 갱신 가정 + is_stale 적용.

    USDT helper와 다른 환경 (ADR-026 mirror 기반 vs ADR-029 direct write).
    """

    def setUp(self):
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def _redis_value(self, rate: float, mirrored_age_seconds: float = 0.0) -> bytes:
        """fresh mirrored_at으로 value JSON 생성. age_seconds 증가하면 stale."""
        from datetime import datetime, timezone, timedelta
        import json
        kst = timezone(timedelta(hours=9))
        mirrored_at = datetime.now(kst) - timedelta(seconds=mirrored_age_seconds)
        return json.dumps({
            "rate": rate,
            "timestamp": "2026-05-13T15:00:00+09:00",
            "mirrored_at": mirrored_at.isoformat(),
        }).encode("utf-8")

    def test_fresh_hit_returns_entry(self):
        from app.latest_rates_cache import get_latest_bank_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.get.return_value = self._redis_value(1370.0, mirrored_age_seconds=1.0)
        latest_rates_cache._sync_client = fake_client
        result = get_latest_bank_rate_from_sync_job("kb", "usd-krw")
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "kb")
        self.assertEqual(result["asset"], "usd-krw")
        self.assertEqual(result["rate"], 1370.0)
        fake_client.get.assert_called_once_with("latest:bank:kb:usd-krw")

    def test_miss_returns_none(self):
        from app.latest_rates_cache import get_latest_bank_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.get.return_value = None
        latest_rates_cache._sync_client = fake_client
        self.assertIsNone(get_latest_bank_rate_from_sync_job("kb", "usd-krw"))

    def test_stale_returns_none(self):
        """mirrored_at이 6초 초과(STALE_RATIO=2 × 3초 interval) → None."""
        from app.latest_rates_cache import get_latest_bank_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.get.return_value = self._redis_value(1370.0, mirrored_age_seconds=10.0)
        latest_rates_cache._sync_client = fake_client
        self.assertIsNone(get_latest_bank_rate_from_sync_job("kb", "usd-krw"))

    def test_parse_fail_returns_none(self):
        from app.latest_rates_cache import get_latest_bank_rate_from_sync_job
        import json
        fake_client = MagicMock()
        # naive datetime → deserialize_value None
        fake_client.get.return_value = json.dumps({
            "rate": 1.0, "timestamp": "...",
            "mirrored_at": "2026-05-13T15:00:00",  # naive
        }).encode("utf-8")
        latest_rates_cache._sync_client = fake_client
        self.assertIsNone(get_latest_bank_rate_from_sync_job("kb", "usd-krw"))

    def test_exception_returns_none(self):
        from app.latest_rates_cache import get_latest_bank_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.get.side_effect = ConnectionError("fault")
        latest_rates_cache._sync_client = fake_client
        with patch.object(latest_rates_cache.logger, "warning"):
            self.assertIsNone(get_latest_bank_rate_from_sync_job("kb", "usd-krw"))


class TestInvestingRedisReadHelper(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None

    def tearDown(self):
        latest_rates_cache._sync_client = None

    def _fresh_value(self, rate: float = 1371.0) -> bytes:
        from datetime import datetime, timezone, timedelta
        import json
        kst = timezone(timedelta(hours=9))
        return json.dumps({
            "rate": rate,
            "timestamp": "2026-05-13T15:00:00+09:00",
            "mirrored_at": datetime.now(kst).isoformat(),
        }).encode("utf-8")

    def test_hit_returns_topic_native_with_source_investing(self):
        from app.latest_rates_cache import get_latest_investing_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.get.return_value = self._fresh_value(1371.5)
        latest_rates_cache._sync_client = fake_client
        result = get_latest_investing_rate_from_sync_job("usd-krw")
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "investing")
        self.assertEqual(result["asset"], "usd-krw")
        self.assertEqual(result["rate"], 1371.5)
        fake_client.get.assert_called_once_with("latest:investing:usd-krw")

    def test_miss_returns_none(self):
        from app.latest_rates_cache import get_latest_investing_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.get.return_value = None
        latest_rates_cache._sync_client = fake_client
        self.assertIsNone(get_latest_investing_rate_from_sync_job("usd-krw"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
