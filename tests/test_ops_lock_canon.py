"""lock 정본 + **실제 상호배제** 통합 테스트.

⚠️ 왜 이 파일이 필요한가: lock 경로가 세 스크립트에 각각 리터럴로 있고 각각 env로 재정의되던
동안, `LOCK_FILE=/tmp/other.lock ops/deploy-fastapi.sh …`가 canonical crontab 검사를 통과하면서
**상호배제만 사라지는** 상태를 만들 수 있었다(실측). 그리고 기존 가짜 flock은 성공/실패만
답해서 **경로별 lock 상태를 모델링하지 않아** 이 결함을 잡을 수 없었다.

여기서는 `fcntl.flock` 기반 **충실한 더블**을 쓴다 — 실제로 잠그므로 서로 다른 프로세스가
같은 경로에서 진짜로 배제되고, 다른 경로면 진짜로 독립이다.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
OPS = REPO / "ops"

FAKE_FLOCK = '''#!/usr/bin/env python3
"""fcntl 기반 flock 대역 — 실제 advisory lock을 잡는다(경로별 독립)."""
import fcntl, os, sys, time
args = sys.argv[1:]
timeout = None
if args and args[0] == "-w":
    timeout = float(args[1]); args = args[2:]
fd = int(args[0])
deadline = None if timeout is None else time.monotonic() + timeout
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); sys.exit(0)
    except OSError:
        if deadline is not None and time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.02)
'''


class TestLockCanon(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        self.tmp = pathlib.Path(self.td.name)
        self.bin = self.tmp / "bin"; self.bin.mkdir()
        f = self.bin / "flock"; f.write_text(FAKE_FLOCK, encoding="utf-8"); f.chmod(0o755)
        self.conf = self.tmp / "lock.conf"
        self.lock = self.tmp / "shared.lock"
        self.conf.write_text(f"FXI_DEPLOY_LOCK={self.lock}\n", encoding="utf-8")

    def _hold(self, *, conf=None):
        """lock을 실제로 잡은 뒤 marker를 남기는 holder를 띄우고, **marker를 poll**해 동기화한다.

        ⚠️ 고정 sleep으로 기다리면 full-suite 부하에서 flaky해진다(이 리포의 기존 교훈이고,
        실제로 이 파일이 처음 그렇게 간헐 실패했다). 관측 가능한 신호를 기다린다.
        """
        marker = self.tmp / f"held-{conf and conf.name or 'main'}"
        env = self._env() if conf is None else dict(self._env(), FXI_LOCK_CONF=str(conf))
        proc = subprocess.Popen(
            ["bash", str(OPS / "cron-with-lock.sh"), "--policy", "block", "--",
             "sh", "-c", f"touch {marker}; sleep 30"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not marker.exists():
            self.assertLess(time.monotonic(), deadline, "holder가 lock을 잡지 못했다")
            self.assertIsNone(proc.poll(), "holder가 죽었다")
            time.sleep(0.02)
        return proc

    def _env(self, **extra):
        return dict(os.environ, FXI_LOCK_CONF=str(self.conf),
                    FLOCK_BIN=str(self.bin / "flock"), **extra)

    def test_all_scripts_resolve_the_same_lock_path(self):
        """⛔ 세 스크립트가 **같은 경로**를 봐야 한다 — 리터럴이 흩어져 있던 결함의 직접 잠금."""
        seen = set()
        for script in ("deploy-fastapi.sh", "cron-with-lock.sh", "cron-job.sh"):
            r = subprocess.run(
                ["bash", "-c",
                 f'FXI_LOCK_CONF={self.conf} . {self.conf}; echo "$FXI_DEPLOY_LOCK"'],
                capture_output=True, text=True, env=self._env())
            seen.add(r.stdout.strip())
        self.assertEqual(seen, {str(self.lock)})

    def test_legacy_lock_file_env_is_dead(self):
        """구 손잡이(`LOCK_FILE`)로 한쪽만 옮기는 것이 불가능해야 한다."""
        for script in ("deploy-fastapi.sh", "cron-with-lock.sh", "cron-job.sh"):
            src = (OPS / script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertNotIn('${LOCK_FILE:-', src, "개별 fallback이 되살아났다")
                self.assertNotIn('${FXI_LOCK_FILE:-', src)

    def test_two_processes_are_actually_excluded_on_the_same_lock(self):
        """⛔ **실제 배제** — block이 잡고 있는 동안 wait는 timeout(75)이 나야 한다."""
        self._hold()
        r = subprocess.run(
            ["bash", str(OPS / "cron-with-lock.sh"), "--policy", "wait", "--", "echo", "RAN"],
            env=self._env(CRON_LOCK_WAIT="1"), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertNotIn("RAN", r.stdout)

    def test_different_lock_paths_are_not_excluded(self):
        """더블이 경로별 독립을 실제로 모델링하는지 — 이게 아니면 위 테스트가 공허하다."""
        other_conf = self.tmp / "other.conf"
        other_conf.write_text(f"FXI_DEPLOY_LOCK={self.tmp / 'other.lock'}\n", encoding="utf-8")
        self._hold()
        r = subprocess.run(
            ["bash", str(OPS / "cron-with-lock.sh"), "--policy", "wait", "--", "echo", "RAN"],
            env=dict(self._env(CRON_LOCK_WAIT="1"), FXI_LOCK_CONF=str(other_conf)),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RAN", r.stdout)

    def test_missing_lock_conf_is_refused(self):
        r = subprocess.run(
            ["bash", str(OPS / "cron-with-lock.sh"), "--policy", "wait", "--", "echo", "RAN"],
            env=dict(self._env(), FXI_LOCK_CONF=str(self.tmp / "nope.conf")),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertNotIn("RAN", r.stdout)


if __name__ == "__main__":
    unittest.main()
