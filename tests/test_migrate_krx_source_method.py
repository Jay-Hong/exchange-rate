"""KRX source_method migration 단위 테스트 (ADR-034 §11 / Step 4B 전환).

migrate_bithumb 패턴 미러링 + KRX 핵심 차이(date window scoping) 검증:
  1. 정상 relabel + source_method 외 전 컬럼 불변 (surgical)
  2. expected old/new count mismatch → update 전 abort
  3. idempotent rerun → skip (+ stale abort)
  4. **window scoping — window 밖 kis row 미touch** (KRX 전용 핵심)
  5. inverted window → abort
  6. KRX 외 source(hana/bithumb) row 불변
  7. 음수/0-0 count fail-open 차단
  8. production guard 차단

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
import migrate_krx_source_method as M  # noqa: E402

_WS = date(2026, 4, 20)
_WE = date(2026, 5, 27)


def _row(*, source="krx", asset="usd-krw-futures", date_kst=date(2026, 4, 20),
         source_method="kis_daily_backfill", close="1496.5", contract_code="A75605",
         close_basis="krx_cf_close_1545"):
    c = Decimal(close)
    return SourceDailyRate(
        source=source, asset=asset, date_kst=date_kst,
        rate=c, high=c, low=c, close=c,
        ohlc_quality="source_ohlc", close_basis=close_basis, source_method=source_method,
        contract_code=contract_code, basis_date=None, published_at=None,
        metadata_json={"contract_short_code": contract_code, "contract_month": "202604"},
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
            .filter(SourceDailyRate.source == "krx", SourceDailyRate.asset == "usd-krw-futures")
            .group_by(SourceDailyRate.source_method).all()
        )

    def test_normal_relabel_and_other_cols_unchanged(self):
        """[1] 정상 relabel + source_method 외 전 컬럼 불변 (surgical)."""
        with self.Session() as db:
            for i in range(3):
                db.add(_row(date_kst=date(2026, 4, 20 + i), close=str(1496 + i)))
            db.commit()
            orig = {
                r.id: (r.close, r.high, r.low, r.contract_code, r.metadata_json,
                       r.captured_at, r.close_basis, r.ohlc_quality, r.date_kst)
                for r in db.query(SourceDailyRate).all()
            }
        with self.Session() as db:
            status, detail = M.run_migration(db, _WS, _WE, expected_old=3, expected_new=0)
            self.assertEqual(status, "MIGRATE_OK", detail)
            db.commit()
        with self.Session() as db:
            c = self._counts(db)
            self.assertEqual(c.get("kis_daily_backfill", 0), 0)
            self.assertEqual(c.get("krx_openapi_daily", 0), 3)
            for r in db.query(SourceDailyRate).all():
                o = orig[r.id]
                self.assertEqual(
                    (r.close, r.high, r.low, r.contract_code, r.metadata_json,
                     r.captured_at, r.close_basis, r.ohlc_quality, r.date_kst), o)
                self.assertEqual(r.source_method, "krx_openapi_daily")

    def test_old_count_mismatch_aborts(self):
        with self.Session() as db:
            db.add(_row())
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, _WS, _WE, expected_old=5, expected_new=0)
            self.assertEqual(status, "ABORT")
            self.assertIn("old_count", detail)
            db.rollback()
        with self.Session() as db:  # update 전 abort → 원본 유지
            self.assertEqual(self._counts(db).get("kis_daily_backfill", 0), 1)

    def test_new_count_mismatch_aborts(self):
        with self.Session() as db:
            db.add(_row())
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, _WS, _WE, expected_old=1, expected_new=5)
            self.assertEqual(status, "ABORT")
            self.assertIn("new_count", detail)

    def test_idempotent_rerun_skip(self):
        with self.Session() as db:
            for i in range(3):
                db.add(_row(date_kst=date(2026, 4, 20 + i)))
            db.commit()
        with self.Session() as db:
            M.run_migration(db, _WS, _WE, expected_old=3, expected_new=0)
            db.commit()
        with self.Session() as db:  # 재실행: old=0, new=3=N+M → SKIP
            status, detail = M.run_migration(db, _WS, _WE, expected_old=3, expected_new=0)
            self.assertEqual(status, "SKIP", detail)

    def test_idempotent_stale_aborts(self):
        # old=0 이미 migrated인데 new(3) != N+M(3+1=4) → stale abort
        with self.Session() as db:
            for i in range(3):
                db.add(_row(date_kst=date(2026, 4, 20 + i), source_method="krx_openapi_daily"))
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, _WS, _WE, expected_old=3, expected_new=1)
            self.assertEqual(status, "ABORT")
            self.assertIn("stale", detail)

    def test_window_scoping_outside_untouched(self):
        """[4] KRX 핵심 — window 밖 kis row는 절대 안 건드림."""
        with self.Session() as db:
            db.add(_row(date_kst=date(2026, 4, 20)))   # window 안
            db.add(_row(date_kst=date(2026, 5, 27)))   # window 경계 (포함)
            db.add(_row(date_kst=date(2026, 4, 19)))   # window 밖 (전)
            db.add(_row(date_kst=date(2026, 6, 1)))    # window 밖 (후)
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, _WS, _WE, expected_old=2, expected_new=0)
            self.assertEqual(status, "MIGRATE_OK", detail)
            db.commit()
        with self.Session() as db:
            rows = {r.date_kst: r.source_method for r in db.query(SourceDailyRate).all()}
            self.assertEqual(rows[date(2026, 4, 20)], "krx_openapi_daily")   # 전환
            self.assertEqual(rows[date(2026, 5, 27)], "krx_openapi_daily")   # 경계 전환
            self.assertEqual(rows[date(2026, 4, 19)], "kis_daily_backfill")  # 미touch
            self.assertEqual(rows[date(2026, 6, 1)], "kis_daily_backfill")   # 미touch

    def test_inverted_window_aborts(self):
        with self.Session() as db:
            db.add(_row())
            db.commit()
        with self.Session() as db:
            status, detail = M.run_migration(db, _WE, _WS, expected_old=1, expected_new=0)  # start>end
            self.assertEqual(status, "ABORT")
            self.assertIn("역전", detail)

    def test_negative_count_aborts(self):
        with self.Session() as db:
            status, _ = M.run_migration(db, _WS, _WE, expected_old=-1, expected_new=1)
            self.assertEqual(status, "ABORT")

    def test_zero_zero_empty_target_aborts(self):
        with self.Session() as db:
            status, _ = M.run_migration(db, _WS, _WE, expected_old=0, expected_new=0)
            self.assertEqual(status, "ABORT")

    def test_other_source_unchanged(self):
        """[6] KRX 외 source(hana/bithumb) + 다른 asset row 불변."""
        with self.Session() as db:
            db.add(_row())  # krx 대상 1
            db.add(_row(source="hana", asset="usd-krw", source_method="hana_observed_eod",
                        close_basis="hana_observed_eod", contract_code=None))
            db.add(_row(source="bithumb", asset="usdt-krw", source_method="bithumb_candlestick_api",
                        close_basis="bithumb_24h_kst_close", contract_code=None))
            db.commit()
        with self.Session() as db:
            status, _ = M.run_migration(db, _WS, _WE, expected_old=1, expected_new=0)
            self.assertEqual(status, "MIGRATE_OK")
            db.commit()
        with self.Session() as db:
            methods = {(r.source, r.source_method) for r in db.query(SourceDailyRate).all()}
            self.assertIn(("krx", "krx_openapi_daily"), methods)
            self.assertIn(("hana", "hana_observed_eod"), methods)       # 불변
            self.assertIn(("bithumb", "bithumb_candlestick_api"), methods)  # 불변


class TestProductionGuard(unittest.TestCase):
    def _engine(self, dialect, host="db.example.com"):
        eng = MagicMock()
        eng.url.get_dialect.return_value.name = dialect
        eng.url.host = host
        return eng

    def test_rejects_non_sqlite_without_allow(self):
        with patch("app.database.engine", self._engine("postgresql")):
            err = M.check_production_write_guard(allow_production=False)
        self.assertIsNotNone(err)
        self.assertIn("production write 차단", err)
        self.assertNotIn("db.example.com", err)  # host redacted

    def test_allows_with_flag(self):
        with patch("app.database.engine", self._engine("postgresql")):
            err = M.check_production_write_guard(allow_production=True)
        self.assertIsNone(err)

    def test_sqlite_passes(self):
        with patch("app.database.engine", self._engine("sqlite")):
            err = M.check_production_write_guard(allow_production=False)
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
