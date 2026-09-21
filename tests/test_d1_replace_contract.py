"""D1 slice 4A contract — narrow name replacement, its evidence, and correspondence before and after storage.

Written before the implementation (Claude), from `d1_detection_policy_v1.md` §0 and §7.1–§7.2
(policy_spec_sha256 47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635) and the slice-4 design
agreement with Codex (2026-09-21). The implementer reads this file and does not edit it.

API (module `tools/fixture_capture/d1_replace.py`):
- `PLACEHOLDER == "[성명삭제]"`.
- `Entry(route, selector, label)` — one reviewed disclosure location. `REGISTRY` is a tuple of them, separate from
  the extraction selector registry. A route with no entry has no permission to replace anything.
- `replace_names(soup, route, recorded_extraction, *, entries=None) -> Replacements` — `entries=None` means
  `REGISTRY`. Replaces in place, then checks, on that soup, that every D1 finding is the preserved label of a node
  this call replaced, and that no removed value survives in any string value of `recorded_extraction`.
- `verify_stored(fixture_bytes, replacements, metadata=None) -> None` — reparses the bytes that will be stored,
  re-verifies every replacement at exactly its path, recomputes the whole D1 list (with `metadata` when given) and
  requires the same correspondence.
Every refusal is `CaptureError`; its text never carries a page value.

This slice is a library. Nothing here makes the capture enforce D1 — the wiring is slice 5.
"""

import copy
import re
import unicodedata

import pytest
from bs4 import BeautifulSoup, NavigableString

from tools.fixture_capture import d1_replace as R
from tools.fixture_capture.errors import CaptureError

NAME = "홍길동"
ROUTE = "citi_primary"


def page(li2="대표자 홍길동", *, before="", after="<p>환율</p>", li1="상호 한국씨티은행", li3="주소 서울"):
    return BeautifulSoup(
        f"<html><body>{before}<footer><div><ul><li>{li1}</li><li>{li2}</li><li>{li3}</li></ul></div></footer>"
        f"{after}</body></html>", "html.parser")


ENTRY = R.Entry(ROUTE, "footer > div > ul > li:nth-child(2)", "대표자")


def replace(soup, recorded=None, *, route=ROUTE, entries=(ENTRY,)):
    return R.replace_names(soup, route, {} if recorded is None else recorded, entries=list(entries))


# A diagnostic is a policy code and a structural location: ASCII identifiers, digits, brackets, dots, colons.
# No Hangul and no page word can appear in it — checking for one name at a time let others through.
_DIAGNOSTIC = re.compile(r"[A-Za-z0-9_.:\[\] -]*")


def refused(call):
    with pytest.raises(CaptureError) as caught:
        call()
    text = str(caught.value)
    assert _DIAGNOSTIC.fullmatch(text), text.encode("unicode_escape")
    for word in ("John", "Smith", "SECRET"):
        assert word not in text
    return caught.value


def li(soup, n):
    return soup.select(f"footer li:nth-child({n})")[0]


# ── the replacement itself ───────────────────────────────────────────────────

def test_the_registered_value_becomes_the_placeholder_and_nothing_else_changes():
    soup = page()
    replace(soup)
    assert R.PLACEHOLDER == "[성명삭제]"
    assert [child for child in li(soup, 2).contents] == ["대표자 [성명삭제]"]
    assert type(li(soup, 2).contents[0]) is NavigableString
    assert li(soup, 1).get_text() == "상호 한국씨티은행" and li(soup, 3).get_text() == "주소 서울"
    assert NAME not in str(soup)


def test_the_separator_is_kept_verbatim():
    soup = page("대표자　홍길동")
    replace(soup)
    assert li(soup, 2).get_text() == "대표자　[성명삭제]"


def test_an_unregistered_route_has_no_permission():
    soup = page()
    refused(lambda: replace(soup, route="bs_official"))


def test_an_unregistered_route_without_titles_is_fine():
    soup = page("대표번호 1588-7000")
    replacements = replace(soup, route="bs_official")
    R.verify_stored(str(soup).encode("utf-8"), replacements)


@pytest.mark.parametrize("selector", [
    "footer > div > ul > li:nth-child(9)",                    # matches nothing
    "footer li",                                               # matches three
])
def test_a_selector_must_match_exactly_one_element(selector):
    soup = page()
    refused(lambda: replace(soup, entries=[R.Entry(ROUTE, selector, "대표자")]))


def test_several_matches_refuse_even_when_the_first_is_a_valid_target():
    # Taking the first match would replace this name and leave nothing else to object to.
    soup = page(li1="대표자 홍길동", li2="대표번호 1588-7000")
    refused(lambda: replace(soup, entries=[R.Entry(ROUTE, "footer li", "대표자")]))


@pytest.mark.parametrize("text", [
    "대 표 자 홍길동",            # a detected spelling, not the registered label
    "대표이사 홍길동",            # another title
    "대표자 John Smith",          # whitespace inside the value
    "대표자 ",                    # empty value
    "대표자",                     # no separator, no value
    "대표자홍길동",                # no separator
    "대표자 홍길동\n",             # `$` would match before this LF; fullmatch must not
    "대표자 홍¨동",           # U+00A8 has no whitespace, but NFKC turns it into " ̈"
    "대표자 [성명삭제]",           # an existing placeholder is not a replacement
    "대표자 ［성명삭제］",          # nor one that normalizes to it
    "대표자\u001c홍길동",          # U+001C is Python whitespace, but not the policy's W
    " 대표자 홍길동",              # nothing is stripped: a leading space is outside the exact label
])
def test_the_value_boundary_is_exact(text):
    if text == "대표자 홍¨동":
        assert " " in unicodedata.normalize("NFKC", "홍¨동")       # the input exercises the rule
    soup = page(text)
    refused(lambda: replace(soup))


@pytest.mark.parametrize("li2", [
    "대표자 <b>홍길동</b>",       # a child tag
    "대표자 홍길동<!-- x -->",     # a second node
    "대표자 홍길동<span>x</span>", # a trailing element: the first node alone would match and correspond
    "대표자 홍길동<br>",           # a trailing void element
])
def test_only_a_single_plain_text_node_is_supported(li2):
    soup = page(li2)
    refused(lambda: replace(soup))


def test_the_selected_element_must_be_a_list_item():
    soup = BeautifulSoup("<footer><div><p>대표자 홍길동</p></div></footer>", "html.parser")
    refused(lambda: replace(soup, entries=[R.Entry(ROUTE, "footer > div > p", "대표자")]))


# ── what the removed value must not survive in ───────────────────────────────

@pytest.mark.parametrize("recorded", [
    {"events": [{"labels": {"item_text": "대표자 홍길동"}}]},
    {"events": [{"labels": {"row_text": "상호 한국씨티은행 | 대표자 홍길동 | 주소"}}]},      # inside a longer value
    {"events": [{"labels": {"item_text": unicodedata.normalize("NFD", NAME)}}]},         # same after NFKC
    {"exception": {"args": ["x", [NAME]]}},                                               # nested list leaf
])
def test_the_removed_value_must_not_survive_in_the_recorded_extraction(recorded):
    soup = page()
    before = copy.deepcopy(recorded)
    refused(lambda: replace(soup, recorded))
    assert recorded == before                                   # the record is refused, never repaired


@pytest.mark.parametrize("value, recorded_text", [
    ("John", "JOHN"),              # equal only after N (NFKC + ASCII case): NFKC alone would miss it
    ("A", "A\u030a"),              # a raw substring whose N differs (a vs å): N alone would miss it
])
def test_the_residual_check_compares_raw_and_normalized_separately(value, recorded_text):
    soup = page(f"대표자 {value}")
    refused(lambda: replace(soup, {"events": [{"t": recorded_text}]}))


def test_a_recorded_extraction_without_the_value_is_fine():
    soup = page()
    replace(soup, {"events": [{"labels": {"item_text": "USD 1,390.0"}}], "queries": ["li"]})


def test_dictionary_keys_are_not_value_leaves():
    soup = page()
    replace(soup, {"events": [{NAME: "x"}]})


# ── correspondence on the replaced soup ──────────────────────────────────────

@pytest.mark.parametrize("extra", [
    "<p>은행장 김철수</p>",                                      # another title somewhere else
    '<p title="대표자">x</p>',                                    # an attribute finding
    "<p>대<span>표</span>자 김철수</p>",                         # a finding only the run sees
])
def test_any_finding_not_owned_by_a_replacement_refuses(extra):
    soup = page(after=extra)
    refused(lambda: replace(soup))


def test_a_pre_existing_placeholder_is_not_evidence_even_when_its_text_is_identical():
    # After replacement both items read "대표자 [성명삭제]". Only the one this call replaced is evidence;
    # equal strings are not the same node.
    soup = BeautifulSoup(
        "<footer><div><ul><li>대표자 [성명삭제]</li><li>대표자 홍길동</li></ul></div></footer>", "html.parser")
    refused(lambda: replace(soup))


# ── after storage: reparse and exact paths ───────────────────────────────────

def _stored(soup):
    return str(soup).encode("utf-8")


def test_the_stored_bytes_verify():
    soup = page()
    replacements = replace(soup)
    R.verify_stored(_stored(soup), replacements)
    R.verify_stored(_stored(soup), replacements, metadata={"recorded_extraction": {"events": []}})


def test_an_empty_replacement_list_still_checks_everything():
    soup = page("대표번호 1588-7000")
    replacements = replace(soup, route="bs_official")          # nothing to replace, nothing found
    stored = _stored(soup).replace(b"<p>", "<p>대표이사 김철수</p><p>".encode(), 1)
    refused(lambda: R.verify_stored(stored, replacements))
    refused(lambda: R.verify_stored(_stored(soup), replacements,
                                    metadata={"recorded_extraction": {"events": [{"t": "은행장 확인"}]}}))


def test_metadata_with_a_title_refuses():
    soup = page()
    replacements = replace(soup)
    refused(lambda: R.verify_stored(_stored(soup), replacements,
                                    metadata={"recorded_extraction": {"events": [{"t": "대표자 확인"}]}}))


@pytest.mark.parametrize("mutate", [
    lambda b: b.replace(b"<li>", b"<li>x</li><li>", 1),                        # the item moved one position
    lambda b: b.replace("[성명삭제]".encode(), "[삭제]".encode()),               # its text changed
    lambda b: b.replace("대표자 [성명삭제]".encode(), "대표자 <span>[성명삭제]</span>".encode()),   # split
    lambda b: b.replace("대표자 [성명삭제]".encode(), "대표자 [성명삭제]<span>x</span>".encode()),  # a sibling added
    lambda b: b.replace(b"<ul>", b"<ol>").replace(b"</ul>", b"</ol>"),         # same indices and text, other ancestors
    lambda b: b.replace(b"<p>", "<p>대표이사 김철수</p><p>".encode(), 1),          # a new finding
    lambda b: b + b"\xff",                                                      # not UTF-8
])
def test_the_stored_bytes_must_match_the_replacement_exactly(mutate):
    soup = page()
    replacements = replace(soup)
    stored = mutate(_stored(soup))
    assert stored != _stored(soup)
    refused(lambda: R.verify_stored(stored, replacements))


# ── the registry itself ──────────────────────────────────────────────────────

def test_the_registry_is_narrow():
    assert R.REGISTRY, "the reviewed citi locations are registered"
    for entry in R.REGISTRY:
        assert entry.route in {"citi_primary", "citi_secondary"}
        assert entry.label == "대표자"
        assert "#" not in entry.selector and "." not in entry.selector       # structure only: ids/classes are stripped
