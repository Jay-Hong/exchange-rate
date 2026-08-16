import hashlib
import json
import stat
import subprocess
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from app import ws_connection_metrics
from ops import capture_topic_auth_rollout as capture


IMAGE = "a" * 64
CONTAINER = "b" * 64
HEAD = "c" * 40
STARTED = "1786613120.3894908"
NOW = datetime(2026, 8, 14, 9, 25, tzinfo=timezone.utc)


def response(*, started=STARTED, attempts=4, first_seen=4):
    return json.dumps(
        {
            "metrics": {
                "topic_auth_rollout": {
                    "started_at_epoch_seconds": float(started),
                    "stage": "compatibility",
                    "topic_dispatcher_enabled": False,
                    "anonymous_subscribe_attempts_total": attempts,
                    "unverified_token_bearing_subscribe_attempts_total": attempts,
                    "unverified_token_bearing_policy_attempts_total": attempts,
                    "unverified_token_bearing_final_stage_rc_candidate_attempts_total": attempts,
                    "unverified_token_bearing_final_stage_rc_candidate_topics": [
                        "fx:usd-krw",
                        "usdt:krw",
                    ],
                    "unverified_token_bearing_policy_first_seen_connections_total": first_seen,
                    "unverified_token_bearing_per_topic_attempts": {
                        "fx:usd-krw": attempts,
                        "usdt:krw": attempts,
                    },
                    "unverified_token_bearing_per_topic_first_seen_connections": {
                        "fx:usd-krw": first_seen,
                        "usdt:krw": first_seen,
                    },
                    "unverified_token_bearing_active_connections_tracked": 0,
                    "anonymous_subscribe_arrival": {
                        "bucket_seconds": 10,
                        "buckets_kept": 60,
                        "buckets_present": 1,
                        "max_in_bucket": attempts,
                        "current_bucket": attempts,
                        "peak_in_bucket_since_start": attempts,
                    },
                    "unverified_token_bearing_subscribe_arrival": {
                        "bucket_seconds": 10,
                        "buckets_kept": 60,
                        "buckets_present": 1,
                        "max_in_bucket": attempts,
                        "current_bucket": attempts,
                        "peak_in_bucket_since_start": attempts,
                    },
                    "unverified_token_bearing_attempts_on_one_observation_epoch_max": attempts,
                }
            }
        },
        separators=(",", ":"),
    ).encode()


def inspect(*, container=CONTAINER, image=IMAGE, running=True):
    return (
        f"{container}\tsha256:{image}\t2026-08-13T09:25:16.860622799Z\t"
        f"{'true' if running else 'false'}\n"
    ).encode()


class FakeRunner:
    def __init__(self, *, body=None, before=None, after=None, after_error=None):
        self.body = response() if body is None else body
        self.before = inspect() if before is None else before
        self.after = self.before if after is None else after
        self.after_error = after_error
        self.inspect_calls = 0
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append((list(command), timeout))
        if command[:2] == ["docker", "inspect"]:
            self.inspect_calls += 1
            if self.inspect_calls == 2 and self.after_error is not None:
                raise self.after_error
            return self.before if self.inspect_calls == 1 else self.after
        if command[:2] == ["docker", "exec"]:
            return self.body
        if command[:3] == ["git", "-C", "/repo"]:
            return (HEAD + "\n").encode()
        raise AssertionError(f"unexpected command: {command}")


class CaptureFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "captures"

    def run_capture(self, runner=None, **changes):
        args = {
            "expected_rollout_started_at": STARTED,
            "expected_image_id": IMAGE,
            "expected_stage": "compatibility",
            "expected_dispatcher_enabled": False,
            "checkpoint": "T+24h",
            "output_dir": self.output,
            "repo_root": Path("/repo"),
            "deployment_label": "843462e",
            "runner": runner or FakeRunner(),
            "now": NOW,
        }
        args.update(changes)
        return capture.capture(**args)


class TestSuccessfulCapture(CaptureFixture):
    def test_capture_bucket_identity_matches_production_defaults(self):
        """캡처의 wire identity 상수는 production ring 기본값과 함께 바뀌어야 한다."""
        self.assertEqual(
            capture.ARRIVAL_BUCKET_SECONDS,
            ws_connection_metrics.HANDSHAKE_BUCKET_SECONDS,
        )
        self.assertEqual(
            capture.ARRIVAL_BUCKETS_KEPT,
            ws_connection_metrics.HANDSHAKE_BUCKETS_KEPT,
        )

    def test_preserves_raw_bytes_and_separates_derived_metadata(self):
        raw = response() + b"\n"
        result = self.run_capture(FakeRunner(body=raw))

        self.assertFalse(result.window_changed)
        self.assertTrue(result.metric_schema_valid)
        self.assertEqual(result.raw_path.read_bytes(), raw)
        meta = json.loads(result.meta_path.read_text())
        self.assertEqual(meta["window"]["status"], "matched")
        self.assertEqual(meta["runtime"]["host_repo_head"], HEAD)
        self.assertEqual(meta["runtime"]["deployment_label"], "843462e")
        self.assertEqual(
            meta["runtime"]["deployment_label_evidence"],
            "operator_supplied_not_runtime_verified",
        )
        self.assertTrue(meta["source"]["raw_is_exact_http_response_body"])
        self.assertFalse(meta["manual_websocket_probe_sent"])
        self.assertFalse(meta["firebase_or_revenuecat_called"])
        self.assertTrue(meta["metric_schema"]["valid"])
        self.assertEqual(
            meta["source"]["capture_program_sha256"],
            hashlib.sha256(Path(capture.__file__).read_bytes()).hexdigest(),
        )

    def test_sidecars_are_relative_and_recomputable(self):
        result = self.run_capture()
        for artifact in (result.raw_path, result.meta_path):
            sidecar = artifact.with_name(artifact.name + ".sha256")
            digest, relative_name = sidecar.read_text().strip().split("  ", 1)
            self.assertEqual(relative_name, artifact.name)
            self.assertEqual(digest, hashlib.sha256(artifact.read_bytes()).hexdigest())

    def test_output_permissions_are_owner_only(self):
        result = self.run_capture()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for path in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)

    def test_fetch_command_does_not_contain_a_password_value(self):
        runner = FakeRunner()
        self.run_capture(runner)
        fetch = next(command for command, _ in runner.commands if command[:2] == ["docker", "exec"])
        joined = " ".join(fetch)
        self.assertIn("ADMIN_PASSWORD", joined)
        self.assertNotIn("test-secret", joined)
        self.assertNotIn("curl", joined)

    def test_container_side_fetch_program_is_valid_python(self):
        compile(capture.ADMIN_FETCH_PY, "<admin-fetch>", "exec")

    def test_inspect_command_uses_a_real_separator_not_backslash_n(self):
        template = capture._inspect_command("app")[-1]
        self.assertEqual(template.count("\t"), 3)
        self.assertNotIn("\\n", template)


class TestFailClosedWindow(CaptureFixture):
    def test_restart_is_captured_but_marked_changed(self):
        result = self.run_capture(FakeRunner(body=response(started="1786619999.0")))
        self.assertTrue(result.window_changed)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(meta["window"]["rollout_restarted"])
        self.assertEqual(meta["window"]["status"], "changed")

    def test_image_change_is_captured_but_marked_changed(self):
        result = self.run_capture(
            FakeRunner(before=inspect(image="d" * 64), after=inspect(image="d" * 64))
        )
        self.assertTrue(result.window_changed)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(meta["window"]["image_changed"])

    def test_container_change_during_capture_is_detected(self):
        result = self.run_capture(
            FakeRunner(after=inspect(container="e" * 64))
        )
        self.assertTrue(result.window_changed)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(meta["window"]["container_changed_during_capture"])

    def test_stage_change_is_captured_but_marked_changed(self):
        result = self.run_capture(expected_stage="reject_anonymous_fx")
        self.assertTrue(result.window_changed)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(meta["window"]["stage_changed"])

    def test_dispatcher_change_is_captured_but_marked_changed(self):
        result = self.run_capture(expected_dispatcher_enabled=True)
        self.assertTrue(result.window_changed)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(meta["window"]["dispatcher_changed"])

    def test_missing_token_metric_is_preserved_but_not_accepted(self):
        document = json.loads(response())
        del document["metrics"]["topic_auth_rollout"][
            "unverified_token_bearing_policy_attempts_total"
        ]
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        self.assertTrue(result.raw_path.exists(), "malformed live evidence was discarded")
        meta = json.loads(result.meta_path.read_text())
        self.assertFalse(meta["metric_schema"]["valid"])
        self.assertTrue(any("policy_attempts_total" in error
                            for error in meta["metric_schema"]["errors"]))

    def test_missing_arrival_peak_is_preserved_but_not_accepted(self):
        document = json.loads(response())
        del document["metrics"]["topic_auth_rollout"][
            "anonymous_subscribe_arrival"
        ]["peak_in_bucket_since_start"]
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        meta = json.loads(result.meta_path.read_text())
        self.assertIn(
            "anonymous_subscribe_arrival must have the fixed HandshakeBuckets schema",
            meta["metric_schema"]["errors"],
        )

    def test_arrival_bucket_identity_drift_is_preserved_but_not_accepted(self):
        document = json.loads(response())
        rollout = document["metrics"]["topic_auth_rollout"]
        rollout["anonymous_subscribe_arrival"]["bucket_seconds"] = 11
        rollout["unverified_token_bearing_subscribe_arrival"]["buckets_kept"] = 61
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        meta = json.loads(result.meta_path.read_text())
        identity_errors = [
            error for error in meta["metric_schema"]["errors"]
            if "bucket identity must be 10s x 60" in error
        ]
        self.assertEqual(len(identity_errors), 2)

    def test_arrival_presence_and_max_emptiness_must_agree(self):
        document = json.loads(response())
        arrival = document["metrics"]["topic_auth_rollout"][
            "anonymous_subscribe_arrival"
        ]
        arrival["buckets_present"] = 0
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(any(
            "buckets_present and max_in_bucket emptiness must agree" in error
            for error in meta["metric_schema"]["errors"]
        ))

    def test_impossible_epoch_high_water_is_preserved_but_not_accepted(self):
        document = json.loads(response(attempts=4))
        document["metrics"]["topic_auth_rollout"][
            "unverified_token_bearing_attempts_on_one_observation_epoch_max"
        ] = 5
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(any(
            "must not exceed unverified_token_bearing_subscribe_attempts_total" in error
            for error in meta["metric_schema"]["errors"]
        ))

    def test_per_topic_key_drift_is_preserved_but_not_accepted(self):
        document = json.loads(response())
        del document["metrics"]["topic_auth_rollout"][
            "unverified_token_bearing_per_topic_first_seen_connections"
        ]["usdt:krw"]
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        meta = json.loads(result.meta_path.read_text())
        self.assertIn(
            "token-bearing per-topic maps must have identical fixed keys",
            meta["metric_schema"]["errors"],
        )

    def test_impossible_aggregate_counts_are_preserved_but_not_accepted(self):
        document = json.loads(response(attempts=4, first_seen=4))
        rollout = document["metrics"]["topic_auth_rollout"]
        rollout["unverified_token_bearing_policy_attempts_total"] = 3
        rollout["unverified_token_bearing_final_stage_rc_candidate_attempts_total"] = 4
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(any(
            "RC candidates <= policy attempts" in error
            for error in meta["metric_schema"]["errors"]
        ))

    def test_policy_first_seen_cannot_exceed_policy_attempts(self):
        document = json.loads(response(attempts=4, first_seen=4))
        rollout = document["metrics"]["topic_auth_rollout"]
        rollout["unverified_token_bearing_policy_attempts_total"] = 3
        rollout["unverified_token_bearing_final_stage_rc_candidate_attempts_total"] = 3
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        meta = json.loads(result.meta_path.read_text())
        self.assertIn(
            "token-bearing policy first-seen connections must not exceed policy attempts",
            meta["metric_schema"]["errors"],
        )

    def test_active_connections_cannot_exceed_subscribe_attempts(self):
        document = json.loads(response(attempts=4, first_seen=4))
        rollout = document["metrics"]["topic_auth_rollout"]
        rollout["unverified_token_bearing_active_connections_tracked"] = 5
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        meta = json.loads(result.meta_path.read_text())
        self.assertIn(
            "active token-bearing connections must not exceed subscribe attempts",
            meta["metric_schema"]["errors"],
        )

    def test_impossible_first_seen_counts_are_preserved_but_not_accepted(self):
        document = json.loads(response(attempts=4, first_seen=4))
        rollout = document["metrics"]["topic_auth_rollout"]
        rollout["unverified_token_bearing_per_topic_first_seen_connections"][
            "usdt:krw"
        ] = 5
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        self.assertFalse(result.metric_schema_valid)
        meta = json.loads(result.meta_path.read_text())
        self.assertTrue(any(
            "per-topic first-seen counts exceed attempts" in error
            for error in meta["metric_schema"]["errors"]
        ))

    def test_per_topic_attempts_must_account_for_policy_attempts(self):
        document = json.loads(response(attempts=4, first_seen=1))
        rollout = document["metrics"]["topic_auth_rollout"]
        rollout["unverified_token_bearing_policy_attempts_total"] = 3
        rollout["unverified_token_bearing_final_stage_rc_candidate_attempts_total"] = 3
        for topic in rollout["unverified_token_bearing_per_topic_attempts"]:
            rollout["unverified_token_bearing_per_topic_attempts"][topic] = 1
            rollout["unverified_token_bearing_per_topic_first_seen_connections"][topic] = 1
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        meta = json.loads(result.meta_path.read_text())
        self.assertIn(
            "per-topic attempts cannot account for policy attempts",
            meta["metric_schema"]["errors"],
        )

    def test_per_topic_first_seen_must_account_for_policy_first_seen(self):
        document = json.loads(response(attempts=4, first_seen=3))
        rollout = document["metrics"]["topic_auth_rollout"]
        for topic in rollout["unverified_token_bearing_per_topic_first_seen_connections"]:
            rollout["unverified_token_bearing_per_topic_first_seen_connections"][topic] = 1
        result = self.run_capture(
            FakeRunner(body=json.dumps(document, separators=(",", ":")).encode())
        )
        meta = json.loads(result.meta_path.read_text())
        self.assertIn(
            "per-topic first-seen counts cannot account for policy first-seen connections",
            meta["metric_schema"]["errors"],
        )

    def test_stopped_container_fails_before_writing(self):
        with self.assertRaises(capture.CaptureError):
            self.run_capture(FakeRunner(before=inspect(running=False)))
        self.assertFalse(any(self.output.iterdir()))

    def test_raw_survives_failure_in_post_fetch_provenance(self):
        raw = response()
        with self.assertRaisesRegex(capture.CaptureError, "post-fetch inspect failed"):
            self.run_capture(FakeRunner(
                body=raw,
                after_error=capture.CaptureError("post-fetch inspect failed"),
            ))
        raw_path = self.output / "20260814T092500Z.raw.json"
        self.assertEqual(raw_path.read_bytes(), raw)
        self.assertTrue(raw_path.with_name(raw_path.name + ".sha256").exists())
        self.assertFalse((self.output / "20260814T092500Z.meta.json").exists())


class TestArtifactSafety(CaptureFixture):
    def test_invalid_admin_json_is_preserved_as_unverifiable_evidence(self):
        result = self.run_capture(FakeRunner(body=b"not-json"))
        self.assertFalse(result.metric_schema_valid)
        self.assertTrue(result.window_changed)
        self.assertEqual(result.raw_path.read_bytes(), b"not-json")
        meta = json.loads(result.meta_path.read_text())
        self.assertEqual(meta["window"]["status"], "unverifiable")
        self.assertIsNone(meta["window"]["rollout_restarted"])
        self.assertIn("not valid JSON", meta["metric_schema"]["response_parse_error"])

    def test_missing_rollout_object_is_preserved_as_unverifiable_evidence(self):
        raw = b'{"metrics":{}}'
        result = self.run_capture(FakeRunner(body=raw))
        self.assertFalse(result.metric_schema_valid)
        self.assertEqual(result.raw_path.read_bytes(), raw)
        meta = json.loads(result.meta_path.read_text())
        self.assertIn("topic_auth_rollout", meta["metric_schema"]["response_parse_error"])

    def test_same_capture_id_never_overwrites_existing_artifacts(self):
        first = self.run_capture()
        original = first.raw_path.read_bytes()
        with self.assertRaisesRegex(capture.CaptureError, "already exist"):
            self.run_capture(FakeRunner(body=response(attempts=99)))
        self.assertEqual(first.raw_path.read_bytes(), original)
        self.assertEqual(len(list(self.output.iterdir())), 4)

    def test_symlink_output_directory_is_rejected(self):
        real = Path(self.temp.name) / "real"
        real.mkdir()
        link = Path(self.temp.name) / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(capture.CaptureError, "must not be a symlink"):
            self.run_capture(output_dir=link)

    def test_expected_image_requires_a_full_digest(self):
        with self.assertRaisesRegex(capture.CaptureError, "full 64-character"):
            self.run_capture(expected_image_id="a505d741a529")

    def test_publish_os_error_is_controlled_and_leaves_no_artifacts(self):
        with unittest.mock.patch("ops.capture_topic_auth_rollout.os.link",
                                 side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(capture.CaptureError, "could not publish"):
                self.run_capture()
        self.assertFalse(any(self.output.iterdir()))


class TestCommandFailure(unittest.TestCase):
    def test_missing_executable_is_a_controlled_capture_error(self):
        with unittest.mock.patch("subprocess.run", side_effect=FileNotFoundError("missing")):
            with self.assertRaisesRegex(capture.CaptureError, "could not execute"):
                capture._run(["missing-command"], 1)

    def test_timeout_is_a_controlled_capture_error(self):
        with unittest.mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 1)
        ):
            with self.assertRaisesRegex(capture.CaptureError, "timed out"):
                capture._run(["docker"], 1)


class TestCliExitStatus(CaptureFixture):
    def test_constants_keep_window_change_distinct_from_capture_failure(self):
        self.assertNotEqual(capture.EXIT_WINDOW_CHANGED, capture.EXIT_CAPTURE_FAILED)
        self.assertNotEqual(capture.EXIT_WINDOW_CHANGED, 0)

    def test_invalid_metric_schema_exits_as_capture_failure(self):
        result = capture.CaptureResult(
            capture_id="x",
            raw_path=Path("x.raw.json"),
            meta_path=Path("x.meta.json"),
            window_changed=False,
            metric_schema_valid=False,
            summary={"metric_schema_valid": False},
        )
        with unittest.mock.patch("ops.capture_topic_auth_rollout.capture", return_value=result), \
             redirect_stdout(StringIO()) as output:
            rc = capture.main([
                "--expected-rollout-started-at", STARTED,
                "--expected-image-id", IMAGE,
                "--expected-stage", "compatibility",
                "--expected-dispatcher-enabled", "false",
                "--checkpoint", "T+24h",
            ])
        self.assertEqual(rc, capture.EXIT_CAPTURE_FAILED)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_changed_window_exits_with_the_distinct_window_code(self):
        result = capture.CaptureResult(
            capture_id="x",
            raw_path=Path("x.raw.json"),
            meta_path=Path("x.meta.json"),
            window_changed=True,
            metric_schema_valid=True,
            summary={"window_status": "changed"},
        )
        with unittest.mock.patch("ops.capture_topic_auth_rollout.capture", return_value=result), \
             redirect_stdout(StringIO()) as output:
            rc = capture.main([
                "--expected-rollout-started-at", STARTED,
                "--expected-image-id", IMAGE,
                "--expected-stage", "compatibility",
                "--expected-dispatcher-enabled", "false",
                "--checkpoint", "T+24h",
            ])
        self.assertEqual(rc, capture.EXIT_WINDOW_CHANGED)
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
