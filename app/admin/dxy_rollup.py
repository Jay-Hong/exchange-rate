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

수동 복구:
  - backfill_hourly_range(): 지정 구간의 realtime → hourly 일괄 생성
  - backfill_daily_range(): 지정 KST 날짜 구간의 hourly/realtime → daily 일괄 생성
  - 스케줄러 중단/배포 공백 등으로 hourly/daily가 비었을 때 사용

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


# ═════════════════════════════════════════════════════════════
# 수동 gap 복구: 지정 구간 hourly 일괄 생성
# ═════════════════════════════════════════════════════════════

def backfill_hourly_range(start_utc_str: str, end_utc_str: str) -> int:
    """
    지정 UTC 구간의 realtime DXY → hourly 일괄 생성.

    스케줄러 중단/배포 공백 등으로 hourly가 비었을 때 수동 실행용.
    각 정시 버킷에 대해 close(마지막 값) + 실제 source를 저장.
    ON CONFLICT UPDATE로 idempotent.

    Args:
        start_utc_str: 시작 시각 (UTC, "2026-03-10 16:00:00") — 포함
        end_utc_str:   종료 시각 (UTC, "2026-03-13 04:00:00") — 배타 상한
                       (03:00~03:59 버킷까지 포함하려면 04:00:00 지정)

    Returns:
        생성된 hourly 레코드 수

    Usage:
        docker exec exchange-rate-app python3 -c "
            from app.admin.dxy_rollup import backfill_hourly_range
            n = backfill_hourly_range('2026-03-10 16:00:00', '2026-03-13 04:00:00')
            print(f'Created {n} hourly records')
        "
    """
    start = datetime.strptime(start_utc_str, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    end = datetime.strptime(end_utc_str, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )

    # 정시 경계로 정렬
    bucket = start.replace(minute=0, second=0, microsecond=0)
    if bucket < start:
        bucket += timedelta(hours=1)

    created = 0

    with get_db_context() as db:
        while bucket < end:
            bucket_end = bucket + timedelta(hours=1)
            b_start_str = bucket.strftime("%Y-%m-%d %H:%M:%S")
            b_end_str = bucket_end.strftime("%Y-%m-%d %H:%M:%S")

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
                {"start": b_start_str, "end": b_end_str},
            ).fetchone()

            if row:
                close_rate = round(float(row[0]), 3)
                actual_source = row[1]
                record_ts = bucket.replace(tzinfo=None)

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
                created += 1
                logger.debug(
                    f"  backfill hourly: {bucket.strftime('%Y-%m-%d %H:%M')} UTC, "
                    f"source={actual_source}, close={close_rate}"
                )

            bucket += timedelta(hours=1)

        db.commit()

    logger.info(f"✅ DXY hourly backfill: {created} records created ({start_utc_str} ~ {end_utc_str})")
    return created


def backfill_daily_range(start_kst_date_str: str, end_kst_date_str: str) -> int:
    """
    지정 KST 날짜 구간의 hourly/realtime DXY → daily 일괄 생성.

    Args:
        start_kst_date_str: 시작 날짜 (KST, "2026-03-11") — 포함
        end_kst_date_str:   종료 날짜 (KST, "2026-03-13") — 배타 상한

    Returns:
        생성된 daily 레코드 수

    Usage:
        docker exec exchange-rate-app python3 -c "
            from app.admin.dxy_rollup import backfill_daily_range
            n = backfill_daily_range('2026-03-11', '2026-03-13')
            print(f'Created {n} daily records')
        "
    """
    current_date = datetime.strptime(start_kst_date_str, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_kst_date_str, "%Y-%m-%d").date()
    created = 0

    with get_db_context() as db:
        while current_date < end_date:
            day_start_kst = datetime(
                current_date.year,
                current_date.month,
                current_date.day,
                tzinfo=KST,
            )
            day_end_kst = day_start_kst + timedelta(days=1)

            start_str = day_start_kst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_str = day_end_kst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            record_ts = datetime(
                current_date.year,
                current_date.month,
                current_date.day,
            )

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

            if row:
                close_rate = round(float(row[0]), 3)
                actual_source = row[1]

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
                created += 1
                logger.debug(
                    f"  backfill daily: {current_date.isoformat()} KST, "
                    f"source={actual_source}, close={close_rate}"
                )

            current_date += timedelta(days=1)

        db.commit()

    logger.info(
        f"✅ DXY daily backfill: {created} records created "
        f"({start_kst_date_str} ~ {end_kst_date_str}, KST)"
    )
    return created
