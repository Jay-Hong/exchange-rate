"""WS 구독 lease의 **순수 계산 primitive** (ADR-039 §8.1 A1 horizon / A2 만료 판정).

bounded-lease v1(제품 결정 S5)의 산술만 담는다. registry 조작·전송 인가·sweep 은
`app/topic_dispatcher.py` 소유다.

⛔ 한때 여기 "strict cache / A5 single-flight / A6 3-state verifier 는 **후속 슬라이스**"라고
적었는데 **낡았다** — 그 설계는 **ADR-040 리셋에서 폐기**됐다. 현재 계약은 소비자가 생긴 뒤에만
만드는 것이고, 관측 캐시는 실제 병목을 측정하기 전에는 넣지 않는다. (`lease_id` 발급은 land 됐다.)

## A1 — lease는 "부여 시점"이 아니라 "마지막 authoritative 확인 시점"에 묶인다

    lease_expires_at = min(now, *authoritative_관측_시각들) + LEASE_MAX

권한 상실 시각 L 이전의 마지막 authoritative 확인 V(≤ L)로 부여된 lease 는 늦어도
`V + 15분 ≤ L + 15분`에 만료된다. authoritative 한 것은 모두 같은 horizon 모델을 탄다(특례 없음).

⛔ **정확한 문장은 "판정 상한"이다.** 한때 "총 revoke 상한이 정확히 15분"이라고 적었는데,
그건 **배달 상한처럼 읽힌다**. 15분이 제한하는 것은 **새로운 전송 인가**다:

> 권한 상실 전 마지막 authoritative 관측으로부터 **15분 이내에 새로운 전송 인가가 중단된다.
> 이미 인가를 통과한 전송의 배달 완료 시각에는 별도 상한이 없다.**

`leased_subscribers()` 가 만료를 걸러도, 그 직전에 통과해 이미 `await send` 로 들어간 프레임을
소급 취소하지는 않는다. 이 트랙은 같은 "판정 상한 vs 배달 상한" 혼동을 이미 한 번 겪었다.

⚠️ **축 개수는 topic 마다 다르고, 이름이 다르면 권위도 다르다**:

| 계산기 | 축 |
| --- | --- |
| `compute_identity_only_lease_expiry` | Firebase identity |
| `compute_lease_expiry` | + RevenueCat **premium** |
| `compute_gated_lease_expiry` | + KRX **entitlement**(우리 DB row) |

⛔ 축을 줄여 쓰려고 **다른 축의 인자에 값을 밀어 넣지 말 것**(예: `min(premium, entitlement)` 를
`premium_verified_at_mono` 에 전달). 수학적으로 같아도 그 인자의 **정의가 거짓**이 된다 —
이 모듈은 같은 형태의 거짓말을 이미 두 번 고쳤다(premium 자리에 `now` / 4축을 3축에 압축).

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

⚠️ 아래는 `compute_lease_expiry`(3축) 기준 서술이지만, **축 개수와 무관하게** 성립한다.
관측 시각들이 **모두** wall epoch로 들어오면 서로의 관계가 정상이라 gross-skew 가드를 통과한다
(결과는 epoch+900이 되어 진짜 monotonic now와 비교 시 수십 년간 미만료). 축 보증은 배선
슬라이스가 져야 한다 — 모든 값이 같은 주입 `Clock.mono`에서 나오고, **판정기가 통과시킨**
결과(`app/topic_authorization.py` 의 `Granted`)만 계산기에
도달하며, 이미 만료로 계산되면 등록·ack accepted를 하지 않는다는 것까지.
"""
from __future__ import annotations

import math
from typing import Mapping

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


def _lease_expiry_from_observations(
    *,
    now_mono: float,
    observations: "Mapping[str, float]",
) -> float:
    """**공통 계산 primitive** — 이름표가 붙은 관측 시각들의 `min(now, *관측) + LEASE_MAX`.

    ⛔ 축 개수만 다른 계산기를 복제하지 않기 위해 뽑았다. 이름표는 **오류 메시지 전용**이다 —
    어느 축이 축 위반인지 말하지 못하면 wall epoch 혼입을 진단할 수 없다.

    ⚠️ A1 원식은 각 항에 `+ LEASE_MAX` 를 더한 뒤 min 이지만, IEEE754 덧셈이 단조라 축약형과
    **bit-for-bit 동등**하다. 그 동등성은 **모든 항의 horizon 상수가 같을 때만** 성립하므로,
    축을 늘릴 때 상수를 축마다 다르게 주려면 이 함수부터 갈라야 한다.
    """
    # ⛔ 관측이 **하나도 없으면** lease 를 계산할 근거가 없다. 방치하면
    #    `min(now_mono)` 가 `TypeError: 'float' object is not iterable` 로 죽는다 —
    #    fail-open 은 아니지만(값을 돌려주지 않는다) 호출부 실수를 **쓸모없는 메시지**로
    #    알려 준다. 여기서 명시적으로 접는다(Crash Early).
    if not observations:
        raise ValueError("lease 계산에 관측 시각이 하나도 없다 — 축을 빠뜨린 호출이다")

    if not math.isfinite(now_mono) or not all(
        math.isfinite(v) for v in observations.values()
    ):
        raise ValueError(
            "lease 관측 시각은 모두 finite여야 — "
            f"now={now_mono!r} " + " ".join(f"{k}={v!r}" for k, v in observations.items())
        )

    # 관측은 판정보다 앞선다 — 크게 미래인 값은 skew가 아니라 축 위반이다(예: `time.time()`
    # 결과가 섞이면 min이 now를 골라 **항상 상한 전량** lease가 나가고 A1의 상한 증명이
    # 조용히 무효가 된다). 미세한 미래는 min이 clamp하므로 그대로 통과시킨다.
    horizon_ceiling = now_mono + MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS
    for name, value in observations.items():
        if value > horizon_ceiling:
            raise ValueError(
                f"lease: {name}가 now보다 {MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS}s 넘게 미래 — "
                f"monotonic 축 위반 의심 (got {value!r}, now={now_mono!r})"
            )

    return min(now_mono, *observations.values()) + LEASE_MAX_SECONDS


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
        firebase_identity_verified_at_mono: `check_revoked=True` 확인 시각.
            ⚠️ 이 값은 **검증된 토큰**(fingerprint / `auth_time`) 단위로 관측·저장돼야 한다 —
            UID 키로 캐시하면 revoked된 구 토큰이 같은 UID의 새 토큰 검증 결과를 공유해
            권한이 섞인다(A1). 이름이 `premium_*`과 대칭이라고 같은 키 공간이 아니다.
            ⚠️ 지금은 **캐시 자체가 없다** — `verify_ws_subscribe_token` 이 매 subscribe 마다
            `verify_id_token(check_revoked=True)` 를 부른다(`app/main.py`). 그래서 이 위험은
            현존이 아니라 **잠재**다. 강제를 미루던 strict cache 슬라이스는 ADR-040 에서
            **폐기**됐으므로(이 파일 모듈 docstring), 캐시를 넣는 사람이 그때 키 공간을 함께 정한다.

    Raises:
        ValueError: 비유한 입력, 또는 관측 시각이 `now`보다
            `MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS`를 넘어 미래인 경우(축 혼동 의심).

    Note:
        A1 원식은 세 항에 각각 `+ LEASE_MAX`를 더한 뒤 min을 취한다. IEEE754 덧셈이 단조라
        축약형과 **bit-for-bit 동등**하지만, 그 동등성은 **세 항의 horizon 상수가 동일할
        때만** 성립한다 — 항별 horizon이 갈리는 날엔 원식으로 되돌릴 것.
    """
    return _lease_expiry_from_observations(
        now_mono=now_mono,
        observations={
            "premium_verified_at_mono": premium_verified_at_mono,
            "firebase_identity_verified_at_mono": firebase_identity_verified_at_mono,
        },
    )


def compute_identity_only_lease_expiry(
    *,
    now_mono: float,
    identity_verified_at_mono: float,
) -> float:
    """**premium 제약이 없는** topic 의 lease 만료 (identity 축 하나만).

    ⛔ 호출부에서 `compute_lease_expiry(premium_verified_at_mono=now_mono, …)` 를 쓰지 말 것.
    그 인자는 위 docstring 이 **"authoritative RevenueCat 관측 시각"** 으로 정의한다 —
    관측한 적 없는 값을 넣으면 주석을 아무리 달아도 **타입 계약을 어긴다**(codex Medium).
    "premium 은 제약이 아니다"라는 사실은 **이 함수의 이름**이 표현하고, `now` 를 넘겨
    구속하지 않게 만드는 것은 여기 한 곳에서만 일어난다.

    ⚠️ 유료 topic 은 **이미 accept 한다** — 그 경로는 이 함수가 아니라 축에 맞는 계산기를 쓴다:
    per-user 판정이 붙는 현행 KRX 는 `compute_gated_lease_expiry`(4축), premium 만 보는 가상의
    경로라면 `compute_lease_expiry`(3축). ⚠️ `compute_lease_expiry` 는 **현재 프로덕션 호출자가
    없다**(테스트만 쓴다) — 지우기 전에 3축 소비자가 생길지부터 볼 것.
    """
    return _lease_expiry_from_observations(
        now_mono=now_mono,
        observations={"firebase_identity_verified_at_mono": identity_verified_at_mono},
    )


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


def compute_gated_lease_expiry(
    *,
    now_mono: float,
    identity_verified_at_mono: float,
    premium_verified_at_mono: float,
    entitlement_verified_at_mono: float,
) -> float:
    """per-user 판정이 필요한 topic(KRX)의 lease 만료 — **4축**.

    ⛔ `min(premium, entitlement)` 를 `compute_lease_expiry` 의 `premium_verified_at_mono` 에
    넘기는 우회를 쓰지 말 것. 수학적으로 같아도 그 인자는 docstring 이 **"authoritative
    RevenueCat 관측 시각"** 으로 정의하므로 **타입 계약이 거짓**이 된다(codex Blocker) —
    이 리포는 같은 형태의 거짓말을 이미 한 번 고쳤다(premium 자리에 `now`).

    ⚠️ entitlement 축이 실제로 필요한 이유: entitlement 는 **독립적인 철회 축**이다(우리 DB row).
    "DB 조회가 RC 뒤라 항상 지배당한다"는 순서 논증은 **런타임 성질**이라, 조회에 캐시가 붙는
    순간 조용히 깨진다. 축을 명시하면 그 위험이 사라진다.
    """
    return _lease_expiry_from_observations(
        now_mono=now_mono,
        observations={
            "firebase_identity_verified_at_mono": identity_verified_at_mono,
            "premium_verified_at_mono": premium_verified_at_mono,
            "entitlement_verified_at_mono": entitlement_verified_at_mono,
        },
    )
