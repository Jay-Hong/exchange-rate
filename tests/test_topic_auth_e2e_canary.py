import argparse
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from scripts import topic_auth_e2e_canary as canary


def off_status():
    return {
        "command": "status",
        "state": "OFF",
        "file_auth_stage": canary.REQUIRED_AUTH_STAGE,
        "auth_stage": canary.REQUIRED_AUTH_STAGE,
        "fx_topic_enabled": True,
        "activation_pending": False,
        "operation_locked": False,
    }


class TestTokenLoading(unittest.TestCase):
    def test_stdin_requires_two_distinct_values(self):
        args = argparse.Namespace(tokens_stdin=True, token_files=None)
        self.assertEqual(
            canary.load_token_pair(args, io.StringIO("paid\nfree\n")),
            ("paid", "free"),
        )
        with self.assertRaises(canary.CanaryFailure):
            canary.load_token_pair(args, io.StringIO("same\nsame\n"))

    def test_token_files_must_be_owner_only(self):
        with tempfile.TemporaryDirectory() as temp:
            paid = Path(temp) / "paid"
            free = Path(temp) / "free"
            paid.write_text("paid")
            free.write_text("free")
            paid.chmod(0o600)
            free.chmod(0o644)
            args = argparse.Namespace(
                tokens_stdin=False,
                token_files=(str(paid), str(free)),
            )
            with self.assertRaisesRegex(canary.CanaryFailure, "0600"):
                canary.load_token_pair(args)

    def test_token_file_must_be_a_bounded_regular_single_line(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paid = root / "paid"
            free = root / "free"
            paid.write_text("paid\nsecond\n")
            free.write_text("free\n")
            paid.chmod(0o600)
            free.chmod(0o600)
            args = argparse.Namespace(
                tokens_stdin=False,
                token_files=(str(paid), str(free)),
            )
            with self.assertRaisesRegex(canary.CanaryFailure, "단일행"):
                canary.load_token_pair(args)

            paid.unlink()
            paid.symlink_to(free)
            with self.assertRaises(canary.CanaryFailure):
                canary.load_token_pair(args)


class TestSecretTransport(unittest.TestCase):
    def test_probe_tokens_are_stdin_only(self):
        captured = {}

        def fake_run(command, *, timeout, stdin_text=None):
            captured["command"] = command
            captured["stdin"] = stdin_text
            return 0, {"ok": True}

        with patch.object(canary, "_run_json_command", side_effect=fake_run):
            canary.run_container_probe("paid-secret", "free-secret", 30)
        argv = " ".join(captured["command"])
        self.assertNotIn("paid-secret", argv)
        self.assertNotIn("free-secret", argv)
        self.assertEqual(captured["stdin"], "paid-secret\nfree-secret\n")


class TestWatchdogArming(unittest.TestCase):
    def test_canary_deadlines_cover_the_watchdog_contract(self):
        source = canary.WATCHDOG.read_text()

        def value(name):
            match = re.search(rf"^{name}=(\d+)", source, re.MULTILINE)
            self.assertIsNotNone(match, name)
            return int(match.group(1))

        self.assertEqual(
            canary.WATCHDOG_HOLD_GRACE_SECONDS,
            value("HOLD_GRACE"),
        )
        self.assertGreaterEqual(
            canary.CONTRACT_AFTER_FIRE_SECONDS,
            value("OFF_BUDGET") + value("STATUS_BUDGET") + 30,
        )

    def test_ready_is_required_and_process_is_detached(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "watchdog.log"
            kwargs_seen = {}

            class FakeProcess:
                pid = 4321
                returncode = None

                @staticmethod
                def poll():
                    return None

            def fake_popen(command, **kwargs):
                kwargs_seen.update(kwargs)
                log.write_text("READY pid=4321 watcher=4322 fire=x contract=y\n")
                return FakeProcess()

            process = canary.arm_watchdog(
                1000,
                1500,
                log,
                popen=fake_popen,
                clock=lambda: 0.0,
                sleeper=lambda _: None,
            )
        self.assertEqual(process.pid, 4321)
        self.assertTrue(kwargs_seen["start_new_session"])
        self.assertTrue(kwargs_seen["close_fds"])
        self.assertIs(kwargs_seen["stdin"], canary.subprocess.DEVNULL)


class TestCanaryTransaction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.watchdog_log = Path(self.temp.name) / "watchdog.log"
        self.artifact = Path(self.temp.name) / "result.json"

    def _common_patches(self, events, probe_effect=None):
        def topic_flag(command, timeout):
            events.append(f"flag:{command}")
            if command == "status" and events.count("flag:status") == 1:
                return 0, off_status()
            if command == "on":
                return 0, {"command": "on", "ok": True, "state": "ON"}
            if command == "off":
                return 0, {"command": "off", "ok": True, "state": "OFF"}
            return 0, {"command": "status", "state": "OFF"}

        def arm(*args, **kwargs):
            events.append("watchdog:ready")
            return SimpleNamespace(pid=1234)

        def run_probe(*args, **kwargs):
            events.append("probe")
            if probe_effect is not None:
                raise probe_effect
            return {"ok": True}

        return (
            patch.object(canary, "run_topic_flag", side_effect=topic_flag),
            patch.object(canary, "verify_container_probe_hash", return_value="a" * 64),
            patch.object(
                canary,
                "_new_artifact_paths",
                return_value=(self.watchdog_log, self.artifact),
            ),
            patch.object(canary, "arm_watchdog", side_effect=arm),
            patch.object(canary, "run_container_probe", side_effect=run_probe),
            patch.object(canary, "_write_artifact"),
        )

    def test_watchdog_precedes_on_and_success_always_returns_off(self):
        events = []
        patches = self._common_patches(events)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = canary.execute_canary("paid", "free", epoch=lambda: 1000.0)
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["watchdog"]["permanent_on_not_before_epoch"],
            1000
            + canary.FIRE_DELAY_SECONDS
            + canary.CONTRACT_AFTER_FIRE_SECONDS
            + canary.WATCHDOG_HOLD_GRACE_SECONDS,
        )
        self.assertEqual(
            result["watchdog"]["done_marker"],
            f"{self.watchdog_log}.done",
        )
        self.assertEqual(result["preflight"]["auth_stage"], canary.REQUIRED_AUTH_STAGE)
        self.assertLess(events.index("watchdog:ready"), events.index("flag:on"))
        self.assertEqual(
            events,
            ["flag:status", "watchdog:ready", "flag:on", "probe", "flag:off", "flag:status"],
        )

    def test_probe_failure_still_runs_off_and_status(self):
        events = []
        patches = self._common_patches(
            events, probe_effect=canary.CanaryFailure("wire mismatch")
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             self.assertRaisesRegex(canary.CanaryFailure, "wire mismatch"):
            canary.execute_canary("paid", "free", epoch=lambda: 1000.0)
        self.assertEqual(events[-2:], ["flag:off", "flag:status"])

    def test_non_off_initial_state_stops_before_watchdog(self):
        with patch.object(
            canary,
            "run_topic_flag",
            return_value=(0, {**off_status(), "state": "ON"}),
        ), patch.object(canary, "arm_watchdog") as arm, \
             self.assertRaises(canary.CanaryFailure):
            canary.execute_canary("paid", "free")
        arm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
