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

⚠️ **단위**: watermark는 **밀리초**(`1000 * int(valid_since)`), `iat`는 **초**.
⛔ 위험한 방향은 과차단이 아니라 **과허용(fail-open)**이다 — 이 모듈이 `* 1000`을 스스로 하므로
"전부 revoked로 오판"되는 방향은 **도달 불가**하고, 실제로 남는 호출부 실수는 전부 반대다:
watermark를 초로 넘기거나 `iat`를 ms로 넘기거나 둘 중 하나가 `NaN`이면 비교가 False가 되어
**revoke된 토큰이 그대로 통과**한다(감사에서 4종 전부 재현). 그래서 아래 `derive_verdict`가
두 피연산자의 **축 규모를 검증**한다 — 구 docstring은 안전한 방향만 경고해 잘못된 안심을 줬다.
⚠️ `auth_time`으로 대체하지 말 것 — 세션 최초 로그인 시각으로 **고정**돼 갱신마다 전진하는
`iat`와 다르다(`auth_time <= iat`). SDK보다 엄격한 **다른 술어**가 되며 단위 테스트로는 안 드러난다.
⚠️ `clock_skew_seconds`는 이 비교에 **적용되지 않는다**(SDK도 서명/exp 검증에만 쓴다).

## 신선도는 verdict가 아니라 **관측**에 붙는다

- **양성 관측**은 `LEASE_MAX_SECONDS`가 상한이다 — 그보다 오래됐으면 A1 horizon상 이미 만료된
  lease밖에 못 주므로, **계산해서 만료를 발견하는 대신 lookup 단계에서 재검증으로 보낸다**.
- **부정 관측**은 A1 horizon을 타지 않으므로 별도 상한이 필요하다. 아니면 결제한 사용자가
  webhook 유실 시 **영구 거부**된다.
- ⚠️ `token_revoked`에는 **별도 상수가 없다** — 그건 저장된 관측이 아니라 요청마다 `iat`로
  파생하는 판정이라 "갱신" 개념 자체가 없기 때문이다. 반면 `disabled`/`not_found`는
  재활성화·uid 재생성이 가능해 monotone이 아니라서 상한이 필요하다.
  ⛔ 단 **구현은 record 신선도를 먼저 본다** — stale record가 이미 revoke를 증명하더라도
  `NeedsVerification`이 나온다(결과는 재검증 후 `Inactive`로 같고, RTT 1회를 더 쓸 뿐이다).
  이걸 최적화하려면 **한 방향으로만 건전**하다는 점을 지켜야 한다: watermark는 단조 증가하므로
  *stale이 "revoked"라고 하면 fresh도 revoked*(건전)지만, *stale이 "not revoked"라고 해도
  fresh는 revoked일 수 있다*(불건전). 그리고 같은 record의 `disabled`/`NotFound`는 monotone이
  아니므로 **그 최적화는 revoke 술어에만** 적용해야 한다. 실측상 freshness 게이트 앞에 단락을
  넣으면 SDK와 맞춘 `disabled > revoked` 우선순위도 함께 깨진다. 지금은 채택하지 않았다 —
  정확성 이득이 0이고(결과 동일) 비대칭 분기의 미묘함이 비용보다 크다
  (`tests/test_strict_authz.py::TestStaleRecordDoesNotShortCircuitRevoke`가 현 동작을 못 박는다).

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

# 관측 시각이 `now`보다 미래일 수 있는 허용 폭 — **여기서 허용한 s초는 각 horizon에 그대로 가산된다**.
# ⛔ `topic_lease.MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS`(=900)를 재사용하면 안 된다. 그 값의 근거는
#    "3-way `min`이 clamp하므로 표현력 손실 0"인데, **신선도에서 `verified_at`은 clamp되는 floor가
#    아니라 horizon을 재는 anchor**라 근거가 전이되지 않는다. 실제로 900을 쓰면 음성 관측의
#    상한이 문서값 300초가 아니라 **1200초(4×)**가 됐다(감사 실측).
# ⛔ `min(verified, now)` clamp로도 해결되지 않는다 — 미래 구간에서 `now < now + horizon`이 항상
#    참이라 창이 그대로 s+horizon이다(실측 확인). **밴드를 좁히는 것만이 해법**이다.
# 단일 monotonic 시계에서 관측은 판정보다 먼저 쓰이므로 정상 skew는 0이다. 이 값은 샘플링 지터용.
MAX_OBSERVATION_FUTURE_SKEW_SECONDS = 1.0

# revocation 비교 피연산자의 축 sanity 범위. 규모가 두 자릿수 이상 어긋나면 단위 오배선이다.
# `tokens_valid_after_ms == 0`은 **never-revoked**를 뜻하므로 반드시 허용한다(SDK가 그렇게 준다).
_WATERMARK_MS_MIN = 10**12          # ≈2001년을 ms로
_WATERMARK_MS_MAX = 10**14
_IAT_SECONDS_MIN = 10**9            # ≈2001년을 초로
_IAT_SECONDS_MAX = 10**11


def _require_bool(value: object, label: str) -> None:
    """⚠️ truthiness로 소비되는 필드는 **타입을 강제**한다.

    저장 슬라이스가 Redis hash(값이 항상 문자열)나 느슨한 역직렬화를 쓰면 `"false"` / `"0"`이
    **참으로 접혀 비구독자가 통과**한다. 역직렬화 경계에서 bool 복원을 강제하는 효과가 있다.
    """
    if type(value) is not bool:
        raise ValueError(f"{label}: bool이어야 — got {value!r}")


def _require_uid(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: 비어 있지 않은 str이어야 — got {value!r}")


# ── 관측 (저장 대상 — UID 키) ────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class PremiumObservation:
    """RevenueCat entitlement를 authoritative하게 관측한 결과.

    ⚠️ `active: bool`로 충분한 이유: RevenueCat 404는 *"신규 사용자 = 비구독"*으로 접히므로
    identity와 달리 not-found 변종이 없다(`app/subscription.py`의 provider 참조).
    ⚠️ REST의 `PremiumStatus`를 재사용하지 말 것 — `PENDING`이 **판정 불가와 구매 전파 유예
    두 의미를 이미 섞고 있는** REST 전용 상태다.
    """

    uid: str
    active: bool
    verified_at_mono: float
    epoch: int

    def __post_init__(self) -> None:
        _require_bool(self.active, "PremiumObservation.active")
        _require_uid(self.uid, "PremiumObservation.uid")


@dataclass(frozen=True, kw_only=True)
class IdentityAuthorityFound:
    """`get_user()`가 돌려준 계정 권위 정보. **verdict가 아니라 관측**이다."""

    uid: str
    disabled: bool
    tokens_valid_after_ms: int
    verified_at_mono: float
    epoch: int

    def __post_init__(self) -> None:
        _require_bool(self.disabled, "IdentityAuthorityFound.disabled")
        _require_uid(self.uid, "IdentityAuthorityFound.uid")
        if type(self.tokens_valid_after_ms) is not int:
            raise ValueError(
                f"IdentityAuthorityFound.tokens_valid_after_ms: int이어야 — "
                f"got {self.tokens_valid_after_ms!r}"
            )


@dataclass(frozen=True, kw_only=True)
class IdentityAuthorityNotFound:
    """계정이 존재하지 않는다(`UserNotFoundError`).

    합타입으로 나눈 이유: 평면 구조면 `not_found=True`인데 watermark도 있는 **불가능한 상태**를
    만들 수 있다. 타입 검사기가 없는 리포라도 이득이 있다 — 여기서 `.tokens_valid_after_ms`를
    읽으면 **오용 지점에서 즉시** `AttributeError`가 난다(`Optional`이면 `None`이 산술까지 흘러
    한 단계 늦게 터진다).
    """

    uid: str
    verified_at_mono: float
    epoch: int

    def __post_init__(self) -> None:
        _require_uid(self.uid, "IdentityAuthorityNotFound.uid")


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
    ⚠️ 이 규칙이 bound하는 것은 **관측 레코드의 나이**뿐이다. A1의 "stale fallback 결과로는
    lease를 연장하지 않는다"는 *stale fallback 값을 관측으로 승격하지 않는다*는 **별개 불변식**이고
    (`PremiumObservation`에는 출처를 표현할 필드도 없다), 그 G 행은 A6 verifier 슬라이스가 진다.
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

    ⚠️ **축 위반은 조용히 넘기지 않고 `ValueError`로 전파**한다(A6-1: programming 오류는 verdict가
    아니다). 이유는 이 경로에서 실패가 **완전히 무성(無聲)이기 때문**이다 — wall epoch(~1.7e9)가
    `verified_at_mono`로 새면 `now(~1e5) < 1.7e9 + 300`이 항상 참이라 **음성 관측이 영구 fresh**가
    되고, `premium_inactive` / `account_disabled` / `account_deleted`가 재검증 상한을 우회해
    정상 사용자가 **영구 거부**된다(실측 확인).
    ⚠️ `topic_lease.compute_lease_expiry`의 gross-skew 가드는 이걸 못 막는다 — **음성 verdict는
    lease 계산을 아예 거치지 않는다.** 같은 위험을 한쪽에만 걸어 둔 비대칭이었다.

    ⚠️ `topic_lease.is_expired`는 같은 상황에서 **만료로 접고 raise하지 않는다** — 그건 B1
    전송 직전 hot path라 예외가 더 나쁘기 때문이다. 여기는 subscribe당 1회 파생 경로다.
    """
    if not (math.isfinite(verified_at_mono) and math.isfinite(now_mono)):
        raise ValueError(
            f"strict_authz: 시각은 finite여야 — verified_at={verified_at_mono!r} now={now_mono!r}"
        )
    if verified_at_mono > now_mono + MAX_OBSERVATION_FUTURE_SKEW_SECONDS:
        raise ValueError(
            "strict_authz: verified_at_mono가 now보다 "
            f"{MAX_OBSERVATION_FUTURE_SKEW_SECONDS}s 넘게 미래 — monotonic 축 위반 의심 "
            f"(got {verified_at_mono!r}, now={now_mono!r})"
        )
    return now_mono < verified_at_mono + horizon


# ── 파생 ────────────────────────────────────────────────────────────────────


def _require_revocation_axes(token_iat_seconds: object, tokens_valid_after_ms: int) -> None:
    """이 슬라이스에서 **가장 중요한 가드** — revocation 비교의 두 피연산자 축을 검증한다.

    ⚠️ 이 비교가 revoke를 결정하는 **유일한 지점**인데, 도달 가능한 오배선이 전부 fail-open이다
    (감사 실측): watermark를 초로 / `iat`를 ms로 / 둘 중 하나가 `NaN` → 전부 비교가 False가 되어
    **revoked 토큰이 통과**한다. 게다가 그 결과 관측은 *양성*(found+enabled)이라 신선도가 True로
    유지되고, 만료 후 재검증도 같은 잘못된 값을 다시 기록해 **영구적**이다.

    `_within`이 시각 축에 거는 것과 **같은 강도**의 정책이다 — 같은 함수 안의 두 수치 비교가
    정반대 정책을 쓰고 있었다.
    """
    if type(token_iat_seconds) is not int:
        raise ValueError(f"token_iat_seconds: int이어야(초 epoch) — got {token_iat_seconds!r}")
    if not (_IAT_SECONDS_MIN <= token_iat_seconds < _IAT_SECONDS_MAX):
        raise ValueError(
            f"token_iat_seconds 축 위반 — 초 epoch이어야 하는데 {token_iat_seconds!r} "
            f"(ms를 넘기지 않았는지 확인)"
        )
    # 0 = never revoked (SDK가 `validSince` 부재 시 0을 준다) — 반드시 허용한다.
    if tokens_valid_after_ms == 0:
        return
    if not (_WATERMARK_MS_MIN <= tokens_valid_after_ms < _WATERMARK_MS_MAX):
        raise ValueError(
            f"tokens_valid_after_ms 축 위반 — 밀리초여야 하는데 {tokens_valid_after_ms!r} "
            f"(초를 넘기지 않았는지 확인)"
        )


def derive_verdict(
    *,
    uid: str,
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

    ⚠️ **`epoch` fence는 이 함수가 지지 않는다.** 관측에 `epoch` 필드가 실려 있지만 여기서는
    **어떤 판정에도 쓰이지 않는다**(inertness 회귀 테스트로 잠금). 소비(lease 발급) 시점 fence는
    호출부/저장소 몫이다(§8.1 A4 "게시만이 아니라 소비까지 fence"). 즉 이 계층이 구조로 강제하는
    것은 **시간 신선도와 uid 결속**뿐이고, 세대(epoch) 결속은 **아직 호출부 약속**이다 —
    다음 슬라이스가 "이미 방어됨"으로 가정하지 말 것.

    Args:
        uid: 이 요청의 주체. 두 관측의 `uid`와 일치해야 한다.
        token_iat_seconds: 검증된 ID token의 `iat` 클레임(**초**). `auth_time`이 아니다.
    """
    _require_uid(uid, "derive_verdict.uid")
    # ⚠️ 주체 결속 — 관측이 **이 요청의 uid 것인지** 확인한다. 없으면 A의 identity + B의 premium을
    #    섞어 넣어도 그대로 Active가 나온다(무료 사용자가 타인의 구독으로 인가). 이건 데이터 상태가
    #    아니라 **배선 버그**이므로 verdict로 접지 않고 전파한다.
    for obs, label in ((premium, "premium"), (identity, "identity")):
        if obs is not None and obs.uid != uid:
            raise ValueError(f"derive_verdict: {label} 관측의 uid 불일치 — {obs.uid!r} != {uid!r}")

    if identity is None or not identity_observation_is_fresh(identity, now_mono=now_mono):
        return NeedsVerification(concern=Concern.IDENTITY)

    if isinstance(identity, IdentityAuthorityNotFound):
        return Inactive(reason=InactiveReason.ACCOUNT_DELETED)
    if identity.disabled:
        return Inactive(reason=InactiveReason.ACCOUNT_DISABLED)
    # ⚠️ `iat`(초) → ms 로 올려서 비교한다. strict `<` — 같으면 revoked 아님.
    _require_revocation_axes(token_iat_seconds, identity.tokens_valid_after_ms)
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
