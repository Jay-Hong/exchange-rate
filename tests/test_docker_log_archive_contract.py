"""Docker log archive contract (B v1) — written before the implementation, read-only for the implementer.

Plan: `design/log-retention/plan_v1.md` + `plan_v2_delta.md` (Claude·Codex agreement 2026-09-21).
Scope: B v1 keeps what reached **Docker** stdout/stderr. Tier C child file logs are out of scope.

What is pinned is behaviour, not layout: no file or directory name inside the archive root is part of
the contract. The API is `collect`, `runs`, `restore`, `coverage`, `cursor`, `failures`, `expire`,
`deletions`, `locked` and `ArchiveError`. `runner` is the only Docker boundary, called like
`subprocess.run` (bytes, no `text=True`); `stdout`/`stderr` may be file objects the runner writes to, or
the runner returns the bytes. `inspect` must yield the JSON object `{"Id": ..., "StartedAt": ...}` on stdout.
`logs` output lines start with the RFC3339 timestamp that `docker logs --timestamps` prints.
Times the API returns are UTC strings: `datetime.isoformat()` with `+00:00` written as `Z` (the tests compare
them as strings, and `coverage` intervals are `[start, end]` lists of such strings).

Not pinned here, pinned by the implementer's own tests (agreed 2026-09-21): the durable ORDER of writes (gzip footer →
file fsync → name/directory → manifest → cursor; a crash-free test cannot see it), refusal of a manifest whose raw-hash
disagrees with correct compressed bytes, and the server wrapper (guards, expiry independent of guards, slot checks).
"""

import gzip
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import docker_log_archive as A

UTC = timezone.utc
CID, OTHER = "a" * 64, "b" * 64
NOW = datetime(2026, 9, 23, 1, 0, 0, tzinfo=UTC)
STARTED = "2026-08-01T00:00:00Z"      # before every collection time used below


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def line(ts, text=b'{"message": "ok"}'):
    return iso(ts).encode() + b" " + text + b"\n"


def _arg(args, name):
    return datetime.fromisoformat(args[args.index(name) + 1].replace("Z", "+00:00"))


class Docker:
    def __init__(self, stdout=b"", stderr=b"", *, cid=CID, started=STARTED, failure=None):
        self.stdout, self.stderr = stdout, stderr
        self.cid, self.started, self.failure = cid, started, failure
        self.calls = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        assert not kwargs.get("text") and not kwargs.get("universal_newlines"), "bytes, not text"
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, (int, float)) and 0 < timeout < math.inf, "every Docker call has a finite timeout"
        if args[1] == "inspect":
            fmt = args[args.index("--format") + 1]
            assert "{{json .Id}}" in fmt and "{{json .State.StartedAt}}" in fmt, "inspect must print JSON"

            if self.failure == "inspect_exit":
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
            payload = json.dumps({"Id": self.cid, "StartedAt": self.started}).encode()
            return SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
        assert args[1] == "logs"
        assert "--timestamps" in args, "coverage is computed from Docker's own timestamps"
        assert args[-1] == self.cid, "logs must use the inspected ID, never the name"
        if self.failure == "logs_timeout":
            raise subprocess.TimeoutExpired(args, 1)
        out, err = self.stdout, self.stderr
        if hasattr(kwargs.get("stdout"), "write"):
            kwargs["stdout"].write(out)
            out = None
        if hasattr(kwargs.get("stderr"), "write"):
            kwargs["stderr"].write(err)
            err = None
        return SimpleNamespace(returncode=1 if self.failure == "logs_exit" else 0, stdout=out, stderr=err)

    def logs_calls(self):
        return [c for c in self.calls if c[1] == "logs"]


def root_of(tmp_path):
    return tmp_path / "docker-archive"


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ── collection ───────────────────────────────────────────────────────────────

def test_both_streams_are_kept_byte_for_byte(tmp_path):
    out = line(NOW - timedelta(minutes=5)) + b"\xff\xfe not utf-8\n"
    err = line(NOW - timedelta(minutes=4), b"stderr line")
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(out, err), now=NOW) == 0
    [run] = A.runs(root)
    assert A.restore(root, run["run_id"]) == (out, err)
    assert run["streams"]["stdout"]["raw_sha256"] == sha(out)
    assert run["streams"]["stderr"]["raw_sha256"] == sha(err)


def test_logs_use_the_inspected_id(tmp_path):
    docker = Docker(line(NOW - timedelta(minutes=1)))
    assert A.collect(root_of(tmp_path), container="exchange-rate-app", runner=docker, now=NOW) == 0
    assert docker.logs_calls()[0][-1] == CID


@pytest.mark.parametrize("started, since", [
    (NOW - timedelta(hours=2), NOW - timedelta(hours=2)),     # a young container: from its start
    (NOW - timedelta(days=30), NOW - timedelta(hours=24)),    # an old one: 24 hours back
])
def test_the_first_run_starts_at_the_later_of_24h_back_and_container_start(tmp_path, started, since):
    docker = Docker(line(NOW - timedelta(minutes=1)), started=iso(started))
    root = root_of(tmp_path)
    assert A.collect(root, runner=docker, now=NOW) == 0
    call = docker.logs_calls()[0]
    assert _arg(call, "--since") == since
    assert _arg(call, "--until") == NOW
    [run] = A.runs(root)
    assert (run["since"], run["until"]) == (iso(since), iso(NOW))


def test_the_next_run_overlaps_the_previous_by_ten_minutes(tmp_path):
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=1))), now=NOW) == 0
    later = NOW + timedelta(hours=1)
    docker = Docker(line(later - timedelta(minutes=1)))
    assert A.collect(root, runner=docker, now=later) == 0
    assert _arg(docker.logs_calls()[0], "--since") == NOW - timedelta(minutes=10)
    assert A.cursor(root)["until"] == iso(later)


@pytest.mark.parametrize("failure", ["inspect_exit", "logs_exit", "logs_timeout"])
def test_a_docker_failure_moves_nothing_and_is_recorded(tmp_path, failure):
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=1))), now=NOW) == 0
    before = (A.cursor(root), [r["run_id"] for r in A.runs(root)], len(A.failures(root)))
    later = NOW + timedelta(hours=1)
    assert A.collect(root, runner=Docker(line(later), failure=failure), now=later) == 2
    assert (A.cursor(root), [r["run_id"] for r in A.runs(root)]) == before[:2]
    failures = A.failures(root)
    assert len(failures) == before[2] + 1
    assert isinstance(failures[-1].get("stage"), str) and failures[-1]["stage"]


def test_no_fsync_failure_leaves_the_cursor_ahead_of_the_archive(tmp_path, monkeypatch):
    """Whatever durable step fails, the cursor never points past data that `restore` can return.

    The implementation's own fsync count is measured first; a durable writer must have some.
    Then each fsync in turn is made to fail.
    """
    real = os.fsync
    later = NOW + timedelta(hours=1)
    count = {"n": 0}

    def counting(fd):
        count["n"] += 1
        return real(fd)

    probe = tmp_path / "probe"
    assert A.collect(probe, runner=Docker(line(NOW - timedelta(minutes=1))), now=NOW) == 0
    monkeypatch.setattr(os, "fsync", counting)
    assert A.collect(probe, runner=Docker(line(later - timedelta(minutes=1))), now=later) == 0
    monkeypatch.setattr(os, "fsync", real)
    assert count["n"] >= 2, "a durable archive fsyncs its data and its cursor"

    for k in range(1, count["n"] + 1):
        root = tmp_path / f"k{k}"
        assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=1))), now=NOW) == 0
        seen = {"n": 0}

        def failing(fd, k=k):
            seen["n"] += 1
            if seen["n"] == k:
                raise OSError("injected fsync failure")
            return real(fd)

        before_cursor, before_failures = A.cursor(root), len(A.failures(root))
        monkeypatch.setattr(os, "fsync", failing)
        try:
            code = A.collect(root, runner=Docker(line(later - timedelta(minutes=1))), now=later)
        finally:
            monkeypatch.setattr(os, "fsync", real)
        assert code in (0, 2), f"fsync #{k}: collect reports, it does not raise"
        runs = A.runs(root)
        for run in runs:
            A.restore(root, run["run_id"])                     # every listed run is complete
        newest = max(run["until"] for run in runs)
        cursor = A.cursor(root)
        assert cursor["until"] <= newest, f"cursor ahead of archive after fsync #{k}"
        if code == 0:
            assert newest == iso(later)
        else:
            assert cursor == before_cursor or cursor["until"] == iso(later) == newest, f"fsync #{k}"
            assert len(A.failures(root)) == before_failures + 1, f"fsync #{k}: the failure is recorded"


def _gz_files(root):
    return sorted(p for p in root.rglob("*") if p.is_file() and p.read_bytes()[:2] == b"\x1f\x8b")


@pytest.mark.parametrize("which", [0, 1])
def test_restore_refuses_a_tampered_stream_one_at_a_time(tmp_path, which):
    # Both streams are archived; corrupt only one, so a check of just one stream cannot pass.
    secret = b"api_key=SECRETVALUE9"
    out = line(NOW - timedelta(minutes=1), secret) * 50
    err = line(NOW - timedelta(minutes=2), secret) * 50
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(out, err), now=NOW) == 0
    [run] = A.runs(root)
    compressed = _gz_files(root)
    assert len(compressed) == 2, "each stream is its own gzip"
    data = bytearray(compressed[which].read_bytes())
    data[-5] ^= 0xFF                                           # inside the gzip trailer (CRC32)
    compressed[which].write_bytes(bytes(data))
    with pytest.raises(A.ArchiveError) as caught:
        A.restore(root, run["run_id"])
    assert "SECRETVALUE9" not in str(caught.value)


def test_restore_refuses_stored_bytes_that_differ_even_if_they_decompress_the_same(tmp_path):
    # Flip the gzip header's OS byte: the stream still decompresses to identical bytes, so only a check of
    # the stored bytes against the recorded hash can notice. A CRC-breaking tamper would be caught by gzip itself.
    root = root_of(tmp_path)
    out = line(NOW - timedelta(minutes=1)) * 10
    assert A.collect(root, runner=Docker(out), now=NOW) == 0
    [run] = A.runs(root)
    for path in _gz_files(root)[:1]:                          # one stream only
        data = bytearray(path.read_bytes())
        data[9] = (data[9] + 1) % 256
        assert gzip.decompress(bytes(data)) == gzip.decompress(path.read_bytes())
        path.write_bytes(bytes(data))
    with pytest.raises(A.ArchiveError):
        A.restore(root, run["run_id"])


def test_compressed_streams_decompress_to_the_recorded_hash(tmp_path):
    out = line(NOW - timedelta(minutes=1)) * 100
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(out), now=NOW) == 0
    [run] = A.runs(root)
    compressed = [p.read_bytes() for p in _gz_files(root)]
    assert sha(out) in {sha(gzip.decompress(c)) for c in compressed}
    assert run["streams"]["stdout"]["gz_sha256"] in {sha(c) for c in compressed}


def test_lock_held_means_no_docker_call_and_nothing_written(tmp_path):
    root = root_of(tmp_path)
    docker = Docker(line(NOW - timedelta(minutes=1)))
    with A.locked(root):
        assert A.collect(root, runner=docker, now=NOW) == 3
    assert docker.calls == []
    assert A.runs(root) == [] and A.cursor(root) is None


# ── replacement and restart ──────────────────────────────────────────────────

@pytest.mark.parametrize("new_start", [
    NOW + timedelta(minutes=30),
    NOW - timedelta(minutes=10),        # the new container started while the old one was still being read
])
def test_a_replaced_container_is_recorded_and_its_tail_is_not_called_archived(tmp_path, new_start):
    # With no observed stop time for the old container, its unarchived tail runs from its last archived `until`
    # to the collection that first saw the new ID — the new container's start is no bound on the old one's end.
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=1))), now=NOW) == 0
    later = NOW + timedelta(hours=1)
    docker = Docker(line(new_start + timedelta(seconds=5)), cid=OTHER, started=iso(new_start))
    assert A.collect(root, runner=docker, now=later) == 0
    assert _arg(docker.logs_calls()[0], "--since") == new_start      # the old ID cannot be read from the new one
    newest = A.runs(root)[-1]
    assert (newest["container_id"], newest["container_changed"], newest["restarted"]) == (OTHER, True, False)
    assert [iso(NOW), iso(later)] in A.coverage(root)["unarchived_tail"]


def test_a_restart_with_the_same_id_is_recorded(tmp_path):
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=1))), now=NOW) == 0
    restart = NOW + timedelta(minutes=30)
    later = NOW + timedelta(hours=1)
    docker = Docker(line(restart + timedelta(seconds=5)), started=iso(restart))
    assert A.collect(root, runner=docker, now=later) == 0
    newest = A.runs(root)[-1]
    assert (newest["container_changed"], newest["restarted"]) == (False, True)


# ── coverage ─────────────────────────────────────────────────────────────────

def test_an_empty_run_is_unknown_not_archived_and_not_a_gap(tmp_path):
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(b"", b""), now=NOW) == 0
    [run] = A.runs(root)
    cover = A.coverage(root)
    assert [run["since"], run["until"]] in cover["unknown"]
    assert cover["archived"] == [] and cover["gaps"] == []


def test_a_first_line_one_second_after_the_overlap_start_is_not_a_gap(tmp_path):
    # Codex counterexample: previous until = T, next requested since = T-10min, first line at T-10min+1s.
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=30)) + line(NOW - timedelta(seconds=1))),
                     now=NOW, ) == 0
    later = NOW + timedelta(hours=1)
    first = NOW - timedelta(minutes=10) + timedelta(seconds=1)
    assert A.collect(root, runner=Docker(line(first) + line(later - timedelta(seconds=1))), now=later) == 0
    assert A.coverage(root)["gaps"] == []


def test_a_hole_between_archived_stretches_is_a_gap(tmp_path):
    root = root_of(tmp_path)
    assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=30))), now=NOW) == 0
    later = NOW + timedelta(hours=1)
    first = NOW + timedelta(minutes=5)                         # Docker returned nothing earlier than this
    assert A.collect(root, runner=Docker(line(first) + line(later - timedelta(seconds=1))), now=later) == 0
    assert A.coverage(root)["gaps"] == [[iso(NOW), iso(first)]]


# ── expiry ───────────────────────────────────────────────────────────────────

def _history(root, ends):
    for end in ends:
        assert A.collect(root, runner=Docker(line(end - timedelta(minutes=1))), now=end) == 0


def test_expire_deletes_only_runs_ending_more_than_14_days_ago(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=20), NOW - timedelta(days=14), NOW - timedelta(days=1)])
    old = A.runs(root)[0]["run_id"]
    result = A.expire(root, now=NOW)
    assert result["deleted"] == [old]
    assert [r["until"] for r in A.runs(root)] == [iso(NOW - timedelta(days=14)), iso(NOW - timedelta(days=1))]
    assert [d["run_id"] for d in A.deletions(root)] == [old]


def test_expire_leaves_foreign_files_inside_the_root_alone(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=20), NOW - timedelta(days=1)])
    (root / "operator-notes.txt").write_text("keep")
    assert len(A.expire(root, now=NOW)["deleted"]) == 1
    assert (root / "operator-notes.txt").read_text() == "keep"


def test_a_symlinked_root_is_refused(tmp_path):
    real = root_of(tmp_path)
    _history(real, [NOW - timedelta(days=20)])
    link = tmp_path / "archive-link"
    link.symlink_to(real)
    with pytest.raises(A.ArchiveError):
        A.expire(link, now=NOW)
    with pytest.raises(A.ArchiveError):
        A.collect(link, runner=Docker(line(NOW)), now=NOW)
    assert len(A.runs(real)) == 1


def test_a_lock_held_by_another_process_blocks_collect_and_expire(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=20), NOW - timedelta(days=1)])
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; from scripts import docker_log_archive as A\n"
                               "ctx = A.locked(sys.argv[1]); ctx.__enter__(); print('held', flush=True); time.sleep(60)",
         str(root)], cwd=repo, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        docker = Docker(line(NOW))
        assert A.collect(root, runner=docker, now=NOW + timedelta(hours=1)) == 3
        assert docker.calls == []
        with pytest.raises(A.ArchiveError):
            A.expire(root, now=NOW)
        assert len(A.runs(root)) == 2
    finally:
        holder.kill()
        holder.wait()


def test_expire_never_deletes_the_run_the_cursor_rests_on(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=30), NOW - timedelta(days=29)])
    result = A.expire(root, now=NOW)
    assert len(result["deleted"]) == 1
    [kept] = A.runs(root)
    assert kept["until"] == A.cursor(root)["until"]


def test_dry_run_lists_candidates_and_deletes_nothing(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=20), NOW - timedelta(days=1)])
    before = [r["run_id"] for r in A.runs(root)]
    result = A.expire(root, now=NOW, dry_run=True)
    assert result["candidates"] == before[:1] and result["deleted"] == []
    assert [r["run_id"] for r in A.runs(root)] == before and A.deletions(root) == []


def test_expired_failures_are_removed_too(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=21)])
    assert A.collect(root, runner=Docker(failure="logs_exit"), now=NOW - timedelta(days=20)) == 2
    _history(root, [NOW - timedelta(days=1)])
    assert len(A.failures(root)) == 1
    A.expire(root, now=NOW)
    assert A.failures(root) == []


def test_expire_refuses_a_root_it_did_not_create(tmp_path):
    foreign = tmp_path / "somewhere"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("x")
    with pytest.raises(A.ArchiveError):
        A.expire(foreign, now=NOW)
    assert (foreign / "keep.txt").exists()


@pytest.mark.parametrize("layout", ["is_observation", "contains_observation"])
def test_the_observation_archive_is_never_a_root(tmp_path, layout):
    observation = tmp_path / "logs" / "investing-observe"
    observation.mkdir(parents=True)
    (observation / "evidence").write_text("keep")
    root = observation if layout == "is_observation" else tmp_path / "logs"
    with pytest.raises(A.ArchiveError):
        A.collect(root, runner=Docker(line(NOW)), now=NOW)
    with pytest.raises(A.ArchiveError):
        A.expire(root, now=NOW)
    assert (observation / "evidence").read_text() == "keep"


def test_an_observation_archive_appearing_inside_an_archive_root_stops_both(tmp_path):
    # The root was ours (marker present) before the observation directory appeared inside it.
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=20), NOW - timedelta(days=1)])
    (root / "investing-observe").mkdir()
    ((root / "investing-observe") / "evidence").write_text("keep")
    with pytest.raises(A.ArchiveError):
        A.collect(root, runner=Docker(line(NOW)), now=NOW + timedelta(hours=1))
    with pytest.raises(A.ArchiveError):
        A.expire(root, now=NOW)
    assert (root / "investing-observe" / "evidence").read_text() == "keep"
    assert len(A.runs(root)) == 2


def test_a_symlink_under_the_root_stops_expiry(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=20), NOW - timedelta(days=1)])
    target = tmp_path / "elsewhere"
    target.mkdir()
    (root / "link").symlink_to(target)
    before = [r["run_id"] for r in A.runs(root)]
    with pytest.raises(A.ArchiveError):
        A.expire(root, now=NOW)
    assert [r["run_id"] for r in A.runs(root)] == before


def test_expire_waits_for_the_lock(tmp_path):
    root = root_of(tmp_path)
    _history(root, [NOW - timedelta(days=20), NOW - timedelta(days=1)])
    with A.locked(root):
        with pytest.raises(A.ArchiveError):
            A.expire(root, now=NOW)
    assert len(A.runs(root)) == 2


# ── what leaves the archive ──────────────────────────────────────────────────

def test_no_log_content_reaches_output_or_metadata(tmp_path, capsys):
    secret = b"api_key=SECRETVALUE9 person=\xed\x99\x8d\xea\xb8\xb8\xeb\x8f\x99"       # 홍길동 in UTF-8
    root = root_of(tmp_path)
    first = NOW - timedelta(days=15)
    assert A.collect(root, runner=Docker(line(first - timedelta(minutes=5), secret),
                                         line(first - timedelta(minutes=4), secret)), now=first) == 0
    for failure in ("logs_exit", "logs_timeout", "inspect_exit"):
        assert A.collect(root, runner=Docker(line(NOW - timedelta(hours=1), secret), line(NOW - timedelta(hours=1), secret),
                                             failure=failure), now=NOW - timedelta(minutes=30)) == 2
    assert A.collect(root, runner=Docker(line(NOW - timedelta(minutes=1), secret)), now=NOW) == 0
    A.expire(root, now=NOW)
    # ensure_ascii=False: with the default, 홍길동 would be \\uXXXX-escaped and this check could never fail.
    shown = json.dumps([A.runs(root), A.coverage(root), A.cursor(root), A.failures(root), A.deletions(root)],
                       ensure_ascii=False)
    captured = capsys.readouterr()
    for text in (shown, captured.out, captured.err):
        assert "SECRETVALUE9" not in text and "홍길동" not in text
