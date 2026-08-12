"""산출물의 코드 단정 후보에 근접 근거가 있고 그 근거가 실재하는지 검사한다.

matrix 규칙 4 — *"새 문서의 모든 코드 단정이 baseline 항목 id 또는 새 file:line 을 인용"*.
구조 게이트는 이 축을 보지 않는다(마커·관계·상태만 본다). 그래서 별도 게이트가 필요하다.

두 방향을 **함께** 본다. 하나만 있으면 뚫린다:

1. **근접성** — 코드 심볼(백틱)로 현재 동작을 단정하는 논리 단위는 그 단위 안에 근거를
   가져야 한다. RID 어딘가의 무관한 근거 하나로 모든 단정을 통과시키지 않는다.
2. **실재** — 인용한 `path:line` 이 **고정 commit 의 실제 파일·행**이어야 하고,
   `baseline <ID>` 는 baseline 파일에 실재해야 한다.
   ⛔ 지어낸 근거는 근거 없음보다 나쁘다 — 확인했다는 착시를 만든다.

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
BASELINE_ID = re.compile(r"\*{0,2}([A-F]\d+(?:-[a-z]+)?)\*{0,2}", re.I)
FILE_LINE = re.compile(
    r"`([^`\n]+?\.(?:py|swift|json|md|conf|plist|pbxproj|ya?ml|toml|sh|[mh])):"
    r"(\d+)(?:-(\d+))?`"
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
def _pinned_file_line_count(repo_key: str, relative_path: str) -> tuple[int | None, str | None]:
    lock = json.loads(LOCK.read_text())
    commit = lock["pinned_commit"][repo_key]
    root = IOS if repo_key == "ios" else REPO
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{relative_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or "git show failed"
    return len(result.stdout.splitlines()), None


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
