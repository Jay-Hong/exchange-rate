"""KRX close finalizer Stage 2 — crud.insert_source_rate_unconditional tests.

KRX_CLOSE_SNAPSHOT_PLAN §5.2 (2026-05-17). close finalizer 전용 unconditional INSERT
helper — `insert_source_rate_if_changed`와 contract 다름:
    - timestamp 필수 (DB DEFAULT 사용 금지)
    - 가격 동일도 INSERT (dedup 우회)
    - 예외는 caller 전파 (격리 X)
    - 성공 시 항상 True (skip 분기 없음)

In-memory SQLite — 외부 DB 의존성 0.
"""
from __future__ import annotations

import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models


class TestInsertSourceRateUnconditional(unittest.TestCase):
    """unconditional INSERT — dedup 우회 + timestamp 필수 + bool contract."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_same_rate_inserts_new_row(self):
        """동일 가격이라도 INSERT (insert_if_changed 우회 검증).

        plan §5.2: close grace tick은 가격 동일 무관 unconditional INSERT.
        """
        ts1 = datetime(2026, 5, 19, 6, 45, 0)  # UTC naive = KST 15:45
        ts2 = datetime(2026, 5, 19, 6, 46, 0)
        with self.SessionFactory() as db:
            # 첫 row: insert_if_changed로 1490.6 저장
            crud.insert_source_rate_if_changed(
                db, "krx", "usd-krw-futures", 1490.6, timestamp=ts1,
            )
            # 같은 가격을 unconditional로 다시 저장 → 새 row 생성
            ok = crud.insert_source_rate_unconditional(
                db, "krx", "usd-krw-futures", 1490.6, timestamp=ts2,
            )
            self.assertTrue(ok)
            rows = (
                db.query(models.SourceRate)
                .filter(models.SourceRate.source == "krx")
                .order_by(models.SourceRate.timestamp.asc())
                .all()
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].rate, 1490.6)
            self.assertEqual(rows[0].timestamp, ts1)
            self.assertEqual(rows[1].rate, 1490.6)
            self.assertEqual(rows[1].timestamp, ts2)

    def test_explicit_timestamp_stored(self):
        """timestamp 인자가 SourceRate.timestamp에 정확히 저장 (UTC naive)."""
        boundary_utc = datetime(2026, 5, 19, 6, 45, 0)  # KST 15:45 → UTC 06:45
        with self.SessionFactory() as db:
            crud.insert_source_rate_unconditional(
                db, "krx", "usd-krw-futures", 1490.6, timestamp=boundary_utc,
            )
            row = db.query(models.SourceRate).filter(
                models.SourceRate.source == "krx"
            ).one()
            self.assertEqual(row.timestamp, boundary_utc)
            self.assertEqual(row.rate, 1490.6)

    def test_missing_timestamp_raises_type_error(self):
        """timestamp 누락 → TypeError (positional required, default 없음)."""
        with self.SessionFactory() as db:
            with self.assertRaises(TypeError):
                crud.insert_source_rate_unconditional(
                    db, "krx", "usd-krw-futures", 1490.6,
                )

    def test_returns_true_on_success(self):
        """성공 시 항상 True (skip 분기 없음 — `insert_if_changed`와 차이)."""
        ts = datetime(2026, 5, 19, 6, 45, 0)
        with self.SessionFactory() as db:
            self.assertIs(
                crud.insert_source_rate_unconditional(
                    db, "krx", "usd-krw-futures", 1490.6, timestamp=ts,
                ),
                True,
            )


class TestSuperLiteExchangeTs(unittest.TestCase):
    """§12.9.8 ③ super-lite — exchange event ts를 timestamp로 저장 → out-of-order stale이
    latest로 오판되지 않게 (migration/watermark 없이 기존 timestamp param + DESC 쿼리)."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _latest_row(self, db, source="upbit", asset="usdt-krw"):
        return (
            db.query(models.SourceRate)
            .filter(models.SourceRate.source == source, models.SourceRate.asset == asset)
            .order_by(models.SourceRate.timestamp.desc(), models.SourceRate.id.desc())
            .first()
        )

    def _count(self, db, source="upbit", asset="usdt-krw"):
        return (
            db.query(models.SourceRate)
            .filter(models.SourceRate.source == source, models.SourceRate.asset == asset)
            .count()
        )

    def test_event_ms_to_utc_naive(self):
        """ms → UTC naive datetime (KST aware 금지 계약). concrete 값 잠금."""
        ms = 1781000000000
        got = crud.event_ms_to_utc_naive(ms)
        self.assertIsNone(got.tzinfo)  # naive
        self.assertEqual(got, datetime(2026, 6, 9, 10, 13, 20))  # 1781000000s = 2026-06-09 10:13:20 UTC

    def test_stale_late_arrival_not_latest(self):
        """fresh T1 후 stale T0(<T1)가 늦게 INSERT돼도 latest는 T1 (super-lite 핵심)."""
        t1 = datetime(2026, 6, 11, 1, 0, 0)   # exchange ts (fresh)
        t0 = datetime(2026, 6, 11, 0, 59, 0)  # older (stale, 늦게 도착)
        with self.SessionFactory() as db:
            self.assertTrue(crud.insert_source_rate_if_changed(
                db, "upbit", "usdt-krw", 1500.0, timestamp=t1))
            # stale은 rate가 달라 INSERT됨(history엔 남음 — DB는 history store) — 단 latest 아님
            self.assertTrue(crud.insert_source_rate_if_changed(
                db, "upbit", "usdt-krw", 1499.0, timestamp=t0))
            self.assertEqual(self._count(db), 2)         # 둘 다 history에 존재
            self.assertEqual(self._latest_row(db).rate, 1500.0)  # latest = T1 (timestamp DESC)
            self.assertEqual(self._latest_row(db).timestamp, t1)

    def test_same_rate_skip_preserved(self):
        """동일 rate는 여전히 skip (super-lite가 기존 change-only 동작 불변)."""
        t1 = datetime(2026, 6, 11, 1, 0, 0)
        t2 = datetime(2026, 6, 11, 1, 1, 0)
        with self.SessionFactory() as db:
            self.assertTrue(crud.insert_source_rate_if_changed(
                db, "upbit", "usdt-krw", 1500.0, timestamp=t1))
            self.assertFalse(crud.insert_source_rate_if_changed(
                db, "upbit", "usdt-krw", 1500.0, timestamp=t2))  # same rate → skip
            self.assertEqual(self._count(db), 1)

    def test_dedup_compares_timestamp_latest_not_stale(self):
        """stale INSERT 후 dedup이 timestamp-latest(T1)와 비교 — change-only 의미 보존.

        T1(1500)+stale T0(1499) 후 incoming 1500은 timestamp-latest(T1=1500)와 같아 skip.
        (만약 stale T0=1499와 비교했다면 1500≠1499라 INSERT됐을 것 → dedup ordering 잠금.)
        """
        t1 = datetime(2026, 6, 11, 1, 0, 0)
        t0 = datetime(2026, 6, 11, 0, 59, 0)
        t2 = datetime(2026, 6, 11, 1, 2, 0)
        with self.SessionFactory() as db:
            crud.insert_source_rate_if_changed(db, "upbit", "usdt-krw", 1500.0, timestamp=t1)
            crud.insert_source_rate_if_changed(db, "upbit", "usdt-krw", 1499.0, timestamp=t0)
            self.assertFalse(crud.insert_source_rate_if_changed(
                db, "upbit", "usdt-krw", 1500.0, timestamp=t2))  # dedup vs T1(1500) → skip
            self.assertEqual(self._count(db), 2)


if __name__ == "__main__":
    unittest.main()
