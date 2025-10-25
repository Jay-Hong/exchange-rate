# Architecture Decision Records (ADR)

프로젝트의 주요 기술적 의사결정과 그 근거를 기록합니다.

---

## ADR-001: WebSocket 구현 - Python FastAPI vs Node.js

**날짜:** 2025-10-11
**상태:** 수락됨

### 상황

- **목표:** 실시간 환율 브로드캐스트 (10초 주기), 500명 동시 접속 지원
- **현재 구조:** Python FastAPI + asyncio WebSocket
- **검토 배경:** Node.js의 이벤트 루프 기반 WebSocket 성능이 더 우수하다는 의견
- **고려사항:** 크롤링은 Python으로 구현되어 있음

### 결정

**Python FastAPI 모놀리식 구조 유지**

### 근거

#### 1. 성능 분석
```
Python FastAPI (asyncio):
- 동시 접속: 2,000-5,000명 (목표의 4-10배)
- 메모리: ~300-400MB
- CPU: 10초 브로드캐스트 시 5-10%

Node.js (분리 시):
- 동시 접속: 10,000+ (과도한 스펙)
- 메모리: ~200-300MB (Node.js만)
- CPU: 비슷
```
→ **500명 목표에서 성능 차이 체감 불가**

#### 2. 아키텍처 복잡도

**현재 (모놀리식):**
```
크롤러(Python) → SQLite → WebSocket(Python) → 클라이언트
```

**Node.js 분리 시:**
```
크롤러(Python) → SQLite → ??? → Node.js → 클라이언트
                     ↓
            Redis/MQ 추가 필요
```

**문제점:**
- SQLite는 파일 기반 → 다중 프로세스 쓰기 어려움
- Python ↔ Node.js 데이터 전달 위해 Redis Pub/Sub 또는 Message Queue 필요
- t2.micro 메모리 부족 (Python 400MB + Node.js 300MB + Redis 100MB = 800MB > 1GB)

#### 3. 운영 복잡도
- **배포:** 단일 서비스 → 2개 서비스 관리
- **로그:** 통합 로깅 → 분산 로깅
- **모니터링:** 1개 프로세스 → 2개 프로세스 + 의존성 관리

### 결과

**장점:**
- 개발/운영 단순성 유지
- 인프라 비용 절감 (추가 서비스 불필요)
- 크롤링과 WebSocket의 코드 응집도 유지

**단점:**
- 대규모 확장 시 제약 (하지만 1,000명까지는 문제없음)

**향후 재검토 시점:**
- 동시 접속 1,000명 초과 시
- PostgreSQL + Redis 도입 후 마이크로서비스 필요성 발생 시

---

## ADR-002: 성능 최적화 - WebSocket 압축 & 연결 풀

**날짜:** 2025-10-11
**상태:** 거부됨 (조기 최적화)

### 상황

**실측 데이터:**
- 브로드캐스트 크기: 3.37 KB (30개 환율 데이터)
- gzip 압축 시: 0.64 KB (80.9% 절약)
- 브로드캐스트 주기: 10초

**AWS 대역폭 비용 (사용자 500명 기준):**
- 압축 없음: 416.5 GB/월 → $36.14/월
- 압축 사용: 79.1 GB/월 → $5.77/월
- 월간 절약: $30.37

### 결정

**둘 다 지금은 구현하지 않음**

### 근거

#### 1. WebSocket 압축

**기술적 제약:**
- FastAPI/Starlette는 WebSocket 압축 기본 미지원
- 직접 구현 필요:
  ```python
  # 서버
  compressed = gzip.compress(json_str.encode())
  await websocket.send_bytes(compressed)

  # 클라이언트 (iOS/Android/Web 모두 수정)
  decompressed = decompress(data)
  ```
- 3개 플랫폼 클라이언트 모두 수정 필요
- 개발 공수: ~1주일

**비용 대비 효과:**
| 사용자 수 | 압축 없음 (비용) | 프리티어 초과 여부 |
|-----------|-----------------|-------------------|
| 18명      | $0              | ❌ (15GB 이내)     |
| 100명     | $6.15/월        | ✅                |
| 200명     | $13.64/월       | ✅                |
| 500명     | $36.14/월       | ✅                |

- 프리티어 초과 시점: 18명
- 하지만 **현재 사용자: 0명 (런칭 전)**

**결론:** 개발 공수 대비 비효율

#### 2. 연결 풀 최적화

**제안된 코드:**
```python
SessionLocal = sessionmaker(
    bind=engine,
    pool_size=20,
    max_overflow=40
)
```

**문제점:**
- **SQLite는 파일 기반 DB** → 연결 풀 개념 부적합
- 연결 생성 비용: <1ms (무시 가능)
- 네트워크 오버헤드: 0 (로컬 파일)
- `check_same_thread=False`로 이미 멀티스레드 지원

**효과:** 성능 개선 0%

**연결 풀이 필요한 경우:**
- PostgreSQL/MySQL (네트워크 DB)
- 연결 생성 비용 >50ms
- 동시 접속 수십~수백 개

### 결과

**대안: 단계별 최적화 로드맵**

| 사용자 수 | 월간 비용 | 조치사항 | 우선순위 |
|-----------|----------|----------|----------|
| 0~18명 | $0 | **없음** (프리티어) | 사용자 확보 |
| 18~100명 | $0~6 | 모니터링만 | 기능 개발 |
| 100~200명 | $6~14 | JSON 구조 최적화 검토 | 중 |
| 200~500명 | $14~36 | JSON 최적화 or PostgreSQL 전환 | 상 |
| 500명+ | $36+ | PostgreSQL + Redis + CDN | 필수 |

**JSON 구조 최적화 (Phase 2 대안):**
```json
// 현재 (3.37 KB)
{
  "type": "rates",
  "data": {
    "rates": [
      {"bank": "kb", "currency": "usd-krw", "rate": 1340.5, "timestamp": "2025-10-11T..."},
      ...
    ]
  }
}

// 최적화 (1.8 KB, 50% 절약)
{
  "t": "r",
  "d": [
    ["kb", "usd", 1340.5, 1728648000],  // 배열 형식, unix timestamp
    ...
  ]
}
```

- 효과: 크기 50% 절약 (압축 없이)
- CPU 오버헤드: 0%
- 개발 공수: 2-3일

### 핵심 교훈

**"조기 최적화는 만악의 근원" (Premature optimization is the root of all evil)**

- 현재 병목: 없음 (사용자 0명)
- 실제 문제 발생 시점: 사용자 100명+
- **우선순위: 서비스 출시 → 사용자 확보 → 실제 데이터 기반 최적화**

---

## ADR-003: 알림 시스템 - WebSocket vs Push Notification

**날짜:** 2025-10-11
**상태:** 계획됨 (Phase 2-3 구현 예정)

### 상황

**사용자 요구사항:**
- 관심 있는 환율 변동 시 실시간 알림
- **앱이 꺼져 있을 때도** 특정 환율 도달 시 알림 받기
- 배터리/데이터 절약

**현재 구현:**
- WebSocket 기반 실시간 브로드캐스트 (10초 주기)
- 앱 실행 중에만 작동

### 핵심 문제: WebSocket의 한계

```
❌ WebSocket = 앱 실행 중일 때만 작동

iOS/Android 앱 상태:
- ✅ Foreground (화면에 보임): WebSocket 연결 유지
- ⚠️  Background (백그라운드): 10-30초 후 연결 자동 종료
- ❌ Terminated (앱 종료): 연결 완전 끊김

→ 앱이 꺼져 있으면 알림 못 받음!
```

**기술적 이유:**
- WebSocket은 실시간 연결 유지 필요
- 모바일 OS는 배터리/데이터 절약을 위해 백그라운드 연결 강제 종료
- iOS: 백그라운드 30초 제한
- Android: Doze 모드에서 네트워크 차단

### 결정

**하이브리드 알림 시스템 구축 (Phase 2-3)**

```
서버 ─┬─ WebSocket ──→ 앱 실행 중: 실시간 환율 데이터
      │
      └─ Push ──→ APNs/FCM ──→ 앱 꺼져 있을 때: 중요 알림
```

### 근거

#### 1. WebSocket Notification (현재 구현)

**용도:** 앱 실행 중 실시간 데이터 스트리밍

**장점:**
- 즉각적인 데이터 전송 (10초 주기)
- 양방향 통신 가능
- 서버 부하 낮음

**단점:**
- 앱 실행 중에만 작동
- 백그라운드/종료 시 연결 끊김
- 배터리 소모 (지속 연결)

**적합한 사용 사례:**
- 실시간 환율 대시보드
- 차트 업데이트
- 즉시성 높은 데이터

---

#### 2. Push Notification (계획)

**용도:** 앱 꺼져 있을 때 중요 알림

**작동 방식:**
```
1. 사용자가 앱에서 알림 설정
   "USD-KRW 1350원 도달 시 알림"

2. 서버 DB에 알림 조건 저장

3. 크롤러가 환율 업데이트 시 조건 체크

4. 조건 충족 → Push 서버로 전송
   Python → Firebase/APNs → 디바이스 📱

5. 앱 꺼져 있어도 알림 표시!
   🔔 "USD-KRW 1350원 도달!"
```

**장점:**
- 앱 꺼져 있어도 알림 도착
- OS 레벨 최적화 (배터리 효율)
- 사용자 재방문율 증가

**단점:**
- 인프라 추가 필요 (Firebase/APNs)
- 비용 발생 가능 (월 $0-25)
- 구현 복잡도 증가

**적합한 사용 사례:**
- 목표 환율 도달 알림
- 급격한 환율 변동 경고
- 일일 환율 요약

---

#### 3. 비교표

| 특성 | WebSocket | Push Notification |
|------|----------|-------------------|
| **앱 상태** | 실행 중만 | 언제나 |
| **전송 빈도** | 높음 (10초) | 낮음 (이벤트 발생 시) |
| **데이터 크기** | 제한 없음 | ~4KB |
| **배터리 소모** | 높음 | 낮음 |
| **구현 복잡도** | ✅ 낮음 | ⚠️ 중간 |
| **인프라 비용** | ✅ $0 | ⚠️ $0-25/월 |

### 구현 계획

#### Phase 1: WebSocket Notification (✅ 완료)
```python
# 현재 구현
- 10초 주기 브로드캐스트
- 변경 감지 최적화 (has_changes)
- 앱 실행 중 실시간 데이터
```

#### Phase 2: 맞춤형 WebSocket 알림 (계획)
```python
# app/main.py 확장
class ConnectionManager:
    subscriptions: Dict[WebSocket, UserPreferences]

    async def notify_subscribers(self, change_event):
        """관심 통화/은행 구독자에게만 알림"""
        for ws, prefs in self.subscriptions.items():
            if should_notify(prefs, change_event):
                await ws.send_json({
                    "type": "notification",
                    "data": change_event
                })
```

**개발 공수:** 2-3일
**효과:** 불필요한 알림 90% 감소

---

#### Phase 3: Push Notification (계획)
```python
# 새 파일 추가
app/
├── push_notification.py   # FCM/APNs 전송
├── user_alerts.py         # 알림 조건 관리
└── models.py              # UserAlert 모델 추가

# DB 테이블 추가
UserAlert:
- user_id, device_token, platform (ios/android)
- currency, target_rate, condition (above/below)
- is_active, created_at

# 크롤러 통합
def insert_bank_rates_into_db(...):
    if should_save:
        db.add(new_entry)
        db.commit()

        # 푸시 알림 체크
        await check_and_send_alerts(new_rate)
```

**필요 인프라:**
- Firebase Cloud Messaging (FCM) - Android & iOS 통합
- 또는 Apple Push Notification Service (APNs) - iOS 전용

**개발 공수:** 1-2주
**인프라 비용:**
- FCM: 무료 (월 100만 건까지)
- APNs: 무료
- 서버 증설 필요 없음

**API 엔드포인트 추가:**
```
POST /api/alerts/register    - 알림 등록
GET  /api/alerts              - 내 알림 목록
DELETE /api/alerts/{id}       - 알림 삭제
POST /api/device/register     - 디바이스 토큰 등록
```

---

#### Phase 4: 고급 알림 (선택)
```python
# 이벤트 기반 알림
- 급격한 변동 감지 (1% 이상)
- 은행 간 환율 차이 알림 (차익 거래 기회)
- 패턴 감지 (연속 상승/하락)

# ML 기반 예측 알림
- "30분 내 1350원 돌파 예상"
- 사용자 행동 학습 (자주 보는 시간대 알림)
```

**개발 공수:** 2-4주
**우선순위:** 낮음 (사용자 1,000명+ 시 고려)

### 결과

**단계별 로드맵:**

| 단계 | 기능 | 구현 시기 | 필수 여부 |
|------|------|----------|----------|
| Phase 1 | WebSocket 브로드캐스트 | ✅ 완료 | 필수 |
| Phase 2 | 맞춤형 WebSocket 알림 | 사용자 200명+ | 권장 |
| Phase 3 | Push Notification | 사용자 500명+ | 필수 |
| Phase 4 | 고급 알림 (ML) | 사용자 1,000명+ | 선택 |

**비용 예측 (500명 기준):**
- WebSocket 대역폭: $14-36/월 (ADR-002 참조)
- Push Notification: $0/월 (FCM 무료 티어)
- 총 비용: ~$20/월

**핵심 전략:**
1. 초기: WebSocket만으로 시작 (개발 빠름)
2. 성장기: Push 추가 (사용자 재방문율 ↑)
3. 성숙기: 고급 알림 (차별화)

### 기술 선택

**Push 서비스 비교:**

| 서비스 | 지원 플랫폼 | 무료 한도 | 장점 |
|--------|------------|----------|------|
| **Firebase (FCM)** | iOS, Android | 100만 건/월 | 통합 관리, 분석 도구 |
| **APNs** | iOS만 | 무제한 | Apple 공식, 안정적 |
| **OneSignal** | iOS, Android, Web | 10,000 구독자 무료 | UI 제공, 쉬운 설정 |

**권장: Firebase FCM**
- iOS/Android 단일 API
- 무료 한도 충분 (월 100만 건)
- Google Cloud 인프라 안정성

---

## ADR-005: 로그 시스템 단순화 (4개→2개)

**날짜:** 2025-10-14
**상태:** ✅ 완료 (Completed)

### 상황

**기존:** 4개 파일 (app.log, error.log, crawler.log, debug.log)
- 디스크 사용: 146MB (며칠 운영)
- 문제점: 크롤러 에러 1건이 3곳 중복 기록 (디스크 3배 낭비)
- 복잡도: 4개 × 5백업 = 최대 24개 파일

### 결정

**2개 파일만 사용:**
```
├── app.log      # 모든 운영 로그 (INFO+, 크롤러 포함)
└── error.log    # 에러/경고만 (WARNING+)
```

### 근거

1. **디스크 66% 절약** (146MB → 50MB)
2. **파일 75% 감소** (24개 → 6개)
3. **중복 제거** (크롤러 에러 3곳 → 2곳)
4. **관리 단순화** (관리자 페이지 필터링으로 크롤러 전용 파일 대체)

### 결과

- 월간 로그: 1GB → 400MB (60% 절약)
- AWS 프리티어 친화적
- 관리자 페이지 은행 필터로 오히려 더 강력한 기능 제공

**교훈:** "단순함이 최고의 복잡도다" - 프로젝트 규모에 맞는 시스템 설계

---

## ADR-009: Docker Compose 기반 배포 아키텍처

**날짜:** 2025-10-21
**상태:** 📋 설계 완료, 구현 대기

### 상황

**현재 배포 환경:**
- 개발: 로컬 Python + SQLite
- 운영: AWS EC2 t2.micro (1GB RAM, 프리티어)
- 배포 방식: 수동 배포 (rsync, git pull)

**향후 확장 요구사항:**
- 사용자 증가에 따른 단계적 확장 (200명 → 1,000명+)
- SQLite → PostgreSQL 전환 (사용자 500명+)
- Redis 캐싱 도입 (성능 개선)
- Nginx Reverse Proxy (보안, SSL, 로드 밸런싱)

### 결정

**Docker Compose 기반 단계적 확장 아키텍처 채택**

```
Phase 1: Nginx + FastAPI + SQLite         (200-500명)
Phase 2: + PostgreSQL + Redis              (500-1,000명)
Phase 3: + 서비스 분리 (크롤러/앱)         (1,000명+)
```

### 근거

#### 1. 대안 검토

| 대안 | 장점 | 단점 | 채택 여부 |
|------|------|------|----------|
| **Docker Compose (단계별)** | - 점진적 확장<br>- 메모리 최적화<br>- 무중단 마이그레이션<br>- 프리티어 친화적 | - 초기 학습 곡선<br>- 설정 파일 복잡도 | ✅ 채택 |
| 수동 배포 (현재) | - 단순함 | - 확장 어려움<br>- PostgreSQL 전환 복잡<br>- 의존성 관리 어려움 | ❌ 기각 |
| Kubernetes | - 자동 스케일링<br>- 최고 확장성 | - 메모리 1GB+ 필요<br>- 복잡도 매우 높음<br>- Over-engineering | ❌ 기각 |
| Docker Swarm | - K8s보다 단순 | - 여전히 복잡<br>- 단일 서버에서 불필요 | ❌ 기각 |

#### 2. AWS 프리티어 메모리 최적화

**Phase 1 메모리 할당 (총 ~315MB):**
```yaml
nginx:     32MB   (0.1 CPU)
fastapi:   600MB  (0.9 CPU)
시스템:    100MB
여유:      ~635MB
```

**Phase 2 메모리 할당 (총 ~565MB):**
```yaml
nginx:     32MB
fastapi:   600MB
postgres:  200MB  (shared_buffers=64MB)
redis:     96MB   (maxmemory=64mb)
시스템:    100MB
여유:      ~385MB
```

→ t2.micro(1GB)에서 안정적 운영 가능

**Phase 3: t3.small(2GB) 이상 권장**

#### 3. 단계별 확장 전략

**Phase 1 → Phase 2 마이그레이션:**
```bash
# 무중단 전환
1. PostgreSQL 컨테이너 시작
2. SQLite → PostgreSQL 데이터 마이그레이션 (백그라운드)
3. FastAPI 재시작 (새 DB 연결)
4. 검증 후 SQLite 제거

# 실행 명령
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d
./scripts/migrate-to-postgres.sh
```

**설정 파일 오버라이드 방식:**
- `docker-compose.yml` - Phase 1 기본 설정
- `docker-compose.phase2.yml` - Phase 2 추가 설정 (PostgreSQL, Redis)
- `docker-compose.phase3.yml` - Phase 3 추가 설정 (서비스 분리)

#### 4. 보안 강화

**네트워크 분리:**
```yaml
networks:
  frontend:   # Nginx ↔ FastAPI
  backend:    # FastAPI ↔ Redis
  database:   # FastAPI ↔ PostgreSQL (내부 전용)
```

**포트 노출 전략:**
- Nginx만 외부 노출 (80, 443)
- FastAPI, PostgreSQL, Redis는 내부 네트워크만
- SSL/TLS 종료 (Let's Encrypt)

#### 5. 리소스 제한

**OOM Killer 우선순위:**
```yaml
fastapi:     oom_score_adj: -500  # 보호
nginx:       oom_score_adj: -300
postgres:    oom_score_adj: -400
redis:       oom_score_adj: 100   # 필요 시 먼저 종료
```

**메모리 한계:**
```yaml
services:
  fastapi:
    deploy:
      resources:
        limits:
          memory: 600M
        reservations:
          memory: 300M
```

### 파일 구조

```
F06_GitHub/
├── docker-compose.yml              # Phase 1
├── docker-compose.phase2.yml       # Phase 2
├── docker-compose.phase3.yml       # Phase 3
├── Dockerfile                      # FastAPI
├── .dockerignore
├── nginx/
│   ├── Dockerfile
│   └── conf.d/
│       ├── phase1.conf
│       ├── phase2.conf
│       └── phase3.conf
├── postgres/
│   ├── init/
│   └── conf/
├── scripts/
│   ├── deploy.sh
│   ├── migrate-to-postgres.sh
│   ├── backup-db.sh
│   └── ssl-renew.sh
└── volumes/
    ├── sqlite-data/
    ├── postgres-data/
    ├── redis-data/
    └── logs/
```

### 구현 예시

#### docker-compose.yml (Phase 1)

```yaml
version: '3.8'

services:
  nginx:
    build: ./nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./static:/var/www/static:ro
      - ./volumes/ssl:/etc/letsencrypt:ro
    networks:
      - frontend
    depends_on:
      fastapi:
        condition: service_healthy
    deploy:
      resources:
        limits:
          cpus: '0.1'
          memory: 32M

  fastapi:
    build: .
    env_file: .env
    environment:
      - DATABASE_URL=sqlite:////data/exchange_rates.db
    volumes:
      - ./volumes/sqlite-data:/data
      - ./volumes/logs/app:/app/logs
    expose:
      - "8000"
    networks:
      - frontend
    deploy:
      resources:
        limits:
          cpus: '0.9'
          memory: 600M
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s

networks:
  frontend:
    driver: bridge
```

#### docker-compose.phase2.yml (PostgreSQL + Redis 추가)

```yaml
version: '3.8'

services:
  fastapi:
    environment:
      - DATABASE_URL=postgresql://user:${DB_PASSWORD}@postgres:5432/exchange_db
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - frontend
      - backend
      - database

  postgres:
    image: postgres:15-alpine
    environment:
      - POSTGRES_PASSWORD=${DB_PASSWORD}
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./postgres/init:/docker-entrypoint-initdb.d:ro
    command: postgres -c shared_buffers=64MB -c max_connections=20
    expose:
      - "5432"
    networks:
      - database
    deploy:
      resources:
        limits:
          memory: 200M

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 64mb --maxmemory-policy allkeys-lru
    volumes:
      - redis-data:/data
    expose:
      - "6379"
    networks:
      - backend
    deploy:
      resources:
        limits:
          memory: 96M

networks:
  backend:
    driver: bridge
  database:
    driver: bridge
    internal: true  # 외부 접근 차단

volumes:
  postgres-data:
  redis-data:
```

### 결과

#### 1. 메모리 효율성

| Phase | 메모리 사용 | t2.micro 적합성 | 비용 |
|-------|------------|----------------|------|
| Phase 1 | ~315MB | ✅ 여유 635MB | $0 |
| Phase 2 | ~565MB | ✅ 여유 385MB | $0 |
| Phase 3 | ~845MB | ⚠️ 여유 105MB | ~$15/월 (t3.small) |

#### 2. 확장성

**수평 확장 (Phase 3):**
```yaml
# FastAPI 2개 인스턴스
fastapi-1:
  replicas: 1
fastapi-2:
  replicas: 1

# Nginx 로드 밸런싱
upstream fastapi_backend {
    server fastapi-1:8000;
    server fastapi-2:8000;
}
```

#### 3. 운영 편의성

**배포:**
```bash
# 초기 배포
docker-compose up -d

# Phase 2로 업그레이드
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d

# 로그 확인
docker-compose logs -f fastapi
```

**백업:**
```bash
# SQLite
./scripts/backup-db.sh

# PostgreSQL
docker-compose exec postgres pg_dump -U user > backup.sql
```

### 트레이드오프

**장점:**
- ✅ AWS 프리티어에서 안정적 운영
- ✅ 단계적 확장 (사용자 증가에 따라)
- ✅ 무중단 마이그레이션 지원
- ✅ 보안 강화 (네트워크 분리)
- ✅ 재현 가능한 환경 (로컬/운영 동일)
- ✅ 의존성 관리 간소화

**단점:**
- ⚠️ Docker 학습 곡선 (초기 투자)
- ⚠️ 설정 파일 관리 필요 (3-4개)
- ⚠️ 디스크 공간 사용 증가 (~2GB, 이미지 포함)

### 향후 개선 가능성

**Phase 2+:**
- Prometheus + Grafana 모니터링
- 자동 백업 (cron)
- CI/CD (GitHub Actions)

**Phase 3+:**
- Docker Swarm or Kubernetes (사용자 5,000명+)
- CDN 도입 (CloudFront)
- Multi-region 배포

### 참고 문서

상세 구현 가이드: [DOCKER.md](DOCKER.md)

---

## 문서 히스토리

- 2025-10-11: ADR-001, ADR-002, ADR-003 작성 (아키텍처 설계 단계)
- 2025-10-13: ADR-004 추가 (IBK 크롤러 성능 최적화)
- 2025-10-14: ADR-005 추가 (로그 시스템 단순화)
- 2025-10-14: ADR-006 추가 (메모리 기반 크롤러 상태 관리)
- 2025-10-15: ADR-007 추가 (백엔드 bank 필터링)
- 2025-10-17: ADR-008 추가 (관리자 페이지 재설계 - 통합 API & 심플 대시보드)
- 2025-10-21: ADR-009 추가 (Docker Compose 기반 배포 아키텍처)
- 2025-10-25: 문서 간소화 (ADR-006 삭제, ADR-005 축약 228줄→35줄)
