"""Step 4B 단위 1 — N-contract chain sequence (calendar 순수 계산) 단위 테스트.

master / fetch / DB 의존 0 — _compute_expiry_date(셋째 월요일 + 휴장 보정)만 사용.
boundary 중심 (KRX_STEP4B_PLAN.md §12 Resolved):
  - 일반일 / 만기일 당일=next / 휴장일 segment=calendar
  - window_end 직전 current만 포함, next 제외
  - 1월/12월 year rollover
  - partition 연속성 (segment_end[i] == segment_start[i+1], 겹침/gap 0)
  - window_start ∈ C_0 segment / 반전 window / 1일 window
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_kis_source_daily_rates as B  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


class TestResolveFrontMonth(unittest.TestCase):
    def test_general_day_before_expiry(self):
        # 2026-05 만기(셋째 월요일)는 2026-05-18. 그 전 2026-05-15는 5월물.
        c = B._resolve_front_month(date(2026, 5, 15))
        self.assertEqual(c.contract_month, "202605")
        self.assertEqual(c.short_code, "A75605")

    def test_expiry_day_resolves_to_next(self):
        # 만기일 당일 = next contract (boundary=next, invariant 2)
        expiry = B._compute_expiry_date("202605")
        self.assertEqual(expiry, date(2026, 5, 18))
        c = B._resolve_front_month(expiry)
        self.assertEqual(c.contract_month, "202606")
        self.assertEqual(c.short_code, "A75606")

    def test_after_expiry_resolves_to_next(self):
        c = B._resolve_front_month(date(2026, 5, 19))
        self.assertEqual(c.contract_month, "202606")

    def test_holiday_resolves_by_calendar(self):
        # 일요일(휴장)이어도 segment는 calendar 기준 resolve (거래일 무관, 2-layer 분리)
        d = date(2026, 5, 17)  # Sunday, 2026-05-18 만기 전
        self.assertEqual(d.weekday(), 6)
        c = B._resolve_front_month(d)
        self.assertEqual(c.contract_month, "202605")


class TestHolidayAdjustedExpiryBoundary(unittest.TestCase):
    """휴장 보정 만기일이 chain segment 경계에 전파되는지 (2026-02 / 2026-08).

    `build_contract_sequence`가 `_compute_expiry_date`를 직접 소비하므로
    (`scripts/backfill_kis_source_daily_rates.py`의 import), 만기 보정은 런타임
    resolver뿐 아니라 백필 segment 경계에도 그대로 반영돼야 한다.

    무보정 구현에서는 8/14·2/13이 아직 만기월물 구간이라 전부 FAIL 한다.
    """

    def test_2026_08_expiry_day_is_08_14_and_resolves_next(self):
        # 실제 최종거래일 8/14(금) — 8/17(월)은 광복절 대체공휴일
        self.assertEqual(B._compute_expiry_date("202608"), date(2026, 8, 14))
        c = B._resolve_front_month(date(2026, 8, 14))
        self.assertEqual(c.contract_month, "202609")  # boundary=next

    def test_2026_08_13_still_current_month(self):
        c = B._resolve_front_month(date(2026, 8, 13))
        self.assertEqual(c.contract_month, "202608")

    def test_2026_02_expiry_day_is_02_13_and_resolves_next(self):
        # 실제 최종거래일 2/13(금) — 2/16(월)은 설 연휴
        self.assertEqual(B._compute_expiry_date("202602"), date(2026, 2, 13))
        c = B._resolve_front_month(date(2026, 2, 13))
        self.assertEqual(c.contract_month, "202603")

    def test_2026_02_12_still_current_month(self):
        c = B._resolve_front_month(date(2026, 2, 12))
        self.assertEqual(c.contract_month, "202602")

    def test_shifted_boundary_weekend_belongs_to_next(self):
        """앞당겨진 만기일 이후 주말(달력일)도 차월물 segment에 속한다.

        2026-08-14(금) 만기 → 8/15(토)·8/16(일)·8/17(월 대체휴일)은 모두 9월물 구간.
        segment는 거래일이 아니라 calendar date 기준이므로 휴장일도 포함된다.
        """
        for d in (date(2026, 8, 15), date(2026, 8, 16), date(2026, 8, 17)):
            with self.subTest(d=d):
                self.assertEqual(B._resolve_front_month(d).contract_month, "202609")

    def test_shifted_boundary_segment_contiguous(self):
        """보정된 경계에서도 segment 반열린 연속성(겹침/gap 0)이 유지된다."""
        seq = B.build_contract_sequence(date(2026, 1, 1), date(2026, 12, 31))
        by_month = {c.contract_month: (s, e) for c, s, e in seq}
        self.assertEqual(by_month["202602"][1], date(2026, 2, 13))   # 2월물 segment_end
        self.assertEqual(by_month["202603"][0], date(2026, 2, 13))   # 3월물 segment_start
        self.assertEqual(by_month["202608"][1], date(2026, 8, 14))
        self.assertEqual(by_month["202609"][0], date(2026, 8, 14))
        for i in range(len(seq) - 1):
            self.assertEqual(seq[i][2], seq[i + 1][1])


class TestMonthRollover(unittest.TestCase):
    def test_next_month_dec_to_jan(self):
        self.assertEqual(B._next_month(2025, 12), (2026, 1))

    def test_prev_month_jan_to_dec(self):
        self.assertEqual(B._prev_month(2026, 1), (2025, 12))

    def test_make_contract_jan_short_code(self):
        c = B._make_usd_futures_contract(2026, 1)
        self.assertEqual(c.contract_month, "202601")
        self.assertEqual(c.short_code, "A75601")

    def test_resolve_front_month_across_year_boundary(self):
        # 2025-12 만기일 → 2026-01물 (year rollover + boundary=next)
        expiry_dec = B._compute_expiry_date("202512")
        c = B._resolve_front_month(expiry_dec)
        self.assertEqual(c.contract_month, "202601")
        self.assertEqual(c.short_code, "A75601")


class TestCoverageWindow(unittest.TestCase):
    def test_today_minus_1_end_and_one_year_start(self):
        now = datetime(2026, 6, 3, 10, 0, tzinfo=KST)
        start, end = B._compute_coverage_window(now)
        self.assertEqual(end, date(2026, 6, 2))   # today - 1
        self.assertEqual(start, date(2025, 6, 2))  # 1년 전

    def test_leap_day_end_adjusts_start_to_28(self):
        # today=2028-03-01 → end=2028-02-29(윤년) → start 2027-02-29 불가 → 2027-02-28
        now = datetime(2028, 3, 1, 10, 0, tzinfo=KST)
        start, end = B._compute_coverage_window(now)
        self.assertEqual(end, date(2028, 2, 29))
        self.assertEqual(start, date(2027, 2, 28))


class TestBuildContractSequence(unittest.TestCase):
    WS = date(2025, 6, 2)
    WE = date(2026, 6, 2)

    def test_window_start_in_first_segment(self):
        seq = B.build_contract_sequence(self.WS, self.WE)
        self.assertTrue(seq)
        _, seg_start, seg_end = seq[0]
        self.assertLessEqual(seg_start, self.WS)
        self.assertLess(self.WS, seg_end)  # window_start ∈ [seg_start, seg_end)

    def test_partition_contiguous_no_overlap_no_gap(self):
        seq = B.build_contract_sequence(self.WS, self.WE)
        self.assertGreaterEqual(len(seq), 12)  # 1년 ≈ 12~13 contracts
        for i in range(len(seq) - 1):
            _, _, seg_end_i = seq[i]
            _, seg_start_next, _ = seq[i + 1]
            self.assertEqual(seg_end_i, seg_start_next)  # 반열린 연속 (겹침/gap 0)

    def test_window_end_current_included_next_excluded(self):
        seq = B.build_contract_sequence(self.WS, self.WE)
        _, last_seg_start, last_seg_end = seq[-1]
        self.assertLessEqual(last_seg_start, self.WE)  # 마지막 segment가 window_end 덮음
        self.assertGreater(last_seg_end, self.WE)      # next의 segment_start > window_end → 제외

    def test_segments_monotonic_increasing(self):
        seq = B.build_contract_sequence(self.WS, self.WE)
        starts = [s for _, s, _ in seq]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(starts), len(set(starts)))  # 단조 증가 + 중복 0

    def test_window_start_equals_expiry_first_is_next(self):
        # window_start == 만기일 → 첫 contract는 next, segment_start = 만기일 (boundary=next)
        expiry = B._compute_expiry_date("202506")  # 2025-06 만기
        seq = B.build_contract_sequence(expiry, self.WE)
        first_contract, seg_start, _ = seq[0]
        self.assertEqual(first_contract.contract_month, "202507")
        self.assertEqual(seg_start, expiry)

    def test_reversed_window_raises(self):
        with self.assertRaises(ValueError):
            B.build_contract_sequence(self.WE, self.WS)

    def test_single_day_window(self):
        d = date(2026, 5, 15)  # 2026-05물 구간 내 거래일
        seq = B.build_contract_sequence(d, d)
        self.assertEqual(len(seq), 1)
        self.assertEqual(seq[0][0].contract_month, "202605")


if __name__ == "__main__":
    unittest.main()
