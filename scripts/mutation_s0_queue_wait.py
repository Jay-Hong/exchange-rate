#!/usr/bin/env python3
"""S0 queue_wait 축 변이 배터리.

⛔ pytest exit 1(테스트 실패)만 KILLED — 2·3·4·5 는 인프라이고 그걸 KILLED 로 세면
   배터리가 자기를 속인다.
⛔ ast.parse 로 구문 파괴 변이를 INVALID 로 거른다. INVALID 는 **실패로 센다**.
⛔ 앵커는 대상 안에서 유일해야 한다(유일하지 않으면 판별력을 잃는다 — C3 에서 실측).
⛔ try/finally + sha256 복원.
"""
import ast, hashlib, pathlib, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SLM = REPO / "app" / "subscribe_load_metrics.py"
SNAP = REPO / "app" / "topic_initial_snapshot.py"
MAIN = REPO / "app" / "main.py"
CAP = REPO / "ops" / "capture_topic_auth_rollout.py"
TESTS = ["tests/test_subscribe_load_metrics.py", "tests/test_topic_auth_rollout_capture.py",
         "tests/test_topic_initial_snapshot_e2e.py", "tests/test_ws_metrics_exposure.py"]

WRAP_CALL = "        payload = await build_snapshot_observed(topic)"

MUTANTS = [
    # ── 계약 ──────────────────────────────────────────────────────────
    ("CONTRACT_VERSION 을 /5 로 되돌림", SLM,
     'CONTRACT_VERSION = "subscribe-load/6"', 'CONTRACT_VERSION = "subscribe-load/5"'),
    ("캡처기가 /6 를 수용하지 않음", CAP,
     'SUBSCRIBE_LOAD_CONTRACT = "subscribe-load/6"',
     'SUBSCRIBE_LOAD_CONTRACT = "subscribe-load/5"'),
    ("캡처기 subscribe_load 검증 배선 제거", CAP,
     "        metric_schema_errors.extend(_subscribe_load_schema_errors(raw_body))\n", ""),
    # ── 배선 ──────────────────────────────────────────────────────────
    ("REST twin 배선 제거(구 직접 to_thread 복원)", MAIN,
     WRAP_CALL, "        payload = await asyncio.to_thread(_build_snapshot_sync, topic)"),
    ("WS 이중 계수(래퍼를 두 번 호출)", SNAP,
     "            payload = await build_snapshot_observed(topic)\n",
     "            payload = await build_snapshot_observed(topic)\n"
     "            payload = await build_snapshot_observed(topic)\n"),
    # ── 측정 지점 ─────────────────────────────────────────────────────
    ("제출 시각을 to_thread **뒤**로 (queue_wait 정의 파괴)", SNAP,
     "        load.mark_submitted()\n        payload = await asyncio.to_thread(",
     "        payload = await asyncio.to_thread("),
    # ⛔ 저장 스키마(_blank)만 오염시키는 변이는 **무해**하다 — 노출 투영이 걸러낸다.
    #    계약 표면은 투영이므로 거기를 노린다(변이 선택 자체가 판별력의 일부다).
    ("caller-only 축에 worker 필드를 노출", SLM,
     '            if axis in WORKER_AXES:\n                snap["worker_started_total"]',
     '            if True:\n                snap["worker_started_total"]'),
    ("노출 투영에서 queue_wait 제거(저장만 하고 안 보임)", SLM,
     '                snap["queue_wait_ms_max"] = block["queue_wait_ms_max"]\n', ""),
    ("미관측을 0 으로 채움(계기 없음 = 대기 없음)", SLM,
     "    queue_wait_ms: Optional[float] = None\n", "    queue_wait_ms: Optional[float] = 0.0\n"),
]


def run() -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:asyncio", *TESTS],
                          cwd=REPO, capture_output=True, text=True).returncode


def main() -> int:
    files = [SLM, SNAP, MAIN, CAP]
    orig = {f: f.read_bytes() for f in files}
    sig = {f: hashlib.sha256(b).hexdigest() for f, b in orig.items()}
    base = run()
    if base != 0:
        print(f"⛔ 기준선이 green 이 아니다(rc={base}) — 배터리 무효")
        return 2
    print("기준선 green (rc=0)\n")
    killed = survived = invalid = infra = 0
    try:
        for name, target, old, new in MUTANTS:
            text = target.read_text()
            if text.count(old) != 1:
                print(f"INVALID  {name}  ← 앵커 {text.count(old)}회"); invalid += 1; continue
            target.write_text(text.replace(old, new))
            try:
                ast.parse(target.read_text())
            except SyntaxError as exc:
                print(f"INVALID  {name}  ← 구문 파괴 {exc}"); invalid += 1
                target.write_bytes(orig[target]); continue
            rc = run()
            verdict = "KILLED" if rc == 1 else "SURVIVED" if rc == 0 else f"INFRA(rc={rc})"
            if rc == 1: killed += 1
            elif rc == 0: survived += 1
            else: infra += 1
            print(f"{verdict:9} {name}")
            target.write_bytes(orig[target])
    finally:
        for f in files: f.write_bytes(orig[f])
        bad = [f.name for f in files if hashlib.sha256(f.read_bytes()).hexdigest() != sig[f]]
        print(f"\n복원 {'✅ 전부 일치' if not bad else '⛔ 불일치 ' + str(bad)}")
    print(f"killed={killed} survived={survived} invalid={invalid} infra={infra} / {len(MUTANTS)}")
    return 0 if survived == invalid == infra == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
