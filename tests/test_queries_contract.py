"""D1 slice 5b-1 contract — `query_key` lives in a registry-free module so the detector's import closure stays pure.

Why: the admission layer (slice 5b-2) reuses the detector, and the D1 policy manifest must hold every file in the entry
modules' import closure. `detector -> registry -> runtime` pulled in `runtime`, which imports the app with `importlib`
(forbidden in that closure). Moving the pure function breaks the chain; nothing about the call shape changes.
"""

from pathlib import Path

import pytest

from tests.test_d1_digest_contract import _imports
from tools.fixture_capture import detector, queries, recorder, registry
from tools.fixture_capture.errors import CaptureError

PACKAGE = Path(__file__).resolve().parents[1] / "tools" / "fixture_capture"


def closure(*entries):
    seen, queue = set(), list(entries)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(_imports(PACKAGE / f"{name}.py") - seen)
    return seen


def test_there_is_one_query_key_and_the_old_names_are_the_same_function():
    assert queries.query_key.__module__ == "tools.fixture_capture.queries"
    assert registry.query_key is queries.query_key
    assert detector.query_key is queries.query_key
    assert recorder.query_key is queries.query_key


def test_the_detector_no_longer_reaches_the_registry_or_the_app_boundary():
    assert closure("detector") == {"detector", "queries", "errors"}
    assert closure("queries") == {"queries", "errors"}


def test_the_registry_still_owns_the_app_boundary():
    # The split must not hide runtime: the registry still derives selectors from the app. (Its full closure cannot be
    # walked here — the walker refuses runtime's dynamic import, which is exactly why the detector must not reach it.)
    assert {"queries", "runtime"} <= _imports(PACKAGE / "registry.py")
    with pytest.raises(AssertionError, match="dynamic import"):
        _imports(PACKAGE / "runtime.py")


@pytest.mark.parametrize("call, expected", [
    (("select", ["a > b"], {}), ("select", (("str", "a > b"),), ())),
    (("find_all", ["td"], {"recursive": False, "limit": 2}),
     ("find_all", (("str", "td"),), (("limit", ("int", 2)), ("recursive", ("bool", False))))),
    (("select_one", ["x", None], {}), ("select_one", (("str", "x"), ("NoneType", None)), ())),
    (("find_all", [["td", "th"]], {}), ("find_all", (("list", (("str", "td"), ("str", "th"))),), ())),
    (("find", [], {"recursive": True}), ("find", (), (("recursive", ("bool", True)),))),
])
def test_the_call_shape_is_unchanged(call, expected):
    assert queries.query_key(*call) == expected


def test_bool_and_int_stay_distinct():
    assert queries.query_key("find", [1], {}) != queries.query_key("find", [True], {})


@pytest.mark.parametrize("bad", [1.5, ("tuple",), {"k": "v"}, b"bytes"])
def test_unsupported_argument_types_refuse_with_the_same_code(bad):
    with pytest.raises(CaptureError) as caught:
        queries.query_key("select", [bad], {})
    assert (caught.value.rule, caught.value.location) == ("query_arguments", "registry")
