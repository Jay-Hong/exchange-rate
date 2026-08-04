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
        "/admin/api/ws-auth-executor-metrics": {"metrics": {"count": 1}},
        "/admin/api/default-executor-probe": {"metrics": {"outstanding": 0}},
    }

    async def fake_run(command, *, timeout=None, cwd=None):
        if command[0] == "docker" and command[1] == "inspect":
            return "2026-08-04T00:00:00Z\n"
        if command[0] == "docker" and command[1] == "compose" and command[2] == "logs":
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


def test_collect_counts_real_error_lines_from_logs(monkeypatch):
    async def fake_run(command, *, timeout=None, cwd=None):
        if command[0] == "docker" and command[1] == "inspect":
            return "T\n"
        if command[0] == "docker" and command[2] == "logs":
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
        if command[0] == "docker" and command[2] == "logs":
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
    """⛔ **이전 주장이 틀렸다.** 컨테이너 셸에서 `-u admin:"$ADMIN_PASSWORD"` 를 쓰면 셸이
    확장한 **실제 값이 최종 `curl` 의 argv 에 들어간다**(`ps` 로 보인다). heredoc → `--config -`
    이면 셸이 **파이프에 쓰므로** 어떤 argv 에도 값이 없다."""
    script = mon.admin_fetch_script("/health")
    assert "--config -" in script, "stdin config 방식이 아니다"
    assert "-u admin:" not in script and "--user" not in script, "자격증명이 curl 인자에 있다"
    assert "$ADMIN_PASSWORD" in script, "변수 이름이 아니라 값을 박았다"
    assert "<<CFG" in script and "<<'CFG'" not in script, "따옴표 구분자는 변수 확장을 막는다"


def test_load_token_is_passed_by_stdin_not_argv():
    parser = mon.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--token", "secret"])
    assert parser.allow_abbrev is False


def test_monitor_reuses_the_shared_abort_rules():
    from scripts import ws_auth_load
    assert mon.evaluate_server_abort is ws_auth_load.evaluate_server_abort


# ── CLI 진입점이 실재하는가 ─────────────────────────────────────────────────


def test_cli_entry_point_exists_and_dry_run_describes_the_real_wiring():
    """⛔ 한때 이 파일은 **수집기에서 끝났다** — CLI · 부하 자식 · stop · rollback 이 전부
    없는데 문서에는 "배선 완료"라고 적혀 있었다."""
    for name in ("main", "start_load_process", "rollback_topic_dispatcher", "LoadProcess"):
        assert hasattr(mon, name), f"{name} 이 없다 — 실행기가 아니다"
    assert mon.main(["--token-stdin", "--dry-run"]) == 0
