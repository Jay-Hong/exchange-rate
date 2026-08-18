#!/usr/bin/env python3
"""Investing per-currency hourly append — PLAN + WRITE (ADR-035 D3 — going-forward freshness).

source_hourly_rates의 Investing(usd/jpy/eur)을 going-forward로 최신 유지하는 append.
**default PLAN(read-only)** — 통화별 upsert/prune 계획 산출 + 로그. **`--write` 시 통화별 독립 transaction upsert + retention prune.** cron 연결은 별도 GO.

설계 (Bithumb append와 동형, per-currency):
  - cadence: hourly (HH:05 권장 — correctness는 catch-up이 보장, minute은 freshness tuning)
  - correctness = 최근 window_days일 catch-up re-roll + idempotent upsert (늦은 tick 다음 실행에서 보정)
  - 직전 완료 hour까지만 — 현재 incomplete hour 제외
  - retention prune 계획: config.SOURCE_HOURLY_RETENTION_DAYS(14d) 밖 bucket (통화별)
  - **per-currency 독립** — 통화별 fetch/rollup/classification/prune

재사용 경계 (Codex 합의 — Bithumb append 불변):
  - **pure generic은 Bithumb append(`A`)에서 import**: previous_complete_hour / kst_naive_to_utc_naive /
    retention_cutoff_kst / AppendPlan (source 무관)
  - fetch/rollup은 Investing(`I`): fetch_investing_observations + rollup_to_hourly(quantize)
  - classification은 Investing-local (B.SOURCE/ASSET 하드코딩 회피, per-currency) — 동형 로직.
    Hana/KRX까지 붙어 중복 확인되면 그때 generic helper 추출 (지금은 운영 Bithumb append 불변 우선)

사용법:
  # plan (read-only, default):
  python scripts/hourly_append_investing_source_hourly_rates.py [--currency all] [--window-days 2] [--as-of 2026-06-08T14:30]
  # write (통화별 upsert+prune — production DB는 --allow-production-write):
  python scripts/hourly_append_investing_source_hourly_rates.py --currency all --write --allow-production-write

안전 제약:
  - `--as-of`는 PLAN 전용이다. write와 결합하면 과거 bucket 재생성 또는 미래 cutoff 기반
    대량 prune이 가능하므로 DB 접근 전에 fail-close한다.
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
import backfill_bithumb_source_hourly_rates as B  # noqa: E402  write_with_transaction(param화) + guard + ensure_table
import backfill_investing_source_hourly_rates as I  # noqa: E402  fetch + rollup + 상수 + resolve_currencies
import hourly_append_source_hourly_rates as A  # noqa: E402  pure generic helper (Bithumb append 불변 재사용)
import app.source_hourly_rates as H  # noqa: E402
from app import config  # noqa: E402
from app.models import SourceHourlyRate  # noqa: E402

_KST_TZ = timezone(timedelta(hours=9))


def fetch_candidates_investing(db, now_kst: datetime, window_days: int, asset: str):
    """직전 완료 hour까지 window_days일 investing_exchange_rates rollup → (window_start, prev_complete, candidates).

    window/fetch 경계는 Bithumb append와 동일(pure generic A 재사용). fetch/rollup만 Investing.
    """
    prev_complete = A.previous_complete_hour(now_kst)
    window_start = prev_complete - timedelta(hours=window_days * 24 - 1)
    fetch_start_utc = A.kst_naive_to_utc_naive(window_start)
    fetch_end_utc = A.kst_naive_to_utc_naive(prev_complete + timedelta(hours=1)) - timedelta(microseconds=1)
    obs = I.fetch_investing_observations(db, fetch_start_utc, fetch_end_utc, asset)
    return window_start, prev_complete, I.rollup_to_hourly(obs, asset)


def compute_append_plan_investing(
    db,
    asset: str,
    candidates: list,
    window_start: datetime,
    prev_complete: datetime,
    now_kst: datetime,
    retention_days: int,
) -> "A.AppendPlan":
    """Investing per-currency plan — classification(inserted/updated_same/changed/metadata) + prune count.

    candidate snapshot 기반 (write가 동일 snapshot 쓰도록 fetch와 분리 — double-fetch 불일치 회피).
    upsert가 metadata_json도 갱신하므로 price 동일 + metadata 변동도 no-op 아님(updated_metadata_changed).
    """
    existing = {e.bucket_ts_kst: e for e in
                H.get_range(db, I.SOURCE, asset, window_start, prev_complete)}
    inserted = updated_same = updated_metadata_changed = 0
    changed_buckets = []
    for c in candidates:
        e = existing.get(c["bucket_ts_kst"])
        if e is None:
            inserted += 1
        elif (float(e.close) != float(c["close"]) or float(e.high) != float(c["high"])
              or float(e.low) != float(c["low"])):
            changed_buckets.append(c["bucket_ts_kst"])           # price(close/high/low) 변동
        elif (e.metadata_json or {}) != (c.get("metadata_json") or {}):
            updated_metadata_changed += 1                         # price 동일 + metadata 변동 (late tick)
        else:
            updated_same += 1                                     # 완전 동일 (진짜 no-op)

    retention_cutoff = A.retention_cutoff_kst(now_kst, retention_days)
    prune_count = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == I.SOURCE,
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


def prune_old_buckets_investing(db, assets, cutoff: datetime) -> int:
    """retention 밖(bucket_ts_kst < cutoff) Investing(assets) bucket을 **단일 transaction atomic** 삭제.

    assets(통화 리스트) 전체를 한 delete+commit으로 — all-or-nothing이라 **통화별 부분 prune 불가**
    (중간 실패 시 일부만 prune되어 anchor로 복구 못하는 구멍 차단 — Codex finding). 실패 시 rollback + raise.
    Returns: 삭제 수.
    """
    try:
        deleted = (
            db.query(SourceHourlyRate)
            .filter(
                SourceHourlyRate.source == I.SOURCE,
                SourceHourlyRate.asset.in_(assets),
                SourceHourlyRate.bucket_ts_kst < cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted
    except Exception:
        db.rollback()
        raise


def print_plan_investing(cur: str, asset: str, plan: "A.AppendPlan") -> None:
    print(f"=== {cur} ({asset}) ===")
    print(f"  window (직전 완료 hour까지): {plan.window_start} ~ {plan.window_end}")
    print(f"  candidate bucket: {plan.candidate_count}")
    print(f"  upsert 계획: inserted={plan.inserted} / updated_same(no-op)={plan.updated_same} "
          f"/ updated_changed(price)={plan.updated_changed} "
          f"/ updated_metadata_changed(late-tick, price 무변)={plan.updated_metadata_changed}")
    if plan.changed_buckets:
        print(f"    changed 샘플: {[b.isoformat() for b in plan.changed_buckets]}")
    print(f"  prune 계획: retention_cutoff={plan.retention_cutoff} 미만 {plan.prune_count} bucket 삭제 대상")
    lag = f"{plan.latest_lag_seconds:.0f}s" if plan.latest_lag_seconds is not None else "n/a"
    print(f"  latest bucket: {plan.latest_bucket_ts} / last_tick={plan.latest_last_ts_kst} / 수집 lag={lag}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Investing per-currency hourly append (ADR-035 D3 — default PLAN/read-only, --write 시 통화별 upsert+prune. cron은 별 GO)"
    )
    parser.add_argument("--currency", default="all", help="usd|jpy|eur|all (기본 all)")
    parser.add_argument("--window-days", type=int, default=2, help="catch-up re-roll window (기본 2일)")
    parser.add_argument("--as-of", default=None, help="기준 시각 ISO (테스트/재현용, 기본 now KST)")
    parser.add_argument("--write", action="store_true",
                        help="plan 출력 후 통화별 upsert(require_empty 미사용) + retention prune (default: PLAN only)")
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

    try:
        currencies = I.resolve_currencies(args.currency)
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

    # write 진입 가드 (DB read 전)
    if args.write:
        guard_err = B.check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[GUARD 차단] {guard_err}")
            sys.exit(2)
        B.ensure_source_hourly_rates_table_created()

    mode_label = "WRITE (plan 후 통화별 upsert+prune)" if args.write else "PLAN (read-only, write 0)"
    print(f"모드: {mode_label} — Investing per-currency hourly append")
    print(f"통화: {currencies} / now(KST)={now_kst} / window={args.window_days}d / retention={retention_days}d")

    from app.database import SessionLocal
    # PLAN loop — 통화별 1회 fetch (plan 출력과 write가 동일 snapshot 사용 — double-fetch 불일치 회피)
    per_currency = {}   # asset → (candidates, window_start, prev_complete)
    for cur in currencies:
        asset = I._CURRENCY_MAP[cur]
        db = SessionLocal()
        try:
            window_start, prev_complete, candidates = fetch_candidates_investing(
                db, now_kst, args.window_days, asset)
            plan = compute_append_plan_investing(
                db, asset, candidates, window_start, prev_complete, now_kst, retention_days)
        finally:
            db.close()
        per_currency[asset] = (candidates, window_start, prev_complete)
        print()
        print_plan_investing(cur, asset, plan)

    print()
    if not args.write:
        print("[PLAN ONLY] 실제 upsert/prune은 --write / cron 연결도 별 GO.")
        return

    # rollback anchor (이미 commit된 통화 — 동일 작업 창 내 즉시 rollback 시에만)
    def _emit_rollback_anchor(committed):
        if not committed:
            return
        print("=== rollback anchor (이미 commit된 통화, 동일 작업 창 내 즉시 rollback 시에만) ===")
        print("  from datetime import datetime; from app.database import SessionLocal")
        print("  from app.source_hourly_rates import delete_range; db = SessionLocal()")
        for a, ws, pc in committed:
            print(f'  delete_range(db, "{I.SOURCE}", "{a}", '
                  f'datetime.fromisoformat("{ws.isoformat()}"), '
                  f'datetime.fromisoformat("{pc.isoformat()}"))')
        print("  주의: range delete 안전성 = 동일 작업 창 + 교집합 write 부재일 때만. 종료 후엔 snapshot 복구.")

    # WRITE: 통화별 독립 transaction (한 통화 실패가 다른 통화 rollback 안 함)
    print("=== WRITE — 통화별 upsert (require_empty 미사용 — append 갱신 허용) ===")
    committed = []   # (asset, window_start, prev_complete)
    for cur in currencies:
        asset = I._CURRENCY_MAP[cur]
        candidates, ws, pc = per_currency[asset]
        success, issues, outcome = B.write_with_transaction(
            candidates, require_empty=False,
            source=I.SOURCE, asset=asset, close_basis=I.CLOSE_BASIS,
            source_method=I.SOURCE_METHOD, ohlc_quality=I.OHLC_QUALITY)
        if not success:
            print(f"  [{cur} WRITE 실패] {len(issues)}건 issue — 이 통화 rollback")
            for issue in issues[:10]:
                print(f"    - {issue}")
            if committed:
                print(f"\n  ⚠️ 이미 commit된 통화 {[a for a, _, _ in committed]} — 아래 anchor로 복구 필요:")
                _emit_rollback_anchor(committed)
            sys.exit(1)
        committed.append((asset, ws, pc))
        print(f"  [{cur} UPSERT OK] inserted={outcome['inserted']} updated={outcome['updated']} (commit)")

    # PRUNE — 모든 write 성공 후 atomic retention cleanup (단일 transaction, all-or-nothing — 부분 prune 방지)
    cutoff = A.retention_cutoff_kst(now_kst, retention_days)
    prune_assets = [I._CURRENCY_MAP[cur] for cur in currencies]
    db_p = SessionLocal()
    try:
        pruned = prune_old_buckets_investing(db_p, prune_assets, cutoff)
    finally:
        db_p.close()
    print(f"  [PRUNE OK] cutoff={cutoff} 미만 {pruned} bucket 삭제 (atomic, {len(prune_assets)}통화)")

    print()
    _emit_rollback_anchor(committed)
    print()
    print("[복구 범위] append upsert는 위 delete_range anchor로 복구 가능(additive). "
          "retention prune은 destructive cleanup이라 anchor 아닌 snapshot/RDS backup 복구 영역.")
    print("[WRITE 완료] cron 연결은 별 GO.")


if __name__ == "__main__":
    main()
