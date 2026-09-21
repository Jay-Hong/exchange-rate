"""D1 slice 4A: reviewed name replacement and execution-local evidence (§7).

Spec: d1_detection_policy_v1.md
(policy_spec_sha256 47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635).
Capture calls it (slice 5a: roundtrip and capture_route's final check); admission
does not yet (slice 5b). Evidence stays in memory;
it is neither an approval record nor a substitute for inspecting stored bytes.
"""

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

from .d1_findings import _metadata_scalars, findings_with_evidence
from .d1_observe import _W, normalize
from .errors import CaptureError

PLACEHOLDER = "[성명삭제]"
_SPACE = re.compile(_W)


@dataclass(frozen=True)
class Entry:
    route: str
    selector: str
    label: str


# Independently selected on both 2026-09-19 captures: exactly one li, at contents
# path (1, 3, 3, 9, 9, 1, 3, 1). CSS nth-child counts elements, not text nodes.
# This disclosure registry is deliberately separate from extraction selectors.
_CITI_DISCLOSURE = (
    "html:nth-child(1) > body:nth-child(2) > div:nth-child(2) > "
    "footer:nth-child(5) > div:nth-child(5) > div:nth-child(1) > "
    "ul:nth-child(2) > li:nth-child(1)"
)
REGISTRY = (
    Entry("citi_primary", _CITI_DISCLOSURE, "대표자"),
    Entry("citi_secondary", _CITI_DISCLOSURE, "대표자"),
)


@dataclass(frozen=True, eq=False)
class Replacement:
    """One successful edit, with the new node's identity and storage binding.

    No removed value is retained. Suppress DOM/text reprs because a node holds
    references to the rest of its page. Equality must not use bs4 value equality.
    """

    node: NavigableString = field(repr=False)
    path: tuple[int, ...]
    ancestors: tuple[str, ...]
    label: str
    label_start: int
    label_end: int
    text: str = field(repr=False)


Replacements = tuple[Replacement, ...]


def _where(path):
    return f"fixture.node[{'.'.join(map(str, path))}]"


def _location(soup, node):
    path, ancestors = [], []
    while node is not soup:
        parent = node.parent
        if not isinstance(parent, Tag):
            raise CaptureError("d1_replacement_correspondence", "fixture")
        # list.index uses ==, which aliases distinct, equal bs4 nodes.
        index = next((i for i, child in enumerate(parent.contents) if child is node), None)
        if index is None:
            raise CaptureError("d1_replacement_correspondence", "fixture")
        path.append(index)
        if parent is not soup:
            ancestors.append(parent.name)
        node = parent
    return tuple(reversed(path)), tuple(reversed(ancestors))


def _resolve(soup, path):
    node = soup
    for index in path:
        if not isinstance(node, Tag) or not 0 <= index < len(node.contents):
            raise CaptureError("d1_replacement_correspondence", _where(path))
        node = node.contents[index]
    return node


def _single_text(element, where):
    if (element.name != "li" or len(element.contents) != 1
            or type(element.contents[0]) is not NavigableString):
        raise CaptureError("d1_replacement_structure", where)
    return element.contents[0]


def _check_boundary(label, separator, value, where):
    if normalize(label + separator + value) != (
            normalize(label) + normalize(separator) + normalize(value)):
        raise CaptureError("d1_normalization_boundary_unsupported", where)


def _replacement_text(text, label, where):
    # W is the policy's exact set. Neither strip nor Python \s expresses it.
    match = re.fullmatch(rf"{re.escape(label)}({_W}+)(.*)", text, re.DOTALL)
    if match is None:
        raise CaptureError("d1_replacement_boundary", where)
    separator, value = match.groups()
    normalized_value = normalize(value)
    if (not value or not normalized_value or _SPACE.search(value)
            or _SPACE.search(normalized_value)
            or normalized_value == normalize(PLACEHOLDER)):
        raise CaptureError("d1_replacement_boundary", where)
    _check_boundary(label, separator, value, where)
    _check_boundary(label, separator, PLACEHOLDER, where)
    return label + separator + PLACEHOLDER, value


def _check_residual(recorded_extraction, value, where):
    normalized_value = normalize(value)
    # Reuse §1.2's JSON value traversal: keys are not value leaves; cycles and
    # invalid trees refuse with numeric diagnostics. Never repair the record.
    for _, text in _metadata_scalars(recorded_extraction):
        if value in text or normalized_value in normalize(text):
            raise CaptureError("d1_removed_value_in_extraction", where)


def _bound_node(soup, replacement, require_identity):
    node = _resolve(soup, replacement.path)
    where = _where(replacement.path)
    if (type(node) is not NavigableString
            or (require_identity and node is not replacement.node)
            or not isinstance(node.parent, Tag)
            or node.parent.name != "li"
            or len(node.parent.contents) != 1
            or node.parent.contents[0] is not node
            or str(node) != replacement.text
            or _location(soup, node) != (replacement.path, replacement.ancestors)
            or not str(node).startswith(replacement.label)
            or replacement.label_start != 0
            or replacement.label_end != len(normalize(replacement.label))
            or normalize(str(node))[replacement.label_start:replacement.label_end]
            != normalize(replacement.label)):
        raise CaptureError("d1_replacement_correspondence", where)
    return node


def _check_correspondence(soup, replacements, metadata, *, require_identity=True):
    # Bind every edit even if a changed stored node now has no D1 findings.
    bound = [(_bound_node(soup, item, require_identity), item) for item in replacements]
    findings, evidence = findings_with_evidence(soup, metadata)
    for finding in findings:
        observation = evidence.get(finding["occurrence_index"])
        if (finding["source"] != "html_text" or observation is None
                or "node" not in observation.sources):
            raise CaptureError("d1_replacement_correspondence", "fixture")
        owned = False
        for span in observation.nodes:
            observed_node = _resolve(soup, span.node_path)
            if any(observed_node is node and span.node_path == item.path
                   and item.label_start <= span.node_start < span.node_end <= item.label_end
                   for node, item in bound):
                owned = True
                break
        if not owned:
            raise CaptureError("d1_replacement_correspondence", "fixture")


def replace_names(soup, route, recorded_extraction, *, entries=None) -> Replacements:
    """Replace reviewed single-node li values in place, then check all findings.

    A refusal can leave the soup edited; callers must discard it on failure.
    recorded_extraction is only read. Removed values never enter the evidence.
    entries=None uses REGISTRY; an explicit empty iterable grants no edits.
    """
    if not isinstance(soup, BeautifulSoup):
        raise CaptureError("d1_invalid_root", "fixture")
    replaced = []
    try:
        for index, entry in enumerate(REGISTRY if entries is None else entries):
            where = f"registry.entries[{index}]"
            if (not isinstance(entry, Entry)
                    or not all(type(value) is str and value
                               for value in (entry.route, entry.selector, entry.label))):
                raise CaptureError("d1_invalid_replacement_entry", where)
            if entry.route != route:
                continue
            try:
                matches = soup.select(entry.selector)
            except Exception:
                # Selector errors can echo their input. Only this parser boundary
                # is translated; no parser error is treated as an empty match.
                raise CaptureError("d1_replacement_selector", where) from None
            if len(matches) != 1:
                raise CaptureError("d1_replacement_selector", where)
            node = _single_text(matches[0], where)
            path, ancestors = _location(soup, node)
            where = _where(path)
            text, value = _replacement_text(str(node), entry.label, where)
            _check_residual(recorded_extraction, value, where)
            new_node = NavigableString(text)
            node.replace_with(new_node)
            replaced.append(Replacement(new_node, path, ancestors, entry.label,
                                        0, len(normalize(entry.label)), text))
        result = tuple(replaced)
        _check_correspondence(soup, result, {})
    except RecursionError:
        raise CaptureError("d1_structure_depth_unsupported", "capture") from None
    return result


def verify_stored(fixture_bytes, replacements, metadata=None, *, parse=None) -> None:
    """Reparse actual UTF-8 bytes, rebind every edit, and recompute all of D1.

    Rebinding permits new Python identities only after exact path, ancestors,
    full text, single-node li and label-range checks. It never searches for text.
    metadata must already have passed the caller's closed schema (§1.2).
    An optional parse(text) supplies the caller's budgeted BeautifulSoup parse.
    """
    if type(fixture_bytes) is not bytes:
        raise CaptureError("d1_invalid_fixture_bytes", "fixture")
    try:
        text = fixture_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise CaptureError("d1_invalid_utf8", "fixture") from None
    try:
        soup = BeautifulSoup(text, "html.parser") if parse is None else parse(text)
    except Exception:
        # DeadlineExpired inherits BaseException and passes through unchanged.
        raise CaptureError("d1_fixture_parse", "fixture") from None
    if parse is not None and not isinstance(soup, BeautifulSoup):
        raise CaptureError("d1_fixture_parse", "fixture")
    _check_correspondence(soup, replacements, {} if metadata is None else metadata,
                          require_identity=False)
