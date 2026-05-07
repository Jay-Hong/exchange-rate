"""KRX baseline extractor 단위 테스트.

`scripts/krx_baseline_extract.py` v1 (log-only) parser + summary 검증.
"""
from __future__ import annotations

import io
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# scripts/ 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from krx_baseline_extract import (  # noqa: E402
    MetricRow,
    extract_counter_events,
    main,
    parse_age,
    parse_log_line,
    percentile,
    session_end_minutes,
    summarize_session,
)


SAMPLE_METRIC_LINE = (
    "2026-05-07 17:57:41 | INFO | exchange_rate.scheduler:_summary_log_loop:788 | "
    "[kis_ws] metrics session=CM status=normal frames_per_min=44 frame_age=0 "
    "trade_age=None quote_age=0 max_total_gap=7.4 max_trade_gap=0.0 "
    "max_quote_gap=7.4 stale_transitions=0 reconnect_attempts=0"
)


# ---------------------------------------------------------------------------
# parse_age
# ---------------------------------------------------------------------------


class TestParseAge(unittest.TestCase):

    def test_none_string(self):
        self.assertIsNone(parse_age("None"))

    def test_zero(self):
        self.assertEqual(parse_age("0"), 0.0)

    def test_float(self):
        self.assertEqual(parse_age("17.5"), 17.5)

    def test_invalid(self):
        self.assertIsNone(parse_age("invalid"))


# ---------------------------------------------------------------------------
# parse_log_line
# ---------------------------------------------------------------------------


class TestParseLogLine(unittest.TestCase):

    def test_parses_metric_line(self):
        r = parse_log_line(SAMPLE_METRIC_LINE)
        self.assertIsNotNone(r)
        self.assertEqual(r.ts, datetime(2026, 5, 7, 17, 57, 41))
        self.assertEqual(r.session, "CM")
        self.assertEqual(r.status, "normal")
        self.assertEqual(r.frames_per_min, 44)
        self.assertEqual(r.frame_age, 0.0)
        self.assertIsNone(r.trade_age)  # "None" → None
        self.assertEqual(r.quote_age, 0.0)
        self.assertEqual(r.max_total_gap, 7.4)
        self.assertEqual(r.max_trade_gap, 0.0)
        self.assertEqual(r.max_quote_gap, 7.4)
        self.assertEqual(r.stale_transitions, 0)
        self.assertEqual(r.reconnect_attempts, 0)

    def test_returns_none_for_non_metric_line(self):
        self.assertIsNone(parse_log_line("random log line\n"))
        self.assertIsNone(parse_log_line(""))
        self.assertIsNone(
            parse_log_line(
                "2026-05-07 17:57:41 | INFO | other:func | other message"
            )
        )

    def test_parses_stale_status(self):
        line = (
            "2026-05-07 18:05:00 | INFO | x | [kis_ws] metrics session=CM status=stale "
            "frames_per_min=10 frame_age=65 trade_age=120 quote_age=65 "
            "max_total_gap=65.0 max_trade_gap=120.0 max_quote_gap=65.0 "
            "stale_transitions=1 reconnect_attempts=0"
        )
        r = parse_log_line(line)
        self.assertEqual(r.status, "stale")
        self.assertEqual(r.stale_transitions, 1)


# ---------------------------------------------------------------------------
# session_end_minutes
# ---------------------------------------------------------------------------


class TestSessionEndMinutes(unittest.TestCase):

    def test_cf_morning(self):
        # CF 9:00 → 종료 15:45 (6시간 45분 = 405분)
        ts = datetime(2026, 5, 8, 9, 0)
        self.assertEqual(session_end_minutes(ts, "CF"), 405)

    def test_cf_just_before_close(self):
        ts = datetime(2026, 5, 8, 15, 44)
        self.assertEqual(session_end_minutes(ts, "CF"), 1)

    def test_cm_evening_same_day_to_next(self):
        # CM 18:00 시작 → 다음날 06:00 종료 (12시간 = 720분)
        ts = datetime(2026, 5, 8, 18, 0)
        self.assertEqual(session_end_minutes(ts, "CM"), 720)

    def test_cm_dawn_same_day(self):
        # CM 새벽 03:00 → 같은 날 06:00 종료 (3시간 = 180분)
        ts = datetime(2026, 5, 8, 3, 0)
        self.assertEqual(session_end_minutes(ts, "CM"), 180)

    def test_unknown_session(self):
        self.assertIsNone(session_end_minutes(datetime(2026, 5, 8, 12, 0), "XX"))


# ---------------------------------------------------------------------------
# percentile
# ---------------------------------------------------------------------------


class TestPercentile(unittest.TestCase):

    def test_p50_odd(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3.0)

    def test_p50_even(self):
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)

    def test_p95(self):
        # linear-interp p95 of 1..10 = 9.55
        self.assertAlmostEqual(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 9.55, delta=0.01)

    def test_empty(self):
        self.assertEqual(percentile([], 50), 0.0)

    def test_single(self):
        self.assertEqual(percentile([42], 95), 42.0)


# ---------------------------------------------------------------------------
# extract_counter_events (cumulative delta)
# ---------------------------------------------------------------------------


def _row(ts: datetime, session: str = "CM", stale: int = 0, rc: int = 0) -> MetricRow:
    return MetricRow(
        ts=ts, session=session, status="normal", frames_per_min=44,
        frame_age=0.0, trade_age=None, quote_age=0.0,
        max_total_gap=7.4, max_trade_gap=0.0, max_quote_gap=7.4,
        stale_transitions=stale, reconnect_attempts=rc,
    )


class TestExtractCounterEvents(unittest.TestCase):

    def test_no_change(self):
        rows = [
            _row(datetime(2026, 5, 7, 18, 0), stale=0),
            _row(datetime(2026, 5, 7, 18, 1), stale=0),
        ]
        self.assertEqual(extract_counter_events(rows, "stale_transitions"), [])

    def test_single_increase(self):
        rows = [
            _row(datetime(2026, 5, 7, 18, 0), stale=0),
            _row(datetime(2026, 5, 7, 18, 5), stale=1),  # event
            _row(datetime(2026, 5, 7, 18, 6), stale=1),  # no change
        ]
        events = extract_counter_events(rows, "stale_transitions")
        self.assertEqual(len(events), 1)
        ts, session, delta, cur, _ = events[0]
        self.assertEqual(ts, datetime(2026, 5, 7, 18, 5))
        self.assertEqual(delta, 1)
        self.assertEqual(cur, 1)

    def test_multiple_increases(self):
        rows = [
            _row(datetime(2026, 5, 7, 18, 0), rc=0),
            _row(datetime(2026, 5, 7, 18, 1), rc=1),
            _row(datetime(2026, 5, 7, 18, 2), rc=3),  # delta=2
            _row(datetime(2026, 5, 7, 18, 3), rc=3),  # no change
        ]
        events = extract_counter_events(rows, "reconnect_attempts")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][2], 1)  # delta
        self.assertEqual(events[1][2], 2)


# ---------------------------------------------------------------------------
# summarize_session
# ---------------------------------------------------------------------------


class TestSummarizeSession(unittest.TestCase):

    def test_empty(self):
        out = summarize_session([], "CF")
        self.assertEqual(len(out), 1)
        self.assertIn("0 rows", out[0])

    def test_with_rows_trade_age_all_none(self):
        rows = [
            _row(datetime(2026, 5, 7, 18, 0)),
            _row(datetime(2026, 5, 7, 18, 1)),
        ]
        out = summarize_session(rows, "CM")
        joined = "\n".join(out)
        self.assertIn("2 rows", joined)
        # trade_age 모두 None 명시
        self.assertIn("trade_age: 모두 None", joined)
        # frames_per_min 통계
        self.assertIn("frames_per_min", joined)


# ---------------------------------------------------------------------------
# main() end-to-end (stdin)
# ---------------------------------------------------------------------------


class TestMainStdin(unittest.TestCase):

    def test_stdin_pipeline(self):
        sample = "\n".join([
            SAMPLE_METRIC_LINE,
            "random non-metric line",
            "2026-05-07 18:05:00 | INFO | x | [kis_ws] metrics session=CM status=stale "
            "frames_per_min=10 frame_age=65 trade_age=120 quote_age=65 "
            "max_total_gap=65.0 max_trade_gap=120.0 max_quote_gap=65.0 "
            "stale_transitions=1 reconnect_attempts=0",
            "2026-05-07 18:06:00 | INFO | x | [kis_ws] metrics session=CM status=normal "
            "frames_per_min=80 frame_age=2 trade_age=2 quote_age=2 "
            "max_total_gap=65.0 max_trade_gap=120.0 max_quote_gap=65.0 "
            "stale_transitions=1 reconnect_attempts=1",
        ]) + "\n"

        captured_out = io.StringIO()
        with patch("sys.stdin", io.StringIO(sample)), \
             patch("sys.stdout", captured_out), \
             patch("sys.argv", ["krx_baseline_extract.py"]):
            rc = main()

        self.assertEqual(rc, 0)
        out = captured_out.getvalue()
        # 핵심 출력 검증
        self.assertIn("KRX baseline", out)
        self.assertIn("Total 3 rows", out)
        self.assertIn("CM:", out)
        self.assertIn("stale events: 1", out)
        self.assertIn("reconnect events: 1", out)

    def test_no_matching_lines_returns_1(self):
        captured_err = io.StringIO()
        with patch("sys.stdin", io.StringIO("only random log lines\n")), \
             patch("sys.stderr", captured_err), \
             patch("sys.argv", ["krx_baseline_extract.py"]):
            rc = main()
        self.assertEqual(rc, 1)
        self.assertIn("매칭", captured_err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
