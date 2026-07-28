"""topic lease registry — §8.1 B4 / C1 / C3.

운영 dispatcher(`app/topic_dispatcher.py`)를 **건드리지 않는 별도 모듈**이다. 상태기계와 B4
경쟁을 여기서 잠근 뒤에 최소 배선해야, 문제가 났을 때 원인이 registry인지 dispatcher인지 갈린다.

## 이 모듈이 지키는 것

- **identity**: `(ws, topic, uid, lease_id)`. 제거·갱신은 `lease_id` CAS다(§C3) — 구 sweep이
  갱신된 lease를 지우면 살아 있는 구독이 사라진다.
- **연결별 `asyncio.Lock`**: UID binding · lease CAS · ack 자료 확정이 한 lock 아래 있어야
  찢어지지 않는다. 전역 lock이면 한 느린 연결이 전체를 막는다.
- **고정 순서**(§B4): `등록(비활성) → is_current 재확인 → **ack 송신** → 활성화`.
  ack이 활성화보다 **먼저**여야 한다 — 아니면 클라가 ack을 받기 전에 live 메시지가 먼저 도착하고,
  통지 순서가 역전된다(계획 632행). 그래서 `issue`가 **sender를 주입받아** 순서를 강제한다:
  호출자는 활성화를 앞당길 방법이 없다.
- **소비 fence 순서**(§A4): `등록(비활성) → cache.is_current(snapshot) → 활성화`.
  등록을 먼저 하는 이유는, 재확인이 **등록 이후**여야 그 사이 무효화를 볼 수 있기 때문이다.
  재확인 **이후**의 무효화는 정상 발급 직후 webhook과 구별되지 않는 통상적 중도 무효화이고,
  `compute_lease_expiry`가 관측 시각에 고정된 3-way min이라 A1/A3의 상한 안이다.
- **요청당 단일 `now_mono`**: 이 모듈은 **시계를 스스로 읽지 않는다**. 호출자가 요청 경계에서
  1회 읽은 값을 넘긴다 — 같은 요청 안에서 축이 어긋나면 lease 길이가 호출 순서에 따라 흔들린다.

## 이 모듈이 하지 않는 것

- **snapshot 전송**. 무겁기 때문에 build는 lock 밖이고, **전송 직전 lock을 다시 잡아** lease를
  재검증한다(§B1과 결합). 그건 배선의 일이다.
  ⚠️ 반면 **ack은 lock 안**이다(§B4 629행) — 한때 이 문서가 "전송은 lock 밖"이라고 뭉뚱그렸는데
  **틀렸다**. ack까지 밖으로 내보내면 §B4의 고정 순서를 지킬 방법이 없다.
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

# ack 송신 상한. §D6의 클라 ack timeout이 10초라 그보다 작아야 클라가 먼저 포기하지 않는다.
# ⚠️ 상한이 없으면 멈춘 클라가 **연결 lock을 무한 점유**해 그 연결의 모든 상태 전이가 막힌다.
ACK_TIMEOUT_SECONDS = 5.0


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
class AckFailed:
    """ack을 못 보냈다 — lease를 **발급하지 않았다**.

    ⚠️ 호출자는 §B2a에 따라 **소켓을 닫아야** 한다. 전송 실패는 연결이 죽은 것으로 간주한다 —
    부분 삭제 후 소켓 유지는 "ack이 광고한 상태"가 무통지로 거짓이 되는 길이다.
    무효화(`Discarded`)와 구분되는 이유: 그쪽은 재시도가 맞고 이쪽은 연결 종료가 맞다.
    """

    reason: str


@dataclass(frozen=True)
class Rejected:
    """이 연결에서 받을 수 없는 요청. 상태를 바꾸지 않았다."""

    reason: str


IssueResult = Union[Issued, Discarded, Rejected, AckFailed]


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
        # ⚠️ **closed tombstone.** teardown 뒤에도 남아 있던 dispatch task가 lease를 되살리는 것을
        # 막는다. ws가 살아 있는 동안만 유지되면 충분하므로(되살릴 주체도 ws를 들고 있다) 여기서도
        # 약한 참조를 쓴다.
        self._closed: "weakref.WeakSet" = weakref.WeakSet()

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
        send_ack,
    ) -> IssueResult:
        """`등록(비활성) → is_current → **ack** → 활성화`를 **한 lock 안에서** 수행한다.

        ⚠️ `now_mono`는 호출자가 요청 경계에서 **1회** 읽은 값이다. 이 모듈은 시계를 읽지 않는다.

        ⚠️ `send_ack`는 **필수**다(기본값 없음). 선택으로 두면 배선이 한 번 빠뜨렸을 때 ack 없이
        활성화되어 §B4 순서가 조용히 깨진다. `await send_ack(lease)`는 **lock을 쥔 채** 실행된다 —
        ⛔ 그 안에서 이 registry를 다시 부르면 `asyncio.Lock`은 재진입 불가라 **데드락**이다.
        """
        async with self.connection_lock(ws):
            if ws in self._closed:
                # teardown이 끝난 연결이다. 되살리면 "끊긴 소켓에 살아 있는 구독"이 생긴다.
                return Rejected(reason="connection_closed")

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
                # 3) ack — **활성화보다 먼저**(§B4 632행). 실패하면 발급하지 않는다.
                try:
                    await asyncio.wait_for(send_ack(lease), timeout=ACK_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    return AckFailed(reason="ack_timeout")
                except Exception as exc:  # noqa: BLE001 — 전송 실패는 연결 사망으로 본다(§B2a)
                    return AckFailed(reason=type(exc).__name__)

                # 4) 활성화 — 여기서부터 live 전송 대상이다.
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

    async def remove_websocket(self, ws) -> None:
        """연결 종료 정리 — **그 연결 것만**, **연결 lock 아래에서**.

        ⚠️ lock을 안 잡으면 B4 우회로가 된다 — 다른 task가 `issue` 중일 때 상태가 통째로 사라진다.
        ⚠️ **lock 항목은 지우지 않는다.** 지우면 누가 구 lock을 쥔 채로 teardown이 지나갔을 때
        다음 `connection_lock(ws)`이 **새 lock**을 만들어, 같은 연결에 대해 서로 다른 두 lock을
        동시에 쥘 수 있다(상호배제 붕괴, 실측 재현). 연결이 사라지면 weak dictionary가 알아서
        정리하므로 수동 삭제할 이유가 없다.
        """
        async with self.connection_lock(ws):
            self._active.pop(ws, None)
            self._pending.pop(ws, None)
            self._uids.pop(ws, None)
            self._closed.add(ws)
