"""C2 재-baseline 실행기 — pin 전진 · 해시 체인 갱신 · semantic epoch 봉인.

⛔ **이 리포의 C2 는 손으로 하면 어긋난다.** 아래는 전부 한 세션에서 **실측된** 실패다.

  · `grep -rl "- manifest SHA:"` 가 패턴을 **옵션으로 먹어** 0건 매칭인데 루프는
    "고정점 도달" 을 출력했다 → 문서 헤더 6곳이 stale 인 채 진행됐다(테스트가 잡았다).
  · pin 일괄 치환이 `evidence[].sha`(**역사 기록**)를 덮을 수 있었다.
  · parent 지문을 손으로 옮겨 적었다(3회).
  · 체인이 서로를 먹여(문서 헤더 → 문서 해시 → 두 원장 → 지문) 봉인을 세 번 다시 했다.

그래서 여기 검사는 전부 **0건을 실패로** 만든다. 탐색 결과 0 을 "할 일 없음" 으로 읽는
것이 이 도구가 막으려는 실패다. 그리고 **손으로 넣는 값을 최소화**한다 — parent 지문과
epoch 번호는 이력에서 도출하지, 사람이 옮겨 적지 않는다.

사용:
    python3 scripts/topic_c2.py coords --ios-from <sha>      # 좌표 이동 여부만
    python3 scripts/topic_c2.py pin --ios <sha> [--dry-run]
    python3 scripts/topic_c2.py refresh [--dry-run]
    python3 scripts/topic_c2.py seal --author <A> --reviewer <B> [--dry-run]
    python3 scripts/topic_c2.py c2 --ios <sha> --author <A> --reviewer <B>
    python3 scripts/topic_c2.py verify                        # 체인이 이미 고정점인가
"""

from __future__ import annotations

import argparse
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
# 문서는 정확히 6개(ADR·HAND·CLIENT·CUT·LOAD·HEALTH). 이 수가 틀리면 **탐색이 깨진 것**이다.
EXPECTED_DOC_HEADER_FILES = 6

SHA40 = re.compile(r"^[0-9a-f]{40}$")
FIXPOINT_LIMIT = 8


class C2Error(RuntimeError):
    """치명 — 절대 계속 진행하지 않는다."""


def _say(tag: str, message: str) -> None:
    print(f"[{tag}] {message}")


def _run(args: list[str], cwd: pathlib.Path | None = None) -> str:
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
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

def current_ios_pin() -> str:
    data = json.loads(LOCK.read_text())
    pin = data.get("pinned_commit", {}).get("ios")
    if not isinstance(pin, str) or not SHA40.match(pin):
        raise C2Error(f"lock 의 pinned_commit.ios 가 40자리 sha 가 아니다: {pin!r}")
    return pin


def _pin_files(old: str) -> list[pathlib.Path]:
    out = _run(["grep", "-rl", "-e", old, *PIN_ROOTS], cwd=REPO)
    return [REPO / line for line in out.split()]


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


def advance_pin(new: str, *, dry_run: bool = False) -> dict:
    if not SHA40.match(new):
        raise C2Error(f"iOS pin 은 40자리 full sha 여야 한다: {new!r}")
    resolved = _run(["git", "-C", str(IOS_ROOT), "rev-parse", new]).strip()
    if resolved != new:
        raise C2Error(f"{new} 가 iOS 리포에서 {resolved} 로 풀린다 — full sha 를 넘겨라")
    old = current_ios_pin()
    if old == new:
        _say("PIN", f"이미 {new[:7]} — 변경 없음")
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
    _say("PIN", f"{old[:7]} → {new[:7]} / 치환 {replaced}곳 · evidence 보존 {preserved}곳 · 파일 {len(touched)}")
    return {"old": old, "new": new, "replaced": replaced,
            "evidence_preserved": preserved, "files": touched}


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

def _history_entries() -> list[tuple]:
    module = _test_module("test_topic_only_semantic_review", "SEMANTIC_BINDING_HISTORY")
    return list(module.SEMANTIC_BINDING_HISTORY)


def _committed_text(relative: str) -> str:
    try:
        return _run(["git", "-C", str(REPO), "show", f"HEAD:{relative}"])
    except C2Error:
        return ""


def _prose_added_since_head() -> int:
    """산문 작업 단위가 실제로 늘었는가. epoch 은 **기록할 일이 있을 때만** 연다."""
    head = _committed_text("spec/topic-only-semantic-review.json")
    if not head:
        return 1
    prefix = _test_module("test_topic_only_semantic_review", "SEMANTIC_BINDING_PREFIX").SEMANTIC_BINDING_PREFIX

    def prose(raw: str) -> int:
        data = json.loads(raw)
        return sum(
            1
            for edge in data["review_process"]["review_edges"]
            for item in edge["scope"]
            if not item.startswith(prefix)
        )

    return prose(SEMANTIC.read_text()) - prose(head)


def seal_epoch(author: str, reviewer: str, *, dry_run: bool = False,
               allow_reseal: bool = False) -> tuple[int, str]:
    module = _test_module(
        "test_topic_only_semantic_review",
        "_semantic_review_fingerprint", "_semantic_binding_marker", "SEMANTIC_BINDING_PREFIX",
    )
    history = _history_entries()
    if not history:
        raise C2Error("이력이 비어 있다 — genesis 부터는 손으로 처리하라")
    last_sequence, _, last_fingerprint = history[-1]
    sequence = last_sequence + 1
    parent = last_fingerprint            # ⛔ 손으로 옮겨 적지 않는다(3회 오타 위험 실측)

    added = _prose_added_since_head()
    if added <= 0 and not allow_reseal:
        raise C2Error(
            "HEAD 대비 새 산문 작업 단위가 없다 — 기록할 일이 없으면 epoch 을 열지 않는다. "
            "미커밋 epoch 을 다시 봉인하려면 --allow-reseal"
        )

    data = json.loads(SEMANTIC.read_text())
    edges = data["review_process"]["review_edges"]
    if len(edges) != 2:
        raise C2Error(f"review_edges 가 {len(edges)}개 — 2 여야 한다")
    author_edge, reviewer_edge = edges

    existing = re.compile(rf"^{re.escape(module.SEMANTIC_BINDING_PREFIX)}{sequence:04d}:")
    already = any(existing.match(item) for edge in edges for item in edge["scope"])
    if already:
        if not allow_reseal:
            raise C2Error(f"epoch {sequence:04d} 가 이미 있다 — 재봉인은 --allow-reseal")
        if f"{sequence:04d}:" in _committed_text("spec/topic-only-semantic-review.json"):
            # append-only 계약: 커밋된 epoch 은 **절대** 다시 쓰지 않는다. 앞으로 supersede 한다.
            raise C2Error(f"epoch {sequence:04d} 는 이미 커밋됐다 — 재봉인 금지, 다음 epoch 으로 정정하라")
        for edge in edges:
            edge["scope"] = [item for item in edge["scope"] if not existing.match(item)]

    def sync() -> None:
        by_name = {p["name"]: p for p in data["review_process"]["participants"]}
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
    anchor = f'        "{parent}",\n    ),\n)'
    if text.count(anchor) != 1:
        raise C2Error(f"이력 끝 앵커를 {text.count(anchor)}번 찾았다 — 1이어야 한다")
    tuple_text = f'        "{parent}",\n    ),\n    (\n        {sequence},\n        "{parent}",\n        "{fingerprint}",\n    ),\n)'

    if dry_run:
        _say("SEAL", f"[dry-run] epoch {sequence:04d} parent={parent[:12]} fp={fingerprint[:12]} (산문 +{added})")
        return sequence, fingerprint

    SEMANTIC.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    HISTORY_FILE.write_text(text.replace(anchor, tuple_text))
    _say("SEAL", f"epoch {sequence:04d} 봉인 / parent={parent[:12]} fp={fingerprint} (산문 +{added})")
    return sequence, fingerprint


# ---------------------------------------------------------------- coords

def _cited_lines() -> dict[str, set[int]]:
    pattern = re.compile(r"([A-Za-z0-9_./-]+\.(?:swift|md|py|json)):(\d+)(?:-(\d+))?")
    cited: dict[str, set[int]] = {}
    sources = list((REPO / "spec").rglob("*.json")) + list((REPO / "spec").rglob("*.md"))
    sources.append(REPO / "DECISIONS.md")
    for path in sources:
        for match in pattern.finditer(path.read_text()):
            name = match.group(1).split("/")[-1]
            cited.setdefault(name, set()).add(int(match.group(2)))
            if match.group(3):
                cited[name].add(int(match.group(3)))
    return cited


def check_coordinates(ios_from: str, ios_to: str = "HEAD") -> list[str]:
    """**pin 전진은 좌표 재도출이 아니다.** 인용 위쪽에 줄이 끼면 아래 인용이 조용히 밀린다.

    citation 게이트는 "그 행이 파일 안에 있는가" 만 보므로 **CI 는 통과한다**(실측 12건).
    """
    cited = _cited_lines()
    changed = _run(["git", "-C", str(IOS_ROOT), "diff", "--name-only", f"{ios_from}..{ios_to}"]).split()
    if not changed:
        raise C2Error(f"{ios_from}..{ios_to} 사이 변경 파일이 0 — 범위를 확인하라")
    warnings: list[str] = []
    for name in changed:
        base = name.split("/")[-1]
        if base not in cited:
            _say("COORD", f"{name}: 인용 없음")
            continue
        diff = _run(["git", "-C", str(IOS_ROOT), "diff", "-U0", f"{ios_from}..{ios_to}", "--", name])
        starts = [int(m.group(1)) for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)", diff, re.M)]
        if not starts:
            continue
        top, first = max(cited[base]), min(starts)
        if first <= top:
            warnings.append(f"{name}: 최초 변경행 {first} ≤ 최대 인용행 {top} — 주장문으로 좌표를 **다시 찾아라**")
            _say("COORD", f"⚠️ {warnings[-1]}")
        else:
            _say("COORD", f"✅ {name}: 최초 변경 {first} > 최대 인용 {top} — 이동 없음")
    return warnings


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    coords = sub.add_parser("coords", help="pin 전진 전 좌표 이동 여부")
    coords.add_argument("--ios-from", required=True)
    coords.add_argument("--ios-to", default="HEAD")

    pin = sub.add_parser("pin", help="iOS pin 전진 (evidence sha 보존)")
    pin.add_argument("--ios", required=True)
    pin.add_argument("--dry-run", action="store_true")

    refresh = sub.add_parser("refresh", help="해시 체인 고정점 갱신")
    refresh.add_argument("--dry-run", action="store_true")

    seal = sub.add_parser("seal", help="semantic epoch 봉인 (parent·번호 자동)")
    seal.add_argument("--author", required=True)
    seal.add_argument("--reviewer", required=True)
    seal.add_argument("--dry-run", action="store_true")
    seal.add_argument("--allow-reseal", action="store_true")

    whole = sub.add_parser("c2", help="pin → refresh → seal")
    whole.add_argument("--ios", required=True)
    whole.add_argument("--author", required=True)
    whole.add_argument("--reviewer", required=True)
    whole.add_argument("--allow-reseal", action="store_true")

    sub.add_parser("verify", help="체인이 이미 고정점인가 (변경 필요하면 실패)")

    args = parser.parse_args(argv)
    try:
        if args.command == "coords":
            return 1 if check_coordinates(args.ios_from, args.ios_to) else 0
        if args.command == "pin":
            advance_pin(args.ios, dry_run=args.dry_run)
            return 0
        if args.command == "refresh":
            refresh_chain(dry_run=args.dry_run)
            return 0
        if args.command == "seal":
            seal_epoch(args.author, args.reviewer, dry_run=args.dry_run, allow_reseal=args.allow_reseal)
            return 0
        if args.command == "verify":
            pending = refresh_chain(dry_run=True)
            if pending:
                for item in pending:
                    print(f"STALE: {item}")
                return 1
            print("체인 고정점 — 갱신할 것 없음")
            return 0
        if args.command == "c2":
            check_coordinates(current_ios_pin(), args.ios)
            advance_pin(args.ios)
            refresh_chain()
            seal_epoch(args.author, args.reviewer, allow_reseal=args.allow_reseal)
            return 0
    except C2Error as error:
        print(f"ERROR: {error}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
