import configparser
import hashlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
OPS = REPO / "ops"
SYSTEMD = OPS / "systemd"
SERVICE = SYSTEMD / "fxi-topic-auth-capture.service"
TIMER = SYSTEMD / "fxi-topic-auth-capture.timer"
ENV = SYSTEMD / "fxi-topic-auth-capture.env"
SUM = SYSTEMD / "fxi-topic-auth-capture.sha256"
INSTALLER = OPS / "install-topic-auth-capture.sh"
PROGRAM = OPS / "capture_topic_auth_rollout.py"
MANAGED_PATHS = (
    Path("ops/capture_topic_auth_rollout.py"),
    Path("ops/install-topic-auth-capture.sh"),
    Path("ops/systemd/fxi-topic-auth-capture.env"),
    Path("ops/systemd/fxi-topic-auth-capture.sha256"),
    Path("ops/systemd/fxi-topic-auth-capture.service"),
    Path("ops/systemd/fxi-topic-auth-capture.timer"),
)


def unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(path.read_text())
    return parser


class TestBoundedSchedule(unittest.TestCase):
    def test_exactly_seven_non_recurring_utc_checkpoints(self):
        entries = re.findall(r"^OnCalendar=(.+)$", TIMER.read_text(), re.MULTILINE)
        self.assertEqual(len(entries), 7)
        self.assertFalse(any("*" in entry for entry in entries))

        observed = [
            datetime.strptime(entry, "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc
            )
            for entry in entries
        ]
        rollout_start = datetime.fromtimestamp(
            1786613120.3894908, tz=timezone.utc
        )
        for day, checkpoint in enumerate(observed, 1):
            delay = checkpoint - (rollout_start + timedelta(days=day))
            self.assertGreaterEqual(delay, timedelta(0))
            self.assertLess(delay, timedelta(seconds=1))

    def test_timer_is_precise_persistent_and_has_no_random_delay(self):
        timer = unit(TIMER)["Timer"]
        self.assertEqual(timer["AccuracySec"], "1s")
        self.assertEqual(timer["RandomizedDelaySec"], "0")
        self.assertEqual(timer["Persistent"], "true")
        self.assertEqual(timer["RemainAfterElapse"], "true")
        self.assertEqual(timer["Unit"], "fxi-topic-auth-capture.service")


class TestPinnedService(unittest.TestCase):
    def test_environment_pins_the_live_window_without_secrets(self):
        values = dict(
            line.split("=", 1)
            for line in ENV.read_text().splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(values["EXPECTED_ROLLOUT_STARTED_AT"], "1786613120.3894908")
        self.assertRegex(values["EXPECTED_IMAGE_ID"], r"^[0-9a-f]{64}$")
        self.assertEqual(values["EXPECTED_STAGE"], "compatibility")
        self.assertEqual(values["EXPECTED_DISPATCHER_ENABLED"], "false")
        self.assertEqual(values["DEPLOYMENT_LABEL"], "843462e")
        self.assertFalse(any("password" in key.lower() or "token" in key.lower()
                             for key in values))

    def test_checksum_pins_the_exact_capture_program(self):
        digest, target = SUM.read_text().strip().split("  ", 1)
        self.assertEqual(
            target,
            "/home/ubuntu/exchange-rate/ops/capture_topic_auth_rollout.py",
        )
        self.assertEqual(digest, hashlib.sha256(PROGRAM.read_bytes()).hexdigest())

    def test_service_keeps_window_change_as_a_failure(self):
        text = SERVICE.read_text()
        service = unit(SERVICE)["Service"]
        self.assertNotIn("SuccessExitStatus", service)
        self.assertIn("sha256sum --check --status", service["ExecStartPre"])
        self.assertIn("CHECKPOINT=bounded-7-day-window", SERVICE.read_text())
        for option in (
            "--expected-rollout-started-at",
            "--expected-image-id",
            "--expected-stage",
            "--expected-dispatcher-enabled",
        ):
            self.assertIn(option, service["ExecStart"])
        self.assertNotRegex(text, r"(?i)curl|websocket|firebase|revenuecat")

    def test_service_is_owner_scoped_and_writes_only_the_artifact_directory(self):
        service = unit(SERVICE)["Service"]
        self.assertEqual(service["User"], "ubuntu")
        self.assertEqual(service["Group"], "ubuntu")
        self.assertEqual(service["UMask"], "0077")
        self.assertEqual(service["NoNewPrivileges"], "true")
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["ProtectHome"], "read-only")
        self.assertEqual(
            service["ReadWritePaths"], "/home/ubuntu/logs/topic-auth-rollout"
        )


class TestInstallerContract(unittest.TestCase):
    def _git_tree(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        temporary_root = Path(temporary.name)
        root = temporary_root / "repo"
        remote = temporary_root / "origin.git"
        root.mkdir()
        subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
        for relative in MANAGED_PATHS:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / relative, destination)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Capture Test",
                "-c",
                "user.email=capture-test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "push", "-q", "-u", "origin", "HEAD:master"],
            cwd=root,
            check=True,
        )
        return root

    def _commit(
        self, root: Path, message: str, *paths: str, push: bool = True
    ) -> None:
        subprocess.run(["git", "add", *paths], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Capture Test",
                "-c",
                "user.email=capture-test@example.invalid",
                "commit",
                "-qm",
                message,
            ],
            cwd=root,
            check=True,
        )
        if push:
            subprocess.run(
                ["git", "push", "-q", "origin", "HEAD:master"],
                cwd=root,
                check=True,
            )

    def _source_check(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; check_source',
                "capture-source-check",
                str(root / "ops/install-topic-auth-capture.sh"),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )

    def _assert_source_baseline(self, root: Path) -> None:
        result = self._source_check(root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_never_uses_cron_and_validates_before_enable(self):
        text = INSTALLER.read_text()
        self.assertNotIn("crontab", text)
        validate = text.index('systemctl start "$SERVICE"')
        enable = text.index('systemctl enable --now "$TIMER"')
        self.assertLess(validate, enable)
        self.assertIn("check_no_dropins", text)
        self.assertIn("check_paths_are_not_symlinks", text)
        self.assertIn("cmp -s", text)

    def test_installer_requires_all_inputs_to_match_git_head(self):
        text = INSTALLER.read_text()
        for path in (
            "ops/capture_topic_auth_rollout.py",
            "ops/install-topic-auth-capture.sh",
            "ops/systemd/fxi-topic-auth-capture.env",
            "ops/systemd/fxi-topic-auth-capture.sha256",
            "ops/systemd/fxi-topic-auth-capture.service",
            "ops/systemd/fxi-topic-auth-capture.timer",
        ):
            self.assertIn(path, text)
        self.assertIn("ls-files --error-unmatch", text)
        self.assertIn('diff --quiet -- "${MANAGED_PATHS[@]}"', text)
        self.assertIn('diff --cached --quiet -- "${MANAGED_PATHS[@]}"', text)
        self.assertIn("rev-parse --symbolic-full-name '@{upstream}'", text)
        self.assertIn('refs/remotes/*', text)
        self.assertIn('[ "$head" = "$upstream_head" ]', text)

    def test_source_check_rejects_unstaged_managed_input(self):
        root = self._git_tree()
        self._assert_source_baseline(root)
        with (root / "ops/capture_topic_auth_rollout.py").open("a") as stream:
            stream.write("\n# unstaged drift\n")

        result = self._source_check(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unstaged drift", result.stderr)

    def test_source_check_rejects_clean_but_unpushed_commit(self):
        root = self._git_tree()
        self._assert_source_baseline(root)
        env = root / "ops/systemd/fxi-topic-auth-capture.env"
        env.write_text(env.read_text() + "# local-only commit\n")
        self._commit(
            root,
            "local scheduler change",
            "ops/systemd/fxi-topic-auth-capture.env",
            push=False,
        )

        result = self._source_check(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local upstream ref와 다르다", result.stderr)

    def test_source_check_rejects_committed_program_sha_drift(self):
        root = self._git_tree()
        self._assert_source_baseline(root)
        program = root / "ops/capture_topic_auth_rollout.py"
        program.write_bytes(program.read_bytes() + b"\n# committed SHA drift\n")
        self._commit(
            root,
            "drift program without checksum",
            "ops/capture_topic_auth_rollout.py",
        )

        result = self._source_check(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("capture program SHA drift", result.stderr)

    def test_source_check_requires_runtime_program_sha_guard(self):
        root = self._git_tree()
        self._assert_source_baseline(root)
        service = root / "ops/systemd/fxi-topic-auth-capture.service"
        service.write_text(
            "\n".join(
                line
                for line in service.read_text().splitlines()
                if "ExecStartPre=/usr/bin/sha256sum --check --status" not in line
            )
            + "\n"
        )
        self._commit(
            root,
            "remove runtime checksum guard",
            "ops/systemd/fxi-topic-auth-capture.service",
        )

        result = self._source_check(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("service 가 capture program SHA", result.stderr)

    def test_failed_validation_capture_never_arms_the_timer(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        events = Path(temporary.name) / "systemctl-events"
        harness = textwrap.dedent(
            r'''
            EVENTS="$2"
            source "$1"
            check_source() { :; }
            check_no_dropins() { :; }
            check_paths_are_not_symlinks() { :; }
            check_install_window_open() { :; }
            check_installed() { :; }
            install() { :; }
            function systemd-analyze { :; }
            journalctl() { :; }
            systemctl() {
              printf '%s\n' "$*" >> "$EVENTS"
              if [ "$1" = start ] && [ "$2" = "$SERVICE" ]; then
                return 1
              fi
              return 0
            }
            install_schedule
            '''
        )

        result = subprocess.run(
            ["bash", "-c", harness, "capture-install-test", str(INSTALLER), str(events)],
            capture_output=True,
            text=True,
        )
        observed = events.read_text().splitlines()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"start fxi-topic-auth-capture.service", observed)
        self.assertNotIn(f"enable --now fxi-topic-auth-capture.timer", observed)

    def test_reinstall_disables_old_timer_before_replacing_inputs(self):
        text = INSTALLER.read_text()
        disable = text.index('systemctl disable --now "$TIMER"')
        first_install = text.index('install -m 0600 "$ENV_SOURCE"')
        self.assertLess(disable, first_install)
        self.assertNotIn('systemctl stop "$TIMER"', text)

    def test_installer_exposes_only_explicit_install_and_check_modes(self):
        text = INSTALLER.read_text()
        self.assertRegex(text, r"--check\)\s+require_root --check")
        self.assertRegex(text, r"--install\)\s+require_root --install")
        self.assertNotRegex(text, r"\$\{1:-\}\s*=|case.*\*.*install_schedule")


if __name__ == "__main__":
    unittest.main()
