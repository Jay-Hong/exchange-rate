"""인증 전용 executor — **격리를 행동으로** 검증한다.

⛔ "구조 트립와이어로만 잠긴다"는 틀렸다: 기본 executor 를 점유해 놓고 인증이 진행되는지 보면
   **행동으로** 확인된다. 그리고 **반대 방향이 진짜 목표다** — 인증이 폭주해도 동거 `to_thread`
   작업이 진행되는가(측정치 3967ms → 31ms 가 말하는 축이 이쪽이다). 한 방향만 보면
   "인증이 분리됐다"만 알고 "동거인이 보호된다"는 모른다.

⚠️ 순서는 `threading.Event` **게이트**로 만든다 — 실제 sleep 으로 만들면 부하에서 뒤집힌다.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import auth_executor, config


@pytest.fixture(autouse=True)
def _clean_executor():
    """각 테스트는 깨끗한 전역에서 시작하고, 끝나면 반드시 정리한다."""
    auth_executor.shutdown_auth_executor()
    yield
    auth_executor.shutdown_auth_executor()


def _run(coro):
    return asyncio.run(coro)


# ── 계약 ────────────────────────────────────────────────────────────────────


def test_worker_count_must_be_positive():
    with pytest.raises(ValueError):
        auth_executor.start_auth_executor(0)
    with pytest.raises(ValueError):
        auth_executor.start_auth_executor(-1)
    assert not auth_executor.is_auth_executor_running()


def test_missing_executor_is_fail_closed_not_a_silent_fallback():
    """⛔ 미기동 시 `asyncio.to_thread` 로 흘려보내면 **격리가 조용히 사라진다** — 하필 그게
    필요한 순간(기동 직후 폭주)에.
    ⚠️ 이 예외는 **§8-C 로 분류되지 않는다** — `_classify_ws_subscribe_auth_failure` 가 모르므로
    `None` → 호출부가 **그대로 재전파**해 연결이 정리된다(fail-loud). 사용자 인증 실패가 아니라
    lifecycle·프로그래밍 결함이라 넓은 availability verdict 로 접으면 안 된다."""

    async def scenario():
        with pytest.raises(auth_executor.AuthExecutorNotReady):
            await auth_executor.run_in_auth_executor(lambda: "should not run")

    _run(scenario())


def test_keyword_arguments_survive_the_executor_hop():
    """⛔ `loop.run_in_executor` 는 **kwargs 를 받지 않는다**. `functools.partial` 없이 넘기면
    `app=`/`check_revoked=` 가 조용히 사라지고 — DEFAULT app 이 쓰여 낮춘 `httpTimeout` 이
    no-op 이 되며 revoked 토큰이 통과한다. 어디에도 흔적이 안 남으므로 여기서 못박는다."""
    seen: dict = {}

    def fake_verify(token, *, app=None, check_revoked=False):
        seen.update(token=token, app=app, check_revoked=check_revoked)
        return {"uid": "u"}

    async def scenario():
        auth_executor.start_auth_executor(2)
        return await auth_executor.run_in_auth_executor(
            fake_verify, "tok", app="ws-auth-app", check_revoked=True
        )

    assert _run(scenario()) == {"uid": "u"}
    assert seen == {"token": "tok", "app": "ws-auth-app", "check_revoked": True}


def test_default_executor_is_not_replaced():
    """⛔ `set_default_executor` 로 갈아끼우면 격리가 아니라 **전체 이주**다 — 인증이 아닌
    작업까지 이 pool 의 W 에 갇힌다."""

    async def scenario():
        loop = asyncio.get_running_loop()
        before = getattr(loop, "_default_executor", None)
        auth_executor.start_auth_executor(2)
        await auth_executor.run_in_auth_executor(lambda: None)
        after = getattr(loop, "_default_executor", None)
        return before, after

    before, after = _run(scenario())
    assert after is before or after is None


# ── 행동 4방향 ──────────────────────────────────────────────────────────────


def test_saturated_default_executor_does_not_block_auth():
    """방향 1 — 기본 pool 이 꽉 차도 인증은 진행된다(= 인증이 기본 pool 에서 분리됐다)."""
    release = threading.Event()
    entered = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        auth_executor.start_auth_executor(2)
        loop = asyncio.get_running_loop()
        # ⛔ **기본 pool 크기를 추측하지 않는다.** 한때 `_default_executor._max_workers` 를 보고
        #    없으면 8로 가정해 12개를 띄웠는데, 실제 기본값은 `min(32, cpu+4)` 라 **CPU 9개 이상인
        #    기계에서는 포화되지 않는다** — 격리가 없어도 통과하는 false green 이다.
        #    대신 **1-worker executor 를 이 loop 의 default 로 명시 설치**한다: blocker 하나로
        #    확실히 포화되고, private 속성과 CPU 수 어디에도 의존하지 않는다.
        #    ⚠️ 이건 **테스트 loop 한정**이다 — 프로덕션은 default 를 교체하지 않는다(별도 테스트).
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        squatters = [asyncio.create_task(asyncio.to_thread(blocker))]
        # ⛔ 여기서 `asyncio.to_thread(entered.wait, ...)` 를 쓰면 **그것도 같은(포화된) pool 을
        #    필요로 해** blocker 가 timeout 될 때까지 매달린다 — 그러면 포화가 **끝난 뒤에** 인증을
        #    시험하게 되어 테스트가 무의미해진다(실측: 0.06s → 10.07s 로 늘며 의미 상실).
        #    loop 에서 직접 관측한다.
        for _ in range(2000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set(), "기본 pool 이 점유되지 않았다 — 전제가 깨졌다"
        try:
            # 격리가 없으면 이 await 는 blocker 가 풀릴 때까지 반환하지 못한다.
            return await asyncio.wait_for(
                auth_executor.run_in_auth_executor(lambda: "auth-ok"), timeout=5
            )
        finally:
            release.set()
            await asyncio.gather(*squatters, return_exceptions=True)

    assert _run(scenario()) == "auth-ok"


def test_saturated_auth_executor_does_not_block_cohabitant_work():
    """⛔ **이 방향이 목표다**(3967ms → 31ms). 인증이 폭주해 전용 pool 이 꽉 차도 동거
    `asyncio.to_thread` 작업은 진행돼야 한다. 방향 1만 보면 이걸 놓친다 — 인증이 분리됐다는
    사실과 동거인이 보호된다는 사실은 **다른 명제**다."""
    workers = 3
    release = threading.Event()
    entered = threading.Semaphore(0)

    def auth_blocker():
        entered.release()
        release.wait(timeout=10)

    async def scenario():
        auth_executor.start_auth_executor(workers)
        # 전용 pool 을 W 개 모두 **포화** + 큐에도 쌓는다.
        floods = [asyncio.create_task(auth_executor.run_in_auth_executor(auth_blocker))
                  for _ in range(workers * 3)]
        for _ in range(workers):
            await asyncio.to_thread(entered.acquire)
        try:
            return await asyncio.wait_for(asyncio.to_thread(lambda: "cohabitant-ok"), timeout=5)
        finally:
            release.set()
            await asyncio.gather(*floods, return_exceptions=True)

    assert _run(scenario()) == "cohabitant-ok"


def test_cancelled_queued_request_never_runs_later():
    """방향 3 — 큐에만 있던 요청이 wire deadline 으로 취소되면, blocker 를 풀어도 **뒤늦게
    실행되지 않는다**. (실행 중인 것은 취소되지 않는다 — 그건 `httpTimeout` 의 몫이다.)"""
    release = threading.Event()
    entered = threading.Event()
    ran_late = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        auth_executor.start_auth_executor(1)          # 큐를 만들기 쉽게 W=1
        occupied = asyncio.create_task(auth_executor.run_in_auth_executor(blocker))
        await asyncio.to_thread(entered.wait, 10)
        queued = asyncio.create_task(auth_executor.run_in_auth_executor(ran_late.set))
        await asyncio.sleep(0)                        # 제출만 되게 한 턴 양보
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await asyncio.gather(occupied, return_exceptions=True)
        # blocker 가 풀린 뒤에도 큐 항목이 실행되지 않았음을 확인한다.
        await asyncio.to_thread(lambda: None)         # pool 을 한 바퀴 돌린다
        return ran_late.is_set()

    assert _run(scenario()) is False


def test_shutdown_cancels_queue_and_next_start_uses_a_fresh_executor():
    """방향 4 — 종료 시 큐 항목은 취소되고, **다음 lifespan 은 새 executor** 로 정상 동작한다.
    ⚠️ `TestClient` 는 lifespan 을 여러 번 연다 — 종료된 pool 을 재사용하면 두 번째가 통째로 죽는다."""
    release = threading.Event()
    entered = threading.Event()
    ran_after_shutdown = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def first_lifespan():
        auth_executor.start_auth_executor(1)
        occupied = asyncio.create_task(auth_executor.run_in_auth_executor(blocker))
        await asyncio.to_thread(entered.wait, 10)
        queued = asyncio.create_task(
            auth_executor.run_in_auth_executor(ran_after_shutdown.set)
        )
        await asyncio.sleep(0)
        # ⛔ **순서가 판별의 전부다.** 한때 `release.set()` 을 shutdown **앞**에 두었는데, 그러면
        #    worker 가 먼저 비어 큐 항목이 **실행돼 버릴 수 있다**(전체 스위트에서 실측 red,
        #    단독은 통과 — 부하 의존). worker 를 붙잡아 둔 채 shutdown 을 걸고, **큐 항목이 실제로
        #    취소된 것을 관측한 뒤에만** 푸다. 성공 경로는 취소 즉시 빠져나가므로 시간 의존이 없다.
        shutdown_task = asyncio.create_task(
            asyncio.to_thread(auth_executor.shutdown_auth_executor)
        )
        for _ in range(2000):                      # hang 방지 상한 (성공 시 즉시 탈출)
            if queued.done():
                break
            await asyncio.sleep(0.001)
        release.set()
        await shutdown_task
        await asyncio.gather(occupied, queued, return_exceptions=True)
        assert queued.cancelled(), "종료가 큐 항목을 취소하지 않았다"

    async def second_lifespan():
        auth_executor.start_auth_executor(2)
        return await auth_executor.run_in_auth_executor(lambda: "second-ok")

    _run(first_lifespan())
    assert not auth_executor.is_auth_executor_running()
    assert ran_after_shutdown.is_set() is False, "종료가 큐 항목을 실행해 버렸다"
    assert _run(second_lifespan()) == "second-ok"


def test_production_two_phase_shutdown_keeps_the_event_loop_running():
    """⛔ **프로덕션 경로를 직접 구동한다.** 기존 테스트는 동기 편의 함수
    `shutdown_auth_executor()` 만 썼는데, 프로덕션 lifespan 은 `begin_...` + `await_...` 를 **따로**
    부른다 — 그래서 `await_...` 를 no-op 으로 만들어도 기존 테스트는 통과했다(false green).

    이 테스트가 잠그는 것이 분리의 **이유** 그 자체다: worker 가 막혀 있는 동안에도
    **event loop 는 계속 돌아야** 뒤따르는 drain(trigger/crawler/scheduler)이 실행된다.
    """
    release = threading.Event()
    entered = threading.Event()
    ran_queued = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        auth_executor.start_auth_executor(1)                    # W=1 — 큐를 만들기 쉽게
        running = asyncio.create_task(auth_executor.run_in_auth_executor(blocker))
        for _ in range(2000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set(), "worker 가 점유되지 않았다 — 전제가 깨졌다"
        queued = asyncio.create_task(auth_executor.run_in_auth_executor(ran_queued.set))
        await asyncio.sleep(0)

        # ── 1단계: 차단만 ──
        closing = auth_executor.begin_auth_executor_shutdown()
        assert closing is not None
        assert not auth_executor.is_auth_executor_running(), "전역이 비워지지 않았다"
        with pytest.raises(auth_executor.AuthExecutorNotReady):
            await auth_executor.run_in_auth_executor(lambda: "new-submit")   # 신규 submit fail-closed

        # ── 2단계: 대기는 아직 끝나면 안 된다 ──
        awaiting = asyncio.create_task(auth_executor.await_auth_executor_shutdown(closing))
        heartbeats = 0
        for _ in range(50):
            await asyncio.sleep(0.001)
            heartbeats += 1                                     # ← loop 가 살아 있다는 증거
            if awaiting.done():
                break
        assert not awaiting.done(), "worker 가 막혀 있는데 대기가 끝났다 — 합류를 안 한 것이다"
        assert heartbeats >= 50, "event loop 가 멈췄다 — 이 분리의 이유가 사라진다"

        # ── worker 해제 → 합류 완료, 다음 executor 정상 기동 ──
        release.set()
        await asyncio.wait_for(awaiting, timeout=10)
        await asyncio.gather(running, queued, return_exceptions=True)
        assert queued.cancelled(), "큐 항목이 취소되지 않았다"
        assert not ran_queued.is_set(), "취소된 큐 항목이 실행됐다"

        auth_executor.start_auth_executor(2)
        return await auth_executor.run_in_auth_executor(lambda: "next-lifespan-ok")

    assert _run(scenario()) == "next-lifespan-ok"


def _timing_logs(caplog) -> list[dict]:
    return [r.__dict__ for r in caplog.records if r.getMessage() == "ws_auth_timing"]


def test_execution_time_is_recorded_by_the_worker_not_the_cancelled_caller(monkeypatch, caplog):
    """⛔ **이 슬라이스에서 가장 중요한 계측 축.** wire deadline 이 발화해도 스레드는 계속 도는 것이
    설계의 핵심이라, 그 경우가 바로 Firebase 실행시간을 재야 할 자리다. 한때 caller 쪽에서 기록해
    취소 순간 execution 을 **0ms 로 확정**했고 worker 종료 뒤에도 갱신하지 않았다(실측 0.0/0.0)."""
    monkeypatch.setattr(config, "WS_AUTH_EXECUTOR_LOG_TIMINGS", True)
    caplog.set_level("INFO", logger="exchange_rate.auth_executor")
    release = threading.Event()
    entered = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        auth_executor.reset_auth_executor_metrics()
        auth_executor.start_auth_executor(1)
        task = asyncio.create_task(auth_executor.run_in_auth_executor(blocker))
        for _ in range(2000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        during = auth_executor.auth_executor_metrics()
        release.set()
        for _ in range(2000):                      # worker 가 실제로 끝나기를 기다린다
            if auth_executor.auth_executor_metrics()["execution_ms_max"] > 0:
                break
            await asyncio.sleep(0.001)
        return during, auth_executor.auth_executor_metrics()

    during, after = _run(scenario())
    assert during["caller_cancelled_while_running"] == 1, "실행 중 취소가 기록되지 않았다"
    assert during["execution_ms_max"] == 0.0, "worker 가 끝나기 전에 execution 을 확정했다"
    assert after["execution_ms_max"] > 0, "worker 종료 뒤에도 실제 execution 이 기록되지 않았다"
    assert after["never_started"] == 0, "실행 중 취소를 never_started 로 오분류했다"


def test_completed_worker_is_not_counted_as_cancelled_while_running(monkeypatch):
    """⛔ **반대 방향.** 기존 취소 테스트는 worker 를 계속 막아 두므로 "이미 끝난 worker" 를 보지
    못한다. `future.cancelled()` 로 판정하면 worker 가 **정상 완료**했는데 event loop 가 결과 전달을
    아직 못 한 창에서 `not cancelled()` 가 참이 되어 **완료된 건을 '실행 중 취소'로 오분류**한다
    (실측: outcome=ok 인데 caller_cancelled_while_running=1). 부하가 클 때 — W 를 재려는 바로 그
    상황에서 — 전달이 늦어 자주 생기므로 W 판단을 왜곡한다.
    """
    # ⛔ **경합으로 창을 만들지 말 것.** 한때 "worker future 가 `done()` 이 될 때까지 loop 를
    #    붙잡고 그 사이에 취소한다"로 짰는데, 그건 취소가 `_set_state`(concurrent → asyncio 결과
    #    전달) 를 **이겨야만** 성립한다. 로컬에선 늘 이기고 **CI(Linux)에선 늘 져서** 8회 연속
    #    `DID NOT RAISE` 로 red 였다. 그리고 지면 취소가 아예 안 일어나므로 단언이 **공허**해진다
    #    (`except CancelledError` 블록에 닿지 못해 어떤 구현이든 통과한다).
    # → worker 가 `_timed` 를 **완전히 끝낸 뒤**(= `_record` + `finished_evt` 완료) concurrent
    #   future 의 완료만 gate 로 붙잡는다. 그러면 "worker 는 끝났는데 전달만 남은" 창이
    #   **플랫폼과 무관하게 결정적으로** 열리고, 그 안에서 취소가 반드시 도달한다.
    gate = threading.Event()
    worker_done = threading.Event()

    class _DelayedDeliveryExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            def wrapped():
                result = fn(*args, **kwargs)   # ← `_timed` 가 여기서 이미 제 값을 기록한다
                worker_done.set()
                gate.wait(timeout=10)          # 결과 **전달만** 지연 (future 는 PENDING 유지)
                return result
            return super().submit(wrapped)

    async def scenario():
        auth_executor.reset_auth_executor_metrics()
        monkeypatch.setattr(auth_executor, "ThreadPoolExecutor", _DelayedDeliveryExecutor)
        auth_executor.start_auth_executor(1)
        task = asyncio.create_task(auth_executor.run_in_auth_executor(lambda: "ok"))

        deadline = time.monotonic() + 5
        while not worker_done.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert worker_done.is_set(), "worker 가 끝나지 않았다 — 전제가 깨졌다"
        assert not task.done(), "결과 전달이 gate 로 막히지 않았다 — 전제가 깨졌다"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        for _ in range(2000):
            if auth_executor.auth_executor_metrics()["count"] >= 1:
                break
            await asyncio.sleep(0.001)
        return auth_executor.auth_executor_metrics()

    m = _run(scenario())
    assert m["by_outcome"].get("ok") == 1, "worker 는 정상 완료했어야 한다"
    assert m["caller_cancelled_while_running"] == 0, \
        "이미 끝난 worker 를 '실행 중 취소'로 셌다 — W 판단이 왜곡된다"
    assert m["never_started"] == 0


def test_queued_cancellation_is_recorded_and_logged_with_nulls(monkeypatch, caplog):
    """큐에서 취소된 건은 **과부하 신호**다 — 집계뿐 아니라 구조화 로그에도 남아야 canary 에서 본다."""
    monkeypatch.setattr(config, "WS_AUTH_EXECUTOR_LOG_TIMINGS", True)
    caplog.set_level("INFO", logger="exchange_rate.auth_executor")
    release = threading.Event()
    entered = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        auth_executor.reset_auth_executor_metrics()
        auth_executor.start_auth_executor(1)
        running = asyncio.create_task(auth_executor.run_in_auth_executor(blocker))
        for _ in range(2000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        queued = asyncio.create_task(auth_executor.run_in_auth_executor(lambda: "never"))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        for _ in range(2000):
            if auth_executor.auth_executor_metrics()["never_started"] == 1:
                break
            await asyncio.sleep(0.001)
        return auth_executor.auth_executor_metrics()

    m = _run(scenario())
    assert m["never_started"] == 1
    nulls = [r for r in _timing_logs(caplog)
             if r["outcome"] == "never_started"]
    assert len(nulls) == 1, "never_started 가 구조화 로그에 남지 않았다 — canary 에서 못 본다"
    assert nulls[0]["queue_wait_ms"] is None and nulls[0]["execution_ms"] is None


class _QueuedMarker(Exception):
    """queue-heavy 호출을 **값과 무관한 축**(outcome)으로 식별하기 위한 표식."""


def test_queue_heavy_and_execution_heavy_calls_are_not_swapped(monkeypatch, caplog):
    """⛔ 집계 max 두 개가 각각 >0 인지만 보면 **두 값을 서로 바꿔도 통과**한다.
    ⛔ 그렇다고 per-call 로그에서 **측정값으로 레코드를 고르면 여전히 못 잡는다** — 값이 뒤바뀌면
    라벨도 함께 뒤바뀌어 단언이 순환한다(실측: 그렇게 썼더니 swap 변이가 SURVIVED).
    → 각 호출을 **outcome** 으로 식별한다. outcome 은 swap 의 영향을 받지 않는다.
    """
    monkeypatch.setattr(config, "WS_AUTH_EXECUTOR_LOG_TIMINGS", True)
    caplog.set_level("INFO", logger="exchange_rate.auth_executor")
    release = threading.Event()
    entered = threading.Event()

    def blocker():                                  # execution-heavy: 즉시 시작, 오래 실행
        entered.set()
        release.wait(timeout=10)

    def queued_marker():                            # queue-heavy: 오래 대기, 즉시 종료
        raise _QueuedMarker()

    async def scenario():
        auth_executor.reset_auth_executor_metrics()
        auth_executor.start_auth_executor(1)
        heavy = asyncio.create_task(auth_executor.run_in_auth_executor(blocker))
        for _ in range(2000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        waiting = asyncio.create_task(auth_executor.run_in_auth_executor(queued_marker))
        await asyncio.sleep(0.05)                   # 큐에서 확실히 대기시킨다
        release.set()
        await heavy
        with pytest.raises(_QueuedMarker):
            await waiting
        return _timing_logs(caplog)

    logs = _run(scenario())
    by_outcome = {r["outcome"]: r for r in logs if r["queue_wait_ms"] is not None}
    assert "ok" in by_outcome and "_QueuedMarker" in by_outcome, f"기록이 부족하다: {by_outcome}"

    execution_heavy = by_outcome["ok"]              # blocker — 즉시 시작해 오래 실행
    queue_heavy = by_outcome["_QueuedMarker"]       # 줄 서 있다 즉시 종료

    assert execution_heavy["execution_ms"] > execution_heavy["queue_wait_ms"], \
        "즉시 시작해 오래 실행된 호출인데 execution 이 queue wait 보다 작다 — 두 값이 뒤바뀌었다"
    assert queue_heavy["queue_wait_ms"] > queue_heavy["execution_ms"], \
        "줄 서 있다 즉시 끝난 호출인데 queue wait 가 execution 보다 작다 — 두 값이 뒤바뀌었다"


def test_repeated_start_does_not_leak_a_second_pool():
    """중복 startup 방어 — 새로 만들면 구 pool 이 누수된다(스레드가 남는다)."""

    async def scenario():
        auth_executor.start_auth_executor(2)
        first = auth_executor._executor
        auth_executor.start_auth_executor(5)
        return first is auth_executor._executor

    assert _run(scenario()) is True
