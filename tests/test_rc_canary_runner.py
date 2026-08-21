"""host-side runner 의 계약 잠금 — provenance · 출력 선생성 · 최소권한 env · outer timeout.

가짜 `docker`/`timeout` 을 PATH 에 앞세워 실제 실행 없이 argv·env 파일 내용을 기록한다.
git 가드는 임시 저장소 + bare origin 으로 실측한다(설치기 테스트와 같은 패턴).
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNNER = REPO / "ops" / "run_rc_canary.sh"
FAKE_KEY = "sk_fake_canary_key_for_tests_only"


class RunnerHarness(unittest.TestCase):
    def _fixture(self, *, rc_key: bool = True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = pathlib.Path(temporary.name)
        root = base / "repo"
        remote = base / "origin.git"
        bin_dir = base / "bin"
        log = base / "calls.log"
        state = base / "container.state"
        out_dir = base / "out"
        for d in (root / "scripts", root / "ops", bin_dir):
            d.mkdir(parents=True)
        shutil.copy2(REPO / "scripts/rc_canary.py", root / "scripts/rc_canary.py")
        shutil.copy2(RUNNER, root / "ops/run_rc_canary.sh")
        env_lines = ["ADMIN_PASSWORD=not-forwarded\n", "TELEGRAM_BOT_TOKEN=not-forwarded\n"]
        if rc_key:
            env_lines.append(f"REVENUECAT_API_KEY={FAKE_KEY}\n")
        (root / ".env").write_text("".join(env_lines))

        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
             "commit", "-qm", "fixture"], cwd=root, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=root, check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD:master"],
                       cwd=root, check=True)

        image = "sha256:" + "a" * 64
        # 가짜 docker — production inspect, one-shot lifecycle, argv/env-file을 기록한다.
        (bin_dir / "docker").write_text(
            "#!/bin/sh\n"
            f'LOG="{log}"\n'
            f'STATE="{state}"\n'
            'echo "docker $*" >> "$LOG"\n'
            'if [ "$1" = "inspect" ]; then\n'
            f'  if [ "$2" = "exchange-rate-app" ]; then echo {image}; exit 0; fi\n'
            '  [ -f "$STATE" ] && exit 0 || exit 1\n'
            'fi\n'
            'if [ "$1" = "kill" ]; then unlink "$STATE" 2>/dev/null || true; exit 0; fi\n'
            'if [ "$1" = "container" ] && [ "$2" = "rm" ]; then\n'
            '  unlink "$STATE" 2>/dev/null || true; exit 0\n'
            'fi\n'
            'prev=""\n'
            'for arg in "$@"; do\n'
            '  if [ "$prev" = "--env-file" ]; then\n'
            '    echo "ENVFILE_BEGIN" >> "$LOG"; cat "$arg" >> "$LOG"; echo "ENVFILE_END" >> "$LOG"\n'
            '  fi\n'
            '  prev="$arg"\n'
            'done\n'
            'if [ "$1" = "run" ]; then\n'
            '  echo running > "$STATE"\n'
            '  if [ "${FAKE_DOCKER_LEAVE:-0}" != "1" ]; then unlink "$STATE"; fi\n'
            'fi\n'
            'exit 0\n'
        )
        (bin_dir / "timeout").write_text(
            "#!/bin/sh\n"
            f'echo "timeout $*" >> "{log}"\n'
            'if [ -n "${FAKE_TIMEOUT_READY:-}" ]; then\n'
            '  : > "$FAKE_TIMEOUT_READY"\n'
            '  while [ ! -e "${FAKE_TIMEOUT_RELEASE:-}" ]; do sleep 0.02; done\n'
            'fi\n'
            'while [ $# -gt 0 ]; do\n'
            '  case "$1" in --kill-after=*|[0-9]*) shift ;; *) break ;; esac\n'
            'done\n'
            'if [ -n "${FAKE_TIMEOUT_SLEEP:-}" ]; then sleep "$FAKE_TIMEOUT_SLEEP"; fi\n'
            'if [ "${FAKE_TIMEOUT_FAIL:-0}" = "1" ]; then\n'
            '  FAKE_DOCKER_LEAVE=1 "$@"; exit 124\n'
            'fi\n'
            'exec "$@"\n'
        )
        (bin_dir / "flock").write_text(
            "#!/usr/bin/env python3\n"
            "import fcntl, sys\n"
            "fd = int(sys.argv[-1])\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n"
            "    raise SystemExit(1)\n"
        )
        for f in (bin_dir / "docker", bin_dir / "timeout", bin_dir / "flock"):
            f.chmod(0o755)
        return root, bin_dir, log, out_dir

    def _env(self, root, bin_dir, out_dir, **extra):
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["REPO_ROOT"] = str(root)
        env["OUTPUT_DIR"] = str(out_dir)
        env.update(extra)
        return env

    def _uid_file(self, root, value="canary-x", *, mode=0o600):
        path = root.parent / f"uid-{len(list(root.parent.glob('uid-*')))}.txt"
        path.write_text(value)
        path.chmod(mode)
        return path

    def _uid_args(self, root, value="canary-x", expected="denied_premium_required"):
        return (
            "--uid-file",
            str(self._uid_file(root, value)),
            "--expected-label",
            expected,
        )

    def _run(self, root, bin_dir, out_dir, *args, extra_env=None):
        env = self._env(root, bin_dir, out_dir, **(extra_env or {}))
        return subprocess.run(
            ["bash", str(root / "ops/run_rc_canary.sh"), *args],
            capture_output=True, text=True, env=env,
        )


class TestProvenanceGuards(RunnerHarness):
    def test_unstaged_drift_refuses_before_docker(self):
        root, bin_dir, log, out = self._fixture()
        with (root / "scripts/rc_canary.py").open("a") as stream:
            stream.write("\n# drift\n")
        result = self._run(root, bin_dir, out, *self._uid_args(root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unstaged drift", result.stderr)
        self.assertFalse(log.exists(), "docker 가 호출됐다 — 가드가 뒤에 있다")

    def test_runner_itself_must_match_head(self):
        root, bin_dir, log, out = self._fixture()
        with (root / "ops/run_rc_canary.sh").open("a") as stream:
            stream.write("\n# runner drift\n")
        result = self._run(root, bin_dir, out, *self._uid_args(root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run_rc_canary.sh", result.stderr)
        self.assertIn("unstaged drift", result.stderr)
        self.assertFalse(log.exists())

    def test_clean_but_unpushed_commit_refuses(self):
        root, bin_dir, log, out = self._fixture()
        with (root / "scripts/rc_canary.py").open("a") as stream:
            stream.write("\n# local only\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
             "commit", "-qm", "local"], cwd=root, check=True)
        result = self._run(root, bin_dir, out, *self._uid_args(root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("upstream ref와 다르다", result.stderr)
        self.assertFalse(log.exists())

    def test_missing_rc_key_refuses_before_docker(self):
        root, bin_dir, log, out = self._fixture(rc_key=False)
        result = self._run(root, bin_dir, out, *self._uid_args(root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REVENUECAT_API_KEY", result.stderr)
        self.assertFalse(log.exists())

    def test_duplicate_rc_key_definition_refuses_before_docker(self):
        root, bin_dir, log, out = self._fixture()
        with (root / ".env").open("a") as stream:
            stream.write("REVENUECAT_API_KEY=\n")
        result = self._run(root, bin_dir, out, *self._uid_args(root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("한 줄로만", result.stderr)
        self.assertFalse(log.exists())

    def test_uid_is_mandatory(self):
        root, bin_dir, log, out = self._fixture()
        result = self._run(root, bin_dir, out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--uid-file", result.stderr)
        self.assertFalse(log.exists())

    def test_expected_label_is_mandatory_and_allowlisted(self):
        root, bin_dir, log, out = self._fixture()
        uid_file = self._uid_file(root)
        missing = self._run(root, bin_dir, out, "--uid-file", str(uid_file))
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--expected-label", missing.stderr)
        invalid = self._run(
            root,
            bin_dir,
            out,
            "--uid-file",
            str(uid_file),
            "--expected-label",
            "accept_anything",
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("denied_premium_required", invalid.stderr)
        self.assertFalse(log.exists())

    def test_raw_uid_argument_is_rejected_without_echoing_the_value(self):
        root, bin_dir, log, out = self._fixture()
        result = self._run(root, bin_dir, out, "--uid", "raw-sensitive-uid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("허용하지 않는 인자", result.stderr)
        self.assertNotIn("raw-sensitive-uid", result.stdout + result.stderr)
        self.assertFalse(log.exists())

    def test_unknown_argument_never_echoes_its_value(self):
        """⛔ `--uid=<UID>` 형태는 argparse allowlist 밖이라 거부되지만, 구 메시지가 토큰
        전체를 되울려 UID 가 stderr 에 남았다(실측)."""
        root, bin_dir, log, out = self._fixture()
        result = self._run(root, bin_dir, out, "--uid=synthetic-secret-value")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("synthetic-secret-value", result.stdout + result.stderr)
        self.assertIn("허용하지 않는 인자", result.stderr)
        self.assertFalse(log.exists())

    def test_uid_file_fifo_is_rejected_without_hanging(self):
        """⛔ O_RDONLY 만으로 열면 writer 없는 FIFO 에서 **무기한 block** 하고 S_ISREG 검사에
        도달하지 못한다(실측). O_NONBLOCK 이 그 경로를 닫는다."""
        root, bin_dir, log, out = self._fixture()
        fifo = root.parent / "uid-fifo"
        os.mkfifo(fifo, 0o600)
        result = subprocess.run(
            ["bash", str(root / "ops/run_rc_canary.sh"),
             "--uid-file", str(fifo), "--expected-label", "denied_premium_required"],
            env=self._env(root, bin_dir, out), capture_output=True, text=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)
        self.assertFalse(log.exists())

    def test_uid_file_accepts_multibyte_uid_within_the_character_contract(self):
        """⛔ 계약은 **문자** 수다. 바이트 상한으로 bounded read 하면 다중바이트 UID 가
        절단돼 UnicodeDecodeError 로 오거부된다(실측: 44자·132바이트가 거부됐다)."""
        root, bin_dir, log, out = self._fixture()
        uid = "가" * 128  # 128자 = 384바이트
        result = self._run(
            root, bin_dir, out,
            "--uid-file", str(self._uid_file(root, uid)),
            "--expected-label", "denied_premium_required",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = log.read_text().split("ENVFILE_BEGIN\n")[1].split("ENVFILE_END")[0]
        self.assertIn(f"CANARY_UID={uid}", body)

    def test_uid_file_rejects_one_character_over_the_contract(self):
        root, bin_dir, log, out = self._fixture()
        result = self._run(
            root, bin_dir, out,
            "--uid-file", str(self._uid_file(root, "a" * 129)),
            "--expected-label", "denied_premium_required",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log.exists())

    def test_uid_file_must_be_owner_only(self):
        root, bin_dir, log, out = self._fixture()
        uid_file = self._uid_file(root, mode=0o644)
        result = self._run(
            root, bin_dir, out,
            "--uid-file", str(uid_file),
            "--expected-label", "denied_premium_required",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)
        self.assertFalse(log.exists())

    def test_uid_file_must_not_be_a_symlink(self):
        root, bin_dir, log, out = self._fixture()
        target = self._uid_file(root)
        link = root.parent / "uid-link.txt"
        link.symlink_to(target)
        result = self._run(
            root, bin_dir, out,
            "--uid-file", str(link),
            "--expected-label", "denied_premium_required",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)
        self.assertFalse(log.exists())

    def test_uid_file_must_contain_exactly_one_nonempty_line(self):
        root, bin_dir, log, out = self._fixture()
        for value in ("", "first\nsecond\n", "first\rsecond"):
            with self.subTest(value=repr(value)):
                uid_file = self._uid_file(root, value)
                result = self._run(
                    root, bin_dir, out,
                    "--uid-file", str(uid_file),
                    "--expected-label", "denied_premium_required",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("단일행", result.stderr)
        self.assertFalse(log.exists())

    def test_unknown_argument_cannot_override_the_output_path(self):
        root, bin_dir, log, out = self._fixture()
        result = self._run(
            root, bin_dir, out, *self._uid_args(root), "--output-dir", "/tmp/escape"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("허용하지 않는 인자", result.stderr)
        self.assertFalse(log.exists())

    def test_runtime_text_is_not_evaluated_as_bash_arithmetic(self):
        root, bin_dir, log, out = self._fixture()
        marker = root.parent / "arithmetic-command-ran"
        payload = f"BASH_VERSINFO[$(touch {marker})0]"
        result = self._run(
            root, bin_dir, out, *self._uid_args(root), "--max-runtime-seconds", payload
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists(), "max-runtime 문자열이 shell arithmetic으로 실행됐다")
        self.assertFalse(log.exists())


class TestLaunchContract(RunnerHarness):
    def _launch(self):
        root, bin_dir, log, out = self._fixture()
        result = self._run(root, bin_dir, out, *self._uid_args(root),
                           "--max-runtime-seconds", "300", "--iterations", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        return root, log.read_text(), out, result

    def test_output_dir_is_precreated_with_owner_only_mode(self):
        _, _, out, _ = self._launch()
        self.assertTrue(out.is_dir(), "출력 디렉터리를 docker 전에 만들지 않았다")
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)

    def test_env_file_carries_only_canary_contract_values_and_rc_key(self):
        _, log, _, _ = self._launch()
        body = log.split("ENVFILE_BEGIN\n")[1].split("ENVFILE_END")[0]
        keys = sorted(line.split("=")[0] for line in body.strip().splitlines())
        self.assertEqual(
            keys,
            [
                "CANARY_EXPECTED_LABEL",
                "CANARY_IMAGE_ID",
                "CANARY_OUTER_TIMEOUT_SECONDS",
                "CANARY_RUNNER_SHA256",
                "CANARY_RUNNER_SOURCE_HEAD",
                "CANARY_UID",
                # ⛔ PYTHONPATH 는 장식이 아니다 — 없으면 컨테이너에서 `app` import 가 죽는다
                #    (실측: ModuleNotFoundError). 가짜 docker 는 이 경로를 실행하지 않으므로
                #    이 키 존재 자체가 유일한 회귀 방어다.
                "PYTHONPATH",
                "REVENUECAT_API_KEY",
            ],
        )
        self.assertNotIn("ADMIN_PASSWORD", body)
        self.assertNotIn("TELEGRAM", body)
        self.assertIn("CANARY_IMAGE_ID=sha256:" + "a" * 64, body)
        self.assertIn("CANARY_UID=canary-x", body)
        self.assertIn("CANARY_EXPECTED_LABEL=denied_premium_required", body)
        self.assertIn("PYTHONPATH=/app", body)

    def test_key_never_appears_in_argv_or_output(self):
        _, log, _, result = self._launch()
        docker_lines = [l for l in log.splitlines() if l.startswith(("docker ", "timeout "))]
        for line in docker_lines:
            self.assertNotIn(FAKE_KEY, line)
            self.assertNotIn("canary-x", line)
        self.assertNotIn(FAKE_KEY, result.stdout + result.stderr)
        self.assertNotIn("canary-x", result.stdout + result.stderr)

    def test_outer_timeout_wraps_docker_with_kill_after_and_margin(self):
        _, log, _, _ = self._launch()
        timeout_line = next(l for l in log.splitlines() if l.startswith("timeout "))
        self.assertIn("--kill-after=15", timeout_line)
        self.assertIn(" 360 ", timeout_line)  # 300 + 60 margin
        self.assertIn("docker", timeout_line)

    def test_script_is_mounted_read_only_on_the_pinned_image(self):
        _, log, _, _ = self._launch()
        run_line = next(l for l in log.splitlines()
                        if l.startswith("docker run"))
        self.assertIn("scripts/rc_canary.py:ro", run_line)
        self.assertIn("sha256:" + "a" * 64, run_line)
        self.assertIn("--user", run_line)
        self.assertNotIn("--uid", run_line)
        self.assertIn("--max-runtime-seconds 300", run_line)
        self.assertIn("--iterations 5", run_line)  # passthrough 보존

    def test_timeout_failure_kills_any_lingering_container(self):
        root, bin_dir, log, out = self._fixture()
        result = self._run(
            root,
            bin_dir,
            out,
            *self._uid_args(root),
            extra_env={"FAKE_TIMEOUT_FAIL": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        body = log.read_text()
        self.assertIn("docker kill fxi-rc-canary-", body)
        self.assertIn("docker container rm fxi-rc-canary-", body)

    def test_host_lock_rejects_a_second_concurrent_runner(self):
        root, bin_dir, log, out = self._fixture()
        ready = root.parent / "timeout.ready"
        release = root.parent / "timeout.release"
        command = ["bash", str(root / "ops/run_rc_canary.sh"), *self._uid_args(root)]
        first = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(
                root,
                bin_dir,
                out,
                FAKE_TIMEOUT_READY=str(ready),
                FAKE_TIMEOUT_RELEASE=str(release),
            ),
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < deadline:
                if first.poll() is not None:
                    stdout, stderr = first.communicate()
                    self.fail(f"첫 runner가 lock 보유 전에 종료했다: {stdout}{stderr}")
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "첫 runner가 timeout 진입 전 멈췄다")

            second = self._run(root, bin_dir, out, *self._uid_args(root, "canary-y"))
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("이미 실행 중", second.stderr)
        finally:
            release.touch()
            try:
                stdout, stderr = first.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                first.kill()
                stdout, stderr = first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        run_calls = [line for line in log.read_text().splitlines() if line.startswith("docker run")]
        self.assertEqual(len(run_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
