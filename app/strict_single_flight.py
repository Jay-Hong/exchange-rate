"""A5 single-flight — 같은 사용자의 동시 authoritative 검증을 **1회로 합친다**.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A5.

한 사용자가 여러 연결·여러 topic으로 동시에 구독하면 authority(Firebase·RevenueCat) 조회가
그만큼 곱해진다. 이 모듈이 그걸 하나로 접는다.

## 왜 `shield`인가 (실측)

    await task          + waiter 취소 → 공유 task가 **CANCELLED**
    await shield(task)  + waiter 취소 → 공유 task가 completed

shield가 없으면 **한 WebSocket이 끊기는 것만으로 그 uid의 공유 검증이 죽고**, 결과를 기다리던
다른 연결이 전부 실패한다. 그래서 task를 `ensure_future`로 **떼어 놓고**, 생성자를 포함한
**모든** 대기자가 `shield`로 기다린다. 어느 대기자의 취소도 검증 자체를 죽이지 못한다.

## 관측이 아니라 **종결 방식**을 나른다

`run()`은 factory의 반환값을 그대로 돌려준다. 계약은 **"관측을 나르지 않는다"**이지
"아무것도 나르지 않는다"가 아니다 — 그 구분이 load-bearing이다:

- **관측을 나르면 안 되는 이유**: 판정이 **토큰마다** 달라야 하는데(§A6-1의 저장/파생 분리),
  관측을 값으로 넘기면 대기자가 남의 토큰 기준 판정을 물려받는다. 대기자는 flight 종료 후
  **저장소에서 다시 읽어** 재파생해야 한다.
- **종결 방식은 날라야 하는 이유** (실측): 안 나르면 대기자가 전부 "저장소에 아무것도 없네"를
  보고 **각자 다시 owner가 된다**. 초안이 그랬고, 결과는 *합치기가 장애 때 완전히 사라지는 것*이었다:

      정상 N=20 → provider 호출 **1**회
      장애 N=14 → provider 호출 **14**회 (= single-flight 없는 것과 동일)
      장애 N=20 → 그 재-루프가 검증기의 반복 예산을 소진해 `StrictVerifierConfigError` **6건**
                  (= "죽은 API key" 경보가 provider 장애에 오발화)

  종결 방식은 uid·epoch·concern에만 의존하고 **토큰과 무관**하므로 공유해도 판정이 섞이지 않는다.

## 키 granularity — `(uid, epoch, concern)`

⚠️ **정확성은 이 키가 아니라 저장소의 epoch CAS가 보장한다.** 키를 어떻게 잡아도 잘못된 관측이
저장되지는 않는다. 키가 사는 것은 **지연**이다. 실측으로 나눠 적는다:

- **`epoch`: 측정된 이득.** 진행 중 flight가 무효화된 뒤 도착한 요청이, 어차피 쓰기가 거부될
  **doomed flight에 붙어 한 RTT를 버리는 것**을 막는다.
      정본       → 새 요청이 무효 flight에 붙음 = False (joined 1)
      epoch 제거 → 새 요청이 무효 flight에 붙음 = True  (joined 3)
- **`concern`: 이득을 입증하지 못했다.** "identity 대기가 premium 조회에 막힌다"는 시나리오를
  구성해 봤지만 정본과 차이가 없었다(둘 다 막힘=False). 슬롯이 완료 즉시 해제되고 검증 루프가
  매 반복 저장소에서 다시 읽기 때문으로 보인다. **의미상 정확해서 유지하지만, 필요하다고
  주장하지 않는다** — 이걸 근거로 다른 설계를 정당화하지 말 것.

`app/strict_cache.py`의 forward 계약에는 `(uid, epoch)`로 적혀 있었다. 여기가 정본이다.

## 단일 event loop 전제

`asyncio.Task`와 평범한 dict를 쓴다. 저장소(`app/strict_cache.py`)가 `threading.Lock`을 쓰는
것과 다른데, 그건 저장소가 `asyncio.to_thread` 안에서도 불릴 수 있기 때문이고 **여기는 코루틴
전용**이다. 다른 스레드에서 이 클래스를 부르면 dict 경쟁이 생기므로, 그때는 `run()`을 loop에
넘기거나(`run_coroutine_threadsafe`) lock을 도입해야 한다.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Hashable


class SharedFlightCancelled(RuntimeError):
    """공유 flight가 **내 요청이 아닌** 이유로 취소됐다 — 재시도 가능한 도메인 오류.

    `CancelledError`를 그대로 퍼뜨리면 `BaseException`이라 배선의 `except Exception`을 통과해
    대기자 전원의 registry 구독이 삭제된다. 그래서 여기서 도메인 오류로 바꾼다.
    ⛔ 단 **내가 취소를 요청한 경우는 바꾸지 않는다** — 그건 진짜 취소이고 삼키면 종료가 막힌다.
    """


class StrictSingleFlight:
    """키가 같은 동시 호출을 하나의 detached task로 합친다."""

    def __init__(self) -> None:
        self._flights: dict[Hashable, asyncio.Task] = {}
        self._started = 0
        self._joined = 0

    async def run(self, key: Hashable, factory: Callable[[], Awaitable]):
        """`key`에 대해 `factory()`를 **최대 1회** 실행하고, 그 **종결 방식**을 돌려준다.

        factory는 관측이 아니라 종결 방식(저장됨 / 판정 불가 / 설정 오류)을 돌려줘야 한다.
        예외는 모든 대기자에게 전파된다 — 합치기의 의미가 "같은 시도를 공유한다"이므로.
        """
        task = self._flights.get(key)
        if task is None:
            # ⚠️ `create_task`로 **떼어 놓는다**. 여기서 `await factory()`를 직접 하면 생성자의
            # 취소가 곧 검증의 취소가 되어 §A5의 owner 격리가 깨진다.
            task = asyncio.ensure_future(factory())
            self._flights[key] = task
            task.add_done_callback(lambda done, k=key: self._release(k, done))
            self._started += 1
        else:
            self._joined += 1

        # ⚠️ 생성자도 **shield로** 기다린다. 생성자만 맨몸으로 기다리면 그 소켓이 끊길 때
        # 검증이 죽어, 기다리던 다른 연결이 전부 실패한다(실측으로 확인).
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # ⚠️ shield는 **안쪽** task가 취소되면 그 취소를 모든 대기자에게 퍼뜨린다.
            # `CancelledError`는 `BaseException`이라 `main.py:998`의 `except Exception`에
            # 안 걸리고, 대기자 전원이 `finally`로 떨어져 **각 연결의 구독이 통째로 삭제**된다
            # (C4 "registry 불변" 위반). flight는 detached라 주인이 없으므로, 내가 취소를
            # 요청하지 않았는데 task가 취소됐다면 그건 **내 취소가 아니다** — 도메인 오류로 바꾼다.
            # ⚠️ `_self_cancel_requests() == 0` 조건은 **판별 테스트를 만들지 못했다**.
            # 갈리는 지점은 "task가 이미 취소로 종료됐고 **동시에** 내가 자기 취소를 요청한"
            # 순간인데, shield가 안쪽 완료 즉시 깨우기 때문에 그 인터리빙을 구성하지 못했다.
            # 그래도 남긴다 — 이 조건은 변환이 일어나는 범위를 **좁히기만** 하고(=fail-safe 방향),
            # 없앴다가 진짜 종료 취소를 삼키면 프로세스가 안 죽는다. 필요하다고 주장하지는 않는다.
            if task.cancelled() and _self_cancel_requests() == 0:
                raise SharedFlightCancelled(f"공유 flight가 취소됐다: {key!r}") from None
            raise


    def _release(self, key: Hashable, done: asyncio.Task) -> None:
        """완료된 task의 슬롯을 비운다.

        ⚠️ **자기 것일 때만** 지운다 — 이미 다음 flight가 같은 키를 차지했으면 그걸 지워선 안 된다.
        슬롯이 안 비면 (a) 성공 시 낡은 결과가 영구 재사용되고 (b) 실패 시 그 키가 영구히 막힌다.
        """
        if self._flights.get(key) is done:
            del self._flights[key]

    def in_flight_count(self) -> int:
        """진행 중인 flight 수 — 누수 감시용."""
        return len(self._flights)

    def stats(self) -> dict:
        """계측: `started`는 실제 authority 호출 횟수, `joined`는 그 덕에 아낀 횟수."""
        return {"started": self._started, "joined": self._joined}


def _self_cancel_requests() -> int:
    """현재 task에 걸린 취소 요청 수 (없으면 0). Python 3.11+의 `Task.cancelling()`."""
    current = asyncio.current_task()
    if current is None:
        return 0
    cancelling = getattr(current, "cancelling", None)
    return cancelling() if cancelling is not None else 0
