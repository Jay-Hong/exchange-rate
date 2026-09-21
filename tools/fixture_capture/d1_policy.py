"""D1 §2 — element classification and the structures this policy refuses to read.

The policy spec is `design/c1b2/d1_detection_policy_v1.md`
(policy_spec_sha256 47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635).

Only §2 lives here: which tag names may appear, which text kinds may appear, and
which structures are refused outright. Producing detections is §3-§5
(`d1_observe`) and §6 (`d1_findings`).

Refusal is the mechanism for anything this policy cannot read. A structure we do
not support is rejected, never guessed at and never silently skipped: `get_text()`
drops `RubyTextString` and concatenates across block boundaries, so "we did not
see a title" and "there is no title" are different statements for such input.
"""

from bs4 import NavigableString, Tag

from .errors import CaptureError

# I — joined inside a block without ending the run (§2.1).
INLINE = frozenset("""
a abbr b bdi bdo big br cite code del dfn em font i img ins
kbd label mark q s samp small span strike strong sub sup time
tt u var wbr
""".split())

# B — ends the run (§2.1). This is the list that breaks runs here, not the web's
# visual block list: `button`, `input`, `option` and `meta` are classified that way.
BLOCK = frozenset("""
address article aside blockquote body button caption center col
colgroup dd details dialog dir div dl dt fieldset figcaption
figure footer form h1 h2 h3 h4 h5 h6 head header hgroup hr html
input legend li link main menu meta nav noscript ol optgroup
option p pre script section select style summary table tbody
td template textarea tfoot th thead title tr ul
""".split())

# U is the complement of I ∪ B, never an enumerated list. Namespaced vendor tags
# (`bsib:mnu`, seen in a captured bank page) land here through that definition —
# classification uses the parsed `tag.name` whole, so `vendor:span` is not span.
VOID = frozenset("br wbr img hr col input link meta".split())
INERT = frozenset("script style noscript template".split())


def classify(name):
    if name in INLINE:
        return "I"
    if name in BLOCK:
        return "B"
    return "U"


def is_empty_unsupported(element):
    """A U element carrying nothing at all, judged on the element AS GIVEN.

    ⛔ Callers must ask this BEFORE attribute/child cleanup. Cleanup strips
    attributes and removes comments and `meta`/`link`/`input` children, so a U
    element that carried any of those becomes indistinguishable from one that was
    always empty (measured: 4 of 6 such shapes end up empty). Asking afterwards
    would quietly widen this to elements we never inspected.
    """
    return (classify(element.name) == "U"
            and not element.attrs
            and not list(element.contents))


def validate_structure(soup):
    """§2 — refuse anything this policy cannot read. Raises, never repairs.

    Diagnostics carry a structural position and a policy-owned code, never the
    page's own bytes: a tag name is page-derived too, and an unsupported document
    can name its elements anything (`<x-person-홍길동>` was enough to leak a name
    through an earlier version of this function).
    """
    for index, node in enumerate(soup.descendants):
        where = f"fixture.nodes[{index}]"
        if isinstance(node, Tag):
            if classify(node.name) == "U":
                raise CaptureError("d1_unsupported_element", where)
            if node.name in VOID and list(node.contents):
                raise CaptureError("d1_invalid_empty_element", where)
            if node.name in INERT and list(node.contents):
                # Whitespace is a child too; a cleared inert element has none.
                raise CaptureError("d1_nonempty_inert_element", where)
        elif type(node) is not NavigableString:
            # `isinstance` is True for Comment, RubyTextString and the rest, so it
            # cannot express "plain text only" — the exact type is the contract.
            # The bs4 class name is ours, not the page's, so it may be named.
            raise CaptureError("d1_unsupported_text_kind", f"{where}.{type(node).__name__}")
