"""Actual local Python subprocesses only; no crawler, network, DB, Chrome, or Telegram.

These tests exercise transport, not the live scheduler integration. POSIX sessions are
created solely for each test; cancellation/timeout must reap their direct children.
"""

import asyncio
import json
import os
import signal
import sys
import time
from unittest.mock import MagicMock

import pytest

from app.ibk_result_protocol import PAIRS, SENTINEL, IbkExecutionFailure, IbkStatus, decide_ibk_process_result
from app.ibk_subprocess_capture import _CaptureProtocol, capture_ibk_subprocess

pytestmark = pytest.mark.skipif(os.name != "posix", reason="owned POSIX process groups")


def command(code):
    return (sys.executable, "-u", "-c", code)


def run(code, **kwargs):
    return asyncio.run(capture_ibk_subprocess(command(code), **kwargs))


def assert_gone(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.fixture
def result_frame():
    payload = dict(
        schema_version=1, run_id="transport_test", status="OBSERVED", reason="NORMAL",
        observed_at="2026-09-07T08:30:36+09:00", expected_service_date="2026-09-07",
        observed_service_date="2026-09-07", official_completed_at="2026-09-07T08:30:00+09:00",
        source="official_post", observed_pairs=sorted(PAIRS), preserved_pairs=sorted(PAIRS),
        missing_pairs=[], changed_count=0, db_snapshot_complete=True,
    )
    return SENTINEL + json.dumps(payload).encode() + b"\n"


def decode_capture(capture):
    # This is a test consumer, not the production scheduler adapter. Error and
    # incomplete cleanup need explicit mapping in the future runtime integration.
    assert capture.error is None and not capture.cleanup_incomplete
    return decide_ibk_process_result(
        capture.stdout, expected_run_id="transport_test", returncode=capture.returncode,
        timed_out=capture.timed_out, stdout_truncated=capture.stdout_truncated,
    )


def test_real_child_frame_reaches_existing_result_validator(result_frame):
    result = run(f"import os; os.write(1, b'ordinary log\\n'); os.write(1, {result_frame!r})")
    decision = decode_capture(result)
    assert decision.result.status is IbkStatus.OBSERVED
    assert not decision.should_retry and not decision.needs_attention


def test_valid_frame_then_stdout_flood_is_not_false_success(result_frame):
    result = run(f"""
import os
os.write(1, {result_frame!r})
for _ in range(512):
    os.write(1, b'x' * 4096)
""", stdout_limit=len(result_frame) + 1024, timeout=3)
    assert result.returncode == 0 and result.transport_ok and result.stdout_truncated
    decision = decode_capture(result)
    assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    assert decision.protocol_error == "OUTPUT_LIMIT"
    assert not decision.should_retry


def test_stdout_flood_then_tail_frame_is_output_limit_not_missing_result(result_frame):
    # A result emitted at the end can lie entirely outside the retained prefix.
    # Pass truncation through: lack of a retained frame is not RESULT_COUNT here.
    result = run(f"""
import os
for _ in range(512):
    os.write(1, b'x' * 4096)
os.write(1, b'\\n' + {result_frame!r})
""", stdout_limit=1024, timeout=3)
    assert result.returncode == 0 and result.transport_ok and result.stdout_truncated
    assert result.stdout == b'x' * 1024
    decision = decode_capture(result)
    assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    assert decision.protocol_error == "OUTPUT_LIMIT"
    assert not decision.should_retry


def test_result_only_stdout_bootstrap_survives_ordinary_output_flood(result_frame):
    # Design probe only, not the real runner: reserve its result writer BEFORE
    # application imports and send ordinary fd 1 output (not just logging) to fd 2.
    # No third parent pipe or on-disk result file is introduced by this approach.
    result = run(f"""
import os, sys
result_fd = os.dup(1)
os.set_inheritable(result_fd, False)
os.dup2(2, 1)
import logging, subprocess
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logging.info('ordinary logging')
print('ordinary print', flush=True)
for _ in range(512):
    os.write(1, b'x' * 4096)
subprocess.run([sys.executable, '-c', "import os; os.write(1,b'nested output')"], check=True)
frame = {result_frame!r}
try:
    while frame:
        frame = frame[os.write(result_fd, frame):]
finally:
    os.close(result_fd)
""", stderr_limit=1024, timeout=3)
    assert result.returncode == 0 and result.transport_ok
    assert result.stdout == result_frame and not result.stdout_truncated
    assert result.stderr_truncated and result.stderr_bytes > 2 * 1024 * 1024
    assert decode_capture(result).result.status is IbkStatus.OBSERVED


def test_stderr_flood_does_not_lose_stdout_frame(result_frame):
    result = run(f"""
import os
for _ in range(512):
    os.write(2, b'e' * 4096)
os.write(1, {result_frame!r})
""", stderr_limit=1024, timeout=3)
    assert result.returncode == 0 and result.transport_ok
    assert result.stderr_truncated and not result.stdout_truncated
    assert decode_capture(result).result.status is IbkStatus.OBSERVED


def test_frame_does_not_override_timeout(result_frame):
    result = run(f"import os,time; os.write(1, {result_frame!r}); time.sleep(60)",
                 timeout=0.2, cleanup_timeout=1)
    assert result.stdout == result_frame
    decision = decode_capture(result)
    assert decision.result is None and decision.failure is IbkExecutionFailure.PROCESS_TIMEOUT
    assert decision.should_retry


def test_small_stdout_stderr_and_exit_code():
    result = run("import os; os.write(1, b'out'); os.write(2, b'err')")
    assert result.stdout == b"out" and result.stderr == b"err"
    assert result.stdout_bytes == result.stderr_bytes == 3
    assert not result.stdout_truncated and not result.stderr_truncated
    assert result.returncode == 0 and result.transport_ok
    assert not result.timed_out and not result.cleanup_incomplete


@pytest.mark.parametrize("code", [1, 2, 7])
def test_nonzero_is_reaped_but_not_promoted_to_success(code):
    result = run(f"import sys; print('partial'); sys.exit({code})")
    assert result.returncode == code
    assert result.stdout == b"partial\n"
    assert result.transport_ok  # transport completion != crawler success


@pytest.mark.parametrize("fd", [1, 2])
@pytest.mark.parametrize("size", [0, 1023, 1024, 1025, 2 * 1024 * 1024])
def test_output_limit_boundaries_with_real_pipe(fd, size):
    # Loop over os.write's return value: a short write is not lost by the test producer.
    code = f"""
import os
data = b'x' * {size}
while data:
    data = data[os.write({fd}, data):]
"""
    result = run(code, stdout_limit=1024, stderr_limit=1024, timeout=3)
    prefix = result.stdout if fd == 1 else result.stderr
    count = result.stdout_bytes if fd == 1 else result.stderr_bytes
    truncated = result.stdout_truncated if fd == 1 else result.stderr_truncated
    assert result.returncode == 0 and result.transport_ok
    assert prefix == b"x" * min(size, 1024)
    assert count == size
    assert truncated is (size > 1024)


def test_both_streams_flood_concurrently_without_wait_deadlock():
    result = run("""
import os, threading
def write(fd, byte):
    for _ in range(512):
        data = byte * 4096
        while data:
            data = data[os.write(fd, data):]
a = threading.Thread(target=write, args=(1, b'o'))
b = threading.Thread(target=write, args=(2, b'e'))
a.start(); b.start(); a.join(); b.join()
""", stdout_limit=8192, stderr_limit=4096, timeout=5)
    assert result.returncode == 0 and result.transport_ok
    assert result.stdout == b"o" * 8192 and result.stderr == b"e" * 4096
    assert result.stdout_bytes == result.stderr_bytes == 2 * 1024 * 1024
    assert result.stdout_truncated and result.stderr_truncated


def test_zero_retention_still_drains_both_streams():
    result = run("import os; os.write(1, b'x' * 200000); os.write(2, b'y' * 200000)",
                 stdout_limit=0, stderr_limit=0, timeout=3)
    assert result.returncode == 0 and result.transport_ok
    assert result.stdout == result.stderr == b""
    assert result.stdout_bytes == result.stderr_bytes == 200000
    assert result.stdout_truncated and result.stderr_truncated


def test_timeout_keeps_partial_output_and_reaps_child():
    before = time.monotonic()
    result = run("import os,time; print(os.getpid(), flush=True); time.sleep(60)",
                 timeout=0.2, cleanup_timeout=1)
    assert time.monotonic() - before < 3
    assert result.timed_out and not result.cleanup_incomplete
    assert result.returncode == -signal.SIGKILL
    assert not result.transport_ok
    assert_gone(int(result.stdout.strip()))


def test_unending_output_is_time_bounded_and_retention_bounded():
    result = run("import os\nwhile True: os.write(1, b'x' * 4096); os.write(2, b'y' * 4096)",
                 stdout_limit=512, stderr_limit=256, timeout=0.2, cleanup_timeout=1)
    assert result.timed_out and result.returncode == -signal.SIGKILL
    assert result.stdout == b"x" * 512 and result.stderr == b"y" * 256
    assert result.stdout_truncated and result.stderr_truncated
    assert not result.cleanup_incomplete


def test_root_exit_does_not_hide_descendant_holding_pipe():
    result = run("""
import os, time
pid = os.fork()
if pid == 0:
    time.sleep(3)
    os._exit(0)
print(os.getpid(), flush=True)
os._exit(0)
""", timeout=0.2, cleanup_timeout=1)
    assert result.returncode == 0  # root exited before the pipe-holding descendant
    assert result.timed_out and not result.transport_ok
    assert not result.cleanup_incomplete
    assert_gone(int(result.stdout.strip()))


def test_escaped_pipe_holder_reports_incomplete_cleanup_instead_of_hanging():
    result = run("""
import os, time
pid = os.fork()
if pid == 0:
    os.setsid()
    print('ESCAPED:' + str(os.getpid()), flush=True)
    time.sleep(1)
    os._exit(0)
os._exit(0)
""", timeout=0.15, cleanup_timeout=0.05)
    # This test intentionally creates an escaped session, outside the helper's
    # kill group. Clean up only its explicitly reported test PID, not a broad match.
    escaped_pid = int(result.stdout.decode().strip().removeprefix("ESCAPED:"))
    try:
        assert result.returncode == 0 and result.timed_out
        assert result.cleanup_incomplete and not result.transport_ok
    finally:
        try:
            os.kill(escaped_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_cancel_after_spawn_reaps_child_and_propagates(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        real_spawn = loop.subprocess_exec
        ready = asyncio.Event()
        holder = {}
        async def spawn(*args, **kwargs):
            transport, protocol = await real_spawn(*args, **kwargs)
            holder["pid"] = transport.get_pid()
            ready.set()
            return transport, protocol
        monkeypatch.setattr(loop, "subprocess_exec", spawn)
        task = asyncio.create_task(capture_ibk_subprocess(
            command("import time; time.sleep(60)"), cleanup_timeout=1))
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert_gone(holder["pid"])
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


def test_cancel_during_spawn_return_does_not_lose_owned_process(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        real_spawn = loop.subprocess_exec
        ready = asyncio.Event()
        holder = {}
        async def delayed_return(*args, **kwargs):
            transport, protocol = await real_spawn(*args, **kwargs)
            holder["pid"] = transport.get_pid()
            ready.set()
            await asyncio.Event().wait()
            return transport, protocol
        monkeypatch.setattr(loop, "subprocess_exec", delayed_return)
        task = asyncio.create_task(capture_ibk_subprocess(command("import time; time.sleep(60)")))
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert_gone(holder["pid"])
    asyncio.run(scenario())


def test_cleanup_wait_is_bounded_if_completion_callback_never_arrives(monkeypatch):
    async def scenario():
        # Fault injection, not an assertion that a real kernel refused SIGKILL.
        protocol = None
        transport = MagicMock()
        def made(p):
            nonlocal protocol
            protocol = p
            p.connection_made(transport)
        async def spawn(factory, *args, **kwargs):
            made(factory())
            return transport, protocol
        monkeypatch.setattr(asyncio.get_running_loop(), "subprocess_exec", spawn)
        monkeypatch.setattr(os, "killpg", MagicMock())
        before = time.monotonic()
        result = await capture_ibk_subprocess(("unused",), timeout=0.02, cleanup_timeout=0.02)
        assert time.monotonic() - before < 0.5
        assert result.timed_out and result.cleanup_incomplete
        assert not result.transport_ok
        transport.close.assert_called()
    asyncio.run(scenario())


def test_late_connection_after_abort_is_killed_and_closed(monkeypatch):
    async def scenario():
        protocol = _CaptureProtocol(0, 0)
        protocol.abort()  # no transport existed when cleanup was requested
        transport = MagicMock()
        kill = MagicMock()
        monkeypatch.setattr(os, "killpg", kill)
        protocol.connection_made(transport)
        kill.assert_called_once_with(transport.get_pid(), signal.SIGKILL)
        transport.close.assert_called_once()
    asyncio.run(scenario())


def test_repeated_cancellation_does_not_cancel_cleanup(monkeypatch):
    async def scenario():
        ready = asyncio.Event()
        transport = MagicMock()
        protocol = None
        async def spawn(factory, *args, **kwargs):
            nonlocal protocol
            protocol = factory()
            protocol.connection_made(transport)
            ready.set()
            return transport, protocol
        monkeypatch.setattr(asyncio.get_running_loop(), "subprocess_exec", spawn)
        killed = MagicMock()
        monkeypatch.setattr(os, "killpg", killed)
        task = asyncio.create_task(capture_ibk_subprocess(("unused",), cleanup_timeout=0.05))
        await ready.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        killed.assert_called()
        transport.close.assert_called()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())


def test_spawn_failure_does_not_disclose_command_or_exception():
    result = asyncio.run(capture_ibk_subprocess(("/nonexistent/PRIVATE_TEST_VALUE",)))
    assert result.error == "SPAWN_ERROR" and result.returncode is None
    assert not result.transport_ok and not result.cleanup_incomplete
    assert "PRIVATE_TEST_VALUE" not in repr(result)


@pytest.mark.parametrize("argv", [(), [], "python", ("",), (None,), (1,)])
def test_invalid_argv_is_rejected_before_launch(argv, monkeypatch):
    async def scenario():
        spawn = MagicMock(side_effect=AssertionError("must not spawn"))
        monkeypatch.setattr(asyncio.get_running_loop(), "subprocess_exec", spawn)
        with pytest.raises(ValueError, match="INVALID_ARGV"):
            await capture_ibk_subprocess(argv)
        spawn.assert_not_called()
    asyncio.run(scenario())


def test_output_is_excluded_from_repr():
    result = run("import sys; print('PRIVATE_TEST_VALUE'); print('PRIVATE_TEST_VALUE', file=sys.stderr)")
    assert b"PRIVATE_TEST_VALUE" in result.stdout and b"PRIVATE_TEST_VALUE" in result.stderr
    assert "PRIVATE_TEST_VALUE" not in repr(result)


@pytest.mark.parametrize("kwargs", [
    {"timeout": 0}, {"timeout": -1}, {"timeout": float('inf')}, {"timeout": float('nan')},
    {"timeout": True}, {"cleanup_timeout": 0}, {"cleanup_timeout": float('inf')},
    {"stdout_limit": -1}, {"stdout_limit": True}, {"stderr_limit": 1.5},
])
def test_invalid_options_rejected_before_spawn(kwargs, monkeypatch):
    async def scenario():
        spawn = MagicMock(side_effect=AssertionError("must not spawn"))
        monkeypatch.setattr(asyncio.get_running_loop(), "subprocess_exec", spawn)
        with pytest.raises(ValueError):
            await capture_ibk_subprocess(command("pass"), **kwargs)
        spawn.assert_not_called()
    asyncio.run(scenario())


def test_pipe_error_is_fixed_code_and_never_healthy(monkeypatch):
    async def scenario():
        transport = MagicMock()
        transport.get_returncode.return_value = 0
        async def spawn(factory, *args, **kwargs):
            protocol = factory()
            protocol.connection_made(transport)
            protocol.pipe_connection_lost(1, OSError("PRIVATE_TEST_VALUE"))
            protocol.process_exited()
            protocol.connection_lost(None)
            return transport, protocol
        monkeypatch.setattr(asyncio.get_running_loop(), "subprocess_exec", spawn)
        monkeypatch.setattr(os, "killpg", MagicMock())
        result = await capture_ibk_subprocess(("unused",))
        assert result.returncode == 0 and result.error == "PIPE_ERROR"
        assert not result.transport_ok
        assert "PRIVATE_TEST_VALUE" not in repr(result)
    asyncio.run(scenario())
