"""Implementation-side tests complement the READ-ONLY v2 contract.

Pin durable ORDER: gzip footer -> regular-file fsync -> final name and directory
fsync -> durable manifest -> cursor. Distinguish directory fds from file fds.
Also reject a wrong manifest raw hash when the compressed hash is still correct
(each stream independently). These are the agreed R26/R8b contract limitations.
No real Docker daemon, application modules, network, or server is used.
"""

from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import docker_log_archive as A


NOW = datetime(2026, 9, 23, 1, 41, tzinfo=timezone.utc)
SECRET = "api_key=PRIVATE9 홍길동".encode()
CID = "c" * 64


class Docker:
    def __init__(self, now=NOW, *, fail=None, empty=False):
        self.now, self.fail, self.empty = now, fail, empty
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[1] == "inspect":
            return SimpleNamespace(returncode=0, stderr=b"", stdout=json.dumps({
                "Id": CID, "StartedAt": "2026-08-01T00:00:00Z"}).encode())
        payload = b"" if self.empty else (A._iso(self.now - timedelta(seconds=1)).encode() + b" " + SECRET + b"\n")
        kwargs["stdout"].write(payload)
        kwargs["stderr"].write(payload + (b"\xff\xfe\n" if payload else b""))
        if self.fail == "write":
            raise OSError(SECRET.decode())
        if self.fail == "timeout":
            raise subprocess.TimeoutExpired(args, 1, output=payload, stderr=payload)
        return SimpleNamespace(returncode=1 if self.fail == "exit" else 0, stdout=None, stderr=None)


def collect(root, now=NOW, **kwargs):
    return A.collect(root, runner=Docker(now), now=now, **kwargs)


def manifest_path(root, run):
    return root / "runs" / run["run_id"] / "manifest.json"


def test_durable_order_and_footer_with_real_file_and_directory_fds(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    assert collect(root) == 0
    events, paths = [], {}
    real_sync, real_replace, real_open = os.fsync, os.replace, os.open

    def opening(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        paths[fd] = Path(path)
        return fd

    def syncing(fd):
        path = paths[fd]
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        if path.suffix == ".gz":
            # This fails if fsync happens before the footer or before buffer flush.
            assert SECRET in gzip.decompress(path.read_bytes())
        events.append(("sync", kind, path))
        real_sync(fd)

    def replacing(source, target):
        events.append(("rename", Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "fsync", syncing)
    monkeypatch.setattr(os, "replace", replacing)
    assert collect(root, NOW + timedelta(hours=1)) == 0
    run = A.runs(root)[-1]
    final = manifest_path(root, run).parent
    publish = next(i for i, e in enumerate(events) if e[0] == "rename" and e[2] == final)
    gz_syncs = [i for i, e in enumerate(events) if e[:2] == ("sync", "file") and e[2].suffix == ".gz"]
    assert len(gz_syncs) == 2 and max(gz_syncs) < publish
    work_dir_sync = next(i for i, e in enumerate(events) if e[:2] == ("sync", "directory") and e[2].name.startswith(".pending-"))
    assert max(gz_syncs) < work_dir_sync < publish
    name_sync = next(i for i, e in enumerate(events) if i > publish and e == ("sync", "directory", root / "runs"))
    manifest_sync = next(i for i, e in enumerate(events) if e[:2] == ("sync", "file") and e[2].name.startswith(".manifest.json."))
    manifest_rename = next(i for i, e in enumerate(events) if e[0] == "rename" and e[2] == final / "manifest.json")
    manifest_dir_sync = next(i for i, e in enumerate(events) if i > manifest_rename and e == ("sync", "directory", final))
    cursor_sync = next(i for i, e in enumerate(events) if e[:2] == ("sync", "file") and e[2].name.startswith(".cursor.json."))
    cursor_rename = next(i for i, e in enumerate(events) if e[0] == "rename" and e[2] == root / "cursor.json")
    cursor_dir_sync = next(i for i, e in enumerate(events) if i > cursor_rename and e == ("sync", "directory", root))
    assert publish < name_sync < manifest_sync < manifest_rename < manifest_dir_sync < cursor_sync < cursor_rename < cursor_dir_sync


@pytest.mark.parametrize("stream", A.STREAMS)
def test_wrong_raw_hash_refused_even_with_correct_gzip_hash(tmp_path, stream):
    root = tmp_path / "archive"
    assert collect(root) == 0
    [run] = A.runs(root)
    path = manifest_path(root, run)
    payload = (path.parent / (stream + ".gz")).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == run["streams"][stream]["gz_sha256"]
    assert SECRET in gzip.decompress(payload)
    run["streams"][stream]["raw_sha256"] = "0" * 64
    path.write_text(json.dumps(run))
    with pytest.raises(A.ArchiveError) as caught:
        A.restore(root, run["run_id"])
    assert "PRIVATE9" not in str(caught.value) and "홍길동" not in str(caught.value)


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("failure", ["write", "timeout", "exit", "gzip", "footer", "rename", "manifest", "cursor"])
def test_storage_failures_are_reported_without_advancing_unpublished_cursor(tmp_path, monkeypatch, capsys, initial, failure):
    root = tmp_path / "archive"
    if not initial:
        assert collect(root, NOW - timedelta(hours=1)) == 0
    else:
        with A.locked(root):
            pass
    before = A.cursor(root)
    real_replace, real_json = os.replace, A._atomic_json
    real_close = gzip.GzipFile.close

    def error(*args, **kwargs):
        raise OSError(SECRET.decode())

    def bad_rename(source, destination):
        if Path(source).name.startswith(".pending-"):
            error()
        return real_replace(source, destination)

    def bad_json(path, value):
        if path.name == ("manifest.json" if failure == "manifest" else "cursor.json"):
            error()
        return real_json(path, value)

    def bad_close(file):
        was_open = file.fileobj is not None
        real_close(file)
        if was_open and file.mode == gzip.WRITE:
            error()

    if failure == "gzip":
        monkeypatch.setattr(gzip.GzipFile, "write", error)
    elif failure == "footer":
        monkeypatch.setattr(gzip.GzipFile, "close", bad_close)
    elif failure == "rename":
        monkeypatch.setattr(os, "replace", bad_rename)
    elif failure in ("manifest", "cursor"):
        monkeypatch.setattr(A, "_atomic_json", bad_json)
    assert A.collect(root, runner=Docker(fail=failure), now=NOW) == 2
    assert A.cursor(root) == before
    assert len(A.failures(root)) == 1
    for run in A.runs(root):
        A.restore(root, run["run_id"])
    output = capsys.readouterr()
    assert "PRIVATE9" not in output.out + output.err and "홍길동" not in output.out + output.err


@pytest.mark.parametrize("failure", ["exit", "timeout", "write"])
def test_first_collection_failure_then_retry(tmp_path, failure):
    root = tmp_path / "archive"
    assert A.collect(root, runner=Docker(fail=failure), now=NOW) == 2
    assert A.cursor(root) is None and A.runs(root) == [] and len(A.failures(root)) == 1
    assert collect(root, NOW + timedelta(hours=1)) == 0
    assert len(A.runs(root)) == 1


def test_initialization_fsync_failures_return_two_and_allow_retry(tmp_path, monkeypatch):
    real = os.fsync
    for fail_at in range(1, 7):
        root = tmp_path / str(fail_at)
        seen = 0

        def failing(fd):
            nonlocal seen
            seen += 1
            if seen == fail_at:
                raise OSError("injected")
            real(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", failing)
            assert A.collect(root, runner=Docker(), now=NOW) == 2
        assert len(A.failures(root)) == 1
        assert A.cursor(root) is None
        assert collect(root) == 0


def test_expiry_protects_active_unpublished_files_and_foreign_run_files(tmp_path):
    root = tmp_path / "archive"
    old = NOW - timedelta(days=20)
    assert collect(root, old) == 0
    [run] = A.runs(root)
    note = manifest_path(root, run).parent / "operator-note"
    note.write_text("keep")

    class DuringCapture(Docker):
        def __call__(self, args, **kwargs):
            if args[1] == "logs":
                kwargs["stdout"].write(b"active")
                kwargs["stdout"].flush()
                partials = list((root / "runs").glob(".pending-*/stdout.raw"))
                with pytest.raises(A.ArchiveError):
                    A.expire(root, now=NOW)
                assert [p.read_bytes() for p in partials] == [b"active"]
            return super().__call__(args, **kwargs)

    assert A.collect(root, runner=DuringCapture(), now=NOW) == 0
    assert A.expire(root, now=NOW)["deleted"] == [run["run_id"]]
    assert note.read_text() == "keep"


def test_expiry_uses_manifest_until_and_keeps_unrecorded_partial(tmp_path):
    root = tmp_path / "archive"
    assert collect(root, NOW - timedelta(days=20)) == 0
    assert collect(root) == 0
    unknown = root / "runs" / (".pending-" + "d" * 32)
    unknown.mkdir()
    partial = unknown / "stdout.raw"
    partial.write_bytes(b"keep crash evidence")
    os.utime(partial, (0, 0))
    for path in root.rglob("*.gz"):
        os.utime(path, (0, 0))
    assert len(A.expire(root, now=NOW)["deleted"]) == 1
    assert partial.read_bytes() == b"keep crash evidence"
    assert len(A.runs(root)) == 1


def test_expiry_removes_only_recorded_failed_payloads_after_14_days(tmp_path):
    root = tmp_path / "archive"
    old = NOW - timedelta(days=20)
    assert A.collect(root, runner=Docker(old, fail="exit"), now=old) == 2
    partial = next((root / "runs").glob(".pending-*"))
    note = partial / "note"
    note.write_text("keep")
    assert collect(root) == 0
    A.expire(root, now=NOW)
    assert not (partial / "stdout.raw").exists()
    assert note.read_text() == "keep" and A.failures(root) == []


def test_parent_symlink_is_canonicalized_but_leaf_is_refused(tmp_path):
    # resolve the pytest temporary base first, independent of macOS /var aliasing.
    base = tmp_path.resolve()
    real = base / "real"
    real.mkdir()
    alias = base / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert collect(alias / "archive") == 0
    assert A.cursor(alias / "archive") == A.cursor(real / "archive")
    with A.locked(real / "archive"):
        assert collect(alias / "archive") == 3
    leaf = base / "leaf"
    leaf.symlink_to(real / "archive")
    with pytest.raises(A.ArchiveError):
        collect(leaf)
    assert A.expire(alias / "archive", now=NOW)["deleted"] == []


def test_parent_alias_to_observation_archive_is_refused(tmp_path):
    observation = tmp_path / "investing-observe"
    observation.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(observation)
    with pytest.raises(A.ArchiveError):
        collect(alias / "archive")
    assert list(observation.iterdir()) == []


@pytest.mark.parametrize("guard", ["archive_limit", "free_space", "measurement_failed"])
def test_cycle_expires_even_when_collection_guard_blocks(tmp_path, monkeypatch, guard):
    root = tmp_path / "archive"
    assert collect(root, NOW - timedelta(days=20)) == 0
    assert collect(root, NOW - timedelta(days=1)) == 0
    docker = Docker()
    options = {}
    if guard == "archive_limit":
        options["max_archive_bytes"] = 1
    elif guard == "free_space":
        monkeypatch.setattr(A.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    else:
        def unreadable(_):
            raise OSError("unavailable")
        monkeypatch.setattr(A, "_usage", unreadable)
    result = A.cycle(root, runner=docker, now=NOW, **options)
    assert result["guard"] == guard and docker.calls == []
    assert len(result["expiry"]["deleted"]) == 1 and len(A.runs(root)) == 1


def test_capacity_includes_pending_raw_files_and_peak_reserve(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    assert collect(root) == 0
    original = A._usage(root)
    partial = root / "runs" / ".pending-orphan"
    partial.mkdir()
    (partial / "stdout.raw").write_bytes(b"x" * 20000)
    assert A._usage(root) >= original + 20000
    docker = Docker()
    monkeypatch.setattr(A.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    result = A.cycle(root, runner=docker, now=NOW, max_archive_bytes=original + A.PEAK_RESERVE_BYTES + 10000)
    assert result["guard"] == "archive_limit" and docker.calls == []


def test_check_missing_and_pending_slots_manual_runs_do_not_fill_slots(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    monkeypatch.setattr(A.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    assert A.cycle(root, runner=Docker(), now=NOW)["collection_code"] == 0
    assert collect(root, NOW + timedelta(hours=1)) == 0  # manual: not a cron success
    future = NOW + timedelta(hours=2, minutes=5)
    result = A.check(root, now=future, first_slot=NOW)
    assert result["missed_slots"] == [A._iso(NOW + timedelta(hours=1))]
    assert result["pending_slots"] == [A._iso(NOW + timedelta(hours=2))]
    assert result["last_success"] == A._iso(NOW + timedelta(hours=1))
    assert not result["ok"]
    # At the delayed lower boundary, yesterday's previously pending slot is checked.
    delayed = A.check(root, now=NOW + timedelta(days=1, minutes=5), first_slot=NOW)
    assert A._iso(NOW + timedelta(hours=1)) in delayed["missed_slots"]


def test_check_detects_corruption_and_stale_success(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    monkeypatch.setattr(A.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    assert A.cycle(root, runner=Docker(), now=NOW)["collection_code"] == 0
    [run] = A.runs(root)
    assert A.check(root, now=NOW + timedelta(minutes=11), first_slot=NOW)["ok"]
    assert A.check(root, now=NOW + timedelta(hours=2), first_slot=NOW)["stale"]
    (manifest_path(root, run).parent / "stderr.gz").write_bytes(b"broken")
    result = A.check(root, now=NOW + timedelta(minutes=11), first_slot=NOW)
    assert result["invalid_runs"] == [run["run_id"]]
    assert result["last_success"] is None and not result["ok"]


def test_default_runner_drains_both_streams_and_limits_bytes(tmp_path, monkeypatch):
    real_popen = subprocess.Popen

    def child(args, **kwargs):
        return real_popen([sys.executable, "-B", "-c",
                           "import os; os.write(1,b'o'*100000); os.write(2,b'e'*100000)"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", child)
    with (tmp_path / "out").open("wb") as out, (tmp_path / "err").open("wb") as err:
        result = A._run_docker(["docker", "logs"], stdout=out, stderr=err, timeout=5)
    assert result.returncode == 0 and result.stdout is None and result.stderr is None
    assert (tmp_path / "out").read_bytes() == b"o" * 100000
    assert (tmp_path / "err").read_bytes() == b"e" * 100000
    monkeypatch.setattr(A, "MAX_CAPTURE_BYTES", 1000)
    with pytest.raises(A.ArchiveError, match="capture limit"):
        A._run_docker(["docker", "logs"], timeout=5)


def test_default_runner_kills_and_reaps_timeout(monkeypatch):
    real_popen = subprocess.Popen
    children = []

    def child(args, **kwargs):
        process = real_popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", child)
    with pytest.raises(A.ArchiveError, match="timeout"):
        A._run_docker(["docker", "logs"], timeout=0.1)
    assert children[0].returncode is not None


def test_private_permissions_and_content_free_cli_verification(tmp_path, capsys):
    root = tmp_path / "archive"
    assert collect(root) == 0
    for path in [root, *root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    [run] = A.runs(root)
    assert A.main(["verify", "--root", str(root), "--run-id", run["run_id"]]) == 0
    captured = capsys.readouterr()
    assert '"verified": true' in captured.out
    assert "PRIVATE9" not in captured.out + captured.err and "홍길동" not in captured.out + captured.err


@pytest.mark.parametrize("change", [None, "id", "restart", "failure"])
def test_pre_switch_rechecks_identity_after_verified_collection(tmp_path, change):
    class SwitchingDocker(Docker):
        def __call__(self, args, **kwargs):
            result = super().__call__(args, **kwargs)
            if args[1] == "inspect" and len(self.calls) > 2:
                payload = json.loads(result.stdout)
                if change == "id":
                    payload["Id"] = "d" * 64
                elif change == "restart":
                    payload["StartedAt"] = A._iso(NOW)
                elif change == "failure":
                    result.returncode = 1
                result.stdout = json.dumps(payload).encode()
            return result

    root = tmp_path / "archive"
    docker = SwitchingDocker()
    result = A.pre_switch(root, runner=docker, now=NOW)
    assert result["ready"] == (change is None)
    assert result["collection_code"] == (0 if change is None else 2)
    assert [c[1] for c in docker.calls] == ["inspect", "logs", "inspect"]
    assert len(A.runs(root)) == 1  # the completed archive remains valid even if the recheck fails
    assert len(A.failures(root)) == (0 if change is None else 1)


def test_failed_replacement_attempt_is_still_the_first_observation(tmp_path):
    class ReplacedDocker(Docker):
        def __call__(self, args, **kwargs):
            result = super().__call__(args, **kwargs)
            if args[1] == "inspect":
                value = json.loads(result.stdout)
                value["Id"] = "e" * 64
                result.stdout = json.dumps(value).encode()
            return result

    root = tmp_path / "archive"
    assert collect(root) == 0
    seen = NOW + timedelta(hours=1)
    assert A.collect(root, runner=ReplacedDocker(seen, fail="exit"), now=seen) == 2
    tail = [A._iso(NOW), A._iso(seen)]
    assert A.coverage(root)["unarchived_tail"] == [tail]
    later = NOW + timedelta(hours=2)
    assert A.collect(root, runner=ReplacedDocker(later), now=later) == 0
    assert A.runs(root)[-1]["unarchived_tail"] == tail
    assert A.coverage(root)["unarchived_tail"] == [tail]


def test_empty_window_between_nonempty_runs_stays_unknown(tmp_path):
    root = tmp_path / "archive"
    assert collect(root) == 0
    empty_at = NOW + timedelta(hours=1)
    assert A.collect(root, runner=Docker(empty_at, empty=True), now=empty_at) == 0
    assert collect(root, NOW + timedelta(hours=2)) == 0
    report = A.coverage(root)
    assert [A._iso(NOW - timedelta(minutes=10)), A._iso(empty_at)] in report["unknown"]
    assert report["gaps"] == [[A._iso(empty_at), A._iso(NOW + timedelta(hours=2, seconds=-1))]]


@pytest.mark.parametrize("damage", ["empty_cursor", "wrong_cursor", "corrupt_anchor"])
def test_invalid_cursor_anchor_stops_collection_and_deletion(tmp_path, damage):
    root = tmp_path / "archive"
    assert collect(root, NOW - timedelta(days=20)) == 0
    assert collect(root, NOW - timedelta(days=19)) == 0
    saved = A.cursor(root)
    if damage == "empty_cursor":
        (root / "cursor.json").write_text("{}")
    elif damage == "wrong_cursor":
        saved["until"] = A._iso(NOW)
        (root / "cursor.json").write_text(json.dumps(saved))
    else:
        (root / "runs" / saved["run_id"] / "stdout.gz").write_bytes(b"broken")
    before = A.cursor(root)
    docker = Docker()
    assert A.collect(root, runner=docker, now=NOW) == 2
    assert docker.calls == [] and A.cursor(root) == before
    with pytest.raises(A.ArchiveError):
        A.expire(root, now=NOW)
    assert len(A.runs(root)) == 2 and A.deletions(root) == []


def test_dry_run_cannot_accidentally_collect(tmp_path):
    root = tmp_path / "archive"
    with pytest.raises(SystemExit) as caught:
        A.main(["collect", "--dry-run", "--root", str(root)])
    assert caught.value.code == 2 and not root.exists()


def expiry_history(tmp_path):
    root = tmp_path / "archive"
    assert collect(root, NOW - timedelta(days=20)) == 0
    [old] = A.runs(root)
    assert collect(root, NOW - timedelta(days=1)) == 0
    return root, old


@pytest.mark.parametrize("expiry_fails", [True, False])
def test_cycle_cli_collects_when_failure_or_resolution_history_cannot_be_saved(
        tmp_path, monkeypatch, capsys, expiry_fails):
    root, old = expiry_history(tmp_path)
    path = manifest_path(root, old)
    original = path.read_bytes()
    path.write_bytes(b"not json")
    if not expiry_fails:
        with pytest.raises(A.ArchiveError):
            A.expire(root, now=NOW)
        path.write_bytes(original)
    real_json, real_cycle = A._atomic_json, A.cycle
    attempts = []

    def unavailable(path, value):
        if path.name == A.EXPIRY_STATE:
            attempts.append(value["expiry_failed"])
            raise PermissionError(SECRET.decode())
        return real_json(path, value)

    docker = Docker()
    monkeypatch.setattr(A, "_atomic_json", unavailable)
    monkeypatch.setattr(A, "cycle", lambda root, **kwargs: real_cycle(root, runner=docker, now=NOW, **kwargs))
    assert A.main(["cycle", "--root", str(root)]) == 5
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert attempts == [expiry_fails]  # exercises failure AND successful-clear writes
    assert result["collection_code"] == 0 and result["exit_code"] == 5
    assert result["expiry_failed"] is True and result["expiry_history_failed"] is True
    assert (result["expiry"] is None) is expiry_fails
    assert [call[1] for call in docker.calls] == ["inspect", "logs"]
    assert A.cursor(root)["until"] == A._iso(NOW)
    assert captured.err == "ARCHIVE_EXPIRY_HISTORY_UNAVAILABLE\n"
    assert "PRIVATE9" not in captured.out + captured.err and "홍길동" not in captured.out + captured.err
    if not expiry_fails:
        assert result["expiry"]["deleted"] == [old["run_id"]]
        assert A.check(root, first_slot=NOW, now=NOW + timedelta(minutes=11))["expiry_failed"] is True


@pytest.mark.parametrize("error", [RuntimeError, KeyError, TypeError, ValueError, AssertionError])
def test_cycle_does_not_swallow_programming_errors_in_expiry(tmp_path, monkeypatch, error):
    root, _ = expiry_history(tmp_path)
    docker = Docker()

    def bug(*args):
        raise error("programming error")

    monkeypatch.setattr(A, "_expire_locked", bug)
    with pytest.raises(error, match="programming error"):
        A.cycle(root, runner=docker, now=NOW)
    assert docker.calls == [] and not (root / A.EXPIRY_STATE).exists()


@pytest.mark.parametrize("expiry_fails", [True, False])
def test_expiry_history_does_not_swallow_programming_errors(tmp_path, monkeypatch, expiry_fails):
    root, old = expiry_history(tmp_path)
    if expiry_fails:
        manifest_path(root, old).write_bytes(b"not json")
    real_json = A._atomic_json

    def bug(path, value):
        if path.name == A.EXPIRY_STATE:
            raise RuntimeError("history bug")
        return real_json(path, value)

    monkeypatch.setattr(A, "_atomic_json", bug)
    with pytest.raises(RuntimeError, match="history bug"):
        A.cycle(root, runner=Docker(), now=NOW)


def test_unlink_refusal_is_injected_even_as_root_and_pending_deletion_is_retried(tmp_path, monkeypatch):
    root, old = expiry_history(tmp_path)
    directory = manifest_path(root, old).parent
    real_unlink = os.unlink
    refused = []

    def unlink(path, *args, **kwargs):
        if Path(path) == directory / "stdout.gz":
            refused.append(path)
            raise PermissionError("injected unlink refusal")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        result = A.cycle(root, runner=Docker(), now=NOW)
    assert len(refused) > 0
    assert result["expiry_failed"] is True and result["exit_code"] == 5 and result["collection_code"] == 0
    assert not (directory / "manifest.json").exists() and (directory / "stdout.gz").exists()
    report = A.check(root, first_slot=NOW, now=NOW + timedelta(minutes=11))
    assert report["pending_deletions"] == [old["run_id"]] and report["ok"] is False
    # A preview preserves both the intent and the unresolved state.
    preview = A.expire(root, now=NOW, dry_run=True)
    assert preview["candidates"] == [old["run_id"]] and preview["expiry_failed"] is True
    later = NOW + timedelta(hours=1)
    result = A.cycle(root, runner=Docker(later), now=later)
    assert result["exit_code"] == 0 and result["expiry"]["deleted"] == [old["run_id"]]
    assert not directory.exists()
    report = A.check(root, first_slot=NOW, now=later + timedelta(minutes=11))
    assert report["pending_deletions"] == [] and report["expiry_failed"] is False and report["ok"] is True


@pytest.mark.parametrize("refuse", [False, True])
def test_expiry_intent_deletion_and_resolution_are_durable_before_collection(tmp_path, monkeypatch, refuse):
    root, old = expiry_history(tmp_path)
    directory = manifest_path(root, old).parent
    events, paths = [], {}
    real_open, real_sync, real_replace, real_unlink = os.open, os.fsync, os.replace, os.unlink

    def opening(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        paths[fd] = Path(path)
        return fd

    def syncing(fd):
        real_sync(fd)
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(("sync", kind, paths[fd]))

    def replacing(source, target):
        value = json.loads(Path(source).read_bytes()) if Path(target).suffix == ".json" else None
        real_replace(source, target)
        events.append(("rename", Path(source), Path(target), value))

    def unlink(path, *args, **kwargs):
        path = Path(path)
        if path.parent == directory:
            if refuse and path.name == "stdout.gz":
                events.append(("refused", path))
                raise PermissionError("injected")
            real_unlink(path, *args, **kwargs)
            events.append(("unlink", path))
            return
        return real_unlink(path, *args, **kwargs)

    class ObservedDocker(Docker):
        def __call__(self, args, **kwargs):
            events.append(("docker", args[1]))
            return super().__call__(args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "fsync", syncing)
    monkeypatch.setattr(os, "replace", replacing)
    monkeypatch.setattr(os, "unlink", unlink)
    result = A.cycle(root, runner=ObservedDocker(), now=NOW)
    assert result["collection_code"] == 0 and result["expiry_failed"] is refuse

    def durable_json(target, key, value):
        rename = next(i for i, e in enumerate(events)
                      if e[0] == "rename" and e[2] == target and e[3].get(key) == value)
        file_sync = next(i for i, e in enumerate(events) if e == ("sync", "file", events[rename][1]))
        dir_sync = next(i for i, e in enumerate(events)
                        if i > rename and e == ("sync", "directory", target.parent))
        assert file_sync < rename < dir_sync
        return file_sync, dir_sync

    audit = root / "deletions" / (old["run_id"] + ".json")
    _, intent_sync = durable_json(audit, "status", "pending")
    first_unlink = next(i for i, e in enumerate(events) if e[0] == "unlink")
    assert intent_sync < first_unlink
    state_file_sync, state_dir_sync = durable_json(root / A.EXPIRY_STATE, "expiry_failed", refuse)
    if refuse:
        refused = next(i for i, e in enumerate(events) if e[0] == "refused")
        assert first_unlink < refused < state_file_sync
        assert not any(e[0] == "rename" and e[2] == audit and e[3]["status"] == "deleted" for e in events)
    else:
        last_unlink = max(i for i, e in enumerate(events) if e[0] == "unlink")
        payload_dir_sync = next(i for i, e in enumerate(events) if e == ("sync", "directory", directory))
        removal_sync = next(i for i, e in enumerate(events) if e == ("sync", "directory", root / "runs"))
        deleted_file_sync, deleted_dir_sync = durable_json(audit, "status", "deleted")
        assert last_unlink < payload_dir_sync < removal_sync < deleted_file_sync < deleted_dir_sync < state_file_sync
    assert state_dir_sync < next(i for i, e in enumerate(events) if e == ("docker", "inspect"))


@pytest.mark.parametrize("damage", ["until", "streams", "stream", "first", "scheduled_slot", "unarchived_tail"])
def test_malformed_run_fields_are_normalized_at_read_boundary_and_check_continues(tmp_path, damage):
    root, old = expiry_history(tmp_path)
    path = manifest_path(root, old)
    value = json.loads(path.read_bytes())
    if damage == "stream":
        value["streams"]["stdout"] = None
    elif damage == "first":
        value["streams"]["stdout"]["first"] = {}
    else:
        value[damage] = {}
    path.write_text(json.dumps(value))
    with pytest.raises(A.ArchiveError):
        A.runs(root)
    result = A.cycle(root, runner=Docker(), now=NOW)
    assert result["expiry_failed"] is True and result["collection_code"] == 0
    report = A.check(root, first_slot=NOW, now=NOW + timedelta(minutes=11))
    assert report["invalid_runs"] == [old["run_id"]] and report["last_success"] == A._iso(NOW)
    assert report["missed_slots"] == [] and report["ok"] is False


@pytest.mark.parametrize("replacement", [None, {}, {"interval": []}])
def test_malformed_failure_history_stays_strict_on_container_replacement(tmp_path, replacement):
    root, _ = expiry_history(tmp_path)
    assert A.collect(root, runner=Docker(fail="exit"), now=NOW - timedelta(hours=1)) == 2
    [failed] = A.failures(root)
    failed["replacement"] = replacement
    (root / "failures" / (failed["run_id"] + ".json")).write_text(json.dumps(failed))
    with pytest.raises(A.ArchiveError):
        A.failures(root)
    assert A.cycle(root, runner=Docker(), now=NOW)["exit_code"] == 5
    before = A.cursor(root)

    class ChangedDocker(Docker):
        def __call__(self, args, **kwargs):
            result = super().__call__(args, **kwargs)
            if args[1] == "inspect":
                value = json.loads(result.stdout)
                value["Id"] = "e" * 64
                result.stdout = json.dumps(value).encode()
            return result

    docker = ChangedDocker()
    result = A.cycle(root, runner=docker, now=NOW + timedelta(hours=1))
    assert result["exit_code"] == 2 and result["expiry_failed"] is True
    assert [c[1] for c in docker.calls] == ["inspect"] and A.cursor(root) == before


def test_cycle_lock_contention_never_attempts_expiry_or_collection(tmp_path):
    root, old = expiry_history(tmp_path)
    docker = Docker()
    with A.locked(root):
        with pytest.raises(A.ArchiveError):
            A.cycle(root, runner=docker, now=NOW)
        assert A.main(["cycle", "--root", str(root)]) == 3
    assert docker.calls == [] and manifest_path(root, old).exists()
    assert not (root / A.EXPIRY_STATE).exists()


@pytest.mark.parametrize("expiry_fails", [True, False])
def test_manual_expire_cli_reports_unavailable_failure_and_resolution_history(
        tmp_path, monkeypatch, capsys, expiry_fails):
    root, old = expiry_history(tmp_path)
    path = manifest_path(root, old)
    original = path.read_bytes()
    path.write_bytes(b"not json")
    if not expiry_fails:
        with pytest.raises(A.ArchiveError):
            A.expire(root, now=NOW)
        path.write_bytes(original)
    before = A.cursor(root)
    real_json, real_expire = A._atomic_json, A.expire
    attempts = []

    def unavailable(path, value):
        if path.name == A.EXPIRY_STATE:
            attempts.append(value["expiry_failed"])
            raise OSError(SECRET.decode())
        return real_json(path, value)

    monkeypatch.setattr(A, "_atomic_json", unavailable)
    monkeypatch.setattr(A, "expire", lambda root, **kwargs: real_expire(root, now=NOW, **kwargs))
    assert A.main(["expire", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert attempts == [expiry_fails]
    assert result["expiry_history_failed"] is True and result["expiry_failed"] is True
    assert captured.err == ("ARCHIVE_EXPIRY_HISTORY_UNAVAILABLE\n"
                            + ("ARCHIVE_OPERATION_FAILED\n" if expiry_fails else ""))
    assert A.cursor(root) == before


def test_retry_finishes_pending_audit_when_the_run_directory_is_already_gone(tmp_path, monkeypatch):
    root, old = expiry_history(tmp_path)
    directory = manifest_path(root, old).parent
    real_json = A._atomic_json
    refused = []

    def unavailable(path, value):
        if path.parent == root / "deletions" and value["status"] == "deleted":
            refused.append(path)
            raise OSError("deleted audit unavailable")
        return real_json(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(A, "_atomic_json", unavailable)
        result = A.cycle(root, runner=Docker(), now=NOW)
    assert refused and result["expiry_failed"] is True and result["collection_code"] == 0
    assert not directory.exists()
    assert A.check(root, first_slot=NOW, now=NOW)["pending_deletions"] == [old["run_id"]]
    result = A.expire(root, now=NOW)
    assert result["deleted"] == [old["run_id"]] and result["expiry_failed"] is False
    report = A.check(root, first_slot=NOW, now=NOW)
    assert report["pending_deletions"] == [] and report["ok"] is True


def test_unreadable_expiry_state_cannot_make_check_healthy(tmp_path):
    root = tmp_path / "archive"
    assert A.cycle(root, runner=Docker(), now=NOW)["exit_code"] == 0
    (root / A.EXPIRY_STATE).write_bytes(b"not json")
    report = A.check(root, first_slot=NOW, now=NOW + timedelta(minutes=11))
    assert report["expiry_failed"] is True and report["ok"] is False
    assert report["invalid_runs"] == [] and report["missed_slots"] == [] and report["stale"] is False
    assert A.expire(root, now=NOW)["expiry_failed"] is False
    assert A.check(root, first_slot=NOW, now=NOW + timedelta(minutes=11))["ok"] is True
