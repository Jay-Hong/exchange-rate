"""D1 slice 5a contract — the capture path runs the reviewed replacement and both storage checks, inside one parse budget.

Written before the implementation (Claude) from the slice-5 design agreement with Codex (2026-09-21, r1–r2) and
`d1_detection_policy_v1.txt` §7.1–§7.2 (+ amendment 1). The implementer reads this file and does not edit it.

Pinned wiring (order agreed in design r1):
  roundtrip: parse ① → record → deidentify → `replace_names(fixture, route.name, original)` → encode → size check →
             `verify_stored(serialized, replacements, parse=<budgeted>)` ② → reparse ③ → replay equality
             → returns `(serialized, reparsed, original, replacements)`
  capture_route: owns one `[PARSE_SECONDS]` budget, passes it to `roundtrip(..., parse_budget=)`, and after
             `validate_metadata` runs `verify_stored(fixture, replacements, metadata=meta, parse=<budgeted>)` ④.
- `roundtrip(..., parse_budget=None)` creates its own budget only when none is given, and consumes the given list in place.
- `verify_stored(..., *, parse=None)`: None keeps 4A behaviour; a given `parse(text)` is used for the reparse; an ordinary
  exception from it is `d1_fixture_parse`; a `DeadlineExpired` passes through untouched.
- The parse budget accounting is the existing `parse_html` one (`roundtrip.time.monotonic`, `roundtrip.BeautifulSoup`);
  this contract controls that clock and parser to make the shared budget observable without real sleeping.
- `replacements` stay in memory: never in `Artifact` or metadata.
D1 now gates **every** route: a route with no registry entry has nothing to replace, but any D1 finding still refuses.
Why the capture wiring is not in the policy digest manifest: approval binds the final stored bytes and the current D1
judgement; whether the capture ran the replacement is proved by these wiring tests and by re-capture (design r1).
"""

import inspect
import json
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from tests._fixture_capture import REVIEWED_NAME, citi_html, disclosure, getter, mibank_html, official_html
from tools.fixture_capture import capture as capture_module
from tools.fixture_capture import d1_replace
from tools.fixture_capture import roundtrip as roundtrip_module
from tools.fixture_capture.capture import capture_route
from tools.fixture_capture.errors import CaptureError, DeadlineExpired
from tools.fixture_capture.limits import PARSE_SECONDS, Deadline
from tools.fixture_capture.registry import Registry
from tools.fixture_capture.roundtrip import roundtrip

PLACED = "대표자 [성명삭제]"
CITI_ROUTES = ("citi_primary", "citi_secondary")
OTHER_ROUTES = ("bs_official", "bs_mibank", "citi_mibank")
ALL_ROUTES = CITI_ROUTES + OTHER_ROUTES
TITLED_HEADER = "text/html; charset=utf-8; note=대표자"


@pytest.fixture
def registry():
    return Registry()


def page(route, **kwargs):
    if route == "citi_primary":
        return citi_html(**kwargs)
    if route in ("citi_secondary", "bs_official"):
        return official_html(route, **kwargs)
    return mibank_html(**kwargs)


def with_title(html, title="<p>대표이사 김철수</p>"):
    # As the body's last child, so the reviewed disclosure keeps its position as the second child.
    assert html.count("</body>") == 1
    return html.replace("</body>", title + "</body>")


def capture(route, registry, html=None, **response):
    get, _ = getter(page(route) if html is None else html, **response)
    return capture_route(route, registry=registry, get=get)


def refused(call, rule=None):
    with pytest.raises(CaptureError) as caught:
        call()
    if rule is not None:
        assert caught.value.rule == rule, (caught.value.rule, caught.value.location)
    assert REVIEWED_NAME not in str(caught.value)
    return caught.value


# ── roundtrip performs the reviewed replacement ──────────────────────────────

@pytest.mark.parametrize("route", CITI_ROUTES)
def test_roundtrip_replaces_the_reviewed_name_and_returns_the_evidence(registry, route):
    serialized, reparsed, original, replacements = roundtrip(page(route), registry.routes[route], registry, Deadline())
    text = serialized.decode("utf-8")
    assert REVIEWED_NAME not in text and PLACED in text
    assert [item.text for item in replacements] == [PLACED]
    assert REVIEWED_NAME not in json.dumps(original, ensure_ascii=False)
    assert reparsed.get_text().count(PLACED) == 1


@pytest.mark.parametrize("route", OTHER_ROUTES)
def test_a_route_without_a_registry_entry_replaces_nothing(registry, route):
    *_, replacements = roundtrip(page(route), registry.routes[route], registry, Deadline())
    assert tuple(replacements) == ()


@pytest.mark.parametrize("route", CITI_ROUTES)
def test_a_citi_page_without_the_reviewed_disclosure_is_refused(registry, route):
    refused(lambda: roundtrip(page(route, name=None), registry.routes[route], registry, Deadline()),
            "d1_replacement_selector")


def test_the_replacement_runs_on_the_sanitized_page(registry):
    # A comment beside the disclosure is removed by the existing sanitization. Replacing before it would bind the
    # evidence to a path that the stored bytes no longer have (and D1 cannot read a comment at all).
    html = citi_html(name=None).replace("</body>", "<!-- note -->" + disclosure() + "</body>")
    serialized, _, _, replacements = roundtrip(html, registry.routes["citi_primary"], registry, Deadline())
    assert b"<!--" not in serialized and PLACED.encode() in serialized and len(replacements) == 1


def test_the_name_left_in_the_extraction_record_is_refused(registry):
    html = citi_html(labels=("USD " + REVIEWED_NAME, "CNY", "EUR", "JPY"))
    refused(lambda: roundtrip(html, registry.routes["citi_primary"], registry, Deadline()),
            "d1_removed_value_in_extraction")


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_any_other_title_on_the_page_is_refused_on_every_route(registry, route):
    html = with_title(page(route))
    refused(lambda: roundtrip(html, registry.routes[route], registry, Deadline()), "d1_replacement_correspondence")
    refused(lambda: capture(route, registry, html), "d1_replacement_correspondence")


# ── capture_route: the final check with full metadata, and nothing leaks ─────

@pytest.mark.parametrize("route", CITI_ROUTES)
def test_the_artifact_holds_the_placeholder_and_no_evidence(registry, route):
    artifact = capture(route, registry)
    assert set(vars(artifact)) == {"fixture", "metadata", "capture_id", "route"}
    assert PLACED.encode() in artifact.fixture and REVIEWED_NAME.encode() not in artifact.fixture
    assert REVIEWED_NAME.encode() not in artifact.metadata
    meta = json.loads(artifact.metadata)
    assert "replacements" not in meta and "replacements" not in meta["recorded_extraction"]


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_a_title_only_in_the_metadata_is_refused_by_the_final_check(registry, route):
    # The response header is page-derived metadata that roundtrip never sees: only the check after
    # validate_metadata, with the full metadata, can catch it.
    refused(lambda: capture(route, registry, content_type=TITLED_HEADER), "d1_replacement_correspondence")


@pytest.mark.parametrize("route", ["citi_primary", "bs_mibank"])
def test_the_final_check_runs_after_the_metadata_schema(registry, route):
    # Both an identifier and a title in the header: the closed-schema validation must speak first, because
    # D1 reads metadata only after it has passed that schema (§1.2).
    refused(lambda: capture(route, registry, content_type=TITLED_HEADER + " 12345678901"), "long_number")


# ── verify_stored's parse hook ───────────────────────────────────────────────

def _stored_citi(registry):
    serialized, _, _, replacements = roundtrip(citi_html(), registry.routes["citi_primary"], registry, Deadline())
    return serialized, replacements


def test_the_given_parser_is_the_one_whose_result_is_verified(registry):
    serialized, replacements = _stored_citi(registry)
    seen = []

    def parse(text):
        seen.append(text)
        return BeautifulSoup(text.replace("[성명삭제]", "[삭제]"), "html.parser")

    refused(lambda: d1_replace.verify_stored(serialized, replacements, parse=parse), "d1_replacement_correspondence")
    assert seen == [serialized.decode("utf-8")]
    d1_replace.verify_stored(serialized, replacements)                        # None: the 4A default still verifies


def test_an_ordinary_parser_failure_is_the_policy_parse_refusal(registry):
    serialized, replacements = _stored_citi(registry)

    def parse(text):
        raise ValueError("/Users/someone/private " + REVIEWED_NAME)

    error = refused(lambda: d1_replace.verify_stored(serialized, replacements, parse=parse), "d1_fixture_parse")
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


@pytest.mark.parametrize("rule", ["parse_timeout", "total_timeout"])
def test_a_deadline_passes_through_as_the_same_object(registry, rule):
    serialized, replacements = _stored_citi(registry)
    expired = DeadlineExpired(rule)

    def parse(text):
        raise expired

    with pytest.raises(DeadlineExpired) as caught:
        d1_replace.verify_stored(serialized, replacements, parse=parse)
    assert caught.value is expired


def test_the_stored_bytes_check_really_verifies_the_second_parse(registry, monkeypatch):
    # ② reparses the stored bytes. Corrupt only what that parse sees: a plain reparse, or a check moved after the
    # replay (③ does not read the footer), would still succeed.
    real, calls = roundtrip_module.BeautifulSoup, []

    def parse(text, parser):
        calls.append(len(text))
        if len(calls) == 2:
            text = text.replace("[성명삭제]", "[삭제]")
        return real(text, parser)

    monkeypatch.setattr(roundtrip_module, "BeautifulSoup", parse)
    refused(lambda: roundtrip(citi_html(), registry.routes["citi_primary"], registry, Deadline()),
            "d1_replacement_correspondence")
    assert len(calls) == 2


# ── one parse budget across all four parses ──────────────────────────────────

class _Clock:
    """Each parse through roundtrip's BeautifulSoup costs `cost` seconds on roundtrip's own clock."""

    def __init__(self, monkeypatch, cost):
        self.now, self.cost, self.calls = 1000.0, cost, 0
        real = roundtrip_module.BeautifulSoup

        def parse(text, parser):
            self.calls += 1
            self.now += self.cost
            return real(text, parser)

        monkeypatch.setattr(roundtrip_module, "time", SimpleNamespace(monotonic=lambda: self.now))
        monkeypatch.setattr(roundtrip_module, "BeautifulSoup", parse)


def test_roundtrip_consumes_the_budget_it_is_given_in_place(registry, monkeypatch):
    clock = _Clock(monkeypatch, 1.0)
    budget = [PARSE_SECONDS]
    roundtrip(citi_html(), registry.routes["citi_primary"], registry, Deadline(), parse_budget=budget)
    assert clock.calls == 3                                       # ① original, ② stored-bytes check, ③ replay
    assert budget[0] == pytest.approx(PARSE_SECONDS - 3.0)


def test_a_given_budget_is_spent_from_its_current_value(registry, monkeypatch):
    # 1.5 s left and 1.0 s per parse: ① leaves 0.5, ② leaves -0.5, ③ cannot start. Refilling the list would pass.
    clock = _Clock(monkeypatch, 1.0)
    with pytest.raises(DeadlineExpired, match="parse_timeout"):
        roundtrip(citi_html(), registry.routes["citi_primary"], registry, Deadline(), parse_budget=[1.5])
    assert clock.calls == 2


def test_an_exhausted_budget_parses_nothing(registry, monkeypatch):
    clock = _Clock(monkeypatch, 1.0)
    with pytest.raises(DeadlineExpired, match="parse_timeout"):
        roundtrip(citi_html(), registry.routes["citi_primary"], registry, Deadline(), parse_budget=[0.0])
    assert clock.calls == 0


def test_the_stored_bytes_check_inside_roundtrip_spends_the_shared_budget(registry, monkeypatch):
    # 5.1 s each: ① leaves 4.9, ② (verify_stored) leaves -0.2, so ③ cannot start. A verify_stored that
    # parsed outside the budget would let ③ and ④ run and the capture succeed.
    clock = _Clock(monkeypatch, 5.1)
    refused(lambda: capture("citi_primary", registry), "parse_timeout")
    assert clock.calls == 2


def test_the_final_check_in_capture_spends_the_same_budget(registry, monkeypatch):
    # 3.4 s each: ① 6.6, ② 3.2, ③ -0.2, so ④ (the final verify_stored) cannot start.
    clock = _Clock(monkeypatch, 3.4)
    refused(lambda: capture("citi_primary", registry), "parse_timeout")
    assert clock.calls == 3


def test_within_budget_all_four_parses_run(registry, monkeypatch):
    clock = _Clock(monkeypatch, 2.0)
    capture("citi_primary", registry)
    assert clock.calls == 4


def test_the_capture_budget_is_the_parse_budget_constant(registry, monkeypatch):
    seen = []
    original = capture_module.roundtrip

    def spy(*args, **kwargs):
        # Positional or keyword: the call style is not part of the contract, the budget is.
        budget = inspect.signature(original).bind(*args, **kwargs).arguments["parse_budget"]
        seen.append(list(budget))
        return original(*args, **kwargs)

    monkeypatch.setattr(capture_module, "roundtrip", spy)
    capture("citi_primary", registry)
    assert seen == [[PARSE_SECONDS]]
