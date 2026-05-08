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

v1.1 추가 (Codex 권고, 2026-05-08):
- status 전이 로그 (`normal → stale` / `stale → normal`) 직접 파싱
  → cumulative counter delta의 시간 지연 한계 해결 (휴장 동안 summary 안 찍히면
  CM 마지막 stale이 다음 active session 첫 summary로 밀림)
- 6 sub-session 분류: CF/CM × OPEN_AUCTION / CONTINUOUS / CLOSE_AUCTION
  단일가 구간(10분) 별도 라벨링 → PR6d-2b session-end grace 자료
  출처: KRX FICC 파생상품 개편 후 일반 거래일 시간표 (특수일/만기일 예외)
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


# v1.1 — status 전이 로그 파서 (`[kis_ws] status normal → stale` 등)
# cumulative counter delta보다 정확한 stale 발화/복구 시각.
STATUS_TRANSITION_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r".*?\[kis_ws\] status (?P<from_status>\w+) → (?P<to_status>\w+)"
)


# v1.1 — KRX 일반 거래일 sub-session 시간 (KRX FICC 개편 후 자료 기반)
# 만기일/특수일 예외 — caveat은 KRX_CANARY runbook에 명시
_CF_OPEN_AUCTION_END = time(8, 45)     # 시가 단일가 종료
_CF_CLOSE_AUCTION_START = time(15, 35)  # 종가 단일가 시작
_CF_END = time(15, 45)
_CM_OPEN_AUCTION_END = time(18, 0)      # 야간 시가 단일가 종료
_CM_CLOSE_AUCTION_START = time(5, 50)   # 야간 종가 단일가 시작 (다음날 새벽)
_CM_END = time(6, 0)


@dataclass
class StatusTransition:
    """status 전이 1건 (v1.1 추가)."""
    ts: datetime
    from_status: str
    to_status: str


def parse_status_transition(line: str) -> Optional[StatusTransition]:
    """`[kis_ws] status X → Y` 라인 파싱. 비매칭 시 None."""
    m = STATUS_TRANSITION_RE.search(line)
    if not m:
        return None
    return StatusTransition(
        ts=datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"),
        from_status=m.group("from_status"),
        to_status=m.group("to_status"),
    )


def classify_sub_session(ts: datetime, session: str) -> str:
    """일반 거래일 sub-session 분류 (KRX FICC 자료 기반).

    Returns:
        "CF_OPEN_AUCTION" / "CF_CONTINUOUS" / "CF_CLOSE_AUCTION"
        "CM_OPEN_AUCTION" / "CM_CONTINUOUS" / "CM_CLOSE_AUCTION"
        "UNKNOWN" (session이 CF/CM 아니거나 시간이 정의 외)

    만기일은 거래시간 단축 (08:45~11:30) — v1.1은 일반 거래일만.
    """
    t = ts.time()
    if session == "CF":
        if t < _CF_OPEN_AUCTION_END:
            return "CF_OPEN_AUCTION"
        if t < _CF_CLOSE_AUCTION_START:
            return "CF_CONTINUOUS"
        if t <= _CF_END:
            return "CF_CLOSE_AUCTION"
        return "UNKNOWN"
    if session == "CM":
        # 야간: 17:50~18:00 (open auction), 18:00~05:50 (continuous), 05:50~06:00 (close auction)
        if t >= time(17, 50):
            if t < _CM_OPEN_AUCTION_END:
                return "CM_OPEN_AUCTION"
            return "CM_CONTINUOUS"  # 18:00~23:59
        if t < _CM_CLOSE_AUCTION_START:
            return "CM_CONTINUOUS"  # 00:00~05:50
        if t <= _CM_END:
            return "CM_CLOSE_AUCTION"  # 05:50~06:00
        return "UNKNOWN"
    return "UNKNOWN"


def pair_stale_transitions(
    transitions: List[StatusTransition],
) -> List[Tuple[StatusTransition, Optional[StatusTransition]]]:
    """`normal → stale` 와 다음 `stale → normal` 페어링.

    Returns:
        list of (start, end_or_None). end가 None이면 미복구 (윈도우 끝까지 stale).
    """
    pairs: List[Tuple[StatusTransition, Optional[StatusTransition]]] = []
    pending_start: Optional[StatusTransition] = None
    for tr in transitions:
        if tr.to_status == "stale":
            pending_start = tr
        elif tr.from_status == "stale" and pending_start is not None:
            pairs.append((pending_start, tr))
            pending_start = None
    if pending_start is not None:
        pairs.append((pending_start, None))
    return pairs


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

    Note:
        2분 미만 정밀도가 필요한 출력에는 format_session_end_delta() 사용.
        (int 절삭으로 39초 → 0분 표시되는 오해 차단, Codex 권고)
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


def format_session_end_delta(ts: datetime, session: str) -> Optional[str]:
    """v1.1 — session_end 까지 남은 시간 표시 (2분 미만은 초 단위).

    `int(delta_sec / 60)`만 사용하면 39초가 "0분"으로 잘려 분석자가 "종료 시점
    이후/동시"로 오해 가능 (Codex 권고). 2분 (120s) 미만은 초 단위로.

    Returns:
        "session_end -39s" / "session_end -4 min" / "session_end -32 min" 등.
        미지원 session은 None.
    """
    if session == "CF":
        end = datetime.combine(ts.date(), time(15, 45))
    elif session == "CM":
        if ts.time() >= time(17, 50):
            end = datetime.combine(ts.date() + timedelta(days=1), time(6, 0))
        else:
            end = datetime.combine(ts.date(), time(6, 0))
    else:
        return None
    delta_sec = (end - ts).total_seconds()
    if delta_sec < 0:
        return f"session_end +{int(-delta_sec)}s"  # 종료 후 (이론상 발생 X)
    if delta_sec < 120:
        return f"session_end -{int(delta_sec)}s"
    return f"session_end -{int(delta_sec / 60)} min"


def session_at_ts(
    ts: datetime,
    metric_rows: List["MetricRow"],
) -> str:
    """status 전이 시점의 session 추정 (v1.1, Codex 권고).

    1순위: ts 이전 가장 가까운 metric row의 session (인접 추정 — 정확)
    2순위: timestamp 기반 직접 분류 (metric row가 ts 이전에 없을 때 fallback)

    timestamp fallback 분류:
        08:30~15:45 → CF
        17:50~23:59 또는 00:00~06:00 → CM
        그 외 (휴장 break) → "UNKNOWN"
    """
    candidates = [r for r in metric_rows if r.ts <= ts]
    if candidates:
        return candidates[-1].session
    # fallback — timestamp 직접 분류 (휴장 경계 보호)
    t = ts.time()
    if time(8, 30) <= t <= time(15, 45):
        return "CF"
    if t >= time(17, 50) or t <= time(6, 0):
        return "CM"
    return "UNKNOWN"


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
    transitions: List[StatusTransition] = []
    try:
        for line in f:
            r = parse_log_line(line)
            if r is not None:
                if since and r.ts < since:
                    pass
                elif until and r.ts > until:
                    pass
                else:
                    rows.append(r)
            tr = parse_status_transition(line)
            if tr is not None:
                if since and tr.ts < since:
                    continue
                if until and tr.ts > until:
                    continue
                transitions.append(tr)
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

    # v1.1 — Status transitions from log (정확한 시각 + 지속시간)
    stale_pairs = pair_stale_transitions(transitions)
    print(f"\nStatus transitions (status log 기준 — 정확):")
    print(f"  stale events: {len(stale_pairs)}건")
    rows_by_ts = sorted(rows, key=lambda r: r.ts)

    for start, end in stale_pairs:
        session = session_at_ts(start.ts, rows_by_ts)
        sub = classify_sub_session(start.ts, session)
        end_str = format_session_end_delta(start.ts, session) or "session N/A"
        if end is not None:
            duration = (end.ts - start.ts).total_seconds()
            print(
                f"    [{start.ts.strftime('%Y-%m-%d %H:%M:%S')} → "
                f"{end.ts.strftime('%H:%M:%S')}, {duration:.0f}s] "
                f"{sub} ({end_str})"
            )
        else:
            print(
                f"    [{start.ts.strftime('%Y-%m-%d %H:%M:%S')} → 미복구] "
                f"{sub} ({end_str})"
            )

    # cumulative counter는 보조 cross-check
    cum_stale = extract_counter_events(rows, "stale_transitions")
    if len(cum_stale) != len(stale_pairs):
        print(
            f"  (참고: cumulative stale_transitions delta={len(cum_stale)}, "
            f"transition log 기준={len(stale_pairs)} — 차이 시 transition log가 정확)"
        )

    # Reconnect events (cumulative counter delta — reconnect 전용 status 로그 별도 X)
    rc_events = extract_counter_events(rows, "reconnect_attempts")
    print(f"\n  reconnect events: {len(rc_events)}건")
    for ts, session, delta, cur, r in rc_events:
        sub = classify_sub_session(ts, session)
        print(
            f"    [{ts.strftime('%Y-%m-%d %H:%M:%S')}] {sub} "
            f"attempt={cur} (Δ={delta})"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
