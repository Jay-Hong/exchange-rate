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

실시간 환율 정보를 클라이언트에게 전송하기 위한 WebSocket 서버 구현이 필요함.

### 고려 사항

1. **Python FastAPI**: 크롤링과 동일 언어, 단일 코드베이스
2. **Node.js**: WebSocket 성능 최적화, 동시 접속 처리 탁월

### 결정

**Python FastAPI 채택**

**근거:**
- **유지보수성 우선**: 크롤링 로직과 WebSocket을 하나의 언어로 통합
- **초기 성능 충분**: 목표 200~500명 수준에서는 FastAPI 성능 충분
- **확장성 확보**: 병목 발생 시 Node.js로 마이그레이션 가능 (REST API 통신)

### 트레이드오프

**장점:**
- 단일 코드베이스 (Python 한 가지만 관리)
- 크롤링-DB-WebSocket 간 데이터 전달 간소화
- 팀 학습 비용 감소

**단점:**
- Node.js 대비 동시 접속 성능 열세 (500명 이상 시 병목)
- GIL로 인한 CPU-bound 작업 제약

### 향후 재검토 시점

- 사용자 500명 이상
- WebSocket 연결 지연 발생 시

---

## ADR-002: 성능 최적화 - WebSocket 압축 & 연결 풀

**날짜:** 2025-10-11
**상태:** 보류됨 (필요 시 적용)

### 상황

WebSocket 최적화 방안:
1. 메시지 압축 (gzip)
2. 데이터베이스 연결 풀
3. Redis 캐싱

### 결정

**현재 적용 안 함** (YAGNI 원칙)

**근거:**
- **초기 사용자**: 200~500명 수준에서는 기본 설정으로 충분
- **복잡도 증가**: 압축/캐싱은 설정, 디버깅, 모니터링 복잡도 증가
- **프리미엄 최적화**: 최적화는 병목이 발생한 후에 적용

### 트레이드오프

**현재 단순 구현 유지:**
- 빠른 개발, 낮은 복잡도
- 사용자 증가 시 병목 가능

**나중에 적용 (사용자 500명 이상 시):**
1. WebSocket 압축 (per-message deflate)
2. SQLAlchemy 연결 풀 크기 조정
3. Redis 캐싱 도입

### 향후 재검토 시점

- 사용자 500명 도달
- WebSocket 메시지 크기 평균 1KB 초과
- DB 쿼리 응답 시간 100ms 초과

---

## ADR-003: 알림 시스템 - WebSocket vs Push Notification

**날짜:** 2025-10-11
**상태:** 수락됨

### 상황

환율 변동 알림 방법:
1. **WebSocket 실시간 스트리밍**: 연결 유지, 자동 업데이트
2. **Push Notification**: 특정 조건 충족 시 알림

### 결정

**WebSocket 기반 실시간 스트리밍 채택**

**근거:**
- **초기 단순성**: Push Notification은 FCM/APNs 설정 + 백엔드 로직 복잡
- **사용자 경험**: 앱 실행 중에는 즉시 업데이트 (Push보다 빠름)
- **단계적 확장**: WebSocket 먼저 구현 → 필요 시 Push 추가

### Phase 1 (현재): WebSocket만 사용

```
앱 실행 중: WebSocket 연결 → 실시간 업데이트
앱 종료: 업데이트 없음
```

### Phase 2 (사용자 500명 이상): WebSocket + Push Notification 병행

**조건:**
- 급격한 환율 변동 (5% 이상)
- 사용자 설정 임계값 도달

**구현 방향:**
1. 백엔드에서 알림 조건 감지
2. FCM/APNs 전송 (iOS/Android 분기)
3. WebSocket은 그대로 유지 (앱 실행 중 실시간 업데이트)

### 트레이드오프

**WebSocket (Phase 1):**
- ✅ 빠른 구현, 단순한 아키텍처
- ❌ 앱 종료 시 알림 불가

**WebSocket + Push (Phase 2):**
- ✅ 앱 종료 상태에서도 알림
- ❌ FCM/APNs 설정 복잡도, 비용 증가

### 향후 재검토 시점

- 사용자 요청 (앱 종료 시 알림 필요)
- 환율 급변 시나리오 빈번 발생 시

---

## ADR-004: 로그 시스템 단순화 (4개→2개)

**날짜:** 2025-10-14
**상태:** 수락됨

### 상황

초기 로그 파일 설계 (4개):
- `app.log`: 일반 로그
- `error.log`: 에러만
- `crawler.log`: 크롤러 전용
- `debug.log`: 개발 디버깅

### 결정

**2개 파일로 통합**

**근거:**
- **복잡도 증가**: 파일 4개 관리 부담 (로테이션, 디스크, 모니터링)
- **실제 사용**: `app.log`와 `error.log`만 활발히 사용
- **관리 단순화**: 파일 적을수록 유지보수 용이

### 새로운 구조

- `app.log`: 모든 운영 로그 (INFO+, 크롤러 포함)
- `error.log`: 에러/경고만 (WARNING+)

**크롤러 필터링**:
```python
logger = logging.getLogger("exchange_rate.crawler.kb")
```
→ `app.log`에서 `exchange_rate.crawler.*` 패턴으로 검색

### 트레이드오프

**통합 후:**
- ✅ 파일 관리 간소화
- ✅ 로그 로테이션 단순화
- ❌ 크롤러 로그만 보려면 grep 필요

### 향후 재검토 시점

- 로그 분석 도구 도입 시 (ELK, Datadog)
- 크롤러 로그 양이 과도하게 증가 시

---

## ADR-005: Docker Compose 기반 배포 아키텍처

**날짜:** 2025-10-21
**상태:** 수락됨

### 상황

AWS 프리티어 (t2.micro) 환경에서 배포 방안:
1. **직접 배포**: Python venv + systemd
2. **Docker Compose**: 컨테이너 기반
3. **Kubernetes**: 오케스트레이션

### 결정

**Docker Compose 채택**

**근거:**
- **환경 일관성**: 로컬/프로덕션 동일 환경 보장
- **의존성 격리**: Python, Chrome, SQLite 버전 고정
- **단순성**: K8s는 초기 단계에서 과도한 복잡도

### 아키텍처

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - ENV=production
```

### 확장 계획

**Phase 1 (현재)**: 단일 컨테이너
```
app (FastAPI + 크롤러 + SQLite)
```

**Phase 2 (500명+)**: 서비스 분리
```
app (FastAPI + WebSocket)
crawler (크롤러 전용)
postgres (DB)
redis (캐시)
```

### 트레이드오프

**Docker Compose:**
- ✅ 빠른 배포, 환경 일관성
- ✅ 로컬 개발 환경과 동일
- ❌ 멀티 호스트 확장 제한 (→ K8s로 마이그레이션)

**직접 배포:**
- ✅ 리소스 오버헤드 없음
- ❌ 의존성 관리 복잡, 환경 불일치 위험

### 향후 재검토 시점

- 사용자 1000명 이상 (멀티 호스트 필요)
- AWS ECS/EKS로 마이그레이션 고려 시

---

## ADR-006: Selenium 크롤러 동시 실행 제어 - Semaphore vs AsyncIO Queue

**날짜:** 2025-11-06
**상태:** 수락됨 → **AsyncIO Queue 채택**

### 상황

Selenium 크롤러 4개 (shinhan, ibk, nh, sc)가 동시 실행되면서 **메모리 누적 문제** 발생:
- Chrome 프로세스 메모리 누적 (각 150~200MB)
- Timeout 발생 시에도 driver.quit() 실행 안됨
- 시간이 지날수록 시스템 느려짐

### 고려 사항

**1. Semaphore (기존 방식)**
```python
selenium_semaphore = asyncio.Semaphore(1)

async def crawl_selenium():
    async with selenium_semaphore:
        driver = create_selenium_driver()
        # 크롤링 로직
        driver.quit()
```

**문제점:**
- **경합 (Race Condition)**: 4개 크롤러가 세마포어 획득 경쟁
- **순서 불확실**: 먼저 실행된 크롤러가 먼저 완료되지 않음
- **Timeout 시 정리 실패**: asyncio.wait_for() 취소 시 driver.quit() 실행 안됨

---

**2. AsyncIO Queue (새로운 방식)**
```python
selenium_queue = asyncio.Queue()

# Worker (순차 처리)
async def selenium_worker():
    while True:
        crawler_func = await selenium_queue.get()
        await crawler_func()  # 하나씩 실행
        selenium_queue.task_done()

# Scheduler
def enqueue_selenium_job(crawler_func):
    selenium_queue.put_nowait(crawler_func)
```

**장점:**
- **순차 실행**: Worker가 하나씩 처리 → 동시 실행 0개
- **경합 제거**: Queue에 넣기만 하면 자동으로 순서대로 처리
- **메모리 안정화**: 동시에 1개 Chrome만 실행 → 메모리 예측 가능

---

### 결정

**AsyncIO Queue 채택**

**근거:**
1. **메모리 안정성**: 동시 Chrome 프로세스 1개로 제한
2. **경합 제거**: Semaphore 획득 경쟁 없음
3. **순차 보장**: FIFO 순서로 예측 가능한 실행

### 구현 상세

**Queue Worker (백그라운드 실행):**
```python
async def selenium_job_executor():
    while True:
        job_func = await selenium_queue.get()
        try:
            await job_func()
        except Exception as e:
            logger.exception("Selenium Queue Worker 오류")
        selenium_queue.task_done()

# FastAPI 시작 시 Worker 실행
loop = asyncio.get_event_loop()
loop.create_task(selenium_job_executor())
```

**APScheduler 연동:**
```python
def enqueue_selenium_job(crawler_func):
    selenium_queue.put_nowait(crawler_func)

# APScheduler에서 호출
scheduler.add_job(
    lambda: enqueue_selenium_job(crawl_shinhan_bank_exchange_rates),
    IntervalTrigger(seconds=38.3, timezone=KST),
    id='task_shinhan'
)
```

### 트레이드오프

| 항목 | Semaphore | AsyncIO Queue |
|------|-----------|--------------|
| **동시 실행** | 1개 (제한) | 1개 (순차) |
| **경합** | ❌ 경합 발생 | ✅ 경합 없음 |
| **순서** | ❌ 불확실 | ✅ FIFO 보장 |
| **메모리** | ⚠️ 불안정 | ✅ 안정적 |
| **복잡도** | 낮음 | 중간 |

### 실행 예시

**Semaphore (기존):**
```
00:00 - shinhan, ibk, nh, sc 동시 요청
00:00 - shinhan 세마포어 획득 (실행)
00:00 - ibk, nh, sc 대기
00:05 - shinhan 완료
00:05 - ibk, nh, sc 중 하나 획득 (순서 랜덤)
```

**AsyncIO Queue (변경 후):**
```
00:00 - shinhan Queue 추가
00:01 - ibk Queue 추가
00:02 - nh Queue 추가
00:03 - sc Queue 추가
00:00 - Worker가 shinhan 처리 시작
00:05 - shinhan 완료 → Worker가 ibk 처리 시작
00:10 - ibk 완료 → Worker가 nh 처리 시작
...
```

### 성능 영향

**Before (Semaphore):**
- 메모리: 600MB ~ 1.2GB (Chrome 4개 동시)
- CPU: 스파이크 발생 (4개 경합)

**After (AsyncIO Queue):**
- 메모리: 150MB ~ 300MB (Chrome 1개만)
- CPU: 안정적 (순차 실행)

### 향후 재검토 시점

- **사용자 1000명 이상**: 병렬 실행 필요 시 Queue Worker 2~3개로 확장
- **크롤러 10개 이상**: Queue 우선순위 도입 (중요도 기반)

### 관련 결정

- [ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout): Priority Queue + Timeout 전략
- [ADR-008](#adr-008-queue-압력-완화-전략-실시간성-vs-완전성): Queue 압력 완화 전략

---

## ADR-007: Selenium 크롤러 우선순위 기반 실행 (Priority Queue + Timeout)

**날짜:** 2025-11-08
**상태:** 수락됨

### 상황

[ADR-006](#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue)에서 AsyncIO Queue 도입 후 **새로운 문제** 발생:
- **느린 크롤러 블로킹**: sc(150초) 실행 중이면 빠른 크롤러(shinhan 38초)가 대기
- **실시간성 저하**: 중요한 데이터(hana, ibk)가 느린 크롤러에 막힘
- **Queue 포화**: 느린 작업이 Queue를 점유 → 새 작업 추가 불가

### 고려 사항

**1. PriorityQueue + 타임아웃**
- 빠른 크롤러 우선 실행
- 느린 크롤러 타임아웃으로 격리

**2. 멀티 Worker**
- Worker 2~3개로 병렬 실행
- 메모리 증가 (Chrome 2~3개 동시)

**3. 크롤러 주기 조정**
- 느린 크롤러 주기 늘림 (sc 150초 → 300초)
- 데이터 신선도 저하

---

### 결정

**PriorityQueue + 개별 타임아웃 채택**

**근거:**
1. **실시간성 우선**: 빠른 크롤러(hana, ibk)가 먼저 실행
2. **메모리 유지**: Worker 1개 유지 (동시 Chrome 1개)
3. **느린 크롤러 격리**: sc, shinhan은 타임아웃으로 빠르게 실패

### 구현 상세

**1. PriorityQueue 도입**
```python
selenium_queue = asyncio.PriorityQueue()

# 우선순위 설정 (낮을수록 높은 우선순위)
SELENIUM_PRIORITY_MAP = {
    "hana": 1,     # 가장 빠름 (Request 폴백 있음)
    "ibk": 2,
    "nh": 3,
    "sc": 4,
    "shinhan": 5   # 가장 느림
}

# Queue에 추가
priority = SELENIUM_PRIORITY_MAP.get(bank_name, 999)
selenium_queue.put_nowait((priority, time.time(), bank_name, job_func))
```

**2. 크롤러별 타임아웃**
```python
SELENIUM_TIMEOUT_MAP = {
    "hana": 45,      # Request 폴백 먼저 시도 (빠름)
    "ibk": 45,       # Request 폴백 있음
    "nh": 45,        # 단순 로직
    "sc": 45,        # 복잡하지만 타임아웃 강제
    "shinhan": 45    # 복잡하지만 타임아웃 강제
}

# Worker에서 타임아웃 적용
async def selenium_job_executor():
    while True:
        priority, timestamp, bank_name, job_func = await selenium_queue.get()
        timeout = SELENIUM_TIMEOUT_MAP.get(bank_name, 60)

        try:
            await asyncio.wait_for(job_func(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"{bank_name} 타임아웃 ({timeout}초)")

        selenium_queue.task_done()
```

**3. 재시도 로직 (실패 시)**
```python
# 타임아웃 or 실패 시 낮은 우선순위로 재시도
if not success and not is_retry:
    retry_priority = priority + 1000  # 낮은 우선순위
    selenium_queue.put_nowait((
        retry_priority, time.time(), bank_name, job_func, True
    ))
```

### 실행 예시

**기존 Queue (FIFO):**
```
Queue: [sc(150초), shinhan(38초), ibk(55초)]
00:00 - sc 시작
02:30 - sc 완료 (150초)
02:30 - shinhan 시작 (ibk는 2분 30초 대기!)
```

**PriorityQueue + Timeout:**
```
Queue: [sc(우선순위 4), shinhan(5), ibk(2)]
00:00 - ibk 시작 (우선순위 2, 가장 높음)
00:45 - ibk 완료
00:45 - sc 시작 (우선순위 4)
01:30 - sc 타임아웃 (45초) → 실패 처리 → 재시도 Queue 추가
01:30 - shinhan 시작
```

### 트레이드오프

| 항목 | 기존 Queue | PriorityQueue + Timeout |
|------|-----------|------------------------|
| **실시간성** | ❌ 느림 | ✅ 빠름 (우선순위) |
| **메모리** | ✅ 안정 | ✅ 안정 (Worker 1개) |
| **완전성** | ✅ 모든 작업 완료 | ⚠️ 타임아웃 → 재시도 |
| **복잡도** | 낮음 | 중간 |

### 성능 영향

**Before (FIFO Queue):**
- 평균 대기 시간: 1~2분
- 최악: 5분 (sc → shinhan → ibk 순서)

**After (PriorityQueue + Timeout):**
- 평균 대기 시간: 10~30초
- 최악: 1분 (빠른 작업 우선 처리)

### 향후 재검토 시점

- 타임아웃 빈도 > 10% (너무 많은 실패)
- 재시도 Queue 포화 (>50%)

### 관련 결정

- [ADR-006](#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue): AsyncIO Queue 기반 순차 실행
- [ADR-008](#adr-008-queue-압력-완화-전략-실시간성-vs-완전성): Queue 압력 완화 전략

---

## ADR-009: 3-Tier 스케줄링 아키텍처 (t3.small/medium 최적화)

**날짜:** 2025-11-10
**상태:** 수락됨

### 상황

기존 스케줄링 구조 문제:
- **IN/OUT 모드만 존재**: 너무 단순함 (영업시간 vs 주말)
- **Interval 기반 스케줄링**: 예측 불가능 (38.3초마다 → 누적 오차)
- **동시 실행 스파이크**: 여러 크롤러가 동시에 실행되면 CPU 과부하

### 고려 사항

**1. Interval 기반 (기존)**
```python
IntervalTrigger(seconds=38.3, timezone=KST)
```
- 문제: 누적 오차, 예측 불가능

**2. Cron 절대 시간 (Broadcasting 동기화)**
```python
CronTrigger(second='7,17,27,37,47,57', timezone=KST)
```
- 장점: 정확한 시간, Broadcasting 기준 동기화

**3. 3-Tier 분류 (중요도 기반)**
- Tier A: 기준 환율 (investing) - 가장 중요
- Tier B: Request 기반 (kb, hana, woori, bs, citi)
- Tier C: Selenium 기반 (shinhan, ibk, nh, sc)

---

### 결정

**3-Tier Cron 절대 시간 동기화 채택**

**근거:**
1. **Broadcasting 동기화**: 크롤러가 Broadcasting X초 전에 실행 → 최신 데이터 반영
2. **예측 가능성**: Cron 절대 시간 → 매시간 같은 패턴
3. **리소스 분산**: 동시 실행 최소화 (엇갈림 배치)

### 구현 상세

**Tier A (investing): 기준 환율, 최우선**
- IN: 10초마다 (Broadcasting 3초 전)
  ```python
  CronTrigger(second='7,17,27,37,47,57', timezone=KST)
  ```
- OUT: 10분마다
  ```python
  CronTrigger(minute='7,17,27,37,47,57', second='0', timezone=KST)
  ```

**Tier B (kb, hana): 은행 환율, 중요**
- IN: 20초마다 (Broadcasting 5초 전, 서로 10초 엇갈림)
  ```python
  kb:   CronTrigger(second='15,35,55', timezone=KST)
  hana: CronTrigger(second='5,25,45', timezone=KST)
  ```
- OUT: 10분마다 (완전 분산)
  ```python
  kb:   CronTrigger(minute='5,15,25,35,45,55', second='0', timezone=KST)
  hana: CronTrigger(minute='0,10,20,30,40,50', second='0', timezone=KST)
  ```

**Tier B (woori, bs, citi): 일반 빈도**
- IN: 60초마다 (Broadcasting 7초 전, 20초씩 엇갈림)
  ```python
  woori: CronTrigger(minute='*', second='13', timezone=KST)
  bs:    CronTrigger(minute='*', second='33', timezone=KST)
  citi:  CronTrigger(minute='*', second='53', timezone=KST)
  ```
- OUT: 60분마다 (완전 분산)
  ```python
  woori: CronTrigger(minute='13', second='0', timezone=KST)
  bs:    CronTrigger(minute='33', second='0', timezone=KST)
  citi:  CronTrigger(minute='53', second='0', timezone=KST)
  ```

**Tier C (shinhan, ibk, nh, sc): Selenium, 순차 처리**
- IN: interval (Broadcasting 독립)
  ```python
  shinhan: IntervalTrigger(seconds=38.3, timezone=KST)
  ibk:     IntervalTrigger(seconds=55.5, timezone=KST)
  nh:      IntervalTrigger(seconds=90, timezone=KST)
  sc:      IntervalTrigger(seconds=150, timezone=KST)
  ```
- OUT: cron (60분마다, 완전 분산)
  ```python
  shinhan: CronTrigger(minute='23', second='0', timezone=KST)
  ibk:     CronTrigger(minute='43', second='0', timezone=KST)
  nh:      CronTrigger(minute='3', second='0', timezone=KST)
  sc:      CronTrigger(minute='36', second='0', timezone=KST)
  ```

### IN 모드 타임라인 (1분 기준)

```
00초: Broadcasting
05초: hana
07초: investing
10초: Broadcasting
13초: woori
15초: kb
17초: investing
20초: Broadcasting
25초: hana
27초: investing
30초: Broadcasting
33초: bs
35초: kb
37초: investing
40초: Broadcasting
45초: hana
47초: investing
50초: Broadcasting
53초: citi
55초: kb
57초: investing
```

### 설계 원칙

1. **Broadcasting 동기화**: 크롤러가 Broadcasting X초 전에 실행 → 최신 데이터 반영
2. **리소스 분산**: 동시 실행 최대 1~2개 (Request 기반 크롤러만)
3. **예측 가능성**: Cron 절대 시간 → 매시간 같은 패턴
4. **Tier별 차별화**: 중요도에 따라 주기 조정

### 트레이드오프

| 항목 | Interval 기반 | 3-Tier Cron |
|------|--------------|-------------|
| **예측성** | ❌ 누적 오차 | ✅ 정확한 시간 |
| **동기화** | ❌ 불가능 | ✅ Broadcasting 기준 |
| **복잡도** | 낮음 | 중간 |
| **리소스** | ⚠️ 스파이크 | ✅ 분산 |

### 성능 영향

**Before (Interval):**
- 동시 실행: 3~5개 (랜덤)
- CPU 스파이크: 20~30%

**After (3-Tier Cron):**
- 동시 실행: 1~2개 (예측 가능)
- CPU 안정: 5~10%

### 향후 재검토 시점

- 사용자 1000명 이상 (더 세밀한 분산 필요)
- Broadcasting 주기 변경 시 (10초 → 5초)

### 관련 결정

- [ADR-008](#adr-008-queue-압력-완화-전략-실시간성-vs-완전성): Queue 압력 완화 전략
- [ADR-010](#adr-010-websocket-broadcasting-스케줄링-방식): Broadcasting 스케줄링 방식

---

## ADR-008: Queue 압력 완화 전략 (실시간성 vs 완전성)

**날짜:** 2025-11-10
**상태:** 수락됨

### 상황

[ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout)에서 PriorityQueue + Timeout 도입 후 **Queue 압력 문제** 발생:
- IN 모드에서 크롤러 간격 짧아짐 (38초, 55초, 90초, 150초)
- Queue가 빠르게 차면 새 작업 추가 불가
- Worker가 처리 속도보다 추가 속도가 빠름

### 고려 사항

**1. Queue 크기 무제한**
- 장점: 모든 작업 수용
- 단점: 메모리 폭증, OOM 위험

**2. Queue 포화 시 작업 거부**
- 장점: 메모리 안정
- 단점: 완전성 저하 (일부 크롤링 누락)

**3. Worker 2~3개 병렬 실행**
- 장점: 처리 속도 증가
- 단점: 메모리 증가 (Chrome 2~3개 동시)

---

### 결정

**Queue 압력 완화 전략 채택**

**구성:**
1. Queue 크기 제한: 25 (메모리 최적화)
2. 압력 완화: 80% (20/25) 초과 시 새 작업 거부
3. Non-blocking put: put_nowait()로 APScheduler 멈춤 방지
4. 타임아웃 엄격화: 45초 통일 (빠른 실패 → Queue 정체 방지)

**근거:**
1. **실시간성 우선**: 오래된 데이터보다 최신 데이터가 중요
2. **메모리 안정**: Queue 크기 제한으로 메모리 예측 가능
3. **자동 복구**: 다음 스케줄에서 자동으로 재시도

### 구현 상세

**1. Queue 크기 제한 (25)**
```python
selenium_queue = asyncio.PriorityQueue(maxsize=25)
```

**2. 압력 완화 (80% 임계값)**
```python
def enqueue_selenium_job(bank_name: str, job_func):
    current_size = selenium_queue.qsize()
    max_size = 25

    if current_size >= 20:  # 80% 초과 (20/25)
        logger.warning(f"{bank_name} Queue 압력 초과로 skip ({current_size}/{max_size})")
        return

    try:
        selenium_queue.put_nowait((priority, time.time(), bank_name, job_func))
    except asyncio.QueueFull:
        logger.warning(f"{bank_name} Queue 포화로 skip")
```

**3. Non-blocking put**
- `put_nowait()` 사용 → APScheduler event loop blocking 방지
- Queue 가득 차면 즉시 예외 발생 → 조용히 skip

**4. 타임아웃 통일 (45초)**
```python
SELENIUM_TIMEOUT_MAP = {
    "hana": 45,
    "ibk": 45,
    "nh": 45,
    "sc": 45,
    "shinhan": 45
}
```
- 기존: 60~120초 (느린 크롤러 대기)
- 변경: 45초 통일 (빠른 실패 → Queue 정체 방지)

### 실시간성 vs 완전성 트레이드오프

**실시간성 우선 (채택):**
- ✅ 최신 데이터 우선
- ✅ Queue 포화 방지
- ✅ 메모리 안정
- ❌ 일부 크롤링 누락 가능 (다음 스케줄에서 재시도)

**완전성 우선 (기각):**
- ✅ 모든 작업 완료
- ❌ Queue 포화 → 메모리 폭증
- ❌ 오래된 데이터 전송

### 실행 예시

**압력 완화 동작:**
```
Queue: [19/25] (76%)
→ 새 작업 추가 성공

Queue: [20/25] (80%)
→ 새 작업 거부 (압력 완화)
→ 다음 스케줄에서 재시도

Queue: [15/25] (60%)
→ 압력 해소, 정상 추가 재개
```

### 모니터링

**Queue 상태 모니터링 (10초마다):**
```python
def report_queue_status():
    size = selenium_queue.qsize()
    usage_percent = (size / 25) * 100

    if size < 20:  # 80% 미만
        logger.debug(f"[Queue] {size}/25 ({usage_percent:.1f}%)")
    else:  # 경고 상태
        logger.warning(f"⚠️ [Queue] 포화 임박: {size}/25 ({usage_percent:.1f}%)")
```

### 트레이드오프

| 항목 | Queue 무제한 | Queue 제한 + 압력 완화 |
|------|------------|---------------------|
| **메모리** | ❌ 폭증 위험 | ✅ 안정 (예측 가능) |
| **완전성** | ✅ 모든 작업 | ⚠️ 일부 skip |
| **실시간성** | ❌ 오래된 데이터 | ✅ 최신 데이터 |
| **복잡도** | 낮음 | 중간 |

### 성능 영향

**Before (Queue 무제한):**
- Queue 크기: 0~50 (불안정)
- 메모리: 최대 1.5GB
- 최신 데이터 지연: 3~5분

**After (Queue 제한 + 압력 완화):**
- Queue 크기: 0~20 (안정)
- 메모리: 최대 800MB
- 최신 데이터 지연: 30초~1분

### 향후 재검토 시점

- Queue skip 빈도 > 20% (너무 많은 누락)
- Worker 2~3개 병렬 실행 고려 시

### 관련 결정

- [ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout): PriorityQueue + Timeout 전략
- [ADR-009](#adr-009-3-tier-스케줄링-아키텍처-t3smallmedium-최적화): 3-Tier 스케줄링 아키텍처

---

## ADR-010: WebSocket Broadcasting 스케줄링 방식

**날짜:** 2025-11-12
**상태:** 수락됨

### 상황

WebSocket Broadcasting 실행 방식:
1. **asyncio.sleep() 기반 루프**
2. **APScheduler cron job**

### 고려 사항

**1. asyncio.sleep() 기반 (기존)**
```python
async def broadcast_loop():
    while True:
        now = datetime.now(KST)
        next_broadcast = (now.second // 10 + 1) * 10
        delay = next_broadcast - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(delay)
        await broadcast_rates_once()
```

**문제점:**
- **누적 오차**: asyncio.sleep()는 정확하지 않음
- **복잡한 로직**: 다음 실행 시간 계산 필요
- **크롤러 동기화 어려움**: Broadcasting 시간이 예측 불가능

---

**2. APScheduler cron job (새로운 방식)**
```python
scheduler.add_job(
    broadcast_rates_once,
    CronTrigger(second='0,10,20,30,40,50', timezone=KST),
    id="websocket_broadcast"
)
```

**장점:**
- **정확한 시간**: Cron 표현식으로 정확한 시간 보장
- **크롤러 동기화**: Broadcasting 기준으로 크롤러 스케줄링 가능
- **단순성**: 다음 실행 시간 계산 불필요

---

### 결정

**APScheduler cron job 채택**

**근거:**
1. **정확한 시간 보장**: Cron 표현식으로 매분 00, 10, 20, 30, 40, 50초 정확히 실행
2. **크롤러 동기화 전제조건**: Broadcasting을 기준으로 크롤러들이 X초 전에 실행되도록 스케줄링
3. **코드 단순화**: asyncio.sleep() 계산 로직 제거

### 구현 상세

**AsyncIO sleep 제거:**
```python
# 제거됨
async def broadcast_loop():
    while True:
        await asyncio.sleep(delay)
        await broadcast_rates_once()
```

**APScheduler cron 추가:**
```python
scheduler.add_job(
    broadcast_rates_once,  # async 함수 직접 등록
    CronTrigger(second='0,10,20,30,40,50', timezone=KST),
    id="websocket_broadcast",
    max_instances=1,
    misfire_grace_time=5
)
```

**크롤러 동기화:**
```python
# Broadcasting 3초 전 (07,17,27,37,47,57초)
scheduler.add_job(
    make_request_crawler_wrapper('investing', investing.crawl_and_save_investing_exchange_rates),
    CronTrigger(second='7,17,27,37,47,57', timezone=KST),
    id='task_investing'
)

# Broadcasting 5초 전 (05,15,25,35,45,55초)
scheduler.add_job(
    make_request_crawler_wrapper('kb', kb.crawl_and_save_kb_bank_exchange_rates),
    CronTrigger(second='15,35,55', timezone=KST),
    id='task_kb'
)
```

### 트레이드오프

| 항목 | asyncio.sleep | APScheduler cron |
|------|--------------|-----------------|
| **정확성** | ❌ 누적 오차 | ✅ 정확한 시간 |
| **동기화** | ❌ 불가능 | ✅ 크롤러 기준점 |
| **복잡도** | 중간 | 낮음 |
| **의존성** | 없음 | APScheduler |

### 성능 영향

**Before (asyncio.sleep):**
- 실행 시간: 00.1초, 10.3초, 20.5초 (누적 오차)
- 크롤러 동기화: 불가능

**After (APScheduler cron):**
- 실행 시간: 정확히 00초, 10초, 20초, ...
- 크롤러 동기화: Broadcasting X초 전에 실행 → 최신 데이터 반영

### 향후 재검토 시점

- Broadcasting 주기 변경 시 (10초 → 5초)
- APScheduler 성능 이슈 발생 시

### 관련 결정

- [ADR-009](#adr-009-3-tier-스케줄링-아키텍처-t3smallmedium-최적화): 3-Tier 스케줄링 아키텍처 (Broadcasting 기준)

---

## ADR-011: Selenium Timeout 최적화 - Race Condition 제거

**날짜:** 2025-11-13
**상태:** 수락됨

### 상황

[ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout)에서 도입한 `asyncio.wait_for()` 타임아웃에서 **Race Condition** 발생:

**문제 시나리오:**
```python
async def execute_with_timeout(bank_name: str):
    timeout = SELENIUM_TIMEOUT_MAP.get(bank_name, 60)
    driver = None

    try:
        driver = create_selenium_driver()
        await asyncio.wait_for(crawl_logic(driver), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"{bank_name} 타임아웃")
    finally:
        if driver:
            driver.quit()
```

**Race Condition:**
1. `await asyncio.wait_for()` 타임아웃 발생
2. `asyncio.TimeoutError` 예외 발생 → `except` 블록 진입
3. **동시에** `crawl_logic(driver)` 내부에서 `driver.quit()` 실행 가능
4. `finally` 블록에서 다시 `driver.quit()` 호출
5. → `InvalidSessionIdException` 발생 또는 Chrome 프로세스 누수

### 고려 사항

**1. Lock 기반 제어**
```python
driver_lock = asyncio.Lock()

async with driver_lock:
    driver.quit()
```
- 문제: 복잡도 증가, 데드락 위험

**2. subprocess 격리**
```python
proc = await asyncio.create_subprocess_exec(
    sys.executable, "-m", "app.crawlers.runner", bank_name
)
await asyncio.wait_for(proc.wait(), timeout=timeout)
proc.kill()  # 타임아웃 시 프로세스 강제 종료
```
- 장점: Race Condition 원천 차단
- Chrome 포함 전체 프로세스 강제 종료

---

### 결정

**subprocess 기반 타임아웃 제어 채택**

**근거:**
1. **Race Condition 제거**: 프로세스 단위 제어로 driver.quit() 이중 호출 불가능
2. **Chrome 정리 보장**: proc.kill()로 Chrome 포함 전체 프로세스 강제 종료
3. **Event Loop Blocking 제거**: subprocess는 별도 프로세스에서 실행

### 구현 상세

**1. runner.py 엔트리포인트 (새로 추가)**
```python
# app/crawlers/runner.py
import sys
from app.crawlers import shinhan, ibk, nh, sc

if __name__ == "__main__":
    bank_name = sys.argv[1]

    if bank_name == "shinhan":
        shinhan.crawl_and_save_shinhan_bank_exchange_rates()
    elif bank_name == "ibk":
        ibk.crawl_and_save_ibk_bank_exchange_rates()
    # ...
```

**2. subprocess 기반 실행**
```python
async def execute_with_timeout(bank_name: str) -> bool:
    timeout = SELENIUM_TIMEOUT_MAP.get(bank_name, 45)

    try:
        # subprocess 생성
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.crawlers.runner", bank_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # 타임아웃 제어
        await asyncio.wait_for(proc.wait(), timeout=timeout)

        # exit code 확인
        if proc.returncode == 0:
            return True
        else:
            logger.error(f"{bank_name} 실패 (exit code: {proc.returncode})")
            return False

    except asyncio.TimeoutError:
        # 프로세스 강제 종료 (Chrome 포함)
        logger.warning(f"{bank_name} 타임아웃 ({timeout}초) - 프로세스 강제 종료")
        proc.kill()
        await proc.wait()  # 종료 대기
        return False
```

### 트레이드오프

| 항목 | asyncio.wait_for | subprocess |
|------|-----------------|-----------|
| **Race Condition** | ❌ 발생 가능 | ✅ 없음 |
| **Chrome 정리** | ⚠️ 불확실 | ✅ 보장 (proc.kill) |
| **복잡도** | 낮음 | 중간 |
| **오버헤드** | 없음 | 프로세스 생성 (~50ms) |

### 성능 영향

**Before (asyncio.wait_for):**
- Chrome 정리: 불확실 (driver.quit() 이중 호출)
- Race Condition: 가끔 발생

**After (subprocess):**
- Chrome 정리: 100% 보장 (proc.kill())
- Race Condition: 완전 제거
- 오버헤드: 프로세스 생성 50ms (무시 가능)

### 향후 재검토 시점

- subprocess 오버헤드가 병목이 되는 경우 (현재는 무시 가능)
- 크롤러 실행 빈도가 초 단위 이하로 줄어드는 경우

### 관련 결정

- [ADR-007](#adr-007-selenium-크롤러-우선순위-기반-실행-priority-queue--timeout): PriorityQueue + Timeout 전략
- [ADR-012](#adr-012-selenium-폴백-subprocess-격리-chrome-프로세스-좀비화-방지): Selenium 폴백 subprocess 격리

---

## ADR-012: Selenium 폴백 subprocess 격리 (Chrome 프로세스 좀비화 방지)

**날짜:** 2025-11-14
**상태:** 수락됨

### 상황

[ADR-011](#adr-011-selenium-timeout-최적화---race-condition-제거)에서 Queue 기반 Selenium 크롤러 (shinhan, ibk, nh, sc)를 subprocess로 격리 완료.

**새로운 문제**: 하이브리드 크롤러 (hana, woori)의 Selenium 폴백도 **Chrome 좀비 프로세스** 발생:
- Request 실패 시 Selenium 폴백 실행
- 하지만 폴백 Selenium 함수(`hana_selenium`, `woori_selenium`)는 **동기 함수** (subprocess 격리 없음)
- driver.quit() 실패 시 Chrome 프로세스 누적

### 고려 사항

**1. 기존 방식 (동기 함수)**
```python
def crawl_hana_bank():
    try:
        # Request 시도
        response = requests.get(...)
        # ...
    except:
        # Selenium 폴백 (동기 함수)
        hana_selenium()  # driver.quit() 실패 가능
```

**문제점:**
- driver.quit() 실패 시 Chrome 프로세스 누적
- asyncio.wait_for() 타임아웃 시 Race Condition

---

**2. subprocess 격리 (새로운 방식)**
```python
def crawl_hana_bank():
    try:
        # Request 시도
        response = requests.get(...)
        # ...
    except:
        # Selenium 폴백 (subprocess 격리)
        result = subprocess.run(
            [sys.executable, "-m", "app.crawlers.runner", "hana_selenium"],
            timeout=45
        )
```

**장점:**
- Chrome 프로세스 강제 종료 보장
- Race Condition 완전 제거

---

### 결정

**모든 Selenium 실행을 subprocess로 격리**

**근거:**
1. **Chrome 좀비 프로세스 완전 제거**: proc.kill()로 강제 종료 보장
2. **일관성**: Queue 기반 + 폴백 기반 모두 subprocess 사용
3. **안정성 우선**: 오버헤드(50ms)보다 안정성이 중요

### 구현 상세

**Phase 1 (이전 완료): Queue 기반 Selenium**
- shinhan, ibk, nh, sc → AsyncIO subprocess (ADR-011)

**Phase 2 (현재): 폴백 기반 Selenium**
- hana, woori → 동기 subprocess fallback

**1. runner.py에 폴백 엔트리포인트 추가**
```python
# app/crawlers/runner.py
if __name__ == "__main__":
    bank_name = sys.argv[1]

    if bank_name == "hana_selenium":
        hana.hana_selenium()
    elif bank_name == "woori_selenium":
        woori.woori_selenium()
    # ...
```

**2. 하이브리드 크롤러에서 subprocess 폴백 호출**
```python
def crawl_hana_bank():
    try:
        # Request 시도
        response = requests.get(...)
        # ...
    except:
        logger.info("Request 실패 → Selenium 폴백 (subprocess)")

        try:
            result = subprocess.run(
                [sys.executable, "-m", "app.crawlers.runner", "hana_selenium"],
                timeout=45,
                capture_output=True
            )

            if result.returncode == 0:
                logger.info("Selenium 폴백 성공")
            else:
                logger.error(f"Selenium 폴백 실패 (exit code: {result.returncode})")

        except subprocess.TimeoutExpired:
            logger.warning("Selenium 폴백 타임아웃 (45초)")
```

### 트레이드오프

| 항목 | 동기 함수 | subprocess 격리 |
|------|----------|----------------|
| **Chrome 정리** | ⚠️ 불확실 | ✅ 보장 |
| **Race Condition** | ❌ 가능 | ✅ 없음 |
| **복잡도** | 낮음 | 중간 |
| **오버헤드** | 없음 | 50ms (무시 가능) |

### 모든 Selenium 크롤러 subprocess 격리 완료

**Phase 1 (이전)**: shinhan, ibk, nh, sc → AsyncIO Queue + subprocess
**Phase 2 (현재)**: hana_selenium, woori_selenium → 동기 subprocess fallback

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

## ADR-013: 4단계 모드 - 환율 고시 스케줄 기반 최적화

**날짜:** 2025-11-16
**상태:** 수락됨

### 상황

기존 2단계 모드 (IN/OUT)의 한계:
- **IN 모드**: 월 04:00 ~ 토 07:59 (영업시간 전체를 하나로)
- **OUT 모드**: 그 외 시간 (주말)
- **문제점**: 자정~03시 심야 시간대와 03~08시 개장 준비 시간대를 영업시간과 동일하게 처리 → 리소스 낭비

**실제 환율 고시 스케줄 추적 결과:**
- 일부 은행은 자정에 환율 고시 종료 (shinhan, sc)
- 일부 은행은 02:30에 종료 (woori, ibk)
- 일부 은행은 주말에도 가끔 변동 (hana, bs, nh)

### 고려 사항

**1. 기존 2단계 유지**
- 장점: 단순함
- 단점: 리소스 낭비 (환율 고시 없는 시간에도 크롤링)

**2. 5단계 세분화 (NIGHT, EARLY, ACTIVE, EVENING, OUT)**
- 장점: 가장 정밀한 최적화
- 단점: 복잡도 증가, Broadcasting 주기도 변경 필요

**3. 4단계 모드 (IN, BREAK1, BREAK2, OUT)**
- 장점: 적절한 세분화 + 복잡도 관리
- 단점: Broadcasting 주기는 그대로 유지

---

### 결정

**4단계 모드 채택 (IN, BREAK1, BREAK2, OUT)**

**근거:**
1. **환율 고시 스케줄 기반**: 실제 운영 데이터 추적으로 은행별 고시 시간 확인
2. **리소스 최적화**: 환율 고시 없는 시간에는 크롤러 비활성화
3. **복잡도 관리**: Broadcasting은 그대로 유지 (10초 주기)

### 4단계 모드 정의

**IN 모드: 월~금 08:00~24:00 (영업시간)**
- 모든 크롤러 활성 (10개 은행)
- 가장 빈번한 크롤링
- Broadcasting: 매분 00, 10, 20, 30, 40, 50초

**BREAK1 모드: 화~토 00:00~03:00 (심야)**
- 제외: shinhan, sc (자정에 환율 고시 종료)
- 유지: investing, kb, hana, woori, bs, citi, ibk, nh (8개)
- Broadcasting: 동일 (매분 00, 10, 20, 30, 40, 50초)

**BREAK2 모드: 월 04:00~08:00, 화~금 03:00~08:00, 토 03:00~07:59 (개장 준비)**
- 제외: woori, ibk (02:30 고시 종료), shinhan, sc (자정 종료)
- 유지: investing, kb, hana, bs, citi, nh (6개)
- Broadcasting: 동일

**OUT 모드: 토 08:00 ~ 월 04:00 (주말)**
- 제외: woori, ibk, shinhan, sc, citi (주말 고시 없음)
- 유지: investing, kb, hana, bs, nh (5개)
- Broadcasting: 동일
- 크롤러: 완전 분산 (동시 실행 0개)

### 은행별 환율 고시 스케줄 (실제 데이터)

| 은행 | 고시 시작 | 고시 종료 | BREAK1 | BREAK2 | OUT |
|------|----------|----------|--------|--------|-----|
| **investing** | 월 06:00 | 토 06:00 | ✅ | ✅ | ✅ |
| **kb** | 평일 08:30 | 익일(토 포함) 05:00 | ✅ | ✅ | ✅ |
| **hana** | 평일 08:30 | 익일(토 포함) 06:00 | ✅ | ✅ | ✅ |
| **shinhan** | 평일 08:00 | 당일 24:00 | ❌ | ❌ | ❌ |
| **woori** | 평일 08:30 | 익일 02:30 | ✅ | ❌ | ❌ |
| **ibk** | 평일 08:30 | 익일 02:30 | ✅ | ❌ | ❌ |
| **nh** | 평일 08:40 | 당일 24:00 | ✅ | ✅ | ✅ |
| **sc** | 평일 09:00 | 당일 24:00 | ❌ | ❌ | ❌ |
| **bs** | 평일 08:10 | 당일 24:00 | ✅ | ✅ | ✅ |
| **citi** | 평일 09:00 | 익일(토 포함) 06:00 | ✅ | ✅ | ❌ |

### 크롤러 우선순위 조정

**woori/citi 순서 변경:**
- 기존: `woori='13초'`, `citi='53초'`
- 변경: `woori='53초'`, `citi='13초'`
- 이유: woori가 우선순위 높음 → 같은 1분 내 늦게 크롤링 → 사용자 표시 시간이 실제 변경 시간과 유사

**Selenium 크롤러 Request 우선 전략 (2025-11-16):**
- shinhan, nh, sc: Request(mibank) → Selenium 폴백
- ibk: IN 모드(08:30~) Request 우선, BREAK1(00:00~03:00) Selenium만
- 목적: Queue 압력 대폭 감소 (대부분 Request 성공)

### 구현 상세

**1. 모드 판별 함수**
```python
def get_market_mode(now: datetime) -> str:
    weekday = now.weekday()  # 월=0, 화=1 ... 일=6
    hour = now.hour

    # OUT: 토 08:00 ~ 월 04:00
    if (weekday == 5 and hour >= 8) or (weekday == 6) or (weekday == 0 and hour < 4):
        return "OUT"

    # BREAK1: 00:00~03:00 (화~토)
    if hour < 3:
        return "BREAK1"

    # BREAK2: 03:00~08:00
    if hour < 8:
        return "BREAK2"

    # IN: 08:00~24:00
    return "IN"
```

**2. 모드별 크롤러 등록**
```python
def switch_jobs(mode: str):
    # 기존 작업 제거
    for job in scheduler.get_jobs():
        if job.id.startswith("task_"):
            scheduler.remove_job(job.id)

    if mode == "IN":
        # 모든 크롤러 등록 (10개)
        pass
    elif mode == "BREAK1":
        # shinhan, sc 제외 (8개)
        pass
    elif mode == "BREAK2":
        # woori, ibk, shinhan, sc 제외 (6개)
        pass
    elif mode == "OUT":
        # woori, ibk, shinhan, sc, citi 제외 (5개)
        pass
```

### Selenium 크롤러 Interval → Cron 전환

**기존 (Interval):**
```python
IntervalTrigger(seconds=38.3, timezone=KST)  # 누적 오차
```

**변경 (Cron 절대 시간):**
```python
CronTrigger(minute='*', second='18', timezone=KST)  # 매분 18초 정확히
```

**장점:**
- 예측 가능한 스케줄 (매시간 같은 패턴)
- Broadcasting 동기화 (필요 시)
- 로그 분석 용이

### 트레이드오프

| 항목 | 2단계 (IN/OUT) | 4단계 (IN/BREAK1/BREAK2/OUT) |
|------|---------------|------------------------------|
| **리소스** | ❌ 낭비 | ✅ 최적화 (30~40% 감소) |
| **복잡도** | 낮음 | 중간 |
| **정확성** | ⚠️ 불필요 크롤링 | ✅ 고시 시간 기반 |
| **유지보수** | 쉬움 | 중간 |

### 성능 영향

**Before (2단계):**
- 일일 크롤링: ~8,600회
- 심야 시간대 (00:00~03:00): IN과 동일 (10개 은행)
- 리소스 낭비: 고시 없는 은행도 크롤링

**After (4단계):**
- 일일 크롤링: ~5,200회 (40% 감소)
- BREAK1 (00:00~03:00): 8개 은행
- BREAK2 (03:00~08:00): 6개 은행
- OUT (주말): 5개 은행

### 향후 재검토 시점

- 은행별 환율 고시 시간 변경 시
- 추가 세분화 필요 시 (예: EVENING 모드 추가)

### 관련 결정

- [ADR-009](#adr-009-3-tier-스케줄링-아키텍처-t3smallmedium-최적화): 3-Tier 스케줄링 아키텍처
- [ADR-010](#adr-010-websocket-broadcasting-스케줄링-방식): Broadcasting 스케줄링 방식

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
- 2025-11-16: ADR-013 작성 (4단계 모드 - 환율 고시 스케줄 기반 최적화)
