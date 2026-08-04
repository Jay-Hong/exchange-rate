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
   들어간다**(`ps` 로 보인다). 지금은 heredoc 으로 `curl --config -` 의 **stdin** 에 넣는다 —
   셸이 파이프에 쓰므로 어떤 argv 에도 값이 없다. 토큰도 같은 이유로 자식 stdin 으로만 준다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ws_auth_load import (  # noqa: E402
    PHASES, evaluate_server_abort, load_id_token, maximum_plan_seconds,
)

BROADCAST_STALL_SECONDS = 30.0
DEFAULT_POLL_INTERVAL = 1.0
COMMAND_TIMEOUT_SECONDS = 8.0          # 개별 docker/curl 호출
RECREATE_TIMEOUT_SECONDS = 180.0       # 컨테이너 재기동은 느리다
STOP_GRACE_SECONDS = 5.0
REPO_ROOT = Path(__file__).resolve().parent.parent


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


def admin_fetch_command(path: str) -> list[str]:
    """admin endpoint 조회 — **컨테이너 안 Python 이 env 를 직접 읽는다**.

    ⛔ 셸 경유 curl 을 쓰지 않는 이유 두 가지:
       1. `-u admin:"$ADMIN_PASSWORD"` 는 셸이 확장한 **실제 값이 curl argv 에 남는다**(`ps`).
       2. heredoc `curl --config -` 로 피해도 **비밀번호에 `"`·`\\`·개행이 있으면 config 문법이
          깨진다** — 조용히 401 이 되고, 그건 "지표가 깨끗하다"로 오독되기 쉽다.
       여기서는 값이 **프로세스 메모리 안에서만** 쓰이고 어떤 argv 에도, 어떤 인용 규칙에도
       노출되지 않는다.
    ⚠️ 출력 형식은 `body\\n<status>` — `parse_curl_response` 가 그대로 받는다.
    """
    return ["docker", "compose", "exec", "-T", "fastapi",
            "python", "-c", ADMIN_FETCH_PY, path]


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


async def run_command(command: Sequence[str], *, timeout: float = COMMAND_TIMEOUT_SECONDS,
                      cwd: Optional[Path] = None) -> str:
    """⛔ 종료코드·stderr·timeout 을 **전부** 본다. 하나라도 어긋나면 올린다."""
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
    container_started_at: Optional[str] = None
    new_error_count: int = 0
    broadcast_age_seconds: Optional[float] = 0.0
    auth_metrics: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)


def evaluate_snapshot_abort(snapshot: Snapshot, *, baseline_started_at: Optional[str],
                            previous_probe_outstanding: int = 0) -> list[str]:
    """합의한 **즉시 중단 6종** 전부."""
    reasons: list[str] = []
    if not snapshot.health_ok:
        reasons.append("health 실패")
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
    # ⛔ 서버 지표 판정은 **재구현하지 않는다** — 부하 도구와 같은 함수를 쓴다.
    reasons.extend(evaluate_server_abort(snapshot.auth_metrics, snapshot.probe,
                                         previous_probe_outstanding))
    return reasons


async def collect_from_server(*, timeout: float = COMMAND_TIMEOUT_SECONDS) -> Snapshot:
    """운영 수집기 — **서버에서** 실행한다. 조회 실패는 전부 `CollectorError`."""
    health = parse_curl_response(await run_command(admin_fetch_command("/health"), timeout=timeout))
    heartbeat = parse_curl_response(
        await run_command(admin_fetch_command("/admin/api/broadcast-heartbeat"), timeout=timeout))
    auth = parse_curl_response(
        await run_command(admin_fetch_command("/admin/api/ws-auth-executor-metrics"), timeout=timeout))
    probe = parse_curl_response(
        await run_command(admin_fetch_command("/admin/api/default-executor-probe"), timeout=timeout))
    started = (await run_command(
        ["docker", "inspect", "exchange-rate-app", "--format", "{{.State.StartedAt}}"],
        timeout=timeout)).strip()
    logs = await run_command(["docker", "compose", "logs", "fastapi", "--since", "5s"],
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
    process.stdin.write(token.encode("utf-8"))     # ⛔ argv 가 아니라 stdin
    await process.stdin.drain()
    process.stdin.close()
    return LoadProcess(process)


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
        if key in remaining and not stripped.startswith("#"):
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
            continue                                   # None → 줄 자체를 지운다
        out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


class EnvRestorer:
    """env 를 **정확히 원래대로** 되돌린다(값이든 키 부재든). 여러 번 불려도 한 번만 일한다."""

    def __init__(self, original: dict[str, Optional[str]], env_path: Path):
        self._original, self._env_path = original, env_path
        self.done = False

    async def restore(self) -> None:
        if self.done:
            return
        self._env_path.write_text(
            apply_env_values(self._env_path.read_text(encoding="utf-8"), self._original),
            encoding="utf-8")
        await recreate_and_verify_health()
        current = read_env_values(self._env_path.read_text(encoding="utf-8"), self._original)
        if current != self._original:
            raise CollectorError(f"env 가 원상복원되지 않았다: {current} != {self._original}")
        self.done = True


async def recreate_and_verify_health() -> None:
    """⛔ env 는 컨테이너 **재생성** 시에만 다시 읽힌다(`restart` 는 반영 안 된다).
    확인 없이 끝내면 "적용했다"는 보고만 남고 실제로는 구 값으로 돈다."""
    await run_command(["docker", "compose", "up", "-d", "--force-recreate", "fastapi"],
                      timeout=RECREATE_TIMEOUT_SECONDS)
    health = parse_curl_response(await run_command(admin_fetch_command("/health"),
                                                   timeout=RECREATE_TIMEOUT_SECONDS))
    if health.get("status") != "healthy":
        raise CollectorError(f"재기동 후 health 가 정상이 아니다: {health.get('status')}")


CONTAINER_ENV_PY = (
    "import json,os,sys\n"
    "sys.stdout.write(json.dumps({k: os.environ.get(k) for k in sys.argv[1:]})+'\\n200')\n"
)


async def container_env(keys: Sequence[str]) -> dict:
    return parse_curl_response(await run_command(
        ["docker", "compose", "exec", "-T", "fastapi", "python", "-c", CONTAINER_ENV_PY, *keys]))


async def preflight() -> list[str]:
    """⛔ **부하를 만들기 전에** 창의 전제가 실제로 적용됐는지 본다.

    이게 없으면 probe 가 꺼진 채로 canary 가 돌아 **빈 지표를 성공으로** 읽는다 — 이 트랙에서
    반복된 false green 의 정확한 형태다. 하나라도 어긋나면 부하 프로세스는 **0건** 생성된다.
    """
    problems: list[str] = []
    env = await container_env(list(CANARY_ENV))
    for key, expected in CANARY_ENV.items():
        actual = env.get(key)
        if (actual or "").strip().lower() != expected:
            problems.append(f"{key}={actual!r} (기대 {expected!r})")

    probe = parse_curl_response(await run_command(
        admin_fetch_command("/admin/api/default-executor-probe")))
    if not probe.get("enabled"):
        problems.append("probe enabled=false")
    if not probe.get("running"):
        problems.append("probe running=false — flag 만 켜지고 task 가 안 돈다")

    auth = parse_curl_response(await run_command(
        admin_fetch_command("/admin/api/ws-auth-executor-metrics")))
    metrics = auth.get("metrics") if isinstance(auth.get("metrics"), dict) else {}
    if metrics.get("max_workers") != EXPECTED_AUTH_WORKERS:
        problems.append(f"auth max_workers={metrics.get('max_workers')} (기대 {EXPECTED_AUTH_WORKERS})")

    heartbeat = parse_curl_response(await run_command(
        admin_fetch_command("/admin/api/broadcast-heartbeat")))
    age = heartbeat.get("age_seconds")
    if not isinstance(age, (int, float)) or age >= BROADCAST_STALL_SECONDS:
        problems.append(f"broadcast heartbeat 이 신선하지 않다: {age}")
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
    emit: Callable[[str], None] = print,
) -> CanaryOutcome:
    wait_task = asyncio.ensure_future(load.wait())
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="W canary 실행기 (env 적용 + 부하 자식 + 자동 중단 watchdog)",
        allow_abbrev=False)
    parser.add_argument("--url", default="ws://localhost:8000/ws")
    source = parser.add_mutually_exclusive_group(required=True)
    # ⛔ `--token` 은 의도적으로 없다 — argv 는 `ps` 로 보인다.
    source.add_argument("--token-stdin", action="store_true")
    source.add_argument("--token-file")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--dry-run", action="store_true", help="실행 없이 계획·명령만 출력")
    return parser


async def _amain(args) -> int:
    poll_interval = validate_poll_interval(args.poll_interval)
    if args.dry_run:
        print(f"  env 적용: {CANARY_ENV}")
        print(f"  부하: {REPO_ROOT / 'scripts' / 'ws_auth_load.py'} --token-stdin --url {args.url}")
        print(f"  창: 최악 {maximum_plan_seconds():.0f}s ({len(PHASES)} phase)")
        print(f"  폴링: {poll_interval}s — health · broadcast · auth · probe · 재시작 · ERROR")
        print("  복원: 원래 값/키 부재까지 정확히 되돌린 뒤 force-recreate + health")
        return 0

    # ⛔ **env 를 건드리기 전에** 실패할 수 있는 것들을 먼저 끝낸다 — 여기서 실패하면 되돌릴
    #    상태 자체가 없다(복원 공백 0).
    token = load_id_token(token_file=args.token_file)
    baseline = (await run_command(
        ["docker", "inspect", "exchange-rate-app", "--format", "{{.State.StartedAt}}"])).strip()

    env_path = Path(args.env_file)
    original = read_env_values(env_path.read_text(encoding="utf-8"), CANARY_ENV)
    restorer = EnvRestorer(original, env_path)
    load: Optional[LoadProcess] = None
    outcome: Optional[CanaryOutcome] = None

    # ⛔ **이 지점부터 가장 바깥 `finally` 가 env 를 소유한다.** 한때 monitor 는 flag 를 켜지
    #    않으면서 rollback 만 했다 — 운영자가 먼저 켜고 자식 생성이 실패하면 **켜진 채 남았다**.
    try:
        env_path.write_text(
            apply_env_values(env_path.read_text(encoding="utf-8"), dict(CANARY_ENV)),
            encoding="utf-8")
        await recreate_and_verify_health()

        problems = await preflight()
        if problems:
            print(json.dumps({"ok": False, "preflight": problems}, ensure_ascii=False))
            return 1                                   # ⛔ 부하 프로세스 **0건** 생성

        load = await start_load_process(token=token, url=args.url)
        outcome = await run_monitored_canary(
            collect=collect_from_server, load=load, rollback=restorer.restore,
            baseline_started_at=baseline, poll_interval=poll_interval,
        )
    finally:
        if load is not None:
            try:
                await load.kill()
            except Exception as exc:                   # noqa: BLE001
                print(f"[CLEANUP] kill 실패: {exc}", file=sys.stderr)
        await restorer.restore()                       # ⚠️ 멱등 — monitor 가 이미 했으면 no-op

    print(json.dumps({
        "ok": outcome.ok, "aborted": outcome.aborted, "reasons": outcome.reasons,
        "polls": outcome.polls, "rollback_done": outcome.rollback_done,
        "cleanup_errors": outcome.cleanup_errors,
    }, ensure_ascii=False))
    return 0 if outcome.ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        # ⚠️ 취소는 `run_monitored_canary` 의 `finally` 를 통과하므로 rollback 은 이미 돌았다.
        print("[INTERRUPTED] cleanup 완료 후 종료", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
