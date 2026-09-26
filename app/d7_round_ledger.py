"""Bounded, in-memory D7 round ledger. All clocks are supplied by callers."""

from __future__ import annotations

import copy
import hashlib
import json
import threading

from .d7_round_axes import PAIRS, REGISTRY, normalize_round_axes


REASON_ENUM = frozenset("""
validated not_started not_observed empty_or_placeholder nan_value out_of_range parse_failed selector_missing http_403
attempt_failed attempt_interrupted cooldown mibank_untrusted_window per_currency_write_unverified not_submitted_to_writer
telemetry_error validation_rejected not_instrumented evidence_incomplete v2_evidence_unconfirmed no_value unobserved
previous_attempt_succeeded value_none equal_to_last_record committed commit_outcome_unknown commit_not_reached
guard_unrecorded staging_incomplete withheld_before_writer write_mode_uninitialized write_mode_halt reason_unrecorded
report_malformed other
""".split())

_SOURCES = tuple(REGISTRY)
_MAX_TIME = 2**63 - 1 - 4260000000
_MAX_COUNT = 2**64 - 1
_MINUTE = 60000000
_CLOSE_DELAY = 4200000000
_ABSENT = object()
_COLLECTION = frozenset(("valid", "missing", "unknown", "not_attempted"))
_INVESTING_WRITE = frozenset(("unknown", "not_attempted"))
_BANK_WRITE = frozenset(("performed", "no_change_needed", "policy_blocked", "not_attempted", "unknown"))
_FIELDS = {
    ("investing", "collection"): ("status", "reason", "normalized_rate", "attempt_id", "error_type"),
    ("investing", "writing"): ("status", "reason", "attempt_ids"),
    ("bank", "collection"): ("status", "reason", "detail", "path", "attempt_id", "observation_sequences",
                             "miss_sequences", "error_type"),
    ("bank", "writing"): ("status", "reason", "detail", "path", "writer_call_id", "writer_call_ids",
                          "lost_writer_calls"),
}
_PATHS = frozenset(("official_primary", "official_secondary", "mibank"))
_JSON = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _diag(codes=(), level=None, global_=False):
    return {
        "codes": sorted(set(codes)),
        "baseline_invalidated": level in ("B", "F"),
        "coverage_error": level is not None,
        "uncertain_pairs": [] if global_ or level is None else list(PAIRS),
        "cumulative_evidence_uncertain": level == "F",
    }


def _merge_diag(dst, src):
    dst["codes"] = sorted(set(dst["codes"]) | set(src["codes"]))
    for key in ("baseline_invalidated", "coverage_error", "cumulative_evidence_uncertain"):
        dst[key] |= src[key]
    dst["uncertain_pairs"] = [p for p in PAIRS if p in dst["uncertain_pairs"] or p in src["uncertain_pairs"]]


def _strict_bytes(value, ceiling):
    if not value:
        return "invalid_argument"
    if len(value) > ceiling:
        return "envelope_string_limit"
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return "invalid_unicode"
    return "envelope_string_limit" if size > ceiling else None


def _canonical(value):
    kind = type(value)
    if value is _ABSENT:
        return ["absent"]
    if value is None:
        return ["null"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        return ["int", str(value)]
    if kind is float:
        if value != value:
            return ["float_nonfinite", "nan"]
        if value == float("inf"):
            return ["float_nonfinite", "+inf"]
        if value == -float("inf"):
            return ["float_nonfinite", "-inf"]
        return ["float", value.hex()]
    if kind is str:
        raw = value.encode("utf-8")
        if len(raw) > 256:
            return ["str_hash", str(len(raw)), hashlib.sha256(raw).hexdigest()]
        return ["str", value]
    if kind is list:
        return ["list", [_canonical(item) for item in value]]
    if kind is dict:
        return ["dict", [[key, _canonical(value[key])] for key in sorted(value)]]
    return ["unsupported"]


def _extras(value, allowed):
    return sorted(key for key in value if key not in allowed)


def _item_repr(source, axis, value):
    if type(value) is not dict:
        kind = "absent" if value is _ABSENT else "null" if value is None else "not_dict"
        return {"kind": kind, "raw": _canonical(value)}
    fields = _FIELDS[("investing" if source == "investing" else "bank", axis)]
    status = value.get("status", _ABSENT)
    allowed = _COLLECTION if axis == "collection" else _INVESTING_WRITE if source == "investing" else _BANK_WRITE
    if not value:
        kind = "empty_dict"
    elif status is _ABSENT:
        kind = "status_missing"
    elif type(status) is not str:
        kind = "status_not_str"
    elif status not in allowed:
        kind = "status_not_allowed"
    else:
        kind = "ok"
    return {"kind": kind, "fields": {field: _canonical(value.get(field, _ABSENT)) for field in fields},
            "unexpected": _extras(value, fields)}


def _axis_repr(source, axis, value):
    if type(value) is not dict:
        kind = "absent" if value is _ABSENT else "null" if value is None else "not_dict"
        return {"kind": kind, "raw": _canonical(value)}
    return {"kind": "dict", "pairs": {pair: _item_repr(source, axis, value.get(pair, _ABSENT)) for pair in PAIRS},
            "unexpected": _extras(value, PAIRS)}


def _digest_object(source, round_id, schema, contract, wall, mono, summary, telemetry):
    return {
        "digest_version": "d7-ledger/2", "source": source, "round_id": round_id,
        "report_schema": _canonical(schema), "validity_contract": _canonical(contract),
        "finished_wall": _canonical(wall), "finished_mono": _canonical(mono),
        "telemetry_error_present": telemetry,
        "summary": {
            "collection": _axis_repr(source, "collection", summary.get("collection", _ABSENT)),
            "writing": _axis_repr(source, "writing", summary.get("writing", _ABSENT)),
            "final_db": _canonical(summary.get("final_db", _ABSENT)),
            "unexpected": _extras(summary, ("collection", "writing", "final_db")),
        },
    }


def _encoded_size_and_hash(value, limit):
    size = 0
    digest = hashlib.sha256()
    for chunk in _JSON.iterencode(value):
        raw = chunk.encode("utf-8")
        size += len(raw)
        if size > limit:
            return size, None
        digest.update(raw)
    return size, digest.hexdigest()


class _InputProblem(Exception):
    def __init__(self, code, shape=False):
        self.code = code
        self.shape = shape


class CumulativeMergeFailureForTest(Exception):
    """One-shot test fault before an atomic cumulative close."""


def _check_summary(summary):
    """Check the fixed limits in traversal order without copying caller data."""
    total_keys = total_items = total_bytes = nodes = 0
    ancestors = set()

    def text_size(value, ceiling, code):
        if len(value) > ceiling:
            raise _InputProblem(code)
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise _InputProblem("invalid_unicode", True) from None
        if size > ceiling:
            raise _InputProblem(code)
        return size

    def visit(value, depth):
        nonlocal total_keys, total_items, total_bytes, nodes
        kind = type(value)
        container = kind is dict or kind is list
        if container and id(value) in ancestors:
            raise _InputProblem("cyclic_input", True)
        if depth > 6:
            raise _InputProblem("depth_limit")
        nodes += 1
        if nodes > 256:
            raise _InputProblem("nodes_limit")
        if container:
            length = len(value)
            if length > 32:
                raise _InputProblem("dict_keys_limit" if kind is dict else "list_length_limit")
            if kind is dict:
                total_keys += length
                if total_keys > 128:
                    raise _InputProblem("total_keys_limit")
                keys = list(value)
                if any(type(key) is not str for key in keys):
                    raise _InputProblem("non_string_key", True)
                # Key failure categories have a fixed priority independent of insertion order.
                if any(len(key) > 128 for key in keys):
                    raise _InputProblem("key_bytes_limit")
                key_sizes = []
                invalid_unicode = False
                too_long = False
                for key in keys:
                    try:
                        size = len(key.encode("utf-8"))
                    except UnicodeEncodeError:
                        invalid_unicode = True
                        continue
                    if size > 128:
                        too_long = True
                    key_sizes.append((key, size))
                if too_long:
                    raise _InputProblem("key_bytes_limit")
                if invalid_unicode:
                    raise _InputProblem("invalid_unicode", True)
                ancestors.add(id(value))
                try:
                    for key, size in sorted(key_sizes):
                        total_bytes += size
                        if total_bytes > 16384:
                            raise _InputProblem("total_string_bytes_limit")
                        visit(value[key], depth + 1)
                finally:
                    ancestors.remove(id(value))
            else:
                total_items += length
                if total_items > 128:
                    raise _InputProblem("total_list_items_limit")
                ancestors.add(id(value))
                try:
                    for item in value:
                        visit(item, depth + 1)
                finally:
                    ancestors.remove(id(value))
        elif kind is str:
            total_bytes += text_size(value, 4096, "string_bytes_limit")
            if total_bytes > 16384:
                raise _InputProblem("total_string_bytes_limit")
        elif kind is int and value.bit_length() > 64:
            raise _InputProblem("integer_bits_limit")

    visit(summary, 0)


def _positive(value):
    return type(value) is int and value > 0


def _nonnegative(value):
    return type(value) is int and value >= 0


def _evidence_ok(field, value):
    if value is None:
        return True
    if field == "normalized_rate":
        return type(value) in (int, float, str)
    if field in ("attempt_id",):
        return _positive(value)
    if field in ("lost_writer_calls",):
        return _nonnegative(value)
    if field in ("detail", "error_type"):
        return type(value) is str
    if field == "path":
        return type(value) is str and value in _PATHS
    if field == "writer_call_id":
        return type(value) is str and bool(value)
    if field in ("attempt_ids", "observation_sequences", "miss_sequences"):
        return type(value) is list and all(_positive(item) for item in value)
    if field == "writer_call_ids":
        return type(value) is list and all(type(item) is str and bool(item) for item in value)
    return True


def _detail_and_codes(source, schema, contract, summary, telemetry):
    clean = {"collection": {}, "writing": {}, "final_db": summary.get("final_db")}
    codes = set()
    if any(key not in ("collection", "writing", "final_db") for key in summary):
        codes.add("unexpected_keys")
    for axis in ("collection", "writing"):
        mapping = summary.get(axis)
        if type(mapping) is not dict:
            continue
        if any(key not in PAIRS for key in mapping):
            codes.add("unexpected_keys")
        fields = _FIELDS[("investing" if source == "investing" else "bank", axis)]
        allowed = _COLLECTION if axis == "collection" else _INVESTING_WRITE if source == "investing" else _BANK_WRITE
        for pair in PAIRS:
            item = mapping.get(pair)
            if type(item) is not dict:
                continue
            if any(key not in fields for key in item):
                codes.add("unexpected_keys")
            for field in fields[2:]:
                if field in item and not _evidence_ok(field, item[field]):
                    codes.add("evidence_malformed")
            status = item.get("status")
            reason = item.get("reason")
            if type(status) is not str or status not in allowed:
                clean[axis][pair] = {"status": None, "reason": None}
            else:
                if type(reason) is not str or not reason:
                    clean_reason = None
                elif reason not in REASON_ENUM:
                    clean_reason = "other"
                    codes.add("reason_other")
                    if len(reason.encode("utf-8")) > 256:
                        codes.add("reason_oversize")
                else:
                    clean_reason = reason
                clean[axis][pair] = {"status": status, "reason": clean_reason}
    if type(clean["final_db"]) is not str:
        clean["final_db"] = None
    detail = normalize_round_axes(source, schema, contract, clean, telemetry)
    if detail["diagnostics"]["malformed_axis_items"]:
        codes.add("report_malformed")
    if detail["diagnostics"]["reason_unrecorded"]:
        codes.add("reason_unrecorded")
    if telemetry:
        codes.add("telemetry_error")
    return detail, codes


class RoundLedger:
    def __init__(self, epoch: str, *, aggregation_started_at: int, limits: dict | None = None):
        self._lock = threading.RLock()
        if type(epoch) is not str:
            raise TypeError("epoch must be exact str")
        if _strict_bytes(epoch, 128) is not None:
            raise ValueError("invalid epoch")
        if type(aggregation_started_at) is not int:
            raise TypeError("aggregation_started_at must be exact int")
        if not 0 <= aggregation_started_at <= _MAX_TIME:
            raise ValueError("invalid aggregation_started_at")
        if limits is not None and type(limits) is not dict:
            raise TypeError("limits must be dict or None")
        defaults = {"max_records": 131072, "max_retained_details": 2048, "max_detail_bytes": 4096}
        for key, value in (limits or {}).items():
            if key not in defaults or type(value) is not int or not 1 <= value <= defaults[key]:
                raise ValueError("invalid limit")
            defaults[key] = value
        self.epoch = epoch
        self.aggregation_started_at = aggregation_started_at
        self._first_bucket_start = aggregation_started_at // _MINUTE * _MINUTE
        self._cumulative_end = None
        self._cumulative_rows = self._empty_rows()
        self._cumulative_rounds = self._empty_rounds()
        self._cumulative_first = None
        self._cumulative_last = None
        self._merge_failure_bucket = None
        self._post_close = {name: 0 for name in (
            "post_close_duplicate", "post_close_conflict", "post_close_finish", "post_close_unverified")}
        self._cumulative_evidence_uncertain = False
        self._limits = defaults
        self._records = {}
        self._seq = []
        self._owners = {}
        self._health = {
            "registered_records": 0, "retained_details": 0, "admission_stopped": False,
            "admission_stopped_at": None, "untracked_invocations": 0,
            "coverage_complete": True, "uncertain_sources": [], "clock_error": False,
            "index_error": False, "counter_saturated": False,
            "last_received_at": None, "last_received_mono": None,
        }

    def _bump(self, key):
        if self._health[key] == _MAX_COUNT:
            self._health["counter_saturated"] = True
        else:
            self._health[key] += 1

    def _observe(self, diag, source=None, global_=False):
        if not diag["coverage_error"]:
            return
        self._health["coverage_complete"] = False
        affected = _SOURCES if global_ or source is None else (source,)
        current = set(self._health["uncertain_sources"])
        current.update(affected)
        self._health["uncertain_sources"] = [name for name in _SOURCES if name in current]

    def _result(self, classification, records=(), changes=(), diag=None):
        if classification in self._post_close:
            self._post_close[classification] = min(_MAX_COUNT, self._post_close[classification] + 1)
        if diag is not None and diag["cumulative_evidence_uncertain"]:
            self._cumulative_evidence_uncertain = True
        return {"classification": classification,
                "records": [copy.deepcopy(rec) for rec in sorted(records, key=lambda rec: rec["seq"])],
                "changes": copy.deepcopy(sorted(changes, key=lambda ch: self._records[ch["invocation_id"]]["seq"])),
                "diagnostics": copy.deepcopy(diag if diag is not None else _diag()),
                "health": copy.deepcopy(self._health)}

    def _reject(self, classification, codes, level=None, source=None, global_=False):
        diag = _diag(codes, level, global_)
        self._observe(diag, source, global_)
        return self._result(classification, diag=diag)

    def _envelope(self, fields):
        first_error = None
        for kind, value in fields:
            if kind == "str":
                if type(value) is not str:
                    raise TypeError("envelope string must be exact str")
                error = _strict_bytes(value, 128)
            else:
                if type(value) is not int:
                    raise TypeError("envelope integer must be exact int")
                ceiling = _MAX_TIME if kind == "time" else 2**31 - 1
                error = "invalid_argument" if not 0 <= value <= ceiling else None
            if first_error is None:
                first_error = error
        return first_error

    def _envelope_reject(self, error):
        if error == "envelope_string_limit":
            return self._reject("input_limit_exceeded", (error, "input_limit_exceeded"), "G", global_=True)
        codes = ("invalid_argument", "invalid_unicode") if error == "invalid_unicode" else ("invalid_argument",)
        return self._reject("invalid_argument", codes, "G", global_=True)

    def _clock_codes(self, wall, mono):
        codes = []
        if self._health["last_received_at"] is not None:
            if wall < self._health["last_received_at"]:
                codes.append("receipt_wall_regressed")
            if mono < self._health["last_received_mono"]:
                codes.append("receipt_mono_regressed")
        return codes

    def _clock_reject(self, codes, query=False, aggregation=False):
        self._health["clock_error"] = True
        diag = _diag((*codes, "time_integrity_error"), "G", True)
        self._observe(diag, global_=True)
        if aggregation:
            return self._aggregation_reject("time_integrity_error", diag)
        if query:
            return self._query_result("time_integrity_error", (), None, diag)
        return self._result("time_integrity_error", diag=diag)

    def _origin_reject(self, source=None, query=False):
        diag = _diag(("before_aggregation_start", "invalid_argument"), None if query else "G",
                     source is None)
        if query:
            return self._aggregation_reject("invalid_argument", diag)
        self._observe(diag, source, source is None)
        return self._result("invalid_argument", diag=diag)

    def _empty_rows(self):
        rows = {}
        for source in _SOURCES:
            for pair in PAIRS:
                rows[(source, pair)] = {
                    "source": source, "pair": pair, "validity_contract": REGISTRY[source][1],
                    "aggregation_rule_version": "d7-aggregation/1", "process_epoch": self.epoch,
                    "round_kind": "primary", "collection": {key: 0 for key in ("V", "M", "U", "N")},
                    "collection_unknown_reasons": {}, "collection_not_attempted_reasons": {},
                    "writing": {}, "final_db": {}, "malformed_axis_items": 0, "reason_unrecorded_items": 0,
                }
        return rows

    def _empty_rounds(self):
        return {source: {
            "source": source, "validity_contract": REGISTRY[source][1],
            "aggregation_rule_version": "d7-aggregation/1", "process_epoch": self.epoch,
            "round_kind": "primary", "rounds": 0, "malformed_rounds": 0,
            "telemetry_error_rounds": 0, "partial_rounds": 0,
        } for source in _SOURCES}

    @staticmethod
    def _checked_add(left, right):
        value = left + right
        if value > _MAX_COUNT:
            raise OverflowError("aggregation counter saturated")
        return value

    def _merge_rows(self, base, delta):
        merged = copy.deepcopy(base)
        for key, row in delta.items():
            out = merged[key]
            for field in ("collection", "collection_unknown_reasons", "collection_not_attempted_reasons",
                          "writing", "final_db"):
                for name, amount in row[field].items():
                    out[field][name] = self._checked_add(out[field].get(name, 0), amount)
            for field in ("malformed_axis_items", "reason_unrecorded_items"):
                out[field] = self._checked_add(out[field], row[field])
        return merged

    def _merge_rounds(self, base, delta):
        merged = copy.deepcopy(base)
        for source, row in delta.items():
            for field in ("rounds", "malformed_rounds", "telemetry_error_rounds", "partial_rounds"):
                merged[source][field] = self._checked_add(merged[source][field], row[field])
        return merged

    def _count_records(self, records):
        rows = self._empty_rows()
        rounds = self._empty_rounds()
        first = last = None
        for rec in records:
            detail = rec["detail"]
            source = rec["source"]
            finished = rec["first_finished_wall"]
            first = finished if first is None else min(first, finished)
            last = finished if last is None else max(last, finished)
            round_row = rounds[source]
            round_row["rounds"] = self._checked_add(round_row["rounds"], 1)
            diag = detail["diagnostics"]
            if diag["malformed_axis_items"]:
                round_row["malformed_rounds"] = self._checked_add(round_row["malformed_rounds"], 1)
            if diag["telemetry_error"]:
                round_row["telemetry_error_rounds"] = self._checked_add(round_row["telemetry_error_rounds"], 1)
            valid_count = sum(detail["pairs"][pair]["collection"]["status"] == "valid" for pair in PAIRS)
            if 0 < valid_count < len(PAIRS):
                round_row["partial_rounds"] = self._checked_add(round_row["partial_rounds"], 1)
            for pair in PAIRS:
                row = rows[(source, pair)]
                axes = detail["pairs"][pair]
                bucket = {"valid": "V", "missing": "M", "unknown": "U", "not_attempted": "N"}[
                    axes["collection"]["status"]]
                row["collection"][bucket] = self._checked_add(row["collection"][bucket], 1)
                for axis, field in (("collection", "collection_unknown_reasons" if bucket == "U" else
                                     "collection_not_attempted_reasons" if bucket == "N" else None),
                                    ("writing", "writing"), ("final_db", "final_db")):
                    if field is None:
                        continue
                    name = axes[axis]["reason"] if axis == "collection" else axes[axis]["status"]
                    row[field][name] = self._checked_add(row[field].get(name, 0), 1)
                row["malformed_axis_items"] = self._checked_add(
                    row["malformed_axis_items"], sum(item[0] == pair for item in diag["malformed"]))
                row["reason_unrecorded_items"] = self._checked_add(
                    row["reason_unrecorded_items"], sum(item[0] == pair for item in diag["reason_unrecorded"]))
        return rows, rounds, first, last

    @staticmethod
    def _present_rows(rows):
        result = []
        for row in rows.values():
            item = copy.deepcopy(row)
            counts = item["collection"]
            determinate = counts["V"] + counts["M"]
            evidence = determinate + counts["U"]
            item["collection_rate"] = counts["V"] / determinate if determinate else None
            item["determinable_ratio"] = determinate / evidence if evidence else None
            ratio = item["determinable_ratio"]
            item["insufficient_evidence"] = ratio < 0.9 if ratio is not None else None
            result.append(item)
        return result

    def _aggregation_reject(self, classification, diag):
        return {"classification": classification, "as_of": self._health["last_received_at"],
                "as_of_mono": self._health["last_received_mono"], "diagnostics": copy.deepcopy(diag),
                "health": copy.deepcopy(self._health)}

    def _advance_and_close(self, wall, mono):
        """Prepare a close candidate, then publish it with the receipt pair."""
        target_end = ((wall - 70 * _MINUTE) // _MINUTE) * _MINUTE
        previous_end = self._cumulative_end if self._cumulative_end is not None else self._first_bucket_start
        closing = []
        rows = rounds = None
        first = last = None
        if target_end > previous_end:
            closing = [rec for rec in self._records.values()
                       if rec["bucket_end"] is not None and not rec["closed"] and rec["bucket_end"] <= target_end]
            included = [rec for rec in closing if rec["inclusion"] == "open_included"]
            rows, rounds, first, last = self._count_records(included)
            if self._merge_failure_bucket is not None and previous_end < self._merge_failure_bucket <= target_end:
                self._merge_failure_bucket = None
                raise CumulativeMergeFailureForTest("cumulative merge failed for test")
            rows = self._merge_rows(self._cumulative_rows, rows)
            rounds = self._merge_rounds(self._cumulative_rounds, rounds)
        # Everything above is read-only, apart from consuming the one-shot fault.
        if target_end > previous_end:
            self._cumulative_rows = rows
            self._cumulative_rounds = rounds
            if first is not None:
                self._cumulative_first = first if self._cumulative_first is None else min(self._cumulative_first, first)
                self._cumulative_last = last if self._cumulative_last is None else max(self._cumulative_last, last)
            self._cumulative_end = target_end
            for rec in closing:
                rec["closed"] = True
                if rec["inclusion"] == "open_included":
                    rec["inclusion"] = "frozen_included"
                elif rec["inclusion"] == "open_excluded":
                    rec["inclusion"] = "frozen_excluded"
                if rec["detail"] is not None:
                    rec["detail"] = None
                    self._health["retained_details"] -= 1
        self._health["last_received_at"] = wall
        self._health["last_received_mono"] = mono

    def _unverified(self, records=()):
        records = tuple(records)
        self._health["index_error"] = True
        changes = []
        event = _diag(("identity_unverified",), "B", not records)
        if records:
            event = _diag()
            for rec in records:
                local = _diag(("identity_unverified",), "F" if rec["closed"] else "B")
                self._isolate(rec, "identity_unverified", changes)
                _merge_diag(rec["diagnostics"], local)
                _merge_diag(event, local)
                self._observe(local, rec["source"])
        else:
            self._observe(event, global_=True)
        return self._result("post_close_unverified", records, changes, event)

    def _check_identity(self, invocation_id, rec=None, candidate=None):
        if rec is None:
            if any(owner == invocation_id for owner in self._owners.values()):
                return self._unverified()
        elif rec["connection"] == "started":
            if self._owners.get((rec["source"], rec["round_id"])) != invocation_id:
                return self._unverified((rec,))
        if candidate is not None:
            owner_id = self._owners.get(candidate)
            if owner_id is not None:
                owner = self._records.get(owner_id)
                if owner is None:
                    return self._unverified()
                if owner["connection"] != "started" or (owner["source"], owner["round_id"]) != candidate:
                    return self._unverified((owner,))
        return None

    def _early(self, epoch, invocation_id, candidate=None):
        if epoch != self.epoch:
            return None, self._result("foreign_epoch", diag=_diag(("foreign_epoch",)))
        if self._health["index_error"]:
            return None, self._reject("post_close_unverified", ("identity_unverified",), "B", global_=True)
        rec = self._records.get(invocation_id)
        fault = self._check_identity(invocation_id, rec, candidate)
        return rec, fault

    @staticmethod
    def _change(rec, action):
        return {"invocation_id": rec["invocation_id"], "action": action, "detail": rec["detail"]}

    def _isolate(self, rec, reason, changes, terminal=None):
        if rec["inclusion"] == "open_included":
            changes.append(self._change(rec, "remove"))
            rec["inclusion"] = "open_excluded"
        if terminal == "mixed":
            rec["lifecycle"] = "contract_mixed"
            rec["unavailable_reason"] = None
        elif terminal == "conflict":
            if rec["lifecycle"] != "contract_mixed":
                rec["lifecycle"] = "conflicting"
            rec["unavailable_reason"] = None
        elif rec["lifecycle"] not in ("contract_mixed", "conflicting"):
            rec["lifecycle"] = "report_unavailable"
            rec["unavailable_reason"] = reason

    def _finish_record(self, classification, records, changes, diag, global_=False):
        for rec in records:
            if rec["lifecycle"] == "report_unavailable":
                _merge_diag(diag, _diag(("ever_unavailable",)))
            _merge_diag(rec["diagnostics"], diag)
        if diag["coverage_error"]:
            for rec in records:
                self._observe(diag, rec["source"], global_)
        return self._result(classification, records, changes, diag)

    def _conflict(self, records):
        changes = []
        event = _diag()
        for rec in records:
            local = _diag(("identity_conflict",), "F" if rec["closed"] else "B")
            self._isolate(rec, None, changes, "conflict")
            _merge_diag(rec["diagnostics"], local)
            _merge_diag(event, local)
            self._observe(local, rec["source"])
        return self._result("identity_conflict", records, changes, event)

    def register(self, *, epoch: str, invocation_id: str, source: str, started_wall: int,
                 started_mono: int, received_at: int, received_mono: int) -> dict:
        with self._lock:
            error = self._envelope((("str", epoch), ("str", invocation_id), ("str", source),
                                    ("time", started_wall), ("time", started_mono),
                                    ("time", received_at), ("time", received_mono)))
            if error:
                return self._envelope_reject(error)
            rec, early = self._early(epoch, invocation_id)
            if early:
                return early
            if rec is None and started_wall < self.aggregation_started_at:
                return self._origin_reject(source if source in REGISTRY else None)
            if self._health["last_received_at"] is None and received_at < self.aggregation_started_at:
                return self._origin_reject(source if source in REGISTRY else None)
            codes = self._clock_codes(received_at, received_mono)
            if codes:
                return self._clock_reject(codes)
            self._advance_and_close(received_at, received_mono)
            if rec is not None:
                if (source, started_wall, started_mono) == (rec["source"], rec["started_wall"], rec["started_mono"]):
                    return self._finish_record("duplicate_invocation", (rec,), [], _diag(("duplicate_invocation",)))
                changes = []
                self._isolate(rec, None, changes, "conflict")
                diag = _diag(("registration_conflict", "identity_conflict"), "F" if rec["closed"] else "B")
                return self._finish_record("registration_conflict", (rec,), changes, diag)
            time_codes = []
            if started_wall > received_at:
                time_codes.append("start_wall_in_future")
            if started_mono > received_mono:
                time_codes.append("start_mono_in_future")
            if time_codes:
                self._health["clock_error"] = True
                return self._reject("time_integrity_error", (*time_codes, "time_integrity_error"), "G", source=source,
                                    global_=source not in REGISTRY)
            if source not in REGISTRY:
                return self._reject("unsupported_source", ("unsupported_source",))
            if self._health["admission_stopped"] or len(self._records) >= self._limits["max_records"]:
                if not self._health["admission_stopped"]:
                    self._health["admission_stopped"] = True
                    self._health["admission_stopped_at"] = received_at
                self._bump("untracked_invocations")
                return self._reject("admission_stopped", ("admission_stopped", "aggregation_capacity"), "G", source)
            schema, contract = REGISTRY[source]
            rec = {"epoch": self.epoch, "invocation_id": invocation_id, "source": source,
                   "seq": len(self._seq) + 1, "started_wall": started_wall, "started_mono": started_mono,
                   "expected_report_schema": schema, "expected_validity_contract": contract,
                   "round_id": None, "linked_report_schema": None, "linked_validity_contract": None,
                   "connection": "unbound", "lifecycle": "awaiting_report", "unavailable_reason": None,
                   "first_finished_wall": None, "first_finished_mono": None, "first_digest": None,
                   "bucket_start": None, "bucket_end": None, "close_at": None, "closed": False,
                   "inclusion": "none", "diagnostics": _diag(), "detail": None}
            self._records[invocation_id] = rec
            self._seq.append(invocation_id)
            self._bump("registered_records")
            return self._result("registered", (rec,))

    def link_round(self, *, epoch: str, invocation_id: str, round_id: str, report_schema: int,
                   validity_contract: str, received_at: int, received_mono: int) -> dict:
        with self._lock:
            error = self._envelope((("str", epoch), ("str", invocation_id), ("str", round_id),
                                    ("schema", report_schema), ("str", validity_contract),
                                    ("time", received_at), ("time", received_mono)))
            if error:
                return self._envelope_reject(error)
            rec, early = self._early(epoch, invocation_id)
            if early:
                return early
            if rec is None:
                return self._reject("orphan", ("orphan_start",), "G", global_=True)
            fault = self._check_identity(invocation_id, rec, (rec["source"], round_id))
            if fault:
                return fault
            if self._health["last_received_at"] is None and received_at < self.aggregation_started_at:
                return self._origin_reject(rec["source"])
            codes = self._clock_codes(received_at, received_mono)
            if codes:
                return self._clock_reject(codes)
            self._advance_and_close(received_at, received_mono)
            other_id = self._owners.get((rec["source"], round_id))
            if (rec["connection"] == "started" and rec["round_id"] != round_id) or (other_id and other_id != invocation_id):
                involved = [rec]
                if other_id and other_id != invocation_id:
                    involved.append(self._records[other_id])
                return self._conflict(involved)
            if rec["connection"] == "started":
                if (report_schema, validity_contract) == (rec["linked_report_schema"], rec["linked_validity_contract"]):
                    return self._finish_record("relinked_same", (rec,), [], _diag(("relinked_same",)))
                changes = []
                self._isolate(rec, None, changes, "mixed")
                codes = ["contract_mixed"]
                if (report_schema, validity_contract) != REGISTRY[rec["source"]]:
                    codes.append("unregistered_contract")
                diag = _diag(codes, "F" if rec["closed"] else "B")
                return self._finish_record("contract_mixed", (rec,), changes, diag)
            if (report_schema, validity_contract) != REGISTRY[rec["source"]]:
                changes = []
                self._isolate(rec, "unregistered_contract", changes)
                return self._finish_record("unregistered_contract", (rec,), changes,
                                           _diag(("unregistered_contract",), "G"))
            rec["round_id"] = round_id
            rec["linked_report_schema"] = report_schema
            rec["linked_validity_contract"] = validity_contract
            rec["connection"] = "started"
            self._owners[(rec["source"], round_id)] = invocation_id
            if rec["lifecycle"] == "awaiting_report":
                rec["lifecycle"] = "in_flight"
            return self._finish_record("linked", (rec,), [], _diag())

    def finish(self, *, epoch: str, invocation_id: str, round_id: str, report_schema: int,
               validity_contract: str, finished_wall: int, finished_mono: int,
               selected_summary: dict, telemetry_error_present: bool,
               received_at: int, received_mono: int) -> dict:
        with self._lock:
            fields = (("str", epoch), ("str", invocation_id), ("str", round_id), ("schema", report_schema),
                      ("str", validity_contract), ("time", finished_wall), ("time", finished_mono),
                      ("time", received_at), ("time", received_mono))
            error = self._envelope(fields)
            if type(selected_summary) is not dict or type(telemetry_error_present) is not bool:
                raise TypeError("summary must be exact dict and telemetry must be exact bool")
            if error:
                return self._envelope_reject(error)
            rec, early = self._early(epoch, invocation_id)
            if early:
                return early
            if rec is None:
                return self._reject("orphan", ("orphan_finish",), "G", global_=True)
            fault = self._check_identity(invocation_id, rec, (rec["source"], round_id))
            if fault:
                return fault
            if self._health["last_received_at"] is None and received_at < self.aggregation_started_at:
                return self._origin_reject(rec["source"])
            codes = self._clock_codes(received_at, received_mono)
            if codes:
                return self._clock_reject(codes)
            self._advance_and_close(received_at, received_mono)
            other_id = self._owners.get((rec["source"], round_id))
            if other_id is not None and other_id != invocation_id:
                return self._conflict((rec, self._records[other_id]))
            if rec["connection"] == "unbound":
                changes = []
                self._isolate(rec, "start_unrecorded", changes)
                return self._finish_record("start_unrecorded", (rec,), changes,
                                           _diag(("start_unrecorded", "report_unavailable"), "G"))
            if rec["round_id"] != round_id:
                return self._conflict((rec,))
            mixed = (report_schema, validity_contract) != (rec["linked_report_schema"], rec["linked_validity_contract"])
            time_codes = []
            if finished_wall < rec["started_wall"]:
                time_codes.append("finish_wall_before_start")
            if finished_mono < rec["started_mono"]:
                time_codes.append("finish_mono_before_start")
            if finished_wall > received_at:
                time_codes.append("finish_wall_in_future")
            if finished_mono > received_mono:
                time_codes.append("finish_mono_in_future")
            if time_codes:
                time_codes.append("time_integrity_error")
                self._health["clock_error"] = True
            problem = None
            candidate = None
            try:
                _check_summary(selected_summary)
                candidate_obj = _digest_object(rec["source"], round_id, report_schema, validity_contract,
                                               finished_wall, finished_mono, selected_summary, telemetry_error_present)
                _, candidate = _encoded_size_and_hash(candidate_obj, 65536)
                if candidate is None:
                    raise _InputProblem("canonical_bytes_limit")
            except _InputProblem as exc:
                problem = exc
            extra_codes = []
            if problem is not None:
                problem_class = "input_shape_error" if problem.shape else "input_limit_exceeded"
                extra_codes = [problem_class, problem.code, "report_unavailable"]
            elif rec["first_digest"] is not None and candidate != rec["first_digest"]:
                extra_codes = ["post_close_conflict" if rec["closed"] else "conflicting_finish"]
            if mixed:
                changes = []
                self._isolate(rec, None, changes, "mixed")
                mixed_codes = ["contract_mixed"]
                if (report_schema, validity_contract) != REGISTRY[rec["source"]]:
                    mixed_codes.append("unregistered_contract")
                return self._finish_record("contract_mixed", (rec,), changes,
                                           _diag((*mixed_codes, *extra_codes, *time_codes),
                                                 "F" if rec["closed"] else "B"))
            if problem is not None:
                changes = []
                previous = rec["first_digest"] is not None or rec["lifecycle"] in ("conflicting", "contract_mixed")
                self._isolate(rec, "input_shape" if problem.shape else "input_limit", changes)
                level = ("F" if rec["closed"] else "B") if previous else "G"
                return self._finish_record(problem_class, (rec,), changes, _diag((*extra_codes, *time_codes), level))
            if extra_codes:
                changes = []
                self._isolate(rec, None, changes, "conflict")
                return self._finish_record("post_close_conflict" if rec["closed"] else "conflicting_finish",
                                           (rec,), changes, _diag((*extra_codes, *time_codes),
                                                                   "F" if rec["closed"] else "B"))
            if time_codes:
                changes = []
                previous = rec["first_digest"] is not None or rec["lifecycle"] in ("conflicting", "contract_mixed")
                self._isolate(rec, "time_integrity_error", changes)
                level = ("F" if rec["closed"] else "B") if previous else "G"
                return self._finish_record("time_integrity_error", (rec,), changes, _diag(time_codes, level))
            if rec["lifecycle"] in ("conflicting", "contract_mixed"):
                return self._finish_record("after_conflict_redelivery", (rec,), [],
                                           _diag(("after_conflict_redelivery",), "F" if rec["closed"] else "B"))
            if rec["first_digest"] is not None:
                duplicate = "post_close_duplicate" if rec["closed"] else "duplicate_finish"
                codes = ("duplicate_finish", "post_close_duplicate") if rec["closed"] else ("duplicate_finish",)
                return self._finish_record(duplicate, (rec,), [], _diag(codes))
            prior_unavailable = rec["lifecycle"] == "report_unavailable"
            bucket = (finished_wall // _MINUTE) * _MINUTE
            rec["first_finished_wall"] = finished_wall
            rec["first_finished_mono"] = finished_mono
            rec["first_digest"] = candidate
            rec["bucket_start"] = bucket
            rec["bucket_end"] = bucket + _MINUTE
            rec["close_at"] = bucket + _MINUTE + _CLOSE_DELAY
            if received_at >= rec["close_at"]:
                rec["closed"] = True
                rec["lifecycle"] = "finalized"
                rec["unavailable_reason"] = None
                rec["inclusion"] = "post_close_excluded"
                codes = ["post_close_finish"]
                if prior_unavailable:
                    codes.append("late_finish_accepted")
                return self._finish_record("post_close_finish", (rec,), [], _diag(codes, "G"))
            detail, detail_codes = _detail_and_codes(rec["source"], report_schema, validity_contract,
                                                     selected_summary, telemetry_error_present)
            size, _ = _encoded_size_and_hash(detail, self._limits["max_detail_bytes"])
            if size > self._limits["max_detail_bytes"] or self._health["retained_details"] >= self._limits["max_retained_details"]:
                rec["inclusion"] = "open_excluded"
                self._isolate(rec, "aggregation_capacity", [])
                return self._finish_record("report_unavailable", (rec,), [],
                                           _diag(("report_unavailable", "aggregation_capacity", *detail_codes), "G"))
            rec["detail"] = detail
            self._bump("retained_details")
            rec["lifecycle"] = "finalized"
            rec["unavailable_reason"] = None
            rec["inclusion"] = "open_included"
            codes = set(detail_codes)
            if prior_unavailable:
                codes.add("late_finish_accepted")
            return self._finish_record("finalized", (rec,), [self._change(rec, "add")], _diag(codes))

    def record(self, invocation_id: str) -> dict | None:
        with self._lock:
            if type(invocation_id) is not str:
                raise TypeError("invocation_id must be exact str")
            if _strict_bytes(invocation_id, 128) is not None:
                raise ValueError("invalid invocation_id")
            return copy.deepcopy(self._records.get(invocation_id))

    def _query_result(self, classification, entries, next_seq, diag):
        return {"classification": classification, "as_of": self._health["last_received_at"],
                "as_of_mono": self._health["last_received_mono"], "entries": copy.deepcopy(list(entries)),
                "next_seq": next_seq, "diagnostics": copy.deepcopy(diag), "health": copy.deepcopy(self._health)}

    def contributions_open(self, *, as_of: int, as_of_mono: int, after_seq: int = 0, limit: int = 16) -> dict:
        with self._lock:
            for value in (as_of, as_of_mono, after_seq, limit):
                if type(value) is not int:
                    raise TypeError("query integers must be exact int")
            if (not 0 <= as_of <= _MAX_TIME or not 0 <= as_of_mono <= _MAX_TIME
                    or not 0 <= after_seq <= self._limits["max_records"] or not 1 <= limit <= 16):
                return self._query_result("invalid_argument", (), None, _diag(("invalid_argument",)))
            if self._health["index_error"]:
                return self._query_result("post_close_unverified", (), None,
                                          _diag(("identity_unverified",), "B", True))
            if self._health["last_received_at"] is None and as_of < self.aggregation_started_at:
                return self._query_result("invalid_argument", (), None,
                                          _diag(("before_aggregation_start", "invalid_argument")))
            codes = self._clock_codes(as_of, as_of_mono)
            if codes:
                return self._clock_reject(codes, query=True)
            self._advance_and_close(as_of, as_of_mono)
            entries = []
            more = False
            for seq in range(after_seq + 1, len(self._seq) + 1):
                rec = self._records[self._seq[seq - 1]]
                if rec["inclusion"] != "open_included":
                    continue
                if len(entries) == limit:
                    more = True
                    break
                entries.append({"seq": seq, "invocation_id": rec["invocation_id"], "detail": rec["detail"]})
            return self._query_result("snapshot", entries, entries[-1]["seq"] if more else None, _diag())

    def aggregation_snapshot(self, *, as_of: int, as_of_mono: int) -> dict:
        with self._lock:
            if type(as_of) is not int or type(as_of_mono) is not int:
                raise TypeError("query integers must be exact int")
            if not 0 <= as_of <= _MAX_TIME or not 0 <= as_of_mono <= _MAX_TIME:
                return self._aggregation_reject("invalid_argument", _diag(("invalid_argument",)))
            if self._health["index_error"]:
                return self._aggregation_reject("post_close_unverified", _diag(("identity_unverified",), "B", True))
            if self._health["last_received_at"] is None and as_of < self.aggregation_started_at:
                return self._origin_reject(query=True)
            codes = self._clock_codes(as_of, as_of_mono)
            if codes:
                return self._clock_reject(codes, aggregation=True)
            self._advance_and_close(as_of, as_of_mono)
            window_end = as_of // _MINUTE * _MINUTE
            window_start = window_end - 60 * _MINUTE
            recent = [rec for rec in self._records.values() if rec["inclusion"] == "open_included"
                      and window_start <= rec["bucket_start"] and rec["bucket_end"] <= window_end]
            rows, rounds, first, last = self._count_records(recent)
            return {
                "classification": "snapshot", "as_of": as_of, "as_of_mono": as_of_mono,
                "process_epoch": self.epoch, "aggregation_rule_version": "d7-aggregation/1",
                "aggregation_started_at": self.aggregation_started_at,
                "first_bucket_start": self._first_bucket_start,
                "first_bucket_partial": self.aggregation_started_at != self._first_bucket_start,
                "window_start": window_start, "window_end": window_end,
                "recent": self._present_rows(rows), "cumulative": self._present_rows(self._cumulative_rows),
                "recent_rounds": copy.deepcopy(list(rounds.values())),
                "cumulative_rounds": copy.deepcopy(list(self._cumulative_rounds.values())),
                "recent_first_finished_at": first, "recent_last_finished_at": last,
                "cumulative_first_finished_at": self._cumulative_first,
                "cumulative_last_finished_at": self._cumulative_last,
                "cumulative_end": self._cumulative_end,
                "warming_up": window_start < self.aggregation_started_at,
                "coverage": {"ledger_health_complete": self._health["coverage_complete"],
                             "external_observation_verified": False, "complete": False,
                             "uncertain_sources": list(self._health["uncertain_sources"])},
                "close_policy": {"window_minutes": 60, "grace_minutes": 10, "bucket_minutes": 1},
                "thresholds": {"insufficient_evidence": 0.9},
                "post_close_diagnostics": {**self._post_close,
                                           "cumulative_evidence_uncertain": self._cumulative_evidence_uncertain},
                "health": copy.deepcopy(self._health),
            }

    def _inject_cumulative_merge_failure_for_test(self, *, bucket_end: int) -> None:
        with self._lock:
            if type(bucket_end) is not int:
                raise TypeError("bucket_end must be exact int")
            previous_end = self._cumulative_end if self._cumulative_end is not None else self._first_bucket_start
            if (not 0 <= bucket_end <= _MAX_TIME or bucket_end % _MINUTE
                    or bucket_end < self._first_bucket_start + _MINUTE
                    or bucket_end <= previous_end or self._merge_failure_bucket is not None):
                raise ValueError("invalid bucket_end or merge failure already armed")
            self._merge_failure_bucket = bucket_end

    def _inject_identity_fault_for_test(self, *, invocation_id: str, fault: str) -> None:
        with self._lock:
            if type(invocation_id) is not str or type(fault) is not str:
                raise TypeError("fault arguments must be exact str")
            rec = self._records.get(invocation_id)
            if (fault not in ("missing_record", "missing_owner") or self._health["index_error"]
                    or rec is None or rec["connection"] != "started"
                    or self._owners.get((rec["source"], rec["round_id"])) != invocation_id):
                raise ValueError("fault precondition failed")
            if fault == "missing_record":
                del self._records[invocation_id]
            else:
                del self._owners[(rec["source"], rec["round_id"])]
