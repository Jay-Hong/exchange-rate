"""Wall-clock budgets, including blocking I/O, on the CLI's POSIX main thread."""

import signal
import threading
import time
from contextlib import contextmanager

from .errors import CaptureError, DeadlineExpired

TOTAL_SECONDS = 30
PARSE_SECONDS = 10
BODY_LIMIT = 2 * 1024 * 1024
HTML_LIMIT = 256 * 1024
METADATA_LIMIT = 64 * 1024


class Deadline:
    def __init__(self, seconds=TOTAL_SECONDS, clock=time.monotonic):
        self.clock = clock
        self.expires = clock() + seconds

    def remaining(self):
        remaining = self.expires - self.clock()
        if remaining <= 0:
            raise DeadlineExpired("total_timeout")
        return remaining


@contextmanager
def wall_timeout(seconds, rule):
    """Nested timers preserve the earlier deadline and its diagnostic.

    A timeout is BaseException so reporting helpers cannot swallow it. The public
    capture boundary translates it to a safe CaptureError without a traceback.
    No worker thread or request is left running after a timeout.
    """
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise CaptureError("posix_main_thread_required")
    if seconds <= 0:
        raise DeadlineExpired(rule)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(signum, frame):
        raise DeadlineExpired(rule)

    earlier = previous_delay > 0 and previous_delay <= seconds
    if not earlier:
        signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, previous_delay if earlier else seconds)
    try:
        yield
        if time.monotonic() - started >= seconds:
            raise DeadlineExpired(rule)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        remaining = previous_delay - (time.monotonic() - started)
        if previous_delay and remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_interval)
