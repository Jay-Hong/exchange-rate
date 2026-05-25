"""KRX Stage E E-1 tests — KRX_REDIS_TICK_WRITE_ENABLED tick-level Redis writer.

KRX_FANOUT_REFACTOR_PLAN §5.2 E. USDT 5b-bis + 5d-a 패턴 mirror —
tick-level Redis write + freshness metadata 5-field schema + SET-only trigger.

검증 영역 (코덱스 분리 권고 — helper 단위 / writer 단위 분리):
    1. Helper unit (set_latest_krx_rate_from_sync_job_tick_level):
       - SET / SKIPPED / FAILED 정확 반환
       - 5s grain coalescing (same rate + same bucket → SKIPPED)
       - same rate + next bucket → SET + rate_changed_at 유지
       - rate change → SET + rate_changed_at 갱신
    2. Writer unit (KrxRedisLatestWriter.__call__):
       - SET 시점만 trigger
       - SKIPPED / FAILED 시 trigger 미호출
       - close grace window 안 tick → skip
       - flag=false 시 defensive return
    3. Integration (KrxDbWriter + flag):
       - flag=false: 기존 동작 (DB-bound Redis write)
       - flag=true: KrxDbWriter._sync_db_write가 Redis write/trigger skip
    4. Scheduler:
       - flag=false: KrxRedisLatestWriter handler 미등록
       - flag=true: handler 등록
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, latest_rates_cache
from app.crawlers.krx_kis import (
    KrxDbWriter,
    KrxRedisLatestWriter,
)
from app.latest_rates_cache import (
    KrxLatestWriteOutcome,
    serialize_usdt_value,
    set_latest_krx_rate_from_sync_job_tick_level,
)

KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# Helper unit tests — set_latest_krx_rate_from_sync_job_tick_level
# ---------------------------------------------------------------------------

class TestKrxTickLevelHelper(unittest.TestCase):
    """Helper 단위 — KrxLatestWriteOutcome 반환 + 5s coalescing 검증."""

    def setUp(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_krx_state.clear()

    def tearDown(self):
        latest_rates_cache._sync_client = None
        latest_rates_cache._last_written_krx_state.clear()

    def test_set_outcome_on_first_write(self):
        """첫 tick (state empty + Redis miss) → SET."""
        client = MagicMock()
        client.get.return_value = None
        latest_rates_cache._sync_client = client

        outcome = set_latest_krx_rate_from_sync_job_tick_level(
            asset="usd-krw-futures",
            rate=1500.5,
            timestamp="2026-05-26T15:00:00+09:00",
        )
        self.assertIs(outcome, KrxLatestWriteOutcome.SET)
        client.set.assert_called_once()

    def test_skipped_on_same_rate_same_5s_bucket(self):
        """same rate + same 5s bucket → SKIPPED (no SET)."""
        existing = serialize_usdt_value(
            Decimal("1500.5"),
            datetime(2026, 5, 26, 15, 0, 0, 100_000, tzinfo=KST),  # rate_changed_at
            datetime(2026, 5, 26, 15, 0, 0, tzinfo=KST),            # seen_at floor
            datetime(2026, 5, 26, 15, 0, 0, 200_000, tzinfo=KST),
        )
        client = MagicMock()
        client.get.return_value = existing.encode()
        latest_rates_cache._sync_client = client

        outcome = set_latest_krx_rate_from_sync_job_tick_level(
            asset="usd-krw-futures",
            rate=1500.5,
            timestamp="2026-05-26T15:00:02.500000+09:00",  # 같은 5s bucket
        )
        self.assertIs(outcome, KrxLatestWriteOutcome.SKIPPED)
        client.set.assert_not_called()

    def test_set_on_same_rate_next_5s_bucket_preserves_rate_changed_at(self):
        """same rate + next 5s bucket → SET, rate_changed_at 유지."""
        original_rate_changed = datetime(2026, 5, 26, 15, 0, 0, 100_000, tzinfo=KST)
        existing = serialize_usdt_value(
            Decimal("1500.5"),
            original_rate_changed,
            datetime(2026, 5, 26, 15, 0, 0, tzinfo=KST),
            datetime(2026, 5, 26, 15, 0, 0, 200_000, tzinfo=KST),
        )
        client = MagicMock()
        client.get.return_value = existing.encode()
        latest_rates_cache._sync_client = client

        outcome = set_latest_krx_rate_from_sync_job_tick_level(
            asset="usd-krw-futures",
            rate=1500.5,
            timestamp="2026-05-26T15:00:05.123000+09:00",  # 다음 bucket
        )
        self.assertIs(outcome, KrxLatestWriteOutcome.SET)
        client.set.assert_called_once()
        # rate_changed_at 유지 검증
        _, args, _ = client.set.mock_calls[0]
        parsed = latest_rates_cache.deserialize_usdt_value(args[1])
        self.assertEqual(parsed["rate_changed_at"], original_rate_changed)
        self.assertEqual(parsed["seen_at"].second, 5)  # 새 bucket

    def test_set_on_rate_change_updates_rate_changed_at(self):
        """rate change → SET + rate_changed_at = 새 tick_ts (full precision)."""
        existing = serialize_usdt_value(
            Decimal("1500.5"),
            datetime(2026, 5, 26, 15, 0, 0, 100_000, tzinfo=KST),
            datetime(2026, 5, 26, 15, 0, 0, tzinfo=KST),
            datetime(2026, 5, 26, 15, 0, 0, 200_000, tzinfo=KST),
        )
        client = MagicMock()
        client.get.return_value = existing.encode()
        latest_rates_cache._sync_client = client

        outcome = set_latest_krx_rate_from_sync_job_tick_level(
            asset="usd-krw-futures",
            rate=1501.2,  # 새 rate
            timestamp="2026-05-26T15:00:02.500000+09:00",
        )
        self.assertIs(outcome, KrxLatestWriteOutcome.SET)
        _, args, _ = client.set.mock_calls[0]
        parsed = latest_rates_cache.deserialize_usdt_value(args[1])
        # rate_changed_at = full precision tick (02.500)
        self.assertEqual(parsed["rate_changed_at"].second, 2)
        self.assertEqual(parsed["rate_changed_at"].microsecond, 500_000)
        self.assertEqual(parsed["rate"], Decimal("1501.2"))

    def test_failed_on_client_init_failure(self):
        """Redis client init 실패 → FAILED."""
        with patch.object(latest_rates_cache.redis_sync, "from_url",
                          side_effect=RuntimeError("init fail")):
            outcome = set_latest_krx_rate_from_sync_job_tick_level(
                asset="usd-krw-futures",
                rate=1500.5,
                timestamp="2026-05-26T15:00:00+09:00",
            )
        self.assertIs(outcome, KrxLatestWriteOutcome.FAILED)

    def test_failed_on_set_exception(self):
        """Redis SET 예외 → FAILED + warning."""
        client = MagicMock()
        client.get.return_value = None
        client.set.side_effect = ConnectionError("redis down")
        latest_rates_cache._sync_client = client

        with patch.object(latest_rates_cache.logger, "warning"):
            outcome = set_latest_krx_rate_from_sync_job_tick_level(
                asset="usd-krw-futures",
                rate=1500.5,
                timestamp="2026-05-26T15:00:00+09:00",
            )
        self.assertIs(outcome, KrxLatestWriteOutcome.FAILED)


# ---------------------------------------------------------------------------
# Writer unit tests — KrxRedisLatestWriter.__call__
# ---------------------------------------------------------------------------

class TestKrxRedisLatestWriterCall(unittest.IsolatedAsyncioTestCase):
    """Writer 단위 — SET-only trigger + close grace skip + flag defensive."""

    def _make_payload(self, rate="1500.5", session="CF",
                      received_at="2026-05-26T15:00:00") -> dict:
        return {
            "source": "krx",
            "asset": "usd-krw-futures",
            "price": rate,
            "session": session,
            "received_at": received_at,
        }

    async def test_set_outcome_triggers_topic(self):
        """outcome=SET → request_tether_topic_trigger 호출."""
        writer = KrxRedisLatestWriter()
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level",
                 return_value=KrxLatestWriteOutcome.SET,
             ), patch(
                 "app.tether_topic_trigger.request_tether_topic_trigger",
             ) as mock_trigger:
            await writer(self._make_payload(received_at="2026-05-26T10:00:00"))

        mock_trigger.assert_called_once()
        kwargs = mock_trigger.call_args.kwargs
        self.assertEqual(kwargs["source"], "krx")
        self.assertEqual(kwargs["asset"], "usd-krw-futures")
        self.assertEqual(kwargs["reason"], "krx_redis_write_success")

    async def test_skipped_outcome_no_trigger(self):
        """outcome=SKIPPED → trigger 미호출."""
        writer = KrxRedisLatestWriter()
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level",
                 return_value=KrxLatestWriteOutcome.SKIPPED,
             ), patch(
                 "app.tether_topic_trigger.request_tether_topic_trigger",
             ) as mock_trigger:
            await writer(self._make_payload(received_at="2026-05-26T10:00:00"))

        mock_trigger.assert_not_called()

    async def test_failed_outcome_no_trigger(self):
        """outcome=FAILED → trigger 미호출."""
        writer = KrxRedisLatestWriter()
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level",
                 return_value=KrxLatestWriteOutcome.FAILED,
             ), patch(
                 "app.tether_topic_trigger.request_tether_topic_trigger",
             ) as mock_trigger:
            await writer(self._make_payload(received_at="2026-05-26T10:00:00"))

        mock_trigger.assert_not_called()

    async def test_close_grace_window_skip(self):
        """close grace window 안 tick (CF 15:45:30) → tick writer skip + KrxCloseWindowWriter 단독 처리."""
        writer = KrxRedisLatestWriter()
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level",
             ) as mock_helper, patch(
                 "app.tether_topic_trigger.request_tether_topic_trigger",
             ) as mock_trigger:
            # CF close grace 15:45:00 ~ 15:46:00
            await writer(self._make_payload(
                session="CF", received_at="2026-05-26T15:45:30",
            ))

        # helper + trigger 둘 다 미호출
        mock_helper.assert_not_called()
        mock_trigger.assert_not_called()

    async def test_flag_false_defensive_early_return(self):
        """flag=false → defensive early return (scheduler 미등록이지만 안전망)."""
        writer = KrxRedisLatestWriter()
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level",
             ) as mock_helper:
            await writer(self._make_payload(received_at="2026-05-26T10:00:00"))

        mock_helper.assert_not_called()

    async def test_naive_received_at_normalized_to_kst_aware_for_helper(self):
        """Blocker 회귀 잠금 — 운영 payload의 KST naive ISO를 helper에 +09:00
        포함 aware ISO로 전달.

        KRX KisFuturesClient payload `received_at`은 KST naive ISO
        (datetime.now(KST).replace(tzinfo=None).isoformat()). helper의
        _parse_kst()는 USDT 5b-bis strict 정책상 naive 거부 — writer가
        normalize 안 하면 모든 tick이 FAILED. 본 테스트가 사고 회귀 차단.
        """
        writer = KrxRedisLatestWriter()
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level",
                 return_value=KrxLatestWriteOutcome.SET,
             ) as mock_helper, patch(
                 "app.tether_topic_trigger.request_tether_topic_trigger",
             ):
            # 운영 payload 형식: KST naive ISO (tzinfo 정보 없음)
            await writer(self._make_payload(received_at="2026-05-26T10:00:00"))

        # helper에 +09:00 포함된 KST-aware ISO 전달 검증
        mock_helper.assert_called_once()
        kwargs = mock_helper.call_args.kwargs
        self.assertIn("+09:00", kwargs["timestamp"])
        # 시각 자체는 보존 (naive를 KST로 attach만, 시각 변환 X)
        self.assertIn("2026-05-26T10:00:00", kwargs["timestamp"])

    async def test_aware_received_at_normalized_to_kst_for_helper(self):
        """Forward-compat 잠금 — 다른 timezone aware ISO도 KST로 변환해 helper 전달."""
        writer = KrxRedisLatestWriter()
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch(
                 "app.latest_rates_cache.set_latest_krx_rate_from_sync_job_tick_level",
                 return_value=KrxLatestWriteOutcome.SET,
             ) as mock_helper, patch(
                 "app.tether_topic_trigger.request_tether_topic_trigger",
             ):
            # UTC aware ISO (2026-05-26 01:00 UTC = 2026-05-26 10:00 KST)
            await writer(self._make_payload(received_at="2026-05-26T01:00:00+00:00"))

        mock_helper.assert_called_once()
        kwargs = mock_helper.call_args.kwargs
        # KST로 변환된 결과 — +09:00 포함 + 시각도 KST로 환산
        self.assertIn("+09:00", kwargs["timestamp"])
        self.assertIn("10:00:00", kwargs["timestamp"])


# ---------------------------------------------------------------------------
# Integration — KrxDbWriter._sync_db_write flag 분기
# ---------------------------------------------------------------------------

class TestKrxDbWriterFlagBranch(unittest.TestCase):
    """KrxDbWriter._sync_db_write가 flag true 시 Redis write/trigger skip."""

    def test_flag_false_db_bound_redis_write(self):
        """flag=false (default): inserted=True 시 KrxRedisLatestWriter.write_after_db_insert 호출 (기존 동작)."""
        tick = {
            "source": "krx",
            "asset": "usd-krw-futures",
            "price": "1500.5",
        }
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch("app.database.get_db_context") as mock_ctx, \
             patch(
                 "app.crawlers.krx_kis.KrxRedisLatestWriter.write_after_db_insert",
                 return_value=True,
             ) as mock_redis:
            mock_ctx.return_value.__enter__.return_value = MagicMock()
            result = KrxDbWriter._sync_db_write(tick)

        self.assertTrue(result)
        mock_redis.assert_called_once()

    def test_flag_true_skips_redis_write(self):
        """flag=true: inserted=True여도 KrxRedisLatestWriter.write_after_db_insert 미호출 + return False (caller trigger 차단)."""
        tick = {
            "source": "krx",
            "asset": "usd-krw-futures",
            "price": "1500.5",
        }
        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch("app.database.get_db_context") as mock_ctx, \
             patch(
                 "app.crawlers.krx_kis.KrxRedisLatestWriter.write_after_db_insert",
             ) as mock_redis:
            mock_ctx.return_value.__enter__.return_value = MagicMock()
            result = KrxDbWriter._sync_db_write(tick)

        # Redis write 미호출
        mock_redis.assert_not_called()
        # return False — caller(_flush_after_window)의 trigger 분기 차단
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Scheduler — flag true 시 KrxRedisLatestWriter handler 등록
# ---------------------------------------------------------------------------

class TestKrxRedisLatestWriterSchedulerRegistration(unittest.IsolatedAsyncioTestCase):
    """Scheduler bootstrap이 flag 분기 정확히 등록."""

    async def test_flag_false_no_redis_latest_writer_handler(self):
        """flag=false: KrxRedisLatestWriter handler 미등록."""
        from app import scheduler as scheduler_mod
        from app.crawlers.krx_kis import (
            KrxRedisLatestWriter, KrxDbWriter, KrxCloseWindowWriter,
        )

        added_handlers = []

        class _FakeClient:
            def add_tick_handler(self, h):
                added_handlers.append(h)

        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.dict("os.environ", {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}), \
             patch("app.crawlers.krx_kis.KisFuturesClient",
                   return_value=_FakeClient()), \
             patch("app.crawlers.krx_kis.KisApprovalManager"), \
             patch("app.crawlers.krx_kis.KisAccessTokenManager"), \
             patch("app.scheduler.resolve_active_krx_futures_contract",
                   new=AsyncMock(return_value=MagicMock(
                       short_code="A75605",
                       expiry_date=__import__("datetime").date(2026, 6, 15),
                   ))), \
             patch("asyncio.create_task",
                   side_effect=lambda coro: (coro.close(), MagicMock())[1]):
            await scheduler_mod._bootstrap_krx_futures_client()

        handler_types = [type(h).__name__ for h in added_handlers]
        self.assertIn("KrxDbWriter", handler_types)
        self.assertNotIn("KrxRedisLatestWriter", handler_types)

    async def test_flag_true_registers_redis_latest_writer_handler(self):
        """flag=true: KrxRedisLatestWriter handler 추가 등록."""
        from app import scheduler as scheduler_mod

        added_handlers = []

        class _FakeClient:
            def add_tick_handler(self, h):
                added_handlers.append(h)

        with patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.dict("os.environ", {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}), \
             patch("app.crawlers.krx_kis.KisFuturesClient",
                   return_value=_FakeClient()), \
             patch("app.crawlers.krx_kis.KisApprovalManager"), \
             patch("app.crawlers.krx_kis.KisAccessTokenManager"), \
             patch("app.scheduler.resolve_active_krx_futures_contract",
                   new=AsyncMock(return_value=MagicMock(
                       short_code="A75605",
                       expiry_date=__import__("datetime").date(2026, 6, 15),
                   ))), \
             patch("asyncio.create_task",
                   side_effect=lambda coro: (coro.close(), MagicMock())[1]):
            await scheduler_mod._bootstrap_krx_futures_client()

        handler_types = [type(h).__name__ for h in added_handlers]
        self.assertIn("KrxDbWriter", handler_types)
        self.assertIn("KrxRedisLatestWriter", handler_types)
        self.assertIn("KrxCloseWindowWriter", handler_types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
