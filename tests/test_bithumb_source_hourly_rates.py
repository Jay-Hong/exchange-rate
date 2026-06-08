"""Bithumb source_rates → source_hourly_rates dry-run validator 단위 테스트 (ADR-035 D3 Step 2).

`scripts/backfill_bithumb_source_hourly_rates.py`의 rollup + validation suite + gap 진단 +
fetch를 in-memory SQLite/synthetic tick으로 검증. 외부 DB/API 의존성 0. write 0 (dry-run).

검증 포인트:
- rollup: UTC naive tick → KST 1h bucket (9h 변환), close=마지막 tick, high/low=max/min, point_count
- validation: invariant / OHLC ordering / enum / bucket alignment 위반 catch
- gap 진단: requested window 기준 빈 hour (leading/trailing 포함, carry-forward 안 함)
- evaluate_dry_run: 0 bucket / validation issue fail-close
- fetch: source/asset/range 필터
- kst_date_range_to_utc 경계
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import SourceRate  # noqa: E402
import backfill_bithumb_source_hourly_rates as B  # noqa: E402


def _utc(h, m=0, s=0):
    """2026-06-07 {h}:{m}:{s} UTC naive (source_rates 저장 형식)."""
    return datetime(2026, 6, 7, h, m, s)


def _kst_bucket(h):
    """2026-06-07 {h}:00 KST naive bucket."""
    return datetime(2026, 6, 7, h, 0, 0)


class TestRollup(unittest.TestCase):
    def test_utc_tick_maps_to_kst_bucket(self):
        # UTC 05:30 → KST 14:30 → floor 14:00 (9h 변환 핵심)
        rows = B.rollup_ticks_to_hourly([(1475.0, _utc(5, 30))])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucket_ts_kst"], _kst_bucket(14))

    def test_multiple_ticks_same_hour_ohlc(self):
        # UTC 05:00/05:30/05:59 모두 KST 14:00 bucket
        rows = B.rollup_ticks_to_hourly([
            (1475.0, _utc(5, 0)), (1480.0, _utc(5, 30)), (1478.0, _utc(5, 59))])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["high"], 1480.0)
        self.assertEqual(r["low"], 1475.0)
        self.assertEqual(r["close"], 1478.0)      # 마지막 tick
        self.assertEqual(r["rate"], 1478.0)        # invariant rate==close
        self.assertEqual(r["metadata_json"]["point_count"], 3)

    def test_separate_hours_sorted(self):
        rows = B.rollup_ticks_to_hourly([
            (1480.0, _utc(6, 30)),   # KST 15:00
            (1475.0, _utc(5, 30))])  # KST 14:00 (비순서 입력)
        self.assertEqual([r["bucket_ts_kst"] for r in rows],
                         [_kst_bucket(14), _kst_bucket(15)])  # bucket ASC

    def test_provenance_fields(self):
        r = B.rollup_ticks_to_hourly([(1475.0, _utc(5, 30))])[0]
        self.assertEqual(r["close_basis"], "bithumb_observed_hourly")
        self.assertEqual(r["source_method"], "observed_rollup")
        self.assertEqual(r["ohlc_quality"], "observed_rollup")

    def test_empty(self):
        self.assertEqual(B.rollup_ticks_to_hourly([]), [])


class TestValidationSuite(unittest.TestCase):
    def _valid_rows(self):
        return B.rollup_ticks_to_hourly([
            (1475.0, _utc(5, 0)), (1480.0, _utc(5, 30)),
            (1478.0, _utc(6, 10))])

    def test_all_pass_on_valid(self):
        rows = self._valid_rows()
        for _, fn in B.VALIDATIONS:
            self.assertEqual(fn(rows), [], f"{fn.__name__} 가 valid rows에서 issue")

    def test_ohlc_ordering_catches(self):
        bad = [{"bucket_ts_kst": _kst_bucket(14), "rate": 1475.0, "close": 1475.0,
                "high": 1470.0, "low": 1480.0}]  # high<low, close 범위 밖
        self.assertTrue(B.validate_ohlc_ordering(bad))

    def test_enum_catches_wrong_close_basis(self):
        bad = [{"bucket_ts_kst": _kst_bucket(14), "close_basis": "bithumb_24h_kst_close",
                "source_method": "observed_rollup", "ohlc_quality": "observed_rollup"}]
        issues = B.validate_enum(bad)
        self.assertTrue(any("close_basis" in i for i in issues))

    def test_bucket_alignment_catches_non_floored(self):
        bad = [{"bucket_ts_kst": datetime(2026, 6, 7, 14, 30, 0)}]  # minute=30
        self.assertTrue(B.validate_bucket_alignment(bad))

    def test_bucket_alignment_catches_tzaware(self):
        bad = [{"bucket_ts_kst": datetime(2026, 6, 7, 14, 0, 0, tzinfo=timezone.utc)}]
        self.assertTrue(B.validate_bucket_alignment(bad))

    def test_invariant_catches(self):
        bad = [{"bucket_ts_kst": _kst_bucket(14), "rate": 1475.0, "close": 1480.0}]
        self.assertTrue(B.validate_invariant(bad))


class TestGapReport(unittest.TestCase):
    def test_gap_within_span(self):
        # KST 14, 15, 17 bucket (16 빠짐)
        rows = B.rollup_ticks_to_hourly([
            (1.0, _utc(5, 0)),   # 14:00
            (2.0, _utc(6, 0)),   # 15:00
            (3.0, _utc(8, 0))])  # 17:00
        gap = B.compute_gap_report(rows)
        self.assertEqual(gap["bucket_count"], 3)
        self.assertEqual(gap["span_hours"], 4)   # 14~17
        self.assertEqual(gap["gap_count"], 1)    # 16:00
        self.assertEqual(gap["gap_hours"], [_kst_bucket(16).isoformat()])

    def test_no_gap(self):
        rows = B.rollup_ticks_to_hourly([(1.0, _utc(5, 0)), (2.0, _utc(6, 0))])
        self.assertEqual(B.compute_gap_report(rows)["gap_count"], 0)

    def test_empty(self):
        self.assertEqual(B.compute_gap_report([])["bucket_count"], 0)

    def test_requested_window_leading_trailing(self):
        # bucket 14,15,16 (KST) / 요청 window 12:00~18:00 → leading 12,13 + trailing 17,18 gap
        rows = B.rollup_ticks_to_hourly([
            (1.0, _utc(5, 0)), (2.0, _utc(6, 0)), (3.0, _utc(7, 0))])
        gap = B.compute_gap_report(rows, _kst_bucket(12), _kst_bucket(18))
        self.assertEqual(gap["span_hours"], 7)   # 12~18
        self.assertEqual(gap["gap_count"], 4)
        self.assertEqual(gap["gap_hours"],
                         [_kst_bucket(h).isoformat() for h in (12, 13, 17, 18)])


class TestDryRunVerdict(unittest.TestCase):
    def test_fail_close_on_empty(self):
        ok, msg = B.evaluate_dry_run([], 0)
        self.assertFalse(ok)
        self.assertIn("0건", msg)

    def test_fail_on_validation_issues(self):
        ok, _ = B.evaluate_dry_run([{"bucket_ts_kst": _kst_bucket(14)}], 3)
        self.assertFalse(ok)

    def test_ok_on_valid(self):
        ok, _ = B.evaluate_dry_run([{"bucket_ts_kst": _kst_bucket(14)}], 0)
        self.assertTrue(ok)


class TestFetchAndRange(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def test_fetch_filters_source_asset_range(self):
        self.db.add_all([
            SourceRate(source="bithumb", asset="usdt-krw", rate=1475.0, timestamp=_utc(5, 0)),
            SourceRate(source="bithumb", asset="usdt-krw", rate=1480.0, timestamp=_utc(6, 0)),
            SourceRate(source="upbit", asset="usdt-krw", rate=1476.0, timestamp=_utc(5, 0)),   # 다른 source
            SourceRate(source="bithumb", asset="btc-krw", rate=999.0, timestamp=_utc(5, 0)),    # 다른 asset
            SourceRate(source="bithumb", asset="usdt-krw", rate=1400.0, timestamp=_utc(20, 0)),  # 범위 밖
        ])
        self.db.commit()
        ticks = B.fetch_bithumb_ticks(self.db, _utc(4, 0), _utc(7, 0))
        self.assertEqual([t[0] for t in ticks], [1475.0, 1480.0])  # bithumb/usdt-krw, 범위 안만

    def test_kst_date_range_to_utc(self):
        start_utc, end_utc = B.kst_date_range_to_utc(date(2026, 6, 7), date(2026, 6, 7))
        # KST 2026-06-07 00:00 = UTC 2026-06-06 15:00
        self.assertEqual(start_utc, datetime(2026, 6, 6, 15, 0, 0))
        # KST 2026-06-07 23:59:59.999999 = UTC 2026-06-07 14:59:59.999999
        self.assertEqual(end_utc.replace(microsecond=0), datetime(2026, 6, 7, 14, 59, 59))

    def test_fetch_then_rollup_end_to_end(self):
        self.db.add_all([
            SourceRate(source="bithumb", asset="usdt-krw", rate=1475.0, timestamp=_utc(5, 10)),
            SourceRate(source="bithumb", asset="usdt-krw", rate=1480.0, timestamp=_utc(5, 50)),
        ])
        self.db.commit()
        ticks = B.fetch_bithumb_ticks(self.db, _utc(0, 0), _utc(23, 0))
        rows = B.rollup_ticks_to_hourly(ticks)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucket_ts_kst"], _kst_bucket(14))
        self.assertEqual(rows[0]["close"], 1480.0)


if __name__ == "__main__":
    unittest.main()
