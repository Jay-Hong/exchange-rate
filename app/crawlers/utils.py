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
MIBANK_DEFAULT_REQUIRED_CODES = ("USD", "JPY", "EUR")


def _extract_mibank_currency_code(row) -> Optional[str]:
    link = row.select_one('a[href*="currency="]')
    if link:
        href = link.get("href", "")
        code = parse_qs(urlparse(href).query).get("currency", [None])[0]
        if code:
            return code.upper()

    flag = row.select_one('img[src*="flag_"]')
    if not flag:
        return None
    src = flag.get("src", "")
    match = re.search(r"flag_([a-z]{3})(?:_|\.)", src, re.IGNORECASE)
    code = match.group(1) if match else None
    return code.upper() if code else None


def _get_mibank_base_rate_column_index(tbody) -> Optional[int]:
    table = tbody.find_parent("table")
    header_row = table.select_one("thead tr") if table else None
    if not header_row:
        return None

    for index, cell in enumerate(header_row.find_all(["th", "td"], recursive=False)):
        if "기준환율" in cell.get_text(" ", strip=True):
            return index
    return None


def _extract_mibank_rate_text(row, base_rate_column_index: Optional[int] = None) -> Optional[str]:
    if base_rate_column_index is not None:
        cells = row.find_all("td", recursive=False)
        if base_rate_column_index < len(cells):
            cell = cells[base_rate_column_index]
            counter = cell.select_one("span.counter")
            text = counter.get_text(strip=True) if counter else cell.get_text(strip=True)
            return text if text and text != "-" else None

    cells = []
    for selector in MIBANK_RATE_CELL_SELECTORS:
        cells = row.select(selector)
        if cells:
            break
    if not cells:
        return None
    # Last cell = base rate column on mibank
    text = cells[-1].get_text(strip=True)
    return text if text and text != "-" else None


def crawl_mibank_rates(
    url: str,
    bank_name: str,
    required_codes: tuple = MIBANK_DEFAULT_REQUIRED_CODES,
    require_all: bool = True,
) -> dict:
    """
    Crawl mibank rates using currency code from href query params.
    """
    response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    tbody = None
    for selector in MIBANK_TABLE_SELECTORS:
        tbody = soup.select_one(selector)
        if tbody:
            break
    if not tbody:
        raise RuntimeError("mibank 테이블을 찾을 수 없음")

    base_rate_column_index = _get_mibank_base_rate_column_index(tbody)
    required_set = {code.upper() for code in required_codes}
    found_codes = []
    current_rates = {}

    for row in tbody.find_all("tr"):
        code = _extract_mibank_currency_code(row)
        if not code:
            continue
        found_codes.append(code)
        if code not in required_set:
            continue

        rate_text = _extract_mibank_rate_text(row, base_rate_column_index)
        if not rate_text:
            continue
        current_rates[f"{code.lower()}-krw"] = parse_rate_text(rate_text)

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
