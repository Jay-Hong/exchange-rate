"""Slice B — source 알림 히스토리 read 경로 (crud.get_source_notification_logs).

source_notification_logs는 기존 create-only였고 이 함수가 최초 reader. 검증:
    - user_id 스코프 (cross-user 격리 — 보안 핵심)
    - 최신순 (sent_at DESC)
    - asset 필터 (SQL WHERE)
    - success_only (실패 row 제외 = 사용자용 '받은 알림' / 기본 True)
    - limit (crud 레벨 — '최근 N건'; endpoint의 1..200 cap은 main.py inline)

In-memory SQLite — 외부 의존성 0 (firebase/RDS 불요).
endpoint(auth/premium/cap/builder) wiring은 codex impl 리뷰 + 기존
source-notification unit-test precedent에 따라 crud 레벨로 검증 (endpoint
TestClient 테스트는 client 계약이 되는 PR2에서 추가 검토).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models


class TestGetSourceNotificationLogs(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _add_log(
        self,
        db,
        user_id,
        source,
        asset,
        sent_at,
        success=True,
        triggered_rate=1500.0,
    ):
        log = models.SourceNotificationLog(
            user_id=user_id,
            setting_id=None,
            source=source,
            asset=asset,
            condition="above",
            threshold=1490.0,
            triggered_rate=triggered_rate,
            success=success,
            error_message=None if success else "no successful sends",
            sent_at=sent_at,
        )
        db.add(log)
        db.commit()
        return log

    def test_returns_only_own_user_rows(self):
        """cross-user 격리 — user A는 user B row를 절대 못 본다 (보안 핵심)."""
        base = datetime(2026, 6, 25, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "userA", "bithumb", "usdt-krw", base)
            self._add_log(db, "userB", "upbit", "usdt-krw", base)
            rows = crud.get_source_notification_logs(db, "userA")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].user_id, "userA")
            self.assertEqual(rows[0].source, "bithumb")

    def test_latest_first(self):
        """sent_at DESC — 최신 발송이 먼저."""
        base = datetime(2026, 6, 25, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "u", "bithumb", "usdt-krw", base)
            self._add_log(
                db, "u", "krx", "usd-krw-futures", base + timedelta(hours=1)
            )
            rows = crud.get_source_notification_logs(db, "u")
            self.assertEqual([r.source for r in rows], ["krx", "bithumb"])

    def test_asset_filter(self):
        """asset 필터 — KRX(usd-krw-futures)만 / 거래소 제외."""
        base = datetime(2026, 6, 25, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "u", "bithumb", "usdt-krw", base)
            self._add_log(
                db, "u", "krx", "usd-krw-futures", base + timedelta(hours=1)
            )
            rows = crud.get_source_notification_logs(
                db, "u", asset="usd-krw-futures"
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].asset, "usd-krw-futures")

    def test_success_only_excludes_failures(self):
        """기본 success_only=True → 실패 row 제외 (사용자용 '받은 알림').

        success_only=False → 운영 진단용으로 전체 반환.
        """
        base = datetime(2026, 6, 25, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "u", "bithumb", "usdt-krw", base, success=True)
            self._add_log(
                db,
                "u",
                "upbit",
                "usdt-krw",
                base + timedelta(minutes=1),
                success=False,
            )
            rows = crud.get_source_notification_logs(db, "u")
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].success)

            rows_all = crud.get_source_notification_logs(
                db, "u", success_only=False
            )
            self.assertEqual(len(rows_all), 2)

    def test_limit_caps_and_returns_most_recent(self):
        """limit으로 '최근 N건'만 — 최신순 cap."""
        base = datetime(2026, 6, 25, 0, 0, 0)
        with self.SessionFactory() as db:
            for i in range(5):
                self._add_log(
                    db, "u", "bithumb", "usdt-krw", base + timedelta(minutes=i)
                )
            rows = crud.get_source_notification_logs(db, "u", limit=3)
            self.assertEqual(len(rows), 3)
            # 가장 최신(분 4)이 첫 번째
            self.assertEqual(rows[0].sent_at, base + timedelta(minutes=4))
            self.assertEqual(rows[-1].sent_at, base + timedelta(minutes=2))

    def test_empty_when_no_rows(self):
        """row 없으면 빈 목록 (신규 사용자)."""
        with self.SessionFactory() as db:
            rows = crud.get_source_notification_logs(db, "nobody")
            self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
