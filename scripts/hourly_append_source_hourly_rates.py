#!/usr/bin/env python3
"""Bithumb hourly append PLAN (ADR-035 D3 Step — going-forward freshness).

source_hourly_rates를 going-forward로 최신 유지하기 위한 append의 **plan/dry-run 모드**.
실제 write·cron 연결은 별도 GO. 본 PR은 plan만 — 어떤 bucket을 upsert/prune할지 + 로그.

설계 (다른 Claude / Codex 수렴):
  - cadence: hourly (HH:05 권장 — correctness는 catch-up이 보장, minute은 freshness tuning)
  - **correctness = 최근 2일 catch-up re-roll + idempotent upsert** (늦게 들어온 tick을 다음 실행에서 보정)
  - **직전 완료 hour까지만** — 현재 incomplete hour는 제외 (그 hour는 다음 실행에서 완성 후 반영)
  - require_empty 미사용 (append는 갱신 허용)
  - retention prune: config.SOURCE_HOURLY_RETENTION_DAYS(14d) 밖 bucket 삭제 계획
  - 정상 현상: latest hour 값이 다음 실행에서 미세 revise 가능 (불완전→완전). 로그로 표면화.

본 plan은 read-only: source_rates(rollup) + source_hourly_rates(기존 비교) 조회만, write 0.

사용법 (plan, read-only):
  python scripts/hourly_append_source_hourly_rates.py [--window-days 2] [--as-of 2026-06-08T14:30]
"""

# 표준 라이브러리
import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# 프로젝트 루트 + scripts 디렉토리 sys.path (backfill 함수 재사용)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _SCRIPT_DIR)

# 로컬 애플리케이션
import backfill_bithumb_source_hourly_rates as B  # noqa: E402
import app.source_hourly_rates as H  # noqa: E402
from app import config  # noqa: E402
from app.models import SourceHourlyRate  # noqa: E402

_KST_TZ = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────
# 시간 / window
# ─────────────────────────────────────────────────────────────

def kst_naive_to_utc_naive(dt_kst: datetime) -> datetime:
    """naive KST datetime → naive UTC datetime (source_rates 조회 경계용)."""
    return dt_kst.replace(tzinfo=_KST_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def previous_complete_hour(now_kst: datetime) -> datetime:
    """now 기준 직전 완료 hour bucket (naive KST). 현재 incomplete hour 제외.

    예: now=14:30 → 현재 hour 14:00(incomplete) → 직전 완료 13:00 반환.
    """
    current_hour = now_kst.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    return current_hour - timedelta(hours=1)


@dataclass(frozen=True)
class AppendPlan:
    window_start: datetime          # 첫 bucket (KST naive)
    window_end: datetime            # 마지막 bucket = 직전 완료 hour (KST naive)
    candidate_count: int            # rollup 결과 bucket 수
    inserted: int                   # 신규 (기존에 없음)
    updated_same: int               # 기존 존재 + price OHLC + metadata 완전 동일 (진짜 no-op)
    updated_changed: int            # 기존 존재 + price(close/high/low) 변동 (late tick revise)
    updated_metadata_changed: int   # 기존 존재 + price 동일 but metadata_json 변동 (late tick, 가격 무변)
    changed_buckets: list           # updated_changed(price) bucket 샘플
    prune_count: int                # retention 밖 (삭제 대상)
    retention_cutoff: datetime      # 이 시각 미만 bucket prune
    latest_bucket_ts: Optional[datetime]
    latest_last_ts_kst: Optional[str]
    latest_lag_seconds: Optional[float]   # now - latest bucket 마지막 tick (수집 지연)


def compute_append_plan(
    db,
    now_kst: datetime,
    window_days: int,
    retention_days: int,
) -> AppendPlan:
    """append plan 계산 (read-only — write 0).

    1) 직전 완료 hour까지의 최근 window_days 일 source_rates rollup → candidate bucket
    2) 기존 source_hourly_rates와 비교 → inserted / updated_same / updated_changed
    3) retention(now - retention_days) 밖 bucket prune 계획
    """
    prev_complete = previous_complete_hour(now_kst)
    window_start = prev_complete - timedelta(hours=window_days * 24 - 1)

    # source_rates 조회: [window_start, prev_complete 59:59] KST → UTC
    # (window_start = 직전 완료 hour 기준 rolling window_days*24h 시작점 — 00:00 고정 아님)
    fetch_start_utc = kst_naive_to_utc_naive(window_start)
    fetch_end_utc = kst_naive_to_utc_naive(prev_complete + timedelta(hours=1)) - timedelta(microseconds=1)
    ticks = B.fetch_bithumb_ticks(db, fetch_start_utc, fetch_end_utc)
    candidates = B.rollup_ticks_to_hourly(ticks)

    # 기존 bucket 비교 — price 변동 / metadata-only 변동 / 완전 동일 구분
    # (upsert는 metadata_json도 갱신하므로, price 동일해도 point_count/last_ts 변경은 실제 row 변경 = no-op 아님)
    existing = {e.bucket_ts_kst: e for e in
                H.get_range(db, B.SOURCE, B.ASSET, window_start, prev_complete)}
    inserted = updated_same = updated_metadata_changed = 0
    changed_buckets = []
    for c in candidates:
        e = existing.get(c["bucket_ts_kst"])
        if e is None:
            inserted += 1
        elif (float(e.close) != c["close"] or float(e.high) != c["high"]
              or float(e.low) != c["low"]):
            changed_buckets.append(c["bucket_ts_kst"])          # price(close/high/low) 변동
        elif (e.metadata_json or {}) != (c.get("metadata_json") or {}):
            updated_metadata_changed += 1                        # price 동일 + metadata 변동 (late tick)
        else:
            updated_same += 1                                    # price + metadata 완전 동일 (진짜 no-op)

    # retention prune 계획
    retention_cutoff = now_kst.replace(minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(days=retention_days)
    prune_count = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == B.SOURCE,
            SourceHourlyRate.asset == B.ASSET,
            SourceHourlyRate.bucket_ts_kst < retention_cutoff,
        )
        .count()
    )

    # latest bucket 진단 (수집 지연 = now - 마지막 tick)
    latest = candidates[-1] if candidates else None
    latest_lag = None
    latest_last_ts = None
    if latest is not None:
        latest_last_ts = latest["metadata_json"].get("last_ts_kst")
        if latest_last_ts:
            last_dt = datetime.fromisoformat(latest_last_ts)
            now_aware = now_kst if now_kst.tzinfo else now_kst.replace(tzinfo=_KST_TZ)
            latest_lag = (now_aware - last_dt).total_seconds()

    return AppendPlan(
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


def print_plan(plan: AppendPlan) -> None:
    print("=== Bithumb hourly append PLAN (read-only, write 0) ===")
    print(f"  window (직전 완료 hour까지): {plan.window_start} ~ {plan.window_end}")
    print(f"  candidate bucket: {plan.candidate_count}")
    print(f"  upsert 계획: inserted={plan.inserted} / updated_same(no-op)={plan.updated_same} "
          f"/ updated_changed(price revise)={plan.updated_changed} "
          f"/ updated_metadata_changed(late-tick, price 무변)={plan.updated_metadata_changed}")
    if plan.changed_buckets:
        print(f"    changed 샘플: {[b.isoformat() for b in plan.changed_buckets]}")
    print(f"  prune 계획: retention_cutoff={plan.retention_cutoff} 미만 {plan.prune_count} bucket 삭제 대상")
    print(f"  latest bucket: {plan.latest_bucket_ts} / last_tick={plan.latest_last_ts_kst} "
          f"/ 수집 lag={plan.latest_lag_seconds:.0f}s" if plan.latest_lag_seconds is not None
          else f"  latest bucket: {plan.latest_bucket_ts}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bithumb hourly append PLAN (ADR-035 D3, read-only — write/cron은 별 GO)"
    )
    parser.add_argument("--window-days", type=int, default=2,
                        help="catch-up re-roll window (기본 2일)")
    parser.add_argument("--as-of", default=None,
                        help="기준 시각 ISO (테스트/재현용, 기본 now KST)")
    args = parser.parse_args()

    if args.window_days <= 0:
        print(f"[CONFIG 실패] --window-days는 1 이상이어야 함 (got {args.window_days})")
        sys.exit(2)

    if args.as_of:
        now_kst = datetime.fromisoformat(args.as_of)
        if now_kst.tzinfo is not None:
            now_kst = now_kst.astimezone(_KST_TZ).replace(tzinfo=None)
    else:
        now_kst = datetime.now(_KST_TZ).replace(tzinfo=None)

    retention_days = config.SOURCE_HOURLY_RETENTION_DAYS
    print(f"모드: PLAN (read-only, write 0) — now(KST)={now_kst} / window={args.window_days}d "
          f"/ retention={retention_days}d")
    print()

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        plan = compute_append_plan(db, now_kst, args.window_days, retention_days)
    finally:
        db.close()

    print_plan(plan)
    print()
    print("[PLAN ONLY] 실제 upsert/prune은 write 경로(별 GO)에서. cron 연결도 별 GO.")


if __name__ == "__main__":
    main()
