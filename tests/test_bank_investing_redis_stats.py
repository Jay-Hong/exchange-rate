"""bank_investing_redis_stats — Bank/Investing SET-outcome telemetry (item 4) unit tests.

process-local + Lock 모듈. per-(source,asset) + derived aggregate / 실패 3종 분류 /
consecutive_failures / last_* / 예외 비전파 / deepcopy 격리를 잠근다.
"""
from __future__ import annotations

import unittest

from app import bank_investing_redis_stats as stats


class TestBankInvestingRedisStats(unittest.TestCase):

    def setUp(self):
        stats.reset_stats()

    def tearDown(self):
        stats.reset_stats()

    def test_empty_state(self):
        out = stats.get_stats()
        self.assertIn("started_at", out)
        self.assertEqual(out["per_source"], {})
        self.assertEqual(out["aggregate"]["attempt"], 0)
        self.assertEqual(out["aggregate"]["success"], 0)
        self.assertEqual(out["aggregate"]["failure"], 0)

    def test_attempt_success_recorded_per_source_asset(self):
        stats.record_attempt("kb", "usd-krw")
        stats.record_success("kb", "usd-krw")
        a = stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]
        self.assertEqual(a["attempt"], 1)
        self.assertEqual(a["success"], 1)
        self.assertEqual(a["failure"], 0)
        self.assertEqual(a["consecutive_failures"], 0)
        self.assertIsNotNone(a["last_attempt_at"])
        self.assertIsNotNone(a["last_success_at"])

    def test_failure_reasons_classified(self):
        for reason in stats.FAILURE_REASONS:
            stats.record_attempt("hana", "jpy-krw")
            stats.record_failure("hana", "jpy-krw", reason, error=f"{reason} boom")
        a = stats.get_stats()["per_source"]["hana"]["per_asset"]["jpy-krw"]
        self.assertEqual(a["failure"], 3)
        for reason in stats.FAILURE_REASONS:
            self.assertEqual(a["failure_by_reason"][reason], 1)
        self.assertEqual(a["consecutive_failures"], 3)
        self.assertEqual(a["last_failure_reason"], stats.FAILURE_REASONS[-1])
        self.assertIsNotNone(a["last_failure_error"])
        self.assertEqual(a["failure"], sum(a["failure_by_reason"].values()))

    def test_consecutive_failures_reset_on_success(self):
        stats.record_failure("kb", "usd-krw", "set_exception")
        stats.record_failure("kb", "usd-krw", "set_exception")
        self.assertEqual(
            stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]["consecutive_failures"],
            2,
        )
        stats.record_success("kb", "usd-krw")
        self.assertEqual(
            stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]["consecutive_failures"],
            0,
        )

    def test_aggregate_rollup_source_and_top(self):
        stats.record_attempt("kb", "usd-krw"); stats.record_success("kb", "usd-krw")
        stats.record_attempt("kb", "jpy-krw"); stats.record_failure("kb", "jpy-krw", "client_unavailable")
        stats.record_attempt("investing", "usd-krw"); stats.record_success("investing", "usd-krw")
        out = stats.get_stats()
        kb = out["per_source"]["kb"]["aggregate"]
        self.assertEqual((kb["attempt"], kb["success"], kb["failure"]), (2, 1, 1))
        self.assertEqual(kb["failure_by_reason"]["client_unavailable"], 1)
        top = out["aggregate"]
        self.assertEqual((top["attempt"], top["success"], top["failure"]), (3, 2, 1))

    def test_last_failure_error_bounded(self):
        stats.record_failure("kb", "usd-krw", "set_exception", error="x" * 1000)
        a = stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]
        self.assertLessEqual(len(a["last_failure_error"]), stats._LAST_ERROR_MAX_LEN)

    def test_unknown_reason_normalized_to_unknown_bucket(self):
        stats.record_failure("kb", "usd-krw", "weird_reason")
        a = stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]
        self.assertEqual(a["failure"], 1)
        self.assertEqual(a["failure_by_reason"]["unknown"], 1)  # 정규화
        self.assertEqual(a["last_failure_reason"], "weird_reason")  # 원 문자열 보존
        self.assertEqual(a["failure"], sum(a["failure_by_reason"].values()))  # 불변식

    def test_record_never_raises_on_bad_input(self):
        """unhashable key 등 비정상 입력에도 예외 비전파 (writer hot path 보호)."""
        bad = ["unhashable"]
        try:
            stats.record_attempt(bad, "usd-krw")  # type: ignore[arg-type]
            stats.record_success(bad, "usd-krw")  # type: ignore[arg-type]
            stats.record_failure(bad, "usd-krw", "set_exception")  # type: ignore[arg-type]
        except Exception as e:  # pragma: no cover
            self.fail(f"record_* raised on bad input: {e!r}")

    def test_get_stats_deepcopy_isolation(self):
        stats.record_attempt("kb", "usd-krw")
        out = stats.get_stats()
        out["per_source"]["kb"]["per_asset"]["usd-krw"]["attempt"] = 999
        self.assertEqual(
            stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]["attempt"],
            1,
        )

    def test_failure_equals_sum_by_reason_invariant(self):
        """failure == sum(failure_by_reason) — 알려진+미등록 reason 혼합에도 유지."""
        stats.record_failure("kb", "usd-krw", "client_unavailable")
        stats.record_failure("kb", "usd-krw", "set_exception")
        stats.record_failure("kb", "usd-krw", "writer_exception")
        stats.record_failure("kb", "usd-krw", "weird")  # → unknown
        out = stats.get_stats()
        a = out["per_source"]["kb"]["per_asset"]["usd-krw"]
        self.assertEqual(a["failure"], 4)
        self.assertEqual(a["failure"], sum(a["failure_by_reason"].values()))
        top = out["aggregate"]
        self.assertEqual(top["failure"], sum(top["failure_by_reason"].values()))

    def test_last_failure_error_paired_with_reason(self):
        """(reason, error)는 항상 동일 최신 사건 — error 없는 후속 실패가 과거 메시지 제거."""
        stats.record_failure("kb", "usd-krw", "set_exception", error="TimeoutError: boom")
        a = stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]
        self.assertEqual(a["last_failure_error"], "TimeoutError: boom")  # 선행 사건
        stats.record_failure("kb", "usd-krw", "client_unavailable")  # error=None
        a = stats.get_stats()["per_source"]["kb"]["per_asset"]["usd-krw"]
        self.assertEqual(a["last_failure_reason"], "client_unavailable")
        self.assertIsNone(a["last_failure_error"])  # 과거 set_exception 메시지 잔존 X

    def test_aggregate_counters_only(self):
        """aggregate는 카운터만 — consecutive_failures/last_* 미포함 (계약 잠금)."""
        stats.record_attempt("kb", "usd-krw")
        stats.record_failure("kb", "usd-krw", "set_exception")
        out = stats.get_stats()
        for agg in (out["aggregate"], out["per_source"]["kb"]["aggregate"]):
            self.assertEqual(
                set(agg.keys()), {"attempt", "success", "failure", "failure_by_reason"}
            )


if __name__ == "__main__":
    unittest.main()
