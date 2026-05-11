"""테더 탭 topic publish orchestration (PR Z-2b Stage 3 Level 1+2 + Telemetry, 2026-05-10).

builder/helper와 publish_topic을 잇는 orchestration 계층 — Single Responsibility.

위치 분리 이유:
    - app/usdt_topic_payload.py: builder + DB helper (정규화/정렬/dispatch)
    - app/tether_topic_publisher.py: orchestration (config FF + subscriber guard +
      builder 호출 + publish_topic dispatch). topic_dispatcher/config/registry를
      모두 인지하는 계층은 builder와 분리.

Level 1 (완료, 9215eaf):
    - publish_tether_tab_snapshot wrapper — guard로 FF=false / subscriber 0 시
      builder/DB 호출 0 (hot path 비용 보호). include_krx=False 고정.

Level 2 (완료, 39c6592): broadcast cycle 임시 hook 연결.
    - broadcast_rates_once `is_changed` 분기 안에서 safe_publish_tether_tab_snapshot
      호출. async/main loop 안이라 sync/async 경계 위험 회피.
    - safe_publish_tether_tab_snapshot이 예외 격리 — broadcast 정상 흐름 보호.
    - TOPIC_DISPATCHER_ENABLED=false default라 wrapper 진입 즉시 return →
      publish/builder 효과 0 (호출 경로만 활성, μs guard return).
    - **임시 위치**: USDT WebSocket/Redis-first 전환 후 mirror/topic pipeline으로
      이동 예정. 그때까지 broadcast cycle hook으로 동작.

Telemetry (Stage 3 추가, 2026-05-10):
    - Redis-backed counter (topic:tether:stats hash). 재배포 시 reset 안 됨 →
      배포/관찰 자유도 ↑.
    - best-effort 기록 — redis_cache.client raw 사용 + circuit.can_attempt() 체크만,
      record_failure() 호출 X. telemetry 실패가 broadcast/latest mirror 같은 core
      Redis 경로에 영향 미치지 않게 격리.
    - counter: hook_called / skipped_disabled / skipped_no_subscribers / built /
      publish_called / publish_sent_total / publish_zero / error
    - last 상태: last_result / last_at_kst / last_error (str(exc)[:500])

Level 3 / activation (5/19+):
    - include_krx 정책 결정 (KRX_TOPIC_INCLUDE 신규 / KRX_BROADCAST_INCLUDE 재사용 /
      데이터 존재 게이트) 후 KRX 포함 wrapper 또는 매개변수 추가
    - TOPIC_DISPATCHER_ENABLED=true 활성화 + 클라이언트 release 동기화
    - dev/test client subscribe + 짧은 FF=true 시험으로 운영 검증

참조:
    - USDT_TOPIC_MIGRATION_PLAN.md §4 Z-2b
    - DECISIONS.md ADR-028 (Topic-only Tether/KRX)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from pytz import timezone as pytz_timezone

from app import config, topic_dispatcher
from app.cache import redis_cache
from app.usdt_topic_payload import load_and_build_tether_tab_payload

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger("exchange_rate.tether_topic_publisher")
_KST = pytz_timezone("Asia/Seoul")

# Topic 이름 상수 — 활성화 직전까지 자유 변경 (한 곳에서 끝).
TETHER_TOPIC: str = "usdt:krw"

# Redis-backed telemetry key (재배포 시 reset 안 됨)
_TELEMETRY_KEY: str = "topic:tether:stats"

# last_error 길이 제한 — long traceback 누적 방지 (Codex 권고)
_LAST_ERROR_MAX_LEN: int = 500

# counter 필드 이름
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


async def _record_topic_event(
    *,
    result: str,
    increment_hook: bool = False,
    sent: int = 0,
    error: Optional[str] = None,
) -> None:
    """best-effort Redis telemetry 기록 — circuit_breaker 오염 차단.

    원칙 (Codex 권고):
        - redis_cache.client raw 직접 사용 (circuit wrapper 우회)
        - circuit.can_attempt() 체크만 — record_failure() 호출 X
        - 실패 시 logger.debug + 조용히 skip
        - telemetry 실패가 broadcast/latest mirror 같은 core 경로에 영향 X

    Args:
        result: last_result에 기록할 분류 (skipped_disabled / skipped_no_subscribers /
                sent / publish_zero / error / built 등)
        increment_hook: True면 hook_called +1 (safe_publish_tether_tab_snapshot 진입 시)
        sent: publish_topic이 반환한 sent count (publish_sent_total += sent)
        error: 예외 문자열 (str(exc)[:500] 제한 권장 — 호출자 책임)
    """
    client = redis_cache.client
    if client is None:
        return  # Redis 연결 X — best-effort skip
    try:
        if not await redis_cache.circuit.can_attempt():
            return  # circuit open — best-effort skip
    except Exception:
        return  # circuit 체크 자체 실패도 격리

    now_iso = datetime.now(_KST).isoformat()

    try:
        # counter increment
        if increment_hook:
            await client.hincrby(_TELEMETRY_KEY, "hook_called", 1)
        if result in _COUNTER_FIELDS:
            await client.hincrby(_TELEMETRY_KEY, result, 1)
        if sent > 0:
            await client.hincrby(_TELEMETRY_KEY, "publish_sent_total", sent)

        # last 상태
        await client.hset(_TELEMETRY_KEY, "last_result", result)
        await client.hset(_TELEMETRY_KEY, "last_at_kst", now_iso)
        if error is not None:
            await client.hset(_TELEMETRY_KEY, "last_error", error[:_LAST_ERROR_MAX_LEN])
    except Exception:
        # circuit_breaker.record_failure() 호출 X — telemetry 실패 격리
        logger.debug("topic telemetry 기록 실패 (격리, broadcast 영향 X)", exc_info=True)


async def publish_tether_tab_snapshot(
    db: "Session",
    *,
    include_krx: bool = False,
) -> bool:
    """테더 탭 snapshot을 TETHER_TOPIC 구독자에게 publish.

    Guard (builder 호출 비용 차단):
        1. config.TOPIC_DISPATCHER_ENABLED=false → 즉시 False
        2. registry.subscribed_connection_count == 0 → 즉시 False
        둘 다 통과 시에만 load_and_build_tether_tab_payload 호출.

    Args:
        include_krx: KRX 미국달러선물 포함 여부 (호출자 결정, env flag 미해석).
            main.py broadcast hook은 config.KRX_TOPIC_INCLUDE를 전달.
            기본 False — wrapper 자체는 보수 default 유지.

    Telemetry: 각 분기에서 _record_topic_event 호출 (best-effort, broadcast 영향 X).

    Returns:
        True  — guard 통과 + 1명 이상 send 성공.
        False — FF=false / subscriber 0 / 모든 send 실패.
    """
    if not config.TOPIC_DISPATCHER_ENABLED:
        await _record_topic_event(result="skipped_disabled")
        return False
    if topic_dispatcher.registry.subscribed_connection_count == 0:
        await _record_topic_event(result="skipped_no_subscribers")
        return False

    payload = load_and_build_tether_tab_payload(db, include_krx=include_krx)
    await _record_topic_event(result="built")

    sent = await topic_dispatcher.publish_topic(TETHER_TOPIC, payload)
    if sent > 0:
        await _record_topic_event(result="publish_called", sent=sent)
        # last_result는 publish_called를 'sent'로 분류해 의미 명확화
        # built / publish_called 카운터는 이미 증가, last_result만 갱신
        client = redis_cache.client
        if client is not None:
            try:
                if await redis_cache.circuit.can_attempt():
                    await client.hset(_TELEMETRY_KEY, "last_result", "sent")
            except Exception:
                logger.debug("last_result 갱신 실패 (격리)", exc_info=True)
        return True

    # Codex 권고: last_result 최종값이 "publish_zero"가 되도록 순서 정렬
    # (counter는 둘 다 +1, 마지막 last_result만 publish_zero)
    await _record_topic_event(result="publish_called")
    await _record_topic_event(result="publish_zero")
    return False


async def safe_publish_tether_tab_snapshot(
    db: "Session",
    *,
    include_krx: bool = False,
) -> bool:
    """예외 격리 wrapper — hot path 호출자가 try/except 안 써도 안전.

    publish_tether_tab_snapshot의 어떤 단계 예외도 False 반환 + logger.exception.
    broadcast/mirror 같은 hot path의 정상 흐름 보호 entrypoint.

    Args:
        include_krx: KRX 포함 여부 (publish_tether_tab_snapshot에 그대로 전달).

    Telemetry:
        진입 시 hook_called +1. 예외 시 error counter +1 + last_error 기록.

    Returns:
        publish_tether_tab_snapshot 결과 (True/False), 예외 시 False.
    """
    await _record_topic_event(result="hook_entered", increment_hook=True)

    try:
        return await publish_tether_tab_snapshot(db, include_krx=include_krx)
    except Exception as exc:
        logger.exception("테더 topic publish 실패 (격리, hot path 영향 X)")
        await _record_topic_event(
            result="error",
            error=f"{type(exc).__name__}: {str(exc)}",
        )
        return False


async def get_topic_telemetry() -> Dict[str, Any]:
    """admin endpoint 노출용 telemetry snapshot (best-effort).

    Returns:
        enabled / topic / subscribed_connection_count + Redis hash 값.
        Redis 미가용 시 counter 값들은 0 또는 None.
    """
    base: Dict[str, Any] = {
        "enabled": config.TOPIC_DISPATCHER_ENABLED,
        "topic": TETHER_TOPIC,
        # PR Level 3 (Codex 권고): TOPIC_DISPATCHER_ENABLED와 분리해 KRX 포함
        # 여부를 별도 노출. 운영 중 "topic 켜져 있으나 KRX 격리" 상태 즉시 확인.
        "krx_topic_include": config.KRX_TOPIC_INCLUDE,
        "subscribed_connection_count": topic_dispatcher.registry.subscribed_connection_count,
    }
    # counter/last 기본값
    for field in _COUNTER_FIELDS:
        base[field] = 0
    base["last_result"] = None
    base["last_at_kst"] = None
    base["last_error"] = None

    client = redis_cache.client
    if client is None:
        return base
    try:
        if not await redis_cache.circuit.can_attempt():
            return base
    except Exception:
        return base

    try:
        raw = await client.hgetall(_TELEMETRY_KEY)
    except Exception:
        logger.debug("topic telemetry 조회 실패 (격리)", exc_info=True)
        return base

    if not raw:
        return base

    # raw는 bytes dict (decode_responses=False) — decode 필요
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

    return base


async def reset_topic_telemetry() -> bool:
    """telemetry counter 전체 reset (시험 구간 분리용).

    DEL topic:tether:stats — HDEL보다 단순 (필드 추가 자동 적용, Codex 권고).

    Returns:
        True — reset 성공 또는 key 부재 (성공 동등)
        False — Redis 미가용 / circuit open / 예외
    """
    client = redis_cache.client
    if client is None:
        return False
    try:
        if not await redis_cache.circuit.can_attempt():
            return False
    except Exception:
        return False

    try:
        await client.delete(_TELEMETRY_KEY)
        return True
    except Exception:
        logger.debug("topic telemetry reset 실패 (격리)", exc_info=True)
        return False
