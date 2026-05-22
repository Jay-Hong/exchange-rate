#!/usr/bin/env python3
"""USDT WS baseline extractor (Option A v2 — log-only snapshot).

Docker log의 `[usdt_ws.SOURCE] metrics` 라인을 파싱해 5 source latest snapshot +
Gopax fallback 통계 + ERROR/Traceback count 요약. 배포 전/직후/24h 관찰 표준화.

USDT_WS_DESIGN_PLAN §12.8 backlog 분석 input 수집 + Phase B.2 배포 검증 + 5
source 자연 관찰 baseline 캡처에 재사용.

사용 (stdin, 권장):
    docker compose logs fastapi --since 24h 2>&1 \\
        | python scripts/usdt_ws_baseline_extract.py

사용 (SSH 한 줄):
    ssh ... 'cd ~/exchange-rate && docker compose logs fastapi --since 1h 2>&1' \\
        | python scripts/usdt_ws_baseline_extract.py

사용 (file):
    python scripts/usdt_ws_baseline_extract.py --file /path/to/app.log

윈도우 필터 (file 모드 보조):
    python scripts/usdt_ws_baseline_extract.py --file app.log \\
        --since 2026-05-22T13:27:00 --until 2026-05-22T14:30:00

scope: scripts/krx_baseline_extract.py mirror, latest snapshot 출력만. window
시계열 통계 (avg/p50/p95/p99/max), JSON 출력, 24h trend 분석은 후속 PR.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


# 5 source list (출력 순서 고정)
SOURCES = ("upbit", "bithumb", "coinone", "korbit", "gopax")


# [usdt_ws.SOURCE] metrics ... 라인 매칭 + source 추출.
# rest 부분은 key=value pair로 별도 파싱 (source별 fields 차이 처리).
METRIC_LINE_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r".*?\[usdt_ws\.(?P<source>upbit|bithumb|coinone|korbit|gopax)\] metrics\s+"
    r"(?P<rest>.+?)$"
)

# Gopax REST fallback 로그 카운트 (probe start / success / timeout / None / error).
GOPAX_FALLBACK_PATTERNS = {
    "probe_start": re.compile(r"\[usdt_ws\.gopax\.fallback\] probe start"),
    "probe_success": re.compile(r"\[usdt_ws\.gopax\.fallback\] probe success"),
    "probe_timeout": re.compile(r"\[usdt_ws\.gopax\.fallback\] probe timeout"),
    "probe_returned_none": re.compile(r"\[usdt_ws\.gopax\.fallback\] probe returned None"),
    "probe_error": re.compile(r"\[usdt_ws\.gopax\.fallback\] probe error"),
}

# ERROR / Traceback count (운영 안정성 baseline).
ERROR_RE = re.compile(r"\| ERROR \|")
TRACEBACK_RE = re.compile(r"^Traceback ")


@dataclass
class MetricRow:
    """한 cycle의 metrics 표현 — source별 fields 차이는 Optional로 흡수."""
    source: str
    ts: datetime
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def fpm(self) -> Optional[int]:
        v = self.raw.get("frames_per_min")
        return int(v) if v is not None else None

    @property
    def last_tick_age(self) -> Optional[float]:
        v = self.raw.get("last_tick_age")
        return float(v) if v is not None else None

    @property
    def last_heartbeat_age(self) -> Optional[float]:
        v = self.raw.get("last_heartbeat_age")
        return float(v) if v is not None else None

    @property
    def max_frame_gap(self) -> Optional[float]:
        v = self.raw.get("max_frame_gap")
        return float(v) if v is not None else None

    @property
    def reconnect_attempts(self) -> Optional[int]:
        v = self.raw.get("reconnect_attempts")
        return int(v) if v is not None else None

    @property
    def status(self) -> Optional[str]:
        """1-dim status (Upbit/Bithumb). 2-signal source는 None."""
        return self.raw.get("status")

    @property
    def connection_status(self) -> Optional[str]:
        """2-signal connection_status (Coinone/Korbit/Gopax). 1-dim source는 None."""
        return self.raw.get("connection_status")

    @property
    def ticker_freshness_status(self) -> Optional[str]:
        return self.raw.get("ticker_freshness_status")

    @property
    def status_transitions(self) -> Optional[str]:
        return self.raw.get("status_transitions")

    @property
    def redis_saturation_count(self) -> Optional[int]:
        v = self.raw.get("redis_saturation_count")
        return int(v) if v is not None else None

    @property
    def fallback_probe_scheduled_count(self) -> Optional[int]:
        v = self.raw.get("fallback_probe_scheduled_count")
        return int(v) if v is not None else None


def parse_metric_line(line: str) -> Optional[MetricRow]:
    """`[usdt_ws.SOURCE] metrics ...` 라인 파싱. 비매칭 시 None.

    rest 부분은 key=value pair로 split → raw dict. source별 fields 차이는
    Optional property로 흡수.
    """
    m = METRIC_LINE_RE.search(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # rest = "key1=val1 key2=val2 ..." 형태.
    # 단순 split(" ")으로 처리 — value 안에 공백 없는 형식 가정 (현재 로그 보장).
    raw: Dict[str, str] = {}
    for token in m.group("rest").strip().split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        raw[key] = value
    return MetricRow(source=m.group("source"), ts=ts, raw=raw)


def extract_latest_snapshot(
    rows: List[MetricRow],
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, Optional[MetricRow]]:
    """5 source 각 latest cycle 1개씩 추출. 윈도우 필터 적용."""
    latest: Dict[str, Optional[MetricRow]] = {s: None for s in SOURCES}
    for row in rows:
        if since is not None and row.ts < since:
            continue
        if until is not None and row.ts > until:
            continue
        prev = latest.get(row.source)
        if prev is None or row.ts > prev.ts:
            latest[row.source] = row
    return latest


def count_gopax_fallback(lines: List[str]) -> Dict[str, int]:
    """Gopax REST fallback 카테고리별 count.

    keys: probe_start, probe_success, probe_timeout, probe_returned_none, probe_error
    """
    counts = {k: 0 for k in GOPAX_FALLBACK_PATTERNS}
    for line in lines:
        for key, pattern in GOPAX_FALLBACK_PATTERNS.items():
            if pattern.search(line):
                counts[key] += 1
                break  # 한 라인은 한 카테고리만 매칭
    return counts


def count_errors(lines: List[str]) -> Dict[str, int]:
    """ERROR / Traceback 라인 카운트."""
    error = 0
    traceback = 0
    for line in lines:
        if ERROR_RE.search(line):
            error += 1
        if TRACEBACK_RE.match(line.lstrip()):
            traceback += 1
    return {"error": error, "traceback": traceback}


def format_snapshot(
    latest: Dict[str, Optional[MetricRow]],
    fallback_counts: Dict[str, int],
    error_counts: Dict[str, int],
    since: Optional[datetime],
    until: Optional[datetime],
) -> str:
    """Human-readable text 출력 (배포 검증 + 24h 관찰 baseline용)."""
    lines: List[str] = []
    lines.append("=== USDT WS Baseline Snapshot ===")
    window_str = ""
    if since is not None:
        window_str += f"since={since.isoformat()} "
    if until is not None:
        window_str += f"until={until.isoformat()}"
    if window_str:
        lines.append(f"Window: {window_str.strip()}")
    lines.append("")

    lines.append("--- 5 Source Metrics (latest cycle each) ---")
    # status column width=20: "normal/normal" (13), "stale/degraded" (14) 등 2-signal 표현 수용
    header = (
        f"{'source':<10}{'fpm':>5}{'tick_age':>10}{'hb_age':>9}"
        f"{'max_gap':>10}  {'status':<20}{'reconnect':>10}{'sat':>5}{'probe':>7}"
        f"  cycle_ts"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for source in SOURCES:
        row = latest.get(source)
        if row is None:
            lines.append(f"{source:<10}{'(no data in window)':>60}")
            continue
        # status 표현: 1-dim은 `status`, 2-signal은 `conn/ticker`
        if row.status is not None:
            status_str = row.status
        elif row.connection_status is not None:
            cs = row.connection_status or "?"
            ts_ = row.ticker_freshness_status or "?"
            status_str = f"{cs}/{ts_}"
        else:
            status_str = "?"

        sat = row.redis_saturation_count
        probe = row.fallback_probe_scheduled_count
        sat_str = str(sat) if sat is not None else "-"
        probe_str = str(probe) if probe is not None else "-"
        lines.append(
            f"{source:<10}{row.fpm or 0:>5}"
            f"{row.last_tick_age or 0:>10.1f}{row.last_heartbeat_age or 0:>9.1f}"
            f"{row.max_frame_gap or 0:>10.1f}  {status_str:<20}"
            f"{row.reconnect_attempts or 0:>10}{sat_str:>5}{probe_str:>7}"
            f"  {row.ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    lines.append("")

    # Gopax status_transitions (전이 누적 별도 출력 — status 표현엔 안 넣음)
    gopax = latest.get("gopax")
    if gopax is not None and gopax.status_transitions:
        lines.append("--- Gopax status_transitions (cumulative) ---")
        lines.append(gopax.status_transitions)
        lines.append("")

    # Gopax REST fallback 통계 (probe lifecycle counters)
    # 두 종류 카운트:
    #   - log count: input window 안의 probe lifecycle 로그 라인 카운트 (윈도우 기반)
    #   - summary counter: Gopax latest summary metric의 `fallback_probe_scheduled_count`
    #     (instance lifetime cumulative since container start)
    # 두 값이 다를 수 있음 — 입력 윈도우보다 이전에 발생한 probe는 log count에 빠짐
    lines.append("--- Gopax REST Fallback (log count in input window) ---")
    lines.append(f"probe_start:         {fallback_counts['probe_start']}")
    lines.append(f"probe_success:       {fallback_counts['probe_success']}")
    lines.append(f"probe_timeout:       {fallback_counts['probe_timeout']}")
    lines.append(f"probe_returned_none: {fallback_counts['probe_returned_none']}")
    lines.append(f"probe_error:         {fallback_counts['probe_error']}")
    total_failure = (
        fallback_counts['probe_timeout']
        + fallback_counts['probe_returned_none']
        + fallback_counts['probe_error']
    )
    if fallback_counts['probe_start'] > 0:
        rate = 100.0 * fallback_counts['probe_success'] / fallback_counts['probe_start']
        lines.append(
            f"success_rate:        {rate:.1f}% "
            f"({fallback_counts['probe_success']}/{fallback_counts['probe_start']} in input window, "
            f"failure={total_failure})"
        )
    else:
        lines.append("success_rate:        N/A (no probes in input window)")
    # Gopax summary counter (cumulative since container start) — 비교용
    gopax = latest.get("gopax")
    if gopax is not None and gopax.fallback_probe_scheduled_count is not None:
        lines.append(
            f"summary cumulative:  fallback_probe_scheduled_count="
            f"{gopax.fallback_probe_scheduled_count} (since container start, from latest summary metric)"
        )
    lines.append("")

    # ERROR/Traceback count (운영 안정성 baseline)
    lines.append("--- ERROR/Traceback (raw line count in window) ---")
    lines.append(f"ERROR lines:     {error_counts['error']}")
    lines.append(f"Traceback lines: {error_counts['traceback']}")

    return "\n".join(lines)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO timestamp: {value}") from exc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract USDT WS 5-source baseline snapshot from docker logs",
    )
    parser.add_argument(
        "--file", "-f",
        help="로그 파일 경로 (지정 안하면 stdin에서 읽음)",
    )
    parser.add_argument(
        "--since",
        type=_parse_iso,
        help="window 시작 ISO timestamp (file 모드 보조, stdin은 docker logs --since로 처리)",
    )
    parser.add_argument(
        "--until",
        type=_parse_iso,
        help="window 종료 ISO timestamp",
    )
    args = parser.parse_args(argv)

    # 입력 라인 읽기 (stdin 또는 file)
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    else:
        lines = sys.stdin.readlines()

    # 메트릭 라인 파싱
    rows: List[MetricRow] = []
    for line in lines:
        row = parse_metric_line(line)
        if row is not None:
            rows.append(row)

    if not rows:
        print("매칭 [usdt_ws.SOURCE] metrics 라인 0개", file=sys.stderr)
        return 1

    # 5 source latest snapshot + Gopax fallback + ERROR count
    latest = extract_latest_snapshot(rows, since=args.since, until=args.until)
    fallback_counts = count_gopax_fallback(lines)
    error_counts = count_errors(lines)

    print(format_snapshot(latest, fallback_counts, error_counts, args.since, args.until))
    return 0


if __name__ == "__main__":
    sys.exit(main())
