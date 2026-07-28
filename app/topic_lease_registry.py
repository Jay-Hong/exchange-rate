"""topic lease registry — §8.1 B4 / C1 / C3.

운영 dispatcher(`app/topic_dispatcher.py`)를 **건드리지 않는 별도 모듈**이다. 상태기계와 B4
경쟁을 여기서 잠근 뒤에 최소 배선해야, 문제가 났을 때 원인이 registry인지 dispatcher인지 갈린다.

## 이 모듈이 지키는 것

- **identity**: `(ws, topic, uid, lease_id)`. 제거·갱신은 `lease_id` CAS다(§C3) — 구 sweep이
  갱신된 lease를 지우면 살아 있는 구독이 사라진다.
- **연결별 `asyncio.Lock`**: UID binding · lease CAS · ack 자료 확정이 한 lock 아래 있어야
  찢어지지 않는다. 전역 lock이면 한 느린 연결이 전체를 막는다.
- **요청 단위 트랜잭션**: 한 subscribe 요청 = **lock 1회 · ack 1건 · 전부 아니면 전무**.
  ⛔ topic 단위 API로는 성립할 수 없다. 3 topic 요청에서 가능한 배선은 둘뿐이고 둘 다 깨진다 —
  (i) ack 3건은 §8-B의 "요청당 1건"과 클라의 `request_id` 상관을 깨고, (ii) 마지막에만 ack을
  보내면 앞의 두 topic이 **ack보다 먼저 활성화**된다(계획 632행의 pre-ack live 수신).
  게다가 topic마다 lock을 잡았다 놓으면 그 사이 sweep·unsubscribe가 끼어들어, ack이
  **한 번도 존재한 적 없는 상태**를 광고할 수 있다(D2 713행 "단일 snapshot" 위반).
- **고정 순서**(§B4): `등록(비활성) → is_current 재확인 → **ack 송신** → 활성화`.
  ack이 활성화보다 **먼저**여야 한다 — 아니면 클라가 ack을 받기 전에 live 메시지가 먼저 도착하고,
  통지 순서가 역전된다(계획 632행). 그래서 `apply_subscribe`가 **sender를 주입받아** 순서를
  강제한다: 호출자는 활성화를 앞당길 방법이 없다.
- **소비 fence 순서**(§A4): `등록(비활성) → cache.is_current(snapshot) → 활성화`.
  등록을 먼저 하는 이유는, 재확인이 **등록 이후**여야 그 사이 무효화를 볼 수 있기 때문이다.
  재확인 **이후**의 무효화는 정상 발급 직후 webhook과 구별되지 않는 통상적 중도 무효화이고,
  `compute_lease_expiry`가 관측 시각에 고정된 3-way min이라 A1/A3의 상한 안이다.
- **요청당 단일 `now_mono`**: 이 모듈은 **시계를 스스로 읽지 않는다**. 호출자가 요청 경계에서
  1회 읽은 값을 넘긴다 — 같은 요청 안에서 축이 어긋나면 lease 길이가 호출 순서에 따라 흔들린다.

## `send_ack` 계약 (호출자가 지켜야 하는 것)

`await send_ack(ack)`은 **연결 lock을 쥔 채** 실행된다. 그래서:

1. **`True`를 반환해야 한다.** ⛔ `wait_for`의 제어 흐름은 배달의 증거가 아니다 — sender가
   `CancelledError`를 삼키고 정상 반환하면 `wait_for`는 **정상 반환**한다
   (`asyncio.Timeout.__aexit__`는 예외가 실제로 전파될 때만 `TimeoutError`로 바꾼다).
   반환값 계약이 "그냥 `await ws.send_json(...)`만 하고 끝나는" 흔한 형태를 잡는다.
   ⚠️ 한계: 삼킨 뒤 `True`를 돌려주면 여전히 속는다. 아래 2·3은 규칙일 뿐 강제되지 않는다.
2. **`CancelledError`를 삼키거나 shield하지 말 것.** shield하면 여기서는 실패로 접었는데
   ack은 실제로 나간다 — 서버가 버린 lease를 클라가 가졌다고 믿게 된다.
3. **자체 timeout을 걸지 말 것.** 예산은 이 모듈(`ACK_TIMEOUT_SECONDS`)이 소유한다.
   sender가 던진 `TimeoutError`는 우리 예산 만료와 **구별 불가**라, 호출자 버그가 조용히
   "죽은 클라"로 재분류된다.
4. **이 registry를 다시 부르지 말 것.** `asyncio.Lock`은 재진입 불가다 — 재진입은 hang이
   아니라 `ReentrantRegistryCall`로 시끄럽게 실패한다(아래).

## ack 실패 = **연결 사망**(§B2a), "발급 안 함"이 아니다

⛔ 취소는 이미 transport 버퍼에 들어간 프레임을 **되돌리지 못한다**(websockets legacy는
`write_frame_sync`로 프레임 전체를 버퍼에 넣은 뒤 `drain()`을 await한다 — 취소는 대기를
버릴 뿐 바이트를 버리지 않는다). 역압이 풀리면 그 ack이 **나중에 배달**돼, 클라는 lease를
가졌다고 믿는데 서버엔 없는 상태가 된다: 데이터 0 · 오류 0 · 통지 대상 lease도 없음 →
D6 재인증 타이머까지 방치. 그래서 registry가 **직접 tombstone을 찍어** 같은 소켓의 재발급을
fail-closed로 막는다. 호출자의 소켓 close 의무는 그 위에 얹힌다(registry는 I/O를 하지 않는다).

## 이 모듈이 하지 않는 것

- **snapshot 전송**. 무겁기 때문에 build는 lock 밖이고, **전송 직전 lock을 다시 잡아** lease를
  재검증한다(§B1과 결합). 그건 배선의 일이다.
  ⚠️ 반면 **ack은 lock 안**이다(§B4 629행) — 한때 이 문서가 "전송은 lock 밖"이라고 뭉뚱그렸는데
  **틀렸다**. ack까지 밖으로 내보내면 §B4의 고정 순서를 지킬 방법이 없다.
- **만료 sweep·재인증 통지**(§C3의 claim-then-notify) — 다음 조각.
  ⚠️ sweep은 연결 lock을 **무한 대기로 잡으면 안 된다**: ack이 최대 `ACK_TIMEOUT_SECONDS`
  동안 그 lock을 쥐므로, 단일 sweeper가 순회하며 무한 대기하면 역압 연결 K개에 대해 한 사이클이
  최대 5s×K 늘어나 D-const의 "만료→통지 10초" 관측 계약이 **무관한 다른 연결에서** 깨진다.
- **dispatcher 배선**. 기존 `registry.register()` 우회가 남지 않았음을 확인하는 것은 배선
  슬라이스의 일이다. ⚠️ 그 슬라이스의 가장 자연스러운 최소 편집(= `register()`를 그대로 두고
  뒤에 `apply_subscribe`를 부르기)은 **여기서 고친 순서를 그대로 되돌린다** — live 대상 집합이
  `_active`가 아니라 dispatcher 쪽 구독 집합이기 때문이다. 배선은 그 집합을 `_active`와
  교집합으로 만들거나 `register()` 경로를 제거해야 하고, publish 경로는 연결 lock을 잡지
  말아야 한다(잡으면 느린 한 연결의 ack이 fanout 전체를 지연시킨다).

⚠️ **새 feature flag를 만들지 않는다.** `TOPIC_DISPATCHER_ENABLED`가 이미 전체 진입 게이트이고,
별도 auth flag는 조합 실수(dispatcher=on / auth=off)로 **무인증 legacy 경로**를 남길 수 있다.
불가피하게 추가한다면 그 조합은 구 경로 실행이 아니라 **subscribe 거부**로 fail-closed여야 한다.
"""
from __future__ import annotations

import asyncio
import math
import uuid
import weakref
from dataclasses import dataclass
from typing import Optional, Sequence, Union

from app.topic_lease import compute_lease_expiry, is_expired

# ack 송신 상한. §D6의 클라 ack timeout이 10초라 그보다 작아야 클라가 먼저 포기하지 않는다.
# ⚠️ 상한이 없으면 멈춘 클라가 **연결 lock을 무한 점유**해 그 연결의 모든 상태 전이가 막힌다.
ACK_TIMEOUT_SECONDS = 5.0


class ReentrantRegistryCall(RuntimeError):
    """`send_ack` 안에서 registry를 다시 불렀다 — `asyncio.Lock`은 재진입 불가다.

    ⛔ 이게 없으면 **영구 정지**다. 가장 자연스러운 §B2a sender가
    `finally: await registry.remove_websocket(ws)`인데, ack 예산이 끝나 취소가 그 `finally`로
    풀려도 `lock.acquire()`를 기다리고 그 lock은 **우리 자신의 바깥 프레임**이 쥐고 있다 —
    아무도 깨우지 못한다. 그 연결의 teardown · sweep · unsubscribe가 통째로 죽는다.

    판정 기준이 ws가 아니라 **task 동일성**인 이유: 무관한 task(sweep·teardown)가 같은 연결의
    lock을 기다리는 것은 **정상**이다. ws만 보면 그 정당한 대기까지 오탐한다.
    """


@dataclass(frozen=True)
class Lease:
    """활성 구독 하나. `lease_id`가 CAS 토큰이다.

    ⚠️ **`ws`를 담지 않는다.** 연결은 이 lease가 저장된 **키**이고, 값이 키를 강하게 참조하면
    `WeakKeyDictionary`가 통째로 무력화된다(`dict → Lease → ws` 경로가 키를 살려 둔다).
    실측: handler가 정리를 못 부르고 연결이 사라져도 `connection_count`가 1로 남았다.
    호출자가 ws가 필요하면 `Applied.ws`를 쓴다 — 그건 반환값이라 수명이 짧다.
    """

    topic: str
    uid: str
    lease_id: str
    epoch: int
    expires_at_mono: float


@dataclass(frozen=True)
class ActiveSubscription:
    """ack의 topic별 항목 (D1 — `lease_id`·duration은 **topic 단위 필드**다).

    ⛔ 문자열 목록이면 안 된다: 이번 요청에 없던 기존 topic의 lease 상태를 복구할 수 없어
    클라가 상태를 잃었거나 UID reset 후 수렴할 때 부족하다(계획 710~712행).
    """

    topic: str
    lease_id: str
    lease_duration_seconds: int


@dataclass(frozen=True)
class AckState:
    """ack이 실어야 할 **권위 있는 상태**. lock 아래 **단일 snapshot**에서 만든다(계획 713행).

    ⛔ registry가 만들어 넘기는 이유: sender가 조회로 만들면 그 시점 새 lease는 아직
    `_pending`이라 **보이지 않는다** — ack이 방금 수락한 topic을 빠뜨리고, 클라는 구독이
    드롭됐다고 결론짓는데 서버는 곧바로 그 topic을 발행하기 시작한다.

    - `accepted` — 이번 요청에서 새로 발급한 lease(요청 순서, 중복 제거).
    - `removed` — 이번 요청의 결과로 **더 이상 활성이 아닌** topic. producer는 두 가지다:
      §C1의 UID purge와 §C2의 reject eviction(계획 743행). ⚠️ 제거된 topic을 같은 요청이 다시
      잡으면 여기 넣지 않는다 — 그건 제거가 아니라 **교체**이고, 두 목록에 동시에 실으면 모순이다.
      ⚠️ `removed`는 **상태 델타**다: 거부됐지만 원래 활성이 아니던 topic은 들어가지 않는다.
    - `active` — 그 연결의 **최종 상태 전체**(topic 정렬). 이번 요청에 없던 기존 topic도 포함.
    """

    uid: str
    # D2(계획 758행) — **연결별**이고 **같은 소켓의 UID 재바인딩만** 표현한다.
    # ⛔ strict cache의 `snapshot.epoch`을 쓰면 안 된다: 그건 **UID별**이고 최초 관측 순서로
    #    할당돼 먼저 본 UID가 더 작은 값을 갖는다 → A→B 재바인딩에서 **감소**하고, "구
    #    identity_generation ack 무시"를 지키는 클라가 유효한 새 ack을 버린다(실측 A=2→B=1).
    identity_generation: int
    accepted: tuple
    removed: tuple
    active: tuple


@dataclass(frozen=True)
class Applied:
    """트랜잭션 커밋. `ack`은 **실제로 전송된 바로 그** 객체다.

    `ws`는 **여기에만** 있다 — 저장 구조에 넣으면 약한 참조가 무력화된다.
    """

    ws: object
    ack: AckState


@dataclass(frozen=True)
class Discarded:
    """재확인에서 무효화가 확인돼 **아무것도 적용하지 않았다**. registry에 흔적이 없다.

    호출자 대응은 **재시도**다(연결은 멀쩡하다) — `AckFailed`와 정반대다.
    """

    reason: str = "invalidated_before_activation"


@dataclass(frozen=True)
class AckFailed:
    """ack을 못 보냈다(또는 배달을 확인할 수 없다) — **연결이 죽은 것으로 본다**(§B2a).

    적용된 변경은 없고, registry가 tombstone을 찍어 같은 소켓의 재발급을 막았다.
    ⚠️ 호출자는 소켓을 **닫아야** 한다 — 이 모듈은 I/O를 하지 않는다.
    """

    reason: str


@dataclass(frozen=True)
class Rejected:
    """이 연결에서 받을 수 없는 요청. 상태를 바꾸지 않았다."""

    reason: str


TransitionResult = Union[Applied, Discarded, Rejected, AckFailed]


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
        # ws -> weakref(ack을 실행 중인 task). ⚠️ **약한 참조로 담는다** — task의 프레임이
        # `ws`를 잡고 있어서, 강하게 담으면 `dict → task → frame → ws`로 키가 되살아난다
        # (`Lease`에 ws를 안 담는 것과 같은 이유).
        self._ack_owner: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        # ws -> int. D2의 연결별 `identity_generation` (미바인딩 0, 최초 바인딩 1, 재바인딩마다 +1).
        self._generations: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    # ── lock ────────────────────────────────────────────────────────────
    def connection_lock(self, ws) -> asyncio.Lock:
        """연결별 lock. **전역이 아니다** — 느린 연결 하나가 전체를 막으면 안 된다."""
        lock = self._locks.get(ws)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ws] = lock
        return lock

    def _guard_reentrant(self, ws) -> None:
        """⛔ **lock을 잡기 전에** 부른다 — 잡은 뒤면 이미 늦어서 그대로 hang한다."""
        ref = self._ack_owner.get(ws)
        if ref is None:
            return
        owner = ref()
        if owner is not None and owner is asyncio.current_task():
            raise ReentrantRegistryCall(
                "send_ack 안에서 registry를 다시 불렀다 — 연결 lock은 재진입 불가다. "
                "sender는 렌더링·전송만 하고 상태 전이는 apply_subscribe가 소유한다."
            )

    # ── 요청 단위 전이 ──────────────────────────────────────────────────
    async def apply_subscribe(
        self,
        *,
        ws,
        uid: str,
        topics: Sequence[str],
        rejected_topics: Sequence[str] = (),
        snapshot,
        cache,
        now_mono: float,
        premium_verified_at_mono: float,
        identity_verified_at_mono: float,
        send_ack,
    ) -> TransitionResult:
        """한 subscribe 요청을 **한 트랜잭션으로** 적용한다.

        `등록(비활성) → is_current → ack 1건 → 일괄 활성화`를 **lock 1회 안에서** 수행하고,
        ack 실패·fence 실패면 **아무 변경도 적용하지 않는다**.

        `topics`는 이 요청에서 **수락된** topic, `rejected_topics`는 **거부된** topic이다
        (per-topic 판정은 상위 §D5 3~5단계가 한다). 둘 다 **빈 리스트가 정상**이다:
        premium이 전부 거부돼도 §C1 purge는 커밋돼야 한다(계획 665행 — "B의 premium 확인이
        실패해도 A 구독을 복원하지 않는다").

        ⚠️ `rejected_topics`가 필요한 이유는 §C2다(계획 709행): "인증 성공한 subscribe에서
        **reject된 topic은 registry에서 제거**한다". accepted만 받으면 권한을 잃은 기존 등록이
        **lease 만료까지 잔존**해(710~711행), 같은 UID 재인증에서 KRX entitlement를 잃어도
        최대 15분 더 데이터가 나간다. **언급되지 않은 topic은 불변**이다(712행, 증분 subscribe 보존).
        거부 사유 문자열은 ack 봉투를 만드는 호출자가 싣는다(registry는 wire schema를 모른다).

        ⚠️ `now_mono`는 호출자가 요청 경계에서 **1회** 읽은 값이다. 이 모듈은 시계를 읽지 않는다.
        ⚠️ `send_ack`는 **필수**다(기본값 없음). 선택으로 두면 배선이 한 번 빠뜨렸을 때 ack 없이
        활성화되어 §B4 순서가 조용히 깨진다. 계약은 모듈 docstring "`send_ack` 계약" 참조.
        """
        contradictory = set(topics) & set(rejected_topics)
        if contradictory:
            # ⛔ D5 단계상 한 topic이 accepted이면서 rejected일 수 없다. 조용히 한쪽을 고르면
            #    "거부했는데 발급됐다"가 배선 버그로 남는다. 호출부 계약 위반이므로 크게 실패한다.
            raise ValueError(
                f"topic이 accepted와 rejected에 동시에 있다: {sorted(contradictory)}"
            )
        self._guard_reentrant(ws)
        async with self.connection_lock(ws):
            if ws in self._closed:
                # teardown이 끝났거나 ack이 실패한 연결이다. 되살리면 "끊긴 소켓에 살아 있는
                # 구독" 또는 "서버가 버린 lease를 클라가 가졌다고 믿는 상태"가 생긴다.
                return Rejected(reason="connection_closed")

            # §C1 — 바인딩 UID != 토큰 UID면 **그 ws의 기존 구독 전부 제거** 후 재바인딩
            # (계획 664행). 거부가 아니다: 거부하면 A의 구독이 B의 소켓에서 계속 살아 있고,
            # 708행이 `removed_topics`의 producer로 명시한 "C1의 UID purge"가 성립하지 않는다.
            bound = self._uids.get(ws)
            purge = bound is not None and bound != uid

            new_leases: dict = {}
            if topics:
                # D1 — 한 요청의 accepted는 **같은 순간** 갱신되므로 expiry를 1회만 계산한다.
                expiry = compute_lease_expiry(
                    now_mono=now_mono,
                    premium_verified_at_mono=premium_verified_at_mono,
                    firebase_identity_verified_at_mono=identity_verified_at_mono,
                )
                # ⛔ 3-way min이 이미 지났으면 **태어날 때부터 죽은** lease다. 검증기의 freshness
                # 게이트가 보통 막아 주지만 이 모듈은 독립이라 그 불변식에 기대면 안 된다.
                # 경계는 포함(§A2, fail-closed).
                #
                # ⚠️ 이 판정은 **발급에만** 건다. 발급할 lease가 없는 순수 purge 요청까지 막으면,
                # 접근을 없애기만 하는 요청이 거부돼 B의 소켓이 A의 데이터를 계속 받는 fail-open이
                # 된다. 그래서 `if topics:` 안에 있다.
                if is_expired(now_mono=now_mono, expires_at_mono=expiry):
                    return Rejected(reason="lease_horizon_already_expired")
                # 한 요청 안의 중복 topic은 **이 dict가** 접는다(같은 키 재대입).
                # ⚠️ 명시적 `if topic in new_leases: continue` 가드를 뒀었는데, mutation으로
                # 지워도 전부 green이었다 — 실제로 관측 가능한 차이가 없다(uuid 하나 덜 만들
                # 뿐이고 어느 lease가 이기든 동등하다). 중복 방어를 자료구조가 지고 있음을
                # 여기 적어 둔다.
                for topic in topics:
                    new_leases[topic] = Lease(
                        topic=topic,
                        uid=uid,
                        lease_id=uuid.uuid4().hex,
                        epoch=snapshot.epoch,
                        expires_at_mono=expiry,
                    )

            # 전이 계획을 **여기서 한 번** 확정한다 — lock을 쥐고 있으므로 ack 송신 중에도
            # 아무도 `_active`를 바꿀 수 없고, 따라서 ack이 광고한 상태와 커밋이 정확히 같다.
            current = self._active.get(ws, {})
            if purge:
                departing = set(current)                       # §C1 — 전부 걷어 낸다
            else:
                # §C2 — 거부된 topic 중 **실제로 활성이던 것**만 제거 대상이다.
                # 언급되지 않은 topic은 불변(계획 712행, 증분 subscribe 보존).
                departing = {t for t in rejected_topics if t in current}
            surviving = {t: l for t, l in current.items() if t not in departing}
            final = dict(surviving)
            final.update(new_leases)
            # 같은 요청이 다시 잡은 topic은 제거가 아니라 **교체**다.
            removed = tuple(sorted(t for t in departing if t not in new_leases))
            generation = self._next_generation_locked(ws, bound=bound, uid=uid)

            # 1) 등록(비활성) — 전 topic 한꺼번에. 재확인이 **등록 이후**여야 그 사이 무효화를 본다.
            pending = self._pending.setdefault(ws, {})
            pending.update(new_leases)
            try:
                # 2) 재확인 — 검증을 만든 **그 snapshot**으로, 요청당 **1회**.
                #    (topic마다 확인하면 topic별로 다른 판정이 나와 요청이 찢어진다.)
                if not cache.is_current(snapshot):
                    return Discarded()

                # 3) ack 자료 확정 — post-transition 상태를 **여기서 한 번** 만든다(D2 713행).
                ack = self._build_ack_locked(
                    uid=uid, generation=generation, now_mono=now_mono,
                    new_leases=new_leases, removed=removed, final=final,
                )

                # 4) ack 송신 — **활성화보다 먼저**(§B4 632행).
                self._ack_owner[ws] = weakref.ref(asyncio.current_task())
                try:
                    confirmed = await asyncio.wait_for(
                        send_ack(ack), timeout=ACK_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # ⚠️ sender가 자체 timeout을 걸면 그 예외와 구별 불가다 — 그래서 계약이
                    # "자체 timeout 금지"다. 여기서는 fail-closed로 접는다.
                    return self._ack_failed_locked(ws, "ack_timeout")
                except asyncio.CancelledError:
                    # ⛔ 반환값으로 바꾸지 않는다 — teardown의 구조적 취소가 조용히 삼켜진다.
                    #    같은 fail-closed 정리는 하고 다시 던진다.
                    self._closed.add(ws)
                    raise
                except Exception as exc:  # noqa: BLE001 — 전송 실패는 연결 사망으로 본다(§B2a)
                    return self._ack_failed_locked(ws, type(exc).__name__)
                finally:
                    self._ack_owner.pop(ws, None)

                if confirmed is not True:
                    return self._ack_failed_locked(ws, "ack_not_confirmed")

                # 5) 커밋 — 전부 함께. 여기서부터 live 전송 대상이다.
                #    ack을 만든 **바로 그** `final`을 통째로 넣는다 — 다시 계산하면 둘이 갈린다.
                self._active[ws] = final
                self._uids[ws] = uid
                self._generations[ws] = generation
                return Applied(ws=ws, ack=ack)
            finally:
                leftover = self._pending.get(ws)
                if leftover is not None:
                    for topic in new_leases:
                        leftover.pop(topic, None)
                    if not leftover:
                        self._pending.pop(ws, None)

    def _ack_failed_locked(self, ws, reason: str) -> AckFailed:
        """ack 실패 = **연결 사망**(§B2a). 변경은 적용하지 않고 tombstone만 찍는다.

        ⛔ "발급 안 함"으로 끝내면 안 되는 이유는 모듈 docstring 참조 — 취소된 ack이
        나중에 배달될 수 있어, 클라만 lease를 가졌다고 믿는 상태가 남는다.
        """
        self._closed.add(ws)
        return AckFailed(reason=reason)

    def _next_generation_locked(self, ws, *, bound, uid) -> int:
        """D2의 연결별 `identity_generation` — **같은 소켓의 UID 재바인딩만** 표현한다(계획 758행).

        미바인딩 → 1(최초 바인딩) / 재바인딩 → +1 / 같은 UID 재인증 → **불변**.
        ⚠️ 같은 UID 재인증에서 올리면 클라가 자기 상태를 불필요하게 버린다(재바인딩이 아니다).
        """
        current = self._generations.get(ws, 0)
        return current + 1 if (bound is None or bound != uid) else current

    def _build_ack_locked(self, *, uid, generation, now_mono, new_leases, removed, final) -> AckState:
        """**lock을 쥔 상태에서만** 부른다 — 단일 snapshot이어야 D2가 성립한다.

        전이 계획(`removed`/`final`)은 호출부가 이미 확정해 넘긴다. 여기서 다시 계산하면
        ack이 광고하는 상태와 실제 커밋이 갈릴 수 있다.
        """
        return AckState(
            uid=uid,
            identity_generation=generation,
            accepted=tuple(self._as_subscription(l, now_mono) for l in new_leases.values()),
            removed=removed,
            # ⚠️ 만료된 기존 lease는 **광고하지 않는다**(계획 715행: `≤ 0`이면 넣지 않는다).
            #    그렇다고 registry에서 지우지는 않는다 — 실제 제거와 `reauth_required` 통지는
            #    C3 sweep의 소유이고, 여기서 조용히 지우면 그 통지가 영영 나가지 않는다.
            active=tuple(
                s
                for s in (self._as_subscription(final[t], now_mono) for t in sorted(final))
                if s.lease_duration_seconds > 0
            ),
        )

    @staticmethod
    def _as_subscription(lease: Lease, now_mono: float) -> ActiveSubscription:
        """잔여 duration은 **내림(floor)**이다(계획 714행) — 올리면 만료 뒤를 살아 있다고 광고한다."""
        return ActiveSubscription(
            topic=lease.topic,
            lease_id=lease.lease_id,
            lease_duration_seconds=math.floor(lease.expires_at_mono - now_mono),
        )

    # ── 조회 ────────────────────────────────────────────────────────────
    def active_lease(self, ws, topic: str) -> Optional[Lease]:
        """**인가 응답**이다 — 이 연결에서 지금 이 topic을 보낼 수 있는가.

        **활성** lease만 보인다(미활성은 존재하되 전송 대상이 아니다).
        lock을 잡지 않는다(§B1 전송 직전 재검증이 이 경로를 쓴다 — 거기서 lock을 잡으면
        느린 한 연결의 ack이 fanout 전체를 지연시킨다).

        ⛔ **tombstone에서 즉시 fail-closed다.** tombstone이 신규 전이만 막으면 부족하다:
        호출자의 close가 지연되거나 실패하면 죽었다고 판정한 소켓으로 **기존 UID의 데이터가
        계속** 나간다. cross-UID 상황에서는 그게 곧 entitlement 우회다 — A의 KRX lease가
        살아 있는 채로, 클라는 (늦게 배달된 ack을 보고) B 세션이라고 믿는다.
        """
        if ws in self._closed:
            return None
        return self._active.get(ws, {}).get(topic)

    def identity_generation(self, ws) -> int:
        """D2의 연결별 generation. 미바인딩은 0."""
        return self._generations.get(ws, 0)

    def has_pending(self, ws, topic: str) -> bool:
        """등록됐지만 아직 활성화되지 않았는가 (fence 순서 검증용)."""
        return topic in self._pending.get(ws, {})

    def bound_uid(self, ws) -> Optional[str]:
        """현재 바인딩된 UID — **기록**이다.

        ⚠️ 인가 판정에 쓰지 말 것. tombstone을 보지 않으므로 죽은 연결에서도 값이 남는다.
        인가는 `active_lease`(fail-closed)를 거친다.
        """
        return self._uids.get(ws)

    def connection_count(self) -> int:
        return len(self._uids)

    # ── 제거 ────────────────────────────────────────────────────────────
    async def remove(self, ws, topic: str, lease_id: str) -> bool:
        """`(ws, topic, lease_id)` **CAS**. 자기 lease일 때만 지운다(§C3).

        ⚠️ id를 안 보면 구 sweep이 **갱신된** lease를 지워 살아 있는 구독이 사라진다.
        ⚠️ **연결 lock을 잡는다.** 안 잡으면 이 API가 B4 우회로가 된다.
        이미 lock을 쥔 곳에서는 `_remove_locked`를 쓴다.
        """
        self._guard_reentrant(ws)
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

        ⚠️ lock을 안 잡으면 B4 우회로가 된다 — 다른 task가 전이 중일 때 상태가 통째로 사라진다.
        ⚠️ **lock 항목은 지우지 않는다.** 지우면 누가 구 lock을 쥔 채로 teardown이 지나갔을 때
        다음 `connection_lock(ws)`이 **새 lock**을 만들어, 같은 연결에 대해 서로 다른 두 lock을
        동시에 쥘 수 있다(상호배제 붕괴, 실측 재현). 연결이 사라지면 weak dictionary가 알아서
        정리하므로 수동 삭제할 이유가 없다.
        """
        self._guard_reentrant(ws)
        async with self.connection_lock(ws):
            self._active.pop(ws, None)
            self._pending.pop(ws, None)
            self._uids.pop(ws, None)
            self._generations.pop(ws, None)
            self._closed.add(ws)
