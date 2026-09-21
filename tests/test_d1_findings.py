"""Implementation-side checks supplementing the read-only slice 3 contract.

These pin memory evidence for the next slice and refusal boundaries not covered
by the independent approval-identifier contract.
"""

from itertools import permutations

import pytest
from bs4 import BeautifulSoup

from tools.fixture_capture.d1_findings import (NodeEvidence, TextEvidence, findings,
                                              findings_with_evidence, select_longest)
from tools.fixture_capture.errors import CaptureError


def soup(html=""):
    return BeautifulSoup(html, "html.parser")


def test_exact_node_run_merge_retains_both_sources_and_node_coordinates():
    page = soup('<p title="행장">㈜대표자 홍길동</p>')
    result, evidence = findings_with_evidence(page, {"a": "ceo"})
    assert result == findings(page, {"a": "ceo"})
    assert evidence == {
        1: TextEvidence(frozenset({"node", "run"}), (NodeEvidence((0, 0), 3, 6),)),
    }
    assert len(result) == 3
    assert "홍길동" not in repr((result, evidence))
    assert all("sources" not in item and "nodes" not in item for item in result)


def test_node_only_evidence_uses_node_local_coordinates_after_an_expansion():
    result, evidence = findings_with_evidence(soup('<p>ﬁ<b>ＣＥＯ홍길동</b></p>'), {})
    assert (result[0]["start"], result[0]["end"]) == (2, 5)
    assert evidence == {
        1: TextEvidence(frozenset({"node"}), (NodeEvidence((0, 1, 0), 0, 3),)),
    }


@pytest.mark.parametrize("html,token,end", [
    ("<p>대<b>표자</b></p>", "대표자", 3),
    ("<p><span>공동</span><b>대표이사</b></p>", "공동대표이사", 6),
    ("<p>대표<br>자</p>", "대표자", 4),
])
def test_run_evidence_never_borrows_nodes_from_an_overlapping_shorter_candidate(html, token, end):
    result, evidence = findings_with_evidence(soup(html), {})
    assert [(item["token"], item["start"], item["end"]) for item in result] == [(token, 0, end)]
    assert evidence == {1: TextEvidence(frozenset({"run"}), ())}


def test_evidence_stays_with_its_run_and_final_occurrence():
    result, evidence = findings_with_evidence(soup('<div>대표자<p>대표자</p>대표자</div>'), {})
    assert [item["occurrence_index"] for item in result] == [1, 2, 3]
    assert evidence == {
        1: TextEvidence(frozenset({"node", "run"}), (NodeEvidence((0, 0), 0, 3),)),
        2: TextEvidence(frozenset({"node", "run"}), (NodeEvidence((0, 1, 0), 0, 3),)),
        3: TextEvidence(frozenset({"node", "run"}), (NodeEvidence((0, 2), 0, 3),)),
    }


def test_duplicates_ties_and_overlap_chains_are_invariant_under_permutation():
    candidates = [(0, 4, "ceo"), (0, 4, "대표자"), (0, 4, "대표자"),
                  (3, 7, "대표이사"), (6, 9, "행장")]
    for order in permutations(candidates):
        values = list(order)
        before = values.copy()
        assert select_longest(values) == [(0, 4, "대표자"), (6, 9, "행장")]
        assert values == before


def test_attribute_index_counts_names_without_findings_and_lists_use_numeric_order():
    page = soup('<div title="x" alt="x" class="x"></div>')
    page.div["class"] = ["x"] * 11
    page.div["class"][2] = "행장"
    page.div["class"][10] = "은행장"
    result = findings(page, {})
    assert [(item["attribute_index"], item["list_index"], item["token"])
            for item in result] == [(1, 2, "행장"), (1, 10, "은행장")]


def test_parsed_bs4_string_subclasses_are_attribute_strings():
    page = soup('<meta charset="ＣＥＯ">')
    assert isinstance(page.meta["charset"], str)
    assert type(page.meta["charset"]) is not str
    result = findings(page, {})
    assert [(item["source"], item["part"], item["list_index"], item["token"])
            for item in result] == [("html_attribute", "value", None, "ceo")]


@pytest.mark.parametrize("invalid", [None, True, b"secret", ("secret",), ["대표자", None]])
def test_a_late_invalid_attribute_value_refuses_all_prior_findings(invalid):
    page = soup('<p title="대표자">대표자</p><div></div>')
    page.div["data-홍길동"] = invalid
    with pytest.raises(CaptureError) as caught:
        findings_with_evidence(page, {"a": "대표자"})
    assert caught.value.rule == "d1_invalid_attribute"
    assert "홍길동" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_an_invalid_attribute_name_does_not_leak_through_sorting():
    page = soup('<div title="대표자"></div>')
    page.div.attrs[("홍길동",)] = "대표자"
    with pytest.raises(CaptureError) as caught:
        findings(page, {})
    assert caught.value.rule == "d1_invalid_attribute"
    assert "홍길동" not in str(caught.value)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), {1: "홍길동"}])
def test_late_non_json_metadata_refuses_without_echoing_even_a_string_key(invalid):
    with pytest.raises(CaptureError) as caught:
        findings(soup('<p>대표자</p>'), {"a": "대표자", "홍길동": invalid})
    assert caught.value.rule == "d1_invalid_metadata"
    assert "홍길동" not in str(caught.value)


@pytest.mark.parametrize("container", [dict, list])
def test_metadata_cycles_are_unreadable_even_without_a_candidate(container):
    child = container()
    if isinstance(child, dict):
        child["홍길동"] = child
    else:
        child.append(child)
    with pytest.raises(CaptureError) as caught:
        findings(soup(), {"a": child})
    assert caught.value.rule == "d1_invalid_metadata"
    assert "홍길동" not in str(caught.value)


def test_shared_containers_at_different_paths_remain_separate_fields():
    child = ["대표자"]
    result = findings(soup(), {"b": child, "a": child})
    assert [item["path"] for item in result] == [["a", 0], ["b", 0]]
    assert [item["total_count"] for item in result] == [2, 2]


def test_metadata_paths_preserve_punctuation_and_complete_container_nesting():
    metadata = {"a": {"0": ["대표자"]}, "a.[0]/": ["대표자"]}
    result = findings(soup(), metadata)
    assert [item["path"] for item in result] == [["a", "0", 0], ["a.[0]/", 0]]


def test_metadata_strings_have_no_hash_parser_or_enum_exemptions():
    # Synthetic values exercise the D1 layer, not the caller's schema validator.
    result = findings(soup(), {"origin": "대표자", "parser": {"name": "CEO"},
                               "fixture_sha256": "은행장"})
    assert [(item["path"], item["token"]) for item in result] == [
        (["fixture_sha256"], "은행장"), (["origin"], "대표자"), (["parser", "name"], "ceo"),
    ]


def test_encoded_metadata_values_are_not_recursively_interpreted():
    assert findings(soup(), {"a": "대<b>표</b>자", "b": r"\uB300\uD45C\uC790",
                             "c": "%EB%8C%80%ED%91%9C%EC%9E%90"}) == []


@pytest.mark.parametrize("html,rule", [
    ("<p>대표자</p><x-person-홍길동></x-person-홍길동>", "d1_unsupported_element"),
    ("<p>대표자</p><p><b>ᄃ</b><i>ᅢ</i>x</p>", "d1_normalization_boundary_unsupported"),
    ("<p><b>ᄃ</b><i>ᅢ</i>x</p>", "d1_normalization_boundary_unsupported"),
])
def test_findings_never_skip_structure_or_no_match_normalization_failures(html, rule):
    with pytest.raises(CaptureError) as caught:
        findings_with_evidence(soup(html), {})
    assert caught.value.rule == rule
    assert "홍길동" not in str(caught.value)


def test_a_deep_metadata_tree_is_refused_as_unreadable():
    metadata = {}
    cursor = metadata
    for _ in range(1500):
        cursor["a"] = {}
        cursor = cursor["a"]
    with pytest.raises(CaptureError) as caught:
        findings(soup(), metadata)
    assert caught.value.rule == "d1_structure_depth_unsupported"
