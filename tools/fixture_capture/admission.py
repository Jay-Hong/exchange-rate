"""Historical evidence admission: the C1d layer A, followed by D1 approval.

Schema checks and scans do not depend on current selectors or the capture path.
Historical provenance stays in layer A; D1 recomputes under the current policy.
"""

import hashlib
import json
import math
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

from bs4 import BeautifulSoup, Comment, Doctype, ProcessingInstruction, Tag

from . import d1_approval
from . import detector as d
from .errors import CaptureError
from .limits import HTML_LIMIT, METADATA_LIMIT
from .queries import query_key

# Historical C1b/1 schema, not Registry(): selectors and code may move later.
EXTRACTORS = {"bs_official": "selector", "citi_primary": "citi", "citi_secondary": "selector",
              "bs_mibank": "mibank", "citi_mibank": "mibank"}
HASH_PATTERN = r"[0-9a-f]{64}"
TOKEN_PATTERN = r"[A-Za-z0-9_-]+"


def require(condition, location):
    d._require(condition, location)


def matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def load_json(raw, location):
    """No duplicate keys, non-JSON numbers, BOM or undecodable evidence."""
    def reject(*args):
        raise ValueError("invalid JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject()
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            reject()
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=reject, parse_float=number)
    except (ValueError, UnicodeError, RecursionError):
        raise CaptureError("invalid_json", location) from None


def _exception(value):
    loc = "recorded_extraction.exception"
    d._keys(value, ("type", "args", "site"), loc)
    require(value["type"] in ("ValueError", "RuntimeError", "TypeError", "KeyError",
                             "IndexError", "AttributeError", "OverflowError", "ZeroDivisionError"), loc)

    def arguments(items):
        require(type(items) is list, loc + ".args")
        for item in items:
            if type(item) is str:
                d.scan_string(item, loc + ".args")
            elif type(item) is list:
                arguments(item)
            else:
                require(item is None or type(item) in (bool, int, float), loc + ".args")
                if type(item) is float:
                    require(math.isfinite(item), loc + ".args")
    arguments(value["args"])
    require(type(value["site"]) is list and bool(value["site"]), loc + ".site")
    for frame in value["site"]:
        require(type(frame) is list and len(frame) == 3, loc + ".site")
        filename, function, line = frame
        require(filename in ("bs.py", "citi.py", "utils.py"), loc + ".site.file")
        require(matches(function, r"[A-Za-z_][A-Za-z0-9_]*"), loc + ".site.function")
        require(type(line) is int and line > 0, loc + ".site.line")


def _fact(key, value, loc):
    if key in ("pair", "column_basis", "code_basis"):
        require(type(value) is str, loc)
    if key in ("selector", "order", "matched_code"):
        require(type(value) is str, loc)
        d.scan_string(value, loc)
    elif key == "value_basis" and type(value) is dict and value.get("branch") == "fallback":
        d._keys(value, ("branch", "reason", "selector", "matched_count"), loc)
        require(value["reason"] in ("column_index_unresolved", "row_cells_insufficient"), loc)
        require(value["selector"] is None or type(value["selector"]) is str, loc)
        if value["selector"] is not None:
            d.scan_string(value["selector"], loc + ".selector")
        d._index(value["matched_count"], loc)
    else:
        # All remaining branches use only schema primitives/enums, not registry.
        d._fact(key, value, None, loc)


def validate_stored_recording(record, route_name):
    """A: closed shapes/types and page-string scans; historical sites only."""
    loc = "recorded_extraction"
    d._keys(record, ("events", "queries", "returned", "exception"), loc)
    require(route_name in EXTRACTORS, "route")
    route = SimpleNamespace(extractor=EXTRACTORS[route_name])
    require(type(record["events"]) is list, loc + ".events")
    completed = False
    for event in record["events"]:
        d._keys(event, ("kind", "facts", "labels"), loc + ".event")
        require(type(event["kind"]) is str and not completed, loc + ".kind")
        keys = d._event_keys(event["kind"], route)
        require(keys is not None, loc + ".kind")
        d._keys(event["facts"], keys, loc + ".facts")
        for key, value in event["facts"].items():
            _fact(key, value, loc + ".facts." + key)
        d._labels(event["labels"], loc + ".labels")
        completed = event["kind"] == "loop_completed"
    require(type(record["queries"]) is list, loc + ".queries")
    for query in record["queries"]:
        d._keys(query, ("method", "args", "kwargs", "root", "results"), loc + ".query")
        require(type(query["method"]) is str and type(query["args"]) is list
                and type(query["kwargs"]) is dict
                and all(type(k) is str for k in query["kwargs"]), loc + ".query.arguments")
        query_key(query["method"], query["args"], query["kwargs"])  # types only, no membership
        d._path(query["root"], loc + ".query.root")
        require(type(query["results"]) is list, loc + ".query.results")
        for path in query["results"]:
            d._path(path, loc + ".query.results")
    if record["exception"] is not None:
        require(record["returned"] is None and not completed, loc)
        _exception(record["exception"])
    else:
        require(completed, loc + ".completed")
        returned = record["returned"]
        if route.extractor == "mibank":
            require(type(returned) is list and len(returned) == 2 and type(returned[1]) is list, loc)
            for code in returned[1]:
                d.scan_string(code, loc + ".found_codes")
            returned = returned[0]
        require(type(returned) is dict and set(returned) <= d._PAIRS, loc + ".returned")
        for rate in returned.values():
            d._number(rate, loc + ".returned")


def validate_structure(soup):
    for node in soup.descendants:
        require(not isinstance(node, (Comment, Doctype, ProcessingInstruction)), "fixture.special_node")
        if not isinstance(node, Tag):
            continue
        require(node.name not in ("script", "style", "noscript", "template") or not node.contents,
                "fixture.tag_body")
        require(set(node.attrs) <= {"id", "class", "href", "src", "colspan", "rowspan"}, "fixture.attributes")
        for key, value in node.attrs.items():
            loc = "fixture.attributes." + key
            if key == "href":
                require(node.name == "a" and matches(value, r"\?currency=(?:[A-Za-z]{3})?(?:&currency=(?:[A-Za-z]{3})?)*"), loc)
            elif key == "src":
                require(node.name == "img" and matches(value, r"flag_[A-Za-z]{3}[_.]"), loc)
            elif key in ("colspan", "rowspan"):
                require(type(value) is str and matches(value.strip(), r"[+-]?[0-9]+")
                        and 1 <= int(value) <= 20, loc)
            elif key == "id":
                require(matches(value, TOKEN_PATTERN), loc)
            else:
                # BeautifulSoup uses AttributeValueList, a list subclass.
                require(isinstance(value, list) and all(matches(v, TOKEN_PATTERN) for v in value), loc)
    d.scan_fixture(soup)


@dataclass(frozen=True)
class Evidence:
    route: str
    fixture: bytes
    metadata_bytes: bytes
    metadata: dict

    @property
    def fixture_id(self):
        return f"{self.route}/{self.metadata['capture_id']}"

    def soup(self):
        return BeautifulSoup(self.fixture.decode("utf-8"), "html.parser")


def validate_integrity(fixture, metadata_bytes, route_name):
    require(route_name in EXTRACTORS, "route")
    require(len(fixture) <= HTML_LIMIT, "fixture.size")
    require(len(metadata_bytes) <= METADATA_LIMIT, "metadata.size")
    meta = load_json(metadata_bytes, "metadata")
    d._keys(meta, d.METADATA_POLICY, "metadata")
    require(meta["origin"] == "dev_machine", "metadata.origin")
    require(meta["route"] == route_name, "metadata.route")
    for key in ("fixture_sha256", "original_body_sha256"):
        require(matches(meta[key], HASH_PATTERN), "metadata." + key)
    require(meta["fixture_sha256"] == sha256(fixture), "metadata.fixture_sha256")
    require(matches(meta["source_commit"], r"[0-9a-f]{40}"), "metadata.source_commit")
    require(matches(meta["extraction_contract"], r"c1b/1:" + HASH_PATTERN), "metadata.extraction_contract")
    try:
        capture_id = UUID(meta["capture_id"])
        require(capture_id.version == 4 and str(capture_id) == meta["capture_id"], "metadata.capture_id")
        timestamp = datetime.strptime(meta["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
        require(timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") == meta["fetched_at"], "metadata.fetched_at")
        url = urlsplit(meta["source_url_base"])
        require(type(meta["source_url_base"]) is str and url.scheme in ("http", "https")
                and bool(url.hostname) and not url.username and not url.password and not url.fragment
                and not re.search(r"\s", meta["source_url_base"]), "metadata.source_url_base")
    except (ValueError, TypeError, AttributeError):
        raise CaptureError("field_contract", "metadata.provenance") from None
    d._keys(meta["parser"], ("name", "beautifulsoup4", "soupsieve", "python"), "metadata.parser")
    require(meta["parser"]["name"] == "html.parser", "metadata.parser.name")
    for key in ("beautifulsoup4", "soupsieve", "python"):
        require(matches(meta["parser"][key], r"[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+-]*)"), "metadata.parser." + key)
    require(type(meta["http_status"]) is int and 200 <= meta["http_status"] < 300, "metadata.http_status")
    d.scan_string(meta["content_type"], "metadata.content_type")
    if meta["charset"] is not None:
        d.scan_string(meta["charset"], "metadata.charset")
    validate_stored_recording(meta["recorded_extraction"], route_name)
    evidence = Evidence(route_name, fixture, metadata_bytes, meta)
    try:
        soup = evidence.soup()
    except UnicodeError:
        raise CaptureError("invalid_utf8", "fixture") from None
    validate_structure(soup)
    return evidence


def load_evidence(route_name, root):
    """Explicit historical route enumeration by callers makes a missing pair fail closed."""
    require(route_name in EXTRACTORS, "route")
    directory = Path(root) / route_name
    for filename, limit in (("fixture.html", HTML_LIMIT), ("metadata.json", METADATA_LIMIT)):
        path = directory / filename
        require(path.is_file(), f"{route_name}/{filename}.missing")
        require(path.stat().st_size <= limit, f"{route_name}/{filename}.size")
    return validate_integrity((directory / "fixture.html").read_bytes(),
                              (directory / "metadata.json").read_bytes(), route_name)


def admit(fixture, metadata_bytes, route_name, approval_bytes):
    """Validate layer A before parsing an optional approval or invoking D1."""
    evidence = validate_integrity(fixture, metadata_bytes, route_name)
    approval = d1_approval.NO_APPROVAL
    if approval_bytes is not None:
        if len(approval_bytes) > METADATA_LIMIT:
            raise CaptureError("d1_approval_schema", "approval.size")
        approval = load_json(approval_bytes, "approval")
    return d1_approval.check(evidence, approval)


def approval_path(reviews_root, evidence):
    """Use only the historical route and canonical capture id validated by A."""
    return Path(reviews_root) / evidence.route / f"{evidence.metadata['capture_id']}.json"


def read_approval(path):
    """Read a regular approval file, bounded by the metadata byte limit.

    Only a missing directory entry means absence. Failed reads (including a
    file disappearing after stat) and nonregular entries must refuse.
    """
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise CaptureError("d1_approval_unreadable", "approval") from None
    if not stat.S_ISREG(info.st_mode):
        raise CaptureError("d1_approval_unreadable", "approval")
    if info.st_size > METADATA_LIMIT:
        raise CaptureError("d1_approval_schema", "approval.size")
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        raise CaptureError("d1_approval_unreadable", "approval") from None
    if len(raw) > METADATA_LIMIT:
        raise CaptureError("d1_approval_schema", "approval.size")
    return raw
