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
from app import models, schemas, crud, scheduler
from app.database import engine, SessionLocal, Base
from app.admin.stats import broadcast_stats
from app.cache import redis_cache, BROADCAST_CACHE_KEY
from app.notifications.fcm import init_firebase, is_firebase_initialized, send_fcm_data_only
from app.subscription import verify_premium_status, PremiumStatus
from app.webhooks import router as webhooks_router

# 로거 설정
logger = logging.getLogger("exchange_rate.main")

PENDING_RETRY_AFTER_SECONDS = "5"

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
    premium_status = await verify_premium_status(user_id)

    if premium_status == PremiumStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Subscription status pending. Retry later.",
            headers={"Retry-After": PENDING_RETRY_AFTER_SECONDS},
        )

    if premium_status == PremiumStatus.INACTIVE:
        if allow_empty:
            return False
        raise HTTPException(
            status_code=403,
            detail="Premium subscription required",
        )

    return True


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


# DB 테이블 생성
Base.metadata.create_all(bind=engine)

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

    latest_dxy = crud.get_latest_dxy_rate(db)
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

    # 스케줄러 시작 (Queue를 사용하는 작업 + WebSocket Broadcasting 포함)
    scheduler.start_scheduler()

    # ✅ Broadcasting은 APScheduler에서 자동 실행 (매분 00, 10, 20, 30, 40, 50초)

    yield

    # Shutdown code
    logger.info("🛑 FastAPI 서버 종료")

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

        # 1b) payload build (DB SELECT + 직렬화 객체 구성)
        # 분해 계측: investing/bank/source_rates_legacy 단계별 timing을 함께 받는다.
        t1 = time.perf_counter()
        payload, query_timings = build_rates_payload_with_timings(db)
        timings["payload_build_ms"] = (time.perf_counter() - t1) * 1000
        timings.update(query_timings)

        # 2) JSON serialize + diff
        t2 = time.perf_counter()
        new_json = json.dumps(payload, ensure_ascii=False)
        is_changed = new_json != cached_json
        timings["serialize_diff_ms"] = (time.perf_counter() - t2) * 1000

        if is_changed:
            await redis_cache.set(BROADCAST_CACHE_KEY, new_json)

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
            # 클라이언트로부터 ping 메시지를 받으면 JSON 형식으로 pong 응답
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        logger.info("🔌 클라이언트 연결 해제")


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


@app.get("/api/rates", response_model=schemas.ExchangeRatesResponse)
def get_rates_for_mobile(db: Session = Depends(get_db)):
    """모바일 앱과 AJAX용 플랫 배열 구조 API (폴백용)"""
    try:
        all_rates = crud.get_all_rates_flat(db=db)

        # metadata.currencies/metadata.banks는 레거시 호환용 dead field.
        # build_rates_payload와 동일한 정책으로 레거시 값 고정.
        currencies = sorted(crud.SUPPORTED_CURRENCY_PAIRS)
        banks = sorted(crud.LEGACY_METADATA_BANKS)

        current_time = crud.to_kst_isoformat(datetime.now(dt_timezone.utc))

        return {
            "rates": all_rates,
            "metadata": {
                "updated_at": current_time,
                "currencies": currencies,
                "banks": banks,
                "total_count": len(all_rates)
            }
        }

    except Exception as e:
        # 에러 발생 시 빈 데이터 반환
        return {
            "rates": [],
            "metadata": {
                "updated_at": crud.to_kst_isoformat(datetime.now(dt_timezone.utc)),
                "currencies": ["usd-krw", "jpy-krw", "eur-krw"], 
                "banks": [],
                "total_count": 0,
                "error": str(e)
            }
        }


@app.get("/api/rates/{currency}")
def get_rates_by_currency(currency: str, db: Session = Depends(get_db)):
    """특정 통화쌍의 모든 환율 조회 (모바일 앱용)"""
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
            is_enabled=body.is_enabled
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
                "is_enabled": body.is_enabled
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

    updated = crud.update_notification_setting(
        db=db,
        setting_id=setting_id,
        user_id=user_id,
        bank=bank_value,
        condition=condition_value,
        threshold=body.threshold,
        enabled=body.is_enabled
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
# USDT Phase 1: Source 기반 알림 API (/api/source-notification-settings)
# ═══════════════════════════════════════════════════════════════════════════════
# - 기존 /api/notification-settings와 완전 분리된 새 API family
# - DB: source_notification_settings 테이블 사용
# - source/asset은 source_registry에서 검증 (phase1_enabled=True만 허용)

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
        created_at=crud.to_kst_isoformat(setting.created_at),
        updated_at=crud.to_kst_isoformat(setting.updated_at),
        triggered_at=crud.to_kst_isoformat(setting.last_notified_at),
    )


def _validate_phase1_source_asset(source: str, asset: str) -> None:
    """Phase 1 source 알림 대상 검증.

    허용 대상:
    - phase1_enabled=True
    - category == "exchange" (거래소만)

    reference 소스(investing, kb, hana)는 기존 `/api/notification-settings`를 사용해야 한다.
    이유: process_source_rate_alerts는 usdt_sources 크롤러에서만 호출되므로,
    reference 소스를 허용하면 생성은 되지만 발송되지 않는 "dead alert"가 된다.

    derivative(KRX 등)는 Phase 2에서 별도 정책으로 허용 여부 결정.
    """
    from app import source_registry
    definition = source_registry.get_source_definition(source, asset)
    if definition is None or not definition.phase1_enabled:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source/asset combination: {source}:{asset}",
        )
    if definition.category != "exchange":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Source alerts are only supported for exchange sources in Phase 1 "
                f"(got category={definition.category}). "
                f"Use /api/notification-settings for bank/investing alerts."
            ),
        )


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
    Source 기반 알림 설정 생성 (USDT Phase 1).

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

    _validate_phase1_source_asset(body.source, body.asset)

    try:
        setting = crud.create_source_notification_setting(
            db=db,
            user_id=user_id,
            source=body.source,
            asset=body.asset,
            condition=body.condition.value,
            threshold=body.threshold,
            is_enabled=body.is_enabled,
        )

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
        asset: asset 필터 (선택, 예: usdt-krw)
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
        - source/asset 변경 시 phase1_enabled 조합인지 검증
        - source, asset, condition, threshold 실제 변경 시 triggered 초기화
        - is_enabled: False→True 전환 시에도 triggered 초기화
    """
    user_id = await verify_firebase_token(request)
    await require_premium(user_id, allow_empty=False)

    setting = crud.get_source_notification_setting_by_id(db=db, setting_id=setting_id, user_id=user_id)
    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")

    # source/asset 중 하나라도 바뀌면 최종 조합을 검증
    new_source = body.source if body.source is not None else setting.source
    new_asset = body.asset if body.asset is not None else setting.asset
    if body.source is not None or body.asset is not None:
        _validate_phase1_source_asset(new_source, new_asset)

    condition_value = body.condition.value if body.condition else None

    updated = crud.update_source_notification_setting(
        db=db,
        setting_id=setting_id,
        user_id=user_id,
        source=body.source,
        asset=body.asset,
        condition=condition_value,
        threshold=body.threshold,
        enabled=body.is_enabled,
    )

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

    crud.delete_source_notification_setting(db=db, setting_id=setting_id, user_id=user_id)

    logger.info(
        "🔔 source 알림 설정 삭제",
        extra={"event": "source_notification_setting_delete", "setting_id": setting_id},
    )

    await notify_user_devices_sync(db, user_id)
    return schemas.DeleteResponse(success=True, message="Setting deleted")


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

        # Source 기반 알림 (USDT Phase 1)
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
