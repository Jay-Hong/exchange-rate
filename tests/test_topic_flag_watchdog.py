import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = REPO_ROOT / "scripts" / "topic_flag_watchdog.sh"
LINUX_TOOLS = ("flock", "setsid", "timeout")


def wait_for_text(path: Path, needle: str, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        if needle in text:
            return text
        time.sleep(0.1)
    raise AssertionError(f"{needle!r} not found in {path}\n{text}")


class TestTopicFlagWatchdogSyntax(unittest.TestCase):
    def test_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(WATCHDOG)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(
    all(shutil.which(tool) for tool in LINUX_TOOLS),
    "watchdog integration tests require Linux util-linux/coreutils",
)
class TestTopicFlagWatchdogIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "exchange-rate" / "scripts").mkdir(parents=True)
        (self.home / "logs").mkdir()
        self.script = self.home / "topic_flag_watchdog.sh"

        # Keep production behavior intact; only shorten the polling interval in
        # the disposable test copy so SIGKILL lock-release tests stay fast.
        source = WATCHDOG.read_text()
        old = "POLL_SECONDS=2            # 발화·계약 시각 감지 지연 상한"
        new = "POLL_SECONDS=1            # test copy"
        self.assertEqual(source.count(old), 1)
        self.script.write_text(source.replace(old, new))
        self.script.chmod(0o700)

        self.state = self.home / "runtime-state"
        self.state.write_text("OFF")
        stub = self.home / "exchange-rate" / "scripts" / "topic_flag.py"
        stub.write_text(
            """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

home = pathlib.Path(os.environ["HOME"])
state_path = home / "runtime-state"
command = sys.argv[1]
if command == "off":
    if (home / "off-slow").exists():
        time.sleep(30)
    if not (home / "off-noop").exists():
        state_path.write_text("OFF")
    print(json.dumps({"command": "off", "ok": True, "state": "OFF"}))
    raise SystemExit(0)
if (home / "status-hang").exists():
    time.sleep(30)
print(json.dumps({"command": "status", "state": state_path.read_text().strip()}))
"""
        )
        stub.chmod(0o700)
        self.process_groups = set()
        self.processes = []

    def tearDown(self):
        for pgid in self.process_groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def start(self, fire_at: int, contract_at: int, log_name: str, extra_env=None):
        log = self.home / "logs" / log_name
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        if extra_env:
            env.update(extra_env)
        process = subprocess.Popen(
            ["setsid", "bash", str(self.script), str(fire_at), str(contract_at), str(log)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.process_groups.add(process.pid)
        self.processes.append(process)
        return process, log

    @staticmethod
    def ready_pids(text: str):
        match = re.search(r"READY pid=(\d+) watcher=(\d+)", text)
        if not match:
            raise AssertionError(f"READY did not contain exact pids:\n{text}")
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def wait_for_child_command(parent_pid: int, command: str, timeout: float = 5.0) -> int:
        deadline = time.monotonic() + timeout
        children_path = Path(f"/proc/{parent_pid}/task/{parent_pid}/children")
        while time.monotonic() < deadline:
            try:
                children = children_path.read_text().split()
            except FileNotFoundError:
                children = []
            for child in children:
                try:
                    comm = Path(f"/proc/{child}/comm").read_text().strip()
                except FileNotFoundError:
                    continue
                if comm == command:
                    return int(child)
            time.sleep(0.01)
        raise AssertionError(f"{command!r} child not found for pid={parent_pid}")

    def test_live_instance_rejects_duplicate_but_main_sigkill_allows_rearm(self):
        now = int(time.time())
        first, first_log = self.start(now + 30, now + 40, "first.log")
        first_text = wait_for_text(first_log, "READY")
        main_pid, watcher_pid = self.ready_pids(first_text)
        self.assertEqual(main_pid, first.pid)
        os.kill(watcher_pid, 0)

        duplicate, duplicate_log = self.start(now + 20, now + 35, "duplicate.log")
        self.assertEqual(duplicate.wait(timeout=5), 4)
        self.assertIn("중복 무장 거부", duplicate_log.read_text())

        # Neither the contract observer nor a polling sleep may inherit the
        # singleton flock. Synchronize on a live sleep child, then kill main;
        # waiting for that child to exit would mask the inheritance bug.
        self.assertFalse(Path(f"/proc/{watcher_pid}/fd/9").exists())
        sleep_pid = self.wait_for_child_command(main_pid, "sleep")
        self.assertFalse(Path(f"/proc/{sleep_pid}/fd/9").exists())
        os.kill(main_pid, signal.SIGKILL)
        first.wait(timeout=5)

        replacement, replacement_log = self.start(now + 20, now + 35, "replacement.log")
        replacement_text = wait_for_text(replacement_log, "READY")
        replacement_main, replacement_watcher = self.ready_pids(replacement_text)
        self.assertEqual(replacement_main, replacement.pid)
        os.kill(replacement_watcher, 0)

    def test_orphaned_off_child_does_not_block_rearm(self):
        # `off` is the longest-lived child (up to OFF_BUDGET=420s in production).
        # If it inherits the singleton flock, killing main leaves a lock with no
        # process able to converge to OFF, and the replacement is refused with
        # exit 4 -- the refusal even names the already-dead pid as "alive".
        (self.home / "off-slow").touch()
        (self.home / "off-noop").touch()
        self.state.write_text("ON")
        now = int(time.time())
        first, first_log = self.start(now + 1, now + 600, "orphan-first.log")
        main_pid, _ = self.ready_pids(wait_for_text(first_log, "FIRE — 수렴 시작"))
        time.sleep(2)  # let the slow `off` child actually start

        os.kill(main_pid, signal.SIGKILL)
        first.wait(timeout=5)
        time.sleep(1.2)

        replacement, replacement_log = self.start(now + 2, now + 400, "orphan-replacement.log")
        replacement_main, replacement_watcher = self.ready_pids(
            wait_for_text(replacement_log, "READY", timeout=10)
        )
        self.assertEqual(replacement_main, replacement.pid)
        os.kill(replacement_watcher, 0)
        text = replacement_log.read_text()
        self.assertNotIn("중복 무장 거부", text)
        self.assertNotIn("singleton 격하", text)

    def test_orphaned_status_probe_does_not_block_rearm(self):
        # Closing fd 9 only on the pipeline inside `$(probe_status)` is not
        # enough if the command-substitution shell itself keeps the lock while
        # a status call hangs. Killing main must still permit immediate rearm.
        (self.home / "status-hang").touch()
        self.state.write_text("ON")
        now = int(time.time())
        first, first_log = self.start(now + 1, now + 600, "status-first.log")
        main_pid, _ = self.ready_pids(wait_for_text(first_log, "off attempt=1"))
        time.sleep(0.5)  # status stub is now inside its 30s sleep

        os.kill(main_pid, signal.SIGKILL)
        first.wait(timeout=5)

        replacement, replacement_log = self.start(
            now + 300, now + 500, "status-replacement.log"
        )
        replacement_text = wait_for_text(replacement_log, "READY", timeout=10)
        replacement_main, replacement_watcher = self.ready_pids(replacement_text)
        self.assertEqual(replacement_main, replacement.pid)
        os.kill(replacement_watcher, 0)

    def test_orphaned_timestamp_subshell_does_not_block_rearm(self):
        # Even a normally short command substitution can become long under
        # resource pressure. Its shell must close fd 9 before waiting for the
        # helper, rather than relying on an operator to retry a rejected arm.
        real_date = shutil.which("date")
        self.assertIsNotNone(real_date)
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        date_wrapper = bin_dir / "date"
        date_wrapper.write_text(
            f"""#!/bin/bash
if [ -e "$HOME/date-hang" ]; then
    echo entered >"$HOME/date-entered"
    sleep 30
fi
exec {real_date} "$@"
"""
        )
        date_wrapper.chmod(0o700)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

        now = int(time.time())
        first, first_log = self.start(now + 300, now + 600, "timestamp-first.log", env)
        first_text = wait_for_text(first_log, "ARMED")
        main_pid, watcher_pid = self.ready_pids(first_text)
        os.kill(watcher_pid, signal.SIGKILL)
        (self.home / "date-hang").touch()
        wait_for_text(self.home / "date-entered", "entered", timeout=5)

        os.kill(main_pid, signal.SIGKILL)
        first.wait(timeout=5)
        (self.home / "date-hang").unlink()

        replacement, replacement_log = self.start(
            now + 300, now + 500, "timestamp-replacement.log", env
        )
        replacement_text = wait_for_text(replacement_log, "READY", timeout=10)
        replacement_main, replacement_watcher = self.ready_pids(replacement_text)
        self.assertEqual(replacement_main, replacement.pid)
        os.kill(replacement_watcher, 0)

    def test_lock_is_never_bypassed_using_dead_metadata_pid(self):
        # Metadata is diagnostic only. A dead/reused pid does not prove that
        # bypassing a real kernel lock is safe; doing so lets every subsequent
        # invocation arm concurrently without a singleton.
        doomed = subprocess.Popen(["sleep", "60"])
        doomed.kill()
        doomed.wait(timeout=5)
        lock = self.home / ".topic_flag_watchdog.lock"
        lock.write_text(f"pid={doomed.pid} fire=0 contract=0 log=/dev/null\n")

        holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "60"])
        self.processes.append(holder)
        self.addCleanup(holder.kill)
        time.sleep(0.5)

        now = int(time.time())
        process, log = self.start(now + 30, now + 60, "degraded.log")
        self.assertEqual(process.wait(timeout=5), 4)
        text = log.read_text()
        self.assertIn("중복 무장 거부", text)
        self.assertNotIn("READY", text)

    def test_contract_watcher_uses_a_fresh_status_probe(self):
        (self.home / "off-noop").touch()
        now = int(time.time())
        contract = now + 7
        process, log = self.start(now + 2, contract, "fresh-contract.log")
        wait_for_text(log, "CONFIRMED OFF")

        # Main is now in its 15s hold sleep. A cached-state watcher would miss
        # this late ON transition; the independent contract probe must see it.
        self.state.write_text("ON")
        sentinel = self.home / "logs" / f"WATCHDOG_CONTRACT_BREACH.{contract}"
        wait_for_text(sentinel, "state=ON", timeout=10)
        self.assertIn("CONTRACT BREACH", log.read_text())
        self.assertIsNone(process.poll())

    def test_retry_sleep_is_clamped_to_the_remaining_contract_budget(self):
        (self.home / "off-noop").touch()
        self.state.write_text("ON")
        now = int(time.time())
        process, log = self.start(now + 1, now + 5, "retry-clamp.log")

        # RETRY_FAST is 20s, but only about 4s remain after the first attempt.
        # The second attempt must start at the contract boundary instead of
        # sleeping through the last pre-contract opportunity.
        wait_for_text(log, "off attempt=2", timeout=9)
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
