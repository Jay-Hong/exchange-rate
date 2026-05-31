#!/usr/bin/env python3
"""Bithumb source_method rename migration — `bithumb_candlestick_backfill` → `bithumb_candlestick_api`.

ADR-034 Amendment (Bithumb provenance 정정): Bithumb은 backfill·daily refresh 모두
공식 24h candlestick API를 사용하므로 source_method는 단일 값(`bithumb_candlestick_api`)이
정확. 기존 `bithumb_candlestick_backfill`("backfill" 함의)을 rename.

**가격/OHLC/metadata/captured_at 변경 0** — provenance `source_method` 문자열만 UPDATE.

Migration 계약 (Codex 협업 7 round):
- dry-run default / --write / --allow-production-write (강한 production guard — dialect 검사)
- `--expected-old-count N` / `--expected-new-count M` (--write 필수, Stage 3 직전 pre-query 결과)
- transaction:
  1. old method row FOR UPDATE 조회 (PostgreSQL — 기존 row 잠금)
  2. lock 후 old/new/total count 재계산 → expected와 비교
  3. old row snapshot (source_method 외 전 컬럼)
  4. old method만 direct UPDATE → rowcount == N 확인
  5. 동일 row id 기준 source_method 외 모든 컬럼 불변 확인 (surgical)
  6. old_after=0 / new_after=M+N / total_after=total_before
  7. 하나라도 불일치 → rollback
- idempotent rerun: old=0 AND new=M+N → SKIP / old=0 AND new≠M+N → stale ABORT
- **배포 순서**: 신코드(rename) 배포 후 (cron이 new method 기록) + cron 시각(15:01 UTC) 회피
  + daily_append one-shot 컨테이너 미실행 확인 (운영 race 절차 차단 — FOR UPDATE는 신규 insert 못 막음).

사용법:
  # dry-run (default) — 현재 count 출력만, write X
  python scripts/migrate_bithumb_source_method.py
  # write (production migration)
  python scripts/migrate_bithumb_source_method.py --write --expected-old-count N --expected-new-count M --allow-production-write
"""

# 표준 라이브러리
import argparse
import os
import sys
from typing import Optional

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE = "bithumb"
ASSET = "usdt-krw"
OLD_METHOD = "bithumb_candlestick_backfill"
NEW_METHOD = "bithumb_candlestick_api"

# surgical 검증 대상 — source_method / id 제외 전 컬럼 (불변 확인)
_COMPARE_COLS = [
    "source", "asset", "date_kst", "rate", "high", "low", "close",
    "ohlc_quality", "close_basis", "contract_code", "basis_date",
    "published_at", "captured_at", "metadata_json",
]


def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """write 진입 전 production DB guard (backfill writer 패턴 재사용 — 약한 기존 migrate 복제 금지).

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


def run_migration(db, expected_old: int, expected_new: int) -> tuple[str, str]:
    """transaction 내 migration 로직 (commit/rollback은 caller).

    Returns: (status, detail)
      - "MIGRATE_OK": rename 완료 (caller commit)
      - "SKIP": 이미 migrated (idempotent, caller rollback/commit 무관)
      - "ABORT": 검증 실패 (caller rollback)
    """
    # count 인자 방어 (fail-open 차단 — 음수 / 빈 대상 0/0 거부).
    # 운영 Bithumb migration은 대상 row가 반드시 존재 → N+M > 0 필수.
    if expected_old < 0 or expected_new < 0:
        return "ABORT", f"expected count 음수 거부 (old={expected_old}, new={expected_new})"
    if expected_old + expected_new <= 0:
        return "ABORT", "expected_old + expected_new <= 0 — 운영 migration은 대상 Bithumb row 필수"

    from app.models import SourceDailyRate

    def _base():
        return db.query(SourceDailyRate).filter(
            SourceDailyRate.source == SOURCE, SourceDailyRate.asset == ASSET,
        )

    # 1. old method row 잠금 (PostgreSQL FOR UPDATE; SQLite는 미지원이라 skip)
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
            return "SKIP", f"이미 migrated (old=0, new={new_count}=N+M={expected_total_new})"
        return "ABORT", (
            f"old=0이나 new={new_count} != N+M={expected_total_new} "
            "(expected count stale — Stage 3 pre-query 재실행 필요)"
        )

    # 3. expected count 검증
    if old_count != expected_old:
        return "ABORT", f"old_count={old_count} != --expected-old-count={expected_old}"
    if new_count != expected_new:
        return "ABORT", f"new_count={new_count} != --expected-new-count={expected_new}"

    # 4. snapshot (source_method 외)
    snaps = {r.id: _snapshot(r) for r in old_rows}

    # 5. old method만 UPDATE
    rowcount = (
        _base().filter(SourceDailyRate.source_method == OLD_METHOD)
        .update({SourceDailyRate.source_method: NEW_METHOD}, synchronize_session=False)
    )
    if rowcount != expected_old:
        return "ABORT", f"UPDATE rowcount={rowcount} != expected_old={expected_old}"
    db.expire_all()  # bulk update 후 session fresh read 강제

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

    return "MIGRATE_OK", f"rename {expected_old} rows (old→new) / new total={new_after} / total {total_after} 불변"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bithumb source_method rename migration (bithumb_candlestick_backfill → bithumb_candlestick_api)"
    )
    parser.add_argument("--write", action="store_true", help="실 migration 수행 (default: dry-run count 출력만)")
    parser.add_argument("--expected-old-count", type=int, default=None, help="[--write 필수] old method row 기대 수 (Stage 3 pre-query)")
    parser.add_argument("--expected-new-count", type=int, default=None, help="[--write 필수] new method row 기대 수 (Stage 3 pre-query)")
    parser.add_argument("--allow-production-write", action="store_true", help="non-SQLite DB write 허용 (default off)")
    args = parser.parse_args()

    from app.database import SessionLocal

    # dry-run: 현재 count 출력만
    if not args.write:
        from app.models import SourceDailyRate
        db = SessionLocal()
        try:
            old_c = db.query(SourceDailyRate).filter(
                SourceDailyRate.source == SOURCE, SourceDailyRate.asset == ASSET,
                SourceDailyRate.source_method == OLD_METHOD).count()
            new_c = db.query(SourceDailyRate).filter(
                SourceDailyRate.source == SOURCE, SourceDailyRate.asset == ASSET,
                SourceDailyRate.source_method == NEW_METHOD).count()
            total = db.query(SourceDailyRate).filter(
                SourceDailyRate.source == SOURCE, SourceDailyRate.asset == ASSET).count()
            print("[DRY-RUN] Bithumb usdt-krw source_method count:")
            print(f"  old ({OLD_METHOD}): {old_c}")
            print(f"  new ({NEW_METHOD}): {new_c}")
            print(f"  total: {total}")
            print()
            print(f"  → --write 시 --expected-old-count {old_c} --expected-new-count {new_c} 전달 권장")
            print("  (주의: cron 발화로 count 변동 가능 — Stage 3 실행 직전 본 dry-run 재실행)")
        finally:
            db.close()
        return

    # write: guard + 필수 인자 검증
    if args.expected_old_count is None or args.expected_new_count is None:
        print("[CONFIG 실패] --write 시 --expected-old-count / --expected-new-count 필수")
        sys.exit(1)
    # count fail-open 차단 (CLI 방어 — run_migration도 이중 방어)
    if args.expected_old_count < 0 or args.expected_new_count < 0:
        print("[CONFIG 실패] expected count는 음수 불가")
        sys.exit(1)
    if args.expected_old_count + args.expected_new_count <= 0:
        print("[CONFIG 실패] expected_old + expected_new > 0 필요 (대상 Bithumb row 필수)")
        sys.exit(1)
    guard_err = check_production_write_guard(args.allow_production_write)
    if guard_err:
        print(f"[PRODUCTION 가드] {guard_err}")
        sys.exit(1)

    print(f"모드: WRITE (migration) / expected old={args.expected_old_count} new={args.expected_new_count}")
    db = SessionLocal()
    try:
        status, detail = run_migration(db, args.expected_old_count, args.expected_new_count)
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
