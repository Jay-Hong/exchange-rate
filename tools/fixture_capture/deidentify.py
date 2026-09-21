"""Value-level deidentification; structural changes still require roundtrip proof."""

import copy
import re
from urllib.parse import parse_qsl, urlparse

from bs4 import Comment, Doctype, ProcessingInstruction, Tag

from .d1_policy import is_empty_unsupported
from .errors import CaptureError

EMPTY_BODY_TAGS = frozenset(("script", "style", "noscript", "template"))
REMOVE_OUTSIDE = EMPTY_BODY_TAGS | frozenset(("meta", "link", "input"))
# Kept refusable on purpose: D1 §2.3 rejects every ruby structure, empty ones too,
# so removing an empty one here would quietly convert a refusal into a pass.
RUBY = frozenset(("ruby", "rb", "rt", "rtc", "rp"))
_CODE = re.compile(r"[A-Za-z]{3}\Z", re.ASCII)
_FLAG = re.compile(r"flag_([a-z]{3})(_|\.)", re.IGNORECASE)


def _boundary(soup, registry):
    """Selected subtrees, ancestors and their element siblings; tables include heads.

    Protecting every sibling along the ancestor chain preserves numeric paths
    as well as nth-child. Unselected sibling *contents* need not be protected.
    All registered selectors are considered, even when this route did not use
    them. That is conservative and is followed by independent identifier scans.
    """
    protected = set()
    selected = [element for selector in registry.selectors for element in soup.select(selector)]
    for element in selected:
        protected.add(id(element))
        protected.update(id(child) for child in element.descendants if isinstance(child, Tag))
        for ancestor in element.parents:
            protected.add(id(ancestor))
            protected.update(id(child) for child in ancestor.children if isinstance(child, Tag))
            if ancestor.name == "table":
                protected.update(id(child) for child in ancestor.descendants if isinstance(child, Tag))
    return protected


def _attributes(element, registry):
    kept = {}
    for name in ("colspan", "rowspan"):
        if name in element.attrs:
            value = element.attrs[name]
            if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]+", value.strip()):
                raise CaptureError("span_out_of_range", "fixture.attributes")
            number = int(value)
            if not 1 <= number <= 20:
                raise CaptureError("span_out_of_range", "fixture.attributes")
            kept[name] = value
    if element.get("id") in registry.ids:
        kept["id"] = element["id"]
    classes = [token for token in element.get("class", []) if token in registry.classes]
    if classes:
        kept["class"] = classes
    if element.name == "a" and "href" in element.attrs:
        values = [value for key, value in parse_qsl(urlparse(element["href"]).query,
                                                   keep_blank_values=True) if key == "currency"]
        if values and all(value == "" or _CODE.fullmatch(value) for value in values):
            kept["href"] = "?" + "&".join("currency=" + value for value in values)
    if element.name == "img" and "src" in element.attrs:
        match = _FLAG.search(element["src"])
        if match:
            kept["src"] = "flag_" + match.group(1) + match.group(2)
    element.attrs = kept


def deidentify(soup, registry):
    try:
        fixture = copy.deepcopy(soup)
        protected = _boundary(fixture, registry)
        # Validate attributes even on elements about to be removed.
        elements = [node for node in fixture.descendants if isinstance(node, Tag)]
        # Decided BEFORE any cleanup: cleanup strips attributes and removes
        # comment/meta/link/input children, after which an element that carried any
        # of those is indistinguishable from one that was always empty (measured on
        # 6 shapes, 4 of them end up empty). Asking later would widen this silently.
        empty_unsupported = [element for element in elements
                             if is_empty_unsupported(element) and element.name not in RUBY]
        for element in elements:
            _attributes(element, registry)
        for node in list(fixture.descendants):
            if isinstance(node, (Comment, Doctype, ProcessingInstruction)):
                node.extract()
        # Reverse order means descendants are processed before clear/decompose.
        for element in reversed(elements):
            if element.name in EMPTY_BODY_TAGS:
                element.clear()
            if element.name in REMOVE_OUTSIDE and id(element) not in protected:
                element.decompose()
        # Same boundary rule as every other removal. Only the elements listed before
        # cleanup qualify, so nothing that merely became empty above is touched and
        # no parent is removed for having lost its children.
        for element in empty_unsupported:
            if id(element) not in protected:
                element.decompose()
        return fixture
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("deidentify_failed") from None
