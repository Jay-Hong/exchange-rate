"""FCM cleanup 실패 관측이 UID/token을 새지 않고 세션을 복구하는지 검증."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import OperationalError

from app import crud, database, main
from app.notifications import alert_storage_backend, comparison_evaluator
from app.notifications.alert_storage_backend import FxCanaryBackend, SourceAlertBackend


SECRET_UID = "UID_SECRET_7f9"
SECRET_TOKEN = "FCM_SECRET_TOKEN_xyz"


def _result(*failed_tokens: str) -> dict:
    return {
        "success_count": 0,
        "failure_count": len(failed_tokens),
        "failed_tokens": list(failed_tokens),
    }


def _setting(setting_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=setting_id,
        condition="above",
        threshold=1400.0,
        repeat_interval_sec=None,
    )


def _candidate(*, source="upbit", asset="usdt-krw") -> SimpleNamespace:
    return SimpleNamespace(
        setting_id=1,
        user_id=SECRET_UID,
        source=source,
        asset=asset,
        condition="above",
        threshold=1400.0,
        repeat_interval_sec=None,
        device_tokens=(SECRET_TOKEN,),
    )


def _db_error() -> OperationalError:
    """실제 SQLAlchemy 문자열화가 bind parameter를 노출하는 오류."""
    return OperationalError(
        "DELETE FROM user_devices WHERE user_id=? AND device_token=?",
        (SECRET_UID, SECRET_TOKEN),
        RuntimeError("database unavailable"),
    )


def _assert_safe_failure_log(case, log) -> None:
    case.assertTrue(log.called, "실패 로그가 남지 않았다")
    rendered = repr(log.call_args_list)
    case.assertNotIn(SECRET_UID, rendered)
    case.assertNotIn(SECRET_TOKEN, rendered)
    for call_item in log.call_args_list:
        case.assertFalse(call_item.kwargs.get("exc_info", False))
        case.assertEqual(call_item.kwargs["extra"]["error_type"], "OperationalError")


class TestMainCleanupFailurePrivacy(unittest.IsolatedAsyncioTestCase):
    def test_fixture_would_leak_both_bind_parameters_if_stringified(self):
        rendered = str(_db_error())
        self.assertIn(SECRET_UID, rendered)
        self.assertIn(SECRET_TOKEN, rendered)

    async def test_cleanup_failure_rolls_back_without_secret_traceback(self):
        db = MagicMock()
        devices = [SimpleNamespace(device_token=SECRET_TOKEN)]
        with patch.object(main.crud, "get_devices_by_user", return_value=devices), \
             patch.object(
                 main,
                 "send_fcm_data_only",
                 AsyncMock(return_value=_result(SECRET_TOKEN)),
             ), \
             patch.object(
                 main.crud,
                 "purge_unregistered_by_owner",
                 side_effect=_db_error(),
             ), \
             patch.object(main.logger, "warning") as warning:
            await main.notify_user_devices_sync(db, SECRET_UID)

        db.rollback.assert_called_once_with()
        db.commit.assert_not_called()
        _assert_safe_failure_log(self, warning)

    async def test_cleanup_rollback_failure_is_sanitized_and_does_not_escape(self):
        db = MagicMock()
        db.rollback.side_effect = RuntimeError(f"{SECRET_UID}/{SECRET_TOKEN}")
        devices = [SimpleNamespace(device_token=SECRET_TOKEN)]
        with patch.object(main.crud, "get_devices_by_user", return_value=devices), \
             patch.object(
                 main,
                 "send_fcm_data_only",
                 AsyncMock(return_value=_result(SECRET_TOKEN)),
             ), \
             patch.object(
                 main.crud,
                 "purge_unregistered_by_owner",
                 side_effect=_db_error(),
             ), \
             patch.object(main.logger, "warning") as warning:
            await main.notify_user_devices_sync(db, SECRET_UID)

        _assert_safe_failure_log(self, warning)
        self.assertEqual(
            warning.call_args.kwargs["extra"]["rollback_error_type"],
            "RuntimeError",
        )

    async def test_outer_send_failure_omits_exception_secrets(self):
        db = MagicMock()
        devices = [SimpleNamespace(device_token=SECRET_TOKEN)]
        with patch.object(main.crud, "get_devices_by_user", return_value=devices), \
             patch.object(main, "send_fcm_data_only", AsyncMock(side_effect=_db_error())), \
             patch.object(main.logger, "warning") as warning:
            await main.notify_user_devices_sync(db, SECRET_UID)

        db.commit.assert_not_called()
        _assert_safe_failure_log(self, warning)


class TestLegacyCleanupFailurePrivacy(unittest.TestCase):
    def _common_patches(self, *, source: bool):
        item = {
            "setting": _setting(1),
            "user_id": SECRET_UID,
            "devices": [SimpleNamespace(device_token=SECRET_TOKEN)],
        }
        patches = [
            patch("app.notifications.fcm.init_firebase", return_value=True),
            patch(
                "app.notifications.fcm.send_fcm_multicast_sync",
                return_value=_result(SECRET_TOKEN),
            ),
            patch.object(
                crud,
                "purge_unregistered_by_owner",
                side_effect=_db_error(),
            ),
            patch.object(crud.logger, "error"),
        ]
        if source:
            patches.extend([
                patch.object(
                    crud,
                    "get_triggered_source_settings_for_rate",
                    return_value=[item],
                ),
                patch.object(crud, "create_source_notification_log"),
                patch(
                    "app.source_registry.get_source_definition",
                    return_value=SimpleNamespace(display_name="Upbit"),
                ),
            ])
        else:
            patches.extend([
                patch(
                    "app.notifications.comparison_evaluator.emit_comparison_observation"
                ),
                patch.object(crud, "get_triggered_settings_for_rate", return_value=[item]),
                patch.object(crud, "mark_setting_triggered"),
                patch.object(crud, "create_notification_log"),
                patch.object(crud, "_record_fx_legacy_match"),
            ])
        return patches

    def test_rate_cleanup_failure_rolls_back_without_secret_traceback(self):
        db = MagicMock()
        patches = self._common_patches(source=False)
        with patches[0], patches[1], patches[2], patches[3] as error, \
             patches[4], patches[5], patches[6], patches[7], patches[8]:
            crud.process_rate_alerts(
                db, [{"bank": "kb", "currency": "usd-krw", "rate": 1450.0}]
            )
        db.rollback.assert_called_once_with()
        _assert_safe_failure_log(self, error)

    def test_rate_rollback_failure_is_sanitized_and_does_not_escape(self):
        db = MagicMock()
        db.rollback.side_effect = RuntimeError(f"{SECRET_UID}/{SECRET_TOKEN}")
        patches = self._common_patches(source=False)
        with patches[0], patches[1], patches[2], patches[3] as error, \
             patches[4], patches[5], patches[6], patches[7], patches[8]:
            crud.process_rate_alerts(
                db, [{"bank": "kb", "currency": "usd-krw", "rate": 1450.0}]
            )
        db.rollback.assert_called_once_with()
        _assert_safe_failure_log(self, error)
        self.assertEqual(
            error.call_args.kwargs["extra"]["rollback_error_type"],
            "RuntimeError",
        )

    def test_source_cleanup_failure_rolls_back_without_secret_traceback(self):
        db = MagicMock()
        patches = self._common_patches(source=True)
        with patches[0], patches[1], patches[2], patches[3] as error, \
             patches[4], patches[5], patches[6]:
            crud.process_source_rate_alerts(
                db,
                [{"source": "upbit", "asset": "usdt-krw", "rate": 1450.0}],
            )
        db.rollback.assert_called_once_with()
        _assert_safe_failure_log(self, error)

    def test_source_rollback_failure_is_sanitized_and_does_not_escape(self):
        db = MagicMock()
        db.rollback.side_effect = RuntimeError(f"{SECRET_UID}/{SECRET_TOKEN}")
        patches = self._common_patches(source=True)
        with patches[0], patches[1], patches[2], patches[3] as error, \
             patches[4], patches[5], patches[6]:
            crud.process_source_rate_alerts(
                db,
                [{"source": "upbit", "asset": "usdt-krw", "rate": 1450.0}],
            )
        _assert_safe_failure_log(self, error)
        self.assertEqual(
            error.call_args.kwargs["extra"]["rollback_error_type"],
            "RuntimeError",
        )

    def test_rate_item_failure_omits_exception_secrets(self):
        db = MagicMock()
        with patch(
            "app.notifications.comparison_evaluator.emit_comparison_observation"
        ), patch(
            "app.notifications.fcm.init_firebase", return_value=True
        ), patch.object(
            crud, "get_triggered_settings_for_rate", side_effect=_db_error()
        ), patch.object(crud.logger, "error") as error:
            crud.process_rate_alerts(
                db, [{"bank": "kb", "currency": "usd-krw", "rate": 1450.0}]
            )
        _assert_safe_failure_log(self, error)

    def test_source_item_failure_omits_exception_secrets(self):
        db = MagicMock()
        with patch(
            "app.notifications.fcm.init_firebase", return_value=True
        ), patch.object(
            crud, "get_triggered_source_settings_for_rate", side_effect=_db_error()
        ), patch.object(crud.logger, "error") as error:
            crud.process_source_rate_alerts(
                db,
                [{"source": "upbit", "asset": "usdt-krw", "rate": 1450.0}],
            )
        _assert_safe_failure_log(self, error)


class TestBackendCleanupFailurePrivacy(unittest.TestCase):
    def _run_backend(self, backend, *, candidate, rollback_fails=False):
        db = MagicMock()
        if rollback_fails:
            db.rollback.side_effect = RuntimeError(f"{SECRET_UID}/{SECRET_TOKEN}")
        ctx = MagicMock()
        ctx.__enter__.return_value = db
        ctx.__exit__.return_value = None
        with patch("app.database.get_db_context", return_value=ctx), \
             patch.object(crud, "create_source_notification_log"), \
             patch.object(
                 crud,
                 "purge_unregistered_devices",
                 side_effect=_db_error(),
             ), \
             patch.object(alert_storage_backend.logger, "error") as error:
            backend.persist_result(candidate, 1450.0, _result(SECRET_TOKEN))
        return db, error

    def test_source_backend_rolls_back_without_secret_traceback(self):
        db, error = self._run_backend(SourceAlertBackend(), candidate=_candidate())
        db.rollback.assert_called_once_with()
        _assert_safe_failure_log(self, error)

    def test_fx_backend_rolls_back_without_secret_traceback(self):
        db, error = self._run_backend(
            FxCanaryBackend(), candidate=_candidate(source="kb", asset="usd-krw")
        )
        db.rollback.assert_called_once_with()
        _assert_safe_failure_log(self, error)

    def test_backend_rollback_failures_are_sanitized_and_do_not_escape(self):
        for backend, candidate in (
            (SourceAlertBackend(), _candidate()),
            (FxCanaryBackend(), _candidate(source="kb", asset="usd-krw")),
        ):
            with self.subTest(backend=type(backend).__name__):
                _, error = self._run_backend(
                    backend, candidate=candidate, rollback_fails=True
                )
                _assert_safe_failure_log(self, error)
                self.assertEqual(
                    error.call_args.kwargs["extra"]["rollback_error_type"],
                    "RuntimeError",
                )

    def test_zero_delete_is_not_logged_as_deleted(self):
        db = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = db
        ctx.__exit__.return_value = None
        with patch("app.database.get_db_context", return_value=ctx), \
             patch.object(crud, "create_source_notification_log"), \
             patch.object(crud, "purge_unregistered_devices", return_value=0), \
             patch.object(alert_storage_backend.logger, "info") as info:
            SourceAlertBackend().persist_result(
                _candidate(), 1450.0, _result(SECRET_TOKEN)
            )
        cleanup_log = next(
            item
            for item in info.call_args_list
            if "정리 결과" in item.args[0]
        )
        self.assertEqual(cleanup_log.kwargs["extra"]["deleted_count"], 0)
        self.assertEqual(
            cleanup_log.kwargs["extra"]["outcome"], "not_present_or_rebound"
        )

    def test_commit_failure_never_emits_cleanup_success(self):
        db = MagicMock()
        db.commit.side_effect = _db_error()
        ctx = MagicMock()
        ctx.__enter__.return_value = db
        ctx.__exit__.return_value = None
        with patch("app.database.get_db_context", return_value=ctx), \
             patch.object(crud, "create_source_notification_log"), \
             patch.object(crud, "purge_unregistered_devices", return_value=1), \
             patch.object(alert_storage_backend.logger, "info") as info, \
             patch.object(alert_storage_backend.logger, "error") as error:
            SourceAlertBackend().persist_result(
                _candidate(), 1450.0, _result(SECRET_TOKEN)
            )
        self.assertFalse(
            any("정리 결과" in item.args[0] for item in info.call_args_list),
            "commit 전에 cleanup 성공 로그가 기록됐다",
        )
        db.rollback.assert_called_once_with()
        _assert_safe_failure_log(self, error)


class TestComparisonFailurePrivacy(unittest.IsolatedAsyncioTestCase):
    async def test_evaluator_isolation_log_omits_exception_secrets(self):
        evaluator = comparison_evaluator.ComparisonAlertEvaluator()
        with patch.object(
            evaluator,
            "_get_candidates",
            AsyncMock(side_effect=_db_error()),
        ), patch.object(comparison_evaluator.logger, "error") as error:
            await evaluator._evaluate_async("kb", "usd-krw")
        _assert_safe_failure_log(self, error)


class TestRegistrationFailurePrivacy(unittest.IsolatedAsyncioTestCase):
    def test_application_engine_hides_sql_bind_parameters(self):
        self.assertTrue(database.engine.hide_parameters)
        hidden = OperationalError(
            "INSERT INTO user_devices(user_id, device_token) VALUES (?, ?)",
            (SECRET_UID, SECRET_TOKEN),
            RuntimeError("database unavailable"),
            hide_parameters=database.engine.hide_parameters,
        )
        rendered = str(hidden)
        self.assertNotIn(SECRET_UID, rendered)
        self.assertNotIn(SECRET_TOKEN, rendered)
        self.assertIn("SQL parameters hidden", rendered)
        self.assertIn("INSERT INTO user_devices", rendered)

    def test_crud_registration_failure_uses_safe_rollback_and_type_only_log(self):
        db = MagicMock()
        db.get_bind.return_value.dialect.name = "sqlite"
        db.query.return_value.filter.return_value.first.return_value = None
        db.execute.side_effect = _db_error()

        with patch.object(crud.logger, "error") as error:
            with self.assertRaises(OperationalError):
                crud.register_device(
                    db,
                    user_id=SECRET_UID,
                    device_token=SECRET_TOKEN,
                    platform="ios",
                )

        db.rollback.assert_called_once_with()
        _assert_safe_failure_log(self, error)

    def test_crud_registration_rollback_failure_is_sanitized(self):
        db = MagicMock()
        db.get_bind.return_value.dialect.name = "sqlite"
        db.query.return_value.filter.return_value.first.return_value = None
        db.execute.side_effect = _db_error()
        db.rollback.side_effect = RuntimeError(f"{SECRET_UID}/{SECRET_TOKEN}")

        with patch.object(crud.logger, "error") as error:
            with self.assertRaises(OperationalError):
                crud.register_device(
                    db,
                    user_id=SECRET_UID,
                    device_token=SECRET_TOKEN,
                    platform="android",
                )

        _assert_safe_failure_log(self, error)
        self.assertEqual(
            error.call_args.kwargs["extra"]["rollback_error_type"],
            "RuntimeError",
        )

    async def test_endpoint_severs_sql_exception_context_and_uses_safe_detail(self):
        body = SimpleNamespace(device_token=SECRET_TOKEN, platform="ios")
        with patch.object(
            main, "verify_firebase_token", AsyncMock(return_value=SECRET_UID)
        ), patch.object(
            main.crud, "register_device", side_effect=_db_error()
        ), patch.object(main.logger, "error") as error:
            with self.assertRaises(main.HTTPException) as raised:
                await main.register_device(MagicMock(), body, MagicMock())

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "Device registration failed")
        self.assertNotIn(SECRET_UID, str(raised.exception.detail))
        self.assertNotIn(SECRET_TOKEN, str(raised.exception.detail))
        self.assertIsNone(raised.exception.__context__)
        _assert_safe_failure_log(self, error)


if __name__ == "__main__":
    unittest.main()
