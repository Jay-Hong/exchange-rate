"""topic lease registry — §8.1 B4 / C1 / C3.

운영 dispatcher(`app/topic_dispatcher.py`)를 **건드리지 않는 별도 모듈**이다. 상태기계와 B4
경쟁을 여기서 잠근 뒤에 최소 배선해야, 문제가 났을 때 원인이 registry인지 dispatcher인지 갈린다.

## 이 모듈이 지키는 것

- **identity**: `(ws, topic, uid, lease_id)`. 제거·갱신은 `lease_id` CAS다(§C3) — 구 sweep이
  갱신된 lease를 지우면 살아 있는 구독이 사라진다.
- **연결별 `asyncio.Lock`**: UID binding · lease CAS · ack 자료 확정이 한 lock 아래 있어야
  찢어지지 않는다. 전역 lock이면 한 느린 연결이 전체를 막는다.
- **소비 fence 순서**(§A4): `등록(비활성) → cache.is_current(snapshot) → 활성화`.
  등록을 먼저 하는 이유는, 재확인이 **등록 이후**여야 그 사이 무효화를 볼 수 있기 때문이다.
  재확인 **이후**의 무효화는 정상 발급 직후 webhook과 구별되지 않는 통상적 중도 무효화이고,
  `compute_lease_expiry`가 관측 시각에 고정된 3-way min이라 A1/A3의 상한 안이다.
- **요청당 단일 `now_mono`**: 이 모듈은 **시계를 스스로 읽지 않는다**. 호출자가 요청 경계에서
  1회 읽은 값을 넘긴다 — 같은 요청 안에서 축이 어긋나면 lease 길이가 호출 순서에 따라 흔들린다.

## 이 모듈이 하지 않는 것

- **전송**. `issue`는 ack에 필요한 자료만 돌려주고 I/O는 하지 않는다 — lock을 쥔 채 `await
  send_json`을 하면 느린 구독자 하나가 그 연결의 모든 상태 전이를 막는다.
- **만료 sweep·재인증 통지**(§C3의 claim-then-notify) — 다음 조각.
- **dispatcher 배선**. 기존 `registry.register()` 우회가 남지 않았음을 확인하는 것은 배선 슬라이스의 일이다.

⚠️ **새 feature flag를 만들지 않는다.** `TOPIC_DISPATCHER_ENABLED`가 이미 전체 진입 게이트이고,
별도 auth flag는 조합 실수(dispatcher=on / auth=off)로 **무인증 legacy 경로**를 남길 수 있다.
불가피하게 추가한다면 그 조합은 구 경로 실행이 아니라 **subscribe 거부**로 fail-closed여야 한다.
"""
from __future__ import annotations

import asyncio
import uuid
import weakref
from dataclasses import dataclass
from typing import Optional, Union

from app.topic_lease import compute_lease_expiry, is_expired


@dataclass(frozen=True)
class Lease:
    """활성 구독 하나. `lease_id`가 CAS 토큰이다.

    ⚠️ **`ws`를 담지 않는다.** 연결은 이 lease가 저장된 **키**이고, 값이 키를 강하게 참조하면
    `WeakKeyDictionary`가 통째로 무력화된다(`dict → Lease → ws` 경로가 키를 살려 둔다).
    실측: handler가 정리를 못 부르고 연결이 사라져도 `connection_count`가 1로 남았다.
    호출자가 ws가 필요하면 `Issued.ws`를 쓴다 — 그건 반환값이라 수명이 짧다.
    """

    topic: str
    uid: str
    lease_id: str
    epoch: int
    expires_at_mono: float


@dataclass(frozen=True)
class Issued:
    """발급 성공. `lease`의 내용이 ack의 근거다(전송은 호출자가 lock 밖에서).

    `ws`는 **여기에만** 있다 — 저장 구조에 넣으면 약한 참조가 무력화된다.
    """

    ws: object
    lease: Lease


@dataclass(frozen=True)
class Discarded:
    """재확인에서 무효화가 확인돼 **발급하지 않았다**. registry에 흔적이 없다."""

    reason: str = "invalidated_before_activation"


@dataclass(frozen=True)
class Rejected:
    """이 연결에서 받을 수 없는 요청. 상태를 바꾸지 않았다."""

    reason: str


IssueResult = Union[Issued, Discarded, Rejected]


class TopicLeaseRegistry:
    """연결별 lock 아래에서 lease를 발급·보관한다."""

    def __init__(self) -> None:
        # ⚠️ **약한 참조로 연결 객체를 직접 키에 쓴다.** `id(ws)`를 키로 쓰면 연결이 GC된 뒤
        # 그 id가 재사용되어 **새 연결이 남의 UID 바인딩·lease를 물려받는다** — handler가
        # 비정상 종료해 `remove_websocket`이 안 불린 경우 실제로 도달 가능하다.
        # (테스트가 이걸 잡았다: 임시 `_WS()` 두 개가 같은 lock을 받았다.)
        self._locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        self._active: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()   # ws -> {topic: Lease}
        self._pending: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # ws -> {topic: Lease}
        self._uids: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()     # ws -> uid

    # ── lock ────────────────────────────────────────────────────────────
    def connection_lock(self, ws) -> asyncio.Lock:
        """연결별 lock. **전역이 아니다** — 느린 연결 하나가 전체를 막으면 안 된다."""
        lock = self._locks.get(ws)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ws] = lock
        return lock

    # ── 발급 ────────────────────────────────────────────────────────────
    async def issue(
        self,
        *,
        ws,
        topic: str,
        uid: str,
        snapshot,
        cache,
        now_mono: float,
        premium_verified_at_mono: float,
        identity_verified_at_mono: float,
    ) -> IssueResult:
        """`등록(비활성) → is_current → 활성화`를 **한 lock 안에서** 수행한다.

        ⚠️ `now_mono`는 호출자가 요청 경계에서 **1회** 읽은 값이다. 이 모듈은 시계를 읽지 않는다.
        """
        async with self.connection_lock(ws):
            bound = self._uids.get(ws)
            if bound is not None and bound != uid:
                # §C1 — 한 소켓이 두 사용자 권한을 섞으면 안 된다. 두 대안 중 **거부**를 택했다
                # (fail-closed). 다른 대안(클라 소유 identity generation)을 나중에 고르는 것을
                # 막지 않으면서, 지금 섞이는 것만은 확실히 막는다.
                return Rejected(reason="uid_rebinding_on_live_socket")

            lease = Lease(
                topic=topic,
                uid=uid,
                lease_id=uuid.uuid4().hex,
                epoch=snapshot.epoch,
                expires_at_mono=compute_lease_expiry(
                    now_mono=now_mono,
                    premium_verified_at_mono=premium_verified_at_mono,
                    firebase_identity_verified_at_mono=identity_verified_at_mono,
                ),
            )
            # ⛔ 3-way min이 이미 지났으면 **태어날 때부터 죽은** lease다. 검증기의 freshness
            # 게이트가 보통 막아 주지만 이 모듈은 독립이라 그 불변식에 기대면 안 된다 —
            # 죽은 lease를 조용히 발급하는 쪽이 바로 여기다. 경계는 포함(§A2, fail-closed).
            if is_expired(now_mono=now_mono, expires_at_mono=lease.expires_at_mono):
                return Rejected(reason="lease_horizon_already_expired")

            # 1) 등록(비활성) — 재확인이 **등록 이후**여야 그 사이 무효화를 본다.
            self._pending.setdefault(ws, {})[topic] = lease
            try:
                # 2) 재확인 — 검증을 만든 **그 snapshot**으로. 다시 뜨면 fence가 자기 자신을 통과한다.
                if not cache.is_current(snapshot):
                    return Discarded()
                # 3) 활성화 — 여기서부터 전송 대상이다.
                self._uids[ws] = uid
                self._active.setdefault(ws, {})[topic] = lease
                return Issued(ws=ws, lease=lease)
            finally:
                self._pending.get(ws, {}).pop(topic, None)

    # ── 조회 ────────────────────────────────────────────────────────────
    def active_lease(self, ws, topic: str) -> Optional[Lease]:
        """**활성** lease만 보인다 — 미활성은 존재하되 전송 대상이 아니다."""
        return self._active.get(ws, {}).get(topic)

    def has_pending(self, ws, topic: str) -> bool:
        """등록됐지만 아직 활성화되지 않았는가 (fence 순서 검증용)."""
        return topic in self._pending.get(ws, {})

    def bound_uid(self, ws) -> Optional[str]:
        return self._uids.get(ws)

    def connection_count(self) -> int:
        return len(self._uids)

    # ── 제거 ────────────────────────────────────────────────────────────
    async def remove(self, ws, topic: str, lease_id: str) -> bool:
        """`(ws, topic, lease_id)` **CAS**. 자기 lease일 때만 지운다(§C3).

        ⚠️ id를 안 보면 구 sweep이 **갱신된** lease를 지워 살아 있는 구독이 사라진다.
        ⚠️ **연결 lock을 잡는다.** 안 잡으면 이 API가 B4 우회로가 된다 — 오늘은 내부에 `await`가
        없어 우연히 직렬이지만, 다음 조각인 sweep의 claim/CAS가 들어오면 곧바로 깨진다.
        이미 lock을 쥔 곳에서는 `_remove_locked`를 쓴다.
        """
        async with self.connection_lock(ws):
            return self._remove_locked(ws, topic, lease_id)

    def _remove_locked(self, ws, topic: str, lease_id: str) -> bool:
        """**lock을 쥔 상태에서만** 부른다."""
        topics = self._active.get(ws, {})
        lease = topics.get(topic)
        if lease is None or lease.lease_id != lease_id:
            return False
        del topics[topic]
        return True

    def remove_websocket(self, ws) -> None:
        """연결 종료 정리 — **그 연결 것만**."""
        self._active.pop(ws, None)
        self._pending.pop(ws, None)
        self._uids.pop(ws, None)
        self._locks.pop(ws, None)
