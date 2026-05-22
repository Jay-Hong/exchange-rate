"""USDT WS baseline extractor 단위 테스트.

`scripts/usdt_ws_baseline_extract.py` v1 (Option A v2, log-only snapshot)
parser + count + format 검증.
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

from usdt_ws_baseline_extract import (  # noqa: E402
    MetricRow,
    SOURCES,
    count_errors,
    count_gopax_fallback,
    extract_latest_snapshot,
    format_snapshot,
    main,
    parse_metric_line,
)


# ---------------------------------------------------------------------------
# Sample lines (실제 production 로그 형식)
# ---------------------------------------------------------------------------

UPBIT_LINE = (
    "exchange-rate-app  | 2026-05-22 14:29:13 | INFO | "
    "exchange_rate.crawler.usdt_ws.upbit:_summary_log_loop:787 | "
    "[usdt_ws.upbit] metrics frames_per_min=45 last_tick_age=0.3 "
    "last_heartbeat_age=19.9 max_frame_gap=9.5 status=normal "
    "reconnect_attempts=0 status_transitions=normal:0/reconnecting:0/stale:0 "
    "redis_saturation_count=0"
)

BITHUMB_LINE = (
    "exchange-rate-app  | 2026-05-22 14:29:13 | INFO | "
    "exchange_rate.crawler.usdt_ws.bithumb:_summary_log_loop:1007 | "
    "[usdt_ws.bithumb] metrics frames_per_min=28 last_tick_age=1.4 "
    "last_heartbeat_age=19.6 max_frame_gap=14.3 status=normal "
    "reconnect_attempts=0 status_transitions=normal:0/reconnecting:0/stale:0 "
    "redis_saturation_count=0 fallback_probe_scheduled_count=0"
)

COINONE_LINE = (
    "exchange-rate-app  | 2026-05-22 14:29:13 | INFO | "
    "exchange_rate.crawler.usdt_ws.coinone:_summary_log_loop:1189 | "
    "[usdt_ws.coinone] metrics frames_per_min=12 last_tick_age=0.7 "
    "last_heartbeat_age=180.6 max_frame_gap=30.0 connection_status=normal "
    "ticker_freshness_status=normal reconnect_attempts=0 "
    "status_transitions=connection_normal:0/connection_reconnecting:0/connection_stale:0/"
    "ticker_normal:0/ticker_warning:0/ticker_degraded:0 "
    "redis_saturation_count=0 fallback_probe_scheduled_count=0"
)

GOPAX_LINE = (
    "exchange-rate-app  | 2026-05-22 14:30:13 | INFO | "
    "exchange_rate.crawler.usdt_ws.gopax:_summary_log_loop:1205 | "
    "[usdt_ws.gopax] metrics frames_per_min=3 last_tick_age=1.1 "
    "last_heartbeat_age=9.2 max_frame_gap=150.4 connection_status=normal "
    "ticker_freshness_status=normal reconnect_attempts=0 "
    "status_transitions=connection_normal:0/connection_reconnecting:0/connection_stale:0/"
    "ticker_normal:0/ticker_warning:0/ticker_degraded:0 "
    "redis_saturation_count=0 fallback_probe_scheduled_count=20"
)

GOPAX_FALLBACK_START = (
    "exchange-rate-app  | 2026-05-22 14:31:00 | INFO | "
    "[usdt_ws.gopax.fallback] probe start (reason=ticker_degraded)"
)

GOPAX_FALLBACK_SUCCESS = (
    "exchange-rate-app  | 2026-05-22 14:31:01 | INFO | "
    "[usdt_ws.gopax.fallback] probe success (rate=1487.0, ts_ms=1, reason=ticker_degraded)"
)

ERROR_LINE = (
    "exchange-rate-app  | 2026-05-22 14:32:00 | ERROR | some.module | "
    "something failed"
)

TRACEBACK_LINE = "Traceback (most recent call last):"


# ---------------------------------------------------------------------------
# parse_metric_line
# ---------------------------------------------------------------------------


class TestParseMetricLine(unittest.TestCase):
    """metrics 라인 파싱 — 5 source 모두 + 비매칭 케이스."""

    def test_upbit_1dim_line(self):
        row = parse_metric_line(UPBIT_LINE)
        self.assertIsNotNone(row)
        self.assertEqual(row.source, "upbit")
        self.assertEqual(row.ts, datetime(2026, 5, 22, 14, 29, 13))
        self.assertEqual(row.fpm, 45)
        self.assertAlmostEqual(row.last_tick_age, 0.3)
        self.assertAlmostEqual(row.last_heartbeat_age, 19.9)
        self.assertAlmostEqual(row.max_frame_gap, 9.5)
        self.assertEqual(row.status, "normal")
        self.assertIsNone(row.connection_status)  # 1-dim source
        self.assertIsNone(row.ticker_freshness_status)
        self.assertEqual(row.reconnect_attempts, 0)
        self.assertEqual(row.redis_saturation_count, 0)
        self.assertIsNone(row.fallback_probe_scheduled_count)  # Upbit는 probe 없음

    def test_bithumb_1dim_with_probe(self):
        row = parse_metric_line(BITHUMB_LINE)
        self.assertIsNotNone(row)
        self.assertEqual(row.source, "bithumb")
        self.assertEqual(row.status, "normal")
        self.assertEqual(row.redis_saturation_count, 0)
        self.assertEqual(row.fallback_probe_scheduled_count, 0)

    def test_coinone_2signal(self):
        row = parse_metric_line(COINONE_LINE)
        self.assertIsNotNone(row)
        self.assertEqual(row.source, "coinone")
        self.assertIsNone(row.status)  # 2-signal source는 1-dim status 없음
        self.assertEqual(row.connection_status, "normal")
        self.assertEqual(row.ticker_freshness_status, "normal")
        self.assertEqual(row.redis_saturation_count, 0)
        self.assertEqual(row.fallback_probe_scheduled_count, 0)

    def test_gopax_2signal_with_probe_count(self):
        row = parse_metric_line(GOPAX_LINE)
        self.assertIsNotNone(row)
        self.assertEqual(row.source, "gopax")
        self.assertEqual(row.connection_status, "normal")
        self.assertEqual(row.ticker_freshness_status, "normal")
        self.assertEqual(row.fallback_probe_scheduled_count, 20)
        self.assertAlmostEqual(row.max_frame_gap, 150.4)

    def test_non_metric_line_returns_none(self):
        self.assertIsNone(parse_metric_line("not a metric line"))

    def test_unknown_source_returns_none(self):
        line = (
            "2026-05-22 14:29:13 | INFO | [usdt_ws.unknown] metrics "
            "frames_per_min=1"
        )
        self.assertIsNone(parse_metric_line(line))


# ---------------------------------------------------------------------------
# extract_latest_snapshot
# ---------------------------------------------------------------------------


class TestExtractLatestSnapshot(unittest.TestCase):
    """5 source 각 latest cycle 1개씩 추출 + 윈도우 필터."""

    def test_latest_per_source(self):
        rows = [
            parse_metric_line(UPBIT_LINE),
            parse_metric_line(BITHUMB_LINE),
            parse_metric_line(COINONE_LINE),
            parse_metric_line(GOPAX_LINE),
        ]
        # Korbit는 sample 없음
        latest = extract_latest_snapshot([r for r in rows if r is not None])
        self.assertIsNotNone(latest["upbit"])
        self.assertIsNotNone(latest["bithumb"])
        self.assertIsNotNone(latest["coinone"])
        self.assertIsNone(latest["korbit"])  # 데이터 없음
        self.assertIsNotNone(latest["gopax"])
        # Gopax는 14:30:13 (가장 늦은 ts)
        self.assertEqual(latest["gopax"].ts, datetime(2026, 5, 22, 14, 30, 13))

    def test_multiple_cycles_picks_latest(self):
        """같은 source 여러 cycle 중 가장 늦은 ts 선택."""
        early = MetricRow(source="gopax", ts=datetime(2026, 5, 22, 13, 0, 0), raw={"frames_per_min": "1"})
        late = MetricRow(source="gopax", ts=datetime(2026, 5, 22, 14, 0, 0), raw={"frames_per_min": "5"})
        latest = extract_latest_snapshot([early, late])
        self.assertEqual(latest["gopax"].ts, datetime(2026, 5, 22, 14, 0, 0))
        self.assertEqual(latest["gopax"].fpm, 5)

    def test_window_filter(self):
        """since/until 윈도우 외 rows 제외."""
        rows = [
            MetricRow(source="upbit", ts=datetime(2026, 5, 22, 12, 0), raw={}),
            MetricRow(source="upbit", ts=datetime(2026, 5, 22, 14, 0), raw={}),
            MetricRow(source="upbit", ts=datetime(2026, 5, 22, 16, 0), raw={}),
        ]
        latest = extract_latest_snapshot(
            rows,
            since=datetime(2026, 5, 22, 13, 0),
            until=datetime(2026, 5, 22, 15, 0),
        )
        # 14:00만 윈도우 안에 있음
        self.assertEqual(latest["upbit"].ts, datetime(2026, 5, 22, 14, 0))


# ---------------------------------------------------------------------------
# count_gopax_fallback
# ---------------------------------------------------------------------------


class TestCountGopaxFallback(unittest.TestCase):
    """Gopax REST fallback 카테고리별 count."""

    def test_start_success_basic(self):
        lines = [GOPAX_FALLBACK_START, GOPAX_FALLBACK_SUCCESS]
        counts = count_gopax_fallback(lines)
        self.assertEqual(counts["probe_start"], 1)
        self.assertEqual(counts["probe_success"], 1)
        self.assertEqual(counts["probe_timeout"], 0)
        self.assertEqual(counts["probe_returned_none"], 0)
        self.assertEqual(counts["probe_error"], 0)

    def test_zero_counts_on_empty(self):
        counts = count_gopax_fallback([])
        for key in counts:
            self.assertEqual(counts[key], 0)

    def test_unrelated_lines_ignored(self):
        counts = count_gopax_fallback(["random line", "[other] log"])
        for key in counts:
            self.assertEqual(counts[key], 0)


# ---------------------------------------------------------------------------
# count_errors
# ---------------------------------------------------------------------------


class TestCountErrors(unittest.TestCase):

    def test_error_line_match(self):
        counts = count_errors([ERROR_LINE])
        self.assertEqual(counts["error"], 1)
        self.assertEqual(counts["traceback"], 0)

    def test_traceback_line_match(self):
        counts = count_errors([TRACEBACK_LINE])
        self.assertEqual(counts["error"], 0)
        self.assertEqual(counts["traceback"], 1)

    def test_both_in_mix(self):
        counts = count_errors([
            ERROR_LINE, TRACEBACK_LINE, "normal log", ERROR_LINE,
        ])
        self.assertEqual(counts["error"], 2)
        self.assertEqual(counts["traceback"], 1)


# ---------------------------------------------------------------------------
# format_snapshot — output 포맷 sanity check
# ---------------------------------------------------------------------------


class TestFormatSnapshot(unittest.TestCase):

    def test_output_contains_all_sections(self):
        rows = [
            parse_metric_line(UPBIT_LINE),
            parse_metric_line(GOPAX_LINE),
        ]
        latest = extract_latest_snapshot([r for r in rows if r is not None])
        fallback = {"probe_start": 20, "probe_success": 20, "probe_timeout": 0,
                    "probe_returned_none": 0, "probe_error": 0}
        errors = {"error": 0, "traceback": 0}
        out = format_snapshot(latest, fallback, errors, since=None, until=None)
        self.assertIn("USDT WS Baseline Snapshot", out)
        self.assertIn("5 Source Metrics", out)
        self.assertIn("upbit", out)
        self.assertIn("gopax", out)
        self.assertIn("Gopax REST Fallback", out)
        self.assertIn("success_rate:", out)
        self.assertIn("ERROR/Traceback", out)

    def test_success_rate_computed(self):
        latest = {s: None for s in SOURCES}
        fallback = {"probe_start": 20, "probe_success": 20, "probe_timeout": 0,
                    "probe_returned_none": 0, "probe_error": 0}
        errors = {"error": 0, "traceback": 0}
        out = format_snapshot(latest, fallback, errors, since=None, until=None)
        self.assertIn("100.0%", out)

    def test_no_probe_yet_message(self):
        latest = {s: None for s in SOURCES}
        fallback = {"probe_start": 0, "probe_success": 0, "probe_timeout": 0,
                    "probe_returned_none": 0, "probe_error": 0}
        errors = {"error": 0, "traceback": 0}
        out = format_snapshot(latest, fallback, errors, since=None, until=None)
        self.assertIn("N/A (no probes in input window)", out)

    def test_label_uses_input_window_terminology(self):
        """Codex 권고: fallback section label은 'cumulative since uptime'이 아니라
        'log count in input window' — log line count는 입력 윈도우 기반."""
        latest = {s: None for s in SOURCES}
        fallback = {"probe_start": 0, "probe_success": 0, "probe_timeout": 0,
                    "probe_returned_none": 0, "probe_error": 0}
        errors = {"error": 0, "traceback": 0}
        out = format_snapshot(latest, fallback, errors, since=None, until=None)
        self.assertIn("log count in input window", out)
        self.assertNotIn("since uptime start", out)

    def test_summary_cumulative_counter_included_for_gopax(self):
        """Codex 보강: Gopax latest summary metric의 fallback_probe_scheduled_count도
        함께 표시 — log count vs summary cumulative 비교 가능."""
        gopax_row = parse_metric_line(GOPAX_LINE)
        latest = {s: None for s in SOURCES}
        latest["gopax"] = gopax_row
        fallback = {"probe_start": 0, "probe_success": 0, "probe_timeout": 0,
                    "probe_returned_none": 0, "probe_error": 0}
        errors = {"error": 0, "traceback": 0}
        out = format_snapshot(latest, fallback, errors, since=None, until=None)
        # GOPAX_LINE는 fallback_probe_scheduled_count=20 포함
        self.assertIn("summary cumulative", out)
        self.assertIn("fallback_probe_scheduled_count=20", out)
        self.assertIn("since container start", out)


# ---------------------------------------------------------------------------
# main — stdin integration
# ---------------------------------------------------------------------------


class TestMainStdin(unittest.TestCase):

    def test_stdin_pipeline(self):
        """stdin으로 metric lines 입력 → 정상 출력 + exit 0."""
        input_lines = "\n".join([UPBIT_LINE, GOPAX_LINE, GOPAX_FALLBACK_START,
                                  GOPAX_FALLBACK_SUCCESS]) + "\n"
        with patch("sys.stdin", io.StringIO(input_lines)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        output = mock_stdout.getvalue()
        self.assertIn("upbit", output)
        self.assertIn("gopax", output)
        self.assertIn("probe_start:         1", output)
        self.assertIn("probe_success:       1", output)

    def test_empty_stdin_returns_1(self):
        """no metric lines → exit 1 + stderr message."""
        with patch("sys.stdin", io.StringIO("random non-metric log\n")), \
             patch("sys.stderr", new_callable=io.StringIO):
            exit_code = main([])
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
