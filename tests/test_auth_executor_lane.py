"""S1a — lane 캡슐화 계약.

두 축을 **따로** 잠근다. 하나로 합치면 어느 쪽이 변이를 죽였는지 귀속되지 않는다.
  ① 구조(trip-wire): lane 안에서 lane-owned 이름은 반드시 `self.<name>`.
  ② 행동(두-lane): 상태 격리 **그리고** lane 고유값(prefix/event/flag)이 실제로 갈린다.

⛔ ②가 상태 격리만 보면 `thread_prefix` 를 WS 값으로 하드코딩해도 통과한다(상태는 어차피
   인스턴스별이니까). 그래서 A/B 에 **서로 다른** prefix·event·flag 를 주입하고 그 셋이
   행동으로 드러나는지 단언한다.
"""
import ast
import asyncio
import inspect
import pathlib
import threading

import pytest

from app import auth_executor


def _run(coro):
    # ⛔ `new_event_loop()` 를 만들고 닫지 않으면 loop 가 샌다 — 기존 관례(`test_auth_executor.py`)
    #    와 같이 `asyncio.run` 을 쓴다.
    return asyncio.run(coro)


def _lane_class_node() -> ast.ClassDef:
    src = pathlib.Path(inspect.getfile(auth_executor)).read_text()
    return next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.ClassDef) and n.name == "AuthExecutorLane")


def _lane_owned_names(cls: ast.ClassDef) -> set[str]:
    """⛔ **자동 도출**한다 — 수기 목록이면 새 메서드·상태가 늘 때 조용히 낡는다."""
    owned = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            owned.add(node.attr)
    return owned


def test_lane_never_references_its_own_names_unqualified():
    """⛔ 호출만 보면 안 된다 — `_safe_ledger(_note_submit_succeeded)` 처럼 **참조로** 넘기는
    형태가 정확히 이렇게 샜다(실측). 그래서 `ast.Name(Load)` 를 전수로 본다.
    ⚠️ 무수식이면 모듈 facade 로 해결돼 **다른 lane 을 건드린다** — NameError 조차 아니라
       컴파일·기존 테스트 어디에도 안 걸린다."""
    cls = _lane_class_node()
    owned = _lane_owned_names(cls)
    bad = sorted({n.id for n in ast.walk(cls)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in owned})
    assert bad == [], f"lane 안에서 무수식으로 참조된 lane-owned 이름: {bad}"


def _lane(prefix: str, event: str, flag: list[bool]) -> auth_executor.AuthExecutorLane:
    return auth_executor.AuthExecutorLane(
        thread_prefix=prefix, timing_event=event, log_timings=lambda: flag[0])


class TestTwoLanes:
    """A/B 를 **서로 다른 설정**으로 띄운다."""

    @staticmethod
    def _pair():
        return _lane("lane-a", "a_timing", [False]), _lane("lane-b", "b_timing", [False])

    def test_shutting_down_one_lane_leaves_the_other_runnable(self):
        a, b = self._pair()
        a.start_auth_executor(1); b.start_auth_executor(1)
        try:
            assert _run(b.run_in_auth_executor(lambda: "b-ok")) == "b-ok"
            b.shutdown_auth_executor()
            assert not b.is_auth_executor_running()
            assert a.is_auth_executor_running(), "B 종료가 A 를 함께 내렸다"
            assert _run(a.run_in_auth_executor(lambda: "a-ok")) == "a-ok"
        finally:
            a.shutdown_auth_executor(); b.shutdown_auth_executor()

    def test_one_lane_work_and_reset_do_not_touch_the_other_ledger(self):
        a, b = self._pair()
        a.start_auth_executor(1); b.start_auth_executor(1)
        try:
            _run(a.run_in_auth_executor(lambda: None))
            _run(b.run_in_auth_executor(lambda: None))
            b.reset_auth_executor_metrics()
            assert a.auth_executor_metrics()["count"] == 1, "B reset 이 A 장부를 밀었다"
            assert b.auth_executor_metrics()["count"] == 0
        finally:
            a.shutdown_auth_executor(); b.shutdown_auth_executor()

    def test_each_lane_runs_on_its_own_thread_prefix(self):
        """⛔ 상태 격리만 보면 prefix 하드코딩이 통과한다 — 실행 스레드 이름으로 잠근다."""
        a, b = self._pair()
        a.start_auth_executor(1); b.start_auth_executor(1)
        try:
            na = _run(a.run_in_auth_executor(lambda: threading.current_thread().name))
            nb = _run(b.run_in_auth_executor(lambda: threading.current_thread().name))
            assert na.startswith("lane-a"), f"A 가 제 prefix 로 안 돌았다: {na}"
            assert nb.startswith("lane-b"), f"B 가 제 prefix 로 안 돌았다: {nb}"
        finally:
            a.shutdown_auth_executor(); b.shutdown_auth_executor()

    def test_log_flag_is_read_at_call_time_per_lane(self, caplog):
        """⛔ **생성 후에** flag 를 바꾼다. 생성 전에 정하면 생성 시점 bool 캡처 변이가 같은
        답을 내 통과한다(실측 — 초판이 정확히 그랬다). 두 단계로 나눠 event 도 각각 잠근다.
          1단계 A만 on  → `a_timing` 만
          2단계 A off / B on → 새로 남는 것은 `b_timing` 뿐
        """
        flag_a, flag_b = [False], [False]          # ⚠️ 생성 시점엔 **둘 다 off**
        a = _lane("lane-a", "a_timing", flag_a)
        b = _lane("lane-b", "b_timing", flag_b)
        a.start_auth_executor(1); b.start_auth_executor(1)
        try:
            flag_a[0] = True                        # 생성 **뒤** 변경 → 동적 조회여야 반영된다
            with caplog.at_level("INFO"):
                _run(a.run_in_auth_executor(lambda: None))
                _run(b.run_in_auth_executor(lambda: None))
            first = [r.getMessage() for r in caplog.records]
            assert "a_timing" in first, "생성 후 켠 flag 가 반영되지 않았다 — 생성 시점 캡처다"
            assert "b_timing" not in first, "B 는 off 인데 로그가 남았다"

            caplog.clear()
            flag_a[0], flag_b[0] = False, True      # 반전 — 양방향으로 잠근다
            with caplog.at_level("INFO"):
                _run(a.run_in_auth_executor(lambda: None))
                _run(b.run_in_auth_executor(lambda: None))
            second = [r.getMessage() for r in caplog.records]
            assert "b_timing" in second, "B 의 timing event 가 쓰이지 않았다"
            assert "a_timing" not in second, "A 를 껐는데 계속 남는다 — 동적 조회가 아니다"
        finally:
            a.shutdown_auth_executor(); b.shutdown_auth_executor()


if __name__ == "__main__":
    pytest.main([__file__])
