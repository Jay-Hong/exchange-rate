"""subscribe-load 계측(S1) — 계약 회귀.

⛔ 이 모듈은 **호출자 0** 인 상태로 land 한다(S1). 여기 잠그는 것은 필드 계약과 전이 규칙이며,
   배선은 S2~S6 이 한다.
"""
import asyncio
import logging
import threading
import unittest

from app import subscribe_load_metrics as slm


def _run(coro):
    return asyncio.run(coro)


class SubscribeLoadTestCase(unittest.TestCase):
    def setUp(self):
        self._hard_reset()

    def tearDown(self):
        """⛔ hard reset 만 하면 **경로 한정 변이가 게이지를 흘려도 그 테스트가 통과한 뒤
        증거를 지운다**. 먼저 불변식과 **공개** reset(진행 중이면 거부)으로 판정하고,
        격리는 `finally` 가 책임진다."""
        try:
            self._invariant(slm.subscribe_load_metrics())
            slm.reset_subscribe_load_metrics()
        finally:
            self._hard_reset()

    @staticmethod
    def _hard_reset():
        """⛔ 공개 `reset` 은 진행 중이면 **거부**하는 게 계약이라, 한 테스트가 관측을 흘리면
        이후 모든 테스트의 setUp 이 막힌다(실측: 20건 연쇄 실패). 테스트 격리는 내부 상태를
        직접 갈아끼운다 — 공개 계약은 아래 TestResetContract 가 따로 잠근다."""
        with slm._lock:
            slm._metrics.clear()
            slm._metrics.update(slm._blank())

    def _snapshot(self):
        return slm.subscribe_load_metrics()

    def _invariant(self, snap):
        """⛔ 이 부등식이 캡처 측 검증의 근거다."""
        for axis in slm.AXIS_OUTCOMES:
            block = snap[axis]
            self.assertEqual(
                block["started_total"],
                sum(block["by_outcome"].values()) + block["callers_awaiting"],
                f"{axis}: started != sum(by_outcome) + callers_awaiting",
            )


class TestFieldContract(SubscribeLoadTestCase):
    def test_blank_snapshot_has_fixed_cardinality(self):
        snap = self._snapshot()
        self.assertEqual(snap["contract_version"], "subscribe-load/4")
        self.assertEqual(snap["scope"], "process")
        for axis, outcomes in slm.AXIS_OUTCOMES.items():
            self.assertEqual(sorted(snap[axis]["by_outcome"]), sorted(outcomes))
        # ⛔ 저장 key 는 제출 allowlist(CHANNELS/SEND_OUTCOMES)가 아니라 storage 스키마다.
        self.assertEqual(sorted(snap["snapshot_send"]["calls_by_channel"]), sorted(slm.CHANNEL_KEYS))
        self.assertEqual(sorted(snap["snapshot_send"]["sends_by_outcome"]), sorted(slm.SEND_OUTCOME_KEYS))
        self.assertNotIn("unclassified", slm.CHANNELS, "unclassified 는 storage-only 다")
        self.assertNotIn("unclassified", slm.SEND_OUTCOMES, "unclassified 는 storage-only 다")

    def test_contract_constants_are_locked_literally(self):
        """⛔ `slm.*` 자기참조 비교는 상수 축소를 못 잡는다 — `CHANNEL_KEYS = CHANNELS + (...)`
        파생이라 함께 줄어 통과했다(실측: token_bearing / lease_skipped / unavailable_persistent /
        built 제거 변이 4종 전부 생존). 기대값은 여기 **literal** 로 박는다."""
        self.assertEqual(slm.CHANNELS, ("anonymous", "token_bearing", "unattributed"))
        self.assertEqual(slm.SEND_OUTCOMES, ("sent", "lease_skipped", "connection_closed", "raised"))
        self.assertEqual(slm.SUBMITTABLE_OUTCOMES, {
            slm.PREMIUM_RC: frozenset(
                {"granted", "denied", "unavailable_transient", "unavailable_persistent"}),
            slm.KRX_ENTITLEMENT: frozenset(
                {"granted", "denied", "unavailable_transient", "unavailable_persistent"}),
            slm.SNAPSHOT_BUILD: frozenset({"built", "none_payload"}),
        })
        self.assertEqual(slm.AXIS_OUTCOMES, {
            slm.PREMIUM_RC: (
                "granted", "denied", "unavailable_transient", "unavailable_persistent",
                "cancelled", "raised", "unclassified"),
            slm.KRX_ENTITLEMENT: (
                "granted", "denied", "unavailable_transient", "unavailable_persistent",
                "cancel_worker_start_not_observed", "cancel_worker_start_observed",
                "raised", "unclassified"),
            slm.SNAPSHOT_BUILD: (
                "built", "none_payload", "build_failed",
                "cancel_worker_start_not_observed", "cancel_worker_start_observed", "unclassified"),
        })

    def test_schema_consistency_is_enforced_at_import(self):
        """⛔ SUBMITTABLE ⊆ storage 가 깨지면 축 경로는 종료 전이 KeyError → half-open +
        awaiting 호출당 영구 누수(불변식 1==0+1 유지라 corrupt 검사 무력), send 경로는 관측
        통째 소실(실측 probe). 편집 실수는 import 실패로 잡는다."""
        import ast
        import inspect
        from unittest.mock import patch

        full_premium = slm.SUBMITTABLE_OUTCOMES[slm.PREMIUM_RC]
        bad_cases = [
            ("제출이 저장 밖 (partition 위반)", {**slm.SUBMITTABLE_OUTCOMES,
                                slm.PREMIUM_RC: frozenset({"granted", "ghost"})}),
            # ⛔ partition 은 유지되고 서로소만 깨지는 케이스 — 도메인 4값 전부 + 파생 키 1개.
            #    부분집합으로 만들면 partition 검사가 대신 잡아 disjoint 검사 제거 변이가 산다.
            ("제출·파생 겹침 (disjoint 위반)", {**slm.SUBMITTABLE_OUTCOMES,
                                slm.PREMIUM_RC: full_premium | {"cancelled"}}),
            ("축 집합 불일치", {k: v for k, v in slm.SUBMITTABLE_OUTCOMES.items()
                                if k != slm.PREMIUM_RC}),
        ]
        for label, bad in bad_cases:
            with self.subTest(case=label):
                with patch.object(slm, "SUBMITTABLE_OUTCOMES", bad):
                    with self.assertRaises(slm.SubscribeLoadContractError):
                        slm._validate_schema()
        # snapshot_send 쪽 불일치도 각각 독립으로 잠근다 — 축 케이스만 있으면
        # send/channel 저장 key 검사 제거 변이가 산다(실측).
        send_channel_cases = [
            ("send 제출에 unclassified 침투", "SEND_OUTCOMES",
             ("sent", "lease_skipped", "connection_closed", "raised", "unclassified")),
            ("send 저장 key 에 unclassified 누락", "SEND_OUTCOME_KEYS",
             ("sent", "lease_skipped", "connection_closed", "raised")),
            ("channel 제출에 unclassified 침투", "CHANNELS",
             ("anonymous", "token_bearing", "unattributed", "unclassified")),
            ("channel 저장 key 에 unclassified 누락", "CHANNEL_KEYS",
             ("anonymous", "token_bearing", "unattributed")),
        ]
        for label, attr, bad in send_channel_cases:
            with self.subTest(case=label):
                with patch.object(slm, attr, bad):
                    with self.assertRaises(slm.SubscribeLoadContractError):
                        slm._validate_schema()
        slm._validate_schema()  # 현행 상수는 통과
        # ⛔ import-time 호출 자체를 AST 로 잠근다 — 가드가 있어도 안 불리면 무의미하다.
        tree = ast.parse(inspect.getsource(slm))
        top_level_calls = [
            n.value.func.id for n in tree.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
        ]
        self.assertIn("_validate_schema", top_level_calls,
                      "_validate_schema() 가 모듈 top-level 에서 호출되지 않는다")

    def test_exact_key_sets_are_locked(self):
        """⛔ '있다/없다' 만 보면 임의 필드 추가(예: 파생 중복 `snapshot_calls_total`)를 못 잡는다."""
        snap = self._snapshot()
        self.assertEqual(sorted(snap), sorted(
            ["contract_version", "scope", "caveat", "metrics_internal_errors_total",
             slm.PREMIUM_RC, slm.KRX_ENTITLEMENT, slm.SNAPSHOT_BUILD, "snapshot_send"]))
        caller_fields = ["started_total", "by_outcome", "duration_ms_sum", "duration_ms_max",
                         "callers_awaiting", "callers_awaiting_max"]
        worker_fields = ["worker_started_total", "worker_finished_total",
                         "worker_in_flight", "worker_in_flight_max"]
        self.assertEqual(sorted(snap[slm.PREMIUM_RC]), sorted(caller_fields),
                         "premium 에 worker 축이 생기면 '정상 0' 과 '고장 0' 이 섞인다")
        for axis in (slm.KRX_ENTITLEMENT, slm.SNAPSHOT_BUILD):
            self.assertEqual(sorted(snap[axis]), sorted(caller_fields + worker_fields))
        self.assertEqual(sorted(snap["snapshot_send"]),
                         sorted(["calls_by_channel", "topics_deduped_total", "sends_by_outcome"]))

    def test_unknown_axis_is_refused(self):
        with self.assertRaises(slm.SubscribeLoadContractError):
            _run(self._observe_once("no_such_axis", "granted"))

    async def _observe_once(self, axis, outcome):
        async with slm.observe(axis) as handle:
            handle.finish(outcome)


class TestTerminalTransition(SubscribeLoadTestCase):
    async def _normal(self, axis, outcome):
        async with slm.observe(axis) as handle:
            handle.finish(outcome)

    def test_normal_finish_counts_once_and_releases_the_gauge(self):
        _run(self._normal(slm.PREMIUM_RC, "granted"))
        snap = self._snapshot()
        block = snap[slm.PREMIUM_RC]
        self.assertEqual(block["started_total"], 1)
        self.assertEqual(block["by_outcome"]["granted"], 1)
        self.assertEqual(block["callers_awaiting"], 0)
        self.assertEqual(block["callers_awaiting_max"], 1)
        self.assertGreaterEqual(block["duration_ms_sum"], 0.0)
        self._invariant(snap)

    def test_missing_outcome_becomes_unclassified_with_an_internal_error(self):
        async def scenario():
            async with slm.observe(slm.PREMIUM_RC):
                pass
        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
            _run(scenario())
        snap = self._snapshot()
        self.assertEqual(snap[slm.PREMIUM_RC]["by_outcome"]["unclassified"], 1)
        self.assertEqual(snap["metrics_internal_errors_total"], 1)
        self._invariant(snap)

    def test_contract_error_propagates_without_polluting_external_failures(self):
        """계측 배선 오류는 모든 축에서 unclassified 진단으로 남고 본문 밖으로 전파된다."""
        async def scenario(axis):
            async with slm.observe(axis):
                raise slm.SubscribeLoadContractError("axis/handle mismatch")

        for axis in slm.AXIS_OUTCOMES:
            with self.subTest(axis=axis):
                slm.reset_subscribe_load_metrics()
                with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                    with self.assertRaisesRegex(slm.SubscribeLoadContractError, "axis/handle mismatch") as ctx:
                        _run(scenario(axis))
                # ⛔ 진단 문구가 오진이면 안 된다 — "미지정/제출 불가" 가 아니라 계약 오류이고,
                #    원본 예외가 exc_info 로 실려야 한다.
                record = next(r for r in logs.records if "계측 계약 오류" in r.getMessage())
                self.assertIsNotNone(record.exc_info)
                self.assertIs(record.exc_info[1], ctx.exception)
                snap = self._snapshot()
                block = snap[axis]
                self.assertEqual(block["by_outcome"][slm._RAISED_OUTCOME[axis]], 0)
                self.assertEqual(block["by_outcome"]["unclassified"], 1)
                self.assertEqual(block["callers_awaiting"], 0)
                self.assertEqual(snap["metrics_internal_errors_total"], 1)
                self._invariant(snap)

    def test_outcome_outside_the_allowlist_never_creates_a_key(self):
        async def scenario(value):
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish(value)
        for value in ("made_up", 7, None, ["unhashable"]):
            with self.subTest(value=value):
                slm.reset_subscribe_load_metrics()
                with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
                    _run(scenario(value))
                snap = self._snapshot()
                self.assertEqual(sorted(snap[slm.PREMIUM_RC]["by_outcome"]),
                                 sorted(slm.AXIS_OUTCOMES[slm.PREMIUM_RC]))
                self.assertEqual(snap[slm.PREMIUM_RC]["by_outcome"]["unclassified"], 1)
                self._invariant(snap)

    def test_duplicate_finish_keeps_the_first_and_does_not_re_transition(self):
        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish("granted")
                handle.finish("denied")
        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
            _run(scenario())
        snap = self._snapshot()
        block = snap[slm.PREMIUM_RC]
        self.assertEqual(block["by_outcome"]["granted"], 1)
        self.assertEqual(block["by_outcome"]["denied"], 0)
        self.assertEqual(snap["metrics_internal_errors_total"], 1)
        self._invariant(snap)

    def test_duplicate_finish_bookkeeping_failure_never_reaches_the_caller(self):
        """⛔ `finish()` 는 **본문 안**에서 불린다 — 그 부기가 예외를 올리면 계측이 서비스 코드에
        예외를 주입하는 셈이다. 중복/늦은 호출의 internal-error 기록도 best-effort 여야 한다."""
        from unittest.mock import patch

        class _FailingLock:
            def __enter__(self):
                raise RuntimeError("lock-failed")

            def __exit__(self, *exc):
                return False
        handle = slm.WorkHandle(slm.PREMIUM_RC)
        handle.finish("granted")
        with patch.object(slm, "_lock", _FailingLock()):
            handle.finish("denied")  # 예외가 나가면 red

    def test_late_finish_after_the_context_does_not_re_transition(self):
        holder = {}

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish("granted")
                holder["handle"] = handle
        _run(scenario())
        before = self._snapshot()
        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
            holder["handle"].finish("denied")
        after = self._snapshot()
        self.assertEqual(after[slm.PREMIUM_RC]["by_outcome"], before[slm.PREMIUM_RC]["by_outcome"])
        self.assertEqual(after["metrics_internal_errors_total"],
                         before["metrics_internal_errors_total"] + 1)

    def test_cancellation_wins_over_a_recorded_candidate(self):
        """⛔ 예외·취소는 후보 유무와 **무관하게** 우선한다."""
        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish("granted")
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            _run(scenario())
        snap = self._snapshot()
        block = snap[slm.PREMIUM_RC]
        self.assertEqual(block["by_outcome"]["cancelled"], 1)
        self.assertEqual(block["by_outcome"]["granted"], 0)
        self._invariant(snap)

    def test_exception_wins_and_maps_per_axis(self):
        async def scenario(axis):
            async with slm.observe(axis) as handle:
                handle.finish("granted" if axis != slm.SNAPSHOT_BUILD else "built")
                raise RuntimeError("boom")
        for axis, expected in ((slm.PREMIUM_RC, "raised"),
                               (slm.KRX_ENTITLEMENT, "raised"),
                               (slm.SNAPSHOT_BUILD, "build_failed")):
            with self.subTest(axis=axis):
                slm.reset_subscribe_load_metrics()
                with self.assertRaises(RuntimeError):
                    _run(scenario(axis))
                snap = self._snapshot()
                self.assertEqual(snap[axis]["by_outcome"][expected], 1)
                self._invariant(snap)

    def test_body_exception_is_never_swallowed(self):
        async def scenario():
            async with slm.observe(slm.PREMIUM_RC):
                raise ValueError("본문 예외")
        with self.assertRaises(ValueError):
            _run(scenario())


class TestBookkeepingIsFailSafe(SubscribeLoadTestCase):
    """⛔ 계측은 관측이지 정책이 아니다 — 부기 실패가 본문 결과를 **대체**하면 안 된다."""

    def test_resolution_failure_preserves_the_body_exception_and_releases_the_gauge(self):
        from unittest.mock import patch

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish("granted")
                raise ValueError("body-failed")
        with patch.object(slm, "_resolve_outcome", side_effect=RuntimeError("metrics-failed")):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
                with self.assertRaises(ValueError):
                    _run(scenario())
        snap = self._snapshot()
        self.assertEqual(snap[slm.PREMIUM_RC]["callers_awaiting"], 0,
                         "전이를 건너뛰면 게이지가 영구 누수된다")
        self.assertEqual(snap[slm.PREMIUM_RC]["by_outcome"]["unclassified"], 1)
        self.assertEqual(snap["metrics_internal_errors_total"], 1)
        self._invariant(snap)

    def test_resolution_failure_preserves_a_normal_return(self):
        from unittest.mock import patch

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish("granted")
                return "ok"
        with patch.object(slm, "_resolve_outcome", side_effect=RuntimeError("metrics-failed")):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
                self.assertEqual(_run(scenario()), "ok")
        self._invariant(self._snapshot())

    def test_clock_failure_preserves_every_exit_path_and_closes_the_transition(self):
        """⛔ duration 계산이 보호 밖이면 종료 clock 실패가 **전파 예외를 바꾸고 게이지를
        누수**시킨다(실측). 정상 반환·본문 예외·취소 세 경로 모두 잠근다."""
        from unittest.mock import patch

        class _BoomClock:
            def __init__(self):
                self._n = 0

            def monotonic(self):
                self._n += 1
                if self._n > 1:
                    raise RuntimeError("clock-failed")
                return 100.0

        async def normal():
            async with slm.observe(slm.PREMIUM_RC) as h:
                h.finish("granted")
                return "ok"

        async def body_raises():
            async with slm.observe(slm.PREMIUM_RC) as h:
                h.finish("granted")
                raise ValueError("body-failed")

        async def body_cancels():
            async with slm.observe(slm.PREMIUM_RC):
                raise asyncio.CancelledError()
        for label, factory, expected in (("normal", normal, None),
                                         ("raised", body_raises, ValueError),
                                         ("cancelled", body_cancels, asyncio.CancelledError)):
            with self.subTest(path=label):
                self._hard_reset()
                with patch.object(slm, "time", _BoomClock()):
                    with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
                        if expected is None:
                            self.assertEqual(_run(factory()), "ok")
                        else:
                            with self.assertRaises(expected):
                                _run(factory())
                snap = self._snapshot()
                self.assertEqual(snap[slm.PREMIUM_RC]["callers_awaiting"], 0)
                self.assertEqual(snap[slm.PREMIUM_RC]["by_outcome"]["unclassified"], 1)
                self._invariant(snap)

    def test_degraded_warning_carries_the_metrics_exception_not_the_body_one(self):
        """⛔ `exc_info=True` 로 두면 **본문 예외**가 찍혀 정작 잡은 계측 예외가 사라진다(실측).
        진단이 거꾸로 되면 계측 결함을 서비스 결함으로 오독한다."""
        from unittest.mock import patch

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as h:
                h.finish("granted")
                raise ValueError("body-failed")
        with patch.object(slm, "_resolve_outcome", side_effect=RuntimeError("metrics-failed")):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                with self.assertRaises(ValueError):
                    _run(scenario())
        record = next(r for r in logs.records if "분류" in r.getMessage())
        self.assertIsNotNone(record.exc_info, "WARNING 이 예외를 싣지 않았다")
        self.assertIs(record.exc_info[0], RuntimeError,
                      "잡은 계측 예외가 아니라 본문 예외가 찍혔다")
        self.assertIn("metrics-failed", str(record.exc_info[1]))

    def test_entry_bookkeeping_failure_never_blocks_the_body(self):
        """⛔ 진입 clock·lock 실패가 본문을 **한 번도 실행하지 않고** 예외로 나갔다(실측).
        계측이 서비스 경로를 결정하면 그건 관측이 아니라 정책이다."""
        from unittest.mock import patch

        ran = {"body": 0}

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                ran["body"] += 1
                handle.finish("granted")
                return "ok"

        class _FailingClock:
            def monotonic(self):
                raise RuntimeError("first-clock-failed")

        class _FailingLock:
            def __enter__(self):
                raise RuntimeError("entry-lock-failed")

            def __exit__(self, *exc):
                return False
        async def body_raises():
            async with slm.observe(slm.PREMIUM_RC):
                ran["body"] += 1
                raise ValueError("body-failed")

        async def body_cancels():
            async with slm.observe(slm.PREMIUM_RC):
                ran["body"] += 1
                raise asyncio.CancelledError()
        for label, attr, replacement, marker in (
            ("clock", "time", _FailingClock(), "first-clock-failed"),
            ("lock", "_lock", _FailingLock(), "entry-lock-failed"),
        ):
            for path, factory, expected in (("normal", scenario, None),
                                            ("raised", body_raises, ValueError),
                                            ("cancelled", body_cancels, asyncio.CancelledError)):
                with self.subTest(entry=label, path=path):
                    self._hard_reset()
                    ran["body"] = 0
                    with patch.object(slm, attr, replacement):
                        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                            if expected is None:
                                self.assertEqual(_run(factory()), "ok")
                            else:
                                # ⛔ `finally` 안 `return` 은 대기 중인 예외를 **삼킨다**(실측).
                                with self.assertRaises(expected):
                                    _run(factory())
                    self.assertEqual(ran["body"], 1, "본문이 실행되지 않았다")
                    record = next(r for r in logs.records if "진입" in r.getMessage())
                    self.assertIsNotNone(record.exc_info)
                    self.assertIn(marker, str(record.exc_info[1]))
                    snap = self._snapshot()
                    block = snap[slm.PREMIUM_RC]
                    self.assertEqual(block["started_total"], 0, "진입 실패는 장부에 남지 않는다")
                    self.assertEqual(block["callers_awaiting"], 0, "게이지가 음수/누수가 되면 안 된다")
                    self._invariant(snap)

    def test_diagnostic_emission_never_decides_the_service_result(self):
        """⛔ 로깅 실패가 결과를 바꾸면 그건 관측이 아니라 정책이다 — 진입 WARNING 실패는
        본문을 아예 막았고, 종료 WARNING 실패는 본문 예외를 대체했다(실측)."""
        from unittest.mock import patch

        ran = {"body": 0}

        def _boom(*args, **kwargs):
            raise RuntimeError("logger-failed")

        class _FailingClock:
            def monotonic(self):
                raise RuntimeError("first-clock-failed")

        async def normal():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                ran["body"] += 1
                handle.finish("granted")
                return "ok"

        async def body_raises():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                ran["body"] += 1
                handle.finish("granted")
                raise ValueError("body-failed")
        with patch.object(slm.logger, "warning", _boom):
            with patch.object(slm, "time", _FailingClock()):
                self.assertEqual(_run(normal()), "ok")
            self.assertEqual(ran["body"], 1)
            self._hard_reset()
            with patch.object(slm, "_resolve_outcome", side_effect=RuntimeError("metrics-failed")):
                with self.assertRaises(ValueError):
                    _run(body_raises())
        self._hard_reset()

    def test_terminal_transition_failure_counts_an_internal_error_when_it_can(self):
        """⛔ 이 경로만 WARNING 만 내고 counter 는 시도하지 않았다(실측). lock 이 **일시적으로**
        실패한 경우엔 counter 가 올라야 "격하는 조용하지 않다" 가 성립한다."""
        from unittest.mock import patch

        real = slm._lock

        class _FailOnceOnSecond:
            def __init__(self):
                self._n = 0

            def __enter__(self):
                self._n += 1
                if self._n == 2:
                    raise RuntimeError("one-shot-lock-failure")
                return real.__enter__()

            def __exit__(self, *exc):
                return False if self._n == 2 else real.__exit__(*exc)

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish("granted")
                return "ok"
        with patch.object(slm, "_lock", _FailOnceOnSecond()):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
                self.assertEqual(_run(scenario()), "ok")
        self.assertEqual(self._snapshot()["metrics_internal_errors_total"], 1)
        self._hard_reset()

    def test_terminal_transition_failure_leaves_a_half_open_observation(self):
        """전이가 깨지면 관측이 사라지는 게 아니라 **half-open/부분 전이** 로 남는다.
        ⚠️ 정확한 모양은 실패 지점에 달렸다 — 여기서 주입한 **종료 lock 획득 실패**에서는
        `started=1 / awaiting=1 / terminal 0` 이고, 임계구역 **안**에서 깨지면 일부 필드만
        먼저 바뀔 수 있다. 어느 쪽이든 공개 reset 이 거부한다."""
        from unittest.mock import patch

        real = slm._lock
        entered = {"count": 0}

        class _FailOnSecond:
            def __enter__(self):
                entered["count"] += 1
                if entered["count"] >= 2:
                    raise RuntimeError("terminal-lock-failed")
                return real.__enter__()

            def __exit__(self, *exc):
                if entered["count"] >= 2:
                    return False
                return real.__exit__(*exc)

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                handle.finish("granted")
                return "ok"
        with patch.object(slm, "_lock", _FailOnSecond()):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                self.assertEqual(_run(scenario()), "ok", "본문 결과가 보존되지 않았다")
        record = next(r for r in logs.records if "전이 실패" in r.getMessage())
        self.assertIn("terminal-lock-failed", str(record.exc_info[1]))
        block = self._snapshot()[slm.PREMIUM_RC]
        self.assertEqual(block["started_total"], 1)
        self.assertEqual(block["callers_awaiting"], 1)
        self.assertEqual(sum(block["by_outcome"].values()), 0)
        with self.assertRaises(slm.SubscribeLoadContractError):
            slm.reset_subscribe_load_metrics()
        self._hard_reset()

    def test_only_cancelled_error_counts_as_cancellation(self):
        """⛔ `not isinstance(exc, Exception)` 으로 넓히면 SystemExit·KeyboardInterrupt·
        GeneratorExit 까지 취소로 기록된다(실측)."""
        for exc_type in (SystemExit, KeyboardInterrupt):
            with self.subTest(exc=exc_type.__name__):
                slm.reset_subscribe_load_metrics()

                async def scenario():
                    async with slm.observe(slm.PREMIUM_RC):
                        raise exc_type("stop")
                with self.assertRaises(exc_type):
                    _run(scenario())
                block = self._snapshot()[slm.PREMIUM_RC]["by_outcome"]
                self.assertEqual(block["cancelled"], 0)
                self.assertEqual(block["raised"], 1)


class TestDurationAccounting(SubscribeLoadTestCase):
    """취소·예외 경로도 duration 을 **한 번** 기록한다(공통 부수효과)."""

    def _fake_clock(self, values):
        """⛔ 전역 `time.monotonic` 을 패치하면 logging·pytest 도 그걸 불러 side_effect 가
        소진되고(StopIteration) 관측이 흘러 이후 테스트가 줄줄이 막힌다(실측). 모듈
        네임스페이스의 `time` **참조만** 갈아끼우고, 여분 호출은 마지막 값을 돌려준다."""
        from unittest.mock import patch

        class _Clock:
            def __init__(self, seq):
                self._seq = list(seq)
                self._i = 0

            def monotonic(self):
                value = self._seq[min(self._i, len(self._seq) - 1)]
                self._i += 1
                return value
        return patch.object(slm, "time", _Clock(values))

    def test_duration_is_recorded_on_normal_cancel_and_raise(self):
        async def normal():
            async with slm.observe(slm.PREMIUM_RC) as h:
                h.finish("granted")

        async def cancelled():
            async with slm.observe(slm.PREMIUM_RC):
                raise asyncio.CancelledError()

        async def raised():
            async with slm.observe(slm.PREMIUM_RC):
                raise RuntimeError("x")
        for label, coro_factory, exc in (("normal", normal, None),
                                         ("cancelled", cancelled, asyncio.CancelledError),
                                         ("raised", raised, RuntimeError)):
            with self.subTest(path=label):
                slm.reset_subscribe_load_metrics()
                with self._fake_clock([100.0, 100.25]):
                    if exc is None:
                        _run(coro_factory())
                    else:
                        with self.assertRaises(exc):
                            _run(coro_factory())
                block = self._snapshot()[slm.PREMIUM_RC]
                self.assertAlmostEqual(block["duration_ms_sum"], 250.0, places=3)
                self.assertAlmostEqual(block["duration_ms_max"], 250.0, places=3)


class TestCancelSubclassification(SubscribeLoadTestCase):
    """⛔ 두 키는 **관측 사실**이다 — 어느 쪽도 'DB 세션 0' 을 함의하지 않는다."""

    def test_cancel_without_observed_worker_start(self):
        async def scenario():
            async with slm.observe(slm.KRX_ENTITLEMENT):
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            _run(scenario())
        snap = self._snapshot()
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["by_outcome"]["cancel_worker_start_not_observed"], 1)
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["by_outcome"]["cancel_worker_start_observed"], 0)

    def test_cancel_after_observed_worker_start(self):
        async def scenario():
            async with slm.observe(slm.KRX_ENTITLEMENT) as handle:
                handle.mark_worker_started()
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            _run(scenario())
        snap = self._snapshot()
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["by_outcome"]["cancel_worker_start_observed"], 1)


class TestWorkerAxis(SubscribeLoadTestCase):
    def test_worker_axis_counts_and_derives_in_flight(self):
        """⚠️ 첫 판에서 `await to_thread(...) or handle.finish(...)` 로 썼더니 결과가 truthy 라
        `finish()` 가 **한 번도 안 불렸고**(outcome=unclassified) worker 필드만 봐서 통과했다."""
        async def scenario():
            async with slm.observe(slm.KRX_ENTITLEMENT) as handle:
                value = await asyncio.to_thread(
                    slm.timed_call, slm.KRX_ENTITLEMENT, handle, lambda v: v * 2, 21,
                )
                handle.finish("granted")
                return value
        self.assertEqual(_run(scenario()), 42)
        full = self._snapshot()
        snap = full[slm.KRX_ENTITLEMENT]
        self.assertEqual(snap["worker_started_total"], 1)
        self.assertEqual(snap["worker_finished_total"], 1)
        self.assertEqual(snap["worker_in_flight"], 0)
        self.assertEqual(snap["worker_in_flight_max"], 1)
        self.assertEqual(snap["by_outcome"]["granted"], 1)
        self.assertEqual(snap["by_outcome"]["unclassified"], 0)
        self.assertEqual(full["metrics_internal_errors_total"], 0)
        self._invariant(full)

    def test_timed_call_runs_on_a_different_thread_than_the_caller(self):
        seen = {}

        async def scenario():
            async with slm.observe(slm.SNAPSHOT_BUILD) as handle:
                seen["caller"] = threading.get_ident()
                await asyncio.to_thread(
                    slm.timed_call, slm.SNAPSHOT_BUILD, handle,
                    lambda: seen.__setitem__("worker", threading.get_ident()),
                )
                handle.finish("built")
        _run(scenario())
        self.assertNotEqual(seen["caller"], seen["worker"],
                            "loop 스레드에서 직접 부르면 to_thread 격리가 사라진다")

    def test_handle_axis_must_match_the_axis_argument(self):
        """⛔ 배선 오타를 통과시키면 worker counter 는 A 축, worker-start 관측은 B 축 handle 에
        남아 두 축이 갈린다(실측)."""
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        with self.assertRaises(slm.SubscribeLoadContractError):
            slm.timed_call(slm.KRX_ENTITLEMENT, handle, lambda: None)
        snap = self._snapshot()
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["worker_started_total"], 0)
        self.assertFalse(handle.worker_start_observed)

    def test_timed_call_passes_keyword_arguments_through(self):
        """⛔ 키워드가 조용히 사라지면 프레임·로그 어디에도 흔적이 없다(auth_executor 선례)."""
        seen = {}

        def target(a, *, mono=None):
            seen["a"], seen["mono"] = a, mono
            return a
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        slm.timed_call(slm.SNAPSHOT_BUILD, handle, target, 3, mono="clock")
        self.assertEqual(seen, {"a": 3, "mono": "clock"})

    def test_timed_call_marks_worker_start_before_calling(self):
        order = []
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)

        def target():
            order.append(handle.worker_start_observed)
        slm.timed_call(slm.SNAPSHOT_BUILD, handle, target)
        self.assertEqual(order, [True], "worker-start 를 fn 뒤에 set 하면 취소 분류가 뒤집힌다")

    def test_worker_finished_increments_even_when_the_target_raises(self):
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        with self.assertRaises(RuntimeError):
            slm.timed_call(slm.SNAPSHOT_BUILD, handle, lambda: (_ for _ in ()).throw(RuntimeError("x")))
        snap = self._snapshot()[slm.SNAPSHOT_BUILD]
        self.assertEqual((snap["worker_started_total"], snap["worker_finished_total"]), (1, 1))
        self.assertEqual(snap["worker_in_flight"], 0)

    def test_positional_only_protects_targets_that_share_parameter_names(self):
        """⛔ positional-only 가 없으면 대상 함수의 `axis`/`handle`/`fn` 키워드가 wrapper 의
        동명 파라미터와 **충돌**한다(`TypeError: multiple values for argument`). 대상이 그 이름을
        쓸 수 있어야 하므로 wrapper 쪽을 positional-only 로 닫는다."""
        seen = {}

        def target(*, axis=None, handle=None, fn=None):
            seen.update(axis=axis, handle=handle, fn=fn)
        h = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        slm.timed_call(slm.SNAPSHOT_BUILD, h, target, axis="A", handle="H", fn="F")
        self.assertEqual(seen, {"axis": "A", "handle": "H", "fn": "F"})

    def test_worker_bookkeeping_failure_never_reaches_the_service_path(self):
        """⛔ `timed_call` 은 **실제 DB/build 함수**를 감싼다 — 그 부기가 예외를 올리면 계측이
        서비스 결과를 결정한다(실측: 진입 lock 실패 → target 0회, 종료 lock 실패 → 반환값 대체).
        ⚠️ 축·결속 **검증**은 계약 위반이라 계속 예외여야 한다."""
        from unittest.mock import patch

        class _FailingLock:
            def __enter__(self):
                raise RuntimeError("lock-failed")

            def __exit__(self, *exc):
                return False
        ran = {"n": 0}

        def target():
            ran["n"] += 1
            return "ok"
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        with patch.object(slm, "_lock", _FailingLock()):
            self.assertEqual(slm.timed_call(slm.SNAPSHOT_BUILD, handle, target), "ok")
            # 계약 위반은 여전히 예외
            with self.assertRaises(slm.SubscribeLoadContractError):
                slm.timed_call(slm.KRX_ENTITLEMENT, handle, target)
        self.assertEqual(ran["n"], 1)
        self._hard_reset()

    def test_worker_gauge_never_goes_negative_when_entry_bookkeeping_failed(self):
        """진입을 못 셌으면 종료도 세지 않는다 — 아니면 `worker_in_flight` 가 음수가 된다."""
        from unittest.mock import patch

        class _FailOnFirst:
            def __init__(self, real):
                self._real, self._n = real, 0

            def __enter__(self):
                self._n += 1
                if self._n == 1:
                    raise RuntimeError("entry-lock-failed")
                return self._real.__enter__()

            def __exit__(self, *exc):
                return self._real.__exit__(*exc) if self._n > 1 else False
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        with patch.object(slm, "_lock", _FailOnFirst(slm._lock)):
            slm.timed_call(slm.SNAPSHOT_BUILD, handle, lambda: None)
        snap = self._snapshot()[slm.SNAPSHOT_BUILD]
        self.assertGreaterEqual(snap["worker_in_flight"], 0)
        self.assertEqual(snap["worker_started_total"], snap["worker_finished_total"])

    def test_handle_creation_failure_degrades_instead_of_blocking_the_body(self):
        """⛔ `threading.Event()` **생성**도 계측 동작이다 — 보호 없이 두면 본문이 0회 실행된다
        (실측). 실패하면 bool fallback 으로 격하하고 관측은 계속된다."""
        from unittest.mock import patch

        ran = {"body": 0}

        async def scenario():
            async with slm.observe(slm.KRX_ENTITLEMENT) as handle:
                ran["body"] += 1
                handle.mark_worker_started()
                self.assertTrue(handle.worker_start_observed, "fallback 이 관측을 잃었다")
                handle.finish("granted")
                return "ok"
        with patch.object(slm.threading, "Event", side_effect=RuntimeError("event-init-failed")):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                self.assertEqual(_run(scenario()), "ok")
        self.assertEqual(ran["body"], 1)
        # ⛔ 격하는 조용하면 안 된다 — lock·logger 가 멀쩡한 경로이므로 둘 다 남아야 한다.
        self.assertTrue(any("Event 생성 실패" in r.getMessage() for r in logs.records))
        snap = self._snapshot()
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["by_outcome"]["granted"], 1)
        self.assertGreaterEqual(snap["metrics_internal_errors_total"], 1)
        self._invariant(snap)

    def test_worker_start_observation_failure_preserves_the_target(self):
        """⛔ `mark_worker_started()` 도 계측 동작이다 — 보호 밖에 두니 실패가 전파되고
        target 이 0회 실행됐다(실측). `Event.set()` 이 안 던진다는 건 구현 세부이지 계약이 아니다."""
        from unittest.mock import patch

        ran = {"n": 0}
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)

        def _boom(self):
            raise RuntimeError("event-set-failed")
        with patch.object(slm.WorkHandle, "mark_worker_started", _boom):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                self.assertEqual(
                    slm.timed_call(slm.SNAPSHOT_BUILD, handle,
                                   lambda: (ran.__setitem__("n", 1), "ok")[1]),
                    "ok")
        self.assertEqual(ran["n"], 1)
        # ⛔ "조용히 degrade 하지 않는다" 가 계약이다 — 진단을 지워도 통과하면 안 된다.
        self.assertTrue(any("worker-start" in r.getMessage() for r in logs.records))
        self.assertEqual(self._snapshot()["metrics_internal_errors_total"], 1)
        snap = self._snapshot()[slm.SNAPSHOT_BUILD]
        self.assertEqual(snap["worker_started_total"], 1, "관측 실패해도 counter 는 계속 센다")
        self.assertEqual(snap["worker_finished_total"], 1)
        self._hard_reset()

    def test_worker_exit_bookkeeping_failure_preserves_the_target_result(self):
        """⛔ 진입은 성공하고 **종료만** 실패하는 경로를 따로 잠근다 — 둘 다 실패시키면
        `counted=False` 로 종료 부기가 아예 실행되지 않아 그 계약이 검사되지 않는다(실측)."""
        from unittest.mock import patch

        real = slm._lock

        class _FailOnSecond:
            def __init__(self):
                self._n = 0

            def __enter__(self):
                self._n += 1
                if self._n >= 2:
                    raise RuntimeError("exit-lock-failed")
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc) if self._n < 2 else False
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        with patch.object(slm, "_lock", _FailOnSecond()):
            self.assertEqual(slm.timed_call(slm.SNAPSHOT_BUILD, handle, lambda: "ok"), "ok")
        self._hard_reset()

    def test_snapshot_recorders_are_no_throw(self):
        from unittest.mock import patch

        class _FailingLock:
            def __enter__(self):
                raise RuntimeError("lock-failed")

            def __exit__(self, *exc):
                return False
        with patch.object(slm, "_lock", _FailingLock()):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                slm.record_snapshot_call("anonymous")
                slm.record_snapshot_topic()
                slm.record_snapshot_send("sent")
        self.assertEqual(sum("snapshot 기록 실패" in r.getMessage() for r in logs.records), 3)
        self._hard_reset()

    def test_premium_axis_rejects_timed_call(self):
        handle = slm.WorkHandle(slm.PREMIUM_RC)
        with self.assertRaises(slm.SubscribeLoadContractError):
            slm.timed_call(slm.PREMIUM_RC, handle, lambda: None)


class TestSnapshotSendAxis(SubscribeLoadTestCase):
    def test_unknown_values_fold_to_unclassified_not_into_semantic_buckets(self):
        """⛔ allowlist 밖 값을 의미 버킷으로 접으면 그 버킷이 오염된다(실측: typo →
        `raised`=1 은 실제 전송 예외 신호를, typo → `unattributed`=1 은 channel 배선 gap 신호를
        각각 오염시켰고 진단은 0이었다). unclassified + internal error + WARNING 이 계약이다."""
        slm.record_snapshot_call("anonymous")
        slm.record_snapshot_topic()
        slm.record_snapshot_send("sent")
        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
            slm.record_snapshot_call("made_up_channel")
            slm.record_snapshot_send("who_knows")
        snap = self._snapshot()
        send = snap["snapshot_send"]
        self.assertEqual(send["calls_by_channel"]["anonymous"], 1)
        self.assertEqual(send["calls_by_channel"]["unclassified"], 1)
        self.assertEqual(send["calls_by_channel"]["unattributed"], 0, "배선 gap 버킷이 오염됐다")
        self.assertEqual(send["topics_deduped_total"], 1)
        self.assertEqual(send["sends_by_outcome"]["sent"], 1)
        self.assertEqual(send["sends_by_outcome"]["unclassified"], 1)
        self.assertEqual(send["sends_by_outcome"]["raised"], 0, "전송 예외 버킷이 오염됐다")
        self.assertEqual(sorted(send["calls_by_channel"]), sorted(slm.CHANNEL_KEYS))
        self.assertEqual(snap["metrics_internal_errors_total"], 2)
        self.assertEqual(sum("unclassified 로 접는다" in r.getMessage() for r in logs.records), 2)


class TestDerivedSentinelsAreNotSubmittable(SubscribeLoadTestCase):
    """⛔ 저장 스키마 ≠ 제출 allowlist. 저장 key 집합으로 검증하면 정상 종료 본문의
    `finish("cancelled")` 가 진짜 취소로, `finish("build_failed")` 가 진짜 build 실패로
    기록되고(실측: 파생 5종 제출 전부 해당 버킷 + internal error 0), 정확한 sentinel
    문자열이 fold 진단을 우회했다(실측). 취소 2종·raised·build_failed 는 `__aexit__` 의
    실제 예외·취소 종료에서만 파생되고, unclassified 는 terminal resolution 이 생성한다
    (미지정·제출 불가 값·분류 격하 — 정상 종료도 만든다). 어느 쪽도 제출로는 못 만든다."""

    def test_synthetic_derived_outcomes_fold_to_unclassified_with_diagnostics(self):
        cases = [
            (slm.PREMIUM_RC, "cancelled"),
            (slm.PREMIUM_RC, "raised"),
            (slm.PREMIUM_RC, "unclassified"),
            (slm.KRX_ENTITLEMENT, "cancel_worker_start_not_observed"),
            (slm.KRX_ENTITLEMENT, "cancel_worker_start_observed"),
            (slm.KRX_ENTITLEMENT, "raised"),
            (slm.SNAPSHOT_BUILD, "build_failed"),
            (slm.SNAPSHOT_BUILD, "cancel_worker_start_observed"),
        ]
        for axis, synthetic in cases:
            with self.subTest(axis=axis, synthetic=synthetic):
                self._hard_reset()

                async def scenario():
                    async with slm.observe(axis) as handle:
                        handle.finish(synthetic)
                with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                    _run(scenario())
                snap = self._snapshot()
                block = snap[axis]
                self.assertEqual(block["by_outcome"]["unclassified"], 1)
                if synthetic != "unclassified":
                    self.assertEqual(block["by_outcome"][synthetic], 0,
                                     f"파생 버킷 {synthetic} 이 제출로 오염됐다")
                self.assertEqual(snap["metrics_internal_errors_total"], 1,
                                 "unclassified 기록에는 internal error 가 짝으로 남아야 한다")
                self.assertTrue(any("allowlist 밖" in r.getMessage() for r in logs.records))
                self._invariant(snap)
        self._hard_reset()

    def test_terminal_fold_never_survives_without_its_internal_diagnosis(self):
        """snapshot point-counter 뿐 아니라 축 terminal fold 도 진단-먼저다.

        구 순서는 internal counter 쓰기만 지속 실패시 `unclassified=1`,
        `internal_errors=0`, `awaiting=0`을 남겼다. 불변식도 정상이라 reset 이
        그 진단 없는 관측을 삭제할 수 있었다.
        """
        from unittest.mock import patch

        class _FailInternalWrite(dict):
            def __setitem__(self, key, value):
                if key == "metrics_internal_errors_total":
                    raise RuntimeError("persistent internal-counter failure")
                return super().__setitem__(key, value)

        cases = (
            (slm.PREMIUM_RC, "cancelled"),
            (slm.KRX_ENTITLEMENT, "raised"),
            (slm.SNAPSHOT_BUILD, "build_failed"),
        )
        for axis, synthetic in cases:
            with self.subTest(axis=axis):
                self._hard_reset()

                async def scenario():
                    async with slm.observe(axis) as handle:
                        handle.finish(synthetic)

                with patch.object(slm, "_metrics", _FailInternalWrite(slm._metrics)):
                    with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                        _run(scenario())
                    snap = self._snapshot()
                    block = snap[axis]
                    self.assertEqual(block["by_outcome"]["unclassified"], 0)
                    self.assertEqual(snap["metrics_internal_errors_total"], 0)
                    self.assertEqual(block["callers_awaiting"], 1,
                                     "진단을 못 남겼으면 terminal 전이도 완료하지 않는다")
                    self.assertTrue(any("terminal 전이 실패" in r.getMessage()
                                        for r in logs.records))
                    with self.assertRaises(slm.SubscribeLoadContractError):
                        slm.reset_subscribe_load_metrics()
        self._hard_reset()

    def test_submittable_domain_outcomes_are_unchanged(self):
        """경계의 반대편 — 도메인 결과 제출은 진단 없이 그대로 기록된다.

        ⛔ 축당 1개 샘플이면 제출 집합 **축소** 변이가 생존한다(실측: unavailable_persistent /
        built 제거 변이 통과). 제출 가능한 **전수**를 순회한다."""
        for axis in slm.AXIS_OUTCOMES:
          for outcome in sorted(slm.SUBMITTABLE_OUTCOMES[axis]):
            with self.subTest(axis=axis, outcome=outcome):
                self._hard_reset()

                async def scenario():
                    async with slm.observe(axis) as handle:
                        handle.finish(outcome)
                _run(scenario())
                snap = self._snapshot()
                self.assertEqual(snap[axis]["by_outcome"][outcome], 1)
                self.assertEqual(snap["metrics_internal_errors_total"], 0)
        self._hard_reset()

    def test_exact_unclassified_string_cannot_bypass_fold_diagnostics(self):
        """S1b 의 fold 진단을 정확한 sentinel 문자열로 우회할 수 있었다(실측)."""
        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
            slm.record_snapshot_call("unclassified")
            slm.record_snapshot_send("unclassified")
        snap = self._snapshot()
        send = snap["snapshot_send"]
        self.assertEqual(send["calls_by_channel"]["unclassified"], 1)
        self.assertEqual(send["sends_by_outcome"]["unclassified"], 1)
        self.assertEqual(snap["metrics_internal_errors_total"], 2)
        self.assertEqual(sum("unclassified 로 접는다" in r.getMessage() for r in logs.records), 2)


class TestFoldPairingIsAtomic(SubscribeLoadTestCase):
    def test_fold_counts_unclassified_and_internal_error_in_one_lock_section(self):
        """⛔ 나누면 동시 스냅샷이 `unclassified > internal_errors` 를 일시 관측한다(실측:
        race probe 22,700회/12만, 최대 lead 2). fold 1건 = lock 획득 정확히 1회가 계약이다."""
        real = slm._lock

        class _CountingLock:
            def __init__(self):
                self.acquisitions = 0

            def __enter__(self):
                self.acquisitions += 1
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        from unittest.mock import patch
        for label, record in (("channel", lambda: slm.record_snapshot_call("typo")),
                              ("send", lambda: slm.record_snapshot_send("typo"))):
            with self.subTest(record=label):
                self._hard_reset()
                counting = _CountingLock()
                with patch.object(slm, "_lock", counting):
                    with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
                        record()
                self.assertEqual(counting.acquisitions, 1,
                                 "fold 의 counter 와 internal error 가 다른 임계구역에 있다")
                snap = self._snapshot()
                self.assertEqual(snap["metrics_internal_errors_total"], 1)
        self._hard_reset()


class TestFoldNeverCountsWithoutItsDiagnosis(SubscribeLoadTestCase):
    def test_persistent_internal_counter_failure_withholds_the_unclassified_count(self):
        """⛔ 관측을 먼저 쓰면 두 번째(진단) 쓰기 실패가 **진단 없는 unclassified** 를 남긴다
        (실측: unclassified=1 / internal_errors=0 — S1b 진입 Blocker 와 같은 형태). 진단-먼저
        순서면 실패 잔여는 "관측 미기록 + WARNING" 뿐이라 거짓 pairing 이 구조적으로 불가."""
        from unittest.mock import patch

        class _FailInternalWrite(dict):
            def __setitem__(self, key, value):
                if key == "metrics_internal_errors_total":
                    raise RuntimeError("persistent internal-counter failure")
                return super().__setitem__(key, value)

        for label, record in (("channel", lambda: slm.record_snapshot_call("typo")),
                              ("send", lambda: slm.record_snapshot_send("typo"))):
            with self.subTest(record=label):
                self._hard_reset()
                with patch.object(slm, "_metrics", _FailInternalWrite(slm._metrics)):
                    with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                        record()
                    snap = self._snapshot()
                    self.assertEqual(snap["snapshot_send"]["calls_by_channel"]["unclassified"]
                                     + snap["snapshot_send"]["sends_by_outcome"]["unclassified"], 0,
                                     "진단이 못 남았으면 관측도 세지 않는다")
                    self.assertTrue(any("기록 실패" in r.getMessage() for r in logs.records))
        self._hard_reset()

    def test_unclassified_write_failure_records_both_internal_errors(self):
        """진단 쓰기 후 unclassified 쓰기만 실패하면 두 사건을 각각 센다.

        잘못된 입력 진단은 이미 남았고, 그 입력을 unclassified 로 기록하려는
        관측 자체도 실패했다. 후자를 더 세지 않으면 기록 실패가 조용히 사라진다.
        """
        class _FailUnclassifiedWrite(dict):
            def __setitem__(self, key, value):
                if key == "unclassified":
                    raise RuntimeError("unclassified-write-failed")
                return super().__setitem__(key, value)

        cases = (
            ("calls_by_channel", lambda: slm.record_snapshot_call("typo")),
            ("sends_by_outcome", lambda: slm.record_snapshot_send("typo")),
        )
        for path, record in cases:
            with self.subTest(path=path):
                self._hard_reset()
                with slm._lock:
                    original = slm._metrics["snapshot_send"][path]
                    slm._metrics["snapshot_send"][path] = _FailUnclassifiedWrite(original)
                with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                    record()
                snap = self._snapshot()
                self.assertEqual(snap["metrics_internal_errors_total"], 2)
                self.assertEqual(snap["snapshot_send"][path]["unclassified"], 0)
                self.assertTrue(any("기록 실패" in r.getMessage() for r in logs.records))
        self._hard_reset()


class TestFoldDiagnosticsNeverEvaluateTheValue(SubscribeLoadTestCase):
    def test_a_repr_bomb_value_still_folds_safely(self):
        """⛔ 진단 경로에 값을 실으면(`f"...{value!r}"`) **`__repr__` 자체가 던질 수 있고**,
        그 f-string 평가는 no-throw try 밖이라 서비스 경로로 전파된다. 값을 싣지 않는 게 계약."""
        class _ReprBomb:
            def __repr__(self):
                raise RuntimeError("repr-bomb")

        with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
            slm.record_snapshot_call(_ReprBomb())
            slm.record_snapshot_send(_ReprBomb())
        snap = self._snapshot()
        send = snap["snapshot_send"]
        self.assertEqual(send["calls_by_channel"]["unclassified"], 1)
        self.assertEqual(send["sends_by_outcome"]["unclassified"], 1)
        self.assertEqual(snap["metrics_internal_errors_total"], 2)
        self.assertEqual(sum("unclassified 로 접는다" in r.getMessage() for r in logs.records), 2)


class TestResetContract(SubscribeLoadTestCase):
    def test_reset_is_refused_while_a_caller_is_awaiting(self):
        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                with self.assertRaises(slm.SubscribeLoadContractError):
                    slm.reset_subscribe_load_metrics()
                handle.finish("granted")
        _run(scenario())

    def test_reset_is_refused_while_a_worker_is_in_flight(self):
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        started, release = threading.Event(), threading.Event()

        def target():
            started.set()
            release.wait(5)
        worker = threading.Thread(
            target=slm.timed_call, args=(slm.SNAPSHOT_BUILD, handle, target), daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(slm.SubscribeLoadContractError):
                slm.reset_subscribe_load_metrics()
        finally:
            release.set()
            worker.join(5)
        slm.reset_subscribe_load_metrics()

    def test_reset_reseeds_fixed_keys_instead_of_clearing(self):
        _run(self._one())
        slm.reset_subscribe_load_metrics()
        snap = self._snapshot()
        for axis, outcomes in slm.AXIS_OUTCOMES.items():
            self.assertEqual(sorted(snap[axis]["by_outcome"]), sorted(outcomes))
            self.assertEqual(snap[axis]["started_total"], 0)
            self.assertEqual(snap[axis]["callers_awaiting_max"], 0)
        self.assertEqual(snap["metrics_internal_errors_total"], 0)

    async def _one(self):
        async with slm.observe(slm.PREMIUM_RC) as handle:
            handle.finish("granted")


class TestEntryExceptionAtomicity(SubscribeLoadTestCase):
    """⛔ 한 lock 은 **동시성** 원자성만 준다. 두 번째 쓰기만 실패하면
    `started=1 / awaiting=0 / terminal 0` 부분 전이가 남았고(실측), 게이지만 보는 reset 이
    그 손상을 정상으로 수용·삭제했다(실측). 진입은 단일 대입 commit point 로 전무-아니면-전부다."""

    def test_mid_entry_write_failure_cannot_leave_a_partial_record(self):
        class _FailAwaitingWrite(dict):
            def __setitem__(self, key, value):
                if key == "callers_awaiting":
                    raise RuntimeError("injected partial entry failure")
                return super().__setitem__(key, value)

        with slm._lock:
            slm._metrics[slm.PREMIUM_RC] = _FailAwaitingWrite(slm._metrics[slm.PREMIUM_RC])
        ran = {"body": 0}

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                ran["body"] += 1
                handle.finish("granted")
                return "ok"
        self.assertEqual(_run(scenario()), "ok")
        self.assertEqual(ran["body"], 1)
        snap = self._snapshot()
        block = snap[slm.PREMIUM_RC]
        # ⛔ 복사본에 계산하므로 살아있는 block 에 대한 쓰기 주입은 발화하지 않아야 한다 —
        #    in-place 로 되돌리면 여기서 `started=1 / granted=0` 부분 전이가 잡힌다.
        self.assertEqual(block["started_total"], 1)
        self.assertEqual(block["by_outcome"]["granted"], 1)
        self.assertEqual(block["callers_awaiting"], 0)
        self._invariant(snap)
        self._hard_reset()

    def test_commit_point_failure_leaves_nothing_and_reset_still_works(self):
        from unittest.mock import patch

        class _FailAxisSwap(dict):
            def __setitem__(self, key, value):
                if key == slm.PREMIUM_RC:
                    raise RuntimeError("commit-point-failed")
                return super().__setitem__(key, value)

        ran = {"body": 0}

        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as handle:
                ran["body"] += 1
                handle.finish("granted")
                return "ok"
        with patch.object(slm, "_metrics", _FailAxisSwap(slm._metrics)):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING") as logs:
                self.assertEqual(_run(scenario()), "ok")
            self.assertEqual(ran["body"], 1)
            snap = self._snapshot()
            self.assertEqual(snap[slm.PREMIUM_RC]["started_total"], 0, "전무가 아니라 부분이 남았다")
            self._invariant(snap)
            self.assertTrue(any("진입 부기 실패" in r.getMessage() for r in logs.records))
            # 장부가 일관되므로 공개 reset 은 **수용**해야 한다 (손상 거부와의 경계)
            slm.reset_subscribe_load_metrics()
        self._hard_reset()

    def test_reset_refuses_a_corrupted_ledger_instead_of_erasing_evidence(self):
        """⛔ 게이지 검사만으로는 `started=3 / awaiting=0 / terminal 0` 손상을 정상으로
        수용·삭제했다(실측). 손상 장부는 증거다."""
        with slm._lock:
            slm._metrics[slm.SNAPSHOT_BUILD]["started_total"] = 3
        try:
            with self.assertRaises(slm.SubscribeLoadContractError) as ctx:
                slm.reset_subscribe_load_metrics()
            self.assertIn("불변식", str(ctx.exception))
            self.assertIn("snapshot_build", str(ctx.exception))
        finally:
            self._hard_reset()


class TestEntryTransitionStructure(SubscribeLoadTestCase):
    """⛔ 진입 전이의 원자성은 **결정적으로 관측할 수 없다**(경합 창을 재현해야 한다).
    그래서 구조로 잠근다 — `observe` 의 진입 `with _lock` **하나 안**에 세 갱신이 모두 있어야
    한다. 나누면 그 사이 snapshot 이 `started == sum(by_outcome) + callers_awaiting` 를 깬다."""

    def test_entry_and_exit_each_use_exactly_one_lock_block(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(slm))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "observe")

        def is_lock_with(node):
            return (isinstance(node, ast.With) and len(node.items) == 1
                    and isinstance(node.items[0].context_expr, ast.Name)
                    and node.items[0].context_expr.id == "_lock")

        # ⛔ 소스 순서로 모은다 — `ast.walk` 는 순서를 보장하지 않는다.
        locks = sorted((n for n in ast.walk(fn) if is_lock_with(n)), key=lambda n: n.lineno)
        # 진입 1 + 진입 실패 시 best-effort internal error 1 + 종료 1 = 3
        self.assertEqual(len(locks), 3,
                         "`_lock` 임계구역 수가 계약과 다르다 — nullcontext 치환이나 분할을 의심한다")
        entry, fallback, exit_block = (ast.dump(n) for n in locks)
        for field in ("started_total", "callers_awaiting", "callers_awaiting_max"):
            self.assertIn(field, entry, f"{field} 갱신이 진입 `_lock` 임계구역 밖이다")
        self.assertIn("metrics_internal_errors_total", fallback,
                      "진입 실패 fallback 이 internal error 를 세지 않는다")
        for field in ("by_outcome", "duration_ms_sum", "duration_ms_max", "callers_awaiting",
                      "metrics_internal_errors_total"):
            self.assertIn(field, exit_block, f"{field} 갱신이 종료 `_lock` 임계구역 밖이다")


class TestSnapshotAtomicity(SubscribeLoadTestCase):
    def test_invariant_holds_while_callers_are_in_flight(self):
        """진입 전이가 원자적이지 않으면 여기서 찢어진다."""
        async def scenario():
            async with slm.observe(slm.PREMIUM_RC) as outer:
                snap = self._snapshot()
                self.assertEqual(snap[slm.PREMIUM_RC]["callers_awaiting"], 1)
                self._invariant(snap)
                async with slm.observe(slm.PREMIUM_RC) as inner:
                    snap = self._snapshot()
                    self.assertEqual(snap[slm.PREMIUM_RC]["callers_awaiting"], 2)
                    self.assertEqual(snap[slm.PREMIUM_RC]["callers_awaiting_max"], 2)
                    self._invariant(snap)
                    inner.finish("granted")
                outer.finish("denied")
        _run(scenario())
        self._invariant(self._snapshot())

    def test_worker_in_flight_is_derived_while_a_worker_is_actually_running(self):
        """⛔ 빈 스냅샷에서는 0 == 0-0 이라 어떤 상수로 바꿔도 통과한다 — **물려 있는 동안**
        재야 파생값임이 잠긴다. ⚠️ 단 caller 축과의 단순 차이는 잔여가 아니다 — `callers_awaiting == 0` 인 정지 캡처에서만
        그렇게 읽을 수 있다."""
        handle = slm.WorkHandle(slm.SNAPSHOT_BUILD)
        started, release = threading.Event(), threading.Event()

        def target():
            started.set()
            release.wait(5)
        worker = threading.Thread(
            target=slm.timed_call, args=(slm.SNAPSHOT_BUILD, handle, target), daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            snap = self._snapshot()[slm.SNAPSHOT_BUILD]
            self.assertEqual(snap["worker_started_total"], 1)
            self.assertEqual(snap["worker_finished_total"], 0)
            self.assertEqual(snap["worker_in_flight"], 1)
            self.assertEqual(
                snap["worker_in_flight"],
                snap["worker_started_total"] - snap["worker_finished_total"],
            )
            self.assertEqual(snap["worker_in_flight_max"], 1)
        finally:
            release.set()
            worker.join(5)
        after = self._snapshot()[slm.SNAPSHOT_BUILD]
        self.assertEqual(after["worker_in_flight"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
