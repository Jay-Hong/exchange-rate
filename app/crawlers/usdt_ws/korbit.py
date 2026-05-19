"""USDT WebSocket — Korbit canary client (Phase B.5 Stage K2 skeleton).

USDT_WS_DESIGN_PLAN §12.7 Phase B.5 Stage K2.
Coinone Stage C2 패턴 mirror — 작은 단위 stage 분할 (KRX close finalizer
1100 lines → 3 High findings → revert + Phase B.3/B.4 분할 재시작 학습 적용).

K2 lifecycle skeleton:
    - `_stop_event` + `_running` (network/Redis/DB/Alert import 없음)
    - flag=false 시 scheduler가 KorbitWsClient 자체를 생성 안 함 (acceptance)
    - flag=true 시 task 등록되지만 `start()`는 `_stop_event.wait()` 대기만

K3 이후 누적 예정 (USDT_WS_DESIGN_PLAN §12.7.4):
    - K3: WS connect/subscribe/parse — list-wrap subscribe build
      (`[{requestId, method:"subscribe", type:"ticker", symbols:["usdt_krw"]}]`) +
      ticker parser (`type=="ticker"`, `data.close`, `data.lastTradedAt`) +
      unified ACK/ERROR `status` 분기 + snapshot 무시 처리
    - K4: 5분 PING (ws.ping() Bithumb mirror) + 5s PONG timeout +
      2-signal 분리 (PONG=liveness / DATA age=freshness, 30s warning / 120s degraded)
    - K5: KorbitRedisWriter + topic trigger 자동 발화
    - K6: KorbitDbWriter + REST fallback + `fetch_korbit_usdt_tick()` normalized helper
      (contract `{source, asset, rate, timestamp_ms}` Coinone/Bithumb mirror)
    - K7: UsdtAlertEvaluator wiring
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.korbit")


class KorbitWsClient:
    """Korbit USDT/KRW WebSocket client — K2 skeleton only.

    State:
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag

    K3 이후 누적 예정 (network/Redis/DB/Alert state — Coinone `CoinoneWsClient` 패턴).
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def start(self) -> None:
        """K2 skeleton — stop_event 대기만, network 호출 없음.

        Acceptance (Stage K2):
            - flag=true 시 task 등록되지만 본 메서드는 stop_event 대기 외 무동작
            - 중복 start 방지 (_running flag)

        K3 이후 reconnect loop + connect/subscribe/parse 추가 예정.
        """
        if self._running:
            logger.debug("[usdt_ws.korbit] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.korbit] start (K2 skeleton — stop_event 대기)")
        try:
            await self._stop_event.wait()
        finally:
            self._running = False

    async def stop(self) -> None:
        """stop signal — `start()` 의 `_stop_event.wait()` 해제."""
        self._stop_event.set()
