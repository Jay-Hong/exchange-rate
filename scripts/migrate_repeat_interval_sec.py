#!/usr/bin/env python3
"""
notification_settings + source_notification_settings 에 repeat_interval_sec 컬럼 추가
(ADR-036 B2 — 가격알림 반복 발송).

배경:
  - 가격알림을 "조건 만족 동안 사용자 설정 간격으로 반복 발송"으로 확장.
  - repeat_interval_sec: NULL=once-only(현행 1회성), 정수=초 간격(60/300/.../86400).
  - SQLAlchemy create_all()은 기존 테이블에 컬럼을 추가하지 않음 → ALTER 필요.

변경 내용:
  - notification_settings.repeat_interval_sec        INTEGER NULL  (bank, PR2에서 wiring)
  - source_notification_settings.repeat_interval_sec INTEGER NULL  (source, PR1에서 wiring)

기본값 정책:
  - 컬럼 기본 NULL → 기존 모든 row = once-only 보존 (behavior-change-0).
  - DEFAULT/UPDATE step 불필요 (NULL이 원하는 기본).

사용법:
  python scripts/migrate_repeat_interval_sec.py [--dry-run]

주의:
  - 운영 DB 실행 전 백업 권장.
  - 이미 컬럼이 있으면 안전하게 스킵 (멱등).
"""

# 표준 라이브러리
import argparse
import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 서드파티 라이브러리
from sqlalchemy import inspect, text

# 로컬 애플리케이션
from app.database import engine

_TABLES = ("notification_settings", "source_notification_settings")
_COLUMN = "repeat_interval_sec"


def get_db_type() -> str:
    """PostgreSQL / SQLite 판별."""
    return "postgresql" if "postgresql" in str(engine.url) else "sqlite"


def column_exists(table: str, column: str) -> bool:
    """테이블에 특정 컬럼이 존재하는지 확인."""
    inspector = inspect(engine)
    return column in [c["name"] for c in inspector.get_columns(table)]


def migrate(dry_run: bool):
    db_type = get_db_type()
    # INTEGER는 PostgreSQL/SQLite 공통. nullable이라 DEFAULT 불요(NULL=once).
    col_type = "INTEGER"
    print(f"DB 타입: {db_type}")
    print(f"모드: {'DRY-RUN' if dry_run else 'LIVE'}")
    print()

    with engine.begin() as conn:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        for table in _TABLES:
            if table not in existing_tables:
                print(f"[SKIP] {table} 테이블 없음 (create_all로 신규 생성됨)")
                continue

            if column_exists(table, _COLUMN):
                print(f"[SKIP] {table}.{_COLUMN} 이미 존재")
                continue

            sql = f"ALTER TABLE {table} ADD COLUMN {_COLUMN} {col_type} NULL"
            print(f"[ADD] {table}.{_COLUMN}")
            print(f"  SQL: {sql}")
            if not dry_run:
                conn.execute(text(sql))

    print("\n완료!")


def main():
    parser = argparse.ArgumentParser(
        description="repeat_interval_sec 컬럼 마이그레이션 (ADR-036 B2)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 DB에 변경하지 않고 SQL만 출력",
    )
    args = parser.parse_args()
    migrate(args.dry_run)


if __name__ == "__main__":
    main()
