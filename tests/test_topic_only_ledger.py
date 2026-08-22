"""이행 대장(`spec/topic-only-implementation-ledger.json`)이 **active 전량을 덮고, 닫힘을 근거로만 선언**하는지 검사한다.

⛔ 역할 분리 — 셋을 섞으면 "남은 일" 질문에 계속 틀린 답이 나온다.

  · 동결 manifest  = 구속력(disposition) · 소유(normative_owner) · 관계(references) 의 정본
  · 이 대장        = 작업 종류 · 완료 상태 · 검증 근거 의 정본
  · 제목 필터/참조 폐쇄 = **탐색용 힌트일 뿐 게이트가 아니다**

⛔ 파생 부분집합으로 "남은 일" 을 뽑으려던 시도가 실제로 실패했다(2026-08-11 실측):
   제목 필터 34 ∪ 참조 폐쇄 40 = 56 뿐이고 **active 20건이 양쪽 모두의 사각지대**였다
   (`R-LOAD-3`/`R-LOAD-4`/`R-CLI-24` 포함). 그래서 대장은 active 전량을 덮는다.

⛔ **이 파일의 1차 초안은 fail-open 이었다**(같은 날 외부 검토가 8건 재현). 그 교훈이 설계에 박혀 있다:

   · `evidence=["trust me"]` / `[True]` / `[0]` 이 verified 를 통과했다
     → 근거를 **구조화된 객체**로 받고 commit 변경 경로, test node, doc anchor 를 실제로 확인한다.
   · `status="not_required"` 로 active 를 임의 무효화할 수 있었고 **전량 무효화도 초록**이었다
     → `not_required` 를 없애고 `non_actionable`(사유 enum + evidence + note + reviewer)로 대체한다.
       이것은 구속력 취소가 아니라 **책임이 남는 분류**다.
   · `schema_version`/`status_values` 를 지워도, `work_kind` 에 임의 객체를 넣어도 통과했다
     → 최상위 스키마와 enum 을 검사한다.

⛔ 닫힌 상태는 **`verified` 하나뿐**이다. `done` 은 "구현했다는 판단" 이라 근거를 요구하되 닫힘이 아니다.
   `closed_statuses` 를 데이터로 두고 그 값까지 잠근다 — 나중에 조용히 넓히지 못하게.

`unreviewed` 를 기본값으로 허용하는 것은 의도다 — 76건 분류가 끝날 때까지 구현을 막지 않는다.
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import topic_migration_manifest as MIGRATION  # noqa: E402

IOS = MIGRATION.provenance_root("ios")
MANIFEST = REPO / "spec" / "topic-only-migration-manifest.json"
LEDGER = REPO / "spec" / "topic-only-implementation-ledger.json"
FULL_WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
DOC_WORKFLOW = REPO / ".github" / "workflows" / "topic-only-docs.yml"
TOPIC_ONLY_LOCK = REPO / "spec" / "topic-only.lock.json"
DOC_GATE_TESTS = {
    "test_adr041_grounds.py",
    "test_document_citations.py",
    "test_krx_deploy_verifier.py",
    "test_topic_migration_doc_bundle.py",
    "test_topic_migration_launcher.py",
    "test_topic_migration_validator.py",
    "test_topic_only_documents.py",
    "test_topic_only_ledger.py",
    "test_topic_only_semantic_review.py",
    "test_topic_wire.py",
    "test_ws_message_limit.py",
}
MARKDOWN_LITERAL_CANDIDATE_INVENTORY = {
    "test_adr041_grounds.py",
    "test_document_citations.py",
    "test_krx_deploy_verifier.py",
    "test_topic_c2.py",
    # 실사용 통합 시험 — 복제본 iOS 런북(.md)을 실제로 고친다.
    "test_topic_c2_integration.py",
    "test_topic_docs_semantic_drift.py",
    "test_topic_migration_doc_bundle.py",
    # 2026-08-21 canonical-only provenance 행동 시험이 임시 canonical.md RID 문서를 만든다.
    # workflow에는 이미 이 모듈이 명시돼 있으므로 inventory 축만 동기화한다.
    "test_topic_migration_validator.py",
    "test_topic_only_ledger.py",
    "test_topic_wire.py",
    "test_ws_message_limit.py",
}

STATUSES = {"unreviewed", "non_actionable", "todo", "in_progress", "done", "verified"}
ACTIONABLE_STATUSES = {"todo", "in_progress", "done", "verified"}
CLOSED = ["verified"]  # ⛔ 넓히려면 이 상수와 대장 양쪽을 고쳐야 한다(조용한 확장 차단)
WORK_KINDS = {"server_impl", "client_impl", "ops", "doc", "decision"}
NA_REASONS = {"context_statement", "recorded_correction", "resolved_open", "covered_by_other_rid"}
EVIDENCE_KINDS = {"commit", "test", "deploy", "doc"}
DEPLOY_ENVIRONMENTS = {"canary", "staging", "production"}
TOP_LEVEL_KEYS = {
    "schema_version", "manifest_sha256", "note", "closed_statuses", "rules",
    "status_values", "work_kinds", "non_actionable_reasons", "evidence_kinds",
    "deploy_environments", "entries",
}
ENTRY_KEYS = {
    "rid", "status", "owner", "block_title",
    "work_kind", "evidence", "non_actionable_reason", "reviewer", "note",
}
EVIDENCE_KEYS = {
    "commit": {"kind", "repo", "sha", "paths"},
    "test": {"kind", "repo", "node_id"},
    "deploy": {"kind", "environment", "reference", "repo", "sha"},
    "doc": {"kind", "path", "anchor"},
}
VERIFIED_EVIDENCE = {
    "server_impl": {"commit", "test"},
    "client_impl": {"commit", "test"},
    "ops": {"deploy"},
    "doc": {"doc"},
    "decision": {"doc"},
}
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEPLOY_REFERENCE_RE = re.compile(r"^(?:https://\S+|sha256:[0-9a-f]{64})$")
RID_REF_RE = re.compile(r"\bR-[A-Z]+-\d+\b", re.IGNORECASE)
_COMMIT_PATH_CACHE: dict[tuple[str, str], frozenset[str]] = {}
_COMMIT_RESOLVE_CACHE: set[tuple[str, str]] = set()
_PYTEST_NODE_CACHE: dict[tuple[str, int, int], frozenset[str]] = {}


def _manifest_active() -> dict[str, tuple[str | None, str]]:
    m = json.loads(MANIFEST.read_text())
    return {
        req["rid"]: (req.get("normative_owner"), block["title"])
        for block in m["blocks"] if block["disposition"] == "active"
        for req in block["requirements"]
    }


def _repo_root(repo_key: str) -> pathlib.Path:
    return IOS if repo_key == "ios" else REPO


def _safe_relative_path(value) -> str | None:
    """Return a normalized repository-relative POSIX path, or None."""
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        return None
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _worktree_file(root: pathlib.Path, value) -> pathlib.Path | None:
    relative = _safe_relative_path(value)
    if relative is None:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _commit_changed_paths(repo_key: str, sha: str) -> tuple[frozenset[str] | None, str | None]:
    """Resolve a full commit and return the paths it actually changed."""
    root = _repo_root(repo_key)
    key = (str(root.resolve()), sha)
    if key in _COMMIT_PATH_CACHE:
        return _COMMIT_PATH_CACHE[key], None

    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-p", sha],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if commit.returncode != 0:
        return None, commit.stderr.strip() or "git cat-file failed"

    parents = re.findall(r"^parent ([0-9a-f]{40})$", commit.stdout, re.M)
    for parent in parents:
        try:
            available = subprocess.run(
                ["git", "-C", str(root), "cat-file", "-e", f"{parent}^{{commit}}"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, str(exc)
        if available.returncode != 0:
            return None, f"commit parent {parent} 가 없다 — shallow checkout 을 확인하라"

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff-tree", "--root", "--no-commit-id",
             "--name-only", "-r", "-m", "-z", sha],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, os.fsdecode(result.stderr).strip() or "git diff-tree failed"
    paths = frozenset(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)
    _COMMIT_PATH_CACHE[key] = paths
    return paths, None


def _commit_resolves(repo_key: str, sha: str) -> bool:
    root = _repo_root(repo_key)
    key = (str(root.resolve()), sha)
    if key in _COMMIT_RESOLVE_CACHE:
        return True
    try:
        resolved = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            timeout=15,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    if resolved:
        _COMMIT_RESOLVE_CACHE.add(key)
    return resolved


def _pytest_nodes(relative_path: str) -> tuple[frozenset[str] | None, str | None]:
    """Collect the exact pytest nodes from one repository test file."""
    target = REPO / relative_path
    try:
        stat = target.stat()
    except OSError as exc:
        return None, str(exc)
    key = (relative_path, stat.st_mtime_ns, stat.st_size)
    if key in _PYTEST_NODE_CACHE:
        return _PYTEST_NODE_CACHE[key], None
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", relative_path, "--collect-only", "-q", "-p", "no:asyncio"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip() or "pytest collection failed"
    prefix = f"{relative_path}::"
    nodes = frozenset(line.strip() for line in result.stdout.splitlines() if line.startswith(prefix))
    _PYTEST_NODE_CACHE[key] = nodes
    return nodes, None


def _xctest_method_resolves(text: str, selector: str) -> tuple[bool, str | None]:
    """Resolve a source-level ``XCTestCase/testMethod`` locator.

    Linux CI cannot ask Xcode to enumerate tests. This deliberately checks a
    narrow source convention instead: a top-level XCTestCase class and an
    instance ``test*()`` declaration inside that class.
    """
    if selector.count("/") != 1:
        return False, "iOS selector 는 XCTestCase/testMethod 형식이어야 한다"
    class_name, symbol = selector.split("/", 1)
    identifier = r"[A-Za-z_][A-Za-z0-9_]*"
    if not re.fullmatch(identifier, class_name):
        return False, f"XCTestCase 이름이 Swift 식별자가 아니다 ({class_name})"
    if not re.fullmatch(r"test[A-Za-z0-9_]*", symbol):
        return False, f"XCTest 테스트 메서드 이름이 아니다 ({symbol}) — test 로 시작해야 한다"

    class_decl = re.search(
        rf"^(?:(?:@[A-Za-z_][A-Za-z0-9_.]*(?:\([^\n)]*\))?|"
        rf"public|internal|private|fileprivate|open|final)\s+)*"
        rf"class\s+{re.escape(class_name)}\s*:[^{{\n]*\bXCTestCase\b[^{{\n]*\{{",
        text,
        re.M,
    )
    if class_decl is None:
        return False, f"top-level XCTestCase 선언이 없다 ({class_name})"

    # Project test sources use top-level XCTestCase declarations whose closing
    # brace starts in column zero. Failing closed on another layout is safer
    # than accepting a method from a sibling or top-level scope.
    close = re.search(r"^}", text[class_decl.end():], re.M)
    if close is None:
        return False, f"XCTestCase 본문 경계를 찾지 못했다 ({class_name})"
    body = text[class_decl.end():class_decl.end() + close.start()]
    decl = re.search(
        rf"^[ \t]+(?P<modifiers>(?:(?:@[A-Za-z_][A-Za-z0-9_.]*(?:\([^\n)]*\))?|"
        rf"public|internal|final|override|private|fileprivate|static|class|nonisolated)\s+)*)"
        rf"func\s+{re.escape(symbol)}\s*\(\s*\)",
        body,
        re.M,
    )
    if decl is None:
        return False, f"해당 XCTestCase 안에 인자 없는 테스트 메서드가 없다 ({class_name}/{symbol})"
    forbidden = {"private", "fileprivate", "static", "class"}
    modifiers = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", decl.group("modifiers")))
    invalid = sorted(forbidden & modifiers)
    if invalid:
        return False, f"XCTest 인스턴스 테스트가 아닌 modifier 다 ({class_name}/{symbol}: {invalid})"
    return True, None


def _test_node_resolves(repo_key: str, node_id: str) -> tuple[bool, str | None]:
    # ⛔ pytest 는 file.py::Class::test_x 도 수집한다 — `::` 개수를 1개로 강제하면
    #    정상 클래스 노드를 거부한다(실측: TestKisFuturesClientMetrics::test_admin_endpoint_branches).
    if not isinstance(node_id, str) or "::" not in node_id:
        return False, "node_id 는 path::selector 형식이어야 한다"
    raw_path, selector = node_id.split("::", 1)
    relative_path = _safe_relative_path(raw_path)
    if relative_path is None or not selector:
        return False, "node_id 경로 또는 selector 가 올바르지 않다"

    root = _repo_root(repo_key)
    target = _worktree_file(root, relative_path)
    if target is None:
        return False, f"테스트 파일이 없다 ({relative_path})"

    if repo_key == "server":
        if not (relative_path.startswith("tests/") and relative_path.endswith(".py")):
            return False, "server test 는 tests/*.py 여야 한다"
        nodes, error = _pytest_nodes(relative_path)
        if nodes is None:
            return False, error
        if node_id not in nodes:
            return False, f"pytest node 가 수집되지 않는다 ({node_id})"
        return True, None

    if not (relative_path.startswith("FXiTests/") and relative_path.endswith(".swift")):
        return False, "ios test 는 FXiTests/*.swift 여야 한다"
    text = target.read_text(errors="replace")
    return _xctest_method_resolves(text, selector)


def _doc_anchor_resolves(path, anchor) -> tuple[bool, str | None, str | None]:
    relative_path = _safe_relative_path(path)
    if relative_path is None or not relative_path.endswith(".md"):
        return False, f"doc 근거는 저장소의 Markdown 파일이어야 한다 ({path!r})", None
    target = _worktree_file(REPO, relative_path)
    if target is None:
        return False, f"문서 파일이 없다 ({path!r})", None
    normalized = _text(anchor).removeprefix("#")
    if not normalized or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", normalized):
        return False, f"anchor 형식이 올바르지 않다 ({anchor!r})", None
    text = target.read_text(errors="replace")
    escaped = re.escape(normalized)
    explicit = bool(
        re.search(rf"<a\b[^>]*\bid=(?:\"{escaped}\"|'{escaped}')[^>]*>", text)
        or re.search(rf"<!--\s*rid:\s*{escaped}\s*-->", text)
    )
    evidence_supports = {
        match.upper()
        for match in re.findall(
            rf"<!--\s*evidence:\s*{escaped}\s+supports=(R-[A-Z]+-\d+)\s*-->",
            text,
            re.IGNORECASE,
        )
    }
    if not explicit and not evidence_supports:
        return False, f"명시적 Markdown anchor 가 없다 ({path}#{normalized})", None

    scopes = set(evidence_supports)
    anchor_rid = _canonical_rid_anchor(normalized)
    if explicit and anchor_rid is not None:
        scopes.add(anchor_rid)
    if not scopes:
        return False, f"doc anchor 가 RID 범위를 선언하지 않는다 ({path}#{normalized})", None
    if len(scopes) != 1:
        return False, f"doc anchor 의 RID 범위가 모호하다 ({path}#{normalized}: {sorted(scopes)})", None
    return True, None, next(iter(scopes))


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


RID_ANCHOR_RE = re.compile(r"^R-[A-Z]+-\d+$", re.IGNORECASE)


def _canonical_rid_anchor(value) -> str | None:
    normalized = _text(value).removeprefix("#")
    return normalized.upper() if RID_ANCHOR_RE.fullmatch(normalized) else None


def _evidence_rid_scopes(evidence) -> set[str]:
    if not isinstance(evidence, list):
        return set()
    scopes: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") != "doc":
            continue
        resolved, _, scoped_rid = _doc_anchor_resolves(item.get("path"), item.get("anchor"))
        if resolved and scoped_rid is not None:
            scopes.add(scoped_rid)
    return scopes


def _check_evidence(
    rid: str,
    evidence,
    fail: list[str],
    *,
    allowed_other_rids: set[str] | None = None,
) -> tuple[set[str], dict[str, set[str]]]:
    """근거 목록을 검사하고 종류별 해석 검사를 통과한 집합을 돌려준다."""
    kinds: set[str] = set()
    repos: dict[str, set[str]] = {}
    if not isinstance(evidence, list):
        fail.append(f"[E_SHAPE] {rid}: evidence 가 배열이 아니다")
        return kinds, repos
    for item in evidence:
        if not isinstance(item, dict):
            fail.append(f"[E_EVSHAPE] {rid}: 근거가 객체가 아니다 ({item!r}) — 문자열 근거는 검증 불가")
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in EVIDENCE_KINDS:
            fail.append(f"[E_EVKIND] {rid}: 알 수 없는 근거 종류 {kind!r}")
            continue
        if set(item) != EVIDENCE_KEYS[kind]:
            fail.append(
                f"[E_EVSCHEMA] {rid}: {kind} 근거 필드가 다르다 "
                f"{sorted(set(item) ^ EVIDENCE_KEYS[kind])}"
            )
            continue
        if kind == "commit":
            repo, sha, paths = item.get("repo"), item.get("sha"), item.get("paths")
            if repo not in {"server", "ios"}:
                fail.append(f"[E_EVFIELD] {rid}: commit 근거의 repo 가 server|ios 가 아니다 ({repo!r})")
            elif not (isinstance(sha, str) and FULL_SHA_RE.fullmatch(sha)):
                fail.append(f"[E_EVFIELD] {rid}: commit 근거는 full 40자 sha 여야 한다 ({sha!r})")
            elif not isinstance(paths, list) or not paths or len(paths) != len(set(map(str, paths))):
                fail.append(f"[E_EVFIELD] {rid}: commit 근거의 paths 는 중복 없는 비어 있지 않은 배열이어야 한다")
            elif not _commit_resolves(repo, sha):
                fail.append(f"[E_EVRESOLVE] {rid}: commit {sha} 가 {repo} 저장소에서 resolve 되지 않는다")
            else:
                safe_paths = [_safe_relative_path(path) for path in paths]
                if any(path is None for path in safe_paths):
                    fail.append(f"[E_EVFIELD] {rid}: commit 근거에 안전하지 않은 path 가 있다 ({paths!r})")
                    continue
                changed, error = _commit_changed_paths(repo, sha)
                missing = set(safe_paths) - set(changed or ())
                if changed is None:
                    fail.append(f"[E_EVRESOLVE] {rid}: commit 변경 경로를 읽지 못했다 ({error})")
                elif missing:
                    fail.append(f"[E_EVRELATION] {rid}: commit 이 paths 를 변경하지 않았다 ({sorted(missing)})")
                else:
                    kinds.add("commit")
                    repos.setdefault("commit", set()).add(repo)
        elif kind == "test":
            repo, node = item.get("repo"), item.get("node_id")
            if repo not in {"server", "ios"}:
                fail.append(f"[E_EVFIELD] {rid}: test 근거의 repo 가 server|ios 가 아니다 ({repo!r})")
            else:
                resolved, error = _test_node_resolves(repo, node)
                if not resolved:
                    fail.append(f"[E_EVRESOLVE] {rid}: test 근거를 해석하지 못했다 ({error})")
                else:
                    kinds.add("test")
                    repos.setdefault("test", set()).add(repo)
        elif kind == "deploy":
            environment = item.get("environment")
            reference, repo, sha = item.get("reference"), item.get("repo"), item.get("sha")
            if environment not in DEPLOY_ENVIRONMENTS:
                fail.append(f"[E_EVFIELD] {rid}: deploy environment 가 enum 이 아니다 ({environment!r})")
            elif repo not in {"server", "ios"}:
                fail.append(f"[E_EVFIELD] {rid}: deploy 근거의 repo 가 server|ios 가 아니다 ({repo!r})")
            elif not (isinstance(sha, str) and FULL_SHA_RE.fullmatch(sha) and _commit_resolves(repo, sha)):
                fail.append(f"[E_EVRESOLVE] {rid}: deploy 근거의 full sha 가 저장소에서 resolve 되지 않는다 ({sha!r})")
            elif not (isinstance(reference, str) and DEPLOY_REFERENCE_RE.fullmatch(reference)):
                fail.append(f"[E_EVFIELD] {rid}: deploy reference 는 https URL 또는 sha256 digest 여야 한다")
            else:
                kinds.add("deploy")
                repos.setdefault("deploy", set()).add(repo)
        elif kind == "doc":
            resolved, error, scoped_rid = _doc_anchor_resolves(item.get("path"), item.get("anchor"))
            allowed_rids = {rid} | set(allowed_other_rids or ())
            if not resolved:
                fail.append(f"[E_EVRESOLVE] {rid}: doc 근거를 해석하지 못했다 ({error})")
            elif scoped_rid not in allowed_rids:
                fail.append(
                    f"[E_EVSCOPE] {rid}: doc 근거가 다른 RID 를 지원한다 "
                    f"({scoped_rid}) — covered_by_other_rid 대상이어야 한다"
                )
            else:
                kinds.add("doc")
    return kinds, repos


def validate(ledger: dict, active: dict[str, tuple[str | None, str]], manifest_sha: str) -> list[str]:
    """대장 하나를 검사해 위반 목록을 돌려준다. 반례 시험이 이 함수를 직접 호출한다."""
    fail: list[str] = []

    if not isinstance(ledger, dict):
        return [f"[E_SHAPE] 최상위가 객체가 아니다 ({ledger!r})"]
    if set(ledger) != TOP_LEVEL_KEYS:
        fail.append(f"[E_TOPSHAPE] 최상위 필드 집합이 다르다 {sorted(set(ledger) ^ TOP_LEVEL_KEYS)}")
    if ledger.get("schema_version") != 1:
        fail.append(f"[E_SCHEMA] schema_version 이 1 이 아니다 ({ledger.get('schema_version')!r})")
    if ledger.get("manifest_sha256") != manifest_sha:
        fail.append("[E_MANIFEST_DRIFT] manifest 가 대장이 기록한 것과 다르다 — 대장을 다시 검토하라")
    if ledger.get("closed_statuses") != CLOSED:
        fail.append(f"[E_CLOSED] 닫힘 상태 집합이 {CLOSED} 가 아니다 — 조용히 넓힐 수 없다")
    if not _text(ledger.get("note")):
        fail.append("[E_TOPTEXT] note 가 비어 있다")
    rules = ledger.get("rules")
    if not isinstance(rules, list) or not rules or any(not _text(rule) for rule in rules):
        fail.append("[E_TOPTEXT] rules 는 비어 있지 않은 문자열 배열이어야 한다")
    for key, expected, code in (
        ("status_values", STATUSES, "E_STATUSDOC"),
        ("work_kinds", WORK_KINDS, "E_KINDDOC"),
        ("non_actionable_reasons", NA_REASONS, "E_REASONDOC"),
        ("evidence_kinds", EVIDENCE_KINDS, "E_EVDOC"),
        ("deploy_environments", DEPLOY_ENVIRONMENTS, "E_DEPLOYDOC"),
    ):
        got = ledger.get(key)
        if (
            not isinstance(got, dict)
            or set(got) != expected
            or any(not _text(description) for description in got.values())
        ):
            fail.append(f"[{code}] {key} 가 코드의 집합과 다르다")

    entries = ledger.get("entries")
    if not isinstance(entries, list):
        return fail + ["[E_SHAPE] entries 가 배열이 아니다"]

    entry_by_rid = {
        entry.get("rid"): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("rid"), str)
    }
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, dict):
            fail.append(f"[E_SHAPE] entry 가 객체가 아니다: {e!r}")
            continue
        rid = e.get("rid")
        if not isinstance(rid, str):
            fail.append(f"[E_RID] rid 가 문자열이 아니다 ({rid!r})")
            continue
        if set(e) != ENTRY_KEYS:
            fail.append(f"[E_SHAPE] {rid}: 필드 집합이 다르다 {sorted(set(e) ^ ENTRY_KEYS)}")
        if rid in seen:
            fail.append(f"[E_DUP] {rid} 이 두 번 나온다")
        seen.add(rid)

        if rid not in active:
            fail.append(f"[E_FOREIGN] {rid} 은 manifest 의 active 요구가 아니다")
        else:
            own, title = active[rid]
            if e.get("owner") != own:
                fail.append(f"[E_OWNER] {rid}: owner 가 manifest 와 다르다 ({e.get('owner')!r} != {own!r})")
            if e.get("block_title") != title:
                fail.append(f"[E_TITLE] {rid}: block_title 이 manifest 와 다르다")

        # ⛔ 해시 불가능한 값(dict/list)이 오면 `in <set>` 이 TypeError 로 **죽는다**.
        #    크래시는 거부가 아니다 — 먼저 문자열인지 본다(반례가 실제로 잡아냈다).
        status = e.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            fail.append(f"[E_STATUS] {rid}: 알 수 없는 status {status!r}")
            continue

        work_kind = e.get("work_kind")
        if work_kind is not None and (not isinstance(work_kind, str) or work_kind not in WORK_KINDS):
            fail.append(f"[E_WORKKIND] {rid}: 알 수 없는 work_kind {work_kind!r}")
        reason, reviewer = e.get("non_actionable_reason"), e.get("reviewer")
        note_rids = (
            {match.upper() for match in RID_REF_RE.findall(_text(e.get("note")))}
            if status == "non_actionable" and reason == "covered_by_other_rid"
            else set()
        )
        covered = (note_rids & set(active)) - {rid}
        kinds, evidence_repos = _check_evidence(
            rid,
            e.get("evidence"),
            fail,
            allowed_other_rids=covered,
        )

        if status == "unreviewed":
            if work_kind is not None or e.get("evidence") or reason is not None or reviewer is not None:
                fail.append(f"[E_UNREVIEWED] {rid}: unreviewed 인데 분류·근거가 붙어 있다")
        elif status == "non_actionable":
            # ⛔ manifest 의 active 구속력을 취소하는 것이 아니라, 책임이 남는 분류다.
            if work_kind is not None:
                fail.append(f"[E_NAKIND] {rid}: non_actionable 인데 work_kind 가 붙어 있다")
            if not isinstance(reason, str) or reason not in NA_REASONS:
                fail.append(f"[E_NAREASON] {rid}: non_actionable 사유가 enum 이 아니다 ({reason!r})")
            if not kinds:
                fail.append(f"[E_NAEVIDENCE] {rid}: non_actionable 인데 확인 가능한 근거가 없다")
            if not _text(e.get("note")):
                fail.append(f"[E_NANOTE] {rid}: non_actionable 인데 이유 설명이 없다")
            if not _text(reviewer):
                fail.append(f"[E_NAREVIEWER] {rid}: non_actionable 인데 검토자가 없다")
            if reason == "covered_by_other_rid":
                foreign = note_rids - set(active)
                if foreign:
                    fail.append(f"[E_NACOVER] {rid}: note 에 active 가 아닌 RID 가 있다 ({sorted(foreign)})")
                if not covered:
                    fail.append(f"[E_NACOVER] {rid}: covered_by_other_rid note 에 다른 active RID 가 없다")
                else:
                    missing_covered_evidence = covered - _evidence_rid_scopes(e.get("evidence"))
                    if missing_covered_evidence:
                        fail.append(
                            f"[E_NACOVEREVIDENCE] {rid}: note 의 covered RID 를 가리키는 doc 근거가 없다 "
                            f"({sorted(missing_covered_evidence)})"
                        )
                    inactive_targets = sorted(
                        target for target in covered
                        if entry_by_rid.get(target, {}).get("status") not in ACTIONABLE_STATUSES
                    )
                    if inactive_targets:
                        fail.append(
                            f"[E_NACOVERTARGET] {rid}: covered target 이 실제 작업 상태가 아니다 "
                            f"({inactive_targets})"
                        )
        else:
            if work_kind is None:
                fail.append(f"[E_NOKIND] {rid}: {status} 인데 work_kind 가 없다")
            if reason is not None:
                fail.append(f"[E_NAMISUSE] {rid}: {status} 인데 non_actionable 사유가 붙어 있다")
            if status in {"done", "verified"} and not kinds:
                fail.append(f"[E_NOEVIDENCE] {rid}: {status} 인데 확인된 근거가 없다")
            if status == "verified":
                if not _text(reviewer):
                    fail.append(f"[E_VERIFYREVIEWER] {rid}: verified 인데 의미 적합성을 확인한 검토자가 없다")
                required = VERIFIED_EVIDENCE.get(work_kind, set()) if isinstance(work_kind, str) else set()
                missing = required - kinds
                if missing:
                    fail.append(
                        f"[E_VERIFIEDEVIDENCE] {rid}: {work_kind} verified 인데 "
                        f"{sorted(missing)} 근거가 없다"
                    )
                expected_repo = (
                    {"server_impl": "server", "client_impl": "ios"}.get(work_kind)
                    if isinstance(work_kind, str)
                    else None
                )
                if expected_repo is not None:
                    wrong_repo_kinds = [
                        kind for kind in ("commit", "test")
                        if kind in kinds and expected_repo not in evidence_repos.get(kind, set())
                    ]
                    if wrong_repo_kinds:
                        fail.append(
                            f"[E_EVIDENCEREPO] {rid}: {work_kind} verified 근거가 "
                            f"{expected_repo} 저장소의 {wrong_repo_kinds} 가 아니다"
                        )

    for rid in sorted(set(active) - seen):
        fail.append(f"[E_MISSING] {rid} 이 대장에 없다 — 대장은 active 전량을 덮어야 한다")
    return fail


@pytest.fixture(scope="module")
def loaded():
    if not LEDGER.is_file():
        pytest.fail(f"이행 대장이 없다: {LEDGER.relative_to(REPO)}")
    return (
        json.loads(LEDGER.read_text()),
        _manifest_active(),
        hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
    )


def _mutate(loaded, fn):
    ledger, active, sha = loaded
    copy = json.loads(json.dumps(ledger))
    fn(copy)
    return validate(copy, active, sha)


def test_ledger_is_clean(loaded):
    ledger, active, sha = loaded
    fail = validate(ledger, active, sha)
    assert not fail, "이행 대장 위반:\n" + "\n".join(f"  {x}" for x in fail)


def test_ledger_covers_every_active_requirement(loaded):
    """부분집합이 아니라 전량이어야 한다 — 사각지대 20건이 생긴 이유가 이것이다."""
    ledger, active, _ = loaded
    assert {e["rid"] for e in ledger["entries"]} == set(active)


def test_only_verified_is_closed():
    assert CLOSED == ["verified"], "닫힘은 verified 하나뿐 — done 은 닫힘이 아니다"


# ── 반례 — 검사가 공허하지 않음을 증명한다 ─────────────────────────────────────

def _impl(**over):
    base = {"status": "verified", "work_kind": "server_impl", "non_actionable_reason": None,
            "reviewer": "schema-test-reviewer", "note": "", "evidence": []}
    return lambda d: d["entries"][0].update({**base, **over})


def _entry(ledger: dict, rid: str) -> dict:
    return next(entry for entry in ledger["entries"] if entry["rid"] == rid)


def _covered_by(ledger: dict, target: str) -> None:
    ledger["entries"][0].update(
        status="non_actionable",
        work_kind=None,
        evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": target}],
        non_actionable_reason="covered_by_other_rid",
        reviewer="jay",
        note=f"{target} 이행에 포함되어 별도 작업 단위가 아니다.",
    )
    _entry(ledger, target).update(status="todo", work_kind="client_impl")


SERVER_SHA = "5478c3af26f029a3bd5034f5e142b426756fe2c5"
IOS_SHA = "8aadc2fb66be926a809d6e1bc5dff42951f15a7a"
REAL_COMMIT = {
    "kind": "commit", "repo": "server", "sha": SERVER_SHA,
    "paths": ["tests/test_topic_migration_validator.py"],
}
REAL_TEST = {
    "kind": "test", "repo": "server",
    "node_id": "tests/test_topic_only_ledger.py::test_ledger_is_clean",
}
REAL_IOS_COMMIT = {
    "kind": "commit", "repo": "ios", "sha": IOS_SHA,
    "paths": ["FXiTests/TetherDataPathTests.swift"],
}
REAL_IOS_TEST = {
    "kind": "test", "repo": "ios",
    "node_id": (
        "FXiTests/TetherDataPathTests.swift::"
        "TetherDataPathTests/testFetchTetherSnapshot_404_throwsInvalidResponse"
    ),
}
REAL_DEPLOY = {
    "kind": "deploy", "environment": "production",
    "reference": "https://github.com/Jay-Hong/exchange-rate/actions/runs/31468582425",
    "repo": "server", "sha": SERVER_SHA,
}
REAL_DOC = {"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "R-CLI-1"}
COVERED_DOC = {"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "R-CLI-2"}


@pytest.mark.parametrize(
    "code,mutation",
    [
        # 커버리지
        ("E_MISSING", lambda d: d["entries"].pop()),
        ("E_FOREIGN", lambda d: d["entries"].append({**d["entries"][0], "rid": "R-NOPE-1"})),
        ("E_DUP", lambda d: d["entries"].append(dict(d["entries"][0]))),
        ("E_SHAPE", lambda d: d["entries"][0].pop("evidence")),
        # 최상위 스키마 (1차 초안이 전부 놓쳤던 것)
        ("E_SCHEMA", lambda d: d.pop("schema_version")),
        ("E_TOPSHAPE", lambda d: d.pop("rules")),
        ("E_TOPSHAPE", lambda d: d.update(escape_hatch=True)),
        ("E_TOPTEXT", lambda d: d.update(note="")),
        ("E_TOPTEXT", lambda d: d.update(rules=[""])),
        ("E_STATUSDOC", lambda d: d.pop("status_values")),
        ("E_STATUSDOC", lambda d: d.update(status_values={k: None for k in d["status_values"]})),
        ("E_KINDDOC", lambda d: d.pop("work_kinds")),
        ("E_EVDOC", lambda d: d.pop("evidence_kinds")),
        ("E_DEPLOYDOC", lambda d: d.pop("deploy_environments")),
        ("E_MANIFEST_DRIFT", lambda d: d.update(manifest_sha256="0" * 64)),
        ("E_CLOSED", lambda d: d.update(closed_statuses=["verified", "done"])),
        # manifest 정합
        ("E_OWNER", lambda d: d["entries"][0].update(owner="누군가")),
        ("E_TITLE", lambda d: d["entries"][0].update(block_title="딴 절")),
        ("E_STATUS", lambda d: d["entries"][0].update(status="대충끝남")),
        ("E_WORKKIND", lambda d: _impl(work_kind={"아무": "객체"},
                                       evidence=[REAL_COMMIT, REAL_TEST])(d)),
        # 근거 — 1차 초안의 fail-open 8건이 여기 박혀 있다
        ("E_EVSHAPE", _impl(evidence=["trust me"])),
        ("E_EVSHAPE", _impl(evidence=[True])),
        ("E_EVSHAPE", _impl(evidence=[0])),
        ("E_EVKIND", _impl(evidence=[{"kind": "느낌"}])),
        ("E_EVSCHEMA", _impl(evidence=[{"kind": "commit", "repo": "server", "sha": SERVER_SHA}])),
        ("E_EVSCHEMA", _impl(evidence=[{**REAL_TEST, "extra": True}])),
        ("E_EVFIELD", _impl(evidence=[
            {"kind": "commit", "repo": "server", "sha": "5478c3a", "paths": ["x"]}])),
        ("E_EVFIELD", _impl(evidence=[
            {"kind": "commit", "repo": "아무데나", "sha": SERVER_SHA, "paths": ["x"]}])),
        ("E_EVRESOLVE", _impl(evidence=[
            {"kind": "commit", "repo": "server", "sha": "deadbeef" * 5, "paths": ["x"]}])),
        ("E_EVRELATION", _impl(evidence=[{**REAL_COMMIT, "paths": ["DECISIONS.md"]}])),
        ("E_EVRESOLVE", _impl(evidence=[
            {"kind": "test", "repo": "server", "node_id": "tests/없는파일.py::t"}])),
        ("E_EVRESOLVE", _impl(evidence=[
            {"kind": "test", "repo": "server", "node_id": "tests/test_topic_only_ledger.py::없는테스트"}])),
        ("E_EVRESOLVE", _impl(evidence=[
            {"kind": "test", "repo": "server", "node_id": "CLAUDE.md::not_a_test"}])),
        # iOS — 파일 안 아무 func 이나 근거로 인정하면 안 된다(private helper 실측 사례)
        ("E_EVRESOLVE", _impl(work_kind="client_impl", evidence=[REAL_COMMIT,
            {"kind": "test", "repo": "ios",
             "node_id": "FXiTests/TopicSnapshotMergerTests.swift::TopicSnapshotMergerTests/t"}])),
        ("E_EVRESOLVE", _impl(work_kind="client_impl", evidence=[REAL_COMMIT,
            {"kind": "test", "repo": "ios",
             "node_id": "FXiTests/TetherDataPathTests.swift::TetherDataPathTests/setUp"}])),
        ("E_EVRESOLVE", _impl(evidence=[{"kind": "doc", "path": "없는문서.md", "anchor": "§1"}])),
        ("E_EVRESOLVE", _impl(evidence=[{"kind": "doc", "path": "DECISIONS.md", "anchor": "없는-anchor"}])),
        ("E_EVRESOLVE", _impl(evidence=[{"kind": "doc", "path": "spec", "anchor": "anything"}])),
        ("E_EVRESOLVE", _impl(evidence=[{"kind": "doc", "path": "../ios/README.md", "anchor": "readme"}])),
        ("E_EVRESOLVE", _impl(work_kind="doc", evidence=[
            {"kind": "doc", "path": "templates/admin.html", "anchor": "cpu-card"}])),
        ("E_EVRESOLVE", _impl(work_kind="doc", evidence=[
            {"kind": "doc", "path": "app/scheduler.py", "anchor": "websocket_broadcast"}])),
        ("E_EVFIELD", _impl(evidence=[{**REAL_DEPLOY, "environment": "prod"}])),
        ("E_EVFIELD", _impl(evidence=[{**REAL_DEPLOY, "reference": "trust me"}])),
        ("E_EVRESOLVE", _impl(evidence=[{**REAL_DEPLOY, "sha": "deadbeef" * 5}])),
        ("E_NOEVIDENCE", _impl(status="done", evidence=[])),
        ("E_NOEVIDENCE", _impl(evidence=[])),
        ("E_VERIFIEDEVIDENCE", _impl(evidence=[REAL_COMMIT])),      # test 근거 없음
        ("E_VERIFIEDEVIDENCE", _impl(evidence=[REAL_TEST])),        # commit 근거 없음
        ("E_VERIFIEDEVIDENCE", _impl(work_kind="ops", evidence=[REAL_COMMIT])),
        ("E_VERIFIEDEVIDENCE", _impl(work_kind="doc", evidence=[REAL_COMMIT])),
        ("E_VERIFYREVIEWER", _impl(reviewer=None, evidence=[REAL_COMMIT, REAL_TEST])),
        # 구속력 무효화 차단 — 1차 초안 최악의 구멍
        ("E_NAREASON", lambda d: d["entries"][0].update(status="non_actionable", note="그냥 안 함")),
        ("E_NANOTE", lambda d: d["entries"][0].update(
            status="non_actionable", non_actionable_reason="context_statement", reviewer="jay")),
        ("E_NAREVIEWER", lambda d: d["entries"][0].update(
            status="non_actionable", evidence=[REAL_DOC],
            non_actionable_reason="context_statement", note="배경 서술")),
        ("E_NAEVIDENCE", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None, evidence=[], non_actionable_reason="context_statement",
            reviewer="jay", note="배경 서술")),
        ("E_NAKIND", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind="server_impl", evidence=[REAL_DOC],
            non_actionable_reason="context_statement", reviewer="jay", note="배경 서술")),
        ("E_NACOVER", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None, evidence=[REAL_DOC],
            non_actionable_reason="covered_by_other_rid", reviewer="jay", note="다른 곳에서 처리")),
        ("E_NACOVEREVIDENCE", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None, evidence=[REAL_DOC],
            non_actionable_reason="covered_by_other_rid", reviewer="jay",
            note="R-CLI-5 이행에 포함됨")),
        ("E_NACOVEREVIDENCE", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None,
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "R-CLI-5"}],
            non_actionable_reason="covered_by_other_rid", reviewer="jay",
            note="R-CLI-5 및 R-CLI-6 이행에 포함됨")),
        ("E_NAREASON", lambda d: [e.update(status="non_actionable", note="x") for e in d["entries"]]),
        ("E_NOKIND", lambda d: d["entries"][0].update(status="todo")),
        ("E_NAMISUSE", lambda d: d["entries"][0].update(
            status="todo", work_kind="ops", non_actionable_reason="resolved_open")),
        ("E_UNREVIEWED", lambda d: d["entries"][0].update(work_kind="ops")),
        # 근거 anchor 가 남의 RID — 복사 실수를 기계로 잡는다
        ("E_EVSCOPE", lambda d: d["entries"][0].update(
            status="verified", work_kind="doc", reviewer="jay", non_actionable_reason=None,
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "R-CLI-5"}])),
        ("E_EVSCOPE", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None, reviewer="jay",
            non_actionable_reason="context_statement", note="배경 서술",
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "R-CLI-5"}])),
        ("E_EVSCOPE", lambda d: d["entries"][0].update(
            status="verified", work_kind="doc", reviewer="jay", non_actionable_reason=None,
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "r-cli-5"}])),
        ("E_EVSCOPE", lambda d: d["entries"][0].update(
            status="verified", work_kind="doc", reviewer="jay", non_actionable_reason=None,
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "E-WIRE-1"}])),
        ("E_EVSCOPE", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None, reviewer="jay",
            non_actionable_reason="covered_by_other_rid", note="R-CLI-5 이행에 포함됨",
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "R-CLI-6"}])),
        ("E_NACOVERTARGET", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None, reviewer="jay",
            non_actionable_reason="covered_by_other_rid", note="R-CLI-5 이행에 포함됨",
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "R-CLI-5"}])),
        ("E_NACOVER", lambda d: (
            _covered_by(d, "R-CLI-5"),
            d["entries"][0].update(note="R-CLI-5 및 R-NOPE-1 이행에 포함됨"),
        )),
        ("E_EVIDENCEREPO", _impl(work_kind="server_impl", evidence=[REAL_IOS_COMMIT, REAL_IOS_TEST])),
        ("E_EVIDENCEREPO", _impl(work_kind="client_impl", evidence=[REAL_COMMIT, REAL_TEST])),
        ("E_UNREVIEWED", lambda d: d["entries"][0].update(reviewer="jay")),
        # 해시 불가 값 — 거부가 아니라 TypeError 로 죽던 자리 3곳
        ("E_RID", lambda d: d["entries"][0].update(rid={"a": 1})),
        ("E_STATUS", lambda d: d["entries"][0].update(status={"a": 1})),
        ("E_WORKKIND", lambda d: d["entries"][0].update(status="todo", work_kind=["x"])),
        ("E_NAREASON", lambda d: d["entries"][0].update(
            status="non_actionable", non_actionable_reason={"a": 1}, note="x", reviewer="jay")),
        ("E_EVKIND", _impl(status="done", work_kind="ops", evidence=[{"kind": {"a": 1}}])),
    ],
)
def test_counterexample_is_caught(loaded, code, mutation):
    fail = _mutate(loaded, mutation)
    assert any(code in f for f in fail), f"{code} 를 잡지 못했다 — 검사가 공허하다. 실제: {fail}"


# ── 양성 대조군 — 반례만 있으면 "전부 거부" 하는 검사도 통과한다 ─────────────────

def test_initial_all_unreviewed_is_allowed(loaded):
    """분류가 구현을 막지 않아야 한다."""
    fail = _mutate(loaded, lambda d: [
        e.update(status="unreviewed", work_kind=None, evidence=[],
                 non_actionable_reason=None, reviewer=None) for e in d["entries"]])
    assert not fail, f"초기 상태를 거부하면 안 된다: {fail}"


@pytest.mark.parametrize(
    "desc,mutation",
    [
        ("서버 구현 verified — 변경 경로가 있는 commit + 수집되는 pytest",
         _impl(evidence=[REAL_COMMIT, REAL_TEST])),
        ("클라이언트 구현 verified — 변경 경로가 있는 iOS commit + 존재하는 XCTest 함수",
         _impl(work_kind="client_impl", evidence=[REAL_IOS_COMMIT, REAL_IOS_TEST])),
        ("운영 verified — commit 과 구조화된 locator를 가진 deploy attestation",
         _impl(work_kind="ops", evidence=[REAL_DEPLOY])),
        ("문서 verified — 실재 파일의 명시적 anchor",
         _impl(work_kind="doc", evidence=[REAL_DOC])),
        ("non_actionable — 사유 + 근거 + 검토자", lambda d: d["entries"][0].update(
            status="non_actionable", work_kind=None, evidence=[REAL_DOC],
            non_actionable_reason="context_statement", reviewer="jay", note="배경 서술이라 이행 대상 아님")),
        ("covered_by_other_rid — actionable active RID 명시", lambda d: _covered_by(d, "R-CLI-2")),
        ("todo — 종류만", lambda d: d["entries"][0].update(status="todo", work_kind="server_impl")),
        ("자기 RID anchor 는 허용", lambda d: d["entries"][0].update(
            status="verified", work_kind="doc", reviewer="jay", non_actionable_reason=None,
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md",
                       "anchor": d["entries"][0]["rid"].lower()}])),
        ("covered_by_other_rid 는 actionable 남의 RID 허용", lambda d: _covered_by(d, "R-CLI-5")),
        ("evidence marker 의 supports RID 는 그 RID 근거로 허용", lambda d: _entry(d, "R-CLI-9").update(
            status="verified", work_kind="doc", reviewer="jay", non_actionable_reason=None,
            evidence=[{"kind": "doc", "path": "spec/ios-topic-state-machine.md", "anchor": "E-WIRE-1"}])),
    ],
)
def test_valid_entry_is_accepted(loaded, desc, mutation):
    """⛔ 이게 없으면 '무조건 거부' 하는 검사도 위 반례 전부를 통과한다."""
    fail = _mutate(loaded, mutation)
    assert not fail, f"정상 입력을 거부했다 ({desc}): {fail}"


def test_pytest_class_node_is_supported():
    node = "tests/test_activate_atomic_fx.py::TestResolveResume::test_atomic_completed_ready_noop"
    resolved, error = _test_node_resolves("server", node)
    assert resolved, error


@pytest.mark.parametrize(
    "want,selector",
    [
        (True, "FakeTests/testPublicOne"),
        (False, "FakeTests/testPrivateOne"),
        (False, "FakeTests/testWithArg"),
        (False, "FakeTests/helperNotATest"),
        (False, "FakeTests/testStaticOne"),
        (False, "NotTests/testHelper"),
        (False, "MissingTests/testPublicOne"),
        (False, "FakeTests/testTopLevel"),
        (False, "testPublicOne"),
    ],
)
def test_xctest_source_locator_rules(tmp_path, monkeypatch, want, selector):
    (tmp_path / "FXiTests").mkdir()
    (tmp_path / "FXiTests" / "FakeTests.swift").write_text(
        "import XCTest\n"
        "final class FakeTests: XCTestCase {\n"
        "    func testPublicOne() {}\n"
        "    private func testPrivateOne() {}\n"
        "    func testWithArg(_ x: Int) {}\n"
        "    func helperNotATest() {}\n"
        "    static func testStaticOne() {}\n"
        "}\n\n"
        "final class NotTests {\n"
        "    func testHelper() {}\n"
        "}\n\n"
        "func testTopLevel() {}\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "IOS", tmp_path)
    resolved, error = _test_node_resolves("ios", f"FXiTests/FakeTests.swift::{selector}")
    assert resolved is want, f"{selector}: {error}"


def _git(repo: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_git_repo(path: pathlib.Path) -> None:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "ledger-test@example.com")
    _git(path, "config", "user.name", "Ledger Test")


def test_commit_paths_preserve_non_ascii_names(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    relative = "assets/빗썸_65.jpeg"
    (repo / "assets").mkdir()
    (repo / relative).write_bytes(b"image")
    _git(repo, "add", relative)
    _git(repo, "commit", "-qm", "add unicode path")
    sha = _git(repo, "rev-parse", "HEAD")

    monkeypatch.setattr(sys.modules[__name__], "REPO", repo)
    changed, error = _commit_changed_paths("server", sha)
    assert error is None
    assert changed == frozenset({relative})


def test_shallow_commit_without_parent_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _init_git_repo(source)
    (source / "a.txt").write_text("one")
    _git(source, "add", "a.txt")
    _git(source, "commit", "-qm", "first")
    (source / "a.txt").write_text("two")
    _git(source, "commit", "-qam", "second")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", f"file://{source}", str(shallow)],
        check=True,
    )
    sha = _git(shallow, "rev-parse", "HEAD")
    monkeypatch.setattr(sys.modules[__name__], "IOS", shallow)
    changed, error = _commit_changed_paths("ios", sha)
    assert changed is None
    assert "shallow checkout" in (error or "")


def test_transient_pytest_collection_failure_is_not_cached(monkeypatch):
    _PYTEST_NODE_CACHE.clear()
    real_run = subprocess.run
    attempts = 0

    def flaky_run(args, *pargs, **kwargs):
        nonlocal attempts
        if len(args) >= 3 and args[1:3] == ["-m", "pytest"]:
            attempts += 1
            if attempts == 1:
                raise subprocess.TimeoutExpired(args, 60)
        return real_run(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", flaky_run)
    nodes, error = _pytest_nodes("tests/test_topic_only_ledger.py")
    assert nodes is None and error
    nodes, error = _pytest_nodes("tests/test_topic_only_ledger.py")
    assert error is None
    assert "tests/test_topic_only_ledger.py::test_ledger_is_clean" in nodes
    assert attempts == 2


def _assert_workflow_ios_checkout_refs(
    workflows: tuple[tuple[str, str], ...], pinned_ios: str
) -> None:
    """Bind each named iOS checkout block to the lock's full commit independently."""
    assert re.fullmatch(r"[0-9a-f]{40}", pinned_ios)
    checkout_pattern = re.compile(
        r"^\s{6}- name: Checkout pinned iOS provenance repository\s*$"
        r"(?P<body>.*?)(?=^\s{6}- |\Z)",
        re.M | re.S,
    )
    for workflow_name, workflow in workflows:
        checkouts = list(checkout_pattern.finditer(workflow))
        assert len(checkouts) == 1, f"{workflow_name}: pinned iOS checkout must occur once"
        checkout_body = checkouts[0].group("body")
        assert re.search(r"^\s+fetch-depth:\s*0\s*$", checkout_body, re.M)
        refs = re.findall(r"^\s+ref:\s*([0-9a-f]{40})\s*$", checkout_body, re.M)
        assert refs == [pinned_ios], (
            f"{workflow_name}: iOS checkout ref must equal lock.pinned_commit.ios"
        )


def test_ci_skips_full_suite_but_runs_topic_gate_for_markdown_changes():
    full_workflow = FULL_WORKFLOW.read_text()
    doc_workflow = DOC_WORKFLOW.read_text()
    assert full_workflow.count("paths-ignore: ['**.md']") == 2
    assert doc_workflow.count("paths:\n      - '**.md'") == 2
    assert "paths-ignore" not in doc_workflow

    lock = json.loads(TOPIC_ONLY_LOCK.read_text())
    pinned_ios = lock["pinned_commit"]["ios"]

    # Both workflows independently checkout the private iOS provenance tree. The lock is
    # the sole source of truth; changing one or both copied refs must fail this gate.
    _assert_workflow_ios_checkout_refs((
        ("tests", full_workflow),
        ("topic-only-docs", doc_workflow),
    ), pinned_ios)

    assert "python scripts/topic_migration_manifest.py preflight" in doc_workflow
    listed = set(re.findall(r"tests/(test_[a-z_0-9]+\.py)", doc_workflow))
    assert DOC_GATE_TESTS <= listed


@pytest.mark.parametrize("drifted_workflows", [("tests",), ("topic-only-docs",),
                                                ("tests", "topic-only-docs")])
def test_ci_ios_checkout_ref_gate_rejects_each_drift(drifted_workflows):
    """Positive controls: either copied ref, and both together, must make the gate red."""
    lock = json.loads(TOPIC_ONLY_LOCK.read_text())
    pinned_ios = lock["pinned_commit"]["ios"]
    drifted_ios = "0" * 40 if pinned_ios != "0" * 40 else "1" * 40
    workflows = []
    for name, path in (("tests", FULL_WORKFLOW), ("topic-only-docs", DOC_WORKFLOW)):
        text = path.read_text()
        if name in drifted_workflows:
            assert text.count(pinned_ios) == 1
            text = text.replace(pinned_ios, drifted_ios)
        workflows.append((name, text))

    with pytest.raises(AssertionError, match="must equal lock.pinned_commit.ios"):
        _assert_workflow_ios_checkout_refs(tuple(workflows), pinned_ios)


def test_ios_checkout_ref_gate_stays_wired_to_the_real_workflow_files():
    """⛔ 위 대조군은 helper 를 **직접** 호출한다 — 실제 파일 배선이 사라져도 셋 다 초록이다
    (실측: 게이트에서 helper 호출 한 덩어리를 지우자 대조군 rc=0, 게이트 rc=0). 대조군은
    helper 의 판정력을 증명할 뿐 그것이 **연결돼 있다**는 것은 증명하지 못한다. 그래서
    배선 자체를 구조로 잠근다 — helper 를 정확히 한 번, 두 워크플로 텍스트와 lock 에서
    읽은 pin 으로 호출해야 한다."""
    import ast
    import inspect

    gate = test_ci_skips_full_suite_but_runs_topic_gate_for_markdown_changes
    tree = ast.parse(inspect.getsource(gate))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_assert_workflow_ios_checkout_refs"
    ]
    assert len(calls) == 1, "실제-파일 게이트가 결속 helper 를 정확히 한 번 호출해야 한다"
    rendered = ast.unparse(calls[0])
    for token in ("full_workflow", "doc_workflow", "pinned_ios"):
        assert token in rendered, f"helper 호출이 {token} 를 넘기지 않는다: {rendered}"

    # 인자가 **실제 파일과 lock** 에서 와야 한다. 리터럴로 바꿔치기하면 helper 는 통과하고
    # 워크플로 drift 는 영원히 안 잡힌다.
    source = ast.unparse(tree)
    for token in ('FULL_WORKFLOW.read_text()', 'DOC_WORKFLOW.read_text()',
                  'TOPIC_ONLY_LOCK.read_text()'):
        assert token in source, f"게이트가 {token} 를 읽지 않는다"


def _modules_with_markdown_literals() -> set[str]:
    """Return a conservative tripwire for tests containing Markdown path literals.

    This does not prove that a module opens the path, and it cannot see paths
    imported from another module or assembled dynamically. The explicit
    ``DOC_GATE_TESTS`` set remains the reviewed contract.
    """
    import ast

    found = set()
    for path in sorted((REPO / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:  # 파싱 불가 모듈은 판단하지 않는다(조용히 넘기지 않고 이름을 남긴다)
            found.add(path.name)
            continue
        docstrings = {
            doc
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and (doc := ast.get_docstring(node))
        }
        literals = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        if {x for x in literals - docstrings if x.endswith(".md")}:
            found.add(path.name)
    return found


def test_docs_gate_covers_markdown_literal_candidates():
    """직접 Markdown 리터럴 후보가 문서 게이트에서 빠지면 차단한다.

    실측 사례: `test_topic_wire`(REALTIME_V2_CLIENT_GUIDE.md 핸드오프 계약)와
    `test_ws_message_limit`(DOCKER.md 의 Dockerfile CMD 복제본)이 목록에서 빠져 있었다.
    docs-only 커밋이 그 두 문서를 바꾸면 어느 워크플로도 돌지 않는다.

    ⛔ 이것은 완전성 증명이 아니다. `test_topic_migration_launcher`처럼 import 과정에서
    다른 모듈이 Markdown을 읽거나 `test_topic_only_documents`처럼 import된 경로 상수를
    쓰는 경우는 이 AST tripwire가 보지 못한다. 그런 의존성은 명시 목록과 review가 책임진다.
    """
    # ⛔ 파일 전체를 grep 하면 **실행되지 않는 자리**(주석·주변 줄)에 이름만 있어도 통과한다.
    #    실측: 두 모듈을 명령 밖에 잘못 넣었는데 이 검사가 초록이었다. 실행 명령만 본다.
    doc_workflow = DOC_WORKFLOW.read_text()
    command = re.search(r"python -m pytest\b(?P<args>(?:[^\n]*\\\n)*[^\n]*)", doc_workflow)
    assert command is not None, "문서 게이트에 pytest 실행 명령이 없다"
    joined = re.sub(r"\\\s*\n\s*", " ", command.group("args"))
    listed = set(re.findall(r"tests/(test_[a-z_0-9]+\.py)", joined))
    candidates = _modules_with_markdown_literals()
    assert candidates == MARKDOWN_LITERAL_CANDIDATE_INVENTORY, (
        "Markdown 리터럴 후보 inventory가 바뀌었다 — AST 탐색 축소인지 의도된 문서 의존성 "
        "변경인지 검토한 뒤 inventory와 workflow를 함께 갱신하라: "
        f"actual={sorted(candidates)} expected={sorted(MARKDOWN_LITERAL_CANDIDATE_INVENTORY)}"
    )
    missing = sorted(candidates - listed)
    assert not missing, (
        "Markdown 경로 리터럴 후보가 있는데 문서 게이트가 돌리지 않는 모듈:\n"
        + "\n".join(f"  {m}" for m in missing)
        + "\n→ .github/workflows/topic-only-docs.yml 의 pytest 목록에 추가하라"
    )
