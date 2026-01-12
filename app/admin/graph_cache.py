"""
24시간 그래프 데이터 생성 및 캐싱 (10분 단일 버킷)

주요 함수:
- build_graph_series(source, currency): 24h 구간을 10분 버킷으로 집계해
  [ts, max, min, close] 리스트 반환. 데이터가 없을 때는 직전 close를
  carry-forward하여 캔들/라인이 끊기지 않도록 한다.
- refresh_graph_cache(): 매분 03초 실행, Redis에 캐시 저장

Note: async 제거 - SQLite는 동기만 지원, APScheduler가 별도 스레드에서 실행
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Dict, Optional
from sqlalchemy import text

from app.database import get_db_context
from app.logging import get_logger

logger = get_logger("exchange_rate.admin.graph_cache")
KST = timezone(timedelta(hours=9))


def _to_utc_datetime(ts_val: object) -> datetime:
    """DB에서 읽은 timestamp를 UTC-aware datetime으로 변환."""
    if isinstance(ts_val, datetime):
        dt = ts_val
    else:
        dt = datetime.fromisoformat(ts_val)  # type: ignore[arg-type]

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_last_point(source: str, currency: str) -> Optional[List[int]]:
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
    ts_val, rate = row
    try:
        dt = _to_utc_datetime(ts_val)
        return [int(dt.timestamp()), round(float(rate), 2)]
    except Exception:
        return None



def fetch_last_before(source: str, currency: str, cutoff_ts: int) -> Optional[List[int]]:
    """cutoff_ts(Unix timestamp) 이전/동일 최신 1건 반환."""
    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = "AND bank = :bank" if source != "investing" else ""

        cutoff_dt = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)
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
    ts_val, rate = row
    try:
        dt = _to_utc_datetime(ts_val)
        return [int(dt.timestamp()), round(float(rate), 2)]
    except Exception:
        return None


def build_graph_series(source: str, currency: str) -> Tuple[List[List[float]], int]:
    """
    24시간 구간을 10분 버킷으로 집계하여 [ts, max, min, close] 리스트를 만든다.
    - ts: 버킷 시작 시각(Unix, 초)
    - max/min/close: 버킷 내 최고/최저/종가. 데이터가 없으면 직전 close를 carry-forward.
    Returns: (series, latest_ts)
    """

    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = "AND bank = :bank" if source != "investing" else ""

        now_kst = datetime.now(KST)
        window_start_kst = now_kst - timedelta(hours=24)
        window_start_ts = int(window_start_kst.timestamp())
        # 버킷 정렬: 10분 경계로 맞춰 시작 (KST 기준 유지)
        bucket_start_ts = window_start_ts - (window_start_ts % 600)

        # 모든 DB에서 UTC 기준으로 쿼리
        start_query = window_start_kst.astimezone(timezone.utc)
        end_query = now_kst.astimezone(timezone.utc)

        # 윈도우 내 데이터 조회
        rows = db.execute(
            text(f"""
                SELECT timestamp, rate
                FROM {table}
                WHERE timestamp >= :start
                  AND timestamp <= :end
                  AND currency = :currency
                  {where_clause}
                ORDER BY timestamp ASC
            """),
            {
                "currency": currency,
                "start": start_query.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end_query.strftime("%Y-%m-%d %H:%M:%S"),
                **({"bank": source} if source != "investing" else {})
            }
        ).fetchall()

    # 윈도우 시작 이전 직전 값 (carry-forward 초기값)
    last_before = fetch_last_before(source, currency, bucket_start_ts)
    prev_close: Optional[float] = last_before[1] if last_before else None

    points: List[Tuple[int, float]] = []
    for ts_val, rate in rows:
        try:
            dt = _to_utc_datetime(ts_val)
            points.append((int(dt.timestamp()), float(rate)))
        except Exception:
            continue

    series: List[List[float]] = []
    latest_ts = 0

    idx = 0
    total = len(points)

    ts_cursor = bucket_start_ts
    now_ts = int(now_kst.timestamp())

    while ts_cursor <= now_ts:
        bucket_end = ts_cursor + 600
        bucket_vals: List[float] = []

        # 수집
        while idx < total and points[idx][0] < bucket_end:
            bucket_vals.append(points[idx][1])
            idx += 1

        if bucket_vals:
            bucket_max = max(bucket_vals)
            bucket_min = min(bucket_vals)
            bucket_close = bucket_vals[-1]
            prev_close = bucket_close
        elif prev_close is not None:
            # 데이터가 없으면 직전 close를 유지
            bucket_max = bucket_min = bucket_close = prev_close
        else:
            ts_cursor = bucket_end
            continue

        series.append([ts_cursor, round(bucket_max, 2), round(bucket_min, 2), round(bucket_close, 2)])
        latest_ts = ts_cursor
        ts_cursor = bucket_end

    return series, latest_ts
def refresh_graph_cache():
    """
    24시간 그래프 데이터 갱신 (동기, 매분 03초)
    - 10분 단일 버킷 [ts, max, min, close]
    - 데이터 없을 때 직전 close로 채워 연속성 유지
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
    try:
        for currency in currencies:
            graph_data: Dict[str, List[List[float]]] = {}
            max_ts = 0

            for source in sources:
                series, latest_ts = build_graph_series(source, currency)
                graph_data[source] = series
                if latest_ts > max_ts:
                    max_ts = latest_ts

            cache_value = {
                "data": graph_data,
                "data_timestamp": max_ts,
                "cached_at": int(time.time())
            }
            redis_client.setex(f"graph:{currency}", 120, json.dumps(cache_value))
            logger.debug(
                "✅ 그래프 캐시 갱신",
                extra={"currency": currency, "data_timestamp": max_ts, "cached_at": cache_value["cached_at"]}
            )
    except Exception:
        logger.exception("그래프 캐시 갱신 실패")
    finally:
        try:
            redis_client.close()
        except Exception:
            pass
