#!/usr/bin/env python3
"""Validate and aggregate archived Investing events (schema 2 and 3), without inferring DB writes."""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
import sys

if __package__:
    from .investing_observe_extract import KST, sha256, strict_json, timestamp
else:  # Direct script execution: do not import the application or its DB configuration.
    from investing_observe_extract import KST, sha256, strict_json, timestamp


START = "investing_round_started"
FX = "investing_fx_evidence"
FINISH = "investing_round_finished"
EVENTS = (START, FX, FINISH)
PAIRS = {"usd-krw", "jpy-krw", "eur-krw"}
STATES = {"valid", "missing", "unknown", "not_attempted"}
# schema_version -> judgment contract (SOURCE_HEALTH_PLAN D10). Schema 2 events predate the field and are
# mapped explicitly; an unknown combination is rejected rather than guessed. Summary-dependent metrics are
# reported per contract and never summed: /1 picks missing before unknown, /2 picks unknown before missing.
CONTRACTS = {2: "investing_range_checked/1", 3: "investing_range_checked/2"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def string(value):
    return isinstance(value, str) and bool(value)


def integer(value):
    return type(value) is int and value >= 0


def state(value, allowed):
    require(isinstance(value, dict), "state must be an object")
    require(isinstance(value.get("status"), str) and value["status"] in allowed,
            "invalid state status")
    require(string(value.get("reason")), "state reason missing")


def pair_map(value):
    require(isinstance(value, dict) and set(value) == PAIRS, "expected all three FX pairs")


def validate_record(record):
    """Reject unusable shapes before hashing, indexing, or counting any fields."""
    require(isinstance(record, dict), "record must be an object")
    timestamp(record.get("ts"))
    require(string(record.get("container_id")), "container_id missing")
    event = record.get("event")
    require(isinstance(event, dict), "event must be an object")
    version = event.get("schema_version")
    require(type(version) is int and version in CONTRACTS, "unsupported schema_version")
    if version == 2:
        require("validity_contract" not in event, "schema 2 must not carry validity_contract")
    else:
        require(event.get("validity_contract") == CONTRACTS[version], "unsupported validity_contract")
    require(event.get("source") == "investing", "invalid source")
    require(string(event.get("round_id")), "round_id missing")
    name = event.get("event")
    require(isinstance(name, str) and name in EVENTS, "unknown event")
    require("attempt_id" in event, "attempt_id missing")
    if name == FX:
        require(type(event["attempt_id"]) is int and event["attempt_id"] in (1, 2),
                "FX attempt_id must be 1 or 2")
    else:
        require(event["attempt_id"] is None, "round event attempt_id must be null")
    if name == START:
        require(event.get("format") == "lifecycle", "start format must be lifecycle")
        return
    state(event.get("execution"), {"running", "normal", "abnormal", "timeout", "cancelled"})
    if name == FINISH:
        require(event["execution"]["status"] != "running", "finished event is running")
        require(type(event["execution"].get("exception_propagated")) is bool,
                "exception_propagated must be boolean")
    require(event.get("session") in ("creating", "open", "closing", "closed"),
            "invalid session")
    require(event.get("final_db") == "not_checked", "unexpected final_db contract")
    if event.get("format") == "compact":
        require(event.get("outcome") == "all_valid", "invalid compact outcome")
        require(type(event.get("fx_attempt_id")) is int and event["fx_attempt_id"] == 1,
                "compact fx_attempt_id must be 1")
        require(name != FX or event["attempt_id"] == 1, "compact FX must be attempt 1")
        pair_map(event.get("rates"))
        require(all(type(rate) in (int, float) and math.isfinite(rate) and rate > 0
                    for rate in event["rates"].values()), "invalid compact rates")
        require(integer(event.get("writer_returned_count")), "invalid writer_returned_count")
        require(event.get("writing") == "per_currency_write_unverified", "invalid writing")
        require(event["session"] == ("closed" if name == FINISH else "open"),
                "compact session contradicts event")
        require(event["execution"]["status"] == ("normal" if name == FINISH else "running"),
                "compact execution contradicts event")
        return
    require(event.get("format") == "detail", "unsupported format")
    errors = event.get("telemetry_errors")
    require(isinstance(errors, list) and all(string(e) for e in errors),
            "telemetry_errors must be a list of strings")
    pair_map(event.get("collection_attempts"))
    attempts = event.get("attempts")
    require(isinstance(attempts, list) and len(attempts) == 2, "expected two attempts")
    by_id = {}
    for attempt in attempts:
        require(isinstance(attempt, dict), "attempt must be an object")
        aid = attempt.get("attempt_id")
        require(type(aid) is int and aid in (1, 2) and aid not in by_id, "invalid attempt ID")
        by_id[aid] = attempt
        state(attempt, {"not_reached", "unnecessary", "attempted", "succeeded", "failed",
                        "policy_skipped", "unknown"})
        if "collection" not in attempt:
            require(not errors and attempt["status"] in ("not_reached", "unnecessary")
                    and set(attempt) == {"attempt_id", "status", "reason"},
                    "invalid abbreviated attempt")
            continue
        pair_map(attempt["collection"])
        for observed in attempt["collection"].values():
            state(observed, STATES)
            if observed["status"] == "valid":
                rate = observed.get("normalized_rate")
                require(type(rate) in (int, float) and math.isfinite(rate) and rate > 0,
                        "valid observation has invalid rate")
        state(attempt.get("execution"), {"not_attempted", "running", "normal", "abnormal",
                                         "timeout", "cancelled", "unknown"})
        writer = attempt.get("writer")
        require(isinstance(writer, dict), "writer must be an object")
        require("called" in writer and (writer["called"] is None or type(writer["called"]) is bool),
                "writer called must be boolean or null")
        pairs = writer.get("input_pairs")
        require(isinstance(pairs, list) and all(isinstance(p, str) and p in PAIRS for p in pairs),
                "invalid writer input_pairs")
        require(len(set(pairs)) == len(pairs), "duplicate writer input_pairs")
        require("returned_count" in writer and (writer["returned_count"] is None
                or integer(writer["returned_count"])), "invalid writer returned_count")
        require("error_type" in writer and (writer["error_type"] is None
                or string(writer["error_type"])), "invalid writer error_type")
        if writer["called"] is False:
            require(not pairs and writer["returned_count"] is None and writer["error_type"] is None,
                    "uncalled writer contains call evidence")
    for aid in event["collection_attempts"].values():
        require(type(aid) is int and aid in by_id, "dangling collection_attempts reference")
    pair_map(event.get("writing"))
    for writing in event["writing"].values():
        state(writing, {"unknown", "not_attempted"})
        ids = writing.get("attempt_ids")
        require(isinstance(ids, list) and all(type(i) is int and i in by_id for i in ids),
                "invalid writing attempt_ids")


def contract(event):
    return CONTRACTS[event["schema_version"]]


def collection(event):
    if event["format"] == "compact":
        return {pair: {"status": "valid", "reason": "validated"} for pair in event["rates"]}
    attempts = {a["attempt_id"]: a for a in event["attempts"]}
    # The producer has already selected the strongest evidence. Never last-write-wins.
    return {pair: attempts[aid].get("collection", {}).get(
        pair, {"status": "not_attempted", "reason": "not_started"})
        for pair, aid in event["collection_attempts"].items()}


def load(paths):
    """key -> canonical payload -> variant; metadata order cannot choose a winner."""
    groups, invalid = {}, []
    for path in sorted(set(map(Path, paths))):
        with path.open("rb") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = strict_json(line)
                    validate_record(record)
                except (ValueError, UnicodeError, TypeError, OverflowError) as error:
                    invalid.append({"path": str(path), "line": number, "error": str(error)})
                    continue
                event = record["event"]
                key = (record["container_id"], event["round_id"], event["event"], event["attempt_id"])
                body = json.dumps(event, sort_keys=True, ensure_ascii=False, allow_nan=False)
                variants = groups.setdefault(key, {})
                variant = variants.setdefault(body, {"event": event, "timestamps": set()})
                variant["timestamps"].add(record["ts"])
    return groups, invalid


def load_runs(root):
    """Read the last durable phase; verify prepared artifacts before using coverage."""
    runs, invalid = [], []
    if root is None:
        return runs, invalid
    for folder in sorted((Path(root) / "runs").glob("*")):
        if not folder.is_dir():
            continue
        try:
            last = next((folder / name for name in
                         ("result.json", "prepared.json", "request.json", "started.json")
                         if (folder / name).exists()), None)
            require(last is not None, "missing run journal")
            run = strict_json(last.read_bytes())
            require(isinstance(run, dict) and run.get("schema_version") == 1, "invalid run schema")
            require(run.get("run_id") == folder.name, "run ID mismatch")
            start, end = timestamp(run.get("since")), timestamp(run.get("until"))
            require(start <= end, "reversed extraction interval")
            require(run.get("phase") in ("started", "prepared", "finished", "failed"),
                    "invalid run phase")
            require(type(run.get("container_changed")) is bool and type(run.get("restarted")) is bool,
                    "invalid lifecycle flags")
            require(integer(run.get("parse_failed")), "invalid parse_failed")
            previous = run.get("previous_cursor")
            if previous is not None:
                require(isinstance(previous, dict), "invalid previous_cursor")
                require(timestamp(previous.get("until")) <= end, "future previous_cursor")
            if run["phase"] == "finished":
                require(run.get("ok") is True and run.get("cursor_committed") is True,
                        "invalid finished run")
                prepared = strict_json((folder / "prepared.json").read_bytes())
                require(prepared.get("phase") == "prepared" and prepared.get("run_id") == folder.name,
                        "missing prepared history")
                for field in ("since", "until", "container_id", "started_at", "coverage_since",
                              "container_changed", "restarted", "parse_failed", "artifacts"):
                    require(prepared.get(field) == run.get(field), f"prepared/result mismatch: {field}")
                artifacts = run.get("artifacts")
                require(isinstance(artifacts, dict) and len(artifacts) >= 3, "missing artifacts")
                for relative, digest in artifacts.items():
                    path = (Path(root) / relative).resolve()
                    require(path.is_relative_to(Path(root).resolve()), "artifact outside archive")
                    require(sha256(path) == digest, f"artifact checksum mismatch: {relative}")
                if run.get("coverage_since") is not None:
                    require(start <= timestamp(run["coverage_since"]) <= end, "invalid coverage interval")
            runs.append(run)
        except (ValueError, UnicodeError, TypeError, OSError, AttributeError) as error:
            invalid.append({"path": str(folder), "error": str(error)})
    return runs, invalid


def day_bounds(day):
    start = datetime.combine(date.fromisoformat(day), time(), KST)
    return start, start + timedelta(days=1)


def coverage(day, runs, invalid, bad_events, conflicted):
    start, end = day_bounds(day)
    intervals, reasons = [], set()
    if not runs:
        reasons.add("no_extraction_history")
    if invalid:
        reasons.add("invalid_extraction_history")
    if bad_events:
        reasons.add("invalid_event_input_date_unknown")
    if conflicted:
        reasons.add("conflicting_events")
    for run in runs:
        since, until = timestamp(run["since"]), timestamp(run["until"])
        if since >= end or until <= start:
            continue
        if run["phase"] != "finished":
            reasons.add("failed_or_incomplete_extraction")
        else:
            if run["parse_failed"]:
                reasons.add("parse_rejects")
            if run.get("coverage_since"):
                lo, hi = max(start, timestamp(run["coverage_since"])), min(end, until)
                if lo < hi:
                    intervals.append((lo, hi))
        if run["container_changed"] or run["restarted"]:
            previous = run.get("previous_cursor") or {}
            # A replacement can lose the old container's tail. Do not flag the
            # entire cold-start lookback when the transition happened today.
            risk_start = timestamp(previous["until"]) if previous.get("until") else since
            if risk_start < end and until > start:
                reasons.add("container_changed" if run["container_changed"] else "container_restarted")
    cursor, covered = start, 0.0
    for lo, hi in sorted(intervals):
        if hi > max(cursor, lo):
            covered += (hi - max(cursor, lo)).total_seconds()
        cursor = max(cursor, hi)
    if covered < (end - start).total_seconds():
        reasons.add("uncovered_extraction_window")
    return {"partial": bool(reasons), "reasons": sorted(reasons),
            "covered_seconds": covered, "scope": "archived_log_windows_only"}


def counter_rows(counter, fields):
    return [dict(zip(fields, key), count=count) for key, count in sorted(counter.items(), key=str)]


def summarize(groups, invalid, runs, bad_runs, days):
    rounds, formats, conflicts = defaultdict(list), defaultdict(Counter), []
    event_counts = Counter()
    for key, variants in sorted(groups.items(), key=lambda item: str(item[0])):
        cid, rid, name, aid = key
        ordered = [variants[body] for body in sorted(variants)]
        for variant in ordered:
            ts = min(map(timestamp, variant["timestamps"]))
            variant = {**variant, "time": ts, "conflict": len(variants) > 1}
            rounds[(cid, rid)].append(variant)
            day = ts.astimezone(KST).date().isoformat()
            event_counts[day] += 1
            if len(variants) == 1:
                formats[(day, name)][variant["event"]["format"]] += 1
        if len(variants) > 1:
            conflicts.append({"key": list(key), "variants": [
                {"event": v["event"], "timestamps": sorted(v["timestamps"])} for v in ordered]})
    by_day = defaultdict(list)
    conflict_days = set()
    mixed_rounds = 0
    for identity, variants in sorted(rounds.items()):
        starts = [v for v in variants if v["event"]["event"] == START]
        at = min(v["time"] for v in (starts or variants))
        day = at.astimezone(KST).date().isoformat()
        if len({contract(v["event"]) for v in variants}) > 1:
            # One process emits one contract per round. A mix has no authoritative meaning.
            mixed_rounds += 1
            variants = [{**v, "conflict": True} for v in variants]
        by_day[day].append((identity, variants, bool(starts)))
        if any(v["conflict"] for v in variants):
            conflict_days.add(day)
            conflict_days.update(v["time"].astimezone(KST).date().isoformat() for v in variants)
    result = {}
    for day in days:
        status, reasons, cross = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
        execution = Counter()
        attempt_calls, returned, round_calls, telemetry = Counter(), Counter(), Counter(), Counter()
        missing = Counter({"start_only": 0, "finish_only": 0, "fx_only": 0})
        excluded, provisional, finalized = 0, 0, 0
        for identity, variants, has_start in by_day[day]:
            provisional += not has_start
            names = {v["event"]["event"] for v in variants}
            missing["start_only"] += START in names and FINISH not in names
            missing["finish_only"] += FINISH in names and START not in names
            missing["fx_only"] += START not in names and FINISH not in names
            if any(v["conflict"] for v in variants):
                excluded += 1
                continue  # A conflict has no authoritative winner, even if repeated.
            finished = [v["event"] for v in variants if v["event"]["event"] == FINISH]
            if not finished:
                continue
            finalized += 1
            event = finished[0]
            kind = contract(event)
            observed = collection(event)
            status[kind].update(item["status"] for item in observed.values())
            reasons[kind].update(item["reason"] for item in observed.values())
            ex = event["execution"]
            execution[(ex["status"], ex["reason"], ex["exception_propagated"])] += 1
            cross[kind][(ex["status"], ex["reason"],
                         sum(o["status"] == "valid" for o in observed.values()))] += 1
            if event["format"] == "compact":
                writers = [{"called": True, "returned_count": event["writer_returned_count"]}]
            else:
                writers = [a["writer"] for a in event["attempts"] if "writer" in a]
                telemetry.update(set(event["telemetry_errors"]))
            round_calls["unknown" if any(w["called"] is None for w in writers)
                        else str(sum(w["called"] is True for w in writers))] += 1
            for writer in writers:
                attempt_calls[str(writer["called"]).lower()] += 1
                if writer["returned_count"] is not None:
                    returned[str(writer["returned_count"])] += 1
        result[day] = {
            "event_variants": event_counts[day], "rounds_observed": len(by_day[day]),
            "finalized_unambiguous_rounds": finalized, "conflicted_rounds_excluded": excluded,
            "provisional_date_rounds": provisional, "lifecycle": dict(missing),
            "format_by_event": {name: dict(formats[(day, name)]) for name in EVENTS},
            "by_contract": {kind: {
                "currency_status": dict(status[kind]), "currency_reason": dict(reasons[kind]),
                "execution_valid_currencies_rounds": counter_rows(cross[kind], ("status", "reason", "valid")),
            } for kind in sorted(status)},
            "execution_rounds": counter_rows(execution, ("status", "reason", "exception_propagated")),
            "writer_called_attempts": dict(attempt_calls),
            "writer_returned_count_attempts": dict(returned),
            "writer_call_count_rounds": dict(round_calls), "telemetry_error_rounds": dict(telemetry),
            "coverage": coverage(day, runs, bad_runs, invalid, day in conflict_days),
            "both_lifecycle_events_absent_rounds": "not_identifiable",
        }
    return {"schema_version": 2, "timezone": "Asia/Seoul", "days": result,
            "contract_mixed_rounds": mixed_rounds,
            "conflict_keys": len(conflicts), "conflict_extra_variants": sum(len(c["variants"]) - 1 for c in conflicts),
            "conflicts": conflicts, "invalid_events": invalid, "invalid_runs": bad_runs,
            "script_sha256": {name: sha256(Path(__file__).with_name(name)) for name in
                              ("investing_observe_extract.py", "investing_observe_aggregate.py")},
            "metric_scope": "finalized unambiguous rounds; writer returned counts are per attempt, not DB writes; "
                            "by_contract metrics must not be summed across contracts"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path, help="explicit archived event JSONL files")
    parser.add_argument("--archive", type=Path, help="extractor output directory (all dates + run journals)")
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat, help="inclusive KST date")
    args = parser.parse_args(argv)
    if args.start_date > args.end_date:
        parser.error("start-date must not exceed end-date")
    if not args.files and args.archive is None:
        parser.error("provide --archive or event files")
    try:
        # Snapshot history first. Otherwise an extractor finishing between the
        # event scan and history scan could contribute coverage but no events.
        runs, bad_runs = load_runs(args.archive)
        paths = args.files + (sorted((args.archive / "events").glob("*/*.jsonl")) if args.archive else [])
        groups, invalid = load(paths)
        days = [(args.start_date + timedelta(days=i)).isoformat()
                for i in range((args.end_date - args.start_date).days + 1)]
        result = summarize(groups, invalid, runs, bad_runs, days)
    except OSError as error:
        print(f"AGGREGATE_INPUT_FAILED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 1 if (invalid or bad_runs or result["conflict_keys"] or
                 any(day["coverage"]["partial"] for day in result["days"].values())) else 0


if __name__ == "__main__":
    sys.exit(main())
