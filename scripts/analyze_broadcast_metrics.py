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
    # PR3 — Redis-first read path (mirror cycle freshness, latest:index 기준)
    "mirror_age_ms",
    # PR3.5 — fetch_rates_from_redis 단계별 분해 + assemble/dxy/unmeasured.
    # 과거 PR1/PR2/PR3 로그(필드 없음)는 metric_stats가 자동 제외 (n=0).
    "latest_fetch_total_ms",
    "latest_index_get_ms",
    "latest_index_parse_ms",
    "latest_data_get_ms",
    "latest_decode_ms",
    "latest_key_count",
    "payload_assemble_ms",
    "dxy_query_ms",
    "payload_assemble_without_dxy_ms",
    "payload_build_unmeasured_ms",
    # PR5 — DXY mirror (Redis DXY GET 시간). DB DXY 조회는 dxy_query_ms 그대로 유지
    # — Redis 성공 시 미기록이라 PR5 후 dxy_query_ms n 급감으로 효과 가시화.
    "latest_dxy_get_ms",
]

# PR5 — DXY 전용 fallback reason 분류 (rates fallback_reason과 분리).
# 과거 로그(필드 없음)는 Counter가 자연 제외.
DXY_FALLBACK_REASONS = ["redis_miss", "redis_stale", "redis_error", "circuit_open"]

# PR3 — fallback reason 분류 (broadcast_rates_once의 timings extra와 일치).
# 과거 PR1/PR2 로그에는 fallback_reason 필드가 없어 Counter가 자연스럽게 0건 처리.
# Z-2f(ADR-030) 후 추가: per_key_stale (개별 data key mirrored_at stale).
# redis_stale은 Z-2f 후 rates path에서 deprecated이지만, 과거 로그 호환 및
# rollback 대비로 분류 유지.
# B step2(mirror-retirement): per_key_stale_ceiling = 완화 ON인데 age가 ceiling 초과라 fallback.
FALLBACK_REASONS = ["redis_miss", "redis_stale", "redis_error", "circuit_open", "per_key_stale", "per_key_stale_ceiling"]


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


def classify_latest_source(rec: Dict[str, Any]) -> str:
    """PR3 timings.latest_source 분류. 과거 PR1/PR2 로그 호환을 위해 'none' 반환.

    - 'redis': broadcast가 Redis-first path로 rates 조립 (PR3 success)
    - 'db_fallback': Redis miss/stale/error/circuit_open으로 DB fallback (PR3)
    - 'none': latest_source 필드 자체 없음 (PR1/PR2 로그 또는 REDIS_LATEST_ENABLED=false)
    """
    src = rec.get("latest_source")
    if src == "redis":
        return "redis"
    if src == "db_fallback":
        return "db_fallback"
    return "none"


def classify_dxy_path(rec: Dict[str, Any]) -> str:
    """PR5 timings.dxy_path 분류 (DXY mirror cache 경로).

    - 'redis': DXY Redis 성공 (PR5 success)
    - 'db_fallback': DXY Redis 실패 → DB fallback 성공 (DXY-only, rates는 영향 없음)
    - 'missing': DXY Redis 실패 + DB도 없음 → indices 키 생략
    - 'none': dxy_path 필드 자체 없음 (PR4 이전 로그 또는 rates fallback path)
    """
    p = rec.get("dxy_path")
    if p in ("redis", "db_fallback", "missing"):
        return p
    return "none"


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

    # PR3 — latest_source 분포 + fallback_reason 카운트.
    # 과거 PR1/PR2 로그(latest_source 필드 없음)는 'none'으로 분류 → PR3 전후
    # 비교 시 'none' 비율로 PR3 적용 시점 파악 가능.
    latest_bucket = Counter(classify_latest_source(r) for r in records)
    pr3_active = (latest_bucket["redis"] + latest_bucket["db_fallback"]) > 0
    if pr3_active:
        lines.append("")
        lines.append("[PR3 latest_source 분포]")
        total_pr3 = sum(latest_bucket.values()) or 1
        for k in ("redis", "db_fallback", "none"):
            v = latest_bucket.get(k, 0)
            lines.append(f"  {k:15s}: {v:6d} ({v*100/total_pr3:.1f}%)")
        # redis_hit_rate: PR3 적용 records (redis + db_fallback) 중 redis 비율
        pr3_records = latest_bucket["redis"] + latest_bucket["db_fallback"]
        if pr3_records > 0:
            hit_rate = latest_bucket["redis"] * 100 / pr3_records
            lines.append(
                f"  redis_hit_rate (PR3 records 한정): {hit_rate:.2f}% "
                f"({latest_bucket['redis']}/{pr3_records})"
            )

        fallback_bucket = Counter()
        for r in records:
            reason = r.get("fallback_reason")
            if reason:
                fallback_bucket[reason] += 1
        if fallback_bucket:
            lines.append("")
            lines.append("[PR3 fallback_reason 분포] (rates Redis fallback 전용)")
            fb_total = sum(fallback_bucket.values()) or 1
            for k in FALLBACK_REASONS:
                v = fallback_bucket.get(k, 0)
                if v > 0:
                    lines.append(f"  {k:15s}: {v:6d} ({v*100/fb_total:.1f}%)")
            # 분류기 외 reason도 노출 (방어)
            unknown = {k: v for k, v in fallback_bucket.items() if k not in FALLBACK_REASONS}
            for k, v in sorted(unknown.items(), key=lambda x: -x[1]):
                lines.append(f"  {k:15s}: {v:6d} ({v*100/fb_total:.1f}%) [unknown]")

    # B step2 — per_key_stale 완화로 old-but-present를 서빙한 record 집계 (rollout 지표).
    # mirror ON 중엔 거의 0(mirror가 fresh 유지) → 무해성; 실제 노출은 mirror cadence 축소 후.
    served_recs = [r for r in records if r.get("served_stale_key_count")]
    if served_recs:
        total_served = sum(r.get("served_stale_key_count", 0) for r in served_recs)
        max_age = max((r.get("served_stale_max_age_s", 0) or 0) for r in served_recs)
        lines.append("")
        lines.append("[B step2 served_stale 분포] (per_key_stale 완화 = old-but-present 서빙)")
        lines.append(f"  served_stale broadcasts : {len(served_recs)} / {len(records)}")
        lines.append(f"  served_stale key total  : {total_served}")
        lines.append(f"  served_stale max age (s): {max_age}")

    # PR5 — DXY path 분포 + DXY fallback reason (rates와 분리)
    dxy_bucket = Counter(classify_dxy_path(r) for r in records)
    pr5_active = (dxy_bucket["redis"] + dxy_bucket["db_fallback"] + dxy_bucket["missing"]) > 0
    if pr5_active:
        lines.append("")
        lines.append("[PR5 dxy_path 분포]")
        total_dxy = sum(dxy_bucket.values()) or 1
        for k in ("redis", "db_fallback", "missing", "none"):
            v = dxy_bucket.get(k, 0)
            lines.append(f"  {k:15s}: {v:6d} ({v*100/total_dxy:.1f}%)")
        # dxy_redis_hit_rate: PR5 records (redis + db_fallback + missing) 중 redis 비율
        pr5_records = dxy_bucket["redis"] + dxy_bucket["db_fallback"] + dxy_bucket["missing"]
        if pr5_records > 0:
            hit_rate = dxy_bucket["redis"] * 100 / pr5_records
            lines.append(
                f"  dxy_redis_hit_rate (PR5 records 한정): {hit_rate:.2f}% "
                f"({dxy_bucket['redis']}/{pr5_records})"
            )

        dxy_fb_bucket = Counter()
        for r in records:
            reason = r.get("latest_dxy_fallback_reason")
            if reason:
                dxy_fb_bucket[reason] += 1
        if dxy_fb_bucket:
            lines.append("")
            lines.append("[PR5 latest_dxy_fallback_reason 분포] (DXY-only fallback)")
            dxy_fb_total = sum(dxy_fb_bucket.values()) or 1
            for k in DXY_FALLBACK_REASONS:
                v = dxy_fb_bucket.get(k, 0)
                if v > 0:
                    lines.append(f"  {k:15s}: {v:6d} ({v*100/dxy_fb_total:.1f}%)")

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

    # PR3 — latest_source 분포 + fallback_reason 카운트 + redis_hit_rate
    latest_bucket = Counter(classify_latest_source(r) for r in records)
    fallback_bucket = Counter()
    for r in records:
        reason = r.get("fallback_reason")
        if reason:
            fallback_bucket[reason] += 1
    pr3_records = latest_bucket["redis"] + latest_bucket["db_fallback"]
    redis_hit_rate = (
        latest_bucket["redis"] * 100 / pr3_records if pr3_records > 0 else None
    )

    # PR5 — dxy_path 분포 + latest_dxy_fallback_reason + dxy_redis_hit_rate
    dxy_bucket = Counter(classify_dxy_path(r) for r in records)
    dxy_fb_bucket = Counter()
    for r in records:
        reason = r.get("latest_dxy_fallback_reason")
        if reason:
            dxy_fb_bucket[reason] += 1
    pr5_records = dxy_bucket["redis"] + dxy_bucket["db_fallback"] + dxy_bucket["missing"]
    dxy_redis_hit_rate = (
        dxy_bucket["redis"] * 100 / pr5_records if pr5_records > 0 else None
    )

    # B step2 — per_key_stale 완화 served_stale 집계 (rollout 지표, --json 자동 수집용)
    served_recs = [r for r in records if r.get("served_stale_key_count")]
    served_stale = {
        "broadcasts": len(served_recs),
        "key_total": sum(r.get("served_stale_key_count", 0) for r in served_recs),
        "max_age_s": max((r.get("served_stale_max_age_s", 0) or 0) for r in served_recs) if served_recs else 0,
    }

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
            # PR3 필드 (없으면 None — 과거 로그 호환)
            "latest_source": r.get("latest_source"),
            "fallback_reason": r.get("fallback_reason"),
            "mirror_age_ms": r.get("mirror_age_ms"),
            # PR5 필드
            "dxy_path": r.get("dxy_path"),
            "latest_dxy_fallback_reason": r.get("latest_dxy_fallback_reason"),
            "latest_dxy_get_ms": r.get("latest_dxy_get_ms"),
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
            # PR3 신규 (과거 로그 호환: latest_source 없으면 'none' 카운트만)
            "pr3": {
                "latest_source": dict(latest_bucket),
                "fallback_reason": dict(fallback_bucket),
                "redis_hit_rate_percent": redis_hit_rate,
                "pr3_records": pr3_records,
            },
            # PR5 신규 (DXY mirror — rates와 분리. 과거 로그는 dxy_path 'none' 카운트)
            "pr5": {
                "dxy_path": dict(dxy_bucket),
                "latest_dxy_fallback_reason": dict(dxy_fb_bucket),
                "dxy_redis_hit_rate_percent": dxy_redis_hit_rate,
                "pr5_records": pr5_records,
            },
            # B step2 (mirror-retirement) — per_key_stale 완화로 서빙한 stale 집계
            "served_stale": served_stale,
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
