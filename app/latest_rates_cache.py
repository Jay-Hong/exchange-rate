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
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy.orm import Session

from app import config, crud
from app.cache import redis_cache
from app.config import LATEST_MIRROR_INTERVAL_SECONDS
from app.database import SessionLocal

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
    """Mirror가 broadcast latest:index에 포함할 source/asset인지 결정 (PR6b-2a).

    PR6 KRX 미국달러선물의 broadcast 노출 토글 — KRX_BROADCAST_INCLUDE
    default false. KRX usd-krw-futures는 토글 false 시 mirror skip
    → broadcast rates 배열에 등장 X (앱 호환성 검증 전 안전 차단).

    scope: source="krx" + asset="usd-krw-futures" 한정. KRX의 다른
    asset이나 다른 source는 토글 무관 (기존 mirror 동작 유지).

    Returns:
        True — mirror 포함 (default 동작)
        False — KRX usd-krw-futures이고 토글 false인 경우만
    """
    if source == "krx" and asset == "usd-krw-futures":
        return config.KRX_BROADCAST_INCLUDE
    return True


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
    # PR6b-2a — KRX usd-krw-futures는 KRX_BROADCAST_INCLUDE=true일 때만 mirror.
    # default false → broadcast/app 영향 0 (앱 호환성 검증 후 토글 ON).
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

    cycle freshness는 latest:index.mirrored_at 단일 source로 판단 (모든 data key가
    같은 cycle에서 적재되므로 개별 mirrored_at 재검증 불필요).

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

    # 3. cycle freshness 판정 (single source of truth)
    if is_stale(index["mirrored_at"]):
        meta["latest_source"] = "db_fallback"
        meta["fallback_reason"] = "redis_stale"
        meta["latest_fetch_total_ms"] = (time.perf_counter() - fetch_t0) * 1000
        return None, None, meta

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

    # 5. decode + key→rate 변환
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
