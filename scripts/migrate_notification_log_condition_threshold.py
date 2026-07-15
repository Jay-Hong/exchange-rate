#!/usr/bin/env python3
"""
notification_logs 에 condition / threshold 컬럼 추가 (FX 알림 히스토리 완전판, 2026-07-15).

배경:
  - FX 은행 가격알림 발송 히스토리를 사용자에게 노출(/api/notification-logs + iOS sheet).
  - source/comparison log는 condition/threshold를 inline 저장해 "above 1475 도달" 상세를
    보여주지만, notification_logs는 bank/currency/rate만 저장 → 발화 조건 표시 불가.
  - 발화 시점 condition/threshold를 inline 스냅샷(설정 삭제 후에도 안전) → source 수준 상세.
  - SQLAlchemy create_all()은 기존 테이블에 컬럼을 추가하지 않음 → ALTER 필요.

변경 내용:
  - notification_logs.condition  VARCHAR NULL  ('above' | 'below')
  - notification_logs.threshold  DOUBLE/FLOAT NULL

기본값 정책:
  - 컬럼 기본 NULL → 보강 이전 old row 하위호환(behavior-change-0). 신규 발송부터 채워짐.
  - 응답 스키마도 Optional — old row는 condition/threshold 미표시.

사용법:
  python scripts/migrate_notification_log_condition_threshold.py [--dry-run]

주의:
  - 운영 DB 실행 전 백업 권장.
  - 이미 컬럼이 있으면 안전하게 스킵 (멱등).
  - 코드 배포 순서: 마이그레이션 먼저 → 그 다음 신 코드(create_notification_log가 신 컬럼 write).
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

_TABLE = "notification_logs"
# (컬럼명, PostgreSQL 타입, SQLite 타입)
_COLUMNS = (
    ("condition", "VARCHAR", "VARCHAR"),
    ("threshold", "DOUBLE PRECISION", "FLOAT"),
)


def get_db_type() -> str:
    return "postgresql" if "postgresql" in str(engine.url) else "sqlite"


def column_exists(table: str, column: str) -> bool:
    inspector = inspect(engine)
    return column in [c["name"] for c in inspector.get_columns(table)]


def migrate(dry_run: bool):
    db_type = get_db_type()
    print(f"DB 타입: {db_type}")
    print(f"모드: {'DRY-RUN' if dry_run else 'LIVE'}")
    print()

    # ⚠️ 컬럼별 개별 트랜잭션 (self-block 방지, 2026-07-16 사고 교훈):
    # 하나의 engine.begin() 안에서 첫 ALTER(ACCESS EXCLUSIVE lock)를 잡은 뒤 다음 컬럼의
    # column_exists()가 inspect(engine)로 **별도 connection**을 열어 같은 테이블 카탈로그를 읽으면
    # 자기 lock을 기다려 application self-block(PG deadlock 감지 안 됨 — idle-in-transaction).
    # → 각 ALTER를 자체 트랜잭션으로 즉시 commit해 lock을 먼저 해제, 다음 column_exists는 lock 없이 실행.
    if _TABLE not in set(inspect(engine).get_table_names()):
        print(f"[SKIP] {_TABLE} 테이블 없음 (create_all로 신규 생성됨)")
        print("\n완료!")
        return

    for column, pg_type, sqlite_type in _COLUMNS:
        if column_exists(_TABLE, column):   # lock 미보유 상태에서 검사 (직전 ALTER 이미 commit)
            print(f"[SKIP] {_TABLE}.{column} 이미 존재")
            continue
        col_type = pg_type if db_type == "postgresql" else sqlite_type
        sql = f"ALTER TABLE {_TABLE} ADD COLUMN {column} {col_type} NULL"
        print(f"[ADD] {_TABLE}.{column}")
        print(f"  SQL: {sql}")
        if not dry_run:
            with engine.begin() as conn:    # 컬럼별 트랜잭션 → 즉시 commit → lock 해제
                conn.execute(text(sql))

    print("\n완료!")


def main():
    parser = argparse.ArgumentParser(
        description="notification_logs condition/threshold 컬럼 마이그레이션 (FX 히스토리 완전판)"
    )
    parser.add_argument("--dry-run", action="store_true", help="실제 DB 변경 없이 SQL만 출력")
    args = parser.parse_args()
    migrate(args.dry_run)


if __name__ == "__main__":
    main()
