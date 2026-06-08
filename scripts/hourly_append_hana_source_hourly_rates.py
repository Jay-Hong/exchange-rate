#!/usr/bin/env python3
"""Hana per-currency hourly append — PLAN (ADR-035 D3 — going-forward freshness).

source_hourly_rates의 Hana(usd/jpy/eur)를 going-forward로 최신 유지하는 append의 **PLAN mode**.
**read-only (write 0)** — 통화별로 어떤 bucket을 upsert/prune할지 산출 + 로그. **write path / cron은 별도 GO.**

설계 (Investing append와 동형, Hana 고유):
  - cadence: hourly (HH:09 권장 — Bithumb :05, Investing :07 다음 스태거)
  - correctness = 최근 window_days일 catch-up re-roll + idempotent upsert
  - 직전 완료 hour까지만 — 현재 incomplete hour 제외
  - retention prune 계획: config.SOURCE_HOURLY_RETENTION_DAYS(14d) 밖 bucket (per-currency)
  - **per-currency 독립** — 통화별 fetch/rollup/classification/prune
  - **carry-forward 미적용** — 실관측 buckets만 (canonical source of truth, step render는 read-side defer)

재사용 경계 (Codex 합의):
  - **pure generic은 Bithumb append(`A`)에서 import**: previous_complete_hour / kst_naive_to_utc_naive /
    retention_cutoff_kst / AppendPlan (source 무관)
  - fetch/rollup/상수/resolve_currencies는 Hana validator(`HV`): fetch_hana_observations + rollup_to_hourly(ohlc_quality 분기)
  - classification은 Hana-local — Investing 동형 + **ohlc_quality 비교 포함**(close_only↔observed_rollup 전이는
    point_count 함수라 metadata가 이미 잡지만, rollup quality 도출 변경 대비 defense-in-depth)

사용법 (read-only):
  python scripts/hourly_append_hana_source_hourly_rates.py [--currency all] [--window-days 2] [--as-of 2026-06-08T14:30]
"""

# 표준 라이브러리
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

# 프로젝트 루트 + scripts 디렉토리 sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _SCRIPT_DIR)

# 로컬 애플리케이션
import backfill_hana_source_hourly_rates as HV  # noqa: E402  fetch + rollup + 상수 + resolve_currencies
import hourly_append_source_hourly_rates as A  # noqa: E402  pure generic helper (Bithumb append 불변 재사용)
import app.source_hourly_rates as H  # noqa: E402
from app import config  # noqa: E402
from app.models import SourceHourlyRate  # noqa: E402

_KST_TZ = timezone(timedelta(hours=9))


def fetch_candidates_hana(db, now_kst: datetime, window_days: int, asset: str):
    """직전 완료 hour까지 window_days일 bank_exchange_rates(bank=hana) rollup → (window_start, prev_complete, candidates).

    window/fetch 경계는 Bithumb/Investing append와 동일(pure generic A 재사용). fetch/rollup만 Hana.
    """
    prev_complete = A.previous_complete_hour(now_kst)
    window_start = prev_complete - timedelta(hours=window_days * 24 - 1)
    fetch_start_utc = A.kst_naive_to_utc_naive(window_start)
    fetch_end_utc = A.kst_naive_to_utc_naive(prev_complete + timedelta(hours=1)) - timedelta(microseconds=1)
    obs = HV.fetch_hana_observations(db, fetch_start_utc, fetch_end_utc, asset)
    return window_start, prev_complete, HV.rollup_to_hourly(obs, asset)


def compute_append_plan_hana(
    db,
    asset: str,
    candidates: list,
    window_start: datetime,
    prev_complete: datetime,
    now_kst: datetime,
    retention_days: int,
) -> "A.AppendPlan":
    """Hana per-currency plan — classification(inserted/updated_same/changed/metadata) + prune count.

    Investing 동형 + **ohlc_quality 비교 포함**: late tick으로 single-tick(close_only)→multi-tick(observed_rollup)
    전이 시 ohlc_quality 변동. 이 전이는 point_count(1→2) 함수라 metadata 비교가 이미 잡지만, rollup quality
    도출 로직 변경 대비 defense-in-depth로 명시 비교 (no-op 오분류 차단).
    """
    existing = {e.bucket_ts_kst: e for e in
                H.get_range(db, HV.SOURCE, asset, window_start, prev_complete)}
    inserted = updated_same = updated_metadata_changed = 0
    changed_buckets = []
    for c in candidates:
        e = existing.get(c["bucket_ts_kst"])
        if e is None:
            inserted += 1
        elif (float(e.close) != float(c["close"]) or float(e.high) != float(c["high"])
              or float(e.low) != float(c["low"])):
            changed_buckets.append(c["bucket_ts_kst"])           # price(close/high/low) 변동
        elif (e.ohlc_quality != c["ohlc_quality"]
              or (e.metadata_json or {}) != (c.get("metadata_json") or {})):
            updated_metadata_changed += 1                         # ohlc_quality 또는 metadata 변동 (late tick)
        else:
            updated_same += 1                                     # 완전 동일 (진짜 no-op)

    retention_cutoff = A.retention_cutoff_kst(now_kst, retention_days)
    prune_count = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == HV.SOURCE,
            SourceHourlyRate.asset == asset,
            SourceHourlyRate.bucket_ts_kst < retention_cutoff,
        )
        .count()
    )

    latest = candidates[-1] if candidates else None
    latest_lag = None
    latest_last_ts = None
    if latest is not None:
        latest_last_ts = latest["metadata_json"].get("last_ts_kst")
        if latest_last_ts:
            last_dt = datetime.fromisoformat(latest_last_ts)
            now_aware = now_kst if now_kst.tzinfo else now_kst.replace(tzinfo=_KST_TZ)
            latest_lag = (now_aware - last_dt).total_seconds()

    return A.AppendPlan(
        window_start=window_start,
        window_end=prev_complete,
        candidate_count=len(candidates),
        inserted=inserted,
        updated_same=updated_same,
        updated_changed=len(changed_buckets),
        updated_metadata_changed=updated_metadata_changed,
        changed_buckets=changed_buckets[:10],
        prune_count=prune_count,
        retention_cutoff=retention_cutoff,
        latest_bucket_ts=latest["bucket_ts_kst"] if latest else None,
        latest_last_ts_kst=latest_last_ts,
        latest_lag_seconds=latest_lag,
    )


def print_plan_hana(cur: str, asset: str, plan: "A.AppendPlan") -> None:
    print(f"=== {cur} ({asset}) ===")
    print(f"  window (직전 완료 hour까지): {plan.window_start} ~ {plan.window_end}")
    print(f"  candidate bucket: {plan.candidate_count}")
    print(f"  upsert 계획: inserted={plan.inserted} / updated_same(no-op)={plan.updated_same} "
          f"/ updated_changed(price)={plan.updated_changed} "
          f"/ updated_metadata_changed(ohlc_quality/metadata)={plan.updated_metadata_changed}")
    if plan.changed_buckets:
        print(f"    changed 샘플: {[b.isoformat() for b in plan.changed_buckets]}")
    print(f"  prune 계획: retention_cutoff={plan.retention_cutoff} 미만 {plan.prune_count} bucket 삭제 대상")
    lag = f"{plan.latest_lag_seconds:.0f}s" if plan.latest_lag_seconds is not None else "n/a"
    print(f"  latest bucket: {plan.latest_bucket_ts} / last_tick={plan.latest_last_ts_kst} / 수집 lag={lag}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hana per-currency hourly append PLAN (ADR-035 D3 — read-only. write/cron은 별 GO)"
    )
    parser.add_argument("--currency", default="all", help="usd|jpy|eur|all (기본 all)")
    parser.add_argument("--window-days", type=int, default=2, help="catch-up re-roll window (기본 2일)")
    parser.add_argument("--as-of", default=None, help="기준 시각 ISO (테스트/재현용, 기본 now KST)")
    args = parser.parse_args()

    if args.window_days <= 0:
        print(f"[CONFIG 실패] --window-days는 1 이상이어야 함 (got {args.window_days})")
        sys.exit(2)

    try:
        currencies = HV.resolve_currencies(args.currency)
    except ValueError as e:
        print(f"[CONFIG 실패] {e}")
        sys.exit(2)

    if args.as_of:
        now_kst = datetime.fromisoformat(args.as_of)
        if now_kst.tzinfo is not None:
            now_kst = now_kst.astimezone(_KST_TZ).replace(tzinfo=None)
    else:
        now_kst = datetime.now(_KST_TZ).replace(tzinfo=None)

    retention_days = config.SOURCE_HOURLY_RETENTION_DAYS

    print(f"모드: PLAN (read-only, write 0) — Hana per-currency hourly append")
    print(f"통화: {currencies} / now(KST)={now_kst} / window={args.window_days}d / retention={retention_days}d")

    from app.database import SessionLocal
    for cur in currencies:
        asset = HV._CURRENCY_MAP[cur]
        db = SessionLocal()
        try:
            window_start, prev_complete, candidates = fetch_candidates_hana(
                db, now_kst, args.window_days, asset)
            plan = compute_append_plan_hana(
                db, asset, candidates, window_start, prev_complete, now_kst, retention_days)
        finally:
            db.close()
        print()
        print_plan_hana(cur, asset, plan)

    print()
    print("[PLAN ONLY] 실제 upsert/prune은 write path(별 GO) / cron 연결도 별 GO.")


if __name__ == "__main__":
    main()
