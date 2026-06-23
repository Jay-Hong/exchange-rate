"""fanout step 4 S3 — sender seam (alert_evaluator) + FX alert shadow (fx_alert_shadow).

검증:
- sender seam: UsdtAlertEvaluator/KrxAlertEvaluator default sender=None (byte-identity) +
  None→class patch(_send_fcm_multicast) late-bind 호출 + 주입 sender 우선(class patch 미호출).
- fx_alert_shadow: FxNotificationBackend+dedicated cache+no-op sender lazy singleton /
  noop_sender FCM 0 + would_fire 카운트 + key 계약 / evaluate_fx_batch_shadow pure-eval
  (load_settings+refetch_snapshot mock) match/multi/no-match/empty / import side-effect 0.
"""
from __future__ import annotations

import asyncio
import importlib
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.notifications import fx_alert_shadow
from app.notifications.alert_evaluator import (
    AlertObservation,
    CachedAlertSetting,
    FreshSettingSnapshot,
    KrxAlertEvaluator,
    UsdtAlertEvaluator,
    get_default_alert_settings_cache,
)
from app.notifications.alert_storage_backend import (
    FxNotificationBackend,
    SourceAlertBackend,
)
from app.notifications.fx_alert_shadow import (
    _reset_fx_shadow_state,
    _shadow_noop_sender,
    evaluate_fx_batch_shadow,
    get_fx_alert_evaluator,
    get_fx_would_fire_counts,
)


def _usdt_candidate() -> CachedAlertSetting:
    return CachedAlertSetting(
        setting_id=1, user_id="u1", source="upbit", asset="usdt-krw",
        condition="above", threshold=1450.0, device_tokens=("t1",),
    )


def _fx_candidate(setting_id: int = 5) -> CachedAlertSetting:
    return CachedAlertSetting(
        setting_id=setting_id, user_id="u1", source="kb", asset="usd-krw",
        condition="above", threshold=1450.0, device_tokens=("t1",),
    )


def _fx_snapshot(setting_id: int = 5) -> FreshSettingSnapshot:
    return FreshSettingSnapshot(
        setting_id=setting_id, enabled=True, triggered=False,
        source="kb", asset="usd-krw", condition="above", threshold=1450.0,
    )


def _fx_observation(rate: float = 1455.0) -> AlertObservation:
    return AlertObservation(
        source="kb", asset="usd-krw", rate=rate, timestamp_ms=0, kind="rest_probe",
    )


# ---------------------------------------------------------------------------
# sender seam (alert_evaluator.py) — USDT/KRX byte-identity 보존
# ---------------------------------------------------------------------------

class TestSenderSeam(unittest.TestCase):

    def test_default_sender_is_none(self):
        # USDT/KRX 기본 인스턴스 sender=None → _do_send_and_persist에서 late-bind
        self.assertIsNone(UsdtAlertEvaluator()._sender)
        self.assertIsNone(KrxAlertEvaluator()._sender)

    def test_none_sender_late_binds_class_method(self):
        # sender=None → (self._sender or self._send_fcm_multicast) → class patch 호출 (11 patch 보존 등가)
        ev = UsdtAlertEvaluator()
        cand = _usdt_candidate()
        with patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
            return_value={"success_count": 1, "failure_count": 0, "failed_tokens": []},
        ) as mock_default, patch.object(SourceAlertBackend, "persist_result") as mock_persist:
            asyncio.run(ev._do_send_and_persist(cand, Decimal("1455.0")))
        mock_default.assert_called_once()
        mock_persist.assert_called_once()

    def test_injected_sender_bypasses_class_method(self):
        # sender 주입 → 주입 sender 호출 + class _send_fcm_multicast 미호출
        spy = MagicMock(return_value={"success_count": 0, "failure_count": 0, "failed_tokens": []})
        ev = UsdtAlertEvaluator(sender=spy)
        cand = _usdt_candidate()
        with patch.object(UsdtAlertEvaluator, "_send_fcm_multicast") as mock_default, \
             patch.object(SourceAlertBackend, "persist_result"):
            asyncio.run(ev._do_send_and_persist(cand, Decimal("1455.0")))
        spy.assert_called_once()
        mock_default.assert_not_called()


# ---------------------------------------------------------------------------
# fx_alert_shadow — factory / noop sender / batch eval / dead code
# ---------------------------------------------------------------------------

class TestFxShadowFactory(unittest.TestCase):

    def tearDown(self):
        _reset_fx_shadow_state()

    def test_factory_backend_and_dedicated_cache(self):
        ev = get_fx_alert_evaluator()
        self.assertIsInstance(ev._backend, FxNotificationBackend)
        # dedicated cache (process-wide singleton 재사용 금지 — 교차오염 방지)
        self.assertIsNot(ev._cache, get_default_alert_settings_cache())

    def test_factory_lazy_singleton(self):
        ev1 = get_fx_alert_evaluator()
        ev2 = get_fx_alert_evaluator()
        self.assertIs(ev2, ev1)
        self.assertIs(ev2._cache, ev1._cache)


class TestFxShadowNoopSender(unittest.TestCase):

    def tearDown(self):
        _reset_fx_shadow_state()

    def test_noop_sender_counts_and_no_delivery(self):
        result = _shadow_noop_sender(
            ["t1"], "title", "body", {"bank": "kb", "currency": "usd-krw"},
        )
        # no-delivery dict (FCM 미발송)
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(result["failed_tokens"], [])
        # key 계약 = (data["bank"], data["currency"]) — FxNotificationBackend.build_payload 출력 의존
        self.assertEqual(get_fx_would_fire_counts().get(("kb", "usd-krw")), 1)

    def test_noop_sender_accumulates(self):
        for _ in range(3):
            _shadow_noop_sender(["t1"], "t", "b", {"bank": "kb", "currency": "usd-krw"})
        self.assertEqual(get_fx_would_fire_counts().get(("kb", "usd-krw")), 3)


class TestFxShadowBatchEval(unittest.TestCase):

    def tearDown(self):
        _reset_fx_shadow_state()

    def test_match_increments_count_no_fcm_no_db(self):
        # persist_result는 _do_send_and_persist에서 항상 호출되나 FxNotificationBackend는
        # no-op(DB 미접촉) → get_db_context 미호출로 "shadow = DB mutation 0" 직접 검증.
        with patch.object(FxNotificationBackend, "load_settings", return_value=(_fx_candidate(),)), \
             patch.object(FxNotificationBackend, "refetch_snapshot", return_value=_fx_snapshot()), \
             patch("app.database.get_db_context") as mock_db, \
             patch("app.notifications.fcm.send_fcm_multicast_sync") as mock_fcm:
            asyncio.run(evaluate_fx_batch_shadow([_fx_observation()]))
        self.assertEqual(get_fx_would_fire_counts().get(("kb", "usd-krw")), 1)
        mock_fcm.assert_not_called()   # shadow = FCM 0
        mock_db.assert_not_called()    # persist_result no-op → DB 세션 0 (load/refetch도 mock)

    def test_multi_candidate_counts_per_pair(self):
        cands = (_fx_candidate(setting_id=5), _fx_candidate(setting_id=6))

        def _snap(setting_id, user_id):
            return _fx_snapshot(setting_id=setting_id)

        with patch.object(FxNotificationBackend, "load_settings", return_value=cands), \
             patch.object(FxNotificationBackend, "refetch_snapshot", side_effect=_snap), \
             patch("app.notifications.fcm.send_fcm_multicast_sync"):
            asyncio.run(evaluate_fx_batch_shadow([_fx_observation()]))
        # key=(bank,currency) pair 단위 집계 → 같은 (kb,usd-krw) 2 setting → 2
        self.assertEqual(get_fx_would_fire_counts().get(("kb", "usd-krw")), 2)

    def test_no_match_no_count(self):
        with patch.object(FxNotificationBackend, "load_settings", return_value=(_fx_candidate(),)), \
             patch.object(FxNotificationBackend, "refetch_snapshot", return_value=_fx_snapshot()), \
             patch("app.notifications.fcm.send_fcm_multicast_sync"):
            # above threshold=1450, rate=1400 → 미만족
            asyncio.run(evaluate_fx_batch_shadow([_fx_observation(rate=1400.0)]))
        self.assertEqual(get_fx_would_fire_counts(), {})

    def test_empty_batch_no_count(self):
        asyncio.run(evaluate_fx_batch_shadow([]))
        self.assertEqual(get_fx_would_fire_counts(), {})


class TestFxShadowDeadCode(unittest.TestCase):

    def tearDown(self):
        _reset_fx_shadow_state()

    def test_import_side_effect_zero(self):
        # 모듈 reload → 어떤 backend/cache/evaluator도 import 시점 미생성 (lazy singleton)
        importlib.reload(fx_alert_shadow)
        self.assertIsNone(fx_alert_shadow._fx_shadow_evaluator)
        self.assertEqual(fx_alert_shadow.get_fx_would_fire_counts(), {})


if __name__ == "__main__":
    unittest.main()
