"""Observation regressions: actual reports, fake Docker, isolated archive files."""

from copy import deepcopy
from datetime import timedelta
import fcntl
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.crawlers.investing_report import InvestingReport
from scripts import investing_observe_aggregate as aggregate
from scripts import investing_observe_extract as extract


CID = "a" * 64
OTHER_CID = "b" * 64
NOW = extract.timestamp("2026-09-17T01:00:00Z")
TS = "2026-09-17T00:50:00.123456789Z"
STARTED = "2026-09-01T00:00:00Z"
RATES = {"usd-krw": 1350.0, "jpy-krw": 900.0, "eur-krw": 1500.0}


class Logger:
    def __init__(self):
        self.events = []

    def info(self, value):
        self.events.append(json.loads(value))


def report_events(retry=False, writer_count=0, cooldown=False):
    logger = Logger()
    report = InvestingReport(logger, RATES)
    report.round_id = "round-1"
    report.emit(aggregate.START)
    report.session_state("open")
    if cooldown:
        report.cooldown()
    else:
        report.start_attempt(1)
        for pair, rate in RATES.items():
            report.observation(1, pair, text=str(rate), rate=rate)
        report.writer_started(1, RATES)
        report.writer_finished(1, count=writer_count)
        report.emit(aggregate.FX, 1)
        report.finish_attempt(1, RuntimeError("DXY failed") if retry else None)
        if retry:
            report.start_attempt(2)
            report.finish_attempt(2, TimeoutError("second URL"))
    report.session_state("closed")
    report.finish()
    return logger.events


def row(event, ts=TS, cid=CID):
    return {"ts": ts, "container_id": cid, "event": event}


def docker_line(event, ts=TS):
    return ts.encode() + b" " + extract.json_bytes({"message": json.dumps(event)})


class Docker:
    def __init__(self, data=b"", *, cid=CID, failure=None):
        self.data, self.cid, self.failure = data, cid, failure
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[1] == "inspect":
            if self.failure == "inspect_timeout":
                raise subprocess.TimeoutExpired(args, 180)
            if self.failure == "inspect_bad":
                return SimpleNamespace(returncode=0, stdout=b"[]")
            return SimpleNamespace(returncode=1 if self.failure == "inspect_exit" else 0,
                                   stdout=extract.json_bytes({"Id": self.cid, "StartedAt": STARTED}))
        assert args[1] == "logs"
        assert args[-1] == self.cid  # A name here would allow replacement contamination.
        kwargs["stdout"].write(self.data)
        if self.failure == "logs_timeout":
            raise subprocess.TimeoutExpired(args, 180)
        return SimpleNamespace(returncode=1 if self.failure == "logs_exit" else 0)


def archive(tmp_path, data=None, **kwargs):
    root = tmp_path / "archive"
    docker = Docker(data if data is not None else b"".join(map(docker_line, report_events())), **kwargs)
    return root, docker


def results(root):
    return [json.loads(p.read_bytes()) for p in sorted((root / "runs").glob("*/result.json"))]


def summarize_rows(tmp_path, rows, days=("2026-09-17",)):
    path = tmp_path / "input.jsonl"
    path.write_bytes(b"".join(map(extract.json_bytes, rows)))
    groups, invalid = aggregate.load([path])
    return aggregate.summarize(groups, invalid, [], [], days)


def test_p1_selected_first_valid_survives_second_timeout(tmp_path):
    events = report_events(retry=True)
    assert events[-1]["collection_attempts"] == dict.fromkeys(RATES, 1)
    summary = summarize_rows(tmp_path, list(map(row, events)))["days"]["2026-09-17"]
    assert summary["currency_status"] == {"valid": 3}
    assert summary["execution_valid_currencies_rounds"] == [
        {"status": "timeout", "reason": "timeout", "valid": 3, "count": 1}]
    assert summary["writer_returned_count_attempts"] == {"0": 1}


def test_p2_conflicting_variants_are_order_and_repetition_independent(tmp_path):
    a = report_events()[-1]
    b = deepcopy(a)
    b["rates"]["usd-krw"] = 1360.0
    snapshots = []
    for sequence in ((a, b), (b, a), (a, b, b), (b, a, b), (b, b, a)):
        summary = summarize_rows(tmp_path, list(map(row, sequence)))
        assert summary["conflict_keys"] == summary["conflict_extra_variants"] == 1
        assert summary["days"]["2026-09-17"]["currency_status"] == {}
        assert summary["days"]["2026-09-17"]["conflicted_rounds_excluded"] == 1
        snapshots.append(summary)
    assert all(s == snapshots[0] for s in snapshots)


def test_duplicate_payload_and_same_round_id_across_containers(tmp_path):
    event = report_events()[-1]
    rows = [row(event), row(event), row(event, cid=OTHER_CID)]
    summary = summarize_rows(tmp_path, rows)["days"]["2026-09-17"]
    assert summary["rounds_observed"] == 2
    assert summary["currency_status"] == {"valid": 6}


def test_p3_replacement_after_inspect_stays_pinned_to_old_id(tmp_path):
    root, docker = archive(tmp_path)
    assert extract.extract(root, runner=docker, now=NOW) == 0
    assert len(docker.calls) == 2
    assert docker.calls[0][-1] == "exchange-rate-app"
    assert docker.calls[1][-1] == CID
    assert all(r["container_id"] == CID for p in (root / "events").glob("*/*.jsonl")
               for r in map(json.loads, p.read_text().splitlines()))
    assert json.loads((root / "cursor.json").read_bytes())["started_at"] == STARTED
    # If the pinned old container vanishes, fail; never retry using the new name.
    docker.failure = "logs_exit"
    before = (root / "cursor.json").read_bytes()
    assert extract.extract(root, runner=docker, now=NOW + timedelta(hours=1)) == 2
    assert (root / "cursor.json").read_bytes() == before


def test_p4_malformed_original_is_durable_before_cursor_moves(tmp_path):
    raw = b'2026-09-17T00:50:00Z {"message":"{\\"event\\":\\"investing_broken\\""}\xff\n'
    root, docker = archive(tmp_path, raw)
    assert extract.extract(root, runner=docker, now=NOW) == 1
    result, = results(root)
    folder = root / "runs" / result["run_id"]
    assert (folder / "stdout.raw").read_bytes() == raw
    assert json.loads((folder / "rejects.jsonl").read_bytes())["raw_line"] == 1
    assert result["parse_failed"] == 1
    assert (folder / "prepared.json").exists()
    assert (root / "cursor.json").exists()


@pytest.mark.parametrize("stage", ["rejects_write", "events_write", "history_prepare", "cursor_write"])
def test_p4_p5_storage_failures_keep_old_cursor_and_record_stage(tmp_path, monkeypatch, stage):
    root, docker = archive(tmp_path)
    assert extract.extract(root, runner=docker, now=NOW) == 0
    before = (root / "cursor.json").read_bytes()
    original = extract.atomic_bytes

    def fail(path, data):
        matches = {"rejects_write": path.name == "rejects.jsonl",
                   "events_write": "events" in path.parts,
                   "history_prepare": path.name == "prepared.json",
                   "cursor_write": path.name == "cursor.json"}
        if matches[stage]:
            raise OSError("injected disk failure")
        original(path, data)

    monkeypatch.setattr(extract, "atomic_bytes", fail)
    assert extract.extract(root, runner=docker, now=NOW + timedelta(minutes=1)) == 2
    assert (root / "cursor.json").read_bytes() == before
    failed, = [r for r in results(root) if r["phase"] == "failed"]
    assert failed["stage"] == stage
    assert failed["ok"] is False


@pytest.mark.parametrize("failure,stage", [
    ("inspect_exit", "inspect"), ("inspect_timeout", "inspect"), ("inspect_bad", "inspect"),
    ("logs_exit", "raw_capture"), ("logs_timeout", "raw_capture"),
])
def test_p5_docker_failures_have_journal_and_no_cursor(tmp_path, failure, stage):
    root, docker = archive(tmp_path, failure=failure)
    assert extract.extract(root, runner=docker, now=NOW) == 2
    result, = results(root)
    assert result["stage"] == stage and result["ok"] is False
    assert not (root / "cursor.json").exists()
    if stage == "raw_capture":
        assert (root / "runs" / result["run_id"] / "stdout.raw").read_bytes() == docker.data


def test_p5_corrupt_cursor_is_not_silently_reset(tmp_path):
    root, docker = archive(tmp_path)
    root.mkdir()
    (root / "cursor.json").write_bytes(b"{broken")
    assert extract.extract(root, runner=docker, now=NOW) == 2
    assert docker.calls == []
    assert results(root)[0]["stage"] == "cursor_read"
    assert (root / "cursor.json").read_bytes() == b"{broken"


def test_p5_lock_loser_is_recorded_without_docker_access(tmp_path):
    root, docker = archive(tmp_path)
    root.mkdir()
    with (root / "extract.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert extract.extract(root, runner=docker, now=NOW) == 3
    assert not docker.calls
    assert results(root)[0]["stage"] == "lock"


def test_p5_prepared_history_is_fsynced_before_cursor_replace(tmp_path, monkeypatch):
    root, docker = archive(tmp_path)
    original = extract.atomic_json
    stages = []

    def record(path, value):
        if path.name == "cursor.json":
            folder = root / "runs" / value["run_id"]
            prepared = json.loads((folder / "prepared.json").read_bytes())
            assert prepared["phase"] == "prepared"
            assert prepared["artifacts"]
        original(path, value)
        stages.append(path.name)

    monkeypatch.setattr(extract, "atomic_json", record)
    assert extract.extract(root, runner=docker, now=NOW) == 0
    assert stages.index("prepared.json") < stages.index("cursor.json") < stages.index("result.json")


def test_p5_result_write_failure_leaves_prepared_history_and_partial(tmp_path, monkeypatch, capsys):
    root, docker = archive(tmp_path)
    original = extract.atomic_json

    def fail(path, value):
        if path.name == "result.json":
            raise OSError("history disk unavailable")
        original(path, value)

    monkeypatch.setattr(extract, "atomic_json", fail)
    assert extract.extract(root, runner=docker, now=NOW) == 2
    assert "EXTRACT_HISTORY_FAILED" in capsys.readouterr().err
    runs, bad = aggregate.load_runs(root)
    assert not bad and runs[0]["phase"] == "prepared"
    assert "failed_or_incomplete_extraction" in aggregate.coverage("2026-09-17", runs, [], [], False)["reasons"]


def test_p4_raw_write_failure_never_advances_cursor(tmp_path, monkeypatch):
    root, docker = archive(tmp_path)
    original = Path.open

    def fail(path, *args, **kwargs):
        if path.name == "stdout.raw":
            raise OSError("raw storage unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    assert extract.extract(root, runner=docker, now=NOW) == 2
    assert not (root / "cursor.json").exists()
    assert results(root)[0]["stage"] == "raw_capture"


def test_p5_storage_unavailable_reports_stderr_without_docker(tmp_path, monkeypatch, capsys):
    root, docker = archive(tmp_path)

    def fail(path, value):
        raise OSError("no space")

    monkeypatch.setattr(extract, "atomic_json", fail)
    assert extract.extract(root, runner=docker, now=NOW) == 2
    assert not docker.calls
    assert "EXTRACT_HISTORY_FAILED" in capsys.readouterr().err


def test_atomic_json_fsyncs_file_and_directory_around_replace(tmp_path, monkeypatch):
    calls = []
    original_sync, original_replace = extract.os.fsync, extract.os.replace

    def sync(fd):
        calls.append("sync")
        original_sync(fd)

    def replace(src, dst):
        calls.append("replace")
        original_replace(src, dst)

    monkeypatch.setattr(extract.os, "fsync", sync)
    monkeypatch.setattr(extract.os, "replace", replace)
    extract.atomic_json(tmp_path / "value.json", {"durable": True})
    assert calls == ["sync", "replace", "sync"]


def test_p6_event_date_partitions_and_cross_midnight_round(tmp_path):
    events = report_events()
    times = ["2026-09-16T14:59:59Z", "2026-09-16T15:00:00Z", "2026-09-16T15:00:01Z"]
    root, docker = archive(tmp_path, b"".join(docker_line(e, t) for e, t in zip(events, times)))
    assert extract.extract(root, runner=docker, now=NOW) == 0
    assert sorted(p.name for p in (root / "events").iterdir()) == ["2026-09-16", "2026-09-17"]
    groups, invalid = aggregate.load((root / "events").glob("*/*.jsonl"))
    runs, bad = aggregate.load_runs(root)
    assert not invalid and not bad
    summary = aggregate.summarize(groups, invalid, runs, bad, ["2026-09-16", "2026-09-17"])
    assert summary["days"]["2026-09-16"]["currency_status"] == {"valid": 3}
    assert summary["days"]["2026-09-17"]["rounds_observed"] == 0
    assert summary["days"]["2026-09-17"]["event_variants"] == 2
    assert all(day["coverage"]["partial"] for day in summary["days"].values())


def coverage_run(since, until, **changes):
    return {"since": since, "until": until, "coverage_since": since,
            "phase": "finished", "parse_failed": 0, "container_changed": False,
            "restarted": False, **changes}


def test_p6_coverage_merges_overlaps_and_flags_gaps_empty_days_and_replacement():
    start, noon, end = "2026-09-16T15:00:00Z", "2026-09-17T03:00:00Z", "2026-09-17T15:00:00Z"
    runs = [coverage_run(start, noon), coverage_run(noon, end)]
    complete = aggregate.coverage("2026-09-17", runs, [], [], False)
    assert complete["partial"] is False and complete["covered_seconds"] == 86400
    assert aggregate.coverage("2026-09-17", runs[:1], [], [], False)["partial"] is True
    assert aggregate.coverage("2026-09-18", runs, [], [], False)["partial"] is True
    runs[1]["container_changed"] = True
    runs[1]["previous_cursor"] = {"until": noon}
    assert "container_changed" in aggregate.coverage("2026-09-17", runs, [], [], False)["reasons"]
    assert "container_changed" not in aggregate.coverage("2026-09-16", runs, [], [], False)["reasons"]


def test_p6_failure_ending_at_midnight_does_not_taint_following_day():
    midnight = "2026-09-16T15:00:00Z"
    failed = coverage_run("2026-09-16T14:00:00Z", midnight, phase="failed")
    full = coverage_run(midnight, "2026-09-17T15:00:00Z")
    assert aggregate.coverage("2026-09-17", [failed, full], [], [], False)["partial"] is False


def test_p6_complete_archived_window_with_no_reports_cannot_count_missing_rounds(tmp_path, capsys):
    root, docker = archive(tmp_path, b'2026-09-16T15:00:00Z {"message":"other log"}\n')
    end = extract.timestamp("2026-09-17T15:00:00Z")
    assert extract.extract(root, runner=docker, now=end) == 0
    assert aggregate.main(["--archive", str(root), "--start-date", "2026-09-17",
                           "--end-date", "2026-09-17"]) == 0
    summary = json.loads(capsys.readouterr().out)["days"]["2026-09-17"]
    assert summary["coverage"]["partial"] is False
    assert summary["rounds_observed"] == 0
    assert summary["both_lifecycle_events_absent_rounds"] == "not_identifiable"


def test_extraction_finishing_during_aggregate_cannot_add_coverage_without_events(tmp_path, monkeypatch, capsys):
    day_start = "2026-09-16T15:00:00Z"
    root, docker = archive(tmp_path, b"".join(docker_line(e, day_start) for e in report_events()))
    original = aggregate.load_runs

    def finish_and_load(path):
        assert extract.extract(root, runner=docker, now=extract.timestamp("2026-09-17T15:00:00Z")) == 0
        return original(path)

    monkeypatch.setattr(aggregate, "load_runs", finish_and_load)
    assert aggregate.main(["--archive", str(root), "--start-date", "2026-09-17",
                           "--end-date", "2026-09-17"]) == 0
    summary = json.loads(capsys.readouterr().out)["days"]["2026-09-17"]
    assert summary["coverage"]["partial"] is False
    assert summary["currency_status"] == {"valid": 3}


def test_writer_counts_are_attempts_not_rounds_and_skip_uncalled_returns(tmp_path):
    events = report_events(retry=True)
    second = events[-1]["attempts"][1]
    second["writer"].update(called=True, input_pairs=list(RATES), returned_count=2)
    summary = summarize_rows(tmp_path, list(map(row, events)))["days"]["2026-09-17"]
    assert summary["writer_returned_count_attempts"] == {"0": 1, "2": 1}
    assert summary["writer_call_count_rounds"] == {"2": 1}
    assert summary["finalized_unambiguous_rounds"] == 1
    assert summary["writer_called_attempts"] == {"true": 2}


def test_valid_abbreviated_attempt_and_cooldown(tmp_path):
    events = report_events(cooldown=True)
    summary = summarize_rows(tmp_path, list(map(row, events)))["days"]["2026-09-17"]
    assert summary["currency_status"] == {"not_attempted": 3}
    assert summary["writer_returned_count_attempts"] == {}
    logger = Logger()
    report = InvestingReport(logger, RATES)
    report.finish(RuntimeError("session creation failed"))
    summary = summarize_rows(tmp_path, list(map(row, logger.events)))["days"]["2026-09-17"]
    assert summary["currency_status"] == {"not_attempted": 3}
    assert summary["provisional_date_rounds"] == 1


@pytest.mark.parametrize("mutate", [
    lambda r: [],
    lambda r: {**r, "event": []},
    lambda r: {**r, "container_id": None},
    lambda r: {**r, "ts": "2026-09-17"},
    lambda r: {**r, "event": {**r["event"], "schema_version": 1}},
    lambda r: {**r, "event": {**r["event"], "round_id": []}},
    lambda r: {**r, "event": {**r["event"], "attempt_id": True}},
    lambda r: {**r, "event": {**r["event"], "attempts": [None, None]}},
    lambda r: {**r, "event": {**r["event"], "collection_attempts": dict.fromkeys(RATES, 3)}},
    lambda r: {**r, "event": {**r["event"], "telemetry_errors": "observation"}},
])
def test_invalid_structure_is_diagnostic_not_counted_or_crash(tmp_path, mutate):
    bad = mutate(row(report_events(retry=True)[-1]))
    summary = summarize_rows(tmp_path, [bad])
    assert len(summary["invalid_events"]) == 1
    assert summary["days"]["2026-09-17"]["rounds_observed"] == 0


def test_archive_integrity_failure_and_missing_history_are_partial(tmp_path):
    root, docker = archive(tmp_path)
    assert extract.extract(root, runner=docker, now=NOW) == 0
    next((root / "runs").glob("*/stdout.raw")).write_bytes(b"truncated")
    runs, bad = aggregate.load_runs(root)
    assert runs == [] and len(bad) == 1
    assert "checksum" in bad[0]["error"]


@pytest.mark.parametrize("raw", [b'{"event":1,"event":2}', b'{"value":NaN}', b'{"value":1e999}', b'null'])
def test_strict_json_inputs_are_rejected(tmp_path, raw):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(raw + b"\n")
    groups, invalid = aggregate.load([path])
    assert not groups and len(invalid) == 1


def test_changed_container_and_restart_record_previous_boundary(tmp_path):
    root, docker = archive(tmp_path)
    assert extract.extract(root, runner=docker, now=NOW) == 0
    docker.cid = OTHER_CID
    assert extract.extract(root, runner=docker, now=NOW + timedelta(minutes=1)) == 0
    changed, = [r for r in results(root) if r["container_changed"]]
    assert changed["previous_cursor"]["until"] == NOW.isoformat()
    assert changed["since"] == (NOW + timedelta(minutes=1) - extract.COLD_START).isoformat()
    cursor = json.loads((root / "cursor.json").read_bytes())
    cursor["started_at"] = "2026-08-30T00:00:00Z"
    extract.atomic_json(root / "cursor.json", cursor)
    assert extract.extract(root, runner=docker, now=NOW + timedelta(minutes=2)) == 0
    restarted, = [r for r in results(root) if r["restarted"]]
    assert restarted["container_changed"] is False


def test_conflict_across_date_boundary_excludes_round_on_start_date(tmp_path):
    start, _, finish = report_events()
    variant = deepcopy(finish)
    variant["writer_returned_count"] = 1
    rows = [row(start, "2026-09-16T14:59:59Z"), row(finish, "2026-09-16T15:00:00Z"),
            row(variant, "2026-09-16T15:00:01Z")]
    summary = summarize_rows(tmp_path, rows, ("2026-09-16", "2026-09-17"))
    assert summary["days"]["2026-09-16"]["conflicted_rounds_excluded"] == 1
    assert all("conflicting_events" in d["coverage"]["reasons"] for d in summary["days"].values())


def test_cli_reports_empty_requested_day_and_nonzero_partial(tmp_path, capsys):
    assert aggregate.main(["--archive", str(tmp_path), "--start-date", "2026-09-17",
                           "--end-date", "2026-09-18"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert len(summary["days"]) == 2
    assert all(d["rounds_observed"] == 0 and d["coverage"]["partial"] for d in summary["days"].values())
