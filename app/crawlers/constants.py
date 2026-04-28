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
    # [2025-11-08 이전] 초기 메모리 절약 설정 (Swap 27.8% 발생 전)
    # "--window-size=800,600",           # 1920x1080 → 800x600 (30MB 절약)
    # [2025-11-08] Swap 사용 완화를 위한 추가 메모리 절감 (50-80MB 추가 절약)
    "--window-size=400,300",           # 800x600 → 400x300 (30-50MB 추가 절약)
    "--blink-settings=imagesEnabled=false",  # 이미지 차단 (20-30MB 절약)
    "--disable-javascript",            # JS 엔진 메모리 절감 (20-30MB 절약)
    "--disable-webgl",                 # WebGL 메모리 해제 (10-20MB 절약)
    # NOTE: --single-process 제거 (Chrome 142+에서 작동 불안정, 프로세스 누수 원인)

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
SELENIUM_DRIVER_TIMEOUT = 42  # Selenium 드라이버 전체 타임아웃 (초)

# ═════════════════════════════════════════════════════════════
# 환율 검증 공통 상수 (mibank 기반)
# ═════════════════════════════════════════════════════════════
MIBANK_REQUIRED_CODES = ("USD", "JPY", "EUR")
MIBANK_REQUIRED_PAIRS = ("usd-krw", "jpy-krw", "eur-krw")
MIBANK_RATE_RANGES = {
    "usd-krw": (1000, 2000),
    "jpy-krw": (600, 1400),
    "eur-krw": (1100, 2200),
}

# ═════════════════════════════════════════════════════════════
# 크롤러 과거 데이터 조회 설정
# ═════════════════════════════════════════════════════════════
# IBK, SC, Woori 크롤러는 공휴일/주말 대비를 위해 과거 날짜 조회
# 보관기간과 별개로 크롤링 부하를 제한하기 위해 최근 10일까지만 조회
MAX_DAYS_LOOKBACK = 10  # 최대 조회 가능한 과거 날짜 수 (12→10 감소)

# ═════════════════════════════════════════════════════════════
# Chrome 프로세스 수명 제한 (좀비 프로세스 방지)
# ═════════════════════════════════════════════════════════════
CHROME_MAX_LIFETIME_SECONDS = 60  # Chrome 프로세스 최대 수명: 1분 (180→60 감소)
CHROME_CLEANUP_INTERVAL_MINUTES = 1  # 좀비 프로세스 정리 주기: 1분 (3→1 감소)

# ═════════════════════════════════════════════════════════════
# Selenium 크롤러 우선순위 시스템 (2025-11-08 추가)
# ═════════════════════════════════════════════════════════════
# 우선순위 정의 (작은 숫자 = 높은 우선순위 = 먼저 실행)
# - 빠른 크롤러를 먼저 실행하여 전체 처리 속도 향상
# - 느린 크롤러는 타임아웃으로 격리되어 전체 시스템에 영향 최소화
# ─────────────────────────────────────────────────────────────
SELENIUM_PRIORITY_MAP = {
    "hana": 0,     # 가장빠름 → 1순위
    "shinhan": 1,  # 빠름    → 2순위
    "nh": 2,       # 중간    → 3순위
    "ibk": 3,      # 느림    → 4순위
    "sc": 4,       # 가장느림 → 5순위
}

# 크롤러별 타임아웃 (초)
# - 빠른 크롤러: 짧은 타임아웃 (빠른 실패)
# - 느린 크롤러: 여유 있는 타임아웃 (정상 동작 보장)
# - 느린 크롤러는 타임아웃으로 조기 종료 → Queue 정체 방지
# - 실패 시 재시도 메커니즘으로 데이터 손실 방지
SELENIUM_TIMEOUT_MAP = {
    "hana": 45,    # 스케줄러 타임아웃 통일
    "shinhan": 45,
    "nh": 45,
    "ibk": 45,
    "sc": 45,
}
