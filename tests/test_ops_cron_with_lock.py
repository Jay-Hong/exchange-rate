"""ops/cron-with-lock.sh 정책 테스트 — 정책 의미는 wrapper가 지고 여기서 잠근다."""
import os, pathlib, subprocess, sys, tempfile, unittest
W = pathlib.Path(__file__).resolve().parent.parent / "ops" / "cron-with-lock.sh"

class TestCronWithLock(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        self.tmp = pathlib.Path(self.td.name); self.bin = self.tmp / "bin"; self.bin.mkdir()

    def _conf(self):
        c = self.tmp / "lock.conf"
        c.write_text(f"FXI_DEPLOY_LOCK={self.tmp}/l.lock\n", encoding="utf-8")
        return c

    def _flock(self, *, acquires=True):
        """⚠️ 인자를 **기록**한다 — 정책의 계약은 "flock을 어떻게 부르는가"이므로 성공/실패만
        보는 가짜로는 `-w 0`과 무한 대기를 구별할 수 없다(실측: 그 변이가 생존했다)."""
        f = self.bin / "flock"
        f.write_text(f'#!/usr/bin/env bash\necho "$*" >> {self.tmp}/flock_args\n'
                     + ("exit 0\n" if acquires else "exit 1\n"), encoding="utf-8")
        f.chmod(0o755); return f

    @property
    def flock_args(self):
        f = self.tmp / "flock_args"
        return f.read_text(encoding="utf-8").strip() if f.exists() else ""

    def _run(self, *args, acquires=True):
        env = dict(os.environ, FLOCK_BIN=str(self._flock(acquires=acquires)),
                   FXI_LOCK_CONF=str(self._conf()), CRON_LOCK_WAIT="7")
        r = subprocess.run(["bash", str(W), *args], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_wait_policy_runs_the_command(self):
        rc, out = self._run("--policy", "wait", "--", "echo", "RAN")
        self.assertEqual(rc, 0, out); self.assertIn("RAN", out)

    def test_wait_policy_skips_with_tempfail_on_timeout(self):
        """⛔ 조용한 skip 금지 — timeout은 EX_TEMPFAIL(75)로 cron 로그에 실패로 남는다."""
        rc, out = self._run("--policy", "wait", "--", "echo", "RAN", acquires=False)
        self.assertEqual(rc, 75, out); self.assertNotIn("RAN", out); self.assertIn("건너뛴다", out)

    def test_block_policy_uses_a_blocking_flock(self):
        """⛔ block의 계약은 "**건너뛰지 않는다**" — 즉 flock에 timeout을 주지 않는다.

        성공/실패만 보는 가짜로는 `-w 0`으로 바꾸는 변이를 못 잡는다(실측 생존).
        그래서 flock **인자**를 관측한다.
        """
        rc, out = self._run("--policy", "block", "--", "echo", "RAN")
        self.assertEqual(rc, 0, out); self.assertIn("RAN", out)
        self.assertNotIn("-w", self.flock_args,
                         f"block인데 timeout을 줬다: {self.flock_args!r}")

    def test_wait_policy_passes_the_timeout(self):
        rc, out = self._run("--policy", "wait", "--", "echo", "RAN")
        self.assertEqual(rc, 0, out)
        self.assertIn("-w 7", self.flock_args, f"wait인데 timeout이 없다: {self.flock_args!r}")

    def test_missing_policy_is_refused(self):
        rc, out = self._run("--", "echo", "RAN")
        self.assertEqual(rc, 2, out); self.assertNotIn("RAN", out)

    def test_unknown_policy_is_refused(self):
        rc, out = self._run("--policy", "nonblock", "--", "echo", "RAN")
        self.assertEqual(rc, 2, out); self.assertNotIn("RAN", out)

    def test_missing_command_is_refused(self):
        rc, out = self._run("--policy", "wait")
        self.assertEqual(rc, 2, out)

class TestCronJobTable(unittest.TestCase):
    """job 표의 **정책 배정**을 잠근다 — 표만 있고 테스트가 없으면 조용히 뒤집힌다.

    ⚠️ 실측: `daily-all`을 `block` → `wait`로 바꾸는 변이가 **생존했다**. 그건 daily append가
    timeout 시 건너뛰어 **그 날 row가 비는 것**을 허용하는 회귀다(hourly는 다음 회차가 회복하지만
    daily는 회복하지 않는다).
    """

    JOB = pathlib.Path(__file__).resolve().parent.parent / "ops" / "cron-job.sh"

    def _spec(self, job):
        r = subprocess.run(["bash", "-c",
                            f'source /dev/stdin <<<"$(sed -n \'/^job_spec()/,/^}}/p\' {self.JOB})"; job_spec {job}'],
                           capture_output=True, text=True)
        return r.stdout.strip()

    def test_daily_never_skips(self):
        self.assertTrue(self._spec("daily-all").startswith("block|"),
                        f"daily가 skip 가능 정책이다: {self._spec('daily-all')!r}")

    def test_hourly_may_skip_because_it_self_heals(self):
        for job in ("hourly-bithumb", "hourly-investing", "hourly-hana", "hourly-krx"):
            with self.subTest(job=job):
                self.assertTrue(self._spec(job).startswith("wait|"), self._spec(job))

    def test_unknown_job_is_refused(self):
        r = subprocess.run(["bash", str(self.JOB), "no-such-job"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_print_crontab_has_no_policy_or_docker_or_env(self):
        """⛔ 정본 줄에 정책·docker 명령·env 대입이 있으면 crontab이 다시 임의 shell이 된다."""
        out = subprocess.run(["bash", str(self.JOB), "--print-crontab"],
                             capture_output=True, text=True, check=True).stdout
        self.assertTrue(out.strip())
        for bad in ("--policy", "docker", "flock", "LOCK_FILE="):
            self.assertNotIn(bad, out, f"정본 줄에 {bad} 가 있다")


if __name__ == "__main__":
    unittest.main()
