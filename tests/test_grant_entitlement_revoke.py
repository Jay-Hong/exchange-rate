"""grant_entitlement.py cmd_revoke — KRX 알림 자동 disable 범위 잠금.

ADR-038 D4 비교알림 확장 (2026-07-10, codex): revoke disable 대상이
signed(김프 counter, right=krx)뿐 아니라 **usd absolute 달러선물 비교**
(canonical 정렬로 krx가 left/right 어느 쪽이든 저장)까지 커버해야 함 —
구 signed+right 한정이면 회수 후 absolute KRX 비교알림이 계속 발사.

conftest.py가 DATABASE_URL=sqlite로 격리 — get_db_context는 그 엔진 사용.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import models
from app.database import SessionLocal, engine, get_db_context  # noqa: F401

import grant_entitlement  # scripts/grant_entitlement.py

models.Base.metadata.create_all(engine)

_UID = "revoke-test-user"


def _mk_cmp(db, **over):
    defaults = dict(
        user_id=_UID, tab="tether",
        left_source="bithumb", left_asset="usdt-krw",
        right_source="krx", right_asset="usd-krw-futures",
        diff_type="signed", operator="lte", threshold=-20.0,
        enabled=True, triggered=False,
    )
    defaults.update(over)
    row = models.ComparisonAlert(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestCmdRevokeDisableScope(unittest.TestCase):

    def setUp(self):
        self.db = SessionLocal()
        self._cleanup()
        # entitlement 부여 상태에서 시작
        self.db.add(models.UserEntitlement(user_id=_UID, key="krx_futures"))
        self.db.commit()

    def tearDown(self):
        self._cleanup()
        self.db.close()

    def _cleanup(self):
        self.db.query(models.ComparisonAlert).filter(
            models.ComparisonAlert.user_id == _UID).delete()
        self.db.query(models.SourceNotificationSetting).filter(
            models.SourceNotificationSetting.user_id == _UID).delete()
        self.db.query(models.UserEntitlement).filter(
            models.UserEntitlement.user_id == _UID).delete()
        self.db.commit()

    def test_revoke_disables_signed_right_and_absolute_both_sides(self):
        # 1) signed 김프 counter (right=krx) — 기존 대상
        kimchi = _mk_cmp(self.db)
        # 2) usd absolute, krx가 right (canonical: hana < krx)
        abs_right = _mk_cmp(self.db, tab="usd", diff_type="absolute", operator="gte",
                            threshold=5.0, left_source="hana", left_asset="usd-krw",
                            right_source="krx", right_asset="usd-krw-futures")
        # 3) usd absolute, krx가 left (canonical: krx < shinhan)
        abs_left = _mk_cmp(self.db, tab="usd", diff_type="absolute", operator="gte",
                           threshold=3.0, left_source="krx", left_asset="usd-krw-futures",
                           right_source="shinhan", right_asset="usd-krw")
        # 4) krx 무관 비교 (은행끼리) — disable 대상 아님
        plain = _mk_cmp(self.db, tab="usd", diff_type="absolute", operator="gte",
                        threshold=2.0, left_source="investing", left_asset="usd-krw",
                        right_source="kb", right_asset="usd-krw")

        grant_entitlement.cmd_revoke(_UID, "krx_futures", write=True)

        db2 = SessionLocal()
        try:
            def enabled(row_id):
                return db2.get(models.ComparisonAlert, row_id).enabled
            self.assertFalse(enabled(kimchi.id), "signed right=krx disable")
            self.assertFalse(enabled(abs_right.id), "absolute right=krx disable")
            self.assertFalse(enabled(abs_left.id), "absolute left=krx disable (구 쿼리는 누락)")
            self.assertTrue(enabled(plain.id), "krx 무관 비교는 유지")
            # entitlement 삭제 확인
            ent = db2.query(models.UserEntitlement).filter(
                models.UserEntitlement.user_id == _UID).first()
            self.assertIsNone(ent)
        finally:
            db2.close()

    def test_dry_run_changes_nothing(self):
        alert = _mk_cmp(self.db, tab="usd", diff_type="absolute", operator="gte",
                        threshold=3.0, left_source="krx", left_asset="usd-krw-futures",
                        right_source="woori", right_asset="usd-krw")
        grant_entitlement.cmd_revoke(_UID, "krx_futures", write=False)
        db2 = SessionLocal()
        try:
            self.assertTrue(db2.get(models.ComparisonAlert, alert.id).enabled)
            self.assertIsNotNone(db2.query(models.UserEntitlement).filter(
                models.UserEntitlement.user_id == _UID).first())
        finally:
            db2.close()


if __name__ == "__main__":
    unittest.main()
