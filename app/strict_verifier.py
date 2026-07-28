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
2. **`StrictVerifierConfigError`를 타입으로 잡아 변환한다** (정책은 그 클래스 docstring에 정본).
   ⛔ `except Exception`으로 뭉개지 말 것(§A6-1이 금지하는 조용한 세탁) — 그렇다고 **놓치면 더
   나쁘다**: `main.py:998`→`finally:1006`이 그 연결의 registry 구독을 통째로 지우고 클라엔 오류
   프레임도 안 간다. 타입으로 잡아 `temporarily_unavailable` + `retry_after`를 상한값으로 주고,
   **transient와 분리된 카운터 + ERROR 로그**를 남긴다. registry와 기존 lease는 건드리지 않는다.
3. **`retry_after_seconds`에 하향 jitter를 넣는다** — 여기서 주는 값은 기본값일 뿐이다. 전역 장애에서
   전원이 같은 초에 복귀하면 herd가 유지된다(§D6가 재인증 타이머에 `U(0,60)`을 넣은 것과 같은 이유).
4. **provider가 자기 작업에 스스로 상한을 건다**(SDK/transport timeout). 이 루프의
   `VERIFY_DEADLINE_SECONDS`는 **호출자**만 풀어 준다 — `asyncio.to_thread`의 실제 작업은
   취소되지 않고, `httpx` timeout도 **단계별**이라 왕복 총시간 상한이 아니다.
   ⚠️ **그래서 provider가 끝나지 않으면 그 (uid, epoch, concern)은 영구히 열등 상태다**:
   호출자는 매번 `temporarily_unavailable`을 받고(=매달리지 않고) 작업도 곱해지지 않지만,
   **복구는 오직 provider가 스스로 끝날 때만** 일어난다(실측: 4회 시도 → provider 호출 1,
   `in_flight`은 1로 유지). Firebase adapter는 동기 SDK를 `to_thread`로 감쌀 예정이므로
   여기서 timeout을 반드시 구현하고, **타임아웃 후 `in_flight_count()==0`과 다음 호출 성공**을
   통합 테스트로 잠가야 이 항목이 완전히 닫힌다.

## 이 슬라이스가 하지 않는 것

- **firebase 기반 identity provider 구현** — 다음 슬라이스. 위 4번이 그 슬라이스의 필수 과제다.
- **lease 발급·registry 반영** — 배선 슬라이스. 소비 fence 계약은 §A4(등록 → `is_current` 1회
  재확인 → 활성화)를 따른다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
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
from app.strict_single_flight import SharedFlightCancelled
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

# **한 요청 전체**의 시간 상한(§D6의 ack timeout 10초 아래). 호출별 상한이 아니다.
# ⚠️ 구 주석은 "호출 2번이니 4초×2=8초"라고 적었는데 **틀렸다** — 무효화 경쟁이 끼면 같은
# concern을 다시 조회하므로 호출이 3회 이상 될 수 있다. 실측(축소): 호출별 60ms 상한인데
# 전체 154ms / 조회 3회. 그래서 호출별이 아니라 **절대 monotonic deadline**으로 잡는다.
VERIFY_DEADLINE_SECONDS = 8.0


class _FlightOutcome(str, Enum):
    """flight의 **종결 방식**. 관측이 아니라서 대기자와 공유해도 판정이 섞이지 않는다."""

    STORED = "stored"            # 관측이 저장됐다 → 저장소에서 다시 읽어 재파생
    UNAVAILABLE = "unavailable"  # authority 판정 불가 → 그대로 TemporarilyUnavailable


class ConfigFaultKind(str, Enum):
    """설정·계약 결함의 **경계 있는** 분류.

    대응 주체가 다르다 — 죽은 API key는 즉시 호출, 스키마 drift는 계약 재검토다. 메시지 문자열은
    `repr(result)`를 담아 cardinality가 열려 있으므로 **카운터는 이 enum으로** 집계해야 한다
    (계획이 webhook event type에 같은 규율을 요구한다).
    """

    IDENTITY_MISCONFIGURED = "identity_misconfigured"
    IDENTITY_UNKNOWN_VALUE = "identity_unknown_value"
    PREMIUM_MISCONFIGURED = "premium_misconfigured"
    PREMIUM_BAD_REQUEST = "premium_bad_request"
    PREMIUM_PROTOCOL_VIOLATION = "premium_protocol_violation"
    PREMIUM_UNKNOWN_VALUE = "premium_unknown_value"


@dataclass(frozen=True)
class ConfigFault:
    """flight가 나르는 설정 결함. **frozen**이라 공유해도 `__context__`가 얹히지 않는다.

    ⚠️ 이걸 값으로 나르지 않으면 대기자들은 owner의 closure를 못 보고 **내용 없는 stub**을
    받는다 — 실측: 6건 중 5건이 provider payload도 HTTP status도 없는 문자열이었다.
    운영자 신호가 로그인데 83%가 비면 계약이 깨진 것이다.
    """

    kind: ConfigFaultKind
    message: str


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

    ## 정책 (2026-07-28 확정 — 층을 나눠서 읽어야 한다)

    - **이 모듈은 절대 변환하지 않는다.** `TemporarilyUnavailable`을 돌려주지도, 캐시하지도
      않는다. API key 오설정이나 응답 스키마 붕괴는 재시도해도 낫지 않아서, 검증기가 이걸
      retryable 결과로 접으면 전 사용자가 무한 재시도 storm을 만든다. 그래서 **raise**한다.
    - **배선 경계는 타입으로 잡아 반드시 변환한다.** 그냥 전파시키면 안 된다 — 실측:
      `app/main.py:998`의 `except Exception`이 루프를 빠져나가고 `finally`(:1003-1007)가
      `registry.remove_websocket()`을 불러 **그 연결의 구독이 통째로 삭제**되며, 클라에는
      오류 프레임조차 가지 않는다. C4의 "registry 불변"과 정반대이고, 클라의 즉시 재연결이
      retry_after보다 빨라 **storm이 오히려 더 조인다**.

    두 문장은 모순이 아니다 — §A6-1이 금지하는 것은 *광범위 `except Exception`에 의한 조용한
    세탁*이고, 여기서 요구하는 것은 *타입 기반의 의도된 변환 + 계측*이다. §8-C의
    `temporarily_unavailable` 정의 자체가 "인증·권한을 **판정할 수 없음**"이라 죽은 API key도
    그 정의에 정확히 들어간다. 운영자 신호는 wire가 아니라 **전용 카운터와 ERROR 로그**가 낸다.

    잠글 성질 3개(배선 슬라이스의 mutation 대상): (a) 이 모듈 안에 광범위 `except`가 없다,
    (b) 배선의 catch가 **타입 기반**이고 transient와 **다른 카운터**를 올린다,
    (c) 설정 오류가 registry 항목과 기존 lease를 **바꾸지 않는다**.

    `kind`는 경계 있는 분류다 — **카운터는 이걸로** 집계하고 메시지는 로그에만 쓴다.
    """

    def __init__(self, message: str, *, kind: ConfigFaultKind) -> None:
        super().__init__(message)
        self.kind = kind


async def verify_strict(
    uid: str,
    *,
    token_iat_seconds: int,
    clock,
    cache: StrictObservationCache,
    premium_provider,
    identity_provider,
    single_flight=None,
) -> StrictVerification:
    """관측이 부족하면 authority에 물어 채운 뒤 3-state를 돌려준다.

    루프의 모양:

        snapshot(epoch 캡처) → derive → 부족하면 그 concern만 authority 조회
        → 관측으로 변환 → CAS 저장 → **다시 snapshot부터** 파생

    ⚠️ 매 반복마다 snapshot을 **새로** 뜬다. I/O를 가로지른 로컬 관측을 재사용하면 그 사이의
    무효화를 못 보고, 동시에 존재한 적 없는 쌍으로 판정하게 된다.
    """
    deadline_mono = clock.mono() + VERIFY_DEADLINE_SECONDS
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
        put_result: dict = {}

        async def _fetch_and_store():
            """A5가 합치는 단위 — authority 조회 + CAS 저장. **종결 방식**을 돌려준다."""
            provider = identity_provider if concern is Concern.IDENTITY else premium_provider
            to_observation = (
                _identity_observation if concern is Concern.IDENTITY else _premium_observation
            )
            # ⚠️ 여기엔 상한을 걸지 않는다. `wait_for`는 `asyncio.to_thread`의 **실제 작업을
            # 멈추지 못한다** — Python은 스레드를 취소할 수 없다. factory 안에서 상한을 걸면
            # 슬롯만 비고 스레드는 계속 돌아, 다음 요청이 **같은 작업을 또 시작**한다
            # (실측: 실행 중 스레드 1 → 2). t3.small의 기본 executor는 6 스레드라 몇 번만
            # 반복돼도 리포 전역의 `to_thread` 86곳이 함께 막힌다.
            # → 상한은 **호출자 쪽**에 건다(아래). flight는 실제 작업이 끝날 때까지 슬롯을
            #   쥐고 있으므로 뒤이은 요청은 새 스레드를 만들지 않고 **같은 flight에 붙는다**.
            # ⚠️ 그래도 provider는 **자기 작업에 스스로 상한을 걸어야 한다**(SDK timeout).
            #   이건 우리가 강제할 수 없는 provider 계약이다.
            raw = await provider(uid)
            try:
                obs = to_observation(raw, uid=uid, snapshot=snapshot, clock=clock)
            except StrictVerifierConfigError as exc:
                # ⚠️ 예외 **인스턴스**를 공유하면 배선이 그 위에 `__context__`를 얹어 한 연결의
                # 로그에 다른 연결의 상태가 찍힌다. 그래서 frozen 값으로 바꿔 나른다 —
                # 그러면 대기자도 owner와 **같은 내용**을 받으면서 각자 자기 인스턴스를 만든다.
                return ConfigFault(kind=exc.kind, message=str(exc))
            if obs is None:
                return _FlightOutcome.UNAVAILABLE
            put_result["put"] = (
                cache.put_identity(obs, expected_epoch=snapshot.epoch)
                if concern is Concern.IDENTITY
                else cache.put_premium(obs, expected_epoch=snapshot.epoch)
            )
            return _FlightOutcome.STORED

        remaining = deadline_mono - clock.mono()
        if remaining <= 0:
            # ⚠️ 이 명시 검사는 **오늘 기준 중복**이다 — 아래 `wait_for`에 음수 timeout이 가도
            # 같은 결과가 나오고 provider 호출도 늘지 않는다(실측: 둘 다 `calls=ii`).
            # 그래도 남긴다: `wait_for(timeout<=0)`의 거동에 의존하지 않고 의도를 코드로 드러내기
            # 위해서다. 필요하다고 주장하지는 않는다(mutation 생존을 인정한다).
            return TemporarilyUnavailable(concern=concern)
        try:
            if single_flight is None:
                flight_outcome = await asyncio.wait_for(_fetch_and_store(), timeout=remaining)
            else:
                flight_outcome = await asyncio.wait_for(
                    single_flight.run((uid, snapshot.epoch, concern), _fetch_and_store),
                    timeout=remaining,
                )
        except asyncio.TimeoutError:
            # 호출자만 포기한다. flight는 계속 돌며 슬롯을 쥐고 있으므로 작업이 곱해지지 않는다.
            return TemporarilyUnavailable(concern=concern)
        except SharedFlightCancelled:
            # ⚠️ 공유 flight가 취소됐다. 이걸 그냥 올려보내면 `main.py:998`의 `except Exception`에
            # 걸려 **이 연결의 registry 구독이 삭제**된다(C4 위반). 도메인 오류로 바꾸는 것만으론
            # 부족했고 — **여기서 3-state로 접어야** 비로소 닫힌다.
            return TemporarilyUnavailable(concern=concern)

        if isinstance(flight_outcome, ConfigFault):
            raise StrictVerifierConfigError(flight_outcome.message, kind=flight_outcome.kind)
        if flight_outcome is _FlightOutcome.UNAVAILABLE:
            # ⚠️ 합쳐진 대기자도 **여기서 끝낸다**. 저장소를 다시 보게 하면 "아무것도 없네"를 보고
            # 각자 다시 owner가 되어, 장애 때 합치기가 사라지고 반복 예산까지 태운다(실측).
            return TemporarilyUnavailable(concern=concern)

        if "put" not in put_result:
            continue  # 다른 요청이 저장을 마쳤다 — 저장소에서 확인하러 돌아간다
        if isinstance(put_result["put"], PutAccepted):
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
        raise StrictVerifierConfigError(
            f"identity provider 설정 오류: {result!r}", kind=ConfigFaultKind.IDENTITY_MISCONFIGURED
        )
    raise StrictVerifierConfigError(
        f"identity provider가 모르는 값을 돌려줬다: {result!r}",
        kind=ConfigFaultKind.IDENTITY_UNKNOWN_VALUE,
    )


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
        kind = {
            ProviderMisconfigured: ConfigFaultKind.PREMIUM_MISCONFIGURED,
            BadRequest: ConfigFaultKind.PREMIUM_BAD_REQUEST,
            ProtocolViolation: ConfigFaultKind.PREMIUM_PROTOCOL_VIOLATION,
        }[type(result)]
        raise StrictVerifierConfigError(
            f"entitlement provider 설정·계약 오류: {result!r}", kind=kind
        )
    raise StrictVerifierConfigError(
        f"entitlement provider가 모르는 값을 돌려줬다: {result!r}",
        kind=ConfigFaultKind.PREMIUM_UNKNOWN_VALUE,
    )
