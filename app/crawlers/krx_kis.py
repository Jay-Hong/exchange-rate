"""KRX 미국달러선물 (KIS Open API) — operational glue (PR6a-1).

`app/sources/kis_futures.py` pure adapter를 운영에 연결하기 위한 골격.

PR6a-1 범위 (이 파일):
  - approval_key cache/refresh/lock (asyncio.Lock으로 동시 발급 방지)
  - active session → TR 자동 선택 (CF 주간 / CM 야간)
  - WebSocket connect/subscribe + reconnect 상태 머신
  - tick → normalized payload (11 필드)
  - callback fanout hook (handler 등록은 PR6b 이후)
  - handler 예외는 logger.warning으로 가시화 (fanout 다른 handler에 영향 X)
  - A75605 static canary + front-month resolver TODO 명시

PR6a-1 미포함 (PR6b/c 이후):
  - REST access token helper (snapshot/fallback 시점 추가)
  - DB write (source_rates) — 1초 window + insert-if-changed 정책
  - Redis write (latest:source:krx:usd-krw-futures) — 매 tick SET
  - Alert evaluator hook — 매 tick 평가
  - scheduler 등록 / crawler_config 토글
  - front-month contract resolver — 마스터 파일 (`fo_com_code.mst`) 기반

운영 시 fanout 흐름 (PR6b/c 시점):
  WebSocket tick → on_tick() → callback fanout
                                  ├─ Redis latest SET (매 tick)
                                  ├─ Alert evaluator (매 tick 평가)
                                  └─ DB writer debouncer (1초 window의 last)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
import websockets

from app.sources.kis_futures import (
    get_active_session,
    parse_h0cfasp0_payload,
    parse_h0cfcnt0_payload,
    parse_h0mfasp0_payload,
    parse_h0mfcnt0_payload,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KIS_PROD_HOST = "https://openapi.koreainvestment.com:9443"
KIS_WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"

# approval_key 캐시 위치 — smoke script와 공유 (chmod 600 운영자 책임).
# PR6c 시점에 config로 분리 검토.
APPROVAL_CACHE_PATH = Path(".cache/kis_ws_approval.json")

# approval_key 만료 5분 전 갱신 (race condition 방지 마진).
APPROVAL_REFRESH_MARGIN_SEC = 300

# A75605 static canary (2026-05-18 만기). PR6b/c 전 마스터 기반 resolver 필수.
# TODO(PR6b): front-month contract resolver — fo_com_code.mst 기반 자동 선택.
TR_KEY_STATIC = "A75605"
CONTRACT_MONTH_STATIC = "202605"
CONTRACT_EXPIRES_ON_STATIC = date(2026, 5, 18)

KST = ZoneInfo("Asia/Seoul")

# 세션별 TR 매핑 (smoke script와 동일 decision logic).
SESSION_TR_MAP: Dict[str, List[str]] = {
    "CF": ["H0CFCNT0", "H0CFASP0"],
    "CM": ["H0MFCNT0", "H0MFASP0"],
}

# tick parser dispatch.
PARSER_DISPATCH = {
    "H0CFCNT0": parse_h0cfcnt0_payload,
    "H0CFASP0": parse_h0cfasp0_payload,
    "H0MFCNT0": parse_h0mfcnt0_payload,
    "H0MFASP0": parse_h0mfasp0_payload,
}

# WebSocket reconnect backoff — Codex 권고 (1/2/4/8/16/30, 이후 30s).
RECONNECT_BACKOFF_SEQ = (1, 2, 4, 8, 16, 30)
RECONNECT_BACKOFF_TAIL = 30

# stale 전환 임계 — Codex 권고 60s 미수신.
STALE_AFTER_SEC = 60


# ---------------------------------------------------------------------------
# Normalized payload (PR6a 출력)
# ---------------------------------------------------------------------------

def make_normalized_payload(
    parsed: Dict[str, str],
    *,
    session: str,
    tr_id: str,
    received_at_kst: datetime,
    contract_code: str = TR_KEY_STATIC,
    contract_month: str = CONTRACT_MONTH_STATIC,
    expires_on: date = CONTRACT_EXPIRES_ON_STATIC,
) -> Dict[str, Any]:
    """KIS 체결 (H0CFCNT0/H0MFCNT0) parser 결과를 PR6 표준 normalized payload로 변환.

    PR6a 최소 11 필드 — Codex 합의 양식. price는 str 유지 (DB writer가 변환).

    **체결 tick 전용**: futs_prpr (현재가)을 price로 사용. 호가 tick
    (H0CFASP0/H0MFASP0)은 fanout 대상 아님 — 호가가 체결보다 19× 빈도이고
    매도호가1을 대표값으로 저장하면 "현재가"가 아닌 잘못된 값이 되기 때문.
    호가 데이터 필요해지면 별도 payload_type 또는 별도 path 도입.
    """
    return {
        "source": "krx",
        "asset": "usd-krw-futures",
        "session": session,
        "contract_code": contract_code,
        "contract_month": contract_month,
        "expires_on": expires_on.isoformat(),
        "price": parsed.get("futs_prpr", ""),
        "market_time": parsed.get("bsop_hour", ""),
        "received_at": received_at_kst.isoformat(),
        "tr_id": tr_id,
        "status": "normal",
    }


# ---------------------------------------------------------------------------
# Approval key manager — asyncio.Lock + cache + refresh
# ---------------------------------------------------------------------------

class KisApprovalManager:
    """KIS WebSocket approval_key 발급/캐시 관리.

    동시 발급 방지 (asyncio.Lock) + 만료 5분 전 갱신 + 발급 실패 시 기존
    valid 캐시 사용. KIS 1일 1회 발급 원칙 + 잦은 발급 제한 대응.

    Note: REST access token은 PR6b 이후 (snapshot/fallback 시점)에 추가.
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        cache_path: Path = APPROVAL_CACHE_PATH,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._cache_path = cache_path
        self._lock = asyncio.Lock()

    async def get_approval_key(self) -> str:
        """현재 valid한 approval_key 반환. 만료 임박 시 자동 갱신."""
        async with self._lock:
            cached = self._load_cache()
            if cached and self._is_fresh(cached):
                return cached["approval_key"]

            # 발급 필요 — 단, 발급 실패 시 기존 valid 캐시 fallback.
            try:
                # KIS REST 호출은 동기 requests. asyncio 이벤트 루프 블로킹
                # 회피를 위해 to_thread로 실행. PR6b/c에서 httpx async 검토.
                new_key = await asyncio.to_thread(self._issue_new)
                self._save_cache(new_key)
                return new_key["approval_key"]
            except Exception as e:
                logger.warning(
                    "[kis_approval] 발급 실패 — 기존 캐시 fallback 시도: %s: %s",
                    type(e).__name__, e,
                )
                if cached and self._is_still_valid(cached):
                    return cached["approval_key"]
                raise

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        if not self._cache_path.exists():
            return None
        try:
            return json.loads(self._cache_path.read_text())
        except Exception as e:
            logger.warning("[kis_approval] 캐시 읽기 실패: %s: %s", type(e).__name__, e)
            return None

    def _is_fresh(self, cached: Dict[str, Any]) -> bool:
        """만료 마진(5분) 이상 남아 있으면 True."""
        exp = float(cached.get("expires_at_epoch") or 0)
        return exp > time.time() + APPROVAL_REFRESH_MARGIN_SEC

    def _is_still_valid(self, cached: Dict[str, Any]) -> bool:
        """만료 시각 자체는 안 지났으면 True (발급 실패 fallback 용)."""
        exp = float(cached.get("expires_at_epoch") or 0)
        return exp > time.time()

    def _issue_new(self) -> Dict[str, Any]:
        """KIS REST: POST /oauth2/Approval — smoke script와 동일 endpoint."""
        url = f"{KIS_PROD_HOST}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        r = requests.post(url, json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        approval_key = data.get("approval_key")
        if not approval_key:
            # 응답 전체 노출 X — 진단에 필요한 필드만 인용 (외부 API 응답 secret 가능성).
            raise RuntimeError(
                f"approval_key missing: rt_cd={data.get('rt_cd')} "
                f"msg_cd={data.get('msg_cd')} msg1={data.get('msg1')}"
            )
        # KIS approval_key는 명시적 expires_in이 응답에 없음 — 24h 가정 (smoke와 동일).
        # 실제 만료는 KIS 정책 의존, 보수적으로 23.5h로 두어 갱신 마진 확보.
        expires_in = 23 * 3600 + 1800
        now = time.time()
        return {
            "approval_key": approval_key,
            "expires_at_epoch": now + expires_in,
            "created_at_epoch": now,
        }

    def _save_cache(self, payload: Dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            self._cache_path.chmod(0o600)
        except OSError as e:
            logger.warning("[kis_approval] chmod 600 실패: %s", e)


# ---------------------------------------------------------------------------
# WebSocket client — connect/subscribe + reconnect 상태 머신
# ---------------------------------------------------------------------------

TickHandler = Callable[[Dict[str, Any]], Awaitable[None]]


class KisFuturesClient:
    """KRX 미국달러선물 WebSocket client.

    상태 머신: normal / reconnecting / stale
    - normal: active session, tick 정상 수신
    - reconnecting: disconnect 발생, backoff 재시도 중
    - stale: 60s 이상 tick 미수신 (운영 가시화)

    fanout: tick handler 0개로 시작 (PR6b에서 add_tick_handler로 등록).
    handler 예외는 logger.warning으로 가시화, fanout 다른 handler 영향 X.

    PR6a 한정: 운영 미연결. 외부에서 start() 호출하면 active session
    탐지 후 WebSocket 시작. None 세션이면 대기 (sleep + 주기 체크).
    """

    def __init__(self, approval_manager: KisApprovalManager) -> None:
        self._approval = approval_manager
        self._tick_handlers: List[TickHandler] = []
        self._status: str = "normal"
        self._last_tick_at: Optional[float] = None
        self._stop = asyncio.Event()

    @property
    def status(self) -> str:
        return self._status

    def add_tick_handler(self, handler: TickHandler) -> None:
        """PR6b에서 Redis/Alert/DB writer 등록 진입점."""
        self._tick_handlers.append(handler)

    async def start(self) -> None:
        """메인 루프 — active session 동안 WebSocket 유지, 변경 시 재연결.

        세션 None (휴장/break) 동안엔 30초마다 재체크.
        """
        while not self._stop.is_set():
            now = datetime.now(KST).replace(tzinfo=None)
            session = get_active_session(now)
            if session is None:
                logger.debug("[kis_ws] no active session, sleep 30s")
                self._set_status("normal")  # 휴장은 stale 아님
                await asyncio.sleep(30)
                continue
            await self._run_session(session)

    async def stop(self) -> None:
        self._stop.set()

    async def _run_session(self, session: str) -> None:
        """단일 active session 동안 WebSocket 연결 유지 + reconnect."""
        attempt = 0
        while not self._stop.is_set():
            now = datetime.now(KST).replace(tzinfo=None)
            current_session = get_active_session(now)
            if current_session != session:
                logger.info("[kis_ws] session changed %s → %s, exit loop", session, current_session)
                return

            try:
                approval_key = await self._approval.get_approval_key()
                self._set_status("reconnecting" if attempt > 0 else "normal")
                await self._connect_and_listen(session, approval_key)
                attempt = 0  # 정상 종료 (세션 변경 등) 시 reset
            except Exception as e:
                attempt += 1
                self._set_status("reconnecting")
                backoff = self._compute_backoff(attempt)
                logger.warning(
                    "[kis_ws] connection error (attempt %d): %s: %s — backoff %ds",
                    attempt, type(e).__name__, e, backoff,
                )
                # stale 임계 도달 시 status 갱신 (handler에서 사용 가능)
                if self._last_tick_at and (time.time() - self._last_tick_at > STALE_AFTER_SEC):
                    self._set_status("stale")
                await asyncio.sleep(backoff)

    @staticmethod
    def _compute_backoff(attempt: int) -> int:
        if attempt <= len(RECONNECT_BACKOFF_SEQ):
            return RECONNECT_BACKOFF_SEQ[attempt - 1]
        return RECONNECT_BACKOFF_TAIL

    def _set_status(self, new_status: str) -> None:
        if self._status != new_status:
            logger.info("[kis_ws] status %s → %s", self._status, new_status)
            self._status = new_status

    async def _connect_and_listen(self, session: str, approval_key: str) -> None:
        """WebSocket 연결 + subscribe + recv loop. 예외 시 caller가 reconnect."""
        async with websockets.connect(
            KIS_WS_URL, ping_interval=None, open_timeout=10
        ) as ws:
            logger.info("[kis_ws] connected, session=%s", session)
            for tr_id in SESSION_TR_MAP[session]:
                await ws.send(self._sub_message(approval_key, tr_id, TR_KEY_STATIC))
                logger.info("[kis_ws] subscribed tr_id=%s key=%s", tr_id, TR_KEY_STATIC)

            self._set_status("normal")

            while not self._stop.is_set():
                # 세션 boundary 도달 체크 (15:45 / 06:00 등)
                now = datetime.now(KST).replace(tzinfo=None)
                if get_active_session(now) != session:
                    logger.info("[kis_ws] session boundary reached, disconnect")
                    return

                # stale 임계 — 마지막 tick 후 60s 초과 시 status 갱신
                if (
                    self._last_tick_at
                    and time.time() - self._last_tick_at > STALE_AFTER_SEC
                ):
                    self._set_status("stale")

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue  # idle, 다시 boundary/stale 체크

                # PINGPONG heartbeat — 응답 안 하면 KIS가 일정 시간 후 disconnect.
                # smoke script와 동일 패턴 (line 203-204).
                if isinstance(raw, str) and "PINGPONG" in raw:
                    try:
                        await ws.pong(raw.encode())
                    except Exception as e:
                        logger.warning("[kis_ws] pong send failed: %s: %s", type(e).__name__, e)
                    continue

                await self._handle_frame(raw, session)

    @staticmethod
    def _sub_message(approval_key: str, tr_id: str, tr_key: str) -> str:
        body = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
        return json.dumps(body)

    async def _handle_frame(self, raw: str, session: str) -> None:
        """KIS WebSocket frame 분류 — system message vs tick payload."""
        if not raw:
            return
        if raw.startswith(("0|", "1|")):
            # tick frame: "0|TR_ID|count|data^data^..."
            parts = raw.split("|", 3)
            if len(parts) < 4:
                logger.warning("[kis_ws] malformed tick frame: %r", raw[:120])
                return
            tr_id, _count, data = parts[1], parts[2], parts[3]
            await self._dispatch_tick(tr_id, data, session)
        else:
            # system message (subscribe success/PINGPONG/error 등)
            try:
                msg = json.loads(raw)
                header = msg.get("header") or {}
                body = msg.get("body") or {}
                logger.info(
                    "[kis_ws] system tr_id=%s rt_cd=%s msg_cd=%s msg1=%s",
                    header.get("tr_id"), body.get("rt_cd"),
                    body.get("msg_cd"), body.get("msg1"),
                )
            except Exception:
                logger.debug("[kis_ws] non-JSON system frame: %r", raw[:200])

    async def _dispatch_tick(self, tr_id: str, data: str, session: str) -> None:
        parser = PARSER_DISPATCH.get(tr_id)
        if parser is None:
            logger.warning("[kis_ws] unknown tr_id: %s", tr_id)
            return

        # last_tick_at 갱신은 tick 종류 무관 (호가/체결 둘 다 연결 정상 신호)
        self._last_tick_at = time.time()
        if self._status != "normal":
            self._set_status("normal")

        # PR6a: 체결 tick (H0CFCNT0/H0MFCNT0)만 fanout. 호가 (H0CFASP0/H0MFASP0)는
        # 매도호가1을 대표값으로 잘못 저장할 위험 + 체결보다 19× 빈도라 latest를
        # 매도호가로 덮어쓰기 — 호가 데이터 필요 시 별도 path 도입.
        if tr_id in ("H0CFASP0", "H0MFASP0"):
            logger.debug("[kis_ws] quote tick ignored (PR6a fanout): tr_id=%s", tr_id)
            return

        parsed = parser(data)
        if parsed is None:
            logger.warning("[kis_ws] parser returned None tr_id=%s", tr_id)
            return

        payload = make_normalized_payload(
            parsed,
            session=session,
            tr_id=tr_id,
            received_at_kst=datetime.now(KST).replace(tzinfo=None),
        )
        await self._fanout(payload)

    async def _fanout(self, payload: Dict[str, Any]) -> None:
        """모든 handler 동시 실행 — 한 handler 실패가 다른 handler 영향 X."""
        if not self._tick_handlers:
            return
        results = await asyncio.gather(
            *[h(payload) for h in self._tick_handlers],
            return_exceptions=True,
        )
        for handler, result in zip(self._tick_handlers, results):
            if isinstance(result, Exception):
                # Codex 권고 — 예외 삼키기 X, logger.warning으로 가시화.
                logger.warning(
                    "[kis_ws] handler %s exception: %s: %s",
                    getattr(handler, "__qualname__", repr(handler)),
                    type(result).__name__, result,
                )


# ---------------------------------------------------------------------------
# KrxDbWriter — DB write debouncer (PR6b-2b)
# ---------------------------------------------------------------------------

class KrxDbWriter:
    """KRX 체결 tick → source_rates DB write (1초 window debounce + insert-if-changed).

    REALTIME_ARCHITECTURE_PLAN.md DB writer 정책 (line 88-93):
    매 tick INSERT X, 1초 window의 last 값만 → source_rates 폭증 방지.
    USDT Phase 1 insert-if-changed 정책과 일관 (가격 변동 없으면 0건 INSERT).

    asyncio loop 차단 방지:
      sync SQLAlchemy 호출은 asyncio.to_thread로 격리.
      DB session은 to_thread 내부의 새 thread에서 get_db_context()로 짧게.

    failure isolation (KRX optional source 원칙):
      DB write 실패는 logger.warning으로 가시화하되 propagate X —
      tick 수신 loop / 다른 handler 영향 0.

    race 방지 (PR6b-2b Codex 보정):
      DB write (to_thread) 진행 중 새 tick 들어오면 _last_tick 갱신.
      그러나 _timer는 아직 done X 라 __call__이 새 timer 안 만듦.
      write 종료 후 finally 블록에서 _last_tick 재확인하고 새 timer
      예약 — 누락 방지.

    PR6b-2b 한정: handler 클래스만 정의. scheduler 등록은 PR6c.
    """

    def __init__(self, *, window_sec: float = 1.0) -> None:
        self._window_sec = window_sec
        self._last_tick: Optional[Dict[str, Any]] = None
        self._timer: Optional[asyncio.Task] = None

    async def __call__(self, payload: Dict[str, Any]) -> None:
        """Tick handler — KisFuturesClient fanout에서 호출.

        매 tick의 last 값만 유지, 1초 window timer 시작/유지.
        """
        self._last_tick = payload
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self) -> None:
        """window 만료 후 last tick을 DB에 insert-if-changed.

        DB write (to_thread) 진행 중 새 tick이 들어오면 finally 블록에서
        새 timer 예약 — race 방지 (PR6b-2b Codex 보정).
        """
        await asyncio.sleep(self._window_sec)
        tick = self._last_tick
        self._last_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except Exception as e:
            # KRX optional 원칙 — 격리. propagate X.
            logger.warning(
                "[krx_db_writer] DB write failed (격리): %s: %s",
                type(e).__name__, e,
            )
        finally:
            # DB write 진행 중 들어온 tick 처리 — 새 window 예약.
            # finally 블록은 같은 코루틴 frame 안 — 다른 코루틴 race X
            # (await 없는 sync 영역). __call__이 _timer.done() 체크하기
            # 전에 새 task 할당 완료.
            if self._last_tick is not None:
                self._timer = asyncio.create_task(self._flush_after_window())

    @staticmethod
    def _sync_db_write(tick: Dict[str, Any]) -> None:
        """sync DB 호출 — to_thread 내부에서 실행.

        SessionLocal은 thread-local이라 새 thread에서 새 session 열고 닫기.
        crud.insert_source_rate_if_changed는 내부에서 db.commit() 호출.
        """
        # 함수 내부 import — to_thread만 import 비용, 모듈 로드 영향 X.
        from app import crud
        from app.database import get_db_context

        with get_db_context() as db:
            crud.insert_source_rate_if_changed(
                db=db,
                source=tick["source"],
                asset=tick["asset"],
                rate=float(tick["price"]),
            )


# ---------------------------------------------------------------------------
# Factory — env 기반 client 생성 (운영 진입 시 PR6c에서 사용)
# ---------------------------------------------------------------------------

def build_kis_futures_client() -> KisFuturesClient:
    """env에서 KIS_APP_KEY/KIS_APP_SECRET 로드 + client 생성.

    PR6a-1에서는 entry point만 — 실제 호출은 PR6c (scheduler 등록 시점).
    """
    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        raise RuntimeError(
            "KIS_APP_KEY / KIS_APP_SECRET 환경변수 필요 — "
            "PR6c에서 운영 .env 통합"
        )
    approval = KisApprovalManager(app_key=app_key, app_secret=app_secret)
    return KisFuturesClient(approval_manager=approval)
