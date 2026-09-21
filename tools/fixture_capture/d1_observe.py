"""D1 §3-§5 — where text lives, how titles are spelled, and what was observed.

Spec: `d1_detection_policy_v1.md`
(policy_spec_sha256 47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635).

Nothing here reports what it found. An observation carries a token from a closed
list and a position; the text that produced it never leaves this module, because a
page can put a person's name anywhere a diagnostic might echo.

Selecting among observations — merge, longest match, numbering — is §6, in
`d1_findings`.
"""

import re
import unicodedata

from bs4 import NavigableString, Tag

from .d1_policy import classify, validate_structure
from .errors import CaptureError

# §4.1 — the whitespace this policy recognises, written out because Python's `\s`
# is a different set and a title may be split by a line break from `br`.
_W = r"[\x09-\x0D\x20\x85\xA0  -     　]"
_HYPHEN = r"(?:-|‐)"
_SEP = rf"(?:{_W}+|{_W}*{_HYPHEN}{_W}*)"


def normalize(text):
    """§4.1 — NFKC, then ASCII A-Z only. Not casefold: it folds far more."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in folded)


def _spaced(token):
    return (_W + r"*").join(re.escape(c) for c in token)


_CEO = rf"(?:{_spaced('ceo')}"                                  \
       rf"|c{_W}*\.{_W}*e{_W}*\.{_W}*o(?:{_W}*\.)?"             \
       rf"|c{_W}*{_HYPHEN}{_W}*e{_W}*{_HYPHEN}{_W}*o)"
_CHIEF_EXEC = rf"{_spaced('chief')}{_SEP}{_spaced('executive')}"

# §4.2 — a closed list. Order is the tie-break for equal spans (§6.2 step 3).
TITLES = (
    ("대표자", _spaced("대표자")),
    ("대표이사", _spaced("대표이사")),
    ("대표집행임원", _spaced("대표집행임원")),
    ("은행장", _spaced("은행장")),
    ("행장", _spaced("행장")),
    ("공동대표", _spaced("공동대표")),
    ("각자대표", _spaced("각자대표")),
    ("공동대표이사", _spaced("공동대표이사")),
    ("각자대표이사", _spaced("각자대표이사")),
    # English titles carry an explicit ASCII-letter boundary. Python's default `\b`
    # is Unicode-aware and counts 홍 as a word character, so `\bceo\b` finds nothing
    # in `CEO홍길동` — a title we must catch. Adding `re.ASCII` fixes that case but
    # is a different rule than the spec's, so the lookarounds are written out.
    ("ceo", rf"(?<![a-z]){_CEO}(?![a-z])"),
    ("chief executive", rf"(?<![a-z]){_CHIEF_EXEC}(?![a-z])"),
    ("chief executive officer", rf"(?<![a-z]){_CHIEF_EXEC}{_SEP}{_spaced('officer')}(?![a-z])"),
)
_COMPILED = tuple((token, re.compile(pattern)) for token, pattern in TITLES)


def candidates(normalized):
    """Every match at every start, for every token separately.

    §6 picks among these, so overlapping spellings must all reach it. Running each
    token's pattern separately is what preserves them: one merged alternation with
    `finditer` returns a single match per position and loses `공동대표이사` beside
    its parts. Per-token `finditer` happens to agree today — no token can overlap
    itself — but §4.2 requires the longest match not be decided by regex order,
    and one added token could break that coincidence.
    """
    found = set()
    for token, pattern in _COMPILED:
        for start in range(len(normalized) + 1):
            match = pattern.match(normalized, start)
            if match:
                found.add((match.start(), match.end(), token))
    return found


def _emit(runs, counters, owner_path, buffer):
    # §3.2 rule 8: an empty run is not emitted. The test is the joined text, not
    # the piece list — a zero-length text node is a piece, and emitting for it
    # would advance `segment_index` for a run nobody can observe.
    if not "".join(text for text, _ in buffer):
        return
    index = counters.get(tuple(owner_path), 0)
    counters[tuple(owner_path)] = index + 1
    runs.append((list(owner_path), index, list(buffer)))


def runs(soup):
    """§3.2 — one run per (owner block, stretch between child blocks).

    Text belongs to its nearest block ancestor; a child block ends the parent's
    current run and the parent resumes with a fresh one. Joining across a block
    boundary would erase the word boundary that `CEO` depends on, and never
    joining inline tags would miss a title split across them — both measured.

    §3.2 rule 9: an unsupported structure is refused here, not skipped. Without
    this the traversal treats a U tag as a block, so `대<x-vendor>표</x-vendor>자`
    splits into three runs and the title is never seen — a zero that means "could
    not read", reported as if it meant "not present". Any gate built on an empty
    result would then admit the page. The refusal belongs in the traversal
    (§3.2), not in a caller: this module has no caller yet, and a rule enforced
    only by a future one is not enforced.
    """
    validate_structure(soup)
    collected, counters = [], {}

    def walk(node, path, owner_path, buffer):
        for index, child in enumerate(node.contents):
            child_path = path + [index]
            # `type(...) is`, not isinstance: Comment and RubyTextString are
            # NavigableString subclasses, so isinstance would splice a comment's
            # text into the run. validate_structure has already refused those,
            # and this keeps the two statements of the same rule from drifting.
            if type(child) is NavigableString:
                buffer.append((str(child), child_path))
                continue
            if not isinstance(child, Tag):
                continue
            if classify(child.name) == "I":
                if child.name == "br":
                    buffer.append(("\n", None))
                elif child.name not in ("wbr", "img"):
                    walk(child, child_path, owner_path, buffer)
                continue
            _emit(collected, counters, owner_path, buffer)
            buffer.clear()
            child_buffer = []
            walk(child, child_path, child_path, child_buffer)
            _emit(collected, counters, child_path, child_buffer)

    buffer = []
    walk(soup, [], [], buffer)
    _emit(collected, counters, [], buffer)
    return collected


def observe(soup):
    """§5 — node and run observations, each needed for what the other misses.

    A run finds a title split across inline tags; a node finds one whose English
    boundary the neighbouring text destroyed. Returns dicts with a token and
    positions only.

    An unsupported structure raises rather than returning an empty list; `runs`
    refuses it on entry (§3.2 rule 9). A caller must therefore not read an
    exception, a timeout or a skipped call as "nothing found".
    """
    observations = []
    for owner_path, segment_index, pieces in runs(soup):
        raw = "".join(text for text, _ in pieces)
        normalized = normalize(raw)
        # §4.3 — normalization must distribute over the pieces, or the offsets
        # below would silently point at the wrong characters. NFKC can compose
        # across a boundary (`ᄃ` + `ᅢ` becomes `대`), so this is checked every
        # time, including when nothing matched.
        if normalized != "".join(normalize(text) for text, _ in pieces):
            raise CaptureError("d1_normalization_boundary_unsupported",
                               f"fixture.run[{'.'.join(map(str, owner_path))}][{segment_index}]")
        where = (list(owner_path), segment_index)
        for start, end, token in candidates(normalized):
            observations.append({"token": token, "source": "run", "run": where,
                                 "start": start, "end": end,
                                 "node_path": None, "node_start": None, "node_end": None})
        offset = 0
        for text, node_path in pieces:
            piece = normalize(text)
            if node_path is not None:
                for start, end, token in candidates(piece):
                    # Both coordinate systems are kept: the run position is what §6
                    # merges and orders by, and the node-internal position is what
                    # §7 checks a substitution against.
                    observations.append({"token": token, "source": "node", "run": where,
                                         "start": offset + start, "end": offset + end,
                                         "node_path": list(node_path),
                                         "node_start": start, "node_end": end})
            offset += len(piece)
    return observations
