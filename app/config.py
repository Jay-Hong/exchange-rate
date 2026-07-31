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
# LATEST_MIRROR_INTERVAL_SECONDS: mirror job 주기 (초). 기본 3s, 운영 60s
# (mirror-retirement cadence-reduce 2026-06-25 — polling 3s→60s 20×↓, 60s recovery net 유지).
# stale 판정은 mirrored_at 기준 interval * 2 초 초과 (latest_rates_cache.is_stale → 운영 120s).
REDIS_LATEST_ENABLED = os.getenv("REDIS_LATEST_ENABLED", "false").lower() == "true"
LATEST_MIRROR_INTERVAL_SECONDS = int(os.getenv("LATEST_MIRROR_INTERVAL_SECONDS", "3"))

# mirror-retirement Slice 1: DXY direct latest writer 토글 (default OFF = dormant).
# true 시 dxy_spot 수집 성공 직후 latest:dxy:current를 직접 SET(mirror cycle의 DXY 갱신을
# 크롤러 측으로 이관하는 1단계). mirror와 공존(동일 v1 serialize_dxy_value, last-write-wins).
# 배포는 flag off라 behavior-change-0, 이후 env=true로 canary 활성화. mirror 제거는 후속
# 슬라이스(replace-before-remove) — 본 flag는 direct write 추가만.
DXY_DIRECT_LATEST_ENABLED = os.getenv("DXY_DIRECT_LATEST_ENABLED", "false").lower() == "true"

# mirror-retirement B step2: read-path per_key_stale 완화 토글 (default OFF = 현 동작 유지).
# true 시 fetch_rates_from_redis가 per-key mirrored_at이 is_stale(interval×2; 기본 6s/운영 120s)이어도 전체 DB fallback 대신
# old-but-present 값을 서빙(rate는 정확, mirrored_at만 늙음) + served_stale meta 표시. 단 age가
# CEILING 초과면 fallback(naive 무한 서빙 금지). miss/parse/redis_error/circuit은 계속 fallback(완화 X).
# mirror 은퇴의 forcing function(A heartbeat가 아니라 read-path freshness 의미 재정의). mirror ON 채
# 배포해 회귀 격리 — mirror 살아있으면 per_key_stale 자체가 거의 0이라 무해성만 검증, 실제 노출은
# mirror cadence 축소 후. 진짜 Redis<DB malfunction은 시간 아닌 revision reconciliation(step4)으로.
LATEST_PER_KEY_STALE_SERVE_ENABLED = os.getenv("LATEST_PER_KEY_STALE_SERVE_ENABLED", "false").lower() == "true"
# CEILING: 관대한 절대 backstop (default 7일). off-hours 주말(~60h)/긴 연휴(추석·설 ~5일)의 정당한
# 무변경 staleness를 서빙해야 B의 off-hours 이점이 유지됨 → 분/시간 단위 ceiling이면 주말 전체 fallback.
# 시간 기반은 market-closed와 writer-failure를 구분 못 하므로(codex 019ef9c5) malfunction 판정은
# step4 revision reconciliation에 맡기고, 본 ceiling은 "절대 비상식적으로 오래된 값 서빙 금지" 한계만.
LATEST_PER_KEY_STALE_CEILING_SECONDS = int(os.getenv("LATEST_PER_KEY_STALE_CEILING_SECONDS", "604800"))
# 검증: ceiling은 is_stale 임계(interval × STALE_RATIO)보다 커야 의미 있음(완화 효과 존재).
if LATEST_PER_KEY_STALE_CEILING_SECONDS <= LATEST_MIRROR_INTERVAL_SECONDS * 2:
    raise ValueError(
        "LATEST_PER_KEY_STALE_CEILING_SECONDS must exceed is_stale threshold "
        f"(LATEST_MIRROR_INTERVAL_SECONDS×STALE_RATIO=2) "
        f"(got {LATEST_PER_KEY_STALE_CEILING_SECONDS})."
    )

# ATOMIC_MODE_POLL_INTERVAL_SECONDS: P1b A2-2 write-mode cache poll 주기 (초).
# scheduler가 N초마다 atomic_write_control row를 읽어 atomic_write_runtime cache를 갱신
# (외부/admin/C6의 control 변경을 따라잡는 backstop). 0 이하면 IntervalTrigger 미정의.
ATOMIC_MODE_POLL_INTERVAL_SECONDS = int(os.getenv("ATOMIC_MODE_POLL_INTERVAL_SECONDS", "10"))
if ATOMIC_MODE_POLL_INTERVAL_SECONDS < 1:
    raise ValueError(
        f"ATOMIC_MODE_POLL_INTERVAL_SECONDS must be >= 1 "
        f"(got {ATOMIC_MODE_POLL_INTERVAL_SECONDS}). 0 이하이면 poll 주기 오설정."
    )

# source_hourly_rates retention (ADR-035 D3 Step 1).
# v2 1w graph reads 7 days, but the canonical hourly table keeps a buffer for
# deploy delay, weekend/session boundaries, and short operational interruptions.
SOURCE_HOURLY_RETENTION_DAYS = int(os.getenv("SOURCE_HOURLY_RETENTION_DAYS", "14"))

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

# USDT_WS_SUPERVISOR_ENABLED: §12.9.8 ② 5-source WS collector task supervisor 토글.
# default false — 배포 ≠ 동작 변화 (job 등록 자체를 gate, 비활성 시 코드 경로 0).
# 활성화 = env toggle + recreate 별도 GO. supervisor는 죽은 collector task(asyncio
# task crash/cancel)를 주기 watchdog으로 감지 → idempotent teardown(shutdown_*) 후
# fresh 재시작(start_*). ①(silent-stale reconnect)은 살아있는 task 안의 reconnect라
# task-death 미커버 → 본 supervisor가 마지막 안전망.
USDT_WS_SUPERVISOR_ENABLED = os.getenv("USDT_WS_SUPERVISOR_ENABLED", "false").lower() == "true"
# supervisor watchdog 주기 (초). done() 체크는 무비용이라 30s면 복구 지연 충분.
USDT_WS_SUPERVISOR_INTERVAL_SECONDS = int(os.getenv("USDT_WS_SUPERVISOR_INTERVAL_SECONDS", "30"))
if USDT_WS_SUPERVISOR_INTERVAL_SECONDS < 1:
    raise ValueError(
        f"USDT_WS_SUPERVISOR_INTERVAL_SECONDS must be >= 1 "
        f"(got {USDT_WS_SUPERVISOR_INTERVAL_SECONDS}). 0 이하이면 IntervalTrigger 동작이 "
        "미정의 — watchdog 주기 오설정 차단."
    )

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

# Fix (2026-06-10, silent-session reconnect): data frame 침묵이 이 임계를 넘으면
# WS 연결을 능동 재수립 (_KrxSilentSessionError raise → 기존 reconnect backoff 경로).
# KRX_STALE_SEC(60s)는 라벨 + REST fallback 평가 신호로 유지, 본 임계는 reconnect
# "액션" 임계 — 2단 분리. 6/9 사고: CF 15:04 silent stall (PINGPONG으로 TCP 유지,
# data frame 0) 40분 고착 → reconnect_attempts=0 → 15:45 종가 누락.
# default 150s 근거: 정상 세션 max_total_gap 실측 ~34s (체결+호가 합산, CF/CM
# 2026-06-09~10) → ~4.4× 마진. 호가 frame도 _last_tick_at 갱신하므로 정상 장중/
# 단일가/pre-open(08:30 실측 호가 즉시 흐름)에는 미발화.
KRX_SILENT_RECONNECT_SEC = int(os.getenv("KRX_SILENT_RECONNECT_SEC", "150"))

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

if KRX_SILENT_RECONNECT_SEC <= KRX_STALE_SEC:
    raise ValueError(
        f"KRX_SILENT_RECONNECT_SEC ({KRX_SILENT_RECONNECT_SEC}) must be > "
        f"KRX_STALE_SEC ({KRX_STALE_SEC}). reconnect 액션 임계가 stale 라벨 임계보다 "
        "작거나 같으면 의미 모순 — stale 가시화/REST 평가 전에 연결을 끊어버린다."
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

if SOURCE_HOURLY_RETENTION_DAYS < 8:
    raise ValueError(
        f"SOURCE_HOURLY_RETENTION_DAYS must be >= 8 (got {SOURCE_HOURLY_RETENTION_DAYS}). "
        "1w(7일) 노출 window보다 작거나 같으면 boundary/운영 지연 버퍼가 사라진다."
    )

# Phase Z-2b: Topic dispatcher 토글 (PR Z-2b Stage 1).
# default OFF로 골격만 배포 — TopicRegistry / publish_topic 모두 import 가능하나
# main.py WebSocket subscribe handler 미연결 (Stage 2 별도 PR).
# Stage 1: 모듈 + flag만, 운영 영향 0.
# Stage 2: WebSocket handler 연결 (FF=true시 subscribe 메시지 처리).
# Stage 3: publish_topic을 실제 source data hook에 연결 (5/19+ 권장).
TOPIC_DISPATCHER_ENABLED = os.getenv("TOPIC_DISPATCHER_ENABLED", "false").lower() == "true"

# (제거됨 2026-07-08, ADR-038 Decision 2) KRX_TOPIC_INCLUDE — usdt:krw payload의
# usd_krw_futures group 포함 토글이었으나 group 자체가 제거됨. KRX는 독립 topic
# krx:usd-krw-futures 전용이며 발행 게이트는 KRX_CLIENT_DISTRIBUTION_EFFECTIVE.
# (.env에 잔존해도 무해 — 미참조.)

# ADR-038 G2 — 모든 client-facing KRX distribution 게이트 (topic group + graph v2 krx series +
# KRX 알림 생성). G3(KRX_FUTURES_ENABLED, 수집)와 분리 — G2 off면 수집/DB/Redis는 지속하되
# 단말 배포 표면만 차단. default false (안전) — 운영은 .env로 활성.
KRX_CLIENT_DISTRIBUTION_ENABLED = os.getenv("KRX_CLIENT_DISTRIBUTION_ENABLED", "false").lower() == "true"

# 파생 effective — "게이트 하나라도 닫히면 전 표면 미노출" (ADR-038 Decision 4).
# G3 off면 수집 중단 후에도 Redis/DB 잔존값이 노출될 수 있어 G3도 client-facing 판정에 포함
# (codex 보강 2026-07-08). graph v2 / entitlements 판정은 이 값(또는 두 flag 조합)을 사용.
KRX_CLIENT_DISTRIBUTION_EFFECTIVE = KRX_FUTURES_ENABLED and KRX_CLIENT_DISTRIBUTION_ENABLED


# KRX_CLOSE_FINALIZER_ENABLED: KRX_CLOSE_SNAPSHOT_PLAN §5.2/§5.3 2차 작업 토글.
# default true — Stage 3+ 정책 (WS-first close finalizer) 활성:
#   - KrxDbWriter close grace window 진입 시 일반 path skip (F1 fix, Plan §5.2)
#   - KrxCloseWindowWriter: close grace window 안 last candidate 1건 unconditional
#     INSERT + Redis SET + flag SET (DB+Redis 모두 성공 시만 — F2 fix, Plan §5.3)
#   - Stage 4 (예정) KrxCloseSnapshotController 단순화: 3 retry → 1 + captured flag GET
#   - Stage 5 (예정) WS listen grace drain
# false 시 1차 PR (c0855ff) 동작 그대로 — 회귀 시 즉시 rollback path.
KRX_CLOSE_FINALIZER_ENABLED = os.getenv("KRX_CLOSE_FINALIZER_ENABLED", "true").lower() == "true"

# KRX_CLOSE_REST_WRITE_ENABLED: KrxCloseSnapshotController의 REST 기반 DB/Redis
# write path 차단 토글 (2026-05-25 운영 사고 대응).
#
# 사고: 2026-05-25 휴장일에 KIS REST가 5/22 stale 종가(rate=1516.8)를 rt_cd=0
# 정상 응답으로 반환 → close snapshot REST fallback이 그 stale 값을 5/25 15:45
# KST timestamp로 DB row(id=429785) + Redis latest 기록 → 단말 노출.
#
# 본질: KIS REST는 휴장/만기 후에도 stale 응답을 정상 형식으로 반환. REST 응답
# 만으로 "오늘 종가"임을 증명할 수 없으므로 write source로 부적합.
#
# 정책 재설계 (2026-06-10, #4 — 6/9 WS silent-stall 종가 누락 사고 후속):
# 6/9에 WS가 종가를 못 잡았을 때 REST는 정확한 종가(1514.7, 공식 종가와 일치 확인)를
# 확보했으나 본 flag 무조건 차단에 막혀 일봉이 누락됨 → "무조건 차단"을
# "gate-checked write"로 재설계. gate chain(fail-closed, 전부 통과 시에만 write):
#   gate 1 calendar         — is_krx_business_day(boundary date) (5/25 모드 1차 차단)
#   gate 2 contract identity — REST 응답 월물/만기 == 캡처 contract (5/18 rollover 차단)
#   gate 3 session evidence  — 우리 WS의 마지막 tick이 boundary −
#       KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS 이내 (자기 데이터로 "오늘 시장이
#       실제 거래했다" 증명 — 캘린더 라이브러리 미등록 휴장(5/25 모드 본질)까지 차단.
#       last=None도 reject — 구 sanity-skip 구멍 폐쇄)
#   + price sanity ±2% — 기존 유지. 단 실제 평가 순서는 sanity가 gate 1~3보다
#     먼저 (기존 분기 보존). 비대칭 주의: sanity abort=False(legacy 모드 retry) /
#     gate reject=True(terminal short-circuit) — stale+±2% 동시 이탈 날은
#     rest_sanity_aborted로 귀속되고 gate verdict가 안 남음 (telemetry 해석 시 참고).
#
# semantics (default false):
#   - flag=false: REST fetch + sanity + **gate chain shadow 평가** (verdict는
#     counter/event telemetry로만 기록 — rest_write_gate_rejected_{reason} 또는
#     rest_write_blocked(gates=passed)), DB/Redis write는 차단 + retry short-circuit.
#     주의: finalizer 경로 REST는 WS-miss 날에만 실행되므로 shadow 샘플은 그런 날에만
#     쌓임 (정상일 0건이 정상 — "데이터가 안 쌓였네" 오독 금지).
#   - flag=true: **gate-checked write** — gate 전부 통과 시에만 DB/Redis write +
#     (CF + KRX_DAILY_APPEND_ENABLED 시) daily append tail. 구 "무가드 write 복원"
#     의미는 폐기 (2026-06-10 supersede).
#
# KRX_CLOSE_FINALIZER_ENABLED 값과 무관하게 동일 적용 — finalizer=true 경로의 REST
# fallback 1회, finalizer=false rollback 경로의 retry 3회 모두 gate 경유.
#
# 영향 범위 외: KrxCloseWindowWriter (WS close frame 기반, 신뢰 source) — 변경 X.
KRX_CLOSE_REST_WRITE_ENABLED = os.getenv("KRX_CLOSE_REST_WRITE_ENABLED", "false").lower() == "true"

# gate 3 (session evidence) 임계: 마지막 WS tick이 boundary로부터 이 시간(시간 단위)
# 이내면 "오늘 세션이 실제 거래했다"로 인정. 실측 근거: 6/9 사고는 last tick 15:04
# (boundary −41min) → pass가 정답 / 5/25는 last ts=5/23 06:00 KST (5/22 야간 CM close,
# boundary −2.4일) → reject가 정답.
# 보수적 3h: 장중 거래정지(halt) edge에서 halt 가격을 종가로 쓰는 것도 거름 (fail-closed).
# 대안(세션 open 이후 tick 존재 — 복구 범위 넓음)과의 비교는 KRX_CLOSE_SNAPSHOT_PLAN
# §5.7 amendment 참조. insert-if-changed 특성상 가격 3h+ 정체 시 false-reject 가능
# (이중 희귀: WS-miss AND 정체 — fail-closed 수용, next-day OpenAPI 복구 경로 존재).
KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS = float(
    os.getenv("KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS", "3")
)

if KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS <= 0:
    raise ValueError(
        f"KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS must be > 0 "
        f"(got {KRX_CLOSE_REST_WRITE_TICK_RECENCY_HOURS}). 0 이하면 gate 3 "
        "(session evidence)가 모든 REST close write를 무조건 reject한다."
    )

# KRX_DAILY_APPEND_ENABLED: ADR-034 Phase 2d KRX daily-append (source_daily_rates) 토글.
# default false — Unit 4b CF finalizer hook(append_krx_cf_daily_row)을 배포와 분리해
# 활성화하기 위함. critical path(close finalizer)에 붙은 코드라 "배포 ≠ 동작 변화" 보장:
#   - false: KrxCloseWindowWriter._sync_write의 CF 성공 tail에서 daily-append 미실행
#            (close finalizer 기존 동작과 완전 동일 — source_rates/Redis/flag/tether/return 불변)
#   - true: CF 정규장 종가 확정 후 source_daily_rates에 daily row append (격리된 best-effort tail)
# 활성화 순서: EC2 배포(gate off 동작 확인) → env 토글 → 다음 CF close canary 관찰.
KRX_DAILY_APPEND_ENABLED = os.getenv("KRX_DAILY_APPEND_ENABLED", "false").lower() == "true"

# NOTE(2026-06-10, ADR-035 D3): KRX_HOURLY_APPEND_ENABLED env + in-process hourly hook은
# 제거됨. KRX hourly는 cron append(scripts/hourly_append_krx_source_hourly_rates.py — 월물
# 제거 + CF/CM 통합 + 일봉 의존 절단)로 재설계되어 다른 hourly source(Bithumb/Investing/Hana)와
# 동일한 OS cron(:11) self-maintaining 구조로 전환. daily append(KRX_DAILY_APPEND_ENABLED, 위)만 유지.

# KRX_REDIS_TICK_WRITE_ENABLED: KRX Stage E — Redis latest write timing 변경 토글
# (KRX_FANOUT_REFACTOR_PLAN.md §5.2 E).
#
# 현재 동작 (flag=false default): KrxDbWriter가 1초 window debounce 후 DB
# insert-if-changed 성공 시점에 Redis latest SET + tether topic trigger 발사
# (DB-insert-bound). 가격 stagnant 시 DB insert 없음 → Redis timestamp /
# mirrored_at도 갱신 안 됨.
#
# Stage E 동작 (flag=true): tick path를 DB insert 여부와 분리. 신규
# KrxRedisLatestWriter tick handler가 매 tick → Redis latest SET (USDT 5b-bis
# 5-field schema + in-memory state + 5s grain coalescing) → KrxLatestWriteOutcome.SET
# 시점에만 trigger 발사. KrxDbWriter는 DB history만 담당 (Redis write/trigger skip).
#
# 권장 조합:
#   finalizer=true + tick-write=true   → Stage E 정상 운영
#   finalizer=true + tick-write=false  → 현재 default, Stage E 미적용
#   finalizer=false + tick-write=false → 1차 PR 동작 rollback (안전)
# 비권장 조합 (운영자 가드 — 코드는 강제 차단 X, 주석 가이드만):
#   finalizer=false + tick-write=true  → close 종가 결측 risk (KrxCloseWindowWriter
#     비활성 + close REST_WRITE=false 차단으로 close 정각 종가 누락 가능).
#     finalizer rollback 시 tick-write도 함께 false로 되돌리는 게 안전.
#
# 영향 범위 외: KrxCloseWindowWriter (WS close frame 기반, 신뢰 source) — 변경 X.
# Close grace window 안 tick은 Stage E writer skip (KrxCloseWindowWriter 단독 처리).
KRX_REDIS_TICK_WRITE_ENABLED = os.getenv("KRX_REDIS_TICK_WRITE_ENABLED", "false").lower() == "true"

# KRX_ALERT_EVALUATOR_ENABLED: KRX 가격 알림 evaluator 토글 (F-1, 2026-05-26).
#
# 현재 동작 (flag=false default): KRX tick은 alert evaluator로 흐르지 X
# (KrxAlertTickHandler 미등록). KRX 가격 알림 자체가 비활성.
#
# Flag=true 동작: KisFuturesClient에 `KrxAlertTickHandler` tick handler 등록.
# 매 WS tick → AlertObservation(kind="tick") 생성 → `UsdtAlertEvaluator` (source-
# neutral helper 재사용) `schedule()`. USDT 5 source와 동일한 settings cache /
# coalescer / refetch / FCM 경로 공유.
#
# 정책 anchor (Codex 정정 반영, F-1 설계):
#   - SET-only ❌ — 모든 tick 평가 대상 (Stage E layer와 직교, alert는 SET/SKIPPED 분기 없음)
#   - Close grace skip ❌ — close grace tick도 평가 대상 (종가 crossing 보존)
#   - Thin wrapper 패턴 — `UsdtAlertEvaluator`가 이미 source-neutral이라 KRX
#     전용 별도 evaluator class 분리 X (`KrxAlertEvaluator(UsdtAlertEvaluator)`는
#     로깅/타이핑 목적 thin subclass).
#
# F-2 / F-3 후속 단계:
#   - F-2: source_registry KRX phase1_enabled=True + API category validation 확장
#     (현재 API는 category="derivative"라 KRX 알림 등록 차단 — F-2에서 허용)
#   - F-3: env=true 활성 (force-recreate) + 테스트 iOS canary 관찰
KRX_ALERT_EVALUATOR_ENABLED = os.getenv("KRX_ALERT_EVALUATOR_ENABLED", "false").lower() == "true"

# COMPARISON_ALERT_ENABLED: 비교 알림(ADR-037) evaluator 발화 게이트 (default false — dormant).
# emit_comparison_observation 첫 줄 gate (KRX_ALERT_EVALUATOR_ENABLED 패턴 — flag off면
# zero-overhead return). API/스키마는 flag 무관 land(dead until flag). 활성화 = env true +
# force-recreate + iOS canary end-to-end (F-3 절차).
COMPARISON_ALERT_ENABLED = os.getenv("COMPARISON_ALERT_ENABLED", "false").lower() == "true"

# FX_ALERT_SHADOW_ENABLED: FX(bank+investing) alert shadow 토글 (fanout step 4 S4, 2026-06-23).
#
# 현재 동작 (flag=false default): crud의 _emit_fx_alert_shadow가 첫 줄 early-return →
# schedule_on_loop 미호출 = zero overhead. legacy process_rate_alerts(authoritative)만 동작.
#
# Flag=true 동작 (S5): bank/investing 환율 변경 batch마다 changes→AlertObservation 변환 후
# topic_trigger_bridge로 main loop에 마샬링 → fx_alert_shadow.evaluate_fx_batch_shadow(
# FxNotificationBackend + dedicated cache + no-op sender)로 병렬 telemetry-only 평가.
# persist/FCM no-op → legacy와 2× 발사 없음. would_fire/shadow_stats = 보조 진단(execution-proof),
# parity 기준선은 legacy baseline(crud _fx_legacy_match_counts) — async shadow는 parity 아님(S6 demote).
# ([ALL_SOURCE_FANOUT_UNIFICATION_PLAN.md] §6.1 S4/S5/S6)
FX_ALERT_SHADOW_ENABLED = os.getenv("FX_ALERT_SHADOW_ENABLED", "false").lower() == "true"

# FX_ALERT_CUTOVER_CANARY_*: FX 알림 cutover canary (setting_id allowlist, 2026-06-24, §6.1 canary plan).
#
# legacy crud.process_rate_alerts → 새 evaluator(FxCanaryBackend, real persist+FCM) 전환을 setting 단위로.
# allowlist setting만 canary가 real 발사 + legacy가 skip(중복 방지). non-allowlist = legacy 그대로.
# shadow(diagnostic, no-op)와 별 evaluator/cache. flag off면 _emit_fx_alert_canary early-return +
# legacy skip 미발동(allowlist 빈) → prod 동작 불변(behavior-change-0).
# ⚠️ canary는 async best-effort — schedule_on_loop 실패 시 legacy skip된 setting이 0회 발사(miss).
#    canary phase(본인 setting 1개 watch)는 수용, **확대 전 enqueue-confirmed-skip 필요**(§6.1 B1).
FX_ALERT_CUTOVER_CANARY_ENABLED = os.getenv("FX_ALERT_CUTOVER_CANARY_ENABLED", "false").lower() == "true"
FX_ALERT_CUTOVER_CANARY_SETTING_IDS = frozenset(
    int(x) for x in os.getenv("FX_ALERT_CUTOVER_CANARY_SETTING_IDS", "").split(",") if x.strip()
)

# KRX_CLOSE_EVENT_LOG_ENABLED: KRX close finalizer structured event persistence
# 토글 (2026-05-26). 활성 시 KrxCloseWindowWriter + KrxCloseSnapshotController
# emit point에서 Redis ZSET `krx:close_finalizer_events`에 event_type 단위로
# append-only 저장. 14일 ZREMRANGEBYSCORE retention. admin endpoint
# `/admin/api/krx-finalizer-stats`에서 (date_kst, session) aggregation + case
# 분류 조회.
#
# 설계 원칙 (코덱스 + Claude 검증 합의):
#   - best-effort / no-throw: Redis 실패/JSON 실패/trim 실패가 close finalizer
#     본 동작(WS save / REST skip / rest_write_blocked counter)에 영향 0
#   - 기존 control flow 변경 0: emit은 side effect chain 끝에 별도 try/except
#   - 저장 시점 case 분류 X: event_type만 store, case는 query 시점 aggregation
#   - dedup_skipped는 실제 코드 분기 부재로 catalog 제외 (speculation 차단)
#
# 운영 emergency rollback: env=false → force-recreate 시 emit 자체 skip.
# 본 flag default true — telemetry 보존이 본 land 의도.
KRX_CLOSE_EVENT_LOG_ENABLED = os.getenv("KRX_CLOSE_EVENT_LOG_ENABLED", "true").lower() == "true"

# Phase Z-2c — FX topic 발사 토글 (fx:usd-krw / fx:jpy-krw / fx:eur-krw).
# TOPIC_DISPATCHER_ENABLED와 분리 (구 KRX_TOPIC_INCLUDE[제거됨]과 동일 철학):
#   - TOPIC_DISPATCHER_ENABLED: topic dispatch 전체 on/off
#   - FX_TOPIC_ENABLED: FX 3 topic 발사 여부 (단일 flag — per-currency 분리 X)
# default OFF로 골격만 배포 — fx_topic_publisher 모듈 import 가능하나 main.py
# broadcast hook이 flag=true일 때만 발사. usdt:krw와는 독립 (USDT 끄지 않고
# FX만 켜기/끄기 가능).
FX_TOPIC_ENABLED = os.getenv("FX_TOPIC_ENABLED", "false").lower() == "true"

# P1b C6-7 — atomic cutover publisher gate **shadow observe** (dry-run, default OFF).
# true일 때만 _publish_fx_snapshot이 publish 직전 cutover gate disposition을 read해 would-block을
# 별도 gate_* telemetry로 기록(legacy publish는 절대 차단 안 함 — dry-run). off면 snapshot() 미호출
# (zero hot-path 추가). real enforcement(closed→skip publish)는 C6-FLIP. refresh_from_db 미스케줄이라
# C6-7에선 켜도 prod는 _INITIAL/PASS_THROUGH 상시(WOULD_BLOCK은 test-injected만).
FX_CUTOVER_GATE_OBSERVE_ENABLED = os.getenv("FX_CUTOVER_GATE_OBSERVE_ENABLED", "false").lower() == "true"

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

# Phase Z-2 §6.6.2 — Bank/Investing β PR C (C1) source-routed topic trigger.
# bank/investing 변경을 fx:* (+ kb·hana·investing usd-krw는 usdt:krw cross-route)
# topic으로 발사하는 write-through trigger 모드. tether 패턴 mirror.
# 이름이 fx가 아닌 source(bank/investing) 기준인 이유: fx:* + usdt:krw cross-route
# 둘 다 게이트하므로 (§6.6.2).
#   - legacy_piggyback (default): emission 전체 noop → land behavior-change-0
#     (기존 main.py legacy hook만 publish).
#   - dual_shadow: fx coalesce 측정 (publish X) + 조건부 cross-route는
#     tether_route_shadow counter만 (실제 tether 호출 X — live tether 발행 방지).
#   - direct_coalesced: fx publish + 조건부 cross-route tether 실호출.
BANK_INVESTING_TOPIC_TRIGGER_MODE = os.getenv(
    "BANK_INVESTING_TOPIC_TRIGGER_MODE", "legacy_piggyback"
).strip().lower()
BANK_INVESTING_TOPIC_TRIGGER_ALLOWED_MODES = (
    "legacy_piggyback",
    "dual_shadow",
    "direct_coalesced",
)
BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS = int(
    os.getenv("BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS", "500")
)

if BANK_INVESTING_TOPIC_TRIGGER_MODE not in BANK_INVESTING_TOPIC_TRIGGER_ALLOWED_MODES:
    raise ValueError(
        "BANK_INVESTING_TOPIC_TRIGGER_MODE must be one of "
        f"{BANK_INVESTING_TOPIC_TRIGGER_ALLOWED_MODES} "
        f"(got {BANK_INVESTING_TOPIC_TRIGGER_MODE!r})."
    )

if BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS < 1:
    raise ValueError(
        "BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS must be >= 1 "
        f"(got {BANK_INVESTING_TOPIC_TRIGGER_COALESCE_MS})."
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

# ── WS subscribe 인증 시간 계약 (ADR-040 timeout 슬라이스) ────────────────────
# 두 축이 **함께** 있어야 한다. 측정으로 확인한 것:
#   · caller deadline 만으로는 간섭이 줄지 않는다 — T=8.0 고정에서 D 를 2.0→0.5 로 4배 낮춰도
#     동거 `to_thread` 작업 지연이 7966→7978ms(변화 없음). 지배항은 **실행 중 작업 시간**이다.
#   · 반대로 실행 중 작업 시간(T)을 낮추면 지연이 그에 비례해 내려간다.
# 그래서 ① wire deadline 만 넣으면 "호출자는 빨리 포기하는데 프로세스는 계속 막혀 있는" 상태가 된다.

# ② SDK transport 상한 — 인증 전용 named app 의 `httpTimeout`(**per-attempt**).
# ⚠️ **측정 없음, 보수적 기본값.** 두 제약으로 좁혔다:
#   (a) 서울 EC2 → Google `accounts:lookup` warm RTT 대비 여유,
#   (b) **프로세스 기동 후 첫 subscribe 1건은 콜드 인증서 fetch(TLS+GET)를 이 안에 끝내야 한다**
#       — 1~2초면 그 1건이 상시 `temporarily_unavailable` 이 된다.
# 무엇을 재면 정해지나: flag ON 후 `verify_ws_subscribe_token` 벽시계 p50/p99 를 **콜드 첫 호출과
# 분리해서** 측정 → `T = p99 × 3`.
WS_AUTH_HTTP_TIMEOUT_SECONDS = 5

# ① wire deadline — dispatcher 가 호출자 대기를 끊는 상한. **측정 없음**(= 2T).
WS_AUTH_WIRE_DEADLINE_SECONDS = 10

# §8-C `temporarily_unavailable` 동반값 (일시 장애).
WS_AUTH_RETRY_AFTER_SECONDS = 5

# ⛔ 재시도로 낫지 않는 결함(서버측 401/403·설정 결함·미초기화)은 같은 간격을 주면 안 된다 —
#    분류해 놓고 5초마다 재시도를 지시하면 retry storm 이 되고 운영자 신호가 희석된다.
WS_AUTH_PERSISTENT_FAULT_RETRY_AFTER_SECONDS = 30

# ⛔ **D ≤ T 면 ②가 죽은 코드가 된다** — transport 상한이 발동하기 전에 호출자가 먼저 포기하므로
#    낮춘 `httpTimeout` 이 아무것도 바꾸지 않는다. 그 상태를 import 시점에 막는다.
if WS_AUTH_WIRE_DEADLINE_SECONDS <= WS_AUTH_HTTP_TIMEOUT_SECONDS:
    raise ValueError(
        "WS_AUTH_WIRE_DEADLINE_SECONDS 는 WS_AUTH_HTTP_TIMEOUT_SECONDS 보다 커야 한다 — "
        f"got D={WS_AUTH_WIRE_DEADLINE_SECONDS} T={WS_AUTH_HTTP_TIMEOUT_SECONDS}"
    )
