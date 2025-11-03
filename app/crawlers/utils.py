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
from threading import Semaphore

# 서드파티 라이브러리
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

# 로컬 애플리케이션
from app.crawlers.constants import SELENIUM_OPTIONS, SELENIUM_DRIVER_TIMEOUT

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler")

# ═════════════════════════════════════════════════════════════
# Selenium 동시 실행 제어 (AWS 프리티어 메모리 최적화)
# ═════════════════════════════════════════════════════════════
# 글로벌 세마포어: 최대 2개의 Selenium 드라이버만 동시 실행
# - 4개 동시 실행 시: 280-360MB
# - 2개 제한 시: 140-180MB (50% 절약)
# ─────────────────────────────────────────────────────────────
_selenium_semaphore = Semaphore(2)


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
    Selenium 드라이버 컨텍스트 매니저 (세마포어 자동 관리)

    최대 2개의 Selenium 드라이버만 동시 실행을 보장하며,
    세마포어 획득/해제와 드라이버 생성/종료를 자동으로 관리합니다.

    Usage:
        try:
            with selenium_driver_context() as driver:
                driver.get("https://example.com")
                element = driver.find_element(By.ID, "rate")
                rate = element.text
        except RuntimeError:
            # 세마포어 획득 실패 → Request 폴백으로
            logger.warning("Selenium 사용 불가, Request 폴백")
        except Exception as e:
            # 크롤링 실패
            logger.error("Selenium 크롤링 실패", exc_info=True)

    Yields:
        webdriver.Chrome: Selenium Chrome 드라이버 인스턴스

    Raises:
        RuntimeError: 세마포어 획득 실패 (이미 2개 실행 중)

    Notes:
        - 세마포어는 논블로킹 모드로 획득 시도
        - 획득 실패 시 즉시 RuntimeError 발생 (대기하지 않음)
        - 정상 종료/에러 발생 모두 자동으로 세마포어 해제
        - driver.quit() 자동 호출 (메모리 누수 방지)
    """
    # 세마포어 획득 시도 (논블로킹)
    acquired = _selenium_semaphore.acquire(blocking=False)
    if not acquired:
        logger.warning(
            "⏸️ Selenium 세마포어 획득 실패 (최대 2개 실행 중)",
            extra={"max_instances": 2}
        )
        raise RuntimeError("Selenium semaphore unavailable - max 2 instances running")

    driver = None
    try:
        # 드라이버 생성
        driver = create_selenium_driver()

        # Timeout 설정 (모든 Selenium 크롤러에 자동 적용)
        driver.set_page_load_timeout(SELENIUM_DRIVER_TIMEOUT)  # 페이지 로드 60초
        driver.set_script_timeout(SELENIUM_DRIVER_TIMEOUT)     # 스크립트 실행 60초

        logger.debug("✅ Selenium 드라이버 생성 성공 (세마포어 획득, timeout 60초)")
        yield driver

    except Exception as e:
        # 드라이버 생성 실패 또는 크롤링 에러
        logger.error(
            "❌ Selenium 드라이버 에러",
            exc_info=True
        )
        raise

    finally:
        # 항상 실행: 드라이버 종료 + 세마포어 해제
        if driver:
            try:
                driver.quit()
                logger.debug("✅ Selenium 드라이버 종료 완료")
            except Exception as e:
                logger.error("⚠️ Selenium 드라이버 종료 실패", exc_info=True)

        _selenium_semaphore.release()
        logger.debug("✅ Selenium 세마포어 해제")


def is_mibank_rate_reliable() -> bool:
    """
    MIBANK의 IBK, SC, WOORI 은행 환율이 신뢰할 만한지 여부 판단

    MIBANK는 평일 자정 이후 및 주말에는 영업일 마지막 환율(자정 직전)을
    제공하므로, 해당 시간대에는 부정확한 데이터로 간주합니다.

    Returns:
        True: 평일 09:00 ~ 24:00 (MIBANK 환율 신뢰 가능)
        False: 평일 00:00 ~ 09:00, 주말 (MIBANK 환율 부정확)

    Notes:
        - 주말이 아닌 일반 공휴일은 고려하지 못함
        - IBK, SC, WOORI 크롤러에서 공통 사용
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
