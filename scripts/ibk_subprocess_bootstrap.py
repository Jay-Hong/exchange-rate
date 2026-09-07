#!/usr/bin/env python3
"""IBK 결과 전용 stdout. app import보다 먼저 fd를 분리한다.

No app imports at module scope. Launched by absolute script path, not `-m app...`.
Only the private non-inheritable descriptor writes to the parent's stdout pipe.
Ordinary fd 1 output goes to stderr, including import-time/native/subprocess output.
Launch only through IbkParentRunner (or a test of the same result protocol), NEVER
through an exit-code-only collector. Exit 0 is not a semantic success assertion.
"""

import os
from pathlib import Path
import sys


def _runner_entry(args, result_fd):
    # Lazy import is required: app.__init__ binds its logging stream on import.
    from app.crawlers.runner import main
    return main(argv=args, result_fd=result_fd)


def main(argv=None, *, entrypoint=None) -> int:
    """entrypoint is a local test seam, never accepted as a CLI import/path argument."""
    result_fd = None
    try:
        sys.stdout.flush()
        result_fd = os.dup(1)
        os.set_inheritable(result_fd, False)
        os.dup2(2, 1)
        # scripts/ is copied by the existing Dockerfile; don't depend on cwd/PYTHONPATH.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        args = tuple(sys.argv[1:] if argv is None else argv)
        code = (entrypoint or _runner_entry)(args, result_fd)
        return code if type(code) is int and code in (0, 1, 2) else 1
    except Exception:
        # Do not expose exception text, argv, DB URLs or log excerpts.
        os.write(2, b"IBK_BOOTSTRAP_ERROR\n")
        return 1
    finally:
        if result_fd is not None:
            os.close(result_fd)


if __name__ == "__main__":
    sys.exit(main())
