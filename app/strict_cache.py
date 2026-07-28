"""strict WS 인가용 **관측 저장소** (process-local).

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A4 / A6-1 / A6-2.

## 이 모듈이 저장하는 것

**관측(observation)만** 저장한다. verdict는 저장하지 않는다(§A6-1) — verdict를 저장하면
UID 단위 `Inactive`를 같은 UID의 **새 토큰이 상속**하고, 신선도 판단이 쓰기 시점에 박혀
read 시점 정책을 적용할 자리가 없어진다.

**신선도도 여기서 판단하지 않는다.** freshness는 관측에 붙고 요청마다 평가된다(§A6-2) —
`app.strict_authz.derive_verdict`가 그 일을 한다. 저장소가 freshness를 따로 구현하면 두
구현이 갈라진다.

## 왜 CAS인가 (§A4, 2026-07-27)

`검증 시작 → webhook invalidate → 구 검증이 ACTIVE로 완료 → cache 재기록` 경쟁이 있다.
**lookup 시점 epoch 검사만으로는 부족하다** — 그건 *소비*만 막는다. 구 owner가 쓰기 자체를
하면 이미 기록된 fresh 항목을 **덮어써** 멀쩡한 상태를 파괴한다. 그래서 `put(..., expected_epoch=N)`이
**쓰기 시점에** 비교해 불일치면 쓰기를 거부한다.

## 왜 `threading.Lock`인가

`asyncio.Lock`은 **스레드를 직렬화하지 못한다**. 이 리포는 `asyncio.to_thread`를 86곳에서 쓰고,
동기 DB 조회(`app/entitlements.py:28 has_entitlement`)가 그 안으로 들어갈 자연스러운 후보다.
epoch 읽기·비교·저장이 한 lock 안에서 원자적이어야 한다.

⚠️ **process-local이다.** 운영은 `--workers 1`(`Dockerfile:131`)이라 성립한다. 워커를 늘리면
워커마다 독립된 저장소·epoch를 갖게 되어 **webhook 무효화가 한 워커에만 적용된다** — 그때는
공유 저장소(Redis 등)로 옮겨야 한다.

## 이 슬라이스가 하지 않는 것

- **어떤 webhook 이벤트가 `bump`를 부르는지**의 정책(§A4-2) — 별도 슬라이스
- **single-flight**(§A5) — 별도 슬라이스
- verifier 배선 / lease 발급 — 별도 슬라이스
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Union

from app.strict_authz import (
    IdentityAuthorityFound,
    IdentityAuthorityNotFound,
    PremiumObservation,
)

IdentityObservation = Union[IdentityAuthorityFound, IdentityAuthorityNotFound]


@dataclass(frozen=True)
class PutAccepted:
    """쓰기가 반영됐다."""

    epoch: int


@dataclass(frozen=True)
class PutRejected:
    """쓰기가 **거부**됐다 — 저장소는 바뀌지 않았다.

    호출자는 이걸 오류로 다루지 말 것. 정상적인 경쟁 결과이며, 의미는 "내 결과는 이미 낡았다"다.
    이 결과로 **lease를 발급해서는 안 된다**(§A4 — 소비까지 fence).
    """

    expected_epoch: int
    current_epoch: int


PutResult = Union[PutAccepted, PutRejected]


def _require_match(observation, *, key: str, expected_epoch: int) -> None:
    """관측이 들고 있는 값과 호출 인자가 어긋나면 **오용**이다.

    같은 상태가 두 곳에 있으면 반드시 어긋난다. 저장 시점에 일치를 강제해 "어느 쪽이 맞는가"
    라는 질문 자체를 없앤다. 이건 verdict가 아니라 **프로그래밍 오류**라서 예외로 전파한다
    (§A6-1: programming 오류는 캐시하지도, wire 오류로 접지도 않는다).
    """
    if observation.uid != key:
        raise ValueError(
            f"observation.uid={observation.uid!r} != key={key!r} — 다른 사용자의 관측을 쓰려 한다"
        )
    if observation.epoch != expected_epoch:
        raise ValueError(
            f"observation.epoch={observation.epoch} != expected_epoch={expected_epoch} — "
            "캡처한 epoch와 관측이 어긋난다"
        )


class StrictObservationCache:
    """UID 키, concern별로 분리된 관측 저장소 + UID별 epoch.

    epoch는 **UID당 하나**이고 두 concern을 함께 덮는다(§A4 "UID별 epoch"). 과잉 무효화는
    fail-closed(재검증할 뿐)이고 과소 무효화는 안전하지 않으므로, 모호하면 과잉이 맞다.
    ⚠️ 다만 비용은 있다 — entitlement와 무관한 고빈도 webhook(§A4-2의 `PAYWALL_*`)이
    identity 관측까지 떨어뜨려 Firebase 왕복이 한 번 더 든다. concern별 epoch 분리는
    측정 후 결정할 일이고, 그때 바꿀 지점은 **이 클래스 하나**다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._epochs: dict[str, int] = {}
        self._premium: dict[str, PremiumObservation] = {}
        self._identity: dict[str, IdentityObservation] = {}

    # ── epoch ────────────────────────────────────────────────────────────
    def current_epoch(self, uid: str) -> int:
        """검증 시작 전에 캡처한다. 이 값을 그대로 `put(expected_epoch=)`에 넘긴다.

        ⚠️ 캡처와 put 사이는 당연히 열려 있다 — 그 창을 닫는 게 CAS의 목적이다.
        """
        with self._lock:
            return self._epochs.get(uid, 0)

    def bump(self, uid: str) -> int:
        """무효화 — epoch를 올리고 그 UID의 관측을 **버린다**.

        진행 중인 검증은 이 시점부터 `PutRejected`가 된다.
        """
        with self._lock:
            epoch = self._epochs.get(uid, 0) + 1
            self._epochs[uid] = epoch
            self._premium.pop(uid, None)
            self._identity.pop(uid, None)
            return epoch

    # ── 저장 ─────────────────────────────────────────────────────────────
    def put_premium(
        self, observation: PremiumObservation, *, expected_epoch: int, key: Optional[str] = None
    ) -> PutResult:
        uid = key if key is not None else observation.uid
        _require_match(observation, key=uid, expected_epoch=expected_epoch)
        with self._lock:
            return self._put_locked(self._premium, uid, observation, expected_epoch)

    def put_identity(
        self, observation: IdentityObservation, *, expected_epoch: int, key: Optional[str] = None
    ) -> PutResult:
        uid = key if key is not None else observation.uid
        _require_match(observation, key=uid, expected_epoch=expected_epoch)
        with self._lock:
            return self._put_locked(self._identity, uid, observation, expected_epoch)

    def _put_locked(self, slot: dict, uid: str, observation, expected_epoch: int) -> PutResult:
        """lock을 **잡은 채로** 비교하고 저장한다 — 그 사이에 `await`도 I/O도 없어야 한다."""
        current = self._epochs.get(uid, 0)
        if current != expected_epoch:
            return PutRejected(expected_epoch=expected_epoch, current_epoch=current)

        existing = slot.get(uid)
        if existing is not None and observation.verified_at_mono < existing.verified_at_mono:
            # 같은 epoch 안의 **역행 쓰기**. CAS는 epoch만 보므로 이걸 못 막는다.
            # 겹친 두 검증에서 느린 쪽이 나중에 끝나면 오래된 관측이 새 관측을 덮고,
            # 취소된 구독이 freshness 창만큼 되살아난다(보안 방향 손실).
            return PutRejected(expected_epoch=expected_epoch, current_epoch=current)

        slot[uid] = observation
        return PutAccepted(epoch=current)

    # ── 조회 ─────────────────────────────────────────────────────────────
    # 신선도는 여기서 보지 않는다 — `derive_verdict`가 요청 시점에 평가한다(§A6-2).
    def get_premium(self, uid: str) -> Optional[PremiumObservation]:
        with self._lock:
            return self._premium.get(uid)

    def get_identity(self, uid: str) -> Optional[IdentityObservation]:
        with self._lock:
            return self._identity.get(uid)

    def clear(self) -> None:
        """테스트 격리용 — 운영 경로에서 부르지 말 것."""
        with self._lock:
            self._epochs.clear()
            self._premium.clear()
            self._identity.clear()


_cache = StrictObservationCache()


def get_strict_cache() -> StrictObservationCache:
    """프로세스 공용 인스턴스."""
    return _cache


def _reset_strict_cache_state() -> None:
    """테스트 격리용 (리포 관용구: `app/notifications/fx_alert_shadow.py`)."""
    _cache.clear()
