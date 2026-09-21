"""Closed DOM query contract derived from production selector constants."""

import re
from dataclasses import dataclass

from .errors import CaptureError
from .queries import query_key  # re-exported: registry.query_key is queries.query_key
from .runtime import load_sources


_IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"
_TAG = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_TOKEN = re.compile(
    rf'(?P<id>#{_IDENT})|(?P<class>\.{_IDENT})|'
    r'(?P<nth>:nth-child\([1-9][0-9]*\))|'
    rf'''(?P<attr>\[{_IDENT}\*=(?:"[^"\\\]\r\n]*"|'[^'\\\]\r\n]*')\])''')


def selector_tokens(selector):
    """Parse only the agreed positive CSS grammar, consuming every character."""
    if not isinstance(selector, str) or not selector or selector != selector.strip():
        raise CaptureError("selector_syntax", "registry")
    ids, classes = set(), set()
    pos = 0
    while pos < len(selector):
        start = pos
        tag = _TAG.match(selector, pos)
        if tag:
            pos = tag.end()
        while pos < len(selector):
            token = _TOKEN.match(selector, pos)
            if not token:
                break
            if token.lastgroup == "id":
                ids.add(token.group()[1:])
            elif token.lastgroup == "class":
                classes.add(token.group()[1:])
            pos = token.end()
        if pos == start:
            raise CaptureError("selector_syntax", "registry")
        if pos == len(selector):
            break
        space_start = pos
        while pos < len(selector) and selector[pos].isspace():
            pos += 1
        had_space = pos > space_start
        if pos < len(selector) and selector[pos] == ">":
            pos += 1
            while pos < len(selector) and selector[pos].isspace():
                pos += 1
        elif not had_space:
            raise CaptureError("selector_syntax", "registry")
        if pos == len(selector):
            raise CaptureError("selector_syntax", "registry")
    return frozenset(ids), frozenset(classes)


@dataclass(frozen=True)
class Route:
    name: str
    url: str
    extractor: str
    selectors: object = None


class Registry:
    def __init__(self, sources=None):
        self.sources = sources or load_sources()
        bs, citi, utils = self.sources.bs, self.sources.citi, self.sources.utils
        self.selectors = tuple(dict.fromkeys((
            *bs.BS_BANK_SELECTORS.values(), *citi.CITI_BANK_SELECTORS.values(),
            citi.AFTER_CITI_BANK_SELECTORS, *citi.SECOND_CITI_BANK_SELECTORS.values(),
            *utils.MIBANK_TABLE_SELECTORS, *utils.MIBANK_RATE_CELL_SELECTORS,
            utils.MIBANK_CODE_LINK_SELECTOR, utils.MIBANK_FLAG_IMAGE_SELECTOR,
            utils.MIBANK_HEADER_ROW_SELECTOR, utils.MIBANK_COUNTER_SELECTOR,
        )))
        parsed = [selector_tokens(s) for s in self.selectors]
        self.ids = frozenset().union(*(p[0] for p in parsed))
        self.classes = frozenset().union(*(p[1] for p in parsed))
        # bank_report's inline "thead tr" equals the shared production constant.
        # Its real call is checked at runtime; no app edit is necessary.
        calls = [("select_one", (s,), {}) for s in self.selectors]
        calls += [("select", (s,), {}) for s in utils.MIBANK_RATE_CELL_SELECTORS]
        calls += [("find_all", ("tr",), {}),
                  ("find_all", ("td",), {"recursive": False}),
                  ("find_all", (["th", "td"],), {"recursive": False}),
                  ("find_all", (["td", "th"],), {"recursive": False}),
                  ("find_parent", ("table",), {}),
                  ("find_parent", ("tr",), {}),
                  ("find_parent", (["td", "th"],), {})]
        self.allowed_queries = frozenset(query_key(*call) for call in calls)
        self.evidence_selectors = frozenset(self.selectors) | frozenset(
            s + citi.AFTER_CITI_BANK_SELECTORS for s in citi.CITI_BANK_SELECTORS.values())
        self.routes = {r.name: r for r in (
            Route("bs_official", bs.BS_BANK_URL, "selector", bs.BS_BANK_SELECTORS),
            Route("citi_primary", citi.CITI_BANK_URL, "citi", citi.CITI_BANK_SELECTORS),
            Route("citi_secondary", citi.SECOND_CITI_BANK_URL, "selector", citi.SECOND_CITI_BANK_SELECTORS),
            Route("bs_mibank", bs.MIBANK_BS_URL, "mibank"),
            Route("citi_mibank", citi.MIBANK_CITI_URL, "mibank"),
        )}

    def extract(self, route, soup, on_event):
        if route.extractor == "selector":
            return self.sources.utils.extract_selector_rates(soup, route.selectors, on_event)
        if route.extractor == "citi":
            return self.sources.citi.extract_citi_items(soup, route.selectors, on_event)
        if route.extractor == "mibank":
            return self.sources.utils.extract_mibank_rates(
                soup, self.sources.constants.MIBANK_REQUIRED_CODES, on_event)
        raise CaptureError("unknown_route", "registry")
