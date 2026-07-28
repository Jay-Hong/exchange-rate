"""A6 strict verifier — WS 인가용 **3-state** 검증기.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A6 / A6-1 / A6-2 / C4 / D5.

    active(verified_at_monotonic) | inactive(reason) | temporarily_unavailable(retry_after_seconds)

`bool`로는 **"권한 없음"과 "확인 불가"를 구분할 수 없다**. 구분하지 못하면 C4(공급자 장애 시
registry 불변 + lease 미연장)가 성립하지 않고, 장애가 곧 대량 거부가 된다.

## 이 모듈이 하는 일

저장소(`app.strict_cache`)와 파생(`app.strict_authz.derive_verdict`) 사이를 잇는 **오케스트레이션**이다.
관측이 없거나 낡았을 때만 authority에 묻고, 그 결과를 관측으로 바꿔 CAS로 저장한 뒤 다시 파생한다.

## provider는 **주입**한다

이 모듈은 `firebase_admin`도 `httpx`도 최상위에서 import하지 않는다. 리포 관용구다 —
본체를 import chain에 묶으면 단위 테스트가 깨진다(`app/entitlements.py:3`). 덕분에 이 파일의
테스트는 네트워크·Firebase 없이 전부 돌아간다.

## 배선 슬라이스가 반드시 지켜야 할 것

1. **`VerifiedActive.snapshot`을 그대로 넘겨 fence한다** — `등록(비활성) → cache.is_current(snapshot)
   → 활성화`(§A4). ⛔ 호출자가 `cache.snapshot(uid)`을 **다시 뜨면 fence가 무의미해진다**:
   `snapshot()`은 없는 uid에 generation을 할당하는 **쓰기**라 다시 뜬 값은 언제나 '현행'이다.
2. **`StrictVerifierConfigError`는 타입으로 잡는다.** ⛔ `except Exception`으로 뭉개면 안 되고,
   반대로 **놓치면 연결이 끊기고 그 연결의 registry 항목이 통째로 지워진다**(C4의 "registry 불변"과
   정반대). 잡아서 `temporarily_unavailable` + 긴 `retry_after`로 접되, **전용 카운터와 ERROR 로그**를
   남긴다 — 이건 우리 설정 결함이라 사람이 봐야 한다.
3. **`retry_after_seconds`에 하향 jitter를 넣는다** — 여기서 주는 값은 기본값일 뿐이다. 전역 장애에서
   전원이 같은 초에 복귀하면 herd가 유지된다(§D6가 재인증 타이머에 `U(0,60)`을 넣은 것과 같은 이유).
4. **provider 호출에 시간 상한을 건다** — 이 루프에는 deadline이 없다. `httpx` timeout은 **단계별**이라
   왕복 총시간 상한이 아니고, `asyncio.to_thread`는 취소되지 않는다. §D6의 ack timeout(10초)보다
   길어지면 클라가 새 request_id로 재시도해 같은 uid의 검증이 **2배**가 된다(A5 전까지 특히).

## 이 슬라이스가 하지 않는 것

- **A5 single-flight** — 같은 uid의 동시 검증 합치기. 다음 슬라이스.
- **firebase 기반 identity provider 구현** — 배선 슬라이스.
- **lease 발급·registry 반영** — 배선 슬라이스. 소비 fence 계약은 §A4(등록 → `is_current` 1회
  재확인 → 활성화)를 따른다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from app.strict_authz import (
    Active,
    Concern,
    IdentityAuthorityFound,
    IdentityAuthorityNotFound,
    Inactive,
    InactiveReason,
    NeedsVerification,
    PremiumObservation,
    derive_verdict,
)
from app.strict_cache import PutAccepted, StrictObservationCache, StrictSnapshot
from app.subscription import (
    BadRequest,
    Determined,
    ProtocolViolation,
    ProviderMisconfigured,
    ProviderUnavailable,
)

# 클라가 다시 시도하기까지 기다릴 시간. §8-C의 `temporarily_unavailable`은 `retry_after_seconds`를
# **반드시** 동반한다. 값은 측정 전 보수적 기본값이고, C4에서 클라는
# `min(retry_after, lease_remaining - 30s)`로 쓴다.
# §D-const는 **정수 초, 1~30**을 규정한다. 서버가 산출해 보내는 값이라 하향 jitter도 서버 몫인데,
# jitter와 cause별 차등(429 `Retry-After` 반영 등)은 **wire 경계**의 일이다 — 여기선 기본값만 정한다.
TEMPORARILY_UNAVAILABLE_RETRY_AFTER_SECONDS = 5

# 외부 무효화 경쟁의 재시도 상한. **epoch 변화와 쓰기 거부 둘 다** 이 예산을 소비한다 —
# 둘 중 하나만 세면 나머지 경로가 예산 없이 무한히 돌 수 있다(실측으로 확인된 구멍).
MAX_CONTENTION_RETRIES = 3

# 논리 버그 대비 backstop. 위 규칙상 반복은 `(경쟁 예산+1) × concern 2개`로 이미 닫히므로
# 여기에 도달하면 **우리 버그**다 — 조용히 retryable로 접지 않고 시끄럽게 실패시킨다.
_ITERATION_BACKSTOP = 2 * (MAX_CONTENTION_RETRIES + 2) + 4


# ── identity authority 결과 (premium 쪽 typed provider와 대칭) ──────────────
@dataclass(frozen=True)
class IdentityFound:
    disabled: bool
    tokens_valid_after_ms: int


@dataclass(frozen=True)
class IdentityNotFound:
    """계정이 없다 — 삭제됐거나 uid가 재생성됐다."""


@dataclass(frozen=True)
class IdentityUnavailable:
    """판정 **불가** — Firebase 장애·타임아웃. 거부가 아니다."""

    code: str = ""


@dataclass(frozen=True)
class IdentityMisconfigured:
    """**영구** 결함 — 서비스 계정 권한 상실, 자격증명 폐기, 프로젝트 오설정.

    premium 쪽 `ProviderMisconfigured`와 대칭으로 존재해야 한다. 이 버킷이 없으면 adapter가
    영구 결함을 `IdentityUnavailable`로 접고, 전 사용자가 `retry_after` 주기로 **영원히**
    재시도한다 — §A6-1이 막으려는 바로 그 storm이다.
    """

    code: str = ""


IdentityResult = Union[
    IdentityFound, IdentityNotFound, IdentityUnavailable, IdentityMisconfigured
]


# ── 검증 결과 3-state ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class VerifiedActive:
    """**이것만 horizon을 연장한다**(§A6). 두 시각은 A1의 3-way min 입력이다.

    ⚠️ `snapshot`은 **이 판정을 만든 바로 그 스냅샷**이다. §A4의 소비 fence
    (`등록(비활성) → is_current 1회 재확인 → 활성화`)는 이 객체를 그대로 넘겨야 성립한다 —
    호출자가 `cache.snapshot(uid)`을 **다시 뜨면** 그 사이의 무효화를 못 보고, 게다가
    `snapshot()`은 없는 uid에 generation을 할당하는 **쓰기**라 fence가 자기 자신을 통과시킨다.
    """

    snapshot: StrictSnapshot
    premium_verified_at_mono: float
    identity_verified_at_mono: float


@dataclass(frozen=True)
class VerifiedInactive:
    """권한 **없음**이 확인됐다 — 즉시 거부. 재시도해도 같다."""

    reason: InactiveReason


@dataclass(frozen=True)
class TemporarilyUnavailable:
    """**판정 불가** — 거부가 아니다.

    §C4: registry 불변 + lease 미연장. 기존 lease는 원래 시각에 만료되므로 장애가 길어지면
    자연히 fail-closed로 수렴한다. 이걸 `VerifiedInactive`로 접으면 공급자 장애가 곧
    **대량 강제 로그아웃**이 된다.
    """

    concern: Concern
    retry_after_seconds: int = TEMPORARILY_UNAVAILABLE_RETRY_AFTER_SECONDS

    def __post_init__(self) -> None:
        # §D-const: 정수 초, 1~30. 클라는 Int로 디코드한다.
        if not isinstance(self.retry_after_seconds, int) or isinstance(self.retry_after_seconds, bool):
            raise ValueError(f"retry_after_seconds는 정수여야 한다: {self.retry_after_seconds!r}")
        if not 1 <= self.retry_after_seconds <= 30:
            raise ValueError(f"retry_after_seconds는 1~30이어야 한다: {self.retry_after_seconds}")


StrictVerification = Union[VerifiedActive, VerifiedInactive, TemporarilyUnavailable]


class StrictVerifierConfigError(RuntimeError):
    """공급자 설정·계약 위반. **verdict가 아니다**(§A6-1).

    ⛔ 이걸 `TemporarilyUnavailable`로 바꾸지 말 것 — API key 오설정이나 응답 스키마 붕괴는
    재시도해도 낫지 않는데 retryable로 접으면 전 사용자가 무한 재시도 storm을 만든다.
    캐시하지도 않고 wire 오류로도 접지 않는다. 위로 전파해 **시끄럽게** 실패시킨다.
    """


async def verify_strict(
    uid: str,
    *,
    token_iat_seconds: int,
    clock,
    cache: StrictObservationCache,
    premium_provider,
    identity_provider,
) -> StrictVerification:
    """관측이 부족하면 authority에 물어 채운 뒤 3-state를 돌려준다.

    루프의 모양:

        snapshot(epoch 캡처) → derive → 부족하면 그 concern만 authority 조회
        → 관측으로 변환 → CAS 저장 → **다시 snapshot부터** 파생

    ⚠️ 매 반복마다 snapshot을 **새로** 뜬다. I/O를 가로지른 로컬 관측을 재사용하면 그 사이의
    무효화를 못 보고, 동시에 존재한 적 없는 쌍으로 판정하게 된다.
    """
    contention_left = MAX_CONTENTION_RETRIES
    written_in_epoch: set = set()
    seen_epoch = None
    last_concern = Concern.IDENTITY   # identity가 먼저이므로 최초 귀속도 identity다

    for _ in range(_ITERATION_BACKSTOP):
        snapshot = cache.snapshot(uid)

        if snapshot.epoch != seen_epoch:
            if seen_epoch is not None:
                # 외부 무효화가 들어왔다. **쓰기 거부와 똑같이** 예산을 소비한다 —
                # 거부만 세면 "쓰기는 성공했는데 그 직후 bump" 경로가 예산 없이 무한히 돈다.
                contention_left -= 1
                if contention_left < 0:
                    return TemporarilyUnavailable(concern=last_concern)
            seen_epoch = snapshot.epoch
            written_in_epoch = set()

        verdict = derive_verdict(
            uid=uid,
            now_mono=clock.mono(),
            premium=snapshot.premium,
            identity=snapshot.identity,
            token_iat_seconds=token_iat_seconds,
        )

        if isinstance(verdict, Active):
            return VerifiedActive(
                snapshot=snapshot,
                premium_verified_at_mono=verdict.premium_verified_at_mono,
                identity_verified_at_mono=verdict.identity_verified_at_mono,
            )
        if isinstance(verdict, Inactive):
            return VerifiedInactive(reason=verdict.reason)

        assert isinstance(verdict, NeedsVerification)  # 3-state union이라 남는 경우가 없다
        concern = verdict.concern
        last_concern = concern

        if concern in written_in_epoch:
            # 같은 epoch에서 **성공적으로 기록한** 관측이 곧바로 다시 낡았다고 나온다. 쓰기와
            # 다음 파생 사이엔 로컬 연산뿐이라 이건 경쟁이 아니라 **우리 설정·논리 오류**다
            # (freshness horizon이 0 이하이거나 시간 축이 어긋난 경우).
            # ⛔ 이걸 `TemporarilyUnavailable`로 접으면 §A6-1이 금지한 재시도 storm이 된다 —
            # 실측: horizon을 0으로 두자 **요청 1건당 RevenueCat 6회** 후 retryable 반환이었다.
            #
            # ⚠️ **쓰기가 거부된 경우는 여기 해당하지 않는다** — 그땐 관측이 반영되지 않았으므로
            # 같은 concern을 다시 조회하는 게 정상이고, 경쟁 예산이 그 반복을 제한한다.
            raise StrictVerifierConfigError(
                f"{concern.value} 관측을 같은 epoch에서 다시 검증하라고 나온다 — "
                "freshness horizon이 0 이하이거나 시간 축이 어긋났다"
            )

        # ⚠️ 조회 결과가 판정 불가면 **아무것도 저장하지 않는다**.
        # 판정 불가를 저장하면 그 창 동안 재시도가 무의미해진다.
        if concern is Concern.IDENTITY:
            observation = _identity_observation(
                await identity_provider(uid), uid=uid, snapshot=snapshot, clock=clock
            )
        else:
            observation = _premium_observation(
                await premium_provider(uid), uid=uid, snapshot=snapshot, clock=clock
            )
        if observation is None:
            return TemporarilyUnavailable(concern=concern)

        put = (
            cache.put_identity(observation, expected_epoch=snapshot.epoch)
            if concern is Concern.IDENTITY
            else cache.put_premium(observation, expected_epoch=snapshot.epoch)
        )
        if isinstance(put, PutAccepted):
            written_in_epoch.add(concern)
            continue  # 진행 — 다음 반복이 나머지 concern을 본다
        contention_left -= 1
        if contention_left < 0:
            return TemporarilyUnavailable(concern=concern)

    # 위 규칙상 반복은 `(경쟁 예산+1) × concern 2개`로 닫힌다. 여기 오면 **우리 버그**다.
    raise StrictVerifierConfigError(
        "verify_strict가 종료 조건에 도달하지 못했다 — 루프 불변식이 깨졌다"
    )


def _identity_observation(result: IdentityResult, *, uid: str, snapshot, clock):
    """authority 응답 → 저장할 관측. 판정 불가면 None."""
    now_mono = clock.mono()
    if isinstance(result, IdentityFound):
        return IdentityAuthorityFound(
            uid=uid,
            disabled=result.disabled,
            tokens_valid_after_ms=result.tokens_valid_after_ms,
            verified_at_mono=now_mono,
            epoch=snapshot.epoch,
        )
    if isinstance(result, IdentityNotFound):
        return IdentityAuthorityNotFound(uid=uid, verified_at_mono=now_mono, epoch=snapshot.epoch)
    if isinstance(result, IdentityUnavailable):
        return None
    if isinstance(result, IdentityMisconfigured):
        # premium 쪽 `ProviderMisconfigured`와 대칭 — 영구 결함은 verdict가 아니다(§A6-1).
        raise StrictVerifierConfigError(f"identity provider 설정 오류: {result!r}")
    raise StrictVerifierConfigError(f"identity provider가 모르는 값을 돌려줬다: {result!r}")


def _premium_observation(result, *, uid: str, snapshot, clock):
    """authority 응답 → 저장할 관측. 판정 불가면 None. 설정·계약 오류면 raise."""
    if isinstance(result, Determined):
        return PremiumObservation(
            uid=uid,
            active=result.is_premium,
            verified_at_mono=clock.mono(),
            epoch=snapshot.epoch,
        )
    if isinstance(result, ProviderUnavailable):
        return None
    if isinstance(result, (ProviderMisconfigured, BadRequest, ProtocolViolation)):
        # §A6-1 — 설정·계약 오류는 verdict가 아니다. 재시도해도 낫지 않는다.
        raise StrictVerifierConfigError(f"entitlement provider 설정·계약 오류: {result!r}")
    raise StrictVerifierConfigError(f"entitlement provider가 모르는 값을 돌려줬다: {result!r}")
