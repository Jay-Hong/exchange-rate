"""fanout step 4 S1/S2 — AlertStorageBackend / SourceAlertBackend / FxNotificationBackend characterization.

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
from app.notifications.alert_storage_backend import (
    AlertStorageBackend,
    FxNotificationBackend,
    SourceAlertBackend,
)


def _candidate() -> CachedAlertSetting:
    return CachedAlertSetting(
        setting_id=1, user_id="user-1", source="upbit", asset="usdt-krw",
        condition="above", threshold=1450.0, device_tokens=("t1",),
    )


def _assert_owner_fenced_delete(case, mock_db, *, uid: str, tokens: tuple) -> None:
    """삭제가 **user_id fence + 보낸 토큰 교집합** 두 조건으로 걸렸는지 단언한다.

    `mock_db.query.assert_called()` 만으로는 token-only DELETE 회귀를 잡지 못한다
    (구 코드도 query 를 호출했다). 실제 WHERE 절을 컴파일해 확인한다.
    """
    mock_db.query.assert_called()
    filter_calls = mock_db.query.return_value.filter.call_args_list
    case.assertTrue(filter_calls, "filter() 가 호출되지 않았다")
    clauses = [
        str(c.compile(compile_kwargs={"literal_binds": True}))
        for c in filter_calls[-1].args
    ]
    case.assertTrue(
        any(f"user_id = '{uid}'" in c for c in clauses),
        f"user_id fence 없음: {clauses}",
    )
    token_sql = ", ".join(repr(t) for t in sorted(tokens))
    case.assertTrue(
        any(f"device_token IN ({token_sql})" in c for c in clauses),
        f"토큰 교집합 불일치: {clauses}",
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
        self.assertIn("업비트", title)          # display_name
        self.assertIn("테더", title)            # asset 표시명 (usdt-krw → 테더)
        self.assertIn("1455.5", title)          # 현재가는 title 마지막 (trailing zero 제거)
        self.assertEqual(body, "[ 1450 ↑이상 도달 ]")   # body=목표/조건만

    def test_build_payload_below(self):
        cand = CachedAlertSetting(
            setting_id=2, user_id="user-2", source="bithumb", asset="usdt-krw",
            condition="below", threshold=1400.0, device_tokens=("t2",),
        )
        title, body, data = SourceAlertBackend().build_payload(cand, Decimal("1399.0"))
        self.assertEqual(data["condition"], "below")
        self.assertIn("📉", title)
        self.assertIn("1399", title)            # 현재가 title (1399.00 → 1399)
        self.assertEqual(body, "[ 1400 ↓이하 도달 ]")

    def test_build_payload_wide_gap_nbsp(self):
        # 사용자 2026-07-09: 컬럼 사이는 NBSP 2개(합쳐지지 않는 넓은 간격), 이모지 뒤는 일반 1칸.
        from app.crud import WIDE_GAP
        title, _, _ = SourceAlertBackend().build_payload(_candidate(), Decimal("1455.5"))
        self.assertIn(WIDE_GAP, title)               # 컬럼 사이 NBSP 존재
        self.assertTrue(title.startswith("📈 "))     # 이모지 뒤 일반 스페이스 1칸
        self.assertNotIn("  ", title)                # 일반 스페이스 2연속 없음(NBSP로 대체)

    def test_build_payload_krx_uses_display_name_no_asset(self):
        # ADR-038 ③ + 문구 정리: KRX title = "📈  달러선물  {현재가}" (asset 'USD-KRW-FUTURES' 미표시)
        cand = CachedAlertSetting(
            setting_id=7, user_id="u7", source="krx", asset="usd-krw-futures",
            condition="above", threshold=1500.0, device_tokens=("t7",),
        )
        title, body, _ = SourceAlertBackend().build_payload(cand, Decimal("1505.3"))
        self.assertIn("달러선물", title)             # display_name (구 '미국달러F' 아님)
        self.assertNotIn("미국달러F", title)
        self.assertNotIn("USD-KRW-FUTURES", title)   # asset 미표시
        self.assertIn("1505.3", title)
        self.assertEqual(body, "[ 1500 ↑이상 도달 ]")

    def test_build_payload_is_repeat_false_for_once(self):
        # B2 (ADR-036) payload-flag: repeat_interval_sec None(once) → "false" (str, FCM data 호환)
        _, _, data = SourceAlertBackend().build_payload(_candidate(), Decimal("1455.5"))
        self.assertEqual(data["is_repeat"], "false")

    def test_build_payload_is_repeat_true_for_repeat(self):
        cand = CachedAlertSetting(
            setting_id=3, user_id="user-3", source="upbit", asset="usdt-krw",
            condition="above", threshold=1450.0, device_tokens=("t3",),
            repeat_interval_sec=300,
        )
        _, _, data = SourceAlertBackend().build_payload(cand, Decimal("1455.5"))
        self.assertEqual(data["is_repeat"], "true")


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
                # 보낸 토큰(device_tokens=("t1",)) 안의 것이어야 fence 를 통과한다
                {"success_count": 1, "failure_count": 1, "failed_tokens": ["t1"]},
            )
        # failed_tokens 존재 → UserDevice cleanup + commit (같은 세션, WHOLE)
        _assert_owner_fenced_delete(self, mock_db, uid="user-1", tokens=("t1",))
        mock_db.commit.assert_called()

    def test_out_of_sent_failed_token_is_not_deleted(self):
        """보낸 적 없는 토큰이 failed_tokens 로 와도 삭제하지 않는다 (fail-closed)."""
        mock_db, mock_ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=mock_ctx), \
             patch("app.crud.mark_source_setting_triggered"), \
             patch("app.crud.create_source_notification_log"):
            SourceAlertBackend().persist_result(
                _candidate(), 1455.0,
                {"success_count": 1, "failure_count": 1, "failed_tokens": ["never-sent"]},
            )
        self.assertEqual(
            mock_db.query.return_value.filter.return_value.delete.call_count, 0,
            "sent_tokens 밖 토큰이 삭제됐다",
        )

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


class TestFxNotificationBackend(unittest.TestCase):
    """FX(notification_settings) backend (S2, dead code) — value pass-through / legacy payload / persist no-op."""

    def _fx_candidate(self) -> CachedAlertSetting:
        return CachedAlertSetting(
            setting_id=5, user_id="user-1", source="kb", asset="usd-krw",
            condition="above", threshold=1450.0, device_tokens=("t1",),
        )

    def test_is_alert_storage_backend(self):
        self.assertIsInstance(FxNotificationBackend(), AlertStorageBackend)

    def test_build_payload_legacy_rate_alert_format(self):
        title, body, data = FxNotificationBackend().build_payload(self._fx_candidate(), Decimal("1455.5"))
        self.assertEqual(data["type"], "rate_alert")     # legacy type (source_rate_alert 아님)
        self.assertEqual(data["bank"], "kb")             # bank/currency 키 (source/asset 아님)
        self.assertEqual(data["currency"], "usd-krw")
        self.assertNotIn("source", data)
        self.assertNotIn("asset", data)
        self.assertIn("📈", title)
        self.assertIn("국민은행", title)                  # BANK_NAMES_KR 적용
        self.assertIn("1455.5", title)                    # 현재가 title 마지막
        self.assertEqual(body, "[ 1450 ↑이상 도달 ]")     # body=목표/조건만
        self.assertEqual(data["is_repeat"], "false")     # B2 (ADR-036): once 기본

    def test_build_payload_is_repeat_true(self):
        # B2 (ADR-036) payload-flag: FX backend도 candidate.repeat_interval_sec → is_repeat
        cand = CachedAlertSetting(
            setting_id=9, user_id="user-9", source="hana", asset="usd-krw",
            condition="above", threshold=1500.0, device_tokens=("t9",),
            repeat_interval_sec=600,
        )
        _, _, data = FxNotificationBackend().build_payload(cand, Decimal("1501.0"))
        self.assertEqual(data["is_repeat"], "true")

    def test_persist_result_is_noop(self):
        with patch("app.database.get_db_context") as mock_ctx, \
             patch("app.crud.mark_setting_triggered") as mock_mark, \
             patch("app.crud.create_notification_log") as mock_log:
            FxNotificationBackend().persist_result(
                self._fx_candidate(), 1455.0,
                {"success_count": 1, "failure_count": 0, "failed_tokens": ["bad"]},
            )
        # shadow no-op: DB 세션·mark·log·cleanup 전부 미호출 (failed_tokens 있어도)
        mock_ctx.assert_not_called()
        mock_mark.assert_not_called()
        mock_log.assert_not_called()

    def test_load_settings_value_passthrough(self):
        # notification_settings.bank/currency → CachedAlertSetting.source/asset (value pass-through)
        mock_setting = MagicMock(
            id=1, user_id="user-A", bank="kb", currency="usd-krw",
            condition="above", threshold=1450.0, enabled=True, triggered=False,
        )
        mock_device = MagicMock(user_id="user-A", device_token="token-A")
        query_results = [[mock_setting], [mock_device]]
        query_idx = [0]

        def make_query(model):
            q = MagicMock()
            q.filter.return_value = q
            q.all.return_value = query_results[query_idx[0]]
            query_idx[0] += 1
            return q

        mock_session = MagicMock()
        mock_session.query.side_effect = make_query
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.database.get_db_context", return_value=mock_ctx):
            result = FxNotificationBackend().load_settings("kb", "usd-krw")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source, "kb")        # bank → source
        self.assertEqual(result[0].asset, "usd-krw")    # currency → asset
        self.assertEqual(result[0].device_tokens, ("token-A",))

    def test_refetch_snapshot_carries_repeat_fields(self):
        # B2 (ADR-036): FX backend refetch가 repeat_interval_sec/last_notified_at을 snapshot에 실어야
        # delivery_allowed가 FX(shadow/canary) 경로에서도 interval throttle 적용 (누락 시 repeat→once 오인).
        import datetime as _dt
        last = _dt.datetime(2026, 6, 29, 1, 2, 3)
        mock_setting = MagicMock(
            id=7, user_id="user-B", bank="hana", currency="usd-krw",
            condition="above", threshold=1500.0, enabled=True, triggered=False,
            repeat_interval_sec=300, last_notified_at=last,
        )
        with patch("app.crud.get_notification_setting_by_id", return_value=mock_setting), \
             patch("app.database.get_db_context") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.return_value.__exit__ = MagicMock(return_value=None)
            snap = FxNotificationBackend().refetch_snapshot(setting_id=7, user_id="user-B")

        self.assertIsNotNone(snap)
        self.assertEqual(snap.repeat_interval_sec, 300)
        self.assertEqual(snap.last_notified_at, last)


if __name__ == "__main__":
    unittest.main()
