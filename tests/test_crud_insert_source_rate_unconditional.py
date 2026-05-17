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


if __name__ == "__main__":
    unittest.main()
