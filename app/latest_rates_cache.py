# app/latest_rates_cache.py

"""Redis-first broadcast을 위한 latest state mirror 모듈 (PR3 Phase 2).

cache.py의 RedisCache primitive 위에 latest 도메인 로직을 격리한다.
DB가 ground truth, Redis는 mirror view. broadcast hot path가 매초 DB SELECT를
실행하지 않도록 mirror job이 주기적으로 DB latest를 Redis에 동기화하고,
broadcast는 Redis JSON을 읽는다.

PR3 Step 2 시점 구현: key helper / value serialize·deserialize / stale 판정 /
mirror core / warmup·mirror_once wrapper. broadcast Redis-first read path
(build_rates_payload_from_redis)는 다음 step.

설계 합의: REALTIME_ARCHITECTURE_PLAN.md §12 Phase 2 PR3 참고.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Union

from sqlalchemy.orm import Session

from app import crud
from app.cache import redis_cache
from app.config import LATEST_MIRROR_INTERVAL_SECONDS
from app.database import SessionLocal

logger = logging.getLogger("exchange_rate.latest_rates_cache")

# KST는 모듈 자기완결 (외부 의존 회피)
_KST = timezone(timedelta(hours=9))

# stale 판정 ratio. interval * STALE_RATIO 초 초과 시 stale.
# 운영에서 조정 필요해지면 env로 분리 (현재 코드 상수 시작).
STALE_RATIO = 2


# ── Key helpers ──────────────────────────────────────────────


def latest_key_bank(bank: str, currency: str) -> str:
    """은행 latest mirror Redis key. 예: latest:bank:kb:usd-krw"""
    return f"latest:bank:{bank}:{currency}"


def latest_key_source(source: str, asset: str) -> str:
    """거래소 source latest mirror Redis key. 예: latest:source:upbit:usdt-krw"""
    return f"latest:source:{source}:{asset}"


def latest_key_investing(currency: str) -> str:
    """Investing.com latest mirror Redis key (single source). 예: latest:investing:usd-krw"""
    return f"latest:investing:{currency}"


# ── Value serialization ──────────────────────────────────────


def serialize_value(rate: float, timestamp: str, mirrored_at: datetime) -> str:
    """latest value를 JSON 문자열로 직렬화.

    Args:
        rate: 환율
        timestamp: 환율 발생 시각 (ISO 8601 KST 문자열)
        mirrored_at: mirror job이 갱신한 시각 (timezone-aware datetime)
    """
    payload = {
        "rate": rate,
        "timestamp": timestamp,
        "mirrored_at": mirrored_at.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False)


def deserialize_value(raw: Union[str, bytes]) -> Optional[Dict[str, Any]]:
    """Redis raw 값을 dict로 파싱. mirrored_at은 timezone-aware datetime으로 변환.

    Redis client가 decode_responses=False로 설정돼 있어 bytes로 들어올 수 있고,
    cache.py wrapper를 거치면 str로 들어온다. json.loads는 둘 다 처리.

    파싱 실패 또는 mirrored_at이 naive datetime이면 None 반환 — 호출자가
    redis_error fallback reason으로 분류한다. naive datetime은 silent KST 부여
    대신 명시적 실패로 처리해 디버깅을 쉽게 한다.

    Returns:
        성공: {rate: float, timestamp: str, mirrored_at: datetime (tz-aware)}
        실패: None
    """
    try:
        data = json.loads(raw)
        parsed_at = datetime.fromisoformat(data["mirrored_at"])
        if parsed_at.tzinfo is None:
            return None
        return {
            "rate": float(data["rate"]),
            "timestamp": data["timestamp"],
            "mirrored_at": parsed_at,
        }
    except (ValueError, KeyError, TypeError):
        return None


# ── Stale 판정 ──────────────────────────────────────────────


def is_stale(
    mirrored_at: datetime,
    interval_seconds: Optional[int] = None,
) -> bool:
    """mirror가 최근에 갱신되지 않았는지(stale) 판정.

    stale 기준: now - mirrored_at > interval * STALE_RATIO

    naive datetime 입력 시 True 반환 (stale로 간주 → fallback DB read 유도).
    deserialize_value가 이미 naive를 거르지만 외부 직접 호출 방어용.
    silent KST 부여 대신 stale 처리 — TypeError로 broadcast 깨지는 것보다 안전.

    Args:
        mirrored_at: mirror가 갱신한 시각 (timezone-aware datetime 권장)
        interval_seconds: mirror 주기 (None이면 LATEST_MIRROR_INTERVAL_SECONDS env)
    """
    if mirrored_at.tzinfo is None:
        return True
    if interval_seconds is None:
        interval_seconds = LATEST_MIRROR_INTERVAL_SECONDS
    age_seconds = (datetime.now(_KST) - mirrored_at).total_seconds()
    return age_seconds > interval_seconds * STALE_RATIO


# ── Redis write helper (private) ──────────────────────────────


async def _set_latest(key: str, value: str) -> bool:
    """Redis SET with success/failure tracking.

    cache.py wrapper(redis_cache.set)는 None만 반환해 성공/실패 구분 불가하므로
    PR3 내부에서 client + circuit을 직접 다룬다. cache.py 공용 API는 그대로 둠.

    Returns:
        True: write 성공
        False: client 미연결 / circuit_open / 예외 발생
    """
    if not redis_cache.client or not await redis_cache.circuit.can_attempt():
        return False
    try:
        await redis_cache.client.set(key, value)
        await redis_cache.circuit.record_success()
        return True
    except Exception:
        await redis_cache.circuit.record_failure()
        logger.debug("Redis SET 실패 (latest mirror)", exc_info=True, extra={"key": key})
        return False


# ── Mirror core + wrappers ────────────────────────────────────


async def _mirror_all_latest(db: Session) -> Dict[str, int]:
    """DB latest를 Redis로 적재하는 core. warmup과 mirror_once가 공유.

    invariant:
        attempted_total = loaded_total + failed
        loaded_total    = bank + investing + source

    DB에서 받은 record만 시도 — hardcoded expected count 없음. 운영에서 은행 추가/
    제거 또는 일부 데이터 누락 시 attempted_total이 자연스럽게 반영됨.

    Returns:
        stats dict ({attempted_total, loaded_total, bank, investing, source, failed})
    """
    stats: Dict[str, int] = {
        "attempted_total": 0,
        "loaded_total": 0,
        "bank": 0,
        "investing": 0,
        "source": 0,
        "failed": 0,
    }
    mirrored_at = datetime.now(_KST)

    for pair in crud.SUPPORTED_CURRENCY_PAIRS:
        # investing — 단일 source, currency당 0~1 record
        inv = crud.select_a_latest_investing_rate_from_db(db, pair)
        if inv:
            stats["attempted_total"] += 1
            value = serialize_value(inv["rate"], inv["timestamp"], mirrored_at)
            if await _set_latest(latest_key_investing(inv["currency"]), value):
                stats["loaded_total"] += 1
                stats["investing"] += 1
            else:
                stats["failed"] += 1

        # bank — currency당 N개 은행
        for record in crud.select_latest_bank_rates_from_db(db, pair):
            stats["attempted_total"] += 1
            value = serialize_value(record["rate"], record["timestamp"], mirrored_at)
            if await _set_latest(latest_key_bank(record["bank"], record["currency"]), value):
                stats["loaded_total"] += 1
                stats["bank"] += 1
            else:
                stats["failed"] += 1

    # source — 5 거래소 × 1 asset (asset=None: 전체)
    # legacy adapter shape: {currency: asset, bank: source, rate, timestamp}
    for record in crud.get_source_rates_as_legacy_format(db):
        stats["attempted_total"] += 1
        value = serialize_value(record["rate"], record["timestamp"], mirrored_at)
        if await _set_latest(latest_key_source(record["bank"], record["currency"]), value):
            stats["loaded_total"] += 1
            stats["source"] += 1
        else:
            stats["failed"] += 1

    return stats


async def warmup_latest_rates() -> Optional[Dict[str, int]]:
    """앱 시작 시 1회 호출 — DB latest를 Redis에 모두 적재.

    FastAPI lifespan startup에서 호출 (Step 3에서 연결). 이 commit에선 호출처 없음.
    실패해도 broadcast가 DB fallback으로 동작하므로 앱 시작 자체는 막지 않는다.

    Returns:
        성공 시 stats dict, 예외 시 None (수동 smoke test/디버깅용).
        scheduler/lifespan에선 반환값 무시 가능.
    """
    db = SessionLocal()
    try:
        stats = await _mirror_all_latest(db)
        logger.info("✅ Redis latest mirror warmup 완료", extra=stats)
        return stats
    except Exception:
        logger.exception("❌ Redis latest mirror warmup 실패")
        return None
    finally:
        db.close()


async def mirror_latest_rates_once() -> Optional[Dict[str, int]]:
    """Scheduler IntervalTrigger 주기 호출 — DB latest를 Redis로 갱신.

    매 LATEST_MIRROR_INTERVAL_SECONDS마다 호출 (Step 3에서 scheduler 등록).
    이 commit에선 호출처 없음. 매 주기 INFO 폭증 회피를 위해 정상 시 DEBUG,
    실패가 있으면 WARNING으로 가시화.

    Returns:
        성공 시 stats dict, 예외 시 None (수동 smoke test/디버깅용).
        scheduler에선 반환값 무시 가능.
    """
    db = SessionLocal()
    try:
        stats = await _mirror_all_latest(db)
        if stats["failed"] > 0:
            logger.warning("⚠️ Redis latest mirror 일부 실패", extra=stats)
        else:
            logger.debug("Redis latest mirror 갱신", extra=stats)
        return stats
    except Exception:
        logger.exception("❌ Redis latest mirror 갱신 실패")
        return None
    finally:
        db.close()


__all__ = [
    "STALE_RATIO",
    "latest_key_bank",
    "latest_key_source",
    "latest_key_investing",
    "serialize_value",
    "deserialize_value",
    "is_stale",
    "warmup_latest_rates",
    "mirror_latest_rates_once",
]
