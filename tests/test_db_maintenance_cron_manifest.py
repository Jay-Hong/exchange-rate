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


class TestInstallerDurability(unittest.TestCase):
    """⛔ 백업·오류 보존·바이트 보존 — 셋 다 **land 후 codex 가 잡은 실제 결함**이다."""

    def setUp(self):
        import tempfile

        if not shutil.which("bash"):
            self.skipTest("bash 없음")
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "crontab.txt"
        self.backups = self.tmp / "state" / "fxi-cron-backups"
        self.fake = self.tmp / "crontab"

    def _install_fake(self, body: str):
        self.fake.write_text(body)
        self.fake.chmod(0o755)

    def _env(self):
        return dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}",
                    CRON_STATE=str(self.state), CRON_WRITES=str(self.tmp / "w.txt"),
                    XDG_STATE_HOME=str(self.tmp / "state"),
                    XDG_RUNTIME_DIR=str(self.tmp))

    def _run(self, mode: str):
        return subprocess.run(["bash", str(INSTALLER), mode], capture_output=True,
                              text=True, env=self._env(), cwd=REPO, timeout=60)

    def test_crontab_read_failure_is_not_treated_as_empty(self):
        """⛔ 실측: `exit 42` 인 crontab 이 **len=0** 으로 둔갑해 managed=0 legacy=0 오진이 됐다.

        대상 호스트는 기존 5줄이 **필수**라, 조회 실패를 빈 것으로 접으면 "부분 상태" 라는
        엉뚱한 사유로 거부하고 진짜 원인(권한·환경)이 사라진다.
        """
        self._install_fake('#!/usr/bin/env bash\necho "boom" >&2\nexit 42\n')
        r = self._run("--check")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("crontab -l 실패", r.stderr + r.stdout)
        self.assertNotIn("부분 상태", r.stdout, "조회 실패를 상태 판정으로 둔갑시켰다")

    def test_every_nonzero_exit_is_fail_closed(self):
        """⛔ 한때 `exit 1` + `no crontab` **부분문자열**이면 빈 것으로 봤다.

        `backend unavailable: no crontab service` + exit 1 도 통과했다(실측). 대상 호스트는
        기존 5줄이 **필수**라 "빈 crontab" 은 어차피 정상 상태가 아니다 — 구별하려 애쓰기보다
        전부 막고 사람이 보게 하는 쪽이 정확하다.
        """
        for stderr, code in (("no crontab for tester", 1),
                             ("backend unavailable: no crontab service", 1),
                             ("permission denied", 13)):
            with self.subTest(stderr=stderr):
                self._install_fake(f'#!/usr/bin/env bash\necho "{stderr}" >&2\nexit {code}\n')
                r = self._run("--check")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("crontab -l 실패", r.stderr + r.stdout,
                              "조회 실패를 빈 crontab 으로 접었다")

    @unittest.skipUnless(shutil.which("flock"), "flock 없는 플랫폼")
    def test_install_writes_a_persistent_backup_before_touching_crontab(self):
        """⛔ 메모리의 값과 임시 파일은 **복구본이 아니다** — 프로세스와 함께 사라진다."""
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        before = "\n".join([UNRELATED] + legacy_lines()) + "\n"
        self.state.write_text(before)
        r = self._run("--install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backups = sorted(self.backups.glob("crontab.*.bak.*"))
        self.assertEqual(len(backups), 1, f"영속 백업이 없다: {r.stdout}")
        self.assertEqual(backups[0].read_text(), before, "백업이 원본과 다르다")
        self.assertEqual(oct(backups[0].stat().st_mode)[-3:], "600")
        self.assertIn(str(backups[0]), r.stdout, "복구 경로를 알려주지 않았다")

    @unittest.skipUnless(shutil.which("flock"), "flock 없는 플랫폼")
    def test_unrelated_bytes_are_preserved_exactly(self):
        """⛔ `assertIn` 은 **바이트 보존을 증명하지 못한다**.

        실측: command substitution 이 trailing LF 3개를 1개로 줄였다. 기대 바이트 전체와
        비교해야 그 손실이 드러난다 — 그래서 스냅샷 파일 자체를 변환하도록 고쳤다.
        """
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        before = (UNRELATED + "\n\n# 사람이 남긴 주석\n"
                  + "\n".join(legacy_lines()) + "\n\n\n")   # trailing LF 3개
        self.state.write_text(before)
        r = self._run("--install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        expected = before
        for old, new in zip(legacy_lines(), managed_lines()):
            expected = expected.replace(old + "\n", new + "\n", 1)
        self.assertEqual(self.state.read_text(), expected,
                         "무관 바이트가 바뀌었다(trailing LF·주석·빈 줄 포함)")

    @unittest.skipUnless(shutil.which("flock"), "flock 없는 플랫폼")
    def test_backup_restores_the_original_exactly(self):
        """백업이 **실제로 되돌리는지** — 파일이 있다는 것만으로는 복구를 증명하지 못한다."""
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        before = "\n".join([UNRELATED] + legacy_lines()) + "\n\n"
        self.state.write_text(before)
        r = self._run("--install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = sorted(self.backups.glob("crontab.*.bak.*"))[0]
        # ⛔ **출력된 복원 명령을 실제로 실행한다.** 초판은 `backup.read_text()` 를 상태 파일에
        #    직접 써서 복구 경로를 **우회**했다 — 그러면 `crontab "$backup"` 이 깨져도 통과한다.
        self.assertIn(f'crontab "{backup}"', r.stdout, "복원 명령을 출력하지 않았다")
        subprocess.run(["bash", "-c", f'crontab "{backup}"'], check=True,
                       env=self._env(), timeout=30)
        self.assertEqual(self.state.read_text(), before, "복원 명령이 원본을 되돌리지 못했다")

    @unittest.skipUnless(shutil.which("flock"), "flock 없는 플랫폼")
    def test_two_runs_in_the_same_second_do_not_overwrite_a_backup(self):
        """⛔ 초 단위 이름은 같은 초에 두 번 돌면 **앞 백업을 덮는다**(실측: 1개만 남았다).

        복구본이 덮이면 백업이 있다는 사실 자체가 거짓 안심이 된다.
        """
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        first = "\n".join([UNRELATED] + legacy_lines()) + "\n"
        self.state.write_text(first)
        self.assertEqual(self._run("--install").returncode, 0)
        second = "\n".join([UNRELATED, "# 두 번째"] + legacy_lines()) + "\n"
        self.state.write_text(second)
        self.assertEqual(self._run("--install").returncode, 0)
        backups = sorted(self.backups.glob("crontab.*.bak.*"))
        self.assertEqual(len(backups), 2, "백업이 덮였다")
        self.assertEqual({b.read_text() for b in backups}, {first, second})

    @unittest.skipUnless(shutil.which("flock"), "flock 없는 플랫폼")
    def test_aborted_install_leaves_no_stale_backup(self):
        """⛔ 백업이 최종 CAS **앞**이면, 중단된 실행이 stale 백업과 그 복원 명령을 남긴다 —
        현재 상태와 다른 것을 되돌리라고 안내하는 셈이다."""
        # 두 snapshot 사이에 crontab 을 바꾸는 fake — 두 번째 -l 호출에서 내용이 달라진다.
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then\n'
            '  cat "$CRON_STATE" 2>/dev/null\n'
            '  echo "# 남이 끼어들었다" >> "$CRON_STATE"\n'
            '  exit 0\n'
            'fi\n'
            'echo "WROTE" >> "$CRON_WRITES"\n'
            'cat "$1" > "$CRON_STATE"\n')
        self.tmp.joinpath("w.txt").write_text("")
        self.state.write_text("\n".join([UNRELATED] + legacy_lines()) + "\n")
        r = self._run("--install")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("바뀌었다", r.stdout)
        self.assertEqual(self.tmp.joinpath("w.txt").read_text().strip(), "", "중단인데 write 했다")
        self.assertEqual(list(self.backups.glob("crontab.*.bak.*")), [],
                         "중단된 실행이 stale 백업을 남겼다")


class TestLockPathIsPerUser(unittest.TestCase):
    def test_lock_path_includes_the_uid(self):
        """⛔ 공용 `/tmp` 고정 경로는 **다른 사용자와 충돌**한다."""
        body = INSTALLER.read_text()
        self.assertRegex(body, r'LOCK=.*\$\(id -u\)', "락 경로에 UID 가 없다")


if __name__ == "__main__":
    unittest.main()
