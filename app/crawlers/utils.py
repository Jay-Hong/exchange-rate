# app/crawlers/utils.py

"""
크롤러 공통 함수

모든 크롤러에서 사용하는 공통 함수들
- 환율 텍스트 파싱
- Selenium 드라이버 생성
"""

# 서드파티 라이브러리
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

# 로컬 애플리케이션
from app.crawlers.constants import SELENIUM_OPTIONS


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
    표준 Selenium Chrome 드라이버 생성 (Chromium 사용)

    Returns:
        webdriver.Chrome 인스턴스

    Notes:
        - Headless 모드로 실행
        - Docker에서 시스템 Chromium/ChromeDriver 사용
        - SELENIUM_OPTIONS 상수에서 옵션 로드

    Examples:
        >>> driver = create_selenium_driver()
        >>> driver.get("https://example.com")
        >>> driver.quit()
    """
    import os

    options = webdriver.ChromeOptions()

    # Chromium 바이너리 경로 설정 (Docker 환경)
    chromium_bin = os.getenv('CHROME_BIN', '/usr/bin/chromium')
    if os.path.exists(chromium_bin):
        options.binary_location = chromium_bin

    # 옵션 추가
    for arg in SELENIUM_OPTIONS:
        options.add_argument(arg)

    # ChromeDriver 경로 (Docker 환경)
    chromedriver_path = os.getenv('CHROMEDRIVER_PATH', '/usr/bin/chromedriver')

    # Service 생성 (webdriver-manager 제거)
    if os.path.exists(chromedriver_path):
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
