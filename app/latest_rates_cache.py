# app/latest_rates_cache.py

"""Redis-first broadcast을 위한 latest state mirror 모듈 (PR3 Phase 2).

cache.py의 RedisCache primitive 위에 latest 도메인 로직을 격리한다.
DB가 ground truth, Redis는 mirror view. broadcast hot path가 매초 DB SELECT를
실행하지 않도록 mirror job이 주기적으로 DB latest를 Redis에 동기화하고,
broadcast는 Redis JSON을 읽는다.

이 commit은 PR3 Step 1 — key helper / value serialize·deserialize / stale 판정만.
warmup / mirror_latest_rates_once / build_rates_payload_from_redis 는 다음 commit.

설계 합의: REALTIME_ARCHITECTURE_PLAN.md §12 Phase 2 PR3 참고.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Union

from app.config import LATEST_MIRROR_INTERVAL_SECONDS

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

    Args:
        mirrored_at: mirror가 갱신한 시각 (timezone-aware datetime)
        interval_seconds: mirror 주기 (None이면 LATEST_MIRROR_INTERVAL_SECONDS env)
    """
    if interval_seconds is None:
        interval_seconds = LATEST_MIRROR_INTERVAL_SECONDS
    age_seconds = (datetime.now(_KST) - mirrored_at).total_seconds()
    return age_seconds > interval_seconds * STALE_RATIO


__all__ = [
    "STALE_RATIO",
    "latest_key_bank",
    "latest_key_source",
    "latest_key_investing",
    "serialize_value",
    "deserialize_value",
    "is_stale",
]
