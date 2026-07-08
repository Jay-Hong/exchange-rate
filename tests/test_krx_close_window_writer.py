"""KRX close finalizer Stage 3 — KrxCloseWindowWriter + KrxDbWriter close-grace skip tests.

KRX_CLOSE_SNAPSHOT_PLAN §5.2 Stage 3 (2026-05-17). 14 tests:
    - F1 fix (3): KrxDbWriter skip CF/CM close grace + 일반 시간 정상
    - F2 fix (3): flag SET 조건 (db_ok AND redis_ok / db fail / redis fail)
    - 기본 동작 (8): last_tick / window-end / same-price / env off /
                     single-price / non-grace / 2-frame / drain
    - F3 fix: 모든 시간 fixture가 `datetime.now(KST) ± timedelta` 기반 (미래 sleep X)
    - 핵심 2개에 asyncio.sleep mock + assert_not_awaited (hang 재발 빠른 검출)

외부 의존성 0 — mock + in-memory level만 사용.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def _make_close_grace_dt(session: str, offset_sec: int = 0) -> datetime:
    """현재 시각 기반 close grace window 안 datetime (KST naive).

    F3 fix: 미래 boundary 절대 금지. now ± offset만 사용.
    """
    now_kst = datetime.now(KST).replace(tzinfo=None)
    if session == "CF":
        # 15:45:00 ~ 15:45:59 안에 있도록 시각만 교체 (date는 today)
        return now_kst.replace(hour=15, minute=45, second=max(0, min(59, 1 + offset_sec)), microsecond=0)
    if session == "CM":
        return now_kst.replace(hour=6, minute=0, second=max(0, min(59, 1 + offset_sec)), microsecond=0)
    raise ValueError(session)


def _make_single_price_dt(session: str) -> datetime:
    """현재 시각 기반 single-price window 안 datetime (KST naive)."""
    now_kst = datetime.now(KST).replace(tzinfo=None)
    if session == "CF":
        return now_kst.replace(hour=15, minute=40, second=0, microsecond=0)
    if session == "CM":
        return now_kst.replace(hour=5, minute=55, second=0, microsecond=0)
    raise ValueError(session)


def _make_non_grace_dt() -> datetime:
    now_kst = datetime.now(KST).replace(tzinfo=None)
    return now_kst.replace(hour=12, minute=30, second=0, microsecond=0)


def _make_payload(dt_kst_naive: datetime, session: str = "CF", price: str = "1490.6") -> dict:
    """make_normalized_payload 결과 shape."""
    return {
        "source": "krx",
        "asset": "usd-krw-futures",
        "session": session,
        "contract_code": "A75605",
        "contract_month": "202605",
        "expires_on": "2026-05-18",
        "price": price,
        "market_time": dt_kst_naive.strftime("%H%M%S"),
        "received_at": dt_kst_naive.isoformat(),
        "tr_id": "H0CFCNT0",
        "status": "normal",
    }


# ═════════════════════════════════════════════════════════════════════════════
# F1 fix — KrxDbWriter close-grace skip (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestKrxDbWriterClosesGraceSkip(unittest.IsolatedAsyncioTestCase):
    """F1: CF/CM close grace tick이 KrxDbWriter._sync_db_write를 호출 안 함.

    Plan §5.2: close grace 시간에는 KrxCloseWindowWriter가 전담, DB row 1 보장.
    """

    def setUp(self):
        from app.crawlers.krx_kis import reset_krx_close_finalizer_stats_for_tests
        reset_krx_close_finalizer_stats_for_tests()
        # F3 design fix (2026-05-17 Stage 3 보강): 2단 fix.
        # (1) compute_close_grace_end_kst → datetime.now(KST) patch.
        #     → __call__ 자동 예약 background flush task가 _flush_at_window_end에서
        #       sleep_sec <= 0 분기로 즉시 진행 (미래 sleep 차단).
        # (2) DB/Redis 외부 호출 default mock — _sync_write background 실행 시
        #     실제 DB connection 시도로 75초 timeout이 발생하던 문제 차단.
        #     명시 검증 필요한 test는 with patch(...)로 override 가능.
        # 핵심 2 test의 asyncio.sleep mock + assert_not_awaited는 추가 안전망으로 유지.
        self._grace_end_patcher = patch(
            "app.crawlers.krx_kis.compute_close_grace_end_kst",
            side_effect=lambda now, session: datetime.now(KST),
        )
        self._crud_patcher = patch(
            "app.crud.insert_source_rate_unconditional", return_value=True,
        )
        self._redis_patcher = patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True,
        )
        self._flag_patcher = patch(
            "app.latest_rates_cache.set_krx_close_captured_flag", return_value=True,
        )
        self._trigger_patcher = patch(
            "app.krx_topic_publisher.request_krx_topic_publish",
        )
        # Unit 4b/4c: CF daily-append default mock — gate true 테스트/미래 테스트의 실 DB
        # append(get_db_context) hang(75s) 방지 (gate default false라 평소 redundant·safety net)
        self._append_patcher = patch(
            "app.source_daily_rates.append_krx_cf_daily_row", return_value=("SKIP", "test"),
        )
        self._grace_end_patcher.start()
        self._crud_patcher.start()
        self._redis_patcher.start()
        self._flag_patcher.start()
        self._trigger_patcher.start()
        self._append_patcher.start()

    def tearDown(self):
        # LIFO order
        self._append_patcher.stop()
        self._trigger_patcher.stop()
        self._flag_patcher.stop()
        self._redis_patcher.stop()
        self._crud_patcher.stop()
        self._grace_end_patcher.stop()

    async def test_db_writer_skips_close_grace_window_cf(self):
        from app.crawlers.krx_kis import KrxDbWriter
        from app import config

        writer = KrxDbWriter(window_sec=0.01)  # 빠른 flush
        cf_close_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_close_dt, "CF", "1490.6")

        sync_db_write_mock = MagicMock(return_value=True)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(KrxDbWriter, "_sync_db_write", staticmethod(sync_db_write_mock)):
            await writer(payload)
            # window 만료 대기 (0.01s + 약간 여유)
            await asyncio.sleep(0.05)

        # `_sync_db_write` 호출 안 됨 — close grace skip 분기로 빠짐
        sync_db_write_mock.assert_not_called()

    async def test_db_writer_skips_close_grace_window_cm(self):
        from app.crawlers.krx_kis import KrxDbWriter
        from app import config

        writer = KrxDbWriter(window_sec=0.01)
        cm_close_dt = _make_close_grace_dt("CM")
        payload = _make_payload(cm_close_dt, "CM", "1490.6")

        sync_db_write_mock = MagicMock(return_value=True)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(KrxDbWriter, "_sync_db_write", staticmethod(sync_db_write_mock)):
            await writer(payload)
            await asyncio.sleep(0.05)

        sync_db_write_mock.assert_not_called()

    async def test_db_writer_processes_normal_time(self):
        """close grace 외 일반 시간 frame은 KrxDbWriter._sync_db_write 정상 호출 (회귀 안전망)."""
        from app.crawlers.krx_kis import KrxDbWriter
        from app import config

        writer = KrxDbWriter(window_sec=0.01)
        normal_dt = _make_non_grace_dt()  # 12:30 (정규장)
        payload = _make_payload(normal_dt, "CF", "1490.6")

        sync_db_write_mock = MagicMock(return_value=True)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(KrxDbWriter, "_sync_db_write", staticmethod(sync_db_write_mock)):
            await writer(payload)
            await asyncio.sleep(0.05)

        sync_db_write_mock.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
# F2 fix — captured flag SET 조건 (DB+Redis 모두 성공 시만, 3 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestCloseFinalizerFlagCondition(unittest.IsolatedAsyncioTestCase):
    """F2: captured flag SET은 db_ok AND redis_ok일 때만.

    Plan §5.3: DB 또는 Redis 한쪽 실패 시 REST fallback이 보정 가능해야 함.
    """

    def setUp(self):
        from app.crawlers.krx_kis import reset_krx_close_finalizer_stats_for_tests
        reset_krx_close_finalizer_stats_for_tests()
        # F3 design fix (2026-05-17 Stage 3 보강): 2단 fix.
        # (1) compute_close_grace_end_kst → datetime.now(KST) patch.
        #     → __call__ 자동 예약 background flush task가 _flush_at_window_end에서
        #       sleep_sec <= 0 분기로 즉시 진행 (미래 sleep 차단).
        # (2) DB/Redis 외부 호출 default mock — _sync_write background 실행 시
        #     실제 DB connection 시도로 75초 timeout이 발생하던 문제 차단.
        #     명시 검증 필요한 test는 with patch(...)로 override 가능.
        # 핵심 2 test의 asyncio.sleep mock + assert_not_awaited는 추가 안전망으로 유지.
        self._grace_end_patcher = patch(
            "app.crawlers.krx_kis.compute_close_grace_end_kst",
            side_effect=lambda now, session: datetime.now(KST),
        )
        self._crud_patcher = patch(
            "app.crud.insert_source_rate_unconditional", return_value=True,
        )
        self._redis_patcher = patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True,
        )
        self._flag_patcher = patch(
            "app.latest_rates_cache.set_krx_close_captured_flag", return_value=True,
        )
        self._trigger_patcher = patch(
            "app.krx_topic_publisher.request_krx_topic_publish",
        )
        # Unit 4b/4c: CF daily-append default mock — gate true 테스트/미래 테스트의 실 DB
        # append(get_db_context) hang(75s) 방지 (gate default false라 평소 redundant·safety net)
        self._append_patcher = patch(
            "app.source_daily_rates.append_krx_cf_daily_row", return_value=("SKIP", "test"),
        )
        self._grace_end_patcher.start()
        self._crud_patcher.start()
        self._redis_patcher.start()
        self._flag_patcher.start()
        self._trigger_patcher.start()
        self._append_patcher.start()

    def tearDown(self):
        # LIFO order
        self._append_patcher.stop()
        self._trigger_patcher.stop()
        self._flag_patcher.stop()
        self._redis_patcher.stop()
        self._crud_patcher.stop()
        self._grace_end_patcher.stop()

    async def test_flag_set_when_both_db_and_redis_ok(self):
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF", "1490.6")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("app.crud.insert_source_rate_unconditional", return_value=True) as db_mock, \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True) as redis_mock, \
             patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True) as flag_mock, \
             patch("app.krx_topic_publisher.request_krx_topic_publish"):
            await writer(payload)
            # _flush_at_window_end 직접 호출 (grace_end = 현재 → sleep skip)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        db_mock.assert_called_once()
        redis_mock.assert_called_once()
        flag_mock.assert_called_once_with("CF", cf_dt.date().isoformat())
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_grace_saved, 1)

    async def test_flag_not_set_when_db_fails(self):
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("app.crud.insert_source_rate_unconditional", side_effect=RuntimeError("db down")), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True), \
             patch("app.latest_rates_cache.set_krx_close_captured_flag") as flag_mock:
            await writer(payload)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        # DB 실패 → flag SET 안 됨 (REST fallback 보정 capacity 유지)
        flag_mock.assert_not_called()
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_grace_saved, 0)

    async def test_flag_not_set_when_redis_fails(self):
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("app.crud.insert_source_rate_unconditional", return_value=True), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=False), \
             patch("app.latest_rates_cache.set_krx_close_captured_flag") as flag_mock:
            await writer(payload)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        # Redis 실패 → flag SET 안 됨
        flag_mock.assert_not_called()
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_grace_saved, 0)


# ═════════════════════════════════════════════════════════════════════════════
# 기본 동작 (8 tests)
# ═════════════════════════════════════════════════════════════════════════════

class TestKrxCloseWindowWriterBasic(unittest.IsolatedAsyncioTestCase):
    """KrxCloseWindowWriter 기본 동작 — __call__ 분기 / window-end / drain / env / 등."""

    def setUp(self):
        from app.crawlers.krx_kis import reset_krx_close_finalizer_stats_for_tests
        reset_krx_close_finalizer_stats_for_tests()
        # F3 design fix (2026-05-17 Stage 3 보강): 2단 fix.
        # (1) compute_close_grace_end_kst → datetime.now(KST) patch.
        #     → __call__ 자동 예약 background flush task가 _flush_at_window_end에서
        #       sleep_sec <= 0 분기로 즉시 진행 (미래 sleep 차단).
        # (2) DB/Redis 외부 호출 default mock — _sync_write background 실행 시
        #     실제 DB connection 시도로 75초 timeout이 발생하던 문제 차단.
        #     명시 검증 필요한 test는 with patch(...)로 override 가능.
        # 핵심 2 test의 asyncio.sleep mock + assert_not_awaited는 추가 안전망으로 유지.
        self._grace_end_patcher = patch(
            "app.crawlers.krx_kis.compute_close_grace_end_kst",
            side_effect=lambda now, session: datetime.now(KST),
        )
        self._crud_patcher = patch(
            "app.crud.insert_source_rate_unconditional", return_value=True,
        )
        self._redis_patcher = patch(
            "app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True,
        )
        self._flag_patcher = patch(
            "app.latest_rates_cache.set_krx_close_captured_flag", return_value=True,
        )
        self._trigger_patcher = patch(
            "app.krx_topic_publisher.request_krx_topic_publish",
        )
        # Unit 4b/4c: CF daily-append default mock — gate true 테스트/미래 테스트의 실 DB
        # append(get_db_context) hang(75s) 방지 (gate default false라 평소 redundant·safety net)
        self._append_patcher = patch(
            "app.source_daily_rates.append_krx_cf_daily_row", return_value=("SKIP", "test"),
        )
        self._grace_end_patcher.start()
        self._crud_patcher.start()
        self._redis_patcher.start()
        self._flag_patcher.start()
        self._trigger_patcher.start()
        self._append_patcher.start()

    def tearDown(self):
        # LIFO order
        self._append_patcher.stop()
        self._trigger_patcher.stop()
        self._flag_patcher.stop()
        self._redis_patcher.stop()
        self._crud_patcher.stop()
        self._grace_end_patcher.stop()

    async def test_close_tick_in_grace_window_updates_last_tick(self):
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF", "1490.6")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True):
            await writer(payload)

        self.assertEqual(writer._last_tick["CF"], payload)
        self.assertEqual(writer._frame_count["CF"], 1)
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_grace_tick_count, 1)
        # flush_task 예약 확인 (close 시 cleanup)
        self.assertIn("CF", writer._flush_task)
        await writer.close(timeout=0.1)

    async def test_window_end_inserts_unconditional_with_event_timestamp(self):
        """`_flush_at_window_end` → DB unconditional INSERT with event_at_kst → UTC naive timestamp.

        F3 fix: grace_end = datetime.now(KST) → sleep skip.
        + asyncio.sleep mock으로 hang 재발 빠른 검출 (Codex 권장).
        """
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF", "1490.6")

        # sleep mock: hang 재발 시 즉시 검출
        sleep_mock = AsyncMock()
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("asyncio.sleep", sleep_mock), \
             patch("app.crud.insert_source_rate_unconditional", return_value=True) as db_mock, \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True), \
             patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True), \
             patch("app.krx_topic_publisher.request_krx_topic_publish"):
            await writer(payload)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        # asyncio.sleep 호출 안 됨 — grace_end가 현재라 sleep_sec <= 0 분기 (F3 안전망)
        sleep_mock.assert_not_awaited()
        # DB INSERT 호출 — kwargs로 timestamp 확인
        db_mock.assert_called_once()
        call_kwargs = db_mock.call_args.kwargs
        self.assertEqual(call_kwargs["source"], "krx")
        self.assertEqual(call_kwargs["asset"], "usd-krw-futures")
        self.assertEqual(call_kwargs["rate"], 1490.6)
        # timestamp는 UTC naive (event_at_kst.astimezone(utc).replace(tzinfo=None))
        ts = call_kwargs["timestamp"]
        self.assertIsNone(ts.tzinfo)

    async def test_same_price_close_inserts_new_row(self):
        """가격 동일 case도 unconditional INSERT 호출 (Stage 2 helper 검증)."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF", "1490.6")  # 직전 가격과 같다고 가정

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("app.crud.insert_source_rate_unconditional", return_value=True) as db_mock, \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True), \
             patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True), \
             patch("app.krx_topic_publisher.request_krx_topic_publish"):
            await writer(payload)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        # insert_source_rate_unconditional 호출 = 가격 비교 없이 INSERT (Stage 2 unconditional 검증)
        db_mock.assert_called_once()

    async def test_env_off_skips_call(self):
        """KRX_CLOSE_FINALIZER_ENABLED=false → __call__ early return."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False):
            await writer(payload)

        # State 변경 0
        self.assertNotIn("CF", writer._last_tick)
        self.assertNotIn("CF", writer._frame_count)
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_grace_tick_count, 0)

    async def test_single_price_window_counts_telemetry_only(self):
        """Single-price 진행 시간 frame → counter ++ but close path 미진입."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_single_price_dt("CF")  # 15:40
        payload = _make_payload(cf_dt, "CF")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True):
            await writer(payload)

        self.assertNotIn("CF", writer._last_tick)
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.single_price_window_frame_count, 1)
        self.assertEqual(stats.close_grace_tick_count, 0)

    async def test_non_grace_time_no_state_change(self):
        """정규장 한복판 (12:30) frame → 모든 state unchanged."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        normal_dt = _make_non_grace_dt()
        payload = _make_payload(normal_dt, "CF")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True):
            await writer(payload)

        self.assertNotIn("CF", writer._last_tick)
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_grace_tick_count, 0)
        self.assertEqual(stats.single_price_window_frame_count, 0)

    async def test_two_frames_triggers_duplicate_counter(self):
        """Window 안 2 frame → close_duplicate_count ++ + last candidate 1건만 저장.

        + asyncio.sleep mock (Codex 권장 — hang 재발 빠른 검출).
        """
        from app.crawlers.krx_kis import KrxCloseWindowWriter, get_krx_close_finalizer_stats
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt1 = _make_close_grace_dt("CF", offset_sec=0)
        cf_dt2 = _make_close_grace_dt("CF", offset_sec=4)  # 같은 분 안 다른 초
        payload1 = _make_payload(cf_dt1, "CF", "1490.6")
        payload2 = _make_payload(cf_dt2, "CF", "1490.8")  # 가격 변동

        sleep_mock = AsyncMock()
        sync_write_mock = MagicMock(return_value=True)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("asyncio.sleep", sleep_mock), \
             patch.object(KrxCloseWindowWriter, "_sync_write", staticmethod(sync_write_mock)):
            await writer(payload1)
            await writer(payload2)
            self.assertEqual(writer._frame_count["CF"], 2)
            self.assertEqual(writer._last_tick["CF"], payload2)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        sleep_mock.assert_not_awaited()
        # _sync_write 1회만 호출 (last candidate)
        sync_write_mock.assert_called_once()
        # 호출 args = payload2 (마지막)
        self.assertEqual(sync_write_mock.call_args.args[0], payload2)
        stats = get_krx_close_finalizer_stats()
        self.assertEqual(stats.close_grace_tick_count, 2)
        self.assertEqual(stats.close_duplicate_count, 1)

    async def test_close_drain_completes_pending_flush(self):
        """close() → pending task 대기 후 종료. timeout 시 cancel."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        from app import config

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF")
        payload = _make_payload(cf_dt, "CF")

        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(KrxCloseWindowWriter, "_sync_write", staticmethod(MagicMock(return_value=True))):
            await writer(payload)
            # flush task 예약된 상태에서 drain
            await writer.close(timeout=2.0)

        # task done 상태
        task = writer._flush_task.get("CF")
        if task is not None:
            self.assertTrue(task.done())

    async def test_market_time_used_when_present_overrides_received_at(self):
        """Plan §5.2: market_time(HHMMSS) 우선 + received_at fallback.

        market_time="154501", received_at=15:45:05 → 저장 timestamp는 15:45:01.
        """
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        from app import config
        from datetime import timezone

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF", offset_sec=4)  # received_at = 15:45:05
        payload = _make_payload(cf_dt, "CF", "1490.6")
        payload["market_time"] = "154501"  # KIS 체결시각 = 15:45:01

        db_mock = MagicMock(return_value=True)
        redis_mock = MagicMock(return_value=True)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("app.crud.insert_source_rate_unconditional", new=db_mock), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", new=redis_mock), \
             patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True), \
             patch("app.krx_topic_publisher.request_krx_topic_publish"):
            await writer(payload)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        # DB timestamp = 15:45:01 KST → UTC naive
        db_mock.assert_called_once()
        db_ts = db_mock.call_args.kwargs["timestamp"]
        expected_kst = cf_dt.replace(hour=15, minute=45, second=1, microsecond=0).replace(tzinfo=KST)
        expected_utc = expected_kst.astimezone(timezone.utc).replace(tzinfo=None)
        self.assertEqual(db_ts, expected_utc)

        # Redis timestamp = 15:45:01 KST ISO
        redis_mock.assert_called_once()
        redis_ts = redis_mock.call_args.kwargs["timestamp"]
        self.assertEqual(redis_ts, expected_kst.isoformat())

    async def test_market_time_fallback_when_empty(self):
        """market_time empty → received_at fallback (KST aware)."""
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        from app import config
        from datetime import timezone

        writer = KrxCloseWindowWriter()
        cf_dt = _make_close_grace_dt("CF", offset_sec=4)  # received_at = 15:45:05
        payload = _make_payload(cf_dt, "CF", "1490.6")
        payload["market_time"] = ""  # malformed/empty

        db_mock = MagicMock(return_value=True)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch("app.crud.insert_source_rate_unconditional", new=db_mock), \
             patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True), \
             patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True), \
             patch("app.krx_topic_publisher.request_krx_topic_publish"):
            await writer(payload)
            await writer._flush_at_window_end("CF", datetime.now(KST))

        db_mock.assert_called_once()
        db_ts = db_mock.call_args.kwargs["timestamp"]
        # received_at = cf_dt (KST naive) → KST aware → UTC naive
        expected_utc = cf_dt.replace(tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)
        self.assertEqual(db_ts, expected_utc)


class TestKrxCloseWindowWriterDailyAppend(unittest.IsolatedAsyncioTestCase):
    """Unit 4b — CF daily-append hook (_sync_write tail, ADR-034 §9).

    CF만 append / CM 미호출(gate) / append 예외 격리(flag 흐름 불변) /
    call order(flag·tether 뒤 tail) / contract_code 누락 격리.
    """

    def setUp(self):
        from app.crawlers.krx_kis import reset_krx_close_finalizer_stats_for_tests
        reset_krx_close_finalizer_stats_for_tests()
        # db_ok·redis_ok=True 성공 블록 진입 + emit/get_db_context mock (append session no-op)
        self._patchers = [
            patch("app.crawlers.krx_kis.compute_close_grace_end_kst",
                  side_effect=lambda now, session: datetime.now(KST)),
            patch("app.crud.insert_source_rate_unconditional", return_value=True),
            patch("app.latest_rates_cache.set_latest_krx_rate_from_sync_job", return_value=True),
            patch("app.latest_rates_cache.emit_krx_close_event"),
            patch("app.database.get_db_context", MagicMock()),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()

    async def _flush(self, session, payload, *, daily_append=True):
        from app.crawlers.krx_kis import KrxCloseWindowWriter
        from app import config
        writer = KrxCloseWindowWriter()
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(config, "KRX_DAILY_APPEND_ENABLED", daily_append):
            await writer(payload)
            await writer._flush_at_window_end(session, datetime.now(KST))

    async def test_cf_calls_append(self):
        dt = _make_close_grace_dt("CF")
        payload = _make_payload(dt, "CF", "1490.6")
        with patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True), \
             patch("app.krx_topic_publisher.request_krx_topic_publish"), \
             patch("app.source_daily_rates.append_krx_cf_daily_row",
                   return_value=("INSERT", "ok")) as append_mock:
            await self._flush("CF", payload)
        append_mock.assert_called_once()
        args = append_mock.call_args.args  # (db, date_kst, close, contract_code)
        self.assertEqual(args[1], dt.date())
        self.assertEqual(args[2], 1490.6)
        self.assertEqual(args[3], "A75605")

    async def test_cm_does_not_call_append(self):
        dt = _make_close_grace_dt("CM")
        payload = _make_payload(dt, "CM", "1490.6")
        with patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True), \
             patch("app.krx_topic_publisher.request_krx_topic_publish"), \
             patch("app.source_daily_rates.append_krx_cf_daily_row") as append_mock:
            await self._flush("CM", payload)
        append_mock.assert_not_called()

    async def test_append_exception_isolated_flag_still_set(self):
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats
        payload = _make_payload(_make_close_grace_dt("CF"), "CF")
        with patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True) as flag_mock, \
             patch("app.krx_topic_publisher.request_krx_topic_publish"), \
             patch("app.source_daily_rates.append_krx_cf_daily_row",
                   side_effect=RuntimeError("append boom")):
            await self._flush("CF", payload)  # 예외 raise X (격리)
        flag_mock.assert_called_once()  # 기존 흐름 불변 (append 실패해도 flag SET됨)
        self.assertEqual(get_krx_close_finalizer_stats().close_grace_saved, 1)

    async def test_append_called_after_flag_and_tether(self):
        payload = _make_payload(_make_close_grace_dt("CF"), "CF")
        manager = MagicMock()
        with patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True) as flag_mock, \
             patch("app.krx_topic_publisher.request_krx_topic_publish") as tether_mock, \
             patch("app.source_daily_rates.append_krx_cf_daily_row") as append_mock:
            manager.attach_mock(flag_mock, "flag")
            manager.attach_mock(tether_mock, "tether")
            manager.attach_mock(append_mock, "append")
            append_mock.return_value = ("INSERT", "ok")
            await self._flush("CF", payload)
        names = [c[0] for c in manager.mock_calls]
        # 최소 순서만 (brittle 회피): flag·tether < append (tail 위치 잠금)
        self.assertLess(names.index("flag"), names.index("append"))
        self.assertLess(names.index("tether"), names.index("append"))

    async def test_missing_contract_code_isolated(self):
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats
        payload = _make_payload(_make_close_grace_dt("CF"), "CF")
        payload["contract_code"] = None  # 누락
        with patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True) as flag_mock, \
             patch("app.krx_topic_publisher.request_krx_topic_publish"), \
             patch("app.source_daily_rates.append_krx_cf_daily_row",
                   side_effect=ValueError("contract_code 필수")) as append_mock:
            await self._flush("CF", payload)  # ValueError 격리
        append_mock.assert_called_once()
        self.assertIsNone(append_mock.call_args.args[3])  # contract_code=None 전달
        flag_mock.assert_called_once()  # 기존 흐름 불변
        self.assertEqual(get_krx_close_finalizer_stats().close_grace_saved, 1)

    async def test_gate_off_no_append_even_cf(self):
        # Unit 4c: KRX_DAILY_APPEND_ENABLED=False(default) → CF여도 append 미호출 (배포 ≠ 동작 변화)
        from app.crawlers.krx_kis import get_krx_close_finalizer_stats
        payload = _make_payload(_make_close_grace_dt("CF"), "CF")
        with patch("app.latest_rates_cache.set_krx_close_captured_flag", return_value=True) as flag_mock, \
             patch("app.krx_topic_publisher.request_krx_topic_publish"), \
             patch("app.source_daily_rates.append_krx_cf_daily_row") as append_mock:
            await self._flush("CF", payload, daily_append=False)
        append_mock.assert_not_called()
        flag_mock.assert_called_once()  # gate off = 기존 finalizer 흐름 그대로 (behavior-neutral)
        self.assertEqual(get_krx_close_finalizer_stats().close_grace_saved, 1)


if __name__ == "__main__":
    unittest.main()
