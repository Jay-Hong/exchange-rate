"""USDT WebSocket — Korbit canary client (Phase B.5 Stage K2-K3).

USDT_WS_DESIGN_PLAN §12.7 Phase B.5.
Coinone Stage C2-C3 패턴 mirror (작은 단위 stage 분할 학습 — KRX close finalizer
1100 lines → 3 High findings → revert + Phase B.3/B.4 분할 재시작 학습 적용).

K2 lifecycle skeleton (완료):
    - `_stop_event` + `_running` (network/Redis/DB/Alert import 없음)
    - flag=false 시 scheduler가 KorbitWsClient 자체를 생성 안 함 (acceptance)

K3 connect/subscribe/parse + log only (현재):
    - Constants: WS URL / symbol / recv timeout / subscribe requestId
    - `_build_subscribe_payload`: Korbit list-wrap form (Coinone single-dict와 다름)
    - `_parse_ticker_message`: ticker frame → normalized tick (close string → float,
      `data.lastTradedAt` 우선 + top-level `timestamp` fallback, snapshot key 무시)
    - `_handle_message`: 분기 3+1 (`status` 필드 unified ACK/ERROR + `type=="ticker"` +
      unknown safe log-only) + ticker tick return, 그 외 None
    - `_run_one_session`: connect + subscribe + recv loop (1s timeout for stop_event 반응)
    - `start`: `_run_one_session` 단일 실행 (reconnect 없음, K4로 분리)
    - **scope guard**: Redis/DB/Alert/REST writer/PING loop/reconnect 일체 미진입

K4 이후 누적 예정 (USDT_WS_DESIGN_PLAN §12.7.4):
    - K4: liveness + ws.ping() (Bithumb mirror, K-2 smoke 검증) + 2 status 분리
      (PONG=liveness / DATA age=freshness, 30s warning / 120s degraded provisional)
      + reconnect loop + UsdtLivenessMonitor
    - K5: KorbitRedisWriter + topic trigger 자동 발화
    - K6: KorbitDbWriter + REST fallback + `fetch_korbit_usdt_tick()` normalized helper
      (contract `{source, asset, rate, timestamp_ms}` Coinone/Bithumb과 동일, 내부
      timestamp source는 `data.lastTradedAt`)
    - K7: UsdtAlertEvaluator wiring
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.korbit")


# ─────────────────────────────────────────────────────────────────────────
# Constants — USDT_EXCHANGE_WEBSOCKET_GUIDE §6 + USDT_WS_DESIGN_PLAN §12.7.3
# ─────────────────────────────────────────────────────────────────────────

KORBIT_WS_URL = "wss://ws-api.korbit.co.kr/v2/public"
# Korbit symbol notation — 소문자 underscore (WS/REST 일관). guide §6 참조.
KORBIT_SYMBOL = "usdt_krw"
# Subscribe payload requestId — ACK frame echo로 추적 가능. K-2 smoke 검증: 1 OK.
SUBSCRIBE_REQUEST_ID = 1

# K3 recv loop timeout — stop_event 즉시 반응 + idle 허용 (Coinone/Bithumb 동일).
RECV_TIMEOUT_SEC = 1.0


class KorbitWsClient:
    """Korbit USDT/KRW WebSocket client — K2 skeleton + K3 connect/parse.

    State:
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first ticker INFO log 1회 emit 후 DEBUG 격하

    K4 이후 누적 예정 (liveness/Redis/DB/Alert state — Coinone `CoinoneWsClient` 패턴).
    """

    def __init__(self) -> None:
        # K2 lifecycle
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # K3 session state
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False

    @staticmethod
    def _build_subscribe_payload() -> list:
        """Korbit subscribe payload — list-wrap form (Coinone single-dict와 다름).

        USDT_EXCHANGE_WEBSOCKET_GUIDE §6: list 안에 단일 subscribe dict.
        K-2 smoke 검증: 30분 valid run에서 ACK + 213 ticker frame 정상 수신.
        """
        return [
            {
                "requestId": SUBSCRIBE_REQUEST_ID,
                "method": "subscribe",
                "type": "ticker",
                "symbols": [KORBIT_SYMBOL],
            }
        ]

    def _parse_ticker_message(self, message: dict) -> Optional[dict]:
        """Korbit ticker frame → normalized tick dict, None on invalid/non-ticker.

        Normalized shape (Upbit/Bithumb/Coinone `fetch_*_usdt_tick` contract 일치):
            {"source": "korbit", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}

        timestamp_ms는 raw int 유지 (KST/UTC 변환은 K6 DB writer 영역).
        timestamp source: `data.lastTradedAt` 우선, 부재 시 top-level `timestamp` fallback
        (USDT_WS_DESIGN_PLAN §12.7.3 결정 5 — sparse-time 보수적 보존).

        Guard:
            - type != "ticker" → None
            - symbol != "usdt_krw" → None
            - data dict 부재 → None
            - close 부재 / parse 실패 / <= 0 → None
            - lastTradedAt + top-level timestamp 둘 다 부재 / parse 실패 → None

        Snapshot 처리 (K-2 smoke 검증):
            - first frame에 `snapshot: true` 1회, 이후 ticker는 key 자체 누락
            - 가격값은 동일 추출 가능 → snapshot 필드 무시
        """
        if message.get("type") != "ticker":
            return None
        if message.get("symbol") != KORBIT_SYMBOL:
            return None
        data = message.get("data")
        if not isinstance(data, dict):
            return None

        close_raw = data.get("close")
        if close_raw is None:
            return None
        try:
            rate = float(close_raw)
        except (TypeError, ValueError):
            return None
        if rate <= 0:
            return None

        # timestamp: data.lastTradedAt 우선, 부재 시 top-level timestamp fallback
        ts_raw = data.get("lastTradedAt")
        if ts_raw is None:
            ts_raw = message.get("timestamp")
        if ts_raw is None:
            return None
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            return None

        return {
            "source": "korbit",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> Optional[dict]:
        """Korbit frame → 분기 3+1 + log only. ticker tick dict 반환, 그 외 None.

        Frame types (USDT_EXCHANGE_WEBSOCKET_GUIDE §6 + K-2 smoke 실측):
            - ACK/ERROR (unified `status` 키): `{"status":"success"|"fail", "requestId":...}`
              — subscribe ACK 1회 (status=success) + error 시 status=fail
            - ticker (`type=="ticker"`): `_parse_ticker_message`로 normalize.
              first tick INFO log + 이후 DEBUG (Coinone 패턴 mirror).
            - unknown / missing: WARNING (safe log).

        Acceptance (K3):
            - return: ticker만 tick dict / 그 외 None (K5~K7 callsite 재사용 위해 유지)
            - downstream IO 0 (Redis/DB/alert/REST 미호출 — K5~K7 영역)
        """
        # raw decode (bytes/str/dict)
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("[usdt_ws.korbit] non-utf8 frame, skip")
                return None
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[usdt_ws.korbit] invalid JSON frame, skip")
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            logger.warning("[usdt_ws.korbit] unexpected raw type=%s, skip", type(raw).__name__)
            return None

        if not isinstance(message, dict):
            logger.warning("[usdt_ws.korbit] non-dict message, skip")
            return None

        # Unified ACK/ERROR via `status` 분기 (K-2 smoke 검증, USDT_WS_DESIGN_PLAN §12.7.3 결정 2)
        if "status" in message:
            status = message.get("status")
            if status == "success":
                logger.info(
                    "[usdt_ws.korbit] subscribe ACK requestId=%s",
                    message.get("requestId"),
                )
            elif status == "fail":
                logger.warning(
                    "[usdt_ws.korbit] ERROR code=%s message=%s requestId=%s",
                    message.get("code"),
                    message.get("message"),
                    message.get("requestId"),
                )
            else:
                logger.warning(
                    "[usdt_ws.korbit] unknown status=%r, skip",
                    status,
                )
            return None

        if message.get("type") == "ticker":
            tick = self._parse_ticker_message(message)
            if tick is None:
                logger.warning("[usdt_ws.korbit] ticker parse failed, skip")
                return None
            if not self._first_tick_logged:
                logger.info("[usdt_ws.korbit] first tick", extra=tick)
                self._first_tick_logged = True
            else:
                logger.debug("[usdt_ws.korbit] tick", extra=tick)
            return tick

        # unknown / missing type — safe log (Coinone 패턴 mirror)
        logger.warning(
            "[usdt_ws.korbit] unknown frame type=%r keys=%s, skip",
            message.get("type"),
            list(message.keys()),
        )
        return None

    async def _run_one_session(self) -> None:
        """단일 connect → subscribe → recv loop. log only (downstream IO 0).

        Acceptance (K3):
            - websockets.connect(URL, ping_interval=None) — WS protocol PING은 K4
              (ws.ping() Bithumb mirror, K-2 smoke 검증 완료: 5회 정상, median 14.64ms)
            - subscribe payload send (list-wrap, K-2 검증)
            - recv loop with RECV_TIMEOUT_SEC=1.0 (stop_event 즉시 반응)
            - `_handle_message(raw)` 호출, return value 무시 (log only)
            - ConnectionClosed → propagate (start에서 catch)
            - reconnect loop / liveness / PING은 K4
        """
        async with websockets.connect(
            KORBIT_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[usdt_ws.korbit] connected url=%s", KORBIT_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.korbit] subscribed symbol=%s requestId=%s",
                KORBIT_SYMBOL, SUBSCRIBE_REQUEST_ID,
            )

            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    if self._stop_event.is_set():
                        logger.info("[usdt_ws.korbit] connection closed after stop")
                        return
                    raise
                # K3: parse + log only (return value 무시). K5~K7에서 downstream IO 추가 예정.
                self._handle_message(raw)

    async def start(self) -> None:
        """K3 — `_run_one_session` 단일 실행 (reconnect 없음, K4로 분리).

        Acceptance (Stage K3):
            - 중복 start 방지 (_running flag)
            - 단일 session 실행 — session crash 시 logger.warning + return
            - reconnect loop은 K4에서 추가
        """
        if self._running:
            logger.debug("[usdt_ws.korbit] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.korbit] start (K3 — connect/subscribe/parse log only)")
        try:
            await self._run_one_session()
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            logger.warning("[usdt_ws.korbit] connection closed (no reconnect in K3): %s", exc)
        except Exception:
            logger.exception("[usdt_ws.korbit] session error (no reconnect in K3)")
        finally:
            self._running = False
            self._ws = None

    async def stop(self) -> None:
        """stop signal — `_run_one_session()`의 recv loop가 다음 RECV_TIMEOUT 시점에 빠짐."""
        self._stop_event.set()
