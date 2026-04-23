"""Source registry for source + asset based data feeds.

새 source 기반 세계의 단일 truth source.

기존 bank/investing 세계(bank_exchange_rates, investing_exchange_rates)와는
완전히 분리된 새 데이터 모델이다.

- DB 내부: source + asset
- 레거시 API (/api/rates, WebSocket rates 배열): bank + currency로 변환되어 응답
- 새 API (비교 알림 등): source + asset 구조 그대로 사용

Phase 1 범위: 업비트/빗썸/코인원/고팍스/코빗 USDT/KRW
Phase 2 예정: KRX 미국달러선물 (phase1_enabled=False로 자리만 확보)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceDefinition:
    """하나의 (source, asset) 조합에 대한 메타데이터."""

    source: str
    asset: str
    display_name: str
    category: str                        # "exchange" | "reference" | "derivative"
    sort_order: int
    freshness_seconds: Optional[int]     # None = Phase 2에서 결정 (KRX 등)
    phase1_enabled: bool = True


# 기본 표시 순서: 인베스팅 → KB → 하나 → (미국달러F) → 업비트 → 빗썸 → 코인원 → 고팍스 → 코빗
_ALL_SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        source="investing",
        asset="usd-krw",
        display_name="인베스팅",
        category="reference",
        sort_order=10,
        freshness_seconds=90,
    ),
    SourceDefinition(
        source="kb",
        asset="usd-krw",
        display_name="국민은행",
        category="reference",
        sort_order=20,
        freshness_seconds=300,
    ),
    SourceDefinition(
        source="hana",
        asset="usd-krw",
        display_name="하나은행",
        category="reference",
        sort_order=30,
        freshness_seconds=300,
    ),
    SourceDefinition(
        source="krx",
        asset="usd-krw-futures",
        display_name="미국달러F",
        category="derivative",
        sort_order=40,
        freshness_seconds=None,
        phase1_enabled=False,
    ),
    SourceDefinition(
        source="upbit",
        asset="usdt-krw",
        display_name="업비트",
        category="exchange",
        sort_order=50,
        freshness_seconds=35,
    ),
    SourceDefinition(
        source="bithumb",
        asset="usdt-krw",
        display_name="빗썸",
        category="exchange",
        sort_order=60,
        freshness_seconds=35,
    ),
    SourceDefinition(
        source="coinone",
        asset="usdt-krw",
        display_name="코인원",
        category="exchange",
        sort_order=70,
        freshness_seconds=35,
    ),
    SourceDefinition(
        source="gopax",
        asset="usdt-krw",
        display_name="고팍스",
        category="exchange",
        sort_order=80,
        freshness_seconds=35,
    ),
    SourceDefinition(
        source="korbit",
        asset="usdt-krw",
        display_name="코빗",
        category="exchange",
        sort_order=90,
        freshness_seconds=35,
    ),
)

_LOOKUP: dict[tuple[str, str], SourceDefinition] = {
    (s.source, s.asset): s for s in _ALL_SOURCES
}


def build_canonical_key(source: str, asset: str) -> str:
    """내부 helper: canonical key 생성. 외부 API에는 노출하지 않음."""
    return f"{source}:{asset}"


def get_source_definition(source: str, asset: str) -> Optional[SourceDefinition]:
    """등록된 SourceDefinition 조회. 미등록이면 None."""
    return _LOOKUP.get((source, asset))


def is_registered_source(source: str, asset: str) -> bool:
    return (source, asset) in _LOOKUP


def is_phase1_source(source: str, asset: str) -> bool:
    definition = _LOOKUP.get((source, asset))
    return definition is not None and definition.phase1_enabled


def get_all_sources() -> list[SourceDefinition]:
    """등록된 모든 소스 (phase1_enabled 여부 무관). sort_order 기준 정렬."""
    return sorted(_ALL_SOURCES, key=lambda s: s.sort_order)


def get_enabled_sources() -> list[SourceDefinition]:
    """phase1_enabled=True인 소스만. sort_order 기준 정렬."""
    return [s for s in get_all_sources() if s.phase1_enabled]


def get_usdt_exchange_entries() -> list[SourceDefinition]:
    """Phase 1 USDT 거래소 소스 목록. 크롤러가 사용."""
    return [
        s
        for s in get_enabled_sources()
        if s.category == "exchange" and s.asset == "usdt-krw"
    ]
