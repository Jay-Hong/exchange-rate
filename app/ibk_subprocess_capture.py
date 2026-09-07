"""IBK용 유한 subprocess 캡처 준비 코드. 운영 scheduler에는 아직 연결하지 않는다.

stdout/stderr를 asyncio pipe callbacks로 동시에 소비한다. 상한은 보관할 prefix의
크기이며, 초과분도 EOF까지 소비한다. 반환 bytes/원문 stderr는 로그에 출력하지 않는다.
timeout은 subprocess_exec가 반환한 뒤부터 process exit + pipe EOF까지 적용한다.
OS 프로세스 생성 자체의 hard 시간 제한이나 별도 세션으로 이탈한 자손 정리는 보장하지 않는다.
"""

import asyncio
import math
import os
import signal
import subprocess
from dataclasses import dataclass, field

from app.ibk_result_protocol import MAX_STDOUT_BYTES

MAX_STDERR_BYTES = 64 * 1024


@dataclass(frozen=True)
class IbkProcessCapture:
    returncode: int | None
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    stdout_bytes: int
    stderr_bytes: int
    timed_out: bool
    cleanup_incomplete: bool
    error: str | None

    @property
    def stdout_truncated(self) -> bool:
        return self.stdout_bytes > len(self.stdout)

    @property
    def stderr_truncated(self) -> bool:
        return self.stderr_bytes > len(self.stderr)

    @property
    def transport_ok(self) -> bool:
        """전송/종료만 확인한다. exit 0·공식 관측·DB 저장 성공과는 별개다."""
        return (not self.timed_out and not self.cleanup_incomplete
                and self.error is None and self.returncode is not None)


class _CaptureProtocol(asyncio.SubprocessProtocol):
    def __init__(self, stdout_limit: int, stderr_limit: int):
        self.limits = {1: stdout_limit, 2: stderr_limit}
        self.buffers = {1: bytearray(), 2: bytearray()}
        self.received = {1: 0, 2: 0}
        self.transport = None
        self.returncode = None
        self.error = None
        self.aborting = False
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport):
        self.transport = transport
        if self.aborting:
            self.abort()
            # Cancellation cleanup may already have exhausted its deadline before
            # connection_made arrived; do not leave a late child's pipes open.
            transport.close()

    def pipe_data_received(self, fd, data):
        if fd not in self.buffers:
            return
        self.received[fd] += len(data)
        remaining = self.limits[fd] - len(self.buffers[fd])
        if remaining > 0:
            self.buffers[fd].extend(data[:remaining])
        # Do not pause/stop reading when full: that would reintroduce PIPE deadlock.

    def pipe_connection_lost(self, fd, exc):
        if exc is not None:
            self.error = self.error or "PIPE_ERROR"
            self.abort()

    def process_exited(self):
        self.returncode = self.transport.get_returncode()

    def connection_lost(self, exc):
        if exc is not None:
            self.error = self.error or "PIPE_ERROR"
        if self.transport is not None:
            self.returncode = self.transport.get_returncode()
        if not self.closed.done():
            self.closed.set_result(None)

    def abort(self):
        self.aborting = True
        if self.transport is None:
            return  # connection_made will kill a late-created child after cancellation.
        # Only the session created by this function: start_new_session=True => PGID=PID.
        # Also needed if the root exited but descendants still hold its stdout/stderr.
        try:
            os.killpg(self.transport.get_pid(), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            self.error = self.error or "KILL_ERROR"
            # close() below still attempts to kill the direct child.

    def snapshot(self, *, timed_out: bool, cleanup_incomplete: bool) -> IbkProcessCapture:
        return IbkProcessCapture(
            self.returncode, bytes(self.buffers[1]), bytes(self.buffers[2]),
            self.received[1], self.received[2], timed_out, cleanup_incomplete, self.error,
        )


async def _abort_and_close(protocol: _CaptureProtocol, cleanup_timeout: float) -> bool:
    """Return whether cleanup was incomplete. Never wait indefinitely after kill."""
    protocol.abort()
    try:
        await asyncio.wait_for(asyncio.shield(protocol.closed), cleanup_timeout)
        return protocol.returncode is None
    except asyncio.TimeoutError:
        return True
    finally:
        if protocol.transport is not None:
            # Public transport API closes pipes even when an escaped descendant holds them.
            protocol.transport.close()


async def _cleanup_after_cancel(protocol: _CaptureProtocol, cleanup_timeout: float) -> None:
    cleanup = asyncio.create_task(_abort_and_close(protocol, cleanup_timeout))
    # A second cancellation must not cancel the bounded cleanup itself.
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    cleanup.result()


async def capture_ibk_subprocess(
    argv: tuple[str, ...], *, timeout: float = 45, cleanup_timeout: float = 2,
    stdout_limit: int = MAX_STDOUT_BYTES, stderr_limit: int = MAX_STDERR_BYTES,
) -> IbkProcessCapture:
    """Start an owned POSIX session, drain both pipes, and reap/close with bounded waits.

    No shell, DB, semantic result parsing, counters, retry, or alert sending here.
    asyncio cancellation is propagated after cleanup, not converted to success/failure.
    error/cleanup_incomplete must block semantic acceptance even if returncode is 0.
    Limits cover retained output only, not total process RSS or OS/asyncio buffers.
    """
    if os.name != "posix":
        raise ValueError("POSIX_REQUIRED")
    if not isinstance(argv, tuple) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
        raise ValueError("INVALID_ARGV")
    for value in (timeout, cleanup_timeout):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("INVALID_TIMEOUT")
    for value in (stdout_limit, stderr_limit):
        if type(value) is not int or value < 0:
            raise ValueError("INVALID_OUTPUT_LIMIT")

    protocol = _CaptureProtocol(stdout_limit, stderr_limit)
    timed_out = False
    cleanup_incomplete = False
    try:
        try:
            await asyncio.get_running_loop().subprocess_exec(
                lambda: protocol, *argv, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError:
            # Fixed code only: spawn exceptions may include command/path/secrets.
            protocol.error = "SPAWN_ERROR"
            if protocol.transport is not None:
                cleanup_incomplete = await _abort_and_close(protocol, cleanup_timeout)
            return protocol.snapshot(timed_out=False, cleanup_incomplete=cleanup_incomplete)

        try:
            # connection_lost follows both process exit and pipe closure, not exit alone.
            await asyncio.wait_for(asyncio.shield(protocol.closed), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            cleanup_incomplete = await _abort_and_close(protocol, cleanup_timeout)
        return protocol.snapshot(timed_out=timed_out, cleanup_incomplete=cleanup_incomplete)
    except asyncio.CancelledError:
        await _cleanup_after_cancel(protocol, cleanup_timeout)
        raise
    finally:
        if protocol.transport is not None:
            protocol.transport.close()
