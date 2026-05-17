"""USDT WebSocket — Bithumb canary client (Phase B.3 Stage U3 connect/subscribe/parse).

USDT_WS_DESIGN_PLAN §12.5 Phase B.3 Stage U2-U3.
Upbit Phase B.1 PR1-PR2 패턴 작은 복제 (KRX close finalizer 분할 학습 — 큰 추상화 금지).

U2 누적 (lifecycle skeleton):
    - `_stop_event` + `_running` (network/Redis/DB import 없음)
    - flag=false 시 scheduler가 BithumbWsClient 자체를 생성 안 함 (acceptance)

U3 (현재): connect/subscribe/parse + log only.
    - Bithumb WS endpoint + ticket + target code 상수
    - `_build_subscribe_payload` (Upbit-compatible)
    - `_parse_ticker_message` (Upbit-compatible payload format)
    - `_handle_message` (parse + first_tick log + tick log, downstream IO 없음)
    - `_run_one_session` (connect + subscribe + recv loop + parse, no reconnect)
    - `start` (U2 stop_event 대기 → U3 single session loop)
    - NO reconnect/liveness (U4)
    - NO Redis/DB/topic/fallback (U5/U6)

Bithumb Upbit-compatible 근거 (USDT_EXCHANGE_WEBSOCKET_GUIDE §4 +
2026-05-17 Python websockets smoke 재확인):
    - Subscribe message format 동일: `[{ticket}, {type=ticker, codes=["KRW-USDT"]}, {format=DEFAULT}]`
    - Payload format 동일: `{type=ticker, code, trade_price, trade_timestamp, timestamp}`
    - 파싱 동일: `float(trade_price)` + `int(trade_timestamp or timestamp)`

후속 stage 예정 (별도 GO):
    U4: UsdtLivenessMonitor 재사용 + reconnect + ping/pong heartbeat
    U5: BithumbRedisWriter (tick-level) + topic trigger 자동 발화
    U6: BithumbDbWriter (1초 window) + BithumbRestFallbackController +
        fetch_bithumb_usdt_tick() normalized REST helper
        ({source, asset, rate, timestamp_ms} shape — Upbit helper contract 일치)

운영 활성화 조건 (USDT_WS_DESIGN_PLAN §12.5.3):
    KRX close finalizer 5/18 CF + 5/19 CM 첫 실측 + 7일 telemetry 안정 후
    별도 deploy GO. 그 전까지 USDT_WS_BITHUMB_ENABLED=false default.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import websockets

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.bithumb")


# ─────────────────────────────────────────────────────────────────────────
# Constants — Upbit-compatible (USDT_EXCHANGE_WEBSOCKET_GUIDE §4)
# ─────────────────────────────────────────────────────────────────────────

BITHUMB_WS_URL = "wss://ws-api.bithumb.com/websocket/v1"
BITHUMB_SUBSCRIBE_TICKET = "fxi-usdt-bithumb"
BITHUMB_TARGET_CODE = "KRW-USDT"

# U3 recv loop timeout — Upbit 동일 (stop_event 즉시 반응 + 정상 idle 허용)
RECV_TIMEOUT_SEC = 1.0


class BithumbWsClient:
    """Bithumb USDT/KRW WebSocket client — U3 connect/subscribe/parse + log only.

    State (U3):
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first tick INFO log 1회 emit 후 DEBUG로 격하

    U3 핵심:
        - Single-session connect + subscribe + recv loop
        - parse + log only (downstream IO 없음)
        - No reconnect (U4), no liveness/heartbeat (U4)
        - No Redis/DB/topic/fallback (U5/U6)
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False

    @staticmethod
    def _build_subscribe_payload() -> list[dict]:
        """Bithumb subscribe payload — Upbit-compatible 3-element list."""
        return [
            {"ticket": BITHUMB_SUBSCRIBE_TICKET},
            {"type": "ticker", "codes": [BITHUMB_TARGET_CODE]},
            {"format": "DEFAULT"},
        ]

    def _parse_ticker_message(self, raw) -> Optional[dict]:
        """Bithumb ticker frame → normalized tick dict, None on invalid/ignore.

        Accepts bytes (utf-8 JSON), str (JSON), or dict (pre-parsed for tests).
        Normalized shape (U5+ 재사용 계약, Upbit fetch_upbit_usdt_tick contract 일치):
            {"source": "bithumb", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}

        Bithumb payload format (Upbit-compatible, USDT_EXCHANGE_WEBSOCKET_GUIDE §4):
            {"type": "ticker", "code": "KRW-USDT", "trade_price": <number>,
             "trade_timestamp": <ms>, "timestamp": <ms>, "stream_type": "REALTIME"/"SNAPSHOT"}

        Guard (Upbit 패턴 mirror):
            - 0/negative rate → None (잘못된 fanout 차단)
            - timestamp missing → None
            - type != "ticker" or code != KRW-USDT → None (다른 frame 무시)
        """
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            return None

        if not isinstance(message, dict):
            return None
        if message.get("type") != "ticker":
            return None
        if message.get("code") != BITHUMB_TARGET_CODE:
            return None

        try:
            rate = float(message["trade_price"])
        except (TypeError, ValueError, KeyError):
            return None
        if rate <= 0:
            return None

        ts_raw = message.get("trade_timestamp") or message.get("timestamp")
        if ts_raw is None:
            return None
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            return None

        return {
            "source": "bithumb",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> Optional[dict]:
        """parse + log only. Returns normalized tick dict or None.

        U3 시점: downstream IO 없음 (Redis/DB/alert는 U5/U6).
        non-ticker (status / 다른 code / parse 실패) → None (silent skip).
        """
        tick = self._parse_ticker_message(raw)
        if tick is None:
            return None
        if not self._first_tick_logged:
            logger.info("[usdt_ws.bithumb] first tick", extra=tick)
            self._first_tick_logged = True
        else:
            logger.debug("[usdt_ws.bithumb] tick", extra=tick)
        return tick

    async def _run_one_session(self) -> None:
        """단일 connect → subscribe → recv loop. U3은 reconnect 없음.

        Upbit `_run_one_session` PR2 시점 mirror — liveness/Redis/DB/alert/fallback 없음.
        U4에서 reconnect + UsdtLivenessMonitor + ping/pong 추가 예정.
        """
        async with websockets.connect(
            BITHUMB_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[usdt_ws.bithumb] connected url=%s", BITHUMB_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.bithumb] subscribed code=%s ticket=%s",
                BITHUMB_TARGET_CODE, BITHUMB_SUBSCRIBE_TICKET,
            )

            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    if self._stop_event.is_set():
                        logger.info("[usdt_ws.bithumb] connection closed after stop")
                        return
                    raise
                # U3: parse + log only. downstream IO는 U5/U6에서 추가.
                self._handle_message(raw)

    async def start(self) -> None:
        """U3 single-session start — connect + subscribe + recv loop.

        중복 start 방지. stop_event 도달 또는 ConnectionClosed/Exception 시 종료.
        U4에서 reconnect loop + backoff 추가 예정.
        """
        if self._running:
            logger.debug("[usdt_ws.bithumb] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.bithumb] start (U3 single session)")
        try:
            await self._run_one_session()
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed as exc:
            logger.warning("[usdt_ws.bithumb] connection closed: %s", exc)
        except Exception as exc:
            logger.warning(
                "[usdt_ws.bithumb] session error: %s: %s",
                type(exc).__name__, exc,
            )
        finally:
            self._running = False
            self._ws = None
            logger.info("[usdt_ws.bithumb] start exit")

    async def stop(self) -> None:
        """stop_event set → recv loop 종료.

        idempotent — 이미 set이어도 무동작.
        """
        self._stop_event.set()
