#!/usr/bin/env python3
"""S6 — arrival ring · 비소멸 peak · 관측 에포크 high-water 변이 배터리.

⛔ 종료 코드만 보고 KILLED 라 하지 말 것 — pytest 부재·수집 실패도 비-0 이라 모든 변이가
   KILLED 로 보인다. 무변이 기준선 green 선확인 + pytest exit 1(실패) ⊥ 2·3·4·5(인프라) +
   ast.parse 로 구문 파괴 변이 제외 + try/finally · sha256 복원.
"""

# 표준 라이브러리
import ast
import pathlib
import subprocess
import sys

from mutation_battery_guard import (
    BatteryLockBusy,
    MutatedFile,
    battery_lock,
    isolated_worktree,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = ["tests/test_topic_auth_rollout.py", "tests/test_ws_connection_metrics.py",
         "tests/test_topic_auth_rollout_capture.py"]
_TEST_FAILED = 1
_INFRA = {2, 3, 4, 5}

ROLLOUT = REPO / "app" / "topic_auth_rollout.py"
METRICS = REPO / "app" / "ws_connection_metrics.py"
EXPECTED_T = REPO / "tests" / "test_topic_auth_rollout.py"
CAPTURE = REPO / "ops" / "capture_topic_auth_rollout.py"

MUTANTS: list[tuple[str, pathlib.Path, list[tuple[str, str]]]] = [
    ("S6-1 peak 갱신 제거", METRICS,
     [("        if self._counts[index] > self._peak_since_start:\n"
       "            self._peak_since_start = self._counts[index]\n", "")]),
    ("S6-1b peak 을 보존 버킷 최댓값으로(=eviction 에 종속)", METRICS,
     [('            "peak_in_bucket_since_start": self._peak_since_start,',
       '            "peak_in_bucket_since_start": max(self._counts.values(), default=0),')]),
    ("S6-2 익명 ring 을 topic 루프 안으로", ROLLOUT,
     [("        self._anonymous_attempts += 1\n        self._anonymous_arrival.record()\n",
       "        self._anonymous_attempts += 1\n"),
      ("            self._per_topic_attempts[topic] += 1\n",
       "            self._per_topic_attempts[topic] += 1\n            self._anonymous_arrival.record()\n")]),
    ("S6-2b token-bearing ring 을 topic 루프 안으로", ROLLOUT,
     [("        self._tb_attempts += 1\n        self._tb_arrival.record()\n",
       "        self._tb_attempts += 1\n"),
      ("            self._tb_per_topic_attempts[topic] += 1\n",
       "            self._tb_per_topic_attempts[topic] += 1\n"
       "            self._tb_arrival.record()\n")]),
    ("S6-3 high-water 를 연결 관측에만(=disconnect 로 소실)", ROLLOUT,
     [("            \"unverified_token_bearing_attempts_on_one_observation_epoch_max\": (\n"
       "                self._tb_attempts_on_one_epoch_max),",
       "            \"unverified_token_bearing_attempts_on_one_observation_epoch_max\": max(\n"
       "                (o.token_bearing_attempts for o in self._seen_by_connection.values()),\n"
       "                default=0),")]),
    ("S6-3b high-water 를 누적 총계로", ROLLOUT,
     [("        observation.token_bearing_attempts += 1\n"
       "        if observation.token_bearing_attempts > self._tb_attempts_on_one_epoch_max:\n"
       "            self._tb_attempts_on_one_epoch_max = observation.token_bearing_attempts",
       "        observation.token_bearing_attempts += 1\n"
       "        self._tb_attempts_on_one_epoch_max += 1")]),
    ("S6-4 EXPECTED 에서 신규 3키 제거", EXPECTED_T,
     [('        "anonymous_subscribe_arrival",\n'
       '        "unverified_token_bearing_subscribe_arrival",\n'
       '        "unverified_token_bearing_attempts_on_one_observation_epoch_max",', "")]),
    ("S6-5 기존 max_in_bucket 을 비소멸로 오염", METRICS,
     [('            "max_in_bucket": max(self._counts.values(), default=0),',
       '            "max_in_bucket": self._peak_since_start,')]),
    ("ring 공통 시각 해제(두 ring 이 다른 monotonic)", ROLLOUT,
     [('            "anonymous_subscribe_arrival": self._anonymous_arrival.snapshot(now=arrival_now),\n'
       '            "unverified_token_bearing_subscribe_arrival": self._tb_arrival.snapshot(now=arrival_now),',
       '            "anonymous_subscribe_arrival": self._anonymous_arrival.snapshot(),\n'
       '            "unverified_token_bearing_subscribe_arrival": self._tb_arrival.snapshot(),')]),
    ("capture arrival 검증 호출 제거", CAPTURE,
     [("    errors.extend(_arrival_schema_errors(rollout))\n", "")]),
    ("capture bucket identity 검증 제거", CAPTURE,
     [("        if (\n"
       "            values[\"bucket_seconds\"] != ARRIVAL_BUCKET_SECONDS\n"
       "            or values[\"buckets_kept\"] != ARRIVAL_BUCKETS_KEPT\n"
       "        ):\n"
       "            errors.append(\n"
       "                f\"{field} bucket identity must be \"\n"
       "                f\"{ARRIVAL_BUCKET_SECONDS}s x {ARRIVAL_BUCKETS_KEPT}\"\n"
       "            )\n", "")]),
    ("production/capture bucket identity 결속 해제", METRICS,
     [("HANDSHAKE_BUCKET_SECONDS = 10\n", "HANDSHAKE_BUCKET_SECONDS = 11\n")]),
]


def _run(cwd: pathlib.Path):
    return subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:asyncio", "-x"],
                          capture_output=True, text=True, cwd=cwd)


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
    try:
        import pytest  # noqa: F401
    except ImportError:
        print(f"❌ INFRA: {sys.executable} 에 pytest 가 없다"); return 2
    base = _run(work)
    if base.returncode != 0:
        print(f"❌ INFRA: 무변이 기준선 exit {base.returncode}"); print(base.stdout[-1200:]); return 2
    print("✅ 기준선 green\n")

    handles = {
        path: MutatedFile(work / path.relative_to(REPO))
        for path in {ROLLOUT, METRICS, EXPECTED_T, CAPTURE}
    }
    originals = {path: handle.original for path, handle in handles.items()}
    killed = survived = invalid = infra = 0
    try:
        for name, target, pairs in MUTANTS:
            text, ok = originals[target], True
            for old, new in pairs:
                if old not in text:
                    ok = False
                    break
                text = text.replace(old, new, 1)
            if not ok:
                print(f"  INVALID  {name}  ← 패턴 불일치(코드 이동 — 재조준 필요)"); invalid += 1; continue
            try:
                ast.parse(text)
            except SyntaxError as exc:
                print(f"  INVALID  {name}  ← 구문 파괴: {exc.msg}"); invalid += 1; continue
            handle = handles[target]
            handle.write_mutant(text)
            try:
                r = _run(work)
            finally:
                handle.restore()
            if r.returncode == _TEST_FAILED:
                hint = next((l for l in r.stdout.splitlines() if l.startswith("FAILED") or "Error" in l), "")
                print(f"  KILLED   {name}  ← {hint[:74]}"); killed += 1
            elif r.returncode in _INFRA:
                print(f"  INFRA    {name}  ← pytest exit {r.returncode}"); infra += 1
            elif r.returncode == 0:
                print(f"  SURVIVED {name}  ⚠️ 테스트가 못 잡는다"); survived += 1
            else:
                print(f"  INFRA    {name}  ← 예상 밖 exit {r.returncode}"); infra += 1
    finally:
        for handle in handles.values():
            handle.restore()

    print(f"\n  killed={killed} survived={survived} invalid={invalid} infra={infra} / {len(MUTANTS)}")
    return 0 if (survived == 0 and invalid == 0 and infra == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
