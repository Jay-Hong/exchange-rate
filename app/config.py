# app/config.py

import os
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# 환경 변수
ENV = os.getenv("ENV", "development")  # development, production
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if ENV == "development" else "INFO")

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
    "backupCount": 5,               # 총 50MB (약 5-10일분)
    "encoding": "utf-8"
}

# 텔레그램 설정 (Phase 2용, 현재 비활성화)
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
