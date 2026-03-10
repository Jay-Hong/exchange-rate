"""
그래프 데이터 생성 및 캐싱

주요 함수:
- build_graph_series(source, currency): 24h 환율 (10분 버킷)
- build_dxy_graph_series(): 24h DXY (10분 버킷, realtime)
- build_period_graph_series(): 1w/3m/1y (시간봉/일봉 버킷)
- refresh_graph_cache(): 매분 03초 실행, Redis에 캐시 저장

기간별 설정:
  1d: 10분 버킷, 환율(investing/kb/hana) + DXY(realtime)
  1w: 1시간 버킷, 환율(investing) + DXY(과거=hourly, 오늘=realtime>hourly timestamp단위)
  3m: 1일 버킷, 환율(investing) + DXY(과거=daily, 오늘=realtime있으면daily제외)
  1y: 1일 버킷, 환율(investing) + DXY(과거=daily, 오늘=realtime있으면daily제외)

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

    return _bucketize(points, bucket_start_ts, int(now_kst.timestamp()), 600, prev_close, 2)


# ═════════════════════════════════════════════════════════════
# 기간별 설정
# ═════════════════════════════════════════════════════════════

PERIOD_CONFIG = {
    "1d": {
        "window": timedelta(hours=24),
        "bucket_seconds": 600,         # 10분
        "exchange_sources": ["investing", "kb", "hana"],
        "dxy_granularities": ["realtime"],
        "dxy_currencies": ["usd-krw"],  # DXY는 USD/KRW만
    },
    "1w": {
        "window": timedelta(days=7),
        "bucket_seconds": 3600,        # 1시간
        "exchange_sources": ["investing"],
        "dxy_granularities": ["realtime", "hourly"],
        "dxy_currencies": ["usd-krw"],
    },
    "3m": {
        "window": timedelta(days=90),
        "bucket_seconds": 86400,       # 1일
        "exchange_sources": ["investing"],
        "dxy_granularities": ["realtime", "daily"],
        "dxy_currencies": ["usd-krw"],
    },
    "1y": {
        "window": timedelta(days=365),
        "bucket_seconds": 86400,       # 1일
        "exchange_sources": ["investing"],
        "dxy_granularities": ["realtime", "daily"],
        "dxy_currencies": ["usd-krw"],
    },
}


# ═════════════════════════════════════════════════════════════
# DXY 그래프 시리즈 (1일용, 10분 버킷)
# ═════════════════════════════════════════════════════════════

def build_dxy_graph_series() -> Tuple[List[List[float]], int]:
    """
    24시간 DXY 데이터를 10분 버킷으로 집계 (realtime granularity만).
    같은 timestamp에 investing/yahoo 공존 시 investing 우선 (ROW_NUMBER).

    Returns: (series, latest_ts) — 기존 build_graph_series와 동일 형식
    """
    with get_db_context() as db:
        now_kst = datetime.now(KST)
        window_start_kst = now_kst - timedelta(hours=24)
        window_start_ts = int(window_start_kst.timestamp())
        bucket_start_ts = window_start_ts - (window_start_ts % 600)

        start_query = window_start_kst.astimezone(timezone.utc)
        end_query = now_kst.astimezone(timezone.utc)

        # source dedup: investing > yahoo (같은 timestamp)
        rows = db.execute(
            text("""
                SELECT timestamp, rate FROM (
                    SELECT timestamp, rate,
                        ROW_NUMBER() OVER (
                            PARTITION BY timestamp
                            ORDER BY CASE WHEN source = 'investing' THEN 0 ELSE 1 END, id DESC
                        ) AS rn
                    FROM market_index_rates
                    WHERE instrument = 'dxy'
                      AND granularity = 'realtime'
                      AND timestamp >= :start
                      AND timestamp <= :end
                ) ranked
                WHERE rn = 1
                ORDER BY timestamp ASC
            """),
            {
                "start": start_query.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end_query.strftime("%Y-%m-%d %H:%M:%S"),
            }
        ).fetchall()

        # carry-forward: investing 우선으로 직전 1건
        before_row = db.execute(
            text("""
                SELECT rate FROM market_index_rates
                WHERE instrument = 'dxy'
                  AND granularity = 'realtime'
                  AND timestamp < :start
                ORDER BY timestamp DESC,
                    CASE WHEN source = 'investing' THEN 0 ELSE 1 END,
                    id DESC
                LIMIT 1
            """),
            {"start": start_query.strftime("%Y-%m-%d %H:%M:%S")}
        ).fetchone()

    prev_close: Optional[float] = float(before_row[0]) if before_row else None

    points: List[Tuple[int, float]] = []
    for ts_val, rate in rows:
        try:
            dt = _to_utc_datetime(ts_val)
            points.append((int(dt.timestamp()), float(rate)))
        except Exception:
            continue

    return _bucketize(points, bucket_start_ts, int(now_kst.timestamp()), 600, prev_close, 3)


# ═════════════════════════════════════════════════════════════
# 장기 그래프 시리즈 (1w/3m/1y)
# ═════════════════════════════════════════════════════════════

def build_period_exchange_series(
    source: str, currency: str, period: str,
) -> Tuple[List[List[float]], int]:
    """
    장기 환율 그래프 시리즈 (1w/3m/1y).
    1w=1시간 버킷, 3m/1y=1일 버킷.
    source가 "investing"이 아닌 은행이면 해당 은행 테이블 조회.
    장기(1w+)에서는 API key 'reference'로 표시하지만 실제 데이터는 investing 테이블.
    """
    config = PERIOD_CONFIG[period]
    bucket_seconds = config["bucket_seconds"]

    with get_db_context() as db:
        table = "investing_exchange_rates" if source == "investing" else "bank_exchange_rates"
        where_clause = "AND bank = :bank" if source != "investing" else ""

        now_kst = datetime.now(KST)
        window_start_kst = now_kst - config["window"]
        window_start_ts = int(window_start_kst.timestamp())
        bucket_start_ts = window_start_ts - (window_start_ts % bucket_seconds)

        start_query = window_start_kst.astimezone(timezone.utc)
        end_query = now_kst.astimezone(timezone.utc)

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

        # carry-forward 초기값
        before_row = db.execute(
            text(f"""
                SELECT rate FROM {table}
                WHERE timestamp < :start
                  AND currency = :currency
                  {where_clause}
                ORDER BY timestamp DESC LIMIT 1
            """),
            {
                "currency": currency,
                "start": start_query.strftime("%Y-%m-%d %H:%M:%S"),
                **({"bank": source} if source != "investing" else {})
            }
        ).fetchone()

    prev_close: Optional[float] = float(before_row[0]) if before_row else None

    points: List[Tuple[int, float]] = []
    for ts_val, rate in rows:
        try:
            dt = _to_utc_datetime(ts_val)
            points.append((int(dt.timestamp()), float(rate)))
        except Exception:
            continue

    return _bucketize(points, bucket_start_ts, int(now_kst.timestamp()), bucket_seconds, prev_close, 2)


def build_period_dxy_series(period: str) -> Tuple[List[List[float]], int]:
    """
    장기 DXY 그래프 시리즈 (1w/3m/1y).

    데이터 분리 전략 (daily/hourly + realtime 혼재 방지):
      - 과거 구간: daily(3m/1y) 또는 hourly(1w)만 사용
      - 오늘 구간(UTC 00:00 이후):
        · 1w(시간봉): timestamp 단위 realtime > hourly 선택 (ROW_NUMBER)
        · 3m/1y(일봉): 날짜 단위 배타적 선택 (realtime 있으면 daily 제외)
      → 1w는 hourly 데이터 보존, 3m/1y는 daily 00:00 오염 방지

    source 우선순위: investing > yahoo (같은 timestamp).
    """
    config = PERIOD_CONFIG[period]
    bucket_seconds = config["bucket_seconds"]
    # realtime 외의 granularity (daily 또는 hourly)
    backfill_granularities = [g for g in config["dxy_granularities"] if g != "realtime"]

    with get_db_context() as db:
        now_kst = datetime.now(KST)
        window_start_kst = now_kst - config["window"]
        window_start_ts = int(window_start_kst.timestamp())
        bucket_start_ts = window_start_ts - (window_start_ts % bucket_seconds)

        start_query = window_start_kst.astimezone(timezone.utc)
        end_query = now_kst.astimezone(timezone.utc)

        # 오늘 UTC 00:00:00 — 이 시점 기준으로 과거/오늘 분리
        today_utc = end_query.replace(hour=0, minute=0, second=0, microsecond=0)
        today_str = today_utc.strftime("%Y-%m-%d %H:%M:%S")

        all_points: List[Tuple[int, float]] = []

        # Part 1: 과거 구간 — backfill granularity (daily/hourly)만
        if backfill_granularities:
            placeholders = ", ".join(f":g{i}" for i in range(len(backfill_granularities)))
            params = {
                "start": start_query.strftime("%Y-%m-%d %H:%M:%S"),
                "end": today_str,  # 오늘 미포함
            }
            for i, g in enumerate(backfill_granularities):
                params[f"g{i}"] = g

            past_rows = db.execute(
                text(f"""
                    SELECT timestamp, rate FROM (
                        SELECT timestamp, rate,
                            ROW_NUMBER() OVER (
                                PARTITION BY timestamp, granularity
                                ORDER BY CASE WHEN source = 'investing' THEN 0 ELSE 1 END, id DESC
                            ) AS rn
                        FROM market_index_rates
                        WHERE instrument = 'dxy'
                          AND granularity IN ({placeholders})
                          AND timestamp >= :start
                          AND timestamp < :end
                    ) ranked
                    WHERE rn = 1
                    ORDER BY timestamp ASC
                """),
                params,
            ).fetchall()

            for ts_val, rate in past_rows:
                try:
                    dt = _to_utc_datetime(ts_val)
                    all_points.append((int(dt.timestamp()), float(rate)))
                except Exception:
                    continue

        # Part 2: 오늘 구간
        # 1w(시간봉): timestamp 단위로 realtime > hourly 선택 (ROW_NUMBER)
        #   → hourly 데이터 활용하면서 realtime 있는 시각은 realtime 우선
        # 3m/1y(일봉): 날짜 단위 배타적 선택 (realtime 있으면 daily 전체 제외)
        #   → daily 00:00 종가와 realtime 장중 틱이 같은 버킷에 섞이는 왜곡 방지
        today_end_str = end_query.strftime("%Y-%m-%d %H:%M:%S")
        use_daily_bucket = bucket_seconds >= 86400  # 1일 이상 버킷

        if use_daily_bucket:
            # 3m/1y: 날짜 단위 배타적 선택
            has_realtime_today = db.execute(
                text("""
                    SELECT 1 FROM market_index_rates
                    WHERE instrument = 'dxy'
                      AND granularity = 'realtime'
                      AND timestamp >= :today
                      AND timestamp <= :end
                    LIMIT 1
                """),
                {"today": today_str, "end": today_end_str},
            ).fetchone() is not None

            today_grans = ["realtime"] if has_realtime_today else backfill_granularities
        else:
            # 1w: timestamp 단위 우선순위 (realtime + hourly 공존 허용)
            today_grans = ["realtime"] + backfill_granularities

        today_placeholders = ", ".join(f":tg{i}" for i in range(len(today_grans)))
        today_params = {
            "today": today_str,
            "end": today_end_str,
        }
        for i, g in enumerate(today_grans):
            today_params[f"tg{i}"] = g

        # 1w: granularity 우선순위 (realtime > hourly > daily) + source 우선순위
        # 3m/1y: source 우선순위만 (단일 granularity이므로)
        if not use_daily_bucket and len(today_grans) > 1:
            order_clause = """
                                CASE granularity
                                    WHEN 'realtime' THEN 0
                                    WHEN 'hourly' THEN 1
                                    WHEN 'daily' THEN 2
                                    ELSE 3
                                END,
                                CASE WHEN source = 'investing' THEN 0 ELSE 1 END,
                                id DESC"""
        else:
            order_clause = "CASE WHEN source = 'investing' THEN 0 ELSE 1 END, id DESC"

        today_rows = db.execute(
            text(f"""
                SELECT timestamp, rate FROM (
                    SELECT timestamp, rate,
                        ROW_NUMBER() OVER (
                            PARTITION BY timestamp
                            ORDER BY {order_clause}
                        ) AS rn
                    FROM market_index_rates
                    WHERE instrument = 'dxy'
                      AND granularity IN ({today_placeholders})
                      AND timestamp >= :today
                      AND timestamp <= :end
                ) ranked
                WHERE rn = 1
                ORDER BY timestamp ASC
            """),
            today_params,
        ).fetchall()

        for ts_val, rate in today_rows:
            try:
                dt = _to_utc_datetime(ts_val)
                all_points.append((int(dt.timestamp()), float(rate)))
            except Exception:
                continue

        # carry-forward: 윈도우 시작 직전
        before_row = None
        if backfill_granularities:
            placeholders = ", ".join(f":g{i}" for i in range(len(backfill_granularities)))
            bf_params = {"start": start_query.strftime("%Y-%m-%d %H:%M:%S")}
            for i, g in enumerate(backfill_granularities):
                bf_params[f"g{i}"] = g

            before_row = db.execute(
                text(f"""
                    SELECT rate FROM market_index_rates
                    WHERE instrument = 'dxy'
                      AND granularity IN ({placeholders})
                      AND timestamp < :start
                    ORDER BY timestamp DESC,
                        CASE WHEN source = 'investing' THEN 0 ELSE 1 END,
                        id DESC
                    LIMIT 1
                """),
                bf_params,
            ).fetchone()

    prev_close: Optional[float] = float(before_row[0]) if before_row else None

    return _bucketize(all_points, bucket_start_ts, int(now_kst.timestamp()), bucket_seconds, prev_close, 3)


# ═════════════════════════════════════════════════════════════
# 공통 버킷 집계
# ═════════════════════════════════════════════════════════════

def _bucketize(
    points: List[Tuple[int, float]],
    bucket_start_ts: int,
    now_ts: int,
    bucket_seconds: int,
    prev_close: Optional[float],
    decimal_places: int = 2,
) -> Tuple[List[List[float]], int]:
    """
    포인트 리스트를 버킷으로 집계하여 [ts, max, min, close] 시리즈 생성.

    Args:
        points: [(epoch_seconds, value), ...] — timestamp ASC 정렬
        bucket_start_ts: 첫 버킷 시작 시각 (epoch)
        now_ts: 현재 시각 (epoch, 마지막 버킷 한계)
        bucket_seconds: 버킷 크기 (초)
        prev_close: carry-forward 초기값
        decimal_places: 소수점 자릿수 (환율=2, DXY=3)
    """
    series: List[List[float]] = []
    latest_ts = 0

    idx = 0
    total = len(points)
    ts_cursor = bucket_start_ts

    while ts_cursor <= now_ts:
        bucket_end = ts_cursor + bucket_seconds
        bucket_vals: List[float] = []

        while idx < total and points[idx][0] < bucket_end:
            bucket_vals.append(points[idx][1])
            idx += 1

        if bucket_vals:
            bucket_max = max(bucket_vals)
            bucket_min = min(bucket_vals)
            bucket_close = bucket_vals[-1]
            prev_close = bucket_close
        elif prev_close is not None:
            bucket_max = bucket_min = bucket_close = prev_close
        else:
            ts_cursor = bucket_end
            continue

        series.append([
            ts_cursor,
            round(bucket_max, decimal_places),
            round(bucket_min, decimal_places),
            round(bucket_close, decimal_places),
        ])
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

            # DXY는 USD/KRW만 (1일 그래프용 realtime)
            if currency == "usd-krw":
                dxy_series, dxy_ts = build_dxy_graph_series()
                if dxy_series:
                    graph_data["dxy"] = dxy_series
                    if dxy_ts > max_ts:
                        max_ts = dxy_ts

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
