"""Validate the six topic-only deliverables against the migration manifest.

The production checks intentionally stay red until ADR-041 and the five spec
documents exist. Synthetic controls below keep the gate itself testable before
those deliverables are authored.
"""
import json
import pathlib
import re
import shutil
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import topic_migration_doc_bundle as B  # noqa: E402


def _manifest() -> dict:
    return json.loads((REPO / B.PIN_FILES[1]).read_text())


@pytest.mark.parametrize("destination", sorted(B.DOCUMENT_PATHS))
def test_document_matches_manifest(destination):
    errors = B.check_destination_document(destination)
    assert not errors, "\n".join(errors)


def _synthetic_document(destination: str) -> str:
    manifest = _manifest()
    requirements = B.requirements_from_manifest(manifest)
    by_rid = {r["rid"]: r for r in requirements}
    mine = [r for r in requirements if r["destination"] == destination]
    title = "## ADR-041: synthetic" if destination == "ADR" else f"# {destination} synthetic"
    output = [title, "", B.render_document_header(destination), ""]

    for requirement in mine:
        rid = requirement["rid"]
        owner = requirement.get("normative_owner")
        output.extend(
            [
                f"<!-- rid: {rid} -->",
                f"<!-- requirement-meta: disposition={requirement['disposition']} "
                f"owner={owner} -->",
                f'<a id="{rid.lower()}"></a>',
                f"### {rid}",
                f"Requirement prose for {rid}.",
            ]
        )
        if requirement["disposition"] == "proposed":
            output.append(B.PROPOSED_MARK)
        for field, _label in B.RELATIONS:
            if field == "supports":
                continue
            for target in B.as_list(requirement.get(field)):
                output.append(f"<!-- relation: {field} target={target} -->")
                output.append(B.render_relation_line(destination, field, target, by_rid))
        for evidence in requirements:
            if rid not in B.as_list(evidence.get("supports")):
                continue
            evidence_rid = evidence["rid"]
            output.extend(
                [
                    f"<!-- evidence: {evidence_rid} supports={rid} -->",
                    f"Evidence prose for {evidence_rid}.",
                    f"<!-- /evidence: {evidence_rid} -->",
                ]
            )
        output.extend([f"<!-- /rid: {rid} -->", ""])
    return "\n".join(output)


def _synthetic_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    for name in B.PIN_FILES[:3]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, target)
    for destination, name in B.DOCUMENT_PATHS.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_synthetic_document(destination))
    return tmp_path


@pytest.mark.parametrize("destination", sorted(B.DOCUMENT_PATHS))
def test_synthetic_document_is_an_allowed_positive_control(destination):
    assert B.document_errors(destination, _synthetic_document(destination), _manifest()) == []


def test_active_requirement_may_mention_proposed_state_inline():
    text = _mention_proposed_state_from_active_requirement(_synthetic_document("ADR"))
    assert B.document_errors("ADR", text, _manifest()) == []


def test_check_documents_accepts_all_six_synthetic_files(tmp_path):
    repo = _synthetic_repo(tmp_path)
    assert B.check_documents(repo) == []


def test_check_documents_rejects_a_missing_synthetic_file(tmp_path):
    repo = _synthetic_repo(tmp_path)
    (repo / B.DOCUMENT_PATHS["LOAD"]).unlink()
    errors = B.check_documents(repo)
    assert any("LOAD: document does not exist" in error for error in errors)


def _remove_first_requirement(text: str) -> str:
    match = re.search(
        r"<!-- rid: ([RE]-[A-Z]+-\d+) -->.*?<!-- /rid: \1 -->\n?",
        text,
        re.S,
    )
    assert match
    return text[:match.start()] + text[match.end():]


def _duplicate_first_requirement(text: str) -> str:
    match = re.search(
        r"<!-- rid: ([RE]-[A-Z]+-\d+) -->.*?<!-- /rid: \1 -->\n?",
        text,
        re.S,
    )
    assert match
    return text + "\n" + match.group(0)


def _add_foreign_requirement(text: str) -> str:
    return (
        text
        + "\n<!-- rid: R-HAND-1 -->\n"
        + "<!-- requirement-meta: disposition=active owner=HAND -->\n"
        + "Foreign requirement.\n<!-- /rid: R-HAND-1 -->\n"
    )


def _empty_first_requirement(text: str) -> str:
    return re.sub(r"^Requirement prose for [RE]-[A-Z]+-\d+\.$", "", text, count=1, flags=re.M)


def _drop_proposed_mark(text: str) -> str:
    return text.replace(B.PROPOSED_MARK, "", 1)


def _hide_proposed_mark(text: str) -> str:
    """A state marker in an HTML comment is not visible contract text."""
    return text.replace(B.PROPOSED_MARK, f"<!-- {B.PROPOSED_MARK} -->", 1)


def _duplicate_proposed_mark(text: str) -> str:
    """Duplicate state labels make the binding ambiguous."""
    return text.replace(B.PROPOSED_MARK, f"{B.PROPOSED_MARK}\n{B.PROPOSED_MARK}", 1)


def _mark_active_requirement_as_proposed(text: str) -> str:
    """An active requirement must not be visibly downgraded to proposed."""
    marker = "<!-- requirement-meta: disposition=active owner=ADR -->"
    assert marker in text
    return text.replace(marker, marker + f"\n{B.PROPOSED_MARK}", 1)


def _mention_proposed_state_from_active_requirement(text: str) -> str:
    """Active prose may explain that it is independent of a proposed policy."""
    marker = "<!-- requirement-meta: disposition=active owner=ADR -->"
    assert marker in text
    return text.replace(
        marker,
        marker + f"\n이 요구는 ADR의 `{B.PROPOSED_MARK}` 정책 채택 여부와 무관하다.",
        1,
    )


def _drop_first_relation(text: str) -> str:
    return re.sub(r"^<!-- relation: .*? -->\n", "", text, count=1, flags=re.M)


def _drop_first_visible_relation(text: str) -> str:
    return re.sub(
        r"^- (?:references|conditional_references|deferred_references): .*?\n",
        "",
        text,
        count=1,
        flags=re.M,
    )


def _change_relation_kind(text: str) -> str:
    return text.replace("relation: references", "relation: conditional_references", 1)


def _replace_relation_with_prefix_collision(text: str) -> str:
    return text.replace("target=R-CUT-1 -->", "target=R-CUT-12 -->", 1)


def _forge_relation_href(text: str) -> str:
    return re.sub(
        r"^(- (?:references|conditional_references|deferred_references): "
        r"\[[RE]-[A-Z]+-\d+\]\()[^)]+(\))$",
        r"\1wrong-target\2",
        text,
        count=1,
        flags=re.M,
    )


def _drop_first_anchor(text: str) -> str:
    return re.sub(r'^<a id="[re]-[a-z]+-\d+"></a>\n', "", text, count=1, flags=re.M)


def _drop_first_rid_heading(text: str) -> str:
    return re.sub(r"^### [RE]-[A-Z]+-\d+\n", "", text, count=1, flags=re.M)


def _drop_first_evidence(text: str) -> str:
    return re.sub(
        r"<!-- evidence: (E-[A-Z]+-\d+) supports=[RE]-[A-Z]+-\d+ -->.*?"
        r"<!-- /evidence: \1 -->\n?",
        "",
        text,
        count=1,
        flags=re.S,
    )


def _duplicate_first_evidence(text: str) -> str:
    match = re.search(
        r"<!-- evidence: (E-[A-Z]+-\d+) supports=[RE]-[A-Z]+-\d+ -->.*?"
        r"<!-- /evidence: \1 -->\n?",
        text,
        re.S,
    )
    assert match
    return text[:match.end()] + "\n" + match.group(0) + text[match.end():]


def _empty_first_evidence(text: str) -> str:
    return re.sub(r"^Evidence prose for E-[A-Z]+-\d+\.$", "", text, count=1, flags=re.M)


def _cross_evidence_blocks(text: str) -> str:
    pattern = re.compile(
        r"(?P<o1><!-- evidence: E-INV-1 supports=R-INV-1 -->)"
        r"(?P<b1>.*?)(?P<c1><!-- /evidence: E-INV-1 -->)\n"
        r"(?P<o2><!-- evidence: E-INV-2 supports=R-INV-1 -->)"
        r"(?P<b2>.*?)(?P<c2><!-- /evidence: E-INV-2 -->)",
        re.S,
    )
    match = pattern.search(text)
    assert match
    crossed = (
        match.group("o1")
        + match.group("b1")
        + match.group("o2")
        + match.group("b2")
        + match.group("c1")
        + "\n"
        + match.group("c2")
    )
    return text[:match.start()] + crossed + text[match.end():]


def _forge_duty(text: str) -> str:
    return re.sub(r"^- 책임: .*$", "- 책임: 잘못된 책임", text, count=1, flags=re.M)


def _forge_archive_sha(text: str) -> str:
    return re.sub(
        r"^- archive SHA: `[0-9a-f]{64}`$",
        f"- archive SHA: `{'0' * 64}`",
        text,
        count=1,
        flags=re.M,
    )


def _add_conflicting_status_header(text: str) -> str:
    """The correct status line must not mask a second contradictory value."""
    line = next(line for line in text.splitlines() if line.startswith("- 상태:"))
    return text.replace(line, line + "\n- 상태: Approved", 1)


def _weaken_verify_command(text: str) -> str:
    return text.replace("topic_migration_manifest.py preflight", "topic_migration_manifest.py verify", 1)


def _forge_requirement_metadata(text: str) -> str:
    return text.replace("disposition=active owner=ADR", "disposition=proposed owner=ADR", 1)


def _nest_requirement_blocks(text: str) -> str:
    """⛔ A 를 닫기 전에 B 를 열면 구간이 **중첩**된다 — 소유 경계가 무너진다."""
    rids = re.findall(r"<!-- rid: ([RE]-[A-Z]+-\d+) -->", text)
    assert len(rids) >= 2
    a, b = rids[0], rids[1]
    return text.replace(f"<!-- /rid: {a} -->\n", "", 1).replace(
        f"<!-- /rid: {b} -->", f"<!-- /rid: {b} -->\n<!-- /rid: {a} -->", 1
    )


def _mismatch_requirement_close(text: str) -> str:
    """⛔ 여는 표식과 **다른 RID 로 닫으면** 두 구간의 본문이 뒤섞인다."""
    rids = re.findall(r"<!-- rid: ([RE]-[A-Z]+-\d+) -->", text)
    assert len(rids) >= 2
    a, b = rids[0], rids[1]
    out = text.replace(f"<!-- /rid: {a} -->", "<!-- /rid: __SWAP__ -->", 1)
    out = out.replace(f"<!-- /rid: {b} -->", f"<!-- /rid: {a} -->", 1)
    return out.replace("<!-- /rid: __SWAP__ -->", f"<!-- /rid: {b} -->", 1)


def _unclose_last_requirement(text: str) -> str:
    """⛔ 닫지 않으면 그 뒤 문서 전체가 그 구간의 본문으로 읽힌다."""
    rids = re.findall(r"<!-- rid: ([RE]-[A-Z]+-\d+) -->", text)
    assert rids
    last = rids[-1]
    return text.replace(f"<!-- /rid: {last} -->", "", 1)


@pytest.mark.parametrize(
    ("name", "mutate", "message"),
    [
        ("missing RID", _remove_first_requirement, "opening markers differ"),
        ("duplicate RID", _duplicate_first_requirement, "opening markers differ"),
        ("foreign RID", _add_foreign_requirement, "opening markers differ"),
        ("empty requirement", _empty_first_requirement, "no substantive prose"),
        ("missing proposed state", _drop_proposed_mark, "visible [제안·결정 대기] count differs"),
        ("hidden proposed state", _hide_proposed_mark, "visible [제안·결정 대기] count differs"),
        ("duplicate proposed state", _duplicate_proposed_mark, "visible [제안·결정 대기] count differs"),
        ("active marked proposed", _mark_active_requirement_as_proposed, "visible [제안·결정 대기] count differs"),
        ("missing relation", _drop_first_relation, "relation markers differ"),
        ("hidden-only relation", _drop_first_visible_relation, "visible relation links differ"),
        ("wrong relation kind", _change_relation_kind, "relation markers differ"),
        ("RID prefix collision", _replace_relation_with_prefix_collision, "relation markers differ"),
        ("wrong relation href", _forge_relation_href, "visible relation links differ"),
        ("missing RID anchor", _drop_first_anchor, "expected one visible target anchor"),
        ("missing RID heading", _drop_first_rid_heading, "expected one RID heading"),
        ("missing evidence", _drop_first_evidence, "evidence markers differ"),
        ("duplicate evidence", _duplicate_first_evidence, "evidence markers differ"),
        ("empty evidence", _empty_first_evidence, "has no substantive prose"),
        ("crossed evidence", _cross_evidence_blocks, "nested evidence block"),
        ("wrong duty", _forge_duty, "missing header line"),
        ("wrong archive SHA", _forge_archive_sha, "missing header line"),
        ("conflicting status header", _add_conflicting_status_header, "missing header line"),
        ("weakened command", _weaken_verify_command, "missing header line"),
        ("wrong requirement metadata", _forge_requirement_metadata, "metadata differs"),
        ("nested requirement block", _nest_requirement_blocks, "nested requirement block"),
        ("mismatched requirement close", _mismatch_requirement_close, "mismatched requirement close"),
        ("unclosed requirement block", _unclose_last_requirement, "unclosed requirement block"),
    ],
)
def test_synthetic_counterexamples_are_rejected(name, mutate, message):
    original = _synthetic_document("ADR")
    mutant = mutate(original)
    assert mutant != original, f"counterexample did not mutate input: {name}"
    errors = B.document_errors("ADR", mutant, _manifest())
    assert errors, f"counterexample escaped: {name}"
    assert any(message in error for error in errors), "\n".join(errors)
