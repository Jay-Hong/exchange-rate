"""FX topic publish orchestration (PR Z-2c Step 2).

fx_topic_payload builder와 topic_dispatcher.publish_topic을 잇는 orchestration
계층. tether_topic_publisher.py와 같은 패턴 — Single Responsibility 보존.

설계 (tether_topic_publisher.py 복제 + multi-topic 적응):
    - FX_TOPIC_ASSETS 3개(usd-krw/jpy-krw/eur-krw) per-asset publisher 내부 helper
      `_publish_fx_snapshot(db, asset)` + 일괄 wrapper
      `safe_publish_all_fx_snapshots(db)` 2-layer.
    - main.py broadcast hook은 한 줄: `safe_publish_all_fx_snapshots(db)`.
    - 1개 topic 실패해도 다른 topic publish 계속 (격리).
    - per-topic Redis telemetry key (`topic:fx:<asset>:stats`). 운영자가
      각 topic counter 독립 관찰. usdt:krw `topic:tether:stats`는 legacy 유지.

Guard (builder 호출 비용 차단, per topic):
    1. config.FX_TOPIC_ENABLED=false → 전체 즉시 skip (3개 모두)
    2. config.TOPIC_DISPATCHER_ENABLED=false → 즉시 skip (registry/publish 차단)
    3. registry.subscriber_count(topic) == 0 → 해당 topic만 skip
       (subscribed_connection_count 아님 — multi-topic 환경 정확성 보존)

Telemetry (best-effort, broadcast 영향 X):
    - Redis raw client + circuit.can_attempt() 체크만 (record_failure 호출 X).
      tether와 같은 패턴 — telemetry 실패가 core Redis 경로 오염 차단.
    - per topic counter: hook_called / skipped_disabled / skipped_no_subscribers /
                         built / publish_called / publish_sent_total / publish_zero / error
    - per topic last 상태: last_result / last_at_kst / last_error (str(exc)[:500])

참조:
    - USDT_PHASE1_CLIENT_GUIDE.md "FX topic schema" 섹션
    - app/tether_topic_publisher.py (패턴 원본)
    - DECISIONS.md ADR-028
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from pytz import timezone as pytz_timezone

from app import config, topic_dispatcher
from app.cache import redis_cache
from app.fx_topic_payload import FX_TOPIC_ASSETS, load_and_build_fx_topic_payload

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger("exchange_rate.fx_topic_publisher")
_KST = pytz_timezone("Asia/Seoul")

# asset → topic 이름 매핑 (FX_TOPIC_ASSETS에서 파생, lock-in 1번)
FX_TOPICS: Dict[str, str] = {asset: f"fx:{asset}" for asset in FX_TOPIC_ASSETS}

# last_error 길이 제한 — long traceback 누적 방지 (tether 패턴 복제)
_LAST_ERROR_MAX_LEN: int = 500

# counter 필드 이름 (tether와 동일)
_COUNTER_FIELDS = (
    "hook_called",
    "skipped_disabled",
    "skipped_no_subscribers",
    "built",
    "publish_called",
    "publish_sent_total",
    "publish_zero",
    "error",
)


def _telemetry_key(asset: str) -> str:
    """asset별 Redis hash key. usdt:krw `topic:tether:stats`와 분리."""
    return f"topic:fx:{asset}:stats"


async def _record_topic_event(
    asset: str,
    *,
    result: str,
    increment_hook: bool = False,
    sent: int = 0,
    error: Optional[str] = None,
) -> None:
    """best-effort Redis telemetry 기록 — circuit_breaker 오염 차단.

    tether_topic_publisher._record_topic_event 패턴 복제 + asset 파라미터 추가.

    원칙:
        - redis_cache.client raw 직접 사용 (circuit wrapper 우회)
        - circuit.can_attempt() 체크만 — record_failure() 호출 X
        - 실패 시 logger.debug + 조용히 skip
    """
    client = redis_cache.client
    if client is None:
        return
    try:
        if not await redis_cache.circuit.can_attempt():
            return
    except Exception:
        return

    now_iso = datetime.now(_KST).isoformat()
    key = _telemetry_key(asset)

    try:
        if increment_hook:
            await client.hincrby(key, "hook_called", 1)
        if result in _COUNTER_FIELDS:
            await client.hincrby(key, result, 1)
        if sent > 0:
            await client.hincrby(key, "publish_sent_total", sent)

        await client.hset(key, "last_result", result)
        await client.hset(key, "last_at_kst", now_iso)
        if error is not None:
            await client.hset(key, "last_error", error[:_LAST_ERROR_MAX_LEN])
    except Exception:
        # circuit_breaker.record_failure() 호출 X — telemetry 실패 격리
        logger.debug(
            "fx topic telemetry 기록 실패 (격리, broadcast 영향 X)",
            extra={"asset": asset},
            exc_info=True,
        )


async def _publish_fx_snapshot(db: "Session", asset: str) -> bool:
    """단일 FX asset의 snapshot publish.

    Args:
        db: SQLAlchemy session.
        asset: FX_TOPIC_ASSETS 중 하나.

    Returns:
        True — guard 통과 + 1명 이상 send 성공.
        False — FF=false / subscriber 0 / 모든 send 실패.

    Guard (builder/publish 호출 비용 차단 — telemetry는 분기마다 기록됨):
        1. config.FX_TOPIC_ENABLED=false → skipped_disabled telemetry + False return,
           builder/publish 호출 0
        2. config.TOPIC_DISPATCHER_ENABLED=false → skipped_disabled telemetry + False,
           builder/publish 호출 0 (publish_topic도 어차피 no-op이지만 builder 비용 차단)
        3. registry.subscriber_count(topic) == 0 → skipped_no_subscribers telemetry,
           builder/publish 호출 0

    Note: "skip"은 builder/publish 비용을 말하며, telemetry는 best-effort로 분기마다
    기록됨. safe_publish_all_fx_snapshots가 FF=false 환경에서 3 asset 순회해도
    각 asset당 hook_called/skipped_disabled counter는 +1 (관찰용).
    """
    topic = FX_TOPICS[asset]

    if not config.FX_TOPIC_ENABLED:
        await _record_topic_event(asset, result="skipped_disabled")
        return False
    if not config.TOPIC_DISPATCHER_ENABLED:
        await _record_topic_event(asset, result="skipped_disabled")
        return False

    if topic_dispatcher.registry.subscriber_count(topic) == 0:
        await _record_topic_event(asset, result="skipped_no_subscribers")
        return False

    payload = load_and_build_fx_topic_payload(db, asset)
    # multi-topic 환경에서 단말이 메시지 topic 식별 가능하게 inject
    # (builder는 topic-agnostic 유지 — wrapper 책임).
    payload["topic"] = topic
    await _record_topic_event(asset, result="built")

    sent = await topic_dispatcher.publish_topic(topic, payload)
    if sent > 0:
        await _record_topic_event(asset, result="publish_called", sent=sent)
        # last_result는 'sent'로 갱신 (의미 명확화)
        client = redis_cache.client
        if client is not None:
            try:
                if await redis_cache.circuit.can_attempt():
                    await client.hset(_telemetry_key(asset), "last_result", "sent")
            except Exception:
                logger.debug(
                    "fx last_result 갱신 실패 (격리)",
                    extra={"asset": asset},
                    exc_info=True,
                )
        return True

    # last_result 최종값이 publish_zero가 되도록 순서 정렬 (tether 패턴)
    await _record_topic_event(asset, result="publish_called")
    await _record_topic_event(asset, result="publish_zero")
    return False


async def safe_publish_all_fx_snapshots(db: "Session") -> Dict[str, bool]:
    """3개 FX topic 모두 publish — 1개 실패해도 나머지 계속.

    main.py broadcast hook이 호출하는 단일 entry point. 한 줄로 끝.

    Args:
        db: SQLAlchemy session.

    Returns:
        {asset: bool} dict — 각 asset publish 결과. 예외 시 해당 asset만 False.

    예외 격리:
        per-asset try/except — 1개 topic의 builder/publish 실패가 다른 topic에
        전파되지 않음. broadcast hot path 보호 entrypoint.

    Telemetry:
        진입 시 per asset hook_called +1. 예외 시 per asset error +1 + last_error.
    """
    results: Dict[str, bool] = {}
    for asset in FX_TOPIC_ASSETS:
        await _record_topic_event(asset, result="hook_entered", increment_hook=True)
        try:
            results[asset] = await _publish_fx_snapshot(db, asset)
        except Exception as exc:
            logger.exception(
                "fx topic publish 실패 (격리, hot path 영향 X)",
                extra={"asset": asset},
            )
            await _record_topic_event(
                asset,
                result="error",
                error=f"{type(exc).__name__}: {str(exc)}",
            )
            results[asset] = False
    return results


async def safe_publish_fx_snapshot(asset: str) -> bool:
    """단일 FX asset publish — 자체 DB session 소유 (direct trigger flush 전용).

    §6.6.2 C1: `FxTopicTriggerController` flush가 호출. main.py legacy hook이 쓰는
    `safe_publish_all_fx_snapshots(db)`와 **별개** — direct flush는 caller db가
    없으므로 자체 session을 연다 (tether `_default_publish_tether_snapshot` 패턴).
    코어 `_publish_fx_snapshot`은 공유, **all-wrapper는 불변** (legacy 경로 db
    session 1개 유지 = behavior-change-0).

    Args:
        asset: FX_TOPIC_ASSETS 중 하나. 외 invalid면 False (방어 — controller가
            이미 거르지만 이중).

    Returns:
        True — guard 통과 + 1명 이상 send 성공. False — invalid asset / FF off /
        subscriber 0 / 모든 send 실패 / 예외.

    예외 격리: builder/publish/DB 실패가 flush 호출자(trigger controller)로
    전파되지 않음. all-wrapper와 동일 hook_entered/error telemetry 보존.
    """
    if asset not in FX_TOPIC_ASSETS:
        logger.warning("safe_publish_fx_snapshot invalid asset (격리)", extra={"asset": asset})
        return False

    from app.database import get_db_context

    await _record_topic_event(asset, result="hook_entered", increment_hook=True)
    try:
        with get_db_context() as db:
            return await _publish_fx_snapshot(db, asset)
    except Exception as exc:
        logger.exception(
            "fx topic single publish 실패 (격리, flush 호출자 영향 X)",
            extra={"asset": asset},
        )
        await _record_topic_event(
            asset,
            result="error",
            error=f"{type(exc).__name__}: {str(exc)}",
        )
        return False


async def get_fx_topic_telemetry() -> Dict[str, Dict[str, Any]]:
    """admin endpoint 노출용 telemetry snapshot (3 asset).

    Returns:
        {topic: {...}} — topic 이름(`fx:<asset>`) 키로 per-topic snapshot dict.
        각 snapshot은 tether `get_topic_telemetry` 와 같은 shape.
        Redis 미가용 시 counter는 0/None.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for asset in FX_TOPIC_ASSETS:
        topic = FX_TOPICS[asset]
        base: Dict[str, Any] = {
            "enabled": config.FX_TOPIC_ENABLED and config.TOPIC_DISPATCHER_ENABLED,
            "fx_topic_enabled": config.FX_TOPIC_ENABLED,
            "topic_dispatcher_enabled": config.TOPIC_DISPATCHER_ENABLED,
            "topic": topic,
            "asset": asset,
            "subscriber_count": topic_dispatcher.registry.subscriber_count(topic),
        }
        for field in _COUNTER_FIELDS:
            base[field] = 0
        base["last_result"] = None
        base["last_at_kst"] = None
        base["last_error"] = None

        client = redis_cache.client
        if client is None:
            out[topic] = base
            continue
        try:
            if not await redis_cache.circuit.can_attempt():
                out[topic] = base
                continue
        except Exception:
            out[topic] = base
            continue

        try:
            raw = await client.hgetall(_telemetry_key(asset))
        except Exception:
            logger.debug(
                "fx topic telemetry 조회 실패 (격리)",
                extra={"asset": asset},
                exc_info=True,
            )
            out[topic] = base
            continue

        if not raw:
            out[topic] = base
            continue

        decoded: Dict[str, str] = {}
        for k, v in raw.items():
            key = k.decode("utf-8") if isinstance(k, bytes) else k
            val = v.decode("utf-8") if isinstance(v, bytes) else v
            decoded[key] = val

        for field in _COUNTER_FIELDS:
            if field in decoded:
                try:
                    base[field] = int(decoded[field])
                except ValueError:
                    base[field] = 0
        if "last_result" in decoded:
            base["last_result"] = decoded["last_result"]
        if "last_at_kst" in decoded:
            base["last_at_kst"] = decoded["last_at_kst"]
        if "last_error" in decoded:
            base["last_error"] = decoded["last_error"]

        out[topic] = base

    return out


async def reset_fx_topic_telemetry() -> Dict[str, bool]:
    """3개 FX topic telemetry counter 일괄 reset.

    DEL topic:fx:<asset>:stats — HDEL보다 단순 (필드 추가 자동 적용).

    Returns:
        {asset: bool} — per asset reset 성공 여부. Redis 미가용 시 모두 False.
    """
    out: Dict[str, bool] = {}
    client = redis_cache.client
    if client is None:
        return {asset: False for asset in FX_TOPIC_ASSETS}

    try:
        if not await redis_cache.circuit.can_attempt():
            return {asset: False for asset in FX_TOPIC_ASSETS}
    except Exception:
        return {asset: False for asset in FX_TOPIC_ASSETS}

    for asset in FX_TOPIC_ASSETS:
        try:
            await client.delete(_telemetry_key(asset))
            out[asset] = True
        except Exception:
            logger.debug(
                "fx topic telemetry reset 실패 (격리)",
                extra={"asset": asset},
                exc_info=True,
            )
            out[asset] = False
    return out
