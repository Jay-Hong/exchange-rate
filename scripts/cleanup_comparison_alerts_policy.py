#!/usr/bin/env python3
"""
비교알림 신정책 위반 row 정리 (ADR-037 Amendment 2026-07-04 — A1 one-time cleanup).

배경:
  - Amendment로 validation이 강화됨 (absolute=탭별 대칭 집합[테더는 거래소 5끼리] +
    threshold≥0 + canonical ordering / signed=테더 전용 김프[거래소×hana/kb/investing]).
  - 기존 생성 row(dev 계정 잔여 등) 중 신정책 위반 조합은 evaluator가 계속 평가하므로
    (evaluator에 정책 재검사 이중 로직을 두지 않는 대신) 배포 시점 1회 정리 (codex blocker).

판정: source_registry.validate_comparison_alert 재사용 (단일 진실 소스) +
      absolute의 비-canonical 순서도 위반으로 간주(재정렬 대신 delete — dev 잔여 전제).

사용법:
  python scripts/cleanup_comparison_alerts_policy.py            # dry-run (기본 — 위반 row 나열만)
  python scripts/cleanup_comparison_alerts_policy.py --write    # 실제 delete
"""

# 표준 라이브러리
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def find_violations(db):
    from app import models
    from app.source_registry import canonicalize_absolute_pair, validate_comparison_alert

    violations = []
    for row in db.query(models.ComparisonAlert).all():
        error = validate_comparison_alert(
            row.tab, row.left_source, row.left_asset,
            row.right_source, row.right_asset, row.diff_type, row.threshold)
        if error is None and row.diff_type == "absolute":
            canonical = canonicalize_absolute_pair(
                row.left_source, row.left_asset, row.right_source, row.right_asset)
            if canonical != (row.left_source, row.left_asset, row.right_source, row.right_asset):
                error = "absolute pair not in canonical order"
        if error is not None:
            violations.append((row, error))
    return violations


def main() -> None:
    parser = argparse.ArgumentParser(description="비교알림 신정책 위반 row 정리 (ADR-037 Amendment)")
    parser.add_argument("--write", action="store_true", help="실제 delete (기본은 dry-run)")
    args = parser.parse_args()

    from app.database import get_db_context

    with get_db_context() as db:
        violations = find_violations(db)
        total = len(violations)
        print(f"검사 대상 위반 row: {total}건 (mode={'WRITE' if args.write else 'DRY-RUN'})")
        for row, error in violations:
            print(f"  id={row.id} user={row.user_id[:8]}... tab={row.tab} "
                  f"{row.left_source}:{row.left_asset} vs {row.right_source}:{row.right_asset} "
                  f"{row.diff_type}/{row.operator}/{row.threshold} — {error}")
        if not args.write:
            print("[DRY-RUN] 삭제 안 함 — --write로 실행하면 위 row를 delete")
            return
        if total == 0:
            print("[OK] 위반 row 없음")
            return
        for row, _ in violations:
            db.delete(row)
        db.commit()
        print(f"[OK] {total}건 delete 완료")


if __name__ == "__main__":
    main()
