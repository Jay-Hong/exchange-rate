"""`scripts/env_flip.py` — 운영 `.env` 1행 플립의 **정리 계약**을 잠근다.

⛔ 이 스크립트가 틀리면 남는 것은 "운영 `.env` 가 반쯤 바뀐 상태"다. 그래서 검증 대상은
결과값이 아니라 **각 단계에서 죽었을 때 무엇이 남는가**이다:

- write **전** 실패 → 원본 그대로 + backup 없음 + **컨테이너 재생성 없음**
  (`EnvRestorer.restore()` 에는 no-op 분기가 없어 부르면 운영 컨테이너를 헛되이 재생성한다)
- write **후** 실패 → 원본 바이트 복원 + backup 제거 + 재생성 1회
- 성공 → 변경 유지 + backup 제거(디렉터리 fsync 까지가 commit point)
- signal(SIGHUP/SIGTERM) → 위 규칙 그대로. **반복 signal 은 정리를 끊지 않는다**
- SIGKILL(정리 불가) → **backup 이 남아 수동 복구가 가능**하다
"""

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import canary_monitor
from scripts import env_flip
from scripts.canary_monitor import Target

ORIGINAL_ENV = (
    "# 운영 설정\n"
    "ENV=development\n"
    "LOG_LEVEL=INFO\n"
    "ADMIN_PASSWORD=super-secret-value\n"
    "TOPIC_DISPATCHER_ENABLED=false\n"
    "KRX_CLIENT_DISTRIBUTION_ENABLED=false\n"
)
FLIPPED_ENV = ORIGINAL_ENV.replace("ENV=development", "ENV=production")


class TestPlanNewBytes(unittest.TestCase):
    """⛔ 치환은 **바꾸기 전에** 판정한다 — `sed` 가 못 하던 지점이 여기다."""

    def test_replaces_single_line_and_preserves_everything_else(self):
        data = b"A=1\nENV=development\nB=2\n"
        self.assertEqual(env_flip.plan_new_bytes(data), b"A=1\nENV=production\nB=2\n")

    def test_preserves_crlf(self):
        data = b"A=1\r\nENV=development\r\nB=2\r\n"
        self.assertEqual(env_flip.plan_new_bytes(data), b"A=1\r\nENV=production\r\nB=2\r\n")

    def test_preserves_missing_final_newline(self):
        self.assertEqual(env_flip.plan_new_bytes(b"A=1\nENV=development"),
                         b"A=1\nENV=production")

    def test_similar_keys_are_untouched(self):
        """⚠️ `ENVIRONMENT=`·`ENV_MODE=` 는 `ENV=` 가 아니다."""
        data = b"ENVIRONMENT=x\nENV=development\nENV_MODE=y\n"
        self.assertEqual(env_flip.plan_new_bytes(data),
                         b"ENVIRONMENT=x\nENV=production\nENV_MODE=y\n")

    def test_aborts_when_absent(self):
        """⛔ `sed` 는 여기서 **아무 일도 안 하면서 성공**한다."""
        with self.assertRaises(env_flip.Abort):
            env_flip.plan_new_bytes(b"A=1\nB=2\n")

    def test_aborts_on_duplicate_keys(self):
        """⛔ `sed` 는 여기서 **둘 다** 바꾼다."""
        with self.assertRaises(env_flip.Abort):
            env_flip.plan_new_bytes(b"ENV=development\nENV=development\n")

    def test_aborts_when_already_production(self):
        with self.assertRaises(env_flip.Abort):
            env_flip.plan_new_bytes(b"ENV=production\n")

    def test_aborts_on_unexpected_value(self):
        with self.assertRaises(env_flip.Abort):
            env_flip.plan_new_bytes(b"ENV=staging\n")


class TestAmbientGuard(unittest.TestCase):
    """⛔ 컨테이너의 `ENV` 는 `env_file` 이 아니라 `environment:` 에서 오고,
    `${...}` interpolation 은 **셸 변수가 `--env-file` 을 이긴다**."""

    def test_names_are_derived_from_the_real_compose_file(self):
        """⚠️ 거부 목록을 하드코딩하면 compose 에 변수가 추가될 때 조용히 낡는다."""
        names = env_flip.compose_interpolated_names(
            env_flip.COMPOSE_FILE.read_text(encoding="utf-8"))
        self.assertIn("ENV", names)
        for expected in ("LOG_LEVEL", "DATABASE_URL", "REDIS_URL", "REDIS_PASSWORD"):
            self.assertIn(expected, names, f"{expected} 가 보호 대상에서 빠졌다")

    def test_clean_shell_has_no_problems(self):
        self.assertEqual(env_flip.ambient_problems({"PATH": "/usr/bin"}, {"ENV", "LOG_LEVEL"}), [])

    def test_interpolated_variable_in_shell_is_rejected(self):
        problems = env_flip.ambient_problems({"ENV": "production"}, {"ENV"})
        self.assertEqual(len(problems), 1)
        self.assertIn("ENV", problems[0])

    def test_secret_value_is_never_echoed(self):
        """⛔ 거부 메시지에 값이 실리면 `DATABASE_URL`·`REDIS_PASSWORD` 가 그대로 샌다."""
        problems = env_flip.ambient_problems(
            {"REDIS_PASSWORD": "hunter2-do-not-print"}, {"REDIS_PASSWORD"})
        self.assertEqual(len(problems), 1)
        self.assertNotIn("hunter2-do-not-print", problems[0])

    def test_target_selectors_are_rejected(self):
        """⛔ `PRODUCTION_TARGET` 은 `-p`·`-f` 를 붙이지 않는다 — 이 변수들이 대상을 바꾼다."""
        for name in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "DOCKER_HOST", "DOCKER_CONTEXT"):
            with self.subTest(name=name):
                self.assertTrue(env_flip.ambient_problems({name: "x"}, set()))


class TestClassifyStdout(unittest.TestCase):
    """⚠️ "모든 줄이 JSON" 을 요구하면 **false red** 다 — uvicorn 은 평문을 계속 낸다."""

    def test_mixed_uvicorn_plaintext_and_app_json(self):
        logs = (
            "INFO:     Uvicorn running on http://0.0.0.0:8000\n"
            + json.dumps({"timestamp": "t", "level": "INFO", "logger": "exchange_rate",
                          "message": "\\ud83d\\ude80 start", "env": "production"}) + "\n"
        )
        result = env_flip.classify_stdout(logs)
        self.assertEqual(result["app_json_lines"], 1)
        self.assertEqual(result["startup_env"], "production")

    def test_escaped_korean_does_not_break_detection(self):
        """⛔ python-json-logger 는 `ensure_ascii` 라 한국어가 escape 된다 — **원문 grep 은 뒤집힌다**."""
        line = json.dumps({"timestamp": "t", "level": "INFO", "logger": "exchange_rate",
                           "message": "서버 시작", "env": "production"}, ensure_ascii=True)
        self.assertNotIn("서버 시작", line)                     # escape 되었음을 실증
        self.assertEqual(env_flip.classify_stdout(line)["startup_env"], "production")

    def test_console_format_yields_no_app_json(self):
        """전환 **전** 형식 — 이 0 이 곧 판별 근거다."""
        logs = "2026-08-05 01:00:00 | INFO     | exchange_rate:boot:1 | 시작\n"
        self.assertEqual(env_flip.classify_stdout(logs)["app_json_lines"], 0)

    def test_counts_error_lines(self):
        logs = "ERROR something\nTraceback (most recent call last):\nINFO fine\n"
        self.assertEqual(env_flip.classify_stdout(logs)["error_lines"], 2)


class TestErrorLogAccounting(unittest.TestCase):
    """⚠️ size 만 보면 **회전을 못 본다**(10MB, backupCount 3)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "error.log"
        self.addCleanup(self._tmp.cleanup)

    def test_counts_only_the_tail_after_baseline(self):
        self.path.write_text("ERROR 옛날 것\n", encoding="utf-8")
        baseline = env_flip.log_baseline(self.path)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("ERROR 새 것\nINFO 괜찮음\n")
        self.assertEqual(env_flip.count_new_errors(self.path, baseline), 1)

    def test_absent_file_stays_zero(self):
        self.assertEqual(env_flip.count_new_errors(self.path, env_flip.log_baseline(self.path)), 0)

    def test_rotation_by_inode_is_fail_closed(self):
        """⛔ 새 파일만 읽으면 회전 직전 구간을 통째로 놓쳐 **틀린 안심**이 된다.

        ⚠️ **`unlink` 후 재생성으로 회전을 흉내내면 안 된다** — macOS(APFS)는 새 inode 를
        주지만 **Linux(ext4)는 같은 번호를 재사용**해 CI 에서만 깨진다(실제로 그렇게 red 가 났다:
        `93176 == 93176`). `RotatingFileHandler` 가 실제로 하는 대로 **rename 후 새 파일 생성**
        을 쓴다 — 구 inode 를 `error.log.1` 이 계속 붙잡으므로 새 파일은 **반드시** 다른 inode 를
        받는다(파일시스템 무관).
        ⚠️ 새 파일은 baseline 보다 **크게** 만든다 — 작으면 size 검사가 먼저 잡아 inode 경로가
        격리되지 않는다(1차 변이 테스트에서 그 변이가 생존했다).
        """
        self.path.write_text("ERROR 옛날 것\n" * 20, encoding="utf-8")
        baseline = env_flip.log_baseline(self.path)
        rotated = self.path.parent / (self.path.name + ".1")
        os.rename(self.path, rotated)                                   # 구 inode 를 붙잡아 둔다
        self.path.write_text("INFO 새 파일\n" * 50, encoding="utf-8")
        self.assertGreater(self.path.stat().st_size, baseline["size"],
                           "size 검사가 대신 잡으면 inode 경로를 검증하지 못한다")
        self.assertNotEqual(self.path.stat().st_ino, baseline["inode"],
                            "회전을 재현하지 못했다 — 이 테스트는 inode 경로를 검증하지 않는다")
        with self.assertRaises(env_flip.Abort):
            env_flip.count_new_errors(self.path, baseline)

    def test_truncation_is_fail_closed(self):
        self.path.write_text("ERROR 길게 길게 길게\n", encoding="utf-8")
        baseline = env_flip.log_baseline(self.path)
        with self.path.open("r+b") as handle:                      # inode 유지 + 축소
            handle.truncate(0)
        with self.assertRaises(env_flip.Abort):
            env_flip.count_new_errors(self.path, baseline)

    def test_disappearance_is_fail_closed(self):
        self.path.write_text("x\n", encoding="utf-8")
        baseline = env_flip.log_baseline(self.path)
        self.path.unlink()
        with self.assertRaises(env_flip.Abort):
            env_flip.count_new_errors(self.path, baseline)


class TestFileInvariants(unittest.TestCase):

    def test_accepts_the_expected_shape(self):
        env_flip.check_file_invariants(ORIGINAL_ENV, "t")          # 예외 없음

    def test_rejects_enabled_release_flag(self):
        text = ORIGINAL_ENV.replace("TOPIC_DISPATCHER_ENABLED=false",
                                    "TOPIC_DISPATCHER_ENABLED=true")
        with self.assertRaises(env_flip.Abort):
            env_flip.check_file_invariants(text, "t")

    def test_rejects_empty_admin_password(self):
        """⛔ production 전환이 곧 admin endpoint 503 이 되는 조합이다."""
        text = ORIGINAL_ENV.replace("ADMIN_PASSWORD=super-secret-value", "ADMIN_PASSWORD=")
        with self.assertRaises(env_flip.Abort):
            env_flip.check_file_invariants(text, "t")

    def test_rejects_missing_admin_password(self):
        text = ORIGINAL_ENV.replace("ADMIN_PASSWORD=super-secret-value\n", "")
        with self.assertRaises(env_flip.Abort):
            env_flip.check_file_invariants(text, "t")

    def test_abort_message_does_not_leak_the_password(self):
        text = ORIGINAL_ENV.replace("TOPIC_DISPATCHER_ENABLED=false",
                                    "TOPIC_DISPATCHER_ENABLED=true")
        with self.assertRaises(env_flip.Abort) as caught:
            env_flip.check_file_invariants(text, "t")
        self.assertNotIn("super-secret-value", str(caught.exception))

    def test_rejects_group_or_world_readable_env(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(ORIGINAL_ENV, encoding="utf-8")
            path.chmod(0o664)
            with self.assertRaises(env_flip.Abort) as caught:
                env_flip.check_env_file_mode(path)
            self.assertIn("chmod 600", str(caught.exception))

    def test_accepts_owner_only_env(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(ORIGINAL_ENV, encoding="utf-8")
            path.chmod(0o600)
            env_flip.check_env_file_mode(path)

    def test_rejects_group_or_world_readable_legacy_backup(self):
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(ORIGINAL_ENV, encoding="utf-8")
            backup = Path(tmp) / ".env.bak.20260805"
            backup.write_text("SECRET=old\n", encoding="utf-8")
            backup.chmod(0o664)
            with self.assertRaises(env_flip.Abort) as caught:
                env_flip.check_legacy_backup_modes(env)
            self.assertIn("chmod 600", str(caught.exception))
            self.assertNotIn("SECRET=old", str(caught.exception))

    def test_accepts_owner_only_legacy_backups(self):
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(ORIGINAL_ENV, encoding="utf-8")
            backup = Path(tmp) / ".env.bak"
            backup.write_text("SECRET=old\n", encoding="utf-8")
            backup.chmod(0o600)
            env_flip.check_legacy_backup_modes(env)


class TestPreflight(unittest.IsolatedAsyncioTestCase):
    """⛔ **실제 `preflight`** 를 돌린다.

    ⚠️ 정리-계약 테스트는 `preflight` 를 통째로 patch 하므로 이 가드들을 **한 번도 실행하지
    않는다** — 변이 테스트에서 "backup 이 남아도 계속 진행" 변이가 그래서 살아남았다.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.env_path = root / ".env"
        self.env_path.write_text(ORIGINAL_ENV, encoding="utf-8")
        self.env_path.chmod(0o600)
        self.error_log = root / "error.log"
        self.target = Target(container="test-app", env_file=self.env_path)
        self.addCleanup(self._tmp.cleanup)

    def _stub_docker(self, *, oneoff="", env_value="development"):
        async def fake_identity(target):
            return []

        async def fake_run(command, **kwargs):
            if "ps" in command:
                return oneoff
            return "{}"

        async def fake_container_env(keys, target=None):
            return {key: (env_value if key == "ENV" else "false") for key in keys}

        return (
            patch.object(env_flip, "ambient_problems", lambda *a, **k: []),
            patch.object(env_flip, "check_target_identity", fake_identity),
            patch.object(env_flip, "run_command", fake_run),
            patch.object(env_flip, "container_env", fake_container_env),
            patch.object(env_flip, "inspect_value",
                         lambda target, fmt: asyncio.sleep(0, result="cid")),
            patch.object(env_flip, "project_label",
                         lambda target: asyncio.sleep(0, result="p")),
        )

    async def _preflight(self, **kwargs):
        for item in self._stub_docker(**kwargs):
            item.start()
            self.addCleanup(item.stop)
        return await env_flip.preflight(self.target, self.env_path, self.error_log)

    async def test_passes_on_a_clean_stack(self):
        baseline = await self._preflight()
        self.assertEqual(baseline["project"], "p")

    async def test_aborts_while_a_previous_backup_remains(self):
        """⛔ 잔존 backup 위에 새 창을 열면 **바뀐 상태를 원본으로** 저장해 버린다."""
        canary_monitor.create_env_backup(self.env_path)
        with self.assertRaises(env_flip.Abort) as caught:
            await self._preflight()
        self.assertIn("--recover", str(caught.exception),
                      "잔존 backup 을 그냥 되돌리면 완료된 보안 수정이 사라진다 — 경고가 있어야 한다")

    async def test_aborts_while_a_cron_oneoff_container_runs(self):
        with self.assertRaises(env_flip.Abort):
            await self._preflight(oneoff="abc123\n")

    async def test_aborts_when_the_container_is_not_development(self):
        with self.assertRaises(env_flip.Abort):
            await self._preflight(env_value="production")

    async def test_aborts_when_env_is_group_or_world_readable(self):
        self.env_path.chmod(0o664)
        with self.assertRaises(env_flip.Abort) as caught:
            await self._preflight()
        self.assertIn("chmod 600", str(caught.exception))

    async def test_aborts_when_a_legacy_backup_is_group_or_world_readable(self):
        backup = self.env_path.parent / ".env.bak.old"
        backup.write_text("SECRET=old\n", encoding="utf-8")
        backup.chmod(0o664)
        with self.assertRaises(env_flip.Abort) as caught:
            await self._preflight()
        self.assertIn(".env.bak.old", str(caught.exception))

    async def test_ambient_env_is_rejected_before_anything_else(self):
        """⛔ 셸 변수가 `--env-file` 을 이기므로 **파일을 보기 전에** 막아야 한다."""
        for item in self._stub_docker()[1:]:                 # ambient 는 진짜를 쓴다
            item.start()
            self.addCleanup(item.stop)
        with patch.dict(os.environ, {"ENV": "production"}):
            with self.assertRaises(env_flip.Abort) as caught:
                await env_flip.preflight(self.target, self.env_path, self.error_log)
        self.assertIn("ENV", str(caught.exception))


class TestSmokeWiring(unittest.IsolatedAsyncioTestCase):
    """smoke helper를 직접 호출해 운영 판정 배선을 잠근다."""

    async def _run_smoke(self, ws_output="rates 30"):
        target = Target(container="test-app")
        baseline = {
            "container_id": "old-id",
            "project": "exchange-rate",
            "error_log": {"exists": False, "inode": None, "size": 0},
        }

        async def inspect(target, fmt):
            if fmt == "{{.Id}}":
                return "new-id"
            if fmt == "{{.RestartCount}}":
                return "0"
            if fmt == env_flip.MATCH_FMT % "production":
                return "MATCH"
            raise AssertionError(f"예상하지 않은 inspect: {fmt}")

        async def identity(target):
            return []

        async def project(target):
            return "exchange-rate"

        async def live_env(keys, target=None):
            return {"ENV": "production", **{key: "false" for key in env_flip.FLAG_KEYS}}

        startup = json.dumps({
            "timestamp": "t", "level": "INFO", "logger": "exchange_rate",
            "message": "start", "env": "production",
        })

        async def command(argv, **kwargs):
            if "logs" in argv:
                return startup
            joined = " ".join(argv)
            if env_flip.WRONG_PW_PY in joined:
                return "401"
            if env_flip.WS_PY in joined:
                return ws_output
            raise AssertionError(f"예상하지 않은 command: {argv}")

        async def admin_ok(target, path):
            return "200"

        with patch.object(env_flip, "inspect_value", inspect), \
             patch.object(env_flip, "check_target_identity", identity), \
             patch.object(env_flip, "project_label", project), \
             patch.object(env_flip, "container_env", live_env), \
             patch.object(env_flip, "run_command", command), \
             patch.object(env_flip, "_admin_ok", admin_ok), \
             patch.object(env_flip, "count_new_errors", return_value=0):
            return await env_flip.smoke(target, baseline, Path("unused-error.log"))

    async def test_happy_path_requires_a_nonempty_legacy_payload(self):
        result = await self._run_smoke("rates 30")
        self.assertEqual(result["ws_initial"], {"type": "rates", "rate_count": 30})
        self.assertEqual(result["new_errors"], 0)

    async def test_empty_legacy_payload_is_not_a_success(self):
        with self.assertRaises(env_flip.Abort, msg="type만 rates면 빈 payload도 성공으로 접혔다"):
            await self._run_smoke("rates 0")


class _Stage:
    """단계별 실패 주입 — 어느 단계에서 죽어도 정리 계약이 지켜지는가."""

    def __init__(self):
        self.recreates = 0

    async def recreate(self, target=None):
        self.recreates += 1


class TestRunFlipCleanupContract(unittest.IsolatedAsyncioTestCase):
    """⛔ 핵심 계약: **무엇이 남는가**."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.env_path = root / ".env"
        self.env_path.write_text(ORIGINAL_ENV, encoding="utf-8")
        self.error_log = root / "error.log"
        self.error_log.write_text("", encoding="utf-8")
        self.target = Target(container="test-app", env_file=self.env_path)
        self.backup = canary_monitor.backup_path_for(self.env_path)
        self.stage = _Stage()
        self.addCleanup(self._tmp.cleanup)

    def _patch(self, *, preflight_ok=True, smoke_result=None, smoke_error=None,
               recreate_error=None, restore_recreate_error=None):
        """⚠️ **S5 의 재생성과 복원의 재생성은 다른 호출**이다 — 같은 fake 로 묶으면
        "S5 실패"를 주입했을 뿐인데 복원까지 실패해 계약을 잘못 검증한다(실제로 그랬다).
        둘 다 실패하는 경우는 `restore_recreate_error` 로 **따로** 검증한다."""

        async def fake_preflight(target, env_path, error_log):
            if not preflight_ok:
                raise env_flip.Abort("주입된 preflight 실패")
            return {"container_id": "old", "project": "p",
                    "error_log": env_flip.log_baseline(error_log)}

        async def fake_recreate(target=None):                    # S5
            if recreate_error is not None:
                raise recreate_error
            await self.stage.recreate(target)

        async def fake_restore_recreate(target=None):            # EnvRestorer 내부
            if restore_recreate_error is not None:
                raise restore_recreate_error
            await self.stage.recreate(target)

        async def fake_smoke(target, baseline, error_log):
            if smoke_error is not None:
                raise smoke_error
            return smoke_result or {"new_errors": 0}

        return (
            patch.object(env_flip, "preflight", fake_preflight),
            patch.object(env_flip, "recreate_and_verify_health", fake_recreate),
            patch.object(env_flip, "smoke", fake_smoke),
            # EnvRestorer 는 **진짜** 를 쓴다 — 파일 복원·byte 검증·backup 삭제가 검증 대상이다.
            patch.object(canary_monitor, "recreate_and_verify_health", fake_restore_recreate),
            patch.object(env_flip, "inspect_value",
                         lambda target, fmt: asyncio.sleep(0, result="MATCH")),
        )

    async def _run(self, **kwargs):
        patches = self._patch(**kwargs)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return await env_flip.run_flip(self.target, env_path=self.env_path,
                                       error_log=self.error_log)

    async def test_success_keeps_the_change_and_removes_the_backup(self):
        summary = await self._run()
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), FLIPPED_ENV)
        self.assertFalse(self.backup.exists(), "성공했는데 secret 사본이 남았다")
        self.assertEqual(self.stage.recreates, 1, "성공 경로는 재생성 1회여야 한다")

    async def test_preflight_failure_leaves_no_backup_and_no_recreate(self):
        """⛔ 읽기 전용 실패가 **운영 컨테이너를 재생성하면 안 된다**."""
        summary = await self._run(preflight_ok=False)
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["wrote"])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.stage.recreates, 0, "아무것도 안 바꿨는데 재생성했다")

    async def test_os_error_in_preflight_returns_a_summary_instead_of_escaping(self):
        """⚠️ preflight 는 `.env` 와 과거 backup 을 `stat`/`read` 한다 — 깨진 symlink·권한
        문제는 **Abort 가 아닌 `OSError`** 로 온다. 그게 그대로 올라가면 summary 없이
        traceback 만 남아 "어디까지 갔는지"가 안 보인다(이 구간은 아직 만든 것이 없다)."""
        patches = self._patch()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch.object(env_flip, "preflight",
                          side_effect=PermissionError("주입된 stat 실패")):
            summary = await env_flip.run_flip(self.target, env_path=self.env_path,
                                              error_log=self.error_log)
        self.assertFalse(summary["ok"])
        self.assertIn("PermissionError", summary["error"])
        self.assertFalse(summary["wrote"])
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.stage.recreates, 0)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)

    async def test_plan_failure_leaves_no_backup(self):
        self.env_path.write_text(ORIGINAL_ENV.replace("ENV=development", "ENV=production"),
                                 encoding="utf-8")
        summary = await self._run()
        self.assertFalse(summary["ok"])
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.stage.recreates, 0)

    async def test_recreate_failure_restores_original_bytes(self):
        summary = await self._run(recreate_error=RuntimeError("주입된 재생성 실패"))
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["wrote"])
        self.assertTrue(summary["rolled_back"], summary)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)
        self.assertFalse(self.backup.exists(), "복원 검증 후엔 backup 을 지운다")

    async def test_smoke_failure_restores_original_bytes(self):
        summary = await self._run(smoke_error=env_flip.Abort("주입된 smoke 실패"))
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["rolled_back"], summary)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)
        self.assertFalse(self.backup.exists())

    async def test_cancellation_after_write_restores(self):
        """⛔ signal 은 **취소**로 도착한다 — `except Exception` 이면 복원이 통째로 건너뛰어진다."""
        summary = await self._run(recreate_error=asyncio.CancelledError())
        self.assertTrue(summary["wrote"])
        self.assertTrue(summary["rolled_back"], summary)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)

    async def test_failing_rollback_preserves_the_backup_for_manual_recovery(self):
        """⛔ **가장 나쁜 경로**: S5 도 복원도 실패한다(docker 자체가 죽은 경우).

        이때 남아야 하는 것: (1) `.env` 는 **원본 바이트로 되돌아가 있고**
        (`EnvRestorer` 가 recreate **전에** 파일을 먼저 되쓴다) (2) backup 이 **남아 있어**
        수동 복구가 가능하며 (3) summary 가 그 사실을 말한다. 조용히 성공처럼 끝나면 안 된다.
        """
        summary = await self._run(recreate_error=RuntimeError("docker 죽음"),
                                  restore_recreate_error=RuntimeError("docker 여전히 죽음"))
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["wrote"])
        self.assertFalse(summary["rolled_back"])
        self.assertIn("rollback_error", summary)
        self.assertIn("manual", summary)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV,
                         "복원은 recreate 전에 파일을 먼저 되쓴다")
        self.assertTrue(self.backup.exists(), "복원이 실패했는데 수동 복구 수단까지 사라졌다")
        self.assertEqual(self.backup.read_text(encoding="utf-8"), ORIGINAL_ENV)

    async def test_write_failure_removes_backup_without_recreate(self):
        """write 자체가 실패하면 파일은 원본 그대로다 — 재생성할 이유가 없다."""
        patches = self._patch()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch.object(env_flip, "atomic_write_bytes",
                          side_effect=OSError("주입된 write 실패")):
            summary = await env_flip.run_flip(self.target, env_path=self.env_path,
                                              error_log=self.error_log)
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["wrote"])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.stage.recreates, 0)

    async def test_post_replace_failure_restores_instead_of_deleting_backup(self):
        """replace 뒤 fsync 실패를 write 전 실패로 접으면 env/container 상태가 갈라진다."""
        patches = self._patch()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        def replace_then_fail(path, data, *, mode=None):
            Path(path).write_bytes(data)
            raise OSError("주입된 directory fsync 실패")

        with patch.object(env_flip, "atomic_write_bytes", side_effect=replace_then_fail):
            summary = await env_flip.run_flip(self.target, env_path=self.env_path,
                                              error_log=self.error_log)
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["wrote"])
        self.assertTrue(summary["rolled_back"])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.stage.recreates, 1, "변경 가능성이 있으면 원본으로 재생성해야 한다")

    async def test_stale_plan_is_rejected_before_write(self):
        """S1 뒤 env 가 바뀌면 과거 bytes 로 다른 운영 변경을 덮어쓰면 안 된다."""
        patches = self._patch()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        real_backup = env_flip.create_env_backup

        def backup_then_external_edit(path):
            backup = real_backup(path)
            Path(path).write_text(ORIGINAL_ENV + "CONCURRENT=1\n", encoding="utf-8")
            return backup

        with patch.object(env_flip, "create_env_backup", side_effect=backup_then_external_edit):
            summary = await env_flip.run_flip(self.target, env_path=self.env_path,
                                              error_log=self.error_log)
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["wrote"])
        self.assertIn("stale", summary["error"])
        self.assertEqual(self.env_path.read_text(encoding="utf-8"),
                         ORIGINAL_ENV + "CONCURRENT=1\n")
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.stage.recreates, 0)


class TestSigkillSurvivability(unittest.TestCase):
    """⛔ SIGKILL 은 정리를 돌릴 수 없다 — 그래서 **backup 이 남아야** 수동 복구가 가능하다."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.env_path = Path(self._tmp.name) / ".env"
        self.env_path.write_text(ORIGINAL_ENV, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_backup_holds_the_original_between_write_and_commit(self):
        """이 구간에서 프로세스가 즉사해도 backup 만으로 원본을 되돌릴 수 있어야 한다."""
        backup = canary_monitor.create_env_backup(self.env_path)
        canary_monitor.atomic_write_bytes(
            self.env_path, env_flip.plan_new_bytes(self.env_path.read_bytes()))

        # ── 여기서 SIGKILL 이 왔다고 가정한다(정리 코드는 한 줄도 돌지 않는다) ──
        self.assertTrue(backup.exists(), "backup 이 없으면 수동 복구 수단이 없다")
        self.assertEqual(backup.read_text(encoding="utf-8"), ORIGINAL_ENV)
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), FLIPPED_ENV)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600, "secret 사본 권한이 느슨하다")

        # 수동 복구가 실제로 가능한가
        canary_monitor.atomic_write_bytes(self.env_path, backup.read_bytes())
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)

    def test_second_run_refuses_while_a_backup_remains(self):
        """⛔ 잔존 backup 위에 새 창을 열면 **바뀐 상태를 원본으로 저장**해 버린다."""
        canary_monitor.create_env_backup(self.env_path)
        with self.assertRaises(canary_monitor.CollectorError):
            canary_monitor.create_env_backup(self.env_path)


class TestSignalContract(unittest.TestCase):
    """⛔ 첫 signal 만 취소로 바꾸고 **반복은 무시**한다 — 정리 중 재취소는 복원을 끊는다."""

    def test_reuses_the_canary_single_shot_canceller(self):
        self.assertIs(env_flip.make_signal_canceller, canary_monitor.make_signal_canceller)
        self.assertIs(env_flip.CANCEL_ON_SIGNALS, canary_monitor.CANCEL_ON_SIGNALS)

    def test_hangup_and_terminate_are_both_covered(self):
        """⚠️ SSH 끊김은 SIGHUP 이다 — SIGTERM 만 잡으면 그 경로가 안 잡힌다."""
        self.assertIn(signal_number("SIGHUP"), env_flip.CANCEL_ON_SIGNALS)
        self.assertIn(signal_number("SIGTERM"), env_flip.CANCEL_ON_SIGNALS)

    def test_repeated_signals_do_not_cancel_again(self):
        class _Task:
            def __init__(self):
                self.cancels = 0

            def cancel(self):
                self.cancels += 1

        task = _Task()
        received: dict = {"signum": None}
        handler = canary_monitor.make_signal_canceller(task, received)
        handler(15)
        handler(15)
        handler(1)
        self.assertEqual(task.cancels, 1, "반복 signal 이 정리 중인 복원을 끊는다")

    def test_handler_is_installed_before_the_backup_exists(self):
        """⛔ 순서가 계약이다 — backup 이 생긴 뒤의 중단에는 반드시 정리 경로가 있어야 한다."""
        import inspect
        source = inspect.getsource(env_flip.amain_with_signals)
        self.assertIn("add_signal_handler", source)
        # backup 생성은 `run_flip` 안이고, 그 호출은 handler 설치 뒤의 `await task` 로만 도달한다.
        self.assertIn("create_env_backup", inspect.getsource(env_flip.run_flip))
        self.assertNotIn("create_env_backup", source)

    def test_real_process_signals_restore_then_return_signal_exit_code(self):
        """helper 멤버십이 아니라 실제 프로세스의 env 복원과 종료코드를 본다."""
        child_source = r'''
import asyncio
import pathlib
import sys

sys.path.insert(0, sys.argv[1])
from scripts import canary_monitor, env_flip

env_path = pathlib.Path(sys.argv[2])
error_log = pathlib.Path(sys.argv[3])
target = canary_monitor.Target(container="test-app", env_file=env_path)
real_run_flip = env_flip.run_flip

async def fake_preflight(target, env_path, error_log):
    return {"container_id": "old", "project": "p",
            "error_log": env_flip.log_baseline(error_log)}

async def wait_for_signal(target=None):
    print("READY", flush=True)
    await asyncio.sleep(60)

async def fake_smoke(target, baseline, error_log):
    raise AssertionError("signal 뒤 smoke로 진행하면 안 된다")

async def restore_recreate(target=None):
    await asyncio.sleep(0.05)

async def wrapped_run_flip():
    return await real_run_flip(target, env_path=env_path, error_log=error_log)

env_flip.preflight = fake_preflight
env_flip.recreate_and_verify_health = wait_for_signal
env_flip.smoke = fake_smoke
env_flip.inspect_value = lambda target, fmt: asyncio.sleep(0, result="MATCH")
canary_monitor.recreate_and_verify_health = restore_recreate
env_flip.run_flip = wrapped_run_flip
raise SystemExit(asyncio.run(env_flip.amain_with_signals([])))
'''
        for signame, expected in (("SIGTERM", 143), ("SIGHUP", 129)):
            with self.subTest(signame=signame), TemporaryDirectory() as tmp:
                root = Path(tmp)
                env_path = root / ".env"
                env_path.write_text(ORIGINAL_ENV, encoding="utf-8")
                env_path.chmod(0o600)
                error_log = root / "error.log"
                error_log.write_text("", encoding="utf-8")
                child = subprocess.Popen(
                    [sys.executable, "-c", child_source, str(env_flip.REPO_ROOT),
                     str(env_path), str(error_log)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    ready = ""
                    while ready != "READY":
                        line = child.stdout.readline()
                        self.assertTrue(line, "자식이 READY 전에 종료했다")
                        ready = line.strip()
                    child.send_signal(signal_number(signame))
                    code = child.wait(timeout=20)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)

                self.assertEqual(code, expected)
                self.assertEqual(env_path.read_text(encoding="utf-8"), ORIGINAL_ENV)
                self.assertFalse(canary_monitor.backup_path_for(env_path).exists())


def signal_number(name: str) -> int:
    import signal as signal_module
    return getattr(signal_module, name)


class TestDurableUnlink(unittest.TestCase):

    def test_removes_the_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "x"
            path.write_text("x", encoding="utf-8")
            env_flip.unlink_durably(path)
            self.assertFalse(path.exists())

    def test_success_commit_point_includes_directory_fsync(self):
        """⚠️ unlink 만 하고 죽으면 backup 이 되살아나 '미적용'과 구분되지 않는다."""
        import inspect
        self.assertIn("os.fsync", inspect.getsource(env_flip.unlink_durably))


class TestNoSecretsInOutput(unittest.TestCase):
    """⛔ 산출물·stdout 에 secret 이 실리지 않는가.

    ⚠️ **문자열 매칭으로 검사하지 않는다.** 처음엔 소스 전체에서 금지 문자열을 찾았는데,
    그러면 "이 포맷은 위험하다"고 **설명하는 주석**까지 위반으로 잡힌다(실제로 그랬다).
    검사 대상은 서술이 아니라 **실제 호출 인자**다 → AST 로 본다.
    """

    SECRET_HINTS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "DATABASE_URL")

    def setUp(self):
        import ast
        self.tree = ast.parse(Path(env_flip.__file__).read_text(encoding="utf-8"))
        self.ast = ast

    def _calls(self, name: str):
        for node in self.ast.walk(self.tree):
            if isinstance(node, self.ast.Call):
                func = node.func
                if getattr(func, "id", None) == name or getattr(func, "attr", None) == name:
                    yield node

    def test_container_env_is_never_asked_for_secret_keys(self):
        """⛔ `container_env` 는 받은 키의 **값을 그대로** 직렬화해 돌려준다."""
        seen = 0
        for call in self._calls("container_env"):
            seen += 1
            keys = call.args[0]
            self.assertIsInstance(keys, self.ast.List, "키 목록이 리터럴이 아니면 검사할 수 없다")
            names = []
            for element in keys.elts:
                if isinstance(element, self.ast.Constant):
                    names.append(element.value)
                elif isinstance(element, self.ast.Starred):          # *FLAG_KEYS
                    names.extend(getattr(env_flip, element.value.id))
                else:
                    self.fail(f"검사할 수 없는 키 표현식: {self.ast.dump(element)}")
            for name in names:
                for hint in self.SECRET_HINTS:
                    self.assertNotIn(hint, name.upper(),
                                     f"container_env 에 secret 후보 키를 넘겼다: {name}")
        self.assertGreater(seen, 0, "호출을 하나도 못 찾았다 — 검사가 헛돌고 있다")

    def test_no_call_argument_dumps_the_whole_container_env(self):
        """⛔ `{{json .Config.Env}}` 는 ADMIN_PASSWORD·KIS_APP_SECRET·DATABASE_URL 을 전량 덤프한다.

        ⚠️ `.Config.Env` 자체는 금지 대상이 아니다 — `MATCH_FMT` 은 같은 필드를 순회하면서
        **리터럴 `MATCH` 하나만** 낸다. 금지되는 것은 **통째로 직렬화하는 형태**다.
        """
        checked = 0
        for node in self.ast.walk(self.tree):
            if not isinstance(node, self.ast.Call):
                continue
            for argument in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(argument, self.ast.Constant) and isinstance(argument.value, str):
                    checked += 1
                    self.assertNotIn("json .Config.Env", argument.value)
        self.assertGreater(checked, 0)

    def test_match_format_emits_only_a_literal(self):
        self.assertIn("MATCH", env_flip.MATCH_FMT)
        self.assertNotIn("{{json", env_flip.MATCH_FMT)
        self.assertIn(".Config.Env", env_flip.MATCH_FMT)              # 필드는 쓰되 값은 안 낸다


if __name__ == "__main__":
    unittest.main()
