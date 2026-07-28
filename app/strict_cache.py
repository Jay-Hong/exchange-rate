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

import math
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from app.strict_authz import (
    IdentityAuthorityFound,
    IdentityAuthorityNotFound,
    PremiumObservation,
)

IdentityObservation = Union[IdentityAuthorityFound, IdentityAuthorityNotFound]

# monotonic 값이 이 이상이면 **wall clock을 잘못 넘긴 것**이다 (1e9초 ≈ 31.7년 uptime,
# 반면 지금의 wall epoch는 ≈1.79e9). `strict_authz._IAT_SECONDS_MIN`과 같은 크기-축 판별 관용구다.
# 이 가드가 없으면 오염된 값 하나가 역행 가드에 눌러앉아 그 uid를 **영구히 브릭**한다(실측).
_MONOTONIC_SUSPICION_CEILING = 10**9


@dataclass(frozen=True)
class PutAccepted:
    """쓰기가 반영됐다."""

    epoch: int


class RejectReason(str, Enum):
    """거부 **사유** — 호출자의 다음 행동이 다르다.

    타입 하나로 뭉치면 "다시 시도해야 하나"를 알 수 없다(실측: 두 경우가 반환값으로 구분 불가였다).
    """

    STALE_EPOCH = "stale_epoch"    # 무효화됨 → snapshot부터 다시 떠서 재검증
    REGRESSION = "regression"      # 다른 owner가 이미 **더 새로운** 관측을 썼다 → 재시도 불필요
    UNKNOWN_UID = "unknown_uid"    # snapshot 없이 쓰려 함(또는 축출됨) → snapshot부터
    CONFLICT = "conflict"          # **같은 시각**에 내용이 다른 관측 — 한 순간이 두 상태일 수 없다


@dataclass(frozen=True)
class PutRejected:
    """쓰기가 **거부**됐다 — 저장소는 바뀌지 않았다.

    호출자는 이걸 오류로 다루지 말 것. 정상적인 경쟁 결과이며, 의미는 "내 결과는 이미 낡았다"다.
    이 결과로 **lease를 발급해서는 안 된다**(§A4 — 소비까지 fence).
    """

    reason: RejectReason
    expected_epoch: int
    current_epoch: Optional[int]


@dataclass(frozen=True)
class StrictSnapshot:
    """한 번의 lock 획득으로 읽은 **일관된** 상태.

    ⚠️ 두 concern을 따로 읽으면 **동시에 존재한 적 없는 쌍**을 조립하게 된다 — 두 read 사이에
    `bump`가 끼면 premium은 epoch N, identity는 N+1이 된다. `derive_verdict`는 둘 다 필요하고
    `epoch`은 보지 않으므로, 일관성은 여기서 보장해야 한다. 그래서 concern별 read는 **노출하지
    않는다** — 표현할 수 없으면 실수할 수 없다.
    """

    uid: str
    epoch: int
    premium: Optional[PremiumObservation]
    identity: Optional[IdentityObservation]


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
    at = observation.verified_at_mono
    if not isinstance(at, (int, float)) or isinstance(at, bool) or not math.isfinite(at):
        # NaN은 특히 위험하다 — 비교가 **항상 False**라 역행 가드를 통째로 무장 해제한다(실측).
        raise ValueError(f"verified_at_mono가 유한한 수가 아니다: {at!r}")
    if at < 0:
        raise ValueError(f"verified_at_mono가 음수다: {at!r}")
    if at >= _MONOTONIC_SUSPICION_CEILING:
        raise ValueError(
            f"verified_at_mono={at!r}는 monotonic이 아니라 **wall clock**으로 보인다 "
            f"(>= {_MONOTONIC_SUSPICION_CEILING}). 축을 섞으면 그 uid가 영구히 브릭된다."
        )


class StrictObservationCache:
    """UID 키, concern별로 분리된 관측 저장소 + UID별 epoch.

    ## epoch는 **전역 단조 증가**다 (uid별 0-기반이 아니다)

    per-uid로 0부터 세면 항목이 사라졌다 재생성될 때 **같은 값이 재사용**되어, 그 사이에 캡처된
    stale write가 CAS를 조용히 통과한다(ABA). 실측으로 재현했다. 계획이 `lease_id`에 이미 같은
    규칙을 두고 있다(§8.1 "정수 generation을 0부터 다시 세면 안 된다").
    → 그래서 (a) `snapshot()`이 처음 보는 uid에 generation을 **할당**하고(=capture primitive라
    쓰기를 겸한다), (b) `put_*`는 **없는 항목을 만들지 않으며**, (c) `bump()`는 없는 uid에 대해
    아무것도 하지 않고 `False`를 돌려준다(연결 없는 사용자에게 온 webhook이 항목을 새지 않게).

    ⚠️ **테스트에서 epoch 절대값(`== 1` 등)을 단언하지 말 것** — 전역 시퀀스라 uid 간 값이 비연속이다.

    ## 메모리

    지금은 bound가 없다. 1C에서 `snapshot()`은 **인증된** uid로만 도달하므로 증가는 익명
    트래픽이 아니라 실제 사용자 수에 묶인다. 나중에 LRU를 넣는다면 **epoch 항목까지 축출하면
    위 ABA가 되살아난다** — 축출 후에는 `put`이 `UNKNOWN_UID`로 fail-closed되므로 add-on은
    안전하지만, 그 비대칭(관측 맵은 버려도 되고 generation은 안 된다)을 지키는 게 조건이다.

    ## lock 계약 (지키지 않으면 서버가 멈춘다)

    `threading.Lock`은 재진입 불가다. critical section 안에서는 **dict 읽기/쓰기와 정수 증가만**
    한다 — `await`·I/O·블로킹 로깅·호출자 콜백·store 재진입 금지. 현재 lock 안의 유일한 외부
    접근은 frozen dataclass의 속성 읽기다.

    ## 단일 프로세스 전제

    운영은 `--workers 1`(`Dockerfile:131`). 워커를 늘리면 워커마다 독립된 저장소·generation을
    가져 **webhook 무효화가 그 워커에만 적용된다** — A1의 15분 상한 자체는 유지되지만 revoke
    지연이 사실상 lease 길이까지 늘어난다. 그때는 공유 저장소로 옮겨야 한다.

    ## A5(single-flight) 앞선 계약

    공유 flight의 결과는 관측 값을 실어 나르지 않는다. waiter는 flight 완료 후 `snapshot()`을
    **다시 떠서** 재파생한다 — 그러면 거부된 경우 관측이 없어 자연히 `NeedsVerification`이 되고
    다음 owner가 된다. flight 키는 `(uid, epoch)`다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_epoch = 0
        self._epochs: dict[str, int] = {}
        self._premium: dict[str, PremiumObservation] = {}
        self._identity: dict[str, IdentityObservation] = {}

    # ── 읽기 (유일한 read API) ────────────────────────────────────────────
    def snapshot(self, uid: str) -> StrictSnapshot:
        """검증 시작 **전에** 뜬다. 반환된 `epoch`을 그대로 `put(expected_epoch=)`에 넘긴다.

        신선도는 여기서 보지 않는다 — `derive_verdict`가 요청 시점에 평가한다(§A6-2).

        ⚠️ 읽기지만 **상태를 만든다**(처음 보는 uid에 generation 할당). capture primitive라서다.
        """
        with self._lock:
            epoch = self._epochs.get(uid)
            if epoch is None:
                self._next_epoch += 1
                epoch = self._next_epoch
                self._epochs[uid] = epoch
            return StrictSnapshot(
                uid=uid,
                epoch=epoch,
                premium=self._premium.get(uid),
                identity=self._identity.get(uid),
            )

    def is_current(self, snapshot: StrictSnapshot) -> bool:
        """이 snapshot의 epoch가 아직 현행인가. **시점 확인이지 상호배제가 아니다.**

        ⛔ 구 docstring은 "lease를 등록하는 critical section 안에서 호출하면 된다"고 했는데
        **틀렸다**. `bump()`는 이 저장소의 lock만 잡으므로, 호출자가 B4 연결 lock을 쥐고 있어도
        `bump`를 배제하지 못한다. 어떤 외부 lock 아래에서 불러도 이 검사 하나로는 fence가 안 된다.

        ## 올바른 사용 — 등록 후 1회 재확인 (§A4 "소비(lease 발급)까지 fence")

            snap = store.snapshot(uid)          # epoch 캡처 (검증 I/O 시작 전)
            ... 검증 I/O ...
            store.put_*(obs, expected_epoch=snap.epoch)
            lease = 등록(비활성, epoch=snap.epoch)   # ← 먼저 등록
            if not store.is_current(snap): 폐기   # ← 그 다음 1회 재확인
            lease 활성화

        **왜 이걸로 충분한가**: epoch는 전진만 하므로 재확인 이전에 들어온 `bump`는 **반드시**
        잡힌다. 재확인 이후에 들어온 `bump`는 *정상적으로 발급된 lease 직후에 webhook이 온 것*과
        구별되지 않는다 — 즉 설계가 이미 수용하는 통상적인 중도 무효화이고, `compute_lease_expiry`가
        `verified_at_mono`에 고정된 3-way min이라 `expiry <= premium_verified_at + LEASE_MAX
        < bump 시각 + LEASE_MAX`로 **A1/A3의 15분 상한 안**이다. 공유 lock이 필요 없다.

        ⛔ **이걸 "매 전송마다 검사"로 확대하지 말 것.** §A4는 *발급*을 fence하라 하고 B1의 검사
        항목에는 epoch 항이 없다. 확대하면 A4-2의 `unknown → invalidate`(denylist는 `TEST` 단독)와
        곱해져, 권한이 오히려 **강해진** 사용자의 live lease까지 `RENEWAL`·`PAYWALL_*` 같은
        고빈도·비-entitlement 이벤트마다 중도에 끊는다 — 지키는 것 거의 없이 가용성만 잃고,
        S5(15분)라는 제품 결정을 조용히 0으로 재가격한다.
        """
        with self._lock:
            return self._epochs.get(snapshot.uid) == snapshot.epoch

    # ── 무효화 ───────────────────────────────────────────────────────────
    def bump(self, uid: str) -> bool:
        """무효화 — generation을 새로 할당하고 그 UID의 관측을 **버린다**.

        진행 중인 검증은 이 시점부터 거부된다. 모르는 uid면 아무것도 하지 않고 `False`
        (연결 없는 사용자에게 온 webhook이 항목을 새지 않게 — webhook은 alias마다 돈다).
        반환값은 strict 전용 계측의 입력이기도 하다(§A4-2).
        """
        with self._lock:
            if uid not in self._epochs:
                return False
            self._next_epoch += 1
            self._epochs[uid] = self._next_epoch
            self._premium.pop(uid, None)
            self._identity.pop(uid, None)
            return True

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
        current = self._epochs.get(uid)
        if current is None:
            # snapshot 없이 쓰려 함(또는 축출됨). 항목을 **만들지 않는다** — 만들면 ABA가 열린다.
            return PutRejected(
                reason=RejectReason.UNKNOWN_UID, expected_epoch=expected_epoch, current_epoch=None
            )
        if current != expected_epoch:
            return PutRejected(
                reason=RejectReason.STALE_EPOCH,
                expected_epoch=expected_epoch,
                current_epoch=current,
            )

        existing = slot.get(uid)
        if existing is not None:
            if observation.verified_at_mono < existing.verified_at_mono:
                # 같은 epoch 안의 **역행 쓰기**. CAS는 epoch만 보므로 이걸 못 막는다.
                # 겹친 두 검증에서 느린 쪽이 나중에 끝나면 오래된 관측이 새 관측을 덮고,
                # 취소된 구독이 freshness 창만큼 되살아난다(보안 방향 손실).
                return PutRejected(
                    reason=RejectReason.REGRESSION,
                    expected_epoch=expected_epoch,
                    current_epoch=current,
                )
            if observation.verified_at_mono == existing.verified_at_mono and observation != existing:
                # 동률을 받아주는 근거는 **"같은 관측의 재기록은 무해하다"**였다. 내용이 다르면
                # 그 근거가 성립하지 않는다 — 한 순간이 active이면서 inactive일 수는 없다.
                # 실측: `inactive@100` 뒤 `active@100`이 둘 다 통과해 **취소가 되살아났다**.
                # 어느 쪽이 진짜인지 알 수 없으므로 **먼저 기록된 쪽을 지킨다**(보수적).
                return PutRejected(
                    reason=RejectReason.CONFLICT,
                    expected_epoch=expected_epoch,
                    current_epoch=current,
                )

        slot[uid] = observation
        return PutAccepted(epoch=current)

    def clear(self) -> None:
        """테스트 격리용 — 운영 경로에서 부르지 말 것.

        ⚠️ generation 카운터는 **되감지 않는다**. 되감으면 clear 이전에 캡처된 epoch가 다시
        유효해져 stale write가 통과한다(ABA) — 실측으로 이 테스트가 잡아냈다. 관측 맵은 버려도
        안전하지만(재검증 유도 = fail-closed) generation 재사용은 안전하지 않다는 **비대칭**이
        여기서도 그대로 적용된다.
        """
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
