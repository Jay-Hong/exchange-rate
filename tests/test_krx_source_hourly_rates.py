"""KRX hourly CF-only dry-run validator 단위 테스트 (ADR-035 D3 Step 2).

scripts/backfill_krx_source_hourly_rates.py의 CF-session filter rollup + contract resolver +
session/contract/enum validation을 synthetic으로 검증. write 0. 외부 의존성 0.

KST = UTC + 9 (source_rates.timestamp는 UTC naive). 예: KST 09:15 = UTC 00:15 / KST 08:30 = UTC 전날 23:30.

검증 포인트:
- rollup_cf_ticks_to_hourly: CF 세션(08:30~15:45 KST) tick만 → CM 야간 drop / OHLC / session=CF / contract resolve
- CF 경계 inclusive (08:30:00 / 15:45:00) + 15:46 drop
- validate_session: CM 누출(hour ∉ [8,15]) + 잘못된 label 검출
- validate_contract: contract None(daily 부재) 검출
- validate_enum: krx_observed_hourly 외(daily enum 등) 차단
- fetch_daily_contract_map: source_daily_rates source/asset/range 필터
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models  # noqa: E402
from app.models import SourceDailyRate  # noqa: E402
import backfill_krx_source_hourly_rates as K  # noqa: E402


class TestRollupCf(unittest.TestCase):
    def test_cf_filter_drops_cm_and_rolls_up(self):
        cmap = {date(2026, 6, 8): "A75606"}
        ticks = [
            (1500.0, datetime(2026, 6, 8, 0, 15)),   # KST 09:15 CF
            (1502.0, datetime(2026, 6, 8, 0, 30)),   # KST 09:30 CF (high)
            (1499.0, datetime(2026, 6, 8, 0, 45)),   # KST 09:45 CF (close, low)
            (1490.0, datetime(2026, 6, 8, 10, 0)),   # KST 19:00 CM 야간 → drop
        ]
        rows = K.rollup_cf_ticks_to_hourly(ticks, cmap)
        self.assertEqual(len(rows), 1)                            # CF 09:00 only (CM drop)
        r = rows[0]
        self.assertEqual(r["bucket_ts_kst"], datetime(2026, 6, 8, 9, 0))
        self.assertEqual(r["close"], 1499.0)                      # 마지막 CF tick
        self.assertEqual(r["rate"], r["close"])                   # invariant
        self.assertEqual(r["high"], 1502.0)                       # CM 1490 미반영
        self.assertEqual(r["low"], 1499.0)
        self.assertEqual(r["close_basis"], "krx_observed_hourly")
        self.assertEqual(r["source_method"], "observed_rollup")
        self.assertEqual(r["contract_code"], "A75606")
        self.assertEqual(r["metadata_json"]["session"], "CF")
        self.assertEqual(r["metadata_json"]["point_count"], 3)    # CF 3개 (CM 제외)

    def test_cf_boundary_inclusive(self):
        # KST 08:30:00(=UTC 전날 23:30) / 15:45:00(=UTC 06:45) 경계 inclusive, 15:46 drop
        cmap = {date(2026, 6, 8): "A75606"}
        ticks = [
            (1500.0, datetime(2026, 6, 7, 23, 30)),  # KST 08:30:00 → 08:00 bucket
            (1501.0, datetime(2026, 6, 8, 6, 45)),   # KST 15:45:00 → 15:00 bucket
            (1502.0, datetime(2026, 6, 8, 6, 46)),   # KST 15:46:00 → CF 밖 drop
        ]
        rows = K.rollup_cf_ticks_to_hourly(ticks, cmap)
        self.assertEqual(sorted(r["bucket_ts_kst"].hour for r in rows), [8, 15])  # 경계 inclusive

    def test_contract_missing_none(self):
        ticks = [(1500.0, datetime(2026, 6, 8, 0, 15))]           # KST 09:15 CF
        rows = K.rollup_cf_ticks_to_hourly(ticks, {})             # daily row 부재
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["contract_code"])               # 부재 → None

    def test_empty(self):
        self.assertEqual(K.rollup_cf_ticks_to_hourly([], {}), [])


class TestValidations(unittest.TestCase):
    def _row(self, hour=9, session="CF", contract="A75606", close_basis="krx_observed_hourly"):
        return {
            "source": "krx", "asset": "usd-krw-futures",
            "bucket_ts_kst": datetime(2026, 6, 8, hour, 0),
            "rate": 1500.0, "close": 1500.0, "high": 1500.0, "low": 1500.0,
            "ohlc_quality": "observed_rollup", "close_basis": close_basis,
            "source_method": "observed_rollup", "contract_code": contract,
            "metadata_json": {"point_count": 1, "first_ts_kst": "x", "last_ts_kst": "x", "session": session},
        }

    def test_session_catches_cm_leak(self):
        issues = K.validate_session([self._row(hour=19)])         # CM 야간 hour
        self.assertTrue(any("[8,15]" in i for i in issues))

    def test_session_catches_wrong_label(self):
        issues = K.validate_session([self._row(session="CM")])
        self.assertTrue(any("session != CF" in i for i in issues))

    def test_session_ok(self):
        self.assertEqual(K.validate_session([self._row()]), [])

    def test_contract_flags_missing(self):
        issues = K.validate_contract([self._row(contract=None)])
        self.assertTrue(any("contract_code 미resolve" in i for i in issues))

    def test_contract_dedup_dates(self):
        # 같은 date 여러 bucket이 None이어도 issue는 date당 1회
        rows = [self._row(hour=9, contract=None), self._row(hour=10, contract=None)]
        self.assertEqual(len(K.validate_contract(rows)), 1)       # 2026-06-08 1회

    def test_enum_catches_daily_close_basis(self):
        issues = K.validate_enum([self._row(close_basis="krx_cf_close_1545")])  # daily enum → hourly 불가
        self.assertTrue(any("close_basis 위반" in i for i in issues))

    def test_enum_ok(self):
        self.assertEqual(K.validate_enum([self._row()]), [])


class TestFetchDailyContractMap(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def test_lookup_filters_source_asset_range(self):
        self.db.add_all([
            SourceDailyRate(source="krx", asset="usd-krw-futures", date_kst=date(2026, 6, 8),
                            rate=1500, close=1500, ohlc_quality="source_ohlc",
                            close_basis="krx_cf_close_1545", source_method="krx_openapi_daily",
                            contract_code="A75606"),
            SourceDailyRate(source="krx", asset="usd-krw-futures", date_kst=date(2026, 5, 1),   # 범위 밖
                            rate=1490, close=1490, ohlc_quality="source_ohlc",
                            close_basis="krx_cf_close_1545", source_method="krx_openapi_daily",
                            contract_code="A75605"),
            SourceDailyRate(source="bithumb", asset="usdt-krw", date_kst=date(2026, 6, 8),       # 다른 source
                            rate=1400, close=1400, ohlc_quality="source_ohlc",
                            close_basis="bithumb_24h_kst_close", source_method="bithumb_candlestick_api"),
        ])
        self.db.commit()
        cmap = K.fetch_daily_contract_map(self.db, date(2026, 6, 7), date(2026, 6, 9))
        self.assertEqual(cmap, {date(2026, 6, 8): "A75606"})      # krx + 범위 안만


if __name__ == "__main__":
    unittest.main()
