"""의미 교차검토 journal이 정확한 산출물과 닫힌 finding에 묶였는지 검사한다.

이 테스트는 자연어 판정이나 저자·검토자 진술이 참임을 증명하지 않는다. 대신 검토한 입력·출력·원장과
journal이 갈라지는 것을 fail-closed로 막고, 자기검토 edge나 열린 finding을 pass로 기록하지 못하게 한다.
"""
import hashlib
import importlib.util
import json
import pathlib
import re


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

# `status=fixed` / `verdict=pass` 로 닫고도 disposition 또는 residual limit 에
# 이전 라운드의 보류 문구를 남긴 실수가 두 번 있었다(SEM-020/021). 자연어 전체의
# 수명 상태를 증명하지는 못하지만, 실제로 재발한 표현 부류는 pass 와 양립시키지 않는다.
PENDING_REVIEW_PATTERNS = (
    re.compile(r"\bneeds_cross_review\b", re.I),
    re.compile(r"\bnot (?:yet )?been independently reviewed\b", re.I),
    re.compile(r"\bnot covered by a pass verdict until\b", re.I),
    re.compile(r"\bkeep(?:s)? (?:this )?finding open\b", re.I),
    re.compile(r"\bsubject to the open cross-review\b", re.I),
    re.compile(
        r"\b(?:awaits?|awaiting|pending)\b.{0,80}"
        r"\b(?:cross[- ]review|independent review)\b",
        re.I,
    ),
    re.compile(r"\bclosing (?:this )?journal cannot be cross-reviewed\b", re.I),
    re.compile(r"\bbookkeeping\b.{0,120}\bself-attested\b", re.I),
    # ⛔ pass 인데 "다음 pass 가 확인한다" 는 스스로를 미완으로 선언하는 것이다.
    #    내 전-필드 스캔이 이걸 놓쳤다 — 필드는 다 훑었는데 **패턴 집합**이 좁았다.
    re.compile(r"\ba future pass\b", re.I),
)


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

    # `issue` 는 과거 결함을 인용하므로 검사하지 않는다. 현재 처분과 잔여 한계만
    # 검사해야, 역사 기록의 "needs_cross_review" 를 지우지 않으면서 pass 상태의
    # 수명 모순은 막을 수 있다.
    if data.get("verdict") == "pass":
        lifecycle_surfaces = []
        if isinstance(findings, list):
            lifecycle_surfaces.extend(
                (f"finding {finding.get('id')} disposition", finding.get("disposition"))
                for finding in findings
                if isinstance(finding, dict)
            )
        residual_limits = data.get("residual_limits")
        if isinstance(residual_limits, list):
            lifecycle_surfaces.extend(
                (f"residual limit #{index}", value)
                for index, value in enumerate(residual_limits)
            )
        for label, value in lifecycle_surfaces:
            if not isinstance(value, str):
                continue
            for pattern in PENDING_REVIEW_PATTERNS:
                if pattern.search(value):
                    errors.append(
                        f"pass review retains pending-review language in {label}: "
                        f"{pattern.pattern!r}"
                    )
                    break

    if data.get("verdict") != "pass":
        errors.append("review verdict is not pass")
    return errors


def _review() -> dict:
    return json.loads(REVIEW.read_text())


def _synthetic_open_journal(data: dict) -> dict:
    """검증 대상과 무관하게 **열린** journal 을 만든다.

    ⛔ 반례 base 를 살아 있는 journal 에서 바로 만들면, 그 journal 이 이미 pass 인 순간
       `_closed_validator_fixture` 가 무용해져 helper 제거 변이가 통과한다(실측: 최종 후보
       tree 에서 SURVIVED). base 를 먼저 열어 두면 helper 는 상태와 무관하게 하중을 받는다.
    """
    findings = [dict(data["findings"][0], status="open"), *data["findings"][1:]]
    return dict(data, findings=findings, verdict="needs_cross_review")


def _closed_validator_fixture(data: dict) -> dict:
    """open journal 상태와 무관한 **유효한 양성 대조군**을 만든다.

    실제 journal이 교차검토 대기 중이면 `review_errors(_review())` 자체가 이미 red다. 그 값을
    반례의 base로 쓰면 어떤 변이도 기존 오류에 기대어 통과하는 공허한 테스트가 된다. open
    finding의 처분만 합성 완료 문구로 바꾸며, 해시·scope·edge 등 검증 대상은 그대로 둔다.
    """
    findings = []
    for finding in data["findings"]:
        if finding.get("status") in {"fixed", "rebutted"}:
            findings.append(dict(finding))
        else:
            findings.append(
                dict(
                    finding,
                    status="fixed",
                    disposition="Synthetic validator fixture: reciprocal review completed.",
                )
            )
    return dict(data, findings=findings, verdict="pass")


def test_semantic_review_is_bound_to_current_outputs_and_closed_findings():
    assert not review_errors(_review())


def test_synthetic_open_base_actually_opens_the_journal():
    """⛔ **scaffold 자체를 잠근다.** `_synthetic_open_journal` 이 no-op 이 되면 반례 base 가 다시
    살아 있는 journal 이 되고, 그 journal 이 pass 인 동안 `_closed_validator_fixture` 도 무용해져
    helper 제거 변이가 무증상으로 통과한다(실측). 합성 open 이 **실제로 오류를 낳는지** 직접
    확인해야 그 되돌림이 red 가 된다.
    """
    assert review_errors(_synthetic_open_journal(_review())), (
        "합성 open journal 이 오류를 내지 않는다 — 반례의 양성 대조군이 공허해진다")


def _replace(data: dict, key: str, value):
    """최상위 한 필드만 바꾼 사본."""
    return dict(data, **{key: value})


def _process(data: dict, **changes):
    """`review_process` 안의 필드만 바꾼 사본."""
    return dict(data, review_process=dict(data["review_process"], **changes))


def _edges(data: dict, mutate):
    """첫 edge 를 `mutate` 로 바꾼 사본. 나머지 edge 는 그대로 둔다."""
    edges = data["review_process"]["review_edges"]
    return _process(data, review_edges=[mutate(edges[0]), *edges[1:]])


def _participants(data: dict, mutate):
    """첫 participant 를 `mutate` 로 바꾼 사본."""
    people = data["review_process"]["participants"]
    return _process(data, participants=[mutate(people[0]), *people[1:]])


def _targeted_branch_controls(base: dict):
    """`review_errors` 의 append 지점별 반례 — (라벨, 조작된 journal, 기대 진단).

    ⛔ **각 반례가 그 분기만 단독으로 트리거할 필요는 없다.** 여러 진단이 함께 나와도
       목표 문자열을 직접 단언하면 그 분기는 하중을 받는다. 단독 fixture 를 요구하면
       작업만 커지고 얻는 것이 없다.
    ⚠️ 기대 문자열은 **구별 가능해야** 한다 — `participant #0 must be an object` 와
       `review edge #0 must be an object` 는 접두사를 포함해야 서로 안 섞인다.
    """
    people = base["review_process"]["participants"]
    edges = base["review_process"]["review_edges"]
    first_name = people[0]["name"]
    first_scope = edges[0]["scope"]
    return [
        # ── 최상위 ──
        ("최상위 필드 추가", _replace(base, "unexpected_field", 1),
         "top-level fields differ from the review schema"),
        ("schema_version 변경", _replace(base, "schema_version", 999),
         "schema_version mismatch"),
        ("frozen input 변경",
         _replace(base, "inputs", dict(base["inputs"], server_commit="0" * 40)),
         "frozen input hashes or pinned commits changed"),
        ("review_process 비-object", _replace(base, "review_process", "not-an-object"),
         "review_process must be an object"),
        ("scope 비-object", _replace(base, "scope", "not-an-object"),
         "scope must be an object"),
        ("findings 빈 목록", _replace(base, "findings", []),
         "findings must be a non-empty list"),
        ("verdict 변경", _replace(base, "verdict", "fail"),
         "review verdict is not pass"),
        # ── review_process ──
        ("process 필드 추가", _process(base, unexpected=1),
         "review_process fields differ from the schema"),
        ("mode 변경", _process(base, mode="solo_review"),
         "review mode is not reciprocal_cross_review"),
        ("participant 1명", _process(base, participants=people[:1]),
         "at least two review participants are required"),
        ("edge 1개", _process(base, review_edges=edges[:1]),
         "at least two reciprocal review edges are required"),
        ("한 사람만 저자",
         _process(base, review_edges=[dict(edge, author=first_name) for edge in edges]),
         "every participant must appear as both author and reviewer"),
        ("limitations 빈 목록", _process(base, limitations=[]),
         "review_process limitations are missing"),
        # ── scope ──
        ("scope 값 불일치",
         _replace(base, "scope", dict(base["scope"], manifest_output_requirements=-1)),
         "scope manifest_output_requirements=-1"),
        # ── participants ──
        ("participant 비-object", _participants(base, lambda p: "not-an-object"),
         "participant #0 must be an object"),
        ("participant 필드 추가", _participants(base, lambda p: dict(p, extra=1)),
         "participant #0 fields differ from the schema"),
        ("participant name 공백", _participants(base, lambda p: dict(p, name="   ")),
         "participant #0 name is missing"),
        ("participant 이름 중복",
         _process(base, participants=[people[0], dict(people[1], name=first_name)]),
         f"duplicate participant: {first_name}"),
        ("participant roles 빈 목록", _participants(base, lambda p: dict(p, roles=[])),
         "participant #0 roles is incomplete"),
        ("authored 중복",
         _participants(base, lambda p: dict(p, authored_or_modified=[
             *p["authored_or_modified"], p["authored_or_modified"][0]])),
         f"participant {first_name} repeats an authored scope"),
        ("authored 가 edge 와 불일치",
         _participants(base, lambda p: dict(p, authored_or_modified=["무관한 범위"])),
         f"participant {first_name} authored scopes differ from review edges"),
        ("reviewed 중복",
         _participants(base, lambda p: dict(p, independently_reviewed=[
             *p["independently_reviewed"], p["independently_reviewed"][0]])),
         f"participant {first_name} repeats a reviewed scope"),
        # ── review edges ──
        ("edge 비-object", _edges(base, lambda e: "not-an-object"),
         "review edge #0 must be an object"),
        ("edge 필드 추가", _edges(base, lambda e: dict(e, extra=1)),
         "review edge #0 fields differ from the schema"),
        ("edge author 비-문자열", _edges(base, lambda e: dict(e, author={"not": "a string"})),
         "review edge #0 author/reviewer must be strings"),
        ("edge 가 모르는 참가자 지목", _edges(base, lambda e: dict(e, author="아무개")),
         "review edge #0 names an unknown participant"),
        ("edge scope 빈 목록", _edges(base, lambda e: dict(e, scope=[])),
         "review edge #0 scope is incomplete"),
        ("edge scope 중복",
         _edges(base, lambda e: dict(e, scope=[*e["scope"], e["scope"][0]])),
         "review edge #0 repeats a scope"),
        ("edge 가 다른 edge 의 scope 재사용",
         _process(base, review_edges=[edges[0],
                                      *[dict(edge, scope=list(first_scope)) for edge in edges[1:]]]),
         "reuses scopes already assigned to another edge"),
        ("edge result 변경", _edges(base, lambda e: dict(e, result="fail")),
         "review edge #0 did not pass"),
        # ── findings ──
        ("finding id 중복",
         _replace(base, "findings", [base["findings"][0], dict(base["findings"][1],
                                                               id=base["findings"][0]["id"])]),
         "finding ids are missing or duplicated"),
        ("finding severity 누락",
         _replace(base, "findings", [dict(base["findings"][0], severity=""),
                                     *base["findings"][1:]]),
         f"finding {base['findings'][0].get('id')} lacks severity"),
    ]


def test_every_validator_branch_is_named_by_its_own_control():
    """⛔ 반례가 "오류가 있다" 만 보면 **인접 진단이 규칙 삭제를 가린다**.

    실측(2026-08-13): `review_errors` 의 40개 `errors.append` 를 하나씩 중화했을 때
    비어 있지 않음만 보는 단언 아래에서 **32개가 무증상**이었다 — self-review 금지처럼
    프로토콜의 근간인 규칙도 그중 하나였다(SEM-026). 그래서 각 반례는 **자기 진단을
    직접 지목**한다.

    ⚠️ **완결 범위**: 이 검사가 잠그는 것은 `errors.append` **지점**이다. 한 지점이
    반복문 안에서 여러 participant 필드·scope flag·finding 필드를 처리하므로,
    40/40 을 달성해도 "각 변형이 모두 잠겼다" 는 뜻이 **아니다**.
    """
    base = _closed_validator_fixture(_synthetic_open_journal(_review()))
    assert not review_errors(base), "반례의 양성 대조군부터 유효해야 한다"

    controls = _targeted_branch_controls(base)
    assert len(controls) == 32, f"반례 표가 32개가 아니다: {len(controls)}"

    for label, mutated, expected in controls:
        errors = review_errors(mutated)
        assert any(expected in error for error in errors), (
            f"{label}: 기대 진단 {expected!r} 이(가) 없다 — 실제 {errors}"
        )


def test_review_validator_rejects_stale_and_open_controls():
    base = _closed_validator_fixture(_synthetic_open_journal(_review()))
    assert not review_errors(base), "반례의 양성 대조군부터 유효해야 한다"
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
    pending_disposition_findings = [
        dict(
            base["findings"][0],
            disposition=(
                "The correction has not been independently reviewed; "
                "keep this finding open and use needs_cross_review."
            ),
        ),
        *base["findings"][1:],
    ]
    pending_disposition = dict(base, findings=pending_disposition_findings)
    pending_residual = dict(
        base,
        residual_limits=[
            *base["residual_limits"],
            "SEM-X is not covered by a pass verdict until cross-review completes.",
        ],
    )
    self_attested_closure = dict(
        base,
        residual_limits=[
            *base["residual_limits"],
            "The bookkeeping that marks the journal closed is self-attested.",
        ],
    )
    assert review_errors(stale_doc)
    assert review_errors(stale_ledger)
    assert review_errors(open_review)
    assert review_errors(incomplete_scope)
    self_review_errors = review_errors(self_review)
    assert any("is self-review" in error for error in self_review_errors), (
        "self-review 전용 오류가 없다 — 다른 동반 오류가 규칙 제거를 숨길 수 있다"
    )

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
    assert review_errors(pending_disposition)
    assert review_errors(pending_residual)
    future_pass = dict(
        base,
        residual_limits=[
            *base["residual_limits"],
            "A future pass confirms the cited code path at the pinned commit.",
        ],
    )
    assert review_errors(self_attested_closure)
    assert review_errors(future_pass)
