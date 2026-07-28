"""WS 구독 lease의 **순수 계산 primitive** (ADR-039 §8.1 A1 horizon / A2 만료 판정).

bounded-lease v1(제품 결정 S5)의 산술만 담는다. registry 조작·전송 인가·sweep은
`app/topic_dispatcher.py` 소유이고, strict cache(`verified_at_monotonic` 저장) / A5
single-flight / A6 3-state verifier / `lease_id` 발급은 **후속 슬라이스**다.

## A1 — lease는 "부여 시점"이 아니라 "마지막 authoritative 확인 시점"에 묶인다

    lease_expires_at = min(now, premium_verified_at, firebase_identity_verified_at) + LEASE_MAX

권한 상실 시각 L 이전의 마지막 authoritative 확인 V(≤ L)로 부여된 lease는 늦어도
`V + 15분 ≤ L + 15분`에 만료된다 → **총 revoke 상한이 정확히 15분**. entitlement든
identity(revocation)든 authoritative한 것은 모두 같은 horizon 모델을 탄다(특례 없음).

## 시간 축 규약 — `_mono` 접미사

이 리포는 `now: float` 한 이름을 두 축에 쓴다(P1b retry 정책의 `decide_retry(now=)`는 monotonic,
`app/crawlers/krx_kis.py`의 `evaluate(now_epoch=)`는 wall epoch). A1의 상한 증명은 축 일치에
전적으로 의존하므로 **이 모듈의 시각 파라미터는 전부 `_mono` 접미사**를 단다.
새 코드도 monotonic 축이면 이 접미사를 쓸 것.
(⚠️ P1b 계열을 심볼로만 가리키는 이유: 그 계열 dormancy trip-wire가 **문자열 리터럴까지**
위반으로 세므로 모듈 파일명을 docstring에 적을 수 없다.)

## 왜 `Clock`을 받지 않고 float를 받는가

D1("한 ack 안의 accepted들은 **같은 순간** 갱신")과 D2("`active_subscriptions`는 lock 아래
**단일 snapshot**에서 생성한다 — topic마다 다른 시점에 읽으면 모순된 상태를 내보낸다")를
지키려면 `now_mono`를 **호출부가 요청당 1회** 읽어 그 요청의 전 topic에 공유해야 한다.
계산기가 스스로 시각을 만들면 그 성질을 단위 테스트로 잠글 수 없다. harness 선행 (5)의
`sweep_once(now)`와 P1b retry 정책("모든 time은 injected now")도 같은 형태다.

    # 호출부 계약 (B4 lock 진입 후)
    now = clock.mono()                      # ← 요청당 1회
    expiry = compute_lease_expiry(now_mono=now, ...)   # 전 topic 공유
    # 단, B1 전송 직전 재검증은 **새로** 읽는다(그 시점의 만료 여부를 봐야 하므로).

## 이 모듈이 막지 못하는 것 (호출부 precondition)

세 인자가 **모두** wall epoch로 들어오면 서로의 관계가 정상이라 gross-skew 가드를 통과한다
(결과는 epoch+900이 되어 진짜 monotonic now와 비교 시 수십 년간 미만료). 축 보증은 배선
슬라이스가 져야 한다 — 세 값이 같은 주입 `Clock.mono`에서 나오고, A6의 `active`만 계산기에
도달하며, 이미 만료로 계산되면 등록·ack accepted를 하지 않는다는 것까지.
"""
from __future__ import annotations

import math

# 제품 결정 S5(2026-07-25). **상한**이며 실제 부여 lease는 3-way min으로 가변이다
# (§8-B ack 예시의 `lease_duration_seconds`도 900이 아니라 660) — wire의 duration을
# 이 상수로 채우면 안 된다. 항상 expiry에서 파생할 것.
#
# env가 아닌 코드 상수인 이유: (1) 불변식 `CACHE_TTL < LEASE`의 상대 피연산자가
# `app/subscription.py`의 모듈 상수인데 `app/config.py`는 app을 import하지 않아 리포의
# 표준 env-guard 자리에 놓을 수 없다, (2) 운영 레버가 아니다 — 낮추면 재인증 storm,
# 높이면 S5의 15분 revoke 상한이라는 보안 보증 자체가 흔들린다(A3).
LEASE_MAX_SECONDS = 900.0

# 관측 시각이 `now`보다 미래일 수 있는 허용 폭. 값은 lease 상한에서 **파생**된다 —
# `now + LEASE_MAX` 를 넘는 관측은 3-way min의 출력상 상한과 구별되지 않으므로,
# 거부해도 표현력 손실이 0이고 대신 축 혼동(wall epoch 혼입)을 시끄럽게 만든다.
# ⚠️ 이름을 분리해 둔 이유: "lease 상한"과 "허용 skew"는 서로 다른 정책 지식이다.
#    한쪽만 바꿔야 할 날이 오면 이 유도 관계를 끊을 것.
MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS = LEASE_MAX_SECONDS


def compute_lease_expiry(
    *,
    now_mono: float,
    premium_verified_at_mono: float,
    firebase_identity_verified_at_mono: float,
) -> float:
    """A1 3-way horizon. 반환값은 monotonic 축의 만료 시각.

    Args:
        now_mono: 요청 경계에서 **1회** 읽은 `clock.mono()`.
        premium_verified_at_mono: RevenueCat entitlement를 authoritative하게 관측한 시각.
            **UID 단위**가 맞다 — 권한은 계정 속성이다. stale fallback 결과로는 갱신하지 않는다.
        firebase_identity_verified_at_mono: 계정 권위(`get_user`) 관측 시각.
            **UID 단위 저장이 맞다** — 저장하는 것이 *verdict*가 아니라 *관측*(disabled +
            `tokens_valid_after_ms`)이고, 토큰별 판정은 요청마다 `iat`로 파생하기 때문이다
            (`app/strict_authz.py`). 그래서 revoked된 구 토큰과 새 토큰이 **같은 record에서
            서로 다른 verdict**를 얻어 A1의 권한 혼입 우려가 구조적으로 해소된다.
            ⛔ 구 서술("검증된 토큰 fingerprint/`auth_time` 단위로 저장")은 **폐기**됐다 —
            SDK 술어가 `auth_time`이 아니라 `iat`이고(§8.1 A1), 토큰 단위 키는 불필요하다.

    Raises:
        ValueError: 비유한 입력, 또는 관측 시각이 `now`보다
            `MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS`를 넘어 미래인 경우(축 혼동 의심).

    Note:
        A1 원식은 세 항에 각각 `+ LEASE_MAX`를 더한 뒤 min을 취한다. IEEE754 덧셈이 단조라
        축약형과 **bit-for-bit 동등**하지만, 그 동등성은 **세 항의 horizon 상수가 동일할
        때만** 성립한다 — 항별 horizon이 갈리는 날엔 원식으로 되돌릴 것.
    """
    # 비유한 값 fail-closed. `min`은 NaN을 **첫 인자일 때만** 전파하므로
    # (`min(1.0, nan, 2.0) == 1.0`) 가드가 없으면 NaN horizon이 조용히 무시되고
    # 상한 전량 lease가 나간다. `inf` sentinel은 영구 lease가 된다.
    if not (
        math.isfinite(now_mono)
        and math.isfinite(premium_verified_at_mono)
        and math.isfinite(firebase_identity_verified_at_mono)
    ):
        raise ValueError(
            "compute_lease_expiry: 세 시각 모두 finite여야 — "
            f"now={now_mono!r} premium={premium_verified_at_mono!r} "
            f"identity={firebase_identity_verified_at_mono!r}"
        )

    # 관측은 판정보다 앞선다 — 크게 미래인 값은 skew가 아니라 축 위반이다(예: `time.time()`
    # 결과가 섞이면 min이 now를 골라 **항상 상한 전량** lease가 나가고 A1의 상한 증명이
    # 조용히 무효가 된다). 미세한 미래는 min이 clamp하므로 그대로 통과시킨다.
    horizon_ceiling = now_mono + MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS
    if premium_verified_at_mono > horizon_ceiling:
        raise ValueError(
            "compute_lease_expiry: premium_verified_at_mono가 now보다 "
            f"{MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS}s 넘게 미래 — monotonic 축 위반 의심 "
            f"(got {premium_verified_at_mono!r}, now={now_mono!r})"
        )
    if firebase_identity_verified_at_mono > horizon_ceiling:
        raise ValueError(
            "compute_lease_expiry: firebase_identity_verified_at_mono가 now보다 "
            f"{MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS}s 넘게 미래 — monotonic 축 위반 의심 "
            f"(got {firebase_identity_verified_at_mono!r}, now={now_mono!r})"
        )

    return min(
        now_mono, premium_verified_at_mono, firebase_identity_verified_at_mono
    ) + LEASE_MAX_SECONDS


def is_expired(*, now_mono: float, expires_at_mono: float) -> bool:
    """A2 만료 판정 — **경계 포함**(`now >= expires_at`)이 계약이다(fail-closed).

    비유한 입력은 "판정 불가"이므로 만료로 접는다. ⚠️ 이 선검사를 빼고
    `not (now_mono < expires_at_mono)` 형태로 NaN만 닫으려 하면 `now = -inf`와
    `expires_at = +inf`에서 **fail-open**한다(둘 다 "미만료"). 유한 구간에서 두 형태는
    동치라 경계 테스트로는 그 회귀가 드러나지 않는다.
    """
    if not (math.isfinite(now_mono) and math.isfinite(expires_at_mono)):
        return True
    return now_mono >= expires_at_mono
