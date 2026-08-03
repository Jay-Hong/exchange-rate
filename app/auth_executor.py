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
  fail-closed 로 `AuthExecutorNotReady` 를 던진다.
  ⚠️ **§8-C 로 분류되지 않는다** — `_classify_ws_subscribe_auth_failure` 는 이 예외를 모르므로
  `None` 을 돌려주고 호출부가 **그대로 재전파**해 연결이 정리된다(= fail-loud). 그게 맞다:
  이건 사용자 인증 실패가 아니라 **lifecycle·프로그래밍 결함**이라, 넓은 availability verdict
  (`temporarily_unavailable`)로 접으면 클라가 영구 재시도하고 운영자는 신호를 못 받는다.
  (한때 이 주석이 "호출자가 §8-C 로 분류하게 둔다"고 적었는데 **코드와 달랐다**.)
- ⚠️ **이것은 큐 상한이 아니다.** `ThreadPoolExecutor` 의 작업 큐는 `SimpleQueue` 라 **무제한**이다.
  격리는 *동거인* 을 지킬 뿐 인증 자신의 적체는 그대로다 — ingress 상한(nginx)과
  **연결 내부 subscribe 남용**은 여전히 **별도 열린 항목**이다. "자원 상한 완료" 라고 쓰지 말 것.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from app import config

logger = logging.getLogger("exchange_rate.auth_executor")

T = TypeVar("T")


class AuthExecutorNotReady(RuntimeError):
    """인증 전용 executor 가 없다(미기동·종료됨). ⛔ 여기서 fallback 하지 않는다."""


_executor: ThreadPoolExecutor | None = None
_configured_workers: int = 0

# ── W canary 계측 ───────────────────────────────────────────────────────────
# ⛔ **총 wall time 만 보면 W 부족과 Firebase 지연을 구분할 수 없다** — 그게 W 를 정하려는 canary 의
#    전부인데. 그래서 **큐 대기(queue_wait)** 와 **실행(execution)** 을 나눠 기록한다.
# ⚠️ 토큰·UID 는 기록하지 않는다.
_metrics_lock = threading.Lock()
_metrics: dict[str, Any] = {
    "count": 0,
    "queue_wait_ms_sum": 0.0,
    "queue_wait_ms_max": 0.0,
    "execution_ms_sum": 0.0,
    "execution_ms_max": 0.0,
    "never_started": 0,      # 큐에서 취소돼 worker 에 닿지 못한 건수
    "by_outcome": {},
}


def auth_executor_metrics() -> dict[str, Any]:
    """읽기 전용 스냅샷(진단·테스트). ⚠️ 집계라 분포는 못 준다 — 분포가 필요하면 아래 per-call
    구조화 로그를 canary 기간에만 켠다(`WS_AUTH_EXECUTOR_LOG_TIMINGS`)."""
    with _metrics_lock:
        snapshot = dict(_metrics)
        snapshot["by_outcome"] = dict(_metrics["by_outcome"])
    snapshot["max_workers"] = _configured_workers
    return snapshot


def reset_auth_executor_metrics() -> None:
    with _metrics_lock:
        _metrics.update(count=0, queue_wait_ms_sum=0.0, queue_wait_ms_max=0.0,
                        execution_ms_sum=0.0, execution_ms_max=0.0, never_started=0)
        _metrics["by_outcome"] = {}


def _record(queue_wait_ms: float | None, execution_ms: float | None, outcome: str) -> None:
    with _metrics_lock:
        _metrics["count"] += 1
        _metrics["by_outcome"][outcome] = _metrics["by_outcome"].get(outcome, 0) + 1
        if queue_wait_ms is None:
            _metrics["never_started"] += 1
            return
        _metrics["queue_wait_ms_sum"] += queue_wait_ms
        _metrics["queue_wait_ms_max"] = max(_metrics["queue_wait_ms_max"], queue_wait_ms)
        if execution_ms is not None:
            _metrics["execution_ms_sum"] += execution_ms
            _metrics["execution_ms_max"] = max(_metrics["execution_ms_max"], execution_ms)
    if config.WS_AUTH_EXECUTOR_LOG_TIMINGS:
        # ⚠️ canary 기간에만 켠다 — 재연결 폭주에서 subscribe 마다 한 줄이면 로그가 는다.
        logger.info(
            "ws_auth_timing",
            extra={
                "queue_wait_ms": round(queue_wait_ms, 1),
                "execution_ms": round(execution_ms, 1) if execution_ms is not None else None,
                "outcome": outcome,
                "max_workers": _configured_workers,
            },
        )


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
    global _configured_workers
    _configured_workers = max_workers
    logger.info("✅ 인증 전용 executor 기동", extra={"max_workers": max_workers})


def begin_auth_executor_shutdown() -> ThreadPoolExecutor | None:
    """**차단만 하고 기다리지 않는다** — 신규 submit 차단 + 큐 항목 취소.

    ⛔ `cancel_futures=True` — 큐에만 있는 작업은 **실행하지 않고** 취소한다(종료 중에 남은
    인증을 굳이 다 돌릴 이유가 없다). 실행 중인 것은 SDK 의 `httpTimeout` 이 끊는다.
    ⚠️ 반환한 executor 는 **반드시** `await_auth_executor_shutdown` 으로 마무리해야 한다 —
    안 그러면 다음 lifespan 이 구 스레드와 겹친다.
    """
    global _executor
    executor, _executor = _executor, None
    if executor is None:
        return None
    executor.shutdown(wait=False, cancel_futures=True)
    return executor


async def await_auth_executor_shutdown(executor: ThreadPoolExecutor | None) -> None:
    """실행 중 worker 종료 대기 — **다른 async drain 이 끝난 뒤** 마지막에 await 한다.

    ⛔ **event loop 에서 동기로 `shutdown(wait=True)` 를 부르지 말 것.** 인증은 per-attempt
    `httpTimeout` **+ SDK 재시도/backoff** 라 총시간이 길 수 있는데, 그동안 loop 가 얼어 뒤따르는
    trigger drain · crawler · scheduler 종료가 **한 줄도 못 돈다**. 배포 중 container grace 가
    끝나면 그 정리가 통째로 날아간다. (한때 shutdown 맨 앞에서 동기로 불렀다 — 그 형태였다.)
    """
    if executor is None:
        return
    await asyncio.to_thread(executor.shutdown, wait=True)
    logger.info("🛑 인증 전용 executor 종료")


def shutdown_auth_executor() -> None:
    """동기 편의형 — **테스트/단발 정리 전용**. 프로덕션 lifespan 은 위 2단계를 쓴다."""
    executor = begin_auth_executor_shutdown()
    if executor is None:
        return
    executor.shutdown(wait=True)
    logger.info("🛑 인증 전용 executor 종료")


def is_auth_executor_running() -> bool:
    """관측용(진단 endpoint·테스트)."""
    return _executor is not None


async def run_in_auth_executor(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """인증 호출을 **이 pool 에서만** 돌린다.

    ⛔ `loop.run_in_executor` 는 **키워드 인자를 받지 않는다.** 여기서는 `_timed` **클로저**가
    `*args, **kwargs` 를 그대로 나른다. 이 경로가 깨지면 `app=`/`check_revoked=` 가 조용히
    사라지고 — DEFAULT app 이 쓰여 낮춘 `httpTimeout` 이 통째로 no-op 이 되며 revoked 토큰이
    통과한다(둘 다 프레임·로그 어디에도 흔적이 안 남는다). 그래서 전용 테스트로 잠근다.
    """
    executor = _executor
    if executor is None:
        raise AuthExecutorNotReady("인증 전용 executor 가 없다")
    loop = asyncio.get_running_loop()
    submitted = time.monotonic()
    # ⚠️ worker **스레드가** 채운다 — 큐에서 취소되면 비어 있고, 그게 "실행되지 않았다"의 증거다.
    timings: dict[str, float] = {}

    def _timed() -> T:
        timings["started"] = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            timings["finished"] = time.monotonic()

    # ⛔ `loop.run_in_executor` 는 **위치 인자만** 받는다 — 그래서 인자·키워드를 **클로저**(`_timed`)
    #    가 나른다. (한때 `functools.partial(_timed)` 로 한 번 더 감쌌는데 `_timed` 는 인자가 없어
    #    **아무 일도 하지 않았다** — 변이로 확인하고 걷어냈다. 계약을 지키는 것은 클로저다.)
    try:
        result = await loop.run_in_executor(executor, _timed)
    except BaseException as exc:
        _record(*_split(submitted, timings), outcome=type(exc).__name__)
        raise
    _record(*_split(submitted, timings), outcome="ok")
    return result


def _split(submitted: float, timings: dict[str, float]) -> tuple[float | None, float | None]:
    """제출→worker 시작(queue wait) 과 worker 내부 실행(execution) 을 **나눈다**."""
    started = timings.get("started")
    if started is None:
        return None, None                       # 큐에서 취소 — worker 에 닿지 못했다
    finished = timings.get("finished", started)
    return (started - submitted) * 1000.0, (finished - started) * 1000.0
