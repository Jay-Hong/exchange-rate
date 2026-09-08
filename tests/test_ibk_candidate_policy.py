"""IBK 후보 날짜 계획 — legacy 와 새 생성기가 공유할 순서를 잠근다.

이 정책이 조용히 넓어지거나 좁아지면 "왜 그 날짜를 조회했나" 를 사후에 설명할 수 없다.
특히 `max_days_back` 은 달력 범위이고 주말이 이를 소비한다 — 평일 후보 수로 재정의하면
탐색이 넓어진다. 그 의미를 여기서 못 박는다.
"""

import datetime
import unittest

from app.crawlers.constants import MAX_DAYS_LOOKBACK
from app.ibk_candidate_policy import plan_candidate_dates
from app.ibk_run_context import KST, SERVICE_DATE_ROLLOVER_TIME

FRIDAY = datetime.date(2026, 8, 28)


def _at(year, month, day, hour, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=KST)


class CandidateDatePlanTest(unittest.TestCase):
    def test_the_rollover_boundary_decides_whether_today_is_a_candidate(self):
        before = plan_candidate_dates(_at(2026, 8, 28, 7, 59), max_days_back=3)
        after = plan_candidate_dates(_at(2026, 8, 28, 8, 0), max_days_back=3)

        self.assertNotIn(FRIDAY, before, "08:00 전에는 당일이 아직 조회기준일이 아니다")
        self.assertEqual(after[0], FRIDAY, "08:00 부터 당일이 첫 후보다")

    def test_the_boundary_is_the_single_sourced_constant(self):
        """부모의 expected_service_date 와 같은 상수를 쓰는지 잠근다."""
        edge = datetime.datetime.combine(FRIDAY, SERVICE_DATE_ROLLOVER_TIME, tzinfo=KST)
        self.assertEqual(plan_candidate_dates(edge, max_days_back=0), (FRIDAY,))

    def test_candidates_run_from_newest_to_oldest(self):
        plan = plan_candidate_dates(_at(2026, 8, 28, 12), max_days_back=5)
        self.assertEqual(list(plan), sorted(plan, reverse=True))

    def test_weekends_are_skipped_but_still_consume_the_calendar_range(self):
        """⛔ 이 시험이 이 모듈의 핵심 계약이다 — 달력 범위지 평일 개수가 아니다."""
        plan = plan_candidate_dates(_at(2026, 8, 28, 12), max_days_back=10)

        self.assertEqual(len(plan), 9, "달력 11일에서 주말 2일을 빼면 평일 9개다")
        self.assertEqual(plan[-1], datetime.date(2026, 8, 18), "하한은 달력 10일 전이다")
        self.assertTrue(all(day.weekday() < 5 for day in plan))

    def test_a_weekend_reference_still_bounds_by_the_calendar_range(self):
        """주말에 돌더라도 하한이 달력 기준으로 유지된다 — 주말이 범위를 소비한다."""
        saturday = plan_candidate_dates(_at(2026, 8, 29, 12), max_days_back=10)
        self.assertEqual(saturday[0], FRIDAY, "토요일 08:00 이후의 첫 후보는 금요일이다")
        self.assertEqual(saturday[-1], datetime.date(2026, 8, 19))

    def test_max_days_back_zero_yields_at_most_today(self):
        self.assertEqual(plan_candidate_dates(_at(2026, 8, 28, 12), max_days_back=0), (FRIDAY,))
        self.assertEqual(plan_candidate_dates(_at(2026, 8, 28, 3), max_days_back=0), ())

    def test_a_weekend_only_range_is_empty_rather_than_reaching_further_back(self):
        """범위 안이 전부 주말이면 빈 계획이다 — 몰래 더 과거로 가지 않는다."""
        self.assertEqual(plan_candidate_dates(_at(2026, 8, 30, 12), max_days_back=1), ())

    def test_the_plan_does_not_read_the_clock(self):
        """같은 reference_time 이면 언제 불러도 같은 계획이어야 한다 — 실행 중 자정을 넘겨도."""
        moment = _at(2026, 8, 28, 23, 59)
        self.assertEqual(
            plan_candidate_dates(moment, max_days_back=MAX_DAYS_LOOKBACK),
            plan_candidate_dates(moment, max_days_back=MAX_DAYS_LOOKBACK),
        )

    def test_the_reference_time_is_normalised_to_kst(self):
        """UTC 로 줘도 KST 기준일로 계획한다 — 부모는 UTC 로 시각을 넘긴다."""
        utc = datetime.datetime(2026, 8, 27, 23, 30, tzinfo=datetime.timezone.utc)  # KST 08-28 08:30
        self.assertEqual(plan_candidate_dates(utc, max_days_back=0), (FRIDAY,))

    def test_a_naive_reference_time_is_rejected(self):
        with self.assertRaises(ValueError):
            plan_candidate_dates(datetime.datetime(2026, 8, 28, 12), max_days_back=1)

    def test_an_invalid_range_is_rejected(self):
        for bad in (-1, 1.5, True, "3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                plan_candidate_dates(_at(2026, 8, 28, 12), max_days_back=bad)


class LegacyEquivalenceTest(unittest.TestCase):
    """추출이 행위를 바꾸지 않았는지 — 구 계산을 동결 사본으로 두고 전수 대조한다."""

    @staticmethod
    def _frozen_legacy(reference_time, max_days_back):
        local = reference_time.astimezone(KST)
        reference_date = local.date()
        first_days_back = 0 if local.time() >= datetime.time(8, 0) else 1
        out = []
        for days_back in range(first_days_back, max_days_back + 1):
            query_date = reference_date - datetime.timedelta(days=days_back)
            if query_date.weekday() >= 5:
                continue
            out.append(query_date)
        return tuple(out)

    def test_matches_the_frozen_legacy_calculation_across_a_year_of_hours(self):
        base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for hours in range(0, 24 * 370, 7):
            moment = base + datetime.timedelta(hours=hours)
            with self.subTest(moment=moment.isoformat()):
                self.assertEqual(
                    plan_candidate_dates(moment, max_days_back=MAX_DAYS_LOOKBACK),
                    self._frozen_legacy(moment, MAX_DAYS_LOOKBACK),
                )


if __name__ == "__main__":
    unittest.main()
