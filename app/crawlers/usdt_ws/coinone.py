"""USDT WebSocket — Coinone canary client (Phase B.4 Stage C2-C3).

USDT_WS_DESIGN_PLAN §12.6 Phase B.4.
Bithumb Stage U2-U3 패턴 mirror (작은 단위 stage 분할 학습 — KRX close finalizer
1100 lines → 3 High findings → revert + Phase B.3 U2~U7 분할 재시작 학습 적용).

C2 lifecycle skeleton (완료):
    - `_stop_event` + `_running` (network/Redis/DB/Alert import 없음)
    - flag=false 시 scheduler가 CoinoneWsClient 자체를 생성 안 함 (acceptance)

C3 connect/subscribe/parse + log only (현재):
    - Constants: WS URL / quote/target currency / recv timeout
    - `_build_subscribe_payload`: Coinone single-dict form (별 protocol)
    - `_parse_data_message`: DATA response → normalized tick (last string → float,
      timestamp int ms raw 유지)
    - `_handle_message`: response_type 분기 5+1 (CONNECTED → session_id 캡처,
      SUBSCRIBED, DATA, PONG, ERROR, unknown safe log-only) + DATA tick return,
      그 외 None
    - `_run_one_session`: connect + subscribe + recv loop (1s timeout for
      stop_event reactivity)
    - `start`: `_run_one_session` 단일 실행 (reconnect 없음, C4로 분리)
    - **scope guard**: Redis/DB/Alert/REST writer/PING loop/reconnect 일체 미진입

C4 이후 누적 예정 (USDT_WS_DESIGN_PLAN §12.6.4):
    - C4: 5분 application PING + 5s PONG timeout + 2-signal 분리 (PONG=liveness /
      DATA age=freshness) + reconnect loop + UsdtLivenessMonitor
    - C5: CoinoneRedisWriter + topic trigger 자동 발화
    - C6: CoinoneDbWriter + REST fallback + `fetch_coinone_usdt_tick()` normalized helper
    - C7: UsdtAlertEvaluator wiring
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.coinone")


# ─────────────────────────────────────────────────────────────────────────
# Constants — USDT_EXCHANGE_WEBSOCKET_GUIDE §5 + USDT_WS_DESIGN_PLAN §12.6.3
# ─────────────────────────────────────────────────────────────────────────

COINONE_WS_URL = "wss://stream.coinone.co.kr"
COINONE_QUOTE_CURRENCY = "KRW"
COINONE_TARGET_CURRENCY = "USDT"

# C3 recv loop timeout — stop_event 즉시 반응 + idle 허용 (Bithumb 동일).
RECV_TIMEOUT_SEC = 1.0


class CoinoneWsClient:
    """Coinone USDT/KRW WebSocket client — C2 skeleton + C3 connect/parse.

    State:
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first DATA tick INFO log 1회 emit 후 DEBUG 격하
        - `_session_id`: CONNECTED frame `data.session_id` 캡처 (debugging/logging)

    C4 이후 누적 예정 (liveness/Redis/DB/Alert state — Bithumb `BithumbWsClient` 패턴).
    """

    def __init__(self) -> None:
        # C2 lifecycle
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # C3 session state
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False
        self._session_id: Optional[str] = None

    @staticmethod
    def _build_subscribe_payload() -> dict:
        """Coinone subscribe payload — single-dict form (별 protocol, Upbit/Bithumb과 다름).

        USDT_EXCHANGE_WEBSOCKET_GUIDE §5: 대문자 enum 필수 (request_type/channel).
        DEFAULT format 유지 (USDT_WS_DESIGN_PLAN §12.6.3 결정 4 — SHORT는 범위 밖).
        """
        return {
            "request_type": "SUBSCRIBE",
            "channel": "TICKER",
            "topic": {
                "quote_currency": COINONE_QUOTE_CURRENCY,
                "target_currency": COINONE_TARGET_CURRENCY,
            },
        }

    def _parse_data_message(self, message: dict) -> Optional[dict]:
        """Coinone DATA frame → normalized tick dict, None on invalid/non-DATA.

        Normalized shape (Upbit/Bithumb fetch_*_usdt_tick contract 일치):
            {"source": "coinone", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}

        timestamp_ms는 raw int 유지 (KST/UTC 변환은 C6 DB writer 영역).

        Guard:
            - response_type != "DATA" → None
            - data dict 부재 → None
            - quote_currency != KRW or target_currency != USDT → None
            - last 부재 / parse 실패 / <= 0 → None
            - timestamp 부재 / parse 실패 → None
        """
        if message.get("response_type") != "DATA":
            return None
        data = message.get("data")
        if not isinstance(data, dict):
            return None
        if data.get("quote_currency") != COINONE_QUOTE_CURRENCY:
            return None
        if data.get("target_currency") != COINONE_TARGET_CURRENCY:
            return None

        last_raw = data.get("last")
        if last_raw is None:
            return None
        try:
            rate = float(last_raw)
        except (TypeError, ValueError):
            return None
        if rate <= 0:
            return None

        ts_raw = data.get("timestamp")
        if ts_raw is None:
            return None
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            return None

        return {
            "source": "coinone",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> Optional[dict]:
        """Coinone frame → response_type 분기 + log only. DATA tick dict 반환, 그 외 None.

        Response types (USDT_EXCHANGE_WEBSOCKET_GUIDE §5 + C0 smoke 실측):
            - CONNECTED: handshake 직후 1회. `data.session_id` 캡처 (debugging).
            - SUBSCRIBED: subscribe ACK 1회. log only.
            - DATA: ticker frame. `_parse_data_message`로 normalize.
              first tick INFO log + 이후 DEBUG (Bithumb 패턴 mirror).
            - PONG: PING 응답. C4에서 latency 측정 예정. C3는 DEBUG only.
            - ERROR: 서버 error 응답. WARNING (error_message log).
            - unknown / missing: WARNING (safe log — Codex 강조 2: unknown safe).

        Acceptance (C3):
            - return: DATA만 tick dict / 그 외 None (C5~C7 callsite 재사용 위해 유지)
            - downstream IO 0 (Redis/DB/alert/REST 미호출 — C5~C7 영역)
        """
        # raw decode (bytes/str/dict)
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("[usdt_ws.coinone] non-utf8 frame, skip")
                return None
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[usdt_ws.coinone] invalid JSON frame, skip")
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            logger.warning("[usdt_ws.coinone] unexpected raw type=%s, skip", type(raw).__name__)
            return None

        if not isinstance(message, dict):
            logger.warning("[usdt_ws.coinone] non-dict message, skip")
            return None

        rtype = message.get("response_type")

        if rtype == "CONNECTED":
            # data field가 dict가 아닐 경우 (malformed frame) safe fallback — C3 "safe
            # log-only" invariant 유지 (AttributeError로 session crash 방지).
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            session_id = data.get("session_id")
            if isinstance(session_id, str) and session_id:
                self._session_id = session_id
            logger.info(
                "[usdt_ws.coinone] CONNECTED session_id=%s",
                self._session_id,
            )
            return None

        if rtype == "SUBSCRIBED":
            # data field defensive — CONNECTED와 동일 패턴
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "[usdt_ws.coinone] SUBSCRIBED channel=%s topic=%s",
                message.get("channel"), data,
            )
            return None

        if rtype == "DATA":
            tick = self._parse_data_message(message)
            if tick is None:
                logger.warning("[usdt_ws.coinone] DATA parse failed, skip")
                return None
            if not self._first_tick_logged:
                logger.info("[usdt_ws.coinone] first tick", extra=tick)
                self._first_tick_logged = True
            else:
                logger.debug("[usdt_ws.coinone] tick", extra=tick)
            return tick

        if rtype == "PONG":
            logger.debug("[usdt_ws.coinone] PONG")
            return None

        if rtype == "ERROR":
            logger.warning(
                "[usdt_ws.coinone] ERROR error_code=%s error_message=%s",
                message.get("error_code"), message.get("error_message"),
            )
            return None

        # unknown / missing response_type — safe log (Codex 강조 2)
        logger.warning(
            "[usdt_ws.coinone] unknown response_type=%r, skip",
            rtype,
        )
        return None

    async def _run_one_session(self) -> None:
        """단일 connect → subscribe → recv loop. log only (downstream IO 0).

        Acceptance (C3):
            - websockets.connect(URL, ping_interval=None) — application-level PING은 C4
            - subscribe payload send (single-dict, 별 protocol)
            - recv loop with RECV_TIMEOUT_SEC=1.0 (stop_event 즉시 반응)
            - `_handle_message(raw)` 호출, return value 무시 (log only)
            - ConnectionClosed → propagate (start에서 catch)
            - reconnect loop / liveness / PING은 C4
        """
        async with websockets.connect(
            COINONE_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[usdt_ws.coinone] connected url=%s", COINONE_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.coinone] subscribed channel=TICKER topic=%s/%s",
                COINONE_QUOTE_CURRENCY, COINONE_TARGET_CURRENCY,
            )

            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    if self._stop_event.is_set():
                        logger.info("[usdt_ws.coinone] connection closed after stop")
                        return
                    raise
                # C3: parse + log only (return value 무시). C5~C7에서 downstream IO 추가 예정.
                self._handle_message(raw)

    async def start(self) -> None:
        """C3 — `_run_one_session` 단일 실행 (reconnect 없음, C4로 분리).

        Acceptance (Stage C3):
            - 중복 start 방지 (_running flag)
            - 단일 session 실행 — session crash 시 logger.warning + return
            - reconnect loop은 C4에서 추가
        """
        if self._running:
            logger.debug("[usdt_ws.coinone] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.coinone] start (C3 — connect/subscribe/parse log only)")
        try:
            await self._run_one_session()
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            logger.warning("[usdt_ws.coinone] connection closed (no reconnect in C3): %s", exc)
        except Exception:
            logger.exception("[usdt_ws.coinone] session error (no reconnect in C3)")
        finally:
            self._running = False
            self._ws = None

    async def stop(self) -> None:
        """stop signal — `_run_one_session()`의 recv loop가 다음 RECV_TIMEOUT 시점에 빠짐."""
        self._stop_event.set()
