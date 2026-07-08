#!/usr/bin/env python3
"""
user_entitlements 테이블 생성 마이그레이션 (ADR-038 G1).

배경:
  - KRX 달러선물 노출 게이트의 G1(운영자 수동 부여 entitlement) 저장소.
  - app/models.py의 UserEntitlement ORM class 정의 기준
    (user_id + key UNIQUE, key 예: 'krx_futures').

main.py의 create_all_app_tables도 동일 효과를 가지지만, 본 script는 명시적
적용/검증/audit log 용도 (migrate_comparison_alerts.py 패턴):
  - Idempotent (checkfirst=True — 이미 있으면 skip)
  - 운영 진입 시점 명시화 (코드 배포 ≠ schema migration 분리 가능)
  - --dry-run 모드 (DB 연결 없이 DDL 출력만)

사용법:
  python scripts/migrate_user_entitlements.py [--dry-run] [--dialect postgresql|sqlite]

주의:
  - 신규 빈 테이블 1개 추가라 lock 거의 없음, ALTER 없음.
  - entitlement 부여/회수는 scripts/grant_entitlement.py 사용.
"""

# 표준 라이브러리
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def dry_run_ddl(dialect_name: str) -> None:
    """DB 연결 없이 CREATE TABLE DDL 출력."""
    from sqlalchemy import create_mock_engine
    from sqlalchemy.schema import CreateTable

    from app.models import UserEntitlement

    url = {"postgresql": "postgresql://", "sqlite": "sqlite://"}[dialect_name]

    def _dump(sql, *args, **kwargs):
        print(str(sql.compile(dialect=engine.dialect)).strip() + ";")

    engine = create_mock_engine(url, _dump)
    print(f"-- dialect: {dialect_name}")
    print(str(CreateTable(UserEntitlement.__table__).compile(dialect=engine.dialect)).strip() + ";")


def apply() -> None:
    from app.database import engine
    from app.models import UserEntitlement

    exists_before = UserEntitlement.__table__.exists(bind=engine) \
        if hasattr(UserEntitlement.__table__, "exists") else None
    UserEntitlement.__table__.create(bind=engine, checkfirst=True)

    # 검증: 테이블 존재 확인
    from sqlalchemy import inspect
    insp = inspect(engine)
    ok = "user_entitlements" in insp.get_table_names()
    print(f"user_entitlements: {'OK (존재)' if ok else 'FAILED (미생성)'}"
          + ("" if exists_before is None else f" (before={exists_before})"))
    if not ok:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="user_entitlements 테이블 마이그레이션 (ADR-038 G1)")
    parser.add_argument("--dry-run", action="store_true", help="DDL 출력만 (DB 연결 없음)")
    parser.add_argument("--dialect", default="postgresql", choices=["postgresql", "sqlite"],
                        help="--dry-run에서 사용할 dialect")
    args = parser.parse_args()

    if args.dry_run:
        dry_run_ddl(args.dialect)
        return
    apply()


if __name__ == "__main__":
    main()
