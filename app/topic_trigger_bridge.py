"""Worker-thread → main event loop bridge for topic trigger emission (§6.6.2 C1).

`crud.insert_bank_rates_into_db` 류 저장 함수는 APScheduler **sync worker thread**
(running event loop 없음)에서 실행된다. fx/tether topic trigger controller는
`asyncio.create_task`로 coalesce timer를 돌려야 하므로 running loop이 필요한데,
worker thread에서 직접 `request_trigger`를 호출하면 controller의 no_loop 분기로
항상 skip된다 (USDT/KRX는 async 컨텍스트에서 호출하므로 무관).

본 bridge는 lifespan startup에서 capture한 main loop으로 emission callback을
`call_soon_threadsafe` 마샬링한다. crud는 main.py를 import하지 않고 (cycle 회피)
본 모듈만 함수 내부 import한다.

설계 계약 (§6.6.2 verification axis #1/#4):
    - register_main_loop: scheduler 시작 **전** lifespan에서 호출.
    - signal_shutdown: shutdown 진입 즉시 신규 enqueue 차단 (drain 전).
    - schedule_on_loop: loop 미등록 / closed / shutdown → best-effort skip
      (호출자 흐름 영향 X — PR C 중엔 legacy hook이 publish 커버).
    - callback은 loop 위에서 실행되며 예외는 _run_guarded가 격리 (loop default
      exception handler 오염 차단).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger("exchange_rate.topic_trigger_bridge")

# lifespan이 등록한 main event loop. worker thread는 이 참조로 마샬링한다.
_main_loop: Optional[asyncio.AbstractEventLoop] = None
# shutdown 진입 신호 — set되면 신규 enqueue 거부 (drain 중 새 작업 방지).
_shutting_down: bool = False
# schedule_on_loop(worker thread) ⊥ signal_shutdown(main loop) 직렬화 lock.
# flag set 후 어떤 worker thread도 enqueue 못 하게 → shutdown 후 orphan callback
# 방지 (Codex review). schedule는 hot path 아님(변경 드묾) → lock 비용 무시.
_lock = threading.Lock()


def register_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """lifespan startup에서 main event loop 등록 (scheduler 시작 전).

    재등록 시 shutdown 플래그도 초기화 (재시작/테스트 안전).
    """
    global _main_loop, _shutting_down
    with _lock:
        _main_loop = loop
        _shutting_down = False


def signal_shutdown() -> None:
    """shutdown 진입 시 신규 enqueue 차단 (controller drain 전에 호출).

    lock으로 schedule_on_loop과 직렬화 — 이 함수 반환 후엔 어떤 worker thread도
    새 callback을 enqueue할 수 없다 (flag check + enqueue가 같은 lock 안).
    """
    global _shutting_down
    with _lock:
        _shutting_down = True


def reset_for_tests() -> None:
    """테스트 격리용 — 모듈 전역 상태 초기화."""
    global _main_loop, _shutting_down
    with _lock:
        _main_loop = None
        _shutting_down = False


def _run_guarded(callback: Callable[..., Any], args: tuple) -> None:
    """main loop 위에서 callback 실행 — 예외 격리.

    call_soon_threadsafe로 스케줄된 callback이 raise하면 loop의 default
    exception handler로 흘러간다. emission 실패가 그 경로를 오염시키지 않도록
    여기서 흡수한다 (best-effort 철학, broadcast/trigger 정상 경로 영향 X).
    """
    try:
        callback(*args)
    except Exception:
        logger.exception("topic trigger bridge callback 실패 (격리)")


def schedule_on_loop(callback: Callable[..., Any], *args: Any) -> bool:
    """worker thread → main loop로 callback 마샬링 (call_soon_threadsafe).

    Args:
        callback: main loop 위에서 실행할 sync 함수 (예: controller.request_trigger).
        *args: callback 인자.

    Returns:
        True: 스케줄 성공.
        False: loop 미등록 / shutdown 진입 / loop closed / 마샬링 race —
            best-effort skip (호출자 흐름 영향 X).
    """
    # lock으로 signal_shutdown과 직렬화 — flag set 후 enqueue 차단 보장
    # (shutdown 후 orphan callback 방지). flag check + enqueue가 atomic.
    with _lock:
        loop = _main_loop
        if loop is None or _shutting_down:
            return False
        if loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(_run_guarded, callback, args)
            return True
        except RuntimeError:
            # check와 call 사이에 loop이 닫힌 shutdown race — best-effort skip.
            return False


async def drain_loop_callbacks() -> None:
    """signal_shutdown 후 호출 — 이미 큐잉된 bridge callback을 모두 실행시킨다.

    `call_soon_threadsafe`로 enqueue된 callback은 loop _ready에 FIFO. sentinel을
    `call_soon`으로 뒤에 넣고 await하면 앞선 callback이 모두 실행된 뒤 resume →
    그 후 controller를 drain하면 callback이 만든 flush task까지 drain된다
    (shutdown orphan 방지, Codex review). signal_shutdown으로 신규 enqueue가
    이미 차단된 뒤 호출해야 의미가 있다.
    """
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    done = loop.create_future()
    loop.call_soon(done.set_result, None)
    await done
