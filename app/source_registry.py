"""Source registry for source + asset based data feeds.

새 source 기반 세계의 단일 truth source.

기존 bank/investing 세계(bank_exchange_rates, investing_exchange_rates)와는
완전히 분리된 새 데이터 모델이다.

- DB 내부: source + asset
- 레거시 API (/api/rates, WebSocket rates 배열): bank + currency로 변환되어 응답
- 새 API (비교 알림 등): source + asset 구조 그대로 사용

Phase 1 범위: 업비트/빗썸/코인원/고팍스/코빗 USDT/KRW
Phase 2 진입 (F-2, 2026-05-26): KRX 미국달러선물 `phase1_enabled=True` —
    `_validate_alert_source_asset_or_400` (main.py wrapper, F-2 cleanup에서
    rename됨 — 이전 `_validate_phase1_source_asset`)가 derivative category도
    허용. 알림 발송은 별도 축인 `KRX_ALERT_EVALUATOR_ENABLED` env (F-3)로 분리.
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


# 기본 표시 순서: 인베스팅 → KB → 하나 → (달러선물) → 업비트 → 빗썸 → 코인원 → 고팍스 → 코빗
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
        display_name="달러선물",
        category="derivative",
        sort_order=40,
        # F-2 (2026-05-26): phase1_enabled=False → True. main.py wrapper
        # `_validate_alert_source_asset_or_400` (rename 이전 이름은
        # `_validate_phase1_source_asset`)가 derivative category도 허용해 KRX
        # 알림 설정 등록 가능. 발송은 별도 축인 `KRX_ALERT_EVALUATOR_ENABLED`
        # env(F-3)가 열려야 발화 — F-2 land ~ F-3 활성 사이는 의도된 canary
        # staging gap (dead alert). 운영 단말 영향 0.
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

    F-2 (2026-05-26) 진입: main.py에 두던 `_validate_phase1_source_asset`
    (F-2 cleanup에서 `_validate_alert_source_asset_or_400`로 rename)의 검증
    로직 본체를 여기로 분리. 이유는 과거 메모리 기록 `project_main_py_helper_placement`
    참조 — main.py 안에 helper 두면 단위 테스트가 firebase_admin import chain으로
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


# ---------------------------------------------------------------------------
# 비교 알림 조합 정책 (ADR-037 Amendment 2026-07-04 — 제품 의미 분리)
# ---------------------------------------------------------------------------
# ⚠️ 구 COMPARISON_TAB_SOURCES(그래프 catalog krw series와 1:1 invariant)는 폐기 —
# 그래프 표시 소스 ≠ 비교 허용 소스가 새 정책의 본질 (테더 그래프엔 krx/참조가 있어도
# 일반 비교는 거래소 5끼리만).
#
# 정책 (diff_type이 제품 의미를 가름 — validation이 이 invariant를 강제해야
# "absolute=일반 비교 / signed=김프알림" 섹션 구분이 성립):
#   - absolute("차이 벌어지면/좁혀지면") = 일반 비교알림. 탭별 대칭 pair 집합.
#     threshold ≥ 0 강제 (음수 absolute gte는 항상 참에 수렴).
#   - signed = 김프(역프) 알림. 테더 탭 전용 — left(기준) ∈ 거래소 5 ×
#     right(비교 상대) ∈ {hana, kb, investing}. threshold 부호 자유 (음수=역프).
#     krx는 ADR-038(entitlement) 구현 후 상대 집합에 추가.
# ⚠️ 기존 validate_alert_source_asset 재사용 금지 (reference 거부 로직 상충).

_FX_BANKS_COMPARISON = ("kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs")   # Citi 제외

_USDT_EXCHANGES = frozenset(
    {(ex, "usdt-krw") for ex in ("upbit", "bithumb", "coinone", "korbit", "gopax")}
)

# absolute(일반 비교) 탭별 허용 (source, asset) — pair 양쪽 모두 이 집합 안 + left≠right.
COMPARISON_ABSOLUTE_SOURCES: dict = {
    "tether": _USDT_EXCHANGES,   # 거래소 5끼리만 (cross-world는 김프알림 전담)
    "usd": frozenset({("investing", "usd-krw")} | {(b, "usd-krw") for b in _FX_BANKS_COMPARISON}),
    "jpy": frozenset({("investing", "jpy-krw")} | {(b, "jpy-krw") for b in _FX_BANKS_COMPARISON}),
    "eur": frozenset({("investing", "eur-krw")} | {(b, "eur-krw") for b in _FX_BANKS_COMPARISON}),
}

# signed(김프/역프) — 테더 탭 전용. left=기준(거래소), right=비교 상대(환율계).
KIMCHI_BASE_SOURCES = _USDT_EXCHANGES
KIMCHI_COUNTER_SOURCES = frozenset({
    ("hana", "usd-krw"), ("kb", "usd-krw"), ("investing", "usd-krw"),
    # krx는 구조적으로 유효한 counter (ADR-038 G1/G2 land 2026-07-08) — 단 entitlement/G2/G3
    # 게이트는 handler(main.py)가 403으로 강제. validator는 구조 유효성만 (순수성 유지, codex Q3).
    ("krx", "usd-krw-futures"),
})

# 서버 hard cap (codex 2026-07-04): 클라 UI는 ±1000(넓힘)이나 서버가 signed threshold를 무제한
# 허용하면 API 직접 호출/구버전 클라로 이상값 유입 가능. 클라 범위와 결합하지 않는 관대한 절대
# sanity 상한 — 기록된 최대 급등(+4232, 2025-10-11 빗썸 wick)의 2배 이상이라 정상값은 거부 안 함.
COMPARISON_THRESHOLD_ABS_MAX = 10000.0


def validate_comparison_alert(
    tab: str,
    left_source: str,
    left_asset: str,
    right_source: str,
    right_asset: str,
    diff_type: str,
    threshold: float,
) -> Optional[str]:
    """비교/김프 알림 조합 검증 — 에러 메시지 반환 (정상이면 None).

    ADR-037 Amendment 2026-07-04: diff_type이 정책을 가름.
    FastAPI 비의존 (main.py thin wrapper가 HTTPException 변환).
    """
    left = (left_source, left_asset)
    right = (right_source, right_asset)
    if left == right:
        return "left and right must differ (same source+asset pair)"

    # 공통 sanity 상한 (signed/absolute 모두) — API 직접호출/구버전 이상값 방어 (codex).
    if abs(threshold) > COMPARISON_THRESHOLD_ABS_MAX:
        return f"threshold out of range (|threshold| must be <= {COMPARISON_THRESHOLD_ABS_MAX:g})"

    if diff_type == "absolute":
        allowed = COMPARISON_ABSOLUTE_SOURCES.get(tab)
        if allowed is None:
            return f"Unknown tab '{tab}'. Allowed: {sorted(COMPARISON_ABSOLUTE_SOURCES)}"
        if threshold < 0:
            return "absolute threshold must be >= 0"
        for side, pair in (("left", left), ("right", right)):
            if pair not in allowed:
                return (f"{side} ({pair[0]}:{pair[1]}) is not allowed for absolute comparison "
                        f"in tab '{tab}'.")
        return None

    if diff_type == "signed":
        # 김프(역프) 알림 — 테더 탭 전용 (ADR-037 Amendment 3)
        if tab != "tether":
            return "signed (kimchi premium) alerts are only supported in tab 'tether'"
        if left not in KIMCHI_BASE_SOURCES:
            return (f"kimchi base ({left[0]}:{left[1]}) must be one of the 5 USDT exchanges")
        if right not in KIMCHI_COUNTER_SOURCES:
            return (f"kimchi counter ({right[0]}:{right[1]}) must be one of "
                    f"hana/kb/investing (usd-krw)")
        return None

    return f"Unknown diff_type '{diff_type}' (signed | absolute)"


def canonicalize_absolute_pair(
    left_source: str, left_asset: str, right_source: str, right_asset: str,
) -> tuple:
    """absolute pair 정규화 — (source, asset) 사전순으로 (left, right) 정렬.

    left/right 개념이 UI에서 소멸(|A−B|=|B−A|)했으므로 저장 전 정규화해 A−B/B−A
    dedup 중복을 차단 (ADR-037 Open 6 absolute에 한해 closed). signed는 방향 의미
    보존이라 미적용. 반환: (left_source, left_asset, right_source, right_asset).
    """
    a = (left_source, left_asset)
    b = (right_source, right_asset)
    lo, hi = (a, b) if a <= b else (b, a)
    return (lo[0], lo[1], hi[0], hi[1])
