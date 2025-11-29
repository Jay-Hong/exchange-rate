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
    """최근 1시간 데이터를 1분 버킷으로 반환 (KST 오프셋 포함 문자열 안전 처리)."""
    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = "AND bank = :bank" if source != "investing" else ""
        rows = db.execute(
            text(f"""
                SELECT timestamp, rate
                FROM {table}
                WHERE timestamp >= datetime('now','localtime','-1 hour')
                  AND currency = :currency
                  {where_clause}
            """),
            {"currency": currency, **({"bank": source} if source != "investing" else {})}
        ).fetchall()

    # 파싱 및 1분 버킷 평균
    buckets = {}
    for ts_str, rate in rows:
        try:
            dt = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        bucket = dt.replace(second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(float(rate))

    result = []
    for bucket_dt in sorted(buckets.keys()):
        avg = sum(buckets[bucket_dt]) / len(buckets[bucket_dt])
        result.append([int(bucket_dt.timestamp()), round(avg, 2)])
    return result


def fetch_day_23h(source: str, currency: str) -> List[List]:
    """1~24시간 데이터를 10분 버킷으로 반환 (KST 오프셋 포함 문자열 안전 처리)."""
    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = "AND bank = :bank" if source != "investing" else ""
        rows = db.execute(
            text(f"""
                SELECT timestamp, rate
                FROM {table}
                WHERE timestamp >= datetime('now','localtime','-24 hours')
                  AND timestamp <  datetime('now','localtime','-1 hour')
                  AND currency = :currency
                  {where_clause}
            """),
            {"currency": currency, **({"bank": source} if source != "investing" else {})}
        ).fetchall()

    buckets = {}
    for ts_str, rate in rows:
        try:
            dt = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        minute_bucket = (dt.minute // 10) * 10
        bucket = dt.replace(minute=minute_bucket, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(float(rate))

    result = []
    for bucket_dt in sorted(buckets.keys()):
        avg = sum(buckets[bucket_dt]) / len(buckets[bucket_dt])
        result.append([int(bucket_dt.timestamp()), round(avg, 2)])
    return result


def fetch_last_point(source: str, currency: str) -> List[int] | None:
    """마지막 환율 1개 반환. 문자열 파싱 후 epoch 계산."""
    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = "AND bank = :bank" if source != "investing" else ""
        row = db.execute(
            text(f"""
                SELECT timestamp, rate
                FROM {table}
                WHERE currency = :currency
                  {where_clause}
                ORDER BY timestamp DESC
                LIMIT 1
            """),
            {"currency": currency, **({"bank": source} if source != "investing" else {})}
        ).fetchone()
    if not row:
        return None
    ts_str, rate = row
    try:
        dt = datetime.fromisoformat(ts_str)
        return [int(dt.timestamp()), round(float(rate), 2)]
    except Exception:
        return None




def fetch_last_before(source: str, currency: str, cutoff_ts: int) -> list | None:
    """cutoff_ts(Unix, KST 기준) 이전/동일 최신 1건 반환."""
    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = "AND bank = :bank" if source != "investing" else ""
        cutoff_dt = datetime.fromtimestamp(cutoff_ts, tz=KST)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
        row = db.execute(
            text(f"""
                SELECT timestamp, rate
                FROM {table}
                WHERE timestamp <= :cutoff
                  AND currency = :currency
                  {where_clause}
                ORDER BY timestamp DESC
                LIMIT 1
            """),
            {"currency": currency, "cutoff": cutoff_str, **({"bank": source} if source != "investing" else {})}
        ).fetchone()
    if not row:
        return None
    ts_str, rate = row
    try:
        dt = datetime.fromisoformat(ts_str)
        return [int(dt.timestamp()), round(float(rate), 2)]
    except Exception:
        return None
def refresh_graph_cache():
    """
    24시간 그래프 데이터 갱신 (동기, 매분 03초)
    - day: 24h 구간, 윈도우 시작 이전 마지막 값으로 캡핑 + now까지 연장
    - recent: 최근 1h 데이터만, 과거 backfill 없음, 마지막 포인트만 now까지 연장
    """
    import redis as sync_redis
    from app.config import REDIS_URL, REDIS_PASSWORD

    try:
        redis_client = sync_redis.from_url(
            REDIS_URL,
            password=REDIS_PASSWORD or None,
            decode_responses=True
        )
        redis_client.ping()
    except Exception as e:
        logger.warning(f"Redis 연결 실패: {e}")
        return

    currencies = ["usd-krw", "jpy-krw", "eur-krw"]
    sources = ["investing", "kb", "hana"]
    now_ts = int(time.time())
    window_start = now_ts - 24 * 60 * 60

    try:
        for currency in currencies:
            graph_data = {}
            max_ts = 0

            for source in sources:
                recent = fetch_recent_1h(source, currency)
                day = fetch_day_23h(source, currency)
                last_point = fetch_last_point(source, currency)
                last_before = fetch_last_before(source, currency, window_start)

                # day: 윈도우 시작 보정
                if day:
                    if day[0][0] > window_start:
                        if last_before:
                            day = [[window_start, last_before[1]]] + day
                        elif last_point:
                            day = [[window_start, last_point[1]]] + day
                else:
                    # Day 없을 때: window_start 포인트만 추가 (now_ts는 나중에 조건부 추가)
                    if last_before:
                        day = [[window_start, last_before[1]]]
                    elif last_point:
                        day = [[window_start, last_point[1]]]

                # day: now까지 연장 (Recent 없을 때만 - 중복 방지)
                if not recent and day and day[-1][0] < now_ts:
                    day = day + [[now_ts, day[-1][1]]]

                # recent: backfill 없이, 있을 때만 now까지 연장
                if recent and recent[-1][0] < now_ts:
                    recent = recent + [[now_ts, recent[-1][1]]]

                for series in (recent, day):
                    if series and series[-1][0] > max_ts:
                        max_ts = series[-1][0]

                graph_data[source] = {"recent": recent, "day": day}

            cache_value = {
                "data": graph_data,
                "data_timestamp": max_ts,
                "cached_at": int(time.time())
            }
            redis_client.setex(f"graph:{currency}", 120, json.dumps(cache_value))
            logger.debug("✅ 그래프 캐시 갱신", extra={"currency": currency, "data_timestamp": max_ts, "cached_at": cache_value["cached_at"]})
    except Exception:
        logger.exception("그래프 캐시 갱신 실패")
    finally:
        try:
            redis_client.close()
        except Exception:
            pass

