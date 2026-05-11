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
| REST 성공 시 저장 위치 | `source_rates`에 insert-if-changed / topic snapshot만 갱신 | DB 저장까지 수행해 provenance 유지. topic snapshot/publish 경로는 ADR-028 기반 Phase Z-2에서 확정 |
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
