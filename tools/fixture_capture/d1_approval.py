"""D1 approval schema, byte bindings and exact recomputed finding comparison.

The caller supplies layer-A-validated evidence and strictly parsed JSON. Only
NO_APPROVAL denotes an absent file; JSON null is an invalid approval object.
Review runtime is recorded for provenance, not compared with today's runtime.
"""

import hashlib
import re
from datetime import datetime
from uuid import UUID

from . import d1_digest
from .d1_findings import findings
from .errors import CaptureError

SCHEMA_VERSION = 1
DECISIONS = ("name_removed", "not_person_name_context")
NO_APPROVAL = object()

_KEYS = frozenset(("schema_version", "route", "capture_id", "fixture_sha256",
                   "metadata_sha256", "policy_digest", "runtime", "items",
                   "reviewer", "reviewed_at"))
_RUNTIME_VERSIONS = ("python_implementation", "python_version", "unicode_version",
                     "bs4_version", "soupsieve_version")
_RUNTIME_HASHES = ("html_parser_sha256", "markupbase_sha256")
_RUNTIME_KEYS = frozenset((*_RUNTIME_VERSIONS, "parser", *_RUNTIME_HASHES))


def _require(condition, location):
    if not condition:
        raise CaptureError("d1_approval_schema", location) from None


def _matches(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _sha256(value, location):
    _require(_matches(value, r"[0-9a-f]{64}"), location)


def _validate_runtime(runtime):
    location = "approval.runtime"
    _require(type(runtime) is dict and runtime.keys() == _RUNTIME_KEYS, location)
    for key in _RUNTIME_VERSIONS:
        _require(type(runtime[key]) is str and bool(runtime[key]), location + "." + key)
    _require(runtime["parser"] == "html.parser", location + ".parser")
    for key in _RUNTIME_HASHES:
        _sha256(runtime[key], location + "." + key)


def _validate_schema(approval):
    _require(type(approval) is dict and approval.keys() == _KEYS, "approval")
    _require(type(approval["schema_version"]) is int
             and approval["schema_version"] == SCHEMA_VERSION, "approval.schema_version")
    _require(type(approval["route"]) is str and bool(approval["route"]), "approval.route")
    _require(type(approval["capture_id"]) is str, "approval.capture_id")
    try:
        capture_id = UUID(approval["capture_id"])
    except ValueError:
        raise CaptureError("d1_approval_schema", "approval.capture_id") from None
    _require(capture_id.version == 4 and str(capture_id) == approval["capture_id"],
             "approval.capture_id")
    for key in ("fixture_sha256", "metadata_sha256", "policy_digest"):
        _sha256(approval[key], "approval." + key)
    _validate_runtime(approval["runtime"])
    _require(_matches(approval["reviewer"], r"[A-Za-z0-9_.-]{1,64}"), "approval.reviewer")
    _require(type(approval["reviewed_at"]) is str, "approval.reviewed_at")
    try:
        timestamp = datetime.strptime(approval["reviewed_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise CaptureError("d1_approval_schema", "approval.reviewed_at") from None
    _require(timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") == approval["reviewed_at"],
             "approval.reviewed_at")
    _require(type(approval["items"]) is list, "approval.items")
    for index, item in enumerate(approval["items"]):
        location = f"approval.items[{index}]"
        _require(type(item) is dict and item.keys() == {"finding", "decision"}, location)
        _require(type(item["finding"]) is dict, location + ".finding")
        _require(type(item["decision"]) is str and item["decision"] in DECISIONS,
                 location + ".decision")


def check(evidence, approval):
    """Check a parsed approval against validated evidence; return new findings.

All four decision-table cases obtain today's policy and runtime and recompute
findings. An unavailable input or failed inspection must propagate as refusal.
"""
    policy = d1_digest.policy_digest(d1_digest.policy_descriptor())
    d1_digest.runtime_descriptor()
    current = findings(evidence.soup(), evidence.metadata)
    if approval is NO_APPROVAL:
        if current:
            raise CaptureError("d1_approval_missing", "approval")
        return current

    _validate_schema(approval)
    bindings = {
        "route": evidence.route,
        "capture_id": evidence.metadata["capture_id"],
        "fixture_sha256": hashlib.sha256(evidence.fixture).hexdigest(),
        "metadata_sha256": hashlib.sha256(evidence.metadata_bytes).hexdigest(),
        "policy_digest": policy,
    }
    for key, value in bindings.items():
        if approval[key] != value:
            raise CaptureError("d1_approval_binding", "approval." + key)

    # Canonical bytes distinguish bool/int/float and retain list order. The
    # recomputed closed variants also reject extra/missing fields in a finding.
    recorded = [item["finding"] for item in approval["items"]]
    try:
        recorded_bytes = d1_digest.canonical_json(recorded)
    except CaptureError:
        raise CaptureError("d1_approval_items", "approval.items") from None
    if recorded_bytes != d1_digest.canonical_json(current):
        raise CaptureError("d1_approval_items", "approval.items")
    return current
