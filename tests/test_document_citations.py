"""산출물의 코드 단정 후보에 근접 근거가 있고 그 근거가 실재하는지 검사한다.

matrix 규칙 4 — *"새 문서의 모든 코드 단정이 baseline 항목 id 또는 새 file:line 을 인용"*.
구조 게이트는 이 축을 보지 않는다(마커·관계·상태만 본다). 그래서 별도 게이트가 필요하다.

세 방향을 **함께** 본다. 하나만 있으면 뚫린다:

1. **근접성** — 코드 심볼(백틱)로 현재 동작을 단정하는 논리 단위는 그 단위 안에 근거를
   가져야 한다. RID 어딘가의 무관한 근거 하나로 모든 단정을 통과시키지 않는다.
2. **실재** — 인용한 `path:line` 이 **고정 commit 의 실제 파일·행**이어야 하고,
   `baseline <ID>` 는 baseline 파일에 실재해야 한다.
   ⛔ 지어낸 근거는 근거 없음보다 나쁘다 — 확인했다는 착시를 만든다.
3. **링크 표면** — canonical 문서 전체의 Markdown `#L` 링크를 검사한다. RID 블록과
   백틱형만 보면 ADR-041 밖의 과거 ADR 링크가 전부 사각지대가 된다.

⚠️ 이 검사는 보수적 lint 다. 자연어의 모든 코드 단정이나 근거의 의미 적합성을 증명하지 않는다.
최종 완전성은 별도 의미 리뷰가 판정한다. 근거가 안 붙은 동안 이 테스트는 **빨강**이다.
"""
import functools
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import topic_migration_manifest as MIGRATION  # noqa: E402

IOS = MIGRATION.provenance_root("ios")
ARCHIVE = REPO / "TOPIC_ONLY_DELIVERY_CONTRACT.archive.md"
MANIFEST = REPO / "spec" / "topic-only-migration-manifest.json"
BASELINE = REPO / "spec" / "topic-only-baseline-facts.md"
LOCK = REPO / "spec" / "topic-only.lock.json"

DOCS = {
    "ADR": REPO / "DECISIONS.md",
    "HAND": REPO / "spec" / "topic-snapshot-handoff.md",
    "CLIENT": REPO / "spec" / "ios-topic-state-machine.md",
    "CUT": REPO / "spec" / "legacy-cutover.md",
    "LOAD": REPO / "spec" / "revalidation-and-load.md",
    "HEALTH": REPO / "spec" / "publisher-health-slo.md",
}

# 근거 두 형태. baseline 표시는 같은 논리 단위 안의 ID 목록을 연다.
BASELINE_MARK = re.compile(r"\bbaseline\b", re.I)
BASELINE_ID = re.compile(
    r"(?<![0-9A-Za-z])\*{0,2}([A-F]\d+(?:-[a-z]+)?)\*{0,2}(?![0-9A-Za-z-])",
    re.I,
)
FILE_LINE = re.compile(
    r"`((?:[^`\n\s:]+/)*[^`\n\s/:]+\.[^`\n\s/:]+):"
    r"(\d+)(?:-(\d+))?`"
)
# FILE_LINE과 독립된 더 넓은 구문 oracle. checked surface에서 두 추출 결과가 같아야,
# extractor를 특정 확장자 allowlist로 되돌려 인용 축 전체가 사라지는 회귀를 잡을 수 있다.
BACKTICK_LINE_CANDIDATE = re.compile(
    r"`([^`\n\s:]+\.[^`\n\s:]+):(\d+)(?:-(\d+))?`"
)
MARKDOWN_LINE_LINK = re.compile(
    r"\[([^\]\n]+)\]\(([^)\s]+)#L(\d+)(?:-L?(\d+))?\)"
)
SAME_REPO_BLOB_PREFIX = "https://github.com/Jay-Hong/exchange-rate/blob/"
SAME_REPO_BLOB = re.compile(
    re.escape(SAME_REPO_BLOB_PREFIX) + r"([0-9a-f]{40})/(.+)"
)
LABEL_LINE = re.compile(r":(\d+)(?:-(\d+))?")
LABEL_FILE_LINE = re.compile(
    r"([^`\s\[\]()]+?\.(?:py|swift|json|md|conf|plist|pbxproj|ya?ml|toml|sh|[mh])):"
    r"(\d+)(?:-(\d+))?"
)

# 코드 단정 후보 = 코드 span + 현재 구현을 가리키는 단서 + 서술 어미.
# 줄 단위로 보면 wrap 된 단정을 놓치므로 먼저 Markdown 논리 단위로 합친다.
CODE_SYMBOL = re.compile(r"`[^`\n]+`")
CURRENT_CUE = re.compile(
    r"현재|현행|실측|코드|구현|호출|기본값|사용처|직결|디코드|영속화|복원|"
    r"0건|없(?:다|음)|있(?:다|음)|받는다|보낸다|취소한다|등록한다"
)
ASSERTIVE = re.compile(
    r"(한다|된다|이다|없다|있다|받는다|보낸다|지운다|버린다|막는다|건다|쓴다|"
    r"나온다|간다|취소한다|복원한다|영속화된다)\b"
)

DESTS = sorted(DOCS)
MARKDOWN_LINK_DOCS = sorted(set(DOCS.values()))
EXPECTED_MARKDOWN_LINK_DOCS = {
    REPO / "DECISIONS.md",
    REPO / "spec/ios-topic-state-machine.md",
    REPO / "spec/legacy-cutover.md",
    REPO / "spec/publisher-health-slo.md",
    REPO / "spec/revalidation-and-load.md",
    REPO / "spec/topic-snapshot-handoff.md",
}
RID_BACKTICK_LOCATOR_INVENTORY = {
    "ADR": 32,
    "CLIENT": 24,
    "CUT": 36,
    "HAND": 19,
    "HEALTH": 8,
    "LOAD": 6,
}


def _doc(dest: str) -> str:
    path = DOCS[dest]
    if not path.is_file():
        pytest.fail(f"{dest} 산출물이 없다: {path.relative_to(REPO)}")
    text = path.read_text()
    if dest != "ADR":
        return text
    i = text.find("## ADR-041")
    if i < 0:
        pytest.fail("DECISIONS.md 에 ADR-041 구간이 없다")
    nxt = re.compile(r"^#{2,4}\s*ADR-(?!041)", re.M).search(text, i + 1)
    return text[i: nxt.start() if nxt else len(text)]


def _blocks(dest: str) -> dict[str, str]:
    text, out = _doc(dest), {}
    manifest = json.loads(MANIFEST.read_text())
    for b in manifest["blocks"]:
        for r in b.get("requirements", []):
            if r["destination"] != dest:
                continue
            o, c = f"<!-- rid: {r['rid']} -->", f"<!-- /rid: {r['rid']} -->"
            if o not in text or c not in text:
                pytest.fail(f"{dest}: {r['rid']} 구간이 없다")
            body = text.split(o, 1)[1].split(c, 1)[0]
            out[r["rid"]] = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    return out


def _logical_units(body: str) -> list[str]:
    """Wrapped prose, list items, and table rows become citation-sized units."""
    units = []
    for paragraph in re.split(r"\n\s*\n", body):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        current = []

        def flush() -> None:
            if current:
                units.append(" ".join(current))
                current.clear()

        for line in lines:
            if line.startswith("|"):
                flush()
                units.append(line)
            elif re.match(r"^(?:[-*+] |\d+[.)] )", line):
                flush()
                current.append(line)
            else:
                current.append(line)
        flush()
    return units


def _claim_units(body: str) -> list[str]:
    return [
        unit
        for unit in _logical_units(body)
        if CODE_SYMBOL.search(unit) and CURRENT_CUE.search(unit) and ASSERTIVE.search(unit)
    ]


def _baseline_refs(unit: str) -> set[str]:
    return set(BASELINE_ID.findall(unit)) if BASELINE_MARK.search(unit) else set()


def _has_provenance(unit: str) -> bool:
    return bool(_baseline_refs(unit) or FILE_LINE.search(unit))


def _repo_path(path: str) -> tuple[str, pathlib.Path, str]:
    """Return lock key, repository root, and path relative to that repository."""
    clean = path.removeprefix("exchange-rate/")
    if clean.startswith("ios/"):
        return "ios", IOS, clean.removeprefix("ios/")
    if clean.startswith(("FXi/", "FXiTests/", "FXi.xcodeproj/")):
        return "ios", IOS, clean
    return "server", REPO, clean


@functools.lru_cache(maxsize=None)
def _file_line_count_at_commit(
    repo_key: str,
    relative_path: str,
    commit: str,
) -> tuple[int | None, str | None]:
    root = IOS if repo_key == "ios" else REPO
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{relative_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or "git show failed"
    return len(result.stdout.splitlines()), None


def _pinned_file_line_count(repo_key: str, relative_path: str) -> tuple[int | None, str | None]:
    lock = json.loads(LOCK.read_text())
    return _file_line_count_at_commit(
        repo_key,
        relative_path,
        lock["pinned_commit"][repo_key],
    )


def _markdown_link_location(
    document: pathlib.Path,
    target: str,
) -> tuple[str, str, str] | str | None:
    """Return (repo key, relative path, commit), an error, or None for external links."""
    if target.startswith(SAME_REPO_BLOB_PREFIX):
        match = SAME_REPO_BLOB.fullmatch(target)
        if match is None:
            return "같은 저장소의 과거 blob 링크는 40자리 commit SHA로 고정해야 한다"
        return "server", match.group(2), match.group(1)
    if "://" in target:
        return None

    resolved = (document.parent / target).resolve()
    for repo_key, root in (("server", REPO), ("ios", IOS)):
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        lock = json.loads(LOCK.read_text())
        return repo_key, relative, lock["pinned_commit"][repo_key]
    return f"저장소 밖을 가리키는 로컬 링크다: {target}"


def _markdown_line_link_errors(document: pathlib.Path, text: str | None = None) -> list[str]:
    """Validate full-document Markdown line links against their declared commits."""
    errors = []
    body = document.read_text() if text is None else text
    for label, target, start, end in MARKDOWN_LINE_LINK.findall(body):
        location = _markdown_link_location(document, target)
        if location is None:
            continue
        if isinstance(location, str):
            errors.append(f"{label}: {location}")
            continue

        repo_key, relative_path, commit = location
        label_line = LABEL_LINE.search(label)
        label_file = LABEL_FILE_LINE.search(label)
        if label_file is not None:
            shown_path = label_file.group(1).removeprefix("exchange-rate/")
            if relative_path != shown_path and not relative_path.endswith("/" + shown_path):
                errors.append(
                    f"{label}: 표시 파일 `{shown_path}`과 링크 대상 `{relative_path}`가 다르다"
                )
        if label_line is not None and int(label_line.group(1)) != int(start):
            errors.append(
                f"{label}: 표시 시작행 {label_line.group(1)}과 링크 #L{start}가 다르다"
            )
        if (
            label_line is not None
            and label_line.group(2) is not None
            and end
            and int(label_line.group(2)) != int(end)
        ):
            errors.append(
                f"{label}: 표시 끝행 {label_line.group(2)}과 링크 끝행 L{end}가 다르다"
            )

        total, error = _file_line_count_at_commit(repo_key, relative_path, commit)
        if error:
            errors.append(f"{label}: `{relative_path}` 가 {commit[:12]}에 없음: {error}")
            continue
        last = int(end or start)
        if not (1 <= int(start) <= last <= total):  # type: ignore[operator]
            suffix = f"-L{end}" if end else ""
            errors.append(
                f"{label}: `{relative_path}#L{start}{suffix}` 범위 밖(총 {total}행)"
            )
    return errors


def _backtick_line_errors(text: str, *, context: str) -> list[str]:
    errors = []
    references = FILE_LINE.findall(text)
    if not references:
        return [f"{context}: backtick file:line 인용이 0건이다"]
    for path, start, end in references:
        repo_key, _root, relative_path = _repo_path(path)
        total, error = _pinned_file_line_count(repo_key, relative_path)
        if error:
            errors.append(f"{context}: `{path}` 가 pinned {repo_key} commit에 없음: {error}")
            continue
        last = int(end or start)
        if not (1 <= int(start) <= last <= total):  # type: ignore[operator]
            suffix = f"-{end}" if end else ""
            errors.append(f"{context}: `{path}:{start}{suffix}` 범위 밖(총 {total}행)")
    return errors


def _known_baseline_ids() -> set[str]:
    return set(
        re.findall(
            r"^\s*(?:-\s+)?\*\*([A-F]\d+(?:-[a-z]+)?)\b",
            BASELINE.read_text(),
            re.M,
        )
    )


@pytest.mark.parametrize("dest", DESTS)
def test_code_claim_candidates_carry_local_provenance(dest):
    """code_fact만 근접 근거를 요구한다. normative 분류는 원장이 명시한다."""
    entries = _ledger_by_key()
    errors = []
    for rid, body in _blocks(dest).items():
        for claim in _claim_units(body):
            key = (dest, rid, _unit_id(claim))
            entry = entries.get(key)
            if entry is None:
                errors.append(f"{rid}: 원장 분류 없음 — {claim[:80]}")
            elif entry.get("classification") == "code_fact" and not _has_provenance(claim):
                errors.append(f"{rid}: code_fact 근거 없음 — {claim[:80]}")
    assert not errors, f"{dest}: 코드 단정 후보 오류 {len(errors)}건\n" + "\n".join(
        "  " + item for item in errors
    )


@pytest.mark.parametrize("dest", DESTS)
def test_cited_file_lines_actually_exist(dest):
    """⛔ current worktree가 아니라 lock의 pinned commit에서 인용을 확인한다."""
    bad = []
    for rid, body in _blocks(dest).items():
        for path, start, end in FILE_LINE.findall(body):
            repo_key, _root, relative_path = _repo_path(path)
            total, error = _pinned_file_line_count(repo_key, relative_path)
            if error:
                bad.append(f"{rid}: `{path}` 가 pinned {repo_key} commit에 없음: {error}")
                continue
            last = int(end or start)
            if not (1 <= int(start) <= last <= total):  # type: ignore[operator]
                bad.append(f"{rid}: `{path}:{start}{'-'+end if end else ''}` 범위 밖(총 {total}행)")
    assert not bad, f"{dest}: pinned commit에 실재하지 않는 인용 {len(bad)}건\n" + "\n".join(
        "  " + item for item in bad
    )


def test_rid_backtick_locator_inventory_keeps_every_surface_visible():
    """RID 블록 하나나 저장소 축 전체가 사라져도 나머지 인용으로 숨지 못하게 한다."""
    references = [
        (dest, path)
        for dest in DESTS
        for body in _blocks(dest).values()
        for path, _start, _end in FILE_LINE.findall(body)
    ]
    by_destination = {
        dest: sum(reference_dest == dest for reference_dest, _path in references)
        for dest in DESTS
    }
    by_repo = {
        repo_key: sum(_repo_path(path)[0] == repo_key for _dest, path in references)
        for repo_key in ("server", "ios")
    }
    assert len(references) == 125
    assert by_destination == RID_BACKTICK_LOCATOR_INVENTORY
    assert by_repo == {"server": 61, "ios": 64}


def test_markdown_line_link_gate_covers_every_canonical_document():
    """현재 링크가 0건인 문서도 미래 링크가 생길 수 있으므로 검사 표면에서 빠지면 안 된다."""
    assert set(MARKDOWN_LINK_DOCS) == EXPECTED_MARKDOWN_LINK_DOCS
    assert len(MARKDOWN_LINK_DOCS) == 6


@pytest.mark.parametrize("document", MARKDOWN_LINK_DOCS, ids=lambda path: path.name)
def test_markdown_line_links_exist_in_declared_commits(document):
    """RID 밖까지 포함한 canonical 문서 전체의 Markdown line link를 검사한다."""
    errors = _markdown_line_link_errors(document)
    assert not errors, f"{document.name}: 잘못된 Markdown line link {len(errors)}건\n" + "\n".join(
        "  " + item for item in errors
    )


def test_baseline_backtick_line_references_exist_at_pinned_commits():
    """baseline은 RID 산출물이 아니므로 정본 파일 전체의 40개 locator를 별도로 검사한다."""
    errors = _backtick_line_errors(BASELINE.read_text(), context=BASELINE.name)
    assert not errors, f"baseline의 잘못된 backtick file:line 인용 {len(errors)}건\n" + "\n".join(
        "  " + item for item in errors
    )


def test_baseline_backtick_locator_inventory_keeps_both_repositories_visible():
    """부분적으로 약해진 extractor가 한 저장소 축을 통째로 숨기지 못하게 한다.

    0건 가드만 있으면 `FILE_LINE`에서 `swift`를 빠뜨려도 server 30건이 양성 대조군으로
    남아서 iOS 10건의 소실을 감춘다. baseline이 의도적으로 바뀔 때만 이 inventory를
    함께 검토해 갱신한다.
    """
    references = FILE_LINE.findall(BASELINE.read_text())
    by_repo = {
        repo_key: sum(_repo_path(path)[0] == repo_key for path, _start, _end in references)
        for repo_key in ("server", "ios")
    }
    # 2026-08-16 S0: E2 가 `to_thread` 의 **실제 위치**(공유 래퍼)와 REST twin 호출부를
    #                함께 인용하게 되며 server locator 2건 증가 (40 → 42).
    assert len(references) == 42
    assert by_repo == {"server": 32, "ios": 10}


def test_file_line_extractor_matches_extension_independent_oracle():
    """baseline과 RID에서 locator 모양인 모든 backtick을 FILE_LINE도 보아야 한다."""
    synthetic = "`contracts/example.future-extension:7-9`"
    assert FILE_LINE.findall(synthetic) == BACKTICK_LINE_CANDIDATE.findall(synthetic)

    surfaces = {BASELINE.name: BASELINE.read_text()}
    surfaces.update(
        {
            f"{dest}/{rid}": body
            for dest in DESTS
            for rid, body in _blocks(dest).items()
        }
    )
    for context, text in surfaces.items():
        assert FILE_LINE.findall(text) == BACKTICK_LINE_CANDIDATE.findall(text), (
            f"{context}: FILE_LINE 확장자 allowlist가 locator-shaped backtick을 놓쳤다"
        )


def test_baseline_backtick_line_gate_rejects_empty_and_bad_ranges():
    assert _backtick_line_errors("인용 없음", context="synthetic")
    assert not _backtick_line_errors("`app/main.py:1`", context="synthetic")
    errors = _backtick_line_errors(
        "`app/main.py:0` `app/main.py:2-1` `app/main.py:999999`",
        context="synthetic",
    )
    assert len(errors) == 3
    assert all("범위 밖" in error for error in errors)


def test_markdown_line_link_gate_rejects_bad_ranges_and_mutable_history():
    valid = "[app/main.py:1](app/main.py#L1)"
    assert not _markdown_line_link_errors(DOCS["ADR"], valid)
    assert not _markdown_line_link_errors(DOCS["ADR"], "line link 없음")

    bad = (
        "[app/main.py:2](app/main.py#L1)\n"
        "[app/main.py:1-3](app/main.py#L1-L2)\n"
        "[app/main.py:999999](app/main.py#L999999)\n"
        "[app/main.py:2-1](app/main.py#L2-L1)\n"
        "[app/not-main.py:1](app/main.py#L1)\n"
        "[app/main.py:1 @ short]"
        "(https://github.com/Jay-Hong/exchange-rate/blob/a562391/app/main.py#L1)"
    )
    errors = _markdown_line_link_errors(DOCS["ADR"], bad)
    assert len(errors) == 6
    assert any("표시 파일" in error for error in errors)
    assert any("표시 시작행" in error for error in errors)
    assert any("표시 끝행" in error for error in errors)
    assert sum("범위 밖" in error for error in errors) == 2
    assert any("40자리 commit SHA" in error for error in errors)


def test_markdown_line_link_gate_covers_decisions_before_adr_041():
    """`_doc("ADR")` 슬라이스로 되돌리면 과거 ADR 링크가 다시 사각지대가 된다."""
    text = DOCS["ADR"].read_text()
    before_adr_041 = text.split("## ADR-041", 1)[0]
    links = MARKDOWN_LINE_LINK.findall(before_adr_041)
    assert links, "ADR-041 이전 Markdown line link 양성 대조군이 없다"
    assert any("a499a08d210ab1e6b8c5309357eab3bcf1283fa5" in target for _, target, _, _ in links)
    assert not _markdown_line_link_errors(DOCS["ADR"], before_adr_041)


def test_reviewed_adr_030_and_031_targets_do_not_regress_to_stale_worktree_lines():
    """범위 존재만으로는 의미 적합성을 못 보므로, 확인한 세 교정 target을 정확히 잠근다."""
    text = DOCS["ADR"].read_text()
    reviewed_targets = {
        "https://github.com/Jay-Hong/exchange-rate/blob/"
        "a499a08d210ab1e6b8c5309357eab3bcf1283fa5/"
        "app/latest_rates_cache.py#L885-L890",
        "app/latest_rates_cache.py#L1993-L2016",
        "https://github.com/Jay-Hong/exchange-rate/blob/"
        "2d5c8adfd7a46944617c854d8221a5535bb1a95b/"
        "app/usdt_topic_payload.py#L326-L330",
        "https://github.com/Jay-Hong/exchange-rate/blob/"
        "0756329228d6048f986e6f759ac31037325bc41b/"
        "app/usdt_topic_payload.py#L327-L333",
    }
    targets = {target + "#L" + start + ("-L" + end if end else "")
               for _label, target, start, end in MARKDOWN_LINE_LINK.findall(text)}
    assert reviewed_targets <= targets
    assert "app/latest_rates_cache.py#L886" not in targets
    assert "app/usdt_topic_payload.py#L330" not in targets


@pytest.mark.parametrize("dest", DESTS)
def test_cited_baseline_ids_exist(dest):
    """baseline ID 는 baseline 파일에 실재해야 한다."""
    known = _known_baseline_ids()
    bad = [
        f"{rid}: baseline {cid}"
        for rid, body in _blocks(dest).items()
        for unit in _logical_units(body)
        for cid in _baseline_refs(unit)
        if cid not in known
    ]
    assert not bad, f"{dest}: baseline 에 없는 ID {len(bad)}건\n" + "\n".join("  " + x for x in bad)


def test_claim_lint_does_not_drop_bullets_or_wrapped_prose():
    bullet = "- 현재 `currentCall` 은 실행된다."
    wrapped = "현재 `currentCall` 은\n실행된다."
    assert _claim_units(bullet) == [bullet]
    assert _claim_units(wrapped) == ["현재 `currentCall` 은 실행된다."]


def test_one_list_items_basis_does_not_cover_a_sibling_claim():
    body = (
        "- 현재 `firstCall` 은 실행된다.\n"
        "- 현재 `secondCall` 은 실행된다. 근거: baseline A1"
    )
    claims = _claim_units(body)
    assert len(claims) == 2
    assert not _has_provenance(claims[0])
    assert _has_provenance(claims[1])


def test_unrelated_rid_level_basis_does_not_cover_a_claim():
    body = "현재 `currentCall` 은 실행된다.\n\n별도 설명의 근거: baseline A1"
    claim = _claim_units(body)
    assert len(claim) == 1
    assert not _has_provenance(claim[0])


def test_file_line_must_exist_in_pinned_commit_not_only_worktree():
    """⛔ helper 는 **worktree 가 아니라 pinned commit** 을 읽어야 한다.

    ⚠️ 예전에는 이 테스트 파일 자신을 예시로 썼는데, 재-baseline 으로 pin 이 그 파일을 포함하는
       commit 으로 옮겨가자 **음성 대조군이 무력화**됐다(601줄을 반환). pin 이 어디로 가든
       성립하도록, worktree 에만 존재하는 파일을 그때그때 만든다.
    """
    # repo-relative 경로여야 pinned lookup 에 넘길 수 있다. 고정 파일명은 사용자가 같은 이름의
    # 파일을 가지고 있을 때 덮어쓴 뒤 삭제하므로, 충돌 불가능한 임시파일을 repo 안에 만든다.
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=REPO,
        prefix="__pinned_commit_probe_",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write("worktree only\n")
        probe = pathlib.Path(handle.name)
    try:
        count, error = _pinned_file_line_count("server", probe.name)
    finally:
        probe.unlink(missing_ok=True)
    assert count is None, "worktree 에만 있는 파일이 pinned commit 에서 읽혔다"
    assert error


def test_baseline_ids_come_from_declarations_only():
    known = _known_baseline_ids()
    assert {"A1", "B3-op", "E2-inf", "F14"} <= known
    assert "F99" not in known


def test_baseline_refs_do_not_extract_an_id_from_a_commit_hash():
    unit = "commit `0cfe474`의 동작. 근거: baseline F12"
    assert _baseline_refs(unit) == {"F12"}


# ── 코드 단정 리뷰 원장 ───────────────────────────────────────────────────────
# ⛔ 원장은 **분류가 참임을 증명하지 않는다**. 코드 사실을 normative 로 잘못 적어도 기계는 모른다.
#    원장이 하는 일은 셋뿐이다 — ① 후보마다 **명시적 결정**을 강제 ② drift(누락·orphan·중복·문구 변경)
#    차단 ③ 감사 가능하게 기록. **분류의 진실성은 별도 상호 의미 교차검토가 판단한다.**
# ⛔ 한 단위에 현재 코드 사실과 규범이 섞였으면 `code_fact` 다. `normative` 로 분류하려면 문서에서
#    단위를 먼저 분리해야 한다. 이 판단도 기계가 증명하지 못하므로 상호 의미 교차검토 대상이다.
# ⛔ 그리고 후보 집합 자체가 완전하지 않다(보수적 lint). 최종 리뷰는 후보 **밖** 단정도 역방향으로 찾는다.
LEDGER_PATH = REPO / "spec" / "topic-only-code-claim-review.json"
LEDGER_SCHEMA_VERSION = 1
REASONS = {
    "design-decision",      # 무엇을 할지 정하는 문장 — 현재 코드 상태 단정이 아니다
    "requirement",          # 지켜야 할 계약 — 구현 여부와 별개
    "future-work",          # 아직 없는 것을 하겠다는 서술
}


def _norm(unit: str) -> str:
    return re.sub(r"\s+", " ", unit).strip()


def _unit_id(unit: str) -> str:
    return hashlib.sha256(_norm(unit).encode()).hexdigest()


def _candidate_map(items: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], str]:
    out = {}
    duplicates = []
    for dest, rid, unit in items:
        key = (dest, rid, _unit_id(unit))
        if key in out:
            duplicates.append(key)
        out[key] = unit
    if duplicates:
        rendered = ", ".join(f"{d}/{r}/{sha[:12]}" for d, r, sha in duplicates[:5])
        raise AssertionError(f"동일 후보 단위 중복 {len(duplicates)}건: {rendered}")
    return out


def _candidates() -> dict[tuple[str, str, str], str]:
    """(destination, rid, unit_sha) -> unit 원문. 동일 단위 반복은 거부한다."""
    items = []
    for dest in DESTS:
        for rid, body in _blocks(dest).items():
            for unit in _claim_units(body):
                items.append((dest, rid, unit))
    return _candidate_map(items)


def _expected_ledger_inputs() -> dict[str, str]:
    lock = json.loads(LOCK.read_text())
    return {
        "archive_sha256": hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(),
        "server_commit": lock["pinned_commit"]["server"],
        "ios_commit": lock["pinned_commit"]["ios"],
    }


def _expected_ledger_documents() -> dict[str, str]:
    """Bind the semantic review to all output prose, including non-candidate text."""
    return {
        destination: hashlib.sha256(_doc(destination).encode()).hexdigest()
        for destination in DESTS
    }


def _ledger_metadata_errors(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["최상위가 object 가 아님"]
    expected_fields = {"schema_version", "inputs", "documents", "entries"}
    errors = []
    if set(data) != expected_fields:
        errors.append(
            f"최상위 필드 actual={sorted(data)} expected={sorted(expected_fields)}"
        )
    if data.get("schema_version") != LEDGER_SCHEMA_VERSION:
        errors.append(
            f"schema_version={data.get('schema_version')!r} expected={LEDGER_SCHEMA_VERSION}"
        )
    expected_inputs = _expected_ledger_inputs()
    if data.get("inputs") != expected_inputs:
        errors.append("동결 입력이 현재 archive/manifest/baseline/lock 및 pinned commit 과 다름")
    if data.get("documents") != _expected_ledger_documents():
        errors.append("검토 문서 SHA가 현재 ADR-041/spec 5종과 다름")
    if not isinstance(data.get("entries"), list):
        errors.append("entries 는 배열이어야 함")
    return errors


def _ledger() -> list[dict]:
    if not LEDGER_PATH.is_file():
        pytest.fail(
            f"리뷰 원장이 없다: {LEDGER_PATH.relative_to(REPO)}\n"
            "후보 단위마다 code_fact / normative 를 명시해야 한다 — 분류 전이면 이 실패가 정상이다."
        )
    try:
        data = json.loads(LEDGER_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        pytest.fail(f"리뷰 원장을 읽을 수 없다: {exc}")
    metadata_errors = _ledger_metadata_errors(data)
    if metadata_errors:
        pytest.fail("리뷰 원장 메타데이터 오류\n" + "\n".join("  " + x for x in metadata_errors))
    entries = data["entries"]
    return entries


def _entry_key(entry: object) -> tuple[str, str, str] | None:
    if not isinstance(entry, dict):
        return None
    values = (entry.get("destination"), entry.get("rid"), entry.get("unit_sha256"))
    return values if all(isinstance(value, str) for value in values) else None


def _ledger_by_key() -> dict[tuple[str, str, str], dict]:
    return {
        key: entry
        for entry in _ledger()
        if (key := _entry_key(entry)) is not None and isinstance(entry, dict)
    }


def _provenance_tokens(unit: str) -> set[str]:
    tokens = {f"baseline:{cid}" for cid in _baseline_refs(unit)}
    for path, start, end in FILE_LINE.findall(unit):
        suffix = f"{start}-{end}" if end else start
        tokens.add(f"file:{path}:{suffix}")
    return tokens


def _ledger_coverage_errors(
    candidates: dict[tuple[str, str, str], str], entries: list[dict]
) -> list[str]:
    keys = [key for entry in entries if (key := _entry_key(entry)) is not None]
    duplicate = sorted({key for key in keys if keys.count(key) > 1})
    missing = sorted(set(candidates) - set(keys))
    orphan = sorted(set(keys) - set(candidates))
    errors = [f"중복 {d}/{r}/{sha[:12]}" for d, r, sha in duplicate]
    errors.extend(f"누락 {d}/{r}/{sha[:12]}" for d, r, sha in missing)
    errors.extend(f"orphan {d}/{r}/{sha[:12]}" for d, r, sha in orphan)
    return errors


def _ledger_shape_errors(entries: list[dict]) -> list[str]:
    errors = []
    common = {
        "destination",
        "rid",
        "unit_sha256",
        "unit_text",
        "classification",
        "detail",
    }
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"#{index}: object 아님")
            continue
        tag = f"#{index} {entry.get('destination')}/{entry.get('rid')}"
        cls = entry.get("classification")
        allowed = common | ({"provenance"} if cls == "code_fact" else {"reason"})
        if set(entry) != allowed:
            errors.append(f"{tag}: 필드가 계약과 다름 actual={sorted(entry)} expected={sorted(allowed)}")
        if entry.get("destination") not in DESTS:
            errors.append(f"{tag}: destination 오류")
        if not re.fullmatch(r"R-[A-Z]+-\d+", str(entry.get("rid", ""))):
            errors.append(f"{tag}: rid 오류")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("unit_sha256", ""))):
            errors.append(f"{tag}: unit_sha256 오류")
        if not isinstance(entry.get("unit_text"), str) or not entry["unit_text"].strip():
            errors.append(f"{tag}: unit_text 가 비었다")
        if cls not in {"code_fact", "normative"}:
            errors.append(f"{tag}: classification={cls!r}")
            continue
        if len(str(entry.get("detail") or "").strip()) < 10:
            errors.append(f"{tag}: detail 이 비었다")
        if cls == "code_fact":
            provenance = entry.get("provenance")
            if (
                not isinstance(provenance, list)
                or not provenance
                or any(not isinstance(item, str) or not item for item in provenance)
                or len(set(provenance)) != len(provenance)
            ):
                errors.append(f"{tag}: provenance 는 중복 없는 비어 있지 않은 문자열 배열이어야 한다")
        elif entry.get("reason") not in REASONS:
            errors.append(f"{tag}: reason={entry.get('reason')!r} (허용 {sorted(REASONS)})")
    return errors


def _ledger_provenance_errors(
    candidates: dict[tuple[str, str, str], str], entries: list[dict]
) -> list[str]:
    errors = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("classification") != "code_fact":
            continue
        key = _entry_key(entry)
        unit = candidates.get(key) if key is not None else None
        if unit is None:
            continue
        expected = _provenance_tokens(unit)
        actual = set(entry.get("provenance", [])) if isinstance(entry.get("provenance"), list) else set()
        if actual != expected:
            errors.append(
                f"{entry.get('destination')}/{entry.get('rid')}: provenance actual={sorted(actual)} "
                f"expected={sorted(expected)}"
            )
    return errors


def _ledger_unit_text_errors(
    candidates: dict[tuple[str, str, str], str], entries: list[dict]
) -> list[str]:
    errors = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = _entry_key(entry)
        unit = candidates.get(key) if key is not None else None
        if unit is None:
            continue
        if entry.get("unit_text") != _norm(unit):
            errors.append(
                f"{entry.get('destination')}/{entry.get('rid')}: unit_text 가 문서 후보 원문과 다름"
            )
    return errors


def test_ledger_covers_exactly_the_candidate_set():
    """⛔ 누락·orphan·중복을 모두 막는다 — 셋 중 하나만 빠져도 원장이 현실과 갈린다."""
    errors = _ledger_coverage_errors(_candidates(), _ledger())
    assert not errors, "원장 집합 오류 " + str(len(errors)) + "건\n" + "\n".join(
        "  " + error for error in errors[:12]
    )


def test_ledger_entries_are_well_formed():
    """classification 은 정확히 하나. code_fact 는 근거, normative 는 제한된 사유를 요구한다."""
    errors = _ledger_shape_errors(_ledger())
    assert not errors, "원장 항목 오류 " + str(len(errors)) + "건\n" + "\n".join(
        "  " + error for error in errors
    )


def test_code_fact_provenance_appears_in_its_own_unit():
    """원장의 canonical provenance 집합과 문서 단위에서 파싱한 집합이 정확히 같아야 한다."""
    errors = _ledger_provenance_errors(_candidates(), _ledger())
    assert not errors, "원장↔문서 근거 불일치 " + str(len(errors)) + "건\n" + "\n".join(
        "  " + error for error in errors
    )


def test_ledger_keeps_the_exact_reviewed_unit_text():
    """opaque SHA만 남기지 않는다. 사람이 원장만 열어도 분류 대상을 감사할 수 있어야 한다."""
    errors = _ledger_unit_text_errors(_candidates(), _ledger())
    assert not errors, "원장 후보 원문 오류 " + str(len(errors)) + "건\n" + "\n".join(
        "  " + error for error in errors
    )


def _sample_candidate_and_entries():
    unit = "현재 `sampleCall` 은 실행된다. 근거: baseline A1"
    key = ("ADR", "R-SAMPLE-1", _unit_id(unit))
    candidate = {key: unit}
    normative = {
        "destination": key[0],
        "rid": key[1],
        "unit_sha256": key[2],
        "unit_text": _norm(unit),
        "classification": "normative",
        "reason": "design-decision",
        "detail": "합성 양성 대조군의 설계 문장 분류다.",
    }
    code_fact = {
        "destination": key[0],
        "rid": key[1],
        "unit_sha256": key[2],
        "unit_text": _norm(unit),
        "classification": "code_fact",
        "detail": "합성 양성 대조군의 현재 코드 사실이다.",
        "provenance": ["baseline:A1"],
    }
    return candidate, normative, code_fact


def test_candidate_map_rejects_identical_units_in_one_rid():
    unit = "현재 `sameCall` 은 실행된다."
    with pytest.raises(AssertionError, match="동일 후보 단위 중복"):
        _candidate_map([("ADR", "R-SAMPLE-1", unit), ("ADR", "R-SAMPLE-1", unit)])


def test_ledger_helpers_cover_positive_and_drift_controls():
    candidate, normative, _code_fact = _sample_candidate_and_entries()
    assert not _ledger_shape_errors([normative])
    assert not _ledger_coverage_errors(candidate, [normative])
    assert _ledger_coverage_errors(candidate, [])
    assert _ledger_coverage_errors(candidate, [normative, normative])
    orphan = dict(normative, unit_sha256="0" * 64)
    assert _ledger_coverage_errors(candidate, [normative, orphan])
    assert not _ledger_unit_text_errors(candidate, [normative])
    assert _ledger_unit_text_errors(candidate, [dict(normative, unit_text="다른 문장")])


def test_ledger_metadata_pins_the_review_inputs():
    data = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "inputs": _expected_ledger_inputs(),
        "documents": _expected_ledger_documents(),
        "entries": [],
    }
    assert not _ledger_metadata_errors(data)
    changed = dict(data, inputs=dict(data["inputs"], lock_sha256="0" * 64))
    assert _ledger_metadata_errors(changed)
    changed = dict(data, documents=dict(data["documents"], ADR="0" * 64))
    assert _ledger_metadata_errors(changed)


def test_archive_quote_is_not_a_normative_exclusion_reason():
    _candidate, normative, _code_fact = _sample_candidate_and_entries()
    assert _ledger_shape_errors([dict(normative, reason="archive-quote")])


def test_code_fact_ledger_requires_exact_canonical_provenance():
    candidate, _normative, code_fact = _sample_candidate_and_entries()
    assert not _ledger_shape_errors([code_fact])
    assert not _ledger_provenance_errors(candidate, [code_fact])
    assert _ledger_provenance_errors(candidate, [dict(code_fact, provenance=["현재"])])


def test_normative_ledger_entry_is_wired_into_the_obligation_gate(monkeypatch):
    candidate, normative, _code_fact = _sample_candidate_and_entries()
    (_dest, rid, _sha), unit = next(iter(candidate.items()))
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_ledger", lambda: [normative])
    monkeypatch.setattr(module, "_blocks", lambda _destination: {rid: unit})
    test_code_claim_candidates_carry_local_provenance("ADR")
