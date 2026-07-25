# app/main.py

# 표준 라이브러리
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import List, Dict, Any, Optional

# 서드파티 라이브러리
from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pytz import timezone
from sqlalchemy.orm import Session
import secrets

# 로컬 애플리케이션
from app import models, schemas, crud, scheduler, topic_dispatcher, tether_topic_publisher, fx_topic_publisher, legacy_policy, usdt_redis_stats, tether_topic_trigger, bank_investing_redis_stats, entitlements
from app.database import engine, SessionLocal, Base, create_all_app_tables
from app.admin.stats import broadcast_stats
from app.cache import redis_cache, BROADCAST_CACHE_KEY
from app import config
from app.config import REDIS_LATEST_ENABLED
from app.latest_rates_cache import fetch_rates_from_redis, warmup_latest_rates
from app.notifications.fcm import init_firebase, is_firebase_initialized, send_fcm_data_only
from sqlalchemy import exc as sqlalchemy_exc

from app.subscription import verify_premium_status, PremiumStatus
from app.webhooks import router as webhooks_router

# 로거 설정
logger = logging.getLogger("exchange_rate.main")

PENDING_RETRY_AFTER_SECONDS = "5"

# DB **인프라 transient**만 — 재시도가 의미 있는 것들. 503 변환 경계다.
# ⛔ `SQLAlchemyError` 전체를 쓰면 안 된다: `ProgrammingError`(잘못된 SQL/누락 테이블),
# `InvalidRequestError`·`ResourceClosedError`·`ArgumentError`·`CompileError`(ORM 사용 결함),
# `IntegrityError`·`DataError`(데이터 결함)가 전부 하위라 **영구 결함을 무한 재시도로 안내**하게 된다
# (codex Major 2026-07-26). 아래 4종은 connect 실패 / 네트워크 단절 / statement timeout /
# 풀 고갈 / failover를 커버하고 위 결함들은 걸리지 않는다(계층 실측 + 회귀 테스트로 잠금).
TRANSIENT_DB_ERRORS = (
    sqlalchemy_exc.OperationalError,
    sqlalchemy_exc.InterfaceError,
    sqlalchemy_exc.TimeoutError,        # 풀 고갈 (pool_size=3 + max_overflow=2)
    sqlalchemy_exc.DisconnectionError,
)

# HTTP Basic Auth 설정
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """관리자 인증 확인"""
    admin_password = os.getenv("ADMIN_PASSWORD")

    # Production 환경에서는 ADMIN_PASSWORD 필수
    if not admin_password:
        env = os.getenv("ENV", "development")
        if env == "production":
            logger.error("🚨 ADMIN_PASSWORD 환경변수가 설정되지 않음 (production)")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin authentication not configured",
            )
        else:
            # 개발 환경에서만 기본값 허용
            admin_password = "admin1234"
            logger.warning("⚠️ 기본 관리자 비밀번호 사용 중 (개발 환경 전용)")

    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, admin_password)

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def require_premium(user_id: str, allow_empty: bool) -> bool:
    """프리미엄 게이트 — **허용은 ACTIVE에만** (fail-closed).

    구 코드는 "PENDING·INACTIVE가 아니면 True"였다. `PremiumStatus`가 정확히 3값이고
    `verify_premium_status`의 모든 return이 그 3값이라 **오늘은 동치**지만(behavior-change-0,
    tests/test_require_premium.py 6-case 매트릭스가 영수증), 새 상태가 추가되면 그 상태가
    자동으로 프리미엄 통과가 된다 — 게이트의 기본값이 '허용'이면 안 된다.
    (`/api/entitlements`는 이미 `status == ACTIVE`로 fail-closed였다. 여기만 예외였다.)

    알 수 없는 상태는 **INACTIVE와 동일 처리**한다 — fail-closed이면서 `allow_empty` 계약을
    보존한다. 여기서 503을 던지면 `allow_empty=True` read endpoint(알림/로그 조회)가
    잠재 fail-open에서 **실제 장애**로 바뀐다. 가드가 만든 회귀가 가드가 막는 결함보다 커진다.
    """
    premium_status = await verify_premium_status(user_id)

    if premium_status == PremiumStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Subscription status pending. Retry later.",
            headers={"Retry-After": PENDING_RETRY_AFTER_SECONDS},
        )

    if premium_status == PremiumStatus.ACTIVE:
        return True

    if premium_status != PremiumStatus.INACTIVE:
        # 현 enum 3값에선 도달 불가. 조용히 거부하면 enum 확장 사고를 운영이 못 본다.
        logger.error(
            "알 수 없는 premium 상태 — fail-closed 처리",
            extra={"event": "premium_status_unknown", "premium_status": str(premium_status)},
        )

    if allow_empty:
        return False
    raise HTTPException(
        status_code=403,
        detail="Premium subscription required",
    )


async def notify_user_devices_sync(db: Session, user_id: str):
    """
    해당 사용자의 모든 기기에 sync_alerts 발송 (best-effort)

    다중 기기 알림 설정 동기화를 위한 사일런트 푸시.
    - CRUD 성공 후 호출 (응답 전에 실행됨)
    - 실패해도 HTTP 응답에 영향 없음

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
    """
    try:
        devices = crud.get_devices_by_user(db, user_id)
        tokens = [d.device_token for d in devices]

        if not tokens:
            return

        # 500개씩 batch 처리 (FCM API 제한)
        FCM_BATCH_SIZE = 500
        total_success = 0
        total_failure = 0
        all_failed_tokens = []

        for i in range(0, len(tokens), FCM_BATCH_SIZE):
            batch_tokens = tokens[i:i + FCM_BATCH_SIZE]
            result = await send_fcm_data_only(
                tokens=batch_tokens,
                data={"type": "sync_alerts"}
            )
            total_success += result["success_count"]
            total_failure += result["failure_count"]
            all_failed_tokens.extend(result["failed_tokens"])

        # 무효 토큰 일괄 삭제 (batch)
        if all_failed_tokens:
            db.query(models.UserDevice).filter(
                models.UserDevice.device_token.in_(all_failed_tokens)
            ).delete(synchronize_session=False)
            db.commit()

        logger.info(
            "동기화 푸시 발송",
            extra={
                "event": "sync_alerts_sent",
                "user_id": user_id,
                "success": total_success,
                "failure": total_failure,
                "total": len(tokens)
            }
        )

    except Exception:
        logger.warning("동기화 푸시 실패 (무시됨)", exc_info=True)


# DB 테이블 생성 — atomic_write_control(P1 control plane)은 제외 (migration script가
# 운영(non-test) 유일 생성 경로 → import 시점에 운영 PG로 신규 CHECK DDL emit 안 함 = A1 behavior-change-0).
# 제외 로직은 app/database.create_all_app_tables 단일 진실 소스 (backfill 등 다른 진입점도 공유).
create_all_app_tables(engine)

# HTML 템플릿 디렉토리 설정
templates = Jinja2Templates(directory="templates")

# WebSocket 연결 관리
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("✅ WebSocket 연결 성공", extra={"connections": len(self.active_connections)})

    def disconnect(self, websocket: WebSocket):
        try:
            self.active_connections.remove(websocket)
            logger.info("❌ WebSocket 연결 해제", extra={"connections": len(self.active_connections)})
        except ValueError:
            logger.warning("⚠️ 이미 제거된 WebSocket 연결 시도", extra={"connections": len(self.active_connections)})

    async def broadcast(self, message: dict, send_timeout: float = 5.0):
        """모든 연결된 클라이언트에게 메시지 병렬 전송 (PR1: 직렬 → asyncio.gather + timeout).

        Args:
            message: 전송할 JSON-serializable 메시지
            send_timeout: 클라이언트별 send_json 타임아웃 (초). 느린 클라이언트가 다른
                          전송을 지연시키지 않도록 격리. PR2부터 mode별로 다른 값을
                          호출자(`broadcast_rates_once`)에서 결정해 전달
                          (normal 5.0초 / fast·window 0.5초).
        """
        # 리스트 복사본으로 스냅샷 (순회 중 수정 방지)
        connections = self.active_connections[:]
        if not connections:
            return

        async def _send_to(conn: WebSocket):
            try:
                await asyncio.wait_for(conn.send_json(message), timeout=send_timeout)
                return None
            except asyncio.TimeoutError:
                logger.warning("⚠️ 전송 타임아웃", extra={"timeout_sec": send_timeout})
                return conn
            except Exception:
                logger.warning("⚠️ 전송 실패", exc_info=True)
                return conn

        results = await asyncio.gather(*[_send_to(c) for c in connections])

        # 실패한 연결 제거 (timeout/예외 모두 동일 처리)
        failed = [r for r in results if r is not None]
        for conn in failed:
            try:
                self.active_connections.remove(conn)
            except ValueError:
                # 이미 제거됨 (disconnect()에서 제거된 경우)
                pass

        if failed:
            logger.warning(
                "⚠️ 일부 전송 실패 (병렬 broadcast)",
                extra={"failed": len(failed), "total": len(connections)},
            )

manager = ConnectionManager()

# ═════════════════════════════════════════════════════════════
# 그래프 API 인메모리 캐시 (Tier 2 Fallback) - Phase 1A
# ═════════════════════════════════════════════════════════════
_memory_cache = {}
_cache_timestamps = {}
_db_query_timestamps = {}

KST = timezone("Asia/Seoul")

# ═════════════════════════════════════════════════════════════
# Broadcast Mode (PR2) — 1초 broadcast PoC + window 모드
# ═════════════════════════════════════════════════════════════
# scheduler는 cron='*'로 매초 wake-up하지만, broadcast_rates_once 첫 줄에서
# mode + second 체크로 DB 조회 전 즉시 return.
#
# normal: second ∈ {0,10,20,30,40,50}만 실행 (기존 10초 cron과 동일 동작)
# fast:   모든 second 매초 실행
# window: BROADCAST_FAST_HOURS 안이면 매초, 밖이면 normal과 동일
#
# NOTE (cleanup): 30일 보관 정책 도입(2026-04-28, ADR-023) 직후라 현재
# bank/source의 30일 초과 row가 0건 → 03:20-03:32 cleanup 부하 거의 없음.
# 5월 중순 이후 30일 데이터 누적되면 cleanup 시간대 영향 재평가 필요.
BROADCAST_MODE = os.environ.get("BROADCAST_MODE", "normal").strip().lower()
BROADCAST_SEND_TIMEOUT_SECONDS = float(os.environ.get("BROADCAST_SEND_TIMEOUT_SECONDS", "5.0"))
BROADCAST_FAST_SEND_TIMEOUT_SECONDS = float(os.environ.get("BROADCAST_FAST_SEND_TIMEOUT_SECONDS", "0.5"))
NORMAL_INTERVAL_SECONDS = {0, 10, 20, 30, 40, 50}


def _parse_fast_hours(env_value: str) -> tuple:
    """`BROADCAST_FAST_HOURS=2-4` → (2, 4) 형태 파싱. KST [start, end) 의미.

    잘못된 입력은 default (2, 4)로 fallback. 운영 안전 우선.
    """
    try:
        start_str, end_str = env_value.split("-", 1)
        start = int(start_str.strip())
        end = int(end_str.strip())
        if 0 <= start < end <= 24:
            return (start, end)
    except Exception:
        pass
    return (2, 4)


BROADCAST_FAST_HOURS = _parse_fast_hours(os.environ.get("BROADCAST_FAST_HOURS", "2-4"))


def _is_in_fast_window(now_kst: datetime) -> bool:
    """현재 시간이 BROADCAST_FAST_HOURS 윈도우 안인지."""
    return BROADCAST_FAST_HOURS[0] <= now_kst.hour < BROADCAST_FAST_HOURS[1]


def _should_run_broadcast(now_kst: datetime) -> tuple:
    """broadcast 실행 여부 결정. (run, skip_reason) 반환.

    Returns:
        (True, None): 실행
        (False, 'normal_interval'): normal 모드 + 10초 슬롯 외 → skip
        (False, 'outside_window'): window 모드 + fast hour 밖 + 10초 슬롯 외 → skip
    """
    sec = now_kst.second
    if BROADCAST_MODE == "fast":
        return (True, None)
    if BROADCAST_MODE == "window":
        if _is_in_fast_window(now_kst):
            return (True, None)
        # window 밖이면 normal처럼 10초 슬롯만
        if sec in NORMAL_INTERVAL_SECONDS:
            return (True, None)
        return (False, "outside_window")
    # normal (default)
    if sec in NORMAL_INTERVAL_SECONDS:
        return (True, None)
    return (False, "normal_interval")


def _resolved_send_timeout(now_kst: datetime) -> float:
    """현재 시점에서 적용할 send_timeout 결정.

    fast 모드 또는 window 모드 + fast hour 안에서는 짧은 timeout.
    그 외에는 기본 timeout.
    """
    if BROADCAST_MODE == "fast":
        return BROADCAST_FAST_SEND_TIMEOUT_SECONDS
    if BROADCAST_MODE == "window" and _is_in_fast_window(now_kst):
        return BROADCAST_FAST_SEND_TIMEOUT_SECONDS
    return BROADCAST_SEND_TIMEOUT_SECONDS

def build_rates_payload(db: SessionLocal) -> dict:
    """DB에서 최신 환율을 조회해 표준 메시지 포맷으로 반환."""
    all_rates = crud.get_all_rates_flat(db=db)

    # metadata.currencies/metadata.banks는 레거시 호환용 dead field.
    # USDT 도입 후 자동 계산 로직으로는 거래소 이름이 섞여 들어가 시맨틱이 오염되므로
    # 레거시 값으로 고정한다. 새 앱은 SourceRegistry를 직접 참조한다.
    currencies = sorted(crud.SUPPORTED_CURRENCY_PAIRS)
    banks = sorted(crud.LEGACY_METADATA_BANKS)

    # DB 데이터의 실제 최신 timestamp 사용 (변경 감지 정확성)
    latest_timestamp = max(
        (rate["timestamp"] for rate in all_rates),
        default=crud.to_kst_isoformat(datetime.now(dt_timezone.utc))
    )

    data_section = {
        "rates": all_rates,
        "metadata": {
            "updated_at": latest_timestamp,
            "currencies": currencies,
            "banks": banks,
            "total_count": len(all_rates),
        },
    }

    # DXY live tick (investing 우선, yahoo 폴백). insert_dxy_rate_into_db가
    # rate/source 변경 시에만 레코드를 남기므로 timestamp 변화 = 실제 값 변화.
    latest_dxy = crud.get_latest_dxy_rate(db)
    if latest_dxy:
        data_section["indices"] = {
            "dxy": {
                "rate": latest_dxy["rate"],
                "timestamp": latest_dxy["timestamp"],
                "source": latest_dxy["source"],
            }
        }

    return {
        "type": "rates",
        "data": data_section,
    }


def build_rates_payload_with_timings(db: SessionLocal) -> tuple:
    """build_rates_payload의 분해 계측 변형. (payload, timings) tuple 반환.

    broadcast_rates_once 전용. 다른 호출자(warmup, websocket connect 등)는 기존
    build_rates_payload를 그대로 사용한다 (회귀 위험 제거).

    PR5: DXY DB 조회 시간을 dxy_query_ms로 query_timings에 포함 — rates Redis
    실패로 전체 DB fallback 타는 case에서도 DXY 비용이 동일 의미로 집계되도록.
    """
    all_rates, query_timings = crud.get_all_rates_flat_with_timings(db=db)

    currencies = sorted(crud.SUPPORTED_CURRENCY_PAIRS)
    banks = sorted(crud.LEGACY_METADATA_BANKS)

    latest_timestamp = max(
        (rate["timestamp"] for rate in all_rates),
        default=crud.to_kst_isoformat(datetime.now(dt_timezone.utc))
    )

    data_section = {
        "rates": all_rates,
        "metadata": {
            "updated_at": latest_timestamp,
            "currencies": currencies,
            "banks": banks,
            "total_count": len(all_rates),
        },
    }

    t_dxy0 = time.perf_counter()
    latest_dxy = crud.get_latest_dxy_rate(db)
    query_timings["dxy_query_ms"] = (time.perf_counter() - t_dxy0) * 1000
    # PR5 fix: rates fallback path에서도 dxy_path 기록 (analyzer 4-state 일관성).
    # 이 함수는 case 1 (rates Redis 실패 → 전체 DB)에서만 호출되며, DXY 결과에
    # 따라 db_fallback / missing 분류. analyzer가 이 레코드를 'none'으로 잘못
    # 분류하지 않도록.
    query_timings["dxy_path"] = "db_fallback" if latest_dxy else "missing"

    if latest_dxy:
        data_section["indices"] = {
            "dxy": {
                "rate": latest_dxy["rate"],
                "timestamp": latest_dxy["timestamp"],
                "source": latest_dxy["source"],
            }
        }

    payload = {
        "type": "rates",
        "data": data_section,
    }
    return payload, query_timings


def _assemble_payload_from_rates(db: SessionLocal, rates: list, redis_dxy: Optional[dict]) -> tuple:
    """Redis에서 받은 rates + DXY로 build_rates_payload와 동일 shape 조립 (PR5).

    DXY-only fallback (PR5):
    - redis_dxy 있음 → indices.dxy = redis_dxy (DB 조회 안 함, dxy_query_ms 미측정)
    - redis_dxy 없음 → DB fallback (DXY-only). 성공 시 indices.dxy, 실패 시 indices 생략.
    - rates 자체는 항상 Redis (호출자가 보장)

    rates 인자 출처는 fetch_rates_from_redis()의 반환값 — get_all_rates_flat과
    동일한 legacy shape: [{currency, bank, rate, timestamp}, ...].
    redis_dxy 인자는 fetch_rates_from_redis()의 반환값 — {rate, timestamp, source} 또는 None.

    Returns:
        (payload, dxy_path, dxy_query_ms_or_None):
        - payload: build_rates_payload와 동일 shape
        - dxy_path: 'redis' / 'db_fallback' / 'missing' (3-state)
        - dxy_query_ms_or_None: DB DXY fallback 시도 시 측정값, Redis 성공 시 None
          (None이면 timings에 미기록 — analyzer n 급감으로 PR5 효과 가시화)
    """
    currencies = sorted(crud.SUPPORTED_CURRENCY_PAIRS)
    banks = sorted(crud.LEGACY_METADATA_BANKS)

    latest_timestamp = max(
        (rate["timestamp"] for rate in rates),
        default=crud.to_kst_isoformat(datetime.now(dt_timezone.utc))
    )

    data_section = {
        "rates": rates,
        "metadata": {
            "updated_at": latest_timestamp,
            "currencies": currencies,
            "banks": banks,
            "total_count": len(rates),
        },
    }

    if redis_dxy is not None:
        # case 2: DXY Redis 성공 — DB call 없음, dxy_query_ms 미기록
        data_section["indices"] = {
            "dxy": {
                "rate": redis_dxy["rate"],
                "timestamp": redis_dxy["timestamp"],
                "source": redis_dxy["source"],
            }
        }
        dxy_path = "redis"
        dxy_query_ms: Optional[float] = None
    else:
        # case 3/4: DXY Redis 실패 → DB fallback (DXY-only)
        t_dxy0 = time.perf_counter()
        latest_dxy = crud.get_latest_dxy_rate(db)
        dxy_query_ms = (time.perf_counter() - t_dxy0) * 1000
        if latest_dxy:
            data_section["indices"] = {
                "dxy": {
                    "rate": latest_dxy["rate"],
                    "timestamp": latest_dxy["timestamp"],
                    "source": latest_dxy["source"],
                }
            }
            dxy_path = "db_fallback"
        else:
            # DB도 없으면 indices 키 자체 생략 (기존 build_rates_payload 동작)
            dxy_path = "missing"

    payload = {
        "type": "rates",
        "data": data_section,
    }
    return payload, dxy_path, dxy_query_ms


async def warmup_broadcast_cache():
    """서버 시작 시 DB→Redis 워밍업 (캐시 미스 방지)."""
    db = SessionLocal()
    try:
        payload = build_rates_payload(db)
        payload_json = json.dumps(payload, ensure_ascii=False)
        await redis_cache.set(BROADCAST_CACHE_KEY, payload_json)
        logger.info("✅ Redis 브로드캐스트 캐시 워밍업 완료", extra={"rate_count": len(payload["data"]["rates"])})
    except Exception:
        logger.warning("⚠️ Redis 워밍업 실패 (DB 폴백 유지)", exc_info=True)
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup code
    logger.info("🚀 FastAPI 서버 시작", extra={"env": os.getenv("ENV", "development")})

    # Production 환경에서는 ADMIN_PASSWORD 필수 (서버 시작 실패)
    if os.getenv("ENV", "development") == "production" and not os.getenv("ADMIN_PASSWORD"):
        logger.error("🚨 ADMIN_PASSWORD 환경변수가 설정되지 않음 (production)")
        raise RuntimeError("ADMIN_PASSWORD required in production")

    # Redis 연결 및 워밍업 (브로드캐스트 캐시)
    await redis_cache.connect()
    await warmup_broadcast_cache()

    # PR3: Redis latest mirror warmup (REDIS_LATEST_ENABLED=true 시)
    # broadcast Redis-first 경로가 첫 호출부터 데이터 있도록 startup에 1회 적재.
    # 실패해도 broadcast는 DB fallback으로 동작 → 앱 시작은 막지 않음.
    if REDIS_LATEST_ENABLED:
        # Bug-fix(incident 2026-06-21): startup mirror(warmup) 전에 write-mode cache refresh — warmup이
        # stale _INITIAL=LEGACY 대신 실제 mode(post-flip atomic 등)를 반영하게. idempotent(start_scheduler도
        # refresh), no-throw, control table 부재 시 fail-soft. Fix B(is_initialized skip)가 1차 방어, 이건
        # ordering belt-and-suspenders.
        from app.atomic_write_refresh import refresh_write_mode_cache
        await asyncio.to_thread(refresh_write_mode_cache)
        await warmup_latest_rates()

    # ADR-038 — KRX 게이트(G2/G3) flip 시 구 그래프 payload(krx 포함/미포함) 잔존 방지:
    # 테더 탭 graph v2 캐시를 startup에서 무조건 DEL (env 변경 = force-recreate 전제라
    # 이 지점이 flip 직후 유일한 훅. 방향 무관 안전 + rebuild 저렴[다음 요청/cron ≤22 SELECT]).
    try:
        await redis_cache.delete(
            "graph_v2:tab:tether:1d", "graph_v2:tab:tether:1d:in_progress",
            "graph_v2:tab:tether:1w", "graph_v2:tab:tether:3m", "graph_v2:tab:tether:1y",
            # ADR-038 D4 ② — usd 탭도 krx 시리즈 편입: gate flip/시리즈 구성 변경 잔존 방어
            "graph_v2:tab:usd:1d", "graph_v2:tab:usd:1d:in_progress",
            "graph_v2:tab:usd:1w", "graph_v2:tab:usd:3m", "graph_v2:tab:usd:1y",
        )
        logger.info("🧹 테더/달러 graph v2 캐시 초기화 (ADR-038 KRX gate 정합)")
    except Exception as cache_e:  # 캐시 DEL 실패는 비치명 (TTL 자연 만료로 수렴)
        logger.warning(f"테더 graph 캐시 초기화 실패 (비치명): {cache_e}")

    # Firebase Admin SDK 초기화 (Phase 2 - FCM)
    if init_firebase():
        logger.info("✅ Firebase Admin SDK 초기화 완료")
    else:
        logger.warning("⚠️ Firebase Admin SDK 초기화 실패 (FCM 비활성화)")

    # Crawler Config 초기화 (Phase 1.8 - DB 테이블 생성)
    db = SessionLocal()
    try:
        crud.init_crawler_config(db)
        logger.info("✅ Crawler Config 초기화 완료", extra={"table": "crawler_config"})
    except Exception as e:
        logger.error("❌ Crawler Config 초기화 실패", exc_info=True)
    finally:
        db.close()

    # Selenium Queue 초기화 (스케줄러보다 먼저 실행)
    scheduler.init_selenium_queue()

    # §6.6.2 C1 — topic trigger bridge: crud worker thread → main loop 마샬링용 loop
    # 등록 (scheduler 시작 전). crud emission이 이 loop으로 fx/tether trigger를 보낸다.
    from app import topic_trigger_bridge
    topic_trigger_bridge.register_main_loop(asyncio.get_running_loop())

    # 스케줄러 시작 (Queue를 사용하는 작업 + WebSocket Broadcasting 포함)
    scheduler.start_scheduler()

    # C6-quiesce Q4a — recreate된 fresh app의 halt-관측 ACK (§9 step6, no-throw).
    # start_scheduler()가 startup refresh_write_mode_cache()를 동기 실행한 *직후*라 snapshot이 durable halt
    # 반영. open quiesce session 없으면(=legacy steady state) no-op, table 부재 시에도 no-throw → startup 영향 0.
    from app.atomic_quiesce_startup import record_app_ack_if_quiescing
    await asyncio.to_thread(record_app_ack_if_quiescing)

    # ✅ Broadcasting은 APScheduler에서 자동 실행 (매분 00, 10, 20, 30, 40, 50초)

    # PR6c-2b — KRX 미국달러선물 client (background bootstrap, 즉시 return)
    # KRX_FUTURES_ENABLED=false 또는 어떤 단계 실패도 startup 영향 0.
    await scheduler.start_krx_futures_client()

    # Phase B.1 PR1 — USDT WebSocket Upbit canary skeleton.
    # USDT_WS_UPBIT_ENABLED=false 시 lifecycle 비활성 (startup 영향 0).
    await scheduler.start_usdt_ws_upbit_client()

    # Phase B.3 Stage U2 — USDT WebSocket Bithumb canary skeleton (USDT_WS_DESIGN_PLAN §12.5).
    # USDT_WS_BITHUMB_ENABLED=false 시 lifecycle 비활성 (startup 영향 0).
    await scheduler.start_usdt_ws_bithumb_client()

    # Phase B.4 Stage C2 — USDT WebSocket Coinone canary skeleton (USDT_WS_DESIGN_PLAN §12.6).
    # USDT_WS_COINONE_ENABLED=false 시 lifecycle 비활성 (startup 영향 0).
    await scheduler.start_usdt_ws_coinone_client()

    # Phase B.5 Stage K2 — USDT WebSocket Korbit canary skeleton (USDT_WS_DESIGN_PLAN §12.7).
    # USDT_WS_KORBIT_ENABLED=false 시 lifecycle 비활성 (startup 영향 0).
    await scheduler.start_usdt_ws_korbit_client()

    # Phase B.6 Stage G1 — USDT WebSocket Gopax canary skeleton (USDT_WS_DESIGN_PLAN §12.9).
    # USDT_WS_GOPAX_ENABLED=false 시 lifecycle 비활성 (startup 영향 0).
    await scheduler.start_usdt_ws_gopax_client()

    yield

    # Shutdown code
    logger.info("🛑 FastAPI 서버 종료")

    # §6.6.2 C1 — bridge 신규 enqueue 차단(drain 전) + 큐된 callback flush + fx drain.
    # 순서: 차단 → barrier(큐된 bridge callback 실행 완료 → flush task 생성) →
    # fx drain(그 task까지 drain, orphan 방지) → tether drain → (이후) scheduler 종료.
    from app import topic_trigger_bridge, fx_topic_trigger
    topic_trigger_bridge.signal_shutdown()
    await topic_trigger_bridge.drain_loop_callbacks()
    # §6.1 canary (B2): bridge callback drain 후 canary evaluator의 pending real FCM task drain.
    # (canary 비활성이면 evaluator 미생성 → no-op.) bridge drain 뒤여야 ev.schedule된 task까지 포함.
    from app.notifications import fx_alert_shadow
    await fx_alert_shadow.close_fx_canary_evaluator()
    await fx_topic_trigger.shutdown_fx_topic_trigger()

    # Phase B.2 PR1 — pending tether topic trigger flush 정리.
    await tether_topic_trigger.shutdown_tether_topic_trigger()

    # §12.9.8 ② + KRX task-death — collector 재시작 로직(USDT supervisor + KRX reconcile)이
    #   shutdown 체인 중 종료되는 task/client를 되살리지 못하도록 공유 flag set. USDT/KRX
    #   shutdown 진입 전 필수 — stop()/task await 구간 globals not-None window를 'None skip'
    #   만으론 못 막음 (main.py shutdown 순서 직접 확인).
    scheduler.signal_collector_shutdown_initiated()

    # USDT WebSocket Upbit client 종료
    await scheduler.shutdown_usdt_ws_upbit_client()

    # USDT WebSocket Bithumb client 종료 (Phase B.3 Stage U2)
    await scheduler.shutdown_usdt_ws_bithumb_client()

    # USDT WebSocket Coinone client 종료 (Phase B.4 Stage C2)
    await scheduler.shutdown_usdt_ws_coinone_client()

    # USDT WebSocket Korbit client 종료 (Phase B.5 Stage K2)
    await scheduler.shutdown_usdt_ws_korbit_client()

    # USDT WebSocket Gopax client 종료 (Phase B.6 Stage G1)
    await scheduler.shutdown_usdt_ws_gopax_client()

    # KRX 미국달러선물 client 종료 (bootstrap 진행 중도 안전 cancel)
    await scheduler.shutdown_krx_futures_client()

    # Selenium Queue Worker 종료
    await scheduler.shutdown_selenium_queue()

    # 스케줄러 종료
    scheduler.scheduler.shutdown()

async def build_graph_buckets() -> dict:
    """
    모든 통화의 마지막 그래프 버킷 반환 (WebSocket용)

    Returns:
        {
            "usd-krw": {"investing": {...}, "kb": {...}, "hana": {...}},
            "jpy-krw": {...},
            "eur-krw": {...}
        }
    """
    graph_buckets = {}

    for currency in ["usd-krw", "jpy-krw", "eur-krw"]:
        cache_key = f"graph:{currency}"
        try:
            cached = await redis_cache.get(cache_key)
            if cached:
                data = json.loads(cached)
                currency_data = {}

                for source, series in (data.get("data") or {}).items():
                    if series and len(series) > 0:
                        last_bucket = series[-1]  # [ts, max, min, close]
                        currency_data[source] = {
                            "bucket_ts": last_bucket[0],
                            "max": last_bucket[1],
                            "min": last_bucket[2],
                            "close": last_bucket[3]
                        }

                if currency_data:
                    graph_buckets[currency] = currency_data
        except Exception as e:
            logger.warning(f"그래프 버킷 조회 실패: {currency}", extra={"error": str(e)})
            continue

    return graph_buckets


async def broadcast_rates_once():
    """브로드캐스트 표준 흐름 (Redis 캐시 + 변경 감지 + 조건부 전송).

    PR1 계측: 단계별 raw duration을 logger extra로 남긴다 (외부 분석 도구에서 p50/p99 집계).
    PR2 mode: BROADCAST_MODE에 따라 normal/fast/window 분기. 첫 줄 second 체크로
    DB 조회 전 early return하여 normal 모드에서 기존 10초 동작과 동등.
    """
    # PR2: mode + second + window 분기 (DB 조회 전 early return)
    now_kst = datetime.now(KST)
    should_run, skip_reason = _should_run_broadcast(now_kst)
    if not should_run:
        # normal_interval / outside_window — INFO 로그 안 남김 (매초 wake-up이라 폭증 회피)
        broadcast_stats.record_skip(reason=skip_reason)
        return

    db = SessionLocal()
    timings: Dict[str, float] = {}
    job_start = time.perf_counter()
    send_timeout = _resolved_send_timeout(now_kst)
    in_fast_window = (BROADCAST_MODE == "fast") or (
        BROADCAST_MODE == "window" and _is_in_fast_window(now_kst)
    )
    mode_meta = {
        "broadcast_mode": BROADCAST_MODE,
        "in_fast_window": in_fast_window,
        "send_timeout_sec": send_timeout,
    }
    try:
        # 1a) Redis cache read
        t0 = time.perf_counter()
        cached_json = await redis_cache.get(BROADCAST_CACHE_KEY)
        timings["redis_get_ms"] = (time.perf_counter() - t0) * 1000

        # 1b) payload build (PR3: Redis-first 분기 + DB fallback, PR5: DXY mirror + DXY-only fallback)
        # REDIS_LATEST_ENABLED=true 시 fetch_rates_from_redis 시도 → rates 성공이면
        # Redis path. DXY는 redis_dxy로 별도 (PR5: rates 성공이어도 DXY Redis 실패 시 DB-only fallback).
        # rates 실패는 기존 build_rates_payload_with_timings (PR5: DXY DB 시간도 dxy_query_ms 포함).
        t1 = time.perf_counter()
        if REDIS_LATEST_ENABLED:
            redis_rates, redis_dxy, redis_meta = await fetch_rates_from_redis()
            # success/fallback 모두 단계별 timing이 redis_meta에 포함 (PR3.5 + PR5 latest_dxy_*)
            timings.update(redis_meta)
            if redis_rates is not None:
                # rates Redis success — DXY는 redis_dxy 또는 DB fallback (PR5)
                t_assemble0 = time.perf_counter()
                payload, dxy_path, dxy_query_ms = _assemble_payload_from_rates(
                    db, redis_rates, redis_dxy
                )
                payload_assemble_ms = (time.perf_counter() - t_assemble0) * 1000
                timings["payload_assemble_ms"] = payload_assemble_ms
                timings["dxy_path"] = dxy_path
                # PR5: dxy_query_ms는 DB fallback 시에만 기록 (Redis 성공 시 미기록 → analyzer n 급감)
                if dxy_query_ms is not None:
                    timings["dxy_query_ms"] = dxy_query_ms
                    timings["payload_assemble_without_dxy_ms"] = payload_assemble_ms - dxy_query_ms
                else:
                    # Redis 성공 path는 assemble 거의 전부가 metadata 조립 (DXY DB call 없음)
                    timings["payload_assemble_without_dxy_ms"] = payload_assemble_ms
                query_timings = {}
            else:
                # rates Redis fallback — 전체 DB path (build_rates_payload_with_timings가 dxy_query_ms 포함)
                payload, query_timings = build_rates_payload_with_timings(db)
        else:
            payload, query_timings = build_rates_payload_with_timings(db)
        timings["payload_build_ms"] = (time.perf_counter() - t1) * 1000
        timings.update(query_timings)

        # PR3.5 unmeasured: payload_build_ms 안에서 측정되지 않은 시간 (Redis success
        # path에 한해 의미). 큰 값이면 event loop blocking 또는 코루틴 재개 지연 신호.
        if "latest_fetch_total_ms" in timings and "payload_assemble_ms" in timings:
            timings["payload_build_unmeasured_ms"] = (
                timings["payload_build_ms"]
                - timings["latest_fetch_total_ms"]
                - timings["payload_assemble_ms"]
            )

        # 2) JSON serialize + diff
        t2 = time.perf_counter()
        new_json = json.dumps(payload, ensure_ascii=False)
        is_changed = new_json != cached_json
        timings["serialize_diff_ms"] = (time.perf_counter() - t2) * 1000

        if is_changed:
            await redis_cache.set(BROADCAST_CACHE_KEY, new_json)

            # PR Z-2b Stage 3 Level 2 — 임시 async-safe topic publish hook.
            # USDT WebSocket/Redis-first 전환 전까지의 위치. 전환 후 mirror/topic
            # pipeline으로 이동 예정. 변경 시에만 발화 + legacy 구독자 유무 무관
            # (topic은 별개 채널). FF=false / subscriber 0이면 publisher 내부
            # guard로 즉시 return — builder/publish_topic 호출 0회. 예외 격리는
            # safe_publish_tether_tab_snapshot에서 처리 (broadcast 영향 X).
            # ADR-038 Decision 2: usdt:krw는 KRX 미포함 — KRX는 krx_topic_publisher 전담
            # (호출자 책임 분리, KRX_BROADCAST_INCLUDE legacy 의미와 분리).
            # ADR-038 Decision 2 — usdt:krw는 KRX group 미포함 (KRX는 독립 topic)
            await tether_topic_publisher.safe_publish_tether_tab_snapshot(db)

            # PR Z-2c Step 3 — FX topic broadcast hook (fx:usd-krw/jpy-krw/eur-krw).
            # tether와 같은 격리 원칙: is_changed 분기 안 + active_connections 분기
            # 바깥. config.FX_TOPIC_ENABLED=false default → publisher 내부 guard로
            # builder/publish/DB 호출 0회 (단 per-asset hook_called/skipped_disabled
            # telemetry write는 발생 — Redis HINCRBY/HSET 작은 비용. 사용자 영향 0).
            # 1개 asset 예외도 safe_publish_all_fx_snapshots가 per-asset try/except로 격리.
            await fx_topic_publisher.safe_publish_all_fx_snapshots(db)

            if manager.active_connections:
                # 3) build_graph_buckets
                t3 = time.perf_counter()
                graph_buckets = await build_graph_buckets()
                timings["build_graph_buckets_ms"] = (time.perf_counter() - t3) * 1000
                total_graph_sources = sum(len(sources) for sources in graph_buckets.values())

                if total_graph_sources:
                    payload["graph_buckets"] = graph_buckets

                # 4) manager.broadcast send (병렬, mode별 timeout)
                t4 = time.perf_counter()
                await manager.broadcast(payload, send_timeout=send_timeout)
                timings["broadcast_send_ms"] = (time.perf_counter() - t4) * 1000

                # 5) payload bytes
                payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

                broadcast_stats.record_success(
                    data_size_bytes=payload_bytes,
                    rate_count=len(payload["data"]["rates"]),
                )

                # 6) SQLAlchemy pool 상태 (가능한 경우만 — QueuePool에 한해 집계됨)
                pool_status = _get_pool_status()

                timings["job_duration_ms"] = (time.perf_counter() - job_start) * 1000
                logger.info(
                    "📡 환율 데이터 브로드캐스트 & 🅾️ Redis 업데이트 완료",
                    extra={
                        "rate_count": len(payload["data"]["rates"]),
                        "connections": len(manager.active_connections),
                        "graph_currencies": len(graph_buckets),
                        "graph_sources": total_graph_sources,
                        "payload_bytes": payload_bytes,
                        **mode_meta,
                        **timings,
                        **pool_status,
                    },
                )
            else:
                broadcast_stats.record_skip(reason="no_connections")
                timings["job_duration_ms"] = (time.perf_counter() - job_start) * 1000
                logger.info(
                    "🅾️ Redis 업데이트만 수행 (활성 연결 없음)",
                    extra={"skip_reason": "no_connections", **mode_meta, **timings},
                )
        else:
            broadcast_stats.record_skip(reason="no_changes")
            timings["job_duration_ms"] = (time.perf_counter() - job_start) * 1000
            logger.info(
                "⏸️ 변경사항 없음 - 브로드캐스트 스킵",
                extra={"skip_reason": "no_changes", **mode_meta, **timings},
            )

    except Exception as e:
        broadcast_stats.record_failure(error_message=str(e))
        logger.error(
            "❌ 브로드캐스트 오류",
            exc_info=True,
            extra={"connections": len(manager.active_connections), **mode_meta, **timings},
        )
    finally:
        db.close()


def _get_pool_status() -> Dict[str, Any]:
    """SQLAlchemy connection pool 상태를 dict로 반환.

    QueuePool 외에는 일부 메서드가 없을 수 있으므로 안전하게 처리.
    """
    pool = engine.pool
    status: Dict[str, Any] = {}
    for attr in ("size", "checkedin", "checkedout", "overflow"):
        getter = getattr(pool, attr, None)
        if callable(getter):
            try:
                status[f"pool_{attr}"] = getter()
            except Exception:
                pass
    return status



app = FastAPI(lifespan=lifespan)

# 정적 파일 서비스 (은행 아이콘 이미지)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Webhook 라우터
app.include_router(webhooks_router)

# Dependency (DB 세션 연결)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ===== WebSocket 엔드포인트 =====
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """실시간 환율 데이터 스트리밍"""
    await manager.connect(websocket)
    
    db = SessionLocal()
    try:
        cached_json = await redis_cache.get(BROADCAST_CACHE_KEY)

        if cached_json:
            # Redis 캐시 데이터 전송
            cached_payload = json.loads(cached_json)
            await websocket.send_json(cached_payload)
            logger.info("📨 Redis 캐시로 초기 데이터 전송", extra={"connections": len(manager.active_connections)})
        else:
            # DB 폴백 데이터 전송
            initial_payload = build_rates_payload(db)
            await websocket.send_json(initial_payload)

            # 캐시 미스 시 Redis에 채워 넣기
            try:
                cache_payload = build_rates_payload(db)
                await redis_cache.set(
                    BROADCAST_CACHE_KEY,
                    json.dumps(cache_payload, ensure_ascii=False),
                )
            except Exception:
                logger.debug("Redis 캐시 적재 실패 (초기 전송 후)", exc_info=True)

            logger.info("📨 DB 폴백으로 초기 데이터 전송", extra={"rate_count": len(initial_payload["data"]["rates"] )})

    except Exception:
        logger.error("❌ 초기 데이터 전송 오류", exc_info=True)
    finally:
        db.close()
    
    try:
        # 연결 유지 (클라이언트로부터 메시지 대기)
        while True:
            data = await websocket.receive_text()
            await topic_dispatcher.handle_client_message(websocket, data)
    except WebSocketDisconnect:
        logger.info("🔌 클라이언트 연결 해제")
    except Exception:
        # PR Z-2b Stage 2 (Codex 권고): send_json 실패 / handler unhandled error 등
        # 모든 비정상 경로에서도 finally로 정리. 기존엔 except WebSocketDisconnect만
        # 잡아 다른 예외 시 connection 누락 가능성.
        logger.exception("❌ WebSocket 메시지 처리 오류")
    finally:
        # PR Z-2b Stage 2: topic 구독 정리 (FF 무관 — 안전 정리, idempotent).
        # manager.disconnect도 ValueError 자체 처리하므로 어떤 경로로 진입해도 안전.
        topic_dispatcher.registry.remove_websocket(websocket)
        manager.disconnect(websocket)


# ===== REST API 엔드포인트 (폴백/초기 로드용) =====
@app.get("/api/investing/{pair}", response_model=schemas.BankExchangeRateResponse)
def get_a_latest_investing_rate(pair: str, db: Session = Depends(get_db)):
    """인베스팅 특정 통화쌍 최신 환율 조회"""
    result = crud.select_a_latest_investing_rate_from_db(db=db, pair=pair)
    if not result:
        raise HTTPException(status_code=404, detail=f"'{pair}' 통화쌍의 인베스팅 환율 데이터를 찾을 수 없습니다.")
    return result


@app.get("/api/banks/{pair}", response_model=List[schemas.BankExchangeRateResponse])
def get_a_pair_of_banks_rates(pair: str, db: Session = Depends(get_db)):
    """모든 은행의 특정 통화쌍 최신 환율 조회"""
    result = crud.select_latest_bank_rates_from_db(db=db, pair=pair)
    if not result:
        raise HTTPException(status_code=404, detail=f"'{pair}' 통화쌍의 은행 환율 데이터를 찾을 수 없습니다.")
    return result


# /api/rates Redis-first (ADR-026) — DB 커넥션 풀(pool_size 3+overflow 2=5) 고갈로 인한
# nginx `/api/` proxy_read_timeout 30s → 504를 격리. Redis read 상한(hang 방지).
_API_RATES_REDIS_TIMEOUT_S = 2.0


def _build_rates_flat_response(all_rates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """flat /api/rates 응답 구성. metadata.updated_at=현재 시각(기존 REST 계약 유지 — broadcast의
    max rate timestamp 의미로 바꾸지 않음, codex). currencies/banks는 레거시 dead field 고정값."""
    return {
        "rates": all_rates,
        "metadata": {
            "updated_at": crud.to_kst_isoformat(datetime.now(dt_timezone.utc)),
            "currencies": sorted(crud.SUPPORTED_CURRENCY_PAIRS),
            "banks": sorted(crud.LEGACY_METADATA_BANKS),
            "total_count": len(all_rates),
        },
    }


def _fetch_all_rates_flat_owning_session() -> List[Dict[str, Any]]:
    """DB fallback — worker thread에서 자체 Session 생성→조회→close (FastAPI Session을 thread로
    넘기지 않음, codex). asyncio.to_thread로 호출돼 이벤트 루프가 풀 대기에 막히지 않게 한다."""
    db = SessionLocal()
    try:
        return crud.get_all_rates_flat(db=db)
    finally:
        db.close()


@app.get("/api/rates", response_model=schemas.ExchangeRatesResponse)
async def get_rates_for_mobile():
    """모바일 앱/AJAX용 플랫 배열 API (폴백용).

    Redis-first (ADR-026 broadcast hot path 재사용): DB 커넥션 풀(5) 고갈로 /api/rates가 30초 풀
    대기 → nginx `/api/` proxy_read_timeout 30s → 504 되던 문제(2026-07-15 실측)를 격리한다.
    - Redis hit(REDIS_LATEST_ENABLED + latest mirror 신선): DB 미접촉으로 즉시 반환.
    - Redis off/miss/실패/timeout: DB fallback(worker thread에서 Session 소유 → 이벤트 루프 비차단).
    - Redis·DB 모두 실패: 빈 200이 아니라 503 — 빈 200은 클라가 성공(빈 데이터)으로 처리해 캐시
      fallback을 못 타므로(iOS A2 cache-first 계약, codex).
    """
    # 1. Redis-first (DB-free). 실패/timeout/빈 리스트는 모두 miss로 간주 → DB fallback.
    if REDIS_LATEST_ENABLED:
        redis_rates = None
        fallback_reason = None
        try:
            redis_rates, _redis_dxy, meta = await asyncio.wait_for(
                fetch_rates_from_redis(), timeout=_API_RATES_REDIS_TIMEOUT_S
            )
            fallback_reason = (meta or {}).get("fallback_reason")
        except asyncio.TimeoutError:
            fallback_reason = "redis_timeout"
            logger.warning("api_rates_redis_timeout", extra={"timeout_s": _API_RATES_REDIS_TIMEOUT_S})
        except Exception as e:
            fallback_reason = "redis_error"
            logger.warning("api_rates_redis_error", extra={"error": str(e)})
        # 비어있지 않은 리스트만 hit — 빈 리스트 []는 비정상/불완전 mirror로 보고 DB fallback(codex).
        # 정상 empty mirror는 latest:index miss로 None을 주므로 []는 기대되지 않는 상태.
        if redis_rates:
            return _build_rates_flat_response(redis_rates)
        # miss/[]/실패 — 배포 후 hit율·회귀 진단용 이유 로깅(broad catch가 Redis 회귀를 조용히
        # DB 부하로 바꾸지 않게, codex).
        logger.info("api_rates_db_fallback", extra={"reason": fallback_reason or "empty_redis"})

    # 2. DB fallback — worker thread에서 자체 Session 소유(이벤트 루프 비차단).
    try:
        all_rates = await asyncio.to_thread(_fetch_all_rates_flat_owning_session)
    except Exception as e:
        logger.warning("api_rates_db_fallback_failed", extra={"error": str(e)})
        raise HTTPException(status_code=503, detail="rates temporarily unavailable")
    return _build_rates_flat_response(all_rates)


@app.get("/api/rates/{currency}")
def get_rates_by_currency(currency: str, db: Session = Depends(get_db)):
    """특정 통화쌍의 모든 환율 조회 (모바일 앱용).

    Z-2d Step 4: topic-only 자산(usdt-krw, usd-krw-futures)은 410 Gone +
    use_topic 안내 — 새 단말은 topic API로 마이그레이션. 정책 상수는
    app.legacy_policy.LEGACY_REMOVED_RATE_TOPICS 단일 진실 소스.
    """
    # legacy에서 제거된 asset 분기 — DB 호출 전 fail-fast
    removed_detail = legacy_policy.build_legacy_removed_detail(currency)
    if removed_detail is not None:
        raise HTTPException(status_code=410, detail=removed_detail)

    try:
        rates = crud.get_rates_by_currency(db=db, currency=currency)

        if not rates:
            raise HTTPException(status_code=404, detail=f"'{currency}' 통화쌍의 환율 데이터를 찾을 수 없습니다.")

        return {
            "rates": rates,
            "metadata": {
                "updated_at": crud.to_kst_isoformat(datetime.now(dt_timezone.utc)),
                "currency": currency,
                "total_count": len(rates)
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"서버 오류: {str(e)}")


# ===== HTML 웹페이지 =====
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """WebSocket 실시간 환율 비교 웹페이지 (관리자/디버깅용)"""
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico")
async def favicon():
    """브라우저 파비콘 제공"""
    favicon_path = "static/favicon.ico"
    
    if os.path.exists(favicon_path):
        return FileResponse(
            favicon_path,
            media_type="image/x-icon",
            headers={
                "Cache-Control": "public, max-age=31536000"  # 1년 캐싱
            }
        )
    else:
        # favicon이 없을 경우 404 대신 기본 응답
        from fastapi.responses import Response
        return Response(status_code=204)  # No Content
    

@app.get("/health")
def health_check():
    """서버 상태 확인용 헬스체크 엔드포인트"""
    return {
        "status": "healthy",
        "message": "환율 서비스가 정상적으로 작동 중입니다.",
        "websocket_connections": len(manager.active_connections)
    }


# ===== 관리자 페이지 =====
@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_page(request: Request):
    """관리자 대시보드 페이지"""
    return templates.TemplateResponse(request, "admin.html")


# ===== 관리자 API 엔드포인트 =====
@app.get("/admin/api/dashboard", dependencies=[Depends(verify_admin)])
def get_dashboard():
    """
    통합 대시보드 API - 모든 모니터링 데이터를 한 번에 반환

    기존 4개 API (system-status, crawler-status, broadcast-status, log-stats)를
    하나로 통합하여 API 호출 횟수 75% 감소 (4회 → 1회)
    """
    import psutil
    import time
    from app.config import BASE_DIR
    from sqlalchemy import text

    KST = timezone('Asia/Seoul')
    now = datetime.now(KST)

    # ===== 시스템 상태 =====
    memory = psutil.virtual_memory()

    # DATABASE_URL에서 실제 DB 경로 추출 (Docker/로컬 환경 모두 호환)
    from app.database import DATABASE_URL
    from pathlib import Path as PathLib

    db_size_mb = None
    if DATABASE_URL and "sqlite:///" in DATABASE_URL:
        # sqlite:///경로 → 경로 추출 (sqlite:/// 제거)
        db_file_path = DATABASE_URL.replace("sqlite:///", "")
        db_path = PathLib(db_file_path)
        if db_path.exists():
            db_size_mb = round(db_path.stat().st_size / (1024 * 1024), 2)
        else:
            db_size_mb = 0
    elif DATABASE_URL and DATABASE_URL.startswith("postgresql"):
        db = SessionLocal()
        try:
            result = db.execute(text("SELECT pg_database_size(current_database())"))
            size_bytes = result.scalar()
            if size_bytes is not None:
                db_size_mb = round(size_bytes / (1024 * 1024), 2)
        except Exception:
            logger.debug("PostgreSQL DB 크기 조회 실패", exc_info=True)
        finally:
            db.close()

    process = psutil.Process()
    uptime_seconds = time.time() - process.create_time()
    current_mode = scheduler.current_mode or "UNKNOWN"

    system_status = {
        "websocket_connections": len(manager.active_connections),
        "memory_mb": round(memory.used / (1024 * 1024), 1),
        "memory_percent": round(memory.percent, 1),
        "db_size_mb": db_size_mb,
        "uptime_seconds": int(uptime_seconds),
        "current_mode": current_mode
    }

    # ===== 브로드캐스트 상태 =====
    broadcast_status = broadcast_stats.get_stats()

    # ===== 로그 기반 에러 카운트 (최근 1시간, ERROR + WARNING) =====
    from app.admin.log_reader import read_logs

    # 한 번만 로그를 읽어서 모든 크롤러 에러 카운트
    all_error_logs = read_logs(log_type="app", hours=1, limit=10000)

    crawler_error_counts = {}
    for log in all_error_logs:
        if log.get('level') in ['ERROR', 'WARNING']:
            logger_name = log.get('logger', '')
            # exchange_rate.crawler.{bank} 형식에서 bank 추출
            if logger_name.startswith('exchange_rate.crawler.'):
                bank = logger_name.replace('exchange_rate.crawler.', '')
                crawler_error_counts[bank] = crawler_error_counts.get(bank, 0) + 1

    # ===== 크롤러 에러 개수만 표시 (단순화) =====
    crawlers = []

    for job in scheduler.scheduler.get_jobs():
        if job.id.startswith("task_"):
            crawler_name = job.id.replace("task_", "")
            error_count = crawler_error_counts.get(crawler_name, 0)

            crawlers.append({
                "name": crawler_name,
                "error_count": error_count
            })

    # ===== 전체 에러 개수 (최근 1시간) =====
    total_errors_1h = sum(c["error_count"] for c in crawlers)

    return {
        "system": system_status,
        "broadcast": broadcast_status,
        "crawlers": crawlers,
        "errors_1h": total_errors_1h
    }




@app.get("/admin/api/logs", dependencies=[Depends(verify_admin)])
def get_admin_logs(
    log_type: str = "app",
    level: str = None,
    limit: int = 300,
    hours: int = 24,
    bank: str = None
):
    """로그 조회 (백엔드 필터링 지원)"""
    from app.admin.log_reader import read_logs

    logs = read_logs(log_type=log_type, level=level, limit=limit, hours=hours, bank=bank)
    return {"logs": logs, "total": len(logs)}




@app.get("/admin/api/download-logs", dependencies=[Depends(verify_admin)])
def download_logs(
    log_type: str = "app",
    level: str = None,
    hours: int = 24
):
    """로그 파일 다운로드"""
    from fastapi.responses import FileResponse
    from app.admin.log_reader import read_logs
    from app.config import LOG_DIR
    import tempfile
    import json

    # 로그 읽기
    logs = read_logs(log_type=log_type, level=level, limit=10000, hours=hours)

    # 임시 파일 생성
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log', encoding='utf-8') as f:
        for log in logs:
            f.write(json.dumps(log, ensure_ascii=False) + '\n')
        temp_path = f.name

    return FileResponse(
        path=temp_path,
        filename=f"{log_type}_{datetime.now().strftime('%Y%m%d')}.log",
        media_type='application/octet-stream'
    )


@app.get("/admin/api/monitor/current", dependencies=[Depends(verify_admin)])
def get_current_monitor_stats():
    """현재 시스템 모니터링 상태 조회"""
    from app.admin.monitor import system_monitor
    return system_monitor.get_current_stats()


@app.get("/admin/api/monitor/history", dependencies=[Depends(verify_admin)])
def get_monitor_history(hours: int = 1):
    """시간별 모니터링 히스토리 조회 (Chart.js용)"""
    from app.admin.monitor import system_monitor
    return system_monitor.get_history(hours=hours)


@app.get("/admin/api/crawler/stats", dependencies=[Depends(verify_admin)])
def get_crawler_stats():
    """
    크롤러별 통계 조회 (성공/실패/소요시간)

    Returns:
        {
            "investing": {
                "bank": "investing",
                "success_count": 150,
                "fail_count": 2,
                "total_duration": 245.3,
                "avg_duration": 1.6,
                "success_rate": 98.7,
                "last_success_at": "2025-11-10T14:30:00",
                "last_fail_at": null,
                "last_duration": 1.5
            },
            ...
        }
    """
    from app.admin.crawler_stats import crawler_stats
    return crawler_stats.get_stats()


@app.get("/admin/api/queue-status", dependencies=[Depends(verify_admin)])
def get_queue_status():
    """
    Selenium Priority Queue 상태 조회

    Returns:
        {
            "size": 3,
            "max_size": 25,
            "usage_percent": 12.0,
            "current_job": "shinhan",
            "waiting_jobs": [
                {
                    "bank": "ibk",
                    "priority": 3,
                    "is_retry": false,
                    "waiting_seconds": 5
                },
                ...
            ],
            "updated_at": "2025-11-13T21:30:15+09:00"
        }
    """
    from app.scheduler import queue_status_cache
    return queue_status_cache


@app.get("/admin/api/redis-status", dependencies=[Depends(verify_admin)])
async def get_redis_status():
    """
    Redis 메모리 및 상태 조회 (Phase 1.7)

    Returns:
        {
            "status": "connected" | "disconnected" | "circuit-open" | "error",
            "memory_human": "1.16M",
            "memory_bytes": 1216720,
            "maxmemory_bytes": 104857600,
            "usage_percent": 1.16,
            "keys": 2,
            "circuit_state": "closed"
        }
    """
    from app.cache import redis_cache

    # Redis 클라이언트 없음
    if not redis_cache.client:
        return {
            "status": "disconnected",
            "memory_human": "N/A",
            "memory_bytes": 0,
            "maxmemory_bytes": 104857600,  # 100MB (docker-compose.yml 설정)
            "usage_percent": 0.0,
            "keys": 0,
            "circuit_state": redis_cache.circuit.state
        }

    # Circuit Breaker 열림
    if redis_cache.circuit.state == "open":
        return {
            "status": "circuit-open",
            "memory_human": "N/A",
            "memory_bytes": 0,
            "maxmemory_bytes": 104857600,
            "usage_percent": 0.0,
            "keys": 0,
            "circuit_state": "open",
            "circuit_until": redis_cache.circuit.open_until.isoformat() if redis_cache.circuit.open_until else None
        }

    # Redis 정보 조회
    try:
        info = await redis_cache.client.info("memory")
        dbsize = await redis_cache.client.dbsize()

        used_memory = info.get("used_memory", 0)
        maxmemory = info.get("maxmemory", 104857600)
        usage_percent = round(used_memory / maxmemory * 100, 2) if maxmemory > 0 else 0.0

        return {
            "status": "connected",
            "memory_human": info.get("used_memory_human", "N/A"),
            "memory_bytes": used_memory,
            "maxmemory_bytes": maxmemory,
            "usage_percent": usage_percent,
            "keys": dbsize,
            "circuit_state": redis_cache.circuit.state
        }
    except Exception as e:
        logger.error("Redis 상태 조회 실패", exc_info=True)
        return {
            "status": "error",
            "memory_human": "Error",
            "memory_bytes": 0,
            "maxmemory_bytes": 104857600,
            "usage_percent": 0.0,
            "keys": 0,
            "circuit_state": redis_cache.circuit.state,
            "error": str(e)
        }


@app.get("/admin/api/atomic-write-control-status", dependencies=[Depends(verify_admin)])
async def get_atomic_write_control_status():
    """P1 atomic-write control plane 상태 조회 (A1 — dormant, read-only 진단 전용).

    behavior-change-0: writer/broadcast hot path를 건드리지 않는다. control table
    read 실패도 HTTP 500이 아니라 JSON control_read_error로 surface (redis-status
    never-crash 패턴). effective_mode는 control row 기준 진단값.

    ⚠️ A2 이후 writer는 control을 **consume**한다(cached snapshot enforced_action으로 gate).
    `writer_enforced`/`enforced_action`은 writer가 실제 gate하는 **cached snapshot** 기준 —
    legacy면 pass-through라 writer_enforced=false, atomic/halt면 true. (A1 docstring의
    "writer가 consume 안 함"은 A2 land로 stale — 정정.)

    Returns (항상 200):
        {status, control_available, control, effective_mode, preflight,
         enforced_action, writer_enforced, control_read_error}
    """
    from app import atomic_write_control as awc

    db = None
    control = {}
    effective_mode = None
    preflight = None
    control_read_error = None
    try:
        db = SessionLocal()  # try 안에서 open — SessionLocal() 자체 예외도 never-crash 포섭
        row = awc.read_control_row(db)
        if row is None:
            # fail-closed 진단: control row 없음 = writer라면 halt (§7). None으로 두지 않음.
            effective_mode = awc.compute_effective_mode(None)
            preflight = awc.evaluate_preflight(None)
            control_read_error = {
                "reason": "row_missing",
                "message": "atomic_write_control 싱글톤 row 없음 (migration 미실행)",
            }
        else:
            control = awc.get_control_state_dict(row)
            effective_mode = awc.compute_effective_mode(row)
            preflight = awc.evaluate_preflight(row)
    except Exception as e:
        logger.error("atomic-write control 상태 조회 실패", exc_info=True)
        # control 못 읽음 = fail-closed halt (§7). awc 미import 가능성 대비 literal.
        effective_mode = "halt"
        control_read_error = {
            "reason": type(e).__name__,
            "message": "control table을 읽을 수 없습니다",
        }
    finally:
        if db is not None:
            db.close()

    # writer가 실제 gate하는 cached snapshot 기준 enforced_action (A2 이후). snapshot()은 no-throw,
    # import만 never-crash 가드. legacy → pass-through(writer_enforced=false), atomic/halt → true.
    enforced_action = None
    try:
        from app import atomic_write_runtime
        enforced_action = atomic_write_runtime.snapshot().enforced_action
    except Exception:
        logger.error("atomic_write_runtime snapshot 조회 실패 (status endpoint)", exc_info=True)

    return {
        "status": "success" if control_read_error is None else "error",
        "control_available": control_read_error is None,
        "control": control,
        "effective_mode": effective_mode,
        "preflight": preflight,
        "enforced_action": enforced_action,  # writer가 gate하는 cached snapshot 값
        "writer_enforced": enforced_action is not None and enforced_action != awc.WriterMode.LEGACY,
        "control_read_error": control_read_error,
    }


@app.get("/admin/api/atomic-cutover-status", dependencies=[Depends(verify_admin)])
async def get_atomic_cutover_status():
    """P1b C6-9a — atomic FX cutover go/no-go 상태 (read-only 진단, behavior-change-0, dormant observability).

    cutover state(pure fresh read — live gate snapshot cache 무간섭) + C6-7 dry-run gate shadow 분포 +
    config + future 신호(available:false). atomic-write-control status는 별도 endpoint
    (/admin/api/atomic-write-control-status)로 조회. authorized 기준 always-200(per-block degraded dict).
    """
    from app.atomic_cutover_status import build_cutover_status_dict
    return await build_cutover_status_dict(SessionLocal)


@app.get("/admin/api/atomic-write-outcomes", dependencies=[Depends(verify_admin)])
async def get_atomic_write_outcomes():
    """C7-a — atomic v2 writer compare_write outcome telemetry (read-only, process-local, behavior-change-0).

    cutover flip 후 atomic writer(bank+investing)의 1차 건강 신호를 surface. crud의 write-only
    counter(`_atomic_write_outcome_counts`)를 read accessor로 노출(§19 C7-a). aggregate/per_source
    by_state + critical(conflict+failed_structural=§17 corruption=G3) + health(ok|critical) + g3_ok.
    process-local(재시작 reset, started_at으로 해석), reset route 없음. coordinator-side persisted
    counter(atomic-cutover-status future stub)와 별개 — 여기는 writer-side live counter. never-crash.
    """
    try:
        return {"status": "success", "outcomes": crud.get_atomic_write_outcome_counts()}
    except Exception:
        logger.error("atomic-write-outcomes 조회 실패", exc_info=True)
        return {"status": "error", "outcomes": None, "read_error": "outcome_read_error"}


@app.get("/admin/api/latest-mirror-outcomes", dependencies=[Depends(verify_admin)])
async def get_latest_mirror_outcomes():
    """Slice 1a (mirror-retirement measure-first) — latest mirror atomic compare_write outcome telemetry.

    mirror cycle(LATEST_MIRROR_INTERVAL_SECONDS 주기, 운영 60s)의 atomic_outcomes(advance/refreshed_equal/skipped_newer/conflict/structural 등)를
    process-local 누적으로 surface(read-only, behavior-change-0). interpretation은 3 의미 분리:
    advance=direct writer revision gap 보정(은퇴 위험) / freshness_refresh=refreshed_equal(mirrored_at
    재기록=read-path freshness 유지, 은퇴 시 대체 필요) / redundant=skipped_newer만(진짜 잉여) +
    stability_concern. = mirror-retirement go/no-go 신호. process-local
    (재시작 reset, started_at 해석), reset route 없음, never-crash. atomic writer-side counter
    (/admin/api/atomic-write-outcomes)와 별개 — 이건 mirror-side.
    """
    try:
        from app import latest_rates_cache
        return {"status": "success", "mirror_outcomes": latest_rates_cache.get_mirror_outcome_counts()}
    except Exception:
        logger.error("latest-mirror-outcomes 조회 실패", exc_info=True)
        return {"status": "error", "mirror_outcomes": None, "read_error": "mirror_outcome_read_error"}


@app.get("/admin/api/fx-shadow-counts", dependencies=[Depends(verify_admin)])
async def get_fx_shadow_counts():
    """fanout step 4 S5/S6 — FX alert shadow telemetry (read-only, process-local, behavior-change-0).

    FX_ALERT_SHADOW_ENABLED 활성 시 surface (counter는 running process in-memory, 재시작 reset, reset route 없음):
    - ✅ `legacy_match`(per bank/currency) = **parity 기준선** — legacy `triggered_items` pre-mutation
      count(crud S6b, mark_triggered 전 = legacy가 실제 발사할 대상). 신뢰 가능.
    - ⚠️ `would_fire`(per source/asset) + `shadow_stats` = **보조 진단(execution-proof), parity 기준 아님**.
      post-legacy async shadow는 legacy 발사 후 setting이 enabled=False가 되어 fresh load서 사라짐 →
      matched_candidates는 cache-hit subset만 잡힘(구조적 신뢰 불가). "새 async 경로가 prod서 무에러로
      도는가 / 어디서 빠지는가"(batch_seen/settings_loaded/matched_candidates/refetch_skipped_triggered/
      would_send) 진단 용도. **would_fire ≈ legacy_match로 parity 단정 금지.**
    - 최종 cutover 판단 = single-setting canary (S7 inline dual-compute는 매칭 byte-identical=tautological이라 폐기, open decision 7 별도).
    `shadow_enabled=false`면 누적 0(dormant). never-crash.
    """
    try:
        from app import config as app_config
        from app import crud
        from app.notifications.fx_alert_shadow import (
            get_fx_shadow_stats,
            get_fx_would_fire_counts,
        )
        counts = get_fx_would_fire_counts()
        legacy = crud.get_fx_legacy_match_counts()
        return {
            "status": "success",
            "shadow_enabled": app_config.FX_ALERT_SHADOW_ENABLED,
            "would_fire": [
                {"source": k[0], "asset": k[1], "count": v}
                for k, v in sorted(counts.items())
            ],
            "total": sum(counts.values()),
            "legacy_match": [
                {"bank": k[0], "currency": k[1], "count": v}
                for k, v in sorted(legacy.items())
            ],
            "shadow_stats": get_fx_shadow_stats(),
        }
    except Exception:
        logger.error("fx-shadow-counts 조회 실패", exc_info=True)
        return {"status": "error", "would_fire": None, "read_error": "fx_shadow_read_error"}


# ═══════════════════════════════════════════════════════════════════════════════
# KRX Status API (PR6d-2a, ADR-027) — raw frame metric / status observability
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/krx-status", dependencies=[Depends(verify_admin)])
async def get_krx_status():
    """KRX 미국달러선물 WebSocket client 상태 / metric 조회 (PR6d-2a).

    KisFuturesClient.get_metrics() 결과를 그대로 반환. client가 None
    (KRX_FUTURES_ENABLED=false 또는 bootstrap 미완)이면 enabled / started /
    reason 정도만 반환. read-only — 호출자가 polling해도 안전.

    Returns (client present):
        {"enabled": true, "client": {"status", "active_session", "contract",
         "lifecycle", "last_*_age_sec", "counters", "gap_buckets", "max_gap_sec"}}

    Returns (client absent):
        {"enabled": false, "started": false, "reason": "..."}

    ADR-027 PR6d-2a 계획 — REST fallback / topic publish / Stage 2 노출 변경 X.
    """
    from app import scheduler

    # PR6c-2d-2 (Codex 권고): reconcile shape는 enabled/started 무관 항상 동일.
    # admin UI / grep / jq가 단순해짐. disabled는 job_registered=false + last_*=null.
    reconcile_status = scheduler.get_krx_reconcile_status()

    if not config.KRX_FUTURES_ENABLED:
        return {
            "enabled": False,
            "started": False,
            "reason": "KRX_FUTURES_ENABLED=false (lifecycle 비활성)",
            "reconcile": reconcile_status,
        }

    client = getattr(scheduler, "krx_futures_client", None)
    if client is None:
        return {
            "enabled": True,
            "started": False,
            "reason": "client 미생성 — bootstrap 진행 중이거나 실패 (logs 확인)",
            "reconcile": reconcile_status,
        }

    return {
        "enabled": True,
        "started": True,
        "client": client.get_metrics(),
        "reconcile": reconcile_status,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Tether Topic Telemetry (PR Z-2b Stage 3 + Telemetry, 2026-05-10)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/topic-status", dependencies=[Depends(verify_admin)])
async def get_topic_status():
    """테더 탭 topic publish telemetry 조회.

    Redis-backed counter (재배포 시 reset 안 됨). best-effort 기록이라 Redis
    미가용 시 counter는 0/None.

    Returns:
        {
          "enabled": bool,                       # config.TOPIC_DISPATCHER_ENABLED
          "topic": str,                          # TETHER_TOPIC 상수
          "subscribed_connection_count": int,    # 현재 구독자 수
          "hook_called": int,                    # safe_publish_tether_tab_snapshot 진입
          "skipped_disabled": int,               # FF=false 차단
          "skipped_no_subscribers": int,         # subscriber 0 차단
          "built": int,                          # builder 호출 성공
          "publish_called": int,                 # publish_topic 호출
          "publish_sent_total": int,             # publish_topic sent 누적
          "publish_zero": int,                   # publish_topic이 0 반환
          "error": int,                          # safe wrapper 격리 예외
          "last_result": str | None,             # 마지막 분류
          "last_at_kst": str | None,             # 마지막 발화 KST ISO
          "last_error": str | None,              # 마지막 예외 (str(exc)[:500])
        }
    """
    return await tether_topic_publisher.get_topic_telemetry()


@app.post("/admin/api/topic-status/reset", dependencies=[Depends(verify_admin)])
async def reset_topic_status():
    """테더 탭 topic telemetry counter reset (시험 구간 분리).

    DEL topic:tether:stats. Redis 미가용 시 success=false.

    Returns:
        {"success": bool, "reason": str | None}
    """
    success = await tether_topic_publisher.reset_topic_telemetry()
    return {
        "success": success,
        "reason": None if success else "Redis 미가용 또는 circuit open / 예외",
    }


@app.get("/admin/api/krx-finalizer-stats", dependencies=[Depends(verify_admin)])
async def get_krx_finalizer_stats(days: int = 7):
    """KRX close finalizer structured event 조회 + case aggregation (2026-05-26).

    Redis `krx:close_finalizer_events` ZSET을 `days` 일 범위로 query +
    (date_kst, session) 단위 case 분류 (A/B/C/unclassified).

    Args:
        days: 조회 기간 (기본 7일). 14일 retention cap 안.

    Returns:
        {
            "since_kst": str (ISO),
            "until_kst": str (ISO),
            "total_events": int,
            "events_by_type": {event_type: count, ...},
            "closes": [{date_kst, session, types: [...], case: "A"|"B"|"C"|"unclassified"}, ...],
            "case_summary": {"A": N, "B": M, "C": K, "unclassified": L}
        }

    Case 분류 원칙 (저장 시점 X, query 시점 aggregation):
        - Case A: ws_close_saved 있음 + rest_write_blocked/rest_write_saved 둘 다 없음
        - Case B: rest_write_blocked 또는 rest_write_saved 있음 (REST가 close source 됐거나 될 뻔)
        - Case C: dedup_skipped 있음 (현 구현 catalog 외, 0건 예상)
        - unclassified: 그 외 (rest_fallback_attempted 단독, returned_none/missing/parse_failed/sanity_aborted/write_failed 등 — 새 분기/관찰 신호)

    no-throw: Redis 장애 또는 parse 실패 시 빈 결과 반환 (catastrophic backup 안전).
    """
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone as _tz
    from app import latest_rates_cache

    if days < 1:
        days = 1
    if days > 14:
        days = 14  # retention cap

    kst = _tz(timedelta(hours=9))
    now_kst = datetime.now(kst)
    since_kst = now_kst - timedelta(days=days)
    since_ms = int(since_kst.timestamp() * 1000)
    until_ms = int(now_kst.timestamp() * 1000)

    events = latest_rates_cache.get_krx_close_events(
        since_epoch_ms=since_ms, until_epoch_ms=until_ms,
    )

    events_by_type: Dict[str, int] = defaultdict(int)
    by_close: Dict[tuple, list] = defaultdict(list)
    for e in events:
        # Defensive normalize — malformed-but-valid JSON (event_type None / non-str) 방어.
        # str(None) → "None" 방지 위해 falsy check 우선.
        et = str(e.get("event_type") or "unknown")
        events_by_type[et] += 1
        date_kst_v = str(e.get("date_kst") or "unknown")
        session_v = str(e.get("session") or "unknown")
        by_close[(date_kst_v, session_v)].append(e)

    closes = []
    case_summary: Dict[str, int] = defaultdict(int)
    for (date_kst, session), evs in sorted(by_close.items()):
        types = {str(e.get("event_type") or "unknown") for e in evs}
        if "ws_close_saved" in types and not (types & {"rest_write_blocked", "rest_write_saved"}):
            case = "A"
        elif types & {"rest_write_blocked", "rest_write_saved"}:
            case = "B"
        elif "dedup_skipped" in types:
            case = "C"
        else:
            case = "unclassified"
        case_summary[case] += 1
        closes.append({
            "date_kst": date_kst,
            "session": session,
            "types": sorted(types),
            "case": case,
            "event_count": len(evs),
        })

    return {
        "since_kst": since_kst.isoformat(),
        "until_kst": now_kst.isoformat(),
        "days": days,
        "total_events": len(events),
        "events_by_type": dict(events_by_type),
        "closes": closes,
        "case_summary": dict(case_summary),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FX Topic Telemetry (PR Z-2c Step 3, 2026-05-12)
#
# Routing 경계: 현재 /admin/api/topic-status 계열은 모두 static path
# (/topic-status, /topic-status/reset, /topic-status/fx, /topic-status/fx/reset).
# 미래에 /admin/api/topic-status/{topic} 같은 path param 추가 금지 — 'reset',
# 'fx' 등 기존 static segment와 매칭 충돌 발생. 신규 domain group은 /topic-status/<name>
# (예: /topic-status/krx) 패턴으로 명시 segment 추가하는 쪽이 일관.
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/topic-status/fx", dependencies=[Depends(verify_admin)])
async def get_fx_topic_status():
    """FX 3 topic (fx:usd-krw/jpy-krw/eur-krw) telemetry 일괄 조회.

    Redis-backed counter per topic. best-effort 기록이라 Redis 미가용 시 counter
    는 0/None. tether endpoint(/admin/api/topic-status)와 분리 — 기존 호출자
    영향 없음.

    Returns:
        {
          "fx:usd-krw": {
            "enabled": bool,                       # FX_TOPIC_ENABLED AND TOPIC_DISPATCHER_ENABLED
            "fx_topic_enabled": bool,              # config.FX_TOPIC_ENABLED
            "topic_dispatcher_enabled": bool,      # config.TOPIC_DISPATCHER_ENABLED
            "topic": "fx:usd-krw",
            "asset": "usd-krw",
            "subscriber_count": int,               # 해당 topic 구독자 수
            "hook_called": int, "skipped_disabled": int, "skipped_no_subscribers": int,
            "built": int, "publish_called": int, "publish_sent_total": int,
            "publish_zero": int, "error": int,
            "last_result": str | None,
            "last_at_kst": str | None,
            "last_error": str | None,
            # C1 trigger 측 카운터 (fx_topic_trigger가 같은 hash에 trigger_ prefix로 기록):
            "trigger_request": int, "trigger_flush_direct": int,
            "trigger_publish_success": int, "trigger_error": int,
            "trigger_tether_route_shadow": int, ...(Redis-backed trigger counter 전체),
            "trigger_last_result": str | None, "trigger_last_reason": str | None,
            "trigger_last_source": str | None, ...(trigger_last_* 전체),
            # NOTE: trigger_no_loop은 미노출 — loop 부재 시만 발생해 Redis 미기록
            #   (in-process no_loop_skipped만 증가). 노출 시 항상 0이라 오해 소지라 제외.
          },
          "fx:jpy-krw": {...},
          "fx:eur-krw": {...},
        }
    """
    return await fx_topic_publisher.get_fx_topic_telemetry()


@app.post("/admin/api/topic-status/fx/reset", dependencies=[Depends(verify_admin)])
async def reset_fx_topic_status():
    """FX 3 topic telemetry counter 일괄 reset (시험 구간 분리).

    DEL topic:fx:<asset>:stats × 3. Redis 미가용 시 per-asset success=false.

    Returns:
        {
          "results": {"usd-krw": bool, "jpy-krw": bool, "eur-krw": bool},
          "success": bool,                       # 3개 모두 성공 시 true
          "reason": str | None,                  # 부분/전체 실패 사유
        }
    """
    results = await fx_topic_publisher.reset_fx_topic_telemetry()
    all_ok = all(results.values())
    return {
        "results": results,
        "success": all_ok,
        "reason": None if all_ok else "Redis 미가용 또는 circuit open / 예외 (일부 또는 전체)",
    }


@app.get("/admin/api/bank-investing-redis-stats", dependencies=[Depends(verify_admin)])
async def get_bank_investing_redis_stats():
    """Bank/Investing direct-SET outcome telemetry (item 4).

    set_latest_bank/investing_rate_from_sync_job의 Redis SET 시도/성공/실패(원인 3종:
    client_unavailable / writer_exception / set_exception)를 process-local로 집계한
    snapshot. PR D(legacy FX hook 격하)의 운영 계측 축 — SET 실패 빈도·원인 관찰.

    Redis 미저장 (Redis 장애 순간의 실패까지 기록해야 해 Redis를 telemetry sink로
    쓰면 바로 그 실패가 유실 — writer_exception은 Redis 무관하나 일관성 위해 동일
    sink) → in-process counter 직접 read. **process 재시작 시 reset** (started_at으로
    누적 구간 해석). reset route 없음 (운영 실수 회피 — reset_stats는 테스트 전용).

    get_stats()는 deepcopy 1회(작은 dict)라 동기 호출 (event loop 차단 무시 가능).

    Returns:
        {
          "started_at": str,                       # 누적 시작(KST ISO, 재시작 시 갱신)
          "aggregate": {"attempt", "success", "failure",
                        "failure_by_reason": {client_unavailable, writer_exception,
                                              set_exception, unknown}},
          "per_source": {
            "<source>": {                          # bank명(kb/hana/...) 또는 "investing"
              "aggregate": {...},
              "per_asset": {"<asset>": {attempt, success, failure, failure_by_reason,
                consecutive_failures, last_attempt_at, last_success_at,
                last_failure_at, last_failure_reason, last_failure_error}},
            }, ...
          },
        }

    운영 sanity check (correctness invariant 아님): telemetry record 유실이 없고
    writer가 정지된 시점에 한해 aggregate.attempt == success + failure가 기대됨.
    차이의 두 원인 — (1) in-flight writer: record_attempt가 먼저 +1된 뒤 success/
    failure +1 전 동시 조회 시 attempt가 일시적으로 앞섬, (2) best-effort telemetry
    기록 유실(_safe_record_*가 예외를 삼킴): 정지 시점에도 등식이 영구적으로 어긋날
    수 있음. 따라서 정확한 무결성 척도가 아닌 운영 점검 지표.
    """
    return bank_investing_redis_stats.get_stats()


# ═══════════════════════════════════════════════════════════════════════════════
# USDT Redis-first path stats (PR Z-2e B-Step Telemetry, 2026-05-13)
#
# ADR-029 명시 trade-off("direct write 영구 실패 시 영구 stale 위험 — 별도
# telemetry 필요") future enhancement를 닫는 1차 계측. process-bound counter라
# 재시작 시 reset — started_at으로 카운터 누적 시작 시각 명시.
# reset endpoint 미존재 (운영 실수 회피, reset_stats는 테스트 전용 모듈 함수).
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/usdt-redis-stats", dependencies=[Depends(verify_admin)])
async def get_usdt_redis_stats():
    """USDT Redis-first 경로(direct write + sync read + DB fallback) 계측 조회.

    Returns:
        {
          "started_at": ISO 8601 KST,        # process 시작 시점 (counter 누적 시작)
          "per_source": {
            "<source>": {
              "direct_write_success": int, "direct_write_failure": int,
              "direct_write_regression_skipped": int,
              "redis_read_hit": int, "redis_read_miss": int,
              "redis_read_parse_fail": int, "redis_read_error": int,
              "last_direct_write_success_at": ISO 8601 KST | None,
              "last_redis_read_hit_at": ISO 8601 KST | None,
            }, ...
          },
          "aggregate": {
            "db_fallback_count": int,
            "last_db_fallback_at": ISO 8601 KST | None,
            "db_fallback_by_asset": {"usdt-krw": int, ...},
          }
        }

    정상 운영 invariant:
        - direct_write_success 증가, direct_write_failure ≈ 0
        - redis_read_hit 증가, miss/parse_fail/error 낮음
        - db_fallback_count 낮음 (Redis 모두 hit이 정상)
    """
    return usdt_redis_stats.get_stats()


# ═══════════════════════════════════════════════════════════════════════════════
# Crawler Toggle API (Phase 1.8)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/api/crawler-config", dependencies=[Depends(verify_admin)])
async def get_crawler_config():
    """
    크롤러 활성화/비활성화 설정 조회 (Phase 1.8)

    Returns:
        {
            "status": "success",
            "configs": [
                {"crawler_name": "investing", "enabled": true, "updated_at": "2025-11-27T10:00:00+09:00"},
                {"crawler_name": "kb", "enabled": false, "updated_at": "2025-11-27T10:00:00+09:00"},
                ...
            ]
        }
    """
    db = SessionLocal()
    try:
        configs = crud.get_all_crawler_configs(db)
        return {"status": "success", "configs": configs}
    except Exception as e:
        logger.error("크롤러 설정 조회 실패", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/admin/api/crawler-config", dependencies=[Depends(verify_admin)])
async def toggle_crawler(request: Request):
    """
    크롤러 활성화/비활성화 토글 (Phase 1.8)

    Body:
        {
            "crawler_name": "kb",
            "enabled": false
        }

    Returns:
        {
            "status": "success",
            "message": "kb 크롤러가 비활성화되었습니다"
        }
    """
    from app.scheduler import crawler_manager

    data = await request.json()
    crawler_name = data.get("crawler_name")
    enabled = data.get("enabled")

    if not crawler_name or enabled is None:
        raise HTTPException(status_code=400, detail="crawler_name과 enabled 필드가 필요합니다")

    try:
        crawler_manager.toggle_crawler(crawler_name, enabled)
        return {
            "status": "success",
            "message": f"{crawler_name} 크롤러가 {'활성화' if enabled else '비활성화'}되었습니다"
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"크롤러 토글 실패: {crawler_name}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═════════════════════════════════════════════════════════════
# 뉴스 API (Phase 1B v2)
# ═════════════════════════════════════════════════════════════

from app.news.filters import normalize_title as _normalize_title

_CONTENT_TYPE_PRIORITY = {"external_link": 3, "report_pdf": 4}
_TAIL_PATTERN = __import__("re").compile(r'\((상보|종합|속보|수정|1보|2보|3보|본문없음)\)')


def _collapse_near_duplicates(items: list, gap_minutes: int = 60) -> list:
    """
    초보/상보 near-duplicate collapse (시간 클러스터 방식).
    조건: 클러스터 내 꼬리표(상보/종합) 또는 is_bodyless 기사가 있을 때만 collapse.
    """
    if not items:
        return items

    groups: dict = {}

    for i, item in enumerate(items):
        raw_title = item.get("title", "")
        norm = _normalize_title(raw_title)
        ct_priority = _CONTENT_TYPE_PRIORITY.get(item.get("content_type", ""), 0)

        has_tail = bool(_TAIL_PATTERN.search(raw_title.replace("&quot;", '"')))
        is_bodyless = item.get("_is_bodyless", False)

        if has_tail:
            ct_priority += 10

        groups.setdefault(norm, []).append(
            (i, ct_priority, has_tail or is_bodyless, item.get("published_at", ""))
        )

    keep_indices: set = set()

    for norm, candidates in groups.items():
        if len(candidates) == 1:
            keep_indices.add(candidates[0][0])
            continue

        try:
            sorted_cands = sorted(candidates, key=lambda c: c[3])
        except TypeError:
            for c in candidates:
                keep_indices.add(c[0])
            continue

        clusters = [[sorted_cands[0]]]
        for c in sorted_cands[1:]:
            prev = clusters[-1][-1]
            try:
                t_prev = datetime.fromisoformat(prev[3])
                t_curr = datetime.fromisoformat(c[3])
                gap = abs((t_curr - t_prev).total_seconds()) / 60
            except (ValueError, TypeError):
                gap = 0
            if gap <= gap_minutes:
                clusters[-1].append(c)
            else:
                clusters.append([c])

        for cluster in clusters:
            if len(cluster) == 1:
                keep_indices.add(cluster[0][0])
                continue

            any_collapsible = any(c[2] for c in cluster)
            if not any_collapsible:
                for c in cluster:
                    keep_indices.add(c[0])
                continue

            best = max(cluster, key=lambda c: (c[1], c[3]))
            keep_indices.add(best[0])

    return [items[i] for i in range(len(items)) if i in keep_indices]


@app.get("/api/news", response_model=schemas.NewsResponse)
async def get_news(
    limit: int = 100,
    hours: float = 24.0,
):
    """뉴스 목록 반환 (Redis 캐시 기반, 시간순)"""
    limit = max(1, min(100, limit))
    hours = max(0.5, min(24.0, hours))

    cutoff_ts = time.time() - (hours * 3600)

    nsids = await redis_cache.zrevrangebyscore("news:index", "+inf", cutoff_ts)

    now_iso = datetime.now(dt_timezone(timedelta(hours=9))).isoformat()

    if not nsids:
        return {
            "news": [],
            "metadata": {"returned_count": 0, "window_hours": hours, "responded_at": now_iso},
        }

    news_items = []

    for nsid in nsids:
        item = await redis_cache.hgetall(f"news:item:{nsid}")
        if not item:
            continue

        content_type = item.get("content_type", "external_link")
        entry = {
            "id": nsid,
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "content_type": content_type,
            "published_at": item.get("published_at", ""),
            "link": item.get("link") or None,
        }

        # collapse용 내부 플래그 (API 응답에서 제거됨)
        if item.get("is_bodyless") == "true":
            entry["_is_bodyless"] = True

        news_items.append(entry)

    news_items = _collapse_near_duplicates(news_items)
    news_items = news_items[:limit]

    # 내부 플래그 제거
    for item in news_items:
        item.pop("_is_bodyless", None)

    return {
        "news": news_items,
        "metadata": {
            "returned_count": len(news_items),
            "window_hours": hours,
            "responded_at": now_iso,
        },
    }

    return {
        "news": news_items,
        "metadata": {
            "returned_count": len(news_items),
            "window_hours": hours,
            "responded_at": now_iso,
        },
    }


# ═════════════════════════════════════════════════════════════
# 그래프 API (Phase 1A) - 계층적 Fallback 전략
# ═════════════════════════════════════════════════════════════
@app.get("/api/graph/{currency}")
async def get_graph_data(currency: str, range: str = "1d"):
    """
    그래프 데이터 반환

    Args:
        currency: "usd-krw" | "jpy-krw" | "eur-krw"
        range: "1d" | "1w" | "3m" | "1y" (기본값: "1d")

    Returns:
        {
            "pair": "usd-krw",
            "period": "1d",
            "bucket_size": "10m",
            "as_of": "...",
            "sources": {
                "investing": [[ts, max, min, close], ...],
                "kb": [...], "hana": [...],
                "dxy": [...]  // USD/KRW만
            }
        }

    1d Fallback 순서:
        1. Redis 캐시 (120초 TTL)
        2. 인메모리 캐시 (60초 TTL)
        3. DB 조회 (Rate Limiting: 10초에 1번)
        4. 503 Service Unavailable

    1w/3m/1y: Redis lazy 캐시 → DB 조회
    """
    if currency not in ["usd-krw", "jpy-krw", "eur-krw"]:
        raise HTTPException(status_code=400, detail="Invalid currency pair")

    if range not in ["1d", "1w", "3m", "1y"]:
        raise HTTPException(status_code=400, detail="Invalid range. Use: 1d, 1w, 3m, 1y")

    # 장기 그래프 (1w/3m/1y)는 별도 핸들러
    if range != "1d":
        return await _get_period_graph_data(currency, range)

    now = time.time()

    # Tier 1: Redis 캐시
    redis = redis_cache.client  # await 제거 (client는 속성, 코루틴 아님)
    cache_key = f"graph:{currency}"

    if redis:
        try:
            cached = await redis_cache.get(cache_key)
            if cached:
                cache_obj = json.loads(cached)

                logger.info(
                    f"✅ Redis 캐시 히트",
                    extra={"currency": currency, "tier": "redis"}
                )

                return {
                    "pair": currency,
                    "period": "1d",
                    "bucket_size": "10m",
                    "as_of": datetime.fromtimestamp(
                        cache_obj["data_timestamp"],
                        tz=KST
                    ).isoformat(),
                    "sources": cache_obj["data"]
                }
        except Exception as e:
            logger.warning(f"Redis 조회 실패: {e}")

    # Tier 2: 인메모리 캐시 (60초 TTL)
    if currency in _memory_cache:
        cache_age = now - _cache_timestamps.get(currency, 0)

        if cache_age < 60:
            logger.info(
                f"✅ 메모리 캐시 히트",
                extra={"currency": currency, "cache_age": cache_age, "tier": "memory"}
            )

            response = _memory_cache[currency].copy()
            return response
        else:
            del _memory_cache[currency]
            del _cache_timestamps[currency]

    # Tier 3: DB 직접 조회 (Rate Limiting)
    logger.warning(
        f"⚠️ 캐시 미스, DB 조회 시도",
        extra={"currency": currency, "tier": "database"}
    )

    last_db_query = _db_query_timestamps.get(currency, 0)
    time_since_last = now - last_db_query

    if time_since_last < 10:
        logger.error(
            f"❌ DB 조회 Rate Limit",
            extra={
                "currency": currency,
                "time_since_last": time_since_last,
                "retry_after": 10 - time_since_last
            }
        )

        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service temporarily unavailable",
                "reason": "Cache refresh in progress",
                "retry_after": int(10 - time_since_last)
            }
        )

    _db_query_timestamps[currency] = now

    try:
        from concurrent.futures import ThreadPoolExecutor
        import asyncio

        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_event_loop()

        def _fetch_graph():
            from app.admin.graph_cache import build_graph_series, build_dxy_graph_series

            sources_data = {}
            max_timestamp = 0

            for source in ["investing", "kb", "hana"]:
                series, latest_ts = build_graph_series(source, currency)
                sources_data[source] = series
                if latest_ts > max_timestamp:
                    max_timestamp = latest_ts

            # DXY는 USD/KRW만
            if currency == "usd-krw":
                dxy_series, dxy_ts = build_dxy_graph_series()
                if dxy_series:
                    sources_data["dxy"] = dxy_series
                    if dxy_ts > max_timestamp:
                        max_timestamp = dxy_ts

            return sources_data, max_timestamp

        try:
            sources_data, max_timestamp = await loop.run_in_executor(executor, _fetch_graph)

            response = {
                "pair": currency,
                "period": "1d",
                "bucket_size": "10m",
                "as_of": datetime.fromtimestamp(max_timestamp, tz=KST).isoformat(),
                "sources": sources_data
            }

            # 인메모리 캐시 저장
            _memory_cache[currency] = response.copy()
            _cache_timestamps[currency] = now

            # 메모리 캐시 크기 제한 (최대 3개)
            if len(_memory_cache) > 3:
                oldest_key = min(_cache_timestamps, key=_cache_timestamps.get)
                del _memory_cache[oldest_key]
                del _cache_timestamps[oldest_key]

            logger.info(
                f"✅ DB 조회 성공",
                extra={"currency": currency, "data_timestamp": max_timestamp}
            )

            return response
        finally:
            # 스레드 누수 방지 (Critical)
            executor.shutdown(wait=False)

    except Exception as e:
        logger.exception(f"❌ DB 조회 실패", extra={"currency": currency})

        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service temporarily unavailable",
                "reason": "Database query failed"
            }
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 장기 그래프 (1w/3m/1y)
# ═══════════════════════════════════════════════════════════════════════════════

# 장기 그래프 캐시 (인메모리)
_period_cache: Dict[str, dict] = {}          # key: "graph:{currency}:{range}"
_period_cache_timestamps: Dict[str, float] = {}

PERIOD_CACHE_TTL = {
    "1w": 600,     # 10분
    "3m": 3600,    # 1시간
    "1y": 3600,    # 1시간
}

BUCKET_SIZE_LABELS = {
    "1d": "10m",
    "1w": "1h",
    "3m": "1d",
    "1y": "1d",
}


async def _get_period_graph_data(currency: str, period: str):
    """
    장기 그래프 데이터 반환 (1w/3m/1y).
    Redis lazy 캐시 → 인메모리 캐시 → DB 조회.
    """
    cache_key = f"graph:{currency}:{period}"
    now = time.time()
    ttl = PERIOD_CACHE_TTL[period]

    # Tier 1: Redis 캐시
    redis = redis_cache.client
    if redis:
        try:
            cached = await redis_cache.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    # Tier 2: 인메모리 캐시
    if cache_key in _period_cache:
        cache_age = now - _period_cache_timestamps.get(cache_key, 0)
        if cache_age < ttl:
            return _period_cache[cache_key]
        else:
            _period_cache.pop(cache_key, None)
            _period_cache_timestamps.pop(cache_key, None)

    # Tier 3: DB 조회
    from concurrent.futures import ThreadPoolExecutor
    import asyncio
    from app.admin.graph_cache import (
        build_period_exchange_series, build_period_dxy_series, PERIOD_CONFIG,
    )

    config = PERIOD_CONFIG[period]

    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_event_loop()

    def _fetch():
        sources_data = {}
        max_ts = 0

        for source in config["exchange_sources"]:
            series, latest_ts = build_period_exchange_series(source, currency, period)
            # 장기(1w+)에서 investing은 "reference" 키로 반환
            key = "reference" if source == "investing" and period != "1d" else source
            sources_data[key] = series
            if latest_ts > max_ts:
                max_ts = latest_ts

        # DXY는 USD/KRW만
        if currency in config["dxy_currencies"]:
            dxy_series, dxy_ts = build_period_dxy_series(period)
            if dxy_series:
                sources_data["dxy"] = dxy_series
                if dxy_ts > max_ts:
                    max_ts = dxy_ts

        return sources_data, max_ts

    try:
        sources_data, max_timestamp = await loop.run_in_executor(executor, _fetch)

        response = {
            "pair": currency,
            "period": period,
            "bucket_size": BUCKET_SIZE_LABELS[period],
            "as_of": datetime.fromtimestamp(max_timestamp, tz=KST).isoformat() if max_timestamp else None,
            "sources": sources_data,
        }

        # 캐시 저장 (인메모리 + Redis)
        _period_cache[cache_key] = response
        _period_cache_timestamps[cache_key] = now

        if redis:
            try:
                await redis_cache.set(cache_key, json.dumps(response), ex=ttl)
            except Exception:
                pass

        return response
    finally:
        executor.shutdown(wait=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Graph API v2 (ADR-035 Phase 2e MVP) — catalog + tab (source_daily_rates read)
#   로직은 app/graph_v2.py (endpoint 없이 12 tests 완결). 여기는 thin wiring만.
#   v1 /api/graph/{currency}는 변경 0 (legacy 공존, GRAPH §12). cache(§11)는 후속 optional.
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/v2/graph/catalog")
async def get_v2_graph_catalog():
    """전체 catalog (tab × period × series). MVP = 3m/1y subset. DB 불필요 (정적 상수)."""
    from app.graph_v2 import build_catalog
    return build_catalog()


# v2 graph tab read-through 캐시 TTL(초) — 데이터 변경주기(1w 시간/3m·1y 하루)보다 짧게(보수적 시작).
# 문제 없으면 확대 가능. live-tail이 client-side(topic)라 캐시 stale해도 그래프 끝 현재값엔 영향 없음.
_GRAPH_V2_CACHE_TTL_SECONDS = {"1w": 300, "3m": 1800, "1y": 1800}


# intraday 1d miss-rebuild single-flight (process-local, per-tab). precompute(scheduler */10)가 캐시를
# warm하게 유지하므로 miss는 드묾(cold start / cron 사망 + TTL 만료). ⚠️ 현 prod는 --workers 1이라
# process-local로 충분 — multi-worker 전환 시 Redis SET NX EX 필요(redis_cache.set은 nx 미지원 → 별도 구현).
# lock은 tab별 lazy 생성(setdefault — 단일 이벤트 루프라 원자적).
_intraday_1d_rebuild_locks: dict = {}
_intraday_1d_in_progress_locks: dict = {}
# ADR-039 무료 snapshot — process-local last-good canonical (key -> payload).
# serve는 **cron이 만든 canonical만** 서빙하고 DB로 재생성하지 않는다(무료=1시간 고정 불변식). local은 Redis
# 성공 read값을 보존해 Redis 장애 시 마지막 canonical을 반환하기 위한 것(만료 없음, 다음 성공 read가 덮어씀).
_free_snapshot_local: dict = {}


async def _serve_intraday_1d_cached(tab: str):
    """탭 1d 응답 = closed-bucket payload + optional `in_progress` seed(현재 10분봉 high/low/close).

    closed는 precompute 캐시(완료봉만). in_progress는 additive short-TTL cache-aside — cold-open/resync
    시 클라가 현재 봉 앞부분 high/low를 못 보는 문제 해소용 seed. 실패해도 seed만 생략(closed 정상),
    클라는 seed로 현재 봉을 채우고 없으면 client-only 누적 fallback. graph_v2_intraday.
    """
    payload = await _get_intraday_1d_closed(tab)
    payload["in_progress"] = await _get_intraday_1d_in_progress(tab)   # {} 가능(데이터 없음/실패) → 클라 fallback
    return payload


async def _get_intraday_1d_closed(tab: str) -> dict:
    """closed-bucket payload — precompute key read, miss/stale 시 lock 아래 1회 rebuild(process-local single-flight).

    stale = 경계 통과 후 precompute(:12) 전 창. 캐시의 `_in_progress_start_ts`(build 시점 경계)가 현재
    경계보다 작으면, 방금 닫힌 봉이 아직 캐시에 없음(precompute 미실행) → 온디맨드 rebuild로 즉시 반영해
    cold-open ~12초 gap 제거(사용자 실측: 경계 직후 재실행 시 직전 완료 봉 누락). 그 외엔 캐시 read(빠름).
    """
    import time

    from app.graph_v2_intraday import (
        CACHE_TTL_SECONDS,
        _bucket_align,
        build_tab_1d_payload,
        cache_key_1d,
    )

    cache_key = cache_key_1d(tab)
    current_boundary = _bucket_align(int(time.time()))

    def _fresh(payload) -> bool:
        # 캐시가 현재 경계 이후에 build됐으면(방금 닫힌 봉 포함) fresh. 구 캐시(_in_progress_start_ts 부재)는
        # 0 → stale 간주 → 배포 직후 1회 rebuild로 self-heal. non-dict JSON(내부 writer만 쓰나 방어)은
        # isinstance로 걸러 rebuild(500 회피, codex hardening).
        return isinstance(payload, dict) and payload.get("_in_progress_start_ts", 0) >= current_boundary

    cached = await redis_cache.get(cache_key)
    if cached is not None:
        try:
            payload = json.loads(cached)
            if _fresh(payload):
                return payload
            # stale-by-one-bucket (경계 통과, precompute 전) → 아래서 rebuild
        except (ValueError, TypeError):
            logger.warning("graph_v2 %s 1d 캐시 parse 실패 — rebuild", tab)

    async with _intraday_1d_rebuild_locks.setdefault(tab, asyncio.Lock()):
        # lock 대기 중 다른 요청/precompute가 fresh하게 채웠을 수 있음 → double-check (중복 build 회피)
        cached = await redis_cache.get(cache_key)
        if cached is not None:
            try:
                payload = json.loads(cached)
                if _fresh(payload):
                    return payload
            except (ValueError, TypeError):
                pass
        payload = await asyncio.to_thread(build_tab_1d_payload, tab)
        await redis_cache.set(cache_key, json.dumps(payload), ex=CACHE_TTL_SECONDS)
        return payload


async def _get_intraday_1d_in_progress(tab: str) -> dict:
    """진행 중(현재) 10분봉 seed — short-TTL cache-aside(miss 시 lock 아래 현재 봉만 재계산).
    실패는 격리: 빈 dict 반환 → 클라는 seed 없이 client-only 누적 fallback(closed 그래프는 정상)."""
    from app.graph_v2_intraday import (
        IN_PROGRESS_TTL_SECONDS,
        build_tab_1d_in_progress,
        cache_key_1d_in_progress,
    )

    cache_key = cache_key_1d_in_progress(tab)
    try:
        cached = await redis_cache.get(cache_key)
        if cached is not None:
            return json.loads(cached)
        async with _intraday_1d_in_progress_locks.setdefault(tab, asyncio.Lock()):
            cached = await redis_cache.get(cache_key)
            if cached is not None:
                return json.loads(cached)
            seed = await asyncio.to_thread(build_tab_1d_in_progress, tab)
            await redis_cache.set(cache_key, json.dumps(seed), ex=IN_PROGRESS_TTL_SECONDS)
            return seed
    except Exception:
        logger.warning("graph_v2 %s 1d in_progress seed 실패 — 생략(client-only fallback)", tab, exc_info=True)
        return {}


@app.get("/api/v2/graph/tab")
async def get_v2_graph_tab(tab: str, response: Response, period: str = "3m"):
    """탭×기간 모든 series 데이터. period∈{3m,1y,1w} = source_daily/hourly_rates read-through.
    period=1d = intraday 탭(테더+usd/jpy/eur) 10min closed-bucket precompute(graph_v2_intraday)."""
    from app.graph_v2 import (
        MVP_PERIODS,
        attach_graph_v2_domain,
        build_tab,
        is_supported_period,
        known_tabs,
        period_domain,
        strip_krx_if_not_allowed,
    )

    # 라이브 그래프(1d 10min 진행봉 + in_progress seed, 3m/1y/1w도 매일/매시 갱신)는 클라가 HTTP
    # 캐시하면 cold-open에 stale 응답을 내줌 → no-store로 어떤 캐시 레이어도 저장 안 하게 함. iOS
    # URLCache가 cache 헤더 없는 200 GET을 휴리스틱 캐싱해 cold-launch에 직전 세션 그래프를 ~2-3분
    # 내주던 문제(닫힌 봉+seed 둘 다 지연) 대응. 속도는 서버 Redis 캐시가 담당(클라 캐시 불필요).
    # 성공(dict) 응답에만 적용(에러 JSONResponse는 자체 반환이라 미적용, 무해).
    response.headers["Cache-Control"] = "no-store"

    if tab not in known_tabs():
        return JSONResponse(status_code=404, content={
            "error": "unknown_tab",
            "detail": f"tab '{tab}' not found",
            "known_tabs": known_tabs(),
        })

    # serve-time now — 요청당 1회(start/end 동일 anchor). 404 이후·모든 성공 경로 공통 domain 프레임(ADR-039 X축 통일).
    anchor = datetime.now(KST)

    # 1d — intraday 지원 탭(테더 + usd/jpy/eur, 10min precompute). 그 외 탭은 400 + v1 hint(미래 탭 방어).
    if period == "1d":
        from app.graph_v2_intraday import INTRADAY_TABS

        if tab not in INTRADAY_TABS:
            return JSONResponse(status_code=400, content={
                "error": "unsupported_period",
                "detail": f"period '1d' is not supported for tab '{tab}' in Graph API v2",
                "supported_periods": list(MVP_PERIODS),
                "fallback": {
                    "type": "legacy_graph_api",
                    "hint": "Use legacy /api/graph/{currency}?range=1d",
                },
            })
        payload = await _serve_intraday_1d_cached(tab)
    elif is_supported_period(period):
        # read-through Redis cache (graph_v2:tab:{tab}:{period}) — 동일 tab×period는 사용자 공통 +
        # daily/hourly 저빈도 변경이라 캐시 효율 높음. Redis 장애/circuit-open 시 get None → miss →
        # DB build로 자동 fallback (cache.py Circuit Breaker). live-tail은 client-side(topic)라 캐시가
        # stale해도 그래프 끝 현재값엔 영향 없음. TTL-only(cron 무효화 없음). 404/400은 위에서 bypass.
        # ⚠️ Redis에 굽는 건 domain 없는 원본만(캐시 date-less이라 자정 넘어 hit해도 stale domain 방지) — domain은 아래 serve-time attach.
        cache_key = f"graph_v2:tab:{tab}:{period}"
        cached = await redis_cache.get(cache_key)
        payload = None
        if cached is not None:
            try:
                parsed = json.loads(cached)
            except (ValueError, TypeError):
                # corrupt 캐시 → miss로 처리하고 rebuild (다음 set으로 자연 복구, delete 불필요)
                parsed = None
                logger.warning("graph_v2 캐시 parse 실패 — rebuild", extra={"cache_key": cache_key})
            if isinstance(parsed, dict):
                payload = parsed
            elif parsed is not None:
                # JSON-valid non-dict([], "x", 1)도 miss로 처리 — attach가 mapping 전제(1d 경로 isinstance 가드와 대칭).
                logger.warning("graph_v2 캐시 non-dict — rebuild", extra={"cache_key": cache_key})
        if payload is None:
            def _build():
                db = SessionLocal()
                try:
                    # today_kst=anchor.date() — domain(period_domain(anchor))과 데이터 window(period_range)를
                    # **같은 anchor 날짜**로 고정. 미전달 시 build_tab이 자체 datetime.now()를 재호출 → 요청이
                    # KST 자정을 가로지르면 domain frameStart와 data start가 1일 어긋나 carry_in seed가 backdated
                    # (codex blocker). 캐시-hit-stale 경우는 client `first.ts > frameStart` 가드가 seed 스킵해 안전.
                    return build_tab(db, tab, period, today_kst=anchor.date())
                finally:
                    db.close()

            payload = await asyncio.to_thread(_build)
            await redis_cache.set(
                cache_key, json.dumps(payload), ex=_GRAPH_V2_CACHE_TTL_SECONDS.get(period, 1800)
            )
    else:
        # MVP 범위 밖 — insufficient_history 아님, 명시적 미지원 + v1 fallback hint
        return JSONResponse(status_code=400, content={
            "error": "unsupported_period",
            "detail": f"period '{period}' is not in Graph API v2 MVP scope",
            "supported_periods": list(MVP_PERIODS),
            "fallback": {
                "type": "legacy_graph_api",
                "hint": "Use legacy /api/graph/{currency}?range={period} for 1d",
            },
        })

    # serve-time KRX fail-closed — 캐시 hit은 build 필터를 안 거치므로 여기서 한 번 더 (ADR-039 §6.1).
    # 이 지점이 1d/캐시-hit/캐시-miss 3경로 공통 exit이라 우회 경로가 없다.
    payload = strip_krx_if_not_allowed(payload)
    # serve-time domain 부착(metadata) — 캐시 원본 불변(위 set은 원본만), copy에만. 1d/캐시-hit/캐시-miss 3경로 공통.
    return attach_graph_v2_domain(payload, period_domain(period, anchor))


async def _free_snapshot_cache_get(cache_key: str, tab: str, period: str):
    """Redis GET(timeout — B2) + 역직렬화 + serve-time 재검증(B1). 유효 canonical dict 또는 None(→last-good/503).

    timeout으로 Redis hang이 무한 대기하지 않고 miss로 폴백(/api/rates:1008 asyncio.wait_for 패턴).
    validate_snapshot_payload로 오염 캐시(KRX/null/list/wrong-tab)를 거른다.
    """
    from app.free_snapshot import validate_snapshot_payload

    try:
        raw = await asyncio.wait_for(redis_cache.get(cache_key), timeout=2.0)
    except asyncio.TimeoutError:
        # hang(응답 정지)은 redis_cache 내부 except Exception이 CancelledError를 못 잡아 record_failure 누락(NB1).
        # circuit에 직접 실패 기록 → 반복 hang 시 circuit open → 후속 GET fast-fail(무한 2s 대기 반복 방지).
        # ⚠️ record_failure는 임계치에서 _persist_state(timeout 없는 Redis SET)를 호출하므로, Redis hang 중
        #    이 SET이 또 무한 대기할 수 있다 → wait_for(0.5)로 bound. in-memory 상태(failure_count/state=open)는
        #    persist SET **전에** 갱신되므로, SET이 취소돼도 circuit은 정상적으로 열린다(persist만 best-effort skip).
        try:
            await asyncio.wait_for(redis_cache.circuit.record_failure(), timeout=0.5)
        except Exception:
            pass
        return None
    except Exception:
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not validate_snapshot_payload(payload, tab, period):
        logger.warning("free_snapshot %s/%s 캐시 검증 실패(오염/schema) — last-good/503 폴백", tab, period)
        return None
    return payload


def _free_snapshot_local_get(cache_key: str):
    """process-local last-good canonical — Redis 장애 시 마지막 canonical(hourly-fixed) 반환용.

    Redis 성공 read마다 갱신 → 정상 시 최신 canonical, 장애 시 마지막 canonical. **DB rebuild 아님**
    (serve는 canonical만 서빙 = 1시간 고정 불변식). 만료 없음(다음 성공 read가 덮어씀).
    """
    return _free_snapshot_local.get(cache_key)


def _free_snapshot_local_put(cache_key: str, payload: dict) -> None:
    _free_snapshot_local[cache_key] = payload


@app.get("/api/v2/free/snapshot")
async def get_v2_free_snapshot(request: Request, response: Response, tab: str, period: str = "3m"):
    """무료(비구독) 매시간 고정 스냅샷 — Firebase 인증만(premium 불요), KRX 제외 (ADR-039 §4.1).

    **핵심 불변식(무료=매시 고정, HH:30 basis)**: serve는 cron(매시 :30:19)이 만든 canonical(hourly-fixed,
    `timestamp <= as_of` cutoff)만 반환하고 **DB로 재생성하지 않는다**. serve가 DB 최신값으로 rebuild하면 같은 시간대에도 값이 바뀌어 유료(실시간) 차등이 깨짐.
    Redis canonical read(timeout+재검증+circuit, B1/B2) → 성공값을 process-local last-good 보존 → Redis 장애 시
    마지막 canonical만 반환 → canonical이 아예 없으면(cold-start 전 첫 cron / 전면 손실) 503(최신값 fabricate 금지).
    빈/오염 payload도 validate가 거부 → last-good/503. 최신 표면(/api/v2/graph/tab 등)의 premium enforcement는 미접촉.
    """
    from app.free_snapshot import (
        FREE_SNAPSHOT_PERIODS,
        FREE_SNAPSHOT_TABS,
        attach_free_snapshot_domain,
        attach_refresh_not_before,
        free_snapshot_key,
        is_snapshot_too_stale,
    )

    await verify_firebase_token(request)   # 로그인 필수(401/503), require_premium 미호출 → 무료 접근
    if tab not in FREE_SNAPSHOT_TABS:
        return JSONResponse(status_code=404, content={
            "error": "unknown_tab",
            "detail": f"tab '{tab}' is not free-available",
            "free_tabs": list(FREE_SNAPSHOT_TABS),
        })
    if period not in FREE_SNAPSHOT_PERIODS:   # N6: endpoint·precompute 단일 진실소스
        return JSONResponse(status_code=400, content={
            "error": "unsupported_period",
            "detail": f"period '{period}' not free-available",
            "free_periods": list(FREE_SNAPSHOT_PERIODS),
        })

    response.headers["Cache-Control"] = "no-store"   # 서버가 freshness 소유 (graph/tab 관례)
    cache_key = free_snapshot_key(tab, period)

    # cron canonical만 서빙. validate가 empty/오염/KRX 거부(→ last-good/503). serve는 DB build 안 함.
    # serve-time 부착 2종(canonical 불변, copy에만): graph domain + refresh_not_before(재요청 권장 시각 = 다음 :31,
    # client 5분 폴링→one-shot 대체 축). 503(스냅샷 자체 없음)엔 부착 안 함.
    # S6 24h hard cutoff: Redis/local 각 후보를 개별 age 검사. fresh(<24h)한 첫 후보만 서빙,
    # 둘 다 부재/24h+ stale이면 503. **stale Redis는 local에 미저장** — 더 최신일 수 있는 local 보존(codex).
    cached = await _free_snapshot_cache_get(cache_key, tab, period)
    if cached is not None and not is_snapshot_too_stale(cached):
        _free_snapshot_local_put(cache_key, cached)   # last-good = 마지막 **fresh** canonical(domain 없는 원본 보존)
        return attach_refresh_not_before(attach_free_snapshot_domain(cached, period))
    # Redis miss/hang/오염/**24h+ stale** → process-local last-good fallback(더 최신일 수 있음). **DB fabricate 금지.**
    local = _free_snapshot_local_get(cache_key)
    if local is not None and not is_snapshot_too_stale(local):
        return attach_refresh_not_before(attach_free_snapshot_domain(local, period))
    # 두 후보 다 없음/24h+ stale(S6 cutoff) → 503. 첫 cron 전 cold-start/전면 손실/빈 payload도 여기로 귀결.
    return JSONResponse(status_code=503, content={"error": "snapshot_unavailable"})


@app.get("/api/v2/topics/snapshot")
async def get_v2_topic_snapshot(request: Request, topic: str):
    """topic 현재 snapshot REST bootstrap (WS 미연결/실패 시 cold-start fallback, OPEN 1).

    WS subscribe의 snapshot-on-subscribe와 **동일 builder**(`_build_snapshot_sync`) →
    동일 schema(type/version/topic/data + usdt_krw/krx tick entry의 rate_changed_at).
    KRX는 독립 topic krx:usd-krw-futures (ADR-038 D2). client는 REST/WS 동일 merge 로직
    (`rate_changed_at ?? timestamp`). REALTIME_V2_CLIENT_GUIDE §3.

    **접근 게이트 (ADR-039 §8.1 E3)** — 이 endpoint는 WS topic의 REST twin이라 §3.1 매트릭스가
    그대로 적용된다: Firebase 인증 + premium, KRX는 entitlement 추가.
    게이트가 없으면 1C가 `TOPIC_DISPATCHER_ENABLED`를 켜는 순간(1C는 그 flag를 켜야 동작한다)
    현재 flag-off로 완화 중인 **무인증 KRX REST 경로가 되열린다**.

    순서가 계약이다:
      1. dormant flag — 인증보다 **먼저**(dormant 계약 보존 + 불필요한 Firebase RTT 회피)
      2. 인증 → 3. premium → 4. per-user topic 해석
      인증을 topic 판정보다 앞에 둬야 미인증자가 supported 목록을 열거하지 못한다.

    DB 세션은 `Depends(get_db)`로 받지 **않는다** — request-scoped 세션은 응답까지 커넥션을 쥐고,
    그 뒤 builder가 두 번째 세션을 열어 좁은 풀(3+2)을 요청당 2개씩 소비한다.
    가시성 조회는 `visible_snapshot_topics_sync`가 worker thread 안에서 열고 닫는다.
    """
    from app import config
    from app.topic_initial_snapshot import (
        _build_snapshot_sync, resolve_snapshot_topic_access_sync)

    # 개인화 응답(비-entitled의 KRX 404 포함) — 오류까지 캐시 금지. 200만 no-store면 private
    # HTTP 캐시가 404를 휴리스틱 저장해 entitlement 부여 뒤에도 404가 남을 수 있다.
    no_store = {"Cache-Control": "no-store"}

    if not config.TOPIC_DISPATCHER_ENABLED:
        # 출시 전 dormant — supported_topics 비노출 (codex 019efe2d)
        return JSONResponse(status_code=404, content={"error": "topics_disabled"},
                            headers=no_store)
    user_id = await verify_firebase_token(request)          # 401 (부수효과 0 — 첫 실행문 계약)
    # INACTIVE 403 / PENDING 503. 반환값을 그대로 쓴다 — `premium_active=True` 하드코딩은
    # require_premium이 ACTIVE 외 상태를 raise한다는 전제에 묶여 상태가 늘면 fail-open이 된다.
    premium_active = await require_premium(user_id, allow_empty=False)
    # per-user KRX 가시성(G1) 적용 — 비-entitled에겐 KRX가 목록·판정 양쪽에서 사라져
    # 미지원 topic과 구분 불가해진다 (§3.2). 판정이 결과를 바꿀 수 없는 요청(비-게이팅 topic)은
    # entitlement를 조회하지 않는다 — FX/USDT bootstrap이 entitlement DB에 묶이지 않도록.
    try:
        access = await asyncio.to_thread(
            resolve_snapshot_topic_access_sync, topic, user_id, premium_active=premium_active)
        if not access.allowed:
            return JSONResponse(status_code=404, headers=no_store, content={
                "error": "unknown_topic",
                "detail": f"topic '{topic}' not supported",
                "supported_topics": list(access.supported_topics),
            })
        payload = await asyncio.to_thread(_build_snapshot_sync, topic)
    except TRANSIENT_DB_ERRORS:
        # DB 순단 = 인프라 transient지 결함이 아니다 → 503(구 동작은 plain 500이라 클라 재시도
        # 분류에서 빠졌다). 판정·빌드를 **함께** 감싼다 — 한쪽만 감싸면 상태코드가 topic 종류에
        # 따라 갈린다(비-게이팅 topic은 판정 경로를 안 타므로).
        # ⛔ 경계는 `TRANSIENT_DB_ERRORS`(좁은 4종)다 — `except Exception`은 물론
        # `except SQLAlchemyError`도 너무 넓다(ProgrammingError·InvalidRequestError 등 영구 결함 포함).
        # `Retry-After`도 없다: 이 헤더는 구독 판정 PENDING(초 단위) 신호로 이미 쓰이고,
        # DB failover는 분 단위라 5초 재시도를 지시하면 storm이 된다.
        logger.exception("topic snapshot DB 순단 — 503",
                         extra={"event": "topic_snapshot_db_unavailable", "topic": topic})
        return JSONResponse(status_code=503, headers=no_store,
                            content={"error": "temporarily_unavailable"})
    if payload is None:
        # 지원 topic이지만 현재 미제공 (예: fx FX_TOPIC_ENABLED off) — WS publish 가능성과 일치
        return JSONResponse(status_code=404, headers=no_store,
                            content={"error": "topic_unavailable", "topic": topic})
    # latest 성격 — 캐시 금지 (codex 019efe2d). body는 WS snapshot과 동일 contract.
    return JSONResponse(content=payload, headers=no_store)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Firebase Auth + FCM 알림 API
# ═══════════════════════════════════════════════════════════════════════════════

async def verify_firebase_token(request: Request, check_revoked: bool = False) -> str:
    """
    Firebase ID Token 검증 및 user_id 추출

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Args:
        request: FastAPI Request 객체
        check_revoked: True면 revoke된 토큰도 차단 (파괴적 작업에 권장)

    Returns:
        user_id (Firebase uid)

    Raises:
        HTTPException 401: 인증 실패 (만료, 무효, revoke 포함)
        HTTPException 503: Firebase 미초기화
    """
    from firebase_admin import auth
    from google.auth import exceptions as google_auth_exceptions
    import requests

    if not is_firebase_initialized():
        raise HTTPException(
            status_code=503,
            detail="Firebase not initialized"
        )

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header"
        )

    token = auth_header.replace("Bearer ", "")

    try:
        decoded_token = auth.verify_id_token(token, check_revoked=check_revoked)
        user_id = decoded_token["uid"]
        return user_id
    except auth.RevokedIdTokenError:
        raise HTTPException(status_code=401, detail="Token has been revoked")
    except auth.ExpiredIdTokenError:
        raise HTTPException(status_code=401, detail="Token expired")
    except auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except auth.CertificateFetchError as e:
        logger.warning("Firebase 인증서 조회 실패", extra={"error": str(e)})
        raise HTTPException(status_code=503, detail="Firebase auth unavailable")
    except (google_auth_exceptions.TransportError, requests.exceptions.RequestException) as e:
        logger.warning("Firebase 네트워크 오류", extra={"error": str(e)})
        raise HTTPException(status_code=503, detail="Firebase auth unavailable")
    except Exception as e:
        logger.warning("Firebase 토큰 검증 실패", extra={"error": str(e)})
        raise HTTPException(status_code=401, detail="Token verification failed")


def build_notification_setting_response(setting: models.NotificationSetting) -> schemas.NotificationSettingResponse:
    """
    NotificationSetting DB 모델을 API 응답으로 변환

    필드 매핑:
    - enabled → is_enabled
    - last_notified_at → triggered_at
    """
    return schemas.NotificationSettingResponse(
        id=setting.id,
        user_id=setting.user_id,
        bank=setting.bank,
        currency=setting.currency,
        condition=setting.condition,
        threshold=setting.threshold,
        is_enabled=setting.enabled,
        triggered=setting.triggered,
        repeat_interval_sec=setting.repeat_interval_sec,  # B2 (ADR-036): iOS picker 초기값 로드용
        created_at=crud.to_kst_isoformat(setting.created_at),
        updated_at=crud.to_kst_isoformat(setting.updated_at),
        triggered_at=crud.to_kst_isoformat(setting.last_notified_at)
    )


@app.post("/api/register-device", response_model=schemas.RegisterDeviceResponse)
async def register_device(
    request: Request,
    body: schemas.RegisterDeviceRequest,
    db: Session = Depends(get_db)
):
    """
    FCM Device Token 등록

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Body:
        {
            "device_token": "FCM_TOKEN_HERE",
            "platform": "ios" | "android"
        }

    Returns:
        {
            "success": true,
            "message": "Device registered successfully",
            "device_id": 123
        }
    """
    user_id = await verify_firebase_token(request)

    try:
        device = crud.register_device(
            db=db,
            user_id=user_id,
            device_token=body.device_token,
            platform=body.platform
        )

        logger.info(
            "📱 디바이스 등록",
            extra={
                "event": "device_register",
                "user_id": user_id,
                "platform": body.platform,
                "device_id": device.id
            }
        )

        return schemas.RegisterDeviceResponse(
            success=True,
            message="Device registered successfully",
            device_id=device.id
        )

    except Exception as e:
        logger.error("디바이스 등록 실패", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/register-device")
async def unregister_device(
    request: Request,
    device_token: str,
    db: Session = Depends(get_db)
):
    """
    FCM Device Token 삭제 (로그아웃 시)

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Query:
        device_token: FCM Device Token
    """
    user_id = await verify_firebase_token(request)

    deleted = crud.delete_device(db=db, user_id=user_id, device_token=device_token)

    if deleted:
        logger.info("📱 디바이스 삭제", extra={"user_id": user_id})
        return {"success": True, "message": "Device unregistered"}
    else:
        raise HTTPException(status_code=404, detail="Device not found")


@app.post("/api/notification-settings", response_model=schemas.NotificationSettingResponse)
async def create_notification_setting(
    request: Request,
    body: schemas.NotificationSettingRequest,
    db: Session = Depends(get_db)
):
    """
    알림 설정 생성

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Body:
        {
            "bank": "hana",
            "currency": "usd-krw",
            "condition": "above",
            "threshold": 1475.0,
            "is_enabled": true
        }

    Note:
        - bank, currency, condition은 Enum으로 자동 검증됨
        - is_enabled 생략 시 기본값 true
        - 동일 조건(bank, currency, condition, threshold)의 알림이 이미 있으면
          기존 설정의 enabled 상태만 업데이트 (중복 생성 방지)
    """
    user_id = await verify_firebase_token(request)

    await require_premium(user_id, allow_empty=False)

    try:
        setting = crud.create_notification_setting(
            db=db,
            user_id=user_id,
            bank=body.bank.value,
            currency=body.currency.value,
            condition=body.condition.value,
            threshold=body.threshold,
            is_enabled=body.is_enabled,
            repeat_interval_sec=body.repeat_interval_sec,  # B2 (ADR-036): null=once
        )

        logger.info(
            "🔔 알림 설정 생성",
            extra={
                "event": "notification_setting_create",
                "user_id": user_id,
                "bank": body.bank.value,
                "currency": body.currency.value,
                "condition": body.condition.value,
                "threshold": body.threshold,
                "is_enabled": body.is_enabled,
                "repeat_interval_sec": body.repeat_interval_sec,
            }
        )

        # CRUD 성공 후 best-effort 동기화 (헬퍼 내부에서 예외 처리됨)
        await notify_user_devices_sync(db, user_id)

        return build_notification_setting_response(setting)

    except Exception as e:
        logger.error("알림 설정 생성 실패", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/notification-settings", response_model=schemas.NotificationSettingsListResponse)
async def get_notification_settings(
    request: Request,
    currency: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    사용자의 알림 설정 목록 조회

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Query Parameters:
        currency: 통화쌍 필터 (선택, 예: usd-krw)
    """
    user_id = await verify_firebase_token(request)

    if not await require_premium(user_id, allow_empty=True):
        return schemas.NotificationSettingsListResponse(settings=[], total_count=0)

    settings = crud.get_notification_settings(db=db, user_id=user_id)

    # currency 필터링 (선택)
    if currency:
        settings = [s for s in settings if s.currency == currency]

    return schemas.NotificationSettingsListResponse(
        settings=[build_notification_setting_response(s) for s in settings],
        total_count=len(settings)
    )


@app.put("/api/notification-settings/{setting_id}", response_model=schemas.NotificationSettingResponse)
async def update_notification_setting(
    request: Request,
    setting_id: int,
    body: schemas.NotificationSettingUpdateRequest,
    db: Session = Depends(get_db)
):
    """
    알림 설정 수정 (PUT - 부분 업데이트 지원)

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Body (모두 선택):
        bank: 은행 코드 (BankEnum)
        condition: 조건 (above/below)
        threshold: 임계값
        is_enabled: 활성화 여부 (토글)
        repeat_interval_sec: 반복 간격(초). null=once / 정수=repeat (B2 ADR-036)

    Notes:
        - bank, condition, threshold 중 실제로 값이 변경되면 triggered 초기화
        - is_enabled: False→True 전환 시에도 triggered 초기화
    """
    user_id = await verify_firebase_token(request)

    await require_premium(user_id, allow_empty=False)

    setting = crud.get_notification_setting_by_id(db=db, setting_id=setting_id, user_id=user_id)

    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")

    # enum을 문자열로 변환 (None이면 None 유지)
    bank_value = body.bank.value if body.bank else None
    condition_value = body.condition.value if body.condition else None

    # B2 (ADR-036): repeat_interval_sec는 "미제공"(변경 안 함)과 "명시적 null(=once 전환)"을
    # model_fields_set으로 구분 — 제공됐을 때만 crud에 전달(미제공이면 crud 기본 sentinel _UNSET).
    repeat_kwargs = (
        {"repeat_interval_sec": body.repeat_interval_sec}
        if "repeat_interval_sec" in body.model_fields_set
        else {}
    )

    updated = crud.update_notification_setting(
        db=db,
        setting_id=setting_id,
        user_id=user_id,
        bank=bank_value,
        condition=condition_value,
        threshold=body.threshold,
        enabled=body.is_enabled,
        **repeat_kwargs,
    )

    logger.info(
        "🔔 알림 설정 수정",
        extra={"event": "notification_setting_update", "setting_id": setting_id}
    )

    # CRUD 성공 후 best-effort 동기화 (헬퍼 내부에서 예외 처리됨)
    await notify_user_devices_sync(db, user_id)

    return build_notification_setting_response(updated)


@app.delete("/api/notification-settings/{setting_id}", response_model=schemas.DeleteResponse)
async def delete_notification_setting(
    request: Request,
    setting_id: int,
    db: Session = Depends(get_db)
):
    """
    알림 설정 삭제

    Headers:
        Authorization: Bearer <Firebase ID Token>
    """
    user_id = await verify_firebase_token(request)

    await require_premium(user_id, allow_empty=False)

    setting = crud.get_notification_setting_by_id(db=db, setting_id=setting_id, user_id=user_id)

    # 멱등성 DELETE: 이미 없으면 성공 반환 (다른 기기에서 삭제된 경우)
    if not setting:
        logger.info(
            "🔔 알림 설정 삭제 (이미 없음)",
            extra={"event": "notification_setting_delete_idempotent", "setting_id": setting_id}
        )
        return schemas.DeleteResponse(success=True, message="Setting already deleted")

    crud.delete_notification_setting(db=db, setting_id=setting_id, user_id=user_id)

    logger.info(
        "🔔 알림 설정 삭제",
        extra={"event": "notification_setting_delete", "setting_id": setting_id}
    )

    # CRUD 성공 후 best-effort 동기화 (헬퍼 내부에서 예외 처리됨)
    await notify_user_devices_sync(db, user_id)

    return schemas.DeleteResponse(success=True, message="Setting deleted")


# ═══════════════════════════════════════════════════════════════════════════════
# Source 기반 알림 API (/api/source-notification-settings)
# ═══════════════════════════════════════════════════════════════════════════════
# - 기존 /api/notification-settings와 완전 분리된 새 API family
# - DB: source_notification_settings 테이블 사용
# - source/asset은 source_registry에서 검증 (phase1_enabled=True + category
#   in {exchange, derivative}만 허용). USDT exchange는 Phase 1 (2026-04-23),
#   KRX derivative는 F-2 (2026-05-26)에서 추가.

def build_source_notification_setting_response(
    setting: models.SourceNotificationSetting,
) -> schemas.SourceNotificationSettingResponse:
    """SourceNotificationSetting DB 모델을 API 응답으로 변환."""
    return schemas.SourceNotificationSettingResponse(
        id=setting.id,
        user_id=setting.user_id,
        source=setting.source,
        asset=setting.asset,
        condition=setting.condition,
        threshold=setting.threshold,
        is_enabled=setting.enabled,
        triggered=setting.triggered,
        repeat_interval_sec=setting.repeat_interval_sec,  # B2 (ADR-036): iOS picker 초기값 로드용
        created_at=crud.to_kst_isoformat(setting.created_at),
        updated_at=crud.to_kst_isoformat(setting.updated_at),
        triggered_at=crud.to_kst_isoformat(setting.last_notified_at),
    )


def build_notification_log_response(
    log: models.NotificationLog,
) -> schemas.NotificationLogResponse:
    """NotificationLog(FX 은행 알림) DB 모델을 API 응답으로 변환.

    build_source_notification_log_response 미러. condition/threshold는 보강 이전 old row에선
    None. sent_at은 crud.to_kst_isoformat()로 KST(+09:00) ISO 직렬화(iOS 디코더 정합).
    """
    return schemas.NotificationLogResponse(
        id=log.id,
        setting_id=log.setting_id,
        bank=log.bank,
        currency=log.currency,
        condition=log.condition,
        threshold=log.threshold,
        rate=log.rate,
        sent_at=crud.to_kst_isoformat(log.sent_at),
    )


def build_source_notification_log_response(
    log: models.SourceNotificationLog,
) -> schemas.SourceNotificationLogResponse:
    """SourceNotificationLog DB 모델을 API 응답으로 변환.

    sent_at은 다른 응답과 동일하게 crud.to_kst_isoformat()로 KST(+09:00) ISO 직렬화
    (from_attributes에 의존하면 naive UTC가 나가 iOS 디코더와 어긋남).
    """
    return schemas.SourceNotificationLogResponse(
        id=log.id,
        setting_id=log.setting_id,
        source=log.source,
        asset=log.asset,
        condition=log.condition,
        threshold=log.threshold,
        triggered_rate=log.triggered_rate,
        sent_at=crud.to_kst_isoformat(log.sent_at),
    )


def _validate_alert_source_asset_or_400(source: str, asset: str) -> None:
    """Thin wrapper — `source_registry.validate_alert_source_asset` + HTTPException 변환.

    F-2 (2026-05-26): 검증 로직 본체는 `source_registry`로 분리 (과거 메모리
    기록 `project_main_py_helper_placement` 참조 — main.py 안에 helper 두면
    단위 테스트가 firebase_admin import chain으로 깨짐, 2026-05-10 Z-2b Stage 2
    발견 사례 재발). 본 wrapper는 사용자 노출용 HTTPException 변환만 담당.

    Rename history: F-2 이전 `_validate_phase1_source_asset` (Phase 1 USDT
    exchange only 의미) → F-2에서 category in {"exchange","derivative"}
    확장 후 의미 변화로 `_validate_alert_source_asset_or_400`로 cleanup
    (2026-05-26). `_or_400` suffix가 wrapper 책임(HTTPException 400 변환)을 명시.

    `validate_alert_source_asset` 동작 (`source_registry.py`):
        허용: phase1_enabled=True + category in ("exchange", "derivative")
        차단: 기타 모든 조합 (reference / 미등록 / phase1_disabled)

    F-2 land ~ F-3 (`KRX_ALERT_EVALUATOR_ENABLED=true`) 활성 사이 KRX 알림은
    의도된 staging gap — API 등록 가능하나 발송은 evaluator flag가 닫혀 있음.
    운영 단말 영향 0 (테더 탭 자체가 운영 앱에 없음, 테스트 iOS canary 전용).
    """
    from app import source_registry
    error = source_registry.validate_alert_source_asset(source, asset)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)


@app.post(
    "/api/source-notification-settings",
    response_model=schemas.SourceNotificationSettingResponse,
)
async def create_source_notification_setting(
    request: Request,
    body: schemas.SourceNotificationSettingRequest,
    db: Session = Depends(get_db),
):
    """
    Source 기반 알림 설정 생성 (USDT exchange + KRX derivative).

    허용 대상 (`source_registry.validate_alert_source_asset`):
    - USDT exchange (upbit/bithumb/coinone/korbit/gopax + usdt-krw)
    - KRX derivative (krx + usd-krw-futures) — F-2 (2026-05-26) 허용 추가.
      실제 발송은 `KRX_ALERT_EVALUATOR_ENABLED=true`(F-3) 활성 필요.

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Body:
        {
            "source": "upbit",
            "asset": "usdt-krw",
            "condition": "below",
            "threshold": 1480.0,
            "is_enabled": true
        }

    중복 처리:
    - 동일 (source, asset, condition, threshold) 조합이 있으면 enabled 상태만 업데이트
    - is_enabled=true → triggered 초기화 (재알림 가능)
    - is_enabled=false → triggered 유지
    """
    user_id = await verify_firebase_token(request)
    await require_premium(user_id, allow_empty=False)

    _validate_alert_source_asset_or_400(body.source, body.asset)
    # ADR-038 G1/G2 — KRX 단일 가격알림 생성은 entitlement+게이트 필요 (403)
    if (body.source, body.asset) == entitlements.KRX_PAIR:
        _require_krx_alert_allowed_or_403(db, user_id)

    try:
        setting = crud.create_source_notification_setting(
            db=db,
            user_id=user_id,
            source=body.source,
            asset=body.asset,
            condition=body.condition.value,
            threshold=body.threshold,
            is_enabled=body.is_enabled,
            repeat_interval_sec=body.repeat_interval_sec,  # B2 (ADR-036): null=once
        )

        # Settings CRUD cache invalidation (PR6 follow-up):
        # 새 setting을 evaluator가 다음 tick부터 즉시 평가 (TTL 10s 기다리지 X).
        from app.notifications.alert_evaluator import invalidate_alert_settings_cache
        invalidate_alert_settings_cache(body.source, body.asset)

        logger.info(
            "🔔 source 알림 설정 생성",
            extra={
                "event": "source_notification_setting_create",
                "user_id": user_id,
                "source": body.source,
                "asset": body.asset,
                "condition": body.condition.value,
                "threshold": body.threshold,
                "is_enabled": body.is_enabled,
            },
        )

        await notify_user_devices_sync(db, user_id)
        return build_source_notification_setting_response(setting)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("source 알림 설정 생성 실패", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/api/source-notification-settings",
    response_model=schemas.SourceNotificationSettingsListResponse,
)
async def get_source_notification_settings(
    request: Request,
    asset: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    사용자의 source 기반 알림 설정 목록 조회.

    Query Parameters:
        asset: asset 필터 (선택, 예: usdt-krw / usd-krw-futures)
    """
    user_id = await verify_firebase_token(request)

    if not await require_premium(user_id, allow_empty=True):
        return schemas.SourceNotificationSettingsListResponse(settings=[], total_count=0)

    settings = crud.get_source_notification_settings(db=db, user_id=user_id)

    if asset:
        settings = [s for s in settings if s.asset == asset]

    return schemas.SourceNotificationSettingsListResponse(
        settings=[build_source_notification_setting_response(s) for s in settings],
        total_count=len(settings),
    )


@app.put(
    "/api/source-notification-settings/{setting_id}",
    response_model=schemas.SourceNotificationSettingResponse,
)
async def update_source_notification_setting(
    request: Request,
    setting_id: int,
    body: schemas.SourceNotificationSettingUpdateRequest,
    db: Session = Depends(get_db),
):
    """
    Source 기반 알림 설정 수정 (PUT - 부분 업데이트).

    Body (모두 선택):
        source, asset, condition, threshold, is_enabled

    Notes:
        - source/asset 변경 시 phase1_enabled + category in {exchange, derivative}
          조합인지 검증 (F-2: KRX 포함)
        - source, asset, condition, threshold 실제 변경 시 triggered 초기화
        - is_enabled: False→True 전환 시에도 triggered 초기화
    """
    user_id = await verify_firebase_token(request)
    await require_premium(user_id, allow_empty=False)

    setting = crud.get_source_notification_setting_by_id(db=db, setting_id=setting_id, user_id=user_id)
    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")

    # 옛 source/asset 캡처 (cache invalidation 위해 update 전에)
    old_source = setting.source
    old_asset = setting.asset

    # source/asset 중 하나라도 바뀌면 최종 조합을 검증
    new_source = body.source if body.source is not None else setting.source
    new_asset = body.asset if body.asset is not None else setting.asset
    if body.source is not None or body.asset is not None:
        _validate_alert_source_asset_or_400(new_source, new_asset)

    # ADR-038 G1/G2 — 최종 조합이 KRX면 gate. source/asset 미변경 PUT은 위 validator를
    # 건너뛰므로(재활성 우회 경로) 최종 조합 기준 무조건 검사. 예외는 **끄기 전용** PUT뿐 —
    # is_enabled=False 단독(다른 변경 필드 전무)만 허용 (권한 상실 사용자도 자기 알림은 끌 수
    # 있어야). is_enabled=False에 변경을 얹은 요청은 gate (우회 차단 — codex Q7+blocker 1).
    _disable_only = (
        body.is_enabled is False
        and body.source is None and body.asset is None
        and body.condition is None and body.threshold is None
        and "repeat_interval_sec" not in body.model_fields_set
    )
    if (new_source, new_asset) == entitlements.KRX_PAIR and not _disable_only:
        _require_krx_alert_allowed_or_403(db, user_id)

    condition_value = body.condition.value if body.condition else None

    # B2 (ADR-036): repeat_interval_sec는 "미제공"(변경 안 함)과 "명시적 null(=once 전환)"을
    # model_fields_set으로 구분 — 제공됐을 때만 crud에 전달(미제공이면 crud 기본 sentinel _UNSET).
    repeat_kwargs = (
        {"repeat_interval_sec": body.repeat_interval_sec}
        if "repeat_interval_sec" in body.model_fields_set
        else {}
    )

    updated = crud.update_source_notification_setting(
        db=db,
        setting_id=setting_id,
        user_id=user_id,
        source=body.source,
        asset=body.asset,
        condition=condition_value,
        threshold=body.threshold,
        enabled=body.is_enabled,
        **repeat_kwargs,
    )

    # Settings CRUD cache invalidation (PR6 follow-up):
    # source/asset 변경 가능 → old + new 둘 다 invalidate (옛 cache key의
    # 잔존 setting + 새 cache key의 다음 miss populate 보장).
    # 같은 source/asset이면 한 번 invalidate (set으로 dedup).
    from app.notifications.alert_evaluator import invalidate_alert_settings_cache
    invalidate_keys = {(old_source, old_asset), (updated.source, updated.asset)}
    for src, ast in invalidate_keys:
        invalidate_alert_settings_cache(src, ast)

    logger.info(
        "🔔 source 알림 설정 수정",
        extra={"event": "source_notification_setting_update", "setting_id": setting_id},
    )

    await notify_user_devices_sync(db, user_id)
    return build_source_notification_setting_response(updated)


@app.delete(
    "/api/source-notification-settings/{setting_id}",
    response_model=schemas.DeleteResponse,
)
async def delete_source_notification_setting(
    request: Request,
    setting_id: int,
    db: Session = Depends(get_db),
):
    """Source 기반 알림 설정 삭제."""
    user_id = await verify_firebase_token(request)
    await require_premium(user_id, allow_empty=False)

    setting = crud.get_source_notification_setting_by_id(db=db, setting_id=setting_id, user_id=user_id)

    # 멱등성 DELETE
    if not setting:
        logger.info(
            "🔔 source 알림 설정 삭제 (이미 없음)",
            extra={"event": "source_notification_setting_delete_idempotent", "setting_id": setting_id},
        )
        return schemas.DeleteResponse(success=True, message="Setting already deleted")

    # 삭제 전 source/asset 캡처 (cache invalidation 위해)
    deleted_source = setting.source
    deleted_asset = setting.asset

    crud.delete_source_notification_setting(db=db, setting_id=setting_id, user_id=user_id)

    # Settings CRUD cache invalidation (PR6 follow-up):
    # 삭제된 setting을 evaluator가 다음 tick부터 평가 후보에서 즉시 제거.
    from app.notifications.alert_evaluator import invalidate_alert_settings_cache
    invalidate_alert_settings_cache(deleted_source, deleted_asset)

    logger.info(
        "🔔 source 알림 설정 삭제",
        extra={"event": "source_notification_setting_delete", "setting_id": setting_id},
    )

    await notify_user_devices_sync(db, user_id)
    return schemas.DeleteResponse(success=True, message="Setting deleted")


# Source 알림 발송 히스토리 (테더 탭: USDT 거래소 + KRX 달러선물).
# notification_logs(FX 은행) + source_notification_logs 공통 히스토리 조회 정책 (auth/premium 동일).
_NOTIFICATION_LOG_DEFAULT_LIMIT = 100
_NOTIFICATION_LOG_MAX_LIMIT = 200


@app.get(
    "/api/notification-logs",
    response_model=schemas.NotificationLogsListResponse,
)
async def get_notification_logs(
    request: Request,
    currency: Optional[str] = None,
    limit: int = _NOTIFICATION_LOG_DEFAULT_LIMIT,
    db: Session = Depends(get_db),
):
    """FX 은행 가격알림 발송 히스토리 조회 (달러/엔/유로 탭 은행 알림).

    사용자에게 '받은 알림' 히스토리를 보여준다 — 발송 성공(success=True) row만, 최신순
    (sent_at DESC). 전송 실패 row는 운영 진단용이라 미노출. source-notification-logs 정책 동일.

    Query Parameters:
        currency: 통화쌍 필터 (선택, 예: usd-krw / jpy-krw / eur-krw)
        limit: 1..200 (기본 100). offset 없음 — '최근 N건'. total_count는 페이지 길이.

    premium 게이팅 (settings GET 동일): INACTIVE → 빈 목록 / PENDING → 503 / ACTIVE → 조회.
    """
    user_id = await verify_firebase_token(request)

    if not await require_premium(user_id, allow_empty=True):
        return schemas.NotificationLogsListResponse(logs=[], total_count=0)

    capped_limit = max(1, min(limit, _NOTIFICATION_LOG_MAX_LIMIT))
    logs = crud.get_notification_logs(
        db=db,
        user_id=user_id,
        currency=currency,
        success_only=True,
        limit=capped_limit,
    )
    items = [build_notification_log_response(log) for log in logs]
    return schemas.NotificationLogsListResponse(logs=items, total_count=len(items))


# source_notification_logs의 최초 user-facing reader. settings GET과 auth/premium 정책 동일.
_SOURCE_NOTIFICATION_LOG_DEFAULT_LIMIT = 100
_SOURCE_NOTIFICATION_LOG_MAX_LIMIT = 200


@app.get(
    "/api/source-notification-logs",
    response_model=schemas.SourceNotificationLogsListResponse,
)
async def get_source_notification_logs(
    request: Request,
    asset: Optional[str] = None,
    limit: int = _SOURCE_NOTIFICATION_LOG_DEFAULT_LIMIT,
    db: Session = Depends(get_db),
):
    """Source 기반 알림 발송 히스토리 조회 (테더 탭: USDT 거래소 + KRX 달러선물).

    사용자에게 '받은 알림' 히스토리를 보여준다 — 발송 성공(success=True) row만,
    최신순(sent_at DESC). 전송 실패 row는 운영 진단용이라 미노출.

    Query Parameters:
        asset: asset 필터 (선택, 예: usdt-krw / usd-krw-futures)
        limit: 1..200 (기본 100). offset 없음 — '최근 N건'.
            total_count는 반환된 페이지 길이(전체 카운트 아님).

    premium 게이팅은 settings GET과 동일:
        INACTIVE → 빈 목록 / PENDING → 503(Retry-After) / ACTIVE → 조회.
    """
    user_id = await verify_firebase_token(request)

    if not await require_premium(user_id, allow_empty=True):
        return schemas.SourceNotificationLogsListResponse(logs=[], total_count=0)

    capped_limit = max(1, min(limit, _SOURCE_NOTIFICATION_LOG_MAX_LIMIT))
    logs = crud.get_source_notification_logs(
        db=db,
        user_id=user_id,
        asset=asset,
        success_only=True,
        limit=capped_limit,
    )
    items = [build_source_notification_log_response(log) for log in logs]
    return schemas.SourceNotificationLogsListResponse(
        logs=items,
        total_count=len(items),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 비교 알림 API (/api/comparison-alerts) — ADR-037 S3
# ═══════════════════════════════════════════════════════════════════════════════
# within-tab v1: tab-scope 검증은 source_registry.validate_comparison_alert (main.py 밖 —
# project_main_py_helper_placement). auth/premium 정책은 source-notification-* 와 동일.
# 발화는 COMPARISON_ALERT_ENABLED flag (S2 evaluator) — API/스키마는 flag 무관 동작.

_COMPARISON_LOG_DEFAULT_LIMIT = 100
_COMPARISON_LOG_MAX_LIMIT = 200


def _validate_comparison_alert_or_400(tab: str, left_source: str, left_asset: str,
                                      right_source: str, right_asset: str,
                                      diff_type: str, threshold: float) -> None:
    """Thin wrapper — source_registry.validate_comparison_alert + HTTPException 변환.

    ADR-037 Amendment: diff_type이 정책을 가름 (absolute=일반 비교[탭별 대칭 집합 + threshold≥0] /
    signed=김프알림[테더 전용, 거래소×환율계]).
    """
    from app import source_registry
    error = source_registry.validate_comparison_alert(
        tab, left_source, left_asset, right_source, right_asset, diff_type, threshold)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)


def _require_krx_alert_allowed_or_403(db: Session, user_id: str) -> None:
    """ADR-038 G1/G2 — KRX 알림 생성/재활성/변경 gate (thin wrapper — 본체 app/entitlements.py).

    403 = 유효하지만 게이트/권한이 닫힌 조합 (구조적 무효 400과 구분 — codex Q5).
    정상 클라 흐름에선 도달하지 않음 (krx_visible=false면 UI 자체 미노출)."""
    error = entitlements.krx_alert_gate_error(db, user_id)
    if error is not None:
        raise HTTPException(status_code=403, detail=error)


def build_comparison_alert_response(alert) -> schemas.ComparisonAlertResponse:
    return schemas.ComparisonAlertResponse(
        id=alert.id, user_id=alert.user_id, tab=alert.tab,
        left_source=alert.left_source, left_asset=alert.left_asset,
        right_source=alert.right_source, right_asset=alert.right_asset,
        diff_type=alert.diff_type, operator=alert.operator, threshold=alert.threshold,
        is_enabled=alert.enabled, triggered=alert.triggered,
        repeat_interval_sec=alert.repeat_interval_sec,
        last_notified_spread=alert.last_notified_spread,
        created_at=crud.to_kst_isoformat(alert.created_at),
        updated_at=crud.to_kst_isoformat(alert.updated_at) if alert.updated_at else None,
    )


def _invalidate_comparison_cache() -> None:
    """비교알림 후보 캐시 무효화 — 다음 tick부터 즉시 반영 (TTL 10s 대기 회피)."""
    from app.notifications.comparison_evaluator import get_comparison_evaluator
    get_comparison_evaluator().invalidate_cache()


@app.post("/api/comparison-alerts", response_model=schemas.ComparisonAlertResponse)
async def create_comparison_alert(
    request: Request,
    body: schemas.ComparisonAlertRequest,
    db: Session = Depends(get_db),
):
    """비교 알림 생성 (spread = left − right, ADR-037).

    검증: tab-scope(within-tab, KRW축만 — 400) + diff_type/operator/repeat enum(422).
    dedup: exact match → 기존 설정 enabled 갱신 (멱등).
    """
    user_id = await verify_firebase_token(request)
    await require_premium(user_id, allow_empty=False)

    _validate_comparison_alert_or_400(body.tab, body.left_source, body.left_asset,
                                      body.right_source, body.right_asset,
                                      body.diff_type, body.threshold)

    # ADR-038 G1/G2 — KRX가 좌우 어느 쪽에든 포함되면 entitlement+게이트 필요 (403).
    # signed(김프 counter)는 right만 가능(validator가 left=거래소 강제)하고, absolute는
    # canonical 정렬로 krx가 left/right 어느 쪽이든 저장됨 → 양측 검사 (codex 2026-07-10).
    if (entitlements.KRX_PAIR in ((body.left_source, body.left_asset),
                                  (body.right_source, body.right_asset))):
        _require_krx_alert_allowed_or_403(db, user_id)

    # absolute는 저장 전 canonical ordering — A−B/B−A dedup 중복 차단 (ADR-037 Amendment).
    left_source, left_asset = body.left_source, body.left_asset
    right_source, right_asset = body.right_source, body.right_asset
    if body.diff_type == "absolute":
        from app import source_registry
        left_source, left_asset, right_source, right_asset = (
            source_registry.canonicalize_absolute_pair(
                left_source, left_asset, right_source, right_asset))

    try:
        alert = crud.create_comparison_alert(
            db=db, user_id=user_id, tab=body.tab,
            left_source=left_source, left_asset=left_asset,
            right_source=right_source, right_asset=right_asset,
            diff_type=body.diff_type, operator=body.operator, threshold=body.threshold,
            is_enabled=body.is_enabled, repeat_interval_sec=body.repeat_interval_sec,
        )
        _invalidate_comparison_cache()
        logger.info("📊 비교 알림 생성", extra={
            "event": "comparison_alert_create", "user_id": user_id, "tab": body.tab,
            "left": f"{body.left_source}:{body.left_asset}",
            "right": f"{body.right_source}:{body.right_asset}",
            "diff_type": body.diff_type, "operator": body.operator, "threshold": body.threshold,
        })
        await notify_user_devices_sync(db, user_id)
        return build_comparison_alert_response(alert)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("비교 알림 생성 실패", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/comparison-alerts", response_model=schemas.ComparisonAlertsListResponse)
async def get_comparison_alerts(
    request: Request,
    tab: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """사용자의 비교 알림 목록 (tab 필터 선택)."""
    user_id = await verify_firebase_token(request)
    if not await require_premium(user_id, allow_empty=True):
        return schemas.ComparisonAlertsListResponse(alerts=[], total_count=0)

    alerts = crud.get_comparison_alerts(db=db, user_id=user_id)
    if tab:
        alerts = [a for a in alerts if a.tab == tab]
    return schemas.ComparisonAlertsListResponse(
        alerts=[build_comparison_alert_response(a) for a in alerts],
        total_count=len(alerts),
    )


@app.put("/api/comparison-alerts/{setting_id}", response_model=schemas.ComparisonAlertResponse)
async def update_comparison_alert(
    request: Request,
    setting_id: int,
    body: schemas.ComparisonAlertUpdateRequest,
    db: Session = Depends(get_db),
):
    """비교 알림 수정 — A4: is_enabled + repeat + threshold + operator(방향).

    pair(소스 조합)/diff_type은 편집 불가(삭제+재생성). threshold/operator 편집 시 신정책
    재검증(pair/diff_type은 기존 값) — signed 음수 허용 / absolute≥0 / hard cap ±10000.
    """
    user_id = await verify_firebase_token(request)
    await require_premium(user_id, allow_empty=False)

    alert = crud.get_comparison_alert(db, setting_id, user_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Comparison alert not found")

    # A4: threshold/operator 편집이면 기존 pair/diff_type + 새 threshold로 재검증
    if body.threshold is not None or body.operator is not None:
        new_threshold = body.threshold if body.threshold is not None else alert.threshold
        _validate_comparison_alert_or_400(
            alert.tab, alert.left_source, alert.left_asset,
            alert.right_source, alert.right_asset, alert.diff_type, new_threshold)

    # ADR-038 G1/G2 — 기존 alert가 KRX counter(김프)면 **끄기 전용** PUT 외 전부 gate
    # (재활성 우회 + "변경하며 끄기" 우회 둘 다 차단 — codex Q7+blocker 1).
    _disable_only = (
        body.is_enabled is False
        and body.threshold is None and body.operator is None
        and "repeat_interval_sec" not in body.model_fields_set
    )
    # KRX가 좌우 어느 쪽이든(signed 김프 counter + absolute 달러선물 비교) 게이트 (codex 2026-07-10)
    if (entitlements.KRX_PAIR in ((alert.left_source, alert.left_asset),
                                  (alert.right_source, alert.right_asset))
            and not _disable_only):
        _require_krx_alert_allowed_or_403(db, user_id)

    repeat_kwargs = (
        {"repeat_interval_sec": body.repeat_interval_sec}
        if "repeat_interval_sec" in body.model_fields_set
        else {}
    )
    updated = crud.update_comparison_alert(
        db=db, setting_id=setting_id, user_id=user_id,
        is_enabled=body.is_enabled,
        threshold=body.threshold, operator=body.operator, **repeat_kwargs,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Comparison alert not found")

    _invalidate_comparison_cache()
    logger.info("📊 비교 알림 수정", extra={
        "event": "comparison_alert_update", "setting_id": setting_id,
        "is_enabled": body.is_enabled,
        "threshold": body.threshold, "operator": body.operator,
    })
    await notify_user_devices_sync(db, user_id)
    return build_comparison_alert_response(updated)


@app.delete("/api/comparison-alerts/{setting_id}", response_model=schemas.DeleteResponse)
async def delete_comparison_alert(
    request: Request,
    setting_id: int,
    db: Session = Depends(get_db),
):
    """비교 알림 삭제 (멱등). 로그는 setting_id nullable로 보존."""
    user_id = await verify_firebase_token(request)
    await require_premium(user_id, allow_empty=False)

    deleted = crud.delete_comparison_alert(db=db, setting_id=setting_id, user_id=user_id)
    _invalidate_comparison_cache()
    logger.info("📊 비교 알림 삭제", extra={
        "event": "comparison_alert_delete", "setting_id": setting_id, "deleted": deleted,
    })
    await notify_user_devices_sync(db, user_id)
    return schemas.DeleteResponse(
        success=True,
        message="Comparison alert deleted" if deleted else "Comparison alert already deleted",
    )


@app.get("/api/entitlements", response_model=schemas.EntitlementsResponse)
async def get_entitlements(
    request: Request,
    db: Session = Depends(get_db),
):
    """ADR-038 — krx_visible 단일 신호 (G3 ∧ G2 ∧ G1 ∧ premium). 클라는 게이트 조합을
    계산하지 않고 이 값 하나로 KRX 표면(그래프 series/시세/알림 선택지) 노출을 결정.

    premium PENDING → 503 대신 200 {krx_visible:false, premium_pending:true} (read API —
    fail-closed + 클라 retry_after_seconds 후 재요청, codex Q2). INACTIVE → krx_visible=false.
    """
    user_id = await verify_firebase_token(request)
    status = await verify_premium_status(user_id)
    if status == PremiumStatus.PENDING:
        return schemas.EntitlementsResponse(
            krx_visible=False, premium_pending=True, retry_after_seconds=5)
    visible = entitlements.compute_krx_visible(
        db, user_id, premium_active=(status == PremiumStatus.ACTIVE))
    return schemas.EntitlementsResponse(krx_visible=visible)


@app.get("/api/comparison-notification-logs",
         response_model=schemas.ComparisonNotificationLogsListResponse)
async def get_comparison_notification_logs(
    request: Request,
    tab: Optional[str] = None,
    diff_type: Optional[str] = None,   # 'signed'(김프) | 'absolute'(비교) — 섹션별 필터
    limit: int = _COMPARISON_LOG_DEFAULT_LIMIT,
    db: Session = Depends(get_db),
):
    """비교 알림 발송 히스토리 — 발화 시점 스냅샷(left/right rate + spread + observed_at).

    success=True 최신순, limit 1..200 (source-notification-logs 정책 동일).
    premium: INACTIVE → 빈 목록 / PENDING → 503 / ACTIVE → 조회.
    """
    user_id = await verify_firebase_token(request)
    if not await require_premium(user_id, allow_empty=True):
        return schemas.ComparisonNotificationLogsListResponse(logs=[], total_count=0)

    capped = max(1, min(limit, _COMPARISON_LOG_MAX_LIMIT))
    logs = crud.get_comparison_notification_logs(
        db=db, user_id=user_id, tab=tab, diff_type=diff_type, success_only=True, limit=capped)
    items = [schemas.ComparisonNotificationLogResponse(
        id=lg.id, setting_id=lg.setting_id, tab=lg.tab,
        left_source=lg.left_source, left_asset=lg.left_asset,
        right_source=lg.right_source, right_asset=lg.right_asset,
        diff_type=lg.diff_type, operator=lg.operator, threshold=lg.threshold,
        left_rate=lg.left_rate, right_rate=lg.right_rate, spread=lg.spread,
        left_observed_at=crud.to_kst_isoformat(lg.left_observed_at) if lg.left_observed_at else None,
        right_observed_at=crud.to_kst_isoformat(lg.right_observed_at) if lg.right_observed_at else None,
        is_repeat=lg.is_repeat,
        sent_at=crud.to_kst_isoformat(lg.sent_at),
    ) for lg in logs]
    return schemas.ComparisonNotificationLogsListResponse(logs=items, total_count=len(items))


# ═══════════════════════════════════════════════════════════════════════════════
# 계정 삭제 API (Apple App Store 5.1.1(v) 준수)
# ═══════════════════════════════════════════════════════════════════════════════

@app.delete("/api/user/me", status_code=204)
async def delete_user_account(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    사용자 계정 삭제 (모든 관련 데이터 삭제)

    Apple App Store 가이드라인 5.1.1(v) 준수를 위한 계정 삭제 API.
    해당 user_id와 연결된 모든 FXi 서버 데이터를 삭제합니다.

    삭제 대상:
    - NotificationLog: 알림 발송 기록
    - NotificationSetting: 환율 알림 설정
    - SourceNotificationLog: source 기반 알림 발송 기록
    - SourceNotificationSetting: source 기반 알림 설정
    - UserDevice: FCM 토큰 (푸시 알림용)

    Headers:
        Authorization: Bearer <Firebase ID Token>

    Returns:
        204 No Content: 삭제 성공

    Raises:
        401: Firebase 토큰 검증 실패
        500: 데이터베이스 오류

    Note:
        - 구독 상태와 무관하게 삭제 가능 (require_premium 호출 안 함)
        - iOS에서 Firebase Auth 삭제 전에 호출해야 함
        - check_revoked=True로 revoke된 토큰 차단 (보안 강화)
    """
    # 파괴적 작업이므로 revoke된 토큰도 차단
    user_id = await verify_firebase_token(request, check_revoked=True)

    try:
        # 삭제 순서: 외래 키 의존성 없으므로 순서 무관하나, 로그 먼저 삭제
        deleted_logs = db.query(models.NotificationLog).filter(
            models.NotificationLog.user_id == user_id
        ).delete(synchronize_session=False)

        deleted_settings = db.query(models.NotificationSetting).filter(
            models.NotificationSetting.user_id == user_id
        ).delete(synchronize_session=False)

        # Source 기반 알림 (USDT exchange + KRX derivative)
        deleted_source_logs = db.query(models.SourceNotificationLog).filter(
            models.SourceNotificationLog.user_id == user_id
        ).delete(synchronize_session=False)

        deleted_source_settings = db.query(models.SourceNotificationSetting).filter(
            models.SourceNotificationSetting.user_id == user_id
        ).delete(synchronize_session=False)

        deleted_devices = db.query(models.UserDevice).filter(
            models.UserDevice.user_id == user_id
        ).delete(synchronize_session=False)

        db.commit()

        logger.info(
            "🗑️ 계정 삭제 완료",
            extra={
                "event": "account_deletion",
                "user_id": user_id[:8] + "...",  # 보안: UID 일부만 로깅
                "deleted_logs": deleted_logs,
                "deleted_settings": deleted_settings,
                "deleted_source_logs": deleted_source_logs,
                "deleted_source_settings": deleted_source_settings,
                "deleted_devices": deleted_devices,
            }
        )

        return Response(status_code=204)

    except Exception as e:
        db.rollback()
        logger.error(
            "❌ 계정 삭제 실패",
            extra={"event": "account_deletion_failed", "error": str(e)}
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to delete user data"
        )
