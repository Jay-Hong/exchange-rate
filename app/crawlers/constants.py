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

# ═════════════════════════════════════════════════════════════
# Selenium Chrome Options (AWS 프리티어 1GB RAM 최적화)
# ═════════════════════════════════════════════════════════════
# 메모리 절약 효과:
# - window-size: 800x600 (30MB 절약)
# - imagesEnabled=false (20-30MB 절약)
# - single-process (50-70MB 절약)
# 총: 약 100-130MB 절약 (Selenium 인스턴스당)
# ─────────────────────────────────────────────────────────────
SELENIUM_OPTIONS = [
    # 필수 설정
    "--headless=new",                  # 최신 headless 모드
    "--no-sandbox",                    # Docker 필수 (sandbox 비활성화)
    "--disable-setuid-sandbox",        # Docker 필수 (권한 문제 해결)
    "--disable-dev-shm-usage",         # /dev/shm 용량 부족 방지
    "--disable-gpu",                   # GPU 비활성화

    # 메모리 최적화 (AWS 프리티어 필수)
    "--window-size=800,600",           # 1920x1080 → 800x600 (30MB 절약)
    "--blink-settings=imagesEnabled=false",  # 이미지 차단 (20-30MB 절약)
    "--single-process",                # 단일 프로세스 (50-70MB 절약, 안정성 희생)

    # 봇 감지 방지
    "--disable-blink-features=AutomationControlled",
    "--lang=ko_KR",
    "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",

    # 프로세스 최적화
    "--disable-software-rasterizer",   # GPU 렌더링 프로세스 감소
    "--disable-extensions",            # Extension 프로세스 제거
    "--disable-background-networking", # Background 프로세스 감소
    "--disable-default-apps",          # 기본 앱 비활성화
    "--disable-sync",                  # 동기화 프로세스 제거
    "--metrics-recording-only",        # 불필요한 통계 수집 프로세스 제거
    "--no-first-run",                  # 초기 실행 프로세스 제거
    "--disable-breakpad",              # 크래시 리포터 비활성화
    "--disable-component-extensions-with-background-pages",  # 백그라운드 확장 프로세스 제거

    # 추가 최적화
    "--disable-notifications",         # 알림 비활성화
    "--disable-popup-blocking",        # 팝업 차단 비활성화
    "--disable-infobars",              # 인포바 비활성화
]

# ═════════════════════════════════════════════════════════════
# Timeout 설정
# ═════════════════════════════════════════════════════════════
DEFAULT_TIMEOUT = 10  # requests timeout (초)
SELENIUM_WAIT_TIMEOUT = 5  # WebDriverWait timeout (초)
SELENIUM_WAIT_TIMEOUT_SHORT = 3  # WebDriverWait timeout (초, 짧은 대기)
SELENIUM_WAIT_TIMEOUT_LONG = 10  # WebDriverWait timeout (초, 긴 대기)
SELENIUM_DRIVER_TIMEOUT = 60  # Selenium 드라이버 전체 타임아웃 (초)
