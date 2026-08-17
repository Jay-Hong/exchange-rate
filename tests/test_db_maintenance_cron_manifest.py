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
        # ⛔ `flock` 이 없는 플랫폼(macOS)에서 이 계약들이 **영구히 skip** 되면, "Linux CI 가
        #    볼 것" 이라는 말은 기전이 아니다. 실제로 일회성 fake flock 으로 돌렸을 때
        #    `mktemp` 템플릿 결함과 테스트 결함이 나왔다 — fixture 가 직접 설치한다.
        #    ⚠️ 진짜 flock 이 있으면 **그것을 쓴다**(Linux CI 는 실물 경로를 검증한다).
        if not shutil.which("flock"):
            stub = self.tmp / "flock"
            stub.write_text("#!/usr/bin/env bash\nexit 0\n")
            stub.chmod(0o755)
        # ⛔ 시간을 **고정**한다. 실제 `date` 를 쓰면 초 경계를 넘는 순간 충돌 회귀가 있어도
        #    통과한다 — "같은 초에 두 번" 을 시험하려면 같은 초를 강제해야 한다.
        fake_date = self.tmp / "date"
        fake_date.write_text('#!/usr/bin/env bash\necho "20260101T000000"\n')
        fake_date.chmod(0o755)

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
        # ⛔ **출력에서 명령을 추출해 그대로 실행한다.**
        #    1판: `backup.read_text()` 를 상태 파일에 직접 write → 복구 경로 자체를 우회.
        #    2판: `assertIn` 으로 문자열 존재만 확인하고 **명령은 따로 조립** → 출력이
        #         `false # crontab "…"` 로 깨져도 통과한다(실측).
        #    이제 출력된 그 줄을 그대로 실행한다 — 그것이 사람이 복사할 명령이다.
        m = re.search(r"^\s*복원:\s*(.+)$", r.stdout, re.M)
        self.assertIsNotNone(m, f"복원 명령을 출력하지 않았다: {r.stdout}")
        self.state.write_text("WRECKED\n")            # 되돌릴 대상이 실제로 달라야 한다
        rr = subprocess.run(["bash", "-c", m.group(1)], capture_output=True, text=True,
                            env=self._env(), timeout=30, cwd=REPO)
        self.assertEqual(rr.returncode, 0, rr.stdout + rr.stderr)
        self.assertEqual(self.state.read_text(), before, "복원 명령이 원본을 되돌리지 못했다")

    def test_every_write_path_acquires_the_lock(self):
        """⛔ `--restore` 가 락 밖이면 `--install` 과 **같은 사용자 상태를 동시에 덮는다**.

        판정은 "락 획득이 실패하면 그 경로가 **쓰지 않는가**" 다 — 항상 실패하는 fake flock 을
        놓고, 쓰기 모드가 전부 거부되는지 본다. 구조(호출 유무)가 아니라 **행동**으로 본다.
        """
        (self.tmp / "flock").write_text("#!/usr/bin/env bash\nexit 1\n")
        (self.tmp / "flock").chmod(0o755)
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'echo "WROTE" >> "$CRON_WRITES"\n'
            'cat "$1" > "$CRON_STATE"\n')
        self.tmp.joinpath("w.txt").write_text("")
        before = "\n".join([UNRELATED] + legacy_lines()) + "\n"
        self.state.write_text(before)

        # 백업 파일 하나를 만들어 --restore 도 같은 조건에서 시험한다.
        bak = self.tmp / "some.bak"
        bak.write_text(before)
        import hashlib
        sha = hashlib.sha256(before.encode()).hexdigest()

        for mode in (["--install"], ["--restore", str(bak), sha]):
            with self.subTest(mode=mode[0]):
                r = subprocess.run(["bash", str(INSTALLER), *mode], capture_output=True,
                                   text=True, env=self._env(), cwd=REPO, timeout=60)
                self.assertNotEqual(r.returncode, 0, f"락 실패인데 진행했다: {r.stdout}")
                self.assertIn("다른 installer", r.stdout)
        self.assertEqual(self.tmp.joinpath("w.txt").read_text().strip(), "",
                         "락을 못 잡았는데 crontab 을 썼다")

    def test_restore_fails_when_the_write_is_not_reflected(self):
        """⛔ **exit 0 은 설치의 증거가 아니다.**

        쓰기 요청을 무시하고 0 을 반환하는 `crontab` 에서 상태는 그대로인데 "복원 완료" 가
        출력됐다(실측). 사후에 다시 읽어 **바이트가 같을 때만** 성공이다.
        """
        import hashlib

        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'exit 0\n')                      # 쓰기를 무시하고 성공을 반환한다
        self.state.write_text("CURRENT\n")
        bak = self.tmp / "b.bak"; bak.write_text("ORIGINAL\n")
        sha = hashlib.sha256(b"ORIGINAL\n").hexdigest()
        r = subprocess.run(["bash", str(INSTALLER), "--restore", str(bak), sha],
                           capture_output=True, text=True, env=self._env(), cwd=REPO, timeout=60)
        self.assertNotEqual(r.returncode, 0, f"거짓 성공: {r.stdout}")
        self.assertIn("반영되지 않았다", r.stdout)
        self.assertNotIn("복원 완료", r.stdout)

    def test_restore_hint_is_an_absolute_path(self):
        """⛔ 상대 경로면 다른 디렉터리에서 복사해 실행할 때 **exit 127** 이 난다."""
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        self.state.write_text("\n".join([UNRELATED] + legacy_lines()) + "\n")
        r = subprocess.run(["bash", "ops/install-db-maintenance-cron.sh", "--install"],
                           capture_output=True, text=True, env=self._env(), cwd=REPO, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cmd = re.search(r"^\s*복원:\s*(.+)$", r.stdout, re.M).group(1)
        self.assertTrue(cmd.split()[0].startswith("/"),
                        f"안내가 절대 경로가 아니다: {cmd}")
        # 다른 디렉터리에서 실행해도 동작해야 한다 — 그게 절대 경로를 쓰는 이유다.
        rr = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                            env=self._env(), cwd=str(self.tmp), timeout=60)
        self.assertEqual(rr.returncode, 0, rr.stdout + rr.stderr)

    def test_restore_takes_a_pre_restore_backup(self):
        """⛔ cutover 이후 무관 cron 이 바뀌었다면 복원이 그것까지 되돌린다 — 되돌릴 길을 남긴다."""
        import hashlib

        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        current = "CURRENT STATE\n"
        self.state.write_text(current)
        bak = self.tmp / "b.bak"; bak.write_text("ORIGINAL\n")
        r = subprocess.run(["bash", str(INSTALLER), "--restore", str(bak),
                            hashlib.sha256(b"ORIGINAL\n").hexdigest()],
                           capture_output=True, text=True, env=self._env(), cwd=REPO, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pres = sorted(self.backups.glob("crontab.*.pre-restore.*"))
        self.assertEqual(len(pres), 1, f"pre-restore 백업이 없다: {r.stdout}")
        self.assertEqual(pres[0].read_text(), current, "복원 직전 상태를 담지 않았다")
        self.assertIn("되돌리기:", r.stdout, "되돌리기 명령을 안내하지 않았다")

    def test_the_pre_restore_hint_actually_undoes_the_restore(self):
        """⛔ 안내 **문자열의 존재**는 되돌릴 수 있다는 증거가 아니다.

        `test_restore_takes_a_pre_restore_backup` 은 `assertIn("되돌리기:", …)` 로 존재만 본다 —
        그 명령을 `false # …` 로 깨뜨려도 **파일 전체가 초록**이었다(외부 검토, 실측 26 passed).
        복원을 되돌릴 길이 사라졌는데 아무도 모르는 상태다. 출력된 그 줄을 그대로 실행한다.
        """
        import hashlib

        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        current = "CURRENT STATE\n"
        self.state.write_text(current)
        bak = self.tmp / "b.bak"; bak.write_text("ORIGINAL\n")
        r = subprocess.run(["bash", str(INSTALLER), "--restore", str(bak),
                            hashlib.sha256(b"ORIGINAL\n").hexdigest()],
                           capture_output=True, text=True, env=self._env(), cwd=REPO, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.state.read_text(), "ORIGINAL\n", "복원 자체가 안 됐다")
        m = re.search(r"^\s*되돌리기:\s*(.+)$", r.stdout, re.M)
        self.assertIsNotNone(m, f"되돌리기 명령을 출력하지 않았다: {r.stdout}")
        rr = subprocess.run(["bash", "-c", m.group(1)], capture_output=True, text=True,
                            env=self._env(), timeout=30, cwd=REPO)
        self.assertEqual(rr.returncode, 0, "되돌리기 명령이 실패했다:\n" + rr.stdout + rr.stderr)
        self.assertEqual(self.state.read_text(), current,
                         "되돌리기 명령이 복원 직전 상태를 되돌리지 못했다")

    def test_the_pre_restore_hint_is_an_absolute_path(self):
        """⛔ **첫 안내와 같은 계약이 두 번째 안내에도 필요하다.**

        `test_restore_hint_is_an_absolute_path` 는 *설치* 백업의 안내만 덮는다. pre-restore
        안내의 `$SELF` 를 `${BASH_SOURCE[0]}` 로 회귀시켜도 **파일 전체가 초록**이었다
        (외부 검토, 실측 27 passed) — 실행 검증 테스트가 installer 를 절대경로로 부르고
        되돌리기도 `cwd=REPO` 에서 돌려 차이가 드러나지 않았기 때문이다.
        그래서 여기서는 **상대경로로 호출**하고 **다른 cwd 에서** 되돌린다.
        """
        import hashlib

        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        current = "CURRENT STATE\n"
        self.state.write_text(current)
        bak = self.tmp / "b.bak"; bak.write_text("ORIGINAL\n")
        r = subprocess.run(["bash", "ops/install-db-maintenance-cron.sh", "--restore",
                            str(bak), hashlib.sha256(b"ORIGINAL\n").hexdigest()],
                           capture_output=True, text=True, env=self._env(), cwd=REPO, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = re.search(r"^\s*되돌리기:\s*(.+)$", r.stdout, re.M)
        self.assertIsNotNone(m, f"되돌리기 명령을 출력하지 않았다: {r.stdout}")
        cmd = m.group(1)
        self.assertTrue(cmd.split()[0].startswith("/"),
                        f"되돌리기 안내가 절대 경로가 아니다: {cmd}")
        rr = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                            env=self._env(), cwd=str(self.tmp), timeout=60)
        self.assertEqual(rr.returncode, 0,
                         "다른 디렉터리에서 되돌리기가 실패했다:\n" + rr.stdout + rr.stderr)
        self.assertEqual(self.state.read_text(), current, "복원 직전 상태로 되돌아가지 않았다")

    def test_restore_installs_the_verified_snapshot_not_the_original_path(self):
        """⛔ SHA 를 백업 **경로**에서 재고 **같은 경로**를 다시 설치하면 그 사이 파일이 바뀔 수
        있다(TOCTOU) — 검증하지 않은 것을 설치하게 된다.

        경쟁 창을 결정적으로 만든다: 가짜 `shasum` 이 호출된 **직후** 백업 파일을 변조한다.
        고친 코드는 미리 뜬 스냅샷을 설치하므로 검증한 내용이 들어가고, 원본 경로를 설치하는
        회귀는 변조본이 들어간다.
        """
        import hashlib

        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        bak = self.tmp / "b.bak"
        bak.write_text("ORIGINAL\n")
        sha = hashlib.sha256(b"ORIGINAL\n").hexdigest()
        # 진짜 shasum 을 감싸: 정상 결과를 낸 뒤 백업을 변조한다(검증 ↔ 설치 사이의 창).
        real = shutil.which("shasum") or shutil.which("sha256sum")
        self.assertIsNotNone(real, "shasum 없음")
        (self.tmp / "shasum").write_text(
            f'#!/usr/bin/env bash\n"{real}" "$@"\nrc=$?\n'
            f'if [ -w "{bak}" ]; then echo "TAMPERED" > "{bak}"; fi\nexit $rc\n')
        (self.tmp / "shasum").chmod(0o755)
        self.state.write_text("CURRENT\n")
        r = subprocess.run(["bash", str(INSTALLER), "--restore", str(bak), sha],
                           capture_output=True, text=True, env=self._env(), cwd=REPO, timeout=60)
        # ⛔ 초판은 `assertNotIn("TAMPERED", …)` **하나뿐**이었다 — 순수한 **부정** 단언이라
        #    복원이 아무것도 설치하지 않아도(조기 실패·no-op) 만족된다. 상태는 그대로
        #    `CURRENT\n` 이고 거기엔 TAMPERED 가 없기 때문이다(외부 검토, 실측 확인).
        #    캡처한 `r` 조차 쓰이지 않았다. 계약을 **양방향**으로 고정한다.
        self.assertEqual(r.returncode, 0, "복원이 성공하지 않았다:\n" + r.stdout + r.stderr)
        self.assertIn("TAMPERED", bak.read_text(),
                      "경쟁 창이 발화하지 않았다 — 이 시험의 전제가 성립하지 않는다")
        self.assertEqual(self.state.read_text(), "ORIGINAL\n",
                         "검증한 스냅샷이 설치되지 않았다")
        self.assertNotIn("TAMPERED", self.state.read_text(),
                         "검증한 내용이 아니라 그 뒤 바뀐 파일을 설치했다(TOCTOU)")

    def test_restore_refuses_a_tampered_backup(self):
        """⛔ SHA 를 출력만 하고 검증하지 않으면 **잘린 백업도 그대로 설치**된다."""
        self._install_fake(
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "-l" ]; then cat "$CRON_STATE" 2>/dev/null; exit 0; fi\n'
            'cat "$1" > "$CRON_STATE"\n')
        before = "\n".join([UNRELATED] + legacy_lines()) + "\n"
        self.state.write_text(before)
        r = self._run("--install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = sorted(self.backups.glob("crontab.*.bak.*"))[0]
        cmd = re.search(r"^\s*복원:\s*(.+)$", r.stdout, re.M).group(1)
        backup.write_text("TAMPERED\n")               # 백업이 손상됐다
        self.state.write_text("CURRENT\n")
        rr = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                            env=self._env(), timeout=30, cwd=REPO)
        self.assertNotEqual(rr.returncode, 0, "손상된 백업을 그대로 설치했다")
        self.assertIn("SHA 불일치", rr.stdout)
        self.assertEqual(self.state.read_text(), "CURRENT\n", "실패인데 crontab 을 바꿨다")

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
