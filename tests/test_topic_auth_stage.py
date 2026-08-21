"""Operational contract for the durable WebSocket topic auth-stage switch."""

import asyncio
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from scripts import topic_auth_stage, topic_flag
from scripts.canary_monitor import Target
from scripts.env_operation_lock import env_operation_lock


def env_bytes(*, stage=topic_auth_stage.COMPATIBILITY_STAGE, dispatcher="false",
              krx="false", fx="true", env="production", admin="test-secret"):
    rows = [
        f"ENV={env}",
        f"ADMIN_PASSWORD={admin}",
        f"KRX_CLIENT_DISTRIBUTION_ENABLED={krx}",
        f"FX_TOPIC_ENABLED={fx}",
    ]
    if stage is not None:
        rows.append(f"WS_TOPIC_AUTH_STAGE={stage}")
    rows.extend((f"TOPIC_DISPATCHER_ENABLED={dispatcher}", "OTHER=preserve-me"))
    return ("\n".join(rows) + "\n").encode()


def live_values(*, stage=topic_auth_stage.COMPATIBILITY_STAGE, dispatcher="false",
                krx="false", fx="true", env="production"):
    return {
        "ENV": env,
        "TOPIC_DISPATCHER_ENABLED": dispatcher,
        "KRX_CLIENT_DISTRIBUTION_ENABLED": krx,
        "FX_TOPIC_ENABLED": fx,
        "WS_TOPIC_AUTH_STAGE": stage,
    }


def topic_observation(data, *, stage=topic_auth_stage.COMPATIBILITY_STAGE,
                      runtime=False):
    raw_file_stage = topic_flag.read_key_value(data, topic_auth_stage.AUTH_STAGE_KEY)
    return {
        "other_tool_backup": False,
        "file_readable": True,
        "file_value": topic_flag.read_key_value(data),
        "file_line_count": len(topic_flag.key_line_indexes(data)),
        "file_auth_stage": (
            raw_file_stage.decode("utf-8") if raw_file_stage is not None else None
        ),
        "file_auth_stage_line_count": len(topic_flag.key_line_indexes(
            data, topic_auth_stage.AUTH_STAGE_KEY
        )),
        "container": runtime,
        "inspect": runtime,
        "dispatcher_endpoint": runtime,
        "auth_stage": stage,
        "fx": {topic: runtime for topic in topic_flag.FX_TOPICS},
        "fx_topic_enabled": True,
    }


class TestPlanning(unittest.TestCase):
    def test_final_rewrite_preserves_crlf_last_newline_and_other_bytes(self):
        original = (
            b"A=1\r\n"
            b"WS_TOPIC_AUTH_STAGE=compatibility\r\n"
            b"B=2"
        )
        self.assertEqual(
            topic_auth_stage.plan_stage(original, topic_auth_stage.FINAL_STAGE),
            (
                b"A=1\r\n"
                b"WS_TOPIC_AUTH_STAGE=enforce_authenticated_premium\r\n"
                b"B=2"
            ),
        )

    def test_stage_must_be_one_exact_valid_line(self):
        invalid = (
            b"WS_TOPIC_AUTH_STAGE=compatibility\nWS_TOPIC_AUTH_STAGE=compatibility\n",
            b"WS_TOPIC_AUTH_STAGE=compatibility \n",
            b"WS_TOPIC_AUTH_STAGE=unknown\n",
            b"WS_TOPIC_AUTH_STAGE=\n",
        )
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(topic_auth_stage.Abort):
                topic_auth_stage.plan_stage(data, topic_auth_stage.FINAL_STAGE)

    def test_absent_stage_is_the_app_compatibility_default(self):
        original = b"A=1\n"
        self.assertEqual(
            topic_auth_stage.file_stage(original),
            topic_auth_stage.COMPATIBILITY_STAGE,
        )
        self.assertIs(topic_auth_stage.plan_stage(
            original, topic_auth_stage.COMPATIBILITY_STAGE
        ), original)

    def test_absent_stage_append_preserves_newline_style_and_final_newline(self):
        key = b"WS_TOPIC_AUTH_STAGE=enforce_authenticated_premium"
        cases = (
            (b"A=1\nB=2\n", b"A=1\nB=2\n" + key + b"\n"),
            (b"A=1\r\nB=2\r\n", b"A=1\r\nB=2\r\n" + key + b"\r\n"),
            (b"A=1\nB=2", b"A=1\nB=2\n" + key),
            (b"A=1\r\nB=2", b"A=1\r\nB=2\r\n" + key),
            (b"", key + b"\n"),
        )
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(
                    topic_auth_stage.plan_stage(original, topic_auth_stage.FINAL_STAGE),
                    expected,
                )

    def test_app_config_defaults_an_absent_stage_to_compatibility(self):
        environment = os.environ.copy()
        environment.pop("WS_TOPIC_AUTH_STAGE", None)
        code = """
from unittest.mock import patch
with patch('dotenv.load_dotenv', return_value=False):
    from app.config import WS_TOPIC_AUTH_STAGE
print(WS_TOPIC_AUTH_STAGE.value)
"""
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.stdout.strip(), topic_auth_stage.COMPATIBILITY_STAGE)

    def test_all_app_stages_are_valid_sources_and_targets(self):
        for source in topic_auth_stage.VALID_STAGES:
            for target in topic_auth_stage.VALID_STAGES:
                with self.subTest(source=source, target=target):
                    planned = topic_auth_stage.plan_stage(env_bytes(stage=source), target)
                    self.assertEqual(topic_auth_stage.file_stage(planned), target)

    def test_stage_constants_are_the_app_enum_values(self):
        from app.config import TopicAuthStage

        self.assertEqual(
            set(topic_auth_stage.VALID_STAGES),
            {member.value for member in TopicAuthStage},
        )


class TestStatusClassification(unittest.TestCase):
    def classify(self, *, file_value=topic_auth_stage.FINAL_STAGE,
                 runtime_value=topic_auth_stage.FINAL_STAGE,
                 dispatcher_state=topic_flag.STATE_OFF,
                 dispatcher_active=False,
                 locked=False, pending=False, count=1, runtime_observed=True):
        return topic_auth_stage.classify_status(
            file_value=file_value,
            file_line_count=count,
            runtime_value=runtime_value,
            runtime_observed=runtime_observed,
            dispatcher_state=dispatcher_state,
            dispatcher_active=dispatcher_active,
            operation_locked=locked,
            activation_pending=pending,
        )

    def test_stable_stage_states(self):
        self.assertEqual(self.classify(), topic_auth_stage.STATE_FINAL)
        self.assertEqual(
            self.classify(
                file_value=topic_auth_stage.COMPATIBILITY_STAGE,
                runtime_value=topic_auth_stage.COMPATIBILITY_STAGE,
            ),
            topic_auth_stage.STATE_COMPATIBILITY,
        )
        self.assertEqual(
            self.classify(
                file_value=topic_auth_stage.INTERMEDIATE_STAGE,
                runtime_value=topic_auth_stage.INTERMEDIATE_STAGE,
            ),
            topic_auth_stage.STATE_INTERMEDIATE,
        )

    def test_file_runtime_drift_is_pending_recreate(self):
        self.assertEqual(
            self.classify(runtime_value=topic_auth_stage.COMPATIBILITY_STAGE),
            topic_auth_stage.STATE_PENDING_RECREATE,
        )

    def test_non_final_active_dispatcher_is_unsafe(self):
        self.assertEqual(
            self.classify(
                file_value=topic_auth_stage.COMPATIBILITY_STAGE,
                runtime_value=topic_auth_stage.COMPATIBILITY_STAGE,
                dispatcher_state=topic_flag.STATE_ON,
                dispatcher_active=True,
            ),
            topic_auth_stage.STATE_UNSAFE_ACTIVE,
        )
        self.assertEqual(
            self.classify(
                dispatcher_state=topic_flag.STATE_ON,
                dispatcher_active=True,
            ),
            topic_auth_stage.STATE_FINAL_ACTIVE,
        )

    def test_lock_pending_and_duplicate_are_never_stable(self):
        self.assertEqual(self.classify(locked=True), topic_auth_stage.STATE_AMBIGUOUS)
        self.assertEqual(self.classify(pending=True), topic_auth_stage.STATE_AMBIGUOUS)
        self.assertEqual(self.classify(count=2), topic_auth_stage.STATE_AMBIGUOUS)

    def test_absent_file_and_observed_absent_runtime_are_compatibility(self):
        self.assertEqual(
            self.classify(file_value=None, runtime_value=None, count=0),
            topic_auth_stage.STATE_COMPATIBILITY,
        )

    def test_runtime_observation_failure_is_unknown_not_compatibility(self):
        self.assertEqual(
            self.classify(
                file_value=None,
                runtime_value=None,
                count=0,
                runtime_observed=False,
            ),
            topic_auth_stage.STATE_UNKNOWN,
        )


class TestPreflight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.path.write_bytes(env_bytes())
        os.chmod(self.path, 0o600)
        self.target = Target(container="test-app", project="test", env_file=self.path)

    async def run_preflight(self, *, live=None, observation=None):
        data = self.path.read_bytes()
        stage = topic_auth_stage.file_stage(data)
        live = live or live_values(stage=stage)
        observation = observation or topic_observation(data, stage=live["WS_TOPIC_AUTH_STAGE"])
        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_auth_stage, "ambient_problems", return_value=[]), \
             patch.object(topic_auth_stage, "run_command", AsyncMock(return_value="")), \
             patch.object(topic_auth_stage, "check_env_file_mode"), \
             patch.object(topic_auth_stage, "check_legacy_backup_modes"), \
             patch.object(topic_auth_stage, "wait_until_healthy", AsyncMock()), \
             patch.object(topic_auth_stage, "_runtime_values", AsyncMock(return_value=live)), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation)):
            return await topic_auth_stage.preflight(self.target, self.path)

    async def test_returns_exact_snapshot_and_running_stage(self):
        data, stage = await self.run_preflight()
        self.assertEqual(data, self.path.read_bytes())
        self.assertEqual(stage, topic_auth_stage.COMPATIBILITY_STAGE)

    async def test_absent_file_and_runtime_stage_use_the_app_default(self):
        self.path.write_bytes(env_bytes(stage=None))
        data, stage = await self.run_preflight(
            live=live_values(stage=None),
            observation=topic_observation(
                self.path.read_bytes(), stage=None
            ),
        )
        self.assertEqual(data, self.path.read_bytes())
        self.assertEqual(stage, topic_auth_stage.COMPATIBILITY_STAGE)

    async def test_file_or_runtime_dispatcher_on_is_rejected(self):
        self.path.write_bytes(env_bytes(dispatcher="true"))
        with self.assertRaises(topic_auth_stage.Abort):
            await self.run_preflight()

        self.path.write_bytes(env_bytes())
        with self.assertRaises(topic_auth_stage.Abort):
            await self.run_preflight(live=live_values(dispatcher="true"))

    async def test_dispatcher_cross_check_must_also_be_off(self):
        data = self.path.read_bytes()
        with self.assertRaises(topic_auth_stage.Abort):
            await self.run_preflight(
                observation=topic_observation(data, runtime=True)
            )

    async def test_krx_off_fx_on_and_production_are_required_on_both_sides(self):
        file_cases = (
            env_bytes(krx="true"),
            env_bytes(fx="false"),
            env_bytes(env="development"),
        )
        for data in file_cases:
            self.path.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(topic_auth_stage.Abort):
                await self.run_preflight()

        self.path.write_bytes(env_bytes())
        runtime_cases = (
            live_values(krx="true"),
            live_values(fx="false"),
            live_values(env="development"),
        )
        for live in runtime_cases:
            with self.subTest(live=live), self.assertRaises(topic_auth_stage.Abort):
                await self.run_preflight(live=live)

    async def test_runtime_booleans_are_not_leniently_coerced_to_false(self):
        with self.assertRaises(topic_auth_stage.Abort):
            await self.run_preflight(live=live_values(dispatcher="garbage"))

    async def test_interrupted_topic_activation_blocks_stage_mutation(self):
        marker = topic_flag.pending_marker_path(self.path)
        marker.touch(mode=0o600)
        with self.assertRaises(topic_auth_stage.Abort):
            await self.run_preflight()


class TestPostRecreateVerification(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.final = env_bytes(stage=topic_auth_stage.FINAL_STAGE)
        self.path.write_bytes(self.final)
        self.target = Target(container="test-app", project="test", env_file=self.path)

    async def verify(self, *, stage=topic_auth_stage.FINAL_STAGE,
                     observation=None, live=None):
        observation = observation or topic_observation(self.final, stage=stage)
        live = live or live_values(stage=stage)
        with patch.object(topic_flag, "measure", AsyncMock(return_value=observation)), \
             patch.object(topic_auth_stage, "_runtime_values", AsyncMock(return_value=live)):
            return await topic_auth_stage._verify(
                self.target, self.path, self.final, topic_auth_stage.FINAL_STAGE
            )

    async def test_exact_file_runtime_stage_and_dispatcher_off_pass(self):
        result = await self.verify()
        self.assertTrue(result["env_matches_plan"])
        self.assertEqual(result["runtime_stage"], topic_auth_stage.FINAL_STAGE)
        self.assertEqual(result["dispatcher_state"], topic_flag.STATE_OFF)

    async def test_runtime_stage_regression_fails(self):
        with self.assertRaises(topic_auth_stage.Abort):
            await self.verify(stage=topic_auth_stage.COMPATIBILITY_STAGE)

    async def test_dispatcher_activation_fails_even_with_final_stage(self):
        with self.assertRaises(topic_auth_stage.Abort):
            await self.verify(observation=topic_observation(
                self.final,
                stage=topic_auth_stage.FINAL_STAGE,
                runtime=True,
            ))

    async def test_env_edit_during_runtime_observation_fails(self):
        async def measure_then_edit(*args, **kwargs):
            self.path.write_bytes(self.final + b"EXTERNAL=1\n")
            return topic_observation(self.final, stage=topic_auth_stage.FINAL_STAGE)

        with patch.object(topic_flag, "measure", side_effect=measure_then_edit), \
             patch.object(
                 topic_auth_stage, "_runtime_values",
                 AsyncMock(return_value=live_values(stage=topic_auth_stage.FINAL_STAGE)),
             ), self.assertRaises(topic_auth_stage.Abort):
            await topic_auth_stage._verify(
                self.target, self.path, self.final, topic_auth_stage.FINAL_STAGE
            )

    async def test_absent_file_and_runtime_stage_verify_as_compatibility(self):
        implicit = env_bytes(stage=None)
        self.path.write_bytes(implicit)
        with patch.object(
                 topic_flag, "measure",
                 AsyncMock(return_value=topic_observation(implicit, stage=None)),
             ), patch.object(
                 topic_auth_stage, "_runtime_values",
                 AsyncMock(return_value=live_values(stage=None)),
             ):
            result = await topic_auth_stage._verify(
                self.target,
                self.path,
                implicit,
                topic_auth_stage.COMPATIBILITY_STAGE,
            )

        self.assertEqual(result["file_stage"], topic_auth_stage.COMPATIBILITY_STAGE)
        self.assertEqual(result["runtime_stage"], topic_auth_stage.COMPATIBILITY_STAGE)
        self.assertFalse(result["file_stage_explicit"])
        self.assertFalse(result["runtime_stage_explicit"])


class TestTransactions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.path.write_bytes(env_bytes())
        os.chmod(self.path, 0o600)
        self.target = Target(container="test-app", project="test", env_file=self.path)

    async def test_final_transition_writes_recreates_and_verifies(self):
        original = self.path.read_bytes()
        verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.FINAL_STAGE,
            "runtime_stage": topic_auth_stage.FINAL_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }
        verify = AsyncMock(return_value=verified)
        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(original, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", AsyncMock()
             ) as recreate, patch.object(topic_auth_stage, "_verify", verify):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["wrote"])
        self.assertEqual(topic_auth_stage.file_stage(self.path.read_bytes()), topic_auth_stage.FINAL_STAGE)
        recreate.assert_awaited_once()
        verify.assert_awaited_once()

    async def test_final_transition_appends_an_absent_stage(self):
        original = env_bytes(stage=None)
        self.path.write_bytes(original)
        verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.FINAL_STAGE,
            "runtime_stage": topic_auth_stage.FINAL_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }
        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(original, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", AsyncMock()
             ) as recreate, patch.object(
                 topic_auth_stage, "_verify", AsyncMock(return_value=verified)
             ):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["wrote"])
        self.assertEqual(
            topic_flag.key_line_indexes(
                self.path.read_bytes(), topic_auth_stage.AUTH_STAGE_KEY
            ),
            [6],
        )
        self.assertEqual(
            topic_auth_stage.file_stage(self.path.read_bytes()),
            topic_auth_stage.FINAL_STAGE,
        )
        recreate.assert_awaited_once()

    async def test_stable_target_is_noop_without_recreate(self):
        final = env_bytes(stage=topic_auth_stage.FINAL_STAGE)
        self.path.write_bytes(final)
        recreate = AsyncMock()
        verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.FINAL_STAGE,
            "runtime_stage": topic_auth_stage.FINAL_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }
        verify = AsyncMock(return_value=verified)
        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(final, topic_auth_stage.FINAL_STAGE)),
             ), patch.object(topic_auth_stage, "recreate_and_verify_health", recreate), \
             patch.object(topic_auth_stage, "_verify", verify):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["wrote"])
        recreate.assert_not_awaited()
        verify.assert_awaited_once()

    async def test_implicit_compatibility_target_is_noop_without_recreate(self):
        implicit = env_bytes(stage=None)
        self.path.write_bytes(implicit)
        verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "runtime_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }
        recreate = AsyncMock()
        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(implicit, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", recreate
             ), patch.object(
                 topic_auth_stage, "_verify", AsyncMock(return_value=verified)
             ):
            result = await topic_auth_stage.transition(
                topic_auth_stage.COMPATIBILITY_STAGE, self.target, env_path=self.path
            )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["wrote"])
        self.assertEqual(self.path.read_bytes(), implicit)
        recreate.assert_not_awaited()

    async def test_failure_restores_the_stage_that_was_running(self):
        original = self.path.read_bytes()
        rollback_verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "runtime_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }
        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(original, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", AsyncMock()
             ) as recreate, patch.object(
                 topic_auth_stage, "_verify",
                 AsyncMock(side_effect=[topic_auth_stage.Abort("injected"), rollback_verified]),
             ):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rollback"]["ok"], result)
        self.assertEqual(
            topic_auth_stage.file_stage(self.path.read_bytes()),
            topic_auth_stage.COMPATIBILITY_STAGE,
        )
        self.assertEqual(recreate.await_count, 2)

    async def test_failed_final_transition_removes_the_inserted_stage_key(self):
        original = env_bytes(stage=None)
        self.path.write_bytes(original)
        rollback_verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "runtime_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }
        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(original, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", AsyncMock()
             ) as recreate, patch.object(
                 topic_auth_stage, "_verify",
                 AsyncMock(side_effect=[topic_auth_stage.Abort("injected"), rollback_verified]),
             ):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rollback"]["ok"], result)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(
            topic_flag.key_line_indexes(
                self.path.read_bytes(), topic_auth_stage.AUTH_STAGE_KEY
            ),
            [],
        )
        self.assertEqual(recreate.await_count, 2)

    async def test_interrupted_file_final_runtime_compat_rolls_back_to_runtime_stage(self):
        original = env_bytes(stage=topic_auth_stage.FINAL_STAGE)
        self.path.write_bytes(original)
        rollback_verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "runtime_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }
        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(original, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", AsyncMock()
             ) as recreate, patch.object(
                 topic_auth_stage, "_verify",
                 AsyncMock(side_effect=[topic_auth_stage.Abort("injected"), rollback_verified]),
             ):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rollback"]["ok"], result)
        self.assertEqual(
            topic_auth_stage.file_stage(self.path.read_bytes()),
            topic_auth_stage.COMPATIBILITY_STAGE,
        )
        self.assertEqual(recreate.await_count, 2)

    async def test_post_replace_failure_still_restores_previous_stage(self):
        original = self.path.read_bytes()
        real_atomic = topic_auth_stage.atomic_write_bytes
        calls = {"count": 0}

        def replace_then_fail(path, data, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                Path(path).write_bytes(data)
                raise OSError("injected directory fsync failure")
            real_atomic(path, data, **kwargs)

        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(original, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "atomic_write_bytes", side_effect=replace_then_fail
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", AsyncMock()
             ), patch.object(
                 topic_auth_stage, "_verify", AsyncMock(return_value={
                     "env_matches_plan": True,
                     "file_stage": topic_auth_stage.COMPATIBILITY_STAGE,
                     "runtime_stage": topic_auth_stage.COMPATIBILITY_STAGE,
                     "dispatcher_state": topic_flag.STATE_OFF,
                 })
             ):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rollback"]["ok"], result)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(calls["count"], 2)

    async def test_external_edit_to_target_stage_before_cas_is_not_rolled_back(self):
        original = self.path.read_bytes()
        changed = topic_auth_stage.plan_stage(original, topic_auth_stage.FINAL_STAGE)

        async def preflight_then_edit(*args, **kwargs):
            self.path.write_bytes(changed)
            return original, topic_auth_stage.COMPATIBILITY_STAGE

        with patch.object(topic_auth_stage, "preflight", side_effect=preflight_then_edit), \
             patch.object(topic_auth_stage, "recreate_and_verify_health", AsyncMock()) as recreate:
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertFalse(result["ok"])
        self.assertNotIn("rollback", result)
        self.assertEqual(self.path.read_bytes(), changed)
        recreate.assert_not_awaited()

    async def test_cancelled_transition_attempts_rollback(self):
        original = self.path.read_bytes()
        rollback_verified = {
            "env_matches_plan": True,
            "file_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "runtime_stage": topic_auth_stage.COMPATIBILITY_STAGE,
            "dispatcher_state": topic_flag.STATE_OFF,
        }

        async def cancel_after_start(target, env_path, original, planned, stage, summary):
            summary["mutation_started"] = True
            Path(env_path).write_bytes(planned)
            raise asyncio.CancelledError()

        with patch.object(
                 topic_auth_stage, "preflight",
                 AsyncMock(return_value=(original, topic_auth_stage.COMPATIBILITY_STAGE)),
             ), patch.object(
                 topic_auth_stage, "_apply", side_effect=cancel_after_start
             ), patch.object(
                 topic_auth_stage, "recreate_and_verify_health", AsyncMock()
             ) as recreate, patch.object(
                 topic_auth_stage, "_verify", AsyncMock(return_value=rollback_verified)
             ):
            result = await topic_auth_stage.transition(
                topic_auth_stage.FINAL_STAGE, self.target, env_path=self.path
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["rollback"]["ok"], result)
        self.assertEqual(self.path.read_bytes(), original)
        recreate.assert_awaited_once()


class TestStatusReport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.implicit = env_bytes(stage=None)
        self.path.write_bytes(self.implicit)
        self.target = Target(container="test-app", project="test", env_file=self.path)

    async def report(self, runtime_values):
        observation = topic_observation(self.implicit, stage=None)
        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation)), \
             patch.object(topic_auth_stage, "_runtime_values", runtime_values), \
             patch.object(topic_auth_stage, "env_operation_is_locked", return_value=False), \
             patch.object(
                 topic_auth_stage, "topic_flag_pending_entry_exists", return_value=False
             ):
            return await topic_auth_stage.report_status(
                self.target, env_path=self.path
            )

    async def test_absent_file_and_observed_runtime_key_report_compatibility(self):
        result = await self.report(AsyncMock(return_value=live_values(stage=None)))

        self.assertEqual(result["state"], topic_auth_stage.STATE_COMPATIBILITY)
        self.assertIsNone(result["file_stage"])
        self.assertIsNone(result["runtime_stage"])
        self.assertEqual(
            result["effective_file_stage"], topic_auth_stage.COMPATIBILITY_STAGE
        )
        self.assertEqual(
            result["effective_runtime_stage"], topic_auth_stage.COMPATIBILITY_STAGE
        )
        self.assertFalse(result["file_stage_explicit"])
        self.assertFalse(result["runtime_stage_explicit"])

    async def test_runtime_collection_failure_reports_unknown(self):
        result = await self.report(
            AsyncMock(side_effect=topic_auth_stage.Abort("injected observation failure"))
        )

        self.assertEqual(result["state"], topic_auth_stage.STATE_UNKNOWN)
        self.assertIsNone(result["effective_runtime_stage"])
        self.assertIsNone(result["runtime_stage_explicit"])


class TestCliLock(unittest.TestCase):
    def test_mutation_refuses_while_another_env_tool_holds_the_lock(self):
        with TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_bytes(env_bytes())
            target = Target(container="test-app", env_file=env)
            called = AsyncMock()

            with patch.object(topic_auth_stage, "PRODUCTION_TARGET", target), \
                 patch.object(topic_auth_stage, "transition", called), \
                 env_operation_lock(env, "topic_flag:off"), \
                 redirect_stdout(StringIO()):
                code = asyncio.run(topic_auth_stage.amain(["final"]))

            self.assertEqual(code, 1)
            called.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
