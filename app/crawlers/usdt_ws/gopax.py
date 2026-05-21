"""Gopax USDT/KRW WebSocket client — Phase B.6 Stage G1 skeleton.

USDT_WS_DESIGN_PLAN §12.9 (Phase B.6, 2026-05-21).

G1 scope (이 파일의 현재 범위):
    flag + scheduler + lifecycle skeleton만. Subscribe/Parse/Primus heartbeat/
    Liveness/Writers/Telemetry는 G2~G7 + 별도 stage.

G1 acceptance:
    USDT_WS_GOPAX_ENABLED=false 시 함수 즉시 return + GopaxWsClient 생성 X +
    network connect X + Redis/DB writer X. 본 stage 이후 G2-G7 운영 영향 0 보장.

Lifecycle minimal (Codex 최종 권장 — G4 선반영 attribute 제외):
    - state: `_stop_event` / `_running` 만 보유
    - start(): `_running` guard + `await self._stop_event.wait()` + finally `_running=False`
    - stop(): `_stop_event.set()` idempotent

향후 stage scope (본 G1 미포함):
    - G2: SubscribeToTickers + initial array / TickerEvent dict parse + USDT-KRW
      클라이언트 필터링 + normalized tick shape
    - G3: Primus `::ping::` raw text matching + `::pong::` replacement 응답
    - G4: UsdtLivenessMonitor 통합 + reconnect loop + status 차원 결정
      (Codex 권장: 2-signal 시작 — 단 확정은 G4 단계에서)
    - G5: GopaxRedisWriter (Coinone C5 / Bithumb U5 패턴 mirror)
    - G6a: GopaxDbWriter (1초 window debounce)
    - G6b: REST helper + RestFallbackController (`fetch_gopax_usdt_tick()` 신규 작성 필요 —
      현재 `_fetch_gopax()`는 rate-only)
    - G7: Alert evaluator wiring
    - PR 2e (옵션): Telemetry — summary log + saturation + probe counter
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.gopax")

# Gopax WebSocket endpoint — G2부터 사용. G1은 connect 호출 없음.
# 공식 문서: https://gopax.github.io/wsapi/
GOPAX_WS_URL = "wss://wsapi.gopax.co.kr"


class GopaxWsClient:
    """Gopax USDT/KRW WebSocket client — Phase B.6 Stage G1 skeleton.

    G1 minimal lifecycle: flag invariant + start/stop placeholder만.
    Codex 최종 권장 — `_connection_status`/`_ticker_freshness_status`/
    `_reconnect_attempt_count`/`_ws` 등 G2~G4에서 실제 필요한 attribute는
    본 G1 단계에서는 추가하지 않는다 (G4 설계 선반영 회피).
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def start(self) -> None:
        """G1 placeholder lifecycle — flag=true 시에도 network 호출 없음.

        `_running` guard로 중복 방지, `await self._stop_event.wait()`로 stop 신호
        대기. G2부터 reconnect loop + connect/subscribe/parse 추가 예정.

        Acceptance:
            - 중복 start 방지 (`_running` flag)
            - stop_event set 시 즉시 종료
            - production 영향 0 — flag=true에서도 외부 network 호출 0
        """
        if self._running:
            logger.debug("[usdt_ws.gopax] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.gopax] start (G1 placeholder — no network, awaits stop_event)")
        try:
            await self._stop_event.wait()
        finally:
            self._running = False
            logger.info("[usdt_ws.gopax] start exited (G1 placeholder)")

    async def stop(self) -> None:
        """stop signal — start()의 stop_event.wait()을 풀어줌. idempotent."""
        self._stop_event.set()
