# app/config.py

import os
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# 환경 변수
ENV = os.getenv("ENV", "development")  # development, production
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if ENV == "development" else "INFO")

# DXY 수집 모드
# - futures_coupled: investing.py에서 exchange-rates-table과 함께 수집 (기존)
# - spot_independent: dxy_spot.py 독립 크롤러 사용
DXY_MODE = os.getenv("DXY_MODE", "futures_coupled").strip().lower()
if DXY_MODE not in {"futures_coupled", "spot_independent"}:
    DXY_MODE = "futures_coupled"

# 로그 디렉토리
BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 로그 파일 설정 (Option 1: 단순화 - 2개만 사용)
LOG_FILES = {
    "app": LOG_DIR / "app.log",           # 모든 운영 로그 (INFO+)
    "error": LOG_DIR / "error.log",       # 에러/경고만 (WARNING+)
    # crawler.log, debug.log 제거 (중복 제거, 관리 단순화)
}

# 로그 파일 로테이션 설정
LOG_ROTATION = {
    "maxBytes": 10 * 1024 * 1024,  # 10MB
    "backupCount": 3,               # 총 30MB (5→3 감소, 디스크 I/O 절약)
    "encoding": "utf-8"
}

# Redis 설정 (Phase 1.7 - 브로드캐스트 캐시, Phase 1A - 그래프 캐시)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None  # 빈 문자열 → None 변환

# 텔레그램 설정 (Phase 2용, 현재 비활성화)
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Firebase 설정 (Phase 2 - FCM 푸시 알림)
# Firebase Console > 프로젝트 설정 > 서비스 계정 > 새 비공개 키 생성
FIREBASE_CREDENTIALS_PATH = os.getenv(
    "FIREBASE_CREDENTIALS_PATH",
    str(BASE_DIR / "firebase-service-account.json")
)

# RevenueCat 설정 (Phase 3 - 서버 사이드 구독 검증)
REVENUECAT_API_KEY = os.getenv("REVENUECAT_API_KEY", "")
REVENUECAT_WEBHOOK_AUTH_KEY = os.getenv("REVENUECAT_WEBHOOK_AUTH_KEY", "")
