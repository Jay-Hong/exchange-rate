"""USDT direct Redis write Foundation 단위 테스트 (PR Z-2e B-Step 1 재시도).

이번 spec은 sync `redis.Redis` client 사용 — 직전 실패(asyncio.run + main loop
async client mismatch + circuit_breaker 오염) 정면 해결.

검증 4 group:
    1. _get_sync_client — lazy init + 재호출 시 cache, env reset 동작
    2. set_latest_usdt_rate_from_sync_job — key/value sync `.set()` 호출 정확
    3. set_latest_usdt_rate_from_sync_job — Redis 예외 시 False + warning
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
from datetime import datetime, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from app import latest_rates_cache
from app import bank_investing_redis_stats
from app.latest_rates_cache import (
    UsdtLatestWriteOutcome,
    _get_sync_client,
    set_latest_usdt_rate_from_sync_job,
)


# ---------------------------------------------------------------------------
# Group 1 — _get_sync_client lazy init + reset 동작
# ---------------------------------------------------------------------------

class TestGetSyncClient(unittest.TestCase):

    def setUp(self):
        # 매 테스트 시작 시 module-level cache reset (Codex 권고)
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

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
        latest_rates_cache._last_written_usdt_state.clear()
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
# Group 2 — set_latest_usdt_rate_from_sync_job key/value 검증
# ---------------------------------------------------------------------------

class TestSetLatestUsdtRateFromSyncJob(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def test_calls_set_with_correct_key_and_value(self):
        """latest_key_source + serialize_value 정확 사용."""
        fake_client = MagicMock()
        latest_rates_cache._sync_client = fake_client  # 직접 주입
        result = set_latest_usdt_rate_from_sync_job(
            source="upbit",
            asset="usdt-krw",
            rate=1485.5,
            timestamp="2026-05-12T15:00:00+09:00",
        )
        self.assertIs(result, UsdtLatestWriteOutcome.SET)
        fake_client.set.assert_called_once()
        called_args = fake_client.set.call_args.args
        self.assertEqual(called_args[0], "latest:source:upbit:usdt-krw")
        # value JSON에 rate, timestamp, mirrored_at 포함
        import json
        parsed = json.loads(called_args[1])
        self.assertEqual(parsed["rate"], 1485.5)
        self.assertEqual(parsed["timestamp"], "2026-05-12T15:00:00+09:00")
        self.assertIn("mirrored_at", parsed)

    def test_returns_failed_when_client_init_fails(self):
        """init 실패 → FAILED (best-effort skip).

        logger.warning patch — _get_sync_client init 실패 시 발생하는
        warning(exc_info=True) traceback이 회귀 stderr 노출되지 않도록 차단 +
        warning 호출 contract 검증.
        """
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            result = set_latest_usdt_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        self.assertIs(result, UsdtLatestWriteOutcome.FAILED)
        mock_warn.assert_called_once()


# ---------------------------------------------------------------------------
# Group 3 — Redis 예외 시 False + warning
# ---------------------------------------------------------------------------

class TestSyncRedisExceptionIsolation(unittest.TestCase):

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def test_set_exception_returns_failed_with_warning(self):
        """sync client .set() 예외 → FAILED + warning (async circuit 미호출)."""
        fake_client = MagicMock()
        fake_client.set.side_effect = ConnectionError("simulated redis fault")
        latest_rates_cache._sync_client = fake_client
        with patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            result = set_latest_usdt_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        self.assertIs(result, UsdtLatestWriteOutcome.FAILED)
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
            set_latest_usdt_rate_from_sync_job(
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
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

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
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
                   return_value=UsdtLatestWriteOutcome.SET) as mock_set:
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
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
                   return_value=UsdtLatestWriteOutcome.SET) as mock_set, \
             patch("app.crawlers.usdt_sources.logger.warning") as mock_warn:
            result = usdt_sources._mirror_changed_source_to_redis(
                db=db, source="upbit", asset="usdt-krw"
            )
        self.assertFalse(result)
        mock_get.assert_called_once_with(db, "upbit", "usdt-krw")
        mock_set.assert_not_called()
        mock_warn.assert_called_once()

    def test_skipped_outcome_treated_as_success(self):
        """(5d-a) helper SKIPPED outcome → mirror bool interface True (no false-positive warning).

        SKIPPED는 정상적인 5s grain coalesce — caller에게 success로 반환.
        """
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
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
                   return_value=UsdtLatestWriteOutcome.SKIPPED), \
             patch("app.crawlers.usdt_sources.logger.warning") as mock_warn:
            result = usdt_sources._mirror_changed_source_to_redis(
                db=db, source="upbit", asset="usdt-krw"
            )
        self.assertTrue(result)
        mock_warn.assert_not_called()

    def test_failed_outcome_returns_false_with_warning(self):
        """(5d-a) helper FAILED outcome → mirror bool interface False + warning."""
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
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
                   return_value=UsdtLatestWriteOutcome.FAILED), \
             patch("app.crawlers.usdt_sources.logger.warning") as mock_warn:
            result = usdt_sources._mirror_changed_source_to_redis(
                db=db, source="upbit", asset="usdt-krw"
            )
        self.assertFalse(result)
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
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
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
    """set_latest_usdt_rate_from_sync_job 호출 site에서 stats counter가 정확히
    증가하는지 잠금. 모듈 단위 test(test_usdt_redis_stats.py)는 record_* 자체만
    검증 — 호출 site에서 record_*가 정말 호출되는지는 별도 검증 필요.
    """

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()
        from app import usdt_redis_stats
        usdt_redis_stats.reset_stats()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def test_success_increments_direct_write_success(self):
        from app import usdt_redis_stats
        fake_client = MagicMock()
        latest_rates_cache._sync_client = fake_client
        set_latest_usdt_rate_from_sync_job(
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
            set_latest_usdt_rate_from_sync_job(
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
            set_latest_usdt_rate_from_sync_job(
                "bithumb", "usdt-krw", 1486.0, "2026-05-12T15:00:00+09:00"
            )
        stats = usdt_redis_stats.get_stats()
        bithumb = stats["per_source"]["bithumb"]
        self.assertEqual(bithumb["direct_write_failure"], 1)


class TestRedisReadStatsCounters(unittest.TestCase):
    """get_latest_usdt_rate_from_sync_job 호출 site counter 검증."""

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()
        from app import usdt_redis_stats
        usdt_redis_stats.reset_stats()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def test_hit_increments_redis_read_hit(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_usdt_rate_from_sync_job
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

        result = get_latest_usdt_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNotNone(result)
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_hit"], 1)
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_miss"], 0)

    def test_miss_increments_redis_read_miss(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_usdt_rate_from_sync_job

        fake_client = MagicMock()
        fake_client.get.return_value = None
        latest_rates_cache._sync_client = fake_client

        result = get_latest_usdt_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNone(result)
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_miss"], 1)

    def test_parse_fail_increments_redis_read_parse_fail(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_usdt_rate_from_sync_job

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

        result = get_latest_usdt_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNone(result)
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"]["upbit"]["redis_read_parse_fail"], 1)

    def test_get_exception_increments_redis_read_error(self):
        from app import usdt_redis_stats
        from app.latest_rates_cache import get_latest_usdt_rate_from_sync_job

        fake_client = MagicMock()
        fake_client.get.side_effect = ConnectionError("fault")
        latest_rates_cache._sync_client = fake_client

        with patch.object(latest_rates_cache.logger, "warning"):
            result = get_latest_usdt_rate_from_sync_job("upbit", "usdt-krw")
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
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

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
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

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


# ---------------------------------------------------------------------------
# Step 3b — bank/investing direct write (set helper + crud wrapper)
# ---------------------------------------------------------------------------


class TestSetLatestBankRateFromSyncJob(unittest.TestCase):
    """bank latest direct writer 단위 (PR Z-2e Step 3b)."""

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()
        bank_investing_redis_stats.reset_stats()  # item 4: 전역 telemetry 누적 격리

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()
        bank_investing_redis_stats.reset_stats()

    def test_calls_set_with_correct_key_and_value(self):
        from app.latest_rates_cache import set_latest_bank_rate_from_sync_job
        fake_client = MagicMock()
        latest_rates_cache._sync_client = fake_client
        result = set_latest_bank_rate_from_sync_job(
            bank="kb",
            asset="usd-krw",
            rate=1371.5,
            timestamp="2026-05-13T10:00:00+09:00",
        )
        self.assertTrue(result)
        fake_client.set.assert_called_once()
        called_args = fake_client.set.call_args.args
        self.assertEqual(called_args[0], "latest:bank:kb:usd-krw")
        import json
        parsed = json.loads(called_args[1])
        self.assertEqual(parsed["rate"], 1371.5)
        self.assertEqual(parsed["timestamp"], "2026-05-13T10:00:00+09:00")
        self.assertIn("mirrored_at", parsed)

    def test_returns_false_when_client_init_fails(self):
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning"):
            from app.latest_rates_cache import set_latest_bank_rate_from_sync_job
            result = set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1371.5, "2026-05-13T10:00:00+09:00"
            )
        self.assertFalse(result)

    def test_set_exception_returns_false_and_skips_async_circuit(self):
        from app.latest_rates_cache import set_latest_bank_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.set.side_effect = ConnectionError("redis fault")
        latest_rates_cache._sync_client = fake_client
        with patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_success") as mock_success, \
             patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_failure") as mock_failure, \
             patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            result = set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1371.5, "2026-05-13T10:00:00+09:00"
            )
        self.assertFalse(result)
        mock_warn.assert_called_once()
        # 핵심: async circuit_breaker는 호출 안 됨 (broadcast/mirror path 격리)
        mock_success.assert_not_called()
        mock_failure.assert_not_called()


class TestSetLatestInvestingRateFromSyncJob(unittest.TestCase):
    """investing latest direct writer 단위 (PR Z-2e Step 3b)."""

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()
        bank_investing_redis_stats.reset_stats()  # item 4: 전역 telemetry 누적 격리

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()
        bank_investing_redis_stats.reset_stats()

    def test_calls_set_with_correct_key_and_value(self):
        from app.latest_rates_cache import set_latest_investing_rate_from_sync_job
        fake_client = MagicMock()
        latest_rates_cache._sync_client = fake_client
        result = set_latest_investing_rate_from_sync_job(
            asset="usd-krw",
            rate=1371.5,
            timestamp="2026-05-13T10:00:00+09:00",
        )
        self.assertTrue(result)
        fake_client.set.assert_called_once()
        called_args = fake_client.set.call_args.args
        self.assertEqual(called_args[0], "latest:investing:usd-krw")
        import json
        parsed = json.loads(called_args[1])
        self.assertEqual(parsed["rate"], 1371.5)
        self.assertEqual(parsed["timestamp"], "2026-05-13T10:00:00+09:00")

    def test_returns_false_when_client_init_fails(self):
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning"):
            from app.latest_rates_cache import set_latest_investing_rate_from_sync_job
            result = set_latest_investing_rate_from_sync_job(
                "usd-krw", 1371.5, "2026-05-13T10:00:00+09:00"
            )
        self.assertFalse(result)

    def test_set_exception_returns_false_and_skips_async_circuit(self):
        from app.latest_rates_cache import set_latest_investing_rate_from_sync_job
        fake_client = MagicMock()
        fake_client.set.side_effect = ConnectionError("redis fault")
        latest_rates_cache._sync_client = fake_client
        with patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_success") as mock_success, \
             patch.object(latest_rates_cache.redis_cache.circuit,
                          "record_failure") as mock_failure, \
             patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            result = set_latest_investing_rate_from_sync_job(
                "usd-krw", 1371.5, "2026-05-13T10:00:00+09:00"
            )
        self.assertFalse(result)
        mock_warn.assert_called_once()
        mock_success.assert_not_called()
        mock_failure.assert_not_called()


class TestBankInvestingSetTelemetryWiring(unittest.TestCase):
    """Layer A 배선 (item 4) — set_latest_* SET outcome이 bank_investing_redis_stats에
    원인별 집계되는지 + telemetry 예외가 writer bool 계약을 깨지 않는지 잠금."""

    _TS = "2026-06-15T10:00:00+09:00"

    def setUp(self):
        latest_rates_cache._sync_client = None
        bank_investing_redis_stats.reset_stats()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        bank_investing_redis_stats.reset_stats()

    def _bank_asset(self, source="kb", asset="usd-krw"):
        return bank_investing_redis_stats.get_stats()["per_source"][source]["per_asset"][asset]

    # ── bank: success + 3 failure zone ──
    def test_bank_success_records_attempt_and_success(self):
        latest_rates_cache._sync_client = MagicMock()
        ok = latest_rates_cache.set_latest_bank_rate_from_sync_job(
            "kb", "usd-krw", 1371.5, self._TS)
        self.assertTrue(ok)
        a = self._bank_asset()
        self.assertEqual((a["attempt"], a["success"], a["failure"]), (1, 1, 0))

    def test_bank_client_unavailable_records_failure(self):
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning"):
            ok = latest_rates_cache.set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1371.5, self._TS)
        self.assertFalse(ok)
        a = self._bank_asset()
        self.assertEqual((a["attempt"], a["success"], a["failure"]), (1, 0, 1))
        self.assertEqual(a["failure_by_reason"]["client_unavailable"], 1)

    def test_bank_client_raise_records_client_unavailable(self):
        # _get_sync_client가 (방어적으로) 예외를 던져도 bool 계약 유지 + client_unavailable
        with patch.object(latest_rates_cache, "_get_sync_client",
                          side_effect=RuntimeError("boom")):
            ok = latest_rates_cache.set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1371.5, self._TS)
        self.assertFalse(ok)
        a = self._bank_asset()
        self.assertEqual(a["failure_by_reason"]["client_unavailable"], 1)
        self.assertEqual(a["last_failure_reason"], "client_unavailable")
        self.assertIsNotNone(a["last_failure_error"])

    def test_bank_writer_exception_logs_with_bank_asset_not_key(self):
        latest_rates_cache._sync_client = MagicMock()
        with patch.object(latest_rates_cache, "latest_key_bank",
                          side_effect=ValueError("bad key")), \
             patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            ok = latest_rates_cache.set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1371.5, self._TS)
        self.assertFalse(ok)
        mock_warn.assert_called_once()
        # key 미정의 시점 → extra는 bank/asset (undefined 'key' 참조 X)
        self.assertEqual(mock_warn.call_args.kwargs["extra"],
                         {"bank": "kb", "asset": "usd-krw"})
        self.assertEqual(self._bank_asset()["failure_by_reason"]["writer_exception"], 1)

    def test_bank_set_exception_records_set_exception(self):
        fake = MagicMock()
        fake.set.side_effect = ConnectionError("redis fault")
        latest_rates_cache._sync_client = fake
        with patch.object(latest_rates_cache.logger, "warning"):
            ok = latest_rates_cache.set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1371.5, self._TS)
        self.assertFalse(ok)
        self.assertEqual(self._bank_asset()["failure_by_reason"]["set_exception"], 1)

    # ── investing: success + zone (source="investing") ──
    def test_investing_success_recorded_under_investing_source(self):
        latest_rates_cache._sync_client = MagicMock()
        ok = latest_rates_cache.set_latest_investing_rate_from_sync_job(
            "usd-krw", 1371.5, self._TS)
        self.assertTrue(ok)
        a = self._bank_asset(source="investing")
        self.assertEqual((a["attempt"], a["success"], a["failure"]), (1, 1, 0))

    def test_investing_client_unavailable_records_failure(self):
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")), \
             patch.object(latest_rates_cache.logger, "warning"):
            ok = latest_rates_cache.set_latest_investing_rate_from_sync_job(
                "usd-krw", 1371.5, self._TS)
        self.assertFalse(ok)
        a = self._bank_asset(source="investing")
        self.assertEqual((a["attempt"], a["success"], a["failure"]), (1, 0, 1))
        self.assertEqual(a["failure_by_reason"]["client_unavailable"], 1)

    def test_investing_set_exception_records_set_exception(self):
        fake = MagicMock()
        fake.set.side_effect = ConnectionError("fault")
        latest_rates_cache._sync_client = fake
        with patch.object(latest_rates_cache.logger, "warning"):
            ok = latest_rates_cache.set_latest_investing_rate_from_sync_job(
                "usd-krw", 1371.5, self._TS)
        self.assertFalse(ok)
        self.assertEqual(
            self._bank_asset(source="investing")["failure_by_reason"]["set_exception"], 1)

    def test_investing_writer_exception_logs_with_asset_only(self):
        latest_rates_cache._sync_client = MagicMock()
        with patch.object(latest_rates_cache, "latest_key_investing",
                          side_effect=ValueError("bad")), \
             patch.object(latest_rates_cache.logger, "warning") as mock_warn:
            ok = latest_rates_cache.set_latest_investing_rate_from_sync_job(
                "usd-krw", 1371.5, self._TS)
        self.assertFalse(ok)
        self.assertEqual(mock_warn.call_args.kwargs["extra"], {"asset": "usd-krw"})
        self.assertEqual(
            self._bank_asset(source="investing")["failure_by_reason"]["writer_exception"], 1)

    # ── quiesced invariant: attempt == success + failure ──
    def test_aggregate_attempt_equals_success_plus_failure(self):
        latest_rates_cache._sync_client = MagicMock()
        latest_rates_cache.set_latest_bank_rate_from_sync_job("kb", "usd-krw", 1.0, self._TS)
        latest_rates_cache.set_latest_investing_rate_from_sync_job("usd-krw", 2.0, self._TS)
        fake = MagicMock()
        fake.set.side_effect = ConnectionError("x")
        latest_rates_cache._sync_client = fake
        with patch.object(latest_rates_cache.logger, "warning"):
            latest_rates_cache.set_latest_bank_rate_from_sync_job("hana", "usd-krw", 3.0, self._TS)
        agg = bank_investing_redis_stats.get_stats()["aggregate"]
        self.assertEqual(agg["attempt"], agg["success"] + agg["failure"])
        self.assertEqual(agg["attempt"], 3)

    # ── telemetry 예외 비전파 (helper 격리) — 3종 각각 raise ──
    def test_telemetry_record_raise_does_not_break_writer(self):
        # record_attempt raise (성공 경로 진입 전) → 여전히 True
        latest_rates_cache._sync_client = MagicMock()
        with patch.object(bank_investing_redis_stats, "record_attempt",
                          side_effect=RuntimeError("boom")):
            self.assertTrue(latest_rates_cache.set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1.0, self._TS))
        # record_success raise (SET 성공 직후) → 여전히 True
        latest_rates_cache._sync_client = MagicMock()
        with patch.object(bank_investing_redis_stats, "record_success",
                          side_effect=RuntimeError("boom")):
            self.assertTrue(latest_rates_cache.set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1.0, self._TS))
        # record_failure raise (SET 실패 경로) → 여전히 False (중단 X)
        fake = MagicMock()
        fake.set.side_effect = ConnectionError("x")
        latest_rates_cache._sync_client = fake
        with patch.object(bank_investing_redis_stats, "record_failure",
                          side_effect=RuntimeError("boom")), \
             patch.object(latest_rates_cache.logger, "warning"):
            self.assertFalse(latest_rates_cache.set_latest_bank_rate_from_sync_job(
                "kb", "usd-krw", 1.0, self._TS))


class TestWriteChangedBankRatesToRedis(unittest.TestCase):
    """crud._write_changed_bank_rates_to_redis 단위 (PR Z-2e Step 3b)."""

    def test_iterates_all_entries(self):
        from app import crud
        redis_updates = [
            {"source": "kb", "asset": "usd-krw", "rate": 1371.0, "timestamp": "2026-05-13T10:00:00+09:00"},
            {"source": "kb", "asset": "jpy-krw", "rate": 9.1, "timestamp": "2026-05-13T10:00:00+09:00"},
        ]
        with patch("app.latest_rates_cache.set_latest_bank_rate_from_sync_job") as mock_set:
            crud._write_changed_bank_rates_to_redis(redis_updates)
        self.assertEqual(mock_set.call_count, 2)
        first_call = mock_set.call_args_list[0]
        self.assertEqual(first_call.kwargs["bank"], "kb")
        self.assertEqual(first_call.kwargs["asset"], "usd-krw")
        self.assertEqual(first_call.kwargs["rate"], 1371.0)

    def test_skips_when_empty(self):
        from app import crud
        with patch("app.latest_rates_cache.set_latest_bank_rate_from_sync_job") as mock_set:
            crud._write_changed_bank_rates_to_redis([])
        mock_set.assert_not_called()

    def test_isolates_helper_exception(self):
        """sync helper가 raise해도 다음 entry 처리 + 호출자 흐름에 영향 X."""
        from app import crud
        redis_updates = [
            {"source": "kb", "asset": "usd-krw", "rate": 1371.0, "timestamp": "2026-05-13T10:00:00+09:00"},
            {"source": "kb", "asset": "jpy-krw", "rate": 9.1, "timestamp": "2026-05-13T10:00:00+09:00"},
        ]
        with patch("app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
                   side_effect=[RuntimeError("boom"), True]) as mock_set, \
             patch.object(crud.logger, "exception") as mock_log:
            # 함수 자체가 raise하지 않아야 함
            crud._write_changed_bank_rates_to_redis(redis_updates)
        self.assertEqual(mock_set.call_count, 2)
        mock_log.assert_called_once()


class TestWriteChangedInvestingRatesToRedis(unittest.TestCase):
    """crud._write_changed_investing_rates_to_redis 단위 (PR Z-2e Step 3b)."""

    def test_iterates_all_entries(self):
        from app import crud
        redis_updates = [
            {"source": "investing", "asset": "usd-krw", "rate": 1371.0, "timestamp": "2026-05-13T10:00:00+09:00"},
            {"source": "investing", "asset": "eur-krw", "rate": 1500.0, "timestamp": "2026-05-13T10:00:00+09:00"},
        ]
        with patch("app.latest_rates_cache.set_latest_investing_rate_from_sync_job") as mock_set:
            crud._write_changed_investing_rates_to_redis(redis_updates)
        self.assertEqual(mock_set.call_count, 2)
        first_call = mock_set.call_args_list[0]
        self.assertEqual(first_call.kwargs["asset"], "usd-krw")
        self.assertEqual(first_call.kwargs["rate"], 1371.0)

    def test_isolates_helper_exception(self):
        from app import crud
        redis_updates = [
            {"source": "investing", "asset": "usd-krw", "rate": 1371.0, "timestamp": "2026-05-13T10:00:00+09:00"},
        ]
        with patch("app.latest_rates_cache.set_latest_investing_rate_from_sync_job",
                   side_effect=RuntimeError("boom")), \
             patch.object(crud.logger, "exception"):
            crud._write_changed_investing_rates_to_redis(redis_updates)


class TestInsertBankRatesCallOrder(unittest.TestCase):
    """insert_bank_rates_into_db 호출 순서 + shape 잠금 (PR Z-2e Step 3b 핵심 회귀 방지).

    1) 호출 순서: db.commit() → _write_changed_bank_rates_to_redis → process_rate_alerts
       역전 시 DB-Redis 비정합 + alert 누락 위험.
    2) redis_updates payload: topic-native shape + DB ts 일관.
    3) changed_rates payload: FCM shape 그대로 (timestamp leak X).
    4) unchanged pair: commit/redis/alerts 모두 skip.
    5) commit 실패: redis/alerts 미호출 (자연 보장이지만 invariant 잠금).
    """

    def test_commit_then_redis_then_alerts_with_shape_and_timestamp(self):
        from app import crud
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        fixed_utc_ts = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
        manager = MagicMock()
        with patch.object(db, "commit", new=manager.commit), \
             patch.object(crud, "_write_changed_bank_rates_to_redis",
                          new=manager.write_redis), \
             patch.object(crud, "process_rate_alerts",
                          new=manager.process_alerts), \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = fixed_utc_ts
            crud.insert_bank_rates_into_db(
                db=db,
                current_rates={"usd-krw": 1371.5},
                bank_name="kb",
            )
        # 1) 순서 단언
        call_names = [c[0] for c in manager.mock_calls if c[0] in ("commit", "write_redis", "process_alerts")]
        self.assertEqual(call_names, ["commit", "write_redis", "process_alerts"])

        # 2) redis_updates shape + DB ts 일관 (Codex Medium-1)
        redis_payload = manager.write_redis.call_args.args[0]
        self.assertEqual(redis_payload, [{
            "source": "kb",
            "asset": "usd-krw",
            "rate": 1371.5,
            "timestamp": "2026-05-13T10:00:00+09:00",  # UTC 01:00 → KST 10:00
        }])

        # 3) changed_rates FCM shape 보존 — timestamp/source leak 없음 (Codex Medium-2)
        alerts_payload = manager.process_alerts.call_args.args[1]
        self.assertEqual(alerts_payload, [{
            "bank": "kb",
            "currency": "usd-krw",
            "rate": 1371.5,
        }])

    def test_unchanged_pair_skips_commit_redis_alerts(self):
        """last_record.rate == current_rate → INSERT/commit/redis/alerts 모두 skip (Codex Low-3)."""
        from app import crud
        db = MagicMock()
        last_record = MagicMock()
        last_record.rate = 1371.5
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = last_record

        with patch.object(crud, "_write_changed_bank_rates_to_redis") as mock_redis, \
             patch.object(crud, "process_rate_alerts") as mock_alerts:
            count = crud.insert_bank_rates_into_db(
                db=db,
                current_rates={"usd-krw": 1371.5},
                bank_name="kb",
            )
        self.assertEqual(count, 0)
        db.commit.assert_not_called()
        mock_redis.assert_not_called()
        mock_alerts.assert_not_called()

    def test_commit_failure_skips_redis_and_alerts(self):
        """db.commit() raise → 후속 Redis write/alerts 미호출 (Codex Low-4 invariant 잠금)."""
        from app import crud
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        db.commit.side_effect = RuntimeError("commit failed")

        with patch.object(crud, "_write_changed_bank_rates_to_redis") as mock_redis, \
             patch.object(crud, "process_rate_alerts") as mock_alerts, \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            with self.assertRaises(RuntimeError):
                crud.insert_bank_rates_into_db(
                    db=db,
                    current_rates={"usd-krw": 1371.5},
                    bank_name="kb",
                )
        mock_redis.assert_not_called()
        mock_alerts.assert_not_called()

    def test_redis_helper_internal_failure_does_not_block_alerts(self):
        """sync writer가 raise해도 _write_changed_*가 내부 catch → alerts 호출 보장.

        Step 3b 회귀 방지: Redis 실패가 FCM 알림 흐름을 차단하지 않아야 한다.
        """
        from app import crud
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch("app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
                   side_effect=ConnectionError("redis dead")), \
             patch.object(crud, "process_rate_alerts") as mock_alerts, \
             patch.object(crud, "models") as mock_models, \
             patch.object(crud.logger, "exception"):
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            crud.insert_bank_rates_into_db(
                db=db,
                current_rates={"usd-krw": 1371.5},
                bank_name="kb",
            )
        mock_alerts.assert_called_once()


class TestInsertInvestingRatesCallOrder(unittest.TestCase):
    """insert_investing_rates_into_db 호출 순서 + shape 잠금 (PR Z-2e Step 3b)."""

    def test_commit_then_redis_then_alerts_with_shape_and_timestamp(self):
        from app import crud
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        fixed_utc_ts = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
        manager = MagicMock()
        with patch.object(db, "commit", new=manager.commit), \
             patch.object(crud, "_write_changed_investing_rates_to_redis",
                          new=manager.write_redis), \
             patch.object(crud, "process_rate_alerts",
                          new=manager.process_alerts), \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = fixed_utc_ts
            crud.insert_investing_rates_into_db(
                db=db,
                current_rates={"usd-krw": 1371.5},
            )
        # 1) 순서 단언
        call_names = [c[0] for c in manager.mock_calls if c[0] in ("commit", "write_redis", "process_alerts")]
        self.assertEqual(call_names, ["commit", "write_redis", "process_alerts"])

        # 2) redis_updates shape + DB ts (Codex Medium-1)
        redis_payload = manager.write_redis.call_args.args[0]
        self.assertEqual(redis_payload, [{
            "source": "investing",
            "asset": "usd-krw",
            "rate": 1371.5,
            "timestamp": "2026-05-13T10:00:00+09:00",
        }])

        # 3) FCM shape 보존 (Codex Medium-2) — bank="investing" 고정
        alerts_payload = manager.process_alerts.call_args.args[1]
        self.assertEqual(alerts_payload, [{
            "bank": "investing",
            "currency": "usd-krw",
            "rate": 1371.5,
        }])

    def test_unchanged_pair_skips_commit_redis_alerts(self):
        """last_record.rate == current_rate → 모두 skip (Codex Low-3)."""
        from app import crud
        db = MagicMock()
        last_record = MagicMock()
        last_record.rate = 1371.5
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = last_record

        with patch.object(crud, "_write_changed_investing_rates_to_redis") as mock_redis, \
             patch.object(crud, "process_rate_alerts") as mock_alerts:
            count = crud.insert_investing_rates_into_db(
                db=db,
                current_rates={"usd-krw": 1371.5},
            )
        self.assertEqual(count, 0)
        db.commit.assert_not_called()
        mock_redis.assert_not_called()
        mock_alerts.assert_not_called()

    def test_commit_failure_skips_redis_and_alerts(self):
        """db.commit() raise → 후속 Redis write/alerts 미호출 (Codex Low-4 invariant 잠금)."""
        from app import crud
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        db.commit.side_effect = RuntimeError("commit failed")

        with patch.object(crud, "_write_changed_investing_rates_to_redis") as mock_redis, \
             patch.object(crud, "process_rate_alerts") as mock_alerts, \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            with self.assertRaises(RuntimeError):
                crud.insert_investing_rates_into_db(
                    db=db,
                    current_rates={"usd-krw": 1371.5},
                )
        mock_redis.assert_not_called()
        mock_alerts.assert_not_called()


# ---------------------------------------------------------------------------
# 5b-bis (§12.8.3 결정 #1) — USDT Redis freshness grain coalescing
# ---------------------------------------------------------------------------

from decimal import Decimal as _Decimal

from app.latest_rates_cache import (
    _KST as _kst,
    _floor_5s,
    _parse_kst,
    deserialize_usdt_value,
    serialize_usdt_value,
)


class TestUsdtSerializeDeserialize(unittest.TestCase):
    """serialize_usdt_value / deserialize_usdt_value round-trip + old schema migration."""

    def test_first_write_no_existing(self):
        """First write: rate_changed_at full precision, seen_at = floor_5s(tick)."""
        tick = datetime(2026, 5, 24, 20, 34, 47, 123_000, tzinfo=_kst)  # 47.123초
        seen_at = _floor_5s(tick)
        mirrored = datetime(2026, 5, 24, 20, 34, 47, 456_000, tzinfo=_kst)
        raw = serialize_usdt_value(_Decimal("1473.5"), tick, seen_at, mirrored)
        parsed = deserialize_usdt_value(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["rate"], _Decimal("1473.5"))
        # rate_changed_at = full precision (not floor)
        self.assertEqual(parsed["rate_changed_at"].microsecond, 123_000)
        # seen_at = 5s floor (microsecond 0, second 45)
        self.assertEqual(parsed["seen_at"].second, 45)
        self.assertEqual(parsed["seen_at"].microsecond, 0)
        # legacy timestamp = seen_at alias
        self.assertEqual(parsed["timestamp"], parsed["seen_at"])

    def test_first_write_at_exact_5s_boundary(self):
        """Tick이 정확히 5s boundary면 rate_changed_at == seen_at (우연)."""
        tick = datetime(2026, 5, 24, 20, 34, 45, 0, tzinfo=_kst)  # 정확히 45.0
        seen_at = _floor_5s(tick)
        mirrored = datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst)
        raw = serialize_usdt_value(_Decimal("1473.5"), tick, seen_at, mirrored)
        parsed = deserialize_usdt_value(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["rate_changed_at"], parsed["seen_at"])

    def test_old_schema_with_mirrored_at_migration(self):
        """기존 운영 schema {rate, timestamp, mirrored_at} → derive 정확.

        rate_changed_at = timestamp, seen_at = timestamp, mirrored_at 보존.
        """
        old_raw = (
            '{"rate": 1473.5, "timestamp": "2026-05-24T20:34:47+09:00", '
            '"mirrored_at": "2026-05-24T20:34:47.456000+09:00"}'
        )
        parsed = deserialize_usdt_value(old_raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["rate"], _Decimal("1473.5"))
        # rate_changed_at + seen_at derive from timestamp
        ts = _parse_kst("2026-05-24T20:34:47+09:00")
        self.assertEqual(parsed["rate_changed_at"], ts)
        self.assertEqual(parsed["seen_at"], ts)
        # mirrored_at preserved (운영 정상 migration 경로)
        self.assertEqual(
            parsed["mirrored_at"],
            _parse_kst("2026-05-24T20:34:47.456000+09:00"),
        )

    def test_old_schema_without_mirrored_at_very_old_legacy(self):
        """매우 옛 legacy: mirrored_at 자체 없음 → timestamp fallback (운영 경로 외)."""
        very_old_raw = '{"rate": 1473.5, "timestamp": "2026-05-24T20:34:47+09:00"}'
        parsed = deserialize_usdt_value(very_old_raw)
        self.assertIsNotNone(parsed)
        ts = _parse_kst("2026-05-24T20:34:47+09:00")
        self.assertEqual(parsed["mirrored_at"], ts)

    def test_deserialize_invalid_json_returns_none(self):
        self.assertIsNone(deserialize_usdt_value("not a json"))

    def test_deserialize_naive_datetime_returns_none(self):
        """timestamp가 naive면 None — deserialize_value 패턴 정합."""
        raw = '{"rate": 1473.5, "timestamp": "2026-05-24T20:34:47", "mirrored_at": "2026-05-24T20:34:47"}'
        self.assertIsNone(deserialize_usdt_value(raw))

    def test_deserialize_missing_required_field_returns_none(self):
        raw_missing_rate = '{"timestamp": "2026-05-24T20:34:47+09:00", "mirrored_at": "2026-05-24T20:34:47+09:00"}'
        self.assertIsNone(deserialize_usdt_value(raw_missing_rate))
        raw_missing_ts = '{"rate": 1473.5, "mirrored_at": "2026-05-24T20:34:47+09:00"}'
        self.assertIsNone(deserialize_usdt_value(raw_missing_ts))

    def test_empty_raw_returns_none(self):
        self.assertIsNone(deserialize_usdt_value(""))


class TestSetLatestUsdtRateCoalescing(unittest.TestCase):
    """set_latest_usdt_rate_from_sync_job — Redis SET coalescing (§12.8.3 결정 #1).

    5b-bis 옵션 A: in-memory state per (source, asset) — Redis GET 회피.
    setUp/tearDown에서 _last_written_usdt_state.clear() 격리 필수.
    """

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def _make_client_with_existing(self, existing_raw):
        """Mock client — get() returns existing_raw, set() tracks calls."""
        client = MagicMock()
        client.get.return_value = existing_raw.encode() if isinstance(existing_raw, str) else existing_raw
        return client

    def test_same_rate_same_bucket_skip(self):
        """Same rate + same 5s bucket → SKIPPED, success counter 미증가."""
        existing = serialize_usdt_value(
            _Decimal("1473.5"),
            datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst),  # rate_changed_at
            datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),            # seen_at floor
            datetime(2026, 5, 24, 20, 34, 45, 200_000, tzinfo=_kst),   # mirrored_at
        )
        client = self._make_client_with_existing(existing)
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success") as mock_success:
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:47.500000+09:00",  # 같은 bucket
            )
        self.assertIs(result, UsdtLatestWriteOutcome.SKIPPED)
        client.set.assert_not_called()  # SET skip
        mock_success.assert_not_called()  # success counter 미증가

    def test_same_rate_next_bucket_sets_with_preserved_rate_changed_at(self):
        """Same rate + 다음 bucket → SET, seen_at 갱신, rate_changed_at 유지."""
        original_rate_changed = datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst)
        existing = serialize_usdt_value(
            _Decimal("1473.5"),
            original_rate_changed,
            datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
            datetime(2026, 5, 24, 20, 34, 45, 200_000, tzinfo=_kst),
        )
        client = self._make_client_with_existing(existing)
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success") as mock_success:
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:50.123000+09:00",  # 다음 bucket
            )
        self.assertIs(result, UsdtLatestWriteOutcome.SET)
        client.set.assert_called_once()
        mock_success.assert_called_once()
        # SET value 확인 — rate_changed_at 유지, seen_at 새 bucket
        _, args, _ = client.set.mock_calls[0]
        new_value = args[1]
        parsed_new = deserialize_usdt_value(new_value)
        self.assertEqual(parsed_new["rate_changed_at"], original_rate_changed)  # 유지
        self.assertEqual(parsed_new["seen_at"].second, 50)  # 새 bucket

    def test_rate_change_updates_rate_changed_at(self):
        """Rate 변경 → rate_changed_at = 새 tick_ts (full precision)."""
        existing = serialize_usdt_value(
            _Decimal("1473.5"),
            datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst),
            datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
            datetime(2026, 5, 24, 20, 34, 45, 200_000, tzinfo=_kst),
        )
        client = self._make_client_with_existing(existing)
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success"):
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1474.0,  # 새 rate
                timestamp="2026-05-24T20:34:47.500000+09:00",  # 같은 bucket이지만 rate 변경
            )
        self.assertIs(result, UsdtLatestWriteOutcome.SET)
        client.set.assert_called_once()
        _, args, _ = client.set.mock_calls[0]
        parsed_new = deserialize_usdt_value(args[1])
        # rate_changed_at = full precision tick (47.500), 기존 (45.100) 아님
        self.assertEqual(parsed_new["rate_changed_at"].second, 47)
        self.assertEqual(parsed_new["rate_changed_at"].microsecond, 500_000)
        self.assertEqual(parsed_new["rate"], _Decimal("1474.0"))

    def test_decimal_comparison_no_float_drift(self):
        """Float 부동소수 drift 시나리오 — Decimal(str(rate)) 정확 비교."""
        # 0.1 + 0.2 = 0.30000000000000004 (float drift)
        existing = serialize_usdt_value(
            _Decimal("1473.3"),  # str 정확
            datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
            datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
            datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
        )
        client = self._make_client_with_existing(existing)
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success") as mock_success:
            # Caller가 float 1473.3 넘김 → Decimal(str(1473.3)) = Decimal("1473.3") 일치
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.3,
                timestamp="2026-05-24T20:34:47.500000+09:00",
            )
        # Same bucket + same rate → skip (float drift 없음)
        self.assertIs(result, UsdtLatestWriteOutcome.SKIPPED)
        client.set.assert_not_called()
        mock_success.assert_not_called()


class TestUsdtInMemoryStateCoalescing(unittest.TestCase):
    """5b-bis 옵션 A — in-memory state로 Redis GET 회피 (saturation 완화 핵심)."""

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_usdt_state.clear()

    def test_cold_start_triggers_redis_get(self):
        """state miss (cold-start) → client.get 호출됨."""
        client = MagicMock()
        client.get.return_value = None  # Redis miss
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success"):
            set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:47+09:00",
            )
        client.get.assert_called_once()

    def test_cold_start_redis_miss_sets_and_populates_state(self):
        """cold-start + Redis 없음 → 반드시 SET + state populate (Codex 잠금)."""
        client = MagicMock()
        client.get.return_value = None  # Redis miss
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success"):
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:47+09:00",
            )
        self.assertIs(result, UsdtLatestWriteOutcome.SET)
        client.set.assert_called_once()
        # State populated
        state = latest_rates_cache._last_written_usdt_state.get(("upbit", "usdt-krw"))
        self.assertIsNotNone(state)
        self.assertEqual(state["rate"], _Decimal("1473.5"))
        self.assertEqual(state["seen_at"].second, 45)  # 5s floor

    def test_cold_start_existing_same_bucket_skips_set_after_get(self):
        """cold-start GET 후 Redis hit same bucket → SET skip + state populate (Codex 잠금)."""
        # Redis 기존 값: same rate + same bucket
        existing = serialize_usdt_value(
            _Decimal("1473.5"),
            datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst),  # rate_changed_at
            datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),            # seen_at floor
            datetime(2026, 5, 24, 20, 34, 45, 200_000, tzinfo=_kst),
        )
        client = MagicMock()
        client.get.return_value = existing.encode()
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success") as mock_success:
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:47.500000+09:00",  # same bucket
            )
        self.assertIs(result, UsdtLatestWriteOutcome.SKIPPED)
        client.get.assert_called_once()  # cold-start GET 1회
        client.set.assert_not_called()    # same bucket → SET skip
        mock_success.assert_not_called()
        # State populated (Redis 기존 값 기반)
        state = latest_rates_cache._last_written_usdt_state.get(("upbit", "usdt-krw"))
        self.assertIsNotNone(state)
        self.assertEqual(state["rate"], _Decimal("1473.5"))

    def test_warm_state_skips_redis_get(self):
        """warm state → client.get 호출 안 됨 (옵션 A 핵심 효과)."""
        # State 미리 세팅 (warm)
        latest_rates_cache._last_written_usdt_state[("upbit", "usdt-krw")] = {
            "rate": _Decimal("1473.5"),
            "seen_at": datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
            "rate_changed_at": datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst),
        }
        client = MagicMock()
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success") as mock_success:
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:47.500000+09:00",  # same bucket
            )
        self.assertIs(result, UsdtLatestWriteOutcome.SKIPPED)
        client.get.assert_not_called()  # warm state → GET 회피 (핵심)
        client.set.assert_not_called()  # same bucket → SET skip
        mock_success.assert_not_called()

    def test_state_updated_only_after_set_success(self):
        """SET 성공 후 state 갱신 — 새 bucket으로 진입 시."""
        # Warm state: rate=1473.5, bucket=20:34:45
        latest_rates_cache._last_written_usdt_state[("upbit", "usdt-krw")] = {
            "rate": _Decimal("1473.5"),
            "seen_at": datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
            "rate_changed_at": datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst),
        }
        client = MagicMock()
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success"):
            set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:50.123000+09:00",  # 다음 bucket
            )
        client.set.assert_called_once()
        state = latest_rates_cache._last_written_usdt_state[("upbit", "usdt-krw")]
        self.assertEqual(state["seen_at"].second, 50)  # 새 bucket
        # rate_changed_at 유지 (rate 안 바뀜)
        self.assertEqual(state["rate_changed_at"].second, 45)

    def test_state_not_updated_when_set_fails(self):
        """SET exception → state 미갱신 (Redis-memory drift 방지)."""
        original_state = {
            "rate": _Decimal("1473.5"),
            "seen_at": datetime(2026, 5, 24, 20, 34, 45, tzinfo=_kst),
            "rate_changed_at": datetime(2026, 5, 24, 20, 34, 45, 100_000, tzinfo=_kst),
        }
        latest_rates_cache._last_written_usdt_state[("upbit", "usdt-krw")] = dict(original_state)
        client = MagicMock()
        client.set.side_effect = Exception("Redis down")
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_failure"):
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1474.0,  # rate 변경 → SET 시도
                timestamp="2026-05-24T20:34:50+09:00",
            )
        self.assertIs(result, UsdtLatestWriteOutcome.FAILED)
        # State 그대로 (drift 방지)
        state = latest_rates_cache._last_written_usdt_state[("upbit", "usdt-krw")]
        self.assertEqual(state["rate"], original_state["rate"])
        self.assertEqual(state["seen_at"], original_state["seen_at"])

    def test_state_per_source_asset_isolation(self):
        """서로 다른 (source, asset) state 독립."""
        client = MagicMock()
        client.get.return_value = None
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success"):
            set_latest_usdt_rate_from_sync_job(
                "upbit", "usdt-krw", 1473.5, "2026-05-24T20:34:47+09:00",
            )
            set_latest_usdt_rate_from_sync_job(
                "bithumb", "usdt-krw", 1474.0, "2026-05-24T20:34:47+09:00",
            )
        upbit_state = latest_rates_cache._last_written_usdt_state[("upbit", "usdt-krw")]
        bithumb_state = latest_rates_cache._last_written_usdt_state[("bithumb", "usdt-krw")]
        self.assertEqual(upbit_state["rate"], _Decimal("1473.5"))
        self.assertEqual(bithumb_state["rate"], _Decimal("1474.0"))

    def test_redis_get_failure_proceeds_to_set_and_updates_state(self):
        """GET 실패 → coalescing 없이 SET 시도 → 성공하면 state 갱신 (Codex 잠금)."""
        client = MagicMock()
        client.get.side_effect = Exception("GET timeout")  # GET 실패
        # SET은 성공 (default MagicMock 반환)
        latest_rates_cache._sync_client = client

        with patch("app.usdt_redis_stats.record_direct_write_success") as mock_success:
            result = set_latest_usdt_rate_from_sync_job(
                source="upbit", asset="usdt-krw",
                rate=1473.5,
                timestamp="2026-05-24T20:34:47+09:00",
            )
        self.assertIs(result, UsdtLatestWriteOutcome.SET)
        client.set.assert_called_once()
        mock_success.assert_called_once()
        # State 갱신 (GET 실패해도 SET 성공 → state 새로 populate)
        state = latest_rates_cache._last_written_usdt_state.get(("upbit", "usdt-krw"))
        self.assertIsNotNone(state)
        self.assertEqual(state["rate"], _Decimal("1473.5"))


class TestInsertRatesAlertAndRedisFalseIsolation(unittest.TestCase):
    """insert_bank/investing_rates_into_db — alert 예외 미전파 + Redis writer False-return 격리.

    PR A (Bank/Investing β characterization). 기존 TestInsertBankRatesCallOrder는
    redis EXCEPTION→alert 계속(test_redis_helper_internal_failure_does_not_block_alerts)을
    잠갔으나, (i) writer **False-return**(예외 아님) 경로와 (ii) process_rate_alerts
    **예외 미전파**는 미잠금이었다 — 본 클래스가 보강. β에서 Redis writer가 bool→outcome으로
    바뀌므로 False-return 경로를 명시 고정 (회귀 가드).
    """

    @staticmethod
    def _db_with_no_prior():
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        return db

    def test_bank_alert_exception_does_not_propagate(self):
        """process_rate_alerts raise → insert 밖으로 전파 X, 저장 count/commit 유지 (try/except 233)."""
        from app import crud
        db = self._db_with_no_prior()
        with patch.object(crud, "_write_changed_bank_rates_to_redis"), \
             patch.object(crud, "process_rate_alerts",
                          side_effect=RuntimeError("fcm dead")), \
             patch.object(crud, "models") as mock_models, \
             patch.object(crud.logger, "exception"):
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            count = crud.insert_bank_rates_into_db(
                db=db, current_rates={"usd-krw": 1371.5}, bank_name="kb",
            )
        self.assertEqual(count, 1)
        db.commit.assert_called_once()

    def test_investing_alert_exception_does_not_propagate(self):
        from app import crud
        db = self._db_with_no_prior()
        with patch.object(crud, "_write_changed_investing_rates_to_redis"), \
             patch.object(crud, "process_rate_alerts",
                          side_effect=RuntimeError("fcm dead")), \
             patch.object(crud, "models") as mock_models, \
             patch.object(crud.logger, "exception"):
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            count = crud.insert_investing_rates_into_db(
                db=db, current_rates={"usd-krw": 1371.5},
            )
        self.assertEqual(count, 1)
        db.commit.assert_called_once()

    def test_bank_redis_writer_false_return_does_not_block_alerts(self):
        """sync writer가 False 반환(예외 아님) → _write_changed가 무시 → alerts 계속."""
        from app import crud
        db = self._db_with_no_prior()
        with patch("app.latest_rates_cache.set_latest_bank_rate_from_sync_job",
                   return_value=False), \
             patch.object(crud, "process_rate_alerts") as mock_alerts, \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            count = crud.insert_bank_rates_into_db(
                db=db, current_rates={"usd-krw": 1371.5}, bank_name="kb",
            )
        self.assertEqual(count, 1)
        mock_alerts.assert_called_once()
        db.commit.assert_called_once()  # Redis False가 commit/count 무영향 (직접 잠금)

    def test_investing_redis_writer_false_return_does_not_block_alerts(self):
        from app import crud
        db = self._db_with_no_prior()
        with patch("app.latest_rates_cache.set_latest_investing_rate_from_sync_job",
                   return_value=False), \
             patch.object(crud, "process_rate_alerts") as mock_alerts, \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            count = crud.insert_investing_rates_into_db(
                db=db, current_rates={"usd-krw": 1371.5},
            )
        self.assertEqual(count, 1)
        mock_alerts.assert_called_once()
        db.commit.assert_called_once()  # Redis False가 commit/count 무영향 (직접 잠금)


    def test_bank_payload_conversion_failure_before_commit(self):
        """to_kst_isoformat(payload 변환) 실패 → commit 전 차단 → DB 미커밋 (구 staging-loop 변환과 동일 boundary, PR B)."""
        from app import crud
        db = self._db_with_no_prior()
        with patch.object(crud, "to_kst_isoformat", side_effect=RuntimeError("ts boom")), \
             patch.object(crud, "_write_changed_bank_rates_to_redis") as mock_redis, \
             patch.object(crud, "process_rate_alerts") as mock_alerts, \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            with self.assertRaises(RuntimeError):
                crud.insert_bank_rates_into_db(
                    db=db, current_rates={"usd-krw": 1371.5}, bank_name="kb",
                )
        db.commit.assert_not_called()
        mock_redis.assert_not_called()
        mock_alerts.assert_not_called()

    def test_investing_payload_conversion_failure_before_commit(self):
        from app import crud
        db = self._db_with_no_prior()
        with patch.object(crud, "to_kst_isoformat", side_effect=RuntimeError("ts boom")), \
             patch.object(crud, "_write_changed_investing_rates_to_redis") as mock_redis, \
             patch.object(crud, "process_rate_alerts") as mock_alerts, \
             patch.object(crud, "models") as mock_models:
            mock_models.get_utc_now.return_value = datetime(2026, 5, 13, 1, 0, tzinfo=dt_timezone.utc)
            with self.assertRaises(RuntimeError):
                crud.insert_investing_rates_into_db(
                    db=db, current_rates={"usd-krw": 1371.5},
                )
        db.commit.assert_not_called()
        mock_redis.assert_not_called()
        mock_alerts.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
