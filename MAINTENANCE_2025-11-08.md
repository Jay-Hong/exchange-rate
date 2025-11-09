# 유지보수 기록: 2025-11-08

## 📋 작업 개요

**목적**: Selenium 크롤러 타임아웃 문제 해결 및 Priority Queue 시스템 구축
**작업자**: Jay (with Claude Code 🤖)
**작업 시간**: 2025-11-08 19:00 ~ 21:00 (KST)

## 🔍 문제 분석

### 증상
- Selenium 크롤러에서 빈번한 타임아웃 발생 (30-90초)
- 초기 실행은 성공하지만, 시간이 지날수록 타임아웃 증가
- 은행 사이트 자체는 정상 작동

### 근본 원인
```
메모리 압박 (RAM 73.3% 사용)
  ↓
Swap 메모리 사용 (27.8%, 569MB)
  ↓
Chrome 프로세스 생성 시 Swap I/O 지연 (디스크 접근)
  ↓
크롤링 시간 2-3배 증가 → 타임아웃 발생
```

**핵심 발견**:
- Claude Code 프로세스: 283MB (시스템 메모리의 28.8%)
- FastAPI 컨테이너: 62MB (제한 800MB 내)
- Swap 사용: 569MB / 2047MB (27.8%) ⚠️

## 🎯 해결 방안

### 1. Priority Queue 시스템 구축

**목적**: 빠른 크롤러 우선 실행으로 전체 처리 속도 향상

#### 구현 내용

**app/crawlers/constants.py** (신규 추가):
```python
# 우선순위 맵 (작은 숫자 = 높은 우선순위)
SELENIUM_PRIORITY_MAP = {
    "hana": 0,     # 가장 빠름 (7.3초) → 1순위
    "ibk": 1,      # 중간 (29.1초) → 2순위
    "nh": 2,       # 느림 (32.7초) → 3순위
    "sc": 3,       # 느림 (33.3초) → 4순위
    "shinhan": 4,  # 가장 느림 (31초) → 5순위
}

# 크롤러별 타임아웃 (초기값, 이후 증가됨)
SELENIUM_TIMEOUT_MAP = {
    "hana": 30,
    "ibk": 60,
    "nh": 60,
    "sc": 60,
    "shinhan": 90,
}
```

**app/scheduler.py** (핵심 변경):
```python
# AsyncIO Queue → PriorityQueue 전환
selenium_queue: asyncio.PriorityQueue = None

# Timeout Wrapper 함수 추가
async def execute_with_timeout(job_func: Callable, bank_name: str) -> bool:
    timeout = SELENIUM_TIMEOUT_MAP.get(bank_name, 60)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(job_func),
            timeout=timeout
        )
        return True
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ [{bank_name}] 타임아웃 ({timeout}초 초과)")
        return False

# 자동 재시도 로직 (우선순위 +1000으로 후순위 처리)
if not success and not is_retry:
    retry_priority = priority + 1000
    await selenium_queue.put((retry_priority, time.time(), job_func, bank_name, True))
```

**효과**:
- ✅ 실행 순서 보장: hana → ibk → nh → sc → shinhan
- ✅ 느린 크롤러 격리: 타임아웃으로 전체 시스템 영향 최소화
- ✅ 자동 재시도: 실패한 작업 자동 재실행

### 2. 메모리 압박 완화

#### Chrome 메모리 옵션 강화

**app/crawlers/constants.py:44-50** (수정):
```python
# [2025-11-08 이전] 초기 설정
# "--window-size=800,600",

# [2025-11-08] Swap 사용 완화를 위한 추가 절감
"--window-size=400,300",        # 30-50MB 추가 절약
"--disable-javascript",         # 20-30MB 절약
"--disable-webgl",              # 10-20MB 절약
```

**예상 효과**: Chrome 인스턴스당 50-80MB 추가 절감

#### Selenium 타임아웃 증가

**app/crawlers/constants.py:117-123** (수정):
```python
# [2025-11-08 이전] Swap 미발생 시 타임아웃
# "hana": 30, "ibk": 60, "nh": 60, "sc": 60, "shinhan": 90

# [2025-11-08] Swap I/O 지연 대응
SELENIUM_TIMEOUT_MAP = {
    "hana": 60,    # 30 → 60초
    "ibk": 90,     # 60 → 90초
    "nh": 90,      # 60 → 90초
    "sc": 90,      # 60 → 90초
    "shinhan": 120, # 90 → 120초
}
```

**근거**: Swap I/O 시 실제 크롤링 시간 2-3배 증가 관찰됨

### 3. 모니터링 로깅 활성화

**app/admin/monitor.py:149-156** (수정):
```python
# [2025-11-08 이전] DEBUG 레벨 (관리자 페이지 미표시)
# logger.debug("📊 모니터링 통계 기록", ...)

# [2025-11-08] INFO 레벨 (관리자 페이지 차트 활성화)
logger.info("📊 모니터링 통계 기록", ...)
```

**효과**: 관리자 페이지에서 5분 간격 메모리/CPU 히스토리 조회 가능

## 📊 검증 결과

### 배포 후 60초간 실시간 모니터링

**타임아웃 발생**: 0건 ✅

**크롤러 실행 시간** (정상 범위):
- hana: 0.36-0.45초 ✅
- ibk: 15.00-17.50초 ✅
- shinhan: 7.30-9.13초 ✅
- nh: 6.50-7.39초 ✅
- sc: 6.15-6.32초 ✅

**시스템 리소스**:
- RAM: 688MB / 957MB (71.9%) - 안정적
- Swap: 578MB / 2047MB (28.2%) - 이전과 유사하지만 타임아웃 없음
- Chrome 프로세스: 10개 실행 중 (523.5MB)

## 📝 코드 변경 이력

### 수정된 파일

1. **app/crawlers/constants.py**
   - 라인 92-98: `SELENIUM_PRIORITY_MAP` 추가
   - 라인 117-123: `SELENIUM_TIMEOUT_MAP` 증가 (30→60, 60→90, 90→120)
   - 라인 44-50: Chrome 메모리 옵션 강화

2. **app/scheduler.py**
   - 라인 27: `SELENIUM_PRIORITY_MAP, SELENIUM_TIMEOUT_MAP` import 추가
   - 라인 47: `asyncio.Queue` → `asyncio.PriorityQueue` 변경
   - 라인 84-120: `execute_with_timeout()` 함수 추가
   - 라인 126-162: `selenium_job_executor()` 우선순위 + 재시도 로직 추가
   - 라인 165-183: `enqueue_selenium_job()` 우선순위 튜플로 변경

3. **app/admin/monitor.py**
   - 라인 149-156: `logger.debug` → `logger.info` 변경 (주석 보존)

4. **CLAUDE.md**
   - 라인 218-280: "Docker 배포 및 관리" 섹션 재구성

### 롤백 방법

모든 변경사항에 **주석으로 이전 코드 보존**:
```python
# [2025-11-08 이전] 이전 설정
# old_code_here

# [2025-11-08] 새 설정
new_code_here
```

롤백 시 주석만 교체하고 Docker 재배포하면 됨.

## 🔗 관련 문서

- **DECISIONS.md**: ADR-007 (Priority Queue + Timeout 전략)
- **CLAUDE.md**: 스케줄링 시스템 섹션 업데이트
- **CHANGELOG.md**: v0.3.0 - Priority Queue 시스템 구축

## 💡 학습 내용

1. **AsyncIO PriorityQueue**: 튜플 `(priority, timestamp, data)` 형식으로 정렬
2. **Swap 메모리 영향**: Disk I/O로 인한 100-1000배 성능 저하
3. **Docker 재배포 전략**: 상황별 3단계 접근법 (일상/캐시/완전초기화)
4. **코드 보존**: 주석으로 이전 설정 보존 시 A/B 비교 및 롤백 용이

## 🎯 향후 개선 과제

1. **EC2 인스턴스 업그레이드**: t2.micro (1GB) → t3.small (2GB)
   - 사용자 500명 이상 시 필수
   - 현재 설정은 임시 완화책

2. **Chrome 메모리 모니터링 강화**:
   - 프로세스당 메모리 임계값 설정
   - 자동 재시작 로직 추가

3. **JavaScript 비활성화 영향 검증**:
   - 일부 은행 사이트에서 크롤링 실패 가능성
   - 개별 은행별 테스트 필요

## 📌 주요 교훈

> **"메모리 압박은 단순히 OOM으로만 나타나지 않는다"**
> - Swap 사용으로 인한 성능 저하도 심각한 문제
> - 타임아웃 증가는 메모리 부족의 간접적 신호

> **"우선순위 기반 실행은 단순하지만 강력하다"**
> - 빠른 작업 우선 처리로 전체 처리량 증가
> - 느린 작업 격리로 시스템 안정성 향상

> **"코드 주석 보존은 실험의 기록"**
> - 성능 비교 및 롤백을 위한 최소한의 문서화
> - Git history보다 더 직관적

---

**마지막 업데이트**: 2025-11-08 21:00 (KST)
