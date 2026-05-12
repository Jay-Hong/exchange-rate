"""USDT direct Redis write Foundation 단위 테스트 (PR Z-2e B-Step 1).

검증 4 group:
    1. set_latest_source_rate (async helper) — key/serialize/SET 경로 정확
    2. set_latest_source_rate_from_sync_job (sync wrapper) — async helper 호출
    3. set_latest_source_rate_from_sync_job — running loop 안 RuntimeError
    4. _mirror_changed_source_to_redis — inserted=True 분기 호출, latest=None skip

운영 영향:
- 사용자-facing 영향 0
- hot path 소비자 변화 0
- 변경 INSERT 시 best-effort Redis SET 추가 (작은 운영 부하)
- 실패 시 mirror cycle(3초)이 safety repair
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import latest_rates_cache
from app.latest_rates_cache import (
    set_latest_source_rate,
    set_latest_source_rate_from_sync_job,
)


# ---------------------------------------------------------------------------
# Group 1 — async helper 경로 검증
# ---------------------------------------------------------------------------

class TestSetLatestSourceRateAsync(unittest.IsolatedAsyncioTestCase):

    async def test_calls_set_latest_with_correct_key_and_value(self):
        """latest_key_source / serialize_value / _set_latest 정확 호출."""
        with patch.object(latest_rates_cache, "_set_latest",
                          new=AsyncMock(return_value=True)) as mock_set:
            result = await set_latest_source_rate(
                source="upbit",
                asset="usdt-krw",
                rate=1485.5,
                timestamp="2026-05-12T15:00:00+09:00",
            )
        self.assertTrue(result)
        mock_set.assert_awaited_once()
        # key 검증
        called_key = mock_set.await_args.args[0]
        self.assertEqual(called_key, "latest:source:upbit:usdt-krw")
        # value JSON 검증 — rate / timestamp 포함, mirrored_at 존재
        called_value = mock_set.await_args.args[1]
        import json
        parsed = json.loads(called_value)
        self.assertEqual(parsed["rate"], 1485.5)
        self.assertEqual(parsed["timestamp"], "2026-05-12T15:00:00+09:00")
        self.assertIn("mirrored_at", parsed)

    async def test_returns_false_when_set_latest_fails(self):
        with patch.object(latest_rates_cache, "_set_latest",
                          new=AsyncMock(return_value=False)):
            result = await set_latest_source_rate(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Group 2 — sync wrapper가 async helper 호출
# ---------------------------------------------------------------------------

class TestSetLatestSourceRateFromSyncJob(unittest.TestCase):

    def test_calls_async_helper_and_returns_result_true(self):
        """sync wrapper가 async helper를 새 event loop에서 실행, 결과 반환."""
        async def fake_async(*args, **kwargs):
            return True
        with patch.object(latest_rates_cache, "set_latest_source_rate",
                          side_effect=fake_async) as mock_async:
            result = set_latest_source_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        self.assertTrue(result)
        mock_async.assert_called_once_with(
            "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
        )

    def test_returns_false_when_async_helper_returns_false(self):
        async def fake_async(*args, **kwargs):
            return False
        with patch.object(latest_rates_cache, "set_latest_source_rate",
                          side_effect=fake_async):
            result = set_latest_source_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Group 3 — running loop 안 호출 시 RuntimeError
# ---------------------------------------------------------------------------

class TestSyncWrapperRunningLoopGuard(unittest.IsolatedAsyncioTestCase):
    """IsolatedAsyncioTestCase는 test 안에서 running loop을 제공 — sync wrapper가
    이를 감지하고 RuntimeError raise하는지 검증.
    """

    async def test_raises_when_called_from_async_context(self):
        with self.assertRaises(RuntimeError) as cm:
            set_latest_source_rate_from_sync_job(
                "upbit", "usdt-krw", 1485.0, "2026-05-12T15:00:00+09:00"
            )
        # 메시지에 sync_job/active event loop 표현 포함 (운영 디버깅용 contract)
        self.assertIn("event loop", str(cm.exception))


# ---------------------------------------------------------------------------
# Group 4 — _mirror_changed_source_to_redis helper
# ---------------------------------------------------------------------------

class TestMirrorChangedSourceToRedis(unittest.TestCase):

    def test_calls_redis_helper_when_latest_found(self):
        """get_latest_source_rate=record → Redis helper 호출 + 결과 반환."""
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

    def test_skips_redis_when_latest_none(self):
        """get_latest_source_rate=None → Redis helper 호출 X, False 반환.

        logger.warning patch — 회귀 출력 stderr 노출 차단 + "latest 재조회 None
        시 warning 로그 1회"라는 contract 명시 검증.
        """
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
        """Redis helper에서 예외 → False 반환, 호출자에 전파 X (격리)."""
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
                   side_effect=RuntimeError("simulated redis fault")), \
             patch("app.crawlers.usdt_sources.logger.exception") as mock_log:
            result = usdt_sources._mirror_changed_source_to_redis(
                db=db, source="upbit", asset="usdt-krw"
            )
        self.assertFalse(result)
        self.assertEqual(mock_log.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
