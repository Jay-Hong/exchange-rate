#!/usr/bin/env python3
"""Generate and verify the pinned input bundle for topic-only document authoring.

The bundle is derived data. A valid bundle must be byte-for-byte identical to a
fresh render from the five pinned inputs; checking only the pin lines is not
enough because requirement bodies or relation edges could be removed.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import hashlib
import json
import pathlib
import posixpath
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PIN_FILES = (
    "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md",
    "spec/topic-only-migration-manifest.json",
    "spec/topic-only-baseline-facts.md",
    "spec/topic-only.lock.json",
    "TOPIC_ONLY_MIGRATION_MATRIX.md",
)
DESTINATIONS = {
    "ADR": "DECISIONS.md ADR-041 (canonical; draft in /tmp)",
    "HAND": "spec/topic-snapshot-handoff.md",
    "CLIENT": "spec/ios-topic-state-machine.md",
    "CUT": "spec/legacy-cutover.md",
    "LOAD": "spec/revalidation-and-load.md",
    "HEALTH": "spec/publisher-health-slo.md",
    "BASE": "spec/topic-only-baseline-facts.md (existing; do not rewrite)",
}
DESTINATION_ORDER = ("ADR", "HAND", "CLIENT", "CUT", "LOAD", "HEALTH", "BASE")
DOCUMENT_PATHS = {
    "ADR": "DECISIONS.md",
    "HAND": "spec/topic-snapshot-handoff.md",
    "CLIENT": "spec/ios-topic-state-machine.md",
    "CUT": "spec/legacy-cutover.md",
    "LOAD": "spec/revalidation-and-load.md",
    "HEALTH": "spec/publisher-health-slo.md",
}
LINK_PATHS = {**DOCUMENT_PATHS, "BASE": "spec/topic-only-baseline-facts.md"}
DOCUMENT_DUTIES = {
    "ADR": "불변식 · 결정 · arming 게이트",
    "HAND": "서버 build · ack · close 계약",
    "CLIENT": "클라이언트 상태기계 · 재시도 · 재검증",
    "CUT": "삭제 범위 · 문서 정정 · 테스트 · 순서",
    "LOAD": "jitter · single-flight · bounded wait",
    "HEALTH": "publisher health · SLO",
}
DOCUMENT_VERIFY_COMMAND = "python3 scripts/topic_migration_manifest.py preflight"
RELATIONS = (
    ("references", "OUT ref"),
    ("conditional_references", "OUT cond"),
    ("deferred_references", "OUT defer"),
    ("supports", "OUT supports"),
)
PIN_RE = re.compile(r"^# SHA256 ([0-9a-f]{64})  (\S+)$", re.M)
COUNT_RE = re.compile(r"^# total_lines=(\d+)  requirements=(\d+)$", re.M)
ADR_HEADING_RE = re.compile(r"^(#{2,4})\s*ADR-041\b", re.M)
RID_MARK_RE = re.compile(r"<!--\s*(/?)rid:\s*([RE]-[A-Z]+-\d+)\s*-->")
META_RE = re.compile(
    r"<!--\s*requirement-meta:\s*disposition=(\w+)\s+owner=([A-Z]+|None)\s*-->"
)
RELATION_RE = re.compile(
    r"<!--\s*relation:\s*"
    r"(references|conditional_references|deferred_references)\s+"
    r"target=([RE]-[A-Z]+-\d+)\s*-->"
)
VISIBLE_RELATION_RE = re.compile(
    r"^- (references|conditional_references|deferred_references): "
    r"\[([RE]-[A-Z]+-\d+)\]\(([^)\s]+)\)$",
    re.M,
)
EVIDENCE_OPEN_RE = re.compile(
    r"<!--\s*evidence:\s*(E-[A-Z]+-\d+)\s+supports=([RE]-[A-Z]+-\d+)\s*-->"
)
EVIDENCE_CLOSE_RE = re.compile(r"<!--\s*/evidence:\s*(E-[A-Z]+-\d+)\s*-->")
EVIDENCE_MARK_RE = re.compile(
    r"<!--\s*(/?)evidence:\s*(E-[A-Z]+-\d+)"
    r"(?:\s+supports=[RE]-[A-Z]+-\d+)?\s*-->"
)
EVIDENCE_BLOCK_RE = re.compile(
    r"<!--\s*evidence:\s*(?P<rid>E-[A-Z]+-\d+)\s+"
    r"supports=(?P<target>[RE]-[A-Z]+-\d+)\s*-->.*?"
    r"<!--\s*/evidence:\s*(?P=rid)\s*-->",
    re.S,
)
PROPOSED_MARK = "[제안·결정 대기]"
HEADER_LINES = 40


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def as_list(value) -> list[str]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return []


def source_fence(source: str) -> str:
    """Return a fence longer than every backtick run in the source."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", source)), default=0)
    return "`" * max(4, longest + 1)


def requirements_from_manifest(manifest: dict) -> list[dict]:
    return [
        dict(
            requirement,
            disposition=block["disposition"],
            block_id=block["id"],
            block_title=block["title"],
        )
        for block in manifest["blocks"]
        for requirement in block.get("requirements", [])
    ]


def render_document_header(destination: str, repo: pathlib.Path = REPO) -> str:
    if destination not in DOCUMENT_PATHS:
        raise ValueError(f"unknown document destination: {destination}")
    manifest = json.loads((repo / PIN_FILES[1]).read_text())
    metadata = manifest["source_metadata"]
    commits = manifest["pinned_commit"]
    return "\n".join(
        [
            f"- 책임: {DOCUMENT_DUTIES[destination]}",
            f"- 상태: {metadata['status']}",
            f"- 코드 근거 기준일: {metadata['as_of']}",
            f"- server 기준 commit: `{commits['server']}`",
            f"- iOS 기준 commit: `{commits['ios']}`",
            f"- archive SHA: `{sha256(repo / PIN_FILES[0])}`",
            f"- manifest SHA: `{sha256(repo / PIN_FILES[1])}`",
            f"- baseline SHA: `{sha256(repo / PIN_FILES[2])}`",
            f"- 검증: `{DOCUMENT_VERIFY_COMMAND}`",
        ]
    )


def relation_href(source_destination: str, target: str, by_rid: dict[str, dict]) -> str:
    target_destination = by_rid[target]["destination"]
    anchor = f"#{target.lower()}"
    if source_destination == target_destination:
        return anchor
    source_dir = posixpath.dirname(DOCUMENT_PATHS[source_destination]) or "."
    relative = posixpath.relpath(LINK_PATHS[target_destination], start=source_dir)
    return f"{relative}{anchor}"


def render_relation_line(
    source_destination: str,
    field: str,
    target: str,
    by_rid: dict[str, dict],
) -> str:
    return f"- {field}: [{target}]({relation_href(source_destination, target, by_rid)})"


def render_bundle(repo: pathlib.Path = REPO) -> str:
    manifest = json.loads((repo / PIN_FILES[1]).read_text())
    archive = (repo / PIN_FILES[0]).read_text().splitlines()
    requirements = requirements_from_manifest(manifest)
    rid_counts = collections.Counter(r["rid"] for r in requirements)
    duplicates = sorted(rid for rid, count in rid_counts.items() if count != 1)
    if duplicates:
        raise ValueError(f"requirement RID is not unique: {duplicates}")
    by_rid = {r["rid"]: r for r in requirements}

    inbound: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    evidence_by_target: dict[str, list[dict]] = collections.defaultdict(list)
    for requirement in requirements:
        for relation, _label in RELATIONS:
            for target in as_list(requirement.get(relation)):
                if target not in by_rid:
                    raise ValueError(f"{requirement['rid']} has missing target {target}")
                inbound[target].append(
                    (requirement["rid"], requirement["destination"], relation)
                )
                if relation == "supports":
                    evidence_by_target[target].append(requirement)

    output = ["# topic-only document authoring source - pinned inputs", "#"]
    output.extend(f"# SHA256 {sha256(repo / name)}  {name}" for name in PIN_FILES)
    output.extend(
        [
            f"# total_lines={manifest['total_lines']}  requirements={len(requirements)}",
            "# source_metadata="
            + json.dumps(manifest["source_metadata"], ensure_ascii=False, sort_keys=True),
            "# pinned_commit="
            + json.dumps(manifest["pinned_commit"], ensure_ascii=False, sort_keys=True),
            "",
        ]
    )

    for destination in DESTINATION_ORDER:
        mine = sorted(
            (r for r in requirements if r["destination"] == destination),
            key=lambda r: (r["source"]["start"], r["rid"]),
        )
        output.extend(
            [
                "",
                "=" * 78,
                f"# {destination} -> {DESTINATIONS[destination]}  requirements={len(mine)}",
                "",
            ]
        )
        if destination in DOCUMENT_PATHS:
            header = render_document_header(destination, repo)
            header_fence = source_fence(header)
            output.extend(
                [
                    "required document header:",
                    header_fence,
                    header,
                    header_fence,
                    "",
                ]
            )
        for requirement in mine:
            source = requirement["source"]
            start, end = source["start"], source["end"]
            if not (1 <= start <= end <= len(archive)):
                raise ValueError(f"{requirement['rid']} has invalid source range {start}-{end}")
            output.append(
                f"## {requirement['rid']}  L{start}-{end}  "
                f"{requirement['disposition']}  owner={requirement['normative_owner']}  "
                f"block={requirement['block_id']}"
            )
            output.append(f"block title: {requirement['block_title']}")
            document_contract = []
            if destination in DOCUMENT_PATHS:
                document_contract.extend(
                    [
                        f"<!-- rid: {requirement['rid']} -->",
                        "<!-- requirement-meta: "
                        f"disposition={requirement['disposition']} "
                        f"owner={requirement['normative_owner']} -->",
                        f'<a id="{requirement["rid"].lower()}"></a>',
                        f"### {requirement['rid']}",
                    ]
                )
                if requirement["disposition"] == "proposed":
                    document_contract.append(PROPOSED_MARK)
            for relation, label in RELATIONS:
                targets = as_list(requirement.get(relation))
                if targets:
                    output.append(
                        f"{label}: "
                        + " ".join(f"{target}({by_rid[target]['destination']})" for target in targets)
                    )
                if destination in DOCUMENT_PATHS and relation != "supports":
                    for target in targets:
                        document_contract.append(
                            f"<!-- relation: {relation} target={target} -->"
                        )
                        document_contract.append(
                            render_relation_line(destination, relation, target, by_rid)
                        )
            if destination in DOCUMENT_PATHS:
                for evidence in evidence_by_target.get(requirement["rid"], []):
                    evidence_rid = evidence["rid"]
                    evidence_source = evidence["source"]
                    evidence_start = evidence_source["start"]
                    evidence_end = evidence_source["end"]
                    if not (1 <= evidence_start <= evidence_end <= len(archive)):
                        raise ValueError(
                            f"{evidence_rid} has invalid source range "
                            f"{evidence_start}-{evidence_end}"
                        )
                    evidence_body = "\n".join(
                        archive[evidence_start - 1:evidence_end]
                    )
                    document_contract.extend(
                        [
                            f"<!-- evidence: {evidence_rid} "
                            f"supports={requirement['rid']} -->",
                            evidence_body,
                            f"<!-- /evidence: {evidence_rid} -->",
                        ]
                    )
                document_contract.append(f"<!-- /rid: {requirement['rid']} -->")
                contract = "\n".join(document_contract)
                contract_fence = source_fence(contract)
                output.extend(
                    [
                        "required document markers:",
                        contract_fence,
                        contract,
                        contract_fence,
                    ]
                )
            backlinks = [
                f"{rid}({source_destination},{relation})"
                for rid, source_destination, relation in inbound.get(requirement["rid"], [])
                if source_destination != destination
            ]
            if backlinks:
                output.append("IN backlinks: " + " ".join(backlinks))
            body = "\n".join(archive[start - 1:end])
            fence = source_fence(body)
            output.extend([fence, body, fence])

    output.extend(["", "=" * 78, "# SUPPLEMENTAL PINNED INPUT CONTENT"])
    for name in (PIN_FILES[2], PIN_FILES[3], PIN_FILES[4]):
        content = (repo / name).read_text().rstrip("\n")
        fence = source_fence(content)
        output.extend(["", f"## {name}", fence, content, fence])

    return "\n".join(output) + "\n"


def pin_errors(text: str, repo: pathlib.Path = REPO) -> list[str]:
    pins = PIN_RE.findall(text)
    paths = [path for _digest, path in pins]
    counts = collections.Counter(paths)
    required = set(PIN_FILES)
    errors: list[str] = []

    duplicates = sorted(path for path, count in counts.items() if count > 1)
    missing = sorted(required - set(paths))
    extra = sorted(set(paths) - required)
    if duplicates:
        errors.append(f"duplicate pins: {duplicates}")
    if missing:
        errors.append(f"missing pins: {missing}")
    if extra:
        errors.append(f"unexpected pins: {extra}")

    for expected, name in pins:
        if name in required and (repo / name).is_file():
            actual = sha256(repo / name)
            if actual != expected:
                errors.append(
                    f"SHA mismatch {name}: bundle {expected[:12]} != actual {actual[:12]}"
                )
        elif name in required:
            errors.append(f"missing input file: {name}")

    count_matches = COUNT_RE.findall(text)
    if len(count_matches) != 1:
        errors.append(f"expected one count header, found {len(count_matches)}")
    return errors


def check_bundle(path: pathlib.Path, repo: pathlib.Path = REPO) -> list[str]:
    if not path.is_file():
        return [f"bundle does not exist: {path}"]
    actual = path.read_text()
    errors = pin_errors(actual, repo)
    try:
        expected = render_bundle(repo)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return errors + [f"could not render expected bundle: {exc}"]
    if actual != expected:
        diff = "\n".join(
            list(
                difflib.unified_diff(
                    actual.splitlines(),
                    expected.splitlines(),
                    fromfile=str(path),
                    tofile="fresh render",
                    n=2,
                )
            )[:20]
        )
        errors.append("bundle differs from a fresh render" + (f"\n{diff}" if diff else ""))
    return errors


def _document_section(destination: str, text: str) -> tuple[str | None, list[str]]:
    if destination != "ADR":
        return text, []
    headings = list(ADR_HEADING_RE.finditer(text))
    if len(headings) != 1:
        return None, [f"ADR: expected one ADR-041 heading, found {len(headings)}"]
    level = re.escape(headings[0].group(1))
    next_heading = re.compile(rf"^{level}\s*ADR-(?!041)", re.M).search(
        text, headings[0].end()
    )
    end = next_heading.start() if next_heading else len(text)
    return text[headings[0].start():end], []


def _requirement_block(text: str, rid: str) -> tuple[str | None, list[str]]:
    opening = f"<!-- rid: {rid} -->"
    closing = f"<!-- /rid: {rid} -->"
    errors = []
    if text.count(opening) != 1 or text.count(closing) != 1:
        return None, [
            f"{rid}: expected one opening/closing marker, found "
            f"{text.count(opening)}/{text.count(closing)}"
        ]
    start = text.find(opening) + len(opening)
    end = text.find(closing)
    if end <= start:
        errors.append(f"{rid}: requirement markers are reversed")
        return None, errors
    return text[start:end], errors


def _marker_order_errors(text: str) -> list[str]:
    errors = []
    current = None
    for match in RID_MARK_RE.finditer(text):
        closing, rid = match.groups()
        if not closing:
            if current is not None:
                errors.append(f"nested requirement block: {current} contains {rid}")
            current = rid
        elif current != rid:
            errors.append(f"mismatched requirement close: open={current}, close={rid}")
            current = None
        else:
            current = None
    if current is not None:
        errors.append(f"unclosed requirement block: {current}")
    return errors


def _evidence_order_errors(text: str) -> list[str]:
    errors = []
    current = None
    for closing, rid in EVIDENCE_MARK_RE.findall(text):
        if not closing:
            if current is not None:
                errors.append(f"nested evidence block: {current} contains {rid}")
            current = rid
        elif current != rid:
            errors.append(f"mismatched evidence close: open={current}, close={rid}")
            current = None
        else:
            current = None
    if current is not None:
        errors.append(f"unclosed evidence block: {current}")
    return errors


def _visible_requirement_text(body: str) -> str:
    without_evidence = EVIDENCE_BLOCK_RE.sub("", body)
    without_comments = re.sub(r"<!--.*?-->", "", without_evidence, flags=re.S)
    without_relations = VISIBLE_RELATION_RE.sub("", without_comments)
    without_anchors = re.sub(
        r'^<a id="[re]-[a-z]+-\d+"></a>$', "", without_relations, flags=re.M
    )
    without_headings = re.sub(
        r"^#{1,6}\s+[RE]-[A-Z]+-\d+\b.*$", "", without_anchors, flags=re.M
    )
    return without_headings.replace(PROPOSED_MARK, "").strip()


def _visible_proposed_mark_count(body: str) -> int:
    """Count standalone state labels, excluding comments and evidence prose."""
    without_evidence = EVIDENCE_BLOCK_RE.sub("", body)
    without_comments = re.sub(r"<!--.*?-->", "", without_evidence, flags=re.S)
    return sum(
        line.strip() == PROPOSED_MARK for line in without_comments.splitlines()
    )


def document_errors(
    destination: str,
    text: str,
    manifest: dict,
    repo: pathlib.Path = REPO,
) -> list[str]:
    """Validate one already-isolated destination document against the manifest."""
    if destination not in DOCUMENT_PATHS:
        return [f"unknown document destination: {destination}"]

    requirements = requirements_from_manifest(manifest)
    mine = [r for r in requirements if r["destination"] == destination]
    by_rid = {r["rid"]: r for r in requirements}
    expected_rids = collections.Counter({r["rid"]: 1 for r in mine})
    errors: list[str] = []

    header_lines = text.splitlines()[:HEADER_LINES]
    expected_header = render_document_header(destination, repo).splitlines()
    for line in expected_header:
        field = line.split(":", 1)[0] + ":"
        actual = [candidate for candidate in header_lines if candidate.startswith(field)]
        if actual != [line]:
            errors.append(
                f"{destination}: missing header line: {line}; "
                f"actual {field} entries={actual}"
            )

    openings = collections.Counter(
        rid for closing, rid in RID_MARK_RE.findall(text) if not closing
    )
    closings = collections.Counter(
        rid for closing, rid in RID_MARK_RE.findall(text) if closing
    )
    if openings != expected_rids:
        errors.append(
            f"{destination}: requirement opening markers differ: "
            f"actual={dict(openings)}, expected={dict(expected_rids)}"
        )
    if closings != expected_rids:
        errors.append(
            f"{destination}: requirement closing markers differ: "
            f"actual={dict(closings)}, expected={dict(expected_rids)}"
        )
    errors.extend(f"{destination}: {error}" for error in _marker_order_errors(text))

    expected_doc_relations = collections.Counter(
        (field, target)
        for requirement in mine
        for field, _label in RELATIONS
        if field != "supports"
        for target in as_list(requirement.get(field))
    )
    actual_doc_relations = collections.Counter(RELATION_RE.findall(text))
    if actual_doc_relations != expected_doc_relations:
        errors.append(
            f"{destination}: relation markers differ: actual={dict(actual_doc_relations)}, "
            f"expected={dict(expected_doc_relations)}"
        )

    expected_visible_relations = collections.Counter(
        (
            field,
            target,
            relation_href(destination, target, by_rid),
        )
        for requirement in mine
        for field, _label in RELATIONS
        if field != "supports"
        for target in as_list(requirement.get(field))
    )
    actual_visible_relations = collections.Counter(VISIBLE_RELATION_RE.findall(text))
    if actual_visible_relations != expected_visible_relations:
        errors.append(
            f"{destination}: visible relation links differ: "
            f"actual={dict(actual_visible_relations)}, "
            f"expected={dict(expected_visible_relations)}"
        )

    expected_doc_evidence = collections.Counter(
        (evidence["rid"], target)
        for evidence in requirements
        for target in as_list(evidence.get("supports"))
        if by_rid.get(target, {}).get("destination") == destination
    )
    actual_doc_evidence = collections.Counter(EVIDENCE_OPEN_RE.findall(text))
    if actual_doc_evidence != expected_doc_evidence:
        errors.append(
            f"{destination}: evidence markers differ: actual={dict(actual_doc_evidence)}, "
            f"expected={dict(expected_doc_evidence)}"
        )
    expected_evidence_closes = collections.Counter(
        evidence for evidence, _target in expected_doc_evidence.elements()
    )
    actual_evidence_closes = collections.Counter(EVIDENCE_CLOSE_RE.findall(text))
    if actual_evidence_closes != expected_evidence_closes:
        errors.append(
            f"{destination}: evidence closing markers differ: "
            f"actual={dict(actual_evidence_closes)}, expected={dict(expected_evidence_closes)}"
        )
    errors.extend(f"{destination}: {error}" for error in _evidence_order_errors(text))

    for requirement in mine:
        rid = requirement["rid"]
        body, block_errors = _requirement_block(text, rid)
        errors.extend(f"{destination}: {error}" for error in block_errors)
        if body is None:
            continue

        expected_owner = requirement.get("normative_owner")
        expected_meta = collections.Counter(
            [(requirement["disposition"], str(expected_owner))]
        )
        actual_meta = collections.Counter(META_RE.findall(body))
        if actual_meta != expected_meta:
            errors.append(
                f"{destination}/{rid}: requirement metadata differs: "
                f"actual={dict(actual_meta)}, expected={dict(expected_meta)}"
            )
        anchor = f'<a id="{rid.lower()}"></a>'
        if body.count(anchor) != 1:
            errors.append(
                f"{destination}/{rid}: expected one visible target anchor {anchor}, "
                f"found {body.count(anchor)}"
            )
        heading_count = len(
            re.findall(rf"^#{{1,6}}\s+{re.escape(rid)}\b", body, flags=re.M)
        )
        if heading_count != 1:
            errors.append(
                f"{destination}/{rid}: expected one RID heading, found {heading_count}"
            )
        if not re.search(r"\w", _visible_requirement_text(body), re.UNICODE):
            errors.append(f"{destination}/{rid}: requirement has no substantive prose")
        proposed_count = _visible_proposed_mark_count(body)
        expected_proposed_count = 1 if requirement["disposition"] == "proposed" else 0
        if proposed_count != expected_proposed_count:
            errors.append(
                f"{destination}/{rid}: visible {PROPOSED_MARK} count differs: "
                f"actual={proposed_count}, expected={expected_proposed_count}"
            )

        expected_relations = collections.Counter(
            (field, target)
            for field, _label in RELATIONS
            if field != "supports"
            for target in as_list(requirement.get(field))
        )
        actual_relations = collections.Counter(RELATION_RE.findall(body))
        if actual_relations != expected_relations:
            errors.append(
                f"{destination}/{rid}: relations differ: actual={dict(actual_relations)}, "
                f"expected={dict(expected_relations)}"
            )
        expected_links = collections.Counter(
            (
                field,
                target,
                relation_href(destination, target, by_rid),
            )
            for field, _label in RELATIONS
            if field != "supports"
            for target in as_list(requirement.get(field))
        )
        actual_links = collections.Counter(VISIBLE_RELATION_RE.findall(body))
        if actual_links != expected_links:
            errors.append(
                f"{destination}/{rid}: visible relation links differ: "
                f"actual={dict(actual_links)}, expected={dict(expected_links)}"
            )

        expected_evidence = collections.Counter(
            (evidence["rid"], rid)
            for evidence in requirements
            if rid in as_list(evidence.get("supports"))
        )
        actual_evidence = collections.Counter(EVIDENCE_OPEN_RE.findall(body))
        if actual_evidence != expected_evidence:
            errors.append(
                f"{destination}/{rid}: evidence differs: actual={dict(actual_evidence)}, "
                f"expected={dict(expected_evidence)}"
            )
        for evidence_rid, target in expected_evidence:
            opening = f"<!-- evidence: {evidence_rid} supports={target} -->"
            closing = f"<!-- /evidence: {evidence_rid} -->"
            if body.count(opening) != 1 or body.count(closing) != 1:
                continue
            start = body.find(opening) + len(opening)
            end = body.find(closing, start)
            evidence_body = body[start:end]
            visible = re.sub(r"<!--.*?-->", "", evidence_body, flags=re.S).strip()
            if end < start or not re.search(r"\w", visible, re.UNICODE):
                errors.append(
                    f"{destination}/{rid}: evidence {evidence_rid} has no substantive prose"
                )

    return errors


def check_destination_document(
    destination: str, repo: pathlib.Path = REPO
) -> list[str]:
    try:
        manifest = json.loads((repo / PIN_FILES[1]).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"could not read manifest: {exc}"]

    relative_path = DOCUMENT_PATHS[destination]
    path = repo / relative_path
    if not path.is_file():
        return [f"{destination}: document does not exist: {relative_path}"]
    section, errors = _document_section(destination, path.read_text())
    if section is not None:
        errors.extend(document_errors(destination, section, manifest, repo))
    return errors


def check_documents(repo: pathlib.Path = REPO) -> list[str]:
    errors = []
    for destination in DOCUMENT_PATHS:
        errors.extend(check_destination_document(destination, repo))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--out", type=pathlib.Path, required=True)
    generate.add_argument("--force", action="store_true")
    check = subparsers.add_parser("check")
    check.add_argument("--bundle", type=pathlib.Path, required=True)
    subparsers.add_parser("check-docs")
    args = parser.parse_args(argv)

    if args.command == "generate":
        if args.out.exists() and not args.force:
            print(f"bundle already exists: {args.out}; pass --force to replace it")
            return 2
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render_bundle())
        print(f"generated bundle: {args.out}")
        return 0

    checking_bundle = args.command == "check"
    errors = check_bundle(args.bundle) if checking_bundle else check_documents()
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print("bundle check passed" if checking_bundle else "document check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
