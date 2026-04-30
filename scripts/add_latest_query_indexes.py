"""
broadcast latest 조회용 복합 인덱스 추가 마이그레이션

배경:
  - PR1 24h baseline에서 payload_build_ms p99 ~1초, max 2~3초 spike가 정각/45분
    패턴으로 반복됨을 확인.
  - 분해 계측(Phase 1.5) 결과 spike 주범이 bank_total_ms / investing_total_ms로 좁혀짐.
  - EXPLAIN ANALYZE 분석:
      * Investing 최신값 조회: Parallel Seq Scan + Sort (~59ms 평소, spike 시 700ms+)
      * 은행 최신값 조회: currency 필터 후 ~29k row scan + window function
        (평소 18ms, spike 시 900ms+)
  - 현재 운영 DB에는 latest 조회 패턴(WHERE currency=X ORDER BY timestamp DESC, id DESC)에
    맞는 복합 인덱스가 없음.

추가 인덱스:
  1. ix_investing_currency_ts_id
     ON investing_exchange_rates (currency, timestamp DESC, id DESC)
     -> select_a_latest_investing_rate_from_db 쿼리에서 Parallel Seq Scan + Sort 회피

  2. ix_bank_currency_bank_ts_id
     ON bank_exchange_rates (currency, bank, timestamp DESC, id DESC)
     -> select_latest_bank_rates_from_db 쿼리에서 row scan 회피.
        기존 ix_bank_currency_timestamp(bank, currency, timestamp)는 현재 쿼리 패턴과
        컬럼 순서가 어긋나 비효율. 즉시 제거하지 않고 새 인덱스 효과 측정 후 정리.

55cd671(2026-03-17) 인덱스 정리는 PK 중복 인덱스(ix_*_id) 제거였고 조회 성능과 무관.
이번 인덱스는 1초 broadcast 준비 + spike 완화 목적으로 새로 필요해진 것.

비용 추정 (운영 DB 크기 기준):
  - investing_exchange_rates: 424k rows / 25 MB table -> 새 인덱스 ~10-15 MB 예상
  - bank_exchange_rates:       94k rows / 8.8 MB table -> 새 인덱스 ~5-10 MB 예상
  - 합계 ~25 MB. RDS micro 1GB의 ~0.5%. 메모리 압박 무시 가능.

실행 방법:
  # 로컬
  python scripts/add_latest_query_indexes.py [--dry-run]

  # Docker (운영 EC2)
  docker compose run --rm fastapi python scripts/add_latest_query_indexes.py

주의:
  - PostgreSQL CONCURRENTLY는 트랜잭션 밖에서만 실행 가능 -> AUTOCOMMIT 모드 사용.
  - SQLite는 CONCURRENTLY 미지원 -> 일반 CREATE INDEX IF NOT EXISTS.
  - IF NOT EXISTS로 idempotent 보장 (재실행 안전).
"""

import argparse
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine


INDEXES = [
    {
        "name": "ix_investing_currency_ts_id",
        "table": "investing_exchange_rates",
        "columns_pg": "(currency, timestamp DESC, id DESC)",
        "columns_sqlite": "(currency, timestamp DESC, id DESC)",
    },
    {
        "name": "ix_bank_currency_bank_ts_id",
        "table": "bank_exchange_rates",
        "columns_pg": "(currency, bank, timestamp DESC, id DESC)",
        "columns_sqlite": "(currency, bank, timestamp DESC, id DESC)",
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="SQL만 출력하고 실행 안 함")
    args = parser.parse_args()

    is_postgres = "postgresql" in str(engine.url)
    db_kind = "PostgreSQL" if is_postgres else "SQLite"
    print(f"DB: {db_kind}")

    statements = []
    for idx in INDEXES:
        if is_postgres:
            sql = (
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx['name']} "
                f"ON {idx['table']} {idx['columns_pg']}"
            )
        else:
            sql = (
                f"CREATE INDEX IF NOT EXISTS {idx['name']} "
                f"ON {idx['table']} {idx['columns_sqlite']}"
            )
        statements.append((idx["name"], sql))

    if args.dry_run:
        print("[DRY-RUN] 실행하지 않음. 아래 SQL이 실제 실행될 예정:")
        for name, sql in statements:
            print(f"  -- {name}")
            print(f"  {sql};")
        return

    with engine.connect() as conn:
        if is_postgres:
            # CONCURRENTLY는 트랜잭션 밖에서만 가능
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")

        for name, sql in statements:
            try:
                conn.execute(text(sql))
                print(f"  Created (or exists): {name}")
            except Exception as e:
                print(f"  Skip: {name} ({e})")

    print("Done.")


if __name__ == "__main__":
    main()
