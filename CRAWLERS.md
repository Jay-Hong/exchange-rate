# 크롤러 특수 로직 가이드

> 📅 **마지막 업데이트**: 2025-10-25
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
- MAX_DAYS_LOOKBACK (최대 12일) 과거 조회

---

## 상세 가이드

### 📦 Group A: 표준 Requests

#### KB국민은행 (`app/crawlers/kb.py`)

**특징**:
- 3개 폴백 URL (obank 2개 + mibank 1개)
- requests만으로 크롤링 가능

**주요 코드**:
```python
# 폴백 전략 (51-67줄)
try:
    crawl_and_save_routine(KB_BANK_URL, KB_BANK_SELECTORS, db)
except:
    try:
        crawl_and_save_routine(SECOND_KB_BANK_URL, KB_BANK_SELECTORS, db)
    except:
        crawl_and_save_routine(MIBANK_KB_URL, MIBANK_SELECTORS, db)
```

**주의사항**:
- 메인 URL 실패 시 자동으로 폴백 URL 시도
- Selector가 주기적으로 변경될 수 있음 (은행 웹사이트 개편 시)

---

#### 씨티은행 (`app/crawlers/citi.py`)

**특징**:
- **국가 순서가 동적으로 변경됨** (USD, JPY, EUR 위치가 매번 바뀜)
- 문자열 검색으로 통화 매칭

**주요 코드**:
```python
# 통화 매칭 (94-107줄)
for index, currency in enumerate(CURRENCY_TEXTS):  # ['USD', 'JPY', 'EUR']
    if currency in item.get_text():  # 문자열 검색
        rate_element = item.select_one(AFTER_CITI_BANK_SELECTORS)
        current_rate = parse_rate_text(rate_text)
        current_rates[PAIRS[index]] = current_rate  # 올바른 pair에 할당
```

**주의사항**:
- Selector 순서에 의존하지 않고 **문자열 검색** 사용
- 국가명(USD, JPY, EUR)이 변경되면 크롤링 실패

---

#### Investing.com (`app/crawlers/investing.py`)

**특징**:
- JPY-KRW는 **100엔당 원화**로 표시 (스케일링 필요)

**주요 코드**:
```python
# JPY 스케일링 (77-80줄)
current_rate = parse_rate_text(rate_text)
if pair in SCALED_CURRENCY_PAIRS:  # {"jpy-krw": 100}
    current_rate *= SCALED_CURRENCY_PAIRS[pair]
current_rates[pair] = current_rate
```

**주의사항**:
- JPY만 100배 스케일링 (1엔 → 100엔)
- 다른 통화 추가 시 스케일링 여부 확인 필요

---

### 🔧 Group B: 기본 Selenium

#### 신한은행 (`app/crawlers/shinhan.py`)

**특징**:
- JavaScript 렌더링 필요 (정적 HTML 불가)
- 표준 Selenium 패턴 (특수 로직 없음)

**주요 코드**:
```python
# 표준 Selenium 패턴 (78-98줄)
driver = create_selenium_driver()
driver.get(url)
wait = WebDriverWait(driver, 10)

for pair, selector in selectors.items():
    rate_element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
    rate_text = rate_element.text.strip()
    current_rate = parse_rate_text(rate_text)
```

**주의사항**:
- 폴백 URL도 모두 Selenium 사용
- ChromeDriver 버전 호환성 확인 필요

---

#### 하나은행 (`app/crawlers/hana.py`)

**특징**:
- **iframe 내부에 환율 데이터** 존재
- iframe 전환 필수

**주요 코드**:
```python
# iframe 전환 (139-140줄)
iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
driver.switch_to.frame(iframe)  # ⚠️ 중요: iframe 전환 후 크롤링

# 이후 표준 Selenium 패턴
for pair, selector in selectors.items():
    rate_element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
```

**주의사항**:
- iframe 전환 없이 크롤링 시 Selector 찾지 못함
- 메인 URL은 requests, 폴백 URL은 Selenium + iframe

---

### ⚙️ Group C: 복잡한 특수 로직

#### NH 농협은행 (`app/crawlers/nh.py`) ⭐⭐

**특징**:
- **메인 페이지 접속 → 클릭 → 환율 페이지 이동**
- 직접 환율 페이지 URL 접근 불가 (환율 정보 미제공)

**주요 코드**:
```python
# 클릭 이동 (86-91줄)
driver.get(NH_BANK_URL)  # 메인 페이지 접속
wait = WebDriverWait(driver, 5)

# 환율 페이지 링크 클릭
element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, NH_MAIN_TO_EXCHANGE_RATES_PAGE)))
element.click()  # ⚠️ 클릭 후 환율 페이지 로드

# 이후 표준 Selenium 패턴
for pair, selector in selectors.items():
    rate_element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
```

**주의사항**:
- 메인 페이지 구조 변경 시 Selector 업데이트 필요
- 클릭 후 페이지 로드 대기 시간 조정 가능 (현재 5초)

---

#### IBK 기업은행 (`app/crawlers/ibk.py`) ⭐⭐⭐

**특징**:
- **하이브리드 전략**: requests 먼저 시도 → 실패 시 Selenium
- 자정/주말: 날짜를 **input 태그에 직접 입력** (Keys.ENTER)

**주요 코드**:
```python
# 하이브리드 전략 (64-71줄)
if try_crawl_with_requests(db):  # 1차: requests (빠른 경로)
    logger.debug(f"✅ {BANK_NAME} Requests 크롤링 성공")
    return

# 2차: Selenium (날짜 변경 필요)
logger.info(f"➡️ {BANK_NAME} Selenium으로 전환 (환율 데이터 없음)")
crawl_and_save_ibk_routine_selenium(IBK_BANK_URL, IBK_BANK_SELECTORS, db)

# 날짜 변경 (171-179줄)
selected_date = selected_date - datetime.timedelta(days=1)
input_element.clear()
input_element.send_keys(selected_date.strftime('%Y.%m.%d'))  # ⚠️ 직접 입력
input_element.send_keys(Keys.ENTER)  # ⚠️ Enter로 제출
```

**주의사항**:
- 평일 영업시간: requests로 빠르게 크롤링
- 자정/주말: Selenium으로 전환 (날짜 변경 필요)
- MAX_DAYS_LOOKBACK (12일) 제한

---

#### Woori 우리은행 (`app/crawlers/woori.py`) ⭐⭐⭐⭐

**특징**:
- **AJAX 응답 감지**: 테이블 행 개수 변화로 페이지 갱신 확인
- 날짜 선택: **년/월/일 select 박스** 각각 선택

**주요 코드**:
```python
# 데이터 유무 판단 (154-165줄)
tr_elements = driver.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr')
row_count = len(tr_elements)

if row_count > 1:  # 행이 2개 이상이면 데이터 있음
    current_rates = crawl_woori_current_date_rates(driver, wait, selectors)
else:
    # 과거 날짜 조회
    current_rates = crawl_woori_past_date_rates(driver, wait, selectors)

# 년/월/일 select 박스 (258-270줄)
year_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, YEAR_SELECTOR))))
month_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, MONTH_SELECTOR))))
day_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, DAY_SELECTOR))))

year_select.select_by_value(str(selected_date.year))
month_select.select_by_value(f"{selected_date.month:02d}")
day_select.select_by_value(f"{selected_date.day:02d}")

# AJAX 응답 감지 (272-283줄)
old_row_count = len(driver.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr'))
submit_button.click()

# ⚠️ 테이블 행 개수 변화 대기 (AJAX 응답 완료 확인)
WebDriverWait(driver, 3).until(
    lambda d: len(d.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr')) != old_row_count
)
```

**주의사항**:
- 테이블 행 개수로 데이터 유무 판단 (1개=헤더만, 2개 이상=데이터 있음)
- AJAX 응답 대기 없이 크롤링 시 이전 데이터 읽을 수 있음
- select 박스의 value 형식 변경 시 크롤링 실패 (현재: "2025", "01", "01")

---

#### SC 제일은행 (`app/crawlers/sc.py`) ⭐⭐⭐⭐

**특징**:
- **AJAX 응답 감지**: #TMP_RATE 개수로 데이터 유무 판단
- **Alert 처리**: 자정/주말 메시지 자동 수락

**주요 코드**:
```python
# Alert 처리 (112-118줄)
try:
    alert = driver.switch_to.alert
    alert_text = alert.text
    logger.debug(f"Alert 감지: {alert_text}")
    alert.accept()  # ⚠️ Alert 수락
except Exception:
    pass  # Alert 없으면 무시

# #TMP_RATE 개수로 데이터 유무 판단 (122-132줄)
rate_elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
element_count = len(rate_elements)

if element_count > 1:  # 여러 개 = 평일 영업시간
    current_rates = crawl_current_date_rates(driver, wait, selector)
else:  # 1개만 = 자정/주말
    current_rates = crawl_past_date_rates(driver, wait, selector)

# AJAX 응답 감지 (239-250줄)
old_count = len(driver.find_elements(By.CSS_SELECTOR, selector))
submit_button.click()

# ⚠️ #TMP_RATE 개수 변화 대기 (AJAX 응답 완료 확인)
WebDriverWait(driver, 3).until(
    lambda d: len(d.find_elements(By.CSS_SELECTOR, selector)) != old_count
)
```

**주의사항**:
- Alert 미처리 시 크롤링 중단
- #TMP_RATE가 1개만 있을 때는 의미 없는 값 (무시 필요)
- AJAX 응답 대기 없이 크롤링 시 이전 데이터 읽을 수 있음

---

## 트러블슈팅

### 🔍 일반적인 문제

#### 1. Selector 오류 (가장 빈번)

**증상**:
```
⚠️ SELECTOR 오류: usd-krw
```

**원인**:
- 은행 웹사이트 구조 변경 (가장 흔함)
- 폴백 URL의 Selector가 메인 URL과 다름

**해결**:
1. 브라우저 개발자 도구에서 새 Selector 확인
2. 크롤러 파일의 `SELECTORS` 딕셔너리 업데이트
3. 폴백 URL의 Selector도 확인 필요

#### 2. Selenium Timeout

**증상**:
```
selenium.common.exceptions.TimeoutException
```

**원인**:
- 페이지 로딩이 느림
- JavaScript 렌더링 지연
- Selector가 잘못됨

**해결**:
```python
# SELENIUM_WAIT_TIMEOUT 증가 (현재 5초)
wait = WebDriverWait(driver, 10)  # 5초 → 10초
```

#### 3. 날짜 변경 실패 (IBK, Woori, SC)

**증상**:
```
⚠️ 날짜 변경 실패
```

**원인**:
- select 박스 value 형식 변경
- input 태그 속성 변경
- AJAX 응답 대기 시간 부족

**해결**:
1. select 박스 value 형식 확인 (년: "2025", 월: "01", 일: "01")
2. AJAX 대기 시간 증가 (3초 → 5초)
3. 브라우저 개발자 도구로 네트워크 탭 확인

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
    ("investing", investing.crawl_and_save_investing_exchange_rates, 4.4),
    ("kb", kb.crawl_and_save_kb_bank_exchange_rates, 7.9),
    # ... 기존 은행들
    ("<은행코드>", <은행코드>.crawl_and_save_<은행코드>_bank_exchange_rates, <주기>),  # ⬅️ 추가
]
```

### 5️⃣ 테스트

```bash
# 1. Import 테스트
cd /Users/jay/Downloads/Projects/FXi/F06_GitHub
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

**마지막 업데이트**: 2025-10-25
**작성자**: Claude Code
**버전**: 1.0
