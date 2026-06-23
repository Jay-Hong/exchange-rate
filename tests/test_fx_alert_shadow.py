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
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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


def _changed_rate(source="kb", asset="usd-krw", rate=1455.0):
    from app.crud import ChangedRate
    # naive UTC — production get_utc_now() 형태 (helper의 replace(utc) TZ-safety 검증용)
    return ChangedRate(
        source=source, asset=asset, rate=rate,
        changed_at=datetime(2026, 6, 23),
    )


class TestS4FxShadowWiring(unittest.TestCase):
    """fanout step 4 S4 — crud _emit_fx_alert_shadow / _run_fx_alert_shadow wiring."""

    def setUp(self):
        import app.topic_trigger_bridge as bridge
        bridge.reset_for_tests()

    def tearDown(self):
        import app.crud as crud_mod
        import app.topic_trigger_bridge as bridge
        crud_mod._fx_shadow_tasks.clear()
        bridge.reset_for_tests()
        _reset_fx_shadow_state()

    def test_flag_off_no_schedule(self):
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_SHADOW_ENABLED", False), \
             patch("app.topic_trigger_bridge.schedule_on_loop") as mock_sched:
            crud_mod._emit_fx_alert_shadow([_changed_rate()])
        mock_sched.assert_not_called()  # flag off → bridge 미호출 = zero overhead

    def test_flag_on_marshals_sync_wrapper_with_observations(self):
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_SHADOW_ENABLED", True), \
             patch("app.topic_trigger_bridge.schedule_on_loop") as mock_sched:
            crud_mod._emit_fx_alert_shadow([_changed_rate(rate=1455.0)])
        mock_sched.assert_called_once()
        cb, observations = mock_sched.call_args.args
        # BLOCKER 방지: coroutine 아닌 sync wrapper 전달
        self.assertIs(cb, crud_mod._run_fx_alert_shadow)
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertEqual(obs.source, "kb")        # ChangedRate.source → AlertObservation.source
        self.assertEqual(obs.asset, "usd-krw")
        self.assertEqual(obs.rate, 1455.0)
        self.assertEqual(obs.kind, "fx_change")
        # timestamp_ms = changed_at(2026-06-23 UTC) 도출
        self.assertEqual(obs.timestamp_ms, int(datetime(2026, 6, 23, tzinfo=timezone.utc).timestamp() * 1000))

    def test_flag_on_empty_changes_no_schedule(self):
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_SHADOW_ENABLED", True), \
             patch("app.topic_trigger_bridge.schedule_on_loop") as mock_sched:
            crud_mod._emit_fx_alert_shadow([])
        mock_sched.assert_not_called()

    def test_run_wrapper_creates_strong_ref_task(self):
        import app.crud as crud_mod

        async def _run():
            with patch(
                "app.notifications.fx_alert_shadow.evaluate_fx_batch_shadow",
                new=AsyncMock(),
            ) as mock_eval:
                crud_mod._run_fx_alert_shadow([])
                # create_task + strong-ref(_fx_shadow_tasks)로 GC 방지 — in-flight 동안 보존
                self.assertEqual(len(crud_mod._fx_shadow_tasks), 1)
                task = next(iter(crud_mod._fx_shadow_tasks))
                await task
                # done_callback(discard) — poll-until (fixed sleep flaky 회피)
                for _ in range(20):
                    if not crud_mod._fx_shadow_tasks:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(len(crud_mod._fx_shadow_tasks), 0)
                mock_eval.assert_awaited_once()

        asyncio.run(_run())

    def test_no_loop_silent_skip(self):
        # main loop 미등록(setUp reset_for_tests) → schedule_on_loop False → 예외 없이 skip
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_SHADOW_ENABLED", True):
            crud_mod._emit_fx_alert_shadow([_changed_rate()])  # raise 없으면 통과


if __name__ == "__main__":
    unittest.main()
