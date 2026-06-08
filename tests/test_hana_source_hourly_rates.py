"""Hana per-currency hourly rollup dry-run validator 단위 테스트 (ADR-035 D3 Step 2).

`scripts/backfill_hana_source_hourly_rates.py`의 resolve_currencies / rollup(UTC→KST + ohlc_quality 분기) /
validate_enum / fetch / compute_carry_forward_stats를 in-memory SQLite/synthetic으로 검증. write 0. 외부 의존성 0.

검증 포인트:
- rollup ohlc_quality 분기: multi-tick → observed_rollup / single-tick → close_only(high=low=close)
- carry-forward stats: within-span 빈 hour(후보) + single-tick(close_only) 집계
- validate_enum: hana_observed_hourly + ohlc_quality 2값(observed_rollup/close_only) 허용
- fetch: bank="hana" + currency 필터
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import BankExchangeRate  # noqa: E402
import backfill_hana_source_hourly_rates as HV  # noqa: E402


def _utc(h, m=0, s=0):
    return datetime(2026, 6, 7, h, m, s)


def _kst_bucket(h):
    return datetime(2026, 6, 7, h, 0, 0)


class TestResolveCurrencies(unittest.TestCase):
    def test_all(self):
        self.assertEqual(HV.resolve_currencies("all"), ["usd", "jpy", "eur"])

    def test_single(self):
        self.assertEqual(HV.resolve_currencies("usd"), ["usd"])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            HV.resolve_currencies("gbp")


class TestRollup(unittest.TestCase):
    def test_ohlc_quality_multi(self):
        # UTC 05:00~05:59 = KST 14:00 bucket, 3 changes → observed_rollup
        rows = HV.rollup_to_hourly([
            (1380.0, _utc(5, 0)), (1382.0, _utc(5, 30)), (1381.0, _utc(5, 59))], "usd-krw")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["bucket_ts_kst"], _kst_bucket(14))
        self.assertEqual(r["ohlc_quality"], "observed_rollup")
        self.assertEqual(r["high"], Decimal("1382.0"))
        self.assertEqual(r["low"], Decimal("1380.0"))
        self.assertEqual(r["close"], Decimal("1381.0"))   # 마지막
        self.assertEqual(r["rate"], r["close"])            # invariant
        self.assertEqual(r["close_basis"], "hana_observed_hourly")
        self.assertEqual(r["metadata_json"]["point_count"], 3)

    def test_ohlc_quality_single(self):
        # 1 change → close_only (high=low=close)
        rows = HV.rollup_to_hourly([(1380.5, _utc(5, 30))], "usd-krw")
        r = rows[0]
        self.assertEqual(r["ohlc_quality"], "close_only")
        self.assertEqual(r["high"], r["low"])
        self.assertEqual(r["high"], r["close"])
        self.assertEqual(r["close"], Decimal("1380.5"))
        self.assertEqual(r["metadata_json"]["point_count"], 1)

    def test_empty(self):
        self.assertEqual(HV.rollup_to_hourly([], "usd-krw"), [])


class TestCarryForwardStats(unittest.TestCase):
    def test_within_span_gaps_and_single_tick(self):
        # 14:00 (single-tick) + 16:00 (multi) — 15:00은 within-span 빈 hour(carry-forward 후보)
        rows = HV.rollup_to_hourly([
            (1380.0, _utc(5, 30)),                          # KST 14:00 single
            (1382.0, _utc(7, 10)), (1383.0, _utc(7, 40)),   # KST 16:00 multi
        ], "usd-krw")
        self.assertEqual(len(rows), 2)
        cf = HV.compute_carry_forward_stats(rows)
        self.assertEqual(cf["buckets"], 2)
        self.assertEqual(cf["span_hours"], 3)              # 14:00, 15:00, 16:00
        self.assertEqual(cf["within_span_gaps"], 1)        # 15:00 (carry-forward 후보)
        self.assertEqual(cf["single_tick"], 1)             # 14:00

    def test_empty(self):
        self.assertEqual(HV.compute_carry_forward_stats([])["buckets"], 0)


class TestValidateEnum(unittest.TestCase):
    def test_pass_on_both_ohlc_quality(self):
        rows = HV.rollup_to_hourly([
            (1380.0, _utc(5, 0)), (1382.0, _utc(5, 30))], "usd-krw")  # observed_rollup
        rows += HV.rollup_to_hourly([(900.0, _utc(7, 30))], "jpy-krw")  # close_only
        self.assertEqual(HV.validate_enum(rows), [])

    def test_catch_wrong_close_basis(self):
        bad = [{"bucket_ts_kst": _kst_bucket(14), "close_basis": "hana_observed_eod",
                "source_method": "observed_rollup", "ohlc_quality": "observed_rollup"}]
        self.assertTrue(any("close_basis" in i for i in HV.validate_enum(bad)))

    def test_catch_wrong_ohlc_quality(self):
        bad = [{"bucket_ts_kst": _kst_bucket(14), "close_basis": "hana_observed_hourly",
                "source_method": "observed_rollup", "ohlc_quality": "source_ohlc"}]  # Hana 불가
        self.assertTrue(any("ohlc_quality" in i for i in HV.validate_enum(bad)))


class TestDecimalPrecision(unittest.TestCase):
    def test_catches_over_6_decimals(self):
        # Numeric(14,6) 초과(7자리) → dry-run에서 차단 (no-quantize 방어)
        bad = [{"bucket_ts_kst": _kst_bucket(14),
                "rate": Decimal("1380.1234567"), "high": Decimal("1380.1234567"),
                "low": Decimal("1380.1234567"), "close": Decimal("1380.1234567")}]
        self.assertTrue(any("소수부" in i for i in HV.validate_decimal_precision(bad)))

    def test_ok_within_6_decimals(self):
        rows = HV.rollup_to_hourly([(1380.5, _utc(5, 30))], "usd-krw")   # clean 고시값
        self.assertEqual(HV.validate_decimal_precision(rows), [])


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def test_fetch_filters_bank_and_currency(self):
        self.db.add_all([
            BankExchangeRate(bank="hana", currency="usd-krw", rate=1380.0, timestamp=_utc(5, 0)),
            BankExchangeRate(bank="hana", currency="usd-krw", rate=1381.0, timestamp=_utc(6, 0)),
            BankExchangeRate(bank="hana", currency="jpy-krw", rate=900.0, timestamp=_utc(5, 0)),   # 다른 통화
            BankExchangeRate(bank="kb", currency="usd-krw", rate=1382.0, timestamp=_utc(5, 0)),    # 다른 은행
            BankExchangeRate(bank="hana", currency="usd-krw", rate=1400.0, timestamp=_utc(20, 0)),  # 범위 밖
        ])
        self.db.commit()
        obs = HV.fetch_hana_observations(self.db, _utc(4, 0), _utc(7, 0), "usd-krw")
        self.assertEqual([o[0] for o in obs], [1380.0, 1381.0])  # hana usd-krw, 범위 안만

    def test_fetch_then_rollup_end_to_end(self):
        self.db.add_all([
            BankExchangeRate(bank="hana", currency="usd-krw", rate=1380.0, timestamp=_utc(5, 10)),
            BankExchangeRate(bank="hana", currency="usd-krw", rate=1382.0, timestamp=_utc(5, 50)),
        ])
        self.db.commit()
        obs = HV.fetch_hana_observations(self.db, _utc(0, 0), _utc(23, 0), "usd-krw")
        rows = HV.rollup_to_hourly(obs, "usd-krw")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucket_ts_kst"], _kst_bucket(14))
        self.assertEqual(rows[0]["close"], Decimal("1382.0"))
        self.assertEqual(rows[0]["ohlc_quality"], "observed_rollup")


if __name__ == "__main__":
    unittest.main()
