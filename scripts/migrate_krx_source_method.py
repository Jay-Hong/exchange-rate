#!/usr/bin/env python3
"""KRX source_method migration — `kis_daily_backfill` → `krx_openapi_daily` (Step 4B source 전환).

ADR-034 §11 / Step 4B KRX OpenAPI 전환: 기존 KIS daily backfill로 적재된 KRX 미국달러선물
source_daily_rates를, scoped range dry-run에서 KRX OpenAPI 값과 전수 동일
(PASS_WITH_TRANSITIONAL, COMPARE_HARD=0)임이 확인된 뒤 provenance만 전환한다.

**가격/OHLC/contract_code/metadata/captured_at 변경 0** — provenance `source_method` 문자열만 UPDATE.
KRX 재호출 없음 (DB-only). range dry-run이 이미 값 일치 검증 → migration은 relabel.

대상 (Codex spec):
  - source='krx', asset='usd-krw-futures', source_method='kis_daily_backfill'
  - scoped window [--start-date, --end-date] — range dry-run에서 검증된 구간만 (미검증 row 미touch)
  - 업데이트 컬럼: source_method만 / 전후 불변: 그 외 전 컬럼 (surgical)

Migration 계약 (migrate_bithumb_source_method 패턴 미러링):
  - dry-run default / --write / --allow-production-write (production guard — dialect, host redacted)
  - --expected-old-count N / --expected-new-count M (--write 필수, range dry-run 직전 pre-query)
  - transaction: FOR UPDATE → count 재확인 → snapshot → source_method만 UPDATE → surgical 불변 → post-verify
  - idempotent: window 내 old=0 AND new=N+M → SKIP / old=0 AND new≠N+M → stale ABORT

사용법:
  # dry-run (window 내 count 출력만, write X)
  python scripts/migrate_krx_source_method.py --start-date 2026-04-20 --end-date 2026-05-27
  # write (production migration)
  python scripts/migrate_krx_source_method.py --start-date 2026-04-20 --end-date 2026-05-27 \
    --write --expected-old-count 25 --expected-new-count 0 --allow-production-write
"""

# 표준 라이브러리
import argparse
import os
import sys
from datetime import date
from typing import Optional

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE = "krx"
ASSET = "usd-krw-futures"
OLD_METHOD = "kis_daily_backfill"
NEW_METHOD = "krx_openapi_daily"

# surgical 검증 대상 — source_method / id 제외 전 컬럼 (불변 확인)
_COMPARE_COLS = [
    "source", "asset", "date_kst", "rate", "high", "low", "close",
    "ohlc_quality", "close_basis", "contract_code", "basis_date",
    "published_at", "captured_at", "metadata_json",
]


def _date_arg(s: str) -> date:
    """argparse type validator: ISO YYYY-MM-DD."""
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"date format은 YYYY-MM-DD (입력: {s!r})")


def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """write 진입 전 production DB guard (backfill writer 패턴 재사용).

    dialect 검사 (sqlite 안전, 그 외 default reject). host redacted (보안 원칙).
    """
    from app.database import engine
    dialect_name = engine.url.get_dialect().name
    if dialect_name == "sqlite":
        return None
    if not allow_production:
        host = engine.url.host or "(unknown)"
        redacted = "***" if host and host != "(unknown)" else "(unknown)"
        return (
            f"non-SQLite DB detected (dialect={dialect_name} host={redacted}). "
            "--allow-production-write 명시 안 됨 — production write 차단."
        )
    return None


def _snapshot(row) -> dict:
    """source_method 외 전 컬럼 snapshot (surgical 비교용)."""
    return {c: getattr(row, c) for c in _COMPARE_COLS}


def run_migration(
    db, start_date: date, end_date: date, expected_old: int, expected_new: int
) -> tuple[str, str]:
    """transaction 내 migration 로직 (commit/rollback은 caller).

    scoped window [start_date, end_date] 내 source='krx'/asset='usd-krw-futures' 대상만 (window 밖 미touch).
    Returns: (status, detail)
      - "MIGRATE_OK": relabel 완료 (caller commit)
      - "SKIP": 이미 migrated (idempotent)
      - "ABORT": 검증 실패 (caller rollback)
    """
    if start_date > end_date:
        return "ABORT", f"start_date({start_date}) > end_date({end_date}) — 역전 range"
    # count 인자 방어 (fail-open 차단 — 음수 / 빈 대상 0/0 거부).
    if expected_old < 0 or expected_new < 0:
        return "ABORT", f"expected count 음수 거부 (old={expected_old}, new={expected_new})"
    if expected_old + expected_new <= 0:
        return "ABORT", "expected_old + expected_new <= 0 — 운영 migration은 대상 KRX row 필수"

    from app.models import SourceDailyRate

    def _base():
        return db.query(SourceDailyRate).filter(
            SourceDailyRate.source == SOURCE, SourceDailyRate.asset == ASSET,
            SourceDailyRate.date_kst >= start_date, SourceDailyRate.date_kst <= end_date,
        )

    # 1. old method row 잠금 (PostgreSQL FOR UPDATE; SQLite 미지원이라 skip)
    old_q = _base().filter(SourceDailyRate.source_method == OLD_METHOD)
    if db.bind.dialect.name == "postgresql":
        old_q = old_q.with_for_update()
    old_rows = old_q.all()

    # 2. lock 후 count 재계산
    old_count = len(old_rows)
    new_count = _base().filter(SourceDailyRate.source_method == NEW_METHOD).count()
    total_before = _base().count()
    expected_total_new = expected_old + expected_new

    # idempotent rerun
    if old_count == 0:
        if new_count == expected_total_new:
            return "SKIP", f"이미 migrated (window 내 old=0, new={new_count}=N+M={expected_total_new})"
        return "ABORT", (
            f"old=0이나 new={new_count} != N+M={expected_total_new} "
            "(expected count stale — pre-query 재실행 필요)"
        )

    # 3. expected count 검증 (window mismatch / drift 차단)
    if old_count != expected_old:
        return "ABORT", f"old_count={old_count} != --expected-old-count={expected_old}"
    if new_count != expected_new:
        return "ABORT", f"new_count={new_count} != --expected-new-count={expected_new}"

    # 4. snapshot (source_method 외)
    snaps = {r.id: _snapshot(r) for r in old_rows}

    # 5. window 내 old method만 UPDATE
    rowcount = (
        _base().filter(SourceDailyRate.source_method == OLD_METHOD)
        .update({SourceDailyRate.source_method: NEW_METHOD}, synchronize_session=False)
    )
    if rowcount != expected_old:
        return "ABORT", f"UPDATE rowcount={rowcount} != expected_old={expected_old}"
    db.expire_all()  # bulk update 후 fresh read 강제

    # 6. surgical 검증 — source_method 외 모든 컬럼 불변
    for rid, snap in snaps.items():
        r = db.get(SourceDailyRate, rid)
        if r is None:
            return "ABORT", f"id={rid} 사라짐 (post-update)"
        if r.source_method != NEW_METHOD:
            return "ABORT", f"id={rid} source_method != {NEW_METHOD}"
        for c in _COMPARE_COLS:
            if getattr(r, c) != snap[c]:
                return "ABORT", f"id={rid} col '{c}' 변경됨 (surgical 위반): {snap[c]!r} → {getattr(r, c)!r}"

    # 7. post-verify
    old_after = _base().filter(SourceDailyRate.source_method == OLD_METHOD).count()
    new_after = _base().filter(SourceDailyRate.source_method == NEW_METHOD).count()
    total_after = _base().count()
    if old_after != 0:
        return "ABORT", f"old_after={old_after} != 0"
    if new_after != expected_total_new:
        return "ABORT", f"new_after={new_after} != N+M={expected_total_new}"
    if total_after != total_before:
        return "ABORT", f"total_after={total_after} != total_before={total_before}"

    return "MIGRATE_OK", (
        f"relabel {expected_old} rows (old→new) window [{start_date}, {end_date}] / "
        f"new total={new_after} / total {total_after} 불변"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KRX source_method migration (kis_daily_backfill → krx_openapi_daily, scoped window)"
    )
    parser.add_argument("--start-date", type=_date_arg, required=True,
                        help="대상 window 시작 (YYYY-MM-DD, range dry-run 검증 구간)")
    parser.add_argument("--end-date", type=_date_arg, required=True,
                        help="대상 window 종료 (YYYY-MM-DD)")
    parser.add_argument("--write", action="store_true",
                        help="실 migration 수행 (default: dry-run count 출력만)")
    parser.add_argument("--expected-old-count", type=int, default=None,
                        help="[--write 필수] old(kis_daily_backfill) row 기대 수 (range dry-run pre-query)")
    parser.add_argument("--expected-new-count", type=int, default=None,
                        help="[--write 필수] new(krx_openapi_daily) row 기대 수")
    parser.add_argument("--allow-production-write", action="store_true",
                        help="non-SQLite DB write 허용 (default off)")
    args = parser.parse_args()

    if args.start_date > args.end_date:
        print(f"[CONFIG 실패] start_date({args.start_date}) > end_date({args.end_date})")
        sys.exit(1)

    from app.database import SessionLocal

    # dry-run: window 내 count 출력만
    if not args.write:
        from app.models import SourceDailyRate
        db = SessionLocal()
        try:
            def base():
                return db.query(SourceDailyRate).filter(
                    SourceDailyRate.source == SOURCE, SourceDailyRate.asset == ASSET,
                    SourceDailyRate.date_kst >= args.start_date,
                    SourceDailyRate.date_kst <= args.end_date)
            old_c = base().filter(SourceDailyRate.source_method == OLD_METHOD).count()
            new_c = base().filter(SourceDailyRate.source_method == NEW_METHOD).count()
            total = base().count()
            print(f"[DRY-RUN] KRX usd-krw-futures source_method count (window [{args.start_date}, {args.end_date}]):")
            print(f"  old ({OLD_METHOD}): {old_c}")
            print(f"  new ({NEW_METHOD}): {new_c}")
            print(f"  total: {total}")
            print()
            print(f"  → --write 시 --expected-old-count {old_c} --expected-new-count {new_c} 전달 권장")
            print("  (window 밖 row는 미touch. write 직전 본 dry-run 재실행 권장)")
        finally:
            db.close()
        return

    # write: 필수 인자 + guard
    if args.expected_old_count is None or args.expected_new_count is None:
        print("[CONFIG 실패] --write 시 --expected-old-count / --expected-new-count 필수")
        sys.exit(1)
    if args.expected_old_count < 0 or args.expected_new_count < 0:
        print("[CONFIG 실패] expected count는 음수 불가")
        sys.exit(1)
    if args.expected_old_count + args.expected_new_count <= 0:
        print("[CONFIG 실패] expected_old + expected_new > 0 필요 (대상 KRX row 필수)")
        sys.exit(1)
    guard_err = check_production_write_guard(args.allow_production_write)
    if guard_err:
        print(f"[PRODUCTION 가드] {guard_err}")
        sys.exit(1)

    print(
        f"모드: WRITE (migration) / window [{args.start_date}, {args.end_date}] / "
        f"expected old={args.expected_old_count} new={args.expected_new_count}"
    )
    db = SessionLocal()
    try:
        status, detail = run_migration(
            db, args.start_date, args.end_date, args.expected_old_count, args.expected_new_count
        )
        if status == "MIGRATE_OK":
            db.commit()
            print(f"[MIGRATE 완료] {detail}")
        elif status == "SKIP":
            db.rollback()
            print(f"[SKIP] {detail}")
        else:  # ABORT
            db.rollback()
            print(f"[ABORT] {detail} (rollback)")
            sys.exit(1)
    except Exception as e:
        db.rollback()
        print(f"[ABORT] exception: {type(e).__name__}: {e} (rollback)")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
