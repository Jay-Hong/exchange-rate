"""market_mode — 은행 모드 경계 변경 + DXY 정책 시계 분리 회귀 잠금 (2026-08-28).

배경:
    은행 BREAK1 시작을 21:00 → 19:00으로 당겼다(SC 고시 종료 실측). 그런데 DXY 외부 fallback
    정책이 같은 `get_market_mode()`를 쓰고 있어서, 그대로 두면 19:00~21:00 구간의 DXY 장애 복구가
    20초(실패 임계 3→5회) 또는 최대 15분(grace 15→30분) 늦어진다. 그래서 DXY는 전용 시계
    `get_dxy_policy_state()`로 분리하고 **구 경계를 동결**했다.

이 파일이 잠그는 것:
    1. DXY 정책이 구 모드 로직과 **일주일 전 구간에서 완전히 동일**하다 (동결 사본 대조).
    2. 은행 모드의 새 경계가 의도대로다.
    3. 두 시계가 19:00~20:59에 **실제로 갈라진다** (재결합 방지 trip-wire).
    4. dxy_spot이 `get_market_mode`를 다시 참조하지 않는다 (소스 trip-wire).
"""
from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta

from app.market_mode import (
    DXY_ACTIVE,
    DXY_QUIET,
    DXY_WEEKEND_PRESERVE,
    KST,
    get_dxy_policy_state,
    get_market_mode,
)


def _legacy_market_mode(now: datetime) -> str:
    """⛔ 2026-08-28 이전 `get_market_mode()`의 **동결 사본**. 절대 수정하지 말 것.

    DXY 정책 등가성의 기준선이다. 여기를 현행 로직에 맞춰 고치면 등가성 검증이
    자기 자신과의 비교가 되어 아무것도 잡지 못한다.

    출처 검증: `git show 0c811f1:app/market_mode.py` 의 `get_market_mode` 본문과
    **AST 동일**함을 확인했다(docstring 제외 후 `ast.unparse` 비교). 이 사본이 의심되면
    같은 방법으로 재대조할 것 — 육안 비교로는 조건절 하나가 바뀐 것을 놓친다.
    """
    weekday = now.weekday()
    hour = now.hour

    if (weekday == 5 and hour >= 7) or (weekday == 6) or (weekday == 0 and hour < 6):
        return "OUT"

    if 0 <= weekday <= 4 and 21 <= hour:
        return "BREAK1"
    elif 1 <= weekday <= 5 and hour < 3:
        return "BREAK1"

    if 3 <= hour < 8:
        return "BREAK2"

    return "IN"


# 구 모드 → DXY 정책 상태 기대 매핑
_LEGACY_TO_DXY = {
    "IN": DXY_ACTIVE,
    "BREAK1": DXY_QUIET,
    "BREAK2": DXY_QUIET,
    "OUT": DXY_WEEKEND_PRESERVE,
}

# 2026-08-24(월) 00:00 KST 부터 일주일 — 요일 7종을 모두 포함한다.
_WEEK_START = datetime(2026, 8, 24, 0, 0, tzinfo=KST)


def _week_minutes():
    for i in range(7 * 24 * 60):
        yield _WEEK_START + timedelta(minutes=i)


class TestDxyPolicyEquivalence(unittest.TestCase):
    """DXY 정책 시계가 구 모드 로직과 일주일 전 구간에서 동일한가."""

    def test_full_week_minute_by_minute_equivalence(self):
        checked = 0
        for t in _week_minutes():
            expected = _LEGACY_TO_DXY[_legacy_market_mode(t)]
            actual = get_dxy_policy_state(t)
            self.assertEqual(
                actual, expected,
                f"{t:%a %H:%M} — 구 모드 {_legacy_market_mode(t)} → {expected} 기대, {actual} 반환",
            )
            checked += 1
        self.assertEqual(checked, 10080)  # 표본이 조용히 줄어드는 것 방지

    def test_all_three_states_actually_occur(self):
        """세 상태가 모두 실제로 발생하는지 — 한 상태로 붕괴하면 위 등가성도 무의미해진다."""
        seen = {get_dxy_policy_state(t) for t in _week_minutes()}
        self.assertEqual(seen, {DXY_ACTIVE, DXY_QUIET, DXY_WEEKEND_PRESERVE})


class TestBankModeNewBoundaries(unittest.TestCase):
    """은행 모드의 새 경계."""

    def _mode(self, day, hour, minute=0):
        return get_market_mode(datetime(2026, 8, day, hour, minute, tzinfo=KST))

    def test_break1_starts_at_19_on_weekdays(self):
        # 8/25 = 화요일
        self.assertEqual(self._mode(25, 18, 59), "IN")
        self.assertEqual(self._mode(25, 19, 0), "BREAK1")

    def test_break1_runs_through_night_to_06(self):
        # 8/26 = 수요일 새벽 — 구 로직이라면 03:00부터 BREAK2였다
        self.assertEqual(self._mode(26, 2, 59), "BREAK1")
        self.assertEqual(self._mode(26, 3, 0), "BREAK1")
        self.assertEqual(self._mode(26, 5, 59), "BREAK1")

    def test_break2_is_06_to_08(self):
        self.assertEqual(self._mode(26, 6, 0), "BREAK2")
        self.assertEqual(self._mode(26, 7, 59), "BREAK2")
        self.assertEqual(self._mode(26, 8, 0), "IN")

    def test_saturday_break1_then_break2_then_out(self):
        # 8/29 = 토요일: 금요일 세션이 06:00까지 이어짐
        self.assertEqual(self._mode(29, 5, 59), "BREAK1")
        self.assertEqual(self._mode(29, 6, 0), "BREAK2")
        self.assertEqual(self._mode(29, 6, 59), "BREAK2")
        self.assertEqual(self._mode(29, 7, 0), "OUT")

    def test_monday_out_then_break2_then_in(self):
        # 8/24 = 월요일
        self.assertEqual(self._mode(24, 5, 59), "OUT")
        self.assertEqual(self._mode(24, 6, 0), "BREAK2")
        self.assertEqual(self._mode(24, 8, 0), "IN")

    def test_sunday_is_out_all_day(self):
        for hour in range(24):
            self.assertEqual(self._mode(30, hour), "OUT")


class TestClocksAreSeparated(unittest.TestCase):
    """두 시계가 재결합되지 않았는지 — 이 테스트가 실패하면 DXY 회귀가 돌아온 것이다."""

    def test_banks_and_dxy_diverge_between_19_and_21(self):
        # 평일 19:00~20:59: 은행은 이미 BREAK1, DXY는 아직 ACTIVE 여야 한다.
        for hour in (19, 20):
            t = datetime(2026, 8, 25, hour, 30, tzinfo=KST)
            self.assertEqual(get_market_mode(t), "BREAK1", f"{hour}시 은행 모드")
            self.assertEqual(get_dxy_policy_state(t), DXY_ACTIVE, f"{hour}시 DXY 정책")

    def test_banks_and_dxy_diverge_between_03_and_06(self):
        # 평일 03:00~05:59: 은행은 BREAK1(연장), DXY는 QUIET(구 BREAK2와 동일 정책).
        t = datetime(2026, 8, 26, 4, 30, tzinfo=KST)
        self.assertEqual(get_market_mode(t), "BREAK1")
        self.assertEqual(get_dxy_policy_state(t), DXY_QUIET)

    def test_dxy_spot_does_not_reference_get_market_mode(self):
        """소스 trip-wire — dxy_spot이 은행 시계를 다시 import/호출하면 실패."""
        import app.crawlers.dxy_spot as dxy_spot

        src = inspect.getsource(dxy_spot)
        self.assertNotIn(
            "get_market_mode", src,
            "dxy_spot이 get_market_mode를 다시 참조한다 — 두 시계가 재결합되면 "
            "19:00~21:00 DXY fallback 회귀가 조용히 돌아온다.",
        )


if __name__ == "__main__":
    unittest.main()
