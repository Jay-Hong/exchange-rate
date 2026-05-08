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
    StatusTransition,
    classify_sub_session,
    extract_counter_events,
    format_session_end_delta,
    main,
    pair_stale_transitions,
    parse_age,
    parse_log_line,
    parse_status_transition,
    percentile,
    session_at_ts,
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
            # v1.1 — status transition log (정확한 stale 시각 source)
            "2026-05-07 18:04:30 | INFO | x | [kis_ws] status normal → stale",
            "2026-05-07 18:05:00 | INFO | x | [kis_ws] metrics session=CM status=stale "
            "frames_per_min=10 frame_age=65 trade_age=120 quote_age=65 "
            "max_total_gap=65.0 max_trade_gap=120.0 max_quote_gap=65.0 "
            "stale_transitions=1 reconnect_attempts=0",
            "2026-05-07 18:05:50 | INFO | x | [kis_ws] status stale → normal",
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


# ---------------------------------------------------------------------------
# v1.1 — status transition log parser
# ---------------------------------------------------------------------------


class TestParseStatusTransition(unittest.TestCase):

    def test_parses_normal_to_stale(self):
        line = "2026-05-08 05:27:56 | INFO | x:y:z | [kis_ws] status normal → stale"
        tr = parse_status_transition(line)
        self.assertIsNotNone(tr)
        self.assertEqual(tr.ts, datetime(2026, 5, 8, 5, 27, 56))
        self.assertEqual(tr.from_status, "normal")
        self.assertEqual(tr.to_status, "stale")

    def test_parses_stale_to_normal(self):
        line = "2026-05-08 05:28:04 | INFO | x | [kis_ws] status stale → normal"
        tr = parse_status_transition(line)
        self.assertEqual(tr.from_status, "stale")
        self.assertEqual(tr.to_status, "normal")

    def test_returns_none_for_metric_line(self):
        line = (
            "2026-05-07 17:57:41 | INFO | x | [kis_ws] metrics session=CM "
            "status=normal frames_per_min=44 frame_age=0 trade_age=None "
            "quote_age=0 max_total_gap=7.4 max_trade_gap=0.0 max_quote_gap=7.4 "
            "stale_transitions=0 reconnect_attempts=0"
        )
        self.assertIsNone(parse_status_transition(line))

    def test_returns_none_for_unrelated(self):
        self.assertIsNone(parse_status_transition("random line"))


# ---------------------------------------------------------------------------
# v1.1 — sub-session classification
# ---------------------------------------------------------------------------


class TestClassifySubSession(unittest.TestCase):

    def test_cf_open_auction(self):
        ts = datetime(2026, 5, 8, 8, 35, 0)
        self.assertEqual(classify_sub_session(ts, "CF"), "CF_OPEN_AUCTION")

    def test_cf_continuous_morning(self):
        ts = datetime(2026, 5, 8, 9, 0, 0)
        self.assertEqual(classify_sub_session(ts, "CF"), "CF_CONTINUOUS")

    def test_cf_continuous_just_before_close(self):
        ts = datetime(2026, 5, 8, 15, 34, 59)
        self.assertEqual(classify_sub_session(ts, "CF"), "CF_CONTINUOUS")

    def test_cf_close_auction_start(self):
        ts = datetime(2026, 5, 8, 15, 35, 0)
        self.assertEqual(classify_sub_session(ts, "CF"), "CF_CLOSE_AUCTION")

    def test_cf_close_auction_end(self):
        ts = datetime(2026, 5, 8, 15, 45, 0)
        self.assertEqual(classify_sub_session(ts, "CF"), "CF_CLOSE_AUCTION")

    def test_cm_open_auction(self):
        ts = datetime(2026, 5, 8, 17, 55, 0)
        self.assertEqual(classify_sub_session(ts, "CM"), "CM_OPEN_AUCTION")

    def test_cm_continuous_evening(self):
        ts = datetime(2026, 5, 8, 20, 0, 0)
        self.assertEqual(classify_sub_session(ts, "CM"), "CM_CONTINUOUS")

    def test_cm_continuous_dawn(self):
        # 새벽 (00:00~05:50)
        ts = datetime(2026, 5, 8, 5, 30, 0)
        self.assertEqual(classify_sub_session(ts, "CM"), "CM_CONTINUOUS")

    def test_cm_close_auction(self):
        ts = datetime(2026, 5, 8, 5, 55, 0)
        self.assertEqual(classify_sub_session(ts, "CM"), "CM_CLOSE_AUCTION")

    def test_cm_close_auction_end(self):
        ts = datetime(2026, 5, 8, 6, 0, 0)
        self.assertEqual(classify_sub_session(ts, "CM"), "CM_CLOSE_AUCTION")

    def test_unknown_session(self):
        ts = datetime(2026, 5, 8, 12, 0, 0)
        self.assertEqual(classify_sub_session(ts, "XX"), "UNKNOWN")

    def test_break_time_returns_unknown(self):
        # 16:00 — CF/CM 사이 break
        ts = datetime(2026, 5, 8, 16, 0, 0)
        self.assertEqual(classify_sub_session(ts, "CF"), "UNKNOWN")


# ---------------------------------------------------------------------------
# v1.1 — pair_stale_transitions
# ---------------------------------------------------------------------------


class TestPairStaleTransitions(unittest.TestCase):

    def test_paired_normally(self):
        transitions = [
            StatusTransition(datetime(2026, 5, 8, 5, 27, 56), "normal", "stale"),
            StatusTransition(datetime(2026, 5, 8, 5, 28, 4), "stale", "normal"),
        ]
        pairs = pair_stale_transitions(transitions)
        self.assertEqual(len(pairs), 1)
        start, end = pairs[0]
        self.assertEqual(start.ts.second, 56)
        self.assertEqual(end.ts.second, 4)

    def test_multiple_pairs(self):
        transitions = [
            StatusTransition(datetime(2026, 5, 8, 5, 27, 56), "normal", "stale"),
            StatusTransition(datetime(2026, 5, 8, 5, 28, 4), "stale", "normal"),
            StatusTransition(datetime(2026, 5, 8, 5, 33, 23), "normal", "stale"),
            StatusTransition(datetime(2026, 5, 8, 5, 33, 38), "stale", "normal"),
        ]
        pairs = pair_stale_transitions(transitions)
        self.assertEqual(len(pairs), 2)

    def test_unrecovered_stale(self):
        """stale 발화 후 normal 복구 없으면 end=None."""
        transitions = [
            StatusTransition(datetime(2026, 5, 8, 5, 59, 21), "normal", "stale"),
        ]
        pairs = pair_stale_transitions(transitions)
        self.assertEqual(len(pairs), 1)
        start, end = pairs[0]
        self.assertIsNone(end)

    def test_empty(self):
        self.assertEqual(pair_stale_transitions([]), [])


# ---------------------------------------------------------------------------
# v1.1 — format_session_end_delta (Codex BLOCKING fix)
# ---------------------------------------------------------------------------


class TestFormatSessionEndDelta(unittest.TestCase):
    """2분 미만은 초 단위, 그 이상은 분 단위. int 절삭 0분 오해 차단."""

    def test_seconds_under_2min_for_cm_close(self):
        """05:59:21 + CM → 39초 전, '39s'로 표시 (int 절삭 0min 차단)."""
        ts = datetime(2026, 5, 8, 5, 59, 21)
        self.assertEqual(format_session_end_delta(ts, "CM"), "session_end -39s")

    def test_seconds_just_under_120(self):
        """119초 → '119s' (경계)."""
        ts = datetime(2026, 5, 8, 5, 58, 1)  # 06:00까지 119초
        self.assertEqual(format_session_end_delta(ts, "CM"), "session_end -119s")

    def test_minutes_at_120(self):
        """120초 → '2 min' (경계 — 분 단위 진입)."""
        ts = datetime(2026, 5, 8, 5, 58, 0)  # 06:00까지 정확히 120초
        self.assertEqual(format_session_end_delta(ts, "CM"), "session_end -2 min")

    def test_cf_minutes(self):
        """CF 09:00 → 종료 15:45까지 405분."""
        ts = datetime(2026, 5, 8, 9, 0, 0)
        self.assertEqual(format_session_end_delta(ts, "CF"), "session_end -405 min")

    def test_unknown_session_none(self):
        ts = datetime(2026, 5, 8, 12, 0, 0)
        self.assertIsNone(format_session_end_delta(ts, "XX"))


# ---------------------------------------------------------------------------
# v1.1 — session_at_ts (Codex robustness 권고)
# ---------------------------------------------------------------------------


class TestSessionAtTs(unittest.TestCase):

    @staticmethod
    def _row(ts: datetime, session: str) -> MetricRow:
        return MetricRow(
            ts=ts, session=session, status="normal", frames_per_min=44,
            frame_age=0.0, trade_age=None, quote_age=0.0,
            max_total_gap=7.4, max_trade_gap=0.0, max_quote_gap=7.4,
            stale_transitions=0, reconnect_attempts=0,
        )

    def test_uses_nearest_prior_metric_row(self):
        """1순위: ts 이전 가장 가까운 metric row의 session."""
        rows = [
            self._row(datetime(2026, 5, 8, 5, 27, 0), "CM"),
        ]
        # 5/8 05:27:56 stale → 인접 metric row CM 사용
        self.assertEqual(
            session_at_ts(datetime(2026, 5, 8, 5, 27, 56), rows),
            "CM",
        )

    def test_fallback_timestamp_cf_morning(self):
        """metric row 없을 때 timestamp 기반 fallback — 09:00 → CF."""
        ts = datetime(2026, 5, 8, 9, 0, 0)
        self.assertEqual(session_at_ts(ts, []), "CF")

    def test_fallback_timestamp_cm_evening(self):
        """metric row 없을 때 timestamp fallback — 18:30 → CM."""
        ts = datetime(2026, 5, 8, 18, 30, 0)
        self.assertEqual(session_at_ts(ts, []), "CM")

    def test_fallback_timestamp_cm_dawn(self):
        """metric row 없을 때 timestamp fallback — 05:30 → CM."""
        ts = datetime(2026, 5, 8, 5, 30, 0)
        self.assertEqual(session_at_ts(ts, []), "CM")

    def test_fallback_break_unknown(self):
        """휴장 break (16:00) — fallback UNKNOWN."""
        ts = datetime(2026, 5, 8, 16, 0, 0)
        self.assertEqual(session_at_ts(ts, []), "UNKNOWN")

    def test_metric_row_after_ts_ignored(self):
        """ts 이후 metric row만 있으면 fallback 사용."""
        rows = [
            self._row(datetime(2026, 5, 8, 9, 5, 0), "CF"),  # ts보다 늦음
        ]
        # ts=08:31 (fallback CF) — metric row 미사용
        self.assertEqual(
            session_at_ts(datetime(2026, 5, 8, 8, 31, 0), rows),
            "CF",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
