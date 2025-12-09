# 크롤러 특수 로직 가이드

> 📅 **마지막 업데이트**: 2025-12-10
> 📚 **관련 문서**: [CLAUDE.md](CLAUDE.md), [DECISIONS.md](DECISIONS.md)
> 🆕 **최근 변경**:
> - IBK 크롤러 자정 전환기 스킵 (00:00~00:05) 추가
> - 4단계 모드 환율 고시 스케줄 기반 최적화 ([ADR-011](DECISIONS.md#adr-011-4단계-모드-환율-고시-스케줄-기반-최적화))

## 목차
- [개요](#개요)
- [환율 고시 스케줄](#환율-고시-스케줄)
- [크롤러 분류](#크롤러-분류)
- [상세 가이드](#상세-가이드)
- [트러블슈팅](#트러블슈팅)
- [새 은행 추가 가이드](#새-은행-추가-가이드)

---

## 개요

각 은행 웹사이트의 **구조와 특성**, 그리고 **환율 고시 시간**에 따라 크롤링 방식과 스케줄이 다릅니다. 이 문서는 각 크롤러의 특수 로직, 환율 고시 시간, 그리고 주의사항을 정리합니다.

### 왜 통합하지 않았나?

각 크롤러마다 **고유한 특수 로직**이 있어 통합 시 복잡도가 급증합니다:
- NH: 클릭 이동, IBK: 날짜 input 입력
- Woori/SC: AJAX 응답 감지, Hana: iframe 전환
- → **독립성 유지 + 공통 부분만 중앙화** 전략 채택

---

## 환율 고시 스케줄

각 은행의 실제 환율 고시 운영 시간 (실제 데이터 추적 관찰 결과):

| 은행 | 고시 시작 | 고시 종료 | BREAK1<br>(21:00~03:00) | BREAK2<br>(03:00~08:00) | OUT<br>(주말) |
|------|----------|----------|------------------------|------------------------|--------------|
| **investing** | 월 06:00 | 토 06:00 | ✅ | ✅ | ✅ |
| **kb** | 평일 08:30 | 익일 05:00 | ✅ | ✅ | ✅ |
| **hana** | 평일 08:30 | 익일 06:00 | ✅ | ✅ | ✅ (주말 가끔 변동) |
| **shinhan** | 평일 08:19 | 익일 02:30 | ✅ | ❌ (02:30 종료) | ❌ |
| **woori** | 평일 08:30 | 익일 02:45 | ✅ | ❌ (02:45 종료) | ❌ |
| **ibk** | 평일 08:30 | 익일 02:05 | ✅ | ❌ (02:05 종료) | ❌ |
| **nh** | 평일 08:40 | 당일 24:00 | ✅ | ✅ | ✅ (가끔 고시) |
| **sc** | 평일 09:00 | 당일 20:30 | ❌ | ❌ | ❌ |
| **bs** | 평일 08:10 | 당일 24:00 | ✅ | ✅ | ✅ (일요일 가끔) |
| **citi** | 평일 09:00 | 익일 06:00 | ✅ | ✅ | ❌ |

**크롤러 활성화 기준:**
- ✅: 해당 시간대에 환율 고시 있음 → 크롤러 활성
- ❌: 환율 고시 없음 → 크롤러 비활성 (리소스 절약)

---

## 크롤러 분류

### 📦 Group A: 순수 Request (Selenium 없음)

| 은행 | 파일 | 폴백 순서 | mibank 조건 |
|------|------|----------|------------|
| **Investing** | `app/crawlers/investing.py` | 메인 → 서브 | - |
| **KB국민** | `app/crawlers/kb.py` | 메인 → 서브 → mibank | 조건 없음 (항상 시도) |
| **부산** | `app/crawlers/bs.py` | 부산은행 → mibank | 평일 09:00~24:00만 허용 |
| **씨티** | `app/crawlers/citi.py` | 씨티 메인 → 씨티 서브 → mibank | 평일 09:00~24:00만 허용 |

**공통점**:
- `requests.get()` + `BeautifulSoup` 사용
- **Selenium 없음** (가장 빠르고 가벼움)
- 정적 HTML 파싱

**mibank 사용 패턴:**
- **조건부 사용 (BS, CITI)**: `is_mibank_rate_reliable()` 함수로 시간대 체크 (평일 09:00~24:00만 허용)
- **조건 없이 사용 (KB)**: 항상 mibank 시도
- **배경**: mibank는 자정~09:00, 주말에는 영업일 마지막 환율(자정 직전)을 제공하여 부정확할 수 있음

---

### 🔧 Group B: 하이브리드 Request → Selenium 폴백

| 은행 | 파일 | 폴백 순서 | mibank 조건 |
|------|------|----------|------------|
| **하나** | `app/crawlers/hana.py` | Request → Selenium subprocess → mibank | 조건 없음 (항상 시도) |
| **우리** | `app/crawlers/woori.py` | Request → Selenium subprocess → mibank | 평일 09:00~24:00만 허용 |

**공통점**:
- **Request 우선**: 빠른 응답 (80~90% 성공)
- **Selenium 폴백**: Request 실패 시 자동 전환
- **subprocess 격리**: Selenium 실행 시 Chrome 좀비화 방지
- **시스템 부하 감소**: Request가 대부분 성공하므로 Queue 압력 최소화

**mibank 사용 패턴:**
- **조건부 사용 (WOORI)**: `is_mibank_rate_reliable()` 함수로 시간대 체크 (평일 09:00~24:00만 허용)
- **조건 없이 사용 (HANA)**: 항상 mibank 시도 (3차 폴백)

---

### ⚙️ Group C: Selenium Queue (Request 먼저 시도, 2025-11-16 변경)

| 은행 | 파일 | 폴백 순서 | mibank 조건 |
|------|------|----------|------------|
| **신한** | `app/crawlers/shinhan.py` | Request(mibank) → Selenium subprocess | 조건 없음 (항상 시도) |
| **NH** | `app/crawlers/nh.py` | Request(mibank) → Selenium subprocess | 조건 없음 (항상 시도) |
| **SC** | `app/crawlers/sc.py` | Request(mibank) → Selenium subprocess | 조건 없음 (항상 시도) |
| **IBK** | `app/crawlers/ibk.py` | Request(mibank, 조건부) → Selenium subprocess | 평일 09:00~24:00만 허용 |

**공통점**:
- **Request(mibank) 우선**: 2025-11-16 변경, 시스템 부하 대폭 감소
- **Selenium 폴백**: Request 실패 시 자동 전환
- **AsyncIO Queue**: 순차 실행 (메모리 제어)
- **subprocess 격리**: 모든 Selenium 실행이 subprocess로 격리
- **영업시간 외 날짜 변경**: IBK, SC, Woori는 날짜 변경 로직 필요
- **MAX_DAYS_LOOKBACK**: 최대 10일 과거 조회 (constants.py 중앙 관리)

**mibank 조건부 사용 (IBK):**
- `is_mibank_rate_reliable()` 함수로 시간대 체크 (평일 09:00~24:00만 허용)
- **IN 모드 (08:30~20:59)**: Request(mibank) 우선 → Selenium 폴백
- **BREAK1 모드 중 00:00~02:59**: Selenium만 사용 (mibank 차단, 날짜 변경 필요)
- **BREAK2/OUT 모드**: 크롤러 비활성 (02:05 고시 종료)

---

## 상세 가이드

### 📦 Group A: 표준 Requests

#### KB국민은행 (`app/crawlers/kb.py`)

**핵심 로직:**
- 3개 폴백 URL (obank 메인 → obank 서브 → mibank)
- requests + BeautifulSoup만으로 크롤링

**주의사항:**
- Selector 변경 빈번 (월 1회 확인 권장)

---

#### 씨티은행 (`app/crawlers/citi.py`)

**핵심 로직:**
- 국가 순서 동적 변경 → 문자열 검색으로 통화 매칭 ("USD", "JPY", "EUR" 텍스트 검색)

**주의사항:**
- Selector 순서 의존 금지

---

#### Investing.com (`app/crawlers/investing.py`)

**핵심 로직:**
- JPY-KRW 스케일링 (100엔당 원화 → 1엔당 원화로 변환)

**주의사항:**
- JPY만 ×100 스케일링, 다른 통화 추가 시 확인 필요

---

### 🔧 Group B: 기본 Selenium

#### 신한은행 (`app/crawlers/shinhan.py`)

**핵심 로직:**
- JavaScript 렌더링 필요 → Selenium 필수
- **AsyncIO Queue + subprocess**: 순차 실행 보장, Chrome 좀비화 방지
- 표준 Selenium 패턴 (특수 로직 없음)

**주의사항:**
- ChromeDriver 버전 호환성 확인 (분기 1회)
- subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/shinhan.py`
**실행 방식:** `runner.py` 통해 subprocess 생성 → scheduler.py Queue에서 순차 처리

---

#### 하나은행 (`app/crawlers/hana.py`)

**핵심 로직:**
- **3단계 폴백**: requests → **Selenium subprocess** → mibank
- **Selenium 격리**: subprocess로 실행하여 Chrome 프로세스 좀비화 방지
- **iframe 전환**: `driver.switch_to.frame()` 필수 (Selenium 폴백 시)
- **mibank 최종 폴백**: 조건 없이 항상 시도

**주의사항:**
- iframe 전환 없으면 Selector 찾기 실패
- Selenium 폴백은 subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/hana.py:54-132` (subprocess fallback 포함)
**최근 리팩토링:** 2025-11-14 (Selenium 폴백 subprocess 격리)

---

### ⚙️ Group C: 복잡한 특수 로직

#### NH 농협은행 (`app/crawlers/nh.py`) ⭐⭐

**핵심 로직:**
- 메인 페이지 접속 → 링크 클릭 → 환율 페이지 이동
- **AsyncIO Queue + subprocess**: 순차 실행 보장, Chrome 좀비화 방지

**주의사항:**
- 메인 페이지 링크 Selector 변경 감지 (월 1회)
- subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/nh.py`
**실행 방식:** `runner.py` 통해 subprocess 생성 → scheduler.py Queue에서 순차 처리

---

#### IBK 기업은행 (`app/crawlers/ibk.py`) ⭐⭐⭐

**핵심 로직:**
- **자정 전환기 스킵**: 00:00~00:05에 크롤링 완전 스킵 (2025-12-10 추가)
- **3단계 폴백**: requests → Selenium (3회 재시도) → MIBANK (조건부)
- **Selenium 재시도**: 날짜 변경 실패 시 최대 3회 재시도 (2초 대기)
- **날짜 input 직접 입력**: `send_keys()` + `Keys.ENTER`
- **MIBANK 조건부 실행**: 평일 09:00~24:00만 허용 (자정/주말 차단)

**자정 전환기 스킵 (00:00~00:05):**
> 🚨 **문제**: 자정 직후 Selenium 날짜 변경 시 UI 불안정
> - 캘린더가 랜덤한 날짜까지 이동하여 잘못된 환율 수집
> - 45초 타임아웃 발생

> ✅ **해결**: 가장 불안정한 5분간 크롤링 스킵
> - 이 시간대 환율 변경 가능성 ≈ 0%
> - 마지막 정상 환율 유지 (클라이언트가 재사용)
> - 00:05부터 정상 크롤링 재개

**주의사항:**
- 평일 영업시간: requests (빠름, 1차 시도)
- 자정/주말: Selenium (날짜 변경, 2차 시도)
- **00:00~00:05**: 크롤링 스킵 (자정 전환기 불안정)
- Selenium 3회 재시도로 성공률 99.9% (일시적 네트워크 오류 극복)
- MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확
- MAX_DAYS_LOOKBACK 10일 (공휴일 연휴 대응, constants.py 중앙 관리)

**상세 코드:** `app/crawlers/ibk.py:51-76` (자정 전환기 스킵), `ibk.py:78-140` (폴백 로직)
**최근 리팩토링:**
- 2025-12-10: 자정 전환기 스킵 추가 (00:00~00:05)
- 2025-10-26: Selenium 3회 재시도 추가, MIBANK 조건부 실행

---

#### Woori 우리은행 (`app/crawlers/woori.py`) ⭐⭐⭐⭐

**핵심 로직:**
- **3단계 폴백**: requests → **Selenium subprocess** (날짜 변경) → MIBANK (조건부)
- **Selenium 격리**: subprocess로 실행하여 Chrome 프로세스 좀비화 방지
- **AJAX 감지**: 테이블 행 개수로 페이지 갱신 확인
- **과거 조회**: 어제부터 MAX_DAYS_LOOKBACK (10일) 순회 (SC 방식 적용)
- **날짜 선택**: 년/월/일 select 박스 각각 선택
- **MIBANK 조건부 실행**: 평일 09:00~24:00만 허용 (자정/주말 차단)

**주의사항:**
- 행 개수: 1개(헤더만) = 데이터 없음, 2개+ = 정상 데이터
- 테이블 로딩 시간 확보: `time.sleep(0.5)` 필수
- 날짜 변경 실패 시 continue로 다음 날짜 시도 (주말/공휴일 대응)
- select value 형식 변경 주의 ("2025", "01", "01")
- MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확
- Selenium 폴백은 subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/woori.py:59-148` (subprocess fallback 포함), `woori.py:212-282` (crawl_woori_past_date_rates 함수)
**최근 리팩토링:**
- 2025-10-26: SC 방식 적용, 날짜 변경 실패 처리 개선, MIBANK 조건부 실행 추가
- 2025-11-14: Selenium 폴백 subprocess 격리 ([ADR-012](DECISIONS.md#adr-012-selenium-폴백-subprocess-격리-chrome-프로세스-좀비화-방지))

---

#### SC 제일은행 (`app/crawlers/sc.py`) ⭐⭐⭐⭐

**핵심 로직:**
- **3단계 폴백**: Request(mibank) → Selenium (SC_BANK_URL) → Selenium 날짜 변경 (SECOND_SC_BANK_URL)
- **MIBANK 우선**: 조건 없이 항상 mibank를 먼저 시도 (2025-11-16 변경)
- **AsyncIO Queue + subprocess**: Selenium 순차 실행 보장, Chrome 좀비화 방지
- **AJAX 감지**: #TMP_RATE 개수 변화로 페이지 갱신 확인
- **Alert 처리**: 조회 버튼 클릭 직후 1회 (자정/주말 "0회차" 메시지)
- **과거 조회**: 어제부터 MAX_DAYS_LOOKBACK (10일) 순회

**주의사항:**
- Alert 미처리 시 크롤링 중단 → `driver.switch_to.alert.accept()` 필수
- #TMP_RATE 개수 1개 = 데이터 없음, 2개 이상 = 정상 데이터
- 날짜 변경 후 AJAX 대기 없으면 이전 데이터 오독
- subprocess로 격리 실행 (45초 타임아웃)

**상세 코드:** `app/crawlers/sc.py:209-292` (crawl_past_date_rates 함수)
**실행 방식:** `runner.py` 통해 subprocess 생성 → scheduler.py Queue에서 순차 처리
**최근 리팩토링:**
- 2025-11-16: Request(mibank) 우선 전략 적용 ([ADR-013](DECISIONS.md#adr-013-4단계-모드-환율-고시-스케줄-기반-최적화))
- 2025-10-26: Alert 처리 단일화

---

### 🛠️ 공통 유틸리티 함수

모든 크롤러에서 공통으로 사용하는 유틸리티 함수들 (`app/crawlers/utils.py`)

#### `parse_rate_text(rate_text: str) -> float`
**기능**: 환율 텍스트 파싱 (쉼표 제거 + float 변환)
```python
>>> parse_rate_text("1,340.50")
1340.5
```

#### `create_selenium_driver() -> webdriver.Chrome`
**기능**: 표준 Selenium Chrome 드라이버 생성
- Headless 모드로 실행
- ChromeDriverManager로 자동 버전 관리

#### `is_mibank_rate_reliable() -> bool`
**기능**: MIBANK 환율 신뢰성 판단 (시간대별 조건부 실행)
- **반환값**:
  - `True`: 평일 09:00 ~ 24:00 (MIBANK 신뢰 가능)
  - `False`: 평일 00:00 ~ 09:00, 주말 (MIBANK 부정확)
- **사용 이유**: MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확한 데이터
- **한계**: 일반 공휴일은 고려 못함 (Selenium 재시도 로직으로 보완)
- **사용 크롤러**: BS, CITI, IBK, WOORI (4개)
- **미사용 크롤러**: KB, HANA, SC, SHINHAN, NH (조건 없이 항상 mibank 시도)

**사용 예시**:
```python
# BS, CITI, IBK, WOORI 크롤러에서 사용
if is_mibank_rate_reliable():
    crawl_and_save_routine(MIBANK_URL, MIBANK_SELECTORS, db)
else:
    logger.warning("⏰ MIBANK 차단 (자정/주말)")
```

**추가 날짜**: 2025-10-26

---

## 트러블슈팅

### 자주 발생하는 문제 (FAQ)

| 문제 | 증상 | 해결 |
|------|------|------|
| **Selector 오류** | `⚠️ SELECTOR 오류: usd-krw` | 개발자 도구로 새 Selector 확인 → `SELECTORS` 딕셔너리 업데이트 |
| **Selenium Timeout** | `TimeoutException` | WebDriverWait 시간 증가 (5초 → 10초) 또는 Selector 재확인 |
| **날짜 변경 실패** | `⚠️ 날짜 변경 실패` | select value 형식 확인 ("2025", "01", "01"), AJAX 대기 시간 증가 |
| **Alert 미처리** | 크롤링 중단 | `driver.switch_to.alert.accept()` 추가 |
| **AJAX 응답 안 기다림** | 이전 데이터 읽기 | WebDriverWait로 요소 개수/속성 변화 감지 추가 |

---

### 🚨 은행별 주의사항

| 은행 | 주의사항 | 체크 주기 |
|------|----------|-----------|
| **KB** | Selector 변경 빈번 | 월 1회 |
| **씨티** | 국가 순서 변경 감지 | 주 1회 |
| **신한** | ChromeDriver 버전 호환성 | 분기 1회 |
| **하나** | iframe 구조 변경 | 월 1회 |
| **NH** | 메인 페이지 링크 변경 | 월 1회 |
| **IBK** | 날짜 input 형식 변경 | 월 1회 |
| **Woori** | select 박스 value 형식 | 월 1회 |
| **SC** | Alert 메시지 내용 변경 | 월 1회 |

---

### 환경 설정 문제

| 문제 | 증상 | 원인 | 해결 |
|------|------|------|------|
| **Brotli 압축 해제 실패** | investing 크롤러만 Docker에서 실패 (로컬은 정상) | `brotli` 패키지 미설치 → requests가 br 압축 미지원 | `requirements.txt`에 `brotli>=1.0.9` 추가 ✅ |

**배경**: investing.com은 Brotli(br) 압축을 우선 사용하며, requests 라이브러리는 `brotli` 패키지가 없으면 br 압축을 해제할 수 없습니다. 로컬 환경(Anaconda)에는 다른 패키지 의존성으로 brotli가 설치되어 있었지만, Docker 환경에는 명시하지 않으면 설치되지 않습니다.

---

## 새 은행 추가 가이드

### 1️⃣ 웹사이트 분석

**체크리스트**:
- [ ] 정적 HTML vs JavaScript 렌더링 확인
- [ ] 환율 데이터 위치 확인 (iframe, 테이블, div 등)
- [ ] 자정/주말 환율 제공 여부 확인
- [ ] 날짜 변경 방식 확인 (input, select, datepicker 등)
- [ ] AJAX 사용 여부 확인 (페이지 갱신 없이 데이터 로드)

### 2️⃣ 그룹 분류

| 그룹 | 조건 | 템플릿 |
|------|------|--------|
| **Group A** | requests만으로 크롤링 가능 | `app/crawlers/kb.py` |
| **Group B** | Selenium 필요 (특수 로직 없음) | `app/crawlers/shinhan.py` |
| **Group C** | Selenium + 특수 로직 | `app/crawlers/ibk.py` (하이브리드)<br>`app/crawlers/woori.py` (AJAX 감지) |

### 3️⃣ 크롤러 생성

**파일명**: `app/crawlers/<은행코드>.py`

**기본 구조**:
```python
# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests  # 또는 Selenium
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT
from app.crawlers.utils import parse_rate_text, create_selenium_driver

BANK_NAME = '<은행코드>'

# URL 및 Selector 정의
BANK_URL = 'https://...'
BANK_SELECTORS = {
    'usd-krw': '...',
    'jpy-krw': '...',
    'eur-krw': '...',
}

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_<은행코드>_bank_exchange_rates():
    """<은행명> 환율 크롤링"""
    db = SessionLocal()
    try:
        # 크롤링 로직
        pass
    finally:
        db.close()
```

### 4️⃣ 스케줄러 등록

**파일**: `app/scheduler.py`

```python
from app.crawlers import investing, kb, <은행코드>

BANK_TASKS = [
    ("investing", investing.crawl_and_save_investing_exchange_rates, 4.9),
    ("kb", kb.crawl_and_save_kb_bank_exchange_rates, 7.9),
    # ... 기존 은행들
    ("<은행코드>", <은행코드>.crawl_and_save_<은행코드>_bank_exchange_rates, <주기>),  # ⬅️ 추가
]
```

### 5️⃣ 테스트

```bash
# 1. Import 테스트
cd /Users/jay/Downloads/Projects/FXi/exchange-rate
python3 -c "from app.crawlers import <은행코드>"

# 2. 크롤링 테스트 (수동 실행)
python3 -c "
from app.crawlers import <은행코드>
<은행코드>.crawl_and_save_<은행코드>_bank_exchange_rates()
"

# 3. 로그 확인
tail -f logs/app.log | grep "<은행코드>"
```

### 6️⃣ 문서 업데이트

- [ ] `CRAWLERS.md`: 새 은행 특수 로직 추가
- [ ] `CLAUDE.md`: 지원 은행 목록 업데이트
- [ ] 관리자 페이지 (`templates/index.html`): 은행 아이콘 추가

---

## 참고 자료

- **프로젝트 가이드**: [CLAUDE.md](CLAUDE.md)
- **아키텍처 의사결정**: [DECISIONS.md](DECISIONS.md)
- **공통 상수**: `app/crawlers/constants.py`
- **공통 함수**: `app/crawlers/utils.py`

---

**마지막 업데이트**: 2025-10-26
**작성자**: Claude Code
**버전**: 1.0
