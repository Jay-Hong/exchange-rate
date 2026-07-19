"""source_hourly_rates helper CRUD 단위 테스트 (ADR-035 D3 Step 1).

`app/source_hourly_rates.py` core CRUD (upsert/get/batch/delete/drift/row_to_dict)를
in-memory SQLite로 검증. 외부 DB 의존성 0. source_daily_rates helper 패턴 동형.

검증 포인트:
- upsert insert / idempotent update / 중복 row 없음
- invariant rate == close
- close_only high/low fallback
- null overwrite 방지 (metadata/contract_code 등 incoming None이면 기존값 보존)
- get_by_key / get_range(정렬) / batch_upsert / delete_range
- find_rate_close_drift (직접 삽입한 drift row catch)
- row_to_dict (Decimal→float + bucket_ts_kst isoformat)
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app import models
from app.models import SourceHourlyRate
import app.source_hourly_rates as H


def _bucket(hour: int) -> datetime:
    """2026-06-07 {hour}:00 KST bucket (naive)."""
    return datetime(2026, 6, 7, hour, 0, 0)


class TestSourceHourlyRatesHelper(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()

    def _upsert(self, hour=14, close=1500.0, **kw):
        H.upsert(
            self.db,
            source=kw.pop("source", "bithumb"),
            asset=kw.pop("asset", "usdt-krw"),
            bucket_ts_kst=kw.pop("bucket_ts_kst", _bucket(hour)),
            close=close,
            ohlc_quality=kw.pop("ohlc_quality", "source_ohlc"),
            close_basis=kw.pop("close_basis", "bithumb_24h_kst_close"),
            source_method=kw.pop("source_method", "bithumb_candlestick_api"),
            **kw,
        )

    # --- config / bucket helper ---

    def test_retention_has_1w_buffer(self):
        self.assertGreater(config.SOURCE_HOURLY_RETENTION_DAYS, 7)

    def test_floor_bucket_ts_kst_naive(self):
        ts = datetime(2026, 6, 7, 14, 59, 58, 123456)
        self.assertEqual(H.floor_bucket_ts_kst(ts), _bucket(14))

    def test_floor_bucket_ts_kst_aware_returns_naive_key(self):
        kst = timezone(timedelta(hours=9))
        ts = datetime(2026, 6, 7, 14, 30, 1, tzinfo=kst)
        bucket = H.floor_bucket_ts_kst(ts)
        self.assertEqual(bucket, _bucket(14))
        self.assertIsNone(bucket.tzinfo)

    def test_floor_bucket_ts_kst_converts_utc_to_kst(self):
        ts = datetime(2026, 6, 7, 5, 30, 1, tzinfo=timezone.utc)
        self.assertEqual(H.floor_bucket_ts_kst(ts), _bucket(14))

    # --- upsert / invariant ---

    def test_upsert_insert_and_get(self):
        self._upsert(hour=14, close=1500.0, high=1510.0, low=1490.0)
        row = H.get_by_key(self.db, "bithumb", "usdt-krw", _bucket(14))
        self.assertIsNotNone(row)
        self.assertEqual(float(row.close), 1500.0)
        self.assertEqual(float(row.rate), 1500.0)  # invariant rate==close
        self.assertEqual(float(row.high), 1510.0)

    def test_upsert_idempotent_update_no_duplicate(self):
        self._upsert(hour=14, close=1500.0)
        self._upsert(hour=14, close=1505.0)  # 같은 key, 다른 close
        rows = H.get_range(self.db, "bithumb", "usdt-krw", _bucket(14), _bucket(14))
        self.assertEqual(len(rows), 1)  # 중복 없음
        self.assertEqual(float(rows[0].close), 1505.0)  # update 반영
        self.assertEqual(float(rows[0].rate), 1505.0)  # invariant 유지

    def test_close_only_high_low_fallback(self):
        self._upsert(hour=15, close=1500.0, ohlc_quality="close_only", high=None, low=None)
        row = H.get_by_key(self.db, "bithumb", "usdt-krw", _bucket(15))
        self.assertEqual(float(row.high), 1500.0)
        self.assertEqual(float(row.low), 1500.0)

    def test_null_overwrite_prevention(self):
        # 1차: metadata + contract_code 채움
        self._upsert(hour=14, close=1500.0, metadata_json={"point_count": 7},
                     contract_code="A75606")
        # 2차: 같은 key, close 변경 + metadata/contract_code = None
        self._upsert(hour=14, close=1505.0, metadata_json=None, contract_code=None)
        row = H.get_by_key(self.db, "bithumb", "usdt-krw", _bucket(14))
        self.assertEqual(float(row.close), 1505.0)         # close는 update
        self.assertEqual(row.metadata_json, {"point_count": 7})  # 보존
        self.assertEqual(row.contract_code, "A75606")      # 보존

    # --- get_range / batch / delete ---

    def test_get_range_sorted(self):
        for h in (16, 14, 15):  # 비순서 삽입
            self._upsert(hour=h, close=1500.0 + h)
        rows = H.get_range(self.db, "bithumb", "usdt-krw", _bucket(14), _bucket(16))
        self.assertEqual([r.bucket_ts_kst for r in rows],
                         [_bucket(14), _bucket(15), _bucket(16)])  # ASC

    def test_get_range_exclusive_boundary(self):
        self._upsert(hour=13, close=1.0)
        self._upsert(hour=14, close=2.0)
        self._upsert(hour=17, close=3.0)
        rows = H.get_range(self.db, "bithumb", "usdt-krw", _bucket(14), _bucket(16))
        self.assertEqual([float(r.close) for r in rows], [2.0])  # 13·17 제외 (inclusive 14~16)

    def test_batch_upsert(self):
        rows = [
            {"source": "bithumb", "asset": "usdt-krw", "bucket_ts_kst": _bucket(h),
             "close": 1500.0 + h, "ohlc_quality": "source_ohlc",
             "close_basis": "bithumb_24h_kst_close", "source_method": "bithumb_candlestick_api"}
            for h in (14, 15, 16)
        ]
        n = H.batch_upsert(self.db, rows)
        self.assertEqual(n, 3)
        self.assertEqual(len(H.get_range(self.db, "bithumb", "usdt-krw", _bucket(14), _bucket(16))), 3)

    def test_delete_range(self):
        for h in (14, 15, 16, 17):
            self._upsert(hour=h, close=1500.0)
        deleted = H.delete_range(self.db, "bithumb", "usdt-krw", _bucket(15), _bucket(16))
        self.assertEqual(deleted, 2)
        remaining = H.get_range(self.db, "bithumb", "usdt-krw", _bucket(0), _bucket(23))
        self.assertEqual([r.bucket_ts_kst for r in remaining], [_bucket(14), _bucket(17)])

    # --- monitoring / row_to_dict ---

    def test_find_rate_close_drift(self):
        # 정상 row
        self._upsert(hour=14, close=1500.0)
        # drift row 직접 삽입 (upsert는 invariant 강제라 우회)
        self.db.add(SourceHourlyRate(
            source="krx", asset="usd-krw-futures", bucket_ts_kst=_bucket(15),
            rate=Decimal("1.0"), close=Decimal("2.0"), high=Decimal("2.0"), low=Decimal("1.0"),
            ohlc_quality="source_ohlc", close_basis="krx_cf_close_1545",
            source_method="krx_openapi_daily"))
        self.db.commit()
        drift = H.find_rate_close_drift(self.db)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0].source, "krx")

    def test_row_to_dict(self):
        self._upsert(hour=14, close=1500.0, high=1510.0, low=1490.0,
                     metadata_json={"point_count": 7})
        row = H.get_by_key(self.db, "bithumb", "usdt-krw", _bucket(14))
        d = H.row_to_dict(row)
        self.assertEqual(d["close"], 1500.0)
        self.assertIsInstance(d["close"], float)            # Decimal→float
        self.assertEqual(d["bucket_ts_kst"], _bucket(14).isoformat())
        self.assertEqual(d["rate"], 1500.0)
        self.assertEqual(d["metadata_json"], {"point_count": 7})
        self.assertIsNone(d["contract_code"])               # None 보존

    # --- get_last_before (v2 carry_in seed, ADR-039 §5.2) ---

    def test_get_last_before_hourly_strictly_before(self):
        # start_ts 직전/당시/이후 bucket → **직전(strict '<')** 1건만. start_ts 당시 bucket은 get_range가 이미 포함.
        self._upsert(hour=12, close=1490.0)   # before
        self._upsert(hour=14, close=1500.0)   # == start_ts
        self._upsert(hour=15, close=1505.0)   # after
        row = H.get_last_before(self.db, "bithumb", "usdt-krw", _bucket(14))
        self.assertIsNotNone(row)
        self.assertEqual(row.bucket_ts_kst, _bucket(12))   # start_ts(14)가 아니라 직전(12)
        self.assertEqual(float(row.close), 1490.0)

    def test_get_last_before_hourly_returns_most_recent_prior(self):
        # 여러 직전 중 가장 최근(내림차순 first).
        self._upsert(hour=10, close=1480.0)
        self._upsert(hour=13, close=1495.0)   # 가장 최근 직전
        row = H.get_last_before(self.db, "bithumb", "usdt-krw", _bucket(14))
        self.assertEqual(row.bucket_ts_kst, _bucket(13))

    def test_get_last_before_hourly_none_when_no_prior(self):
        self._upsert(hour=14, close=1500.0)   # start_ts 당시만
        self._upsert(hour=16, close=1505.0)   # 이후만
        self.assertIsNone(H.get_last_before(self.db, "bithumb", "usdt-krw", _bucket(14)))


if __name__ == "__main__":
    unittest.main()
