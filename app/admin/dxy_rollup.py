"""
DXY 실시간 데이터 → hourly/daily 집계 (rollup)

스케줄:
  - hourly: 매시 :05분에 실행, 직전 완료 시간 집계
  - daily: 매일 00:05 KST에 실행, 직전 완료일 집계

설계 원칙:
  - 닫힌 버킷만 집계 (현재 진행 중인 시간/일은 제외)
  - INSERT ON CONFLICT UPDATE로 idempotent (여러 번 돌려도 동일 결과)
  - 시간봉: UTC 정시 경계
  - 일봉: 00:00 UTC 저장 (backfill 관례와 동일)
  - source 보존: realtime 원본의 실제 source를 그대로 사용 (investing/yahoo 등)

market_index_rates 테이블 UNIQUE: (instrument, source, timestamp, granularity)
  → 같은 시각에 source='yahoo'(backfill)와 source='investing'(rollup) 공존 가능
  → 쿼리 시 investing 우선 dedup이 적용되어 rollup 데이터가 자동 우선
"""

from datetime import datetime, timedelta, timezone
from sqlalchemy import text

from app.database import get_db_context
from app.logging import get_logger

logger = get_logger("exchange_rate.admin.dxy_rollup")
KST = timezone(timedelta(hours=9))


def rollup_dxy_hourly():
    """
    직전 완료 시간의 realtime DXY → hourly 1건 생성.

    매시 :05에 실행 (예: 14:05 KST → 05:00~05:59 UTC 집계).
    rate = close (해당 시간의 마지막 값).
    """
    now_utc = datetime.now(timezone.utc)
    # 직전 완료 시간: 현재 UTC 시간의 정각 - 1시간
    bucket_end = now_utc.replace(minute=0, second=0, microsecond=0)
    bucket_start = bucket_end - timedelta(hours=1)

    start_str = bucket_start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = bucket_end.strftime("%Y-%m-%d %H:%M:%S")

    # naive UTC timestamp (DB 저장용)
    record_ts = bucket_start.replace(tzinfo=None)

    with get_db_context() as db:
        # realtime 데이터에서 close(마지막 값) + 실제 source 조회
        row = db.execute(
            text("""
                SELECT rate, source FROM market_index_rates
                WHERE instrument = 'dxy'
                  AND granularity = 'realtime'
                  AND timestamp >= :start
                  AND timestamp < :end
                ORDER BY timestamp DESC
                LIMIT 1
            """),
            {"start": start_str, "end": end_str},
        ).fetchone()

        if not row:
            logger.debug(
                f"DXY hourly rollup: realtime 데이터 없음 "
                f"({bucket_start.strftime('%Y-%m-%d %H:%M')} UTC)"
            )
            return

        close_rate = round(float(row[0]), 3)
        actual_source = row[1]  # 'investing' 또는 'yahoo'

        db.execute(
            text("""
                INSERT INTO market_index_rates
                    (instrument, source, rate, timestamp, granularity)
                VALUES ('dxy', :source, :rate, :ts, 'hourly')
                ON CONFLICT (instrument, source, timestamp, granularity)
                DO UPDATE SET rate = :rate
            """),
            {"source": actual_source, "rate": close_rate, "ts": record_ts},
        )
        db.commit()

    logger.info(
        f"✅ DXY hourly rollup: {bucket_start.strftime('%Y-%m-%d %H:%M')} UTC, "
        f"source={actual_source}, close={close_rate}"
    )


def rollup_dxy_daily():
    """
    직전 완료일의 DXY → daily 1건 생성.

    매일 00:05 KST에 실행.
    집계 범위: 전일 00:00~23:59 KST (= 전전일 15:00 UTC ~ 전일 15:00 UTC).
    저장 timestamp: 전일 날짜의 00:00 UTC (backfill 관례 동일).
    rate = close (해당 일의 마지막 값).

    소스 우선순위: hourly(rollup) > realtime (hourly가 있으면 hourly에서 집계).
    """
    now_kst = datetime.now(KST)
    # 직전 완료일: 어제 (KST 기준)
    yesterday_kst = (now_kst - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_kst = yesterday_kst + timedelta(days=1)

    # UTC 변환
    start_utc = yesterday_kst.astimezone(timezone.utc)
    end_utc = today_kst.astimezone(timezone.utc)

    start_str = start_utc.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_utc.strftime("%Y-%m-%d %H:%M:%S")

    # 저장 timestamp: 해당 날짜의 00:00 UTC (naive) — backfill 관례 동일
    record_ts = datetime(
        yesterday_kst.year, yesterday_kst.month, yesterday_kst.day
    )

    with get_db_context() as db:
        # 우선 hourly에서 close + source 조회 (rollup hourly가 있으면 사용)
        row = db.execute(
            text("""
                SELECT rate, source FROM market_index_rates
                WHERE instrument = 'dxy'
                  AND granularity = 'hourly'
                  AND timestamp >= :start
                  AND timestamp < :end
                ORDER BY timestamp DESC
                LIMIT 1
            """),
            {"start": start_str, "end": end_str},
        ).fetchone()

        # hourly 없으면 realtime fallback
        if not row:
            row = db.execute(
                text("""
                    SELECT rate, source FROM market_index_rates
                    WHERE instrument = 'dxy'
                      AND granularity = 'realtime'
                      AND timestamp >= :start
                      AND timestamp < :end
                    ORDER BY timestamp DESC
                    LIMIT 1
                """),
                {"start": start_str, "end": end_str},
            ).fetchone()

        if not row:
            logger.debug(
                f"DXY daily rollup: 데이터 없음 ({yesterday_kst.strftime('%Y-%m-%d')})"
            )
            return

        close_rate = round(float(row[0]), 3)
        actual_source = row[1]  # hourly 또는 realtime의 실제 source

        db.execute(
            text("""
                INSERT INTO market_index_rates
                    (instrument, source, rate, timestamp, granularity)
                VALUES ('dxy', :source, :rate, :ts, 'daily')
                ON CONFLICT (instrument, source, timestamp, granularity)
                DO UPDATE SET rate = :rate
            """),
            {"source": actual_source, "rate": close_rate, "ts": record_ts},
        )
        db.commit()

    logger.info(
        f"✅ DXY daily rollup: {yesterday_kst.strftime('%Y-%m-%d')}, "
        f"source={actual_source}, close={close_rate}"
    )
