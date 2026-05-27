#!/usr/bin/env python3
"""
source_daily_rates 테이블 생성 마이그레이션 (Phase 2d Step 1).

배경:
  - v2 장기 그래프 (3m/1y) hot path가 읽을 canonical daily table.
  - ADR-034 §3 schema (Numeric(14,6) + unique (source, asset, date_kst) +
    close_basis/source_method/ohlc_quality top-level columns).
  - app/models.py의 SourceDailyRate ORM class 정의 기준.

main.py의 Base.metadata.create_all(bind=engine)도 동일 효과를 가지지만,
본 script는 명시적 적용/검증/audit log 용도:
  - Idempotent (checkfirst=True — 이미 있으면 skip)
  - 운영 진입 시점 명시화 (코드 배포 ≠ schema migration 분리 가능)
  - --dry-run 모드 (DB 연결 없이 DDL 출력만)

사용법:
  python scripts/migrate_source_daily_rates.py [--dry-run]

주의:
  - 운영 DB에서 실행 전 반드시 백업 (`scripts/backup-db.sh`).
  - 신규 빈 테이블 추가라 lock 거의 없음, ALTER 없음.
  - 이미 source_daily_rates 테이블이 있으면 안전하게 skip.
  - --dry-run은 DB 연결 없이 DDL만 출력 (PostgreSQL dialect 기본).
"""

# 표준 라이브러리
import argparse
import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_dry_run_ddl(dialect_name: str = "postgresql") -> str:
    """DB 연결 없이 dialect별 CREATE TABLE + CREATE INDEX DDL 생성 (--dry-run 용도).

    PostgreSQL/SQLite dialect 명시적 인자 (engine 의존 회피).
    """
    # 함수 내부 import — DB 연결 회피 (--dry-run 시 engine import도 회피)
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models import SourceDailyRate

    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql
        dialect = postgresql.dialect()
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects import sqlite
        dialect = sqlite.dialect()
    else:
        raise ValueError(f"지원하지 않는 dialect: {dialect_name}")

    table = SourceDailyRate.__table__
    parts = [str(CreateTable(table).compile(dialect=dialect))]

    # __table_args__의 Index도 함께 출력 (production __table__.create()가 indexes도 자동 생성)
    for index in table.indexes:
        parts.append(str(CreateIndex(index).compile(dialect=dialect)))

    return "\n".join(parts)


def apply_live() -> None:
    """실제 DB에 테이블 생성 (DB 연결 + create_all checkfirst pattern)."""
    # 함수 내부 import — --dry-run에서는 import 자체도 회피
    from sqlalchemy import inspect

    from app.database import engine
    from app.models import SourceDailyRate

    inspector = inspect(engine)
    table_name = SourceDailyRate.__tablename__

    db_url = str(engine.url)
    db_type = "postgresql" if "postgresql" in db_url else "sqlite"
    print(f"DB 타입: {db_type}")
    print()

    if table_name in inspector.get_table_names():
        print(f"[SKIP] '{table_name}' 테이블이 이미 존재함")
        columns = [c["name"] for c in inspector.get_columns(table_name)]
        indexes = [idx["name"] for idx in inspector.get_indexes(table_name)]
        print(f"  Columns ({len(columns)}): {columns}")
        print(f"  Indexes: {indexes}")
        return

    print(f"[CREATE] '{table_name}' 테이블 생성...")
    print(f"  Schema: SourceDailyRate (ADR-034 §3, app/models.py)")

    # idempotent create (checkfirst=True — 이미 있으면 skip)
    SourceDailyRate.__table__.create(bind=engine, checkfirst=True)
    print(f"  [OK] '{table_name}' 테이블 생성 완료")

    # 검증
    inspector_after = inspect(engine)
    columns = [c["name"] for c in inspector_after.get_columns(table_name)]
    indexes = [idx["name"] for idx in inspector_after.get_indexes(table_name)]
    print(f"  Columns ({len(columns)}): {columns}")
    print(f"  Indexes: {indexes}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="source_daily_rates 테이블 생성 마이그레이션 (ADR-034 Phase 2d Step 1)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 연결 없이 DDL만 출력 (PostgreSQL dialect 기본)",
    )
    parser.add_argument(
        "--dialect",
        choices=["postgresql", "sqlite"],
        default="postgresql",
        help="--dry-run 시 DDL 생성 dialect (기본: postgresql)",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(f"모드: DRY-RUN ({args.dialect} dialect, DB 연결 없음)")
        print()
        ddl = get_dry_run_ddl(args.dialect)
        print(ddl)
        print()
        print("[DRY-RUN] 실제 실행 안 함. --dry-run 없이 다시 실행하면 적용됨.")
    else:
        print("모드: LIVE (DB 연결 + create_all checkfirst)")
        apply_live()

    print("\n완료!")


if __name__ == "__main__":
    main()
