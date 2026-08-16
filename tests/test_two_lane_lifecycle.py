"""S1b — WS/REST **두 lane 의 lifecycle 소유권**.

## 무엇을 잠그는가

1. **startup 4상태 매트릭스** — 시작 전 상태(둘 다 없음 / WS만 / REST만 / 둘 다)에 따라
   실패 시 **이번 시도가 만든 것만** 되돌린다. 남의 lane 을 내리면 안 된다.
2. **shutdown 격리** — 한 lane 의 종료 실패가 다른 lane 의 종료를 **건너뛰지 않는다**.
3. **런타임 격리** — 한 lane 의 포화·종료가 다른 lane 을 막지 않고, 계측이 섞이지 않는다.

## ⛔ 왜 조합 scope 가 아니라 lane 별 scope 인가

한 scope 가 둘을 관리하면 `created` 판정이 결합된다. WS 가 이미 살아 있는 상태에서 REST
startup 이 실패하면 조합 scope 는 "내가 만들지 않은" WS 까지 내릴 위험이 있다 — 그 회귀는
**정상 경로 테스트로는 전혀 드러나지 않는다**(실패 경로에서만 발화한다).

⚠️ 이 파일은 module-global lane 상태를 만진다. 각 테스트는 **양쪽 lane 정지**를 baseline 으로
   삼고 끝날 때 그 baseline 으로 되돌린다 — 안 그러면 실행 순서에 따라 결과가 흔들린다(실측).
"""
import asyncio
import threading
import unittest
from unittest.mock import patch

from app import auth_executor, config


async def _stop_both():
    for begin, join in (
        (auth_executor.begin_auth_executor_shutdown, auth_executor.await_auth_executor_shutdown),
        (auth_executor.begin_rest_auth_executor_shutdown, auth_executor.await_rest_auth_executor_shutdown),
    ):
        await join(begin())


class _Baseline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _stop_both()
        self.addAsyncCleanup(_stop_both)

    def _state(self) -> tuple[bool, bool]:
        return (auth_executor.is_auth_executor_running(),
                auth_executor.is_rest_auth_executor_running())


class TestStartupOwnershipMatrix(_Baseline):
    """시작 전 4상태 × startup 실패 → **이번 시도가 만든 것만** 되돌아간다."""

    async def _open_both_then_fail(self):
        """lifespan 과 같은 형태로 두 scope 를 함께 열고 안에서 실패시킨다."""
        with self.assertRaises(RuntimeError):
            async with auth_executor.ws_auth_lane_startup_scope(2), \
                       auth_executor.rest_auth_lane_startup_scope(2):
                raise RuntimeError("startup 실패")

    async def test_neither_running_rolls_back_both(self):
        self.assertEqual(self._state(), (False, False))
        await self._open_both_then_fail()
        self.assertEqual(self._state(), (False, False), "이번에 만든 두 lane 이 남았다")

    async def test_ws_already_running_is_preserved(self):
        """⛔ WS 는 **내가 만든 게 아니다** — REST startup 이 실패해도 내리면 안 된다."""
        auth_executor.start_auth_executor(2)
        self.assertEqual(self._state(), (True, False))
        await self._open_both_then_fail()
        self.assertEqual(self._state(), (True, False),
                         "남의 WS lane 을 내렸거나 내가 만든 REST 를 남겼다")

    async def test_rest_already_running_is_preserved(self):
        auth_executor.start_rest_auth_executor(2)
        self.assertEqual(self._state(), (False, True))
        await self._open_both_then_fail()
        self.assertEqual(self._state(), (False, True))

    async def test_both_already_running_are_preserved(self):
        auth_executor.start_auth_executor(2)
        auth_executor.start_rest_auth_executor(2)
        self.assertEqual(self._state(), (True, True))
        await self._open_both_then_fail()
        self.assertEqual(self._state(), (True, True), "둘 다 남의 것인데 내렸다")

    async def test_rest_start_failure_rolls_back_the_ws_lane_this_attempt_created(self):
        """⛔ **부분 성공**: WS 는 떴는데 REST start 가 던진다.

        `rest` scope 의 `start` 는 `try` 안이라 자기 것은 정리하고, 바깥 `ws` scope 는 자기가
        만든 WS 를 되돌린다. 어느 한쪽이라도 빠지면 pool 이 샌다.
        """
        real_start = auth_executor._rest_lane.start_auth_executor

        def boom(_max_workers):
            raise RuntimeError("REST start 실패")

        auth_executor._rest_lane.start_auth_executor = boom
        try:
            with self.assertRaises(RuntimeError):
                async with auth_executor.ws_auth_lane_startup_scope(2), \
                           auth_executor.rest_auth_lane_startup_scope(2):
                    self.fail("여기 도달하면 안 된다")
        finally:
            auth_executor._rest_lane.start_auth_executor = real_start
        self.assertEqual(self._state(), (False, False), "부분 성공에서 WS pool 이 샜다")

    async def test_normal_exit_leaves_both_lanes_live(self):
        """정상 경로에서는 아무것도 되돌리지 않는다 — scope 는 **startup 실패 전용**이다."""
        async with auth_executor.ws_auth_lane_startup_scope(2), \
                   auth_executor.rest_auth_lane_startup_scope(2):
            pass
        self.assertEqual(self._state(), (True, True))


class TestRuntimeIsolation(_Baseline):
    async def test_saturating_one_lane_does_not_block_the_other(self):
        """⛔ 격리의 **목적** 자체다. WS 를 꽉 채워도 REST 는 진행해야 한다.

        pool 을 1 worker 로 띄우고 그 하나를 잡아둔 뒤, 반대 lane 이 실제로 완료되는지 본다.
        """
        auth_executor.start_auth_executor(1)
        auth_executor.start_rest_auth_executor(1)
        release = threading.Event()
        self.addCleanup(release.set)

        def hog():
            release.wait(timeout=5)
            return "ws-done"

        ws_task = asyncio.create_task(auth_executor.run_in_auth_executor(hog))
        await asyncio.sleep(0.05)   # WS worker 를 확실히 점유시킨다
        rest = await asyncio.wait_for(
            auth_executor.run_in_rest_auth_executor(lambda: "rest-done"), timeout=3
        )
        self.assertEqual(rest, "rest-done", "WS 포화가 REST 를 막았다 = pool 이 공유됐다")
        release.set()
        self.assertEqual(await asyncio.wait_for(ws_task, timeout=3), "ws-done")

    async def test_shutting_down_one_lane_leaves_the_other_usable(self):
        auth_executor.start_auth_executor(1)
        auth_executor.start_rest_auth_executor(1)
        await auth_executor.await_auth_executor_shutdown(
            auth_executor.begin_auth_executor_shutdown()
        )
        self.assertEqual(self._state(), (False, True))
        self.assertEqual(await auth_executor.run_in_rest_auth_executor(lambda: "ok"), "ok")

    async def test_metrics_and_reset_do_not_cross(self):
        """⛔ 계측이 섞이면 REST 부하가 WS 신호로 보이고, 그 반대도 된다."""
        auth_executor.start_auth_executor(1)
        auth_executor.start_rest_auth_executor(1)
        auth_executor.reset_auth_executor_metrics()
        auth_executor.reset_rest_auth_executor_metrics()

        ws_before_rest = auth_executor.auth_executor_metrics()
        await auth_executor.run_in_rest_auth_executor(lambda: None)
        ws_after_rest = auth_executor.auth_executor_metrics()
        self.assertEqual(ws_after_rest, ws_before_rest,
                         "REST 작업이 WS 계측을 움직였다")

        rest_metrics = auth_executor.rest_auth_executor_metrics()
        self.assertNotEqual(rest_metrics, ws_after_rest, "두 lane 계측이 같은 객체를 본다")

        # ⛔ 한쪽 reset 이 다른 쪽을 지우면 안 된다.
        auth_executor.reset_auth_executor_metrics()
        self.assertEqual(auth_executor.rest_auth_executor_metrics(), rest_metrics,
                         "WS reset 이 REST 계측을 지웠다")

        ws_metrics = auth_executor.auth_executor_metrics()
        auth_executor.reset_rest_auth_executor_metrics()
        self.assertEqual(auth_executor.auth_executor_metrics(), ws_metrics,
                         "REST reset 이 WS 계측을 지웠다")

    async def test_worker_threads_carry_distinct_prefixes(self):
        """스레드 이름은 운영에서 lane 을 가르는 **유일한 표식**이다(스택·프로파일러)."""
        auth_executor.start_auth_executor(1)
        auth_executor.start_rest_auth_executor(1)
        ws = await auth_executor.run_in_auth_executor(lambda: threading.current_thread().name)
        rest = await auth_executor.run_in_rest_auth_executor(lambda: threading.current_thread().name)
        self.assertTrue(ws.startswith("ws-auth"), ws)
        self.assertTrue(rest.startswith("rest-auth"), rest)

    async def test_singletons_use_distinct_dynamic_timing_flags_and_events(self):
        """⛔ REST singleton 이 WS event/flag 를 복사하면 두 lane 로그가 구분되지 않는다."""
        auth_executor.start_auth_executor(1)
        auth_executor.start_rest_auth_executor(1)

        with patch.object(config, "WS_AUTH_EXECUTOR_LOG_TIMINGS", False), \
             patch.object(config, "REST_AUTH_EXECUTOR_LOG_TIMINGS", True), \
             self.assertLogs("exchange_rate.auth_executor", level="INFO") as first:
            await auth_executor.run_in_auth_executor(lambda: None)
            await auth_executor.run_in_rest_auth_executor(lambda: None)
        first_messages = [record.getMessage() for record in first.records]
        self.assertIn("rest_auth_timing", first_messages)
        self.assertNotIn("ws_auth_timing", first_messages)

        with patch.object(config, "WS_AUTH_EXECUTOR_LOG_TIMINGS", True), \
             patch.object(config, "REST_AUTH_EXECUTOR_LOG_TIMINGS", False), \
             self.assertLogs("exchange_rate.auth_executor", level="INFO") as second:
            await auth_executor.run_in_auth_executor(lambda: None)
            await auth_executor.run_in_rest_auth_executor(lambda: None)
        second_messages = [record.getMessage() for record in second.records]
        self.assertIn("ws_auth_timing", second_messages)
        self.assertNotIn("rest_auth_timing", second_messages)


class TestShutdownIsolation(_Baseline):
    async def test_one_lane_begin_failure_does_not_skip_the_other(self):
        """⛔ 순차로 부르면 첫 `begin` 이 던지는 순간 나머지 lane 은 **종료조차 시도되지 않는다**.

        `app/main.py` 의 shutdown 이 두 lane 을 각각 try 로 감싸는 이유다. 여기서는 그 계약을
        lane 표면에서 재현한다 — main 배선은 `tests/test_lifespan_auth_lane_wiring.py` 가 본다.
        """
        auth_executor.start_auth_executor(1)
        auth_executor.start_rest_auth_executor(1)
        calls: list[str] = []

        def ws_begin():
            calls.append("ws")
            raise RuntimeError("WS begin 실패")

        real_ws = auth_executor._ws_lane.begin_auth_executor_shutdown
        auth_executor._ws_lane.begin_auth_executor_shutdown = ws_begin
        try:
            closing = {}
            for name, begin in (("ws", auth_executor.begin_auth_executor_shutdown),
                                ("rest", auth_executor.begin_rest_auth_executor_shutdown)):
                try:
                    closing[name] = begin()
                except BaseException:
                    closing[name] = None
            for name, join in (("ws", auth_executor.await_auth_executor_shutdown),
                               ("rest", auth_executor.await_rest_auth_executor_shutdown)):
                await join(closing.get(name))
        finally:
            auth_executor._ws_lane.begin_auth_executor_shutdown = real_ws
        self.assertEqual(calls, ["ws"])
        self.assertFalse(auth_executor.is_rest_auth_executor_running(),
                         "WS begin 실패가 REST 종료를 건너뛰었다")


class TestShutdownLoggingDoesNotBreakIsolation(_Baseline):
    """⛔ **진단 로깅이 격리를 다시 무너뜨리면 안 된다**(codex).

    lane 예외를 잡아놓고 `logger.exception` 을 보호 없이 부르면, 로깅이 던지는 순간 loop 가
    깨져 **다음 lane 은 종료조차 시도되지 않는다**. S1a′ 이 rollback 경로에 잠근 것과 같은
    계약이고, shutdown 경로에서 한 번 더 어겼다.

    ⚠️ 여기서는 `app/main.py` 의 loop **형태**를 재현해 계약을 시험한다. 그 배선이 실제로
       그 형태인지는 `tests/test_lifespan_auth_lane_wiring.py` 의 AST 검사가 본다 — 두 축을
       합쳐야 계약이 닫힌다(재현만 하면 production 이 달라져도 초록이다).
    """

    async def _drain_like_main(self, *, begin_boom=False, log_boom=False) -> list[str]:
        """`app/main.py` shutdown loop 와 같은 형태 — 로깅 보호 포함."""
        attempted: list[str] = []
        closing: dict[str, object] = {}

        def logger_exception(*_a, **_k):
            if log_boom:
                raise RuntimeError("로깅도 실패")

        for name, begin in (("ws", auth_executor.begin_auth_executor_shutdown),
                            ("rest", auth_executor.begin_rest_auth_executor_shutdown)):
            attempted.append(name)
            try:
                if begin_boom and name == "ws":
                    raise RuntimeError("WS begin 실패")
                closing[name] = begin()
            except BaseException:
                closing[name] = None
                try:
                    logger_exception()
                except BaseException:
                    pass
        for name, join in (("ws", auth_executor.await_auth_executor_shutdown),
                           ("rest", auth_executor.await_rest_auth_executor_shutdown)):
            await join(closing.get(name))
        return attempted

    async def test_begin_failure_plus_logging_failure_still_attempts_the_other_lane(self):
        auth_executor.start_auth_executor(1)
        auth_executor.start_rest_auth_executor(1)
        attempted = await self._drain_like_main(begin_boom=True, log_boom=True)
        self.assertEqual(attempted, ["ws", "rest"],
                         "로깅 실패가 다음 lane 종료 시도를 막았다")
        self.assertFalse(auth_executor.is_rest_auth_executor_running())

    async def test_production_shutdown_protects_both_logging_sites(self):
        """⛔ 재현이 아니라 **production 소스**를 본다 — 재현만 하면 배선이 달라져도 초록이다.

        ⚠️ 첫 판정식은 **공허했다**(실측 SURVIVED): "이 호출을 감싼 try 중 BaseException 을
           잡는 게 있나" 로 물으면, lane 예외를 잡는 **바깥** try 가 항상 그 조건을 만족시켜
           보호를 지워도 초록이었다. 계약은 "로깅이 **어떤 try 의 body**(handler 가 아니라)
           안에 있고 그 try 가 BaseException 을 잡는다" 이다.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("app/main.py").read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "lifespan")

        def _protected(call: ast.AST) -> bool:
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try):
                    continue
                # ⛔ **body 만** 본다 — handler 안에 있는 것은 그 try 가 보호하지 않는다.
                in_body = any(call is c for stmt in node.body for c in ast.walk(stmt))
                if in_body and any(h.type is not None
                                   and ast.unparse(h.type) == "BaseException"
                                   for h in node.handlers):
                    return True
            return False

        sites = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "exception" and "auth lane" in ast.unparse(n)]
        self.assertEqual(len(sites), 2, f"auth lane 로깅 지점이 2곳이어야 한다: {len(sites)}")
        unprotected = [ast.unparse(s)[:60] for s in sites if not _protected(s)]
        self.assertEqual(unprotected, [], f"보호되지 않은 auth lane 로깅: {unprotected}")
