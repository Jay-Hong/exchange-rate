"""Source registry for source + asset based data feeds.

새 source 기반 세계의 단일 truth source.

기존 bank/investing 세계(bank_exchange_rates, investing_exchange_rates)와는
완전히 분리된 새 데이터 모델이다.

- DB 내부: source + asset
- 레거시 API (/api/rates, WebSocket rates 배열): bank + currency로 변환되어 응답
- 새 API (비교 알림 등): source + asset 구조 그대로 사용

Phase 1 범위: 업비트/빗썸/코인원/고팍스/코빗 USDT/KRW
Phase 2 진입 (F-2, 2026-05-26): KRX 미국달러선물 `phase1_enabled=True` —
    `_validate_phase1_source_asset`가 category="derivative"도 허용. 알림
    발송은 별 축인 `KRX_ALERT_EVALUATOR_ENABLED` env (F-3)로 분리.
    F-2 land ~ F-3 활성 사이는 의도된 canary staging 상태 — API 등록은
    가능하나 발송은 안 됨 (dead alert gap). 운영 단말 영향 0 (테더 탭 자체가
    운영 앱에 없음, 테스트 iOS canary 전용).

Stale 판정에 대하여:
    현재 timestamp는 "마지막 값 변경 시각" (insert-if-changed 정책 결과)이라
    "마지막 수집 성공 시각"과 다르다. 은행 주말이나 USDT 저유동성 구간에서
    오진이 발생하므로 Phase 1에서는 stale 판정 기능을 도입하지 않는다.
    향후 observed_at 또는 last_collection_success_at을 별도 추적하게 되면
    그때 stale 관련 필드를 재도입한다.
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
    phase1_enabled: bool = True


# 기본 표시 순서: 인베스팅 → KB → 하나 → (미국달러F) → 업비트 → 빗썸 → 코인원 → 고팍스 → 코빗
_ALL_SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        source="investing",
        asset="usd-krw",
        display_name="인베스팅",
        category="reference",
        sort_order=10,
    ),
    SourceDefinition(
        source="kb",
        asset="usd-krw",
        display_name="국민은행",
        category="reference",
        sort_order=20,
    ),
    SourceDefinition(
        source="hana",
        asset="usd-krw",
        display_name="하나은행",
        category="reference",
        sort_order=30,
    ),
    SourceDefinition(
        source="krx",
        asset="usd-krw-futures",
        display_name="미국달러F",
        category="derivative",
        sort_order=40,
        # F-2 (2026-05-26): phase1_enabled=False → True. `_validate_phase1_source_asset`
        # 가 category="derivative"도 허용해 KRX 알림 설정 등록 가능. 발송은 별 축인
        # `KRX_ALERT_EVALUATOR_ENABLED` env(F-3)가 열려야 발화 — F-2 land ~ F-3 활성
        # 사이는 의도된 canary staging gap (dead alert). 운영 단말 영향 0.
        phase1_enabled=True,
    ),
    SourceDefinition(
        source="upbit",
        asset="usdt-krw",
        display_name="업비트",
        category="exchange",
        sort_order=50,
    ),
    SourceDefinition(
        source="bithumb",
        asset="usdt-krw",
        display_name="빗썸",
        category="exchange",
        sort_order=60,
    ),
    SourceDefinition(
        source="coinone",
        asset="usdt-krw",
        display_name="코인원",
        category="exchange",
        sort_order=70,
    ),
    SourceDefinition(
        source="korbit",
        asset="usdt-krw",
        display_name="코빗",
        category="exchange",
        sort_order=80,
    ),
    SourceDefinition(
        source="gopax",
        asset="usdt-krw",
        display_name="고팍스",
        category="exchange",
        sort_order=90,
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


def validate_alert_source_asset(source: str, asset: str) -> Optional[str]:
    """알림 등록 가능 여부 검증 — FastAPI 비의존 helper.

    F-2 (2026-05-26) 진입: main.py에 두던 `_validate_phase1_source_asset`를
    여기로 분리. 이유는 [project_main_py_helper_placement.md] 메모리 기록 —
    main.py 안에 helper 두면 단위 테스트가 firebase_admin import chain으로
    깨짐. source_registry는 fastapi/firebase 의존성 없는 도메인 모듈이라
    검증 로직 자연 위치 + 테스트 격리 가능.

    Returns:
        None: 통과 (알림 등록 허용).
        str: 차단 사유 (400 응답 detail에 그대로 노출 가능한 형태).

    허용 대상:
    - phase1_enabled=True
    - category in ("exchange", "derivative") — 거래소(USDT 5 source) + KRX 미국달러선물

    reference 소스(investing, kb, hana)는 기존 `/api/notification-settings`를 사용해야 한다.
    이유: process_source_rate_alerts는 usdt_sources 크롤러에서만 호출되므로,
    reference 소스를 허용하면 생성은 되지만 발송되지 않는 "dead alert"가 된다.

    F-2 land ~ F-3 (`KRX_ALERT_EVALUATOR_ENABLED=true`) 활성 사이 KRX 알림은
    의도된 staging gap — API 등록 가능하나 발송은 evaluator flag가 닫혀 있음.
    운영 단말 영향 0 (테더 탭 자체가 운영 앱에 없음, 테스트 iOS canary 전용).
    """
    definition = _LOOKUP.get((source, asset))
    if definition is None or not definition.phase1_enabled:
        return f"Unsupported source/asset combination: {source}:{asset}"
    if definition.category not in ("exchange", "derivative"):
        return (
            f"Source alerts are only supported for exchange/derivative sources "
            f"(got category={definition.category}). "
            f"Use /api/notification-settings for bank/investing alerts."
        )
    return None
