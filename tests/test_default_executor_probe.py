"""default executor sentinel — **격리의 보호 대상**을 실제로 재는가.

⛔ 이 sentinel 이 없으면 canary 는 자기 성공 기준을 관측하지 못한다: 전용 pool endpoint 는
   전용 pool 만 보므로 "인증이 분리됐다"만 알고 **"동거인이 보호된다"는 끝내 모른다**.
⛔ 그리고 sentinel 이 **있어도 안 돌면** 같은 결과다 — 그래서 여기서는 계산뿐 아니라
   **수명주기와 프로덕션 배선**까지 본다.
"""

import ast
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import auth_executor, default_executor_probe as probe


@pytest.fixture(autouse=True)
def _clean():
    probe.reset_default_executor_probe_metrics()
    auth_executor.shutdown_auth_executor()
    yield
    probe.reset_default_executor_probe_metrics()
    auth_executor.shutdown_auth_executor()


def _run(coro):
    return asyncio.run(coro)


async def _poll_until(predicate, timeout=5.0):
    """⚠️ loop 쪽에서 폴링한다 — `asyncio.to_thread(event.wait)` 로 기다리면 **재려는 pool 을
    테스트가 스스로 굶긴다**(이전에 실제로 겪은 형태)."""
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


# ── 큐 지연 계산 ────────────────────────────────────────────────────────────


def test_idle_default_executor_shows_a_small_delay():
    delay = _run(probe.probe_once())
    assert delay >= 0
    m = probe.default_executor_probe_metrics()
    assert m["started_count"] == 1
    assert m["submitted_count"] == 1
    assert m["outstanding"] == 0
    assert sum(m["histogram"].values()) == 1


def test_saturated_default_executor_shows_a_large_queue_delay():
    """⛔ **이것이 이 sentinel 의 존재 이유다** — 동거 작업이 실제로 기다리는 시간을 잡아낸다."""
    release = threading.Event()
    entered = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        loop = asyncio.get_running_loop()
        # ⚠️ 1-worker 를 default 로 명시 설치 — CPU 수에 의존하지 않고 확실히 포화시킨다.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        squatter = asyncio.create_task(asyncio.to_thread(blocker))
        assert await _poll_until(entered.is_set), "default pool 이 점유되지 않았다 — 전제가 깨졌다"

        probe_task = asyncio.create_task(probe.probe_once())
        await asyncio.sleep(0.05)                 # 이 동안 probe 는 **큐에서 대기**한다
        assert not probe_task.done(), "포화 상태인데 probe 가 즉시 끝났다 — 큐를 못 보고 있다"
        release.set()
        delay = await asyncio.wait_for(probe_task, timeout=10)
        await asyncio.gather(squatter, return_exceptions=True)
        return delay

    delay = _run(scenario())
    assert delay >= 40, f"큐 대기가 반영되지 않았다: {delay}ms"
    assert probe.default_executor_probe_metrics()["queue_delay_ms_max"] >= 40


def test_fully_blocked_pool_is_visible_as_outstanding_not_as_no_samples():
    """⛔ **최악의 상태가 가장 안 보이던 결함.** 한때 측정을 caller 가 소유해서
    `await to_thread(...)` 가 **돌아온 뒤에야** 기록했다 — pool 이 완전히 막히면 probe 가 영영
    돌아오지 않으므로 endpoint 는 `0` 만 보여주고, 그건 **"아직 표본 없음"과 구분되지 않는다**.
    이제 제출은 제출대로 세므로 `submitted=1, started=0, outstanding=1` 로 **직접 보인다**."""
    release = threading.Event()
    entered = threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        squatter = asyncio.create_task(asyncio.to_thread(blocker))
        assert await _poll_until(entered.is_set), "전제가 깨졌다 — pool 이 점유되지 않았다"

        probe_task = asyncio.create_task(probe.probe_once())
        assert await _poll_until(
            lambda: probe.default_executor_probe_metrics()["submitted_count"] == 1
        ), "제출이 세어지지 않았다 — 막힌 pool 이 여전히 '표본 없음'으로 보인다"

        snapshot = probe.default_executor_probe_metrics()
        assert snapshot["started_count"] == 0, "worker 가 시작도 안 했는데 시작으로 세어졌다"
        assert snapshot["outstanding"] == 1, "포화가 outstanding 으로 드러나지 않는다"

        release.set()
        await asyncio.wait_for(probe_task, timeout=10)
        await asyncio.gather(squatter, return_exceptions=True)
        # 갇혀 있던 probe 가 풀리면 스스로 정산된다.
        assert probe.default_executor_probe_metrics()["outstanding"] == 0

    _run(scenario())


class _DelayedResultExecutor(ThreadPoolExecutor):
    """worker 는 **끝났는데** 결과가 loop 로 전달되기 전인 창을 만든다.

    이 창이 있어야 "측정을 누가 소유하는가"를 **결정적으로** 물을 수 있다 — 실제 pool 로는
    그 창이 마이크로초라 경합 테스트가 된다.
    """

    def __init__(self, ran: threading.Event, release: threading.Event):
        super().__init__(max_workers=1)
        self._ran, self._release = ran, release

    def submit(self, fn, /, *args, **kwargs):
        def wrapped():
            result = fn(*args, **kwargs)      # ← 이 안에서 이미 측정이 끝나 있어야 한다
            self._ran.set()
            self._release.wait(timeout=10)    # 결과 전달만 지연시킨다
            return result
        return super().submit(wrapped)


def test_measurement_survives_caller_cancellation_after_the_worker_ran():
    """⛔ 측정 소유권이 **worker** 에 있는지 본다. worker 가 이미 돌았다면, 결과가 loop 로 돌아오기
    전에 caller 가 취소돼도 그 표본은 남아야 한다 — caller 가 기록하면 **그 구간이 통째로
    증발**하고, 하필 가장 오래 기다린 표본이 취소되기 쉬워 **꼬리부터** 사라진다."""
    ran, release = threading.Event(), threading.Event()

    async def scenario():
        asyncio.get_running_loop().set_default_executor(_DelayedResultExecutor(ran, release))
        probe_task = asyncio.create_task(probe.probe_once())
        assert await _poll_until(ran.is_set), "worker 가 돌지 않았다 — 전제가 깨졌다"

        probe_task.cancel()                                    # ← 결과 전달 전에 caller 소멸
        await asyncio.gather(probe_task, return_exceptions=True)
        release.set()

        assert probe.default_executor_probe_metrics()["started_count"] == 1, \
            "caller 취소로 표본이 사라졌다 — 측정을 worker 가 소유하지 않는다"

    _run(scenario())


def test_caller_cancellation_while_queued_keeps_the_stuck_evidence():
    """⚠️ 반대 경우 — 큐에서 대기하다 취소되면 worker 는 **아예 실행되지 않는다**
    (`concurrent.futures` 가 아직 시작 안 한 job 을 실제로 취소한다). 보존할 측정값 자체가 없다.
    ⛔ 대신 **"갇혀 있었다"는 증거는 남아야 한다** — 구 구현은 caller 가 죽으면 아무 흔적도
    남기지 않아 "한 번도 안 돌았다"와 구분되지 않았다."""
    release, entered = threading.Event(), threading.Event()

    def blocker():
        entered.set()
        release.wait(timeout=10)

    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        squatter = asyncio.create_task(asyncio.to_thread(blocker))
        assert await _poll_until(entered.is_set)

        probe_task = asyncio.create_task(probe.probe_once())
        await _poll_until(lambda: probe.default_executor_probe_metrics()["submitted_count"] == 1)
        probe_task.cancel()
        await asyncio.gather(probe_task, return_exceptions=True)
        release.set()
        await asyncio.gather(squatter, return_exceptions=True)

        m = probe.default_executor_probe_metrics()
        assert m["submitted_count"] == 1 and m["started_count"] == 0
        assert m["outstanding"] == 1, "caller 가 죽자 '갇혔다'는 증거까지 사라졌다"

    _run(scenario())


def test_probe_does_not_use_the_auth_executor():
    """⛔ 전용 pool 로 재면 **재려는 대상이 뒤바뀐다**. auth executor 를 아예 띄우지 않아도
    probe 는 동작해야 한다(띄우지 않은 상태에서 전용 pool 을 쓰면 fail-closed 로 예외가 난다)."""

    async def scenario():
        assert not auth_executor.is_auth_executor_running()
        return await probe.probe_once()

    assert _run(scenario()) >= 0


def test_histogram_boundaries_are_fixed_and_bounded():
    """⛔ 무제한 시계열을 만들지 않는다 — 꼬리가 **어느 구간**인지만 알면 된다."""
    for delay, expected in [(0.5, "<=1"), (1, "<=1"), (3, "<=5"), (7, "<=10"),
                            (60, "<=100"), (900, "<=1000"), (9999, ">5000")]:
        assert probe._bucket_label(delay) == expected, f"{delay} → {probe._bucket_label(delay)}"

    for delay in [0.1 * i for i in range(500)]:
        probe._record_started(delay)
    histogram = probe.default_executor_probe_metrics()["histogram"]
    assert len(histogram) <= len(probe.BUCKET_BOUNDARIES_MS) + 1


def test_metrics_accumulate_across_probes():
    async def scenario():
        for _ in range(3):
            await probe.probe_once()

    _run(scenario())
    m = probe.default_executor_probe_metrics()
    assert m["started_count"] == 3
    assert m["submitted_count"] == 3
    assert m["queue_delay_ms_sum"] >= 0
    assert sum(m["histogram"].values()) == 3


# ── interval 검증 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, 0.5, float("nan"), float("inf")])
def test_bad_interval_fails_fast_when_enabled(bad):
    """⛔ 잘못된 값이면 sentinel 이 관측이 아니라 **부하원**이 된다(0·nan → hot loop) 또는
    **조용히 죽는다**(inf → 첫 probe 뒤 영원히 sleep). 켜는 순간 크게 실패해야 한다."""

    async def scenario():
        with pytest.raises(ValueError):
            probe.start_default_executor_probe(True, bad)

    _run(scenario())
    assert probe._probe_task is None, "실패했는데 task 가 남았다"


def test_bad_interval_does_not_block_startup_while_disabled():
    """⚠️ off 면 쓰이지 않는 값이다 — 그것 때문에 기동을 막으면 무관한 배포가 죽는다."""

    async def scenario():
        assert probe.start_default_executor_probe(False, 0) is None

    _run(scenario())


def test_default_interval_is_accepted():
    from app import config
    assert probe.validate_probe_interval(config.DEFAULT_EXECUTOR_PROBE_INTERVAL_SECONDS) == 5.0


# ── 수명주기 (프로덕션 진입점) ──────────────────────────────────────────────


def test_enabled_probe_actually_submits_work():
    """⛔ 배선의 핵심: 켜면 **실제로 제출이 일어나야** 한다. 계산 함수만 테스트하면
    "endpoint 는 있는데 probe 가 안 도는 상태"를 아무도 못 잡는다."""

    async def scenario():
        task = probe.start_default_executor_probe(True, 1.0)
        assert task is not None, "켰는데 task 가 만들어지지 않았다"
        assert await _poll_until(
            lambda: probe.default_executor_probe_metrics()["started_count"] >= 1
        ), "probe task 가 만들어졌는데 실제로 제출·측정하지 않는다"
        await probe.stop_default_executor_probe()

    _run(scenario())


def test_disabled_probe_submits_nothing():
    async def scenario():
        assert probe.start_default_executor_probe(False, 5.0) is None
        await asyncio.sleep(0.05)
        m = probe.default_executor_probe_metrics()
        assert m["submitted_count"] == 0, "off 인데 제출이 일어났다"
        assert m["started_count"] == 0

    _run(scenario())


def test_stop_cancels_and_joins_the_probe_task():
    """⛔ **cancel 만으로는 종료가 보장되지 않는다** — `cancel()` 은 예약일 뿐이라, 합류 없이
    돌아오면 그 시점에 task 는 아직 `done()` 이 아니다. lifespan 은 그 상태로 다음 종료 단계로
    넘어가고, 다음 lifespan 이 구 task 와 겹칠 수 있다."""

    async def scenario():
        task = probe.start_default_executor_probe(True, 1.0)
        await _poll_until(lambda: probe.default_executor_probe_metrics()["started_count"] >= 1)

        await probe.stop_default_executor_probe()

        assert task.done(), "stop 이 돌아왔는데 task 가 아직 살아 있다 — cancel 만 하고 합류를 안 했다"
        assert task.cancelled(), "task 가 취소로 끝나지 않았다"
        assert probe._probe_task is None, "stop 후에도 모듈이 죽은 task 를 붙들고 있다"

    _run(scenario())


def test_stop_is_safe_when_never_started():
    _run(probe.stop_default_executor_probe())


def test_probe_loop_survives_a_failing_probe():
    """⚠️ 관측이 서비스를 멈추게 하지 않는다 — 한 번 실패해도 루프는 계속 돈다."""
    calls = {"n": 0}
    original = probe.probe_once

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("첫 회 실패")
        return await original()

    async def scenario():
        probe.probe_once = flaky
        try:
            probe.start_default_executor_probe(True, 1.0)
            assert await _poll_until(lambda: calls["n"] >= 2), "실패 후 루프가 멈췄다"
            await probe.stop_default_executor_probe()
        finally:
            probe.probe_once = original

    _run(scenario())


# ── 프로덕션 배선 (lifespan) ────────────────────────────────────────────────


def _lifespan_calls() -> set[str]:
    """`app/main.py` 의 `lifespan` 이 실제로 부르는 이름들.

    ⚠️ 왜 AST 인가: 이 리포의 `main.lifespan` 은 DB·Redis·scheduler·크롤러를 전부 띄우므로
    테스트에서 진짜로 진입할 수 없다. 같은 이유로 `test_atomic_write_a2_2.py` 도 (2026-06-21
    startup-ordering 사고 뒤) 배선을 AST 로 잠갔다 — 그 선례를 따른다.
    """
    tree = ast.parse(Path("app/main.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "lifespan"),
              None)
    assert fn is not None, "lifespan 함수를 찾지 못했다"
    return {n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def test_lifespan_starts_and_stops_the_probe():
    """⛔ **배선 제거 변이를 여기서 잡는다.** 수명주기 함수만 테스트하면 lifespan 에서
    호출 두 줄을 지워도 전부 통과한다 — 이 세션에서 반복된 helper-only false green 이다."""
    calls = _lifespan_calls()
    assert "start_default_executor_probe" in calls, "lifespan 이 probe 를 기동하지 않는다"
    assert "stop_default_executor_probe" in calls, "lifespan 이 probe 를 종료하지 않는다"
