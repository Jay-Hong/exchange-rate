"""후보 검색 정책(순수) — 종료 원인과 그 근거를 잠근다.

`search_candidates` 는 HTTP·DB 없이 돈다. 여기 시험은 **어떤 사실을 들고 나오는가**만
본다 — 그 사실을 어떤 status/reason 으로 접을지는 호출자 몫이고 별도 시험이 본다.
"""

import datetime
import unittest

from app.ibk_candidate_policy import (
    CandidateSearchStop,
    search_candidates,
)
from app.ibk_run_context import KST

FRI = datetime.date(2026, 8, 28)
THU = datetime.date(2026, 8, 27)
PAYLOAD = ({"usd-krw": 1382.0}, "05:59:55")


def _at(day, hour, month=8):
    return datetime.datetime(2026, month, day, hour, 10, tzinfo=KST)


def _search(reference, fetch, *, budget=True, max_days_back=10,
            classify=lambda exc, day: None):
    return search_candidates(
        reference, max_days_back=max_days_back, fetch=fetch,
        has_budget=(lambda: True) if budget else (lambda: False), classify=classify,
    )


class SearchStopTest(unittest.TestCase):
    def test_first_valid_candidate_stops_the_search(self):
        seen = []

        def fetch(day):
            seen.append(day)
            return PAYLOAD

        result = _search(_at(28, 12), fetch)
        self.assertIs(result.stop, CandidateSearchStop.ACCEPTED)
        self.assertEqual(result.service_date, FRI)
        self.assertIs(result.payload, PAYLOAD)
        self.assertEqual(seen, [FRI], "유효 후보를 얻으면 더 조회하지 않는다")

    def test_all_no_session_exhausts_the_horizon(self):
        result = _search(_at(28, 12), lambda day: None)
        self.assertIs(result.stop, CandidateSearchStop.HORIZON_EXHAUSTED)
        self.assertTrue(result.saw_no_session)
        self.assertIsNone(result.service_date)

    def test_no_budget_stops_before_the_first_request(self):
        called = []
        result = _search(_at(28, 12), lambda day: called.append(day) or PAYLOAD, budget=False)

        self.assertIs(result.stop, CandidateSearchStop.BUDGET_EXHAUSTED)
        self.assertEqual(called, [], "예산이 없으면 요청 자체를 시작하지 않는다")
        self.assertEqual(result.attempted, 0)

    def test_budget_is_checked_before_each_request_not_retroactively(self):
        """마지막 후보까지 다 보고 나서 소급 판정하면 legacy 와 달라진다."""
        budget = iter([True, True, False])
        seen = []
        result = search_candidates(
            _at(28, 12), max_days_back=10,
            fetch=lambda day: seen.append(day) or None,
            has_budget=lambda: next(budget), classify=lambda exc, day: None,
        )
        self.assertIs(result.stop, CandidateSearchStop.BUDGET_EXHAUSTED)
        self.assertEqual(len(seen), 2, "예산이 끊긴 시점까지만 요청한다")
        self.assertTrue(result.saw_no_session, "그 전에 본 무고시 사실은 유지된다")

    def test_a_technical_failure_stops_and_carries_its_reason(self):
        def fetch(day):
            raise RuntimeError("net")

        result = _search(_at(28, 12), fetch, classify=lambda exc, day: "TRANSPORT")
        self.assertIs(result.stop, CandidateSearchStop.TECHNICAL_FAILURE)
        self.assertEqual(result.failure_reason, "TRANSPORT")
        self.assertEqual(result.attempted, 1, "기술 실패에서는 더 과거로 가지 않는다")

    def test_a_semantic_rejection_continues_to_older_candidates(self):
        seen = []

        def fetch(day):
            seen.append(day)
            if day == FRI:
                raise RuntimeError("preopen")
            return PAYLOAD

        result = _search(_at(28, 12), fetch, classify=lambda exc, day: None)
        self.assertIs(result.stop, CandidateSearchStop.ACCEPTED)
        self.assertEqual(result.service_date, THU)
        self.assertTrue(result.saw_preopen_pending)
        self.assertEqual(seen, [FRI, THU])

    def test_classify_receives_the_actual_query_date(self):
        """기대 날짜로 고정해 분류하면 과거 후보의 표 부재를 개장 전으로 오인한다."""
        seen = []

        def fetch(day):
            raise RuntimeError("absent")

        def classify(exc, day):
            seen.append(day)
            return None if day == FRI else "AMBIGUOUS"

        result = _search(_at(28, 12), fetch, classify=classify)
        self.assertEqual(seen, [FRI, THU], "각 후보의 실제 날짜로 분류해야 한다")
        self.assertIs(result.stop, CandidateSearchStop.TECHNICAL_FAILURE)
        self.assertEqual(result.failure_reason, "AMBIGUOUS")

    def test_an_unclassifiable_exception_propagates(self):
        """classify 가 감당 못 하는 예외를 삼키면 원인이 사라진다."""
        def fetch(day):
            raise KeyError("unknown")

        def classify(exc, day):
            raise exc

        with self.assertRaises(KeyError):
            _search(_at(28, 12), fetch, classify=classify)


class WeekendEvidenceTest(unittest.TestCase):
    """달력상 비세션일만 건너뛰어 과거에 닿은 사실을 별도로 들고 나오는지."""

    def test_a_weekend_skip_does_not_fake_a_no_session_response(self):
        """토요일 실행은 금요일 후보에 주말 skip 으로 닿는다 — 무고시 응답은 받은 적 없다."""
        result = _search(_at(29, 12), lambda day: PAYLOAD)

        self.assertIs(result.stop, CandidateSearchStop.ACCEPTED)
        self.assertEqual(result.service_date, FRI, "첫 평일 후보는 금요일이다")
        self.assertFalse(result.saw_no_session, "HTTP 무고시 응답을 받은 적이 없다")
        self.assertFalse(result.saw_preopen_pending)
        self.assertEqual(result.attempted, 1, "주말은 요청 없이 건너뛴다")

    def test_an_all_weekend_plan_reports_horizon_exhausted_without_requests(self):
        result = _search(_at(30, 12), lambda day: PAYLOAD, max_days_back=1)  # 일요일, 범위 전부 주말
        self.assertIs(result.stop, CandidateSearchStop.HORIZON_EXHAUSTED)
        self.assertEqual(result.attempted, 0)
        self.assertFalse(result.saw_no_session)


if __name__ == "__main__":
    unittest.main()
