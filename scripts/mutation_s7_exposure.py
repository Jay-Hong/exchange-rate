#!/usr/bin/env python3
"""S7 — 노출 계약 변이 배터리 (실패 도메인 독립 · flag 합성 · 미러 동일성).

⛔ 종료 코드만 보고 KILLED 라 하지 말 것 — 무변이 기준선 green 선확인 + pytest exit 1(실패) ⊥
   2·3·4·5(인프라) + ast.parse 구문 검사 + try/finally · sha256 복원.
"""

# 표준 라이브러리
import ast
import hashlib
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = REPO / "app" / "main.py"
TESTS = ["tests/test_ws_metrics_exposure.py", "tests/test_topic_auth_rollout_capture.py"]
_TEST_FAILED = 1
_INFRA = {2, 3, 4, 5}

MUTANTS = [
    ("S7-① subscribe_load 를 공유 try 로(도메인 병합)",
     [('    try:\n        metrics["subscribe_load"] = subscribe_load_metrics.subscribe_load_metrics()\n'
       '    except Exception:\n        logger.error("subscribe-load metrics 조회 실패", exc_info=True)\n'
       '        metrics["subscribe_load"] = {"error": "unavailable"}\n',
       '    metrics["subscribe_load"] = subscribe_load_metrics.subscribe_load_metrics()\n')]),
    ("S7-①b executor 미러를 공유 try 로",
     [('    try:\n        metrics["ws_auth_executor"] = auth_executor.auth_executor_metrics()\n'
       '    except Exception:\n        logger.error("ws-auth-executor 미러 조회 실패", exc_info=True)\n'
       '        metrics["ws_auth_executor"] = {"error": "unavailable"}\n',
       '    metrics["ws_auth_executor"] = auth_executor.auth_executor_metrics()\n')]),
    ("S7-② executor endpoint 의 flag 합성 제거",
     [('                "running": auth_executor.is_auth_executor_running(),',
       '                "running": auth_executor.is_auth_executor_running()},\n'
       '        # 아래 flag 제거 변이\n        _unused = {')]),
    ("S7 미러를 수치 복사로(별 소스)",
     [('        metrics["ws_auth_executor"] = auth_executor.auth_executor_metrics()',
       '        metrics["ws_auth_executor"] = {"count": 0, "queued_now": 0, "in_flight": 0}')]),
    ("S7 subscribe_load 블록 누락",
     [('        metrics["subscribe_load"] = subscribe_load_metrics.subscribe_load_metrics()',
       '        metrics["subscribe_load_TYPO"] = subscribe_load_metrics.subscribe_load_metrics()')]),
    ("S7 subscribe_load 실패가 executor 미러를 오염",
     [('        metrics["ws_auth_executor"] = auth_executor.auth_executor_metrics()',
       '        metrics["ws_auth_executor"] = (\n'
       '            {"error": "unavailable"}\n'
       '            if metrics["subscribe_load"].get("error") == "unavailable"\n'
       '            else auth_executor.auth_executor_metrics()\n'
       '        )')]),
    ("S7 executor 실패가 subscribe_load 블록을 오염",
     [('        metrics["ws_auth_executor"] = {"error": "unavailable"}',
       '        metrics["ws_auth_executor"] = {"error": "unavailable"}\n'
       '        metrics["subscribe_load"] = {"error": "unavailable"}')]),
    ("S7 executor fallback 의 flag 제거",
     [('        return {"metrics": None, "running": None, "error": "unavailable",\n'
       '                "topic_dispatcher_enabled": config.TOPIC_DISPATCHER_ENABLED}',
       '        return {"metrics": None, "running": None, "error": "unavailable"}')]),
]


def _run():
    return subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:asyncio", "-x"],
                          capture_output=True, text=True, cwd=REPO)


def main() -> int:
    try:
        import pytest  # noqa: F401
    except ImportError:
        print(f"❌ INFRA: {sys.executable} 에 pytest 가 없다"); return 2
    base = _run()
    if base.returncode != 0:
        print(f"❌ INFRA: 무변이 기준선 exit {base.returncode}"); print(base.stdout[-1200:]); return 2
    print("✅ 기준선 green\n")

    original = MAIN.read_text()
    digest = hashlib.sha256(original.encode()).hexdigest()
    killed = survived = invalid = infra = 0
    try:
        for name, pairs in MUTANTS:
            text, ok = original, True
            for old, new in pairs:
                if old not in text:
                    ok = False
                    break
                text = text.replace(old, new, 1)
            if not ok:
                print(f"  INVALID  {name}  ← 패턴 불일치"); invalid += 1; continue
            try:
                ast.parse(text)
            except SyntaxError as exc:
                print(f"  INVALID  {name}  ← 구문 파괴: {exc.msg}"); invalid += 1; continue
            MAIN.write_text(text)
            r = _run()
            MAIN.write_text(original)
            if r.returncode == _TEST_FAILED:
                hint = next((l for l in r.stdout.splitlines() if l.startswith("FAILED") or "Error" in l), "")
                print(f"  KILLED   {name}  ← {hint[:70]}"); killed += 1
            elif r.returncode in _INFRA:
                print(f"  INFRA    {name}  ← pytest exit {r.returncode}"); infra += 1
            elif r.returncode == 0:
                print(f"  SURVIVED {name}  ⚠️ 테스트가 못 잡는다"); survived += 1
            else:
                print(f"  INFRA    {name}  ← 예상 밖 exit {r.returncode}"); infra += 1
    finally:
        MAIN.write_text(original)
        assert hashlib.sha256(MAIN.read_text().encode()).hexdigest() == digest, "복원 실패"

    print(f"\n  killed={killed} survived={survived} invalid={invalid} infra={infra} / {len(MUTANTS)}")
    return 0 if (survived == 0 and invalid == 0 and infra == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
