# Architecture Decision Records (ADR)

프로젝트의 주요 기술적 의사결정과 그 근거를 기록합니다.

---

## ADR 작성 정책

### ✅ 무엇을 기록하는가?

**기록해야 할 것 (Architecture Decision):**
- 기술 스택 선택 (Python vs Node.js, SQLite vs PostgreSQL)
- 아키텍처 패턴 (모놀리식 vs 마이크로서비스)
- 인프라 결정 (Docker, AWS 제약 대응)
- 트레이드오프가 명확한 결정 (성능 vs 복잡도, 비용 vs 확장성)

**기록하지 말아야 할 것 (Implementation Detail):**
- 구체적인 코드 최적화 (함수 리팩토링, 변수명 변경)
- 버그 수정 과정
- 특정 라이브러리 사용법
- 상세한 설정 파일 내용

### 📋 작성 기준 (4가지 질문)

1. **비가역성**: 나중에 쉽게 바꾸기 어려운 결정인가?
2. **영향 범위**: 시스템 전체 또는 주요 컴포넌트에 영향을 주는가?
3. **대안 존재**: 2개 이상의 선택지가 있었는가?
4. **장기 유지**: 6개월 후에도 이 결정의 맥락을 알아야 하는가?

→ **4개 모두 YES면 ADR 작성, 그렇지 않으면 별도 문서 또는 커밋 메시지**

### 📌 예시

| 내용 | ADR 필요 | 이유 | 대신 기록할 곳 |
|------|---------|------|--------------|
| WebSocket 구현 - Python vs Node.js | ✅ YES | 기술 스택 선택, 비가역적 | - |
| Docker Compose 채택 | ✅ YES | 인프라 아키텍처 결정 | - |
| IBK 크롤러 날짜 input 처리 | ❌ NO | 구현 세부사항 | CRAWLERS.md |
| 관리자 페이지 통합 API | ❌ NO | 코드 리팩토링 | Git 커밋 |
| 로그 파일 4개→2개 | ✅ YES | 시스템 설계 변경 | ADR-004 |

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

## ADR-004: 로그 시스템 단순화 (4개→2개)

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

## ADR-005: Docker Compose 기반 배포 아키텍처

**날짜:** 2025-10-21
**상태:** 📋 설계 완료, 구현 대기

### 상황

**현재:**
- AWS EC2 t2.micro (1GB RAM, 프리티어)
- 수동 배포 (rsync, git pull)

**확장 요구사항:**
- 단계적 확장 (200명 → 1,000명+)
- SQLite → PostgreSQL 전환
- Redis 캐싱, Nginx Reverse Proxy

### 결정

**Docker Compose 기반 단계적 확장 아키텍처 채택**

```
Phase 1: Nginx + FastAPI + SQLite         (200-500명)
Phase 2: + PostgreSQL + Redis              (500-1,000명)
Phase 3: + 서비스 분리 (크롤러/앱)         (1,000명+)
```

### 근거

#### 1. 대안 검토

| 대안 | 장점 | 단점 | 채택 |
|------|------|------|------|
| **Docker Compose** | 점진적 확장, 메모리 최적화, 무중단 마이그레이션 | 초기 학습 곡선 | ✅ |
| 수동 배포 | 단순 | 확장 어려움, PostgreSQL 전환 복잡 | ❌ |
| Kubernetes | 자동 스케일링 | 메모리 1GB+ 필요, Over-engineering | ❌ |

#### 2. AWS 프리티어 메모리 최적화

| Phase | 메모리 사용 | t2.micro 적합성 | 비용 |
|-------|------------|----------------|------|
| Phase 1 | ~315MB (nginx 32MB + fastapi 600MB) | ✅ 여유 635MB | $0 |
| Phase 2 | ~565MB (+ postgres 200MB + redis 96MB) | ✅ 여유 385MB | $0 |
| Phase 3 | ~845MB (+ 크롤러 분리 150MB) | ⚠️ 여유 105MB | $15/월 (t3.small) |

→ **Phase 1, 2는 프리티어에서 안정적 운영 가능**

#### 3. 단계별 전환 전략

**무중단 마이그레이션 (Phase 1 → 2):**
```bash
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d postgres
./scripts/migrate-to-postgres.sh  # 백그라운드 데이터 마이그레이션
docker-compose -f docker-compose.yml -f docker-compose.phase2.yml up -d
```

**설정 오버라이드:**
- `docker-compose.yml` - Phase 1 기본
- `docker-compose.phase2.yml` - PostgreSQL + Redis 추가
- `docker-compose.phase3.yml` - 서비스 분리

### 트레이드오프

**장점:**
- ✅ 프리티어 친화적 (Phase 1-2)
- ✅ 무중단 확장
- ✅ 보안 강화 (네트워크 분리, Nginx만 외부 노출)
- ✅ 재현 가능한 환경

**단점:**
- ⚠️ Docker 학습 필요
- ⚠️ 설정 파일 관리 (3-4개)
- ⚠️ 디스크 사용 증가 (~2GB)

### 참고 문서

**상세 구현 가이드:** [DOCKER.md](DOCKER.md)
- 파일 구조, docker-compose.yml 전체 예시
- Nginx, PostgreSQL, Redis 설정
- 배포 명령어, 트러블슈팅

---

---

## ADR-006: Selenium 크롤러 동시 실행 제어 - Semaphore vs AsyncIO Queue

**날짜:** 2025-11-06
**상태:** 수락됨

### 상황

- **문제:** Selenium 크롤러(5개) 동시 실행 시 메모리 부족 및 경합 발생
- **현재 구조:** Threading Semaphore(1)로 최대 1개 드라이버 제한
- **발견된 문제:**
  - Semaphore acquire 실패 → 즉시 Fallback URL로 전환
  - 정상 데이터 수집 가능함에도 MIBANK까지 불필요하게 내려감
  - 메모리 압박 시 false negative 현상
- **환경:** AWS t2.micro (1 vCPU, 1GB RAM)

### 결정

**AsyncIO Queue 기반 순차 실행 시스템 채택**

크롤러를 2개 그룹으로 분리:
1. **Request 기반** (investing, kb, woori, bs, citi) - 기존 APScheduler 방식 유지
2. **Selenium 기반** (hana, shinhan, ibk, nh, sc) - AsyncIO Queue로 순차 처리

### 근거

#### 1. Semaphore 경합 문제

**Semaphore 방식의 한계:**
```python
# 크롤러 A, B, C가 거의 동시에 실행
A: semaphore.acquire(blocking=False)  # ✅ 성공
B: semaphore.acquire(blocking=False)  # ❌ 실패 → Fallback
C: semaphore.acquire(blocking=False)  # ❌ 실패 → Fallback
```

**문제:**
- B, C는 정상 경로 수집 가능함에도 경합으로 Fallback
- Fallback URL도 Selenium 사용 → 다시 경합 발생
- 최종적으로 MIBANK까지 내려가는 악순환

**Queue 방식의 해결:**
```python
# 모든 작업이 Queue에 추가됨
Queue: [A, B, C]
Worker: A 실행 → 완료 → B 실행 → 완료 → C 실행
```

- 경합 자체가 불가능
- 모든 크롤러가 정상 경로 시도 보장
- 불필요한 Fallback 70% 감소 예상

#### 2. 메모리 안정성

**현재 (Semaphore):**
- 이론: 최대 1개 드라이버
- 실제: 드라이버 종료 지연, 좀비 프로세스로 2-3개 동시 존재
- 메모리 스파이크: ~500MB (1GB의 50%)

**Queue 방식:**
- 보장: 항상 정확히 1개만 실행
- Worker가 완료 확인 후 다음 작업 시작
- 메모리 사용량: ~300MB (안정적)

#### 3. 스케줄러 전환 필요성

**AsyncIOScheduler 채택 이유:**
- BackgroundScheduler는 Thread 기반 → `asyncio.Queue` 사용 불가
- AsyncIOScheduler는 asyncio 네이티브 → Queue 자연스럽게 통합
- FastAPI의 lifespan과 완벽하게 호환

**마이그레이션 비용:**
- 코드 변경: ~200줄 (scheduler.py, main.py, utils.py)
- 호환성: APScheduler의 Job, Trigger 모두 동일하게 동작
- 리스크: 낮음 (AsyncIOScheduler는 성숙한 라이브러리)

#### 4. 트레이드오프 분석

| 항목 | Semaphore | AsyncIO Queue | 결론 |
|------|-----------|--------------|------|
| **Fallback 감소** | 경합 빈번 | 경합 없음 | ✅ 70% 개선 |
| **메모리 안정성** | 불안정 (2-3개) | 안정 (1개) | ✅ 40% 개선 |
| **Chrome 좀비** | 다수 발생 | 자동 정리 | ✅ 80% 감소 |
| **총 실행 시간** | 동시 실행 | 순차 실행 | ⚠️ 20-30% 증가 |
| **코드 복잡도** | 낮음 | 중간 | ⚠️ 약간 증가 |

**결론:** t2.micro 환경에서 메모리 안정성이 속도보다 중요

### 구현 개요

```python
# 1. AsyncIO Queue 생성
selenium_queue = asyncio.Queue(maxsize=10)

# 2. Worker 백그라운드 실행
async def selenium_job_executor():
    while True:
        job_func, bank_name = await selenium_queue.get()
        await asyncio.to_thread(job_func)  # 순차 실행
        selenium_queue.task_done()

# 3. 스케줄러에서 Queue에 추가
async def wrapper():
    await selenium_queue.put((crawler_func, bank_name))

scheduler.add_job(wrapper, IntervalTrigger(...))
```

### 결과

**예상 효과:**
- ✅ Semaphore 경합 완전 제거
- ✅ 메모리 사용량 40% 감소 (500MB → 300MB)
- ✅ 불필요한 Fallback 70% 감소
- ✅ Chrome 좀비 프로세스 80% 감소
- ⚠️ 총 실행 시간 20-30% 증가 (허용 가능)

**모니터링 필요:**
- Queue 대기 시간 추적
- CPU 사용률 (Request 크롤러 부하 분산 필요 가능성)
- 실제 Fallback 감소율 측정

**향후 개선 (Phase 2):**
- Priority Queue 도입 (중요 크롤러 우선 실행)
- Interval 최적화 (현재 7.3초~33.3초 → 60초 균등 분배)
- Request 크롤러 부하 분산 (CPU throttling 완화)

### 참고 문서

**상세 구현 기록:** [MAINTENANCE_2025-11-06.md](MAINTENANCE_2025-11-06.md)

---

## ADR-007: Selenium 크롤러 우선순위 기반 실행 (Priority Queue + Timeout)

**날짜:** 2025-11-08
**상태:** 수락됨 ✅

### 상황

**배경:**
- ADR-006에서 구축한 AsyncIO Queue 순차 실행 시스템이 메모리 압박으로 인한 타임아웃 문제 발생
- Swap 메모리 사용 (27.8%, 569MB)으로 Chrome 프로세스 생성 시 I/O 지연
- 크롤링 시간 2-3배 증가 → 타임아웃 빈번히 발생

**문제:**
1. 느린 크롤러(IBK 29초, NH 32초)가 빠른 크롤러(Hana 7초) 실행을 지연
2. 단순 Queue는 FIFO → 실행 순서 최적화 불가
3. 고정 타임아웃(30-90초)이 Swap I/O 지연을 고려하지 않음

### 결정

**Priority Queue + Timeout 전략 도입**

#### 1. 우선순위 기반 실행 순서
```python
SELENIUM_PRIORITY_MAP = {
    "hana": 0,     # 가장 빠름 (7.3초) → 1순위
    "ibk": 1,      # 중간 (29.1초) → 2순위
    "nh": 2,       # 느림 (32.7초) → 3순위
    "sc": 3,       # 느림 (33.3초) → 4순위
    "shinhan": 4,  # 가장 느림 (31초) → 5순위
}
```

#### 2. 개별 타임아웃 설정
```python
SELENIUM_TIMEOUT_MAP = {
    "hana": 60,    # 빠른 크롤러: 짧은 타임아웃
    "ibk": 90,     # 중간 크롤러: 여유있게
    "nh": 90,
    "sc": 90,
    "shinhan": 120, # 느린 크롤러: 가장 길게
}
```

#### 3. 자동 재시도 메커니즘
```python
# 실패 시 우선순위 +1000 (정상 작업 후순위)
if not success and not is_retry:
    retry_priority = priority + 1000
    await selenium_queue.put((retry_priority, time.time(), job_func, bank_name, True))
```

### 근거

#### Round-Robin 방식과 비교

| 방식 | 전체 처리 시간 | 격리 수준 | 확장성 | 복잡도 |
|------|--------------|---------|--------|--------|
| **Round-Robin** | 느림 | 낮음 | 낮음 | 낮음 |
| **Priority Queue** | 빠름 | 높음 | 높음 | 중간 |

**Priority Queue를 선택한 이유:**
1. **처리 속도 향상**: 빠른 크롤러 우선 실행으로 평균 대기 시간 단축
2. **느린 크롤러 격리**: 타임아웃으로 전체 시스템 영향 최소화
3. **확장성**: 새 크롤러 추가 시 우선순위만 설정하면 됨
4. **자동 복구**: 실패 작업 자동 재시도로 안정성 향상

#### 타임아웃 증가 정당성
- **관찰 데이터**: Swap I/O 시 실제 크롤링 시간 2-3배 증가
- **근거**: 30초 → 60초 증가는 Swap 지연을 충분히 흡수
- **트레이드오프**: 타임아웃 시간 증가 vs 불필요한 실패 감소
  - ✅ 선택: 타임아웃 증가 (안정성 우선)

### 결과

#### 성공 지표 (60초 모니터링)
- ✅ **타임아웃 발생**: 0건 (이전: 80% 실패율)
- ✅ **크롤러 실행 시간**: 모두 정상 범위 (7-17초)
- ✅ **Priority Queue 순서**: 정확히 보장 (hana → ibk → nh → sc → shinhan)

#### 시스템 리소스
- RAM: 688MB / 957MB (71.9%) - 안정적
- Swap: 578MB / 2047MB (28.2%) - 타임아웃 없이 정상 작동

#### 장점
- ✅ 타임아웃 문제 완전 해결
- ✅ 실행 순서 보장으로 예측 가능성 향상
- ✅ 자동 재시도로 일시적 실패 복구
- ✅ 크롤러별 맞춤형 타임아웃 설정 가능

#### 단점
- ⚠️ 우선순위 낮은 크롤러 대기 시간 증가 (허용 가능)
- ⚠️ PriorityQueue 복잡도 증가 (튜플 형식: priority, timestamp, data)

#### 향후 재검토 시점
1. **EC2 인스턴스 업그레이드 시** (t2.micro → t3.small):
   - 타임아웃 값 재조정 필요 (Swap 사용 감소 예상)
   - 우선순위 맵 재평가 (실행 시간 변화 가능)

2. **사용자 500명 이상 도달 시**:
   - Priority Queue → Multiple Queue (크롤러 그룹별)
   - 동적 우선순위 조정 (최근 성공률 기반)

3. **새 크롤러 추가 시**:
   - 평균 실행 시간 측정 후 우선순위 할당
   - 타임아웃 값 실험 (baseline × 2-3배)

### 대안 검토 (기각)

#### 1. 메모리 확장만으로 해결
- **기각 이유**: EC2 인스턴스 업그레이드 비용 발생
- **판단**: 소프트웨어 최적화로 우선 해결 시도

#### 2. Chrome 프로세스 풀링
- **기각 이유**: 프로세스 누수 위험, 복잡도 증가
- **판단**: 현재 단계에서 과도한 엔지니어링

#### 3. 크롤러 실행 간격 증가
- **기각 이유**: 실시간성 저하 (IN 모드 7-33초 간격 유지 필요)
- **판단**: 사용자 경험 우선

### 참고 문서

**상세 구현 기록:** [MAINTENANCE_2025-11-08.md](MAINTENANCE_2025-11-08.md)

---

## ADR-009: 3-Tier 스케줄링 아키텍처 (t3.small/medium 최적화)

**날짜:** 2025-11-10
**상태:** 수락됨 ✅

### 상황

**배경:**
- t2.micro (1GB RAM) → t3.small/medium (2-4GB RAM) 업그레이드 계획
- 기존: interval 기반 스케줄링 (IN 모드 10초, OUT 모드 100초)
- 문제점: OUT 모드에서 6-8개 크롤러 동시 실행 → CPU 스파이크

**요구사항:**
1. **IN 모드**: Broadcasting(10초)과 동기화, 매 Broadcasting마다 최신 데이터 반영
2. **OUT 모드**: 리소스 완전 분산, 동시 실행 0개
3. **확장성**: t3.small/medium 환경에 최적화
4. **단순성**: 코드 복잡도 최소화

### 결정

**3-Tier cron 기반 스케줄링 아키텍처 채택**

**Tier A (investing):** 최우선
- IN: 10초마다 (Broadcasting 3초 전)
- OUT: 10분마다 (07, 17, 27, 37, 47, 57분)

**Tier B (kb, hana, woori, bs, citi):** 중요
- IN: 20-60초마다 (Broadcasting 3-7초 전, 엇갈림)
- OUT: 10-60분마다 (완전 분산)

**Tier C (shinhan, ibk, nh, sc):** Selenium
- IN: interval (38.3-150초, Broadcasting 독립)
- OUT: 60분마다 (완전 분산)

### 근거

#### 1. IN 모드: cron 절대 시간 동기화

**Broadcasting 타임라인:**
```
00초, 10초, 20초, 30초, 40초, 50초 (10초 간격)
```

**크롤러 실행 시점:**
```python
# A Group: 3초 전
investing: cron(second='7,17,27,37,47,57')
# 목표: 07초 실행 → 09초 완료 → 10초 Broadcasting 반영

# B Group: 5초 전 (10초 엇갈림)
kb:   cron(second='5,25,45')
hana: cron(second='15,35,55')

# B Group: 7초 전 (20초씩 엇갈림)
woori: cron(minute='*', second='13')
bs:    cron(minute='*', second='33')
citi:  cron(minute='*', second='53')

# C Group: interval (Broadcasting 독립)
shinhan: interval(38.3초)
ibk:     interval(55.5초)
nh:      interval(90초)
sc:      interval(150초)
```

**리소스 분산 효과:**
```
타임라인 (1분 기준):
05초: kb
07초: investing
13초: woori
15초: haba
17초: investing
...
```
→ **최대 동시 실행: 1개**

#### 2. OUT 모드: cron 시간 단위 완전 분산

**크롤러 실행 시점:**
```python
# A Group: 10분마다
investing: cron(minute='7,17,27,37,47,57')

# B Group: 10분마다
kb:   cron(minute='5,15,25,35,45,55')
hana: cron(minute='0,10,20,30,40,50')

# B Group: 60분마다
woori: cron(minute='13')
bs:    cron(minute='33')
citi:  cron(minute='53')

# C Group: 60분마다
shinhan: cron(minute='23')
ibk:     cron(minute='43')
nh:      cron(minute='3')
sc:      cron(minute='36')
```

**1시간 타임라인:**
```
00분: hana
03분: nh
05분: kb
07분: investing
10분: hana
13분: woori
15분: kb
17분: investing
20분: hana
23분: shinhan
25분: kb
27분: investing
30분: hana
33분: bs
35분: kb
36분: sc
37분: investing
40분: hana
43분: ibk
45분: kb
47분: investing
50분: hana
53분: citi
55분: kb
57분: investing
```
→ **최대 동시 실행: 1개**

#### 3. 성능 분석

| 지표 | 기존 (interval) | 새 방식 (cron) | 개선 |
|------|----------------|---------------|------|
| OUT 모드 동시 실행 | 6-8개 | 1개 | 85% 감소 |
| CPU 스파이크 | 있음 | 없음 | 100% 제거 |
| 예측 가능성 | 낮음 | 높음 | 매시간 같은 분 |
| 서버 재시작 안전성 | 낮음 | 높음 | 절대 시간 기반 |

### 결과

**장점:**
- ✅ **OUT 모드 리소스 완전 분산**: 동시 실행 0개
- ✅ **IN 모드 Broadcasting 동기화**: X초 전 실행으로 실시간 반영
- ✅ **예측 가능성**: 매시간 같은 분에 실행
- ✅ **서버 재시작 안전**: 절대 시간 기반, 시차 설정 불필요
- ✅ **코드 단순성**: cron 표현식만으로 완전 제어

**단점:**
- ⚠️ **OUT 모드 배수**: 정확히 10배 아님 (환율 변동 드묾, 영향 미미)
- ⚠️ **IN/OUT 코드 차이**: cron vs interval 혼용 (하지만 명확히 분리)

**향후 재검토 시점:**
- t3.small에서도 OUT 모드 리소스 부족 시
- 더 세밀한 Broadcasting 동기화 필요 시

### 관련 결정

- [ADR-007](DECISIONS.md#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout): Priority Queue 유지 (C Group만 사용)
- [ADR-008](DECISIONS.md#adr-008-queue-압력-완화-전략-실시간성-vs-완전성): Queue 압력 완화 정책 유지

---

## ADR-008: Queue 압력 완화 전략 (실시간성 vs 완전성)

**날짜:** 2025-11-10
**상태:** 수락됨 ✅

### 상황

**배경:**
- ADR-007의 Priority Queue + Timeout 시스템이 Queue 100% 포화 문제 발생
- IBK Worker가 6시간 동안 stuck (asyncio.wait_for()가 blocking thread 종료 불가)
- NH crawler가 66초 소요 (실시간 요구사항 미충족)
- Queue 크기 50/50 (100%) 지속적 포화

**근본 원인:**
```
Input Rate > Processing Rate

Input:  크롤러 5개 × 평균 8초 간격 = 0.625 작업/초
Output: 평균 처리 시간 20초 = 0.05 작업/초

→ Queue는 계속 채워지고, Worker는 따라가지 못함
→ 시간이 지날수록 대기 작업 누적 → 100% 포화
```

**시스템 제약:**
- AWS t2.micro (1GB RAM)
- Swap 메모리 28.2% 사용 → I/O 지연 발생
- NH crawler 실제 실행 시간: 66초 (타임아웃 90초보다 짧음)

### 결정

**3단계 Queue 압력 완화 전략 채택**

#### 1. 타임아웃 50% 감축 (실시간성 강화)
```python
# app/crawlers/constants.py
SELENIUM_TIMEOUT_MAP = {
    "hana": 30,    # 실시간성 우선
    "shinhan": 45,
    "nh": 45,
    "ibk": 45,
    "sc": 45,
}
```

**근거:**
- 실시간 서비스에서 90초 대기는 너무 김
- 타임아웃으로 느린 크롤러를 조기 종료 → Queue 정체 방지
- 재시도 메커니즘이 있으므로 데이터 손실 최소화

#### 2. Queue 크기 50% 감축 (메모리 최적화)
```python
# app/scheduler.py
selenium_queue = asyncio.PriorityQueue(maxsize=25)  # 50 → 25
```

**근거:**
- 80% 압력 완화 정책(20개)을 고려하면 25면 충분
- 과도한 작업 누적 방지 (50개 대기는 과도함)
- 메모리 절약 (Queue 객체 크기 감소)

#### 3. 80% 압력 완화 정책 (과부하 방지)
```python
# app/scheduler.py
def enqueue_selenium_job(job_func, bank_name):
    current_size = selenium_queue.qsize()
    max_size = 25

    if current_size >= 20:  # 80% 초과 (20/25)
        logger.warning(
            f"🚫 [{bank_name}] Queue 압력 초과로 skip ({current_size}/{max_size})"
        )
        return  # 새 작업 거부
```

**근거:**
- Queue 100% 포화 → APScheduler 블로킹 발생
- 80% 이상 시 새 작업 거부 → 시스템 안정성 유지
- 거부된 작업은 다음 스케줄에서 재시도 (데이터 손실 없음)

#### 4. Worker 헬스체크 시스템 (Stuck 방지)
```python
# app/scheduler.py
async def check_worker_health():
    elapsed = time.time() - selenium_worker_last_heartbeat
    max_job_time = 180  # 3분

    if elapsed > max_job_time:
        logger.error(f"🚨 Selenium Worker 멈춤 감지...")
        selenium_worker_task.cancel()  # Worker 강제 취소
        selenium_worker_task = loop.create_task(selenium_job_executor())  # 재시작
```

**근거:**
- `asyncio.wait_for()`는 blocking thread를 종료하지 못함
- 3분 이상 같은 작업 처리 시 Worker 재시작
- 자동 복구로 운영 안정성 향상

#### 5. 크롤러 스케줄 재조정 (부하 분산)
```python
# app/scheduler.py (사용자 최종 조정)
SELENIUM_BASED_TASKS = [
    ("hana", ..., 20),     # 빠른 크롤러: 높은 빈도
    ("shinhan", ..., 60),  # 느린 크롤러: 중간 빈도
    ("ibk", ..., 60),
    ("nh", ..., 90),       # 가장 느린 크롤러: 낮은 빈도
    ("sc", ..., 120),      # 가장 불안정: 최소 빈도
]
```

**근거:**
- 느린 크롤러의 실행 빈도를 줄여 Queue 압박 감소
- Input Rate 감소 → Processing Rate와 균형

### 근거

#### 대안 검토

| 대안 | 장점 | 단점 | 채택 |
|------|------|------|------|
| **하드웨어 업그레이드** (t3.small, 2GB RAM) | 근본 해결, Swap 미사용 | 비용 $15/월 | ❌ |
| **타임아웃 증가** | 데이터 완전성 ↑ | 실시간성 ↓, Queue 정체 | ❌ |
| **Queue 크기 증가** | 작업 손실 ↓ | 메모리 압박 ↑, 근본 미해결 | ❌ |
| **압력 완화 + 타임아웃 감축** | 실시간성 ↑, 시스템 안정 | 일부 데이터 손실 가능 | ✅ |

**선택 이유:**
1. 코드 최적화로 하드웨어 비용 절감 (프리티어 유지)
2. 실시간 서비스 특성상 실시간성 > 완전성
3. 재시도 메커니즘으로 데이터 손실 최소화
4. 시스템 안정성 우선 (100% 포화보다 80% 안정이 나음)

#### 트레이드오프 분석

| 항목 | 변경 전 (ADR-007) | 변경 후 (ADR-008) | 영향 |
|------|------------------|-----------------|------|
| **Queue 포화** | 100% (50/50) | ~80% (20/25) | ✅ 안정화 |
| **타임아웃** | 60-120초 | 30-60초 | ✅ 실시간성 향상 |
| **데이터 완전성** | 높음 | 중간 | ⚠️ 일부 손실 가능 |
| **시스템 안정성** | 불안정 (Worker stuck) | 안정 (헬스체크) | ✅ 자동 복구 |
| **메모리 사용** | Queue 50개 | Queue 25개 | ✅ 절약 |

### 결과

#### 성공 지표 (모니터링 결과)

**Queue 안정화:**
- ✅ Queue 사용률: 100% (50/50) → 80% (20/25)
- ✅ "Queue 압력 초과" 경고: 정상 작동 (압력 완화 작동 중)

**실시간성 향상:**
- ✅ NH crawler: 66초 → 45초 타임아웃 (초과 시 조기 종료)
- ✅ Hana crawler: 4-25초 (30초 타임아웃 내 완료)

**Worker 안정성:**
- ✅ 헬스체크: 60초마다 자동 점검
- ✅ Stuck Worker: 180초 초과 시 자동 재시작

**타임아웃 동작:**
- ✅ Shinhan: 60초 타임아웃 정상 작동 → 재시도 큐 진입

#### 장점
- ✅ Queue 포화 방지 (80% 안정 운영)
- ✅ 실시간성 강화 (타임아웃 50% 감축)
- ✅ Worker 자동 복구 (헬스체크 시스템)
- ✅ 메모리 절약 (Queue 크기 50% 감축)
- ✅ 프리티어 유지 (하드웨어 비용 $0)

#### 단점
- ⚠️ 일부 크롤링 실패 가능 (타임아웃 엄격화)
- ⚠️ 80% 초과 시 작업 거부 (다음 스케줄에서 재시도)
- ⚠️ 데이터 완전성 약간 감소 (실시간성과 트레이드오프)

#### 핵심 전략: **실시간성 > 완전성**

**이유:**
1. 환율 서비스는 **실시간성이 핵심 가치**
   - 60-90초 대기보다 빠른 타임아웃으로 새 데이터 수집이 나음
2. **재시도 메커니즘** 존재
   - 실패 시 우선순위 +1000으로 자동 재시도
   - 영구적 데이터 손실 아님
3. **폴백 URL** 존재
   - 대부분 크롤러에 2-3개 폴백 URL 구현됨
   - 특정 은행 실패해도 서비스 지속 가능

### 향후 재검토 시점

1. **EC2 인스턴스 업그레이드 시** (t2.micro → t3.small):
   - Swap 사용 감소 → 타임아웃 재조정 필요
   - 크롤러 실행 시간 감소 → Queue 크기 재평가

2. **데이터 완전성 요구 증가 시**:
   - 타임아웃 완화 (30-60초 → 60-120초)
   - Queue 크기 증가 (25 → 50)
   - 단, 하드웨어 업그레이드 선행 필요

3. **사용자 피드백 발생 시**:
   - "특정 은행 데이터가 자주 누락됨" → 해당 은행 타임아웃 증가
   - "환율 업데이트가 느림" → 타임아웃 추가 감축

### 결론

**현 단계(프리티어, 초기 사용자)에서는 실시간성과 시스템 안정성을 우선하며, 사용자 증가 및 하드웨어 업그레이드 시점에 데이터 완전성을 강화하는 전략이 적절함.**

---

## ADR-010: WebSocket Broadcasting 스케줄링 방식

**날짜:** 2025-11-12
**상태:** 수락됨 ✅

### 상황

**배경:**
- ADR-009에서 3-Tier 스케줄링 아키텍처 설계
- 전제조건: "WebSocket Broadcasting이 매분 00, 10, 20, 30, 40, 50초에 정확히 실행"
- 크롤러들은 Broadcasting 기준으로 X초 전에 실행 (A Group: 3초 전, B Group: 5-7초 전)

**문제:**
- 기존 Broadcasting 구현: `asyncio.sleep(10)` 무한 루프
- 서버 시작 시점에 따라 부정확한 시간에 실행 (예: 03, 13, 23초 또는 05, 15, 25초)
- **크롤러 동기화 불가능**: Broadcasting이 언제 실행될지 예측 불가능

**예시:**
```python
# 기존 구현 (main.py)
async def broadcast_rates():
    while True:
        await asyncio.sleep(10)  # 서버 시작 시점 기준 상대적 시간
        # 브로드캐스트 로직
```

서버 시작: 14:25:03 → Broadcasting: 14:25:13, 14:25:23, 14:25:33, ...
서버 시작: 14:25:07 → Broadcasting: 14:25:17, 14:25:27, 14:25:37, ...

→ **ADR-009의 전제조건 미충족**

### 결정

**APScheduler cron job으로 Broadcasting 스케줄링**

```python
# scheduler.py
from app.main import broadcast_rates_once

scheduler.add_job(
    broadcast_rates_once,  # async 함수 직접 등록
    CronTrigger(second='0,10,20,30,40,50', timezone=KST),
    id="websocket_broadcast",
    max_instances=1,
    misfire_grace_time=5
)
```

```python
# main.py
async def broadcast_rates_once():
    """환율 데이터 한 번 브로드캐스트 (APScheduler에서 매분 00, 10, 20, 30, 40, 50초에 호출)"""
    if not manager.active_connections:
        return

    # 변경사항 체크 후 브로드캐스트
    # ...
```

### 근거

#### 1. APScheduler cron job (채택)

**장점:**
- ✅ **정확한 시간 보장**: cron 표현식으로 절대 시간 지정
- ✅ **크롤러와 일관성**: 모든 스케줄링이 APScheduler로 통일
- ✅ **ADR-009 전제조건 충족**: 크롤러 동기화 가능
- ✅ **코드 단순성**: 크롤러와 동일한 패턴

**단점:**
- ⚠️ APScheduler 의존성 증가 (이미 사용 중이므로 영향 없음)

#### 2. asyncio.sleep(10) 무한 루프 (기존)

**장점:**
- ✅ 구현 간단
- ✅ 외부 의존성 없음

**단점:**
- ❌ **부정확한 시간**: 서버 시작 시점에 의존
- ❌ **크롤러 동기화 불가**: 예측 불가능한 실행 시점
- ❌ **ADR-009 위반**: 전제조건 미충족

#### 3. 동적 sleep 계산

다음 00, 10, 20, 30, 40, 50초까지 남은 시간을 계산하여 sleep:

```python
async def broadcast_rates():
    while True:
        now = datetime.now(KST)
        target_seconds = [0, 10, 20, 30, 40, 50]
        # 다음 target까지 sleep 시간 계산
        sleep_duration = calculate_sleep(now, target_seconds)
        await asyncio.sleep(sleep_duration)
        # 브로드캐스트
```

**장점:**
- ✅ 정확한 시간 (약간의 오차 가능)
- ✅ 기존 asyncio task 구조 유지

**단점:**
- ❌ **복잡도 증가**: sleep 계산 로직 추가
- ❌ **오차 누적 가능**: sleep 시간이 정확하지 않으면 오차 누적
- ❌ **검증 어려움**: 계산 로직 테스트 필요
- ❌ **크롤러와 패턴 불일치**: 크롤러는 cron, Broadcasting은 동적 계산

### 결과

**장점:**
- ✅ **ADR-009 전제조건 충족**: 크롤러 동기화 가능
- ✅ **정확한 시간**: 매분 00, 10, 20, 30, 40, 50초 정확히 실행
- ✅ **코드 일관성**: 크롤러와 동일한 스케줄링 패턴
- ✅ **유지보수성**: 모든 스케줄링 로직이 `scheduler.py`에 집중

**단점:**
- 없음 (이미 APScheduler 사용 중)

**검증 결과 (2025-11-12):**
```
01:26:40 - Running job "broadcast_rates_once" ✅
01:26:50 - Running job "broadcast_rates_once" ✅
01:27:00 - Running job "broadcast_rates_once" ✅
01:27:10 - Running job "broadcast_rates_once" ✅
```

**향후 재검토 시점:**
- APScheduler를 다른 스케줄러로 교체 시
- Broadcasting 주기 변경 시 (10초 → 5초 등)

### 관련 결정

- [ADR-009](#adr-009-3-tier-스케줄링-아키텍처-t3smallmedium-최적화): Broadcasting 동기화 전제조건
- [ADR-001](#adr-001-websocket-구현---python-fastapi-vs-nodejs): WebSocket 구현 방식

---

## ADR-011: Selenium Timeout 최적화 - Race Condition 제거

**날짜:** 2025-11-13
**상태:** 수락됨

### 상황

Selenium 크롤러에서 timeout race condition 발생:
- **Selenium timeout (60초)** > **Scheduler timeout (45초)** → 15초 좀비 프로세스 윈도우 발생
- Worker health check가 60초마다 실행 → stuck 감지 지연 (최대 60초)
- Chrome 프로세스 수명이 180초로 너무 길어 메모리 누적

**문제점:**
1. Selenium이 타임아웃되기 전에 Scheduler가 먼저 종료 → Chrome 프로세스 고아화
2. Health check 주기가 길어 stuck worker 감지 지연
3. Chrome 프로세스가 오래 살아남아 메모리 압박

### 선택지

#### 옵션 1: Selenium timeout을 Scheduler timeout보다 짧게 설정

**변경사항:**
```python
# constants.py
SELENIUM_DRIVER_TIMEOUT = 42  # 60→42 (3초 안전 버퍼)
SELENIUM_TIMEOUT_MAP = {
    "hana": 45,    # 30→45 (통일)
    "shinhan": 45, # 45 (유지)
    "nh": 45,      # 45 (유지)
    "ibk": 45,     # 45 (유지)
    "sc": 45,      # 45 (유지)
}
CHROME_MAX_LIFETIME_SECONDS = 60  # 180→60
```

**장점:**
- ✅ Race condition 완전 제거 (Selenium 42초 < Scheduler 45초)
- ✅ 3초 안전 버퍼로 확실한 종료 순서 보장
- ✅ Chrome 프로세스 수명 단축 (180→60초) → 메모리 효율 개선

**단점:**
- ❌ 느린 크롤러(SC, NH)는 타임아웃으로 실패 가능 → 재시도 메커니즘으로 커버

#### 옵션 2: Scheduler timeout을 Selenium보다 길게 설정

**변경사항:**
```python
SELENIUM_TIMEOUT_MAP = {"all": 60}  # 유지
# scheduler.py에서 모든 timeout을 70초로 증가
```

**장점:**
- ✅ 기존 Selenium timeout 유지 (크롤러 안정성)

**단점:**
- ❌ Queue 정체 심화 (하나의 slow job이 70초 동안 Queue 점유)
- ❌ 실시간성 저하 (ADR-008 실시간성 우선 원칙 위배)
- ❌ 메모리 압박 증가 (Chrome 프로세스가 더 오래 살아남음)

#### 옵션 3: IntervalTrigger → CronTrigger (Health Check 강화)

**변경사항:**
```python
# scheduler.py
# 60초마다 1회 → 매분 03/23/43초 (20초 간격, 3회)
for second in ['3', '23', '43']:
    scheduler.add_job(
        check_worker_health,
        CronTrigger(second=second, timezone=KST),
        id=f"worker_health_check_{second}"
    )
```

**장점:**
- ✅ Stuck 감지 시간 67% 개선 (60초 → 20초)
- ✅ Time drift 방지 (CronTrigger는 절대 시간 기준)
- ✅ Chrome cleanup과 동시 실행 (리소스 관리 일관성)

**단점:**
- 없음 (기존 IntervalTrigger 완전 대체)

### 결정

**옵션 1 + 옵션 3 조합 채택**

**이유:**
1. **Race condition 제거**: Selenium 42초 < Scheduler 45초 → 확실한 종료 순서
2. **실시간성 우선**: ADR-008 원칙 준수, 빠른 실패로 Queue 흐름 유지
3. **Stuck 감지 강화**: 60초 → 20초 (67% 개선)
4. **메모리 효율**: Chrome 180초 → 60초, 좀비 프로세스 신속 정리
5. **이중 안전장치**: Health check에서 Worker 재시작 전 cleanup 실행

**트레이드오프:**
- 느린 크롤러 타임아웃 증가 → 재시도 메커니즘으로 데이터 손실 방지
- 실시간성(빠른 응답) > 완전성(모든 크롤러 성공)

**검증 지표 (기대 효과):**
```
[Before]
- 좀비 Chrome: 1-3개 상시 존재
- Stuck 감지: 60초 평균
- 타임아웃: 불확실한 종료 순서

[After]
- 좀비 Chrome: 0개 (20초마다 cleanup)
- Stuck 감지: 20초 평균 (67% 개선)
- 타임아웃: 확실한 종료 순서 (42 < 45)
```

### 결과

**성능 개선:**
- Race condition 완전 제거
- Worker health check 주기 3배 증가
- Chrome 프로세스 수명 3분의 1로 단축
- Stuck worker 감지 시간 67% 개선

**시스템 안정성:**
- 타임아웃 순서 명확화로 좀비 프로세스 방지
- CronTrigger로 time drift 제거
- 이중 안전장치 (health check + cleanup)

**향후 재검토 시점:**
- 크롤러 성능 개선으로 45초 내 완료 가능 시 (timeout 추가 단축 고려)
- Queue 압력이 완화되어 여유가 생길 시 (timeout 증가 고려)

### 관련 결정

- [ADR-008](#adr-008-queue-압력-완화-전략-실시간성-vs-완전성): 실시간성 우선 원칙
- [ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout): Timeout 전략
- [ADR-006](#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue): Queue 기반 순차 실행

---

## ADR-012: Selenium 폴백 subprocess 격리 (Chrome 프로세스 좀비화 방지)

**날짜:** 2025-11-14
**상태:** 수락됨

### 상황

**Phase 1: Queue 기반 Selenium 크롤러 (이전 세션에서 완료)**
- **날짜:** 2025-11-14 03:22 KST
- **대상:** shinhan, ibk, nh, sc (순수 Selenium 크롤러)
- **해결:** `asyncio.create_subprocess_exec()` 기반 subprocess 격리
- **결과:** Chrome 프로세스 20개 누적 문제 해결
- **참고:** [ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout)

**Phase 2: Selenium 폴백 크롤러 (현재 세션)**
- **날짜:** 2025-11-14 04:20 KST
- **발견:** hana/woori도 Selenium 폴백을 APScheduler thread에서 직접 실행
- **위험:** 동일한 구조적 문제 - Thread blocking 시 `driver.quit()` 미실행
- **기존 구조:**
  ```python
  # hana.py (APScheduler thread에서 실행)
  def crawl_hana():
      try:
          request_crawl()  # Request 실패
      except:
          driver = create_selenium_driver()  # Selenium 폴백
          driver.get(url)  # <- blocking 시 멈춤
      finally:
          driver.quit()  # <- 도달 못함
  ```

### 결정

**나머지 Selenium 폴백도 subprocess로 격리 실행**
- hana/woori의 2차 폴백(Selenium)을 subprocess로 전환
- 1차(Request), 3차(MIBANK)는 그대로 유지 (성능 우선)

### 대안 검토

#### Option 1: Selenium 폴백 subprocess 격리 ✅ 채택

**구조:**
```python
# hana.py
def crawl_hana():
    try:
        request_crawl()  # 1차: Request
    except:
        subprocess.run(
            ["python", "-m", "app.crawlers.runner", "hana_selenium"],
            timeout=45  # 타임아웃 보장
        )
```

**장점:**
- ✅ Request 성능 유지 (빠름)
- ✅ Selenium만 subprocess 격리 (안전)
- ✅ 타임아웃 보장 (`proc.kill()`)
- ✅ 기존 인프라 재활용 (runner.py, execute_with_timeout)

**단점:**
- ⚠️ subprocess 오버헤드 (1-2초)
- ⚠️ 코드 수정 필요 (3개 파일)

#### Option 2: hana/woori 전체를 Selenium Queue에 추가

**구조:**
```python
def crawl_hana():
    try:
        request_crawl()
    except:
        enqueue_selenium_job('hana')  # Queue 추가
```

**장점:**
- ✅ 순차 실행 보장
- ✅ 우선순위 관리 가능

**단점:**
- ❌ Queue 압력 증가
- ❌ 대기 시간 발생 (실시간성 저하)
- ❌ Request + Queue 혼합 복잡도

#### Option 3: Thread timeout wrapper

**구조:**
```python
with ThreadPoolExecutor() as executor:
    future = executor.submit(selenium_crawl)
    future.result(timeout=45)
```

**장점:**
- ✅ 코드 수정 최소화

**단점:**
- ❌ Thread kill 불가능 (Python 한계)
- ❌ Chrome 프로세스 여전히 좀비화
- ❌ Thread pool 고갈 위험

### 근거

#### 1. Python Thread 한계

**Thread blocking 시나리오:**
```
1. APScheduler thread 시작
2. Chrome 생성 (PID 1234)
3. driver.get(url) → 네트워크 응답 대기
4. 60초 경과... thread 계속 blocking
5. finally 블록 도달 못함
6. Chrome(PID 1234) 좀비화
```

**Python의 구조적 한계:**
- Thread 강제 종료 불가능 (GIL 설계)
- blocking된 thread는 영원히 대기
- `driver.quit()` 미실행 → Chrome 프로세스 고아

#### 2. Subprocess 격리의 안전성

**Subprocess 타임아웃 시나리오:**
```
1. Main thread: subprocess 생성 (PID 1000)
2. Subprocess: Chrome 생성 (PID 1234)
3. Subprocess: driver.get(url) 대기
4. 45초 경과
5. Main thread: subprocess.TimeoutExpired 감지
6. Main thread: proc.kill() 호출
7. OS: Subprocess(PID 1000) 종료 → Chrome(PID 1234) 함께 종료
8. 완전 정리 완료 ✅
```

**핵심 차이:**
- ✅ Subprocess는 OS 레벨 강제 종료 가능
- ✅ 자식 프로세스(Chrome) 함께 종료 (프로세스 트리)
- ✅ `driver.quit()` 실행 여부 무관

#### 3. 성능 vs 안정성 트레이드오프

**Request 유지 장점:**
- ⚡ 빠른 응답 (0.5초)
- 💰 낮은 리소스 사용
- 🎯 대부분 성공 (90%+)

**Selenium 폴백 필요성:**
- 🛡️ 안정성 보장 (Request 실패 시)
- 🔄 자동 복구 (타임아웃 보장)
- 📊 드물게 사용 (10% 미만)

→ **Request 성능 + Selenium 안전성 동시 확보**

### 구현 세부사항

#### 1. runner.py 확장

```python
CRAWLER_MAP = {
    # 기존 Queue 기반
    'shinhan': shinhan.crawl_and_save_shinhan_bank_exchange_rates,
    'ibk': ibk.crawl_and_save_ibk_bank_exchange_rates,
    'nh': nh.crawl_and_save_nh_bank_exchange_rates,
    'sc': sc.crawl_and_save_sc_bank_exchange_rates,

    # 신규 폴백 엔트리포인트
    'hana_selenium': hana.crawl_and_save_hana_routine_selenium_entrypoint,
    'woori_selenium': woori.crawl_and_save_woori_routine_selenium_entrypoint,
}
```

#### 2. hana.py/woori.py 수정

```python
def crawl_and_save_hana_bank_exchange_rates():
    try:
        # 1차: Request (빠름)
        crawl_and_save_routine(HANA_BANK_URL, HANA_BANK_SELECTORS, db)
    except:
        # 2차: Selenium subprocess (안전)
        _run_selenium_subprocess_fallback('hana_selenium', timeout=45)
    except:
        # 3차: MIBANK Request (최종 폴백)
        crawl_and_save_routine(MIBANK_HANA_URL, MIBANK_SELECTORS, db)

def _run_selenium_subprocess_fallback(name: str, timeout: int):
    result = subprocess.run(
        [sys.executable, "-m", "app.crawlers.runner", name],
        capture_output=True,
        timeout=timeout
    )
```

#### 3. 엔트리포인트 추가

```python
def crawl_and_save_hana_routine_selenium_entrypoint():
    """subprocess 엔트리포인트 (DB 자체 관리)"""
    db = SessionLocal()
    try:
        return crawl_and_save_hana_routine_selenium(
            SECOND_HANA_BANK_URL,
            SECOND_HANA_BANK_SELECTORS,
            db
        )
    finally:
        db.close()
```

### 검증 결과

**Manual Test:**
```
✅ hana_selenium subprocess 정상 실행 (1분 51초)
✅ Chrome 프로세스 완전 정리 (0개 남음)
✅ Container: healthy
```

**Production Logs (15분):**
```
✅ nh, sc, shinhan, ibk 타임아웃 작동 확인
✅ "subprocess killed (Chrome 포함 전체 정리)" 로그 확인
✅ Chrome 프로세스 수: 0-1개 (정상 범위)
```

### 결과

**모든 Selenium 크롤러 subprocess 격리 완료:**
- **Phase 1 (이전)**: shinhan, ibk, nh, sc → AsyncIO Queue + subprocess
- **Phase 2 (현재)**: hana_selenium, woori_selenium → 동기 subprocess fallback

**최종 아키텍처:**
```
크롤러 유형별 Selenium 실행 방식
├─ Queue 기반 (순수 Selenium)
│  ├─ shinhan: AsyncIO subprocess (38초마다)
│  ├─ ibk: AsyncIO subprocess (55초마다)
│  ├─ nh: AsyncIO subprocess (90초마다)
│  └─ sc: AsyncIO subprocess (150초마다)
│
└─ 폴백 기반 (하이브리드)
   ├─ hana: Request → 실패 시 subprocess → MIBANK
   └─ woori: Request → 실패 시 subprocess → MIBANK

공통: runner.py 엔트리포인트, 45초 타임아웃, proc.kill() 보장
```

**안정성 개선:**
- ✅ 모든 Selenium 실행이 subprocess 격리
- ✅ Chrome 프로세스 누적 위험 완전 제거
- ✅ 타임아웃 보장 (45초, OS 레벨 강제 종료)
- ✅ Request 성능 유지 (하이브리드 크롤러)

**향후 재검토 시점:**
- kb, bs, citi에서 Selenium 폴백 추가 시 (동일 패턴 적용)
- 폴백 사용 빈도 증가로 성능 이슈 발생 시

### 관련 결정

- [ADR-011](#adr-011-selenium-timeout-최적화---race-condition-제거): Timeout 최적화
- [ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout): Subprocess 기반 격리
- [ADR-006](#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue): Queue 순차 실행

---

## 문서 히스토리

- 2025-10-11: ADR-001, ADR-002, ADR-003 작성 (아키텍처 설계 단계)
- 2025-10-14: ADR-004 작성 (로그 시스템 단순화)
- 2025-10-21: ADR-005 작성 (Docker Compose 기반 배포)
- 2025-10-26: ADR 작성 정책 추가
- 2025-11-06: ADR-006 작성 (AsyncIO Queue 기반 Selenium 순차 실행)
- 2025-11-08: ADR-007 작성 (Priority Queue + Timeout 전략)
- 2025-11-10: ADR-008 작성 (Queue 압력 완화 전략 - 실시간성 vs 완전성)
- 2025-11-10: ADR-009 작성 (3-Tier 스케줄링 아키텍처 - t3.small/medium 최적화)
- 2025-11-12: ADR-010 작성 (WebSocket Broadcasting 스케줄링 방식 - asyncio.sleep vs APScheduler cron)
- 2025-11-13: ADR-011 작성 (Selenium Timeout 최적화 - Race Condition 제거)
- 2025-11-14: ADR-012 작성 (Selenium 폴백 subprocess 격리 - Chrome 프로세스 좀비화 방지)
