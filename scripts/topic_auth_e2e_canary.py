"""Bounded production E2E canary for the final topic authentication stage.

The transaction is intentionally narrower than ``canary_monitor``: no load
generator, no shared env backup, and no permanent activation.  It arms the
independent topic flag watchdog, enables the dispatcher, executes a functional
probe inside the running app container, then converges to OFF in ``finally``.

The watchdog remains the SIGKILL/OOM recovery owner.  A host reboot is the
same explicitly accepted gap documented by ``topic_flag_watchdog.sh``.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
TOPIC_FLAG = REPO_ROOT / "scripts" / "topic_flag.py"
WATCHDOG = REPO_ROOT / "scripts" / "topic_flag_watchdog.sh"
CONTAINER_PROBE = REPO_ROOT / "scripts" / "topic_auth_e2e_probe.py"
CONTAINER_PROBE_PATH = "/app/scripts/topic_auth_e2e_probe.py"

REQUIRED_AUTH_STAGE = "enforce_authenticated_premium"
FIRE_DELAY_SECONDS = 480
# watchdog off(420s) + fresh status(90s) + scheduling margin.
CONTRACT_AFTER_FIRE_SECONDS = 600
WATCHDOG_READY_TIMEOUT_SECONDS = 15
TOPIC_FLAG_ON_TIMEOUT_SECONDS = 450
TOPIC_FLAG_OFF_TIMEOUT_SECONDS = 480
STATUS_TIMEOUT_SECONDS = 100
PROBE_TIMEOUT_SECONDS = 90
PROBE_FIRE_MARGIN_SECONDS = 20
WATCHDOG_HOLD_GRACE_SECONDS = 120
MAX_TOKEN_FILE_BYTES = 16 * 1024


class CanaryFailure(RuntimeError):
    """A secret-free canary orchestration failure."""


def _secure_file_token(path: str) -> str:
    token_path = Path(path)
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(token_path, flags)
    except OSError as exc:
        raise CanaryFailure(
            f"토큰 파일을 읽을 수 없다: {token_path.name} ({type(exc).__name__})"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CanaryFailure(f"토큰 경로가 일반 파일이 아니다: {token_path.name}")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise CanaryFailure(
                f"토큰 파일은 호출자 소유의 0600이어야 한다: {token_path.name}"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_TOKEN_FILE_BYTES + 2)
    finally:
        os.close(fd)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if (
        not raw
        or len(raw) > MAX_TOKEN_FILE_BYTES
        or b"\n" in raw
        or b"\r" in raw
        or b"\x00" in raw
    ):
        raise CanaryFailure(f"토큰 파일은 유계 UTF-8 단일행이어야 한다: {token_path.name}")
    try:
        token = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CanaryFailure(f"토큰 파일이 UTF-8이 아니다: {token_path.name}") from None
    if not token:
        raise CanaryFailure(f"토큰 파일이 비어 있다: {token_path.name}")
    return token


def load_token_pair(args: argparse.Namespace, stream: Any = None) -> tuple[str, str]:
    if args.tokens_stdin:
        stream = sys.stdin if stream is None else stream
        if stream.isatty():
            premium = getpass.getpass("Premium Firebase ID token: ").strip()
            nonpremium = getpass.getpass("Nonpremium Firebase ID token: ").strip()
        else:
            lines = stream.read().splitlines()
            if len(lines) != 2:
                raise CanaryFailure("stdin에는 premium/nonpremium 토큰 두 줄이 정확히 필요하다")
            premium, nonpremium = (line.strip() for line in lines)
    else:
        premium_path, nonpremium_path = args.token_files
        premium = _secure_file_token(premium_path)
        nonpremium = _secure_file_token(nonpremium_path)
    if not premium or not nonpremium:
        raise CanaryFailure("premium/nonpremium 토큰은 비어 있을 수 없다")
    if premium == nonpremium:
        raise CanaryFailure("premium/nonpremium 토큰이 같아 권한 경계를 검증할 수 없다")
    return premium, nonpremium


def _last_json_line(stdout: str) -> dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise CanaryFailure("명령 출력에서 JSON summary를 찾지 못했다")


def _run_json_command(
    command: Sequence[str],
    *,
    timeout: float,
    stdin_text: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    try:
        result = subprocess.run(
            list(command),
            cwd=REPO_ROOT,
            input=stdin_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CanaryFailure(f"{Path(command[0]).name} 명령이 {timeout:.0f}초를 초과했다") from None
    summary = _last_json_line(result.stdout)
    return result.returncode, summary


def run_topic_flag(command: str, timeout: float) -> tuple[int, dict[str, Any]]:
    return _run_json_command(
        [sys.executable, str(TOPIC_FLAG), command], timeout=timeout
    )


def _compose_exec(*args: str) -> list[str]:
    return [
        "docker", "compose", "--env-file", str(REPO_ROOT / ".env"),
        "exec", "-T", "fastapi", *args,
    ]


def verify_container_probe_hash() -> str:
    host_hash = hashlib.sha256(CONTAINER_PROBE.read_bytes()).hexdigest()
    code = (
        "import hashlib,pathlib,sys;"
        "print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())"
    )
    try:
        result = subprocess.run(
            _compose_exec("python", "-c", code, CONTAINER_PROBE_PATH),
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CanaryFailure("컨테이너 probe SHA 확인이 30초를 초과했다") from None
    if result.returncode != 0:
        raise CanaryFailure("컨테이너에 현재 E2E probe가 배포되지 않았다")
    container_hash = result.stdout.strip()
    if container_hash != host_hash:
        raise CanaryFailure("호스트와 컨테이너 E2E probe SHA가 다르다")
    return host_hash


def _new_artifact_paths() -> tuple[Path, Path]:
    log_dir = Path.home() / "logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    watchdog_log = log_dir / f"topic-auth-e2e-{stamp}.watchdog.log"
    artifact = log_dir / f"topic-auth-e2e-{stamp}.json"
    if watchdog_log.exists() or artifact.exists():
        raise CanaryFailure("canary artifact 이름 충돌")
    return watchdog_log, artifact


def arm_watchdog(
    fire_at: int,
    contract_at: int,
    log_path: Path,
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> subprocess.Popen:
    process = popen(
        ["bash", str(WATCHDOG), str(fire_at), str(contract_at), str(log_path)],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = clock() + WATCHDOG_READY_TIMEOUT_SECONDS
    text = ""
    while clock() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if f"READY pid={process.pid} " in text:
                return process
        if process.poll() is not None:
            raise CanaryFailure(
                f"watchdog가 READY 전에 종료했다(exit={process.returncode})"
            )
        sleeper(0.1)
    raise CanaryFailure("watchdog READY를 확인하지 못했다")


def run_container_probe(
    premium_token: str, nonpremium_token: str, timeout: float
) -> dict[str, Any]:
    rc, summary = _run_json_command(
        _compose_exec(
            "python", CONTAINER_PROBE_PATH, "--tokens-stdin"
        ),
        timeout=timeout,
        stdin_text=f"{premium_token}\n{nonpremium_token}\n",
    )
    if rc != 0 or summary.get("ok") is not True:
        detail = summary.get("error") if isinstance(summary.get("error"), str) else "probe 실패"
        raise CanaryFailure(detail)
    return summary


def _require_initial_off(summary: dict[str, Any]) -> None:
    if summary.get("state") != "OFF":
        raise CanaryFailure(f"canary 시작 상태가 OFF가 아니다(state={summary.get('state')!r})")
    for field in ("file_auth_stage", "auth_stage"):
        if summary.get(field) != REQUIRED_AUTH_STAGE:
            raise CanaryFailure(f"{field}가 최종 인증 stage가 아니다")
    if summary.get("fx_topic_enabled") is not True:
        raise CanaryFailure("FX_TOPIC_ENABLED가 true가 아니다")
    if summary.get("activation_pending") is not False:
        raise CanaryFailure("미완료 activation marker가 남아 있다")
    if summary.get("operation_locked") is not False:
        raise CanaryFailure("다른 env 트랜잭션이 진행 중이다")


def _cleanup_off() -> tuple[dict[str, Any], Optional[str]]:
    errors = []
    off_summary: dict[str, Any] = {}
    try:
        rc, off_summary = run_topic_flag("off", TOPIC_FLAG_OFF_TIMEOUT_SECONDS)
        if rc != 0 or off_summary.get("ok") is not True:
            errors.append("topic_flag off 실패")
    except Exception as exc:
        errors.append(f"topic_flag off 예외({type(exc).__name__})")
    try:
        rc, status = run_topic_flag("status", STATUS_TIMEOUT_SECONDS)
        if rc != 0 or status.get("state") != "OFF":
            errors.append(f"OFF 사후 확인 실패(state={status.get('state')!r})")
        off_summary["verified_status"] = status.get("state")
    except Exception as exc:
        errors.append(f"OFF 사후 확인 예외({type(exc).__name__})")
    return off_summary, "; ".join(errors) if errors else None


def execute_canary(
    premium_token: str,
    nonpremium_token: str,
    *,
    epoch: Callable[[], float] = time.time,
) -> dict[str, Any]:
    status_rc, initial = run_topic_flag("status", STATUS_TIMEOUT_SECONDS)
    if status_rc != 0:
        raise CanaryFailure("topic_flag status가 안정 상태를 확인하지 못했다")
    _require_initial_off(initial)
    probe_sha = verify_container_probe_hash()

    watchdog_log, artifact = _new_artifact_paths()
    fire_at = int(epoch()) + FIRE_DELAY_SECONDS
    contract_at = fire_at + CONTRACT_AFTER_FIRE_SECONDS
    watchdog = arm_watchdog(fire_at, contract_at, watchdog_log)

    primary: Optional[BaseException] = None
    probe_summary: Optional[dict[str, Any]] = None
    on_summary: dict[str, Any] = {}
    off_summary: dict[str, Any] = {}
    cleanup_error: Optional[str] = None
    try:
        rc, on_summary = run_topic_flag("on", TOPIC_FLAG_ON_TIMEOUT_SECONDS)
        if rc != 0 or on_summary.get("ok") is not True or on_summary.get("state") != "ON":
            raise CanaryFailure("topic_flag on이 검증된 ON으로 수렴하지 못했다")
        remaining = fire_at - epoch()
        if remaining < PROBE_TIMEOUT_SECONDS + PROBE_FIRE_MARGIN_SECONDS:
            raise CanaryFailure("watchdog FIRE 전 probe 예산이 부족하다")
        probe_summary = run_container_probe(
            premium_token, nonpremium_token, PROBE_TIMEOUT_SECONDS
        )
    except BaseException as exc:
        primary = exc
    finally:
        off_summary, cleanup_error = _cleanup_off()

    result = {
        "ok": primary is None and cleanup_error is None,
        "preflight": {
            "state": initial.get("state"),
            "file_auth_stage": initial.get("file_auth_stage"),
            "auth_stage": initial.get("auth_stage"),
            "fx_topic_enabled": initial.get("fx_topic_enabled"),
        },
        "watchdog": {
            "pid": watchdog.pid,
            "fire_epoch": fire_at,
            "contract_epoch": contract_at,
            # The watchdog intentionally keeps asserting OFF through this
            # point.  A permanent ON before then would be reverted later.
            "permanent_on_not_before_epoch": (
                contract_at + WATCHDOG_HOLD_GRACE_SECONDS
            ),
            "log": str(watchdog_log),
            "done_marker": f"{watchdog_log}.done",
        },
        "probe_sha256": probe_sha,
        "on": {"state": on_summary.get("state"), "ok": on_summary.get("ok")},
        "probe": probe_summary,
        "off": {
            "state": off_summary.get("state"),
            "ok": off_summary.get("ok"),
            "verified_status": off_summary.get("verified_status"),
        },
        "error": _safe_exception(primary) if primary is not None else cleanup_error,
    }
    _write_artifact(artifact, result)
    result["artifact"] = str(artifact)

    if primary is not None:
        if cleanup_error:
            raise CanaryFailure(
                f"canary 실패 후 즉시 OFF 확인도 실패했다: {cleanup_error}; watchdog 유지"
            ) from primary
        raise primary
    if cleanup_error:
        raise CanaryFailure(f"probe 통과 후 OFF 확인 실패: {cleanup_error}; watchdog 유지")
    return result


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(
            (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
        )
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("artifact write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _safe_exception(exc: BaseException) -> str:
    if isinstance(exc, CanaryFailure):
        return str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    return f"unexpected {type(exc).__name__}"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="bounded authenticated topic E2E canary",
        allow_abbrev=False,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--tokens-stdin",
        action="store_true",
        help="premium/nonpremium Firebase token을 두 줄로 입력",
    )
    source.add_argument(
        "--token-files",
        nargs=2,
        metavar=("PREMIUM_0600", "NONPREMIUM_0600"),
        help="각각 chmod 600인 토큰 파일",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        premium, nonpremium = load_token_pair(args)
        result = execute_canary(premium, nonpremium)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted; watchdog가 OFF를 강제한다"}, ensure_ascii=False))
        return 130
    except CanaryFailure as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": f"unexpected {type(exc).__name__}; watchdog가 OFF를 강제한다",
        }, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
