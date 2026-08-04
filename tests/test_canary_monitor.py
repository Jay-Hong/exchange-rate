"""W canary 실행기 — **중단 조건이 실행 경로에 연결돼 있는가 + 운영 배선이 fail-closed 인가**.

⛔ 이 모듈이 없던 동안 `evaluate_server_abort()` 는 순수 함수인데 **호출자가 없었다**.
⛔ 그리고 watchdog 을 만든 뒤에도 한동안 **실행기가 없었다**(수집기에서 파일이 끝났다) —
   "배선 완료"라고 적었지만 CLI · 부하 자식 · stop · rollback 이 전부 부재였다.
⛔ 더 조용한 결함: `broadcast_age_seconds` 를 **아무도 채우지 않아** legacy broadcast 정지가
   합성 Snapshot 에서만 발화했다. 그래서 여기서는 **실제 수집기까지 수직으로** 본다.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import canary_monitor as mon  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakeLoad:
    """부하 자식 대역 — 종료 코드·생존·정지 실패를 주입한다."""

    def __init__(self, *, seconds=0.5, exit_code=0, raises=None,
                 stop_error=None, stays_alive=False):
        self._seconds, self._exit_code, self._raises = seconds, exit_code, raises
        self._stop_error, self._stays_alive = stop_error, stays_alive
        self.alive = True
        self.stopped = self.killed = False

    async def wait(self):
        await asyncio.sleep(self._seconds)
        if self._raises:
            raise self._raises
        if not self._stays_alive:      # ⚠️ 전제: 이 대역은 stop·kill 로도 안 죽는다
            self.alive = False
        return self._exit_code

    async def stop(self):
        self.stopped = True
        if self._stop_error:
            raise self._stop_error
        if not self._stays_alive:
            self.alive = False

    async def kill(self):
        self.killed = True
        if not self._stays_alive:
            self.alive = False

    def is_alive(self):
        return self.alive


class _Harness:
    def __init__(self, snapshots, *, collect_error=None, rollback_error=None):
        self._snapshots = list(snapshots)
        self._collect_error = collect_error
        self._rollback_error = rollback_error
        self.collected = 0
        self.calls: list[str] = []

    async def collect(self):
        # ⚠️ 실제 수집기는 I/O 라 반드시 양보한다. 즉시 반환하면 loop 가 한 번도 안 돌아
        #    부하 task 가 **시작조차 못 한 채** 취소되고, 취소 관련 단언이 무의미해진다.
        await asyncio.sleep(0)
        if self._collect_error:
            raise self._collect_error
        self.collected += 1
        return self._snapshots.pop(0) if self._snapshots else mon.Snapshot()

    async def rollback(self):
        self.calls.append("rollback")
        if self._rollback_error:
            raise self._rollback_error


def _outcome(harness, load=None, **kw):
    load = load or _FakeLoad()
    return _run(mon.run_monitored_canary(
        collect=harness.collect, load=load, rollback=harness.rollback,
        poll_interval=0.001, emit=lambda _: None, **kw)), load


# ── 판정이 실행 경로에 연결돼 있는가 ────────────────────────────────────────


def test_monitor_actually_polls_the_server():
    harness = _Harness([])
    outcome, _ = _outcome(harness)
    assert harness.collected >= 1 and outcome.polls >= 1, "서버를 한 번도 보지 않았다"


@pytest.mark.parametrize("snapshot,expected", [
    (mon.Snapshot(health_ok=False), "health 실패"),
    (mon.Snapshot(new_error_count=1), "신규 ERROR"),
    (mon.Snapshot(broadcast_age_seconds=30), "broadcast"),
    (mon.Snapshot(broadcast_age_seconds=None), "관측 불가"),
    (mon.Snapshot(auth_metrics={"queue_wait_ms_max": 5000}), "queue_wait_ms_max"),
    (mon.Snapshot(auth_metrics={"never_started": 1}), "never_started"),
    (mon.Snapshot(auth_metrics={"caller_cancelled_while_running": 1}), "caller_cancelled"),
    (mon.Snapshot(probe={"queue_delay_ms_max": 1000}), "queue delay"),
    (mon.Snapshot(auth_running=False), "auth executor 정지"),
    (mon.Snapshot(probe_enabled=False), "probe disabled"),
    (mon.Snapshot(probe_running=False), "probe 정지"),
])
def test_each_agreed_condition_actually_aborts(snapshot, expected):
    outcome, _ = _outcome(_Harness([snapshot]))
    assert outcome.aborted and any(expected in r for r in outcome.reasons), outcome.reasons
    assert not outcome.ok


def test_container_restart_aborts_only_against_a_baseline():
    outcome, _ = _outcome(_Harness([mon.Snapshot(container_started_at="T2")]),
                          baseline_started_at="T1")
    assert outcome.aborted and "재시작" in outcome.reasons[0]

    same, _ = _outcome(_Harness([mon.Snapshot(container_started_at="T1")]),
                       baseline_started_at="T1")
    assert not same.aborted


def test_probe_outstanding_needs_two_consecutive_observations():
    once, _ = _outcome(_Harness([mon.Snapshot(probe={"outstanding": 1}), mon.Snapshot()]))
    assert not once.aborted, "1회 관측으로 중단했다"

    # ⛔ 직전 상태를 **실제로 나르는지** 가른다 — 위 케이스만 두면 상수 1로 박아도 통과한다.
    late, _ = _outcome(_Harness([mon.Snapshot(), mon.Snapshot(probe={"outstanding": 1}),
                                 mon.Snapshot()]))
    assert not late.aborted, "직전이 정상인데 뒤늦은 1회 관측으로 중단했다"

    twice, _ = _outcome(_Harness([mon.Snapshot(probe={"outstanding": 1}),
                                  mon.Snapshot(probe={"outstanding": 1})]))
    assert twice.aborted and any("2회 연속" in r for r in twice.reasons)


# ── 부하 프로세스의 실패를 삼키지 않는가 ────────────────────────────────────


def test_nonzero_exit_of_the_load_process_is_an_abort():
    """⛔ 한때 `return_exceptions=True` 로 삼켜 **비정상 종료가 성공으로** 보였다."""
    outcome, _ = _outcome(_Harness([]), load=_FakeLoad(seconds=0.01, exit_code=1))
    assert outcome.aborted and "exit=1" in outcome.reasons[0]
    assert not outcome.ok


def test_exception_from_the_load_process_is_an_abort():
    outcome, _ = _outcome(_Harness([]),
                          load=_FakeLoad(seconds=0.01, raises=RuntimeError("자식 폭발")))
    assert outcome.aborted and "예외" in outcome.reasons[0]


def test_clean_exit_of_the_load_process_is_not_an_abort():
    outcome, _ = _outcome(_Harness([]), load=_FakeLoad(seconds=0.01, exit_code=0))
    assert not outcome.aborted and outcome.ok


def test_clean_exit_before_the_plan_duration_is_an_abort():
    """exit 0만 보면 PHASES가 비거나 조기 return해 인증 부하 0건이어도 성공이 된다."""
    outcome, _ = _outcome(
        _Harness([]), load=_FakeLoad(seconds=0.01, exit_code=0),
        minimum_clean_exit_seconds=1.0,
    )
    assert outcome.aborted and any("조기 정상 종료" in reason for reason in outcome.reasons)


def test_clean_exit_duration_starts_before_the_load_is_spawned():
    """monitor 진입부터 재면 spawn 직후 시작한 정상 자식을 조기 종료로 오판한다."""
    outcome, _ = _outcome(
        _Harness([]), load=_FakeLoad(seconds=0.01, exit_code=0),
        minimum_clean_exit_seconds=1.0, load_started_at=100.0,
        clock=lambda: 101.0,
    )
    assert outcome.ok and not outcome.aborted


def test_clean_exit_takes_one_final_server_snapshot_before_success():
    """마지막 poll 뒤 생긴 장애를 자식 exit 0이 앞질러 성공으로 만들면 안 된다."""
    completed = asyncio.Event()
    snapshots = [mon.Snapshot(), mon.Snapshot(auth_running=False)]
    collected = []

    class Load(_FakeLoad):
        async def wait(self):
            await completed.wait()
            self.alive = False
            return 0

    async def collect():
        collected.append(1)
        snapshot = snapshots.pop(0)
        if len(collected) == 1:
            completed.set()
            await asyncio.sleep(0)  # wait task가 done이 된 뒤 다음 loop로 간다.
        return snapshot

    harness = _Harness([])
    harness.collect = collect
    outcome, _ = _outcome(harness, load=Load())
    assert outcome.aborted
    assert any("auth executor 정지" in reason for reason in outcome.reasons)
    assert len(collected) == 2


# ── cleanup 이 구조적으로 보장되는가 ────────────────────────────────────────


def test_abort_stops_the_load_and_rolls_back_in_order():
    harness = _Harness([mon.Snapshot(health_ok=False)])
    outcome, load = _outcome(harness)
    assert load.stopped, "부하를 끊지 않았다"
    assert harness.calls == ["rollback"] and outcome.rollback_done
    assert not load.is_alive()


def test_rollback_runs_even_when_nothing_aborted():
    """⛔ canary 는 유계 실험이다 — 창이 닫히면 flag 도 닫혀야 한다(성공해도)."""
    harness = _Harness([])
    outcome, _ = _outcome(harness, load=_FakeLoad(seconds=0.01))
    assert not outcome.aborted and "rollback" in harness.calls


def test_collect_failure_aborts_and_still_rolls_back():
    """⛔ **fail-closed** — 지표를 못 읽은 것을 '깨끗하다'로 접지 않는다."""
    harness = _Harness([], collect_error=mon.CollectorError("401"))
    outcome, load = _outcome(harness)
    assert outcome.aborted and "수집 실패" in outcome.reasons[0]
    assert "rollback" in harness.calls and not load.is_alive()


def test_rollback_still_runs_when_stopping_the_load_fails():
    """⚠️ 자식 종료 실패가 rollback 까지 삼키면 flag 가 켜진 채 남는다."""
    harness = _Harness([mon.Snapshot(health_ok=False)])
    outcome, load = _outcome(harness, load=_FakeLoad(stop_error=RuntimeError("종료 실패")))
    assert any("stop_load 실패" in e for e in outcome.cleanup_errors)
    assert "rollback" in harness.calls, "자식 종료 실패가 rollback 까지 삼켰다"
    assert load.killed, "stop 이 실패했는데 kill 로 확인하지 않았다"
    assert not outcome.ok


def test_load_is_killed_even_when_rollback_fails():
    """⛔ rollback 실패가 **사망 확인**까지 건너뛰게 하면 부하가 살아남는다."""
    harness = _Harness([mon.Snapshot(health_ok=False)], rollback_error=RuntimeError("rollback 실패"))
    outcome, load = _outcome(harness)
    assert any("rollback 실패" in e for e in outcome.cleanup_errors)
    assert load.killed and not load.is_alive()
    assert not outcome.ok, "rollback 실패가 성공으로 보고됐다"


def test_surviving_load_process_makes_the_outcome_fail():
    """⛔ stop·kill 을 다 했는데도 살아 있으면 **성공이라고 말하면 안 된다**."""
    outcome, _ = _outcome(_Harness([]), load=_FakeLoad(seconds=0.01, stays_alive=True))
    assert outcome.load_alive_after_cleanup
    assert any("살아 있다" in e for e in outcome.cleanup_errors) and not outcome.ok


def test_rollback_runs_when_the_monitor_is_cancelled():
    """⛔ SIGINT(=취소)에서도 되돌린다."""
    harness = _Harness([])
    load = _FakeLoad(seconds=5)

    async def scenario():
        task = asyncio.ensure_future(mon.run_monitored_canary(
            collect=harness.collect, load=load, rollback=harness.rollback,
            poll_interval=0.05, emit=lambda _: None))
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _run(scenario())
    assert "rollback" in harness.calls and load.stopped


# ── 운영 수집기가 fail-closed 인가 ──────────────────────────────────────────


def _curl(body: dict, status: int = 200) -> str:
    return json.dumps(body) + f"\n{status}"


@pytest.mark.parametrize("raw,why", [
    (_curl({}, 401), "401 을 성공으로 접었다"),
    (_curl({}, 500), "500 을 성공으로 접었다"),
    ("not-json\n200", "잘못된 JSON 을 접었다"),
    ("[]\n200", "객체가 아닌 JSON 을 접었다"),
    ("no-status-line", "상태 코드 없음을 접았다"),
])
def test_non_2xx_and_malformed_responses_are_failures(raw, why):
    with pytest.raises(mon.CollectorError):
        mon.parse_curl_response(raw)


def test_2xx_json_object_parses():
    assert mon.parse_curl_response(_curl({"status": "healthy"})) == {"status": "healthy"}


def test_command_failure_is_raised_not_swallowed():
    """⛔ 한때 종료코드와 stderr 를 버려 docker 오류가 `{}` 로 접혔다."""
    with pytest.raises(mon.CollectorError) as caught:
        _run(mon.run_command([sys.executable, "-c",
                              "import sys; sys.stderr.write('boom'); sys.exit(3)"]))
    assert "exit=3" in str(caught.value)


def test_hanging_command_hits_the_timeout_instead_of_freezing_the_watchdog():
    """⛔ 호출 하나가 멈추면 watchdog 이 함께 멈춰 **중단도 rollback 도 영영 안 돈다**."""
    with pytest.raises(mon.CollectorError) as caught:
        _run(mon.run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3))
    assert "끝나지 않았다" in str(caught.value)


def test_collect_from_server_fills_broadcast_age(monkeypatch):
    """⛔ **실제 수집기까지 수직으로 본다.** 한때 `broadcast_age_seconds` 를 아무도 채우지 않아
    legacy broadcast 정지가 **합성 Snapshot 에서만** 발화했다(운영에선 영구 0)."""
    responses = {
        "/health": {"status": "healthy"},
        "/admin/api/broadcast-heartbeat": {"age_seconds": 4.0, "last_broadcast_time": "x"},
        "/admin/api/ws-auth-executor-metrics": {"running": True, "metrics": {"count": 1}},
        "/admin/api/default-executor-probe": {
            "enabled": True, "running": True, "metrics": {"outstanding": 0}},
    }

    async def fake_run(command, *, timeout=None, cwd=None):
        if command[0] == "docker" and command[1] == "inspect":
            return "2026-08-04T00:00:00Z\n"
        if command[0] == "docker" and command[1] == "compose" and "logs" in command:
            return "all quiet\n"
        script = command[-1]
        for path, body in responses.items():
            if path in script:
                return _curl(body)
        raise AssertionError(f"예상 밖 명령: {command}")

    monkeypatch.setattr(mon, "run_command", fake_run)
    snapshot = _run(mon.collect_from_server())
    assert snapshot.broadcast_age_seconds == 4.0, "수집기가 broadcast age 를 채우지 않는다"
    assert mon.evaluate_snapshot_abort(snapshot, baseline_started_at=None) == []

    responses["/admin/api/broadcast-heartbeat"] = {"age_seconds": 31.0}
    stalled = _run(mon.collect_from_server())
    reasons = mon.evaluate_snapshot_abort(stalled, baseline_started_at=None)
    assert any("정지" in r for r in reasons), "실제 수집 경로에서 broadcast 정지가 안 잡힌다"

    responses["/admin/api/broadcast-heartbeat"] = {"age_seconds": 1.0}
    responses["/admin/api/ws-auth-executor-metrics"]["running"] = False
    auth_stopped = _run(mon.collect_from_server())
    reasons = mon.evaluate_snapshot_abort(auth_stopped, baseline_started_at=None)
    assert any("auth executor 정지" in r for r in reasons), "수집기가 running 상태를 버렸다"


def test_collect_counts_real_error_lines_from_logs(monkeypatch):
    async def fake_run(command, *, timeout=None, cwd=None):
        if command[0] == "docker" and command[1] == "inspect":
            return "T\n"
        if command[0] == "docker" and command[1] == "compose" and "logs" in command:
            return "line ok\nsomething ERROR here\nTraceback (most recent call last):\n"
        body = {"status": "healthy"} if "/health" in command[-1] else {
            "age_seconds": 1.0} if "heartbeat" in command[-1] else {"metrics": {}}
        return _curl(body)

    monkeypatch.setattr(mon, "run_command", fake_run)
    assert _run(mon.collect_from_server()).new_error_count == 2


def test_endpoint_reported_error_is_a_collector_failure(monkeypatch):
    """⚠️ never-crash endpoint 는 오류에도 200 을 준다 — 본문의 `error` 를 봐야 한다."""
    async def fake_run(command, *, timeout=None, cwd=None):
        if command[0] == "docker" and command[1] == "inspect":
            return "T\n"
        if command[0] == "docker" and command[1] == "compose" and "logs" in command:
            return ""
        if "/health" in command[-1]:
            return _curl({"status": "healthy"})
        if "heartbeat" in command[-1]:
            return _curl({"age_seconds": None, "error": "unavailable"})
        return _curl({"metrics": {}})

    monkeypatch.setattr(mon, "run_command", fake_run)
    with pytest.raises(mon.CollectorError):
        _run(mon.collect_from_server())


# ── 비밀 비노출 ─────────────────────────────────────────────────────────────


def test_admin_password_never_reaches_any_process_argv():
    """⛔ **이전 주장이 두 번 틀렸다.** (1) 셸에서 `-u admin:"$ADMIN_PASSWORD"` 를 쓰면 확장된
    **실제 값이 curl argv 에 남는다**. (2) heredoc `--config -` 로 피해도 비밀번호에 `"`·`\\`·
    개행이 있으면 **config 문법이 깨져 조용히 401** 이 된다. 지금은 컨테이너 안 Python 이
    `os.environ` 을 직접 읽는다 — argv 에도, 인용 규칙에도 노출되지 않는다."""
    command = mon.admin_fetch_command("/health")
    joined = " ".join(command)
    assert "-u admin:" not in joined and "--user" not in joined, "자격증명이 인자에 있다"
    assert "--config" not in joined, "셸 인용에 의존하는 config 방식이 남아 있다"
    assert "os.environ['ADMIN_PASSWORD']" in mon.ADMIN_FETCH_PY, "env 를 직접 읽지 않는다"
    assert command[-1] == "/health" and "python" in command


@pytest.mark.parametrize("password", ['pa"ss', "pa\\ss", "pa\nss", "pa ss", "'quoted'"])
def test_awkward_passwords_do_not_break_the_fetch_contract(password):
    """⛔ 셸 config 방식이었다면 이 값들에서 문법이 깨졌다. 값은 **명령 어디에도 없어야** 한다."""
    joined = " ".join(mon.admin_fetch_command("/health"))
    assert password not in joined


def test_load_token_is_passed_by_stdin_not_argv():
    parser = mon.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--token", "secret"])
    assert parser.allow_abbrev is False


def test_stdin_failure_reaps_the_load_process_before_raising(monkeypatch):
    """handle 반환 전 stdin이 깨지면 바깥 cleanup은 자식 존재를 모른다."""
    class BrokenStdin:
        def __init__(self):
            self.closed = False

        def write(self, data):
            assert data == b"secret"

        async def drain(self):
            raise BrokenPipeError("broken")

        def close(self):
            self.closed = True

    class Process:
        def __init__(self):
            self.stdin = BrokenStdin()
            self.returncode = None
            self.terminated = self.killed = False

        def terminate(self):
            self.terminated = True
            self.returncode = 143

        def kill(self):
            self.killed = True
            self.returncode = 137

        async def wait(self):
            return self.returncode

    process = Process()

    async def fake_create(*args, **kwargs):
        return process

    monkeypatch.setattr(mon.asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(BrokenPipeError):
        _run(mon.start_load_process(token="secret", url="ws://example/ws"))
    assert process.stdin.closed
    assert process.terminated or process.killed
    assert process.returncode is not None


def test_monitor_reuses_the_shared_abort_rules():
    from scripts import ws_auth_load
    assert mon.evaluate_server_abort is ws_auth_load.evaluate_server_abort


# ── CLI 진입점이 실재하는가 ─────────────────────────────────────────────────


def test_cli_entry_point_exists_and_dry_run_describes_the_real_wiring():
    """⛔ 한때 이 파일은 **수집기에서 끝났다** — CLI · 부하 자식 · stop · rollback 이 전부
    없는데 문서에는 "배선 완료"라고 적혀 있었다."""
    for name in ("main", "start_load_process", "EnvRestorer", "preflight", "LoadProcess"):
        assert hasattr(mon, name), f"{name} 이 없다 — 실행기가 아니다"
    assert mon.main(["--url", "ws://127.0.0.1:18000/ws", "--token-stdin", "--dry-run"]) == 0


# ── env 소유권: 정확한 복원 ─────────────────────────────────────────────────


ENV_TEXT = (
    "# 주석\n"
    "OTHER=keep\n"
    "TOPIC_DISPATCHER_ENABLED=false\n"
    "WS_AUTH_EXECUTOR_LOG_TIMINGS=false\n"
)


def test_env_read_distinguishes_absent_key_from_empty_value():
    """⚠️ 부재를 `""` 로 접으면 복원 때 **없던 키가 생긴다**."""
    values = mon.read_env_values("A=\nB=x\n", ["A", "B", "C"])
    assert values == {"A": "", "B": "x", "C": None}


def test_env_apply_adds_absent_keys_and_removes_on_none():
    """⛔ `sed 's/^KEY=.*/KEY=v/'` 는 **키가 없으면 아무 일도 안 하면서 성공처럼 보인다**."""
    added = mon.apply_env_values(ENV_TEXT, {"DEFAULT_EXECUTOR_PROBE_ENABLED": "true"})
    assert "DEFAULT_EXECUTOR_PROBE_ENABLED=true" in added
    assert "OTHER=keep" in added, "무관한 키를 잃었다"

    removed = mon.apply_env_values(added, {"DEFAULT_EXECUTOR_PROBE_ENABLED": None})
    assert "DEFAULT_EXECUTOR_PROBE_ENABLED" not in removed, "키 제거가 되지 않았다"


def test_env_apply_removes_duplicate_keys_instead_of_leaving_a_later_override():
    text = "TOPIC_DISPATCHER_ENABLED=false\nTOPIC_DISPATCHER_ENABLED=true\n"
    applied = mon.apply_env_values(text, {"TOPIC_DISPATCHER_ENABLED": "true"})
    assert applied.count("TOPIC_DISPATCHER_ENABLED=") == 1
    assert "TOPIC_DISPATCHER_ENABLED=true" in applied


def test_atomic_env_write_keeps_the_old_file_if_replace_fails(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OLD=present\n")

    def fail_replace(source, target):
        raise OSError("교체 실패")

    monkeypatch.setattr(mon.os, "replace", fail_replace)
    with pytest.raises(OSError):
        mon.atomic_write_text(env, "NEW=value\n")
    assert env.read_text() == "OLD=present\n", "실패 중 기존 env가 잘리거나 바뀌었다"
    assert list(tmp_path.glob("..env.*")) == [], "실패한 임시 파일이 남았다"


@pytest.mark.parametrize("key", [
    "TOPIC_DISPATCHER_ENABLED",
    "KRX_CLIENT_DISTRIBUTION_ENABLED",
])
def test_canary_refuses_to_start_when_a_release_flag_is_already_true(key):
    values = {name: None for name in mon.CANARY_ENV}
    values[key] = "true"
    assert any(key in problem for problem in mon.validate_canary_start_env(values))


def test_restore_returns_the_file_to_its_exact_original_bytes(tmp_path, monkeypatch):
    """⛔ **키 단위 복원으로는 부족하다.** 한때 수동 절차로 `sed 's/^KEY=.*/KEY=false/'` 를
    적어 뒀는데 (1) 키가 없으면 **아무 일도 안 하면서 성공처럼 보이고** (2) 원자적이지 않으며
    (3) canary 가 바꾼 **나머지 키를 되돌리지 않는다**. 자동·수동 복원이 서로 다른 기준을 쓰면
    어긋나는 순간을 아무도 못 잡는다 → **파일 전체 backup 하나**로 통일한다."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=true\nOTHER=keep\n")
    original = env.read_text()

    async def noop(target=None):
        return None

    monkeypatch.setattr(mon, "recreate_and_verify_health", noop)
    mon.create_env_backup(env)
    mon.atomic_write_text(env, mon.apply_env_values(env.read_text(), dict(mon.CANARY_ENV)))
    assert "KRX_CLIENT_DISTRIBUTION_ENABLED" in env.read_text(), "전제: canary 가 키를 추가했다"

    _run(mon.EnvRestorer(env).restore())
    assert env.read_text() == original, "원본과 바이트 동일하게 복원되지 않았다"
    assert not mon.backup_path_for(env).exists(), "검증 후 backup 이 정리되지 않았다"


def test_backup_is_created_before_mutation_with_owner_only_mode(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SECRET=value\n")
    backup = mon.create_env_backup(env)
    assert backup.read_text() == "SECRET=value\n"
    assert oct(backup.stat().st_mode & 0o777) == "0o600", "backup 이 다른 사용자에게 열려 있다"


def test_second_start_is_refused_while_a_backup_remains(tmp_path):
    """⛔ backup 이 남아 있다 = **이전 canary 가 복구되지 않았다**. 그 위에 또 시작하면
    원본이 덮여 영영 되돌릴 수 없다."""
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    mon.create_env_backup(env)
    with pytest.raises(mon.CollectorError) as caught:
        mon.create_env_backup(env)
    assert "--recover" in str(caught.value), "복구 수단을 안내하지 않는다"


def test_backup_survives_so_a_new_process_can_recover(tmp_path, monkeypatch):
    """⛔ **SIGKILL 은 in-memory cleanup 을 통째로 건너뛴다.** 그때 남는 것은 이 파일뿐이고,
    새 프로세스의 `--recover` 가 그것만으로 되돌릴 수 있어야 한다."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\nKEEP=1\n")
    original = env.read_text()
    mon.create_env_backup(env)
    mon.atomic_write_text(env, mon.apply_env_values(env.read_text(), dict(mon.CANARY_ENV)))
    # ← 여기서 프로세스가 죽었다고 본다(restorer 인스턴스는 사라졌다)

    async def noop(target=None):
        return None

    monkeypatch.setattr(mon, "recreate_and_verify_health", noop)
    _run(mon.EnvRestorer(env).restore())          # 새 프로세스의 --recover 와 같은 경로
    assert env.read_text() == original
    assert not mon.backup_path_for(env).exists()


def test_backup_is_kept_when_health_verification_fails(tmp_path, monkeypatch):
    """⛔ 먼저 지우면 **재시도 수단이 사라진다** — 검증이 끝난 뒤에만 지운다."""
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    mon.create_env_backup(env)
    mon.atomic_write_text(env, "A=2\n")

    async def failing(target=None):
        raise mon.CollectorError("재기동 실패")

    monkeypatch.setattr(mon, "recreate_and_verify_health", failing)
    with pytest.raises(mon.CollectorError):
        _run(mon.EnvRestorer(env).restore())
    assert mon.backup_path_for(env).exists(), "복원 실패인데 backup 을 지웠다"


def test_restorer_is_idempotent(tmp_path, monkeypatch):
    """⚠️ monitor 와 바깥 `finally` 가 **둘 다** 부른다 — 두 번 재기동하면 창만 길어진다."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    mon.create_env_backup(env)
    calls = []

    async def counting(target=None):
        calls.append(1)

    monkeypatch.setattr(mon, "recreate_and_verify_health", counting)
    restorer = mon.EnvRestorer(env)
    _run(restorer.restore())
    _run(restorer.restore())
    assert len(calls) == 1, "복원이 두 번 재기동했다"


def test_recover_is_a_standalone_mode(tmp_path):
    """⚠️ 복원은 **토큰도 부하도 필요 없다**."""
    # ⚠️ `_cli` 헬퍼는 리허설 기본값(`inject_abort_after=3`)을 넣으므로 명시적으로 끈다.
    assert _cli(recover=True, token_stdin=False, inject_abort_after=None) == []
    assert any("단독" in p for p in _cli(recover=True, inject_abort_after=None,
                                        rehearse=True, container="c", project="p"))
    assert any("단독" in p for p in _cli(recover=True, inject_abort_after=2))


# ── preflight: 전제가 실제로 적용됐는가 ─────────────────────────────────────


def _preflight_responses(**overrides):
    env = {k: v for k, v in mon.CANARY_ENV.items()}
    payloads = {
        "env": env,
        "/admin/api/default-executor-probe": {"enabled": True, "running": True, "metrics": {}},
        "/admin/api/ws-auth-executor-metrics": {"running": True, "metrics": {"max_workers": 4}},
        "/admin/api/broadcast-heartbeat": {"age_seconds": 3.0},
    }
    payloads.update(overrides)
    return payloads


def _patch_preflight(monkeypatch, payloads, *, project_label="prod"):
    async def fake_run(command, *, timeout=None, cwd=None):
        if command[0] == "docker" and command[1] == "inspect":
            return json.dumps({
                "com.docker.compose.project": project_label,
                "com.docker.compose.service": "fastapi",
            }) + "\n"
        if command[-1].startswith("/admin"):
            return json.dumps(payloads[command[-1]]) + "\n200"
        return json.dumps(payloads["env"]) + "\n200"      # container_env
    monkeypatch.setattr(mon, "run_command", fake_run)


def test_preflight_passes_when_every_precondition_holds(monkeypatch):
    _patch_preflight(monkeypatch, _preflight_responses())
    assert _run(mon.preflight()) == []


@pytest.mark.parametrize("overrides,expected", [
    ({"/admin/api/default-executor-probe": {"enabled": False, "running": False}}, "enabled"),
    ({"/admin/api/default-executor-probe": {"enabled": True, "running": False}}, "running=false"),
    ({"/admin/api/ws-auth-executor-metrics": {"running": False,
                                                "metrics": {"max_workers": 4}}},
     "auth executor running=false"),
    ({"/admin/api/ws-auth-executor-metrics": {"metrics": {"max_workers": 8}}}, "max_workers"),
    ({"/admin/api/broadcast-heartbeat": {"age_seconds": 99.0}}, "heartbeat"),
    ({"/admin/api/broadcast-heartbeat": {"age_seconds": None}}, "heartbeat"),
])
def test_preflight_catches_each_precondition(monkeypatch, overrides, expected):
    """⛔ probe 가 꺼진 채 canary 가 돌면 **빈 지표를 성공으로** 읽는다 — 반복된 false green."""
    _patch_preflight(monkeypatch, _preflight_responses(**overrides))
    problems = _run(mon.preflight())
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize("key", list(mon.CANARY_ENV))
def test_preflight_catches_each_unapplied_env_key(monkeypatch, key):
    env = dict(mon.CANARY_ENV)
    env[key] = "somethingelse"
    _patch_preflight(monkeypatch, _preflight_responses(env=env))
    assert any(key in p for p in _run(mon.preflight()))


def test_preflight_waits_for_the_first_scheduler_tick_without_hiding_the_result(monkeypatch):
    results = [["broadcast heartbeat 이 신선하지 않다: None"], []]
    sleeps = []

    async def fake_preflight(target=None, *, url=None):
        return results.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(mon, "preflight", fake_preflight)
    monkeypatch.setattr(mon.asyncio, "sleep", fake_sleep)
    assert _run(mon.wait_for_preflight(attempts=2, retry_seconds=0.25)) == []
    assert sleeps == [0.25]


def test_preflight_returns_the_last_failure_after_the_bounded_grace(monkeypatch):
    calls = []

    async def always_bad(target=None, *, url=None):
        calls.append(1)
        return ["probe running=false"]

    async def no_wait(seconds):
        return None

    monkeypatch.setattr(mon, "preflight", always_bad)
    monkeypatch.setattr(mon.asyncio, "sleep", no_wait)
    assert _run(mon.wait_for_preflight(attempts=3)) == ["probe running=false"]
    assert len(calls) == 3


def test_krx_distribution_is_forced_off_during_the_window():
    """⛔ "원래 꺼져 있겠지"로 두면 누가 켜 둔 상태에서 유료 데이터가 새는 창이 된다."""
    assert mon.CANARY_ENV["KRX_CLIENT_DISTRIBUTION_ENABLED"] == "false"


# ── poll interval ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, 0.5, float("nan"), float("inf")])
def test_bad_poll_interval_is_rejected(bad):
    """⛔ hot loop 면 **watchdog 자신이 부하원**이 된다(tick 마다 docker subprocess 6개)."""
    with pytest.raises(ValueError):
        mon.validate_poll_interval(bad)


def test_default_poll_interval_is_accepted():
    assert mon.validate_poll_interval(mon.DEFAULT_POLL_INTERVAL) == 1.0


# ── _amain: env 를 건드린 순간부터 복원이 소유한다 ──────────────────────────


class _AmainArgs:
    def __init__(self, env_file, **kw):
        self.env_file = str(env_file)
        self.url = "ws://127.0.0.1:18000/ws"      # ⛔ 기본값 없음 — 명시해야 한다
        self.token_stdin = True
        self.token_file = None
        self.poll_interval = 1.0
        self.command_timeout = mon.COMMAND_TIMEOUT_SECONDS
        self.container = mon.PRODUCTION_CONTAINER
        self.project = None
        self.compose_file = None
        self.rehearse = False
        self.inject_abort_after = None
        self.recover = False
        self.dry_run = False
        self.__dict__.update(kw)


def _patch_amain(monkeypatch, tmp_path, *, start_error=None, preflight=None,
                 recreate_error=None):
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\nOTHER=keep\n")
    calls = {"recreate": 0, "started": 0}

    async def fake_recreate(target=None):
        calls["recreate"] += 1
        if recreate_error and calls["recreate"] == 1:
            raise recreate_error

    async def fake_run(command, *, timeout=None, cwd=None):
        if command[0] == "docker" and command[1] == "inspect" and "Config.Labels" in command[-1]:
            return json.dumps({
                "com.docker.compose.project": "exchange-rate",
                "com.docker.compose.service": "fastapi",
            }) + "\n"
        return "STARTED_AT\n"

    async def fake_preflight(target=None, **kw):
        return list(preflight or [])

    async def fake_start(**kw):
        calls["started"] += 1
        if start_error:
            raise start_error
        raise AssertionError("이 테스트는 여기까지 오면 안 된다")

    monkeypatch.setattr(mon, "recreate_and_verify_health", fake_recreate)
    monkeypatch.setattr(mon, "run_command", fake_run)
    monkeypatch.setattr(mon, "wait_for_preflight", fake_preflight)
    monkeypatch.setattr(mon, "start_load_process", fake_start)
    monkeypatch.setattr(mon, "load_id_token", lambda **kw: "tok")
    return env, calls


def test_env_is_restored_when_the_child_cannot_be_started(monkeypatch, tmp_path):
    """⛔ 한때 monitor 는 flag 를 **켜지 않으면서** rollback 만 했다 — 운영자가 먼저 켜고
    자식 생성이 실패하면 **켜진 채 남았다**. 이제 env 를 건드린 순간부터 복원이 소유한다."""
    env, calls = _patch_amain(monkeypatch, tmp_path, start_error=BrokenPipeError("stdin 파열"))
    with pytest.raises(BrokenPipeError):
        _run(mon._amain(_AmainArgs(env)))
    text = env.read_text()
    assert "TOPIC_DISPATCHER_ENABLED=false" in text, "자식 생성 실패 후 flag 가 켜진 채 남았다"
    assert "KRX_CLIENT_DISTRIBUTION_ENABLED" not in text, "원래 없던 키가 남았다"
    assert calls["started"] == 1


def test_preflight_failure_creates_zero_load_processes_and_restores(monkeypatch, tmp_path):
    """⛔ 전제가 안 맞으면 **부하는 0건** 생성이다 — 켠 채로 두고 사람이 판단하게 하지 않는다."""
    env, calls = _patch_amain(monkeypatch, tmp_path, preflight=["probe running=false"])
    assert _run(mon._amain(_AmainArgs(env))) == 1
    assert calls["started"] == 0, "preflight 실패인데 부하를 띄웠다"
    assert "TOPIC_DISPATCHER_ENABLED=false" in env.read_text()


def test_already_enabled_topic_flag_creates_no_load_and_does_not_recreate(monkeypatch, tmp_path):
    env, calls = _patch_amain(monkeypatch, tmp_path)
    env.write_text("TOPIC_DISPATCHER_ENABLED=true\nOTHER=keep\n")
    assert _run(mon._amain(_AmainArgs(env))) == 1
    assert calls == {"recreate": 0, "started": 0}
    assert "TOPIC_DISPATCHER_ENABLED=true" in env.read_text()


def test_restart_baseline_is_captured_after_the_intentional_recreate(monkeypatch, tmp_path):
    """재생성 전 StartedAt을 baseline으로 쓰면 첫 poll이 canary 자체의 재생성을 장애로 본다."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    events = []
    writes = []

    class Load:
        async def kill(self):
            events.append("kill")

    async def fake_recreate(target=None):
        events.append("recreate")

    async def fake_preflight(target=None, *, url=None):
        events.append("preflight")
        return []

    async def fake_run(command, *, timeout=None, cwd=None):
        events.append("inspect")
        if "Config.Labels" in command[-1]:
            return json.dumps({
                "com.docker.compose.project": "exchange-rate",
                "com.docker.compose.service": "fastapi",
            }) + "\n"
        return "AFTER_RECREATE\n"

    async def fake_start(**kwargs):
        events.append("start")
        return Load()

    async def fake_monitor(**kwargs):
        events.append(("baseline", kwargs["baseline_started_at"]))
        return mon.CanaryOutcome(False, [], 1, True, False, False)

    def fake_atomic_write(path, text, *, mode=None):
        writes.append(text)
        Path(path).write_text(text)

    monkeypatch.setattr(mon, "load_id_token", lambda **kwargs: "tok")
    monkeypatch.setattr(mon, "recreate_and_verify_health", fake_recreate)
    monkeypatch.setattr(mon, "wait_for_preflight", fake_preflight)
    monkeypatch.setattr(mon, "run_command", fake_run)
    monkeypatch.setattr(mon, "start_load_process", fake_start)
    monkeypatch.setattr(mon, "run_monitored_canary", fake_monitor)
    monkeypatch.setattr(mon, "atomic_write_text", fake_atomic_write)

    assert _run(mon._amain(_AmainArgs(env))) == 0
    started_at_inspect = [i for i, event in enumerate(events) if event == "inspect"][1]
    assert events.index("recreate") < started_at_inspect < events.index("start")
    assert ("baseline", "AFTER_RECREATE") in events
    # backup → 활성화 → 복원. 셋 다 원자적 write 경로를 타야 한다.
    assert len(writes) == 3, "backup·활성화·복원 중 하나가 원자적 write 경로를 우회했다"
    assert "TOPIC_DISPATCHER_ENABLED=true" not in writes[0], \
        "backup 이 활성화 **뒤에** 만들어졌다 — 그러면 되돌릴 원본이 이미 덮인 상태다"
    assert "TOPIC_DISPATCHER_ENABLED=true" in writes[1], "활성화가 두 번째 write 가 아니다"
    assert "TOPIC_DISPATCHER_ENABLED=false" in writes[2], "복원이 마지막 write 가 아니다"


def test_env_is_restored_when_recreate_fails_right_after_applying(monkeypatch, tmp_path):
    """⚠️ env 를 쓴 **직후** 재기동이 실패하는 창 — 여기서 복원이 없으면 파일만 바뀐 채 남는다."""
    env, calls = _patch_amain(monkeypatch, tmp_path,
                              recreate_error=mon.CollectorError("재기동 실패"))
    with pytest.raises(mon.CollectorError):
        _run(mon._amain(_AmainArgs(env)))
    assert "TOPIC_DISPATCHER_ENABLED=false" in env.read_text()
    assert calls["started"] == 0


def test_token_failure_happens_before_any_env_mutation(monkeypatch, tmp_path):
    """⚠️ 되돌릴 상태가 없도록 **env 를 건드리기 전에** 실패할 것들을 끝낸다."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    before = env.read_text()

    def boom(**kw):
        raise ValueError("토큰이 비어 있다")

    monkeypatch.setattr(mon, "load_id_token", boom)
    with pytest.raises(ValueError):
        _run(mon._amain(_AmainArgs(env)))
    assert env.read_text() == before, "토큰 실패인데 env 가 변경됐다"


def test_target_mismatch_happens_before_any_env_mutation_or_recreate(monkeypatch, tmp_path):
    """helper만 검사하지 않는다. `_amain`이 target 확인을 빼거나 env 변경 뒤로 옮기면 red다."""
    env = tmp_path / ".env.rehearsal"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    before = env.read_bytes()
    calls = {"recreate": 0}

    async def wrong_target(command, *, timeout=None, cwd=None):
        return json.dumps({
            "com.docker.compose.project": "production",
            "com.docker.compose.service": "fastapi",
        }) + "\n"

    async def recreate(target=None):
        calls["recreate"] += 1

    monkeypatch.setattr(mon, "run_command", wrong_target)
    monkeypatch.setattr(mon, "recreate_and_verify_health", recreate)

    args = _AmainArgs(
        env, rehearse=True, token_stdin=False, container="fxi-rehearsal-app",
        project="fxi-rehearsal", compose_file="docker-compose.rehearsal.yml",
        inject_abort_after=3,
    )
    assert _run(mon._amain(args)) == 1
    assert env.read_bytes() == before
    assert calls["recreate"] == 0


@pytest.mark.parametrize("bad", [0, float("nan")])
def test_amain_validates_poll_interval_before_touching_env(monkeypatch, tmp_path, bad):
    """⛔ 검증 함수만 테스트하면 `_amain` 에서 그 호출을 **지워도 통과한다**(실측 SURVIVED).
    hot loop 는 watchdog 자신을 부하원으로 만든다 — env 를 건드리기 전에 막아야 한다."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    before = env.read_text()
    monkeypatch.setattr(mon, "load_id_token", lambda **kw: "tok")

    with pytest.raises(ValueError):
        _run(mon._amain(_AmainArgs(env, poll_interval=bad)))
    assert env.read_text() == before, "잘못된 poll interval 인데 env 를 건드렸다"


# ── target 주입: 리허설이 운영 스택을 건드릴 수 없는가 ──────────────────────


def test_commands_are_derived_from_the_injected_target():
    """⛔ 한때 `docker compose ...` 와 `exchange-rate-app` 이 코드에 박혀 있었다 — 그 상태로
    "EC2 에 별도 compose 로 리허설" 을 제안했는데, 그러면 monitor 가 **운영 스택을 재생성**한다."""
    target = mon.Target(container="fxi-rehearsal-app", project="fxi-rehearsal",
                        compose_file="docker-compose.rehearsal.yml",
                        env_file=Path("/tmp/.env.rehearsal"))
    compose = target.compose("up", "-d")
    assert compose == [
        "docker", "compose", "--env-file", "/tmp/.env.rehearsal",
        "-p", "fxi-rehearsal", "-f", "docker-compose.rehearsal.yml", "up", "-d",
    ]
    assert target.inspect("{{.Id}}")[2] == "fxi-rehearsal-app"
    assert "exchange-rate-app" not in " ".join(mon.admin_fetch_command("/health", target))
    assert not target.is_production


def test_rehearsal_compose_is_standalone_and_uses_interpolated_environment():
    """리허설 project가 달라도 base compose를 병합하면 고정 포트·bind mount가 그대로 샌다.

    서비스 `env_file`도 금지한다. 실행기의 `--env-file`과 다른 파일을 서비스가 읽으면 flag를
    바꿨는데 컨테이너에는 반영되지 않는 false green이 된다.
    """
    import yaml

    compose = yaml.safe_load((mon.REPO_ROOT / "docker-compose.rehearsal.yml").read_text())
    assert set(compose["services"]) == {"redis", "fastapi"}
    fastapi = compose["services"]["fastapi"]
    redis = compose["services"]["redis"]
    assert "env_file" not in fastapi
    assert fastapi["ports"] == ["127.0.0.1:18000:8000"]
    assert "ports" not in redis
    assert all(not str(volume).startswith("./") for volume in fastapi["volumes"])
    rendered_env = "\n".join(fastapi["environment"])
    for key in mon.CANARY_ENV:
        assert f"{key}=${{{key}" in rendered_env


def test_rehearsal_env_template_is_secret_free_and_gitignored():
    template = (mon.REPO_ROOT / ".env.rehearsal.example").read_text()
    ignored = (mon.REPO_ROOT / ".gitignore").read_text().splitlines()
    assert ".env.rehearsal" in ignored
    assert "local-rehearsal-admin" in template
    assert "local-rehearsal-redis" in template
    assert "REVENUECAT_API_KEY=\n" in template
    assert "TOPIC_DISPATCHER_ENABLED=false" in template


def test_production_target_is_the_default_and_recognisable():
    assert mon.PRODUCTION_TARGET.is_production
    assert mon.PRODUCTION_TARGET.container == "exchange-rate-app"


def test_target_identity_is_verified_fail_closed(monkeypatch):
    """⛔ 확인 못 하면 **거부**한다 — 확인 없이 진행하면 리허설이 운영을 재생성할 수 있다."""
    target = mon.Target(container="c", project="want")

    async def wrong(command, *, timeout=None, cwd=None):
        return json.dumps({
            "com.docker.compose.project": "other",
            "com.docker.compose.service": "fastapi",
        }) + "\n"

    monkeypatch.setattr(mon, "run_command", wrong)
    assert any("project" in p for p in _run(mon.check_target_identity(target)))

    async def boom(command, *, timeout=None, cwd=None):
        raise mon.CollectorError("no such container")

    monkeypatch.setattr(mon, "run_command", boom)
    assert _run(mon.check_target_identity(target)), "확인 실패인데 통과시켰다"

    async def right(command, *, timeout=None, cwd=None):
        return json.dumps({
            "com.docker.compose.project": "want",
            "com.docker.compose.service": "fastapi",
        }) + "\n"

    monkeypatch.setattr(mon, "run_command", right)
    assert _run(mon.check_target_identity(target)) == []

    async def wrong_service(command, *, timeout=None, cwd=None):
        return json.dumps({
            "com.docker.compose.project": "want",
            "com.docker.compose.service": "redis",
        }) + "\n"

    monkeypatch.setattr(mon, "run_command", wrong_service)
    assert any("fastapi" in p for p in _run(mon.check_target_identity(target)))


# ── 리허설/운영 구조적 분리 ────────────────────────────────────────────────


def _cli(env_file="/tmp/.env.rehearsal", **kw):
    defaults = {
        "compose_file": "docker-compose.rehearsal.yml",
        "inject_abort_after": 3,
    }
    defaults.update(kw)
    return mon.validate_cli_combo(_AmainArgs(env_file, **defaults))


def test_rehearsal_cannot_target_production():
    """⛔ 가짜 부하·주입 중단이 **운영으로 새면 안 된다**."""
    problems = _cli(rehearse=True, container=mon.PRODUCTION_CONTAINER, project="p")
    assert any("운영 컨테이너" in p for p in problems)


def test_rehearsal_requires_an_explicit_project():
    assert any("--project" in p for p in _cli(rehearse=True, container="other"))


def test_injected_abort_is_rehearsal_only():
    """⛔ 운영 canary 에서 중단을 **주입**할 수 있으면 그 창의 결과는 의미가 없다."""
    assert any("--rehearse 전용" in p for p in _cli(inject_abort_after=2,
                                                           compose_file=None))


def test_real_canary_requires_a_token():
    assert any("토큰" in p for p in _cli(token_stdin=False, token_file=None))


def test_valid_rehearsal_combo_passes():
    assert _cli(rehearse=True, container="fxi-rehearsal-app", project="fxi-rehearsal",
                token_stdin=False) == []


@pytest.mark.parametrize("value", [None, 0, -1])
def test_rehearsal_requires_a_positive_injected_abort_poll(value):
    problems = _cli(rehearse=True, container="fxi-rehearsal-app", project="fxi-rehearsal",
                    token_stdin=False, inject_abort_after=value)
    assert any("inject-abort-after" in p for p in problems)


def test_injected_abort_flips_health_after_n_polls():
    """⚠️ 실제 서비스를 깨지 않고 **순서**(중단 → 부하 종료 → env 복원)를 증명하는 수단."""
    async def inner():
        return mon.Snapshot()

    collect = mon.injected_abort_collector(inner, 2)
    assert _run(collect()).health_ok is True
    injected = _run(collect())
    assert injected.health_ok is False
    assert injected.injected_health_failure is True
    assert mon.evaluate_snapshot_abort(injected, baseline_started_at=None) == [
        "health 실패 (injected)"]


def _rehearsal_outcome(**changes):
    values = {
        "aborted": True,
        "reasons": ["health 실패 (injected)"],
        "polls": 3,
        "load_stopped": True,
        "rollback_done": True,
        "load_alive_after_cleanup": False,
        "cleanup_errors": [],
    }
    values.update(changes)
    return mon.CanaryOutcome(**values)


def test_rehearsal_success_requires_the_expected_injected_abort_and_full_cleanup():
    assert mon.rehearsal_succeeded(_rehearsal_outcome(), expected_abort_poll=3)
    assert not mon.rehearsal_succeeded(
        _rehearsal_outcome(reasons=["컨테이너 재시작"]), expected_abort_poll=3)
    assert not mon.rehearsal_succeeded(_rehearsal_outcome(polls=2), expected_abort_poll=3)
    assert not mon.rehearsal_succeeded(_rehearsal_outcome(polls=4), expected_abort_poll=3)
    assert not mon.rehearsal_succeeded(
        _rehearsal_outcome(reasons=["health 실패"]), expected_abort_poll=3)
    assert not mon.rehearsal_succeeded(
        _rehearsal_outcome(rollback_done=False), expected_abort_poll=3)
    assert not mon.rehearsal_succeeded(
        _rehearsal_outcome(cleanup_errors=["kill 실패"]), expected_abort_poll=3)


@pytest.mark.parametrize("reasons,expected", [
    (["health 실패 (injected)"], 0),
    (["health 실패"], 1),
    (["컨테이너 재시작"], 1),
])
def test_amain_rehearsal_exit_code_uses_the_exact_injected_abort_contract(
        monkeypatch, tmp_path, reasons, expected):
    """helper가 아니라 CLI 출구까지 잠근다. 다른 중단을 리허설 성공으로 접지 않는다."""
    env = tmp_path / ".env.rehearsal"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")

    class Load:
        async def kill(self):
            return None

    async def fake_run(command, *, timeout=None, cwd=None):
        if "Config.Labels" in command[-1]:
            return json.dumps({
                "com.docker.compose.project": "fxi-rehearsal",
                "com.docker.compose.service": "fastapi",
            }) + "\n"
        return "AFTER_RECREATE\n"

    async def no_op(*args, **kwargs):
        return None

    async def no_problems(*args, **kwargs):
        return []

    async def start():
        return Load()

    async def monitor(**kwargs):
        return _rehearsal_outcome(reasons=reasons)

    monkeypatch.setattr(mon, "run_command", fake_run)
    monkeypatch.setattr(mon, "recreate_and_verify_health", no_op)
    monkeypatch.setattr(mon, "wait_for_preflight", no_problems)
    monkeypatch.setattr(mon, "start_rehearsal_load", start)
    monkeypatch.setattr(mon, "run_monitored_canary", monitor)

    args = _AmainArgs(
        env, rehearse=True, token_stdin=False, container="fxi-rehearsal-app",
        project="fxi-rehearsal", compose_file="docker-compose.rehearsal.yml",
        inject_abort_after=3,
    )
    assert _run(mon._amain(args)) == expected


def test_amain_requires_the_nominal_plan_duration_for_a_real_canary(monkeypatch, tmp_path):
    """helper 기본값 0에 기대면 `_amain`에서 배선을 빼도 조기 exit 0이 다시 성공한다."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    observed = []

    class Load:
        async def kill(self):
            return None

    async def fake_run(command, *, timeout=None, cwd=None):
        if "Config.Labels" in command[-1]:
            return json.dumps({
                "com.docker.compose.project": "exchange-rate",
                "com.docker.compose.service": "fastapi",
            }) + "\n"
        return "AFTER_RECREATE\n"

    async def no_op(*args, **kwargs):
        return None

    async def no_problems(*args, **kwargs):
        return []

    async def start(**kwargs):
        return Load()

    async def monitor(**kwargs):
        observed.append((kwargs["minimum_clean_exit_seconds"], kwargs["load_started_at"]))
        return mon.CanaryOutcome(False, [], 1, True, True, False)

    monkeypatch.setattr(mon, "load_id_token", lambda **kwargs: "tok")
    monkeypatch.setattr(mon, "run_command", fake_run)
    monkeypatch.setattr(mon, "recreate_and_verify_health", no_op)
    monkeypatch.setattr(mon, "wait_for_preflight", no_problems)
    monkeypatch.setattr(mon, "start_load_process", start)
    monkeypatch.setattr(mon, "run_monitored_canary", monitor)

    assert _run(mon._amain(_AmainArgs(env))) == 0
    assert len(observed) == 1
    minimum, load_started_at = observed[0]
    assert minimum == sum(phase.seconds for phase in mon.PHASES)
    assert isinstance(load_started_at, float) and load_started_at > 0


def test_url_has_no_default_so_an_unreachable_stack_is_not_assumed():
    """⛔ compose 의 fastapi 는 `expose` 만 있고 host port 를 publish 하지 않는다 —
    기본 URL 을 두면 **닿지 않는 주소로 리허설하고 rollback 만 검증**하게 된다."""
    parser = mon.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--rehearse", "--project", "p", "--container", "c"])
    parsed = parser.parse_args(["--url", "ws://127.0.0.1:18000/ws"])
    assert parsed.url == "ws://127.0.0.1:18000/ws"


def test_ws_reachability_failure_is_a_preflight_problem(monkeypatch):
    problems = _run(mon.check_ws_reachable("ws://127.0.0.1:1/ws", timeout=0.5))
    assert problems and "연결할 수 없다" in problems[0]


def test_preflight_actually_checks_the_target_identity(monkeypatch):
    """⛔ helper 만 테스트하면 `preflight()` 에서 그 호출을 **지워도 통과한다**(실측 SURVIVED).
    그 상태면 리허설이 운영 스택을 재생성해도 preflight 가 막지 못한다."""
    _patch_preflight(monkeypatch, _preflight_responses(), project_label="somewhere-else")
    problems = _run(mon.preflight(mon.Target(container="c", project="fxi-rehearsal")))
    assert any("project" in p for p in problems), problems


def test_preflight_actually_checks_ws_reachability(monkeypatch):
    """⛔ 같은 이유 — 도달성 호출을 지우면 **닿지 않는 주소로 리허설**하고 rollback 만 검증한다."""
    _patch_preflight(monkeypatch, _preflight_responses())
    assert _run(mon.preflight(mon.PRODUCTION_TARGET)) == [], "전제: url 없으면 도달성은 보지 않는다"

    problems = _run(mon.preflight(mon.PRODUCTION_TARGET, url="ws://127.0.0.1:1/ws"))
    assert any("연결할 수 없다" in p for p in problems), problems


def test_rehearsal_refuses_to_edit_the_production_env_file():
    """⛔ 실행기는 env 파일을 **직접 고쳐 쓰고 재기동**한다. 기본값이 운영 `.env` 라,
    리허설이 그걸 그대로 쓰면 컨테이너만 격리되고 **설정은 운영을 건드린다**."""
    problems = _cli(str(mon.REPO_ROOT / ".env"), rehearse=True,
                    container="fxi-rehearsal-app", project="p", token_stdin=False)
    assert any(".env" in p for p in problems), problems

    assert _cli(rehearse=True, container="fxi-rehearsal-app", project="p",
                token_stdin=False) == []


# ── 리허설 스택이 정말 격리돼 있는가 (compose 정적 검사) ────────────────────


def _rehearsal_compose() -> dict:
    import yaml
    return yaml.safe_load((mon.REPO_ROOT / "docker-compose.rehearsal.yml").read_text())


def test_rehearsal_stack_never_binds_production_paths():
    """⛔ **실측으로 확인한 위험이다.** 리허설 구성을 기본 compose 와 **병합**하면
    `./data`(운영 SQLite) · `volumes/logs/app` · `firebase-service-account.json` 을 그대로
    bind mount 하고, `ports` 는 **덮이지 않고 이어붙어** redis 가 `6379` 와 `16379` 를 **둘 다**
    물려 한다(`docker compose config` 로 재현). 그래서 이 파일은 **독립 스택**이어야 한다."""
    config = _rehearsal_compose()
    rendered = str(config)
    for forbidden in ("./data", "firebase-service-account.json", "volumes/logs"):
        assert forbidden not in rendered, f"운영 경로를 mount 한다: {forbidden}"

    for service in config["services"].values():
        for volume in service.get("volumes", []):
            source = volume.split(":")[0] if isinstance(volume, str) else volume.get("source", "")
            assert not source.startswith("."), f"bind mount 가 남아 있다: {volume}"


def test_rehearsal_stack_publishes_only_loopback_18000():
    """⛔ 운영 포트(6379·80·443)를 물면 리허설이 운영과 자원을 다툰다."""
    published = [p for service in _rehearsal_compose()["services"].values()
                 for p in service.get("ports", [])]
    assert published == ["127.0.0.1:18000:8000"], published


def test_rehearsal_containers_are_named_apart_from_production():
    names = [s.get("container_name") for s in _rehearsal_compose()["services"].values()]
    assert mon.PRODUCTION_CONTAINER not in names
    assert all(name and name.startswith("fxi-rehearsal-") for name in names)


# ── 재기동 뒤 health 대기 (리허설에서 실측된 결함) ──────────────────────────


def test_health_check_waits_for_the_app_to_start_listening(monkeypatch):
    """⛔ **리허설을 실제로 돌려서 나온 결함이다.** `docker compose up -d` 는 컨테이너가
    *시작*되면 반환하고 앱은 아직 listen 하지 않는다 — 즉시 조회하면 연결 거부가 나는데,
    그걸 health 실패로 접으면 **정상 재기동을 장애로 판정**한다(운영에서는 rollback 이
    "되돌리지 못했다"로 보고된다)."""
    attempts = {"n": 0}

    async def flaky(command, *, timeout=None, cwd=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise mon.CollectorError("exit=1 docker: ConnectionRefusedError")
        return json.dumps({"status": "healthy"}) + "\n200"

    monkeypatch.setattr(mon, "run_command", flaky)
    _run(mon.wait_until_healthy(mon.PRODUCTION_TARGET, timeout=5, poll_seconds=0.01))
    assert attempts["n"] == 3, "기동 중 연결 거부를 즉시 장애로 판정했다"


def test_health_wait_is_bounded_and_still_fails_closed(monkeypatch):
    """⚠️ 기다리되 **무한정은 아니다** — 영구 장애는 시간으로 갈라야 한다."""
    async def never(command, *, timeout=None, cwd=None):
        raise mon.CollectorError("ConnectionRefused")

    monkeypatch.setattr(mon, "run_command", never)
    with pytest.raises(mon.CollectorError) as caught:
        _run(mon.wait_until_healthy(mon.PRODUCTION_TARGET, timeout=0.05, poll_seconds=0.01))
    assert "안에 health 가 정상이 되지 않았다" in str(caught.value)


def test_health_wait_never_gives_one_command_more_than_the_remaining_budget(monkeypatch):
    """전체 5초 창인데 개별 command timeout 30초를 주면 `timeout=5` 계약이 거짓이 된다."""
    clock = iter([100.0, 101.0, 102.0, 104.5, 105.0])
    seen = []

    async def fail(command, *, timeout=None, cwd=None):
        seen.append(timeout)
        raise mon.CollectorError("ConnectionRefused")

    async def no_sleep(seconds):
        return None

    original = mon._command_timeout
    try:
        mon._command_timeout = 30.0
        monkeypatch.setattr(mon, "run_command", fail)
        monkeypatch.setattr(mon.asyncio, "sleep", no_sleep)
        with pytest.raises(mon.CollectorError):
            _run(mon.wait_until_healthy(
                timeout=5, poll_seconds=2, clock=lambda: next(clock)))
    finally:
        mon._command_timeout = original
    assert seen == [4.0, 0.5]


def test_unhealthy_status_within_the_window_is_not_accepted(monkeypatch):
    async def unhealthy(command, *, timeout=None, cwd=None):
        return json.dumps({"status": "degraded"}) + "\n200"

    monkeypatch.setattr(mon, "run_command", unhealthy)
    with pytest.raises(mon.CollectorError):
        _run(mon.wait_until_healthy(mon.PRODUCTION_TARGET, timeout=0.05, poll_seconds=0.01))


@pytest.mark.parametrize("bad", [0, -1, 0.5, float("nan"), float("inf")])
def test_bad_command_timeout_is_rejected(bad):
    """⛔ 0/NaN 이면 모든 호출이 즉시 실패해 canary 가 시작조차 못 한다."""
    with pytest.raises(ValueError):
        mon.set_command_timeout(bad)


def test_command_timeout_is_a_policy_value_not_a_constant(monkeypatch):
    """⚠️ 목적은 "호출 하나가 멈춰 watchdog 이 얼지 않게"뿐이라 느린 환경에서는 키울 수 있어야
    한다 — 에뮬레이션 리허설 스택에서 재기동 직후 exec 가 8초를 넘겼다(실측). 운영 기본은 유지."""
    original = mon._command_timeout
    seen = []
    real_wait_for = mon.asyncio.wait_for

    async def record(awaitable, timeout):
        seen.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    try:
        mon.set_command_timeout(30)
        monkeypatch.setattr(mon.asyncio, "wait_for", record)
        assert _run(mon.run_command([sys.executable, "-c", "print('ok')"])).strip() == "ok"
        assert mon._command_timeout == 30
        assert mon.COMMAND_TIMEOUT_SECONDS == 8.0, "운영 기본값이 바뀌었다"
    finally:
        mon._command_timeout = original
    assert seen == [30.0], "정책값이 실제 subprocess 대기에 전달되지 않았다"


def test_amain_applies_the_command_timeout_before_touching_env(monkeypatch, tmp_path):
    """⛔ CLI 가 값을 받아도 **적용하지 않으면** 의미가 없다(helper-vs-배선)."""
    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\n")
    before = env.read_text()
    monkeypatch.setattr(mon, "load_id_token", lambda **kw: "tok")

    with pytest.raises(ValueError):
        _run(mon._amain(_AmainArgs(env, command_timeout=0)))
    assert env.read_text() == before, "잘못된 command timeout 인데 env 를 건드렸다"


def test_final_snapshot_failure_is_not_folded_into_success():
    """⛔ **마지막 순간의 fail-open 방지.** 정상 종료를 받아들이기 직전 최종 수집이 실패했는데
    그걸 삼키면, canary 는 **끝 상태를 읽지 못한 채** 성공을 보고한다 — 하필 종료 직전 장애가
    생기는 구간이다. (프로덕션은 이미 fail-closed 였고, 비어 있던 것은 **이 분기의 테스트**다.)

    ⚠️ 순서를 **이벤트로 고정**한다. 한때 `_FakeLoad(seconds=0.02)` 의 타이밍에 기댔는데,
    `wait()` 가 반환 **전에** `alive=False` 로 만들어 `wait_task.done()` 이 아직 False 인 창이
    생겼다 — 그 창에 폴링 collect 가 먼저 실패해 **다른 경로**를 짚었다(isolation 통과·전체
    실행 실패의 전형적 부하 의존 flake)."""
    first_poll_done = asyncio.Event()

    class _ExitAfterFirstPoll(_FakeLoad):
        async def wait(self):
            await first_poll_done.wait()          # 첫 poll 이 끝난 뒤에만 종료한다
            self.alive = False
            return 0

    class _FailOnFinalCollect(_Harness):
        async def collect(self):
            await asyncio.sleep(0)
            if first_poll_done.is_set():          # = 정상 종료 직후의 **최종** 수집
                raise mon.CollectorError("401")
            first_poll_done.set()
            return mon.Snapshot()

    load = _ExitAfterFirstPoll()
    harness = _FailOnFinalCollect([])
    outcome = _run(mon.run_monitored_canary(
        collect=harness.collect, load=load, rollback=harness.rollback,
        poll_interval=0.001, emit=lambda _: None))

    assert outcome.aborted, "최종 수집 실패인데 성공으로 보고했다"
    assert any("최종 지표 수집 실패" in r for r in outcome.reasons), outcome.reasons
    assert not outcome.ok and "rollback" in harness.calls


# ── SSH 단절(SIGHUP)·SIGTERM 에서도 rollback 이 도는가 ──────────────────────


def test_repeated_signals_do_not_cancel_the_cleanup():
    """⛔ **두 번째 취소가 rollback 을 끊는다.** cleanup 은 이미 취소된 task 의 `finally` 안에서
    `await` 하는데, 거기로 취소가 또 오면 그 await 가 끊겨 **flag 를 되돌리는 경로가 사라진다**."""
    cancels = []

    class _Task:
        def cancel(self):
            cancels.append(1)

    received: dict = {"signum": None}
    handle = mon.make_signal_canceller(_Task(), received)
    handle(15)
    handle(1)
    handle(15)
    assert cancels == [1], "반복 signal 이 cleanup 을 다시 취소했다"
    assert received["signum"] == 15, "첫 signal 이 기록되지 않았다"


def test_sighup_and_sigterm_are_both_handled():
    """⚠️ SSH 단절은 **SIGHUP** 이다 — SIGTERM 만 막으면 정작 그 경로가 열려 있다."""
    import signal as _signal
    assert _signal.SIGTERM in mon.CANCEL_ON_SIGNALS
    assert _signal.SIGHUP in mon.CANCEL_ON_SIGNALS


_SIGNAL_CHILD = """
import asyncio, pathlib, sys
sys.path.insert(0, {repo!r})
from scripts import canary_monitor as mon

marker = pathlib.Path(sys.argv[1])

async def fake_amain(args):
    # ⛔ READY 는 **handler 설치 이후**여야 한다. 설치 전에 알리면 그 창에 도착한 signal 이
    #    기본 동작(즉시 종료)으로 처리돼, 테스트가 "cleanup 이 안 돈다"고 **잘못** 말한다.
    #    이 coroutine 은 `add_signal_handler` 뒤 `await task` 에서 처음 스케줄된다.
    print("READY", flush=True)
    try:
        await asyncio.sleep(60)
        return 0
    finally:
        # ⛔ **await 가 있는 cleanup** 이어야 의미가 있다 — 취소된 task 의 finally 에서
        #    await 가 살아 있는지가 rollback(재기동·health 확인)의 전제다.
        await asyncio.sleep(0.05)
        marker.write_text("cleanup-ran")

mon._amain = fake_amain
raise SystemExit(asyncio.run(mon._amain_with_signals(None)))
"""


@pytest.mark.parametrize("signame,expected", [("SIGTERM", 143), ("SIGHUP", 129)])
def test_signal_lets_cleanup_finish_then_exits_nonzero(tmp_path, signame, expected):
    """⛔ **실제 프로세스에 실제 signal 을 보낸다.** 기본 동작은 *즉시 종료* 라 Python `finally`
    가 한 줄도 돌지 않는다 — 그러면 `.env` 의 `TOPIC_DISPATCHER_ENABLED=true` 가 남는다.
    SSH 세션이 끊기면 SIGHUP 이 오므로 7분 창에서 충분히 현실적인 경로다."""
    import signal as _signal
    import subprocess

    marker = tmp_path / "cleanup.txt"
    child = subprocess.Popen(
        [sys.executable, "-c", _SIGNAL_CHILD.format(repo=str(mon.REPO_ROOT)), str(marker)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert child.stdout.readline().strip() == "READY", "자식이 준비되지 않았다"
        child.send_signal(getattr(_signal, signame))
        code = child.wait(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    assert marker.exists(), f"{signame} 에서 cleanup 이 돌지 않았다 — rollback 이 사라진다"
    assert marker.read_text() == "cleanup-ran"
    assert code == expected, f"종료 코드가 128+signal 이 아니다: {code}"


def test_ws_reachability_success_path_actually_connects():
    """⛔ **성공 경로가 테스트된 적이 없었다.** 실패 경로(연결 거부)만 잠겨 있어서, 만약
    `async with await connect(...)` 형태가 라이브러리 버전에 따라 깨지면 **도달 가능한 URL 도
    불가로 보고**해 preflight 가 영영 통과하지 못한다 — canary 가 시작조차 못 하는 형태다.
    (실측으로 15.0.1·16.0 둘 다 정상임을 확인했지만, 그 확인이 테스트로 남아 있지 않았다.)"""
    import websockets

    async def scenario():
        async def handler(ws):
            await asyncio.sleep(5)

        # ⚠️ 고정 포트는 병렬 CI·로컬 프로세스와 충돌한다 — 0 으로 bind 하고 실제 포트를 받는다.
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await mon.check_ws_reachable(f"ws://127.0.0.1:{port}", timeout=5)
        finally:
            server.close()
            await server.wait_closed()

    assert _run(scenario()) == [], "도달 가능한 URL 을 불가로 보고했다"


_SIGKILL_CHILD = """
import asyncio, pathlib, sys
sys.path.insert(0, {repo!r})
from scripts import canary_monitor as mon

env = pathlib.Path(sys.argv[1])
mon.create_env_backup(env)
mon.atomic_write_text(env, mon.apply_env_values(env.read_text(), dict(mon.CANARY_ENV)))
print("APPLIED", flush=True)
asyncio.run(asyncio.sleep(120))
"""


def test_sigkill_leaves_a_backup_that_recover_can_restore(tmp_path):
    """⛔ **SIGKILL 은 signal handler 도 `finally` 도 건너뛴다.** 그 순간 남는 것은 backup
    파일뿐이고, 새 프로세스가 그것만으로 되돌릴 수 있어야 한다 — 자동 복원이 불가능한 유일한
    경로라 여기서 잠근다."""
    import signal as _signal
    import subprocess

    env = tmp_path / ".env"
    env.write_text("TOPIC_DISPATCHER_ENABLED=false\nOTHER=keep\n")
    original = env.read_text()

    child = subprocess.Popen(
        [sys.executable, "-c", _SIGKILL_CHILD.format(repo=str(mon.REPO_ROOT)), str(env)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert child.stdout.readline().strip() == "APPLIED"
        child.send_signal(_signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()

    # ⛔ 죽은 뒤의 상태: flag 는 켜져 있고 backup 만 남아 있다.
    assert "TOPIC_DISPATCHER_ENABLED=true" in env.read_text(), "전제: 활성화된 채 죽었다"
    assert mon.backup_path_for(env).exists(), "SIGKILL 후 복구 수단이 남지 않았다"

    async def noop(target=None):
        return None

    import unittest.mock as _mock
    with _mock.patch.object(mon, "recreate_and_verify_health", noop):
        _run(mon.EnvRestorer(env).restore())       # = 새 프로세스의 `--recover`

    assert env.read_text() == original, "복원이 바이트 동일하지 않다"
    assert not mon.backup_path_for(env).exists(), "복원 후 backup 이 남았다"


def test_restore_verifies_bytes_before_discarding_the_backup(tmp_path, monkeypatch):
    """⛔ 검증 없이 backup 을 지우면 **잘못된 복원을 성공으로 확정**한다 — 그 시점부터 원본은
    어디에도 없다. write 가 조용히 다른 내용을 남겼다고 가정하고 확인한다."""
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    mon.create_env_backup(env)
    mon.atomic_write_text(env, "A=2\n")

    real_write = mon.atomic_write_text

    def corrupting_write(path, text, *, mode=None):
        real_write(path, text.replace("A=1", "A=999"), mode=mode)

    async def noop(target=None):
        return None

    monkeypatch.setattr(mon, "recreate_and_verify_health", noop)
    monkeypatch.setattr(mon, "atomic_write_text", corrupting_write)

    with pytest.raises(mon.CollectorError) as caught:
        _run(mon.EnvRestorer(env).restore())
    assert "바이트 동일" in str(caught.value)
    assert mon.backup_path_for(env).exists(), "검증 실패인데 backup 을 버렸다 — 원본이 사라진다"
