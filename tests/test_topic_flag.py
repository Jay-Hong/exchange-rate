import asyncio
import inspect
import os
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from scripts import topic_flag
from scripts.canary_monitor import Target


def env_bytes(*, topic="false", krx="false", fx="true", env="production",
              auth_stage=topic_flag.REQUIRED_AUTH_STAGE, admin="test-secret"):
    return (
        f"ENV={env}\n"
        f"ADMIN_PASSWORD={admin}\n"
        f"KRX_CLIENT_DISTRIBUTION_ENABLED={krx}\n"
        f"FX_TOPIC_ENABLED={fx}\n"
        f"WS_TOPIC_AUTH_STAGE={auth_stage}\n"
        f"TOPIC_DISPATCHER_ENABLED={topic}\n"
        "OTHER=preserve-me\n"
    ).encode()


def observation(data, runtime, *, inspect=None, endpoint=None, fx_ready=True,
                file_auth_stage=topic_flag.REQUIRED_AUTH_STAGE,
                auth_stage=topic_flag.REQUIRED_AUTH_STAGE, backup=False):
    inspect = runtime if inspect is None else inspect
    endpoint = runtime if endpoint is None else endpoint
    return {
        "other_tool_backup": backup,
        "file_readable": True,
        "file_value": topic_flag.read_key_value(data),
        "file_line_count": len(topic_flag.key_line_indexes(data)),
        "file_auth_stage": file_auth_stage,
        "file_auth_stage_line_count": 1,
        "container": runtime,
        "inspect": inspect,
        "dispatcher_endpoint": endpoint,
        "auth_stage": auth_stage,
        "fx": ({name: bool(runtime and fx_ready) for name in topic_flag.FX_TOPICS}
               if fx_ready is not None else None),
        "fx_topic_enabled": fx_ready,
    }


class TestPlanning(unittest.TestCase):
    def test_enable_preserves_crlf_and_non_target_bytes(self):
        original = b"A=1\r\nTOPIC_DISPATCHER_ENABLED=false\r\nB=2"
        self.assertEqual(
            topic_flag.plan_enable(original),
            b"A=1\r\nTOPIC_DISPATCHER_ENABLED=true\r\nB=2",
        )

    def test_enable_requires_one_unambiguous_boolean(self):
        bad = (
            b"A=1\n",
            b"TOPIC_DISPATCHER_ENABLED=false\nTOPIC_DISPATCHER_ENABLED=false\n",
            b"TOPIC_DISPATCHER_ENABLED=garbage\n",
            b"TOPIC_DISPATCHER_ENABLED= true\n",
            b"TOPIC_DISPATCHER_ENABLED=\n",
        )
        for data in bad:
            with self.subTest(data=data), self.assertRaises(topic_flag.Abort):
                topic_flag.plan_enable(data)

    def test_disable_normalizes_every_duplicate_without_touching_other_bytes(self):
        original = (
            b"TOPIC_DISPATCHER_ENABLED=true\n"
            b"OTHER=x\r\n"
            b"TOPIC_DISPATCHER_ENABLED=garbage"
        )
        self.assertEqual(
            topic_flag.plan_disable(original),
            b"TOPIC_DISPATCHER_ENABLED=false\nOTHER=x\r\nTOPIC_DISPATCHER_ENABLED=false",
        )

    def test_app_boolean_predicate_does_not_strip(self):
        self.assertTrue(topic_flag.is_true("TRUE"))
        self.assertFalse(topic_flag.is_true(" true"))
        self.assertFalse(topic_flag.is_true("true "))


class TestClassification(unittest.TestCase):
    def test_stable_states(self):
        self.assertEqual(topic_flag.classify(observation(env_bytes(), False)), topic_flag.STATE_OFF)
        self.assertEqual(
            topic_flag.classify(observation(env_bytes(topic="true"), True)),
            topic_flag.STATE_ON,
        )

    def test_fx_failure_cannot_be_called_on(self):
        state = topic_flag.classify(observation(env_bytes(topic="true"), True, fx_ready=False))
        self.assertEqual(state, topic_flag.STATE_DRIFTED)

    def test_non_final_auth_stage_cannot_be_called_on(self):
        state = topic_flag.classify(observation(
            env_bytes(topic="true", auth_stage="compatibility"),
            True,
            auth_stage="compatibility",
        ))
        self.assertEqual(state, topic_flag.STATE_DRIFTED)

    def test_pending_file_auth_downgrade_cannot_be_called_on(self):
        state = topic_flag.classify(observation(
            env_bytes(topic="true", auth_stage="compatibility"),
            True,
            file_auth_stage="compatibility",
        ))
        self.assertEqual(state, topic_flag.STATE_DRIFTED)

    def test_auth_stage_does_not_block_emergency_off_status(self):
        state = topic_flag.classify(observation(
            env_bytes(auth_stage="compatibility"),
            False,
            auth_stage="compatibility",
        ))
        self.assertEqual(state, topic_flag.STATE_OFF)

    def test_fx_observation_failure_is_unknown(self):
        state = topic_flag.classify(observation(env_bytes(topic="true"), True, fx_ready=None))
        self.assertEqual(state, topic_flag.STATE_UNKNOWN)

    def test_garbage_file_value_is_ambiguous_not_stable_off(self):
        state = topic_flag.classify(observation(env_bytes(topic="garbage"), False))
        self.assertEqual(state, topic_flag.STATE_AMBIGUOUS)

    def test_runtime_disagreement_is_drifted(self):
        state = topic_flag.classify(observation(env_bytes(), False, inspect=True))
        self.assertEqual(state, topic_flag.STATE_DRIFTED)

    def test_global_off_with_fx_still_enabled_is_drifted(self):
        current = observation(env_bytes(), False)
        current["fx"] = {name: True for name in topic_flag.FX_TOPICS}
        self.assertEqual(topic_flag.classify(current), topic_flag.STATE_DRIFTED)


class TestPathGuard(unittest.TestCase):
    def test_regular_file_is_accepted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(env_bytes())
            self.assertEqual(topic_flag.resolve_env_path(path), path.resolve())

    def test_symlink_is_rejected_before_resolve(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "secret"
            target.write_bytes(env_bytes())
            link = root / ".env"
            link.symlink_to(target)
            with self.assertRaises(topic_flag.Abort):
                topic_flag.resolve_env_path(link)


class TestActivationMarker(unittest.TestCase):
    def test_marker_is_empty_owner_only_and_durable_until_explicit_clear(self):
        with TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_bytes(env_bytes())

            marker = topic_flag.ensure_activation_marker(env)
            self.assertEqual(marker.read_bytes(), b"")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertTrue(topic_flag.activation_marker_exists(env))
            self.assertTrue(topic_flag.clear_activation_marker(env))
            self.assertFalse(marker.exists())

    def test_symlink_marker_is_never_followed_or_deleted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_bytes(env_bytes())
            foreign = root / "foreign"
            foreign.write_text("do-not-touch")
            topic_flag.pending_marker_path(env).symlink_to(foreign)

            # ⛔ **어느 가드가 발동했는지까지 본다.** `Abort` 여부만 보면 판별되지 않는다 —
            #    symlink 의 `lstat` 모드는 `0o777` 이라 symlink 검사를 지워도 **권한 가드가
            #    대신 잡아** 테스트가 통과한다(변이 실측: SURVIVED). 두 가드가 서로를 가린다.
            for call in (lambda: topic_flag.activation_marker_exists(env),
                         lambda: topic_flag.clear_activation_marker(env)):
                with self.assertRaises(topic_flag.Abort) as caught:
                    call()
                self.assertIn("일반 파일", str(caught.exception),
                              "symlink 가 **파일 종류** 가드에 걸리지 않았다")
            self.assertEqual(foreign.read_text(), "do-not-touch")

    def test_group_or_world_accessible_marker_is_rejected(self):
        """⛔ 권한 가드는 **정규 파일**로 따로 잠근다 — symlink 경로에 가려 미검증이었다."""
        with TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_bytes(env_bytes())
            marker = topic_flag.pending_marker_path(env)
            marker.write_bytes(b"")
            marker.chmod(0o644)                       # 정규 파일 + 느슨한 권한

            with self.assertRaises(topic_flag.Abort) as caught:
                topic_flag.activation_marker_exists(env)
            self.assertIn("소유자/권한", str(caught.exception))
            self.assertTrue(marker.exists(), "거부하면서 남의 파일을 지우면 안 된다")

    def test_owner_only_regular_marker_is_accepted(self):
        """⚠️ 반대 방향 대조군 — 가드가 정상 marker 까지 막으면 복구가 불가능해진다."""
        with TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_bytes(env_bytes())
            marker = topic_flag.pending_marker_path(env)
            marker.write_bytes(b"")
            marker.chmod(0o600)
            self.assertTrue(topic_flag.activation_marker_exists(env))

    def test_marker_owned_by_another_uid_is_rejected(self):
        """권한과 UID 검사를 분리한다 — mode 0600만으로 소유권을 증명할 수 없다."""
        with TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_bytes(env_bytes())
            marker = topic_flag.pending_marker_path(env)
            marker.write_bytes(b"")
            marker.chmod(0o600)

            with patch.object(topic_flag.os, "geteuid", return_value=os.geteuid() + 1), \
                 self.assertRaises(topic_flag.Abort) as caught:
                topic_flag.activation_marker_exists(env)

            self.assertIn("소유자/권한", str(caught.exception))


class TestPreflight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.path.write_bytes(env_bytes())
        os.chmod(self.path, 0o600)
        self.target = Target(container="test-app", project="test", env_file=self.path)

    def patches(self, *, live=None, health=None):
        live = live or {
            "ENV": "production",
            "KRX_CLIENT_DISTRIBUTION_ENABLED": "false",
            "FX_TOPIC_ENABLED": "true",
            "WS_TOPIC_AUTH_STAGE": topic_flag.REQUIRED_AUTH_STAGE,
        }
        return (
            patch.object(topic_flag, "preflight_common", AsyncMock()),
            patch.object(topic_flag, "ambient_problems", return_value=[]),
            patch.object(topic_flag, "run_command", AsyncMock(return_value="")),
            patch.object(topic_flag, "check_env_file_mode"),
            patch.object(topic_flag, "check_legacy_backup_modes"),
            patch.object(topic_flag, "observe", AsyncMock(return_value=live)),
            patch.object(topic_flag, "wait_until_healthy", AsyncMock(side_effect=health)),
        )

    async def run_preflight(self, *, live=None, health=None):
        contexts = self.patches(live=live, health=health)
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)
        return await topic_flag.preflight_on(self.target, self.path)

    async def test_returns_the_exact_snapshot_that_passed_validation(self):
        original = self.path.read_bytes()

        async def mutate_after_validation(*args, **kwargs):
            self.path.write_bytes(original.replace(b"OTHER=preserve-me", b"OTHER=changed"))

        snapshot = await self.run_preflight(health=mutate_after_validation)
        self.assertEqual(snapshot, original)
        self.assertNotEqual(self.path.read_bytes(), original)

    async def test_pending_file_krx_enable_is_rejected_even_if_runtime_is_off(self):
        self.path.write_bytes(env_bytes(krx="true"))
        with self.assertRaises(topic_flag.Abort):
            await self.run_preflight()

    async def test_file_and_runtime_fx_must_both_be_enabled(self):
        self.path.write_bytes(env_bytes(fx="false"))
        with self.assertRaises(topic_flag.Abort):
            await self.run_preflight()

        self.path.write_bytes(env_bytes())
        with self.assertRaises(topic_flag.Abort):
            await self.run_preflight(live={
                "ENV": "production",
                "KRX_CLIENT_DISTRIBUTION_ENABLED": "false",
                "FX_TOPIC_ENABLED": "false",
                "WS_TOPIC_AUTH_STAGE": topic_flag.REQUIRED_AUTH_STAGE,
            })

    async def test_file_and_runtime_auth_stage_must_both_be_final(self):
        self.path.write_bytes(env_bytes(auth_stage="compatibility"))
        with self.assertRaises(topic_flag.Abort) as caught:
            await self.run_preflight()
        self.assertIn("WS_TOPIC_AUTH_STAGE", str(caught.exception))

        self.path.write_bytes(env_bytes())
        with self.assertRaises(topic_flag.Abort) as caught:
            await self.run_preflight(live={
                "ENV": "production",
                "KRX_CLIENT_DISTRIBUTION_ENABLED": "false",
                "FX_TOPIC_ENABLED": "true",
                "WS_TOPIC_AUTH_STAGE": "compatibility",
            })
        self.assertIn("WS_TOPIC_AUTH_STAGE", str(caught.exception))

    async def test_empty_admin_password_is_rejected_before_recreate(self):
        self.path.write_bytes(env_bytes(admin=""))
        with self.assertRaises(topic_flag.Abort):
            await self.run_preflight()


    async def test_padded_file_auth_stage_is_rejected_before_touching_production(self):
        """⛔ 후행 공백은 관대한 파서(`read_env_values`)로 보면 통과한다 — 그런데
        `app.config.parse_topic_auth_stage` 는 정확 일치만 받고 ValueError 를 던지므로
        재생성하면 **컨테이너가 import 에서 죽는다**. 사전검사에서 막아야 한다.
        """
        self.path.write_bytes(env_bytes(auth_stage=topic_flag.REQUIRED_AUTH_STAGE + " "))
        with self.assertRaises(topic_flag.Abort):
            await self.run_preflight()


class TestTransactions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"
        self.path.write_bytes(env_bytes())
        os.chmod(self.path, 0o600)
        self.target = Target(container="test-app", project="test", env_file=self.path)

    async def test_measure_does_not_coerce_missing_endpoint_fields_to_off(self):
        data = self.path.read_bytes()
        with patch.object(topic_flag, "observe", AsyncMock(return_value={
                 "TOPIC_DISPATCHER_ENABLED": "false",
                 "WS_TOPIC_AUTH_STAGE": topic_flag.REQUIRED_AUTH_STAGE,
             })), patch.object(topic_flag, "inspect_value", AsyncMock(return_value="")), \
             patch.object(topic_flag, "admin_json", AsyncMock(side_effect=[
                 {"enabled": False}, {},
             ])):
            result = await topic_flag.measure(self.target, self.path, data)

        self.assertIs(result["dispatcher_endpoint"], False)
        self.assertTrue(all(value is None for value in result["fx"].values()))
        self.assertIsNone(result["fx_topic_enabled"])
        self.assertEqual(topic_flag.classify(result), topic_flag.STATE_UNKNOWN)

    async def test_apply_rejects_non_final_runtime_auth_stage_after_recreate(self):
        original = self.path.read_bytes()
        planned = topic_flag.plan_enable(original)
        incompatible = observation(planned, True, auth_stage="compatibility")

        with patch.object(topic_flag, "recreate_and_verify_health", AsyncMock()), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=incompatible)):
            with self.assertRaises(topic_flag.Abort) as caught:
                await topic_flag._apply(
                    self.target, self.path, original, planned, True,
                    {"wrote": False, "recreated": False},
                )

        self.assertIn("auth_stage='compatibility'", str(caught.exception))

    async def test_apply_cas_runs_even_when_target_row_needs_no_write(self):
        original = env_bytes(topic="true")
        self.path.write_bytes(original + b"CONCURRENT=1\n")
        recreate = AsyncMock()
        with patch.object(topic_flag, "recreate_and_verify_health", recreate):
            with self.assertRaises(topic_flag.Abort):
                await topic_flag._apply(
                    self.target, self.path, original, original, True,
                    {"wrote": False, "recreated": False},
                )
        recreate.assert_not_awaited()

    async def test_apply_rejects_env_drift_during_recreate(self):
        original = self.path.read_bytes()
        planned = topic_flag.plan_enable(original)

        async def recreate_then_edit(target):
            self.path.write_bytes(planned + b"CONCURRENT=1\n")

        with patch.object(
                 topic_flag, "recreate_and_verify_health", side_effect=recreate_then_edit,
             ), patch.object(
                 topic_flag, "measure", AsyncMock(return_value=observation(planned, True)),
             ):
            with self.assertRaises(topic_flag.Abort) as caught:
                await topic_flag._apply(
                    self.target, self.path, original, planned, True,
                    {"wrote": False, "recreated": False},
                )

        self.assertIn("env_matches_plan=False", str(caught.exception))

    async def test_apply_rechecks_env_after_runtime_observation(self):
        original = self.path.read_bytes()
        planned = topic_flag.plan_enable(original)

        async def measure_then_edit(target, env_path, snapshot=None):
            self.assertEqual(snapshot, planned)
            self.path.write_bytes(planned + b"CONCURRENT=1\n")
            return observation(snapshot, True)

        with patch.object(topic_flag, "recreate_and_verify_health", AsyncMock()), \
             patch.object(topic_flag, "measure", side_effect=measure_then_edit):
            with self.assertRaises(topic_flag.Abort) as caught:
                await topic_flag._apply(
                    self.target, self.path, original, planned, True,
                    {"wrote": False, "recreated": False},
                )

        self.assertIn("env_matches_plan=False", str(caught.exception))

    async def test_stable_on_is_idempotent_and_does_not_recreate(self):
        data = env_bytes(topic="true")
        self.path.write_bytes(data)
        apply = AsyncMock()
        with patch.object(topic_flag, "preflight_on", AsyncMock(return_value=data)), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation(data, True))), \
             patch.object(topic_flag, "_apply", apply):
            result = await topic_flag.turn_on(self.target, env_path=self.path)
        self.assertTrue(result["ok"], result)
        apply.assert_not_awaited()

    async def test_interrupted_marker_forces_on_to_reverify_before_commit(self):
        data = env_bytes(topic="true")
        self.path.write_bytes(data)
        topic_flag.ensure_activation_marker(self.path)

        async def apply(target, env_path, planned_from, planned, expect_true, summary):
            self.assertTrue(topic_flag.activation_marker_exists(env_path))
            summary["state"] = topic_flag.STATE_ON

        with patch.object(topic_flag, "preflight_on", AsyncMock(return_value=data)), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation(data, True))), \
             patch.object(topic_flag, "_apply", side_effect=apply) as mocked_apply:
            result = await topic_flag.turn_on(self.target, env_path=self.path)

        self.assertTrue(result["ok"], result)
        mocked_apply.assert_awaited_once()
        self.assertFalse(topic_flag.activation_marker_exists(self.path))

    async def test_failed_on_preserves_marker_when_off_convergence_fails(self):
        data = self.path.read_bytes()
        with patch.object(topic_flag, "preflight_on", AsyncMock(return_value=data)), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation(data, False))), \
             patch.object(topic_flag, "_apply", AsyncMock(side_effect=RuntimeError("injected"))), \
             patch.object(topic_flag, "_converge_off", AsyncMock(return_value={
                 "attempted": True, "ok": False, "error": "docker unavailable",
             })):
            result = await topic_flag.turn_on(self.target, env_path=self.path)

        self.assertFalse(result["ok"])
        self.assertTrue(topic_flag.activation_marker_exists(self.path))

    async def test_on_noop_rejects_env_drift_after_preflight(self):
        data = env_bytes(topic="true")
        self.path.write_bytes(data.replace(b"OTHER=preserve-me", b"OTHER=changed"))
        with patch.object(topic_flag, "preflight_on", AsyncMock(return_value=data)), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation(data, True))):
            result = await topic_flag.turn_on(self.target, env_path=self.path)
        self.assertFalse(result["ok"])
        self.assertIn("변경", result["error"])
        self.assertNotIn("converge", result, "도구가 쓰기 전인 외부 변경을 되돌리면 안 된다")

    async def test_post_replace_failure_still_converges_file_and_runtime_off(self):
        original = env_bytes()
        self.path.write_bytes(original)
        runtime = {"value": False}
        real_atomic_write = topic_flag.atomic_write_bytes
        calls = {"count": 0}

        def replace_then_fail_once(path, data, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                Path(path).write_bytes(data)
                raise OSError("injected directory fsync failure")
            real_atomic_write(path, data, **kwargs)

        async def fake_measure(target, env_path, data=None):
            snapshot = Path(env_path).read_bytes() if data is None else data
            return observation(snapshot, runtime["value"])

        async def fake_recreate(target):
            runtime["value"] = topic_flag.is_true(topic_flag.read_key_value(self.path.read_bytes()))

        with patch.object(topic_flag, "preflight_on", AsyncMock(return_value=original)), \
             patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", side_effect=fake_measure), \
             patch.object(topic_flag, "atomic_write_bytes", side_effect=replace_then_fail_once), \
             patch.object(topic_flag, "recreate_and_verify_health", side_effect=fake_recreate):
            result = await topic_flag.turn_on(self.target, env_path=self.path)

        self.assertFalse(result["ok"])
        self.assertTrue(result["converge"]["ok"], result)
        self.assertFalse(topic_flag.is_true(topic_flag.read_key_value(self.path.read_bytes())))
        self.assertFalse(runtime["value"])
        self.assertEqual(calls["count"], 2)

    async def test_stable_off_is_idempotent_and_does_not_recreate(self):
        data = self.path.read_bytes()
        topic_flag.ensure_activation_marker(self.path)
        apply = AsyncMock()
        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation(data, False))), \
             patch.object(topic_flag, "_apply", apply):
            result = await topic_flag.turn_off(self.target, env_path=self.path)
        self.assertTrue(result["ok"], result)
        apply.assert_not_awaited()
        self.assertFalse(topic_flag.activation_marker_exists(self.path))

    async def test_off_noop_rejects_env_drift_after_observation(self):
        data = self.path.read_bytes()
        changed = data.replace(b"OTHER=preserve-me", b"OTHER=changed")

        async def measure_then_edit(target, env_path, snapshot=None):
            self.assertEqual(snapshot, data)
            self.path.write_bytes(changed)
            return observation(data, False)

        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", side_effect=measure_then_edit), \
             patch.object(topic_flag, "_apply", AsyncMock()) as apply:
            result = await topic_flag.turn_off(self.target, env_path=self.path)

        self.assertFalse(result["ok"])
        self.assertIn("변경", result["error"])
        self.assertEqual(self.path.read_bytes(), changed)
        apply.assert_not_awaited()

    async def test_off_does_not_noop_when_inspect_still_reports_true(self):
        data = self.path.read_bytes()
        apply = AsyncMock()
        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(
                 topic_flag, "measure",
                 AsyncMock(return_value=observation(data, False, inspect=True)),
             ), patch.object(topic_flag, "_apply", apply):
            result = await topic_flag.turn_off(self.target, env_path=self.path)
        self.assertTrue(result["ok"], result)
        apply.assert_awaited_once()

    async def test_off_requires_fx_endpoint_to_confirm_every_topic_is_disabled(self):
        data = env_bytes(topic="true")
        self.path.write_bytes(data)
        runtime = {"value": True}

        async def recreate(target):
            runtime["value"] = False

        after = observation(env_bytes(), False)
        after["fx"] = None
        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", AsyncMock(side_effect=[
                 observation(data, True), after,
             ])), patch.object(topic_flag, "recreate_and_verify_health", side_effect=recreate):
            result = await topic_flag.turn_off(self.target, env_path=self.path)

        self.assertFalse(result["ok"], result)
        self.assertIn("런타임 검증 실패", result["error"])
        self.assertFalse(topic_flag.is_true(topic_flag.read_key_value(self.path.read_bytes())))

    async def test_signal_cancellation_during_off_retries_to_convergence(self):
        data = env_bytes(topic="true")
        self.path.write_bytes(data)
        runtime = {"value": True}
        recreates = {"count": 0}

        async def measure(target, env_path, data=None):
            snapshot = Path(env_path).read_bytes() if data is None else data
            return observation(snapshot, runtime["value"])

        async def recreate(target):
            recreates["count"] += 1
            if recreates["count"] == 1:
                raise asyncio.CancelledError()
            runtime["value"] = False

        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", side_effect=measure), \
             patch.object(topic_flag, "recreate_and_verify_health", side_effect=recreate):
            result = await topic_flag.turn_off(self.target, env_path=self.path)

        self.assertFalse(result["ok"], "signal을 받은 원 실행은 성공으로 접지 않는다")
        self.assertTrue(result["converge"]["ok"], result)
        self.assertFalse(runtime["value"])
        self.assertFalse(topic_flag.is_true(topic_flag.read_key_value(self.path.read_bytes())))
        self.assertEqual(recreates["count"], 2)

    async def test_status_returns_two_for_an_unstable_state(self):
        with patch.object(
                topic_flag, "report_status",
                AsyncMock(return_value={"command": "status", "state": topic_flag.STATE_UNKNOWN}),
        ), redirect_stdout(StringIO()):
            code = await topic_flag.amain(["status"])
        self.assertEqual(code, 2)

    async def test_status_reports_active_transaction_as_ambiguous(self):
        data = self.path.read_bytes()
        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation(data, False))), \
             patch.object(topic_flag, "env_operation_is_locked", return_value=True):
            result = await topic_flag.report_status(self.target, env_path=self.path)
        self.assertEqual(result["state"], topic_flag.STATE_AMBIGUOUS)
        self.assertTrue(result["operation_locked"])

    async def test_status_preserves_interrupted_evidence_after_runtime_becomes_on(self):
        data = env_bytes(topic="true")
        self.path.write_bytes(data)
        topic_flag.ensure_activation_marker(self.path)
        with patch.object(topic_flag, "preflight_common", AsyncMock()), \
             patch.object(topic_flag, "measure", AsyncMock(return_value=observation(data, True))), \
             patch.object(topic_flag, "env_operation_is_locked", return_value=False):
            result = await topic_flag.report_status(self.target, env_path=self.path)
        self.assertEqual(result["state"], topic_flag.STATE_INTERRUPTED)
        self.assertTrue(result["activation_pending"])

    async def test_status_rejects_an_unverified_target_before_measurement(self):
        measure = AsyncMock()
        with patch.object(
                 topic_flag, "preflight_common",
                 AsyncMock(side_effect=topic_flag.Abort("target identity mismatch")),
             ), patch.object(topic_flag, "measure", measure):
            with self.assertRaises(topic_flag.Abort):
                await topic_flag.report_status(self.target, env_path=self.path)

        measure.assert_not_awaited()


_SIGNAL_CHILD = r"""
import asyncio, pathlib, sys
sys.path.insert(0, {repo!r})
from scripts import topic_flag
from scripts.canary_monitor import Target

env = pathlib.Path(sys.argv[1])
marker = pathlib.Path(sys.argv[2])
topic_flag.PRODUCTION_TARGET = Target(container="test-app", env_file=env)

async def fake_turn_on(*args, **kwargs):
    print("READY", flush=True)
    try:
        await asyncio.sleep(60)
    finally:
        await asyncio.sleep(0.05)
        marker.write_text("cleanup-ran")

topic_flag.turn_on = fake_turn_on
raise SystemExit(asyncio.run(topic_flag.amain_with_signals(["on"])))
"""


class TestSignalContract(unittest.TestCase):
    def test_real_signals_finish_cleanup_and_release_the_flock(self):
        from scripts.env_operation_lock import env_operation_lock

        for signame, expected in (("SIGTERM", 143), ("SIGHUP", 129)):
            with self.subTest(signame=signame), TemporaryDirectory() as directory:
                root = Path(directory)
                env = root / ".env"
                env.write_bytes(env_bytes())
                marker = root / "cleanup"
                child = subprocess.Popen(
                    [sys.executable, "-c", _SIGNAL_CHILD.format(
                        repo=str(topic_flag.Path(__file__).resolve().parent.parent)
                    ), str(env), str(marker)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                try:
                    self.assertEqual(child.stdout.readline().strip(), "READY")
                    child.send_signal(getattr(signal, signame))
                    code = child.wait(timeout=20)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)

                self.assertEqual(code, expected)
                self.assertEqual(marker.read_text(), "cleanup-ran")
                with env_operation_lock(env, "parent-after-signal"):
                    pass


class TestAuthStageParserParity(unittest.TestCase):
    """⛔ 파일 쪽 auth stage 는 **앱과 같은 strict 규칙**으로 봐야 한다.

    `topic_flag` 는 두 파서를 쓴다: `read_env_values`(canary_monitor 에서 임포트, 행 전체를
    `.strip()` 하는 **관대한** 파서)와 자기 `read_key_value`(raw). 후행 공백이 붙으면 둘이 갈리고,
    관대한 쪽으로 사전검사를 하면 **통과 → 재생성 → 컨테이너가 import 에서 죽는다**
    (`app.config` 는 모듈 레벨이고 `parse_topic_auth_stage` 는 정확 일치만 받는다).
    그 뒤 판정은 raw 비교라 영구 DRIFTED — 재실행이 재생성을 반복한다.

    ⚠️ 반대로 판정부를 strip 으로 "맞추면" 앱이 거부하는 값을 도구가 승인하게 된다.
       방향은 항상 strict 쪽이다(`is_true` docstring 과 같은 이유).
    """

    def test_padded_value_diverges_and_app_rejects_it(self):
        from app.config import parse_topic_auth_stage

        padded = topic_flag.REQUIRED_AUTH_STAGE + " "
        data = env_bytes(topic="true", auth_stage=padded)

        lenient = topic_flag.read_env_values(data.decode(), [topic_flag.AUTH_STAGE_KEY])
        self.assertEqual(
            lenient[topic_flag.AUTH_STAGE_KEY], topic_flag.REQUIRED_AUTH_STAGE,
            "관대한 파서가 후행 공백을 삼키지 않는다면 이 위험 자체가 없다 — 전제가 바뀌었다",
        )
        strict = topic_flag.read_key_value(data, topic_flag.AUTH_STAGE_KEY.encode()).decode()
        self.assertNotEqual(strict, topic_flag.REQUIRED_AUTH_STAGE)
        with self.assertRaises(ValueError):
            parse_topic_auth_stage(padded)   # 앱은 실제로 죽는다

    def test_exact_value_is_accepted_by_the_app(self):
        """상수 drift 방어 — REQUIRED_AUTH_STAGE 가 앱 enum 과 갈라지면 여기서 잡힌다."""
        from app.config import TopicAuthStage, parse_topic_auth_stage

        self.assertIs(
            parse_topic_auth_stage(topic_flag.REQUIRED_AUTH_STAGE),
            TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM,
        )

    def test_preflight_on_reads_auth_stage_with_the_strict_parser(self):
        source = inspect.getsource(topic_flag.preflight_on)
        self.assertIn("read_key_value(data, AUTH_STAGE_KEY.encode())", source)
        self.assertNotIn(
            "pending.get(AUTH_STAGE_KEY)", source,
            "관대한 파서로 auth stage 를 검사하면 앱이 거부하는 값이 운영에 배포된다",
        )



if __name__ == "__main__":
    unittest.main()
