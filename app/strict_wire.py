"""strict 판정 → **wire 결과** 매핑 (§8-C / §8.1 D5).

registry 배선의 첫 조각이다. lease·registry 없이 독립으로 성립하는 순수 계층이라, 여기서
두 가지를 닫는다: **전용 오류 카운터 + ERROR 로그**, **`retry_after` 하향 jitter**.

## §8.1 D5 — 오류는 단일 전순서가 아니라 **처리 단계**다

    1) payload validation      → 형식 오류
    2) token                   → invalid_token | temporarily_unavailable
    3) capability / topic 존재 → topics_disabled | unknown_topic
    4) premium                 → temporarily_unavailable | premium_required
    5) KRX entitlement         → krx_entitlement_required
    6) state transition        → UID 바인딩·lease CAS·registry 반영

1~2는 **전체-요청**, 3~5는 **per-topic**이다.
⚠️ 단 `temporarily_unavailable`은 **어느 단계에서 발생하든 전체-요청으로 승격**된다(§8-C).
per-topic으로 두면 "일부 topic만 조용히 빠진" 상태가 되고, 클라는 그걸 알 방법이 없다.

## 이 계층은 순수하다

registry도 lease도 건드리지 않는다(카운터 제외). 그래야 배선이 C4의 "registry 불변"을
**구조적으로** 말할 수 있다 — 매핑이 부수효과를 가지면 그 주장이 검증 불가가 된다.

## 이 슬라이스가 하지 않는 것

- lease 발급·registry 등록(§B4/§C1) — `Accepted.snapshot`을 그대로 넘겨
  `등록(비활성) → is_current → 활성화` 순서를 지키는 것이 다음 조각이다.
- topic별 capability/entitlement 판정(D5 3·5단계).
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import Optional, Union

from app.strict_authz import InactiveReason
from app.strict_verifier import (
    StrictVerifierConfigError,
    TemporarilyUnavailable,
    VerifiedActive,
    VerifiedInactive,
)

_MISSING = object()   # `None`을 유효한 uid로 오인하지 않기 위한 sentinel

logger = logging.getLogger("exchange_rate.strict_wire")

# §D-const: 정수 초, 1~30.
RETRY_AFTER_MIN_SECONDS = 1
RETRY_AFTER_MAX_SECONDS = 30

# 설정 결함은 재시도해도 낫지 않으므로 **가장 길게** 미룬다.
CONFIG_FAULT_RETRY_AFTER_SECONDS = RETRY_AFTER_MAX_SECONDS

# jitter 폭 — base의 이 비율만큼 **아래로만** 흔든다.
# ⚠️ 상향은 금지다. `retry_after`는 상한 계약이라 위로 흔들면 deadline을 침범한다(§D6).
JITTER_FRACTION = 0.4


@dataclass(frozen=True)
class Accepted:
    """판정 통과. **`snapshot`을 그대로 들고 간다.** lease 발급의 **authorization 토큰**이다.

    ⚠️ 배선이 `cache.snapshot(uid)`을 다시 뜨면 §A4의 소비 fence가 자기 자신을 통과시킨다
    (`snapshot()`은 없는 uid에 generation을 할당하는 쓰기다). 그래서 여기서 떨어뜨리면 안 된다.

    ## 왜 네 값이 **한 객체**로 묶여 있는가 (2026-07-30)

    구 `apply_subscribe`는 `uid`·`snapshot`·두 관측 시각을 **각각 따로** 받았다. 그래서 배선이
    이미 배포된 `verify_premium_status`(stale hit이 `PremiumStatus.ACTIVE`를 돌려준다 —
    `app/subscription.py`의 4단계 fallback)를 쓰고 `premium_verified_at_mono=clock.mono()`를
    넣는 것만으로 A1의 노출 상한이 무력화됐고, **어떤 타입 가드도 발화하지 않았다**
    (`Determined`도 `PremiumObservation`도 등장하지 않는 경로다).

    ⛔ `uid`는 **`snapshot.uid`에서 파생**된다(`map_verification`). 따로 받으면 "A의 snapshot +
    uid=B" 조합이 표현 가능해지고, 그때 fence(`cache.is_current(snapshot)`)는 A의 epoch를 보는데
    lease는 B의 것으로 발급된다 — 무료 사용자 B가 A의 premium 관측으로 인가된다.
    여기서 둘을 묶고 `__post_init__`이 일치를 강제하므로 그 조합이 **표현 불가**다.

    ⚠️ 이 토큰이 막는 것은 **위 두 실수**뿐이다. 값 자체의 권위는 보증하지 못한다 —
    누구든 이 생성자를 직접 부를 수 있다(§C-INV 1은 관측 스탬프 축에서 따로 닫아야 한다).
    그래서 생성 지점을 이 모듈로 못박는 AST 트립와이어가 짝이다.
    """

    snapshot: object
    premium_verified_at_mono: float
    identity_verified_at_mono: float
    uid: str

    def __post_init__(self) -> None:
        if not isinstance(self.uid, str) or not self.uid:
            raise ValueError(f"Accepted.uid: 비어 있지 않은 str이어야 — got {self.uid!r}")
        # ⛔ fail-closed: uid를 확인할 수 없는 snapshot은 거부한다. `getattr(..., None)`로
        #    조용히 넘기면 검사가 **있는 척만** 하게 된다(가장 위험한 실패 모드).
        snapshot_uid = getattr(self.snapshot, "uid", _MISSING)
        if snapshot_uid is _MISSING:
            raise ValueError(
                f"Accepted.snapshot에 uid가 없다 — 주체를 확정할 수 없다: {self.snapshot!r}"
            )
        if snapshot_uid != self.uid:
            raise ValueError(
                "Accepted: uid와 snapshot.uid가 다르다 — 한 주체의 판정이 아니다 "
                f"(uid={self.uid!r}, snapshot.uid={snapshot_uid!r})"
            )
        for name in ("premium_verified_at_mono", "identity_verified_at_mono"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"Accepted.{name}: finite 수여야 — got {v!r}")


@dataclass(frozen=True)
class WholeRequestFailure:
    """§8-C 전체-요청 오류. registry는 **건드리지 않는다**."""

    error: str
    retry_after_seconds: Optional[int] = None


@dataclass(frozen=True)
class PerTopicRejection:
    """§8-C per-topic 결과. 다른 topic은 살아남는다."""

    error: str


WireResult = Union[Accepted, WholeRequestFailure, PerTopicRejection]

# ⚠️ identity 축 실패는 전부 D5 **2단계**라 전체-요청이다. premium만 4단계(per-topic)다.
_IDENTITY_REASONS = frozenset(
    {
        InactiveReason.TOKEN_REVOKED,
        InactiveReason.ACCOUNT_DISABLED,
        InactiveReason.ACCOUNT_DELETED,
    }
)

_counters: dict = {"transient": 0, "config_fault": 0, "config_fault_by_kind": {}}


def map_verification(verification, *, rng=random) -> WireResult:
    """`StrictVerification` → wire 결과."""
    if isinstance(verification, VerifiedActive):
        return Accepted(
            snapshot=verification.snapshot,
            premium_verified_at_mono=verification.premium_verified_at_mono,
            identity_verified_at_mono=verification.identity_verified_at_mono,
            # ⛔ 파생이 요점이다 — 별도 인자로 받으면 불일치가 표현 가능해진다.
            uid=verification.snapshot.uid,
        )

    if isinstance(verification, VerifiedInactive):
        if verification.reason in _IDENTITY_REASONS:
            # D5 2단계 — 토큰 축. 전체-요청이다.
            return WholeRequestFailure(error="invalid_token")
        # D5 4단계 — 구독 없음. per-topic이라 다른 topic은 받을 수 있다.
        return PerTopicRejection(error="premium_required")

    if isinstance(verification, TemporarilyUnavailable):
        # ⚠️ concern이 무엇이든 **전체-요청으로 승격**한다(§8-C).
        _counters["transient"] += 1
        return WholeRequestFailure(
            error="temporarily_unavailable",
            retry_after_seconds=_jitter_down(verification.retry_after_seconds, rng),
        )

    raise TypeError(f"알 수 없는 판정: {verification!r}")


def map_config_error(error: StrictVerifierConfigError, *, rng=random) -> WholeRequestFailure:
    """설정·계약 결함 → wire 결과 + **전용 계측**.

    wire로는 `temporarily_unavailable`이다 — §8-C에 internal_error가 없고, 죽은 API key도
    그 정의("인증·권한을 **판정할 수 없음**")에 정확히 들어가며, 클라가 할 수 있는 게 그것뿐이다.
    ⚠️ 운영자 신호는 wire가 아니라 **여기의 카운터와 ERROR 로그**가 낸다 — transient와 같은
    카운터에 섞으면 죽은 API key를 영영 못 찾는다.
    """
    kind = getattr(error.kind, "value", str(error.kind))
    _counters["config_fault"] += 1
    _counters["config_fault_by_kind"][kind] = (
        _counters["config_fault_by_kind"].get(kind, 0) + 1
    )
    # ⚠️ 카운터는 **경계 있는 kind**로 집계하고, 원문 메시지는 로그에만 둔다
    # (메시지는 `repr(result)`를 담아 cardinality가 열려 있다).
    # 메시지에도 kind를 넣는다(운영자가 grep한다) + 구조화 필드로도 남긴다(리포 관용구).
    logger.error(
        "strict 인가 설정 결함 — 사람이 확인해야 한다 (kind=%s)",
        kind,
        extra={"config_fault_kind": kind, "detail": str(error)},
    )
    return WholeRequestFailure(
        error="temporarily_unavailable",
        retry_after_seconds=_jitter_down(CONFIG_FAULT_RETRY_AFTER_SECONDS, rng),
    )


def _jitter_down(base_seconds: int, rng) -> int:
    """`base`에서 **아래로만** 흔들고 정수 초로 접는다.

    ⚠️ 하향 전용인 이유: `retry_after`는 상한 계약이라 위로 흔들면 deadline을 침범한다(§D6).
    ⚠️ 바닥을 두는 이유: 0초로 내려가면 전역 장애 회복 시 즉시 재시도 herd가 된다.
    """
    spread = base_seconds * JITTER_FRACTION
    jittered = base_seconds - rng.uniform(0.0, spread)
    return max(RETRY_AFTER_MIN_SECONDS, min(RETRY_AFTER_MAX_SECONDS, int(round(jittered))))


def wire_counters() -> dict:
    """읽기 전용 스냅샷. `config_fault`는 transient와 **분리된** 축이다."""
    return {
        "transient": _counters["transient"],
        "config_fault": _counters["config_fault"],
        "config_fault_by_kind": dict(_counters["config_fault_by_kind"]),
    }


def reset_wire_counters() -> None:
    """테스트 격리용 — 운영 경로에서 부르지 말 것."""
    _counters["transient"] = 0
    _counters["config_fault"] = 0
    _counters["config_fault_by_kind"] = {}
