"""독립 의미 리뷰 journal이 정확한 산출물과 닫힌 finding에 묶였는지 검사한다.

이 테스트는 자연어 판정이 참임을 증명하지 않는다. 대신 reviewer가 실제로 본 입력·출력·원장과
journal이 갈라지는 것을 fail-closed로 막고, 열린 finding을 pass로 기록하지 못하게 한다.
"""
import hashlib
import importlib.util
import json
import pathlib


REPO = pathlib.Path(__file__).resolve().parent.parent
REVIEW = REPO / "spec" / "topic-only-semantic-review.json"
LEDGER = REPO / "spec" / "topic-only-code-claim-review.json"
CITATION_TEST = REPO / "tests" / "test_document_citations.py"
SCHEMA_VERSION = 1
REQUIRED_SCOPE_FLAGS = {
    "candidate_outside_reverse_audit",
    "archive_normative_preservation_review",
    "classification_truth_review",
    "citation_support_review",
}


def _citation_module():
    spec = importlib.util.spec_from_file_location("topic_only_citations", CITATION_TEST)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _expected_scope(module) -> dict:
    manifest = json.loads(module.MANIFEST.read_text())
    requirements = sum(
        1
        for block in manifest["blocks"]
        for requirement in block.get("requirements", [])
        if requirement["destination"] in module.DOCS
    )
    entries = json.loads(LEDGER.read_text())["entries"]
    return {
        "manifest_output_requirements": requirements,
        "claim_candidates": len(module._candidates()),
        "code_fact_entries": sum(e["classification"] == "code_fact" for e in entries),
        "normative_entries": sum(e["classification"] == "normative" for e in entries),
    }


def review_errors(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["review journal top level must be an object"]

    module = _citation_module()
    errors = []
    expected_fields = {
        "schema_version",
        "reviewer",
        "reviewed_at",
        "inputs",
        "documents",
        "claim_ledger_sha256",
        "scope",
        "findings",
        "residual_limits",
        "verdict",
    }
    if set(data) != expected_fields:
        errors.append("top-level fields differ from the review schema")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    if data.get("inputs") != module._expected_ledger_inputs():
        errors.append("frozen input hashes or pinned commits changed")
    if data.get("documents") != module._expected_ledger_documents():
        errors.append("reviewed ADR/spec document hashes changed")
    if data.get("claim_ledger_sha256") != hashlib.sha256(LEDGER.read_bytes()).hexdigest():
        errors.append("reviewed claim ledger hash changed")

    reviewer = data.get("reviewer")
    if not isinstance(reviewer, dict):
        errors.append("reviewer must be an object")
    else:
        if reviewer.get("role") != "independent semantic reviewer":
            errors.append("reviewer role is not independent semantic reviewer")
        independent_from = reviewer.get("independent_from")
        if not isinstance(independent_from, list) or len(independent_from) < 2:
            errors.append("reviewer independence boundary is missing")

    scope = data.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope must be an object")
    else:
        for key, expected in _expected_scope(module).items():
            if scope.get(key) != expected:
                errors.append(f"scope {key}={scope.get(key)!r}, expected {expected}")
        for key in REQUIRED_SCOPE_FLAGS:
            if scope.get(key) is not True:
                errors.append(f"scope audit flag is not complete: {key}")

    findings = data.get("findings")
    if not isinstance(findings, list) or not findings:
        errors.append("findings must be a non-empty list")
    else:
        ids = [finding.get("id") for finding in findings if isinstance(finding, dict)]
        if len(ids) != len(findings) or len(set(ids)) != len(ids):
            errors.append("finding ids are missing or duplicated")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if finding.get("status") not in {"fixed", "rebutted"}:
                errors.append(f"finding is not closed: {finding.get('id')}")
            for key in ("severity", "locations", "issue", "disposition"):
                if not finding.get(key):
                    errors.append(f"finding {finding.get('id')} lacks {key}")

    if data.get("verdict") != "pass":
        errors.append("review verdict is not pass")
    return errors


def _review() -> dict:
    return json.loads(REVIEW.read_text())


def test_semantic_review_is_bound_to_current_outputs_and_closed_findings():
    assert not review_errors(_review())


def test_review_validator_rejects_stale_and_open_controls():
    base = _review()
    stale_doc = dict(base, documents=dict(base["documents"], ADR="0" * 64))
    stale_ledger = dict(base, claim_ledger_sha256="0" * 64)
    open_findings = [dict(base["findings"][0], status="open"), *base["findings"][1:]]
    open_review = dict(base, findings=open_findings)
    incomplete_scope = dict(
        base,
        scope=dict(base["scope"], candidate_outside_reverse_audit=False),
    )
    assert review_errors(stale_doc)
    assert review_errors(stale_ledger)
    assert review_errors(open_review)
    assert review_errors(incomplete_scope)
