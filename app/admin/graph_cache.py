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
_KST_OFFSET_SECONDS = 9 * 3600  # KST = UTC+9


def _align_bucket_start(ts: int, bucket_seconds: int) -> int:
    """
    버킷 시작 시각 정렬.
    - 일봉(86400초 이상): KST 00:00 경계로 정렬
    - 시간봉/10분봉: UTC 정시 경계 (KST 오프셋이 3600의 배수이므로 결과 동일)
    """
    if bucket_seconds >= 86400:
        # KST 자정 경계: ts를 KST 기준으로 변환 후 정렬, 다시 UTC로
        return ts - ((ts + _KST_OFFSET_SECONDS) % bucket_seconds)
    return ts - (ts % bucket_seconds)


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
        bucket_start_ts = _align_bucket_start(window_start_ts, bucket_seconds)

        # 쿼리 시작점: bucket_start_ts로 확장 (첫 버킷 완전 데이터 보장)
        query_start = datetime.fromtimestamp(bucket_start_ts, tz=timezone.utc)
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
                "start": query_start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end_query.strftime("%Y-%m-%d %H:%M:%S"),
                **({"bank": source} if source != "investing" else {})
            }
        ).fetchall()

        # carry-forward 초기값 (bucket_start_ts 이전에서 탐색)
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
                "start": query_start.strftime("%Y-%m-%d %H:%M:%S"),
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

    기간별 쿼리 전략:
      1w: full-window 전략
        - hourly + realtime을 전체 7일 윈도우에서 모두 조회
        - 같은 exact timestamp에서 hourly > realtime dedup (hourly가 더 정확)
        - 7일 realtime은 ~1만건 수준이라 성능 부담 없음
        - hourly에 gap이 있어도 realtime이 자연스럽게 보충 → carry-forward 최소화
        - 이유: rollup 중단/배포 공백/수집 실패 시에도 그래프가 깨지지 않도록

      3m/1y: 2단계 쿼리 전략 (realtime 스캔 범위 최적화)
        - backfill(daily)을 전체 윈도우에서 조회
        - realtime은 backfill 마지막 시점 이후만 조회 (gap 보충 목적)
        - realtime이 수십만 건으로 늘어도 스캔 범위가 제한됨

    dedup 주의사항:
      - PARTITION BY timestamp는 정확한 timestamp 일치에만 적용됨
      - 예: daily(00:00 UTC)와 realtime(14:30 UTC)은 timestamp가 달라 동일 버킷에 공존
      - 이는 의도된 동작: 같은 일봉 버킷에 daily + realtime이 섞여 max/min이 더 정확해짐

    source 우선순위: investing > yahoo (같은 timestamp).
    """
    config = PERIOD_CONFIG[period]
    bucket_seconds = config["bucket_seconds"]

    with get_db_context() as db:
        now_kst = datetime.now(KST)
        window_start_kst = now_kst - config["window"]
        window_start_ts = int(window_start_kst.timestamp())
        bucket_start_ts = _align_bucket_start(window_start_ts, bucket_seconds)

        # 쿼리 시작점: bucket_start_ts로 확장 (첫 버킷 완전 데이터 보장)
        query_start = datetime.fromtimestamp(bucket_start_ts, tz=timezone.utc)
        end_query = now_kst.astimezone(timezone.utc)

        start_str = query_start.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_query.strftime("%Y-%m-%d %H:%M:%S")

        all_points: List[Tuple[int, float]] = []

        if period == "1w":
            # ── 1w: full-window 전략 ──
            # hourly + realtime을 전체 윈도우에서 조회, hourly > realtime dedup
            all_grans = config["dxy_granularities"]  # ["realtime", "hourly"]
            gran_placeholders = ", ".join(f":g{i}" for i in range(len(all_grans)))
            params: Dict = {"start": start_str, "end": end_str}
            for i, g in enumerate(all_grans):
                params[f"g{i}"] = g

            rows = db.execute(
                text(f"""
                    SELECT timestamp, rate FROM (
                        SELECT timestamp, rate,
                            ROW_NUMBER() OVER (
                                PARTITION BY timestamp
                                ORDER BY
                                    CASE granularity WHEN 'hourly' THEN 0 ELSE 1 END,
                                    CASE WHEN source = 'investing' THEN 0 ELSE 1 END,
                                    id DESC
                            ) AS rn
                        FROM market_index_rates
                        WHERE instrument = 'dxy'
                          AND granularity IN ({gran_placeholders})
                          AND timestamp >= :start
                          AND timestamp <= :end
                    ) ranked
                    WHERE rn = 1
                    ORDER BY timestamp ASC
                """),
                params,
            ).fetchall()

            for ts_val, rate in rows:
                try:
                    dt = _to_utc_datetime(ts_val)
                    all_points.append((int(dt.timestamp()), float(rate)))
                except Exception:
                    continue
        else:
            # ── 3m/1y: 2단계 쿼리 전략 ──
            backfill_granularities = [g for g in config["dxy_granularities"] if g != "realtime"]
            realtime_start_str = start_str  # fallback: backfill 없으면 전체 윈도우

            if backfill_granularities:
                bf_placeholders = ", ".join(f":bf{i}" for i in range(len(backfill_granularities)))
                bf_params: Dict = {"start": start_str, "end": end_str}
                for i, g in enumerate(backfill_granularities):
                    bf_params[f"bf{i}"] = g

                backfill_rows = db.execute(
                    text(f"""
                        SELECT timestamp, rate FROM (
                            SELECT timestamp, rate,
                                ROW_NUMBER() OVER (
                                    PARTITION BY timestamp
                                    ORDER BY CASE WHEN source = 'investing' THEN 0 ELSE 1 END,
                                        id DESC
                                ) AS rn
                            FROM market_index_rates
                            WHERE instrument = 'dxy'
                              AND granularity IN ({bf_placeholders})
                              AND timestamp >= :start
                              AND timestamp <= :end
                        ) ranked
                        WHERE rn = 1
                        ORDER BY timestamp ASC
                    """),
                    bf_params,
                ).fetchall()

                last_bf_ts: Optional[datetime] = None
                for ts_val, rate in backfill_rows:
                    try:
                        dt = _to_utc_datetime(ts_val)
                        all_points.append((int(dt.timestamp()), float(rate)))
                        last_bf_ts = dt
                    except Exception:
                        continue

                # realtime 시작점: backfill 마지막 시점 이후
                if last_bf_ts is not None:
                    realtime_start_str = last_bf_ts.strftime("%Y-%m-%d %H:%M:%S")

            # realtime은 backfill 끊긴 이후만 조회
            rt_rows = db.execute(
                text("""
                    SELECT timestamp, rate FROM (
                        SELECT timestamp, rate,
                            ROW_NUMBER() OVER (
                                PARTITION BY timestamp
                                ORDER BY CASE WHEN source = 'investing' THEN 0 ELSE 1 END,
                                    id DESC
                            ) AS rn
                        FROM market_index_rates
                        WHERE instrument = 'dxy'
                          AND granularity = 'realtime'
                          AND timestamp > :rt_start
                          AND timestamp <= :end
                    ) ranked
                    WHERE rn = 1
                    ORDER BY timestamp ASC
                """),
                {"rt_start": realtime_start_str, "end": end_str},
            ).fetchall()

            for ts_val, rate in rt_rows:
                try:
                    dt = _to_utc_datetime(ts_val)
                    all_points.append((int(dt.timestamp()), float(rate)))
                except Exception:
                    continue

        # timestamp 순 정렬
        all_points.sort(key=lambda p: p[0])

        # carry-forward: 윈도우 시작 직전
        all_grans_cf = list(set(config["dxy_granularities"]))
        gran_placeholders_cf = ", ".join(f":g{i}" for i in range(len(all_grans_cf)))
        cf_params: Dict = {"start": start_str}
        for i, g in enumerate(all_grans_cf):
            cf_params[f"g{i}"] = g

        before_row = db.execute(
            text(f"""
                SELECT rate FROM market_index_rates
                WHERE instrument = 'dxy'
                  AND granularity IN ({gran_placeholders_cf})
                  AND timestamp < :start
                ORDER BY timestamp DESC,
                    CASE WHEN source = 'investing' THEN 0 ELSE 1 END,
                    id DESC
                LIMIT 1
            """),
            cf_params,
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
