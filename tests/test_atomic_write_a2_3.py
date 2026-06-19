"""P1b A2-3 — usdt/krx Redis writer write-mode gate 테스트.

conftest.py가 firebase stub + DATABASE_URL=sqlite 처리. mock atomic_write_runtime.snapshot로
enforced_action별 writer 동작 검증:
- legacy → gate 통과(_get_sync_client 호출) / halt·atomic → BLOCKED + **Redis I/O 0**(_get_sync_client 미호출).
- set_latest_krx(597, close finalizer/REST/routine 공유)는 **C6-5b-2가 gate**(TestKrxDirectSetterGated) —
  halt/atomic block + legacy 진행. (A2-3 시점엔 close partial 방지로 미gate였고 그 lock을 C6-5b-2서 의도 flip.)
- usdt_sources polling이 BLOCKED를 success로 오인 안 함(Medium 1).
- BLOCKED counter(approximate) 증가.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import latest_rates_cache as lrc
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot

_TS = "2026-06-17T10:00:00+09:00"


def _snap(enforced: str) -> WriteModeSnapshot:
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced,
        activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced,
        mode_generation=0,
    )


class TestUsdtGate(unittest.TestCase):

    def test_legacy_passes_gate(self):
        # legacy → gate 통과 → _get_sync_client 호출(None→FAILED). gate가 안 막음(통과 증거).
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.latest_rates_cache._get_sync_client", return_value=None) as gsc:
            outcome = lrc.set_latest_usdt_rate_from_sync_job("upbit", "usdt-krw", 1300.0, _TS)
        gsc.assert_called_once()
        self.assertEqual(outcome, lrc.UsdtLatestWriteOutcome.FAILED)

    def test_halt_blocked_no_redis_io(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError("Redis I/O!")) as gsc:
            outcome = lrc.set_latest_usdt_rate_from_sync_job("upbit", "usdt-krw", 1300.0, _TS)
        self.assertEqual(outcome, lrc.UsdtLatestWriteOutcome.BLOCKED)
        gsc.assert_not_called()  # gate 최상단 — Redis I/O 0

    def test_atomic_blocked(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError):
            outcome = lrc.set_latest_usdt_rate_from_sync_job("upbit", "usdt-krw", 1300.0, _TS)
        self.assertEqual(outcome, lrc.UsdtLatestWriteOutcome.BLOCKED)  # A3 전 fail-closed (legacy 아님)


class TestKrxTickGate(unittest.TestCase):

    def test_legacy_passes_gate(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.latest_rates_cache._get_sync_client", return_value=None) as gsc:
            outcome = lrc.set_latest_krx_rate_from_sync_job_tick_level("usd-krw-futures", 1500.0, _TS)
        gsc.assert_called_once()
        self.assertEqual(outcome, lrc.KrxLatestWriteOutcome.FAILED)

    def test_halt_blocked_no_redis_io(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError) as gsc:
            outcome = lrc.set_latest_krx_rate_from_sync_job_tick_level("usd-krw-futures", 1500.0, _TS)
        self.assertEqual(outcome, lrc.KrxLatestWriteOutcome.BLOCKED)
        gsc.assert_not_called()

    def test_atomic_blocked(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError):
            outcome = lrc.set_latest_krx_rate_from_sync_job_tick_level("usd-krw-futures", 1500.0, _TS)
        self.assertEqual(outcome, lrc.KrxLatestWriteOutcome.BLOCKED)


class TestKrxDirectSetterGated(unittest.TestCase):
    """C6-5b-2 — set_latest_krx_rate_from_sync_job(597, close finalizer/REST/routine 공유)에 write-mode gate.

    ⚠️ A2-3 시점엔 close finalizer partial 방지로 **미gate**였고 이 클래스는 그 ungated 계약(TestKrxCloseWriter
    NotGated)을 잠갔었다. C6-5b-2가 halt/atomic을 gate(latest:source:krx freeze + §11 v2 invariant)하면서 그
    lock을 **의도적으로 flip**(codex Q4). legacy는 불변(진행) — close finalizer DB write는 gate 밖이라
    mode-independent. :597은 bool 계약이라 BLOCKED enum 대신 False. cascade(flag/daily-append skip)는
    test_krx_close_window_writer F2 lock(redis_ok=False)이 이미 잠금 — 여기선 gate 자체만 검증.
    """

    def test_legacy_proceeds(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.latest_rates_cache._get_sync_client", return_value=None) as gsc:
            result = lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS)
        gsc.assert_called_once()  # legacy → 진행 (client None → False)
        self.assertFalse(result)

    def test_halt_blocked_no_redis_io(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError("Redis I/O!")) as gsc:
            result = lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS)
        gsc.assert_not_called()  # gate 최상단 — Redis I/O 0
        self.assertFalse(result)  # bool 계약 (BLOCKED enum 없음)

    def test_atomic_blocked(self):
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError):
            result = lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS)
        self.assertFalse(result)  # A3 전 fail-closed (legacy fallback 금지)

    def test_block_counter_increments_neutral_label(self):
        # neutral label "krx:direct:{asset}" (tick-level "krx:{asset}"와 구분)
        lrc._write_mode_block_counts.clear()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError):
            lrc.set_latest_krx_rate_from_sync_job("usd-krw-futures", 1500.0, _TS)
        self.assertEqual(
            lrc._write_mode_block_counts.get(("krx:direct:usd-krw-futures", WriterMode.HALT)), 1
        )


class TestBlockedCounter(unittest.TestCase):

    def test_counter_increments_on_block(self):
        lrc._write_mode_block_counts.clear()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.latest_rates_cache._get_sync_client", side_effect=AssertionError):
            lrc.set_latest_usdt_rate_from_sync_job("upbit", "usdt-krw", 1300.0, _TS)
        self.assertEqual(
            lrc._write_mode_block_counts.get(("usdt:upbit:usdt-krw", WriterMode.HALT)), 1
        )


class TestKrxRoutineFlagFalseGate(unittest.TestCase):
    """Medium (xHigh review) — flag=false(KRX_REDIS_TICK_WRITE_ENABLED=false, Stage E rollback)
    routine Redis(_sync_db_write→write_after_db_insert→571)도 gate.

    DB insert는 진행(A2-3 = Redis-side only) / Redis SET은 halt·atomic 차단(benign partial,
    read-path DB-fallback). close/REST(571 직접, 2308/2880)는 write_after_db_insert 경유 안 하니 무영향.
    """

    _TICK = {"price": "1500.0", "source": "krx", "asset": "usd-krw-futures"}

    def test_flag_false_halt_skips_redis(self):
        from app.crawlers import krx_kis
        with patch("app.crawlers.krx_kis.config.KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.HALT)), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch.object(krx_kis.KrxRedisLatestWriter, "write_after_db_insert",
                          side_effect=AssertionError("routine Redis 호출됨")) as wadi:
            result = krx_kis.KrxDbWriter._sync_db_write(self._TICK)
        wadi.assert_not_called()  # halt → routine Redis 차단
        self.assertFalse(result)  # return False → _flush_after_window trigger 자연 미발화

    def test_flag_false_atomic_skips_redis(self):
        # gate는 != LEGACY라 atomic도 차단 (A3 전 fail-closed — legacy fallback 아님).
        from app.crawlers import krx_kis
        with patch("app.crawlers.krx_kis.config.KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch.object(krx_kis.KrxRedisLatestWriter, "write_after_db_insert",
                          side_effect=AssertionError("routine Redis 호출됨")) as wadi:
            result = krx_kis.KrxDbWriter._sync_db_write(self._TICK)
        wadi.assert_not_called()
        self.assertFalse(result)

    def test_flag_false_legacy_proceeds_to_redis(self):
        from app.crawlers import krx_kis
        with patch("app.crawlers.krx_kis.config.KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.LEGACY)), \
             patch("app.crud.insert_source_rate_if_changed", return_value=True), \
             patch.object(krx_kis.KrxRedisLatestWriter, "write_after_db_insert", return_value=True) as wadi:
            result = krx_kis.KrxDbWriter._sync_db_write(self._TICK)
        wadi.assert_called_once()  # legacy → routine Redis 진행(기존 흐름)
        self.assertTrue(result)


class TestUsdtSourcesPollingBlocked(unittest.TestCase):
    """Medium 1 — polling helper(_mirror_changed_source_to_redis)가 BLOCKED를 success로 오인 안 함."""

    def test_blocked_returns_false_not_success(self):
        from app.crawlers import usdt_sources
        latest = {"rate": 1300.0, "timestamp": _TS}
        with patch("app.crawlers.usdt_sources.crud.get_latest_source_rate", return_value=latest), \
             patch("app.crawlers.usdt_sources.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
                   return_value=lrc.UsdtLatestWriteOutcome.BLOCKED):
            result = usdt_sources._mirror_changed_source_to_redis(MagicMock(), "upbit", "usdt-krw")
        self.assertFalse(result)  # BLOCKED → not success (SKIPPED coalesce와 구분)


if __name__ == "__main__":
    unittest.main()
