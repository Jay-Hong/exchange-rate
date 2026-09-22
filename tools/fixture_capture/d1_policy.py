"""D1 §2 — element classification and the structures this policy refuses to read.

The policy spec is `design/c1b2/d1_detection_policy_v1.md`
(policy_spec_sha256 47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635).

§2 lives here: which tag names may appear, which text kinds may appear, and
which structures are refused outright. Amendment 2 §A4 also defines which empty
U elements cleanup may remove. Producing detections is §3-§5
(`d1_observe`) and §6 (`d1_findings`).

Refusal is the mechanism for anything this policy cannot read. A structure we do
not support is rejected, never guessed at and never silently skipped: `get_text()`
drops `RubyTextString` and concatenates across block boundaries, so "we did not
see a title" and "there is no title" are different statements for such input.
"""

from types import MappingProxyType

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

# Amendment 2 §A4: exact parsed names and full attribute-name sets. Changing this
# table is a policy change; neither the mapping nor its values may be mutated.
REVIEWED_EMPTY_UNSUPPORTED = MappingProxyType({
    "bsib:mnu": frozenset(("currentwidget", "menu", "targetwidget")),
})


def classify(name):
    if name in INLINE:
        return "I"
    if name in BLOCK:
        return "B"
    return "U"


def is_empty_unsupported(element):
    """Amendment 2 §A4 removal candidate, judged on the element AS GIVEN.

    A U element must have no children and either no attributes (the slice-4B
    rule) or an exact reviewed name/attribute-name set. Values are not read.
    `deidentify` still excludes ruby and enforces the protected boundary.

    ⛔ Callers must ask this BEFORE attribute/child cleanup. Cleanup strips
    attributes and removes comments and `meta`/`link`/`input` children, so a U
    element can become indistinguishable from one that qualified originally.
    Decide all candidates before removing any of them, so removal cannot cascade
    to a parent emptied by cleanup. Every remaining U is still refused by §2.1.
    """
    return (classify(element.name) == "U"
            and not list(element.contents)
            and (not element.attrs
                 or frozenset(element.attrs) == REVIEWED_EMPTY_UNSUPPORTED.get(element.name)))


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
