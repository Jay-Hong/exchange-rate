"""Topic snapshot-on-subscribe — realtime topic release readiness 1st slice.

신규 topic-consuming 앱(legacy WS broadcast 대신 topic 구독)이 subscribe 직후
현재 상태(snapshot)를 즉시 받도록 한다. 없으면 다음 publish(값 변경)까지 빈 화면 —
조용한 시간대/주말엔 무한 대기 가능 = 신규 앱 출시 blocker (PREFLIGHT wf_0f5f9dcf 1순위 gap).

scope: 실제 구현된 topic만 — fx:usd-krw / fx:jpy-krw / fx:eur-krw + usdt:krw
+ krx:usd-krw-futures(ADR-038 D2 독립 topic — KRX_CLIENT_DISTRIBUTION_EFFECTIVE=true일 때만
supported list 포함, G2 off면 snapshot 404 = 발행 중단과 동일 gate).
DXY/news/graph는 publisher 미구현이라 범위 밖(매핑에 없으면 register는 유지하되 snapshot skip,
forward-compatible).

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
from typing import Any, Dict, List, Optional, TYPE_CHECKING

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
        if not config.FX_TOPIC_ENABLED:
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
