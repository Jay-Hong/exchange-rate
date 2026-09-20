"""Independent identifier detection and field-by-field provenance validation.

Regex non-detection is not proof of anonymity. Short, separated, nonhex tokens
and rate-shaped identifiers can pass; C1c human review remains mandatory.
"""

import ast
import hashlib
import math
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID

from bs4 import NavigableString, Tag

from .errors import CaptureError
from .registry import query_key


# This table is deliberately exhaustive. No metadata-wide exemption exists.
METADATA_POLICY = {
    "origin": "constant:dev_machine",
    "capture_id": "canonical_uuid4:generated",
    "extraction_contract": "exact:current_contract",
    "source_commit": "exact:clean_git_head",
    "source_url_base": "exact:route_url_constant",
    "route": "exact:registered_route",
    "parser": "exact:running_parser_versions",
    "fetched_at": "utc_timestamp:generated",
    "http_status": "integer:2xx",
    "content_type": "response_string:scan",
    "charset": "response_string_or_null:scan",
    "original_body_sha256": "sha256:bounded_response_content",
    "fixture_sha256": "sha256:serialized_fixture",
    "recorded_extraction": "closed_schema:events_queries_return_exception",
}
RULES = (
    ("long_alphanumeric", re.compile(r"[A-Za-z0-9]{20,}")),
    ("long_hex", re.compile(r"[A-Fa-f0-9]{16,}")),
    ("at_token", re.compile(r"@")),
    ("url_scheme", re.compile(r"://")),
    ("long_number", re.compile(r"[0-9]{10,}")),
    # `long_number` only sees UNBROKEN digit runs, so hyphenated identifiers pass it
    # (measured: "901231-1234567" and "1234-5678-9012-3456" were both accepted).
    # These two add exactly those two shapes. They are NOT a general identifier
    # detector: "010-1234-5678" and space-separated card numbers still pass, and a
    # digit-count threshold cannot be used instead because 11 joined digits occur in
    # a legitimate corporate phone number ("82-2-1588-6200") in the captured pages.
    # Known limits, deliberately out of scope here: these run per text node, so a
    # shape split across inline tags ("901231-<b>1234567</b>") is not seen, and a
    # non-ASCII hyphen is a different character. They also use search(), so a longer
    # digit string that merely CONTAINS one of the shapes is rejected too.
    # The seventh RRN digit is not narrowed: 9 and 0 occur (pre-1900 births), and
    # narrowing to [1-8] would not have avoided a single false positive — a plain
    # amount range like "100000-1000000" matches either way (measured).
    ("rrn_shape", re.compile(r"[0-9]{6}-[0-9]{7}")),
    ("card_shape", re.compile(r"[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{4}")),
)
# Exactly the format accepted as an ordinary decimal rate representation here;
# no scientific notation, integer identifiers or arbitrary surrounding text.
_RATE = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,6})\.[0-9]{1,6}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PAIRS = frozenset(("usd-krw", "jpy-krw", "eur-krw"))


def _require(condition, location):
    if not condition:
        raise CaptureError("field_contract", location)


def scan_string(value, location):
    _require(type(value) is str, location)
    for name, pattern in RULES:
        if name == "long_number" and _RATE.fullmatch(value.strip()):
            continue
        if pattern.search(value):
            raise CaptureError(name, location)


def scan_fixture(soup):
    for index, node in enumerate(soup.descendants):
        location = f"fixture.nodes[{index}]"
        if isinstance(node, Tag):
            scan_string(node.name, location + ".tag")
            for attr_index, (key, value) in enumerate(node.attrs.items()):
                scan_string(key, location + f".attributes[{attr_index}].name")
                for token_index, token in enumerate(value if isinstance(value, list) else [value]):
                    scan_string(token, location + f".attributes[{attr_index}].values[{token_index}]")
        elif isinstance(node, NavigableString):
            scan_string(str(node), location + ".text")


def _keys(value, keys, location):
    _require(type(value) is dict and set(value) == set(keys), location)


def _index(value, location, nullable=False):
    _require((nullable and value is None) or (type(value) is int and value >= 0), location)


def _path(value, location):
    _require(type(value) is list, location)
    for index in value:
        _index(index, location)


def _number(value, location):
    _require((type(value) in (int, float) and math.isfinite(value)) or
             (type(value) is dict and set(value) == {"nonfinite"}
              and value["nonfinite"] in ("nan", "inf", "-inf")), location)


def _basis(value, registry, location):
    _require(type(value) is dict, location)
    if value.get("branch") == "header_index":
        _keys(value, ("branch", "column_index", "row_cell_count", "used_counter_span"), location)
        _index(value["column_index"], location)
        _index(value["row_cell_count"], location)
        _require(type(value["used_counter_span"]) is bool, location)
    else:
        _keys(value, ("branch", "reason", "selector", "matched_count"), location)
        _require(value["branch"] == "fallback", location)
        _require(value["reason"] in ("column_index_unresolved", "row_cells_insufficient"), location)
        _require(value["selector"] is None or value["selector"] in registry.sources.utils.MIBANK_RATE_CELL_SELECTORS, location)
        _index(value["matched_count"], location)


def _fact(key, value, registry, location):
    # Each exemption has a specific code source or enum; page-derived codes and
    # labels still pass through scan_string even if they resemble an enum.
    if key in ("rate_text", "code"):
        scan_string(value, location)
    elif key == "pair":
        _require(value in _PAIRS, location)
    elif key == "selector":
        _require(value in registry.evidence_selectors, location)
    elif key == "order":
        _require(value in registry.sources.citi.CITI_BANK_SELECTORS, location)
    elif key == "matched_code":
        _require(value in registry.sources.citi.CURRENCY_TEXTS, location)
    elif key == "rate":
        _number(value, location)
    elif key in ("element", "item", "row", "tbody"):
        _keys(value, ("element_path",), location)
        _path(value["element_path"], location)
    elif key == "column_index":
        _index(value, location, nullable=True)
    elif key == "row_index":
        _index(value, location)
    elif key == "column_basis":
        _require(value in ("label_found", "header_row_absent", "label_not_found"), location)
    elif key == "code_basis":
        _require(value in ("explicit_code_param", "flag_filename"), location)
    elif key == "value_basis":
        _basis(value, registry, location)
    else:
        raise CaptureError("unknown_fact", location)


def _event_keys(kind, route):
    if kind == "loop_completed":
        return set()
    if route.extractor == "mibank":
        if kind == "table_structure":
            return {"tbody", "column_index", "column_basis"}
        if kind == "code_outside_required":
            return {"code", "code_basis"}
        base = {"pair", "row_index", "row", "code_basis", "value_basis"}
        return {"empty_value": base, "parse_error": base | {"rate_text"},
                "observed": base | {"rate_text", "rate", "code"}}.get(kind)
    if route.extractor == "citi":
        return {"item_miss": {"order", "selector"},
                "selector_miss": {"order", "pair", "selector"},
                "parse_error": {"order", "pair", "rate_text"},
                "observed": {"order", "pair", "rate_text", "rate", "selector",
                             "element", "item", "matched_code"}}.get(kind)
    return {"selector_miss": {"pair", "selector"},
            "parse_error": {"pair", "selector", "rate_text"},
            "observed": {"pair", "selector", "rate_text", "rate", "element"}}.get(kind)


def _labels(labels, location):
    _require(type(labels) is dict, location)
    for index, (key, value) in enumerate(labels.items()):
        item_location = location + f"[{index}]"
        if key in ("item_text", "value_container_text", "row_text", "header_text"):
            if value is not None:
                scan_string(value, item_location)
        elif key in ("cell_index", "row_cell_count"):
            _index(value, item_location, nullable=key == "cell_index")
        elif key in ("row_has_span", "header_has_span"):
            _require(type(value) is bool, item_location)
        else:
            raise CaptureError("unknown_label", item_location)


def _exception_record(value, registry, location):
    _keys(value, ("type", "args", "site"), location)
    _require(value["type"] in ("ValueError", "RuntimeError", "TypeError", "KeyError",
                               "IndexError", "AttributeError", "OverflowError", "ZeroDivisionError"), location)

    def arguments(values):
        _require(type(values) is list, location)
        for index, item in enumerate(values):
            if type(item) is str:
                scan_string(item, location + f".args[{index}]")
            elif type(item) is list:
                arguments(item)
            else:
                _require(item is None or type(item) in (bool, int, float), location)
    arguments(value["args"])
    # Traceback locations are verified against the actual production source AST.
    sites = {}
    for module in (registry.sources.utils, registry.sources.citi, registry.sources.bs):
        path = Path(module.__file__)
        sites[path.name] = [(node.name, node.lineno, node.end_lineno)
                            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    _require(type(value["site"]) is list and bool(value["site"]), location)
    for frame in value["site"]:
        _require(type(frame) is list and len(frame) == 3, location)
        filename, function, line = frame
        _require(type(line) is int and any(name == function and first <= line <= last
                 for name, first, last in sites.get(filename, [])), location)


def validate_recording(record, route, registry):
    location = "recorded_extraction"
    _keys(record, ("events", "queries", "returned", "exception"), location)
    _require(type(record["events"]) is list, location)
    completed = False
    for index, event in enumerate(record["events"]):
        loc = location + f".events[{index}]"
        _keys(event, ("kind", "facts", "labels"), loc)
        _require(not completed, loc)
        keys = _event_keys(event["kind"], route)
        _require(keys is not None, loc)
        _keys(event["facts"], keys, loc)
        for key, value in event["facts"].items():
            _fact(key, value, registry, loc + "." + key)
        _labels(event["labels"], loc + ".labels")
        completed = event["kind"] == "loop_completed"
    _require(type(record["queries"]) is list, location)
    for index, query in enumerate(record["queries"]):
        loc = location + f".queries[{index}]"
        _keys(query, ("method", "args", "kwargs", "root", "results"), loc)
        _require(query_key(query["method"], query["args"], query["kwargs"]) in registry.allowed_queries, loc)
        _path(query["root"], loc)
        _require(type(query["results"]) is list, loc)
        for path in query["results"]:
            _path(path, loc)
    if record["exception"] is not None:
        _require(record["returned"] is None and not completed, location)
        _exception_record(record["exception"], registry, location + ".exception")
    else:
        _require(completed, location)
        returned = record["returned"]
        if route.extractor == "mibank":
            _require(type(returned) is list and len(returned) == 2 and type(returned[1]) is list, location)
            for index, code in enumerate(returned[1]):
                scan_string(code, location + f".found_codes[{index}]")
            returned = returned[0]
        _require(type(returned) is dict and set(returned) <= _PAIRS, location)
        for rate in returned.values():
            _number(rate, location + ".returned")


def validate_metadata(meta, *, route, registry, source_commit, contract, parser, original_hash, fixture):
    _keys(meta, METADATA_POLICY, "metadata")
    exact = {"origin": "dev_machine", "route": route.name, "source_url_base": route.url,
             "source_commit": source_commit, "extraction_contract": contract, "parser": parser,
             "original_body_sha256": original_hash, "fixture_sha256": hashlib.sha256(fixture).hexdigest()}
    for key, value in exact.items():
        _require(meta[key] == value, "metadata." + key)
    for key in ("original_body_sha256", "fixture_sha256"):
        _require(type(meta[key]) is str and _HASH.fullmatch(meta[key]), "metadata." + key)
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", meta["source_commit"])), "metadata.source_commit")
    try:
        capture_id = UUID(meta["capture_id"])
        _require(capture_id.version == 4 and str(capture_id) == meta["capture_id"], "metadata.capture_id")
        timestamp = datetime.strptime(meta["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
        _require(timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") == meta["fetched_at"], "metadata.fetched_at")
    except (ValueError, TypeError, AttributeError):
        raise CaptureError("field_contract", "metadata.generated") from None
    _require(type(meta["http_status"]) is int and 200 <= meta["http_status"] < 300, "metadata.http_status")
    scan_string(meta["content_type"], "metadata.content_type")
    if meta["charset"] is not None:
        scan_string(meta["charset"], "metadata.charset")
    validate_recording(meta["recorded_extraction"], route, registry)
