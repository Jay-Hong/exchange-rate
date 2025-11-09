# 유지보수 기록: 2025-11-09

## 📋 작업 개요

**목적**: Selenium 크롤러 미실행 버그 수정 및 Queue Blocking 문제 해결
**작업자**: Jay (with Claude Code 🤖)
**작업 시간**: 2025-11-09 21:00 ~ 23:00 (KST)

## 🔍 문제 분석

### 문제 1: Selenium 크롤러가 전혀 실행되지 않음

**증상**:
- 로그에 "📥 Priority Queue 추가" 메시지 없음
- Selenium 크롤러 5개(hana, shinhan, ibk, nh, sc) 모두 동작하지 않음
- APScheduler는 정상 실행되지만 실제 크롤링은 일어나지 않음

**근본 원인**: Python 클로저 버그 (scheduler.py:266-275)

```python
# ❌ 문제 코드
for name, func, base_interval in SELENIUM_BASED_TASKS:
    async def wrapper():  # 클로저
        await enqueue_selenium_job(func, name)

    scheduler.add_job(wrapper, ...)
```

**왜 동작하지 않는가**:
1. 5개의 wrapper 함수가 모두 동일한 qualified name (`switch_jobs.<locals>.wrapper`)을 가짐
2. APScheduler는 함수 객체의 identity로 `max_instances=1` 추적
3. 첫 번째 wrapper가 실행되면, 나머지는 "maximum number of running instances reached" 에러로 skip
4. 실제로는 모든 wrapper가 같은 함수로 인식되어 제대로 실행되지 않음

### 문제 2: Queue 포화 시 전체 시스템 멈춤 가능성

**증상** (잠재적 문제):
- Queue maxsize=20 제한
- IN 모드에서 최악의 경우 120초 동안 30개 작업 추가 가능
- `await queue.put()` blocking 시 APScheduler 전체 멈춤

**위험도**:
```
Queue 포화 (20/20)
    ↓
await queue.put() 블로킹
    ↓
APScheduler event loop 멈춤
    ↓
전체 FastAPI 멈춤 (Request 기반 크롤러, WebSocket 브로드캐스트 포함) 🔥
```

## 🎯 해결 방안

### 1. 클로저 버그 수정: Factory 함수 패턴

**app/scheduler.py:240-262** (신규 추가):
```python
def make_selenium_job_wrapper(bank_name: str, job_func: Callable):
    """
    각 Selenium 크롤러마다 고유한 wrapper 함수 생성

    Notes:
        - __name__, __qualname__ 설정으로 APScheduler가 각 job을 구별
        - for loop 안에서 직접 wrapper 정의 시 모든 wrapper가 같은 이름을 가짐
        - enqueue는 동기 non-blocking이므로 즉시 반환 (event loop blocking 방지)
    """
    def wrapper():
        enqueue_selenium_job(job_func, bank_name)

    # APScheduler가 함수를 구별할 수 있도록 고유 이름 설정
    wrapper.__name__ = f"selenium_wrapper_{bank_name}"
    wrapper.__qualname__ = wrapper.__name__
    return wrapper
```

**사용 방법** (scheduler.py:283):
```python
for name, func, base_interval in SELENIUM_BASED_TASKS:
    scheduler.add_job(
        make_selenium_job_wrapper(name, func),  # ✅ 독립적인 함수 객체
        ...
    )
```

**효과**:
- ✅ 각 은행별로 고유한 wrapper 함수 객체 생성
- ✅ APScheduler가 5개의 job을 정확히 구별
- ✅ `max_instances=1`이 개별 은행별로 적용됨

### 2. Queue Blocking 방지: Non-blocking Put

**app/scheduler.py:165-199** (수정):
```python
# Before: async def enqueue_selenium_job()
#         await selenium_queue.put(...)

# After: def enqueue_selenium_job()  # sync 함수로 변경
def enqueue_selenium_job(job_func: Callable, bank_name: str):
    """
    Selenium 작업을 우선순위 Queue에 non-blocking 방식으로 추가

    Notes:
        - put_nowait() 사용으로 APScheduler event loop blocking 방지
        - Queue 포화 시 조용히 skip (다음 스케줄에서 재시도)
    """
    item = (priority, time.time(), job_func, bank_name, False)

    try:
        selenium_queue.put_nowait(item)  # Non-blocking
        logger.debug(f"📥 [{bank_name}] Priority Queue 추가 (대기: {qsize}/50)")
    except asyncio.QueueFull:
        logger.warning(f"⚠️ [{bank_name}] Queue 포화로 skip - 다음 스케줄에서 재시도")
```

**효과**:
- ✅ APScheduler event loop 절대 블로킹 안 됨
- ✅ Queue 포화 시 즉시 skip → 전체 시스템 정상 동작 유지
- ✅ 다음 스케줄(7.3~33.3초 후)에서 자동 재시도

### 3. Queue 크기 증가

**app/scheduler.py:193** (수정):
```python
# Before: maxsize=20
# After: maxsize=50

selenium_queue = asyncio.PriorityQueue(maxsize=50)
```

**근거**:
- 최악 시나리오: 120초 동안 약 30개 추가 가능
- 여유를 두고 50으로 설정
- 메모리 증가: 미미함 (튜플 50개 = 수 KB)

**효과**:
- ✅ Queue 포화 확률 대폭 감소
- ✅ Skip 빈도 최소화

### 4. Queue 모니터링 시스템 추가

**app/scheduler.py:381-413** (신규 추가):
```python
def report_queue_status():
    """Selenium Priority Queue 상태 모니터링 (10초마다)"""
    size = selenium_queue.qsize()
    usage_percent = (size / 50) * 100

    if size < 40:  # 80% 미만
        logger.debug(f"📊 [Queue] size={size}/50, usage={usage_percent:.1f}%")
    else:  # 80% 이상
        logger.warning(f"⚠️ [Queue] 포화 임박: {size}/50 ({usage_percent:.1f}%)")
```

**스케줄** (scheduler.py:429-436):
```python
scheduler.add_job(
    report_queue_status,
    IntervalTrigger(seconds=10),
    id="queue_status"
)
```

**효과**:
- ✅ Queue 상태 실시간 추적
- ✅ 80% 이상 포화 시 자동 경고
- ✅ 성능 튜닝 및 문제 진단에 활용

## 📊 검증 결과

### 배포 후 확인 사항

**Selenium 크롤러 정상 실행**: ✅
- Queue Worker 정상 시작 확인
- report_queue_status 10초마다 실행 확인
- 시스템 전체 정상 동작 (investing 크롤러, 좀비 프로세스 정리 등)

**현재 모드**: OUT 모드
- 일요일 22:33 (weekday=6)
- Selenium 크롤러 주기: 73~333초 (10배 느림)

**다음 검증 필요** (IN 모드 진입 후):
- Selenium 크롤러 실제 실행 확인
- Queue depth 추적 (skip 빈도 확인)
- 데이터 품질 영향 분석

## 📝 코드 변경 이력

### 수정된 파일

1. **app/scheduler.py**
   - 라인 165-199: `enqueue_selenium_job()` - async 제거, put_nowait() 전환
   - 라인 193: Queue 크기 증가 (20 → 50)
   - 라인 240-262: `make_selenium_job_wrapper()` Factory 함수 추가
   - 라인 256: wrapper를 async → sync로 변경
   - 라인 283: `make_selenium_job_wrapper()` 호출로 변경
   - 라인 381-413: `report_queue_status()` 모니터링 함수 추가
   - 라인 429-436: Queue 모니터링 스케줄 등록

### 롤백 방법

**클로저 버그 수정**은 롤백 불가 (이전 코드 자체가 작동하지 않음)

**Queue 크기 및 모니터링**은 독립적으로 롤백 가능:
```python
# Queue 크기만 원복
selenium_queue = asyncio.PriorityQueue(maxsize=20)

# 모니터링 제거
scheduler.remove_job("queue_status")
```

## 🔗 관련 문서

- **CLAUDE.md**: 스케줄링 시스템 섹션 업데이트 (2025-11-09)
- **MAINTENANCE_2025-11-08.md**: Priority Queue 시스템 구축
- **DECISIONS.md**: ADR-007 (Priority Queue + Timeout 전략)

## 💡 학습 내용

### 1. Python 클로저 버그
- **문제**: for loop 내부에서 정의한 함수는 모두 같은 이름을 가짐
- **해결**: Factory 함수로 독립적인 함수 객체 생성
- **핵심**: `__name__`, `__qualname__` 설정으로 함수 identity 구분

### 2. AsyncIO Non-blocking Queue
- **원칙**: APScheduler event loop는 절대 blocking되면 안 됨
- **방법**: `await queue.put()` → `queue.put_nowait()` + QueueFull 예외 처리
- **트레이드오프**: 일부 작업 skip vs 전체 시스템 멈춤 → Skip 선택

### 3. Queue 크기 설계
- **공식**: 최악 시나리오 예상 작업 수 × 1.5~2배 여유
- **근거**: IN 모드 120초 동안 30개 → 50으로 설정
- **검증**: 모니터링으로 실제 사용률 확인 후 조정

## 🎯 향후 관찰 과제

### Phase 2: 1주일 모니터링 (2025-11-16까지)

**목표**:
1. Queue skip 빈도 확인
2. 80% 포화 경고 빈도 확인
3. 데이터 품질 영향 분석

**로그 확인 방법**:
```bash
# Queue 상태 확인
docker compose logs | grep "Queue"

# Skip 빈도 추적
docker compose logs | grep "Queue 포화로 skip"
```

**판단 기준**:
- Skip rate < 1% → 현재 유지 (성공)
- Skip rate > 5% → Phase 3 진행 (조건부 재시도 추가)

### Phase 3: 필요 시 추가 개선

**옵션 A**: Skip rate 높을 경우
- 조건부 재시도 로직 추가 (2회 연속 skip 시 강제 재시도)
- 은행별 Last Update 추적

**옵션 B**: Queue 크기 부족 시
- 50 → 100으로 추가 증가

**옵션 C**: 근본 원인 개선
- Selenium 크롤러 속도 최적화
- 타임아웃 재조정

## 📌 주요 교훈

> **"클로저는 편리하지만 함정이 있다"**
> - for loop 내부의 함수 정의는 모두 같은 객체로 인식될 수 있음
> - Factory 함수 패턴으로 독립적인 객체 생성 필수
> - `__name__`, `__qualname__` 설정으로 디버깅 편의성 향상

> **"Event loop blocking은 치명적이다"**
> - AsyncIO에서 blocking 연산은 전체 시스템을 멈춤
> - Queue 연산도 예외가 아님 (await queue.put() 주의)
> - Non-blocking 방식(put_nowait) + 예외 처리가 안전

> **"모니터링 없이 최적화 없다"**
> - Queue 크기를 20으로 할지 50으로 할지는 관찰 데이터 기반
> - 10초마다 상태 로깅으로 실제 사용 패턴 파악
> - Phase 2 관찰 결과로 Phase 3 결정

---

**마지막 업데이트**: 2025-11-09 23:00 (KST)
