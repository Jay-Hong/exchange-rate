"""Implementation-side checks beyond the independent, read-only 4A contract.

All HTML is synthetic. Real capture selectors are checked locally, not by
introducing captures or their contents into repository tests.
"""

import copy
import re

import pytest
from bs4 import BeautifulSoup, NavigableString

from tools.fixture_capture import d1_replace as R
from tools.fixture_capture.errors import CaptureError

ROUTE = "citi_primary"
ENTRY = R.Entry(ROUTE, "footer > ul > li:nth-child(2)", "대표자")


def page(value="홍길동", separator=" "):
    return BeautifulSoup(
        f"<footer><ul><li>상호 은행</li><li>대표자{separator}{value}</li>"
        "<li>주소 서울</li></ul></footer>", "html.parser")


def default_page():
    # Explicit synthetic topology with element siblings and whitespace siblings.
    # It intentionally does not derive its shape from REGISTRY's selectors.
    return BeautifulSoup(
        "<html>\n<head></head>\n<body><main></main>\n<div>"
        "<p></p><p></p><p></p><p></p>\n<footer>"
        "<p></p><p></p><p></p><p></p>\n<div><div>\n<p></p>\n"
        "<ul>\n<li>대표자 홍길동</li>\n<li>주소 서울</li>\n</ul>\n"
        "</div></div></footer></div></body></html>", "html.parser")


def replace(soup, recorded=None, entries=(ENTRY,)):
    return R.replace_names(soup, ROUTE, {} if recorded is None else recorded,
                           entries=entries)


def refused(call, rule):
    with pytest.raises(CaptureError) as caught:
        call()
    assert caught.value.rule == rule
    assert re.fullmatch(r"[A-Za-z0-9_.:\[\] -]*", str(caught.value))
    assert not any(word in str(caught.value) for word in ("John", "Smith", "SECRET"))


@pytest.mark.parametrize("route", ["citi_primary", "citi_secondary"])
def test_default_registry_replaces_only_the_reviewed_disclosure(route):
    soup = default_page()
    assert isinstance(R.REGISTRY, tuple)
    assert {entry.route for entry in R.REGISTRY} == {"citi_primary", "citi_secondary"}
    entry, = (entry for entry in R.REGISTRY if entry.route == route)
    matches = soup.select(entry.selector)
    assert len(matches) == 1 and matches[0] is soup.li
    original = soup.li.string
    result = R.replace_names(soup, route, {}, entries=None)
    item, = result
    assert item.node is soup.li.contents[0]
    assert item.node is not original and original.parent is None
    assert str(item.node) == "대표자 [성명삭제]"
    assert soup.select("li")[1].get_text() == "주소 서울"
    assert item.ancestors == ("html", "body", "div", "footer", "div", "div", "ul", "li")
    R.verify_stored(str(soup).encode("utf-8"), result)


def test_omitting_entries_uses_registry_and_empty_entries_does_not():
    soup = default_page()
    assert len(R.replace_names(soup, ROUTE, {})) == 1
    untouched = default_page()
    before = str(untouched)
    refused(lambda: R.replace_names(untouched, ROUTE, {}, entries=[]),
            "d1_replacement_correspondence")
    assert str(untouched) == before


def test_evidence_retains_identity_and_exact_contents_path_without_removed_value():
    soup = page()
    original = soup.select("li")[1].string
    result = replace(soup)
    item, = result
    assert item.node is soup.select("li")[1].contents[0]
    assert item.node is not original
    assert item.path == (0, 0, 1, 0)
    assert item.ancestors == ("footer", "ul", "li")
    assert (item.label, item.label_start, item.label_end) == ("대표자", 0, 3)
    assert item.text == "대표자 [성명삭제]"
    assert "홍길동" not in repr(result)
    assert all("홍길동" not in str(value) for value in vars(item).values())


def test_equal_new_node_at_the_same_path_is_not_live_replacement_evidence():
    soup = page()
    result = replace(soup)
    item, = result
    equal_node = NavigableString(str(item.node))
    assert equal_node == item.node and equal_node is not item.node
    item.node.replace_with(equal_node)
    refused(lambda: R._check_correspondence(soup, result, {}),
            "d1_replacement_correspondence")
    # Reparse necessarily loses identity; all structural and textual bindings
    # must still pass there. Execution-local evidence is not an integrity token.
    R.verify_stored(str(soup).encode("utf-8"), result)


def test_equal_sibling_elements_keep_distinct_paths_and_node_identities():
    soup = BeautifulSoup("<footer><ul><li>대표자 홍길동</li><li>대표자 홍길동</li>"
                         "</ul></footer>", "html.parser")
    entries = [R.Entry(ROUTE, f"footer > ul > li:nth-child({n})", "대표자")
               for n in (1, 2)]
    result = replace(soup, entries=entries)
    assert [item.path for item in result] == [(0, 0, 0, 0), (0, 0, 1, 0)]
    assert result[0].node == result[1].node
    assert result[0].node is not result[1].node
    assert all(item.node is li.contents[0] for item, li in zip(result, soup.select("li")))
    R.verify_stored(str(soup).encode("utf-8"), result)


# Independent enumeration of §4.1 W; Python's extra U+001C..U+001F are absent.
WHITESPACE = tuple(map(chr, (*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680,
                             *range(0x2000, 0x200B), 0x2028, 0x2029,
                             0x202F, 0x205F, 0x3000)))


@pytest.mark.parametrize("separator", WHITESPACE)
def test_every_policy_separator_is_preserved_through_storage(separator):
    soup = page(separator=separator)
    result = replace(soup)
    assert soup.select("li")[1].string == f"대표자{separator}[성명삭제]"
    R.verify_stored(str(soup).encode("utf-8"), result)


@pytest.mark.parametrize("value", ["홍\u001c동", "ß"])
def test_value_whitespace_and_case_use_policy_rules_not_isspace_or_casefold(value):
    soup = page(value)
    result = replace(soup, {"t": "ss"})
    R.verify_stored(str(soup).encode("utf-8"), result)


@pytest.mark.parametrize("value, recorded", [
    ("John", {"t": ["JOHN"]}),
    ("A", {"t": ["A\u030a"]}),
    ("ﬁ", {"t": ["prefix-fi-suffix"]}),
])
def test_both_residual_comparisons_leave_the_record_unchanged(value, recorded):
    before = copy.deepcopy(recorded)
    refused(lambda: replace(page(value), recorded), "d1_removed_value_in_extraction")
    assert recorded == before


@pytest.mark.parametrize("whole", ["대표자 홍길동", "대표자 [성명삭제]"])
def test_normalization_boundary_guard_on_input_and_output_by_fault_injection(monkeypatch, whole):
    # No reachable counterexample exists for today's exact-label/W grammar.
    # Inject a non-distributive N to exercise the required defensive branch,
    # without claiming this is an accepted real-world spelling.
    original_normalize = R.normalize

    def non_distributive(text):
        return original_normalize(text) + ("x" if text == whole else "")

    monkeypatch.setattr(R, "normalize", non_distributive)
    refused(lambda: replace(page()), "d1_normalization_boundary_unsupported")


def test_an_extra_finding_inside_the_placeholder_is_not_in_the_preserved_label(monkeypatch):
    # The real placeholder has no title. This fault injection isolates the
    # label-span guard from identity and path: both remain valid for the hit.
    monkeypatch.setattr(R, "PLACEHOLDER", "[은행장]")
    refused(lambda: replace(page()), "d1_replacement_correspondence")


@pytest.mark.parametrize("html", [
    "<p title='대표자'>x</p>",
    "<p>대<span>표</span>자</p>",
    "<p>대표자 [성명삭제]</p>",
])
def test_empty_replacements_still_recompute_all_stored_findings(html):
    soup = BeautifulSoup("<p>환율</p>", "html.parser")
    result = R.replace_names(soup, "bs_official", {})
    assert result == ()
    refused(lambda: R.verify_stored(html.encode("utf-8"), result),
            "d1_replacement_correspondence")


def test_stored_binding_is_checked_even_when_all_titles_disappear():
    soup = page()
    result = replace(soup)
    data = str(soup).replace("대표자 [성명삭제]", "공시 정보").encode("utf-8")
    refused(lambda: R.verify_stored(data, result), "d1_replacement_correspondence")


def test_stored_path_counts_text_siblings_and_never_searches_elsewhere():
    soup = page()
    result = replace(soup)
    data = str(soup).replace("<ul>", "<ul>\n", 1).encode("utf-8")
    refused(lambda: R.verify_stored(data, result), "d1_replacement_correspondence")


def test_selector_failure_cannot_echo_untrusted_selector_text():
    entry = R.Entry(ROUTE, "li:SECRET(", "대표자")
    refused(lambda: replace(page(), entries=[entry]), "d1_replacement_selector")


def test_cyclic_extraction_and_invalid_metadata_are_not_silent_successes():
    record = {"SECRET": []}
    record["SECRET"].append(record)
    refused(lambda: replace(page(), record), "d1_invalid_metadata")
    assert record["SECRET"][0] is record
    soup = page()
    result = replace(soup)
    refused(lambda: R.verify_stored(str(soup).encode("utf-8"), result, metadata=[]),
            "d1_invalid_metadata")


def test_library_rejects_wrong_roots_and_non_byte_storage_inputs():
    refused(lambda: R.replace_names(page().footer, ROUTE, {}, entries=[ENTRY]),
            "d1_invalid_root")
    refused(lambda: R.verify_stored("<p>환율</p>", ()), "d1_invalid_fixture_bytes")


@pytest.mark.parametrize("li", ["<li></li>", "<li><b>대표자 홍길동</b></li>"])
def test_an_empty_or_text_free_list_item_refuses_with_a_capture_error(li):
    # Codex review 2026-09-21: without the length check an empty <li> reached `contents[0]` and raised
    # IndexError — a refusal, but not the contract's CaptureError. Pin the error type, not just "raised".
    soup = BeautifulSoup(f"<footer><ul><li>상호 은행</li>{li}<li>주소 서울</li></ul></footer>", "html.parser")
    with pytest.raises(CaptureError) as caught:
        replace(soup)
    assert caught.value.rule == "d1_replacement_structure"
