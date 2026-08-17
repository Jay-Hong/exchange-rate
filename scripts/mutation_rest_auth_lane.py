#!/usr/bin/env python3
"""S1b — REST auth lane **배선·lifecycle** 변이 배터리.

## 왜 필요한가

21개 REST endpoint 테스트는 `verify_firebase_token` 을 **통째로 patch** 한다
(`patch("app.main.verify_firebase_token", new=AsyncMock(...))`). 그래서 helper 안에서 `app=`
이 빠지든 WS lane 으로 새든 그 테스트들은 전부 초록이다. 아래 계약을 잠그는 것은 S1b 가
추가한 테스트들뿐이고, **그게 실제로 무는지**를 여기서 증명한다.

## ⛔ 배타 실행 — 실제 사고에서 나온 요구

감사 workflow 의 병렬 에이전트들에게 "변이를 적용해 pytest 로 확인하고 원복하라" 고 지시했고,
그것들이 **같은 worktree** 에서 동시에 production 파일을 쓰고 읽었다. 무관한 25건이 red 가
됐고, 동시 쓰기로 **복원이 덮여** `init_rest_auth_app()` 이 `pass` 로 남았다.

⛔ 근본 기전은 **작업 격리**다 — 그런 workflow 는 `isolation: "worktree"` 로 띄운다.
   이 배터리의 커널 락과 충돌 감지는 그 위의 **방어층**이지 근본 대책이 아니다.
   상세: `scripts/mutation_battery_guard.py`.

## 판정 규칙

- **probe 분리**: 변이마다 **자기 probe 만** 돌린다. 여러 파일을 한 번에 돌리면 파일 순서에
  따라 실패 노드가 달라져 귀속이 흔들린다(실측). 무관한 probe 를 섞지도 않는다.
- **exit code 로 판정**: 실패가 `subTest` 안이면 `FAILED` 줄이 안 찍힌다. rc 로 본다.
  rc ∉ {0,1} 은 INFRA 이지 SURVIVED 가 아니다.
- **앵커 4중 검증**: 발생 1회 · 줄머리 정렬 · no-op 금지 · 변이본 `ast.parse` 성공.

⚠️ `REST_AUTH_HTTP_TIMEOUT_SECONDS` 의 **값**(10)은 변이하지 않는다 — 그건 측정 전 잠정값이지
   영구 계약이 아니다. 잠그는 것은 `rest-auth` app 이 **REST 전용 config 를 options 로 넘긴다**는
   결속이다.
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
MAIN = "app/main.py"
LANE = "app/auth_executor.py"
FCM = "app/notifications/fcm.py"

WIRING = "tests/test_rest_auth_lane_wiring.py"
MAPPING = "tests/test_firebase_auth_mapping.py"
LIFECYCLE = "tests/test_two_lane_lifecycle.py"
LIFESPAN = "tests/test_lifespan_auth_lane_wiring.py"

_CALL = "auth.verify_id_token, token, app=auth_app, check_revoked=check_revoked"
_GUARD = (
    "    auth_app = rest_auth_app()\n"
    "    if auth_app is None:\n"
    '        raise HTTPException(status_code=503, detail="Firebase auth unavailable")\n'
)
_SUBMIT = "        decoded_token = await auth_executor.run_in_rest_auth_executor(\n"
_SCOPES = (
    "    async with auth_executor.ws_auth_lane_startup_scope(\n"
    "            config.WS_AUTH_EXECUTOR_WORKERS), \\\n"
    "            auth_executor.rest_auth_lane_startup_scope(\n"
    "                config.REST_AUTH_EXECUTOR_WORKERS):\n"
)
_BEGIN_TRY = (
    "        try:\n"
    "            _lane_closing[_lane_name] = _lane_begin()\n"
    "        except BaseException:  # noqa: BLE001 — 다른 lane 종료가 우선이다\n"
    "            _lane_closing[_lane_name] = None\n"
    '            logger.exception("auth lane 종료 시작 실패", extra={"lane": _lane_name})\n'
)
_AWAIT_TRY = (
    "            try:\n"
    "                await _lane_await(_lane_closing.get(_lane_name))\n"
    "            except BaseException:  # noqa: BLE001 — 나머지 lane 합류가 우선이다\n"
    '                logger.exception("auth lane 합류 실패", extra={"lane": _lane_name})\n'
)

# (설명, 대상 파일, [(old, new)], probes)
MUTANTS: list[tuple[str, str, list[tuple[str, str]], tuple[str, ...]]] = [
    ('S1b-1 `app=` 제거 → DEFAULT app 이 쓰여 낮춘 httpTimeout 이 통째로 no-op', 'app/main.py',
     [('auth.verify_id_token, token, app=auth_app, check_revoked=check_revoked', 'auth.verify_id_token, token, check_revoked=check_revoked')], ('tests/test_rest_auth_lane_wiring.py',)),
    ('S1b-2 check_revoked 하드코딩 → /api/user/me 의 revoke 검사가 죽는다', 'app/main.py',
     [('auth.verify_id_token, token, app=auth_app, check_revoked=check_revoked', 'auth.verify_id_token, token, app=auth_app, check_revoked=False')], ('tests/test_rest_auth_lane_wiring.py', 'tests/test_firebase_auth_mapping.py')),
    ('S1b-3 REST lane → WS lane (격리 파괴, 동작은 정상이라 증상 없음)', 'app/main.py',
     [('await auth_executor.run_in_rest_auth_executor(', 'await auth_executor.run_in_auth_executor(')], ('tests/test_rest_auth_lane_wiring.py',)),
    ('S1b-4 전용 pool → asyncio.to_thread (기본 executor 오염)', 'app/main.py',
     [('await auth_executor.run_in_rest_auth_executor(', 'await asyncio.to_thread(')], ('tests/test_rest_auth_lane_wiring.py',)),
    ('S1b-5 rest_auth_app None 가드 제거', 'app/main.py',
     [('    auth_app = rest_auth_app()\n    if auth_app is None:\n        raise HTTPException(status_code=503, detail="Firebase auth unavailable")\n', '    auth_app = rest_auth_app()\n')], ('tests/test_rest_auth_lane_wiring.py',)),
    ('S1b-6 AuthExecutorNotReady 절 무력화 → 재전파(500)로 우리 준비 상태를 서버 버그로 위장', 'app/main.py',
     [('    except auth_executor.AuthExecutorNotReady:\n', '    except _Unreachable:\n'),
      ('import logging\n', 'import logging\nclass _Unreachable(Exception): pass\n')], ('tests/test_rest_auth_lane_wiring.py', 'tests/test_firebase_auth_mapping.py')),
    ('S1b-7 맨 동기 호출로 회귀 → 이벤트 루프를 직접 막는다', 'app/main.py',
     [('        decoded_token = await auth_executor.run_in_rest_auth_executor(\n            auth.verify_id_token, token, app=auth_app, check_revoked=check_revoked\n        )', '        decoded_token = auth.verify_id_token(token, app=auth_app, check_revoked=check_revoked)')], ('tests/test_rest_auth_lane_wiring.py',)),
    ('S1b-8 REST timing_event 를 WS 것으로 오배선 → 두 lane 계측이 한 이름으로 섞인다', 'app/auth_executor.py',
     [('    timing_event="rest_auth_timing",', '    timing_event="ws_auth_timing",')], ('tests/test_two_lane_lifecycle.py',)),
    ('S1b-9 REST 동적 flag 를 WS flag 로 오배선 → REST canary 를 WS 스위치가 지배한다', 'app/auth_executor.py',
     [('    log_timings=lambda: config.REST_AUTH_EXECUTOR_LOG_TIMINGS,', '    log_timings=lambda: config.WS_AUTH_EXECUTOR_LOG_TIMINGS,')], ('tests/test_two_lane_lifecycle.py',)),
    ('S1b-10 REST scope 를 먼저 닫아 마지막 startup 단계가 WS scope 에만 속하게 → 존재 단언은 초록', 'app/main.py',
     [('    async with auth_executor.ws_auth_lane_startup_scope(\n            config.WS_AUTH_EXECUTOR_WORKERS), \\\n            auth_executor.rest_auth_lane_startup_scope(\n                config.REST_AUTH_EXECUTOR_WORKERS):\n', '    async with auth_executor.rest_auth_lane_startup_scope(\n            config.REST_AUTH_EXECUTOR_WORKERS):\n        pass\n    async with auth_executor.ws_auth_lane_startup_scope(\n            config.WS_AUTH_EXECUTOR_WORKERS):\n')], ('tests/test_lifespan_auth_lane_wiring.py',)),
    ('S1b-11 REST app 의 httpTimeout options 제거 → named app 을 만든 이유가 사라진다', 'app/notifications/fcm.py',
     [('                {"httpTimeout": config.REST_AUTH_HTTP_TIMEOUT_SECONDS},\n', '                {},\n')], ('tests/test_two_lane_lifecycle.py', 'tests/test_rest_auth_lane_wiring.py', 'tests/test_firebase_auth_mapping.py')),
    ('S1b-12 REST app 이 WS timeout config 를 쓰도록 교차 배선', 'app/notifications/fcm.py',
     [('                {"httpTimeout": config.REST_AUTH_HTTP_TIMEOUT_SECONDS},\n', '                {"httpTimeout": config.WS_AUTH_HTTP_TIMEOUT_SECONDS},\n')], ('tests/test_two_lane_lifecycle.py', 'tests/test_rest_auth_lane_wiring.py', 'tests/test_firebase_auth_mapping.py')),
    ('S1b-13 `name=REST_AUTH_APP_NAME` 제거 → DEFAULT app 을 다시 초기화한다', 'app/notifications/fcm.py',
     [('                name=REST_AUTH_APP_NAME,\n', '')], ('tests/test_two_lane_lifecycle.py', 'tests/test_rest_auth_lane_wiring.py', 'tests/test_firebase_auth_mapping.py')),
    ('S1b-14 재진입 가드 제거 → 두 번째 호출이 `initialize_app` 중복으로 죽는다', 'app/notifications/fcm.py',
     [('    if _rest_auth_app is not None:\n        return True\n', '')], ('tests/test_two_lane_lifecycle.py', 'tests/test_rest_auth_lane_wiring.py', 'tests/test_firebase_auth_mapping.py')),
    ('S1b-15 shutdown begin loop 의 per-lane try 제거 → 첫 실패가 나머지 lane 종료를 건너뛴다', 'app/main.py',
     [('        try:\n            _lane_closing[_lane_name] = _lane_begin()\n        except BaseException:  # noqa: BLE001 — 다른 lane 종료가 우선이다\n            _lane_closing[_lane_name] = None\n            # ⛔ **진단 로깅도 보호한다.** 여기서 `logger.exception` 이 던지면 loop 가 그대로\n            #    깨져 **다음 lane 은 종료조차 시도되지 않는다** — 격리를 세워놓고 로깅으로\n            #    다시 무너뜨리는 셈이다(S1a′ 에서 같은 계약을 rollback 경로에 잠갔다).\n            try:\n                logger.exception("auth lane 종료 시작 실패", extra={"lane": _lane_name})\n            except BaseException:  # pragma: no cover - 최후 방어\n                pass\n', '        _lane_closing[_lane_name] = _lane_begin()\n')], ('tests/test_lifespan_auth_lane_wiring.py',)),
    ('S1b-16 shutdown await loop 의 per-lane try 제거', 'app/main.py',
     [('            try:\n                await _lane_await(_lane_closing.get(_lane_name))\n            except BaseException:  # noqa: BLE001 — 나머지 lane 합류가 우선이다\n                # ⛔ 위와 같은 이유로 로깅을 보호한다 — 로깅 실패가 다음 lane 합류를 막는다.\n                try:\n                    logger.exception("auth lane 합류 실패", extra={"lane": _lane_name})\n                except BaseException:  # pragma: no cover - 최후 방어\n                    pass\n', '            await _lane_await(_lane_closing.get(_lane_name))\n')], ('tests/test_lifespan_auth_lane_wiring.py',)),
    ('S1b-17 shutdown begin 로깅 보호 제거 → 로깅 실패가 다음 lane 종료를 건너뛴다', 'app/main.py',
     [('            try:\n                logger.exception("auth lane 종료 시작 실패", extra={"lane": _lane_name})\n            except BaseException:  # pragma: no cover - 최후 방어\n                pass\n', '            logger.exception("auth lane 종료 시작 실패", extra={"lane": _lane_name})\n')], ('tests/test_two_lane_lifecycle.py',)),
    ('S1b-18 shutdown await 로깅 보호 제거', 'app/main.py',
     [('                try:\n                    logger.exception("auth lane 합류 실패", extra={"lane": _lane_name})\n                except BaseException:  # pragma: no cover - 최후 방어\n                    pass\n', '                logger.exception("auth lane 합류 실패", extra={"lane": _lane_name})\n')], ('tests/test_two_lane_lifecycle.py',)),
]

# ⛔ 줄머리에서 시작하지 않는 앵커는 **명시적으로** 등록한다 — 그러지 않으면 정렬 검사가
#    올바른 변이까지 INVALID 로 막는다(실측 3건).
MID_LINE_ANCHORS = {
    _CALL,
    "await auth_executor.run_in_rest_auth_executor(",
}


def apply_pairs(source: str, pairs: list[tuple[str, str]]) -> tuple[str | None, str]:
    """변이 적용 — **순수 함수**. 반환 `(결과, 사유)`. 실패면 `(None, 사유)`."""
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
    # ⛔ **전체 no-op** 도 막는다 — pair 별 검사만으로는 두 pair 가 서로를 취소해 최종본이
    #    원본과 같아지는 경우를 놓친다(codex). 그런 변이는 SURVIVED 로 오독된다.
    if cur == source:
        return None, "전체 no-op (최종본이 원본과 같다)"
    try:
        ast.parse(cur)
    except SyntaxError as exc:
        return None, f"변이본 문법 오류: {exc}"
    return cur, "ok"


def classify(codes: dict[str, int]) -> str:
    """probe 별 rc → 판정. ⛔ rc ∉ {0,1} 은 INFRA 다 — SURVIVED 로 세면 거짓 안심이다."""
    if any(rc not in (0, 1) for rc in codes.values()):
        return "INFRA"
    return "KILLED" if any(rc == 1 for rc in codes.values()) else "SURVIVED"


def _run(probe: str) -> tuple[int, list[str]]:
    r = subprocess.run([sys.executable, "-m", "pytest", probe, "-q", "-p", "no:asyncio"],
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
    all_probes = sorted({p for m in MUTANTS for p in m[3]})
    try:
        base = {p: _run(p)[0] for p in all_probes}
        if any(rc != 0 for rc in base.values()):
            print(f"❌ 기준선이 초록이 아니다: {base}")
            return 2
        print(f"기준선 초록 · probe {len(all_probes)}개 · 변이 {len(MUTANTS)}개\n")

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
                for p in probes:
                    codes[p], killers[p] = _run(p)
            finally:
                handle.restore()
            verdict = classify(codes)
            counts[verdict] += 1
            mark = {"KILLED": "✅", "SURVIVED": "❌", "INFRA": "⚠️"}[verdict]
            print(f"{verdict:<8} {mark} {name}")
            for p in probes:
                if codes[p] == 1:
                    print(f"          ← {p.split('/')[-1]}  {killers[p][:3]}")
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
