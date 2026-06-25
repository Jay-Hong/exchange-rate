"""테더 탭 topic payload builder (PR Z-2b Stage 3 준비).

USDT_TOPIC_MIGRATION_PLAN §2 / ADR-028 합의된 topic-only Tether/KRX 출시 계약을
위한 순수 builder. DB 의존 X — 호출자가 fetch한 데이터를 입력으로 받음.

설계 원칙:
    1. **topic-agnostic**: payload에 topic 필드 미생성. publish 호출자가
       `await publish_topic("usdt:krw", payload)` 형태로 topic 이름 결정.
    2. **legacy shape 호환 입력**: `bank/currency` 키 (legacy)와 `source/asset` 키
       (topic-native) 둘 다 받아서 정규화. 기존 crud 함수의 반환 shape를 그대로
       전달 가능.
    3. **topic-native 출력**: 모든 항목은 `source/asset/rate/timestamp` shape.
       legacy `bank/currency` 키는 출력에서 제거. `display_name`은 서버 미전송
       (2026-05-11 제거) — 단말이 (source, asset) tuple로 자체 registry lookup.
    4. **builder 내부 정렬**: 호출자가 정렬 안 해도 builder가 보장.
       - list 그룹 (usdt_krw, usd_krw_banks): SourceRegistry sort_order +
         BANK_DISPLAY_ORDER fallback, 미등록은 뒤로 + 코드순.
       - singleton 그룹 (usd_krw_reference, usd_krw_futures): expected
         source/asset만 허용, 아니면 키 누락 (schema 무결성 보호).
    5. **None optional 키 누락**: investing_rate / krx_futures_rate가 None이면
       해당 그룹 키 자체 누락 (forward-compat — 클라이언트는 없는 키 무시).
    6. **env flag 직접 해석 X**: KRX 포함 여부는 호출자(wire-up)가 결정.
       Builder는 받은 그대로 처리.

Schema (version=1):
    {
      "type": "snapshot",
      "version": 1,
      "data": {
        "usdt_krw": [{"source", "asset", "rate", "timestamp"}, ...],
        "usd_krw_banks": [...],
        "usd_krw_reference": {...},     # source="investing" + asset="usd-krw" only
        "usd_krw_futures": {...}        # source="krx" + asset="usd-krw-futures" only (optional)
      }
    }

    Entry 식별자는 (source, asset) tuple. 단말은 자체 registry로 표시명/아이콘/
    색상/정렬 결정 (display_name 서버 미전송, 2026-05-11 제거).

참조:
    - REALTIME_ARCHITECTURE_PLAN.md §5 (topic 채널 분리)
    - USDT_TOPIC_MIGRATION_PLAN.md §2 (Target Contract)
    - DECISIONS.md ADR-028 (Topic-only Tether/KRX + dual-emit FX)
    - USDT_PHASE1_DESIGN.md (SourceRegistry / source 모델)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from app.crud import (
    _bank_display_sort_key,
    get_latest_source_rate,
    get_latest_source_rates_for_topic,
    select_a_latest_investing_rate_from_db,
    select_latest_bank_rates_from_db,
)
from app.latest_rates_cache import (
    get_latest_bank_rate_from_sync_job,
    get_latest_investing_rate_from_sync_job,
    get_latest_krx_rate_from_sync_job,
    get_latest_usdt_rate_from_sync_job,
)
from app.source_registry import get_source_definition, get_usdt_exchange_entries

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# singleton 슬롯에 허용되는 expected (source, asset)
_REFERENCE_EXPECTED: Tuple[str, str] = ("investing", "usd-krw")
_FUTURES_EXPECTED: Tuple[str, str] = ("krx", "usd-krw-futures")

# 테더 탭에 표시할 은행 source list (kb→hana 순서). select_latest_bank_rates_from_db
# 가 9개 은행을 반환하면 이 상수 순서로만 필터/build → helper 출력 순서 안정.
TETHER_TAB_BANK_SOURCES: Tuple[str, ...] = ("kb", "hana")

# 테더 탭에 표시할 USDT 거래소 source list (PR Z-2e B-Step 2).
# Redis-first read의 source 인자 + DB fallback 호출 시 동일 list 사용 → 출력
# 순서 안정 + Redis miss 판정 단위와 DB fallback 단위 일관. SourceRegistry의
# get_usdt_exchange_entries()와 정합(단위 test로 검증) — 명시 상수가 단말
# 출력 순서 계약 단일 진실 소스.
TETHER_TAB_EXCHANGE_SOURCES: Tuple[str, ...] = (
    "upbit", "bithumb", "coinone", "korbit", "gopax",
)


def _normalize_entry(
    raw: Dict[str, Any],
    *,
    fallback_source: Optional[str] = None,
    fallback_asset: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """입력 dict를 topic-native shape로 정규화.

    Args:
        raw: 입력 dict. legacy shape ({"bank", "currency", "rate", "timestamp"}) 또는
             topic-native ({"source", "asset", "rate", "timestamp"}) 모두 허용.
        fallback_source: source 키와 bank 키 둘 다 없을 때 사용 (예: investing
                         singleton에서 호출자가 단순 {"rate", "timestamp"}만 넘긴 경우).
        fallback_asset: asset/currency 키 둘 다 없을 때 사용.

    Returns:
        {"source", "asset", "rate", "timestamp"} 또는 rate/timestamp 부재 시 None.

    Note (display_name 제거, 2026-05-11):
        Entry 식별자는 (source, asset) tuple. 서버 payload는 표시명/아이콘/색상을
        보내지 않음. 단말은 (source, asset)로 자체 registry를 lookup해 표시명,
        짧은 이름, 아이콘, 색상, 정렬을 결정 (iOS Constants.swift Bank enum,
        Android Bank.kt 패턴). i18n 정책 + payload size + single source of truth
        (client) 측면에서 단말 자체 registry가 정합.
    """
    source = raw.get("source") or raw.get("bank") or fallback_source
    asset = raw.get("asset") or raw.get("currency") or fallback_asset
    rate = raw.get("rate")
    timestamp = raw.get("timestamp")

    if source is None or asset is None or rate is None or timestamp is None:
        return None

    return {
        "source": source,
        "asset": asset,
        "rate": rate,
        "timestamp": timestamp,
    }


def _list_sort_key(entry: Dict[str, Any]) -> Tuple[int, int, str]:
    """list 그룹 정렬 key (asset별 정책 분리, Codex 2회차 정정).

    정책 (이중 정렬 체계 혼합 차단):
        - asset="usd-krw" (은행 그룹): BANK_DISPLAY_ORDER만. SourceRegistry는
          정렬에 관여 X (단말이 자체 registry로 표시명 lookup, 서버는 미사용).
        - asset != "usd-krw" (USDT 거래소/derivative 등): SourceRegistry sort_order
          + 미등록 fallback.

    Why 분리: kb/hana는 SourceRegistry+BANK_DISPLAY_ORDER 양쪽 등록되어 두 시스템
    상대순서가 마침 일치하지만 미래 SourceRegistry sort_order 변경 시 BANK_DISPLAY_ORDER
    와 충돌 가능. 은행 정렬은 BANK_DISPLAY_ORDER 단일 source로 잠금.
    """
    source = entry["source"]
    asset = entry["asset"]

    if asset == "usd-krw":
        # 은행 그룹 — BANK_DISPLAY_ORDER만 사용
        bank_key = _bank_display_sort_key(source)
        return (0, bank_key[0], bank_key[1])

    # USDT/derivative 그룹 — SourceRegistry sort_order
    definition = get_source_definition(source, asset)
    if definition is not None:
        return (1, definition.sort_order, source)

    # 미등록: 가장 뒤 + 코드순
    return (2, 0, source)


def build_tether_tab_payload(
    *,
    usdt_rates: List[Dict[str, Any]],
    bank_rates: List[Dict[str, Any]],
    investing_rate: Optional[Dict[str, Any]] = None,
    krx_futures_rate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """테더 탭 snapshot payload 구성 (순수 함수, DB 의존 X).

    Args:
        usdt_rates: USDT/KRW 5거래소 list. legacy 또는 topic-native shape 허용.
        bank_rates: USD/KRW 은행 list (테더 탭은 kb, hana). legacy/topic-native 허용.
        investing_rate: USD/KRW Investing reference. legacy shape일 가능성 높음
            (`{"bank": "investing", "currency": "usd-krw", ...}`). expected source/asset
            아니면 키 누락.
        krx_futures_rate: KRX 미국달러선물. expected source="krx" + asset="usd-krw-futures"
            만 허용. None이면 키 누락. 호출자가 KRX 포함 여부 결정 (env flag 게이트는
            호출자 책임).

    Returns:
        topic payload (topic 필드 없음 — wire-up 시점에 wrapper가 결정).

    Builder 책임:
        - bank → source / currency → asset 정규화 (display_name 미생성 —
          단말이 자체 registry로 표시명 lookup, 2026-05-11 제거)
        - list 그룹 정렬 (SourceRegistry sort_order + BANK_DISPLAY_ORDER fallback)
        - singleton 그룹은 expected source/asset만 허용 + None은 키 누락
        - rate/timestamp 부재 entry는 무시 (drop)
    """
    # USDT 거래소 list — legacy 가능성 낮지만 호환 유지
    usdt_normalized: List[Dict[str, Any]] = []
    for raw in usdt_rates:
        normalized = _normalize_entry(raw, fallback_asset="usdt-krw")
        if normalized is not None:
            usdt_normalized.append(normalized)
    usdt_normalized.sort(key=_list_sort_key)

    # 은행 list — legacy shape (`bank` 키) 가능성 높음
    bank_normalized: List[Dict[str, Any]] = []
    for raw in bank_rates:
        normalized = _normalize_entry(raw, fallback_asset="usd-krw")
        if normalized is not None:
            bank_normalized.append(normalized)
    bank_normalized.sort(key=_list_sort_key)

    data: Dict[str, Any] = {
        "usdt_krw": usdt_normalized,
        "usd_krw_banks": bank_normalized,
    }

    # Investing reference — singleton, expected source="investing" + asset="usd-krw"
    if investing_rate is not None:
        normalized = _normalize_entry(
            investing_rate,
            fallback_source="investing",
            fallback_asset="usd-krw",
        )
        if normalized is not None and (
            normalized["source"],
            normalized["asset"],
        ) == _REFERENCE_EXPECTED:
            data["usd_krw_reference"] = normalized
        # 그 외: 키 누락 (schema 무결성 보호)

    # KRX futures — singleton, expected source="krx" + asset="usd-krw-futures"
    if krx_futures_rate is not None:
        normalized = _normalize_entry(
            krx_futures_rate,
            fallback_source="krx",
            fallback_asset="usd-krw-futures",
        )
        if normalized is not None and (
            normalized["source"],
            normalized["asset"],
        ) == _FUTURES_EXPECTED:
            data["usd_krw_futures"] = normalized
        # 그 외: 키 누락

    return {
        "type": "snapshot",
        "version": 1,
        "data": data,
    }


def load_and_build_tether_tab_payload(
    db: "Session",
    *,
    include_krx: bool = False,
) -> Dict[str, Any]:
    """DB 통합 helper — 저장소별 dispatch 후 build_tether_tab_payload 호출.

    저장소별 조회:
        - USDT 5거래소 (asset=usdt-krw): **Redis-first** (PR Z-2e B-Step 2).
          TETHER_TAB_EXCHANGE_SOURCES 순서로 sync Redis GET (latest:source:*:usdt-krw).
          5개 모두 hit이면 Redis 사용. 1개라도 miss/parse fail이면 전체
          DB fallback(`get_latest_source_rates_for_topic`, legacy_policy 우회).
          Stale 판정 X (USDT는 mirror cycle 미경유 — Z-2d allowlist).
        - 은행 (asset=usd-krw): **Redis-first** (PR Z-2e Step 3a). 각 bank별
          `get_latest_bank_rate_from_sync_job` 호출 → hit이면 사용, miss/stale이면
          그 source만 DB fallback (per-source — USDT의 group fallback과 다름,
          banks는 각 cron 독립). DB는 lazy 조회 (Redis 1+ miss 시점에 1회).
        - Investing (asset=usd-krw): **Redis-first** (Step 3a). 단일 fallback —
          Redis hit이면 사용, miss/stale이면 DB fallback.
        - bank/investing은 mirror cycle 갱신 가정 → `is_stale()` 적용 (interval×2 기준; 운영 120s).
          USDT(mirror skip)와 다른 환경 (ADR-026 vs ADR-029).
        - KRX (asset=usd-krw-futures): include_krx=True일 때만 query.
          False면 호출 자체 X (불필요 DB load 차단).
          **KRX는 mirror skip + direct write 미구축 → DB query 유지** (별도 phase).

    Args:
        db: SQLAlchemy Session.
        include_krx: KRX 미국달러선물 포함 여부. 기본 False — KRX는 optional canary
            라 호출자가 명시적으로 True 줄 때만 query + payload 포함. env flag
            (KRX_FUTURES_ENABLED 등) 해석은 helper 외부 wire-up 호출자 책임.

    Returns:
        build_tether_tab_payload 결과 (topic 필드 없음 — wire-up 호출자가 결정).

    책임 경계:
        - helper는 env flag 직접 해석 X
        - publish 호출 X (wire-up은 별도 PR)
        - 정렬/정규화는 build_tether_tab_payload가 단일 책임
    """
    # USDT — Redis-first read (PR Z-2e B-Step 2).
    # 5거래소 latest:source:*:usdt-krw key 시도 → 모두 hit이면 Redis 사용,
    # 1개라도 miss/parse fail이면 전체 DB fallback (`get_latest_source_rates_for_topic`
    # — Z-2d legacy_policy 우회 topic 전용 fetcher).
    # Stale 판정 X — USDT는 mirror cycle 미경유 (Z-2d allowlist), Redis 있으면
    # 시간 무관 사용. miss 처리만 DB fallback.
    redis_usdt_results = [
        get_latest_usdt_rate_from_sync_job(source, "usdt-krw")
        for source in TETHER_TAB_EXCHANGE_SOURCES
    ]
    if all(r is not None for r in redis_usdt_results):
        # 모두 Redis hit — 그대로 사용 (topic-native shape)
        usdt_rates: List[Dict[str, Any]] = list(redis_usdt_results)  # type: ignore[arg-type]
    else:
        # 1개라도 miss → 전체 DB fallback (topic 전용 fetcher, legacy_policy 우회)
        # PR Z-2e B-Step Telemetry: fallback 호출 카운트 (ADR-029 trade-off 모니터링)
        from app import usdt_redis_stats
        usdt_redis_stats.record_db_fallback("usdt-krw")
        usdt_rates = get_latest_source_rates_for_topic(
            db, asset="usdt-krw", sources=list(TETHER_TAB_EXCHANGE_SOURCES),
        )

    # 은행 — Redis-first read (PR Z-2e Step 3a, per-source fallback)
    # bank/investing은 mirror cycle 갱신 (Z-2d allowlist 통과) → is_stale 적용 가능.
    # KB Redis hit + Hana miss/stale이면 Hana만 DB fallback (USDT의 group fallback과
    # 다름 — banks는 각 cron 독립이라 per-source가 자연).
    bank_rates: List[Dict[str, Any]] = []
    _db_banks_loaded: Optional[Dict[str, Dict[str, Any]]] = None
    for bank_source in TETHER_TAB_BANK_SOURCES:
        redis_entry = get_latest_bank_rate_from_sync_job(bank_source, "usd-krw")
        if redis_entry is not None:
            bank_rates.append(redis_entry)
            continue
        # Redis miss/stale → 해당 source만 DB fallback. lazy DB 조회 (필요 시 1회).
        if _db_banks_loaded is None:
            all_banks_db = select_latest_bank_rates_from_db(db, "usd-krw")
            _db_banks_loaded = {row["bank"]: row for row in all_banks_db}
        db_row = _db_banks_loaded.get(bank_source)
        if db_row is not None:
            bank_rates.append(db_row)

    # Investing reference — Redis-first read (단일 fallback)
    investing_rate = get_latest_investing_rate_from_sync_job("usd-krw")
    if investing_rate is None:
        investing_rate = select_a_latest_investing_rate_from_db(db, "usd-krw")

    # KRX — include_krx=True일 때만 query (False면 호출 0회).
    # ADR-031: KRX Redis-first read + DB fallback. stale 가드 없음 (ADR-027 영역).
    krx_futures_rate: Optional[Dict[str, Any]] = None
    if include_krx:
        krx_futures_rate = get_latest_krx_rate_from_sync_job("usd-krw-futures")
        if krx_futures_rate is None:
            krx_futures_rate = get_latest_source_rate(db, "krx", "usd-krw-futures")

    return build_tether_tab_payload(
        usdt_rates=usdt_rates,
        bank_rates=bank_rates,
        investing_rate=investing_rate,
        krx_futures_rate=krx_futures_rate,
    )
