#!/usr/bin/env python3
"""
DXY spot(`/indices/usdollar`) 검증 스크립트.

검증 항목:
  - __NEXT_DATA__ 경로 유효성
  - lastUpdateTime 파싱 및 갱신 주기
  - Yahoo Finance meta.regularMarketPrice / regularMarketTime 교차검증
  - 선물 계열(`exchange-rates-table`, `/currencies/us-dollar-index`)과의 차이 관찰
  - 05:45~09:00 KST 구간 여부 마킹

예시:
  python scripts/validate_dxy_spot.py --samples 10 --interval 10
  python scripts/validate_dxy_spot.py --samples 540 --interval 10 --jsonl logs/dxy_spot_validation.jsonl
"""

# 표준 라이브러리
import argparse
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 서드파티 라이브러리
from bs4 import BeautifulSoup
from curl_cffi import requests

KST = timezone(timedelta(hours=9))
UTC = timezone.utc

SPOT_URL = "https://kr.investing.com/indices/usdollar"
FUTURES_TABLE_URL = "https://kr.investing.com/currencies/exchange-rates-table"
FUTURES_PAGE_URL = "https://kr.investing.com/currencies/us-dollar-index"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=5m"

SPOT_PRICE_PATH = ("props", "pageProps", "state", "indexStore", "instrument", "price")
FUTURES_PRICE_PATH = ("props", "pageProps", "state", "currencyStore", "instrument", "price")
FUTURES_TABLE_SELECTOR = "#sb_last_8827"
SPOT_CSS_SELECTORS = ['[data-test="instrument-price-last"]', '[class*="text-5xl"]']

TARGET_WINDOW_START = dt_time(5, 45)
TARGET_WINDOW_END = dt_time(9, 0)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1_2) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15"
)


@dataclass
class SpotData:
    rate: float
    source_ts_ms: int
    source_dt_utc: datetime
    delay_sec: float
    path: str
    cache_status: Optional[str]
    css_rate: Optional[float]
    http_status: int
    latency_ms: float


@dataclass
class FuturesTableData:
    rate: float
    cache_status: Optional[str]
    http_status: int
    latency_ms: float


@dataclass
class YahooData:
    rate: float
    source_dt_utc: datetime
    delay_sec: float
    http_status: int
    latency_ms: float


def _extract_next_data(html: str) -> dict:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(\{.*?\})</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("__NEXT_DATA__ not found")
    return json.loads(match.group(1))


def _dig_path(data: dict, path: Tuple[str, ...]) -> dict:
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise KeyError(".".join(path))
        current = current[key]
    if not isinstance(current, dict):
        raise TypeError(".".join(path))
    return current


def _parse_price_from_next_data(
    html: str,
    path: Tuple[str, ...],
    now_utc: datetime,
) -> Tuple[float, int, datetime]:
    next_data = _extract_next_data(html)
    price = _dig_path(next_data, path)

    last = float(price["last"])
    source_ts_ms = int(price["lastUpdateTime"])
    source_dt_utc = datetime.fromtimestamp(source_ts_ms / 1000, tz=UTC)

    if source_dt_utc > now_utc + timedelta(seconds=5):
        raise ValueError(f"source timestamp is in the future: {source_dt_utc.isoformat()}")

    return last, source_ts_ms, source_dt_utc


def _extract_css_rate(html: str) -> Optional[float]:
    soup = BeautifulSoup(html, "html.parser")
    for selector in SPOT_CSS_SELECTORS:
        element = soup.select_one(selector)
        if element is None:
            continue
        text = element.get_text(strip=True).replace(",", "")
        try:
            return float(text)
        except ValueError:
            continue
    return None


def _fetch_spot(session: requests.Session, timeout: int) -> SpotData:
    start = time.monotonic()
    response = session.get(SPOT_URL, headers={"User-Agent": UA}, timeout=timeout)
    latency_ms = (time.monotonic() - start) * 1000
    response.raise_for_status()
    now_utc = datetime.now(UTC)
    rate, source_ts_ms, source_dt_utc = _parse_price_from_next_data(
        response.text,
        SPOT_PRICE_PATH,
        now_utc,
    )
    css_rate = _extract_css_rate(response.text)
    return SpotData(
        rate=rate,
        source_ts_ms=source_ts_ms,
        source_dt_utc=source_dt_utc,
        delay_sec=(now_utc - source_dt_utc).total_seconds(),
        path=".".join(SPOT_PRICE_PATH),
        cache_status=response.headers.get("x-cache-status"),
        css_rate=css_rate,
        http_status=response.status_code,
        latency_ms=round(latency_ms, 1),
    )


def _fetch_futures_page(session: requests.Session, timeout: int) -> SpotData:
    start = time.monotonic()
    response = session.get(FUTURES_PAGE_URL, headers={"User-Agent": UA}, timeout=timeout)
    latency_ms = (time.monotonic() - start) * 1000
    response.raise_for_status()
    now_utc = datetime.now(UTC)
    rate, source_ts_ms, source_dt_utc = _parse_price_from_next_data(
        response.text,
        FUTURES_PRICE_PATH,
        now_utc,
    )
    return SpotData(
        rate=rate,
        source_ts_ms=source_ts_ms,
        source_dt_utc=source_dt_utc,
        delay_sec=(now_utc - source_dt_utc).total_seconds(),
        path=".".join(FUTURES_PRICE_PATH),
        cache_status=response.headers.get("x-cache-status"),
        css_rate=None,
        http_status=response.status_code,
        latency_ms=round(latency_ms, 1),
    )


def _fetch_futures_table(session: requests.Session, timeout: int) -> FuturesTableData:
    start = time.monotonic()
    response = session.get(FUTURES_TABLE_URL, headers={"User-Agent": UA}, timeout=timeout)
    latency_ms = (time.monotonic() - start) * 1000
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    element = soup.select_one(FUTURES_TABLE_SELECTOR)
    if element is None:
        raise ValueError(f"selector not found: {FUTURES_TABLE_SELECTOR}")
    rate = float(element.get_text(strip=True).replace(",", ""))
    return FuturesTableData(
        rate=rate,
        cache_status=response.headers.get("x-cache-status"),
        http_status=response.status_code,
        latency_ms=round(latency_ms, 1),
    )


def _fetch_yahoo(session: requests.Session, timeout: int) -> YahooData:
    start = time.monotonic()
    response = session.get(YAHOO_URL, headers={"User-Agent": UA}, timeout=timeout)
    latency_ms = (time.monotonic() - start) * 1000
    response.raise_for_status()
    now_utc = datetime.now(UTC)

    payload = json.loads(response.text)
    meta = payload["chart"]["result"][0]["meta"]
    rate = float(meta["regularMarketPrice"])
    source_dt_utc = datetime.fromtimestamp(int(meta["regularMarketTime"]), tz=UTC)

    return YahooData(
        rate=rate,
        source_dt_utc=source_dt_utc,
        delay_sec=(now_utc - source_dt_utc).total_seconds(),
        http_status=response.status_code,
        latency_ms=round(latency_ms, 1),
    )


def _is_target_window(now_kst: datetime) -> bool:
    if now_kst.weekday() >= 5:
        return False
    current = now_kst.time()
    return TARGET_WINDOW_START <= current < TARGET_WINDOW_END


def _format_float(value: Optional[float], digits: int = 3) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value:.{digits}f}"


def _write_jsonl(path: Path, row: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _print_sample(row: Dict[str, object]) -> None:
    window_mark = "TARGET" if row["in_target_window"] else "OUT"
    stale_mark = "STALE" if row["spot_stale"] else "FRESH"
    print(
        f"[{row['sample']:03d}] "
        f"{row['observed_at_kst']} "
        f"[{window_mark}] [{stale_mark}] "
        f"spot={_format_float(row['spot_rate'])} "
        f"(delay={_format_float(row['spot_delay_sec'], 1)}s, cache={row['spot_cache_status'] or '-'}) | "
        f"yahoo={_format_float(row['yahoo_rate'])} "
        f"(delay={_format_float(row['yahoo_delay_sec'], 1)}s) | "
        f"table={_format_float(row['futures_table_rate'])} "
        f"(cache={row['futures_table_cache_status'] or '-'}) | "
        f"futures={_format_float(row['futures_page_rate'])} "
        f"(delay={_format_float(row['futures_page_delay_sec'], 1)}s, cache={row['futures_page_cache_status'] or '-'}) | "
        f"spot-yahoo={_format_float(row['spot_vs_yahoo'])} | "
        f"spot-table={_format_float(row['spot_vs_table'])}"
    )


def _print_summary(rows: List[Dict[str, object]], delay_warn_sec: float, yahoo_warn_diff: float) -> None:
    if not rows:
        print("샘플이 없습니다.")
        return

    spot_rows = [row for row in rows if row.get("spot_rate") is not None]
    yahoo_rows = [row for row in rows if row.get("spot_vs_yahoo") is not None]
    target_rows = [row for row in rows if row["in_target_window"]]
    stale_rows = [row for row in rows if row["spot_stale"]]

    print("\n" + "=" * 72)
    print("요약")
    print("=" * 72)
    print(f"총 샘플: {len(rows)}")
    print(f"spot 성공: {len(spot_rows)} / {len(rows)}")
    print(f"spot stale: {len(stale_rows)} / {len(spot_rows) if spot_rows else 0}")
    print(f"05:45~09:00 KST 샘플: {len(target_rows)}")

    if spot_rows:
        delays = [float(row["spot_delay_sec"]) for row in spot_rows]
        print(
            "spot delay(sec): "
            f"min={min(delays):.1f}, max={max(delays):.1f}, avg={statistics.mean(delays):.1f}"
        )

    if yahoo_rows:
        diffs = [abs(float(row["spot_vs_yahoo"])) for row in yahoo_rows]
        print(
            "spot vs yahoo(abs): "
            f"min={min(diffs):.3f}, max={max(diffs):.3f}, avg={statistics.mean(diffs):.3f}"
        )

    table_rows = [row for row in rows if row.get("spot_vs_table") is not None]
    if table_rows:
        table_diffs = [float(row["spot_vs_table"]) for row in table_rows]
        print(
            "spot vs futures-table: "
            f"min={min(table_diffs):.3f}, max={max(table_diffs):.3f}, avg={statistics.mean(table_diffs):.3f}"
        )

    path_ok = all(row.get("spot_path_ok") for row in rows)
    delay_ok = bool(spot_rows) and max(float(row["spot_delay_sec"]) for row in spot_rows) <= delay_warn_sec
    yahoo_ok = bool(yahoo_rows) and max(abs(float(row["spot_vs_yahoo"])) for row in yahoo_rows) <= yahoo_warn_diff
    target_window_seen = bool(target_rows)

    print("\nGo/No-Go 체크")
    print(f"- 경로 유효성: {'PASS' if path_ok else 'FAIL'}")
    print(
        f"- spot delay <= {delay_warn_sec:.0f}s: "
        f"{'PASS' if delay_ok else 'WARN/FAIL'}"
    )
    print(
        f"- spot vs yahoo <= ±{yahoo_warn_diff:.3f}: "
        f"{'PASS' if yahoo_ok else 'WARN/FAIL'}"
    )
    print(
        f"- 05:45~09:00 KST 샘플 확보: "
        f"{'PASS' if target_window_seen else 'PENDING'}"
    )

    if not target_window_seen:
        print("  아직 목표 시간대 샘플이 없으므로 최종 go/no-go 판단은 보류해야 합니다.")


def _analyze_jsonl(path: Path) -> None:
    """JSONL 샘플을 읽어 source_ts vs rate 변화 분리 분석.

    핵심: DB insert 간격이 아닌 원천 source timestamp 변화와 rate 변화를 분리해서 측정.
    """
    if not path.exists():
        raise SystemExit(f"파일 없음: {path}")

    rows: List[Dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    if not rows:
        print("샘플 없음")
        return

    print("=" * 72)
    print(f"분석: {path}")
    print("=" * 72)
    print(f"총 샘플: {len(rows)}")

    def _parse_kst(s: str) -> datetime:
        return datetime.fromisoformat(s)

    # Spot source_ts 변화 vs rate 변화 분리
    spot_prev_ts: Optional[int] = None
    spot_prev_rate: Optional[float] = None
    spot_prev_obs: Optional[datetime] = None

    spot_ts_change_intervals: List[float] = []   # source_ts가 변한 간격 (초)
    spot_rate_change_intervals: List[float] = [] # rate가 변한 간격 (초)
    spot_ts_changed_count = 0
    spot_rate_changed_count = 0
    spot_ts_changed_rate_same_count = 0  # source_ts는 바뀌었는데 rate는 그대로
    spot_both_frozen_count = 0
    spot_errors = 0
    last_ts_change_time: Optional[datetime] = None
    last_rate_change_time: Optional[datetime] = None

    # Futures page
    fut_prev_ts: Optional[int] = None
    fut_prev_rate: Optional[float] = None
    fut_ts_change_intervals: List[float] = []
    fut_rate_change_intervals: List[float] = []
    fut_ts_changed_count = 0
    fut_rate_changed_count = 0
    fut_ts_changed_rate_same_count = 0
    fut_errors = 0
    last_fut_ts_change_time: Optional[datetime] = None
    last_fut_rate_change_time: Optional[datetime] = None

    # Futures table rate
    table_prev_rate: Optional[float] = None
    table_rate_change_intervals: List[float] = []
    last_table_rate_change_time: Optional[datetime] = None
    table_errors = 0

    # CSS vs NEXT_DATA 일치
    css_next_match = 0
    css_next_mismatch = 0

    # latency stats
    spot_latencies: List[float] = []
    fut_latencies: List[float] = []
    table_latencies: List[float] = []

    # cache status 분포
    spot_cache = {}
    fut_cache = {}

    for row in rows:
        obs = _parse_kst(str(row["observed_at_kst"]))

        # Spot
        if row.get("spot_error"):
            spot_errors += 1
        elif row.get("spot_source_ts_ms") is not None:
            ts = int(row["spot_source_ts_ms"])
            rate = float(row["spot_rate"])
            if spot_prev_ts is not None:
                if ts != spot_prev_ts:
                    spot_ts_changed_count += 1
                    if last_ts_change_time is not None:
                        spot_ts_change_intervals.append((obs - last_ts_change_time).total_seconds())
                    last_ts_change_time = obs
                    if rate == spot_prev_rate:
                        spot_ts_changed_rate_same_count += 1
                else:
                    spot_both_frozen_count += 1
                if rate != spot_prev_rate:
                    spot_rate_changed_count += 1
                    if last_rate_change_time is not None:
                        spot_rate_change_intervals.append((obs - last_rate_change_time).total_seconds())
                    last_rate_change_time = obs
            else:
                last_ts_change_time = obs
                last_rate_change_time = obs
            spot_prev_ts = ts
            spot_prev_rate = rate

            # latency
            if row.get("spot_latency_ms") is not None:
                spot_latencies.append(float(row["spot_latency_ms"]))
            # cache
            c = row.get("spot_cache_status") or "none"
            spot_cache[c] = spot_cache.get(c, 0) + 1
            # css vs next
            css = row.get("spot_css_rate")
            if css is not None:
                if abs(float(css) - rate) < 0.001:
                    css_next_match += 1
                else:
                    css_next_mismatch += 1

        # Futures page
        if row.get("futures_page_error"):
            fut_errors += 1
        elif row.get("futures_page_source_ts_ms") is not None:
            ts = int(row["futures_page_source_ts_ms"])
            rate = float(row["futures_page_rate"])
            if fut_prev_ts is not None:
                if ts != fut_prev_ts:
                    fut_ts_changed_count += 1
                    if last_fut_ts_change_time is not None:
                        fut_ts_change_intervals.append((obs - last_fut_ts_change_time).total_seconds())
                    last_fut_ts_change_time = obs
                    if rate == fut_prev_rate:
                        fut_ts_changed_rate_same_count += 1
                if rate != fut_prev_rate:
                    fut_rate_changed_count += 1
                    if last_fut_rate_change_time is not None:
                        fut_rate_change_intervals.append((obs - last_fut_rate_change_time).total_seconds())
                    last_fut_rate_change_time = obs
            else:
                last_fut_ts_change_time = obs
                last_fut_rate_change_time = obs
            fut_prev_ts = ts
            fut_prev_rate = rate
            if row.get("futures_page_latency_ms") is not None:
                fut_latencies.append(float(row["futures_page_latency_ms"]))
            c = row.get("futures_page_cache_status") or "none"
            fut_cache[c] = fut_cache.get(c, 0) + 1

        # Futures table
        if row.get("futures_table_error"):
            table_errors += 1
        elif row.get("futures_table_rate") is not None:
            rate = float(row["futures_table_rate"])
            if table_prev_rate is not None and rate != table_prev_rate:
                if last_table_rate_change_time is not None:
                    table_rate_change_intervals.append((obs - last_table_rate_change_time).total_seconds())
                last_table_rate_change_time = obs
            elif last_table_rate_change_time is None:
                last_table_rate_change_time = obs
            table_prev_rate = rate
            if row.get("futures_table_latency_ms") is not None:
                table_latencies.append(float(row["futures_table_latency_ms"]))

    def _summarize_intervals(label: str, intervals: List[float]) -> None:
        if not intervals:
            print(f"  {label}: 변화 이벤트 없음")
            return
        intervals_sorted = sorted(intervals)
        med = intervals_sorted[len(intervals_sorted) // 2]
        p95 = intervals_sorted[int(len(intervals_sorted) * 0.95)]
        print(
            f"  {label}: n={len(intervals)}, "
            f"min={min(intervals):.0f}s, med={med:.0f}s, p95={p95:.0f}s, "
            f"max={max(intervals):.0f}s, avg={statistics.mean(intervals):.0f}s"
        )

    print("\n[Spot /indices/usdollar]")
    print(f"  fetch 성공: {len(rows) - spot_errors}, 에러: {spot_errors}")
    print(f"  source_ts 변화 이벤트: {spot_ts_changed_count}")
    print(f"    그 중 rate 동일 (dedup 발생 case): {spot_ts_changed_rate_same_count}")
    print(f"  rate 변화 이벤트: {spot_rate_changed_count}")
    print(f"  양쪽 모두 동일 (stale 페이지): {spot_both_frozen_count}")
    _summarize_intervals("source_ts 변화 간격", spot_ts_change_intervals)
    _summarize_intervals("rate 변화 간격    ", spot_rate_change_intervals)
    if spot_latencies:
        ls = sorted(spot_latencies)
        print(f"  latency ms: med={ls[len(ls)//2]:.0f}, p95={ls[int(len(ls)*0.95)]:.0f}, max={max(ls):.0f}")
    print(f"  cache-status: {spot_cache}")
    if css_next_match + css_next_mismatch > 0:
        print(f"  CSS vs NEXT_DATA: match={css_next_match}, mismatch={css_next_mismatch}")

    print("\n[Futures /currencies/us-dollar-index __NEXT_DATA__]")
    print(f"  fetch 성공: {len(rows) - fut_errors}, 에러: {fut_errors}")
    print(f"  source_ts 변화: {fut_ts_changed_count} (그 중 rate 동일: {fut_ts_changed_rate_same_count})")
    print(f"  rate 변화: {fut_rate_changed_count}")
    _summarize_intervals("source_ts 변화 간격", fut_ts_change_intervals)
    _summarize_intervals("rate 변화 간격    ", fut_rate_change_intervals)
    if fut_latencies:
        ls = sorted(fut_latencies)
        print(f"  latency ms: med={ls[len(ls)//2]:.0f}, p95={ls[int(len(ls)*0.95)]:.0f}, max={max(ls):.0f}")
    print(f"  cache-status: {fut_cache}")

    print("\n[Futures exchange-rates-table]")
    print(f"  fetch 성공: {len(rows) - table_errors}, 에러: {table_errors}")
    _summarize_intervals("rate 변화 간격    ", table_rate_change_intervals)
    if table_latencies:
        ls = sorted(table_latencies)
        print(f"  latency ms: med={ls[len(ls)//2]:.0f}, p95={ls[int(len(ls)*0.95)]:.0f}, max={max(ls):.0f}")

    print("\n[핵심 비교]")
    if spot_ts_change_intervals and fut_ts_change_intervals:
        spot_med = sorted(spot_ts_change_intervals)[len(spot_ts_change_intervals)//2]
        fut_med = sorted(fut_ts_change_intervals)[len(fut_ts_change_intervals)//2]
        print(f"  spot source_ts 중앙값 {spot_med:.0f}s vs futures source_ts 중앙값 {fut_med:.0f}s")
        if spot_med > fut_med * 1.5:
            print(f"  → spot이 더 느림 ({spot_med/fut_med:.1f}x) — spot 페이지 특성 가능성")
        elif fut_med > spot_med * 1.5:
            print(f"  → futures가 더 느림 ({fut_med/spot_med:.1f}x)")
        else:
            print(f"  → 비슷 — Investing 전반 이슈 가능성")

    if spot_rate_changed_count and spot_ts_changed_count:
        ratio = spot_rate_changed_count / spot_ts_changed_count
        print(f"  spot rate 변화 / source_ts 변화 = {ratio:.2f}")
        print(f"  → {(1-ratio)*100:.1f}%의 source_ts 변화가 DB insert 안 됨 (rate dedup)")


def main() -> None:
    parser = argparse.ArgumentParser(description="DXY spot(`/indices/usdollar`) 검증 스크립트")
    parser.add_argument("--samples", type=int, default=10, help="반복 수집 횟수 (기본: 10)")
    parser.add_argument("--interval", type=int, default=10, help="수집 간격 초 (기본: 10)")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout 초 (기본: 20)")
    parser.add_argument(
        "--jsonl",
        type=Path,
        help="샘플별 결과를 JSONL로 저장할 경로",
    )
    parser.add_argument(
        "--analyze",
        type=Path,
        help="기존 JSONL 파일을 읽어 source_ts/rate 변화 분리 분석만 수행",
    )
    parser.add_argument(
        "--delay-warn-sec",
        type=float,
        default=120.0,
        help="spot delay 경고 기준 초 (기본: 120)",
    )
    parser.add_argument(
        "--yahoo-warn-diff",
        type=float,
        default=0.1,
        help="spot vs Yahoo 허용 차이 (기본: 0.1)",
    )
    args = parser.parse_args()

    if args.analyze is not None:
        _analyze_jsonl(args.analyze)
        return

    if args.samples <= 0:
        raise SystemExit("--samples must be >= 1")
    if args.interval <= 0:
        raise SystemExit("--interval must be >= 1")

    session = requests.Session(impersonate="safari17_0")
    rows: List[Dict[str, object]] = []
    previous_spot_ts_ms: Optional[int] = None

    print("=" * 72)
    print("DXY spot 검증 시작")
    print("=" * 72)
    print(f"spot URL      : {SPOT_URL}")
    print(f"futures table : {FUTURES_TABLE_URL} ({FUTURES_TABLE_SELECTOR})")
    print(f"futures page  : {FUTURES_PAGE_URL}")
    print(f"yahoo         : {YAHOO_URL}")
    print(f"samples       : {args.samples}")
    print(f"interval(sec) : {args.interval}")
    print(f"timeout(sec)  : {args.timeout}")
    print()

    for sample_no in range(1, args.samples + 1):
        now_utc = datetime.now(UTC)
        now_kst = now_utc.astimezone(KST)
        row: Dict[str, object] = {
            "sample": sample_no,
            "observed_at_utc": now_utc.isoformat(),
            "observed_at_kst": now_kst.isoformat(timespec="seconds"),
            "in_target_window": _is_target_window(now_kst),
        }

        try:
            spot = _fetch_spot(session, args.timeout)
            row.update(
                {
                    "spot_rate": spot.rate,
                    "spot_css_rate": spot.css_rate,
                    "spot_source_ts_ms": spot.source_ts_ms,
                    "spot_source_dt_utc": spot.source_dt_utc.isoformat(),
                    "spot_delay_sec": round(spot.delay_sec, 1),
                    "spot_cache_status": spot.cache_status,
                    "spot_http_status": spot.http_status,
                    "spot_latency_ms": spot.latency_ms,
                    "spot_path": spot.path,
                    "spot_path_ok": spot.path == ".".join(SPOT_PRICE_PATH),
                }
            )
            row["spot_stale"] = previous_spot_ts_ms == spot.source_ts_ms
            previous_spot_ts_ms = spot.source_ts_ms
        except Exception as exc:
            row.update(
                {
                    "spot_error": f"{type(exc).__name__}: {exc}",
                    "spot_stale": False,
                    "spot_path_ok": False,
                }
            )

        try:
            yahoo = _fetch_yahoo(session, args.timeout)
            row.update(
                {
                    "yahoo_rate": yahoo.rate,
                    "yahoo_source_dt_utc": yahoo.source_dt_utc.isoformat(),
                    "yahoo_delay_sec": round(yahoo.delay_sec, 1),
                    "yahoo_http_status": yahoo.http_status,
                    "yahoo_latency_ms": yahoo.latency_ms,
                }
            )
        except Exception as exc:
            row["yahoo_error"] = f"{type(exc).__name__}: {exc}"

        try:
            ft = _fetch_futures_table(session, args.timeout)
            row["futures_table_rate"] = ft.rate
            row["futures_table_cache_status"] = ft.cache_status
            row["futures_table_http_status"] = ft.http_status
            row["futures_table_latency_ms"] = ft.latency_ms
        except Exception as exc:
            row["futures_table_error"] = f"{type(exc).__name__}: {exc}"

        try:
            futures_page = _fetch_futures_page(session, args.timeout)
            row.update(
                {
                    "futures_page_rate": futures_page.rate,
                    "futures_page_source_ts_ms": futures_page.source_ts_ms,
                    "futures_page_source_dt_utc": futures_page.source_dt_utc.isoformat(),
                    "futures_page_delay_sec": round(futures_page.delay_sec, 1),
                    "futures_page_cache_status": futures_page.cache_status,
                    "futures_page_http_status": futures_page.http_status,
                    "futures_page_latency_ms": futures_page.latency_ms,
                }
            )
        except Exception as exc:
            row["futures_page_error"] = f"{type(exc).__name__}: {exc}"

        if row.get("spot_rate") is not None and row.get("yahoo_rate") is not None:
            row["spot_vs_yahoo"] = round(float(row["spot_rate"]) - float(row["yahoo_rate"]), 3)
        else:
            row["spot_vs_yahoo"] = None

        if row.get("spot_rate") is not None and row.get("futures_table_rate") is not None:
            row["spot_vs_table"] = round(float(row["spot_rate"]) - float(row["futures_table_rate"]), 3)
        else:
            row["spot_vs_table"] = None

        _print_sample(row)

        if row.get("spot_error"):
            print(f"  spot_error: {row['spot_error']}")
        if row.get("yahoo_error"):
            print(f"  yahoo_error: {row['yahoo_error']}")
        if row.get("futures_table_error"):
            print(f"  futures_table_error: {row['futures_table_error']}")
        if row.get("futures_page_error"):
            print(f"  futures_page_error: {row['futures_page_error']}")

        if args.jsonl:
            _write_jsonl(args.jsonl, row)

        rows.append(row)

        if sample_no < args.samples:
            time.sleep(args.interval)

    _print_summary(rows, args.delay_warn_sec, args.yahoo_warn_diff)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
