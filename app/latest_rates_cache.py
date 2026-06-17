# app/latest_rates_cache.py

"""Redis-first broadcast을 위한 latest state mirror 모듈 (PR3 Phase 2).

cache.py의 RedisCache primitive 위에 latest 도메인 로직을 격리한다.
DB가 ground truth, Redis는 mirror view. broadcast hot path가 매초 DB SELECT를
실행하지 않도록 mirror job이 주기적으로 DB latest를 Redis에 동기화하고,
broadcast는 Redis JSON을 읽는다.

PR3 Step 4 시점 구현: key helper / value serialize·deserialize / stale 판정 /
mirror core (latest:index control key 포함) / warmup·mirror_once wrapper /
fetch_rates_from_redis (broadcast Redis-first read path).
main.py는 import 안 함 (순환 회피) — DB fallback은 호출자가 처리.

설계 합의: REALTIME_ARCHITECTURE_PLAN.md §12 Phase 2 PR3 참고.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

import redis as redis_sync  # sync client (PR Z-2e B-Step 1, scheduler thread 전용)
from sqlalchemy.orm import Session

from app import config, crud
from app.cache import redis_cache
from app.config import LATEST_MIRROR_INTERVAL_SECONDS
from app.database import SessionLocal
from app.legacy_policy import should_include_source_in_legacy_rates

logger = logging.getLogger("exchange_rate.latest_rates_cache")

# KST는 모듈 자기완결 (외부 의존 회피)
_KST = timezone(timedelta(hours=9))

# stale 판정 ratio. interval * STALE_RATIO 초 초과 시 stale.
# 운영에서 조정 필요해지면 env로 분리 (현재 코드 상수 시작).
STALE_RATIO = 2

# control key — latest:index에 mirror가 적재한 data keys 목록 + cycle mirrored_at.
# fetch path는 이걸 single source of truth로 사용 (각 data key의 mirrored_at 재검증 X).
LATEST_INDEX_KEY = "latest:index"

# DXY는 latest:index와 분리해서 관리한다 (PR5).
# 이유: rates는 atomic snapshot이지만 DXY는 독립 optional indices field.
# DXY 적재 실패가 latest:index 갱신을 막아 rates fallback 전체를 유발하면 안 된다
# (DXY-only fallback 보존). DXY 자체는 단일 key로 충분.
LATEST_DXY_KEY = "latest:dxy:current"


class UsdtLatestWriteOutcome(Enum):
    """USDT direct writer 결과 — (5d-a) topic trigger 정책 입력.

    - FAILED: client init 실패 / parse 실패 / Redis SET 예외. trigger 차단.
    - SKIPPED: 5초 grain 안에서 same rate + same bucket → coalesce skip.
      Topic = change notification 역할이므로 동일 값/동일 bucket 재호출에 trigger 불필요.
    - SKIPPED_REGRESSION: incoming exchange ts < stored seen_at → out-of-order write
      차단 (reconnect snapshot/probe 경합 시 stale이 fresh 값을 덮지 않게). trigger 차단.
    - SET: Redis SET 성공 (cold-start GET miss 후 SET 포함). trigger 발사.
    - BLOCKED: P1b A2-3 write-mode gate(halt/atomic)로 SET 자체 차단. trigger 차단 +
      coalesce SKIPPED와 **구분**(polling caller가 success로 오인 안 함). atomic은 A3 전 fail-closed.
    """

    FAILED = "failed"
    SKIPPED = "skipped"
    SKIPPED_REGRESSION = "skipped_regression"
    SET = "set"
    BLOCKED = "blocked"


class KrxLatestWriteOutcome(Enum):
    """KRX tick-level Redis writer 결과 — Stage E (KRX_REDIS_TICK_WRITE_ENABLED=true) 입력.

    USDT 5d-a `UsdtLatestWriteOutcome` 패턴 mirror. trigger 발사 정책 동일.

    - FAILED: client init 실패 / parse 실패 / Redis SET 예외. trigger 차단.
    - SKIPPED: 5초 grain 안에서 same rate + same bucket → coalesce skip.
    - SET: Redis SET 성공. trigger 발사 (`TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS`).
    - BLOCKED: P1b A2-3 write-mode gate(halt/atomic)로 SET 차단. trigger 차단(SKIPPED와 구분).
    """

    FAILED = "failed"
    SKIPPED = "skipped"
    SET = "set"
    BLOCKED = "blocked"


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


# ── USDT-only freshness metadata helpers (5b-bis, §12.8.3 결정 #1) ──
#
# USDT 5 source 전용 Redis value schema 확장 — rate_changed_at / seen_at /
# mirrored_at 분리. 공통 serialize_value() / deserialize_value()는 미터치
# (KRX/Bank/Investing/mirror cycle isolation). Legacy ``timestamp = seen_at``
# alias로 단말 호환 보존.


def _floor_5s(ts: datetime) -> datetime:
    """5초 wall-clock grain floor (A-3, §12.8.3 결정 #2 정합).

    예: 20:34:47.500 → 20:34:45.000. tzinfo 보존.
    """
    return ts.replace(microsecond=0) - timedelta(seconds=ts.second % 5)


def _parse_kst(s: str) -> datetime:
    """ISO 8601 string → KST aware datetime.

    naive datetime은 silent KST 부여 대신 ValueError raise — 기존
    ``deserialize_value`` 패턴 정합 (line 109). ``deserialize_usdt_value``의
    try/except (ValueError 포함)에서 자연 catch → None 반환.

    Aware datetime은 KST로 normalize (Codex Finding 2 — 이름과 동작 정합).
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed: {s!r}")
    return dt.astimezone(_KST)


def serialize_usdt_value(
    rate: Decimal,
    rate_changed_at: datetime,
    seen_at: datetime,
    mirrored_at: datetime,
) -> str:
    """USDT 전용 Redis value JSON serializer (§12.8.3 결정 #1).

    Dual schema — legacy ``timestamp = seen_at`` (단말 호환). 새 field 3개
    추가: rate_changed_at / seen_at / mirrored_at (Redis SET 시각).
    """
    return json.dumps(
        {
            "rate": float(rate),
            "timestamp": seen_at.isoformat(),
            "rate_changed_at": rate_changed_at.isoformat(),
            "seen_at": seen_at.isoformat(),
            "mirrored_at": mirrored_at.isoformat(),
        },
        ensure_ascii=False,
    )


def deserialize_usdt_value(raw: Union[str, bytes]) -> Optional[Dict[str, Any]]:
    """USDT 전용 Redis value JSON deserializer — old/new schema 정규화.

    Old schema migration ({rate, timestamp, mirrored_at}):
        seen_at = timestamp (없으니 derive)
        rate_changed_at = timestamp (없으니 derive)
        mirrored_at = 기존 값 사용 (운영 정상 경로)
        mirrored_at 자체가 없는 매우 옛 legacy만 timestamp fallback.

    Grain floor는 deserialize에서 강제하지 *않음* — 다음 write 시점에 새 tick의
    seen_at floor가 자연 normalize (구현 단순성).

    실패 정책 (parse fail / naive datetime / 필수 field 누락) → None.
    기존 ``deserialize_value`` 패턴 정합. caller가 first write 분기로 자연 처리.

    Returns:
        성공: {rate: Decimal, timestamp: datetime, rate_changed_at: datetime,
               seen_at: datetime, mirrored_at: datetime} (모두 tz-aware)
        실패: None
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        rate = Decimal(str(data["rate"]))
        timestamp = _parse_kst(data["timestamp"])
        if timestamp.tzinfo is None:
            return None
        rate_changed_at = _parse_kst(data.get("rate_changed_at", data["timestamp"]))
        seen_at = _parse_kst(data.get("seen_at", data["timestamp"]))
        # Normal old schema migration: mirrored_at 보존.
        # Very old legacy fallback (운영 경로 외): mirrored_at 자체가 없을 때만 timestamp.
        mirrored_at = _parse_kst(data.get("mirrored_at", data["timestamp"]))
        return {
            "rate": rate,
            "timestamp": timestamp,
            "rate_changed_at": rate_changed_at,
            "seen_at": seen_at,
            "mirrored_at": mirrored_at,
        }
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
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


# ── Sync direct writer (scheduler thread, async circuit 격리) ──
#
# PR Z-2e B-Step 1 재시도 — 직전 시도(asyncio.run + main loop async client 재사용)는
# event loop binding mismatch로 실패 + async circuit_breaker 오염 위협.
# 이번 spec: sync `redis.Redis` 별도 client + async circuit_breaker 미사용 →
# scheduler thread에 자연 + broadcast/mirror Redis path 보호.
#
# 사용처:
#   - app/crawlers/usdt_sources.py: USDT INSERT 직후 best-effort direct write
#   - 향후 KRX/bank 등 sync crawler 확장 가능
#
# Key/value 포맷은 mirror cycle과 동일 (latest_key_source + serialize_value).
# USDT source 실패는 logger.warning + UsdtLatestWriteOutcome.FAILED 반환. mirror
# cycle은 Z-2d 이후 USDT source skip (legacy allowlist) — 실패 복구는 read
# path(B-Step 2)의 DB fallback이 담당한다. (broadcast 대상 source는 mirror
# cycle이 여전히 safety repair.)


_sync_client: Optional[redis_sync.Redis] = None  # lazy module-level

# 5b-bis 옵션 A (§12.8.3 결정 #1) — USDT 전용 in-memory state per (source, asset).
# 매 tick Redis GET 회피 → Redis I/O ↓↓ + saturation 완화 (Codex Finding 1 해결).
# Process restart 후 첫 tick(cold-start)만 Redis GET. SET 성공 후에만 갱신
# (Redis-memory drift 방지). Test 격리: setUp/tearDown에서 .clear().
_last_written_usdt_state: dict[tuple[str, str], dict] = {}


def _get_sync_client() -> Optional[redis_sync.Redis]:
    """lazy module-level sync Redis client.

    호출 시점에 `app.config.REDIS_URL` / `REDIS_PASSWORD`를 읽음 — 테스트 친화
    (env patch 시 `_sync_client = None` reset 후 재호출하면 새 client 생성).

    Returns:
        Redis client (성공) 또는 None (init 실패 — 로그만 남기고 호출자가 best-effort skip).

    Note:
        redis-py sync `Redis`는 internal connection pool 기반 thread-safe.
        socket_timeout/socket_connect_timeout 1.0s — crawler hot path가 Redis
        지연으로 막히지 않게 (worst case 5거래소 × 1초 = 5초).
        Async circuit_breaker는 건드리지 않음 (broadcast/mirror Redis path 격리).
    """
    global _sync_client
    if _sync_client is None:
        try:
            _sync_client = redis_sync.from_url(
                config.REDIS_URL,
                password=config.REDIS_PASSWORD or None,
                decode_responses=False,
                socket_timeout=1.0,
                socket_connect_timeout=1.0,
            )
        except Exception:
            logger.warning(
                "Redis sync client init 실패 (best-effort, read path DB fallback)",
                exc_info=True,
            )
            return None
    return _sync_client


# Regression skip warning throttle — counter는 매번, warning은 source별 이 간격마다 1회.
_REGRESSION_WARN_THROTTLE_SEC = 60.0
_last_regression_warn_at: Dict[str, float] = {}


def _maybe_warn_regression(source: str, incoming: datetime, stored: datetime) -> None:
    """역행 차단 warning을 source별 throttle (stale snapshot 반복 시 로그 폭주 회피)."""
    now = time.monotonic()
    if now - _last_regression_warn_at.get(source, 0.0) >= _REGRESSION_WARN_THROTTLE_SEC:
        _last_regression_warn_at[source] = now
        logger.warning(
            "USDT Redis write regression skip — incoming seen_at(%s) < stored(%s) "
            "source=%s (out-of-order write 차단, counter=direct_write_regression_skipped)",
            incoming.isoformat(), stored.isoformat(), source,
        )


# P1b A2-3: write-mode block 관측 (process-local approximate counter, debug용). gate가 halt/atomic으로
# SET을 막을 때 누적. 동시 thread RMW race로 일부 유실 가능(no-lock — debug 관측이라 허용). A2엔 0.
_write_mode_block_counts: dict[tuple[str, str], int] = {}


def _record_write_mode_block(label: str, enforced: str) -> None:
    """write-mode gate(halt/atomic)로 Redis SET을 막은 것 기록 (approximate counter + debug log)."""
    key = (label, enforced)
    _write_mode_block_counts[key] = _write_mode_block_counts.get(key, 0) + 1
    logger.debug("write-mode block (Redis SET 차단)", extra={"label": label, "enforced": enforced})


def set_latest_usdt_rate_from_sync_job(
    source: str,
    asset: str,
    rate: float,
    timestamp: str,
) -> UsdtLatestWriteOutcome:
    """sync scheduler thread 전용 direct writer.

    Args:
        source: 데이터 공급자 (예: "upbit").
        asset: 통화쌍 (예: "usdt-krw").
        rate: 환율.
        timestamp: ISO 8601 KST 문자열 (DB record.timestamp 기준).

    Returns:
        UsdtLatestWriteOutcome.SET: Redis SET 성공 — topic trigger 발사.
        UsdtLatestWriteOutcome.SKIPPED: same rate + same 5s bucket coalesce —
            trigger 차단 (Topic = change notification 역할).
        UsdtLatestWriteOutcome.FAILED: client init 실패 / parse 실패 / SET 예외 —
            trigger 차단 + caller warning.

    설계 격리:
        - sync `redis.Redis` client 사용 (cache.py async와 분리)
        - **async circuit_breaker 호출 X** — broadcast/mirror Redis path 오염 회피
        - 실패는 logger.warning + FAILED 반환

    Miss 처리 경로 (PR Z-2e B-Step 2):
        USDT source는 Z-2d allowlist 미통과로 mirror cycle skip. 즉 본 sync write가
        실패해도 mirror cycle이 safety repair하지 않는다. Miss/실패는 read path
        (`get_latest_usdt_rate_from_sync_job`)에서 None 감지 후 호출자가
        DB fallback(`crud.get_latest_source_rates_for_topic`)로 처리한다.
    """
    # P1b A2-3: write-mode gate (최상단, Redis I/O·stats 전). halt/atomic → SET 차단(BLOCKED),
    # legacy → 기존 흐름 한 글자도 안 바뀜. atomic은 A3 전 fail-closed(legacy fallback 금지).
    from app import atomic_write_runtime
    from app.atomic_write_control import WriterMode
    enforced = atomic_write_runtime.snapshot().enforced_action
    if enforced != WriterMode.LEGACY:
        _record_write_mode_block(f"usdt:{source}:{asset}", enforced)
        return UsdtLatestWriteOutcome.BLOCKED

    # 모듈 내부 import — 순환 참조 회피 (usdt_redis_stats가 latest_rates_cache 의존 안 함)
    from app import usdt_redis_stats

    client = _get_sync_client()
    if client is None:
        usdt_redis_stats.record_direct_write_failure(source)
        return UsdtLatestWriteOutcome.FAILED

    redis_key = latest_key_source(source, asset)
    state_key = (source, asset)

    # 5b-bis (§12.8.3 결정 #1) — 옵션 A: in-memory state per (source, asset).
    # Warm state면 Redis GET 회피 (Redis I/O ↓↓ + saturation 완화).
    # Best-effort write 철학 (Codex Finding 3) — parse/serialize/coalesce/SET 전체
    # try-block 안 → 모든 실패는 record_direct_write_failure + FAILED로 통일.
    try:
        rate_decimal = Decimal(str(rate))
        tick_ts = _parse_kst(timestamp)
        seen_at_floor = _floor_5s(tick_ts)

        # 1) In-memory state 우선 (warm path — no Redis I/O)
        state = _last_written_usdt_state.get(state_key)

        # 2) Cold-start (state miss) → Redis GET 1회로 initial state 복원
        if state is None:
            try:
                existing_raw = client.get(redis_key)
            except Exception:
                # GET 실패는 silent — write 시도로 회복 (burst log 폭주 회피)
                existing_raw = None
            existing = deserialize_usdt_value(existing_raw) if existing_raw else None
            if existing is not None:
                state = {
                    "rate": existing["rate"],
                    "seen_at": existing["seen_at"],
                    "rate_changed_at": existing["rate_changed_at"],
                }
                _last_written_usdt_state[state_key] = state

        # 3) Coalesce — same rate + same 5s bucket → skip (no SET, no metric)
        if (
            state is not None
            and state["rate"] == rate_decimal
            and state["seen_at"] == seen_at_floor
        ):
            return UsdtLatestWriteOutcome.SKIPPED

        # 3.5) Regression guard — out-of-order write 차단 (reconnect snapshot/probe 경합).
        # 더 오래된 exchange timestamp(seen_at_floor)가 더 최신 stored 값을 덮지 않게.
        # < strict: 동일 5s bucket의 rate 갱신은 허용 (same rate+bucket은 위 coalesce가 skip).
        # counter는 매번(관찰성), warning은 source별 throttle (stale snapshot 반복 시 로그 폭주).
        if state is not None and seen_at_floor < state["seen_at"]:
            usdt_redis_stats.record_direct_write_regression_skipped(source)
            _maybe_warn_regression(source, seen_at_floor, state["seen_at"])
            return UsdtLatestWriteOutcome.SKIPPED_REGRESSION

        # 4) rate_changed_at — same rate면 보존, 새 rate면 tick_ts (full precision)
        if state is not None and state["rate"] == rate_decimal:
            rate_changed_at = state["rate_changed_at"]
        else:
            rate_changed_at = tick_ts  # first write or rate 변경

        mirrored_at = datetime.now(_KST)
        value = serialize_usdt_value(
            rate_decimal, rate_changed_at, seen_at_floor, mirrored_at
        )
        client.set(redis_key, value)

        # 5) SET 성공한 *후에만* state 갱신 (Redis-memory drift 방지)
        _last_written_usdt_state[state_key] = {
            "rate": rate_decimal,
            "seen_at": seen_at_floor,
            "rate_changed_at": rate_changed_at,
        }
        usdt_redis_stats.record_direct_write_success(source)
        return UsdtLatestWriteOutcome.SET
    except Exception:
        usdt_redis_stats.record_direct_write_failure(source)
        logger.warning(
            "USDT sync Redis SET 실패 (best-effort, read path DB fallback)",
            exc_info=True,
            extra={"key": redis_key},
        )
        return UsdtLatestWriteOutcome.FAILED


def get_latest_usdt_rate_from_sync_job(
    source: str,
    asset: str,
) -> Optional[Dict[str, Any]]:
    """sync builder 전용 Redis GET — usdt:krw topic builder가 호출 (PR Z-2e B-Step 2).

    설계 격리 (write helper와 동일):
        - sync `redis.Redis` client 재사용 (`_get_sync_client`)
        - async circuit_breaker 호출 X
        - 실패는 logger.warning + None

    **Stale 판정 X** (B-Step 2 spec):
        USDT source는 Z-2d allowlist 미통과 → mirror cycle skip → mirrored_at은
        마지막 direct write 시점만 반영. 거래량 적은 source(gopax 등)는 정상
        시장 stagnant라 mirrored_at이 자연 오래된 채로 남음 — stale fallback
        대상 X. 호출자는 Redis 값 시간 무관 사용 (DB도 어차피 같은 값).

    Args:
        source: 데이터 공급자.
        asset: 통화쌍.

    Returns:
        {"source", "asset", "rate", "timestamp"} topic-native shape — DB fallback
        helper(`crud.get_latest_source_rates_for_topic`)와 동일.
        miss(key 부재) / parse fail / 예외 시 None — 호출자가 DB fallback.
    """
    from app import usdt_redis_stats

    client = _get_sync_client()
    if client is None:
        usdt_redis_stats.record_redis_read_error(source)
        return None
    key = latest_key_source(source, asset)
    try:
        raw = client.get(key)
    except Exception:
        usdt_redis_stats.record_redis_read_error(source)
        logger.warning(
            "sync Redis GET 실패 (best-effort, DB fallback에 의존)",
            exc_info=True,
            extra={"key": key},
        )
        return None
    if raw is None:
        usdt_redis_stats.record_redis_read_miss(source)
        return None
    parsed = deserialize_value(raw)
    if parsed is None:
        # parse fail (mirrored_at naive 등) — None 반환, 호출자 DB fallback
        usdt_redis_stats.record_redis_read_parse_fail(source)
        return None
    usdt_redis_stats.record_redis_read_hit(source)
    # topic-native shape으로 반환 — helper 인자 source/asset 그대로 부착
    return {
        "source": source,
        "asset": asset,
        "rate": parsed["rate"],
        "timestamp": parsed["timestamp"],
    }


# ── KRX (topic-only source, PR ADR-031) ─────────────────────────
#
# KRX 미국달러선물은 USDT와 같이 topic-only source이지만 시맨틱이 다르다:
#   - USDT: 24/7 운영, 거래 뜸한 거래소 자연 stale (mirrored_at 갱신 안 됨)
#   - KRX: active session 명확, WebSocket tick 기반, session-end 저유동성
# 따라서 telemetry 시맨틱도 분리 — KRX는 usdt_redis_stats에 기록하지 않는다
# (ADR-031). bank/investing writer 패턴 일관: stats 미부착, async circuit 격리.
# stale 정책은 본 helper 범위 외 — ADR-027 영역에서 결정.


def set_latest_krx_rate_from_sync_job(
    asset: str,
    rate: float,
    timestamp: str,
) -> bool:
    """sync scheduler thread 전용 direct writer — KRX (PR ADR-031).

    `KrxDbWriter`가 DB insert 성공 직후 호출. usdt:krw topic builder의
    `get_latest_krx_rate_from_sync_job` Redis-first read와 짝.

    USDT writer(`set_latest_usdt_rate_from_sync_job`)와 분리한 이유:
        - USDT writer는 `usdt_redis_stats` counter를 갱신하므로 KRX 재사용 시
          텔레메트리 시맨틱 오염 (`usdt_redis_stats.per_source["krx"]` 등장)
        - bank/investing helper 패턴(Step 3b — stats 미부착)과 일관

    Args:
        asset: KRX asset (예: "usd-krw-futures"). source는 "krx" 고정.
        rate: 가격.
        timestamp: ISO 8601 KST 문자열.

    Returns:
        True: Redis SET 성공.
        False: client init 실패 / SET 예외.

    설계 격리:
        - sync `redis.Redis` client 재사용 (`_get_sync_client`)
        - **async circuit_breaker 호출 X**
        - **`usdt_redis_stats` 호출 X** (KRX 전용 — 텔레메트리 분리)
    """
    client = _get_sync_client()
    if client is None:
        return False
    key = latest_key_source("krx", asset)
    mirrored_at = datetime.now(_KST)
    value = serialize_value(rate, timestamp, mirrored_at)
    try:
        client.set(key, value)
        return True
    except Exception:
        logger.warning(
            "KRX sync Redis SET 실패 (best-effort, DB fallback 안전망)",
            exc_info=True,
            extra={"key": key},
        )
        return False


# KRX Stage E — tick-level helper (KRX_REDIS_TICK_WRITE_ENABLED=true 시 사용).
# USDT 5b-bis schema mirror — 5 fields + in-memory state.
# 단일 source/asset이라 dict 불필요, 단일 dict[Optional] state 보관.
_last_written_krx_state: dict[str, dict] = {}  # key=asset


def set_latest_krx_rate_from_sync_job_tick_level(
    asset: str,
    rate: float,
    timestamp: str,
) -> KrxLatestWriteOutcome:
    """KRX tick-level Redis writer — Stage E (KRX_FANOUT_REFACTOR_PLAN §5.2 E).

    `KrxRedisLatestWriter` tick handler가 매 WS tick에서 호출. USDT 5b-bis
    패턴 mirror — 5-field schema (legacy `timestamp=seen_at` alias + 신규
    `rate_changed_at` / `seen_at` / `mirrored_at`) + in-memory state로 warm
    same-rate/same-5s-bucket SET 자체 SKIPPED + Redis GET 회피.

    USDT 5d-a `UsdtLatestWriteOutcome` 정책 mirror:
        - FAILED: client init / parse / SET 실패 → trigger 차단
        - SKIPPED: 5s grain coalesce → trigger 차단 (change notification 의미상)
        - SET: Redis SET 성공 → trigger 발사 (`TETHER_TRIGGER_REASON_KRX_REDIS_WRITE_SUCCESS`)

    Args:
        asset: KRX asset (예: "usd-krw-futures"). source는 "krx" 고정.
        rate: 가격.
        timestamp: ISO 8601 KST 문자열 (KIS WS frame 체결시각).

    Returns:
        KrxLatestWriteOutcome — caller (`KrxRedisLatestWriter`)가 SET 시점에만
        `request_tether_topic_trigger` 호출.

    설계 격리:
        - sync `redis.Redis` client 재사용 (`_get_sync_client`)
        - async circuit_breaker 호출 X
        - usdt_redis_stats 호출 X (KRX 전용, telemetry 분리)
    """
    # P1b A2-3: write-mode gate (최상단, Redis I/O 전). halt/atomic → SET 차단(BLOCKED),
    # legacy → 기존 흐름 불변. atomic은 A3 전 fail-closed(legacy fallback 금지).
    from app import atomic_write_runtime
    from app.atomic_write_control import WriterMode
    enforced = atomic_write_runtime.snapshot().enforced_action
    if enforced != WriterMode.LEGACY:
        _record_write_mode_block(f"krx:{asset}", enforced)
        return KrxLatestWriteOutcome.BLOCKED

    client = _get_sync_client()
    if client is None:
        return KrxLatestWriteOutcome.FAILED

    key = latest_key_source("krx", asset)

    # USDT 5b-bis 옵션 A mirror — warm state면 Redis GET 회피.
    try:
        rate_decimal = Decimal(str(rate))
        tick_ts = _parse_kst(timestamp)
        seen_at_floor = _floor_5s(tick_ts)

        state = _last_written_krx_state.get(asset)

        # Cold-start (state miss) → Redis GET 1회로 initial state 복원
        if state is None:
            try:
                existing_raw = client.get(key)
            except Exception:
                existing_raw = None
            existing = deserialize_usdt_value(existing_raw) if existing_raw else None
            if existing is not None:
                state = {
                    "rate": existing["rate"],
                    "seen_at": existing["seen_at"],
                    "rate_changed_at": existing["rate_changed_at"],
                }
                _last_written_krx_state[asset] = state

        # Coalesce — same rate + same 5s bucket → SKIPPED (no SET)
        if (
            state is not None
            and state["rate"] == rate_decimal
            and state["seen_at"] == seen_at_floor
        ):
            return KrxLatestWriteOutcome.SKIPPED

        # rate_changed_at — same rate면 보존, 새 rate면 tick_ts (full precision)
        if state is not None and state["rate"] == rate_decimal:
            rate_changed_at = state["rate_changed_at"]
        else:
            rate_changed_at = tick_ts

        mirrored_at = datetime.now(_KST)
        value = serialize_usdt_value(
            rate_decimal, rate_changed_at, seen_at_floor, mirrored_at
        )
        client.set(key, value)

        # SET 성공한 *후에만* state 갱신 (Redis-memory drift 방지)
        _last_written_krx_state[asset] = {
            "rate": rate_decimal,
            "seen_at": seen_at_floor,
            "rate_changed_at": rate_changed_at,
        }
        return KrxLatestWriteOutcome.SET
    except Exception:
        logger.warning(
            "KRX tick-level Redis SET 실패 (best-effort, DB fallback 안전망)",
            exc_info=True,
            extra={"key": key},
        )
        return KrxLatestWriteOutcome.FAILED


def get_latest_krx_rate_from_sync_job(
    asset: str,
) -> Optional[Dict[str, Any]]:
    """sync builder 전용 Redis GET — KRX latest (PR ADR-031).

    usdt:krw topic builder가 호출. Redis hit이면 그대로 채택, miss/parse fail
    시 None → 호출자(`load_and_build_tether_tab_payload`)가 DB fallback.

    **Stale 판정 X** (ADR-031 1차 spec):
        KRX는 `insert_source_rate_if_changed`라 가격 stagnant 시 DB row 없음 →
        Redis timestamp도 마지막 변경 시점에 stuck. DB row gap ≠ raw frame gap
        이라 timestamp age로 stale 판단 시 저유동성 정상 상태를 false positive로
        누락 위험. session-end / REST fallback / quote_age 등은 ADR-027 영역.

    Args:
        asset: KRX asset (예: "usd-krw-futures"). source는 "krx" 고정.

    Returns:
        topic-native shape `{source, asset, rate, timestamp}` (DB
        fallback과 동일 shape) 또는 miss/parse fail/error 시 None.

    설계 격리:
        - sync `redis.Redis` client 재사용
        - **async circuit_breaker 호출 X**
        - **`usdt_redis_stats` 호출 X** (KRX 전용)
    """
    client = _get_sync_client()
    if client is None:
        return None
    key = latest_key_source("krx", asset)
    try:
        raw = client.get(key)
    except Exception:
        logger.warning(
            "KRX sync Redis GET 실패 (best-effort, DB fallback 의존)",
            exc_info=True,
            extra={"key": key},
        )
        return None
    if raw is None:
        return None
    parsed = deserialize_value(raw)
    if parsed is None:
        return None
    return {
        "source": "krx",
        "asset": asset,
        "rate": parsed["rate"],
        "timestamp": parsed["timestamp"],
    }


# ---------------------------------------------------------------------------
# KRX close finalizer race-prevention flag (KRX_CLOSE_SNAPSHOT_PLAN §5.3)
# ---------------------------------------------------------------------------
# Redis TTL flag `close_captured:krx:{session}:{kst_date}`:
#   - WS close grace path (KrxCloseWindowWriter, Stage 3 예정)가 DB+Redis 양쪽
#     성공 시 SET — close 확정 신호.
#   - REST fallback (KrxCloseSnapshotController, Stage 4 예정)가 호출 전 GET →
#     captured 시 skip (중복 호출 차단).
#
# Best-effort helper 설계 (insert_source_rate_unconditional와 contract 다름):
#   - 예외는 logger.warning + False return (caller 영향 격리)
#   - GET 실패 시 False default → REST 호출이 진행 (catastrophic backup 안전 default)
#
# TTL 1h — 다음날 자연 expire. process restart 시 idempotent.

_KRX_CLOSE_CAPTURED_KEY_FMT = "close_captured:krx:{session}:{kst_date}"
_KRX_CLOSE_CAPTURED_TTL_SEC = 3600  # 1h


def _krx_close_captured_key(session: str, kst_date: str) -> str:
    """KRX close captured flag Redis key.

    Args:
        session: "CF" / "CM"
        kst_date: ISO date string (예: "2026-05-19"). CM은 boundary 06:00이 찍히는 날.
    """
    return _KRX_CLOSE_CAPTURED_KEY_FMT.format(session=session, kst_date=kst_date)


def set_krx_close_captured_flag(session: str, kst_date: str) -> bool:
    """KRX close grace path가 종가 capture 성공 시 호출 — REST fallback skip 신호.

    Args:
        session: "CF" / "CM"
        kst_date: ISO date (boundary 찍힌 날, CM은 06:00 찍힌 날)

    Returns:
        True: SETEX 성공. False: client init 실패 또는 SETEX 예외 (best-effort 격리).

    설계: sync redis client + 예외는 logger.warning + False return.
        DB/Redis 성공 조건 조합 판단은 caller 책임 (KrxCloseWindowWriter Stage 3).
    """
    client = _get_sync_client()
    if client is None:
        return False
    key = _krx_close_captured_key(session, kst_date)
    try:
        client.setex(key, _KRX_CLOSE_CAPTURED_TTL_SEC, "1")
        return True
    except Exception:
        logger.warning(
            "KRX close captured flag SET 실패 (best-effort, REST fallback이 중복 실행 가능)",
            exc_info=True,
            extra={"key": key},
        )
        return False


def get_krx_close_captured_flag(session: str, kst_date: str) -> bool:
    """KRX close captured 여부 조회 — REST fallback 호출 전 race 차단용.

    Args:
        session: "CF" / "CM"
        kst_date: ISO date

    Returns:
        True: flag SET 됨 (WS path가 이미 capture, REST skip 권장).
        False: flag 없음 / client init 실패 / GET 예외 (catastrophic backup default 안전).

    설계: Redis 장애 시 False return — REST가 catastrophic backup 역할이라
        오류 시 REST 호출 진행이 default가 안전 (DB unconditional INSERT 정책상
        중복 row 발생 가능하지만 종가 누락보다 안전).
    """
    client = _get_sync_client()
    if client is None:
        return False
    key = _krx_close_captured_key(session, kst_date)
    try:
        return client.get(key) is not None
    except Exception:
        logger.warning(
            "KRX close captured flag GET 실패 (best-effort, False return → REST 호출 진행)",
            exc_info=True,
            extra={"key": key},
        )
        return False


# ─────────────────────────────────────────────────────────────────────
# KRX close finalizer structured event persist (2026-05-26)
# ─────────────────────────────────────────────────────────────────────
# event_type append-only log with 14d retention.
# 설계 원칙:
#   - best-effort / no-throw — close finalizer 본 동작 영향 0
#   - 저장 시점 case 분류 X — query 시점 (date_kst, session) aggregation
#   - dedup_skipped catalog 제외 (실제 코드 분기 부재, speculation 차단)
#
# event catalog (10가지):
#   ws_close_saved, rest_skipped_ws_captured, rest_fallback_attempted,
#   rest_returned_none, rest_price_missing, rest_price_parse_failed,
#   rest_sanity_aborted, rest_write_blocked, rest_write_saved, rest_write_failed

_KRX_CLOSE_EVENT_KEY = "krx:close_finalizer_events"
_KRX_CLOSE_EVENT_TTL_SEC = 14 * 24 * 3600  # 14 days


def emit_krx_close_event(
    event_type: str,
    *,
    session: str,
    date_kst: str,
    boundary_at_kst: Optional[str] = None,
    attempt: Optional[int] = None,
    rate: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> bool:
    """KRX close finalizer event를 Redis ZSET에 append-only 저장.

    Args:
        event_type: catalog 10가지 중 1 (예: "ws_close_saved").
        session: "CF" / "CM".
        date_kst: ISO date string (예: "2026-05-26"). CF는 boundary date,
            CM은 boundary 06:00이 찍히는 날.
        boundary_at_kst: ISO KST timestamp (option). close boundary 시각.
        attempt: REST retry attempt 번호 (option, retry sequence 안에서만).
        rate: 가격 (option, emit type별로 다름).
        extra: 추가 metadata dict (option).

    Returns:
        True: ZADD 성공 (ZREMRANGEBYSCORE trim 실패해도 ZADD 성공 보존).
        False: flag false / client init 실패 / serialize 실패 / ZADD 실패
            (best-effort 격리, close finalizer 영향 0).

    설계 (no-throw):
        - flag `KRX_CLOSE_EVENT_LOG_ENABLED=false` 시 즉시 False (no-op)
        - 모든 단계 try/except + logger.warning + False return
        - exception propagate 0 — close finalizer return 값 변경 0
        - trim 실패는 자연 누적 — 다음 emit에서 retry
    """
    from app import config
    if not config.KRX_CLOSE_EVENT_LOG_ENABLED:
        return False

    client = _get_sync_client()
    if client is None:
        return False

    now_kst = datetime.now(_KST)
    emit_at_kst_iso = now_kst.isoformat()
    emit_at_epoch_ms = int(now_kst.timestamp() * 1000)

    # event_id: 같은 close 안 같은 event_type이 여러 번 emit 가능 (attempt별)
    # → attempt + emit_at_epoch_ms로 unique 보장 (ZSET member uniqueness)
    event_id_parts = [date_kst, session, event_type]
    if attempt is not None:
        event_id_parts.append(f"attempt={attempt}")
    event_id_parts.append(f"emit={emit_at_epoch_ms}")
    event_id = ":".join(event_id_parts)

    event = {
        "event_id": event_id,
        "event_type": event_type,
        "session": session,
        "date_kst": date_kst,
        "emit_at_kst": emit_at_kst_iso,
    }
    if boundary_at_kst is not None:
        event["boundary_at_kst"] = boundary_at_kst
    if attempt is not None:
        event["attempt"] = attempt
    if rate is not None:
        event["rate"] = rate
    if extra:
        event["extra"] = extra

    try:
        member = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        logger.warning(
            "KRX close event JSON serialize 실패 (best-effort)",
            exc_info=True,
            extra={"event_type": event_type, "session": session, "date_kst": date_kst},
        )
        return False

    try:
        client.zadd(_KRX_CLOSE_EVENT_KEY, {member: emit_at_epoch_ms})
    except Exception:
        logger.warning(
            "KRX close event ZADD 실패 (best-effort)",
            exc_info=True,
            extra={"event_id": event_id},
        )
        return False

    # 14일 이전 trim — best-effort, 실패해도 ZADD 성공 보존
    try:
        cutoff_ms = emit_at_epoch_ms - _KRX_CLOSE_EVENT_TTL_SEC * 1000
        client.zremrangebyscore(_KRX_CLOSE_EVENT_KEY, "-inf", cutoff_ms)
    except Exception:
        logger.warning(
            "KRX close event ZREMRANGEBYSCORE trim 실패 (best-effort, ZADD 성공 보존)",
            exc_info=True,
        )
        # ZADD 성공이라 True return — trim 실패는 자연 누적

    return True


def get_krx_close_events(
    since_epoch_ms: int,
    until_epoch_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """KRX close finalizer events 시간 범위 query.

    Args:
        since_epoch_ms: 시작 timestamp (포함).
        until_epoch_ms: 종료 timestamp (포함). None이면 +inf (현재까지).

    Returns:
        시간순 정렬된 event dict list. Redis 장애 또는 parse 실패 시 빈 list.

    설계 (no-throw): admin endpoint에서 호출. Redis 장애 시 빈 list →
        endpoint가 빈 aggregation 반환 (catastrophic backup 안전 default).
    """
    client = _get_sync_client()
    if client is None:
        return []

    max_score = "+inf" if until_epoch_ms is None else until_epoch_ms
    try:
        members = client.zrangebyscore(
            _KRX_CLOSE_EVENT_KEY, since_epoch_ms, max_score,
        )
    except Exception:
        logger.warning("KRX close events ZRANGEBYSCORE 실패", exc_info=True)
        return []

    events = []
    for m in members:
        try:
            if isinstance(m, bytes):
                m = m.decode("utf-8")
            events.append(json.loads(m))
        except Exception:
            logger.warning(
                "KRX close event JSON parse 실패 (skip)", exc_info=True,
                extra={"member": str(m)[:200]},
            )
            continue
    return events


def _safe_record_bank_investing_set_stat(fn, *args, **kwargs) -> None:
    """bank/investing SET-outcome telemetry record를 writer hot path에서 격리.

    bank_investing_redis_stats.record_*는 내부 try/except를 갖지만, 향후 모듈
    리팩터/테스트 monkeypatch로 record_*가 예외를 던지더라도 writer의 SET 결과·
    bool 반환이 절대 영향받지 않도록 호출부에서 한 번 더 막는다(production hot
    path 불변 — telemetry는 비필수 관측이라 유실 허용, rate write 중단 불가).

    명칭을 bank/investing SET stat로 좁힌 이유: 같은 모듈의 다른 telemetry
    (usdt_redis_stats 등)에 잘못 재사용되지 않도록 (semantics·로그 문구 전용).
    """
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.debug(
            "bank/investing SET telemetry record 격리 (writer hot path 보호)",
            exc_info=True,
        )


def set_latest_bank_rate_from_sync_job(
    bank: str,
    asset: str,
    rate: float,
    timestamp: str,
) -> bool:
    """sync scheduler thread 전용 direct writer — bank (PR Z-2e Step 3b).

    `crud.insert_bank_rates_into_db`가 commit 직후 호출. broadcast hot path가
    latest:bank:* key를 Redis-first로 읽으므로 mirror cycle 3초 bypass.

    USDT writer와 다른 점: bank는 Z-2d allowlist 통과 → mirror cycle (3s)이 매
    사이클 latest:bank:* key를 재기록한다 (safety net). 따라서 본 direct write가
    실패해도 다음 mirror cycle이 자연 복구한다. 호출자 흐름(FCM alerts)에 영향 X.

    Args:
        bank: 은행 식별자 (예: "kb", "hana").
        asset: 통화쌍 (예: "usd-krw").
        rate: 환율.
        timestamp: ISO 8601 KST 문자열 (DB record.timestamp 기준).

    Returns:
        True: Redis SET 성공.
        False: client 미가용(client_unavailable) / key·serialize 예외
            (writer_exception) / SET 예외(set_exception). 실패는
            bank_investing_redis_stats에 원인별 집계 (item 4).

    설계 격리 (USDT writer와 동일):
        - sync `redis.Redis` client 사용 (broadcast/mirror async path와 분리)
        - **async circuit_breaker 호출 X**
        - SET-outcome telemetry 부착 (item 4 — bank_investing_redis_stats,
          _safe_record_bank_investing_set_stat로 writer hot path 격리)
    """
    from app import bank_investing_redis_stats as set_stats  # 순환 없음(set_stats는 app 미의존)
    _safe_record_bank_investing_set_stat(set_stats.record_attempt, bank, asset)
    try:
        client = _get_sync_client()
    except Exception as e:  # 방어적: _get_sync_client는 현재 내부에서 예외를 흡수하나
        _safe_record_bank_investing_set_stat(  # 향후 리팩터/monkeypatch에도 bool 계약 유지
            set_stats.record_failure, bank, asset, "client_unavailable",
            error=f"{type(e).__name__}: {e}",
        )
        return False
    if client is None:
        _safe_record_bank_investing_set_stat(
            set_stats.record_failure, bank, asset, "client_unavailable"
        )
        return False
    try:
        key = latest_key_bank(bank, asset)
        mirrored_at = datetime.now(_KST)
        value = serialize_value(rate, timestamp, mirrored_at)
    except Exception as e:
        _safe_record_bank_investing_set_stat(
            set_stats.record_failure, bank, asset, "writer_exception",
            error=f"{type(e).__name__}: {e}",
        )
        logger.warning(
            "bank sync Redis writer 예외 (key/serialize 단계, mirror cycle 안전망 의존)",
            exc_info=True,
            extra={"bank": bank, "asset": asset},  # key 미정의 가능 → bank/asset
        )
        return False
    try:
        client.set(key, value)
        _safe_record_bank_investing_set_stat(set_stats.record_success, bank, asset)
        return True
    except Exception as e:
        _safe_record_bank_investing_set_stat(
            set_stats.record_failure, bank, asset, "set_exception",
            error=f"{type(e).__name__}: {e}",
        )
        logger.warning(
            "bank sync Redis SET 실패 (best-effort, mirror cycle 안전망 의존)",
            exc_info=True,
            extra={"key": key},
        )
        return False


def get_latest_bank_rate_from_sync_job(
    bank: str, asset: str,
) -> Optional[Dict[str, Any]]:
    """sync builder 전용 Redis GET — bank latest (PR Z-2e Step 3a).

    USDT source(get_latest_usdt_rate_from_sync_job)와 달리 bank는 Z-2d allowlist
    통과 → mirror cycle이 매 3초 latest:bank:* key를 갱신한다. 따라서 stale 판정
    적용 (is_stale, 6초 기준). stale/miss/parse fail/error 시 None — 호출자 DB
    fallback. async circuit_breaker 미사용 (USDT helper 패턴 일관, broadcast Redis
    path 격리).

    Args:
        bank: 은행 식별자 (예: "kb", "hana").
        asset: 통화쌍 (예: "usd-krw").

    Returns:
        {"source", "asset", "rate", "timestamp"} topic-native shape.
        Stale / miss / parse fail / 예외 시 None.

    Note:
        반환 shape를 topic-native({source, asset, rate, timestamp})로 두는 이유:
        builder의 `_normalize_entry`는 거치지만 legacy shape({bank, currency, ...})
        변환 비용이 없어진다 (USDT helper와 일관, FX topic 재사용 시에도 정합).
    """
    client = _get_sync_client()
    if client is None:
        return None
    key = latest_key_bank(bank, asset)
    try:
        raw = client.get(key)
    except Exception:
        logger.warning(
            "sync Redis GET 실패 — bank (best-effort, DB fallback 의존)",
            exc_info=True,
            extra={"key": key},
        )
        return None
    if raw is None:
        return None
    parsed = deserialize_value(raw)
    if parsed is None:
        return None
    # bank/investing은 mirror cycle 갱신 가정 — stale 시 DB fallback
    if is_stale(parsed["mirrored_at"]):
        return None
    return {
        "source": bank,
        "asset": asset,
        "rate": parsed["rate"],
        "timestamp": parsed["timestamp"],
    }


def set_latest_investing_rate_from_sync_job(
    asset: str,
    rate: float,
    timestamp: str,
) -> bool:
    """sync scheduler thread 전용 direct writer — investing (PR Z-2e Step 3b).

    `crud.insert_investing_rates_into_db`가 commit 직후 호출. bank writer와 동일
    설계 (Z-2d allowlist 통과 → mirror cycle safety net + async circuit 격리 +
    SET-outcome telemetry 부착, item 4 — source="investing").

    Args:
        asset: 통화쌍.
        rate: 환율.
        timestamp: ISO 8601 KST 문자열.

    Returns:
        True: Redis SET 성공.
        False: client 미가용 / key·serialize 예외 / SET 예외
            (bank_investing_redis_stats 원인별 집계).
    """
    from app import bank_investing_redis_stats as set_stats  # 순환 없음
    _safe_record_bank_investing_set_stat(set_stats.record_attempt, "investing", asset)
    try:
        client = _get_sync_client()
    except Exception as e:  # 방어적: bool 계약 유지(향후 리팩터/monkeypatch 대비)
        _safe_record_bank_investing_set_stat(
            set_stats.record_failure, "investing", asset, "client_unavailable",
            error=f"{type(e).__name__}: {e}",
        )
        return False
    if client is None:
        _safe_record_bank_investing_set_stat(
            set_stats.record_failure, "investing", asset, "client_unavailable"
        )
        return False
    try:
        key = latest_key_investing(asset)
        mirrored_at = datetime.now(_KST)
        value = serialize_value(rate, timestamp, mirrored_at)
    except Exception as e:
        _safe_record_bank_investing_set_stat(
            set_stats.record_failure, "investing", asset, "writer_exception",
            error=f"{type(e).__name__}: {e}",
        )
        logger.warning(
            "investing sync Redis writer 예외 (key/serialize 단계, mirror cycle 안전망 의존)",
            exc_info=True,
            extra={"asset": asset},  # key 미정의 가능 → asset
        )
        return False
    try:
        client.set(key, value)
        _safe_record_bank_investing_set_stat(set_stats.record_success, "investing", asset)
        return True
    except Exception as e:
        _safe_record_bank_investing_set_stat(
            set_stats.record_failure, "investing", asset, "set_exception",
            error=f"{type(e).__name__}: {e}",
        )
        logger.warning(
            "investing sync Redis SET 실패 (best-effort, mirror cycle 안전망 의존)",
            exc_info=True,
            extra={"key": key},
        )
        return False


def get_latest_investing_rate_from_sync_job(
    asset: str,
) -> Optional[Dict[str, Any]]:
    """sync builder 전용 Redis GET — Investing latest (PR Z-2e Step 3a).

    bank helper와 같은 정책 (mirror cycle 갱신 가정 + is_stale 적용 + async
    circuit 미사용). source는 "investing" 고정.

    Args:
        asset: 통화쌍 (예: "usd-krw").

    Returns:
        {"source": "investing", "asset", "rate", "timestamp"} 또는 None.
    """
    client = _get_sync_client()
    if client is None:
        return None
    key = latest_key_investing(asset)
    try:
        raw = client.get(key)
    except Exception:
        logger.warning(
            "sync Redis GET 실패 — investing (best-effort, DB fallback 의존)",
            exc_info=True,
            extra={"key": key},
        )
        return None
    if raw is None:
        return None
    parsed = deserialize_value(raw)
    if parsed is None:
        return None
    if is_stale(parsed["mirrored_at"]):
        return None
    return {
        "source": "investing",
        "asset": asset,
        "rate": parsed["rate"],
        "timestamp": parsed["timestamp"],
    }


async def _get_latest_with_reason(key: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Redis GET with explicit fallback reason classification.

    cache.py wrapper(redis_cache.get)는 None만 반환해 redis_miss / circuit_open /
    redis_error 구분 불가 → PR3 내부에서 client + circuit 직접 다룬다.

    Returns:
        (value, None): key 존재
        (None, 'circuit_open'): Circuit Breaker open 상태
        (None, 'redis_error'): client 미연결 또는 명령 실패
        (None, 'redis_miss'): key 없음 (정상 응답이지만 데이터 부재)
    """
    if not redis_cache.client:
        return None, "redis_error"
    if not await redis_cache.circuit.can_attempt():
        return None, "circuit_open"
    try:
        value = await redis_cache.client.get(key)
        await redis_cache.circuit.record_success()
        if value is None:
            return None, "redis_miss"
        return value, None
    except Exception:
        await redis_cache.circuit.record_failure()
        logger.debug("Redis GET 실패 (latest mirror)", exc_info=True, extra={"key": key})
        return None, "redis_error"


# ── latest:index control key (de)serialize ────────────────────


def serialize_index(keys: List[str], mirrored_at: datetime) -> str:
    """latest:index value를 JSON 문자열로 직렬화.

    keys는 mirror가 적재 성공한 data keys 목록 (control 정보, 카운트는 별도 필드).
    mirrored_at은 cycle freshness 대표 (모든 data key의 mirrored_at과 동일).
    """
    return json.dumps({
        "keys": keys,
        "mirrored_at": mirrored_at.isoformat(),
    }, ensure_ascii=False)


def serialize_dxy_value(rate: float, timestamp: str, source: str, mirrored_at: datetime) -> str:
    """DXY value JSON 직렬화 (PR5). source 필드 포함 (rates와 다른 schema).

    DXY는 indices.dxy.source가 'investing'/'cnbc'/'yahoo' 등 실제 데이터 출처라
    payload shape에 포함된다. rates serialize_value()는 source 미지원이라 별도 함수.
    """
    payload = {
        "rate": rate,
        "timestamp": timestamp,
        "source": source,
        "mirrored_at": mirrored_at.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False)


def deserialize_dxy_value(raw: Union[str, bytes]) -> Optional[Dict[str, Any]]:
    """DXY raw → dict. mirrored_at은 timezone-aware datetime으로 변환 (PR5).

    parse 실패 / mirrored_at naive / source 누락 → None (호출자가 redis_error 분류).
    """
    try:
        data = json.loads(raw)
        parsed_at = datetime.fromisoformat(data["mirrored_at"])
        if parsed_at.tzinfo is None:
            return None
        # source 필수 (rates와 다른 점)
        source = data["source"]
        if not isinstance(source, str):
            return None
        return {
            "rate": float(data["rate"]),
            "timestamp": data["timestamp"],
            "source": source,
            "mirrored_at": parsed_at,
        }
    except (ValueError, KeyError, TypeError):
        return None


def deserialize_index(raw: Union[str, bytes]) -> Optional[Dict[str, Any]]:
    """latest:index raw → dict. mirrored_at은 timezone-aware datetime으로 변환.

    parse 실패 / mirrored_at naive / keys 타입 비정상 / keys 원소가 str 아님
    → None (호출자가 redis_error로 분류). control key가 깨졌을 때 fetch가
    예외 path로 빠지지 않도록 명시적 실패.
    """
    try:
        data = json.loads(raw)
        parsed_at = datetime.fromisoformat(data["mirrored_at"])
        if parsed_at.tzinfo is None:
            return None
        keys = data["keys"]
        if not isinstance(keys, list):
            return None
        if not all(isinstance(k, str) for k in keys):
            return None
        return {"keys": keys, "mirrored_at": parsed_at}
    except (ValueError, KeyError, TypeError):
        return None


# ── key + value → legacy rate record 변환 ────────────────────


def _key_to_rate_record(key: str, parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """latest:* key + parsed value → rate record dict (legacy shape).

    legacy shape: {currency, bank, rate, timestamp}. source 모델은 bank 필드에
    source 이름을 넣는 기존 어댑터 패턴 (get_source_rates_as_legacy_format) 따른다.

    key 형식 인식 실패 시 None — 호출자가 redis_error로 분류.
    """
    parts = key.split(":")
    # latest:bank:{bank}:{currency}
    if len(parts) == 4 and parts[0] == "latest" and parts[1] == "bank":
        bank = parts[2]
        currency = parts[3]
    # latest:source:{source}:{asset}  (legacy adapter — source as bank, asset as currency)
    elif len(parts) == 4 and parts[0] == "latest" and parts[1] == "source":
        bank = parts[2]
        currency = parts[3]
    # latest:investing:{currency}
    elif len(parts) == 3 and parts[0] == "latest" and parts[1] == "investing":
        bank = "investing"
        currency = parts[2]
    else:
        return None
    return {
        "currency": currency,
        "bank": bank,
        "rate": parsed["rate"],
        "timestamp": parsed["timestamp"],
    }


# ── Mirror core + wrappers ────────────────────────────────────


def should_include_source_in_latest(source: str, asset: str) -> bool:
    """Legacy latest mirror exposure policy (Redis broadcast latest:* keys only).

    Mirror가 broadcast `latest:index`에 포함할 source/asset인지 결정. 이름은
    "latest"지만 Redis cache 전체 정책이 아니라 broadcast 노출 한정 — topic API
    경로(usdt:krw, fx:*)는 자체 builder 사용으로 본 함수 미경유 (영향 0).

    Z-2d Step 3 (2026-05-12): `legacy_policy.should_include_source_in_legacy_rates`
    위임. REST `/api/rates*` + WebSocket DB fallback + Redis mirror seed가 모두
    동일 allowlist 적용. cartesian product 의미: bank(9) + investing × FX(3) =
    30 조합 True, 그 외(USDT 거래소, KRX 등) False.

    Removed: `KRX_BROADCAST_INCLUDE` env/config는 Z-2d cleanup(2026-05-12)에서
    제거됨. allowlist가 단일 진실 소스 — KRX usd-krw-futures는 allowlist 미포함이라
    항상 mirror skip. Historical context는 DECISIONS.md ADR-027, KRX_CANARY.md,
    CHANGELOG.md 참조.

    Args:
        source: 데이터 공급자 (예: "kb", "investing", "upbit", "krx").
        asset: 통화쌍/상품 (예: "usd-krw", "usdt-krw", "usd-krw-futures").

    Returns:
        True — legacy 노출 대상 (mirror 포함).
        False — topic-only source 또는 미등록 (mirror skip).
    """
    return should_include_source_in_legacy_rates(source, asset)


async def _mirror_all_latest(db: Session) -> Dict[str, Any]:
    """DB latest를 Redis로 적재하는 core. warmup과 mirror_once가 공유.

    invariant (data key 기준):
        attempted_total = loaded_total + failed
        loaded_total    = bank + investing + source

    latest:index control key는 data key와 별도로 분류 (loaded_total에 포함 X).
    failed == 0일 때만 atomic하게 갱신 (부분 실패 시 이전 정상 index 유지 → fetch가
    stale로 fallback).

    DB에서 받은 record만 시도 — hardcoded expected count 없음. 운영에서 은행 추가/
    제거 또는 일부 데이터 누락 시 attempted_total이 자연스럽게 반영됨.

    Returns:
        stats dict — data key 카운트 + index_updated 별도 필드
    """
    stats: Dict[str, Any] = {
        "attempted_total": 0,
        "loaded_total": 0,
        "bank": 0,
        "investing": 0,
        "source": 0,
        "failed": 0,
        "index_updated": False,
        # PR5 — DXY는 별도 카테고리 (latest:index 분리, rates invariant 영향 없음)
        "dxy_attempted": 0,
        "dxy_loaded": 0,
        "dxy_failed": 0,
        # PR6b-2a — broadcast 노출 토글로 mirror skip된 source record 카운트.
        # invariant: source_skipped는 attempted_total에 포함 X (별도 관찰용).
        "source_skipped": 0,
    }
    mirrored_at = datetime.now(_KST)
    loaded_keys: List[str] = []  # 적재 성공 data key만 추적 (latest:index의 keys 값)

    for pair in crud.SUPPORTED_CURRENCY_PAIRS:
        # investing — 단일 source, currency당 0~1 record
        inv = crud.select_a_latest_investing_rate_from_db(db, pair)
        if inv:
            stats["attempted_total"] += 1
            key = latest_key_investing(inv["currency"])
            value = serialize_value(inv["rate"], inv["timestamp"], mirrored_at)
            if await _set_latest(key, value):
                stats["loaded_total"] += 1
                stats["investing"] += 1
                loaded_keys.append(key)
            else:
                stats["failed"] += 1

        # bank — currency당 N개 은행
        for record in crud.select_latest_bank_rates_from_db(db, pair):
            stats["attempted_total"] += 1
            key = latest_key_bank(record["bank"], record["currency"])
            value = serialize_value(record["rate"], record["timestamp"], mirrored_at)
            if await _set_latest(key, value):
                stats["loaded_total"] += 1
                stats["bank"] += 1
                loaded_keys.append(key)
            else:
                stats["failed"] += 1

    # source — 5 거래소 × 1 asset (asset=None: 전체)
    # legacy adapter shape: {currency: asset, bank: source, rate, timestamp}
    # Z-2d (2026-05-12) — should_include_source_in_latest가 legacy_policy allowlist
    # 위임. topic-only source(USDT 5거래소 + KRX usd-krw-futures)는 모두 skip.
    # Historical: PR6b-2a에서 KRX_BROADCAST_INCLUDE 토글로 KRX만 한정 차단 → Z-2d에서
    # allowlist 통일로 일반화 + env 제거.
    # invariant: skip은 attempted_total 전 단계 → attempted = loaded + failed 유지.
    for record in crud.get_source_rates_as_legacy_format(db):
        source = record["bank"]
        asset = record["currency"]
        if not should_include_source_in_latest(source, asset):
            stats["source_skipped"] += 1
            continue
        stats["attempted_total"] += 1
        key = latest_key_source(source, asset)
        value = serialize_value(record["rate"], record["timestamp"], mirrored_at)
        if await _set_latest(key, value):
            stats["loaded_total"] += 1
            stats["source"] += 1
            loaded_keys.append(key)
        else:
            stats["failed"] += 1

    # latest:index control key — atomic: failed == 0일 때만 갱신
    # 부분 실패 시 이전 정상 index 유지 → fetch가 (eventually) stale로 fallback
    if stats["failed"] == 0 and loaded_keys:
        index_value = serialize_index(loaded_keys, mirrored_at)
        if await _set_latest(LATEST_INDEX_KEY, index_value):
            stats["index_updated"] = True

    # PR5 — DXY mirror (별도 카테고리, latest:index 무관)
    # rates 적재 성공 여부와 독립적으로 시도. 실패해도 rates fallback 유발 X.
    dxy_record = crud.get_latest_dxy_rate(db)
    if dxy_record:
        stats["dxy_attempted"] += 1
        dxy_value = serialize_dxy_value(
            dxy_record["rate"],
            dxy_record["timestamp"],
            dxy_record["source"],
            mirrored_at,
        )
        if await _set_latest(LATEST_DXY_KEY, dxy_value):
            stats["dxy_loaded"] += 1
        else:
            stats["dxy_failed"] += 1

    return stats


async def warmup_latest_rates() -> Optional[Dict[str, Any]]:
    """앱 시작 시 1회 호출 — DB latest를 Redis에 모두 적재.

    FastAPI lifespan startup에서 호출. 실패해도 broadcast가 DB fallback으로 동작
    하므로 앱 시작 자체는 막지 않는다.

    data key 일부 실패(failed > 0)이거나 latest:index 갱신 실패(index_updated=False)
    이면 WARNING으로 가시화 — startup에서 INFO만 찍히면 fetch가 즉시 fallback에
    도는 상황을 운영자가 놓칠 수 있음.

    Returns:
        성공 시 stats dict (Dict[str, Any] — index_updated bool 포함),
        예외 시 None.
    """
    db = SessionLocal()
    try:
        stats = await _mirror_all_latest(db)
        if stats["failed"] > 0 or not stats["index_updated"] or stats["dxy_failed"] > 0:
            logger.warning(
                "⚠️ Redis latest mirror warmup 일부 실패 또는 index/DXY 미갱신",
                extra=stats,
            )
        else:
            logger.info("✅ Redis latest mirror warmup 완료", extra=stats)
        return stats
    except Exception:
        logger.exception("❌ Redis latest mirror warmup 실패")
        return None
    finally:
        db.close()


async def fetch_rates_from_redis() -> Tuple[
    Optional[List[Dict[str, Any]]],
    Optional[Dict[str, Any]],
    Dict[str, Any],
]:
    """broadcast Redis-first read path. latest:index 기반 rates + DXY 함께 fetch.

    main.py를 import하지 않고 (순환 회피) Redis read만 책임. rates 실패 시
    rates=None을 반환해 호출자(broadcast_rates_once)가 DB fallback을 트리거.
    DXY는 독립 fetch — DXY Redis 실패가 rates fallback을 유발하지 않음 (PR5
    DXY-only fallback). DB fallback 결과는 호출자(main._assemble_payload)가 결정.

    cycle freshness 시맨틱 (PR Z-2f / ADR-030):
    - `latest:index.mirrored_at`는 read path에서 ignore (membership list로 격하)
    - 각 data key value의 `mirrored_at`을 per-key `is_stale()` 검사
    - 1개라도 stale → `per_key_stale` reason으로 fallback (보수적 1차)
    - 이유: bank/investing direct write 시대에 index single signal이 invariant
      깨짐. 자세한 결정은 ADR-030 참조.

    PR3.5 분해 계측: 성공/실패 모두 meta에 단계별 timing 포함 — 어디서 시간이
    소요되거나 실패했는지(index_get / parse / data_get / decode) 식별 가능.

    Returns:
        (rates, redis_dxy, meta)
        - rates: rates list 또는 None (rates fallback 시)
        - redis_dxy: Redis DXY dict (성공) 또는 None (Redis 실패 — DB fallback은 호출자가)
        - meta:
            * latest_source = 'redis' or 'db_fallback' (rates 기준)
            * fallback_reason (rates Redis 실패 시) — rates 전용
            * latest_dxy_get_ms (DXY Redis GET 시도 시간)
            * latest_dxy_fallback_reason (DXY Redis 실패 시) — DXY 전용
            * 단계별 timing (index/parse/data_get/decode/fetch_total/key_count/mirror_age)
    """
    fetch_t0 = time.perf_counter()
    meta: Dict[str, Any] = {}

    # 1. latest:index 읽기
    t_index_get0 = time.perf_counter()
    raw_index, reason = await _get_latest_with_reason(LATEST_INDEX_KEY)
    meta["latest_index_get_ms"] = (time.perf_counter() - t_index_get0) * 1000
    if reason is not None:
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = reason
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta

    # 2. index parse (mirrored_at tz-aware 검증 포함)
    t_parse0 = time.perf_counter()
    index = deserialize_index(raw_index)
    meta["latest_index_parse_ms"] = (time.perf_counter() - t_parse0) * 1000
    if index is None:
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = "redis_error"
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta

    # 3. (PR Z-2f / ADR-030) index.mirrored_at stale gate 제거
    #    freshness 판정은 step 5 decode loop에서 per-key 수행
    #    `index["mirrored_at"]`은 mirror_age_ms meta(step 6)에만 사용 — 정보용

    # 4. listed data keys MGET → 1 round-trip + 1 await yield (PR4)
    # PR3.5 측정에서 35 sequential async GET wall-clock이 long tail 주범으로 식별됨
    # (per-key 평균 0.4ms → spike 시 17ms+, :16초 96.4% 집중 — mirror cycle interleaving).
    # MGET 1회로 round-trip 35→1, await 지점 35→1, mirror SET 끼어들 기회 35→1.
    # _get_latest_with_reason()이 해주던 client/circuit 체크는 PR4에서 직접 처리.
    t_data_get0 = time.perf_counter()
    keys = index["keys"]

    if not redis_cache.client:
        meta["latest_data_get_ms"] = (time.perf_counter() - t_data_get0) * 1000
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = "redis_error"
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta
    if not await redis_cache.circuit.can_attempt():
        meta["latest_data_get_ms"] = (time.perf_counter() - t_data_get0) * 1000
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = "circuit_open"
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta

    try:
        raw_values = await redis_cache.client.mget(keys)
        await redis_cache.circuit.record_success()
    except Exception:
        await redis_cache.circuit.record_failure()
        meta["latest_data_get_ms"] = (time.perf_counter() - t_data_get0) * 1000
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = "redis_error"
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta

    # mget은 keys와 같은 길이 list 반환해야 정상 (Redis spec)
    if len(raw_values) != len(keys):
        meta["latest_data_get_ms"] = (time.perf_counter() - t_data_get0) * 1000
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = "redis_error"
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta

    # 일부 None — 정책 유지: 1건이라도 누락 → 전체 fallback (PR3 stale와 일관)
    if any(v is None for v in raw_values):
        meta["latest_data_get_ms"] = (time.perf_counter() - t_data_get0) * 1000
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = "redis_miss"
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta

    raw_pairs: List[Tuple[str, Any]] = list(zip(keys, raw_values))
    meta["latest_data_get_ms"] = (time.perf_counter() - t_data_get0) * 1000
    meta["latest_data_get_mode"] = "mget"  # PR4 indicator (analyzer numeric 미포함)

    # 5. decode + key→rate 변환 + per-key stale 검사 (PR Z-2f / ADR-030)
    t_decode0 = time.perf_counter()
    rates: List[Dict[str, Any]] = []
    for key, raw_v in raw_pairs:
        parsed = deserialize_value(raw_v)
        if parsed is None:
            meta["latest_decode_ms"] = (time.perf_counter() - t_decode0) * 1000
            meta["latest_source"] = "db_fallback"
            meta["fallback_reason"] = "redis_error"
            meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
            return None, None, meta
        # PR Z-2f: 개별 data key value의 mirrored_at으로 freshness 판정
        # 1개라도 stale → 전체 fallback (보수적 1차, ADR-030)
        if is_stale(parsed["mirrored_at"]):
            meta["latest_decode_ms"] = (time.perf_counter() - t_decode0) * 1000
            meta["latest_source"] = "db_fallback"
            meta["fallback_reason"] = "per_key_stale"
            meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
            return None, None, meta
        rate_record = _key_to_rate_record(key, parsed)
        if rate_record is None:
            meta["latest_decode_ms"] = (time.perf_counter() - t_decode0) * 1000
            meta["latest_source"] = "db_fallback"
            meta["fallback_reason"] = "redis_error"
            meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
            return None, None, meta
        rates.append(rate_record)
    meta["latest_decode_ms"] = (time.perf_counter() - t_decode0) * 1000

    # 6. rates 성공 메타 — mirror_age_ms (cycle freshness) + key_count
    mirror_age_ms = int((datetime.now(_KST) - index["mirrored_at"]).total_seconds() * 1000)
    meta["latest_source"] = "redis"
    meta["mirror_age_ms"] = mirror_age_ms
    meta["latest_key_count"] = len(rates)

    # 7. DXY Redis fetch (PR5) — rates와 독립. DXY 실패는 rates fallback 안 만듦.
    # DB fallback 시도는 호출자(_assemble_payload_from_rates)가 결정. fetch는 Redis만 책임.
    redis_dxy: Optional[Dict[str, Any]] = None
    t_dxy_get0 = time.perf_counter()
    raw_dxy, dxy_reason = await _get_latest_with_reason(LATEST_DXY_KEY)
    meta["latest_dxy_get_ms"] = (time.perf_counter() - t_dxy_get0) * 1000
    if dxy_reason is not None:
        meta["latest_dxy_fallback_reason"] = dxy_reason
    else:
        parsed_dxy = deserialize_dxy_value(raw_dxy)
        if parsed_dxy is None:
            meta["latest_dxy_fallback_reason"] = "redis_error"
        elif is_stale(parsed_dxy["mirrored_at"]):
            meta["latest_dxy_fallback_reason"] = "redis_stale"
        else:
            redis_dxy = {
                "rate": parsed_dxy["rate"],
                "timestamp": parsed_dxy["timestamp"],
                "source": parsed_dxy["source"],
            }

    meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
    return rates, redis_dxy, meta


async def mirror_latest_rates_once() -> Optional[Dict[str, Any]]:
    """Scheduler IntervalTrigger 주기 호출 — DB latest를 Redis로 갱신.

    매 LATEST_MIRROR_INTERVAL_SECONDS마다 호출. 매 주기 INFO 폭증 회피를 위해
    정상 시 DEBUG, data key 실패(failed > 0) 또는 latest:index 미갱신
    (index_updated=False)이면 WARNING으로 가시화. data 모두 성공인데 index만
    실패한 케이스도 fetch가 fallback에 빠지므로 동일 신호로 다룬다.

    Returns:
        성공 시 stats dict (Dict[str, Any] — index_updated bool 포함),
        예외 시 None.
    """
    db = SessionLocal()
    try:
        stats = await _mirror_all_latest(db)
        if stats["failed"] > 0 or not stats["index_updated"] or stats["dxy_failed"] > 0:
            logger.warning(
                "⚠️ Redis latest mirror 일부 실패 또는 index/DXY 미갱신",
                extra=stats,
            )
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
    "LATEST_INDEX_KEY",
    "LATEST_DXY_KEY",
    "latest_key_bank",
    "latest_key_source",
    "latest_key_investing",
    "serialize_value",
    "deserialize_value",
    "serialize_index",
    "deserialize_index",
    "serialize_dxy_value",
    "deserialize_dxy_value",
    "is_stale",
    "fetch_rates_from_redis",
    "warmup_latest_rates",
    "mirror_latest_rates_once",
]
