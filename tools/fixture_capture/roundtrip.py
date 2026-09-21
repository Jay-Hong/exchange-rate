"""Full extraction events and actual query paths must survive serialization."""

import math
import time
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup, Tag

from . import d1_replace
from .deidentify import deidentify
from .errors import CaptureError
from .limits import HTML_LIMIT, PARSE_SECONDS, wall_timeout
from .recorder import QueryRecorder, element_path
from .runtime import quiet_logging

PARSER = "html.parser"
EVENT_KINDS = frozenset(("selector_miss", "item_miss", "parse_error", "observed",
                         "loop_completed", "table_structure", "code_outside_required", "empty_value"))


def _value(value):
    if isinstance(value, Tag):
        return {"element_path": element_path(value)}
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    if type(value) in (tuple, list):
        return [_value(v) for v in value]
    if type(value) is dict and all(type(k) is str for k in value):
        return {key: _value(v) for key, v in value.items()}
    raise CaptureError("unrecordable_value", "recording")


def _exception(error):
    frames = []
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        filename = Path(code.co_filename)
        if filename.parent.name == "crawlers":
            frames.append([filename.name, code.co_name, tb.tb_lineno])
        tb = tb.tb_next
    return {"type": type(error).__name__, "args": _value(error.args), "site": frames}


class EventRecorder:
    def __init__(self, registry):
        self.registry = registry
        self.events = []
        self.incomplete = False

    def __call__(self, kind, **facts):
        try:
            if kind not in EVENT_KINDS or (self.events and self.events[-1]["kind"] == "loop_completed"):
                raise CaptureError("event_contract", "recording")
            event = {"kind": kind, "facts": _value(facts), "labels": {}}
            self.events.append(event)
            element, item, row = (facts.get(k) for k in ("element", "item", "row"))
            if any(node is not None for node in (element, item, row)):
                # Reuse production label lookup logic, but never its clipping or
                # BankReport observation limits. Only _snippet is replaced here.
                event["labels"] = _value(self.registry.sources.bank_report._label_candidates(element, item, row))
            if kind == "table_structure":
                table = facts["tbody"].find_parent("table")
                header = table.select_one(self.registry.sources.utils.MIBANK_HEADER_ROW_SELECTOR) if table else None
                event["labels"]["header_text"] = header.get_text(" ", strip=True) if header else None
        except Exception:
            self.incomplete = True
            raise CaptureError("incomplete_events", "recording") from None


def record_extraction(soup, route, registry):
    events = EventRecorder(registry)
    returned, exception = None, None
    with quiet_logging(), QueryRecorder(registry) as queries:
        # Full get_text strings, without even whitespace normalization by _snippet.
        with patch.object(registry.sources.bank_report, "_snippet", lambda text: text):
            try:
                returned = _value(registry.extract(route, soup, events))
            except CaptureError:
                raise
            except Exception as error:
                exception = _exception(error)
        queries.assert_valid()
    if events.incomplete or (exception is None and (
            not events.events or events.events[-1]["kind"] != "loop_completed")):
        raise CaptureError("incomplete_events", "recording")
    return {"events": events.events, "queries": queries.calls,
            "returned": returned, "exception": exception}


def parse_html(text, deadline, parse_budget=None):
    # Share the caller's remaining budget across every parser invocation.
    seconds = PARSE_SECONDS if parse_budget is None else parse_budget[0]
    started = time.monotonic()
    with wall_timeout(min(seconds, deadline.remaining()), "parse_timeout"):
        soup = BeautifulSoup(text, PARSER)
    if parse_budget is not None:
        parse_budget[0] -= time.monotonic() - started
    deadline.remaining()
    return soup


def roundtrip(text, route, registry, deadline, parse_budget=None):
    """Return verified bytes, reparsed soup, original record and D1 evidence.

    Matching extraction exceptions can be useful fixtures: args, last event and
    production traceback sites must match. Unexpected/unserializable records or
    lost callbacks are refused. Acceptance does not assert valid exchange rates.
    """
    if parse_budget is None:
        parse_budget = [PARSE_SECONDS]
    soup = parse_html(text, deadline, parse_budget)
    original = record_extraction(soup, route, registry)
    deadline.remaining()
    fixture = deidentify(soup, registry)
    replacements = d1_replace.replace_names(fixture, route.name, original)
    serialized = fixture.encode("utf-8")
    if len(serialized) > HTML_LIMIT:
        raise CaptureError("fixture_size", "fixture")
    d1_replace.verify_stored(serialized, replacements,
                             parse=lambda text: parse_html(text, deadline, parse_budget))
    reparsed = parse_html(serialized.decode("utf-8"), deadline, parse_budget)
    replayed = record_extraction(reparsed, route, registry)
    for field in ("events", "queries", "returned", "exception"):
        if original[field] != replayed[field]:
            raise CaptureError("roundtrip_mismatch", f"recorded_extraction.{field}")
    deadline.remaining()
    return serialized, reparsed, original, replacements
