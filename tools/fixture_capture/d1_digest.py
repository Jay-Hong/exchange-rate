"""D1 slice 4B policy digest and review-environment descriptor.

Specification: tools/fixture_capture/d1_spec/d1_detection_policy_v1.txt §7.3.
Amendment: tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment1.txt
§A2–A3 separates the runtime record from the policy digest.
Amendment: tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment2.txt
§A4 defines which reviewed empty unsupported elements cleanup may remove.

이 모듈은 라이브러리이며 아무 경로도 부르지 않는다(배선은 슬라이스 5)
"""

import _markupbase
import hashlib
import html.parser
import json
import platform
import re
import unicodedata
from pathlib import Path

import bs4
import soupsieve

from .errors import CaptureError

SPEC_PATH = "tools/fixture_capture/d1_spec/d1_detection_policy_v1.txt"
AMENDMENT_PATHS = (
    "tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment1.txt",
    "tools/fixture_capture/d1_spec/d1_detection_policy_v1_amendment2.txt",
)
IMPLEMENTATION_FILES = (
    "tools/fixture_capture/__init__.py",
    "tools/fixture_capture/errors.py",
    "tools/fixture_capture/d1_policy.py",
    "tools/fixture_capture/d1_observe.py",
    "tools/fixture_capture/d1_findings.py",
    "tools/fixture_capture/d1_replace.py",
    "tools/fixture_capture/d1_digest.py",
    "tools/fixture_capture/queries.py",
    "tools/fixture_capture/limits.py",
    "tools/fixture_capture/detector.py",
    "tools/fixture_capture/d1_approval.py",
    "tools/fixture_capture/admission.py",
)

_CHECKOUT = Path(__file__).resolve().parents[2]
_POLICY_KEYS = frozenset(("policy_spec_sha256", "policy_amendments", "implementation_files"))
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _file_digest(path, rule, location):
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        # ValueError covers malformed paths, including embedded NUL characters.
        raise CaptureError(rule, location) from None
    return hashlib.sha256(data).hexdigest()


def policy_descriptor(root=None) -> dict:
    """Read the current policy bytes, without normalization or result caching."""
    root = _CHECKOUT if root is None else Path(root)
    rule = "d1_digest_unreadable"
    return {
        "policy_spec_sha256": _file_digest(root / SPEC_PATH, rule, "policy_spec"),
        "policy_amendments": [
            _file_digest(root / path, rule, f"policy_amendments[{index}]")
            for index, path in enumerate(AMENDMENT_PATHS)
        ],
        "implementation_files": {
            path: _file_digest(root / path, rule, f"implementation_files[{index}]")
            for index, path in enumerate(IMPLEMENTATION_FILES)
        },
    }


def canonical_json(value) -> bytes:
    """Serialize deterministically; report only a code-owned diagnostic."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        # Includes non-JSON values, cycles, non-finite numbers and invalid UTF-8.
        raise CaptureError("d1_descriptor_invalid", "canonical_json") from None


def _validate_sha256(value, location):
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise CaptureError("d1_descriptor_invalid", location) from None


def policy_digest(descriptor) -> str:
    """Validate the closed policy schema, then hash its canonical JSON bytes.

    Manifest membership is fixed by policy_descriptor, not by this serializer:
    callers may digest other policy manifests with the same schema.
    """
    if type(descriptor) is not dict or descriptor.keys() != _POLICY_KEYS:
        raise CaptureError("d1_descriptor_invalid", "policy_descriptor") from None

    _validate_sha256(descriptor["policy_spec_sha256"], "policy_spec_sha256")
    amendments = descriptor["policy_amendments"]
    if type(amendments) is not list:
        raise CaptureError("d1_descriptor_invalid", "policy_amendments") from None
    for index, value in enumerate(amendments):
        _validate_sha256(value, f"policy_amendments[{index}]")

    files = descriptor["implementation_files"]
    if type(files) is not dict:
        raise CaptureError("d1_descriptor_invalid", "implementation_files") from None
    for index, (path, value) in enumerate(files.items()):
        location = f"implementation_files[{index}]"
        if type(path) is not str:
            raise CaptureError("d1_descriptor_invalid", location) from None
        _validate_sha256(value, location)

    return hashlib.sha256(canonical_json(descriptor)).hexdigest()


def _runtime_version(owner, attribute, field, *, call=False):
    location = f"runtime.{field}"
    try:
        value = getattr(owner, attribute)
        if call:
            value = value()
    except Exception:
        # A2 explicitly treats any failure to obtain a version as unavailable.
        # Keep this boundary around retrieval only, not the whole descriptor.
        raise CaptureError("d1_runtime_unavailable", location) from None
    if type(value) is not str or not value:
        raise CaptureError("d1_runtime_unavailable", location) from None
    return value


def _runtime_module_digest(module, field):
    location = f"runtime.{field}"
    try:
        path = Path(module.__file__)
    except (AttributeError, TypeError, ValueError):
        raise CaptureError("d1_runtime_unavailable", location) from None
    return _file_digest(path, "d1_runtime_unavailable", location)


def runtime_descriptor() -> dict:
    """Describe the current review environment; never include module paths."""
    return {
        "python_implementation": _runtime_version(
            platform, "python_implementation", "python_implementation", call=True),
        "python_version": _runtime_version(
            platform, "python_version", "python_version", call=True),
        "unicode_version": _runtime_version(unicodedata, "unidata_version", "unicode_version"),
        "bs4_version": _runtime_version(bs4, "__version__", "bs4_version"),
        "soupsieve_version": _runtime_version(soupsieve, "__version__", "soupsieve_version"),
        "parser": "html.parser",
        "html_parser_sha256": _runtime_module_digest(html.parser, "html_parser_sha256"),
        "markupbase_sha256": _runtime_module_digest(_markupbase, "markupbase_sha256"),
    }
