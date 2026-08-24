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
1. **실시간성 우선**: 빠른 크롤러(hana, shinhan)가 먼저 실행
2. **메모리 유지**: Worker 1개 유지 (동시 Chrome 1개)
3. **느린 크롤러 격리**: ibk, sc는 타임아웃으로 빠르게 실패

### 구현 상세

**1. PriorityQueue 도입**
```python
selenium_queue = asyncio.PriorityQueue()

# 우선순위 설정 (낮을수록 높은 우선순위)
SELENIUM_PRIORITY_MAP = {
    "hana": 0,     # 가장 빠름 (Request 폴백 있음)
    "shinhan": 1,  # 빠름
    "nh": 2,       # 중간
    "ibk": 3,      # 느림
    "sc": 4        # 가장 느림
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
Queue: [sc(우선순위 4), shinhan(1), ibk(3)]
00:00 - shinhan 시작 (우선순위 1, 가장 높음)
00:45 - shinhan 완료
00:45 - ibk 시작 (우선순위 3)
01:30 - ibk 타임아웃 (45초) → 실패 처리 → 재시도 Queue 추가
01:30 - sc 시작
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
- 일부 은행은 20:30±에 환율 고시 종료 (sc)
- 일부 은행은 02:30±에 종료 (ibk 02:05, shinhan 02:30, woori 02:45)
- 일부 은행은 주말에도 가끔 변동 (hana, bs, nh, shinhan)

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

**IN 모드: 월~금 08:00~20:59 (영업시간)**
- 모든 크롤러 활성 (10개 은행)
- 가장 빈번한 크롤링
- Broadcasting: 매분 00, 10, 20, 30, 40, 50초

**BREAK1 모드: 월~금 21:00~23:59, 화~토 00:00~02:59 (심야)**
- 제외: sc (20:30 종료)
- 유지: investing, kb, hana, woori, bs, citi, ibk, nh, shinhan (9개)
- Broadcasting: 동일 (매분 00, 10, 20, 30, 40, 50초)

**BREAK2 모드: 월 06:00~07:59, 화~금 03:00~07:59, 토 03:00~06:59 (개장 준비)**
- 제외: woori (02:45 종료), ibk (02:05 종료), shinhan (02:30 종료), sc (20:30 종료)
- 유지: investing, kb, hana, bs, citi, nh (6개)
- Broadcasting: 동일

**OUT 모드: 토 07:00 ~ 월 05:59 (주말)**
- 제외: woori, ibk, sc, citi (주말 고시 없음)
- 유지: investing, kb, hana, bs, shinhan, nh (6개)
- Broadcasting: 동일
- 크롤러: 완전 분산 (동시 실행 0개)

### 은행별 환율 고시 스케줄 (실제 데이터)

| 은행 | 고시 시작 | 고시 종료 | BREAK1 | BREAK2 | OUT |
|------|----------|----------|--------|--------|-----|
| **investing** | 월 06:00 | 토 06:00 | ✅ | ✅ | ✅ |
| **kb** | 평일 08:30 | 익일(토 포함) 05:00 | ✅ | ✅ | ✅ |
| **hana** | 평일 08:30 | 익일(토 포함) 06:00 | ✅ | ✅ | ✅ |
| **shinhan** | 평일 08:19 | 익일 02:30 | ✅ | ❌ | ✅ |
| **woori** | 평일 08:30 | 익일 02:45 | ✅ | ❌ | ❌ |
| **ibk** | 평일 08:30 | 익일 02:05 | ✅ | ❌ | ❌ |
| **nh** | 평일 08:40 | 당일 24:00 | ✅ | ✅ | ✅ |
| **sc** | 평일 09:00 | 당일 20:30 | ❌ | ❌ | ❌ |
| **bs** | 평일 08:10 | 당일 24:00 | ✅ | ✅ | ✅ |
| **citi** | 평일 09:00 | 익일(토 포함) 06:00 | ✅ | ✅ | ❌ |

### 크롤러 우선순위 조정

**woori/citi 순서 변경:**
- 기존: `woori='13초'`, `citi='53초'`
- 변경: `woori='53초'`, `citi='13초'`
- 이유: woori가 우선순위 높음 → 같은 1분 내 늦게 크롤링 → 사용자 표시 시간이 실제 변경 시간과 유사

**Selenium 크롤러 Request 우선 전략 (2025-11-16):**
- shinhan, nh, sc: Request(mibank) → Selenium 폴백
- ibk: IN 모드(08:30~) Request 우선, BREAK1 구간 중 00:00~02:59은 Selenium만 사용 (날짜 변경 필요)
- 목적: Queue 압력 대폭 감소 (대부분 Request 성공)

### 구현 상세

**1. 모드 판별 함수**
```python
def get_market_mode(now: datetime) -> str:
    weekday = now.weekday()  # 월=0, 화=1 ... 일=6
    hour = now.hour

    # OUT: 토 07:00 ~ 월 05:59
    if (weekday == 5 and hour >= 7) or (weekday == 6) or (weekday == 0 and hour < 6):
        return "OUT"

    # BREAK1: 월~금 21:00~23:59, 화~토 00:00~02:59
    if 0 <= weekday <= 4 and 21 <= hour:
        return "BREAK1"
    if 1 <= weekday <= 5 and hour < 3:
        return "BREAK1"

    # BREAK2: 03:00~07:59
    if 3 <= hour < 8:
        return "BREAK2"

    # IN: 08:00~20:59
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
- 심야 시간대 (21:00~02:59): IN과 동일 (10개 은행)
- 리소스 낭비: 고시 없는 은행도 크롤링

**After (4단계):**
- 일일 크롤링: ~5,200회 (40% 감소)
- BREAK1 (21:00~02:59): 9개 은행 (sc 제외)
- BREAK2 (03:00~07:59): 6개 은행
- OUT (토 07:00~월 05:59): 5개 은행

### 향후 재검토 시점

- 은행별 환율 고시 시간 변경 시
- 추가 세분화 필요 시 (예: EVENING 모드 추가)

### 관련 결정

- [ADR-009](#adr-009-3-tier-스케줄링-아키텍처-t3smallmedium-최적화): 3-Tier 스케줄링 아키텍처
- [ADR-010](#adr-010-websocket-broadcasting-스케줄링-방식): Broadcasting 스케줄링 방식

---

## ADR-014: HTTPS/SSL 도입 (Let's Encrypt)

**날짜:** 2025-11-17
**상태:** 수락됨

### 상황

iOS ATS(App Transport Security)로 인해 HTTP WebSocket(`ws://`)이 차단되어 네이티브 앱에서 실시간 통신이 불가능한 상황. 또한 HTTP 프로토콜 사용 시 다음과 같은 보안 문제 발생:
- 관리자 페이지 Basic Auth 비밀번호 평문 노출
- 환율 데이터 중간자 공격(Man-in-the-Middle) 취약성
- 브라우저 보안 경고 표시

### 고려 사항

**1. Let's Encrypt (무료 SSL)**
- ✅ 무료, 자동 갱신 (90일 주기)
- ✅ 모든 브라우저/OS 신뢰
- ✅ Certbot으로 자동화 가능
- ⚠️ Rate Limit (도메인당 주 50개)
- ⚠️ 90일마다 갱신 필요 (자동화 필수)

**2. Cloudflare Tunnel**
- ✅ 무료, 자동 HTTPS
- ✅ DDoS 보호, CDN 포함
- ✅ 도메인 자동 제공
- ❌ Cloudflare 플랫폼 종속성
- ❌ WebSocket 지연 증가 가능성
- ❌ Origin 서버 제어권 제한

**3. 자체 서명 인증서 (Self-Signed)**
- ✅ 즉시 사용 가능
- ✅ 비용 없음
- ❌ 브라우저 경고 표시 ("연결이 안전하지 않음")
- ❌ iOS ATS 차단 (앱에서 사용 불가)
- ❌ 사용자 신뢰도 하락

**4. 유료 SSL 인증서**
- ✅ 1~3년 유효기간 (갱신 주기 길음)
- ✅ Extended Validation 옵션
- ❌ 연간 비용 ($50~300)
- ❌ 수동 갱신 필요

---

### 결정

**Let's Encrypt + Certbot 자동 갱신 채택**

**근거:**
1. **iOS/Android 필수 요구사항**: ATS 정책으로 HTTPS 필수
2. **무료 + 신뢰성**: 업계 표준, 전 세계 수백만 사이트 사용
3. **자동 갱신**: Systemd Timer로 완전 자동화 (유지보수 부담 없음)
4. **독립성**: 외부 플랫폼 종속성 없음 (Cloudflare vs Let's Encrypt)

### 구현 세부사항

**도메인:**
- `fxi.n-e.kr` (기존 `fxi.kro.kr`은 Rate Limit 초과)

**인증서:**
- Let's Encrypt (90일 유효)
- `/etc/letsencrypt/live/fxi.n-e.kr/fullchain.pem`
- `/etc/letsencrypt/live/fxi.n-e.kr/privkey.pem`

**자동 갱신:**
- Systemd Timer: 매일 KST 04:30, 05:30 검사
- 만료 30일 전부터 갱신 시작
- Deploy Hook: 갱신 성공 시 Nginx 자동 재시작

**Nginx 설정:**
- HTTP (포트 80): HTTPS로 301 리다이렉트
- HTTPS (포트 443): TLS 1.2 & 1.3
- ACME Challenge: `/.well-known/acme-challenge/` 경로 (갱신용)
- HSTS 헤더: 1년, includeSubDomains

**Docker 배포:**
- 포트: 80, 443 노출
- 볼륨 마운트: `/etc/letsencrypt` (호스트 → 컨테이너)
- WebSocket: `wss://` 지원 (X-Forwarded-Proto: https)

### 트레이드오프

| 항목 | HTTP (Before) | HTTPS (After) |
|------|---------------|---------------|
| **iOS 앱** | ❌ 작동 불가 (ATS 차단) | ✅ 정상 작동 |
| **보안** | ❌ 평문 전송 | ✅ 암호화 |
| **브라우저 경고** | ⚠️ "안전하지 않음" | ✅ 표시 없음 |
| **유지보수** | 간단 | 90일 자동 갱신 |
| **비용** | 무료 | 무료 |
| **복잡도** | 낮음 | 중간 (Certbot 설정) |

### 성능 영향

**Before (HTTP):**
- 암호화 오버헤드: 없음
- WebSocket: `ws://`
- 연결 속도: 빠름

**After (HTTPS):**
- TLS Handshake: +50~100ms (최초 연결)
- WebSocket: `wss://` (암호화)
- HTTP/2 지원: ✅ (다중 요청 최적화)
- 체감 성능 변화: 거의 없음 (현대 CPU의 AES-NI 가속)

### 모니터링 & 유지보수

**자동 갱신 검증:**
```bash
# 갱신 테스트
sudo certbot renew --dry-run

# 다음 실행 시간 확인
sudo systemctl list-timers certbot.timer

# 갱신 로그 확인
sudo tail -f /var/log/letsencrypt/letsencrypt.log
sudo tail -f /var/log/certbot-renewal.log
```

**알림:**
- Let's Encrypt: 만료 20일 전 이메일 알림
- 갱신 실패 시: Systemd journald 로그 기록

### 향후 재검토 시점

- 트래픽 급증 시 (CDN 필요성 검토 → Cloudflare)
- 다중 도메인 필요 시 (와일드카드 인증서)
- Extended Validation 필요 시 (기업 인증)

### 관련 결정

- [ADR-001](#adr-001-websocket-구현---python-fastapi-vs-nodejs): WebSocket 구현 (wss:// 지원)
- [ADR-005](#adr-005-배포-방식---docker-compose-vs-kubernetes): Docker 배포 (인증서 볼륨 마운트)

---

## ADR-015: WebSocket Graph Integration vs Incremental API

**날짜:** 2025-12-02
**상태:** 수락됨

### 상황

24시간 환율 그래프의 실시간 업데이트 방법 설계:
- iOS/Android 네이티브 앱: 100+ 동시 접속 사용자
- 그래프 데이터: 10분 버킷, 144개/24시간
- 업데이트 주기: 매분 (새 버킷 10분마다 생성)

### 고려 사항

**1. Periodic Reload (주기적 전체 재조회)**
```javascript
setInterval(() => {
    fetch('/api/graph/usd-krw').then(data => drawGraph(data));
}, 60000);  // 1분마다
```
- 장점: 구현 간단
- 단점: 3.6KB × 60회/시간 = 216KB/시간 (데이터 중복)

---

**2. WebSocket Integration (기존 연결 재사용)**
```javascript
// 기존 WebSocket에 graph_buckets 필드 추가
{
    "rates": [...],  // 기존 실시간 환율
    "graph_buckets": {  // 그래프 마지막 버킷 (3 currencies)
        "usd-krw": {
            "investing": {"bucket_ts": 1733140800, "max": 1340.50, ...},
            "kb": {...},
            "hana": {...}
        },
        "jpy-krw": {...},
        "eur-krw": {...}
    }
}
```
- 장점: 기존 연결 재사용 (모바일 배터리 무영향), 서버 부하 최소
- 단점: 메시지 크기 +600 bytes

---

**3. Incremental API (증분 업데이트)**
```javascript
// 마지막 타임스탬프 이후 데이터만 요청
fetch(`/api/graph/usd-krw/incremental?since=${lastTimestamp}`)
```
- 장점: 최소 데이터 전송
- 단점: 100명 × 60회/시간 = 6,000 HTTP req/시간 (서버 부하)

---

**4. Visibility-based Lazy Reload (가시성 기반)**
```javascript
document.addEventListener('visibilitychange', () => {
    if (!document.hidden && isDataStale()) {
        fetch('/api/graph/usd-krw');
    }
});
```
- 장점: 불필요한 요청 제거
- 단점: 백그라운드 복귀 시 지연, 실시간성 저하

---

**5. Client-side Aggregation (클라이언트 집계)**
```javascript
// WebSocket 실시간 환율로 클라이언트가 버킷 생성
websocket.on('message', (rates) => {
    aggregateIntoBucket(rates);
});
```
- 단점: carry-forward 로직 불가능 (서버 의존), 복잡도 증가, 변경 감지 불가

---

### 결정

**WebSocket Integration with Lazy Loading 채택**

**구성:**
1. WebSocket에 `graph_buckets` 필드 추가 (3 currencies × 3 sources × 1 bucket)
2. Frontend lazy loading: 초기 선택 통화만 로드 (3.6KB)
3. 통화 전환: 캐시 재사용 또는 on-demand 로드
4. 15분 갭 감지 → 전체 새로고침

**근거:**
1. **모바일 배터리 최적화**: 기존 WebSocket 연결 재사용 (새로운 라디오 활성화 0건)
2. **서버 효율성**: 1 broadcast/10초 vs 100+ HTTP req/분 (CPU 99% 감소)
3. **네트워크 효율**: +600 bytes vs 3.6KB × 100명/분 = 360KB/분 절감
4. **단순성**: Incremental API보다 구현 간소

### 구현 상세

**Backend (app/main.py):**
```python
async def build_graph_buckets() -> dict:
    """모든 통화의 마지막 그래프 버킷 반환 (WebSocket용)"""
    graph_buckets = {}
    for currency in ["usd-krw", "jpy-krw", "eur-krw"]:
        cache_key = f"graph:{currency}"
        cached = await redis_cache.get(cache_key)
        if cached:
            data = json.loads(cached)
            for source, series in data["data"].items():
                if series and len(series) > 0:
                    last_bucket = series[-1]  # [ts, max, min, close]
                    graph_buckets[currency][source] = {
                        "bucket_ts": last_bucket[0],
                        "max": last_bucket[1],
                        "min": last_bucket[2],
                        "close": last_bucket[3]
                    }
    return graph_buckets

async def broadcast_rates_once():
    # ... 기존 rates 로직 ...
    if new_json != cached_json:  # 변경 시에만
        graph_buckets = await build_graph_buckets()
        if graph_buckets:
            payload["graph_buckets"] = graph_buckets
        await manager.broadcast(payload)
```

**Frontend (templates/index.html):**
```javascript
// 1. 캐시 저장소
const graphData = {
    'usd-krw': null,
    'jpy-krw': null,
    'eur-krw': null
};

// 2. WebSocket 업데이트
function updateAllGraphBuckets(buckets) {
    for (const [currency, sources] of Object.entries(buckets)) {
        if (!graphData[currency]) continue;
        for (const [source, bucket] of Object.entries(sources)) {
            const series = graphData[currency][source];
            const lastBucket = series[series.length - 1];
            if (lastBucket[0] === bucket.bucket_ts) {
                // 현재 버킷 업데이트
                series[series.length - 1] = [bucket.bucket_ts, bucket.max, bucket.min, bucket.close];
            } else if (bucket.bucket_ts > lastBucket[0]) {
                // 새 버킷 추가
                series.push([bucket.bucket_ts, bucket.max, bucket.min, bucket.close]);
                // 24시간 윈도우 유지
                const cutoff = Math.floor(Date.now() / 1000) - 86400;
                while (series.length && series[0][0] < cutoff) {
                    series.shift();
                }
            }
        }
    }
    drawGraph({sources: graphData[currentGraphCurrency]});
}

// 3. Lazy loading with gap detection
async function loadGraph(currency) {
    const needsFullLoad = !graphData[currency] || detectDataGap(currency);
    if (needsFullLoad) {
        const res = await fetch(`/api/graph/${currency}`);
        const data = await res.json();
        graphData[currency] = data.sources;
    }
    currentGraphCurrency = currency;
    drawGraph({sources: graphData[currency]});
}

function detectDataGap(currency) {
    const cached = graphData[currency];
    if (!cached || !cached.investing) return true;
    const lastBucket = cached.investing[cached.investing.length - 1][0];
    return (Math.floor(Date.now() / 1000) - lastBucket) > 900;  // 15분
}
```

### 트레이드오프

| 항목 | Incremental API | WebSocket Integration |
|------|----------------|----------------------|
| **서버 부하** | ❌ 높음 (6,000 req/시간) | ✅ 낮음 (6 broadcast/분) |
| **모바일 배터리** | ❌ 영향 있음 (새 연결) | ✅ 영향 없음 (재사용) |
| **네트워크** | ⚠️ 360KB/분 | ✅ 3.6KB/분 (99% 감소) |
| **실시간성** | ✅ 즉시 | ✅ 10초 주기 |
| **복잡도** | 중간 | 낮음 |

### 성능 영향

**Incremental API (기각):**
- HTTP 요청: 100명 × 60회/시간 = 6,000 req
- 서버 CPU: 중간~높음 (DB 쿼리 6,000회)
- 모바일 배터리: 라디오 활성화 60회/시간 (영향 있음)

**WebSocket Integration (채택):**
- 추가 요청: 0 (기존 연결 재사용)
- 서버 CPU: 낮음 (broadcast 6회/분, 캐시 조회만)
- 모바일 배터리: 0 (연결 이미 열림)
- 메시지 크기: +600 bytes (5.6% 증가, 1.6KB → 2.2KB)

**데이터 중복:**
- `close` 값이 `rates[]`와 `graph_buckets`에 중복 (~90 bytes)
- Trade-off: 단순성 우선 (중복 제거 시 복잡도 증가)

### 향후 재검토 시점

- 메시지 크기 > 3KB (압축 고려)
- 사용자 1000명 이상 (Brotli, Redis Pub/Sub)
- 실시간성 요구 증가 (5초 주기)

### 관련 결정

- [ADR-001](#adr-001-websocket-구현---python-fastapi-vs-nodejs): WebSocket 구현
- [ADR-010](#adr-010-websocket-broadcasting-스케줄링-방식): Broadcasting 스케줄링

---

## ADR-016: 인증/알림 인프라 - AWS RDS + Firebase Auth + FCM

**날짜:** 2025-12-05
**상태:** 수락됨

### 상황

개인화 환율 알림 기능 구현을 위해 다음 인프라 결정이 필요:
1. **데이터베이스**: 사용자 정보, 알림 설정 저장
2. **인증 시스템**: 사용자 로그인, 크로스 플랫폼 동기화
3. **푸시 알림**: iOS/Android 앱으로 알림 전송

### 고려 사항

#### 1. DB 선택

| 옵션 | 비용 (초기) | 비용 (12개월 후) | 네트워크 지연 |
|------|-----------|-----------------|-------------|
| **AWS RDS PostgreSQL** | $0 (프리티어) | ~$15/월 | 1-3ms (같은 VPC) |
| Supabase PostgreSQL | $0 (500MB) | $25/월 (Pro) | 5-15ms (원격) |
| EC2 내 PostgreSQL Docker | $0 | $0 (EC2 포함) | <1ms (localhost) |

#### 2. Auth 선택

| 옵션 | 비용 | FCM 통합 | SDK 성숙도 |
|------|------|---------|----------|
| **Firebase Auth** | 무료 (50k MAU) | 완벽 (같은 Firebase) | 매우 성숙 |
| Supabase Auth | 무료 (50k MAU) | 별도 설정 필요 | 성장 중 |
| 자체 구현 | $0 | 별도 구현 | - |

#### 3. Push 알림 선택

| 옵션 | 비용 | iOS/Android |
|------|------|-------------|
| **Firebase FCM** | 무료 무제한 | 통합 지원 (APNs 래핑) |
| OneSignal | 무료 (10k MAU) | 통합 지원 |
| 직접 APNs/FCM | $0 | 별도 구현 |

### 결정

**AWS RDS PostgreSQL + Firebase Auth + FCM 채택**

```text
┌─────────────────────────────────────────────────────┐
│ AWS Cloud (서울 리전)                                │
│  ┌──────────────────────────────────────────────┐  │
│  │ VPC (같은 네트워크, 지연 1-3ms)                │  │
│  │  EC2 t3.small ←→ RDS db.t4g.micro            │  │
│  │  (FastAPI)        (PostgreSQL)                │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
              ↓↑ (외부 서비스)
┌─────────────────────────────────────────────────────┐
│ Firebase (Google Cloud)                              │
│ ├── Firebase Auth (로그인)                           │
│ └── FCM (푸시 알림)                                  │
└─────────────────────────────────────────────────────┘
```

**근거:**

1. **비용 최적화**: RDS 프리티어 12개월 무료 ($300+ 절감)
2. **네트워크 지연 최소화**: 같은 VPC 내 1-3ms (Supabase 5-15ms 대비)
3. **기존 인프라 활용**: AWS EC2 이미 사용 중 (VPC, Security Group 재사용)
4. **FCM 통합 용이**: Firebase Auth + FCM이 같은 Firebase 프로젝트
5. **db.t4g.micro 선택**: ARM64 기반, x86 대비 20% 저렴 (~$15/월)

### 트레이드오프

**장점:**
- 초기 12개월: $15/월 (EC2만), 이후 $30/월
- 네트워크 지연 최소 (실시간 환율 서비스에 중요)
- AWS 관리형 서비스 (백업, 패치 자동)
- Firebase SDK 성숙도 높음 (iOS/Android 통합 쉬움)

**단점:**
- 12개월 후 RDS 비용 발생 (~$15/월)
- Firebase 벤더 락인 (Auth만 해당, DB는 AWS)
- Auth/Push와 DB가 다른 벤더 (관리 분산)

### 보안 고려사항

**ID Token 서버 검증 필수:**
```text
[클라이언트]
    ↓ Authorization: Bearer <Firebase ID Token>
[FastAPI 서버]
    ↓ Firebase Admin SDK로 토큰 검증
    ↓ 검증 성공 시 토큰에서 user_id 추출
    ↓ 검증 실패 시 401 Unauthorized
```

- 클라이언트가 보낸 user_id를 **절대 신뢰하지 않음**
- 서버가 ID Token 검증 후 **직접 user_id 추출** (스푸핑 방지)

### 비용 예상

| Phase | 기간 | EC2 | RDS | Auth/FCM | 합계 |
|-------|------|-----|-----|----------|------|
| **1** | 서비스 시작 ~ 12개월 | $15/월 | $0 | $0 | **$15/월** |
| **2** | 12개월 이후 | $15/월 | ~$15/월 | $0 | **$30/월** |
| **3** | 1,000명+ | $30/월 | ~$25/월 | $0 | **$55/월** |

### 향후 재검토 시점

- Firebase Auth MAU 50k 초과 시 (비용 발생)
- RDS 프리티어 종료 후 비용 검토
- 사용자 5,000명 이상 시 Supabase 자체 호스팅 검토

### 관련 결정

- [ADR-001](#adr-001-websocket-구현---python-fastapi-vs-nodejs): WebSocket 구현
- [ADR-003](#adr-003-알림-시스템---websocket-vs-push-notification): 알림 시스템

### 상세 문서

- [ALERT_SUBSCRIPTION_GUIDE.md](ALERT_SUBSCRIPTION_GUIDE.md): 전체 구현 가이드, DB 스키마, API 명세

---

## ADR-017: MIBANK 환율 파싱 - Position 기반 vs Currency-Code 기반

**날짜:** 2026-01-10
**상태:** 수락됨

### 상황

MIBANK 환율 데이터 파싱 시 **잘못된 환율이 저장되는 버그** 발생:
- 위치 기반(position-based) CSS Selector 사용: `tr:nth-child(1)`, `tr:nth-child(2)`, ...
- MIBANK 테이블의 통화 순서가 변경되면 USD 환율에 다른 통화 값이 저장됨
- 9개 은행 크롤러 모두 동일한 방식으로 영향받음

### 고려 사항

| 방식 | 장점 | 단점 |
|------|------|------|
| **Position 기반** (기존) | 구현 간단, CSS Selector만으로 추출 | 테이블 순서 변경 시 잘못된 데이터 저장 |
| **Currency-Code 기반** (채택) | 순서 변경에 안전, 명시적 통화 매칭 | 파싱 로직 복잡도 증가 |
| **개별 수정** | 최소 변경 | 9개 크롤러 각각 수정 필요, 일관성 없음 |

### 결정

**Currency-Code 기반 파싱 + 3단계 검증 시스템 도입**

**핵심 변경:**

1. **통화 코드 추출**: `href="...?currency=USD"` 파라미터에서 통화 코드 추출
2. **매매기준율 컬럼**: 마지막 셀(Last Cell)을 매매기준율로 사용
3. **공통 함수 중앙화**: `app/crawlers/utils.py`에 `crawl_mibank_rates()` 추가
4. **공통 상수**: `app/crawlers/constants.py`에 `MIBANK_REQUIRED_CODES/PAIRS/RANGES` 추가

**2026-04-27 MIBANK URL/DOM 변경 대응 업데이트:**

- URL 형식: `https://www.mibank.me/exchange/bank/index.php?search_code=088` → `https://exchange.mibank.me/bank?bank_cd=088`
- 신 DOM 통화 코드 추출: `flag_usd_*.png` 형태의 국기 이미지 파일명 사용
- 기준환율 추출: `기준환율(원)` 헤더 컬럼 인덱스를 찾아 해당 셀을 사용
- 구 DOM 호환성: `href`의 `currency=USD` 파라미터와 마지막 환율 셀 fallback은 유지

**3단계 검증 시스템:**

```
1. 완전성 검증 (Completeness)
   - USD/JPY/EUR 3개 필수 통화 누락 시 실패
   - require_all=True 옵션으로 제어

2. 절대 범위 검증 (Absolute Range)
   - USD: 1,000~2,000원
   - JPY: 600~1,400원 (100엔당)
   - EUR: 1,100~2,200원
   - 범위 초과 시 ValueError

3. 동적 변동률 검증 (Deviation Check)
   - 이전 저장값과 비교 (시간 gap 고려)
   - soft_fail: 경고 후 저장 (마지막 폴백) 또는 Selenium 재검증
   - hard_fail: 저장 보류
```

**은행별 적용 패턴:**

| 패턴 | 은행 | mibank 위치 | soft_fail 처리 |
|------|------|------------|----------------|
| **Primary MIBANK** | SC, NH, Shinhan | 1차 시도 | Selenium 재검증 |
| **Last Fallback** | IBK, BS, Citi, Woori, KB, Hana | 마지막 폴백 | 경고 후 저장 |

**Timezone 버그 수정:**

```python
# 문제: timezone-naive timestamp와 aware timestamp 비교 시 오류
# 해결: KST localize 후 차이 계산
if prev_ts.tzinfo is None:
    prev_ts = kst.localize(prev_ts)
gap_minutes = (now - prev_ts).total_seconds() / 60
```

### 구현 상세

**새 공통 함수 (`app/crawlers/utils.py`):**

```python
def crawl_mibank_rates(
    url: str,
    bank_name: str,
    required_codes: tuple = ("USD", "JPY", "EUR"),
    require_all: bool = True,
) -> dict:
    """통화코드 기반 MIBANK 환율 크롤링"""

def validate_rate_ranges(rates: dict, ranges: dict):
    """절대 범위 검증"""

def evaluate_rate_deviation(rates: dict, last_rates_info: dict, now):
    """동적 변동률 검증 (soft/hard fail)"""

def get_dynamic_thresholds(minutes_gap: float) -> tuple:
    """시간 gap에 따른 동적 threshold 반환"""
```

**은행별 얇은 래퍼 패턴:**

```python
# 예: app/crawlers/sc.py
def _crawl_mibank_sc(db: Session) -> tuple[dict, dict]:
    rates = crawl_mibank_rates(MIBANK_SC_URL, BANK_NAME, ...)
    validate_rate_ranges(rates, MIBANK_RATE_RANGES)
    last_info = crud.get_last_bank_rates_with_ts(db, BANK_NAME, ...)
    eval_result = evaluate_rate_deviation(rates, last_info, models.get_kst_now())
    return rates, eval_result
```

**DB 조회 유틸 추가 (`app/crud.py`):**

```python
def get_last_bank_rates_with_ts(db, bank_name, pairs) -> dict:
    """은행별 마지막 환율 + 타임스탬프 조회"""
```

### 트레이드오프

**장점:**
- 테이블 순서 변경에 100% 안전
- 잘못된 데이터 저장 원천 차단 (3단계 검증)
- 9개 크롤러 일관된 로직 적용
- 공통 함수로 유지보수성 향상

**단점:**
- 파싱 로직 복잡도 증가 (href 파싱 + 코드 매칭)
- 검증 단계로 인한 미세한 성능 오버헤드
- 기존 MIBANK_SELECTORS 코드 제거 필요

### 코드 정리

**삭제된 코드:**
- 9개 크롤러의 `MIBANK_SELECTORS` 딕셔너리 전체 삭제

**DEPRECATED 표시된 함수:**
- `sc.py`: `crawl_and_save_routine()` (Selenium 전용, requests 미사용)
- `nh.py`: `crawl_and_save_routine()` (Selenium 전용)
- `shinhan.py`: `crawl_and_save_routine()` (SPA 페이지, Selenium 필수)
- `ibk.py`: `crawl_and_save_routine()` (`try_crawl_with_requests()`로 대체)

### 향후 재검토 시점

- MIBANK 웹사이트 구조 변경 시 (href 파라미터명 변경 등)
- 새 통화 추가 시 (CNY 등) - `MIBANK_REQUIRED_CODES` 확장

### 관련 결정

- [ADR-006](#adr-006-selenium-크롤러-동시-실행-제어---semaphore-vs-asyncio-queue): Selenium Queue 순차 실행
- [ADR-013](#adr-013-4단계-모드-환율-고시-스케줄-기반-최적화): 4단계 모드 스케줄링

### 상세 문서

- [CRAWLERS.md](CRAWLERS.md): 크롤러별 mibank 사용 패턴 (조건부/무조건)

---

## ADR-018: Investing Cloudflare 차단 대응 - curl_cffi TLS 지문 위장

**날짜:** 2026-01-29
**상태:** 수락됨

### 상황

Investing.com 크롤러가 **Cloudflare 403 Forbidden**으로 반복 차단됨:
- 1차 장애: 2026-01-27 23:40 ~ 2026-01-28 05:23 (약 5시간 42분)
- 2차 장애: 2026-01-28 19:27 ~ 2026-01-29 12:33 (약 17시간)
- User-Agent 로테이션, Jitter, Circuit Breaker 적용 후에도 차단 지속
- **근본 원인**: Cloudflare가 TLS fingerprint(JA3/JA4)로 봇을 탐지 — HTTP 헤더와 무관

> 위 시각은 로그 기준 추정값입니다.

### 고려 사항

| 방식 | 장점 | 단점 |
|------|------|------|
| **UA 로테이션 + Jitter** (시도) | 구현 간단, 의존성 없음 | TLS 지문 탐지에 무효 |
| **curl_cffi** (채택) | 실제 브라우저 TLS 지문 위장, 경량 | 외부 의존성 추가 (libcurl 기반) |
| **Selenium** | 완전한 브라우저 환경 | 메모리 300MB+, 10초 크롤링에 과잉. 또한 현재 `--disable-javascript` 옵션 사용 중이라 Cloudflare JS Challenge를 통과할 수 없음 |
| **Proxy/IP 우회** | Cloudflare 완전 우회 | 비용 발생, 지연 증가 |

### 결정

**curl_cffi + safari17_0 impersonate 채택**

**핵심 변경:**

1. **HTTP 클라이언트 교체**: `requests` → `curl_cffi` (Investing 크롤러 전용)
2. **TLS 지문 위장**: `impersonate="safari17_0"` (Safari 17.0의 TLS 핸드셰이크 모방)
3. **Graceful 폴백**: `curl_cffi` 미설치 시 자동으로 `requests`로 폴백 (`_USE_CFFI` 플래그)
4. **다른 크롤러 영향 없음**: 은행 크롤러는 기존 `requests` 유지 (Cloudflare 미사용)

**impersonate 선택 과정:**

```
chrome131 → 403 (차단됨)
safari17_0 → 200 OK (성공)
```

Safari impersonate 성공 이유(추정): Cloudflare가 Safari TLS 지문에 덜 엄격한 정책을 적용했을 가능성

**부가 방어 체계 (동시 적용):**

- **Circuit Breaker**: 연속 403 카운트 → 점진적 쿨다운 (5회→1분, 10회→5분, 20회→15분)
- **UA 로테이션**: Safari/Chrome UA 풀에서 impersonate에 맞는 UA 선택
- **Jitter**: 0~2초 랜덤 딜레이 (Broadcasting 타이밍 고려, 최대 2초 제한)
- **로그 억제**: 상태 전이 기반 로깅 (차단시작 1회, 지속 5분마다, 해제 1회)

### 트레이드오프

**장점:**
- Cloudflare 403 차단 완전 해소
- 경량 솔루션 (Selenium 대비 메모리 1/30)
- 기존 코드 구조 최소 변경 (`_http_get()` 래퍼 함수)
- 폴백 안전성 (`_USE_CFFI` 플래그로 graceful degradation)

**단점:**
- 외부 C 라이브러리 의존성 (libcurl-impersonate)
- Cloudflare가 safari17_0 지문도 차단할 경우 재대응 필요
- Docker 이미지 크기 소폭 증가

### 적용 범위

- **적용**: `app/crawlers/investing.py` (Investing.com 크롤러만)
- **미적용**: 9개 은행 크롤러 (Cloudflare 미사용, `requests` 유지)
- **이유**: "If it ain't broke, don't fix it" — 은행 사이트는 Cloudflare를 사용하지 않으며, 불필요한 의존성 변경은 리스크

### 향후 재검토 시점

- Cloudflare가 `safari17_0` 지문도 차단 시 → 다른 impersonate 시도 또는 Proxy 도입
- curl_cffi 메이저 버전 업그레이드 시 → 호환성 확인
- 다른 크롤러에서 Cloudflare 차단 발생 시 → curl_cffi 확대 적용 검토

### 관련 결정

- [ADR-013](#adr-013-4단계-모드-환율-고시-스케줄-기반-최적화): 4단계 모드 스케줄링 (Jitter 2초 제한 근거)

### 상세 문서

- [MAINTENANCE_2026-01-29.md](MAINTENANCE_2026-01-29.md): 장애 타임라인, 원인 분석, 복구 플레이북
- [CRAWLERS.md](CRAWLERS.md): Investing 크롤러 특수 로직

---

## ADR-019: DXY 보조지표 - granularity 기반 2-part merge 전략

**날짜:** 2026-03-10
**상태:** 수락됨

### 상황

USD/KRW 환율 그래프에 **달러지수(DXY)**를 보조지표로 추가해야 함:
- 실시간 크롤링 데이터(10초~1분)와 히스토리 백필 데이터(일봉/시간봉)가 공존
- 그래프 기간별로 다른 데이터 소스 조합이 필요 (1d: realtime, 1w: realtime+hourly, 3m/1y: realtime+daily)
- Investing.com (Primary) + Yahoo Finance (Fallback) 이중 소스 → 동일 timestamp에 중복 가능
- 기존 `investing_exchange_rates` 테이블 구조로는 granularity 구분 불가

### 고려 사항

**1. 데이터 모델**

| 방식 | 장점 | 단점 |
|------|------|------|
| **기존 테이블 재사용** (bank='dxy') | 코드 변경 최소 | bank_exchange_rates 스키마와 불일치, granularity 구분 불가 |
| **별도 market_index_rates 테이블** (채택) | 범용 설계, granularity 컬럼, 향후 다른 지수 확장 가능 | 새 테이블 + 마이그레이션 필요 |

**2. 기간별 데이터 병합 (2-part merge)**

| 방식 | 장점 | 단점 |
|------|------|------|
| **단일 쿼리** (ROW_NUMBER) | 쿼리 1회 | 3m/1y에서 daily 00:00 + realtime 12:00이 같은 일일 버킷에 공존 → 데이터 오염 |
| **2-part merge (과거+오늘)** (채택) | 기간별 정확한 규칙 적용 가능 | 쿼리 2회, 로직 복잡도 증가 |

**3. 오늘 구간 병합 규칙 (1w vs 3m/1y 분리)**

| 방식 | 장점 | 단점 |
|------|------|------|
| **통합 규칙** (date-level exclusive) | 구현 단순 | 1w에서 hourly 전체 손실 (realtime 1건만으로 hourly 23건 날아감) |
| **1w/3m/1y 분리 규칙** (채택) | 각 기간의 버킷 크기에 맞는 최적 전략 | 분기 로직 필요 |

### 결정

**market_index_rates 테이블 + granularity 컬럼 + 기간별 쿼리 전략 채택**

**핵심 설계:**

1. **granularity 컬럼**: `realtime` (크롤링) / `hourly` (시간봉 백필/rollup) / `daily` (일봉 백필/rollup) 3단계 분리
2. **source 우선순위**: `investing > yahoo` (CASE WHEN ROW_NUMBER)
3. **기간별 쿼리 전략**: *(초기 2-part merge → 2026-03-13 개선)*

**기간별 DXY 쿼리 전략 (현재):**

| 기간 | 버킷 크기 | 전략 | 설명 |
|------|----------|------|------|
| **1w** | 1시간 | **full-window** | hourly + realtime 전체 7일 단일 쿼리, `hourly > realtime` dedup. gap 있어도 realtime이 자연 보충 |
| **3m/1y** | 1일 | **daily + realtime tail 7일** | daily 전체 윈도우 + realtime 최근 7일 overlap (`_DXY_DAILY_REALTIME_TAIL_DAYS = 7`). daily gap도 realtime이 보충, 전체 realtime 스캔 방지 |

> **변경 이유 (2026-03-13)**: 초기 2-part merge는 "과거(hourly/daily) + 오늘(realtime)" 분리였으나,
> backfill 종료 ~ rollup 시작 사이의 gap 구간에서 realtime이 누락되어 carry-forward 발생.
> 1w는 full-window로, 3m/1y는 tail overlap으로 전환하여 gap에 대한 내성을 확보.

**DXY 크롤러 이중 소스:** *(초기 설계 — 후속 변경은 [ADR-020](#adr-020-dxy-크롤링-아키텍처-전환--독립-크롤러에서-investing-동반-추출로) 참조)*

- ~~**Primary**: Investing.com 독립 크롤러 (curl_cffi TLS 지문 위장, ADR-018 재사용)~~
- ~~**Fallback**: Yahoo Finance (yfinance, 연속 5회 실패 OR 5분 stale 시 전환)~~
- ~~**Circuit Breaker**: DXY 전용 (investing.py와 독립)~~
- ~~**복구**: 쿨다운 해제 후 Investing 재시도, 성공 시 즉시 복귀~~
- → **현재**: Investing 환율 크롤링 시 동반 추출 + 3-tier 폴백 (ADR-020)

**배포 순서** (비가역적):
1. DB 마이그레이션 (granularity 컬럼 추가, UNIQUE 인덱스 재생성)
2. 코드 배포 (새 크롤러 + API)
3. 히스토리 백필 (yfinance daily 1년 + hourly 7일)

### 트레이드오프

**장점:**
- 범용 테이블 설계로 향후 다른 시장 지수(VIX 등) 확장 가능
- granularity 분리로 백필과 실시간 데이터 충돌 방지
- 2-part merge로 기간별 최적 데이터 품질 보장
- 이중 소스로 단일 소스 장애 시 자동 폴백

**단점:**
- 새 테이블 + 마이그레이션 필요 (Alembic 미사용, 수동 스크립트)
- 2-part merge 로직 복잡도 (crud.py + graph_cache.py 양쪽 동기화 필요)
- yfinance 의존성 추가 (Yahoo API 불안정 가능성)

### 향후 재검토 시점

- 다른 시장 지수 추가 시 (VIX, KOSPI 등) → instrument 확장
- Yahoo Finance API 변경/차단 시 → 대체 폴백 소스 검토
- 데이터 양 증가 시 → 파티셔닝 또는 아카이빙 정책

### 관련 결정

- [ADR-018](#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장): curl_cffi TLS 지문 위장 (DXY 크롤러에서 재사용)
- [ADR-015](#adr-015-websocket-graph-integration-vs-incremental-api): 그래프 WebSocket 통합 (DXY도 동일 패턴)

---

## ADR-020: DXY 크롤링 아키텍처 전환 — 독립 크롤러에서 Investing 동반 추출로

**날짜:** 2026-03-12
**상태:** 수락됨

### 상황

DXY 전용 페이지(`/indices/usdollar`) 크롤링 시 **Investing.com CDN의 캐시 품질 문제**가 발생:
- `x-cache-status: STALE` 응답이 약 40% 빈도로 발생
- Stale 응답은 1~2시간 전 가격을 반환하여 DXY 값이 실제 대비 0.10~0.17 편차
- 이미 10초마다 요청 중인 `exchange-rates-table` 페이지에 DXY(`#sb_last_8827`)가 존재하며, 해당 페이지는 `x-cache-status: BYPASS`로 관측됨 (range 0.005, 40회 테스트)
- `exchange-rates-table`의 DXY는 선물/CFD 계열이지만 기존 현물 DXY와 차이 0.00~0.02 (보조지표 용도로 무의미한 차이)

### 고려 사항

| 방식 | 장점 | 단점 |
|------|------|------|
| **독립 DXY 크롤러 유지** (기존) | 모듈 독립성, 장애 격리 | STALE 40% 발생, 추가 HTTP 요청 2회/10초 |
| **Investing 동반 추출** (채택) | STALE 해소, HTTP 요청 절반, 코드 삭제 | DXY가 investing.py에 종속 |
| **Yahoo Finance 단독** | Investing 의존 제거 | 실시간성 부족 (15분 지연), API 불안정 |

### 결정

**DXY를 investing.py의 crawl_and_save_routine()에서 환율과 함께 추출 (추가 HTTP 요청 0)**

**핵심 변경:**

1. **동반 추출**: `exchange-rates-table` 페이지에서 환율 크롤링 시 DXY(`#sb_last_8827`)도 함께 파싱
2. **DXY 전용 스케줄러 job 제거**: 4개 모드(IN/BREAK1/BREAK2/OUT) 모두에서 DXY 스케줄 삭제
3. **dxy.py 축소**: on-demand fallback 전용 모듈로 변경
   - 2차: `/currencies/us-dollar-index` (같은 선물/CFD 계열)
   - 3차: Yahoo Finance (yfinance)
4. **Fallback cooldown**: 60초 쿨다운 적용 (셀렉터 장기 파손 시 retry storm 방지)
5. **crawler_config 정리**: `dxy` 행 자동 정리 (admin UI 잔존 방지)

**Fallback 트리거 조건:**
- `#sb_last_8827` 셀렉터 파싱 실패 시 즉시 fallback 호출
- Fallback 내부에서 2차 → 3차 순차 시도

### 트레이드오프

**장점:**
- Investing.com 요청 2회/10초 → 1회/10초 (요청량 50% 감소)
- DXY STALE 문제 완전 해소 (BYPASS 페이지에서 추출)
- 코드 약 350줄 삭제 (DXY 스케줄러 + 독립 크롤링 로직)
- 스케줄러 복잡도 감소 (4개 모드에서 DXY job 관리 불필요)

**단점:**
- DXY 수집이 investing.py에 종속 (investing 차단 시 DXY도 함께 중단)
  - **보완**: fallback 체인으로 `/currencies/us-dollar-index` → yfinance 순차 복구
- DB `source='investing'` 통합 (1차/2차 구분은 로그의 fallback_url로만 가능)

**중립:**
- 선물/CFD 계열 DXY 사용 (기존 현물 대비 차이 0.00~0.02, 보조지표 용도로 무의미)

### 향후 재검토 시점

- `#sb_last_8827` 셀렉터 변경 시 → fallback URL 셀렉터도 함께 검증
- Investing.com 장기 차단 시 → Yahoo Finance 단독 모드 검토
- DXY 외 다른 보조지표 추가 시 → 동반 추출 패턴 재사용 여부 판단

### 관련 결정

- [ADR-019](#adr-019-dxy-보조지표---granularity-기반-2-part-merge-전략): DXY 보조지표 도입 (데이터 모델, 2-part merge)
- [ADR-018](#adr-018-investing-cloudflare-차단-대응---curl_cffi-tls-지문-위장): curl_cffi TLS 지문 위장 (동반 추출에서도 동일 적용)

---

## ADR-021: 환율 뉴스 피드 — Redis-only + KB/RSS 병행 수집

> 📅 **작성일**: 2026-03-28
> 🏷️ **상태**: 확정

### 맥락

환율 변동의 원인을 이해할 수 있는 뉴스를 앱에서 제공하려 한다. 연합인포맥스가 주요 소스이지만, RSS는 비단말 사용자 대상으로 ~2시간 딜레이가 있다.

### 결정

#### 1. 저장소: Redis-only (DB 불필요)

- 뉴스는 24시간 윈도우의 휘발성 데이터 → TTL 기반 자동 만료
- DB 테이블/마이그레이션/cleanup job 불필요
- Redis ZSET(시간순 인덱스) + HASH(기사 메타) 구조

**기각 대안**: PostgreSQL 저장 → 24시간 뒤 버리는 데이터에 영구 저장소는 과함

#### 2. 수집: KB API + RSS 병행

- **KB API** (fx.kbstar.com): 딜레이 없는 속보 소스, 5분마다 :15초
- **RSS** (news.einfomax.co.kr): 원문 링크 + 백필, 5분마다 :45초, ETag 조건부 GET
- nsid 기반 중복 제거, RSS 도착 시 link를 einfomax 원문으로 승격

**기각 대안**: RSS만 사용 → ~2시간 딜레이로 속보성 상실

#### 3. 정렬: 순수 시간순 (grouped sort 기각)

- 처음에는 fx > macro_severity > macro 그룹 정렬 시도
- 최신 macro 기사가 오래된 fx 기사 아래로 밀리는 UX 문제 발견
- 필터가 이미 관련성을 보장하므로, 정렬에서 추가 큐레이션 불필요

#### 4. content_type 분류 (v2 단순화)

| content_type | 설명 | 앱 동작 |
|-------------|------|--------|
| `external_link` | 일반 기사 + `*` 속보 (제목에 "(본문없음)" 추가) | link URL 열기 |
| `report_pdf` | 은행 보고서 PDF 직링크 (`[전문]` 접두사) | 외부 브라우저로 PDF 열기 |

> v1에서 `flash`, `direct_text`를 사용했으나 v2에서 삭제.

#### 5. 필터 체계 (v2 단순화)

- 모든 소스 `noise_only` 일원화 (관련도/매크로/산업 필터 삭제)
- `is_noise_title()`: 인사/부고/정치 잡음 제거
- 정치 키워드: 국민의힘, 민주당, 조국혁신당, 선거, 총선, 대선 등
- 시장 키워드 보호: 환율, 달러, 금리, 증시 등이 있으면 제외 안 함

### 영향

- 새 모듈 `app/news/` (sources, filters, fetcher, kb_fetcher, upsert)
- `app/cache.py` ZSET/HASH 메서드 확장
- `app/schemas.py` NewsItem/NewsResponse 추가
- `GET /api/news` 엔드포인트
- 스케줄러 job 2개 추가 (모드 무관, 24시간 동일)

### 관련 결정

- [ADR-008](#adr-008-queue-압력-완화-전략-실시간성-vs-완전성): 실시간성 우선 원칙 (뉴스에도 동일 적용)

---

## ADR-022: DXY Yahoo fallback 시간/가격/fresh-age 가드 정책

> 📅 **작성일**: 2026-04-27
> 🏷️ **상태**: 확정

### 맥락

DXY 운영 피드(`dxy_spot.py`)는 Investing의 `/indices/usdollar`를 1차 소스로 쓰고, 실패 시 Yahoo Finance를 최후 폴백으로 호출한다. Yahoo 폴백이 두 가지 시나리오에서 그래프를 왜곡했다:

1. **시장 마감 후 Yahoo 끼어듦**: 2026-04-27 06:00:12 KST(ICE DX 주간 개장 07:00 KST 직전) Yahoo 98.51 저장. 정상 운영 피드 첫 적재는 07:00:04 — 시장이 안 열린 60분 동안 Yahoo 값이 그래프에 잔존. 직전 Investing 값과 차이는 0.02라 단순 가격 가드로 못 잡힘.
2. **마감 직후 stale 점프**: Yahoo의 `regularMarketPreviousClose`가 ICE 정산종가(예: 금요일 98.80)를 반환. Investing 마지막 live tick과 차이가 0.27까지 벌어져 Saturday 그래프 점프 발생.

### 결정

3중 가드를 `_try_yahoo_fallback()`에 적용:

#### 1. 시간 가드 (ICE DX 주간 세션)

- `_is_dxy_weekly_session_open(now_utc)` 신규 함수
- ICE DX 선물 주간 세션(NY 일 18:00 ~ 금 17:00 ET) OFF 시 Yahoo 저장 차단
  - 토 06:00 KST DST 이후
  - 일 종일
  - 월 07:00 KST DST 이전
- DST/표준시는 `ZoneInfo("America/New_York")`이 자동 처리
- 화~금 일일 휴장(17:00~20:00 ET, KST 06:00~09:00 DST)는 의도적으로 차단 안 함 — 운영 피드가 갱신되는 사례가 관측됨

#### 2. 가격 차이 가드

- 상수 `DXY_YAHOO_DIFF_THRESHOLD = 0.07`
- `abs(yahoo_rate - latest_investing.rate) > 0.07`이면 저장 보류
- 정상 분포 max(0.06) 직바깥 안전마진. 4/22 dual log + 4/27 평일 측정 기반.

#### 3. Fresh-age 조건 (가격 가드 한정)

- 가격 차이 가드는 **latest_investing이 fresh일 때만** 적용
- fresh 기준: 모드별 grace (IN: 15분, BREAK: 30분)
- Investing이 stale(예: 1시간 장애)일 때 Yahoo는 유일 대체 소스이므로, 가격 차이만으로 가드하면 그래프가 끊김 → fresh 조건으로 우회

### 영향

- 시장 시간 안의 Yahoo outlier 차단됨 (weekend Friday close 점프)
- 시장 마감 후 Yahoo 끼어듦 차단됨 (4/27 06:00 케이스)
- Investing 장애 시 Yahoo가 정상적으로 데이터 보충 가능
- 가드 순서: 시간 가드(진입 직후) → 기존 mode 보존 정책 → Yahoo fetch → fresh + 가격 가드 → 저장

### 기각 대안

| 대안 | 기각 사유 |
|---|---|
| 가격 가드만 (fresh 조건 없음) | Investing 장애 시 정상 Yahoo도 차단 — 그래프 끊김 |
| Yahoo `lastPrice` 우선으로 전환 | weekend stuck은 lastPrice도 마지막 값 고정이라 동일 발생 |
| Yahoo 완전 제거 | DXY 단일 장애 시 보충 수단 사라짐 |
| TradingView로 fallback 교체 | undocumented endpoint + ToS 회색지대, 데이터 신뢰성 미검증 |

### 관련 결정

- [ADR-020](#adr-020): DXY 운영 피드를 Investing primary로 전환 — 이 가드는 그 후속 보강
- [ADR-024](#adr-024-미국달러지수-선물-분리-저장--dxy_mode-제거): 같은 라운드 데이터 모델 변경

---

## ADR-023: 데이터 보관 정책 30일 통일 (bank/source_rates/DXY realtime)

> 📅 **작성일**: 2026-04-27
> 🏷️ **상태**: 확정

### 맥락

기존 보관 정책이 테이블별로 달랐다:
- 은행 환율(`bank_exchange_rates`): 10일
- USDT 거래소 가격(`source_rates`): 10일
- DXY(`market_index_rates`): 장기 보관 (cleanup 함수 없음)

dxy_futures(미국달러지수 선물) 추가 시점에 정책을 통일하면서 DXY raw 데이터의 무한 누적 문제도 함께 해결할 필요가 있었다.

### 결정

#### 1. 30일 통일

- 은행 환율: 10일 → **30일**
- USDT 거래소 가격: 10일 → **30일**
- DXY 현물/선물 realtime: **30일** (신규 cleanup)

#### 2. DXY rollup 보존 (granularity 별 정책)

- `realtime` granularity: 30일 cap
- `hourly`/`daily` rollup: **삭제 안 함** (장기 그래프 보존)
- 이유: 3m/1y 그래프는 daily 데이터가 90일/365일 필요. realtime 30일 cap은 1d/1w 그래프 윈도우(24h, 7일)에 영향 없음.

#### 3. 구현

- `crud.delete_old_market_index_rates(days=30, granularities=None)` 신규
  - default `granularities=["realtime"]` — hourly/daily는 호출자가 명시해야 삭제
- `app/scheduler.py` 상수: `BANK_RETENTION_DAYS=30`, `SOURCE_RATE_RETENTION_DAYS=30`, `MARKET_INDEX_RETENTION_DAYS=30`
- 매일 03:30~03:32 KST 순차 실행

### 영향

| 테이블 | 변경 전 | 변경 후 | 디스크 (≈100B/row) |
|---|---|---|---|
| bank_exchange_rates | 10일 | 30일 | 3x ≈ 4MB |
| source_rates | 10일 | 30일 | 3x ≈ 40MB |
| market_index_rates (realtime) | 무한 | 30일 | ≈ 12MB cap |
| market_index_rates (hourly) | 무한 | 무한 | 1.7MB/년 |
| market_index_rates (daily) | 무한 | 무한 | 73KB/년 |

RDS 20GB 무료 tier 대비 0.3% 미만. 디스크 부담 없음.

#### 그래프 일관성

| 기간 | 사용 granularity | 윈도우 | 30일 cap 영향 |
|---|---|---|---|
| 1d | realtime | 24h | 안전 |
| 1w | realtime + hourly | 7일 | 안전 |
| 3m | daily + realtime tail (7일) | 90일 | daily 보존으로 안전 |
| 1y | daily + realtime tail (7일) | 365일 | daily 보존으로 안전 |

### 기각 대안

| 대안 | 기각 사유 |
|---|---|
| 모든 granularity 30일 cap | 3m/1y 그래프 깨짐 (daily 90일/365일 필요) |
| 90일 통일 | 디스크 과적, 가치 낮음 |
| 테이블별 다른 cap 유지 | 정책 복잡도 ↑, 운영 일관성 ↓ |

### 관련 결정

- [ADR-019](#adr-019-dxy-보조지표---granularity-기반-2-part-merge-전략): granularity 기반 그래프 전략 — 이 정책이 hourly/daily 보존 근거
- [ADR-024](#adr-024-미국달러지수-선물-분리-저장--dxy_mode-제거): 같은 라운드, dxy_futures에도 같은 보관 정책 적용

---

## ADR-024: 미국달러지수 선물 분리 저장 + DXY_MODE 제거

> 📅 **작성일**: 2026-04-27
> 🏷️ **상태**: 확정 (ADR-020 부분 supersede)

### 맥락

향후 테더 탭에서 KRX 미국달러 선물과 비교 그래프를 그릴 계획이 있다(REALTIME_ARCHITECTURE_PLAN.md). 그러려면 ICE DX 선물 데이터를 별도로 축적해야 한다.

ADR-020에서는 Investing의 `exchange-rates-table` `#sb_last_8827`을 현물 DXY와 동일한 `instrument='dxy'`로 저장했다. 그러나 이 셀은 사실 선물/CFD 계열이므로, 현물(`/indices/usdollar` 기반 spot)과 섞여 있던 것을 분리할 시점이다.

또한 `DXY_MODE` 환경변수(`futures_coupled` vs `spot_independent`)는 원래 둘 중 하나를 DXY로 쓸지 고르는 toggle이었으나, 이제 둘 다 별도 instrument로 저장하므로 의미를 잃었다. 잘못 설정되거나 .env가 비면 task_dxy가 등록되지 않아 현물 DXY 수집이 끊기는 회귀 위험만 남는 잔재.

### 결정

#### 1. 데이터 모델 분리

- 현물/운영 DXY: `instrument='dxy'` (기존)
- 미국달러지수 선물: `instrument='dxy_futures'` (신규)
- 같은 `market_index_rates` 테이블, 다른 instrument 값
- DB 스키마 변경 없음 (instrument 컬럼 활용)

#### 2. 수집 방식

- **dxy** (현물): `dxy_spot.py` 독립 크롤러 (`/indices/usdollar`) — 변경 없음
- **dxy_futures**: `investing.py` **부가 수집** — exchange-rates-table 환율 fetch와 같은 HTTP 응답에서 `#sb_last_8827` 추출. 추가 네트워크 비용 0
  - 셀렉터 실패 시 `/currencies/us-dollar-index` 별도 폴백 (60초 쿨다운)
  - Yahoo는 선물 저장에 사용 안 함 (다른 시점 데이터 노이즈 방지)

#### 3. crawler_config 단위

- dxy_futures는 별도 `crawler_config` 엔트리 없음
- 활성화/비활성화는 `crawler_config.investing` 토글에 종속 (같은 fetch 단위)
- 향후 dxy_futures가 1급 데이터화(독립 모니터링/주기/장애 대응)되면 별도 등록 검토

#### 4. DXY_MODE 제거

- `app/config.py`에서 상수 제거
- `app/scheduler.py` import + 4곳 분기 제거 — `task_dxy`는 항상 등록 (crawler_config.dxy 토글만 적용)
- 운영 EC2 `.env`의 `DXY_MODE` 라인은 무해하지만 정리 권장

#### 5. 공용 저장 함수

- `crud.insert_market_index_rate_into_db(instrument, rate, source, granularity)` 일반화
- 기존 `insert_dxy_rate_into_db()`는 `instrument='dxy'` wrapper로 호환 유지

### 영향

- 새 데이터 시리즈 축적 시작
- 현재 단계: **수집 + DB 저장만 완료**
  - 후속 작업: 최신값 조회 API, 그래프 API, rollup(hourly/daily), 테더 탭 연결, `source_registry` 등록, 알림/표시명
- ADR-020 결정(선물/현물 동반 추출 + 단일 instrument)은 부분 supersede:
  - 현물은 ADR-020 이후 spot_independent 모드로 이미 분리됨
  - 선물은 이번 ADR로 분리 저장
- DXY_MODE 환경변수 회귀 위험 제거

### 기각 대안

| 대안 | 기각 사유 |
|---|---|
| `MarketIndexRate`에 `contract_type` 컬럼 추가 | 스키마 마이그레이션 필요, instrument 활용으로 충분 |
| 별도 테이블 `dxy_futures_rates` | 그래프 쿼리 합병 복잡, 현 모델로 일반화 가능 |
| Yahoo도 선물 폴백에 사용 | Yahoo는 ICE 선물 시간과 다른 시점일 가능성, 노이즈 우려 |
| `crawler_config.dxy_futures` 별도 등록 | 같은 fetch 단위라 분리 의미 없음 (raw 축적 단계) |
| DXY_MODE 기본값만 변경 (잔재 유지) | 잔재 자체가 회귀 위험, 잘못 설정 시 task_dxy 미등록 |

### 관련 결정

- [ADR-020](#adr-020-dxy-크롤링-아키텍처-전환--독립-크롤러에서-investing-동반-추출로): 이전 DXY 크롤링 아키텍처 — 부분 supersede됨
- [ADR-022](#adr-022-dxy-yahoo-fallback-시간가격fresh-age-가드-정책): 같은 라운드 Yahoo fallback 가드
- [ADR-023](#adr-023-데이터-보관-정책-30일-통일-banksource_ratesdxy-realtime): 같은 라운드 보관 정책 — dxy_futures도 동일 적용

---

## ADR-025: DXY 현물 외부 fallback 체인 — CNBC 추가 + Yahoo 격하

> 📅 **작성일**: 2026-04-28
> 🏷️ **상태**: 확정 (ADR-022 후속 강화)

### 맥락

ADR-022에서 Yahoo fallback에 시간 가드 + 가격 가드 + fresh-age 조건을 추가했지만, **Yahoo의 근본 한계 자체는 그대로**였다:
- 운영 EC2 평일 30샘플 측정에서 Yahoo `regularMarketPreviousClose`의 source data 신선도 median = **601.8초 (10분 stale)**
- p95는 ~99분 stale — 즉 5%의 시간은 1시간 이상 옛 값을 반환
- 가드는 outlier를 차단할 뿐, "더 신선한 fallback"을 제공하진 않음

같은 측정에서 **CNBC `.DXY` quote endpoint**가 다음 특성을 보임:
- HTTP 200, latency median 326ms
- "ICE U.S. Dollar Index" 명시 (출처 안전성)
- Investing primary와 차이 median 0.006 (거의 동일)
- 인증 불필요, JSON 깔끔, ToS 비교적 안전 (자사 사이트 quote endpoint)

### 결정

#### 1. 외부 fallback chain 도입

기존 단일(Yahoo)에서 chain으로 일반화:
```
1차: Investing /indices/usdollar __NEXT_DATA__   [primary, 변경 없음]
2차: Investing /indices/usdollar CSS              [primary fallback, 변경 없음]
3차: CNBC .DXY                                    [NEW, 외부 fallback 1순위]
4차: Yahoo Finance                                [최후 보루로 격하]
```

#### 2. 공통 가드 정책 적용 (CNBC도 Yahoo와 동일)

- **시간 가드**: ICE DX 주간 세션 OFF 시 chain 전체 차단
- **mode 보존 정책**: IN/BREAK/OUT 모드별 grace + failure threshold
- **fresh-age diff guard**: latest_investing이 fresh일 때만 0.07 임계값 적용
- 모든 가드는 ADR-022와 동일

#### 3. ⭐ Chain 내 diff guard 차단 시 후속 source도 차단

Chain의 한 source가 fetch 성공했지만 가격 가드에 걸리면, **다음 source(Yahoo)도 시도하지 않음**.

이유:
- 외부값(CNBC)이 fresh Investing과 0.07 이상 차이난다는 건 **outlier 신호**
- Yahoo는 더 stale(10분 지연) → 더 outlier일 가능성 높음
- chain 전체 종료가 안전 (Investing이 fresh하니 DXY 데이터는 멈추지 않음)

단, **fetch 실패는 다음 source로 진행**. 가드 차단(외부값 자체 의심)과 fetch 실패(네트워크 등 일시 장애)는 명확히 구분.

#### 4. 우선순위 일관성 (DB 조회/그래프)

DB에 `source='cnbc'` 들어오면 기존 조회/그래프 로직이 무시하지 않도록 우선순위 수정:
- `crud.get_latest_dxy_rate()`: `case((investing, 0), (cnbc, 1), else_=2)`
- `app/admin/graph_cache.py` 6곳: `CASE WHEN source = 'investing' THEN 0 WHEN source = 'cnbc' THEN 1 ELSE 2 END`
- 결과: Investing > CNBC > Yahoo 우선순위 일관 적용

#### 5. 운영 메타 필드 유지

`_try_yahoo_fallback → _try_external_fallback` 리팩토링 시 기존 로그 필드 유지:
- `mode`, `last_fresh_age_seconds`, `investing_rate_age_seconds`, `consecutive_failures`, `last_fresh_source`, `investing_rate`, `diff`, `threshold`
- 신규: `fallback_source` (값: `"cnbc" | "yahoo"`) — 어느 폴백이 발동했는지 즉시 식별

기존 호출 위치 호환을 위해 `_try_yahoo_fallback = _try_external_fallback` alias 유지.

### 영향

- Yahoo fallback 발동 빈도 감소 (CNBC가 먼저 잡음)
- weekend/마감 후 stale 위험 감소 — CNBC도 시간 가드 적용으로 같은 보호
- DB `source` 컬럼에 새 값 `'cnbc'` 추가 (스키마 변경 없음)
- 운영 모니터링: `source='cnbc'` 적재 빈도/비율 관찰 → 며칠 후 가치 정량 확인

### 기각 대안

| 대안 | 기각 사유 |
|---|---|
| TradingView TVC:DXY를 fallback에 포함 | undocumented endpoint + ToS 회색지대, 시간대 stale 편차 미검증 (validate_dxy_candidates.py 측정 누적 후 재검토) |
| CNBC fetch 실패 시에도 Yahoo 차단 | fetch 실패와 outlier는 다른 신호 — 일시 장애로 Yahoo까지 차단하면 데이터 끊김 |
| CNBC를 Investing primary 위치로 격상 | Investing primary는 운영 검증된 신뢰 소스, 변경 위험 큼 |
| Yahoo 완전 제거 | DXY 단일 fallback이 사라지면 CNBC 차단 시 Yahoo 없이 데이터 끊김. 이중 안전망 유지 |

### 관련 결정

- [ADR-022](#adr-022-dxy-yahoo-fallback-시간가격fresh-age-가드-정책): 시간/가격/fresh-age 가드 — CNBC도 동일 적용
- [ADR-024](#adr-024-미국달러지수-선물-분리-저장--dxy_mode-제거): 같은 시점 운영 검증으로 CNBC가 더 신선한 후보임을 확인

---

## ADR-026: Redis-first broadcast hot path — latest mirror + DXY mirror로 DB-free 달성

> 📅 **작성일**: 2026-05-04
> 🏷️ **상태**: 확정 (PR3-PR5 시리즈 측정 완료)

### 맥락

24h fast PoC (2026-05-01 15:53 KST~) 측정에서 매초 broadcast가 DB latest SELECT (rates + DXY)에 직접 묶여 있어 정각 spike + Performance Insights에서 wait event가 CPU 단일로 관측되고 LWLock/Lock/IO:WalSync 0건. 같은 구간 PI Top SQL에서 bank+source latest SELECT가 부하의 대부분(bank 0.21 + source 0.15 AAS = 0.36)을 차지.

EC2 t3.small + RDS db.t4g.micro에서 broadcast hot path가 DB CPU에 직접 종속된 구조. 사용자 영향은 정각 spike(payload_build_ms p99 ~340-390ms / max ~1.3초 추정) + 매분 정각 정체.

이 시점에 broadcast 사용자 경로만 떼어내 Redis layer로 분리할 필요가 명확. 옵션:
- (A) DB SELECT 유지 + 캐시 추가 (status quo 보강)
- (B) WebSocket push 직접 (event-driven, broadcast topic protocol 결정 선행 필요)
- (C) Redis latest mirror layer (이 ADR)

### 결정

#### 1. PR3 — Redis latest mirror + Redis-first broadcast read

- 신규 모듈 `app/latest_rates_cache.py` (cache.py·crud.py 변경 0)
- mirror cycle: APScheduler IntervalTrigger 3초
- key 구조:
  - `latest:bank:{bank}:{currency}` — 은행 환율
  - `latest:source:{source}:{asset}` — USDT 거래소
  - `latest:investing:{currency}` — Investing 기준
  - `latest:index` — atomic snapshot 제어 키 (mirror 일관성 보장)
- value: `{rate, timestamp, mirrored_at}` JSON
- broadcast: `fetch_rates_from_redis()`로 Redis 직접 read → 실패 시 DB fallback
- 신규 env: `REDIS_LATEST_ENABLED` (단일 토글) + `LATEST_MIRROR_INTERVAL_SECONDS` (mirror 주기, PoC 후 1/3/5초 비교 영역)
- 신규 메트릭: `latest_source` (redis/db_fallback), `mirror_age_ms`, `fallback_reason` (redis_miss/redis_stale/redis_error/circuit_open)

#### 2. PR3.5 — 분해 계측

- broadcast 내부 단계별 timing 추가 (`latest_index_get_ms`, `latest_index_parse_ms`, `latest_data_get_ms`, `latest_decode_ms`, `payload_assemble_ms`, `payload_assemble_without_dxy_ms`, `payload_build_unmeasured_ms`)
- 잔존 long tail 원인 식별 목적 (PR3에서도 ~195ms p99 / 611ms max 잔존)

#### 3. PR4 — MGET 1회로 통합

- `fetch_rates_from_redis()` 내부 35 sequential GET → 1 MGET
- 분해 계측에서 latest_data_get_ms가 dominant contributor였음 — MGET으로 RTT 1회로 축소

#### 4. PR5 — DXY mirror + DXY-only fallback

- 추가 키: `latest:dxy:current` (mirror cycle 동일 3초)
- DXY-only fallback 패턴: rates Redis 성공 + DXY Redis 실패 시 **DXY만** DB 조회 (rates 재조회 없음)
- 신규 메트릭: `dxy_path` (redis/db_fallback/missing), `latest_dxy_get_ms`, `latest_dxy_fallback_reason`
- DXY mirror 부분 실패도 WARNING 트리거 (`dxy_failed > 0`)

### 영향 — 측정 결과 (직접 검증)

baseline 명확히 구분:

| stage | 측정 윈도우 | n | p99 (ms) | max (ms) | DB hot path | 출처 |
|---|---|---|---|---|---|---|
| PR3 이전 baseline | (참고) | — | ~340~390 | ~1300 | rates+DXY DB SELECT | Codex 보고 (직접 검증 미실시) |
| PR3.5 baseline | 30분 | 1853 | 195 | 611 | rates Redis-first / DXY DB | 직접 측정 |
| PR4 후 30분 | 30분 | 1827 | 86.39 | 229.47 | rates Redis-first 100%, DXY DB 조회 유지 | 직접 측정 |
| PR5 30분 | 30분 | 1871 | 35.77 | 109.61 | rates+DXY 모두 Redis-first 100%, dxy_query_ms n=0 | 직접 측정 |
| **PR5 24h 누적** | 24h | **31495** | **30.25** | 415.14 | **DB hot path 0건** | 직접 측정 |
| PR5 IN mode 30분 | 09:00-09:30 KST | 1801 | 75.97 | 835.45 | DB hot path 0건 | 직접 측정 |

핵심 효과:
- **DB hot path 100% → 0%** (rates + DXY 모두 Redis-first hit 100%, n=31495+1801)
- **PR3.5 → PR5 24h: p99 195 → 30.25 (6.4× 가속)**
- **dxy_query_ms count: 1827 → 0** (PR4 → PR5)
- redis_hit_rate (rates + DXY): 100% (24h 누적 + IN mode 모두)

### 한계 / 운영 환경 (IN mode)

영업시간 IN mode (2026-05-04 09:00-09:30 KST 30분 측정)에서 outlier 비율 증가:
- ≥300ms 비율: weekend 24h 0.029% → IN mode 0.111% (~4× 증가)
- p99: weekend 30.25ms → IN mode 75.97ms (~2.5× 증가)
- mirror_age p99: weekend 2301ms / IN mode 2398ms (정상, 3초 ceiling 이내)
- mirror_age max: weekend 3477ms / IN mode 4314ms (각각 1건씩 ceiling 1회 초과)

원인 분석 (row-level):
- top spike row가 latest_index_get_ms 또는 latest_data_get_ms 단일 step dominant (예: pb 835ms 중 idx 762ms)
- co-spike (2개 이상 step 동시 spike): weekend 7.5h 0건, IN mode 30분 1건 (n=1801 중 1건이라 통계 신호 약함)
- 결론: **Redis read wall-clock jitter가 영업시간에 확장**되는 패턴
- DB 경합 / DXY fallback은 주요 원인으로 보기 어려움 (mirror_age p99 정상, redis hit 100%). mirror cycle은 p99 기준 안정이나 max 기준 1회성 지연이 있어 운영 신호 누적 후 재평가.

### 트레이드오프

- mirror cycle 3초 = 사용자가 받는 데이터의 max age 약 2.4~2.5초 (실측 mirror_age p99)
- DB 부하: mirror SELECT 분당 20회 (3초 주기 기준) — 무시할 수준
- 운영 복잡도: Redis 추가 의존 + fallback 경로 + Circuit Breaker 가시화
- mirror cycle 1회 누락 outlier (ceiling 4314ms 1건) — 24h+30min에 1~2건 수준

### 후속 PR 정당성 (현재 즉시 정당화 X)

- **crawler write-through** (REALTIME_ARCHITECTURE_PLAN.md PR4 원래 계획): mirror_age p99 정상이라 즉시 정당화 X. 정당화 신호: mirror_age p99 ≥ 3초 또는 사용자 UX 피드백
- **Redis 진단** (SLOWLOG / cgroup CPU / pipeline 통합): IN mode jitter 원인 추적용. 정당화 신호: IN mode outlier 비율 추가 증가 또는 사용자 영향 가시화
- **mirror cycle 조정**: 1/3/5초 비교는 운영 신호 누적 후 결정 (현재 3초 안정)

### 기각 대안

| 대안 | 기각 사유 |
|---|---|
| (A) DB SELECT 유지 + 캐시 추가 | 정각 spike + DB CPU 종속 미해결 |
| (B) WebSocket push 직접 (event-driven) | 큰 변경, broadcast topic protocol 결정 선행 필요 |
| crawler write-through 즉시 (mirror 우회) | 16개+ 크롤러 변경 + 분산 일관성 부담, mirror로 충분히 해결 |
| mirror cycle 1초 | broadcast와 1:1 → mirror 의미 약화 + jitter 시 stale 비율 급증 |
| mirror cycle 5~10초 | UX max age 5~10초 stale 가능성 |
| latest:* 캐시를 cache.py 공용 모듈로 통합 | 도메인 격리(Redis latest layer)가 더 명확 + 회귀 위험 분리 |

### 관련 결정

- [ADR-008](#adr-008-queue-압력-완화-전략-실시간성-vs-완전성): broadcast 실시간성 강화 시리즈
- [ADR-010](#adr-010-websocket-broadcasting-스케줄링-방식): broadcast cron 전환 — Phase 2 같은 시리즈
- [REALTIME_ARCHITECTURE_PLAN.md v0.8](REALTIME_ARCHITECTURE_PLAN.md): PR3-PR5 설계 + 측정 결과 본문

---

## ADR-027: KRX 미국달러선물 Stage 2 진입 전 REST snapshot/fallback + stale 정책 (초안)

> 📅 **작성일**: 2026-05-06
> 🏷️ **상태**: Stage A/B + Stage C guard(판정+telemetry) + robust calendar 모두 배포 완료. **telemetry canary 활성화 (2026-06-07 주말 — EC2 deploy `23df2d2` + `KRX_REST_FALLBACK_ENABLED=true` + recreate, verified: health·calendar live(1/1=False)·KRX bootstrap A75606 ERROR 0)**. write/broadcast 0 (telemetry-only). 주말 guard idle. **6/8 1차 관찰(CF 08:30~15:45 + CM 18:00~ read-only 2회 + 독립 교차 확인): stale 0 / guard 발화 0(`rest_guard_pass`·`rejected_{calendar,contract,session}` 0) / fallback 0(`evaluated`·`eligible`·`rest_success`·`rest_error` 0) / write·broadcast 0(`fallback_last_at` null) = WS 전 구간 안정으로 stale fallback 불필요했던 정상 결과.** `evaluate()`는 `status==stale`에서만 호출 → **4-gate guard 로직은 production 미실증**(첫 실측은 향후 WS stale 60s+ 이벤트 시점). counters in-memory(recreate 시 reset), running=6/7 canary 빌드 무중단. **Stage C 실 반영(topic/Redis write)은 미구현** — ADR-028 topic protocol + KRX Stage 2 선행. 설계: 하단 §Stage C guard 설계.

### 맥락

PR6 시리즈로 KRX 미국달러선물(`source="krx", asset="usd-krw-futures"`)을 선택적 데이터 소스로 추가했다.

현재 운영 상태:
- Stage 1 canary 활성: `KRX_FUTURES_ENABLED=true`, `KRX_BROADCAST_INCLUDE=false`
- KIS WebSocket → `source_rates` DB 저장까지 동작
- `latest:index`에는 KRX key를 넣지 않아 broadcast/app 노출 없음
- 2026-05-05 어린이날 휴장 후 2026-05-06 08:30 CF 정규세션 재진입 확인
- PR6e로 새 row rate를 0.1 KRW tick으로 정규화하고, 세션 경계 `_last_tick_at` carry-over에 따른 불필요한 stale 전이를 차단

Stage 2에서 KRX는 사용자에게 노출되어야 한다. 이때 WebSocket 단절, KIS approval 장애, 휴장/세션 break, 만기일 contract rollover가 사용자 화면에 stale 또는 잘못된 값으로 노출될 수 있으므로 REST snapshot/fallback 및 stale 정책이 필요하다.

> ⚠️ **Stage 2 노출 채널 재정의 (2026-05-06, [ADR-028](#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit) 합의)**: Stage 2 진입 시 KRX 노출은 **legacy `rates` 배열이 아니라 새 topic 채널** (예: `krx:usd-krw-futures`). `KRX_BROADCAST_INCLUDE=true`는 legacy 통합 트리거가 아니라 topic 발사 트리거로 의미 재정의. 본 ADR-027의 fallback/stale 정책은 topic 채널 기준으로 적용한다 (legacy `rates`에 KRX 등장은 새 계약 위반이라 진행하지 않음).
>
> 📝 **Historical note (Z-2d cleanup 2026-05-12)**: `KRX_BROADCAST_INCLUDE` env는 Z-2d cleanup에서 제거됨. legacy 노출 정책은 `app/legacy_policy.should_include_source_in_legacy_rates` allowlist로 통일 — KRX는 미포함이라 항상 차단. 본 ADR의 historical 토글 참조는 의사결정 기록 그대로 보존 (immutable history).

### 원칙

1. **KRX optional source 유지**: KRX 실패는 baseline 서비스(은행 + investing + USDT + DXY)와 FastAPI startup/shutdown에 영향 0이어야 한다.
2. **WebSocket primary / REST bounded fallback probe** (2026-05-06 명시):
   > WebSocket remains primary. REST snapshot is not a replacement mode; it is a bounded fallback probe used only while WebSocket frame silence exceeds stale threshold. Any valid WebSocket frame immediately restores WebSocket as primary.

   - WebSocket이 항상 primary source. REST는 대체 모드가 아니라 stale 동안만의 보조 snapshot 경로.
   - **WebSocket status 복귀**: frame 1건 도착 즉시 `normal` (cooldown 무관 — 코드 [krx_kis.py:454-455](app/crawlers/krx_kis.py#L454-L455)의 자동 stale → normal transition 그대로 활용).
   - **REST 호출 cooldown**: REST 호출에만 적용 (중복 폭주 방지). status 복귀를 cooldown으로 늦추지 X.
3. **Stage 2 전 stale 노출 방지**: WebSocket latest가 stale이면 topic publish에서 KRX를 제외한다. (Stage 2 노출 채널은 [ADR-028](#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit) 합의대로 topic 채널이며 legacy `rates`가 아님.)
4. **DB row gap ≠ raw frame gap** (2026-05-06 명시): DB는 `insert_source_rate_if_changed` + 1초 window debounce 정책상 가격 변동 시에만 INSERT. WebSocket frame은 가격 무변동 시에도 호가 frame(H0xASP0)이 계속 도착해 `_last_tick_at`을 갱신 → stale 임계값 baseline은 **raw frame age**이고 DB row gap이 아니다. DB row gap은 throughput / 가격 변동성 / 저장 부하 지표로만 사용.
5. **검증되지 않은 cron 시각은 코드에 박지 않음**: 마스터 갱신 시각, 만기일 11:30 이후 KIS WebSocket 동작, 다음 월물 subscribe 가능 시점은 운영 관찰 후 결정한다.

### 결정 후보 (Stage 1 데이터로 확정 예정)

| 항목 | 후보 | 현재 판단 |
|---|---|---|
| Stale 임계값 (`KRX_STALE_SEC`) | 30s / **60s** / 세션별 동적 (CF 60s + CM 90~120s 분리) | 코드 상수 `STALE_AFTER_SEC=60` 기준값 유지. 5/8 baseline + PR6d-2 raw frame metric 후 CM 분리 여부 결정 |
| REST snapshot 호출 주기 | WebSocket 정상 시 0회 / active session 중 주기 보조 / stale 시에만 호출 | **stale trigger 시에만 호출** (WebSocket 정상 시 0회) |
| **REST → WebSocket 복귀 기준** | frame 1건 즉시 / N초 hysteresis / reconnect 명시 | **frame 1건 도착 즉시 normal** (코드 자동 transition 그대로). cooldown 무관 |
| **REST 호출 cooldown (`KRX_REST_COOLDOWN_SEC`)** | 0s / 30s / 60s | **30s** 후보 (중복 폭주 방지, status 복귀에는 적용 X) |
| **stale 지속 시 REST 재호출 주기** | 호출 후 재호출 X / cooldown마다 1회 / 적응형 | **cooldown마다 1회** 후보 |
| REST 성공 시 저장 위치 | `source_rates`에 insert-if-changed / topic snapshot만 갱신 | ~~DB 저장까지 수행해 provenance 유지~~ → **superseded by 2026-05-25 follow-up**: KIS REST stale 본질 + 5/19/5/25 두 사례로 close write source 부적합 확정. close snapshot REST는 default off (write 차단, diagnostic만 유지). stale 시 호출 일반 REST snapshot/fallback은 본 ADR Stage C 결정 영역. topic snapshot/publish 경로는 ADR-028 기반 Phase Z-2에서 확정 |
| REST 실패 시 정책 | KRX topic publish 제외 / 마지막 값 유지 / stale status 동반 | Stage 2 topic에서는 KRX publish 제외가 기본. 마지막 값 유지 + stale status는 topic schema 확정 후 재검토 |
| **REST quote endpoint/TR** | KIS 상품선물 REST endpoint 재조사 / REST 없이 WebSocket stale 제외 정책 | **resolved**. `FHMIF10000000` + `/inquire-price` 유지, `FID_COND_MRKT_DIV_CODE`를 세션별 `CF`/`CM`으로 분기해야 A75605 USD futures `output1.futs_prpr` 반환 |
| 만기일 rollover | cron / session boundary resolve / 수동 restart | **PR6c-2d-1 (2026-05-07) 결정**: 만기일 07:00 KST swap + 5분 reconcile cron (임시 안전모드). 5/18 검증 통과 후 hybrid (06:01 daily + session boundary)로 축소 검토 (PR6c-2d-5 후보). 자세한 내용은 본 ADR 하단 "PR6c-2d-1 amend" 절 참조 |

### 초기 정책값 (PR6d-1 진입 시 env 후보)

| Env | 초기값 | 비고 |
|---|---|---|
| `KRX_REST_FALLBACK_ENABLED` | `false` (default) | PR6d 코드 배포해도 default false면 fallback logic 미호출. Stage 1 안에서 별도 토글로 검증 가능 (KRX_BROADCAST_INCLUDE=false 상태라 사용자 노출 0) |
| `KRX_STALE_SEC` | `60` | 코드 기존 상수와 동일. 5/8 baseline + raw frame metric 후 조정 |
| `KRX_REST_COOLDOWN_SEC` | `30` | REST 호출 cooldown. status 복귀와 무관 |
| (미래) `KRX_STALE_SEC_CF` / `_CM` | 미설정 | CM 저거래량 false stale 검증 후 분리 결정 |

### PR6d-1 REST smoke 결과 (2026-05-06)

PR6d-1 운영 배포 후, 현재 helper(`FHMIF10000000` + `/uapi/domestic-futureoption/v1/quotations/inquire-price`)를 A75605 미국달러선물 contract로 1회 smoke 검증했다.

| 항목 | 결과 |
|---|---|
| REST access token | 정상 발급 + `.cache/kis_access_token.json` 생성 (token length 346, 약 24h 만료) |
| HTTP / KIS result code | HTTP 200 + `rt_cd=0`, `msg_cd=MCA00000` |
| USD futures price | **미확보** — `futs_prpr` / `prpr` 필드 부재 |
| 실제 응답 | `output2.bstp_nmix_prpr=7384.56` / `output3.bstp_nmix_prpr=1129.63` 등 KOSPI/KOSPI200 지수 출력 |
| 판단 | access token/cache 경로는 검증 완료. quote endpoint/TR은 지수선물 샘플 경로이며, A75605 USD futures snapshot source로는 미검증이 아니라 현재 smoke 기준 부적합 |

따라서 단순 `FID_COND_MRKT_DIV_CODE=F`는 USD futures fallback에 사용할 수 없다.

**추가 endpoint 조사 (2026-05-06, 공식 KIS 샘플 + 운영 smoke):**

- KIS 공식 API portal의 `[국내선물옵션] 기본시세` 목록은 선물옵션 시세/시세호가/기간별시세/분봉조회/전광판 계열이고, `[국내선물옵션] 실시간시세`에만 `상품선물 실시간호가` / `상품선물 실시간체결가`가 별도 존재.
- KIS 공식 GitHub 샘플 tree 기준 `commodity_futures_realtime_conclusion` / `commodity_futures_realtime_quote`는 WebSocket 샘플만 존재. 상품선물 REST current quote 샘플은 확인되지 않음.
- `inquire-asking-price` (`FHMIF10010000`) + `FID_COND_MRKT_DIV_CODE=F` + `A75605` 운영 smoke 결과: `rt_cd=0`이지만 `output1={}`, `output2={}`.
- `inquire-time-fuopchartprice` (`FHKIF03020200`) + `A75605` 운영 smoke 결과: `rt_cd=0`이지만 `output1={}`, `output2=[]`.
- `inquire-daily-fuopchartprice` (`FHKIF03020100`) + `A75605` 운영 smoke 결과: `rt_cd=0`이지만 `output1={}`, `output2=[]`.
- `display-board-futures` (`FHPIF05030200`)는 `MKI`/empty 조건에서 지수선물 board만 반환했고, `A75605` / `미국달러` row는 없음.
- 이후 같은 `inquire-price` endpoint/TR에서 `FID_COND_MRKT_DIV_CODE`만 세션별 상품선물 코드로 바꿔 운영 smoke:
  - `CF` (정규 상품선물): `output1.futs_prpr=1453.900`, `hts_kor_isnm='미국달러 F 202605'`
  - `CM` (야간 상품선물): `output1.futs_prpr=1447.70`, `hts_kor_isnm='미국달러 F 202605'`
  - `JF` (주식선물 후보): USD futures price 부재

결론: KIS REST 기반 USD futures snapshot 경로는 **`/uapi/domestic-futureoption/v1/quotations/inquire-price` + `tr_id=FHMIF10000000` + 세션별 `FID_COND_MRKT_DIV_CODE=CF/CM`**이다. PR6d-2 fallback orchestration은 이 session-aware mapping을 전제로 진행 가능하다. 휴장/break 중에는 active session이 없으므로 REST snapshot도 호출하지 않는다.

### Tentative baseline (5/4 23:46 ~ 5/6 19:13 KST, 약 43.4h)

> ⚠️ **데이터 caveat**: (1) 2026-05-06은 평소보다 **환율 변동폭이 큰 날** (일중 1469 → 1444.6, -24 KRW 하락 중) — DB inter-row gap이 평소보다 짧게 측정될 수 있음. (2) 2026-05-05 어린이날 휴장 포함 → 평일 baseline으로는 제한적. (3) 아래 DB row gap은 **stale 임계값 근거가 아님** — 가격 변동 없으면 INSERT 0건이라 자연 max gap 길어짐. **stale 임계값의 진짜 baseline은 PR6d-2 raw frame metric에서 수집 예정**. 5/7~5/8 평일 + 5/9~5/10 주말 데이터 추가 후 재검토.

**DB throughput / 변동성:**

- 누적 8195 rows (PR6e 배포 5/6 11:19:57 KST 기준 pre/post 분리)
- 가격 범위 1444.6 ~ 1478.1 (33.5 KRW = 약 2.3% 변동, **변동폭 큰 날**)
- 시간당 INSERT: 1 ~ 1280 (변동성 비례)
- 야간세션(CM): 시간당 ~57~303 (5/4 야간) / 363~1157 (5/5 야간, 변동폭 큰 날 영향)
- 정규세션(CF): 시간당 191~1280 (5/6 09시 hour 1280 peak, 변동성 최대 시간대)

**Inter-row gap 분포 (DB row 기준 — stale 근거 아님):**

| 세션 | gap 표본 | avg | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| CF | 1417 | 11.43s | 2.60s | 54.49s | **156.80s** | **631.40s (10분+)** |
| CM | 6775 | 5.19s | 2.78s | 16.99s | 39.02s | 116.07s |

→ CF p99 156.8s / max 631.4s는 점심시간/저거래량 시간 가격 무변동 자연 현상. raw frame은 호가가 들어와 silence 짧을 가능성 높지만 PR6d-2 metric 없이는 미검증.

**0.1 정규화 (PR6e 효과):**

| Phase | rows | off_tick |
|---|---|---|
| pre-PR6e (5/4 23:47 ~ 5/6 11:19 KST) | 3812 | 2891 (75.8%) |
| post-PR6e (5/6 11:19:58 ~) | 4383 | **0 (100% 정규화)** |

→ PR6e fix 운영 검증 100% 정합. pre-PR6e 8자리 잔차는 30일 retention으로 자연 정리 (6/3 03:31~).

**경고 신호:**

- WARNING / ERROR / status 전이 로그 0건 (Stage 1 + PR6e 운영 안정)
- 세션 boundary 2회 통과 (5/5 06:00 CM→None, 5/6 15:45 CF→None) 모두 정시 + 깔끔 (PR6e carry-over fix 검증됨)

### PR6d-2a 계획 — raw frame metric / status observability

DB row gap은 stale 임계값의 직접 근거가 아니므로, PR6d-2a에서는 WebSocket frame 수신 layer의 silence를 먼저 측정한다. 이 단계는 **관측성만 추가**하며, REST fallback 실행 / Redis 변경 / topic publish / Stage 2 노출 / stale 임계값 변경은 하지 않는다.

**목표:**

- 전체 frame silence가 실제로 60초 임계값에 얼마나 근접하는지 측정
- 체결 frame과 호가 frame을 분리해 야간 저거래량 false stale 위험 검증
- status 전이와 reconnect 패턴을 수치화해 PR6d-2b fallback orchestration 입력으로 사용
- Stage 1 invariant 유지 (`latest:index` KRX 0개, 사용자 노출 0)

**비범위 (PR6d-2b 이후):**

- REST fallback 호출 / cooldown orchestration
- REST fallback dry-run counter (`rest_fallback_call_count`, success/error count)
- Redis latest / topic publish gating
- `KRX_REST_FALLBACK_ENABLED=true` 운영 활성
- stale threshold 변경 (`KRX_STALE_SEC` 조정)

**Metric schema (KisFuturesClient.get_metrics() 후보):**

| Metric | 기준 | 용도 |
|---|---|---|
| `status` | `normal` / `reconnecting` / `stale` | 현재 WebSocket 상태 |
| `contract_code`, `contract_month`, `expires_on` | active contract metadata | 만기/rollover 관찰 |
| `active_session` | `CF` / `CM` / `null` | 세션별 baseline 분리 |
| `started_at`, `connected_at` | KST ISO timestamp | lifecycle 추적 |
| `last_frame_at`, `last_trade_frame_at`, `last_quote_frame_at` | KST ISO timestamp 또는 null | silence 원인 분리 |
| `last_frame_age_sec` | 전체 frame (`H0CFCNT0`, `H0MFCNT0`, `H0CFASP0`, `H0MFASP0`) | 실제 stale trigger baseline |
| `last_trade_frame_age_sec` | 체결 frame (`H0CFCNT0`, `H0MFCNT0`) | 체결/가격 변동성 baseline |
| `last_quote_frame_age_sec` | 호가 frame (`H0CFASP0`, `H0MFASP0`) | WebSocket liveness / 야간 false stale 검증 |
| `frame_count_total`, `trade_frame_count`, `quote_frame_count`, `system_frame_count` | frame type별 counter | frames_per_min / 활동량 계산 |
| `malformed_frame_count`, `unknown_tr_id_count` | parse/unknown guard | provider payload 이상 감지 |
| `stale_transition_count`, `normal_transition_count`, `reconnecting_transition_count` | `_set_status()` 전이 | flap / reconnect 상태 baseline |
| `reconnect_attempt_count` | `_run_session` exception path | 연결 안정성 baseline |

**Gap bucket 후보:**

전체 / 체결 / 호가 각각 `<=1s`, `<=2s`, `<=5s`, `<=10s`, `<=30s`, `<=60s`, `>60s` bucket counter를 둔다. boundary는 PR6d-2a 운영 데이터 수집 후 5/8 baseline에서 조정 가능하다.

**Admin endpoint 후보:**

- `GET /admin/api/krx-status` (기존 `verify_admin` dependency 재사용)
- `scheduler.krx_futures_client.get_metrics()` 반환
- client 없음/비활성 시 `{enabled, started: false, reason}` 형태로 반환

**Summary log 후보:**

- active session 중에만 60초마다 INFO 1줄
- logger 후보: 기존 `app.crawlers.krx_kis` 또는 별도 `exchange_rate.krx.metrics` (구현 중 선택)
- 필드: `session`, `status`, `frames_per_min`, `frame_age`, `trade_age`, `quote_age`, `max_frame_gap`, `stale_transition_count`, `reconnect_attempt_count`
- 휴장/break 중에는 로그 생략 (노이즈 방지)

**Test matrix:**

- 체결/호가 frame counter가 분리 증가
- status transition count가 `_set_status()` 전이 때만 증가
- gap bucket assignment (`5s` gap → `<=10s` bucket 등)
- `get_metrics()` 반환 shape 및 age 계산
- admin endpoint: auth 적용 + client 없음 / client 있음 분기

**운영 검증 기준:**

- 배포 후 KRX WebSocket 재연결 정상 (`connected`, `subscribed`, status 이상 전이 없음)
- `/admin/api/krx-status`에서 active session 중 `last_frame_age_sec`가 낮게 유지
- 60초 summary log가 active session 중에만 출력
- Stage 1 invariant 유지 (`latest:index` KRX 0개)
- PR6d-2b 진입 전 최소 24~48h raw frame metric 축적
- admin endpoint 호출 분기(enabled=false / client=None / client present)는 운영 배포 후 admin 페이지 실호출로 검증 (단위 테스트는 `main.py`의 `firebase_admin` 의존 + lifespan side effect로 skip 처리)
- summary log baseline 해석 caveat: **active session 진입 직후 첫 summary log의 `frames_per_min`은 직전 60초 전체 기준**이라 active 상태였던 시간만의 rate가 아닐 수 있다. 예: 휴장 50초 + active 10초이면 active만의 rate는 더 높음. 첫 summary log는 caveat 또는 무시. 24~48h 누적 데이터에서는 무시 가능 수준 (Codex 외부 검토 2026-05-06)
- **active-session gap metric 해석 caveat (2026-05-07 fix)**: 5/7 운영 baseline에서 `max_*_gap_sec`이 9000초대(세션 break 2.5h)로 잡히는 오염 발견. 이 fix 이전(`5/7 fix commit` 이전) 데이터는 lifetime metric이라 session break가 섞임 → baseline 해석 시 무의미. **fix 이후부터 `max_*_gap_sec` / `gap_buckets` / `last_*_at`는 current active session 기준으로만 의미**. 새 active session subscribe 직후 + 휴장 진입 시 reset됨. lifetime counter(`frame_count_total`, `status_transition_count` 등)는 reset되지 않고 그대로 누적.

### 658ea27 구현 vs PR6d-2a 계획 차이 (2026-05-06, follow-up fix 예정)

PR6d-2a 초안 구현(commit 658ea27, 2026-05-06)은 metric state 골격은 박혔지만 본 ADR 계획과 다음 4가지 차이가 있다. follow-up fix commit으로 보강 예정 (외부 검토 + GO 후 진행, 검증 게이트 7a57665 적용):

| # | ADR 계획 | 658ea27 구현 | follow-up fix 방향 |
|---|---|---|---|
| 1 | `started_at` / `connected_at` KST ISO timestamp | epoch float | epoch + ISO 둘 다 반환 (호환성) |
| 2 | `last_frame_at` / `last_trade_frame_at` / `last_quote_frame_at` ISO timestamp | age만 (`last_*_age_sec`), timestamp 부재 | ISO timestamp 추가, age 필드 유지 |
| 3 | active session 중 60초 summary log | 미구현 | 별도 asyncio task로 `start()` 안에 추가 |
| 4 | admin endpoint 단위 테스트 | skip (`firebase_admin` + lifespan side effect) | skip 유지, KRX_CANARY.md 운영 검증 항목으로 명시 |

### Stage 1에서 이미 확인된 운영 신호

- 2026-05-04 23:46 KST: CM 야간세션 `H0MFCNT0` / `H0MFASP0` subscribe success
- 2026-05-05 06:00 KST: CM session boundary 정상 종료
- 2026-05-05 어린이날 휴장: 세션 없음이 정상
- 2026-05-06 08:30 KST: CF 정규세션 `H0CFCNT0` / `H0CFASP0` subscribe success
- Stage 1 invariant 유지: `latest:index` 내 `latest:source:krx:*` 0개
- PR6e 배포 후 신규 KRX DB row는 0.1 KRW tick으로 정규화됨

### Stage 2 진입 전 필수 조건

- 5/6~5/8 평일 baseline에서 reconnect / stale / DB write warning 패턴 확인
- 2026-05-18 만기일 11:30 전후 WebSocket 끊김/무응답/재구독 패턴 관찰
- REST snapshot helper 구현 및 KIS REST token cache 정책 확정
  - 2026-05-06 smoke 기준 access_token/cache는 검증 완료
  - USD futures quote endpoint/TR은 resolved: `FHMIF10000000` + `/inquire-price` + `FID_COND_MRKT_DIV_CODE=CF/CM` session-aware 분기
- KRX stale 시 `latest:index` 제외 또는 status 표현 정책 확정
- iOS/Android가 unknown `source="krx", asset="usd-krw-futures"`를 안전하게 처리하는지 확인

### 관련 문서

- [KRX_CANARY.md](KRX_CANARY.md): Stage 1/2 runbook, SQL/Redis 검증 명령, 만기일 관찰 시나리오
- [ADR-026](#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성): `latest:index` 기반 Redis-first broadcast hot path
- [ADR-028](#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit): KRX/USDT topic-only 노출 + legacy FX dual-emit 계약
- [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md): `source_rates` 기반 source/asset 모델

### PR6d-2b Stage A amend (2026-05-08): REST fallback decision telemetry

**배경**: 5/8 baseline 24h (CM 1 cycle + CF 1 cycle) 분석 결과, 60s stale 단독 fallback은 CM 종료 전 자연 silence에서 false positive 4건/일 발생. Codex 권고에 따라 multi-day 검증 + Codex incremental 접근(toggle-OFF + metrics-only) 채택.

**Stage A (이번 PR — 코드만, 운영 영향 0)**:

- env (default 모두 false/conservative):
  - `KRX_REST_FALLBACK_ENABLED=false` (PR6d-1 기존)
  - `KRX_REST_FALLBACK_STALE_SEC=120` (NEW — status 전이 임계 60s와 분리, Codex 권고)
  - `KRX_REST_FALLBACK_SESSION_END_GRACE_MIN=40` (NEW — 5/8 baseline -32min cluster cover)
  - `KRX_REST_COOLDOWN_SEC=30` (PR6d-1 기존)

- evaluation 호출 시점 (Codex BLOCKING 1 fix):
  - `_set_status` normal → stale 전이 직후 1회 (짧은 stale 6~15s 누락 차단)
  - summary log loop stale 지속 중 60s cycle 1회 (긴 stale 추가 평가)

- 검사 순서 (Codex BLOCKING 2 fix — env=false telemetry 의미 보존):
  1. session_end_grace (시간 기반 차단 우선)
  2. frame_age < threshold → below_threshold
  3. cooldown 미경과 → cooldown
  4. NOT enabled → disabled (마지막)
  5. → eligible
  의도: env=false라도 grace 구간 stale은 grace 분류로 잡혀야 telemetry 의미 (5/8 stale 4건 검증).

- decision telemetry counter (8개):
  - `evaluated` / `eligible`
  - `suppressed_disabled` / `suppressed_below_threshold` / `suppressed_session_end_grace` / `suppressed_cooldown`
  - `rest_success` / `rest_error` (Stage B+ 활성화 시만)

- eligibility 결합 조건 (Codex 권고 — stale 단독 금지):
  ```
  ENABLED=true
  AND frame_age >= STALE_SEC
  AND NOT in_session_end_grace
  AND cooldown_elapsed
  → eligible (Stage B+에서 실제 REST 호출)
  ```

- env 검증: `KRX_REST_FALLBACK_STALE_SEC < KRX_STALE_SEC`이면 ValueError (의미 모순 차단)

**Stage B 코드 골격 (2026-05-09 추가, env=false 유지)**:

env=false default 유지 — 운영 영향 0. 코드 경로만 준비해 multi-day baseline 후 env 변경만으로 즉시 활성화 가능.

구현:
- `KisFuturesClient.__init__`에 `access_token_manager` 인자 추가 (Optional, Stage A 호환)
- `_invoke_rest_fallback` async 메서드: `fetch_kis_futures_quote` 호출 + rest_success/rest_error counter
- `_evaluate_rest_fallback` eligible 분기에 task 생성 (env=true일 때만)
- `_fallback_tasks: set` lifecycle 추적 + `add_done_callback(discard)` 자동 정리 (Codex 1 권고)
- `_last_fallback_at` 갱신을 task 생성 직전에 (Codex 2 권고 — REST hang 시 중복 task 차단)
- `stop()`에서 잔여 fallback task cancel + await
- `_bootstrap_krx_futures_client`에 `KisAccessTokenManager` wiring

REST 결과는 Stage B에서는 log/counter만. broadcast/DB/latest 미반영 (Stage C).

**Stage B 활성화 (5/12+ multi-day baseline 후 별도 GO)**:
- `KRX_REST_FALLBACK_ENABLED=true` 활성화
- 실제 `fetch_kis_futures_quote` 호출
- counter `rest_success` / `rest_error` 누적

**Stage C (5/18 만기 통과 + multi-day eligible 빈도 확인 후)**:
- 임계값 튜닝 (env 변경 — 90s/150s 등)
- 필요 시 reconnect 동반 별도 분기 추가 (현재 v1은 미포함)

**테스트**: `tests/test_krx_fallback_eligibility.py` 14 케이스 (helper grace 7 + evaluate 7).

**5/8 baseline 데이터로 Stage A 차단 검증**:
- stale 4건 모두 (-32min/-26min/-4min/-39s) → `suppressed_session_end_grace` 차단
- 나머지 시간 stale 0건 → evaluation 자체 발생 X

---

### PR6c-2d-1 amend (2026-05-07): 자동 rollover 정책 추가

**배경**: 5/18 만기 직전, 자동 rollover 미구현 시 만기 종목 client가 silence 또는 잘못된 종목 운영 위험. 5/18 관찰을 정책 결정용 → 검증용으로 격상하기 위한 사전 구현.

**결정**:

- **User-facing swap point: 만기일 07:00 KST**.
  - 거래소 만기 시각(11:30)이 아니라 사용자 대표 월물 전환 시점.
  - 만기 직전 영업일 야간장(=만기일 새벽 06:00) 종료 후 swap.
  - 06:00 yatime boundary와 분리 (07:00은 휴장 한가운데).
  - 거래 관행 준수 (전문 트레이더는 만기 전 다음 월물로 이동).

- **5분 reconcile 임시 안전모드**: APScheduler IntervalTrigger 5분마다 `_reconcile_krx_futures_contract()` 실행.
  - 동작: resolve → 현재 client contract 비교 → 다르면 shutdown + start
  - 안전장치: 만기 차이 > 45일 점프 의심 보류 / resolve 실패 격리 / None 처리 / 같은 contract no-op
  - **임시성**: 5/18 통과 후 hybrid (06:01 daily + session boundary)로 축소 검토 (PR6c-2d-5 후보)

**사전 검증 (2026-05-07 retrospective)**:

- A75604 (만기 17일 후 + KIS master 제거 상태) REST 조회: rt_cd=0이지만 `futs_prpr/prpr` 부재, `bstp_*` (지수형) keys 응답.
- helper의 `futs_prpr/prpr 부재` 검사가 None 반환으로 만기/제거 종목 자동 차단 — 안전 규칙 작동 확인.
- A75606 (다음 6월물, 만기 5주+ 전): 이미 정상 quote 응답 + 가격 1448.00 → 선제 swap 안전.
- A75605 vs A75606 베이시스 0.04% (1448.60 vs 1448.00, 만기 1주일 전 자연 수렴).
- 단, "만기 종목은 항상 지수형 응답으로 바뀐다"는 일반 규칙으로 단정 X — 1건 단일 시점 관측, master 제거 시점과 만기 시점 분리 미관측. 안전 규칙은 "rt_cd=0이어도 price field 검증 필수" 형태로 문서화.

**범위 외 (별도 PR)**:

- mmsc_cls_code 파서 + 5/18 ad-hoc 관찰 스크립트 → PR6c-2d-3
- admin status에 rollover metric → PR6c-2d-2
- 가격/contract metadata mismatch 처리 (07:00~08:30 휴장 구간 latest 가격은 옛 월물) → broadcast layer (Phase Z-2)
- Hybrid scheduler (5분 cron 축소) → PR6c-2d-5

**테스트**: `tests/test_kis_master.py::TestSelectActiveContract` (15 케이스, 07:00 swap inclusive 분기 검증) + `tests/test_krx_scheduler.py::TestReconcileKrxFuturesContract` (7 케이스, KRX_FUTURES_ENABLED gate / resolve 실패 / None / no-op / bootstrap / 정상 rollover / 점프 보호) + `tests/test_kis_futures.py::TestActiveSession` (7 추가 케이스, contract-aware session 분기 검증).

**Codex 외부 검토 amend (2026-05-07)**:

1. **BLOCKING — contract-aware `get_active_session`**: 기존 함수가 `is_expiry_day(today)` 캘린더 기반으로 만기일 11:30 종료를 적용. PR6c-2d-1이 07:00에 next month로 swap한 후에도 같은 함수가 호출되어 11:30~15:45 동안 A75606이 disconnect되는 버그.
   - 수정: `get_active_session(now, contract_expiry_date=None)` signature 변경.
   - `contract_expiry_date == today`만 11:30 종료 적용, 다르면(next month) 정상 15:45.
   - `contract_expiry_date=None`은 legacy 호환 (기존 캘린더 동작 유지).
   - 운영 호출 4곳 (krx_kis.py 426/737/811/879) 모두 `self._contract.expiry_date` 또는 `contract.expiry_date` 전달.

2. **Reliability — reconcile 시 두 번째 resolve 회피**: 기존 reconcile은 shutdown 후 `start_krx_futures_client()` 호출 → `_bootstrap_krx_futures_client()`가 다시 resolve. 두 번째 resolve 실패 시 5분간 KRX outage.
   - 수정: `_bootstrap_krx_futures_client(resolved_override=None)` signature 변경.
   - reconcile에서 `_bootstrap_krx_futures_client(resolved_override=resolved)` 직접 호출.
   - 두 번째 resolve 회피 + race 차단 + 동일 contract 보장.

**Amendment 2026-08-17 — 만기일 휴장 보정 (위 1번의 전제 2건 supersede)**:

만기를 "셋째 월요일"로 계산하던 것이 틀렸다. **그날이 휴장이면 만기는 직전
영업일로 앞당겨진다.** 2026-08-17(월)이 광복절 대체공휴일이라 8월물 만기는
**2026-08-14(금)**였는데, 구 코드는 8/17을 만기로 알고 있었다.

- 단일 진실 소스 신설: `app/calendars/krx_calendar.py`
  (`usdf_expiry_date` — 셋째 월요일에서 영업일까지 walk-back,
  `is_krx_regular_business_day` / `is_krx_night_session_open`).
  `holidays.SouthKorea(PUBLIC∪BANK, observed=True)` 기반이라 연도 하드코딩이 없다.
- **위 1번의 `contract_expiry_date=None` legacy 호환은 폐기**됐다 —
  `get_active_session(now, contract_expiry_date)`에서 **필수 인자**다.
  호출자가 빠뜨리면 조용히 캘린더 기본값으로 도는 대신 TypeError가 난다.
- `is_expiry_day` / `KRX_2026_USDF_EXPIRY_DAYS`(연도 하드코딩)는 **삭제**됐다.
  위 1번 서술의 그 심볼은 당시 코드 기준 기록이다.
- scheduler의 rollover 점프 보호가 `expiry_diff > 45일` 휴리스틱에서
  `_is_next_contract_month` 월물 비교로 바뀌었다 (만기 간격과의 결합 제거).
- `2026-02-13` 야간은 설 연휴 전 **일회성** 휴장이라(KIND acptno=20260206002546)
  캘린더로 유도할 수 없어 override 표에 근거와 함께 고정했다 — 연례 규칙이 아니다.

**데이터 영향**: segment 경계가 이동해 교정 전 적재분 중 주중 2일자가 어긋난다
(`2026-02-13` contract 오귀속 / `2026-08-14` 행 부재).
`scripts/repair_krx_daily_rows.py`가 그 2일자만 allowlist로 교정한다(별도 GO).
graph 매핑 규칙 자체는 불변 — [GRAPH_API_V2_CONTRACT.md §7-new](GRAPH_API_V2_CONTRACT.md) Amendment 참조.

**Follow-up: KRX close snapshot 1차 PR (2026-05-15, `c0855ff`)**:

본 ADR-027 영역(KRX REST/stale 정책) 안에서 **boundary-based close snapshot**을 stale fallback과 **별도 trigger**로 분리해 1차 PR로 land. 두 path는 같은 REST endpoint(`KIS_REST_QUOTE_TR_ID`)와 helper(`fetch_kis_futures_quote`)를 공유하지만 **trigger 조건 / 책임 controller가 다름**.

| 항목 | stale fallback (기존, ADR-027 본문) | close snapshot (1차 PR, c0855ff) |
|---|---|---|
| Trigger | 장중 60s+ silence (`status == stale`) | session boundary 도달 직후 (CF 15:45 / CM 06:00) |
| Controller | `KrxRestFallbackController` (stale gating + cooldown) | `KrxCloseSnapshotController` (boundary capture + retry sequence) |
| 호출 빈도 | 장중 stale 발생 시 cooldown 내 1회 | 일 최대 6회 (CF 3 + CM 3, 첫 성공 시 short circuit) |
| 목적 | 장중 장애 보정 | 세션 종료 확정 보정 (단일가 입찰 마감 후 종가) |
| Primary target | Redis latest + DB row (장중 데이터 연속성) | **Redis latest** (DB는 best-effort history — `KRX_CLOSE_SNAPSHOT_PLAN §4.4` ordering 한계 명시) |
| Scope | Stage 2 진입 전 활성화 정책 | 1차 PR — default `KRX_FUTURES_ENABLED=true` Stage 1에서 즉시 활성 |

**1차 PR scope 안 처리**: CF 15:45 / CM 06:00 boundary snapshot. **scope 밖**: 만기일 11:30 expiring CF (07:00 swap 정책으로 자연 처리), schema 변경 (DB ordering 일관성), Stage 2 broadcast 노출 결정 (ADR-028 topic 채널 기준).

**2차 PR 후속**: WS grace drain (CF 15:45:59 / CM 06:00:59까지 listen + grace tick timestamp boundary normalize). 1차 PR 7일 운영 측정 결과 baseline 분석 후 진입 결정.

**2차 작업 완료 (2026-05-17, Stage 1-5 / `68b8702..c2fb796`)**:

WS-first close finalizer + REST fallback 1회 + Redis TTL captured flag race 방지로 land. EC2 deploy 2026-05-17 17:07 KST. F1 (DB row 중복) / F2 (flag race) / F3 (test hang) 모두 구조적 해결. `KRX_CLOSE_FINALIZER_ENABLED` env default true, false 시 1차 PR (c0855ff) 동작 rollback. 첫 실측 2026-05-18 (월) 15:45 KST CF close / 2026-05-19 (화) 06:00 KST CM close. 7일 telemetry (`close_grace_saved` / `close_rest_fallback_used` / 정책 PR `6a43785` 이후 `rest_write_blocked` 추가) 분석 후 3차 PR scope (DB unique constraint / 종가 read API / open auction / REST close fallback **코드 완전 제거** vs 검증 가능성 재검토 [2026-05-25 follow-up 이후 default off] / Stage E) 결정. 상세 + Stage 1-5 commits / 검증 / rollback은 plan 문서 §0 참조.

상세: [KRX_CLOSE_SNAPSHOT_PLAN.md](KRX_CLOSE_SNAPSHOT_PLAN.md).

### 2026-05-18~19 만기 실측 반영 (CF/CM 통과, master batch 가설 b 확정)

> 📅 **실측일**: 2026-05-18 (월) ~ 2026-05-19 (화) — 5/18 CF close finalizer + 5/19 CM close finalizer + master/REST/WS observer + master batch 익일 갱신 확인
> 🏷️ **상태**: CF/CM close finalizer 첫 실측 통과 + master batch 익일 갱신 확정 (가설 b) — 핵심 운영 정책 확정 완료. 7일 telemetry로 case A/B/C 분포 측정 진행 중.

#### 결정 후보 → 확정 변화

본 ADR의 "결정 후보 (Stage 1 데이터로 확정 예정)" 표 중 5/18 실측으로 확정된 항목:

| 항목 | 5/18 실측 결과 → 확정 |
|---|---|
| **active contract 판단 정책** | **자체 calendar/expiry 기반 (PR6c-2d-1)이 옳음 — 확정**. KIS master는 만기 당일 17:05까지 옛 월물 (A75605) `mmsc_cls_code=1` 미변경 + **익일 5/19 07:00~08:00 KST window에 batch 갱신** (5/19 07:00 snapshot까지 A75605 `cls=1` 잔존, 08:00부터 A75605 제거 + A75606 `cls=1` 승급 + A75607 `cls=2` 신규 부각). KIS REST는 만기 후에도 옛 월물 종가 stale 반환. master/REST 응답으로 active 판정하면 운영 위험. |
| **REST stale 응답 위험** | **확정** — A75605 만기 직후 관찰 구간(11:31~12:10) 동안 `rt_cd=0, futs_prpr=1505.800, acml_vol=7846` 그대로 응답 (12:10 이후 REST 미관찰). 단순 `rt_cd == "0"` 분기로 stale 인지 불가. PR6d-2b REST fallback 정책에서 active contract 체크 + timestamp/`acml_vol` 비교 필요. |
| **만기일 rollover (PR6c-2d-1)** | **5/18 실측 통과** — 07:01:47 KST 발화, A75605 → A75606 swap, 08:30 정규세션 정상 진입. PR6c-2d-5 (hybrid scheduler 축소) 검토 가능. |
| **CF/CM close finalizer (c2fb796 Stage 5)** | **양 session 첫 실측 통과 — WS-first path 성공, REST fallback skip**. CF (2026-05-18 15:45:00 KST) rate=1496.5 + CM (2026-05-19 06:00:00 KST) rate=1490.0. log: `[krx_close_window] close saved` + `[krx_close_snapshot] WS captured at entry → REST skip`. 양 session 동일 boundary timestamp semantics (WS path: KIS payload `market_time` 우선 → boundary; REST path: `boundary_at_kst` 고정 → 동일 sink 결과). F1/F2/F3 race 위험은 본 두 케이스 모두에서 노출되지 않음 (양 session 단발 성공으로 일반 해소 단정 X — 7일 telemetry 누적 후 확정). |

#### 만기월 WS 거동 (운영 정책 input)

- **운영 중 만기월 WS subscribe**: same appkey 충돌로 `OPSP8996 ALREADY IN USE`. → 운영 client는 active contract만 subscribe 유지가 옳음 (PR6c-2d-1 swap 후 옛 월물 client 종료 정책 정확).
- **Post-close 만기월 WS subscribe** (15:48~16:02 별도 observer): `SUBSCRIBE SUCCESS`이지만 frame 0건, 14분 후 KIS idle close. → 이번 케이스에서는 운영 데이터 source로 가치 낮음 (운영 client는 active contract만 subscribe).
- **다음 만기 (6/18) 별도 observer는 불필요** — 운영 로그/DB row 사후 확인으로 충분. 옛 월물 실시간 관찰은 별도 appkey 또는 단일 connection multi-contract subscribe 구조가 구현된 경우에만 재검토.

#### KRX 단일가 메커니즘 (운영 close finalizer 설계 input)

5/18~5/19 capture된 네 시점 모두 동일 10분 호가 접수 → 단일가 체결 패턴:

- **만기 CF 종가** (A75605, 5/18 11:30:0X): 1505.800원 × 30계약 (`acml_vol` 7816→7846)
- **정규 CF 종가** (A75606, 5/18 15:45:01): 1496.5원 (운영 close finalizer capture)
- **야간 CM 시가** (A75606, 5/18 18:00:01): 1494.7원
- **야간 CM 종가** (A75606, 5/19 06:00:00): 1490.0원 (운영 close finalizer capture, WS-first path)

→ CF/CM 양 session 모두 동일 단일가 패턴 실측 확인 완료. close finalizer 양 session 첫 실측 통과.

#### Pending — 7일 telemetry 후 결정

- **5/19~5/26 close finalizer 7일 telemetry** — case A (WS-first 성공) / case B (WS-first 실패, REST diagnostic + `rest_write_blocked` write 차단 — 2026-05-25 follow-up 이후) / case C (둘 다 실패) 분포 측정. 현재 CF 1건 + CM 1건 = case A 2건. 결과로 3차 PR scope 결정 (DB unique constraint / 종가 read API / open auction / REST close fallback **코드 완전 제거** vs 검증 가능성 재검토 / Stage E).
- **PR6c-2d-5 hybrid scheduler 축소** — 5/18 rollover 통과 후 검토 가능 (현재 5분 reconcile cron, hybrid 06:01 + boundary로 축소 후보).
- **PR6d-2b REST fallback 정책 설계** — active contract check + session boundary grace + stale/`acml_vol`/timestamp 비교 + 만기월 REST 응답 배제.
- **master 전체 dict diff** (선택, `mmsc_cls_code` 외 `name`, `contract_month`, `last_tr_date` 등 추가 sample 수집 가치 시점에 확인).

상세 관찰 데이터 + 운영 교훈: [KRX_CANARY.md "2026-05-18~19 만기 첫 실측 결과"](KRX_CANARY.md) 참조.

### Follow-up 결정 — KIS REST close write source 격리 (2026-05-25)

> 📅 **작성일**: 2026-05-25 (사고 당일 4단계 대응 완료)
> 🏷️ **상태**: 운영 land 완료 (`170380f` hotfix + `6a43785` 정책 PR)

#### 계기 — 2026-05-25 휴장일 사고

부처님오신날(5/24 일요일) 대체공휴일로 KRX 휴장이었으나 `KRX_2026_KNOWN_HOLIDAYS`에 5/25 미등록 → `is_close_snapshot_eligible("CF", 2026-05-25)=True` → close snapshot REST fallback 발동 → KIS REST가 5/22 stale 종가(rate=1516.8)를 `rt_cd=0` 정상 응답으로 반환 → DB row id=429785 + Redis latest를 5/25 15:45 KST timestamp로 잘못 기록 → 단말 노출 사고.

#### 근본 진단 — REST stale의 본질 (ADR-027 중심 주제 강화)

본 ADR의 원칙 1 ("KIS REST는 stale 응답을 정상 형식으로 반환 가능") 추가 데이터:

- **5/19 만기 실측**: A75605 만기 후에도 `rt_cd=0, futs_prpr=1505.800, acml_vol=7846` 고정 stale 반환. 응답 형식 자체는 정상 — 단순 `rt_cd == "0"` 분기로 stale 인지 불가 (§"2026-05-18~19 만기 첫 실측 결과" 결론 3)
- **5/25 휴장 사고**: KRX 미운영이지만 REST가 5/22 종가 stale 응답을 정상 형식으로 반환
- REST 응답에 거래일 / 체결시각 / 거래량 검증 가능한 명확한 필드 부재 — 자동 코드로 stale 인지 불가

**핵심 결론**: REST 응답만으로 "오늘 종가"임을 증명할 수 없음 → **write source로 부적합**.

#### 결정 — close REST write source 격리

`KrxCloseSnapshotController`의 REST 기반 DB/Redis write를 **default off**로 격하 (env `KRX_CLOSE_REST_WRITE_ENABLED=false`).

**핵심 anchor 5문장**:

> ⚠️ **Amendment 2026-06-10 — gate-checked write 재설계 (#4)**: (1)(2)(3) 부분 supersede.
> 계기: 6/9 KRX WS silent-stall(15:04, reconnect 0 — 별도 fix `87b51c3`)로 종가 frame
> 미수신 → REST가 정확한 종가(1514.7, 공식 종가 일치 확인)를 확보했으나 무조건 차단에
> 막혀 **일봉 누락** — 5/25(차단=정답)와 6/9(write=정답)는 동일 정책의 양방향 실패.
> 재설계: `_evaluate_close_write_gates` gate chain (calendar / contract identity /
> **session evidence** — 우리 WS last tick recency 3h, env
> `KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS`. 미등록 휴장까지 자기 데이터로 차단 +
> 구 last=None sanity-skip 구멍 폐쇄) + 기존 sanity ±2%. flag=false = **shadow 평가 +
> 차단** (gate verdict telemetry — finalizer 경로 REST는 WS-miss 날에만 실행되므로 샘플은
> 그런 날에만 쌓임이 정상) / flag=true = **gate-checked write** + 성공 시 CF daily append
> tail (`metadata_json.origin=rest_close_write`, WS 경로 hook mirror — H/L은 rollup ∪
> {close}, gap-only 미교정 + delete 후 openapi 재실행 escape hatch). 평가 순서는 sanity가
> gate보다 먼저 (기존 분기 보존 — sanity abort=False retry / gate reject=True terminal
> 비대칭). captured flag는 SET 안 함 (WS captured 의미 보존). default false 유지 — 배포
> 무변화, env 활성화는 **2026-06-15 완료** (A75606→A75607 rollover 후 prod `KRX_CLOSE_REST_WRITE_ENABLED=true`, 16:12 KST). 상세:
> [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7.8](KRX_CLOSE_SNAPSHOT_PLAN.md).

1. ~~KIS REST close snapshot은 더 이상 authoritative write source가 아니다.~~ (supersede — gate 전부 통과 시 gated authoritative fallback, WS-first 불변)
2. ~~REST 호출은 diagnostic으로 유지될 수 있지만 DB/Redis write는 default off다.~~ (supersede — default false 유지, true 의미 = gate-checked write)
3. ~~Case B는 REST 성공 write가 아니라 `rest_write_blocked` diagnostic signal로 해석한다.~~ (supersede — flag=true + gate 통과 시 실제 write)
4. `KrxCloseWindowWriter`의 WS close frame write는 신뢰 경로로 유지한다.
5. Stage E는 close finalizer 정책과 직교한다.

#### 차단 범위 — `KRX_CLOSE_FINALIZER_ENABLED` 값과 무관

`KrxCloseSnapshotController._sync_write` 단일 분기로 두 경로 모두 차단:

- **`KRX_CLOSE_FINALIZER_ENABLED=true`** (default) 경로: 2차 PR `c2fb796` REST fallback 1회 — write 차단 + retry short-circuit
- **`KRX_CLOSE_FINALIZER_ENABLED=false`** rollback 경로: 1차 PR `c0855ff` REST retry 3회 — 첫 attempt에서 short-circuit (같은 stale 값 3회 확인 무의미)

`KrxCloseWindowWriter` (WS close frame 기반, 신뢰 source)는 변경 없음 — 본 결정 직교 영역.

#### Case B 의미 변경

- **이전**: KIS WS 미송신 → REST fallback 성공 → DB/Redis write → Redis latest REST 갱신
- **이후**: KIS WS 미송신 → REST diagnostic 호출 → `close_rest_fallback_used` +1 + `rest_write_blocked` +1 → DB/Redis write 없음 → Redis latest는 직전 정상 영업일 종가 유지

상세 telemetry 해석: [KRX_CLOSE_SNAPSHOT_PLAN.md §5.5](KRX_CLOSE_SNAPSHOT_PLAN.md) 참조.

#### Rollback

~~env `KRX_CLOSE_REST_WRITE_ENABLED=true` + `docker compose up -d --force-recreate fastapi`로 기존 1차/2차 PR write 동작 복원 가능. 단 KIS REST stale 위험 동반 — 회귀 사고 가능성.~~
**2026-06-10 amendment 이후**: flag=true = gate-checked write (5/25형 stale은 gate가 차단 — 구 회귀 위험 없음). 완전 차단 복귀는 flag=false 유지.

#### 다음 단계

- 5/19~5/26 7일 telemetry 분석 시 신규 `rest_write_blocked` counter 결합 분석 → case B 분포 + KIS REST stale 재발 빈도 → REST close fallback 코드 완전 제거 vs 검증 가능성 재검토 결정.
- Stage E (KRX Redis tick-level 전환, [KRX_FANOUT_REFACTOR_PLAN.md §5.2 E](KRX_FANOUT_REFACTOR_PLAN.md))는 본 결정과 직교 작업 — 별도 진입.

상세 사고 기록 + 운영 보정 명령: [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7](KRX_CLOSE_SNAPSHOT_PLAN.md) / [KRX_CANARY.md §"2026-05-25 휴장일 사고 + 대응"](KRX_CANARY.md) 참조.

### Stage C guard 설계 (2026-06-07 amendment)

> **현황 재정리**: Stage A/B(helper `fetch_kis_futures_quote` + controller `evaluate`/`_invoke_rest_fallback` — stale gating·cooldown·session-end grace·REST 호출)는 구현·배포 완료. `KRX_REST_FALLBACK_ENABLED=false` 운영. **Stage B는 REST 호출 후 log/counter만**([krx_kis.py:754](app/crawlers/krx_kis.py), 반영 경로 0). Stage C = REST 결과를 topic/Redis/DB에 **반영**. 단 2026-05-25 close snapshot 사고([config.py:201-210](app/config.py) — 휴장일 KIS REST가 5/22 stale 종가를 `rt_cd=0`으로 반환 → DB row id=429785 + Redis 기록 → 단말 노출)가 입증하듯 **REST 응답만으로 freshness 증명 불가** → 반영 전 다층 guard 필수.

**설계 원칙**: REST를 일괄 차단(close snapshot 방식)하지 않는다 — close snapshot은 15:45 마감-순간이라 항상 경계여서 차단이 정답이었으나, **general fallback은 장중 일시 WS 단절** 한정이라 **장중 + 다층 guard 통과 시에만 fill**, 하나라도 실패 시 **exclude/telemetry-only**(default-safe).

**4-gate (앞 3개가 robust 방어 본체, 4번이 보강):**

| # | gate | 통과 조건 | 실패 시 | building block (실재 확인) |
|---|------|-----------|---------|---------------------------|
| 1 | **calendar** | 오늘 KRX 거래일 | exclude | `is_krx_business_day` — **kr_holidays 기반 robust**(연도 무관 + 대체공휴일·근로자의날·제헌절 재지정 + KRX 연말 폐장). 구 2-date 하드코딩 업그레이드 완료 (2026-06-07) |
| 2 | **session** | CF/CM 장중 + 종료 grace 아님 | exclude | `get_active_session` + `is_in_session_end_grace` ✅ |
| 3 | **active contract** | REST 응답 월물 == 현재 active contract 월물 (+ 만기 미경과) | exclude | `output1.hts_kor_isnm`(월물) + **`futs_last_tr_date`(만기일, 2026-06-07 실측 확인)** 둘 다 보강 신호 → 만기 후/rollover stale 월물 차단(5/18 사고) ✅ |
| 4 | **freshness** (secondary, telemetry) | **BAS_DD 부재 확정**(2026-06-07 inquire-price 실측 — output1 31필드 중 거래일 필드 없음) → acml_vol·가격 vs 최근 WS state(stateful) **telemetry 수준**(hard reject 아님 — gate 1-2가 장중을 이미 확정) | telemetry-only | `output1.futs_prpr`+`acml_vol` ✅ / BAS_DD ❌ (확인 완료) |

**gate 1-3가 알려진 두 사고(5/25 휴장 · 5/18 만기 stale 월물)를 직접 차단** = robust 방어 본체. **gate 4(freshness)는 secondary** — acml_vol stateful 비교는 "DB row gap ≠ raw frame gap"(§원칙 4) 진단대로 장중 저유동성 정체를 stale로 오탐 → 단독 차단 근거 부적합(gate 1-2가 "장중"을 이미 확정). BAS_DD 있으면 clean, 없으면 telemetry 수준.

**output policy**: 4 gate 통과 → fill 후보; 하나라도 실패 → `rest_guard_rejected` verdict + telemetry only(반영 0). 이번 세션 `daily_append_verdict` 패턴과 일관.

**의존/순서**: Stage C 실 노출 채널은 ADR-028 **topic-only**(legacy `rates` 아님) + KRX Stage 2. guard+reflect 코드는 작성 가능하나 실 노출은 topic protocol(Phase Z-2) 선행. 권장 구현 순서: (a) inquire-price read-only 1회로 BAS_DD/acml_vol 일관성 확인 → (b) 4-gate guard + `rest_guard_rejected` telemetry 구현(controller 결과 처리부) → (c) topic 반영은 Phase Z-2 정합.

**구현 전 확정 (open)**: ✅ ~~BAS_DD 필드 실재~~ → **2026-06-07 inquire-price read-only 실측으로 BAS_DD/거래일 필드 부재 확정** (output1 31필드 — futs_prpr/acml_vol/hts_kor_isnm/futs_last_tr_date 등, 데이터 거래일 없음. 휴장일 6/7에 거래일 필드 없이 1538.9를 `rt_cd=0`으로 반환 → **REST payload만으로 기준 거래일 판별 불가** 실증. 값 1538.9는 6/5 CF close finalizer 값과 일치 = 직전 거래일 stale 값 추정[단, 응답 자체가 날짜를 증명하진 않음]). freshness=acml_vol telemetry로 닫음. **남은 open**: ✅ ~~calendar 업그레이드~~ → **완료 (2026-06-07)** — `is_krx_business_day`를 kr_holidays + KRX 연말 폐장 wrapper로 교체(연도 무관 동적, 구 2-date 제거). active contract 비교 형식도 guard 첫 PR에서 `_extract_contract_month` exact + `futs_last_tr_date` fail-closed로 적용 완료.

---

## ADR-028: Topic-only Tether/KRX + legacy FX dual-emit

> 📅 **작성일**: 2026-05-06
> 🏷️ **상태**: 합의 (서비스 출시 계약 — 코드 분리는 Phase Z-2 영역)

### 맥락

[REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md) v0.8(2026-05-04 합의)에서 "USDT 거래소 / KRX 미국달러선물은 topic-only" 원칙이 정의됐지만, 그 이전 작업(USDT Phase 1, 2026-04-23)에서 USDT 5거래소를 legacy `/api/rates` + WebSocket `rates` 배열에 통합하는 어댑터 경로(Decision E)가 구현되어 운영 중이다.

운영 사실:
- iOS(2026-01-21) / Android(2026-03-13) 출시 앱에는 **테더 탭 없음** — 즉 USDT가 legacy `rates`에 흘러도 사용자 영향 0
- 백엔드 USDT 5거래소 + KRX 미국달러선물 수집은 정상 진행 중
- WebSocket subscribe 인프라는 미구현 — 모든 클라이언트가 같은 broadcast 받음
- PR6 KRX는 `KRX_BROADCAST_INCLUDE` 토글이 legacy `rates` 통합 의미로 작성되어 있어 [ADR-027](#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안)과 모순 (Stage 2 broadcast 노출이 legacy 통합 가정)
- USDT_PHASE1 시리즈 문서(TAB_PROPOSAL / DESIGN / CLIENT_GUIDE)는 모두 legacy 통합 모델 가정

이 모순을 해소하지 않고 Stage 2 / 테더 탭 출시로 진행하면 (1) legacy `rates` 대역폭이 USDT/KRX까지 포함해 비대해지고, (2) topic 기반 selective subscription의 "필요한 것만 받음" 목표가 폐기되며, (3) 새 클라이언트가 legacy 어댑터에 묶여 향후 V2 protocol 도입 시 마이그레이션 부담이 커진다.

### 결정

**서비스 출시 계약**:

1. **legacy `rates` 채널 데이터 범위**: USD/JPY/EUR + Investing/은행 9개 한정. **USDT 거래소 / KRX 미국달러선물은 미포함.**
2. **새 topic 채널**: `fx:*` (외환), 테더 탭 토픽 (잠정 `usdt:krw` multi-source), `krx:*` (KRX 미국달러선물), `dxy`, `graph:*`, `news` 등.
3. **dual-emit 범위**:
   - 환율 탭 데이터(USD/JPY/EUR + 은행 9개): legacy `rates` + topic delta **양쪽 발사**
   - 테더 탭 데이터(USDT 5거래소): topic delta만 발사
   - KRX 데이터(`source="krx"`, `asset="usd-krw-futures"`): topic delta만 발사
4. **legacy 제거 기준**: iOS/Android 양쪽 활성 구버전 < 1% 동시 도달 + 6개월 경과 시점에 legacy `rates` 전체 폐기 후보 ([REALTIME_ARCHITECTURE_PLAN.md §11](REALTIME_ARCHITECTURE_PLAN.md) 정책 유지).

**현재 구현 vs 계약**:

| 영역 | 현재 구현 (test-era compatibility) | 서비스 계약 (목표) |
| --- | --- | --- |
| `/api/rates` | USDT 어댑터 변환으로 `bank+currency` 응답에 USDT 포함 | USD/JPY/EUR + 은행 9개만. USDT는 별도 endpoint(미정) 또는 topic |
| `/api/rates/usdt-krw` | USDT를 `bank+currency`로 응답 | debug/compat 유지 vs 제거 별도 결정 (Phase Z-2) |
| WebSocket `rates` 배열 | USDT + (Stage 2 시) KRX 포함 가능 | USD/JPY/EUR + 은행 9개만 |
| WebSocket subscribe | 미구현 (전체 broadcast) | hello + subscribe + topic delta dispatch |
| KRX `KRX_BROADCAST_INCLUDE` | legacy `rates` 통합 토글 (현재 코드 의미) | topic 발사 트리거로 재해석 (코드 분리는 Phase Z-2) |

**Follow-up: topic-only 장기 비전 + Phase B.2 direct 활성화 (2026-05-16 합의)**:

2026-05-16에 Phase B.2 `direct_coalesced`를 운영 활성화하고 iOS dev 단말에서 `usdt:krw` topic end-to-end 실시간 수신을 확인했다. 이 결과를 바탕으로 사용자/Codex/Claude가 다음 장기 방향에 합의했다.

- 신규 단말 앱은 legacy broadcast cycle이 아니라 **topic 구독 모델만** 사용한다.
- broadcast cycle은 구버전 단말 호환용으로 유지하고, 활성 구버전 비율/기간 조건을 충족한 뒤 장기적으로 deprecate한다.
- `main.py broadcast_rates_once is_changed` 기반 tether legacy hook은 임시 경로다. 장기적으로는 테더 탭에 표시되는 모든 자산이 Redis latest write-through 성공 지점 기반 trigger를 갖춘 뒤 완전 제거한다.
- REST polling은 단기 safety net으로 유지한다. 장기적으로는 거래소별 WebSocket primary + REST fallback 전용으로 격하한다.
- 은행/Investing처럼 mirror cycle 기반 source는 단순 DB insert 직후 trigger가 아니라 Redis latest 정합성을 보장하는 위치를 별도로 설계한다.

### 단계 (Phase Z)

**Phase Z-1 — 문서 정합 (코드 변경 0, 본 ADR 작성 시점)**:

- REALTIME_ARCHITECTURE_PLAN.md를 단일 진리 source로 명시 + "현재 구현 vs 목표 계약" 분리
- USDT_PHASE1 시리즈 (TAB_PROPOSAL / DESIGN / CLIENT_GUIDE)에 superseded notice
- CLAUDE.md USDT Phase 1 / KRX 섹션을 운영 현실로 정정
- ADR-027의 "Stage 2 broadcast 노출" 표현을 "topic 노출"로 재해석
- KRX_CANARY.md Stage 2 조건에 topic protocol 의존성 추가
- CHANGELOG.md Changed 항목 추가

**Phase Z-2 — 코드 분리 + 문서 정리 (별도 PR, baseline + 5/18 만기 관찰 후 시작)**:

코드 작업:

- `app/main.py` `build_rates_payload`에서 `get_all_rates_flat()` 결과를 currency 화이트리스트(USD/JPY/EUR)로 한정
- WebSocket hello / subscribe / unsubscribe 메시지 schema 설계 + 구현
- topic delta payload schema (`fx:*`, 테더 탭, `krx:*`, `dxy`, `graph:*`, `news`)
- snapshot vs delta 구분 + snapshot 토픽별 분리
- ConnectionManager에 subscribe 상태 관리
- broadcast 분기: 구버전(미 subscribe) = legacy / 신버전(subscribe 메시지 보낸 클라이언트) = topic delta
- KRX는 처음부터 topic-only로 Stage 2 진입 (legacy 통합 단계 건너뜀)
- `/api/rates/usdt-krw` debug/compat 유지 vs 제거 결정 (별도 ADR 또는 본 ADR 후속 amend)
- **`app/crawlers/krx_kis.py` 모듈 분리** (현재 900+ lines, PR6d 시리즈로 비대):
  - `app/sources/kis_futures.py`에 REST quote 응답 정규화 흡수 (현재 `parse_*_payload`만)
  - `app/sources/kis_auth.py` 신설 — `KisApprovalManager` + `KisAccessTokenManager` 이전 (token/approval lifecycle)
  - `app/sources/kis_rest.py` 신설 — `fetch_kis_futures_quote` 등 REST helper 이전
  - `app/crawlers/krx_kis.py`는 WebSocket lifecycle (`KisFuturesClient`) + DB writer (`KrxDbWriter`) + metric만
  - 기준: **stateless adapter는 sources / stateful client·lifecycle은 crawlers** (현재 분류 정합 유지)
  - 즉시 refactor 아님. Phase Z-2 코드 분리 사이클에 포함

문서 정리 (코드 작업과 동시 진행, Phase Z-1 status 라벨에서 본격 통폐합으로 전환):

- **`REALTIME_V2_CLIENT_GUIDE.md` 신설** — iOS/Android V2 클라이언트 가이드. hello/subscribe/snapshot/delta 프로토콜 + 탭별 topic 구독 방식. 기존 `USDT_PHASE1_CLIENT_GUIDE.md`의 RateSource / 어댑터 / SourceRegistry 패턴 흡수
- **`SOURCE_ASSET_MODEL.md` 신설** — `USDT_PHASE1_DESIGN.md`에서 백엔드 도메인 모델만 추출 (`source_rates` 스키마 / SourceRegistry / 알림 모델 / comparison alert 설계). 백엔드 단일 진리 source
- **`USDT_TAB_PROPOSAL.md`** → `archived/` 이동 (역사적 제안서. 모델/방향은 SOURCE_ASSET_MODEL.md에 흡수됨)
- **`USDT_PHASE1_DESIGN.md`** → `archived/` 이동 (도메인 모델 SOURCE_ASSET_MODEL.md로 추출 후)
- **`USDT_PHASE1_CLIENT_GUIDE.md`** → `archived/` 이동 (REALTIME_V2_CLIENT_GUIDE.md 대체 후)
- **`KRX_CANARY.md` → `KRX_OPERATIONS.md`** 이름 변경 검토 (Stage 2 진입 후 일반 운영 runbook으로 격상 시점)
- CLAUDE.md "USDT / KRX 도메인 문서" 섹션 정리 (archive된 문서 참조 제거 + 새 문서 참조 추가)

**Phase Z-3 — legacy 제거 (6개월+, 사용자 통계 기반)**:

- 활성 구버전 < 1% 양쪽 플랫폼 도달 + 6개월 경과 시 legacy `rates` 채널 폐기 후보
- 폐기 결정 시 별도 ADR

### 위험과 완화

- **Phase Z-2 코드 분리 중 회귀 위험**: legacy `rates`에서 USDT 분리 시 운영 앱(테더 탭 없음)에 영향 0이지만, `/api/rates/usdt-krw` 호출자가 있을 수 있음 → 분리 전 traffic 확인 + grace period
- **topic protocol 도입 비용**: WebSocket subscribe 인프라 + 클라이언트 가이드 + dual-emit + snapshot 분리 — Phase Z-2 작업량 큼 (2-4주)
- **KRX Stage 2 진입 지연**: topic protocol v1 도입 완료 전까지 KRX 사용자 노출 불가. 5/18 만기 관찰은 Stage 1(topic-only 격리 상태)에서도 가능

### 관련 문서

- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): topic 라우팅 + dual-emit 전체 plan (단일 진리 source)
- [ADR-027](#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안): KRX REST/stale 정책 (topic 채널 기준 적용)
- [USDT_TAB_PROPOSAL.md](USDT_TAB_PROPOSAL.md): USDT 탭 초기 제안서 (rollout 방식 superseded)
- [USDT_PHASE1_DESIGN.md](USDT_PHASE1_DESIGN.md): 백엔드 도메인 모델 유효 / legacy 통합은 superseded
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md): 데이터 수신 방식은 V2 protocol로 대체 예정
- [KRX_CANARY.md](KRX_CANARY.md): Stage 2 진입 조건에 topic protocol 의존성 명시

---

## ADR-029: USDT source는 mirror cycle 미경유 — direct write + read-path DB fallback

**상태**: Accepted (2026-05-12, PR Z-2e B-Step 1/2 운영 검증 완료)

### 맥락

PR Z-2d (2026-05-12)로 legacy 노출 정책을 `legacy_policy.should_include_source_in_legacy_rates` allowlist로 통일했다. USDT 5거래소(upbit/bithumb/coinone/korbit/gopax)는 allowlist 미통과 → `latest_rates_cache.should_include_source_in_latest`가 False 반환 → mirror cycle이 USDT source를 skip한다.

결과적으로 USDT는 mirror cycle의 safety repair 대상이 아니다. 그러나 usdt:krw topic은 사용자에게 노출되는 채널이라 데이터 freshness가 필요했다 — broadcast hot path는 ADR-026 mirror 기반(broadcast cycle 매초 DB 안 거침)이지만, 그건 mirror 적용 source 한정. USDT topic builder는 별도 데이터 흐름 필요.

### 결정

USDT source는 mirror cycle 대신 **crawler-driven direct write + topic builder Redis-first read** 조합을 사용한다.

1. **Direct write (B-Step 1, `1f3ab36`)**:
   - `app/crawlers/usdt_sources.py`가 `insert_source_rate_if_changed`가 True 반환 시 `latest_rates_cache.set_latest_usdt_rate_from_sync_job` 호출
   - sync `redis.Redis` client 사용 — scheduler thread executor에 자연스러움
   - cache.py의 `redis.asyncio.Redis`(async circuit_breaker 사용)와 격리 — broadcast/mirror Redis path 오염 방지
   - 실패는 logger.warning + False, 호출자(crawler)에 전파 X

2. **Redis-first read (B-Step 2, `6f743f0`)**:
   - `usdt_topic_payload.load_and_build_tether_tab_payload`가 5거래소 `latest:source:*` key를 sync GET
   - 5개 모두 hit → Redis 사용 (DB query 0회)
   - 1개라도 miss/parse fail → `crud.get_latest_source_rates_for_topic`로 **전체 5거래소 DB fallback** (source별 mix 회피로 payload 일관성)
   - **Stale 시간 판정 X** — USDT는 mirror 갱신 없음 → mirrored_at이 마지막 direct write 시점에 stuck. 거래 뜸한 source(gopax 등)는 정상 시장 stagnant라 자연 오래됨. is_stale 적용 시 거의 매번 fallback → Redis-first 무의미.

3. **Legacy adapter 우회**: topic builder는 `crud.get_source_rates_as_legacy_format`(Z-2d filter 적용) 대신 신규 `crud.get_latest_source_rates_for_topic` 사용. legacy_policy 우회 의도 명시.

### Trade-offs

- **장점**:
  - 정상 운영 시 USDT DB query 5회/cycle → 0회 (Redis-first hit)
  - mirrored_at - timestamp ~6-7ms (mirror cycle 3초 대비 즉시)
  - 실패 시 DB fallback 보장 — topic payload 항상 build 가능
  - async circuit_breaker 격리 — broadcast hot path 보호

- **단점 / 미해결**:
  - **direct write 영구 실패 시 영구 stale 위험**: mirror cycle이 복구하지 않음. 일시적 Redis 장애는 read path DB fallback이 처리하지만, write 측 지속 실패는 다음 INSERT까지 latest:source key 갱신 X
  - 거래 뜸한 source는 mirrored_at 자연 오래됨 → 운영자가 "stale인가 정상인가" 구분 어려움. **별도 telemetry 도입** (direct write 성공률, fallback 호출 빈도): B-Step Telemetry(2026-05-13)에서 `app/usdt_redis_stats.py` + `GET /admin/api/usdt-redis-stats`로 1차 계측 도입. process-bound counter(재시작 reset, `started_at` 노출). 향후 Prometheus/Redis hash 고도화는 추세 분석 필요 시점에 검토.

### Alternatives 검토 (rollback 사례)

**시도 1 — async client 재사용** (`887ccd1`, rollback by `82ba062`):
- `asyncio.run(set_latest(...))` + main loop의 `redis.asyncio.Redis` client 재사용
- 결과: event loop binding mismatch — async client는 main loop에 bound, asyncio.run의 새 loop와 호환 X
- direct write 매번 False + `circuit.record_failure()` 누적 → mirror/broadcast Redis path 위협
- mock 단위 test로는 발견 불가, 운영 검증으로만 노출됨
- 교훈: event loop-bound / context-aware library는 실제 environment dry-run 필수 (D1 권장)

**시도 2 — `run_coroutine_threadsafe(coro, main_loop)`**:
- main event loop reference 보존 + scheduler thread에서 submit
- 검토 결과: main loop reference 관리 / shutdown 순서 / timeout / deadlock 회피 등 spec 확장
- B-Step 1 범위에서 거부 — sync client 분리가 더 단순

**시도 3 (채택) — sync `redis.Redis` client 별도**:
- cache.py async와 분리
- scheduler thread에 자연 (redis-py sync는 internal connection pool로 thread-safe)
- async circuit_breaker 격리

### 운영 검증 (2026-05-12)

- B-Step 1 운영 활성화: USDT direct write mirrored_at - timestamp **6-7ms** 확인 (upbit/bithumb 활발 거래소)
- B-Step 2 운영 활성화: `usdt:krw` topic API error=0, DB usdt 조회 0회 (Redis 5거래소 hit), `read path DB fallback` 로그 0건
- async cache.py circuit_breaker 미오염 — broadcast/mirror "일부 실패" warning 0건 유지

### 관련 문서

- [ADR-026](#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성): broadcast hot path Redis-first (mirror 기반, USDT skip 대상)
- [ADR-028](#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit): Topic-only USDT/KRX (legacy 제거 정책)
- [USDT_TOPIC_MIGRATION_PLAN.md](USDT_TOPIC_MIGRATION_PLAN.md): Z-2e B-Step 1/2 작업 history
- [USDT_PHASE1_CLIENT_GUIDE.md](USDT_PHASE1_CLIENT_GUIDE.md): 단말 freshness 기준 = topic payload (legacy rates 미사용)

---

## ADR-030: latest:index 책임 분리 — freshness는 per-key mirrored_at으로 판단

**상태**: Proposed, **Deployed and observing** (2026-05-14)

- 배포 commit: `489359c` (PR Z-2f, 2026-05-13 22:16 KST)
- 즉시 smoke (Z-2f 핵심 시그널):
  - legacy `/api/rates/usd-krw` HTTP 200, `/admin/api/topic-status` (usdt + 3 fx) error 0
  - `fallback_reason=redis_stale` (rates path) **0건** — index stale gate 제거 후 발생 X
  - `per_key_stale` 0건 — 모든 data key fresh, Redis-first 정상 통과
  - Step 3b warning(`bank/investing sync Redis SET 실패`) 0건 — 회귀 가드 OK
- **Accepted 전환 조건** (baseline 누적 후):
  - `fallback_reason=redis_stale` (rates path) 소멸 — 기대값 0 유지
  - `per_key_stale` fallback 빈도 측정 + 기준선 확보
  - topic/broadcast error 0 유지
  - DXY 별도 fallback path 영향 X 유지 (`latest_dxy_fallback_reason` 정책 그대로)

### 맥락

ADR-026 (Redis-first broadcast hot path)은 `latest:index` control key의 `mirrored_at` 필드를 broadcast Redis-first read의 단일 freshness gate로 사용했다 ([app/latest_rates_cache.py:885-890 @ `a499a08`](https://github.com/Jay-Hong/exchange-rate/blob/a499a08d210ab1e6b8c5309357eab3bcf1283fa5/app/latest_rates_cache.py#L885-L890)). mirror cycle이 3초마다 latest:index와 모든 data key를 함께 갱신하는 모델에서는 정합한 단일 시그널이다 — "한 cycle에서 모든 key가 동일한 mirrored_at으로 갱신됨"의 invariant.

PR Z-2e Step 3b(`a499a08`, 2026-05-13)로 bank/investing crawler가 commit 직후 `latest:bank:*` / `latest:investing:*` key를 sync Redis client로 직접 쓰는 direct write 시대에 진입했다. mirror cycle은 여전히 3초 주기로 운영되지만, 개별 data key는 mirror보다 먼저 direct write로 갱신될 수 있다.

이로 인해 `latest:index.mirrored_at` 의미가 흔들리기 시작했다:

1. **freshness 의미 거짓 가능성**: 개별 key만 direct write로 갱신된 상태에서 latest:index를 함께 갱신하면(옵션 A), broadcast가 "모든 key가 신선하다"고 오해. 실제로 다른 key는 mirror cycle 이전 값. 부분 갱신 + 전체 fresh 신호 = invariant 깨짐.

2. **JSON read-modify-write race**: `latest:index = {"keys": [...], "mirrored_at": "..."}` 는 단일 JSON 값. direct write가 mirrored_at만 갱신하려면 GET→modify→SET 필요. mirror cycle과 동시 실행 시 keys list가 옛 값으로 덮일 가능성. Lua/WATCH-MULTI 없이는 race 회피 불가.

### 결정

**옵션 C 채택** — `latest:index`는 key membership list로 격하, freshness는 각 data key의 `mirrored_at`으로 판단.

1. **`latest:index` 역할 재정의**:
   - `keys` 필드: broadcast가 어떤 data key를 읽을지 알려주는 membership list (유지)
   - `mirrored_at` 필드: 1차에서는 schema 유지하되 broadcast read path에서 **ignore** (백워드 호환, rollback 안전성)
   - schema 정리(mirrored_at 제거)는 향후 별도 PR

2. **`fetch_rates_from_redis()` 분기 재작성**:
   - index miss / parse fail → 전체 DB fallback (현 동작 유지)
   - **index `mirrored_at` stale 판정 제거** (현 line 886 gate 삭제)
   - MGET → 개별 data key value deserialize
   - **per-key `is_stale()` 검사** (기존 함수 재사용, `LATEST_MIRROR_INTERVAL_SECONDS * STALE_RATIO`)
   - 1개라도 stale / miss / parse fail → **전체 DB fallback** (보수적 1차)

3. **부분 fallback은 future phase**: per-asset fallback (broken key만 DB, 나머지 Redis)은 옵션 C 안정 후 별도 PR.

### Trade-offs

- **장점**:
  - direct write 시대에 정합한 freshness 시맨틱
  - 개별 key 갱신과 broadcast freshness 판정이 일치
  - Read-modify-write race 회피 (broadcast가 index.mirrored_at 안 봄)
  - mirror cycle interval 격하의 전제 조건 (옵션 C 안정 후 mirror 3s → 10s/30s 검토 가능)

- **단점 / 제약**:
  - broadcast read 비용 증가 (MGET 후 per-key stale 검사 추가) — 메모리 비교라 ms 수준
  - 보수적 1차는 1개 stale도 전체 fallback → 안정 후 부분 fallback 검토 필요
  - mirror cycle 3s 1차 유지 — 옵션 C 자체는 mirror 격하 결정 X

### Alternatives 검토

**옵션 A — direct write가 `latest:index.mirrored_at`만 갱신**:

- 변경 폭 가장 작음 (Redis SET 1회 추가)
- 거부 이유:
  - freshness 시맨틱 invariant 깨짐 (위 "맥락" 참조)
  - JSON read-modify-write race (Lua/WATCH 없으면 keys list 덮어쓰기 위험)

**옵션 B — direct write가 `latest:index` 전체 atomic 재작성**:

- mirror cycle 완전 격하/제거 가능
- 거부 이유:
  - 9 은행 + investing 동시 쓰기 race → Lua 스크립트 / WATCH/MULTI 필요
  - 1차 구현 비용 과대, 회귀 위험 큼

**옵션 C (채택) — `latest:index` membership list 격하**:

- broadcast read path 변경만 필요 (write path는 mirror cycle/direct write 그대로)
- mirror cycle 1차 유지 → safety net 보존
- future mirror 격하 phase의 전제

### Schema 호환 / Rollback 정책

- `latest:index` JSON 형식 변경 X — mirror cycle은 그대로 `{"keys": [...], "mirrored_at": "..."}` write
- broadcast read path만 `mirrored_at` field 무시
- rollback 시 read path 옛 로직 복귀 → 기존 schema 그대로 사용, 운영 무중단
- 백워드 호환 테스트 필수 (기존 latest:index 값으로 새 read path 정상 동작)

### Telemetry / Metrics

- `fallback_reason=redis_stale` (rates path)은 ADR-030 후 사실상 **deprecated** — line 886 index stale gate 자체 제거되므로 rates path에서 발생하지 않게 됨. Z-2f 이후 rates path의 stale 시그널은 `per_key_stale` 사용.
- DXY는 별도 mirror key + 별도 fallback path → `latest_dxy_fallback_reason=redis_stale`은 기존 정책 그대로 유지 (ADR-030 영향 X).
- 신규 `fallback_reason` 값:
  - `per_key_stale`: 1개 이상 data key가 `is_stale()` 통과 못 함 (1차 신규 추가)
- **기존 reason 유지** (rename 없음, analyzer 호환):
  - `redis_miss`: data key 1개 이상 부재 — semantic 그대로
  - `redis_error`: deserialize 실패 / MGET error / length mismatch / key_to_rate 변환 실패 — semantic 그대로
  - 향후 `per_key_miss` / `per_key_parse_fail` 분리 필요 시 별도 PR (metrics analyzer 호환 보강 포함)
- `scripts/analyze_broadcast_metrics.py` 출력은 `per_key_stale` 카테고리 추가 시점에 갱신 (별도 PR)

### Mirror Cycle 책무 (1차 유지)

- `latest:index.keys` 관리 (membership 변동 감지)
- warmup (cold start 시 DB → Redis 적재)
- repair (data key 누락/실패 복구)
- unchanged key의 mirrored_at refresh (OUT 모드 / 야간 hourly crawl source 보호)
- interval 3초 유지 — 격하는 별도 phase

### 후속 Phase

1. **mirror cycle interval 격하** (3s → 10s/30s 단계적): per-key stale 시맨틱 안정 후
2. **mirror cycle 역할 축소**: warmup/repair 전용으로 격하 검토
3. **부분 DB fallback** (per-asset): 옵션 C 1차 안정 후 별도 PR

### 운영 검증 (예정 — Accepted 전환 조건)

- `fallback_reason=redis_stale` (rates path) **소멸 — 기대값 0** (index stale gate 제거로 발생 케이스 자체 사라짐)
- `per_key_*` fallback 빈도 측정 (기준선 형성)
- topic/broadcast error 0 유지
- legacy `/api/rates/usd-krw` HTTP 200 유지
- direct write gap ms 단위 유지 (PR Z-2e Step 3b 회귀 없음)

### 관련 문서

- [ADR-026](#adr-026-redis-first-broadcast-hot-path--latest-mirror--dxy-mirror로-db-free-달성): Redis-first broadcast hot path (latest:index 도입 결정)
- [ADR-029](#adr-029-usdt-source는-mirror-cycle-미경유--direct-write--read-path-db-fallback): USDT direct write (mirror cycle 미경유)
- [USDT_TOPIC_MIGRATION_PLAN.md Z-2f](USDT_TOPIC_MIGRATION_PLAN.md): 본 ADR 구현 PR 추적
- [app/latest_rates_cache.py:2040-2063](app/latest_rates_cache.py#L2040-L2063): 현재 per-key freshness 판정과 fallback 위치

---

## ADR-031: KRX 미국달러선물 Redis 통합 — 1차 부채 해소 (stale/REST는 후속)

**상태**: Proposed, **Deployed and observing** (2026-05-14)

- 배포 commit: `0756329` (2026-05-14 12:53 KST, KRX 정규 세션 중)
- 즉시 smoke: legacy /api/rates 200, usdt:krw topic error 0, KRX Redis key 존재, Step3b/Z2f warning 0
- 결정적 증거: `latest:source:krx:usd-krw-futures` `timestamp`/`mirrored_at` gap 4ms (KRX tick → DB insert → Redis direct write 흐름 ms 단위 작동)
- Telemetry 분리 검증: `usdt_redis_stats.per_source`에 krx 미등장 — KRX 전용 helper의 stats 분리가 운영에서 확인
- **Stable observed 조건** (필수 — 일상 cycle 검증):
  - KRX session 전환 1회 이상 정상 통과 (정규 → 휴식 → 야간 → 휴식)
  - `usdt:krw` topic error 0 유지
  - `usdt_redis_stats.per_source`에 krx 미등장 지속
- **Accepted 조건** (Stable observed + 일회성 만기 event):
  - 위 Stable observed 모두 충족
  - **5/18 만기 contract rollover 통과** (PR6c-2d-1 자동 reconcile + Redis key 자연 갱신)
  - 5/18 관찰 명령 / 체크리스트 / baseline 쿼리는 [KRX_CANARY.md](KRX_CANARY.md) 참조

### 맥락

PR Z-2f([ADR-030](#adr-030-latestindex-책임-분리--freshness는-per-key-mirrored_at으로-판단)) 이후 broadcast Redis-first read path는 per-key mirrored_at 기반으로 정착. USDT는 [ADR-029](#adr-029-usdt-source는-mirror-cycle-미경유--direct-write--read-path-db-fallback)로 Redis-first 모델 정렬. 은행/Investing은 Step 3b(`a499a08`)로 Redis-first 모델 정렬. **KRX만이 유일하게 topic-only source인데 Redis-first 모델 밖**.

[app/usdt_topic_payload.py:326-330 @ `2d5c8ad`](https://github.com/Jay-Hong/exchange-rate/blob/2d5c8adfd7a46944617c854d8221a5535bb1a95b/app/usdt_topic_payload.py#L326-L330):

```python
krx_futures_rate = get_latest_source_rate(db, "krx", "usd-krw-futures")  # DB query only
```

[같은 역사적 파일의 262-264행](https://github.com/Jay-Hong/exchange-rate/blob/2d5c8adfd7a46944617c854d8221a5535bb1a95b/app/usdt_topic_payload.py#L262-L264)에 명시: *"KRX는 mirror skip + direct write 미구축 → DB query 유지 (별도 phase)"*. 그 phase가 본 ADR.

### 결정

KRX tick → Redis direct write + topic builder Redis-first read로 정렬한다. **stale/REST fallback 정책은 본 PR 범위에 포함하지 않는다** ([ADR-027](#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안) 영역으로 분리).

1. **KRX 전용 helper 신설** (`app/latest_rates_cache.py`):
   - `set_latest_krx_rate_from_sync_job(asset, rate, timestamp) -> bool`
   - `get_latest_krx_rate_from_sync_job(asset) -> Optional[Dict[str, Any]]`
   - 둘 다 **`usdt_redis_stats` 미부착** (bank/investing helper 패턴 일관)

2. **`KrxDbWriter` 변경** (`app/crawlers/krx_kis.py`):
   - 기존 1s debounce + `insert_source_rate_if_changed` 그대로 유지
   - DB insert 성공 시 `set_latest_krx_rate_from_sync_job` 호출 (best-effort)
   - Redis write 실패 → `logger.warning`, writer loop 영향 X

3. **Topic builder 변경** ([app/usdt_topic_payload.py:327-333 @ `0756329`](https://github.com/Jay-Hong/exchange-rate/blob/0756329228d6048f986e6f759ac31037325bc41b/app/usdt_topic_payload.py#L327-L333)):

   ```python
   if include_krx:
       krx_redis = get_latest_krx_rate_from_sync_job("usd-krw-futures")
       if krx_redis is not None:
           krx_futures_rate = krx_redis
       else:
           krx_futures_rate = get_latest_source_rate(db, "krx", "usd-krw-futures")
   ```

   - stale/age guard 없음 (1차 의식적 제외)

### Telemetry 분리 — usdt_redis_stats 오염 회피

기존 `set_latest_usdt_rate_from_sync_job` / `get_latest_usdt_rate_from_sync_job`(PR-A 2d5c8ad에서 rename)는 모두 `usdt_redis_stats` counter를 갱신한다 ([latest_rates_cache.py:256-330](app/latest_rates_cache.py)). KRX에서 재사용 시 `usdt_redis_stats.per_source["krx"]`가 등장 → `/admin/api/usdt-redis-stats` 시맨틱 오염.

따라서 KRX는 writer/reader **양쪽 모두 전용 helper**를 둔다. 미래 KRX telemetry 필요 시점에 generic writer 분리 또는 별도 telemetry 모듈 검토 — 본 PR 범위 외.

### 1차에서 의식적으로 안 하는 것

- **timestamp age 기반 stale 누락**: `insert_source_rate_if_changed`라 가격 stagnant 시 DB row 안 생김 → Redis timestamp도 같이 stuck. 즉 row age는 *raw frame liveness*와 다름 (DB row gap ≠ raw frame gap). timestamp age로 stale 판단하면 저유동성 정상 상태를 false positive로 누락 → 기존 stale 관찰 장치 의도와 정면 충돌. → [ADR-027](#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안) 영역.
- **REST snapshot/fallback 결과 Redis 반영**: ADR-027 결정 시점에 진입.
- **Tick fanout handler 분리** (RedisLatestWriter / AlertEvaluator / DbWindowWriter 3-way): 후속 ADR 영역. 1차는 KrxDbWriter 단일 handler 안에서 DB insert → Redis write 호출.
- **KRX 알림 evaluator 연결**: fanout 패턴 정립 phase에 함께.

### Trade-offs

- **장점**:
  - 마지막 비대칭 해소 — USDT/은행/Investing/KRX 모두 Redis-first 정렬
  - topic builder의 DB query 의존 제거 (KRX hit 시 DB 0회)
  - 작업 폭 작음 (~50 LOC code + 12 tests)
  - ADR-027 결정 대기 안 함 — 독립 진행 가능

- **단점 / 제약**:
  - 1차에서 stale guard 없음 → KRX 시장 close 시 옛 timestamp 그대로 노출 (의도된 동작). 클라이언트가 timestamp로 판단.
  - tick-level Redis write 아님 — `insert_source_rate_if_changed` 통과 시만 갱신 (가격 stagnant 시 mirrored_at도 stuck). tick-level은 후속 fanout phase.

### Alternatives 검토

**Option A — `set_latest_usdt_rate_from_sync_job` 재사용**:

- 거부 이유: `usdt_redis_stats.per_source["krx"]` 등장 → telemetry 오염

**Option B — timestamp age 보수 가드 (1시간 등)**:

- 거부 이유: DB row gap ≠ raw frame gap. 저유동성 정상 상태 false positive 위험.

**Option C — tick fanout handler 분리** (RedisLatestWriter / AlertEvaluator / DbWindowWriter):

- 거부 이유 (이번 PR만): 작업 폭 큼. KRX 1차 검증과 패턴 정립을 동시 진행은 risk. 별도 phase에서 정립 후 KRX/USDT/bank에 적용.

**Option D (채택) — KRX 전용 helper + builder Redis-first + stale 정책 분리**:

- 작업 폭 작음, stats 오염 회피, ADR-027 의존 X

### 운영 검증 (예정 — Accepted 전환 조건)

- `usdt:krw` topic API error 0 유지
- KRX `latest:source:krx:usd-krw-futures` key 존재 + KRX session 중 mirrored_at 갱신 확인
- KRX session 외(시장 close): Redis hit이어도 옛 timestamp 그대로 노출 (stale guard 없음 — 클라이언트 timestamp 판단 영역). Redis key 부재 시에만 DB fallback.
- 기존 stale 관찰 장치(`test_krx_fallback_eligibility.py`) 동작 변경 없음 확인

### 후속 Phase

1. **REST snapshot/fallback 활성화 (ADR-027)**: 5/18 만기 데이터 + 평일 baseline 기반 stale threshold 확정. REST 결과를 Redis/DB/topic에 반영하는 정책 결정.
2. **Tick fanout handler 분리**: KRX/USDT WebSocket의 tick path를 Redis/Alert/DB 3갈래로 분리. "알림 모든 tick, DB window close" 원래 계획 ([REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md)) 실현.
3. **은행/Investing fanout 적용**: 크롤링/송출 sub-second 단축 또는 mirror cycle 제거 시점에 동일 패턴 적용.

### 관련 문서

- [ADR-027](#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안): KRX REST snapshot/fallback + stale 정책 (1차 범위 밖)
- [ADR-028](#adr-028-topic-only-tetherkrx--legacy-fx-dual-emit): Topic-only 출시 계약 (KRX는 topic-only)
- [ADR-029](#adr-029-usdt-source는-mirror-cycle-미경유--direct-write--read-path-db-fallback): USDT mirror skip + direct write (유사 패턴)
- [ADR-030](#adr-030-latestindex-책임-분리--freshness는-per-key-mirrored_at으로-판단): per-key freshness (전제)
- [KRX_CANARY.md](KRX_CANARY.md): KRX Stage 1/2 운영 + Stage A/B baseline

---

## ADR-032: KRX 가격알림 evaluator — source-neutral 재사용 + KRX adapter + validate helper 분리

**상태**: Proposed, **Deployed and observing** (2026-05-26)

- 배포 commit: `e5ef42e` (F-1, default false land), `9cbd7ae` (F-2, API 허용), `KRX_ALERT_EVALUATOR_ENABLED=true` env (F-3 활성, 2026-05-26 13:40 KST)
- iOS canary 완전 성공: setting id=6 POST 13:40:06 → FCM 발사 13:40:08 (2초) → iOS 도착 확인 (LG U+ 잠금화면 "📈 미국달러F USD-KRW-FUTURES [1504.7↑이상 도달] 1505.30")
- DB persist 일치: triggered_rate=1505.3, success=True, triggered=True, enabled→False (1회성 자동 disable)
- 중복 발송 차단 검증: `stale cache (enabled/triggered) skip` log 2회 (다음 bucket flush 시점)
- 서버 측 alert_evaluator 예외 0건

### 맥락

[ADR-031](#adr-031-krx-미국달러선물-redis-통합--1차-부채-해소-stalerest는-후속) 1차 KRX Redis 통합에서 "Tick fanout handler 분리 (RedisLatestWriter / AlertEvaluator / DbWindowWriter 3-way)"는 **후속 ADR 영역**으로 분리됐고 "KRX 알림 evaluator 연결: fanout 패턴 정립 phase에 함께"라 명시됨. [KRX_FANOUT_REFACTOR_PLAN §5.2 F](KRX_FANOUT_REFACTOR_PLAN.md)에서 "KrxAlertEvaluator 구현 — 알림은 모든 tick 원칙 실현"이 ADR-031의 후속 Stage F로 자리. 본 ADR이 F-1/F-2/F-3 trilogy를 통해 그 자리를 채운다.

USDT 5 source는 이미 `UsdtAlertEvaluator`로 source-neutral 평가 chain (settings cache + PriceAlertCoalescer 5초 wall-clock grain + refetch + FCM)을 운영 중. KRX 알림은 다음 두 가지 설계 분기점이 있다:
1. **별 evaluator class 분리** vs **source-neutral helper 재사용**
2. **API validation 위치** — main.py thin wrapper vs source_registry 도메인 helper

### 결정

KRX 가격알림은 **`UsdtAlertEvaluator` source-neutral helper를 재사용**하고 KRX-specific 로직은 **adapter layer**에 집중한다. F-2 validation helper는 **`source_registry`로 분리**해 main.py firebase_admin import chain을 우회한다. F-2 (API 허용) / F-3 (env 활성) 단계 분리로 dead alert gap을 의도된 canary staging으로 사용한다.

1. **`KrxAlertEvaluator(UsdtAlertEvaluator)` thin wrapper** (`app/notifications/alert_evaluator.py`):
   - body=`pass`. UsdtAlertEvaluator가 이미 `observation.source/asset` 기반 source-neutral (settings cache, condition_matches, refetch, FCM payload). 별 evaluator 분리는 코드 중복만 늘림.
   - 로깅/타이핑/향후 KRX-specific 분기 자리 확보 목적.

2. **`KrxAlertTickHandler` adapter** (`app/crawlers/krx_kis.py`):
   - KIS payload (source/asset/price/received_at/session) → `AlertObservation(source, asset, rate, timestamp_ms, kind="tick")` 변환.
   - `price`: `Decimal.quantize(0.1)` 0.1 KRW tick 정규화 (`KrxDbWriter`/`KrxRedisLatestWriter` mirror).
   - `received_at`: KST naive ISO → KST aware datetime → UTC epoch ms 변환 (운영 payload는 `datetime.now(KST).replace(tzinfo=None).isoformat()`).
   - `KRX_ALERT_EVALUATOR_ENABLED=false` 시 defensive early return (env 변경 timing 안전망).

3. **`KisFuturesClient._drain_alert_tick_handlers(timeout)` helper** (외부 검토 #1 보강):
   - `PriceAlertCoalescer`는 마지막 5초 bucket을 *다음 bucket의 tick 도래* 또는 *evaluator.close()* 호출 시점에만 emit. KRX는 1 client가 여러 session (CF↔CM) 전환이라 USDT의 "1 source = 1 WS lifecycle 종료 시 finally drain" 패턴이 자동 적용되지 X.
   - session boundary (`_run_session`의 `current_session != session` 분기) + `stop()` 양쪽에서 명시 drain — CF↔CM 갭 동안 close grace crossing 누락 차단.
   - 등록된 `KrxAlertTickHandler` instances만 선택 close (다른 tick handler 영향 X). 예외 격리.

4. **`source_registry.validate_alert_source_asset(source, asset) -> Optional[str]` helper 분리** (FastAPI 비의존):
   - main.py에 helper 두면 단위 테스트가 firebase_admin import chain으로 깨짐 (과거 메모리 기록 `project_main_py_helper_placement` 참조 — 2026-05-10 Z-2b Stage 2에서 동일 패턴 발견 후 영구 적용 권고).
   - 본 helper는 None (통과) 또는 에러 메시지 string (차단) 반환. main.py `_validate_phase1_source_asset`는 HTTPException thin wrapper만 담당.
   - 허용 대상: `category in ("exchange", "derivative")` — F-2 (2026-05-26)에서 derivative=KRX 추가.

5. **F-2 (API 허용) / F-3 (env 활성) 분리 — dead alert gap 의도**:
   - F-2 land (9cbd7ae) 후 `KRX_ALERT_EVALUATOR_ENABLED=false` default → API 등록 가능 + 발송 안 됨 = 의도된 canary staging gap.
   - 운영 단말 영향 0 (테더 탭 자체가 운영 앱에 없음, 테스트 iOS canary 전용).
   - F-3 활성 = env override + force-recreate. EC2에 이미 F-1/F-2 코드(`9cbd7ae`)가 land된 상태면 env 토글만으로 충분(`docker compose up -d --force-recreate fastapi`). 이전 commit이 land되어 있으면 `git pull + docker compose build fastapi + force-recreate` 순서 필요 (5/26 활성 시점은 EC2가 `c3cb1c1`이라 후자 절차로 진행). `docker compose restart`는 env_file 변경을 반영하지 않으므로 `--force-recreate` 필수.

### Anchor — 3 invariants (회귀 잠금)

본 ADR이 land한 이후 회귀 차단할 정책 anchor:

1. **SET-only ❌**: alert는 mirror layer (KrxRedisLatestWriter)의 SET/SKIPPED/FAILED outcome 분기와 직교. 매 tick observation이 평가 대상. coalescer 5초 wall-clock grain이 noise 차단.
2. **Close grace skip ❌**: `KrxRedisLatestWriter.__call__`은 close grace tick (15:45:00~15:45:59 / 06:00:00~06:00:59) skip (KrxCloseWindowWriter non-interference)이지만, `KrxAlertTickHandler.__call__`은 close grace tick **평가 진행** — 종가 crossing 보존 (사용자 알림 누락 방지 우선).
3. **Session boundary drain**: PriceAlertCoalescer pending bucket은 `_run_session` boundary return 전 + `stop()` 양쪽에서 helper로 명시 flush. CF↔CM 갭 동안 close grace crossing 누락 차단.

### Trade-offs

- **장점**:
  - `UsdtAlertEvaluator` source-neutral helper 재사용으로 코드 중복 0 — KRX 추가가 logging/typing wrapper 1줄 + adapter 1개로 land.
  - F-1 default false + F-2 API 허용 + F-3 env 활성의 단계 분리로 each land가 default-safe (운영 영향 0).
  - `source_registry.validate_alert_source_asset` 분리로 main.py firebase_admin import chain 격리 — 향후 알림 validation 단위 테스트 안전.
  - close grace tick alert 평가 진행으로 종가 crossing 알림 누락 0.

- **단점 / 제약**:
  - thin subclass 패턴은 KRX 전용 분기가 필요해질 때까지 의미가 약함 (현재는 logging/typing 목적). 단 분리 추가 비용 0이라 risk 낮음.
  - dead alert gap (F-2 land ~ F-3 활성) — 의도된 staging이지만 외부 사용자 영향 가능성을 운영 정책으로 차단 (테더 탭 운영 앱 미포함).
  - `_validate_phase1_source_asset` historical name — F-2에서 derivative 허용해 의미적으로 "phase1" 표현이 좁아짐. F-3 직후 별도 cleanup commit으로 `_validate_alert_source_asset_or_400`로 rename 완료 (2026-05-26).

### Alternatives 검토

**Option A — `KrxAlertEvaluator` 별 evaluator class 신규 (no subclass)**:

- 거부 이유: USDT 5 source가 이미 source-neutral evaluator 재사용 패턴. KRX만 별도 class면 코드 중복 + 향후 USDT 추가 시 비대칭. thin subclass가 logging/typing 분기 자리 확보 + 동작 동일.

**Option B — main.py에 validation 본체 유지**:

- 거부 이유: firebase_admin import chain으로 단위 테스트 깨짐 (5/10 Z-2b Stage 2 발견 사례). source_registry로 분리 = FastAPI 비의존 + 테스트 격리.

**Option C — `alert_enabled` SourceDefinition 별 필드 추가 (phase1_enabled 의미 보존)**:

- 거부 이유: phase1_enabled의 외부 호출자 0개 (`is_phase1_source`, `get_enabled_sources` 모두 source_registry 내부만, `get_usdt_exchange_entries`는 asset+category 필터). KRX phase1_enabled=True 변경의 부작용 0. alert_enabled 추가는 변경 surface 증가만.

**Option D (채택) — Thin subclass + adapter + validate helper 분리 + F-2/F-3 단계 분리**.

### 운영 검증 (실측 완료 — 2026-05-26 iOS canary)

iOS FCM 도착 + DB persist + 서버 로그 4축 모두 통과:

| 검증 항목 | 결과 |
| --- | --- |
| F-2 API validation (`validate_alert_source_asset("krx", "usd-krw-futures") -> None`) | ✅ POST 200 (setting id=6) |
| F-1 KrxAlertTickHandler payload→AlertObservation 변환 | ✅ source=krx asset=usd-krw-futures rate=1505.3 kind=tick |
| F-1 coalescer 5초 grain flush + evaluate_price_input_async | ✅ 13:40:06 POST → 13:40:08 FCM (2초) |
| F-1 cache invalidate immediate | ✅ POST 직후 다음 tick에서 cache miss → DB query → 1건 평가 |
| F-3 env=true → KrxAlertTickHandler scheduler 등록 | ✅ `KRX_ALERT_EVALUATOR_ENABLED=True` 컨테이너 import 확인 |
| FCM multicast → iOS APNS 도착 | ✅ LG U+ iPhone 잠금화면 알림 표시 |
| 1회성 알림 (triggered=true 자동 disable) | ✅ enabled=False, triggered=True, last_notified_at/rate persist |
| 중복 발송 차단 (stale cache skip) | ✅ 다음 bucket flush에서 `stale cache (enabled/triggered) skip` log 2회 |
| alert_evaluator 예외 | ✅ 0건 |

### Stable observed / Accepted 조건

- **Stable observed** (필수):
  - F-3 활성 후 24h+ 운영 + KRX session 전환 1회 이상 (정규 → 휴식 → 야간 → 휴식) 정상 통과
  - alert_evaluator FCM sent log 누적 + 예외 0 유지
- **Accepted 조건** (Stable observed + 첫 close grace event):
  - 위 Stable observed 모두 충족
  - ✅ **5/26 15:45 CF close 첫 실측 통과 (2026-05-26)** — 4 layer 정합 확인 (close finalizer / Stage E / REST fallback / alert handler 예외 격리):
    - sampler `/tmp/krx_close_1545_sample.log` 5초 grain 36 sample
    - captured_flag SET (value="1" TTL 3594s) — WS 단독 처리 성공 신호
    - DB row id=451680 rate=1502.8 ts_kst=15:45:00.000 unconditional INSERT
    - Redis latest 15:46:00 갱신 (KrxCloseWindowWriter 단독)
    - REST `WS captured at entry → REST skip` (Case A, `rest_write_blocked` 증가 0)
    - pre-close 1분 동안 latest stagnant → Stage E non-interference (close grace tick skip 정상 동작)
    - alert_evaluator 예외 0 (sampler 시간대 docker logs 검증)
    - **(5/26 시점) 단 close grace tick에서 alert evaluation/FCM 발사 path는 직접 실측되지 않음** — 그 시점 활성 KRX 알림 0건이라(setting id=6은 13:40에 이미 triggered=true) candidates empty path를 silent하게 거침. invariant 2 (close grace skip ❌)는 *코드 구조상 보장* (`KrxAlertTickHandler.__call__`이 close grace check 없음 — `KrxRedisLatestWriter`와 다르게)이고, 운영 실측은 별도 close grace 시점 활성 알림 등록 후 검증 필요 — 다음 항목으로 닫힘.
  - ✅ **5/27 15:45 CF close grace + FCM canary 통과 (2026-05-27) — invariant 2 운영 실측 closed**:
    - canary trigger 15:43:15 KST start (custom token + ID token + 114s wait → 15:45:10 POST)
    - 등록 setting id=7 threshold=1490.3 (current-10.0 margin, Stage E close grace skip으로 Redis latest stale 마진 확보)
    - close finalizer 15:46:00 `[krx_close_window] close saved session=CF rate=1499.6 ts=2026-05-27T15:45:00+09:00`
    - alert evaluator 15:46:01 FCM sent (close grace tick path 실측) + DB log id=96 success=True
    - setting id=7 final: triggered=True, enabled=False, last_notified_rate=1499.6 (1회성 자동 disable)
    - iOS APNS 도착: 사용자 캡처 15:46:10 KST (iPad + iPhone 동시) → 서버 sent_at 15:46:01 대비 **사용자 확인 기준 ~9s 이내 (실제 도착은 그보다 빠름)**, 알림 문구 `📈 미국달러F USD-KRW-FUTURES [1490.3 ↑이상 도달] 1499.60`
    - structured event persist (commit `18b06f5` land 후 **첫 운영 검증**): `/admin/api/krx-finalizer-stats?days=1` → `ws_close_saved=1`, `rest_skipped_ws_captured=1`, `case_summary={"A": 1}` (정상 영업일 path)
    - REST skip: `[krx_close_snapshot] WS captured at entry → REST skip` (rest_write_blocked 증가 0)
    - **닫힌 항목**: invariant 2 (close grace skip ❌) — close grace tick에서 alert evaluator 평가 진행 + FCM 발사 + iOS 도착 end-to-end 검증
    - **별도 항목**: invariant 1 (SET-only ❌) + invariant 3 (Session boundary drain) — 본 canary로 직접 검증되지 않음. 1번은 코드 구조 보장 유지, 3번은 CF→CM session boundary 시점 활성 알림 별도 등록 후 검증 필요

### 5/19~5/26 7일 telemetry 분석 결과 — close REST fallback 정책 결론 (2026-05-26)

F-3 활성 직후, 5/19~5/26 close finalizer 데이터를 기준으로 `KRX_CLOSE_REST_WRITE_ENABLED=false` diagnostic-only 정책 ([ADR-027 follow-up](#adr-027-krx-미국달러선물-stage-2-진입-전-rest-snapshotfallback--stale-정책-초안) / 5/25 정책 PR `6a43785`)의 운영 적정성을 평가:

**Evidence level 분리 (정량 telemetry / 운영 관찰 / 한계 명시)**:

- **Hard evidence**: sampler 직접 실측 — 5/26 CF close = Case A 확인 (위 항목 참조)
- **Persistent evidence**: RDS `source_rates` table 5/19~5/26 KRX close boundary row 9건 존재 (CF 5/19~22 + 5/26, CM 5/20~23, 모두 timestamp 정확히 15:45:00 / 06:00:00 KST). 단 path source (KrxCloseWindowWriter vs REST write)는 DB row만으로 확정 불가
- **Operator observation**: 운영 중 수시 확인상 정상 영업일 close는 WS path (KrxCloseWindowWriter)로 처리됨을 직접 확인. 정량 분포 X (계량 telemetry로 포장하지 않음)
- **Unavailable**: 5/26 13:03 KST 재배포 이전 docker logs / container file logs / fastapi process counter 모두 유실. 7일 Case A/B/C **정량 분포 복원 불가**

**정책 결론**: 정상 영업일 close는 운영 중 수시 확인상 WebSocket close path로 처리됐고, 5/26 CF close는 sampler로 Case A를 직접 재확인했다. 다만 container 재배포로 과거 logs/process counters가 유실되어 7일 Case A/B/C 정량 분포는 복원할 수 없다. 따라서 close REST fallback은 **완전 제거하지 않고 `KRX_CLOSE_REST_WRITE_ENABLED=false` diagnostic-only 상태를 유지**한다 (5/25 사고 같은 edge case 진단 가치 보존 + REST 호출 자체는 KIS rate limit 위협 작음). 3차 PR scope에서 close REST 코드 완전 제거는 진행하지 않음.

> **Amendment 2026-06-10 + 2026-06-15 (위 'diagnostic-only / false 유지' 결론 supersede)**: (6/10) flag=true 의미가 diagnostic-only → **gate-checked write**로 재정의됨([KRX_CLOSE_SNAPSHOT_PLAN §5.7.8](KRX_CLOSE_SNAPSHOT_PLAN.md) — calendar/contract/session[tick recency 3h]/sanity gate 전부 통과 시에만 write). (6/15) A75606→A75607 rollover + CF close case A 후 prod `KRX_CLOSE_REST_WRITE_ENABLED=true` **활성**(16:12 KST, 별도 GO). **code default는 여전히 false** (완전 차단 복귀 시 flag=false). 첫 gate-checked write 실측 후보 = 6/16 06:00 CM(WS-miss 시).

**별도 개선 후보 (이번 PR scope 외 — telemetry 보존 인프라)**: CloudWatch log stream / `/app/logs/` host volume mount / `KrxCloseFinalizerStats` Redis/DB persist. 정상 영업일 WS path 운영 관찰 기반 신뢰가 충분하므로 우선순위 낮음. 향후 close finalizer 정책 변경 또는 자동 monitoring 강화 시점에 별도 PR로 진입.

### 후속 Phase

1. ✅ **`_validate_phase1_source_asset` → `_validate_alert_source_asset_or_400` rename** (완료, 2026-05-26 별도 cleanup commit) — F-2 derivative 허용으로 의미 변화 반영. main.py 호출처 2곳(POST + PUT) + history docstring 5곳 sync.
2. **F-2 다른 source 추가 시 자연 확장** — category="derivative" 외 새 카테고리 추가 시 validation 확장. 현재 USDT exchange + KRX derivative만 허용.
3. **iOS/Android client KRX UI** — 운영 앱에 테더 탭 + KRX 알림 UI 추가 (별도 release scope). 현재는 테스트 iOS canary 전용.
4. **`USDT_PHASE1_CLIENT_GUIDE.md` line 571 stale fix** — "서버 검증 (category=='exchange')" → "category in ('exchange','derivative')"로 갱신. 별도 cleanup PR scope (USDT 문서 도메인이라 KRX 알림 PR 안에서 함께 land하면 scope 흐려짐).

### 관련 문서

- [ADR-031](#adr-031-krx-미국달러선물-redis-통합--1차-부채-해소-stalerest는-후속): KRX Redis 통합 1차 (본 ADR 전제)
- [ADR-029](#adr-029-usdt-source는-mirror-cycle-미경유--direct-write--read-path-db-fallback): USDT direct write 패턴 (유사 source-neutral 책임 분리)
- [KRX_FANOUT_REFACTOR_PLAN.md](KRX_FANOUT_REFACTOR_PLAN.md) §5.2 F: KrxAlertEvaluator 구현 — 본 ADR이 그 자리
- [KRX_CANARY.md](KRX_CANARY.md) F-3 runbook: 운영 활성 절차 + 5/26 canary 실측 결과
- [REALTIME_ARCHITECTURE_PLAN.md §4.1](REALTIME_ARCHITECTURE_PLAN.md): "알림 모든 tick, DB window close" 원리 — close grace skip ❌ invariant 근거

---

## ADR-033: Graph API v2 catalog policy — legacy 공존 + Hana backfill + Bithumb/KRX actual-only + DXY_futures 1d only

**상태**: Proposed (2026-05-27)

- Graph API v2 catalog 정책 anchor — 첫 구현 진입 전 비가역 결정 잠금.
- 관련 design 문서: [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md)
- DB audit 근거 (2026-05-27 production EC2):
  - bank_exchange_rates 30.1~30.2일 span (cleanup 30d 실측)
  - investing_exchange_rates USD/JPY/EUR 443.1일 span (장기 backfill 적격)
  - source_rates 30.3~30.4일 span (`SOURCE_RATE_RETENTION_DAYS = 30`), KRX 22.5일
  - market_index_rates dxy daily 376일 / hourly 77일 / dxy_futures realtime **29.1일 only** (hourly/daily rollup 없음)

### 맥락

기존 legacy `/api/graph/{currency}` ([app/main.py:2545](app/main.py#L2545))는 다음 한계를 가진다:

- 3 통화 only (USD/JPY/EUR) — 테더 탭 미지원
- 1d는 KB + 하나 + investing + DXY, 1w+는 investing only — 은행 장기 그래프 미제공
- DXY는 USD 탭에만 표시 (다른 탭 DXY 노출 불가)
- catalog 발견 endpoint 없음 — 클라이언트 hard-coded 자산 목록

신규 단말은 테더 탭 도입 + Citi 제외 모든 은행 그래프 + USD 탭 DXY + Tether 탭 DXY/DXY_futures (DXY_futures는 1d only, USD 탭 1d에는 DXY_futures 미노출) + 테더 탭 5거래소 USDT + KRX 미국달러선물 노출이 필요하다. JPY/EUR 탭은 DXY 계열 미노출 (legacy v1 정책 유지). legacy v1 endpoint를 확장하면 구단말 호환을 깨거나 분기 폭증.

또한 2026-05-27 production DB audit 결과를 catalog 정책에 anchor해야 backfill/insufficient_history 결정이 데이터 측면 근거를 가진다. 회상 기반 backfill 논쟁 차단 + 외부 historical source 미발굴 상태에서도 honest empty state로 사용자에게 노출하는 정책이 잠긴다.

### 결정

Graph API v2를 신규 endpoint set으로 분리하고 다음 10개 정책을 anchor한다:

1. **legacy `/api/graph/{currency}` 유지** — 구단말 호환. v2 endpoint와 hot path/cache key 분리.
2. **new app은 Graph API v2 사용** — 새 단말은 v2 catalog endpoint로 자산 발견 후 tab graph endpoint fetch.
3. **catalog는 tab × period × series** — period-aware catalog. 같은 자산도 period에 따라 series 구성/source 다름.
4. **Citi는 v2 catalog에서 제외, 수집 layer는 유지** — presentation/catalog만 제외. [app/crawlers/citi.py](app/crawlers/citi.py) 수집은 그대로 (수집 중단은 별도 결정).
5. **Hana 장기 그래프 = 최근 30일 Hana actual + 이전 Investing backfill** — provenance chain. `actual_source="hana"` + `fallback_source="investing"` + `fallback_after_days=30`.
6. **Bithumb/KRX Investing backfill 금지** — USDT/KRW (crypto microstructure) ≠ KRX 미국달러선물 (futures instrument) ≠ Investing USD/KRW (interbank 환율). 성격 다른 source로 채우면 graph 의미 왜곡.
7. **Bithumb/KRX 장기 그래프는 external historical source 또는 daily rollup 없으면 insufficient_history** — `supports_periods` dynamic. 외부 source 확보 또는 rollup 도입 전까지 3m/1y empty + `history_policy.insufficient_history=true`.
8. **DXY futures는 1d only** — `market_index_rates.dxy_futures` realtime granularity만 있고 hourly/daily rollup 없음 (29.1일 only). 1d 외 period는 catalog 미노출.
9. **source_rates 30일 cap 때문에 daily rollup 없이는 자연 확장 불가** — `cleanup_old_source_rates` cron 매일 03:31 30d 이전 삭제. "시간이 지나면 확장된다"는 daily rollup 도입 후에만 성립.
10. **Daily rollup retention은 별도 결정** — source_rates realtime 30d 유지는 anchor이지만, 신규 daily rollup retention (영구 / 5년 / 2년) 결정은 storage 비용 산정 후 별 ADR 또는 구현 설계에서 결정. 본 ADR이 영구 보존을 잠그지 않는다.

### Trade-offs

- **장점**:
  - 신규 단말 catalog 요구 (테더 탭 + 8 은행 + USD 탭 DXY + Tether 탭 DXY/DXY_futures, DXY_futures는 1d only)를 legacy endpoint 분기 폭증 없이 land 가능
  - audit 결과를 정책에 anchor — backfill 가능 여부를 데이터 측면 근거로 잠금 (회상 기반 backfill 논쟁 차단)
  - provenance chain explicit — Hana 30d actual + Investing backfill 경계를 catalog metadata에 표시 → 클라이언트 unit/source 표시 일관성
  - Bithumb/KRX actual-only로 graph 의미 왜곡 차단 (USDT vs 환율 vs 선물 성격 분리)
  - retention 미확정 부분은 별도 ADR로 분리 → 본 ADR이 storage 산정 없이 영구 보존을 잠그지 않음

- **단점 / 제약**:
  - Bithumb/KRX 장기 그래프는 외부 source 발굴 또는 daily rollup 구현 전까지 사용자 경험상 3m/1y empty (수용 — 잘못된 backfill보다 insufficient_history 표시가 honest)
  - v2 endpoint 신규 도입으로 클라이언트 마이그레이션 비용 (legacy v1 공존으로 점진 대응)
  - DXY_futures 1d only — catalog UI에서 다른 자산과 period 불일치 (수용 — rollup 미도입 자연 결과)

### Alternatives 검토

**Option A — legacy `/api/graph/{currency}` 확장으로 catalog 요구 흡수**:

- 거부 이유: 통화 grid (USD/JPY/EUR/Tether) + 자산 종류 (은행 9 + investing + USDT 5 + KRX + DXY + DXY_futures) 곱하면 분기 폭증. 구단말 응답 schema 보장 깨질 위험. v2 분리가 hot path 분리 + cache key 분리 + rollout 단계화 모두 reversible.

**Option B — Bithumb/KRX Investing backfill 허용**:

- 거부 이유: USDT/KRW은 거래소 microstructure (premium/discount), KRX 미국달러선물은 futures instrument (basis spread), Investing USD/KRW은 interbank 환율. 성격 다른 source로 backfill = 그래프 의미 왜곡. 사용자가 "거래소 USDT의 과거 추세"로 해석하지만 실제로는 환율 데이터를 보고 있게 됨.

**Option C — daily rollup retention 영구 보존을 본 ADR에서 확정**:

- 거부 이유: 영구 보존 storage 비용 산정 없음. ADR 비가역성 기준상 retention 결정은 storage 산정 + 신규 테이블 schema 결정 후 별 ADR로 분리하는 게 reversible. 본 ADR이 default를 잠그면 향후 retention 조정 비용 증가.

**Option D — single series endpoint도 첫 구현에 포함 (catalog + tab + series 3개)**:

- 거부 이유: 첫 구현 핵심 use case는 "탭 전체 그래프 그리기" (catalog + tab graph 2개로 충분). single series는 디버그/optimization용 — 사용 패턴 확보 후 추가가 ETC. 처음부터 3개 contract 잠그면 client 의존 형성 후 변경 어려움.

**Option E (채택) — Decision 10개 + design 문서 분리 + endpoint 단계 분리** (Initial v2 = catalog + tab graph / Later v2 = single series).

### Stable observed / Accepted 조건

본 ADR은 **정책 결정 ADR**로 운영 검증 항목보다 land 후 design + 구현 진입 anchor 역할이 크다.

- **Proposed (현재 상태)**: ADR 본문 land + design 문서 ([GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md)) land + CLAUDE.md anchor 추가.
- **Accepted 조건**:
  - v2 endpoint 첫 구현 PR (catalog + tab graph 2개 endpoint) land + 신규 단말 client 어댑터 통합 + 1주일 운영 후 catalog grid 변경 없이 안정 운영.
  - Bithumb/KRX insufficient_history 응답이 client UI에서 honest fallback (empty state)으로 정상 처리 확인.
  - legacy `/api/graph/{currency}` 응답 변화 없음 (구단말 회귀 0).

### 후속 Phase

1. **Phase 2b** (본 ADR 직후, 동시 land): [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md) design 문서 + CLAUDE.md anchor.
2. **Phase 2c** (별도 트랙, 병행 가능): Bithumb USDT 외부 historical source 조사 (거래소 API/CSV) + KRX 미국달러선물 외부 historical source 조사 (KRX/KIS/공공데이터포털).
3. **Phase 2d**: daily rollup 구현 — DXY hourly/daily rollup 패턴 ([app/admin/dxy_rollup.py](app/admin/dxy_rollup.py)) mirror to source_rates. retention은 별도 ADR.
4. **Phase 2e**: v2 endpoint 구현 PR (catalog + tab graph 2개) — 별도 PR 분할 land.
5. **Phase 2f (Later, optional)**: single series endpoint (`GET /api/v2/graph/series/{series_id}?period={period}`) — 사용 패턴 확보 후 추가.

### Amendment 2026-05-27 (Phase 2c 검증 후 정책 변경)

2026-05-27 Phase 2c 외부 historical source 조사 결과 (Bithumb / KRX / Hana 3 source 모두 external_historical 확보) 기반으로 Decision 5 / 7 / 10 amendment. 기존 Decision 본문은 historical record로 보존하며, 본 Amendment 섹션이 현행 정책 anchor.

**Amendment 1 — Decision 5 변경**: Hana 30d actual + Investing backfill 폐기 → **Hana official historical endpoint 단일 source** 채택.

- 검증: Hana endpoint `wpfxd651_01i_01.do?ajax=true&curCd=USD&inqStrDt=YYYYMMDD`로 20년+ historical 확인 (2006-05-26 USD 매매기준율 945.50 / 2016-05-27 1,180.00 / 2026-05-26 USD 매매기준율 1,507.50). JPY/EUR 동일 endpoint.
- 휴일/공휴일 요청 시 자동 직전 영업일 fallback (2026-05-24 일 요청 → 응답 기준일 2026-05-22 금 / 2026-01-01 신정 → 2025-12-31).
- **Canonical date 정책**: 응답 안 `기준일` field 사용 (요청 날짜 아님). 같은 기준일 중복 요청은 idempotent skip → 휴일 fallback dedup 차단 anchor.
- **Backfill 호출 수**: calendar-day fetch (단순 loop) ~7300회/20년 + canonical date dedup 필수. business-day calendar 보유 시 ~5200회 (dedup 불필요). 권고는 **calendar-day + dedup** (영업일 calendar 외부 의존 차단).
- **Parser 기준**: `<td class="txtAr">` numeric cell 중 인덱스 7 (8th) = 매매기준율. 컬럼 순서 [0] 현찰 사실 환율 / [1] 사실 spread / [2] 파실 환율 / [3] 파실 spread / [4] 송금 보낼 / [5] 받을 / [6] 외화수표 파실 / [7] **매매기준율** / [8] 환가료율 / [9] 미화환산율.
- **회차 (`pbldSqn`) 정책 — 채택**: `pbldSqn=` 빈값 요청으로 반환되는 Hana 기본 일일 historical row를 v2 daily representative rate로 사용. "최종 회차"라고 공식 명칭으로 단정하지 않음 (Hana 측 공식 용어 검증 X). 응답 회차 값은 provenance/debug metadata로 저장 (예: `{"pbldSqn": 1081}`). 요청일과 응답 기준일 다르면 응답 기준일을 canonical date로 사용. 운영 검증상 최신/최종 고시값으로 동작 (5/27 검증: 2026-05-26 USD = 1081회차, 다음 영업일 07:46 발표).
- DOM 변경 risk: 현재 시점 schema 안정 (응답 size 일관) 확인되지만, Hana template 변경 시 historical fetch 일괄 broken risk → 운영 monitoring 필요 (Open question).
- bulk Excel/TXT endpoint: 단순 추정 URL 실패 + doExcelDown JavaScript 함수 본체 reverse engineering 미진행 → **미발견** (bulk endpoint 없음으로 단정 X). HTML fragment endpoint만으로 production 충분. bulk는 future optimization 보류.
- Investing은 Hana fallback에서 완전 제거 → graph 의미 일관성 (Hana 매매기준율 ≠ Investing 기준환율 source mix 차단).

**Amendment 2 — Decision 7 변경**: Bithumb/KRX insufficient_history 분기 → **external_historical 확정** (insufficient_history는 신규 자산/source 장애/coverage 부족 시점에 여전히 적용 — 완전 제거 X).

- **Bithumb**: 공식 candlestick API `api.bithumb.com/public/candlestick/USDT_KRW/{interval}` 채택. 902일 coverage (2023-12-07 KST 시작). 무인증. raw schema 비표준 **OCHL** 순서 `[ts_ms, open, close, high, low, volume]` — ccxt parse_ohlcv ([ccxt/bithumb.py:640-658](https://github.com/ccxt/ccxt/blob/master/python/ccxt/bithumb.py))로 잠금 + design 문서에 explicit warning 필수. rate limit ccxt 500ms/request (분당 120회) — 운영 사용 패턴 (startup backfill 1회 + daily refresh 1회) limit의 0.001%.
- **KRX**: KIS `inquire-daily-fuopchartprice` (TR_ID=FHKIF03020100) + `FID_COND_MRKT_DIV_CODE=CF` + **A75YMM contract chain** 채택. 만기 지난 월물도 rt_cd=0 정상 조회 (A75605/A75604/A75603/A75602/A75601 각 100/95/70/53/34 rows). 종목 코드 sequential (Y=년 1자리, MM=월 2자리). KIS master에 명시적 continuous front-month 코드 미발견 → chain 패턴 필수.
  - ⚠️ **Step 4B 후속 정정 (2026-06-04) — KIS year-series 한계 + source 전환**: 위 "만기 지난 월물 조회" 검증 사례(A75605~A75601)는 전부 **A756xx(Y=6=2026)**였고, **A755xx(Y=5=2025 만기 series)는 KIS 미조회**(`rt_cd=0`이나 `output2=[]` — Step 4B production dry-run 2026-06-04 확인). 시간 retention이 아니라 **year-series 한계**(A75512=2025-12도 빈 응답). 12개월 coverage가 KIS 단독 불가 → **KRX 공식 OPEN API `fut_bydd_trd`(선물 일별매매정보, 주식선물外) date-based로 source 전환** ([KRX_STEP4B_PLAN.md §0](KRX_STEP4B_PLAN.md)). KIS 대조 1:1 일치(2026-05-18=1496.50 등) + 만기일=next mapping 정합 + 2025 구간 조회 실증(샘플 3날). **KIS는 daily backfill에서 fallback/verification으로 격하** (realtime broadcast는 유지).
- **KRX endpoint 호출 형식**: 표준 GET (`GET /uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice` + headers `appkey/appsecret/authorization/tr_id` + query params).
- **KRX chain round trip**: contract 수는 rollover boundary 정책에 따라 변동. Monthly front-month chain (boundary=만기일 07:00 KST user-facing swap point)이면 1y는 **최대 12 contracts** (월별 만기 × 12개월). 각 contract 사용 구간만 좁게 fetch하면 보통 100건 cap 안. **단일 contract listing 전체를 wide range로 조회하면 cap에 걸림** (5/27 실측: A75605 listing 전체 범위 = 정확히 100 rows cap 도달, A75606 1y range 동일 cap 도달). chain 전략은 cap 자연 회피, 단일 contract wide range만 date range split 필요.
- **KRX provenance**: response 각 point에 `contract_code` 포함 권고 (client tooltip 등 선택적 표시).
- **Rollover boundary — 채택: 만기일 07:00 KST user-facing swap point** (현재 운영 정책 일관, [app/sources/kis_master.py:209+](app/sources/kis_master.py) `select_active_usd_futures_contract`, PR6c-2d-1 2026-05-07).
  - **Intraday 운영 기준**: expiry_date 07:00 KST 이전 = expiring contract, 07:00 이후 = next contract
  - **Daily historical graph 기준** (date-to-contract mapping):
    - expiry_date 이전 날짜 → expiring contract
    - expiry_date 당일 및 이후 날짜 → next contract (07:00 swap 일관)
    - expiring contract의 expiry_date row는 거래소 원월물 이력에는 존재하지만 v2 user-facing graph에서는 제외
  - 만기일 정규장 (08:30~15:45) 거래는 user-facing 노출 X (이미 swap 후)
  - 각 point provenance에 `contract_code` 저장
  - 대안 거부: 거래소 만기일 정산가 boundary (user-facing 운영과 충돌, 단말 vs 그래프 불일치 risk) / 만기 전일 boundary (1일 차이) / Trading volume (구현 복잡 + 과거 재현성 ↓)
  - 근거: B-B 검증 실측 — KIS daily endpoint가 A75606 20260518 close=1496.5 반환 (5/18 만기 후 next contract user-facing 전환됨)

**Amendment 3 — Decision 10 보강**: daily rollup 우선순위 하향 (장기 coverage 확보 목적만).

- source_rates 30d cap 정책 (`cleanup_old_source_rates` cron) **유지** — 운영 정책 변경 X.
- 3 source 모두 external_historical 확보 → **장기 coverage 확보 목적의 daily rollup 우선순위 ↓** (Phase 2c external_historical로 대체).
- **단, daily rollup 자체는 future optimization 후보 보존**: 내부 cache / materialization / 외부 source 일시 장애 대비 / 응답 latency 개선 목적의 rollup은 별도 가치. retention 결정은 그대로 별도 ADR (필요 시점 storage 산정 후).

**Phase 2c 검증 cross-reference**:

- B-A Bithumb 공식 docs: ccxt parse_ohlcv field swap (index 1→1, 3→2, 4→3, 2→4) + rateLimit 500ms + interval 13개 (1m/3m/5m/10m/15m/30m/1h/4h/6h/12h/24h/1w/1mm)
- B-B KRX KIS chain: 만기 지난 월물 (A75605~A75601) daily endpoint rt_cd=0 + A75YMM sequential + 100건 cap (wide range 조건) + continuous code 미발견
- B-C Hana endpoint: 20년+ historical + 휴일 자동 fallback + HTML fragment 매매기준율 인덱스 7 + 회차 정책 추가 필요

**후속 Phase 변경 (Phase 2c → Phase 2c 검증 완료)**:

- ~~Phase 2c~~ (외부 historical source 조사) → ✅ **완료 (본 Amendment)**
- Phase 2d daily rollup 구현 → **장기 coverage 확보 목적 기준 우선순위 ↓ (future optimization)** + retention 별도 ADR. [Amendment 후속에서 hot path 안정화 목적의 source_daily_rates는 우선순위 ↑로 정정 — 두 목적 분리]
- Phase 2e v2 endpoint 구현 → catalog + tab graph + historical scrape job 통합. **Phase 2e 진입 전 확정 결정 (2026-05-27)**: KRX rollover = 만기일 07:00 KST user-facing swap point 채택 (Amendment 2) + Hana representative row = `pbldSqn=` 빈값 기본 일일 historical row 채택 (Amendment 1). **남은 Phase 2e 전 open question**: KRX/Hana 1w bucket 정책 (제품 UX 결정 영역 — KRX 1w 후보 a/b/c: source_rates 30일분 1시간 bucket / KIS intraday endpoint 조사 / daily 다운그레이드. Hana 1w 후보 a/b/c: bank_exchange_rates 30일분 1시간 bucket / daily 다운그레이드 / v2 catalog 제외)
- Phase 2f (Later) single series endpoint — 변경 X

### Amendment 후속 — source_daily_rates canonical table 도입 (★ v2 데이터 모델 결정)

Phase 2e 진입 전 결정 (KRX 07:00 rollover + Hana pbldSqn) 후속 결정 영역으로 도출됨. 표면적으로는 표현 수정처럼 보이지만, 본질은 **v2 장기 그래프의 데이터 모델 결정**. 특히 GRAPH_API_V2_CONTRACT.md §7 (Hana official historical source rule)은 **부분 수정 아닌 정책 재작성**.

**핵심 통찰** (사용자 발견 + Codex 정확화):

- 5/26 USD Hana official endpoint = 1,507.50, 다음날 07:46:45 KST 발표 (다음날 새벽 고시 row)
- 우리 DB의 5/26 24:00 이전 last = 1,503.9 → Hana official과 의미 다름
- "Hana 사이트의 historical row"는 canonical date의 representative이지만 timing은 다음날 새벽
- 다른 source (Bithumb 24h candle close / KRX CF 15:45 정규 종가)와 비교 시 마감 시각 불일치
- → "**소스별 close 의미가 다르다**"는 사실을 숨기지 않고 provenance로 명시하는 게 honest

**핵심 원칙**:

> 외부 API는 backfill / gap repair / 검증용이고, 그래프 요청 hot path는 우리 daily canonical table만 읽는다.

#### Decision A — source_daily_rates canonical table 도입

- 신규 테이블 (Phase 2d 구현 영역): `source_daily_rates`
- 초기 backfill (1회): 각 source의 외부 historical API 호출 후 적재
- 매일 append (운영 중): 각 source 별 daily canonical row 1건 / day
- v2 3m/1y 그래프 hot path = `source_daily_rates` 단일 조회 (외부 API 호출 X)
- 외부 API는 backfill / gap repair / 검증용으로만 유지
- Phase 2d 설계 결정 항목: retention / unique key / rebuild policy 확정

**Schema 초안 (주요 필드)**:

| Field | Nullable | 의미 |
| --- | --- | --- |
| `source` | No | "hana" / "krx" / "bithumb" |
| `asset` | No | "usd-krw" / "usdt-krw" / "usd-krw-futures" 등 |
| `date_kst` | No | canonical date (KST 기준) |
| `rate` | No | 대표값, 기본은 close와 동일 |
| `high` | Yes | source에 따라 없을 수 있음 |
| `low` | Yes | source에 따라 없을 수 있음 |
| `close` | No | daily representative close |
| `close_basis` | No | 의미 (Decision B 참조) |
| `source_method` | No | 획득 방식 (Decision B-bis 참조) |
| `contract_code` | Yes | KRX 전용 (예: A75606) |
| `basis_date` | Yes | Hana official backfill 응답 기준일 등 |
| `published_at` | Yes | Hana official 발표시각 등 (다음날 새벽) |
| `captured_at` | No | 우리 시스템 저장 시각 |
| `metadata` | Yes | pbldSqn, raw fields 등 추가 metadata json |

Unique key 후보: `(source, asset, date_kst)` — Phase 2d 설계 확정.

#### Decision B — close_basis provenance enum (★ source identity 명시)

response 각 series provenance에 `close_basis` field 추가. 9 가지 enum (Investing daily은 ADR-035 D1, Bithumb/Investing/Hana/KRX hourly는 ADR-035 D3 추가):

| `close_basis` | Source | 의미 |
| --- | --- | --- |
| `krx_cf_close_1545` | KRX | CF 정규장 15:45 KST close finalizer |
| `bithumb_24h_kst_close` | Bithumb | 24h candle KST 00:00 boundary close |
| `hana_observed_eod` | Hana (canonical) | 우리 DB에서 KST 해당일 24:00 이전 마지막으로 관측한 Hana 고시값 |
| `hana_official_historical_backfill` | Hana (과거 부족분만) | Hana 사이트 historical row (다음날 새벽 고시) |
| `investing_observed_eod` | Investing (canonical) | 우리 DB(`investing_exchange_rates` 장기 보관)에서 KST 해당일 마지막 관측 기준 환율 (ADR-035 D1, Proposed) |
| `bithumb_observed_hourly` | Bithumb (1w hourly) | `source_rates` raw tick → KST 1h bucket 마지막 관측 close (ADR-035 D3, **source_hourly_rates** — 24h candle close와 다른 granularity. 1w 그래프 전용) |
| `investing_observed_hourly` | Investing (1w hourly) | `investing_exchange_rates` raw 관측 → KST 1h bucket 마지막 관측 close (ADR-035 D3, **source_hourly_rates** — daily `investing_observed_eod`와 다른 granularity. 1w 그래프 전용, per-currency usd/jpy/eur) |
| `hana_observed_hourly` | Hana (1w hourly) | `bank_exchange_rates`(bank=hana) 고시 관측 → KST 1h bucket 마지막 관측 close (ADR-035 D3, **source_hourly_rates** — daily `hana_observed_eod`와 다른 granularity. 1w 그래프 전용, per-currency usd/jpy/eur. ohlc_quality는 multi-tick observed_rollup / single-tick close_only) |
| `krx_observed_hourly` | KRX (1w hourly) | `source_rates`(krx/usd-krw-futures) raw tick → KST 1h bucket 마지막 관측 close (ADR-035 D3, **source_hourly_rates** — daily `krx_cf_close_1545`와 다른 granularity. 1w 그래프 전용. **session-agnostic** — CF/CM 공통 enum, 첫 PR은 CF 정규장 08:30~15:45 coverage. contract_code는 source_daily_rates daily row에서 재사용) **[재설계 2026-06-10: 세션 무관 CF/CM 통합 rollup + contract_code 미저장 — 구 CF-only/contract 재사용 서술 supersede, ADR-035 D3 구현 상태 단락 참조]** |

같은 series 안에서 구간별로 다른 close_basis인 경우 per-point metadata로 표시 (특히 Hana의 backfill vs canonical 경계).

> **granularity 주의**: `bithumb_24h_kst_close`(daily, source_daily_rates)와 `bithumb_observed_hourly`(hourly, source_hourly_rates)는 같은 Bithumb이지만 close 기준이 다르다 (24h candle close vs 시간별 마지막 관측 tick). 3m/1y는 전자, 1w는 후자.

#### Decision B-bis — close_basis vs source_method 직교 분리

`close_basis` (의미)와 `source_method` (수집 방법)는 직교 개념. 두 field 모두 필수.

**source_method enum 6 values**:

- `observed_rollup`
- `external_backfill`
- `close_finalizer`
- `bithumb_candlestick_api` (Amendment 2026-06-01 — 구 `bithumb_candlestick_backfill`)
- `kis_daily_backfill` (Step 4B에서 KIS year-series 한계로 superseded — [§0 KRX 전환](KRX_STEP4B_PLAN.md))
- `krx_openapi_daily` (Step 4B — KRX OPEN API `fut_bydd_trd` date-based)

**close_basis × source_method 매핑**:

| close_basis | source_method | 시점/의미 |
| --- | --- | --- |
| `hana_observed_eod` | `observed_rollup` | 운영 중 매일 append |
| `hana_official_historical_backfill` | `external_backfill` | 초기 부족분 backfill |
| `krx_cf_close_1545` | `close_finalizer` | 운영 중 매일 append (CF close finalizer) |
| `krx_cf_close_1545` | `krx_openapi_daily` | 초기 backfill (KRX OPEN API `fut_bydd_trd` date-based, Step 4B 전환 — 기존 `kis_daily_backfill` 25 rows transitional) |
| `bithumb_24h_kst_close` | `bithumb_candlestick_api` | 초기 backfill + 운영 중 매일 append (동일 candle API — Amendment 2026-06-01) |
| `investing_observed_eod` | `observed_rollup` | `investing_exchange_rates`(장기 raw) daily rollup — backfill + going-forward (ADR-035 D1, Proposed) |
| `bithumb_observed_hourly` | `observed_rollup` | `source_rates`(raw tick) → KST 1h bucket rollup (1w hourly, ADR-035 D3 — daily `bithumb_24h_kst_close`와 별 granularity) |
| `investing_observed_hourly` | `observed_rollup` | `investing_exchange_rates`(raw 관측) → KST 1h bucket rollup (1w hourly, ADR-035 D3 — daily `investing_observed_eod`와 별 granularity, per-currency) |
| `hana_observed_hourly` | `observed_rollup` | `bank_exchange_rates`(bank=hana 고시) → KST 1h bucket rollup (1w hourly, ADR-035 D3 — daily `hana_observed_eod`와 별 granularity, per-currency. ohlc_quality은 single-tick hour만 close_only) |
| `krx_observed_hourly` | `observed_rollup` | `source_rates`(krx/usd-krw-futures) → KST 1h bucket rollup (1w hourly, ADR-035 D3 — daily `krx_cf_close_1545`와 별 granularity. session-agnostic enum, 첫 PR CF 정규장 coverage. contract_code는 source_daily_rates daily row 재사용) **[재설계 2026-06-10: 세션 무관 CF/CM 통합 rollup + contract_code 미저장 — 구 CF-only/contract 재사용 서술 supersede, ADR-035 D3 구현 상태 단락 참조]** |

→ **Bithumb은 backfill·append 동일 방법**(`bithumb_candlestick_api` — Amendment 2026-06-01, close_basis·source_method **모두 단일**). KRX는 close_basis 동일 + source_method 분기 (backfill=`krx_openapi_daily` [Step 4B 전환, 기존 `kis_daily_backfill` transitional] vs append=`close_finalizer`). Hana는 close_basis + source_method 둘 다 분리. provenance 측면 backfill 구간과 운영 구간 명확 식별 가능.

#### Decision C — Source별 daily canonical 정책

| Source | 초기 Backfill | 앞으로 쌓는 값 (canonical) | close_basis (canonical) |
| --- | --- | --- | --- |
| **Bithumb** | 공식 24h candle API | ~~DB tick/source_rates → KST daily rollup~~ → **공식 24h candle API daily refresh** (Amendment 2026-06-01) | `bithumb_24h_kst_close` |
| **Hana** | 부족한 과거만 official historical | DB의 KST 24:00 이전 마지막 관측값 | `hana_observed_eod` |
| **KRX** | KIS daily + A75YMM chain | close finalizer CF 15:45 정규 종가 | `krx_cf_close_1545` |

**Investing은 Hana backfill source에 미사용** (Decision 6 일관 — Hana series identity 유지). Investing은 별 Investing series로만 노출.

**KRX 24:00 통일 거부 이유**:

- 24:00 야간장 (CM session) 진행 중간 관측값은 "close" 의미 부정확
- KIS daily backfill (CF 정규 종가)과 의미 불일치 → 과거/미래 섞임
- 대신 `close_basis=krx_cf_close_1545` provenance로 마감 시각 차이 명시 (숨기지 않는 방식)
- CM 06:00 야간 종가는 별 session close로 보존, 기본 3m/1y daily close에는 미사용

#### Decision D — Phase 2d 우선순위 표현 정정 (Amendment 3 보강)

기존 Amendment 3 ("daily rollup 우선순위 ↓") 표현을 두 목적으로 분리:

- **장기 coverage 확보 목적의 rollup**: 외부 historical source 확보됨 → 우선순위 **↓** (Amendment 2026-05-27 그대로)
- **Hot path 안정화 / canonical daily table 목적의 source_daily_rates**: 외부 API hot path 제거 + 매일 append 통합 → 우선순위 **↑** (이번 후속 amendment 신규)

두 목적은 서로 다른 가치. 한 묶음으로 Phase 2d 우선순위 ↓ 표현은 부정확 — 분리 명시.

#### Decision E — §7 Hana 정책 재작성 (★ 핵심 변경)

기존 §7 표현 ("Hana official historical endpoint 단일 source")은 **폐기**. 새 정책 재작성:

```text
Hana daily canonical:
  앞으로 쌓이는 구간 (DB observed_eod 산출 가능 구간): hana_observed_eod
  과거 부족분 (DB raw 관측값 없음): hana_official_historical_backfill
  두 구간이 섞이는 경계는 source_method provenance로 표시
  Investing은 Hana backfill source에 미사용 (Hana series identity 유지)
```

§7은 부분 수정 X. 전체 정책 재작성 (위 4-line anchor + parser 기준 + 회차 정책 + canonical date 정책 유지).

#### Phase 2c → Phase 2d → Phase 2e 흐름 (정정)

- Phase 2c: external_historical source 확보 (완료, Amendment 2026-05-27)
- **Phase 2d (우선순위 ↑ for hot path 목적)**: source_daily_rates canonical table 구현 — **상세 설계는 [ADR-034](#adr-034-source_daily_rates-canonical-daily-table)** (schema + retention + unique key + rebuild policy + provenance + backfill/append jobs + timezone/date ownership + calendar-aware monitoring + rate==close invariant + rollout sequence)
- Phase 2e: v2 endpoint 구현 — `source_daily_rates` 단일 조회 hot path

### 관련 문서

- [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md): catalog matrix / endpoint contract / provenance schema (본 ADR의 design 문서)
- [ADR-019](#adr-019-dxy-보조지표--granularity-기반-2-part-merge-전략): DXY granularity 2-part merge — Hana backfill 패턴 참조
- [ADR-023](#adr-023-데이터-보관-정책-30일-통일-banksource_ratesdxy-realtime): 30일 cap 정책 anchor
- [REALTIME_ARCHITECTURE_PLAN.md](REALTIME_ARCHITECTURE_PLAN.md): 서비스 계약 source of truth (topic-only / dual-emit)

---

## ADR-034: source_daily_rates canonical daily table

**상태**: Proposed (2026-05-28)

- v2 장기 그래프 (3m/1y) hot path가 읽을 **daily canonical table** 정의 ADR.
- [ADR-033](#adr-033-graph-api-v2-catalog-policy--legacy-공존--hana-backfill--bithumbkrx-actual-only--dxy_futures-1d-only) Amendment 후속 결정 (`source_daily_rates` 도입 + close_basis/source_method provenance)의 구현·저장소·운영 정책 분리.

### 1. Context

[ADR-033](#adr-033-graph-api-v2-catalog-policy--legacy-공존--hana-backfill--bithumbkrx-actual-only--dxy_futures-1d-only) Amendment 2026-05-27 후속 결정에서 v2 장기 그래프 데이터 모델이 외부 API hot path 제거 + canonical daily table 단일 조회로 합의됨. ADR-033은 endpoint contract 수준이고, source_daily_rates는 storage/operational 수준이라 **별 ADR로 분리**.

핵심 원칙:

> 외부 API는 backfill / gap repair / 검증용이고, 그래프 요청 hot path는 우리 daily canonical table만 읽는다.

### 2. Decision

**Accepted** (이미 합의):

1. `source_daily_rates` canonical table 도입 — v2 장기 그래프 hot path 단일 조회
2. Graph hot path에서 외부 API 직접 호출 금지 (Bithumb / KIS / Hana official 모두 backfill / gap repair / 검증용)
3. `close_basis` enum 9 values + `source_method` enum 6 values + `ohlc_quality` enum 3 values **직교 분리** (ADR-033 Amendment 후속 + 본 ADR Decision B + Step 4B KRX 전환 + ADR-035 D1/D3 참조)
4. Unique key 후보: `(source, asset, date_kst)`
5. **`ohlc_quality` top-level column** (검색/필터/렌더링 판단 직접 사용)
6. **`rate == close` app-level invariant** (모든 backfill/append job에서 같은 값으로 write)

**Proposed** (본 ADR 권고):

7. Retention: 무기한 또는 최소 5년 (storage 비용 산정 후 확정)
8. Conflict policy: idempotent upsert
9. Rebuild policy: source / date range 단위 idempotent rebuild
10. Monitoring: calendar-aware daily row missing alert + source_method별 실패 로그 + gap detection + rate != close drift alert
11. metadata_json upsert: incoming non-null이면 replace (shallow merge 거부)

**Open** (추가 검증/결정 필요):

12. Bithumb 24h candle date_kst 매핑: candle start vs close/end 기준 — 공식 docs 추가 검증
13. Retention 최종 기간: 무기한 vs 10년 vs 5년 — storage 비용 산정 후 결정
15. Rebuild command interface: CLI vs admin endpoint vs scheduled cron with manual trigger
18. `ohlc_quality` enum 명칭 표준화: 잠정 `source_ohlc` / `observed_rollup` / `close_only` — Phase 2d 안정화 후 확정

**Phase 2d Step 1 결정 (2026-05-28 Accepted 전환)**:

14. ✅ **Exact indexes**: initially **UNIQUE (source, asset, date_kst)만 시작**. 추가 index는 monitoring/query 패턴 실측 후 추가 (premature optimization 회피).
16. ✅ **Numeric precision = Numeric(14, 6)** 확정 — 환율/선물/USDT/DXY index 모두 충분 (정수부 8자리 / 소수부 6자리). 향후 자산 확장에도 여유. Read path는 Decimal 반환 → helper에서 float() 변환 정책 적용.
17. ✅ **DB CHECK constraint (`rate == close`) 보류** — app-level invariant + monitoring drift alert로 시작. 데이터 안정 후 CHECK 추가는 future amendment 가능 (반대로 처음 적용 후 제거는 reversible 비용 큼).

### 3. Schema

**SQLAlchemy ORM model 형식** (Phase 2d 구현 시):

```python
class SourceDailyRate(Base):
    __tablename__ = "source_daily_rates"
    id = Column(Integer, primary_key=True)
    source = Column(String, nullable=False)
    asset = Column(String, nullable=False)
    date_kst = Column(Date, nullable=False)
    # invariant: rate == close (app-level enforce, §10)
    rate = Column(Numeric(14, 6), nullable=False)
    high = Column(Numeric(14, 6), nullable=True)
    low = Column(Numeric(14, 6), nullable=True)
    close = Column(Numeric(14, 6), nullable=False)
    ohlc_quality = Column(String, nullable=False)         # source_ohlc / observed_rollup / close_only
    close_basis = Column(String, nullable=False)
    source_method = Column(String, nullable=False)
    contract_code = Column(String, nullable=True)
    basis_date = Column(Date, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    metadata_json = Column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint('source', 'asset', 'date_kst'),)
```

**주요 필드**:

| Field | Nullable | 의미 |
| --- | --- | --- |
| `source` | No | "hana" / "krx" / "bithumb" |
| `asset` | No | "usd-krw" / "usdt-krw" / "usd-krw-futures" 등 |
| `date_kst` | No | canonical date (KST 기준, §12 참조) |
| `rate` | No | 대표값. **invariant: rate == close** (§10). legacy 호환 + 단일값 consumer 용 |
| `high` | Yes | source에 따라 없을 수 있음 (close_only 시 = close) |
| `low` | Yes | source에 따라 없을 수 있음 (close_only 시 = close) |
| `close` | No | daily representative close (v2 그래프 consumer 사용) |
| `ohlc_quality` | No | OHLC 품질 — `source_ohlc` / `observed_rollup` / `close_only` (§7) |
| `close_basis` | No | 9 values (§6) |
| `source_method` | No | 6 values (§6) |
| `contract_code` | Yes | KRX 전용 (예: A75606) |
| `basis_date` | Yes | Hana official endpoint 응답 기준일 |
| `published_at` | Yes | Hana official 발표시각 (다음날 새벽) |
| `captured_at` | No | 우리 시스템 저장 시각 |
| `metadata_json` | Yes | pbldSqn / raw response 일부 / diagnostics 등 보조 정보 only |

### 4. Unique key / indexes

**Accepted**:

- Unique key: `(source, asset, date_kst)` — 하루 1 row per (source, asset)

**Proposed**:

- Index 1: `(source, asset, date_kst)` — unique key (range query 자연 cover)
- Index 2: `(source, date_kst)` — source별 daily aggregation (운영 monitoring 용)

**Open**:

- 추가 index 필요 항목 — query 패턴 실측 후 결정 (Phase 2d 구현 시)

### 5. Retention

**Accepted**:

- 그래프용 source_daily_rates는 **장기 보존** 정책 (source_rates 30d cap과 무관)

**Proposed**:

- 기본: 무기한 보존 또는 최소 5년+
- storage 부담은 매우 작음; 5~10년 보존도 RDS 관점에서 negligible
- 무기한 보존이 가장 단순 + storage 비용 미미

**Open**:

- 최종 기간 결정: 무기한 vs 10년 vs 5년 — Phase 2d 진입 시점 storage 비용 산정 후 확정

### 6. close_basis / source_method / ohlc_quality provenance

**3 column 직교 관계 anchor**:

- `close_basis` = **무엇이 close인지** (의미)
- `source_method` = **어떻게 얻었는지** (수집 방법)
- `ohlc_quality` = **OHLC 신뢰도** (품질)

**`close_basis` enum 9 values** (ADR-033 Amendment 후속 Decision B 참조, Investing daily은 ADR-035 D1, Bithumb/Investing/Hana/KRX hourly는 ADR-035 D3):

- `krx_cf_close_1545`: KRX CF 정규장 15:45 KST close finalizer
- `bithumb_24h_kst_close`: Bithumb 24h candle KST 00:00 boundary close (daily, source_daily_rates)
- `hana_observed_eod`: 우리 DB에서 KST 해당일 24:00 이전 마지막으로 관측한 Hana 고시값
- `hana_official_historical_backfill`: Hana 사이트 historical row (다음날 새벽 고시, 과거 부족분 보강용)
- `investing_observed_eod`: 우리 DB(`investing_exchange_rates` 장기 보관)에서 KST 해당일 마지막 관측 기준 환율 (ADR-035 D1, Proposed)
- `bithumb_observed_hourly`: `source_rates` raw tick → KST 1h bucket 마지막 관측 close (ADR-035 D3, source_hourly_rates — 1w 그래프 전용, daily `bithumb_24h_kst_close`와 별 granularity)
- `investing_observed_hourly`: `investing_exchange_rates` raw 관측 → KST 1h bucket 마지막 관측 close (ADR-035 D3, source_hourly_rates — 1w 그래프 전용, per-currency usd/jpy/eur, daily `investing_observed_eod`와 별 granularity)
- `hana_observed_hourly`: `bank_exchange_rates`(bank=hana) 고시 관측 → KST 1h bucket 마지막 관측 close (ADR-035 D3, source_hourly_rates — 1w 그래프 전용, per-currency usd/jpy/eur, ohlc_quality observed_rollup/close_only 분기, daily `hana_observed_eod`와 별 granularity)
- `krx_observed_hourly`: `source_rates`(krx/usd-krw-futures) raw tick → KST 1h bucket 마지막 관측 close (ADR-035 D3, source_hourly_rates — 1w 그래프 전용, daily `krx_cf_close_1545`와 별 granularity. **session-agnostic** — CF/CM 공통 enum, 첫 PR은 CF 정규장 08:30~15:45 coverage. contract_code는 source_daily_rates daily row 재사용) **[재설계 2026-06-10: 세션 무관 CF/CM 통합 rollup + contract_code 미저장 — 구 CF-only/contract 재사용 서술 supersede, ADR-035 D3 구현 상태 단락 참조]**

**`source_method` enum 6 values** (ADR-033 Amendment 후속 Decision B-bis + Step 4B KRX 전환):

- `observed_rollup`: DB 관측 기반 daily rollup (source_rates / bank_exchange_rates / investing_exchange_rates)
- `external_backfill`: 외부 API에서 초기 부족분 backfill (Hana official endpoint)
- `close_finalizer`: KRX CF close finalizer 결과
- `bithumb_candlestick_api`: Bithumb 공식 24h candle API (backfill + daily refresh append **동일 방법** — Amendment 2026-06-01, 구 `bithumb_candlestick_backfill` rename. source_method=획득 방법이므로 backfill/daily 구분은 timing이지 방법 아님)
- `kis_daily_backfill`: KIS daily endpoint + A75YMM chain 초기 backfill (**Step 4B에서 KIS year-series 한계로 superseded** — KRX OpenAPI 전환, [KRX_STEP4B_PLAN.md §0](KRX_STEP4B_PLAN.md))
- `krx_openapi_daily`: **KRX 공식 OPEN API `fut_bydd_trd`(선물 일별매매정보, 주식선물外) date-based 적재** (Step 4B source 전환, 정규장 종가 `TDD_CLSPRC`. KIS와 동일 KRX 원천이나 획득 방법 분리). 기존 `kis_daily_backfill` 25 rows와는 **transitional match**(값/contract/close_basis/ohlc_quality/metadata 전부 일치 시 source_method 차이만 허용 + warning/count surface) — dry-run 전수 일치 확인 후 별도 migration GO를 거치도록 설계(당시 적용한 안전 절차). **→ migration 완료 (2026-06-04): 25 rows 전부 `krx_openapi_daily` (§8 land anchor 참조).**

**`ohlc_quality` enum 3 values** (잠정 명칭, Open):

- `source_ohlc`: source 자체 OHLC 직접 사용 (Bithumb backfill, KRX backfill)
- `observed_rollup`: 관측값 rollup 계산 (daily append jobs)
- `close_only`: representative row만 — high=low=close synthetic (Hana official backfill)

### 7. Source-specific population policy

| Source | 초기 Backfill | 매일 append (canonical) | close_basis | source_method 매핑 |
| --- | --- | --- | --- | --- |
| Bithumb | 공식 24h candle API | **공식 24h candle API daily refresh** (Amendment 2026-06-01 — DB rollup 폐기, candle이 실 OHLC 제공해 우월) | `bithumb_24h_kst_close` | backfill=append=`bithumb_candlestick_api` (단일 방법) |
| Hana | 부족한 과거만 official historical | DB의 KST 24:00 이전 마지막 관측값 | backfill=`hana_official_historical_backfill`, canonical=`hana_observed_eod` | backfill=`external_backfill`, append=`observed_rollup` |
| KRX | **KRX OPEN API `fut_bydd_trd` date-based** (Step 4B 전환 — 기존 KIS daily+A75YMM superseded) | close finalizer CF 15:45 정규 종가 | `krx_cf_close_1545` | backfill=`krx_openapi_daily`, append=`close_finalizer` |

**high/low population policy (★ Phase 2d 구현 input)**:

**Accepted**:

- raw daily OHLC가 있는 source: source 값 그대로 사용
  - Bithumb backfill: 24h candle high/low → `ohlc_quality="source_ohlc"`
  - KRX backfill: KIS daily `futs_hgpr` / `futs_lwpr` → `ohlc_quality="source_ohlc"`
- representative close만 있는 backfill: `high=low=close` + `ohlc_quality="close_only"`
  - Hana official backfill (매매기준율 1개 값)
- close_only synthetic 처리 시 품질 플래그 (`ohlc_quality`) 명시 필수

**Proposed** (Phase 2d 구현 결정):

- raw observations rollup으로 high/low 계산 시 → `ohlc_quality="observed_rollup"`
  - ~~Bithumb daily append: source_rates KST 일자 rollup~~ (Amendment 2026-06-01 폐기 — Bithumb daily refresh는 공식 candle API라 `ohlc_quality="source_ohlc"`, observed_rollup 미해당)
  - Hana observed_eod: bank_exchange_rates KST 일자 rollup
  - KRX daily append: source_rates CF session (08:30~15:45 KST) rollup

**Hana = mixed series** (close_basis 구간별 다름):

- 앞으로 쌓는 구간: `hana_observed_eod` (close_basis) + `observed_rollup` (source_method) + `observed_rollup` ohlc_quality (또는 `close_only` fallback, Phase 2d 결정). **`source_ohlc` 불가능** — Hana는 source 자체에 OHLC 없음 (bank_exchange_rates 단일 rate값 시계열)
- 과거 부족분: `hana_official_historical_backfill` + `external_backfill` + `close_only`

**KRX/Bithumb = single close_basis series** (backfill/append 구간 모두 동일 close_basis, source_method/ohlc_quality만 분기)

### 8. Backfill jobs

**Proposed** (Phase 2d 구현 시):

- **Bithumb backfill**: `api.bithumb.com/public/candlestick/USDT_KRW/24h` 호출 → 902일 일괄 적재
- **KRX backfill**: ~~KIS `inquire-daily-fuopchartprice` + A75YMM contract chain~~ → **Step 4B(2026-06-04)에서 KRX 공식 OPEN API `fut_bydd_trd` date-based로 전환** (KIS year-series 한계 — A755xx(2025) 미조회. [KRX_STEP4B_PLAN.md §0](KRX_STEP4B_PLAN.md) + ADR-033 Amendment 2 Step 4B 정정). Date-to-contract mapping(만기일=next)은 유지 (`build_contract_sequence`/`_resolve_front_month` 재사용 + KRX `contract_month` join). KIS는 verification/fallback. **전환 land 완료 (2026-06-04)**: 구현 8 단위(`c2caa50`..`1103ecf` 구간 [feat 8 + enum docs `c38196b` = 9 commits] — parser→변환→compare transitional→manifest builder[weekday loop + BAS_DD==요청일 stale 가드]→range dry-run→HTTP helper→main CLI→migration writer) + 운영 dry-run GO(scoped `[2026-04-20, 2026-05-27]`, TRANSITIONAL=25 / COMPARE_HARD=0 / PASS_WITH_TRANSITIONAL) + production migration(snapshot `fxi-pre-krx-source-method-2026-06-04`, 25 rows `kis_daily_backfill`→`krx_openapi_daily` in-place **source_method only** surgical relabel, drift 0, boundary 2026-05-18=`1496.500000`/A75606 보존, Claude+Codex 이중 독립 검증). source_daily_rates KRX 25 rows 전부 `krx_openapi_daily`. migration writer = `scripts/migrate_krx_source_method.py` (DB-only, scoped window, `migrate_bithumb_source_method` 패턴). **Full-year backfill land 완료 (2026-06-04 동일)**: gap-only write 경로(`write_krx_gap_rows` insert-only + 입력 pre-check + surgical post-verify, `--write` CLI wiring[fresh full flow → 가드 → write-core], `8d79da4`/`087edc0`) → full-year range dry-run(scoped `[2025-06-03, 2026-06-03]`, MANIFEST 243 / MISSING 19 surface(휴장일 추정·gate 비차단) / EXISTING 25 / ROWS_TO_WRITE 218 / MATCHED 25 / TRANSITIONAL 0 / COMPARE_HARD 0 / PASS) → RDS snapshot `fxi-pre-krx-fullyear-backfill-2026-06-04` → production write(218 gap rows insert, total 25→243, 기존 25 불변) → post-verify(post-write dry-run ROWS_TO_WRITE 0 / MATCHED 243, by_method `{krx_openapi_daily:243}`, **2025 142행**[KIS year-series 미조회 구간 — 전환 목적 달성], 13 contracts A75506~A75606, rate≠close/OHLC/dup drift 0, Claude+Codex 이중 독립 검증). **KRX 1y coverage 달성(243 rows, 2025+2026)** — Hana 1년 backfill 수준과 정렬, Bithumb 1y와 함께 장기 그래프 입력 균형.
- **Hana backfill**: Hana official endpoint historical row → 부족한 과거 구간만
- 모든 backfill = **idempotent** (재실행 시 같은 결과, §10 참조)
- Initial backfill 1회 + Gap repair 호출 시 사용

**Open**:

- Backfill 진행 monitoring (progress / completion log)
- Rate limit 처리 (Bithumb 500ms/request / KIS 100건 cap / Hana 분당 30회 보수)

### 9. Daily append jobs

**Proposed** (Phase 2d 구현 시):

- **Bithumb daily append**: KST 00:01 cron이 **공식 24h candle API로 전일 candle fetch → upsert** (Amendment 2026-06-01 — source_rates rollup 폐기, candlestick API 실 OHLC 우월. source_method=`bithumb_candlestick_api`, ohlc_quality=`source_ohlc`)
- **Hana daily append**: KST 00:01 ~ 00:10 사이 bank_exchange_rates에서 전일 23:XX 마지막 Hana row 추출 → upsert
- **KRX daily append**: CF close finalizer 15:46:00 직후 결과를 source_daily_rates에 직접 write
- 모든 daily append = **idempotent upsert** (재실행 시 덮어쓰기, rate == close 강제)

**Open**:

- Daily append 실패 시 retry 정책
- KRX close finalizer write 실패 시 fallback (KIS daily endpoint로 백업 fetch)

### 10. Conflict / upsert policy + rate == close invariant

**Conflict policy**:

- 같은 `(source, asset, date_kst)` 재계산 시 **idempotent upsert** (`ON CONFLICT DO UPDATE`)
- Update field 정책:
  - Always update: `rate`, `high`, `low`, `close`, `ohlc_quality`, `close_basis`, `source_method`, `captured_at`
  - **Nullable fields**: `published_at`, `basis_date`, `contract_code`은 incoming value non-null일 때만 update (null로 기존 값 덮지 않음). SQL pattern: `COALESCE(EXCLUDED.field, source_daily_rates.field)`
  - `metadata_json`: incoming non-null이면 **replace** (shallow merge 거부 — source별 metadata 구조 다르고 stale key 혼동 risk)
  - Preserve: `id`

**rate == close invariant (★ app-level enforce)**:

- 기본적으로 `rate == close` (모든 row)
- v2 그래프 consumer = close / high / low 사용
- Legacy 또는 단일값 consumer = rate 사용
- Phase 2d backfill / daily append jobs: rate와 close를 항상 같은 값으로 write
- DB CHECK constraint (`CHECK (rate = close)`) — 잠정 보류, Phase 2d 결정 (future flexibility 보존)
- §13 Monitoring에서 `rate != close` row drift alert

**Open**:

- close_basis 변경 시 (예: backfill → canonical 전환) policy: 덮어쓰기 vs audit log — Phase 2d 구현 시 결정

### 11. Rebuild policy

**Proposed**:

- Source / date range 단위 idempotent rebuild
- 예: `rebuild source=hana asset=usd-krw start=2024-01-01 end=2024-12-31`
- Backfill + Daily append 같은 entry point 재호출 → upsert로 자연 적용

**Open**:

- Rebuild command interface: CLI script (`scripts/rebuild_source_daily_rates.py`) vs admin endpoint vs scheduled cron with manual trigger
- Rebuild 진행 monitoring + completion alert

### 12. Timezone / date ownership

**Accepted**:

- `date_kst` = canonical KST date (server 시점 무관, source의 KST 매핑 기준)

**Proposed** — source별 date_kst 매핑 정책:

- **KRX**: CF 15:45 close가 일어난 KST date = 당일 date_kst (예: 2026-05-27 15:45 close → date_kst=2026-05-27)
- **Hana**:
  - `hana_observed_eod`: KST 24:00 이전 마지막 관측 시각의 date = 당일 date_kst (예: 2026-05-27 23:58 last observation → date_kst=2026-05-27)
  - `hana_official_historical_backfill`: 응답의 `basis_date`를 date_kst로 사용 (다음날 새벽 고시 row의 `기준일` field, 예: 2026-05-27 03:30 발표 row의 basis_date=2026-05-26 → date_kst=2026-05-26)
- **Bithumb**: 24h candle의 timestamp 기준 — **Open question** (candle start 기준 vs close/end 기준 미확정. Bithumb 공식 docs 추가 검증 필요. 5/27 검증 시 `1701874800000` = 2023-12-07 00:00:00 KST 첫 candle = candle start 추정이지만 확정 검증 필요)

**Open**:

- Bithumb candle date ownership 확정 검증 (candle start vs close/end)

### 13. Monitoring / repair

**Proposed**:

- **Daily row missing alert** (★ calendar-aware): 매일 KST 01:00 시점에 어제 date_kst의 row 존재 체크. **append source만 대상** (backfill source는 별 track). source별 expected calendar 사용 (false positive 차단):
  - KRX: 한국 영업일 (KRX 휴장일 제외)
  - Bithumb: 365일 (crypto 24/7)
  - Hana observed_eod: 한국 은행 영업일
- **Historical gap detection / rebuild** (backfill source 대상, daily missing alert와 분리):
  - Hana official_historical_backfill: historical coverage 측정 + gap detection → rebuild command
  - Bithumb candle backfill / KIS daily chain backfill: 동일 패턴 (historical gap detection / rebuild trigger)
- **source_method별 실패 로그**: backfill/append job 실패 시 source + source_method + error message 로그
- **Gap detection**: source_daily_rates의 date_kst 연속성 체크 (각 source calendar 기준). gap 발견 시 alert
- **Rate != close drift alert** (★ invariant check): `rate != close` row 존재 검사. 발견 시 alert + 자동 audit log (write path 버그 catch)
- **Rebuild runbook**: 운영자가 gap 발견 시 rebuild command 실행 절차 documentation

**Open**:

- Alert delivery (Telegram / Slack / FCM)
- Gap detection 영업일 calendar (KRX 휴장일 etc.)
- 자동 rebuild vs 수동 rebuild 정책

### 14. Rollout sequence

**Proposed** — Phase 2d 구현 진입 순서:

1. **Schema 추가**: `source_daily_rates` table 마이그레이션 + ORM model. 운영 영향 — 코드 배포 시점에 `app/main.py:193`의 `create_all_app_tables(engine)`(실제 `Base.metadata.create_all`은 `app/database.py:65`, control-plane 테이블만 제외하며 `source_daily_rates`는 제외 대상이 아니다)이 신규 table을 자동 생성 가능 (lock 거의 없음 / ALTER 없음 / 빈 table 추가만). 별도 `scripts/migrate_source_daily_rates.py` (`__table__.create(checkfirst=True)` idempotent pattern)은 명시적 적용/검증/audit 용도 — 유일한 적용 경로는 아니지만 운영 진입 시점 명시화에 권장
2. **Backfill dry-run**: 각 source 별 backfill job 작성 + dry-run 모드 (실제 INSERT X, log only)
3. **Source별 partial backfill**: 1 source씩 (예: KRX 먼저) 일부 date range 실측 적재 → 검증
4. **전체 backfill**: 3 source 모두 historical 적재
5. **Daily append enable**: 매일 KST 00:01 ~ 00:10 cron 활성화 (KRX는 close finalizer 직후 write)
6. **v2 endpoint switch**: catalog/tab endpoint가 source_daily_rates 조회로 전환 (Phase 2e)

각 단계는 이전 단계 검증 후 진입. 운영 영향은 6단계 (v2 endpoint switch)에서만 발생.

**Open**:

- 단계별 진입 검증 기준 (예: backfill 완료율 100% / daily append 7일 연속 성공 등)
- **장기 그래프 DB 표준화 (Phase 2e 진입 전 결정)**: 1w는 `source_hourly_rates`(가칭), 3m/1y는 `source_daily_rates`로 분리하는 2-table 표준화안을 Phase 2e v2 endpoint 구현 PR 직전에 평가한다. 별도 ADR-035에서 DXY/Investing/Hana/KRX/Bithumb 1w 적재 범위와 legacy hybrid 제거 여부를 결정함 (ADR-035 Proposed, 2026-06-05 land — 본 문서 ADR-035 섹션 참조).

### 15. Alternatives considered

**Option A — 외부 API hot path 유지** (거부):

- 그래프 요청 시 외부 API 직접 호출
- 단점: 장애/차단/schema 변경 risk + latency (외부 API ~100ms vs DB ~5ms)
- → 거부 (ADR-033 Amendment 후속 핵심 원칙)

**Option B — graph_buckets / market_index_rates 같은 기존 table 재사용** (거부):

- DXY granularity 2-part merge 패턴 ([ADR-019](#adr-019-dxy-보조지표--granularity-기반-2-part-merge-전략)) 재사용
- 단점: schema가 DXY 한정 (instrument='dxy' / granularity 컬럼). source/asset/close_basis 분리 안 됨
- → 거부 (새 schema 필요)

**Option C — source_daily_rates 신규 (채택)**:

- 별 table + 명시적 schema + provenance metadata (close_basis / source_method / ohlc_quality 직교 분리)
- 운영 정합성 + future extensibility 우위
- rate == close invariant로 legacy/v2 consumer 분리

### Phase 2d Step 1 land (2026-05-28)

ADR-034 §3 schema + Open #14/#16/#17 → Accepted 전환 + helper module 신규 도입.

**변경 파일** (land 완료):

- `app/models.py`: `SourceDailyRate` ORM class 추가 (Numeric(14,6) + unique (source, asset, date_kst) + top-level `ohlc_quality` + `metadata_json` column + invariant comment)
- `scripts/migrate_source_daily_rates.py` (신규): idempotent migration script — `SourceDailyRate.__table__.create(bind=engine, checkfirst=True)` pattern + `--dry-run` 모드 (DB 연결 없이 CREATE TABLE + CREATE INDEX DDL 출력, `--dialect postgresql|sqlite` 명시) + 컬럼/인덱스 audit 출력
- `app/source_daily_rates.py` (신규): CRUD helper — `get_by_key` / `get_range` / `upsert` (rate==close invariant enforce + close_only fallback + dialect 분기 + set_ 조건부 nullable update) / `batch_upsert` (단일 transaction) / `delete_range` (rebuild cleanup) / `find_rate_close_drift` (monitoring) + `row_to_dict` (Numeric → float 변환 정책)

**Upsert null overwrite 방지 패턴**:

- `contract_code` / `basis_date` / `published_at` / `metadata_json`: incoming non-null일 때만 `set_` dict에 포함 (None이면 update 자체 발생 X → 기존 값 자연 보존). COALESCE 대신 **set_ 조건부 추가 패턴** 사용 — dialect-agnostic (SQLite JSON column COALESCE 동작 차이 회피) + 명시적 의도 표현.
- `captured_at`: `func.now()`로 명시 update (row 마지막 update 시각).

**Dialect 분기 (Session.bind 기준)**:

- `db.bind.dialect.name == "postgresql"` → `dialects.postgresql.insert`
- `db.bind.dialect.name == "sqlite"` → `dialects.sqlite.insert` (SQLAlchemy 1.4+ ON CONFLICT 지원)
- 그 외 dialect → `NotImplementedError`
- helper가 `app.database.engine` 직접 의존 X — test용 SQLite in-memory engine 등에서도 동일 helper 검증 가능.

**검증 (Step 1 land 전 7/7 통과)**:

1. invariant `rate == close` enforce
2. nullable fields null overwrite 방지 (`basis_date` / `published_at` / `metadata_json` / `contract_code` 모두 보존)
3. `metadata_json` incoming non-null replace policy
4. `close_only` fallback (high=low=close synthetic)
5. `batch_upsert` 단일 transaction
6. `rate != close` drift monitoring (drift=0 정상)
7. `row_to_dict` Numeric → float 변환

**Production migration 적용**:

- 본 land에 포함 X — 별도 GO 단계
- 검증 명령: `docker compose run --rm fastapi python scripts/migrate_source_daily_rates.py --dry-run`
- 적용 명령: `docker compose run --rm fastapi python scripts/migrate_source_daily_rates.py`

**Step 1 land 후 운영 상태**:

- 코드 배포 시 `app/main.py:193` `create_all_app_tables(engine)`(내부 `Base.metadata.create_all` — `app/database.py:65`)이 빈 `source_daily_rates` table 자동 생성 가능 (lock 거의 없음, ALTER 없음)
- 운영 영향 거의 0 — read/write 호출자 X (helper module은 import 가능하나 사용자 없음)
- ADR-034 §14 Rollout step 1 완료. Step 2 (backfill dry-run) 진입 가능.

**후속 작업 (Step 2+ 영역)**:

- Backfill job (Step 2): Bithumb 24h candle / KIS daily chain / Hana official endpoint
- Daily append job (Step 5): close finalizer (KRX) / observed_eod (Hana) / ~~source_rates KST daily rollup (Bithumb)~~ → Bithumb candle API daily refresh (Amendment 2026-06-01)

### Phase 2d Step 2 land — Bithumb dry-run (2026-05-28)

ADR-034 §14 Rollout step 2 (각 source 별 backfill job 작성 + dry-run 모드) — Bithumb 24h candlestick dry-run validator land. Step 2/3 경계 보존 (DB write 없음, `upsert()` 호출 없음).

**변경 파일** (land 완료):

- `scripts/backfill_bithumb_source_daily_rates.py` (신규): Bithumb USDT/KRW 24h candlestick API → source_daily_rates row dict 변환 + 9 validation suite + issue 발생 시 `sys.exit(1)`. ADR-033 Amendment 1 Decision 2-1의 무인증 / OCHL schema `[ts_ms, open, close, high, low, volume]` / 902일 coverage 정책 그대로 적용. 외부 의존성 0 (requests만 사용, ccxt 도입 회피).

**검증 9 항목 (모든 row 대상 전수 검사)**:

1. raw API shape (first/last raw row 출력)
2. parse 실패 collection (raise → script crash 회피, issue 수집 후 통계 보존)
3. sort order (ascending / descending / mixed 라벨)
4. anchor (모든 row의 KST timestamp가 `00:00:00+09:00`인지 — first/last only가 아닌 전수 검사)
5. `rate == close` invariant
6. duplicate `date_kst`
7. date gap (expected = `(last - first).days + 1`)
8. OHLC non-positive / `high < low`
9. Decimal(14, 6) precision (소수부 6자리 초과)
10. metadata policy (`contract_code` / `basis_date` / `published_at` = None 회귀 가드)

**Dry-run 실행 결과 (전체 904 candles, 2026-05-28 KST 04:07)**:

- parse 실패 / sort order / anchor 전수 / duplicate / date gap / OHLC / precision / metadata policy: 모두 **0건**
- exit code = 0
- date range: 2023-12-07 ~ 2026-05-28 (904일, ADR-033 Amendment 1 "902일"보다 +2일은 시간 흐름 자연 증가)

**핵심 발견 (Step 3+ 정책 anchor)**:

- Bithumb 24h candle `ts_ms`는 **KST 00:00 boundary anchor** — ADR-034 §6 `bithumb_24h_kst_close` 정책과 자연 정렬. Step 3 partial backfill 진입 시 date_kst 변환 logic 추가 불필요 (1:1 매핑).
- **904일 span 누락 candle 0개**. Bithumb historical data 신뢰성 baseline 확정.
- sort order = ascending (별도 sort 단계 불필요).
- OCHL response shape 실측 확정: `[ts_ms_int, open_str, close_str, high_str, low_str, volume_str]` — 가격은 string. `Decimal(str(...))` 변환 일관 적용.
- 단일 호출로 전체 904 candle fetch (페이지네이션 불필요).

**Step 2/3 경계 보존**:

- 본 script는 `app/source_daily_rates.upsert()` 호출하지 않음 (DB write 절대 X)
- row dict 생성 + validation 후 결과 출력만
- Step 3 (partial backfill 실측 적재)은 별도 PR

**published_at 정책 (schema 의미 보존)**:

- Bithumb candle close timestamp는 `published_at`에 넣지 않음 (`published_at`은 Hana official 발표 timestamp 한정 의미)
- `metadata_json.candle_ts_ms` / `metadata_json.candle_ts_kst`로 격리

**보정 history (Codex 5 review rounds 반영)**:

- Round 1 (외부 의존성): ccxt 도입 회피 → requests + inline OCHL tuple unpack
- Round 2 (Step 2/3 경계): `upsert()` 호출 제거 → row dict + validation only
- Round 3 (검증 항목 확장): non-positive OHLC / Decimal precision / duplicate / gap / sort order
- Round 4 (3 blocker): `sys.exit(1)` on issue / anchor 전체 row 검사 / 전체 904 dry-run 실행
- Round 5 (3 non-blocker): `--limit` positive int validator / parse 실패 issue collection / metadata policy validation

**후속 작업 (Step 2 영역 확장 + Step 3 진입)**:

- KIS daily chain dry-run (Step 2 — KRX A75YMM contract chain + 만기일 07:00 KST rollover boundary)
- Hana official_historical dry-run (Step 2 — `pbldSqn` provenance + 휴일 fallback + `basis_date` 매핑)
- Step 3 partial backfill 실측 적재 (예: KRX 먼저, ADR-034 §14 잠정 — service value 기준)
- Step 2 dry-run script는 fetch 1회 + raise propagate. scheduled job (Step 3+) 진입 시 retry/backoff/structured error는 그때 함께 land

### Phase 2d Step 2-2 land — Hana dry-run (2026-05-28)

ADR-034 §14 Rollout step 2 — Hana official_historical dry-run validator land. Step 2/3 경계 보존 (DB write 없음, `upsert()` 호출 없음). Step 2-1 (Bithumb) 다음 자연 단계 — schema/helper의 미검증 path (`close_only` / `basis_date` / `published_at` / `pbldSqn`)를 처음 활성 검증.

**변경 파일** (land 완료):

- `scripts/backfill_hana_source_daily_rates.py` (신규): Hana official endpoint (`wpfxd651_01i_01.do`) HTML response → source_daily_rates row dict 변환 + 10 validation suite + issue 발생 시 `sys.exit(1)`. `[FETCH 실패]` (requests.RequestException) / `[PARSE 실패]` (ValueError/AttributeError/InvalidOperation) structured output. 외부 의존성 0 (requests + bs4, 둘 다 이미 사용 중).

**검증 10 항목 (단건 fetch + transparent row mapping)**:

1. raw fetch + response size (structured failure on RequestException)
2. HTML parse — 기준일 / 고시일시 / 회차 / 매매기준율 idx 7 (structured failure on ValueError·AttributeError·InvalidOperation)
3. fallback signal (`request_date != basis_date` 시 `metadata_json.fallback=true`)
4. txtAr cell count >= 8 (DOM 변경 monitoring, GRAPH_API_V2_CONTRACT.md §14 Open anchor)
5. `rate == close` invariant
6. `close_only` fallback (`high == low == close` + `ohlc_quality == "close_only"`)
7. `date_kst == basis_date` (canonical date 정책)
8. metadata policy (Hana 필수: `basis_date` / `published_at` / `metadata_json.pbldSqn` 모두 not None / `contract_code = None`)
9. `published_at` KST tzinfo + offset +09:00 (Hana 발표 timestamp schema 의미 잠금)
10. `close_basis = "hana_official_historical_backfill"` / `source_method = "external_backfill"` enum + OHLC non-positive + Decimal(14, 6) precision

**Dry-run 4 case smoke (2026-05-28 KST)**:

| Case | currency | request_date | basis_date | fallback | pbldSqn | 매매기준율 | published_at | exit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | USD | 2026-05-26 (화) | 2026-05-26 | false | 1081 | 1507.50 | 2026-05-27 07:46:40 KST | 0 |
| 2 | USD | **2026-05-24 (일)** | **2026-05-22 (금)** | **true** | 2513 | 1512.00 | 2026-05-26 05:42:02 KST | 0 |
| 3 | JPY | 2026-05-26 | 2026-05-26 | false | 1081 | 946.39 | 2026-05-27 07:46:40 KST | 0 |
| 4 | EUR | 2026-05-26 | 2026-05-26 | false | 1081 | 1753.67 | 2026-05-27 07:46:40 KST | 0 |

**핵심 발견 (Step 3+ 정책 anchor)**:

- **휴일 fallback 정책 정확 동작 (Case 2 핵심)**: 2026-05-24 일요일 요청 → 2026-05-22 금요일 자동 fallback, `date_kst = basis_date = 2026-05-22`, `metadata_json.fallback = true` signal 정확. GRAPH_API_V2_CONTRACT.md §7 line 255 정책 사례 ("2026-05-24 일 요청 → 응답 기준일 2026-05-22 금") 100% 재현.
- **USD/JPY/EUR 3통화 동일 schema + 동일 발표 timestamp 공유**: 같은 요청일 3통화 모두 `pbldSqn=1081` + `published_at=2026-05-27 07:46:40 KST` → Hana는 회차 단위로 3통화 동시 발표. 통화별 별도 fetch 필요하나 회차/timestamp는 공유.
- **GRAPH_API_V2_CONTRACT.md §7 anchor 정확 일치**: line 284 검증 사례 (2026-05-26 USD = 1081회차, 다음날 07:46 발표) 1차 fetch부터 100% 재현. response shape 안정성 확인.
- **`close_only` fallback path 처음 활성**: `ohlc_quality="close_only"` + `high == low == close` synthetic — Bithumb dry-run에서는 `source_ohlc` only path만 cover. Hana로 schema/helper close_only path 잠금.
- **published_at top-level field 실사용**: schema 정의의 본 의미 (Hana official 발표 timestamp 한정) — Bithumb은 None, Hana는 `2026-05-27T07:46:40+09:00` KST timezone-aware datetime 저장. tzinfo + offset +09:00 validation 잠금.
- **txtAr cell count = 10** (인덱스 0-9, GRAPH_API_V2_CONTRACT.md §7 매핑과 일치). DOM 변경 monitoring baseline 확정.

**Step 2/3 경계 보존**:

- 본 script는 `app/source_daily_rates.upsert()` 호출하지 않음 (DB write 절대 X)
- row dict 생성 + 10 validation 후 결과 출력만
- Step 3 (partial backfill 실측 적재)은 별도 PR

**회차 (`pbldSqn`) 정책 (ADR-033 Amendment 2)**:

- `pbldSqn=` 빈값 default 요청 → response의 representative rate 사용
- 회차 값 (1081, 2513 등)은 `metadata_json.pbldSqn`로 저장 (provenance/debug)
- "최종 회차" 공식 단정 X — 운영 검증상 최신/최종 고시값으로 동작

**보정 history (Codex 4 review rounds 반영)**:

- Round 1 (외부 의존성): bs4 / requests 이미 사용 중 — 새 의존성 0
- Round 2 (정책 잠금 확인): date_kst = basis_date / 휴일 fallback 옵션 A / published_at top-level / USD/JPY/EUR / endpoint URL / 매매기준율 idx 7 — 모두 GRAPH_API_V2_CONTRACT.md §7과 ADR-033 Amendment 2에 잠긴 상태 확인
- Round 3 (Hana dry-run script 범위): Bithumb과 반대 방향 metadata policy (basis_date / published_at / pbldSqn 필수), close_only path 활성, fallback signal metadata_json
- Round 4 (2 Blocker + 1 Non-blocker):
  - Blocker 1: `[FETCH 실패]` / `[PARSE 실패]` structured output (`requests.RequestException` / `ValueError·AttributeError`)
  - Blocker 2: `published_at` KST timezone + offset +09:00 validation
  - Non-blocker: `decimal.InvalidOperation` catch 추가 (rate cell text 비정상 DOM 회귀 가드)

**후속 작업 (Step 2 영역 마지막 source + Step 3 진입)**:

- KIS daily chain dry-run (Step 2-3 마지막 — KRX A75YMM contract chain + 만기일 07:00 KST rollover boundary + 100건 cap 회피)
- Step 3 partial backfill 실측 적재 (ADR-034 §14 §3 잠정 — KRX 먼저, service value 기준)

### Phase 2d Step 2-3 land — KIS daily chain dry-run (2026-05-28)

ADR-034 §14 Rollout step 2 — KIS daily chain dry-run validator land. **Step 2 영역의 마지막 source** (Bithumb / Hana / KIS 3-source 모두 dry-run 완료 — Step 2 closed). Step 2/3 경계 보존 (DB write 없음, `upsert()` 호출 없음).

**변경 파일** (land 완료):

- `scripts/backfill_kis_source_daily_rates.py` (신규): KIS `inquire-daily-fuopchartprice` REST endpoint → source_daily_rates row dict 변환 + 13 validation suite + 2 mode (`dynamic` default + `--known-boundary-smoke`) + structured failure (`[CONFIG 실패]` / `[TOKEN 실패]` / `[MASTER 실패]` / `[FETCH 실패]` / `[PARSE 실패]`). 외부 의존성 0 (requests + dotenv + 운영 `KisAccessTokenManager` 재사용으로 drift 회피).

**Mode 분리 (Codex 보강, Round 5)**:

- **dynamic (default)** — 운영 영속성. `current.contract_month - 1`로 previous 동적 계산. 시간 흐름 시 자동 갱신.
- **`--known-boundary-smoke`** — GRAPH §7-new line 372-379 검증 사례 재현 전용. `previous = A75605` hardcoded. `current=A75606` 시점에만 정합 (가드 raise).

**Chain 구성 + 사용 구간 (Codex Round 6 정정)**:

각 contract C_i의 user-facing 사용 구간 = `[C_{i-1}.expiry_date, C_i.expiry_date)` 반열린 구간:

- **before_previous** A75604 (expiry 2026-04-20) — anchor only, fetch X
- **previous** A75605 (expiry 2026-05-18) — fetch range `[2026-04-20, 2026-05-17]`
- **current** A75606 (expiry 2026-06-15) — fetch range `[2026-05-18, today-1=2026-05-27]` (today 제외 default)
- **next** A75607 (expiry 2026-07-20) — current.expiry 미도래 → probe range `[2026-06-08, 2026-06-15]` (mapping 미사용)

validation `validate_usage_segment`: `previous_contract.expiry_date <= row.date_kst < current_contract.expiry_date` 양방향 검증.

**Token cache 정책 (Codex Round 2)**:

- default: `.cache/kis_access_token.json` (운영 `KisAccessTokenManager` + `scripts/kis_smoke.py` 공유) — KIS는 1일 1회 발급 원칙이라 shared가 추가 발급 0으로 가장 안전
- `--token-cache-path`: operator가 별도 cache 명시 가능 (보수적 격리 시 새 token 발급 유도 위험 회피용으로는 default가 더 안전)
- `KisAccessTokenManager` (async) → `asyncio.run()` wrapper로 sync 재사용 (drift 최소화)

**검증 13 항목**:

1. `[CONFIG 실패]` env 미설정 (KIS_APP_KEY/KIS_APP_SECRET)
2. `[TOKEN 실패]` KisAccessTokenManager 발급/cache load 실패
3. `[MASTER 실패]` KIS master fetch/parse 실패
4. `[FETCH 실패]` requests.RequestException
5. `[PARSE 실패]` output2 parse / ValueError / InvalidOperation
6. `fetch_range_compliance` — mapped contract OOR = exit 1 (데이터 오염) / probe OOR = WARNING surface only (KIS stale 동작 known, range count 미포함)
7. `usage_segment` — `[previous.expiry, this.expiry)` 양방향 (Codex Round 6 정정)
8. `rate == close` invariant
9. `source_ohlc` OHLC 완전 + `high >= low`
10. metadata policy (KRX 방향: `contract_code` 필수 / `basis_date=None` / `published_at=None`, Hana와 반대)
11. `close_basis = "krx_cf_close_1545"` + `source_method = "kis_daily_backfill"` enum
12. `duplicate date_kst` (mapped_rows 기준, Codex Non-blocker 2)
13. Decimal(14, 6) precision + OHLC non-positive

**Dry-run 결과 (2026-05-28 KST, mapped 25 / probe 1)**:

- **dynamic mode**: exit 0, mapped 25 rows (previous A75605: [2026-04-20, 2026-05-15] / current A75606: [2026-05-18, 2026-05-27]) / probe 1 row
- **known-boundary-smoke mode**: exit 0, 동일 chain (current=A75606 시점이라 dynamic과 결과 일치)
- **GRAPH §7-new line 372-379 B-B probe 사례 100% 재현**: 2026-05-18 row → A75606 contract → close=1496.500
- **probe out-of-range row** (next A75607 fetch [2026-06-08, 2026-06-15] 요청에 2026-05-28 row 반환): WARNING surface only — Step 3 적재 시 차단 신호로 명시 (KIS daily endpoint가 미래 range에 listing 활성 contract 최신 row 반환하는 stale 동작)

**핵심 발견 (Step 3+ 정책 anchor)**:

- **dynamic mode가 GRAPH §7-new anchor를 자동 재현** — 시간 흐름 시 운영 영속성 검증 완료 (current=A75607 미래 시점에도 previous=A75606 자동 계산)
- **user-facing chain mapping** = `[previous.expiry, this.expiry)` — Step 3 적재 시 contract_code mapping 정확성 보장. SAFE_FETCH_DAYS 임의 range는 100건 cap 회피만 보장 (Round 6에서 폐기)
- **today 제외 default** — 15:45 close 미확정 intraday row가 `krx_cf_close_1545`로 잘못 저장되는 위험 회피. `--include-today` opt는 close 도래 후 명시 사용
- **mapped/probe 분리** — global validation은 mapped 기준 (probe stale row가 backfill 후보 품질 오염 차단)
- **KIS access_token 1일 1회 발급 원칙** — shared cache가 추가 발급 회피 + 운영 token invalidation 위험 0
- **100건 cap 회피** — chain 사용 구간 단위로 자연 회피 (previous 28일, current 10일 모두 cap 미접근)

**Step 2/3 경계 보존**:

- 본 script는 `app/source_daily_rates.upsert()` 호출하지 않음 (DB write 절대 X)
- row dict 생성 + 13 validation 후 결과 출력만
- Step 3 (partial backfill 실측 적재)은 별도 PR

**보정 history (Codex 6 review rounds 반영)**:

- Round 1 (외부 의존성): 운영 `KisAccessTokenManager` 재사용 — 신규 의존성 0
- Round 2 (token cache): default shared `.cache/kis_access_token.json` + `--token-cache-path` opt (KIS 1일 1회 원칙)
- Round 3 (chain 옵션 B): current/previous/next 3 contracts + rollover boundary 검증 (단건 fetch 거부)
- Round 4 (사용 구간 정정): `[previous.expiry, this.expiry)` 반열린 구간 + 양방향 validation
- Round 5 (4 Blocker + 2 Non-blocker):
  - Blocker 1: today 제외 default (`--include-today` opt)
  - Blocker 2: `validate_fetch_range_compliance` 신규 — mapped contract OOR = exit 1 / probe OOR = WARNING surface
  - Blocker 3: mapped_rows vs probe_rows 분리 (global validation은 mapped 기준)
  - Blocker 4: mode 분리 (dynamic default + `--known-boundary-smoke`) — 운영 영속성 + anchor 재현 분리
  - Non-blocker 1: `validate_source_ohlc` dead code 제거
  - Non-blocker 2: `validate_duplicates_mapped` 신규 (mapped 기준)
- Round 6 (previous 사용 시작 anchor): `_build_before_previous_dynamic(previous)` — previous fetch range = `[before_previous.expiry, previous.expiry-1]` (SAFE_FETCH_DAYS 임의 range 폐기, user-facing chain mapping 정확성 ↑) + probe section 문구 정정 ("WARNING surface only, range count 미포함")

**후속 작업 (Step 3 진입)**:

- **Step 2 영역 closed** — Bithumb (Step 2) / Hana (Step 2-2) / KIS (Step 2-3) 3-source 모두 dry-run + validation 통과
- Step 3 partial backfill 실측 적재 (ADR-034 §14 §3 잠정 — KRX 먼저, service value 기준 + GRAPH §7-new rollover boundary 검증 완료)
- Step 3 적재 시 KIS probe out-of-range row 차단 logic 별도 land (현재 dry-run에서 WARNING surface 완료)
- retry/backoff/structured error는 scheduled job (Step 3+) 진입 시 함께 land

### Phase 2d Step 3 옵션 B Round 9 — KRX chain previous + current 확장 (2026-05-28)

ADR-034 §14 Rollout step 3 옵션 B 진입 — **chain previous + current 확장** (first PR의 current 한정 → previous A75605 + current A75606 chain 단일 transaction 적재 지원). **Stage 1 (commit `1e30460`) + Stage 2 (origin/master push) + Stage 3 (production RDS execution) 모두 land 완료** (2026-05-28 KST, `--contract previous` only 진행 — current 7 rows 재-upsert 회피 + manual snapshot 생략, 자동 backup 1 Day + `delete_range` rollback anchor 신뢰). Stage 3 production verify: **18 rows committed (A75605) + 7 post-write validations passed + drift 0 + current A75606 7 rows 영향 0 + 전체 chain 25 rows + rollover boundary 2026-05-18 → A75606 close=1496.500 유지** (GRAPH §7-new B-B probe 사례).

**변경 파일** (Stage 1+2+3 land 완료):

- `scripts/backfill_kis_source_daily_rates.py` 확장 (옵션 B Round 9):
  - `--contract current|previous|both` argparse 추가 (default `current`, **후방 호환**)
  - `validate_write_range(chain_start_expiry, chain_end_expiry, chain_label)` 시그니처 확장 — single contract → multi-contract chain anchor
  - `write_with_transaction(contract_codes: set[str])` 시그니처 확장 — multi-contract single transaction
  - post-write SELECT: `contract_code.in_(contract_codes)` + date range filter (same-contract 다른 range row 혼입 차단)
  - post-write validation: `expected_dates set` → **`expected (date_kst, contract_code) pairs set`** (multi-contract — same date 다른 contract 매핑 위험 차단, 강화)
  - main() write section: `--contract` 분기 (current/previous/both별 rows_source / contract_codes / chain anchor 결정)
  - help 문구 정정 ("current contract" → "selected chain mode")

**Chain 사용 구간 (mode별)**:

| Mode | `chain_start_expiry` | `chain_end_expiry` | 적재 대상 |
| --- | --- | --- | --- |
| `current` (default) | `previous.expiry` (=2026-05-18) | `current.expiry` (=2026-06-15) | A75606 |
| `previous` | `before_previous.expiry` (=2026-04-20) | `previous.expiry` (=2026-05-18) | A75605 |
| `both` | `before_previous.expiry` (=2026-04-20) | `current.expiry` (=2026-06-15) | A75605 + A75606 |

**Local SQLite smoke 5단계 (모두 PASS)**:

| Step | 결과 |
| --- | --- |
| 1. py_compile + dry-run regression (`--contract` 없을 때 default current, write 안 함) | exit 0 |
| 2. `--contract previous` smoke ([2026-04-20, 2026-05-17], A75605) | exit 0, **18 rows committed** (영업일 기준) |
| 3. cleanup + `--contract both` smoke ([2026-04-20, 2026-05-27], chain 전체) | exit 0, **25 rows committed** (18 prev + 7 cur, single transaction) |
| 4. idempotent `--contract both` rerun (같은 args) | exit 0, 동일 결과 (unique key 자연 idempotent + COALESCE-style set_ 조건부) |
| 5. cross-mode `--contract previous` re-run on same DB | exit 0, current rows 영향 0, total 25 rows 유지, drift 0 |

**핵심 발견 (rollover boundary 정합 검증)**:

- A75605 사용 구간 정확 분리: rows `[2026-04-20 ~ 2026-05-15]` (만기 5/18 이전 마지막 영업일까지)
- A75606 사용 구간 정확 분리: rows `[2026-05-18 ~ 2026-05-27]` (rollover boundary부터)
- 5/16 토 / 5/17 일은 영업일 아니라 row 없음 (자연 분리)
- **boundary 2026-05-18 → A75606 (current) / close=1496.500** — GRAPH §7-new B-B probe 사례 100% 재현 (both mode에서도)
- cross-mode regression: same DB에 previous-only re-run 시 current 8 rows 영향 0 (mode 분리 정확)

**Codex 검토 (Round 9, Blocker 0)**:

- Blocker 없음 — diff/smoke 결과 모두 정합
- post-write 검증이 `(date_kst, contract_code)` pair 기준 강화되어 multi-contract 혼입/누락 검출 OK
- query 도 `contract_code.in_()` + date range 제한으로 기존 row 혼입 위험 낮음
- Non-blocker (commit 전 정정 완료): `--write`/`--start-date`/`--end-date` help 문구 "current contract" → "selected chain mode"로 정정

**Stage 분리 (옵션 B, Step 3 first PR 패턴 동일)**:

- **Stage 1+2+3 모두 land 완료** (2026-05-28 KST)
- Stage 1: `--contract` 확장 script + 5-단계 SQLite smoke 검증 — commit `1e30460`
- Stage 2: origin/master push 완료
- Stage 3: production RDS execution 완료 (`--contract previous` only, manual snapshot 생략, 자동 backup 1 Day + `delete_range` rollback anchor 신뢰)
  - 절차: SSH → git pull (1e30460 fast-forward) → docker compose build fastapi (image sha `ad30126a13dd...`) → `docker compose run --rm fastapi python scripts/backfill_kis_source_daily_rates.py --write --contract previous --start-date 2026-04-20 --end-date 2026-05-17 --allow-production-write` → post-write 검증 (drift 0 / chain 25 / boundary 1496.500)
  - `--contract both` 미진행 — current 7 rows는 first PR (commit `40279c9`)에서 이미 production land됨, 재-upsert 의미 X
- 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역 (현재 source_daily_rates read 호출자 0 — user-facing 영향 0)

**Rollback anchor**:

```python
from app.database import SessionLocal
from app.source_daily_rates import delete_range
from datetime import date
db = SessionLocal()
# previous only:
delete_range(db, "krx", "usd-krw-futures", date(2026, 4, 20), date(2026, 5, 17))
# 또는 both (chain 전체):
delete_range(db, "krx", "usd-krw-futures", date(2026, 4, 20), date(2026, 5, 27))
```

**후속 작업 (옵션 B Stage 3 진입 후)**:

- Hana / Bithumb Step 3 partial backfill 진입 (KRX 옵션 B 안정성 확인 후)
- Step 4 full backfill (모든 contract chain + 모든 source historical 1년+ range)
- Step 5 daily append job (scheduled cron + retry/backoff/structured error)
- Step 6 v2 endpoint switch (Phase 2e)

### Phase 2d Step 3 Hana first PR Round 1 — USD partial backfill writer (2026-05-28)

ADR-034 §14 Rollout step 3 — **Hana** first PR. KRX Step 3 옵션 B land 후 다음 source 다각화. **Stage 1 (commit `15ef873`) + Stage 2 (origin/master push) + Stage 3 (production RDS execution) 모두 land 완료** (2026-05-28 KST, **manual snapshot 생략** — Hana incremental write 자동 backup 1 Day + 개별 `date_kst.in_()` delete rollback anchor 신뢰). Stage 3 production verify: **Hana USD 4 rows committed (2026-05-21 / 22 / 26 / 27, 휴일 dedup 적용) + post-write validations passed + drift 0 + schema close_only path 처음 production-level 활성** (close_only fallback + basis_date NOT NULL + published_at NOT NULL KST timezone-aware + metadata_json.pbldSqn provenance 각 row별 다른 회차 1356/2513/1081/1271).

**변경 파일** (Stage 1+2+3 land 완료):

- `scripts/backfill_hana_source_daily_rates.py` 확장 (Step 2-2 dry-run script에 write mode 추가):
  - `--write` flag (default off, dry-run only safe)
  - `--start-date` / `--end-date` (calendar-day range, --write 시 필수)
  - `--include-today` (default off, intraday close 오염 회피)
  - `--allow-production-write` (default off, non-SQLite DB 차단)
  - 신규 함수:
    - `check_production_write_guard` (KIS writer 패턴 재사용 — dialect/host redacted)
    - `validate_write_range` (Hana는 contract chain 없음 → 단순 range + today 가드)
    - `ensure_source_daily_rates_table_created` (idempotent)
    - `fetch_and_dedup_calendar_range` (**Hana-specific**: calendar-day loop + basis_date dedup + fallback events 수집)
    - `write_with_transaction_hana` (Hana 방향 metadata policy: contract_code IS NULL / basis_date+published_at+pbldSqn NOT NULL)
  - `typing.Optional` import 추가

**First PR scope (옵션 A — USD only / 짧은 range)**:

- Currency: **USD only** (JPY/EUR는 후속 PR)
- Calendar range: `[2026-05-21, 2026-05-27]` (calendar 7일)
- Expected unique rows: **4** (휴일 dedup: 5/23 토 + 5/24 일 + 5/25 월 대체공휴일 → 5/22 fallback)
- Production guard: dialect검사 + `--allow-production-write` 필요

**Hana writer vs KRX writer 패턴 차이**:

| 항목 | KRX writer | Hana writer |
| --- | --- | --- |
| Fetch 단위 | 1 contract = 1 KIS daily fetch (다수 row) | 1 calendar day = 1 HTTP fetch (1 row) |
| Loop | Contract chain (3 contracts) | **Calendar-day** (start ~ end 매일) |
| Dedup | contract_code IN | **basis_date** (휴일 fallback) |
| post-write SELECT | `contract_code.in_()` + range | **`date_kst.in_(expected_dates)`** (비연속 expected) |
| Metadata policy 방향 | contract_code NOT NULL / basis_date+published_at NULL | contract_code NULL / **basis_date+published_at+pbldSqn NOT NULL** |
| close 의미 | OHLC 완전 (source_ohlc) | **close_only fallback** (high=low=close synthetic) |

**Codex review Round 1 보정 (Blocker 1 + 보완 2)**:

- **Blocker**: post-write SELECT를 `date_kst >= min AND date_kst <= max` (range query) → **`date_kst.in_(expected_dates)`** 정정. Hana는 calendar-day dedup으로 expected_dates 비연속 (예: `{5/21, 5/22, 5/26, 5/27}`, 5/23~5/25 gap). production에서 같은 asset에 다른 path 적재된 row가 gap 안에 있으면 range query는 false failure → `in_()` filter로 정확 매칭. **KRX Round 7 Blocker 2와 동등 패턴** (KRX는 contract_code.in_(), Hana는 date_kst.in_()).
- **권장 보완**: Rollback anchor를 `delete_range` → **expected_dates 개별 `date_kst.in_()` delete snippet** 출력 (range delete는 비연속 dates 휴일 gap 안 기존 row 영향 risk → 개별 delete가 안전).
- **Non-blocker**: `--end-date` help "default today-1" → "today-1 이하 권장" (실제 default=None, --write 시 필수).

**Local SQLite smoke 8단계 모두 PASS**:

| Step | 결과 |
| --- | --- |
| 1. py_compile | OK |
| 2. dry-run regression (`--write` 없을 때 단일 date 검증) | exit 0 |
| 3. `--write` without override (production guard 차단) | exit 1, dialect=postgresql host=*** redacted |
| 4. cleanup + first run | exit 0, **4 rows committed** + post-write validations passed |
| 5. **Codex Blocker 검증** — gap 안 fake row 3개 (5/23, 5/24, 5/25) 삽입 후 write 재실행 | **exit 0** (이전 range query였으면 false failure, `in_()` 정정 후 fake row 영향 0) |
| 6. cleanup (fake + real) + final smoke (clean state) | exit 0 |
| 7. idempotent rerun | exit 0, same result |
| 8. DB query 직접 검증 | 4 rows / `[2026-05-21, 2026-05-22, 2026-05-26, 2026-05-27]` / drift 0 |

**핵심 발견 (Step 3+ 정책 anchor)**:

- **휴일 fallback + dedup 정확 작동**: 5/23 토 + 5/24 일 + 5/25 월 모두 5/22 fallback → unique 4 rows
- **2026-05-25 대체공휴일 자연 흡수**: 5/24 부처님오신날(일) + 다음 월 대체공휴일 5/25 → 모두 5/22 fallback. KRX_CANARY 2026-05-25 사고 사례 (KIS REST stale 5/22) 캘린더와 일관 검증
- **schema close_only path 첫 production-level 활성** (Bithumb/KRX는 source_ohlc만 cover):
  - `ohlc_quality == "close_only"` + `high == low == close == rate`
  - `basis_date == date_kst` (canonical date 정책)
  - `published_at` KST timezone-aware datetime (다음날 발표 시각, 예: `2026-05-27 07:46:40 KST`)
  - `metadata_json.pbldSqn` provenance (각 row 별 다른 회차 — 1356/2513/1081/1271)
- **Hana writer의 post-write false failure risk 잠금** (Codex Round 1 Blocker): production에서 다중 backfill path 또는 daily append job + Step 3 overlap 시에도 false failure 0
- **Rollback anchor production-safe**: 비연속 expected_dates 개별 `date_kst.in_()` delete (range delete의 휴일 gap 안 기존 row 영향 risk 회피)

**Stage 분리 (KRX Step 3 first PR / 옵션 B 패턴 동일)**:

- **Stage 1+2+3 모두 land 완료** (2026-05-28 KST)
- Stage 1: write logic + production guard + calendar-day loop + dedup + transaction + 8-단계 SQLite smoke 검증 — commit `15ef873`
- Stage 2: origin/master push 완료
- Stage 3: production RDS execution 완료 (`--currency USD --start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write`, **manual snapshot 생략** — Hana incremental write 자동 backup + 개별 dates delete rollback anchor 신뢰)
  - 절차: SSH → git pull (1e30460 → 15ef873 fast-forward) → docker compose build fastapi (image sha `8d63fb1435db...`) → `docker compose run --rm fastapi python scripts/backfill_hana_source_daily_rates.py --write --currency USD --start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write` → post-write 검증
  - production verify: 4 rows committed (Hana USD `[2026-05-21, 2026-05-22, 2026-05-26, 2026-05-27]`) + post-write validations passed + drift 0 + 휴일 dedup 정확 (5/23 토 / 5/24 일 / 5/25 월 대체공휴일 모두 5/22 fallback) + close_only path production 활성 + pbldSqn 1356/2513/1081/1271
- 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역 (현재 source_daily_rates read 호출자 0 — user-facing 영향 0)
- 전체 production source_daily_rates state: KRX 25 rows + Hana 4 rows + Bithumb 0 rows

**Rollback anchor (Hana-specific, 개별 date 삭제)**:

```python
from app.database import SessionLocal
from app.models import SourceDailyRate
from datetime import date
db = SessionLocal()
db.query(SourceDailyRate).filter(
    SourceDailyRate.source == "hana",
    SourceDailyRate.asset == "usd-krw",
    SourceDailyRate.date_kst.in_([
        date(2026, 5, 21),
        date(2026, 5, 22),
        date(2026, 5, 26),
        date(2026, 5, 27),
    ]),
).delete(synchronize_session=False)
db.commit()
```

**주의: range delete (`delete_range`) 사용 시 비연속 expected_dates 사이 휴일 gap 안 (5/22 ~ 5/26 gap의 5/23 등) 다른 Hana row가 있으면 같이 삭제될 수 있음. 개별 `date_kst.in_()` delete가 정확.**

**Codex Non-blocker 2개 (first PR 영향 0, follow-up 영역)**:

- published_at value validation은 write path post-write에는 없고 dry-run 쪽에 강함. production verify query 시 자연 표시 권장.
- range가 휴일로 시작 시 basis_date가 start_date보다 앞선 날짜 적재 가능 (canonical date 정책상 정상). 운영 runbook anchor 권장 — calendar range는 가능한 영업일 포함 범위로 잡기.

**후속 작업**:

- Stage 2 (push to origin/master): 별 GO
- Stage 3 (production RDS execution): 별 GO + manual snapshot 생략 + `--allow-production-write` 명시
- Hana JPY/EUR 진입 (3통화 다각화) — first PR 안정성 확인 후
- Bithumb Step 3 first PR — Hana 안정성 확인 후 (24h candle KST 00:00 anchor 패턴, source_ohlc — Hana와 다른 path)
- Step 4 full backfill (모든 source × 1년+ range)
- Step 5 daily append (scheduled cron)
- Step 6 v2 endpoint switch (Phase 2e)

### Phase 2d Step 3 Bithumb first PR Round 1 — USDT/KRW partial backfill writer (2026-05-29)

> ⚠️ [2026-06-02 Step 4A supersede] 본 섹션의 "24/7 연속이라 range delete 안전" 표현(아래 다수 위치)은 일반 정책으로는 부정확 — range delete 안전은 24/7 연속성(내부 휴일 gap 부재)이 아니라 **gap-only write 직전 확인 + 교집합 historical/manual write 부재 + 동일 작업 창 내 즉시 rollback**일 때만 성립. 작업 종료 후엔 snapshot 복구/재검증 필요. 수치·맥락은 history 보존, 최신 Step 4A change-log anchor(2026-06-02) 참조.

ADR-034 §14 Rollout step 3 — **Bithumb** first PR. KRX Step 3 first/옵션 B + Hana first PR Round 1 land 후 source diversity 마지막 핵심 path. **Stage 1 (commit `fba60d6`) + Stage 2 (origin/master push) + Stage 3 (production RDS execution) 모두 land 완료** (2026-05-29 KST, **manual snapshot 생략** — Bithumb incremental write 자동 backup 1 Day + `delete_range` rollback anchor 신뢰, 24/7 연속이라 range delete 안전). Stage 3 production verify: **Bithumb USDT/KRW 7 rows committed (2026-05-21 ~ 05-27 연속) + post-write validations passed + drift 0 + 모든 top-level metadata None path production-level 활성 + all candle_ts_kst present** (KST 00:00 anchor 정책 격리 잠금).

**ADR-034 §3 schema의 3 직교 path 모두 production-level 활성 완료**:
- KRX: `source_ohlc` + `contract_code` 필수 + chain rollover
- Hana: `close_only` + `basis_date`/`published_at`/`pbldSqn` 필수
- Bithumb: `source_ohlc` + 모든 top-level metadata None + candle_ts metadata_json 격리

**변경 파일** (Stage 1+2+3 land 완료):

- `scripts/backfill_bithumb_source_daily_rates.py` 확장 (Step 2 dry-run script에 write mode 추가):
  - `--write` flag (default off, dry-run only safe)
  - `--start-date` / `--end-date` (date_kst range filter, --write 시 필수)
  - `--include-today` (default off, 24h candle 마감 전 미확정 위험 회피)
  - `--allow-production-write` (default off, non-SQLite DB 차단)
  - 신규 함수:
    - `check_production_write_guard` (KRX/Hana writer 패턴 재사용 — dialect/host redacted)
    - `validate_write_range` (Bithumb은 24/7 거래라 휴일 처리 없음 → 단순 range + today 가드)
    - `ensure_source_daily_rates_table_created` (idempotent)
    - `write_with_transaction_bithumb` (Bithumb 방향 metadata policy — 모든 top-level metadata None / candle_ts_ms + candle_ts_kst metadata_json 격리)
  - `_date_arg` ISO YYYY-MM-DD validator (KRX/Hana 패턴 재사용)
  - `typing.Optional` import 추가

**First PR scope (옵션 A — USDT/KRW only / 짧은 range)**:

- Asset: **USDT/KRW only** (Bithumb 1 currency pair)
- date_kst range: `[2026-05-21, 2026-05-27]` (calendar 7일)
- Expected rows: **7** (Bithumb 24/7 거래 — 휴일 dedup 없음, calendar = expected 정확 일치)

**Bithumb writer vs KRX/Hana writer 패턴 차이**:

| 항목 | KRX writer | Hana writer | **Bithumb writer** |
| --- | --- | --- | --- |
| Fetch 단위 | 1 contract chain (3 contracts) | 1 calendar day = 1 HTTP | **단일 fetch (1 HTTP call로 전체 candles)** |
| Loop | Contract chain loop | Calendar-day loop | **fetch all + date range filter** |
| Dedup | contract_code IN | basis_date (휴일 fallback) | **없음** (24/7 거래) |
| Metadata policy | contract_code NOT NULL / basis_date+published_at NULL | contract_code NULL / basis_date+published_at+pbldSqn NOT NULL | **모든 top-level metadata None** + candle_ts metadata_json 격리 |
| post-write SELECT | `contract_code.in_()` + range | `date_kst.in_(expected_dates)` | `date_kst.in_(expected_dates)` (Hana 패턴) |
| Calendar 연속성 | 영업일만 | 영업일 dedup | **24/7 연속** (`end-start+1 == expected_count`) |

**Codex Round 1 보정 (Blocker 1 + Non-blocker 2)**:

- **Blocker — `--write + --limit` hard reject**: 운영자 실수로 `--write --limit N` 실행 시 partial write 위험 (limit 적용된 rows에서 date range filter → expected의 일부만 적재). production guard 근처 early check (fetch 전)에 hard reject 추가 — `[CONFIG 실패] --write와 --limit는 함께 사용할 수 없음 (partial write 위험)`.
- **Non-blocker 1 — candle_ts_kst validation**: post-write에 `metadata_json.candle_ts_ms` 외 **`metadata_json.candle_ts_kst`도 검증** 추가 (KST 00:00 anchor 정책 핵심).
- **Non-blocker 2 — 연속성 회귀 가드**: `expected_count == (end_date - start_date).days + 1` 명시 검증 (Bithumb 24/7 거래 + 휴일 dedup 없음 anchor — 7일 range = 7 rows 정확 일치).
- (cleanup) `InvalidOperation` unused import 제거 — `ArithmeticError`가 superclass라 자동 catch.

**Local SQLite smoke 6단계 모두 PASS**:

| Step | 결과 |
| --- | --- |
| 1. py_compile | OK |
| 2. dry-run regression (`--limit 5` 단순 검증) | exit 0 |
| 3. `--write` without override (production guard 차단) | exit 1, dialect=postgresql host=*** redacted |
| 4. **`--write + --limit` hard reject** (Codex Blocker 검증) | exit 1, `[CONFIG 실패] partial write 위험` |
| 5. cleanup + first smoke + 재실행 | exit 0, **7 rows committed + post-write validations passed** |
| 6. DB query 직접 검증 | 7 rows / `[2026-05-21, 2026-05-27]` / **all candle_ts_kst present** / drift 0 / 모든 top-level metadata None |

**핵심 발견 (Step 3+ 정책 anchor)**:

- **모든 top-level metadata None path 첫 production-level 활성** — KRX (contract_code 필수) / Hana (basis_date+published_at+pbldSqn 필수)와 다른 가장 단순 row mapping path 검증
- **candle_ts_ms / candle_ts_kst metadata_json 격리 정책 적용** — published_at에 candle timestamp 넣지 않는 schema 의미 보존 (KST 00:00 anchor 정책 핵심)
- **24/7 연속성 정확 검증** — calendar 7일 = expected 7 rows (휴일 dedup 0, KRX 영업일 + Hana 휴일 fallback과 다른 패턴)
- **`--write + --limit` partial write 위험 잠금** — production guard 근처 early hard reject (Codex Round 1 Blocker)
- post-write SELECT `date_kst.in_(expected_dates)` 채택 (Hana 패턴 재사용) — future overlap/partial rerun 안전 + range delete도 안전 (24/7 연속이라 휴일 gap 없음)

**Stage 분리 (KRX/Hana first PR 패턴 동일)**:

- **Stage 1+2+3 모두 land 완료** (2026-05-29 KST)
- Stage 1: write logic + production guard + fetch all + date range filter + transaction + 6-단계 SQLite smoke 검증 — commit `fba60d6`
- Stage 2: origin/master push 완료
- Stage 3: production RDS execution 완료 (`--start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write`, **manual snapshot 생략** — Bithumb incremental write 자동 backup + `delete_range` rollback anchor 신뢰, 24/7 연속이라 range delete 안전)
  - 절차: SSH → git pull (15ef873 → fba60d6 fast-forward) → docker compose build fastapi (image sha `11a8bd54ccc8...`) → `docker compose run --rm fastapi python scripts/backfill_bithumb_source_daily_rates.py --write --start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write` → post-write 검증
  - production verify: 7 rows committed (Bithumb usdt-krw `[2026-05-21, 2026-05-27]` 연속) + post-write validations passed + drift 0 + 모든 top-level metadata None + all candle_ts_kst present
- 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역 (현재 source_daily_rates read 호출자 0 — user-facing 영향 0)
- 전체 production source_daily_rates state: **KRX 25 rows + Hana 4 rows + Bithumb 7 rows = total 36 rows**

**Rollback anchor (Bithumb-specific, 24/7 연속이라 range delete 안전)** [정정 2026-06-02 Step 4A anchor: 이 줄은 당시 7-row incremental 기록(history 보존). "24/7 연속 = range delete 안전"이라는 일반 표현은 supersede됨 — 24/7 연속성은 내부 휴일 gap 부재일 뿐이고, range delete 안전은 별개로 gap-only write 직전 확인 + 교집합 historical/manual write 부재 + 동일 작업 창 내 즉시 rollback일 때만 허용. 작업 종료 후엔 snapshot 복구/재검증 필요]:

```python
from app.database import SessionLocal
from app.source_daily_rates import delete_range
from datetime import date
db = SessionLocal()
delete_range(db, "bithumb", "usdt-krw", date(2026, 5, 21), date(2026, 5, 27))
```

**KRX/Hana는 비연속 expected_dates이라 개별 dates `IN` delete 필요했으나, Bithumb은 24/7 거래로 expected_dates 연속이라 range delete 안전.**

**후속 작업**:

- Stage 2 (push to origin/master): 별 GO
- Stage 3 (production RDS execution): 별 GO + manual snapshot 생략 + `--allow-production-write` 명시
- Hana JPY/EUR 진입 (3통화 다각화) — Bithumb first PR 안정성 확인 후
- Step 4 full backfill (모든 source × 1년+ range)
- Step 5 daily append (scheduled cron)
- Step 6 v2 endpoint switch (Phase 2e)

### Phase 2d Step 5 daily append minimum first PR Stage 1 — Bithumb only orchestrator (2026-05-29)

ADR-034 §14 Rollout step 5 (daily append job) **minimum viable first PR**. KRX/Hana/Bithumb 3 source partial backfill production land 후 ongoing freshness 확보 단계 진입. **Stage 1 (commit `78059c9`) + Stage 2 (origin/master push) + Stage 3 (manual smoke production write + OS crontab 등록) 모두 land 완료** (2026-05-29 KST). Stage 3 production verify: **manual smoke 2026-05-28 Bithumb 1 row 적재 + post-write validations passed + drift 0 + candle_ts_kst KST anchor 정확** (`bithumb: PASS (1 rows committed, expected=1, exit=0)`). **OS crontab 등록 완료**: `1 15 * * *` (UTC 15:01 = KST 00:01 매일) — 기존 `monday_reopen_exec.sh` cron 보존 + daily append line 추가. 7일 안정 운영 모니터링 진입 (cron 첫 발화: 2026-05-29 UTC 15:01).

**Scope 분리 정책 (Codex 합의)**:

- **첫 PR scope = Bithumb only** (24h candle close, 적재 anchor: 다음날 00:01 KST)
- **Hana 별 PR (별도 진행)**: `hana_observed_eod` 정책 진입 필요 — 기존 backfill_hana_source_daily_rates.py official endpoint writer 재사용 금지 (schema 위반 위험, ADR-034 §6 + ADR-033 Amendment 후속). 사전 read 5 항목 필요: bank_exchange_rates schema / Hana source naming / 전일 마지막 관측값 추출 기준 / source_method enum / missing day 처리.
- **KRX 별 PR (별도 진행)**: close finalizer 통합 — CF 15:45 KST 종가 직후 trigger 정책 + 운영 close finalizer 정책 연계 필요. 단순 cron으로 분리 부적절.

**변경 파일** (Stage 1 land 대상):

- `scripts/daily_append_source_daily_rates.py` 신규 — orchestrator script:
  - argparse: `--source bithumb` (choices=["bithumb"], 첫 PR scope 잠금) / `--date YYYY-MM-DD` (default yesterday KST) / `--write` (default dry-run = command preview only, subprocess 실행 X) / `--allow-production-write` (writer subprocess에 forward)
  - dry-run mode: 3 command preview (writer full dry-run / local write / production write — operator UX, production cron 진입 전 차이 확인)
  - write mode: 기존 `backfill_bithumb_source_daily_rates.py` subprocess 호출 + capture + structured summary
  - subprocess timeout: 180초/source
  - subprocess cwd: `REPO_ROOT` (production cron robustness)
  - script_path 절대경로 보장 (`.resolve()` 명시)
  - row count stdout regex parse (`r"(\d+) rows committed"`)
  - **Codex Round 1 Blocker 정정**: parse_row_count → `Optional[int]` (regex 매칭 실패 시 None, 0과 명확 구분) + `EXPECTED_ROWS_PER_SOURCE = {"bithumb": 1}` + summary `returncode == 0 AND rows == expected_rows` 양방향 검증 → daily append freshness silent failure 차단

**Local SQLite smoke 5단계 모두 PASS** (Codex Round 1 보정 후):

| Step | 결과 |
| --- | --- |
| 1. py_compile | OK |
| 2. dry-run preview (default yesterday KST 2026-05-28) | 3 command preview 출력, subprocess 실행 X, exit 0 |
| 3. cleanup + write mode (`--write --date 2026-05-28`) | `bithumb: PASS (1 rows committed, expected=1, exit=0)`, [PASS] 모든 source 성공 |
| 4. **silent failure 시뮬** (--date 2020-01-01, Bithumb candle 미존재) | writer SKIP → exit 1 → orchestrator `bithumb: FAIL (unparsed rows committed, expected=1, exit=1, exit_fail)` + 실제 orchestrator exit 1 (Codex 별도 검증) |
| 5. argparse validation (--source hana) | `invalid choice: 'hana' (choose from bithumb)` — 첫 PR scope 잠금 |

**핵심 발견 (Codex Round 1 Blocker 정정 효과)**:

- **freshness silent failure 차단** — `returncode == 0` + `rows == expected_rows` 양방향 검증 진입. writer stdout format 변경 / capture 깨짐 시 → exit 0 + parse 실패 (None) → orchestrator FAIL 표기.
- **operator UX 강화** — dry-run mode에서 3 command preview (validation / local write / production write), production cron 진입 전 명시적 차이 확인.
- **REPO_ROOT cwd + script_path absolute** — production cron 어떤 실행 위치에서도 robustness.
- **첫 PR scope 잠금** — argparse choices=["bithumb"] — Hana/KRX 진입 시 enum 확장 자연.

**writer 재사용 효과**:

- 신규 write logic 0 — 기존 backfill_bithumb_source_daily_rates.py 의 production guard + transaction + post-write validation 7개 그대로 활용
- daily append (`--date today-1` range) = 기존 writer의 1-day range 호출과 동등
- idempotent — unique key 자연 + COALESCE-style set_ 조건부

**Codex Round 1 보정 history**:

- Blocker (rows mismatch silent failure 차단): parse_row_count Optional[int] + EXPECTED_ROWS + summary 양방향 검증
- Non-blocker 1 (dry-run 문구): "validation command" → "writer full dry-run command" + 주의 문구 (range는 target-date 한정 X)
- Non-blocker 2 (subprocess cwd): `cwd=REPO_ROOT` 추가
- Non-blocker 3 (script_path absolute): `.resolve()` 명시 (cron 환경 robustness)

**Stage 분리 (KRX/Hana/Bithumb first PR 패턴 동일)**:

- **Stage 1**: orchestrator script + smoke 5단계 검증 — **본 commit 대상**
- **Stage 2**: origin/master push — **별 GO 대기**
- **Stage 3**: production cron 등록 — **별 GO 대기** (실배포 cron line: `1 15 * * * cd ~/exchange-rate && /usr/bin/docker compose run --rm fastapi python scripts/daily_append_source_daily_rates.py --write --allow-production-write` — EC2 timezone UTC라 `1 15`=KST 00:01, `/usr/bin/docker` cron PATH 안전. Bithumb default(적재 source 미지정). **PR A2에서 `--source all`로 교체**)

**Stage 3 production cron 등록 절차 anchor**:

```bash
# 1. SSH ubuntu@<production-ec2>
# 2. crontab -e (또는 별 systemd timer)
# 3. cron line 추가 (Step 5 first PR 실배포 형태 — Bithumb default, EC2 timezone UTC라 1 15 = KST 00:01):
1 15 * * * cd ~/exchange-rate && /usr/bin/docker compose run --rm fastapi python scripts/daily_append_source_daily_rates.py --write --allow-production-write >> ~/logs/daily_append.log 2>&1
# 4. 7일 안정 운영 모니터링 (drift 0 / 1 row append every day)
# 5. retry/backoff/alert는 운영 데이터 기반 후속 PR
#
# [PR A2] 위 한 줄을 아래 --source all (bithumb + hana) 한 줄로 *교체* (중복 금지).
#   별 GO (production write + 설정 변경). **통합 관찰 정책**: 6/2 Bithumb 단독 검증 대기 불요 —
#   고정 날짜 idempotent pre-smoke로 A2 loop 선검증 후 교체 → 6/2 자연 발화에서 amendment + A2 통합 관찰.
#   (Bithumb 명령은 A2에서 무변경이라 amendment 검증 보존 + 사후 source_method query로 독립 분리 가능)
#   절차: git pull → docker compose build fastapi (one-shot 이미지 rebuild load-bearing) →
#   --source all dry-run preview → manual smoke (--date 2026-05-28 idempotent) → crontab 교체 →
#   중복 line 확인 → 다음 발화 source별 분리 검증 (Bithumb api row / Hana observed_eod row).
1 15 * * * cd ~/exchange-rate && /usr/bin/docker compose run --rm fastapi python scripts/daily_append_source_daily_rates.py --source all --write --allow-production-write >> ~/logs/daily_append.log 2>&1
```

**후속 작업 (Step 5 first PR land 후)**:

- Stage 2 (push to origin/master): 별 GO
- Stage 3 (production cron 등록): 별 GO + 운영 cron line 추가 + 7일 안정 운영 모니터링
- **Hana observed_eod 별 PR**: 사전 read 5 항목 (bank_exchange_rates schema / Hana source naming / 전일 last 추출 기준 / source_method enum / missing day) + 신 writer 작성 + orchestrator `--source choices` 확장
- **KRX 별 PR**: close finalizer 통합 (CF 15:45 KST 종가 직후 trigger) + orchestrator choices 확장
- **retry/backoff/structured error/alert** (운영 데이터 기반 단계적 강화)
- **monitoring**: drift 0 / row count / freshness staleness 감지 (admin endpoint or Slack)
- **calendar-aware missing row alert** (ADR-034 §13)
- **Step 4 full backfill** — Hana observed_eod + Bithumb 902일 + KRX chain 깊이 확장 (Step 5 daily append 안정 운영 base 위에서)
- **Step 6 v2 endpoint switch** (Phase 2e + 장기 그래프 DB 표준화 ADR-035 결정 — `source_hourly_rates` 신 table 도입 여부)

### Phase 2d Bithumb provenance Amendment — source_method rename (`bithumb_candlestick_backfill` → `bithumb_candlestick_api`) (2026-06-01)

ADR-034 §6/§7/§9 Amendment. PR A1 cron 검증 중 발견된 provenance 불일치 정정. **Stage 1+2+3 land 완료** (2026-06-01 — production RDS 11 rows surgical rename old 0→new 11 / drift 0 / 다음 cron 적재 검증 **완료** — 2026-06-02 00:01 KST(= 6/1 15:01 UTC) 발화가 2026-06-01 row를 `bithumb_candlestick_api`로 적재).

**배경 (불일치)**: ADR §7/§9는 Bithumb daily append를 `observed_rollup`(DB source_rates rollup)으로 규정했으나, 구현(Step 5 daily append)은 candlestick backfill writer를 재사용 → 실제 backfill·append 모두 **공식 24h candle API**. 즉 ADR 설계 ≠ 구현. **데이터는 정확** (candle 실 OHLC가 DB rollup보다 우월), provenance **label**(`bithumb_candlestick_backfill` = "backfill" 함의)만 부정확.

**결정 (옵션 a — Codex 협업 7 round)**:
- canonical 획득 방식 = **공식 24h candle API** (DB rollup writer 신규 구현 ❌ — 더 나쁜 데이터를 label에 맞추는 역방향).
- `source_method` = **`bithumb_candlestick_api`** (단일 값). source_method=획득 **방법** → Bithumb은 backfill·daily 동일 방법이므로 하나. backfill/daily 구분은 **timing**(실행 목적)이지 방법 아님.
- `ohlc_quality`=`source_ohlc` / `close_basis`=`bithumb_24h_kst_close` **유지**. enum 5개 구조 유지, 값 하나만 rename.
- **`ingest_mode` metadata 미도입** (YAGNI — backfill/daily는 목적이지 방법, v2 consumer 불요, metadata upsert replace로 소실 위험. 감사 요구 시 writer+flag 함께 별도).
- "24h" method명 중복 회피 (close_basis에 이미 24h).

**변경 파일 (Stage 1)**:
- `scripts/backfill_bithumb_source_daily_rates.py`: `SOURCE_METHOD = "bithumb_candlestick_api"` 상수 신설 → build_row + post-write validator 공유 (literal divergence 방지) + docstring(dry-run only stale) 정리
- `app/models.py` SourceDailyRate docstring: Bithumb append = candle API (source_rates rollup 폐기)
- 문서: DECISIONS §6(enum/표)/§7/§9 + GRAPH §6/§7 + CLAUDE Amendment anchor — rename + Bithumb=candlestick + DB rollup 문구 제거 + "backfill/append 분기"를 KRX 한정 구분. **과거 history line**(Bithumb Step 3 land)은 당시 사실이므로 rewrite ❌, 본 Amendment가 supersede.
- `scripts/migrate_bithumb_source_method.py` 신규 + `tests/test_migrate_bithumb_source_method.py` (회귀 13 — Codex 7종 + 추가 3 + fail-open count 방어 3)

**Migration 계약 (Codex 7 round)**: dry-run default / `--write --expected-old-count N --expected-new-count M --allow-production-write` (강한 dialect guard — 기존 migrate 약한 guard 복제 ❌). transaction: old method FOR UPDATE lock → count 재확인(expected 비교) → snapshot → source_method만 UPDATE → rowcount==N → **source_method 외 전 컬럼 불변 surgical 검증** → post-verify(old_after=0 / new_after=M+N / total_after=total_before) → mismatch rollback. idempotent: old=0 AND new=M+N → SKIP / old=0 AND new≠M+N → stale ABORT.

**배포 순서 (운영 race 절차 차단 — FOR UPDATE는 신규 insert 못 막음)**:
1. 코드 commit/push → EC2 git pull + `docker compose build fastapi` (신코드 배포 = 이후 cron이 `bithumb_candlestick_api` 기록 → old count 안정)
2. 직전 cron 완료 로그 + daily_append one-shot 컨테이너 미실행 확인 + **15:01 UTC(cron) 충분히 회피**
3. read-only pre-query(N/M) → migration(`--expected-old N --expected-new M`) → post-query
4. 다음 cron 1회 새 method 유지 확인

**가격/OHLC/metadata/captured_at 변경 0 — provenance `source_method` 문자열만 UPDATE** (production 11~12 rows surgical rename).

**Local test**: migration 13 (Codex 7종 + 추가 3 + fail-open count 방어 3) + Bithumb writer rename(literal 0, 상수=bithumb_candlestick_api). non-urgent (v2 그래프 미출시) — A2 전 provenance 정리로 Hana(observed_rollup)/Bithumb(candlestick_api) 분리 명확.

### Phase 2d PR A1 — Hana daily append 자동화 foundation (calendar + JSON verdict 계약) (2026-05-31)

ADR-034 §14 Rollout step 5 후속 — Hana USD daily append 자동화를 위한 **계약/calendar foundation**. cron line 변경 없이 writer verdict 계약 + 한국 공휴일 calendar 도입. 운영 cron 전환(`--source all`)은 **PR A2**. **Stage 1 (commit `a7b128d`) + Stage 2 (origin/master push) + Stage 3 (EC2 build + in-image 검증 + Bithumb manual smoke) 모두 land 완료** (2026-05-31). Stage 3 deploy verify: in-image `holidays.__version__==0.97` + 대체공휴일 fixture (5/25·3/2 True / 5/1 근로자의날 True / 12/31 False) + Bithumb JSON manual smoke PASS (`{written,rows:1}` → orchestrator PASS) + production 40 rows idempotent 무결. **cron line 변경 0** → live container recreate 없이 다음 발화(2026-06-01 00:01 KST)부터 JSON verdict 경로 자동 사용. 첫 JSON cron 발화 **PASS** (2026-06-01 00:01 KST cron이 Bithumb 5/31 신규 row를 JSON verdict 경로로 적재 — `{written,rows:1}` → orchestrator PASS, total 40→41, 신규 insert 검증 완료). **A1 운영 검증 완전 closure.**

**배경**: 기존 orchestrator는 `parse_row_count` regex + Bithumb 고정 1-row 가정. Hana는 주말·공휴일 0 row가 정상이라 이 계약으로 통합 불가 → source-aware JSON verdict 계약 필요. + 기존 Hana writer는 공휴일을 평일로 취급(`weekday_no_changes` 오판) → 한국 공휴일 calendar 필요.

**변경 파일**:

- `app/calendars/{__init__, kr_holidays, hana_business_days}.py` (신규): `holidays.SouthKorea(categories=(PUBLIC,BANK), observed=True)` + `classify_hana_calendar_day` 단일 진실 소스 (weekend > holiday > business_day)
- `app/daily_append_verdict.py` (신규): verdict 계약 공유 단일 소스 (sentinel emit / extract / 상수)
- `scripts/backfill_hana_observed_eod_*.py`: calendar 통합 (`holiday_no_changes`→skip_ok / `business_day_no_changes`→skip_error) + calendar_class 3-way + `--emit-daily-append-verdict`
- `scripts/backfill_bithumb_*.py`: `--emit-daily-append-verdict` (success 경로 sentinel)
- `scripts/daily_append_*.py` (orchestrator): regex → JSON verdict fail-closed 판정 + source-aware policy
- `requirements.txt` / `requirements.lock.txt`: `holidays>=0.97,<1.0.0` / `holidays==0.97` (dateutil 기존, churn 0)

**verdict 계약**: `DAILY_APPEND_VERDICT_JSON={version,source,asset,date_kst,status,reason,rows}` sentinel. writer action → status 매핑 (write→written / skip_ok→skipped / skip_error→error). orchestrator fail-closed: exit0→sentinel 정확히 1개 + JSON object + source-aware schema + status×rows / nonzero→FAIL.

**source-aware policy**: bithumb(24/7) = written only / asset usdt-krw / hana = written|skipped / asset usd-krw.

**holiday calendar**: **observed=True load-bearing** (observed=False면 5/25 대체공휴일·3/2 삼일절 대체 등 drop — 실측 확인) + PUBLIC∪BANK (BANK 전용 = 근로자의날 5/1). **12/31은 한국 공휴일 아님 — 정상 영업일** (사용자 정정). override hook 미도입 (YAGNI — 임시공휴일 발생 시 추가).

**case 매트릭스 (갱신)**: changes>=1 → write(written) / 평일·비공휴일 changes==0 → skip_error(business_day_no_changes, FAIL) / 주말 changes==0 → skip_ok(weekend_no_changes) / **공휴일 changes==0 → skip_ok(holiday_no_changes)** (신규).

**Codex review**: 설계 6 round 수렴 + 구현 후 adversarial Blocker 4 + NB (모두 재현 확인 후 fix):

- B1 non-object JSON(`[]`/null/str) → crash 대신 FAIL (`isinstance(v, dict)` 가드)
- B2 asset 미검증 → source별 asset 검증
- B3 Bithumb skipped 허용(freshness guard 약화) → source-aware `allowed_statuses` (bithumb written only)
- B4 bool/str rows(`True==1` 통과) → `type(rows) is int`
- NB written → `reason is None` 강제

**Local test**: **59 PASS** (calendar 9 + Hana writer 22 + orchestrator 28). Codex 재현 입력(missing fields / unsupported source / negative·float rows / malformed sentinel) 전부 FAIL 잠금 + CLI guard subprocess test (emit-without-write / multi-day+emit) + Bithumb end-to-end (orchestrator→writer→verdict→PASS).

**Stage 분리**:

- Stage 1: foundation + test — **commit `a7b128d` 완료**
- Stage 2: origin/master push — **완료** (`3365c1b..a7b128d`)
- Stage 3: EC2 git pull (a7b128d) + `docker compose build fastapi` (새 이미지) + **in-image `holidays.__version__==0.97` + 대체공휴일 fixture 확인 완료** + Bithumb JSON manual smoke PASS + production 40 rows idempotent 무결 — **완료** (2026-05-31). **cron 1회 검증 완료 (2026-06-01 00:01 KST)**: 신규 insert Bithumb 5/31 row JSON verdict 경로 적재 `{written,rows:1}` → orchestrator PASS, total 40→41 — A1 운영 검증 완전 closure
- **PR A2**: `--source bithumb|hana|all` + per-source 예외 격리 + aggregate exit + cron line `--source all` 교체 (Hana 실패가 Bithumb 막지 않음)

**Open (후속)**: ✅ ~~KRX stub(`KRX_2026_KNOWN_HOLIDAYS`)을 공유 calendar base로 migration~~ → **완료 (2026-06-07)** — `is_krx_business_day`를 kr_holidays + KRX 연말 폐장 wrapper로 교체. / crawler-success heartbeat (평일 liveness 정확화) / 임시공휴일 override (라이브러리 lag 시). [Hana production cron 포함 = **PR A2 완료** — cron `--source all` 교체 deploy 2026-06-01 + 2026-06-02 00:01 KST 통합 관찰 41→43]

### Phase 2d Step 3 Hana observed_eod first PR Round 1 — bank_exchange_rates observed_eod writer (2026-05-31)

ADR-034 §14 Rollout step 3 — **Hana observed_eod 별 PR** (Step 5 daily append에서 분리된 canonical append path). 기존 `backfill_hana_source_daily_rates.py` (official_historical_backfill)와 **별 path** — close_basis / source_method / ohlc_quality 모두 다름. 외부 endpoint 미사용, 우리 DB `bank_exchange_rates` 내부 관측값 read. **Stage 1 (commit `e7213bf`) + Stage 2 (origin/master push) + Stage 3 (production RDS execution) 모두 land 완료** (2026-05-31 KST, manual snapshot 생략 — incremental single row + `date_kst.in_()` delete rollback anchor 신뢰). Stage 3 production verify (이중 독립 — write 직후 + 별 read-only query): **Hana usd-krw 2026-05-28 1 row committed + post-write validations passed + drift 0 + overlap 0** (close=rate=1496.1 / high=1510.8 / low=1494.3 / baseline_included=True / day_change_count=1096 / rollup_point_count=1097 / nullable contract_code·basis_date·published_at 모두 None / close_basis hana_observed_eod). hana/usd-krw = 5 (official_historical_backfill 4 `[5/21,22,26,27]` + observed_eod 1 `[5/28]`, corruption 0).

**문서 anchor**: GRAPH_API_V2_CONTRACT.md §7 (`hana_observed_eod` = KST 해당일 24:00 이전 마지막 관측 Hana 고시값) + ADR-034 §6/§7 (close_basis `hana_observed_eod` / source_method `observed_rollup` / ohlc_quality `observed_rollup`) + §13 (한국 은행 영업일 expected calendar).

**변경 파일** (Stage 1 land 대상, 둘 다 신규):

- `scripts/backfill_hana_observed_eod_source_daily_rates.py` 신규 — observed_eod writer:
  - dry-run (bank_exchange_rates read only) / write (calendar-day range) / production guard (dialect) — 4 anchor 재사용 (KRX/Hana official/Bithumb writer 패턴)
  - 핵심 신규 로직: KST→UTC naive boundary 변환 + carry-in baseline (prev) + 당일 changes rollup
- `tests/test_hana_observed_eod_writer.py` 신규 — 영구 회귀 16 test (in-memory SQLite + `patch` 주입, repo unittest 컨벤션)

**First PR scope (옵션 A — overlap-free single row)**:

- Currency: **USD only** (JPY/EUR 후속 PR)
- date_kst: **2026-05-28 단일 row** (official_historical_backfill 4 rows `[5/21,22,26,27]`와 overlap 0 — overlap 회피)
- production guard: dialect 검사 + `--allow-production-write` 필요

**핵심 설계 (Codex 협업 8 round 수렴)**:

- **bank_exchange_rates = change-only table** (insert_bank_rates_into_db: rate 변경 시에만 INSERT). 하루 값 불변이면 그날 row 0개 가능.
- **liveness gate ⊥ baseline quality 직교 분리**:
  - changes 존재 여부 = write 가능 여부 (liveness gate)
  - prev (00:00 직전 마지막 관측) 신선도 = high/low rollup 포함 여부만 (write gate 아님)
- **rollup**: rollup_rows = ([prev] if baseline_ok else []) + changes. close=rate=changes[-1] (invariant). high/low=rollup max/min. baseline_ok = prev 존재 AND age(utc_start - prev.timestamp) <= 7d.
- **case 매트릭스** (A1 calendar 통합 — classify_hana_calendar_day): changes>=1 → write (영업일/주말/공휴일 무관) / 영업일(비공휴일) changes==0 → skip_error (exit 1, `business_day_no_changes`) / 공휴일 changes==0 → skip_ok (exit 0, `holiday_no_changes`) / 주말 changes==0 → skip_ok (exit 0, `weekend_no_changes`).
- nullable: contract_code / basis_date / published_at 모두 None (observed_eod 방향 — 외부 발표/기준일 개념 없음).
- metadata_json: rollup provenance (rollup_mode / calendar_class / liveness_evidence / baseline_included / baseline_exclusion_reason / carry_in_age_at_start_seconds / close_raw_row_id / rollup_point_count 등).

**Hana official_backfill vs observed_eod writer 차이**:

| 항목 | official_backfill | observed_eod |
| --- | --- | --- |
| Source | Hana official endpoint (외부) | bank_exchange_rates (내부 DB) |
| close_basis | hana_official_historical_backfill | hana_observed_eod |
| source_method | external_backfill | observed_rollup |
| ohlc_quality | close_only (high=low=close synthetic) | observed_rollup (rollup max/min) |
| basis_date / published_at | NOT NULL (회차 provenance) | None |

**Codex review (설계 6 round + write-path Blocker 2 round)**:

- 설계 수렴: basis_date/published_at None (§3 정의 + §10 nullable preserve) / age=day-start baseline 신선도 (write gate 아닌 rollup 포함 gate) / 평일 changes==0 skip+error (§13 영업일 + 장애 surface) / 주말 무변동 정상 skip.
- **write-path Blocker 2건** (Codex temp SQLite 재현 + 자기 코드 직접 trace 확인):
  - **B1 range atomicity**: 실패 range 부분 commit → skip_error 시 transaction 전 abort (all-or-nothing).
  - **B2 write-path validation 우회**: 음수/precision raw commit → DRY_RUN_CHECKS를 transaction 전 적용 (pre-write abort).
  - N1 metadata fail-closed (필수 key 누락 시 rollback) / N2 dry-run 이중 query 정리 (prefetch 재사용).
- 영구 test 추가 (Codex 권장): metadata 필수 key rollback + non-SQLite guard reject.

**Local SQLite smoke 11 case + 영구 unittest 16 PASS**:

- /tmp smoke 11 case (process_date + write subprocess + B1/B2 재현 + production guard) — 58 check PASS
- `tests/test_hana_observed_eod_writer.py` 16 test: process_date 6 / write_transaction 4 (commit / idempotent / **official overlap rollback 안전망** / metadata fail-closed) / `_run_write` 3 (B1 atomicity / B2 negative reject / clean) / production guard 3 (non-SQLite reject / allow flag / sqlite pass)

**overlap 안전망 (실측 확인)**: official row와 overlap 시 upsert nullable-preserve로 basis_date/published_at leftover → post-write nullable validation fail → rollback → official row 보존 (corruption 차단). ADR-034 §10 Open (close_basis 전환 정책 미확정) 전까지 overlap write 자동 차단. 첫 PR scope 2026-05-28은 overlap 0.

**Phase 1-3 production read-only 사전 query (2026-05-31, EC2 `docker compose run --rm fastapi`)**:

- (1) 2026-05-28 Hana USD raw change_rows: **1096** (정상 활성 평일) — first KST 00:06=1497.5 / last KST 23:56=1496.1 (→ close) / min·max 1494.3·1510.8
- carry-in baseline (prev): 2026-05-27 14:55:45 UTC, 1497.3, age 254.6s (~4분, fresh ≤ 7d)
- (2) source_daily_rates hana/usd-krw 2026-05-28 existing: **0** (overlap 없음)
- 예상 production row: close=rate=1496.1 / high=1510.8 / low=1494.3 / baseline_included=True / day_change_count=1096 / rollup_point_count=1097 / weekday

**Stage 분리**:

- **Stage 1**: writer + 영구 test 2 파일 + 본 anchor — **commit `e7213bf` 완료**
- **Stage 2**: origin/master push — **완료** (`a533ba9..e7213bf`)
- **Stage 3**: production execution — **완료** (2026-05-31 KST). 절차: EC2 `git pull` (e7213bf) → `docker compose build fastapi` (image `863c75e9`, one-shot 이미지에 신규 writer 반영 — scripts/는 image COPY, live container recreate 불필요) → dry-run 검증 (production data로 row + 7 validation 확인) → `docker compose run --rm fastapi python scripts/backfill_hana_observed_eod_source_daily_rates.py --write --start-date 2026-05-28 --end-date 2026-05-28 --allow-production-write` → 이중 독립 verify. manual snapshot 생략 (incremental write + rollback anchor).

**Rollback anchor** (단일 row, 개별 date IN delete):

```python
from app.database import SessionLocal
from app.models import SourceDailyRate
from datetime import date
db = SessionLocal()
db.query(SourceDailyRate).filter(
    SourceDailyRate.source == "hana", SourceDailyRate.asset == "usd-krw",
    SourceDailyRate.date_kst.in_([date(2026, 5, 28)]),
).delete(synchronize_session=False)
db.commit()
```

**후속 작업**:

- Hana JPY/EUR observed_eod 진입 (3통화 다각화)
- orchestrator `--source` choices 확장 (`["bithumb", "hana", "all"]`) — **PR A2 완료** (Stage 1 구현 + per-source 격리 + Stage 3 production cron `--source all` 교체 (deploy 2026-06-01, pre-smoke PASS) + 2026-06-02 00:01 KST 통합 관찰 41→43)
- holiday calendar 도입 (공휴일 평일 weekday_no_changes false-positive 회피 + age threshold business-day 전환) — **공휴일 분류는 PR A1에서 구현 완료** (holiday_no_changes → skip_ok, 본 §14 PR A1 섹션 참조). age threshold business-day 전환은 미구현 (후속)
- crawler-success heartbeat (평일 liveness_verified 정확화)
- official overlap 시 close_basis 전환 정책 (ADR-034 §10 Open) — **결정 전까지 양방향 overlap write fail-close** (2026-06-02 Step 4A Stage 1): official→observed는 Step 4A overlap guard + conditional conflict update, observed→official은 기존 observed_eod post-write nullable validation rollback safety

### Phase 2d Step 3 first land — KRX current partial backfill writer (2026-05-28)

ADR-034 §14 Rollout step 3 **first PR** — KRX current contract (A75606) partial backfill writer land. **Step 3 진입 첫 source = KRX** (ADR-034 §14 §3 잠정 — service value 기준 + KIS dry-run에서 chain/rollover/cap/stale 모두 검증 완료). **Stage 1 (commit `40279c9`) + Stage 2 (origin/master push) + Stage 3 (production RDS execution) 모두 land 완료** (2026-05-28 KST 18:30, RDS manual snapshot `fxi-pre-step3-2026-05-28` 직후 production write). Stage 3 production verify: **7 rows committed + 7 post-write validations passed + drift 0 + boundary close=1496.500 production-level 재현** (GRAPH §7-new B-B probe 사례). 운영 fastapi runtime 갱신은 Phase 2e endpoint switch 시점 별 배포 영역 (현재 source_daily_rates read 호출자 0 — user-facing 영향 0).

**변경 파일** (Stage 1 land):

- `scripts/backfill_kis_source_daily_rates.py` 확장 (Step 2-3 dry-run script에 write mode 추가):
  - `--write` flag (default off, dry-run only가 default safe)
  - `--start-date` / `--end-date` (required when `--write`)
  - `--allow-production-write` (default off — non-SQLite DB 차단)
  - 신규 함수: `check_production_write_guard()` / `validate_write_range()` / `ensure_source_daily_rates_table_created()` / `write_with_transaction()` / `_date_arg`
  - 외부 의존성 0 (기존 + `app.database.SessionLocal` + `app.models.SourceDailyRate` + `app.source_daily_rates.upsert/delete_range` 재사용)

**Step 3 first PR scope (옵션 A — single contract)**:

- KRX current contract **A75606만** (chain previous + next 제외 — 옵션 B는 후속 PR)
- date range = `[2026-05-18, 2026-05-27]` (rollover boundary 시작 ~ today-1)
- 7 영업일 row (cur_rows 7 중 range 내 7)
- DB write: `source_daily_rates` table만 (Phase 2d schema)
- DB read 호출자: **0** (v2 endpoint Phase 2e 미진입 — user-facing 영향 0)

**Production guard (Codex Round 8 — early call)**:

- 위치: main() args parse 직후 + `.env` load 직후 + KIS API call (token/master/daily fetch) **모두 전**
- 동작: `engine.url.get_dialect().name == "sqlite"` 아니면 default reject
- 출력: `dialect=postgresql host=***` redacted (URL 전체 출력 X, CLAUDE.md 보안 원칙)
- 안전 효과: production .env에서 `--write` 잘못 실행 시 **output 1 line + exit 1** (이전 사고 시 104 line + KIS API call 진행). KIS 1일 1회 발급 원칙 + token rotation risk + rate limit 영향 **모두 0**.

**Transaction pattern (Codex Round 7)**:

- `upsert(commit=False)` loop (명시적 keyword mapping — row dict 추가 키 무시, invariant `rate=close` 안전)
- post-write SELECT with **date range filter** (`date_kst BETWEEN start AND end` + source/asset/contract_code) — same contract 다른 range row 검증 혼입 차단
- 7 post-write validations:
  - exact row count `== expected_count` (`<` 검사 폐기)
  - **`expected_dates set == written_dates set` 정확 일치** (missing/extra 양방향)
  - `rate == close` invariant
  - `contract_code == current.short_code`
  - `basis_date IS NULL` / `published_at IS NULL` (KRX 패턴)
  - duplicate `date_kst` 0
  - enum/literal 회귀 가드 (`source` / `asset` / `source_method` / `close_basis` / `ohlc_quality`)
  - boundary sample — range includes 2026-05-18 시 `close == 1496.500` (GRAPH §7-new B-B probe 사례)
- 검증 통과 → `db.commit()` / 실패 → `db.rollback()` (transaction atomic, partial write 0)

**Local SQLite smoke 결과 (DATABASE_URL=sqlite:///$(pwd)/data/exchange_rates.db override)**:

| Step | 결과 |
| --- | --- |
| py_compile | OK |
| `--write` without override (production .env 상태) | exit 1, output 1 line, KIS API call 0 (Round 8 guard early) |
| SQLite override smoke first run | exit 0, **7 rows committed + 7 post-write validations passed** |
| Idempotent re-run | exit 0, 동일 결과 (unique key 자연 idempotent + COALESCE-style set_ 조건부) |
| Post-write DB query 직접 검증 | 7 rows / `[2026-05-18, 2026-05-27]` / **boundary close=1496.500 (GRAPH §7-new 재현)** / `rate==close=True` / `basis_date/published_at IS NULL` / enum/literal 모두 정확 |

**Rollback anchor** (cleanup 필요 시):

```python
from app.database import SessionLocal
from app.source_daily_rates import delete_range
from datetime import date
db = SessionLocal()
delete_range(db, "krx", "usd-krw-futures", date(2026, 5, 18), date(2026, 5, 27))
```

**Stage 분리 (Codex anchor)**:

- **Stage 1**: `--write` script + helper functions in repo (본 commit)
- **Stage 2**: origin/master push (별 GO)
- **Stage 3**: production RDS execution (별 GO + RDS backup 직전 + SSH 진입 + `--allow-production-write` 명시)
- Stage 3 진입 절차:
  1. RDS backup (`scripts/backup-db.sh` 또는 manual snapshot)
  2. SSH `ubuntu@<production-ec2>` 진입
  3. `docker compose run --rm fastapi python scripts/backfill_kis_source_daily_rates.py --write --start-date 2026-05-18 --end-date 2026-05-27 --allow-production-write`
  4. post-write validation 통과 확인
  5. drift query (`find_rate_close_drift`) 0 확인

**보정 history (Codex 8 review rounds for KIS / Round 7-8 for Step 3 writer)**:

- Round 7 (Step 3 writer 1차): 2 Blocker (production guard 신규 + post-write query date range filter + exact count + dates set 검증)
- Round 7 (사고): `.env` DATABASE_URL이 production RDS 가리킴 발견. network unreachable이 우연히 보호. local log 정리 + script-level guard 추가 필요성 확정
- Round 7 보정: `--allow-production-write` flag + dialect/host redacted + `write_with_transaction(start, end)` 시그니처 확장
- Round 8 (Codex Blocker): guard 위치가 KIS API call 후 → token rotation / master fetch / daily fetch 영향. guard를 main 진입 직후 (args parse + .env load 후, KIS keys 검사 전)로 이동
- Round 8 검증: output 104 lines → **1 line** (token/master/fetch 모두 진입 전 즉시 차단, KIS API call 0)

**Step 3 후속 작업**:

- **Stage 3 production execution** (별 GO + RDS backup 직전) — production RDS에 동일 7 rows 적재
- **Step 3 옵션 B**: chain 확장 (previous A75605 + current A75606) — 후속 PR. 이전 contract row까지 land 시 cleanup/rollback 범위 ↑이라 first PR에서 분리
- **Step 3 KRX 외 source**: Hana / Bithumb partial backfill — Step 3 first PR 안정성 확인 후 진입
- **probe out-of-range row 차단** (현재 dry-run WARNING surface): Step 3 적재 시 hard reject logic 별도 land
- **Step 4 full backfill**: 모든 contract chain + 모든 source historical 적재 (1년+ range)
- **Step 5 daily append**: scheduled job (close finalizer (KRX) / observed_eod (Hana) / Bithumb candle API daily refresh [Amendment 2026-06-01, 구 source_rates rollup]) — retry/backoff/structured error 함께 land
- **Step 6 v2 endpoint switch** (Phase 2e): catalog/tab endpoint가 source_daily_rates 조회로 전환

### 16. Related docs

- [ADR-033](#adr-033-graph-api-v2-catalog-policy--legacy-공존--hana-backfill--bithumbkrx-actual-only--dxy_futures-1d-only): Graph API v2 catalog policy + Amendment 2026-05-27 + Amendment 후속 (Decision A-E)
- [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md): §3 catalog matrix / §6 close_basis schema / §7 Hana / §7-new KRX / §8 Bithumb / §13 Phase 2d rollout
- [ADR-019](#adr-019-dxy-보조지표--granularity-기반-2-part-merge-전략): DXY granularity 2-part merge (legacy comparison)
- [ADR-023](#adr-023-데이터-보관-정책-30일-통일-banksource_ratesdxy-realtime): 30일 cap 정책 (source_rates retention과 무관 — source_daily_rates는 장기 보존)

---

## ADR-035: 장기 그래프 DB 표준화 — Investing daily canonical + source_hourly_rates(1w) + Phase 2e MVP 범위

**Status**: Proposed (2026-06-05) — Phase 2e v2 endpoint 진입 전 결정. ADR-033 Decision 10 + ADR-034 후속. **D1 Investing land 2026-06-05~06-06** (writer `4f38b05`+`a11e6f7` + production backfill 837 rows + daily append orchestrator 통합 `bef10eb` + cron 첫 발화 검증 2026-06-06; source_daily_rates 1357→**2201**). **Phase 2e MVP endpoint land 2026-06-06** (v2 graph_v2 로직 + main.py thin wiring + endpoint harness `7bc1d65`/`9d43773`/`c7c218c`, 3m/1y `source_daily_rates` read hot path, 1d/1w는 v2 400 unsupported, 18 tests; **운영 deploy 완료 2026-06-06**(HEAD 0e52b7a / image 9e2e3186, Claude+Codex 이중 독립 검증: catalog 200·tab usd 3m 200 series[77,63,79]·1d 400·v1 200·KRX status=normal·ERROR 0) — 이로써 **D2**[DXY market_index 별 reader] + **D4**[3m/1y MVP] land, **D3**[1w source_hourly_rates]는 2차 phase로 분리 — **2026-06-08~09 land**: source_hourly_rates 3 source(Bithumb/Investing/Hana) self-maintaining + v2 1w endpoint read-side deploy `88b9505`(구현 상태 §) → **D1~D4 모두 land**). (DECISIONS "장기 그래프 DB 표준화 (Phase 2e 진입 전 결정)" 노트 + [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md) §catalog 1w open question을 본 ADR로 확정.)

### Context

Phase 2d로 KRX/Hana/Bithumb의 `source_daily_rates` canonical daily table이 production land (KRX 245 / Hana 744[3통화] / Bithumb 368 = 1357 rows, 모두 1년 + 최근 observed). 그러나 **v2 그래프 endpoint(Phase 2e)가 아직 source_daily_rates를 읽지 않음** (read caller 0 — *작성 당시 기준; 2026-06-06 Phase 2e MVP land로 graph_v2가 read 구현, 운영 deploy도 2026-06-06 완료, 단 client 미연동이라 실사용 caller는 아직 0*). 진입 전 (1) 표준화 범위, (2) 1w 해상도, (3) Phase 2e MVP 범위를 본 ADR에서 결정.

현재 canonical 상태 (catalog series별):

| series | 장기(3m/1y) 위치 | 1w 위치 | 표준화 상태 |
|--------|-----------------|---------|------------|
| KRX | source_daily_rates ✅ | 없음 | daily 완료 |
| Hana | source_daily_rates ✅ (3통화) | 없음 | daily 완료 |
| Bithumb | source_daily_rates ✅ | 없음 | daily 완료 |
| DXY | market_index_rates.daily ✅ | market_index_rates.hourly ✅ | granularity 별 table (선행 사례) |
| **Investing** | **source_daily_rates ✅ (D1 land 2026-06-05)** | investing_exchange_rates (raw) | **daily 완료** (3m/1y gap 해소) |

### Decision

**D1 — Investing → source_daily_rates daily canonical (rollup)**: investing_exchange_rates(**장기 보관** — 30일 cap 무관)를 daily로 말아 source_daily_rates에 적재. KRX/Hana/Bithumb backfill보다 단순 — external API/contract chain/perishability 없이 **기존 장기 raw 기반 rollup** (investing_exchange_rates는 장기 보존이라 1y+ coverage 기대 — per-currency 실제 coverage는 구현 직전 production read 확인, Open).
- source = `investing` / asset = `usd-krw` | `jpy-krw` | `eur-krw`
- close_basis = `investing_observed_eod` (신규 enum) / source_method = `observed_rollup` / ohlc_quality = `observed_rollup`
- rate = close = 해당 KST 날짜 마지막 관측값 / high·low = 당일 관측값 max·min / contract_code·basis_date·published_at = None (Hana observed_eod 동형)
- metadata_json = {point_count, first_ts, last_ts, source_table: "investing_exchange_rates"}
- 적재 = 과거분 일괄 rollup(backfill) + going-forward daily rollup job (`dxy_rollup.py` daily rollup 선례 동형, KST 00:0X)

**D2 — DXY → market_index_rates 유지 (source_daily_rates 미이전)**: DXY는 index(instrument)지 source/asset 아님. 이미 granularity(realtime/hourly/daily) + `dxy_rollup.py` 구조 보유 → source 모델 강제 이전 시 index/source 구분 흐려짐. **v2 endpoint는 source_daily_rates(sources) + market_index_rates(DXY) 다중 table read** (read 통합 방식은 endpoint PR).

**D3 — source_hourly_rates(1w) → 도입, 단 Phase 2e MVP와 분리된 2차 phase**: 1w(7일)는 daily 7점 부족 + source_rates(30d raw)를 hot path 직접 read는 표준화 취지(external/raw hot path 제거)와 충돌 → **pre-aggregate hourly canonical 신설** (DXY hourly rollup 선례 동형).
- schema = source_daily_rates mirror (date_kst → 시간 bucket_ts_kst, OHLC + provenance) [정확 schema는 구현 PR]
- 대상 = Hana·KRX·Bithumb·Investing (DXY는 market_index_rates.hourly 보유)
- rollup source = **source별 observation provider** — 현재 구현: KRX/Bithumb=`source_rates`, Hana=`bank_exchange_rates`, Investing=`investing_exchange_rates` (DXY=`market_index_rates.hourly` 유지). **향후 Bank/Investing source/asset realtime pipeline 통합([All-source observation fanout](REALTIME_ARCHITECTURE_PLAN.md) Bank/Investing β) 후 동일 provider가 normalized observation(`source_rates`/Redis mirror)을 읽도록 전환** — canonical(source_daily/hourly_rates) schema는 **이미 source/asset 기반이라 input 경로만 변경, 미래 리팩터와 호환**
- source_hourly_rates 자체 retention = 구현 PR 확정 (1w(7일)면 충분, source별 raw table retention과 분리)
- 1w bucket = GRAPH §catalog 후보 a/b/c 중 hourly canonical 채택 (legacy hybrid 제거)
- **Phase 2e MVP 이후 별 phase** — schema + 한 source canary로 시작

**provenance (Step 2~ 구현)**:

- `source_method` = `observed_rollup` / `ohlc_quality` = `observed_rollup` — daily observed rollup 계열 재사용 (신규 enum 값 없음)
- `close_basis` = **source별 신규 hourly 값** (daily 24h/EOD close와 다른 granularity 명시). Bithumb = `bithumb_observed_hourly`, Investing = `investing_observed_hourly`, Hana = `hana_observed_hourly` 확정·운영 반영, KRX = `krx_observed_hourly` 확정 (Decision B enum 표 + §6 + GRAPH §6에 추가 — 9 values). KRX는 **session-agnostic enum** (ADR-035 D3 Step 2, 첫 PR CF-only coverage, CM follow-up도 동일 enum 재사용 — mixed/migration 회피). KRX write path는 Step 3 후속(현재 Step 2 dry-run validator만 land)
- bucket = `floor_bucket_ts_kst` (KST 시 정각 floor). `source_rates` 등 UTC naive timestamp는 `replace(tzinfo=utc)` 후 변환 (Step 1 helper 계약 — UTC naive 직접 전달 시 9h 오차)
- gap = change-only raw source는 무변동 hour에 bucket 없음 → **write는 실관측 bucket만 적재**(carry-forward 안 함 — canonical source of truth, provenance 흐림 방지). Hana 고시 step function 의미론의 carry-forward/step render는 **v2 1w endpoint read-side에서 결정** — dry-run validator가 within-span 빈 hour 분포 surface(Hana ~72/통화·14d), 적용은 endpoint render 정책 (Bithumb/Investing/Hana write 모두 실관측 일관)

**구현 상태 (Bithumb 트랙 완료 — 2026-06-08)**: Step 1 schema/helper/migration (`6e5b9bc`) → Step 2 dry-run validator + contract close_basis `bithumb_observed_hourly` (`854dd08`) → Step 3 write path (`2cb66c4`) → **production backfill 14d 336 bucket** (`[2026-05-25, 2026-06-07]`, 이중 독립 검증 drift/OHLC/provenance 0) → hourly append PLAN (`c83c503`) + WRITE path + retention prune (`39b6681`) → **production append + OS cron `5 * * * *`(매시간 :05) 연결 + 첫 발화 검증** (2026-06-08 18:05 KST: inserted=1 / updated=47 / prune=1, count 336 유지, window `2026-05-25 18:00 ~ 2026-06-08 17:00`로 1시간 전진, log+DB 이중 검증). **→ Bithumb hourly = backfill + auto-append self-maintaining** (retention rolling 14d production 첫 동작). **caveat**: cron은 매시간 :05 발화(매시간이라 tz 무관 — append가 KST 직전 완료 hour 처리, 현재 incomplete hour 제외 + 최근 2일 idempotent re-roll로 correctness 보장 — late tick은 다음 발화에서 self-correct) / append lag metric은 ad-hoc 실행 시 inflated(정상 :05 발화 lag ~5분, :05 보수적). **Investing 트랙 완료 (2026-06-08)**: Step 2 validator+contract `investing_observed_hourly` (`14aaa66`) → Step 3 write path + B.write_with_transaction 파라미터화 (`fc3a4c0`) → production backfill 14d 707 (per-currency usd/jpy/eur) → append PLAN (`c4df362`) + WRITE + atomic prune (`b37bd65`) → production append + OS cron `7 * * * *`(:07) 첫 발화 검증. **Hana 트랙 완료 (2026-06-08~09)**: Step 2 validator+contract `hana_observed_hourly` + ohlc_quality 분기(observed_rollup/close_only) (`9187977`) → Step 3 write path + B ohlc_quality str→set 파라미터화 (`1a04e70`) → production backfill 14d 684 (mixed ohlc_quality) → append PLAN (`9f96550`) + WRITE + atomic prune (`e70e55b`) → production append + OS cron `9 * * * *`(:09) 첫 발화 검증. carry-forward write 미적용=실관측만(step render는 v2 endpoint read-side defer). **→ 3 source(Bithumb :05 / Investing :07 / Hana :09) 모두 backfill + auto-append + atomic prune self-maintaining** (source_hourly_rates total 1735 = Bithumb 336 + Investing 715 + Hana 684, Claude+Codex 이중 독립 검증). **v2 1w endpoint switch read-side land+deploy 완료 (2026-06-09, commit `88b9505`)**: graph_v2 period→granularity 분기(1w=source_hourly_rates / 3m·1y=source_daily_rates) + sdr/market_index reader granularity별(hourly=bucket_ts_kst datetime 경계 + DXY KST 시 floor) + insufficient_history tolerance granularity별(daily 7 / hourly 2 — 1w partial coverage 오판 방지, Codex finding) + main.py 1w 자동 허용(is_supported_period, 로직 0). Hana carry-forward/step render는 read-side에서도 미적용(실관측 bucket 그대로, write 정책 일치). production deploy(`--force-recreate`) 검증: `tab=tether&period=1w` 200 + bucket_size 1h + bithumb 170/investing 117/hana 115/dxy 122 insufficient=False + krx insufficient=True + 1d→400 + v1 무변경 + collection 회귀 clean(error 0 / KRX CM normal). **KRX 트랙 Step 2+3 완료 (2026-06-09)**: Step 2 dry-run validator + `krx_observed_hourly`(신규 enum, session-agnostic — CF/CM 공통) + CF-only(KST 08:30~15:45) filter(CM 야간 제외) + contract_code는 source_daily_rates daily row 재사용(KIS 미호출) + metadata.session=CF (`4ce4828`) → Step 3a write path (공유 B.write_with_transaction을 `allow_contract_code`+`post_write_validator`로 default-preserving 확장 + KRX in-transaction post-write[contract NOT NULL + session=CF, 실패 시 rollback]) (`25d63de`) → **production write 72 rows** (`[2026-05-26, 2026-06-08]` 9 CF 영업일 × 8, gap-only, snapshot 생략+rollback anchor) → 이중 독립 verify (contract A75606×72 / drift·OHLC·session=CF 0 / **15:00 hourly close == daily CF close ALL MATCH**) → **v2 1w endpoint krx.usd-krw-futures insufficient True→False 전환** (data=32, default_close_basis=krx_observed_hourly, contract_code per-point 노출). **→ source_hourly_rates 4-source backfill 완료** (Bithumb/Investing/Hana + KRX). KRX는 append/cron 없어 아직 static. **KRX hourly 재설계 (2026-06-10, D6-① 구현 land)**: "cron vs hook" 설계 결정 종결 — 6/9 사고(hook의 daily 의존 cascade)로 **cron + 월물 제거 + CF/CM 통합**으로 전면 재설계. `scripts/hourly_append_krx_source_hourly_rates.py` 신규 (Bithumb append mirror + KRX 고유 empty-window graceful skip — 주말~월요일 아침 무세션 hour cron 실패 방지) + 15 tests. contract_code 미저장(공유 writer default가 post-write에서 None 강제 — "delete 누락" 시 fail-close+rollback 실증) + 세션 무관 rollup(CM 야간 자연 커버 — bucket 월물 혼합은 무거래 구간 06:00~08:30 ⊃ swap 07:00로 구조적 불가, 정상 reconcile 전제) + PLAN에 report-only daily-close 대조 진단(6/9형 정당 DIVERGE — hard gate 금지) + 06:00 CM close singleton bucket 아티팩트 예고 + #4 시너지(REST gate-checked write의 15:45 close row를 2d re-roll이 자동 치유). **운영 전환 + 정리 완료 (2026-06-10, production end-to-end Claude×Claude 상호 검증)**: ① production 재구축 — 구 72행 dump(rollback anchor) → delete → `--window-days 14` write **188 bucket**. 이중 검증: overlap 주간 56개 close/high/low가 dump와 완전 일치(**무료 회귀 baseline** — 구 CF-only ≡ 신 세션무관 로직) + window 경계 보완(5/26·5/27<22:00 16개 부재 = retention 정상, false failure 0) + 06:00 CM-close singleton 8개 + 야간 hour 확장 + daily-close 진단 8 MATCH / 6/9 DIVERGE(1510.6 blackout vs 1514.7 복구, 정당). ② OS cron `:11` 등록(기존 :05/:07/:09 보존) + 첫 발화 검증(inserted=1/updated=41/prune=1 → net 188 sliding window, range 한 칸 전진 — 사전 등록 기대값과 정확 일치). ③ v2 1w 라이브 — `krx series per_point_metadata []`(contract 자연 소멸, graph_v2 동적 판정 코드 수정 0) + hour `[00~06,08~15,18~23]` 야간 확장 API 도달. ④ 구 hook/모듈/env 제거(별도 commit) — in-process hourly hook(krx_kis.py) + `KRX_HOURLY_APPEND_ENABLED` env(config.py + EC2 .env) + `app/krx_hourly.py` + 구 CF-only backfill 스크립트 + hook/구backfill 2 test 파일(1255 lines) 삭제. 공유 writer B의 `allow_contract_code` True-arm + `post_write_validator`는 caller 0이 되나 **False-arm(contract None 강제 — 모든 caller live 검증)은 잔존** → trip-wire(구 row 위 write fail-close)를 영구 unit test로 승격. **→ 4-source hourly cron(:05/:07/:09/:11) self-maintaining 완성.** 남은: 프론트 client cutover.

**Amendment (2026-08-18, 구현·배포 전 계약)**: 네 hourly append CLI의 `--as-of`는 PLAN 전용이다. `--write`와 함께 쓰면 과거 candidate 재생성 또는 미래 retention cutoff에 의한 정상 bucket 대량 삭제가 가능하므로 DB 진입 전에 exit 2로 차단한다. 실제 cron 인자(:05/:07/:09/:11)는 `--as-of`를 쓰지 않아 기존 write 경로를 유지한다. KRX 2026-08-14 사고의 08:00~11:00 KST 네 bucket은 사건별 정책 상수로 write candidate에서 영구 제외한다. PLAN은 `[GUARD PREVIEW]`, WRITE는 `[GUARD SKIP]`을 남기고 clean candidate와 prune은 계속 처리한다. 기존 오염 행은 경고만 하며 자동 삭제하지 않는다. 삭제·복원은 발행된 영수증과 결속된 사건 전용 `repair_krx_hourly_rows.py`만 담당한다. 이 도구는 module-level `app.*` 의존 없이 단일 파일 import·`--help`·CLI parsing을 유지하지만, 실제 dry-run·write·`--revert-from`은 기존 `app.database`·`app.models`가 있는 앱 이미지에서 실행한다. 신규 incident 모듈과의 값 정합은 테스트가 잠근다. 이 Amendment는 현재 코드 계약이며 운영 반영은 별도 배포 검증 후 확정한다.

**D4 — Phase 2e MVP = 3m/1y 우선, 1w 후속**: 3m/1y는 source_daily_rates(KRX/Hana/Bithumb) + market_index_rates(DXY) + **Investing daily rollup(D1)**만 추가하면 완성 → payoff 즉시. 1w(source_hourly_rates)를 MVP에 묶으면 hourly 표준화 설계로 출시 지연 → **MVP=3m/1y, 1w=source_hourly_rates land 후 추가**.

### Open (구현 PR에서 확정)

- Investing daily rollup 정확 정의 (KST boundary / 외환시장 24/5 주말 gap 처리 / point_count==0 case) + 구현 직전 production read로 investing USD/JPY/EUR coverage 확인
- source_hourly_rates 정확 schema + retention 일수 + bucket size(1h 고정 여부)
- **Bank/Investing source/asset realtime pipeline 통합 후 rollup provider 전환 방식** (기존 per-source raw table 기반 backfill ↔ 신규 normalized `source_rates` 기반 append의 provenance 유지) — [All-source observation fanout](REALTIME_ARCHITECTURE_PLAN.md) Bank/Investing β 트랙과 정합. **단 Phase 2e MVP/D1을 이 리팩터에 묶지 않음** — 현 장기 raw table로 daily canonical 먼저, realtime pipeline은 나중 교체 (canonical schema 불변)
- legacy hybrid (현 1w = investing + DXY hourly + realtime) 제거 시점 (source_hourly_rates land 후)
- ✅ **해소**(Phase 2e MVP 2026-06-06): DXY는 v2 build_tab에서 kind dispatch로 **별 market_index_rates.daily reader** (source_daily_rates 다중 read 아님 — D2 결정대로 별 path)
- ✅ **해소**(Phase 2e MVP 2026-06-06): Phase 2e endpoint contract = `/api/v2/graph/catalog` + `/api/v2/graph/tab` 구현 (catalog/tab 응답 + insufficient_history partial coverage 적용, graph_v2 + main.py wiring, GRAPH §13)

### Consequences

- ✅ 3m/1y hot path 완전 표준화 (Investing 포함 → external/raw 직접 read 0) — v2 MVP 선명
- ✅ provenance 일관 (모든 source canonical에 close_basis/source_method/ohlc_quality)
- ⚠️ 다중 table read (source_daily_rates + market_index_rates) — endpoint 복잡도 ↑ (index/source 모델 명확성과 trade)
- ⚠️ source_hourly_rates 신설 = 신 table + rollup job + retention 관리 (1w 전용) — MVP 후로 분리해 risk 격리

---

## ADR-036: 가격알림 반복 발송 (repeat_interval_sec, B2) — 정책 + 스키마 + evaluator/mark mode 분기

**Status**: Accepted — PR1(source/tether) + PR2(bank/FX) + payload-flag(cross-device race) land + prod deploy 완료 (2026-06-29~30). PR1 server `d2dfe28`/iOS `c4b88eb`(+토글 깜빡임 fix `479d611`), PR2 server `195058b`(crud gate+mark+create/update §7/§8 + FxNotificationBackend.refetch_snapshot repeat-aware)/iOS `d7791c8`(bank interval picker + UpdateAlertRequest 3-state), payload-flag server `b0b21fb`(FCM data `is_repeat` 4 builder + CachedAlertSetting.repeat_interval_sec carry)/iOS `157100c`(push 체인 is_repeat 권위 + handleTriggeredSetting(id:isRepeat:) 양 VM). 컬럼 마이그레이션은 PR1에서 양 테이블 prod 적용 완료. 기기 검증: USDT+bank 반복 e2e 정상(은행은 가격변동 시 재발사 = event-driven floor, 정상). **payload-flag**: FCM `is_repeat`("true"/"false") 권위 플래그로 단말이 stale 로컬 대신 toggle-off 결정 → once↔repeat cross-device race 양방향 차단(구 서버 부재 시 로컬 휴리스틱 폴백). 잔여 없음(B2 완료); iOS는 신규 빌드 배포 시 payload-flag 소비(하위 호환 — 구 앱은 필드 무시).
**관련**: [USDT_WS_DESIGN_PLAN.md §B2](USDT_WS_DESIGN_PLAN.md), ADR-032 (KRX/source alert evaluator), 신규 앱 overhaul(메모리 project_app_overhaul — 가격+비교알림 전 탭). B3(direction-crossing)은 본 ADR 범위 밖(후속).

### Context

현재 가격알림은 **1회성(once-only)**: 조건 충족 시 1회 발송 후 `triggered=true, enabled=false`로 자동 비활성. 재무장은 사용자가 토글 ON([crud.py:2240-2242](app/crud.py#L2240) bank / [3095-3097](app/crud.py#L3095) source).

신규 앱 요구(사용자 확정): **조건이 만족되는 동안 사용자가 선택한 간격으로 반복 발송**. 가격알림 + (후속)비교알림 **공통 기능**(spec §B2).

**검증된 현황(코드 직접 확인, 2026-06-29)**:
- 양 알림 테이블(`notification_settings`, `source_notification_settings`)에 **`last_notified_at` + `last_notified_rate` 이미 존재**([models.py:85+](app/models.py#L85), [153+](app/models.py#L153)). ⚠️ CLAUDE.md 스키마 문서가 **notification_settings 표에서 이 두 필드 누락(stale)** — PR1에서 **양 테이블 표**에 누락필드 정정 + `repeat_interval_sec` 신규 기재.
- 발송 시 `last_notified_at = now` **이미 set**, 재활성/중복/PUT 시 `last_notified_at = None` **이미 리셋**([crud.py:1930/2084/2867/2982](app/crud.py#L1930)).
- evaluator `delivery_allowed`의 B2 분기는 **docstring 예시일 뿐 실행 코드 아님**([alert_evaluator.py:210-225](app/notifications/alert_evaluator.py#L210)); `repeat_interval_sec`는 **모델/스키마 필드로 미존재**.

→ B2 마이그레이션은 **`repeat_interval_sec` 1 컬럼 × 2 테이블만 신규**. 반복 gating 인프라(last_notified_at set/reset)는 이미 깔려 있음.

### Decision — 정책 (확정)

1. **once 판별**: `repeat_interval_sec IS NULL` = once-only (canonical, 단일 sentinel). `0`/음수는 API에서 reject (NULL-or-0 모호성 + `0초=연속 스팸` footgun 제거).
2. **interval enum 고정** (자유 입력 금지, 기존 문서 [§B2:208](USDT_WS_DESIGN_PLAN.md#L208) 후보 채택): **1m / 5m / 10m / 30m / 1h / 2h / 4h / 6h / 12h / 1d** (초: 60 / 300 / 600 / 1800 / 3600 / 7200 / 14400 / 21600 / 43200 / 86400). "한번만"은 enum 아닌 NULL(= interval 없음). floor 1m로 FCM 스팸 1차 차단. (직전 draft의 15m은 문서 미존재 + 임의 추가라 **철회** — 문서 후보를 따름, special reason 없음.)
3. **once mode** (`repeat_interval_sec IS NULL`): 현행 유지 — 발송 후 `triggered=true, enabled=false`.
4. **repeat mode** (`repeat_interval_sec` 설정): 발송 후 **`enabled=true` 유지, `triggered`는 절대 set 안 함**, `last_notified_at=now`. **gating = `enabled AND (last_notified_at IS NULL OR now >= last_notified_at + repeat_interval_sec)`**.
   - ⚠️ **timezone (workflow 검증, load-bearing)**: gate의 `now`/`last_notified_at`은 **둘 다 naive UTC**여야 함. 현 호출부([alert_evaluator.py:622/679](app/notifications/alert_evaluator.py#L622))는 **aware** `datetime.now(timezone.utc)`를 넘기는데 `last_notified_at`은 **naive**([models.get_utc_now](app/models.py#L11) = `.replace(tzinfo=None)`) → B2 뺄셈이 **TypeError(aware−naive)** → `_evaluate_*_async`의 `except Exception`([470/511](app/notifications/alert_evaluator.py#L470))이 삼켜 **repeat 영영 silent 미발화**. PR1은 호출부 622/679를 `models.get_utc_now()`로 바꾸거나 `delivery_allowed` 내부에서 `now`를 naive 정규화(둘 중 하나 필수). 회귀: 기존 테스트가 aware now를 쓰면 정규화 후 green 재확인.
5. **triggered 원칙 (ADR 핵심)**: `triggered=true`는 **once-only 종료 상태 전용**("발송됨, 토글로 재무장"). repeat는 *진행 중*이라 종료 상태가 없음 → repeat 모드에선 `triggered`를 set하지 않는다. UI "발송됨" 인디케이터도 repeat엔 미적용.
6. **조건 release 후 재충족**: repeat 도중 조건이 풀렸다 다시 만족해도 `last_notified_at` 리셋 안 함 = **"마지막 발송 기준 간격"만** 본다. (재크로싱 즉시 발송은 **B3**의 영역 — 본 ADR 분리.)
7. **리셋(`last_notified_at = NULL`)**: OFF→ON(enabled false→true) · threshold · condition · (source의 경우) source/asset · `repeat_interval_sec` **변경 시**. 기존 리셋 지점([crud.py:1930/2867](app/crud.py#L1930)) 재사용.
8. **모드 전환 PUT 정규화** (edge-case 시뮬레이션 발견): 이미 발사된 once-only(`triggered=true, enabled=false`)를 repeat로 변경 시 interval-변경 리셋만으론 `enabled=false`가 남아 안 울림 → **모드 전환 PUT은 `enabled=true · triggered=false · last_notified_at=NULL`로 정규화**(새 모드 clean 시작). 역방향(repeat→once)도 동일 정규화.
9. **dedup key**: 기존 키(bank·source + currency·asset + condition + threshold) **유지**. `repeat_interval_sec`는 **키에 넣지 않고 그 설정의 갱신 가능 속성**(PUT으로 변경 + §7 리셋).
10. **history**: 반복 발송마다 기존 log row 추가(`notification_logs`/`source_notification_logs`). [Slice B PR1 히스토리 endpoint](app/main.py)가 이미 다건 표시 ready.
11. **repeat 재평가 메커니즘** (문서 [§생략금지 #5](USDT_WS_DESIGN_PLAN.md#L1427) + coalescer `repeat_due` slot): 알림 평가는 tick fanout의 **독립 분기**(Redis same-rate-SET-skip / DB writer와 별개). [`PriceAlertCoalescer`](app/notifications/price_alert_coalescer.py)(5s 윈도우)가 same-rate를 **drop 없이 집계** → boundary마다 summary flush → evaluator가 **매 윈도우 평가**(same-rate skip 없음 — [alert_evaluator.py](app/notifications/alert_evaluator.py)의 skip은 in-flight dedup·stale-cache 재검증뿐, 현 코드 확인). 따라서:
    - **source-first(USDT/KRX 등 tick 흐르는 피드)**: 자연 tick 흐름으로 repeat-due가 재평가·재발화. **별도 타이머 불요**(B2-PR1 MVP). ⚠️ 단 정확히 interval 시점이 아니라 **interval 경과 후 *다음 tick* 도래 시** 발화 — coalescer가 last bucket을 다음 tick에서 flush([price_alert_coalescer.py:133](app/notifications/price_alert_coalescer.py#L133)). high-traffic(USDT)은 무시 가능(~수초), low-fpm 피드는 그만큼 지연(정밀 due가 필요하면 `repeat_due` 타이머 — defer).
    - [`PriceAlertEvaluationInput.input_kind="repeat_due"`](app/notifications/price_alert_coalescer.py#L55) forward-compat slot = **조용한 피드**(은행 비고시/off-session)에서 tick 없이 due를 발화시키는 **타이머 트리거용 — PR1·PR2 모두 범위 밖(post-B2 optional enhancement)**. PR2(bank)도 **시장 시간대 자연 tick**으로 repeat 동작 → 비고시/off-session 미발화는 의미상 타당(장 마감 중 스팸 회피). 타이머는 "tick 없는 구간에도 due 발화"가 제품 요구가 될 때 별도 PR.
    - 문서 §생략금지 #5는 **향후 same-rate-skip 최적화(Phase 3 ZSET threshold index)가 repeat-due를 carve-out해야 한다는 제약**으로 유지.

### Schema

```sql
ALTER TABLE notification_settings        ADD COLUMN repeat_interval_sec INTEGER NULL;  -- NULL = once
ALTER TABLE source_notification_settings ADD COLUMN repeat_interval_sec INTEGER NULL;
```
- `last_notified_at` / `last_notified_rate`: **기존 컬럼 재사용**(추가 없음).
- 기본값 NULL → **기존 모든 row = once-only 보존**(behavior-change-0).
- migration: SQLAlchemy `create_all`는 기존 테이블에 컬럼 추가 안 함 → 별도 idempotent migration script(`ALTER ... ADD COLUMN IF NOT EXISTS` 동등) 필요. ADR-034 패턴 따름.

### Evaluator / mark — 두 코드 경로 (codex "둘 다"의 정밀화)

가격알림 평가는 **2개의 분리된 경로**:
- **source path** (USDT/KRX, 신규 앱 테더 reference vertical) — workflow 검증 정밀화:
  - gate(`delivery_allowed`)는 **refetch된 `FreshSettingSnapshot`에서 실행**([alert_evaluator.py:622/679](app/notifications/alert_evaluator.py#L622)) → **`FreshSettingSnapshot`에 `repeat_interval_sec`+`last_notified_at` 추가 필수**, [`SourceAlertBackend.refetch_snapshot`](app/notifications/alert_storage_backend.py)가 두 필드를 채움.
  - **`CachedAlertSetting`은 gate가 cache 미참조라 추가 불요**(workflow 중재 — lens4 "둘 다 cache" 과다 설계).
  - `delivery_allowed` pseudocode→실행 분기화(§4 gate, timezone 정규화 포함).
  - `mark_source_setting_triggered`는 **이미 ORM row 재조회([crud.py:3090](app/crud.py#L3090))하므로 `setting.repeat_interval_sec`로 직접 mode 분기**(persist_result/cache로 interval thread **불요** — lens2/4 과다 설계).
- **bank path** (notification_settings, FX 탭 legacy 경로): `crud.process_rate_alerts` + `mark_setting_triggered` mode 분기.

**Scope = FULL B2 (bank + source 공통, 문서 §B2 정합).** 구현은 risk 관리 위해 **B2 안에서 2 PR로 위상 분리** — **"B2 완료"는 양쪽 끝난 뒤에만 선언**(source-only를 "B2 완료"라 부르지 않음):
- **PR1 (source path)**: `alert_evaluator.delivery_allowed` 실행 분기 + dataclass 필드 + `mark_source_setting_triggered` mode 분기 + source API + `SourceAlertAddSheet` picker. 테더 live 경로(USDT+KRX) + 가장 깨끗한 evaluator(함수형 gate) → **정책 먼저 실증**.
- **PR2 (bank path)**: `process_rate_alerts`의 gate([get_triggered_settings_for_rate](app/crud.py)) repeat-aware화 + `mark_setting_triggered` mode 분기 + bank API + `AlertAddSheet` picker. ⚠️ **FX_ALERT_SHADOW 패리티 로직(메모리 project_fx_alert_shadow_active) 보존 주의** — bank gate가 쿼리 기반 + shadow 무빙파트라 source보다 moderate하게 큼(별 PR 근거).
- **스키마**(`repeat_interval_sec` ×2)는 **PR1에서 함께 추가**(대칭, 2차 migration 회피). bank 컬럼은 PR1~PR2 사이만 잠시 dormant(API/UI 미노출 → NULL → once 불변).

**근거**: bank/source는 별도 코드 경로이고 bank는 쿼리 gate + FX_ALERT_SHADOW로 더 무겁다 → 한 PR에 몰면 risk↑. 그러나 문서가 B2를 공통으로 정의 + 같은 앱에서 테더-repeat/FX-norepeat 비대칭은 사용자 혼란 → **scope는 full, delivery만 source→bank 순서**. vertical-first = scope-제외가 아니라 sequencing.

### API (source, PR1)

- `POST/PUT /api/source-notification-settings` request + response에 `repeat_interval_sec: Optional[int]` 추가.
- validation: `None` 또는 enum 허용 값(60/300/600/1800/3600/7200/14400/21600/43200/86400)만; 그 외(0/음수/비-enum) → 422 reject.
- PUT에서 §7/§8 리셋·정규화 적용.

### iOS (source, PR1)

- `SourceAlertAddSheet`(테더 source 알림 picker — PR1)에 **반복 간격 picker**(한번만 + enum 10) 추가. 한번만 = `repeat_interval_sec=nil` 전송. (bank `AlertAddSheet` picker는 PR2.)
- 수정 모드: 기존 값 로드. summaryText/목록에 반복 표기(예: "5분마다").
- `SourceAlertSetting` 모델 + CRUD payload에 `repeat_interval_sec`.

### B2 / B3 경계

- **B2 (본 ADR)**: 사용자 설정 간격 반복, interval-from-last-send.
- **B3 (후속)**: threshold direction crossing 즉시 발송(조건 release→재충족 즉시). B2 안정 후 별 ADR.

### Consequences

- (+) 신규 앱 핵심 알림 UX(반복) + 비교알림이 올라탈 공통 토대.
- (+) 기존 once 무영향(NULL 기본), 기존 last_notified_at 인프라 재사용으로 작은 변경.
- (−) FCM 발송량 증가 가능(조건 고착 시 interval마다). 1차 완화 = enum floor 1분. **runaway(하루 종일 고착) 대비 일일 cap/auto-expire는 본 ADR 범위 밖, 관찰 후 별도 결정**(최소 floor는 확보).
- (−) bank/source 경로 분리로 full coverage는 2 PR(의도적 vertical-first).

### Alternatives (기각)

- `0 = once`: NULL과 이중 sentinel → 모호 + 0초 연속 스팸 위험. → NULL 단일.
- repeat에서 triggered를 gate로 재사용: once-only 의미와 충돌, UI "발송됨" 오표시. → repeat는 last_notified_at gate, triggered 미사용.
- "cooldown 반복" 표현: implementer-centric → 제품 spec은 "사용자 설정 반복 간격"(spec §B2 reframe).
- **source-only로 "B2 완료" 선언 (기각, codex)**: 문서가 B2를 공통 기능으로 정의 + 같은 앱에서 테더-repeat/FX-norepeat 비대칭 = 사용자/문서 혼란 + 완료 선언 모호. → scope는 full B2(bank+source), 단 delivery는 source→bank 2 PR 위상(완료는 둘 다 후). (source-first를 "B2a"로 개명하는 codex 대안 B보다, full scope 유지 + 정직한 완료 기준이 더 깔끔.)
- bank+source 한 PR full wiring: bank gate(쿼리) + FX_ALERT_SHADOW로 source보다 무거워 단일 PR risk↑ → scope는 full이되 PR은 위상 분리.

### Rollout

1. 본 ADR 확정.
2. DB migration script(`repeat_interval_sec` × 2) + models/schemas.
3. source evaluator: dataclass 필드 + `delivery_allowed` 실행 분기 + `mark_source_setting_triggered` mode 분기.
4. source CRUD/API create/update/read + validation(§API).
5. tests(once 회귀 + repeat gate boundary/nil/release/reset/모드전환).
6. iOS interval picker(source/테더).
7. 실기기 알림 smoke(repeat 발송 + 간격 + 히스토리 다건).
8. **PR2 (bank path)**: `process_rate_alerts` gate repeat-aware + `mark_setting_triggered` mode 분기 + bank API + `AlertAddSheet` picker + **FX_ALERT_SHADOW 패리티 보존**. **"B2 완료"는 PR2 후 선언.**

### PR1 구현 touchpoints (workflow w0xxx3yw4 검증, file:line)

- `app/models.py`: `NotificationSetting`([85+](app/models.py#L85)) + `SourceNotificationSetting`([153+](app/models.py#L153)) 양쪽 `repeat_interval_sec = Column(Integer, nullable=True)`.
- `scripts/migrate_repeat_interval_sec.py` (신규): [migrate_market_index_granularity.py](scripts/migrate_market_index_granularity.py) 패턴 — `column_exists` 멱등 + `--dry-run` + SQLite/PostgreSQL 동일 `ADD COLUMN ... INTEGER NULL`. DEFAULT/UPDATE step 불요(NULL=once).
- `app/notifications/alert_evaluator.py`: `FreshSettingSnapshot`([137-154](app/notifications/alert_evaluator.py#L137)) += `repeat_interval_sec`/`last_notified_at` / `delivery_allowed`([210](app/notifications/alert_evaluator.py#L210)) 실행 분기(+timezone naive 정규화) / 호출부([622/679](app/notifications/alert_evaluator.py#L622)) now naive.
- `app/notifications/alert_storage_backend.py`: `refetch_snapshot`(snapshot에 2필드 채움) + `load_settings`(cache는 `repeat_interval_sec` 선택적·불요).
- `app/crud.py`: `mark_source_setting_triggered`([3084](app/crud.py#L3084)) `setting.repeat_interval_sec` 직접 mode 분기 / `update_source_notification_setting`([2934](app/crud.py#L2934)) §7 리셋(interval 변경) + §8 모드전환 정규화 / `create_source_notification_setting`([2838](app/crud.py#L2838)) dedup 분기에 interval set.
- `app/schemas.py`: SourceNotificationSetting Request/UpdateRequest/Response에 `repeat_interval_sec: Optional[int]` + enum validation(422).
- `app/main.py`: create/update 호출 전달 + `build_source_notification_setting_response`에 노출.
- `CLAUDE.md`: 양 테이블 스키마 표 정정(§13).
- 테스트: delivery_allowed boundary(nil/미경과/경과/once 회귀) + naive/aware 회귀 + mark mode + §7/§8.

## ADR-037: 비교 알림 (comparison alerts) — within-tab v1 + universal schema + 현행 evaluator 재매핑

**날짜**: 2026-07-03
**상태**: Proposed (설계 잠금 — 구현 미착수)
**결정자**: Jay + Claude + Codex (3-way, within-tab은 사용자 확정)

### Context

신규 앱 비전의 핵심 차별 기능 = 소스 간 **가격 차이** 알림 (김프/역프/스프레드). 설계 자산은 이미 존재:
[USDT_PHASE1_DESIGN.md §4](USDT_PHASE1_DESIGN.md)의 `comparison_alerts` 4컬럼 draft + unified lookup/
dual-trigger 인프라 노트, [USDT_TAB_PROPOSAL.md Decision D](USDT_TAB_PROPOSAL.md)의 2축 조건 체계.
단 draft 이후 지형 변화 3개를 반영해야 함: (1) B2 반복발송 land(ADR-036)로 "Phase 1은 1회성 통일"
결정 stale, (2) Decision F(REST polling)는 WS fanout + evaluator composition으로 superseded —
트리거 hook 지점 재매핑 필요, (3) 신규 앱 UX가 tab-based로 확정(그래프 v2 탭 전환 완료).

### Decision 1 — v1 scope: within-tab only (사용자 확정 2026-07-03)

비교는 **같은 탭 안의 소스끼리만** (테더/달러/엔화/유로 4탭, 뉴스 제외). 문서의 "모든 소스 타입 간
자유 비교"(USDT_PHASE1_DESIGN §4 조합표)는 **스키마 가능성으로 보존**하되 제품 v1은 탭 내부로 제한.

- **허용 소스 = 해당 탭 그래프 catalog의 `axis_group == "krw"` series** (단일 진실 소스 = graph v2
  catalog·`TAB_1D_SERIES` — 그래프에 보이는 소스만 비교 가능, UX 일관):
  - 테더: USDT 거래소 5 + krx.usd-krw-futures + investing/kb/hana(usd-krw) — asset 혼합 허용(전부 KRW 단위)
  - 달러: investing + 8 banks (usd-krw)
  - 엔화/유로: investing + 8 banks (jpy-krw / eur-krw)
- **index 축(DXY 계열) 제외**: 지수(≈99)와 KRW rate(≈1500)는 단위가 달라 Decision D의 금액(원) diff가
  무의미. `axis_group == "krw"` 필터가 자동 배제.
- **`tab` 컬럼 명시 저장** (유도 아님): investing/kb/hana usd-krw가 테더·달러 양쪽 탭에 존재해
  (left,right) 조합만으로 탭 유도가 **모호** (예: 인베스팅↔하나 usd는 두 탭 다 가능). 생성 시
  클라이언트가 tab 명시 → 서버가 "그 탭의 허용 소스 집합 내 조합"인지 검증(cross-tab 조합 400).
  GET `?tab=` 필터도 이 컬럼 사용.

### Decision 2 — 스키마: draft 계승 + 3 확장

`comparison_alerts` (USDT_PHASE1_DESIGN §4 draft 계승):
`id / user_id / left_source / left_asset / right_source / right_asset / diff_type(signed|absolute) /
operator(gte|lte) / threshold(REAL, KRW 원 단위) / enabled / triggered / last_notified_at /
created_at / updated_at` + **확장 3개**:

1. `tab TEXT NOT NULL` (Decision 1 — 모호성 근거로 명시 저장)
2. `repeat_interval_sec INTEGER NULL` (B2/ADR-036 정책·enum·validator 그대로 재사용 — NULL=once.
   구 "Phase 1은 1회성 통일"(USDT_TAB_PROPOSAL Open Decision 2)은 본 ADR로 **supersede**)
3. `last_notified_spread REAL NULL` (단일 알림의 last_notified_rate 대응 — **signed raw spread 저장**
   [absolute 평가값은 |spread|로 재계산 가능하므로 raw가 정보 보존]. repeat throttle 게이트는
   `last_notified_at + repeat_interval_sec`만 사용 — 본 컬럼은 진단/히스토리 요약용, codex 확인)

**인덱스** (codex blocker 2 — 고빈도 tick 경로 후보 조회): `(left_source, left_asset, enabled)` +
`(right_source, right_asset, enabled)` + `(user_id, tab)` / logs `(user_id, sent_at)`. 추가로 tick당
DB scan 금지 — 활성 비교알림 후보는 **기존 alert settings cache 패턴(alert_storage_backend TTL cache)
재사용**으로 in-memory 조회.

**dedup 정책** (2026-07-03 S1 체크포인트 보강 — 단일 알림 멱등 선례[crud.py create_source_notification_setting] 계승):
`(user_id, tab, left_source, left_asset, right_source, right_asset, diff_type, operator, threshold)`
**exact match → 새로 생성하지 않고 기존 설정 enabled 갱신** (enabled=true 시 triggered=false 리셋 —
단일 알림과 동일). `tab` 포함: 모호 조합(inv↔hana usd)을 탭별 독립 관리 (제외 시 "달러 탭에서 만든
알림이 테더 탭 목록에 없는데 생성은 거부" UX 혼란). **A−B vs B−A 순서 뒤집힘은 v1에서 별개 취급**
(exact match만): absolute에선 동일 의미지만 curated presets가 방향을 고정해 UI상 뒤집힌 중복 생성
경로가 없음 — canonical ordering 정규화는 signed 부호 반전까지 얽혀 과설계, free builder 개방 시
재검토 (Open 6). DB unique 제약은 두지 않음(앱 레벨 멱등만 — 단일 알림 선례 동일).

`comparison_notification_logs` (신규 — 단일 알림 로그 테이블 패턴 계승):
`id / user_id / setting_id(NULL 허용 — 설정 삭제 후 로그 유지) / tab / left_source / left_asset /
right_source / right_asset / diff_type / operator / threshold / **left_rate / right_rate / spread** /
**left_observed_at / right_observed_at** / **is_repeat BOOLEAN** / success / error_message / sent_at`.
- left_rate/right_rate/spread: 히스토리 row가 "발화 시점 양쪽 값 + 차이"를 표시 (사후 재계산 불가 —
  발화 시점 스냅샷 필수).
- left_observed_at/right_observed_at (codex blocker 3): stale gate 미도입(Open 3) 선택의 전제 조건 —
  심야 은행 stale 값 기반 발화를 로그/FCM/히스토리에서 설명 가능해야 함. unified lookup이
  (rate, observed_at) 튜플을 반환하고 FCM data에도 동봉.
- is_repeat: 기존 source 로그에 없어 반복 여부 표시가 불가능했던 gap을 신규 테이블은 처음부터 회피
  (FCM data의 is_repeat(ADR-036)와 동일 의미).

### Decision 3 — 평가: unified lookup + dual-trigger를 **현행 evaluator 지점**에 hook

- `get_latest_rate_unified(source, asset)` (USDT_PHASE1_DESIGN §4-A 계승): source_rates(USDT/KRX) /
  bank_exchange_rates / investing_exchange_rates 투명 조회. **1차 구현은 Redis latest 우선**
  (`latest:source:*` / `latest:bank:*` / `latest:investing:*` — ADR-026/029/031로 전 소스 커버) +
  DB fallback. 반환은 `(rate, observed_at, origin)` — origin(redis|db)은 서버 구조화 로그까지만
  기록(DB 로그 스키마엔 미저장 — stale 설명 책임은 observed_at[B3]이 담당, origin 영속화는 YAGNI).
  비교식 `spread = left_rate − right_rate` (Decision D).
- **dual-trigger**: left/right 어느 쪽 변해도 재평가 (§4-B 계승). hook 지점은 draft의 크롤러 경로가
  아니라 **현행 alert 평가 지점** (Decision F superseded 반영):
  - USDT 5: `UsdtAlertEvaluator` tick 경로 (coalescer 5s grain 뒤)
  - KRX: `KrxAlertEvaluator` tick 경로 (close grace/세션 정책 승계)
  - bank/investing: `process_rate_alerts` 경로 (FX shadow와 무관 — 비교알림은 greenfield라 shadow
    파이프라인 접점 없음)
- 반대편 값 fresh 조회는 unified lookup — 반대편이 stale(예: 은행 심야 미고시)이어도 마지막 관측값
  기준 평가 (단일 알림과 동일 semantics, 별도 freshness gate는 v1 미도입 — Open 3).
- 발사/반복/once semantics: ADR-036 evaluator 정책(delivery_allowed/mark mode 분기) 그대로 재사용.
- **중복 발송 race 방지 (codex blocker 1)**: left/right 양쪽 hook이 같은 setting을 근접 시점에 칠 수
  있고, 기존 `_in_flight_settings`는 evaluator 인스턴스 내부라 경로 간 미공유 → 비교알림은
  **comparison setting id 기준 in-process claim**을 둔다. ⚠️ 3 hook의 실행 컨텍스트가 혼합
  (USDT/KRX=asyncio 루프, bank/investing `process_rate_alerts`=sync crawler/scheduler thread)이라
  단순 공유 set만으론 부족 — codex 잔여 blocker에서 두 옵션(threading.Lock 보호 / 단일 루프 marshal)
  중 **[S2 구현 확정 2026-07-03] 단일 event loop marshal 채택**: sync hook은
  `topic_trigger_bridge.schedule_on_loop`로 main loop에 마샬링(FX shadow `_emit_fx_alert_shadow`
  선례) → 모든 평가가 단일 `ComparisonAlertEvaluator` 인스턴스의 loop 컨텍스트에서 실행 →
  기존 `_in_flight_settings` 패턴(add → try: send → finally: discard, 해제=persist 완료 후)
  그대로 재사용, Lock 불요. ⚠️ marshal은 best-effort(loop 미등록/shutdown 시 False — tick 1회 소실
  가능하나 크롤러 주기 재평가로 자기 치유, 기존 FX 단일 알림과 동일 그레인). 단일 프로세스
  (`--workers 1`) 전제는 유지 — DB conditional claim은 과설계로 기각(multi-process 전환 시 재평가,
  [[project_atomic_scope_usdt_krx]]와 동일 전제).

### Decision 4 — API + 게이팅 + FCM

- REST: `POST/GET/PUT/DELETE /api/comparison-alerts` + `GET /api/comparison-notification-logs`
  (source-notification-* 4종 + logs 패턴 그대로 — auth/premium 게이팅 동일: INACTIVE 빈 목록 /
  PENDING 503 / ACTIVE 조회, logs는 success-only + limit≤200).
- 검증: tab ∈ {tether,usd,jpy,eur} + left/right 각각 해당 탭 허용 소스 집합 + left≠right(동일
  source+asset 페어 차단) + diff_type/operator/threshold/repeat_interval_sec 형식 (400/422).
  ⚠️ 기존 `source_registry.validate_alert_source_asset` **재사용 금지** (codex) — 그 함수는 reference
  (investing/kb/hana)를 source 알림에서 거부하는데 비교알림은 reference가 1급 시민. 신규 tab-scope
  검증 helper를 source_registry에 별도 구현 (main.py 밖 — [[project_main_py_helper_placement]]).
- FCM payload `type: "comparison_alert"` (기존 앱은 unknown type 무시 — rate_alert/source_rate_alert
  선례). data에 left/right source·rate, spread, is_repeat 포함.

### Decision 5 — v1 UX: curated presets (free builder는 후속)

- 탭별 "비교 알림" 섹션(단일 가격알림 섹션 아래 — USDT_TAB_PROPOSAL §B 계승) + **추천 조합 목록**
  (예: 테더 = 김프[빗썸−인베스팅]/거래소 간/USDT−KRX선물, 달러 = 기준 대비 은행/은행 간)에서 선택 →
  조건(diff_type·operator·threshold·repeat) 설정. 임의 조합 free builder는 스키마가 이미 감당하므로
  후속 슬라이스에서 UI만 확장.
- preset 구체 목록은 iOS 슬라이스에서 확정 (서버는 universal 검증이라 무관).
- 히스토리 sheet는 SourceAlertHistorySheet 패턴 재사용 (spread 중심 row + is_repeat 뱃지).

### Decision 6 — rollout: greenfield flag (shadow 불요)

legacy 비교알림이 없으므로 FX cutover식 shadow/parity 불요. `COMPARISON_ALERT_ENABLED` env
(default false) 하나로 evaluator 발화 게이트 — API/스키마는 flag 무관 land 가능(dead until flag).
iOS canary(F-3 패턴: custom token 단말)로 end-to-end 검증 후 활성.

### Slices (구현 순서)

1. **서버 S1**: models + migration script(멱등, 2 테이블) + schemas — behavior-change-0.
2. **서버 S2**: `get_latest_rate_unified` + `process_comparison_alerts`(dual-trigger hook 3지점) +
   FCM 발사 + logs 기록 + flag 게이트. 단위 테스트(경계/반복/once/모호 조합 검증).
3. **서버 S3**: REST 5종 + premium 게이팅 + tab-scope 검증. endpoint 테스트.
4. **iOS S4**: 비교 알림 섹션(테더 탭 먼저) + curated preset UI + 히스토리 sheet + FCM type 분기.
5. **활성**: env flag + iOS canary end-to-end (F-3 절차) → 전 탭 확장.

### Open (후속 결정)

1. preset 목록 최종안 (iOS S4에서 — 서버 무관).
2. % 기준 diff_type 축 추가 (v2+ — 스키마는 diff_type enum 확장으로 수용 가능).
3. 반대편 stale freshness gate (v1은 마지막 관측값 평가하되 **per-side observed_at을 로그/FCM에 동봉**
   [codex blocker 3 수용] — 심야 misleading 알림이 실측되면 gate 도입 재검토).
4. cross-tab 비교 개방 여부 (스키마는 이미 수용 — 제품 판단만).
5. KRX 재배포 권리 검토(§4-C)는 단일 KRX 알림 F-2/F-3 공개 선례로 v1 포함 판단 — 이슈 발생 시 tab
   허용 집합에서 KRX만 제거하는 축소 경로 존재.
6. A−B/B−A canonical ordering (free builder 개방 시 — v1은 exact-match dedup + preset 방향 고정으로 회피).

### Amendment 2026-07-04 — 제품 의미 분리: 비교알림(absolute-only) + 김프/역프 알림(signed, 테더 전용)

**배경**: S4 land 후 사용자 실사용 판단 — "9개 소스 × 4조건 조합은 개발자도 파악이 어렵다. UI 문제가
아니라 조건 자체가 너무 광범위하다." A/B 생성 UI 시안(preset/자유조합)으로도 해소 불가 →
**조건 축소가 아니라 제품 의미 분리**로 재정의 (사용자 + Codex + Claude 3-way 합의).

**결정 (서버 스키마/evaluator 무변경 — UI·validation 계층 재구성)**:

1. **일반 비교알림 = absolute만** ("차이 벌어지면"[gte] / "차이 좁혀지면"[lte]). signed
   ("더 비싸지면/더 싸지면") 문구는 UI에서 제거 — 서버는 signed 평가 능력 유지(추후 확장).
   absolute는 |A−B|=|B−A|라 **left/right(기준/상대) 개념 자체가 UI에서 소멸** — "두 소스 선택"만.
2. **테더 탭 비교알림 = 거래소 5개끼리만** (upbit/bithumb/coinone/korbit/gopax). cross-world
   (USDT vs 환율계) 비교는 김프알림이 전담 — 역할 분리.
3. **김프(역프) 알림 신설 — 테더 탭 전용, signed 전담**: left(기준) ∈ 거래소 5 ×
   right(비교) ∈ {hana, kb, investing}. **krx는 A1 범위에서 제외** — ADR-038(entitlement)
   구현 후 상대 집합에 추가 (codex 2026-07-04: A1이 미구현 게이트에 결합되는 것 방지,
   김프 가치는 환율계 3소스 대비로 이미 성립).
   조건 = 김프 값(부호 있는 spread) + 이상(gte)/이하(lte) 토글. threshold 범위 sanity =
   현 시세의 ±50%. **signed+gte+음수 threshold 조합이 1급 시민** (구 "크로싱-백 포기" 판단은
   김프알림 도메인에서 뒤집힘 — 사용자 시나리오: 역프 −30 이하 알림→매매→김프 −10 이상
   알림→반대매매. 서버는 원래 지원, UI만 개방). 저장은 동일 comparison_alerts 테이블 —
   김프알림/비교알림 구분은 diff_type(signed/absolute)으로 자연 구분, 신규 컬럼 불요.
4. **테더 탭 단일 가격알림에서 krx 제거** (iOS picker — 거래소 5만). KRX 단일 가격알림은
   달러 탭(게이트 사용자, 후속)으로 이동. 그래프/시세 막대의 krx는 유지(게이트 조건부).
5. **달러/엔/유로 탭 비교알림 = 향후 과제** (섹션 자체 미배치 유지). 정책 확정분: DXY 제외
   탭 내 전 소스 absolute 비교 (+달러 탭은 krx 게이트 조건부).
6. **서버 validation 재정의 (S3 보정)**: tether tab — absolute pair는 거래소 5끼리만 /
   signed pair는 (거래소 5) × {hana, kb, investing} (krx는 ADR-038 후 추가 + entitlement
   403). 위반 조합 400. **구 invariant(COMPARISON_TAB_SOURCES == 그래프 catalog krw series)는
   폐기** — 그래프 표시 소스 ≠ 비교 허용 소스가 새 정책의 본질 (정합 잠금 테스트 교체). usd/jpy/eur 허용 집합은 기존 유지(비교알림 개방 시 적용).
   **invariant (codex 2026-07-04)**: absolute=일반 비교/signed=김프라는 섹션 구분은 이
   validation이 diff_type×pair 조합을 강제할 때만 성립 — 신규 구분 컬럼 불요의 전제.
   **threshold 부호 규칙**: absolute는 threshold ≥ 0 강제(음수 absolute gte는 항상 참에
   수렴 — 422), signed는 음수 허용(김프 도메인).
   **absolute canonical ordering**: left/right 개념이 UI에서 소멸했으므로 absolute 저장 시
   (source,asset) 사전순으로 (left,right) 정규화 — A−B/B−A dedup 중복 차단 (ADR-037 Open 6을
   absolute에 한해 closed; signed는 방향 의미 보존이라 비정규화 유지).
   **기존 데이터 처리**: 신정책 위반 조합의 잔여 row(dev 계정 등)는 A1 배포 시점 one-time
   cleanup(delete)으로 제거 — 생성/수정 validation이 이후 유입을 봉쇄하므로 evaluator에
   정책 재검사 이중 로직은 두지 않음 (flag off + 실사용자 생성 0 시점이라 안전).
7. **UI 결정 supersede**: Decision 5(curated presets) + 2026-07-04 자유조합/A·B 시안은 본
   Amendment로 전면 supersede — 생성 UI는 "비교 알림"(absolute 2조건)과 "김프 알림"(signed,
   김프 값+이상/이하)의 **별도 2개 섹션/시트**로 재구성.

### Slices (Amendment 재스코프)

- **A1 서버**: validation 재정의(조합 정책 테이블) + 김프 조합 krx entitlement 검사(ADR-038 의존).
- **A2 iOS**: A/B 시트 폐기 → "비교 알림" 시트(소스 2택 + 벌어지면/좁혀지면 + 금액) +
  "김프 알림" 섹션/시트(기준 거래소 + 비교 상대 + 김프 값[±] + 이상/이하) 분리.
- **A3**: flag 활성 + canary (기존 계획 유지).

### 구현 land 기록 (2026-07-06~07 — S1~S4 + A1~A5 완료·운영 활성)

Amendment 재스코프 + 후속 편집 슬라이스가 모두 land. flag `COMPARISON_ALERT_ENABLED=true`
운영 활성(2026-07-06, 프리론치 — 앱 미출시라 테스트 단말만 발화, 실행 컨테이너+.env 확인).
서버 커밋은 exchange-rate repo, iOS는 fxi-ios repo.

**서버 (exchange-rate)**:
- **S1~S3** (`673bd9e`/`ec9f4a7`/`68cc9b9`): `comparison_alerts` + `comparison_notification_logs`
  모델/마이그레이션 · unified lookup + dual-trigger evaluator(3 hook, flag-off dormant) ·
  REST 5종 + tab-scope 검증 + schemas.
- **A1** (`dab9118` + hard cap `8fea7ea`): validation 재정의(absolute=탭별 대칭 집합 +
  threshold≥0 + canonical / signed=테더 전용 김프[거래소×hana/kb/investing]) +
  `canonicalize_absolute_pair` + hard cap `abs(threshold)<=10000` + one-time cleanup 스크립트.
  production cleanup 1건 삭제 후 dry-run 0.
- **A3** (env, 코드 커밋 없음): `COMPARISON_ALERT_ENABLED=true` + force-recreate. iOS canary
  성공(비교/김프 FCM 수신 + once/repeat/토글 + 로그/히스토리).
- **A4 편집** (`21c04c8`): PUT에 threshold + operator(방향) 확장. 변경 시 §7 리셋
  (triggered/last_notified 초기화 → once '발송됨' 재활성화). pair/diff_type은 편집 불가.
  + 히스토리 diff_type 필터 분리(`3682a5f`, 김프/비교 섹션별 — `get_comparison_notification_logs`
  에 diff_type 파라미터).

**iOS (fxi-ios)**:
- **S4** (`5cdf21b` 모델/Service/VM/FCM + `4d92afb` 섹션/시트/히스토리) / **A2** (`d32ec98`):
  비교/김프 2개 섹션(diff_type 분리) + 생성 시트 각각(비교=거래소 2택+벌어지면/좁혀지면 /
  김프=기준×상대+김프값[±100 기본, ±1000 넓은범위 세션1회]+이상/이하 가속 스테퍼).
- **히스토리 섹션별 분리** (`0451c0e`): 김프/비교 히스토리 diff_type 필터 + 제목 분리.
- **A4 편집** (`f5872a2`): row 탭 → 편집 시트(threshold/방향/반복).
- **A5 소스 편집** (`549ae3e` + 깜빡임 fix `eab1e30`): pair 편집 가능 → **삭제+재생성**
  (pair 변경 ≈ 새 알림, 새 id). 저장 전 중복 차단(생성/편집 둘 다, "이미 등록된 알림" +
  저장 비활성). VM `duplicateAlert`(absolute set 비교/signed 방향/self 제외) +
  `replacePairAlert`(create→delete, delete 실패는 `.oldNotDeleted` surface — 조용한 실패 금지).

**pair 편집 설계 결정 (2026-07-07 — Workflow 3-lens + subject/proxy 분석)**: 단일 소스/은행
알림이 "소스"를 바꿀 수 있는 건 그게 subject의 proxy/근사대체라서고(은행 알림도 통화쌍은
`let` 고정, 거래소 알림도 asset=usdt-krw 고정 — subject 축은 잠김), 비교/김프의 pair는 그
자체가 subject이며 threshold가 그 pair의 gap에 묶여 있어 pair 변경 = 다른 알림. → **삭제+재생성**이
정합(서버 pair-mutating PUT 대신 client delete+create — 새 id·정직한 identity·서버 변경 0).
중복 collision은 client `isDuplicate` 가드(테더/은행 알림 선례)로 처리 → 서버 신규 dedup 정책 불요.
2:1 판정(단순성·의미정합 유지 vs 일관성 개방) 후 사용자가 delete+create 방식으로 개방 채택.

**미결(ADR-038 의존)**: 김프 비교 상대에 krx 추가는 ADR-038(entitlement 게이트) 구현 후.

## ADR-038: KRX 달러선물 노출 게이트 — 3단 게이트 + 별도 topic + entitlement 수동 부여

**날짜**: 2026-07-04
**상태**: **사용자 노출 축 완료 (2026-07-10)** — G1/G2(d1405e2) · D2 독립 topic(487a245/dab40bd) ·
D4 전 섹션(시세 8b4388d / 그래프 ae1928b / 가격알림 a47e9e2 / 비교알림 8137aa7·436e978) ·
revoke full-cycle 실기기 · 푸시 문구 5종. **잔여 Open = 2번(WS per-user 인증)뿐** —
Security/Access Hardening 트랙(비구독자 1h REST/구독자 WS 방향)에서 일괄 해소 예정.
**결정자**: Jay + Claude + Codex (3-way)

### Context

KRX 달러선물은 [KRX_CANARY.md](KRX_CANARY.md) 정책상 optional source이며 시세 재배포 권리
검토가 미완( ADR-037 Open 5). 전 사용자 공개 대신 **구독 + 운영자 승인 사용자에게만 노출**하고,
상황에 따라 노출 수준을 단계적으로 조정할 수 있어야 한다. (작성 시점) 달러선물은 `usdt:krw`
topic의 optional group(`data.usd_krw_futures`)으로 전달되며 독립 topic 없음 — Decision 2
구현(2026-07-08)으로 독립 topic `krx:usd-krw-futures` 분리 완료.

### Decision 1 — 3단 게이트 (캐스케이드)

| 게이트 | 주체 | 수단 | 닫히면 |
|---|---|---|---|
| **G1 user entitlement** | 운영자 | DB 수동 부여 (사용자별) | 해당 사용자만 KRX 미노출 |
| **G2 server distribution** | 서버 env | `KRX_CLIENT_DISTRIBUTION_ENABLED` (신규) | 전 단말 KRX 미노출 (수집은 지속) |
| **G3 collection** | 서버 env | `KRX_FUTURES_ENABLED` (기존) | 수집 자체 중단 |

- **G1은 운영자 수동 부여** (사용자 확정 2026-07-04): 앱 내 입력 UI 없음 — 이스터에그/액세스
  코드 방식은 **기각** (Apple 2.3.1 hidden features 리젝 리스크 + 수동 부여가 대상 사용자
  직접 통제에 더 적합). 서버 DB 테이블(예: `user_entitlements(user_id, key='krx_futures',
  granted_at)`)에 운영자가 직접 INSERT. 필요 시 관리 스크립트/admin API 후속.
- **G2 범위 = 모든 client-facing KRX distribution** (codex 보강): topic 발행뿐 아니라
  graph v2 catalog/tab의 krx series, topic snapshot/REST bootstrap, KRX 관련 알림 생성 전부 —
  G2 off면 제외/403. "topic만 차단"으로 좁게 정의하면 무인증 graph 경로로 누출.
- 판정 단일화: 클라는 게이트 3개를 조합 계산하지 않음 — **서버가 최종 `krx_visible` 단일
  신호를 내려줌** (G3 ∧ G2 ∧ G1 ∧ premium). 전달 위치는 구현 시 확정(Open 1 — 후보:
  구독 상태 API 확장 / 신규 GET /api/entitlements).

### Decision 2 — 별도 topic `krx:usd-krw-futures` 신설 (옵션 B, 사용자 확정)

- `usdt:krw` topic의 `usd_krw_futures` optional group **제거** → KRX는 독립 topic으로만 발행.
  (신규 앱 미출시 — iOS 코드 동시 수정으로 마이그레이션 부담 없음. 구버전 운영 앱은 테더 탭
  자체가 없어 무영향.)
- entitled 단말만 이 topic 구독 (테더 탭 김프알림 비교소스/그래프 + 달러 탭 전 섹션).
- G2 off → 서버가 topic 발행 중단 (payload 필터링 불요 — topic 단위 차단이 옵션 B 채택 근거).

### Decision 3 — 강제선의 현실 (per-user 한계 명시)

`/ws`는 **익명**(main.py:1091 — 인증 없음)이라 topic 구독 자체를 사용자별로 막을 수 없음
(codex 지적, 코드 확인 2026-07-04). 1차 강제선:

- **인증 있는 REST(알림 API) = G1+G2 서버 강제**: 김프알림 krx 조합 생성/수정 403,
  KRX 단일 가격알림 403 — entitlement 검사 가능.
- **무인증 REST(graph v2 catalog/tab, topic snapshot) = G2만 서버 강제** (codex 정정 —
  graph v2는 인증이 없어 per-user 강제 불가): G2 off면 krx series 전역 제외, G2 on이면
  per-user 노출은 클라 UI gate(krx_visible)가 담당.
  - ⚠️ **Amendment 2026-07-25 (ADR-039 §8.1 E3 land)**: `GET /api/v2/topics/snapshot`은
    **더 이상 무인증이 아니다** — Firebase + premium + (KRX는) G1 entitlement를 서버가 강제한다.
    비-entitled에겐 krx topic이 `unknown_topic` 404 + `supported_topics` 에코 양쪽에서 빠져
    **미지원 topic과 구분 불가**(§3.2 존재 비노출). `graph v2 catalog/tab`은 여전히 무인증이라
    이 bullet이 그대로 적용된다(ADR-039 Stage B에서 해소 예정).
- **WS = global flag(G2) + 클라 UI gate**: G1 없는 단말이 krx topic을 구독해도 시세 payload를
  받을 수 있으나(시세는 민감정보 아님 — 목적은 노출 제어) 클라가 그리지 않음. **완전한
  per-user WS/무인증 REST 강제는 인증 도입 후속 과제** (Open 2).

### Decision 4 — 노출 매트릭스 (게이트 all-open 시)

- 테더 탭: 그래프 series + 시세 막대 + **김프알림 비교소스** (단일 가격알림/일반 비교알림에서는
  제외 — ADR-037 Amendment 4).
- 달러 탭: 그래프/은행별환율(시세)/가격알림/비교알림 **전 섹션 추가** (후속 구현).
- 게이트 하나라도 닫히면: 양 탭 모든 KRX 표면 미노출 (krx_visible=false + REST 미포함 +
  topic 미발행[G2] 또는 미데이터[G3]).

### Open

1. ~~`krx_visible` 전달 위치~~ → **확정: 신규 GET /api/entitlements** (G1/G2 land — 단일 책임
   +확장성, register-device/알림 목록 응답 얹기 기각. PENDING은 503 대신 200
   {krx_visible:false, premium_pending:true, retry_after_seconds:5} — read fail-closed).
2. WS 구독 인증 (per-user topic 강제 — 별도 트랙).
3. ~~entitlement 부여/회수 운영 도구~~ → **scripts/grant_entitlement.py land** (grant/revoke/list,
   dry-run default + production guard. revoke 시 해당 사용자 KRX 단일알림+김프 counter 알림
   자동 disable — evaluator는 entitlement 재검사 안 함[hot path], 회수 시점 차단). admin API는 후속.
4. ~~graph v2 catalog per-user 분기~~ → **확정: G2 전역 게이트만 서버 강제 + per-user는 클라
   krx_visible gate** (무인증 유지 — catalog auth 추가 기각).

### G1/G2 서버 슬라이스 land 기록 (2026-07-08, `d1405e2` — 배포/마이그레이션/grant 완료)

- **G2 env**: `KRX_CLIENT_DISTRIBUTION_ENABLED`(default false, 운영 true) + 파생
  - ⚠️ **Amendment (2026-08-12)**: 위 "운영 true" 는 **2026-07-08 land 시점의 기록**이다.
    저장소의 2026-07-22 라우트 감사 완화 기록은 topic 서버의 **prod flag OFF**까지만 말한다
    (`CLAUDE.md:38`). 이 근거만으로 당시 두 env 의 정확한 조합이나 **현재 운영값**을 확정하지
    않는다. `KRX_CLIENT_DISTRIBUTION_ENABLED` 와 `TOPIC_DISPATCHER_ENABLED` 의 현재값은 활성화
    GO **직전에 서버에서 직접 측정**한다.
    ⚠️ 이 amendment 는 ADR-041 구간 **밖**이라 code-claim 원장·semantic-review 해시에
    포함되지 않는다. **독립 리뷰로 고정되지 않았고**, 별도 수동 검토 대상이다.
  `KRX_CLIENT_DISTRIBUTION_EFFECTIVE`(=G3∧G2 — G3 off 시 Redis/DB 잔존값 노출도 차단, codex 보강)
  + `KRX_TOPIC_INCLUDE_EFFECTIVE`(usdt:krw usd_krw_futures group 포함 3지점 교체:
  broadcast hook / direct_coalesced trigger / WS·REST snapshot 공용 builder).
- **G1**: `user_entitlements`(user_id+key UNIQUE) + migrate 스크립트 + `app/entitlements.py`
  (FastAPI 비의존 — has_entitlement/krx_gates_open/krx_alert_gate_error/compute_krx_visible).
- **알림 API 403 강제**: source POST/PUT + comparison POST/PUT(김프 counter krx —
  KIMCHI_COUNTER_SOURCES에 krx 활성, 구조 유효성은 validator·게이트는 handler 분리).
  PUT은 최종 조합 기준 무조건 검사 + 예외는 **끄기 전용**(is_enabled=False 단독)뿐 —
  재활성/변경+끄기 동시/krx로 변경+끄기 우회 전부 차단 (codex blocker fix).
- **graph v2**: 무인증이라 전역 게이트만 — runtime accessor로 catalog/tab/1d/in_progress/
  precompute 전 경로에서 G3∧G2 off 시 krx series 제외 + lifespan startup 테더 그래프 캐시
  5키 무조건 DEL(게이트 flip 잔존 방어).
- **telemetry**: `krx_topic_include_effective` 추가 (raw include와 게이트 결합값 구분).
- **운영 상태**: G3=G2=true(현 노출 유지) + Jay uid entitlement 부여 — 즉 배포 전후 사용자
  가시 변화 0, 게이트 인프라만 활성. tests 3449 green (신규 14 + 기존 8 갱신).
- **후속 슬라이스 순서** (codex 합의): ① iOS krx_visible 소비(전 KRX 표면 단일 gate + 김프
  counter krx 추가) → ② 별도 topic krx:usd-krw-futures + usdt:krw group 제거(Decision 2) →
  ③ 달러 탭 편입(Decision 4).

### Decision 2 서버 슬라이스 land 기록 (2026-07-08)

- **신규 `app/krx_topic_publisher.py`**: `KRX_TOPIC="krx:usd-krw-futures"` +
  `build_krx_topic_payload`(data.usd_krw_futures entry — 구 group과 동일 shape, iOS
  TopicSourceEntry 하위호환) + `load_krx_topic_entry(db=None)`(Redis-first, db 제공 시만
  crud legacy dict fallback — `_normalize_entry` 재사용) + `publish_krx_topic_snapshot`
  (guard: dispatcher → `KRX_CLIENT_DISTRIBUTION_EFFECTIVE` → subscriber 0) +
  `request_krx_topic_publish`(조기 gate skip 후 `schedule_on_loop` marshal — sync close
  finalizer/async tick 양쪽 안전). KRX tick은 Redis writer가 이미 5초 coalesce라 **별도
  coalescer 없는 경량 설계**.
- **usdt:krw에서 KRX 완전 제거**: builder `krx_futures_rate` 파라미터/조립 블록 +
  `include_krx` + `KRX_TOPIC_INCLUDE`/`KRX_TOPIC_INCLUDE_EFFECTIVE` env + telemetry
  `krx_topic_include*` 필드 제거. krx_kis 3 trigger(DB-bound/Stage E/close finalizer)는
  `request_krx_topic_publish`로 교체 (usdt:krw 재발행 폐기).
- **snapshot/G2-off 계약**: `supported_snapshot_topics()`가 EFFECTIVE=true일 때만 krx topic
  포함 — off면 WS snapshot skip + REST bootstrap 404 + 발행 중단 (동일 gate 3면 일치).
- **legacy 410 안내 갱신**: `LEGACY_REMOVED_RATE_TOPICS["usd-krw-futures"]` = usdt:krw →
  `krx:usd-krw-futures` (codex blocker — 구 안내는 KRX 제거된 topic으로 유도).
- **codex blocker 2 fix**: crud `get_latest_source_rate`는 legacy dict 반환 — attribute
  접근 대신 dict 그대로 `_normalize_entry`에 전달 (테스트도 실제 shape로 잠금).
- tests: 44 계약 회귀 재작성 + `tests/test_krx_topic_publisher.py` 신규 16 + KRX snapshot
  positive 3 — 전체 3459 green.
- **iOS 소비 슬라이스 land (2026-07-08, fxi-ios `dab40bd`)**: `setKrxVisible` flip이
  krx topic 구독 lifecycle 관리(subscribe+REST bootstrap / unsubscribe+취소) +
  `applyKrxSnapshot`이 tetherStore에 merge — 5개 소비처(usdtDisplayState/Kimchi liveRate/
  GraphV2 live-tail/Source·ComparisonAlertAddSheet)가 tetherTopicRates 경유라 코드 변경 0.
  codex 2 blocker 반영: ① krx 수신이 tether 신선도(lastTetherTopicAt)를 연장하지 않음 —
  usdt:krw 발행 사망 시 stale entry fresh 취급으로 MODE 2 revert 무력화 방지(pre-split
  full payload 재발행과 비등가; KRX 장마감 무발행이 정상이라 krx 자체 시간 staleness도
  없음 — 종가 표시는 store 잔존으로 지속). ② in-memory fallback tetherReceived 가드 +
  persist 게이트 — krx 단독 merge가 KRX 1행 렌더/KRX-only disk cache로 새는 경로 차단.
  iOS 152 tests green.
- **D4 ① 달러 탭 시세 편입 land (2026-07-08, fxi-ios `8b4388d`)**: `Bank.krx` 신규
  (displayCases 제외 — 가격알림 picker 오염 방지, ③에서 gate와 함께 편입) + defaultOrder
  인베스팅 다음 + sanitize 위치 특례(기존 저장 사용자 동일 위치) + `displayState(for:)`
  usd 탭 한정 주입. **표시 조건 = krxVisible ∧ store entry 존재** — FX/tether freshness
  모두 비결합(신선도 3규칙: krx 수신은 tether 신선도 비연장 / krx 자체 시간 staleness 없음
  [장마감 무발행 정상] / tetherReceived 가드). **시트 gate-off preference 유실 수정**
  (codex blocker): sanitize 누락-append가 defaultOrder 기본값 재삽입 → gate-off 시트 완료
  시 KRX 커스텀(위치/숨김) 리셋 — Bank/Source 양쪽 `mergingPreservedItems` merge-back
  경유로 보존 + 테더 SourceCustomizeSheet의 동일 잠복 결함·reset 버튼 gate 누락도 동시
  수정. iOS 159 tests green. (후속 polish `ba6529f`: BankIconView krx 아이콘을 테더
  SourceIconView와 동일 렌더링으로 + 순서 시트 KRX 항목 달러 탭 전용[currency 컨텍스트].)
- **D4 ② 달러 탭 그래프 편입 land (2026-07-08, 서버 `ae1928b`+`c22c4e7` 배포 / iOS `7ffec85`)**:
  usd 탭 전 기간(1d/1w/3m/1y)에 krx 시리즈 (investing 다음, **default OFF** — 2026-07-03
  "최소 2개 시작" 결정 정합). 게이트/reader 변경 0 — krx. prefix 필터 + 테더 탭과 동일
  데이터. GRAPH §3/§4 개정. iOS는 기존 메커니즘 자동(필터/토글/라벨/색) + 유일 gap이던
  usd live-tail 보충(krxTopicSourceRate 접근자 — fx topic 생존 시만 append, krx 자체
  신선도는 600s threshold). **배포 실측 함정 1건**: 3m/1y/1w read-through Redis 캐시가
  구 시리즈 구성을 TTL(≤30분)간 잔존 — lifespan purge를 테더 5키 → +usd 5키로 확장
  (`c22c4e7`). 전 기간 points 실측(144/124/61/245).
- **D4 후속 krx 우선 정책 land (2026-07-09, 서버 `0b81ea1` 배포 / iOS `c969373`)**: 달러선물
  활성 단말(=운영자 grant=실선물 거래자)은 krx를 양 탭 최상단·기본 표시 우선. **(1) 소스 순서**:
  Bank/Source defaultOrder krx 맨 앞(비krx는 display 필터로 무영향). **(2) 테더 시세 기본 표시**:
  SourcePreferenceManager.effectiveDefault(krxVisible) = 거래소 5 + (krx면 달러선물 else 하나),
  investing/kb OFF — hasPersisted로 미수정 시 krxVisible live 추적/수정 후 고정. **(3) 테더 그래프
  기본 토글**: 서버 default_visible base=[upbit,bithumb,hana,krx]에서 client가 상호배타(krx 단말
  hana drop / 비krx krx drop), **DXY 기본 OFF**. flip 시 visible+initialized 페어 제거 후 재적용
  (codex blocker — ensureVisible insert-only). **(4) 참조군 토글 순서** krx→investing→kb→hana.
  **(5) 김프 비교소스** krx 우선. **(6) DXY↔선물지수 상호배타 토글**(테더). 시트 완료 무변경 시 apply
  생략(hasPersisted 오고정 방지). codex 3라운드(default swap 미적용/hasPersisted 오고정/visible
  미제거 residual) 반영. iOS 167 tests. ⚠️ 기존 단말은 initializedSeries가 옛 토글을 잠가 새 default가
  소급 적용 안 됨(신규 설치/미수정만) — 필요 시 "기본값으로 초기화".
- **D4 ③ 달러 탭 KRX 가격알림 land (2026-07-09, fxi-ios `a47e9e2`)**: KRX 단일 가격알림을
  달러 탭에 편입. **서버 변경 0** (source API `/api/source-notification-settings`가 F-2로
  derivative[krx] 이미 허용 + G1/G2 403 게이트 완비 — iOS만). 핵심 = **은행 알림/소스 알림은
  별개 시스템**이라 KRX는 반드시 source API (은행 시트에 섞으면 잘못된 API + 두 모델 id 충돌).
  `SourceAlertScope` enum(tether/usdKrx)으로 소스 알림 섹션/시트 파라미터화 재사용. **이중 노출
  버그 수정**: `tetherTabSettings`가 usd-krw-futures를 포함해 KRX 알림이 테더 탭에도 뜨던 것 →
  usdt-krw 전용으로 좁히고 usd 탭은 `usdKrxSettings`(usd-krw-futures) 파티션. liveRate는
  `krxTopicSourceRate`(tether freshness 비결합 — 장마감 후 종가 유지). 섹션 게이트
  (currency==.usdKrw ∧ krxVisible)가 편집-prefill/revoke orphan 자동 방어(미렌더). history
  assetFilter 3콜사이트 통일(codex blocker). MVP: 임계값 정수(1원)/소스 30 쿼터 공유.
  iOS 172 tests(SourceAlertScopeTests 5 + pbxproj 4엔트리 수동 — FXiTests 비동기화 그룹).
- **D4 ④ 비교알림 편입 land (2026-07-10, 서버 `8137aa7` 배포 / iOS `436e978`) — Decision 4
  전 섹션 완료**: usd 일반 비교알림(absolute)에 KRX 달러선물 허용(entitled 전용) + FX 3탭
  (usd/jpy/eur) 비교알림 섹션 신설(환율알림 아래). **서버**: `COMPARISON_ABSOLUTE_SOURCES["usd"]`
  에 krx 추가(jpy/eur/tether 불변) + POST/PUT 게이트 `KRX_PAIR in (left, right)`로 확장
  (canonical 사전순 정렬로 krx가 양쪽 어느 슬롯이든 — codex가 구 signed+right 한정의 구멍
  적발) + **revoke `or_(left==krx, right==krx)` 확장**(구 쿼리면 회수 후 absolute KRX 비교알림
  계속 발사) + endpoint 403/200 게이트·revoke matrix·usd absolute E2E 테스트 — 3474 passed.
  **운영 불변식**: KRX 비교알림 발화는 `KRX_ALERT_EVALUATOR_ENABLED` 전제(KrxAlertTickHandler가
  비교 관측의 단일 진입점 — 의도된 결합). **iOS**: `ComparisonAlertScope`(.tether/.fx) 공용
  일반화 — usd 소스 그리드는 [달러선물(krxVisible, 맨 앞 — krx 우선 정책)]+인베스팅+8은행,
  jpy/eur는 9개. FX 은행 def는 Bank enum 동적 생성(`fxBankDefinition` — SourceRegistry.all
  미등록으로 테더 목록 오염 방지) + find fallback(비교 리스트 raw 코드 → 한글). liveRate는
  fx=fxTopicRates/krx=krxTopicSourceRate(비결합). sanitize는 usdtTabSources membership으로
  명시화. 김프(signed)는 테더 전용 유지. JPY 정수 threshold는 MVP 수용(후속 후보). 180 tests.
- **revoke full-cycle 실기기 검증 (2026-07-09~10)**: 회수(단일알림 3+김프 counter 1 자동
  disable + 전 표면 동시 소멸) → 재부여(표면 복원 + disable 잔존 = 설계) 양방향 통과.
- **푸시 문구 5종 정리 (2026-07-09~10, `3cfd168`+`22e9c23`+`fea46e2`)**: 현재가 title 마지막
  (NBSP 2 간격 — iOS 일반 스페이스 collapse 방지), body=목표/조건만, KRX 표시명
  "미국달러F"→"달러선물" 서버-앱 통일, 비교 '원' 제거, 김프 percent. 실기기 3종 수신 확인.
- **후속 방향 (구현 보류)**: 비구독자=1시간 REST 스냅샷/구독자=WS 실시간 — WS 인증 도입 시
  Open 2(per-user topic 강제)와 같은 hardening 트랙에서 일괄 해소 (memory:
  project_security_hardening_direction). Open 2는 그 트랙 전까지 유지.

## ADR-039: 무료/구독 차등 접근 모델 — hourly 무료 스냅샷 + 최신-데이터 인증 강제 + legacy 종료

**날짜**: 2026-07-17
**상태**: **Proposed** (설계 수렴 rev5, codex 5-round 검토). 상세 설계 = [FREE_TIER_ACCESS_MODEL_PLAN.md](FREE_TIER_ACCESS_MODEL_PLAN.md). Final 승격엔 제품 결정 **S4(유예 기간)만** 남음 — S5(KRX revoke latency)는 2026-07-25 bounded-lease v1 15분으로 확정.
**결정자**: Jay + Claude + Codex (3-way)
**닫을 예정 (Final 시)**: ADR-038 잔여 Open 2(WS per-user 인증) — 이 ADR의 WS 인증 계약(§8) **구현 완료 시** 해소. **현재 Proposed·미구현이라 Open 2는 유지**.

### 요약 (상세는 PLAN 문서)

- **문제**: 비구독=매시간 스냅샷 / 구독=실시간 WS 제품 방향인데, 최신 rate/graph endpoint 대부분 무인증(`app/main.py` 894~2624 auth 0건) → 페이월이 UI에만 존재.
- **결정**: D1 hourly=Firebase 인증만(KRX 항상 제외) / D2 최신 realtime 표면=premium(KRX는 +entitlement, ADR-038 `krx_visible`) / D4 신규 앱 legacy fallback 금지 / D5 무료 그래프=real hourly.
- **접근 강제**: 무인증 최신-데이터 endpoint 전수(11종) 식별 → **Stage A**(신규 표면 인증, 출시 시) + **Stage B**(legacy REST/WS 종료, 양 플랫폼 <1% + 유예).
- **양 플랫폼**: iOS reference → Android 이식(Android는 완전 legacy). Stage B는 iOS·Android 양쪽 기준([REALTIME_ARCHITECTURE_PLAN.md:479](REALTIME_ARCHITECTURE_PLAN.md#L479) 계약 승계).
- **WS 인증 계약(testable)**: subscribe payload 토큰 → `subscription_ack`(accepted/rejected) / `subscription_error`(invalid_token) / bounded-lease revoke.
- **롤아웃**: dormant→flip(C6 규율). **첫 슬라이스 = client-version 관측 계측**(iOS·Android `X-Client-*` + nginx access log, enforcement/동작변경 없음) — 2026-07-17 구현.

### 미결 (제품 결정, Final 전)

- **S4** legacy 유예 기간: 기존 6개월([REALTIME_ARCHITECTURE_PLAN.md:483](REALTIME_ARCHITECTURE_PLAN.md#L483)) 유지 vs 단축(권고 <1%+30일). 명시적 supersede 필요.
  ⚠️ S4는 *legacy 종료 시점* 결정이라 **Stage B 게이트** — WS 인증(1C) 구현의 블로커는 아니다.
- **S5** KRX revoke 반영 지연 — **RESOLVED = bounded-lease v1, 15분**(2026-07-25). 즉시 제거(UID registry + Redis 제어
  이벤트)는 별도 후속. 서버 lease **상한** 15분 / 클라 재인증 ~12분+jitter / 만료 시 topic 제거 +
  `reauth_required`. ⚠️ **실값은 상한보다 짧을 수 있다** — 만료는 부여 시각이 아니라 *가장 오래된
  authoritative 관측*에서 흐르므로, 관측 축이 많은 KRX(identity·premium·entitlement)는 무료 topic
  보다 먼저 만료된다. **만료 시 topic 제거 + `reauth_required` 는 설계이고 아직 미구현이다**
  (과도기: 만료 lease 를 `duration 0` 으로 남긴다 — FREE_TIER_ACCESS_MODEL_PLAN.md §8).
  leak window를 가르는 건 토큰 신선도가 아니라 **서버의 UID 기준 entitlement 재조회 주기**.
  상세·구현 규모 실사는 [FREE_TIER_ACCESS_MODEL_PLAN.md §7 S5 / §8](FREE_TIER_ACCESS_MODEL_PLAN.md).

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
- 2025-11-17: ADR-014 작성 (HTTPS/SSL 도입 - Let's Encrypt vs Cloudflare vs Self-Signed)
- 2025-12-02: ADR-015 작성 (WebSocket Graph Integration vs Incremental API - 모바일 최적화)
- 2025-12-05: ADR-016 작성 (인증/알림 인프라 - AWS RDS + Firebase Auth + FCM)
- 2026-01-10: ADR-017 작성 (MIBANK 환율 파싱 - Currency-Code 기반 + 3단계 검증)
- 2026-01-29: ADR-018 작성 (Investing Cloudflare 차단 대응 - curl_cffi TLS 지문 위장)
- 2026-03-10: ADR-019 작성 (DXY 보조지표 - granularity 기반 2-part merge 전략)
- 2026-03-12: ADR-020 작성 (DXY 크롤링 아키텍처 전환 — 독립 크롤러에서 Investing 동반 추출로)
- 2026-03-28: ADR-021 작성 (환율 뉴스 피드 — Redis-only + KB/RSS 병행 수집)
- 2026-04-01: ADR-021 개정 (v2 단순화 — noise_only 일원화, 24h 윈도우, match_type/flash/category 삭제)
- 2026-04-27: ADR-022 작성 (DXY Yahoo fallback 시간/가격/fresh-age 가드 정책)
- 2026-04-27: ADR-023 작성 (데이터 보관 정책 30일 통일 — bank/source_rates/DXY realtime)
- 2026-04-27: ADR-024 작성 (미국달러지수 선물 분리 저장 + DXY_MODE 제거)
- 2026-04-28: ADR-025 작성 (DXY 현물 외부 fallback 체인 — CNBC 추가 + Yahoo 격하)
- 2026-05-04: ADR-026 작성 (Redis-first broadcast hot path — latest mirror + DXY mirror로 DB-free 달성)
- 2026-05-06: ADR-027 초안 작성 (KRX 미국달러선물 Stage 2 전 REST snapshot/fallback + stale 정책)
- 2026-05-06: ADR-028 작성 (Topic-only Tether/KRX + legacy FX dual-emit)
- 2026-05-12: ADR-029 작성 (USDT source는 mirror cycle 미경유 — direct write + read-path DB fallback)
- 2026-05-13: ADR-030 초안 작성 (latest:index 책임 분리 — freshness는 per-key mirrored_at으로 판단, Proposed)
- 2026-05-13: ADR-031 초안 작성 (KRX 미국달러선물 Redis 통합 — 1차 부채 해소, stale/REST는 후속, Proposed)
- 2026-05-28: ADR-034 초안 작성 (source_daily_rates canonical daily table — Phase 2d 구현 설계, Proposed)
- 2026-05-28: ADR-034 Phase 2d Step 1 land (Open #14/#16/#17 → Accepted + app/models.py SourceDailyRate ORM + scripts/migrate_source_daily_rates.py + app/source_daily_rates.py helper)
- 2026-05-28: ADR-034 Phase 2d Step 2 land — Bithumb 24h candlestick dry-run validator (scripts/backfill_bithumb_source_daily_rates.py 신규 / 9 validation suite / 전체 904 candles 0 issue / KST 00:00 anchor 일관 확정 / 904일 span 누락 0 / Step 2/3 경계 보존 — DB write X)
- 2026-05-28: ADR-034 Phase 2d Step 2-2 land — Hana official_historical dry-run validator (scripts/backfill_hana_source_daily_rates.py 신규 / 10 validation suite / USD·JPY·EUR 4 case smoke 0 issue / 휴일 fallback 정확 검증 — 2026-05-24 일 → 2026-05-22 금 / close_only + basis_date + published_at + pbldSqn schema path 처음 활성 / Step 2/3 경계 보존)
- 2026-05-28: ADR-034 Phase 2d Step 2-3 land — KIS daily chain dry-run validator (scripts/backfill_kis_source_daily_rates.py 신규 / 13 validation suite / 2 mode dynamic+known-boundary-smoke 모두 exit 0 / GRAPH §7-new B-B probe 사례 A75606 20260518 close=1496.500 재현 / chain 사용 구간 [previous.expiry, this.expiry) 반열린 / today 제외 default / mapped/probe 분리 / KisAccessTokenManager 재사용 / Step 2 closed — Bithumb·Hana·KIS 3-source dry-run 완료)
- 2026-05-28: ADR-034 Phase 2d Step 3 first land — KRX current partial backfill writer (scripts/backfill_kis_source_daily_rates.py 확장 / --write + --start-date + --end-date + --allow-production-write / production guard early call — token/master/daily fetch 모두 전 즉시 차단 / transaction pattern upsert(commit=False) + range-filtered post-write SELECT + exact count + expected/written dates set / 7 post-write validations / Codex Round 7-8 보정 — production guard + range filter / SQLite override smoke 7 rows committed + idempotent rerun 통과 + boundary close=1496.500 DB level 재현 / Stage 1 commit (code land), Stage 2 push / Stage 3 production execution 별 GO)
- 2026-05-28: ADR-034 Phase 2d Step 3 production RDS execution 완료 (KST 18:30 — Stage 2 push + Stage 3 production write 모두 land / RDS manual snapshot fxi-pre-step3-2026-05-28 직후 / `--allow-production-write` 적용 / production verify: 7 rows committed + 7 post-write validations passed + drift 0 + range 7 + contract A75606 단일 + **boundary close=1496.500 production-level 재현** (GRAPH §7-new B-B probe 사례) + rate==close + basis_date/published_at IS NULL + enum/literal 모두 정확 / 운영 fastapi runtime 미교체 — Phase 2e endpoint switch 시점 별 배포 영역 / source_daily_rates read 호출자 0 — user-facing 영향 0 / manual snapshot 보존 — Step 3+ rollback anchor)
- 2026-05-28: ADR-034 Phase 2d Step 3 옵션 B Round 9 Stage 1 candidate — KRX chain previous + current 확장 (scripts/backfill_kis_source_daily_rates.py 확장 / --contract current|previous|both default current 후방 호환 / validate_write_range + write_with_transaction multi-contract 시그니처 확장 / post-write SELECT contract_code.in_() + range filter / (date_kst, contract_code) pair set 검증 강화 / Codex Round 9 Blocker 0 + help 문구 polish / Local SQLite smoke 5단계 모두 PASS — previous 18 rows / both 25 rows / idempotent + cross-mode regression / boundary 2026-05-18 → A75606 close=1496.500 both mode 재현 / Stage 1 commit 대상, Stage 2 push + Stage 3 production execution 별 GO 대기)
- 2026-05-28: ADR-034 Phase 2d Step 3 옵션 B Round 9 Stage 2+3 production execution 완료 (Stage 2 push + Stage 3 production write 모두 land / `--contract previous` only 진행 — current 7 rows 재-upsert 회피 + manual snapshot 생략 (옵션 B는 incremental write라 과보호, 자동 backup 1 Day + `delete_range` rollback anchor 신뢰) / production verify: 18 rows committed (A75605 [2026-04-20 ~ 2026-05-15]) + 7 post-write validations passed + drift 0 + current A75606 7 rows 영향 0 (cross-mode regression) + 전체 chain 25 rows + boundary 2026-05-18 → A75606 close=1496.500 유지 / 운영 fastapi runtime 미교체 — Phase 2e endpoint switch 시점 별 배포 영역 / source_daily_rates read 호출자 0 — user-facing 영향 0)
- 2026-05-28: ADR-034 Phase 2d Step 3 Hana first PR Round 1 Stage 1 candidate — USD partial backfill writer (scripts/backfill_hana_source_daily_rates.py 확장 / --write + --start-date + --end-date + --include-today + --allow-production-write + check_production_write_guard + validate_write_range + ensure_source_daily_rates_table_created + fetch_and_dedup_calendar_range (Hana-specific calendar-day loop + basis_date dedup) + write_with_transaction_hana (Hana 방향 metadata policy) / Codex Round 1 Blocker (post-write SELECT date_kst.in_(expected_dates) 정정 — KRX Round 7 Blocker 2와 동등 패턴, 비연속 expected_dates 휴일 gap 안 false failure 차단) + 보완 (개별 dates delete rollback anchor) + Non-blocker (help 문구) / Local SQLite smoke 8단계 모두 PASS — fake gap row 검증으로 Blocker 닫힘 근거 확보 / first run: 4 rows committed (calendar 7일 dedup, 5/23 토 + 5/24 일 + 5/25 월 대체공휴일 모두 5/22 fallback) / schema close_only path 첫 production-level 활성 / drift 0 / Stage 1 commit 대상, Stage 2 push + Stage 3 production execution 별 GO 대기 — manual snapshot 생략 권장 (incremental write))
- 2026-05-28: ADR-034 Phase 2d Step 3 Hana first PR Round 1 Stage 2+3 production execution 완료 (Stage 2 push + Stage 3 production write 모두 land / manual snapshot 생략 — Hana incremental write 자동 backup 1 Day + 개별 dates delete rollback anchor 신뢰 / `--currency USD --start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write` / production verify: 4 rows committed (Hana usd-krw `[2026-05-21, 2026-05-22, 2026-05-26, 2026-05-27]`) + post-write validations passed + drift 0 + 휴일 dedup 정확 (5/23/24/25 → 5/22 fallback, 2026-05-25 대체공휴일 자연 흡수 — KRX_CANARY 사고 캘린더와 일관) + **schema close_only path production-level 첫 활성** (high==low==close + ohlc_quality close_only + basis_date NOT NULL + published_at NOT NULL KST timezone-aware + metadata_json.pbldSqn 각 row별 다른 회차 1356/2513/1081/1271) / 전체 production source_daily_rates state: KRX 25 rows + Hana 4 rows + Bithumb 0 rows / 운영 fastapi runtime 미교체 — Phase 2e endpoint switch 시점 별 배포 영역 / source_daily_rates read 호출자 0 — user-facing 영향 0)
- 2026-05-29: ADR-034 Phase 2d Step 3 Bithumb first PR Round 1 Stage 1 candidate — USDT/KRW partial backfill writer (scripts/backfill_bithumb_source_daily_rates.py 확장 / --write + --start-date + --end-date + --include-today + --allow-production-write + check_production_write_guard + validate_write_range + ensure_source_daily_rates_table_created + write_with_transaction_bithumb (Bithumb 방향 metadata policy — 모든 top-level metadata None + candle_ts_ms/candle_ts_kst metadata_json 격리) / Codex Round 1 Blocker (--write + --limit hard reject — partial write 위험 차단, early check fetch 전) + Non-blocker 2 (candle_ts_kst validation + 연속성 회귀 가드 expected_count == (end-start)+1) + cleanup (InvalidOperation unused import 제거) / Local SQLite smoke 6단계 모두 PASS — production guard 차단 + limit reject + first run 7 rows + DB query (all candle_ts_kst present) + idempotent rerun / **schema의 모든 top-level metadata None path 첫 production-level 활성 준비** (KRX contract_code 필수 + Hana basis_date+published_at+pbldSqn 필수와 다른 가장 단순 row mapping) / Stage 1 commit 대상, Stage 2 push + Stage 3 production execution 별 GO 대기 — manual snapshot 생략 권장)
- 2026-05-29: ADR-034 Phase 2d Step 3 Bithumb first PR Round 1 Stage 2+3 production execution 완료 (Stage 2 push + Stage 3 production write 모두 land / manual snapshot 생략 — Bithumb incremental write 자동 backup 1 Day + `delete_range` rollback anchor 신뢰, 24/7 연속이라 range delete 안전 / `--start-date 2026-05-21 --end-date 2026-05-27 --allow-production-write` / production verify: 7 rows committed (Bithumb usdt-krw `[2026-05-21, 2026-05-27]` 연속) + post-write validations passed + drift 0 + 모든 top-level metadata None (contract_code/basis_date/published_at) + all candle_ts_kst present (KST 00:00 anchor 격리) + rate==close + close_basis=bithumb_24h_kst_close + source_method=bithumb_candlestick_backfill + ohlc_quality=source_ohlc / **ADR-034 §3 schema 3 직교 path 모두 production-level 활성 완료** (KRX source_ohlc+contract_code+rollover / Hana close_only+basis_date+published_at+pbldSqn / Bithumb source_ohlc+모든 top-level None+candle metadata_json) / 전체 production source_daily_rates state: KRX 25 + Hana 4 + Bithumb 7 = 36 rows / 운영 fastapi runtime 미교체 — Phase 2e endpoint switch 시점 별 배포 영역 / source_daily_rates read 호출자 0 — user-facing 영향 0)
- 2026-05-29: ADR-034 Phase 2d Step 5 daily append minimum first PR Stage 1 candidate — Bithumb only orchestrator (scripts/daily_append_source_daily_rates.py 신규 / orchestrator script — 기존 backfill_bithumb_source_daily_rates.py를 subprocess 호출, 신규 write logic 0 / --source bithumb (choices 잠금, 첫 PR scope) + --date YYYY-MM-DD (default yesterday KST) + --write (default dry-run = 3 command preview only) + --allow-production-write (writer subprocess에 forward) / subprocess timeout 180s + cwd=REPO_ROOT (production cron robustness) + script_path .resolve() / Codex Round 1 Blocker 정정 — parse_row_count Optional[int] + EXPECTED_ROWS_PER_SOURCE = {"bithumb": 1} + summary `returncode == 0 AND rows == expected_rows` 양방향 검증 (daily append freshness silent failure 차단) + Non-blocker 3 (cwd / script_path resolve / dry-run 문구 polish) / Local SQLite smoke 5단계 모두 PASS — py_compile + dry-run preview (3 command — validation/local write/production write) + write mode (1 row committed, PASS) + silent failure 시뮬 (--date 2020-01-01 → exit 1 FAIL) + argparse choices=["bithumb"] 차단 (--source hana invalid choice) / Hana observed_eod 별 PR (사전 read 5 항목 — bank_exchange_rates schema / Hana source naming / 전일 last 추출 기준 / source_method enum / missing day) + KRX 별 PR (close finalizer 통합) 분리 정책 / Stage 1 commit 대상, Stage 2 push + Stage 3 production cron 등록 별 GO 대기)
- 2026-05-29: ADR-034 Phase 2d Step 5 daily append minimum first PR Stage 2+3 production execution + OS crontab 등록 완료 (Stage 2 push + Stage 3 manual smoke production write + OS crontab 등록 모두 land / manual smoke verify: Bithumb 2026-05-28 1 row committed (`bithumb: PASS (1 rows committed, expected=1, exit=0)`) + post-write validations passed + drift 0 + candle_ts_kst=2026-05-28T00:00:00+09:00 KST anchor 격리 + 전체 Bithumb rows 8 (7 backfill + 1 daily append) / **OS crontab 등록**: `1 15 * * *` (UTC 15:01 = KST 00:01 매일) — 기존 `monday_reopen_exec.sh` cron 보존 + daily append line 추가, `/usr/bin/docker` 절대경로 사용 (cron PATH 회피) + `~/logs/daily_append.log` 로그 destination / cron 첫 발화 예정: 2026-05-29 UTC 15:01 / **운영 fastapi runtime 미교체** — APScheduler in-process schedule과 별 영역 (OS cron이 `docker compose run --rm` 새 one-shot container 띄움, 운영 fastapi process 영향 0) / 7일 안정 운영 모니터링 진입 / 전체 production source_daily_rates state: KRX 25 + Hana 4 + Bithumb 8 = 37 rows / source_daily_rates read 호출자 0 — user-facing 영향 0)
- 2026-05-31: ADR-034 Phase 2d Step 3 Hana observed_eod first PR Round 1 Stage 1 candidate — bank_exchange_rates observed_eod writer (scripts/backfill_hana_observed_eod_source_daily_rates.py 신규 + tests/test_hana_observed_eod_writer.py 신규 16 test / official_historical_backfill과 별 path — close_basis hana_observed_eod / source_method+ohlc_quality observed_rollup / bank_exchange_rates 내부 관측 read / KST→UTC naive boundary + carry-in baseline(prev) + 당일 changes rollup / **liveness gate ⊥ baseline quality 분리** — changes=write gate, prev age(<=7d)=rollup 포함 gate / case: 평일·주말 changes>=1 write, 평일 changes==0 skip_error exit1 (weekday_no_changes), 주말 changes==0 skip_ok exit0 (weekend_no_changes) / nullable contract_code/basis_date/published_at 모두 None / Codex 8 round 수렴 + write-path Blocker 2 (B1 range atomicity 부분 commit 차단 — skip_error 시 transaction 전 abort + B2 DRY_RUN_CHECKS transaction 전 적용 — 음수/precision raw reject) + N1 metadata fail-closed + N2 dry-run 이중 query 정리 / 영구 test 16 (process_date 6 / write_transaction 4 incl official overlap rollback 안전망 + metadata fail-closed / `_run_write` 3 / production guard 3) + /tmp smoke 11 case 58 check / **overlap 안전망 실측** — official row overlap 시 nullable leftover → post-write fail → rollback → official 보존 (ADR-034 §10 Open 전까지 자동 차단) / First PR scope: USD only / 2026-05-28 단일 row (official 4 rows [5/21,22,26,27]와 overlap 0) / **Phase 1-3 production read-only query** (EC2 docker compose run): change_rows=1096 + prev age 254.6s fresh + existing source_daily_rates row=0 / 예상 row close=1496.1 high=1510.8 low=1494.3 rollup_point_count=1097 / Stage 1 commit 대상, Stage 2 push + Stage 3 production execution 별 GO — Stage 3 선행 EC2 git pull + docker compose build fastapi 필수 (one-shot 이미지 신규 writer 반영, scripts/ image COPY, live container recreate 불필요))
- 2026-05-31: ADR-034 Phase 2d Step 3 Hana observed_eod first PR Round 1 Stage 2+3 production execution 완료 (Stage 1 commit `e7213bf` + Stage 2 origin/master push `a533ba9..e7213bf` + Stage 3 production RDS execution 모두 land / 절차: EC2 git pull e7213bf → docker compose build fastapi (image 863c75e9, one-shot 이미지에 신규 writer bake, live container recreate 불필요) → production data dry-run (row + 7 validation 확인, write X) → `--write --start-date 2026-05-28 --end-date 2026-05-28 --allow-production-write` → 이중 독립 verify (write 직후 + 별 read-only query) / manual snapshot 생략 — incremental single row + `date_kst.in_([date(2026,5,28)])` delete rollback anchor / **production verify: Hana usd-krw 2026-05-28 1 row committed + post-write validations passed + drift 0 + overlap 0** (close=rate=1496.1 / high=1510.8 / low=1494.3 / baseline_included=True / day_change_count=1096 / rollup_point_count=1097 / contract_code·basis_date·published_at 모두 None / close_basis hana_observed_eod / source_method+ohlc_quality observed_rollup) / hana/usd-krw = 5 (official_historical_backfill 4 `[5/21,22,26,27]` + observed_eod 1 `[5/28]`, corruption 0) / **전체 production source_daily_rates state: KRX 25 + Hana 5 + Bithumb 10 = total 40 rows** (Bithumb 8→10: daily append cron `1 15 * * *` 2026-05-29·05-30 row 정상 append 확인 — cron liveness 입증) / 운영 fastapi runtime 미교체 — Phase 2e endpoint switch 시점 별 배포 영역 / source_daily_rates read 호출자 0 — user-facing 영향 0 / 후속: Hana JPY/EUR 다각화 → orchestrator --source 확장 + Hana daily append cron → holiday calendar + heartbeat → Step 4 full backfill → Step 6 v2 endpoint switch)
- 2026-05-31: ADR-034 Phase 2d PR A1 Stage 1 candidate — Hana daily append 자동화 foundation (calendar + JSON verdict 계약) (app/calendars/{__init__,kr_holidays,hana_business_days}.py 신규 — holidays.SouthKorea(categories=(PUBLIC,BANK), observed=True) + classify_hana_calendar_day 단일 진실 소스 weekend>holiday>business_day / app/daily_append_verdict.py 신규 — verdict 계약 공유 소스 / Hana writer calendar 통합 (holiday_no_changes→skip_ok / business_day_no_changes→skip_error) + calendar_class 3-way + --emit-daily-append-verdict / Bithumb writer --emit-daily-append-verdict (success 경로 sentinel) / orchestrator regex→JSON verdict fail-closed + source-aware policy (bithumb written-only/usdt-krw, hana written|skipped/usd-krw) / requirements.txt holidays>=0.97,<1.0.0 + lock holidays==0.97 (dateutil 기존) / **observed=True load-bearing 실측** (observed=False면 5/25 대체공휴일·3/2 삼일절 대체 drop) + PUBLIC∪BANK(근로자의날 5/1 BANK 전용) + 12/31 한국 공휴일 아님(사용자 정정) / **Codex adversarial Blocker 4 + NB 재현 확인 후 fix** — B1 non-object JSON crash→FAIL(isinstance dict) / B2 asset 미검증→source별 asset 검증 / B3 Bithumb skipped 허용→source-aware allowed_statuses / B4 bool·str rows→type(rows) is int / NB written→reason None / Local 59 PASS (calendar 9 + Hana writer 22 + orchestrator 28 incl 재현 6 + CLI guard subprocess 4) + Bithumb end-to-end PASS / cron line 변경 0 (orchestrator만 JSON 전환) / Stage 1 commit 대상, Stage 2 push + Stage 3 (EC2 build + in-image holidays==0.97/fixture 확인 + Bithumb manual smoke) 별 GO / PR A2: --source all + per-source 격리 + cron line 교체)
- 2026-05-31: ADR-034 Phase 2d PR A1 Stage 2+3 production deploy 완료 (Stage 1 commit `a7b128d` + Stage 2 origin/master push `3365c1b..a7b128d` + Stage 3 EC2 배포 모두 land / 절차: EC2 git pull a7b128d → docker compose build fastapi (새 이미지, holidays 패키지 bake) → in-image 검증 (holidays.__version__==0.97 + 대체공휴일 fixture 5/25·3/2 True / 5/1 근로자의날 True / 12/31 False / classify 정상) → Bithumb JSON manual smoke (`{written,asset:usdt-krw,rows:1}` → orchestrator PASS, idempotent 5/30) → read-only verify (hana 5 + bithumb 10 + krx 25 = 40 불변, Bithumb 5/30 close=1481 무결) / **cron line 변경 0** → live exchange-rate-app 컨테이너 recreate 없이 one-shot 이미지만 rebuild (broadcast 영향 0) / Codex 독립 재검증 일치 (EC2 a7b128d / 새 이미지 f7bab620 / in-image holidays + fixture / 40 rows / crontab 기존 line 유지) / **첫 JSON cron 발화 2026-06-01 00:01 KST (EC2 timezone Etc/UTC 실측, cron 1 15 * * * = 15:01 UTC) — 검증 PASS**: 신규 insert Bithumb 5/31 row JSON verdict 경로 적재 (`{written,rows:1}` → orchestrator PASS, total 40→41, candle_ts_kst=2026-05-31T00:00+09:00) — **A1 운영 검증 완전 closure** / 후속: PR A2 (--source all + per-source 격리 + cron line 교체))
- 2026-06-01: ADR-034 Bithumb provenance Amendment Stage 1 candidate — source_method rename `bithumb_candlestick_backfill` → `bithumb_candlestick_api` (PR A1 cron 검증 중 발견 — ADR §7/§9는 Bithumb daily append=observed_rollup(DB rollup) 규정이나 구현은 candlestick backfill writer 재사용 → backfill·append 모두 공식 24h candle API / 데이터 정확, label만 부정확 / 결정 옵션 a: candlestick canonical 인정 + source_method 단일 값 bithumb_candlestick_api (방법=획득방식, backfill/daily는 timing) + ohlc_quality=source_ohlc·close_basis=bithumb_24h_kst_close 유지 + ingest_mode 미도입(YAGNI) / 변경: Bithumb writer SOURCE_METHOD 상수(build+validator 공유) + docstring + models.py docstring + DECISIONS §6/§7/§9 + GRAPH §6/§7 + CLAUDE amend (과거 history rewrite ❌, Amendment supersede) + migrate_bithumb_source_method.py 신규 + 회귀 test 13 (fail-open count 방어 3 포함) / migration 계약: FOR UPDATE lock → count 재확인(--expected-old N --expected-new M) → snapshot → source_method만 UPDATE → all-cols surgical 불변 검증 → post-verify(old_after=0/new_after=M+N/total 불변) → idempotent(old=0 AND new=M+N skip / 아니면 stale abort) / 배포 순서: 신코드 배포 후(cron이 new 기록) → cron 15:01 UTC 회피 + one-shot 미실행 확인 → pre-query → migration → post-query → 다음 cron / 가격·OHLC·metadata·captured_at 변경 0, source_method 문자열만 UPDATE / Local test 13 PASS + Bithumb writer rename(literal 0) / non-urgent — A2 전 provenance 정리로 Hana/Bithumb 수집방식 분리 명확 / Stage 1 commit 대상, Stage 2 push + Stage 3 production migration 별 GO)
- 2026-06-01: ADR-034 Bithumb provenance Amendment Stage 2+3 production migration 완료 (Stage 2 push 6779213 + Stage 3 production migration 모두 land / EC2 git pull a7b128d→6779213 + docker compose build fastapi [image sha256:0a94ac1a0e15 — cron one-shot 이미지에 새 SOURCE_METHOD 반영] / write 직전 dry-run pre-query N=11 M=0 → migrate --write --expected-old-count 11 --expected-new-count 0 --allow-production-write / production verify: rename 11 rows old→new + post-query old 0/new 11/total 11 불변 + source_method 외 전 컬럼 in-transaction surgical 불변 검증 통과 (commit 자체가 11행 before/after 일치 증거) / SSH read-only 독립 전수 (Codex): rate_close_drift 0 + bad OHLC(low≤close≤high) 0 + bad nullable 0 + missing candle_ts_kst 0 + image writer SOURCE_METHOD=bithumb_candlestick_api = committed after-state 확인 / 전체 production source_daily_rates: Bithumb 11 + Hana 5 + KRX 25 = 41 rows / 운영 fastapi recreate 안 함 — 운영 in-process source_daily_rates write 호출자 0 (app/main.py·scheduler.py 미접근, scripts one-shot만 write) / **다음 cron 신규 append api 적재 검증 완료 (2026-06-01 15:01 UTC = 6/2 00:01 KST 발화 — Bithumb 6/1 row close=1468 source_method=bithumb_candlestick_api; A2 --source all 통합 발화로 함께 관찰)** / Rollback anchor: git revert 6779213 → docker compose build fastapi → guarded reverse SQL api→backfill)
- 2026-06-01: ADR-034 Phase 2d PR A2 Stage 1 candidate — daily append orchestrator `--source all` (bithumb + hana) + per-source 격리 (scripts/daily_append_source_daily_rates.py 확장 — 신규 write logic 0, 기존 writer subprocess 재사용 / `SUPPORTED_SOURCES`=["bithumb","hana","all"] + `SOURCE_RUN_ALL`=("bithumb","hana") tuple 순서 고정 + `resolve_sources()` helper / `build_hana_command` 신규 — observed_eod writer CLI quirk 반영 (write→--start-date/--end-date+--emit-daily-append-verdict, dry-run→--date) + `build_command` dispatch에 hana / `execute_source` helper (run_source+evaluate 묶음) / **main loop per-source `except Exception` 격리** — 한 source 예외가 다른 source 막지 않음, synthetic FAIL(detail에 `type(e).__name__`) 기록 후 continue, BaseException 금지(KeyboardInterrupt/SystemExit 전파) / `print_dry_run_preview` source-aware 문구 (bithumb candle fetch vs hana 내부 DB read) / **CLI default "bithumb" 유지** (후방 호환 — 기존 cron은 --source 생략) / SOURCE_POLICY·evaluate_source_result는 A1에서 이미 hana 지원 (변경 0) / **per-source 독립 명시** — cross-source transaction 아님, Bithumb commit 후 Hana 실패 시 Bithumb row 유지+exit 1(의도된 정책), re-run idempotent upsert / Codex 4 round 수렴 — 필수 보완(격리 경계=source 단위 전체) + 정밀화 3(cron history 5184 덮어쓰기 금지·CLI default·dry-run 문구) + 구현 구조(resolve_sources/execute_source helper) + Minor(검증 지점 "소멸" 과장→변수 격리) + smoke 날짜 2026-05-28(idempotent) / **result shape 검증** (`_REQUIRED_RESULT_KEYS` — execute_source가 non-dict/키 누락 반환 시 try 내 synthetic FAIL 변환 → post-try record/print/summary indexing 안전, Codex 재현 None·필수키 누락 닫음) / test 14 추가 (resolve_sources 3 + build_hana 3 + main loop 격리 6 incl synthetic FAIL type name·Bithumb 성공+Hana 실패 보존·malformed result[None/필수키 누락]→synthetic FAIL + all dry-run preview + default bithumb only) → orchestrator 42 PASS / **DECISIONS 5184/5192 정정** — `1 0`→`1 15`(EC2 UTC=KST 00:01)+/usr/bin/docker 실배포값(Step 5 first PR Bithumb default 보존) + A2 `--source all` cron 별도 anchor 추가 / 범위 밖: KRX close finalizer 통합·Hana JPY·EUR·retry/backoff/alert / Stage 1 commit 대상, Stage 2 push 별 GO / **Stage 3 cron 교체는 고정 날짜(2026-05-28) idempotent pre-smoke 후 별 GO** — 통합 관찰 정책(6/2 00:01 KST 자연 발화에서 Bithumb amendment + A2 source별 통합 관찰; Bithumb 명령 A2 무변경이라 amendment 검증 보존 + 변수 격리는 사후 source_method query로 대체. 6/2 발화 전 deploy+교체 못 들어가면 conservative 회귀 — 무해))
- 2026-06-01: ADR-034 Phase 2d PR A2 Stage 2+3 production deploy(2026-06-01) + 통합 관찰(2026-06-02 00:01 KST) 완료 (Stage 2 push `c171974` + Stage 3 EC2 배포 모두 land / 절차: EC2 git pull c171974 → docker compose build fastapi [image 40f3b91560b9, one-shot 이미지에 A2 코드 — in-image SOURCE_RUN_ALL=('bithumb','hana') 확인] → --source all dry-run preview (bithumb candle / hana --date 정확) → **pre-smoke (--source all --date 2026-05-28 idempotent) PASS** (bithumb PASS + hana PASS + aggregate PASS, total 41 무변, 5/28 row 무결) → crontab 기존 Bithumb-default 한 줄을 --source all 한 줄로 *교체* (daily_append line 1개, 중복 0, monday_reopen 보존) / **6/2 통합 관찰 완료** (2026-06-01 15:01 UTC = 6/2 00:01 KST 자연 발화): Bithumb 6/1 close=1468 source_method=bithumb_candlestick_api + Hana 6/1 close=1512.8 observed_rollup + 로그 source별 PASS×2 + aggregate PASS + **total 41→43** (bithumb 12 + hana 6 + krx 25) / **운영 실증 = 두 source 독립 실행·commit (성공 경로)**; 실패 격리(Hana 실패 시 Bithumb 보존)는 영구 test로 검증된 속성 — 이번 발화는 실패 0이라 운영 미발동 / Codex SSH read-only 독립 재검증 일치 (image-internal SOURCE_RUN_ALL + crontab 단일 + total 43 + 6/1 두 row 무결) / **Bithumb amendment + A2 두 pending 동시 closure** / Rollback anchor (cron): `crontab -l | sed 's/ --source all//' | crontab -` (코드 default bithumb 유지라 cron만 복귀) / 후속: Hana JPY/EUR 다각화 → Step 4 full backfill → Phase 2e v2 endpoint switch / KRX close finalizer 통합 별 PR)
- 2026-06-02: ADR-034 Phase 2d Step 4A-0 gap repair 완료 (production, 코드 변경 0 — 기존 observed_eod writer 실행) — A2 cron 교체 전까지 Hana daily 미포함이라 누락된 2026-05-29(Fri)·05-30(Sat) observed_eod 적재. write 직전 read-only gate (5/29·5/30·5/31 부재 + raw 1048/416/0 확인) → `--write --start-date 2026-05-29 --end-date 2026-05-30 --allow-production-write` (atomic 2 rows) → post-verify: total 43→45 / Hana 6→8 / 5/29·30 close_basis=hana_observed_eod·source_method=observed_rollup·nullable None / drift 0 / 5/31(Sun raw 0) 부재 유지 / 기존 official 4 rows [5/21,22,26,27] 불변 (write range 비중첩 구조 보증). rollback anchor: `date_kst.in_([date(2026,5,29), date(2026,5,30)])`.
- 2026-06-02: ADR-034 Phase 2d Step 4A Stage 1 candidate — Hana official writer 안전 보강 (Step 4 → 4A[기존 writer 즉시: Bithumb 1년 + Hana official 1년] / 4B[KRX 12-month contract-chain 구현] 분리). 변경: scripts/backfill_hana_source_daily_rates.py + app/source_daily_rates.py / **overlap guard** (write_with_transaction_hana pre-write — expected_dates IN + FOR UPDATE(pg) + `close_basis != hana_official_historical_backfill` reject → official이 observed_eod canonical 안 덮음, 관측 안전망 대칭) / **conditional conflict update** (공용 upsert `update_only_if_existing_close_basis` optional param, 기본 None 후방 호환 — ON CONFLICT DO UPDATE WHERE close_basis=official → absent-key concurrent observed insert도 update skip → post-validation rollback; PG·SQLite compile 확인) / **HanaWriteOutcome** NamedTuple (success/issues/inserted_dates/updated_dates — rollback inserted-only, official one-shot 단독 전제) / **`--require-empty-target`** (기존 row 있으면 reject → gap-only 강제) / **`--range-dry-run`** DB-free gate (validate_write_range + empty fail-close + `_ROW_DRY_RUN_CHECKS`) / **`_validate_cli_combo`** 한 곳 fail-close (--write+--date / --range-dry-run+--require-empty-target / mode 없는 start·end / require-empty·allow-production 단독) / **validate_write_range 미래 date reject** (--include-today로도 — backfill 과거만) / **write-path pre-write `_ROW_DRY_RUN_CHECKS`** (음수 rate/precision/published_at KST 값 validator를 transaction 전 적용, atomic abort — dry-run 생략한 직접 --write도 self fail-close). **runbook (gap-only monthly batch)**: --range-dry-run → 출력 dedup expected_dates(휴일 fallback로 request range ≠ 실제 date_kst) 확정 → 그 expected_dates DB read-only target 0 확인 → official one-shot 단독 `--write --require-empty-target --allow-production-write` → post-verify + inserted-only rollback. Codex 6 round (race / reverse / future / CLI combo / write-path bypass 엣지 전수). 신규 test 32 (tests/test_hana_official_overlap_guard.py) + 관련 회귀 77 = **109 passed**. Stage 1 commit 대상, push + 1년 backfill 실행 별도 GO [⚠️ monthly batch 표현은 single full-range로 **superseded** — 월 경계 휴일 fallback 충돌, 2026-06-02 Step 4A Bithumb anchor + Hana 보강 PR 참조]. 경계 전환 정책(§10 Open, Hana observed_eod first PR 후속 작업 anchor)은 미결정 — 결정 전까지 overlap fail-close.
- 2026-06-02: ADR-034 Phase 2d Step 4A Bithumb 1년 backfill 완료 (Stage 3 production) — Step 4 full backfill의 **4A Bithumb 부분**. USDT/KRW 1년 gap-only **single full-range write** (Bithumb은 단일 API fetch라 monthly 분할 자체가 불필요). 절차: RDS manual snapshot `fxi-pre-step4a-bithumb-2026-06-02` (`available` 확인) → write 직전 candidate `[2025-06-02, 2026-05-20]` overlap COUNT=0 **재확인** (point-in-time gate, 직전 참고값 아닌 write 시점 gate) → `--write --start-date 2025-06-02 --end-date 2026-05-20 --allow-production-write` (`</dev/null` stdin 차단 + `> log 2>&1; rc=$?` exit code 보존) → 즉시 post-verify. **stdin 함정 1건 발생·해소**: 원격 `bash -s` heredoc 안 파일 기반 `docker compose run -T`가 뒤쪽 shell 명령을 stdin 소비 → write 미실행 (exit-code/log 파일 부재 + DB total 12 유지로 감지) → 파일 명령에 `</dev/null` 추가 재실행, 중복/부분 적재 0. (예외: `python - <<'PY'` inline 코드 전달엔 `</dev/null` 금지 — 코드 본문이 stdin. `python -c`/파일 script에만 적용. write 경로 파이프 회피, 파이프 필요 시만 `PIPESTATUS[0]`. 향후 bulk 감사 로그는 `/tmp` 아닌 `~/logs/`에 보존.) 결과: 신규 **353 rows** → Bithumb 12→**365** (연속 `[2025-06-02, 2026-06-01]`), 전체 source_daily_rates **398** (Bithumb 365 + Hana 8 + KRX 25). **이중 독립 검증** (Claude post-verify + Codex production read-only): date gaps 0 / drift(rate≠close) 0 / OHLC 위반 0 / nullable metadata 0 / candle_ts_kst 누락 0 / close_basis=bithumb_24h_kst_close ×365 / source_method=**bithumb_candlestick_api** ×365 (provenance amendment 반영). **rollback shelf-life**: write+post-verify 종료로 작업 창 닫힘 → `delete_range`는 **상시 rollback 명령 아님**. 작업 중 즉시 rollback = candidate range `delete_range` / **작업 종료 후 = snapshot 기반 복구 또는 교집합 historical/manual write 부재 재검증 후에만**. snapshot = 삭제 전까지 유지되는 **durable recovery anchor** (restore = 별도 인스턴스 생성, "영구 복구" 아님). **rollback 정책 정확화** (과거 Step 3 Bithumb anchor 2026-05-29의 "24/7 연속이라 range delete 안전" 표현 supersede): 24/7 연속성은 **내부 휴일 gap 부재**일 뿐, range delete 안전은 **별개** — gap-only write 직전 확인 + 교집합 historical/manual write 부재 + 동일 작업 창 내 즉시 rollback일 때만 허용. **Hana official 1년 backfill — monthly runbook superseded**: Step 4A Stage 1의 monthly batch runbook(CLAUDE 1305 + DECISIONS 5541 "gap-only monthly batch")은 **single full-range로 폐기·대체** (월 경계 휴일 fallback 충돌). 현재 코드 EXPECTED_DATES_JSON 미출력 → **보강 PR(3.0초 throttle[분당 30회 보수 + live crawler 같은 endpoint 분당 최대 4회 공유, backfill 21+live 4=rolling 25] + 첫 요청 cooldown 코드 강제 + HTTP 429 즉시 abort + EXPECTED_DATES_JSON 출력) 전 Hana 1년 write 금지**. 보강 후 주말 OUT 모드 throttled single full-range dry-run → single write 예정 (pending — Step 4A Hana 부분). commit 분리: ① docs-only completion anchor / ② Bithumb writer line 888 출력 문구 정정 / ③ Hana throttle·429·JSON 보강 PR.
- 2026-06-02: ADR-034 Phase 2d Step 4A Hana 보강 PR (commit ③) — Hana official writer throttle + 429 abort + EXPECTED_DATES_JSON (single full-range backfill 안전 전제). 변경: scripts/backfill_hana_source_daily_rates.py + tests/test_hana_backfill_throttle.py(신규) + tests/test_hana_official_overlap_guard.py(throttle sleep mock 회귀). **throttle**: `THROTTLE_INTERVAL_SECONDS=3.0`, `time.monotonic()` 기준 loop 상단 — 첫 요청 cooldown(함수 시작 시각 초기화 → 첫 fetch도 interval 대기, dry-run→write 프로세스 경계 보호) + 실패 요청 포함(retry storm 방지). 부하: 분당 30회 보수 권장(GRAPH §14 Open) + live Hana crawler가 같은 endpoint(wpfxd651_01i_01.do, hana.py 1차 우선) 분당 최대 4회(rolling) 공유 → backfill floor(60/3)+1=21 + live 4 = 25 < 30. **429**: `HanaRateLimitAbort` — `HTTPError.response.status_code==429` 즉시 abort(Retry-After 보존, exit nonzero, 남은 fetch 0) / 429 외 HTTPError는 일반 fetch 실패 continue(최종 fail-close). **EXPECTED_DATES_JSON**: `expected_dates_json()` helper sorted·unique machine-parse(runbook DB pre-query — expected date 전수 audit), dry-run + write 양쪽 출력. **test**: 신규 7 (throttle fake-clock 3[첫 요청 cooldown·간격 / interval 충족 시 no sleep / 예외 후 throttle 유지] / 429 short-circuit 2[즉시 abort fetch 1회 / 404 continue] / JSON 2[sorted·unique·parse / empty]) + **CLI fail-close 2**[--range-dry-run 429 → exit 1·이후 fetch 0 / --write 429 → exit 1·write_with_transaction_hana 미호출] + 기존 `test_real_fallback_dedup_path`에 sleep mock(throttle 실 sleep 9s→0) + EXPECTED_DATES_JSON= 실출력 parse → 전체 Hana **63 passed**. monthly runbook(1305/5541)은 single full-range로 최종 폐기(월 경계 휴일 fallback 충돌). Stage 1 commit 완료. Stage 2 push와 Hana 1년 single full-range write(주말 OUT throttled dry-run→snapshot→write)는 각각 별도 GO (push는 production write 아님). **운영 게이트**: bulk 실행 중 다른 official historical CLI 동시 실행 금지 + 수동 Hana endpoint probe 금지 (rolling budget 25 보존 — 마진은 비상 완충, 의도적 소비 금지).
- 2026-06-03: ADR-034 Phase 2d Step 4A Hana 1년 write 완료 (Stage 3 production) + **Step 4A 전체 완료** — Hana official_historical_backfill 1년 gap-only single full-range write production 적재. **EC2 배포 선행** (push≠deploy): git pull 24c8694→930e667 + `docker compose build fastapi` → image `6b6a5f`(THROTTLE_INTERVAL_SECONDS=3.0/HanaRateLimitAbort/expected_dates_json marker 3개) + 운영 container `ab4178cca332` 불변 + cron `--source all` 1줄 + rollback retention tag(`rollback-pre-hana-hardening-221a1a1b5c36` / `hana-hardening-930e667-6b6a5f882fae`, latest 불변). **절차**: BREAK1에서 full-range `--range-dry-run` nohup(`~/logs/` log+exitcode+pid + launch 확인) → **4/4 통과**(exit 0 / fetch_errors 0 / 353 calendar days → 116 fallback dedup → EXPECTED_DATES_JSON 237 / DB target count 0) → RDS snapshot `fxi-pre-step4a-hana-2026-06-03` available → write 직전 DB target count 0 **재확인**(point-in-time preflight, 최종 load-bearing은 transaction 내부 `--require-empty-target` 재검증) → nohup `--write --currency USD --require-empty-target --allow-production-write` → 즉시 post-verify. **결과**: 신규 **237 official rows**(inserted 237 / updated 0, log `237 rows committed + post-write validations passed`) → Hana 9→**246**, 전체 source_daily_rates 400→**637**(KRX 25 + Hana 246 + Bithumb 366). **이중 독립 검증**(Claude post-verify + Codex production read-only): dry-run manifest 237 `date_kst.in_()` **1:1**(missing 0 / extra 0) / drift(rate≠close) 0 / official metadata 위반 0(close_only + basis_date NOT NULL + published_at NOT NULL + pbldSqn, 샘플 2025-06-02 pbldSqn=1339) / **observed_eod 5(5/28·29·30·6/1·6/2) = 개수·날짜·분류 유지**(overlap guard 결과 — official이 observed_eod canonical 안 덮음. "전 컬럼 불변"은 before/after snapshot 미비교라 단정 회피). **rollback shelf-life**: snapshot `fxi-pre-step4a-hana-2026-06-03` = 명시 삭제 전까지 유지되는 **DB 전체 recovery anchor**(restore = 별도 DB 인스턴스 생성, 실제 engine compatibility 복구 시 확인) / inserted_dates 237 IN delete는 write+post-verify 종료로 작업 창 닫힘 → **상시 rollback 명령 아님**(이후 snapshot 복구 또는 교집합 historical write 부재 재검증 전제). **post-snapshot** `fxi-post-step4a-2026-06-03` `available`(**Step 4A post-verify 후 생성** — snapshot 2026-06-03 03:20 KST, DB 전체 recovery anchor. post-verify ~01:39 KST와 ~1.6h 시차. 637은 별도 post-verify 결과이며 snapshot 직접 조회로 재확인한 값은 아님). **배포 image** `6b6a5f`(commit 930e667 포함) + 6/3 00:01 KST daily append cron PASS(bithumb·hana written, 새 image 첫 운영 검증). **Step 4A 1년 coverage production 완료** — 2026-06-03 ~01:39 KST post-verify state: KRX 25 + Bithumb **366**(1년 coverage 365 + 2026-06-03 00:01 KST cron이 2026-06-02 row 1건 append) + Hana **246**(official 241[기존 4 + 신규 237] + observed_eod 5) = **637**. **engine**: PostgreSQL **17.9**(2026-06-06 DB 쿼리 재확인 `server_version_info=(17,9)`, ARM64 aarch64; 17.6→17.9 원인은 AWS auto minor upgrade 추정, 무영향). **cron 정상 확인**: daily append cron이 6/6까지 매일 적재(2026-06-04~ 관찰 해소 — total 2208 / 최신 date hana·investing·bithumb 6/6, krx 6/5[주말 close finalizer 없음]). **manual snapshot cleanup 완료**(2026-06-06 사용자 — `fxi-pre-investing-backfill-2026-06-05` 1개만 유지, step3/step4a-bithumb/step4a-hana/post-step4a/krx-source-method/krx-fullyear/hana-jpyeur-official 7개 삭제. backfill 완료·이중검증으로 복구 창 종료). dangling image 0 확인 완료 (2026-06-06 — 40f3 등 이미 제거됨). **후속(별도 GO)**: stale 로그 문구 정정 PR(observed_eod writer line 783 + daily_append line 434 "cron 활성화 가능/별 GO" — 이미 활성 cron) / Step 4B KRX 12개월 contract-chain(현재 KRX 25 rows, Tether 3m/1y critical path) / Hana JPY·EUR 다각화 / Phase 2e endpoint switch.
- 2026-06-05: ADR-034 Phase 2d KRX daily-append (in-process close finalizer) **Units 1-4c land + 6/5 canary 성공** (production end-to-end, Claude+Codex 이중 독립) — §9 KRX daily append 구현. **cron 아닌 in-process close finalizer hook** (Bithumb/Hana cron one-shot과 다른 메커니즘: CF 15:45 KST 종가 직후 trigger — 같은-날 freshness + 종가 캡처에 묶여 "단순 cron 부적절"). **6 units** (`b7bec28`..`f04d9b8`): U1 `get_krx_cf_session_rollup`(CF session 08:30~15:45 KST source_rates rollup → high/low/point_count) / U2 `build_krx_cf_append_row`(point_count==0 → close_only[high=low=close] / else observed_rollup + close clamp[high=max(rollup_high,close)·low=min(rollup_low,close)] + clamp diagnostics) / U3 `decide_krx_cf_append_action`+`write_krx_cf_append_row`(INSERT/SKIP/HARD pure guard — non-KRX·non-finalizer / key mismatch / method allowlist[krx_openapi_daily·close_finalizer] 외 / close mismatch → HARD, close match → SKIP) / U4a `append_krx_cf_daily_row`(rollup→build→guard→INSERT commit / SKIP·HARD rollback) / U4b CF finalizer hook(`KrxCloseWindowWriter._sync_write` success tail session=="CF" → append, **exception 격리 — finalizer 영향 0**) / U4c `KRX_DAILY_APPEND_ENABLED` gate(default false → **배포 ≠ 동작 변화**, 활성화 = env 토글 + 재생성). 변경: `app/source_daily_rates.py` + `app/crawlers/krx_kis.py` + `app/config.py`. 테스트 U1 7 / U2 10 / U3 14 / U4a 8 / U4b·4c close-window +5 회귀 통과. **6/4 manual fill**: gate 활성 전 6/4 CF 종가가 source_rates에 있어 1회성 `append_krx_cf_daily_row(date(2026,6,4), 1530.6, "A75606")` → observed_rollup H=1531.0/L=1522.2/C=1530.6, KRX 243→244 (snapshot 생략 — 1-row delete-anchor, Codex 이중 검증). **6/5 canary**: `.env KRX_DAILY_APPEND_ENABLED=true` + `docker compose up -d --force-recreate fastapi`(**build 없음, image f86195e 불변**) 15:35 KST 활성화 → config 3 flags(DAILY_APPEND·CLOSE_FINALIZER·FUTURES) True + WS fresh(tick age 15.7s) → **15:46:00.76 KST close finalizer 발화** `[krx_cf_append] INSERT date=2026-06-05 contract=A75606 close=1538.9 — 신규 date`. **이중 독립 검증**(Claude post-verify + Codex production read-only): close_finalizer / observed_rollup / **cf_session_point_count 7005 == source_rates CF session[08:30~15:45 KST] tick count 7005** / rate=close=1538.9 / H=1549.0 / L=1531.8(intraday 범위 보존) / close_basis=krx_cf_close_1545 / contract A75606 / basis_date·published_at None / drift(rate≠close)·OHLC·duplicate 0 / **KRX 244→245**. provenance 자연 전환(6/5·6/4 close_finalizer·observed_rollup / 6/2·6/1 krx_openapi_daily·source_ohlc). **첫 라이브 in-process 종가 샘플링** — Units 1-4c end-to-end production 검증. `KRX_DAILY_APPEND_ENABLED=true` 유지(rollback 불필요), 매 CF close going-forward 자동. **env-gate rollback anchor (향후 자동 append 비활성화)**: `cp .env.bak-canary-2026-06-05 .env && docker compose up -d --force-recreate fastapi`(env import-time, KRX 수집/broadcast/finalizer Redis write 영향 0 — daily-append 분기만 미진입). **이미 insert된 6/5 row 삭제는 env-gate가 되돌리지 않음 — 별도 data rollback 판단** (`delete_range("krx", "usd-krw-futures", date(2026,6,5), date(2026,6,5))`, canary 성공이라 미적용). 후속: Hana JPY/EUR 다각화 / Phase 2e endpoint switch.
- 2026-06-05: ADR-034 Phase 2d Hana JPY/EUR 다각화 완료 (통화 파라미터화 + official 1년 backfill + observed_eod 보존 + 3통화 정합, production end-to-end, Claude+Codex 이중 독립) — Hana USD 단독 → USD/JPY/EUR 3통화 확장 (4단계). **(1) 코드** (`cb62208`): observed_eod writer `ASSET` USD 하드코딩 제거 → `--currency {usd-krw,jpy-krw,eur-krw}` 파라미터화 (fetch/build/process_date/verdict/run thread + write는 rows에서 asset 도출) + write persister `SUPPORTED_CURRENCIES` allowlist guard (구 상수 방어 복원) + orchestrator run unit=(source,asset) `resolve_run_units` hana 3통화 per-currency subprocess 격리 + label=source:asset (Codex open Q "writer 내부 3통화 vs orchestrator 3호출" → orchestrator per-currency subprocess 채택, 기존 per-source 격리 패턴 일관). writer 29 + orchestrator 47 = 76 tests (USD default 경로 회귀 포함). **(2) official 1년 backfill** (2025-06-02~5/27, snapshot `fxi-pre-hana-jpyeur-official-2026-06-05` user 생성): JPY/EUR 각 `--range-dry-run`(360 calendar → 241 business / fetch errors 0 / EXPECTED_DATES 2026-05-27까지) → `--write --require-empty-target --allow-production-write` (inserted 241 / updated 0) → 독립 verify (각 241 / external_backfill·close_only / high==low==close / drift·OHLC·dup 0 / basis_date·published_at·pbldSqn NOT NULL / contract_code None / USD·JPY·EUR 2025-06-02 pbldSqn=1339 동일 — Hana 통화별 같은 회차 발표). Hana 248→730. **(3) 배포** (`a2d066b`, build-only): EC2 `git pull`(f04d9b8→a2d066b, fast-forward, tracked clean[untracked .env.bak-* 무영향]) + `docker compose build fastapi` (image f86195e→**c77b5a0**, **`--force-recreate` 안 함**) — f04d9b8..a2d066b는 scripts/tests/docs만(app/ 불변)이라 **live KRX daily-append container(f86195e) untouched, healthy 유지**. smoke: 새 image(`docker compose run`, cron이 쓸 경로)에서 observed writer `--currency {usd-krw,jpy-krw,eur-krw}` + orchestrator `--source all` 4 run units(bithumb + hana 3) 반영 확인 (CLAUDE 2026-05-19 Coinone ModuleNotFoundError build-verify 교훈 동형). **(4) observed_eod 보존** (5/28~6/4, 각 7 rows): cb62208 observed writer가 bank_exchange_rates 내부 read (Hana endpoint 무관, budget gate 무관) → dry-run(JPY/EUR 각 7 write — 5/28·29·30·6/1·2·3·4 / 5/31 weekend_no_changes skip / 5/30 토·6/3 선거일 변동有 write[provenance=weekend/holiday]) → `--write --currency jpy-krw/eur-krw --start-date 2026-05-28 --end-date 2026-06-04 --allow-production-write` (각 7 committed) → 독립 verify (각 7 / hana_observed_eod·observed_rollup / **intraday range[H≠L] 7** — bank 30일 retention age-out 전 perishable 캡처 / basis_date·published_at·contract_code 모두 None[provenance split: observed는 official metadata 없음 — OHLC split과 동형] / drift·OHLC·dup 0). Hana 730→744. **결과: 3통화 완전 동일 구조** — usd/jpy/eur 각 248 = external_backfill 241(≤5/27) + observed_rollup 7(5/28~6/4), 전체 source_daily_rates 1357 (KRX 245 + Hana 744 + Bithumb 368). **provenance boundary 정책 (Y안)**: official ≤5/27 + observed 5/28~6/4 + cron 6/5+ going-forward 활성(6/6 00:01 첫 발화 PASS — 6/5 3통화 적재 확인). X안(official ~6/4 단순)이 아닌 Y안 채택 근거 = observed intraday range가 perishable(bank 30일 retention → 5/28~6/4 ticks ~late June age-out)이라 지금 안 잡으면 영영 못 잡음 + USD와 cross-currency boundary 정합. overlap guard 대칭(official ↔ observed 상호 미덮음)으로 X-then-Y 불가 → official end-date 5/27 선결. snapshot: official write는 user 생성 1개로 둘 다 커버 / observed write는 ~14 rows all-insert라 per-currency 비연속 7-date IN delete-anchor (snapshot 생략). 후속: ✅ 6/6 00:01 cron 6/5 JPY/EUR 3 rows 적재 확인 (Hana 744→747, hana_observed_eod / observed_rollup, drift·OHLC 0 — Investing daily append 통합 cron 동반) / Phase 2e endpoint switch.
- 2026-06-05: ADR-035 D1 Investing daily canonical writer + production backfill 완료 (production end-to-end, Claude+Codex 이중 독립) — ADR-035 D1 첫 구현. `investing_exchange_rates`(장기 raw) → `source_daily_rates` daily rollup, 3m/1y hot path 마지막 gap(Investing) 해소. **(1) writer** (`4f38b05`): `scripts/backfill_investing_source_daily_rates.py` 신규 + tests 신규 29 — Hana observed_eod 패턴 차용 + **단순화**(carry-in baseline 제거 — Investing 데이터 풍부 / business_day_error 제거 → skip-empty calendar, Investing gap은 시장 주도). `--range-dry-run`(skip day-of-week 분포 + 일요일 외 skip flagging — EUR sparse surface) / `--require-empty-target`(gap-only) + `InvestingWriteOutcome` inserted/updated(rollback inserted-only) / 0-row fail-close(multi-day range) + `--expected-min-rows` / CLI mode-arg fail-close(start·end·include-today mode 없이 / allow-production-write write 없이 / date range mode와 → reject). close_basis enum 4→5(`investing_observed_eod`) **전수 정합**(Decision B 표+매핑 + §6 본문 enum list + observed_rollup 설명 + GRAPH §6 + models docstring). **Codex 6 findings**(F1 inserted/updated, F2 enum docs, F3 0-row, F4 CLI combo, F5 model docstring, F6 mode-arg) + 4385 framing(GRAPH:200 정합). **(2) quantize** (`a11e6f7`): range-dry-run이 **production write 전** JPY Float artifact 발견(`921.6100000000001` 13자리, JPY/KRW per-100 환산값 → `Decimal(str(float))` 보존 → Numeric(14,6) 초과 185건). build close/high/low `quantize(0.000001)`(max/min raw 비교 후 결과만 quantize → OHLC ordering 보존). usd/eur 무영향(직접 저장 ≤6자리). 147 passed(Investing 29 + 회귀 118). **(3) production backfill** (range `[2025-06-05, 2026-06-04]` 1년, snapshot `fxi-pre-investing-backfill-2026-06-05` user 생성+available): investing canonical=0 사전 read-only 확인 → **명시적 write GO 분리**(snapshot available 확인 ≠ 837 rows mass write 허가 — auto-mode classifier가 별도 GO 요구, 분류기 판단 타당) → 3통화 순차 `--write --currency <cur> --start-date 2025-06-05 --end-date 2026-06-04 --require-empty-target --expected-min-rows 270 --allow-production-write`(각 inserted 279 / updated 0, log `279 rows committed + post-write validations passed`). **결과**: usd/jpy/eur 각 279, Investing 837, 전체 source_daily_rates **1357→2194**(KRX 245 + Hana 744 + Bithumb 368 + Investing 837). **이중 독립 verify**(Claude post-verify + Codex production read-only 일치): drift(rate≠close) 0 / OHLC violation 0 / **duplicate date 0**(Codex 추가 확인 — (source,asset,date_kst) unique index 실증) / nullable(contract_code·basis_date·published_at) violation 0 / metadata point_count missing 0 / enum 정합 / **JPY quantize 6자리**(957.520000 류, 13자리 artifact 0) / date range 2025-06-05~2026-06-04. **calendar**: 365 → 279 write / 86 skip(Sun 52 FX 휴장 + Sat 32 change-only 주말 저활동 + Thu 2 약한 flag US 휴일/크롤러 gap 추정). **recovery anchor**: snapshot `fxi-pre-investing-backfill-2026-06-05`(DB 전체, restore=별도 인스턴스) + 통화별 inserted_dates IN delete(write+post-verify 종료로 상시 명령 아님) + `~/logs/inv_backfill_*.log`. EC2: a11e6f7 pull + `docker compose build fastapi`(writer image 반영, running fastapi recreate 안 함 — standalone script + models docstring만). v2 endpoint(Phase 2e) 미구현 → user-facing 영향 0. 후속(별도): **Investing going-forward daily append**(✅ 완료 — `bef10eb` orchestrator 통합 + cron 첫 발화 검증 2026-06-06, 아래 별도 entry) / Phase 2e v2 endpoint(source_daily_rates read 전환) / source_hourly_rates(1w).
- 2026-06-06: ADR-035 D1 Investing daily append orchestrator 통합 + cron 첫 발화 검증 (production end-to-end, Claude+Codex 이중 독립) — backfill(2026-06-04까지)에서 멈춘 Investing을 매일 자동 갱신, KRX/Hana/Bithumb going-forward freshness parity. **(1) 구현** (`bef10eb`): verdict 계약(`app/daily_append_verdict.py`) `VALID_SKIPPED_REASONS` union에 `no_observation` 추가 + **orchestrator source-aware `valid_skipped_reasons`**(SOURCE_POLICY per-source subset + subset assert — Hana는 no_observation 비허용 / Investing은 weekend_no_changes 비허용 → 전역 추가의 정책 느슨화 회피) + `SOURCE_RUN_ALL`(bithumb→hana 3→investing 3) + resolve_run_units 다통화 일반화(`_MULTI_CURRENCY_SOURCES`) + build_investing_command + evaluate_source_result source-aware reason + Investing writer `--emit-daily-append-verdict`(단일일 written/skipped, **0-obs도 skipped/no_observation sentinel 방출 — exit-0-no-sentinel orchestrator FAIL 방지, Codex catch**; combo: --write 동반 + start==end 전용; multi-day 0-row fail-close 유지). 163 tests(writer verdict 4 + combo 2 + source-aware reason 4[investing no_observation PASS / investing weekend FAIL / hana no_observation FAIL — 정책 느슨화 회피 입증] + build_investing 3 + resolve 3 + 회귀). **(2) EC2 deploy** (build-only): `git pull` + `docker compose build fastapi`(cron이 쓰는 image 반영, running fastapi recreate 안 함 — orchestrator/writer/verdict은 script). dry-run smoke로 `--source investing` 3통화 인식 + command 확인. **(3) cron 첫 발화 검증** (2026-06-06 00:01 KST `--source all`): investing **2026-06-05** 3통화 written(usd C=1553.83 / jpy C=969.49[**quantize 6자리, artifact 0**] / eur C=1795.45, investing_observed_eod / observed_rollup, drift·OHLC·dup 0) + 동반 bithumb 1 + hana 3 → **cron summary 7 units 전부 PASS**. KRX는 cron unit 아니나 in-process close finalizer(CF 15:45)가 same-date(2026-06-05) row 1건 적재. totals **2194→2201**(bithumb 369 / hana 747 / krx 245 / investing 840). **이중 독립 verify**(Claude post-verify + Codex production read-only 일치). sanity cross-check: 같은 6/5 hana usd 1553.30 vs investing usd 1553.83(~0.5 KRW) / hana jpy 969.30 vs investing jpy 969.49 정합(독립 2 source 일치). **→ 4 source going-forward freshness 완성** (bithumb/hana/investing=cron `--source all` 00:01 KST + krx=in-process close finalizer 15:45 — source_daily_rates 매일 자동 최신). 후속(별도): ✅ Phase 2e v2 endpoint(source_daily_rates read 전환) — Phase 2e MVP land + 운영 deploy 완료 2026-06-06 / source_hourly_rates(1w).
- 2026-06-06: ADR-035 Phase 2e MVP land — v2 graph endpoint (catalog + tab, source_daily_rates read, 3m/1y) (로컬 구현+테스트 + 운영 deploy 완료 2026-06-06) — canonical daily layer를 v2 그래프 endpoint가 직접 read하는 hot path 완성. write-side(4 source 적재) 완료 후 read-side 전환. **(1) graph_v2.py 로직** (`7bc1d65`): `app/graph_v2.py` — `build_catalog()`(3m/1y subset, usd/jpy/eur/tether tab × series + DXY 노출 탭만 index axis_group, §3/§9 근거) + `build_tab(db, tab, period)`(SERIES_REGISTRY id↔DB key 분리 + **kind dispatch**: source_daily_rates=get_range+row_to_dict / DXY=market_index_rates.daily). **동적 provenance**(distinct close_basis 1=single/2+=mixed[Hana만]) + per-point(mixed→close_basis/source_method, KRX→contract_code) + **insufficient partial coverage**(첫 row>start+7일, §6 — 빈 series뿐 아니라 신규 자산 미충족). **Codex 2 findings**(F1 insufficient partial / F2 DXY source priority investing>cnbc>else, crud.py `_dxy_query_single` 일치 — last-row dedup 회귀 차단). 12 tests. **(2) main.py thin wiring** (`9d43773`): `GET /api/v2/graph/catalog` + `/api/v2/graph/tab` — tab 404(unknown_tab) + period 400(unsupported_period + v1 fallback hint) + build_tab(asyncio.to_thread 비차단). 로직 0(graph_v2 호출만) / **v1 `/api/graph/{currency}` 변경 0**(legacy 공존 §12). **(3) endpoint harness** (`c7c218c`): `tests/conftest.py`(firebase stub + DATABASE_URL=file-backed sqlite, collection 시작 시 모든 import 전 — main.py firebase_admin+RDS 연결 회피, 재사용 가능, in-memory override + mkstemp fd close [Codex 2건]) + `tests/test_graph_v2_endpoints.py` 6종(catalog 200/unsupported 400/unknown tab 404/tab empty insufficient/v1 무변경, TestClient lifespan 미진입). **검증 18 tests**(graph_v2 12 + endpoint 6). **MVP 범위**: 3m/1y만(source_daily_rates 단일 조회), 1d=v1 realtime / 1w=source_hourly_rates(D3) 후속 → v2 1d/1w는 400 unsupported_period(insufficient_history와 분리 — 데이터는 v1에 있음). **문서=full target(§4) / 구현=MVP subset(3m/1y)** 비강제. **운영 deploy 완료 2026-06-06**(f04d9b8→0e52b7a recreate, image 9e2e3186; Claude+Codex 이중 독립 검증: catalog 200·tab usd 3m 200 series[77,63,79]·1d 400·v1 200·health healthy·KRX status=normal·ERROR 0; client 미연동이라 실사용 caller 0 — 프론트 cutover 별도). 후속(별도): source_hourly_rates(1w, Phase 2e+) / 프론트 client cutover. 상세: CLAUDE.md Phase 2e MVP anchor + [GRAPH_API_V2_CONTRACT.md §13](GRAPH_API_V2_CONTRACT.md).
- 2026-06-11: ADR-034 §9 KRX daily append **6/11 회귀 canary PASS** — ⑥(ADR-035 D3 구 hourly hook 제거, `KrxCloseWindowWriter._sync_write` 수정) 운영 반영 후 첫 CF close. 15:46:00 `[krx_cf_append] INSERT date=2026-06-11 A75606 close=1531.2`, close_finalizer / observed_rollup / krx_cf_close_1545 / cf_session_point_count=5935 / KRX 248→249 / drift·OHLC·dup 0. 의미: hook 제거가 daily finalizer-tail(같은 `_sync_write` success path) 무손상 — unit trip-wire(shared writer False-arm)가 못 잡는 영역의 live 회귀 확증. **WS Case A** finalizer (정상 close frame 경로) — REST gate-checked write(`KRX_CLOSE_REST_WRITE_ENABLED`, 6/15 A75606 rollover 후 활성 판단) 미실증이라 KRX_CLOSE_SNAPSHOT_PLAN 직교. 기록 위치: CLAUDE.md Phase 2d KRX daily-append anchor + 본 changelog 2파일 (KRX_CANARY.md 선택·생략).
- 2026-07-04: ADR-037 Amendment (제품 의미 분리 — 일반 비교알림 absolute-only[left/right 개념 UI 소멸] + 테더 비교=거래소 5끼리 + 김프/역프 알림 신설[테더 전용 signed, 음수 threshold 1급 시민 — 구 크로싱-백 포기 뒤집힘] + 테더 단일알림 krx 제거 + validation 재정의 + A/B 생성 UI 시안 supersede) + ADR-038 작성 (KRX 노출 게이트 — G1 entitlement 운영자 수동 부여[이스터에그/코드입력 기각, Apple 2.3.1] / G2 distribution env / G3 collection 기존 + 별도 topic krx:usd-krw-futures 신설[옵션 B, usdt:krw optional group 제거] + /ws 익명 한계 명시[REST 서버강제/WS 클라 gate]) — 둘 다 Proposed, 구현 미착수
- 2026-07-03: ADR-037 작성 (비교 알림 — within-tab v1[사용자 확정] + universal schema[tab 컬럼 명시: investing/kb/hana usd 테더·달러 겹침으로 유도 모호] + comparison_notification_logs[left_rate/right_rate/spread/is_repeat] + B2 repeat 재사용[구 "1회성 통일" supersede] + dual-trigger 현행 evaluator 재매핑[Decision F superseded] + curated presets v1 + greenfield flag rollout) — Proposed, 구현 미착수
- 2026-06-29: ADR-036 작성 (가격알림 반복 발송 repeat_interval_sec, B2) — Proposed, 구현 미착수. **정책**(5라운드 codex 수렴): NULL=once(0/음수 reject) / interval enum 1m·5m·10m·30m·1h·2h·4h·6h·12h·1d(문서 §B2) / once=triggered+disable(현행), **repeat=triggered 미설정·enabled 유지·gate `last_notified_at+interval`(naive UTC)** / 조건 release 리셋 없음(재크로싱=B3) / OFF·threshold·condition·source·interval 변경 시 last_notified_at 리셋 + 모드전환 PUT 정규화 / dedup key interval 제외 / 발송마다 log row. **scope=FULL B2(bank+source)** but 구현 2 PR 위상(PR1 source→PR2 bank, "B2 완료"는 둘 다 후). 검토: codex LOCK OK + workflow ready-after-adr-edits(5-lens 구현가능성). **load-bearing gotcha 박제**: delivery_allowed 호출부(622/679) aware now vs last_notified_at naive → B2 뺄셈 TypeError → except 삼켜 repeat silent 미발화 → PR1 naive 정규화 필수. last_notified_at/rate 인프라 양 테이블 기존(CLAUDE.md notification_settings 스키마 누락은 PR1 정정). 후속: PR1(source) 착수.

---

## ADR-040: 1C WebSocket 인증 트랙 리셋 — 소비자 없는 설계 81커밋을 폐기하고 최소 수직 슬라이스로 재시작

**날짜**: 2026-07-30
**상태**: Accepted
**보존 앵커**: `archive/adr039-dormant-2026-07-30` (= 폐기 시점 커밋, 코드·테스트·설계 문서 전량 보존)

### 결정

ADR-039 §8.1(1C WebSocket 인증)의 구현 트랙을 **폐기**하고, 그 직전 상태에서 **최소 수직 슬라이스**로
재시작한다. 운영 서버와 호스트 cron은 **건드리지 않는다**.

### 측정된 근거

폐기 구간은 81커밋 / 13,655줄 추가였고 구성은 app 3,533줄(26%) · 테스트 8,945줄(65%) ·
문서 583줄 · 배포 스크립트 594줄이다. 그 app 코드는 신규 모듈 8개로, `git grep` 결과
**클러스터 밖에서 참조하는 줄이 0건**이었다 — `app/main.py`는 그중 하나도 import하지 않았고,
`main.py`·`subscription.py`·`crud.py`·`scheduler.py`·`cache.py`·`latest_rates_cache.py`는
81커밋 동안 **한 줄도 바뀌지 않았다**. 즉 **live 동작 변화가 0**이었고, 그래서 폐기도 운영
위험 없이 가능했다(운영 이미지 롤백 불요).

같은 구간에서 `feat:` 계열 subject는 12건이고, 본문에 "직전 커밋"이 들어간 커밋(= 자기 직전
주장을 정정하는 커밋)이 40건이었다. **사용자 대면 진전은 0이다.**

### 왜 "배선부터 붙여서 검증"하지 않았는가

한때 반대 안을 냈다: 토픽 dispatch flag가 기본 off이므로 배선을 flag-off로 land하면 운영 노출 0으로
E2E를 얻을 수 있고, 삭제하면 검증된 발견을 잃는다는 것이었다. 이 반론은 **두 지점에서 기각됐다**.

1. **사실 오류**: "발견 중 여럿은 리셋해도 남는다"고 주장했으나, 그 근거로 든 두 항목(관측 캐시의
   미등록 주체 fail-open / 연결 정리 호출의 `await` 누락)은 **폐기 대상 모듈에만 존재**했다.
   같은 turn에 내가 직접 "신규 파일" 목록에 출력해 놓고 반대로 단정한 것이다 — 부분 정보를
   전체로 확대한 전형이고, 이 트랙에서 반복 지적받은 유형이다. 남는 것은 셋 중 하나뿐이었다.
2. **결합도 논거**: flag-off는 *운영 노출*만 막는다. 알려진 문제(관측 출처·주체 결속·연결 결속)를
   가진 구조를 진입점에 연결하면 설계 결합도가 올라가 **이후 리셋 비용이 커진다**. 그리고 E2E
   5종은 그 열린 축들을 검증하지 못한다 — 성공 경로가 green이어도 축은 그대로 열려 있다.

수직 슬라이스에 필요한 *능력*(계정 검증 · 구독 검증 · 만료 있는 인가 · ack · 정리)은 그대로
필요하다. 그러나 그 능력을 **지금 구현대로 써야 한다는 뜻은 아니다** — 최소 구현으로 다시 만드는
것이 재시작의 목적이다.

### 보존한 것 하나

`tests/test_suite_hygiene.py`만 남겼다. 이 트랙과 무관한 **리포 전역 가드**이고(`ast`·`pathlib`·
`unittest`만 import), 폐기 3범주(인가 계층 · lease · 배포 스크립트) 어디에도 속하지 않는다.
실제로 리셋 직후 이 가드가 기존 자산 2파일의 결함을 잡았다 — 그 2파일의 main guard를 파일 끝으로
옮겼다(아래 교훈 6).

### 폐기에서 남기는 교훈

#### 1. 호출자가 없는 코드에는 검증의 종료 조건이 없다

리뷰를 더 돌리면 수렴한다고 봤다. 실제로는 매 라운드의 ground truth가 실행이 아니라 다시 내
추론이어서 루프에 끝이 없었다(정정 커밋 40건이 그 형태다). 파생 3종:

- **소비자의 호출 모양은 처음부터 읽을 수 있었다.** `app/topic_dispatcher.py`의
  `handle_client_message`는 `-> None`이라 종단 신호를 위로 올릴 자리가 없고, subscribe 분기는
  `registry.register(websocket, topics)`로 **topic 리스트를 한 번에** 넘기며, ack을 보내지 않는다.
  그런데 API를 topic 단위로 지어 같은 실패를 세 번 반복했다.
- **상수·기본값을 아무 테스트도 관측하지 않았다.** timeout 상수 6종을 최대 12배로 바꿔도 전체
  스위트가 green이었다(전부 주입 설계라 기본값으로 도는 경로가 없었다).
- **양 끝이 없으면 계약 테스트가 공허하게 green이다.** 서버가 ack을 보내지 않고 클라에 ack 분기가
  없으면, ack 타이밍 계약 테스트는 양쪽이 아무것도 안 해서 통과한다.

→ 종료 조건은 "리뷰 지적 0"이 아니라 **"진입점이 이 함수를 1회 호출하고 그 경로가 실제로 돌았다"**.

#### 2. 노출 상한은 산술로 적고, 그 산술을 강제하는 계약을 먼저 가리켜라

"신선도 캐시 5분 < 인가 상한 15분이니 노출 상한 15분"이라고 적었는데 틀렸다.
`app/subscription.py`의 `CACHE_STALE_TTL`은 1시간이고 그 항목은 나이가 그 값에 도달할 때만 버려지므로
stale 값은 **1시간 직전까지** 쓰인다. 그 마지막 hit가 갱신 기준 시각을 앞으로 밀면 거기서 다시
`app/topic_lease.py`의 `LEASE_MAX_SECONDS`가 나가므로 최악은 약 1시간 15분이다.
올바른 형태는 **(허용 최대 관측 나이 + 인가 상한)** 이고 마지막 한 겹이 붙는다.

더 조용한 두 번째 경로는 **관측 시각의 출처**다. 권위 응답을 우리 타입으로 변환하는 자리에서
시각을 찍으면, 그 응답 객체가 "언제 관측됐는지"를 나르지 못하는 순간(필드가 구독 여부 하나뿐이면
그렇다) 권위 계층이 오래된 객체를 재반환하기만 해도 그 값이 방금 관측으로 승격된다 —
상한이 **무한**이 된다. 닫는 형태: 관측 시각을 응답 타입의 **필수 필드**로 두고 **권위 호출
직전**에 찍는다(응답 시점에 찍으면 데이터가 참이었던 구간 `[호출 시작, 응답]`을 넘길 수 있다).

이 트랙은 유한 상한을 세 번 반올림해 적었고 세 번 다 반증됐다. 발행을 직렬화하는 계약이 없으면
실제 상한은 그 순간 게이트를 통과해 in-flight인 발행 수, 즉 **유한 상한이 없다**.

→ 상한·개수를 쓰기 전에 그것을 강제하는 코드 계약을 한 줄로 가리키고, 못 가리키면 "유한 상한 없음".

#### 3. 저장·재사용은 관측만, 판정은 요청마다 파생

판정은 계정의 권위 상태와 **그 요청의 토큰**의 함수다. 판정을 저장하면 계정 단위 거부를 같은
계정의 새 토큰이 상속하고, 신선도가 쓰기 시점에 박혀 읽기 시점 정책을 적용할 자리가 없어진다.
관측을 재사용하려면 셋을 만족해야 하는데 이 트랙은 셋 다 사후에 발견했다.

- **수치의 단위·규모·유한성**: 인가를 가르는 단일 비교에 두 피연산자 검증이 전무했고, 실측표에서
  단위를 바꾸거나 NaN을 넣은 **네 변형 전부 허용**으로 접혔다. 게다가 그 결과 관측은 양성이라
  신선도가 유지돼 재검증 트리거조차 없다. NaN이 특히 위험한 이유는 `app/topic_lease.py`의
  `compute_lease_expiry` 주석이 이미 적어 뒀다 — `min`은 NaN을 **첫 인자일 때만** 전파하므로
  가드가 없으면 조용히 무시되고 상한 전량이 나간다.
- **주체 결속**: 신선도는 판정 함수에 내재화했는데 주체 일치는 호출부 약속으로 뒀다. 그래서
  서로 다른 주체의 두 관측을 섞어도 통과했고, 인접한 두 정수를 위치 인자로 스왑하면 어떤
  토큰도 무효 판정을 받지 않았다(대응: 필드 추가 + keyword-only 강제).
- **한 번의 읽기**: 두 관측을 각각 잠금을 잡아 읽으면 그 사이에 무효화가 끼어 **동시에 존재한 적
  없는 쌍**으로 허용이 파생된다. 필드에 주체를 박아도 **연결 축은 남는다** — 한 주체의 정품
  인가를 다른 연결에 적용하면 통과했고, 주체를 모르는 계층은 그 축을 스스로 검사할 수 없다.

→ 인가 입력은 "주체가 박힌 **한 번의 원자적 읽기** 결과" 한 묶음, 필드는 keyword-only,
각 수치에 타입·규모대역·유한성 검증. 연결 축은 배선이 결과를 연결 스코프로 소유한다는 회귀
테스트로 따로 닫고, 못 닫는 축은 "열려 있다"고 적는다.

#### 4. fail-closed는 방향·형태·위치를 다 정해야 한다

세 번 다 "안전한 쪽으로 접었다"고 믿었는데 세 번 다 과허용이었다.

- **방향**: 조회 실패를 "권한 없음"으로 접으면 정상 사용자가 축출된다. `app/entitlements.py`의
  `has_entitlement`는 row 존재 여부의 `is not None`만 돌려주므로 **실패 신호가 아예 없고**,
  호출부가 `try/except → False`라는 가장 흔한 패턴을 쓰면 DB 순단이 정상 구독자의 구독 영구
  삭제가 된다. 영구 결함(설정 오류 계열)도 "권한 없음"이 아니라 **"판정 불가"**다.
- **형태**: 실패를 3-state **값**으로 표현하면 닫히지 않는다 — `if not granted: rejected.append(...)`
  한 줄이 그대로 살아 있고 그게 정확히 막으려던 동작이다. 닫는 형태는 **예외 전파**다
  (raise는 거부 목록에 append될 수 없다). 그리고 경계 catch가 특정 예외 목록이면 목록 밖 예외가
  탈출해 상위 광범위 except → finally가 그 연결의 구독을 통째로 지운다 — 막으려던 것보다
  파괴적이다. 경계는 `except Exception`으로 total하게 받고 그 안에서 일시/영구를 가른다.
- **위치**: 접근을 줄이는 전이가 발급 성공에 종속돼 있어 만료·fence 실패·축 위반 3경로 전부
  fail-open이었다. 규칙은 하나다 — **접근을 줄이는 전이는 중단돼도 되돌아가지 않는다.**
  그리고 "검증은 이를수록 좋다"도 틀렸다: 같은 구조 검증을 잠금 앞으로 옮기자 해석 불가한
  요청이 예외로 먼저 나가 구 접근이 생존했고, **해석할 수 없는 쪽이 더 관대**해졌다.
  **검증의 위치가 정책이다.**

#### 5. green의 이유를 확인하라 — 단언·더블·변이 집합이 각각 조용히 거짓말한다

- **기대값을 검사 대상에서 파생시킨 항등식**: allowlist를 `지원 − 게이트`로 정의하고
  `allowlist ∩ 게이트 = ∅`을 단언하면 게이트 내용과 무관하게 항상 참이다. 필수 필드 검사가
  검사 대상 상수를 순회 기준으로 써서 그 상수에서 필드를 지워도 생존한 사례, 루프 변수를 안 써서
  같은 대상을 세 번 검사한 사례가 같은 원인이다.
- **더블이 계약의 축을 기록하지 않음**: 가짜 CLI가 성공/실패만 답하고 인자를 보지 않아 정책 변이가
  생존했고, 가짜 컨테이너 CLI가 두 서브커맨드에 **같은 형식**을 반환해 운영에서 영구 실패할 ID
  형식 불일치를 가렸다. green의 이유가 코드가 옳아서가 아니라 **더블이 보지 않아서**였다.
- **변이 집합의 범위**: "생존 0"이 증명하는 것은 "내가 고른 변이가 관측된다"뿐이다. 추가 방향만
  넣고 **제거** 방향을 빼먹어 "제거만 먼저 커밋"하는 변이가 전체 스위트를 통과했고, 테스트가 잡는
  근거가 타입 **이름**이라 이름을 피하는 변이 2종이 생존했다.
- **순서와 형태**: 두 부수효과가 결국 다 일어나면 최종 상태만 보는 단언은 순서를 자유롭게 둔다 —
  두 단계를 맞바꾼 변이가 30개 테스트를 전부 통과했다. 소스 텍스트/AST 검사기는 별칭·helper
  경유를 놓치고, 반대로 막으려는 안티패턴을 **docstring에 적은 것만으로** red가 되기도 한다.
  검사기에는 **"아무것도 못 찾으면 실패"**하는 자기검사를 붙인다.

→ 단언을 넣기 전에 "이걸 거짓으로 만들 변이"를 적고 **실제로 red를 본 뒤** 커밋한다. 기대값은
검사 대상과 **다른 출처**(행동 호출 결과 또는 손으로 쓴 리터럴)에서 만든다. 변이 보고는
"이 집합 안에서 생존 0"으로 범위를 적는다. 변이가 생존하면 코드보다 **더블을 먼저** 의심한다.

#### 6. main guard 뒤에 정의된 테스트는 직접 실행 시 사라진다 (단, 파일마다 확인할 것)

`unittest.main()`은 호출 시점의 모듈 네임스페이스에서 테스트를 **발견**하므로, guard 뒤에 정의된
클래스는 직접 실행 시 수집조차 되지 않는다. pytest는 모듈을 import만 하니 CI는 전부 green이고,
파일을 직접 돌린 사람만 일부 누락된 채 OK를 본다. 원인을 `sys.exit`로 오진해서도 안 된다 —
프로세스를 죽이지 않는 형태도 똑같이 위험하다.

⚠️ **그러나 격차가 곧 guard 탓은 아니다** (이번 리셋에서 실측):
`tests/test_revenuecat_provider.py`는 같은 내용에서 guard만 파일 끝으로 옮기자 **11 → 20**으로
회복됐다(pytest 20과 일치) — guard가 원인이다. 반면 `tests/test_comparison_api.py`는 이동 전후
모두 21이고 pytest는 25다. 그 4개 격차의 원인은 guard가 아니라 **직접 실행이 `conftest.py`를
로드하지 않는다**는 것이다 — 스텁이 없어 한 클래스가 `setUpClass`에서 죽는다(그 클래스는 애초에
직접 실행이 불가능하다). 두 파일 모두 guard는 옮겼으나, **"격차 = guard 탓"으로 뭉치면 원인 오진이다.**

### 다음 슬라이스 (재시작 계약)

1. 첫 커밋에서 진입점의 실제 호출 모양(topic 리스트 배칭 · 종단 라우팅 부재 · ack 부재)을 읽고
   그것을 API 시그니처의 **출발점**으로 삼는다.
2. 슬라이스 완료 정의 = **"진입점이 1회 호출하고 그 경로가 실제로 돌았다"**.
3. 착수 시 노출 상한을 (허용 최대 관측 나이 + 인가 상한) 산술로 적고, 강제 계약을 못 가리키면
   "유한 상한 없음"으로 적는다.
4. 전달값은 주체가 박힌 관측 **한 묶음**(원자적 1회 읽기 · keyword-only · 수치 검증 · 권위 호출
   직전 스탬프)이고 판정은 매 요청 파생 — 판정과 신선도 판단은 저장하지 않는다.
5. 판정 불가는 예외로 올려 경계 `except Exception`에서 요청 전체를 접고, 접근 축소는 중단돼도
   되돌리지 않으며, 새 단언은 red를 만드는 변이를 **추가·제거 양방향**으로 먼저 확인한 뒤 커밋한다.
6. **넣지 않는다**: 관측 캐시 · 요청 합치기(single-flight) · 만료 청소 주기 작업 · 별도 관측 저장소.
   최적화는 실제 병목을 측정한 뒤에만 추가한다.

#### 스파이크 harness 제약 (2026-07-30 실측 — 착수 전 반드시 읽을 것)

첫 red 테스트를 실제 WebSocket 경계에서 관측할 수 있는지 프로브로 확인했다. **가능하다.**
단 아래 셋을 지켜야 한다.

- ⛔ **`with TestClient(app)` 를 쓰지 말 것 — lifespan이 실행된다.** 실측: 스케줄러·크롤러가
  모두 살아나 KIS WebSocket에 **운영과 같은 appkey로 접속**해 `ALREADY IN USE appkey`가 났고
  (KIS가 새 연결을 거부, 운영 세션은 유지 — `reconnect_attempts=0`로 무영향 확인), Investing
  크롤러가 외부 요청까지 보냈다. 테스트 harness가 프로덕션 통합을 켜는 것은 그 자체로 결함이다.
- ✅ **`TestClient(app)` 을 `with` 없이** 만들면 lifespan을 건너뛰고 `websocket_connect("/ws")` 가
  정상 동작한다(실측: 연결 직후 서버가 `type="rates"` 를 먼저 보낸다 → 첫 `receive` 는 그것이다).
- ⚠️ 로컬에서 Redis(`redis:6379`)는 도달 불가라 호출마다 **전체 트레이스백이 로그로** 나온다.
  앱은 degrade 하고 진행하므로 기능에는 문제가 없지만, 테스트 출력이 묻히므로 로그를 낮추거나
  Redis 계층을 주입 가능하게 만들 것.
- ⛔ **정정 (codex Blocker)**: 한때 "lifespan 미진입이 이미 외부 호출을 없앤다"고 적었는데 **틀렸다**.
  lifespan 회피가 막는 것은 **스케줄러·크롤러**뿐이고, **요청 경로는 그대로 외부를 부른다** —
  `/ws` 핸들러는 연결 즉시 `SessionLocal()`(DB) + `await redis_cache.get(BROADCAST_CACHE_KEY)`(Redis)를
  호출한다(`app/main.py`의 `websocket_endpoint` 앞부분). 실제로 lifespan 미진입 프로브에서 Redis
  트레이스백이 났다는 사실이 그 증거다. **앞으로 subscribe 핸들러는 Firebase·RevenueCat도 부르게
  되므로**, 이 오해를 남겨 두면 스파이크가 그 두 곳에 **실제 호출**을 하게 된다(KIS 사고와 같은 부류).
  요청 경로 네트워크를 막는 것은 **fake 주입**이다.
- ✅ **새 harness를 만들지 말 것 — 이미 있다.** `tests/test_topic_initial_snapshot_e2e.py`가 같은
  `/ws` 경계를 쓰면서 필요한 fake를 전부 명시적으로 주입한다(내가 프로브를 짤 때 이걸 못 찾고 더
  나쁜 버전을 재발명했다):
  `TestClient(app)` **`with` 없이** / `patch.object(config, "TOPIC_DISPATCHER_ENABLED", True)` /
  `patch("app.topic_initial_snapshot._build_snapshot_sync", …)` /
  `patch("app.main.redis_cache.get", AsyncMock(return_value=None))` /
  `patch("app.main.verify_firebase_token", …)` / `patch("app.main.require_premium", …)` /
  그리고 `receive_json`을 thread+join으로 감싸 **미전달을 hang 대신 fast fail**로 바꾸는 헬퍼.
- ⚠️ 소켓을 monkeypatch 해 외부 연결을 차단하려 했더니 **Redis 재시도 경로에서 행**이 났다
  (실측: 180초 timeout). 네트워크 격리를 그 방식으로 하지 말 것 — **fake 주입**으로 할 것.
- ⚠️ **이름 규율**: 서버 E2E가 통과해도 iOS 토큰·ack 처리가 끝나기 전에는 **"제품 E2E"라 부르지 말 것**
  (codex). 서버 경계 E2E와 제품 E2E는 다른 주장이다.


---

## ADR-041: topic-only 전환

- 책임: 불변식 · 결정 · arming 게이트
- 상태: Draft — 구현 착수 전 합의 대상
- 코드 근거 기준일: 2026-08-09
- server 기준 commit: `d8dd4b45a0a90c867e66331c3761a252fa6af296`
- iOS 기준 commit: `10995282ec181a7055f4073e7a7bfa2c48e783f8`
- archive SHA: `cde1d2ca3e714733776e1b0d7e821a542e1f8d183cb2951bef8c93fb444d9814`
- manifest SHA: `50e694c45b143465fc84e0672af9d9f592e8090c1688632c8904194af3eb1913`
- baseline SHA: `4cc944e333789fb4a2ff08217d2dbd3469f29c9f09826946bb1776d43c27d708`
- 검증: `python3 scripts/topic_migration_manifest.py preflight`

이 ADR 은 topic-only 전환의 **불변식 · 결정 · arming 게이트**를 소유한다. 서버 build/ack/close 계약,
클라이언트 상태기계, 삭제 범위, jitter/single-flight/bounded wait, publisher health/SLO 는 각각 별도
문서가 소유하며, 아래 요구사항 구간의 관계 링크로 따라간다. 원문 전체는
`TOPIC_ONLY_DELIVERY_CONTRACT.archive.md` 에 보존돼 있다.

---

<!-- rid: R-CTX-2 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-ctx-2"></a>
### R-CTX-2 — 적용 범위

> **대상**: 신규 iOS(reference) → Android 이식. 서버는 이행 기간 동안 legacy 병행 유지.

<!-- /rid: R-CTX-2 -->

<!-- rid: R-CTX-1 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-ctx-1"></a>
### R-CTX-1 — 왜 이 결정을 새로 기록해야 하는가

리포가 **스스로 갈려 있다**. 같은 질문("topic 이 조용하면 무엇을 보여줄 것인가")에 두 문서가 반대로 답한다.

| 출처 | 서술 |
|---|---|
| `REALTIME_V2_CLIENT_GUIDE.md` §8 | 휴장/주말 stale UI 는 단말 정책 — **권고: 마지막 값 유지** |
| `DECISIONS.md` ADR-038 D2 | MODE 2 revert(45초 후 legacy 전환)를 **전제로 설계** — KRX 수신이 tether 신선도를 연장하지 않게 만든 이유가 그것 |
| `TOPIC_V2_RELEASE_RUNBOOK.md` | "topic 무수신 45s 초과 시 **자동으로 legacy 표시**"를 합격 기준·롤백 근거로 사용 |

즉 MODE 2 는 iOS 가 혼자 만든 정책이 아니라 **서버 ADR 이 한 번 승인한 적 있는** 정책이다.
따라서 "클라를 서버 계약에 맞춰라"로 정리되지 않는다 — **결정을 새로 기록해야** 코드를 고쳐도
다음 사람이 되돌리지 않는다.
<!-- /rid: R-CTX-1 -->

<!-- rid: R-INV-1 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-inv-1"></a>
### R-INV-1 — 불변식: 신규 앱은 legacy 를 읽지 않는다

```
구독자   → topic WS + 인증된 v2 topic snapshot
무료     → 인증된 v2 hourly snapshot
어느 쪽도 legacy REST/WS 를 읽지 않는다 (DXY 포함 — DXY topic 신설이 선행조건이다).
서버의 legacy 병행은 오직 구버전 출시 앱을 위한 것이다.
```

> ✅ **코드 불변식은 충족됐다.** 서버 `8755510`과 iOS `0f2a3f8`이 신규 앱의 legacy
> REST/WS/cache/graph 소비를 제거하고 `dxy:spot`까지 topic 경로로 옮겼다. 다만 운영
> `WS_TOPIC_AUTH_STAGE` 수렴·dispatcher canary·Release arming은 별도 게이트이며 아직 완료가 아니다.
> iOS architecture test가 legacy API/callback/파일의 복원을 거부하고
> (`ios/FXiTests/TopicMessageTests.swift:386-428`), 서버 publisher는 `dxy:spot` payload와 live 발행을
> 소유한다 (`app/dxy_topic_publisher.py:73-83` · `app/dxy_topic_publisher.py:115-134`).
> 아래 [R-INV-2](#r-inv-2) · [R-INV-3](#r-inv-3)은 운영 상태를 구분한다.

**불변식의 근거는 두 겹이다 — 둘 다 적어 둔다.** 하나만 남기면 다른 하나가 잊힌다.

<!-- evidence: E-INV-1 supports=R-INV-1 -->
**근거 ① 인가** — `FREE_TIER_ACCESS_MODEL_PLAN.md` **D4 "신규 앱 legacy _anon_ fallback 금지(양 플랫폼)"**.
legacy `/api/rates`·WS `rates` 는 **전부 무인증**이므로, 신규 앱이 legacy 로 떨어지면
비구독자가 실시간을 공짜로 얻는다 = 페이월 우회.
고정 server commit 의 legacy REST handler 와 `/ws` 연결 경로에도 Firebase/premium 검사가 없다
(`app/main.py:1241-1290` · `app/main.py:1091-1135`).
⚠️ `DECISIONS.md` ADR-039 요약은 이 문장에서 **`anon` 을 떨어뜨렸다**. 요약이 원문보다 강하다 —
같은 슬라이스에서 정정한다.
<!-- /evidence: E-INV-1 -->

<!-- evidence: E-INV-2 supports=R-INV-1 -->
**근거 ② 제품** — 고정 server commit 의 legacy source set 에는 USDT/KRX 가 **없다**.
`app/legacy_policy.py` 의 `LEGACY_RATE_SOURCES` 는 investing + 은행 9곳뿐이고 docstring 이
doctest 로 못 박는다:
`should_include_source_in_legacy_rates("upbit", "usdt-krw") → False`.
과거 테더 화면의 legacy fallback은 USDT/KRX를 공급하지 못하는 **데이터 손실**이었다. 현재 iOS는 그 fallback 자체를
제거했으며, topic gate가 OFF면 legacy reference 행을 대신 그리지 않고 빈 topic 표면을 반환한다
(`ios/FXi/ViewModels/ExchangeRateViewModel.swift:691-697`). 따라서 unarmed artifact는 usable한
fallback 빌드가 아니며 출시할 수 없다.
근거: `app/legacy_policy.py:35-38` · `app/legacy_policy.py:59-60`.
<!-- /evidence: E-INV-2 -->
<!-- /rid: R-INV-1 -->

<!-- rid: R-INV-2 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-inv-2"></a>
### R-INV-2 — 격차 (a): WS 의 FX/USDT premium 강제 — **경로 구현됨, 운영 활성화 미확인**

**(a)** `0cfe474`에서 WS FX/USDT premium 강제 경로가 추가됐다
(`app/topic_policy.py:278-323` · `app/topic_dispatcher.py:792-948`). 적용 여부는
`WS_TOPIC_AUTH_STAGE` 에 따른다:

코드 기본값은 `compatibility` 다(`app/config.py:782-784`). production 의 실제 값은 아래 표가
아니라 운영 직접 측정으로 확정한다.

| stage | 익명 FX | 익명 USDT | 식별 FX/USDT | 식별 KRX |
|---|---|---|---|---|
| `compatibility` | 허용 | 허용 | identity-only | premium + entitlement |
| `reject_anonymous_fx` | 거부 | 허용 | identity-only | premium + entitlement |
| `enforce_authenticated_premium` | 거부 | 거부 | **premium-only** | premium + entitlement |

⛔ **코드 구현과 운영 활성화를 분리한다.** 이 문서 재검토에서는 production env 를 직접 측정하지
않았으므로 현재 운영 stage 나 과거 활성화 이력을 단정하지 않는다. 활성화 GO 직전에 실행 중인
컨테이너와 env 를 직접 확인해야 한다. 최종 stage 에서는 authorizable topic 이 있는 식별 subscribe
마다 RevenueCat 왕복이 1회 생기고(stale fallback 은 REST 전용), FX 의 실효는 무인증 legacy
브로드캐스트 때문에 Stage B 까지 제한된다([R-OPEN-4](#r-open-4)).
근거: `app/config.py:741-784`(기본값 `compatibility`) · `app/topic_authorization.py:278-315`
(cache-free `fetch_revenuecat_result`) · `app/subscription.py:403-464`(stale fallback 은 REST 전용).

구현 근거: `app/config.py:741-784`(stage) · `app/topic_policy.py:78-85`(정책표) ·
`app/topic_policy.py:237-275`(익명 planner) · `app/topic_policy.py:278-323`(식별 planner) ·
`app/topic_authorization.py:372-402`(coordinator) · `app/topic_dispatcher.py:792-948`(배선·등록) ·
`app/main.py:3251-3255`(REST twin).

<!-- relation: references target=R-CLI-6 -->
- references: [R-CLI-6](spec/ios-topic-state-machine.md#r-cli-6)
<!-- /rid: R-INV-2 -->

<!-- rid: R-INV-3 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-inv-3"></a>
### R-INV-3 — 격차 (b) 해소: DXY 는 `dxy:spot` topic 으로 온다

당시 격차는 `8755510`(서버) + `0f2a3f8`(iOS)에서 해소됐다. 서버는 `dxy:spot` publisher와
initial snapshot을 제공하고(`app/dxy_topic_publisher.py:115-134` ·
`app/topic_initial_snapshot.py:972-995`), iOS는 인증 REST bootstrap과 WS topic frame을 같은
`DxyLiveTick`으로 merge한다(`ios/FXi/Services/TopicSnapshotService.swift:53-58` ·
`ios/FXi/ViewModels/ExchangeRateViewModel.swift:980-1010`). 신규 앱의 legacy `indices` 소비는 제거됐다.

⛔ **초안은 여기서 "legacy rate 값만 금지"로 불변식을 좁히고 envelope 예외를 두려 했다. 철회한다.**
`FREE_TIER_ACCESS_MODEL_PLAN.md` §6 롤아웃이 **이미** 순서를 정해 뒀다 —
*"4. iOS legacy 이탈: 4a REST/WS 토큰 전달 → **4b `dxy:spot` topic 신설** → 4c 부팅·offline·stale 를
topic snapshot/cache 기준 전환 → 4d /api/rates + legacy WS 제거"*.
4a와 4b는 끝났고 4c/4d도 같은 iOS cutover commit에 land했다. 남은 것은 최종 WS 인증 stage와
dispatcher를 포함한 운영 canary, 실기기 smoke, Release arming의 별도 GO다.
<!-- /rid: R-INV-3 -->

<!-- rid: R-INV-5 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-inv-5"></a>
### R-INV-5 — 채택: 불변식은 강한 형태 유지, DXY topic 신설을 선행조건으로 올린다

→ **채택**: 불변식은 **강한 형태 그대로 유지**하고, **DXY topic 신설을 출시 선행조건으로 올린다**.

<!-- relation: references target=R-HAND-19 -->
- references: [R-HAND-19](spec/topic-snapshot-handoff.md#r-hand-19)
<!-- /rid: R-INV-5 -->

<!-- rid: R-INV-4 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-inv-4"></a>
### R-INV-4 — 범위 확정: 이번 출시는 `dxy:spot` 하나만

✅ **범위 확정: 이번 출시는 `dxy:spot` 하나만**(로드맵 4b 그대로).
근거 — premium live bridge 가 **spot 만** `dxyLive` 로 보충하고 `dxy_futures` 의 live state 는 없다.
따라서 futures topic 은 **legacy 이탈에 불필요**하며 phased 로 미룬다.
(테더 1d 그래프의 DXY_futures 계열은 그래프 데이터 경로이지 live tail 이 아니다.)
근거: `ios/FXi/Models/ExchangeRate.swift:53-58` ·
`ios/FXi/ViewModels/ExchangeRateViewModel.swift:21-24` ·
`ios/FXi/ViewModels/ExchangeRateViewModel.swift:977-987`.
<!-- /rid: R-INV-4 -->

<!-- rid: R-DEC-1 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-dec-1"></a>
### R-DEC-1 — monitor 의 의미 전환

`8aadc2f` 가 만든 것(관측되는 저장 freshness + deadline monitor + 전용 `ContinuousClock`)은 **유지한다**.
바꾸는 것은 **만료 시 취하는 행동**과 **45초가 무엇의 지표인가**이다.

```
before:  45초 = 데이터 만료  → topic 값 폐기 → legacy 표시
after:   45초 = 전달 이상 의심 → 조용히 재검증 → 실패 확정 시에만 사용자에게 알림
```

**근거 — 45초는 데이터의 나이가 아니다.**

<!-- evidence: E-B-1 supports=R-DEC-1 -->
- **USDT**: Redis coalesce 조건이 `same rate AND same 5s bucket`
  (`latest_rates_cache.py`) — 가격이 평평해도 새 5초 버킷에 tick 이 들어오면 SET → publish.
  → `usdt:krw` 침묵 = 가격 안정이 아니라 **tick 부재**(체결 없음 또는 collector 사망).
  근거: baseline B2.
<!-- /evidence: E-B-1 -->

<!-- evidence: E-B-2 supports=R-DEC-1 -->
- **KRX**: 위 coalesce 는 **Stage E tick writer 경로에서만** 같다(`KRX_REDIS_TICK_WRITE_ENABLED`,
  코드 기본값 false / 운영은 2026-05-26 활성). 일반 KRX writer 는 매번 SET 한다.
  그리고 장마감(15:45) 후 무발행이 정상 — 이미 시간 기반 staleness 가 **없다**(ADR-038 D2).
  근거: baseline B3 · B3-op · `app/latest_rates_cache.py:818-824` ·
  `app/latest_rates_cache.py:662-667` · `app/config.py:392`.
<!-- /evidence: E-B-2 -->

<!-- evidence: E-B-3 supports=R-DEC-1 -->
- **FX**: 주말·휴장 무발행이 정상이다. 최대 수십 시간.
<!-- /evidence: E-B-3 -->

<!-- evidence: E-B-4 supports=R-DEC-1 -->
- publisher 모듈 자체에는 timer 가 없다(baseline B1 의 **범위 한정**). 외부의
  `broadcast_rates_once` 는 매초 wake-up 하지만 publisher 호출은 payload `is_changed` 분기 안이다
  (`app/scheduler.py:1402-1412` · `app/main.py:947-970`). 따라서 현재 경로에는
  **topic data-plane heartbeat·무조건 주기 재발행 계약이 없다**.
  ⚠️ transport 레벨 ping/pong 은 **있다**(iOS 30초 ping ↔ 서버 pong) — 그건 연결 생존만 증명하고
  특정 topic publisher 의 생존은 증명하지 않는다.
  근거: baseline B1 · `ios/FXi/Utils/Constants.swift:243`.
<!-- /evidence: E-B-4 -->

<!-- evidence: E-B-5 supports=R-DEC-1 -->
- ack 에 **`server_time` 이 없다**(`topic_wire.py`) → WS 경로로 기기 시계 오차를 잴 수단이 현재 없다.
  근거: baseline B5.
<!-- /evidence: E-B-5 -->
<!-- /rid: R-DEC-1 -->

<!-- rid: R-OPEN-2 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-open-2"></a>
### R-OPEN-2 — 일관된 관측 나이(후속, 출시 필수 아님)

[제안·결정 대기]

일관된 나이가 필요해지면 둘 중 하나 — merger 가 값을 버릴 때도 `seen_at` 은 갱신하도록
고치거나(클라 전용, 단 DB-fallback 분기 필요), 서버가 `observed_at` 을 추가한다.
**출시 필수가 아니다.**
<!-- /rid: R-OPEN-2 -->

<!-- rid: R-GATE-1 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-gate-1"></a>
### R-GATE-1 — 순서 ① 서버: WS FX/USDT 인증 + premium 강제

**순서와 게이트 — 서버 ①**: WS FX/USDT 인증 + premium 강제 (ADR-039 Stage A). ← 새 최우선.
[R-INV-2](#r-inv-2) 의 격차 (a) 를 닫는 항목이다.

<!-- relation: references target=R-HAND-11 -->
- references: [R-HAND-11](spec/topic-snapshot-handoff.md#r-hand-11)
<!-- /rid: R-GATE-1 -->

<!-- rid: R-GATE-6 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-gate-6"></a>
### R-GATE-6 — 순서 ② 서버: DXY topic 신설

**서버 ②**: **DXY topic 신설**(계획 4b) — **이게 없으면 legacy 이탈이 불가능**하다.

<!-- relation: references target=R-HAND-19 -->
- references: [R-HAND-19](spec/topic-snapshot-handoff.md#r-hand-19)
<!-- relation: references target=R-INV-3 -->
- references: [R-INV-3](#r-inv-3)
<!-- relation: references target=R-INV-4 -->
- references: [R-INV-4](#r-inv-4)
<!-- relation: references target=R-INV-5 -->
- references: [R-INV-5](#r-inv-5)
<!-- /rid: R-GATE-6 -->

<!-- rid: R-GATE-5 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-gate-5"></a>
### R-GATE-5 — 순서 ③④ 서버: send 실패 시 close · snapshot 최소 계약

**서버 ③**: send 실패 시 close.
**서버 ④**: **최소 계약** — 정적 unavailable 만 ack 전, 나머지는 등록→ack→전송, 실패는
close(1013/1011).

<!-- relation: references target=R-HAND-1 -->
- references: [R-HAND-1](spec/topic-snapshot-handoff.md#r-hand-1)
<!-- relation: references target=R-HAND-2 -->
- references: [R-HAND-2](spec/topic-snapshot-handoff.md#r-hand-2)
<!-- relation: references target=R-HAND-6 -->
- references: [R-HAND-6](spec/topic-snapshot-handoff.md#r-hand-6)
<!-- /rid: R-GATE-5 -->

<!-- rid: R-CUT-15 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-cut-15"></a>
### R-CUT-15 — 순서 ⑤ iOS: legacy 소비 전면 cutover

→ iOS **⑤**: legacy 소비 **전면** cutover (startup·WS parser·은행/김프/비교 알림 + DXY).

<!-- relation: references target=R-CUT-1 -->
- references: [R-CUT-1](spec/legacy-cutover.md#r-cut-1)
<!-- relation: references target=R-CUT-2 -->
- references: [R-CUT-2](spec/legacy-cutover.md#r-cut-2)
<!-- relation: references target=R-CUT-3 -->
- references: [R-CUT-3](spec/legacy-cutover.md#r-cut-3)
<!-- relation: references target=R-CUT-4 -->
- references: [R-CUT-4](spec/legacy-cutover.md#r-cut-4)
<!-- relation: references target=R-CUT-5 -->
- references: [R-CUT-5](spec/legacy-cutover.md#r-cut-5)
<!-- relation: references target=R-CUT-6 -->
- references: [R-CUT-6](spec/legacy-cutover.md#r-cut-6)
<!-- relation: references target=R-CUT-7 -->
- references: [R-CUT-7](spec/legacy-cutover.md#r-cut-7)
<!-- relation: references target=R-CUT-8 -->
- references: [R-CUT-8](spec/legacy-cutover.md#r-cut-8)
<!-- relation: references target=R-INV-1 -->
- references: [R-INV-1](#r-inv-1)
<!-- /rid: R-CUT-15 -->

<!-- rid: R-GATE-3 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-gate-3"></a>
### R-GATE-3 — 순서 ⑥⑦⑧⑨ iOS + 문서 정정

- **⑥** last-known + **9종 2층** 사유 상태기계 (`topic_unavailable`·`unknown_topic` 신규 배선)
- **⑦** 조용한 재구독 → 실패 확정 시 배너 / 수신 세대 기반 snapshot deadline
- **⑧** lease hard-expiry (클라 (A))
- **⑨** purge(메모리 + `cached_topic_rates` + 파생 상태) / 알림 cold-start fail-close
- → 문서 정정 + 런북 합격 기준 교체

<!-- relation: references target=R-CLI-10 -->
- references: [R-CLI-10](spec/ios-topic-state-machine.md#r-cli-10)
<!-- relation: references target=R-CLI-11 -->
- references: [R-CLI-11](spec/ios-topic-state-machine.md#r-cli-11)
<!-- relation: references target=R-CLI-12 -->
- references: [R-CLI-12](spec/ios-topic-state-machine.md#r-cli-12)
<!-- relation: references target=R-CLI-13 -->
- references: [R-CLI-13](spec/ios-topic-state-machine.md#r-cli-13)
<!-- relation: references target=R-CLI-18 -->
- references: [R-CLI-18](spec/ios-topic-state-machine.md#r-cli-18)
<!-- relation: references target=R-CLI-3 -->
- references: [R-CLI-3](spec/ios-topic-state-machine.md#r-cli-3)
<!-- relation: references target=R-CLI-6 -->
- references: [R-CLI-6](spec/ios-topic-state-machine.md#r-cli-6)
<!-- relation: references target=R-CLI-9 -->
- references: [R-CLI-9](spec/ios-topic-state-machine.md#r-cli-9)
<!-- relation: references target=R-CUT-18 -->
- references: [R-CUT-18](spec/legacy-cutover.md#r-cut-18)
<!-- relation: references target=R-CUT-9 -->
- references: [R-CUT-9](spec/legacy-cutover.md#r-cut-9)
<!-- /rid: R-GATE-3 -->

<!-- rid: R-CUT-17 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-cut-17"></a>
### R-CUT-17 — 실기기 리허설 (auth 매트릭스 포함)

→ 실기기 리허설 (auth 매트릭스 포함: 무토큰 / non-premium / premium / KRX entitlement / revoke).

<!-- relation: references target=R-CUT-12 -->
- references: [R-CUT-12](spec/legacy-cutover.md#r-cut-12)
<!-- relation: references target=R-CUT-13 -->
- references: [R-CUT-13](spec/legacy-cutover.md#r-cut-13)
<!-- /rid: R-CUT-17 -->

<!-- rid: R-GATE-4 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-gate-4"></a>
### R-GATE-4 — 마지막 두 게이트는 각각 별도 GO

- → ⛔ Release arming (별도 GO)
- → ⛔ 서버 `TOPIC_DISPATCHER_ENABLED=true` (별도 GO)
<!-- /rid: R-GATE-4 -->

<!-- rid: R-GATE-7 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-gate-7"></a>
### R-GATE-7 — phased 로 미루는 것

**phased 로 미루는 것**: 서버 lease expiry sweeper / graph age 의미 재설계 /
usd 탭 KRX tail 결합 해소 / 역사 ADR 정리(단, **운영에 쓰는 런북과 현재 코드 주석은 출시 전**).

<!-- relation: references target=R-CLI-15 -->
- references: [R-CLI-15](spec/ios-topic-state-machine.md#r-cli-15)
<!-- relation: deferred_references target=R-CLI-19 -->
- deferred_references: [R-CLI-19](spec/ios-topic-state-machine.md#r-cli-19)
<!-- relation: deferred_references target=R-HAND-10 -->
- deferred_references: [R-HAND-10](spec/topic-snapshot-handoff.md#r-hand-10)
<!-- relation: deferred_references target=R-OPEN-2 -->
- deferred_references: [R-OPEN-2](#r-open-2)
<!-- /rid: R-GATE-7 -->

<!-- rid: R-GATE-2 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-gate-2"></a>
### R-GATE-2 — Release arming(`TOPIC_V2_RELEASE_ON`)은 사용자 소유의 마지막 게이트

고정 iOS commit 에서 Release 는 `TOPIC_V2_RELEASE_ON` 이 정의될 때만 topic 을 켠다. 그 외에는
fail-closed로 topic 표면이 비며 legacy fallback으로 돌아가지 않는다
(`ios/FXi/Utils/RealtimeV2Config.swift:31-39` ·
`ios/FXi/ViewModels/ExchangeRateViewModel.swift:691-697`). 같은 commit 의
`FXi.xcodeproj/project.pbxproj` 에서 해당 플래그를 찾는 `git grep` 결과는 0건이다.

⚠️ archive 가 기록한 "현재 미커밋 변경 한 건"은 동결 baseline 이 명시적으로 제외한 worktree
관찰이므로 정본 사실로 승격하지 않는다. Release arming 은 계속 **사용자 소유**이며 자동화가
커밋하지 않는다. 정식 커밋 + Release archive 확인은 **마지막 게이트**로 두고 명시적 GO 를 받는다.
<!-- /rid: R-GATE-2 -->

<!-- rid: R-DEC-4 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-dec-4"></a>
### R-DEC-4 — 확정 / 제안의 구분

⛔ **(1) 은 아직 _제안_ 이다 — 사용자 결정 전이다.** (2)(3) 은 확정.

- (1) 파생 숫자(김프/비교 spread) 숨김 정책 — [R-DEC-2](#r-dec-2) · [R-DEC-5](#r-dec-5) ·
  [R-DEC-3](#r-dec-3).
- (2) lease — 출시는 클라 hard-expiry (A), 서버 sweeper (B) 는 후속. (확정)
- (3) nginx — WS handshake `limit_req` 는 추가, IP별 `limit_conn` 은 계측 후. (확정)

<!-- relation: references target=R-CLI-16 -->
- references: [R-CLI-16](spec/ios-topic-state-machine.md#r-cli-16)
<!-- relation: references target=R-LOAD-2 -->
- references: [R-LOAD-2](spec/revalidation-and-load.md#r-load-2)
<!-- /rid: R-DEC-4 -->

<!-- rid: R-DEC-2 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-dec-2"></a>
### R-DEC-2 — (1) 파생 숫자는 시간이 아니라 확정된 전달 이상으로만 숨긴다

[제안·결정 대기]

**(1) 김프 등 파생 숫자는 _시간_ 으로 숨기지 않는다 — _전달 이상이 확정된 경우_ 에만 숨긴다.**

초안과 앞선 설계안은 "두 다리의 나이 ≤120초" 같은 **시간 게이트**를 제안했다. **채택하지 않는다** —
Gopax 정상 tick 간격이 ~180초라 그 게이트는 **평상시에 김프를 상시 숨긴다**(지키려던 기능을 죽인다).
그리고 관측 나이를 잴 신호 자체가 없다.

→ **재구독·재연결이 실패해 전달 이상이 확정된 경우에만** 파생 숫자(김프/비교 spread)를 숨긴다.
**원시 last-known 행은 경고와 함께 유지**한다.

<!-- relation: references target=R-CLI-14 -->
- references: [R-CLI-14](spec/ios-topic-state-machine.md#r-cli-14)
<!-- relation: references target=R-CLI-8 -->
- references: [R-CLI-8](spec/ios-topic-state-machine.md#r-cli-8)
<!-- relation: references target=R-CLI-9 -->
- references: [R-CLI-9](spec/ios-topic-state-machine.md#r-cli-9)
<!-- /rid: R-DEC-2 -->

<!-- rid: R-DEC-5 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-dec-5"></a>
### R-DEC-5 — 이 수용은 조건부다: arming 차단 게이트 3개

[제안·결정 대기]

**⛔ 이 수용은 _조건부_ 다 — 조건이 안 서면 수용하지 않는다.**
클라가 못 보는 실패를 **아무도 안 보면** 그건 수용이 아니라 방치다. **arming 차단 게이트 3개**는
아래 관계로 잠근다 — publisher health/SLO 구현·검증, safety-stop 리허설 실측, 45초 동시 재구독
폭주 완화 구현·검증. 셋은 [R-GATE-4](#r-gate-4) 의 Release arming 앞에 선다.

<!-- relation: references target=R-CUT-14 -->
- references: [R-CUT-14](spec/legacy-cutover.md#r-cut-14)
<!-- relation: references target=R-GATE-4 -->
- references: [R-GATE-4](#r-gate-4)
<!-- relation: references target=R-HLT-1 -->
- references: [R-HLT-1](spec/publisher-health-slo.md#r-hlt-1)
<!-- relation: references target=R-HLT-2 -->
- references: [R-HLT-2](spec/publisher-health-slo.md#r-hlt-2)
<!-- relation: references target=R-LOAD-1 -->
- references: [R-LOAD-1](spec/revalidation-and-load.md#r-load-1)
<!-- /rid: R-DEC-5 -->

<!-- rid: R-DEC-3 -->
<!-- requirement-meta: disposition=proposed owner=None -->
<a id="r-dec-3"></a>
### R-DEC-3 — 게이트 미이행 시 수용 불가 + 잔존 위험의 한계

[제안·결정 대기]

클라 status signal(서버 health 를 클라에 전달)은 후속으로 둔다.
⛔ **셋 중 하나라도 미루면 수용으로 보지 않는다.**
⚠️ **위 1~3 중 하나라도 출시 후속이라면 잔존 위험을 수용해서는 안 된다** — 그 경우 김프 표시 정책을
다시 논의해야 한다.

⚠️ **한계를 명시한다 — "파이프가 죽었나"가 항상 잴 수 있는 건 아니다.** **정상 initial
snapshot 이후의 publisher 사망은 클라가 판별할 수 없다**. 따라서 숨김 조건은 **클라가 확정 가능한**
전달 실패(재구독·재연결 실패, close, 명시적 거부)에 한한다. 그 밖의 조용한 사망에서는 **김프가
last-known 조합으로 계속 보일 수 있다** — 이건 **제품이 수용하는 잔존 위험**이고, 없애려면 서버
health 결과를 클라에 전달하는 status signal 이 필요하다(후속).

<!-- relation: references target=R-HLT-1 -->
- references: [R-HLT-1](spec/publisher-health-slo.md#r-hlt-1)
<!-- /rid: R-DEC-3 -->

<!-- rid: R-OPEN-3 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-open-3"></a>
### R-OPEN-3 — 미결 1 해소: `dxy:spot` / `dxy:futures` 분리

1. ~~`dxy:spot` / `dxy:futures` 분리~~ → **확정: 이번 출시는 `dxy:spot` 만**(로드맵 4b 그대로).
   근거: premium live bridge 가 **spot 만** `dxyLive` 로 보충하고 `dxy_futures` live state 는 없다
   → futures topic 은 legacy 이탈에 **불필요**, phased.
   근거: `ios/FXi/Models/ExchangeRate.swift:53-58` ·
   `ios/FXi/ViewModels/ExchangeRateViewModel.swift:21-24` ·
   `ios/FXi/ViewModels/ExchangeRateViewModel.swift:977-987`.

<!-- relation: references target=R-INV-4 -->
- references: [R-INV-4](#r-inv-4)
<!-- /rid: R-OPEN-3 -->

<!-- rid: R-OPEN-1 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-open-1"></a>
### R-OPEN-1 — 미결 2 해소: Stage A 범위

2. ~~Stage A 범위~~ → **미결이 아니다.** `FREE_TIER_ACCESS_MODEL_PLAN` D2 가 이미 확정했다 —
   **비-KRX 최신 topic = Firebase 인증 + premium / KRX = premium + entitlement**.
<!-- /rid: R-OPEN-1 -->

<!-- rid: R-OPEN-4 -->
<!-- requirement-meta: disposition=active owner=ADR -->
<a id="r-open-4"></a>
### R-OPEN-4 — Stage A 의 보장 범위 한정을 ADR 요약에 명시한다

⚠️ 다만 **보장 범위 한정은 ADR 요약에 명시**한다 — Stage A 는 **신규 앱 계약 준수**이지 서비스
전체의 페이월 우회 제거가 **아니다**(구버전용 익명 legacy 는 Stage B 까지 남는다).

<!-- relation: references target=R-HAND-11 -->
- references: [R-HAND-11](spec/topic-snapshot-handoff.md#r-hand-11)
<!-- relation: references target=R-OPEN-1 -->
- references: [R-OPEN-1](#r-open-1)
<!-- /rid: R-OPEN-4 -->
