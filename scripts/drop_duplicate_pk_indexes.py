"""
중복 PK 인덱스 제거 마이그레이션

models.py에서 primary_key=True, index=True로 선언되어
PK 인덱스와 별도 ix_*_id 인덱스가 동시에 존재하는 문제 해결.

실행 방법:
  # 로컬
  python scripts/drop_duplicate_pk_indexes.py

  # Docker
  docker compose run --rm fastapi python scripts/drop_duplicate_pk_indexes.py
"""

import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine

# PK와 중복되는 ix_*_id 인덱스 목록
DUPLICATE_INDEXES = [
    "ix_investing_exchange_rates_id",
    "ix_bank_exchange_rates_id",
    "ix_market_index_rates_id",
    "ix_notification_settings_id",
    "ix_notification_logs_id",
    "ix_user_devices_id",
    "ix_crawler_config_id",
]


def main():
    is_postgres = "postgresql" in str(engine.url)

    with engine.connect() as conn:
        if is_postgres:
            # CONCURRENTLY는 트랜잭션 밖에서만 가능
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")

        for idx_name in DUPLICATE_INDEXES:
            try:
                if is_postgres:
                    conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {idx_name}"))
                else:
                    conn.execute(text(f"DROP INDEX IF EXISTS {idx_name}"))
                print(f"  Dropped: {idx_name}")
            except Exception as e:
                print(f"  Skip: {idx_name} ({e})")

    print("Done.")


if __name__ == "__main__":
    main()
