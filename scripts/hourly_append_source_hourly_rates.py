#!/usr/bin/env python3
"""Bithumb hourly append — PLAN(default) + WRITE (ADR-035 D3 — going-forward freshness).

source_hourly_rates를 going-forward로 최신 유지하는 append.
**default PLAN(read-only)** — 어떤 bucket을 upsert/prune할지 + 로그. **`--write` 시 실제 upsert + retention prune.** cron 연결은 별도 GO.

설계 (다른 Claude / Codex 수렴):
  - cadence: hourly (HH:05 권장 — correctness는 catch-up이 보장, minute은 freshness tuning)
  - **correctness = 최근 2일 catch-up re-roll + idempotent upsert** (늦게 들어온 tick을 다음 실행에서 보정)
  - **직전 완료 hour까지만** — 현재 incomplete hour는 제외 (그 hour는 다음 실행에서 완성 후 반영)
  - require_empty 미사용 (append는 갱신 허용)
  - retention prune: config.SOURCE_HOURLY_RETENTION_DAYS(14d) 밖 bucket 삭제 계획
  - 정상 현상: latest hour 값이 다음 실행에서 미세 revise 가능 (불완전→완전). 로그로 표면화.

default(PLAN)는 read-only: source_rates(rollup) + source_hourly_rates(기존 비교) 조회만, write 0.
--write 시 upsert(require_empty 미사용 — 갱신 허용) + prune.

사용법:
  # plan (read-only, default):
  python scripts/hourly_append_source_hourly_rates.py [--window-days 2] [--as-of 2026-06-08T14:30]
  # write (upsert+prune — production DB는 --allow-production-write):
  python scripts/hourly_append_source_hourly_rates.py --write --allow-production-write
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


def _fetch_candidates(db, now_kst: datetime, window_days: int):
    """직전 완료 hour까지 최근 window_days 일 source_rates rollup → (window_start, prev_complete, candidates).

    plan과 write가 공유 — window 계산 + fetch + rollup 단일 소스.
    window_start = 직전 완료 hour 기준 rolling window_days*24h 시작점 (00:00 고정 아님).
    fetch 상한 = prev_complete 59:59 → 현재 incomplete hour tick 자연 배제.
    """
    prev_complete = previous_complete_hour(now_kst)
    window_start = prev_complete - timedelta(hours=window_days * 24 - 1)
    fetch_start_utc = kst_naive_to_utc_naive(window_start)
    fetch_end_utc = kst_naive_to_utc_naive(prev_complete + timedelta(hours=1)) - timedelta(microseconds=1)
    ticks = B.fetch_bithumb_ticks(db, fetch_start_utc, fetch_end_utc)
    return window_start, prev_complete, B.rollup_ticks_to_hourly(ticks)


def retention_cutoff_kst(now_kst: datetime, retention_days: int) -> datetime:
    """retention prune 경계 (이 시각 미만 bucket 삭제 대상). now.floor(hour) - retention_days."""
    return now_kst.replace(minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(days=retention_days)


def prune_old_buckets(db, cutoff: datetime) -> int:
    """retention 밖(bucket_ts_kst < cutoff) Bithumb bucket 삭제. Returns: 삭제 수. (자체 commit)"""
    deleted = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == B.SOURCE,
            SourceHourlyRate.asset == B.ASSET,
            SourceHourlyRate.bucket_ts_kst < cutoff,
        )
        .delete()
    )
    db.commit()
    return deleted


def compute_append_plan_from_candidates(
    db,
    candidates: list,
    window_start: datetime,
    prev_complete: datetime,
    now_kst: datetime,
    retention_days: int,
) -> AppendPlan:
    """candidate snapshot으로부터 plan 계산.

    plan 출력과 write가 **동일 candidates snapshot**을 쓰도록 fetch와 분리
    (double-fetch 시 그 사이 late tick으로 plan/commit 불일치 회피).
    """
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
    retention_cutoff = retention_cutoff_kst(now_kst, retention_days)
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


def compute_append_plan(db, now_kst: datetime, window_days: int, retention_days: int) -> AppendPlan:
    """편의 wrapper — fetch 후 compute_append_plan_from_candidates (PLAN-only / 테스트용)."""
    window_start, prev_complete, candidates = _fetch_candidates(db, now_kst, window_days)
    return compute_append_plan_from_candidates(
        db, candidates, window_start, prev_complete, now_kst, retention_days)


def print_plan(plan: AppendPlan) -> None:
    print("=== Bithumb hourly append PLAN ===")
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
        description="Bithumb hourly append (ADR-035 D3 — default PLAN/read-only, --write 시 upsert+prune. cron은 별 GO)"
    )
    parser.add_argument("--window-days", type=int, default=2,
                        help="catch-up re-roll window (기본 2일)")
    parser.add_argument("--as-of", default=None,
                        help="기준 시각 ISO (테스트/재현용, 기본 now KST)")
    parser.add_argument("--write", action="store_true",
                        help="plan 출력 후 실제 upsert(require_empty 미사용) + retention prune")
    parser.add_argument("--allow-production-write", action="store_true",
                        help="[guard] non-SQLite DB(RDS 등)에 --write 허용")
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

    # write 진입 가드 (DB read 전)
    if args.write:
        guard_err = B.check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[GUARD 차단] {guard_err}")
            sys.exit(2)
        B.ensure_source_hourly_rates_table_created()

    mode_label = "WRITE (plan 후 upsert+prune)" if args.write else "PLAN (read-only, write 0)"
    print(f"모드: {mode_label} — now(KST)={now_kst} / window={args.window_days}d / retention={retention_days}d")
    print()

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        # candidate 1회 fetch → plan 출력과 write가 동일 snapshot 사용 (double-fetch 불일치 회피)
        window_start, prev_complete, candidates = _fetch_candidates(db, now_kst, args.window_days)
        plan = compute_append_plan_from_candidates(
            db, candidates, window_start, prev_complete, now_kst, retention_days)
        print_plan(plan)
        print()
        if not args.write:
            print("[PLAN ONLY] 실제 upsert/prune은 --write에서. cron 연결도 별 GO.")
            return
    finally:
        db.close()

    # WRITE: upsert (B.write_with_transaction — 자체 session + post-write 검증 a~i + commit/rollback)
    print(f"=== WRITE — upsert {len(candidates)} candidate (require_empty 미사용 — append 갱신 허용) ===")
    success, write_issues, outcome = B.write_with_transaction(candidates, require_empty=False)
    if not success:
        print(f"[WRITE 실패] {len(write_issues)}건 issue — transaction rollback")
        for issue in write_issues[:10]:
            print(f"  - {issue}")
        sys.exit(1)
    print(f"[UPSERT OK] inserted={outcome['inserted']} updated={outcome['updated']} (commit)")

    # retention prune (별 transaction — recent window와 disjoint)
    cutoff = retention_cutoff_kst(now_kst, retention_days)
    db_p = SessionLocal()
    try:
        pruned = prune_old_buckets(db_p, cutoff)
    finally:
        db_p.close()
    print(f"[PRUNE OK] cutoff={cutoff} 미만 {pruned} bucket 삭제 (commit)")
    print()
    print("[WRITE 완료] cron 연결은 별 GO.")


if __name__ == "__main__":
    main()
