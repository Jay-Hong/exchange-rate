#!/usr/bin/env python3
"""
market_index_rates 테이블에 granularity 컬럼 추가 마이그레이션

배경:
  - DXY 백필 데이터(일봉/시간봉)와 실시간 크롤링 데이터를 구분하기 위해
    granularity 컬럼이 필요 ('realtime' | 'hourly' | 'daily')
  - SQLAlchemy create_all()은 기존 테이블에 컬럼/인덱스를 추가하지 않음

변경 내용:
  1. granularity 컬럼 추가 (기본값 'realtime', NOT NULL)
  2. 기존 row에 'realtime' 기본값 설정
  3. 기존 UNIQUE 인덱스 삭제 후 granularity 포함 재생성
  4. granularity 조회용 인덱스 추가

사용법:
  python scripts/migrate_market_index_granularity.py [--dry-run]

주의:
  - 운영 DB에서 실행 전 반드시 백업
  - 이미 granularity 컬럼이 있으면 안전하게 스킵
"""

# 표준 라이브러리
import argparse
import sys
import os

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 서드파티 라이브러리
from sqlalchemy import text, inspect

# 로컬 애플리케이션
from app.database import engine


def get_db_type() -> str:
    """PostgreSQL / SQLite 판별"""
    url = str(engine.url)
    if "postgresql" in url:
        return "postgresql"
    return "sqlite"


def column_exists(conn, table: str, column: str) -> bool:
    """테이블에 특정 컬럼이 존재하는지 확인"""
    inspector = inspect(engine)
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def index_exists(conn, index_name: str) -> bool:
    """인덱스 존재 여부 확인"""
    db_type = get_db_type()
    if db_type == "postgresql":
        result = conn.execute(text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :name"
        ), {"name": index_name})
    else:
        result = conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name = :name"
        ), {"name": index_name})
    return result.fetchone() is not None


def migrate(dry_run: bool):
    db_type = get_db_type()
    print(f"DB 타입: {db_type}")
    print(f"모드: {'DRY-RUN' if dry_run else 'LIVE'}")
    print()

    with engine.begin() as conn:
        # Step 0: 테이블 존재 확인
        inspector = inspect(engine)
        if "market_index_rates" not in inspector.get_table_names():
            print("[SKIP] market_index_rates 테이블이 없음 (create_all로 생성됨)")
            return

        # Step 1: granularity 컬럼 존재 확인
        if column_exists(conn, "market_index_rates", "granularity"):
            print("[SKIP] granularity 컬럼이 이미 존재함")
            # 인덱스만 확인
        else:
            print("[ADD] granularity 컬럼 추가...")

            if db_type == "postgresql":
                sql = "ALTER TABLE market_index_rates ADD COLUMN granularity VARCHAR NOT NULL DEFAULT 'realtime'"
            else:
                sql = "ALTER TABLE market_index_rates ADD COLUMN granularity TEXT NOT NULL DEFAULT 'realtime'"

            print(f"  SQL: {sql}")
            if not dry_run:
                conn.execute(text(sql))

            # 기존 row 업데이트 (이미 DEFAULT로 설정되지만 명시적으로)
            update_sql = "UPDATE market_index_rates SET granularity = 'realtime' WHERE granularity IS NULL"
            print(f"  SQL: {update_sql}")
            if not dry_run:
                result = conn.execute(text(update_sql))
                print(f"  업데이트: {result.rowcount}행")

        # Step 2: UNIQUE 인덱스 교체 (3컬럼 → 4컬럼)
        # 기존: (instrument, source, timestamp)
        # 신규: (instrument, source, timestamp, granularity)
        uq_name = "uq_market_index"
        need_recreate = False

        if index_exists(conn, uq_name):
            # 인덱스 컬럼 수 확인으로 구/신 구분
            inspector_obj = inspect(engine)
            indexes = inspector_obj.get_indexes("market_index_rates")
            for idx in indexes:
                if idx["name"] == uq_name:
                    if "granularity" not in idx["column_names"]:
                        need_recreate = True
                    break

        if need_recreate:
            print(f"\n[DROP] 기존 UNIQUE 인덱스 '{uq_name}' 삭제 (granularity 미포함)...")
            sql = f"DROP INDEX {uq_name}" if db_type == "sqlite" else f"DROP INDEX IF EXISTS {uq_name}"
            print(f"  SQL: {sql}")
            if not dry_run:
                conn.execute(text(sql))

            print(f"\n[CREATE] 새 UNIQUE 인덱스 '{uq_name}' 생성 (granularity 포함)...")
            sql = (
                f"CREATE UNIQUE INDEX {uq_name} "
                "ON market_index_rates (instrument, source, timestamp, granularity)"
            )
            print(f"  SQL: {sql}")
            if not dry_run:
                conn.execute(text(sql))
        elif not index_exists(conn, uq_name):
            print(f"\n[CREATE] UNIQUE 인덱스 '{uq_name}' 생성...")
            sql = (
                f"CREATE UNIQUE INDEX {uq_name} "
                "ON market_index_rates (instrument, source, timestamp, granularity)"
            )
            print(f"  SQL: {sql}")
            if not dry_run:
                conn.execute(text(sql))
        else:
            print(f"\n[SKIP] '{uq_name}' 인덱스 이미 최신 (granularity 포함)")

        # Step 4: granularity 조회용 인덱스
        gran_idx = "ix_market_index_granularity"
        if not index_exists(conn, gran_idx):
            print(f"\n[CREATE] 인덱스 '{gran_idx}' 생성...")
            sql = (
                f"CREATE INDEX {gran_idx} "
                "ON market_index_rates (instrument, granularity, timestamp)"
            )
            print(f"  SQL: {sql}")
            if not dry_run:
                conn.execute(text(sql))
        else:
            print(f"\n[SKIP] '{gran_idx}' 인덱스 이미 존재함")

    print("\n완료!")


def main():
    parser = argparse.ArgumentParser(
        description="market_index_rates granularity 컬럼 마이그레이션"
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
