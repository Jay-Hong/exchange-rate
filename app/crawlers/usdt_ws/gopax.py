"""Gopax USDT/KRW WebSocket client — Phase B.6 Stage G3.

USDT_WS_DESIGN_PLAN §12.9 (Phase B.6, 2026-05-21).

G3 scope (이 파일의 현재 범위):
    G1 lifecycle skeleton + G2 Subscribe/Parse/USDT-KRW 필터링 + G3 Primus pong
    handler + heartbeat timestamp 기록까지. Liveness + reconnect (G4) / Writers
    + fallback + alert (G5-G7) / Telemetry (PR 2e)는 별도 stage.

G3 주요 변경:
    - `_is_primus_ping()` static method — Primus ping **전용** 매칭 (3 form 처리,
      Codex 정정: pong/open/close 등 다른 control frame은 False).
    - `_handle_primus_ping(ws, raw)` async method — pong replacement send +
      `_last_heartbeat_at` 갱신 (send 성공 시점에만). **bool return** (Codex 정정).
    - `_run_one_session()` 안 Primus 분기 추가 — ping 매칭 시 pong send →
      성공이면 continue, 실패면 session 종료 (Codex 정정: G4 reconnect 부재라 깨진
      session 명시적 종료).
    - 신규 attribute `_last_heartbeat_at: Optional[float]` (G4 liveness에서 활용).

⚠️ Production activation 제약 (G3 단계도 유지 — Codex 강조):
    G3는 Primus pong 처리되지만 **G4 reconnect/liveness 부재**라서:
        1. connect 성공 → SubscribeToTickers 송신 성공
        2. Initial response → USDT-KRW 매칭 → first tick log
        3. ~30초 후 server `"primus::ping::<epoch_ms>"` 송신
        4. G3: pong replacement 송신 + `_last_heartbeat_at` 갱신 → session 유지
        5. **단** pong send 실패 또는 ConnectionClosed 발생 시 `_run_one_session()`
           종료 → G4 reconnect loop 부재라 재시작 없음. `start()` task는
           stop_event 대기 상태로 **idle** 유지 (`_running=True`). 새 tick도
           reconnect도 없는 silent idle 상태 — silent termination이 아니라
           silent stuck에 가깝다.

    따라서 G3 land 후에도 `USDT_WS_GOPAX_ENABLED=true` 토글 금지.
    Production env는 default false 유지하며, **G4 (reconnect + liveness) land 후
    에만 activation 검토** (G3 단독으로는 단일 session 후 idle 위험 여전).

G3 acceptance:
    - flag=false 시 GopaxWsClient 생성 X + task 생성 X (G1/G2 동일)
    - flag=true 시 connect + subscribe + recv + parse + Primus pong까지
    - Primus ping 3 form 모두 pong replacement 송신 + heartbeat 갱신 (Codex 정정:
      ping 전용 — pong/open/close는 별도 처리)
    - pong send 실패 시 session 명시적 종료 (Codex 정정: log-only 금지)

G3 누적 attribute (G1 + G2 + G3):
    - G1: `_stop_event` / `_running`
    - G2: `_ws` / `_first_tick_logged`
    - G3: `_last_heartbeat_at` (G4 liveness에서 활용, G3는 기록만)
    G4 attribute (`_connection_status` / `_ticker_freshness_status` /
    `_reconnect_attempt_count` / `_liveness`)는 여전히 본 stage 제외.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
    """Gopax USDT/KRW WebSocket client — Phase B.6 Stage G3.

    G1 lifecycle skeleton + G2 connect/subscribe/parse + G3 Primus pong handler
    누적. Codex 최종 권고대로 `_connection_status` / `_ticker_freshness_status` /
    `_reconnect_attempt_count` / `_liveness` 등 G4에서 실제 필요해질 attribute는
    본 stage에서 제외 (선반영 회피).

    ⚠️ Production activation 제약 (G3 단계도 유지):
        G3는 Primus pong을 처리하지만 G4 reconnect/liveness 부재라서 pong send
        실패 또는 ConnectionClosed 시 여전히 silent idle/stuck 상태. G3 land 후
        에도 USDT_WS_GOPAX_ENABLED=true 토글 금지. G4 land 후에만 activation
        검토. 자세한 내용은 module docstring 참조.
    """

    def __init__(self) -> None:
        # G1 lifecycle state
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # G2 session state — connect 결과 + first tick log throttle
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False
        # G3 heartbeat — Primus pong send 성공 시점 timestamp.
        # G4 liveness/status 판단에서 활용 예정. G3는 기록만, 판단 X.
        self._last_heartbeat_at: Optional[float] = None

    @staticmethod
    def _build_subscribe_payload() -> dict:
        """Gopax SubscribeToTickers payload — pair 지정 불가, 전체 ticker 구독.

        guide §7 spec: {"n": "SubscribeToTickers", "o": {}}
        클라이언트측에서 USDT-KRW만 필터링 (`_parse_ticker_message` 안).
        """
        return {"n": "SubscribeToTickers", "o": {}}

    @staticmethod
    def _is_primus_ping(raw) -> bool:
        """Primus ping 매칭 — **ping 전용** (Codex 정정).

        G3는 pong 응답 단계라 ping만 잡는다. pong/open/close 등 다른 Primus
        control frame은 False return (`_parse_ticker_message`의 G2 skip 분기에서
        별도 처리). 3 form 모두 처리:
            (i) JSON string form: '"primus::ping::..."' (with outer quotes)
            (ii) Plain text form: 'primus::ping::...' (no quotes)
            (iii) bytes form: G2 fast-path와 동일하게 decode 후 매칭

        Returns:
            True: Primus ping frame (3 form 중 하나)
            False: 그 외 (일반 ticker / non-ping Primus control frame / invalid)
        """
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return False
        if not isinstance(raw, str):
            return False
        text = raw.strip()
        return text.startswith('"primus::ping::') or text.startswith("primus::ping::")

    async def _handle_primus_ping(self, ws, raw) -> bool:
        """Primus ping → pong replacement + ws.send + _last_heartbeat_at 갱신.

        guide §7 line 408-410 명세:
            - JSON string form: '"primus::ping::..."' → '"primus::pong::..."'
            - Plain text form: 'primus::ping::...' → 'primus::pong::...'
            - replace("::ping::", "::pong::")로 두 form 모두 처리 가능

        Heartbeat timestamp 정책 (Codex 검토 포인트 #3):
            send 성공 시점에만 `_last_heartbeat_at = time.time()` 갱신. send 실패
            시 갱신 안 함 (이후 G4 liveness가 stale 판정 가능).

        Returns:
            True: pong 송신 성공 (heartbeat 갱신 완료)
            False: send 실패 — Codex 정정으로 _run_one_session()에서 session 종료
                   (G4 reconnect 부재라 깨진 session 명시적 종료).
        """
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        pong = raw.replace("::ping::", "::pong::")
        try:
            await ws.send(pong)
            self._last_heartbeat_at = time.time()
            logger.debug("[usdt_ws.gopax] primus pong sent")
            return True
        except Exception:
            logger.exception(
                "[usdt_ws.gopax] primus pong send 실패 — session 종료 "
                "(G3 — G4 reconnect 부재)",
            )
            return False

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
        """G3 single session — connect + subscribe + recv loop + Primus pong + parse.

        Lifecycle (G3 scope):
            1. websockets.connect(GOPAX_WS_URL)
            2. SubscribeToTickers payload send
            3. recv loop: wake every RECV_TIMEOUT_SEC (stop_event 반응)
            4. Primus ping → pong send + `_last_heartbeat_at` 갱신 → continue
               (Codex 정정: send 실패 시 session 명시적 종료)
            5. 그 외 frame → parse + handle
            6. fanout (Redis/DB/Alert) 없음 — G5-G7 영역

        Primus 처리 우선 순위 (Codex 검토 포인트):
            - ping만 잡고 pong/open/close 등 다른 control frame은 parse layer로 전달
              (parse는 None return — G2 defensive skip)
            - send 성공 시점에만 heartbeat 갱신 (실패는 갱신 없음)
            - send 실패 → session 종료 (return) → G4 reconnect loop 추가 시 자연 통합

        ⚠️ G3 단독 flag=true는 production 활성화 금지:
            Primus pong은 응답하지만 G4 reconnect/liveness 부재라 ConnectionClosed
            또는 pong send 실패 시 session 종료 → `start()` task는 stop_event
            대기 상태로 **idle** 유지 (silent termination이 아니라 silent stuck).
            자세한 내용은 module docstring 참조.
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
                        # G3: server disconnect — session 종료.
                        # G4 reconnect loop에서 재시도 추가 예정.
                        logger.warning(
                            "[usdt_ws.gopax] connection closed during recv (G3 — no reconnect)",
                        )
                        return

                    # G3: Primus ping 먼저 처리 (pong 송신 + heartbeat 갱신).
                    if self._is_primus_ping(raw):
                        if not await self._handle_primus_ping(ws, raw):
                            # Codex 정정: send 실패 → session 종료 (G4 reconnect 부재).
                            return
                        continue

                    # 그 외 frame → parse + handle. fanout은 G5+ 영역.
                    self._handle_message(raw)
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
            "[usdt_ws.gopax] start (G3 — connect/subscribe/parse + Primus pong, no reconnect/liveness/fanout)",
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
