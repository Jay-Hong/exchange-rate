#!/usr/bin/env python3
"""Archive Docker log bytes before advancing an Investing observation cursor.

Stdlib only. See scripts/INVESTING_OBSERVE.md for the on-disk protocol.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4


UTC = timezone.utc
KST = timezone(timedelta(hours=9))
REPO = Path(__file__).resolve().parents[1]
OVERLAP = timedelta(minutes=10)
COLD_START = timedelta(hours=24)
INSPECT_FORMAT = '{"Id":{{json .Id}},"StartedAt":{{json .State.StartedAt}}}'


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(UTC)


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_dir(path):
    if not path.exists():
        make_dir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        fsync_dir(path.parent)


def atomic_bytes(path, data):
    """fsync contents, replace in the same directory, then fsync the directory."""
    make_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       allow_nan=False, separators=(",", ":")) + "\n").encode()


def atomic_json(path, value):
    atomic_bytes(path, json_bytes(value))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_line(raw):
    """Return a candidate event; malformed candidates raise and retain raw bytes."""
    if b"investing_" not in raw:
        return None
    ts, separator, outer = raw.decode("utf-8").partition(" ")
    if not separator:
        raise ValueError("missing Docker timestamp")
    timestamp(ts)
    envelope = strict_json(outer)
    if not isinstance(envelope, dict):
        raise ValueError("outer log must be an object")
    message = envelope.get("message")
    if not isinstance(message, str):
        raise ValueError("log message must be a string")
    if "investing_" not in message:
        return None
    event = strict_json(message)
    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
        raise ValueError("event must be an object with an event name")
    if not event["event"].startswith("investing_"):
        return None
    return ts, event


def load_cursor(path):
    if not path.exists():
        return None
    cursor = strict_json(path.read_bytes())
    if not isinstance(cursor, dict) or cursor.get("schema_version") != 1:
        raise ValueError("invalid cursor schema")
    for key in ("container_id", "run_id", "started_at", "until"):
        if not isinstance(cursor.get(key), str) or not cursor[key]:
            raise ValueError(f"invalid cursor {key}")
    timestamp(cursor["started_at"])
    timestamp(cursor["until"])
    return cursor


def extract(out_dir, container="exchange-rate-app", *, runner=subprocess.run, now=None):
    """Return 0=archived, 1=archived with rejects, 2=failure, 3=lock busy.

    runner is the only Docker boundary and is replaced in regression tests.
    Per-run journals avoid concurrent append corruption, including lock losers.
    """
    out_dir = Path(out_dir).expanduser().resolve()
    if out_dir.is_relative_to(REPO):
        raise ValueError("observation artifacts must be outside the repository")
    now = now or datetime.now(UTC)
    now = timestamp(now.isoformat())
    run_id = uuid4().hex
    run_dir = out_dir / "runs" / run_id
    info = {"schema_version": 1, "run_id": run_id, "at": now.isoformat(),
            "until": now.isoformat(), "container_name": container,
            "since": (now - COLD_START).isoformat(), "phase": "started",
            "container_id": None, "container_changed": False, "restarted": False,
            "parse_failed": 0, "written": 0, "stage": "initialize"}
    lock = None
    code = 2
    try:
        make_dir(run_dir)
        atomic_json(run_dir / "started.json", info)
        info["stage"] = "lock"
        lock = (out_dir / "extract.lock").open("a+b")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info["stage"] = "provenance"
        info["script_sha256"] = {
            name: sha256(Path(__file__).with_name(name)) for name in
            ("investing_observe_extract.py", "investing_observe_aggregate.py")
        }
        info["stage"] = "cursor_read"
        cursor = load_cursor(out_dir / "cursor.json")
        info["previous_cursor"] = cursor
        if cursor:
            if timestamp(cursor["until"]) > now:
                raise ValueError("cursor is in the future")
            info["since"] = (timestamp(cursor["until"]) - OVERLAP).isoformat()
        info["stage"] = "inspect"
        inspected = runner(["docker", "inspect", "--format", INSPECT_FORMAT, container],
                           capture_output=True, timeout=180, check=False)
        if inspected.returncode:
            raise RuntimeError(f"docker inspect exit {inspected.returncode}")
        identity = strict_json(inspected.stdout)
        cid, started = identity["Id"], identity["StartedAt"]
        if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise ValueError("inspect did not return a full container ID")
        timestamp(started)
        info.update(container_id=cid, started_at=started)
        if cursor:
            info["container_changed"] = cursor["container_id"] != cid
            info["restarted"] = not info["container_changed"] and cursor["started_at"] != started
        if not cursor or info["container_changed"] or info["restarted"]:
            info["since"] = (now - COLD_START).isoformat()
        # One lookup by name only. Every subsequent Docker read uses this exact ID.
        info["stage"] = "raw_capture"
        atomic_json(run_dir / "request.json", info)
        stdout_path, stderr_path = run_dir / "stdout.raw", run_dir / "stderr.raw"
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            try:
                logged = runner(["docker", "logs", "--timestamps", "--since", info["since"],
                                 "--until", info["until"], cid],
                                stdout=stdout, stderr=stderr, timeout=180, check=False)
            finally:
                # Also preserve partial output on timeout / process errors.
                for handle in (stdout, stderr):
                    handle.flush()
                    os.fsync(handle.fileno())
                fsync_dir(run_dir)
        if logged.returncode:
            raise RuntimeError(f"docker logs exit {logged.returncode}")
        info["stage"] = "parse"
        per_day, rejects, first_time = {}, [], None
        since_time = timestamp(info["since"])
        for raw_path in (stdout_path, stderr_path):
            with raw_path.open("rb") as handle:
                for number, raw in enumerate(handle, 1):
                    try:
                        log_time = timestamp(raw.split(b" ", 1)[0].decode("ascii"))
                        if since_time <= log_time <= now:
                            first_time = min(first_time, log_time) if first_time else log_time
                    except (ValueError, UnicodeError):
                        pass  # Non-timestamp diagnostics remain in the raw archive.
                    try:
                        parsed = parse_line(raw)
                        if parsed is None:
                            continue
                        ts, event = parsed
                        event_time = timestamp(ts)
                        if not since_time <= event_time <= now:
                            raise ValueError("event outside requested extraction window")
                        day = event_time.astimezone(KST).date().isoformat()
                        row = {"ts": ts, "container_id": cid, "event": event,
                               "run_id": run_id, "raw_file": raw_path.name, "raw_line": number}
                        per_day.setdefault(day, []).append(row)
                    except (ValueError, UnicodeError) as error:
                        rejects.append({"raw_file": raw_path.name, "raw_line": number,
                                        "error": str(error)})
        info["parse_failed"] = len(rejects)
        info["stage"] = "rejects_write"
        atomic_bytes(run_dir / "rejects.jsonl", b"".join(map(json_bytes, rejects)))
        info["stage"] = "events_write"
        artifacts = [stdout_path, stderr_path, run_dir / "rejects.jsonl"]
        for day, rows in sorted(per_day.items()):
            path = out_dir / "events" / day / f"{run_id}.jsonl"
            atomic_bytes(path, b"".join(map(json_bytes, rows)))
            artifacts.append(path)
            info["written"] += len(rows)
        # Requested --since is NOT proof that Docker retained that much history.
        coverage_start = max(since_time, first_time, timestamp(started)) if first_time else None
        info["coverage_since"] = (coverage_start.isoformat()
                                  if coverage_start is not None and coverage_start <= now else None)
        info["artifacts"] = {str(path.relative_to(out_dir)): sha256(path) for path in artifacts}
        info["stage"] = "history_prepare"
        info["phase"] = "prepared"
        atomic_json(run_dir / "prepared.json", info)
        info["stage"] = "cursor_write"
        atomic_json(out_dir / "cursor.json", {
            "schema_version": 1, "container_id": cid, "started_at": started,
            "until": info["until"], "run_id": run_id,
        })
        code = 1 if rejects else 0
        info.update(phase="finished", ok=True, cursor_committed=True)
    except BlockingIOError as error:
        code = 3
        info.update(phase="failed", ok=False, error_type=type(error).__name__, error=str(error))
    except Exception as error:
        info.update(phase="failed", ok=False, error_type=type(error).__name__, error=str(error))
    finally:
        # A prepared journal already exists before any cursor replace. A missing
        # result after SIGKILL/disk failure is explicitly an incomplete extraction.
        try:
            atomic_json(run_dir / "result.json", info)
        except Exception as error:
            print(f"EXTRACT_HISTORY_FAILED run_id={run_id} stage={info['stage']} "
                  f"error={type(error).__name__}: {error}", file=sys.stderr)
            code = 2
        if lock is not None:
            lock.close()
    print(f"EXTRACT_RESULT run_id={run_id} code={code} stage={info['stage']} "
          f"written={info['written']} parse_failed={info['parse_failed']}", file=sys.stderr)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="exchange-rate-app")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "logs/investing-observe")
    args = parser.parse_args(argv)
    if not args.output_dir.expanduser().resolve().is_relative_to((Path.home() / "logs").resolve()):
        parser.error("--output-dir must be under ~/logs (outside Git)")
    return extract(args.output_dir, args.container)


if __name__ == "__main__":
    sys.exit(main())
