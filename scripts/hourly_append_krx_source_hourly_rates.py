#!/usr/bin/env python3
"""KRX hourly append — PLAN(default) + WRITE (ADR-035 D3 — KRX 시간봉 재설계 2026-06-10).

source_hourly_rates(krx/usd-krw-futures)를 going-forward로 최신 유지하는 append.
**default PLAN(read-only)**, `--write` 시 upsert + retention prune. cron(:11 예정)은 별 GO.

재설계 (구 CF-only in-process close finalizer hook 대체 — 월물 제거 + CF/CM 통합 + 일봉 의존 절단):
  - **contract_code 미저장** (Bithumb/Investing/Hana와 동일 모델). 월물 정보가 필요하면
    read-side에서 daily row와 join (옵션, 현재 미구현 — v2 per-point contract 노출은
    graph_v2 has_contract 동적 판정으로 자연 소멸, caller 0).
  - **세션 필터 없음**: source_rates KRX tick 전체를 KST hour floor로 rollup —
    CF(08:30~15:45)·CM(17:50~06:00)·단일가 전부 자연 포함. 무거래 hour는 row 없음
    (실관측 canonical). CM 야간의 trading-date/contract 매핑 복잡도는 월물 제거로 소멸.
  - **bucket 월물 혼합 구조적 불가**: 무거래 구간이 06:00~08:30 / 15:45~17:50 두 곳이고
    rollover swap point(만기일 07:00 KST)가 전자 안 → 한 bucket에 두 월물 tick이 섞일 수
    없음 (정상 reconcile 전제 — reconcile 지연/실패 모드의 이론상 혼합은 기존 daily
    rollup도 동일한 기왕 리스크, 신규 리스크 아님).
  - **일봉 의존 절단**: 구 hook은 daily row(contract resolve) 의존이라 6/9처럼 daily가
    빠지면 hourly도 cascade 누락. 본 경로는 source_rates 직접 read — 독립 적재.
  - **#4 시너지**: WS-miss 날 REST gate-checked write(KRX_CLOSE_REST_WRITE_ENABLED=true
    활성화 후)가 15:45 close row를 source_rates에 쓰면, 2d idempotent re-roll이 다음
    cron에서 15:00 bucket을 자동 치유.
  - **예상 아티팩트**: close boundary row(CF 15:45:00 / CM 06:00:00 정각) — CM 쪽은
    06:00 bucket이 싱글톤(H=L=C, point_count=1)으로 생김. 정상 (이상치 아님).
  - **KRX 고유: empty-window graceful skip** — Bithumb(24/7)과 달리 KRX는 주말~월요일
    아침 cron 실행 시 window 안 tick이 0일 수 있음 (토 06:00 CM close 후 월 08:30 CF
    open까지 무거래). candidates 0이면 write를 fail-close하지 않고 skip(exit 0) + prune만
    수행 — 무세션 구간의 정상 동작.

PLAN에는 report-only daily-close 대조 진단 포함: window 안 KST date에 krx daily row가
있으면 15:00 bucket candidate close와 비교 (WS가 종가를 캡처한 날은 MATCH 기대 / 6/9형
blackout·복구일은 정당 DIVERGE — hard gate 금지, 관찰용 진단만. 상호 검증 합의).

사용법:
  # plan (read-only, default):
  python scripts/hourly_append_krx_source_hourly_rates.py [--window-days 2] [--as-of 2026-06-10T14:30]
  # 초기 재구축 (기존 KRX rows delete 후, retention 폭 14d):
  python scripts/hourly_append_krx_source_hourly_rates.py --write --window-days 14 --allow-production-write
  # going-forward (cron :11 예정):
  python scripts/hourly_append_krx_source_hourly_rates.py --write --allow-production-write

안전 제약:
  - `--as-of`는 PLAN 전용이다. 과거 시점은 오염 raw tick을 다시 candidate로 만들고,
    미래 시점은 retention cutoff를 앞당겨 정상 버킷을 대량 prune할 수 있어 fail-close한다.
  - 2026-08-14 08:00~11:00 KST 원천 tick은 만료 월물 A75608에서 수집된 값이다.
    이 guard가 포함된 버전에서는 어떤 window 옵션을 쓰더라도 이 네 candidate만
    명시적으로 제외하고 나머지 clean candidate와 retention prune은 계속 처리한다.
    이미 DB에 존재하는 오염 행은 자동 삭제하지 않고 repair 도구 실행을 경고한다.
"""

# 표준 라이브러리
import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# 프로젝트 루트 + scripts 디렉토리 sys.path (backfill 공유 writer 재사용)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _SCRIPT_DIR)

# 로컬 애플리케이션
import backfill_bithumb_source_hourly_rates as B  # noqa: E402  (공유 write_with_transaction)
import hourly_append_source_hourly_rates as A  # noqa: E402  (공유 시간 override guard)
import app.source_hourly_rates as H  # noqa: E402
from app import config  # noqa: E402
from app.krx_hourly_incidents import (  # noqa: E402
    KRX_HOURLY_INCIDENT_20260814_BUCKETS_KST,
)
from app.models import SourceHourlyRate, SourceRate  # noqa: E402
from app.source_hourly_rates import floor_bucket_ts_kst  # noqa: E402

SOURCE = "krx"
ASSET = "usd-krw-futures"
CLOSE_BASIS = "krx_observed_hourly"     # 기존 enum 유지 (session-agnostic — migration 0)
SOURCE_METHOD = "observed_rollup"
OHLC_QUALITY = "observed_rollup"

_KST_TZ = timezone(timedelta(hours=9))

# 2026-08-14 07:00 KST rollover가 휴장 보정 누락으로 지연되어, 아래 버킷의
# source_rates는 만료 월물 A75608 가격이다. 시간봉 행은 2026-08-17에 삭제했지만
# raw tick retention 동안 재집계하면 다시 생길 수 있어 write 후보에서 차단한다.
# 정상 retention이면 원본은 2026-09-14 전후 소멸한다. 상수는 사건 이력으로 영구
# 보존하며, 복구된 데이터셋이나 과거 데이터 재주입에서 이 raw tick을 **재집계하는
# 경로만** 차단한다. 기존 오염 행 정리는 repair_krx_hourly_rows.py의 명시적
# transaction만 담당한다.
KNOWN_CONTAMINATED_BUCKETS = frozenset(
    KRX_HOURLY_INCIDENT_20260814_BUCKETS_KST
)


# ─────────────────────────────────────────────────────────────
# 시간 / window (Bithumb append mirror)
# ─────────────────────────────────────────────────────────────

def _utc_naive_to_kst_iso(ts: datetime) -> str:
    """source_rates UTC naive datetime → KST isoformat."""
    return ts.replace(tzinfo=timezone.utc).astimezone(_KST_TZ).isoformat()


def kst_naive_to_utc_naive(dt_kst: datetime) -> datetime:
    """naive KST datetime → naive UTC datetime (source_rates 조회 경계용)."""
    return dt_kst.replace(tzinfo=_KST_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def previous_complete_hour(now_kst: datetime) -> datetime:
    """now 기준 직전 완료 hour bucket (naive KST). 현재 incomplete hour 제외."""
    current_hour = now_kst.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    return current_hour - timedelta(hours=1)


def retention_cutoff_kst(now_kst: datetime, retention_days: int) -> datetime:
    """retention prune 경계 (이 시각 미만 bucket 삭제 대상). now.floor(hour) - retention_days."""
    return now_kst.replace(minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(days=retention_days)


def known_contaminated_bucket_hits(candidates: list[dict]) -> list[datetime]:
    """후보 중 알려진 만료 월물 오염 버킷을 정렬해 반환한다."""
    return sorted({
        candidate["bucket_ts_kst"]
        for candidate in candidates
        if candidate.get("bucket_ts_kst") in KNOWN_CONTAMINATED_BUCKETS
    })


def exclude_known_contaminated_candidates(candidates: list[dict]) -> list[dict]:
    """알려진 오염 버킷만 제외하고 clean candidate 순서를 보존한다."""
    return [
        candidate
        for candidate in candidates
        if candidate.get("bucket_ts_kst") not in KNOWN_CONTAMINATED_BUCKETS
    ]


def existing_known_contaminated_buckets(db) -> list[datetime]:
    """DB에 이미 존재하는 알려진 오염 시간봉을 조회한다. 자동 삭제하지 않는다."""
    rows = (
        db.query(SourceHourlyRate.bucket_ts_kst)
        .filter(
            SourceHourlyRate.source == SOURCE,
            SourceHourlyRate.asset == ASSET,
            SourceHourlyRate.bucket_ts_kst.in_(
                KRX_HOURLY_INCIDENT_20260814_BUCKETS_KST
            ),
        )
        .order_by(SourceHourlyRate.bucket_ts_kst.asc())
        .all()
    )
    return [row.bucket_ts_kst for row in rows]


# ─────────────────────────────────────────────────────────────
# Fetch / Rollup (pure — 세션 필터 없음, contract 미저장)
# ─────────────────────────────────────────────────────────────

def fetch_krx_ticks(db, start_utc: datetime, end_utc: datetime) -> list[tuple]:
    """source_rates(krx/usd-krw-futures) [start_utc, end_utc] 조회 → (rate, ts) timestamp ASC."""
    rows = (
        db.query(SourceRate.rate, SourceRate.timestamp)
        .filter(
            SourceRate.source == SOURCE,
            SourceRate.asset == ASSET,
            SourceRate.timestamp >= start_utc,
            SourceRate.timestamp <= end_utc,
        )
        .order_by(SourceRate.timestamp.asc(), SourceRate.id.asc())
        .all()
    )
    return [(r.rate, r.timestamp) for r in rows]


def rollup_ticks_to_hourly(ticks: list[tuple]) -> list[dict]:
    """(rate, ts_utc_naive) timestamp-ASC ticks → KST 1h bucket candidate rows.

    세션 무관 — CF/CM/단일가 tick 전부 hour floor로 집계 (재설계 핵심).
    close = bucket 마지막 tick / high = max / low = min. rate == close invariant.
    metadata_json은 point_count + first/last ts(KST)만 — **session/contract 미포함**
    (session은 bucket_ts로 자명, contract는 미저장 정책).
    """
    buckets: dict = {}
    for rate, ts in ticks:
        bucket = floor_bucket_ts_kst(ts.replace(tzinfo=timezone.utc))
        b = buckets.get(bucket)
        if b is None:
            b = {"rates": [], "first_ts": ts, "last_ts": ts, "last_rate": rate}
            buckets[bucket] = b
        b["rates"].append(rate)
        b["last_ts"] = ts
        b["last_rate"] = rate

    rows = []
    for bucket in sorted(buckets):
        b = buckets[bucket]
        close = b["last_rate"]
        rows.append({
            "source": SOURCE,
            "asset": ASSET,
            "bucket_ts_kst": bucket,
            "rate": close,
            "close": close,
            "high": max(b["rates"]),
            "low": min(b["rates"]),
            "ohlc_quality": OHLC_QUALITY,
            "close_basis": CLOSE_BASIS,
            "source_method": SOURCE_METHOD,
            "metadata_json": {
                "point_count": len(b["rates"]),
                "first_ts_kst": _utc_naive_to_kst_iso(b["first_ts"]),
                "last_ts_kst": _utc_naive_to_kst_iso(b["last_ts"]),
            },
        })
    return rows


# ─────────────────────────────────────────────────────────────
# Plan (Bithumb append mirror + KRX 진단)
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppendPlan:
    window_start: datetime
    window_end: datetime
    candidate_count: int
    inserted: int
    updated_same: int
    updated_changed: int
    updated_metadata_changed: int
    changed_buckets: list
    prune_count: int
    retention_cutoff: datetime
    latest_bucket_ts: Optional[datetime]
    latest_last_ts_kst: Optional[str]
    latest_lag_seconds: Optional[float]
    daily_close_reports: list           # KRX 진단 (report-only)


def _fetch_candidates(db, now_kst: datetime, window_days: int):
    """직전 완료 hour까지 최근 window_days 일 source_rates rollup."""
    prev_complete = previous_complete_hour(now_kst)
    window_start = prev_complete - timedelta(hours=window_days * 24 - 1)
    fetch_start_utc = kst_naive_to_utc_naive(window_start)
    fetch_end_utc = kst_naive_to_utc_naive(prev_complete + timedelta(hours=1)) - timedelta(microseconds=1)
    ticks = fetch_krx_ticks(db, fetch_start_utc, fetch_end_utc)
    return window_start, prev_complete, rollup_ticks_to_hourly(ticks)


def daily_close_diagnostic(db, candidates: list) -> list[str]:
    """report-only 진단 — 15:00 bucket candidate close vs 같은 KST date의 krx daily close.

    WS 정상일: 15:00 bucket 마지막 tick(15:45 close boundary row) == daily CF close →
    MATCH 기대. 6/9형 blackout(관측 마지막 tick ≠ 공식 종가) / openapi 복구일은 정당
    DIVERGE — **hard gate 금지** (상호 검증 합의), 관찰용 출력만.
    """
    from app.models import SourceDailyRate
    reports = []
    for c in candidates:
        b = c["bucket_ts_kst"]
        if b.hour != 15:
            continue
        daily = (
            db.query(SourceDailyRate)
            .filter(
                SourceDailyRate.source == SOURCE,
                SourceDailyRate.asset == ASSET,
                SourceDailyRate.date_kst == b.date(),
            )
            .first()
        )
        if daily is None:
            continue
        d_close = float(daily.close)
        c_close = float(c["close"])
        tag = "MATCH" if d_close == c_close else "DIVERGE (blackout/복구일이면 정당)"
        reports.append(
            f"{b.date().isoformat()} 15:00 bucket close={c_close} vs daily close={d_close} → {tag}"
        )
    return reports


def prune_old_buckets(db, cutoff: datetime) -> int:
    """retention 밖(bucket_ts_kst < cutoff) KRX bucket 삭제. Returns: 삭제 수. (자체 commit)"""
    deleted = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == SOURCE,
            SourceHourlyRate.asset == ASSET,
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
    """candidate snapshot으로부터 plan 계산 (plan/write 동일 snapshot — Bithumb mirror)."""
    existing = {e.bucket_ts_kst: e for e in
                H.get_range(db, SOURCE, ASSET, window_start, prev_complete)}
    inserted = updated_same = updated_metadata_changed = 0
    changed_buckets = []
    for c in candidates:
        e = existing.get(c["bucket_ts_kst"])
        if e is None:
            inserted += 1
        elif (float(e.close) != c["close"] or float(e.high) != c["high"]
              or float(e.low) != c["low"]):
            changed_buckets.append(c["bucket_ts_kst"])
        elif (e.metadata_json or {}) != (c.get("metadata_json") or {}):
            updated_metadata_changed += 1
        else:
            updated_same += 1

    retention_cutoff = retention_cutoff_kst(now_kst, retention_days)
    prune_count = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == SOURCE,
            SourceHourlyRate.asset == ASSET,
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
        daily_close_reports=daily_close_diagnostic(db, candidates),
    )


def compute_append_plan(db, now_kst: datetime, window_days: int, retention_days: int) -> AppendPlan:
    """편의 wrapper — fetch 후 compute_append_plan_from_candidates (PLAN-only / 테스트용)."""
    window_start, prev_complete, candidates = _fetch_candidates(db, now_kst, window_days)
    return compute_append_plan_from_candidates(
        db, candidates, window_start, prev_complete, now_kst, retention_days)


def print_plan(plan: AppendPlan) -> None:
    print("=== KRX hourly append PLAN ===")
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
    if plan.daily_close_reports:
        print("  daily-close 대조 (report-only — DIVERGE는 blackout/복구일이면 정당):")
        for line in plan.daily_close_reports:
            print(f"    - {line}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KRX hourly append (재설계 2026-06-10 — default PLAN/read-only, "
                    "--write 시 upsert+prune. cron(:11 예정)은 별 GO)"
    )
    parser.add_argument("--window-days", type=int, default=2,
                        help="catch-up re-roll window (기본 2일, 초기 재구축은 14)")
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

    time_override_error = A.validate_write_time_override(args.write, args.as_of)
    if time_override_error:
        print(f"[GUARD 차단] {time_override_error}")
        sys.exit(2)

    if args.as_of:
        now_kst = datetime.fromisoformat(args.as_of)
        if now_kst.tzinfo is not None:
            now_kst = now_kst.astimezone(_KST_TZ).replace(tzinfo=None)
    else:
        now_kst = datetime.now(_KST_TZ).replace(tzinfo=None)

    retention_days = config.SOURCE_HOURLY_RETENTION_DAYS

    # write 진입 가드 (DB read 전) — Bithumb 공유 guard 재사용
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
    contaminated: list[datetime] = []
    try:
        window_start, prev_complete, candidates = _fetch_candidates(db, now_kst, args.window_days)
        contaminated = known_contaminated_bucket_hits(candidates)
        if contaminated:
            rendered = ", ".join(bucket.isoformat() for bucket in contaminated)
            label = "GUARD SKIP" if args.write else "GUARD PREVIEW"
            print(
                f"[{label}] 만료 월물 A75608에서 수집된 2026-08-14 오염 버킷을 "
                f"write 후보에서 제외: {rendered}"
            )
            candidates = exclude_known_contaminated_candidates(candidates)

        existing_contaminated = existing_known_contaminated_buckets(db)
        if existing_contaminated:
            rendered = ", ".join(bucket.isoformat() for bucket in existing_contaminated)
            print(
                "[GUARD WARNING] DB에 기존 오염 시간봉이 남아 있음. 자동 삭제하지 않음; "
                f"repair_krx_hourly_rows.py로 검토 필요: {rendered}"
            )

        plan = compute_append_plan_from_candidates(
            db, candidates, window_start, prev_complete, now_kst, retention_days)
        print_plan(plan)
        print()
        if not args.write:
            print("[PLAN ONLY] 실제 upsert/prune은 --write에서. cron 연결도 별 GO.")
            return
    finally:
        db.close()

    # KRX 고유: empty-window graceful skip — 주말~월요일 아침 등 무세션 구간은
    # candidates 0이 정상. write fail-close(exit 1) 대신 skip(exit 0) + prune만 수행
    # (cron이 매시간 도는 환경에서 무세션 hour를 오류로 만들지 않음).
    if not candidates:
        if contaminated:
            print("[WRITE SKIP] 오염 버킷 제외 후 clean candidate 0 — upsert 없이 prune 계속.")
        else:
            print("[WRITE SKIP] candidate 0 bucket — 무세션 구간 (주말/휴장/세션 갭) 정상 skip.")
    else:
        print(f"=== WRITE — upsert {len(candidates)} candidate (require_empty 미사용 — append 갱신 허용) ===")
        success, write_issues, outcome = B.write_with_transaction(
            candidates, require_empty=False,
            source=SOURCE, asset=ASSET, close_basis=CLOSE_BASIS,
            source_method=SOURCE_METHOD, ohlc_quality=OHLC_QUALITY,
            # allow_contract_code default False — contract_code None을 post-write에서
            # 강제 (재설계 계약 그 자체). post_write_validator 불필요.
        )
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
