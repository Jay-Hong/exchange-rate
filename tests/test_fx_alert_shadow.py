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
    FxCanaryBackend,
    FxNotificationBackend,
    SourceAlertBackend,
)
from app.notifications.fx_alert_shadow import (
    _noop_sender,
    _reset_fx_canary_state,
    _reset_fx_shadow_state,
    close_fx_canary_evaluator,
    evaluate_fx_batch_shadow,
    get_fx_alert_evaluator,
    get_fx_canary_evaluator,
    get_fx_shadow_stats,
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
    """S6: _noop_sender는 카운팅 책임 제거 → 방어용 no-delivery dict만 (count는 eval이)."""

    def tearDown(self):
        _reset_fx_shadow_state()

    def test_noop_sender_no_delivery_no_count(self):
        result = _noop_sender(["t1"], "title", "body", {"bank": "kb", "currency": "usd-krw"})
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(result["failed_tokens"], [])
        # S6: sender는 더 이상 카운트 안 함 (count는 evaluate_fx_batch_shadow의 condition-match 지점)
        self.assertEqual(get_fx_would_fire_counts(), {})


class TestFxShadowBatchEval(unittest.TestCase):
    """S6: would_fire/matched_candidates = **보조 진단**(execution-proof, cache-hit subset) — parity 아님.
    parity 기준은 crud legacy baseline. 여기선 진단 카운터 동작만 lock."""

    def tearDown(self):
        _reset_fx_shadow_state()

    def test_match_counts_at_condition_match_pre_refetch(self):
        # load → condition-match → would_fire(matched) += (refetch 이전). refetch !triggered → would_send.
        with patch.object(FxNotificationBackend, "load_settings", return_value=(_fx_candidate(),)), \
             patch.object(FxNotificationBackend, "refetch_snapshot", return_value=_fx_snapshot()), \
             patch("app.notifications.fcm.send_fcm_multicast_sync") as mock_fcm:
            asyncio.run(evaluate_fx_batch_shadow([_fx_observation()]))
        self.assertEqual(get_fx_would_fire_counts().get(("kb", "usd-krw")), 1)
        stats = get_fx_shadow_stats()
        self.assertEqual(stats["batch_seen"], 1)
        self.assertEqual(stats["settings_loaded"], 1)
        self.assertEqual(stats["matched_candidates"], 1)
        self.assertEqual(stats["would_send"], 1)              # refetch !triggered → would_send
        self.assertEqual(stats["refetch_skipped_triggered"], 0)
        mock_fcm.assert_not_called()                          # shadow = FCM 0

    def test_cached_match_triggered_refetch_diagnostic(self):
        # 진단(parity 아님): cache에 후보가 있는 cache-hit 케이스면 condition-match로 matched_candidates
        # 카운트되고, refetch서 triggered=true(legacy 선점)는 refetch_skipped_triggered로 분리(post-legacy
        # race 증거) + would_send=0. ⚠️ 실제 prod는 cache-miss가 흔해 이 카운트 자체가 신뢰 불가 →
        # parity는 legacy baseline으로 봐야 함. 이 test는 진단 카운터 분리 동작만 확인.
        triggered_snap = FreshSettingSnapshot(
            setting_id=5, enabled=True, triggered=True,   # legacy가 이미 발사+commit
            source="kb", asset="usd-krw", condition="above", threshold=1450.0,
        )
        with patch.object(FxNotificationBackend, "load_settings", return_value=(_fx_candidate(),)), \
             patch.object(FxNotificationBackend, "refetch_snapshot", return_value=triggered_snap), \
             patch("app.notifications.fcm.send_fcm_multicast_sync"):
            asyncio.run(evaluate_fx_batch_shadow([_fx_observation()]))
        self.assertEqual(get_fx_would_fire_counts().get(("kb", "usd-krw")), 1)  # 여전히 카운트!
        stats = get_fx_shadow_stats()
        self.assertEqual(stats["matched_candidates"], 1)
        self.assertEqual(stats["refetch_skipped_triggered"], 1)  # race 증거
        self.assertEqual(stats["would_send"], 0)

    def test_multi_candidate_counts_per_pair(self):
        cands = (_fx_candidate(setting_id=5), _fx_candidate(setting_id=6))

        def _snap(setting_id, user_id):
            return _fx_snapshot(setting_id=setting_id)

        with patch.object(FxNotificationBackend, "load_settings", return_value=cands), \
             patch.object(FxNotificationBackend, "refetch_snapshot", side_effect=_snap), \
             patch("app.notifications.fcm.send_fcm_multicast_sync"):
            asyncio.run(evaluate_fx_batch_shadow([_fx_observation()]))
        # key=(source,asset) pair 단위 집계 → 같은 (kb,usd-krw) 2 setting → 2
        self.assertEqual(get_fx_would_fire_counts().get(("kb", "usd-krw")), 2)
        self.assertEqual(get_fx_shadow_stats()["matched_candidates"], 2)

    def test_no_match_no_count(self):
        with patch.object(FxNotificationBackend, "load_settings", return_value=(_fx_candidate(),)), \
             patch.object(FxNotificationBackend, "refetch_snapshot", return_value=_fx_snapshot()), \
             patch("app.notifications.fcm.send_fcm_multicast_sync"):
            # above threshold=1450, rate=1400 → 미만족
            asyncio.run(evaluate_fx_batch_shadow([_fx_observation(rate=1400.0)]))
        self.assertEqual(get_fx_would_fire_counts(), {})
        stats = get_fx_shadow_stats()
        self.assertEqual(stats["batch_seen"], 1)
        self.assertEqual(stats["settings_loaded"], 1)
        self.assertEqual(stats["matched_candidates"], 0)

    def test_empty_batch_no_count(self):
        asyncio.run(evaluate_fx_batch_shadow([]))
        self.assertEqual(get_fx_would_fire_counts(), {})
        self.assertEqual(get_fx_shadow_stats()["batch_seen"], 0)


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


class TestLegacyMatchBaseline(unittest.TestCase):
    """S6b: crud legacy pre-mutation match baseline counter (parity 기준선)."""

    def tearDown(self):
        import app.crud as crud
        crud._fx_legacy_match_counts.clear()

    def test_record_and_get_accumulate(self):
        import app.crud as crud
        crud._fx_legacy_match_counts.clear()
        crud._record_fx_legacy_match("kb", "usd-krw", 2)
        crud._record_fx_legacy_match("kb", "usd-krw", 1)
        crud._record_fx_legacy_match("hana", "usd-krw", 3)
        counts = crud.get_fx_legacy_match_counts()
        self.assertEqual(counts[("kb", "usd-krw")], 3)
        self.assertEqual(counts[("hana", "usd-krw")], 3)

    def test_record_zero_or_negative_noop(self):
        import app.crud as crud
        crud._fx_legacy_match_counts.clear()
        crud._record_fx_legacy_match("kb", "usd-krw", 0)
        crud._record_fx_legacy_match("kb", "usd-krw", -1)
        self.assertEqual(crud.get_fx_legacy_match_counts(), {})

    def test_get_returns_copy(self):
        import app.crud as crud
        crud._fx_legacy_match_counts.clear()
        crud._record_fx_legacy_match("kb", "usd-krw", 1)
        snap = crud.get_fx_legacy_match_counts()
        snap[("kb", "usd-krw")] = 999  # snapshot 수정이 원본 오염 X
        self.assertEqual(crud.get_fx_legacy_match_counts()[("kb", "usd-krw")], 1)


def _mock_db_ctx():
    mock_db = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_db)
    ctx.__exit__ = MagicMock(return_value=None)
    return mock_db, ctx


class TestFxCanaryBackend(unittest.TestCase):
    """§6.1 canary: FxCanaryBackend — allowlist load + legacy-mirror real persist."""

    def test_load_settings_allowlist_filter(self):
        cands = (_fx_candidate(setting_id=5), _fx_candidate(setting_id=6))
        with patch.object(FxNotificationBackend, "load_settings", return_value=cands), \
             patch("app.config.FX_ALERT_CUTOVER_CANARY_SETTING_IDS", frozenset({5})):
            result = FxCanaryBackend().load_settings("kb", "usd-krw")
        self.assertEqual(len(result), 1)  # allowlist(5)만
        self.assertEqual(result[0].setting_id, 5)

    def test_persist_success_marks_and_logs_value_passthrough(self):
        _, ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=ctx), \
             patch("app.crud.mark_setting_triggered") as mock_mark, \
             patch("app.crud.create_notification_log") as mock_log:
            FxCanaryBackend().persist_result(
                _fx_candidate(), 1455.0,
                {"success_count": 1, "failure_count": 0, "failed_tokens": []},
            )
        mock_mark.assert_called_once()
        mock_log.assert_called_once()
        self.assertTrue(mock_log.call_args.kwargs["success"])
        self.assertEqual(mock_log.call_args.kwargs["bank"], "kb")        # source→bank
        self.assertEqual(mock_log.call_args.kwargs["currency"], "usd-krw")  # asset→currency

    def test_persist_failure_no_log(self):
        # legacy FX mirror: failure 시 mark/log 둘 다 X (SourceAlertBackend와 달리 failure-log 없음)
        _, ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=ctx), \
             patch("app.crud.mark_setting_triggered") as mock_mark, \
             patch("app.crud.create_notification_log") as mock_log:
            FxCanaryBackend().persist_result(
                _fx_candidate(), 1455.0,
                {"success_count": 0, "failure_count": 1, "failed_tokens": []},
            )
        mock_mark.assert_not_called()
        mock_log.assert_not_called()

    def test_persist_failed_tokens_cleanup(self):
        mock_db, ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=ctx), \
             patch("app.crud.mark_setting_triggered"), \
             patch("app.crud.create_notification_log"):
            FxCanaryBackend().persist_result(
                _fx_candidate(), 1455.0,
                # 보낸 토큰(device_tokens=("t1",)) 안의 것이어야 fence 를 통과한다
                {"success_count": 1, "failure_count": 1, "failed_tokens": ["t1"]},
            )
        _assert_owner_fenced_delete(self, mock_db, uid="u1", tokens=("t1",))
        mock_db.commit.assert_called()

    def test_persist_out_of_sent_failed_token_is_not_deleted(self):
        """보낸 적 없는 토큰은 삭제하지 않는다 (fail-closed)."""
        mock_db, ctx = _mock_db_ctx()
        with patch("app.database.get_db_context", return_value=ctx), \
             patch("app.crud.mark_setting_triggered"), \
             patch("app.crud.create_notification_log"):
            FxCanaryBackend().persist_result(
                _fx_candidate(), 1455.0,
                {"success_count": 1, "failure_count": 1, "failed_tokens": ["never-sent"]},
            )
        self.assertEqual(
            mock_db.query.return_value.filter.return_value.delete.call_count, 0,
            "sent_tokens 밖 토큰이 삭제됐다",
        )


class TestFxCanaryEvaluatorAndHook(unittest.TestCase):
    """§6.1 canary: evaluator(별 singleton, real FCM) + crud hook + shutdown drain."""

    def tearDown(self):
        _reset_fx_canary_state()
        _reset_fx_shadow_state()

    def test_canary_evaluator_structure_and_singleton(self):
        ev = get_fx_canary_evaluator()
        self.assertIsInstance(ev._backend, FxCanaryBackend)
        self.assertIsNone(ev._sender)  # real FCM (late-bind _send_fcm_multicast)
        self.assertIsNot(ev._cache, get_default_alert_settings_cache())  # dedicated
        self.assertIs(get_fx_canary_evaluator(), ev)  # singleton
        self.assertIsNot(ev, get_fx_alert_evaluator())  # shadow와 별 instance

    def test_emit_canary_flag_off_no_schedule(self):
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_CUTOVER_CANARY_ENABLED", False), \
             patch("app.topic_trigger_bridge.schedule_on_loop") as mock_sched:
            result = crud_mod._emit_fx_alert_canary([_changed_rate()])
        mock_sched.assert_not_called()
        self.assertFalse(result)  # B1: flag off → canary_handled False → legacy fallback

    def test_emit_canary_flag_on_schedules_run_canary(self):
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_CUTOVER_CANARY_ENABLED", True), \
             patch("app.topic_trigger_bridge.schedule_on_loop", return_value=True) as mock_sched:
            result = crud_mod._emit_fx_alert_canary([_changed_rate()])
        mock_sched.assert_called_once()
        cb, observations = mock_sched.call_args.args
        self.assertIs(cb, crud_mod._run_fx_alert_canary)  # sync wrapper
        self.assertEqual(observations[0].kind, "fx_canary")
        self.assertTrue(result)  # B1: enqueue 성공 → canary_handled True → legacy allowlist skip

    def test_emit_canary_enqueue_fail_returns_false(self):
        # B1: schedule_on_loop False(no loop/shutdown/race) → _emit False → legacy fallback(no miss)
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_CUTOVER_CANARY_ENABLED", True), \
             patch("app.topic_trigger_bridge.schedule_on_loop", return_value=False) as mock_sched:
            result = crud_mod._emit_fx_alert_canary([_changed_rate()])
        mock_sched.assert_called_once()
        self.assertFalse(result)

    def test_emit_canary_empty_changes_returns_false(self):
        # B1: flag on이어도 changes 비면 enqueue X → False
        import app.crud as crud_mod
        with patch("app.config.FX_ALERT_CUTOVER_CANARY_ENABLED", True), \
             patch("app.topic_trigger_bridge.schedule_on_loop") as mock_sched:
            result = crud_mod._emit_fx_alert_canary([])
        mock_sched.assert_not_called()
        self.assertFalse(result)

    def test_close_canary_noop_when_not_created(self):
        _reset_fx_canary_state()
        asyncio.run(close_fx_canary_evaluator())  # singleton None → 예외 없이 no-op


class TestB1LegacyCanaryGate(unittest.TestCase):
    """§6.1 B1: process_rate_alerts canary_handled gate — enqueue 성공(True)일 때만 allowlist
    setting을 legacy가 skip(canary가 real 발사), 실패/flag-off(False)면 legacy fallback(no miss).
    baseline(_fx_legacy_match_counts)도 대칭으로 제외/포함."""

    def tearDown(self):
        import app.crud as crud
        crud._fx_legacy_match_counts.clear()

    def _fake_triggered(self, setting_id=999):
        setting = MagicMock()
        setting.id = setting_id
        setting.condition = "above"
        setting.threshold = 1530.0
        device = MagicMock()
        device.device_token = "tok-1"
        return [{"setting": setting, "devices": [device], "user_id": "user-1234abcd"}]

    def _run(self, canary_handled, allowlist, shadow_enabled=False, setting_id=999):
        import app.crud as crud_mod
        crud_mod._fx_legacy_match_counts.clear()
        with patch("app.notifications.fcm.init_firebase", return_value=True), \
             patch("app.notifications.fcm.send_fcm_multicast_sync",
                   return_value={"success_count": 1, "failed_tokens": []}) as mock_fcm, \
             patch.object(crud_mod, "get_triggered_settings_for_rate",
                          return_value=self._fake_triggered(setting_id)), \
             patch.object(crud_mod, "mark_setting_triggered") as mock_mark, \
             patch.object(crud_mod, "create_notification_log") as mock_log, \
             patch("app.config.FX_ALERT_CUTOVER_CANARY_SETTING_IDS", frozenset(allowlist)), \
             patch("app.config.FX_ALERT_SHADOW_ENABLED", shadow_enabled):
            db = MagicMock()
            sent = crud_mod.process_rate_alerts(
                db, [{"bank": "kb", "currency": "usd-krw", "rate": 1541.0}],
                canary_handled=canary_handled,
            )
        return sent, mock_fcm, mock_mark

    def test_enqueue_success_skips_allowlist_setting(self):
        # canary_handled=True + setting in allowlist → legacy skip (FCM/persist 0)
        sent, mock_fcm, mock_mark = self._run(canary_handled=True, allowlist={999})
        self.assertEqual(sent, 0)
        mock_fcm.assert_not_called()
        mock_mark.assert_not_called()

    def test_enqueue_fail_legacy_fires(self):
        # canary_handled=False → legacy fallback (no miss): FCM + persist 발생
        sent, mock_fcm, mock_mark = self._run(canary_handled=False, allowlist={999})
        self.assertEqual(sent, 1)
        mock_fcm.assert_called_once()
        mock_mark.assert_called_once()

    def test_non_allowlist_setting_always_fires(self):
        # canary_handled=True여도 setting이 allowlist 밖이면 legacy 그대로 발사 (partition)
        sent, mock_fcm, mock_mark = self._run(canary_handled=True, allowlist={111})
        self.assertEqual(sent, 1)
        mock_fcm.assert_called_once()

    def test_baseline_excludes_allowlist_only_on_enqueue_success(self):
        import app.crud as crud_mod
        # enqueue 성공 + shadow on → baseline = legacy 실제 발사분 = 0 (allowlist 제외)
        self._run(canary_handled=True, allowlist={999}, shadow_enabled=True)
        self.assertEqual(
            crud_mod.get_fx_legacy_match_counts().get(("kb", "usd-krw"), 0), 0
        )

    def test_baseline_includes_when_enqueue_fail(self):
        import app.crud as crud_mod
        # enqueue 실패 + shadow on → legacy가 실제 발사 → baseline 포함
        self._run(canary_handled=False, allowlist={999}, shadow_enabled=True)
        self.assertEqual(
            crud_mod.get_fx_legacy_match_counts().get(("kb", "usd-krw"), 0), 1
        )


class TestB1FourSiteWiring(unittest.TestCase):
    """§6.1 B1: 4 호출부(bank atomic/legacy, investing atomic/legacy) reorder trip-wire.

    canary enqueue를 try/except로 먼저 캡처(예외→canary_handled=False 유지) 후 process_rate_alerts에
    canary_handled를 전달하는 구조를 소스 레벨로 잠금. 호출부는 DB/Redis/atomic 경로라 behavior 통합
    테스트는 비용↑ → 리팩토링이 순서/예외 fallback/전달을 깨면 잡는 구조 회귀 가드(codex non-blocker)."""

    def test_four_site_canary_capture_before_legacy(self):
        import inspect
        import app.crud as crud_mod
        src = inspect.getsource(crud_mod)
        # 4곳 모두: canary_handled=False 초기화(예외 fallback) + _emit 캡처 + process_rate_alerts 전달
        self.assertEqual(src.count("canary_handled = False"), 4)
        self.assertEqual(src.count("canary_handled = _emit_fx_alert_canary(changes)"), 4)
        self.assertEqual(
            src.count("process_rate_alerts(db, changed_rates, canary_handled=canary_handled)"), 4
        )
        # 구 패턴(shadow 뒤 별도 canary 블록) 완전 제거 — reorder로 이동
        self.assertNotIn("§6.1 canary: FX alert cutover canary", src)


if __name__ == "__main__":
    unittest.main()
