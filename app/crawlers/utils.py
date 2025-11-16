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
from contextlib import contextmanager

# 서드파티 라이브러리
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

# 로컬 애플리케이션
from app.crawlers.constants import SELENIUM_OPTIONS, SELENIUM_DRIVER_TIMEOUT

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
        driver.set_page_load_timeout(SELENIUM_DRIVER_TIMEOUT)  # 페이지 로드 60초
        driver.set_script_timeout(SELENIUM_DRIVER_TIMEOUT)     # 스크립트 실행 60초

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
        True: 평일 09:00 ~ 24:00 (MIBANK 환율 신뢰 가능)
        False: 평일 00:00 ~ 09:00, 주말 (MIBANK 환율 부정확)

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
