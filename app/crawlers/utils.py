# app/crawlers/utils.py

"""
크롤러 공통 함수

모든 크롤러에서 사용하는 공통 함수들
- 환율 텍스트 파싱
- Selenium 드라이버 생성
- Selenium 세마포어 (동시 실행 제어)
"""

# 표준 라이브러리
import logging
import re
from contextlib import contextmanager
from datetime import timezone as dt_timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

# 로컬 애플리케이션
from app.crawlers.constants import (
    DEFAULT_TIMEOUT,
    HEADERS,
    SELENIUM_DRIVER_TIMEOUT,
    SELENIUM_OPTIONS,
)

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler")

# ═════════════════════════════════════════════════════════════
# Selenium 순차 실행 (AsyncIO Queue 기반)
# ═════════════════════════════════════════════════════════════
# 변경 사항 (2025-11-06):
# - Semaphore 제거 → AsyncIO Queue로 완전 대체
# - scheduler.py의 selenium_job_executor()가 1개씩 순차 처리
# - 경합 없음 → 불필요한 Fallback 제거, 메모리 안정
# ─────────────────────────────────────────────────────────────


def parse_rate_text(rate_text: str) -> float:
    """
    환율 텍스트 파싱 (쉼표 제거 + float 변환)

    Args:
        rate_text: 환율 텍스트 (예: "1,340.50")

    Returns:
        환율 숫자 (예: 1340.5)

    Raises:
        ValueError: 숫자로 변환할 수 없는 경우

    Examples:
        >>> parse_rate_text("1,340.50")
        1340.5
    """
    cleaned = rate_text.strip().replace(',', '')
    return float(cleaned)


def create_selenium_driver():
    """
    표준 Selenium Chrome 드라이버 생성 (Google Chrome 사용)

    Returns:
        webdriver.Chrome 인스턴스

    Notes:
        - Headless 모드로 실행
        - Docker: Google Chrome (AMD64 최적화)
        - 로컬: webdriver-manager 자동 설치
        - SELENIUM_OPTIONS 상수에서 옵션 로드

    Examples:
        >>> driver = create_selenium_driver()
        >>> driver.get("https://example.com")
        >>> driver.quit()
    """
    import os

    options = webdriver.ChromeOptions()

    # Chrome 바이너리 경로 자동 감지
    chrome_bin = os.getenv('CHROME_BIN')
    if not chrome_bin:
        # 환경변수 없으면 자동 탐색 (Google Chrome 우선)
        for path in ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/google-chrome-stable']:
            if os.path.exists(path):
                chrome_bin = path
                break

    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin

    # 옵션 추가
    for arg in SELENIUM_OPTIONS:
        options.add_argument(arg)

    # ChromeDriver 경로 자동 감지
    chromedriver_path = os.getenv('CHROMEDRIVER_PATH')
    if not chromedriver_path:
        # 환경변수 없으면 자동 탐색
        for path in ['/usr/local/bin/chromedriver', '/usr/bin/chromedriver']:
            if os.path.exists(path):
                chromedriver_path = path
                break

    # Service 생성
    if chromedriver_path and os.path.exists(chromedriver_path):
        service = Service(chromedriver_path)
    else:
        # 로컬 개발 환경 폴백 (webdriver-manager 사용)
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
        except ImportError:
            # webdriver-manager 없으면 기본 경로
            service = Service()

    return webdriver.Chrome(service=service, options=options)


@contextmanager
def selenium_driver_context():
    """
    Selenium 드라이버 컨텍스트 매니저 (AsyncIO Queue 기반 순차 실행)

    드라이버 생성/종료를 자동으로 관리합니다.
    동시 실행 제어는 scheduler.py의 selenium_job_executor()가 담당합니다.

    Usage:
        with selenium_driver_context() as driver:
            driver.get("https://example.com")
            element = driver.find_element(By.ID, "rate")
            rate = element.text

    Yields:
        webdriver.Chrome: Selenium Chrome 드라이버 인스턴스

    Notes:
        - AsyncIO Queue가 순차 처리 보장 (Semaphore 불필요)
        - driver.quit() 자동 호출 (메모리 누수 방지)
        - 좀비 프로세스 강제 종료 로직 포함
    """
    driver = None
    try:
        # 드라이버 생성
        driver = create_selenium_driver()

        # Timeout 설정 (모든 Selenium 크롤러에 자동 적용)
        # ⛔ 숫자를 여기 다시 적지 않는다 — 상수는 42초인데 주석만 60초로 남아 있었고,
        #    그 주석을 근거로 예산을 계산할 뻔했다(2026-09-09). 값은 상수가 단일 정의다.
        driver.set_page_load_timeout(SELENIUM_DRIVER_TIMEOUT)  # 페이지 로드 제한
        driver.set_script_timeout(SELENIUM_DRIVER_TIMEOUT)     # 스크립트 실행 제한

        logger.debug(f"✅ Selenium 드라이버 생성 성공 (timeout {SELENIUM_DRIVER_TIMEOUT}초)")
        yield driver

    except Exception as e:
        # 드라이버 생성 실패 또는 크롤링 에러
        logger.error("❌ Selenium 드라이버 에러", exc_info=True)
        raise

    finally:
        # 항상 실행: 드라이버 종료
        if driver:
            try:
                driver.quit()
                logger.debug("✅ Selenium 드라이버 종료 완료")
            except Exception as e:
                logger.error("⚠️ Selenium 드라이버 종료 실패, 강제 종료 시도", exc_info=True)

                # driver.quit() 실패 시 Chrome 프로세스 강제 종료 (좀비 프로세스 방지)
                try:
                    # 함수 내부 import (psutil은 무거운 라이브러리, 에러 시에만 로드)
                    import psutil
                    import os

                    current_process = psutil.Process(os.getpid())
                    killed_count = 0

                    # 현재 Python 프로세스의 모든 자식 프로세스 중 Chrome 관련 프로세스 강제 종료
                    for child in current_process.children(recursive=True):
                        try:
                            if any(name in child.name().lower() for name in ['chrome', 'chromedriver']):
                                child.kill()
                                child.wait(timeout=3)  # 종료 대기 (최대 3초)
                                killed_count += 1
                                logger.warning(f"🔨 좀비 프로세스 강제 종료: {child.name()} (PID: {child.pid})")
                        except psutil.TimeoutExpired:
                            # 3초 내 종료 안 되면 강제 종료
                            child.kill()
                            logger.warning(f"⚡ 좀비 프로세스 강제 kill: {child.name()} (PID: {child.pid})")
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            # 이미 종료되었거나 권한 없음
                            pass

                    if killed_count > 0:
                        logger.info(f"🧹 강제 종료 완료: {killed_count}개 프로세스")
                except Exception as cleanup_error:
                    logger.error("⚠️ 프로세스 강제 종료 실패", exc_info=True)


def is_mibank_rate_reliable() -> bool:
    """
    MIBANK 환율의 신뢰성 판단 (시간대별 조건부 실행)

    MIBANK는 평일 자정 이후 및 주말에는 영업일 마지막 환율(자정 직전)을
    제공하므로, 해당 시간대에는 부정확한 데이터로 간주합니다.

    Returns:
        True: 평일 10:00 ~ 23:59 (MIBANK 환율 신뢰 가능)
        False: 평일 00:00 ~ 09:59, 주말 (MIBANK 환율 부정확)

    Notes:
        - 주말이 아닌 일반 공휴일은 고려하지 못함
        - BS, CITI, IBK, WOORI 크롤러에서 사용 (4개)
        - KB, HANA, SC, SHINHAN, NH는 조건 없이 항상 mibank 시도 (5개)
        - Selenium 재시도 로직과 조합하여 공휴일 대응

    Examples:
        >>> # 평일 10:00
        >>> is_mibank_rate_reliable()
        True
        >>> # 토요일 14:00
        >>> is_mibank_rate_reliable()
        False
    """
    import datetime
    from pytz import timezone

    now = datetime.datetime.now(timezone('Asia/Seoul'))
    weekday = now.weekday()  # 월=0, 화=1 ... 일=6
    hour = now.hour

    return (weekday in [0, 1, 2, 3, 4] and hour > 9)


MIBANK_TABLE_SELECTORS = (
    "div.box_contents1 table tbody",
    "table.main_table.content tbody",
)
MIBANK_RATE_CELL_SELECTORS = (
    "td.right.counter.rollsty01",
    "span.counter",
)
# 행 안 통화 판별·헤더·값 칸 조회. fixture 캡처 도구가 선택자 등록부를 코드 상수에서 도출하므로 인라인 문자열로 두지 않는다.
MIBANK_CODE_LINK_SELECTOR = 'a[href*="currency="]'
MIBANK_FLAG_IMAGE_SELECTOR = 'img[src*="flag_"]'
MIBANK_HEADER_ROW_SELECTOR = "thead tr"
MIBANK_COUNTER_SELECTOR = "span.counter"
MIBANK_DEFAULT_REQUIRED_CODES = ("USD", "JPY", "EUR")


def _mibank_currency_code_with_basis(row) -> tuple:
    """(통화 코드, 근거). 근거는 실제로 쓴 분기 — `explicit_code_param` / `flag_filename` / None.

    ⚠️ 근거는 **통화 축**만 채운다 — 필드(기준환율)·단위 근거가 아니다.

    ⛔ 판별 로직은 여기 한 곳이다. 보고는 이 결과를 그대로 받고 다시 계산하지 않는다.
    """
    link = row.select_one(MIBANK_CODE_LINK_SELECTOR)
    if link:
        href = link.get("href", "")
        code = parse_qs(urlparse(href).query).get("currency", [None])[0]
        if code:
            return code.upper(), "explicit_code_param"

    flag = row.select_one(MIBANK_FLAG_IMAGE_SELECTOR)
    if not flag:
        return None, None
    src = flag.get("src", "")
    match = re.search(r"flag_([a-z]{3})(?:_|\.)", src, re.IGNORECASE)
    code = match.group(1) if match else None
    return (code.upper(), "flag_filename") if code else (None, None)


def _extract_mibank_currency_code(row) -> Optional[str]:
    return _mibank_currency_code_with_basis(row)[0]


def _mibank_base_rate_column_with_basis(tbody) -> tuple:
    """(기준환율 열 인덱스, 근거). 근거는 `label_found` / `header_row_absent` / `label_not_found` —
    헤더 자체가 없는 것과 라벨을 못 찾은 것은 다르다."""
    table = tbody.find_parent("table")
    header_row = table.select_one(MIBANK_HEADER_ROW_SELECTOR) if table else None
    if not header_row:
        return None, "header_row_absent"

    for index, cell in enumerate(header_row.find_all(["th", "td"], recursive=False)):
        if "기준환율" in cell.get_text(" ", strip=True):
            return index, "label_found"
    return None, "label_not_found"


def _get_mibank_base_rate_column_index(tbody) -> Optional[int]:
    return _mibank_base_rate_column_with_basis(tbody)[0]


def _mibank_rate_text_with_basis(row, base_rate_column_index: Optional[int] = None) -> tuple:
    """(환율 텍스트, 근거). 근거는 실제로 탄 분기다.

    ① `header_index`: 헤더 인덱스 칸을 썼다 — ⛔ 그 칸이 비었거나 `-` 여도 **폴백하지 않고** None 이다.
    ② `fallback` + `row_cells_insufficient`: 헤더 인덱스는 있으나 이 행의 `td` 가 모자란다.
    ③ `fallback` + `column_index_unresolved`: 기준환율 열 인덱스를 확보하지 못했다.
    폴백은 `MIBANK_RATE_CELL_SELECTORS` 중 처음 매칭된 selector 의 **마지막 요소**를 쓴다(마지막 `td` 가 아니다).
    """
    if base_rate_column_index is not None:
        cells = row.find_all("td", recursive=False)
        if base_rate_column_index < len(cells):
            cell = cells[base_rate_column_index]
            counter = cell.select_one(MIBANK_COUNTER_SELECTOR)
            text = counter.get_text(strip=True) if counter else cell.get_text(strip=True)
            basis = {"branch": "header_index", "column_index": base_rate_column_index,
                     "row_cell_count": len(cells), "used_counter_span": counter is not None}
            return (text if text and text != "-" else None), basis
        reason = "row_cells_insufficient"
    else:
        reason = "column_index_unresolved"

    cells = []
    used_selector = None
    for selector in MIBANK_RATE_CELL_SELECTORS:
        cells = row.select(selector)
        if cells:
            used_selector = selector
            break
    basis = {"branch": "fallback", "reason": reason, "selector": used_selector,
             "matched_count": len(cells)}
    if not cells:
        return None, basis
    # Last cell = base rate column on mibank
    text = cells[-1].get_text(strip=True)
    return (text if text and text != "-" else None), basis


def _extract_mibank_rate_text(row, base_rate_column_index: Optional[int] = None) -> Optional[str]:
    return _mibank_rate_text_with_basis(row, base_rate_column_index)[0]


def crawl_mibank_rates(
    url: str,
    bank_name: str,
    required_codes: tuple = MIBANK_DEFAULT_REQUIRED_CODES,
    require_all: bool = True,
    observer=None,
) -> dict:
    """
    Crawl mibank rates using currency code from href query params.

    `observer` 는 보고 전용(`bank_report.PathObserver`)이다. 기본 None 이면 보고 호출·추가 DOM 탐색이 없다
    (판별 함수는 근거 튜플을 늘 함께 돌려준다 — 판별 로직을 두 벌 두지 않기 위해서다).
    ⛔ 은행 이름으로 활성화를 판단하지 않는다 — 관측자를 넘긴 호출만 기록한다.
    """
    response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    current_rates, found_codes = extract_mibank_rates(soup, required_codes, _mibank_observer_events(observer))

    logger.debug(
        "mibank 수집 통화",
        extra={"bank": bank_name, "found": found_codes, "captured": list(current_rates.keys())},
    )

    if require_all:
        missing = [
            f"{code.lower()}-krw"
            for code in required_codes
            if f"{code.lower()}-krw" not in current_rates
        ]
        if missing:
            raise RuntimeError(f"mibank 필수 통화 누락: {missing}")

    return current_rates


def _mibank_observer_events(observer):
    """`extract_mibank_rates` 사건 → 보고 관측자 호출(기존 호출·인자 그대로). 관측자가 없으면 아무것도 하지 않는다."""
    def on_event(kind, **facts):
        if observer is None:
            return
        if kind == "table_structure":
            observer.table_structure(**facts)
        elif kind == "code_outside_required":
            observer.code_outside_required(facts["code"], facts["code_basis"])
        elif kind == "empty_value":
            observer.missed(facts["pair"], "empty_value", item_key=facts["row_index"], row=facts["row"],
                            code_basis=facts["code_basis"], value_basis=facts["value_basis"])
        elif kind == "parse_error":
            observer.missed(facts["pair"], "parse_error", rate_text=facts["rate_text"], item_key=facts["row_index"],
                            row=facts["row"], code_basis=facts["code_basis"], value_basis=facts["value_basis"])
        elif kind == "observed":
            observer.observed(facts["pair"], rate_text=facts["rate_text"], rate=facts["rate"],
                              item_key=facts["row_index"], row=facts["row"], matched_code=facts["code"],
                              code_basis=facts["code_basis"], value_basis=facts["value_basis"])
        elif kind == "loop_completed":
            observer.loop_completed()
    return on_event


def extract_mibank_rates(soup, required_codes, on_event) -> tuple:
    """받은 MIBANK 페이지(soup)에서 필수 통화 값만 뽑는다 — 요청·DB·Redis 없음. (값, 발견 코드 순서) 를 돌려준다.

    사건마다 `on_event(kind, **facts)` 를 **그 자리에서** 부른다 — 운영은 보고 관측자 호출을, fixture 캡처 도구는 완전 기록을 둔다.
    표 없음은 `RuntimeError`, 값 칸 파싱 실패는 사건을 남긴 뒤 `ValueError` 를 그대로 전파한다(기존과 같다).
    필수 통화 누락 판정은 호출자 몫이다(운영에서는 디버그 로그 뒤에 검사한다).
    """
    tbody = None
    for selector in MIBANK_TABLE_SELECTORS:
        tbody = soup.select_one(selector)
        if tbody:
            break
    if not tbody:
        raise RuntimeError("mibank 테이블을 찾을 수 없음")

    base_rate_column_index, column_basis = _mibank_base_rate_column_with_basis(tbody)
    on_event("table_structure", tbody=tbody, column_index=base_rate_column_index, column_basis=column_basis)
    required_set = {code.upper() for code in required_codes}
    found_codes = []
    current_rates = {}

    for row_index, row in enumerate(tbody.find_all("tr")):
        code, code_basis = _mibank_currency_code_with_basis(row)
        if not code:
            continue
        found_codes.append(code)
        if code not in required_set:
            # 필수 밖 코드는 값을 파싱하지 않는다 — 식별 사실만 남긴다.
            on_event("code_outside_required", code=code, code_basis=code_basis)
            continue

        rate_text, value_basis = _mibank_rate_text_with_basis(row, base_rate_column_index)
        pair = f"{code.lower()}-krw"
        facts = {"pair": pair, "row_index": row_index, "row": row, "code_basis": code_basis,
                 "value_basis": value_basis}
        if not rate_text:
            on_event("empty_value", **facts)
            continue
        try:
            current_rates[pair] = parse_rate_text(rate_text)
        except ValueError:
            # 잡지 않고 전파한다 — 어느 행에서 멈췄는지만 사건으로 남긴다.
            on_event("parse_error", rate_text=rate_text, **facts)
            raise
        # 값이 `current_rates` 에 들어간 직후 — 같은 코드의 뒤 행이 덮어쓰면 그 행도 따로 남는다.
        on_event("observed", rate_text=rate_text, rate=current_rates[pair], code=code, **facts)

    on_event("loop_completed")
    return current_rates, found_codes


def selector_routine_events(logger, bank_name, observer):
    """`extract_selector_rates` 사건 → 기존 공식 루틴의 경고 로그와 보고 관측자 호출(문구·extra·순서 그대로).

    ⛔ 파싱 실패 문자열을 로그에 남기는 것은 운영 루틴의 기존 동작이다 — fixture 캡처 도구는 이 함수를 쓰지 않는다.
    """
    def on_event(kind, **facts):
        pair, selector = facts.get("pair"), facts.get("selector")
        if kind == "selector_miss":
            logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": bank_name})
            if observer is not None:
                observer.missed(pair, "selector_miss", selector=selector)
        elif kind == "parse_error":
            logger.warning(f"⚠️ 유효하지 않은 환율: {pair}",
                           extra={"pair": pair, "rate_text": facts["rate_text"], "bank": bank_name})
            if observer is not None:
                observer.missed(pair, "parse_error", selector=selector, rate_text=facts["rate_text"])
        elif kind == "observed":
            if observer is not None:
                observer.observed(pair, rate_text=facts["rate_text"], rate=facts["rate"],
                                  selector=selector, element=facts["element"])
        elif kind == "loop_completed":
            if observer is not None:
                observer.loop_completed()
    return on_event


def extract_selector_rates(soup, selectors, on_event) -> dict:
    """통화별 선택자로 값 칸을 찾아 파싱한다(bs 공식·citi 2차) — 요청·DB·Redis 없음.

    사건마다 `on_event(kind, **facts)` 를 그 자리에서 부른다: `selector_miss` / `parse_error`(건너뜀) / `observed` / 끝에
    `loop_completed`. 그 밖의 예외는 그대로 전파한다.
    """
    current_rates = {}
    for pair, selector in selectors.items():
        rate_element = soup.select_one(selector)
        if not rate_element:
            on_event("selector_miss", pair=pair, selector=selector)
            continue

        rate_text = rate_element.get_text(strip=True)
        try:
            current_rate = parse_rate_text(rate_text)
            current_rates[pair] = current_rate
        except ValueError:
            on_event("parse_error", pair=pair, selector=selector, rate_text=rate_text)
            continue
        on_event("observed", pair=pair, selector=selector, rate_text=rate_text, rate=current_rate,
                 element=rate_element)

    on_event("loop_completed")
    return current_rates


def validate_rate_ranges(rates: dict, ranges: dict):
    """
    Validate rates are within absolute range.
    """
    for pair, rate in rates.items():
        if pair not in ranges:
            continue
        low, high = ranges[pair]
        if rate < low or rate > high:
            raise ValueError(f"환율 범위 초과: {pair}={rate} ({low}~{high})")


def get_dynamic_thresholds(minutes_gap: float) -> tuple:
    if minutes_gap <= 10:
        return 0.08, 0.20
    if minutes_gap <= 60:
        return 0.12, 0.25
    if minutes_gap <= 180:
        return 0.20, 0.30
    return 0.30, 0.40


def evaluate_rate_deviation(rates: dict, last_rates_info: dict, now):
    """
    Returns dict with soft/hard fail flags and details.
    """
    soft_fail = False
    hard_fail = False
    details = {}
    utc = dt_timezone.utc
    now_utc = now.replace(tzinfo=utc) if now.tzinfo is None else now.astimezone(utc)

    for pair, rate in rates.items():
        last_info = last_rates_info.get(pair, {})
        prev_rate = last_info.get("rate")
        prev_ts = last_info.get("timestamp")

        if not prev_rate or not prev_ts:
            continue

        # naive datetime은 UTC로 해석
        if prev_ts.tzinfo is None:
            prev_ts = prev_ts.replace(tzinfo=utc)
        else:
            prev_ts = prev_ts.astimezone(utc)

        gap_minutes = (now_utc - prev_ts).total_seconds() / 60
        soft_thr, hard_thr = get_dynamic_thresholds(gap_minutes)
        pct_change = abs(rate - prev_rate) / prev_rate

        details[pair] = {
            "prev": prev_rate,
            "now": rate,
            "pct": pct_change,
            "gap_min": gap_minutes,
            "soft_thr": soft_thr,
            "hard_thr": hard_thr,
        }

        if pct_change > soft_thr:
            soft_fail = True
        if pct_change > hard_thr:
            hard_fail = True

    return {"soft_fail": soft_fail, "hard_fail": hard_fail, "details": details}
