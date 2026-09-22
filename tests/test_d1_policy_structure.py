"""D1 §2 — refusals, with empty U cleanup under amendment 2 §A4.

The removal is judged before cleanup on purpose; several tests here exist only to
pin that ordering, because judging afterwards passes elements nobody inspected.
"""

import pytest
from bs4 import BeautifulSoup, Comment, NavigableString

from tools.fixture_capture.d1_policy import (BLOCK, INLINE, classify, is_empty_unsupported,
                                             validate_structure)
from tools.fixture_capture.deidentify import deidentify
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.registry import Registry


@pytest.fixture
def registry():
    return Registry()


def soup(html):
    return BeautifulSoup(html, 'html.parser')


def test_the_two_lists_do_not_overlap_and_u_is_their_complement():
    assert not INLINE & BLOCK
    for name in ('span', 'li', 'td'):
        assert classify(name) in ('I', 'B')
    # U is defined as the complement, so anything unlisted lands there — including
    # namespaced vendor tags, which a hand-written example list would have missed.
    for name in ('ruby', 'rt', 'svg', 'bsib:mnu', 'x-part'):
        assert classify(name) == 'U'
    # The whole parsed name is the key; a prefix is not stripped to reach `span`.
    assert classify('vendor:span') == 'U'


@pytest.mark.parametrize('html,rule', [
    ('<div><bsib:mnu id="x"></bsib:mnu></div>', 'd1_unsupported_element'),
    ('<ruby>홍길동<rt>대표자</rt></ruby>', 'd1_unsupported_element'),
    ('<div><script>var a=1</script></div>', 'd1_nonempty_inert_element'),
    ('<div><template> </template></div>', 'd1_nonempty_inert_element'),
    ('<div><!-- c --></div>', 'd1_unsupported_text_kind'),
])
def test_structures_this_policy_cannot_read_are_refused(html, rule):
    with pytest.raises(CaptureError) as caught:
        validate_structure(soup(html))
    assert caught.value.rule == rule


@pytest.mark.parametrize('html', ['<li>대표자 유명순</li>', '<p>대<span>표자</span> 홍길동</p>',
                                  '<div><script></script></div>', '<p>a<br>b</p>'])
def test_supported_structures_pass(html):
    validate_structure(soup(html))


# A tag name is page-derived like any text, and an unsupported document can name
# its elements anything at all. An earlier version put `node.name` in the location
# and leaked a person's name out of `<x-person-홍길동>`.
@pytest.mark.parametrize('html,secret', [
    ('<x-person-홍길동>sample</x-person-홍길동>', '홍길동'),
    ('<div data-user="홍길동"><bsib:mnu>x</bsib:mnu></div>', '홍길동'),
    ('<div><!-- 대표자 홍길동 --></div>', '홍길동'),
    ('<div><script>var name="홍길동"</script></div>', '홍길동'),
])
def test_refusals_report_a_position_and_never_the_page(html, secret):
    with pytest.raises(CaptureError) as caught:
        validate_structure(soup(html))
    assert secret not in str(caught.value)
    assert secret not in caught.value.location
    assert caught.value.location.startswith('fixture.nodes[')


def test_void_element_with_children_is_refused():
    # html.parser will not produce this from markup — `<br>x</br>` makes x a
    # sibling — so build it directly, otherwise the rule is never exercised.
    tree = soup('<p><br></p>')
    tree.find('br').append(NavigableString('x'))
    with pytest.raises(CaptureError) as caught:
        validate_structure(tree)
    assert caught.value.rule == 'd1_invalid_empty_element'
    # Every refusal reports a position, not a name. This one could name a tag
    # without leaking — the eight void names are ours — but a reader cannot tell
    # a safe name from a page-derived one by looking, so the form is uniform.
    assert caught.value.location.startswith('fixture.nodes[')


def test_plain_text_only_cannot_be_expressed_with_isinstance():
    # The reason §2 says "exactly NavigableString": every rejected kind is an
    # instance of it, so isinstance would accept all of them.
    comment = Comment('x')
    assert isinstance(comment, NavigableString)
    assert type(comment) is not NavigableString


@pytest.mark.parametrize('html,removed', [
    ('<div><bsib:mnu></bsib:mnu></div>', True),
    # Everything below carries something at capture time. Cleanup would empty each
    # one, so judging after cleanup would remove all four.
    ('<div><bsib:mnu id="x"></bsib:mnu></div>', False),
    ('<div><bsib:mnu><!-- c --></bsib:mnu></div>', False),
    ('<div><bsib:mnu><input name="q"></bsib:mnu></div>', False),
    ('<div><bsib:mnu><meta charset="utf-8"></bsib:mnu></div>', False),
    # Ruby is refused as a whole by §2.3; removing an empty one would turn that
    # refusal into a pass.
    ('<div><ruby></ruby></div>', False),
    ('<div><rt></rt></div>', False),
])
def test_only_elements_already_empty_at_capture_time_are_removed(registry, html, removed):
    left = [tag.name for tag in deidentify(soup(html), registry).find_all(True)
            if classify(tag.name) == 'U']
    assert (left == []) is removed


def test_an_empty_element_inside_the_selected_subtree_is_kept(registry):
    # Removing it would shift nth-child positions the selectors depend on, so this
    # removal obeys the same boundary as every other one. Without this case the
    # boundary check can be deleted and the suite still passes.
    tree = soup('<div id="content"><ul><li><div>'
                '<bsib:mnu></bsib:mnu>1,553.83</div></li></ul></div>')
    left = [tag.name for tag in deidentify(tree, registry).find_all(True)
            if classify(tag.name) == 'U']
    assert left == ['bsib:mnu']


def test_removal_does_not_cascade_to_a_parent_emptied_by_cleanup(registry):
    # The outer U keeps a child at capture time, so it stays and is refused later.
    tree = soup('<div><bsib:outer><bsib:mnu></bsib:mnu></bsib:outer></div>')
    left = [tag.name for tag in deidentify(tree, registry).find_all(True)
            if classify(tag.name) == 'U']
    assert left == ['bsib:outer']


def test_is_empty_unsupported_reads_the_element_as_given(registry):
    tree = soup('<div><bsib:mnu><!-- c --></bsib:mnu></div>')
    element = tree.find('bsib:mnu')
    assert not is_empty_unsupported(element)
    element.contents[0].extract()
    assert is_empty_unsupported(element)
