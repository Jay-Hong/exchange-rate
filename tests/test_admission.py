"""Implementation-side admission checks beyond the read-only 5b-2 contract."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from tests._fixture_capture import citi_html, getter, no_network, official_html  # noqa: F401
from tools.fixture_capture import admission as A
from tools.fixture_capture import capture, d1_approval, d1_digest
from tools.fixture_capture.detector import scan_string
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.limits import HTML_LIMIT, METADATA_LIMIT
from tools.fixture_capture.registry import Registry


@lru_cache(maxsize=None)
def pair(route="bs_official"):
    get, _ = getter(citi_html() if route == "citi_primary" else official_html())
    with patch.object(capture, "source_identity", lambda registry: ("a" * 40, "c1b/1:" + "b" * 64)):
        artifact = capture.capture_route(route, registry=Registry(), get=get)
    return artifact.fixture, artifact.metadata


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def approval():
    fixture, metadata = pair()
    return {
        "schema_version": 1, "route": "bs_official", "capture_id": json.loads(metadata)["capture_id"],
        "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
        "metadata_sha256": hashlib.sha256(metadata).hexdigest(),
        "policy_digest": d1_digest.policy_digest(d1_digest.policy_descriptor()),
        "runtime": d1_digest.runtime_descriptor(), "items": [], "reviewer": "reviewer_1",
        "reviewed_at": "2026-09-22T00:00:00Z",
    }


def refused(call, rule, location):
    with pytest.raises(CaptureError) as caught:
        call()
    error = caught.value
    assert (error.rule, error.location) == (rule, location)
    assert "SECRET" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    return error


def test_all_current_code_field_constants_pass_the_a_scan():
    # C1d's remaining constant coverage, using only synthetic/current code data.
    registry = Registry()
    constants = (*sorted(registry.evidence_selectors),
                 *registry.sources.citi.CITI_BANK_SELECTORS,
                 *registry.sources.citi.CURRENCY_TEXTS,
                 *registry.sources.utils.MIBANK_RATE_CELL_SELECTORS)
    assert len(constants) == 31
    for value in sorted(constants):
        scan_string(value, "synthetic.current_code_constant")


@pytest.mark.parametrize("name", ["style", "data-x"])
def test_attribute_names_are_checked_even_for_safe_multivalued_attributes(name):
    soup = BeautifulSoup(f'<p {name}="safe"></p>', "html.parser",
                         multi_valued_attributes={"*": [name]})
    assert soup.p[name] == ["safe"]
    refused(lambda: A.validate_structure(soup), "field_contract", "fixture.attributes")


def test_bad_fixture_encoding_is_refused_after_its_hash_is_validated():
    fixture, metadata = pair()
    fixture += b"\xff"
    meta = json.loads(metadata)
    meta["fixture_sha256"] = hashlib.sha256(fixture).hexdigest()
    refused(lambda: A.validate_integrity(fixture, encode(meta), "bs_official"), "invalid_utf8", "fixture")


@pytest.mark.parametrize("filename,limit", [("fixture.html", HTML_LIMIT), ("metadata.json", METADATA_LIMIT)])
def test_load_evidence_checks_disk_sizes_before_loading(tmp_path, monkeypatch, filename, limit):
    directory = tmp_path / "bs_official"
    directory.mkdir()
    for name, body in zip(("fixture.html", "metadata.json"), pair()):
        (directory / name).write_bytes(body)
    (directory / filename).write_bytes(b" " * (limit + 1))

    def unread(*args):
        pytest.fail("an oversized pair must not be read")

    monkeypatch.setattr(Path, "read_bytes", unread)
    refused(lambda: A.load_evidence("bs_official", tmp_path), "field_contract",
            f"bs_official/{filename}.size")


def test_load_evidence_rejects_an_unknown_route_before_path_access(tmp_path, monkeypatch):
    def unread(*args):
        pytest.fail("an unvalidated route must not be used as a path")

    monkeypatch.setattr(Path, "is_file", unread)
    refused(lambda: A.load_evidence("../SECRET", tmp_path), "field_contract", "route")


@pytest.mark.parametrize("raw", [b'{"duplicate":0,"duplicate":1}', b" " * (METADATA_LIMIT + 1)])
def test_layer_a_finishes_before_approval_parsing_or_d1(raw, monkeypatch):
    fixture, metadata = pair()
    meta = json.loads(metadata)
    meta["origin"] = "server"
    parse, seen = A.load_json, []

    def only_metadata(body, location):
        seen.append(location)
        assert location == "metadata", "approval parsing ran before layer A finished"
        return parse(body, location)

    def no_d1(*args):
        pytest.fail("D1 ran before layer A finished")

    monkeypatch.setattr(A, "load_json", only_metadata)
    monkeypatch.setattr(d1_approval, "check", no_d1)
    refused(lambda: A.admit(fixture, encode(meta), "bs_official", raw), "field_contract", "metadata.origin")
    assert seen == ["metadata"]


def test_approval_size_accepts_the_boundary_and_refuses_one_more_byte(tmp_path):
    raw = encode(approval())
    raw += b" " * (METADATA_LIMIT - len(raw))
    path = tmp_path / "approval.json"
    path.write_bytes(raw)
    assert A.read_approval(path) == raw
    assert A.admit(*pair(), "bs_official", raw) == []
    path.write_bytes(raw + b" ")
    refused(lambda: A.read_approval(path), "d1_approval_schema", "approval.size")
    refused(lambda: A.admit(*pair(), "bs_official", raw + b" "), "d1_approval_schema", "approval.size")


def test_an_oversized_approval_is_not_parsed(monkeypatch):
    parse = A.load_json

    def only_metadata(body, location):
        assert location == "metadata"
        return parse(body, location)

    monkeypatch.setattr(A, "load_json", only_metadata)
    refused(lambda: A.admit(*pair(), "bs_official", b" " * (METADATA_LIMIT + 1)),
            "d1_approval_schema", "approval.size")


@pytest.mark.parametrize("field", ["html_parser_sha256", "markupbase_sha256"])
@pytest.mark.parametrize("value", [None, True, 123, [], {}, "a" * 64 + "\n"])
def test_invalid_runtime_hashes_report_the_specific_field(field, value):
    record = approval()
    record["runtime"][field] = value
    refused(lambda: A.admit(*pair(), "bs_official", encode(record)),
            "d1_approval_schema", "approval.runtime." + field)


@pytest.mark.parametrize("field", ["capture_id", "reviewed_at"])
def test_invalid_identifiers_do_not_chain_the_parser_error(field):
    record = approval()
    record[field] = "SECRET/private/reviewer"
    refused(lambda: A.admit(*pair(), "bs_official", encode(record)), "d1_approval_schema", "approval." + field)


@pytest.mark.parametrize("attribute", ["policy_descriptor", "policy_digest", "runtime_descriptor"])
def test_each_policy_and_runtime_callable_is_looked_up_on_each_check(attribute, monkeypatch):
    raw = encode(approval())
    assert A.admit(*pair(), "bs_official", raw) == []
    original, calls = getattr(d1_digest, attribute), []

    def spy(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(d1_digest, attribute, spy)
    assert A.admit(*pair(), "bs_official", None) == []
    assert A.admit(*pair(), "bs_official", raw) == []
    refused(lambda: A.admit(*pair("citi_primary"), "citi_primary", None), "d1_approval_missing", "approval")
    assert len(calls) == 3


def test_current_runtime_failure_precedes_missing_approval(monkeypatch):
    def unavailable():
        raise CaptureError("d1_runtime_unavailable", "runtime.python_version")

    monkeypatch.setattr(d1_digest, "runtime_descriptor", unavailable)
    refused(lambda: A.admit(*pair("citi_primary"), "citi_primary", None),
            "d1_runtime_unavailable", "runtime.python_version")


def test_direct_check_does_not_mutate_the_record_or_evidence():
    evidence = A.validate_integrity(*pair(), "bs_official")
    record = approval()
    before = copy.deepcopy((evidence, record))
    assert d1_approval.check(evidence, record) == []
    assert (evidence, record) == before
    # The absence sentinel must not make a directly supplied JSON null valid.
    refused(lambda: d1_approval.check(evidence, None), "d1_approval_schema", "approval")


def test_a_fifo_is_refused_without_opening_it(tmp_path):
    path = tmp_path / "approval.pipe"
    os.mkfifo(path)
    refused(lambda: A.read_approval(path), "d1_approval_unreadable", "approval")


def test_a_stat_permission_error_is_not_absence(tmp_path, monkeypatch):
    def denied(*args):
        raise PermissionError(13, "SECRET permission denied", "/SECRET/reviewer")

    monkeypatch.setattr(Path, "lstat", denied)
    refused(lambda: A.read_approval(tmp_path / "x.json"), "d1_approval_unreadable", "approval")


@pytest.mark.parametrize("change", ["disappeared", "grew"])
def test_a_file_changed_after_stat_still_refuses(tmp_path, monkeypatch, change):
    path = tmp_path / "approval.json"
    path.write_bytes(b"{}")

    def changed(*args):
        if change == "disappeared":
            raise FileNotFoundError(2, "SECRET missing", "/SECRET/reviewer")
        return b" " * (METADATA_LIMIT + 1)

    monkeypatch.setattr(Path, "read_bytes", changed)
    rule, location = (("d1_approval_unreadable", "approval") if change == "disappeared"
                      else ("d1_approval_schema", "approval.size"))
    refused(lambda: A.read_approval(path), rule, location)


def test_a_fresh_admission_import_does_not_load_the_app(tmp_path):
    probe = """
import sys
from tools.fixture_capture import admission, d1_approval
assert not any(name == 'app' or name.startswith('app.') for name in sys.modules)
assert not any('tools.fixture_capture.' + name in sys.modules
               for name in ('capture', 'registry', 'runtime', 'fetch', 'roundtrip'))
"""
    result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                                 "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout == "" and result.stderr == ""
