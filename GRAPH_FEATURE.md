# 24시간 환율 그래프 기능 - 구현 명세서

> **작성일**: 2025-11-29
> **버전**: 2.0 (AI 검토 반영)
> **목적**: 실시간 환율 서비스에 24시간 환율 변화 그래프 추가

---

## 📋 목차

1. [프로젝트 배경](#1-프로젝트-배경)
2. [기능 요구사항](#2-기능-요구사항)
3. [기술 사양](#3-기술-사양)
4. [API 설계](#4-api-설계)
5. [데이터베이스 쿼리](#5-데이터베이스-쿼리)
6. [백엔드 구현](#6-백엔드-구현)
7. [프론트엔드 구현](#7-프론트엔드-구현)
8. [구현 계획](#8-구현-계획)
9. [성능 예측](#9-성능-예측)
10. [검토 포인트](#10-검토-포인트)
11. [AI 검토 결과 및 해결책](#11-ai-검토-결과-및-해결책)

---

## 1. 프로젝트 배경

### 현재 시스템

- **서비스**: 실시간 은행 간 환율 비교 (Investing.com + 9개 은행)
- **기술 스택**: Python FastAPI, SQLite, Redis, WebSocket
- **인프라**: AWS EC2 t2.micro (1 vCPU, 1GB RAM), Ubuntu 24.04 LTS (x86_64)
- **서버 타임존**: UTC (AWS EC2 기본 설정)
- **사용자**: 200-500명 (iOS/Android 네이티브 앱)
- **실시간성**: WebSocket 매분 6회 브로드캐스트 (00, 10, 20, 30, 40, 50초)
- **제약**: 대역폭 민감 (모바일 데이터 요금 고려)

### 기존 데이터 정책

- **Investing 데이터**: 장기 보관 (`investing_exchange_rates` 테이블)
- **은행 데이터**: 10일분 유지 (`bank_exchange_rates` 테이블)
- **저장 조건**: 변경사항 있을 때만 INSERT
- **타임스탬프**: KST 문자열로 저장 (예: "2025-11-29 15:30:00+09:00")

### 해결하려는 문제

**현재 상황**: 최신 환율만 제공, 시간에 따른 전체적인 환율 변화를 알 수 없음

**목표**: 24시간 환율 변화 그래프 추가로 대략적인 환율 흐름 파악 가능

---

## 2. 기능 요구사항

### 핵심 요구사항

#### ✅ 그래프 대상
- **3개 소스만 표시**: Investing.com, KB국민은행, 하나은행
- **1개 차트에 3개 라인 동시 표시**: 비교 가능하도록

#### ✅ 데이터 범위
- **시간**: 24시간 (현재 시점 기준 과거 24시간)
- **통화**: USD-KRW, JPY-KRW, EUR-KRW (3개)

#### ✅ 데이터 처리
- **이상치 감지**: 하지 않음 (모든 데이터 표시)
  - NDF 환율 (KB/하나 08:20-08:30) 포함
  - Investing 급변동 포함
  - 모든 데이터를 있는 그대로 표시
- **현재 환율**: 그래프 API에 포함하지 않음 (이미 WebSocket으로 서비스 중)
- **계산 방식**: 시간 윈도우별 **단순 평균**만 사용

#### ✅ 실시간 표시
- **Pulsing Dot**: 5분 내 환율 변화가 있었을 때만 표시
- **판단 기준**: WebSocket의 `timestamp` 사용 (그래프 API의 별도 필드 불필요)

### 선택한 이유

| 소스 | 선택 이유 |
|------|----------|
| **Investing** | 기준 환율, 가장 많이 확인, 24시간 운영 (월 06:00 ~ 토 06:00) |
| **KB국민은행** | 국내 최대 은행, 높은 신뢰도, 빈번한 환율 고시 |
| **하나은행** | 빈번한 환율 고시, 장시간 운영 (08:30 ~ 익일 06:00) |

---

## 3. 기술 사양

### 데이터 해상도 (Variable Resolution)

| 구간 | 시간 범위 | 해상도 | 계산 방식 | 포인트 수 |
|------|----------|--------|----------|----------|
| **최근** | 0 ~ 1시간 전 | 1분 | 해당 분의 평균 | **최대 60개** |
| **과거** | 1 ~ 24시간 전 | 10분 | 10분 윈도우 평균 | **최대 138개** |
| **총합** | 24시간 | - | - | **최대 198개/소스** |

**전체 데이터 포인트**: 최대 198 × 3소스 × 3통화 = **최대 1,782개**

> **Note**: 데이터가 없는 시간대(고시 중단 등)는 포인트가 생성되지 않으므로 실제 포인트 수는 이보다 적을 수 있습니다.

### 데이터 구조

**포인트 형식**: `[timestamp, avg_rate]`
- `timestamp`: Unix timestamp (초 단위)
- `avg_rate`: 소수점 2자리 환율 평균

**예시**:
```json
[
  [1732854000, 1340.5],
  [1732854060, 1340.7],
  [1732854120, 1340.9]
]
```

### Timestamp 형식 정책

**설계 원칙**: 성능 최적화 우선 (모바일 대역폭 고려)

| 항목 | 형식 | 크기 | 이유 |
|------|------|------|------|
| **데이터 포인트** (recent, day) | Unix timestamp (초) | 10 bytes | 크기 최소화 (~26KB 절약), Chart.js 최적 |
| **메타데이터** (as_of) | ISO 8601 | 25 bytes | 가독성, 기존 WebSocket API 호환 |

**성능 영향**:
- Unix timestamp: 1,782개 × 10 bytes = **~18KB**
- ISO 8601 대체 시: 1,782개 × 25 bytes = **~44KB**
- **절약량**: 26KB (59% 절감) ✅

**업계 표준 참고**:
- Coinbase, Binance 등 주요 거래소도 차트 데이터는 Unix timestamp 사용
- 메타데이터만 ISO 8601로 가독성 확보하는 패턴

**프론트엔드 구현**:
```javascript
// Chart.js는 밀리초 단위 사용 (1회 변환)
const toChartFormat = (points) =>
  points.map(([ts, rate]) => ({ x: ts * 1000, y: rate }));
```

**기존 시스템 호환성**:
- 현재 WebSocket API: ISO 8601 사용
- 그래프 API: 데이터는 Unix, 메타데이터는 ISO 8601 (혼용)
- 클라이언트는 두 형식 모두 처리 필요

### 성능 목표

| 지표 | 목표 |
|------|------|
| **API 응답 시간** | < 100ms (Redis 캐시) |
| **Payload 크기** | ~6-15KB (Gzip 압축, 실측 필요) |
| **Redis 캐시 히트율** | > 95% |
| **서버 부하 증가** | < 10% (백그라운드 워커) |
| **WebSocket 부하** | 0% 증가 (기존 유지) |

---

## 4. API 설계

### 엔드포인트

```http
GET /api/graph/{currency}
```

**Path Parameters**:
- `currency`: `"usd-krw"` | `"jpy-krw"` | `"eur-krw"`

**Response** (200 OK):
```json
{
  "pair": "usd-krw",
  "as_of": "2025-11-29T14:59:45+09:00",
  "sources": {
    "investing": {
      "recent": [
        [1732854000, 1340.5],
        [1732854060, 1340.7],
        ...
      ],
      "day": [
        [1732800000, 1338.9],
        [1732800600, 1339.2],
        ...
      ]
    },
    "kb": {
      "recent": [...],
      "day": [...]
    },
    "hana": {
      "recent": [...],
      "day": [...]
    }
  }
}
```

**Response 필드 설명**:
- `pair`: 통화쌍 (예: "usd-krw")
- `as_of`: **실제 데이터의 최신 시간** (ISO 8601 형식)
  - 형식: `"2025-11-29T14:59:45+09:00"` (타임존 포함)
  - 계산: `max(all sources' latest timestamp)` → ISO 8601 변환
  - 목적: 캐시가 오래되어도 정확한 데이터 시간 표시
- `sources[source].recent`: 최근 1시간 데이터 (1분 평균)
  - 형식: `[[Unix timestamp (초), rate], ...]`
  - 예시: `[[1732854000, 1340.5], [1732854060, 1340.7]]`
- `sources[source].day`: 1-24시간 데이터 (10분 평균)
  - 형식: `[[Unix timestamp (초), rate], ...]`
  - 예시: `[[1732800000, 1338.9], [1732800600, 1339.2]]`

**Timestamp 형식 주의사항**:
- ⚠️ **데이터 포인트는 Unix timestamp (초 단위)** - Chart.js 사용 시 `× 1000` 변환 필요
- ✅ **as_of는 ISO 8601** - 기존 WebSocket API와 동일한 형식

**Error Responses**:
- `400 Bad Request`: 잘못된 통화 코드
- `503 Service Unavailable`: 캐시 미스 + Rate Limiting

### 캐싱 전략

**Redis 키**: `graph:{currency}`
- 예: `graph:usd-krw`, `graph:jpy-krw`, `graph:eur-krw`

**데이터 타입**: String (JSON 덤프)

**저장 구조**:
```json
{
  "data": {
    "investing": {"recent": [...], "day": [...]},
    "kb": {...},
    "hana": {...}
  },
  "data_timestamp": 1732857540,
  "cached_at": 1732857543
}
```

**TTL**: 120초 (2분, Fail-safe용)

**갱신 주기**: 매분 03초 (백그라운드 워커)

**크기 예측**:
- 1개 통화: ~12KB (JSON) → ~6-10KB (Gzip, 실측 필요)
- 3개 통화 전체: ~36KB → ~18-30KB (Redis 메모리 증가 미미)

---

## 5. 데이터베이스 쿼리

### 테이블 구조 (기존 유지)

```sql
-- Investing 데이터
CREATE TABLE investing_exchange_rates (
    id INTEGER PRIMARY KEY,
    currency TEXT,
    rate REAL,
    timestamp DATETIME,
    INDEX(currency, timestamp)
);

-- 은행 데이터
CREATE TABLE bank_exchange_rates (
    id INTEGER PRIMARY KEY,
    bank TEXT,
    currency TEXT,
    rate REAL,
    timestamp DATETIME,
    INDEX(bank, currency, timestamp)
);
```

### 시간대 처리 (중요!)

**문제**: AWS EC2는 UTC로 설정되어 있지만, 데이터는 KST로 저장됨

**해결**: SQLite 쿼리에서 UTC 기준 `datetime('now')`에 시간 오프셋 추가

```sql
-- UTC → KST 변환
datetime('now', '+9 hours')  -- 현재 KST 시간

-- 1시간 전 (KST 기준)
datetime('now', '+9 hours', '-1 hour')
= datetime('now', '+8 hours')  -- 간결한 표현

-- 24시간 전 (KST 기준)
datetime('now', '+9 hours', '-24 hours')
= datetime('now', '-15 hours')
```

### 쿼리 1: 최근 1시간 (1분 평균)

```sql
-- Investing용
SELECT
    strftime('%s', strftime('%Y-%m-%d %H:%M:00', timestamp)) as bucket_ts,
    AVG(rate) as avg_rate
FROM investing_exchange_rates
WHERE timestamp >= datetime('now', '+8 hours')
  AND currency = :currency
GROUP BY bucket_ts
ORDER BY bucket_ts ASC;

-- 은행용 (KB/하나)
SELECT
    strftime('%s', strftime('%Y-%m-%d %H:%M:00', timestamp)) as bucket_ts,
    AVG(rate) as avg_rate
FROM bank_exchange_rates
WHERE timestamp >= datetime('now', '+8 hours')
  AND currency = :currency
  AND bank = :bank  -- 'kb' 또는 'hana'
GROUP BY bucket_ts
ORDER BY bucket_ts ASC;
```

### 쿼리 2: 1-24시간 (10분 평균)

```sql
-- Investing용
SELECT
    strftime('%s',
        strftime('%Y-%m-%d %H:', timestamp) ||
        printf('%02d:00', CAST(strftime('%M', timestamp) AS INTEGER) / 10 * 10)
    ) as bucket_ts,
    AVG(rate) as avg_rate
FROM investing_exchange_rates
WHERE timestamp >= datetime('now', '-15 hours')
  AND timestamp < datetime('now', '+8 hours')
  AND currency = :currency
GROUP BY bucket_ts
ORDER BY bucket_ts ASC;

-- 은행용 (KB/하나)
SELECT
    strftime('%s',
        strftime('%Y-%m-%d %H:', timestamp) ||
        printf('%02d:00', CAST(strftime('%M', timestamp) AS INTEGER) / 10 * 10)
    ) as bucket_ts,
    AVG(rate) as avg_rate
FROM bank_exchange_rates
WHERE timestamp >= datetime('now', '-15 hours')
  AND timestamp < datetime('now', '+8 hours')
  AND currency = :currency
  AND bank = :bank
GROUP BY bucket_ts
ORDER BY bucket_ts ASC;
```

**성능 최적화**:
- 기존 인덱스 활용: `(currency, timestamp)`, `(bank, currency, timestamp)`
- 별도 요약 테이블 불필요 (쿼리 속도 충분)
- 10일 데이터 내 24시간 조회는 빠름 (< 10ms)

---

## 6. 백엔드 구현

### 파일 구조

```
app/
├── database.py              # 수정 (Context Manager 추가)
├── admin/
│   ├── graph_cache.py      # 신규 (그래프 캐시 관리)
│   ├── log_reader.py        # 기존
│   ├── stats.py             # 기존
│   └── monitor.py           # 기존
├── main.py                  # 수정 (API 엔드포인트 + Fallback)
├── scheduler.py             # 수정 (백그라운드 워커 등록)
└── ...
```

### database.py (Context Manager 추가)

```python
# 기존 파일에 추가

from contextlib import contextmanager

@contextmanager
def get_db_context():
    """
    DB 세션 Context Manager (연결 누수 방지)

    Usage:
        with get_db_context() as db:
            result = db.execute(query)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

### graph_cache.py (신규 파일)

```python
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
    from app.config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD

    try:
        redis_client = sync_redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
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
```

### main.py (계층적 Fallback 전략)

```python
# 기존 파일에 추가

import time
from fastapi import HTTPException
from datetime import datetime, timedelta, timezone
import json

# 인메모리 캐시 (Tier 2)
_memory_cache = {}
_cache_timestamps = {}
_db_query_timestamps = {}

KST = timezone(timedelta(hours=9))


@app.get("/api/graph/{currency}")
async def get_graph_data(currency: str):
    """
    24시간 그래프 데이터 반환 (계층적 Fallback)

    Fallback 순서:
    1. Redis 캐시 (120초 TTL)
    2. 인메모리 캐시 (60초 TTL)
    3. DB 조회 (Rate Limiting: 10초에 1번)
    4. 503 Service Unavailable
    """
    if currency not in ["usd-krw", "jpy-krw", "eur-krw"]:
        raise HTTPException(status_code=400, detail="Invalid currency pair")

    now = time.time()

    # Tier 1: Redis 캐시
    redis = await get_redis_client()
    cache_key = f"graph:{currency}"

    if redis:
        try:
            cached = await redis.get(cache_key)
            if cached:
                cache_obj = json.loads(cached)

                logger.info(
                    f"✅ Redis 캐시 히트",
                    extra={"currency": currency, "tier": "redis"}
                )

                return {
                    "pair": currency,
                    "as_of": datetime.fromtimestamp(
                        cache_obj["data_timestamp"],
                        tz=KST
                    ).isoformat(),
                    "sources": cache_obj["data"]
                }
        except Exception as e:
            logger.warning(f"Redis 조회 실패: {e}")

    # Tier 2: 인메모리 캐시 (60초 TTL)
    if currency in _memory_cache:
        cache_age = now - _cache_timestamps.get(currency, 0)

        if cache_age < 60:
            logger.info(
                f"✅ 메모리 캐시 히트",
                extra={"currency": currency, "cache_age": cache_age, "tier": "memory"}
            )

            response = _memory_cache[currency].copy()
            return response
        else:
            del _memory_cache[currency]
            del _cache_timestamps[currency]

    # Tier 3: DB 직접 조회 (Rate Limiting)
    logger.warning(
        f"⚠️ 캐시 미스, DB 조회 시도",
        extra={"currency": currency, "tier": "database"}
    )

    last_db_query = _db_query_timestamps.get(currency, 0)
    time_since_last = now - last_db_query

    if time_since_last < 10:
        logger.error(
            f"❌ DB 조회 Rate Limit",
            extra={
                "currency": currency,
                "time_since_last": time_since_last,
                "retry_after": 10 - time_since_last
            }
        )

        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service temporarily unavailable",
                "reason": "Cache refresh in progress",
                "retry_after": int(10 - time_since_last)
            }
        )

    _db_query_timestamps[currency] = now

    try:
        from concurrent.futures import ThreadPoolExecutor
        import asyncio

        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_event_loop()

        def _fetch_graph():
            from app.admin.graph_cache import fetch_recent_1h, fetch_day_23h

            sources_data = {}
            max_timestamp = 0

            for source in ["investing", "kb", "hana"]:
                recent = fetch_recent_1h(source, currency)
                day = fetch_day_23h(source, currency)

                # 실제 데이터 최신 시간 추적 (recent + day 모두 확인)
                if recent and recent[-1][0] > max_timestamp:
                    max_timestamp = recent[-1][0]
                if day and day[-1][0] > max_timestamp:
                    max_timestamp = day[-1][0]

                sources_data[source] = {
                    "recent": recent,
                    "day": day
                }

            return sources_data, max_timestamp

        sources_data, max_timestamp = await loop.run_in_executor(executor, _fetch_graph)

        response = {
            "pair": currency,
            "as_of": datetime.fromtimestamp(max_timestamp, tz=KST).isoformat(),
            "sources": sources_data
        }

        # 인메모리 캐시 저장
        _memory_cache[currency] = response.copy()
        _cache_timestamps[currency] = now

        # 메모리 캐시 크기 제한 (최대 3개)
        if len(_memory_cache) > 3:
            oldest_key = min(_cache_timestamps, key=_cache_timestamps.get)
            del _memory_cache[oldest_key]
            del _cache_timestamps[oldest_key]

        logger.info(
            f"✅ DB 조회 성공",
            extra={"currency": currency, "data_timestamp": max_timestamp}
        )

        return response

    except Exception as e:
        logger.exception(f"❌ DB 조회 실패", extra={"currency": currency})

        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service temporarily unavailable",
                "reason": "Database query failed"
            }
        )
```

### scheduler.py (AsyncIO 모드)

```python
# 기존 파일 수정

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.admin.graph_cache import refresh_graph_cache

# 스케줄러 초기화 (AsyncIO 모드)
scheduler = AsyncIOScheduler(timezone=KST)

# 그래프 캐시 갱신 (매분 03초)
scheduler.add_job(
    refresh_graph_cache,
    CronTrigger(second='3', timezone=KST),
    id="graph_cache_refresh",
    max_instances=1,
    coalesce=True
)

logger.info("✅ 그래프 캐시 갱신 스케줄 등록 (매분 03초)")
```

---

## 7. 프론트엔드 구현

### templates/admin.html (웹 관리자 페이지)

#### HTML 카드 추가

```html
<div class="card">
    <h3>📈 24시간 환율 그래프</h3>
    <select id="graph-currency">
        <option value="usd-krw">USD-KRW</option>
        <option value="jpy-krw">JPY-KRW</option>
        <option value="eur-krw">EUR-KRW</option>
    </select>
    <canvas id="rateChart"></canvas>
</div>

<style>
#rateChart {
    height: 400px !important;
    margin-top: 10px;
}
</style>
```

#### JavaScript: Chart.js 통합 (setInterval 방식)

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<script>
let rateChart = null;
let pulsingInterval = null;
let pulsingActive = false;

// Pulsing Dot 플러그인 (setInterval 방식)
const pulsingDotPlugin = {
    id: 'pulsingDot',
    afterDatasetsDraw(chart) {
        const ctx = chart.ctx;
        const now = Date.now();

        if (!window.latestRates) return;

        let hasRecentUpdate = false;

        chart.data.datasets.forEach((dataset, i) => {
            const source = dataset.label.toLowerCase();
            const latestRate = window.latestRates.find(r => r.bank === source);

            if (!latestRate) return;

            const lastUpdate = new Date(latestRate.timestamp).getTime();
            if (now - lastUpdate > 300000) return;  // 5분 초과 스킵

            hasRecentUpdate = true;

            const meta = chart.getDatasetMeta(i);
            const lastPoint = meta.data[meta.data.length - 1];
            if (!lastPoint) return;

            const x = lastPoint.x;
            const y = lastPoint.y;

            // Pulsing 크기 계산
            const pulse = Math.sin(now / 200) * 2 + 6;

            ctx.save();
            ctx.beginPath();
            ctx.arc(x, y, pulse, 0, Math.PI * 2);
            ctx.fillStyle = dataset.borderColor + '40';
            ctx.fill();

            ctx.beginPath();
            ctx.arc(x, y, 4, 0, Math.PI * 2);
            ctx.fillStyle = dataset.borderColor;
            ctx.fill();
            ctx.restore();
        });

        // 5분 내 변화가 있으면 애니메이션 시작
        if (hasRecentUpdate && !pulsingActive) {
            startPulsing(chart);
        } else if (!hasRecentUpdate && pulsingActive) {
            stopPulsing();
        }
    }
};

function startPulsing(chart) {
    if (pulsingInterval) return;

    pulsingActive = true;

    // 200ms(5fps)마다 차트 업데이트
    pulsingInterval = setInterval(() => {
        if (chart) {
            chart.update('none');
        }
    }, 200);
}

function stopPulsing() {
    if (pulsingInterval) {
        clearInterval(pulsingInterval);
        pulsingInterval = null;
        pulsingActive = false;
    }
}

// 페이지 떠날 때 정리
window.addEventListener('beforeunload', stopPulsing);

// 그래프 로드 함수
async function loadGraph(currency) {
    const res = await fetch(`/api/graph/${currency}`);
    const data = await res.json();

    const datasets = Object.entries(data.sources).map(([source, sourceData]) => {
        const points = [
            ...sourceData.day.map(([ts, rate]) => ({ x: ts * 1000, y: rate })),
            ...sourceData.recent.map(([ts, rate]) => ({ x: ts * 1000, y: rate }))
        ];

        const colors = {
            investing: 'rgb(75, 192, 192)',
            kb: 'rgb(255, 99, 132)',
            hana: 'rgb(54, 162, 235)'
        };

        return {
            label: source.toUpperCase(),
            data: points,
            borderColor: colors[source],
            backgroundColor: colors[source] + '20',
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.1
        };
    });

    if (rateChart) {
        rateChart.destroy();
    }

    const ctx = document.getElementById('rateChart').getContext('2d');
    rateChart = new Chart(ctx, {
        type: 'line',
        data: { datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    position: 'top'
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return `${context.dataset.label}: ${context.parsed.y.toFixed(2)}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    type: 'time',
                    time: {
                        unit: 'hour',
                        displayFormats: {
                            hour: 'HH:mm'
                        }
                    },
                    title: {
                        display: true,
                        text: '시간 (KST)'
                    }
                },
                y: {
                    title: {
                        display: true,
                        text: '환율 (KRW)'
                    },
                    ticks: {
                        callback: function(value) {
                            return value.toFixed(2);
                        }
                    }
                }
            }
        },
        plugins: [pulsingDotPlugin]
    });
}

// 통화 변경 이벤트
document.getElementById('graph-currency').addEventListener('change', (e) => {
    loadGraph(e.target.value);
});

// WebSocket 실시간 업데이트 (안전성 강화)
function setupGraphWebSocket() {
    if (!window.ws) {
        console.warn('⚠️ WebSocket not initialized yet, retrying in 1s...');
        setTimeout(setupGraphWebSocket, 1000);
        return;
    }

    const originalOnMessage = window.ws.onmessage;

    window.ws.onmessage = function(event) {
        try {
            // 기존 핸들러 호출
            if (originalOnMessage && typeof originalOnMessage === 'function') {
                originalOnMessage.call(this, event);
            }

            const update = JSON.parse(event.data);
            window.latestRates = update.rates;

            if (!rateChart) return;

            const now = Date.now();

            update.rates.forEach(rate => {
                const source = rate.bank;
                const dataset = rateChart.data.datasets.find(d =>
                    d.label.toLowerCase() === source
                );

                if (dataset) {
                    dataset.data.push({
                        x: now,
                        y: rate.rate
                    });

                    if (dataset.data.length > 200) {
                        dataset.data.shift();
                    }
                }
            });

            // Pulsing 중이 아니면 수동 업데이트
            if (!pulsingActive) {
                rateChart.update('none');
            }

        } catch (error) {
            console.error('❌ WebSocket 메시지 처리 실패:', error);
        }
    };

    console.log('✅ 그래프 WebSocket 핸들러 등록 완료');
}

// DOMContentLoaded 후 실행
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupGraphWebSocket);
} else {
    setupGraphWebSocket();
}

// 초기 로드
loadGraph('usd-krw');

// 백그라운드 복귀 시 새로고침
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && rateChart) {
        const currentCurrency = document.getElementById('graph-currency').value;
        loadGraph(currentCurrency);
    }
});
</script>
```

---

## 8. 구현 계획

### Phase 1A: 즉시 수정 (필수, 1일)

#### Step 1A.1: 시간대 문제 수정 + max_timestamp 버그 수정 (0.3일)
**파일**: `app/admin/graph_cache.py`

**작업 내용**:
- `datetime('now', '+8 hours')` 사용 (1시간 전)
- `datetime('now', '-15 hours')` 사용 (24시간 전)
- `max_timestamp` 계산 시 recent + day 모두 확인

**검증 방법**:
```python
# 테스트 1: Timezone 계산
# 현재 시간이 KST 15:00라면
# datetime('now', '+8 hours') = 14:00 KST (1시간 전) ✅

# 테스트 2: max_timestamp 계산 (야간 시간대)
# 새벽 2시: recent=[], day=[...] → max_timestamp = day의 최신 시간 ✅
# as_of가 "1970-01-01"이 아닌 실제 데이터 시간이어야 함
```

#### Step 1A.2: DB 연결 누수 방지 (0.3일)
**파일**: `app/database.py`, `app/admin/graph_cache.py`

**작업 내용**:
- `get_db_context()` Context Manager 추가
- `with get_db_context() as db:` 사용

**검증 방법**:
```python
# 테스트: 100번 실행 후 DB 연결 개수 확인
for i in range(100):
    fetch_recent_1h("investing", "usd-krw")

# 연결 누수 없어야 함
```

#### Step 1A.3: 동기 함수로 변경 (0.4일)
**파일**: `app/admin/graph_cache.py`

**작업 내용**:
- `async def` → `def` 변경 (SQLite 호환)
- Redis 동기 클라이언트 사용
- AsyncIOScheduler의 기본 executor에서 실행

**검증 방법**:
- 로그 확인: 매분 03초 갱신 확인
- 이벤트 루프 블로킹 없는지 확인

**Note**: scheduler.py는 AsyncIOScheduler 유지 (동기 함수도 실행 가능)

---

### Phase 1B: 강력 권장 (2일)

#### Step 1B.1: Redis 캐시 신선도 개선 (0.5일)
**파일**: `app/admin/graph_cache.py`, `app/main.py`

**작업 내용**:
- `data_timestamp`, `cached_at` 메타데이터 추가
- `as_of`를 실제 데이터 시간으로 변경
- TTL 120초로 축소

**검증 방법**:
```bash
# Redis에서 캐시 확인
redis-cli
> GET graph:usd-krw
# data_timestamp, cached_at 필드 존재 확인
```

#### Step 1B.2: Pulsing Dot setInterval 방식 (0.5일)
**파일**: `templates/admin.html`

**작업 내용**:
- `setInterval 200ms` 사용
- `startPulsing()`, `stopPulsing()` 함수 구현

**검증 방법**:
- 브라우저 개발자 도구: CPU 사용량 < 5%
- Pulsing Dot 5분 내 변화 시만 표시

#### Step 1B.3: 계층적 Fallback 전략 (1일)
**파일**: `app/main.py`

**작업 내용**:
- Tier 1: Redis
- Tier 2: 인메모리 캐시 (60초)
- Tier 3: DB 조회 (Rate Limiting 10초)
- Tier 4: 503 에러

**검증 방법**:
```bash
# Redis 중지 후 테스트
systemctl stop redis

# 10초 이내 2번 요청
curl http://localhost:8000/api/graph/usd-krw  # 1번 → DB 조회
curl http://localhost:8000/api/graph/usd-krw  # 2번 → 503 에러 (Rate Limit)
```

---

### Phase 1C: 안전성 개선 (0.5일)

#### Step 1C.1: WebSocket 핸들러 안전성 (0.2일)
**파일**: `templates/admin.html`

**작업 내용**:
- `setupGraphWebSocket()` 함수
- null 체크 추가
- try-catch 에러 핸들링

#### Step 1C.2: 30초 새로고침 제거 (0.1일)
**파일**: `templates/admin.html`

**작업 내용**:
- `setInterval 30초` 제거
- `visibilitychange` 이벤트로 대체

#### Step 1C.3: 문서 업데이트 (0.2일)
**파일**: `GRAPH_FEATURE.md`

**작업 내용**:
- "최대 60개", "최대 138개" 명시
- AI 검토 결과 섹션 추가

---

**총 소요**: 3.5일 (Phase 1A → 1B → 1C 순차 진행)

---

## 9. 성능 예측

### 데이터 크기

| 항목 | 크기 |
|------|------|
| **1개 통화 JSON** | ~12KB (초기 예측) |
| **1개 통화 Gzip** | ~6-10KB (실측 필요) |
| **3개 통화 전체 (Redis)** | ~36KB → ~18-30KB (Gzip) |
| **단일 API 응답** | ~6-10KB (Gzip) |

**크기 최적화 전략**:
- ✅ **Unix timestamp 사용** (vs ISO 8601): ~26KB 절약 (59% 절감)
- ✅ **배열 형식** (vs 객체): ~5KB 추가 절약
- ✅ **Gzip 압축**: ~50% 추가 절감

**크기 비교 (ISO 8601 사용 시)**:
- Unix timestamp: ~12KB (현재)
- ISO 8601: ~38KB (+217%)
- **선택 근거**: t2.micro + 모바일 대역폭 고려 시 성능 우선

> **Note**: Codex 검토에서 실제 크기가 10-15KB일 수 있다고 지적. Phase 1 구현 후 실측 필요.

### 서버 부하

| 지표 | 현재 | 그래프 추가 후 | 변화 |
|------|------|---------------|------|
| **초기 로딩** | 3.6KB | 3.6KB + 6-10KB = **9.6-13.6KB** | +167-278% (1회만) |
| **WebSocket 주기** | 매분 6회 | 매분 6회 | **0%** ✅ |
| **Redis 메모리** | ~1MB | ~1.02MB | +2% |
| **DB 쿼리** | 매분 6회 | 매분 6회 + 18회 (그래프) | +300% (하지만 캐시됨) |
| **서버 CPU** | ~10% | ~12% | +20% (백그라운드 워커) |

### 병목 지점 분석

**예상 병목**: 없음 (계층적 Fallback으로 완화)

**최악 시나리오** (Redis + 인메모리 캐시 미스):
- DB 직접 조회: ~900ms (SQLite 18회 쿼리)
- Rate Limiting으로 10초에 1번만 허용 → 서버 보호 ✅

---

## 10. 검토 포인트

### 아키텍처 검토

**질문 1**: 데이터 해상도가 적절한가?
- 최근 1시간 1분 / 과거 23시간 10분
- 총 최대 198 포인트/소스
- 대안: 최근 2시간 1분 / 과거 22시간 15분?

**질문 2**: Redis 캐싱 전략이 최선인가?
- 현재: 매분 03초 갱신 (1분 주기), TTL 120초
- 대안: 30초 주기로 더 자주 갱신?

**질문 3**: 3개 소스만으로 충분한가?
- 현재: investing, kb, hana
- 대안: 신한, 우리 등 추가?

### 성능 검토

**질문 4**: 예상 크기가 정확한가?
- 계산: 198 × 3 × 2 × 10 bytes ≈ 12KB
- **AI 검토**: 실제로는 10-15KB일 수 있음
- **액션**: Phase 1 구현 후 실측 필요

**질문 5**: t2.micro에서 안정적으로 운영 가능한가?
- 추가 CPU: ~20%
- 추가 메모리: ~30KB (Redis)
- **AI 검토**: 계층적 Fallback으로 안전성 확보

### 사용성 검토

**질문 6**: Pulsing Dot 조건이 적절한가?
- 현재: 5분 내 변화 시만 표시, 200ms(5fps) 애니메이션
- 대안: 3분? 10분?

**질문 7**: 웹 그래프가 모바일 앱 전에 충분한가?
- 현재: Phase 1-2만 우선 구현
- 사용자 피드백 수집 후 모바일 결정

---

## 11. AI 검토 결과 및 해결책

### 검토 AI: Codex, Opus 4.5, Gemini

### 🔴 High (즉시 수정)

| 문제 | Codex 지적 | 해결책 | 반영 섹션 |
|------|-----------|--------|----------|
| **시간대 9시간 차이** | `datetime('now')` = UTC, 데이터는 KST | `datetime('now', '+8 hours')` 사용 | 섹션 5, 6 |
| **DB 연결 누수** | `next(get_db())` 후 close 안 함 | Context Manager 사용 | 섹션 6 |
| **Async/Sync 불일치** | async 함수 안에서 동기 DB 호출 | async 제거, 동기 함수로 변경 | 섹션 6 |
| **max_timestamp 계산 버그** | recent만 체크, 야간/주말 시 max_timestamp=0 → as_of="1970-01-01" | recent + day 모두 확인하여 최대값 계산 | 섹션 6 |

### 🟠 Medium (강력 권장)

| 문제 | AI 지적 | 해결책 | 반영 섹션 |
|------|--------|--------|----------|
| **데이터 크기 과소평가** | Codex: 실제 10-15KB 예상 | Phase 1 실측 후 확인 | 섹션 9 |
| **Redis 캐시 신선도** | Codex: TTL 5분, 갱신 1분 → 최대 5분 지연 | as_of를 실제 데이터 시간으로 변경, TTL 120초 | 섹션 4, 6 |
| **Pulsing Dot 성능** | Codex, Opus: 무한 루프로 CPU 100% | setInterval 200ms 방식 (5fps) | 섹션 7 |
| **Fallback 전략** | Gemini: Redis 미스 시 DB 부하 우려 | 계층적 Fallback + Rate Limiting | 섹션 6 |

### 🟢 Low (안전성 개선)

| 문제 | AI 지적 | 해결책 | 반영 섹션 |
|------|--------|--------|----------|
| **WebSocket 핸들러** | Codex: null 체크 없음 | setupGraphWebSocket() 함수로 안전성 강화 | 섹션 7 |
| **30초 새로고침** | Opus: WebSocket 있는데 불필요 | visibilitychange 이벤트로 대체 | 섹션 7 |
| **포인트 수 문서화** | Opus: 데이터 없으면 60개 미만 | "최대 60개", "최대 138개" 명시 | 섹션 3 |

---

## 부록 A: 기술 스택

| 계층 | 기술 |
|------|------|
| **백엔드** | Python 3.x, FastAPI |
| **DB** | SQLite (현재) → PostgreSQL (확장 시) |
| **캐시** | Redis (비동기 + 동기 클라이언트) |
| **스케줄러** | APScheduler (AsyncIOScheduler) |
| **프론트엔드** | Chart.js 4, Vanilla JS |
| **모바일** | iOS Charts, MPAndroidChart (Phase 3) |

---

## 부록 B: 참고 자료

### 현재 시스템 문서
- `CLAUDE.md`: 프로젝트 전체 가이드
- `CRAWLERS.md`: 크롤러 구현 세부사항
- `DECISIONS.md`: 아키텍처 의사결정 기록

### 관련 파일
- `app/scheduler.py`: 스케줄링 시스템
- `app/cache.py`: Redis 클라이언트
- `app/main.py`: FastAPI 애플리케이션
- `app/database.py`: DB 세션 관리
- `templates/admin.html`: 관리자 페이지

---

**문서 끝**

이 문서는 Codex, Opus 4.5, Gemini의 검토를 거쳐 모든 문제점을 해결한 최종 버전입니다. 세션이 끊기거나 다른 개발자에게 전달해도 완전한 구현이 가능합니다.
