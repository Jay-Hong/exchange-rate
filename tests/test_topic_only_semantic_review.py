"""의미 교차검토 journal이 정확한 산출물과 닫힌 finding에 묶였는지 검사한다.

이 테스트는 자연어 판정이나 저자·검토자 진술이 참임을 증명하지 않는다. 대신 검토한 입력·출력·원장과
journal이 갈라지는 것을 fail-closed로 막고, 자기검토 edge나 열린 finding을 pass로 기록하지 못하게 한다.
"""
import copy
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
SEMANTIC_BINDING_PREFIX = "semantic-input-binding/v1:"
SEMANTIC_BINDING_GENESIS_SHA256 = (
    "7b648ec1a84bbbc7dbc5f2283bf9cbeb366430789ebe7c825719c449351f47f7"
)
# Current-state validation cannot distinguish an appended epoch from a rewritten latest epoch.
# Pin every reviewed signature here so replacement is a loud test-file change. Legitimate epochs
# append one tuple; existing tuples are never rewritten.
SEMANTIC_BINDING_HISTORY = (
    (1, "GENESIS", "7b648ec1a84bbbc7dbc5f2283bf9cbeb366430789ebe7c825719c449351f47f7"),
    (
        2,
        "7b648ec1a84bbbc7dbc5f2283bf9cbeb366430789ebe7c825719c449351f47f7",
        "577e81455eeff5ff5ade13b63aedfa189f4f96da86c56b1065eaa8ffd88b1a25",
    ),
    (
        3,
        "577e81455eeff5ff5ade13b63aedfa189f4f96da86c56b1065eaa8ffd88b1a25",
        "8b99492c726d0d64f9908edc787c8c0f5f47b56e222da8d12488ab05fae892d8",
    ),
    (
        4,
        "8b99492c726d0d64f9908edc787c8c0f5f47b56e222da8d12488ab05fae892d8",
        "a83e183c29c625ced90a150834bd3da82bdfafd58988f86df23b22e5dab3b54a",
    ),
    (
        5,
        "a83e183c29c625ced90a150834bd3da82bdfafd58988f86df23b22e5dab3b54a",
        "a5981a78d5331881f4cf4b051518d6b0f45cad224b38042348395659cbc41223",
    ),
    (
        6,
        "a5981a78d5331881f4cf4b051518d6b0f45cad224b38042348395659cbc41223",
        "8d5dddc8bb5832e71663131a915c2c54a32e707f859850d3dc9c36c4f2084fe4",
    ),
    (
        7,
        "8d5dddc8bb5832e71663131a915c2c54a32e707f859850d3dc9c36c4f2084fe4",
        "5340af978162539a03ac923f79bd6a626567ea0b7b0093c3d9b821431ff7eccf",
    ),
    (
        8,
        "5340af978162539a03ac923f79bd6a626567ea0b7b0093c3d9b821431ff7eccf",
        "727754ac90555b34d90270887333b21bb3f687500dada525652c2cc2a152afea",
    ),
    (
        9,
        "727754ac90555b34d90270887333b21bb3f687500dada525652c2cc2a152afea",
        "774c556262647752c7114db07d5e8c8bb8c9b55077108927ccb93645725362c9",
    ),
    (
        10,
        "774c556262647752c7114db07d5e8c8bb8c9b55077108927ccb93645725362c9",
        "7f699e324beeff15fee62cbb9c813700ac0af20a5c19b0fba2ac8210cb22baeb",
    ),
    (
        11,
        "7f699e324beeff15fee62cbb9c813700ac0af20a5c19b0fba2ac8210cb22baeb",
        "d2c30fd8036eb3383f72f52f1f291f644808f054fb7ddced37f6784c1987848f",
    ),
)
SEMANTIC_BINDING_RE = re.compile(
    rf"^{re.escape(SEMANTIC_BINDING_PREFIX)}"
    r"(?P<sequence>[0-9]{4}):(?P<parent>GENESIS|[0-9a-f]{64}):"
    r"(?P<fingerprint>[0-9a-f]{64}):author=(?P<author>[^:]+):"
    r"reviewer=(?P<reviewer>[^:]+)$"
)

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


def _without_semantic_binding_markers(data: dict) -> dict:
    """Remove valid markers only from their four schema-owned scope lists."""
    canonical = copy.deepcopy(data)
    process = canonical.get("review_process")
    if not isinstance(process, dict):
        return canonical
    for participant in process.get("participants", []):
        if not isinstance(participant, dict):
            continue
        for field in ("authored_or_modified", "independently_reviewed"):
            values = participant.get(field)
            if isinstance(values, list):
                participant[field] = [
                    value
                    for value in values
                    if not (
                        isinstance(value, str)
                        and SEMANTIC_BINDING_RE.fullmatch(value) is not None
                    )
                ]
    for edge in process.get("review_edges", []):
        if not isinstance(edge, dict) or not isinstance(edge.get("scope"), list):
            continue
        edge["scope"] = [
            value
            for value in edge["scope"]
            if not (
                isinstance(value, str)
                and SEMANTIC_BINDING_RE.fullmatch(value) is not None
            )
        ]
    return canonical


def _semantic_review_fingerprint(data: dict) -> str:
    """Hash every semantic surface while excluding only the binding markers."""
    canonical = json.dumps(
        _without_semantic_binding_markers(data),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _semantic_binding_marker(
    sequence: int,
    parent: str,
    fingerprint: str,
    author: str,
    reviewer: str,
) -> str:
    return (
        f"{SEMANTIC_BINDING_PREFIX}{sequence:04d}:{parent}:{fingerprint}:"
        f"author={author}:reviewer={reviewer}"
    )


def _semantic_binding_histories(data: dict) -> list[tuple[tuple[int, str, str], ...]]:
    """Return directional chains without interpreting their validity."""
    process = data.get("review_process")
    if not isinstance(process, dict) or not isinstance(process.get("review_edges"), list):
        return []
    histories = []
    for edge in process["review_edges"]:
        if not isinstance(edge, dict) or not isinstance(edge.get("scope"), list):
            continue
        chain = []
        for value in edge["scope"]:
            if not isinstance(value, str):
                continue
            match = SEMANTIC_BINDING_RE.fullmatch(value)
            if match is not None:
                chain.append(
                    (int(match["sequence"]), match["parent"], match["fingerprint"])
                )
        if chain:
            histories.append(tuple(chain))
    return histories


def _semantic_binding_history_errors(data: dict) -> list[str]:
    """Lock reviewed epoch history; this is a repository inventory, not a general validator."""
    histories = _semantic_binding_histories(data)
    expected = [SEMANTIC_BINDING_HISTORY, SEMANTIC_BINDING_HISTORY]
    if histories != expected:
        return ["semantic input binding history differs from append-only inventory"]
    return []


def _semantic_binding_errors(data: dict) -> list[str]:
    """Require an append-only reciprocal chain ending at the current review bytes."""
    process = data.get("review_process")
    if not isinstance(process, dict):
        return []
    edges = process.get("review_edges")
    if not isinstance(edges, list):
        return []

    errors = []
    chains: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        author = edge.get("author")
        reviewer = edge.get("reviewer")
        scope = edge.get("scope")
        if (
            not isinstance(author, str)
            or not isinstance(reviewer, str)
            or not isinstance(scope, list)
        ):
            continue
        for value in scope:
            if not isinstance(value, str) or not value.startswith(SEMANTIC_BINDING_PREFIX):
                continue
            match = SEMANTIC_BINDING_RE.fullmatch(value)
            if match is None:
                errors.append("semantic input binding marker is malformed")
                continue
            if match["author"] != author or match["reviewer"] != reviewer:
                errors.append("semantic input binding direction differs from its review edge")
                continue
            chains.setdefault((author, reviewer), []).append(
                (int(match["sequence"]), match["parent"], match["fingerprint"])
            )

    if len(chains) != 2:
        return [*errors, "semantic input binding must have exactly two reciprocal directions"]
    directions = set(chains)
    if any((reviewer, author) not in directions for author, reviewer in directions):
        return [*errors, "semantic input binding directions are not reciprocal"]

    signatures = list(chains.values())
    if signatures[0] != signatures[1]:
        errors.append("reciprocal semantic input binding chains differ")
        return errors

    chain = signatures[0]
    if not chain:
        return [*errors, "semantic input binding chain is empty"]
    if [item[0] for item in chain] != list(range(1, len(chain) + 1)):
        errors.append("semantic input binding sequence is not contiguous")
    if chain[0][1] != "GENESIS" or chain[0][2] != SEMANTIC_BINDING_GENESIS_SHA256:
        errors.append("semantic input binding genesis was replaced")
    for previous, current in zip(chain, chain[1:]):
        if current[1] != previous[2]:
            errors.append("semantic input binding parent chain is broken")
            break
    expected = _semantic_review_fingerprint(data)
    if chain[-1][2] != expected:
        errors.append("latest reciprocal review epoch does not bind current semantic inputs")
    return errors


def _semantic_epoch_work_unit_errors(data: dict) -> list[str]:
    """genesis 뒤 각 epoch 은 산문 work unit 을 최소 하나 동반해야 한다.

    ⛔ binding 은 bytes 만 묶는다 — marker 만 append 해도 chain 은 성립하므로(실측) 무엇을
       검토했는지가 아무데도 남지 않는 전이가 통과했다. residual_limits[0] 이 요구하는 것은
       "work unit 을 명시한 edge" 지 fingerprint 영수증이 아니다.
    ⛔ **양방향을 요구하지 않는다.** work unit 하나는 (author→reviewer) 한 edge 에만 들어가므로
       양쪽을 강제하면 없는 검토를 지어내게 만든다. 게이트가 거짓을 유도해서는 안 된다.
    ⛔ 마지막 marker 뒤에 산문만 추가하는 경우는 여기서 보지 않는다 — fingerprint 가 바뀌어
       `_semantic_binding_errors` 가 이미 red 로 만든다.
    """
    process = data.get("review_process")
    if not isinstance(process, dict):
        return []
    edges = process.get("review_edges")
    if not isinstance(edges, list):
        return []

    segments_by_edge = []
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("scope"), list):
            continue
        positions = [
            index
            for index, value in enumerate(edge["scope"])
            if isinstance(value, str)
            and SEMANTIC_BINDING_RE.fullmatch(value) is not None
        ]
        if len(positions) < 2:
            continue
        segments_by_edge.append([
            edge["scope"][previous + 1:current]
            for previous, current in zip(positions, positions[1:])
        ])
    if not segments_by_edge:
        return []
    depth = min(len(segments) for segments in segments_by_edge)
    if depth != max(len(segments) for segments in segments_by_edge):
        # chain 길이가 갈린 상태다 — `_semantic_binding_errors` 가 보고할 몫이다.
        return []

    errors = []
    for index in range(depth):
        if not any(segments[index] for segments in segments_by_edge):
            errors.append(
                f"semantic input binding epoch {index + 2:04d} carries no prose work unit "
                "in either direction"
            )
    return errors


def _append_synthetic_binding_epoch(data: dict, *, with_work_unit: bool = True) -> dict:
    """Append a valid epoch for validator counterexamples without rewriting genesis.

    ⛔ 기본값은 **산문 work unit 을 동반한 올바른 사용례**다. marker 만 붙이는 퇴화형이 필요한
       반례는 `with_work_unit=False` 로 명시해서 만든다 — 헬퍼가 조용히 퇴화형을 모델링하면
       그걸 쓰는 모든 반례의 양성 대조군이 계약을 어긴 상태가 된다.
    ⛔ work unit 은 **한 방향에만** 넣는다. 규칙이 요구하는 최소치가 그것이고, 양방향을 넣으면
       한 방향만으로 충분한지를 이 헬퍼를 쓰는 테스트들이 더는 보여주지 못한다.
    """
    rebound = copy.deepcopy(data)
    people = {person["name"]: person for person in rebound["review_process"]["participants"]}
    if with_work_unit:
        edge = rebound["review_process"]["review_edges"][0]
        markers = [
            value
            for value in edge["scope"]
            if isinstance(value, str) and SEMANTIC_BINDING_RE.fullmatch(value) is not None
        ]
        unit = (
            f"synthetic counterexample work unit for epoch "
            f"{len(markers) + 1:04d} ({edge['author']} -> {edge['reviewer']})"
        )
        edge["scope"].append(unit)
        people[edge["author"]]["authored_or_modified"].append(unit)
        people[edge["reviewer"]]["independently_reviewed"].append(unit)
    fingerprint = _semantic_review_fingerprint(rebound)
    for edge in rebound["review_process"]["review_edges"]:
        prior = [
            SEMANTIC_BINDING_RE.fullmatch(value)
            for value in edge["scope"]
            if isinstance(value, str) and value.startswith(SEMANTIC_BINDING_PREFIX)
        ]
        prior = [match for match in prior if match is not None]
        last = prior[-1]
        marker = _semantic_binding_marker(
            int(last["sequence"]) + 1,
            last["fingerprint"],
            fingerprint,
            edge["author"],
            edge["reviewer"],
        )
        edge["scope"].append(marker)
        people[edge["author"]]["authored_or_modified"].append(marker)
        people[edge["reviewer"]]["independently_reviewed"].append(marker)
    return rebound


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

    errors.extend(_semantic_binding_errors(data))
    errors.extend(_semantic_epoch_work_unit_errors(data))

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
    return _append_synthetic_binding_epoch(
        dict(data, findings=findings, verdict="pass")
    )


def test_semantic_review_is_bound_to_current_outputs_and_closed_findings():
    assert not review_errors(_review())


def test_semantic_binding_history_rejects_latest_epoch_replacement():
    base = _review()
    assert not _semantic_binding_history_errors(base)

    changed = copy.deepcopy(base)
    changed["residual_limits"][0] += " Latest-epoch replacement counterexample."
    replacement_fingerprint = _semantic_review_fingerprint(changed)
    people = {
        participant["name"]: participant
        for participant in changed["review_process"]["participants"]
    }
    for edge in changed["review_process"]["review_edges"]:
        marker_indexes = [
            index
            for index, value in enumerate(edge["scope"])
            if isinstance(value, str) and SEMANTIC_BINDING_RE.fullmatch(value) is not None
        ]
        marker_index = marker_indexes[-1]
        old_marker = edge["scope"][marker_index]
        match = SEMANTIC_BINDING_RE.fullmatch(old_marker)
        assert match is not None
        new_marker = _semantic_binding_marker(
            int(match["sequence"]),
            match["parent"],
            replacement_fingerprint,
            edge["author"],
            edge["reviewer"],
        )
        edge["scope"][marker_index] = new_marker
        for participant in people.values():
            for field in ("authored_or_modified", "independently_reviewed"):
                participant[field] = [
                    new_marker if value == old_marker else value
                    for value in participant[field]
                ]

    # The current-state validators cannot distinguish replacement from append-only history.
    assert not _semantic_binding_errors(changed)
    assert not _semantic_epoch_work_unit_errors(changed)
    assert not review_errors(changed)
    assert _semantic_binding_history_errors(changed) == [
        "semantic input binding history differs from append-only inventory"
    ]


def test_semantic_change_requires_a_new_reciprocal_binding_epoch():
    base = _review()
    changed = copy.deepcopy(base)
    changed["residual_limits"][0] += " Transition-gate counterexample."

    errors = _semantic_binding_errors(changed)
    assert "latest reciprocal review epoch does not bind current semantic inputs" in errors
    assert (
        "latest reciprocal review epoch does not bind current semantic inputs"
        in review_errors(changed)
    )

    rebound = _append_synthetic_binding_epoch(changed)
    assert not _semantic_binding_errors(rebound)
    assert not review_errors(rebound)

    one_sided = copy.deepcopy(rebound)
    one_sided["review_process"]["review_edges"][1]["scope"].pop()
    assert "reciprocal semantic input binding chains differ" in _semantic_binding_errors(
        one_sided
    )


def test_binding_epoch_requires_a_prose_work_unit_in_at_least_one_direction():
    """epoch 이 fingerprint 영수증만 남기고 지나가지 못하게 한다.

    ⛔ 실측(2026-08-20): binding 도입 직후에는 산문 없이 양방향 marker 만 append 해도
       `_semantic_binding_errors` 와 `review_errors` 가 모두 비었다. chain 은 성립하는데
       무엇을 검토했는지는 아무데도 남지 않는 전이였다.
    ⛔ 요구는 **한 방향 이상**이다 — work unit 하나는 (author→reviewer) 한 edge 에만 들어가므로
       양방향을 강제하면 없는 검토를 지어내게 만든다.
    """
    changed = copy.deepcopy(_review())
    changed["residual_limits"][0] += " Work-unit counterexample."

    marker_only = _append_synthetic_binding_epoch(changed, with_work_unit=False)
    # ⛔ epoch 번호를 상수로 박지 않는다 — chain 이 자라면 그 상수가 먼저 거짓이 된다(실측).
    next_epoch = 1 + sum(
        1
        for value in changed["review_process"]["review_edges"][0]["scope"]
        if isinstance(value, str) and SEMANTIC_BINDING_RE.fullmatch(value) is not None
    )
    expected = (
        f"semantic input binding epoch {next_epoch:04d} carries no prose work unit "
        "in either direction"
    )
    assert _semantic_epoch_work_unit_errors(marker_only) == [expected]
    # ⛔ helper 존재가 아니라 **validator 배선**을 본다.
    assert expected in review_errors(marker_only)

    one_direction = _append_synthetic_binding_epoch(changed)
    assert not _semantic_epoch_work_unit_errors(one_direction)
    assert not review_errors(one_direction)
    edges = one_direction["review_process"]["review_edges"]
    prose_added = [
        sum(
            1
            for value in edge["scope"]
            if isinstance(value, str) and SEMANTIC_BINDING_RE.fullmatch(value) is None
        )
        - sum(
            1
            for value in changed["review_process"]["review_edges"][index]["scope"]
            if isinstance(value, str) and SEMANTIC_BINDING_RE.fullmatch(value) is None
        )
        for index, edge in enumerate(edges)
    ]
    assert sorted(prose_added) == [0, 1], prose_added


def test_semantic_binding_genesis_cannot_be_replaced_during_reseal():
    changed = copy.deepcopy(_review())
    for edge in changed["review_process"]["review_edges"]:
        edge["scope"] = [
            value.replace(
                SEMANTIC_BINDING_GENESIS_SHA256,
                "0" * 64,
            )
            if isinstance(value, str) and value.startswith(SEMANTIC_BINDING_PREFIX)
            else value
            for value in edge["scope"]
        ]
    assert "semantic input binding genesis was replaced" in _semantic_binding_errors(changed)


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
