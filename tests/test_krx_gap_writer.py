"""Step 4B full-year backfill — write_krx_gap_rows (insert-only write-core) 단위 테스트.

in-memory SQLite. 핵심:
  1. 정상 insert + 기존 row 불변 (insert-only, update/delete 0)
  2. expected mismatch → abort
  3. expected <= 0 → abort
  4. unique conflict(이미 존재 date) → abort+rollback (skip/upsert 안 함)
  5. rate != close / source_method 위반 → post-verify abort
  6. window 밖 date → abort
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.models import SourceDailyRate  # noqa: E402
import backfill_krx_openapi_source_daily_rates as K  # noqa: E402

_WS = date(2025, 6, 1)
_WE = date(2026, 6, 3)


def _existing(d, close="1496.5", contract_code="A75606"):
    """기존(이미 적재된) krx_openapi_daily ORM row."""
    c = Decimal(close)
    return SourceDailyRate(
        source="krx", asset="usd-krw-futures", date_kst=d,
        rate=c, high=c, low=c, close=c,
        ohlc_quality="source_ohlc", close_basis="krx_cf_close_1545",
        source_method="krx_openapi_daily", contract_code=contract_code,
        basis_date=None, published_at=None,
        metadata_json={"contract_short_code": contract_code, "contract_month": "202606"},
    )


def _write_dict(d, close="1500.0", source_method="krx_openapi_daily", contract_code="A75506", **over):
    """rows_to_write 항목 (krx_row_to_source_daily 산출 형태)."""
    c = Decimal(close)
    row = {
        "source": "krx", "asset": "usd-krw-futures", "date_kst": d,
        "rate": c, "close": c, "high": c, "low": c,
        "ohlc_quality": "source_ohlc", "close_basis": "krx_cf_close_1545",
        "source_method": source_method, "contract_code": contract_code,
        "basis_date": None, "published_at": None,
        "metadata_json": {"contract_short_code": contract_code, "contract_month": "202506"},
    }
    row.update(over)
    return row


class TestWriteKrxGapRows(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _count(self, db):
        return db.query(func.count()).select_from(SourceDailyRate).scalar()

    def test_normal_insert_existing_unchanged(self):
        # 기존 2 + 신규 2 insert → total 4, 기존 값 불변
        with self.Session() as db:
            db.add(_existing(date(2026, 4, 20), close="1496.5"))
            db.add(_existing(date(2026, 4, 21), close="1497.0"))
            db.commit()
            orig = {r.date_kst: (r.close, r.source_method, r.id) for r in db.query(SourceDailyRate).all()}
        with self.Session() as db:
            rows = [_write_dict(date(2025, 6, 4)), _write_dict(date(2025, 6, 5), close="1501.0")]
            status, detail = K.write_krx_gap_rows(db, rows, expected_rows=2,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "WRITE_OK", detail)
            db.commit()
        with self.Session() as db:
            self.assertEqual(self._count(db), 4)
            cur = {r.date_kst: (r.close, r.source_method, r.id) for r in db.query(SourceDailyRate).all()}
            # 기존 2 불변 (id/값)
            for d in (date(2026, 4, 20), date(2026, 4, 21)):
                self.assertEqual(cur[d], orig[d])
            # 신규 2 = krx_openapi_daily
            self.assertEqual(cur[date(2025, 6, 4)][1], "krx_openapi_daily")

    def test_expected_mismatch_aborts(self):
        with self.Session() as db:
            rows = [_write_dict(date(2025, 6, 4)), _write_dict(date(2025, 6, 5))]
            status, detail = K.write_krx_gap_rows(db, rows, expected_rows=5,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "ABORT")
            self.assertIn("expected_rows", detail)

    def test_expected_zero_aborts(self):
        with self.Session() as db:
            status, detail = K.write_krx_gap_rows(db, [], expected_rows=0,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "ABORT")
            self.assertIn("대상 row 필수", detail)

    def test_unique_conflict_aborts(self):
        # rows_to_write에 이미 존재하는 date 포함 → IntegrityError → ABORT, 기존 불변
        with self.Session() as db:
            db.add(_existing(date(2026, 4, 20)))
            db.commit()
        with self.Session() as db:
            rows = [_write_dict(date(2026, 4, 20))]  # 이미 존재
            status, detail = K.write_krx_gap_rows(db, rows, expected_rows=1,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "ABORT")
            self.assertIn("conflict", detail)
            db.rollback()
        with self.Session() as db:  # 기존 1만 (insert 안 됨)
            self.assertEqual(self._count(db), 1)

    def test_rate_ne_close_aborts(self):
        # post-verify가 rate != close 잡음 (DB엔 잠시 들어가나 caller rollback)
        with self.Session() as db:
            rows = [_write_dict(date(2025, 6, 4), close="1500.0", rate=Decimal("1499.0"))]
            status, detail = K.write_krx_gap_rows(db, rows, expected_rows=1,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "ABORT")
            self.assertIn("rate != close", detail)
            db.rollback()
        with self.Session() as db:
            self.assertEqual(self._count(db), 0)

    def test_wrong_source_method_aborts(self):
        # pre-check가 insert 전에 잡음 (DB touch 0)
        with self.Session() as db:
            rows = [_write_dict(date(2025, 6, 4), source_method="kis_daily_backfill")]
            status, detail = K.write_krx_gap_rows(db, rows, expected_rows=1,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "ABORT")
            self.assertIn("source_method", detail)
            self.assertEqual(self._count(db), 0)  # insert 안 됨 (pre-check fail-fast)

    def test_wrong_source_or_asset_aborts(self):
        with self.Session() as db:
            rows = [_write_dict(date(2025, 6, 4), source="bithumb")]
            status, detail = K.write_krx_gap_rows(db, rows, expected_rows=1,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "ABORT")
            self.assertIn("source/asset", detail)

    def test_date_outside_window_aborts(self):
        with self.Session() as db:
            rows = [_write_dict(date(2024, 1, 1))]  # window [2025-06-01, 2026-06-03] 밖
            status, detail = K.write_krx_gap_rows(db, rows, expected_rows=1,
                                                  window_start=_WS, window_end=_WE)
            self.assertEqual(status, "ABORT")
            self.assertIn("window 밖", detail)


if __name__ == "__main__":
    unittest.main()
