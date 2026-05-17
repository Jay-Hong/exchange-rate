"""USDT WebSocket — Bithumb canary client (Phase B.3 Stage U2 skeleton).

USDT_WS_DESIGN_PLAN §12.5 Phase B.3 Stage U2 (2026-05-17).
Upbit Phase B.1 PR1 skeleton 패턴 작은 복제.

U2 (현재): lifecycle skeleton — `_stop_event` 대기만, network 호출 X.
    flag=false 시 BithumbWsClient 생성도 안 됨 (scheduler가 즉시 return,
    USDT_WS_DESIGN_PLAN §12.5.2 U2 acceptance).

후속 stage 예정 (별도 GO):
    U3: WS connect/subscribe/parse + log only (Upbit-compatible, USDT_EXCHANGE_WEBSOCKET_GUIDE §4)
    U4: UsdtLivenessMonitor 재사용 + reconnect
    U5: BithumbRedisWriter (tick-level) + topic trigger 자동 발화
    U6: BithumbDbWriter (1초 window) + BithumbRestFallbackController +
        fetch_bithumb_usdt_tick() normalized REST helper
        ({source, asset, rate, timestamp_ms} shape — Upbit fetch_upbit_usdt_tick()
        app/crawlers/usdt_sources.py:40-73 contract 일치)

운영 활성화 조건 (USDT_WS_DESIGN_PLAN §12.5.3):
    KRX close finalizer 5/18 CF + 5/19 CM 첫 실측 + 7일 telemetry 안정 후
    별도 deploy GO. 그 전까지 USDT_WS_BITHUMB_ENABLED=false default.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.bithumb")


class BithumbWsClient:
    """Bithumb USDT/KRW WebSocket client — U2 skeleton.

    U2 (현재): stop_event 대기만. network/Redis/DB writer 모두 없음.
        - `__init__`: stop_event + running flag만
        - `start`: stop_event 도달까지 대기
        - `stop`: stop_event set (start loop 종료)

    USDT_WS_DESIGN_PLAN §12.5.4 Rollback:
        - 1차: USDT_WS_BITHUMB_ENABLED=false (lifecycle 격리)
        - process 재생성 필수 (docker compose up -d --force-recreate fastapi)
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def start(self) -> None:
        """U2 skeleton — stop_event 대기만, network 호출 없음.

        중복 start 방지 (test/scheduler restart 시 중복 invoke 무시).
        CancelledError는 propagate (asyncio task lifecycle 준수).
        U3에서 reconnect loop + WS connect/subscribe/parse 추가 예정.
        """
        if self._running:
            logger.debug("[usdt_ws.bithumb] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.bithumb] BithumbWsClient skeleton 시작 (U2)")
        try:
            await self._stop_event.wait()
        finally:
            self._running = False
            logger.info("[usdt_ws.bithumb] stop_event 수신, exit")

    async def stop(self) -> None:
        """stop_event set → start loop 종료.

        idempotent — 이미 set이어도 무동작.
        """
        self._stop_event.set()
