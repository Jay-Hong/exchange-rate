"""Legacy exposure policy (PR Z-2d Step 1).

REST `/api/rates*` + WebSocket broadcast (Redis fast path + DB fallback) +
Redis latest mirror seed가 모두 따르는 단일 정책. topic-only source(USDT
거래소, KRX 미국달러선물 등)는 legacy 경로 노출에서 제외하고, topic API는
영향 받지 않는다.

설계 원칙:
    1. **단일 진실 소스**: legacy 노출 여부 결정을 한 함수로 통일.
       `crud.get_source_rates_as_legacy_format` 내부 filter / Redis mirror
       seed (`latest_rates_cache.should_include_source_in_latest`) / REST
       endpoint 정책이 모두 이 함수 호출.
    2. **Allowlist + (source, asset) AND**: source-only allowlist는
       `(investing, usdt-krw)` 같은 이상 조합 통과 위험. (source, asset) 두
       set의 AND 검증으로 fail-safe.
    3. **Tuple public + internal set 캐시**: 외부 import는 tuple (순서 안정,
       문서/테스트 deterministic, BANK_DISPLAY_ORDER와 일관). lookup은
       module-private set으로 O(1).
    4. **Topic API 격리**: usdt_topic_payload / fx_topic_payload는 자체 DB
       호출 (`select_*`, source_registry 기반) — `get_source_rates_as_legacy_format`
       경로 미사용이라 본 policy 영향 0.

참조:
    - USDT_TOPIC_MIGRATION_PLAN.md §3.1 + Z-2d
    - DECISIONS.md ADR-028 (Topic-only Tether/KRX + dual-emit FX)
"""
from __future__ import annotations

from typing import Tuple

# Public constants — 순서 안정 (BANK_DISPLAY_ORDER와 같은 list/tuple 패턴).
# 새 통화 / 새 source 추가 시 명시 등록 필요 (fail-safe — 등록 안 하면 자동 제외).
LEGACY_RATE_ASSETS: Tuple[str, ...] = ("usd-krw", "jpy-krw", "eur-krw")

LEGACY_RATE_SOURCES: Tuple[str, ...] = (
    "investing",
    "kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi",
)

# Module-private set 캐시 — broadcast hot path lookup O(1).
_LEGACY_RATE_ASSET_SET = set(LEGACY_RATE_ASSETS)
_LEGACY_RATE_SOURCE_SET = set(LEGACY_RATE_SOURCES)


def should_include_source_in_legacy_rates(source: str, asset: str) -> bool:
    """(source, asset)이 legacy 노출 대상인지 결정.

    Args:
        source: 데이터 공급자 식별자 (예: "kb", "investing", "upbit", "krx").
        asset: 통화쌍/상품 (예: "usd-krw", "usdt-krw", "usd-krw-futures").

    Returns:
        True — 두 allowlist 모두 통과 (legacy REST/WebSocket 응답에 포함).
        False — 어느 한쪽이라도 미통과 (topic-only source 또는 미등록 조합).

    Examples:
        >>> should_include_source_in_legacy_rates("kb", "usd-krw")
        True
        >>> should_include_source_in_legacy_rates("upbit", "usdt-krw")
        False
        >>> should_include_source_in_legacy_rates("krx", "usd-krw-futures")
        False
        >>> should_include_source_in_legacy_rates("investing", "usdt-krw")
        False
    """
    return source in _LEGACY_RATE_SOURCE_SET and asset in _LEGACY_RATE_ASSET_SET
