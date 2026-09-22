"""큐형 래퍼의 스레드 경계 계약 (SOURCE_HEALTH_COLLECTION_EXPECTED.md 부록 A §7).

asyncio 큐는 스레드 안전하지 않다. 동기 래퍼는 AsyncIOExecutor 가 기본 스레드 풀로 보내므로
worker 스레드에서 `put_nowait` 와 큐 내부 읽기를 하게 된다(debug 루프에서는 RuntimeError 뒤
소비자가 깨어나지 않는 것까지 재현됨). 래퍼를 코루틴으로 두면 APScheduler 가 큐를 소유한 루프
스레드에서 실행한다. 이 파일은 그 경계와, 적재 판정(중복·압력·포화)이 바뀌지 않았음을 잠근다.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import datetime, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import app.scheduler as sched


class _WrongThreadPut(AssertionError):
    pass


class _RecordingQueue(asyncio.PriorityQueue):
    """put 이 일어난 스레드를 기록한다. `fail_with` 가 있으면 넣지 않고 그 예외를 낸다.

    `owner_thread` 가 정해져 있으면 다른 스레드의 put 을 **큐를 바꾸기 전에** 거부한다 — 회귀(동기
    래퍼)가 생겼을 때 다른 스레드의 조작이 대기 중인 소비자를 깨울 수 없는 상태로 망가뜨려 시험 자체가
    멈추는 것을 막는다(debug 루프에서 재현됨).
    """

    def __init__(self, maxsize=25, fail_with=None):
        super().__init__(maxsize=maxsize)
        self.put_threads = []
        self.fail_with = fail_with
        self.owner_thread = None

    def put_nowait(self, item):
        current = threading.get_ident()
        self.put_threads.append(current)
        if self.owner_thread is not None and current != self.owner_thread:
            raise _WrongThreadPut("put from a thread that does not own the queue")
        if self.fail_with is not None:
            raise self.fail_with
        super().put_nowait(item)


def test_selenium_wrapper_is_a_coroutine_function_with_a_distinct_name():
    wrapper = sched.make_selenium_job_wrapper("nh")
    assert inspect.iscoroutinefunction(wrapper)
    assert wrapper.__name__ == "selenium_wrapper_nh"
    assert wrapper.__qualname__ == "selenium_wrapper_nh"


def _run_through_executor(monkeypatch, queue):
    """실제 AsyncIOExecutor 로 래퍼를 한 번 실행하고 (이벤트 코드, 루프 스레드, 소비된 항목) 을 돌려준다."""
    monkeypatch.setattr(sched, "selenium_queue", queue)
    monkeypatch.setattr(sched, "selenium_worker_current_job", None)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        queue.owner_thread = loop_thread
        events = []
        done = asyncio.Event()
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0)

        scheduler = AsyncIOScheduler(event_loop=loop, timezone=timezone.utc)

        def listener(event):
            events.append(event.code)
            done.set()

        scheduler.add_listener(listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        executor = AsyncIOExecutor()
        executor.start(scheduler, "default")
        job = Job(scheduler, id="task_nh", func=sched.make_selenium_job_wrapper("nh"),
                  args=(), kwargs={}, executor="default",
                  trigger=CronTrigger(second="54", timezone=timezone.utc),
                  misfire_grace_time=30, coalesce=True, max_instances=1)
        job._jobstore_alias = "default"
        try:
            executor.submit_job(job, [datetime.now(timezone.utc)])
            await asyncio.wait_for(done.wait(), 2.0)
            consumed = None
            if events == [EVENT_JOB_EXECUTED] and queue.fail_with is None:
                consumed = await asyncio.wait_for(consumer, 2.0)
            return events, loop_thread, consumed
        finally:
            if not consumer.done():
                consumer.cancel()
                try:
                    await consumer
                except asyncio.CancelledError:
                    pass
            executor.shutdown()

    return asyncio.run(scenario())


def test_enqueue_runs_on_the_loop_thread_and_the_consumer_receives_it(monkeypatch):
    queue = _RecordingQueue()
    events, loop_thread, consumed = _run_through_executor(monkeypatch, queue)
    assert events == [EVENT_JOB_EXECUTED]
    assert queue.put_threads == [loop_thread]
    assert consumed[2] == "nh" and consumed[3] is False


def test_put_failure_is_reported_as_a_job_error(monkeypatch):
    queue = _RecordingQueue(fail_with=ValueError("injected"))
    events, loop_thread, _ = _run_through_executor(monkeypatch, queue)
    assert events == [EVENT_JOB_ERROR]
    assert queue.put_threads == [loop_thread]


# ── 적재 판정은 그대로 ─────────────────────────────────────────────────────────

def _enqueue_once(monkeypatch, queue, current_job=None, bank="nh"):
    monkeypatch.setattr(sched, "selenium_queue", queue)
    monkeypatch.setattr(sched, "selenium_worker_current_job", current_job)
    queue.owner_thread = threading.get_ident()   # asyncio.run 은 이 스레드에서 루프를 돌린다
    asyncio.run(sched.make_selenium_job_wrapper(bank)())


def test_normal_enqueue_puts_the_same_item_shape(monkeypatch):
    queue = _RecordingQueue()
    _enqueue_once(monkeypatch, queue)
    priority, stamp, bank, is_retry = queue.get_nowait()
    assert priority == sched.SELENIUM_PRIORITY_MAP.get("nh", 999)
    assert isinstance(stamp, float)
    assert (bank, is_retry) == ("nh", False)


def test_skips_while_the_worker_is_processing_the_same_bank(monkeypatch):
    queue = _RecordingQueue()
    _enqueue_once(monkeypatch, queue, current_job="nh")
    assert queue.put_threads == [] and queue.qsize() == 0


def test_skips_when_the_bank_is_already_waiting(monkeypatch):
    queue = _RecordingQueue()
    queue.put_nowait((1, 0.0, "nh", False))
    queue.put_threads.clear()
    _enqueue_once(monkeypatch, queue)
    assert queue.put_threads == [] and queue.qsize() == 1


def test_skips_under_pressure_at_twenty_waiting(monkeypatch):
    queue = _RecordingQueue()
    for i in range(20):
        queue.put_nowait((1, float(i), f"other{i}", False))
    queue.put_threads.clear()
    _enqueue_once(monkeypatch, queue)
    assert queue.put_threads == [] and queue.qsize() == 20


def test_queue_full_is_swallowed_with_a_warning(monkeypatch, caplog):
    queue = _RecordingQueue(maxsize=1)
    queue.put_nowait((1, 0.0, "other", False))
    queue.put_threads.clear()
    with caplog.at_level("WARNING", logger=sched.logger.name):
        _enqueue_once(monkeypatch, queue)
    assert len(queue.put_threads) == 1 and queue.qsize() == 1
    assert any("포화" in r.getMessage() for r in caplog.records)


def test_enqueues_at_nineteen_waiting(monkeypatch):
    queue = _RecordingQueue()
    for i in range(19):
        queue.put_nowait((1, float(i), f"other{i}", False))
    queue.put_threads.clear()
    _enqueue_once(monkeypatch, queue)
    assert len(queue.put_threads) == 1 and queue.qsize() == 20


def test_enqueues_while_the_worker_processes_a_different_bank(monkeypatch):
    queue = _RecordingQueue()
    _enqueue_once(monkeypatch, queue, current_job="shinhan")
    assert len(queue.put_threads) == 1 and queue.qsize() == 1
