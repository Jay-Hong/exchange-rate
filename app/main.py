# app/main.py

# 표준 라이브러리
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Dict, Any

# 서드파티 라이브러리
from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pytz import timezone
from sqlalchemy.orm import Session
import secrets

# 로컬 애플리케이션
from app import models, schemas, crud, scheduler
from app.database import engine, SessionLocal, Base
from app.utils.broadcast_stats import broadcast_stats

# 로거 설정
logger = logging.getLogger("exchange_rate.main")

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
        self.active_connections.remove(websocket)
        logger.info("❌ WebSocket 연결 해제", extra={"connections": len(self.active_connections)})

    async def broadcast(self, message: dict):
        """모든 연결된 클라이언트에게 메시지 전송"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning("⚠️ 전송 실패", exc_info=True)
                disconnected.append(connection)

        # 실패한 연결 제거
        for conn in disconnected:
            self.active_connections.remove(conn)

manager = ConnectionManager()

# 마지막 브로드캐스트 시간 추적 (변경 감지용)
last_broadcast_time = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup code
    logger.info("🚀 FastAPI 서버 시작", extra={"env": os.getenv("ENV", "development")})
    scheduler.start_scheduler()
    # WebSocket 브로드캐스트 백그라운드 태스크 시작
    asyncio.create_task(broadcast_rates())
    yield
    # Shutdown code
    logger.info("🛑 FastAPI 서버 종료")

async def broadcast_rates():
    """10초마다 변경사항 체크 후 모든 클라이언트에게 환율 데이터 전송"""
    global last_broadcast_time

    while True:
        await asyncio.sleep(10)  # 10초 대기

        if manager.active_connections:
            db = SessionLocal()
            try:
                # 변경사항 체크 (초경량 쿼리)
                has_changes = crud.has_changes_since(db, last_broadcast_time)

                if has_changes or last_broadcast_time is None:
                    # 변경이 있을 때만 브로드캐스트
                    all_rates = crud.get_all_rates_flat(db=db)

                    # 메타데이터 생성
                    currencies = list(set(rate["currency"] for rate in all_rates))
                    banks = list(set(rate["bank"] for rate in all_rates))

                    current_time = datetime.now().astimezone().isoformat()

                    # 통일된 메시지 형식
                    message = {
                        "type": "rates",
                        "data": {
                            "rates": all_rates,
                            "metadata": {
                                "updated_at": current_time,
                                "currencies": sorted(currencies),
                                "banks": sorted(banks),
                                "total_count": len(all_rates)
                            }
                        }
                    }

                    # 데이터 크기 계산 (통계용)
                    data_size_bytes = len(json.dumps(message, ensure_ascii=False).encode('utf-8'))

                    # 브로드캐스트
                    await manager.broadcast(message)

                    # 마지막 브로드캐스트 시간 업데이트
                    KST = timezone('Asia/Seoul')
                    last_broadcast_time = datetime.now(KST)

                    # 통계 기록 (성공)
                    broadcast_stats.record_success(
                        data_size_bytes=data_size_bytes,
                        rate_count=len(all_rates)
                    )

                    logger.info("📡 환율 데이터 브로드캐스트 완료", extra={"rate_count": len(all_rates), "connections": len(manager.active_connections)})
                else:
                    # 통계 기록 (스킵)
                    broadcast_stats.record_skip(reason="no_changes")
                    logger.info("⏸️ 변경사항 없음 - 브로드캐스트 스킵")

            except Exception as e:
                # 통계 기록 (실패)
                broadcast_stats.record_failure(error_message=str(e))
                logger.error("❌ 브로드캐스트 오류", exc_info=True, extra={"connections": len(manager.active_connections)})
            finally:
                db.close()

app = FastAPI(lifespan=lifespan)

# 정적 파일 서비스 (은행 아이콘 이미지)
app.mount("/static", StaticFiles(directory="static"), name="static")

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
    
    # 연결 직후 즉시 현재 데이터 전송
    db = SessionLocal()
    try:
        all_rates = crud.get_all_rates_flat(db=db)
        currencies = list(set(rate["currency"] for rate in all_rates))
        banks = list(set(rate["bank"] for rate in all_rates))

        current_time = datetime.now().astimezone().isoformat()
        
        # 통일된 메시지 형식
        initial_message = {
            "type": "rates",
            "data": {
                "rates": all_rates,
                "metadata": {
                    "updated_at": current_time,
                    "currencies": sorted(currencies),
                    "banks": sorted(banks),
                    "total_count": len(all_rates)
                }
            }
        }
        
        await websocket.send_json(initial_message)
        logger.info("📨 초기 데이터 전송 완료", extra={"rate_count": len(all_rates)})

    except Exception as e:
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

        current_time = datetime.now().astimezone().isoformat()

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
                "updated_at": datetime.now().astimezone().isoformat(),
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
                "updated_at": datetime.now().astimezone().isoformat(),
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
    db_path = BASE_DIR / "data" / "exchange_rates.db"
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
    from app.utils.log_reader import read_logs

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
    from app.utils.log_reader import read_logs

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
    from app.utils.log_reader import read_logs
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


