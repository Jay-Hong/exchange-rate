"""Gopax USDT/KRW WebSocket client — Phase B.6 Stage G2.

USDT_WS_DESIGN_PLAN §12.9 (Phase B.6, 2026-05-21).

G2 scope (이 파일의 현재 범위):
    G1 lifecycle skeleton + Subscribe/Parse/USDT-KRW 클라이언트 필터링까지.
    Primus heartbeat (G3) / Liveness + reconnect (G4) / Writers + fallback +
    alert (G5-G7) / Telemetry (PR 2e)는 별도 stage.

⚠️ Production activation 제약 (G2 단계 — Codex 강조):
    G2부터 `_run_one_session()`이 실제 `websockets.connect()`를 호출한다.
    flag=true 시:
        1. connect 성공 → SubscribeToTickers 송신 성공
        2. Initial response (전체 ticker array) → USDT-KRW 매칭 → first tick log
        3. ~30초 후 server `"primus::ping::<epoch_ms>"` 송신
        4. G2는 skip만 (pong 응답은 G3에서 추가) → server 30s timeout → disconnect
        5. **G4 reconnect loop 부재** → `_run_one_session()` 종료. session 1회만
           실행되고 reconnect 없음. `start()` task는 종료되지 않고 stop_event
           대기 상태로 **idle** 유지 (`_running=True`). 새 tick도 없고 reconnect도
           없는 silent idle 상태 — silent termination이 아니라 silent stuck에 가깝다.

    따라서 G2 land 후에도 `USDT_WS_GOPAX_ENABLED=true` 토글 금지.
    Production env는 default false 유지하며, **G3 (Primus pong) + G4 (reconnect/
    liveness) land 후에만 activation 검토**.

G2 acceptance:
    - flag=false 시 GopaxWsClient 생성 X + task 생성 X + network connect X (G1 동일)
    - flag=true 시 connect + subscribe + recv + parse 까지만, fanout 0
    - USDT-KRW만 클라이언트 필터링하여 first tick INFO 1회 + 이후 DEBUG
    - Primus ping 3가지 form 모두 skip (pong 응답은 G3)

G2 신규 attribute (Codex 최종 권고 — 필요한 것만):
    - `_ws`: connect 결과 ws 객체 (recv loop 동안 보유)
    - `_first_tick_logged`: first tick INFO 1회 emit (이후 DEBUG)
    G3~G4 attribute (`_connection_status` / `_ticker_freshness_status` /
    `_reconnect_attempt_count` / `_liveness` 등)는 본 stage 제외.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.gopax")

# Gopax WebSocket endpoint (Primus protocol — G3에서 pong 응답 추가).
# 공식 문서: https://gopax.github.io/wsapi/
GOPAX_WS_URL = "wss://wsapi.gopax.co.kr"

# G2 recv loop wake-up cycle — stop_event 즉시 반응용.
RECV_TIMEOUT_SEC = 1.0

# Gopax 거래쌍 명칭 — 클라이언트측 필터링 키.
GOPAX_TARGET_PAIR = "USDT-KRW"


class GopaxWsClient:
    """Gopax USDT/KRW WebSocket client — Phase B.6 Stage G2.

    G1 lifecycle skeleton + G2 connect/subscribe/parse 누적. Codex 최종 권고대로
    `_connection_status` / `_ticker_freshness_status` / `_reconnect_attempt_count` /
    `_liveness` 등 G4에서 실제 필요해질 attribute는 본 stage에서 제외 (선반영 회피).

    ⚠️ Production activation 제약 (G2):
        G2 land 후에도 USDT_WS_GOPAX_ENABLED=true 토글 금지. G3 (Primus pong) +
        G4 (reconnect/liveness) land 후에만 activation 검토. 자세한 내용은 module
        docstring 참조.
    """

    def __init__(self) -> None:
        # G1 lifecycle state
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # G2 session state — connect 결과 + first tick log throttle
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False

    @staticmethod
    def _build_subscribe_payload() -> dict:
        """Gopax SubscribeToTickers payload — pair 지정 불가, 전체 ticker 구독.

        guide §7 spec: {"n": "SubscribeToTickers", "o": {}}
        클라이언트측에서 USDT-KRW만 필터링 (`_parse_ticker_message` 안).
        """
        return {"n": "SubscribeToTickers", "o": {}}

    @staticmethod
    def _normalize_tick(item: dict) -> Optional[dict]:
        """Gopax ticker item → normalized tick {source, asset, rate, timestamp_ms}.

        Guard (다른 4 source 패턴 mirror):
            - last 누락 / 0 / negative → None
            - lastTraded 누락 / 0 → None (timestamp 0은 invalid)
            - 타입 변환 실패 → None (격리)
        """
        try:
            rate = float(item["last"])
            if rate <= 0:
                return None
            timestamp_ms = int(item.get("lastTraded", 0) or 0)
            if timestamp_ms <= 0:
                return None
            return {
                "source": "gopax",
                "asset": "usdt-krw",
                "rate": rate,
                "timestamp_ms": timestamp_ms,
            }
        except (KeyError, ValueError, TypeError):
            return None

    def _parse_ticker_message(self, raw) -> Optional[dict]:
        """Gopax 2종 응답 + Primus ping skip (G2 placeholder) + USDT-KRW 필터링.

        Frame 종류:
            - Initial response: `n="SubscribeToTickers"`, `o.data`는 전체 ticker array
              → array iterate + `tradingPairName=="USDT-KRW"` 매칭
            - Delta: `n="TickerEvent"`, `o["USDT-KRW"]`는 dict
              → key 존재 시 직접 추출
            - Primus ping (server-initiated heartbeat): G2에서 skip만, G3에서 pong 응답

        Primus skip 정책 (G2 scope, Codex 정정 반영):
            1. JSON string form: `'"primus::ping::..."'` → strip 후 startswith 매칭
            2. Plain text form: `'primus::ping::...'` → startswith 매칭
            3. JSON-decoded str: `json.loads()` 결과가 str이고 `"primus::"` 시작
            세 가지 형태 모두 None return. G3에서 pong replacement 응답 추가.

        Returns normalized tick {source, asset, rate, timestamp_ms} or None.
        """
        # 1. bytes → str
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None

        # 2. Primus skip — raw text level (fast path).
        # JSON string form ('"primus::...'") + plain text form ('primus::...') 모두 처리.
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith('"primus::') or text.startswith("primus::"):
                return None

        # 3. JSON parse
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            return None

        # 4. JSON-decoded str case — json.loads('"primus::..."') 결과가 str "primus::..."
        if isinstance(message, str):
            if message.startswith("primus::"):
                return None
            return None  # unknown str frame → skip

        # 5. dict frame type 분기
        if not isinstance(message, dict):
            return None

        n = message.get("n")
        if n == "SubscribeToTickers":
            # Initial response — 전체 ticker array에서 USDT-KRW 매칭
            o = message.get("o", {})
            if not isinstance(o, dict):
                return None
            data = o.get("data", [])
            if not isinstance(data, list):
                return None
            for item in data:
                if isinstance(item, dict) and item.get("tradingPairName") == GOPAX_TARGET_PAIR:
                    return self._normalize_tick(item)
            return None
        elif n == "TickerEvent":
            # Delta — USDT-KRW key 존재 시 직접 추출
            o = message.get("o", {})
            if not isinstance(o, dict):
                return None
            item = o.get(GOPAX_TARGET_PAIR)
            if isinstance(item, dict):
                return self._normalize_tick(item)
            return None

        return None

    def _handle_message(self, raw) -> Optional[dict]:
        """raw frame → normalized tick (or None). First tick INFO 1회 + 이후 DEBUG.

        Bithumb `_handle_message` 패턴 mirror — fanout (Redis/DB/Alert)은 G5-G7 영역.
        """
        tick = self._parse_ticker_message(raw)
        if tick is None:
            return None
        if not self._first_tick_logged:
            logger.info("[usdt_ws.gopax] first tick", extra=tick)
            self._first_tick_logged = True
        else:
            logger.debug("[usdt_ws.gopax] tick", extra=tick)
        return tick

    async def _run_one_session(self) -> None:
        """G2 single session — connect + subscribe + recv loop + parse.

        Lifecycle (G2 scope):
            1. websockets.connect(GOPAX_WS_URL)
            2. SubscribeToTickers payload send
            3. recv loop: wake every RECV_TIMEOUT_SEC (stop_event 반응) + parse only
            4. fanout (Redis/DB/Alert) 없음 — G5-G7 영역

        ⚠️ G2 단독 flag=true는 production 활성화 금지:
            recv loop가 Primus ping 도착 → skip만 (G3 pong 미구현) → server 30s
            disconnect → ConnectionClosed → _run_one_session 종료. G4 reconnect
            loop 부재라 재시작 없음 — `start()` task는 stop_event 대기 상태로
            **idle** 유지 (silent termination이 아니라 silent stuck). 자세한 내용은
            module docstring 참조.
        """
        async with websockets.connect(
            GOPAX_WS_URL,
            ping_interval=None,    # WS auto-ping 비활성 — Primus heartbeat는 G3에서 처리
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[usdt_ws.gopax] connected url=%s", GOPAX_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.gopax] subscribed all tickers (client-side filter pair=%s)",
                GOPAX_TARGET_PAIR,
            )

            try:
                while not self._stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        continue
                    except ConnectionClosed:
                        # G2: server disconnect (e.g., Primus pong 미응답 30s)
                        # → session 종료. G4 reconnect loop에서 재시도 추가 예정.
                        logger.warning(
                            "[usdt_ws.gopax] connection closed during recv (G2 — no reconnect)",
                        )
                        return
                    self._handle_message(raw)
                    # G2 scope: tick detect만, fanout은 G5+ 영역
            finally:
                self._ws = None

    async def start(self) -> None:
        """G2 single session lifecycle — connect/subscribe/recv 1회 후 stop_event 대기.

        G1 lifecycle은 그대로 (no network), G2부터 `_run_one_session()` 호출 추가.
        G4 reconnect loop 부재라 session 종료 시 재시도 없음 — `stop_event.wait()`로
        **idle 상태 진입** (`_running=True` 유지). task 자체는 종료되지 않고
        stop_event set 호출까지 대기. stop() 호출 시에만 finally에서 `_running=False`로
        전환되며 task done.

        ⚠️ 이는 silent termination이 아니라 silent idle/stuck — Primus pong 미응답으로
        세션이 종료되면 새 tick도 없고 reconnect도 없는 상태로 task가 살아있다.
        Production env=true 금지 사유 핵심 — module docstring 참조.

        Acceptance:
            - 중복 start 방지 (`_running` flag)
            - stop_event set 시 즉시 종료 (idle wait 풀림)
            - G4 미구현 — single session 1회만, 종료 후 재시작 없음 (idle 상태로 남음)
            - flag=false 시 본 함수 호출 자체가 발생하지 않음 (scheduler가 차단)
        """
        if self._running:
            logger.debug("[usdt_ws.gopax] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info(
            "[usdt_ws.gopax] start (G2 — connect/subscribe/parse, no reconnect/heartbeat/fanout)",
        )
        try:
            try:
                await self._run_one_session()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[usdt_ws.gopax] _run_one_session crashed (G2 — no reconnect)",
                )
            # G2: single session 종료 후 stop_event 대기 (재시작 없음).
            # G4 reconnect loop 추가 시 본 wait는 reconnect loop으로 대체.
            if not self._stop_event.is_set():
                await self._stop_event.wait()
        finally:
            self._running = False
            logger.info("[usdt_ws.gopax] start exited")

    async def stop(self) -> None:
        """stop signal — recv loop / start wait 모두 풀어줌. idempotent."""
        self._stop_event.set()
