#!/usr/bin/env python3
"""
comparison_alerts + comparison_notification_logs 테이블 생성 마이그레이션 (ADR-037 S1).

배경:
  - 비교 알림 (두 소스 간 가격 차이 spread = left − right) 설정 + 발송 히스토리.
  - ADR-037 Decision 2 schema (universal 4컬럼 + tab 명시 저장 + repeat_interval_sec[B2] +
    logs의 left_rate/right_rate/spread/observed_at/is_repeat 스냅샷).
  - app/models.py의 ComparisonAlert / ComparisonNotificationLog ORM class 정의 기준.

main.py의 Base.metadata.create_all(bind=engine)도 동일 효과를 가지지만,
본 script는 명시적 적용/검증/audit log 용도 (migrate_source_daily_rates.py 패턴):
  - Idempotent (checkfirst=True — 이미 있으면 skip, 테이블별 독립)
  - 운영 진입 시점 명시화 (코드 배포 ≠ schema migration 분리 가능)
  - --dry-run 모드 (DB 연결 없이 DDL 출력만)

사용법:
  python scripts/migrate_comparison_alerts.py [--dry-run] [--dialect postgresql|sqlite]

주의:
  - 운영 DB에서 실행 전 반드시 백업 (`scripts/backup-db.sh`).
  - 신규 빈 테이블 2개 추가라 lock 거의 없음, ALTER 없음.
  - 이미 존재하는 테이블은 안전하게 skip (테이블별 독립 판정).
  - S1은 behavior-change-0: 이 테이블을 읽고 쓰는 코드는 S2/S3에서 land.
"""

# 표준 라이브러리
import argparse
import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_dry_run_ddl(dialect_name: str = "postgresql") -> str:
    """DB 연결 없이 dialect별 CREATE TABLE + CREATE INDEX DDL 생성 (--dry-run 용도)."""
    # 함수 내부 import — DB 연결 회피 (--dry-run 시 engine import도 회피)
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models import ComparisonAlert, ComparisonNotificationLog

    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql
        dialect = postgresql.dialect()
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects import sqlite
        dialect = sqlite.dialect()
    else:
        raise ValueError(f"지원하지 않는 dialect: {dialect_name}")

    parts = []
    for model in (ComparisonAlert, ComparisonNotificationLog):
        table = model.__table__
        parts.append(str(CreateTable(table).compile(dialect=dialect)))
        # __table_args__의 Index도 함께 출력 (production __table__.create()가 indexes도 자동 생성)
        for index in table.indexes:
            parts.append(str(CreateIndex(index).compile(dialect=dialect)))
    return "\n".join(parts)


def apply_live() -> None:
    """실제 DB에 테이블 생성 (DB 연결 + create checkfirst pattern, 테이블별 독립)."""
    # 함수 내부 import — --dry-run에서는 import 자체도 회피
    from sqlalchemy import inspect

    from app.database import engine
    from app.models import ComparisonAlert, ComparisonNotificationLog

    db_url = str(engine.url)
    db_type = "postgresql" if "postgresql" in db_url else "sqlite"
    print(f"DB 타입: {db_type}")
    print()

    for model in (ComparisonAlert, ComparisonNotificationLog):
        inspector = inspect(engine)
        table_name = model.__tablename__

        if table_name in inspector.get_table_names():
            print(f"[SKIP] '{table_name}' 테이블이 이미 존재함")
            columns = [c["name"] for c in inspector.get_columns(table_name)]
            indexes = [idx["name"] for idx in inspector.get_indexes(table_name)]
            print(f"  Columns ({len(columns)}): {columns}")
            print(f"  Indexes: {indexes}")
            continue

        print(f"[CREATE] '{table_name}' 테이블 생성...")
        print(f"  Schema: {model.__name__} (ADR-037 Decision 2, app/models.py)")

        # idempotent create (checkfirst=True — 이미 있으면 skip)
        model.__table__.create(bind=engine, checkfirst=True)
        print(f"  [OK] '{table_name}' 테이블 생성 완료")

        # 검증
        inspector_after = inspect(engine)
        columns = [c["name"] for c in inspector_after.get_columns(table_name)]
        indexes = [idx["name"] for idx in inspector_after.get_indexes(table_name)]
        print(f"  Columns ({len(columns)}): {columns}")
        print(f"  Indexes: {indexes}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="comparison_alerts + comparison_notification_logs 테이블 생성 (ADR-037 S1)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 연결 없이 DDL만 출력",
    )
    parser.add_argument(
        "--dialect",
        choices=["postgresql", "sqlite"],
        default="postgresql",
        help="--dry-run DDL dialect (기본 postgresql)",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(f"=== DRY RUN — DDL ({args.dialect}) ===")
        print(get_dry_run_ddl(args.dialect))
        return

    apply_live()


if __name__ == "__main__":
    main()
