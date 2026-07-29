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
  보내면 앞의 두 topic이 **ack보다 먼저 활성화**된다(§B4 순서 고정의 pre-ack live 수신).
  게다가 topic마다 lock을 잡았다 놓으면 그 사이 sweep·unsubscribe가 끼어들어, ack이
  **한 번도 존재한 적 없는 상태**를 광고할 수 있다(§D2의 "단일 snapshot" 위반).
- **고정 순서**(§B4): `등록(비활성) → is_current 재확인 → **ack 송신** → 활성화`.
  ack이 활성화보다 **먼저**여야 한다 — 아니면 클라가 ack을 받기 전에 live 메시지가 먼저 도착하고,
  통지 순서가 역전된다(§B4 순서 고정). 그래서 `apply_subscribe`가 **sender를 주입받아** 순서를
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
   ⚠️ 삼킨 뒤 `True`를 돌려줘도 **두 경우 모두 검출된다**: **외부** 취소는 바깥 프레임의
   `cancelling()`이 0→1로 남고(예산 만료는 `uncancel()`로 0→0), **예산 만료**는
   `asyncio.timeout(...).expired()`가 잡는다. ⛔ 구 서술 "예산 만료를 삼킨 경우만 구별 불가"는
   **틀렸다** — `wait_for`만 보고 내린 결론이었고, `asyncio.timeout`은 시계 없이 구별한다(실측).
   ⚠️ 다만 검출은 **거부**일 뿐 강제 종료가 아니다 — 비협조 sender는 그만큼 lock을 붙든다.
   아래 2·3은 규칙일 뿐 강제되지 않는다.
2. **`CancelledError`를 삼키거나 shield하지 말 것.** shield하면 여기서는 실패로 접었는데
   ack은 실제로 나간다 — 서버가 버린 lease를 클라가 가졌다고 믿게 된다.
3. **자체 timeout을 걸지 말 것.** 예산은 이 모듈(`ACK_TIMEOUT_SECONDS`)이 소유한다.
   sender가 던진 `TimeoutError`는 우리 예산 만료와 **구별 불가**라, 호출자 버그가 조용히
   "죽은 클라"로 재분류된다.
4. **이 registry를 다시 부르지 말 것.** `asyncio.Lock`은 재진입 불가다 — 재진입은 hang이
   아니라 `ReentrantRegistryCall`로 시끄럽게 실패한다(아래).

## 결과 4종 — **타입이 호출자의 행동을 정한다**

| 결과 | 연결 | 호출자 |
| --- | --- | --- |
| `Applied` | 살아 있음 | ack은 이미 나갔다. 이후 snapshot 전송(§B1) |
| `Discarded` | 살아 있음 | **재시도** |
| `Rejected` | 살아 있음 | 클라에 오류 통지 |
| `ConnectionTerminated` | **죽음** | **소켓을 닫는다** |

⛔ 종단 타입은 **하나뿐이다**. 원인별로 나누면 호출자가 전부 알아야 하고, 하나라도 놓치면
"죽은 연결에 재시도"가 되살아난다 — 실제로 그랬다(축소가 예정된 중단이 tombstone을 찍고도
`Discarded`를 반환해, 계약대로 재시도한 호출자가 영원히 `connection_closed`를 받았다).
진단은 `reason`이 지고, 타입은 **행동**을 진다.
**예외 채널에도 같은 규칙이 적용된다** — 접근 축소 중 예외가 나면 tombstone을 찍고
`ConnectionTerminatedError`로 감싸 던진다(원인은 `__cause__`). tombstone을 찍는 이탈은
결과든 예외든 **반드시 종단 신호를 낸다**.
⚠️ 단 `CancelledError`는 감싸지 않고 그대로 전파한다(취소 arm / 삼킨 외부 취소) — asyncio의
구조적 취소가 그 예외로 동작하고, 그쪽은 이미 teardown 중이라 소켓이 닫힌다.

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
  ⚠️ 반면 **ack은 lock 안**이다(§B4) — 한때 이 문서가 "전송은 lock 밖"이라고 뭉뚱그렸는데
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

⚠️ **계획을 가리킬 때 행번호를 쓰지 않는다.** 계획을 한 줄만 고쳐도 코드 주석의 인용이
전부 어긋난다(실제로 이 모듈이 그렇게 됐다 — 한 커밋에서 5개가 stale/오참조가 됐다).
§C1·§C2·§B4·§D2 같은 **섹션 앵커**만 쓴다.

⚠️ **새 feature flag를 만들지 않는다.** `TOPIC_DISPATCHER_ENABLED`가 이미 전체 진입 게이트이고,
별도 auth flag는 조합 실수(dispatcher=on / auth=off)로 **무인증 legacy 경로**를 남길 수 있다.
불가피하게 추가한다면 그 조합은 구 경로 실행이 아니라 **subscribe 거부**로 fail-closed여야 한다.
"""
from __future__ import annotations

import asyncio
import contextlib
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


class ConnectionLockBusy(RuntimeError):
    """연결 lock을 상한 안에 못 잡았다 — 호출자는 **그 연결만 건너뛴다**(§C3).

    ⚠️ `asyncio.TimeoutError`로 알리면 **본문이 던진 timeout과 구별되지 않는다**(전송 계층
    `TimeoutError`도 같은 타입이다). 획득 실패는 전용 타입이어야 호출자가 안전히 분기한다.
    """


class LockNotHeld(RuntimeError):
    """`*_locked` 메서드를 연결 lock 없이 불렀다.

    ⛔ 이들은 **상태 변경**이라 lock 없이 부르면 §B4 우회로가 된다. private으로 두면 sweep이
    쓸 수 없고(같은 lock 안에서 claim→통지→제거를 해야 한다), public으로 두면 잘못 불릴 수
    있으므로 **호출 시점에 검사**한다.
    """


@dataclass(frozen=True)
class ClaimedLease:
    """sweep이 만료로 claim한 항목. 통지·CAS 제거의 근거다.

    ⚠️ `Lease`와 같은 이유로 `ws`를 담지 않는다(약한 참조 무력화).
    """

    topic: str
    lease_id: str


@contextlib.contextmanager
def cancellation_fence():
    """블록 안에서 **외부** 취소가 삼켜졌는지 검출한다.

    주입된 sender가 `CancelledError`를 삼키고 정상 반환하면 `await`는 정상 종료하고, 호출자는
    그것을 **성공으로 읽는다** — 실측: teardown 취소가 무력화돼 `Applied`로 끝나고(`cancelled()`
    False / `cancelling()` 1), sweep에서는 취소된 통지를 성공으로 보고 lease를 지웠다.

    바깥 프레임의 `cancelling()`이 **외부** 취소에서만 0→1로 남는다(예산 만료는 `asyncio.timeout`이
    `uncancel()`해서 0→0). 그 차이를 여기서 한 번만 구현한다 — 세 곳(ack·unsubscribe·reauth)에
    손으로 복제했다가 두 곳을 빠뜨린 것이 이 helper가 생긴 이유다.

    ⛔ 검사는 **`finally`**에 있다. 한때 `yield` 뒤에 두고 "예외가 전파되면 검사에 도달하지
    않는다(의도) — 그때는 그 예외가 이미 실패를 말한다"고 적었는데 **틀렸다**: 예외는 "이 작업이
    실패했다"를, 취소는 "이 task를 접어라"를 말하는 **다른 축**이다. 호출자가 예외를 결과값으로
    바꾸는 순간(`ConnectionTerminated`) 취소 신호만 사라진다 — 실측: sender가 취소를 삼킨 뒤
    다른 예외를 던지자 `cancelled()`=False / `cancelling()`=1로 정상 종료했다.
    그래서 취소가 **다른 예외보다 우선**한다. 원래 예외는 `__context__`로 보존된다.
    """
    task = asyncio.current_task()
    before = task.cancelling() if task is not None else 0
    try:
        yield
    finally:
        if task is not None and task.cancelling() > before:
            raise asyncio.CancelledError()


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
    클라가 상태를 잃었거나 UID reset 후 수렴할 때 부족하다(§D2 `active_subscriptions`).
    """

    topic: str
    lease_id: str
    lease_duration_seconds: int


@dataclass(frozen=True)
class AckState:
    """ack이 실어야 할 **권위 있는 상태**. lock 아래 **단일 snapshot**에서 만든다(§D2 — connection lock 아래 단일 snapshot).

    ⛔ registry가 만들어 넘기는 이유: sender가 조회로 만들면 그 시점 새 lease는 아직
    `_pending`이라 **보이지 않는다** — ack이 방금 수락한 topic을 빠뜨리고, 클라는 구독이
    드롭됐다고 결론짓는데 서버는 곧바로 그 topic을 발행하기 시작한다.

    - `accepted` — 이번 요청에서 새로 발급한 lease(요청 순서, 중복 제거).
    - `removed` — 이번 요청의 결과로 **더 이상 활성이 아닌** topic. producer는 두 가지다:
      §C2의 reject eviction **하나**다(§D2). ⛔ 구 문서는 §C1의 UID purge도 producer로 들었는데
      B5(d) 결정으로 purge 규칙 자체가 폐기됐다.
      ⚠️ `removed`는 **상태 델타**다: 거부됐지만 원래 활성이 아니던 topic은 들어가지 않는다.
    - `active` — 그 연결의 **최종 상태 전체**(topic 정렬). 이번 요청에 없던 기존 topic도 포함.
    """

    # ⚠️ §8-B ack schema에는 `uid`가 없다 — 호출자의 상관·로깅용으로만 싣는다.
    #    §D8 unsubscribe는 `id_token` 불요라 미바인딩 연결에서는 None일 수 있다.
    uid: Optional[str]
    # §8-B — subscribe/unsubscribe가 **같은 schema**를 쓰므로 구분자가 필요하다.
    operation: str
    # §D2 `identity_generation` — ⚠️ **B5(d) 결정으로 의미가 소진됐다**: 표현하려던 "같은 소켓의
    #    UID 재바인딩"이 불가능해져(cross-UID는 연결 종료) 바인딩 후 **상수 1**이다. schema에서
    #    제거할지 새 의미를 줄지는 wire 슬라이스가 정한다.
    # ⛔ 역사적 주의(새 의미를 준다면 유효): strict cache의 `snapshot.epoch`을 쓰면 안 된다 —
    #    **UID별**이고 최초 관측 순서로 할당돼 순서 판별이 뒤집힌다(실측 A=2→B=1).
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

    호출자 대응은 **재시도**다 — 그리고 이 타입이 돌아왔다면 연결은 **정말로 멀쩡하다**.
    ⚠️ 한때 이 계약이 거짓이었다: 접근 축소가 예정된 중단도 tombstone을 찍으면서 `Discarded`를
    돌려줘, 계약대로 재시도한 호출자가 영원히 `connection_closed`를 받았다(클라는 데이터도
    오류도 없는 상태에 갇힌다). 지금은 그 경우 `ConnectionTerminated`가 나간다.
    """

    reason: str = "invalidated_before_activation"


@dataclass(frozen=True)
class Rejected:
    """이 요청을 받을 수 없다. 상태를 바꾸지 않았고 **연결은 멀쩡하다**.

    호출자 대응은 클라에 오류를 알리는 것이다(닫지 않는다).
    """

    reason: str


class ConnectionTerminatedError(RuntimeError):
    """**예외 채널의 종단 신호** — `ConnectionTerminated`의 쌍둥이. 호출자는 소켓을 닫는다.

    ⛔ 이게 없으면 계약에 구멍이 난다: 접근 축소 중에 예외(예: 축 위반 `ValueError`)가 나면
    tombstone은 찍히는데 **결과값이 없어**, `ConnectionTerminated`만 분기하는 호출자는 close
    필요성을 알 수 없다. 현행 직렬 endpoint는 예외가 상위 루프를 빠져나가 teardown으로
    이어져 가려지지만, §B5(b) task-spawn 배선에서는 task 예외가 소켓 종료로 연결되지 않으면
    다시 무데이터·무오류 상태가 된다.

    원인은 `__cause__`로 chaining한다 — 타입이 **행동**을, `__cause__`가 **진단**을 진다
    (결과 채널에서 타입/`reason`이 나눠 지는 것과 같은 규칙).

    ⚠️ **`Exception`만 감싼다.** `CancelledError`를 감싸면 asyncio의 구조적 취소가 그 예외로
    동작하므로 취소 전파가 깨지고, `SystemExit`/`KeyboardInterrupt`를 감싸면 프로세스 종료가
    막힌다. 셋 다 `BaseException`이라 `isinstance(exc, Exception)` 하나로 정확히 갈린다(실측).
    """

    def __init__(self, reason: str):
        super().__init__(f"연결을 닫아야 한다 (원인: {reason})")
        self.reason = reason


@dataclass(frozen=True)
class ConnectionTerminated:
    """**연결이 죽었다 — 호출자는 소켓을 닫아야 한다.** registry는 I/O를 하지 않는다.

    `reason`은 원인 진단이고(예: `ack_timeout` / `ack_not_confirmed` / 예외 이름 /
    `invalidated_before_activation` / `lease_horizon_already_expired` / `connection_closed`),
    **행동은 언제나 close 하나**다. 그래서 종단 타입을 **하나만** 둔다.

    ⚠️ 원인별로 타입을 나누고 싶은 유혹이 있는데(구 `ConnectionTerminated`가 그랬다), 그러면 호출자가
    종단 타입을 **전부** 알아야 하고 하나라도 놓치면 "죽은 연결에 재시도"가 되살아난다.
    진단은 `reason`이 지고, 타입은 **행동**을 진다.
    """

    reason: str


TransitionResult = Union[Applied, Discarded, Rejected, ConnectionTerminated]


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
        # ws -> int. §D2의 연결별 `identity_generation` (미바인딩 0 / 바인딩 시 1 / 이후 불변 —
        # B5(d)로 재바인딩이 불가능해졌다).
        self._generations: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        # ws -> {topic: lease_id}. §C3 claim-then-notify — **제거 전이라도** 인가에서 제외된다.
        self._claimed: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
        # ws -> weakref(lock을 쥔 task). `Lock.locked()`는 **누가** 쥐었는지 모르므로
        # (다른 task가 쥐어도 True — 실측으로 §B4 우회 성공) 소유자를 따로 기록한다.
        # ⚠️ **약한 참조**다 — task 프레임이 `ws`를 잡으므로 강하게 담으면 키가 되살아난다.
        # ⚠️ 한때 ack 창 전용 `_ack_owner` 장부를 따로 뒀는데 **상위집합에 흡수**했다:
        #    `apply_subscribe`의 lock 보유 구간이 ack 창을 완전히 포함하므로 중복이었고,
        #    분리해 두면 재진입 감지가 ack 창에서만 동작해 sweep·unsubscribe 경로는 조용히
        #    hang한다(실측: `async with hold_connection_lock: await remove(...)` 영구 정지).
        self._lock_owner: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    # ── lock ────────────────────────────────────────────────────────────
    def connection_lock(self, ws) -> asyncio.Lock:
        """연결별 lock. **전역이 아니다** — 느린 연결 하나가 전체를 막으면 안 된다."""
        lock = self._locks.get(ws)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ws] = lock
        return lock

    @contextlib.asynccontextmanager
    async def hold_connection_lock(self, ws, *, timeout: Optional[float] = None):
        """연결 lock을 잡고 **소유 task를 기록**한다 — `*_locked` 검사의 근거다.

        `timeout`을 주면 상한 안에 못 잡을 때 `ConnectionLockBusy`다(§C3 sweeper는 경합 시
        그 연결만 건너뛴다 — 무한 대기하면 무관한 연결의 통지가 밀린다).

        ⚠️ 실측(py3.13): timeout된 `lock.acquire()`는 lock을 **누수하지 않는다**.
        ⚠️ `connection_lock(ws)`을 직접 잡는 것도 여전히 가능하지만 그때는 소유 기록이 없어
        `*_locked`를 부를 수 없다 — 의도된 것이다(registry 상태 변경은 이 경로로만).
        """
        if timeout is not None and timeout <= 0:
            # ⛔ `wait_for(timeout<=0)`은 코루틴을 Task로 감싼 뒤 첫 step 전에 취소하므로
            #    **한가한 lock에서도 항상** busy가 된다(실측). 배선이 "기다리지 말고 한 번만
            #    시도" 의도로 0을 넣으면 전 연결이 skip되고 통지가 영영 안 나가는데, 로그에는
            #    정상 신호인 "경합 중"으로만 보인다. 조용한 무력화라 입력에서 거부한다.
            raise ValueError(
                f"timeout은 양수여야 한다(0은 '즉시 시도'가 아니라 '항상 실패'): {timeout!r}"
            )
        # ⛔ 같은 task의 중첩 진입은 `asyncio.Lock`이 재진입 불가라 **조용히 hang**한다 —
        #    소유자를 알면서 hang하는 것은 §B4의 "시끄럽게 실패" 규칙과 정반대다.
        self._guard_reentrant(ws)
        lock = self.connection_lock(ws)
        if timeout is None:
            await lock.acquire()
        else:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                raise ConnectionLockBusy(f"연결 lock 경합 — 다음 주기로 미룬다: {ws!r}") from None
        task = asyncio.current_task()
        self._lock_owner[ws] = weakref.ref(task) if task is not None else None
        try:
            yield
        finally:
            # ⚠️ 순서: 소유 기록을 먼저 지운다. release 뒤에 지우면 그 사이 lock을 잡은
            #    다른 task가 **우리 기록을 보고** 통과할 수 있다.
            self._lock_owner.pop(ws, None)
            lock.release()

    def _guard_reentrant(self, ws) -> None:
        """⛔ **lock을 잡기 전에** 부른다 — 잡은 뒤면 이미 늦어서 그대로 hang한다."""
        ref = self._lock_owner.get(ws)
        if ref is None:
            return
        owner = ref()
        if owner is not None and owner is asyncio.current_task():
            raise ReentrantRegistryCall(
                "이미 이 연결 lock을 쥔 task가 registry를 다시 불렀다 — 재진입 불가라 그대로 "
                "두면 영구 hang이다. 주입된 sender(ack·reauth)는 렌더링·전송만 하고, lock을 "
                "쥔 코드는 `*_locked` 계열을 쓴다."
            )

    # ── 요청 단위 전이 ──────────────────────────────────────────────────
    async def apply_subscribe(
        self,
        *,
        ws,
        uid: str,
        topics: Sequence[str],
        rejected_topics: Sequence[str],
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
        premium이 전부 거부돼도 §C2 eviction은 커밋돼야 한다 — 접근을 줄이는 전이를 발급 성공에
        종속시키면 권한을 잃은 topic이 계속 흐른다.

        ⚠️ `rejected_topics`가 필요한 이유는 §C2다: "인증 성공한 subscribe에서
        **reject된 topic은 registry에서 제거**한다". accepted만 받으면 권한을 잃은 기존 등록이
        **lease 만료까지 잔존**해(§C2), 같은 UID 재인증에서 KRX entitlement를 잃어도
        최대 15분 더 데이터가 나간다. **언급되지 않은 topic은 불변**이다(§C2, 증분 subscribe 보존).
        거부 사유 문자열은 ack 봉투를 만드는 호출자가 싣는다(registry는 wire schema를 모른다).

        ⛔ **`temporarily_unavailable`을 `rejected_topics`에 넣지 말 것**(§C4). 여기 들어온
        topic은 **제거된다** — 그런데 §C4는 일시적 실패에서 "registry 불변"을 요구한다.
        KRX `has_entitlement`는 캐시 없는 동기 DB 조회라 DB 순단에 False로 접히는데, 그것을
        거부로 넘기면 **정상 사용자의 구독이 영구 제거**된다. "권한 없음"과 "판정 불가"를
        호출부에서 갈라야 하고, 후자는 전체-요청 오류라 애초에 이 함수에 도달하지 않는다.
        ⚠️ 이 구분은 `Sequence[str]` 시그니처로는 강제되지 않는다 — 호출부 규율이다.

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
        async with self.hold_connection_lock(ws):
            if ws in self._closed:
                # teardown이 끝났거나 앞선 전이가 연결을 죽인 상태다. 되살리면 "끊긴 소켓에
                # 살아 있는 구독" 또는 "서버가 버린 lease를 클라가 가졌다고 믿는 상태"가 생긴다.
                # ⚠️ 호출자가 아직 안 닫았을 수 있으므로 **종단 결과**로 답한다(close 재촉).
                return self._terminate_locked(ws, "connection_closed")

            # §C1 — 한 소켓은 **평생 한 UID**다. 바인딩 UID != 토큰 UID면 **tombstone 후 종료**한다.
            # ⛔ 단순 거부로 연결을 살려 두면 안 된다 — 구 UID 데이터가 계속 전송될 수 있다.
            # ⛔ 구 규칙(purge + rebind)은 **폐기**됐다(B5(d) 결정): purge는 *적용하기로 한* 전이의
            #    의미만 정하고 *적용할지*는 정하지 않아, 지연 도착한 구 UID subscribe(C4 retry,
            #    구 토큰은 아직 유효)가 새 UID의 구독을 purge하고 되돌려 놓을 수 있었다 —
            #    거부 정책이면 무해했을 요청이 purge에서는 **파괴적**이었다.
            bound = self._uids.get(ws)
            if bound is not None and bound != uid:
                return self._terminate_locked(ws, "reconnect_required")

            # ── 전이 계획을 **발급보다 먼저** 확정한다 ─────────────────────────────
            # ⛔ 순서가 중요하다. 구 버전은 만료 horizon에서 여기 오기 전에 early return 해서
            #    **제거를 발급 성공에 종속**시켰다 — 실측: 같은 UID 재인증(accepted 있음 +
            #    rejected 있음)에서 거부된 topic이 그대로 살아남고, cross-UID에서는 B의 소켓이
            #    A의 lease로 계속 인가됐다(§C1 "검증 성공 즉시 A의 구독 제거" 위반).
            current = self._active.get(ws, {})
            # §C2 — 거부된 topic 중 **실제로 활성이던 것**만 제거 대상이다.
            # 언급되지 않은 topic은 불변(§C2, 증분 subscribe 보존).
            # ⚠️ B5(d) 결정 이후 이것이 `removed`의 **유일한** producer다(§C1 purge 폐기).
            departing = {t for t in rejected_topics if t in current}
            # ⛔ **접근을 줄이는 전이는 중단돼도 되돌아가지 않는다.** 되돌아가려면 그만큼의
            #    접근이 계속 살아 있어야 하는데, 그게 정확히 §C1/§C2가 막으려는 상태다.
            #    중단 경로마다 부분 커밋 규칙을 만드는 대신 tombstone **하나로** 접근을 0으로
            #    만든다 — 의도한 축소보다 크거나 같으므로 항상 안전한 쪽이다.
            reduces_access = bool(departing)

            try:
                # ⚠️ 축·유한성 **입력 검증은 topics와 무관하게 항상** 돈다. 구 버전은 검증까지
                #    `if topics:` 안에 넣어, 제거만 하는 요청이 wall epoch 혼입 상태로 조용히
                #    통과했다(`compute_lease_expiry`의 gross-skew 가드가 무력화됐다).
                expiry = compute_lease_expiry(
                    now_mono=now_mono,
                    premium_verified_at_mono=premium_verified_at_mono,
                    firebase_identity_verified_at_mono=identity_verified_at_mono,
                )
                new_leases: dict = {}
                if topics:
                    # ⛔ 3-way min이 이미 지났으면 **태어날 때부터 죽은** lease다. 검증기의
                    # freshness 게이트가 보통 막아 주지만 이 모듈은 독립이라 그 불변식에 기대면
                    # 안 된다. 경계는 포함(§A2, fail-closed).
                    #
                    # ⛔ **광고하는 정수와 경계를 맞춘다.** 판정만 raw float로 하면 잔여가
                    # 0<x<1인 lease가 발급되어 `accepted`에는 실리는데 `active`의 `>0` 필터에는
                    # 걸려 빠진다 — ack이 자기모순이 되고(실측 accepted=[(t,0)] / active=[]),
                    # 클라는 드롭으로 결론짓는데 서버는 발행을 시작하며, D6상 duration 0은
                    # 즉시 재구독이라 루프가 된다.
                    if (
                        is_expired(now_mono=now_mono, expires_at_mono=expiry)
                        or math.floor(expiry - now_mono) <= 0
                    ):
                        return self._abort_locked(
                            ws, Rejected(reason="lease_horizon_already_expired"), reduces_access
                        )
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

                surviving = {t: l for t, l in current.items() if t not in departing}
                final = dict(surviving)
                final.update(new_leases)
                # ⚠️ `departing ∩ new_leases`는 공집합이다 — `departing ⊆ rejected_topics`이고
                #    accepted∩rejected는 입력에서 거부되기 때문이다. purge 시절엔 "다시 잡은
                #    topic은 교체"라는 필터가 필요했지만 그 producer가 사라져 **죽은 분기**가 됐다.
                removed = tuple(sorted(departing))
                generation = self._next_generation_locked(ws, bound=bound)

                # 1) 등록(비활성) — 전 topic 한꺼번에. 재확인이 **등록 이후**여야 그 사이 무효화를 본다.
                pending = self._pending.setdefault(ws, {})
                pending.update(new_leases)
                try:
                    # 2) 재확인 — 검증을 만든 **그 snapshot**으로, 요청당 **1회**.
                    #    (topic마다 확인하면 topic별로 다른 판정이 나와 요청이 찢어진다.)
                    if not cache.is_current(snapshot):
                        return self._abort_locked(ws, Discarded(), reduces_access)

                    # 3) ack 자료 확정 — post-transition 상태를 **여기서 한 번** 만든다(§D2).
                    ack = self._build_ack_locked(
                        uid=uid, operation="subscribe", generation=generation, now_mono=now_mono,
                        new_leases=new_leases, removed=removed, final=final,
                    )

                    # 4) ack 송신 — **활성화보다 먼저**(§B4).
                    try:
                        # ⛔ `wait_for`가 아니라 `asyncio.timeout`인 이유: sender가 취소를
                        #    삼키면 `wait_for`는 예산 시점에 반환하지 않고 늦은 `True`를
                        #    정상 반환값으로 준다. `budget.expired()`가 시계 없이 구별한다.
                        with cancellation_fence():
                            async with asyncio.timeout(ACK_TIMEOUT_SECONDS) as budget:
                                confirmed = await send_ack(ack)
                    except asyncio.TimeoutError:
                        # ⚠️ 셋이 여기서 구별되지 않는다: 우리 예산 만료 / **전송 계층**의
                        #    `TimeoutError`(3.11+에서 `OSError` 계열) / sender가 스스로 건 timeout.
                        #    전부 fail-closed로 접는다 — 진단 문자열이 이 모호함을 안고 간다.
                        return self._terminate_locked(ws, "ack_timeout")
                    except asyncio.CancelledError:
                        # ⛔ 반환값으로 바꾸지 않는다 — teardown의 구조적 취소가 조용히 삼켜진다.
                        #    같은 fail-closed 정리는 하고 다시 던진다.
                        self._closed.add(ws)
                        raise
                    except Exception as exc:  # noqa: BLE001 — 전송 실패는 연결 사망으로 본다(§B2a)
                        return self._terminate_locked(ws, type(exc).__name__)

                    if budget.expired():
                        # 예산이 지난 뒤 도착한 성공 보고 — ack이 제때 못 나갔는데 활성화하면
                        # 클라는 자기가 구독한 줄 모르는 상태가 된다.
                        return self._terminate_locked(ws, "ack_confirmed_after_deadline")
                    if confirmed is not True:
                        return self._terminate_locked(ws, "ack_not_confirmed")

                    # 5) 커밋 — 전부 함께. 여기서부터 live 전송 대상이다.
                    #    ack을 만든 **바로 그** `final`을 통째로 넣는다 — 다시 계산하면 둘이 갈린다.
                    #    ⚠️ `_active[ws]`의 **dict 객체가 교체된다**(구 버전은 제자리 갱신).
                    #       C3 sweep이 이 dict 참조를 캐시하면 안 된다 — 매번 다시 읽을 것.
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
            except BaseException as exc:
                # ⛔ 여기까지 오는 이탈은 `compute_lease_expiry`의 축 위반(ValueError) 같은
                #    **예외**다. 접근 축소가 예정돼 있었으면 예외로 나가기 전에 닫는다 —
                #    아니면 호출자가 teardown할 때까지 구 UID의 lease가 계속 인가된다.
                if not reduces_access:
                    # 줄일 것이 없었으면 연결은 멀쩡하다. 감싸면 배선이 멀쩡한 연결을 닫는다.
                    raise
                self._closed.add(ws)
                # tombstone을 찍었으면 **이 채널에도** 종단 신호를 실어야 계약이 닫힌다.
                # `Exception`만 감싸는 이유는 `ConnectionTerminatedError` docstring 참조.
                if isinstance(exc, Exception):
                    raise ConnectionTerminatedError(type(exc).__name__) from exc
                raise

    def _abort_locked(self, ws, result, reduces_access: bool):
        """중단 — 접근 축소가 예정돼 있었으면 **닫고, 결과 타입도 종단으로** 바꾼다.

        ⛔ 되돌리면 그만큼의 접근이 계속 살아 있게 되고, 그게 정확히 §C1/§C2가 막으려는
        상태다. tombstone은 의도한 축소보다 **크거나 같은** 축소라 항상 안전한 쪽이다.

        ⛔ **타입을 함께 바꾸는 것이 핵심이다.** tombstone만 찍고 `Discarded`를 돌려주면
        그 타입의 계약("재시도")이 거짓이 되고, 호출자는 죽은 연결에 영원히 재시도한다.
        `reason`은 그대로 넘겨 원인 진단을 보존한다.
        """
        if not reduces_access:
            return result
        return self._terminate_locked(ws, result.reason)

    def _terminate_locked(self, ws, reason: str) -> ConnectionTerminated:
        """연결 사망 확정 — tombstone을 찍고 **종단 결과**를 돌려준다(§B2a).

        ⛔ ack 실패를 "발급 안 함"으로 끝내면 안 되는 이유는 모듈 docstring 참조 — 취소된
        ack이 나중에 배달될 수 있어, 클라만 lease를 가졌다고 믿는 상태가 남는다.
        """
        self._closed.add(ws)
        return ConnectionTerminated(reason=reason)

    def _next_generation_locked(self, ws, *, bound) -> int:
        """§D2의 연결별 `identity_generation`.

        미바인딩 → 1(최초 바인딩) / 이후 → **불변**.
        ⚠️ B5(d) 결정으로 **재바인딩 자체가 불가능**해져(cross-UID는 연결 종료) 이 값은 바인딩
        후 항상 1이다 — 정보를 싣지 않는다. schema에서 뺄지는 wire 슬라이스가 정한다.
        구 구현의 `bound != uid` → +1 분기는 도달 불가라 제거했다(죽은 코드).
        """
        current = self._generations.get(ws, 0)
        return current + 1 if bound is None else current

    def _build_ack_locked(self, *, uid, operation, generation, now_mono, new_leases,
                          removed, final) -> AckState:
        """**lock을 쥔 상태에서만** 부른다 — 단일 snapshot이어야 D2가 성립한다.

        전이 계획(`removed`/`final`)은 호출부가 이미 확정해 넘긴다. 여기서 다시 계산하면
        ack이 광고하는 상태와 실제 커밋이 갈릴 수 있다.
        """
        return AckState(
            uid=uid,
            operation=operation,
            identity_generation=generation,
            accepted=tuple(self._as_subscription(l, now_mono) for l in new_leases.values()),
            removed=removed,
            # ⚠️ 만료된 기존 lease는 **광고하지 않는다**(§D2: `≤ 0`이면 넣지 않는다).
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
        """잔여 duration은 **내림(floor)**이다(§D2) — 올리면 만료 뒤를 살아 있다고 광고한다."""
        return ActiveSubscription(
            topic=lease.topic,
            lease_id=lease.lease_id,
            lease_duration_seconds=math.floor(lease.expires_at_mono - now_mono),
        )

    # ── 조회 ────────────────────────────────────────────────────────────
    def authorized_lease(self, ws, topic: str, *, now_mono: float) -> Optional[Lease]:
        """**§B2 조회 경계 필터** — 이 연결이 지금 이 topic을 받을 자격이 있는가.

        활성 lease가 존재하고(미활성은 존재하되 전송 대상이 아니다), tombstone이 없고,
        **아직 만료되지 않았다**. read-only다 — 만료분을 제외할 뿐 지우지 않는다(지우면
        sweep이 `reauth_required`를 보낼 근거를 잃는다, §B2).

        ⛔ **§B1의 답이 아니다.** B1은 *전송 직전* 재검증이라 호출자가 **캡처한** identity
        `(uid, lease_id)`와 대조해야 한다 — 그건 `authorizes_send`다. 여기서 non-None만 보고
        보내면 같은 UID 재인증으로 L1→L2가 교체돼도 통과한다(실측).
        ⚠️ 실측 당시의 두 번째 구멍(cross-UID 재바인딩 뒤 A의 결정이 B의 소켓에 적용)은 B5(d)
        결정으로 **경로 자체가 사라졌다** — `uid` 축은 이제 재바인딩 방어가 아니라 **호출부 오류**
        방어다(한 연결은 평생 한 UID라 같은 ws에서 캡처했다면 항상 일치한다).
        조회 단계에는 캡처된 identity가 아직 없으므로 두 조항을 한 메서드로 합칠 수 없다.

        ⛔ 구 이름은 `active_lease`였고 시각을 받지 않았다 — docstring은 "인가 응답"이라
        주장하면서 §B1이 요구하는 `now < expires_at`을 **보지 않았다**(실측: 만료 10000초
        뒤에도 lease를 반환). sweep이 늦거나 아직 미배선이면 그 답을 믿는 전송 경로가
        영원히 통과한다. 그래서 시각을 **필수 인자**로 만들었다 — 만료를 확인하지 않은
        답 자체를 얻을 수 없어야 오용이 불가능하다.

        만료 판정은 `is_expired`에 위임한다 — 경계 포함·비유한 fail-closed 규약(§A2)을
        여기서 다시 쓰면 두 곳이 어긋난다.

        lock을 잡지 않는다(§B5(e) — 전송 경로에서 연결 lock을 잡으면 느린 한 연결의 ack이
        fanout 전체를 지연시킨다).

        ⛔ **tombstone에서 즉시 fail-closed다.** tombstone이 신규 전이만 막으면 부족하다:
        호출자의 close가 지연되거나 실패하면 죽었다고 판정한 소켓으로 **기존 UID의 데이터가
        계속** 나간다. cross-UID 상황에서는 그게 곧 entitlement 우회다 — A의 KRX lease가
        살아 있는 채로, 클라는 (늦게 배달된 ack을 보고) B 세션이라고 믿는다.

        ⚠️ **C3 sweep은 이 메서드를 쓸 수 없다** — sweep이 찾아야 하는 것은 정확히 여기서
        걸러지는 **만료된** lease다. 그건 열거 API(C-API 1)의 몫이고, 그 슬라이스가 자기
        접근자를 갖는다. **이 메서드를 만료 무시로 되돌려 sweep에 재사용하지 말 것.**
        """
        if ws in self._closed:
            return None
        lease = self._active.get(ws, {}).get(topic)
        if lease is None:
            return None
        if self._is_claimed(ws, lease):
            # §C3 — claim된 lease는 **제거 전이라도** 즉시 제외된다. 아니면 통지를 보내는 동안
            # live publish가 그 lease로 다시 통과한다. ⚠️ 만료 시각만으로는 못 막는다 —
            # 이 판정은 호출자가 넘긴 `now`를 쓰므로, 통지 중 tick이 과거 시각을 쓰면 통과한다.
            #
            # ⛔ **topic이 아니라 `lease_id`로 대조한다.** topic 존재만 보면, 취소로 남은 구
            # claim이 **재발급된 새 lease를 영구 차단**한다(실측: 다음 sweep은 미만료 새 lease를
            # claim하지 않아 복구도 없다). claim은 그 lease에 걸린 것이지 topic에 건 것이 아니다.
            #
            # ⚠️ 그래서 "재발급 커밋에서 구 claim을 지운다"는 **별도 청소가 필요 없다.** 한때
            # 둘 다 넣었는데 mutation에서 **양쪽 다 생존**했다 — 서로 잉여였다. 대조 방식이
            # 남을 이유: 청소 방식은 정확성이 "모든 교체 경로가 청소를 기억하는가"에 걸리고,
            # 이 버그가 정확히 그렇게 생겼다. 남은 표식은 존재하지 않는 lease를 가리켜도
            # id가 다르므로 무해하다. ⚠️ 수거자 서술 정정: **현행 lease가 남아 있는 경우에만**
            # 성공한 sweep의 `remove_locked`가 걷고, evict·unsubscribe로 lease가 사라진 topic의 표식은
            # CAS가 항상 False라 **teardown/GC만이** 걷는다(실측). 엔트리는 topic당 1개 상한이고
            # 연결과 함께 회수되므로 유계·무해하다.
            return None
        if is_expired(now_mono=now_mono, expires_at_mono=lease.expires_at_mono):
            return None
        return lease

    def authorizes_send(
        self, ws, topic: str, *, uid: str, lease_id: str, now_mono: float
    ) -> bool:
        """**§B1 전송 직전 인가** — 캡처한 `(uid, lease_id)`로 지금 보내도 되는가.

        계획 §B1의 검사 항목 그대로다: `lease identity(ws, topic, uid, lease_id)` +
        `now < expires_at`. 생존 규칙(tombstone·만료)은 `authorized_lease`에 **위임**한다 —
        여기서 다시 쓰면 §B2 필터와 두 곳이 어긋난다.

        ⛔ identity를 **필수 입력**으로 받는 이유: 조회 결과의 `is not None`만 보는 것이 가장
        자연스러운 사용법인데, 그러면 조회와 전송 사이에 lease가 **교체**된 경우를 못 막는다
        (실측: 같은 UID 재인증 L1→L2가 통과했다). registry가 직접 대조해야 그 오용이 불가능해진다.
        ⚠️ 실측 당시 함께 통과했던 cross-UID 변종(A→B)은 B5(d)로 경로가 사라졌다 — `lease_id`
        축이 여전히 load-bearing이고, `uid` 축은 호출부 오류 방어로 남는다.

        ⚠️ 캡처 시점에 유효했던 lease가 재인증으로 교체되면 그 tick은 여기서 떨어진다.
        의도된 동작이다 — 다음 발행 주기가 새 lease를 캡처한다.
        ⛔ **몇 tick이 떨어지는지는 이 모듈이 보장하지 않는다.** 한때 "§D6상 재인증이 6~7분
        간격이라 topic당 최대 1 tick"이라고 적었는데 **근거 없는 정량 주장**이었다: 재인증
        주기는 교체가 얼마나 자주 일어나는지를 정할 뿐, 그 순간 구 lease를 캡처한 채 진행
        중인 발행 작업이 **몇 개인지**를 정하지 않는다. `publish_topic`에는 직렬화 장치가 없고
        호출부도 topic별 단일 in-flight를 보장하지 않으므로, 겹친 fanout이 모두 떨어질 수 있다.
        상한이 필요하면 **발행 직렬화를 별도 계약으로** 세워야 한다.
        ⚠️ 이 검사와 **실제 `await send_json` 사이**의 창은 **열려 있다**(§B5(e)). B5(d)로 서버 쪽
        바인딩은 흔들리지 않지만, tombstone은 **이미 통과한 이 판정을 소급 취소하지 못한다** —
        실측: ① 여기서 True → ② cross-UID 요청이 tombstone → ③ 그 task가 전송(그 시점 재판정하면
        False인데도 나간다). UID 전환 직후 **구 UID 데이터 1건**이 도달할 수 있고, 서버만으로는
        닫히지 않는다(요청 도착 **전에** 발사된 메시지는 send lock으로도 못 막는다).
        1차 닫힘은 클라 소유 `connectionGeneration`의 구 연결 결과 폐기다.
        """
        lease = self.authorized_lease(ws, topic, now_mono=now_mono)
        if lease is None:
            return False
        return lease.uid == uid and lease.lease_id == lease_id

    def identity_generation(self, ws) -> int:
        """§D2의 연결별 generation. 미바인딩은 0.

        ⚠️ `bound_uid`와 같은 **기록**이다 — tombstone을 보지 않는다. 인가에 쓰지 말 것.
        """
        return self._generations.get(ws, 0)

    def has_pending(self, ws, topic: str) -> bool:
        """등록됐지만 아직 활성화되지 않았는가 (fence 순서 검증용)."""
        return topic in self._pending.get(ws, {})

    def bound_uid(self, ws) -> Optional[str]:
        """현재 바인딩된 UID — **기록**이다.

        ⚠️ 인가 판정에 쓰지 말 것. tombstone을 보지 않으므로 죽은 연결에서도 값이 남는다.
        인가는 `authorized_lease`(만료+tombstone fail-closed)를 거친다.
        """
        return self._uids.get(ws)

    def connection_count(self) -> int:
        return len(self._uids)

    # ── 제거 ────────────────────────────────────────────────────────────
    async def remove(self, ws, topic: str, lease_id: str) -> bool:
        """`(ws, topic, lease_id)` **CAS**. 자기 lease일 때만 지운다(§C3).

        ⚠️ id를 안 보면 구 sweep이 **갱신된** lease를 지워 살아 있는 구독이 사라진다.
        ⚠️ **연결 lock을 잡는다.** 안 잡으면 이 API가 B4 우회로가 된다.
        ⚠️ **tombstone을 보지 않는다** — `authorized_lease`와 의도적으로 비대칭이다. 죽은 연결에서도
        teardown·sweep의 정리는 계속 돌아야 하고, 이건 인가 응답이 아니라 **상태 변경**이다.
        이미 lock을 쥔 곳에서는 `_remove_locked`를 쓴다.
        """
        async with self.hold_connection_lock(ws):
            return self.remove_locked(ws, topic, lease_id)

    async def apply_unsubscribe(self, *, ws, topics, now_mono: float, send_ack) -> TransitionResult:
        """§D8 — 구독 해제. **인증에 종속되지 않는다**(`id_token` 불요).

        계획: "`unsubscribe`를 인증 성공에 종속시키면 **권한 축소가 실패**한다. 토큰 만료·
        revocation·Firebase 장애 시 제거가 막혀 사용자가 끄라고 한 데이터가 lease 만료까지
        계속 흐른다." 그래서 uid·snapshot·cache·premium 축을 **인자로도 받지 않는다** —
        받으면 배선이 그것을 검증에 쓰고 싶어진다.

        ⛔ **순서가 `apply_subscribe`와 반대다**: 여기서는 `제거 → ack`이고, subscribe는
        `ack → 활성화`다. 규칙은 하나다 — **위험한 쪽을 나중에** 둔다. subscribe에서 위험한
        것은 "클라가 모르는 데이터가 먼저 오는 것"이고, unsubscribe에서 위험한 것은 "끄라고 한
        데이터가 ack을 기다리는 동안 계속 흐르는 것"이다.
        그래서 ack이 실패해도 **제거는 되돌리지 않는다**(fail-open) — 되돌리면 정확히 그 데이터가
        다시 흐른다. 연결은 종단 처리하고 클라는 재연결로 상태를 다시 확정한다.

        ⛔ 제거는 **topic 단위**다 — `lease_id` CAS가 아니다. wire의 unsubscribe는 `lease_id`를
        싣지 않고(§8-A), 무엇보다 이건 **사용자 의도**라 그 사이 재인증으로 lease가 갱신됐어도
        여전히 제거가 맞다. (§C3 sweep이 CAS인 것과 대조된다 — 그쪽은 자기가 **관측한 그 lease**에
        대해서만 행동해야 한다.)

        idempotent다 — 원래 없던 topic은 조용히 넘어가고 `removed`에도 넣지 않는다(상태 델타).
        """
        self._guard_reentrant(ws)
        async with self.hold_connection_lock(ws):
            current = self._active.get(ws, {})
            departing = {t for t in topics if t in current}
            final = {t: l for t, l in current.items() if t not in departing}
            removed = tuple(sorted(departing))

            # 1) 제거를 **먼저** 커밋한다(위 순서 규칙).
            self._active[ws] = final
            for topic in departing:
                self._drop_claim_locked(ws, topic)

            if ws in self._closed:
                # ⛔ 제거는 이미 했다 — tombstone에서 거부하면 D8이 제거를 보장해야 하는 바로 그
                #    실패 경로에서 축소가 막힌다. ack만 생략하고 종단으로 답한다(소켓이 죽었다).
                return ConnectionTerminated(reason="connection_closed")

            # 2) ack — 이번 요청 결과 + 최종 상태 전체(§D2).
            ack = self._build_ack_locked(
                uid=self._uids.get(ws), operation="unsubscribe",
                generation=self._generations.get(ws, 0), now_mono=now_mono,
                new_leases={}, removed=removed, final=final,
            )
            try:
                with cancellation_fence():
                    async with asyncio.timeout(ACK_TIMEOUT_SECONDS) as budget:
                        confirmed = await send_ack(ack)
            except asyncio.TimeoutError:
                return self._terminate_locked(ws, "ack_timeout")
            except asyncio.CancelledError:
                # ⚠️ 제거는 되돌리지 않는다(fail-open) — 되돌리면 끈 데이터가 다시 흐른다.
                self._closed.add(ws)
                raise
            except Exception as exc:  # noqa: BLE001 — 전송 실패는 연결 사망으로 본다(§B2a)
                return self._terminate_locked(ws, type(exc).__name__)
            if budget.expired():
                return self._terminate_locked(ws, "ack_confirmed_after_deadline")
            if confirmed is not True:
                return self._terminate_locked(ws, "ack_not_confirmed")
            return Applied(ws=ws, ack=ack)

    def topics_for_test(self, ws) -> tuple:
        """활성 topic 이름 목록 — tombstone과 무관한 **상태 위생** 검증용(인가에 쓰지 말 것)."""
        return tuple(sorted(self._active.get(ws, {})))

    # ── §C3 sweep primitives (모두 **연결 lock을 쥔 상태에서만**) ────────────
    def _require_lock(self, ws) -> None:
        """⛔ lock 없이 상태를 바꾸면 §B4 우회로다. 조용히 통과시키지 않는다."""
        ref = self._lock_owner.get(ws)
        owner = ref() if ref is not None else None
        if owner is None or owner is not asyncio.current_task():
            raise LockNotHeld(
                "연결 lock을 쥔 상태에서만 부를 수 있다 — sweep은 claim→통지→제거를 "
                "한 lock 안에서 수행해야 한다(§C3)."
            )

    def connections_snapshot(self) -> list:
        """lease를 가진 연결의 **스냅샷 목록**(§C3 sweep 열거).

        ⚠️ 스냅샷인 이유: 순회 도중 다른 task가 연결을 추가·제거하거나 GC가 약한 키를
        수거하면 live view 순회가 깨진다. 스냅샷은 순회 동안 대상을 **강하게** 잡으므로
        중간에 사라지지 않는다(대신 그 사이 사라진 연결은 다음 주기가 본다).
        """
        return list(self._active.keys())

    def _is_claimed(self, ws, lease: Lease) -> bool:
        """이 **lease**가 sweep에 claim됐는가 (§C3).

        ⛔ **`Lease`를 받는다 — topic 문자열로는 물을 수 없다.** topic 존재만 보는 제외는
        취소로 남은 구 claim이 **재발급된 새 lease를 영구 차단**하게 만든다(실측). 시각을
        필수 인자로 만들어 만료 미확인 답을 못 얻게 한 것과 같은 수법으로, 잘못된 질문 자체를
        표현 불가능하게 한다 — §C-API 2의 `get_subscribers` 필터가 생길 때 같은 버그가 새
        지점에서 부활하지 않도록.
        """
        return self._claimed.get(ws, {}).get(lease.topic) == lease.lease_id

    def _drop_claim_locked(self, ws, topic: str) -> None:
        """그 topic의 claim 표식을 지운다 — **표식 변경의 단일 경로**.

        ⚠️ 무조건 지운다. 부르는 쪽은 이미 그 topic의 lease를 없앤 뒤이므로, 표식이 그 lease를
        가리켰든 (취소로 남은) 구 lease를 가리켰든 **어느 쪽도 더는 의미가 없다**.
        """
        claimed = self._claimed.get(ws)
        if claimed is None:
            return
        claimed.pop(topic, None)
        if not claimed:
            self._claimed.pop(ws, None)

    def claimed_topics(self, ws) -> tuple:
        """claim 표식이 걸린 topic 목록 — 상태 위생 검증용(인가 판정에 쓰지 말 것)."""
        return tuple(sorted(self._claimed.get(ws, {})))

    def claim_expired_locked(self, ws, *, now_mono: float) -> tuple:
        """만료 lease를 **원자적으로 claim**한다(§C3 claim-then-notify).

        claim된 항목은 제거 전이라도 `authorized_lease`·`authorizes_send`에서 즉시 빠진다 —
        그게 이 표식의 **유일한** 역할이다.

        ⛔ **이미 claim된 topic을 건너뛰지 않는다.** 처음엔 "중복 통지 방지"로 그 가드를 뒀는데
        mutation에서 살아남아 조사해 보니, 이 설계에서는 불필요할 뿐 아니라 **유해**했다:
        통지는 lock 안에서 일어나고 실패·hang은 소켓 정리로 끝나므로 claim이 남는 경로는
        **취소**뿐인데, 그때 가드가 있으면 그 lease는 통지도 제거도 영영 못 받는다(실측:
        인가에서만 빠진 채 좀비로 남는다 — fail-closed지만 클라는 재인증 신호를 못 받아
        자기 D6 타이머까지 방치된다). 다시 담아야 다음 주기가 **자가 복구**한다.
        중복 통지는 §D3상 무해하다 — 클라는 자기가 들고 있는 `lease_id`와 일치하면 적용한다.
        """
        self._require_lock(ws)
        already = self._claimed.setdefault(ws, {})
        claimed = []
        for topic, lease in self._active.get(ws, {}).items():
            if is_expired(now_mono=now_mono, expires_at_mono=lease.expires_at_mono):
                already[topic] = lease.lease_id
                claimed.append(ClaimedLease(topic=topic, lease_id=lease.lease_id))
        if not already:
            self._claimed.pop(ws, None)
        return tuple(claimed)

    def remove_locked(self, ws, topic: str, lease_id: str) -> bool:
        """`(ws, topic, lease_id)` **CAS** 제거 — **lock을 쥔 상태에서만**.

        ⚠️ id를 안 보면 구 sweep이 **갱신된** lease를 지워 살아 있는 구독이 사라진다.
        claim 표식도 함께 지운다 — 같은 topic이 새로 발급되면 그건 claim 대상이 아니다.
        """
        self._require_lock(ws)
        topics = self._active.get(ws, {})
        lease = topics.get(topic)
        if lease is None or lease.lease_id != lease_id:
            return False
        del topics[topic]
        self._drop_claim_locked(ws, topic)
        return True

    async def remove_websocket(self, ws) -> None:
        """연결 종료 정리 — **그 연결 것만**, **연결 lock 아래에서**.

        ⚠️ lock을 안 잡으면 B4 우회로가 된다 — 다른 task가 전이 중일 때 상태가 통째로 사라진다.
        ⚠️ **lock 항목은 지우지 않는다.** 지우면 누가 구 lock을 쥔 채로 teardown이 지나갔을 때
        다음 `connection_lock(ws)`이 **새 lock**을 만들어, 같은 연결에 대해 서로 다른 두 lock을
        동시에 쥘 수 있다(상호배제 붕괴, 실측 재현). 연결이 사라지면 weak dictionary가 알아서
        정리하므로 수동 삭제할 이유가 없다.
        """
        async with self.hold_connection_lock(ws):
            self.teardown_locked(ws)

    def teardown_locked(self, ws) -> None:
        """`remove_websocket`의 **lock 보유판**.

        ⛔ 이미 lock을 쥔 곳(§C3 sweep의 terminate 경로)이 `remove_websocket`을 부르면 재획득이
        필요한데, 그 재획득은 (1) 상한이 없어 §C3가 없애려던 head-of-line이 되살아나고
        (실측: 무관한 연결의 통지가 5.00s 밀렸는데 `skipped_busy=0`이라 telemetry에도 안 보였다)
        (2) release→재획득 **창**에 경쟁 subscribe가 끼어 죽은 연결에 900초 lease를 발급받는다
        (실측: `Applied` + `lease_duration_seconds=900` ack이 실제로 나갔다).
        """
        self._require_lock(ws)
        self._active.pop(ws, None)
        self._pending.pop(ws, None)
        self._uids.pop(ws, None)
        self._generations.pop(ws, None)
        self._claimed.pop(ws, None)
        self._closed.add(ws)
