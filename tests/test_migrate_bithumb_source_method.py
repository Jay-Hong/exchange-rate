"""Bithumb source_method rename migration 단위 테스트 (ADR-034 Amendment).

Codex 명시 회귀 7종:
  1. 정상 rename
  2. expected old/new count mismatch → update 전 abort
  3. idempotent rerun → skip (+ stale abort)
  4. Bithumb 외 source(krx/hana) row 불변
  5. 같은 asset의 unrelated method(observed_rollup) row 불변
  6. source_method 외 OHLC/metadata/captured_at 불변
  7. production guard 차단

In-memory SQLite — 외부 DB 의존성 0.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.models import SourceDailyRate  # noqa: E402
import migrate_bithumb_source_method as M  # noqa: E402


def _row(*, source="bithumb", asset="usdt-krw", date_kst=date(2026, 5, 21),
         source_method="bithumb_candlestick_backfill", close="1500.0",
         ohlc="source_ohlc", close_basis="bithumb_24h_kst_close", meta=None):
    c = Decimal(close)
    return SourceDailyRate(
        source=source, asset=asset, date_kst=date_kst,
        rate=c, high=c, low=c, close=c,
        ohlc_quality=ohlc, close_basis=close_basis, source_method=source_method,
        contract_code=None, basis_date=None, published_at=None,
        metadata_json=meta or {"candle_ts_kst": "2026-05-21T00:00:00+09:00"},
    )


class TestRunMigration(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _counts(self, db):
        return dict(
            db.query(SourceDailyRate.source_method, func.count())
            .filter(SourceDailyRate.source == "bithumb", SourceDailyRate.asset == "usdt-krw")
            .group_by(SourceDailyRate.source_method).all()
        )

    def test_normal_rename_and_other_cols_unchanged(self):
        """[1+6] 정상 rename + source_method 외 OHLC/metadata/captured_at 불변."""
        with self.Session() as db:
            for i in range(3):
                db.add(_row(date_kst=date(2026, 5, 21 + i), close=str(1500 + i)))
            db.commit()
            # 원본 snapshot (id → cols)
            orig = {
                r.id: (r.close, r.high, r.low, r.metadata_json, r.captured_at, r.close_basis, r.ohlc_quality)
                for r in db.query(SourceDailyRate).all()
            }

        with self.Session() as db:
            status, detail = M.run_migration(db, expected_old=3, expected_new=0)
            self.assertEqual(status, "MIGRATE_OK", detail)
            db.commit()

        with self.Session() as db:
            c = self._counts(db)
            self.assertEqual(c.get("bithumb_candlestick_backfill", 0), 0)
            self.assertEqual(c.get("bithumb_candlestick_api", 0), 3)
            # 다른 컬럼 불변
            for r in db.query(SourceDailyRate).all():
                o = orig[r.id]
                self.assertEqual((r.close, r.high, r.low, r.metadata_json, r.captured_at, r.close_basis, r.ohlc_quality), o)
                self.assertEqual(r.source_method, "bithumb_candlestick_api")

    def test_old_count_mismatch_aborts_before_update(self):
        """[2] expected old count mismatch → update 전 abort, 데이터 불변."""
        with self.Session() as db:
            db.add(_row())
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, expected_old=5, expected_new=0)  # actual old=1
            self.assertEqual(status, "ABORT")
            self.assertIn("old_count", detail)
            db.rollback()
        with self.Session() as db:
            self.assertEqual(self._counts(db).get("bithumb_candlestick_backfill"), 1)

    def test_new_count_mismatch_aborts(self):
        """[2] expected new count mismatch → abort."""
        with self.Session() as db:
            db.add(_row())
            db.add(_row(date_kst=date(2026, 5, 30), source_method="bithumb_candlestick_api"))
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, expected_old=1, expected_new=5)  # actual new=1
            self.assertEqual(status, "ABORT")
            self.assertIn("new_count", detail)

    def test_idempotent_rerun_skip(self):
        """[3] 이미 migrated (old=0, new=N+M) → SKIP."""
        with self.Session() as db:
            for i in range(3):
                db.add(_row(date_kst=date(2026, 5, 21 + i), source_method="bithumb_candlestick_api"))
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, expected_old=3, expected_new=0)
            self.assertEqual(status, "SKIP", detail)

    def test_idempotent_stale_aborts(self):
        """[3] old=0이나 new != N+M → stale abort (cron 추가 등)."""
        with self.Session() as db:
            for i in range(4):
                db.add(_row(date_kst=date(2026, 5, 21 + i), source_method="bithumb_candlestick_api"))
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, expected_old=3, expected_new=0)  # N+M=3, actual new=4
            self.assertEqual(status, "ABORT")
            self.assertIn("stale", detail)

    def test_negative_old_count_aborts(self):
        """[fail-open] expected_old 음수 → ABORT (빈 DB여도 SKIP 오인 안 함)."""
        with self.Session() as db:
            status, detail = M.run_migration(db, expected_old=-1, expected_new=1)
            self.assertEqual(status, "ABORT")
            self.assertIn("음수", detail)

    def test_negative_new_count_aborts(self):
        """[fail-open] expected_new 음수 → ABORT."""
        with self.Session() as db:
            status, detail = M.run_migration(db, expected_old=1, expected_new=-1)
            self.assertEqual(status, "ABORT")
            self.assertIn("음수", detail)

    def test_zero_zero_empty_target_aborts(self):
        """[fail-open] N+M==0 (빈 대상) → ABORT (SKIP 오인 차단)."""
        with self.Session() as db:  # 빈 DB
            status, detail = M.run_migration(db, expected_old=0, expected_new=0)
            self.assertEqual(status, "ABORT")
            self.assertIn("대상", detail)

    def test_other_source_unchanged(self):
        """[4] Bithumb 외 source(krx/hana) row 불변."""
        with self.Session() as db:
            db.add(_row())
            db.add(_row(source="krx", asset="usd-krw-futures", source_method="kis_daily_backfill"))
            db.add(_row(source="hana", asset="usd-krw", source_method="external_backfill"))
            db.commit()
        with self.Session() as db:
            status, _ = M.run_migration(db, expected_old=1, expected_new=0)
            self.assertEqual(status, "MIGRATE_OK")
            db.commit()
        with self.Session() as db:
            self.assertEqual(db.query(SourceDailyRate).filter(SourceDailyRate.source == "krx").first().source_method, "kis_daily_backfill")
            self.assertEqual(db.query(SourceDailyRate).filter(SourceDailyRate.source == "hana").first().source_method, "external_backfill")

    def test_unrelated_method_same_asset_unchanged(self):
        """[5] 같은 bithumb/usdt-krw의 unrelated method(observed_rollup) row 불변."""
        with self.Session() as db:
            db.add(_row())  # old method
            db.add(_row(date_kst=date(2026, 6, 1), source_method="observed_rollup"))
            db.commit()
        with self.Session() as db:
            status, _ = M.run_migration(db, expected_old=1, expected_new=0)
            self.assertEqual(status, "MIGRATE_OK")
            db.commit()
        with self.Session() as db:
            orr = db.query(SourceDailyRate).filter(SourceDailyRate.source_method == "observed_rollup").all()
            self.assertEqual(len(orr), 1)  # 불변


class TestProductionGuard(unittest.TestCase):
    """[7] production guard — non-SQLite는 --allow-production-write 필수."""

    def _fake(self, dialect):
        f = MagicMock()
        f.url.get_dialect.return_value.name = dialect
        f.url.host = "rds-host"
        return f

    def test_rejects_non_sqlite_without_allow(self):
        with patch("app.database.engine", self._fake("postgresql")):
            err = M.check_production_write_guard(allow_production=False)
        self.assertIsNotNone(err)
        self.assertIn("postgresql", err)
        self.assertIn("***", err)

    def test_allows_with_flag(self):
        with patch("app.database.engine", self._fake("postgresql")):
            self.assertIsNone(M.check_production_write_guard(allow_production=True))

    def test_sqlite_passes(self):
        with patch("app.database.engine", self._fake("sqlite")):
            self.assertIsNone(M.check_production_write_guard(allow_production=False))


if __name__ == "__main__":
    unittest.main()
