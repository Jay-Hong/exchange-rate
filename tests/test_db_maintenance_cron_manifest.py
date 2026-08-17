"""M0a — DB maintenance cron **manifest·installer 상태머신**.

## 이 파일이 증명하는 것과 못 하는 것

- 증명한다: manifest 형식, legacy 파생의 유일성, installer 상태머신의 3상태, 부분 상태에서
  `crontab` write 가 **0회**, 무관 줄의 **바이트 보존**.
- ⛔ 증명하지 **못한다**: 운영 호스트의 실제 crontab 이 cutover 됐는지. 그건 observed state 라
  `ops/install-db-maintenance-cron.sh --check` 를 **그 호스트에서** 돌려야 안다.
  `DECISIONS.md` 의 cron 기록도 사료이지 현재 상태가 아니다.

## 왜 선택자만 두는가

manifest 에 숫자 timeout 을 박으면 진실원이 둘이 되고 한쪽만 바뀐 채 조용히 갈린다.
cron 은 **profile 을 고르기만** 하고 값은 `app/` 이 소유한다.
"""
import os
import pathlib
import re
import shutil
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = REPO / "ops" / "cron" / "fxi-db-maintenance.crontab"
INSTALLER = REPO / "ops" / "install-db-maintenance-cron.sh"
TOKEN = " -e DB_WORKLOAD_PROFILE=maintenance"

UNRELATED = "55 20 26 4 * /home/ubuntu/scripts/monday_reopen_exec.sh"


def managed_lines() -> list[str]:
    return [l for l in MANIFEST.read_text().splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def legacy_lines() -> list[str]:
    return [l.replace(TOKEN, "", 1) for l in managed_lines()]


class TestManifestShape(unittest.TestCase):
    def test_exactly_five_managed_jobs(self):
        self.assertEqual(len(managed_lines()), 5)

    def test_selector_appears_exactly_once_per_line(self):
        """⛔ 두 번 들어가면 legacy 파생(토큰 하나 제거)이 **여전히 토큰을 남긴다** — 그러면
        상태 판정이 managed 도 legacy 도 아닌 값을 보고 조용히 거부하거나 오분류한다."""
        for line in managed_lines():
            with self.subTest(line=line[:48]):
                self.assertEqual(line.count(TOKEN), 1)

    def test_no_numeric_timeout_is_duplicated_into_cron(self):
        """⛔ 숫자가 cron 에 있으면 진실원이 둘이다 — 값은 `app/` 한 곳이 소유한다."""
        blob = MANIFEST.read_text()
        for bad in ("statement_timeout", "TIMEOUT_MS", "pool_timeout", "connect_timeout"):
            self.assertNotIn(bad, blob, f"manifest 에 숫자/설정 이름이 새어 들어왔다: {bad}")

    def test_env_flag_precedes_the_service_name(self):
        """⛔ `-e` 가 service 이름 뒤면 컨테이너 **인자**로 넘어가 환경이 설정되지 않는다."""
        for line in managed_lines():
            with self.subTest(line=line[:48]):
                self.assertLess(line.index(TOKEN.strip()), line.index(" fastapi "),
                                "-e 가 service 이름보다 뒤에 있다")

    def test_every_job_enters_the_database(self):
        for line in managed_lines():
            with self.subTest(line=line[:48]):
                self.assertIn("docker compose run", line)
                self.assertIn("--allow-production-write", line)


class TestInstallerStateMachine(unittest.TestCase):
    """⛔ 실제 `crontab` 을 건드리지 않는다 — 가짜 `crontab` 을 PATH 앞에 놓아 격리한다."""

    def setUp(self):
        if not shutil.which("bash"):
            self.skipTest("bash 없음")
        import tempfile

        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "crontab.txt"
        self.writes = self.tmp / "writes.txt"
        fake = self.tmp / "crontab"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null || exit 1; exit 0; fi\n'
            'echo "write" >> "$CRON_WRITES"\n'
            'cat "$1" > "$CRON_STATE"\n'
        )
        fake.chmod(0o755)
        self.writes.write_text("")

    def _run(self, mode: str, content: str) -> subprocess.CompletedProcess:
        self.state.write_text(content)
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}",
                   CRON_STATE=str(self.state), CRON_WRITES=str(self.writes))
        return subprocess.run(["bash", str(INSTALLER), mode],
                              capture_output=True, text=True, env=env, cwd=REPO, timeout=60)

    def _writes(self) -> int:
        return len([l for l in self.writes.read_text().splitlines() if l])

    def test_check_succeeds_only_on_managed_state(self):
        """⛔ `--check` 가 LEGACY 를 통과시키면 **아직 cutover 안 된 호스트가 green** 이 된다."""
        managed = "\n".join([UNRELATED] + managed_lines()) + "\n"
        legacy = "\n".join([UNRELATED] + legacy_lines()) + "\n"
        self.assertEqual(self._run("--check", managed).returncode, 0)
        self.assertNotEqual(self._run("--check", legacy).returncode, 0,
                            "cutover 전인데 --check 가 통과했다")
        self.assertEqual(self._writes(), 0, "--check 가 crontab 에 write 했다")

    def test_partial_state_is_rejected_without_any_write(self):
        """⛔ 부분 전환은 **거부**다. 그리고 거부할 때 write 가 0회여야 한다."""
        lines = [UNRELATED] + managed_lines()[:2] + legacy_lines()[2:]
        r = self._run("--install", "\n".join(lines) + "\n")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("부분 상태", r.stdout)
        self.assertEqual(self._writes(), 0, "부분 상태인데 crontab 을 건드렸다")

    def test_duplicate_line_is_rejected_without_any_write(self):
        lines = [UNRELATED] + legacy_lines() + [legacy_lines()[0]]
        r = self._run("--install", "\n".join(lines) + "\n")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("중복", r.stdout)
        self.assertEqual(self._writes(), 0)

    def test_missing_job_is_rejected_without_any_write(self):
        lines = [UNRELATED] + legacy_lines()[:-1]
        r = self._run("--install", "\n".join(lines) + "\n")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self._writes(), 0)

    @unittest.skipUnless(shutil.which("flock"), "flock 없는 플랫폼(macOS) — --install 은 거부된다")
    def test_legacy_cutover_preserves_unrelated_bytes(self):
        """⛔ **무관 줄은 바이트 그대로** 남아야 한다 — 전체 교체가 아니라 줄 단위 치환이다."""
        before = "\n".join([UNRELATED, "", "# 사람이 남긴 주석"] + legacy_lines()) + "\n"
        r = self._run("--install", before)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = self.state.read_text()
        self.assertIn(UNRELATED, after, "무관 job 이 사라졌다")
        self.assertIn("# 사람이 남긴 주석", after, "주석이 사라졌다")
        for line in managed_lines():
            self.assertIn(line, after)
        for line in legacy_lines():
            self.assertNotIn(line + "\n", after, "legacy 줄이 남았다")

    @unittest.skipUnless(shutil.which("flock"), "flock 없는 플랫폼")
    def test_install_is_idempotent_on_managed_state(self):
        managed = "\n".join([UNRELATED] + managed_lines()) + "\n"
        r = self._run("--install", managed)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self._writes(), 0, "이미 설치된 상태인데 다시 write 했다")

    def test_install_refuses_when_flock_is_unavailable(self):
        """⛔ 직렬화를 보장 못 하면 **정확한 이유로 거부**한다.

        ⚠️ 초판은 `flock` 부재를 "다른 installer 가 실행 중" 이라는 **거짓 사유**로 보고했다
           (macOS 로컬 스모크에서 실측). 거짓 진단은 없는 것보다 나쁘다.
        """
        stub = self.tmp / "flock"
        env_path = f"{self.tmp}:{os.environ['PATH']}"
        if shutil.which("flock"):
            self.skipTest("flock 이 있는 플랫폼 — 부재 경로는 macOS 에서 검증된다")
        managed = "\n".join([UNRELATED] + legacy_lines()) + "\n"
        r = self._run("--install", managed)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("flock", r.stdout)
        self.assertNotIn("다른 installer 가 이 호스트에서 실행 중", r.stdout,
                         "부재를 경쟁으로 둘러댔다")
        self.assertEqual(self._writes(), 0)
        del stub, env_path


class TestNoArgIsInert(unittest.TestCase):
    def test_no_mode_does_nothing(self):
        """⛔ 무인자 실행이 뭔가를 하면, 익숙해진 손이 사고를 낸다(기존 installer 의 교훈)."""
        r = subprocess.run(["bash", str(INSTALLER)], capture_output=True, text=True,
                           cwd=REPO, timeout=60)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
