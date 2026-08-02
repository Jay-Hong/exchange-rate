"""Topic snapshot-on-subscribe — realtime topic release readiness 1st slice.

신규 topic-consuming 앱(legacy WS broadcast 대신 topic 구독)이 subscribe 직후
현재 상태(snapshot)를 즉시 받도록 한다. 없으면 다음 publish(값 변경)까지 빈 화면 —
조용한 시간대/주말엔 무한 대기 가능 = 신규 앱 출시 blocker (PREFLIGHT wf_0f5f9dcf 1순위 gap).

scope: 실제 구현된 topic만 — fx:usd-krw / fx:jpy-krw / fx:eur-krw + usdt:krw
+ krx:usd-krw-futures(ADR-038 D2 독립 topic — KRX_CLIENT_DISTRIBUTION_EFFECTIVE=true일 때만
supported list 포함, G2 off면 snapshot 404 = 발행 중단과 동일 gate).
DXY/news/graph는 publisher 미구현이라 범위 밖.
⚠️ 구 서술("매핑에 없으면 register 는 유지하되 snapshot skip")은 **더 이상 맞지 않는다**
(2026-08-02 `4a45173`): 무토큰 경로는 `supported_snapshot_topics()` 안이면서 gated 가 아닌
topic 만 등록하고, 식별된 요청은 미지원 topic 을 `rejected_topics` 로 접는다 — 어느 경로도
**미지원 topic 을 registry 에 넣지 않는다**.

설계 (codex 019efdb3 검토 — 2 blocker 반영):
- SessionLocal 생성+builder+close를 to_thread 내부 sync 함수에서 전부 처리. SQLAlchemy
  Session은 thread-unsafe라 절대 thread 경계를 넘기지 않는다 (codex blocker 1).
- subscribe handler는 WebSocket receive loop라 sync builder 직접 호출 시 모든 connection의
  event loop 반응성을 해침 → asyncio.to_thread로 offload (DB fallback 시 특히).
- send 실패 = connection 실패 → registry 정리 + 남은 snapshot 중단 (publish_topic 패턴,
  codex blocker 2). build 실패 = topic별 격리(다음 topic 계속).
- 빈 payload도 전송(builders는 빈 list/optional omission으로도 schema-valid snapshot 생성 —
  "빈 화면 방지" 목적상 skip 금지).
- enable gate: fx는 FX_TOPIC_ENABLED(publisher와 일관) + TOPIC_DISPATCHER_ENABLED(caller가 이미
  체크). usdt:krw는 TOPIC_DISPATCHER_ENABLED만(별도 TETHER flag 없음). KRX는 독립 topic(ADR-038 D2).
- behavior-change-0: prod subscriber=0(테더 탭 미출시) + TOPIC_DISPATCHER_ENABLED default false
  → live 단말 영향 0. dev/test 구독자만 영향.

⚠️ 알려진 한계 — snapshot↔live-publish race (codex 019efdbc): register(dispatcher) 직후
to_thread build 중 concurrent publish_topic가 이 ws에 newer update를 먼저 보낼 수 있고,
이후 (build 시점 값) snapshot이 도착하면 client가 잠시 구값으로 회귀 가능. register-first는
의도(build 전 publish 누락 방지)라 race는 inherent. **landing엔 무해**(subscriber=0+FF off
dormant)이나 **live subscriber 활성(신규 앱 출시) 전 해소 필요** = client (source,asset)
timestamp-merge 계약(구값으로 신값 덮지 않기, V2 client guide 항목) + 필요 시 server seq(deferred).
"""
import asyncio
import logging
from typing import Any, Dict, List, NamedTuple, Optional, TYPE_CHECKING

from app import config

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger("exchange_rate.topic_initial_snapshot")


def supported_snapshot_topics() -> tuple:
    """snapshot 빌드 가능한 topic 목록 (구현상 지원 set — availability gate와 무관).

    REST bootstrap endpoint(`/api/v2/topics/snapshot`)의 unknown_topic 검증 + client 노출용
    단일 소스. `_build_snapshot_sync`의 dispatch(fx:* + usdt:krw)와 일치. lazy import로
    모듈 경량 유지(FX_TOPICS/TETHER_TOPIC).
    """
    from app.fx_topic_publisher import FX_TOPICS  # {asset: "fx:{asset}"}
    from app.krx_topic_publisher import KRX_TOPIC  # "krx:usd-krw-futures"
    from app.tether_topic_publisher import TETHER_TOPIC  # "usdt:krw"

    topics = tuple(FX_TOPICS.values()) + (TETHER_TOPIC,)
    # ADR-038 Decision 2 — KRX 독립 topic은 G2/G3 열려 있을 때만 지원 목록에 포함
    # (off면 REST 404 unknown_topic + WS snapshot skip — 발행/snapshot 자체 중단 계약).
    if config.KRX_CLIENT_DISTRIBUTION_EFFECTIVE:
        topics += (KRX_TOPIC,)
    return topics


def is_snapshot_topic_enabled(topic: str) -> bool:
    """개별 availability flag 가 지금 켜져 있는가 — `supported_snapshot_topics()` 와 **직교**하다.

    ⛔ 두 축을 섞으면 §8-C 의 `unknown_topic`(미지원)과 `topic_unavailable`(flag off)을 구분할 수
    없다. `supported_snapshot_topics()` 는 docstring 그대로 **구현상 지원 집합**이고 availability
    gate 와 무관하다 — 그래서 그것만 보고 판정하면 **flag off 인 topic 이 accept 된다**
    (실측 재현: `FX_TOPIC_ENABLED=false` 인데 ack·registry 모두 fx 를 활성으로 기록).
    §8-C 가 경고한 "accepted 인데 데이터가 영원히 안 오는 상태"가 정확히 그것이다.

    ⚠️ **이 함수가 그 지식의 단일 소스다.** `_build_snapshot_sync` 도 이것을 쓴다 — 두 곳에
    같은 flag 검사를 두면 갈리는 순간 subscribe 판정과 실제 발사가 어긋난다.

    - fx:* → `FX_TOPIC_ENABLED`
    - usdt:krw → builder 에 flag 검사가 없다(항상 enabled)
    - krx → 배포 flag 가 이미 `supported_snapshot_topics()` 에 반영돼 있어 여기서 중복 판정하지 않는다
    """
    from app.fx_topic_publisher import FX_TOPICS

    if topic in set(FX_TOPICS.values()):
        return config.FX_TOPIC_ENABLED
    return True


def per_user_gated_snapshot_topics() -> frozenset:
    """per-user 판정(entitlement)이 **필요한** topic 집합.

    `visible_snapshot_topics_sync`의 필터와 이 집합은 **같은 지식**이라 반드시 함께 움직여야 한다.
    갈리면 `resolve_snapshot_topic_access_sync`의 shortcut이 새 게이팅 topic을 무조건 허용해
    **판정 우회 + 존재 노출**이 동시에 난다 → 집합대수 trip-wire 테스트가 잠근다
    (`set(supported) - set(visible) ⊆ per_user_gated_snapshot_topics()`).
    """
    from app.krx_topic_publisher import KRX_TOPIC
    return frozenset({KRX_TOPIC})


class SnapshotTopicAccess(NamedTuple):
    """topic 접근 판정 결과.

    `supported_topics`는 **거부일 때만** 채운다 — 허용 응답(200)은 목록을 싣지 않기 때문이다.
    이 결합을 반환 타입에 넣어, 호출부가 200에 목록을 실으려면 여기부터 바꾸게 만든다.
    """
    allowed: bool
    supported_topics: Optional[tuple]


def resolve_snapshot_topic_access_sync(
    topic: str, user_id: str, *, premium_active: bool
) -> SnapshotTopicAccess:
    """topic 접근 판정 — `asyncio.to_thread` 전용 (ADR-039 §8.1 E3).

    **per-user 판정이 결과를 바꿀 수 없는 요청은 entitlement를 조회하지 않는다.**
    이건 최적화가 아니라 **견고성 속성**이다: FX/USDT builder는 Redis-first라 warm이면 DB 커넥션을
    0개 쓰는데, 무조건 entitlement를 조회하면 그 요청이 **DB 필수 요청으로 승격**된다 —
    entitlement DB 순단 하나로 entitlement와 무관한 FX bootstrap이 죽고, 콜드런치 4건(FX 3 동시 +
    tether 1)이 좁은 풀(3+2)을 동시에 문다.

    등가성: `visible ⊆ supported` ∧ `supported \\ visible ⊆ per_user_gated` 이므로
    `topic ∈ supported ∧ topic ∉ per_user_gated ⟹ topic ∈ visible`. 즉 판정 결과가 항상 같고,
    허용 경로는 목록을 싣지 않으므로 §3.2도 그대로다.

    ⛔ **더 나가지 말 것**: "미지원 topic은 어차피 거부니 조회 없이 전역 목록으로 반환"은
    (a) 에코에 KRX가 실려 존재가 노출되고 (b) 비-entitled KRX(조회 1회)와 미지원 topic(0회)이
    지연으로 갈린다 — §3.2 두 축이 동시에 깨진다. `test_unknown_topic_still_consults_entitlement`가
    이 회귀를 red로 만든다.

    ℹ️ `premium_active`는 premium **게이트가 아니다** — premium 강제는 호출부(`require_premium`)
    소관이고 이 값은 `compute_krx_visible`에 그대로 전달될 뿐이다. shortcut이 이 값을 보지 않는 건
    비-KRX topic의 판정이 premium과 무관하기 때문이다(어느 값이든 결과 동일).
    """
    global_topics = supported_snapshot_topics()
    if topic in global_topics and topic not in per_user_gated_snapshot_topics():
        return SnapshotTopicAccess(True, None)

    visible = visible_snapshot_topics_sync(user_id, premium_active=premium_active)
    if topic in visible:
        return SnapshotTopicAccess(True, None)
    return SnapshotTopicAccess(False, visible)


def visible_snapshot_topics_sync(user_id: str, *, premium_active: bool) -> tuple:
    """**per-user** 노출 topic 목록 = 전역 지원 집합 ∧ KRX 가시성 — `asyncio.to_thread` 전용
    (ADR-039 §8.1 E3).

    `supported_snapshot_topics()`는 전역 게이트(G2∧G3)까지만 좁힌다. REST bootstrap은 그 위에
    per-user G1∧premium을 한 번 더 적용해야 §3.1 매트릭스("KRX WS·REST snapshot = Firebase +
    premium + KRX entitlement")를 만족한다.

    §3.2(KRX 존재 완전 비노출): entitlement 없는 사용자에게 KRX는 **미지원 topic과 구분 불가**여야
    하므로 unknown_topic 판정과 `supported_topics` 에코가 **이 함수 하나**를 공유한다 — 둘이 갈리면
    404 코드는 맞는데 목록으로 존재가 새는 회귀가 난다.

    **세션 수명이 계약이다** (`_build_snapshot_sync`와 동일 규율):
    SessionLocal 생성 → 조회 → close를 이 함수(=worker thread) 안에서 **완결**한다.
    - 호출자(핸들러)가 request-scoped 세션을 들고 있으면, 그 커넥션을 쥔 채 뒤이어 builder가
      **두 번째** SessionLocal을 연다. 운영 풀은 `pool_size=3 + max_overflow=2` = 5뿐이라
      bootstrap 동시 요청 3건이면 서로의 builder 커넥션을 기다리다 pool timeout에 걸린다.
    - 동기 SELECT를 이벤트 루프에서 직접 돌리면 RDS 순단 시 **worker 전체**(WS pong 포함)가 멈춘다.
    - SQLAlchemy Session은 thread-unsafe라 thread 경계를 넘기지 않는다.

    전역 게이트가 닫혀 있으면 세션을 **아예 열지 않는다** — 결과가 같은데 장애 표면만 늘기 때문.
    반대로 열려 있으면 반드시 조회한다(fail-closed: 조회 실패는 예외로 전파 → 5xx, 조용한 False 아님).
    """
    from app import entitlements
    from app.krx_topic_publisher import KRX_TOPIC

    topics = supported_snapshot_topics()
    if KRX_TOPIC not in topics:
        return topics

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        visible = entitlements.compute_krx_visible(
            db, user_id, premium_active=premium_active)
    finally:
        db.close()
    if visible:
        return topics
    return tuple(t for t in topics if t != KRX_TOPIC)


def _build_snapshot_sync(topic: str) -> Optional[Dict[str, Any]]:
    """주어진 topic의 현재 snapshot payload 빌드 — asyncio.to_thread 내부 sync 실행 전용.

    SessionLocal 생성 / builder 호출 / close를 모두 이 함수(=worker thread) 안에서 처리한다.
    SQLAlchemy Session은 thread-unsafe라 절대 thread 경계를 넘기지 않는다 (codex 019efdb3 blocker 1).

    Returns:
        payload dict (publisher와 동일하게 ``topic`` 필드 inject) — 빈 데이터도 schema-valid면 반환.
        None — 미지원 topic(매핑 없음) 또는 enable-gate off.
    """
    # 토픽 이름 single source: publisher 상수 재사용(하드코딩 "fx:"/"usdt:krw" 회피). lazy import로
    # dispatcher import 그래프 경량 유지(첫 subscribe 시에만 builder 체인 로드).
    from app.fx_topic_publisher import FX_TOPICS  # {asset: "fx:{asset}"}
    from app.tether_topic_publisher import TETHER_TOPIC  # "usdt:krw"

    fx_asset_by_topic = {channel: asset for asset, channel in FX_TOPICS.items()}

    if topic in fx_asset_by_topic:
        # fx snapshot은 FX_TOPIC_ENABLED 존중(publisher _publish_fx_snapshot과 일관 — off면 미발사).
        # ⚠️ 판정은 `is_snapshot_topic_enabled` 가 소유한다 — subscribe 분류와 같은 소스여야 한다.
        if not is_snapshot_topic_enabled(topic):
            return None
        asset = fx_asset_by_topic[topic]
        from app.database import SessionLocal
        from app.fx_topic_payload import (
            FX_TOPIC_BANK_ORDER,
            load_and_build_fx_topic_payload,
        )

        db = SessionLocal()
        try:
            # public fx:* snapshot도 Citi 제외 8-bank (publish 경로와 동일). atomic/legacy는 기본 9.
            payload = load_and_build_fx_topic_payload(db, asset, bank_order=FX_TOPIC_BANK_ORDER)
        finally:
            db.close()
        payload["topic"] = topic
        return payload

    if topic == TETHER_TOPIC:
        from app.database import SessionLocal
        from app.usdt_topic_payload import load_and_build_tether_tab_payload

        db = SessionLocal()
        try:
            # ADR-038 Decision 2 — usd_krw_futures group 제거: KRX는 독립 topic 전용
            payload = load_and_build_tether_tab_payload(db)
        finally:
            db.close()
        payload["topic"] = topic
        return payload

    from app.krx_topic_publisher import KRX_TOPIC, build_krx_topic_payload, load_krx_topic_entry
    if topic == KRX_TOPIC:
        # ADR-038 — G2/G3 off면 미지원 취급 (supported 목록과 일관)
        if not config.KRX_CLIENT_DISTRIBUTION_EFFECTIVE:
            return None
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            entry = load_krx_topic_entry(db)   # Redis-first + DB fallback
        finally:
            db.close()
        if entry is None:
            return None   # 데이터 없음 — snapshot skip (구독 register는 유지)
        return build_krx_topic_payload(entry)

    return None  # 미지원 topic(dxy/news/graph 등) — snapshot skip, register는 호출자가 유지


async def send_initial_snapshots(websocket: "WebSocket", topics: List[str]) -> int:
    """subscribe 직후 요청 topic들의 현재 snapshot을 이 ws에 즉시 전송.

    handle_client_message subscribe 분기에서 registry.register 직후 호출 — 이 시점은
    TOPIC_DISPATCHER_ENABLED를 이미 통과한 상태.

    격리 정책 (codex 019efdb3):
    - build 실패(to_thread 예외) → 해당 topic만 skip, 다음 topic 계속.
    - send 실패 = connection 실패 → registry에서 ws 제거 + 남은 snapshot 중단(반환).
      (publish_topic의 stale-entry 정리 패턴과 일관.)
    - 요청 내 중복 topic은 de-dupe(같은 subscribe에서 같은 topic 1회만). 재구독(별 요청)은
      resync로 유용하므로 막지 않음.

    Returns:
        성공적으로 send 완료한 snapshot 수.
    """
    sent = 0
    seen: set = set()
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)

        try:
            payload = await asyncio.to_thread(_build_snapshot_sync, topic)
        except Exception:
            logger.warning(
                "initial snapshot build 실패 (격리)",
                extra={"topic": topic},
                exc_info=True,
            )
            continue

        if payload is None:
            continue  # 미지원 topic / enable-gate off

        try:
            await websocket.send_json(payload)
            sent += 1
        except Exception:
            # send 실패 = connection 실패 → registry 정리 후 남은 snapshot 중단.
            from app.topic_dispatcher import registry

            registry.remove_websocket(websocket)
            logger.warning(
                "initial snapshot send 실패 (connection 정리, 남은 snapshot 중단)",
                extra={"topic": topic},
                exc_info=True,
            )
            return sent

    return sent
