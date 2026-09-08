"""실제 bootstrap/runner/pipe/부모/단일 Queue item 연결, IBK 수집기는 합성 결과.

DB·공식 HTTP·Selenium·Telegram 통합 테스트가 아니다. 실제 child에서는 refresh를
명시적으로 차단하며 실제 크롤러 대신 작은 결과 공급자를 주입한다.
"""

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.crawlers import runner
from app import scheduler
from app.ibk_parent_runner import BOOTSTRAP, IbkParentRunner
from app.ibk_result_protocol import (
    PAIRS, IbkExecutionFailure, IbkProtocolError, IbkReason, IbkResult, IbkSource,
    IbkStatus, decode_ibk_result, encode_ibk_result,
)
from app.ibk_run_context import IbkRunContext
from app.ibk_subprocess_capture import IbkProcessCapture, capture_ibk_subprocess


def make_result(context, status=IbkStatus.OBSERVED):
    reason = {IbkStatus.OBSERVED: IbkReason.NORMAL,
              IbkStatus.PRESERVED: IbkReason.OFFICIAL_NO_SESSION,
              IbkStatus.DEGRADED: IbkReason.SELENIUM_STRICT_REJECTED,
              IbkStatus.FAILED: IbkReason.DB_ERROR}[status]
    return IbkResult(
        1, context.run_id, status, reason, context.reference_time.isoformat(),
        context.expected_service_date,
        context.expected_service_date if status is IbkStatus.OBSERVED else None,
        context.reference_time.isoformat() if status is IbkStatus.OBSERVED else None,
        IbkSource.OFFICIAL_POST if status is IbkStatus.OBSERVED else IbkSource.DB_SNAPSHOT,
        tuple(sorted(PAIRS)) if status is IbkStatus.OBSERVED else (),
        None if status is IbkStatus.FAILED else tuple(sorted(PAIRS)),
        None if status is IbkStatus.FAILED else (),
        None if status is IbkStatus.FAILED else 0,
        None if status is IbkStatus.FAILED else True,
    )


def capture_result(context, status=IbkStatus.OBSERVED, **changes):
    stdout = encode_ibk_result(make_result(context, status))
    return replace(IbkProcessCapture(0, stdout, b"", len(stdout), 0, False, False, None), **changes)


@pytest.fixture
def context():
    return IbkRunContext("test_run", datetime.now(timezone.utc))


@pytest.fixture
def refresh(monkeypatch):
    # runner's conditional import sees this stub, never queries even the test DB.
    from app import atomic_write_refresh
    mock = MagicMock()
    monkeypatch.setattr(atomic_write_refresh, "refresh_write_mode_cache", mock)
    return mock


@pytest.mark.parametrize("stamp,day", [
    ("2026-09-07T07:59:59+09:00", "2026-09-06"),
    ("2026-09-07T08:00:00+09:00", "2026-09-07"),
    ("2026-09-07T08:34:59+09:00", "2026-09-07"),
    ("2026-09-07T08:35:00+09:00", "2026-09-07"),
    ("2026-09-04T23:59:59+09:00", "2026-09-04"),
    ("2026-09-05T00:00:00+09:00", "2026-09-04"),
    ("2026-09-05T06:01:34+09:00", "2026-09-04"),
    ("2026-09-06T23:00:00Z", "2026-09-07"),
])
def test_parent_rollover_is_timezone_aware(stamp, day):
    context = IbkRunContext("rollover", datetime.fromisoformat(stamp))
    assert context.expected_service_date == day
    assert IbkRunContext.from_arguments(context.arguments()) == context


@pytest.mark.parametrize("args", [(), ("ibk",), ("nh", "--run-id", "x", "--reference-time", "bad"),
    ("ibk", "--run-id", "!", "--reference-time", "2026-09-07T08:00:00+09:00"),
    ("ibk", "--run-id", "x", "--reference-time", "2026-09-07T08:00:00"),
    ("ibk", "--run-id", "x", "--reference-time", "bad"), None,
])
def test_invalid_context_arguments_fail_closed(args):
    with pytest.raises(IbkProtocolError):
        IbkRunContext.from_arguments(args)


def test_unbound_adapter_stops_before_refresh_or_legacy(context, refresh, monkeypatch):
    legacy = MagicMock(side_effect=AssertionError("must not crawl"))
    monkeypatch.setitem(runner.CRAWLER_MAP, "ibk", legacy)
    assert runner.main(argv=context.arguments(), result_fd=9) == 2
    refresh.assert_not_called()
    legacy.assert_not_called()


@pytest.mark.parametrize("fd", [0, 1, 2, True])
def test_direct_protocol_invocation_requires_private_channel(fd, context, refresh):
    producer = MagicMock()
    assert runner.main(argv=context.arguments(), result_fd=fd, ibk_result_crawler=producer) == 2
    producer.assert_not_called()
    refresh.assert_not_called()


@pytest.mark.parametrize("args", [(), ("ibk", "extra"),
    ("ibk", "--run-id", "direct", "--reference-time", "2026-09-07T08:00:00+09:00")])
def test_no_private_channel_uses_legacy_usage_before_any_crawler(args, refresh, caplog):
    producer = MagicMock()
    with pytest.raises(SystemExit) as exc:
        runner.main(argv=args, ibk_result_crawler=producer)
    assert exc.value.code == 2
    assert "Usage: python -m app.crawlers.runner" in caplog.text
    assert "IBK_RESULT_ARGUMENTS_REJECTED" not in caplog.text
    producer.assert_not_called()
    refresh.assert_not_called()


def test_legacy_launcher_cannot_select_bootstrap(monkeypatch):
    # Characterize the unchanged legacy launcher during the dormant IBK rollout.
    # PIPE kwargs are NOT a permanent policy: an intentional legacy drain repair
    # must update these assertions while preserving the legacy/bootstrap split.
    proc = MagicMock(returncode=0, wait=AsyncMock())
    spawn = AsyncMock(return_value=proc)
    stats = MagicMock()
    monkeypatch.setattr(scheduler.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(scheduler, "crawler_stats", stats)
    assert asyncio.run(scheduler.execute_with_timeout("ibk"))
    spawn.assert_awaited_once_with(sys.executable, "-m", "app.crawlers.runner", "ibk",
                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stats.record_success.assert_called_once()
    stats.record_failure.assert_not_called()


def test_typed_parent_launches_only_fixed_bootstrap():
    async def capture(argv, **kwargs):
        assert argv[:3] == (sys.executable, "-u", str(BOOTSTRAP))
        assert BOOTSTRAP.is_absolute()
        context = IbkRunContext.from_arguments(argv[3:])
        return capture_result(context)
    parent = IbkParentRunner(capture=capture, event_sink=lambda _: True)
    assert asyncio.run(parent.execute()).result.status is IbkStatus.OBSERVED


@pytest.mark.parametrize("status", list(IbkStatus))
def test_runner_emits_one_valid_result_without_blanket_success(status, context, refresh, monkeypatch, caplog):
    written = bytearray()
    def short_write(fd, data):
        assert fd == 9
        n = min(17, len(data))
        written.extend(data[:n])
        return n
    monkeypatch.setattr(runner.os, "write", short_write)
    producer = MagicMock(return_value=make_result(context, status))
    with caplog.at_level("INFO"):
        assert runner.main(argv=context.arguments(), result_fd=9, ibk_result_crawler=producer) == 0
    refresh.assert_called_once()
    producer.assert_called_once_with(context)
    assert decode_ibk_result(bytes(written), context.run_id).status is status
    assert "subprocess 크롤링 성공" not in caplog.text


@pytest.mark.parametrize("value", [None, 0, False, {"status": "OBSERVED"}])
def test_untyped_returns_never_become_success_frames(value, context, refresh, monkeypatch):
    writer = MagicMock()
    monkeypatch.setattr(runner.os, "write", writer)
    assert runner.main(argv=context.arguments(), result_fd=9, ibk_result_crawler=lambda _: value) == 0
    writer.assert_not_called()  # exit0 + missing frame => protocol failure, not semantic success


def test_producer_crash_is_not_semantic_rejection(context, refresh, caplog):
    producer = MagicMock(side_effect=RuntimeError("PRIVATE_TEST_ERROR"))
    assert runner.main(argv=context.arguments(), result_fd=9, ibk_result_crawler=producer) == 1
    assert "PRIVATE_TEST_ERROR" not in caplog.text


def test_emit_failure_is_process_error(context, refresh, monkeypatch):
    monkeypatch.setattr(runner.os, "write", MagicMock(side_effect=BrokenPipeError("PRIVATE")))
    assert runner.main(argv=context.arguments(), result_fd=9,
                       ibk_result_crawler=lambda c: make_result(c)) == 1


@pytest.mark.parametrize("bank", ["ibk", "shinhan", "nh", "sc", "hana_selenium", "woori_selenium"])
def test_existing_one_argument_runner_still_uses_legacy(bank, refresh, monkeypatch):
    old = MagicMock(return_value=None)
    monkeypatch.setitem(runner.CRAWLER_MAP, bank, old)
    with pytest.raises(SystemExit) as exc:
        runner.main(argv=(bank,))
    assert exc.value.code == 0
    old.assert_called_once_with()
    refresh.assert_called_once()


# A real child executes the actual bootstrap and actual runner. Only the result
# producer and write-mode refresh are injected; no test handler path is exposed in CLI.
CHILD = r'''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("ibk_bootstrap", sys.argv[1])
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
mode = sys.argv[2]
def entry(args, fd):
    assert "app" not in sys.modules
    assert not os.get_inheritable(fd)
    if mode == "flood":
        for _ in range(512): os.write(1, b"x" * 4096)
    from app.crawlers import runner
    from app import atomic_write_refresh
    atomic_write_refresh.refresh_write_mode_cache = lambda: None
    from app.ibk_result_protocol import PAIRS, IbkResult, IbkReason, IbkStatus, IbkSource
    from datetime import datetime, timezone
    def produce(c):
        print("ordinary output", flush=True)
        if mode == "none": return None
        if mode == "crash": raise RuntimeError("PRIVATE_CHILD_ERROR")
        state = IbkStatus.OBSERVED if mode == "flood" else IbkStatus(mode)
        return IbkResult(1, c.run_id, state,
            IbkReason.NORMAL if state is IbkStatus.OBSERVED else
            IbkReason.OFFICIAL_NO_SESSION if state is IbkStatus.PRESERVED else IbkReason.DB_ERROR,
            datetime.now(timezone.utc).isoformat(), c.expected_service_date,
            c.expected_service_date if state is IbkStatus.OBSERVED else None,
            c.reference_time.isoformat() if state is IbkStatus.OBSERVED else None,
            IbkSource.OFFICIAL_POST if state is IbkStatus.OBSERVED else IbkSource.DB_SNAPSHOT,
            tuple(sorted(PAIRS)) if state is IbkStatus.OBSERVED else (),
            tuple(sorted(PAIRS)), (), 0, True)
    return runner.main(argv=args, result_fd=fd, ibk_result_crawler=produce)
sys.exit(bootstrap.main(sys.argv[3:], entrypoint=entry))
'''


@pytest.mark.skipif(os.name != "posix", reason="POSIX capture")
@pytest.mark.parametrize("mode", [s.value for s in IbkStatus] + ["none", "crash", "flood"])
def test_real_bootstrap_runner_to_parent_and_queue(mode, monkeypatch):
    async def scenario():
        captures = []
        notices = []
        async def actual_child(argv, **kwargs):
            result = await capture_ibk_subprocess(
                (sys.executable, "-u", "-c", CHILD, str(BOOTSTRAP), mode, *argv[3:]), **kwargs)
            captures.append(result)
            return result
        parent = IbkParentRunner(capture=actual_child, event_sink=lambda n: notices.append(n) or True)
        class RecordingQueue(asyncio.PriorityQueue):
            def __init__(self):
                super().__init__()
                self.put_items = []
            async def put(self, item):
                self.put_items.append(item)
                await super().put(item)
        queue = RecordingQueue()
        monkeypatch.setattr(scheduler, "selenium_queue", queue)
        legacy = MagicMock(side_effect=AssertionError("legacy path must not be called"))
        monkeypatch.setattr(scheduler, "execute_with_timeout", legacy)
        decisions = []
        real_execute = parent.execute
        async def record_decision(**kwargs):
            decision = await real_execute(**kwargs)
            decisions.append(decision)
            return decision
        parent.execute = record_decision
        await queue.put((7, 0, "ibk", False))
        worker = asyncio.create_task(scheduler.selenium_job_executor(ibk_parent=parent))
        try:
            await asyncio.wait_for(queue.join(), 15)
        finally:
            worker.cancel()
            await worker
        decision = decisions[0]
        legacy.assert_not_called()
        assert len(captures) == (2 if mode == "crash" else 1)
        assert sum(parent.snapshot()["counts"].values()) == len(captures)
        if mode == "crash":
            assert decision.failure is IbkExecutionFailure.PROCESS_ERROR
            assert len(queue.put_items) == 2
            priority, _, bank, retry = queue.put_items[1]
            assert (priority, bank, retry) == (1007, "ibk", True)
            assert not decisions[1].should_retry and queue.empty()
            assert len(captures) == 2
            assert b"PRIVATE_CHILD_ERROR" not in captures[0].stderr
        elif mode == "none":
            assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
            assert not decision.should_retry and queue.empty() and notices
        else:
            assert decision.result.status.value == ("OBSERVED" if mode == "flood" else mode)
            assert queue.empty()
            assert b"ordinary output" not in captures[0].stdout
            assert bool(parent.last_current_official_at) == (mode in ("OBSERVED", "flood"))
            assert bool(notices) == (mode in ("DEGRADED", "FAILED"))
            if mode == "flood":
                assert captures[0].stderr_truncated and not captures[0].stdout_truncated
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX capture")
def test_actual_default_bootstrap_is_unbound_and_does_not_crawl(context):
    result = asyncio.run(capture_ibk_subprocess(
        (sys.executable, "-u", str(BOOTSTRAP), *context.arguments()), timeout=5))
    assert result.returncode == 2 and result.stdout == b""
    assert b"IBK_RESULT_ADAPTER_UNAVAILABLE" in result.stderr


@pytest.mark.parametrize("change,code", [
    ({"run_id": "wrong"}, "RUN_ID_MISMATCH"),
    ({"expected_service_date": "2026-01-01", "observed_service_date": "2026-01-01"}, "SERVICE_DATE_MISMATCH"),
    ({"observed_at": "2020-01-01T00:00:00Z"}, "OBSERVATION_TIME_MISMATCH"),
    ({"observed_at": "2099-01-01T00:00:00Z"}, "OBSERVATION_TIME_MISMATCH"),
])
def test_parent_validates_own_context_not_just_child_consistency(change, code):
    async def capture(argv, **kwargs):
        c = IbkRunContext.from_arguments(argv[3:])
        data = encode_ibk_result(replace(make_result(c), **change))
        return IbkProcessCapture(0, data, b"", len(data), 0, False, False, None)
    parent = IbkParentRunner(capture=capture, event_sink=lambda _: True)
    decision = asyncio.run(parent.execute())
    assert decision.protocol_error == code and not decision.should_retry
    assert parent.last_current_official_at is None


@pytest.mark.parametrize("offset,allowed", [(-2, True), (-2.001, False), (2, True), (2.001, False)])
def test_observation_clock_tolerance_boundary(context, offset, allowed):
    result = replace(make_result(context), observed_at=(context.reference_time + timedelta(seconds=offset)).isoformat())
    if allowed:
        context.validate_result(result, received_at=context.reference_time)
    else:
        with pytest.raises(IbkProtocolError, match="OBSERVATION_TIME_MISMATCH"):
            context.validate_result(result, received_at=context.reference_time)


@pytest.mark.parametrize("mode,code", [("duplicate", "RESULT_COUNT"), ("missing", "RESULT_COUNT"),
    ("incomplete", "INCOMPLETE_FRAME"), ("truncated", "OUTPUT_LIMIT"),
    ("bool_returncode", "INVALID_PROCESS_METADATA")])
def test_wire_errors_are_recorded_once_and_not_retried(mode, code):
    async def capture(argv, **kwargs):
        captured = capture_result(IbkRunContext.from_arguments(argv[3:]))
        if mode == "duplicate": return replace(captured, stdout=captured.stdout * 2, stdout_bytes=len(captured.stdout)*2)
        if mode == "missing": return replace(captured, stdout=b"", stdout_bytes=0)
        if mode == "incomplete": return replace(captured, stdout=captured.stdout[:-1], stdout_bytes=len(captured.stdout)-1)
        if mode == "truncated": return replace(captured, stdout_bytes=len(captured.stdout)+1)
        return replace(captured, returncode=False)
    parent = IbkParentRunner(capture=capture, event_sink=lambda _: True)
    decision = asyncio.run(parent.execute())
    assert decision.protocol_error == code and not decision.should_retry
    assert parent.counts == {"RESULT_PROTOCOL_ERROR": 1}
    assert parent.notice_enqueued == 1 and parent.last_current_official_at is None


@pytest.mark.parametrize("changes,blocked,retry", [
    ({"cleanup_incomplete": True}, True, False),
    ({"error": "KILL_ERROR"}, True, False),
    ({"error": "PIPE_ERROR"}, False, True),
    ({"error": "SPAWN_ERROR"}, False, True),
    ({"returncode": None}, True, False),
    ({"timed_out": True}, False, True),
])
def test_transport_problem_overrides_success_and_controls_followup(changes, blocked, retry):
    async def scenario():
        calls = []
        async def capture(argv, **kwargs):
            calls.append(argv)
            return capture_result(IbkRunContext.from_arguments(argv[3:]), **changes)
        parent = IbkParentRunner(capture=capture, event_sink=lambda _: True)
        first = await parent.execute()
        assert first.result is None and first.should_retry is retry
        assert parent.cleanup_blocked is blocked and parent.last_current_official_at is None
        await parent.execute(is_retry=True)
        assert len(calls) == (1 if blocked else 2)
    asyncio.run(scenario())


def test_state_counts_freshness_and_notice_recovery_are_separate():
    async def scenario():
        states = iter([IbkStatus.OBSERVED, IbkStatus.PRESERVED, IbkStatus.DEGRADED,
                       IbkStatus.DEGRADED, IbkStatus.FAILED, IbkStatus.OBSERVED])
        notices = []
        async def capture(argv, **kwargs):
            return capture_result(IbkRunContext.from_arguments(argv[3:]), next(states))
        parent = IbkParentRunner(capture=capture, event_sink=lambda n: notices.append(n) or True)
        await parent.execute()
        first_fresh = parent.last_current_official_at
        for _ in range(4):
            await parent.execute()
            assert parent.last_current_official_at == first_fresh
        await parent.execute()
        assert parent.counts == {"OBSERVED": 2, "PRESERVED": 1, "DEGRADED": 2, "FAILED": 1}
        assert [n.kind for n in notices] == ["attention", "attention", "recovered"]
        assert parent.notice_suppressed == 1
        copy = parent.snapshot()
        copy["counts"].clear()
        assert sum(parent.counts.values()) == 6
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, "raises"])
def test_notice_enqueue_failure_does_not_retry_crawler(failure):
    async def scenario():
        async def capture(argv, **kwargs):
            return capture_result(IbkRunContext.from_arguments(argv[3:]), IbkStatus.DEGRADED)
        def sink(notice):
            if failure == "raises": raise RuntimeError("PRIVATE_NOTICE_ERROR")
            return False
        parent = IbkParentRunner(capture=capture, event_sink=sink)
        for _ in range(2):
            assert not (await parent.execute()).should_retry
        assert parent.notice_enqueue_failed == 2 and parent.notice_enqueued == 0
        assert "PRIVATE" not in str(parent.snapshot())
    asyncio.run(scenario())


def test_cancellation_is_propagated_and_unknown_cleanup_blocks_next_spawn():
    async def scenario():
        capture = MagicMock(side_effect=asyncio.CancelledError)
        notices = []
        parent = IbkParentRunner(capture=capture, event_sink=lambda n: notices.append(n) or True)
        with pytest.raises(asyncio.CancelledError):
            await parent.execute()
        assert parent.cancelled_count == 1 and not parent.counts
        assert parent.last_completed_at is None
        assert len(notices) == 1 and notices[0].kind == "attention"
        assert notices[0].cleanup_block.reason == "CANCELLED_WITHOUT_RECEIPT"
        decision = await parent.execute()
        assert decision.protocol_error == "CLEANUP_UNCONFIRMED"
        assert len(notices) == 1  # suppressed run is not a second transition
        capture.assert_called_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("interval", [False, None, 0, -1, float("nan"), float("inf"), "300"])
def test_blocked_reminder_interval_is_finite_positive_number(interval):
    with pytest.raises(ValueError, match="INVALID_BLOCKED_REMINDER_INTERVAL"):
        IbkParentRunner(event_sink=lambda _: True, blocked_reminder_seconds=interval)


@pytest.mark.parametrize("mode,reason", [
    ("incomplete", "CLEANUP_INCOMPLETE"), ("kill", "KILL_ERROR"),
    ("exit", "EXIT_UNCONFIRMED"), ("raises", "CAPTURE_ERROR"),
    ("invalid", "INVALID_CAPTURE"),
])
def test_cleanup_block_retains_origin_and_reminds_without_respawn(mode, reason):
    async def scenario():
        elapsed = [0.0]
        wall = [datetime(2026, 9, 7, 8, tzinfo=timezone.utc)]
        calls, notices = [], []
        async def capture(argv, **kwargs):
            calls.append(argv)
            if mode == "raises": raise RuntimeError("PRIVATE_CAPTURE_ERROR")
            if mode == "invalid": return None
            changes = {"incomplete": {"cleanup_incomplete": True},
                       "kill": {"error": "KILL_ERROR"}, "exit": {"returncode": None}}[mode]
            return capture_result(IbkRunContext.from_arguments(argv[3:]), **changes)
        parent = IbkParentRunner(capture=capture, event_sink=lambda n: notices.append(n) or True,
                                 clock=lambda: wall[0], monotonic=lambda: elapsed[0])
        assert not (await parent.execute()).should_retry
        origin = parent.snapshot()["cleanup_block"]
        assert origin == {"run_id": IbkRunContext.from_arguments(calls[0][3:]).run_id,
                          "since": wall[0].isoformat(), "reason": reason}
        # Wall-clock jump does not prematurely repeat; exact monotonic boundary does.
        wall[0] += timedelta(days=1)
        elapsed[0] = 299.999
        assert not (await parent.execute()).should_retry
        assert len(notices) == 1
        elapsed[0] = 300.0
        wall[0] -= timedelta(days=2)
        assert not (await parent.execute()).should_retry
        assert [n.kind for n in notices] == ["attention", "reminder"]
        assert notices[0].cleanup_block == notices[1].cleanup_block
        assert notices[0].run_id != notices[1].run_id
        assert parent.snapshot()["cleanup_block"] == origin
        assert len(calls) == 1 and parent.counts == {"RESULT_PROTOCOL_ERROR": 3}
        assert parent.last_current_official_at is None
        copy = parent.snapshot()
        copy["cleanup_block"].clear()
        assert parent.snapshot()["cleanup_block"] == origin
        with pytest.raises(AttributeError):
            parent.cleanup_blocked = False
        with pytest.raises(FrozenInstanceError):
            notices[0].cleanup_block.reason = "CLEARED"
        assert "PRIVATE" not in str(parent.snapshot())
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_kind", ["initial", "reminder"])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_block_notice_enqueue_does_not_reset_reminder_clock_or_retry_capture(fail_kind, raises):
    async def scenario():
        elapsed, calls, notices = [0.0], [], []
        async def capture(argv, **kwargs):
            calls.append(argv)
            return capture_result(IbkRunContext.from_arguments(argv[3:]), cleanup_incomplete=True)
        failure_index = 1 if fail_kind == "initial" else 2
        def sink(notice):
            notices.append(notice)
            if len(notices) == failure_index:
                if raises: raise RuntimeError("PRIVATE_NOTICE_ERROR")
                return False
            return True
        parent = IbkParentRunner(capture=capture, event_sink=sink, monotonic=lambda: elapsed[0])
        assert not (await parent.execute()).should_retry
        if fail_kind == "reminder":
            elapsed[0] = 300.0
            assert not (await parent.execute()).should_retry
        elapsed[0] += 1
        assert not (await parent.execute()).should_retry
        assert parent.notice_enqueue_failed == 1
        assert [n.kind for n in notices] == (["attention", "attention"] if fail_kind == "initial"
                                             else ["attention", "reminder", "reminder"])
        elapsed[0] += 299.999
        assert not (await parent.execute()).should_retry
        assert parent.notice_suppressed == 1 and len(calls) == 1
        elapsed[0] += 0.001
        assert not (await parent.execute()).should_retry
        assert notices[-1].kind == "reminder" and len(calls) == 1
        assert "PRIVATE" not in str(parent.snapshot())
    asyncio.run(scenario())


@pytest.mark.parametrize("bank", ["ibk", "nh"])
def test_legacy_failure_is_retried_exactly_once_per_queue_item(bank, monkeypatch):
    """실패한 회차는 **한 번만** 재등록된다 — 재등록된 작업은 다시 재등록되지 않는다.

    ⛔ 이 상한이 운영 문서의 근거다. shadow 활성 시 "멈춘 관측 → 부모 kill → 회차 재실행" 이
       **큐 항목당 최대 1회** 라고 CRAWLERS.md 가 적고 있는데, 그 1회를 지키는 것은
       `should_retry and not is_retry` 뿐이다(scheduler.py). 여기가 풀리면 실패가 무한
       재등록되고 문서가 거짓이 된다.

    ⛔ 판정은 **호출 횟수**다. 아래 안전장치가 폭주를 끊어 큐가 정상적으로 비므로
       `queue.join()` 시간 초과에 기대지 않는다. 초판 주석은 그렇게 적었지만 안전장치를
       넣은 뒤로는 사실이 아니어서 고쳤다 — `wait_for` 만으로는 이벤트 루프를 독점하는
       폭주를 끊지 못한다.
    """
    async def scenario():
        queue = asyncio.PriorityQueue()
        monkeypatch.setattr(scheduler, "selenium_queue", queue)
        seen = []
        enqueued = []
        counts = {"run": 0, "put": 0}
        original_put = queue.put

        async def recording_put(item):
            # ⛔ 상한이 깨진 변이에서는 무한히 재등록된다. 전량을 모으면 리스트가 폭주하고
            #    실패 메시지 포매팅이 시험을 멈춰 세운다(실측). 앞 몇 건만 남기고 세기만 한다.
            counts["put"] += 1
            if len(enqueued) < 4:
                enqueued.append(item)
            await original_put(item)

        monkeypatch.setattr(queue, "put", recording_put)

        async def always_failing(name):
            counts["run"] += 1
            if len(seen) < 4:
                seen.append(name)
            # ⛔ 상한이 깨진 변이에서는 worker 가 초당 수만 회 돌며 로그를 쏟아 시험을 마비시킨다
            #    (실측: 배터리가 두 번 멈췄다). 폭주를 여기서 끊어 판정이 빠르게 끝나게 한다.
            #    성공을 돌려주면 재등록이 멈추고 큐가 비어 아래 단언이 정상적으로 실패한다.
            return counts["run"] > 6

        monkeypatch.setattr(scheduler, "execute_with_timeout", always_failing)
        await original_put((5, 0, bank, False))
        worker = asyncio.create_task(scheduler.selenium_job_executor())
        try:
            await asyncio.wait_for(queue.join(), 3)
        finally:
            worker.cancel()
            await worker

        assert counts["run"] == 2, f"원래 1회 + 재시도 1회여야 한다 (실제 {counts['run']}회)"
        assert seen[:2] == [bank, bank]
        assert queue.empty(), "재시도 작업이 다시 재등록되면 큐가 비지 않는다"
        assert counts["put"] == 1, f"재등록은 정확히 1회 (실제 {counts['put']}회)"
        priority, _, name, is_retry = enqueued[0]
        assert name == bank and is_retry is True, "재등록 항목은 재시도 표시를 달고 간다"
        assert priority == 5 + 1000, "재시도는 낮은 우선순위로 들어간다"

    asyncio.run(scenario())


@pytest.mark.parametrize("bank,injected", [("ibk", False), ("nh", True)])
def test_existing_worker_default_and_other_banks_remain_legacy(bank, injected, monkeypatch):
    async def scenario():
        queue = asyncio.PriorityQueue()
        monkeypatch.setattr(scheduler, "selenium_queue", queue)
        calls = []
        async def legacy(name):
            calls.append(name)
            return True
        monkeypatch.setattr(scheduler, "execute_with_timeout", legacy)
        parent = MagicMock()
        await queue.put((1, 0, bank, False))
        worker = asyncio.create_task(scheduler.selenium_job_executor(ibk_parent=parent) if injected
                                     else scheduler.selenium_job_executor())
        try:
            await asyncio.wait_for(queue.join(), 1)
        finally:
            worker.cancel()
            await worker
        assert calls == [bank]
        parent.execute.assert_not_called()
    asyncio.run(scenario())
