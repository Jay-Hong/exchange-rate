#!/usr/bin/env python3
"""KRX baseline extractor (PR6c-2d-1 follow-up, v1 — log-only).

Docker log의 `[kis_ws] metrics` 라인을 파싱해 session/status/gap 통계 요약.
5/8 baseline 분석 자동화 + 수동 grep 해석 누수 차단.

사용 (stdin):
    docker compose logs fastapi --since 24h 2>&1 | python scripts/krx_baseline_extract.py

사용 (file):
    python scripts/krx_baseline_extract.py /path/to/app.log

윈도우 필터:
    python scripts/krx_baseline_extract.py --since 2026-05-08T00:00 --until 2026-05-08T23:59 app.log

v1 범위: log-only. admin API snapshot merge / gap_buckets / frame breakdown은 v2.
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple


# 로그 라인 파서 — `[kis_ws] metrics ...` 만 매칭
LOG_LINE_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r".*?\[kis_ws\] metrics\s+"
    r"session=(?P<session>\S+)\s+"
    r"status=(?P<status>\S+)\s+"
    r"frames_per_min=(?P<fpm>\d+)\s+"
    r"frame_age=(?P<fage>\S+)\s+"
    r"trade_age=(?P<tage>\S+)\s+"
    r"quote_age=(?P<qage>\S+)\s+"
    r"max_total_gap=(?P<mtg>\S+)\s+"
    r"max_trade_gap=(?P<mtdg>\S+)\s+"
    r"max_quote_gap=(?P<mqg>\S+)\s+"
    r"stale_transitions=(?P<st>\d+)\s+"
    r"reconnect_attempts=(?P<rc>\d+)"
)


@dataclass
class MetricRow:
    ts: datetime
    session: str
    status: str
    frames_per_min: int
    frame_age: Optional[float]
    trade_age: Optional[float]
    quote_age: Optional[float]
    max_total_gap: float
    max_trade_gap: float
    max_quote_gap: float
    stale_transitions: int
    reconnect_attempts: int


def parse_age(value: str) -> Optional[float]:
    """trade_age=None 같은 문자열 처리 — None 반환."""
    if value == "None":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_log_line(line: str) -> Optional[MetricRow]:
    """`[kis_ws] metrics` 라인 → MetricRow. 비매칭 시 None."""
    m = LOG_LINE_RE.search(line)
    if not m:
        return None
    return MetricRow(
        ts=datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"),
        session=m.group("session"),
        status=m.group("status"),
        frames_per_min=int(m.group("fpm")),
        frame_age=parse_age(m.group("fage")),
        trade_age=parse_age(m.group("tage")),
        quote_age=parse_age(m.group("qage")),
        max_total_gap=float(m.group("mtg")),
        max_trade_gap=float(m.group("mtdg")),
        max_quote_gap=float(m.group("mqg")),
        stale_transitions=int(m.group("st")),
        reconnect_attempts=int(m.group("rc")),
    )


def session_end_minutes(ts: datetime, session: str) -> Optional[int]:
    """ts 시점에서 정상 session 종료까지 남은 분 수.

    만기일 11:30 종료 처리는 v2 — 현재 v1은 정상 boundary만.

    Returns:
        정수 분. CF=15:45, CM=다음날 06:00 (또는 같은 날 06:00). 미지원 session은 None.
    """
    if session == "CF":
        end = datetime.combine(ts.date(), time(15, 45))
    elif session == "CM":
        # CM: 17:50~다음날 06:00
        if ts.time() >= time(17, 50):
            end = datetime.combine(ts.date() + timedelta(days=1), time(6, 0))
        else:
            end = datetime.combine(ts.date(), time(6, 0))
    else:
        return None
    delta_sec = (end - ts).total_seconds()
    return int(delta_sec / 60)


def percentile(data: List[float], p: float) -> float:
    """linear-interpolation percentile. 빈 리스트 → 0.0."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    if f == c:
        return float(sorted_data[f])
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def summarize_session(rows: List[MetricRow], label: str) -> List[str]:
    """session별 통계 요약 라인."""
    out: List[str] = []
    if not rows:
        out.append(f"{label}: 0 rows")
        return out

    fpm_list = [r.frames_per_min for r in rows]
    frame_age_list = [r.frame_age for r in rows if r.frame_age is not None]
    trade_age_list = [r.trade_age for r in rows if r.trade_age is not None]
    quote_age_list = [r.quote_age for r in rows if r.quote_age is not None]

    out.append(f"{label}: {len(rows)} rows")
    out.append(
        f"  frames_per_min: min={min(fpm_list)} avg={statistics.mean(fpm_list):.1f} "
        f"p50={percentile(fpm_list, 50):.0f} p95={percentile(fpm_list, 95):.0f} max={max(fpm_list)}"
    )
    if frame_age_list:
        out.append(
            f"  frame_age max={max(frame_age_list):.0f} (None excluded: {len(rows) - len(frame_age_list)})"
        )
    else:
        out.append(f"  frame_age: 모두 None ({len(rows)} rows)")
    if trade_age_list:
        out.append(
            f"  trade_age max={max(trade_age_list):.0f} (None excluded: {len(rows) - len(trade_age_list)})"
        )
    else:
        out.append(f"  trade_age: 모두 None ({len(rows)} rows)")
    if quote_age_list:
        out.append(
            f"  quote_age max={max(quote_age_list):.0f} (None excluded: {len(rows) - len(quote_age_list)})"
        )
    else:
        out.append(f"  quote_age: 모두 None ({len(rows)} rows)")
    # 주의: 로그의 max_*_gap은 active session 시작 후 cumulative max.
    # windowing(--since/--until)으로 session 중간만 자르면 윈도우 이전에 발생한
    # gap도 포함된 누적값이 표시됨. 윈도우 단위 delta가 아님.
    out.append(
        f"  cumulative max gaps (active session, not per-window) — "
        f"total={max(r.max_total_gap for r in rows):.1f} "
        f"trade={max(r.max_trade_gap for r in rows):.1f} "
        f"quote={max(r.max_quote_gap for r in rows):.1f}"
    )
    return out


def extract_counter_events(
    rows: List[MetricRow], counter: str
) -> List[Tuple[datetime, str, int, int, MetricRow]]:
    """이전 row 대비 cumulative counter 증가 이벤트 추출.

    stale_transitions / reconnect_attempts는 누적이라 값 자체가 아니라 증가만 event.

    Returns:
        list of (timestamp, session, delta, current_value, full_row).
    """
    events: List[Tuple[datetime, str, int, int, MetricRow]] = []
    prev: Optional[int] = None
    for r in rows:
        cur = getattr(r, counter)
        if prev is not None and cur > prev:
            events.append((r.ts, r.session, cur - prev, cur, r))
        prev = cur
    return events


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KRX baseline extractor (v1, log-only)"
    )
    parser.add_argument(
        "logfile", nargs="?", help="로그 파일 경로 (생략 시 stdin)"
    )
    parser.add_argument("--since", help="ISO datetime (KST naive). 예: 2026-05-08T00:00")
    parser.add_argument("--until", help="ISO datetime (KST naive)")
    args = parser.parse_args()

    since = parse_iso(args.since)
    until = parse_iso(args.until)

    if args.logfile:
        f = open(args.logfile, "r", encoding="utf-8", errors="replace")
    else:
        f = sys.stdin

    rows: List[MetricRow] = []
    try:
        for line in f:
            r = parse_log_line(line)
            if r is None:
                continue
            if since and r.ts < since:
                continue
            if until and r.ts > until:
                continue
            rows.append(r)
    finally:
        if args.logfile:
            f.close()

    if not rows:
        print("매칭 [kis_ws] metrics 라인 0개", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: r.ts)
    print(f"=== KRX baseline ({rows[0].ts} ~ {rows[-1].ts}) ===")
    print(f"Total {len(rows)} rows\n")

    # Session breakdown
    print("Session breakdown:")
    for label in ("CF", "CM"):
        session_rows = [r for r in rows if r.session == label]
        for line in summarize_session(session_rows, f"  {label}"):
            print(line)

    # Status transitions — stale events (cumulative counter delta)
    stale_events = extract_counter_events(rows, "stale_transitions")
    print(f"\nStatus transitions:")
    print(f"  stale events: {len(stale_events)}건")
    for ts, session, delta, cur, r in stale_events:
        end_min = session_end_minutes(ts, session)
        end_str = f"session_end -{end_min} min" if end_min is not None else "session N/A"
        qa = r.quote_age if r.quote_age is not None else "None"
        print(
            f"    [{ts.strftime('%Y-%m-%d %H:%M:%S')}] {session} ({end_str})  "
            f"quote_age={qa} reconnects={r.reconnect_attempts}"
        )

    # Reconnect events (cumulative counter delta)
    rc_events = extract_counter_events(rows, "reconnect_attempts")
    print(f"\n  reconnect events: {len(rc_events)}건")
    for ts, session, delta, cur, r in rc_events:
        print(
            f"    [{ts.strftime('%Y-%m-%d %H:%M:%S')}] {session} "
            f"attempt={cur} (Δ={delta})"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
