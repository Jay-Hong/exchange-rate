"""Hana backfill throttle + HTTP 429 abort + EXPECTED_DATES_JSON 단위 테스트
(ADR-034 Phase 2d Step 4A Hana 보강 PR).

- throttle: 첫 요청 cooldown + 요청 시작 간격 >= 3.0s (monotonic, **fake-clock — 실제 sleep 없음**)
- 429: HTTP 429 즉시 abort (HanaRateLimitAbort, 남은 fetch 0) / 429 외 HTTPError는 일반 실패 continue
- EXPECTED_DATES_JSON: sorted·unique·machine-parse

외부 DB 의존성 0 (fetch_html/parse_response/build_row mock).
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_hana_source_daily_rates as W  # noqa: E402


def _ok_build(parsed, request_date, currency):
    """정상 build_row mock — request_date == basis_date (휴일 fallback 없음)."""
    return {
        "basis_date": request_date,
        "date_kst": request_date,
        "close_basis": "hana_official_historical_backfill",
    }


class TestThrottle(unittest.TestCase):
    @patch("backfill_hana_source_daily_rates.build_row", side_effect=_ok_build)
    @patch("backfill_hana_source_daily_rates.parse_response", return_value={})
    @patch("backfill_hana_source_daily_rates.fetch_html", return_value="<html>")
    @patch("backfill_hana_source_daily_rates.time.sleep")
    @patch("backfill_hana_source_daily_rates.time.monotonic")
    def test_first_request_cooldown_and_interval(self, mock_mono, mock_sleep, *_):
        # monotonic 호출: init 1 + loop 2/iter × 3일 = 7회
        # [init=0] / loop1(elapsed=0→sleep3, last=3) / loop2(elapsed=0→sleep3, last=6) / loop3(elapsed=0→sleep3, last=9)
        mock_mono.side_effect = [0.0, 0.0, 3.0, 3.0, 6.0, 6.0, 9.0]
        W.fetch_and_dedup_calendar_range("USD", date(2025, 1, 1), date(2025, 1, 3))
        # 첫 요청 cooldown 포함 + 각 요청 → sleep 3회 (3 calendar days), 각 3.0s
        self.assertEqual(mock_sleep.call_count, 3)
        for c in mock_sleep.call_args_list:
            self.assertAlmostEqual(c.args[0], 3.0)

    @patch("backfill_hana_source_daily_rates.build_row", side_effect=_ok_build)
    @patch("backfill_hana_source_daily_rates.parse_response", return_value={})
    @patch("backfill_hana_source_daily_rates.fetch_html", return_value="<html>")
    @patch("backfill_hana_source_daily_rates.time.sleep")
    @patch("backfill_hana_source_daily_rates.time.monotonic")
    def test_no_sleep_if_interval_already_elapsed(self, mock_mono, mock_sleep, *_):
        # elapsed >= 3.0이면 sleep 안 함. 1일: [init=0, elapsed=5.0(>3)→no sleep, last=5.0]
        mock_mono.side_effect = [0.0, 5.0, 5.0]
        W.fetch_and_dedup_calendar_range("USD", date(2025, 1, 1), date(2025, 1, 1))
        self.assertEqual(mock_sleep.call_count, 0)

    @patch("backfill_hana_source_daily_rates.parse_response", return_value={})
    @patch("backfill_hana_source_daily_rates.time.sleep")
    @patch("backfill_hana_source_daily_rates.time.monotonic", side_effect=[float(i) for i in range(100)])
    @patch("backfill_hana_source_daily_rates.fetch_html")
    def test_throttle_held_after_fetch_exception(self, mock_fetch, mock_mono, mock_sleep, mock_parse):
        # 앞 요청이 예외(timeout 등)여도 다음 요청 throttle 유지 (retry storm 방지).
        # 일반 RequestException → fetch_errors 기록 + 다음 날짜 진행. sleep은 loop 상단이라 매 iter 적용.
        mock_fetch.side_effect = requests.ConnectionError("timeout")
        rows, errors, _ = W.fetch_and_dedup_calendar_range("USD", date(2025, 1, 1), date(2025, 1, 2))
        # 2일 모두 fetch 실패해도 각 iter sleep 호출 (예외 후에도 throttle)
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(len(errors), 2)
        self.assertEqual(mock_sleep.call_count, 2)


class Test429Abort(unittest.TestCase):
    @patch("backfill_hana_source_daily_rates.time.sleep")
    @patch("backfill_hana_source_daily_rates.time.monotonic", side_effect=[float(i) for i in range(100)])
    @patch("backfill_hana_source_daily_rates.fetch_html")
    def test_429_immediate_abort_no_further_fetch(self, mock_fetch, *_):
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "60"}
        mock_fetch.side_effect = requests.HTTPError(response=resp)
        with self.assertRaises(W.HanaRateLimitAbort) as ctx:
            W.fetch_and_dedup_calendar_range("USD", date(2025, 1, 1), date(2025, 1, 10))
        # 첫 429에서 즉시 abort → fetch 1회만 (남은 9일 fetch 0)
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertIn("429", str(ctx.exception))
        self.assertIn("60", str(ctx.exception))  # Retry-After 보존

    @patch("backfill_hana_source_daily_rates.build_row", side_effect=_ok_build)
    @patch("backfill_hana_source_daily_rates.parse_response", return_value={})
    @patch("backfill_hana_source_daily_rates.time.sleep")
    @patch("backfill_hana_source_daily_rates.time.monotonic", side_effect=[float(i) for i in range(100)])
    @patch("backfill_hana_source_daily_rates.fetch_html")
    def test_non_429_httperror_continues(self, mock_fetch, *_):
        resp = MagicMock()
        resp.status_code = 404
        mock_fetch.side_effect = requests.HTTPError(response=resp)
        rows, errors, _ = W.fetch_and_dedup_calendar_range("USD", date(2025, 1, 1), date(2025, 1, 3))
        # 404는 rate-limit 아님 → 일반 fetch 실패로 다음 날짜 진행 (abort 없음)
        self.assertEqual(mock_fetch.call_count, 3)
        self.assertEqual(len(errors), 3)
        self.assertEqual(len(rows), 0)


class TestExpectedDatesJson(unittest.TestCase):
    def test_sorted_unique_parse(self):
        rows = [
            {"date_kst": date(2025, 1, 3)},
            {"date_kst": date(2025, 1, 1)},
            {"date_kst": date(2025, 1, 3)},  # 중복
            {"date_kst": date(2025, 1, 2)},
        ]
        out = W.expected_dates_json(rows)
        self.assertTrue(out.startswith("EXPECTED_DATES_JSON="))
        payload = json.loads(out[len("EXPECTED_DATES_JSON="):])
        self.assertEqual(payload, ["2025-01-01", "2025-01-02", "2025-01-03"])  # sorted
        self.assertEqual(len(payload), len(set(payload)))  # unique

    def test_empty(self):
        out = W.expected_dates_json([])
        payload = json.loads(out[len("EXPECTED_DATES_JSON="):])
        self.assertEqual(payload, [])


if __name__ == "__main__":
    unittest.main()
