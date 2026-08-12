"""의미 교차검토 journal이 정확한 산출물과 닫힌 finding에 묶였는지 검사한다.

이 테스트는 자연어 판정이나 저자·검토자 진술이 참임을 증명하지 않는다. 대신 검토한 입력·출력·원장과
journal이 갈라지는 것을 fail-closed로 막고, 자기검토 edge나 열린 finding을 pass로 기록하지 못하게 한다.
"""
import hashlib
import importlib.util
import json
import pathlib


REPO = pathlib.Path(__file__).resolve().parent.parent
REVIEW = REPO / "spec" / "topic-only-semantic-review.json"
LEDGER = REPO / "spec" / "topic-only-code-claim-review.json"
CITATION_TEST = REPO / "tests" / "test_document_citations.py"
SCHEMA_VERSION = 2
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
        "review_process",
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

    process = data.get("review_process")
    if not isinstance(process, dict):
        errors.append("review_process must be an object")
    else:
        process_fields = {
            "mode",
            "globally_independent_reviewer",
            "participants",
            "review_edges",
            "limitations",
        }
        if set(process) != process_fields:
            errors.append("review_process fields differ from the schema")
        if process.get("mode") != "reciprocal_cross_review":
            errors.append("review mode is not reciprocal_cross_review")
        if process.get("globally_independent_reviewer") is not False:
            errors.append("this review must not claim a globally independent reviewer")

        participants = process.get("participants")
        names = set()
        participants_by_name = {}
        if not isinstance(participants, list) or len(participants) < 2:
            errors.append("at least two review participants are required")
        else:
            for index, participant in enumerate(participants):
                if not isinstance(participant, dict):
                    errors.append(f"participant #{index} must be an object")
                    continue
                expected = {
                    "name", "roles", "authored_or_modified", "independently_reviewed"
                }
                if set(participant) != expected:
                    errors.append(f"participant #{index} fields differ from the schema")
                name = participant.get("name")
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"participant #{index} name is missing")
                elif name in names:
                    errors.append(f"duplicate participant: {name}")
                else:
                    names.add(name)
                    participants_by_name[name] = participant
                for field in ("roles", "authored_or_modified", "independently_reviewed"):
                    values = participant.get(field)
                    if (
                        not isinstance(values, list)
                        or not values
                        or any(not isinstance(value, str) or not value.strip() for value in values)
                    ):
                        errors.append(f"participant #{index} {field} is incomplete")

        edges = process.get("review_edges")
        authors, reviewers = set(), set()
        authored_scopes = {name: set() for name in names}
        reviewed_scopes = {name: set() for name in names}
        claimed_scopes = set()
        if not isinstance(edges, list) or len(edges) < 2:
            errors.append("at least two reciprocal review edges are required")
        else:
            for index, edge in enumerate(edges):
                if not isinstance(edge, dict):
                    errors.append(f"review edge #{index} must be an object")
                    continue
                if set(edge) != {"author", "reviewer", "scope", "result"}:
                    errors.append(f"review edge #{index} fields differ from the schema")
                author, reviewer = edge.get("author"), edge.get("reviewer")
                author_valid = isinstance(author, str) and bool(author.strip())
                reviewer_valid = isinstance(reviewer, str) and bool(reviewer.strip())
                if not author_valid or not reviewer_valid:
                    errors.append(f"review edge #{index} author/reviewer must be strings")
                elif author not in names or reviewer not in names:
                    errors.append(f"review edge #{index} names an unknown participant")
                if author_valid and reviewer_valid and author == reviewer:
                    errors.append(f"review edge #{index} is self-review")
                if author_valid:
                    authors.add(author)
                if reviewer_valid:
                    reviewers.add(reviewer)
                scope = edge.get("scope")
                if (
                    not isinstance(scope, list)
                    or not scope
                    or any(not isinstance(value, str) or not value.strip() for value in scope)
                ):
                    errors.append(f"review edge #{index} scope is incomplete")
                else:
                    if len(set(scope)) != len(scope):
                        errors.append(f"review edge #{index} repeats a scope")
                    duplicate_scopes = claimed_scopes & set(scope)
                    if duplicate_scopes:
                        errors.append(
                            f"review edge #{index} reuses scopes already assigned to another "
                            f"edge: {sorted(duplicate_scopes)}"
                        )
                    claimed_scopes.update(scope)
                    if author_valid and author in authored_scopes:
                        authored_scopes[author].update(scope)
                    if reviewer_valid and reviewer in reviewed_scopes:
                        reviewed_scopes[reviewer].update(scope)
                if edge.get("result") != "pass":
                    errors.append(f"review edge #{index} did not pass")
        if names and (authors != names or reviewers != names):
            errors.append("every participant must appear as both author and reviewer")

        # `review_edges.scope` 가 작업 범위의 단일 정본이다. 참가자별 산문 목록을 따로
        # 신뢰하면 두 표현이 갈려도 통과한다(실측: 무관한 scope와 재문구 자기검토가 통과).
        # 저자·검토자 배정의 진실성 자체는 attestation 이지만, journal 내부 표현은 정확히
        # 일치해야 한다.
        for name, participant in participants_by_name.items():
            authored = participant.get("authored_or_modified")
            reviewed = participant.get("independently_reviewed")
            if isinstance(authored, list) and all(isinstance(v, str) for v in authored):
                if len(set(authored)) != len(authored):
                    errors.append(f"participant {name} repeats an authored scope")
                if set(authored) != authored_scopes.get(name, set()):
                    errors.append(
                        f"participant {name} authored scopes differ from review edges"
                    )
            if isinstance(reviewed, list) and all(isinstance(v, str) for v in reviewed):
                if len(set(reviewed)) != len(reviewed):
                    errors.append(f"participant {name} repeats a reviewed scope")
                if set(reviewed) != reviewed_scopes.get(name, set()):
                    errors.append(
                        f"participant {name} reviewed scopes differ from review edges"
                    )

        limitations = process.get("limitations")
        if (
            not isinstance(limitations, list)
            or not limitations
            or any(not isinstance(value, str) or not value.strip() for value in limitations)
        ):
            errors.append("review_process limitations are missing")

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
    self_review = dict(base["review_process"])
    self_review["review_edges"] = [
        dict(self_review["review_edges"][0], reviewer=self_review["review_edges"][0]["author"]),
        *self_review["review_edges"][1:],
    ]
    self_review = dict(base, review_process=self_review)
    false_independence = dict(
        base,
        review_process=dict(
            base["review_process"], globally_independent_reviewer=True
        ),
    )
    one_way = dict(base["review_process"])
    one_way["review_edges"] = [one_way["review_edges"][0]]
    one_way = dict(base, review_process=one_way)
    malformed_edge = dict(base["review_process"])
    malformed_edge["review_edges"] = [
        dict(malformed_edge["review_edges"][0], author={"not": "a string"}),
        *malformed_edge["review_edges"][1:],
    ]
    malformed_edge = dict(base, review_process=malformed_edge)
    assert review_errors(stale_doc)
    assert review_errors(stale_ledger)
    assert review_errors(open_review)
    assert review_errors(incomplete_scope)
    assert review_errors(self_review)

    # ⛔ 참가자 목록과 edge 범위가 갈리는 형태. 완전히 같은 문자열뿐 아니라 재문구한
    #    자기검토나 무관한 scope도 edge 단일 정본과 불일치하므로 거부해야 한다.
    prose_self = dict(base["review_process"])
    prose_self["participants"] = [
        dict(prose_self["participants"][0],
             independently_reviewed=[
                 prose_self["participants"][0]["authored_or_modified"][0] + " (reviewed)"
             ]),
        *prose_self["participants"][1:],
    ]
    assert review_errors(dict(base, review_process=prose_self))
    scope_disagreement = dict(base["review_process"])
    scope_disagreement["participants"] = [
        dict(scope_disagreement["participants"][0],
             independently_reviewed=["unrelated scope"]),
        *scope_disagreement["participants"][1:],
    ]
    assert review_errors(dict(base, review_process=scope_disagreement))
    reused_scope = dict(base["review_process"])
    reused_scope["review_edges"] = [
        reused_scope["review_edges"][0],
        dict(
            reused_scope["review_edges"][1],
            scope=[reused_scope["review_edges"][0]["scope"][0]],
        ),
    ]
    assert review_errors(dict(base, review_process=reused_scope))
    assert review_errors(false_independence)
    assert review_errors(one_way)
    assert review_errors(malformed_edge)
