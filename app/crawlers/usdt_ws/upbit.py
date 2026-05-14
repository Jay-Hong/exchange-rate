"""USDT WebSocket — Upbit canary client (PR2).

PR2 scope (USDT_WS_DESIGN_PLAN §12.2): websockets.connect / subscribe payload /
ticker message parse / log only. NO reconnect (PR3), NO Redis (PR4),
NO DB (PR5), NO alert (PR6), NO REST fallback (PR7).

Lifecycle (scheduler.py에서 호출):
    client = UpbitWsClient()
    task = asyncio.create_task(client.start())
    ...
    await client.stop()
    await task

Disconnect 동작: PR2는 단일 session. 서버 측 disconnect / 네트워크 에러 시
exception이 wrapper(`_run_usdt_ws_upbit_client`)로 bubble up → logger.exception
→ task 종료. reconnect는 PR3에서 추가.

Parser 계약 (`_parse_ticker_message`)은 PR4 (Redis writer) / PR5 (DB writer) /
PR6 (AlertObservation evaluator)이 재사용할 normalized tick shape를 정의.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.upbit")

UPBIT_WS_URL = "wss://api.upbit.com/websocket/v1"
UPBIT_SUBSCRIBE_TICKET = "fxi-usdt-upbit"
UPBIT_TARGET_CODE = "KRW-USDT"
# recv timeout — stop event 폴링 응답성 (KRX 5s 패턴보다 짧게).
# async for 대신 wait_for 루프를 쓰는 이유: stop event 즉시 반응 + PR3 silence
# metric 부착 시 recv_at/last_tick_at 추적 자연스러움.
RECV_TIMEOUT_SEC = 1.0


class UpbitWsClient:
    """Upbit USDT/KRW WebSocket client (PR2 single-session)."""

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # websockets 16에서 WebSocketClientProtocol deprecated. Any로 유지하고
        # PR3 reconnect 진입 시 안정화된 타입으로 교체.
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False

    async def start(self) -> None:
        """단일 session 실행. reconnect 없음 (PR3에서 추가).

        예외는 wrapper(`_run_usdt_ws_upbit_client`)가 logger.exception 처리.
        """
        if self._running:
            logger.debug("[usdt_ws.upbit] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.upbit] start (PR2 single-session)")
        try:
            await self._run_one_session()
        finally:
            self._running = False
            self._ws = None
            logger.info("[usdt_ws.upbit] session ended")

    async def stop(self) -> None:
        """stop event set + ws close → recv wait_for / async loop 즉시 해제."""
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.exception("[usdt_ws.upbit] ws.close() 실패")

    @staticmethod
    def _build_subscribe_payload() -> list[dict]:
        """Upbit 3-frame subscribe payload (guide §3).

        [{ticket}, {type:ticker, codes:[KRW-USDT]}, {format:DEFAULT}]
        """
        return [
            {"ticket": UPBIT_SUBSCRIBE_TICKET},
            {"type": "ticker", "codes": [UPBIT_TARGET_CODE]},
            {"format": "DEFAULT"},
        ]

    def _parse_ticker_message(self, raw) -> Optional[dict]:
        """Upbit ticker frame → normalized tick dict, None on invalid/ignore.

        Accepts bytes (utf-8 JSON), str (JSON), or dict (pre-parsed for tests).

        Normalized shape (PR4-PR6 재사용):
            {
                "source": "upbit",
                "asset": "usdt-krw",
                "rate": float,           # positive
                "timestamp_ms": int,     # trade_timestamp 우선, timestamp 폴백
            }

        None 케이스:
            - bytes utf-8 decode 실패
            - JSON parse 실패
            - dict 아닌 JSON (list, scalar 등)
            - type != "ticker" (status/error 등 silent ignore)
            - code != "KRW-USDT"
            - trade_price 누락/비수치/<=0
            - trade_timestamp AND timestamp 둘 다 없음/비정수
        """
        # 입력 정규화
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

        # 필터: USDT/KRW ticker만
        if message.get("type") != "ticker":
            return None
        if message.get("code") != UPBIT_TARGET_CODE:
            return None

        # rate: 양수 float
        try:
            rate = float(message["trade_price"])
        except (TypeError, ValueError, KeyError):
            return None
        if rate <= 0:
            return None

        # timestamp: trade_timestamp 우선, timestamp 폴백 (guide §3)
        ts_raw = message.get("trade_timestamp") or message.get("timestamp")
        if ts_raw is None:
            return None
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            return None

        return {
            "source": "upbit",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> None:
        """parse + log only. PR4/PR5/PR6에서 downstream side-effect 추가 예정."""
        tick = self._parse_ticker_message(raw)
        if tick is None:
            return
        # 첫 tick INFO (연결/구독 검증 신호), 후속 DEBUG (운영 noise 회피)
        if not self._first_tick_logged:
            logger.info("[usdt_ws.upbit] first tick", extra=tick)
            self._first_tick_logged = True
        else:
            logger.debug("[usdt_ws.upbit] tick", extra=tick)

    async def _run_one_session(self) -> None:
        """단일 connect → subscribe → recv loop. PR3에서 reconnect wrapping."""
        async with websockets.connect(UPBIT_WS_URL) as ws:
            self._ws = ws
            logger.info("[usdt_ws.upbit] connected url=%s", UPBIT_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.upbit] subscribed code=%s ticket=%s",
                UPBIT_TARGET_CODE, UPBIT_SUBSCRIBE_TICKET,
            )

            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    # stop()이 ws.close()를 부른 경우 정상 종료 — wrapper
                    # `_run_usdt_ws_upbit_client`가 crashed로 잘못 로깅하지 않도록.
                    # 비요청 close (server-side / network)는 raise → wrapper가
                    # exception 로깅 → PR3 reconnect가 필요하다는 신호 유지.
                    if self._stop_event.is_set():
                        logger.info("[usdt_ws.upbit] connection closed after stop")
                        break
                    raise
                self._handle_message(raw)
