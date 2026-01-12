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
from app.notifications.fcm import init_firebase, is_firebase_initialized
from app.subscription import verify_premium_status, PremiumStatus
from app.webhooks import router as webhooks_router

# 로거 설정
logger = logging.getLogger("exchange_rate.main")

PENDING_RETRY_AFTER_SECONDS = "5"

# HTTP Basic Auth 설정
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """관리자 인증 확인"""
    admin_password = os.getenv("ADMIN_PASSWORD", "admin1234")

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

    async def broadcast(self, message: dict):
        """모든 연결된 클라이언트에게 메시지 전송"""
        disconnected = []
        # 리스트 복사본으로 순회 (순회 중 수정 방지)
        for connection in self.active_connections[:]:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning("⚠️ 전송 실패", exc_info=True)
                disconnected.append(connection)

        # 실패한 연결 제거
        for conn in disconnected:
            try:
                self.active_connections.remove(conn)
            except ValueError:
                # 이미 제거됨 (disconnect()에서 제거된 경우)
                pass

manager = ConnectionManager()

# ═════════════════════════════════════════════════════════════
# 그래프 API 인메모리 캐시 (Tier 2 Fallback) - Phase 1A
# ═════════════════════════════════════════════════════════════
_memory_cache = {}
_cache_timestamps = {}
_db_query_timestamps = {}

KST = timezone("Asia/Seoul")

def build_rates_payload(db: SessionLocal) -> dict:
    """DB에서 최신 환율을 조회해 표준 메시지 포맷으로 반환."""
    all_rates = crud.get_all_rates_flat(db=db)
    currencies = list(set(rate["currency"] for rate in all_rates))
    banks = list(set(rate["bank"] for rate in all_rates))

    # DB 데이터의 실제 최신 timestamp 사용 (변경 감지 정확성)
    latest_timestamp = max(
        (rate["timestamp"] for rate in all_rates),
        default=crud.to_kst_isoformat(datetime.now(dt_timezone.utc))
    )

    return {
        "type": "rates",
        "data": {
            "rates": all_rates,
            "metadata": {
                "updated_at": latest_timestamp,
                "currencies": sorted(currencies),
                "banks": sorted(banks),
                "total_count": len(all_rates),
            },
        },
    }


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
    """브로드캐스트 표준 흐름 (Redis 캐시 + 변경 감지 + 조건부 전송)."""
    db = SessionLocal()
    try:
        cached_json = await redis_cache.get(BROADCAST_CACHE_KEY)
        payload = build_rates_payload(db)
        new_json = json.dumps(payload, ensure_ascii=False)

        if new_json != cached_json:
            await redis_cache.set(BROADCAST_CACHE_KEY, new_json)

            if manager.active_connections:
                # 그래프 버킷 추가 (실시간 환율 변경 시에만)
                graph_buckets = await build_graph_buckets()
                total_graph_sources = sum(len(sources) for sources in graph_buckets.values())

                if total_graph_sources:
                    payload["graph_buckets"] = graph_buckets

                await manager.broadcast(payload)

                broadcast_stats.record_success(
                    data_size_bytes=len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                    rate_count=len(payload["data"]["rates"]),
                )

                logger.info(
                    "📡 환율 데이터 브로드캐스트 & 🅾️ Redis 업데이트 완료",
                    extra={
                        "rate_count": len(payload["data"]["rates"]),
                        "connections": len(manager.active_connections),
                        "graph_currencies": len(graph_buckets),
                        "graph_sources": total_graph_sources
                    },
                )
            else:
                broadcast_stats.record_skip(reason="no_connections")
                logger.info("🅾️ Redis 업데이트만 수행 (활성 연결 없음)")
        else:
            broadcast_stats.record_skip(reason="no_changes")
            logger.info("⏸️ 변경사항 없음 - 브로드캐스트 스킵")

    except Exception as e:
        broadcast_stats.record_failure(error_message=str(e))
        logger.error("❌ 브로드캐스트 오류", exc_info=True, extra={"connections": len(manager.active_connections)})
    finally:
        db.close()



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
        currencies = list(set(rate["currency"] for rate in all_rates))
        banks = list(set(rate["bank"] for rate in all_rates))

        current_time = crud.to_kst_isoformat(datetime.now(dt_timezone.utc))

        return {
            "rates": all_rates,
            "metadata": {
                "updated_at": current_time,
                "currencies": sorted(currencies),
                "banks": sorted(banks),
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
    return templates.TemplateResponse("index.html", {"request": request})


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
    return templates.TemplateResponse("admin.html", {"request": request})


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

    KST = timezone('Asia/Seoul')
    now = datetime.now(KST)

    # ===== 시스템 상태 =====
    memory = psutil.virtual_memory()

    # DATABASE_URL에서 실제 DB 경로 추출 (Docker/로컬 환경 모두 호환)
    from app.database import DATABASE_URL
    from pathlib import Path as PathLib

    db_size_mb = 0
    if DATABASE_URL and "sqlite:///" in DATABASE_URL:
        # sqlite:///경로 → 경로 추출 (sqlite:/// 제거)
        db_file_path = DATABASE_URL.replace("sqlite:///", "")
        db_path = PathLib(db_file_path)
        db_size_mb = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0

    process = psutil.Process()
    uptime_seconds = time.time() - process.create_time()
    current_mode = scheduler.current_mode or "UNKNOWN"

    system_status = {
        "websocket_connections": len(manager.active_connections),
        "memory_mb": round(memory.used / (1024 * 1024), 1),
        "memory_percent": round(memory.percent, 1),
        "db_size_mb": round(db_size_mb, 2),
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
# 그래프 API (Phase 1A) - 계층적 Fallback 전략
# ═════════════════════════════════════════════════════════════
@app.get("/api/graph/{currency}")
async def get_graph_data(currency: str):
    """
    24시간 그래프 데이터 반환 (계층적 Fallback)

    Args:
        currency: "usd-krw" | "jpy-krw" | "eur-krw"

    Returns:
        {
            "pair": "usd-krw",
            "as_of": "2025-11-29T14:59:45+09:00",
            "sources": {
                "investing": [[ts, max, min, close], ...],
                "kb": [...],
                "hana": [...]
            }
        }

    Fallback 순서:
        1. Redis 캐시 (120초 TTL)
        2. 인메모리 캐시 (60초 TTL)
        3. DB 조회 (Rate Limiting: 10초에 1번)
        4. 503 Service Unavailable
    """
    if currency not in ["usd-krw", "jpy-krw", "eur-krw"]:
        raise HTTPException(status_code=400, detail="Invalid currency pair")

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
            from app.admin.graph_cache import build_graph_series

            sources_data = {}
            max_timestamp = 0

            for source in ["investing", "kb", "hana"]:
                series, latest_ts = build_graph_series(source, currency)
                sources_data[source] = series
                if latest_ts > max_timestamp:
                    max_timestamp = latest_ts

            return sources_data, max_timestamp

        try:
            sources_data, max_timestamp = await loop.run_in_executor(executor, _fetch_graph)

            response = {
                "pair": currency,
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

    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")

    crud.delete_notification_setting(db=db, setting_id=setting_id, user_id=user_id)

    logger.info(
        "🔔 알림 설정 삭제",
        extra={"event": "notification_setting_delete", "setting_id": setting_id}
    )

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
                "deleted_devices": deleted_devices
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
