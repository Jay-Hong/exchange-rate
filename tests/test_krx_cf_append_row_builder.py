"""KRX CF append row builder (Unit 2) — build_krx_cf_append_row 단위 테스트 (pure).

DB 불필요. 핵심:
  1. observed_rollup 정상 (point_count>=1, close in [low,high]) — clamp 없음
  2. close_only (point_count==0) — high=low=close
  3. clamp above (close > rollup.high) — high=close + 방향/pre-clamp 진단
  4. clamp below (close < rollup.low) — low=close + 진단
  5. Decimal quantize(0.1) — Decimal(str(v)) 경유 (float artifact 회피)
  6. KST 변환 — UTC naive first/last_ts → KST isoformat
  7. invariant rate==close + low<=close<=high
  8. crash-early — close None / contract_code 빈값 → ValueError
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.source_daily_rates import (  # noqa: E402
    CfSessionRollup,
    build_krx_cf_append_row,
)

_KST = timezone(timedelta(hours=9))


def _rollup(*, high=None, low=None, point_count=0, first_ts=None, last_ts=None):
    return CfSessionRollup(high=high, low=low, point_count=point_count,
                           first_ts=first_ts, last_ts=last_ts)


def _utc_naive_from_kst(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=_KST).astimezone(timezone.utc).replace(tzinfo=None)


class TestBuildKrxCfAppendRow(unittest.TestCase):
    def test_observed_rollup_normal(self):
        r = _rollup(high=1510.0, low=1495.0, point_count=1200,
                    first_ts=_utc_naive_from_kst(2026, 6, 3, 9, 0),
                    last_ts=_utc_naive_from_kst(2026, 6, 3, 15, 30))
        row = build_krx_cf_append_row(date(2026, 6, 3), 1505.0, r, "A75606")
        self.assertEqual(row["ohlc_quality"], "observed_rollup")
        self.assertEqual(row["high"], Decimal("1510.0"))
        self.assertEqual(row["low"], Decimal("1495.0"))
        self.assertEqual(row["close"], Decimal("1505.0"))
        self.assertEqual(row["source_method"], "close_finalizer")
        self.assertEqual(row["close_basis"], "krx_cf_close_1545")
        self.assertEqual(row["contract_code"], "A75606")
        self.assertIsNone(row["basis_date"])
        self.assertIsNone(row["published_at"])
        self.assertNotIn("close_outside_rollup", row["metadata_json"])
        self.assertEqual(row["metadata_json"]["cf_session_point_count"], 1200)

    def test_close_only_when_empty(self):
        row = build_krx_cf_append_row(date(2026, 6, 3), 1500.0, _rollup(point_count=0), "A75606")
        self.assertEqual(row["ohlc_quality"], "close_only")
        self.assertEqual(row["high"], Decimal("1500.0"))
        self.assertEqual(row["low"], Decimal("1500.0"))
        self.assertEqual(row["close"], Decimal("1500.0"))
        self.assertEqual(row["metadata_json"]["cf_session_point_count"], 0)
        self.assertIsNone(row["metadata_json"]["cf_session_first_ts"])

    def test_clamp_above(self):
        # close(1515) > rollup.high(1510) → high=close, 진단 above
        r = _rollup(high=1510.0, low=1495.0, point_count=10)
        row = build_krx_cf_append_row(date(2026, 6, 3), 1515.0, r, "A75606")
        self.assertEqual(row["high"], Decimal("1515.0"))   # close까지 확장
        self.assertEqual(row["low"], Decimal("1495.0"))
        m = row["metadata_json"]
        self.assertTrue(m["close_outside_rollup"])
        self.assertTrue(m["close_above_rollup_high"])
        self.assertFalse(m["close_below_rollup_low"])
        self.assertEqual(m["rollup_high_before_clamp"], 1510.0)

    def test_clamp_below(self):
        # close(1490) < rollup.low(1495) → low=close, 진단 below
        r = _rollup(high=1510.0, low=1495.0, point_count=10)
        row = build_krx_cf_append_row(date(2026, 6, 3), 1490.0, r, "A75606")
        self.assertEqual(row["low"], Decimal("1490.0"))    # close까지 확장
        self.assertEqual(row["high"], Decimal("1510.0"))
        m = row["metadata_json"]
        self.assertTrue(m["close_outside_rollup"])
        self.assertTrue(m["close_below_rollup_low"])
        self.assertFalse(m["close_above_rollup_high"])
        self.assertEqual(m["rollup_low_before_clamp"], 1495.0)

    def test_decimal_quantize_str_path(self):
        # float 1500.1 → Decimal("1500.1") (str 경유 — float artifact 아님)
        r = _rollup(high=1500.1, low=1500.1, point_count=5)
        row = build_krx_cf_append_row(date(2026, 6, 3), 1500.1, r, "A75606")
        self.assertEqual(row["close"], Decimal("1500.1"))
        # float artifact였다면 Decimal("1500.1")과 != 였을 것
        self.assertEqual(str(row["close"]), "1500.1")
        # quantize: 1500.14 → 1500.1
        row2 = build_krx_cf_append_row(date(2026, 6, 3), 1500.14, _rollup(point_count=0), "A75606")
        self.assertEqual(row2["close"], Decimal("1500.1"))

    def test_kst_conversion_in_metadata(self):
        # UTC naive (KST 08:30 = UTC 23:30 전일) → metadata KST isoformat
        r = _rollup(high=1510.0, low=1495.0, point_count=3,
                    first_ts=_utc_naive_from_kst(2026, 6, 3, 8, 30),
                    last_ts=_utc_naive_from_kst(2026, 6, 3, 15, 45))
        row = build_krx_cf_append_row(date(2026, 6, 3), 1500.0, r, "A75606")
        self.assertEqual(row["metadata_json"]["cf_session_first_ts"], "2026-06-03T08:30:00+09:00")
        self.assertEqual(row["metadata_json"]["cf_session_last_ts"], "2026-06-03T15:45:00+09:00")

    def test_invariant_rate_eq_close_and_ohlc(self):
        for close, r in [
            (1505.0, _rollup(high=1510.0, low=1495.0, point_count=10)),
            (1500.0, _rollup(point_count=0)),
            (1515.0, _rollup(high=1510.0, low=1495.0, point_count=10)),  # clamp above
            (1490.0, _rollup(high=1510.0, low=1495.0, point_count=10)),  # clamp below
        ]:
            row = build_krx_cf_append_row(date(2026, 6, 3), close, r, "A75606")
            self.assertEqual(row["rate"], row["close"])                  # rate == close
            self.assertLessEqual(row["low"], row["close"])               # low <= close
            self.assertLessEqual(row["close"], row["high"])              # close <= high

    def test_crash_early_close_none(self):
        with self.assertRaises(ValueError):
            build_krx_cf_append_row(date(2026, 6, 3), None, _rollup(point_count=0), "A75606")

    def test_crash_early_empty_contract(self):
        with self.assertRaises(ValueError):
            build_krx_cf_append_row(date(2026, 6, 3), 1500.0, _rollup(point_count=0), "")

    def test_crash_early_rollup_missing_high_low(self):
        # point_count>0인데 high/low None → 명시 ValueError (cryptic InvalidOperation 대신)
        with self.assertRaises(ValueError):
            build_krx_cf_append_row(date(2026, 6, 3), 1500.0,
                                    _rollup(high=None, low=None, point_count=5), "A75606")

    def test_metadata_extra_merged(self):
        """2026-06-10 #4 — metadata_extra(REST-origin 마커)가 metadata_json에 병합."""
        row = build_krx_cf_append_row(
            date(2026, 6, 9), 1514.7,
            _rollup(high=1533.1, low=1509.3, point_count=6104), "A75606",
            metadata_extra={"origin": "rest_close_write"},
        )
        self.assertEqual(row["metadata_json"]["origin"], "rest_close_write")
        # 예약 키는 그대로 유지
        self.assertEqual(row["metadata_json"]["cf_session_point_count"], 6104)

    def test_metadata_extra_reserved_keys_win(self):
        """extra가 예약 키(cf_session_*)를 덮을 수 없음 — 예약 키가 나중에 쓰여 이김."""
        row = build_krx_cf_append_row(
            date(2026, 6, 9), 1514.7,
            _rollup(high=1533.1, low=1509.3, point_count=6104), "A75606",
            metadata_extra={"cf_session_point_count": -1, "origin": "rest_close_write"},
        )
        self.assertEqual(row["metadata_json"]["cf_session_point_count"], 6104)
        self.assertEqual(row["metadata_json"]["origin"], "rest_close_write")

    def test_metadata_extra_default_none_unchanged(self):
        """default None — 기존 WS 경로 호출자 metadata 키 구성 불변."""
        row = build_krx_cf_append_row(
            date(2026, 6, 9), 1514.7,
            _rollup(high=1533.1, low=1509.3, point_count=6104), "A75606",
        )
        self.assertNotIn("origin", row["metadata_json"])
        self.assertEqual(
            set(row["metadata_json"].keys()),
            {"cf_session_point_count", "cf_session_first_ts", "cf_session_last_ts"},
        )

if __name__ == "__main__":
    unittest.main()
