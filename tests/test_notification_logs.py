"""FX 은행 가격알림 히스토리 read 경로 (crud.get_notification_logs) + condition/threshold 보강.

notification_logs는 기존 create-only였고 get_notification_logs가 최초 reader (완전판 2026-07-15).
검증 (test_source_notification_logs 미러):
    - user_id 스코프 (cross-user 격리 — 보안 핵심)
    - 최신순 (sent_at DESC)
    - currency 필터 (SQL WHERE)
    - success_only (실패 row 제외 = 사용자용 '받은 알림' / 기본 True)
    - limit ('최근 N건')
    - create_notification_log의 condition/threshold inline 스냅샷 저장 + old row NULL 하위호환

In-memory SQLite — 외부 의존성 0 (firebase/RDS 불요).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models


class TestGetNotificationLogs(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def _add_log(self, db, user_id, bank, currency, sent_at, success=True, rate=1475.0):
        log = models.NotificationLog(
            user_id=user_id,
            setting_id=None,
            bank=bank,
            currency=currency,
            rate=rate,
            condition="above",
            threshold=1470.0,
            success=success,
            error_message=None if success else "no successful sends",
            sent_at=sent_at,
        )
        db.add(log)
        db.commit()
        return log

    def test_returns_only_own_user_rows(self):
        """cross-user 격리 — user A는 user B row를 절대 못 본다 (보안 핵심)."""
        base = datetime(2026, 7, 15, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "userA", "hana", "usd-krw", base)
            self._add_log(db, "userB", "kb", "usd-krw", base)
            rows = crud.get_notification_logs(db, "userA")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].user_id, "userA")
            self.assertEqual(rows[0].bank, "hana")

    def test_latest_first(self):
        """sent_at DESC — 최신 발송이 먼저."""
        base = datetime(2026, 7, 15, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "u", "hana", "usd-krw", base)
            self._add_log(db, "u", "kb", "jpy-krw", base + timedelta(hours=1))
            rows = crud.get_notification_logs(db, "u")
            self.assertEqual([r.bank for r in rows], ["kb", "hana"])

    def test_currency_filter(self):
        """currency 필터 — usd-krw만."""
        base = datetime(2026, 7, 15, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "u", "hana", "usd-krw", base)
            self._add_log(db, "u", "kb", "jpy-krw", base + timedelta(hours=1))
            rows = crud.get_notification_logs(db, "u", currency="usd-krw")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].currency, "usd-krw")

    def test_success_only_excludes_failures(self):
        """기본 success_only=True → 실패 row 제외. False → 전체(운영 진단)."""
        base = datetime(2026, 7, 15, 0, 0, 0)
        with self.SessionFactory() as db:
            self._add_log(db, "u", "hana", "usd-krw", base, success=True)
            self._add_log(db, "u", "kb", "usd-krw", base + timedelta(minutes=1), success=False)
            rows = crud.get_notification_logs(db, "u")
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].success)
            rows_all = crud.get_notification_logs(db, "u", success_only=False)
            self.assertEqual(len(rows_all), 2)

    def test_limit_caps_and_returns_most_recent(self):
        """limit으로 '최근 N건'만 — 최신순 cap."""
        base = datetime(2026, 7, 15, 0, 0, 0)
        with self.SessionFactory() as db:
            for i in range(5):
                self._add_log(db, "u", "hana", "usd-krw", base + timedelta(minutes=i))
            rows = crud.get_notification_logs(db, "u", limit=3)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0].sent_at, base + timedelta(minutes=4))
            self.assertEqual(rows[-1].sent_at, base + timedelta(minutes=2))

    def test_empty_when_no_rows(self):
        """row 없으면 빈 목록 (신규 사용자)."""
        with self.SessionFactory() as db:
            rows = crud.get_notification_logs(db, "nobody")
            self.assertEqual(rows, [])

    def test_create_stores_condition_threshold(self):
        """create_notification_log이 condition/threshold inline 스냅샷 저장 (완전판)."""
        with self.SessionFactory() as db:
            log = crud.create_notification_log(
                db=db, user_id="u", setting_id=7, bank="hana", currency="usd-krw",
                rate=1476.2, success=True, condition="above", threshold=1470.0,
            )
            self.assertEqual(log.condition, "above")
            self.assertEqual(log.threshold, 1470.0)
            self.assertEqual(log.rate, 1476.2)

    def test_create_defaults_condition_threshold_none(self):
        """condition/threshold 미전달 시 None (old row 하위호환 — behavior-change-0)."""
        with self.SessionFactory() as db:
            log = crud.create_notification_log(
                db=db, user_id="u", setting_id=None, bank="kb", currency="jpy-krw",
                rate=970.0, success=True,
            )
            self.assertIsNone(log.condition)
            self.assertIsNone(log.threshold)


if __name__ == "__main__":
    unittest.main(verbosity=2)
