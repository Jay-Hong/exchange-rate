"""Topic dispatcher 골격 (PR Z-2b Stage 1, 2026-05-10).

Phase Z-2b의 backend topic 분배 인프라 — **골격만**.

목적:
    USDT/KRX 등 topic-only source 출시 계약(ADR-028) 구현 준비.
    Stage 1은 module + config flag만 — main.py 미연결.

단계 분할:
    - Stage 1 (이 모듈): TopicRegistry + publish_topic helper. 운영 영향 0.
    - Stage 2 (별도 PR): main.py WebSocket handler에 subscribe 메시지 분기.
                       config.TOPIC_DISPATCHER_ENABLED=true시 register 가능.
    - Stage 3 (5/19+): publish_topic을 source data hook (crud.insert_source_rate
                       등)에 연결.

설계 원칙:
    - registry는 in-memory (per-process). cluster scale 단계에선 Redis pub/sub
      또는 분산 message broker로 진화 (Open Question §6.3 균형).
    - WebSocket 객체를 직접 키로 사용 — 클라이언트별 unique 인스턴스라 충분.
    - publish_topic은 send 실패를 격리 (한 클라이언트 disconnect가 다른
      구독자에 영향 X).
    - FF=false면 `publish_topic`은 즉시 0 반환(no-op). `register`/`unregister`는
      순수 registry 연산이며, Stage 2에서 main.py WebSocket handler가 flag를 보고
      호출 여부를 결정한다 (Stage 1엔 호출자 없음).

참고:
    - REALTIME_ARCHITECTURE_PLAN.md §5 (topic 채널 분리)
    - USDT_TOPIC_MIGRATION_PLAN.md Z-2b
    - DECISIONS.md ADR-028 (Topic-only Tether/KRX + dual-emit FX)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Iterable, Set

if TYPE_CHECKING:
    from fastapi import WebSocket

from app import config

logger = logging.getLogger("exchange_rate.topic_dispatcher")


class TopicRegistry:
    """WebSocket ↔ topic 구독 관계 in-memory registry.

    Stage 1: Stage 2 main.py 통합 전까지 register 호출 경로 없음.
    publish_topic이 호출되어도 subscribers 0으로 효과적 no-op.

    thread-safety: FastAPI/Starlette WebSocket lifecycle은 단일 event loop
    위에서 직렬화. 별도 lock 미필요 (asyncio.Lock 도입은 cross-task race
    감지 시점에 검토).
    """

    def __init__(self) -> None:
        self._subscriptions: Dict["WebSocket", Set[str]] = {}

    def register(self, websocket: "WebSocket", topics: Iterable[str]) -> Set[str]:
        """websocket을 주어진 topic들에 구독자로 등록.

        Returns:
            업데이트 후 해당 websocket의 구독 topic 전체 (idempotent — 기존 +
            신규 합집합).
        """
        existing = self._subscriptions.setdefault(websocket, set())
        existing.update(topics)
        return set(existing)

    def unregister(self, websocket: "WebSocket", topics: Iterable[str]) -> Set[str]:
        """주어진 topic들에서 websocket 구독 해제.

        Returns:
            업데이트 후 잔여 구독 topic. 비어 있으면 entry 제거.
        """
        existing = self._subscriptions.get(websocket)
        if existing is None:
            return set()
        existing.difference_update(topics)
        if not existing:
            self._subscriptions.pop(websocket, None)
            return set()
        return set(existing)

    def remove_websocket(self, websocket: "WebSocket") -> None:
        """websocket 연결 종료 시 모든 구독 정리. main.py disconnect hook에서 호출 예정."""
        self._subscriptions.pop(websocket, None)

    def get_subscribers(self, topic: str) -> Set["WebSocket"]:
        """주어진 topic을 구독 중인 websocket 집합 반환 (snapshot)."""
        return {ws for ws, topics in self._subscriptions.items() if topic in topics}

    def get_subscriptions(self, websocket: "WebSocket") -> Set[str]:
        """websocket의 현재 구독 topic 집합 (snapshot)."""
        return set(self._subscriptions.get(websocket, set()))

    @property
    def subscribed_connection_count(self) -> int:
        """현재 1개 이상 topic을 구독 중인 WebSocket connection 수 (admin/관찰용).

        주의: (topic, ws) pair 수가 아니라 unique connection 수.
        ConnectionManager.active_connections와는 의미 다름 (connect는 됐지만
        topic 미구독인 ws는 여기 안 잡힘).
        """
        return len(self._subscriptions)


# 싱글톤 — main.py / publish 호출자가 공유.
# Stage 1에선 호출자 없음 (test/wire-up 준비용).
registry = TopicRegistry()


async def publish_topic(topic: str, payload: Dict[str, Any]) -> int:
    """주어진 topic 구독자 전체에게 payload send.

    Args:
        topic: e.g. "usdt:krw".
        payload: JSON-직렬화 가능 dict.

    Returns:
        성공적으로 send 완료한 클라이언트 수. FF=false 또는 구독자 없으면 0.

    실패 격리:
        한 클라이언트의 send 실패는 logger.warning + registry에서 즉시 제거 →
        다음 publish 때 stale entry 재실패 방지. ConnectionManager.broadcast()와
        같은 패턴 (main.py). Stage 2 disconnect hook과 중복돼도 idempotent.
    """
    if not config.TOPIC_DISPATCHER_ENABLED:
        return 0

    subscribers = registry.get_subscribers(topic)
    if not subscribers:
        return 0

    sent = 0
    for ws in subscribers:
        try:
            await ws.send_json(payload)
            sent += 1
        except Exception:
            # disconnected/closed/timeout 등 — stale entry 방지를 위해 즉시 정리.
            registry.remove_websocket(ws)
            logger.warning(
                "topic publish 실패 (격리)",
                extra={"topic": topic},
                exc_info=True,
            )
    return sent
