"""W canary **자동 중단 watchdog + 실행기** — 중단 조건을 *문서*가 아니라 *실행 경로*에 둔다.

⛔ **왜 필요한가.** `ws_auth_load.evaluate_server_abort()` 는 순수 함수로만 있었고 **호출자가
없었다**. 합의한 "즉시 중단 6종" 중 자동화된 것은 클라 timeout/error 둘뿐이었다.
"운영자가 스냅샷을 넣어 판정한다"는 8 동시 연결이 도는 7분 창에서 **실행 가능한 절차가
아니다** — 사람이 1초 주기로 5개 endpoint 를 볼 수 없다.

## 이 모듈이 지키는 것

⛔ **fail-closed 수집.** subprocess 종료코드·stderr·HTTP 상태·JSON 형식 중 하나라도 어긋나면
   **중단**한다. 한때 `_run` 이 종료코드와 stderr 를 버려서 401·500·docker 오류가 전부 `{}` 로
   접혔다 — 그러면 "지표가 깨끗하다"와 "지표를 못 읽었다"가 **구분되지 않는다**.
⛔ **모든 외부 호출에 timeout.** `docker compose exec` 나 `curl` 하나가 멈추면 watchdog 이 함께
   멈춰 즉시 중단도 rollback 도 영영 실행되지 않는다.
⛔ **rollback 은 `finally`**, 그리고 **부하 프로세스의 최종 사망까지 확인**한다. stop 이 실패해도
   rollback 은 돌고, rollback 이 실패해도 kill 은 돈다 — 각각 제 `try` 를 가진다.
⛔ **실패를 숨기지 않는다.** cleanup 실패는 outcome 에 남고 exit code 를 실패로 만든다.
⛔ **비밀번호가 어떤 프로세스의 argv 에도 들어가지 않는다.** 한때 컨테이너 셸에서
   `-u admin:"$ADMIN_PASSWORD"` 로 확장했는데, 그러면 **최종 `curl` 의 argv 에는 실제 값이
   들어간다**(`ps` 로 보인다). 지금은 컨테이너 안 Python 이 env 를 직접 읽으므로 어떤 argv
   에도 값이 없다. 토큰도 같은 이유로 자식 stdin 으로만 준다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ws_auth_load import (  # noqa: E402
    PHASES, evaluate_server_abort, load_id_token, maximum_plan_seconds, total_plan_seconds,
)

BROADCAST_STALL_SECONDS = 30.0
DEFAULT_POLL_INTERVAL = 1.0
COMMAND_TIMEOUT_SECONDS = 8.0          # 개별 docker/exec 호출 (운영 기본)
#: ⚠️ **정책값이다.** 목적은 "호출 하나가 멈춰 watchdog 이 함께 얼지 않게" 하는 것뿐이라,
#: 느린 환경에서는 키울 수 있어야 한다 — Apple Silicon 에뮬레이션 + `cpus: 0.5` 리허설
#: 스택에서는 재기동 직후 창의 `docker compose exec` 가 8초를 넘겼다(실측). 운영 값은 그대로.
_command_timeout = COMMAND_TIMEOUT_SECONDS
RECREATE_TIMEOUT_SECONDS = 180.0       # 컨테이너 재기동은 느리다
STOP_GRACE_SECONDS = 5.0
HEALTH_POLL_SECONDS = 2.0       # 재기동 뒤 앱이 listen 할 때까지의 폴링 간격
REPO_ROOT = Path(__file__).resolve().parent.parent


PRODUCTION_CONTAINER = "exchange-rate-app"


@dataclass(frozen=True)
class Target:
    """이 실행기가 **건드려도 되는 스택**. ⛔ 하드코딩하지 않는다.

    한때 `docker compose ...` 와 `exchange-rate-app` 이 코드에 박혀 있었다. 그 상태로
    "EC2 에 별도 compose 프로젝트를 띄워 리허설한다"를 제안했는데 **위험했다** —
    `docker-compose.yml` 은 세 서비스 모두 `container_name` 이 **고정**이고 redis 6379 ·
    nginx 80/443 을 publish 하므로 `-p` 로 띄워도 **충돌**하고, monitor 는 그와 무관하게
    **운영 컨테이너와 운영 `.env` 를 재생성**했을 것이다.
    """

    container: str = PRODUCTION_CONTAINER
    project: Optional[str] = None
    compose_file: Optional[str] = None
    env_file: Path = REPO_ROOT / ".env"

    @property
    def is_production(self) -> bool:
        return self.container == PRODUCTION_CONTAINER

    def compose(self, *args: str) -> list[str]:
        # Compose interpolation도 실행기가 canary flag를 수정하는 파일을 써야 한다. 이 옵션이
        # 없으면 rehearsal target도 리포 루트 `.env`의 REDIS_PASSWORD 등을 조용히 읽는다.
        command = ["docker", "compose", "--env-file", str(self.env_file)]
        if self.project:
            command += ["-p", self.project]
        if self.compose_file:
            command += ["-f", self.compose_file]
        return command + list(args)

    def inspect(self, fmt: str) -> list[str]:
        return ["docker", "inspect", self.container, "--format", fmt]


PRODUCTION_TARGET = Target()


class CollectorError(Exception):
    """지표를 **읽지 못했다**. ⚠️ "지표가 깨끗하다"와 절대 같게 취급하지 않는다."""


# ── admin 조회 (비밀번호를 argv 에 넣지 않는다) ─────────────────────────────


ADMIN_FETCH_PY = (
    "import json,os,sys,urllib.request,base64\n"
    "cred=base64.b64encode(('admin:'+os.environ['ADMIN_PASSWORD']).encode()).decode()\n"
    "req=urllib.request.Request('http://localhost:8000'+sys.argv[1],\n"
    "                           headers={'Authorization':'Basic '+cred})\n"
    "try:\n"
    "    with urllib.request.urlopen(req,timeout=5) as r: body,code=r.read().decode(),r.status\n"
    "except urllib.error.HTTPError as e: body,code=e.read().decode(),e.code\n"
    "sys.stdout.write(body+'\\n'+str(code))\n"
)


def admin_fetch_command(path: str, target: Target = PRODUCTION_TARGET) -> list[str]:
    """admin endpoint 조회 — **컨테이너 안 Python 이 env 를 직접 읽는다**.

    ⛔ 셸 경유 curl 을 쓰지 않는 이유 두 가지:
       1. `-u admin:"$ADMIN_PASSWORD"` 는 셸이 확장한 **실제 값이 curl argv 에 남는다**(`ps`).
       2. heredoc `curl --config -` 로 피해도 **비밀번호에 `"`·`\\`·개행이 있으면 config 문법이
          깨진다** — 조용히 401 이 되고, 그건 "지표가 깨끗하다"로 오독되기 쉽다.
       여기서는 값이 **프로세스 메모리 안에서만** 쓰이고 어떤 argv 에도, 어떤 인용 규칙에도
       노출되지 않는다.
    ⚠️ 출력 형식은 `body\\n<status>` — `parse_curl_response` 가 그대로 받는다.
    """
    return target.compose("exec", "-T", "fastapi", "python", "-c", ADMIN_FETCH_PY, path)


def parse_curl_response(raw: str) -> dict:
    """`body\\n<status>` 를 가른다. **non-2xx · 형식 오류는 전부 실패**다."""
    text = (raw or "").rstrip("\n")
    body, _, status = text.rpartition("\n")
    if not status.strip().isdigit():
        raise CollectorError("HTTP 상태 코드를 읽지 못했다")
    code = int(status.strip())
    if not 200 <= code < 300:
        raise CollectorError(f"HTTP {code}")
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise CollectorError(f"JSON 파싱 실패: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CollectorError("JSON 객체가 아니다")
    return parsed


def set_command_timeout(seconds: float) -> None:
    """⛔ 유한하고 1초 이상만 — 0/NaN 이면 모든 호출이 즉시 실패해 canary 가 시작조차 못 한다."""
    global _command_timeout
    value = float(seconds)
    if not (value == value) or value in (float("inf"), float("-inf")) or value < 1.0:
        raise ValueError(f"command timeout 은 유한하고 1초 이상이어야 한다: {seconds!r}")
    _command_timeout = value


async def run_command(command: Sequence[str], *, timeout: Optional[float] = None,
                      cwd: Optional[Path] = None) -> str:
    """⛔ 종료코드·stderr·timeout 을 **전부** 본다. 하나라도 어긋나면 올린다."""
    timeout = _command_timeout if timeout is None else timeout
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd or REPO_ROOT),
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        process.kill()
        await process.wait()
        raise CollectorError(f"{timeout}초 안에 끝나지 않았다: {command[0]}") from exc
    if process.returncode != 0:
        detail = err.decode("utf-8", "replace").strip()[:200]
        raise CollectorError(f"exit={process.returncode} {command[0]}: {detail}")
    return out.decode("utf-8", "replace")


# ── 스냅샷 · 판정 ───────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    health_ok: bool = True
    injected_health_failure: bool = False
    container_started_at: Optional[str] = None
    new_error_count: int = 0
    broadcast_age_seconds: Optional[float] = 0.0
    auth_running: bool = True
    probe_enabled: bool = True
    probe_running: bool = True
    auth_metrics: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)


def evaluate_snapshot_abort(snapshot: Snapshot, *, baseline_started_at: Optional[str],
                            previous_probe_outstanding: int = 0) -> list[str]:
    """합의한 즉시 중단 조건과 그 조건을 재는 구성요소의 생존 상태를 판정한다."""
    reasons: list[str] = []
    if not snapshot.health_ok:
        reasons.append("health 실패 (injected)" if snapshot.injected_health_failure
                       else "health 실패")
    if (baseline_started_at is not None
            and snapshot.container_started_at is not None
            and snapshot.container_started_at != baseline_started_at):
        reasons.append("컨테이너 재시작")
    if snapshot.new_error_count > 0:
        reasons.append(f"신규 ERROR/traceback {snapshot.new_error_count}건")
    age = snapshot.broadcast_age_seconds
    if age is None:
        # ⛔ "한 번도 broadcast 하지 않음"을 0 으로 접으면 **정지를 정상으로 읽는다**.
        reasons.append("legacy broadcast 관측 불가(age 없음)")
    elif age >= BROADCAST_STALL_SECONDS:
        reasons.append(f"legacy broadcast {age:.0f}초 정지")
    if not snapshot.auth_running:
        reasons.append("auth executor 정지")
    if not snapshot.probe_enabled:
        reasons.append("default executor probe disabled")
    if not snapshot.probe_running:
        reasons.append("default executor probe 정지")
    # ⛔ 서버 지표 판정은 **재구현하지 않는다** — 부하 도구와 같은 함수를 쓴다.
    reasons.extend(evaluate_server_abort(snapshot.auth_metrics, snapshot.probe,
                                         previous_probe_outstanding))
    return reasons


async def collect_from_server(*, timeout: Optional[float] = None,
                              target: Target = PRODUCTION_TARGET) -> Snapshot:
    """운영 수집기 — **서버에서** 실행한다. 조회 실패는 전부 `CollectorError`."""
    health = parse_curl_response(await run_command(admin_fetch_command("/health", target), timeout=timeout))
    heartbeat = parse_curl_response(
        await run_command(admin_fetch_command("/admin/api/broadcast-heartbeat", target), timeout=timeout))
    auth = parse_curl_response(
        await run_command(admin_fetch_command("/admin/api/ws-auth-executor-metrics", target), timeout=timeout))
    probe = parse_curl_response(
        await run_command(admin_fetch_command("/admin/api/default-executor-probe", target), timeout=timeout))
    started = (await run_command(
        target.inspect("{{.State.StartedAt}}"), timeout=timeout)).strip()
    logs = await run_command(target.compose("logs", "fastapi", "--since", "5s"),
                             timeout=timeout)

    for name, payload in (("heartbeat", heartbeat), ("auth", auth), ("probe", probe)):
        if payload.get("error"):
            raise CollectorError(f"{name} endpoint 가 오류를 보고했다: {payload['error']}")

    age = heartbeat.get("age_seconds")
    return Snapshot(
        health_ok=health.get("status") == "healthy",
        container_started_at=started or None,
        new_error_count=sum(1 for line in logs.splitlines()
                            if "ERROR" in line or "Traceback" in line),
        broadcast_age_seconds=float(age) if isinstance(age, (int, float)) else None,
        auth_running=auth.get("running") is True,
        probe_enabled=probe.get("enabled") is True,
        probe_running=probe.get("running") is True,
        auth_metrics=auth.get("metrics") if isinstance(auth.get("metrics"), dict) else {},
        probe=probe.get("metrics") if isinstance(probe.get("metrics"), dict) else {},
    )


# ── 부하 자식 프로세스 ──────────────────────────────────────────────────────


class LoadHandle(Protocol):
    async def wait(self) -> int: ...
    async def stop(self) -> None: ...
    async def kill(self) -> None: ...
    def is_alive(self) -> bool: ...


@dataclass
class LoadProcess:
    """`ws_auth_load.py` 자식. ⛔ **토큰은 stdin 으로만** 넘어간다(argv 노출 금지)."""

    process: Any
    grace_seconds: float = STOP_GRACE_SECONDS

    async def wait(self) -> int:
        return await self.process.wait()

    def is_alive(self) -> bool:
        return self.process.returncode is None

    async def stop(self) -> None:
        """terminate → **유계 대기** → kill → 종료 확인."""
        if not self.is_alive():
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=self.grace_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            await self.kill()

    async def kill(self) -> None:
        if not self.is_alive():
            return
        self.process.kill()
        await self.process.wait()


async def start_load_process(*, token: str, url: str,
                             script: Path = REPO_ROOT / "scripts" / "ws_auth_load.py",
                             python: str = sys.executable) -> LoadProcess:
    process = await asyncio.create_subprocess_exec(
        python, str(script), "--token-stdin", "--url", url,
        stdin=asyncio.subprocess.PIPE, cwd=str(REPO_ROOT),
    )
    handle = LoadProcess(process)
    try:
        if process.stdin is None:
            raise CollectorError("부하 프로세스 stdin pipe 가 없다")
        process.stdin.write(token.encode("utf-8"))     # ⛔ argv 가 아니라 stdin
        await process.stdin.drain()
        process.stdin.close()
    except BaseException:
        # stdin 전달이 실패하면 호출자는 handle 을 받지 못한다. 여기서 회수하지 않으면
        # 바깥 finally 도 모르는 부하 자식이 canary 뒤까지 살아남는다.
        if process.stdin is not None:
            process.stdin.close()
        try:
            await handle.stop()
        finally:
            await handle.kill()
        raise
    return handle


# ── flag rollback ───────────────────────────────────────────────────────────


#: canary 창 동안 강제할 env. ⛔ KRX 배포는 **명시적으로 false** — "원래 꺼져 있겠지"로
#: 두면 누가 켜 둔 상태에서 유료 데이터가 새는 창이 된다.
CANARY_ENV: dict[str, str] = {
    "TOPIC_DISPATCHER_ENABLED": "true",
    "DEFAULT_EXECUTOR_PROBE_ENABLED": "true",
    "DEFAULT_EXECUTOR_PROBE_INTERVAL_SECONDS": "1",
    "WS_AUTH_EXECUTOR_LOG_TIMINGS": "true",
    "KRX_CLIENT_DISTRIBUTION_ENABLED": "false",
}
EXPECTED_AUTH_WORKERS = 4
PREFLIGHT_ATTEMPTS = 10
PREFLIGHT_RETRY_SECONDS = 1.0


def read_env_values(text: str, keys) -> dict[str, Optional[str]]:
    """⚠️ **키 부재와 빈 값은 다르다.** 부재를 `""` 로 접으면 복원 때 없던 키가 생긴다."""
    found: dict[str, Optional[str]] = {key: None for key in keys}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in found:
            found[key.strip()] = value
    return found


def apply_env_values(text: str, changes: dict[str, Optional[str]]) -> str:
    """`None` 은 **키 제거**다(원래 없던 키를 되살리지 않기 위해).

    ⛔ `sed 's/^KEY=.*/KEY=false/'` 로 하면 **키가 없을 때 아무 일도 안 일어난다** — 그런데
       성공한 것처럼 보인다. 여기서는 부재 시 append 하고, 제거는 실제로 지운다.
    """
    lines = text.splitlines()
    remaining = dict(changes)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else None
        if key in changes and not stripped.startswith("#"):
            # 첫 줄만 새 값으로 바꾸고 같은 키의 중복 줄은 제거한다. 뒤쪽 중복 키가 남으면
            # dotenv 구현에 따라 canary 값이나 복원값을 다시 덮을 수 있다.
            if key in remaining:
                value = remaining.pop(key)
                if value is not None:
                    out.append(f"{key}={value}")
            continue                                   # None 또는 중복 → 줄 자체를 지운다
        out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def atomic_write_bytes(path: Path, data: bytes, *, mode: Optional[int] = None) -> None:
    """같은 디렉터리 임시 파일을 fsync 한 뒤 bytes 그대로 교체한다."""
    path = Path(path)
    if mode is None:
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=TEMP_SUFFIX,
                                     dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def atomic_write_text(path: Path, text: str, *, mode: Optional[int] = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def validate_canary_start_env(values: dict[str, Optional[str]]) -> list[str]:
    """이미 활성화된 release flag를 이 도구가 canary 상태로 오인하지 않게 한다."""
    problems: list[str] = []
    for key in ("TOPIC_DISPATCHER_ENABLED", "KRX_CLIENT_DISTRIBUTION_ENABLED"):
        value = values.get(key)
        if value is not None and value.strip().lower() != "false":
            problems.append(f"{key}는 canary 시작 전에 false 또는 미설정이어야 한다: {value!r}")
    return problems


BACKUP_SUFFIX = ".canary-backup"
#: ⛔ 원자적 write 의 임시 파일에 **고정 접미사**를 붙인다. 그 temp 는 대상 env 파일의 내용
#: (= 운영 secret 전체)을 담는데, 이름이 `..env.<random>` 이면 `.gitignore` 가 못 잡아
#: `os.replace` 전에 프로세스가 죽는 순간 **`git add -A` 대상**이 된다.
#: 접미사를 고정해 두면 대상 파일 이름이 무엇이든 **한 규칙**(`*.canary-tmp`)으로 덮인다.
TEMP_SUFFIX = ".canary-tmp"


def backup_path_for(env_path: Path) -> Path:
    return Path(env_path).with_name(Path(env_path).name + BACKUP_SUFFIX)


def create_env_backup(env_path: Path) -> Path:
    """env 를 건드리기 **전에** 원본 전체를 durable backup 한다.

    ⛔ **키 단위 복원으로는 부족하다.** 한때 수동 절차로 `sed 's/^KEY=.*/KEY=false/'` 를 적어
    뒀는데, 그건 (1) **키가 없으면 아무 일도 안 하면서 성공처럼 보이고** (2) 원자적이지 않으며
    (3) canary 가 바꾼 **나머지 4개 키를 되돌리지 않는다**. 자동 경로와 수동 경로가 서로 다른
    기준으로 복원하면 그 둘이 어긋나는 순간을 아무도 못 잡는다 — 그래서 **파일 전체 backup
    하나**로 통일한다.
    ⛔ backup 이 이미 있으면 **이전 canary 가 복구되지 않은 것**이므로 시작을 거부한다.
    """
    env_path = Path(env_path)
    backup = backup_path_for(env_path)
    data = env_path.read_bytes()

    # ⛔ `exists()` 뒤 `os.replace()`는 TOCTOU다. 두 실행이 동시에 absence를 본 뒤 둘 다
    # backup을 덮을 수 있고, 늦은 쪽은 이미 활성화된 env를 "원본"으로 저장할 수 있다.
    # 완전히 fsync한 임시 inode를 hard-link로 게시하면 목적지가 이미 있을 때 원자적으로 실패한다.
    # temp도 `*.canary-backup*` gitignore 범위 안에 둔다. link 게시 뒤 프로세스가 죽어
    # unlink를 못 해도 secret-bearing temp가 `git status`에 나타나지 않아야 한다.
    fd, temporary = tempfile.mkstemp(prefix=f"{backup.name}.", suffix=TEMP_SUFFIX,
                                     dir=backup.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, backup)
        except FileExistsError as exc:
            raise CollectorError(
                f"이전 canary 의 backup 이 남아 있다: {backup} — 먼저 `--recover` 로 복원할 것"
            ) from exc
        directory_fd = os.open(backup.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return backup
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class EnvRestorer:
    """**backup 파일 전체**를 원자 복원한다. 자동 cleanup 과 `--recover` 가 같은 경로를 쓴다.

    ⚠️ 여러 번 불려도 한 번만 일한다(monitor 의 cleanup 과 바깥 `finally` 가 둘 다 부른다).
    """

    def __init__(self, env_path: Path, target: Target = PRODUCTION_TARGET):
        self._env_path = Path(env_path)
        self._target = target
        self.done = False

    async def restore(self) -> None:
        if self.done:
            return
        backup = backup_path_for(self._env_path)
        if not backup.exists():
            raise CollectorError(f"복원할 canary backup 이 없다: {backup}")
        original = backup.read_bytes()
        atomic_write_bytes(self._env_path, original)
        await recreate_and_verify_health(self._target)
        current = self._env_path.read_bytes()
        if current != original:
            raise CollectorError("env 가 원본과 바이트 동일하게 복원되지 않았다")
        # ⛔ **검증이 끝난 뒤에만** backup 을 지운다 — 먼저 지우면 재시도 수단이 사라진다.
        backup.unlink()
        self.done = True


async def wait_until_healthy(target: Target = PRODUCTION_TARGET, *,
                             timeout: float = RECREATE_TIMEOUT_SECONDS,
                             poll_seconds: float = HEALTH_POLL_SECONDS,
                             clock: Callable[[], float] = time.monotonic) -> None:
    """⛔ **재기동 직후 즉시 조회하면 안 된다.** `docker compose up -d` 는 컨테이너가 *시작*되면
    반환하지만 앱은 아직 listen 하지 않는다 — 그 창에서 조회하면 연결 거부가 나고, 그걸
    "health 실패"로 접으면 **정상 재기동을 장애로 판정**한다(리허설에서 실제로 그렇게 죽었다).
    운영에서도 같다: rollback 의 마지막 확인이 그 이유로 실패하면 "되돌리지 못했다"로 보고된다.

    ⚠️ 그렇다고 무한정 기다리지 않는다 — **유계**이고, 시간 안에 정상이 되지 않으면 올린다
    (기동 중 연결 거부와 **영구 장애**를 시간으로 가른다).
    """
    deadline = clock() + timeout
    last = "확인 시도 없음"
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise CollectorError(
                f"재기동 후 {timeout:.0f}초 안에 health 가 정상이 되지 않았다: {last}")
        try:
            health = parse_curl_response(await run_command(
                admin_fetch_command("/health", target),
                # 전체 health 창보다 개별 docker 호출이 더 오래 기다리면 `timeout` 계약이
                # 거짓이 된다. 마지막 시도는 남은 예산까지만 허용한다.
                timeout=min(_command_timeout, remaining)))
            if health.get("status") == "healthy":
                return
            last = f"status={health.get('status')!r}"
        except CollectorError as exc:
            last = str(exc)                       # 기동 중이면 연결 거부가 **정상**이다
        remaining = deadline - clock()
        if remaining <= 0:
            raise CollectorError(f"재기동 후 {timeout:.0f}초 안에 health 가 정상이 되지 않았다: {last}")
        await asyncio.sleep(min(poll_seconds, remaining))


async def recreate_and_verify_health(target: Target = PRODUCTION_TARGET) -> None:
    """⛔ env 는 컨테이너 **재생성** 시에만 다시 읽힌다(`restart` 는 반영 안 된다).
    확인 없이 끝내면 "적용했다"는 보고만 남고 실제로는 구 값으로 돈다."""
    await run_command(target.compose("up", "-d", "--force-recreate", "fastapi"),
                      timeout=RECREATE_TIMEOUT_SECONDS)
    await wait_until_healthy(target)


CONTAINER_ENV_PY = (
    "import json,os,sys\n"
    "sys.stdout.write(json.dumps({k: os.environ.get(k) for k in sys.argv[1:]})+'\\n200')\n"
)


async def container_env(keys: Sequence[str], target: Target = PRODUCTION_TARGET) -> dict:
    return parse_curl_response(await run_command(
        target.compose("exec", "-T", "fastapi", "python", "-c", CONTAINER_ENV_PY, *keys)))


async def check_target_identity(target: Target) -> list[str]:
    """⛔ **이 컨테이너가 정말 그 target 의 것인가.** 확인 못 하면 fail-closed 로 거부한다 —
    확인 없이 진행하면 리허설이 **운영 스택을 재생성**할 수 있다(실제로 가능했던 경로)."""
    try:
        raw = await run_command(target.inspect("{{json .Config.Labels}}"))
        labels = json.loads(raw)
    except (CollectorError, TypeError, ValueError) as exc:
        return [f"target 컨테이너를 확인할 수 없다: {exc}"]
    if not isinstance(labels, dict):
        return ["target 컨테이너 label이 JSON 객체가 아니다"]
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    problems: list[str] = []
    if target.project and project != target.project:
        problems.append(f"컨테이너 project={project!r} 가 target project={target.project!r} 와 다르다")
    if not project:
        problems.append("컨테이너에 compose project label 이 없다")
    if service != "fastapi":
        problems.append(f"컨테이너 service={service!r} — fastapi가 아니다")
    return problems


async def check_ws_reachable(url: str, *, timeout: float = 5.0) -> list[str]:
    """⛔ 기본 URL 을 **조용히 신뢰하지 않는다.** compose 의 fastapi 는 `expose` 만 있고 host
    port 를 publish 하지 않는다 — 호스트에서 `ws://localhost:8000/ws` 는 닿지 않는다.
    그 상태로 리허설하면 연결 실패 후 **rollback 만 검증**하고 순서는 증명하지 못한다."""
    try:
        import websockets
    except ImportError:
        return ["websockets 패키지가 없어 도달성을 확인할 수 없다"]
    try:
        async with await asyncio.wait_for(websockets.connect(url), timeout=timeout):
            return []
    except Exception as exc:                                  # noqa: BLE001
        return [f"부하 URL 에 연결할 수 없다({type(exc).__name__}): {url}"]


async def preflight(target: Target = PRODUCTION_TARGET, *,
                    url: Optional[str] = None) -> list[str]:
    """⛔ **부하를 만들기 전에** 창의 전제가 실제로 적용됐는지 본다.

    이게 없으면 probe 가 꺼진 채로 canary 가 돌아 **빈 지표를 성공으로** 읽는다 — 이 트랙에서
    반복된 false green 의 정확한 형태다. 하나라도 어긋나면 부하 프로세스는 **0건** 생성된다.
    """
    problems: list[str] = await check_target_identity(target)
    env = await container_env(list(CANARY_ENV), target)
    for key, expected in CANARY_ENV.items():
        actual = env.get(key)
        if (actual or "").strip().lower() != expected:
            problems.append(f"{key}={actual!r} (기대 {expected!r})")

    probe = parse_curl_response(await run_command(
        admin_fetch_command("/admin/api/default-executor-probe", target)))
    if not probe.get("enabled"):
        problems.append("probe enabled=false")
    if not probe.get("running"):
        problems.append("probe running=false — flag 만 켜지고 task 가 안 돈다")

    auth = parse_curl_response(await run_command(
        admin_fetch_command("/admin/api/ws-auth-executor-metrics", target)))
    if not auth.get("running"):
        problems.append("auth executor running=false")
    metrics = auth.get("metrics") if isinstance(auth.get("metrics"), dict) else {}
    if metrics.get("max_workers") != EXPECTED_AUTH_WORKERS:
        problems.append(f"auth max_workers={metrics.get('max_workers')} (기대 {EXPECTED_AUTH_WORKERS})")

    heartbeat = parse_curl_response(await run_command(
        admin_fetch_command("/admin/api/broadcast-heartbeat", target)))
    age = heartbeat.get("age_seconds")
    if not isinstance(age, (int, float)) or age >= BROADCAST_STALL_SECONDS:
        problems.append(f"broadcast heartbeat 이 신선하지 않다: {age}")
    if url:
        problems.extend(await check_ws_reachable(url))
    return problems


async def wait_for_preflight(target: Target = PRODUCTION_TARGET, *, url: Optional[str] = None,
                             attempts: int = PREFLIGHT_ATTEMPTS,
                             retry_seconds: float = PREFLIGHT_RETRY_SECONDS) -> list[str]:
    """재생성 직후 scheduler/probe의 첫 tick만 유계로 기다린다.

    마지막 결과가 여전히 실패면 그대로 반환한다. 수집 오류는 재시도하지 않고 즉시 전파해
    "아직 준비 중"과 "관측 불가"를 섞지 않는다.
    """
    if attempts < 1:
        raise ValueError("preflight attempts는 1 이상이어야 한다")
    problems: list[str] = []
    for attempt in range(attempts):
        problems = await preflight(target, url=url)
        if not problems:
            return []
        if attempt + 1 < attempts:
            await asyncio.sleep(retry_seconds)
    return problems


# ── 실행 (중단 → 자식 종료 → rollback → 사망 확인) ─────────────────────────


@dataclass
class CanaryOutcome:
    aborted: bool
    reasons: list[str]
    polls: int
    load_stopped: bool
    rollback_done: bool
    load_alive_after_cleanup: bool
    cleanup_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.aborted or self.cleanup_errors or self.load_alive_after_cleanup)


async def run_monitored_canary(
    *,
    collect: Callable[[], Awaitable[Snapshot]],
    load: LoadHandle,
    rollback: Callable[[], Awaitable[None]],
    baseline_started_at: Optional[str] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    minimum_clean_exit_seconds: float = 0.0,
    load_started_at: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    emit: Callable[[str], None] = print,
) -> CanaryOutcome:
    wait_task = asyncio.ensure_future(load.wait())
    # 자식은 monitor 진입 전에 이미 spawn된다. 여기서 새로 시각을 잡으면 정상 420초 계획도
    # spawn -> monitor 사이만큼 짧게 보여 조기 종료로 오판할 수 있다.
    started_at = clock() if load_started_at is None else load_started_at
    reasons: list[str] = []
    cleanup_errors: list[str] = []
    polls = 0
    previous_outstanding = 0
    load_stopped = rollback_done = False

    try:
        while True:
            if wait_task.done():
                try:
                    code = wait_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:                      # noqa: BLE001
                    reasons.append(f"부하 프로세스 예외: {type(exc).__name__}: {exc}")
                else:
                    # ⛔ 정상 종료(0)만 계획 완주다. 나머지는 **중단**이다 — 한때
                    #    `return_exceptions=True` 로 삼켜 비정상 종료가 성공으로 보였다.
                    if code != 0:
                        reasons.append(f"부하 프로세스 비정상 종료 exit={code}")
                    elif (elapsed := clock() - started_at) < minimum_clean_exit_seconds:
                        reasons.append(
                            "부하 프로세스 조기 정상 종료 "
                            f"({elapsed:.1f}s < {minimum_clean_exit_seconds:.1f}s)")
                    else:
                        # ⛔ 종료를 먼저 보고 바로 break 하면 직전 poll 뒤에 생긴 ERROR/포화가
                        # 영원히 빠진다. 정상 종료를 받아들이기 전에 마지막 서버 상태를 읽는다.
                        try:
                            snapshot = await collect()
                        except Exception as exc:              # noqa: BLE001 — fail-closed
                            reasons.append(f"최종 지표 수집 실패({type(exc).__name__}): {exc}")
                        else:
                            polls += 1
                            reasons = evaluate_snapshot_abort(
                                snapshot, baseline_started_at=baseline_started_at,
                                previous_probe_outstanding=previous_outstanding,
                            )
                break
            try:
                snapshot = await collect()
            except Exception as exc:                          # noqa: BLE001 — fail-closed
                reasons.append(f"지표 수집 실패({type(exc).__name__}): {exc}")
                break
            polls += 1
            reasons = evaluate_snapshot_abort(
                snapshot, baseline_started_at=baseline_started_at,
                previous_probe_outstanding=previous_outstanding,
            )
            previous_outstanding = int(snapshot.probe.get("outstanding", 0) or 0)
            if reasons:
                emit(f"[ABORT] {reasons}")
                break
            await asyncio.sleep(poll_interval)
    finally:
        # ⛔ 셋 다 **각자의 try** 를 가진다. 하나가 실패해도 나머지가 건너뛰어지면 안 된다 —
        #    특히 마지막 "사망 확인"은 앞의 둘이 모두 실패해도 반드시 돌아야 한다.
        try:
            await load.stop()
            load_stopped = True
        except BaseException as exc:                          # noqa: BLE001
            cleanup_errors.append(f"stop_load 실패: {type(exc).__name__}: {exc}")
        try:
            await rollback()
            rollback_done = True
        except BaseException as exc:                          # noqa: BLE001
            cleanup_errors.append(f"rollback 실패: {type(exc).__name__}: {exc}")
        try:
            await load.kill()
        except BaseException as exc:                          # noqa: BLE001
            cleanup_errors.append(f"kill 실패: {type(exc).__name__}: {exc}")
        if not wait_task.done():
            wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)

    alive = False
    try:
        alive = load.is_alive()
    except Exception as exc:                                  # noqa: BLE001
        cleanup_errors.append(f"생존 확인 실패: {exc}")
        alive = True                                          # ⛔ 모르면 살아 있다고 본다
    if alive:
        cleanup_errors.append("부하 프로세스가 cleanup 후에도 살아 있다")

    return CanaryOutcome(bool(reasons), reasons, polls, load_stopped, rollback_done,
                         alive, cleanup_errors)


# ── CLI ─────────────────────────────────────────────────────────────────────


MIN_POLL_INTERVAL = 1.0


def validate_poll_interval(seconds: float) -> float:
    """⛔ 0·음수·NaN 이면 폴링이 hot loop 가 되어 **watchdog 자신이 부하원**이 된다
    (매 tick 마다 docker subprocess 6개를 띄운다). 켜는 순간 fail-fast."""
    value = float(seconds)
    if not (value == value) or value in (float("inf"), float("-inf")) or value < MIN_POLL_INTERVAL:
        raise ValueError(f"poll interval 은 유한하고 {MIN_POLL_INTERVAL}초 이상이어야 한다: {seconds!r}")
    return value


class RehearsalLoad:
    """리허설 전용 **가짜 부하** — 토큰 없이 순서를 증명하기 위한 자식 프로세스.

    ⛔ **운영 canary 에서는 절대 쓰이지 않는다.** `--rehearse` 없이는 만들어지지 않고,
       `--rehearse` 는 **운영 컨테이너를 target 으로 하면 거부**된다(구조적 분리).
    ⚠️ 왜 필요한가: 실제 부하는 유효 토큰이 있어야 살아 있고, 무효 토큰이면 인증 오류로
       **health 강제 실패보다 먼저 죽어** 정해진 순서를 증명하지 못한다.
    """

    def __init__(self, process: Any):
        self._inner = LoadProcess(process)

    async def wait(self) -> int:
        return await self._inner.wait()

    async def stop(self) -> None:
        await self._inner.stop()

    async def kill(self) -> None:
        await self._inner.kill()

    def is_alive(self) -> bool:
        return self._inner.is_alive()


async def start_rehearsal_load(*, python: str = sys.executable) -> RehearsalLoad:
    process = await asyncio.create_subprocess_exec(
        python, "-c", "import time\nwhile True: time.sleep(1)\n", cwd=str(REPO_ROOT))
    return RehearsalLoad(process)


def rehearsal_succeeded(outcome: CanaryOutcome, *, expected_abort_poll: int) -> bool:
    """주입한 health 중단의 전체 cleanup 계약이 관측된 경우에만 리허설 성공이다."""
    return (
        outcome.aborted
        and outcome.reasons == ["health 실패 (injected)"]
        and outcome.polls == expected_abort_poll
        and outcome.load_stopped
        and outcome.rollback_done
        and not outcome.load_alive_after_cleanup
        and not outcome.cleanup_errors
    )


def injected_abort_collector(inner: Callable[[], Awaitable[Snapshot]], after_polls: int):
    """N 번째 poll 부터 health 실패를 **주입**한다 — 실제 서비스를 깨지 않고 순서를 증명한다."""
    state = {"polls": 0}

    async def collect() -> Snapshot:
        snapshot = await inner()
        state["polls"] += 1
        if state["polls"] >= after_polls:
            return replace(snapshot, health_ok=False, injected_health_failure=True)
        return snapshot

    return collect


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="W canary 실행기 (env 적용 + 부하 자식 + 자동 중단 watchdog)",
        allow_abbrev=False)
    # ⛔ 기본값 없음 — compose 의 fastapi 는 `expose` 만 있고 host port 를 publish 하지 않는다.
    #    기본 URL 을 두면 "닿지 않는 주소로 리허설하고 rollback 만 검증"하게 된다.
    parser.add_argument("--url", help="부하가 붙을 WS URL (실제 canary/리허설은 필수)")
    source = parser.add_mutually_exclusive_group()
    # ⛔ `--token` 은 의도적으로 없다 — argv 는 `ps` 로 보인다.
    source.add_argument("--token-stdin", action="store_true")
    source.add_argument("--token-file")
    parser.add_argument("--container", default=PRODUCTION_CONTAINER)
    parser.add_argument("--project", help="compose project (리허설 스택이면 필수)")
    parser.add_argument("--compose-file")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--command-timeout", type=float, default=COMMAND_TIMEOUT_SECONDS,
                        help="개별 docker/exec 호출 상한(초). 에뮬레이션 스택은 키워야 한다")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--rehearse", action="store_true",
                        help="토큰 없이 순서만 증명(가짜 부하). 운영 컨테이너에는 사용 불가")
    parser.add_argument("--inject-abort-after", type=int,
                        help="리허설 전용 — N번째 poll 부터 health 실패 주입")
    parser.add_argument("--recover", action="store_true",
                        help="이전 canary 의 backup 으로 env 를 복원한다(토큰 불요)")
    parser.add_argument("--dry-run", action="store_true", help="실행 없이 계획·명령만 출력")
    return parser


def validate_cli_combo(args) -> list[str]:
    """⛔ 리허설 경로가 운영으로 **새지 않게** 하는 구조적 경계."""
    problems: list[str] = []
    if getattr(args, "recover", False):
        # ⚠️ 복원은 **토큰도 부하도 필요 없다** — 남은 backup 을 되돌리는 것뿐이다.
        if args.rehearse or args.inject_abort_after is not None:
            problems.append("--recover 는 단독으로 쓴다(--rehearse/--inject-abort-after 불가)")
        if args.dry_run:
            problems.append("--recover 와 --dry-run 을 함께 쓸 수 없다")
        return problems
    if not args.url:
        problems.append("실제 canary/리허설은 --url 이 필요하다")
    target_is_production = args.container == PRODUCTION_CONTAINER
    if args.rehearse:
        if target_is_production:
            problems.append("--rehearse 는 운영 컨테이너를 target 으로 할 수 없다 "
                            "(--container/--project 로 리허설 스택을 지정할 것)")
        if not args.project:
            problems.append("--rehearse 는 --project 로 격리 스택을 명시해야 한다")
        if not args.compose_file:
            problems.append("--rehearse 는 독립 --compose-file 을 명시해야 한다")
        # ⛔ 리허설이 **운영 `.env` 를 고쳐 쓰면** 격리가 무의미하다 — 실행기는 이 파일을
        #    직접 편집하고 재기동한다. 기본값이 운영 `.env` 라 명시를 강제한다.
        if Path(args.env_file).resolve() == (REPO_ROOT / ".env").resolve():
            problems.append("--rehearse 는 운영 .env 를 쓸 수 없다 — --env-file 로 리허설 env 를 지정할 것")
        if args.inject_abort_after is None:
            problems.append("--rehearse 는 --inject-abort-after 로 기대 중단 시점을 명시해야 한다")
        elif args.inject_abort_after < 1:
            problems.append("--inject-abort-after 는 1 이상이어야 한다")
    else:
        if args.inject_abort_after is not None:
            problems.append("--inject-abort-after 는 --rehearse 전용이다")
        if not (args.token_stdin or args.token_file):
            problems.append("실제 canary 는 토큰이 필요하다 (--token-stdin 또는 --token-file)")
    return problems


async def _amain(args) -> int:
    combo = validate_cli_combo(args)
    if combo:
        print(json.dumps({"ok": False, "cli": combo}, ensure_ascii=False))
        return 2
    poll_interval = validate_poll_interval(args.poll_interval)
    set_command_timeout(args.command_timeout)
    target = Target(container=args.container, project=args.project,
                    compose_file=args.compose_file, env_file=Path(args.env_file))

    if args.dry_run:
        print(f"  target: {target.container} project={target.project} env={target.env_file}")
        print(f"  env 적용: {CANARY_ENV}")
        print(f"  부하: {'리허설 가짜 부하' if args.rehearse else 'ws_auth_load.py'} → {args.url}")
        print(f"  창: 최악 {maximum_plan_seconds():.0f}s ({len(PHASES)} phase)")
        print(f"  폴링: {poll_interval}s — health · broadcast · auth · probe · 재시작 · ERROR")
        print("  복원: 원래 값/키 부재까지 정확히 되돌린 뒤 force-recreate + health")
        return 0

    env_path = target.env_file
    if args.recover:
        # ⛔ 토큰·preflight 없이 **복원만** 한다 — 남은 backup 이 곧 "이전 창이 안 닫혔다"는 뜻이다.
        backup = backup_path_for(env_path)
        if not backup.exists():
            print(json.dumps({"ok": False, "recovered": False,
                              "error": f"복원할 canary backup 이 없다: {backup}"},
                             ensure_ascii=False))
            return 1
        identity_problems = await check_target_identity(target)
        if identity_problems:
            print(json.dumps({"ok": False, "target": identity_problems}, ensure_ascii=False))
            return 1
        restorer = EnvRestorer(env_path, target)
        await restorer.restore()
        print(json.dumps({"ok": True, "recovered": True,
                          "backup_remaining": backup_path_for(env_path).exists()},
                         ensure_ascii=False))
        return 0

    original = read_env_values(env_path.read_text(encoding="utf-8"), CANARY_ENV)
    start_problems = validate_canary_start_env(original)
    if start_problems:
        print(json.dumps({"ok": False, "start_env": start_problems}, ensure_ascii=False))
        return 1

    # env 를 건드리기 전에 실패할 수 있는 입력 검증을 끝낸다. 시작 시각은 아래의 의도적
    # force-recreate 뒤에 읽어야 첫 poll에서 그 재생성을 장애로 오판하지 않는다.
    token = None if args.rehearse else load_id_token(token_file=args.token_file)

    # ⛔ env를 쓰거나 compose up을 하기 전에 target 정체를 확인한다. preflight에서만 확인하면
    # 잘못된 target을 이미 재생성한 뒤라 격리 검사가 너무 늦다. 토큰 입력 검증은
    # 외부 Docker 조회보다 먼저 끝내 입력 실패가 운영 상태를 전혀 건드리지 않게 한다.
    identity_problems = await check_target_identity(target)
    if identity_problems:
        print(json.dumps({"ok": False, "target": identity_problems}, ensure_ascii=False))
        return 1

    # ⛔ **env 를 건드리기 직전에** durable backup 을 만든다. 프로세스가 SIGKILL 로 죽어
    #    in-memory cleanup 이 통째로 사라져도, 이 파일 하나면 `--recover` 로 되돌릴 수 있다.
    #    ⚠️ backup 이 이미 있으면 여기서 **시작을 거부**한다(이전 창이 안 닫혔다는 뜻).
    create_env_backup(env_path)
    restorer = EnvRestorer(env_path, target)
    load: Optional[LoadHandle] = None
    outcome: Optional[CanaryOutcome] = None

    # ⛔ **이 지점부터 가장 바깥 `finally` 가 env 를 소유한다.**
    try:
        atomic_write_text(
            env_path,
            apply_env_values(env_path.read_text(encoding="utf-8"), dict(CANARY_ENV)),
        )
        await recreate_and_verify_health(target)

        problems = await wait_for_preflight(target, url=args.url)
        if problems:
            print(json.dumps({"ok": False, "preflight": problems}, ensure_ascii=False))
            return 1                                   # ⛔ 부하 프로세스 **0건** 생성

        baseline = (await run_command(target.inspect("{{.State.StartedAt}}"))).strip()
        load_started_at = time.monotonic()
        load = (await start_rehearsal_load() if args.rehearse
                else await start_load_process(token=token, url=args.url))

        async def collect() -> Snapshot:
            return await collect_from_server(target=target)

        collector = collect
        if args.inject_abort_after:
            collector = injected_abort_collector(collect, args.inject_abort_after)

        outcome = await run_monitored_canary(
            collect=collector, load=load, rollback=restorer.restore,
            baseline_started_at=baseline, poll_interval=poll_interval,
            # 리허설은 지정 poll에서 의도적으로 중단한다. 실제 canary만 7분 계획을 완주해야
            # 정상 종료다. 자식이 버그로 즉시 0을 반환해도 성공으로 접지 않는다.
            minimum_clean_exit_seconds=(0.0 if args.rehearse else total_plan_seconds()),
            load_started_at=load_started_at,
        )
    finally:
        if load is not None:
            try:
                await load.kill()
            except Exception as exc:                   # noqa: BLE001
                print(f"[CLEANUP] kill 실패: {exc}", file=sys.stderr)
        await restorer.restore()                       # ⚠️ 멱등 — monitor 가 이미 했으면 no-op

    rehearsal_passed = bool(args.rehearse) and rehearsal_succeeded(
        outcome, expected_abort_poll=args.inject_abort_after)
    print(json.dumps({
        "ok": outcome.ok, "aborted": outcome.aborted, "reasons": outcome.reasons,
        "polls": outcome.polls, "rollback_done": outcome.rollback_done,
        "cleanup_errors": outcome.cleanup_errors, "rehearsal": bool(args.rehearse),
        "rehearsal_passed": rehearsal_passed,
    }, ensure_ascii=False))
    succeeded = rehearsal_passed if args.rehearse else outcome.ok
    return 0 if succeeded else 1


#: ⛔ SSH 세션이 끊기면 **SIGHUP** 이 온다. 기본 동작은 *프로세스 즉시 종료* 라 Python 의
#: `finally` 가 **한 줄도 돌지 않는다** — 그러면 `.env` 에 `TOPIC_DISPATCHER_ENABLED=true` 가
#: 남고 컨테이너도 켜진 채로 있는다. 7분 창을 일반 SSH 로 돌리면 충분히 현실적인 경로다.
#: (`KeyboardInterrupt` 만 잡던 시절엔 Ctrl-C 만 안전했다.)
CANCEL_ON_SIGNALS = tuple(
    sig for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None))
    if sig is not None
)


def make_signal_canceller(task: "asyncio.Future", received: dict) -> Callable[[int], None]:
    """첫 signal 만 취소로 바꾼다.

    ⛔ **두 번째 이후는 무시해야 한다.** cleanup 은 이미 취소된 task 의 `finally` 안에서
    `await` 하는데, 거기서 다시 취소가 오면 그 await 가 끊겨 **rollback 이 중간에 죽는다** —
    즉 flag 를 되돌리려던 바로 그 경로가 사라진다.
    """
    def _handle(signum: int) -> None:
        if received.get("signum") is not None:
            return                                   # 반복 signal 은 cleanup 을 건드리지 않는다
        received["signum"] = signum
        task.cancel()
    return _handle


async def _amain_with_signals(args) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(_amain(args))
    received: dict = {"signum": None}
    handler = make_signal_canceller(task, received)
    installed = []
    for signum in CANCEL_ON_SIGNALS:
        try:
            loop.add_signal_handler(signum, handler, signum)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):   # 일부 플랫폼/루프는 미지원
            pass
    try:
        return await task
    except asyncio.CancelledError:
        if received["signum"] is None:
            raise                                    # 우리가 보낸 취소가 아니다
        print(f"[SIGNAL] {received['signum']} — cleanup 완료 후 종료", file=sys.stderr)
        return 128 + received["signum"]
    finally:
        for signum in installed:
            try:
                loop.remove_signal_handler(signum)
            except Exception:                        # noqa: BLE001 — 종료 경로를 막지 않는다
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(_amain_with_signals(args))
    except KeyboardInterrupt:
        # ⚠️ 취소는 `run_monitored_canary` 의 `finally` 를 통과하므로 rollback 은 이미 돌았다.
        print("[INTERRUPTED] cleanup 완료 후 종료", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
