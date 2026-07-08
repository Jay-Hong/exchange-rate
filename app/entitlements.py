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


def krx_alert_gate_error(db: Session, user_id: str) -> Optional[str]:
    """KRX 알림 생성/재활성/변경 허용 판정 — None=허용 / str=403 사유.

    호출 지점 (main.py — codex Q7 세밀화):
    - source POST: (source, asset) == KRX_PAIR
    - source PUT: 최종 조합 == KRX_PAIR AND is_enabled is not False
      (끄기 전용 PUT은 허용 — 권한 상실 사용자도 자기 알림을 끌 수 있어야. DELETE도 항상 허용)
    - comparison POST/PUT: signed + counter == KRX_PAIR (PUT은 재활성 우회 차단 포함)
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
