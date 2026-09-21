"""D1 §3-§5 — runs, the title grammar, and why both observations are kept.

The two observation sources look redundant until you try to drop one: each finds
what the other cannot, so both counter-examples are pinned here.
"""

import re

import pytest
from bs4 import BeautifulSoup, NavigableString

from tools.fixture_capture.d1_observe import (TITLES, candidates, normalize,
                                              observe, runs)
from tools.fixture_capture.errors import CaptureError


def soup(html):
    return BeautifulSoup(html, 'html.parser')


def seen(html):
    return sorted({(o['source'], o['token'], o['start'], o['end']) for o in observe(soup(html))})


def tokens(html):
    return sorted({o['token'] for o in observe(soup(html))})


# ── Nothing observed may reach a diagnostic ──────────────────────────────────
# Written before the rest: this module handles run text and normalized strings,
# so page bytes are closer to the surface here than anywhere else in D1.

@pytest.mark.parametrize('html', [
    '<p><span>ᄃ</span><b>ᅢ</b>표자 홍길동</p>',
    '<div data-x="홍길동"><span>ᄃ</span><b>ᅢ</b>x</div>',
])
def test_a_refusal_never_carries_the_text_that_caused_it(html):
    with pytest.raises(CaptureError) as caught:
        observe(soup(html))
    assert '홍길동' not in str(caught.value)
    assert caught.value.rule == 'd1_normalization_boundary_unsupported'
    assert caught.value.location.startswith('fixture.run[')


def test_observations_carry_a_token_and_positions_but_no_text():
    found = observe(soup('<li>대표자 유명순</li>'))
    assert found
    for observation in found:
        assert set(observation) == {'token', 'source', 'run', 'start', 'end',
                                    'node_path', 'node_start', 'node_end'}
        assert '유명순' not in repr(observation)
        # The token comes from the closed list, so it is ours to name; nothing
        # else in the observation may be a slice of the page.
        assert observation['token'] in {token for token, _ in TITLES}


# ── §3.2 rule 9: unreadable structure is refused, never reported as empty ────
# Every case below returned 0 observations before the refusal existed. A gate
# that admits a fixture on "0 observations" would have admitted all of them.

@pytest.mark.parametrize('html,rule', [
    ('<p>대<x-part>표</x-part>자</p>', 'd1_unsupported_element'),
    ('<ruby></ruby>', 'd1_unsupported_element'),
    ('<p>대<!--표-->자</p>', 'd1_unsupported_text_kind'),
])
def test_an_unreadable_structure_raises_instead_of_observing_nothing(html, rule):
    with pytest.raises(CaptureError) as caught:
        observe(soup(html))
    assert caught.value.rule == rule


def test_a_title_hidden_behind_an_unknown_tag_is_refused_not_missed():
    # The whole point. Treating the unknown tag as a block splits `대표자` into
    # three runs, so the name beside it goes unobserved and a fixture carrying it
    # looks clean. Measured before the fix: 0 observations.
    with pytest.raises(CaptureError) as caught:
        observe(soup('<p>대<x-vendor>표</x-vendor>자 홍길동</p>'))
    assert caught.value.rule == 'd1_unsupported_element'
    assert '홍길동' not in str(caught.value)


def test_a_comment_is_not_spliced_into_the_run_text():
    # Comment is a NavigableString subclass, so `isinstance` would join its text
    # into the run and report a title assembled partly from a comment — whose
    # coordinates §7 would then try to substitute in. The refusal comes first,
    # and `runs` uses the exact type so the two statements cannot drift apart.
    with pytest.raises(CaptureError):
        runs(soup('<p>대<!--표-->자</p>'))


def test_runs_refuses_on_its_own_not_only_through_observe():
    # `runs` is reachable directly; a rule enforced only in `observe` would leave
    # this entry point open.
    with pytest.raises(CaptureError):
        runs(soup('<div><bsib:mnu></bsib:mnu></div>'))


# ── §3 runs ──────────────────────────────────────────────────────────────────

def test_a_child_block_splits_the_parent_run_and_numbering_is_per_owner():
    shape = [(path, index, ''.join(text for text, _ in pieces))
             for path, index, pieces in
             runs(soup('<div>대<span>표</span>자 홍길동<p>환율</p>은<b>행</b>장 김철수</div>'))]
    assert shape == [([0], 0, '대표자 홍길동'), ([0, 3], 0, '환율'), ([0], 1, '은행장 김철수')]


def test_a_fragment_belongs_to_the_virtual_root():
    assert [(p, i) for p, i, _ in runs(soup('대<span>표</span>자 홍길동'))] == [([], 0)]


@pytest.mark.parametrize('html,expected', [
    ('<body><div>ABOUT</div><div>CEO홍길동</div></body>', ['ABOUT', 'CEO홍길동']),
    ('<div><p>대</p><p>표자 홍길동</p></div>', ['대', '표자 홍길동']),
])
def test_block_boundaries_are_never_joined(html, expected):
    assert [''.join(t for t, _ in pieces) for _, _, pieces in runs(soup(html))] == expected


def test_br_contributes_one_line_feed_and_wbr_contributes_nothing():
    assert [''.join(t for t, _ in p) for _, _, p in runs(soup('<p>a<br>b</p>'))] == ['a\nb']
    assert [''.join(t for t, _ in p) for _, _, p in runs(soup('<p>a<wbr>b</p>'))] == ['ab']


# ── §4 grammar ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('html,expected', [
    ('<li>대표자 유명순</li>', ['대표자']),
    # `br` is a line feed, not a boundary: a title may be broken across one.
    ('<p>대표<br>자 홍길동</p>', ['대표자']),
    ('<p>C.E.O. 홍길동</p>', ['ceo']),
    # Both spellings are candidates; §6 is what reduces them to one. `ceo` is not
    # among them — `c` is followed by `h`, and the grammar allows only whitespace
    # between a token's letters.
    ('<p>Chief Executive Officer 홍길동</p>',
     ['chief executive', 'chief executive officer']),
    ('<p>Chief‑Executive 홍길동</p>', ['chief executive']),
    ('<p>ＣＥＯ홍길동</p>', ['ceo']),
    ('<p>은행장 김철수</p>', ['은행장', '행장']),
    # Documented non-candidates.
    ('<p>ABOUTCEO홍길동</p>', []),
    ('<p>CEOLOGY</p>', []),
    ('<p>ChiefExecutiveOfficer</p>', []),
    ('<li>매매기준율 1,553.83</li>', []),
])
def test_the_closed_title_list_decides_what_is_a_candidate(html, expected):
    assert tokens(html) == sorted(expected)


def test_why_the_english_boundary_is_not_written_with_a_word_boundary():
    # Pinned because the reason is easy to state backwards: Python's `\b` is
    # Unicode-aware, 홍 is a word character, so there is no boundary after `ceo`
    # and the title we must catch is missed. `re.ASCII` would fix this one input
    # but is a different rule than the spec's lookarounds.
    assert re.compile(r'\bceo\b').search('ceo홍길동') is None
    assert re.compile(r'\bceo\b', re.ASCII).search('ceo홍길동') is not None
    assert tokens('<p>CEO홍길동</p>') == ['ceo']
    # And the input the boundary exists to reject stays rejected either way.
    assert re.compile(r'\bceo\b').search('aboutceo홍길동') is None
    assert tokens('<p>ABOUTCEO홍길동</p>') == []


def test_normalization_is_nfkc_plus_ascii_case_only():
    assert normalize('ＣＥＯ') == 'ceo'
    assert normalize('‑') == '‐'
    # Not casefold: that maps ß to ss and would change lengths we depend on.
    assert normalize('ß') == 'ß'


def test_overlapping_spellings_all_reach_the_selection_step():
    # §6 cannot choose the longest unless the parts and the whole are all present.
    # Per-token scanning is what preserves them; one merged alternation would
    # return a single match here.
    assert {token for _, _, token in candidates('공동대표이사')} == {
        '공동대표', '대표이사', '공동대표이사'}


# ── §5 both observations ─────────────────────────────────────────────────────

def test_a_node_finds_the_title_its_run_lost_to_the_neighbour():
    # The run reads `ABOUTCEO홍길동`, where `(?<![a-z])` fails. The node is its
    # own string, and its candidate moves to run coordinates [5,8).
    assert seen('<p><span>ABOUT</span><b>CEO홍길동</b></p>') == [('node', 'ceo', 5, 8)]


def test_a_run_finds_the_title_its_nodes_were_split_across():
    assert seen('<p>대<b>표자</b> 홍길동</p>') == [('run', '대표자', 0, 3)]


def test_node_observations_carry_both_coordinate_systems():
    found = [o for o in observe(soup('<p>ＡＢ<b>ＣＥＯ홍길동</b></p>')) if o['source'] == 'node']
    # `ＡＢ` normalizes to two characters, so the run position is shifted by two
    # while the node-internal position stays at the start of its own string. §6
    # merges on the former; §7 checks a substitution against the latter.
    assert [(o['start'], o['end'], o['node_start'], o['node_end']) for o in found] == [(2, 5, 0, 3)]


def test_the_run_offset_counts_normalized_characters_not_original_ones():
    # `ﬁ` is one character that normalizes to two. Accumulating raw lengths would
    # put the candidate at 1 instead of 2 — the miscount §4.3 exists to prevent,
    # and invisible to any test whose pieces keep their length.
    found = [o for o in observe(soup('<p>ﬁ<b>CEO홍길동</b></p>')) if o['source'] == 'node']
    assert [(o['start'], o['end']) for o in found] == [(2, 5)]


def test_a_run_observation_has_no_node_coordinates():
    found = [o for o in observe(soup('<p>대<b>표자</b> 홍길동</p>')) if o['source'] == 'run']
    assert [(o['node_path'], o['node_start'], o['node_end']) for o in found] == [(None, None, None)]


def test_a_zero_length_text_node_does_not_consume_a_segment_index():
    # A structurally valid tree can hold an empty string; emitting a run for it
    # would push the next run's segment_index to 1 and break every identifier
    # built on it (§3.2 rule 8).
    tree = soup('<div><p>x</p>대표자</div>')
    tree.find('div').insert(0, NavigableString(''))
    shape = [(path, index, ''.join(text for text, _ in pieces))
             for path, index, pieces in runs(tree)]
    assert shape == [([0, 1], 0, 'x'), ([0], 0, '대표자')]


# ── §4.3 the check that makes those offsets meaningful ───────────────────────

def test_composition_across_a_node_boundary_is_refused_not_reported_as_absent():
    # Both strings render as 대표자 홍길동; only the whole-run normalization
    # composes the jamo. Treating this as "no candidate" would be a false zero.
    with pytest.raises(CaptureError) as caught:
        observe(soup('<p><span>ᄃ</span><b>ᅢ</b>표자 홍길동</p>'))
    assert caught.value.rule == 'd1_normalization_boundary_unsupported'


def test_decomposed_jamo_inside_one_node_is_supported():
    # Written out as code points: a literal 대 in this file is already composed,
    # so it would pass whatever the implementation did and pin nothing.
    decomposed = '대표자 홍길동'
    assert decomposed != '대표자 홍길동'
    assert tokens(f'<p>{decomposed}</p>') == ['대표자']


def test_the_boundary_check_runs_even_when_nothing_matched():
    with pytest.raises(CaptureError):
        observe(soup('<p><span>ᄃ</span><b>ᅢ</b>x</p>'))
