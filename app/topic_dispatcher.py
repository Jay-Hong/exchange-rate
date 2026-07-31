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

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterable, Set

if TYPE_CHECKING:
    from fastapi import WebSocket

from app import config

logger = logging.getLogger("exchange_rate.topic_dispatcher")

# 설정 결함(판정기 부재) 응답의 retry 동반값. §8-C 는 "동반"만 요구하고 정책은 별도 슬라이스다.
_CONFIG_FAULT_RETRY_AFTER_SECONDS = config.WS_AUTH_RETRY_AFTER_SECONDS


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


async def handle_client_message(
    websocket: "WebSocket",
    raw_text: str,
    *,
    authorize_subscribe,
) -> None:
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

    ## `authorize_subscribe` — 인증 seam (ADR-040 첫 수직 슬라이스)

    `authorize_subscribe(id_token) -> uid` (async). **기본값이 없다.** 이 모듈은 firebase를
    import할 수 없으므로(위 문단) 검증자는 진입점이 주입한다.

    ⛔ **기본값을 주지 말 것.** `authorize_subscribe=None`으로 두면 호출자가 빠뜨린 순간
    인증이 **조용히 사라진다**(fail-open). 필수 keyword-only면 그 실수가 `TypeError`가 된다.

    ⚠️ **이 seam이 확인하는 것은 identity뿐이다.** entitlement(유료 판정)는 확인하지 않는다 —
    그래서 아래에서 **per-user 판정이 필요한 topic은 accept하지 않는다**. identity만 확인하고
    그 topic을 받으면 *인가된 것처럼 보이는* 유료 데이터 유출이 되어 무인증보다 나쁘다.
    entitlement 판정은 다음 red 테스트의 주제다.

    ⛔ **REST의 premium 게이트를 여기에 재사용하지 말 것.** 그것은 외부 장애 시 stale 캐시를
    ACTIVE로 돌려주는 **가용성 우선** 정책이라, lease 권한 판정에 쓰면 오래된 판정이 방금 확인한
    것으로 승격된다(폐기된 트랙의 핵심 결함).

    ⚠️ **실패 경로는 아직 없다.** `authorize_subscribe`가 raise하면 예외가 그대로 올라가
    상위 루프가 연결을 정리한다(접근 0 = fail-closed). `subscription_error` 프레임 매핑은
    다음 red 테스트에서 만든다 — 지금 만들면 소비자 없는 코드가 된다.
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
        # lazy import로 dispatcher import 그래프 경량 유지(builder 체인은 첫 subscribe 시 로드).
        from app.topic_initial_snapshot import (
            is_snapshot_topic_enabled,
            per_user_gated_snapshot_topics,
            send_initial_snapshots,
            supported_snapshot_topics,
        )

        from app.topic_wire import SubscribeAuthFailed, build_subscription_error

        id_token = msg.get("id_token")
        if id_token is not None and (not isinstance(id_token, str) or not id_token):
            # ⛔ 형식 위반은 **인증 이전 단계**다(§8-C `invalid_request`). 여기서 걸러 두면
            #    SDK 가 토큰과 무관한 bare `ValueError` 를 던지는 경로가 아예 사라진다 —
            #    그 예외는 자격 오류도 판정 불가도 아니라 어느 코드에도 맞지 않는다.
            await websocket.send_json(
                build_subscription_error(
                    request_id=msg.get("request_id"), error="invalid_request"
                )
            )
            return
        if id_token is None:
            # ⚠️ **무토큰 = 기존 동작 그대로**(등록 + snapshot, ack 없음). 강제 전환을 여기서
            #    하면 안 된다 — enforcement는 capability와 분리돼야 하고(§E1), 현행 클라가
            #    무토큰이라 무조건 요구하면 구 클라가 topic을 잃는다. 그 전환은 legacy 유예와
            #    같은 시점에 묶인 별도 결정이다.
            registry.register(websocket, topics)
            await send_initial_snapshots(websocket, topics)
            return

        # 토큰이 실린 요청만 인증 경로를 탄다.
        try:
            # ⛔ **deadline 은 여기(dispatcher)에 있어야 한다.** 검증자 안에 두면 E2E harness 가
            #    검증자를 통째로 fake 하므로 `/ws` 경계에서 영영 관측되지 않는다.
            #    ⚠️ 이것이 막는 것은 **호출자 대기**뿐이다 — `to_thread` 작업은 취소되지 않으므로
            #    실행 중 스레드는 계속 돈다(직접 재현). 그 잔여는 SDK transport 상한(인증 전용
            #    named app 의 `httpTimeout`)이 맡는다. 두 축이 함께 있어야 의미가 있다.
            # ⛔ `wait_for` 대신 `asyncio.timeout` 을 쓰는 이유: `wait_for` 는 **실제 deadline
            #    초과**와 **검증자 안에서 올라온 `TimeoutError`** 를 같은 예외로 준다. 후자를
            #    "wire deadline 초과"로 기록하면 D 를 튜닝할 telemetry 가 오염된다 —
            #    ⚠️ 그리고 이건 이론이 아니다: 3.10+ 에서 `socket.timeout is TimeoutError` 라
            #    SDK 내부 소켓 timeout 이 그대로 이 타입으로 도착할 수 있다.
            async with asyncio.timeout(config.WS_AUTH_WIRE_DEADLINE_SECONDS) as deadline_cm:
                await authorize_subscribe(id_token)
        except asyncio.TimeoutError:
            if not deadline_cm.expired():
                # 검증자가 스스로 던진 TimeoutError — 분류되지 않은 예외다. 우리 계약은
                # **분류 불가를 삼키지 않는다**(재전파 → 상위가 traceback 을 남기고 연결 정리).
                raise
            # ⚠️ WARNING 인 이유: 이건 **일시 장애**다(재시도로 나을 수 있다). ERROR 로 올리면
            #    "재시도 간격이 길어야 한다"는 결합 규칙과 어긋나고, 고빈도 생산자라 신호가 희석된다.
            #    다만 로그가 **아예 없으면** D 를 튜닝할 근거가 영영 안 생긴다.
            logger.warning(
                "WS subscribe 인증이 wire deadline 을 넘었다",
                extra={"deadline_seconds": config.WS_AUTH_WIRE_DEADLINE_SECONDS},
            )
            await websocket.send_json(
                build_subscription_error(
                    request_id=msg.get("request_id"),
                    error="temporarily_unavailable",
                    retry_after_seconds=config.WS_AUTH_RETRY_AFTER_SECONDS,
                )
            )
            return
        except SubscribeAuthFailed as failure:
            # ⚠️ **연결과 registry 는 불변이다.** §8-C 의 두 코드는 전체-요청 범위이므로 요청만
            #    접는다 — 구 동작은 예외가 상위로 올라가 연결이 닫히고 그 연결의 **다른 구독까지**
            #    사라졌다. 그 차이는 소켓에서만 관측된다.
            await websocket.send_json(
                build_subscription_error(
                    request_id=msg.get("request_id"),
                    error=failure.error,
                    retry_after_seconds=failure.retry_after_seconds,
                )
            )
            return
        # ⚠️ uid를 **저장하지 않는다** — lease 바인딩이 없는 지금 저장하면 소비자 없는 상태가
        #    되고, 그것이 폐기된 트랙의 형태였다. uid는 lease 슬라이스에서 쓴다.

        # ⛔ **"게이트가 아니면 허용"은 틀렸다.** 구 판정은 미지원 topic(`not:a:topic`)까지
        #    accept하고 registry에 등록했다(실측 재현). 지원 집합을 **명시적으로** 봐야 한다.
        supported = set(supported_snapshot_topics())   # flag-aware (KRX는 배포 flag 조건부)
        gated = per_user_gated_snapshot_topics()       # per-user 판정이 필요한 topic

        # ⛔ **판정 불가는 per-topic이 아니라 전체-요청이다**(§8-C). per-user 판정이 필요한 topic이
        #    지원 집합에 **들어 있는데**(배포 flag on) 이 슬라이스엔 판정기가 없다 — 그건 transient가
        #    아니라 **설정 결함**이므로 운영자 신호(ERROR)를 내고 요청을 접는다. 한때 이 경우에도
        #    `topic_unavailable`을 돌려줬는데 **틀렸다**(codex): §8-C에서 그 코드는 "topic 자체가
        #    서버에서 비활성(개별 flag off)"을 뜻하고, flag가 켜진 상태에 그걸 쓰면 운영자가 flag를
        #    보고 코드와 모순을 겪는다.
        unjudgeable = [t for t in topics if t in gated and t in supported]
        if unjudgeable:
            logger.error(
                "per-user 판정이 필요한 topic이 지원 집합에 있으나 WS 판정기가 없다 — 설정 결함",
                extra={"topics": unjudgeable},
            )
            # ⚠️ 설정 결함 응답도 **같은 생성 경로**를 지난다 — 오류 프레임이 두 형태로
            #    갈리지 않게 하는 것이 `build_subscription_error` 의 존재 이유다.
            await websocket.send_json(
                build_subscription_error(
                    request_id=msg.get("request_id"),
                    error="temporarily_unavailable",
                    retry_after_seconds=_CONFIG_FAULT_RETRY_AFTER_SECONDS,
                )
            )
            return

        accepted_names, accepted, rejected = [], [], []
        for topic in topics:                          # 요청 순서 보존
            if topic in gated or not is_snapshot_topic_enabled(topic):
                # 배포/개별 flag가 off라 지금 발사되지 않는 topic — §8-C `topic_unavailable`.
                # (gated인데 여기 온 것은 supported 밖 = 배포 flag off인 경우뿐이다. 위 guard 참조.)
                rejected.append({"topic": topic, "error": "topic_unavailable"})
            elif topic not in supported:
                rejected.append({"topic": topic, "error": "unknown_topic"})
            else:
                accepted_names.append(topic)
                accepted.append({"topic": topic})

        if accepted_names:
            registry.register(websocket, accepted_names)

        # ⚠️ **ack이 데이터보다 먼저다.** snapshot을 먼저 보내면 클라가 ack 전에 데이터를 받아
        #    "요청이 수락됐는지" 모르는 상태로 처리하게 된다. registry 등록은 ack 앞이어야
        #    `active_subscriptions`가 연결의 **최종 상태**를 담을 수 있다(§8-B).
        await websocket.send_json({
            "type": "subscription_ack",
            "request_id": msg.get("request_id"),
            "operation": "subscribe",
            "accepted_topics": accepted,
            "rejected_topics": rejected,
            # 제거 전이(§C2 eviction)는 이 슬라이스에 없다 — 항상 빈 목록이다(§8-B-stage).
            "removed_topics": [],
            "active_subscriptions": [
                {"topic": t} for t in sorted(registry.get_subscriptions(websocket))
            ],
        })
        if accepted_names:
            await send_initial_snapshots(websocket, accepted_names)
    else:  # unsubscribe
        registry.unregister(websocket, topics)
