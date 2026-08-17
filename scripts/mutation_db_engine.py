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

## PG preflight — 예고했던 그 시점이 왔다

한때 PG preflight 를 넣었다가 뺐다. SQLite 계약을 별 클래스로 분리한 직후라 이미 불필요했고,
수정 둘이 서로의 전제를 무효화한 **과교정**이었다(실측: PG 없이 `rc=1` KILLED). 그때 이렇게
적어 뒀다 — "실제 PG 가 필요한 변이가 생기면 그때 그 변이에 preflight 를 단다."

S2 본체에서 그 변이가 생겼다: **S2-6**(create_engine 이 kwargs 를 안 받음)은 순수 함수 시험을
전부 통과하면서 **배선만 끊는다** → 실제 서버에 물어보는 probe 로만 죽는다. 그 probe 는 PG 가
없으면 skip 되어 rc 0 을 내므로 변이가 `SURVIVED` 로 둔갑한다 — "못 시험했다" 가 "안 잡힌다" 로
보인다. 그래서 `PG_PROBES` 를 두고 **PG 가 없으면 배터리 전체를 거절**한다(no silent caps).
"""
from __future__ import annotations

import ast
import os
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
    # ⚠️ S2 본체에서 SQLite kwargs 도출이 `app/database_settings.py` 로 옮겨가며 이 변이의
    #    앵커가 0회가 됐다(실측 INVALID). **삭제가 아니라 재조준한다** — 계약(PG 전용 인자가
    #    SQLite 경로로 새면 안 된다)은 그대로고 소유자만 바뀌었다.
    ('S2-1 PostgreSQL 전용 connect_args 가 SQLite 경로로 누수 → 로컬·테스트가 연결에서 죽는다',
     'app/database_settings.py',
     [('        return {"connect_args": {"check_same_thread": False}}\n',
       '        return {"connect_args": {"check_same_thread": False,\n'
       '                                 "options": "-c statement_timeout=1000"}}\n')],
     ('tests/test_pg_engine_binding.py::TestSqlitePathNeedsNoPostgres::'
      'test_sqlite_path_does_not_receive_postgres_only_kwargs',)),
    ('M0a-1 복원 안내를 깨뜨림 → 사람이 복사할 명령이 동작하지 않는다', 'ops/install-db-maintenance-cron.sh',
     [("  printf '  복원: %s --restore %s %s\\n' \\\n", "  printf '  복원: false # %s --restore %s %s\\n' \\\n")], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_backup_restores_the_original_exactly',)),
    ('M0a-2 SHA 검증 제거 → 변조·잘린 백업이 그대로 설치된다', 'ops/install-db-maintenance-cron.sh',
     [('  if [ "$got" != "$2" ]; then\n', '  if false; then\n')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_restore_refuses_a_tampered_backup',)),
    ('M0a-3 백업 이름을 초 단위 고정으로 → 같은 초 2회 실행이 앞 백업을 덮는다', 'ops/install-db-maintenance-cron.sh',
     [('  backup="$(mktemp "$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S).bak.XXXXXX")"', '  backup="$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S).bak"')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_two_runs_in_the_same_second_do_not_overwrite_a_backup',)),
    ('M0a-4 --restore 가 락을 건너뜀 → --install 과 같은 상태를 동시에 덮는다', 'ops/install-db-maintenance-cron.sh',
     [('do_restore() {   # $1 = backup 파일, $2 = 기대 SHA\n  local got snap pre\n  acquire_write_lock || return 1\n', 'do_restore() {   # $1 = backup 파일, $2 = 기대 SHA\n  local got snap pre\n')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_every_write_path_acquires_the_lock',)),
    ('M0a-5 crontab -l 오류를 빈 것으로 접음 → 조회 실패가 상태 판정으로 둔갑', 'ops/install-db-maintenance-cron.sh',
     [('  if [ "$rc" -ne 0 ]; then\n    echo "❌ crontab -l 실패 (exit $rc): $(head -c 200 "$1.err")" >&2\n    return 1\n  fi\n', '  if [ "$rc" -ne 0 ]; then : >"$1"; fi\n')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_crontab_read_failure_is_not_treated_as_empty',)),
    ("M0a-6 복원 사후조건 제거 → 쓰기가 반영 안 돼도 '복원 완료'", 'ops/install-db-maintenance-cron.sh',
     [('  local post\n', '  local post\n  if true; then echo "✅ 복원 완료: $1"; return 0; fi\n')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_restore_fails_when_the_write_is_not_reflected',)),
    ('M0a-7 안내를 상대 경로로 → 다른 디렉터리에서 exit 127', 'ops/install-db-maintenance-cron.sh',
     [('    "$(printf \'%q\' "$SELF")" "$(printf \'%q\' "$backup")" "$after"\n', '    "$(printf \'%q\' "${BASH_SOURCE[0]}")" "$(printf \'%q\' "$backup")" "$after"\n')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_restore_hint_is_an_absolute_path',)),
    ('M0a-8 pre-restore 백업 제거 → 복원이 무관 변경을 되돌릴 길이 없다', 'ops/install-db-maintenance-cron.sh',
     [('  cp "$pre" "$pre_backup"; chmod 600 "$pre_backup"\n', '  : \n')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_restore_takes_a_pre_restore_backup',)),
    ('M0a-9 복원 TOCTOU — 검증한 스냅샷이 아니라 원본 경로를 설치', 'ops/install-db-maintenance-cron.sh',
     [('  crontab "$snap"\n\n  # ⛔ **exit 0 은 설치의 증거가 아니다.**', '  crontab "$1"\n\n  # ⛔ **exit 0 은 설치의 증거가 아니다.**')], ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::test_restore_installs_the_verified_snapshot_not_the_original_path',)),
    # ⚠️ 아래 셋은 외부 검토가 **기존 변이가 못 겨눈 경로**를 찾아 추가된 것이다.
    #    M0a-8 은 pre-restore 백업 **파일**을, M0a-1 은 *설치* 백업의 **안내**를 덮는데,
    #    pre-restore 백업의 **안내**는 어느 쪽도 안 덮었다(실측: 파일 전체 26 passed).
    ('M0a-10 pre-restore 되돌리기 안내를 깨뜨림 → 복원을 되돌릴 길이 사라져도 아무도 모른다',
     'ops/install-db-maintenance-cron.sh',
     [("  printf '  되돌리기: %s --restore %s %s\\n' \\\n",
       "  printf '  되돌리기: false # %s --restore %s %s\\n' \\\n")],
     ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::'
      'test_the_pre_restore_hint_actually_undoes_the_restore',)),
    # ⚠️ **양성 대조**다. T2 는 한때 `assertNotIn("TAMPERED", …)` 하나뿐이라 복원이 아무것도
    #    설치하지 않아도 통과했다 — 지정 노드가 계약을 고정하지 못했다. 강화된 T2 가 조기
    #    실패를 실제로 잡는지 이 변이가 증명한다(파일 전체가 잡는 것과는 다른 주장이다).
    ('M0a-11 복원 설치를 **조용한 no-op** 으로 → 강화 전 T2 는 통과했다(양성 대조)',
     'ops/install-db-maintenance-cron.sh',
     [('  crontab "$snap"\n', '  true\n')],
     ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::'
      'test_restore_installs_the_verified_snapshot_not_the_original_path',)),
    ('M0a-12 pre-restore 안내를 상대 경로로 → 다른 디렉터리에서 되돌리기가 exit 127',
     'ops/install-db-maintenance-cron.sh',
     [('    "$(printf \'%q\' "$SELF")" "$(printf \'%q\' "$pre_backup")" "$pre_sha"\n',
       '    "$(printf \'%q\' "${BASH_SOURCE[0]}")" "$(printf \'%q\' "$pre_backup")" "$pre_sha"\n')],
     ('tests/test_db_maintenance_cron_manifest.py::TestInstallerDurability::'
      'test_the_pre_restore_hint_is_an_absolute_path',)),
    # ── S2 본체: DB_WORKLOAD_PROFILE + timeout 3종 ──────────────────────────────
    ('S2-2 알 수 없는 profile 을 online 으로 폴백 → 오타가 조용히 통과하고 maintenance 가 잘린다',
     'app/database_settings.py',
     [('    if raw in PROFILES:\n        return raw\n',
       '    if raw in PROFILES:\n        return raw\n    return ONLINE\n')],
     ('tests/test_db_workload_profile.py::TestProfileResolution::'
      'test_everything_else_is_a_startup_failure',)),
    ('S2-3 대소문자·공백 관용 → 틀린 값이 정상처럼 통과한다',
     'app/database_settings.py',
     [('    if raw in PROFILES:\n        return raw\n    raise InvalidDbWorkloadProfile(\n',
       '    if raw.strip().lower() in PROFILES:\n        return raw.strip().lower()\n'
       '    raise InvalidDbWorkloadProfile(\n')],
     ('tests/test_db_workload_profile.py::TestProfileResolution::'
      'test_everything_else_is_a_startup_failure',)),
    ('S2-4 빈 문자열을 미지정으로 간주 → 절반만 고친 설정이 online 으로 통과',
     'app/database_settings.py',
     [('    if raw is None:\n        return ONLINE\n', '    if not raw:\n        return ONLINE\n')],
     ('tests/test_db_workload_profile.py::TestProfileResolution::'
      'test_everything_else_is_a_startup_failure',)),
    ('S2-5 online/maintenance statement 상한 교차배선 → 온라인이 15분을 허용한다',
     'app/database_settings.py',
     [('STATEMENT_TIMEOUT_MS = {ONLINE: 60_000, MAINTENANCE: 900_000}\n',
       'STATEMENT_TIMEOUT_MS = {ONLINE: 900_000, MAINTENANCE: 10_000}\n')],
     ('tests/test_db_workload_profile.py::TestEngineKwargs::'
      'test_maintenance_is_strictly_more_permissive_than_online',)),
    ('S2-6 create_engine 이 kwargs 를 안 받음 → 순수 함수는 맞는데 **배선이 끊긴다**',
     'app/database.py',
     [('engine = create_engine(DATABASE_URL, **_engine_kwargs)\n',
       'engine = create_engine(DATABASE_URL)\n')],
     ('tests/test_pg_engine_binding.py::TestWorkloadProfileReachesTheServer::'
      'test_each_profile_lands_all_three_timeouts_on_a_live_connection',)),
    ('S2-7 profile 검증을 건너뛰고 raw env 를 그대로 사용 → 기동이 안 죽는다',
     'app/database.py',
     [('DB_WORKLOAD_PROFILE = database_settings.resolve_profile_from_env()\n',
       'DB_WORKLOAD_PROFILE = os.getenv("DB_WORKLOAD_PROFILE") or "online"\n')],
     ('tests/test_pg_engine_binding.py::TestInvalidProfileIsAStartupFailure::'
      'test_bad_profiles_kill_the_import_before_the_engine_exists',)),
    ('S2-8 backend 판정을 문자열 포함으로 회귀 → 이름에 sqlite 가 든 PG URL 이 오판된다',
     'app/database_settings.py',
     [('    return make_url(url).get_backend_name() == "sqlite"\n',
       '    return "sqlite" in url\n')],
     ('tests/test_db_workload_profile.py::TestBackendDiscrimination::'
      'test_a_postgres_url_whose_name_contains_sqlite_is_not_sqlite',)),
    ('S2-9 statement 상한을 0 으로 → PostgreSQL 에서 0 은 **무제한**이다',
     'app/database_settings.py',
     [('STATEMENT_TIMEOUT_MS = {ONLINE: 60_000, MAINTENANCE: 900_000}\n',
       'STATEMENT_TIMEOUT_MS = {ONLINE: 0, MAINTENANCE: 0}\n')],
     ('tests/test_db_workload_profile.py::TestEngineKwargs::test_no_timeout_is_zero',)),
    # ⚠️ 이 변이는 호출을 **없애지 않는다** — 없애면 S2-7 과 같은 결함이 되고 순서 계약이
    #    아니라 존재 계약을 시험하게 된다. 호출 수는 그대로 두고 **위치만** 뒤로 옮긴다.
    ('S2-10 검증을 create_engine **뒤로** 이동 → 잘못된 profile 로 engine 이 먼저 만들어진다',
     'app/database.py',
     [('DB_WORKLOAD_PROFILE = database_settings.resolve_profile_from_env()\n',
       '_early_profile = "online"\n'),
      ('_engine_kwargs = database_settings.engine_kwargs(DATABASE_URL, DB_WORKLOAD_PROFILE)\n',
       '_engine_kwargs = database_settings.engine_kwargs(DATABASE_URL, _early_profile)\n'),
      ('engine = create_engine(DATABASE_URL, **_engine_kwargs)\n',
       'engine = create_engine(DATABASE_URL, **_engine_kwargs)\n'
       'DB_WORKLOAD_PROFILE = database_settings.resolve_profile_from_env()\n')],
     ('tests/test_db_workload_profile.py::TestValidationPrecedesEngineCreation::'
      'test_profile_is_resolved_before_create_engine_in_source_order',)),
]

#: ⛔ 실제 PostgreSQL 이 있어야만 판정되는 probe. 없으면 그 테스트는 **skip → rc 0** 이라
#:    변이가 `SURVIVED` 로 둔갑한다 — "못 시험했다" 가 "안 잡힌다" 로 보인다.
#:    그래서 조용히 건너뛰지 않고 **배터리 전체를 거절**한다(no silent caps).
PG_PROBES = frozenset({
    "tests/test_pg_engine_binding.py::TestWorkloadProfileReachesTheServer::"
    "test_each_profile_lands_all_three_timeouts_on_a_live_connection",
})

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
    # ⛔ 락보다 **먼저** 한다 — read-only 판정이라 락이 필요 없고, 락 안에서 거절하면
    #    거절 사유가 "다른 배터리가 실행 중" 처럼 보일 수 있다.
    needed = sorted({n for m in MUTANTS for n in m[3] if n in PG_PROBES})
    if needed and not os.getenv("PG_TEST_URL", "").strip():
        print("❌ PG_TEST_URL 이 없다 — 아래 변이는 **판정 자체가 불가능**하다.\n"
              "   그 probe 는 PG 없이 skip 되어 rc 0 을 내므로 SURVIVED 로 둔갑한다.\n"
              "   조용히 건너뛰면 없는 커버리지를 믿게 되므로 배터리를 거절한다.")
        for n in needed:
            print(f"   - {n.split('::')[-1]}")
        print("   예) PG_TEST_URL='postgresql+psycopg://postgres:…@localhost:54320/fxi_ci'")
        return 2

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
