"""C2 재-baseline 실행기 — pin 전진 · 해시 체인 갱신 · semantic epoch 봉인.

⛔ **이 리포의 C2 는 손으로 하면 어긋난다.** 아래는 전부 한 세션에서 **실측된** 실패다.

  · `grep -rl "- manifest SHA:"` 가 패턴을 **옵션으로 먹어** 0건 매칭인데 루프는
    "고정점 도달" 을 출력했다 → 문서 헤더 6곳이 stale 인 채 진행됐다(테스트가 잡았다).
  · pin 일괄 치환이 `evidence[].sha`(**역사 기록**)를 덮을 수 있었다.
  · parent 지문을 손으로 옮겨 적었다(3회).
  · 체인이 서로를 먹여(문서 헤더 → 문서 해시 → 두 원장 → 지문) 봉인을 세 번 다시 했다.

그래서 **필수 대상을 찾는 탐색**은 0건을 실패로 만든다. 반대로 pin 치환 뒤 구 SHA 0건,
좌표 경고 0건처럼 0이 성공인 단계도 있다. 각 단계의 0 계약을 구분하고, 탐색 결과 0을
무조건 "할 일 없음"으로 읽지 않는다. 그리고 **손으로 넣는 값을 최소화**한다 — parent
지문과 epoch 번호는 이력에서 도출하지, 사람이 옮겨 적지 않는다.

사용:
    python3 scripts/topic_c2.py coords --ios-from <sha>      # iOS 좌표 이동 여부만
    python3 scripts/topic_c2.py coords-server --server-from <sha>  # server 좌표 이동 여부만
    python3 scripts/topic_c2.py pin (--ios|--server) <sha> [--dry-run]
    python3 scripts/topic_c2.py refresh [--dry-run]
    python3 scripts/topic_c2.py seal --author <A> --reviewer <B> [--dry-run]
    python3 scripts/topic_c2.py c2 --ios <sha> [--server <sha>] --author <A> --reviewer <B>
                                                                    # atomic + full gates
    python3 scripts/topic_c2.py verify                        # 고정점 + C2 문서/원장 gates
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
IOS_ROOT = pathlib.Path(
    os.environ.get("TOPIC_MIGRATION_IOS_ROOT", REPO.parent / "ios")
).resolve()

LOCK = REPO / "spec" / "topic-only.lock.json"
MANIFEST = REPO / "spec" / "topic-only-migration-manifest.json"
IMPL_LEDGER = REPO / "spec" / "topic-only-implementation-ledger.json"
CLAIM_LEDGER = REPO / "spec" / "topic-only-code-claim-review.json"
SEMANTIC = REPO / "spec" / "topic-only-semantic-review.json"
HISTORY_FILE = REPO / "tests" / "test_topic_only_semantic_review.py"

# pin 이 사는 곳. 여기서 빠지면 CI 가 **다른 커밋을 checkout** 한다(실측: workflows 2곳 누락).
PIN_ROOTS = ("spec", "DECISIONS.md", ".github/workflows")

DOC_HEADER_RE = re.compile(r"(- manifest SHA: `)[0-9a-f]{64}(`)")
BASELINE_HEADER_RE = re.compile(r"(- baseline SHA: `)[0-9a-f]{64}(`)")
BASELINE = REPO / "spec" / "topic-only-baseline-facts.md"
# 문서는 정확히 6개(ADR·HAND·CLIENT·CUT·LOAD·HEALTH). 이 수가 틀리면 **탐색이 깨진 것**이다.
EXPECTED_DOC_HEADER_FILES = 6

SHA40 = re.compile(r"^[0-9a-f]{40}$")
FIXPOINT_LIMIT = 8
MAX_CITED_RANGE = 2000
GATE_TESTS = (
    "tests/test_topic_c2.py",
    "tests/test_topic_only_semantic_review.py",
    "tests/test_document_citations.py",
    "tests/test_topic_only_documents.py",
    "tests/test_topic_only_ledger.py",
)


class C2Error(RuntimeError):
    """치명 — 절대 계속 진행하지 않는다."""


def _say(tag: str, message: str) -> None:
    print(f"[{tag}] {message}")


def _run(
    args: list[str],
    cwd: pathlib.Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    done = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if done.returncode != 0:
        raise C2Error(f"{' '.join(args)} 실패: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def _test_module(name: str, *required: str):
    """판정 로직의 **단일 진실 소스**는 테스트 모듈이다 — 복제하면 반드시 어긋난다.

    수입한 심볼이 없으면 조용히 다른 길로 가지 말고 즉시 실패한다.
    """
    sys.path.insert(0, str(REPO / "tests"))
    sys.modules.pop(name, None)
    module = __import__(name)
    missing = [symbol for symbol in required if not hasattr(module, symbol)]
    if missing:
        raise C2Error(f"{name} 에 {missing} 가 없다 — 판정 로직이 옮겨졌다. 이 도구를 먼저 고쳐라")
    return module


# ---------------------------------------------------------------- pin

def current_pin(target: str) -> str:
    if target not in {"server", "ios"}:
        raise C2Error(f"알 수 없는 pin target: {target!r}")
    data = json.loads(LOCK.read_text())
    pin = data.get("pinned_commit", {}).get(target)
    if not isinstance(pin, str) or not SHA40.match(pin):
        raise C2Error(f"lock 의 pinned_commit.{target} 가 40자리 sha 가 아니다: {pin!r}")
    return pin


def current_ios_pin() -> str:
    return current_pin("ios")


def current_server_pin() -> str:
    return current_pin("server")


def _pin_files(old: str) -> list[pathlib.Path]:
    # `git grep` 은 tracked working-tree 파일만 본다. 일반 `grep -r` 는 spec 아래의
    # untracked 초안까지 pin 치환 대상으로 끌어들일 수 있다.
    done = subprocess.run(
        ["git", "grep", "-l", "-F", old, "--", *PIN_ROOTS],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if done.returncode == 1:
        # 검색 결과 0건. 사전 탐색에서는 호출자가 치명 처리하고, 치환 후 검증에서는 성공이다.
        return []
    if done.returncode != 0:
        raise C2Error(f"git grep pin 탐색 실패: {done.stderr.strip() or done.stdout.strip()}")
    return [REPO / line for line in done.stdout.splitlines()]


def replace_pin_in_lines(lines: list[str], old: str, new: str) -> tuple[list[str], int, int]:
    """pin 만 바꾸고 `evidence[].sha` 는 남긴다.

    ⛔ 원장에는 sha 가 **두 종류**다. `evidence[].sha` 는 그 작업을 한 commit = **역사 기록**이라
    pin 으로 덮으면 위조가 되고, `manifest_sha256`·`pinned_commit` 은 pin 성격이라 갱신 대상이다.
    일괄 치환은 앞을 덮고, 원장 통째 제외는 뒤를 빠뜨린다 — **줄 단위로 가른다**.
    """
    out: list[str] = []
    replaced = preserved = 0
    for line in lines:
        if old in line and '"sha"' in line:
            preserved += line.count(old)
            out.append(line)
            continue
        if old in line:
            replaced += line.count(old)
            out.append(line.replace(old, new))
        else:
            out.append(line)
    return out, replaced, preserved


def _advance_pin(target: str, new: str, *, dry_run: bool = False) -> dict:
    root = IOS_ROOT if target == "ios" else REPO
    if not SHA40.match(new):
        raise C2Error(f"{target} pin 은 40자리 full sha 여야 한다: {new!r}")
    resolved = _run(["git", "-C", str(root), "rev-parse", new]).strip()
    if resolved != new:
        raise C2Error(f"{new} 가 {target} 리포에서 {resolved} 로 풀린다 — full sha 를 넘겨라")
    old = current_ios_pin() if target == "ios" else current_server_pin()
    if old == new:
        _say("PIN", f"{target} 이미 {new[:7]} — 변경 없음")
        return {"old": old, "new": new, "replaced": 0, "evidence_preserved": 0, "files": []}

    files = _pin_files(old)
    if not files:
        # 0건은 "할 일 없음" 이 아니라 **탐색이 깨졌다**는 신호다.
        raise C2Error(f"구 pin {old[:7]} 을 어느 파일에서도 못 찾았다 — 탐색 경로/패턴을 의심하라")

    replaced = preserved = 0
    touched: list[str] = []
    for path in files:
        out, changed_here, kept = replace_pin_in_lines(path.read_text().split("\n"), old, new)
        replaced += changed_here
        preserved += kept
        changed = changed_here > 0
        if changed:
            touched.append(str(path.relative_to(REPO)))
            if not dry_run:
                path.write_text("\n".join(out))
    if replaced == 0:
        raise C2Error(f"구 pin 이 {len(files)}개 파일에 있는데 치환 0건 — 전부 evidence sha 인가? 확인하라")
    if not dry_run:
        # ⛔ **줄 단위**로 본다. "파일에 `\"sha\"` 라는 글자가 있는가" 로 거르면, 무관한 sha 필드가
        #    같은 파일에 있다는 이유만으로 **진짜 누락을 가려 준다**.
        left = [
            f"{path.relative_to(REPO)}:{number}"
            for path in _pin_files(old)
            for number, line in enumerate(path.read_text().split("\n"), 1)
            if old in line and '"sha"' not in line
        ]
        if left:
            raise C2Error(f"치환 후에도 구 pin 이 남았다: {left}")
    _say(
        "PIN",
        f"{target} {old[:7]} → {new[:7]} / 치환 {replaced}곳 · "
        f"evidence 보존 {preserved}곳 · 파일 {len(touched)}",
    )
    return {"old": old, "new": new, "replaced": replaced,
            "evidence_preserved": preserved, "files": touched}


def advance_pin(new: str, *, dry_run: bool = False) -> dict:
    """Backward-compatible iOS pin entry point."""
    return _advance_pin("ios", new, dry_run=dry_run)


def advance_server_pin(new: str, *, dry_run: bool = False) -> dict:
    return _advance_pin("server", new, dry_run=dry_run)


# ---------------------------------------------------------------- refresh

def _doc_header_files() -> list[pathlib.Path]:
    # ⛔ 패턴이 '-' 로 시작한다 — `-e` 없이 부르면 grep 이 **옵션으로 먹고 0건**을 낸다(실측 사고).
    out = _run(["grep", "-rl", "-e", "- manifest SHA:", "spec", "DECISIONS.md"], cwd=REPO)
    files = [REPO / line for line in out.split()]
    if len(files) != EXPECTED_DOC_HEADER_FILES:
        raise C2Error(
            f"manifest SHA 헤더 문서가 {len(files)}개 — {EXPECTED_DOC_HEADER_FILES} 여야 한다. "
            "탐색이 깨졌거나 문서가 추가/삭제됐다"
        )
    return files


def refresh_chain(*, dry_run: bool = False) -> list[str]:
    """manifest → 문서 헤더 → 두 원장 순으로 **고정점까지** 되풀이한다.

    한 번으로 끝나지 않는 이유: 문서 헤더를 고치면 문서 해시가 바뀌고, 그게 두 원장의
    `documents` 로 들어가며, claim 원장 해시는 다시 semantic 의 `claim_ledger_sha256` 이다.
    """
    changes: list[str] = []
    for iteration in range(FIXPOINT_LIMIT):
        changed = False

        # baseline 이 **먼저**다 — lock·manifest·문서 헤더·두 원장이 전부 이 값을 먹는다.
        # (baseline-facts.md 자체가 바뀌는 C2 에서만 움직인다. 2026-08-23 첫 실측.)
        baseline_sha = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
        lock = json.loads(LOCK.read_text())
        if lock.get("baseline", {}).get("sha256") != baseline_sha:
            changed = True
            changes.append("lock: baseline.sha256")
            lock["baseline"]["sha256"] = baseline_sha
            if not dry_run:
                LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n")
        manifest = json.loads(MANIFEST.read_text())
        if manifest.get("baseline_sha") != baseline_sha:
            changed = True
            changes.append("manifest: baseline_sha")
            manifest["baseline_sha"] = baseline_sha
            if not dry_run:
                MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for path in _doc_header_files():
            text = path.read_text()
            updated = BASELINE_HEADER_RE.sub(rf"\g<1>{baseline_sha}\g<2>", text)
            if updated != text:
                changed = True
                changes.append(f"{path.relative_to(REPO)}: baseline SHA 헤더")
                if not dry_run:
                    path.write_text(updated)

        manifest_sha = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()

        for path in _doc_header_files():
            text = path.read_text()
            updated = DOC_HEADER_RE.sub(rf"\g<1>{manifest_sha}\g<2>", text)
            if updated != text:
                changed = True
                changes.append(f"{path.relative_to(REPO)}: manifest SHA 헤더")
                if not dry_run:
                    path.write_text(updated)

        ledger = json.loads(IMPL_LEDGER.read_text())
        if ledger.get("manifest_sha256") != manifest_sha:
            changed = True
            changes.append("이행 대장: manifest_sha256")
            ledger["manifest_sha256"] = manifest_sha
            if not dry_run:
                IMPL_LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")

        for module_name, path, has_claim in (
            ("test_document_citations", CLAIM_LEDGER, False),
            ("test_topic_only_semantic_review", SEMANTIC, True),
        ):
            # semantic 모듈은 기대값을 **직접** 갖지 않고 `_citation_module()` 로 위임한다.
            # 그래서 요구 심볼이 다르다 — 한쪽 기준으로 뭉뚱그리면 이 가드가 거짓 경보를 낸다.
            required = ("_citation_module", "LEDGER") if has_claim else (
                "_expected_ledger_inputs", "_expected_ledger_documents")
            module = _test_module(module_name, *required)
            source = module._citation_module() if has_claim else module
            for symbol in ("_expected_ledger_inputs", "_expected_ledger_documents"):
                if not hasattr(source, symbol):
                    raise C2Error(f"{module_name} 의 기대값 소스에 {symbol} 이 없다")
            data = json.loads(path.read_text())
            before = json.dumps(data, sort_keys=True)
            data["inputs"] = source._expected_ledger_inputs()
            data["documents"] = source._expected_ledger_documents()
            if has_claim:
                data["claim_ledger_sha256"] = hashlib.sha256(module.LEDGER.read_bytes()).hexdigest()
            if json.dumps(data, sort_keys=True) != before:
                changed = True
                changes.append(f"{path.relative_to(REPO)}: inputs/documents/claim_ledger_sha256")
                if not dry_run:
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

        if not changed:
            _say("REFRESH", f"고정점 도달 (iter {iteration}) / 갱신 {len(changes)}건")
            return changes
        if dry_run:
            # dry-run 은 파일을 안 고치므로 되풀이해도 같은 결과다. 한 바퀴로 끝낸다.
            _say("REFRESH", f"[dry-run] 갱신 필요 {len(changes)}건")
            return changes
    raise C2Error(f"{FIXPOINT_LIMIT}회 안에 체인이 고정점에 닿지 않았다 — 순환 의심")


# ---------------------------------------------------------------- seal

def _history_entries_from_text(text: str, *, source: str) -> list[tuple[int, str, str]]:
    """Parse the pinned history without importing possibly stale working-tree code."""
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise C2Error(f"{source} 의 Python 문법이 깨졌다: {error}") from error

    raw = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "SEMANTIC_BINDING_HISTORY"
            for target in node.targets
        ):
            try:
                raw = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError) as error:
                raise C2Error(f"{source} 의 SEMANTIC_BINDING_HISTORY 를 해석할 수 없다") from error
            break
    if not isinstance(raw, tuple) or not raw:
        raise C2Error(f"{source} 에 비어 있지 않은 SEMANTIC_BINDING_HISTORY tuple 이 없다")

    entries: list[tuple[int, str, str]] = []
    for index, item in enumerate(raw, 1):
        if (
            not isinstance(item, tuple)
            or len(item) != 3
            or not isinstance(item[0], int)
            or not isinstance(item[1], str)
            or not isinstance(item[2], str)
        ):
            raise C2Error(f"{source} 이력 {index}번 항목의 모양이 (int, str, str) 이 아니다")
        entries.append(item)

    for index, (sequence, parent, fingerprint) in enumerate(entries):
        expected_sequence = index + 1
        expected_parent = "GENESIS" if index == 0 else entries[index - 1][2]
        if sequence != expected_sequence or parent != expected_parent:
            raise C2Error(
                f"{source} 이력 체인이 {expected_sequence:04d}에서 끊겼다 "
                f"(sequence={sequence}, parent={parent[:12]})"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise C2Error(f"{source} epoch {sequence:04d} 지문이 64자리 sha 가 아니다")
    return entries


def _history_entries() -> list[tuple[int, str, str]]:
    return _history_entries_from_text(HISTORY_FILE.read_text(), source=str(HISTORY_FILE))


def _committed_text(relative: str) -> str:
    try:
        return _run(["git", "-C", str(REPO), "show", f"HEAD:{relative}"])
    except C2Error:
        return ""


def _committed_history_entries() -> list[tuple[int, str, str]]:
    text = _committed_text(str(HISTORY_FILE.relative_to(REPO)))
    if not text:
        raise C2Error("HEAD 의 semantic binding 이력을 읽지 못했다")
    return _history_entries_from_text(text, source="HEAD semantic binding history")


def _edge_map(data: dict, *, source: str) -> dict[tuple[str, str], dict]:
    process = data.get("review_process")
    edges = process.get("review_edges") if isinstance(process, dict) else None
    if not isinstance(edges, list) or len(edges) != 2:
        count = len(edges) if isinstance(edges, list) else "비-list"
        raise C2Error(f"{source} review_edges 가 {count}개 — 2 여야 한다")

    mapped: dict[tuple[str, str], dict] = {}
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise C2Error(f"{source} review_edges[{index}] 가 object 가 아니다")
        author, reviewer, scope = edge.get("author"), edge.get("reviewer"), edge.get("scope")
        if not isinstance(author, str) or not isinstance(reviewer, str) or not isinstance(scope, list):
            raise C2Error(f"{source} review_edges[{index}] 의 author/reviewer/scope 모양이 잘못됐다")
        if author == reviewer:
            raise C2Error(f"{source} review edge 가 자기검토다: {author}")
        if not all(isinstance(item, str) for item in scope):
            raise C2Error(f"{source} {author}→{reviewer} scope 에 문자열 아닌 항목이 있다")
        key = (author, reviewer)
        if key in mapped:
            raise C2Error(f"{source} review edge 가 중복됐다: {author}→{reviewer}")
        mapped[key] = edge
    return mapped


def _select_review_edges(data: dict, author: str, reviewer: str) -> tuple[dict, dict, dict[str, dict]]:
    if author == reviewer:
        raise C2Error("author 와 reviewer 는 달라야 한다 — 자기검토 봉인 금지")
    process = data.get("review_process")
    participants = process.get("participants") if isinstance(process, dict) else None
    if not isinstance(participants, list):
        raise C2Error("review_process.participants 가 list 가 아니다")
    by_name = {
        person.get("name"): person
        for person in participants
        if isinstance(person, dict) and isinstance(person.get("name"), str)
    }
    if len(by_name) != len(participants):
        raise C2Error("review participant 이름이 없거나 중복됐다")
    missing = [name for name in (author, reviewer) if name not in by_name]
    if missing:
        raise C2Error(f"review participant 에 없는 역할이다: {missing}")
    if set(by_name) != {author, reviewer}:
        raise C2Error(f"review participant 집합이 요청 역할쌍과 다르다: {sorted(by_name)}")

    edges = _edge_map(data, source="working semantic review")
    expected = {(author, reviewer), (reviewer, author)}
    if set(edges) != expected:
        rendered = [f"{left}→{right}" for left, right in sorted(edges)]
        raise C2Error(f"요청 역할의 reciprocal review edge 가 아니다: {rendered}")
    return edges[(author, reviewer)], edges[(reviewer, author)], by_name


def _prose_scope(edge: dict, prefix: str) -> list[str]:
    return [item for item in edge["scope"] if not item.startswith(prefix)]


def _prose_added_since_head() -> int:
    """Require committed prose to remain an exact prefix, then count appended units."""
    head = _committed_text("spec/topic-only-semantic-review.json")
    if not head:
        raise C2Error("HEAD 의 semantic review 를 읽지 못했다 — append-only 를 판정할 수 없다")
    prefix = _test_module("test_topic_only_semantic_review", "SEMANTIC_BINDING_PREFIX").SEMANTIC_BINDING_PREFIX
    try:
        committed = json.loads(head)
        current = json.loads(SEMANTIC.read_text())
    except json.JSONDecodeError as error:
        raise C2Error(f"semantic review JSON 을 읽을 수 없다: {error}") from error

    committed_edges = _edge_map(committed, source="HEAD semantic review")
    current_edges = _edge_map(current, source="working semantic review")
    if set(current_edges) != set(committed_edges):
        raise C2Error("review edge 역할쌍이 HEAD 와 달라졌다 — append-only 위반")

    added = 0
    for roles, committed_edge in committed_edges.items():
        old = _prose_scope(committed_edge, prefix)
        new = _prose_scope(current_edges[roles], prefix)
        if new[:len(old)] != old:
            raise C2Error(
                f"커밋된 산문을 rewrite/reorder 했다: {roles[0]}→{roles[1]} — "
                "정정은 기존 항목을 보존하고 뒤에 append 하라"
            )
        added += len(new) - len(old)
    return added


def seal_epoch(author: str, reviewer: str, *, dry_run: bool = False,
               allow_reseal: bool = False) -> tuple[int, str]:
    module = _test_module(
        "test_topic_only_semantic_review",
        "_semantic_review_fingerprint", "_semantic_binding_marker", "SEMANTIC_BINDING_PREFIX",
        "SEMANTIC_BINDING_RE", "_semantic_binding_histories",
    )
    data = json.loads(SEMANTIC.read_text())
    author_edge, reviewer_edge, by_name = _select_review_edges(data, author, reviewer)
    added = _prose_added_since_head()
    if added <= 0:
        raise C2Error(
            "HEAD 대비 새 산문 작업 단위가 없다 — 기록할 일이 없으면 epoch 을 열지 않는다. "
            "재봉인도 미커밋 산문 작업 단위가 있어야 한다"
        )

    committed_history = _committed_history_entries()
    history = _history_entries()
    if history[:len(committed_history)] != committed_history:
        raise C2Error("working semantic binding history 가 HEAD 이력을 rewrite 했다 — append-only 위반")
    uncommitted = history[len(committed_history):]
    last_sequence, _, last_fingerprint = committed_history[-1]
    marker_histories = module._semantic_binding_histories(data)
    expected_marker_histories = [tuple(history), tuple(history)]
    if marker_histories != expected_marker_histories:
        raise C2Error("semantic 양방향 marker 이력이 working test 이력과 다르다 — 부분 변경/삭제 의심")

    if allow_reseal:
        if len(uncommitted) != 1:
            raise C2Error(
                "--allow-reseal 은 HEAD 뒤 미커밋 epoch 이 정확히 1개일 때만 가능하다 "
                f"(현재 {len(uncommitted)}개)"
            )
        sequence, parent, old_fingerprint = uncommitted[0]
        if sequence != last_sequence + 1 or parent != last_fingerprint:
            raise C2Error("미커밋 epoch 이 HEAD 마지막 지문에 바로 이어지지 않는다")
    else:
        if uncommitted:
            raise C2Error(
                f"미커밋 epoch {uncommitted[0][0]:04d} 가 이미 있다 — "
                "같은 epoch 재봉인은 --allow-reseal, 커밋된 정정은 다음 epoch"
            )
        sequence = last_sequence + 1
        parent = last_fingerprint       # ⛔ 손으로 옮겨 적지 않는다(3회 오타 위험 실측)
        old_fingerprint = None

    existing = re.compile(rf"^{re.escape(module.SEMANTIC_BINDING_PREFIX)}{sequence:04d}:")
    matching = [
        (edge, item)
        for edge in (author_edge, reviewer_edge)
        for item in edge["scope"]
        if existing.match(item)
    ]
    if allow_reseal:
        if len(matching) != 2:
            raise C2Error(f"미커밋 epoch {sequence:04d} marker 가 {len(matching)}개 — 2 여야 한다")
        for edge, item in matching:
            match = module.SEMANTIC_BINDING_RE.fullmatch(item)
            if (
                match is None
                or int(match["sequence"]) != sequence
                or match["parent"] != parent
                or match["fingerprint"] != old_fingerprint
                or match["author"] != edge["author"]
                or match["reviewer"] != edge["reviewer"]
            ):
                raise C2Error(f"미커밋 epoch {sequence:04d} marker 역할/체인이 이력과 다르다")
        for edge in (author_edge, reviewer_edge):
            edge["scope"] = [item for item in edge["scope"] if not existing.match(item)]
    elif matching:
        raise C2Error(f"epoch {sequence:04d} marker 는 있는데 이력 tuple 이 없다 — 부분 변경을 먼저 복구하라")

    def sync() -> None:
        by_name[author]["authored_or_modified"] = list(author_edge["scope"])
        by_name[author]["independently_reviewed"] = list(reviewer_edge["scope"])
        by_name[reviewer]["authored_or_modified"] = list(reviewer_edge["scope"])
        by_name[reviewer]["independently_reviewed"] = list(author_edge["scope"])

    sync()
    fingerprint = module._semantic_review_fingerprint(data)
    author_edge["scope"].append(module._semantic_binding_marker(sequence, parent, fingerprint, author, reviewer))
    reviewer_edge["scope"].append(module._semantic_binding_marker(sequence, parent, fingerprint, reviewer, author))
    sync()
    if module._semantic_review_fingerprint(data) != fingerprint:
        raise C2Error("marker 삽입이 지문을 바꿨다 — marker 제외 규칙이 깨졌다")

    text = HISTORY_FILE.read_text()
    if allow_reseal:
        old_tuple = (
            f'    (\n        {sequence},\n        "{parent}",\n'
            f'        "{old_fingerprint}",\n    ),'
        )
        new_tuple = (
            f'    (\n        {sequence},\n        "{parent}",\n'
            f'        "{fingerprint}",\n    ),'
        )
        if text.count(old_tuple) != 1:
            raise C2Error(f"재봉인할 epoch {sequence:04d} 이력 tuple 을 정확히 찾지 못했다")
        updated_history = text.replace(old_tuple, new_tuple)
    else:
        anchor = f'        "{parent}",\n    ),\n)'
        if text.count(anchor) != 1:
            raise C2Error(f"이력 끝 앵커를 {text.count(anchor)}번 찾았다 — 1이어야 한다")
        tuple_text = (
            f'        "{parent}",\n    ),\n    (\n        {sequence},\n'
            f'        "{parent}",\n        "{fingerprint}",\n    ),\n)'
        )
        updated_history = text.replace(anchor, tuple_text)

    if dry_run:
        action = "재봉인" if allow_reseal else "봉인"
        _say("SEAL", f"[dry-run] epoch {sequence:04d} {action} parent={parent[:12]} fp={fingerprint[:12]} (산문 +{added})")
        return sequence, fingerprint

    SEMANTIC.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    HISTORY_FILE.write_text(updated_history)
    action = "재봉인" if allow_reseal else "봉인"
    _say("SEAL", f"epoch {sequence:04d} {action} / parent={parent[:12]} fp={fingerprint} (산문 +{added})")
    return sequence, fingerprint


# ---------------------------------------------------------------- coords

def _citation_target_path(name: str) -> tuple[str, str]:
    """Return the repository key and repository-relative path for a citation.

    Historical semantic prose contains bare Swift basenames, while current formal
    citations use ``ios/`` or product-relative paths. Keep basename fallback only
    for those bare references; an explicit path must retain its path identity.
    """
    clean = name.removeprefix("exchange-rate/")
    if clean.startswith("ios/"):
        return "ios", clean.removeprefix("ios/")
    if clean.startswith("server/"):
        return "server", clean.removeprefix("server/")
    if clean.startswith(("FXi/", "FXiTests/", "FXi.xcodeproj/")):
        return "ios", clean
    if "/" not in clean and clean.endswith(".swift"):
        return "ios", clean
    return "server", clean


def _cited_lines() -> dict[str, dict[str, set[int]]]:
    # Keep this extension-agnostic like the document citation gate. Restricting
    # the list to source-code suffixes silently dropped nginx ``.conf`` citations.
    pattern = re.compile(
        r"([A-Za-z0-9_./-]+\."
        r"[A-Za-z0-9_+-]*[A-Za-z_][A-Za-z0-9_+-]*):(\d+)(?:-(\d+))?"
    )
    cited: dict[str, dict[str, set[int]]] = {"ios": {}, "server": {}}
    sources = list((REPO / "spec").rglob("*.json")) + list((REPO / "spec").rglob("*.md"))
    sources.append(REPO / "DECISIONS.md")
    for path in sources:
        for match in pattern.finditer(path.read_text()):
            target, name = _citation_target_path(match.group(1))
            start = int(match.group(2))
            end = int(match.group(3)) if match.group(3) else start
            if end < start or end - start > MAX_CITED_RANGE:
                # 뒤집힌 범위/비정상 폭은 좌표가 아니라 오탈자다. 끝점만 표시해 두고 넘어간다.
                cited[target].setdefault(name, set()).update({start, end})
                continue
            cited[target].setdefault(name, set()).update(range(start, end + 1))
    return cited


def parse_hunks(diff: str) -> list[tuple[int, int, int, int]]:
    """`@@ -a,b +c,d @@` → (old_start, old_len, new_start, new_len). 개수 생략은 1이다."""
    return [
        (int(m.group(1)), 1 if m.group(2) is None else int(m.group(2)),
         int(m.group(3)), 1 if m.group(4) is None else int(m.group(4)))
        for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.M)
    ]


def coordinate_findings(cited_lines: set[int],
                        hunks: list[tuple[int, int, int, int]]) -> tuple[bool, bool]:
    """(좌표가 밀렸는가, 인용된 줄의 내용이 바뀌었는가) — **두 신호는 다른 사건이다**.

    ⛔ 처음엔 "최초 변경행 ≤ 최대 인용행" 하나로 뭉쳤다가, 첫 실사용에서 **1:1 치환**
    (`@@ -333 +333 @@`, 체크박스 한 줄)에 거짓 경보가 났다. 줄 수가 그대로면 아래 인용은
    한 줄도 안 밀린다.

    · **밀림**: 인용 위에서 줄 수가 바뀌었다(삽입/삭제) → 좌표를 **다시 도출**해야 한다.
    · **내용 변경**: 인용된 줄 자체가 바뀌었다 → 좌표는 그대로여도 **주장문이 여전히 참인지**
      다시 읽어야 한다(게이트는 줄의 실재만 보고 문장의 참을 안 본다).
    """
    if not cited_lines:
        return False, False
    # **인용별 누적 delta** 로 판정한다. "줄 수가 바뀐 hunk 가 최대 인용행 위에 하나라도 있으면 밀림"
    # 은 과잉이다 — 위쪽에서 -1 과 +1 이 상쇄되면 아래 인용은 **한 줄도 안 움직인다**(실측 false
    # positive 2번째 형태. 첫 번째는 1:1 치환이었다).
    # hunk 가 인용 L 보다 **완전히 위**일 때만 L 을 민다: old_start + max(old_len,1) <= L.
    # (순수 삽입 `@@ -10,0 +11,3 @@` 은 옛 10행 **뒤**라 11행부터 민다 → max(old_len,1)=1 로 맞는다.)
    def shift_at(line: int) -> int:
        return sum(
            new_len - old_len
            for old_start, old_len, _new_start, new_len in hunks
            if old_start + max(old_len, 1) <= line
        )

    shifted = any(shift_at(line) != 0 for line in cited_lines)
    touched = any(
        old_len > 0 and any(old_start <= line < old_start + old_len for line in cited_lines)
        for old_start, old_len, _new_start, _new_len in hunks
    )
    return shifted, touched


def _check_coordinates(
    root: pathlib.Path,
    revision_from: str,
    revision_to: str,
    *,
    target: str,
) -> list[str]:
    """**pin 전진은 좌표 재도출이 아니다.** 인용 위쪽에 줄이 끼면 아래 인용이 조용히 밀린다.

    citation 게이트는 "그 행이 파일 안에 있는가" 만 보므로 **CI 는 통과한다**(실측 12건).
    """
    cited = _cited_lines()[target]
    revision_range = f"{revision_from}..{revision_to}"
    # Rename detection normally reports only the new path. Disabling it exposes
    # both deletion and addition, so a citation to the old path cannot disappear
    # as an apparently uncited new basename. It also gives pure path moves hunks.
    changed = _run([
        "git", "-C", str(root), "diff", "--no-renames", "--name-only", revision_range,
    ]).splitlines()
    if not changed:
        raise C2Error(f"{target} {revision_range} 사이 변경 파일이 0 — 범위를 확인하라")
    warnings: list[str] = []
    for name in changed:
        base = name.split("/")[-1]
        cited_lines = cited.get(name, set()) | cited.get(base, set())
        if not cited_lines:
            _say("COORD", f"{target}:{name}: 인용 없음")
            continue
        diff = _run([
            "git", "-C", str(root), "diff", "--no-renames", "-U0", revision_range,
            "--", name,
        ])
        hunks = parse_hunks(diff)
        if not hunks:
            continue
        shifted, touched = coordinate_findings(cited_lines, hunks)
        if shifted:
            warnings.append(
                f"{target}:{name}: 인용 위에서 줄 수가 바뀌었다 — "
                "좌표를 주장문으로 **다시 찾아라**"
            )
            _say("COORD", f"⚠️ {warnings[-1]}")
        if touched:
            warnings.append(
                f"{target}:{name}: 인용된 줄의 **내용**이 바뀌었다 — "
                "그 주장이 아직 참인지 확인하라"
            )
            _say("COORD", f"⚠️ {warnings[-1]}")
        if not shifted and not touched:
            _say("COORD", f"✅ {target}:{name}: 인용 밀림 없음 · 인용 줄 내용 불변")
    return warnings


def check_coordinates(ios_from: str, ios_to: str = "HEAD") -> list[str]:
    return _check_coordinates(IOS_ROOT, ios_from, ios_to, target="ios")


def check_server_coordinates(server_from: str, server_to: str = "HEAD") -> list[str]:
    return _check_coordinates(REPO, server_from, server_to, target="server")


# ---------------------------------------------------------------- composite safety

def _c2_mutation_paths(*old_pins: str) -> list[pathlib.Path]:
    """Return every tracked file that pin/refresh/seal may mutate."""
    paths: set[pathlib.Path] = set()
    for old_pin in old_pins:
        pin_files = _pin_files(old_pin)
        if not pin_files:
            raise C2Error(f"구 pin {old_pin[:7]} 을 어느 tracked 파일에서도 못 찾았다")
        paths.update(pin_files)
    paths.update(_doc_header_files())
    paths.update((IMPL_LEDGER, CLAIM_LEDGER, SEMANTIC, HISTORY_FILE, LOCK, MANIFEST))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise C2Error(f"C2 mutation 대상 파일이 없다: {[str(path) for path in missing]}")
    return sorted(paths)


def _snapshot_files(paths: list[pathlib.Path]) -> dict[pathlib.Path, bytes]:
    return {path: path.read_bytes() for path in paths}


def _restore_files(snapshot: dict[pathlib.Path, bytes]) -> None:
    errors: list[str] = []
    for path, content in snapshot.items():
        try:
            path.write_bytes(content)
        except OSError as error:
            errors.append(f"{path}: {error}")
    if errors:
        raise C2Error(f"C2 rollback 중 파일 복원 실패: {errors}")


def verify_gates() -> None:
    """Run the repository gates required before a composite C2 can report success."""
    pending = refresh_chain(dry_run=True)
    if pending:
        raise C2Error(f"해시 체인이 고정점이 아니다: {pending}")

    env = dict(os.environ)
    env["TOPIC_MIGRATION_IOS_ROOT"] = str(IOS_ROOT)
    _run([sys.executable, "scripts/topic_migration_manifest.py", "preflight"], cwd=REPO, env=env)
    _run(
        [sys.executable, "-m", "pytest", *GATE_TESTS, "-q", "-p", "no:asyncio"],
        cwd=REPO,
        env=env,
    )
    _run(["git", "diff", "--check"], cwd=REPO)
    _run(["git", "-C", str(IOS_ROOT), "diff", "--check"])
    _say("VERIFY", "체인 고정점 · preflight · 문서/원장/semantic tests · 양 리포 diff-check 통과")


def run_c2(
    ios: str,
    author: str,
    reviewer: str,
    *,
    server: str | None = None,
    allow_reseal: bool = False,
    coords_rederived: bool = False,
) -> None:
    old_ios_pin = current_ios_pin()
    old_server_pin = current_server_pin()
    new_server_pin = server or old_server_pin

    warnings: list[str] = []
    if new_server_pin != old_server_pin:
        warnings.extend(check_server_coordinates(old_server_pin, new_server_pin))
    if ios != old_ios_pin:
        warnings.extend(check_coordinates(old_ios_pin, ios))
    if new_server_pin == old_server_pin and ios == old_ios_pin:
        raise C2Error("server/iOS pin 이 모두 현재값이다 — 전진할 commit 을 확인하라")
    if warnings:
        rendered = " / ".join(warnings)
        if not coords_rederived:
            raise C2Error(f"좌표 재도출 경고가 있어 쓰기 전에 중단한다: {rendered}")
        # ⛔ 이 플래그는 검사를 끄는 게 아니라 **사람의 선언**을 요구한다.
        #
        # 한때 "인용이 pin 시점과 같은 내용을 가리키는가" 로 자동 판정하려 했는데 **공허했다**:
        # 재도출이 균일 shift 라 stale 좌표(`L`)도 재도출 좌표(`L+shift`)도 둘 다
        # `head[L] == pin[L-shift]` 를 만족한다 — 어떤 번호를 넣어도 통과한다(변이로 실측).
        # 게이트는 "무언가 움직였다" 까지만 알 수 있고, **그 범위가 주장을 증명하는가**는
        # 주장문을 읽어야 안다. 그래서 자동 통과 대신 명시 선언으로 남긴다.
        _say("COORD", "⚠️ --coords-rederived: 주장문 기준 재도출을 **선언**하고 진행한다")
        for item in warnings:
            _say("COORD", f"   선언된 경고: {item}")
    elif coords_rederived:
        raise C2Error("--coords-rederived 를 지정했지만 좌표 경고가 0건이다 — 불필요한 우회를 제거하라")

    mutation_pins = [old_ios_pin]
    if new_server_pin != old_server_pin:
        mutation_pins.append(old_server_pin)
    snapshot = _snapshot_files(_c2_mutation_paths(*mutation_pins))
    try:
        if new_server_pin != old_server_pin:
            advance_server_pin(new_server_pin)
        advance_pin(ios)
        refresh_chain()
        seal_epoch(author, reviewer, allow_reseal=allow_reseal)
        verify_gates()
    except BaseException:
        _restore_files(snapshot)
        _say("ROLLBACK", "C2 실패 — pin/chain/semantic/history 파일을 실행 전 bytes 로 복원")
        raise


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    coords = sub.add_parser("coords", help="pin 전진 전 좌표 이동 여부")
    coords.add_argument("--ios-from", required=True)
    coords.add_argument("--ios-to", default="HEAD")

    server_coords = sub.add_parser("coords-server", help="server pin 전진 전 좌표 이동 여부")
    server_coords.add_argument("--server-from", required=True)
    server_coords.add_argument("--server-to", default="HEAD")

    pin = sub.add_parser("pin", help="server 또는 iOS pin 전진 (evidence sha 보존)")
    pin_target = pin.add_mutually_exclusive_group(required=True)
    pin_target.add_argument("--ios")
    pin_target.add_argument("--server")
    pin.add_argument("--dry-run", action="store_true")

    refresh = sub.add_parser("refresh", help="해시 체인 고정점 갱신")
    refresh.add_argument("--dry-run", action="store_true")

    seal = sub.add_parser("seal", help="semantic epoch 봉인 (parent·번호 자동)")
    seal.add_argument("--author", required=True)
    seal.add_argument("--reviewer", required=True)
    seal.add_argument("--dry-run", action="store_true")
    seal.add_argument("--allow-reseal", action="store_true")

    whole = sub.add_parser("c2", help="coords → pin → refresh → seal → verify 원자 실행")
    whole.add_argument("--ios", required=True)
    whole.add_argument("--server")
    whole.add_argument("--author", required=True)
    whole.add_argument("--reviewer", required=True)
    whole.add_argument("--allow-reseal", action="store_true")
    whole.add_argument("--coords-rederived", action="store_true",
                       help="좌표를 주장문 기준으로 재도출했음을 **선언**한다(검사를 끄지 않는다)")

    sub.add_parser("verify", help="고정점·preflight·문서/원장 tests·diff-check 전체 검증")

    args = parser.parse_args(argv)
    try:
        if args.command == "coords":
            return 1 if check_coordinates(args.ios_from, args.ios_to) else 0
        if args.command == "coords-server":
            return 1 if check_server_coordinates(args.server_from, args.server_to) else 0
        if args.command == "pin":
            if args.ios:
                advance_pin(args.ios, dry_run=args.dry_run)
            else:
                advance_server_pin(args.server, dry_run=args.dry_run)
            return 0
        if args.command == "refresh":
            refresh_chain(dry_run=args.dry_run)
            return 0
        if args.command == "seal":
            seal_epoch(args.author, args.reviewer, dry_run=args.dry_run, allow_reseal=args.allow_reseal)
            return 0
        if args.command == "verify":
            verify_gates()
            return 0
        if args.command == "c2":
            run_c2(args.ios, args.author, args.reviewer, allow_reseal=args.allow_reseal,
                   server=args.server, coords_rederived=args.coords_rederived)
            return 0
    except C2Error as error:
        print(f"ERROR: {error}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
