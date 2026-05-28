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
> 🏷️ **상태**: 초안 (Stage 1 canary 운영 데이터 수집 중, 수치 미확정)

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

1. KIS REST close snapshot은 더 이상 authoritative write source가 아니다.
2. REST 호출은 diagnostic으로 유지될 수 있지만 DB/Redis write는 default off다.
3. Case B는 REST 성공 write가 아니라 `rest_write_blocked` diagnostic signal로 해석한다.
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

env `KRX_CLOSE_REST_WRITE_ENABLED=true` + `docker compose up -d --force-recreate fastapi`로 기존 1차/2차 PR write 동작 복원 가능. 단 KIS REST stale 위험 동반 — 회귀 사고 가능성.

#### 다음 단계

- 5/19~5/26 7일 telemetry 분석 시 신규 `rest_write_blocked` counter 결합 분석 → case B 분포 + KIS REST stale 재발 빈도 → REST close fallback 코드 완전 제거 vs 검증 가능성 재검토 결정.
- Stage E (KRX Redis tick-level 전환, [KRX_FANOUT_REFACTOR_PLAN.md §5.2 E](KRX_FANOUT_REFACTOR_PLAN.md))는 본 결정과 직교 작업 — 별도 진입.

상세 사고 기록 + 운영 보정 명령: [KRX_CLOSE_SNAPSHOT_PLAN.md §5.7](KRX_CLOSE_SNAPSHOT_PLAN.md) / [KRX_CANARY.md §"2026-05-25 휴장일 사고 + 대응"](KRX_CANARY.md) 참조.

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
  - `fallback_reason=redis_stale` (rates path) **0건** — line 886 index stale gate 제거 후 발생 X
  - `per_key_stale` 0건 — 모든 data key fresh, Redis-first 정상 통과
  - Step 3b warning(`bank/investing sync Redis SET 실패`) 0건 — 회귀 가드 OK
- **Accepted 전환 조건** (baseline 누적 후):
  - `fallback_reason=redis_stale` (rates path) 소멸 — 기대값 0 유지
  - `per_key_stale` fallback 빈도 측정 + 기준선 확보
  - topic/broadcast error 0 유지
  - DXY 별도 fallback path 영향 X 유지 (`latest_dxy_fallback_reason` 정책 그대로)

### 맥락

ADR-026 (Redis-first broadcast hot path)은 `latest:index` control key의 `mirrored_at` 필드를 broadcast Redis-first read의 단일 freshness gate로 사용한다 ([app/latest_rates_cache.py:886](app/latest_rates_cache.py#L886)). mirror cycle이 3초마다 latest:index와 모든 data key를 함께 갱신하는 모델에서는 정합한 단일 시그널이다 — "한 cycle에서 모든 key가 동일한 mirrored_at으로 갱신됨"의 invariant.

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
- [app/latest_rates_cache.py:886](app/latest_rates_cache.py#L886): 현재 freshness gate 위치

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

[app/usdt_topic_payload.py:330](app/usdt_topic_payload.py#L330):

```python
krx_futures_rate = get_latest_source_rate(db, "krx", "usd-krw-futures")  # DB query only
```

같은 파일 line 264 명시: *"KRX는 mirror skip + direct write 미구축 → DB query 유지 (별도 phase)"*. 그 phase가 본 ADR.

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

3. **Topic builder 변경** (`app/usdt_topic_payload.py:330`):

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

기존 legacy `/api/graph/{currency}` ([app/main.py:1831](app/main.py#L1831))는 다음 한계를 가진다:

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

response 각 series provenance에 `close_basis` field 추가. 4 가지 enum:

| `close_basis` | Source | 의미 |
| --- | --- | --- |
| `krx_cf_close_1545` | KRX | CF 정규장 15:45 KST close finalizer |
| `bithumb_24h_kst_close` | Bithumb | 24h candle KST 00:00 boundary close |
| `hana_observed_eod` | Hana (canonical) | 우리 DB에서 KST 해당일 24:00 이전 마지막으로 관측한 Hana 고시값 |
| `hana_official_historical_backfill` | Hana (과거 부족분만) | Hana 사이트 historical row (다음날 새벽 고시) |

같은 series 안에서 구간별로 다른 close_basis인 경우 per-point metadata로 표시 (특히 Hana의 backfill vs canonical 경계).

#### Decision B-bis — close_basis vs source_method 직교 분리

`close_basis` (의미)와 `source_method` (수집 방법)는 직교 개념. 두 field 모두 필수.

**source_method enum 5 values**:

- `observed_rollup`
- `external_backfill`
- `close_finalizer`
- `bithumb_candlestick_backfill`
- `kis_daily_backfill`

**close_basis × source_method 매핑**:

| close_basis | source_method | 시점/의미 |
| --- | --- | --- |
| `hana_observed_eod` | `observed_rollup` | 운영 중 매일 append |
| `hana_official_historical_backfill` | `external_backfill` | 초기 부족분 backfill |
| `krx_cf_close_1545` | `close_finalizer` | 운영 중 매일 append (CF close finalizer) |
| `krx_cf_close_1545` | `kis_daily_backfill` | 초기 backfill (KIS daily endpoint + A75YMM chain) |
| `bithumb_24h_kst_close` | `bithumb_candlestick_backfill` | 초기 backfill |
| `bithumb_24h_kst_close` | `observed_rollup` | 운영 중 매일 append (DB rollup) |

→ KRX/Bithumb은 close_basis 동일 but source_method 분기 (backfill vs 운영). Hana는 close_basis + source_method 둘 다 분리. provenance 측면 backfill 구간과 운영 구간 명확 식별 가능.

#### Decision C — Source별 daily canonical 정책

| Source | 초기 Backfill | 앞으로 쌓는 값 (canonical) | close_basis (canonical) |
| --- | --- | --- | --- |
| **Bithumb** | 공식 24h candle API | DB tick/source_rates → KST daily rollup | `bithumb_24h_kst_close` |
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
3. `close_basis` enum 4 values + `source_method` enum 5 values + `ohlc_quality` enum 3 values **직교 분리** (ADR-033 Amendment 후속 + 본 ADR Decision B 참조)
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
| `close_basis` | No | 4 values (§6) |
| `source_method` | No | 5 values (§6) |
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

**`close_basis` enum 4 values** (ADR-033 Amendment 후속 Decision B 참조):

- `krx_cf_close_1545`: KRX CF 정규장 15:45 KST close finalizer
- `bithumb_24h_kst_close`: Bithumb 24h candle KST 00:00 boundary close
- `hana_observed_eod`: 우리 DB에서 KST 해당일 24:00 이전 마지막으로 관측한 Hana 고시값
- `hana_official_historical_backfill`: Hana 사이트 historical row (다음날 새벽 고시, 과거 부족분 보강용)

**`source_method` enum 5 values** (ADR-033 Amendment 후속 Decision B-bis 참조):

- `observed_rollup`: DB tick/source_rates/bank_exchange_rates 기반 daily rollup
- `external_backfill`: 외부 API에서 초기 부족분 backfill (Hana official endpoint)
- `close_finalizer`: KRX CF close finalizer 결과
- `bithumb_candlestick_backfill`: Bithumb 공식 24h candle API 초기 backfill
- `kis_daily_backfill`: KIS daily endpoint + A75YMM chain 초기 backfill

**`ohlc_quality` enum 3 values** (잠정 명칭, Open):

- `source_ohlc`: source 자체 OHLC 직접 사용 (Bithumb backfill, KRX backfill)
- `observed_rollup`: 관측값 rollup 계산 (daily append jobs)
- `close_only`: representative row만 — high=low=close synthetic (Hana official backfill)

### 7. Source-specific population policy

| Source | 초기 Backfill | 매일 append (canonical) | close_basis | source_method 매핑 |
| --- | --- | --- | --- | --- |
| Bithumb | 공식 24h candle API | DB tick/source_rates → KST daily rollup | `bithumb_24h_kst_close` | backfill=`bithumb_candlestick_backfill`, append=`observed_rollup` |
| Hana | 부족한 과거만 official historical | DB의 KST 24:00 이전 마지막 관측값 | backfill=`hana_official_historical_backfill`, canonical=`hana_observed_eod` | backfill=`external_backfill`, append=`observed_rollup` |
| KRX | KIS daily + A75YMM chain | close finalizer CF 15:45 정규 종가 | `krx_cf_close_1545` | backfill=`kis_daily_backfill`, append=`close_finalizer` |

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
  - Bithumb daily append: source_rates KST 일자 rollup
  - Hana observed_eod: bank_exchange_rates KST 일자 rollup
  - KRX daily append: source_rates CF session (08:30~15:45 KST) rollup

**Hana = mixed series** (close_basis 구간별 다름):

- 앞으로 쌓는 구간: `hana_observed_eod` (close_basis) + `observed_rollup` (source_method) + `observed_rollup` ohlc_quality (또는 `close_only` fallback, Phase 2d 결정). **`source_ohlc` 불가능** — Hana는 source 자체에 OHLC 없음 (bank_exchange_rates 단일 rate값 시계열)
- 과거 부족분: `hana_official_historical_backfill` + `external_backfill` + `close_only`

**KRX/Bithumb = single close_basis series** (backfill/append 구간 모두 동일 close_basis, source_method/ohlc_quality만 분기)

### 8. Backfill jobs

**Proposed** (Phase 2d 구현 시):

- **Bithumb backfill**: `api.bithumb.com/public/candlestick/USDT_KRW/24h` 호출 → 902일 일괄 적재
- **KRX backfill**: KIS `inquire-daily-fuopchartprice` + A75YMM contract chain → 만기 종목 chain 순회. Date-to-contract mapping (ADR-033 Amendment 후속) 적용
- **Hana backfill**: Hana official endpoint historical row → 부족한 과거 구간만
- 모든 backfill = **idempotent** (재실행 시 같은 결과, §10 참조)
- Initial backfill 1회 + Gap repair 호출 시 사용

**Open**:

- Backfill 진행 monitoring (progress / completion log)
- Rate limit 처리 (Bithumb 500ms/request / KIS 100건 cap / Hana 분당 30회 보수)

### 9. Daily append jobs

**Proposed** (Phase 2d 구현 시):

- **Bithumb daily append**: KST 00:01 ~ 00:10 사이 source_rates → daily rollup → upsert
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

1. **Schema 추가**: `source_daily_rates` table 마이그레이션 + ORM model. 운영 영향 — 코드 배포 시점에 `main.py:152`의 `Base.metadata.create_all(bind=engine)`이 신규 table을 자동 생성 가능 (lock 거의 없음 / ALTER 없음 / 빈 table 추가만). 별도 `scripts/migrate_source_daily_rates.py` (`__table__.create(checkfirst=True)` idempotent pattern)은 명시적 적용/검증/audit 용도 — 유일한 적용 경로는 아니지만 운영 진입 시점 명시화에 권장
2. **Backfill dry-run**: 각 source 별 backfill job 작성 + dry-run 모드 (실제 INSERT X, log only)
3. **Source별 partial backfill**: 1 source씩 (예: KRX 먼저) 일부 date range 실측 적재 → 검증
4. **전체 backfill**: 3 source 모두 historical 적재
5. **Daily append enable**: 매일 KST 00:01 ~ 00:10 cron 활성화 (KRX는 close finalizer 직후 write)
6. **v2 endpoint switch**: catalog/tab endpoint가 source_daily_rates 조회로 전환 (Phase 2e)

각 단계는 이전 단계 검증 후 진입. 운영 영향은 6단계 (v2 endpoint switch)에서만 발생.

**Open**:

- 단계별 진입 검증 기준 (예: backfill 완료율 100% / daily append 7일 연속 성공 등)

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

- 코드 배포 시 `main.py:152` `Base.metadata.create_all(bind=engine)`이 빈 `source_daily_rates` table 자동 생성 가능 (lock 거의 없음, ALTER 없음)
- 운영 영향 거의 0 — read/write 호출자 X (helper module은 import 가능하나 사용자 없음)
- ADR-034 §14 Rollout step 1 완료. Step 2 (backfill dry-run) 진입 가능.

**후속 작업 (Step 2+ 영역)**:

- Backfill job (Step 2): Bithumb 24h candle / KIS daily chain / Hana official endpoint
- Daily append job (Step 5): close finalizer / observed_eod / source_rates KST daily rollup

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
- **Step 5 daily append**: scheduled job (close finalizer / observed_eod / source_rates rollup) — retry/backoff/structured error 함께 land
- **Step 6 v2 endpoint switch** (Phase 2e): catalog/tab endpoint가 source_daily_rates 조회로 전환

### 16. Related docs

- [ADR-033](#adr-033-graph-api-v2-catalog-policy--legacy-공존--hana-backfill--bithumbkrx-actual-only--dxy_futures-1d-only): Graph API v2 catalog policy + Amendment 2026-05-27 + Amendment 후속 (Decision A-E)
- [GRAPH_API_V2_CONTRACT.md](GRAPH_API_V2_CONTRACT.md): §3 catalog matrix / §6 close_basis schema / §7 Hana / §7-new KRX / §8 Bithumb / §13 Phase 2d rollout
- [ADR-019](#adr-019-dxy-보조지표--granularity-기반-2-part-merge-전략): DXY granularity 2-part merge (legacy comparison)
- [ADR-023](#adr-023-데이터-보관-정책-30일-통일-banksource_ratesdxy-realtime): 30일 cap 정책 (source_rates retention과 무관 — source_daily_rates는 장기 보존)

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
