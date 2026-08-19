"""DELETE /api/user/me가 모든 user_id 소유 데이터를 원자적으로 삭제하는지 검증."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.main import (
    _ACCOUNT_DELETION_TARGETS,
    delete_user_account,
)


class TestAccountDeletion(unittest.IsolatedAsyncioTestCase):
    TARGET = "delete-user"
    OTHER = "keep-user"

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self._seed_user(self.TARGET, "target")
        self._seed_user(self.OTHER, "other")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _seed_user(self, user_id: str, suffix: str) -> None:
        self.db.add_all([
            models.UserDevice(
                user_id=user_id, device_token=f"token-{suffix}", platform="ios"),
            models.NotificationSetting(
                user_id=user_id, bank="hana", currency="usd-krw",
                condition="above", threshold=1400.0),
            models.NotificationLog(
                user_id=user_id, setting_id=None, bank="hana", currency="usd-krw",
                rate=1401.0, condition="above", threshold=1400.0),
            models.SourceNotificationSetting(
                user_id=user_id, source="upbit", asset="usdt-krw",
                condition="above", threshold=1400.0),
            models.SourceNotificationLog(
                user_id=user_id, setting_id=None, source="upbit", asset="usdt-krw",
                condition="above", threshold=1400.0, triggered_rate=1401.0),
            models.ComparisonAlert(
                user_id=user_id, tab="tether",
                left_source="upbit", left_asset="usdt-krw",
                right_source="bithumb", right_asset="usdt-krw",
                diff_type="absolute", operator="gte", threshold=2.0),
            models.ComparisonNotificationLog(
                user_id=user_id, setting_id=None, tab="tether",
                left_source="upbit", left_asset="usdt-krw",
                right_source="bithumb", right_asset="usdt-krw",
                diff_type="absolute", operator="gte", threshold=2.0,
                left_rate=1402.0, right_rate=1400.0, spread=2.0),
            models.UserEntitlement(user_id=user_id, key="krx_futures"),
        ])

    def _count(self, model, user_id: str) -> int:
        return self.db.query(model).filter(model.user_id == user_id).count()

    async def _delete(self):
        request = MagicMock()
        auth = AsyncMock(return_value=self.TARGET)
        with patch("app.main.verify_firebase_token", new=auth):
            response = await delete_user_account(request, self.db)
        auth.assert_awaited_once_with(request, check_revoked=True)
        return response

    def test_target_list_covers_every_user_id_model(self):
        """새 user_id 테이블이 생기면 삭제 목록 갱신 누락을 즉시 탐지한다."""
        self.assertEqual(
            tuple(name for name, _ in _ACCOUNT_DELETION_TARGETS),
            ("comparison_logs", "comparison_alerts", "source_logs", "source_settings",
             "logs", "settings", "devices", "entitlements"),
        )
        configured = {model for _, model in _ACCOUNT_DELETION_TARGETS}
        user_owned = {
            mapper.class_ for mapper in models.Base.registry.mappers
            if "user_id" in mapper.local_table.c
        }
        self.assertEqual(configured, user_owned)
        self.assertEqual(len(_ACCOUNT_DELETION_TARGETS), 8)

    async def test_deletes_all_target_rows_and_preserves_other_user(self):
        response = await self._delete()
        self.assertEqual(response.status_code, 204)

        for _, model in _ACCOUNT_DELETION_TARGETS:
            with self.subTest(table=model.__tablename__):
                self.assertEqual(self._count(model, self.TARGET), 0)
                self.assertEqual(self._count(model, self.OTHER), 1)

        second = await self._delete()
        self.assertEqual(second.status_code, 204)
        for _, model in _ACCOUNT_DELETION_TARGETS:
            with self.subTest(second_delete_table=model.__tablename__):
                self.assertEqual(self._count(model, self.TARGET), 0)
                self.assertEqual(self._count(model, self.OTHER), 1)

    async def test_commit_failure_rolls_back_every_table(self):
        with patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaises(HTTPException) as raised:
                await self._delete()

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "Failed to delete user data")
        self.db.expire_all()
        for _, model in _ACCOUNT_DELETION_TARGETS:
            with self.subTest(table=model.__tablename__):
                self.assertEqual(self._count(model, self.TARGET), 1)
                self.assertEqual(self._count(model, self.OTHER), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
