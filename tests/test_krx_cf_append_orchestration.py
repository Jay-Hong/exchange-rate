"""KRX CF daily-append orchestration (Unit 4a) — append_krx_cf_daily_row 테스트.

in-memory SQLite (source_rates seed + source_daily_rates). Unit 1→2→3 묶음 + transaction:
  1. INSERT (CF ticks seed) → observed_rollup row commit (별도 session 확인)
  2. close_only (CF ticks 0) → close_only row commit
  3. SKIP (existing close match) → no write (기존 불변)
  4. HARD (existing close mismatch) → no write (rollback)
  5. builder 오류(contract 빈값) → ValueError 전파 (caller 격리)
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.models import SourceDailyRate, SourceRate  # noqa: E402
from app.source_daily_rates import (  # noqa: E402
    KRX_APPEND_HARD,
    KRX_APPEND_INSERT,
    KRX_APPEND_SKIP,
    append_krx_cf_daily_row,
)

_KST = timezone(timedelta(hours=9))
_D = date(2026, 6, 3)


def _utc_naive_from_kst(hh, mm, ss=0, d=_D):
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=_KST).astimezone(
        timezone.utc).replace(tzinfo=None)


def _existing_daily(*, close, high, low, source_method="krx_openapi_daily"):
    c, h, low_ = Decimal(close), Decimal(high), Decimal(low)
    return SourceDailyRate(
        source="krx", asset="usd-krw-futures", date_kst=_D,
        rate=c, high=h, low=low_, close=c, ohlc_quality="source_ohlc",
        close_basis="krx_cf_close_1545", source_method=source_method,
        contract_code="A75606", basis_date=None, published_at=None, metadata_json={})


class TestAppendKrxCfDailyRow(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _seed_cf_ticks(self, db, rates_at):
        for hh, mm, rate in rates_at:
            db.add(SourceRate(source="krx", asset="usd-krw-futures", rate=rate,
                              timestamp=_utc_naive_from_kst(hh, mm)))
        db.commit()

    def _daily_count(self, db):
        return db.query(func.count()).select_from(SourceDailyRate).scalar()

    def test_insert_observed_rollup(self):
        with self.Session() as db:
            self._seed_cf_ticks(db, [(9, 0, 1500.0), (11, 0, 1510.0), (13, 0, 1495.0)])
            action, _ = append_krx_cf_daily_row(db, _D, 1505.0, "A75606")
            self.assertEqual(action, KRX_APPEND_INSERT)
        with self.Session() as db:  # commit 확인 (별도 session)
            r = db.query(SourceDailyRate).one()
            self.assertEqual(r.ohlc_quality, "observed_rollup")
            self.assertEqual(r.close, Decimal("1505.0"))
            self.assertEqual(r.high, Decimal("1510.0"))
            self.assertEqual(r.low, Decimal("1495.0"))
            self.assertEqual(r.source_method, "close_finalizer")
            self.assertEqual(r.contract_code, "A75606")

    def test_insert_close_only_when_no_ticks(self):
        with self.Session() as db:  # source_rates CF tick 0
            action, _ = append_krx_cf_daily_row(db, _D, 1500.0, "A75606")
            self.assertEqual(action, KRX_APPEND_INSERT)
        with self.Session() as db:
            r = db.query(SourceDailyRate).one()
            self.assertEqual(r.ohlc_quality, "close_only")
            self.assertEqual(r.high, Decimal("1500.0"))
            self.assertEqual(r.low, Decimal("1500.0"))
            self.assertEqual(r.metadata_json["cf_session_point_count"], 0)

    def test_skip_existing_close_match(self):
        with self.Session() as db:
            self._seed_cf_ticks(db, [(11, 0, 1505.0)])
            db.add(_existing_daily(close="1505.0", high="1510.0", low="1495.0"))
            db.commit()
            action, _ = append_krx_cf_daily_row(db, _D, 1505.0, "A75606")
            self.assertEqual(action, KRX_APPEND_SKIP)
        with self.Session() as db:
            self.assertEqual(self._daily_count(db), 1)  # 신규 없음
            self.assertEqual(db.query(SourceDailyRate).one().source_method, "krx_openapi_daily")  # 불변

    def test_hard_existing_close_mismatch(self):
        with self.Session() as db:
            self._seed_cf_ticks(db, [(11, 0, 1505.0)])
            db.add(_existing_daily(close="1500.0", high="1500.0", low="1500.0"))
            db.commit()
            action, _ = append_krx_cf_daily_row(db, _D, 1505.0, "A75606")
            self.assertEqual(action, KRX_APPEND_HARD)
        with self.Session() as db:
            self.assertEqual(self._daily_count(db), 1)  # write 0
            self.assertEqual(db.query(SourceDailyRate).one().close, Decimal("1500.0"))

    def test_builder_error_propagates(self):
        with self.Session() as db:
            with self.assertRaises(ValueError):
                append_krx_cf_daily_row(db, _D, 1500.0, "")  # contract 빈값 → build ValueError

    # ── transaction contract spy (commit/rollback 호출 직접 검증) ──
    def test_insert_calls_commit_not_rollback(self):
        with self.Session() as db:
            self._seed_cf_ticks(db, [(11, 0, 1505.0)])  # seed (실 commit, mock 전)
            with mock.patch.object(db, "commit") as m_commit, \
                 mock.patch.object(db, "rollback") as m_rollback:
                action, _ = append_krx_cf_daily_row(db, _D, 1505.0, "A75606")
            self.assertEqual(action, KRX_APPEND_INSERT)
            m_commit.assert_called_once()
            m_rollback.assert_not_called()

    def test_skip_calls_rollback_not_commit(self):
        with self.Session() as db:
            self._seed_cf_ticks(db, [(11, 0, 1505.0)])
            db.add(_existing_daily(close="1505.0", high="1510.0", low="1495.0"))
            db.commit()
            with mock.patch.object(db, "commit") as m_commit, \
                 mock.patch.object(db, "rollback") as m_rollback:
                action, _ = append_krx_cf_daily_row(db, _D, 1505.0, "A75606")
            self.assertEqual(action, KRX_APPEND_SKIP)
            m_rollback.assert_called_once()
            m_commit.assert_not_called()

    def test_hard_calls_rollback_not_commit(self):
        with self.Session() as db:
            self._seed_cf_ticks(db, [(11, 0, 1505.0)])
            db.add(_existing_daily(close="1500.0", high="1500.0", low="1500.0"))
            db.commit()
            with mock.patch.object(db, "commit") as m_commit, \
                 mock.patch.object(db, "rollback") as m_rollback:
                action, _ = append_krx_cf_daily_row(db, _D, 1505.0, "A75606")
            self.assertEqual(action, KRX_APPEND_HARD)
            m_rollback.assert_called_once()
            m_commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
