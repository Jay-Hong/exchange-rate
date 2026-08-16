# tests/test_auth_executor_ledger.py
"""S5 — auth_executor 제출/시작 장부 계약.

설계 §S5 의 변이 기준 8종을 잠근다. 핵심 불변식은 하나다:

    queued_now = submitted_total - submit_failed_total - started_total - never_started >= 0

이 값이 음수가 되는 배치(예: `submitted_total` 을 `submit()` **뒤**로)는 red 여야 한다.
⚠️ 기존 7필드(count/queue_wait_*/execution_*/never_started/caller_cancelled_while_running)와
   `by_outcome` 의 **값·의미는 바뀌지 않는다** — 그 회귀는 tests/test_auth_executor.py 가 진다.
"""

# 표준 라이브러리
import asyncio
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

# 로컬
from app import auth_executor

_COUNTER_KEYS = ("count", "never_started", "caller_cancelled_while_running",
                 "submitted_total", "submit_failed_total", "started_total",
                 "ledger_errors_total")
_FLOAT_KEYS = ("queue_wait_ms_sum", "queue_wait_ms_max", "execution_ms_sum", "execution_ms_max")
_GAUGE_KEYS = ("in_flight", "in_flight_max", "queued_observed_max")


class LedgerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        auth_executor.start_auth_executor(2)
        auth_executor.reset_auth_executor_metrics()

    async def asyncTearDown(self):
        auth_executor.shutdown_auth_executor()
        with auth_executor._ws_lane._metrics_lock:          # teardown 은 강제 정리(계약 밖)
            auth_executor._ws_lane._metrics.update({k: 0 for k in _COUNTER_KEYS})
            auth_executor._ws_lane._metrics.update({k: 0 for k in _GAUGE_KEYS})
            auth_executor._ws_lane._metrics["by_outcome"] = {}

    @staticmethod
    def _m():
        return auth_executor.auth_executor_metrics()


class TestLedgerInvariant(LedgerTestCase):
    async def test_queued_never_negative_under_concurrency(self):
        """[변이 ①] `submitted_total` 이 submit 뒤로 가면 여기서 음수가 잡힌다."""
        seen = []

        def work():
            seen.append(self._m()["queued_now"])       # worker 안에서도 관측
            return "ok"

        await asyncio.gather(*[auth_executor.run_in_auth_executor(work) for _ in range(12)])
        self.assertTrue(all(q >= 0 for q in seen), f"queued_now 음수 관측: {seen}")
        m = self._m()
        self.assertEqual(m["queued_now"], 0, "전부 끝났는데 큐가 비지 않았다")
        self.assertEqual(m["submitted_total"], 12)
        self.assertEqual(m["started_total"], 12)

    async def test_in_flight_returns_to_zero(self):
        """[변이 ③] 감소가 `_record` 와 다른 finally 로 가면 누수로 red."""
        await asyncio.gather(*[auth_executor.run_in_auth_executor(lambda: 1) for _ in range(8)])
        m = self._m()
        self.assertEqual(m["in_flight"], 0, "worker 종료 후 in_flight 누수")
        self.assertGreaterEqual(m["in_flight_max"], 1)
        self.assertLessEqual(m["in_flight_max"], 2, "max_workers=2 를 넘을 수 없다")

    async def test_worker_exception_still_balances_ledger(self):
        """예외로 끝난 worker 도 in_flight 를 반납한다(기존 by_outcome 의미 불변)."""
        with self.assertRaises(ValueError):
            await auth_executor.run_in_auth_executor(lambda: (_ for _ in ()).throw(ValueError("x")))
        m = self._m()
        self.assertEqual(m["in_flight"], 0)
        self.assertEqual(m["queued_now"], 0)
        self.assertEqual(m["by_outcome"].get("ValueError"), 1, "기존 by_outcome 의미가 바뀌었다")


class TestReservationOrdering(LedgerTestCase):
    async def test_reservation_precedes_submit_deterministically(self):
        """[변이 ①·결정적] `submit()` 이 worker 를 **반환 전에 완주**시켜도 `queued_now` 는
        음수가 될 수 없다. 스레드 스케줄링에 의존하는 동시성 테스트와 달리 이 경로는
        `submitted_total` 이 submit 뒤로 가면 **반드시** -1 을 만든다."""
        observed = []

        def sync_submit(fn, *a, **kw):
            fut: Future = Future()
            try:
                fut.set_result(fn(*a, **kw))     # worker 를 submit 반환 전에 끝낸다
            except BaseException as exc:         # pragma: no cover - 방어
                fut.set_exception(exc)
            observed.append(auth_executor.auth_executor_metrics()["queued_now"])
            return fut

        with patch.object(auth_executor._ws_lane._executor, "submit", side_effect=sync_submit):
            await auth_executor.run_in_auth_executor(lambda: 1)
        self.assertEqual(observed, [0], f"예약이 submit 뒤로 가면 여기서 음수가 된다: {observed}")
        self.assertEqual(self._m()["queued_now"], 0)


class TestSubmitFailure(LedgerTestCase):
    async def test_submit_failure_balances_and_leaves_peak_untouched(self):
        """[변이 ②·⑦] 실패는 상계되고, **peak 은 건드리지 않는다**."""
        before = self._m()["queued_observed_max"]

        class Boom(RuntimeError):
            pass

        with patch.object(auth_executor._ws_lane._executor, "submit", side_effect=Boom("submit 거부")):
            with self.assertRaises(Boom):
                await auth_executor.run_in_auth_executor(lambda: 1)
        m = self._m()
        self.assertEqual(m["submit_failed_total"], 1, "실패가 장부에 없다")
        self.assertEqual(m["submitted_total"], 1)
        self.assertEqual(m["queued_now"], 0, "실패한 제출이 큐에 남았다")
        self.assertEqual(m["queued_observed_max"], before, "실패한 제출이 peak 을 올렸다")
        self.assertEqual(m["started_total"], 0)
        self.assertEqual(m["in_flight"], 0)


    async def test_mixed_failure_and_success_keeps_invariant(self):
        """[codex High] 실패 제출이 동시에 섞여도 **불변식**은 유지된다.
        ⚠️ peak 정확성은 계약이 아니다 — 실패 예약이 다른 스레드의 표본에 섞일 수 있고
           그 사실은 `_note_submit_succeeded` 도크스트링에 명시돼 있다."""
        real_submit = auth_executor._ws_lane._executor.submit
        calls = {"n": 0}

        def flaky(fn, *a, **kw):
            calls["n"] += 1
            if calls["n"] % 3 == 0:
                raise RuntimeError("submit 거부")
            return real_submit(fn, *a, **kw)

        with patch.object(auth_executor._ws_lane._executor, "submit", side_effect=flaky):
            results = await asyncio.gather(
                *[auth_executor.run_in_auth_executor(lambda: 1) for _ in range(9)],
                return_exceptions=True)
        failures = [r for r in results if isinstance(r, RuntimeError)]
        self.assertEqual(len(failures), 3, "주입한 실패 수가 다르다")
        m = self._m()
        self.assertGreaterEqual(m["queued_now"], 0)
        self.assertEqual(m["queued_now"], 0, "전부 종결됐는데 장부가 안 맞는다")
        self.assertEqual(m["submit_failed_total"], 3)
        self.assertEqual(m["submitted_total"], 9)
        self.assertEqual(m["started_total"], 6)


class TestExistingFieldSemantics(LedgerTestCase):
    async def test_started_event_precedes_ledger_write(self):
        """[codex High2·결정적] 장부 기록이 `started_evt.set()` **앞**으로 가면, 기록이 지연되는
        동안 caller 취소가 `caller_cancelled_while_running` 에 안 잡힌다 — 기존 필드 의미 회귀.

        장부 기록만 지연시키고(lock 은 잡지 않는다) 그 창에서 취소해 순서를 관측한다."""
        note_entered, release_note, gate = threading.Event(), threading.Event(), threading.Event()
        original = auth_executor._ws_lane._note_worker_started

        def delayed_note(receipt):
            note_entered.set()
            release_note.wait(5)
            original(receipt)

        with patch.object(auth_executor._ws_lane, "_note_worker_started", side_effect=delayed_note) as _reach0:
            task = asyncio.create_task(auth_executor.run_in_auth_executor(lambda: gate.wait(5)))
            await asyncio.to_thread(note_entered.wait, 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release_note.set()
            gate.set()
            for _ in range(200):                      # worker 종료까지 대기
                if self._m()["in_flight"] == 0: break
                await asyncio.sleep(0.005)
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach0.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach0"
        self.assertEqual(self._m()["caller_cancelled_while_running"], 1,
                         "장부 기록이 started_evt 앞으로 가면 취소가 누락된다")


    async def test_in_flight_underflow_is_rejected(self):
        """[codex (c)] `worker_finished` 오용으로 gauge 가 음수가 되면 reset 가드(`> 0`)가
        영구히 무력해진다 — 그래서 감소 자체를 막는다. 가드를 넣어만 두고 시험하지 않으면
        변이가 생존한다(실측)."""
        self.assertEqual(self._m()["in_flight"], 0)
        with self.assertRaises(RuntimeError):
            auth_executor._ws_lane._record(None, None, "bogus", worker_finished=True)
        m = self._m()
        self.assertEqual(m["in_flight"], 0, "가드가 음수를 허용했다")
        self.assertEqual(m["count"], 0, "가드는 count 증가 **전에** 걸려야 한다")


    async def test_ledger_lock_wait_does_not_leak_into_existing_timings(self):
        """[codex High·결정적] 계측 함수를 80ms 지연시켜도 `queue_wait_ms`/`execution_ms` 는
        오르지 않아야 한다.

        ⚠️ 이전 두 번의 "수정"은 오염을 **옮기기만** 했다 — 처음엔 queue_wait, 다음엔
           execution_ms 로. 그래서 두 필드를 **동시에** 잠근다."""
        DELAY = 0.08
        orig_sub, orig_start = auth_executor._ws_lane._note_submitted, auth_executor._ws_lane._note_worker_started

        def slow_sub():
            time.sleep(DELAY); orig_sub()

        def slow_start(receipt):
            time.sleep(DELAY); orig_start(receipt)

        with patch.object(auth_executor._ws_lane, "_note_submitted", side_effect=slow_sub) as _reach1, \
             patch.object(auth_executor._ws_lane, "_note_worker_started", side_effect=slow_start) as _reach2:
            await auth_executor.run_in_auth_executor(lambda: 1)
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach1.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach1"
            assert _reach2.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach2"
        m = self._m()
        self.assertEqual(m["ledger_errors_total"], 0,
                         "지연 주입이 예외로 삼켜졌다 — 주입 자체가 안 일어나 테스트가 vacuous 하다")
        self.assertEqual(m["started_total"], 1, "worker 장부가 안 올랐다 = 주입 경로가 깨졌다")
        budget = DELAY * 1000 / 2      # 지연의 절반 — 섞이면 반드시 넘는다
        self.assertLess(m["queue_wait_ms_max"], budget,
                        f"제출 장부 lock 대기가 queue_wait 에 섞였다: {m['queue_wait_ms_max']:.2f}ms")
        self.assertLess(m["execution_ms_max"], budget,
                        f"시작 장부 lock 대기가 execution 에 섞였다: {m['execution_ms_max']:.2f}ms")


class TestClockBoundary(unittest.TestCase):
    """[codex Medium] `queue_wait_ms` 의 **경계**를 잠근다.

    base(`b610e60`) 는 `submitted` 를 Event·클로저 생성 **앞**에서 찍었다. 장부 lock 오염을
    피한다며 이 줄을 Event 생성 뒤로 옮기면 오염은 사라지지만 **필드 의미가 바뀐다**
    (codex: Event 생성에 50ms 주입 시 base 110.24ms vs 이동 후 0.16ms).
    → 올바른 배치는 `_note_submitted()` 를 시계보다 앞에 두고 시계는 base 경계에 남기는 것.
    ⚠️ 타이밍 단언은 부하에서 흔들리므로 **소스 순서**로 잠근다."""

    def test_ledger_precedes_clock_which_precedes_event_creation(self):
        import ast
        import inspect
        import textwrap
        # ⛔ **facade 가 아니라 구현을 읽는다** — 공개 이름은 이제 2줄 위임이라 세 지점을 못 찾는다
        #    (심볼은 맞는데 **가리키는 것**이 바뀐 축 — 이름 기반 잔존 검사가 원리적으로 못 잡는다).
        # ⛔ 메서드 소스는 들여쓰기된 채 오므로 dedent 없이는 IndentationError.
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(auth_executor._ws_lane.run_in_auth_executor)))
        # ⚠️ **줄 번호**로 본다 — 보상 `try` 가 생기면서 Event 생성이 중첩됐고, top-level 문만
        #    훑던 판정은 그 순간 무너졌다(실측). 경계는 중첩과 무관한 성질이다.
        order = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Expr, ast.Assign)):
                continue
            src = ast.unparse(node)
            if src.startswith("self._note_submitted()"):
                order.setdefault("ledger", node.lineno)
            elif src.startswith("submitted = time.monotonic()"):
                order.setdefault("clock", node.lineno)
            elif src.startswith("started_evt = threading.Event()"):
                order.setdefault("event", node.lineno)
        self.assertEqual(sorted(order), ["clock", "event", "ledger"], f"세 지점을 못 찾았다: {order}")
        self.assertLess(order["ledger"], order["clock"],
                        "장부가 시계 뒤면 그 lock 대기가 queue_wait 에 섞인다")
        self.assertLess(order["clock"], order["event"],
                        "시계가 Event 생성 뒤면 queue_wait 의 경계가 base 와 달라진다")


class TestObservedPeak(LedgerTestCase):
    async def test_peak_is_positive_when_queueing_is_guaranteed(self):
        """[codex (f)] 성공 workload 에서 peak 이 실제로 올라가는지 — 지금까지 한 번도
        `queued_observed_max > 0` 을 단언하지 않았다(계측이 죽어 있어도 통과했다)."""
        gate = threading.Event()
        try:
            tasks = [asyncio.create_task(auth_executor.run_in_auth_executor(
                lambda: gate.wait(5))) for _ in range(6)]      # max_workers=2 → 4개는 대기
            for _ in range(200):
                if self._m()["queued_observed_max"] > 0: break
                await asyncio.sleep(0.005)
            peak = self._m()["queued_observed_max"]
        finally:
            gate.set()
            await asyncio.gather(*tasks)
        self.assertGreater(peak, 0, "큐가 보장된 workload 인데 peak 이 0 이다")
        self.assertEqual(self._m()["queued_now"], 0, "종료 후 큐가 비지 않았다")


class TestFailureAtomicity(LedgerTestCase):
    """[codex Major] 예약 이후의 실패가 장부를 영구히 어긋나게 두면 안 된다."""

    async def test_reservation_is_compensated_when_event_creation_fails(self):
        """`threading.Event()` 실패는 `submit()` 실패가 아니지만 **예약 이후**다.

        한때 보상 `try` 가 `submit()` 만 감싸서 `submitted=1 / submit_failed=0` 이 남았고
        `queued_now=1` 이 **영구 잔존**해 reset 까지 영구 차단됐다(codex 재현)."""
        with patch.object(auth_executor.threading, "Event", side_effect=RuntimeError("Event 실패")):
            with self.assertRaises(RuntimeError):
                await auth_executor.run_in_auth_executor(lambda: 1)
        m = self._m()
        self.assertEqual(m["submitted_total"], 1)
        self.assertEqual(m["submit_failed_total"], 1, "예약 이후 실패가 상계되지 않았다")
        self.assertEqual(m["queued_now"], 0, "장부가 영구히 어긋난다")
        auth_executor.reset_auth_executor_metrics()      # 영구 차단이 아니어야 한다

    async def test_post_submit_ledger_failure_does_not_replace_business_result(self):
        """제출 성공 뒤의 부기 실패가 caller 에게 반환되면 **계측이 업무 결과를 대체**한다.
        ⛔ 계측이 서비스 경로를 결정하면 그건 관측이 아니라 정책이다."""
        with patch.object(auth_executor._ws_lane, "_note_submit_succeeded",
                          side_effect=RuntimeError("부기 실패")) as _reach3:
            result = await auth_executor.run_in_auth_executor(lambda: "업무결과")
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach3.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach3"
        self.assertEqual(result, "업무결과", "계측 예외가 업무 결과를 대체했다")
        self.assertEqual(self._m()["ledger_errors_total"], 1, "삼켰는데 드러내지도 않았다")


    async def test_clock_failure_is_compensated(self):
        """[codex Major] `time.monotonic()` 도 **예약 이후**다 — 보상 범위 밖에 두면
        clock 실패가 상계되지 않아 queued_now 가 영구 잔존한다."""
        with patch.object(auth_executor.time, "monotonic", side_effect=RuntimeError("clock 실패")):
            with self.assertRaises(RuntimeError):
                await auth_executor.run_in_auth_executor(lambda: 1)
        m = self._m()
        self.assertEqual(m["submit_failed_total"], 1, "clock 실패가 상계되지 않았다")
        self.assertEqual(m["queued_now"], 0)
        auth_executor.reset_auth_executor_metrics()      # 영구 차단이 아니어야 한다

    async def test_diagnostic_logging_failure_does_not_replace_business_result(self):
        """[codex Major] `_safe_ledger` 의 **진단 로깅**이 보호되지 않으면, 장부 실패를 삼켜도
        로깅 실패가 그대로 올라가 원래 결함(계측이 업무 결과를 대체)이 재개방된다."""
        with patch.object(auth_executor._ws_lane, "_note_submit_succeeded", side_effect=RuntimeError("부기")) as _reach4, \
             patch.object(auth_executor.logger, "debug", side_effect=RuntimeError("logging failed")):
            result = await auth_executor.run_in_auth_executor(lambda: "업무결과")
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach4.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach4"
        self.assertEqual(result, "업무결과", "로깅 실패가 업무 결과를 대체했다")

    async def test_commit_then_raise_still_balances_in_flight(self):
        """[codex Major] 장부가 **커밋된 뒤** 예외가 나도 종료 감소가 생략되면 안 된다.

        ⛔ 반환값은 "끝까지 정상 반환"해야 전달되므로 커밋 여부의 증거가 못 된다 —
           그래서 lock **안에서** 채우는 receipt 로 판정한다."""
        real = auth_executor._ws_lane._note_worker_started

        def commit_then_raise(receipt):
            real(receipt)
            raise RuntimeError("커밋 후 예외")

        with patch.object(auth_executor._ws_lane, "_note_worker_started", side_effect=commit_then_raise) as _reach5:
            await auth_executor.run_in_auth_executor(lambda: 1)
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach5.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach5"
        m = self._m()
        self.assertEqual(m["in_flight"], 0, "커밋됐는데 종료 감소가 생략돼 gauge 가 샜다")
        auth_executor.reset_auth_executor_metrics()      # 영구 차단이 아니어야 한다


    async def test_multi_field_commit_regressing_to_in_place_would_leak(self):
        """[codex Major] `_note_worker_started` 의 **다중 필드 커밋**이 제자리 대입으로
        회귀하면 부분 커밋이 gauge 를 누수시킨다.

        ⛔ 대상은 **모든 항목 대입이 아니다.** `_note_submitted` 은 단일 카운터라 제자리 대입이
           정당하고, baseline 에서도 sentinel 은 실제로 **호출된다**(실측: hits ==
           ["submitted_total"]). 자는 것은 `in_flight_max` **트리거 조건**뿐이다.
        ⚠️ 원자성 자체의 증명이 아니라 in-place 회귀를 잡는 **런타임 트립와이어**다. 원자성은
           `TestHelperAtomicity` 의 구조 단언이 진다. 그래서 귀속에는 **이 테스트만 단독 실행**
           해야 한다 — in-place 변이는 구조 단언도 함께 죽인다."""

        # ⛔ **hit counter 를 둔다.** 이 트립와이어의 `in_flight_max` **실패 조건**은 현행
        #    구현에서 잠드는 것이 정상이다(sentinel 자체는 `submitted_total` 로 호출된다).
        #    "테스트가 red 다" 만으로는 그 조건이 깨어났다는 증거가 안 된다 — in-place 회귀는
        #    구조 AST 테스트도 함께 죽이므로 그쪽이 대신 죽인 걸 여기 판별력으로 오귀속하게 된다.
        hits: list[str] = []

        class MaxWriteFails(dict):
            def __setitem__(self, key, value):
                hits.append(key)
                if key == "in_flight_max":
                    raise RuntimeError("max 쓰기 실패 주입")
                super().__setitem__(key, value)

        with patch.object(auth_executor._ws_lane, "_metrics", MaxWriteFails(auth_executor._ws_lane._metrics)) as _reach6:
            await auth_executor.run_in_auth_executor(lambda: 1)
            in_flight = auth_executor._ws_lane._metrics["in_flight"]
        # ⛔ **트리거 단언을 먼저.** 행동 회귀가 먼저 실패하면 단독 red 가 나도 sentinel 이
        #    깨어났다는 증거가 출력되지 않아 귀속이 흐려진다.
        self.assertNotIn("in_flight_max", hits,
                         "다중 필드 커밋이 항목 대입으로 회귀했다 — copy-and-swap 이 아니다")
        self.assertEqual(in_flight, 0, "부분 커밋이 gauge 를 누수시켰다 (reset 영구 차단)")


class TestResetRefusesWhileQueued(LedgerTestCase):
    """[codex Major] 장부가 오염돼도 **일반 reset 은 queue 를 무시하면 안 된다**.

    한때 `ledger_errors_total > 0` 이면 queue 를 무시하도록 완화했는데, start 장부 **직전**에
    멈춰 있는 worker(in_flight=0 · queued=1)까지 무시해 reset 후 그 worker 가 `started_total` 만
    올려 `queued_now = -1` 이 됐다(재현). 영구 차단은 불편할 뿐이지만 음수 장부는 핵심
    불변식을 깨뜨린다.

    ⚠️ 이어서 둔 `force_after_shutdown`(조건 `_executor is None`)도 **제거**됐다 — `begin_...` 이
       drain 전에 `_executor` 를 비우므로 그 이름은 drain 의 증거가 아니었다. 복구 경로는 없고
       프로세스 재시작이 답이다.
    ⚠️ 다만 계약은 "오류가 있으면 못 연다"가 아니라 **"불균형이면 못 연다"** 이다 —
       아래 `test_reset_allowed_when_balanced_despite_errors` 가 그 경계를 잠근다."""

    async def test_pre_ledger_window_still_blocks_reset(self):
        """⛔ 결정적 재현: worker 를 start 장부 **직전**에 세운다(in_flight=0 · queued=1)."""
        gate, reached = threading.Event(), threading.Event()
        original = auth_executor._ws_lane._note_worker_started

        def hold(receipt):
            reached.set()
            gate.wait(5)
            original(receipt)

        with patch.object(auth_executor._ws_lane, "_note_submit_succeeded",
                          side_effect=RuntimeError("장부 오염 주입")) as _reach7:
            await auth_executor.run_in_auth_executor(lambda: 1)
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach7.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach7"
        self.assertGreater(self._m()["ledger_errors_total"], 0, "오염이 안 만들어졌다")

        with patch.object(auth_executor._ws_lane, "_note_worker_started", side_effect=hold) as _reach8:
            task = asyncio.create_task(auth_executor.run_in_auth_executor(lambda: 1))
            try:
                await asyncio.to_thread(reached.wait, 5)
                m = self._m()
                self.assertEqual(m["in_flight"], 0, "이 창은 in_flight=0 이어야 의미가 있다")
                self.assertEqual(m["queued_now"], 1)
                with self.assertRaises(auth_executor.AuthExecutorMetricsBusy):
                    auth_executor.reset_auth_executor_metrics()
            finally:
                gate.set()
                await task
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach8.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach8"
        self.assertEqual(self._m()["queued_now"], 0, "재개 후 장부가 음수/불일치가 됐다")

    async def test_reset_refuses_between_begin_shutdown_and_drain(self):
        """[codex Major] `begin_auth_executor_shutdown()` 은 **drain 전에** `_executor=None` 으로
        만든다 — 운영 lifespan 도 begin 과 await 를 나눠 쓴다. 그 창에서 reset 이 열리면 멈춰
        있던 worker 가 재개해 `queued_now = -1` 이 된다.

        ⚠️ 동기 `shutdown_auth_executor()`(두 단계를 한 번에) 만 쓰는 테스트는 이 공백을 밟지
           못한다 — 그래서 **begin 을 직접** 부른다."""
        gate, reached = threading.Event(), threading.Event()
        original = auth_executor._ws_lane._note_worker_started

        def hold(receipt):
            reached.set()
            gate.wait(5)
            original(receipt)

        with patch.object(auth_executor._ws_lane, "_note_worker_started", side_effect=hold) as _reach9:
            task = asyncio.create_task(auth_executor.run_in_auth_executor(lambda: 1))
            closing = None
            try:
                await asyncio.to_thread(reached.wait, 5)
                self.assertEqual(self._m()["queued_now"], 1)
                closing = auth_executor.begin_auth_executor_shutdown()
                self.assertIsNone(auth_executor._ws_lane._executor, "begin 이 _executor 를 비우지 않았다")
                with self.assertRaises(auth_executor.AuthExecutorMetricsBusy):
                    auth_executor.reset_auth_executor_metrics()
            finally:
                gate.set()
                await task
                if closing is not None:
                    closing.shutdown(wait=True)
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach9.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach9"
        self.assertEqual(self._m()["queued_now"], 0, "재개 후 장부가 음수/불일치가 됐다")
        auth_executor.start_auth_executor(2)          # teardown 이 다시 내린다

    async def test_reset_allowed_when_balanced_despite_errors(self):
        """[codex Minor] 계약 경계 — 장부 **오류**가 아니라 **불균형**이 reset 을 막는다.

        제출 성공 뒤 부기 실패는 `ledger_errors_total` 을 올리지만 균형은 깨지 않는다
        (submitted·started 가 짝을 맞춘다) → reset 은 허용되고 오류 카운터도 새 창을 위해 0."""
        with patch.object(auth_executor._ws_lane, "_note_submit_succeeded",
                          side_effect=RuntimeError("부기 실패")) as _reach10:
            await auth_executor.run_in_auth_executor(lambda: 1)
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach10.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach10"
        m = self._m()
        self.assertEqual(m["ledger_errors_total"], 1)
        self.assertEqual((m["in_flight"], m["queued_now"]), (0, 0), "이 오류는 균형을 깨지 않는다")
        auth_executor.reset_auth_executor_metrics()          # ⛔ 여기서 raise 면 계약 오독
        self.assertEqual(self._m()["ledger_errors_total"], 0, "새 창인데 오류가 남았다")

    async def test_live_worker_still_blocks_reset_even_with_ledger_error(self):
        """살아 있는 worker 는 어떤 경우에도 거부한다."""
        gate, entered = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            gate.wait(5)
            return 1

        with patch.object(auth_executor._ws_lane, "_note_submit_succeeded",
                          side_effect=RuntimeError("장부 이상 주입")) as _reach11:
            task = asyncio.create_task(auth_executor.run_in_auth_executor(blocked))
            await asyncio.to_thread(entered.wait, 5)
            # ⛔ **주입 도달 양성대조** — patch 가 lane 메서드에 닿지 않으면 이 테스트는
            #    아무것도 주입하지 않은 채 초록이 된다(모듈 심볼만 갈아끼운 형태).
            assert _reach11.call_count > 0, "주입이 lane 메서드에 닿지 않았다: _reach11"
        try:
            self.assertGreater(self._m()["ledger_errors_total"], 0)
            with self.assertRaises(auth_executor.AuthExecutorMetricsBusy):
                auth_executor.reset_auth_executor_metrics()
        finally:
            gate.set()
            await task


class TestHelperAtomicity(unittest.TestCase):
    """[codex Major] `_note_worker_started` 가 **단일 커밋**인지 구조로 잠근다.

    ⚠️ 타이밍·주입 테스트만으로는 '어떤 순차 갱신은 우연히 안 걸릴' 수 있다 — 구조를 본다."""

    def test_worker_start_note_commits_exactly_once(self):
        import ast
        import inspect
        import textwrap
        # ⛔ dedent 없이는 IndentationError. 접두사도 `self._metrics[` 로 바꾸지 않으면
        #    **존재할 수 없는 패턴**을 찾게 되어 단언이 영원히 공허해진다.
        fn = ast.parse(textwrap.dedent(
            inspect.getsource(auth_executor._ws_lane._note_worker_started)))
        subscript_writes = [ast.unparse(t) for node in ast.walk(fn)
                            if isinstance(node, (ast.Assign, ast.AugAssign))
                            for t in ([node.target] if isinstance(node, ast.AugAssign) else node.targets)
                            if isinstance(t, ast.Subscript) and ast.unparse(t).startswith("self._metrics[")]
        self.assertEqual(subscript_writes, [],
                         f"_metrics 직접 항목 대입은 부분 커밋을 만든다: {subscript_writes}")
        # ⛔ `_metrics.update(...)` 는 기존 dict 를 **제자리** 갱신한다 — copy-and-swap 이 아니고
        #    부분 실패가 가능하다(codex 재현: update 실패 → queued=1 고착).
        updates = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Call)
                   and ast.unparse(n).startswith("self._metrics.update(")]
        self.assertEqual(updates, [], f"제자리 갱신은 부분 커밋을 만든다: {updates}")
        rebinds = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and any(isinstance(x, ast.Attribute) and x.attr == "_metrics"
                           and isinstance(x.value, ast.Name) and x.value.id == "self"
                           for x in n.targets)]
        self.assertEqual(len(rebinds), 1, "커밋은 **단일 리바인딩**이어야 한다")


class TestResetContract(LedgerTestCase):
    async def test_reset_zeroes_every_counter_key(self):
        """[변이 ⑥] 신규 필드를 reset 에 안 넣으면 red — 키 집합으로 잠근다."""
        await asyncio.gather(*[auth_executor.run_in_auth_executor(lambda: 1) for _ in range(3)])
        auth_executor.reset_auth_executor_metrics()
        m = self._m()
        nonzero = [k for k in (*_COUNTER_KEYS, *_FLOAT_KEYS) if m[k] != 0]
        self.assertEqual(nonzero, [], f"reset 이 덮지 못한 카운터: {nonzero}")
        self.assertEqual(m["by_outcome"], {})

    async def test_reset_body_keeps_max_at_or_above_live_gauge(self):
        """[변이 ④] `max < current` 는 불변식 위반.

        ⚠️ 공개 `reset` 은 ⑧ 가드 때문에 **항상 유휴에서만** 돌아 gauge 가 0 이다 — 그 경로로
        단언하면 vacuous 하다(변이 ④가 실제로 생존했다). 그래서 가드 없는 본체
        `_reset_locked` 를 **gauge 가 살아 있는 상태**로 직접 시험한다.
        ⛔ 이것은 "장래에 가드가 완화돼도 안전하다"는 뜻이 **아니다** — `_reset_locked` 는 활성
        큐에 안전하지 않다(대기분이 시작되면 `started_total` 만 올라 `queued_now` 가 음수가 된다).
        가드는 정확성의 **필수조건**이다."""
        with auth_executor._ws_lane._metrics_lock:
            auth_executor._ws_lane._metrics["in_flight"] = 3
            auth_executor._ws_lane._metrics["submitted_total"] = 5
            auth_executor._ws_lane._metrics["started_total"] = 3
            auth_executor._ws_lane._reset_locked()
            in_flight = auth_executor._ws_lane._metrics["in_flight"]
            in_flight_max = auth_executor._ws_lane._metrics["in_flight_max"]
            queued_max = auth_executor._ws_lane._metrics["queued_observed_max"]
            queued_now = auth_executor._ws_lane._queued_locked()
        self.assertEqual(in_flight, 3, "살아 있는 worker 의 gauge 를 0 으로 밀면 -1 이 음수로 샌다")
        self.assertGreaterEqual(in_flight_max, in_flight)
        # ⚠️ `queued_max >= queued_now` 는 여기서 **vacuous** 하다 — `_reset_locked` 가 카운터를
        #    먼저 0 으로 만들어 둘 다 0 이다(codex 지적). 이 테스트가 실제로 잠그는 것은
        #    `in_flight` 처리뿐이므로, 없는 보증을 있는 척하지 않는다.
        self.assertEqual((queued_max, queued_now), (0, 0), "reset 후 큐 장부는 0 이어야 한다")

    async def test_reset_is_rejected_while_work_is_active(self):
        """[변이 ⑧] 활성 중 reset 은 거부돼야 한다 — 허용하면 감소가 음수로 샌다."""
        gate, entered = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            gate.wait(5)
            return 1

        task = asyncio.create_task(auth_executor.run_in_auth_executor(blocked))
        await asyncio.to_thread(entered.wait, 5)
        try:
            with self.assertRaises(auth_executor.AuthExecutorMetricsBusy):
                auth_executor.reset_auth_executor_metrics()
            self.assertEqual(self._m()["in_flight"], 1)
        finally:
            gate.set()
            await task
        auth_executor.reset_auth_executor_metrics()          # 유휴가 되면 허용
        self.assertEqual(self._m()["in_flight"], 0)


if __name__ == "__main__":
    unittest.main()
