"""Investing per-currency hourly rollup validator/writer 단위 테스트 (ADR-035 D3 Step 2/3).

`scripts/backfill_investing_source_hourly_rates.py`의 resolve_currencies / rollup(UTC→KST bucket +
quantize) / validate_enum / fetch + write path(per-currency 적재/provenance/param mismatch)를
in-memory SQLite/synthetic으로 검증. 외부 의존성 0.

검증 포인트:
- resolve_currencies: usd|jpy|eur|all + invalid
- rollup: UTC naive → KST 1h bucket (9h 변환), close=마지막/high·low=max·min, asset 반영
- rollup quantize: Float artifact(0.1+0.2 류) → Numeric(14,6) 6자리 (Investing JPY per-100 대응)
- validate_enum: investing_observed_hourly allowlist (잘못된 close_basis catch)
- fetch: currency 필터
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import InvestingExchangeRate, SourceHourlyRate  # noqa: E402
import backfill_bithumb_source_hourly_rates as B  # noqa: E402
import backfill_investing_source_hourly_rates as I  # noqa: E402


def _utc(h, m=0, s=0):
    return datetime(2026, 6, 7, h, m, s)


def _kst_bucket(h):
    return datetime(2026, 6, 7, h, 0, 0)


class TestResolveCurrencies(unittest.TestCase):
    def test_all(self):
        self.assertEqual(I.resolve_currencies("all"), ["usd", "jpy", "eur"])

    def test_single(self):
        self.assertEqual(I.resolve_currencies("usd"), ["usd"])
        self.assertEqual(I.resolve_currencies("jpy"), ["jpy"])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            I.resolve_currencies("gbp")


class TestRollup(unittest.TestCase):
    def test_utc_to_kst_bucket_and_asset(self):
        rows = I.rollup_to_hourly([(1500.0, _utc(5, 30))], "usd-krw")  # UTC 05:30 → KST 14:00
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucket_ts_kst"], _kst_bucket(14))
        self.assertEqual(rows[0]["asset"], "usd-krw")
        self.assertEqual(rows[0]["close_basis"], "investing_observed_hourly")
        self.assertEqual(rows[0]["source_method"], "observed_rollup")

    def test_ohlc_close_last(self):
        rows = I.rollup_to_hourly([
            (1500.0, _utc(5, 0)), (1510.0, _utc(5, 30)), (1505.0, _utc(5, 59))], "usd-krw")
        r = rows[0]
        self.assertEqual(r["high"], Decimal("1510.000000"))
        self.assertEqual(r["low"], Decimal("1500.000000"))
        self.assertEqual(r["close"], Decimal("1505.000000"))   # 마지막
        self.assertEqual(r["rate"], r["close"])                # invariant
        self.assertEqual(r["metadata_json"]["point_count"], 3)

    def test_quantize_removes_float_artifact(self):
        # 0.1+0.2 = 0.30000000000000004 (Float artifact) → Decimal(str()).quantize(6자리) = 0.300000
        rows = I.rollup_to_hourly([(0.1 + 0.2, _utc(5, 30))], "jpy-krw")
        self.assertEqual(rows[0]["close"], Decimal("0.300000"))
        self.assertIsInstance(rows[0]["close"], Decimal)

    def test_empty(self):
        self.assertEqual(I.rollup_to_hourly([], "usd-krw"), [])


class TestValidateEnum(unittest.TestCase):
    def test_pass_on_valid(self):
        rows = I.rollup_to_hourly([(1500.0, _utc(5, 30))], "usd-krw")
        self.assertEqual(I.validate_enum(rows), [])

    def test_catch_wrong_close_basis(self):
        bad = [{"bucket_ts_kst": _kst_bucket(14), "close_basis": "investing_observed_eod",
                "source_method": "observed_rollup", "ohlc_quality": "observed_rollup"}]
        issues = I.validate_enum(bad)
        self.assertTrue(any("close_basis" in i for i in issues))


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def test_fetch_filters_currency_and_range(self):
        self.db.add_all([
            InvestingExchangeRate(currency="usd-krw", rate=1500.0, timestamp=_utc(5, 0)),
            InvestingExchangeRate(currency="usd-krw", rate=1510.0, timestamp=_utc(6, 0)),
            InvestingExchangeRate(currency="jpy-krw", rate=900.0, timestamp=_utc(5, 0)),   # 다른 통화
            InvestingExchangeRate(currency="usd-krw", rate=1400.0, timestamp=_utc(20, 0)),  # 범위 밖
        ])
        self.db.commit()
        obs = I.fetch_investing_observations(self.db, _utc(4, 0), _utc(7, 0), "usd-krw")
        self.assertEqual([o[0] for o in obs], [1500.0, 1510.0])  # usd-krw, 범위 안만

    def test_fetch_then_rollup_end_to_end(self):
        self.db.add_all([
            InvestingExchangeRate(currency="usd-krw", rate=1500.0, timestamp=_utc(5, 10)),
            InvestingExchangeRate(currency="usd-krw", rate=1510.0, timestamp=_utc(5, 50)),
        ])
        self.db.commit()
        obs = I.fetch_investing_observations(self.db, _utc(0, 0), _utc(23, 0), "usd-krw")
        rows = I.rollup_to_hourly(obs, "usd-krw")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucket_ts_kst"], _kst_bucket(14))
        self.assertEqual(rows[0]["close"], Decimal("1510.000000"))


class TestInvestingWritePath(unittest.TestCase):
    """B의 파라미터화된 write_with_transaction이 Investing 상수로 적재 + post-write 검증 통과."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_write_with_investing_constants(self):
        rows = I.rollup_to_hourly([(1500.0, _utc(5, 0)), (1510.0, _utc(6, 0))], "usd-krw")
        with patch("app.database.SessionLocal", self.Session):
            success, issues, outcome = B.write_with_transaction(
                rows, require_empty=True,
                source="investing", asset="usd-krw", close_basis="investing_observed_hourly",
                source_method="observed_rollup", ohlc_quality="observed_rollup")
        self.assertTrue(success, issues)
        self.assertEqual(outcome, {"inserted": 2, "updated": 0})
        db = self.Session()
        try:
            w = db.query(SourceHourlyRate).filter(SourceHourlyRate.source == "investing").all()
        finally:
            db.close()
        self.assertEqual(len(w), 2)
        self.assertEqual(w[0].close_basis, "investing_observed_hourly")
        self.assertEqual(w[0].asset, "usd-krw")

    def test_pre_upsert_catches_close_basis_mismatch(self):
        # close_basis param != rows literal → pre-upsert fail-close → rollback (적재 0)
        rows = I.rollup_to_hourly([(1500.0, _utc(5, 0))], "usd-krw")
        with patch("app.database.SessionLocal", self.Session):
            success, issues, _ = B.write_with_transaction(
                rows, require_empty=True,
                source="investing", asset="usd-krw", close_basis="bithumb_observed_hourly",  # 불일치
                source_method="observed_rollup", ohlc_quality="observed_rollup")
        self.assertFalse(success)
        self.assertTrue(any("literal != param" in i for i in issues))
        self.assertEqual(self.Session().query(SourceHourlyRate).count(), 0)

    def test_pre_upsert_catches_asset_mismatch_with_existing(self):
        # Codex 시나리오: rows asset != param asset + require_empty=False + 기존 param-target row 존재.
        # pre-upsert 검증 없으면 잘못된 asset row가 silent commit (post-write SELECT는 param 필터라 못 봄).
        db0 = self.Session()
        db0.add(SourceHourlyRate(
            source="investing", asset="usd-krw", bucket_ts_kst=_kst_bucket(14),
            rate=Decimal("1500.000000"), close=Decimal("1500.000000"),
            high=Decimal("1500.000000"), low=Decimal("1500.000000"),
            ohlc_quality="observed_rollup", close_basis="investing_observed_hourly",
            source_method="observed_rollup",
            metadata_json={"point_count": 1, "first_ts_kst": "x", "last_ts_kst": "x"}))
        db0.commit()
        db0.close()
        rows = I.rollup_to_hourly([(900.0, _utc(5, 0))], "jpy-krw")   # asset 불일치, bucket 동일(14:00)
        with patch("app.database.SessionLocal", self.Session):
            success, _, _ = B.write_with_transaction(
                rows, require_empty=False,
                source="investing", asset="usd-krw", close_basis="investing_observed_hourly",
                source_method="observed_rollup", ohlc_quality="observed_rollup")
        self.assertFalse(success)   # pre-upsert 불일치 catch
        db = self.Session()
        try:
            self.assertEqual(db.query(SourceHourlyRate).filter(SourceHourlyRate.asset == "jpy-krw").count(), 0)   # 미적재
            self.assertEqual(db.query(SourceHourlyRate).filter(SourceHourlyRate.asset == "usd-krw").count(), 1)   # 기존 불변
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
