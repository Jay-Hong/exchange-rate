"""FCM cleanup 호출부가 발송 시점 UID·token을 손실 없이 넘기는지 검증.

helper 단위 테스트만으로는 caller가 마지막 UID, 전체 token 목록,
또는 다른 500개 batch를 넘기는 회귀를 잡지 못한다. 실제 production
caller를 태워 loop-local provenance와 기존 commit 경계를 잠근다.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from app import crud, main
from app.notifications.comparison_evaluator import (
    ComparisonAlertEvaluator,
    ComparisonCandidate,
    FreshComparisonSnapshot,
    UnifiedRate,
)


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


class TestNotifyUserDevicesCleanupWiring(unittest.IsolatedAsyncioTestCase):
    async def test_each_500_token_batch_keeps_its_own_sent_set(self):
        uid = "user-main"
        tokens = [f"t{i}" for i in range(501)]
        devices = [SimpleNamespace(device_token=token) for token in tokens]

        # 첫 batch에서 t500, 둘째 batch에서 t0은 보낸 적 없는 값이다.
        # 각 batch 교집을 먼저 계산해야 t0·t500이 하나씩만 자격을 얻는다.
        sender = AsyncMock(
            side_effect=[_result("t0", "t500"), _result("t500", "t0")]
        )
        db = MagicMock()
        real_accumulate = crud.accumulate_unregistered_by_owner

        with patch.object(main.crud, "get_devices_by_user", return_value=devices), \
             patch.object(main, "send_fcm_data_only", sender), \
             patch.object(
                 main.crud,
                 "accumulate_unregistered_by_owner",
                 wraps=real_accumulate,
             ) as accumulate, \
             patch.object(
                 main.crud, "purge_unregistered_by_owner", return_value=2
             ) as purge:
            await main.notify_user_devices_sync(db, uid)

        self.assertEqual(sender.await_count, 2)
        self.assertEqual(sender.await_args_list[0].kwargs["tokens"], tokens[:500])
        self.assertEqual(sender.await_args_list[1].kwargs["tokens"], tokens[500:])
        self.assertEqual(
            [c.kwargs["owner_uid"] for c in accumulate.call_args_list],
            [uid, uid],
        )
        self.assertEqual(
            [c.kwargs["sent_tokens"] for c in accumulate.call_args_list],
            [tokens[:500], tokens[500:]],
        )
        self.assertEqual(
            [c.kwargs["unregistered_tokens"] for c in accumulate.call_args_list],
            [["t0", "t500"], ["t500", "t0"]],
        )
        purge.assert_called_once_with(db, {uid: {"t0", "t500"}})
        db.commit.assert_called_once_with()


class TestLegacyAlertCleanupWiring(unittest.TestCase):
    def test_rate_alert_loop_preserves_each_owner_and_sent_tokens(self):
        db = MagicMock()
        items = [
            {
                "setting": _setting(1),
                "user_id": "A",
                "devices": [
                    SimpleNamespace(device_token="a1"),
                    SimpleNamespace(device_token="a2"),
                ],
            },
            {
                "setting": _setting(2),
                "user_id": "B",
                "devices": [
                    SimpleNamespace(device_token="b1"),
                    SimpleNamespace(device_token="b2"),
                ],
            },
        ]
        real_accumulate = crud.accumulate_unregistered_by_owner

        with patch(
            "app.notifications.comparison_evaluator.emit_comparison_observation"
        ), patch(
            "app.notifications.fcm.init_firebase", return_value=True
        ), patch(
            "app.notifications.fcm.send_fcm_multicast_sync",
            side_effect=[_result("a2"), _result("b1")],
        ) as sender, patch.object(
            crud, "get_triggered_settings_for_rate", return_value=items
        ), patch.object(
            crud, "accumulate_unregistered_by_owner", wraps=real_accumulate
        ) as accumulate, patch.object(
            crud, "purge_unregistered_by_owner", return_value=2
        ) as purge, patch.object(
            crud, "mark_setting_triggered"
        ), patch.object(
            crud, "create_notification_log"
        ), patch.object(
            crud, "_record_fx_legacy_match"
        ):
            sent = crud.process_rate_alerts(
                db,
                [{"bank": "kb", "currency": "usd-krw", "rate": 1450.0}],
            )

        self.assertEqual(sent, 0)
        self.assertEqual(
            [c.args[0] for c in sender.call_args_list],
            [["a1", "a2"], ["b1", "b2"]],
            "실제 sender의 토큰 집합이 cleanup provenance와 갈라졌다",
        )
        self.assertEqual(
            [
                (
                    c.kwargs["owner_uid"],
                    c.kwargs["sent_tokens"],
                    c.kwargs["unregistered_tokens"],
                )
                for c in accumulate.call_args_list
            ],
            [
                ("A", ["a1", "a2"], ["a2"]),
                ("B", ["b1", "b2"], ["b1"]),
            ],
        )
        purge.assert_called_once_with(db, {"A": {"a2"}, "B": {"b1"}})
        db.commit.assert_called_once_with()

    def test_source_alert_loop_preserves_each_owner_and_sent_tokens(self):
        db = MagicMock()
        items = [
            {
                "setting": _setting(11),
                "user_id": "A",
                "devices": [
                    SimpleNamespace(device_token="a1"),
                    SimpleNamespace(device_token="a2"),
                ],
            },
            {
                "setting": _setting(12),
                "user_id": "B",
                "devices": [
                    SimpleNamespace(device_token="b1"),
                    SimpleNamespace(device_token="b2"),
                ],
            },
        ]
        real_accumulate = crud.accumulate_unregistered_by_owner

        with patch(
            "app.notifications.fcm.init_firebase", return_value=True
        ), patch(
            "app.notifications.fcm.send_fcm_multicast_sync",
            side_effect=[_result("a2"), _result("b1")],
        ) as sender, patch.object(
            crud, "get_triggered_source_settings_for_rate", return_value=items
        ), patch.object(
            crud, "accumulate_unregistered_by_owner", wraps=real_accumulate
        ) as accumulate, patch.object(
            crud, "purge_unregistered_by_owner", return_value=2
        ) as purge, patch.object(
            crud, "create_source_notification_log"
        ), patch(
            "app.source_registry.get_source_definition",
            return_value=SimpleNamespace(display_name="Upbit"),
        ):
            sent = crud.process_source_rate_alerts(
                db,
                [{"source": "upbit", "asset": "usdt-krw", "rate": 1450.0}],
            )

        self.assertEqual(sent, 0)
        self.assertEqual(
            [c.args[0] for c in sender.call_args_list],
            [["a1", "a2"], ["b1", "b2"]],
            "실제 sender의 토큰 집합이 cleanup provenance와 갈라졌다",
        )
        self.assertEqual(
            [
                (
                    c.kwargs["owner_uid"],
                    c.kwargs["sent_tokens"],
                    c.kwargs["unregistered_tokens"],
                )
                for c in accumulate.call_args_list
            ],
            [
                ("A", ["a1", "a2"], ["a2"]),
                ("B", ["b1", "b2"], ["b1"]),
            ],
        )
        purge.assert_called_once_with(db, {"A": {"a2"}, "B": {"b1"}})
        db.commit.assert_called_once_with()


class TestComparisonCleanupWiring(unittest.TestCase):
    def test_persist_uses_cached_owner_and_exact_sent_tokens(self):
        candidate = ComparisonCandidate(
            setting_id=7,
            user_id="comparison-owner",
            tab="compare",
            left_source="kb",
            left_asset="usd-krw",
            right_source="hana",
            right_asset="usd-krw",
            diff_type="absolute",
            operator="gte",
            threshold=1.0,
            device_tokens=("sent",),
        )
        fresh = FreshComparisonSnapshot(
            setting_id=7,
            enabled=True,
            triggered=False,
            tab="compare",
            left_source="kb",
            left_asset="usd-krw",
            right_source="hana",
            right_asset="usd-krw",
            diff_type="absolute",
            operator="gte",
            threshold=1.0,
        )
        left = UnifiedRate(rate=1450.0, observed_at=None, origin="db")
        right = UnifiedRate(rate=1440.0, observed_at=None, origin="db")
        db = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = db
        ctx.__exit__.return_value = None

        with patch("app.database.get_db_context", return_value=ctx), \
             patch.object(
                 crud, "purge_unregistered_devices", return_value=1
             ) as purge:
            ComparisonAlertEvaluator._persist_result_sync(
                candidate,
                fresh,
                left,
                right,
                10.0,
                _result("sent", "never-sent"),
            )

        purge.assert_called_once_with(
            db,
            owner_uid="comparison-owner",
            sent_tokens=("sent",),
            unregistered_tokens=["sent", "never-sent"],
        )
        db.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
