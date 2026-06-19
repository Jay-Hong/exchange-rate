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

from app import atomic_cutover_runtime, config, topic_dispatcher
from app.atomic_cutover import PublisherGateDisposition
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

# C1 trigger 측 필드 — fx_topic_trigger가 같은 hash(topic:fx:<asset>:stats)에 trigger_ prefix로
# 기록. publish 측 _COUNTER_FIELDS와 'error'/'publish_called'가 동명이라 응답에선 prefix 유지로 분리.
# publisher→fx_topic_trigger import는 cycle(trigger가 이미 publisher의 FX_TOPICS import)이라 여기
# 명시 정의하고, tests의 cross-check로 fx_topic_trigger의 Redis-영속 counter 집합 일치를 강제(drift guard).
_TRIGGER_PREFIX = "trigger_"
# no_loop은 running loop 부재 시 발생 → _fire_telemetry(async, loop 필요)가 실행 불가하여 Redis에
# trigger_no_loop를 못 남김(in-process stats.no_loop_skipped만 증가, fx_topic_trigger.py:281).
# Redis endpoint surface에서 제외 — 노출하면 항상 0이라 "발생 안 함"으로 오해됨(실제는 "측정 불가").
_TRIGGER_PROCESS_LOCAL_FIELDS = frozenset({"no_loop"})
_TRIGGER_COUNTER_FIELDS = (
    "request",
    "skipped_legacy",
    "coalesced",
    "flush_dual_shadow",
    "flush_direct",
    "publish_called",
    "publish_success",
    "publish_skipped_shadow",
    "error",
    "tether_route_shadow",
)
# last_ 필드 — fx_topic_trigger._fire_telemetry가 inline 기록(상수 아님, line 117-130). counter와 달리
# trigger-side 상수가 없어 cross-check drift guard는 부재 — 현재 6개는 writer와 일치 확인(Codex review).
# 완전 guard는 _fire_telemetry가 이 목록을 SSOT로 구동하도록 리팩터 필요(future, 저우선).
_TRIGGER_LAST_FIELDS = (
    "last_result",
    "last_reason",
    "last_source",
    "last_window_ms",
    "last_error",
    "last_at_kst",
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


# ── P1b C6-7 cutover gate SHADOW (dry-run observe, legacy publish 절대 미차단) ──
# **disjoint field family** — _COUNTER_FIELDS / last_result / last_at_kst / last_error 무접촉.
# _record_topic_event는 result가 _COUNTER_FIELDS에 없어도 last_result/last_at_kst를 무조건 overwrite
# (line 145-146)하므로, gate 결과를 그 경로로 쓰면 legacy ordering invariant 손상 → 별도 helper 필수.
_GATE_WOULD_BLOCK_FIELD = "gate_would_block_dry_run"
# C6-9a: get_fx_topic_telemetry가 surface하는 gate shadow field (additive — legacy/trigger 무접촉).
_GATE_COUNTER_FIELDS = (_GATE_WOULD_BLOCK_FIELD,)
_GATE_LAST_FIELDS = ("gate_last_disposition", "gate_last_at_kst")


def _read_fx_publisher_gate_disposition() -> PublisherGateDisposition:
    """cutover gate disposition read — **fail-OPEN**(coordinator/runtime의 fail-closed와 반대 polarity).

    live legacy publish는 dormant gate가 죽여선 안 되므로 어떤 예외에도 PASS_THROUGH 반환(legacy 진행).
    C6-7은 refresh_from_db 미스케줄이라 snapshot()==_INITIAL→PASS_THROUGH 상시(WOULD_BLOCK은 test-injected만).
    """
    try:
        snap = atomic_cutover_runtime.snapshot()  # no-throw last-good
        return (
            PublisherGateDisposition.PASS_THROUGH if snap.publisher_gate_open
            else PublisherGateDisposition.WOULD_BLOCK_DRY_RUN
        )
    except Exception:
        return PublisherGateDisposition.PASS_THROUGH  # fail-OPEN — legacy publish 생존 우선


async def _record_gate_shadow_event(asset: str, disposition: PublisherGateDisposition) -> None:
    """C6-7 dry-run gate shadow telemetry — gate_* disjoint field만(legacy 무접촉, best-effort 격리)."""
    client = redis_cache.client
    if client is None:
        return
    try:
        if not await redis_cache.circuit.can_attempt():
            return
    except Exception:
        return
    key = _telemetry_key(asset)
    try:
        if disposition is PublisherGateDisposition.WOULD_BLOCK_DRY_RUN:
            await client.hincrby(key, _GATE_WOULD_BLOCK_FIELD, 1)
        await client.hset(key, "gate_last_disposition", disposition.value)
        await client.hset(key, "gate_last_at_kst", datetime.now(_KST).isoformat())
    except Exception:
        logger.debug(
            "fx gate shadow telemetry 기록 실패 (격리, dry-run no-op)",
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

    # P1b C6-7 cutover gate SHADOW (dry-run, flag-gated). 여기 = publish 직전 = FF/sub/build 통과 = "would
    # publish" 지점(FLIP의 자연 enforcement point). PASS_THROUGH/WOULD_BLOCK 둘 다 legacy publish 진행 —
    # real-block 없음(PublisherGateDisposition에 real-block 값 자체가 없음). flag-off면 snapshot 미호출.
    # ⚠️ C6-FLIP seam: 여기서 dry-run → real enforcement(WOULD_BLOCK & would-publish → 아래 publish_topic
    #    skip + AtomicFxCoordinator.publish_asset route 또는 HALT)로 **국소 1-spot 전환**. fail-polarity도
    #    inversion 필요(C6-7 fail-OPEN ↔ FLIP fail-closed) — 의식적으로 뒤집을 것.
    #    NOTE: publishability는 publish_topic 내부에서 send 시점에 재평가됨(get_subscribers fresh read) —
    #    gate read(여기)와 실제 send(아래)는 atomic 결합 아님. FLIP enforcement는 would-block 판정 후
    #    자연 0-subscriber send가 뒤따를 수 있음을 허용해야 함.
    if config.FX_CUTOVER_GATE_OBSERVE_ENABLED:
        await _record_gate_shadow_event(asset, _read_fx_publisher_gate_disposition())

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
        for tfield in _TRIGGER_COUNTER_FIELDS:
            base[f"{_TRIGGER_PREFIX}{tfield}"] = 0
        for tfield in _TRIGGER_LAST_FIELDS:
            base[f"{_TRIGGER_PREFIX}{tfield}"] = None
        for gfield in _GATE_COUNTER_FIELDS:  # C6-9a gate shadow surface (additive)
            base[gfield] = 0
        for gfield in _GATE_LAST_FIELDS:
            base[gfield] = None

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

        for tfield in _TRIGGER_COUNTER_FIELDS:
            pkey = f"{_TRIGGER_PREFIX}{tfield}"
            if pkey in decoded:
                try:
                    base[pkey] = int(decoded[pkey])
                except ValueError:
                    base[pkey] = 0
        for tfield in _TRIGGER_LAST_FIELDS:
            pkey = f"{_TRIGGER_PREFIX}{tfield}"
            if pkey in decoded:
                base[pkey] = decoded[pkey]
        for gfield in _GATE_COUNTER_FIELDS:  # C6-9a gate shadow surface (additive)
            if gfield in decoded:
                try:
                    base[gfield] = int(decoded[gfield])
                except ValueError:
                    base[gfield] = 0
        for gfield in _GATE_LAST_FIELDS:
            if gfield in decoded:
                base[gfield] = decoded[gfield]

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
