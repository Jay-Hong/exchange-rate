"""KRX close finalizer Stage 1 — kis_futures.py 시간 window helper pure tests.

KRX_CLOSE_SNAPSHOT_PLAN §5.2 (2026-05-17). Stage 1은 helper functions만 검증:
    - is_in_close_grace_window: CF 15:45:00~15:45:59 / CM 06:00:00~06:00:59
    - is_in_single_price_window: CF 15:35:00~15:44:59 / CM 05:50:00~05:59:59
    - compute_close_grace_end_kst: CF 15:46:00 KST / CM 06:01:00 KST

Pure deterministic — 외부 의존성 0 (DB/Redis/asyncio 미포함).
"""
from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.sources.kis_futures import (
    compute_close_grace_end_kst,
    is_in_close_grace_window,
    is_in_single_price_window,
)

KST = ZoneInfo("Asia/Seoul")


class TestIsInCloseGraceWindow(unittest.TestCase):
    """CF 15:45:00~15:45:59 / CM 06:00:00~06:00:59 inclusive of start, exclusive of end."""

    # --- CF close grace window ---

    def test_cf_at_boundary_inclusive(self):
        """15:45:00 정각 = window 안 (inclusive start)."""
        self.assertTrue(is_in_close_grace_window(datetime(2026, 5, 19, 15, 45, 0), "CF"))

    def test_cf_one_second_after_boundary(self):
        """15:45:01 (운영 baseline 종가 frame 시각) = window 안."""
        self.assertTrue(is_in_close_grace_window(datetime(2026, 5, 19, 15, 45, 1), "CF"))

    def test_cf_last_second_inside(self):
        """15:45:59 = window 안 (마지막 inclusive 시각)."""
        self.assertTrue(is_in_close_grace_window(datetime(2026, 5, 19, 15, 45, 59), "CF"))

    def test_cf_microseconds_inside(self):
        """15:45:59.999999 = window 안 (sub-second 경계)."""
        self.assertTrue(is_in_close_grace_window(datetime(2026, 5, 19, 15, 45, 59, 999999), "CF"))

    def test_cf_one_second_before_boundary(self):
        """15:44:59 = window 밖 (single-price 진행 마지막)."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 15, 44, 59), "CF"))

    def test_cf_window_end_exclusive(self):
        """15:46:00 정각 = window 밖 (exclusive end)."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 15, 46, 0), "CF"))

    def test_cf_morning_outside(self):
        """09:00:00 = window 밖 (정규장 한복판)."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 9, 0, 0), "CF"))

    def test_cf_cross_session_time(self):
        """CF session에 CM 시간(06:00) query → False (session 정합 검증)."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 6, 0, 0), "CF"))

    # --- CM close grace window ---

    def test_cm_at_boundary_inclusive(self):
        """06:00:00 정각 = window 안."""
        self.assertTrue(is_in_close_grace_window(datetime(2026, 5, 19, 6, 0, 0), "CM"))

    def test_cm_one_second_after_boundary(self):
        """06:00:01 (운영 baseline 8/9 종가 frame 시각) = window 안."""
        self.assertTrue(is_in_close_grace_window(datetime(2026, 5, 19, 6, 0, 1), "CM"))

    def test_cm_last_second_inside(self):
        """06:00:59 = window 안."""
        self.assertTrue(is_in_close_grace_window(datetime(2026, 5, 19, 6, 0, 59), "CM"))

    def test_cm_one_second_before_boundary(self):
        """05:59:59 = window 밖 (single-price 마지막)."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 5, 59, 59), "CM"))

    def test_cm_window_end_exclusive(self):
        """06:01:00 정각 = window 밖."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 6, 1, 0), "CM"))

    def test_cm_cross_session_time(self):
        """CM session에 CF 시간(15:45) query → False."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 15, 45, 0), "CM"))

    # --- Edge cases ---

    def test_session_none_returns_false(self):
        """휴장 (session=None) → False."""
        self.assertFalse(is_in_close_grace_window(datetime(2026, 5, 19, 15, 45, 30), None))

    def test_aware_datetime_accepted(self):
        """KST aware datetime도 정상 동작 (`.time()` 호출 호환)."""
        aware = datetime(2026, 5, 19, 15, 45, 30, tzinfo=KST)
        self.assertTrue(is_in_close_grace_window(aware, "CF"))


class TestIsInSinglePriceWindow(unittest.TestCase):
    """CF 15:35:00~15:44:59 / CM 05:50:00~05:59:59 inclusive of start, exclusive of close grace start."""

    # --- CF single-price window ---

    def test_cf_start_inclusive(self):
        """15:35:00 정각 = window 안."""
        self.assertTrue(is_in_single_price_window(datetime(2026, 5, 19, 15, 35, 0), "CF"))

    def test_cf_middle(self):
        """15:40:00 = window 안."""
        self.assertTrue(is_in_single_price_window(datetime(2026, 5, 19, 15, 40, 0), "CF"))

    def test_cf_last_second_inside(self):
        """15:44:59 = window 안 (마지막 inclusive 시각)."""
        self.assertTrue(is_in_single_price_window(datetime(2026, 5, 19, 15, 44, 59), "CF"))

    def test_cf_end_exclusive(self):
        """15:45:00 정각 = window 밖 (close grace window가 시작)."""
        self.assertFalse(is_in_single_price_window(datetime(2026, 5, 19, 15, 45, 0), "CF"))

    def test_cf_before_start(self):
        """15:34:59 = window 밖."""
        self.assertFalse(is_in_single_price_window(datetime(2026, 5, 19, 15, 34, 59), "CF"))

    # --- CM single-price window ---

    def test_cm_start_inclusive(self):
        """05:50:00 정각 = window 안."""
        self.assertTrue(is_in_single_price_window(datetime(2026, 5, 19, 5, 50, 0), "CM"))

    def test_cm_last_second_inside(self):
        """05:59:59 = window 안."""
        self.assertTrue(is_in_single_price_window(datetime(2026, 5, 19, 5, 59, 59), "CM"))

    def test_cm_end_exclusive(self):
        """06:00:00 정각 = window 밖 (close grace 시작)."""
        self.assertFalse(is_in_single_price_window(datetime(2026, 5, 19, 6, 0, 0), "CM"))

    def test_cm_before_start(self):
        """05:49:59 = window 밖."""
        self.assertFalse(is_in_single_price_window(datetime(2026, 5, 19, 5, 49, 59), "CM"))

    # --- Edge cases ---

    def test_session_none_returns_false(self):
        self.assertFalse(is_in_single_price_window(datetime(2026, 5, 19, 15, 40, 0), None))

    def test_aware_datetime_accepted(self):
        aware = datetime(2026, 5, 19, 15, 40, 0, tzinfo=KST)
        self.assertTrue(is_in_single_price_window(aware, "CF"))


class TestComputeCloseGraceEndKst(unittest.TestCase):
    """후속 stages에서 KrxCloseWindowWriter flush trigger 시각 계산용."""

    def test_cf_end(self):
        """CF: 15:46:00 KST aware."""
        result = compute_close_grace_end_kst(datetime(2026, 5, 19, 15, 45, 30), "CF")
        self.assertEqual(result, datetime(2026, 5, 19, 15, 46, 0, tzinfo=KST))

    def test_cm_end(self):
        """CM: 06:01:00 KST aware."""
        result = compute_close_grace_end_kst(datetime(2026, 5, 19, 6, 0, 30), "CM")
        self.assertEqual(result, datetime(2026, 5, 19, 6, 1, 0, tzinfo=KST))

    def test_cf_end_returns_aware_datetime(self):
        """Result는 항상 KST aware (tzinfo 명시)."""
        result = compute_close_grace_end_kst(datetime(2026, 5, 19, 15, 45, 0), "CF")
        self.assertEqual(result.tzinfo, KST)

    def test_invalid_session_raises_value_error(self):
        with self.assertRaises(ValueError):
            compute_close_grace_end_kst(datetime(2026, 5, 19, 15, 45, 0), "INVALID")

    def test_accepts_aware_now(self):
        """aware datetime 입력도 OK (date 추출용)."""
        aware = datetime(2026, 5, 19, 15, 45, 30, tzinfo=KST)
        result = compute_close_grace_end_kst(aware, "CF")
        self.assertEqual(result, datetime(2026, 5, 19, 15, 46, 0, tzinfo=KST))


if __name__ == "__main__":
    unittest.main()
