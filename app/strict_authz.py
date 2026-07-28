"""WS strict 인가 — **관측(저장) ⊥ verdict(요청별 파생)** 2계층 (ADR-039 §8.1 A6-1/A6-2).

REST(`app/subscription.py`)는 가용성 우선이라 stale fallback을 갖지만, WS 인가는 fail-closed다
(A4). 이 모듈은 그 strict 쪽의 **순수 계산**만 담는다 — 저장소·epoch CAS·lock·I/O는 후속.

## 왜 verdict를 저장하지 않는가

verdict를 UID 단위로 저장하면 (a) `Inactive`를 **같은 UID의 새 토큰이 상속**하고,
(b) 신선도 판단이 **쓰기 시점에 박혀** read-time 정책을 걸 자리가 없어진다.
→ **관측(observation)을 저장하고 verdict는 요청마다 파생**한다. 그래서 revoked된 구 토큰과
새 토큰이 **같은 authority record에서 서로 다른 verdict**를 얻는다(A1이 요구하는 성질).

## identity 판정은 `auth_time`이 아니라 `iat`다

firebase-admin v6.9.0 `_check_jwt_revoked_or_disabled`의 술어를 그대로 옮긴다:

    user.disabled                                  -> 거부 (revoked보다 **먼저**)
    claims["iat"] * 1000 < tokens_valid_after_ms   -> revoked (**strict `<`**)

⚠️ **단위**: watermark는 **밀리초**(`1000 * int(valid_since)`), `iat`는 **초**. 축을 섞으면
`1.7e9 < 1.7e12`가 참이 되어 **모든 토큰이 revoked로 오판**된다.
⚠️ `auth_time`으로 대체하지 말 것 — 세션 최초 로그인 시각으로 **고정**돼 갱신마다 전진하는
`iat`와 다르다(`auth_time <= iat`). SDK보다 엄격한 **다른 술어**가 되며 단위 테스트로는 안 드러난다.
⚠️ `clock_skew_seconds`는 이 비교에 **적용되지 않는다**(SDK도 서명/exp 검증에만 쓴다).

## 신선도는 verdict가 아니라 **관측**에 붙는다

- **양성 관측**은 `LEASE_MAX_SECONDS`가 상한이다 — 그보다 오래됐으면 A1 horizon상 이미 만료된
  lease밖에 못 주므로, **계산해서 만료를 발견하는 대신 lookup 단계에서 재검증으로 보낸다**.
- **부정 관측**은 A1 horizon을 타지 않으므로 별도 상한이 필요하다. 아니면 결제한 사용자가
  webhook 유실 시 **영구 거부**된다.
- ⚠️ `token_revoked`만은 예외 — 그 토큰에 대해 monotone이라(watermark 단조↑, `iat` 고정)
  갱신이 무의미하다. 반면 `disabled`/`not_found`는 재활성화·uid 재생성이 가능해 monotone이 아니다.

## 이 모듈의 비-책임

registry 조작·전송 인가·sweep은 `app/topic_dispatcher.py`, lease 산술은 `app/topic_lease.py`.
strict cache 저장·epoch CAS·single-flight는 후속 슬라이스.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Union

from app.topic_lease import LEASE_MAX_SECONDS

# 결제한 사용자가 "구독 없음"으로 굳어 있는 최대 시간 (가용성 knob).
# 측정 전 보수적 기본값 — REST가 같은 상황에서 `CACHE_TTL`(5분)에 회복하는 것과 맞춘 값이다.
# ⚠️ 부하는 window가 아니라 **요청 간격 R과 window H의 관계**로 정해진다. R ≫ H면 어느 값이든
#    매 요청이 miss라 등가다(D6 재인증 ~11~12분 기준). 빠른 재시도 클라는 상수가 아니라
#    rate limit으로 다룬다.
PREMIUM_INACTIVE_RECHECK_SECONDS = 300.0

# 비활성화·삭제된 계정이 복구된 뒤에도 거부로 남는 최대 시간 (가용성 knob).
# ⚠️ 위 상수와 **값이 같아도 독립**이다 — 파생 관계로 묶지 말 것. 위험 프로파일이 다르다:
#    premium=흔함/결제 직후, identity=드묾/관리자 조치. 한쪽만 조정할 날이 온다.
IDENTITY_NEGATIVE_RECHECK_SECONDS = 300.0


# ── 관측 (저장 대상 — UID 키) ────────────────────────────────────────────────


@dataclass(frozen=True)
class PremiumObservation:
    """RevenueCat entitlement를 authoritative하게 관측한 결과.

    ⚠️ `active: bool`로 충분한 이유: RevenueCat 404는 *"신규 사용자 = 비구독"*으로 접히므로
    identity와 달리 not-found 변종이 없다(`app/subscription.py`의 provider 참조).
    ⚠️ REST의 `PremiumStatus`를 재사용하지 말 것 — `PENDING`이 **판정 불가와 구매 전파 유예
    두 의미를 이미 섞고 있는** REST 전용 상태다.
    """

    active: bool
    verified_at_mono: float
    epoch: int


@dataclass(frozen=True)
class IdentityAuthorityFound:
    """`get_user()`가 돌려준 계정 권위 정보. **verdict가 아니라 관측**이다."""

    disabled: bool
    tokens_valid_after_ms: int
    verified_at_mono: float
    epoch: int


@dataclass(frozen=True)
class IdentityAuthorityNotFound:
    """계정이 존재하지 않는다(`UserNotFoundError`).

    합타입으로 나눈 이유: 평면 구조면 `not_found=True`인데 watermark도 있는 **불가능한 상태**를
    만들 수 있다. 타입 검사기가 없는 리포라도 이득이 있다 — 여기서 `.tokens_valid_after_ms`를
    읽으면 **오용 지점에서 즉시** `AttributeError`가 난다(`Optional`이면 `None`이 산술까지 흘러
    한 단계 늦게 터진다).
    """

    verified_at_mono: float
    epoch: int


IdentityObservation = Union[IdentityAuthorityFound, IdentityAuthorityNotFound]


# ── verdict (요청별 파생 — 저장 금지) ────────────────────────────────────────


class InactiveReason(str, Enum):
    PREMIUM_INACTIVE = "premium_inactive"
    TOKEN_REVOKED = "token_revoked"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_DELETED = "account_deleted"


class Concern(str, Enum):
    """어느 축의 관측이 부족한가 (재검증 대상 지정용)."""

    PREMIUM = "premium"
    IDENTITY = "identity"


@dataclass(frozen=True)
class Active:
    """인가 통과. **두 관측 시각을 모두 실어** A1 3-way horizon의 입력이 된다.

    `compute_lease_expiry(now_mono=…, premium_verified_at_mono=…,
    firebase_identity_verified_at_mono=…)`의 두 인자가 정확히 이 두 필드다.
    """

    premium_verified_at_mono: float
    identity_verified_at_mono: float


@dataclass(frozen=True)
class Inactive:
    """권한 없음 — 즉시 reject. `reason`은 §8-C wire 오류코드로 매핑된다."""

    reason: InactiveReason


@dataclass(frozen=True)
class NeedsVerification:
    """⚠️ **wire verdict가 아니다.** 관측이 없거나 낡아 판정할 수 없으니 호출부가 해당 concern을
    검증한 뒤 **다시 파생**해야 한다는 내부 제어 신호다.

    `TemporarilyUnavailable`(= 판정 불가)과 **구분**한다 — 이건 "아직 안 물어봤다"이지
    "물어봤는데 답을 못 얻었다"가 아니다. 섞으면 정상 재검증 흐름이 장애 지표를 오염시킨다.
    """

    concern: Concern


Verdict = Union[Active, Inactive, NeedsVerification]


# ── 신선도 (관측에 붙는다) ───────────────────────────────────────────────────


def premium_observation_is_fresh(obs: PremiumObservation, *, now_mono: float) -> bool:
    """양성은 `LEASE_MAX_SECONDS`, 음성은 `PREMIUM_INACTIVE_RECHECK_SECONDS`가 상한.

    양성에 lease 상한을 쓰는 이유: 그보다 오래된 관측으로는 A1 horizon상 **이미 만료된 lease**밖에
    못 준다. 계산해서 만료를 발견하는 것보다 lookup 단계에서 재검증으로 보내는 편이 안전하다
    (A1의 "stale fallback 결과로는 lease를 연장하지 않는다"가 구조로 참이 된다).
    """
    horizon = LEASE_MAX_SECONDS if obs.active else PREMIUM_INACTIVE_RECHECK_SECONDS
    return _within(obs.verified_at_mono, now_mono=now_mono, horizon=horizon)


def identity_observation_is_fresh(obs: IdentityObservation, *, now_mono: float) -> bool:
    """양성(존재 + 비활성 아님)은 `LEASE_MAX_SECONDS`, 그 외는 `IDENTITY_NEGATIVE_RECHECK_SECONDS`.

    ⚠️ `NotFound`와 `disabled=True`는 monotone이 **아니다**(uid 재생성·재활성화 가능) —
    그래서 부정 관측에도 상한이 필요하다. `token_revoked`는 여기 없다: 그건 저장된 관측이 아니라
    `iat`로 매번 파생하는 판정이라 갱신 개념이 없다.
    """
    negative = isinstance(obs, IdentityAuthorityNotFound) or obs.disabled
    horizon = IDENTITY_NEGATIVE_RECHECK_SECONDS if negative else LEASE_MAX_SECONDS
    return _within(obs.verified_at_mono, now_mono=now_mono, horizon=horizon)


def _within(verified_at_mono: float, *, now_mono: float, horizon: float) -> bool:
    """`now < verified_at + horizon` — 경계는 **stale**(`app.topic_lease.is_expired`와 같은 규약).

    비유한 입력은 판정 불가이므로 stale로 접는다(fail-closed).
    """
    if not (math.isfinite(verified_at_mono) and math.isfinite(now_mono)):
        return False
    return now_mono < verified_at_mono + horizon


# ── 파생 ────────────────────────────────────────────────────────────────────


def derive_verdict(
    *,
    now_mono: float,
    premium: PremiumObservation | None,
    identity: IdentityObservation | None,
    token_iat_seconds: int,
) -> Verdict:
    """관측 2개 + 토큰 `iat` → verdict. **순수 함수**(I/O·시각 읽기 없음).

    관측이 없거나(`None`) 낡았으면 `NeedsVerification`을 돌려 **신선도를 우회할 수 없게** 한다 —
    호출부가 "신선한 것만 넘긴다"고 약속하는 형태였다면 그 약속을 강제할 방법이 없다.

    판정 순서는 §8.1 D5의 단계 순서를 따른다 — **identity(인증)가 premium(인가)보다 먼저**.
    identity 안에서는 SDK와 같이 **disabled가 revoked보다 먼저**다(둘 다 해당하면 disabled가 이긴다).

    Args:
        token_iat_seconds: 검증된 ID token의 `iat` 클레임(**초**). `auth_time`이 아니다.
    """
    if identity is None or not identity_observation_is_fresh(identity, now_mono=now_mono):
        return NeedsVerification(concern=Concern.IDENTITY)

    if isinstance(identity, IdentityAuthorityNotFound):
        return Inactive(reason=InactiveReason.ACCOUNT_DELETED)
    if identity.disabled:
        return Inactive(reason=InactiveReason.ACCOUNT_DISABLED)
    # ⚠️ `iat`(초) → ms 로 올려서 비교한다. strict `<` — 같으면 revoked 아님.
    if token_iat_seconds * 1000 < identity.tokens_valid_after_ms:
        return Inactive(reason=InactiveReason.TOKEN_REVOKED)

    if premium is None or not premium_observation_is_fresh(premium, now_mono=now_mono):
        return NeedsVerification(concern=Concern.PREMIUM)
    if not premium.active:
        return Inactive(reason=InactiveReason.PREMIUM_INACTIVE)

    return Active(
        premium_verified_at_mono=premium.verified_at_mono,
        identity_verified_at_mono=identity.verified_at_mono,
    )
