"""USDT Redis-first path telemetry 단위 테스트 (PR Z-2e B-Step Telemetry).

검증:
    - record_* 호출 시 counter increment + last_*_at 갱신
    - get_stats deep copy (mutation 격리)
    - reset_stats 후 counter 0 + last_* None + started_at 재설정
    - per-source lazy populate (record 시점에 source entry 등장)
    - aggregate db_fallback_by_asset 분리 (미래 asset 확장 검증)
    - admin endpoint는 firebase 의존성으로 직접 테스트 X (모듈 단위 test만)
"""
from __future__ import annotations

import unittest

from app import usdt_redis_stats


class TestUsdtRedisStatsRecord(unittest.TestCase):

    def setUp(self):
        usdt_redis_stats.reset_stats()

    def test_initial_state_empty_per_source(self):
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["per_source"], {})
        self.assertEqual(stats["aggregate"]["db_fallback_count"], 0)
        self.assertIsNone(stats["aggregate"]["last_db_fallback_at"])
        self.assertEqual(stats["aggregate"]["db_fallback_by_asset"], {})
        self.assertIsNotNone(stats["started_at"])

    def test_record_direct_write_success_creates_source_entry_and_increments(self):
        usdt_redis_stats.record_direct_write_success("upbit")
        stats = usdt_redis_stats.get_stats()
        upbit = stats["per_source"]["upbit"]
        self.assertEqual(upbit["direct_write_success"], 1)
        self.assertIsNotNone(upbit["last_direct_write_success_at"])
        # 다른 counter는 0
        self.assertEqual(upbit["direct_write_failure"], 0)
        self.assertEqual(upbit["redis_read_hit"], 0)

    def test_record_direct_write_failure_increments_no_timestamp(self):
        usdt_redis_stats.record_direct_write_failure("bithumb")
        stats = usdt_redis_stats.get_stats()
        bithumb = stats["per_source"]["bithumb"]
        self.assertEqual(bithumb["direct_write_failure"], 1)
        # failure에는 별도 last_*_at 없음 (success/hit 시점만 기록)
        self.assertIsNone(bithumb["last_direct_write_success_at"])

    def test_record_redis_read_hit_increments_and_timestamp(self):
        usdt_redis_stats.record_redis_read_hit("coinone")
        stats = usdt_redis_stats.get_stats()
        coinone = stats["per_source"]["coinone"]
        self.assertEqual(coinone["redis_read_hit"], 1)
        self.assertIsNotNone(coinone["last_redis_read_hit_at"])

    def test_record_redis_read_miss_parse_fail_error(self):
        usdt_redis_stats.record_redis_read_miss("korbit")
        usdt_redis_stats.record_redis_read_parse_fail("korbit")
        usdt_redis_stats.record_redis_read_error("korbit")
        stats = usdt_redis_stats.get_stats()
        korbit = stats["per_source"]["korbit"]
        self.assertEqual(korbit["redis_read_miss"], 1)
        self.assertEqual(korbit["redis_read_parse_fail"], 1)
        self.assertEqual(korbit["redis_read_error"], 1)
        # 실패 종류는 별도 timestamp 없음
        self.assertIsNone(korbit["last_redis_read_hit_at"])

    def test_multiple_increments_accumulate(self):
        for _ in range(5):
            usdt_redis_stats.record_direct_write_success("upbit")
        for _ in range(3):
            usdt_redis_stats.record_redis_read_hit("upbit")
        stats = usdt_redis_stats.get_stats()
        upbit = stats["per_source"]["upbit"]
        self.assertEqual(upbit["direct_write_success"], 5)
        self.assertEqual(upbit["redis_read_hit"], 3)

    def test_record_db_fallback_aggregate_and_by_asset(self):
        usdt_redis_stats.record_db_fallback("usdt-krw")
        usdt_redis_stats.record_db_fallback("usdt-krw")
        usdt_redis_stats.record_db_fallback("usdt-krw")
        stats = usdt_redis_stats.get_stats()
        agg = stats["aggregate"]
        self.assertEqual(agg["db_fallback_count"], 3)
        self.assertIsNotNone(agg["last_db_fallback_at"])
        self.assertEqual(agg["db_fallback_by_asset"]["usdt-krw"], 3)

    def test_db_fallback_by_asset_separates_future_assets(self):
        """미래 다른 topic-only asset 추가 시 자동 분리 (Codex 권고)."""
        usdt_redis_stats.record_db_fallback("usdt-krw")
        usdt_redis_stats.record_db_fallback("usdt-krw")
        usdt_redis_stats.record_db_fallback("usd-krw-futures")  # 가상 future asset
        stats = usdt_redis_stats.get_stats()
        by_asset = stats["aggregate"]["db_fallback_by_asset"]
        self.assertEqual(by_asset["usdt-krw"], 2)
        self.assertEqual(by_asset["usd-krw-futures"], 1)
        self.assertEqual(stats["aggregate"]["db_fallback_count"], 3)


class TestUsdtRedisStatsSnapshot(unittest.TestCase):

    def setUp(self):
        usdt_redis_stats.reset_stats()

    def test_get_stats_deep_copy(self):
        """반환 dict 수정해도 internal state 영향 X."""
        usdt_redis_stats.record_direct_write_success("upbit")
        snap = usdt_redis_stats.get_stats()
        snap["per_source"]["upbit"]["direct_write_success"] = 999999
        snap["aggregate"]["db_fallback_count"] = 999999
        snap["aggregate"]["db_fallback_by_asset"]["fake"] = 999999

        # 새 snapshot은 영향 받지 않음
        fresh = usdt_redis_stats.get_stats()
        self.assertEqual(fresh["per_source"]["upbit"]["direct_write_success"], 1)
        self.assertEqual(fresh["aggregate"]["db_fallback_count"], 0)
        self.assertNotIn("fake", fresh["aggregate"]["db_fallback_by_asset"])

    def test_snapshot_shape(self):
        """top-level key set + per-source shape contract."""
        usdt_redis_stats.record_direct_write_success("upbit")
        stats = usdt_redis_stats.get_stats()
        self.assertEqual(
            set(stats.keys()),
            {"started_at", "per_source", "aggregate"},
        )
        self.assertEqual(
            set(stats["per_source"]["upbit"].keys()),
            {
                "direct_write_success", "direct_write_failure",
                "redis_read_hit", "redis_read_miss",
                "redis_read_parse_fail", "redis_read_error",
                "last_direct_write_success_at", "last_redis_read_hit_at",
            },
        )
        self.assertEqual(
            set(stats["aggregate"].keys()),
            {"db_fallback_count", "last_db_fallback_at", "db_fallback_by_asset"},
        )


class TestUsdtRedisStatsReset(unittest.TestCase):

    def test_reset_clears_all_counters_and_timestamps(self):
        usdt_redis_stats.record_direct_write_success("upbit")
        usdt_redis_stats.record_redis_read_hit("bithumb")
        usdt_redis_stats.record_db_fallback("usdt-krw")
        # before
        stats_before = usdt_redis_stats.get_stats()
        self.assertTrue(stats_before["per_source"])
        self.assertEqual(stats_before["aggregate"]["db_fallback_count"], 1)

        # reset
        original_started_at = stats_before["started_at"]
        usdt_redis_stats.reset_stats()

        stats_after = usdt_redis_stats.get_stats()
        self.assertEqual(stats_after["per_source"], {})
        self.assertEqual(stats_after["aggregate"]["db_fallback_count"], 0)
        self.assertIsNone(stats_after["aggregate"]["last_db_fallback_at"])
        self.assertEqual(stats_after["aggregate"]["db_fallback_by_asset"], {})

    def test_reset_resets_started_at(self):
        """reset_stats 후 started_at도 새 시각으로 재설정 (Codex 권고 검증)."""
        import time
        first = usdt_redis_stats.get_stats()["started_at"]
        time.sleep(0.01)  # 시각 차이 보장
        usdt_redis_stats.reset_stats()
        second = usdt_redis_stats.get_stats()["started_at"]
        self.assertNotEqual(first, second)
        self.assertGreater(second, first)  # ISO 8601 string lex 비교 가능 (timezone 동일)


if __name__ == "__main__":
    unittest.main(verbosity=2)
