#!/usr/bin/env python3
"""C3 iOS pin 결속 게이트 변이 배터리.

⛔ pytest exit 1(테스트 실패)만 KILLED 로 센다 — 2·3·4·5 는 인프라 오류이고
   그걸 KILLED 로 세면 배터리가 자기 자신을 속인다.
⛔ ast.parse 로 구문 파괴 변이를 INVALID 로 걸러낸다(구문 오류는 판별력이 아니다).
⛔ try/finally + sha256 로 원본을 복원한다.
"""
import ast, json, pathlib, subprocess, sys

from mutation_battery_guard import (
    BatteryLockBusy,
    MutatedFile,
    battery_lock,
    isolated_worktree,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
TEST = REPO / "tests" / "test_topic_only_ledger.py"
WF_TESTS = REPO / ".github" / "workflows" / "tests.yml"
WF_DOCS = REPO / ".github" / "workflows" / "topic-only-docs.yml"
LOCK = REPO / "spec" / "topic-only.lock.json"
PIN = json.loads(LOCK.read_text())["pinned_commit"]["ios"]
DRIFT_PIN = "0" * 40 if PIN != "0" * 40 else "1" * 40
SEL = "ci_skips_full_suite or ci_ios_checkout or stays_wired"

CALL = """    _assert_workflow_ios_checkout_refs((
        ("tests", full_workflow),
        ("topic-only-docs", doc_workflow),
    ), pinned_ios)
"""

# (이름, 대상파일, old, new) — old 는 대상 안에서 정확히 1회여야 한다
MUTANTS = [
    ("게이트에서 helper 배선 제거", TEST, CALL, "    # 배선 제거\n"),
    ("helper 를 두 번 호출", TEST, CALL, CALL + CALL),
    ("pin 을 리터럴로 바꿔치기", TEST, CALL, CALL.replace("), pinned_ios)", f'), "{PIN}")')),
    ("doc_workflow 를 게이트 호출에서 제거", TEST,
     '        ("topic-only-docs", doc_workflow),\n', ""),
    ("워크플로 텍스트를 리터럴로", TEST,
     "def test_ci_skips_full_suite_but_runs_topic_gate_for_markdown_changes():\n"
     "    full_workflow = FULL_WORKFLOW.read_text()\n"
     "    doc_workflow = DOC_WORKFLOW.read_text()\n",
     "def test_ci_skips_full_suite_but_runs_topic_gate_for_markdown_changes():\n"
     "    full_workflow = ''\n    doc_workflow = ''\n"),
    ("실제 tests.yml ref drift", WF_TESTS, PIN, DRIFT_PIN),
    ("실제 topic-only-docs.yml ref drift", WF_DOCS, PIN, DRIFT_PIN),
    ("실제 파일 둘 다 drift", None, None, None),   # 아래에서 특수 처리
]

def run(cwd: pathlib.Path) -> int:
    test = cwd / TEST.relative_to(REPO)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:asyncio",
                           str(test), "-k", SEL], cwd=cwd,
                          capture_output=True, text=True).returncode

def main() -> int:
    try:
        lock_ctx = battery_lock(REPO)
        lock_ctx.__enter__()
    except BatteryLockBusy as exc:
        print(f"❌ {exc}")
        return 2
    try:
        wt_ctx = isolated_worktree(REPO)
        work = wt_ctx.__enter__()
    except Exception as exc:  # noqa: BLE001 — 격리 실패는 공유 트리 변이의 사유가 못 된다
        lock_ctx.__exit__(None, None, None)
        print(f"❌ 격리 worktree 생성 실패 — 공유 트리에서 변이하지 않는다: {exc}")
        return 2
    print(f"격리 worktree: {work}")
    try:
        return _main_locked(work)
    finally:
        wt_ctx.__exit__(None, None, None)
        lock_ctx.__exit__(None, None, None)


def _main_locked(work: pathlib.Path) -> int:
    files = [TEST, WF_TESTS, WF_DOCS]
    handles = {f: MutatedFile(work / f.relative_to(REPO)) for f in files}
    originals = {f: handle.original for f, handle in handles.items()}
    base = run(work)
    if base != 0:
        print(f"⛔ 기준선이 green 이 아니다(rc={base}) — 배터리 무효"); return 2
    print(f"기준선 green (rc=0)\n")
    killed = survived = invalid = infra = 0
    try:
        for name, target, old, new in MUTANTS:
            try:
                if target is None:      # 둘 다 drift
                    drifted = {}
                    for f in (WF_TESTS, WF_DOCS):
                        text = originals[f]
                        if text.count(PIN) != 1:
                            print(f"INVALID  {name}  ← {f.name} pin 앵커 {text.count(PIN)}회")
                            invalid += 1
                            break
                        drifted[f] = text.replace(PIN, DRIFT_PIN)
                    else:
                        for f, text in drifted.items():
                            handles[f].write_mutant(text)
                    if len(drifted) != 2:
                        continue
                else:
                    text = originals[target]
                    if text.count(old) != 1:
                        print(f"INVALID  {name}  ← 앵커 {text.count(old)}회"); invalid += 1; continue
                    mutated = text.replace(old, new)
                    if target is TEST:
                        try:
                            ast.parse(mutated)
                        except SyntaxError as exc:
                            print(f"INVALID  {name}  ← 구문 파괴 {exc}"); invalid += 1
                            continue
                    handles[target].write_mutant(mutated)
                rc = run(work)
                verdict = ("KILLED" if rc == 1 else "SURVIVED" if rc == 0 else f"INFRA(rc={rc})")
                if rc == 1: killed += 1
                elif rc == 0: survived += 1
                else: infra += 1
                print(f"{verdict:9} {name}")
            finally:
                for handle in handles.values():
                    handle.restore()
    finally:
        for handle in handles.values():
            handle.restore()
        print("\n복원 ✅ 전부 일치")
    total = killed + survived + invalid + infra
    print(f"killed={killed} survived={survived} invalid={invalid} infra={infra} / {total}")
    return 0 if survived == invalid == infra == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
