"""Implementation-side checks supplementing the read-only slice 4B contract."""

import _markupbase
import copy
import hashlib
import html.parser
import json
import platform
import re
import traceback
from pathlib import Path

import bs4
import pytest

from tools.fixture_capture import d1_digest as G
from tools.fixture_capture.errors import CaptureError


def valid_descriptor():
    return {"policy_spec_sha256": "a" * 64, "policy_amendments": ["b" * 64],
            "implementation_files": {"tools/fixture_capture/example.py": "c" * 64}}


def refused(call, rule, location):
    with pytest.raises(CaptureError) as caught:
        call()
    error = caught.value
    assert (error.rule, error.location) == (rule, location)
    assert re.fullmatch(r"[A-Za-z0-9_.:\[\] -]*", str(error))
    assert "SECRET" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    # Show the rendered chain without source frames (which have their own paths).
    rendered = "".join(traceback.TracebackException.from_exception(error, limit=0).format())
    assert "SECRET" not in rendered
    return error


def test_repeated_descriptors_have_stable_digests_and_separate_nested_objects():
    first, second = G.policy_descriptor(), G.policy_descriptor()
    assert first is not second and first == second
    for key in ("policy_amendments", "implementation_files"):
        assert first[key] is not second[key]
    assert G.policy_digest(first) == G.policy_digest(second)


def test_digest_validation_does_not_mutate_its_input():
    descriptor = valid_descriptor()
    original = copy.deepcopy(descriptor)
    G.policy_digest(descriptor)
    assert descriptor == original


@pytest.mark.parametrize("value", [None, [], "SECRET/descriptor", 1, True])
def test_non_object_descriptors_refuse_without_echoing_the_input(value):
    refused(lambda: G.policy_digest(value), "d1_descriptor_invalid", "policy_descriptor")


@pytest.mark.parametrize("value", ["g" * 64, "a" * 64 + "\n", "SECRET/private", None, False])
def test_invalid_sha256_values_have_only_code_owned_locations(value):
    descriptor = valid_descriptor()
    descriptor["policy_spec_sha256"] = value
    refused(lambda: G.policy_digest(descriptor), "d1_descriptor_invalid", "policy_spec_sha256")


def test_unknown_keys_are_never_copied_into_diagnostics():
    descriptor = valid_descriptor()
    descriptor["/SECRET/private/대표자"] = "ignored"
    refused(lambda: G.policy_digest(descriptor), "d1_descriptor_invalid", "policy_descriptor")


def test_manifest_errors_use_an_index_instead_of_the_path_key():
    descriptor = valid_descriptor()
    descriptor["implementation_files"] = {"/SECRET/private/대표자.py": "bad"}
    refused(lambda: G.policy_digest(descriptor), "d1_descriptor_invalid", "implementation_files[0]")


@pytest.mark.parametrize("value", [Path("/SECRET/private"), b"SECRET/private", {"SECRET/private"},
                                   {"SECRET/private": 1, 2: 3}, {"SECRET/private": "\ud800"}])
def test_json_serialization_errors_suppress_input_and_exception_text(value):
    refused(lambda: G.canonical_json(value), "d1_descriptor_invalid", "canonical_json")


def test_circular_json_refuses_without_an_exception_chain():
    value = ["SECRET/private"]
    value.append(value)
    refused(lambda: G.canonical_json(value), "d1_descriptor_invalid", "canonical_json")


@pytest.mark.parametrize("call, rule, location", [
    (G.policy_descriptor, "d1_digest_unreadable", "policy_spec"),
    (G.runtime_descriptor, "d1_runtime_unavailable", "runtime.html_parser_sha256"),
])
def test_read_failures_suppress_os_paths(monkeypatch, call, rule, location):
    def denied(path):
        raise PermissionError(13, "SECRET denied", "/SECRET/private/대표자")

    monkeypatch.setattr(Path, "read_bytes", denied)
    refused(call, rule, location)


def test_embedded_nul_in_root_refuses_without_echoing_the_path():
    refused(lambda: G.policy_descriptor("/SECRET/private\0"),
            "d1_digest_unreadable", "policy_spec")


@pytest.mark.parametrize("module, field", [(html.parser, "html_parser_sha256"),
                                           (_markupbase, "markupbase_sha256")])
@pytest.mark.parametrize("value", [None, 7, b"/SECRET/private", "", "/SECRET/private\0"])
def test_invalid_runtime_module_paths_refuse(monkeypatch, module, field, value):
    monkeypatch.setattr(module, "__file__", value)
    refused(G.runtime_descriptor, "d1_runtime_unavailable", f"runtime.{field}")


@pytest.mark.parametrize("module, field", [(html.parser, "html_parser_sha256"),
                                           (_markupbase, "markupbase_sha256")])
def test_runtime_module_bytes_are_read_again_on_each_call(monkeypatch, module, field):
    original_read = Path.read_bytes
    target = Path(module.__file__)
    content = b"first synthetic module bytes\n"

    def read(path):
        return content if path == target else original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    first = G.runtime_descriptor()
    assert first[field] == hashlib.sha256(content).hexdigest()
    content = b"changed synthetic module bytes\n"
    second = G.runtime_descriptor()
    assert second[field] == hashlib.sha256(content).hexdigest() != first[field]
    assert {key: value for key, value in first.items() if key != field} == {
        key: value for key, value in second.items() if key != field}


@pytest.mark.parametrize("attribute", ["python_implementation", "python_version"])
def test_version_getter_failures_are_sanitized_as_required_by_a2(monkeypatch, attribute):
    def unavailable():
        raise RuntimeError("/SECRET/private/代表者")

    monkeypatch.setattr(platform, attribute, unavailable)
    refused(G.runtime_descriptor, "d1_runtime_unavailable", f"runtime.{attribute}")


def test_version_attribute_lookup_failures_are_also_sanitized(monkeypatch):
    def unavailable(attribute):
        raise RuntimeError("/SECRET/private/代表者")

    monkeypatch.delattr(bs4, "__version__")
    monkeypatch.setattr(bs4, "__getattr__", unavailable, raising=False)
    refused(G.runtime_descriptor, "d1_runtime_unavailable", "runtime.bs4_version")


@pytest.mark.parametrize("exception_type", [AssertionError, RuntimeError])
@pytest.mark.parametrize("boundary", ["policy_read", "runtime_read", "json", "policy_hash", "runtime_hash"])
def test_unexpected_implementation_errors_are_not_capture_refusals(monkeypatch, exception_type, boundary):
    error = exception_type("synthetic programming failure")

    def broken(*args, **kwargs):
        raise error

    if boundary in ("policy_read", "runtime_read"):
        monkeypatch.setattr(Path, "read_bytes", broken)
        call = G.policy_descriptor if boundary == "policy_read" else G.runtime_descriptor
    elif boundary == "json":
        monkeypatch.setattr(json, "dumps", broken)
        call = lambda: G.canonical_json({})
    else:
        monkeypatch.setattr(hashlib, "sha256", broken)
        call = (lambda: G.policy_digest(valid_descriptor())) if boundary == "policy_hash" else G.runtime_descriptor
    with pytest.raises(exception_type) as caught:
        call()
    assert caught.value is error


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_process_control_exceptions_escape_version_collection(monkeypatch, exception_type):
    error = exception_type()

    def interrupted():
        raise error

    monkeypatch.setattr(platform, "python_version", interrupted)
    with pytest.raises(exception_type) as caught:
        G.runtime_descriptor()
    assert caught.value is error
