"""Process-local logging/import boundary; no crawler routine is invoked here."""

import importlib
import io
import logging
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from types import SimpleNamespace


class _Discard(io.TextIOBase):
    def write(self, value):
        return len(value)

    def flush(self):
        pass


@contextmanager
def quiet_logging():
    """Disable all logging, including named handlers and lastResort.

    app.config still creates logs/ and app.logging opens its two files before
    they can be detached. No response exists at that point. New handlers are
    closed; caller-owned handlers are restored only after the capture ends.
    This boundary and the bs4 recorder are for a single-threaded CLI process.
    """
    root = logging.getLogger()
    previous = list(root.handlers)
    level, disabled = root.level, root.manager.disable
    logging.disable(sys.maxsize)
    root.handlers.clear()
    sink = _Discard()
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield
    finally:
        detach_root_handlers(previous)
        root.handlers[:] = previous
        root.setLevel(level)
        logging.disable(disabled)


def detach_root_handlers(keep=()):
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in keep:
            handler.close()


def load_sources():
    # Lazy imports are necessary: app initializes file logging on first import.
    with quiet_logging():
        importlib.import_module("app")
        # quiet_logging blocks emission; this separately bounds handler lifetime
        # to app initialization instead of keeping files open across imports.
        detach_root_handlers()  # Immediately after app, before any crawler import.
        modules = {name: importlib.import_module(f"app.crawlers.{name}")
                   for name in ("bs", "citi", "utils", "bank_report", "constants")}
        return SimpleNamespace(**modules)
