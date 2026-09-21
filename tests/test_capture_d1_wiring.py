"""Implementation checks for parser failures and the capture exception boundary."""

import pytest
from bs4 import BeautifulSoup

from tests._fixture_capture import citi_html, getter, no_network
from tools.fixture_capture import capture as capture_module
from tools.fixture_capture import d1_replace
from tools.fixture_capture import roundtrip as roundtrip_module
from tools.fixture_capture.errors import CaptureError, DeadlineExpired
from tools.fixture_capture.registry import Registry


@pytest.fixture
def registry(monkeypatch):
    # These tests exercise capture wiring, with no Git command or live request.
    monkeypatch.setattr(capture_module, "source_identity",
                        lambda registry: ("a" * 40, "c1b/1:" + "b" * 64))
    return Registry()


@pytest.fixture(params=[False, True], ids=["no-replacements", "one-replacement"])
def stored(request):
    if not request.param:
        return b"<p>USD 1300</p>", ()
    soup = BeautifulSoup("<ul><li>대표자 홍길동</li></ul>", "html.parser")
    replacements = d1_replace.replace_names(
        soup, "citi_primary", {},
        entries=(d1_replace.Entry("citi_primary", "ul > li", "대표자"),))
    return soup.encode("utf-8"), replacements


def assert_refusal(error, rule, location):
    assert type(error) is CaptureError
    assert (error.rule, error.location) == (rule, location)
    assert str(error) == f"{location}: {rule}"
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


@pytest.mark.parametrize("rule", ["parse_timeout", "total_timeout"])
@pytest.mark.parametrize("parse_number", [1, 2, 3, 4],
                         ids=["original", "stored", "replay", "metadata"])
def test_deadlines_at_each_parse_become_safe_capture_errors(
        registry, monkeypatch, rule, parse_number):
    expired = DeadlineExpired(rule)
    real_parse = roundtrip_module.BeautifulSoup
    calls = 0

    def parse(text, parser):
        nonlocal calls
        calls += 1
        if calls == parse_number:
            raise expired
        return real_parse(text, parser)

    monkeypatch.setattr(roundtrip_module, "BeautifulSoup", parse)
    get, response = getter(citi_html())
    with pytest.raises(CaptureError) as caught:
        capture_module.capture_route("citi_primary", registry=registry, get=get)
    assert_refusal(caught.value, rule, "capture")
    assert caught.value.__context__ is expired
    assert calls == parse_number
    get.assert_called_once()
    assert response.raw.closed


@pytest.mark.parametrize("invalid", [None, False, "<p>USD</p>", {},
                                   BeautifulSoup("<p>USD</p>", "html.parser").p],
                         ids=["none", "bool", "text", "dict", "tag"])
def test_parser_must_return_a_soup_even_without_replacements(stored, invalid):
    fixture, replacements = stored
    seen = []

    def parse(text):
        seen.append(text)
        return invalid

    with pytest.raises(CaptureError) as caught:
        d1_replace.verify_stored(fixture, replacements, parse=parse)
    assert_refusal(caught.value, "d1_fixture_parse", "fixture")
    assert seen == [fixture.decode("utf-8")]


def test_a_falsey_parser_is_still_used(stored):
    fixture, replacements = stored

    class Parser:
        def __init__(self):
            self.seen = []

        def __bool__(self):
            return False

        def __call__(self, text):
            self.seen.append(text)
            return BeautifulSoup(text, "html.parser")

    parse = Parser()
    d1_replace.verify_stored(fixture, replacements, parse=parse)
    assert parse.seen == [fixture.decode("utf-8")]


@pytest.mark.parametrize("exception", [TypeError, RecursionError, CaptureError])
def test_ordinary_parser_errors_cannot_supply_the_policy_diagnostic(stored, exception):
    fixture, replacements = stored

    def parse(text):
        raise exception("PRIVATE_VALUE")

    with pytest.raises(CaptureError) as caught:
        d1_replace.verify_stored(fixture, replacements, parse=parse)
    assert_refusal(caught.value, "d1_fixture_parse", "fixture")


@pytest.mark.parametrize("fixture,rule", [
    ("<p>USD</p>", "d1_invalid_fixture_bytes"),
    (b"\xff", "d1_invalid_utf8"),
])
def test_invalid_stored_bytes_are_refused_before_calling_the_parser(fixture, rule):
    calls = []

    def parse(text):
        calls.append(text)
        return BeautifulSoup(text, "html.parser")

    with pytest.raises(CaptureError) as caught:
        d1_replace.verify_stored(fixture, (), parse=parse)
    assert_refusal(caught.value, rule, "fixture")
    assert calls == []


@pytest.mark.parametrize("corruption,rule", [
    ("placeholder", "d1_replacement_correspondence"),
    ("parser-error", "d1_fixture_parse"),
    ("invalid-root", "d1_fixture_parse"),
])
def test_the_final_storage_check_validates_its_own_parse(registry, monkeypatch, corruption, rule):
    real_parse = roundtrip_module.BeautifulSoup
    calls = 0

    def parse(text, parser):
        nonlocal calls
        calls += 1
        if calls == 4:
            if corruption == "parser-error":
                raise ValueError("PRIVATE_VALUE")
            if corruption == "invalid-root":
                return None
            text = text.replace("[성명삭제]", "[삭제]")
        return real_parse(text, parser)

    monkeypatch.setattr(roundtrip_module, "BeautifulSoup", parse)
    get, _ = getter(citi_html())
    with pytest.raises(CaptureError) as caught:
        capture_module.capture_route("citi_primary", registry=registry, get=get)
    assert_refusal(caught.value, rule, "fixture" if corruption != "placeholder"
                   else caught.value.location)
    assert calls == 4
