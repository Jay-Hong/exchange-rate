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
    "backupCount": 3,               # 총 30MB (5→3 감소, 디스크 I/O 절약)
    "encoding": "utf-8"
}

# Redis 설정 (Phase 1.7 - 브로드캐스트 캐시, Phase 1A - 그래프 캐시)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None  # 빈 문자열 → None 변환

# Redis-first broadcast (PR3 - latest mirror)
# REDIS_LATEST_ENABLED: PR3 활성화 토글. default OFF로 코드 배포 후 운영 영향 없이 들어가고,
# 운영에서 env=true로 canary 활성화. guardrail 위반 시 env=false로 즉시 rollback 가능.
# LATEST_MIRROR_INTERVAL_SECONDS: mirror job 주기 (초). PoC 후 1/3/5초 sweet spot 비교 가능.
# stale 판정은 mirrored_at 기준 interval * 2 초 초과 (latest_rates_cache.is_stale).
REDIS_LATEST_ENABLED = os.getenv("REDIS_LATEST_ENABLED", "false").lower() == "true"
LATEST_MIRROR_INTERVAL_SECONDS = int(os.getenv("LATEST_MIRROR_INTERVAL_SECONDS", "3"))

# KRX_FUTURES_ENABLED: PR6 KRX 미국달러선물 수집/저장 자체 토글.
# default false — scheduler 등록되어도 client.start() 호출 안 함.
# false 시 WebSocket 연결, DB 저장, mirror 모두 X (KRX 완전 비활성).
# canary 진입 시 ON. KRX 장애/중단 시 false로 전체 격리 가능.
KRX_FUTURES_ENABLED = os.getenv("KRX_FUTURES_ENABLED", "false").lower() == "true"

# KRX_BROADCAST_INCLUDE: PR6 KRX 미국달러선물의 broadcast 노출 토글.
# 저장된 KRX usd-krw-futures를 latest mirror + broadcast rates 배열에 포함할지.
# default false — 앱 호환성 검증 전 안전 차단. canary 진입 시 단계적 ON 권장.
# scope: source="krx" + asset="usd-krw-futures" 한정 (KRX 다른 자산은 영향 X).
KRX_BROADCAST_INCLUDE = os.getenv("KRX_BROADCAST_INCLUDE", "false").lower() == "true"

# PR6d-1: KRX_REST_FALLBACK_ENABLED — REST snapshot/fallback 경로 토글.
# default false — PR6d 코드 배포해도 helper가 호출되지 않아 운영 영향 0.
# Stage 1에서 토글 ON 시 fallback orchestration이 stale 동안 REST 호출 (사용자
# 노출은 KRX_BROADCAST_INCLUDE=false라 0). Stage 2 진입 전 운영 검증용.
# REST = WebSocket 대체가 아니라 stale 동안의 bounded fallback probe (ADR-027).
KRX_REST_FALLBACK_ENABLED = os.getenv("KRX_REST_FALLBACK_ENABLED", "false").lower() == "true"

# PR6d-1: stale 임계값 (초). _last_tick_at 후 N초 무응답이면 stale 전이.
# 기본 60s = 코드 상수 STALE_AFTER_SEC와 동일. 5/8 baseline + raw frame metric
# (PR6d-2) 후 조정. CM 저거래량 별도 임계값(KRX_STALE_SEC_CM)은 baseline 후 결정.
KRX_STALE_SEC = int(os.getenv("KRX_STALE_SEC", "60"))

# PR6d-1: REST 호출 cooldown (초). status 복귀 cooldown 아님 — REST 호출 중복
# 방지용 (stale 지속 중에는 cooldown마다 1회). status 복귀는 frame 1건 즉시.
KRX_REST_COOLDOWN_SEC = int(os.getenv("KRX_REST_COOLDOWN_SEC", "30"))

# PR6d-2b: REST fallback eligibility threshold (초). status 전이 임계(KRX_STALE_SEC=60)와
# 분리 — "stale 상태가 N초 이상 지속되어야 REST fallback eligible".
# 5/8 baseline 기준 CM 종료 전 자연 silence max 76.1s → 120s 보수적 default.
# Codex 합의 (2026-05-08): status stale 의미와 fallback 트리거 의미 혼동 방지 위해 별도 env.
KRX_REST_FALLBACK_STALE_SEC = int(os.getenv("KRX_REST_FALLBACK_STALE_SEC", "120"))

# PR6d-2b: session-end grace 분. session 종료 시각 -N 분부터 fallback 트리거 차단.
# 5/8 baseline 기준 CM 종료 전 stale 4건 모두 32분 이내 cluster → 40분 default.
# CONTINUOUS 끝부분 저유동성 + CLOSE_AUCTION 단일가 10분 모두 cover.
KRX_REST_FALLBACK_SESSION_END_GRACE_MIN = int(
    os.getenv("KRX_REST_FALLBACK_SESSION_END_GRACE_MIN", "40")
)

if KRX_STALE_SEC < 1:
    raise ValueError(
        f"KRX_STALE_SEC must be >= 1 (got {KRX_STALE_SEC}). "
        "0 이하이면 KRX WebSocket stale 판정이 즉시/무한 전이될 수 있다."
    )

if KRX_REST_FALLBACK_STALE_SEC < KRX_STALE_SEC:
    raise ValueError(
        f"KRX_REST_FALLBACK_STALE_SEC ({KRX_REST_FALLBACK_STALE_SEC}) must be >= "
        f"KRX_STALE_SEC ({KRX_STALE_SEC}). fallback 임계가 status 전이 임계보다 "
        "작으면 의미 모순 — status가 stale로 전이되기 전에 fallback eligible 판정."
    )

if KRX_REST_FALLBACK_SESSION_END_GRACE_MIN < 0:
    raise ValueError(
        f"KRX_REST_FALLBACK_SESSION_END_GRACE_MIN must be >= 0 "
        f"(got {KRX_REST_FALLBACK_SESSION_END_GRACE_MIN}). 음수면 grace 함수가 "
        "항상 False 반환하여 모든 stale이 fallback eligible — 운영 env 오설정 차단."
    )

if KRX_REST_COOLDOWN_SEC < 1:
    raise ValueError(
        f"KRX_REST_COOLDOWN_SEC must be >= 1 (got {KRX_REST_COOLDOWN_SEC}). "
        "REST fallback retry storm 방지를 위해 양수만 허용한다."
    )

if LATEST_MIRROR_INTERVAL_SECONDS < 1:
    raise ValueError(
        f"LATEST_MIRROR_INTERVAL_SECONDS must be >= 1 (got {LATEST_MIRROR_INTERVAL_SECONDS}). "
        "Silent floor 대신 startup 시 명시 실패 (Crash Early)."
    )

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
