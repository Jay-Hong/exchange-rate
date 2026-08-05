"""Serialize host-side operations that rewrite a Compose env file.

The lock file is persistent by design.  Ownership lives in the kernel flock,
not in file existence, so a crash releases the lock without leaving a stale
transaction marker.  Never unlink the file: unlinking allows two processes to
lock different inodes under the same pathname.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


LOCK_SUFFIX = ".operation-lock"
TOPIC_FLAG_PENDING_SUFFIX = ".topic-flag-pending"


class EnvOperationLocked(RuntimeError):
    """Another process currently owns the env mutation lock."""


def operation_lock_path(env_path: Path) -> Path:
    """Return one stable lock pathname for aliases of the same env file."""
    canonical = Path(os.path.abspath(os.fspath(env_path))).resolve(strict=False)
    return canonical.with_name(canonical.name + LOCK_SUFFIX)


def topic_flag_pending_path(env_path: Path) -> Path:
    canonical = Path(os.path.abspath(os.fspath(env_path))).resolve(strict=False)
    return canonical.with_name(canonical.name + TOPIC_FLAG_PENDING_SUFFIX)


def topic_flag_pending_entry_exists(env_path: Path) -> bool:
    """Treat every directory entry, including a dangling symlink, as pending."""
    try:
        topic_flag_pending_path(env_path).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise EnvOperationLocked(
            f"topic activation marker를 확인할 수 없다 ({type(exc).__name__})"
        ) from exc


def _open_lock(path: Path, *, create: bool = True) -> int:
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise EnvOperationLocked(
            f"operation lock을 열 수 없다: {path.name} ({type(exc).__name__})"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EnvOperationLocked(f"operation lock이 일반 파일이 아니다: {path.name}")
        if info.st_uid != os.geteuid():
            raise EnvOperationLocked(f"operation lock 소유자가 현재 사용자와 다르다: {path.name}")
        if create:
            os.fchmod(fd, 0o600)
        elif info.st_mode & 0o077:
            raise EnvOperationLocked(f"operation lock 권한이 owner-only가 아니다: {path.name}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _owner_description(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 256).decode("ascii", "replace").strip()
    except OSError:
        return "owner=unknown"
    return raw or "owner=unknown"


def env_operation_is_locked(env_path: Path) -> bool:
    """Probe lock ownership without changing the diagnostic owner record."""
    path = operation_lock_path(Path(env_path))
    try:
        fd = _open_lock(path, create=False)
    except EnvOperationLocked as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return False
        raise
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True
            raise EnvOperationLocked(
                f"operation lock 상태를 확인할 수 없다: {path.name} ({type(exc).__name__})"
            ) from exc
        return False
    finally:
        # Closing the descriptor releases a successfully acquired probe lock.
        os.close(fd)


@contextmanager
def env_operation_lock(env_path: Path, operation: str) -> Iterator[Path]:
    """Acquire an exclusive, non-blocking lock for an env mutation.

    The operation label is diagnostic only and is restricted to printable
    ASCII so callers cannot inject secrets or multiline output into the lock.
    """
    label = "".join(ch for ch in str(operation) if 32 <= ord(ch) < 127)[:80] or "unknown"
    path = operation_lock_path(Path(env_path))
    fd = _open_lock(path)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise EnvOperationLocked(
                    f"operation lock 획득 실패: {path.name} ({type(exc).__name__})"
                ) from exc
            owner = _owner_description(fd)
            raise EnvOperationLocked(
                f"다른 env 작업이 진행 중이다: {path.name} ({owner})"
            ) from exc

        record = f"pid={os.getpid()} operation={label}\n".encode("ascii")
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, record)
            os.fsync(fd)
        except OSError as exc:
            raise EnvOperationLocked(
                f"operation lock 메타데이터 기록 실패: {path.name} ({type(exc).__name__})"
            ) from exc
        yield path
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
