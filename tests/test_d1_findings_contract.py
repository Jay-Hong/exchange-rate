"""D1 §6 contract — merge, longest match, final order and approval identifiers.

Written before the implementation, from `d1_detection_policy_v1.md` §1.2, §4.2 and §6
(policy_spec_sha256 47d090a63997116d8df9af80b3a04a3e99a5a62c487aaba34236d1897e273635).
The implementer reads this file and does not edit it.

What this pins is the list an approval record is compared against. Every field of a
finding is part of that comparison, so every field is asserted in full — a test that
only counts findings lets a wrong position or a wrong number through.
"""

import pytest
from bs4 import BeautifulSoup

from tools.fixture_capture.d1_findings import findings, select_longest
from tools.fixture_capture.errors import CaptureError

RULE = 'd1_role_context'


def soup(html):
    return BeautifulSoup(html, 'html.parser')


def text_finding(owner_path, segment_index, token, start, end, index, total):
    return {'rule_id': RULE, 'source': 'html_text',
            'owner_path': owner_path, 'segment_index': segment_index,
            'token': token, 'start': start, 'end': end,
            'occurrence_index': index, 'total_count': total}


def attr_finding(element_path, attribute_index, part, list_index, token, start, end, index, total):
    # The attribute is named by its position among the element's attribute names in
    # code point order, never by the name: a name is page text (`data-대표자홍길동`
    # parses as it stands), and D1 slice 1 already leaked a person's name through a
    # tag name used as a location.
    return {'rule_id': RULE, 'source': 'html_attribute',
            'element_path': element_path, 'attribute_index': attribute_index, 'part': part,
            'list_index': list_index,
            'token': token, 'start': start, 'end': end,
            'occurrence_index': index, 'total_count': total}


def meta_finding(path, token, start, end, field_occurrence, field_count, index, total):
    return {'rule_id': RULE, 'source': 'metadata', 'path': path,
            'token': token, 'start': start, 'end': end,
            'field_occurrence': field_occurrence, 'field_count': field_count,
            'occurrence_index': index, 'total_count': total}


def strict_equal(actual, expected):
    """`==` treats 0 and False, 1 and True, 0 and 0.0 as equal. A path is a typed
    array (§6.3: `"0"` and `0` differ), so types are compared too."""
    assert actual == expected
    for got, want in zip(actual, expected):
        assert set(got) == set(want)
        for key in want:
            assert type(got[key]) is type(want[key]), key
            if isinstance(want[key], list):
                assert [type(x) for x in got[key]] == [type(x) for x in want[key]], key


# ── §6.2 — leftmost-longest, as a pure function ──────────────────────────────
# The tie and the transitive chain cannot be produced by the current title list
# from real text, so they are pinned here where they can be.

def test_the_leftmost_start_wins_even_over_a_longer_later_candidate():
    assert select_longest([(1, 6, '대표이사'), (0, 2, '행장')]) == [(0, 2, '행장')]


def test_at_the_same_start_the_longest_wins():
    assert select_longest([(0, 4, '공동대표'), (0, 6, '공동대표이사'), (2, 6, '대표이사')]) \
        == [(0, 6, '공동대표이사')]


def test_overlap_is_not_transitive():
    # A overlaps B, B overlaps C, A does not overlap C. Merging the whole connected
    # component would report one finding where §6.2 step 4-5 reports two.
    assert select_longest([(0, 4, '공동대표'), (3, 7, '대표자'), (6, 9, '행장')]) \
        == [(0, 4, '공동대표'), (6, 9, '행장')]


def test_touching_spans_do_not_overlap():
    assert select_longest([(0, 3, '대표자'), (3, 6, '대표자')]) \
        == [(0, 3, '대표자'), (3, 6, '대표자')]


@pytest.mark.parametrize('tied, winner', [
    ([(0, 3, '행장'), (0, 3, '은행장')], '은행장'),       # same row, left before right
    ([(0, 3, 'ceo'), (0, 3, '대표자')], '대표자'),        # Korean row before English row
    ([(0, 3, 'chief executive'), (0, 3, 'ceo')], 'ceo'),
])
def test_an_equal_span_tie_goes_to_the_earlier_title_in_the_table(tied, winner):
    assert select_longest(tied) == [(0, 3, winner)]
    assert select_longest(list(reversed(tied))) == [(0, 3, winner)]


def test_the_result_is_in_start_order_whatever_the_input_order():
    assert select_longest([(8, 11, '대표자'), (0, 3, '대표자')]) \
        == [(0, 3, '대표자'), (8, 11, '대표자')]


def test_nothing_in_nothing_out():
    assert select_longest([]) == []


# ── §6.2 table — HTML text ───────────────────────────────────────────────────

def test_a_title_split_across_inline_tags_is_one_finding():
    # Node `대표이사` and the run's `공동대표`·`대표이사`·`공동대표이사` are one occurrence.
    strict_equal(findings(soup('<p><span>공동</span><b>대표이사</b> 홍길동</p>'), {}),
                 [text_finding([0], 0, '공동대표이사', 0, 6, 1, 1)])


def test_a_title_inside_a_longer_title_is_not_a_second_finding():
    strict_equal(findings(soup('<p>은행장 김철수</p>'), {}),
                 [text_finding([0], 0, '은행장', 0, 3, 1, 1)])


def test_two_occurrences_in_one_node_are_two_findings():
    strict_equal(findings(soup('<p>대표자 홍길동 대표자 김철수</p>'), {}),
                 [text_finding([0], 0, '대표자', 0, 3, 1, 2),
                  text_finding([0], 0, '대표자', 8, 11, 2, 2)])


def test_the_same_title_in_different_list_items_is_one_finding_each():
    strict_equal(findings(soup('<ul><li>대표자 A</li><li>대표자 B</li></ul>'), {}),
                 [text_finding([0, 0], 0, '대표자', 0, 3, 1, 2),
                  text_finding([0, 1], 0, '대표자', 0, 3, 2, 2)])


def test_a_node_only_candidate_survives_the_merge_in_run_coordinates():
    # The run `ABOUTCEO홍길동` has no candidate; the node `CEO홍길동` does (§5).
    strict_equal(findings(soup('<p><span>ABOUT</span><b>CEO홍길동</b></p>'), {}),
                 [text_finding([0], 0, 'ceo', 5, 8, 1, 1)])


def test_a_run_only_candidate_survives_the_merge():
    strict_equal(findings(soup('<p>대<b>표자</b> 홍길동</p>'), {}),
                 [text_finding([0], 0, '대표자', 0, 3, 1, 1)])


def test_a_later_title_touching_a_longer_one_is_kept():
    # `은행장` removes the `행장` inside it, not the `행장` that starts where it ends.
    strict_equal(findings(soup('<p>은행장행장</p>'), {}),
                 [text_finding([0], 0, '은행장', 0, 3, 1, 2),
                  text_finding([0], 0, '행장', 3, 5, 2, 2)])


def test_positions_are_normalized_code_points_not_raw_ones():
    # NFKC turns `㈜` into three characters, so the title starts at 3, not 1.
    strict_equal(findings(soup('<p>㈜대표자</p>'), {}),
                 [text_finding([0], 0, '대표자', 3, 6, 1, 1)])


def test_text_findings_follow_run_emission_order_not_path_order():
    # Emission: parent segment 0, the child block, parent segment 1. Sorting by
    # (owner_path, segment_index) would put both parent segments first.
    strict_equal(findings(soup('<div>대표자<p>대표자</p>대표자</div>'), {}),
                 [text_finding([0], 0, '대표자', 0, 3, 1, 3),
                  text_finding([0, 1], 0, '대표자', 0, 3, 2, 3),
                  text_finding([0], 1, '대표자', 0, 3, 3, 3)])


def test_longest_selection_does_not_reach_across_runs():
    # Equal coordinates in two runs are two occurrences, not an overlap.
    strict_equal(findings(soup('<p>은행장</p><p>행장</p>'), {}),
                 [text_finding([0], 0, '은행장', 0, 3, 1, 2),
                  text_finding([1], 0, '행장', 0, 2, 2, 2)])


# ── §1.2 — attributes ────────────────────────────────────────────────────────

def test_an_attribute_value_is_its_own_scalar():
    strict_equal(findings(soup('<div title="대표자 홍길동"></div>'), {}),
                 [attr_finding([0], 0, 'value', None, '대표자', 0, 3, 1, 1)])


def test_an_attribute_name_is_scanned_too():
    # html.parser lowercases names; the name `data-대표자` is a scalar of its own.
    strict_equal(findings(soup('<div data-대표자="1"></div>'), {}),
                 [attr_finding([0], 0, 'name', None, '대표자', 5, 8, 1, 1)])


def test_each_element_of_a_list_valued_attribute_is_its_own_scalar():
    strict_equal(findings(soup('<div class="x 대표이사"></div>'), {}),
                 [attr_finding([0], 0, 'value', 1, '대표이사', 0, 4, 1, 1)])


def test_list_elements_are_not_joined():
    # `class="대표 자"` parses to ['대표', '자']; joining would invent `대표 자`.
    assert findings(soup('<div class="대표 자"></div>'), {}) == []


def test_an_element_path_counts_text_nodes():
    # §3.1 coordinates: `contents` indices, text included. The span is child 1 of the
    # p, not child 0 — recorder.element_path() counts tags only and would say [0, 0].
    strict_equal(findings(soup('<p>x<span title="대표자"></span></p>'), {}),
                 [attr_finding([0, 1], 0, 'value', None, '대표자', 0, 3, 1, 1)])


def test_attribute_order_uses_parsed_names_not_normalized_ones():
    # Fullwidth `ｂ` (U+FF42) normalizes to `b`, which would sort before `c`; the
    # parsed name sorts after it, and the parsed name is what the page contains.
    strict_equal(findings(soup('<div ｂ="대표자" c="은행장"></div>'), {}),
                 [attr_finding([0], 0, 'value', None, '은행장', 0, 3, 1, 2),   # c
                  attr_finding([0], 1, 'value', None, '대표자', 0, 3, 2, 2)])  # ｂ


def test_attribute_order_is_name_order_then_name_before_value():
    # Each attribute holds a different title, so the order is visible in the output:
    # with identical findings an index is indistinguishable from any other index.
    # Written in reverse name order, so document order and name order disagree.
    strict_equal(findings(soup('<div 대표자="대표이사" title="행장" alt="은행장"></div>'), {}),
                 [attr_finding([0], 0, 'value', None, '은행장', 0, 3, 1, 4),    # alt
                  attr_finding([0], 1, 'value', None, '행장', 0, 2, 2, 4),      # title
                  attr_finding([0], 2, 'name', None, '대표자', 0, 3, 3, 4),     # 대표자 (name)
                  attr_finding([0], 2, 'value', None, '대표이사', 0, 4, 4, 4)])  # 대표자 (value)


def test_attribute_elements_follow_document_order():
    html = '<div title="대표자"><span title="대표자"></span></div><p title="대표자"></p>'
    strict_equal(findings(soup(html), {}),
                 [attr_finding([0], 0, 'value', None, '대표자', 0, 3, 1, 3),
                  attr_finding([0, 0], 0, 'value', None, '대표자', 0, 3, 2, 3),
                  attr_finding([1], 0, 'value', None, '대표자', 0, 3, 3, 3)])


def test_longest_selection_applies_inside_one_attribute_scalar():
    strict_equal(findings(soup('<div title="공동대표이사"></div>'), {}),
                 [attr_finding([0], 0, 'value', None, '공동대표이사', 0, 6, 1, 1)])


def test_an_attribute_is_never_joined_with_body_text():
    assert findings(soup('<p title="대">표자 홍길동</p>'), {}) == []


def test_two_attributes_with_equal_coordinates_are_two_findings():
    strict_equal(findings(soup('<div alt="은행장" title="행장"></div>'), {}),
                 [attr_finding([0], 0, 'value', None, '은행장', 0, 3, 1, 2),
                  attr_finding([0], 1, 'value', None, '행장', 0, 2, 2, 2)])


# ── §6.3 — metadata ──────────────────────────────────────────────────────────

def test_field_occurrence_counts_every_d1_finding_in_that_field():
    strict_equal(findings(soup(''), {'a': '대표자 홍길동 대표자 김철수'}),
                 [meta_finding(['a'], '대표자', 0, 3, 1, 2, 1, 2),
                  meta_finding(['a'], '대표자', 8, 11, 2, 2, 2, 2)])


def test_field_occurrence_is_across_tokens_not_per_token():
    strict_equal(findings(soup(''), {'a': 'CEO 대표자'}),
                 [meta_finding(['a'], 'ceo', 0, 3, 1, 2, 1, 2),
                  meta_finding(['a'], '대표자', 4, 7, 2, 2, 2, 2)])


def test_field_count_is_counted_after_longest_selection():
    # Four candidates (공동대표 · 공동대표이사 · 대표이사 · 대표자), two occurrences.
    strict_equal(findings(soup(''), {'a': '공동대표이사 대표자'}),
                 [meta_finding(['a'], '공동대표이사', 0, 6, 1, 2, 1, 2),
                  meta_finding(['a'], '대표자', 7, 10, 2, 2, 2, 2)])


def test_the_same_value_in_two_fields_is_two_findings():
    strict_equal(findings(soup(''), {'row_text': '대표자 X', 'item_text': '대표자 X'}),
                 [meta_finding(['item_text'], '대표자', 0, 3, 1, 1, 1, 2),
                  meta_finding(['row_text'], '대표자', 0, 3, 1, 1, 2, 2)])


def test_paths_keep_their_types():
    # A list index is an int and a dict key is a str, even when both read "0".
    strict_equal(findings(soup(''), {'events': ['대표자']}),
                 [meta_finding(['events', 0], '대표자', 0, 3, 1, 1, 1, 1)])
    strict_equal(findings(soup(''), {'events': {'0': '대표자'}}),
                 [meta_finding(['events', '0'], '대표자', 0, 3, 1, 1, 1, 1)])


def test_a_nested_path_is_complete():
    metadata = {'recorded_extraction': {'exception': {'args': ['x', ['대표자']]}}}
    strict_equal(findings(soup(''), metadata),
                 [meta_finding(['recorded_extraction', 'exception', 'args', 1, 0],
                               '대표자', 0, 3, 1, 1, 1, 1)])


def test_list_indexes_order_numerically():
    values = ['x'] * 11
    values[2] = values[10] = '대표자'
    strict_equal(findings(soup(''), {'a': values}),
                 [meta_finding(['a', 2], '대표자', 0, 3, 1, 1, 1, 2),
                  meta_finding(['a', 10], '대표자', 0, 3, 1, 1, 2, 2)])


def test_keys_order_by_code_point():
    # 'Z' (U+005A) < 'a' (U+0061) < '가' (U+AC00), whatever the dict order.
    strict_equal(findings(soup(''), {'가': '대표자', 'a': '대표자', 'Z': '대표자'}),
                 [meta_finding(['Z'], '대표자', 0, 3, 1, 1, 1, 3),
                  meta_finding(['a'], '대표자', 0, 3, 1, 1, 2, 3),
                  meta_finding(['가'], '대표자', 0, 3, 1, 1, 3, 3)])


def test_metadata_positions_are_normalized_code_points():
    strict_equal(findings(soup(''), {'a': '㈜대표자', 'b': 'ＣＥＯ'}),
                 [meta_finding(['a'], '대표자', 3, 6, 1, 1, 1, 2),
                  meta_finding(['b'], 'ceo', 0, 3, 1, 1, 2, 2)])


def test_metadata_keys_are_not_a_source():
    # Keys are checked by the closed schema, not scanned (§1.2).
    assert findings(soup(''), {'대표자': 'x'}) == []


def test_non_string_leaves_are_not_scanned():
    assert findings(soup(''), {'a': 1, 'b': None, 'c': True, 'd': 1.5, 'e': []}) == []


def test_fields_are_never_joined():
    assert findings(soup(''), {'a': '대표', 'b': '자'}) == []


# ── §6.4 — one numbering over every source ───────────────────────────────────

def test_numbering_runs_text_then_attributes_then_metadata():
    html = '<p title="대표자">대표자</p>'
    strict_equal(findings(soup(html), {'a': '대표자'}),
                 [text_finding([0], 0, '대표자', 0, 3, 1, 3),
                  attr_finding([0], 0, 'value', None, '대표자', 0, 3, 2, 3),
                  meta_finding(['a'], '대표자', 0, 3, 1, 1, 3, 3)])


def test_the_same_phrase_in_html_and_metadata_is_two_findings():
    strict_equal(findings(soup('<p>대표자 홍길동</p>'),
                          {'recorded_extraction': {'events': [{'labels': {'item_text': '대표자 홍길동'}}]}}),
                 [text_finding([0], 0, '대표자', 0, 3, 1, 2),
                  meta_finding(['recorded_extraction', 'events', 0, 'labels', 'item_text'],
                               '대표자', 0, 3, 1, 1, 2, 2)])


def test_removed_candidates_never_consume_a_number():
    strict_equal(findings(soup('<p>공동대표이사</p><p>대표자</p>'), {}),
                 [text_finding([0], 0, '공동대표이사', 0, 6, 1, 2),
                  text_finding([1], 0, '대표자', 0, 3, 2, 2)])


def test_no_title_anywhere_is_an_empty_list():
    assert findings(soup('<p title="환율">환율</p>'), {'a': '환율'}) == []


# ── Refusal is not zero ──────────────────────────────────────────────────────
# Each of these must raise. Returning [] would read as "checked, nothing found".

@pytest.mark.parametrize('html', [
    '<p>대<x-vendor>표</x-vendor>자 홍길동</p>',     # unsupported element (§3.2 rule 9)
    '<p><span>ᄃ</span><b>ᅢ</b>표자 홍길동</p>',     # normalization across nodes (§4.3)
    '<p><!-- 홍길동 -->대표자</p>',                     # unsupported text kind (§2.2)
])
def test_an_unreadable_page_raises_and_says_nothing_about_it(html):
    with pytest.raises(CaptureError) as caught:
        findings(soup(html), {})
    assert '홍길동' not in str(caught.value)


@pytest.mark.parametrize('metadata', [
    {'a': ('홍길동',)},          # a tuple is not a JSON value
    {'a': {'홍길동'}},           # neither is a set
    {'a': b'\xed\x99\x8d'},      # nor bytes
    {('홍길동',): 'x'},          # a key that is not a string
    ['홍길동'],                  # the top level is an object (§6.3)
    '홍길동',
])
def test_metadata_that_is_not_a_json_object_tree_raises_without_echoing_it(metadata):
    with pytest.raises(CaptureError) as caught:
        findings(soup(''), metadata)
    assert '홍길동' not in str(caught.value)
    assert '\\xed\\x99\\x8d' not in str(caught.value)     # bytes echoed by repr()


def test_an_attribute_value_of_an_unknown_type_raises():
    page = soup('<div></div>')
    page.div['data-x'] = 5
    with pytest.raises(CaptureError):
        findings(page, {})


def test_a_list_attribute_element_of_an_unknown_type_raises_without_echoing_it():
    page = soup('<div class="a"></div>')
    page.div['class'] = ['a', ('홍길동',)]
    with pytest.raises(CaptureError) as caught:
        findings(page, {})
    assert '홍길동' not in str(caught.value)


def test_a_subtree_is_not_a_document():
    # §3.1: paths start at the document root. Given a tag, every owner_path and
    # element_path would silently be relative to it and match no approval record.
    page = soup('<div><p title="대표자">대표자</p></div>')
    with pytest.raises(CaptureError):
        findings(page.div, {})


# ── What a finding may carry ─────────────────────────────────────────────────

def test_no_finding_carries_the_text_it_was_found_in():
    html = '<p title="대표자 홍길동" data-대표자홍길동="1">대표자 홍길동</p>'
    for finding in findings(soup(html), {'a': '대표자 홍길동'}):
        assert '홍길동' not in repr(finding)


def test_the_input_is_not_modified():
    html = '<div title="대표자"><p>대표자 홍길동</p></div>'
    page = soup(html)
    metadata = {'a': ['대표자', {'b': '대표자'}]}
    before = (str(page), repr(metadata))
    findings(page, metadata)
    assert (str(page), repr(metadata)) == before
