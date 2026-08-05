import asyncio
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts import canary_monitor, env_flip, topic_flag
from scripts.canary_monitor import Target
from scripts.env_operation_lock import (
    EnvOperationLocked,
    env_operation_is_locked,
    env_operation_lock,
    operation_lock_path,
    topic_flag_pending_path,
)


def test_read_only_probe_does_not_create_a_lock_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")

    assert env_operation_is_locked(env) is False
    assert not operation_lock_path(env).exists()


def test_path_aliases_share_one_lock(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(tmp_path, target_is_directory=True)

    assert operation_lock_path(env) == operation_lock_path(alias_dir / ".env")


def test_lock_is_nonblocking_and_reports_the_current_owner(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")

    with env_operation_lock(env, "topic_flag:on"):
        assert env_operation_is_locked(env) is True
        with pytest.raises(EnvOperationLocked) as caught:
            with env_operation_lock(env, "canary_monitor:run"):
                pass

    message = str(caught.value)
    assert "topic_flag:on" in message
    assert "pid=" in message


def test_lock_releases_after_an_exception_and_stays_owner_only(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")

    with pytest.raises(RuntimeError):
        with env_operation_lock(env, "first"):
            raise RuntimeError("injected")

    with env_operation_lock(env, "second") as lock_path:
        assert lock_path == operation_lock_path(env)
        assert lock_path.stat().st_mode & 0o777 == 0o600


def test_symlink_lock_file_is_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    target = tmp_path / "foreign"
    target.write_text("")
    operation_lock_path(env).symlink_to(target)

    with pytest.raises(EnvOperationLocked):
        with env_operation_lock(env, "topic_flag:on"):
            pass


def test_lock_filename_is_gitignored(repo_root=canary_monitor.REPO_ROOT):
    import subprocess

    for env_name in (".env", ".env.prod", ".env.rehearsal"):
        env = repo_root / env_name
        generated = (operation_lock_path(env), topic_flag_pending_path(env))
        for path in generated:
            result = subprocess.run(
                ["git", "check-ignore", "-q", str(path.relative_to(repo_root))],
                cwd=repo_root,
                check=False,
            )
            assert result.returncode == 0, path.name


def test_topic_flag_cli_refuses_while_another_tool_holds_lock(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    target = Target(container="test-app", env_file=env)
    called = AsyncMock()

    with patch.object(topic_flag, "PRODUCTION_TARGET", target), \
         patch.object(topic_flag, "turn_on", called), \
         env_operation_lock(env, "canary_monitor:run"), \
         redirect_stdout(StringIO()):
        code = asyncio.run(topic_flag.amain(["on"]))

    assert code == 1
    called.assert_not_awaited()


def test_env_flip_cli_refuses_while_topic_flag_holds_lock(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ENV=development\n")
    target = Target(container="test-app", env_file=env)
    called = AsyncMock()

    with patch.object(env_flip, "PRODUCTION_TARGET", target), \
         patch.object(env_flip, "run_flip", called), \
         env_operation_lock(env, "topic_flag:on"), \
         redirect_stdout(StringIO()):
        code = asyncio.run(env_flip.amain([]))

    assert code == 1
    called.assert_not_awaited()


def test_canary_recover_is_inside_the_same_lock(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    args = SimpleNamespace(dry_run=False, recover=True, env_file=str(env))
    called = AsyncMock(return_value=0)

    with patch.object(canary_monitor, "_amain", called), \
         env_operation_lock(env, "topic_flag:off"), \
         redirect_stdout(StringIO()):
        code = asyncio.run(canary_monitor._amain_with_operation_lock(args))

    assert code == 1
    called.assert_not_awaited()


def test_canary_dry_run_does_not_contend_for_the_mutation_lock(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    args = SimpleNamespace(dry_run=True, recover=False, env_file=str(env))
    called = AsyncMock(return_value=0)

    with patch.object(canary_monitor, "_amain", called), \
         env_operation_lock(env, "topic_flag:on"):
        code = asyncio.run(canary_monitor._amain_with_operation_lock(args))

    assert code == 0
    called.assert_awaited_once_with(args)


def test_canary_recover_refuses_an_interrupted_topic_activation(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=true\n")
    topic_flag_pending_path(env).touch(mode=0o600)
    args = SimpleNamespace(dry_run=False, recover=True, env_file=str(env))
    called = AsyncMock(return_value=0)

    with patch.object(canary_monitor, "_amain", called), redirect_stdout(StringIO()):
        code = asyncio.run(canary_monitor._amain_with_operation_lock(args))

    assert code == 1
    called.assert_not_awaited()


def test_env_flip_refuses_an_interrupted_topic_activation(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ENV=production\nTOPIC_DISPATCHER_ENABLED=true\n")
    topic_flag_pending_path(env).touch(mode=0o600)
    target = Target(container="test-app", env_file=env)
    called = AsyncMock()

    with patch.object(env_flip, "PRODUCTION_TARGET", target), \
         patch.object(env_flip, "run_flip", called), \
         redirect_stdout(StringIO()):
        code = asyncio.run(env_flip.amain([]))

    assert code == 1
    called.assert_not_awaited()
