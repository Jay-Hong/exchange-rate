# app/crawlers/constants.py

"""
크롤러 공통 상수

모든 크롤러에서 사용하는 공통 설정을 중앙 관리
- HTTP HEADERS
- Selenium Options
- Timeout 설정
"""

# HTTP 헤더 (모든 requests 크롤러 공통)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0'
}

# Selenium Chrome Options (모든 Selenium 크롤러 공통)
SELENIUM_OPTIONS = [
    "--headless=new",
    "--window-size=1280x720",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--lang=ko_KR",
    "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
]

# Timeout 설정
DEFAULT_TIMEOUT = 10  # requests timeout (초)
SELENIUM_WAIT_TIMEOUT = 5  # WebDriverWait timeout (초)
SELENIUM_WAIT_TIMEOUT_SHORT = 3  # WebDriverWait timeout (초, 짧은 대기)
SELENIUM_WAIT_TIMEOUT_LONG = 10  # WebDriverWait timeout (초, 긴 대기)
