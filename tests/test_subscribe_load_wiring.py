# tests/test_subscribe_load_wiring.py
"""S2 — seam ①②(topic_authorization) 배선 계약.

설계 §7 S2 체크리스트를 잠근다:
  ① `premium_observed_at_mono` 가 RC 호출 **앞** — 기존
     `test_observations_are_taken_before_their_io_not_after`(500.0)가 이미 잠근다.
  ② RC 호출 요청당 ≤1 — 기존 `TestCoordinatorCallCounts` + 여기서 counter 로 재확인.
  ③ deadline 취소 시 `callers_awaiting` 0 복귀 + **cancel 키는 결정적으로**(observed /
     not_observed 를 따로 구성 — deadline 이면 무조건 observed 가 아니다).
  ④ `_observe_krx_entitlement_sync` 본문 불변(계측 혼입 금지 + 관측시각 쿼리-직전).
  ⑤ 취소를 `raised` 로 삼키면 red.
"""

# 표준 라이브러리
import asyncio
import threading
import unittest
from unittest.mock import MagicMock, patch

# 로컬
from app import subscribe_load_metrics as slm
from app import subscription
from app import topic_authorization as ta


class _Clock:
    def __init__(self, start=1000.0, step=1.0):
        self.t, self.step = start, step

    def __call__(self):
        value, self.t = self.t, self.t + self.step
        return value


class WiringTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._hard_reset()

    def tearDown(self):
        self._hard_reset()

    @staticmethod
    def _hard_reset():
        with slm._lock:
            slm._metrics.clear()
            slm._metrics.update(slm._blank())

    @staticmethod
    def _snap():
        return slm.subscribe_load_metrics()


class TestPremiumSeam(WiringTestCase):
    def _patch_rc(self, result):
        async def rc(user_id, **kw):
            return result
        return patch("app.subscription.fetch_revenuecat_result", new=rc)

    async def test_each_verdict_lands_in_its_own_bucket_without_diagnostics(self):
        """분류 4분기 → 도메인 버킷 4개. 진단 0 — 정상 도메인 결과는 결함이 아니다."""
        cases = (
            (subscription.Determined(is_premium=True), "granted"),
            (subscription.Determined(is_premium=False), "denied"),
            (subscription.ProviderUnavailable(), "unavailable_transient"),
            (subscription.ProtocolViolation(detail="test"), "unavailable_persistent"),
        )
        for result, bucket in cases:
            with self.subTest(bucket=bucket):
                self._hard_reset()
                with self._patch_rc(result):
                    await ta._observe_premium("u1", mono=_Clock())
                snap = self._snap()
                block = snap[slm.PREMIUM_RC]
                self.assertEqual(block["by_outcome"][bucket], 1)
                self.assertEqual(block["started_total"], 1)
                self.assertEqual(block["callers_awaiting"], 0)
                self.assertEqual(snap["metrics_internal_errors_total"], 0)

    async def test_rc_call_count_contract_is_visible_in_the_counter(self):
        """② RC ≤1 — coordinator 전체 흐름에서 premium 축 started_total 이 정확히 1."""
        from app import topic_policy

        plan = topic_policy.AuthorizationPlan(
            uid="u1", identity_only=(), premium_only=(),
            premium_and_entitlement=("krx:usd-krw-futures",))
        with self._patch_rc(subscription.Determined(is_premium=True)), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement", new=lambda db, uid, key: True):
            await ta.authorize_subscription_plan(plan, mono=_Clock())
        snap = self._snap()
        self.assertEqual(snap[slm.PREMIUM_RC]["started_total"], 1)
        self.assertEqual(snap[slm.PREMIUM_RC]["by_outcome"]["granted"], 1)
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["started_total"], 1)
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["by_outcome"]["granted"], 1)
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["worker_started_total"], 1)
        self.assertEqual(snap[slm.KRX_ENTITLEMENT]["worker_finished_total"], 1)

    async def test_observed_at_is_taken_after_cm_entry_not_before(self):
        """G2 — mono() 를 CM 진입 **앞**으로 hoist 하면 진입 부기 비용이 mono~RC 사이에
        끼어 "RC 직전" 계약이 흐려진다. 주입 시계는 observe 진입으로 전진하지 않아 기존
        테스트로는 이 방향이 안 잠겼다(Workflow 실측) — 진입을 시계 전진으로 관측시킨다."""
        import contextlib

        clock = _Clock(start=1000.0)
        real_observe = slm.observe

        @contextlib.asynccontextmanager
        async def observe_spy(axis):
            clock.t += 50.0  # CM 진입을 주입 시계에 보이게 만든다
            async with real_observe(axis) as handle:
                yield handle

        async def rc(user_id, **kw):
            return subscription.Determined(is_premium=True)
        with patch.object(ta.subscribe_load, "observe", new=observe_spy), \
             patch("app.subscription.fetch_revenuecat_result", new=rc):
            verdict = await ta._observe_premium("u1", mono=clock)
        self.assertEqual(verdict.premium_observed_at_mono, 1050.0,
                         "관측시각이 CM 진입 **앞**에 찍혔다 — hoist 변이")

    async def test_classification_type_error_is_derived_raised_not_swallowed(self):
        """미지의 RC 결과 → `TypeError` 전파(wire 불변) + 축은 `raised` 파생."""
        with self._patch_rc(object()):
            with self.assertRaises(TypeError):
                await ta._observe_premium("u1", mono=_Clock())
        block = self._snap()[slm.PREMIUM_RC]
        self.assertEqual(block["by_outcome"]["raised"], 1)
        self.assertEqual(block["callers_awaiting"], 0, "예외 종료도 게이지를 복귀시킨다")

    async def test_persistent_logging_failure_does_not_pollute_the_axis_buckets(self):
        """RC 판정 로그도 CM 밖이다. 로깅 결함이 persistent 사건을 `raised` 로 바꾸면 안 된다."""
        with self._patch_rc(subscription.ProtocolViolation(detail="test")), \
             patch.object(ta.logger, "error",
                          side_effect=RuntimeError("logging infra broken")):
            with self.assertRaises(RuntimeError):
                await ta._observe_premium("u1", mono=_Clock())
        block = self._snap()[slm.PREMIUM_RC]
        self.assertEqual(block["by_outcome"]["unavailable_persistent"], 1)
        self.assertEqual(block["by_outcome"]["raised"], 0,
                         "로깅 결함이 RC 외부축 결함으로 둔갑했다")
        self.assertEqual(block["callers_awaiting"], 0)


class TestKrxSeam(WiringTestCase):
    def _granted(self, uid="u1"):
        return ta.PremiumGranted(uid=uid, premium_observed_at_mono=1.0)

    async def test_db_error_classification_lands_in_the_matching_bucket(self):
        """DB 오류 → verdict kind 와 같은 unavailable 버킷 (분류는 wire 로직이 소유).

        ⛔ **두 except 분기를 각각** 지나가야 한다 — ProgrammingError 는 두 번째
        (`SQLAlchemyError`) 분기만 지나 첫 분기의 finish 누락 변이가 생존했다(실측)."""
        from sqlalchemy import exc as sqlalchemy_exc

        cases = (
            ("transient-branch", sqlalchemy_exc.OperationalError("stmt", None, Exception("x")),
             "entitlement_db_error"),
            # ⛔ InterfaceError 는 TRANSIENT_DB_ERRORS 소속이지만 OperationalError 가 **아니다**
            #    — except 를 OperationalError 로 협소화하는 변이가 OperationalError 케이스만으론
            #    생존했다(Workflow G4 실측). 집합의 비-OperationalError 원소로 잠근다.
            ("transient-branch-interface", sqlalchemy_exc.InterfaceError("stmt", None, Exception("x")),
             "entitlement_db_error"),
            ("permanent-branch", sqlalchemy_exc.ProgrammingError("stmt", None, Exception("x")),
             "entitlement_db_permanent"),
        )
        for label, exc, reason in cases:
            with self.subTest(branch=label):
                self._hard_reset()

                def boom(user_id, *, mono, _exc=exc):
                    raise _exc
                with patch.object(ta, "_observe_krx_entitlement_sync", new=boom):
                    with self.assertLogs("exchange_rate.topic_authorization") as logs:
                        verdict = await ta._observe_krx_entitlement(
                            "u1", premium=self._granted(), mono=_Clock())
                self.assertIsInstance(verdict, ta.Unavailable)
                self.assertEqual(verdict.reason, reason, "예상한 except 분기를 지나지 않았다")
                bucket = ("unavailable_transient"
                          if verdict.kind is ta.UnavailableKind.TRANSIENT
                          else "unavailable_persistent")
                block = self._snap()[slm.KRX_ENTITLEMENT]
                self.assertEqual(block["by_outcome"][bucket], 1)
                self.assertEqual(sum(block["by_outcome"].values()), 1)
                self.assertEqual(self._snap()["metrics_internal_errors_total"], 0)
                # ⛔ 로그가 CM 밖으로 나가며 traceback 이 소실되면 안 된다 — `caught` 명시
                #    전달이 계약(밖에서는 sys.exc_info() 가 빈다).
                record = next(r for r in logs.records if "DB 오류" in r.getMessage())
                self.assertIsNotNone(record.exc_info, "traceback 이 소실됐다")
                self.assertIs(record.exc_info[1], exc)

    async def test_denied_lands_in_denied(self):
        with patch.object(ta, "_observe_krx_entitlement_sync",
                          new=lambda uid, *, mono: ta.EntitlementObservation(
                              allowed=False, observed_at_mono=mono())):
            verdict = await ta._observe_krx_entitlement(
                "u1", premium=self._granted(), mono=_Clock())
        self.assertEqual(verdict, ta.Denied("krx_entitlement_required"))
        self.assertEqual(self._snap()[slm.KRX_ENTITLEMENT]["by_outcome"]["denied"], 1)

    async def test_programming_error_propagates_and_is_derived_raised(self):
        """⑤의 절반 — 일반 예외는 삼키지 않고(wire 불변) `raised` 로 파생 기록."""
        def boom(user_id, *, mono):
            raise AttributeError("schema drift")
        with patch.object(ta, "_observe_krx_entitlement_sync", new=boom):
            with self.assertRaises(AttributeError):
                await ta._observe_krx_entitlement(
                    "u1", premium=self._granted(), mono=_Clock())
        block = self._snap()[slm.KRX_ENTITLEMENT]
        self.assertEqual(block["by_outcome"]["raised"], 1)
        self.assertEqual(block["callers_awaiting"], 0)

    async def test_logging_failure_does_not_pollute_the_axis_buckets(self):
        """로깅을 CM 밖으로 뺀 **이유** 그 자체 — 로깅 인프라 결함이 실제 DB transient 사건을
        krx `raised` 로 둔갑시켜 `unavailable_*` 신호를 지우면 안 된다(적대 probe 실측).
        wire 계약: 로깅 예외는 이전과 동일하게 전파된다."""
        from sqlalchemy import exc as sqlalchemy_exc

        def boom(user_id, *, mono):
            raise sqlalchemy_exc.OperationalError("stmt", None, Exception("db down"))
        with patch.object(ta, "_observe_krx_entitlement_sync", new=boom), \
             patch.object(ta, "_log_unavailable",
                          side_effect=RuntimeError("logging infra broken")):
            with self.assertRaises(RuntimeError):
                await ta._observe_krx_entitlement(
                    "u1", premium=self._granted(), mono=_Clock())
        block = self._snap()[slm.KRX_ENTITLEMENT]
        self.assertEqual(block["by_outcome"]["unavailable_transient"], 1,
                         "실제 사건(unavailable)이 로깅 결함에 지워졌다")
        self.assertEqual(block["by_outcome"]["raised"], 0,
                         "로깅 결함이 외부축 결함으로 둔갑했다")
        self.assertEqual(block["callers_awaiting"], 0)

    async def test_tripwire_failure_is_a_contract_error_not_an_axis_observation(self):
        """G1 — tripwire(assert_single_gated_topic)가 CM **안**으로 들어가면 그 실패가 krx
        `raised` 를 오염시킨다(Workflow 실측: uid-check 만 잠근 테스트로는 이 변이가 생존)."""
        with patch.object(ta, "assert_single_gated_topic",
                          side_effect=RuntimeError("2번째 gated topic")):
            with self.assertRaises(RuntimeError):
                await ta._observe_krx_entitlement(
                    "u1", premium=self._granted(), mono=_Clock())
        block = self._snap()[slm.KRX_ENTITLEMENT]
        self.assertEqual(block["started_total"], 0, "tripwire 실패가 축 관측을 시작시켰다")
        self.assertEqual(block["by_outcome"]["raised"], 0)

    async def test_uid_mismatch_is_a_contract_error_not_an_axis_observation(self):
        """pre-check 는 관측 CM **밖** — 계약 오류가 krx 축 `raised` 를 오염시키면 안 된다."""
        with patch("app.database.SessionLocal",
                   new=MagicMock(side_effect=AssertionError("DB touched"))):
            with self.assertRaises(ValueError):
                await ta._observe_krx_entitlement(
                    "expected", premium=self._granted(uid="other"), mono=_Clock())
        block = self._snap()[slm.KRX_ENTITLEMENT]
        self.assertEqual(block["started_total"], 0, "축 관측이 시작되면 안 된다")
        self.assertEqual(sum(block["by_outcome"].values()), 0)

    async def test_cancel_before_worker_start_is_not_observed_and_releases_the_gauge(self):
        """③ deadline 취소 (worker 미시작) — `cancel_worker_start_not_observed` + 게이지 복귀."""
        release = asyncio.Event()

        async def never_runs(fn, *args, **kwargs):
            await release.wait()  # worker 를 절대 시작하지 않는다
        with patch.object(ta.asyncio, "to_thread", new=never_runs):
            task = asyncio.ensure_future(ta._observe_krx_entitlement(
                "u1", premium=self._granted(), mono=_Clock()))
            await asyncio.sleep(0)  # CM 진입 + await 도달
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        block = self._snap()[slm.KRX_ENTITLEMENT]
        self.assertEqual(block["by_outcome"]["cancel_worker_start_not_observed"], 1)
        self.assertEqual(block["by_outcome"]["cancel_worker_start_observed"], 0)
        self.assertEqual(block["by_outcome"]["raised"], 0, "취소를 raised 로 삼키면 안 된다")
        self.assertEqual(block["callers_awaiting"], 0, "취소 후 게이지가 복귀해야 한다")

    async def test_cancel_after_worker_start_is_observed_and_the_worker_drains(self):
        """③ 취소 시점에 worker 가 **이미 도는** 시나리오 — observed 키 + worker 축 drain."""
        started, release = threading.Event(), threading.Event()

        def blocking_sync(user_id, *, mono):
            started.set()
            release.wait(5)
            return ta.EntitlementObservation(allowed=True, observed_at_mono=mono())
        try:
            with patch.object(ta, "_observe_krx_entitlement_sync", new=blocking_sync):
                task = asyncio.ensure_future(ta._observe_krx_entitlement(
                    "u1", premium=self._granted(), mono=_Clock()))
                # ⛔ worker 시작을 **관측한 뒤** 취소한다 — deadline 이면 무조건 observed 가 아니다.
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        # 버려진 worker 가 실제로 끝날 때까지 drain (to_thread 는 취소를 전파하지 않는다)
        for _ in range(200):
            if self._snap()[slm.KRX_ENTITLEMENT]["worker_finished_total"] == 1:
                break
            await asyncio.sleep(0.01)
        block = self._snap()[slm.KRX_ENTITLEMENT]
        self.assertEqual(block["by_outcome"]["cancel_worker_start_observed"], 1)
        self.assertEqual(block["by_outcome"]["cancel_worker_start_not_observed"], 0)
        self.assertEqual(block["callers_awaiting"], 0)
        self.assertEqual(block["worker_started_total"], 1)
        self.assertEqual(block["worker_finished_total"], 1,
                         "버려진 worker 도 종료를 센다 — caller 축과 어긋나는 게 관측의 목적")


class TestAxisOutcomeMapping(unittest.TestCase):
    def test_unknown_verdict_type_raises_instead_of_polluting_granted(self):
        """G3 — 말미의 fail-fast 를 `return "granted"` 로 격하하면 미지 verdict 가 조용히
        granted 를 오염시킨다. 도달 불가 방어 분기는 직접 단위로만 잠긴다(Workflow 실측)."""
        with self.assertRaises(TypeError):
            ta._axis_outcome(object())


class TestSyncBodyIsUntouched(unittest.TestCase):
    def test_the_sync_observer_contains_no_instrumentation(self):
        """④ `_observe_krx_entitlement_sync` 본문 불변 — 계측 혼입 금지 트립와이어."""
        import ast
        import inspect

        src = inspect.getsource(ta._observe_krx_entitlement_sync)
        for token in ("subscribe_load", "timed_call", "observe(", "record_"):
            self.assertNotIn(token, src, f"sync 관측자 본문에 계측({token})이 혼입됐다")
        # 관측시각(mono)이 쿼리(has_entitlement)보다 **앞**에 찍히는 구조도 잠근다.
        tree = ast.parse(src)
        fn = tree.body[0]
        mono_line = next(
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name) and n.value.func.id == "mono")
        query_line = next(
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "has_entitlement")
        self.assertLess(mono_line, query_line, "관측시각이 쿼리 직전이 아니다")


if __name__ == "__main__":
    unittest.main()
