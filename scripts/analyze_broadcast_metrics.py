"""
broadcast_rates_once 분해 계측 로그 분석 스크립트

PR1 baseline + Phase 1.5 분해 계측 + 인덱스 적용 효과 측정 및 후속 PR2 window
PoC 모니터링에 재사용 가능한 표준 분석 도구.

기존 SSH heredoc + 인라인 Python으로 반복하던 분석을 코드화해 일관된 출력 + 빠른 반복.

입력:
  /app/logs/app.log{,.1,.2,.3} (CustomJsonFormatter JSON line) — 컨테이너 내부 경로

분석 대상 로그:
  message에 "브로드캐스트" / "🅾️" / "⏸️"가 포함된 broadcast_rates_once 호출 결과.

출력 메트릭:
  - skip_reason 분포 (sent / no_changes / no_connections / other)
  - payload_build_ms / investing_total_ms / bank_total_ms / source_rates_legacy_ms /
    redis_get_ms / serialize_diff_ms / broadcast_send_ms / build_graph_buckets_ms 통계
    (n / avg / p50 / p95 / p99 / max)
  - per-pair timing (usd-krw / jpy-krw / eur-krw)
  - 상위 spike top N (--top, 기본 20)

실행 방법:
  # EC2 운영 환경 (컨테이너 내부 로그 접근)
  docker compose run --rm fastapi python scripts/analyze_broadcast_metrics.py \
    --since "2026-04-30T14:30:00+09:00"

  # 윈도우 끝 지정 + JSON 출력
  docker compose run --rm fastapi python scripts/analyze_broadcast_metrics.py \
    --since "2026-04-30T14:30:00+09:00" \
    --until "2026-04-30T17:30:00+09:00" \
    --top 30 --json
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

KST = timezone(timedelta(hours=9))

DEFAULT_LOG_PATHS = [
    "/app/logs/app.log.3",
    "/app/logs/app.log.2",
    "/app/logs/app.log.1",
    "/app/logs/app.log",
]

METRICS = [
    "payload_build_ms",
    "investing_total_ms",
    "bank_total_ms",
    "source_rates_legacy_ms",
    "redis_get_ms",
    "serialize_diff_ms",
    "build_graph_buckets_ms",
    "broadcast_send_ms",
]


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


def is_broadcast_log(rec: Dict[str, Any]) -> bool:
    msg = rec.get("message", "")
    return ("브로드캐스트" in msg) or ("🅾️" in msg) or ("⏸️" in msg)


def classify_skip_reason(rec: Dict[str, Any]) -> str:
    msg = rec.get("message", "")
    if "변경사항 없음" in msg:
        return "no_changes"
    if "활성 연결 없음" in msg:
        return "no_connections"
    if "브로드캐스트 완료" in msg or "Redis 업데이트 완료" in msg:
        return "sent"
    return "other"


def percentile(arr: List[float], p: float) -> float:
    if not arr:
        return 0.0
    a = sorted(arr)
    k = (len(a) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(a) - 1)
    return a[f] + (a[c] - a[f]) * (k - f)


def load_records(
    paths: List[str], since: datetime, until: Optional[datetime]
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for path in paths:
        try:
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = rec.get("timestamp", "")
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts)
                    except Exception:
                        continue
                    if dt < since:
                        continue
                    if until is not None and dt > until:
                        continue
                    if not is_broadcast_log(rec):
                        continue
                    records.append(rec)
        except FileNotFoundError:
            pass
    return records


def metric_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"n": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "min": 0}
    return {
        "n": len(values),
        "avg": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
        "min": min(values),
    }


def collect_pair_timings(records: Iterable[Dict[str, Any]]) -> Dict[str, List[float]]:
    pair_data: Dict[str, List[float]] = {}
    for r in records:
        pt = r.get("pair_timings", {})
        if isinstance(pt, dict):
            for p, v in pt.items():
                if isinstance(v, (int, float)):
                    pair_data.setdefault(p, []).append(v)
    return pair_data


def format_table(records: List[Dict[str, Any]], top: int, since: datetime, until: Optional[datetime]) -> str:
    lines: List[str] = []
    until_str = until.isoformat() if until else "now"
    lines.append(f"=== Broadcast metrics window: {since.isoformat()} ~ {until_str} ===")
    lines.append(f"Total records: {len(records)}")

    skip_bucket = Counter(classify_skip_reason(r) for r in records)
    total = sum(skip_bucket.values()) or 1
    lines.append("")
    lines.append("[skip_reason]")
    for k, v in sorted(skip_bucket.items(), key=lambda x: -x[1]):
        lines.append(f"  {k:15s}: {v:6d} ({v*100/total:.1f}%)")

    lines.append("")
    lines.append("[메트릭 통계]")
    lines.append(
        f"  {'metric':25s}  {'n':>5s}  {'avg':>8s}  {'p50':>8s}  {'p95':>8s}  {'p99':>8s}  {'max':>9s}"
    )
    lines.append("  " + "-" * 80)
    for m in METRICS:
        vals = [r[m] for r in records if isinstance(r.get(m), (int, float))]
        if not vals:
            continue
        s = metric_stats(vals)
        lines.append(
            f"  {m:25s}  {s['n']:5d}  {s['avg']:8.2f}  {s['p50']:8.2f}  "
            f"{s['p95']:8.2f}  {s['p99']:8.2f}  {s['max']:9.2f}"
        )

    pair_data = collect_pair_timings(records)
    if pair_data:
        lines.append("")
        lines.append("[per-pair timing]")
        lines.append(
            f"  {'pair':10s}  {'n':>5s}  {'avg':>8s}  {'p50':>8s}  {'p95':>8s}  {'p99':>8s}  {'max':>9s}"
        )
        lines.append("  " + "-" * 70)
        for p, vals in sorted(pair_data.items()):
            s = metric_stats(vals)
            lines.append(
                f"  {p:10s}  {s['n']:5d}  {s['avg']:8.2f}  {s['p50']:8.2f}  "
                f"{s['p95']:8.2f}  {s['p99']:8.2f}  {s['max']:9.2f}"
            )

    big = sorted(
        [r for r in records if isinstance(r.get("payload_build_ms"), (int, float))],
        key=lambda r: -r["payload_build_ms"],
    )[:top]
    if big:
        lines.append("")
        lines.append(f"[Top {top} payload_build_ms spike]")
        lines.append(
            f"  {'timestamp':22s}  {'pb':>8s}  {'inv':>7s}  {'bank':>7s}  {'src':>7s}  {'rdis':>6s}  {'send':>6s}"
        )
        for r in big:
            ts = r.get("timestamp", "")[:19]
            pb = r.get("payload_build_ms", 0)
            inv = r.get("investing_total_ms", 0) or 0
            bk = r.get("bank_total_ms", 0) or 0
            sr = r.get("source_rates_legacy_ms", 0) or 0
            rd = r.get("redis_get_ms", 0) or 0
            sn = r.get("broadcast_send_ms", 0) or 0
            lines.append(
                f"  {ts:22s}  {pb:8.2f}  {inv:7.2f}  {bk:7.2f}  {sr:7.2f}  {rd:6.2f}  {sn:6.2f}"
            )

    return "\n".join(lines)


def format_json(records: List[Dict[str, Any]], top: int, since: datetime, until: Optional[datetime]) -> str:
    skip_bucket = Counter(classify_skip_reason(r) for r in records)

    metric_results: Dict[str, Dict[str, float]] = {}
    for m in METRICS:
        vals = [r[m] for r in records if isinstance(r.get(m), (int, float))]
        if vals:
            metric_results[m] = metric_stats(vals)

    pair_data = collect_pair_timings(records)
    pair_results = {p: metric_stats(vals) for p, vals in pair_data.items()}

    big = sorted(
        [r for r in records if isinstance(r.get("payload_build_ms"), (int, float))],
        key=lambda r: -r["payload_build_ms"],
    )[:top]
    top_spikes = [
        {
            "timestamp": r.get("timestamp", ""),
            "payload_build_ms": r.get("payload_build_ms"),
            "investing_total_ms": r.get("investing_total_ms"),
            "bank_total_ms": r.get("bank_total_ms"),
            "source_rates_legacy_ms": r.get("source_rates_legacy_ms"),
            "redis_get_ms": r.get("redis_get_ms"),
            "broadcast_send_ms": r.get("broadcast_send_ms"),
        }
        for r in big
    ]

    return json.dumps(
        {
            "window": {
                "since": since.isoformat(),
                "until": until.isoformat() if until else None,
            },
            "total_records": len(records),
            "skip_reason": dict(skip_bucket),
            "metrics": metric_results,
            "pair_timings": pair_results,
            "top_spikes": top_spikes,
        },
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="ISO 8601 시작 시각 (예: 2026-04-30T14:30:00+09:00)")
    parser.add_argument("--until", default=None, help="ISO 8601 끝 시각 (기본: 현재)")
    parser.add_argument("--top", type=int, default=20, help="Top N spike 출력 (기본 20)")
    parser.add_argument("--json", action="store_true", help="JSON 출력 (기계 판독용)")
    parser.add_argument(
        "--paths",
        nargs="*",
        default=DEFAULT_LOG_PATHS,
        help="분석 대상 로그 파일 경로 (기본: app.log* 4개)",
    )
    args = parser.parse_args()

    since = parse_iso(args.since)
    until = parse_iso(args.until) if args.until else None

    records = load_records(args.paths, since, until)

    if args.json:
        print(format_json(records, args.top, since, until))
    else:
        print(format_table(records, args.top, since, until))


if __name__ == "__main__":
    main()
