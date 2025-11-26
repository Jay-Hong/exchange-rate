# Redis 캐시 도입 효과 분석 (PostgreSQL RDS)

> 📊 **목적**: PostgreSQL RDS 프리티어 환경에서 Redis 캐시 도입 시 예상 효과 분석
> 📅 **작성일**: 2025-11-26
> ⚠️ **주의**: 본 문서의 수치는 **추정치/가정**이며, 실제 측정 후 검증 필요
> 🔖 **결론 요약**: 동시 접속 시 DB 읽기 부하 대부분 제거, 크레딧 소진 완화, 장기 안정성 개선

---

## 1. 시나리오별 성능 비교

### 시나리오 A: 평상시 크롤러 실행 (IN 모드)

#### PostgreSQL만 사용 시 (추정)

**실제 크롤러 스케줄 기반 쿼리량:**
```
IN 모드 크롤러 실행 빈도 (1분 기준):
- investing: 6회 (10초마다)
- kb, hana: 각 3회 (20초마다)
- woori, bs, citi: 각 1회 (60초마다)
- shinhan, ibk, nh, sc: 각 1회 (60초마다)
총: 6 + 3×2 + 1×7 = 19회/분

각 크롤러 실행 시:
- 환율 비교 쿼리: 1회 (특정 bank, currency)
- 쿼리 시간: 3-8ms (추정, 네트워크 1-2ms + 쿼리 2-6ms)

평균 초당 쿼리: 19/60 = 약 0.3 qps
RDS CPU 사용률: 미미 (1% 미만, 추정)
```

**주의**: 위 수치는 실제 RDS 프로파일링 없이 스케줄만으로 계산한 추정치입니다.

#### Redis + PostgreSQL 사용 시 (추정)

**Redis 조회로 대체:**
```
동일한 0.3 qps를 Redis HGET으로 처리
Redis 쿼리 시간: 0.5-1.5ms (추정, 실측 필요)
  - Redis 처리: 0.1-0.5ms (추정)
  - 네트워크: 수백 μs ~ 수 ms (EC2 내부, 추정)

RDS 쿼리: 변경 감지 시에만 (추정 10-20회/일)
RDS CPU 사용률: 거의 없음 (추정, 실측 필요)
```

**절감 효과 (추정):**
| 지표 | PostgreSQL | Redis + PostgreSQL | 비고 |
|------|-----------|-------------------|------|
| 평균 쿼리 시간 | 3-8ms | 0.5-1.5ms | 2-5배 개선 (추정) |
| RDS 쿼리 빈도 | 0.3 qps | 0.001 qps | DB 읽기 대부분 제거 |
| RDS CPU | 미미 (1% 미만) | 거의 없음 | 실측 필요 |

**참고**: 크롤러 환율 비교는 RDS 부하의 주요 원인이 **아닙니다**. 주요 부하는 Broadcasting/WebSocket 초기 접속입니다.

---

### 시나리오 B: 동시 접속 100명 (재시작 직후)

#### PostgreSQL만 사용 시 (추정)

**WebSocket 초기 접속 쿼리:**
```sql
-- 전체 환율 조회 (app/crud.py)
SELECT DISTINCT ON (bank, currency)
  bank, currency, rate, timestamp
FROM bank_exchange_rates
ORDER BY bank, currency, timestamp DESC;

-- 반환 행: 10개 은행 × 3개 통화 = 30행
-- 쿼리 시간: 10-20ms (추정, 네트워크 1-2ms + 인덱스 스캔 8-18ms)
```

**100명 동시 접속 부하 (추정):**
```
Connection Pool: 5개 연결 (기본값)
순차 처리: 100명 / 5 = 20 라운드
각 라운드: 5명 × 15ms (평균) = 75ms
총 시간: 20 × 75ms = 1.5초 (최악)

RDS CPU 스파이크: 추정 불가 (실측 필요)
타임아웃 위험: Connection Pool 부족 시 발생 가능
```

**주의**: CPU 스파이크는 쿼리 수만으로 계산 불가 (플랜, 락, I/O 영향). 실제 측정 필요.

#### Redis 브로드캐스트 캐시 사용 시 (추정)

**Redis GET 조회:**
```
Redis GET "broadcast:latest" (JSON 문자열, ~3KB)
- 쿼리 시간: 0.5-1.5ms (추정, 실측 필요)
  - Redis 처리: 0.1-0.5ms (추정)
  - 네트워크: 수백 μs ~ 수 ms (EC2 내부, 추정)
- 100명 동시 처리: FastAPI 비동기로 병렬 처리

RDS 쿼리: 0 (캐시 히트)
RDS CPU: 거의 없음 (추정, 실측 필요)
타임아웃 위험: 거의 없음
```

**효과 (추정):**
| 지표 | PostgreSQL | Redis | 비고 |
|------|-----------|-------|------|
| 쿼리 시간 | 10-20ms | 0.5-1.5ms | 7-40배 빠름 (추정) |
| RDS 부하 | 100개 쿼리 | 0개 쿼리 | **DB 읽기 완전 제거** |
| Connection Pool 압력 | 높음 | 없음 | 안정성 개선 |

**핵심**: WebSocket 초기 접속은 **Redis 캐시가 가장 효과적인 시나리오**입니다.

---

## 1.5. Broadcasting 표준 흐름 (데이터 권위 및 캐시 정책)

### 데이터 권위 정의

```
DB (PostgreSQL/SQLite): 권위 소스 (Source of Truth)
- 모든 환율 변경사항 영구 저장
- 크롤러가 DB에 INSERT
- 장기 보관 (Investing 데이터)

Redis: 캐시 (Cache)
- 최신 JSON만 저장 (broadcast:latest)
- 변경 감지 기준 (이전 값과 비교)
- TTL 없음 (영속, 주말/야간 캐시 미스 방지)
```

### Broadcasting 6단계 흐름 (Phase 1, Phase 2 공통)

```python
async def broadcast_rates_once():
    """매분 00, 10, 20, 30, 40, 50초 실행"""

    # 1. Redis 캐시 조회 (기존 JSON)
    cached_json = await redis_cache.get("broadcast:latest")

    # 2. DB 조회 (권위 소스, 항상 실행)
    rates = get_latest_rates_from_db()

    # 3. JSON 직렬화
    new_json = format_rates_json(rates)

    # 4. 변경 감지 (Redis 기준 비교)
    if new_json != cached_json:
        # 5. Redis 업데이트 (접속자 여부 무관)
        await redis_cache.set("broadcast:latest", new_json)

        # 6. WebSocket 전송 (접속자 있을 때만)
        if len(active_connections) > 0:
            await broadcast_to_clients(new_json)
```

### 핵심 설계 원칙

- ✅ Broadcasting 항상 실행 → Redis 최신 유지
- ✅ 최초 접속자 오래된 캐시 받는 문제 방지
- ✅ JSON 직렬화 1회만 수행 (중복 제거)
- ✅ WebSocket 전송만 조건부 (네트워크 절약)

---

## 2. RDS 프리티어 제약사항 분석

### CPU 크레딧 시스템 (t3.micro)

**작동 방식:**
```
기본 성능 (Baseline): 10% CPU
버스트 성능: 100% CPU (크레딧 소진 시)

크레딧 축적: 10% 이하 사용 시 축적
크레딧 소진: 10% 초과 사용 시 소진
크레딧 고갈: Baseline 10%로 제한 → 심각한 성능 저하
```

### Redis 없이 PostgreSQL만 사용 시 (추정, 실측 필요)

**CPU 사용 패턴 (추정):**
```
⚠️ 주의: 아래 수치는 추정치이며, 실제로는 크게 다를 수 있음

평상시: 추정 불가 (CloudWatch 실측 필요)
피크 시간 (08:30-21:00): 추정 불가 (부하 테스트 필요)
  - 크롤러 부하: 미미 (0.3 qps)
  - WebSocket 접속: 주요 부하 (동시 접속 수에 따라 변동)
  - Broadcasting 쿼리: 매분 6회

크레딧 소진 속도: 사용 패턴에 따라 다름 (1-2주 모니터링 필요)
크레딧 고갈 가능성: 실측 후 판단
```

**크레딧 고갈 시 예상 영향 (일반론):**
```
RDS CPU Baseline 10%로 제한 시:
- 쿼리 레이턴시: 증가 (정도는 환경마다 다름)
- WebSocket 응답: 지연 가능성
- 크롤러 실패: 타임아웃 증가 가능성
- 사용자 경험: 저하

실제 영향: 실측 필요 (CloudWatch CPU Credit Balance)
```

### Redis + PostgreSQL 사용 시 (추정)

**예상 효과:**
```
평상시: RDS 읽기 부하 대부분 제거 (추정)
피크 시간: WebSocket 초기 접속 부하 제거 (추정)

Redis 캐시 효과:
  - 크롤러 환율 비교: DB → Redis (선택 사항)
  - WebSocket 초기 접속: DB → Redis (필수)
  - Broadcasting: DB 조회 유지 (권위 소스)

크레딧 소진: 개선 예상 (읽기 부하 감소)
크레딧 고갈: 위험 감소 예상

실제 효과: 1-2주 실측 후 판단 (CloudWatch 비교)
```

---

## 3. Connection Pool 효과

### PostgreSQL 연결 오버헤드 (일반론, 환경마다 다름)

**연결 생성 비용 (문헌 기반 추정):**
```
새 연결 생성: 일반적으로 수십 ms (환경/네트워크에 따라 크게 다름)
인증 + 세션 초기화: 추가 수십 ms
총: 환경마다 다름

실측 권장: psycopg2/asyncpg 벤치마크
```

**Connection Pool 필수:**
```python
# app/database.py (PostgreSQL 전환 시)
engine = create_engine(
    settings.DATABASE_URL,
    poolclass=QueuePool,
    pool_size=5,          # 기본 연결 5개
    max_overflow=10,      # 피크 시 최대 15개
    pool_pre_ping=True,   # 연결 유효성 체크
    pool_recycle=3600     # 1시간마다 재생성
)
```

### Redis 없이 (쿼리 빈도 높음, 추정)

**Connection Pool 부하 (추정):**
```
활성 연결: FastAPI 기본값 + 부하에 따라 변동
각 연결 메모리: PostgreSQL 공식 문서 참고, 환경마다 다름

주기적 체크 쿼리 (pool_pre_ping):
- 쿼리 빈도: pool_size × 체크 주기
- RDS CPU: 미미 (추정, 실측 필요)

실측 권장: pg_stat_activity로 실제 연결 수 확인
```

### Redis 사용 (쿼리 빈도 낮음, 추정)

**Connection Pool 부하 (추정):**
```
활성 연결: DB 읽기 감소로 pool 사용률 감소 예상
총 메모리: 환경마다 다름

주기적 체크 쿼리: 동일 (Redis는 pool_pre_ping에 영향 없음)

실측 권장: Redis 도입 전후 pg_stat_activity 비교
```

**예상 효과 (추정):**
```
Connection Pool 압력: 감소 예상 (읽기 부하 감소)
메모리 사용: 환경마다 다름
Ping 오버헤드: 변화 없음 (pool_pre_ping은 pool_size 기반)

실측 권장: CloudWatch DatabaseConnections 지표 비교
```

---

## 4. 장기 운영 안정성 (6개월 후, 추정)

### 데이터 성장 예측 (현재 보존 정책 기준)

**현재 정책:**
```
은행 데이터: 10일분만 유지 (자동 삭제)
Investing 데이터: 장기 보관 (그래프용)

예상 DB 크기 (6개월 후):
- 은행 데이터: ~5MB (10일분, 일정)
- Investing 데이터: ~50-100MB (6개월 누적, 3통화 × 6회/분)
- 총: ~55-105MB

**주의**: 실제 성장률은 환율 변동 빈도에 따라 달라짐
```

### Redis 없이 (추정)

**예상 부하:**
```
DB 크기: 55-105MB (현실적)
쿼리 성능: 큰 변화 없음 (인덱스 유효)

RDS CPU:
- 크롤러: 1% 미만 (영향 미미)
- WebSocket/Broadcasting: 주요 부하 (측정 필요)

크레딧 관리: 사용 패턴에 따라 다름 (예측 어려움)
```

### Redis 사용 (추정)

**예상 효과:**
```
DB 크기: 동일 (55-105MB)
쿼리 빈도: DB 읽기 대부분 캐시로 전가

Redis 메모리 사용 (Phase 1 실측, 2025-11-26):
- broadcast:latest: 3.6KB (30개 환율 JSON)
- circuit:redis: ~1KB
- Redis 오버헤드: ~930KB (Alpine 기본)
- 총: ~1.2MB (maxmemory 100MB의 1.2%)

Redis 메모리 사용 (Phase 2 예상):
- broadcast:latest: 3.6KB (동일)
- rate:* Hash: ~10KB (30개 통화-은행 쌍)
- circuit:redis: ~1KB
- Redis 오버헤드: ~950KB
- 총: ~1.2MB (증가 미미)

RDS CPU: WebSocket/Broadcasting 부하만 남음
크레딧 관리: 개선 (읽기 부하 감소)
```

**핵심**: 장기 운영에서 Redis의 주요 가치는 **Connection Pool 압력 감소**와 **피크 부하 완화**입니다.

---

## 5. 비용 효율성

### RDS 프리티어 (12개월)

```
db.t3.micro: 750시간/월 무료
스토리지: 20GB 무료
백업: 20GB 무료
```

### Redis 옵션

#### 옵션 1: EC2에 Redis 설치 ⭐ 권장
```
기존 t2.micro EC2 사용
Docker로 Redis 실행
메모리 사용: 100MB (추정)
EC2 메모리 여유: 충분 (1GB 중 여유 500MB+)

네트워크 레이턴시: 수백 μs ~ 수 ms (EC2 내부, 추정)
추가 비용: $0
관리: 단순 (docker-compose)

실측 권장: Redis PING 명령어로 RTT 측정
```

#### 옵션 2: Redis Cloud 무료 티어
```
메모리: 30MB 무료
현재 프로젝트: 부족 (브로드캐스트만 가능)
```

**최종 권장: 옵션 1 (EC2에 Redis 설치)**

**참고**: AWS ElastiCache는 프리티어 대상이 아니므로 제외

---

## 6. 종합 비교표

### 6.1 현재 환경 (SQLite) - 실측 가능

| 지표 | 실제 값 | 비고 |
|------|---------|------|
| 크롤러 쿼리 시간 | 1-3ms | 로컬 파일, 인덱스 스캔 |
| WebSocket 초기 접속 | 1-3ms | 단일 SELECT, 로컬 |
| DB 크기 | 5.4MB | 10일 보관 정책 |
| 동시 접속 처리 | 우수 | SQLite Read는 병렬 가능 |

### 6.2 PostgreSQL 전환 (Redis 없이) - 추정

| 지표 | 추정 값 | 근거 |
|------|---------|------|
| 크롤러 쿼리 시간 | 3-8ms | 네트워크 1-2ms + 쿼리 2-6ms |
| WebSocket 초기 접속 | 10-20ms | 네트워크 + 인덱스 스캔 |
| RDS 쿼리 빈도 | 0.3 qps (크롤러) | 스케줄 기반 계산 |
| Connection Pool | 5-10개 | FastAPI 기본값 + 피크 |
| 주요 부하 | Broadcasting/WebSocket | 크롤러는 미미 |

### 6.3 PostgreSQL + Redis - 추정

| 지표 | 추정 값 | 근거 |
|------|---------|------|
| 크롤러 쿼리 시간 | 0.5-1.5ms | Redis HGET, EC2 내부 |
| WebSocket 초기 접속 | 0.5-1.5ms | Redis GET, JSON 직렬화 재사용 |
| RDS 쿼리 빈도 | 0.001 qps (변경 시만) | DB 읽기 대부분 제거 |
| Connection Pool | 2-5개 | 읽기 부하 감소 |
| Redis 메모리 | ~1.2MB (실측) | Phase 1: 1.2MB, Phase 2: 1.2MB (증가 미미) |

### 6.4 핵심 효과 (추정)

| 시나리오 | PostgreSQL | Redis | 효과 |
|---------|-----------|-------|------|
| **크롤러 환율 비교** | 미미 (0.3 qps) | 미미 | 효과 제한적 |
| **WebSocket 초기 접속** | 10-20ms × 100명 | 0.5-1.5ms × 100명 | **7-40배 빠름** ⭐ |
| **Broadcasting** | DB 조회 필수 | JSON 재사용 가능 | 직렬화 중복 제거 |
| **Connection Pool** | 5-10개 | 2-5개 | 압력 감소 |

**결론**: Redis의 주요 가치는 **WebSocket 초기 접속**과 **피크 시 Connection Pool 압력 감소**입니다.

---

## 7. 측정 도구 및 방법

### PostgreSQL RDS 실측 필요 항목

| 측정 항목 | 도구/방법 | 측정 주기 |
|----------|----------|----------|
| **RDS CPU** | CloudWatch CPUUtilization | 실시간 + 1-2주 추이 |
| **CPU 크레딧** | CloudWatch CPUCreditBalance | 실시간 (고갈 감지) |
| **쿼리 레이턴시** | pg_stat_statements | 주요 쿼리별 평균/P95 |
| **Connection Pool** | pg_stat_activity | 실시간 활성 연결 수 |
| **네트워크 지연** | EC2 → RDS ping, Redis PING 명령어 | 1회 (배포 시) |
| **메모리 사용** | CloudWatch FreeableMemory | 실시간 |
| **IOPS** | CloudWatch ReadIOPS/WriteIOPS | 1-2주 추이 |

### Redis 도입 전후 비교 항목

| 비교 항목 | 측정 방법 | 기대 효과 |
|----------|----------|----------|
| DB 읽기 쿼리 수 | pg_stat_database.tup_fetched | 감소 |
| Connection Pool 사용률 | pg_stat_activity | 감소 |
| WebSocket 초기 접속 속도 | 클라이언트 측 타임스탬프 | 개선 |
| RDS CPU 평균 | CloudWatch (1주일 평균) | 감소 |
| 크레딧 축적/소진 패턴 | CloudWatch (1-2주 추이) | 안정화 |

### 실측 계획 (PostgreSQL 전환 후)

```
1주차: 기준선 수립
- Redis 없이 1주일 운영
- 모든 지표 수집 (위 표 참고)
- 피크 시간대 부하 패턴 파악

2주차: Redis 브로드캐스트 캐시 도입
- WebSocket 캐시만 적용
- 동일 지표 수집
- 전후 비교 분석

3-4주차: 추가 확장 판단
- RDS 부하가 여전히 높으면 → 크롤러 환율 비교 Redis 도입
- 안정적이면 → 현상 유지

측정 결과는 본 문서의 추정치를 대체
```

---

## 8. 결론 및 권장사항

### 8.1 Redis 도입 필요성 재평가

**SQLite (현재):**
- ✅ **WebSocket 브로드캐스트 캐시 도입 권장** (동시 접속 대비)
- ❌ 크롤러 환율 비교는 **효과 제한적** (0.3 qps, 미미한 부하)
- ✅ 크롤러 토글: DB 테이블 + In-memory 캐시 (단순성)

**PostgreSQL RDS (프리티어):**
- ✅ **WebSocket 브로드캐스트 캐시 필수** (Connection Pool 압력 완화)
- ⚠️ 크롤러 환율 비교: 선택적 (주요 부하 아님)
- ✅ 크롤러 토글: Redis Hash (멀티 인스턴스 대비)
- ✅ 비용 증가 없음 (EC2 내 설치)

### 8.2 구현 전략 (Phase 정의 표)

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
PostgreSQL 전환 후 1-2주 모니터링 (섹션 7 참고):
├─ RDS CPU 평균 < Baseline 10% && Connection Pool 안정
│  └─ 현상 유지 (브로드캐스트 캐시만)
│
├─ RDS CPU 평균 > Baseline 10% || Connection Pool 부족
│  └─ 크롤러 환율 비교 Redis 도입
│
└─ 멀티 인스턴스 확장 계획
   └─ 크롤러 토글 Redis Hash 전환
```

### 문서 유지 규칙

⚠️ **중요**: Phase 정의 변경 시 아래 두 문서를 **동시 업데이트** 필수
- `REDIS_IMPACT_ANALYSIS.md` (섹션 8.2) ← **이 문서 (권위 버전)**
- `REDIS_STRATEGY.md` (섹션 4)

불일치 발생 시 → 이 문서를 권위 버전으로 간주

### 8.3 측정 계획

**PostgreSQL 전환 후 실측 필요:**
```
1. RDS CloudWatch 지표 수집 (CPU, IOPS, Connection)
2. pg_stat_statements로 쿼리 프로파일링
3. 실제 부하 테스트 (동시 접속 100명)
4. 크레딧 소진 패턴 모니터링 (1-2주)

→ 실측 데이터 기반으로 Redis 전면 도입 여부 재판단
```

### 8.4 핵심 교훈

1. **추정치는 실측으로 검증**: 본 문서의 수치는 가정이며, 실제 측정 필수
2. **주요 부하 파악**: 크롤러(0.3 qps)보다 WebSocket/Broadcasting이 주요
3. **점진적 도입**: SQLite는 브로드캐스트만, PostgreSQL은 전면 확장
4. **보수적 설계**: 과장된 기대보다 안정성 우선

**최종 권장: SQLite는 브로드캐스트 캐시만, PostgreSQL 전환 후 실측 기반 확장**

---

**작성일**: 2025-11-26
**검토 주기**: PostgreSQL 전환 시점, 사용자 500명 도달 시
