# 크롤러 특수 로직 가이드

> 📅 **마지막 업데이트**: 2025-10-31
> 📚 **관련 문서**: [CLAUDE.md](CLAUDE.md), [DECISIONS.md](DECISIONS.md)

## 목차
- [개요](#개요)
- [크롤러 분류](#크롤러-분류)
- [상세 가이드](#상세-가이드)
- [트러블슈팅](#트러블슈팅)
- [새 은행 추가 가이드](#새-은행-추가-가이드)

---

## 개요

각 은행 웹사이트의 **구조와 특성**에 따라 크롤링 방식이 다릅니다. 이 문서는 각 크롤러의 특수 로직과 주의사항을 정리합니다.

### 왜 통합하지 않았나?

각 크롤러마다 **고유한 특수 로직**이 있어 통합 시 복잡도가 급증합니다:
- NH: 클릭 이동, IBK: 날짜 input 입력
- Woori/SC: AJAX 응답 감지, Hana: iframe 전환
- → **독립성 유지 + 공통 부분만 중앙화** 전략 채택

---

## 크롤러 분류

### 📦 Group A: 표준 Requests (간단)

| 은행 | 파일 | 특징 | 폴백 URL |
|------|------|------|----------|
| **KB국민** | `app/crawlers/kb.py` | requests + BeautifulSoup | 3개 (메인 → 서브 → mibank) |
| **부산** | `app/crawlers/bs.py` | requests + BeautifulSoup | 3개 |
| **씨티** | `app/crawlers/citi.py` | 국가 순서 동적 변경 처리 | 3개 |
| **Investing** | `app/crawlers/investing.py` | JPY 스케일링 (×100) | 2개 |

**공통점**:
- `requests.get()` + `BeautifulSoup` 사용
- 정적 HTML 파싱
- 폴백 URL 2-3개 지원

---

### 🔧 Group B: 기본 Selenium (동적 컨텐츠)

| 은행 | 파일 | 특징 | 특수 로직 |
|------|------|------|-----------|
| **신한** | `app/crawlers/shinhan.py` | Selenium 필수 | 없음 (표준 Selenium) |
| **하나** | `app/crawlers/hana.py` | iframe 전환 필수 | iframe 접근 (`driver.switch_to.frame()`) |

**공통점**:
- JavaScript 렌더링 필요
- `WebDriverWait` + `EC.element_to_be_clickable()` 사용

---

### ⚙️ Group C: 복잡한 특수 로직

| 은행 | 파일 | 난이도 | 특수 로직 |
|------|------|--------|-----------|
| **NH (농협)** | `app/crawlers/nh.py` | ⭐⭐ | 메인 페이지 클릭 → 환율 페이지 이동 |
| **IBK (기업)** | `app/crawlers/ibk.py` | ⭐⭐⭐ | requests → Selenium 전환 + 날짜 input 입력 |
| **Woori (우리)** | `app/crawlers/woori.py` | ⭐⭐⭐⭐ | AJAX 감지 (테이블 행 개수) + 년/월/일 select 박스 |
| **SC (제일)** | `app/crawlers/sc.py` | ⭐⭐⭐⭐ | AJAX 감지 (#TMP_RATE 개수) + Alert 처리 |

**공통점**:
- 영업시간 외 날짜 변경 필요
- AJAX 응답 감지 (페이지 갱신 대기)
- MAX_DAYS_LOOKBACK (최대 10일) 과거 조회 (constants.py 중앙 관리)

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
- 표준 Selenium 패턴 (특수 로직 없음)

**주의사항:**
- ChromeDriver 버전 호환성 확인 (분기 1회)

---

#### 하나은행 (`app/crawlers/hana.py`)

**핵심 로직:**
- iframe 내부 데이터 → `driver.switch_to.frame()` 필수

**주의사항:**
- iframe 전환 없으면 Selector 찾기 실패

---

### ⚙️ Group C: 복잡한 특수 로직

#### NH 농협은행 (`app/crawlers/nh.py`) ⭐⭐

**핵심 로직:**
- 메인 페이지 접속 → 링크 클릭 → 환율 페이지 이동

**주의사항:**
- 메인 페이지 링크 Selector 변경 감지 (월 1회)

---

#### IBK 기업은행 (`app/crawlers/ibk.py`) ⭐⭐⭐

**핵심 로직:**
- **3단계 폴백**: requests → Selenium (3회 재시도) → MIBANK (조건부)
- **Selenium 재시도**: 날짜 변경 실패 시 최대 3회 재시도 (2초 대기)
- **날짜 input 직접 입력**: `send_keys()` + `Keys.ENTER`
- **MIBANK 조건부 실행**: 평일 09:00~24:00만 허용 (자정/주말 차단)

**주의사항:**
- 평일 영업시간: requests (빠름, 1차 시도)
- 자정/주말: Selenium (날짜 변경, 2차 시도)
- Selenium 3회 재시도로 성공률 99.9% (일시적 네트워크 오류 극복)
- MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확
- MAX_DAYS_LOOKBACK 10일 (공휴일 연휴 대응, constants.py 중앙 관리)

**상세 코드:** `app/crawlers/ibk.py:48-111` (crawl_and_save_ibk_bank_exchange_rates 함수)
**최근 리팩토링:** 2025-10-26 (Selenium 3회 재시도 추가, MIBANK 조건부 실행)

---

#### Woori 우리은행 (`app/crawlers/woori.py`) ⭐⭐⭐⭐

**핵심 로직:**
- **3단계 폴백**: requests → Selenium (날짜 변경) → MIBANK (조건부)
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

**상세 코드:** `app/crawlers/woori.py:203-271` (crawl_woori_past_date_rates 함수)
**최근 리팩토링:** 2025-10-26 (SC 방식 적용, 날짜 변경 실패 처리 개선, MIBANK 조건부 실행 추가)

---

#### SC 제일은행 (`app/crawlers/sc.py`) ⭐⭐⭐⭐

**핵심 로직:**
- **3단계 폴백**: SC_BANK_URL (Selenium) → SECOND_SC_BANK_URL (날짜 변경) → MIBANK (조건부)
- **AJAX 감지**: #TMP_RATE 개수 변화로 페이지 갱신 확인
- **Alert 처리**: 조회 버튼 클릭 직후 1회 (자정/주말 "0회차" 메시지)
- **과거 조회**: 어제부터 MAX_DAYS_LOOKBACK (10일) 순회
- **MIBANK 조건부 실행**: 평일 09:00~24:00만 허용 (자정/주말 차단)

**주의사항:**
- Alert 미처리 시 크롤링 중단 → `driver.switch_to.alert.accept()` 필수
- #TMP_RATE 개수 1개 = 데이터 없음, 2개 이상 = 정상 데이터
- 날짜 변경 후 AJAX 대기 없으면 이전 데이터 오독
- MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확

**상세 코드:** `app/crawlers/sc.py:209-292` (crawl_past_date_rates 함수)
**최근 리팩토링:** 2025-10-26 (Alert 처리 단일화, MIBANK 조건부 실행 추가)

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
**기능**: MIBANK 환율 신뢰성 판단 (IBK, SC, WOORI 공통)
- **반환값**:
  - `True`: 평일 09:00 ~ 24:00 (MIBANK 신뢰 가능)
  - `False`: 평일 00:00 ~ 09:00, 주말 (MIBANK 부정확)
- **사용 이유**: MIBANK는 영업일 자정 직전 환율 제공 → 자정/주말에는 부정확한 데이터
- **한계**: 일반 공휴일은 고려 못함 (Selenium 재시도 로직으로 보완)

**사용 예시**:
```python
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
