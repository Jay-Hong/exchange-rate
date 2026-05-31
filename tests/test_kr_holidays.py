"""한국 공휴일 calendar 단위 테스트 (ADR-034 Phase 2d).

검증 핵심:
- observed=True load-bearing 잠금 (대체공휴일 — 5/25 KRX 사고 날짜 / 3/2 삼일절 대체)
- PUBLIC ∪ BANK 결합 (근로자의날 5/1은 BANK 전용)
- classify_hana_calendar_day 분류 (weekend > holiday > business_day)
"""
from __future__ import annotations

import unittest
from datetime import date

from app.calendars.kr_holidays import is_kr_holiday, kr_holiday_name
from app.calendars.hana_business_days import (
    classify_hana_calendar_day,
    is_hana_business_day,
)


class TestKrHolidays(unittest.TestCase):

    def test_public_holidays_present(self):
        """주요 공휴일 PUBLIC 포함."""
        for d in [
            date(2026, 1, 1),   # 신정
            date(2026, 2, 17),  # 설날
            date(2026, 5, 5),   # 어린이날
            date(2026, 9, 25),  # 추석
            date(2026, 12, 25),  # 성탄절
        ]:
            self.assertTrue(is_kr_holiday(d), msg=f"{d} should be KR holiday")

    def test_bank_only_labor_day(self):
        """근로자의날(5/1) — PUBLIC엔 없고 BANK에만. PUBLIC∪BANK 결합 검증."""
        self.assertTrue(
            is_kr_holiday(date(2026, 5, 1)),
            "근로자의날 must be holiday (BANK category — public∪bank 결합 필요)",
        )

    def test_substitute_holidays_observed_lock(self):
        """대체공휴일 — observed=True load-bearing 잠금.

        observed=False면 None이 되는 날짜들. 회귀 시 calendar 도입 의미 상실.
        """
        self.assertTrue(
            is_kr_holiday(date(2026, 5, 25)),
            "5/25 부처님오신날 대체 (KRX 운영 사고 날짜) — observed=True 필수",
        )
        self.assertTrue(
            is_kr_holiday(date(2026, 3, 2)),
            "3/2 삼일절 대체 — observed=True 필수",
        )

    def test_non_holiday_business_days(self):
        """공휴일 아닌 날 (12/31 포함 — 한국 공휴일 아님)."""
        self.assertFalse(is_kr_holiday(date(2026, 5, 28)))   # 목, 평일
        self.assertFalse(is_kr_holiday(date(2026, 12, 31)))  # 연말 — 공휴일 아님

    def test_holiday_name_provenance(self):
        """공휴일 명칭 반환 (provenance) / 비공휴일 None."""
        self.assertIsNotNone(kr_holiday_name(date(2026, 5, 5)))
        self.assertIsNone(kr_holiday_name(date(2026, 5, 28)))


class TestHanaClassify(unittest.TestCase):

    def test_business_day(self):
        self.assertEqual(classify_hana_calendar_day(date(2026, 5, 28)), "business_day")  # 목
        self.assertTrue(is_hana_business_day(date(2026, 5, 28)))
        self.assertEqual(classify_hana_calendar_day(date(2026, 12, 31)), "business_day")  # 연말 영업일

    def test_weekend(self):
        self.assertEqual(classify_hana_calendar_day(date(2026, 5, 30)), "weekend")  # 토
        self.assertEqual(classify_hana_calendar_day(date(2026, 5, 31)), "weekend")  # 일
        self.assertFalse(is_hana_business_day(date(2026, 5, 30)))

    def test_holiday_weekday(self):
        self.assertEqual(classify_hana_calendar_day(date(2026, 5, 5)), "holiday")   # 어린이날 화
        self.assertEqual(classify_hana_calendar_day(date(2026, 5, 1)), "holiday")   # 근로자의날 금
        self.assertEqual(classify_hana_calendar_day(date(2026, 5, 25)), "holiday")  # 대체공휴일 월
        self.assertFalse(is_hana_business_day(date(2026, 5, 5)))

    def test_weekend_precedence_over_holiday(self):
        """주말∩공휴일 → weekend 우선 (2026-03-01 삼일절 = 일요일)."""
        self.assertEqual(classify_hana_calendar_day(date(2026, 3, 1)), "weekend")


if __name__ == "__main__":
    unittest.main()
