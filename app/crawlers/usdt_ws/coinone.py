"""USDT WebSocket — Coinone canary client (Phase B.4 Stage C2 skeleton).

USDT_WS_DESIGN_PLAN §12.6 Phase B.4 Stage C2.
Bithumb Stage U2 패턴 mirror (작은 단위 stage 분할 학습 — KRX close finalizer
1100 lines → 3 High findings → revert + Phase B.3 U2~U7 분할 재시작 학습 적용).

C2 lifecycle skeleton:
    - `_stop_event` + `_running` (network/Redis/DB/Alert import 없음)
    - flag=false 시 scheduler가 CoinoneWsClient 자체를 생성 안 함 (acceptance)
    - flag=true 시 task 등록되지만 `start()`는 `_stop_event.wait()` 대기만

C3 이후 누적 예정 (USDT_WS_DESIGN_PLAN §12.6.4):
    - C3: WS connect/subscribe/parse — 별 protocol parser (`request_type=SUBSCRIBE`,
      `data.last` / `data.timestamp`) + CONNECTED.session_id 캡처 + DEFAULT format
    - C4: 5분 PING + 5s PONG timeout + 2-signal 분리 (PONG=liveness / DATA age=freshness)
    - C5: CoinoneRedisWriter + topic trigger 자동 발화
    - C6: CoinoneDbWriter + REST fallback + `fetch_coinone_usdt_tick()` normalized helper
    - C7: UsdtAlertEvaluator wiring
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.coinone")


class CoinoneWsClient:
    """Coinone USDT/KRW WebSocket client — C2 skeleton only.

    State:
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag

    C3 이후 누적 예정 (network/Redis/DB/Alert state — Bithumb `BithumbWsClient` 패턴).
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def start(self) -> None:
        """C2 skeleton — stop_event 대기만, network 호출 없음.

        Acceptance (Stage C2):
            - flag=true 시 task 등록되지만 본 메서드는 stop_event 대기 외 무동작
            - 중복 start 방지 (_running flag)

        C3 이후 reconnect loop + connect/subscribe/parse 추가 예정.
        """
        if self._running:
            logger.debug("[usdt_ws.coinone] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.coinone] start (C2 skeleton — stop_event 대기)")
        try:
            await self._stop_event.wait()
        finally:
            self._running = False

    async def stop(self) -> None:
        """stop signal — `start()` 의 `_stop_event.wait()` 해제."""
        self._stop_event.set()
