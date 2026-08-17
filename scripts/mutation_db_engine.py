#!/usr/bin/env python3
"""S2 — `app/database.py` engine 설정 변이 배터리.

## 왜 S1b 배터리와 분리했나

처음엔 S2-1 을 `mutation_rest_auth_lane.py` 에 넣었는데, 그러면 **인증 검증 전체가 DB 계약에
결합**된다(codex). 보고 수치도 "S1b 19" 로 뭉개져 실제 구성(S1b 18 + S2 1)을 가린다.
도메인이 다르면 배터리도 다르다.

## probe 는 **node selector** 다

파일 전체가 아니라 그 계약을 소유한 테스트 하나만 돌린다 — 무관한 테스트가 섞이면 귀속이
흐려지고, 그 파일에 PG 필요 테스트가 있으면 **없는 환경에서 판정이 흔들린다**.

⚠️ 지금 이 배터리에 PG 는 **필요 없다**. 한때 PG preflight 를 넣었는데, SQLite 계약을 별
   클래스로 분리한 직후라 이미 불필요했다 — 수정 둘이 서로의 전제를 무효화한 **과교정**이었다
   (실측: PG 없이 `rc=1` KILLED). 실제 PG 가 필요한 변이가 생기면 그때 그 변이에
   `requires_pg` 를 달고 **그 배터리에서만** preflight 한다. 쓰지 않을 기전을 미리 만들지 않는다.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mutation_battery_guard import (  # noqa: E402
    BatteryLockBusy, MutatedFile, MutationConflict, battery_lock,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
DB = "app/database.py"
SQLITE_LEAK = ("tests/test_pg_engine_binding.py::TestSqlitePathNeedsNoPostgres::"
               "test_sqlite_path_does_not_receive_postgres_only_kwargs")

_SQLITE_ARGS = '    _engine_kwargs["connect_args"] = {"check_same_thread": False}\n'

MUTANTS: list[tuple[str, str, list[tuple[str, str]], tuple[str, ...]]] = [
    ("S2-1 PostgreSQL 전용 connect_args 가 SQLite 경로로 누수 → 로컬·테스트가 연결에서 죽는다",
     DB, [(_SQLITE_ARGS,
           '    _engine_kwargs["connect_args"] = {"check_same_thread": False,\n'
           '                                      "options": "-c statement_timeout=1000"}\n')],
     (SQLITE_LEAK,)),
]

MID_LINE_ANCHORS: set[str] = set()


def apply_pairs(source: str, pairs: list[tuple[str, str]]) -> tuple[str | None, str]:
    """변이 적용 — **순수 함수**. 앵커 4중 검증 + 전체 no-op 금지."""
    cur = source
    for old, new in pairs:
        if cur.count(old) != 1:
            return None, f"앵커 발생 {cur.count(old)}회"
        i = cur.find(old)
        if old not in MID_LINE_ANCHORS and not (i == 0 or cur[i - 1] == "\n"):
            return None, "앵커가 줄머리에서 시작하지 않는다"
        nxt = cur.replace(old, new, 1)
        if nxt == cur:
            return None, "pair no-op (old == new)"
        cur = nxt
    if cur == source:
        return None, "전체 no-op (최종본이 원본과 같다)"
    try:
        ast.parse(cur)
    except SyntaxError as exc:
        return None, f"변이본 문법 오류: {exc}"
    return cur, "ok"


def classify(codes: dict[str, int]) -> str:
    if any(rc not in (0, 1) for rc in codes.values()):
        return "INFRA"
    return "KILLED" if any(rc == 1 for rc in codes.values()) else "SURVIVED"


def _run(node: str) -> tuple[int, list[str]]:
    r = subprocess.run([sys.executable, "-m", "pytest", node, "-q", "-p", "no:asyncio"],
                       capture_output=True, text=True, cwd=REPO)
    fails = sorted({l.split("::")[-1].split()[0] for l in r.stdout.splitlines()
                    if l.startswith("FAILED")})
    return r.returncode, fails


def main() -> int:
    try:
        lock_ctx = battery_lock(REPO)
        lock_ctx.__enter__()
    except BatteryLockBusy as exc:
        print(f"❌ {exc}")
        return 2

    files = {rel: MutatedFile(REPO / rel) for rel in {m[1] for m in MUTANTS}}
    counts = {"KILLED": 0, "SURVIVED": 0, "INVALID": 0, "INFRA": 0}
    nodes = sorted({n for m in MUTANTS for n in m[3]})
    try:
        base = {n: _run(n)[0] for n in nodes}
        if any(rc != 0 for rc in base.values()):
            print(f"❌ 기준선이 초록이 아니다: {base}")
            return 2
        print(f"기준선 초록 · probe {len(nodes)}개 · 변이 {len(MUTANTS)}개\n")
        for name, rel, pairs, probes in MUTANTS:
            handle = files[rel]
            mutated, why = apply_pairs(handle.original, pairs)
            if mutated is None:
                counts["INVALID"] += 1
                print(f"INVALID  {name}\n          ← {rel}: {why}")
                continue
            handle.write_mutant(mutated)
            try:
                codes, killers = {}, {}
                for n in probes:
                    codes[n], killers[n] = _run(n)
            finally:
                handle.restore()
            verdict = classify(codes)
            counts[verdict] += 1
            mark = {"KILLED": "✅", "SURVIVED": "❌", "INFRA": "⚠️"}[verdict]
            print(f"{verdict:<8} {mark} {name}")
            for n in probes:
                if codes[n] == 1:
                    print(f"          ← {n.split('::')[-1]}  {killers[n][:3]}")
    except MutationConflict as exc:
        print(f"\n❌ 충돌 감지 — 복원하지 않고 멈춘다:\n   {exc}")
        return 2
    finally:
        lock_ctx.__exit__(None, None, None)

    print(f"\nkilled={counts['KILLED']} survived={counts['SURVIVED']} "
          f"invalid={counts['INVALID']} infra={counts['INFRA']} / {len(MUTANTS)}")
    return 0 if counts["SURVIVED"] == counts["INVALID"] == counts["INFRA"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
