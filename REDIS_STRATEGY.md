# Redis 캐시 도입 전략 (최종안)

> 📊 **목적**: 실전 Redis 도입 전략 (다른 AI 피드백 반영)
> 📅 **작성일**: 2025-11-26
> ✅ **검토**: 과장된 수치 제거, 보수적 설계, 단계별 명확화

---

## 1. 핵심 전략

### 원칙
1. **SQLite 단계**: WebSocket 브로드캐스트 캐시만 (크롤러 로직 변경 없음)
2. **PostgreSQL 단계**: 실측 후 필요 시 확장 (크롤러 비교, 토글)
3. **보수적 설계**: 추정치 의존 최소화, 안정성 우선

### Redis 키 설계 (수정)

```python
# app/config.py
REDIS_KEYS = {
    # 브로드캐스트 전용 (JSON 문자열)
    "broadcast_latest": "broadcast:latest",
    # TTL: 없음 (영속) ⭐ 주말/야간 캐시 미스 방지

    # 환율별 최신값 (Hash, Phase 2)
    "rate_hash": "rate:{pair}",  # 예: rate:usd-krw
    # TTL: 없음 또는 30일 ⭐ (긴 영속)

    # 크롤러 토글 (Phase 2)
    "crawler_enabled": "crawler:enabled",
    # TTL: 없음 (영속) + DB 백업 필수

    # 회로차단기 상태 (수정) ⭐
    "circuit_breaker": "circuit:redis",
    # 값: {"state": "open", "until": <epoch_ms>}
}
```

---

## 2. Phase 1: SQLite (현재)

### 2.1 WebSocket 브로드캐스트 캐시만 도입

#### Redis 설정

```yaml
# docker-compose.yml
services:
  redis:
    image: redis:7-alpine
    container_name: exchange_rate_redis
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
      - ./redis.conf:/usr/local/etc/redis/redis.conf
    command: redis-server /usr/local/etc/redis/redis.conf
    restart: unless-stopped

volumes:
  redis_data:
```

```conf
# redis.conf
maxmemory 100mb
maxmemory-policy allkeys-lru

# Persistence (AOF 권장)
appendonly yes
appendfsync everysec

# 보안
requirepass ${REDIS_PASSWORD}
```

#### 회로차단기 (타임스탬프 포함) ⭐

```python
# app/cache.py
import json
from datetime import datetime, timedelta
from typing import Optional

class RedisCircuitBreaker:
    """Redis 장애 시 과도한 재시도 방지 (영구 open 방지)"""

    def __init__(self, redis_client, failure_threshold: int = 5, timeout_seconds: int = 30):
        self.redis = redis_client
        self.failure_threshold = failure_threshold
        self.timeout = timedelta(seconds=timeout_seconds)
        self.failure_count = 0
        self.state = "closed"
        self.state_key = "circuit:redis"

    async def record_success(self):
        """성공 시 카운터 리셋 + Redis 상태 업데이트"""
        self.failure_count = 0
        self.state = "closed"

        # Redis에 상태 저장 (영구 open 방지)
        try:
            await self.redis.set(
                self.state_key,
                json.dumps({"state": "closed", "updated_at": datetime.now().timestamp()})
            )
        except:
            pass  # 회로차단기 자체는 Redis 없어도 동작

    async def record_failure(self):
        """실패 시 카운터 증가 + 타임스탬프 기록"""
        self.failure_count += 1

        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            until = datetime.now() + self.timeout

            # Redis에 상태 + until 저장
            try:
                await self.redis.set(
                    self.state_key,
                    json.dumps({
                        "state": "open",
                        "until": until.timestamp(),
                        "failures": self.failure_count
                    })
                )
            except:
                pass

            logger.warning(
                f"🔴 Redis 회로차단기 열림 ({self.timeout.seconds}초)",
                extra={"until": until.isoformat(), "failures": self.failure_count}
            )

    async def can_attempt(self) -> bool:
        """Redis 시도 가능 여부 (타임스탬프 기반)"""
        if self.state == "closed":
            return True

        # Redis에서 상태 조회 (영구 open 방지)
        try:
            state_data = await self.redis.get(self.state_key)
            if state_data:
                state = json.loads(state_data)

                # until 시간 경과 시 재시도
                if state.get("state") == "open":
                    until = datetime.fromtimestamp(state["until"])
                    if datetime.now() > until:
                        logger.info("🟡 Redis 회로차단기 재시도 (타임아웃 경과)")
                        self.state = "closed"
                        self.failure_count = 0
                        return True
                    else:
                        return False
        except:
            # Redis 조회 실패 시 로컬 상태 사용
            pass

        # 로컬 타임아웃 체크 (폴백)
        # ... (기존 로직)

        return False
```

#### WebSocket 초기 접속 (캐시 재사용)

```python
# app/main.py
from app.cache import redis_cache

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)

    try:
        # Redis에서 최신 브로드캐스트 JSON 조회
        cached_json = await redis_cache.get("broadcast:latest")

        if cached_json:
            await websocket.send_text(cached_json)
            logger.info("✅ Redis 캐시로 초기 데이터 전송")
        else:
            # 폴백: DB 조회
            rates = get_latest_rates_from_db()
            data = format_rates_json(rates)
            await websocket.send_text(data)

            # Redis에 캐시 (TTL 없음, 영속) ⭐
            await redis_cache.set("broadcast:latest", data)
            logger.info("⚠️ DB 폴백 → Redis 재적재")

        # 하트비트 루프
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        active_connections.remove(websocket)
```

#### Broadcasting (직렬화 중복 제거 + 항상 실행) ⭐

```python
# app/scheduler.py
async def broadcast_rates_once():
    """매분 00, 10, 20, 30, 40, 50초 - 접속자 여부 무관하게 항상 실행"""

    # 1. Redis에서 기존 캐시 조회
    cached_json = await redis_cache.get("broadcast:latest")

    # 2. DB에서 최신 환율 조회 (항상 실행) ⭐
    rates = get_latest_rates_from_db()
    new_json = format_rates_json(rates)

    # 3. 변경 감지
    if new_json != cached_json:
        # 4. Redis 업데이트 (TTL 없음, 접속자 여부 무관) ⭐
        await redis_cache.set("broadcast:latest", new_json)

        # 5. WebSocket 전송은 접속자 있을 때만 (최적화) ⭐
        if len(active_connections) > 0:
            await broadcast_to_clients(new_json)
            logger.info("📡 브로드캐스트 전송", extra={
                "clients": len(active_connections)
            })
        else:
            logger.debug("📡 Redis 업데이트 (접속자 없음, 전송 스킵)")
    else:
        logger.debug("브로드캐스트 스킵 (변경 없음)")
```

**핵심 개선**:
- ✅ Broadcasting 항상 실행 → **Redis 항상 최신 유지** (접속자 없어도)
- ✅ WebSocket 전송만 조건부 → 네트워크 절약
- ✅ 최초 접속자 오래된 캐시 받는 문제 방지 ⭐
- ✅ JSON 직렬화 1회만 수행
- ✅ TTL 없음 → 주말/야간 캐시 미스 방지

**엣지 케이스 대응**:
```
시나리오: 모든 사용자 접속 종료 → 환율 변동 → 최초 재접속

Before (위험):
- Broadcasting 스킵 → Redis 업데이트 안 됨
- 최초 접속 → 오래된 캐시 전송 ❌

After (안전):
- Broadcasting 항상 실행 → Redis 업데이트됨
- 최초 접속 → 최신 캐시 전송 ✅
```

#### 재시작 워밍업

```python
# app/main.py
@app.on_event("startup")
async def startup_event():
    await redis_cache.connect()

    # 워밍업: DB → Redis
    rates = get_latest_rates_from_db()
    broadcast_json = format_rates_json(rates)
    await redis_cache.set("broadcast:latest", broadcast_json)

    logger.info("✅ Redis 워밍업 완료")

    start_scheduler()
```

### 2.2 크롤러 토글: DB 테이블 (Phase 1)

**Phase 1에서는 Redis 사용 안 함** (단순성 우선)

```python
# app/models.py
class CrawlerConfig(Base):
    __tablename__ = "crawler_config"

    id = Column(Integer, primary_key=True)
    crawler_name = Column(String, unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
```

```python
# app/scheduler.py
class CrawlerManager:
    def __init__(self, db: Session):
        self.db = db
        self.config_cache: Dict[str, bool] = {}  # In-memory 캐시
        self.load_config()

    def is_enabled(self, crawler_name: str) -> bool:
        """In-memory 캐시에서 조회 (매우 빠름)"""
        return self.config_cache.get(crawler_name, True)

    def toggle_crawler(self, crawler_name: str, enabled: bool):
        """관리자 페이지에서 호출"""
        # DB 업데이트
        config = self.db.query(CrawlerConfig).filter_by(
            crawler_name=crawler_name
        ).first()
        config.enabled = enabled
        self.db.commit()

        # In-memory 캐시 업데이트
        self.config_cache[crawler_name] = enabled

        # APScheduler job pause/resume
        if enabled:
            scheduler.resume_job(f"crawl_{crawler_name}")
        else:
            scheduler.pause_job(f"crawl_{crawler_name}")
```

---

## 3. Phase 2: PostgreSQL RDS

### 3.1 실측 기반 확장 전략

**PostgreSQL 전환 후:**
1. **1-2주 모니터링** (RDS CloudWatch, pg_stat_statements)
2. **부하 테스트** (동시 접속 100명)
3. **실측 데이터 기반 판단**:
   - Connection Pool 압력 높으면 → 크롤러 환율 비교 Redis 도입
   - 크레딧 소진 패턴 불안정하면 → 전면 캐싱 확대
   - 안정적이면 → 현상 유지 (브로드캐스트만)

### 3.2 크롤러 환율 비교 Redis (선택)

**도입 조건**:
- RDS CPU > 10% 지속
- Connection Pool 부족 (5개 → 10개+ 필요)
- 쿼리 레이턴시 > 20ms

**구현** (Phase 1과 동일, `save_rate_if_changed` 수정):
```python
# app/crawlers/utils.py
async def save_rate_if_changed(bank: str, currency: str, new_rate: float) -> bool:
    hash_key = f"rate:{currency}"

    # Redis Hash에서 최신 환율 조회
    cached_rate_str = await redis_cache.hget(hash_key, bank)

    if cached_rate_str is None:
        # DB 폴백
        db_rate = get_latest_rate_from_db(bank, currency)
        cached_rate = db_rate.rate if db_rate else None

        if cached_rate:
            await redis_cache.hset(hash_key, bank, str(cached_rate))
    else:
        cached_rate = float(cached_rate_str)

    # 변경 감지
    if cached_rate is None or abs(new_rate - cached_rate) > 0.001:
        # DB 저장 (권위)
        save_to_db(bank, currency, new_rate)

        # Redis 업데이트 (TTL 없음) ⭐
        await redis_cache.hset(hash_key, bank, str(new_rate))

        return True

    return False
```

### 3.3 크롤러 토글 Redis Hash (Phase 2)

**멀티 인스턴스 대비:**
```python
class CrawlerManager:
    async def is_enabled(self, crawler_name: str) -> bool:
        """Redis에서 실시간 조회"""
        enabled_str = await redis_cache.hget("crawler:enabled", crawler_name)
        return enabled_str == "1" if enabled_str else True

    async def toggle_crawler(self, crawler_name: str, enabled: bool):
        # 1. Redis 업데이트
        await redis_cache.hset("crawler:enabled", crawler_name, "1" if enabled else "0")

        # 2. DB 백업 (영속성) ⭐
        config = self.db.query(CrawlerConfig).filter_by(crawler_name=crawler_name).first()
        config.enabled = enabled
        self.db.commit()

        # 3. APScheduler 제어
        if enabled:
            scheduler.resume_job(f"crawl_{crawler_name}")
        else:
            scheduler.pause_job(f"crawl_{crawler_name}")
```

### 3.4 정합성 검증 (권장값, 환경별 조정 가능)

```python
# app/config.py
class Settings(BaseSettings):
    # Redis 정합성 검증 설정
    REDIS_VALIDATION_ENABLED: bool = True
    REDIS_VALIDATION_INTERVAL: int = 10  # 분 (권장: 5-30)
    REDIS_VALIDATION_SAMPLE_SIZE: int = 5  # 개수 (권장: 3-10)

# app/scheduler.py
import random

async def validate_redis_cache_sampling():
    """Redis-DB 정합성 검증 (샘플링)

    설정:
    - 주기: REDIS_VALIDATION_INTERVAL (기본 10분)
    - 샘플: REDIS_VALIDATION_SAMPLE_SIZE (기본 5개)

    권장 범위:
    - 주기: 5-30분 (환경에 따라 조정)
    - 샘플: 3-10개 (전체 30개 중)

    트레이드오프:
    ┌─────────────┬──────────┬──────────┐
    │ 설정        │ 정확도   │ CPU 부하 │
    ├─────────────┼──────────┼──────────┤
    │ 5분 / 10개  │ 높음     │ 높음     │
    │ 10분 / 5개  │ 중간     │ 낮음 ⭐  │
    │ 30분 / 3개  │ 낮음     │ 매우 낮음│
    └─────────────┴──────────┴──────────┘

    t2.micro: 10분 / 5개 권장 (부하 최소화)
    t3.small 이상: 5분 / 10개 가능 (정확도 우선)
    """

    if not settings.REDIS_VALIDATION_ENABLED:
        return

    sample_size = settings.REDIS_VALIDATION_SAMPLE_SIZE
    all_pairs = [(c, b) for c in ["usd-krw", "jpy-krw", "eur-krw"] for b in BANKS]
    sample = random.sample(all_pairs, min(sample_size, len(all_pairs)))

    mismatches = []
    for currency, bank in sample:
        redis_rate = await redis_cache.hget(f"rate:{currency}", bank)
        db_rate = get_latest_rate_from_db(bank, currency)

        if redis_rate != str(db_rate.rate):
            # 불일치 시 Redis를 DB로 덮어쓰기
            await redis_cache.hset(f"rate:{currency}", bank, str(db_rate.rate))
            mismatches.append({"currency": currency, "bank": bank})

    if mismatches:
        logger.warning("⚠️ Redis-DB 불일치 (자동 수정)", extra={
            "count": len(mismatches),
            "sample_size": sample_size
        })

# 스케줄러 등록 (설정 기반)
if settings.REDIS_VALIDATION_ENABLED:
    scheduler.add_job(
        validate_redis_cache_sampling,
        CronTrigger(
            minute=f"*/{settings.REDIS_VALIDATION_INTERVAL}",
            timezone=KST
        ),
        id="validate_redis_sampling"
    )
```

---

## 4. Phase 정의 (IMPACT_ANALYSIS.md와 동일)

| Phase | 기능 | 도입 | 조건 | 비고 |
|-------|------|------|------|------|
| **Phase 1 (SQLite)** | | | | |
| | WebSocket 브로드캐스트 캐시 | ✅ 필수 | - | 동시 접속 대비 |
| | 재시작 워밍업 | ✅ 필수 | - | Redis 초기화 |
| | 회로차단기 | ✅ 필수 | - | Redis 장애 대응 |
| | 크롤러 토글 | ✅ 필수 | - | **DB + In-memory** |
| | 크롤러 환율 비교 | ❌ 보류 | - | 효과 미미 (0.3 qps) |
| **Phase 2 (PostgreSQL)** | | | | |
| | WebSocket 브로드캐스트 캐시 | ✅ 유지 | - | Phase 1에서 계속 |
| | 크롤러 토글 (Redis 전환) | ⚠️ 선택 | 멀티 인스턴스 시 | 단일 인스턴스면 DB 유지 |
| | 크롤러 환율 비교 | ⚠️ 선택 | RDS 부하 높으면 | 1-2주 실측 후 판단 |
| | 정합성 검증 (샘플링) | ⚠️ 선택 | 캐시 확장 시 | 크롤러 비교 도입 시 필요 |

### Phase 전환 결정 트리

```
PostgreSQL 전환 후 1-2주 모니터링:
├─ RDS CPU 평균 < Baseline 10% && Connection Pool 안정
│  └─ 현상 유지 (브로드캐스트 캐시만)
│
├─ RDS CPU 평균 > Baseline 10% || Connection Pool 부족
│  └─ 크롤러 환율 비교 Redis 도입
│
└─ 멀티 인스턴스 확장 계획
   └─ 크롤러 토글 Redis Hash 전환
```

### 실측 도구 (상세는 IMPACT_ANALYSIS.md 섹션 7 참고)

**PostgreSQL RDS:**
- **CloudWatch**: CPUUtilization, CPUCreditBalance, DatabaseConnections, FreeableMemory
- **pg_stat_statements**: 쿼리별 레이턴시 (평균/P95)
- **pg_stat_activity**: 실시간 연결 수, 활성 쿼리

**Redis:**
- **Redis PING**: 네트워크 RTT 측정
- **redis-benchmark**: 성능 벤치마크

**네트워크:**
- **EC2 → RDS ping**: VPC 내부 레이턴시

### 문서 유지 규칙

⚠️ **중요**: Phase 정의 변경 시 아래 두 문서를 **동시 업데이트** 필수
- `REDIS_IMPACT_ANALYSIS.md` (섹션 8.2)
- `REDIS_STRATEGY.md` (섹션 4)

불일치 발생 시 → `REDIS_IMPACT_ANALYSIS.md`를 권위 버전으로 간주

---

## 5. 구현 순서 (체크리스트)

### Phase 1.7: Redis 브로드캐스트 캐시 (2일)

- [ ] Docker Compose Redis 추가
- [ ] `app/cache.py` 회로차단기 (타임스탬프 포함)
- [ ] WebSocket 초기 접속 Redis 조회
- [ ] Broadcasting 직렬화 중복 제거
- [ ] 재시작 워밍업
- [ ] TTL 없음 (영속)
- [ ] 테스트: 동시 접속 50명

### Phase 1.8: 크롤러 토글 DB (1일)

- [ ] `crawler_config` 테이블
- [ ] `CrawlerManager` In-memory 캐시
- [ ] 관리자 페이지 UI
- [ ] API 엔드포인트

### Phase 2.0: PostgreSQL 전환 (1주)

- [ ] RDS 인스턴스 생성
- [ ] SQLite → PostgreSQL 마이그레이션
- [ ] Connection Pooling 설정

### Phase 2.1: 모니터링 & 판단 (1-2주)

- [ ] RDS CloudWatch 모니터링
- [ ] pg_stat_statements 프로파일링
- [ ] 부하 테스트
- [ ] **실측 데이터 기반 확장 여부 결정**

### Phase 2.2: 선택적 확장 (필요 시)

- [ ] 크롤러 환율 비교 Redis (조건부)
- [ ] 크롤러 토글 Redis Hash
- [ ] 정합성 검증 (10분 샘플링)

---

## 6. 핵심 개선사항 요약

| 항목 | 기존 제안 | 수정안 | 이유 |
|------|----------|--------|------|
| **TTL** | 60초 (짧음) | 없음 (영속) | 주말/야간 캐시 미스 방지 |
| **브로드캐스트** | DB 조회 → 직렬화 | Redis 기반 변경 감지 | 중복 직렬화 제거 |
| **회로차단기** | 상태만 저장 | 상태 + until 타임스탬프 | 영구 open 방지 |
| **크롤러 토글 (Phase 1)** | Redis Hash | DB + In-memory | 단순성 우선 |
| **정합성 검증** | 5분 전체 | 10분 샘플 5개 | t2.micro 부하 감소 |
| **Phase 1 범위** | 전면 도입 | 브로드캐스트만 | 점진적 확장 |
| **수치 표현** | "90% 감소" | "대부분 제거" | 과장 방지 |

---

## 7. 문서 업데이트 체크리스트

- [ ] `DECISIONS.md` - ADR-012 "Redis 캐시 도입 전략" (보수적 설계)
- [ ] `CLAUDE.md` - "기술 스택" Redis 추가, Phase별 전략
- [ ] `DOCKER.md` - Redis 서비스 설정
- [ ] `CHANGELOG.md` - Phase 1.7, 1.8 추가

---

**작성일**: 2025-11-26
**검토**: OpenAI Codex CLI 피드백 반영 완료
**다음 단계**: Phase 1.7 구현 시작
