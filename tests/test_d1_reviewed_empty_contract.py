"""D1 slice 5c-3a-B contract — amendment 2 §A4: the reviewed empty unsupported elements cleanup may remove.

Written before the implementation (Claude) from `d1_detection_policy_v1_amendment2.txt` §A4 (under
`tools/fixture_capture/d1_spec/`) and the 5c-3a-B design agreement with Codex (2026-09-22). The implementer reads this file
and does not edit it.

Background: the 2026-09-22 re-capture of `bs_official` was refused with `d1_unsupported_element`. The page carries two
`bsib:mnu` elements with three attributes and no children, each the only element child of a `form` outside the protected
boundary (measured, structure only). The slice-4B removal takes only U elements with no attributes and no children, judged
before cleanup, so these stayed — attribute-less after cleanup — and §2.1 refused them.

API (module `tools/fixture_capture/d1_policy.py`):
- `REVIEWED_EMPTY_UNSUPPORTED` — a read-only mapping, exactly `{"bsib:mnu": frozenset({"currentwidget", "menu",
  "targetwidget"})}`, equal to the §A4 table. Widening it is a policy change (a new amendment and a new digest).
- `is_empty_unsupported(element)` — True iff the element is U, has no contents, and either has no attributes or its name is
  in the mapping and the frozenset of its parsed attribute names equals that entry. Attribute values are not read. Judged on
  the element as given (callers still ask before cleanup); ruby stays excluded by `deidentify`.
Module `tools/fixture_capture/d1_digest.py`: `AMENDMENT_PATHS` gains amendment 2 after amendment 1.
Nothing else changes: the protected boundary, the no-cascade rule and §2.1's refusal of every remaining U stay as they are.
"""

import hashlib
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from tests._fixture_capture import getter, official_html
from tools.fixture_capture import d1_digest as G
from tools.fixture_capture import d1_policy as P
from tools.fixture_capture.capture import capture_route
from tools.fixture_capture.d1_policy import classify, is_empty_unsupported, validate_structure
from tools.fixture_capture.deidentify import deidentify
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.registry import Registry

REPO = Path(__file__).resolve().parents[1]
AMENDMENT2 = "tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment2.txt"
REVIEWED = {"bsib:mnu": frozenset({"currentwidget", "menu", "targetwidget"})}
SECRET = "홍길동"
MNU = f'<bsib:mnu targetwidget="{SECRET}01" currentwidget="" menu="대표자 {SECRET}"></bsib:mnu>'


@pytest.fixture
def registry():
    return Registry()


def soup(html):
    return BeautifulSoup(html, "html.parser")


def left_unsupported(registry, html):
    return [tag.name for tag in deidentify(soup(html), registry).find_all(True) if classify(tag.name) == "U"]


# ── the list, and that it is the spec's list ────────────────────────────────

def test_the_reviewed_list_is_exactly_the_one_entry():
    assert dict(P.REVIEWED_EMPTY_UNSUPPORTED) == REVIEWED
    assert all(type(value) is frozenset for value in P.REVIEWED_EMPTY_UNSUPPORTED.values())


def test_the_reviewed_list_cannot_be_widened_at_run_time():
    with pytest.raises(TypeError):
        P.REVIEWED_EMPTY_UNSUPPORTED["x-part"] = frozenset()
    assert dict(P.REVIEWED_EMPTY_UNSUPPORTED) == REVIEWED


def test_the_code_list_is_the_amendment_table():
    text = (REPO / AMENDMENT2).read_text(encoding="utf-8")
    section = text.split("## A4.", 1)[1]
    rows = re.findall(r"^\| `([^`]+)` \| ((?:`[^`]+`(?:, )?)+) \|", section, re.M)
    assert {name: frozenset(re.findall(r"`([^`]+)`", names)) for name, names in rows} == REVIEWED


def test_the_digest_lists_amendment_two_after_amendment_one():
    assert tuple(G.AMENDMENT_PATHS) == (
        "tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment1.txt", AMENDMENT2)
    assert G.policy_descriptor()["policy_amendments"][1] == hashlib.sha256((REPO / AMENDMENT2).read_bytes()).hexdigest()


# ── what is_empty_unsupported accepts, judged on the element as given ───────

@pytest.mark.parametrize("html", [
    "<form><bsib:mnu></bsib:mnu></form>",                                       # the slice-4B rule is unchanged
    f"<form>{MNU}</form>",
    '<form><bsib:mnu menu="" currentwidget="" targetwidget=""></bsib:mnu></form>',  # attribute order is not part of it
    '<form><BSIB:MNU TargetWidget="" CurrentWidget="" Menu=""></BSIB:MNU></form>',  # names as the parser gives them
])
def test_accepted(html):
    assert is_empty_unsupported(soup(html).find("bsib:mnu"))


@pytest.mark.parametrize("name", ["x-part", "svg", "iframe", "foo:bar", "bsib:menu"])
def test_the_slice_4b_rule_still_takes_any_attribute_less_empty_unsupported_name(registry, name):
    # The amendment adds to the removal, it does not narrow the old one to the reviewed names (Codex, 5c-3a-B r1).
    element = soup(f"<form><{name}></{name}></form>").find(name)
    assert classify(element.name) == "U" and is_empty_unsupported(element)
    assert left_unsupported(registry, f"<div><form><{name}></{name}></form></div>") == []


@pytest.mark.parametrize("html", [
    '<form><bsib:mnu targetwidget="" currentwidget="" menu="" id="x"></bsib:mnu></form>',    # one more attribute
    '<form><bsib:mnu targetwidget="" menu=""></bsib:mnu></form>',                            # one missing
    '<form><bsib:mnu menu=""></bsib:mnu></form>',
    '<form><bsib:mnu data-menu="" targetwidget="" currentwidget=""></bsib:mnu></form>',     # a near name
    '<form><bsib:mnu targetwidget="" currentwidget="" menu=""> </bsib:mnu></form>',         # whitespace is a child
    '<form><bsib:mnu targetwidget="" currentwidget="" menu=""><!-- c --></bsib:mnu></form>',
    '<form><bsib:mnu targetwidget="" currentwidget="" menu=""><span></span></bsib:mnu></form>',
    '<form><bsib:mnu targetwidget="" currentwidget="" menu=""><input name="q"></bsib:mnu></form>',
])
def test_refused_for_the_reviewed_name(html):
    assert not is_empty_unsupported(soup(html).find("bsib:mnu"))


@pytest.mark.parametrize("name", ["bsib:menu", "bsib:mnu2", "x-mnu", "mnu", "x-part", "rt", "svg"])
def test_the_same_attributes_on_another_unsupported_name_are_refused(name):
    element = soup(f'<form><{name} targetwidget="" currentwidget="" menu=""></{name}></form>').find(name)
    assert classify(element.name) == "U"
    assert not is_empty_unsupported(element)


@pytest.mark.parametrize("html", ['<form><div targetwidget="" currentwidget="" menu=""></div></form>',
                                  "<form><div></div></form>", "<form><span></span></form>", "<form><p></p></form>"])
def test_a_supported_element_is_never_an_empty_unsupported_one(registry, html):
    tree = soup(html)
    assert not is_empty_unsupported(tree.form.contents[0])
    kept = deidentify(tree, registry).form.contents                        # and cleanup does not remove it
    assert len(kept) == 1 and kept[0].name == tree.form.contents[0].name


def test_the_judgement_reads_the_element_as_given():
    element = soup('<form><bsib:mnu targetwidget="" currentwidget="" menu=""><!-- c --></bsib:mnu></form>').find("bsib:mnu")
    assert not is_empty_unsupported(element)
    element.contents[0].extract()
    assert is_empty_unsupported(element)
    element["id"] = "x"
    assert not is_empty_unsupported(element)


# ── what cleanup does with it ────────────────────────────────────────────────

def test_cleanup_removes_the_reviewed_element_and_its_values(registry):
    fixture = deidentify(soup(f'<div><form>{MNU}<input type="hidden" name="a" value="b"></form>{MNU}</div>'), registry)
    assert [tag.name for tag in fixture.find_all(True) if classify(tag.name) == "U"] == []
    assert SECRET not in str(fixture) and "targetwidget" not in str(fixture)
    validate_structure(fixture)
    assert len(fixture.find_all("form")) == 1                                # the emptied form is not removed


@pytest.mark.parametrize("html", [
    '<div><form><bsib:mnu targetwidget="" currentwidget="" menu="" id="x"></bsib:mnu></form></div>',
    '<div><form><bsib:mnu targetwidget="" currentwidget="" menu=""><!-- c --></bsib:mnu></form></div>',
    '<div><form><bsib:mnu targetwidget="" currentwidget="" menu=""><input name="q"></bsib:mnu></form></div>',
    '<div><form><rt targetwidget="" currentwidget="" menu=""></rt></form></div>',
])
def test_anything_that_only_looks_reviewed_after_cleanup_stays_and_is_refused(registry, html):
    # Cleanup strips attributes and removes comments and inputs, so each of these ends up looking like a reviewed or an
    # attribute-less empty element. Judging after cleanup would remove all four.
    fixture = deidentify(soup(html), registry)
    assert len([tag for tag in fixture.find_all(True) if classify(tag.name) == "U"]) == 1
    with pytest.raises(CaptureError) as caught:
        validate_structure(fixture)
    assert caught.value.rule == "d1_unsupported_element"


@pytest.mark.parametrize("name", ["ruby", "rb", "rt", "rtc", "rp"])
def test_an_empty_ruby_element_is_never_removed(registry, name):
    # §2.3 refuses every ruby structure, empty ones too; removing one would turn that refusal into a pass.
    fixture = deidentify(soup(f"<div><{name}></{name}></div>"), registry)
    assert [tag.name for tag in fixture.find_all(True) if classify(tag.name) == "U"] == [name]
    with pytest.raises(CaptureError) as caught:
        validate_structure(fixture)
    assert caught.value.rule == "d1_unsupported_element"


def test_a_reviewed_element_inside_the_protected_boundary_is_kept(registry):
    # Inside a selected subtree removal would shift the positions the selectors depend on.
    tree = ('<div id="content"><ul><li><div><bsib:mnu targetwidget="" currentwidget="" menu=""></bsib:mnu>'
            '1,553.83</div></li></ul></div>')
    assert left_unsupported(registry, tree) == ["bsib:mnu"]


def test_a_reviewed_element_beside_the_selected_subtree_is_kept(registry):
    # The boundary also protects the element siblings along the selected subtree's ancestor chain: removing one before
    # the selection would shift its numeric path.
    tree = ('<div id="content"><bsib:mnu targetwidget="" currentwidget="" menu=""></bsib:mnu><ul><li><div>'
            '1,553.83</div></li></ul></div>')
    assert left_unsupported(registry, tree) == ["bsib:mnu"]


def test_removal_does_not_cascade_to_an_unreviewed_parent(registry):
    tree = '<div><bsib:outer><bsib:mnu targetwidget="" currentwidget="" menu=""></bsib:mnu></bsib:outer></div>'
    assert left_unsupported(registry, tree) == ["bsib:outer"]


# ── the whole capture of the page shape that was refused ─────────────────────

def bs_page(element):
    return official_html("bs_official", extra=f'<form>{element}<input type="hidden" name="a" value="b"></form>')


def test_the_refused_page_shape_now_captures(registry):
    get, _ = getter(bs_page(MNU))
    artifact = capture_route("bs_official", registry=registry, get=get)
    assert b"bsib:mnu" not in artifact.fixture and SECRET.encode() not in artifact.fixture + artifact.metadata


def test_one_more_attribute_still_refuses_the_capture(registry):
    get, _ = getter(bs_page(MNU.replace("<bsib:mnu ", '<bsib:mnu id="x" ')))
    with pytest.raises(CaptureError) as caught:
        capture_route("bs_official", registry=registry, get=get)
    assert caught.value.rule == "d1_unsupported_element"
    assert SECRET not in str(caught.value)
