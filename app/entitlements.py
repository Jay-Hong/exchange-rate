"""ADR-038 G1/G2 — KRX 달러선물 노출 게이트 판정 (FastAPI 비의존 도메인 모듈).

본체를 main.py에 두면 firebase_admin import chain으로 단위 테스트가 깨짐
(선례: source_registry.validate_alert_source_asset 분리 — memory project_main_py_helper_placement).
main.py는 HTTPException 변환 thin wrapper만 가짐.

게이트 캐스케이드 (ADR-038 Decision 1):
  G3 KRX_FUTURES_ENABLED(수집) ∧ G2 KRX_CLIENT_DISTRIBUTION_ENABLED(배포)
  ∧ G1 user_entitlements(운영자 수동 부여) ∧ premium(RevenueCat)

클라는 게이트 조합을 계산하지 않음 — 서버가 krx_visible 단일 신호 제공
(GET /api/entitlements, main.py). 캐시 없음 — 알림 CRUD/entitlements 조회는 저빈도 +
user_entitlements (user_id, key) UNIQUE 조회는 저비용 (codex Q6 합의 2026-07-08).
"""

# 표준 라이브러리
from typing import Optional

# 서드파티 라이브러리
from sqlalchemy.orm import Session

KRX_FUTURES_ENTITLEMENT_KEY = "krx_futures"

# KRX 달러선물 (source, asset) 조합 — 알림 API gate 판정용 (source_registry 정의와 일치)
KRX_PAIR = ("krx", "usd-krw-futures")


def has_entitlement(db: Session, user_id: str, key: str) -> bool:
    """G1 — user_entitlements에 (user_id, key) row 존재 여부."""
    from app import models
    return (
        db.query(models.UserEntitlement.id)
        .filter(models.UserEntitlement.user_id == user_id,
                models.UserEntitlement.key == key)
        .first()
        is not None
    )


def krx_gates_open() -> bool:
    """G3 ∧ G2 — 전역(사용자 무관) client-facing KRX 배포 허용 여부.

    두 flag를 runtime에 직접 읽음 (파생 상수 KRX_CLIENT_DISTRIBUTION_EFFECTIVE는 import-time
    고정이라 테스트 patch 불가 — graph v2 accessor와 동일 이유로 runtime 조합).
    """
    from app import config
    return config.KRX_FUTURES_ENABLED and config.KRX_CLIENT_DISTRIBUTION_ENABLED


def krx_unauthenticated_graph_exposure_allowed() -> bool:
    """무인증 graph v2 표면(`/api/v2/graph/tab`·`/catalog`)이 KRX series를 실어도 되는가.

    **G2(`KRX_CLIENT_DISTRIBUTION_ENABLED`)와 분리된 두 번째 승인 flag**
    (`KRX_GRAPH_ALLOW_UNAUTHENTICATED_EXPOSURE`, default **false**) — ADR-039 §3.1/§6.1, 2026-07-26.

    문제: 이 두 endpoint는 **인증이 없는데** series 목록을 `krx_gates_open()`(G2∧G3)**만으로**
    정했다. 즉 `KRX_CLIENT_DISTRIBUTION_ENABLED=true` **한 줄**로 무인증 caller에게 KRX 그래프
    series가 열린다. E3가 REST twin(`/api/v2/topics/snapshot`)에서 막은 것과 **같은 누수**다.

    왜 flag를 없애지 않고 분리했나: 이 노출은 **실수가 아니라 명세된 계약**이다 —
    ADR-038 Decision 3이 "per-user 노출은 클라 `krx_visible` gate 담당"으로 수용했고(Open 2),
    D4가 탭별 series 구성을 정했으며 `GRAPH_API_V2_CONTRACT` §3/§4와 12개 테스트가 잠그고 있다.
    서버가 일방적으로 빼면 entitled 사용자가 그래프에서 KRX를 잃는다. 그래서 계약은 그대로 두되
    **사고로 열리지 않게** 두 번째 flag로 분리했다 — 이름 자체가 무엇을 여는지 말한다.

    ADR-039 §3.1은 서버 per-user 강제를 요구한다(= 더 강한 계약). 그 게이트가 land하면 이 함수는
    per-user 판정으로 교체되고 flag는 제거된다. 그 전까지 **이 flag를 켜는 것은 §3.2 위반을
    감수하는 명시적 결정**이다.

    ⚠️ **완전한 판정**을 여기서 돌려준다(G3 ∧ G2 ∧ 승인). graph 모듈이 각자 AND를 조립하면
    같은 보안 규칙이 두 곳에 복제돼 drift한다 — 두 모듈은 이 함수만 호출한다.
    """
    from app import config
    return krx_gates_open() and config.KRX_GRAPH_ALLOW_UNAUTHENTICATED_EXPOSURE


def krx_alert_gate_error(db: Session, user_id: str) -> Optional[str]:
    """KRX 알림 생성/재활성/변경 허용 판정 — None=허용 / str=403 사유.

    호출 지점 (main.py — codex Q7 세밀화):
    - source POST: (source, asset) == KRX_PAIR
    - source PUT: 최종 조합 == KRX_PAIR AND is_enabled is not False
      (끄기 전용 PUT은 허용 — 권한 상실 사용자도 자기 알림을 끌 수 있어야. DELETE도 항상 허용)
    - comparison POST/PUT: KRX_PAIR가 left/right 어느 쪽이든 (signed 김프 counter +
      usd absolute 달러선물 비교, ADR-038 D4 2026-07-10. PUT은 재활성 우회 차단 포함)
    """
    if not krx_gates_open():
        return "krx distribution is disabled"
    if not has_entitlement(db, user_id, KRX_FUTURES_ENTITLEMENT_KEY):
        return "krx_futures entitlement required"
    return None


def compute_krx_visible(db: Session, user_id: str, premium_active: bool) -> bool:
    """krx_visible 단일 신호 = G3 ∧ G2 ∧ G1 ∧ premium (ADR-038 Decision 1)."""
    return premium_active and krx_gates_open() and has_entitlement(
        db, user_id, KRX_FUTURES_ENTITLEMENT_KEY)
