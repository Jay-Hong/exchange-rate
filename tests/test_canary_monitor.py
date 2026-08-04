"""W canary 자동 중단 watchdog — **중단 조건이 실행 경로에 연결돼 있는가**.

⛔ 이 모듈이 없던 동안 `evaluate_server_abort()` 는 순수 함수로만 존재하고 **호출자가 없었다**.
   합의한 "즉시 중단 6종" 중 자동화된 것은 클라 timeout/error 둘뿐이었고 나머지는 아무도
   보지 않았다 — "운영자가 스냅샷을 넣는다"는 8 동시 연결이 도는 7분 창에서 **실행 가능한
   절차가 아니다**.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import canary_monitor as mon  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Harness:
    """부하 자식 · rollback 을 대역으로 두고 **호출 여부와 순서**를 기록한다."""

    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self.collected = 0
        self.calls: list[str] = []
        self.load_cancelled = False

    async def collect(self):
        # ⚠️ **실제 수집기는 I/O 라 반드시 양보한다.** 대역이 즉시 반환하면 loop 가 한 번도 안
        #    돌아 부하 task 가 **시작조차 못 한 채** 취소되고, 그러면 "취소를 관측했는가" 류의
        #    단언이 실제 동작과 무관해진다(여기서 실제로 겪었다).
        await asyncio.sleep(0)
        self.collected += 1
        if self._snapshots:
            return self._snapshots.pop(0)
        return mon.Snapshot()

    async def load(self, seconds=0.5):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            self.load_cancelled = True
            raise

    async def stop_load(self):
        self.calls.append("stop_load")

    async def rollback(self):
        self.calls.append("rollback")


def _outcome(harness, **kw):
    return _run(mon.run_monitored_canary(
        collect=harness.collect, load=harness.load(), stop_load=harness.stop_load,
        rollback=harness.rollback, poll_interval=0.001, emit=lambda _: None, **kw))


# ── 판정이 실행 경로에 연결돼 있는가 ────────────────────────────────────────


def test_monitor_actually_polls_the_server():
    """⛔ 폴링이 없으면 나머지 계약은 전부 공허하다."""
    harness = _Harness([])
    outcome = _outcome(harness)
    assert harness.collected >= 1, "서버를 한 번도 보지 않았다"
    assert outcome.polls >= 1


@pytest.mark.parametrize("snapshot,expected", [
    (mon.Snapshot(health_ok=False), "health 실패"),
    (mon.Snapshot(new_error_count=1), "신규 ERROR"),
    (mon.Snapshot(broadcast_age_seconds=30), "broadcast"),
    (mon.Snapshot(auth_metrics={"queue_wait_ms_max": 5000}), "queue_wait_ms_max"),
    (mon.Snapshot(auth_metrics={"never_started": 1}), "never_started"),
    (mon.Snapshot(auth_metrics={"caller_cancelled_while_running": 1}), "caller_cancelled"),
    (mon.Snapshot(probe={"queue_delay_ms_max": 1000}), "queue delay"),
])
def test_each_agreed_condition_actually_aborts(snapshot, expected):
    """⛔ **문서에만 있던 6종을 여기서 실행 경로에 묶는다.**"""
    harness = _Harness([snapshot])
    outcome = _outcome(harness)
    assert outcome.aborted, f"{expected} 인데 중단되지 않았다"
    assert any(expected in reason for reason in outcome.reasons), outcome.reasons


def test_container_restart_aborts_only_against_a_baseline():
    harness = _Harness([mon.Snapshot(container_started_at="T2")])
    outcome = _outcome(harness, baseline_started_at="T1")
    assert outcome.aborted and "재시작" in outcome.reasons[0]

    same = _Harness([mon.Snapshot(container_started_at="T1")])
    assert not _outcome(same, baseline_started_at="T1").aborted


def test_probe_outstanding_needs_two_consecutive_observations():
    """⚠️ 1회는 정상 스케줄링 지터로도 나온다 — 그걸로 창을 닫으면 canary 가 시작도 못 한다.
    ⛔ 반대로 2회 연속을 못 보면 **완전 포화를 놓친다**."""
    once = _Harness([mon.Snapshot(probe={"outstanding": 1}), mon.Snapshot()])
    assert not _outcome(once).aborted, "1회 관측으로 중단했다"

    # ⛔ **직전 상태를 실제로 나르는지** 가른다. 위 케이스만 두면 `previous_outstanding` 을
    #    상수 1로 박아도 통과한다(둘째 tick 의 outstanding 이 0이라 어차피 안 걸린다) —
    #    "2회 연속" 이 상태가 아니라 우연이 된다.
    late = _Harness([mon.Snapshot(), mon.Snapshot(probe={"outstanding": 1}), mon.Snapshot()])
    assert not _outcome(late).aborted, "직전이 정상인데 뒤늦은 1회 관측으로 중단했다"

    twice = _Harness([mon.Snapshot(probe={"outstanding": 1}),
                      mon.Snapshot(probe={"outstanding": 1})])
    outcome = _outcome(twice)
    assert outcome.aborted, "2회 연속인데 포화를 놓쳤다"
    assert any("2회 연속" in reason for reason in outcome.reasons)


# ── 중단 → 자식 종료 → rollback ─────────────────────────────────────────────


def test_abort_stops_the_load_and_rolls_back():
    harness = _Harness([mon.Snapshot(health_ok=False)])
    outcome = _outcome(harness)
    assert outcome.aborted
    assert harness.calls == ["stop_load", "rollback"], \
        "부하를 끊기 전에 flag 를 되돌리면 닫힌 flag 를 계속 두드려 잡음이 된다"
    assert outcome.load_stopped and outcome.rollback_done


def test_rollback_runs_even_when_nothing_aborted():
    """⛔ canary 는 **유계 실험**이다 — 창이 닫히면 flag 도 닫혀야 한다(성공해도)."""
    harness = _Harness([])
    outcome = _outcome(harness)
    assert not outcome.aborted
    assert "rollback" in harness.calls, "정상 종료에서 flag 가 켜진 채 남았다"


def test_rollback_runs_when_collect_raises():
    """⛔ 예외로 빠져나가도 flag 는 되돌아와야 한다 — `finally` 밖에 두면 켜진 채 남는다."""
    harness = _Harness([])

    async def exploding_collect():
        raise RuntimeError("수집 실패")

    with pytest.raises(RuntimeError):
        _run(mon.run_monitored_canary(
            collect=exploding_collect, load=harness.load(), stop_load=harness.stop_load,
            rollback=harness.rollback, poll_interval=0.001, emit=lambda _: None))

    assert harness.calls == ["stop_load", "rollback"], "예외 경로에서 rollback 이 빠졌다"


def test_rollback_runs_when_the_monitor_is_cancelled():
    """⛔ SIGINT(=취소)에서도 되돌린다."""
    harness = _Harness([])

    async def scenario():
        task = asyncio.ensure_future(mon.run_monitored_canary(
            collect=harness.collect, load=harness.load(seconds=5), stop_load=harness.stop_load,
            rollback=harness.rollback, poll_interval=0.05, emit=lambda _: None))
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _run(scenario())
    assert "rollback" in harness.calls, "취소 경로에서 rollback 이 빠졌다"


def test_rollback_still_runs_if_stopping_the_load_fails():
    """⚠️ 자식 종료가 실패해도 flag 는 되돌려야 한다 — 둘을 한 `try` 에 두면 함께 날아간다."""
    harness = _Harness([])

    async def failing_stop():
        harness.calls.append("stop_load")
        raise RuntimeError("자식 종료 실패")

    with pytest.raises(RuntimeError):
        _run(mon.run_monitored_canary(
            collect=harness.collect, load=harness.load(), stop_load=failing_stop,
            rollback=harness.rollback, poll_interval=0.001, emit=lambda _: None))

    assert "rollback" in harness.calls, "자식 종료 실패가 rollback 까지 삼켰다"


def test_load_task_does_not_outlive_the_monitor():
    harness = _Harness([mon.Snapshot(health_ok=False)])
    _outcome(harness)
    assert harness.load_cancelled, "중단 후에도 부하 task 가 살아 있다"


# ── 비밀 비노출 ─────────────────────────────────────────────────────────────


def test_admin_password_value_never_enters_our_argv():
    """⛔ `-u admin:<실제값>` 을 만들면 `ps` 로 다른 사용자에게 보인다. 확장은 **컨테이너 안**에서."""
    command = mon.admin_fetch_command("/admin/api/ws-auth-executor-metrics")
    joined = " ".join(command)
    assert '$ADMIN_PASSWORD' in joined, "컨테이너 내부 확장 형태가 아니다"
    assert "docker" in command[0] and "-T" in command
    # 값이 치환된 흔적(따옴표 없는 리터럴)이 없어야 한다.
    assert 'admin:"$ADMIN_PASSWORD"' in joined


def test_monitor_module_reuses_the_shared_abort_rules():
    """⛔ 판정을 재구현하면 부하 도구와 watchdog 이 **다른 기준**으로 canary 를 판정하게 된다."""
    from scripts import ws_auth_load
    assert mon.evaluate_server_abort is ws_auth_load.evaluate_server_abort
