"""W canary **자동 중단 watchdog** — 중단 조건을 *문서*가 아니라 *실행 경로*에 둔다.

⛔ **왜 필요한가.** `ws_auth_load.evaluate_server_abort()` 는 순수 함수로만 있었고 **호출자가
없었다**. 그래서 합의한 "즉시 중단 6종" 중 자동화된 것은 클라 timeout/error 둘뿐이었고,
health 실패 · 컨테이너 재시작 · 신규 ERROR · auth/probe 지표 · legacy broadcast 정지는
**아무도 보지 않는 상태**였다. "운영자가 스냅샷을 넣어 판정한다"는 8 동시 연결이 도는
7분 창에서 실행 가능한 절차가 아니다 — 사람이 1초 주기로 5개 endpoint 를 볼 수 없다.

## 이 모듈이 지키는 것

⛔ **rollback 은 `finally` 에 있다.** 중단이든 정상 종료든 예외든 SIGINT 든 `TOPIC_DISPATCHER_
   ENABLED` 를 되돌린다. canary 는 **유계 실험**이라 창이 닫히면 flag 도 닫혀야 한다.
⛔ **admin 비밀번호를 이 프로세스가 들지 않는다.** 컨테이너 **안에서** `$ADMIN_PASSWORD` 를
   확장한다 — 우리 argv·로그·예외 어디에도 값이 없다(있으면 `ps` 로 보인다).
⛔ **부하 생성기와 분리한다.** 부하는 자식 프로세스이고, 판정·중단·rollback 은 여기서 한다.
⚠️ `outstanding` 은 **2회 연속**일 때만 포화로 본다 — 1회는 정상 스케줄링 지터로도 나온다.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

from scripts.ws_auth_load import evaluate_server_abort

BROADCAST_STALL_SECONDS = 30.0
DEFAULT_POLL_INTERVAL = 1.0

#: ⛔ 값이 **컨테이너 안에서** 확장된다 — 이 프로세스의 argv 에는 `$ADMIN_PASSWORD` 라는
#:   글자만 남는다. `-u admin:<실제값>` 형태를 만들지 말 것(`ps` 노출).
ADMIN_CURL = 'curl -s -u admin:"$ADMIN_PASSWORD" http://localhost:8000'


def admin_fetch_command(path: str) -> list[str]:
    """admin endpoint 조회 명령. **비밀번호 값이 들어가지 않는다.**"""
    return ["docker", "compose", "exec", "-T", "fastapi", "sh", "-c", f"{ADMIN_CURL}{path}"]


# ── 판정 (순수) ─────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    health_ok: bool = True
    container_started_at: Optional[str] = None
    new_error_count: int = 0
    broadcast_age_seconds: float = 0.0
    auth_metrics: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)


def evaluate_snapshot_abort(snapshot: Snapshot, *, baseline_started_at: Optional[str],
                            previous_probe_outstanding: int = 0) -> list[str]:
    """합의한 **즉시 중단 6종** 전부. 1건이라도 나오면 창을 닫는다."""
    reasons: list[str] = []
    if not snapshot.health_ok:
        reasons.append("health 실패")
    if (baseline_started_at is not None
            and snapshot.container_started_at is not None
            and snapshot.container_started_at != baseline_started_at):
        reasons.append("컨테이너 재시작")
    if snapshot.new_error_count > 0:
        reasons.append(f"신규 ERROR/traceback {snapshot.new_error_count}건")
    if snapshot.broadcast_age_seconds >= BROADCAST_STALL_SECONDS:
        reasons.append(f"legacy broadcast {snapshot.broadcast_age_seconds:.0f}초 정지")
    # ⛔ 서버 지표 판정은 **재구현하지 않는다** — 부하 도구와 같은 함수를 쓴다(어긋나면 두 곳이
    #    다른 기준으로 canary 를 판정하게 된다).
    reasons.extend(evaluate_server_abort(snapshot.auth_metrics, snapshot.probe,
                                         previous_probe_outstanding))
    return reasons


# ── 실행 (중단 → 자식 종료 → rollback) ──────────────────────────────────────


@dataclass
class CanaryOutcome:
    aborted: bool
    reasons: list[str]
    polls: int
    load_stopped: bool
    rollback_done: bool


async def run_monitored_canary(
    *,
    collect: Callable[[], Awaitable[Snapshot]],
    load: Awaitable[Any],
    stop_load: Callable[[], Awaitable[None]],
    rollback: Callable[[], Awaitable[None]],
    baseline_started_at: Optional[str] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    emit: Callable[[str], None] = print,
) -> CanaryOutcome:
    """부하를 돌리며 **1초마다** 서버를 보고, 조건이 걸리면 즉시 닫는다.

    ⛔ `rollback` 은 어떤 경로로 빠져나가든 실행된다(정상 종료 · 중단 · 예외 · 취소).
    """
    load_task = asyncio.ensure_future(load)
    reasons: list[str] = []
    polls = 0
    previous_outstanding = 0
    load_stopped = False
    rollback_done = False

    try:
        while not load_task.done():
            snapshot = await collect()
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
        # ⛔ 순서가 중요하다 — 부하를 먼저 끊고 flag 를 되돌린다. 반대로 하면 flag 가 닫힌 뒤에도
        #    부하가 계속 두드려 "왜 전부 거부되는가" 하는 잡음을 만든다.
        try:
            await stop_load()
            load_stopped = True
        finally:
            await rollback()
            rollback_done = True

    if not load_task.done():
        load_task.cancel()
    await asyncio.gather(load_task, return_exceptions=True)
    return CanaryOutcome(bool(reasons), reasons, polls, load_stopped, rollback_done)


# ── 실제 수집기 (운영) ──────────────────────────────────────────────────────


async def _run(command: Sequence[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL,      # ⚠️ heredoc stdin 소비 방지
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await process.communicate()
    return out.decode("utf-8", "replace")


def _json_or_empty(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def collect_from_server(baseline_started_at: Optional[str] = None) -> Snapshot:
    """운영 수집기 — 서버에서 실행한다(컨테이너 env 로 admin 인증)."""
    health = _json_or_empty(await _run(admin_fetch_command("/health")))
    auth = _json_or_empty(await _run(admin_fetch_command("/admin/api/ws-auth-executor-metrics")))
    probe = _json_or_empty(await _run(admin_fetch_command("/admin/api/default-executor-probe")))
    started = (await _run(["docker", "inspect", "exchange-rate-app",
                           "--format", "{{.State.StartedAt}}"])).strip()
    errors = await _run(["sh", "-c",
                         "docker compose logs fastapi --since 5s 2>&1 | grep -cE 'ERROR|Traceback'"])
    return Snapshot(
        health_ok=health.get("status") == "healthy",
        container_started_at=started or None,
        new_error_count=int((errors.strip() or "0").splitlines()[0] or 0),
        auth_metrics=auth.get("metrics", {}) if isinstance(auth.get("metrics"), dict) else {},
        probe=probe.get("metrics", {}) if isinstance(probe.get("metrics"), dict) else {},
    )
