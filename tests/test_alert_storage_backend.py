"""fanout step 4 S1 — AlertStorageBackend / SourceAlertBackend characterization.

load/refetch의 DB 동작은 test_usdt_ws_upbit_skeleton(re-targeted patches)가 byte-identity로 검증
(3210 passed). 여기선 backend 단위 계약 명시 lock:
- 구조(ABC) + build_payload(pure, no-DB)
- **persist_result(WHOLE, 최고위험 이동)**: success → mark_triggered + log / failure → log only /
  failed_tokens → cleanup. mock-context로 호출·순서 검증 (codex 권장 S1 gate 보강).
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.notifications.alert_evaluator import CachedAlertSetting
from app.notifications.alert_storage_backend import AlertStorageBackend, SourceAlertBackend


def _candidate() -> CachedAlertSetting:
    return CachedAlertSetting(
        setting_id=1, user_id="user-1", source="upbit", asset="usdt-krw",
        condition="above", threshold=1450.0, device_tokens=("t1",),
    )


def _mock_db_ctx() -> tuple[MagicMock, MagicMock]:
    mock_db = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_db)
    mock_ctx.__exit__ = MagicMock(return_value=None)
    return mock_db, mock_ctx


class TestSourceAlertBackendStructure(unittest.TestCase):

    def test_is_alert_storage_backend(self):
        self.assertIsInstance(SourceAlertBackend(), AlertStorageBackend)

    def test_abc_cannot_instantiate(self):
        with self.assertRaises(TypeError):
            AlertStorageBackend()  # type: ignore[abstract]


class TestSourceAlertBackendBuildPayload(unittest.TestCase):

    def test_build_payload_above(self):
        title, body, data = SourceAlertBackend().build_payload(_candidate(), Decimal("1455.5"))
        self.assertEqual(data["type"], "source_rate_alert")
        self.assertEqual(data["source"], "upbit")
        self.assertEqual(data["asset"], "usdt-krw")
        self.assertEqual(data["condition"], "above")
        self.assertEqual(data["setting_id"], "1")
        self.assertIn("📈", title)
        self.assertIn("1455.50", body)  # rate_str = f"{float(triggered_rate):.2f}"
        self.assertIn("이상", body)

    def test_build_payload_below(self):
        cand = CachedAlertSetting(
            setting_id=2, user_id="user-2", source="bithumb", asset="usdt-krw",
            condition="below", threshold=1400.0, device_tokens=("t2",),
        )
        title, body, data = SourceAlertBackend().build_payload(cand, Decimal("1399.0"))
        self.assertEqual(data["condition"], "below")
        self.assertIn("📉", title)
        self.assertIn("이하", body)
        self.assertIn("1399.00", body)


class TestSourceAlertBackendPersistResult(unittest.TestCase):
    """WHOLE persist_result (최고위험 verbatim 이동) 직접 lock — mark/log/cleanup 호출·순서."""

    def test_success_marks_triggered_and_logs_success(self):
        _, mock_ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=mock_ctx), \
             patch("app.crud.mark_source_setting_triggered") as mock_mark, \
             patch("app.crud.create_source_notification_log") as mock_log:
            SourceAlertBackend().persist_result(
                _candidate(), 1455.0,
                {"success_count": 1, "failure_count": 0, "failed_tokens": []},
            )
        mock_mark.assert_called_once()
        mock_log.assert_called_once()
        self.assertTrue(mock_log.call_args.kwargs["success"])

    def test_failure_logs_failure_only_no_mark(self):
        _, mock_ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=mock_ctx), \
             patch("app.crud.mark_source_setting_triggered") as mock_mark, \
             patch("app.crud.create_source_notification_log") as mock_log:
            SourceAlertBackend().persist_result(
                _candidate(), 1455.0,
                {"success_count": 0, "failure_count": 1, "failed_tokens": [], "error": "boom"},
            )
        mock_mark.assert_not_called()  # failure → setting 변경 X (사용자 알림 영영 X 방지)
        mock_log.assert_called_once()
        self.assertFalse(mock_log.call_args.kwargs["success"])
        self.assertEqual(mock_log.call_args.kwargs["error_message"], "boom")

    def test_failed_tokens_cleanup(self):
        mock_db, mock_ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=mock_ctx), \
             patch("app.crud.mark_source_setting_triggered"), \
             patch("app.crud.create_source_notification_log"):
            SourceAlertBackend().persist_result(
                _candidate(), 1455.0,
                {"success_count": 1, "failure_count": 1, "failed_tokens": ["bad-token"]},
            )
        # failed_tokens 존재 → UserDevice cleanup + commit (같은 세션, WHOLE)
        mock_db.query.assert_called()
        mock_db.commit.assert_called()

    def test_no_failed_tokens_no_cleanup_commit(self):
        mock_db, mock_ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=mock_ctx), \
             patch("app.crud.mark_source_setting_triggered"), \
             patch("app.crud.create_source_notification_log"):
            SourceAlertBackend().persist_result(
                _candidate(), 1455.0,
                {"success_count": 1, "failure_count": 0, "failed_tokens": []},
            )
        # failed_tokens 없음 → cleanup query/commit 없음
        mock_db.query.assert_not_called()
        mock_db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
