#!/usr/bin/env python3
"""Docker stdout/stderr retention, B v1. Python 3.12+, standard library only.

The contract tests pin the public API. Implementation tests additionally pin
gzip footer -> file fsync -> final name/directory fsync -> manifest -> cursor,
including regular-file versus directory descriptors, and both raw hashes.
No exception text or Docker output is copied into diagnostics or metadata.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
import uuid
import zlib


UTC = timezone.utc
POLICY = {"policy": "fxi-docker-log-archive", "version": 1, "retention_days": 14}
MARKER = ".archive-policy.json"
EXPIRY_STATE = "expiry-state.json"
STREAMS = ("stdout", "stderr")
TIMEOUT = 120
MAX_CAPTURE_BYTES = 256 * 1024 * 1024  # both raw streams together
# Raw + worst-case gzip + metadata/block allocation headroom, before collection.
PEAK_RESERVE_BYTES = 3 * MAX_CAPTURE_BYTES + 16 * 1024 * 1024
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
CID = re.compile(r"[0-9a-f]{64}\Z")
STAMP = re.compile(rb"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)\Z")


class ArchiveError(Exception):
    """A safe, content-free diagnostic."""


class _Busy(ArchiveError):
    pass


class _ExpiryFailed(ArchiveError):
    def __init__(self, report):
        super().__init__("expiry failed")
        self.report = report


def _now(value=None):
    value = value if value is not None else datetime.now(UTC)
    if value.tzinfo is None:
        raise ArchiveError("timezone required")
    return value.astimezone(UTC)


def _iso(value):
    return _now(value).isoformat().replace("+00:00", "Z")


def _date(value):
    try:
        return _now(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ArchiveError("invalid timestamp") from None


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")


def _atomic_json(path, value):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with _new_file(temporary) as file:
            file.write((json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n").encode())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ArchiveError("invalid metadata file")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ArchiveError("invalid metadata object")
        return value
    except (OSError, ValueError):
        raise ArchiveError("metadata unreadable") from None


def _root(path, *, mutate=False):
    # Canonicalize ancestors once (macOS /var is a symlink); never follow the leaf.
    path = Path(os.path.abspath(Path(path).expanduser()))
    if path.is_symlink():
        raise ArchiveError("symlink root refused")
    path = path.parent.resolve() / path.name
    if mutate:
        if "investing-observe" in path.parts:
            raise ArchiveError("observation archive refused")
        if path.exists():
            if not path.is_dir():
                raise ArchiveError("archive root is not a directory")
            for base, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    entry = Path(base) / name
                    if name == "investing-observe" or entry.is_symlink():
                        raise ArchiveError("unsafe archive tree")
    if path.exists() and (path / MARKER).exists():
        if _json(path / MARKER) != POLICY:
            raise ArchiveError("archive policy mismatch")
    return path


@contextmanager
def locked(root):
    """Nonblocking, process-wide advisory lock shared by collection and expiry."""
    root = _root(root, mutate=True)
    if not root.exists():
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (root / MARKER).exists():
        _fsync_dir(root.parent)
    if not (root / MARKER).exists() and any(p.name != ".lock" for p in root.iterdir()):
        raise ArchiveError("unowned archive root")
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ArchiveError("invalid lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Busy("archive busy") from None
        # Recheck after acquiring the lock; initialization is serialized too.
        _root(root, mutate=True)
        if not (root / MARKER).exists():
            _atomic_json(root / MARKER, POLICY)
        for name in ("runs", "failures", "deletions"):
            directory = root / name
            if not directory.exists():
                directory.mkdir(mode=0o700)
                _fsync_dir(root)
        yield root
    finally:
        os.close(fd)


def _require(root):
    root = _root(root)
    if not (root / MARKER).exists() or _json(root / MARKER) != POLICY:
        raise ArchiveError("unowned archive root")
    return root


def _record_paths(root, kind):
    root = _require(root)
    directory = root / kind
    if directory.is_symlink():
        raise ArchiveError("symlink metadata directory")
    for entry in directory.iterdir():
        if kind == "runs":
            if not RUN_ID.fullmatch(entry.name):
                continue
            path = entry / "manifest.json"
            if not entry.is_symlink() and not path.exists():
                continue  # unpublished/incomplete run
        else:
            if not RUN_ID.fullmatch(entry.stem) or entry.suffix != ".json":
                continue
            path = entry
        yield entry.stem, path


def _validate_record(value, kind, run_id):
    """Normalize malformed persisted fields here, not in cycle's isolation."""
    try:
        if value["policy"] != POLICY["policy"] or _valid_id(value["run_id"]) != run_id:
            raise ArchiveError("invalid record identity")
        until = _date(value["until"])
        if kind == "runs":
            since = _date(value["since"])
            if since > until or _date(value["started_at"]) > until:
                raise ArchiveError("invalid run window")
            if not isinstance(value["container_id"], str) or not CID.fullmatch(value["container_id"]):
                raise ArchiveError("invalid container id")
            for flag in ("container_changed", "restarted"):
                if type(value[flag]) is not bool:
                    raise ArchiveError("invalid run flag")
            for stream in STREAMS:
                metadata = value["streams"][stream]
                for key in ("raw_sha256", "gz_sha256"):
                    if not isinstance(metadata[key], str) or not CID.fullmatch(metadata[key]):
                        raise ArchiveError("invalid stream hash")
                for key in ("raw_bytes", "gz_bytes"):
                    if type(metadata[key]) is not int or metadata[key] < 0:
                        raise ArchiveError("invalid stream size")
                first, last = metadata["first"], metadata["last"]
                if (first is None) != (last is None):
                    raise ArchiveError("invalid stream window")
                if first is not None and not since <= _date(first) <= _date(last) <= until:
                    raise ArchiveError("invalid stream window")
            tail = value["unarchived_tail"]
            if tail is not None:
                _validate_interval(tail)
            slot = value.get("scheduled_slot")
            if slot is not None:
                parsed = _date(slot)
                if parsed.minute != 41 or parsed.second or parsed.microsecond:
                    raise ArchiveError("invalid scheduled slot")
        elif kind == "failures":
            if not isinstance(value["stage"], str) or not value["stage"]:
                raise ArchiveError("invalid failure stage")
            if "replacement" in value:
                replacement = value["replacement"]
                _valid_id(replacement["previous_run_id"])
                cid = replacement["new_container_id"]
                if not isinstance(cid, str) or not CID.fullmatch(cid):
                    raise ArchiveError("invalid replacement id")
                _validate_interval(replacement["interval"])
        elif kind == "deletions":
            _date(value["deleted_at"])
            if value["status"] not in ("pending", "deleted"):
                raise ArchiveError("invalid deletion status")
    except (KeyError, TypeError, ValueError, IndexError):
        raise ArchiveError("invalid record fields") from None
    return value


def _validate_interval(value):
    if not isinstance(value, list) or len(value) != 2 or _date(value[0]) > _date(value[1]):
        raise ArchiveError("invalid interval")


def _read_record(path, kind, run_id):
    if kind == "runs" and path.parent.is_symlink():
        raise ArchiveError("symlink run")
    return _validate_record(_json(path), kind, run_id)


def _records(root, kind):
    result = [_read_record(path, kind, run_id) for run_id, path in _record_paths(root, kind)]
    return sorted(result, key=lambda r: (_date(r["until"]), r["run_id"]))


def runs(root):
    return _records(root, "runs")


def failures(root):
    return _records(root, "failures")


def deletions(root):
    return _records(root, "deletions")


def cursor(root):
    path = _require(root) / "cursor.json"
    return _json(path) if path.exists() else None


def _checked_cursor(root):
    previous = cursor(root)
    if previous is None:
        return None
    try:
        run_id = _valid_id(previous["run_id"])
        run = _read_record(root / "runs" / run_id / "manifest.json", "runs", run_id)
        if any(previous.get(k) != run.get(k)
               for k in ("run_id", "until", "container_id", "started_at")):
            raise ArchiveError("cursor does not match manifest")
        _verify(root, run, materialize=False)
        return previous
    except (KeyError, TypeError):
        raise ArchiveError("invalid cursor") from None


def _run_docker(args, *, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout):
    """subprocess.run-compatible boundary with bounded bytes and finite timeout.

    Drain both pipes concurrently. Direct subprocess.run-to-file cannot enforce
    a disk limit while Docker writes; this pump enforces the reserved raw budget.
    """
    limit = MAX_CAPTURE_BYTES if args[1] == "logs" else 64 * 1024
    out = io.BytesIO() if stdout == subprocess.PIPE else stdout
    err = io.BytesIO() if stderr == subprocess.PIPE else stderr
    deadline = time.monotonic() + timeout
    total = 0
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, out)
                selector.register(process.stderr, selectors.EVENT_READ, err)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ArchiveError("docker timeout")
                    for key, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > limit:
                            raise ArchiveError("docker capture limit")
                        key.data.write(chunk)
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
    return subprocess.CompletedProcess(args, code,
                                       out.getvalue() if stdout == subprocess.PIPE else None,
                                       err.getvalue() if stderr == subprocess.PIPE else None)


def _inspect(runner, container):
    result = runner(["docker", "inspect", "--format",
                     '{"Id":{{json .Id}},"StartedAt":{{json .State.StartedAt}}}', container],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
    if result.returncode:
        raise ArchiveError("docker inspect failed")
    try:
        data = json.loads(result.stdout)
        cid = data["Id"]
        if not isinstance(cid, str) or not CID.fullmatch(cid):
            raise ValueError
        return cid, _date(data["StartedAt"])
    except (KeyError, TypeError, ValueError):
        raise ArchiveError("invalid docker identity") from None


def _hash_file(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _compress(source, target, since, until):
    digest = hashlib.sha256()
    first = last = None
    size = 0
    with source.open("rb") as raw, _new_file(target) as file:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=file, mtime=0) as compressed:
            # Only Docker's timestamp prefix is parsed; payloads remain opaque bytes.
            for line in raw:
                compressed.write(line)
                digest.update(line)
                size += len(line)
                prefix = line[:64].split(b" ", 1)[0]
                if STAMP.fullmatch(prefix):
                    try:
                        timestamp = _date(prefix.decode("ascii"))
                    except ArchiveError:
                        continue
                    if since <= timestamp <= until:
                        first = timestamp if first is None else min(first, timestamp)
                        last = timestamp if last is None else max(last, timestamp)
        # GzipFile.close writes the CRC/size footer before the underlying file sync.
        file.flush()
        os.fsync(file.fileno())
    return {"raw_sha256": digest.hexdigest(), "gz_sha256": _hash_file(target),
            "raw_bytes": size, "gz_bytes": target.stat().st_size,
            "first": _iso(first) if first else None, "last": _iso(last) if last else None}


def _record_failure(root, run_id, until, stage, replacement=None):
    record = {"policy": POLICY["policy"], "run_id": run_id, "until": _iso(until), "stage": stage}
    if replacement is not None:
        record["replacement"] = replacement
    try:
        directory = root / "failures"
        directory.mkdir(mode=0o700, exist_ok=True)
        _atomic_json(directory / (run_id + ".json"), record)
    except (OSError, ArchiveError):
        # No writer can promise durable history when the history device also fails.
        print("ARCHIVE_FAILURE_HISTORY_UNAVAILABLE", file=sys.stderr)


def _collect_locked(root, *, runner, container, now, scheduled_slot=None):
    run_id = uuid.uuid4().hex
    stage = "cursor"
    replacement = None
    try:
        previous = _checked_cursor(root)
        if previous and _date(previous["until"]) > now:
            raise ArchiveError("future cursor")
        stage = "inspect"
        cid, started = _inspect(runner, container)
        if started > now:
            raise ArchiveError("future container start")
        changed = bool(previous and previous["container_id"] != cid)
        restarted = bool(previous and not changed and previous["started_at"] != _iso(started))
        if changed:
            # A failed logs/compression attempt can already have observed the
            # replacement. Retrying later must not move that first observation.
            first_seen = min([now] + [
                _date(f["replacement"]["interval"][1]) for f in failures(root)
                if f.get("replacement", {}).get("previous_run_id") == previous["run_id"]
            ])
            replacement = {"previous_run_id": previous["run_id"], "new_container_id": cid,
                           "interval": [previous["until"], _iso(first_seen)]}
        since = (max(now - timedelta(hours=24), started) if not previous or changed or restarted
                 else max(_date(previous["until"]) - timedelta(minutes=10), started))
        work = root / "runs" / (".pending-" + run_id)
        final = root / "runs" / run_id
        work.mkdir(mode=0o700)
        stage = "logs"
        with _new_file(work / "stdout.raw") as out, _new_file(work / "stderr.raw") as err:
            result = runner(["docker", "logs", "--timestamps", "--since", _iso(since),
                             "--until", _iso(now), cid], stdout=out, stderr=err, timeout=TIMEOUT)
            for file, data in ((out, result.stdout), (err, result.stderr)):
                if data is not None:
                    file.write(data)
            if result.returncode:
                raise ArchiveError("docker logs failed")
        if sum((work / (s + ".raw")).stat().st_size for s in STREAMS) > MAX_CAPTURE_BYTES:
            raise ArchiveError("docker capture limit")
        stage = "compress"
        streams = {s: _compress(work / (s + ".raw"), work / (s + ".gz"), since, now) for s in STREAMS}
        for stream in STREAMS:
            (work / (stream + ".raw")).unlink()
        stage = "publish"
        _fsync_dir(work)
        os.replace(work, final)
        _fsync_dir(final.parent)
        manifest = {"policy": POLICY["policy"], "run_id": run_id, "container_id": cid,
                    "started_at": _iso(started), "since": _iso(since), "until": _iso(now),
                    "container_changed": changed, "restarted": restarted, "streams": streams,
                    "unarchived_tail": replacement["interval"] if replacement else None,
                    "scheduled_slot": scheduled_slot}
        stage = "manifest"
        _atomic_json(final / "manifest.json", manifest)
        stage = "cursor_commit"
        _atomic_json(root / "cursor.json", {key: manifest[key] for key in
                                           ("run_id", "until", "container_id", "started_at")})
        return 0
    except (OSError, ArchiveError, ValueError, KeyError, TypeError, EOFError,
            zlib.error, subprocess.SubprocessError):
        _record_failure(root, run_id, now, stage, replacement)
        return 2


def collect(root, *, runner=_run_docker, container="exchange-rate-app", now=None):
    """Return 0=stored, 2=failure, 3=busy. Unsafe roots raise ArchiveError."""
    now = _now(now)
    root = _root(root, mutate=True)
    try:
        with locked(root) as root:
            return _collect_locked(root, runner=runner, container=container, now=now)
    except _Busy:
        return 3
    except OSError:
        # A transient first-write/fsync failure may precede the policy marker.
        # Reacquire the lock and finish ownership initialization before recording
        # it, so history is readable and the next collection can retry safely.
        try:
            with locked(root) as root:
                _record_failure(root, uuid.uuid4().hex, now, "initialize")
        except (OSError, ArchiveError):
            print("ARCHIVE_FAILURE_HISTORY_UNAVAILABLE", file=sys.stderr)
        return 2


def _valid_id(value):
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise ArchiveError("invalid run id")
    return value


def _verify(root, run, *, materialize):
    result = []
    try:
        _validate_record(run, "runs", _valid_id(run["run_id"]))
        directory = root / "runs" / _valid_id(run["run_id"])
        if (root / "runs").is_symlink() or directory.is_symlink():
            raise ArchiveError("symlink run")
        for stream in STREAMS:
            path = directory / (stream + ".gz")
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ArchiveError("invalid stream file")
            metadata = run["streams"][stream]
            if _hash_file(path) != metadata["gz_sha256"]:
                raise ArchiveError("compressed hash mismatch")
            digest = hashlib.sha256()
            size = 0
            chunks = []
            with gzip.open(path, "rb") as file:
                while chunk := file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_CAPTURE_BYTES:
                        raise ArchiveError("restored stream too large")
                    digest.update(chunk)
                    if materialize:
                        chunks.append(chunk)
            if digest.hexdigest() != metadata["raw_sha256"] or size != metadata["raw_bytes"]:
                raise ArchiveError("raw hash or size mismatch")
            result.append(b"".join(chunks))
        return tuple(result)
    except (OSError, ValueError, KeyError, TypeError, EOFError, zlib.error):
        raise ArchiveError("archive verification failed") from None


def restore(root, run_id):
    root = _require(root)
    run_id = _valid_id(run_id)
    if (root / "runs").is_symlink() or (root / "runs" / run_id).is_symlink():
        raise ArchiveError("symlink run")
    manifest = _read_record(root / "runs" / run_id / "manifest.json", "runs", run_id)
    return _verify(root, manifest, materialize=True)


def coverage(root):
    """Union of verified, nonempty windows; silence never establishes coverage."""
    root = _require(root)
    windows, unknown, tails = [], [], []
    for run in runs(root):
        _verify(root, run, materialize=False)
        firsts = [_date(s["first"]) for s in run["streams"].values() if s["first"]]
        if firsts:
            windows.append((min(firsts), _date(run["until"])))
        else:
            unknown.append([run["since"], run["until"]])
        if run["unarchived_tail"]:
            tails.append(run["unarchived_tail"])
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps = []
    for left, right in zip(merged, merged[1:]):
        remaining = [(left[1], right[0])]
        for a, b in unknown:
            a, b = _date(a), _date(b)
            split = []
            for start, end in remaining:
                if b <= start or a >= end:
                    split.append((start, end))
                else:
                    if start < a:
                        split.append((start, a))
                    if b < end:
                        split.append((b, end))
            remaining = split
        gaps.extend([_iso(a), _iso(b)] for a, b in remaining)
    for failure in failures(root):
        tail = failure.get("replacement", {}).get("interval")
        if tail and tail not in tails:
            tails.append(tail)
    return {"archived": [[_iso(a), _iso(b)] for a, b in merged], "unknown": unknown,
            "gaps": gaps, "unarchived_tail": sorted(tails)}


def _remove_run(root, run):
    directory = root / "runs" / _valid_id(run["run_id"])
    if not directory.exists():
        _fsync_dir(directory.parent)
        return  # retry an intent whose directory removal already completed
    # Never recursive-delete a run: an operator's extra file must survive too.
    for name in ("manifest.json", "stdout.gz", "stderr.gz"):
        (directory / name).unlink(missing_ok=True)
    _fsync_dir(directory)
    if not any(directory.iterdir()):
        directory.rmdir()
        _fsync_dir(directory.parent)


def _expire_locked(root, now, dry_run):
    cutoff = now - timedelta(days=14)
    current = _checked_cursor(root)
    candidates = [r for r in runs(root) if _date(r["until"]) < cutoff
                  and (not current or r["run_id"] != current["run_id"])]
    # A prior deletion may have removed the manifest before a payload unlink
    # failed. Its durable intent is still authoritative for retrying those files.
    candidate_ids = {r["run_id"] for r in candidates}
    for record in deletions(root):
        if record["status"] != "pending" or record["run_id"] in candidate_ids:
            continue
        if (_date(record["until"]) >= cutoff
                or (current and record["run_id"] == current["run_id"])
                or (root / "runs" / record["run_id"] / "manifest.json").exists()):
            raise ArchiveError("inconsistent pending deletion")
        candidates.append(record)
    failed = failures(root)
    result = {"candidates": [r["run_id"] for r in candidates], "deleted": []}
    if dry_run:
        return result
    for run in candidates:
        # Durable intent before deleting; a crash can leave a pending audit record.
        path = root / "deletions" / (run["run_id"] + ".json")
        record = {"policy": POLICY["policy"], "run_id": run["run_id"], "until": run["until"],
                  "deleted_at": _iso(now), "status": "pending"}
        _atomic_json(path, record)
        _remove_run(root, run)
        record["status"] = "deleted"
        _atomic_json(path, record)
        result["deleted"].append(run["run_id"])
    for failure in failed:
        if _date(failure["until"]) < cutoff:
            # Remove only known partial payloads tied to a recorded failure.
            run_id = failure["run_id"]
            for directory in (root / "runs" / (".pending-" + run_id), root / "runs" / run_id):
                if directory.is_dir() and not (directory / "manifest.json").exists():
                    for name in ("stdout.raw", "stderr.raw", "stdout.gz", "stderr.gz"):
                        (directory / name).unlink(missing_ok=True)
                    _fsync_dir(directory)
                    if not any(directory.iterdir()):
                        directory.rmdir()
                        _fsync_dir(directory.parent)
            (root / "failures" / (run_id + ".json")).unlink()
            _fsync_dir(root / "failures")
    return result


def _record_expiry(root, now, failed):
    try:
        _atomic_json(root / EXPIRY_STATE, {"policy": POLICY["policy"],
                                         "until": _iso(now), "expiry_failed": failed})
        return True
    except (OSError, ArchiveError):
        # Retention history cannot be guaranteed on a failing device; keep
        # collection available and expose the loss through stderr AND JSON.
        print("ARCHIVE_EXPIRY_HISTORY_UNAVAILABLE", file=sys.stderr)
        return False


def _expiry_unresolved(root):
    path = root / EXPIRY_STATE
    if not path.exists():
        return False
    value = _json(path)
    if value.get("policy") != POLICY["policy"] or type(value.get("expiry_failed")) is not bool:
        raise ArchiveError("invalid expiry state")
    _date(value.get("until"))
    return value["expiry_failed"]


def _attempt_expiry(root, now, dry_run=False):
    try:
        expired = _expire_locked(root, now, dry_run)
    except (ArchiveError, OSError):
        saved = _record_expiry(root, now, True)
        return {"expiry": None, "expiry_failed": True, "expiry_history_failed": not saved}
    # Both failure recording and successful resolution are best effort. In
    # particular, an unavailable resolution write must not abort collection.
    saved = True if dry_run else _record_expiry(root, now, False)
    return {"expiry": expired, "expiry_failed": _expiry_unresolved(root) if dry_run else not saved,
            "expiry_history_failed": not saved}


def expire(root, *, now=None, dry_run=False):
    root = _require(_root(root, mutate=True))
    with locked(root):
        result = _attempt_expiry(root, _now(now), dry_run)
        if result["expiry"] is None:
            raise _ExpiryFailed(result)
        return {**result["expiry"], "expiry_failed": result["expiry_failed"],
                "expiry_history_failed": result["expiry_history_failed"]}


def _usage(root):
    """Include all files, including raw, pending, failed, and foreign files."""
    return sum(max(p.stat().st_size, p.stat().st_blocks * 512)
               for p in root.rglob("*") if p.is_file())


def check(root, *, first_slot, now=None, grace=timedelta(minutes=10)):
    """Check a sliding 24h window of expected :41 slots, delayed by grace."""
    now = _now(now)
    first = _date(first_slot) if isinstance(first_slot, str) else _now(first_slot)
    if first.minute != 41 or first.second or first.microsecond:
        raise ArchiveError("first slot must be at :41 UTC")
    cutoff = now - grace
    start = max(first, cutoff - timedelta(hours=24))
    slot = start.replace(minute=41, second=0, microsecond=0)
    if slot < start:
        slot += timedelta(hours=1)
    expected, pending = [], []
    while slot <= now:
        (expected if slot <= cutoff else pending).append(_iso(slot))
        slot += timedelta(hours=1)
    root = _require(root)
    successful = set()
    invalid, valid = [], []
    for run_id, path in _record_paths(root, "runs"):
        try:
            run = _read_record(path, "runs", run_id)
            _verify(root, run, materialize=False)
        except ArchiveError:
            invalid.append(run_id)
            continue
        valid.append(run)
        if run.get("scheduled_slot"):
            successful.add(run["scheduled_slot"])
    pending_deletions = [r["run_id"] for r in deletions(root) if r["status"] == "pending"]
    try:
        expiry_failed = _expiry_unresolved(root)
    except ArchiveError:
        expiry_failed = True  # unreadable state cannot certify a resolution
    last = max((_date(r["until"]) for r in valid), default=None)
    missed = [s for s in expected if s not in successful]
    stale = last is None or now - last > timedelta(hours=1) + grace
    return {"last_success": _iso(last) if last else None, "stale": stale,
            "missed_slots": missed, "pending_slots": pending, "invalid_runs": sorted(invalid),
            "pending_deletions": pending_deletions, "expiry_failed": expiry_failed,
            "ok": not (stale or missed or invalid or pending_deletions or expiry_failed)}


def cycle(root, *, runner=_run_docker, container="exchange-rate-app", now=None,
          max_archive_bytes=4 * 1024**3, min_free_bytes=5 * 1024**3):
    """Repository-owned cron wrapper: expire first, then measure/guard/collect."""
    now = _now(now)
    if max_archive_bytes <= 0 or min_free_bytes < 0:
        raise ArchiveError("invalid capacity guard")
    with locked(root) as root:
        result = _attempt_expiry(root, now)
        # A measurement failure aborts collection, after expiry has still run.
        try:
            used, free = _usage(root), shutil.disk_usage(root).free
        except OSError:
            return {**result, "collection_code": 2, "guard": "measurement_failed", "exit_code": 2}
        guard = ("archive_limit" if used + PEAK_RESERVE_BYTES > max_archive_bytes else
                 "free_space" if free - PEAK_RESERVE_BYTES < min_free_bytes else None)
        if guard:
            return {**result, "collection_code": None, "guard": guard, "exit_code": 4,
                    "used_bytes": used, "free_bytes": free, "reserve_bytes": PEAK_RESERVE_BYTES}
        slot = now.replace(minute=41, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(hours=1)
        code = _collect_locked(root, runner=runner, container=container, now=now, scheduled_slot=_iso(slot))
        return {**result, "collection_code": code, "guard": None,
                "exit_code": 2 if code else 5 if result["expiry_failed"] else 0}


def pre_switch(root, *, runner=_run_docker, container="exchange-rate-app", now=None):
    """Collect, verify both streams, and recheck identity under the shared lock.

    The caller still controls the actual switch; logs written after `until` are
    not covered by this receipt. This command never stops or replaces Docker.
    """
    now = _now(now)
    with locked(root) as root:
        code = _collect_locked(root, runner=runner, container=container, now=now)
        if code:
            return {"collection_code": code, "ready": False}
        saved = cursor(root)
        try:
            restore(root, saved["run_id"])
            cid, started = _inspect(runner, container)
            if cid != saved["container_id"] or _iso(started) != saved["started_at"]:
                raise ArchiveError("container changed during pre-switch collection")
        except (ArchiveError, OSError, ValueError, subprocess.SubprocessError):
            _record_failure(root, uuid.uuid4().hex, now, "switch_verify")
            return {"collection_code": 2, "ready": False}
        return {"collection_code": 0, "ready": True, **saved}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "cycle", "pre-switch", "check", "runs", "coverage", "verify", "expire"))
    parser.add_argument("--root", type=Path, default=Path.home() / "logs" / "docker-archive")
    parser.add_argument("--container", default="exchange-rate-app")
    parser.add_argument("--first-slot")
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-archive-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--min-free-bytes", type=int, default=5 * 1024**3)
    args = parser.parse_args(argv)
    if args.dry_run and args.command != "expire":
        parser.error("--dry-run is only supported by expire")
    try:
        if args.command == "collect":
            code = collect(args.root, container=args.container)
            print(json.dumps({"collection_code": code}))
            return code
        if args.command == "cycle":
            result = cycle(args.root, container=args.container, max_archive_bytes=args.max_archive_bytes,
                           min_free_bytes=args.min_free_bytes)
            code = result["exit_code"]
        elif args.command == "pre-switch":
            result = pre_switch(args.root, container=args.container)
            code = result["collection_code"]
        elif args.command == "check":
            if not args.first_slot:
                parser.error("check requires --first-slot")
            result = check(args.root, first_slot=args.first_slot)
            code = 0 if result["ok"] else 1
        elif args.command == "verify":
            if not args.run_id:
                parser.error("verify requires --run-id")
            restore(args.root, args.run_id)
            result, code = {"run_id": args.run_id, "verified": True}, 0
        else:
            result = (expire(args.root, dry_run=args.dry_run) if args.command == "expire" else
                      coverage(args.root) if args.command == "coverage" else runs(args.root))
            code = 2 if args.command == "expire" and result["expiry_history_failed"] else 0
        print(json.dumps(result, sort_keys=True))
        return code
    except _ExpiryFailed as error:
        print(json.dumps(error.report, sort_keys=True))
        print("ARCHIVE_OPERATION_FAILED", file=sys.stderr)
        return 2
    except _Busy:
        print("ARCHIVE_BUSY", file=sys.stderr)
        return 3
    except (ArchiveError, OSError, ValueError, KeyError, TypeError):
        print("ARCHIVE_OPERATION_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
