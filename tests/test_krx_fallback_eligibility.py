"""KRX REST fallback eligibility 단위 테스트 (PR6d-2b Stage A).

테스트 대상:
- is_in_session_end_grace (kis_futures.py helper)
- KisFuturesClient._evaluate_rest_fallback (counter + decision label)

Stage A 검증:
- env=false default → suppressed_disabled
- frame_age < threshold → suppressed_below_threshold
- session-end grace 구간 → suppressed_session_end_grace
- cooldown 동안 → suppressed_cooldown
- 모든 조건 통과 → eligible (실제 REST 호출은 Stage B+)
"""
from __future__ import annotations

import time
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from app import config
from app.crawlers.krx_kis import KisFuturesClient
from app.sources.kis_futures import is_in_session_end_grace
from app.sources.kis_master import ContractInfo


# ---------------------------------------------------------------------------
# is_in_session_end_grace
# ---------------------------------------------------------------------------


class TestIsInSessionEndGrace(unittest.TestCase):

    def test_cf_inside_grace(self):
        # CF 종료 15:45, grace 40분 → 15:05 이후 grace 구간
        ts = datetime(2026, 5, 8, 15, 30, 0)  # 15분 전
        self.assertTrue(is_in_session_end_grace(ts, "CF", 40))

    def test_cf_outside_grace(self):
        # CF 14:00 → 종료까지 105분, grace 40분 밖
        ts = datetime(2026, 5, 8, 14, 0, 0)
        self.assertFalse(is_in_session_end_grace(ts, "CF", 40))

    def test_cm_inside_grace_dawn(self):
        # CM 종료 06:00, grace 40분 → 05:20 이후
        ts = datetime(2026, 5, 8, 5, 30, 0)  # 30분 전
        self.assertTrue(is_in_session_end_grace(ts, "CM", 40))

    def test_cm_outside_grace_evening(self):
        # CM 18:00, 종료(다음날 06:00)까지 12시간 = grace 40분 밖
        ts = datetime(2026, 5, 8, 18, 0, 0)
        self.assertFalse(is_in_session_end_grace(ts, "CM", 40))

    def test_cm_close_auction_inside(self):
        # CM 05:55 (CLOSE_AUCTION 구간) → grace 40분 안
        ts = datetime(2026, 5, 8, 5, 55, 0)
        self.assertTrue(is_in_session_end_grace(ts, "CM", 40))

    def test_session_none_returns_false(self):
        ts = datetime(2026, 5, 8, 12, 0, 0)
        self.assertFalse(is_in_session_end_grace(ts, None, 40))

    def test_grace_min_zero_means_never(self):
        ts = datetime(2026, 5, 8, 15, 44, 59)  # 1초 전
        # grace=0이면 정확히 종료 시각만 grace
        self.assertFalse(is_in_session_end_grace(ts, "CF", 0))


# ---------------------------------------------------------------------------
# KisFuturesClient._evaluate_rest_fallback (Stage A counter telemetry)
# ---------------------------------------------------------------------------


def _make_client() -> KisFuturesClient:
    contract = ContractInfo(
        short_code="A75605",
        standard_code="KR4A75650007",
        name="미국달러 F 202605",
        contract_month="202605",
        expiry_date=date(2026, 5, 18),
    )
    approval = MagicMock()
    return KisFuturesClient(approval, contract=contract)


class TestEvaluateRestFallback(unittest.TestCase):
    """Codex BLOCKING 2 fix: 검사 순서 grace → threshold → cooldown → enabled.

    env=false라도 grace/threshold/cooldown 분류로 잡혀 Stage A telemetry 의미 보존.
    """

    @staticmethod
    def _kst_epoch(year: int, month: int, day: int, h: int, m: int = 0) -> float:
        from datetime import timezone, timedelta
        KST = timezone(timedelta(hours=9))
        return datetime(year, month, day, h, m).replace(tzinfo=KST).timestamp()

    def test_disabled_only_when_all_other_conditions_pass(self):
        """env=false + grace 밖 + threshold 통과 + cooldown 없음 → suppressed_disabled.

        Codex BLOCKING 2 fix: env 검사가 마지막이라 다른 조건 모두 통과해야 disabled.
        """
        client = _make_client()
        ts = self._kst_epoch(2026, 5, 8, 12, 0)  # CF 12:00 (grace 밖)
        client._last_tick_at = ts - 200  # 200s stale (threshold 120 통과)
        client._active_session = "CF"
        client._last_fallback_at = None
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", False), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40), \
             patch.object(config, "KRX_REST_COOLDOWN_SEC", 30):
            decision = client._evaluate_rest_fallback(ts)
        self.assertEqual(decision, "suppressed_disabled")

    def test_env_false_with_grace_classified_as_grace(self):
        """env=false라도 grace 구간이면 suppressed_session_end_grace로 분류.

        Codex BLOCKING 2 핵심 검증: 5/8 stale 4건이 env=false에서도 grace 차단으로
        잡혀야 telemetry 의미 보존 (env 우선이면 모두 disabled로 묶임).
        """
        client = _make_client()
        # CM 05:30 (종료 -30min, grace 40min 안)
        ts = self._kst_epoch(2026, 5, 8, 5, 30)
        client._last_tick_at = ts - 200
        client._active_session = "CM"
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", False), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40):
            decision = client._evaluate_rest_fallback(ts)
        self.assertEqual(decision, "suppressed_session_end_grace")
        self.assertEqual(
            client._fallback_counters["suppressed_session_end_grace"], 1
        )
        self.assertEqual(client._fallback_counters["suppressed_disabled"], 0)

    def test_below_threshold(self):
        """frame_age < KRX_REST_FALLBACK_STALE_SEC → suppressed_below_threshold."""
        client = _make_client()
        ts = self._kst_epoch(2026, 5, 8, 12, 0)  # CF 12:00 (grace 밖)
        client._last_tick_at = ts - 70  # 70s, threshold 120 미달
        client._active_session = "CF"
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", True), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40):
            decision = client._evaluate_rest_fallback(ts)
        self.assertEqual(decision, "suppressed_below_threshold")

    def test_session_end_grace_blocks_first(self):
        """grace 구간이 검사 1순위 — frame_age 무관 차단."""
        client = _make_client()
        ts = self._kst_epoch(2026, 5, 8, 5, 30)  # CM grace 안
        client._last_tick_at = ts - 200
        client._active_session = "CM"
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", True), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40):
            decision = client._evaluate_rest_fallback(ts)
        self.assertEqual(decision, "suppressed_session_end_grace")

    def test_cooldown_blocks(self):
        """grace 밖 + threshold 통과 + cooldown 안 → suppressed_cooldown."""
        client = _make_client()
        ts = self._kst_epoch(2026, 5, 8, 12, 0)  # CF 12:00 (grace 밖)
        client._last_tick_at = ts - 200
        client._active_session = "CF"
        client._last_fallback_at = ts - 10  # 10초 전 (cooldown 30s 안)
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", True), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40), \
             patch.object(config, "KRX_REST_COOLDOWN_SEC", 30):
            decision = client._evaluate_rest_fallback(ts)
        self.assertEqual(decision, "suppressed_cooldown")

    def test_eligible_all_conditions_pass(self):
        """모든 조건 통과 → eligible (Stage B+에서 실제 REST 호출)."""
        client = _make_client()
        ts = self._kst_epoch(2026, 5, 8, 12, 0)  # CF 12:00 (grace 밖)
        client._last_tick_at = ts - 200  # 200s stale (threshold 120 통과)
        client._active_session = "CF"
        client._last_fallback_at = None
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", True), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40), \
             patch.object(config, "KRX_REST_COOLDOWN_SEC", 30):
            decision = client._evaluate_rest_fallback(ts)
        self.assertEqual(decision, "eligible")

    def test_counters_isolated_per_decision(self):
        """단일 evaluation은 evaluated +1 + 한 카테고리만 +1 (mutex)."""
        client = _make_client()
        ts = self._kst_epoch(2026, 5, 8, 12, 0)
        client._last_tick_at = ts - 200
        client._active_session = "CF"
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", False), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40), \
             patch.object(config, "KRX_REST_COOLDOWN_SEC", 30):
            client._evaluate_rest_fallback(ts)
        # evaluated +1, suppressed_disabled +1, 나머지 0
        c = client._fallback_counters
        self.assertEqual(c["evaluated"], 1)
        self.assertEqual(c["suppressed_disabled"], 1)
        self.assertEqual(c["eligible"], 0)
        self.assertEqual(c["suppressed_below_threshold"], 0)
        self.assertEqual(c["suppressed_session_end_grace"], 0)
        self.assertEqual(c["suppressed_cooldown"], 0)

    def test_metrics_exposes_fallback_counters(self):
        """get_metrics()에 fallback counter 노출 (admin endpoint 자료)."""
        client = _make_client()
        ts = self._kst_epoch(2026, 5, 8, 12, 0)
        client._last_tick_at = ts - 70
        client._active_session = "CF"
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", True), \
             patch.object(config, "KRX_REST_FALLBACK_STALE_SEC", 120), \
             patch.object(config, "KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", 40):
            client._evaluate_rest_fallback(ts)
        m = client.get_metrics()
        self.assertIn("fallback", m["counters"])
        self.assertEqual(m["counters"]["fallback"]["evaluated"], 1)
        self.assertEqual(m["counters"]["fallback"]["suppressed_below_threshold"], 1)


# ---------------------------------------------------------------------------
# BLOCKING 1 fix — status transition 시점 evaluation
# ---------------------------------------------------------------------------


class TestSetStatusTriggersEvaluation(unittest.TestCase):
    """Codex BLOCKING 1 fix: normal → stale 전이 시점에 1회 evaluation.

    summary loop 60s cycle만으로는 6~15초 짧은 stale (5/8 4건 모두) 누락.
    """

    def test_normal_to_stale_triggers_evaluation(self):
        """status normal → stale 전이 시 _evaluate_rest_fallback 1회 호출."""
        client = _make_client()
        client._last_tick_at = time.time() - 70
        client._active_session = "CM"
        with patch.object(config, "KRX_REST_FALLBACK_ENABLED", True):
            client._set_status("stale")
        # transition 시점에 evaluation 호출됨 (counter 증가)
        self.assertEqual(client._fallback_counters["evaluated"], 1)

    def test_short_stale_event_captured(self):
        """6초 stale도 transition 시점에 잡힘 (5/8 baseline 4건 같은 짧은 stale 검증)."""
        client = _make_client()
        client._last_tick_at = time.time() - 65
        client._active_session = "CM"
        # transition 1: normal → stale
        client._set_status("stale")
        # 빠른 복구: stale → normal (transition 안 evaluation 안 함)
        client._set_status("normal")
        # 짧은 stale 1회만 evaluation
        self.assertEqual(client._fallback_counters["evaluated"], 1)

    def test_stale_to_normal_does_not_evaluate(self):
        """stale → normal 전이는 evaluation 안 함 (entry만)."""
        client = _make_client()
        client._last_tick_at = time.time() - 65
        client._active_session = "CM"
        client._set_status("stale")  # +1
        prev_count = client._fallback_counters["evaluated"]
        client._set_status("normal")  # +0 (exit는 평가 X)
        self.assertEqual(client._fallback_counters["evaluated"], prev_count)

    def test_normal_to_reconnecting_does_not_evaluate(self):
        """normal → reconnecting 전이도 evaluation 안 함 (stale 전용)."""
        client = _make_client()
        client._last_tick_at = time.time() - 5
        client._active_session = "CM"
        client._set_status("reconnecting")
        self.assertEqual(client._fallback_counters["evaluated"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
