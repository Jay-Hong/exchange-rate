#!/usr/bin/env python3
"""S2 · M0a — DB engine 설정과 cron installer 변이 배터리.

⚠️ M0a 변이 5종은 한때 **일회성 실행**이었다(codex) — 그때 3/3 killed 를 보고했지만 저장소에
   남지 않아 재현 가능한 게이트가 아니었다. 여기 영속화한다.

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
INSTALLER = "ops/install-db-maintenance-cron.sh"
CRON = "tests/test_db_maintenance_cron_manifest.py"
SQLITE_LEAK = ("tests/test_pg_engine_binding.py::TestSqlitePathNeedsNoPostgres::"
               "test_sqlite_path_does_not_receive_postgres_only_kwargs")

_SQLITE_ARGS = '    _engine_kwargs["connect_args"] = {"check_same_thread": False}\n'

MUTANTS: list[tuple[str, str, list[tuple[str, str]], tuple[str, ...]]] = [
    ("S2-1 PostgreSQL 전용 connect_args 가 SQLite 경로로 누수 → 로컬·테스트가 연결에서 죽는다",
     DB, [(_SQLITE_ARGS,
           '    _engine_kwargs["connect_args"] = {"check_same_thread": False,\n'
           '                                      "options": "-c statement_timeout=1000"}\n')],
     (SQLITE_LEAK,)),
    ('M0a-1 복원 안내를 깨뜨림 → 사람이 복사할 명령이 동작하지 않는다',
     INSTALLER, [("  printf '  복원: %s --restore %s %s\\n' \\\n", "  printf '  복원: false # %s --restore %s %s\\n' \\\n")], (CRON,)),
    ('M0a-2 SHA 검증 제거 → 변조·잘린 백업이 그대로 설치된다',
     INSTALLER, [('  if [ "$got" != "$2" ]; then\n', '  if false; then\n')], (CRON,)),
    ('M0a-3 백업 이름을 초 단위 고정으로 → 같은 초 2회 실행이 앞 백업을 덮는다',
     INSTALLER, [('  backup="$(mktemp "$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S).bak.XXXXXX")"', '  backup="$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S).bak"')], (CRON,)),
    ('M0a-4 --restore 가 락을 건너뜀 → --install 과 같은 상태를 동시에 덮는다',
     INSTALLER, [('do_restore() {   # $1 = backup 파일, $2 = 기대 SHA\n  local got snap pre\n  acquire_write_lock || return 1\n', 'do_restore() {   # $1 = backup 파일, $2 = 기대 SHA\n  local got snap pre\n')], (CRON,)),
    ('M0a-5 crontab -l 오류를 빈 것으로 접음 → 조회 실패가 상태 판정으로 둔갑',
     INSTALLER, [('  if [ "$rc" -ne 0 ]; then\n    echo "❌ crontab -l 실패 (exit $rc): $(head -c 200 "$1.err")" >&2\n    return 1\n  fi\n', '  if [ "$rc" -ne 0 ]; then : >"$1"; fi\n')], (CRON,)),
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
    return cur, "ok"


def syntax_ok(rel: str, text: str) -> tuple[bool, str]:
    """⛔ **대상 언어로** 문법을 본다. 하네스가 Python 대상만 가정해 bash 파일에 `ast.parse` 를
    돌렸고, 유효한 변이가 전부 `INVALID` 로 버려졌다(실측 4건) — 커버리지 구멍을 문법 오류로
    위장하는 셈이다."""
    if rel.endswith(".py"):
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return False, f"변이본 문법 오류(python): {exc}"
        return True, "ok"
    if rel.endswith(".sh"):
        tmp = pathlib.Path(subprocess.run(["mktemp"], capture_output=True, text=True).stdout.strip())
        try:
            tmp.write_text(text)
            r = subprocess.run(["bash", "-n", str(tmp)], capture_output=True, text=True)
            return r.returncode == 0, ("ok" if r.returncode == 0
                                       else f"변이본 문법 오류(bash): {r.stderr.strip()[:120]}")
        finally:
            tmp.unlink(missing_ok=True)
    return True, "ok"   # 그 밖 확장자는 문법 검사를 건너뛴다(주장하지 않는다)


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
            if mutated is not None:
                ok, why2 = syntax_ok(rel, mutated)
                if not ok:
                    mutated, why = None, why2
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
