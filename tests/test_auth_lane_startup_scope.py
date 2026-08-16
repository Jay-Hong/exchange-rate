"""S1a′ — startup 실패 시 **이번 시도가 만든** auth lane 만 보상 종료한다.

⛔ 이 scope 는 lifespan 전체를 트랜잭션화하지 않는다. 되돌리는 것은 executor 하나뿐이고,
   probe·scheduler·collector 의 startup side effect 는 남는다.
"""
import asyncio

import pytest

from app import auth_executor


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_lane():
    auth_executor.shutdown_auth_executor()
    # ⛔ shutdown+합류 뒤의 `Busy` 는 **정상 상태가 아니라 앞 테스트의 장부 누수**다 —
    #    삼키면 그 누수가 영원히 숨는다. 그대로 실패시킨다.
    auth_executor.reset_auth_executor_metrics()
    yield
    auth_executor.shutdown_auth_executor()


# ── 기존 계약 보존선 ────────────────────────────────────────────────────────
def test_live_pool_start_is_idempotent():
    """⛔ live pool 교체는 **구 pool 누수**다 — `test_auth_executor.py` 가 잠근 계약을 여기서도 지킨다."""
    auth_executor.start_auth_executor(2)
    first = auth_executor._ws_lane._executor
    auth_executor.start_auth_executor(5)
    assert auth_executor._ws_lane._executor is first
    assert auth_executor.auth_executor_metrics()["max_workers"] == 2


def test_restart_after_shutdown_applies_the_new_configuration():
    auth_executor.start_auth_executor(2)
    auth_executor.shutdown_auth_executor()
    auth_executor.start_auth_executor(5)
    assert auth_executor.auth_executor_metrics()["max_workers"] == 5


# ── scope 계약 ──────────────────────────────────────────────────────────────
def test_normal_exit_keeps_the_lane_live():
    """⛔ `finally` 정리로 바뀌면 정상 startup 뒤 lane 이 죽는다 — 그 변이를 잡는다."""
    async def scenario():
        async with auth_executor.ws_auth_lane_startup_scope(2):
            assert auth_executor.is_auth_executor_running()
        return auth_executor.is_auth_executor_running()
    assert _run(scenario()) is True


def test_tail_failure_rolls_back_the_lane_this_startup_created():
    async def scenario():
        with pytest.raises(RuntimeError):
            async with auth_executor.ws_auth_lane_startup_scope(2):
                raise RuntimeError("late startup 실패")
        return auth_executor.is_auth_executor_running()
    assert _run(scenario()) is False


def test_retry_of_the_scope_applies_the_requested_configuration():
    """⚠️ 이것은 **auth-lane scope 재시도**다 — lifespan 전체 재시도는 보장하지 않는다
    (late failure 뒤에는 probe·scheduler·collector side effect 가 남는다)."""
    async def scenario():
        with pytest.raises(RuntimeError):
            async with auth_executor.ws_auth_lane_startup_scope(2):
                raise RuntimeError("실패")
        async with auth_executor.ws_auth_lane_startup_scope(7):
            pass
        return auth_executor.auth_executor_metrics()["max_workers"]
    assert _run(scenario()) == 7


def test_pre_existing_lane_is_not_ours_to_close(monkeypatch):
    """⛔ 이미 돌던 lane 은 이번 startup 이 만든 게 아니다 — identity 까지 같아야 한다."""
    auth_executor.start_auth_executor(3)
    before = auth_executor._ws_lane._executor

    async def scenario():
        with pytest.raises(RuntimeError):
            async with auth_executor.ws_auth_lane_startup_scope(9):
                raise RuntimeError("실패")
        return auth_executor.is_auth_executor_running(), auth_executor._ws_lane._executor
    running, after = _run(scenario())
    assert running is True, "남의 lane 을 내렸다"
    assert after is before, "같은 executor 가 아니다 — 교체됐다"
    assert auth_executor.auth_executor_metrics()["max_workers"] == 3


def test_partial_success_start_raises_after_creating_still_rolls_back(monkeypatch):
    """⛔ `start()` 를 try **밖**에 두면 이 행이 red 다(codex Blocker) — 생성 뒤 raise 는
    rollback 되지 않아 pool 이 샌다."""
    real = auth_executor._ws_lane.start_auth_executor

    def creating_then_raising(w):
        real(w)                       # executor 를 실제로 만든다
        raise RuntimeError("생성 뒤 예외")
    monkeypatch.setattr(auth_executor._ws_lane, "start_auth_executor", creating_then_raising)

    async def scenario():
        with pytest.raises(RuntimeError):
            async with auth_executor.ws_auth_lane_startup_scope(2):
                pass
        return auth_executor.is_auth_executor_running()
    assert _run(scenario()) is False, "생성 뒤 예외에서 lane 이 남았다"


def test_start_raising_before_creation_keeps_original_error_and_no_lane():
    async def scenario():
        with pytest.raises(ValueError):
            async with auth_executor.ws_auth_lane_startup_scope(0):   # 양수 아님 → 생성 전 raise
                pass
        return auth_executor.is_auth_executor_running()
    assert _run(scenario()) is False


def test_rollback_joins_the_running_worker():
    """⛔ **결정적으로** 합류를 시험한다. 초판은 worker 가 즉시 반환해 `begin` 만으로도
    스레드가 먼저 끝날 수 있었다 — 스케줄링 운에 기댄 테스트였다(codex).

    이제 worker 를 `release` 에 막고, **rollback 이 시작된 뒤** helper 스레드가 푼다.
      · 정상 구현: `await_...` 가 release 까지 기다린다 → 스레드 0
      · 합류 생략: release 전에 scope 를 빠져나온다 → 스레드 살아 있음
    """
    import threading

    prefix = auth_executor._ws_lane._thread_prefix

    def _workers() -> int:
        return len([x for x in threading.enumerate() if x.name.startswith(prefix)])

    entered, release = threading.Event(), threading.Event()

    def occupy():
        entered.set()
        release.wait(5)        # ⛔ 막힌다 — 합류가 실제로 기다려야만 끝난다
        return 1

    async def scenario():
        with pytest.raises(RuntimeError):
            async with auth_executor.ws_auth_lane_startup_scope(2):
                fut = auth_executor._ws_lane._executor.submit(occupy)
                assert entered.wait(5), "worker 가 시작되지 않았다 — 이 테스트가 공허해진다"
                assert _workers() > 0
                # rollback 이 시작된 **뒤** 풀어 준다. 정상 구현은 여기서 기다린다.
                threading.Timer(0.3, release.set).start()
                raise RuntimeError("late startup 실패")
        return _workers(), fut.done()

    workers, done = _run(scenario())
    assert workers == 0, "합류(await_...) 없이 begin 만 했다 — worker 스레드가 남았다"
    assert done, "합류했다면 제출된 작업이 끝나 있어야 한다"


def test_real_task_cancellation_rolls_back_and_propagates():
    """⛔ 수동 `raise CancelledError` 는 **실제 취소가 아니다** — cleanup `await` 가 취소된
    task 안에서 도는지를 시험하지 않는다(codex). 외부 task 를 진짜로 cancel 한다."""
    async def scenario():
        started = asyncio.Event()

        async def body():
            async with auth_executor.ws_auth_lane_startup_scope(2):
                started.set()
                await asyncio.sleep(30)          # 여기서 취소된다

        task = asyncio.create_task(body())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return auth_executor.is_auth_executor_running(), task.cancelled()

    running, cancelled = _run(scenario())
    assert running is False, "실제 취소에서 rollback 이 안 돌았다"
    assert cancelled, "원래 CancelledError 가 전파되지 않았다"


def test_rollback_failure_does_not_mask_the_startup_cause(monkeypatch):
    """⛔ rollback 이 던지면 원래 startup 예외가 사라져 운영자가 진짜 원인을 못 본다."""
    def boom():
        raise RuntimeError("rollback 자체 실패")
    # ⛔ **lane 을 직접 patch 한다.** S1b 에서 scope 가 lane 메서드를 직접 부르도록 바뀌어
    #    모듈 facade patch 는 더 이상 닿지 않는다 — 모듈 docstring 이 경고한 그 함정이다.
    #    (이 테스트들이 red 로 그 변화를 드러냈다: 공허하게 통과하지 않았다.)
    monkeypatch.setattr(auth_executor._ws_lane, "begin_auth_executor_shutdown", boom)

    async def scenario():
        with pytest.raises(RuntimeError, match="원래 startup 실패"):
            async with auth_executor.ws_auth_lane_startup_scope(2):
                raise RuntimeError("원래 startup 실패")
    _run(scenario())


def test_rollback_failure_is_actually_logged(monkeypatch, caplog):
    """⛔ 원인 보존만 단언하면 `logger.exception` 을 `pass` 로 지워도 통과한다 — 그러면
    rollback 실패가 **아무 데도 안 남는다**(codex)."""
    def boom():
        raise RuntimeError("rollback 자체 실패")
    # ⛔ **lane 을 직접 patch 한다.** S1b 에서 scope 가 lane 메서드를 직접 부르도록 바뀌어
    #    모듈 facade patch 는 더 이상 닿지 않는다 — 모듈 docstring 이 경고한 그 함정이다.
    #    (이 테스트들이 red 로 그 변화를 드러냈다: 공허하게 통과하지 않았다.)
    monkeypatch.setattr(auth_executor._ws_lane, "begin_auth_executor_shutdown", boom)

    async def scenario():
        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="원래 startup 실패"):
                async with auth_executor.ws_auth_lane_startup_scope(2):
                    raise RuntimeError("원래 startup 실패")
    _run(scenario())
    assert any("rollback 실패" in r.getMessage() for r in caplog.records), \
        "rollback 실패가 로그에 남지 않았다"


def test_logging_failure_does_not_mask_the_startup_cause(monkeypatch):
    """⛔ 진단 로깅 **자체**가 던지면 바깥 `raise` 에 못 가 원인이 덮인다(codex Blocker)."""
    def boom():
        raise RuntimeError("rollback 자체 실패")
    def log_boom(*a, **k):
        raise RuntimeError("로깅도 실패")
    # ⛔ **lane 을 직접 patch 한다.** S1b 에서 scope 가 lane 메서드를 직접 부르도록 바뀌어
    #    모듈 facade patch 는 더 이상 닿지 않는다 — 모듈 docstring 이 경고한 그 함정이다.
    #    (이 테스트들이 red 로 그 변화를 드러냈다: 공허하게 통과하지 않았다.)
    monkeypatch.setattr(auth_executor._ws_lane, "begin_auth_executor_shutdown", boom)
    monkeypatch.setattr(auth_executor.logger, "exception", log_boom)

    async def scenario():
        with pytest.raises(RuntimeError, match="원래 startup 실패"):
            async with auth_executor.ws_auth_lane_startup_scope(2):
                raise RuntimeError("원래 startup 실패")
    _run(scenario())


def test_rollback_preserves_the_ledger_and_last_applied_configuration():
    """⛔ rollback 은 장부를 리셋하지 않는다. `max_workers` 는 **마지막으로 성공 적용된**
    pool 구성값이다(마지막 *요청*값이 아니다)."""
    async def scenario():
        async with auth_executor.ws_auth_lane_startup_scope(2):
            await auth_executor.run_in_auth_executor(lambda: 1)
        before = auth_executor.auth_executor_metrics()
        auth_executor.shutdown_auth_executor()
        with pytest.raises(RuntimeError):
            async with auth_executor.ws_auth_lane_startup_scope(4):
                raise RuntimeError("실패")
        return before, auth_executor.auth_executor_metrics()
    before, after = _run(scenario())
    for k in ("count", "submitted_total", "started_total", "ledger_errors_total"):
        assert after[k] == before[k], f"rollback 이 장부 {k} 를 건드렸다"
    assert after["max_workers"] == 4, "마지막으로 성공 적용된 구성값이어야 한다"


if __name__ == "__main__":
    pytest.main([__file__])
