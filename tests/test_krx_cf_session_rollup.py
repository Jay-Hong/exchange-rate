"""KRX CF session rollup helper (Unit 1) — get_krx_cf_session_rollup 단위 테스트.

in-memory SQLite. 핵심:
  1. CF session(KST 08:30~15:45) tick만 집계 (high=max, low=min)
  2. CM 야간 / 새벽 tick 제외
  3. window boundary(08:30:00 / 15:45:00) inclusive, 직전·직후 제외
  4. empty window → point_count=0, high/low/ts=None (명시 result object)
  5. 타 source/asset 제외
  6. UTC naive timestamp를 KST로 정확히 해석 (경계 변환) + first/last_ts 정렬
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.models import SourceRate  # noqa: E402
from app.source_daily_rates import CfSessionRollup, get_krx_cf_session_rollup  # noqa: E402

_KST = timezone(timedelta(hours=9))


def _utc_naive_from_kst(y, m, d, hh, mm, ss=0):
    """KST wall-clock → UTC naive (source_rates 저장 형식)."""
    return datetime(y, m, d, hh, mm, ss, tzinfo=_KST).astimezone(timezone.utc).replace(tzinfo=None)


class TestGetKrxCfSessionRollup(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _add(self, db, *, ts_utc_naive, rate, source="krx", asset="usd-krw-futures"):
        db.add(SourceRate(source=source, asset=asset, rate=rate, timestamp=ts_utc_naive))

    def test_cf_session_only_rollup(self):
        d = date(2026, 6, 3)
        with self.Session() as db:
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 9, 0), rate=1500.0)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 11, 0), rate=1510.0)  # high
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 13, 0), rate=1495.0)  # low
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 15, 0), rate=1505.0)
            db.commit()
            r = get_krx_cf_session_rollup(db, d)
        self.assertEqual(r.point_count, 4)
        self.assertEqual(r.high, 1510.0)
        self.assertEqual(r.low, 1495.0)

    def test_cm_night_and_dawn_ticks_excluded(self):
        # CF window 밖(야간 23:00 / 새벽 05:00) tick 제외
        d = date(2026, 6, 3)
        with self.Session() as db:
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 11, 0), rate=1500.0)  # CF (포함)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 23, 0), rate=1600.0)  # 야간 (제외)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 5, 0), rate=1400.0)   # 새벽 (제외)
            db.commit()
            r = get_krx_cf_session_rollup(db, d)
        self.assertEqual(r.point_count, 1)
        self.assertEqual(r.high, 1500.0)
        self.assertEqual(r.low, 1500.0)

    def test_boundary_inclusive(self):
        d = date(2026, 6, 3)
        with self.Session() as db:
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 8, 30, 0), rate=1490.0)   # 시작 경계 (포함)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 15, 45, 0), rate=1520.0)  # 종료 경계 (포함)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 8, 29, 59), rate=1400.0)  # 직전 (제외)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 15, 45, 1), rate=1600.0)  # 직후 (제외)
            db.commit()
            r = get_krx_cf_session_rollup(db, d)
        self.assertEqual(r.point_count, 2)
        self.assertEqual(r.high, 1520.0)
        self.assertEqual(r.low, 1490.0)

    def test_empty_window(self):
        with self.Session() as db:
            r = get_krx_cf_session_rollup(db, date(2026, 6, 3))
        self.assertEqual(r.point_count, 0)
        self.assertIsNone(r.high)
        self.assertIsNone(r.low)
        self.assertIsNone(r.first_ts)
        self.assertIsNone(r.last_ts)

    def test_other_source_asset_excluded(self):
        d = date(2026, 6, 3)
        with self.Session() as db:
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 11, 0), rate=1500.0)  # krx (포함)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 11, 0), rate=9999.0,
                      source="bithumb", asset="usdt-krw")                              # 타 source (제외)
            self._add(db, ts_utc_naive=_utc_naive_from_kst(2026, 6, 3, 11, 0), rate=8888.0,
                      source="krx", asset="usd-krw")                                   # 타 asset (제외)
            db.commit()
            r = get_krx_cf_session_rollup(db, d)
        self.assertEqual(r.point_count, 1)
        self.assertEqual(r.high, 1500.0)

    def test_first_last_ts_ordered(self):
        d = date(2026, 6, 3)
        first = _utc_naive_from_kst(2026, 6, 3, 9, 0)
        last = _utc_naive_from_kst(2026, 6, 3, 15, 0)
        with self.Session() as db:
            self._add(db, ts_utc_naive=last, rate=1505.0)    # insert 순서 뒤집어도 ts 정렬
            self._add(db, ts_utc_naive=first, rate=1500.0)
            db.commit()
            r = get_krx_cf_session_rollup(db, d)
        self.assertEqual(r.first_ts, first)
        self.assertEqual(r.last_ts, last)

    def test_returns_explicit_result_object(self):
        with self.Session() as db:
            r = get_krx_cf_session_rollup(db, date(2026, 6, 3))
        self.assertIsInstance(r, CfSessionRollup)


if __name__ == "__main__":
    unittest.main()
