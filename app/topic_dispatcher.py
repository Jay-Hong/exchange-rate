"""Topic dispatcher (PR Z-2b Stage 1+2, 2026-05-10).

Phase Z-2b의 backend topic 분배 인프라.

목적:
    USDT/KRX 등 topic-only source 출시 계약(ADR-028) 구현 준비.

단계 분할:
    - Stage 1 (이 모듈, 완료): TopicRegistry + publish_topic helper. 운영 영향 0.
    - Stage 2 (이 모듈 + main.py 통합): handle_client_message — WebSocket 클라이언트
                       메시지 dispatch (ping/pong 보존, subscribe/unsubscribe 분기).
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

import json
import logging
from dataclasses import dataclass
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
        topic 미구독인 ws는 여기 안 잡힘). multi-topic 환경에서 publisher guard로
        쓰면 부정확 — 그땐 subscriber_count(topic) 사용.
        """
        return len(self._subscriptions)

    def subscriber_count(self, topic: str) -> int:
        """주어진 topic을 구독 중인 WebSocket connection 수 (multi-topic publisher guard용).

        publish_tether_tab_snapshot 같은 topic-specific publisher가 builder 비용
        차단에 사용. subscribed_connection_count는 multi-topic 환경에서 부정확:
        예) 단말이 fx:usd-krw만 구독해도 subscribed_connection_count=1이라
        usdt:krw publisher가 "구독자 있다"고 잘못 판단. subscriber_count(TETHER_TOPIC)는
        실제 해당 topic 구독자 0이면 0 반환.
        """
        return len(self.get_subscribers(topic))


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


@dataclass(frozen=True)
class TopicSendCounts:
    """publish_topic_detailed의 rich-outcome 반환 — bare int(sent)가 뭉개는 3-way 0 분리 (C6-4, §5.4).

    Fields:
        attempted: send 시도한 구독자 수 (get_subscribers snapshot 크기). FF-off/구독자 0이면 0.
        sent: per-client send 성공 수 (불변: sent <= attempted).
        enabled: send 시점 ``config.TOPIC_DISPATCHER_ENABLED`` (FF). **TOPIC_DISPATCHER_ENABLED만
            의미** — fx publish의 FX_TOPIC_ENABLED(별개 flag)와 무관. FF-off(enabled=False)와
            구독자-0(enabled=True, attempted=0)을 구분(bare int=0은 둘 다 0이라 못 함).

    bare int=0은 FF-off / no-subscribers / all-failed 셋을 conflate한다. attempted + enabled가
    이를 분리 → C6-6 adapter가 island mapper(atomic_fx_publisher.send_counts_to_send_result)로
    SendDisposition 4-way 분류. 이 dataclass는 dispatcher-native(SendResult/SendDisposition import
    안 함 — live 모듈이 island 의존 갖지 않게). C6-4 dormant: live caller 0.
    """
    attempted: int
    sent: int
    enabled: bool

    def __post_init__(self) -> None:
        for name in ("attempted", "sent"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"TopicSendCounts.{name}: non-negative int — got {v!r}")
        if not isinstance(self.enabled, bool):
            raise ValueError(f"TopicSendCounts.enabled: bool — got {self.enabled!r}")
        if self.sent > self.attempted:
            raise ValueError(
                f"TopicSendCounts: sent>attempted 위반 (sent={self.sent}, attempted={self.attempted})"
            )


async def publish_topic_detailed(topic: str, payload: Dict[str, Any]) -> TopicSendCounts:
    """publish_topic의 additive rich-outcome sibling — TopicSendCounts 반환 (C6-4, dormant).

    publish_topic(line 125)과 **동일 로직**: FF early-return / subscriber snapshot / per-client send
    격리(remove_websocket + 동일 warning + continue). 차이는 bare int 대신 (attempted, sent, enabled)
    반환 → NO_SUBSCRIBERS(attempted==0) vs ALL_FAILED(attempted>0, sent==0)를 분리 가능.

    **behavior-change-0**: publish_topic은 byte-identical 유지 — 이 함수가 delegate target이 아님
    (delegation은 live 본문 rewrite라 C7로 defer; parity test가 publish_topic == detailed().sent +
    동일 eviction을 잠금). publish_topic처럼 top-level raise 없음(per-client isolated) — coordinator의
    SEND_EXCEPTION funnel은 구조적 raise 전용. **dormant**: live caller 0 (C6-6 adapter가 첫 caller).
    """
    if not config.TOPIC_DISPATCHER_ENABLED:
        return TopicSendCounts(attempted=0, sent=0, enabled=False)

    subscribers = registry.get_subscribers(topic)
    if not subscribers:
        return TopicSendCounts(attempted=0, sent=0, enabled=True)

    attempted = len(subscribers)
    sent = 0
    for ws in subscribers:
        try:
            await ws.send_json(payload)
            sent += 1
        except Exception:
            registry.remove_websocket(ws)
            logger.warning(
                "topic publish 실패 (격리)",
                extra={"topic": topic},
                exc_info=True,
            )
    return TopicSendCounts(attempted=attempted, sent=sent, enabled=True)


async def handle_client_message(websocket: "WebSocket", raw_text: str) -> None:
    """Client → server WebSocket 메시지 dispatcher (PR Z-2b Stage 2).

    처리 메시지:
      - "ping" (legacy text) → "pong" JSON 응답
      - {"type": "subscribe", "topics": [str, ...]} (JSON) → topic 구독
      - {"type": "unsubscribe", "topics": [str, ...]} (JSON) → topic 구독 해제

    정책:
      - FF=false면 subscribe/unsubscribe 메시지를 silently 무시 (Stage 1 docstring
        과 일치 — register/unregister는 순수 연산이지만 호출 게이트는 Stage 2가 담당).
      - JSON 파싱 실패 / non-dict / unknown type → 조용히 무시 (legacy/forward-compat).
      - topics가 list[str]이 아니면 debug 로그 후 무시 (잘못된 client 보호).

    main.py에 두지 않은 이유: WebSocket integration 없이 단위 테스트 가능 (firebase_admin
    같은 main.py의 무거운 import-time 의존성 회피). topic 메시지 dispatch는 dispatcher
    의 자연스러운 책임이며, ping/pong은 keep-alive로 같이 처리해 main.py 부담 최소화.
    """
    if raw_text == "ping":
        await websocket.send_json({"type": "pong"})
        return

    try:
        msg = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return  # legacy text / 비-JSON 입력 — 무시

    if not isinstance(msg, dict):
        return

    msg_type = msg.get("type")
    if msg_type not in ("subscribe", "unsubscribe"):
        return  # ping은 위에서 처리, 그 외 unknown type은 forward-compat 무시

    if not config.TOPIC_DISPATCHER_ENABLED:
        # FF=false: Stage 2 wiring 차단. registry 변경 X.
        return

    topics = msg.get("topics")
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        logger.debug(
            "topic message: invalid topics payload",
            extra={"type": msg_type, "topics_type": type(topics).__name__},
        )
        return

    if msg_type == "subscribe":
        registry.register(websocket, topics)
    else:  # unsubscribe
        registry.unregister(websocket, topics)
