"""USDT WebSocket — Upbit canary client skeleton (PR1).

PR1 scope: lifecycle plumbing only. NO WebSocket connect, NO subscribe,
NO endpoint URL, NO external library import. Skeleton 동작 = stop event 대기.

PR2 (USDT_WS_DESIGN_PLAN §12.2)에서 connect/subscribe/parse 추가 예정.

Lifecycle (scheduler.py에서 호출):
    client = UpbitWsClient()
    task = asyncio.create_task(client.start())
    ...
    await client.stop()
    await task
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.upbit")


class UpbitWsClient:
    """Upbit USDT/KRW WebSocket client skeleton.

    PR1: start()는 stop event 대기 + 로그만. 외부 네트워크 호출 없음.
    PR2~PR7에서 본 클래스에 connect / LivenessMonitor / RedisLatestWriter /
    DB writer / AlertEvaluator / REST fallback 부착 예정.
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def start(self) -> None:
        """skeleton 실행 — stop event 대기.

        PR2 이후: 본 메서드가 connect 루프 + reconnect backoff 담당.
        """
        if self._running:
            logger.debug("[usdt_ws.upbit] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.upbit] skeleton 시작 (PR1, no network)")
        try:
            await self._stop_event.wait()
        finally:
            self._running = False
            logger.info("[usdt_ws.upbit] skeleton 종료")

    async def stop(self) -> None:
        """stop event set — start() 대기 해제."""
        self._stop_event.set()
