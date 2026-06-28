"""FX topic payload builder (PR Z-2c).

USDT_PHASE1_CLIENT_GUIDE.md "FX topic schema" 합의된 topic-only FX 출시 계약을
위한 순수 builder. DB 의존은 load_and_build_fx_topic_payload에만 한정.

설계 원칙 (usdt_topic_payload.py 패턴 복제):
    1. **topic-agnostic**: payload에 topic 필드 미생성. publisher가
       `payload["topic"] = f"fx:{asset}"` 형태로 주입.
    2. **legacy shape 호환 입력**: `bank/currency` 키 (legacy)와 `source/asset` 키
       (topic-native) 둘 다 받아서 정규화 (`_normalize_entry`).
    3. **topic-native 출력**: 모든 entry는 `{source, asset, rate, timestamp}` shape.
       legacy 키는 출력에서 제거. `display_name` 미전송 (단말이 자체 registry).
    4. **builder 내부 정렬 + whitelist**: bank entry는 `BANK_DISPLAY_ORDER` 명시
       build. `select_latest_bank_rates_from_db` 반환 순서/내용 변경에 격리.
       미등록 은행은 자동 제외 (단말 enum과 동기화 boundary 강제).
    5. **reference Optional**: investing reference None 또는 (source, asset) mismatch
       시 `data["reference"]` key 자체 누락. payload publish 자체는 계속.
    6. **single-asset topic**: data key에 asset prefix 미사용 (`banks`/`reference`).
       `usdt:krw`가 multi-asset 번들(`usd_krw_banks`, `usd_krw_reference` 등)인
       반면 `fx:<asset>`는 topic 하나가 단일 asset 표현.
    7. **asset whitelist fail-fast**: 두 entry point(builder + DB loader) 모두
       `_validate_fx_asset`로 invalid asset 즉시 ValueError. CLAUDE.md "Crash
       Early" 원칙.

Schema (version=1):
    {
      "type": "snapshot",
      "version": 1,
      "data": {
        "banks": [{"source", "asset", "rate", "timestamp"}, ...],
        "reference": {"source", "asset", "rate", "timestamp"}     # Optional
      }
    }

    Entry 식별자는 (source, asset) tuple. 단말은 자체 registry로 표시명/아이콘 결정.

참조:
    - USDT_PHASE1_CLIENT_GUIDE.md "FX topic schema (Phase Z-2c)" 섹션
    - USDT_TOPIC_MIGRATION_PLAN.md Z-2c
    - DECISIONS.md ADR-028 (Topic-only Tether/KRX + dual-emit FX)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from app.crud import (
    BANK_DISPLAY_ORDER,
    select_a_latest_investing_rate_from_db,
    select_latest_bank_rates_from_db,
)
from app.latest_rates_cache import (
    get_latest_bank_rate_from_sync_job,
    get_latest_investing_rate_from_sync_job,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# FX topic이 지원하는 asset whitelist. publisher의 FX_TOPICS 매핑도 이 상수에서
# 파생. 외부 호출자가 잘못된 asset (예: usdt-krw, usd-krw-futures, typo) 시 즉시 차단.
FX_TOPIC_ASSETS: Tuple[str, ...] = ("usd-krw", "jpy-krw", "eur-krw")

# 신규 앱 public fx:* 토픽 표시용 은행 순서 — legacy 9은행에서 Citi 제외(8).
# 근거: ADR-033 / GRAPH_API_V2_CONTRACT (Citi는 모든 v2 tab/period 미노출, 수집은 유지).
# ⚠️ public topic publish/snapshot 경로에서만 builder에 bank_order로 전달한다.
# BANK_DISPLAY_ORDER(9) / fx_membership / atomic 계열은 불변 — builder 기본값 9 유지(아래).
# (legacy 구앱·atomic 9-bank invariant 보존, <10명 신앱 전환 완료 후 legacy 일괄 정리 예정.)
FX_TOPIC_BANK_ORDER: Tuple[str, ...] = tuple(b for b in BANK_DISPLAY_ORDER if b != "citi")

_REFERENCE_SOURCE: str = "investing"


def _validate_fx_asset(asset: str) -> None:
    """FX_TOPIC_ASSETS whitelist 검증. invalid면 ValueError."""
    if asset not in FX_TOPIC_ASSETS:
        raise ValueError(
            f"FX topic asset '{asset}' is not supported. "
            f"Expected one of {FX_TOPIC_ASSETS}"
        )


def _normalize_entry(
    raw: Dict[str, Any],
    *,
    fallback_source: Optional[str] = None,
    fallback_asset: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """입력 dict를 topic-native shape로 정규화.

    Args:
        raw: legacy ({"bank", "currency", "rate", "timestamp"}) 또는
             topic-native ({"source", "asset", "rate", "timestamp"}) 둘 다 허용.
        fallback_source: source/bank 둘 다 없을 때 사용.
        fallback_asset: asset/currency 둘 다 없을 때 사용.

    Returns:
        topic-native dict 또는 rate/timestamp 부재 시 None.

    Note: Step 1에서는 usdt_topic_payload._normalize_entry와 복제 유지. 공통화는
    두 builder 안정화 후 별도 PR에서 검토 (premature abstraction 회피).
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


def build_fx_tab_payload(
    asset: str,
    bank_rates: Iterable[Dict[str, Any]],
    reference: Optional[Dict[str, Any]],
    bank_order: Iterable[str] = BANK_DISPLAY_ORDER,
) -> Dict[str, Any]:
    """FX topic payload 생성 (DB 의존 X — pure builder).

    Args:
        asset: FX_TOPIC_ASSETS 중 하나. 외 ValueError.
        bank_rates: 은행 환율 raw dict iterable (legacy/topic-native 모두 허용).
            정렬은 builder가 BANK_DISPLAY_ORDER 기준으로 재정렬.
        reference: Investing reference raw dict 또는 None.

    Returns:
        Schema v1 payload (topic 필드 없음 — publisher가 inject).

    정책:
        - banks: BANK_DISPLAY_ORDER whitelist + 명시 build. 미등록 source 제외.
                 asset mismatch 자동 제외 (DB filter + normalize fallback_asset).
        - reference: None 또는 (source="investing", asset=<asset>) mismatch 시
                     `data["reference"]` key 자체 누락. publish는 계속.

    Raises:
        ValueError: asset이 FX_TOPIC_ASSETS 외.
    """
    _validate_fx_asset(asset)

    # bank build — BANK_DISPLAY_ORDER 명시 build (DB 정렬에 의존 X)
    by_source: Dict[str, Dict[str, Any]] = {}
    for raw in bank_rates:
        source = raw.get("source") or raw.get("bank")
        if source is None:
            continue
        # 같은 source 중복 입력 시 첫 번째 유지 (DB query는 source당 1건 보장)
        by_source.setdefault(source, raw)

    bank_normalized: List[Dict[str, Any]] = []
    for source in bank_order:
        if source not in by_source:
            continue
        normalized = _normalize_entry(
            by_source[source],
            fallback_source=source,
            fallback_asset=asset,
        )
        if normalized is None:
            continue
        # asset mismatch 방어 (normalize에서 fallback_asset 적용 후에도 raw에 다른
        # asset이 명시되어 있으면 entry.asset이 그 값을 갖게 됨 → 제외)
        if normalized["asset"] != asset:
            continue
        bank_normalized.append(normalized)

    data: Dict[str, Any] = {"banks": bank_normalized}

    # reference build — Optional, (source, asset) 정확 일치 시만 포함
    if reference is not None:
        ref_normalized = _normalize_entry(
            reference,
            fallback_source=_REFERENCE_SOURCE,
            fallback_asset=asset,
        )
        if ref_normalized is not None and (
            ref_normalized["source"],
            ref_normalized["asset"],
        ) == (_REFERENCE_SOURCE, asset):
            data["reference"] = ref_normalized
        # 그 외: key 누락 (schema 무결성 보호)

    return {
        "type": "snapshot",
        "version": 1,
        "data": data,
    }


def load_and_build_fx_topic_payload(
    db: "Session", asset: str, bank_order: Iterable[str] = BANK_DISPLAY_ORDER
) -> Dict[str, Any]:
    """DB/Redis에서 FX 데이터 load 후 build_fx_tab_payload 호출 (PR Z-2e Step 3c).

    Redis-first read 정책 (Step 3a와 동일 패턴):
    - banks: 9개 BANK_DISPLAY_ORDER 순회 — 각 bank별 sync Redis read 시도.
      hit이면 사용, miss/stale이면 그 source만 DB fallback (per-source, lazy 1회).
    - reference: Investing sync Redis read — hit이면 사용, miss/stale이면 DB fallback.
    - mirror cycle 갱신 가정 (Z-2d allowlist 통과) → is_stale 적용 (helper 내부).

    Args:
        db: SQLAlchemy session.
        asset: FX_TOPIC_ASSETS 중 하나. 외 ValueError.

    Returns:
        Schema v1 payload (topic 필드 없음 — publisher가 inject).

    Raises:
        ValueError: asset이 FX_TOPIC_ASSETS 외.
    """
    _validate_fx_asset(asset)

    # bank_order는 아래 load loop + build_fx_tab_payload(끝)에서 2회 순회된다. 타입 계약이
    # Iterable[str]라 one-shot generator가 들어오면 2번째 순회가 비어 silent 0-bank가 됨 →
    # tuple로 고정해 advertised Iterable 계약을 안전하게 보장 (현 caller는 tuple/list라 무변화).
    bank_order = tuple(bank_order)

    # Banks Redis-first per-source fallback (lazy DB load)
    bank_rates: List[Dict[str, Any]] = []
    _db_banks_loaded: Optional[Dict[str, Dict[str, Any]]] = None
    for bank_source in bank_order:
        redis_entry = get_latest_bank_rate_from_sync_job(bank_source, asset)
        if redis_entry is not None:
            bank_rates.append(redis_entry)
            continue
        # Redis miss/stale → DB fallback (lazy — 첫 miss 시 한 번)
        if _db_banks_loaded is None:
            all_banks_db = select_latest_bank_rates_from_db(db, asset)
            _db_banks_loaded = {row["bank"]: row for row in all_banks_db}
        db_row = _db_banks_loaded.get(bank_source)
        if db_row is not None:
            bank_rates.append(db_row)

    # Investing reference — Redis-first 단일 fallback
    reference = get_latest_investing_rate_from_sync_job(asset)
    if reference is None:
        reference = select_a_latest_investing_rate_from_db(db, asset)

    return build_fx_tab_payload(asset, bank_rates, reference, bank_order=bank_order)
