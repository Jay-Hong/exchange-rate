"""Converge the production WebSocket topic auth stage while dispatch stays OFF.

This tool owns only ``WS_TOPIC_AUTH_STAGE``.  Availability remains the
responsibility of ``topic_flag.py``; mixing both axes in one command would make
an auth-policy change capable of publishing topic data.

Both mutation commands require the dispatcher to be OFF in the env file,
container, Docker inspect metadata, and admin endpoints.  Writes are
key-scoped, byte-preserving, and compare-and-swap guarded.  A handled failure
recreates the service with the stage that was running before the transaction.
An uncatchable SIGKILL can leave file/runtime stage drift, but cannot expose
topic data: the dispatcher stays OFF and ``topic_flag.py on`` rejects drift or
any stage other than ``enforce_authenticated_premium``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import topic_flag  # noqa: E402
from scripts.canary_monitor import (  # noqa: E402
    CANCEL_ON_SIGNALS,
    PRODUCTION_TARGET,
    CollectorError,
    Target,
    atomic_write_bytes,
    backup_path_for,
    container_env,
    make_signal_canceller,
    recreate_and_verify_health,
    run_command,
    wait_until_healthy,
)
from scripts.env_flip import (  # noqa: E402
    COMPOSE_FILE,
    ambient_problems,
    check_env_file_mode,
    check_legacy_backup_modes,
    compose_interpolated_names,
    observe,
    say,
)
from scripts.env_operation_lock import (  # noqa: E402
    EnvOperationLocked,
    env_operation_is_locked,
    env_operation_lock,
    topic_flag_pending_entry_exists,
)


AUTH_STAGE_KEY = b"WS_TOPIC_AUTH_STAGE"
DISPATCHER_KEY = b"TOPIC_DISPATCHER_ENABLED"
FINAL_STAGE = "enforce_authenticated_premium"
COMPATIBILITY_STAGE = "compatibility"
INTERMEDIATE_STAGE = "reject_anonymous_fx"
VALID_STAGES = (COMPATIBILITY_STAGE, INTERMEDIATE_STAGE, FINAL_STAGE)

STATE_FINAL = "FINAL"
STATE_FINAL_ACTIVE = "FINAL_ACTIVE"
STATE_COMPATIBILITY = "COMPATIBILITY"
STATE_INTERMEDIATE = "INTERMEDIATE"
STATE_PENDING_RECREATE = "PENDING_RECREATE"
STATE_UNSAFE_ACTIVE = "UNSAFE_ACTIVE"
STATE_DRIFTED = "DRIFTED"
STATE_AMBIGUOUS = "AMBIGUOUS"
STATE_UNKNOWN = "UNKNOWN"
STABLE_STATES = (
    STATE_FINAL,
    STATE_FINAL_ACTIVE,
    STATE_COMPATIBILITY,
    STATE_INTERMEDIATE,
)

RUNTIME_KEYS = (
    "ENV",
    "TOPIC_DISPATCHER_ENABLED",
    "KRX_CLIENT_DISTRIBUTION_ENABLED",
    "FX_TOPIC_ENABLED",
    "WS_TOPIC_AUTH_STAGE",
)


class Abort(Exception):
    """A fail-closed operational precondition or verification failure."""


def _raw_value(data: bytes, key: bytes, *, required: bool = True) -> Optional[bytes]:
    indexes = topic_flag.key_line_indexes(data, key)
    if len(indexes) != 1:
        if not indexes and not required:
            return None
        raise Abort(f"`{key.decode()}=` 행이 {len(indexes)}개다 — 정확히 1개가 필요하다")
    return topic_flag.read_key_value(data, key)


def _text_value(data: bytes, key: bytes, *, required: bool = True) -> Optional[str]:
    raw = _raw_value(data, key, required=required)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Abort(f"`{key.decode()}` 값이 UTF-8이 아니다") from exc


def _strict_bool(data: bytes, key: bytes) -> bool:
    value = _text_value(data, key)
    if value is None or value.lower() not in ("true", "false"):
        raise Abort(f"`{key.decode()}` 값이 공백 없는 true/false가 아니다")
    return value.lower() == "true"


def _strict_runtime_bool(live: dict, key: str) -> bool:
    value = live.get(key)
    if not isinstance(value, str) or value.lower() not in ("true", "false"):
        raise Abort(f"런타임 {key} 값이 공백 없는 true/false가 아니다")
    return value.lower() == "true"


def file_stage(data: bytes) -> str:
    value = _text_value(data, AUTH_STAGE_KEY, required=False)
    if value is None:
        # app/config.py uses the same implicit default when the env key is absent.
        return COMPATIBILITY_STAGE
    if value not in VALID_STAGES:
        raise Abort(
            f"`{AUTH_STAGE_KEY.decode()}` 값이 허용된 stage가 아니다"
            "(공백 포함 정확 일치 필요)"
        )
    return value


def effective_runtime_stage(value: Optional[str]) -> str:
    """Resolve an observed runtime value using the app's exact default."""
    if value is None:
        return COMPATIBILITY_STAGE
    if value not in VALID_STAGES:
        raise Abort("런타임 WS_TOPIC_AUTH_STAGE가 허용된 exact stage가 아니다")
    return value


def _rewrite_stage(data: bytes, stage: str) -> bytes:
    indexes = topic_flag.key_line_indexes(data, AUTH_STAGE_KEY)
    lines = data.splitlines(keepends=True)
    for index in indexes:
        core = lines[index].rstrip(b"\r\n")
        ending = lines[index][len(core):]
        lines[index] = AUTH_STAGE_KEY + b"=" + stage.encode("ascii") + ending
    return b"".join(lines)


def _append_stage(data: bytes, stage: str) -> bytes:
    """Append the previously absent key without normalizing unrelated bytes."""
    row = AUTH_STAGE_KEY + b"=" + stage.encode("ascii")
    if not data:
        return row + b"\n"

    last_lf = data.rfind(b"\n")
    if last_lf >= 0:
        newline = b"\r\n" if last_lf > 0 and data[last_lf - 1:last_lf] == b"\r" else b"\n"
    elif b"\r" in data:
        newline = b"\r"
    else:
        newline = b"\n"

    if data.endswith((b"\r\n", b"\n", b"\r")):
        return data + row + newline
    return data + newline + row


def plan_stage(data: bytes, stage: str) -> bytes:
    """Return a byte-preserving one-key transition for a known auth stage."""
    if stage not in VALID_STAGES:
        raise Abort(f"알 수 없는 목표 auth stage: {stage!r}")
    current = file_stage(data)
    if current == stage:
        return data
    if not topic_flag.key_line_indexes(data, AUTH_STAGE_KEY):
        return _append_stage(data, stage)
    return _rewrite_stage(data, stage)


def classify_status(*, file_value: Optional[str], file_line_count: Optional[int],
                    runtime_value: Optional[str], runtime_observed: bool,
                    dispatcher_state: str,
                    dispatcher_active: Optional[bool],
                    operation_locked: Optional[bool], activation_pending: Optional[bool]) -> str:
    if operation_locked is True:
        return STATE_AMBIGUOUS
    if operation_locked is None or activation_pending is None:
        return STATE_UNKNOWN
    if activation_pending:
        return STATE_AMBIGUOUS
    if file_line_count is None or not runtime_observed:
        return STATE_UNKNOWN
    if file_line_count not in (0, 1):
        return STATE_AMBIGUOUS
    if file_line_count == 0:
        if file_value is not None:
            return STATE_DRIFTED
        effective_file = COMPATIBILITY_STAGE
    else:
        if file_value not in VALID_STAGES:
            return STATE_DRIFTED
        effective_file = file_value
    try:
        effective_runtime = effective_runtime_stage(runtime_value)
    except Abort:
        return STATE_DRIFTED
    if effective_file != effective_runtime:
        return STATE_PENDING_RECREATE
    if dispatcher_active is True:
        return STATE_FINAL_ACTIVE if effective_file == FINAL_STAGE else STATE_UNSAFE_ACTIVE
    if dispatcher_active is None:
        return STATE_UNKNOWN
    if dispatcher_state != topic_flag.STATE_OFF:
        return STATE_DRIFTED
    if effective_file == FINAL_STAGE:
        return STATE_FINAL
    if effective_file == COMPATIBILITY_STAGE:
        return STATE_COMPATIBILITY
    return STATE_INTERMEDIATE


async def _runtime_values(target: Target) -> dict:
    try:
        return await observe(lambda: container_env(RUNTIME_KEYS, target), "auth-stage runtime env")
    except CollectorError as exc:
        raise Abort(f"런타임 env를 확인할 수 없다: {exc}") from exc


def _require_file_environment(data: bytes) -> None:
    if _text_value(data, b"ENV") != "production":
        raise Abort("파일 ENV가 정확히 'production'이 아니다")
    if _strict_bool(data, DISPATCHER_KEY):
        raise Abort("파일 TOPIC_DISPATCHER_ENABLED가 true — auth stage 전환 전에 OFF가 필요하다")
    if _strict_bool(data, b"KRX_CLIENT_DISTRIBUTION_ENABLED"):
        raise Abort("파일 KRX_CLIENT_DISTRIBUTION_ENABLED가 true — 이 전환에서는 OFF여야 한다")
    if not _strict_bool(data, b"FX_TOPIC_ENABLED"):
        raise Abort("파일 FX_TOPIC_ENABLED가 true가 아니다")
    admin = _text_value(data, b"ADMIN_PASSWORD")
    if admin is None or not admin.strip():
        raise Abort("ADMIN_PASSWORD가 비어 있어 사후 교차 검증을 할 수 없다")
    file_stage(data)


def _require_runtime_environment(live: dict) -> str:
    if live.get("ENV") != "production":
        raise Abort(f"런타임 ENV={live.get('ENV')!r} — production이 아니다")
    if _strict_runtime_bool(live, "TOPIC_DISPATCHER_ENABLED"):
        raise Abort("런타임 TOPIC_DISPATCHER_ENABLED가 true — auth stage 전환 전에 OFF가 필요하다")
    if _strict_runtime_bool(live, "KRX_CLIENT_DISTRIBUTION_ENABLED"):
        raise Abort("런타임 KRX_CLIENT_DISTRIBUTION_ENABLED가 true — 이 전환에서는 OFF여야 한다")
    if not _strict_runtime_bool(live, "FX_TOPIC_ENABLED"):
        raise Abort("런타임 FX_TOPIC_ENABLED가 true가 아니다")
    return effective_runtime_stage(live.get("WS_TOPIC_AUTH_STAGE"))


async def preflight(target: Target, env_path: Path) -> tuple[bytes, str]:
    """Return the exact env snapshot and the stage currently running."""
    await topic_flag.preflight_common(target)
    interpolated = compose_interpolated_names(COMPOSE_FILE.read_text(encoding="utf-8"))
    problems = ambient_problems(dict(os.environ), interpolated)
    if problems:
        raise Abort("; ".join(problems))
    if backup_path_for(env_path).exists():
        raise Abort("다른 도구(canary/env_flip)의 트랜잭션이 진행 중이다")
    if topic_flag_pending_entry_exists(env_path):
        raise Abort("미완료 topic 활성화 marker가 있다 — topic_flag.py off로 먼저 수렴할 것")
    oneoff = (await run_command(
        ["docker", "ps", "--filter", "label=com.docker.compose.oneoff=True", "-q"]
    )).strip()
    if oneoff:
        raise Abort("cron one-shot 컨테이너가 실행 중이다 — 끝난 뒤 다시 실행할 것")

    check_env_file_mode(env_path)
    check_legacy_backup_modes(env_path)
    data = env_path.read_bytes()
    _require_file_environment(data)
    await wait_until_healthy(target, timeout=30.0)
    live = await _runtime_values(target)
    runtime_stage = _require_runtime_environment(live)

    observation = await topic_flag.measure(target, env_path, data)
    state = topic_flag.classify(observation)
    if state != topic_flag.STATE_OFF:
        raise Abort(f"dispatcher 교차 검증이 OFF가 아니다: {state}")
    if effective_runtime_stage(observation.get("auth_stage")) != runtime_stage:
        raise Abort("런타임 auth stage가 두 관측 사이에 바뀌었다")
    return data, runtime_stage


async def _verify(target: Target, env_path: Path, planned: bytes, expected_stage: str) -> dict:
    before = env_path.read_bytes()
    observation = await topic_flag.measure(target, env_path, before)
    live = await _runtime_values(target)
    after = env_path.read_bytes()
    dispatcher_state = topic_flag.classify(observation)
    file_raw = _text_value(before, AUTH_STAGE_KEY, required=False)
    runtime_raw = live.get("WS_TOPIC_AUTH_STAGE")
    observed_stage = (
        effective_runtime_stage(observation.get("auth_stage"))
        if dispatcher_state == topic_flag.STATE_OFF
        else None
    )
    effective_file = file_stage(before)
    effective_runtime = _require_runtime_environment(live)
    ok = (
        before == planned
        and after == planned
        and effective_file == expected_stage
        and dispatcher_state == topic_flag.STATE_OFF
        and observed_stage == expected_stage
        and effective_runtime == expected_stage
    )
    result = {
        "env_matches_plan": before == planned and after == planned,
        "file_stage": effective_file,
        "runtime_stage": effective_runtime,
        "file_stage_explicit": file_raw is not None,
        "runtime_stage_explicit": runtime_raw is not None,
        "dispatcher_state": dispatcher_state,
    }
    if not ok:
        raise Abort(
            "auth stage 사후 검증 실패 — "
            f"env_matches_plan={result['env_matches_plan']} "
            f"file_stage={result['file_stage']!r} "
            f"runtime_stage={result['runtime_stage']!r} "
            f"dispatcher_state={dispatcher_state}"
        )
    return result


async def _apply(target: Target, env_path: Path, original: bytes, planned: bytes,
                 expected_stage: str, summary: dict) -> None:
    current = env_path.read_bytes()
    if current != original:
        raise Abort("사전검사/계획 이후 .env가 변경됐다 — stale 계획을 적용하지 않는다")
    # From this point onward a failure may be ours: either the write or the
    # recreate can have changed production.  Before this point rollback must
    # not overwrite an external edit that merely invalidated our CAS.
    summary["mutation_started"] = True
    if planned != original:
        atomic_write_bytes(env_path, planned)
        summary["wrote"] = True
        if env_path.read_bytes() != planned:
            raise Abort("write 결과가 계획한 bytes와 다르다")
    else:
        say("WRITE", "파일이 이미 목표 stage — 쓰기 생략")

    await recreate_and_verify_health(target)
    summary["recreated"] = True
    summary.update(await _verify(target, env_path, planned, expected_stage))


async def _rollback(target: Target, env_path: Path, planned: bytes,
                    rollback: bytes, rollback_stage: str) -> dict:
    result = {"attempted": True, "ok": False, "stage": rollback_stage}
    try:
        current = env_path.read_bytes()
        if current not in (planned, rollback):
            raise Abort("rollback 전에 .env가 외부 변경됐다 — 덮어쓰지 않는다")
        if current != rollback:
            atomic_write_bytes(env_path, rollback)
        await recreate_and_verify_health(target)
        result.update(await _verify(target, env_path, rollback, rollback_stage))
        result["ok"] = True
    except BaseException as exc:  # noqa: BLE001 - cancellation must still report rollback failure
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


async def transition(stage: str, target: Target = PRODUCTION_TARGET, *,
                     env_path: Optional[Path] = None) -> dict:
    summary = {"command": stage, "ok": False, "wrote": False, "recreated": False}
    try:
        env_path = topic_flag.resolve_env_path(env_path or target.env_file)
        say("S0", "dispatcher OFF + env/runtime preflight")
        original, runtime_stage = await preflight(target, env_path)
        planned = plan_stage(original, stage)
        if planned == original and runtime_stage == stage:
            if env_path.read_bytes() != original:
                raise Abort("사전검사 이후 .env가 변경됐다 — no-op으로 접지 않는다")
            verified = await _verify(target, env_path, original, stage)
            say("STAGE", "파일·런타임이 이미 목표 stage — no-op")
            summary.update({
                "ok": True,
                **verified,
            })
            return summary

        # Roll back to the stage that was actually serving before this
        # transaction, not necessarily the possibly drifted file value.
        rollback = plan_stage(original, runtime_stage)
        await _apply(target, env_path, original, planned, stage, summary)
    except BaseException as exc:  # noqa: BLE001 - signal cancellation also rolls back
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if summary.get("mutation_started"):
            summary["rollback"] = await _rollback(
                target, env_path, planned, rollback, runtime_stage
            )
        return summary
    summary["ok"] = True
    return summary


async def report_status(target: Target = PRODUCTION_TARGET, *,
                        env_path: Optional[Path] = None) -> dict:
    env_path = topic_flag.resolve_env_path(env_path or target.env_file)
    await topic_flag.preflight_common(target)
    data = env_path.read_bytes()
    indexes = topic_flag.key_line_indexes(data, AUTH_STAGE_KEY)
    raw = topic_flag.read_key_value(data, AUTH_STAGE_KEY)
    try:
        file_value = raw.decode("utf-8") if raw is not None else None
    except UnicodeDecodeError:
        file_value = None
    try:
        live = await _runtime_values(target)
        runtime_value = live.get("WS_TOPIC_AUTH_STAGE")
        runtime_observed = True
    except Abort:
        runtime_value = None
        runtime_observed = False
    observation = await topic_flag.measure(target, env_path, data)
    dispatcher_state = topic_flag.classify(observation)
    runtime_signals = (
        observation.get("container"),
        observation.get("inspect"),
        observation.get("dispatcher_endpoint"),
    )
    if all(signal is True for signal in runtime_signals):
        dispatcher_active: Optional[bool] = True
    elif all(signal is False for signal in runtime_signals):
        dispatcher_active = False
    else:
        dispatcher_active = None
    try:
        operation_locked = env_operation_is_locked(env_path)
    except EnvOperationLocked:
        operation_locked = None
    try:
        activation_pending = topic_flag_pending_entry_exists(env_path)
    except EnvOperationLocked:
        activation_pending = None
    state = classify_status(
        file_value=file_value,
        file_line_count=len(indexes),
        runtime_value=runtime_value,
        runtime_observed=runtime_observed,
        dispatcher_state=dispatcher_state,
        dispatcher_active=dispatcher_active,
        operation_locked=operation_locked,
        activation_pending=activation_pending,
    )
    return {
        "command": "status",
        "state": state,
        "file_stage": file_value,
        "runtime_stage": runtime_value,
        "effective_file_stage": (
            COMPATIBILITY_STAGE if not indexes else
            file_value if len(indexes) == 1 and file_value in VALID_STAGES else None
        ),
        "effective_runtime_stage": (
            effective_runtime_stage(runtime_value)
            if runtime_observed and runtime_value in (None, *VALID_STAGES)
            else None
        ),
        "file_stage_explicit": len(indexes) == 1,
        "runtime_stage_explicit": runtime_value is not None if runtime_observed else None,
        "dispatcher_state": dispatcher_state,
        "operation_locked": operation_locked,
        "activation_pending": activation_pending,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="운영 WS_TOPIC_AUTH_STAGE final/compatibility/status",
        allow_abbrev=False,
    )
    parser.add_argument("command", choices=["final", "compatibility", "status"])
    return parser


async def amain(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "status":
        try:
            summary = await report_status()
        except Exception as exc:
            summary = {
                "command": "status",
                "state": STATE_UNKNOWN,
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        stage = FINAL_STAGE if args.command == "final" else COMPATIBILITY_STAGE
        try:
            with env_operation_lock(
                PRODUCTION_TARGET.env_file, f"topic_auth_stage:{args.command}"
            ):
                summary = await transition(stage)
        except EnvOperationLocked as exc:
            summary = {"command": args.command, "ok": False, "error": str(exc)}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if args.command == "status":
        return 0 if summary.get("state") in STABLE_STATES else 2
    return 0 if summary.get("ok") else 1


async def amain_with_signals(argv: Optional[Sequence[str]] = None) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(amain(argv))
    received = {"signum": None}
    handler = make_signal_canceller(task, received)
    installed = []
    for signum in CANCEL_ON_SIGNALS:
        try:
            loop.add_signal_handler(signum, handler, signum)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        result = await task
        if received["signum"] is not None:
            print(f"[SIGNAL] {received['signum']} — rollback 완료 후 종료", file=sys.stderr)
            return 128 + received["signum"]
        return result
    except asyncio.CancelledError:
        if received["signum"] is None:
            raise
        return 128 + received["signum"]
    finally:
        for signum in installed:
            try:
                loop.remove_signal_handler(signum)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain_with_signals()))
