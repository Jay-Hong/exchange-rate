"""DXY 외부 fallback 정책 경계 — 상태 분리 후에도 수치 계약이 동일한가 (ADR-042).

`get_dxy_policy_state()` 분리는 "상태 이름"만 바꾼 게 아니라 정책 분기의 입력을 바꿨다.
따라서 이름 매핑(test_market_mode_policy.py)만으로는 부족하고, **실제 수치 경계**와
**ICE 세션 가드가 상태 판정보다 먼저 도는지**까지 잠가야 한다.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app.crawlers.dxy_spot as dxy_spot
from app.market_mode import DXY_ACTIVE, DXY_QUIET, DXY_WEEKEND_PRESERVE

UTC = dt_timezone.utc
NOW = datetime(2026, 8, 25, 4, 0, 0, tzinfo=UTC)  # 화 13:00 KST


def _decide(policy_state, *, fresh_age_s, failures, investing_age_s=0.0):
    """_should_use_yahoo_fallback을 직접 호출 — (외부 chain 허용?, meta)."""
    with patch.object(dxy_spot, "_consecutive_investing_failures", failures), \
         patch.object(dxy_spot, "_last_investing_fetch_ok_at", NOW - timedelta(seconds=fresh_age_s)):
        return dxy_spot._should_use_yahoo_fallback(
            now_utc=NOW,
            policy_state=policy_state,
            latest_investing_ts=NOW - timedelta(seconds=investing_age_s),
        )


class TestGraceBoundaries(unittest.TestCase):
    """ACTIVE 15분 / QUIET 30분."""

    def test_active_protects_under_15min_and_releases_at_15min(self):
        self.assertFalse(_decide(DXY_ACTIVE, fresh_age_s=15 * 60 - 1, failures=0)[0])
        self.assertTrue(_decide(DXY_ACTIVE, fresh_age_s=15 * 60, failures=0)[0])

    def test_quiet_protects_under_30min_and_releases_at_30min(self):
        self.assertFalse(_decide(DXY_QUIET, fresh_age_s=30 * 60 - 1, failures=0)[0])
        self.assertTrue(_decide(DXY_QUIET, fresh_age_s=30 * 60, failures=0)[0])

    def test_quiet_still_protected_where_active_would_release(self):
        """20분 지점 — 두 정책이 갈리는 구간. 이게 19:00~21:00 회귀의 실체다."""
        age = 20 * 60
        self.assertTrue(_decide(DXY_ACTIVE, fresh_age_s=age, failures=0)[0])
        self.assertFalse(_decide(DXY_QUIET, fresh_age_s=age, failures=0)[0])

    def test_shared_grace_helper_uses_the_same_boundaries(self):
        """silent-stale와 diff guard가 쓰는 공용 배선도 15분/30분이어야 한다."""
        self.assertEqual(dxy_spot._grace_seconds_for(DXY_ACTIVE), 15 * 60)
        self.assertEqual(dxy_spot._grace_seconds_for(DXY_QUIET), 30 * 60)
        self.assertEqual(dxy_spot._grace_seconds_for(DXY_WEEKEND_PRESERVE), 30 * 60)


class TestFailureThresholds(unittest.TestCase):
    """ACTIVE 3회 / QUIET 5회."""

    def test_active_threshold_is_three(self):
        self.assertFalse(_decide(DXY_ACTIVE, fresh_age_s=0, failures=2)[0])
        self.assertTrue(_decide(DXY_ACTIVE, fresh_age_s=0, failures=3)[0])

    def test_quiet_threshold_is_five(self):
        self.assertFalse(_decide(DXY_QUIET, fresh_age_s=0, failures=4)[0])
        self.assertTrue(_decide(DXY_QUIET, fresh_age_s=0, failures=5)[0])

    def test_quiet_tolerates_failures_that_active_would_not(self):
        self.assertTrue(_decide(DXY_ACTIVE, fresh_age_s=0, failures=4)[0])
        self.assertFalse(_decide(DXY_QUIET, fresh_age_s=0, failures=4)[0])


class TestWeekendPreserve72h(unittest.TestCase):
    """WEEKEND_PRESERVE는 grace/failure가 아니라 DB 72시간을 본다."""

    def test_preserves_while_db_value_is_under_72h(self):
        allow, meta = _decide(DXY_WEEKEND_PRESERVE, fresh_age_s=10 ** 9, failures=99,
                              investing_age_s=72 * 3600 - 1)
        self.assertFalse(allow, "72h 미만이면 실패가 아무리 쌓여도 외부 chain 미허용")
        self.assertEqual(meta["reason"], "OUT_preserve")

    def test_releases_once_db_value_exceeds_72h(self):
        allow, meta = _decide(DXY_WEEKEND_PRESERVE, fresh_age_s=0, failures=0,
                              investing_age_s=72 * 3600)
        self.assertTrue(allow)
        self.assertEqual(meta["reason"], "OUT_expired_or_no_data")

    def test_grace_and_threshold_do_not_apply_on_weekend(self):
        """ACTIVE라면 즉시 허용될 조건(15분 초과 + 3회 실패)에서도 주말은 보존한다."""
        allow, _ = _decide(DXY_WEEKEND_PRESERVE, fresh_age_s=16 * 60, failures=3,
                           investing_age_s=3600)
        self.assertFalse(allow)


class TestUnknownStateIsRejected(unittest.TestCase):
    """⛔ 조용한 QUIET 기본값 금지 (Crash Early)."""

    def test_unknown_policy_state_raises(self):
        with self.assertRaises(ValueError):
            _decide("BREAK1", fresh_age_s=0, failures=0)   # 구 mode 문자열도 거부

    def test_grace_helper_rejects_unknown(self):
        with self.assertRaises(dxy_spot.InvalidDxyPolicyStateError):
            dxy_spot._grace_seconds_for("IN")

    def test_silent_stale_unknown_state_is_not_misread_as_parse_failure(self):
        """정책 오류가 CSS fallback 저장으로 둔갑하지 않고 crawler 밖으로 전파된다."""
        db = MagicMock()
        old_source_ts = dxy_spot._last_source_ts_ms
        try:
            with patch("app.database.SessionLocal", return_value=db), \
                 patch.object(dxy_spot, "_fetch_spot_page", return_value=MagicMock()), \
                 patch.object(dxy_spot, "_extract_next_data_price", return_value=(104.5, 999)), \
                 patch.object(dxy_spot, "_mark_investing_fetch_ok"), \
                 patch.object(dxy_spot, "get_dxy_policy_state", return_value="BROKEN"), \
                 patch.object(dxy_spot, "_extract_selector_price") as mock_css, \
                 patch.object(dxy_spot, "_store_dxy_observation") as mock_store, \
                 patch.object(dxy_spot, "_try_yahoo_fallback") as mock_external:
                dxy_spot._last_source_ts_ms = 999
                with self.assertRaises(dxy_spot.InvalidDxyPolicyStateError):
                    dxy_spot.crawl_and_save_dxy_spot()
        finally:
            dxy_spot._last_source_ts_ms = old_source_ts

        mock_css.assert_not_called()
        mock_store.assert_not_called()
        mock_external.assert_not_called()
        db.close.assert_called_once()

    def test_hard_failure_unknown_state_is_validated_before_db_query(self):
        db = MagicMock()
        with patch("app.database.SessionLocal", return_value=db), \
             patch.object(dxy_spot, "_fetch_spot_page", side_effect=RuntimeError("primary down")), \
             patch.object(dxy_spot, "_mark_investing_failure", return_value=1), \
             patch.object(dxy_spot, "_is_dxy_weekly_session_open", return_value=True), \
             patch.object(dxy_spot, "get_dxy_policy_state", return_value="BROKEN"):
            with self.assertRaises(dxy_spot.InvalidDxyPolicyStateError):
                dxy_spot.crawl_and_save_dxy_spot()

        db.query.assert_not_called()
        db.close.assert_called_once()


class TestIceGuardRunsBeforePolicy(unittest.TestCase):
    """ICE 주간 세션 가드가 상태 판정보다 **먼저** return 해야 한다.

    이 순서가 뒤집히면 주말에 외부 chain(CNBC/Yahoo)이 DB에 값을 쓸 수 있다.
    """

    def test_session_closed_returns_before_policy_is_consulted(self):
        with patch.object(dxy_spot, "_is_dxy_weekly_session_open", return_value=False), \
             patch.object(dxy_spot, "get_dxy_policy_state") as mock_state, \
             patch.object(dxy_spot, "_should_use_yahoo_fallback") as mock_policy:
            dxy_spot._try_external_fallback(MagicMock(), NOW)
        mock_state.assert_not_called()
        mock_policy.assert_not_called()

    def test_session_open_reaches_policy(self):
        db = MagicMock()
        with patch.object(dxy_spot, "_is_dxy_weekly_session_open", return_value=True), \
             patch.object(dxy_spot, "get_dxy_policy_state", return_value=DXY_ACTIVE), \
             patch.object(dxy_spot, "_should_use_yahoo_fallback",
                          return_value=(False, {"reason": "IN_protected"})) as mock_policy:
            dxy_spot._try_external_fallback(db, NOW)
        mock_policy.assert_called_once()

    def test_out_window_is_inside_ice_closed_window(self):
        """WEEKEND_PRESERVE 구간 전체가 ICE 휴장 안에 들어간다 (72h 분기가 외부 chain
        경로에서 도달 불가인 이유). 토 07:00 ~ 월 06:00 KST 를 30분 간격으로 훑는다."""
        from app.market_mode import KST, get_dxy_policy_state
        t = datetime(2026, 8, 29, 7, 0, tzinfo=KST)   # 토 07:00
        end = datetime(2026, 8, 31, 6, 0, tzinfo=KST)  # 월 06:00
        checked = 0
        while t < end:
            self.assertEqual(get_dxy_policy_state(t), DXY_WEEKEND_PRESERVE, f"{t}")
            self.assertFalse(dxy_spot._is_dxy_weekly_session_open(t.astimezone(UTC)),
                             f"{t:%a %H:%M} KST — OUT 구간인데 ICE 세션이 열려 있다")
            t += timedelta(minutes=30)
            checked += 1
        self.assertEqual(checked, 94)


class TestExternalFallbackChain(unittest.TestCase):
    """가격 가드와 CNBC→Yahoo 진행 규칙을 실제 chain 실행으로 잠근다."""

    def test_default_chain_manifest_is_cnbc_then_yahoo(self):
        self.assertEqual([name for name, _ in dxy_spot._FALLBACK_CHAIN], ["cnbc", "yahoo"])
        self.assertIs(dxy_spot._FALLBACK_CHAIN[0][1], dxy_spot.fetch_dxy_from_cnbc)
        self.assertIs(dxy_spot._FALLBACK_CHAIN[1][1], dxy_spot.fetch_dxy_from_yahoo)

    def _run_chain(self, *, policy_state, investing_age_s, chain):
        db = MagicMock()
        latest = SimpleNamespace(
            rate=100.0,
            timestamp=NOW - timedelta(seconds=investing_age_s),
        )
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = latest
        meta = {
            "mode": policy_state,
            "consecutive_failures": 3,
            "last_fresh_age_seconds": investing_age_s,
            "investing_rate_age_seconds": investing_age_s,
        }
        with patch.object(dxy_spot, "_is_dxy_weekly_session_open", return_value=True), \
             patch.object(dxy_spot, "get_dxy_policy_state", return_value=policy_state), \
             patch.object(dxy_spot, "_should_use_yahoo_fallback", return_value=(True, meta)), \
             patch.object(dxy_spot, "_FALLBACK_CHAIN", chain), \
             patch.object(dxy_spot, "_store_dxy_observation") as mock_store:
            dxy_spot._try_external_fallback(db, NOW)
        return db, mock_store

    def test_active_diff_guard_applies_through_15min_and_releases_after(self):
        blocked_fetch = MagicMock(return_value=100.071)
        _, blocked_store = self._run_chain(
            policy_state=DXY_ACTIVE,
            investing_age_s=15 * 60,
            chain=[("cnbc", blocked_fetch)],
        )
        blocked_store.assert_not_called()

        allowed_fetch = MagicMock(return_value=100.071)
        db, allowed_store = self._run_chain(
            policy_state=DXY_ACTIVE,
            investing_age_s=15 * 60 + 1,
            chain=[("cnbc", allowed_fetch)],
        )
        allowed_store.assert_called_once_with(
            db, rate=100.071, source="cnbc", reason="cnbc_fallback"
        )

    def test_quiet_diff_guard_applies_through_30min_and_releases_after(self):
        _, blocked_store = self._run_chain(
            policy_state=DXY_QUIET,
            investing_age_s=30 * 60,
            chain=[("cnbc", MagicMock(return_value=100.071))],
        )
        blocked_store.assert_not_called()

        db, allowed_store = self._run_chain(
            policy_state=DXY_QUIET,
            investing_age_s=30 * 60 + 1,
            chain=[("cnbc", MagicMock(return_value=100.071))],
        )
        allowed_store.assert_called_once_with(
            db, rate=100.071, source="cnbc", reason="cnbc_fallback"
        )

    def test_exact_diff_threshold_is_allowed(self):
        # 0.125는 이진 부동소수점으로 정확히 표현돼 `>`와 `>=` 변이를 구분한다.
        with patch.object(dxy_spot, "DXY_YAHOO_DIFF_THRESHOLD", 0.125):
            db, store = self._run_chain(
                policy_state=DXY_ACTIVE,
                investing_age_s=0,
                chain=[("cnbc", MagicMock(return_value=100.125))],
            )
        store.assert_called_once_with(
            db, rate=100.125, source="cnbc", reason="cnbc_fallback"
        )

    def test_outlier_stops_the_whole_chain(self):
        first = MagicMock(return_value=100.08)
        second = MagicMock(return_value=100.01)
        _, store = self._run_chain(
            policy_state=DXY_ACTIVE,
            investing_age_s=0,
            chain=[("cnbc", first), ("yahoo", second)],
        )
        first.assert_called_once_with()
        second.assert_not_called()
        store.assert_not_called()

    def test_fetch_failure_advances_to_next_source_and_stores_it(self):
        first = MagicMock(side_effect=RuntimeError("CNBC down"))
        second = MagicMock(return_value=100.01)
        db, store = self._run_chain(
            policy_state=DXY_ACTIVE,
            investing_age_s=0,
            chain=[("cnbc", first), ("yahoo", second)],
        )
        first.assert_called_once_with()
        second.assert_called_once_with()
        store.assert_called_once_with(db, rate=100.01, source="yahoo", reason="yahoo_fallback")


if __name__ == "__main__":
    unittest.main()
