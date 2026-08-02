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
import uuid
from dataclasses import dataclass

from app.clock import system_clock as lease_clock
from app.topic_lease import (
    compute_gated_lease_expiry,
    compute_identity_only_lease_expiry,
    is_expired as is_lease_expired,
)
from app.topic_authorization import (
    Denied as GatedDenied,
    Granted as GatedGranted,
    Unavailable as GatedUnavailable,
    UnavailableKind,
    authorize_gated_subscription,
)


@dataclass(frozen=True)
class TopicLease:
    """(연결, topic) 에 묶인 구독 lease. **monotonic 축**이다(wall 과 섞지 말 것)."""

    lease_id: str
    uid: str
    expires_at_mono: float

    def remaining_seconds(self, now_mono: float) -> int:
        """**지금 기준 남은 시간**. 음수는 0 으로 접는다.

        ⛔ 발급 당시 duration 을 저장해 매 ack 에 되풀이하면 안 된다 — 900초 lease 를 받고
        600초 뒤 다른 topic 을 unsubscribe 하면 실제 잔여는 300초인데 ack 이 다시 900 을
        말한다. 클라가 그걸로 timer 를 재설정하면 **서버 만료보다 늦게 재인증**해 데이터가
        조용히 끊긴다. 그래서 저장하는 진실은 `expires_at_mono` **하나**다.

        ⚠️ `0` 은 "이미 만료 — 지금 재인증하라"는 뜻이다(필드를 빼면 무토큰 구독과
        구분되지 않아 클라가 *무제한*으로 오해한다).
        """
        return max(0, int(self.expires_at_mono - now_mono))

from app.topic_wire import (
    SubscribeAuthFailed,
    SubscribeIdentityConflict,
    build_subscription_ack,
    build_subscription_error,
)
import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING, Any, Dict, Iterable, Set

if TYPE_CHECKING:
    from fastapi import WebSocket

from app import config

logger = logging.getLogger("exchange_rate.topic_dispatcher")

# 설정 결함(판정기 부재) 응답의 retry 동반값. §8-C 는 "동반"만 요구하고 정책은 별도 슬라이스다.


class TopicRegistry:
    """WebSocket ↔ topic 구독 관계 in-memory registry.

    Stage 1: Stage 2 main.py 통합 전까지 register 호출 경로 없음.
    publish_topic이 호출되어도 subscribers 0으로 효과적 no-op.

    thread-safety: FastAPI/Starlette WebSocket lifecycle은 단일 event loop
    위에서 직렬화. 별도 lock 미필요 (asyncio.Lock 도입은 cross-task race
    감지 시점에 검토).
    """

    def __init__(self) -> None:
        # topic → lease. **`None` = 무토큰(§E1) 구독** — lease 가 없는 것이 정상이고,
        # 그런 구독에는 publish 가 그대로 도달한다(인증된 적이 없어 철회할 자격도 없다).
        self._subscriptions: Dict["WebSocket", Dict[str, Optional[TopicLease]]] = {}

    def register(
        self,
        websocket: "WebSocket",
        topics: Iterable[str],
        leases: Optional[Dict[str, "TopicLease"]] = None,
    ) -> Set[str]:
        """websocket을 주어진 topic들에 구독자로 등록.

        Returns:
            업데이트 후 해당 websocket의 구독 topic 전체 (idempotent — 기존 +
            신규 합집합).
        """
        existing = self._subscriptions.setdefault(websocket, {})
        for topic in topics:
            lease = (leases or {}).get(topic)
            # ⛔ 제공되지 않은 lease 로 **기존 lease 를 지우지 않는다.** 지우면 인증된 구독이
            #    무토큰 재구독 하나로 legacy(무제한)가 되어 **fail-open** 이다.
            if lease is not None or topic not in existing:
                existing[topic] = lease
        return set(existing)

    def unregister(self, websocket: "WebSocket", topics: Iterable[str]) -> Set[str]:
        """주어진 topic들에서 websocket 구독 해제.

        Returns:
            업데이트 후 잔여 구독 topic. 비어 있으면 entry 제거.
        """
        existing = self._subscriptions.get(websocket)
        if existing is None:
            return set()
        for topic in topics:
            existing.pop(topic, None)
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

    def get_lease(self, websocket: "WebSocket", topic: str) -> Optional["TopicLease"]:
        """(연결, topic) 의 lease. 무토큰(§E1) 구독이면 `None`."""
        return self._subscriptions.get(websocket, {}).get(topic)

    def get_subscriptions(self, websocket: "WebSocket") -> Set[str]:
        """websocket의 현재 구독 topic 집합 (snapshot)."""
        return set(self._subscriptions.get(websocket, {}))

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


def _lease_wire_map(websocket: "WebSocket", topics: Iterable[str]) -> Dict[str, tuple]:
    """ack 에 실을 `{topic: (lease_id, 남은 초)}`. lease 없는(무토큰) topic 은 빠진다."""
    now_mono = lease_clock().mono()
    out: Dict[str, tuple] = {}
    for topic in topics:
        lease = registry.get_lease(websocket, topic)
        if lease is not None:
            out[topic] = (lease.lease_id, lease.remaining_seconds(now_mono))
    return out


def leased_subscribers(topic: str) -> Set["WebSocket"]:
    """lease 가 살아 있는 구독자만. ⛔ **모든 발행 경로가 공유하는 단일 게이트**다.

    발급 시점 검사만으로는 S5 의 15분 revoke 상한이 보증이 되지 않는다 — 그 사이 자격이
    죽어도 계속 나간다. 그래서 **전송 직전**에 다시 본다.

    ⛔ 발행 함수가 `registry.get_subscribers()` 를 직접 부르면 그 경로만 게이트를 우회한다 —
    실제로 `publish_topic_detailed` 가 그랬고, 그건 live caller 를 가진 함수다.
    ⚠️ `now` 는 **1회**만 읽는다. 구독자마다 읽으면 같은 배치 안에서 판정이 갈린다.
    ⚠️ 만료된 구독을 registry 에서 **지우지는 않는다**(정리는 disconnect / 재구독 축).
    """
    now_mono = lease_clock().mono()
    return {
        ws
        for ws in registry.get_subscribers(topic)
        if (lease := registry.get_lease(ws, topic)) is None
        or not is_lease_expired(now_mono=now_mono, expires_at_mono=lease.expires_at_mono)
    }


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

    subscribers = leased_subscribers(topic)
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
        attempted: **lease 유효성 검사를 통과해 실제 전송 대상으로 선택된 수**
            (raw 구독자 수가 **아니다** — 2026-08-01 변경). FF-off / 구독자 0 / **전원 만료**면 0.
            ⛔ 마지막 경우가 `NO_SUBSCRIBERS`와 구분되지 않는 한계는 `publish_topic_detailed`
            docstring 참조.
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

    publish_topic(line 125)과 **동일 로직**: FF early-return / **lease 게이트** / per-client send
    격리(remove_websocket + 동일 warning + continue). 차이는 bare int 대신 (attempted, sent, enabled)
    반환 → NO_SUBSCRIBERS(attempted==0) vs ALL_FAILED(attempted>0, sent==0)를 분리 가능.

    ⚠️ **`attempted` 는 lease 게이트 *이후* 수다** — "전송 자격이 있어 실제로 시도한 대상"이지
    raw 구독자 수가 아니다(2026-08-01 변경). 따라서 **등록자는 있는데 전부 만료**면
    `attempted == 0` 이라 downstream 이 `NO_SUBSCRIBERS` 로 분류한다.
    ⛔ 그 둘은 운영상 다른 사건이다("아무도 안 본다" vs "다들 재인증을 못 하고 있다").
    lease 가 실제로 발화하기 시작하면 **만료-skip 수를 별도로 세는** 것이 맞다 — 지금은 소비자가
    없어 필드를 늘리지 않고 이 한계를 기록만 한다.

    **behavior-change-0**: publish_topic은 byte-identical 유지 — 이 함수가 delegate target이 아님
    (delegation은 live 본문 rewrite라 C7로 defer; parity test가 publish_topic == detailed().sent +
    동일 eviction을 잠금). publish_topic처럼 top-level raise 없음(per-client isolated) — coordinator의
    SEND_EXCEPTION funnel은 구조적 raise 전용. **dormant**: live caller 0 (C6-6 adapter가 첫 caller).
    """
    if not config.TOPIC_DISPATCHER_ENABLED:
        return TopicSendCounts(attempted=0, sent=0, enabled=False)

    # ⛔ **`registry.get_subscribers` 를 직접 부르지 않는다.** 한때 이 함수만 그렇게 해서
    #    lease 게이트를 통째로 우회했다. 발행 경로는 전부 `leased_subscribers()` 를 지난다.
    #    ⚠️ 한때 이 주석이 "live caller 를 가진다"고 적었는데 **과장이었다**(codex):
    #       `atomic_fx_live` 는 호출 코드는 있으나 **live 진입점이 없는 dormant 모듈**이다.
    #       위험은 "지금 새고 있다"가 아니라 **"활성화되는 순간 샌다"** 이다.
    subscribers = leased_subscribers(topic)
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
    identity,
) -> None:
    """Client → server WebSocket 메시지 dispatcher (PR Z-2b Stage 2).

    처리 메시지:
      - "ping" (legacy text) → "pong" JSON 응답
      - {"type": "subscribe", "topics": [str, ...]} (JSON) → topic 구독
      - {"type": "unsubscribe", "topics": [str, ...]} (JSON) → topic 구독 해제

    정책 — **식별된 요청은 반드시 종결된다** (§8-B-term):

    "식별된" = `request_id` 키를 실었거나 `id_token` 값을 실은 요청. 그런 요청에는
    **종결 프레임 하나 또는 연결 종료**가 보장된다(⛔ "항상 프레임 하나"는 거짓 —
    분류 불가 인증 예외 / 16KB 초과 close 1009 / 반쯤 닫힌 소켓은 0 프레임이다).
      - FF=false → 전 topic `topics_disabled` 인 **ack**(§8-C 에서 per-topic 코드다).
        registry 는 불변. ⚠️ 이 ack 은 인증 **이전**이라 ack 수신 ≠ 인증 통과.
      - topics 형식 위반(비-list / 비-str 원소 / **빈 목록**) → `invalid_request`.
      - 미지 `type` → `invalid_request`.
      - 인증 실패 → §8-C 코드로 매핑된 `subscription_error`(연결·registry 불변).

    미식별 요청(§E1 구 클라: `request_id` 도 `id_token` 도 없음)은 **동작이 불변**이다 —
    등록/해제는 되고 프레임은 0개. JSON 파싱 실패 / non-dict / 미식별 미지 type 도
    조용히 무시한다(legacy 평문이 흐르는 경계라 오류를 쏘면 스팸이 된다).

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

    ⚠️ **실패 경로 매핑은 구현돼 있다**(§8-C). `SubscribeAuthFailed` 는 요청만 접고
    연결·registry 는 **불변**이다 — 구 동작은 예외가 상위로 올라가 연결이 닫히고 그 연결의
    **다른 구독까지** 사라졌다. 다만 **분류 불가** 예외는 여전히 재전파한다: 우리 버그·미지의
    상태를 wire 오류로 접으면 클라가 영구 재시도하고 운영자는 신호를 못 받는다.
    그 경로가 위 "또는 연결 종료"의 실체다.
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

    # ── 이 요청은 **ack 을 받는 요청인가** (§8-A) ────────────────────────────────
    #    §8-A: 상태를 바꾸는 메시지는 `request_id` 를 갖고 ack 을 받는다. 그 계약을 지키는
    #    클라만 "식별된" 요청이고, **식별된 요청은 반드시 종결 프레임을 받는다**(아래 불변식).
    #
    #    ⛔ **truthiness 로 판별하지 말 것.** `request_id: ""` 를 legacy 로 분류하면 조용히
    #       처리되고 클라의 in-flight 슬롯이 영영 안 풀린다 — 그게 이 검사가 없애려는 정지다.
    #       그래서 **키 존재**로 본다. 값 검증은 그 다음이다.
    #    ⛔ subscribe 를 `id_token` **값** 존재만으로 판별하면 `{"request_id":…, "id_token":null}`
    #       이 미식별로 새어 blind register + 프레임 0개가 된다(적대적 검토가 찾은 구멍).
    #       두 축의 **합집합**이라야 그 shape 가 종결된다.
    #    ⚠️ 구 클라(§E1)는 둘 다 보내지 않으므로 미식별로 남는다 — 동작 불변이 그 보호다.
    #    ⛔ `id_token` 을 **타입별로 읽지 말 것.** 한때 `msg_type == "subscribe"` 일 때만 읽었고,
    #       그래서 `{"type":"subscrbe","id_token":"tok"}` 이 미식별로 새어 0 프레임이었다
    #       (실측 재현) — U1 이 문서에만 있고 코드에는 **반만** 있었다. `id_token` 존재는
    #       **신 프로토콜 클라 신호**이므로 타입과 무관하게 식별 축이다. 오타 하나로 같은
    #       shape 가 갈리면 안 된다.
    id_token = msg.get("id_token")
    identified = ("request_id" in msg) or (id_token is not None)

    request_id = None
    if identified:
        raw_request_id = msg.get("request_id")
        if not isinstance(raw_request_id, str) or not raw_request_id:
            # id 를 모르거나 쓸 수 없는 요청이므로 **null 로 응답**한다(§8-B-stage).
            # ⚠️ unsubscribe 도 여기서 접힌다 = **fail-closed**. §8-A 의 "축소는 fail-open" 은
            #    *토큰* 축이고, echo 할 id 가 없으면 ack 자체를 만들 수 없다 — ack 없는
            #    unregister 가 바로 이 검사가 삭제하는 침묵이다.
            await websocket.send_json(
                build_subscription_error(request_id=None, error="invalid_request")
            )
            return
        request_id = raw_request_id

    if msg_type not in ("subscribe", "unsubscribe"):
        # ⛔ **식별된 요청은 미지 타입이어도 종결한다.** 한때 여기서 무조건 return 했고
        #    근거를 "forward-compat"이라 적었는데, request/response 에서 그건 **역방향으로
        #    해롭다**: 서버보다 새 클라가 모르는 타입을 보내면 "미지원"을 배우는 대신
        #    **매단다**. 오타 하나(`subscrbe`)로도 같은 일이 난다.
        # ⚠️ 미식별(=id 를 안 실은) 미지 타입은 여전히 침묵이다 — `/ws` 는 legacy 평문·잡음이
        #    흐르는 경계이고 그쪽엔 기다리는 요청자가 없다.
        if identified:
            await websocket.send_json(
                build_subscription_error(request_id=request_id, error="invalid_request")
            )
        return

    if msg_type == "subscribe" and identified and (
        not isinstance(id_token, str) or not id_token
    ):
        # ⛔ 형식 위반은 **인증 이전 단계**다(§8-C `invalid_request`). 여기서 걸러 두면
        #    SDK 가 토큰과 무관한 bare `ValueError` 를 던지는 경로가 아예 사라진다 —
        #    그 예외는 자격 오류도 판정 불가도 아니라 어느 코드에도 맞지 않는다.
        #    ⚠️ `id_token` 부재(=request_id 로만 식별된 subscribe)도 여기 걸린다: §8-A 의
        #       subscribe 는 토큰을 동반한다.
        await websocket.send_json(
            build_subscription_error(request_id=request_id, error="invalid_request")
        )
        return

    # ── topics 형식 — **flag 앞**이어야 flag-off ack 의 rejected 목록을 만들 수 있다 ──────
    topics = msg.get("topics")
    if (
        not isinstance(topics, list)
        or not topics
        or not all(isinstance(t, str) for t in topics)
    ):
        # ⛔ 빈 리스트도 거부한다(§8-C "빈 topics"). `all()` 은 빈 시퀀스에서 True 라
        #    현행 검사를 **통과했고**, 그러면 성공과 구분되지 않는 빈 ack 이 나간다.
        logger.debug(
            "topic message: invalid topics payload",
            extra={
                "type": msg_type,
                "topics_type": type(topics).__name__,
                "topics_len": len(topics) if isinstance(topics, list) else None,
            },
        )
        if identified:
            await websocket.send_json(
                build_subscription_error(request_id=request_id, error="invalid_request")
            )
        return

    if not config.TOPIC_DISPATCHER_ENABLED:
        # FF=false: Stage 2 wiring 차단. **registry 변경 X.**
        # ⚠️ 응답은 전체-요청 오류가 아니라 **전부 rejected 된 ack** 이다 — §8-C 에서
        #    `topics_disabled` 는 per-topic 범위이고, 전체-요청 코드 4개에 그건 없다.
        # ⚠️ 이 ack 은 **인증 이전**에 나간다 → *ack 수신은 인증 통과를 뜻하지 않는다*.
        #    (REST twin 도 같은 순서다: flag 404 가 토큰 검증보다 앞이다.)
        # ⚠️ registry 를 안 바꾸는 선택이 unsubscribe 에서도 관측되지 않는 이유: flag 는
        #    import 시점 상수라 한 연결의 수명 동안 불변이고, 등록은 이 검사 **뒤**에서만
        #    일어난다 → flag-off 프로세스의 연결은 애초에 뺄 구독이 없다.
        if identified:
            await websocket.send_json(
                build_subscription_ack(
                    request_id=request_id,
                    operation=msg_type,
                    accepted=[],
                    rejected=[(t, "topics_disabled") for t in topics],
                    active=registry.get_subscriptions(websocket),
                )
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

        if id_token is None:
            # ⚠️ **무토큰 = 기존 동작 그대로**(등록 + snapshot, ack 없음). 강제 전환을 여기서
            #    하면 안 된다 — enforcement는 capability와 분리돼야 하고(§E1), 현행 클라가
            #    무토큰이라 무조건 요구하면 구 클라가 topic을 잃는다. 그 전환은 legacy 유예와
            #    같은 시점에 묶인 별도 결정이다.
            #
            # ⛔ **다만 topic 은 분류한다.** 요청한 것을 그대로 등록하면 per-user 판정이 필요한
            #    topic(KRX)이 **lease 없이** 등록되고, publish 는 lease 부재를 "무제한"으로
            #    취급하므로 **무인증 유료 데이터 우회**가 된다(실측 재현).
            #    §E1 이 보존하는 것은 *무료 topic 의 기존 동작*이지 "아무 topic 이나 무인증
            #    허용"이 아니다 — enforcement 분리는 **무료 범위 안에서**의 이야기다.
            free_topics = [
                topic
                for topic in topics
                if topic in set(supported_snapshot_topics())
                and topic not in per_user_gated_snapshot_topics()
            ]
            if free_topics:
                registry.register(websocket, free_topics)
                await send_initial_snapshots(websocket, free_topics)
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
            # ⛔ identity 관측 시각은 **호출 전**을 쓴다. 호출이 끝난 시각을 쓰면 인증 I/O
            #    시간만큼 15분 경계가 **늘어난다**(fail-open). 검증 결과가 반영하는 상태는
            #    아무리 늦어도 호출 시작 시점이므로, 그쪽이 보수적이다.
            identity_observed_at_mono = lease_clock().mono()
            # ⛔ **인증 단계 전체에 하나의 절대 deadline** 이다. 단계마다 새 timeout 을
            #    시작하면 상한이 단계 수만큼 곱해진다 — config 는 이 값을 **"identity + gated
            #    인가의 누적 상한"** 으로 정의하는데, identity 9초 + gated 9초가 각각 통과하면
            #    18초가 되어 그 정의를 어긴다.
            #
            # ⚠️ **범위는 "인증 단계 누적"까지다** — registry 변경과 ack 송신은 이 창 **밖**이라
            #    "10초 안에 ack"을 보장하지 않는다. 그 보장을 원하면 창을 넓혀야 하고, 그건
            #    별도 결정이다(codex Medium).
            # ⛔ 한때 이 주석이 iOS `topicCommandTimeout` 을 근거로 들었는데 **그 상수는
            #    존재하지 않는다**(실측 0건) — **폐기한 reconciler 계획**의 값을 배포된 코드처럼
            #    인용했다. 클라의 명령 응답 timeout 은 아직 **미구현**이고, 구현될 때 이 상한을
            #    전제로 잡아야 한다.
            request_deadline_at = (
                asyncio.get_running_loop().time() + config.WS_AUTH_WIRE_DEADLINE_SECONDS
            )
            async with asyncio.timeout_at(request_deadline_at) as deadline_cm:
                uid = await authorize_subscribe(id_token)
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
                    request_id=request_id,
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
                    request_id=request_id,
                    error=failure.error,
                    retry_after_seconds=failure.retry_after_seconds,
                )
            )
            return
        # ⛔ **UID 결속은 여기다** — identity 성공 직후, premium 판정 **전**. Denied/Unavailable
        #    요청도 같은 소켓 소유권을 유지해야 한다.
        try:
            identity.bind(uid)
        except SubscribeIdentityConflict:
            # ⚠️ close 를 시도하되 **실패해도 예외를 올린다** — 올리지 않으면 endpoint loop 가
            #    닫힌 소켓을 다시 읽어 `RuntimeError` 를 내고, 정상적인 정책 종료가 매번
            #    **ERROR traceback** 을 남긴다(실측).
            # ⛔ `subscription_error` 프레임은 보내지 않는다 — cross-UID 의 결과는 프레임이
            #    아니라 연결 종료다(§8-C 어휘에 해당 코드가 없다).
            try:
                await websocket.close(code=1008)
            except Exception:
                logger.debug("cross-UID close 실패 — 예외는 그대로 올린다", exc_info=True)
            raise

        # ⛔ **"게이트가 아니면 허용"은 틀렸다.** 구 판정은 미지원 topic(`not:a:topic`)까지
        #    accept하고 registry에 등록했다(실측 재현). 지원 집합을 **명시적으로** 봐야 한다.
        supported = set(supported_snapshot_topics())   # flag-aware (KRX는 배포 flag 조건부)
        gated = per_user_gated_snapshot_topics()       # per-user 판정이 필요한 topic

        # ── ① 분류 — registry 는 **아직 건드리지 않는다** ────────────────────────
        gated_requested = [t for t in topics if t in gated and t in supported]
        free_accepted, rejected = [], []
        for topic in topics:                          # 요청 순서 보존
            if topic in gated and topic in supported:
                continue                              # verdict 가 결정한다(②)
            if topic in gated or not is_snapshot_topic_enabled(topic):
                # 배포/개별 flag가 off라 지금 발사되지 않는 topic — §8-C `topic_unavailable`.
                # (gated인데 여기 온 것은 supported 밖 = 배포 flag off인 경우뿐이다.)
                rejected.append((topic, "topic_unavailable"))
            elif topic not in supported:
                rejected.append((topic, "unknown_topic"))
            else:
                free_accepted.append(topic)

        # ── ② gated 인가 판정 ────────────────────────────────────────────────
        verdict = None
        # ⚠️ deadline verdict 는 **판정기를 지나지 않는다** → 그 경로의 transient WARNING 을
        #    아무도 남기지 않는다. 이 flag 가 아래 한 줄의 레벨을 정한다(문자열 keying 회피).
        deadline_hit = False
        if gated_requested:
            try:
                # ⛔ 인가 구간에도 **상한**이 필요하다. RC 는 자체 timeout 이 있지만 entitlement
                #    `to_thread` 는 없고, 풀 획득 기본 대기(30s)가 wire deadline(10s)보다 길다 —
                #    상한이 없으면 §8-B-term "식별된 요청은 반드시 종결된다"가 깨진다.
                # ⚠️ **같은 절대 deadline** 을 공유한다 — identity 가 이미 쓴 시간만큼 예산이
                #    줄어든 상태로 시작한다.
                async with asyncio.timeout_at(request_deadline_at) as gate_cm:
                    verdict = await authorize_gated_subscription(
                        uid, mono=lambda: lease_clock().mono()
                    )
            except asyncio.TimeoutError:
                if not gate_cm.expired():
                    raise                              # 판정기 내부 TimeoutError — 분류 불가
                deadline_hit = True
                verdict = GatedUnavailable(
                    UnavailableKind.TRANSIENT, "authorization_deadline"
                )

            if isinstance(verdict, GatedUnavailable):
                # ⛔ **registry 를 하나도 바꾸지 않고** 전체 요청을 접는다(§8-C whole-request).
                #    무료 topic 조차 **새로 등록하지 않는다** — 판정 불가는 per-topic 이 아니다.
                persistent = verdict.kind is UnavailableKind.PERSISTENT
                # ⚠️ **원인 로깅은 판정기 소유**(레벨 + traceback). 여기서는 **wire 결과**만
                #    남긴다 — 둘 다 레벨을 정하면 영구 장애 하나에 ERROR 가 두 건이 되어
                #    운영 신호가 희석된다.
                # ⚠️ 기본은 **wire 결과만**(INFO) — 원인 로그는 판정기/leaf 소유다.
                #    다만 deadline 은 그쪽을 지나지 않으므로 여기가 유일한 신호다 → WARNING.
                (logger.warning if deadline_hit else logger.info)(
                    "gated topic 인가 판정 불가 — 전체 요청을 접는다",
                    extra={"reason": verdict.reason, "kind": verdict.kind.value,
                           "topics": gated_requested},
                )
                await websocket.send_json(
                    build_subscription_error(
                        request_id=request_id,
                        error="temporarily_unavailable",
                        retry_after_seconds=(
                            config.WS_AUTH_PERSISTENT_FAULT_RETRY_AFTER_SECONDS
                            if persistent
                            else config.WS_AUTH_RETRY_AFTER_SECONDS
                        ),
                    )
                )
                return

        # ── ③ registry 변경 — 여기서만, 그리고 한 번에 ──────────────────────────
        # ⛔ **`now` 는 1회만 읽는다.** topic 마다 읽으면 같은 요청 안에서 만료가 갈린다.
        now_mono = lease_clock().mono()
        leases: Dict[str, TopicLease] = {}

        if isinstance(verdict, GatedDenied):
            # ⛔ **즉시 철회.** 방금 authoritative 한 부정 관측을 얻었다 — 안 지우면 기존 lease 가
            #    만료될 때까지 계속 발행되고, ack 이 `rejected_topics` 와 `active_subscriptions`
            #    로 **서로 모순된 두 문장**을 말한다.
            registry.unregister(websocket, gated_requested)
            rejected.extend((topic, verdict.error) for topic in gated_requested)
        elif isinstance(verdict, GatedGranted):
            # ⚠️ **4축** — identity·premium·entitlement 는 서로 다른 권위다.
            gated_expiry = compute_gated_lease_expiry(
                now_mono=now_mono,
                identity_verified_at_mono=identity_observed_at_mono,
                premium_verified_at_mono=verdict.premium_observed_at_mono,
                entitlement_verified_at_mono=verdict.entitlement_observed_at_mono,
            )
            leases.update({
                topic: TopicLease(lease_id=uuid.uuid4().hex, uid=uid,
                                  expires_at_mono=gated_expiry)
                for topic in gated_requested
            })

        if free_accepted:
            # ⚠️ **identity 축 하나** — 여기 오는 topic 은 per-user 판정 대상이 아니다.
            free_expiry = compute_identity_only_lease_expiry(
                now_mono=now_mono,
                identity_verified_at_mono=identity_observed_at_mono,
            )
            leases.update({
                topic: TopicLease(lease_id=uuid.uuid4().hex, uid=uid,
                                  expires_at_mono=free_expiry)
                for topic in free_accepted
            })

        accepted_set = set(leases)
        accepted_names = [t for t in topics if t in accepted_set]   # 요청 순서 보존
        if accepted_names:
            registry.register(websocket, accepted_names, leases=leases)

        # ── ④ 모든 변경이 끝난 뒤 **정확히 한 번** snapshot ──────────────────────
        # ⚠️ **ack이 데이터보다 먼저다.** snapshot을 먼저 보내면 클라가 ack 전에 데이터를 받아
        #    "요청이 수락됐는지" 모르는 상태로 처리하게 된다.
        # ⛔ 이 지점이 ③ **뒤**여야 한다 — mixed [free, KRX] 에서 Denied 를 처리하고 무료 등록까지
        #    마친 상태를 담아야 `rejected_topics` 와 `active_subscriptions` 가 모순되지 않는다.
        active = registry.get_subscriptions(websocket)
        await websocket.send_json(
            build_subscription_ack(
                request_id=request_id,
                operation="subscribe",
                accepted=accepted_names,
                rejected=rejected,
                active=active,
                leases=_lease_wire_map(websocket, active),
            )
        )
        if accepted_names:
            await send_initial_snapshots(websocket, accepted_names)
    else:  # unsubscribe
        # ⚠️ `unregister` 는 제거분이 아니라 **잔여**를 돌려준다 — 그게 곧 ack 의
        #    `active_subscriptions`(연결 최종 상태)다.
        remaining = registry.unregister(websocket, topics)
        if identified:
            # ⚠️ `accepted` = 요청된 topic **전부**(idempotent). 구독한 적 없는 topic 도
            #    accepted 다 — 요청의 목표 상태("구독하지 않음")가 달성됐고, §8-C 의 닫힌
            #    per-topic 어휘에 "미구독" 코드가 없다.
            # ⛔ 지원 집합(`supported_snapshot_topics`)을 **조회하지 않는다**: 그 집합은
            #    flag-aware 라, KRX 배포 flag 가 꺼진 뒤 `unknown_topic` 으로 거부하면
            #    이미 들고 있는 구독을 영영 뺄 수 없게 된다. 축소 연산에 flag-aware 검증을
            #    붙이면 이득 없이 실패 모드만 는다.
            await websocket.send_json(
                build_subscription_ack(
                    request_id=request_id,
                    operation="unsubscribe",
                    accepted=list(topics),
                    rejected=[],
                    active=remaining,
                    # ⛔ **남아 있는 구독의 lease 를 잃지 않는다.** `active_subscriptions` 는
                    #    연결의 최종 상태이고 클라는 그걸로 재인증 timer 를 잡는다 — 여기서
                    #    lease 가 빠지면 A·B 구독 후 A 만 해제했을 때 B 의 만료를 잃는다.
                    # ⚠️ 제거된 topic 이 `accepted_topics` 에 lease 없이 실리는 것과는 별개다
                    #    (제거된 구독엔 lease 가 없다 — U4).
                    leases=_lease_wire_map(websocket, remaining),
                )
            )
