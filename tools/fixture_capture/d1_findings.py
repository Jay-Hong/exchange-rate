"""D1 §1.2 and §6 — independent sources and final approval identifiers.

Spec: `d1_detection_policy_v1.md`
(policy_spec_sha256 47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635).

The slice 3 contract amends §6.1: attribute_index replaces attribute_name. It
indexes ALL parsed names in code point order, before normalization or detection.
Names are page text, so copying one into an identifier can disclose a person.

Metadata must have passed the caller's closed schema (§1.2); this module checks
the JSON tree types but cannot establish that its keys are schema-owned. Only
values are scanned. No input is repaired, joined across scalars, or exempted.
"""

import math
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from .d1_observe import TITLES, candidates, normalize, observe
from .errors import CaptureError

_TITLE_ORDER = {token: index for index, (token, _) in enumerate(TITLES)}
_RULE = "d1_role_context"


@dataclass(frozen=True)
class NodeEvidence:
    """One exact node observation, in normalized node-local coordinates."""

    node_path: tuple[int, ...]
    node_start: int
    node_end: int


@dataclass(frozen=True)
class TextEvidence:
    """Memory-only provenance for a selected HTML text finding, without text."""

    sources: frozenset[str]
    nodes: tuple[NodeEvidence, ...]


def select_longest(candidates):
    """§6.2: leftmost, then longest, then table order, within ONE run/scalar.

    A rejected overlap does not extend the selected interval: otherwise a chain
    of overlaps could discard a later, disjoint occurrence. Touching is allowed.
    The input consists of (start, end, canonical token) tuples and is not changed.
    """
    ordered = sorted(candidates, key=lambda c: (c[0], -c[1], _TITLE_ORDER[c[2]]))
    selected = []
    end = 0
    for candidate in ordered:
        if candidate[0] >= end:
            selected.append(candidate)
            end = candidate[1]
    return selected


def _finding(source, token, start, end, **position):
    return {"rule_id": _RULE, "source": source, **position,
            "token": token, "start": start, "end": end}


def _text_findings(soup):
    # observe emits each run's observations together in run emission order.
    # Insertion order therefore preserves that order even for node-only hits;
    # sorting owner paths would incorrectly move resumed parent runs forward.
    grouped = {}
    for observation in observe(soup):
        owner_path, segment_index = observation["run"]
        run = (tuple(owner_path), segment_index)
        span = (observation["start"], observation["end"], observation["token"])
        grouped.setdefault(run, {}).setdefault(span, []).append(observation)

    result = []
    for (owner_path, segment_index), merged in grouped.items():
        for start, end, token in select_longest(merged):
            observations = merged[(start, end, token)]
            # Retain only evidence for this exact span/token. A shorter node
            # observation inside a run match cannot prove the whole match was
            # produced by a replacement in that node (§7).
            nodes = sorted({(tuple(o["node_path"]), o["node_start"], o["node_end"])
                            for o in observations if o["source"] == "node"})
            evidence = TextEvidence(
                frozenset(o["source"] for o in observations),
                tuple(NodeEvidence(*node) for node in nodes),
            )
            result.append((_finding("html_text", token, start, end,
                                    owner_path=list(owner_path),
                                    segment_index=segment_index), evidence))
    return result


def _elements(soup):
    # §3.1 uses contents indices, including text, unlike recorder.element_path.
    pending = [(node, [index]) for index, node in reversed(list(enumerate(soup.contents)))
               if isinstance(node, Tag)]
    while pending:
        element, path = pending.pop()
        yield element, path
        pending.extend((node, path + [index])
                       for index, node in reversed(list(enumerate(element.contents)))
                       if isinstance(node, Tag))


def _attribute_scalars(soup):
    for element, path in _elements(soup):
        where = f"fixture.element[{'.'.join(map(str, path))}].attributes"
        if not isinstance(element.attrs, dict) or any(
                not isinstance(name, str) for name in element.attrs):
            raise CaptureError("d1_invalid_attribute", where)
        for attribute_index, name in enumerate(sorted(element.attrs)):
            position = {"element_path": path, "attribute_index": attribute_index}
            yield {**position, "part": "name", "list_index": None}, name
            value = element.attrs[name]
            location = f"{where}[{attribute_index}].value"
            # bs4's AttributeValueList and charset values are list/str subclasses.
            if isinstance(value, str):
                yield {**position, "part": "value", "list_index": None}, value
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if not isinstance(item, str):
                        raise CaptureError("d1_invalid_attribute", f"{location}[{index}]")
                    yield {**position, "part": "value", "list_index": index}, item
            else:
                raise CaptureError("d1_invalid_attribute", location)


def _attribute_findings(soup):
    result = []
    for position, text in _attribute_scalars(soup):
        for start, end, token in select_longest(candidates(normalize(text))):
            result.append(_finding("html_attribute", token, start, end, **position))
    return result


def _metadata_scalars(metadata):
    if type(metadata) is not dict:
        raise CaptureError("d1_invalid_metadata", "metadata")
    active = set()

    def walk(value, path, where):
        kind = type(value)
        if kind is str:
            yield path, value
        elif kind in (dict, list):
            # Only ancestors count as cycles: a shared container at two paths
            # represents two independent fields and must be scanned twice.
            identity = id(value)
            if identity in active:
                raise CaptureError("d1_invalid_metadata", where)
            active.add(identity)
            try:
                if kind is dict:
                    if any(type(key) is not str for key in value):
                        raise CaptureError("d1_invalid_metadata", where)
                    children = ((key, value[key]) for key in sorted(value))
                else:
                    children = enumerate(value)
                for ordinal, (component, child) in enumerate(children):
                    # Diagnostic paths use numeric positions, even for objects.
                    # An invalid input has not earned the schema's key guarantee.
                    yield from walk(child, path + [component], f"{where}[{ordinal}]")
            finally:
                active.remove(identity)
        elif value is None or kind in (bool, int):
            return
        elif kind is float and math.isfinite(value):
            return
        else:
            raise CaptureError("d1_invalid_metadata", where)

    # This DFS is typed-path lexicographic order: siblings are either all string
    # keys in code point order or all integer indices in numeric order. A leaf
    # cannot also be an ancestor, so no other prefix ordering is needed.
    yield from walk(metadata, [], "metadata")


def _metadata_findings(metadata):
    result = []
    for path, text in _metadata_scalars(metadata):
        selected = select_longest(candidates(normalize(text)))
        for occurrence, (start, end, token) in enumerate(selected, 1):
            result.append(_finding("metadata", token, start, end, path=path,
                                   field_occurrence=occurrence, field_count=len(selected)))
    return result


def findings_with_evidence(soup, metadata):
    """Return (closed finding dicts, memory-only text evidence by final index).

    Metadata keys must already be trusted by the caller's closed schema. Evidence
    is tied to this call's selected findings, never cached or added to an approval
    dict. It preserves exact node/run sources for §7, but is not itself proof of
    a successful replacement. Attribute/metadata findings have no node evidence.
    """
    if not isinstance(soup, BeautifulSoup):
        raise CaptureError("d1_invalid_root", "fixture")
    try:
        text_items = _text_findings(soup)
        result = [finding for finding, _ in text_items]
        result.extend(_attribute_findings(soup))
        result.extend(_metadata_findings(metadata))
    except RecursionError:
        # A deeply nested input is unreadable, not a successful empty inspection.
        raise CaptureError("d1_structure_depth_unsupported", "capture") from None

    # No partial list or global numbering escapes if a later source is invalid.
    for index, finding in enumerate(result, 1):
        finding["occurrence_index"] = index
        finding["total_count"] = len(result)
    evidence = {index: item[1] for index, item in enumerate(text_items, 1)}
    return result, evidence


def findings(soup, metadata):
    """Return §6.4's closed variants; inspection failures propagate as errors."""
    return findings_with_evidence(soup, metadata)[0]
