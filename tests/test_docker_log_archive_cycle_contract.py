"""Docker log archive — `cycle`/`check` contract for expiry isolation (decision E), read-only for the implementer.

Written before the implementation (Claude) from `design/log-retention/expiry_isolation_r1.txt` and Codex's answer
(2026-09-21). The v2 contract (`test_docker_log_archive_contract.py`, sha256 e21f8512…) is unchanged; this file adds to it.

Why: an expiry-only fault (one old record unreadable, one old run that cannot be deleted) used to stop the hourly collection
too. Docker keeps ~18–20 hours, so a stop longer than that loses logs for good, while a failed expiry is bounded by the
capacity guard. Shared state (the cursor and the run it rests on) still stops everything.

Pinned behaviour (no file or directory name is pinned; records are found by their content):
- `cycle(...)` returns a dict with at least `expiry` (the expiry result, or None when it failed), `expiry_failed` (bool),
  `collection_code`, `guard` and `exit_code`. Exit code precedence: collection failed (2) > guard skipped collection (4) >
  expiry failed but collection succeeded (5) > 0. A guard or a collection failure never hides `expiry_failed`.
- An expiry fault is any refusal while reading or deleting expiry candidates, including a record with a missing or
  malformed field (normalised to `ArchiveError`, never a stray `KeyError`). The cursor check stays strict.
- A durable "expiry unresolved" state: set by a failed expiry, cleared only by a later expiry that completes for real
  (cycle or manual `expire`). A dry run and a successful collection do not clear it. `check` reports it as `expiry_failed`.
- `check` judges record by record: an unreadable run record is listed in `invalid_runs` by run id and the rest are still
  judged. A deletion left `pending` is listed in `pending_deletions`. Either makes `ok` false.
- The CLI prints the result and returns `exit_code` for `cycle`.
Left to the implementer's own tests (as in v2): a failure to store the expiry history must not stop collection and must be
visible in the JSON; the exact durable write order; and that isolation is limited to expected refusals — a programming
error is not swallowed as an expiry failure.
"""

import json
import os
import stat
from datetime import timedelta

import pytest

from scripts import docker_log_archive as A
from tests.test_docker_log_archive_contract import CID, NOW, OTHER, Docker, iso, line, root_of

SLOT = NOW.replace(minute=41) - timedelta(hours=1)          # the scheduled slot a cycle at NOW fills
AFTER = NOW + timedelta(minutes=11)                          # a check time whose window has closed on SLOT


def _json_records(root):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_bytes())
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                yield path, value


def _manifest_path(root, run_id):
    [path] = [p for p, v in _json_records(root) if v.get("run_id") == run_id and "streams" in v]
    return path


def _failure_path(root, run_id):
    [path] = [p for p, v in _json_records(root) if v.get("run_id") == run_id and "stage" in v]
    return path


def _history(tmp_path, *, failed_between=False):
    """An old run (eligible for expiry), optionally a failed collection after it, and a recent run the cursor rests on.

    Times only move forward: a collection earlier than the cursor would fail on the cursor, not on Docker."""
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(line(NOW - timedelta(days=20, minutes=1))), now=NOW - timedelta(days=20)) == 0
    [old] = A.runs(root)
    if failed_between:
        assert A.collect(root, runner=Docker(failure="logs_exit"), now=NOW - timedelta(days=16)) == 2
        assert [f["stage"] for f in A.failures(root)] != []
    assert A.collect(root, runner=Docker(line(NOW - timedelta(days=1, minutes=1))),
                     now=NOW - timedelta(days=1)) == 0
    return root, old["run_id"]


def _cycle(root, docker=None, **options):
    docker = docker or Docker(line(NOW - timedelta(minutes=1)))
    return A.cycle(root, runner=docker, now=NOW, **options), docker


def _corrupt(path, how):
    if how == "not_json":
        path.write_bytes(b"{ not json")
    else:
        value = json.loads(path.read_bytes())
        del value[how]
        path.write_bytes(json.dumps(value).encode())


def _collected_now(root, result, docker):
    assert result["collection_code"] == 0 and docker.logs_calls()
    assert A.cursor(root)["until"] == iso(NOW)


# ── an expiry-only fault does not stop collection ────────────────────────────

@pytest.mark.parametrize("how", ["not_json", "until", "run_id"])
def test_an_unreadable_old_run_record_fails_expiry_but_not_collection(tmp_path, how):
    root, old = _history(tmp_path)
    _corrupt(_manifest_path(root, old), how)
    result, docker = _cycle(root)
    _collected_now(root, result, docker)
    assert result["expiry_failed"] is True and result["expiry"] is None
    assert result["exit_code"] == 5


@pytest.mark.parametrize("how", ["not_json", "until"])
def test_an_unreadable_old_failure_record_fails_expiry_but_not_collection_on_the_same_container(tmp_path, how):
    root, _ = _history(tmp_path, failed_between=True)
    [failure] = A.failures(root)
    _corrupt(_failure_path(root, failure["run_id"]), how)
    result, docker = _cycle(root)
    _collected_now(root, result, docker)
    assert result["expiry_failed"] is True and result["exit_code"] == 5


def test_an_old_run_that_cannot_be_deleted_fails_expiry_but_not_collection(tmp_path):
    root, old = _history(tmp_path)
    directory = _manifest_path(root, old).parent
    mode = directory.stat().st_mode
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)             # entries can no longer be removed
    try:
        result, docker = _cycle(root)
    finally:
        directory.chmod(mode)
    _collected_now(root, result, docker)
    assert result["expiry_failed"] is True and result["exit_code"] == 5


def test_a_clean_cycle_reports_no_expiry_failure(tmp_path):
    root, old = _history(tmp_path)
    result, docker = _cycle(root)
    _collected_now(root, result, docker)
    assert result["expiry_failed"] is False and result["exit_code"] == 0
    assert result["expiry"]["deleted"] == [old]


# ── shared state still stops everything ──────────────────────────────────────

def test_a_corrupt_cursor_stops_collection_before_docker(tmp_path):
    root, _ = _history(tmp_path)
    saved = A.cursor(root)
    [path] = [p for p, v in _json_records(root) if v == saved]
    path.write_bytes(b"{ not json")
    result, docker = _cycle(root)
    assert result["collection_code"] == 2 and result["exit_code"] == 2 and docker.calls == []
    assert result["expiry_failed"] is True


def test_on_a_replaced_container_an_unreadable_failure_record_still_stops_collection(tmp_path):
    # Replacement recovers the first time it was seen from the failure history; that stays strict (tail contract).
    root, _ = _history(tmp_path, failed_between=True)
    [failure] = A.failures(root)
    _corrupt(_failure_path(root, failure["run_id"]), "not_json")
    result, _ = _cycle(root, Docker(line(NOW - timedelta(minutes=1)), cid=OTHER))
    assert result["collection_code"] == 2 and result["exit_code"] == 2
    assert A.cursor(root)["container_id"] == CID


# ── the exit code keeps every signal ─────────────────────────────────────────

def test_a_guard_on_top_of_an_expiry_failure_is_four_and_still_says_expiry_failed(tmp_path):
    root, old = _history(tmp_path)
    _corrupt(_manifest_path(root, old), "not_json")
    result, docker = _cycle(root, max_archive_bytes=1)
    assert result["guard"] == "archive_limit" and result["collection_code"] is None and docker.calls == []
    assert result["expiry_failed"] is True and result["exit_code"] == 4


def test_a_measurement_failure_on_top_of_an_expiry_failure_is_two(tmp_path, monkeypatch):
    root, old = _history(tmp_path)
    _corrupt(_manifest_path(root, old), "not_json")

    def unavailable(_):
        raise OSError("unavailable")

    monkeypatch.setattr(A.shutil, "disk_usage", unavailable)
    result, _ = _cycle(root)
    assert result["exit_code"] == 2 and result["expiry_failed"] is True


def test_a_guard_alone_is_still_four(tmp_path):
    root, _ = _history(tmp_path)
    result, _ = _cycle(root, max_archive_bytes=1)
    assert result["expiry_failed"] is False and result["exit_code"] == 4


@pytest.mark.parametrize("exit_code", [0, 2, 4, 5])
def test_the_cli_returns_the_cycle_exit_code_and_prints_the_flag(tmp_path, monkeypatch, capsys, exit_code):
    fake = {"expiry": None, "expiry_failed": exit_code == 5, "collection_code": 0, "guard": None,
            "exit_code": exit_code}
    monkeypatch.setattr(A, "cycle", lambda *args, **kwargs: dict(fake))
    assert A.main(["cycle", "--root", str(root_of(tmp_path))]) == exit_code
    printed = json.loads(capsys.readouterr().out)
    assert printed["expiry_failed"] is fake["expiry_failed"] and printed["exit_code"] == exit_code


# ── the unresolved state and check ───────────────────────────────────────────

def _check(root):
    return A.check(root, first_slot=iso(SLOT), now=AFTER)


def test_an_expiry_failure_stays_visible_until_an_expiry_actually_completes(tmp_path):
    root, old = _history(tmp_path)
    path = _manifest_path(root, old)
    original = path.read_bytes()
    _corrupt(path, "not_json")
    assert _cycle(root)[0]["exit_code"] == 5
    first = _check(root)
    assert first["expiry_failed"] is True and first["ok"] is False
    path.write_bytes(original)                                 # the operator repairs the record
    A.expire(root, now=NOW, dry_run=True)                      # a dry run resolves nothing
    alone = _check(root)
    # Nothing else is wrong any more (record repaired, slot filled, fresh), so this is the unresolved state by itself.
    assert alone["invalid_runs"] == [] and alone["missed_slots"] == [] and alone["stale"] is False
    assert alone["expiry_failed"] is True and alone["ok"] is False
    A.collect(root, runner=Docker(line(NOW)), now=NOW + timedelta(minutes=1))   # nor does a collection
    assert _check(root)["expiry_failed"] is True
    A.expire(root, now=NOW + timedelta(minutes=2))             # a real, complete expiry does
    assert _check(root)["expiry_failed"] is False


def test_a_failed_manual_expire_also_leaves_it_unresolved(tmp_path):
    root, old = _history(tmp_path)
    assert _cycle(root)[0]["exit_code"] == 0                   # the old run is gone; nothing unresolved
    assert _check(root)["expiry_failed"] is False
    victim = A.runs(root)[0]["run_id"]
    _corrupt(_manifest_path(root, victim), "not_json")
    with pytest.raises(A.ArchiveError):
        A.expire(root, now=NOW + timedelta(minutes=1))
    assert _check(root)["expiry_failed"] is True


def test_a_later_clean_cycle_also_resolves_it(tmp_path):
    root, old = _history(tmp_path)
    path = _manifest_path(root, old)
    original = path.read_bytes()
    _corrupt(path, "until")
    assert _cycle(root)[0]["exit_code"] == 5
    path.write_bytes(original)
    later = A.cycle(root, runner=Docker(line(NOW)), now=NOW + timedelta(hours=1))
    assert later["expiry_failed"] is False and later["exit_code"] == 0
    assert A.check(root, first_slot=iso(SLOT), now=NOW + timedelta(hours=1, minutes=11))["expiry_failed"] is False


@pytest.mark.parametrize("how", ["not_json", "until", "streams"])
def test_check_judges_the_other_runs_when_one_record_is_unreadable(tmp_path, how):
    root, old = _history(tmp_path)
    assert _cycle(root)[0]["exit_code"] == 0                    # the old run is gone; SLOT is filled
    recent = [r["run_id"] for r in A.runs(root)]
    victim = recent[0]
    _corrupt(_manifest_path(root, victim), how)
    result = _check(root)
    assert victim in result["invalid_runs"]
    assert result["last_success"] == iso(NOW) and result["missed_slots"] == []
    assert result["ok"] is False


def test_check_exit_code_is_one_not_two_for_an_unreadable_record(tmp_path):
    root, _ = _history(tmp_path)
    assert _cycle(root)[0]["exit_code"] == 0
    victim = A.runs(root)[0]["run_id"]
    _corrupt(_manifest_path(root, victim), "not_json")
    code = A.main(["check", "--root", str(root), "--first-slot", iso(SLOT)])
    assert code == 1


def test_a_deletion_left_pending_is_not_hidden_by_a_later_clean_expiry(tmp_path, monkeypatch):
    root, old = _history(tmp_path)
    real_unlink = os.unlink

    def refuse_gzip(path, *args, **kwargs):
        with open(path, "rb") as file:
            if file.read(2) == b"\x1f\x8b":
                raise PermissionError("refused")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse_gzip)
    monkeypatch.setattr(os, "remove", refuse_gzip)
    first = _cycle(root)[0]
    monkeypatch.undo()
    assert first["expiry_failed"] is True and first["collection_code"] == 0
    assert old in _check(root)["pending_deletions"] and _check(root)["ok"] is False
    later = A.cycle(root, runner=Docker(line(NOW)), now=NOW + timedelta(hours=1))
    report = A.check(root, first_slot=iso(SLOT), now=NOW + timedelta(hours=1, minutes=11))
    assert later["collection_code"] == 0
    if old in report["pending_deletions"]:
        # Not finished: the half-deleted run must stay visible, however the later expiry itself went.
        assert report["ok"] is False
    else:
        # A retry that finished the deletion is fine, but then nothing of the old run may be left behind.
        gzip_files = [p for p in root.rglob("*") if p.is_file() and p.read_bytes()[:2] == b"\x1f\x8b"]
        assert len(gzip_files) == 2 * len(A.runs(root))
