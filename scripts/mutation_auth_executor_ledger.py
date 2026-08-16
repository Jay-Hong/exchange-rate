#!/usr/bin/env python3
"""S5 — auth_executor 제출/시작 장부 변이 배터리.

⛔ **종료 코드만 보고 KILLED 라 하지 말 것.** 인터프리터에 pytest 가 없거나 수집이 깨져도
   비-0 이 나와 *모든* 변이가 KILLED 로 보인다 — 판정기가 무엇을 넣어도 통과하는 형태가 된다
   (codex 지적). 그래서 이 하네스는:
     1. **무변이 기준선**을 먼저 돌려 exit 0 이 아니면 즉시 중단하고,
     2. pytest 종료 코드를 **테스트 실패(1)** 와 **인프라 오류(2·3·4·5)** 로 가르며,
     3. 패턴 불일치(INVALID)를 통과가 아니라 **실패**로 센다.
   복원은 `try/finally` + sha256 대조로 보증한다.

사용: python scripts/mutation_auth_executor_ledger.py
"""

# 표준 라이브러리
import ast
import hashlib
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "app" / "auth_executor.py"
TESTS = ["tests/test_auth_executor_ledger.py", "tests/test_auth_executor.py"]

# pytest 종료 코드: 0 통과 · 1 테스트 실패 · 2 중단 · 3 내부오류 · 4 사용오류 · 5 수집 0건
_TEST_FAILED = 1
_INFRA = {2, 3, 4, 5}

MUTANTS: list[tuple[str, list[tuple[str, str]]]] = [
    ("① 예약을 submit 뒤로",
     [("    _note_submitted()               # ⛔ submit **앞**(변이 ①) — 그리고 **시계보다도 앞**\n", ""),
      ("    _safe_ledger(_note_submit_succeeded)", "    _note_submitted()\n    _safe_ledger(_note_submit_succeeded)")]),
    ("② submit 실패 상계 제거",
     [("        _note_submit_failed()       # 장부 상계 후 그대로 재전파 (변이 ②)\n        raise", "        raise")]),
    ("③ in_flight 감소를 _record 밖으로",
     [('        if worker_finished:\n'
       '            if _metrics["in_flight"] <= 0:   # ⛔ 음수면 reset 가드(`> 0`)가 영구히 무력해진다\n'
       '                raise RuntimeError("in_flight underflow — worker_finished 가 잘못 전달됐다")\n'
       '            _metrics["in_flight"] -= 1\n', "")]),
    ('④ reset 이 max 를 0 으로',
     [('    new["in_flight_max"] = new["in_flight"]\n', '    new["in_flight_max"] = 0\n')]),
    ('④b reset 이 in_flight 까지 0 으로',
     [('    new["in_flight_max"] = new["in_flight"]\n', '    new["in_flight"] = 0\n    new["in_flight_max"] = new["in_flight"]\n')]),
    ('⑥ reset 에서 신규 카운터 누락',
     [('               submitted_total=0, submit_failed_total=0, started_total=0,\n               ledger_errors_total=0, by_outcome={})\n', '               by_outcome={})\n')]),
    ("⑦ peak 을 제출 시점에 갱신",
     [('def _note_submitted() -> None:\n    with _metrics_lock:\n        _metrics["submitted_total"] += 1',
       'def _note_submitted() -> None:\n    with _metrics_lock:\n        _metrics["submitted_total"] += 1\n'
       '        q = _queued_locked()\n'
       '        if q > _metrics["queued_observed_max"]: _metrics["queued_observed_max"] = q')]),
    ('⑧ 활성 중 reset 거부 제거',
     [('        # ⚠️ `!= 0` 이다 — 음수도 거부한다. 지켜지는 것은 **불균형** 신호이지 `ledger_errors_total`\n        #    이 아니다(균형이 맞으면 그 카운터는 reset 과 함께 0 으로 내려간다).\n        if _metrics["in_flight"] > 0 or _queued_locked() != 0:\n            raise AuthExecutorMetricsBusy(\n                f"활성 작업 중 reset 거부 (in_flight={_metrics[\'in_flight\']}, "\n                f"queued={_queued_locked()}, ledger_errors={_metrics[\'ledger_errors_total\']})")\n', '')]),
    ('High2 장부 기록을 started_evt 앞으로',
     [('            started_evt.set()\n            start_receipt: list[bool] = []\n            _safe_ledger(lambda: _note_worker_started(start_receipt))   # 실패해도 업무는 계속\n', '            start_receipt: list[bool] = []\n            _safe_ledger(lambda: _note_worker_started(start_receipt))   # 실패해도 업무는 계속\n            started_evt.set()\n')]),
    ("High3a 시계를 장부 앞으로",
     [("        # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가\n        #    영구 잔존한다(codex 재현). 경계는 그대로: Event·클로저 생성 **앞**.\n        submitted = time.monotonic()\n", ""),
      ("    _note_submitted()               # ⛔ submit **앞**(변이 ①) — 그리고 **시계보다도 앞**\n", "    submitted = time.monotonic()\n    _note_submitted()               # ⛔ submit **앞**(변이 ①) — 그리고 **시계보다도 앞**\n")]),
    ("High3b execution 을 dequeue 시각부터",
     [("(time.monotonic() - exec_started) * 1000.0", "(time.monotonic() - dequeued) * 1000.0")]),
    ("High3c 시계를 Event 생성 뒤로",
     [("        # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가\n        #    영구 잔존한다(codex 재현). 경계는 그대로: Event·클로저 생성 **앞**.\n        submitted = time.monotonic()\n", ""),
      ("        finished_evt = threading.Event()\n",
       "        finished_evt = threading.Event()\n        submitted = time.monotonic()\n")]),
    ("Major-A Event 생성을 보상 범위 밖으로",
     [("        started_evt = threading.Event()\n        finished_evt = threading.Event()\n", ""),
      ("    try:\n        # ⚠️ 시계도", "    started_evt = threading.Event()\n    finished_evt = threading.Event()\n    try:\n        # ⚠️ 시계도")]),
    ("Major-B 제출 후 부기를 치명으로",
     [("    _safe_ledger(_note_submit_succeeded)", "    _note_submit_succeeded()")]),
    ('Major-C 시계를 보상 범위 밖으로',
     [('    try:\n        # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가', '    submitted = time.monotonic()\n    try:\n        # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가'),
      ('        # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가\n        #    영구 잔존한다(codex 재현). 경계는 그대로: Event·클로저 생성 **앞**.\n        submitted = time.monotonic()\n', '')]),
    ("Major-D 진단 로깅 보호 제거",
     [('        try:                                    # ⛔ 진단 로깅 실패가 업무 결과를 대체하면 안 된다\n'
       '            logger.debug("auth_executor 장부 기록 실패", exc_info=True)\n'
       '        except Exception:                       # pragma: no cover - 최후 방어\n'
       '            pass\n',
       '        logger.debug("auth_executor 장부 기록 실패", exc_info=True)\n')]),
    ("Major-E receipt 대신 반환값으로 커밋 판정",
     [("        receipt.append(True)", "        pass"),
      ("            started_ok = bool(start_receipt)",
       "            started_ok = _safe_ledger(lambda: _note_worker_started(start_receipt))")]),
    ('Major-F helper 를 순차 in-place 갱신으로',
     [('        new = dict(_metrics)\n        new["started_total"] += 1\n        new["in_flight"] += 1\n        new["in_flight_max"] = max(new["in_flight_max"], new["in_flight"])\n        # ⚠️ receipt 는 커밋 **직전**. 이 뒤 update 가 실패하면 카운터는 안 올랐는데 receipt 만\n        #    남지만, 그 경우는 `_record` 의 underflow 가드가 무해하게 흡수한다(음수 방지).\n        receipt.append(True)\n        _metrics = new              # ⛔ **단일 리바인딩**. `update()` 는 기존 dict 를 제자리\n                                    #    갱신해 부분 실패가 가능하다 — copy-and-swap 이 아니다.\n', '        _metrics["started_total"] += 1\n        _metrics["in_flight"] += 1\n        if _metrics["in_flight"] > _metrics["in_flight_max"]:\n            _metrics["in_flight_max"] = _metrics["in_flight"]\n        receipt.append(True)\n')]),
    ('Major-G 리바인딩을 제자리 update 로',
     [('        _metrics = new              # ⛔ **단일 리바인딩**. `update()` 는 기존 dict 를 제자리\n                                    #    갱신해 부분 실패가 가능하다 — copy-and-swap 이 아니다.\n', '        _metrics.update(new)\n')]),
    ('Major-H 장부 오염 시 queue 무시(완화 재도입)',
     [('        if _metrics["in_flight"] > 0 or _queued_locked() != 0:\n', '        trust_queue = _metrics["ledger_errors_total"] == 0\n        if _metrics["in_flight"] > 0 or (trust_queue and _queued_locked() > 0):\n')]),
    ('Major-I drain 미증명 force 경로 재도입',
     [('        # ⚠️ `!= 0` 이다 — 음수도 거부한다. 지켜지는 것은 **불균형** 신호이지 `ledger_errors_total`\n', '        if _executor is None:          # 변이: drain 미증명 bypass 재도입\n            _reset_locked()\n            return\n        # ⚠️ `!= 0` 이다 — 음수도 거부한다. 지켜지는 것은 **불균형** 신호이지 `ledger_errors_total`\n')]),
    ("(c) in_flight underflow 가드 제거",
     [('            if _metrics["in_flight"] <= 0:   # ⛔ 음수면 reset 가드(`> 0`)가 영구히 무력해진다\n'
       '                raise RuntimeError("in_flight underflow — worker_finished 가 잘못 전달됐다")\n', "")]),
]


def _run() -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:asyncio", "-x"],
                          capture_output=True, text=True, cwd=REPO)


def main() -> int:
    original = SRC.read_text()
    digest = hashlib.sha256(original.encode()).hexdigest()

    try:
        import pytest  # noqa: F401
    except ImportError:
        print(f"❌ INFRA: {sys.executable} 에 pytest 가 없다 — 이대로 돌리면 모든 변이가 KILLED 로 보인다")
        return 2

    baseline = _run()
    if baseline.returncode != 0:
        print(f"❌ INFRA: 무변이 기준선이 exit {baseline.returncode} — 변이 판정 불가")
        print(baseline.stdout[-1500:])
        return 2
    print("✅ 기준선 green (무변이 exit 0)\n")

    killed = survived = invalid = infra = 0
    try:
        for name, pairs in MUTANTS:
            mutated, ok = original, True
            for old, new in pairs:
                if old not in mutated:
                    ok = False
                    break
                mutated = mutated.replace(old, new, 1)
            if not ok:
                print(f"  INVALID  {name}  ← 패턴 불일치(코드가 이동했다 — 재조준 필요)")
                invalid += 1
                continue
            try:                                  # ⛔ 컴파일도 안 되는 변이는 테스트를 시험한 게 아니다
                ast.parse(mutated)                #    (SyntaxError 가 KILLED 로 보였다 — 실측)
            except SyntaxError as exc:
                print(f"  INVALID  {name}  ← 변이가 구문을 깨뜨린다: {exc.msg}")
                invalid += 1
                continue
            SRC.write_text(mutated)
            r = _run()
            if r.returncode == _TEST_FAILED:
                hint = next((ln for ln in r.stdout.splitlines()
                             if ln.startswith("FAILED") or "Error:" in ln), "")
                print(f"  KILLED   {name}  ← {hint[:78]}")
                killed += 1
            elif r.returncode in _INFRA:
                print(f"  INFRA    {name}  ← pytest exit {r.returncode} (테스트 실패 아님)")
                infra += 1
            elif r.returncode == 0:
                print(f"  SURVIVED {name}  ⚠️ 테스트가 이 변이를 못 잡는다")
                survived += 1
            else:
                print(f"  INFRA    {name}  ← 예상 밖 exit {r.returncode}")
                infra += 1
    finally:
        SRC.write_text(original)
        assert hashlib.sha256(SRC.read_text().encode()).hexdigest() == digest, "원본 복원 실패"

    print(f"\n  killed={killed} survived={survived} invalid={invalid} infra={infra} / {len(MUTANTS)}")
    return 0 if (survived == 0 and invalid == 0 and infra == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
