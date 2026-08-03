"""WS subscribe 인증 전용 executor — **동거 `to_thread` 작업과 스레드를 나눈다**.

무엇을 고치나: `asyncio.to_thread` 는 loop 의 **기본** executor 를 쓴다. 그래서 재연결 폭주로
subscribe 인증이 몰리면 같은 pool 을 쓰는 다른 작업(atomic loader/store, DB selector 등)이 함께
밀린다. 측정: 동거 작업 p99 **3967ms → 31ms**(격리 적용 시).

⛔ **admission control 이 아니다.** 아무도 거절하지 않고 자원만 나눈다 — 새 wire 결과가 없다.
   (즉시거절 semaphore 는 별도로 **기각**됐다: 배포 재연결은 평균 유입이 낮아도 *동시 도착*이라
   1초면 빠질 큐를 대량 거절한다.)

설계 계약
- ⛔ **loop 의 default executor 를 교체하지 않는다**(`set_default_executor` 금지). 교체하면 격리가
  아니라 *전체 이주*가 되어, 인증이 아닌 작업까지 이 pool 의 W 에 갇힌다.
- ⛔ **미초기화·종료 상태에서 `asyncio.to_thread` 로 fallback 하지 않는다.** fallback 은 격리를
  **조용히 무효화**하는데, 하필 그게 필요한 순간(기동 직후·종료 중 폭주)에 그렇게 된다.
  fail-closed 로 `AuthExecutorNotReady` 를 던지고, 호출자가 §8-C 로 분류하게 둔다.
- ⚠️ **이것은 큐 상한이 아니다.** `ThreadPoolExecutor` 의 작업 큐는 `SimpleQueue` 라 **무제한**이다.
  격리는 *동거인* 을 지킬 뿐 인증 자신의 적체는 그대로다 — ingress 상한(nginx)과
  **연결 내부 subscribe 남용**은 여전히 **별도 열린 항목**이다. "자원 상한 완료" 라고 쓰지 말 것.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

logger = logging.getLogger("exchange_rate.auth_executor")

T = TypeVar("T")


class AuthExecutorNotReady(RuntimeError):
    """인증 전용 executor 가 없다(미기동·종료됨). ⛔ 여기서 fallback 하지 않는다."""


_executor: ThreadPoolExecutor | None = None


def start_auth_executor(max_workers: int) -> None:
    """lifespan startup 에서 1회. ⚠️ **재진입 가능**해야 한다 — `TestClient` 는 lifespan 을
    여러 번 열고 닫으므로, 종료된 executor 를 재사용하면 두 번째 실행이 통째로 죽는다.
    """
    global _executor
    # ⚠️ 이 검사는 **중복이다** — `ThreadPoolExecutor(max_workers<=0)` 자체가 `ValueError` 를
    #    던진다(변이로 확인: 이 줄만 지워도 테스트가 통과한다). 메시지를 우리 어휘로 주려고
    #    남기지만, **판별력이 있는 가드로 착각하지 말 것**. 진짜 위험은 "양수지만 터무니없는 값"
    #    이고 그건 정책이 없어 코드로 못 막는다 — W 는 측정으로 정해 GO 기록에 남긴다.
    if max_workers <= 0:
        raise ValueError(f"인증 executor worker 수는 양수여야 한다: {max_workers}")
    if _executor is not None:
        # 이미 살아 있으면 그대로 둔다(중복 startup 방어). 새로 만들면 구 pool 이 누수된다.
        return
    _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ws-auth")
    logger.info("✅ 인증 전용 executor 기동", extra={"max_workers": max_workers})


def shutdown_auth_executor() -> None:
    """lifespan shutdown 에서. ⛔ `cancel_futures=True` — 큐에만 있는 작업은 **실행하지 않고**
    취소한다(종료 중에 남은 인증을 굳이 다 돌릴 이유가 없다). 실행 중인 것은 SDK 의
    `httpTimeout` 이 끊는다.
    ⚠️ `wait=False` 로 두지 않는다 — 종료를 기다리지 않으면 다음 lifespan 이 구 스레드와 겹친다.
    """
    global _executor
    executor, _executor = _executor, None
    if executor is None:
        return
    executor.shutdown(wait=True, cancel_futures=True)
    logger.info("🛑 인증 전용 executor 종료")


def is_auth_executor_running() -> bool:
    """관측용(진단 endpoint·테스트)."""
    return _executor is not None


async def run_in_auth_executor(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """인증 호출을 **이 pool 에서만** 돌린다.

    ⛔ `loop.run_in_executor` 는 **키워드 인자를 받지 않는다** — `functools.partial` 로 감싸야
    한다. 이걸 빠뜨리면 `app=`/`check_revoked=` 가 조용히 사라지고, 그러면 DEFAULT app 이 쓰여
    낮춘 `httpTimeout` 이 통째로 no-op 이 되며 revoked 토큰이 통과한다(둘 다 흔적이 안 남는다).
    """
    executor = _executor
    if executor is None:
        raise AuthExecutorNotReady("인증 전용 executor 가 없다")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, functools.partial(func, *args, **kwargs))
