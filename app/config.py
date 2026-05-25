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

# USDT_WS_UPBIT_ENABLED: Phase B.1 PR1 Upbit canary WebSocket 토글
# (USDT_WS_DESIGN_PLAN §12).
# default false — scheduler에 lifecycle은 등록되지만 client.start() 호출 안 함.
# PR1 (skeleton): true → stop event 대기만, 네트워크 호출 없음.
# PR2 이후: true → WS connect/subscribe/parse 동작.
# Stage 1 (운영 활성화) 진입 시 ON. 장애 시 false로 USDT WS Upbit 격리.
# 5거래소 확장은 per-exchange flag (USDT_WS_BITHUMB_ENABLED 등)으로 추가 예정.
USDT_WS_UPBIT_ENABLED = os.getenv("USDT_WS_UPBIT_ENABLED", "false").lower() == "true"

# USDT_WS_BITHUMB_ENABLED: Phase B.3 Bithumb WS canary 토글
# (USDT_WS_DESIGN_PLAN §12.5).
# default false — scheduler에 lifecycle 등록되지만 client.start() 호출 안 함.
# U2 (현재): true → stop_event 대기만, network 호출 없음.
# U3 이후: true → WS connect/subscribe/parse 동작.
# Canary 활성화 조건 — KRX close finalizer 5/18 CF + 5/19 CM 첫 실측 + 7일 telemetry
# 안정 후 별도 deploy GO. 그 전까지 false default 유지 — 운영 영향 0.
# false 시 invariant (acceptance Stage U2): start 함수 즉시 return + BithumbWsClient
# 생성 X + network connect X + Redis/DB writer X (USDT_WS_DESIGN_PLAN §12.5.2 U2 핵심).
USDT_WS_BITHUMB_ENABLED = os.getenv("USDT_WS_BITHUMB_ENABLED", "false").lower() == "true"

# USDT_WS_COINONE_ENABLED: Phase B.4 Coinone WS canary 토글 (USDT_WS_DESIGN_PLAN §12.6).
# default false — scheduler에 lifecycle 등록되지만 client.start() 호출 안 함.
# C2 (현재): true → stop_event 대기만, network 호출 없음.
# C3 이후: true → WS connect/subscribe/parse 동작 (별 protocol parser).
# Canary 활성화 조건 — KRX close finalizer 5/26 telemetry 안정 + C2~C7 land 안정 후 별도 deploy GO.
# false 시 invariant (acceptance Stage C2): start 함수 즉시 return + CoinoneWsClient
# 생성 X + network connect X + Redis/DB writer X (USDT_WS_DESIGN_PLAN §12.6.4 C2 핵심).
USDT_WS_COINONE_ENABLED = os.getenv("USDT_WS_COINONE_ENABLED", "false").lower() == "true"

# USDT_WS_KORBIT_ENABLED: Phase B.5 Korbit WS canary 토글 (USDT_WS_DESIGN_PLAN §12.7).
# default false — scheduler에 lifecycle 등록되지만 client.start() 호출 안 함.
# K2 (현재): true → stop_event 대기만, network 호출 없음.
# K3 이후: true → WS connect/subscribe/parse 동작 (list-wrap subscribe + unified status 분기).
# Canary 활성화 조건 — Coinone canary 24h+ 안정 + K2~K7 land 안정 후 별도 deploy GO.
# false 시 invariant (acceptance Stage K2): start 함수 즉시 return + KorbitWsClient
# 생성 X + network connect X + Redis/DB writer X (USDT_WS_DESIGN_PLAN §12.7.4 K2 핵심).
USDT_WS_KORBIT_ENABLED = os.getenv("USDT_WS_KORBIT_ENABLED", "false").lower() == "true"

# USDT_WS_GOPAX_ENABLED: Phase B.6 Gopax WS canary 토글 (USDT_WS_DESIGN_PLAN §12.9).
# default false — scheduler에 lifecycle 등록되지만 client.start() 호출 안 함.
# G1 (현재): true → stop_event 대기만, network 호출 없음 (placeholder lifecycle).
# G2 이후: true → Primus subscribe + 2종 응답 parse + USDT-KRW 클라이언트 필터링 동작.
# Canary 활성화 조건 — Korbit/Coinone canary 24h+ 안정 + G2~G7 land 안정 후 별도 deploy GO.
# false 시 invariant (acceptance Stage G1): start 함수 즉시 return + GopaxWsClient
# 생성 X + network connect X + Redis/DB writer X.
USDT_WS_GOPAX_ENABLED = os.getenv("USDT_WS_GOPAX_ENABLED", "false").lower() == "true"

# USDT_LEGACY_REST_POLLING_ENABLED: 상시 USDT REST polling cron (collect_usdt_rates) 토글.
# default false — WS 도입 전 과도기 잔재. WS가 매 tick으로 Redis/DB/alert를 모두
# 처리하고, WS stale 시 source-specific REST fallback probe가 동일 fanout을 재사용
# 하므로 상시 polling은 중복. flag=true 시 기존 cron(매분 06,16,26,36,46,56초) 복원.
# rollback: env로 USDT_LEGACY_REST_POLLING_ENABLED=true + force-recreate fastapi.
USDT_LEGACY_REST_POLLING_ENABLED = os.getenv("USDT_LEGACY_REST_POLLING_ENABLED", "false").lower() == "true"

# KRX_BROADCAST_INCLUDE: removed in Z-2d cleanup (2026-05-12).
# 이전: KRX 미국달러선물의 broadcast latest:* 노출 토글.
# Z-2d 통일로 legacy_policy.should_include_source_in_legacy_rates allowlist가
# 단일 진실 소스가 됨. KRX는 allowlist 미포함이라 자동 차단 — env 토글 무의미.
# Historical context: DECISIONS.md ADR-027, KRX_CANARY.md, CHANGELOG.md 참조.

# PR6d-1: KRX_REST_FALLBACK_ENABLED — REST snapshot/fallback 경로 토글.
# default false — PR6d 코드 배포해도 helper가 호출되지 않아 운영 영향 0.
# Stage 1에서 토글 ON 시 fallback orchestration이 stale 동안 REST 호출
# (사용자 노출은 Z-2d allowlist로 0 — legacy_policy 통일).
# Stage 2 진입 전 운영 검증용.
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

# Phase Z-2b: Topic dispatcher 토글 (PR Z-2b Stage 1).
# default OFF로 골격만 배포 — TopicRegistry / publish_topic 모두 import 가능하나
# main.py WebSocket subscribe handler 미연결 (Stage 2 별도 PR).
# Stage 1: 모듈 + flag만, 운영 영향 0.
# Stage 2: WebSocket handler 연결 (FF=true시 subscribe 메시지 처리).
# Stage 3: publish_topic을 실제 source data hook에 연결 (5/19+ 권장).
TOPIC_DISPATCHER_ENABLED = os.getenv("TOPIC_DISPATCHER_ENABLED", "false").lower() == "true"

# Phase Z-2b Stage 3 Level 3 — 테더 topic 안 KRX 미국달러선물 포함 여부.
# TOPIC_DISPATCHER_ENABLED와 의미 분리 (제거된 legacy KRX_BROADCAST_INCLUDE 재사용 X):
#   - TOPIC_DISPATCHER_ENABLED: topic publish 전체 on/off
#   - KRX_TOPIC_INCLUDE: 테더 topic payload에 usd_krw_futures key 포함 여부
# KRX만 격리 가능 — KRX 데이터 이상 시 topic 전체 끌 필요 없이 KRX만 끄고
# USDT/은행/Investing은 그대로 publish 유지.
KRX_TOPIC_INCLUDE = os.getenv("KRX_TOPIC_INCLUDE", "false").lower() == "true"

# KRX_CLOSE_FINALIZER_ENABLED: KRX_CLOSE_SNAPSHOT_PLAN §5.2/§5.3 2차 작업 토글.
# default true — Stage 3+ 정책 (WS-first close finalizer) 활성:
#   - KrxDbWriter close grace window 진입 시 일반 path skip (F1 fix, Plan §5.2)
#   - KrxCloseWindowWriter: close grace window 안 last candidate 1건 unconditional
#     INSERT + Redis SET + flag SET (DB+Redis 모두 성공 시만 — F2 fix, Plan §5.3)
#   - Stage 4 (예정) KrxCloseSnapshotController 단순화: 3 retry → 1 + captured flag GET
#   - Stage 5 (예정) WS listen grace drain
# false 시 1차 PR (c0855ff) 동작 그대로 — 회귀 시 즉시 rollback path.
KRX_CLOSE_FINALIZER_ENABLED = os.getenv("KRX_CLOSE_FINALIZER_ENABLED", "true").lower() == "true"

# Phase Z-2c — FX topic 발사 토글 (fx:usd-krw / fx:jpy-krw / fx:eur-krw).
# TOPIC_DISPATCHER_ENABLED와 분리 (KRX_TOPIC_INCLUDE 패턴과 동일 철학):
#   - TOPIC_DISPATCHER_ENABLED: topic dispatch 전체 on/off
#   - FX_TOPIC_ENABLED: FX 3 topic 발사 여부 (단일 flag — per-currency 분리 X)
# default OFF로 골격만 배포 — fx_topic_publisher 모듈 import 가능하나 main.py
# broadcast hook이 flag=true일 때만 발사. usdt:krw와는 독립 (USDT 끄지 않고
# FX만 켜기/끄기 가능).
FX_TOPIC_ENABLED = os.getenv("FX_TOPIC_ENABLED", "false").lower() == "true"

# Phase B.2 — Tether topic direct trigger 분리.
# default legacy_piggyback: PR1~PR3 배포해도 기존 main.py is_changed hook만 동작.
# dual_shadow: direct trigger/coalesce는 실제처럼 수행하되 publish call만 skip.
# direct_coalesced: coalesce window 뒤 기존 tether publisher를 직접 호출.
TETHER_TOPIC_TRIGGER_MODE = os.getenv(
    "TETHER_TOPIC_TRIGGER_MODE", "legacy_piggyback"
).strip().lower()
TETHER_TOPIC_TRIGGER_ALLOWED_MODES = (
    "legacy_piggyback",
    "dual_shadow",
    "direct_coalesced",
)
TETHER_TOPIC_TRIGGER_COALESCE_MS = int(
    os.getenv("TETHER_TOPIC_TRIGGER_COALESCE_MS", "500")
)

if TETHER_TOPIC_TRIGGER_MODE not in TETHER_TOPIC_TRIGGER_ALLOWED_MODES:
    raise ValueError(
        "TETHER_TOPIC_TRIGGER_MODE must be one of "
        f"{TETHER_TOPIC_TRIGGER_ALLOWED_MODES} (got {TETHER_TOPIC_TRIGGER_MODE!r})."
    )

if TETHER_TOPIC_TRIGGER_COALESCE_MS < 1:
    raise ValueError(
        "TETHER_TOPIC_TRIGGER_COALESCE_MS must be >= 1 "
        f"(got {TETHER_TOPIC_TRIGGER_COALESCE_MS})."
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
