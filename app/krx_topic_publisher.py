"""KRX 달러선물 독립 topic publisher (ADR-038 Decision 2 — `krx:usd-krw-futures`).

usdt:krw optional group(usd_krw_futures) 시대를 종료하고 KRX를 전용 topic으로 분리:
- entitled 단말만 이 topic을 구독. ⚠️ **per-user 판정은 이제 서버 WS 가 한다**(1C `4a45173` —
  토큰 검증 + UID 결속 + KRX entitlement + lease publish gate). 구 주석의 "WS 무인증이라 클라
  krx_visible gate 가 담당"은 **낡았다**; 클라 gate 는 표시 축으로 유지된다(ADR-038 Decision 3).
- G2/G3(KRX_CLIENT_DISTRIBUTION_EFFECTIVE) off → 발행/snapshot 자체 중단
  (payload 필터링 불요 — topic 단위 차단이 옵션 B 채택 근거).

설계 (tether_topic_publisher 미러 + 경량화):
- KRX tick은 Redis writer가 이미 5초 coalesce (Stage E tick-level) → tether trigger
  controller 같은 별도 coalesce 계층 불요. Redis write 성공 시 request_krx_topic_publish
  1회 호출 → schedule_on_loop marshal(B1 패턴 — sync close finalizer/async tick 양쪽 커버).
- payload = {"type":"snapshot","topic":KRX_TOPIC,"version":1,
             "data":{"usd_krw_futures": entry}} — entry shape는 구 usdt:krw group과 동일
  (usdt_topic_payload._normalize_entry 재사용, iOS TopicSourceEntry 하위호환).
"""

# 표준 라이브러리
import logging
from typing import Any, Callable, Dict, Optional

# 로컬 애플리케이션
from app import config, topic_dispatcher, topic_trigger_bridge

logger = logging.getLogger("exchange_rate.krx_topic")

KRX_TOPIC = "krx:usd-krw-futures"
KRX_ASSET = "usd-krw-futures"


def build_krx_topic_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    """정규화된 entry → KRX topic payload (topic 필드 포함 — publisher가 schema 책임)."""
    return {
        "type": "snapshot",
        "topic": KRX_TOPIC,
        "version": 1,
        "data": {"usd_krw_futures": entry},
    }


def load_krx_topic_entry(
    db=None, *, checkpoint: Optional[Callable[[], None]] = None
) -> Optional[Dict[str, Any]]:
    """KRX latest entry 로드 — Redis-first, miss 시 DB fallback (snapshot 경로용).

    tick-발행 경로는 방금 Redis write 성공 직후라 사실상 항상 Redis hit.
    db=None이면 DB fallback 생략 (publish hot path — 세션 생성 비용 회피).
    """
    from app.latest_rates_cache import get_latest_krx_rate_from_sync_job
    from app.usdt_topic_payload import _normalize_entry

    if checkpoint is not None:
        checkpoint()
    raw = get_latest_krx_rate_from_sync_job(KRX_ASSET)
    if raw is None and db is not None:
        from app import crud
        # legacy shape dict({"bank","currency","rate","timestamp"}) 또는 None —
        # _normalize_entry가 legacy 키를 topic-native로 변환 (codex blocker 019f4117).
        if checkpoint is not None:
            checkpoint()
        raw = crud.get_latest_source_rate(db, "krx", KRX_ASSET)
    if raw is None:
        return None
    normalized = _normalize_entry(raw, fallback_source="krx", fallback_asset=KRX_ASSET)
    if normalized is None:
        return None
    if (normalized["source"], normalized["asset"]) != ("krx", KRX_ASSET):
        return None
    return normalized


async def publish_krx_topic_snapshot() -> bool:
    """KRX topic 구독자에게 latest snapshot publish.

    Guard 순서 (builder 비용 차단 — tether publisher 계약 미러):
        1. TOPIC_DISPATCHER_ENABLED off → False
        2. G2/G3 (KRX_CLIENT_DISTRIBUTION_EFFECTIVE) off → False (ADR-038 — 발행 자체 중단)
        3. subscriber_count(KRX_TOPIC) == 0 → False
    """
    if not config.TOPIC_DISPATCHER_ENABLED:
        return False
    if not config.KRX_CLIENT_DISTRIBUTION_EFFECTIVE:
        return False
    if topic_dispatcher.registry.subscriber_count(KRX_TOPIC) == 0:
        return False

    entry = load_krx_topic_entry()
    if entry is None:
        return False
    payload = build_krx_topic_payload(entry)
    sent = await topic_dispatcher.publish_topic(KRX_TOPIC, payload)
    return sent > 0


async def _run_publish() -> None:
    try:
        await publish_krx_topic_snapshot()
    except Exception:
        logger.exception("[krx_topic] publish 실패 (격리)")


def _schedule_publish() -> None:
    """loop 컨텍스트에서 publish task 생성 (schedule_on_loop 콜백)."""
    import asyncio
    asyncio.ensure_future(_run_publish())


def request_krx_topic_publish(reason: str) -> None:
    """KRX Redis write 성공 시 호출 (sync/async 어느 컨텍스트든 안전 — B1 marshal).

    구 경로(request_tether_topic_trigger reason=krx_redis_write_success — usdt:krw 재발행)
    대체. 실패는 격리 — writer hot path에 영향 0.
    """
    try:
        if not config.TOPIC_DISPATCHER_ENABLED or not config.KRX_CLIENT_DISTRIBUTION_EFFECTIVE:
            return   # 조기 skip — marshal 비용도 회피
        topic_trigger_bridge.schedule_on_loop(_schedule_publish)
    except Exception:
        logger.exception("[krx_topic] publish 스케줄 실패 (격리)", extra={"reason": reason})
