"""USDT Redis-first path telemetry (PR Z-2e B-Step Telemetry, 2026-05-13).

ADR-029 "USDT source는 mirror cycle 미경유 → direct write 영구 실패 시 영구
stale 위험. 별도 telemetry 필요"의 future enhancement를 닫는다.

scope:
    USDT direct write + sync Redis read + DB fallback 경로 한정. broadcast hot
    path / topic publisher / FX/KRX 영역은 대상 X.

설계:
    - 프로세스 메모리 counter (threading.Lock 보호) — sync 환경 안전
    - 외부 monitoring 도구 미사용 환경의 1차 계측. Prometheus/Redis hash 확장은
      추세 분석 필요 시점에 검토.
    - 프로세스 재시작 시 counter reset → `started_at`으로 카운터 누적 시작 시각
      운영자에게 명시 (1234 success가 1시간/1일 어느 쪽 누적인지 구분).
    - reset_stats()는 테스트 전용, admin endpoint 미노출 (운영 실수 회피).

참조:
    - DECISIONS.md ADR-029: USDT mirror skip + direct write + read-path DB fallback
    - app/latest_rates_cache.py: set_/get_latest_usdt_rate_from_sync_job
    - app/usdt_topic_payload.py: load_and_build_tether_tab_payload DB fallback 분기
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_KST = timezone(timedelta(hours=9))

# Per-source counter 필드 (record_*에서 increment, get_stats에서 dict로 노출)
_SOURCE_COUNTER_FIELDS = (
    "direct_write_success",
    "direct_write_failure",
    "direct_write_regression_skipped",
    "redis_read_hit",
    "redis_read_miss",
    "redis_read_parse_fail",
    "redis_read_error",
)

# Per-source latest timestamp 필드 (record_*에서 갱신, None 초기값)
_SOURCE_TIMESTAMP_FIELDS = (
    "last_direct_write_success_at",
    "last_redis_read_hit_at",
)


def _now_kst_iso() -> str:
    return datetime.now(_KST).isoformat()


def _empty_source_stats() -> Dict[str, Any]:
    stats: Dict[str, Any] = {field: 0 for field in _SOURCE_COUNTER_FIELDS}
    for field in _SOURCE_TIMESTAMP_FIELDS:
        stats[field] = None
    return stats


def _empty_state() -> Dict[str, Any]:
    return {
        "started_at": _now_kst_iso(),
        "per_source": {},  # lazy populate — record 호출 시 source 등장
        "aggregate": {
            "db_fallback_count": 0,
            "last_db_fallback_at": None,
            "db_fallback_by_asset": {},  # asset별 fallback count
        },
    }


_lock = threading.Lock()
_state: Dict[str, Any] = _empty_state()


def _get_source_stats_locked(source: str) -> Dict[str, Any]:
    """lock이 이미 잡혀 있는 상태에서 호출. lazy init source entry."""
    per_source = _state["per_source"]
    if source not in per_source:
        per_source[source] = _empty_source_stats()
    return per_source[source]


# ── Record functions (sync, threading.Lock 보호) ──


def record_direct_write_success(source: str) -> None:
    """direct write 성공 — set_latest_usdt_rate_from_sync_job 호출 site."""
    with _lock:
        stats = _get_source_stats_locked(source)
        stats["direct_write_success"] += 1
        stats["last_direct_write_success_at"] = _now_kst_iso()


def record_direct_write_failure(source: str) -> None:
    """direct write 실패 — Redis SET 예외 또는 client init 실패."""
    with _lock:
        stats = _get_source_stats_locked(source)
        stats["direct_write_failure"] += 1


def record_direct_write_regression_skipped(source: str) -> None:
    """direct write 역행 차단 — incoming exchange ts < stored seen_at으로 SKIPPED_REGRESSION.

    coalesce SKIPPED(정상 동일 rate/bucket)와 구분되는 out-of-order write 차단 신호.
    counter는 매번 증가(관찰성), warning throttle은 호출 site(latest_rates_cache)에서.
    """
    with _lock:
        stats = _get_source_stats_locked(source)
        stats["direct_write_regression_skipped"] += 1


def record_redis_read_hit(source: str) -> None:
    """sync Redis GET hit — get_latest_usdt_rate_from_sync_job 성공."""
    with _lock:
        stats = _get_source_stats_locked(source)
        stats["redis_read_hit"] += 1
        stats["last_redis_read_hit_at"] = _now_kst_iso()


def record_redis_read_miss(source: str) -> None:
    """Redis key 부재 (raw=None)."""
    with _lock:
        stats = _get_source_stats_locked(source)
        stats["redis_read_miss"] += 1


def record_redis_read_parse_fail(source: str) -> None:
    """deserialize_value None 반환 (naive datetime / 형식 오류)."""
    with _lock:
        stats = _get_source_stats_locked(source)
        stats["redis_read_parse_fail"] += 1


def record_redis_read_error(source: str) -> None:
    """Redis GET 자체 예외 (connection / timeout 등)."""
    with _lock:
        stats = _get_source_stats_locked(source)
        stats["redis_read_error"] += 1


def record_db_fallback(asset: str) -> None:
    """topic builder가 Redis 5개 중 1+ miss로 전체 DB fallback 호출.

    aggregate count + asset별 분리 (미래 다른 topic-only asset 확장 대비).
    """
    with _lock:
        agg = _state["aggregate"]
        agg["db_fallback_count"] += 1
        agg["last_db_fallback_at"] = _now_kst_iso()
        by_asset = agg["db_fallback_by_asset"]
        by_asset[asset] = by_asset.get(asset, 0) + 1


def get_stats() -> Dict[str, Any]:
    """admin endpoint 노출용 snapshot.

    deep copy로 반환 — 호출자 mutation이 internal state에 영향 X.
    """
    with _lock:
        return copy.deepcopy(_state)


def reset_stats() -> None:
    """테스트 전용. 모든 counter/timestamp 0/None 초기화 + started_at 재설정.

    admin endpoint 노출 X (운영 실수 회피).
    """
    global _state
    with _lock:
        _state = _empty_state()
