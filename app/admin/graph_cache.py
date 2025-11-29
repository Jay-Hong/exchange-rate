"""
24시간 그래프 데이터 생성 및 캐싱

주요 함수:
- fetch_recent_1h(source, currency): 최근 1시간 1분 평균
- fetch_day_23h(source, currency): 1-24시간 10분 평균
- refresh_graph_cache(): 매분 03초 실행, Redis에 캐시 저장

Note: async 제거 - SQLite는 동기만 지원, APScheduler가 별도 스레드에서 실행
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import List
from sqlalchemy import text

from app.database import get_db_context
from app.logging import get_logger

logger = get_logger("exchange_rate.admin.graph_cache")
KST = timezone(timedelta(hours=9))


def fetch_recent_1h(source: str, currency: str) -> List[List]:
    """
    최근 1시간 1분 평균 데이터 조회 (동기 함수)

    Args:
        source: "investing" | "kb" | "hana"
        currency: "usd-krw" | "jpy-krw" | "eur-krw"

    Returns:
        [[timestamp, avg_rate], ...] (최대 60개)
    """
    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = f"AND bank = '{source}'" if source != "investing" else ""

        query = text(f"""
            SELECT
                strftime('%s', strftime('%Y-%m-%d %H:%M:00', timestamp)) as bucket_ts,
                AVG(rate) as avg_rate
            FROM {table}
            WHERE timestamp >= datetime('now', '+8 hours')
              AND currency = :currency
              {where_clause}
            GROUP BY bucket_ts
            ORDER BY bucket_ts ASC
        """)

        result = db.execute(query, {"currency": currency}).fetchall()

        return [[int(row[0]), round(float(row[1]), 2)] for row in result]


def fetch_day_23h(source: str, currency: str) -> List[List]:
    """
    1-24시간 10분 윈도우 평균 데이터 조회 (동기 함수)

    Args:
        source: "investing" | "kb" | "hana"
        currency: "usd-krw" | "jpy-krw" | "eur-krw"

    Returns:
        [[timestamp, avg_rate], ...] (최대 138개)
    """
    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = f"AND bank = '{source}'" if source != "investing" else ""

        query = text(f"""
            SELECT
                strftime('%s',
                    strftime('%Y-%m-%d %H:', timestamp) ||
                    printf('%02d:00', CAST(strftime('%M', timestamp) AS INTEGER) / 10 * 10)
                ) as bucket_ts,
                AVG(rate) as avg_rate
            FROM {table}
            WHERE timestamp >= datetime('now', '-15 hours')
              AND timestamp < datetime('now', '+8 hours')
              AND currency = :currency
              {where_clause}
            GROUP BY bucket_ts
            ORDER BY bucket_ts ASC
        """)

        result = db.execute(query, {"currency": currency}).fetchall()

        return [[int(row[0]), round(float(row[1]), 2)] for row in result]


def refresh_graph_cache():
    """
    24시간 그래프 데이터 갱신 (동기 함수, 매분 03초 실행)

    Redis 키: graph:{currency}
    TTL: 120초 (2분)

    Note: APScheduler가 별도 스레드에서 실행하므로 블로킹 OK
    """
    # Redis 동기 클라이언트
    import redis as sync_redis
    from app.config import REDIS_URL
    import os

    # REDIS_URL 파싱 (password 처리)
    redis_password = os.getenv("REDIS_PASSWORD") or None

    try:
        redis_client = sync_redis.from_url(
            REDIS_URL,
            password=redis_password,
            decode_responses=True
        )
        redis_client.ping()
    except Exception as e:
        logger.warning(f"Redis 연결 실패: {e}")
        return

    currencies = ["usd-krw", "jpy-krw", "eur-krw"]
    sources = ["investing", "kb", "hana"]

    for currency in currencies:
        try:
            graph_data = {}
            max_timestamp = 0

            for source in sources:
                recent = fetch_recent_1h(source, currency)
                day = fetch_day_23h(source, currency)

                # 실제 데이터 최신 시간 추적 (recent + day 모두 확인)
                if recent and recent[-1][0] > max_timestamp:
                    max_timestamp = recent[-1][0]
                if day and day[-1][0] > max_timestamp:
                    max_timestamp = day[-1][0]

                graph_data[source] = {
                    "recent": recent,
                    "day": day
                }

            # Redis 저장 (메타데이터 포함)
            cache_value = {
                "data": graph_data,
                "data_timestamp": max_timestamp,
                "cached_at": int(time.time())
            }

            cache_key = f"graph:{currency}"
            redis_client.setex(
                cache_key,
                120,  # TTL 120초 (2분)
                json.dumps(cache_value)
            )

            logger.debug(
                f"✅ 그래프 캐시 갱신",
                extra={
                    "currency": currency,
                    "data_timestamp": max_timestamp,
                    "cached_at": cache_value["cached_at"]
                }
            )

        except Exception as e:
            logger.exception(
                f"그래프 캐시 갱신 실패",
                extra={"currency": currency}
            )
