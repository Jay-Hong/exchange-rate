"""default executor sentinel — **격리의 보호 대상**을 실제로 재는가.

⛔ 이 sentinel 이 없으면 canary 는 자기 성공 기준을 관측하지 못한다: 전용 pool endpoint 는
   전용 pool 만 보므로 "인증이 분리됐다"만 알고 **"동거인이 보호된다"는 끝내 모른다**.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

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


def test_idle_default_executor_shows_a_small_delay():
    async def scenario():
        return await probe.probe_once()

    delay = _run(scenario())
    assert delay >= 0
    m = probe.default_executor_probe_metrics()
    assert m["count"] == 1
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
        for _ in range(2000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set(), "default pool 이 점유되지 않았다 — 전제가 깨졌다"

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
        probe._record(delay)
    histogram = probe.default_executor_probe_metrics()["histogram"]
    assert len(histogram) <= len(probe.BUCKET_BOUNDARIES_MS) + 1


def test_metrics_accumulate_across_probes():
    async def scenario():
        for _ in range(3):
            await probe.probe_once()

    _run(scenario())
    m = probe.default_executor_probe_metrics()
    assert m["count"] == 3
    assert m["queue_delay_ms_sum"] >= 0
    assert sum(m["histogram"].values()) == 3
