"""RC canary 의 계약 잠금 — premium-only 전제 · KRX 배제 · 격리 import 표면 · 시간 박스.

⛔ 이 canary 는 외부 quota(RevenueCat)를 실호출하는 도구다. 여기서 잠그는 것은 "실행해도
   되는가" 가 아니라 **"실행하면 계약대로만 움직이는가"** 다. 네트워크는 전부 주입으로 대체한다.
"""
import argparse
import ast
import asyncio
import importlib.util
import json
import logging
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "scripts" / "rc_canary.py"

_spec = importlib.util.spec_from_file_location("rc_canary_under_test", MODULE_PATH)
canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary)


def _run(coro):
    return asyncio.run(coro)


def _fake_authorize(results):
    """호출마다 순서대로 결과(또는 예외/코루틴 지연)를 돌려주는 주입용 authorize."""
    queue = list(results)

    async def authorize(plan, *, mono):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return await item()
        return item

    return authorize


class TestPlanContract(unittest.TestCase):
    def test_plan_is_premium_only_without_krx(self):
        plan = canary.build_premium_only_plan("canary-uid")
        self.assertTrue(plan.requires_premium())
        self.assertFalse(plan.requires_entitlement())
        self.assertEqual(plan.premium_and_entitlement, ())
        self.assertEqual(plan.premium_only, (canary.CANARY_TOPIC,))

    def test_canary_topic_is_not_the_gated_topic(self):
        from app.krx_topic_publisher import KRX_TOPIC

        self.assertNotEqual(canary.CANARY_TOPIC, KRX_TOPIC)

    def test_wrong_plan_shape_is_refused_even_if_the_planner_drifts(self):
        """⛔ 전제 단언은 planner 가 정상일 때 무하중이다 — drift 를 주입해 하중을 만든다."""
        from unittest.mock import patch

        from app.topic_policy import AuthorizationPlan

        identity_only = AuthorizationPlan(
            uid="u", identity_only=(canary.CANARY_TOPIC,),
            premium_only=(), premium_and_entitlement=(),
        )
        with patch("app.topic_policy.plan_authenticated", return_value=identity_only):
            with self.assertRaises(canary.CanaryContractError):
                canary.build_premium_only_plan("u")   # RC 0회가 되는 공허한 canary

        # ⛔ premium_only 는 **정확히 맞고** gated 만 추가된 plan — 앞의 exact-match
        #    검사에 가려지지 않아 KRX 배제 분기만이 이 반례를 잡는다(변이 생존 실측).
        gated = AuthorizationPlan(
            uid="u", identity_only=(), premium_only=(canary.CANARY_TOPIC,),
            premium_and_entitlement=("krx:usd-krw-futures",),
        )
        with patch("app.topic_policy.plan_authenticated", return_value=gated):
            with self.assertRaises(canary.CanaryContractError):
                canary.build_premium_only_plan("u")   # entitlement DB read 혼입

        # ⛔ RC 조회는 plan.uid 로 나간다 — 다른 UID plan 은 다른 사용자를 조회하고도
        #    provenance 를 요청 UID 로 남기는 오류가 된다 (검토 실측 반례).
        wrong_uid = AuthorizationPlan(
            uid="production-user", identity_only=(),
            premium_only=(canary.CANARY_TOPIC,), premium_and_entitlement=(),
        )
        with patch("app.topic_policy.plan_authenticated", return_value=wrong_uid):
            with self.assertRaises(canary.CanaryContractError):
                canary.build_premium_only_plan("canary-user")

        wrong_topic = AuthorizationPlan(
            uid="u", identity_only=(),
            premium_only=("usdt:krw",), premium_and_entitlement=(),
        )
        with patch("app.topic_policy.plan_authenticated", return_value=wrong_topic):
            with self.assertRaises(canary.CanaryContractError):
                canary.build_premium_only_plan("u")

    def test_missing_rc_config_refuses_before_any_call(self):
        with self.assertRaises(canary.CanaryContractError):
            canary.require_rc_config(getenv=lambda *a, **k: "")
        canary.require_rc_config(getenv=lambda *a, **k: "present")


class TestClassification(unittest.TestCase):
    def _make_outcome(self, premium):
        from app.topic_authorization import AuthorizationOutcome

        plan = canary.build_premium_only_plan("canary-uid")
        return AuthorizationOutcome(plan=plan, premium=premium, entitlement=None)

    def test_verdict_types_map_to_stable_labels(self):
        from app.topic_authorization import Denied, PremiumGranted, Unavailable, UnavailableKind

        granted = self._make_outcome(PremiumGranted(uid="canary-uid", premium_observed_at_mono=1.0))
        denied = self._make_outcome(Denied("premium_required"))
        self.assertEqual(canary.classify(granted), "premium_granted")
        self.assertEqual(canary.classify(denied), "denied_premium_required")
        self.assertEqual(
            canary.classify(Unavailable(kind=UnavailableKind.TRANSIENT, reason="x")),
            "unavailable_transient",
        )
        self.assertEqual(canary.classify(object()), "unexpected_object")

    def test_provider_logging_is_disabled_before_any_real_call(self):
        """provider logger는 원래 raw UID·전송 URL을 남긴다. canary 프로세스에서는 금지다."""
        logger = logging.getLogger("exchange_rate.subscription")
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture()
        previous_disable = logging.root.manager.disable
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            canary.suppress_runtime_logging()
            logger.error("raw-canary-uid https://provider.invalid/subscribers/raw-canary-uid")
            self.assertEqual(records, [])
        finally:
            logging.disable(previous_disable)
            logger.setLevel(previous_level)
            logger.removeHandler(handler)

    def test_main_wires_log_suppression_before_the_canary_run(self):
        import contextlib
        import io
        import os
        import tempfile
        from unittest.mock import patch

        events = []
        summary = {
            "counts_by_label": {"denied_premium_required": 1},
            "calls": [{"label": "denied_premium_required"}],
            "params": {"iterations_requested": 1},
            "latency_ms": {"count": 0},
            "aborted_reason": None,
        }

        async def fake_run_canary(**kwargs):
            events.append("run")
            return summary

        with tempfile.TemporaryDirectory() as tmp:
            artifact = pathlib.Path(tmp) / "result.json"
            with (
                patch.dict(
                    os.environ,
                    {
                        "CANARY_UID": "canary-uid",
                        "CANARY_EXPECTED_LABEL": "denied_premium_required",
                    },
                ),
                patch.object(canary, "suppress_runtime_logging",
                             side_effect=lambda: events.append("suppress")),
                patch.object(canary, "require_rc_config"),
                patch.object(canary, "require_runner_context"),
                patch.object(canary, "reserve_artifact", return_value=artifact),
                patch.object(canary, "run_canary", new=fake_run_canary),
                patch.object(canary, "finalize_artifact", return_value=artifact),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(canary.main(["--output-dir", tmp, "--iterations", "1"]), 0)
        self.assertEqual(events, ["suppress", "run"])

    def test_expected_label_requires_every_requested_call_to_match(self):
        base = {
            "params": {"iterations_requested": 2},
            "calls": [{"label": "premium_granted"}, {"label": "premium_granted"}],
            "counts_by_label": {"premium_granted": 2},
            "aborted_reason": None,
        }
        self.assertTrue(canary.evaluate_expectation(base, "premium_granted")["passed"])
        self.assertIsNone(
            canary.evaluate_expectation(base, "premium_granted")["latency_threshold"],
            "승인된 latency SLO가 없는데 합격 임계값을 만들어내면 안 된다",
        )

        for mutation in (
            {**base, "counts_by_label": {"premium_granted": 1, "timeout": 1}},
            {**base, "calls": base["calls"][:1], "counts_by_label": {"premium_granted": 1}},
            {**base, "aborted_reason": "max_runtime_exceeded"},
        ):
            with self.subTest(mutation=mutation):
                self.assertFalse(
                    canary.evaluate_expectation(mutation, "premium_granted")["passed"]
                )

    def test_unknown_expected_label_is_refused(self):
        summary = {
            "params": {"iterations_requested": 1},
            "calls": [],
            "counts_by_label": {},
            "aborted_reason": None,
        }
        with self.assertRaises(canary.CanaryContractError):
            canary.evaluate_expectation(summary, "anything_goes")

    def test_missing_expected_label_refuses_before_reservation_or_run(self):
        import contextlib
        import io
        import os
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"CANARY_UID": "canary-uid"}, clear=True),
                patch.object(canary, "suppress_runtime_logging"),
                patch.object(canary, "require_rc_config"),
                patch.object(canary, "require_runner_context"),
                patch.object(canary, "reserve_artifact") as reserve,
                patch.object(canary, "run_canary") as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(canary.main(["--output-dir", tmp]), 2)
        reserve.assert_not_called()
        run.assert_not_called()

    def test_main_preserves_mismatch_artifact_and_returns_nonzero(self):
        import contextlib
        import io
        import os
        import tempfile
        from unittest.mock import patch

        summary = {
            "counts_by_label": {"denied_premium_required": 1},
            "calls": [{"label": "denied_premium_required"}],
            "params": {"iterations_requested": 1},
            "latency_ms": {"count": 1},
            "aborted_reason": None,
        }

        async def fake_run_canary(**kwargs):
            return summary

        with tempfile.TemporaryDirectory() as tmp:
            artifact = pathlib.Path(tmp) / "result.json"
            with (
                patch.dict(
                    os.environ,
                    {
                        "CANARY_UID": "canary-uid",
                        "CANARY_EXPECTED_LABEL": "premium_granted",
                    },
                    clear=True,
                ),
                patch.object(canary, "suppress_runtime_logging"),
                patch.object(canary, "require_rc_config"),
                patch.object(canary, "require_runner_context"),
                patch.object(canary, "reserve_artifact", return_value=artifact),
                patch.object(canary, "run_canary", new=fake_run_canary),
                patch.object(canary, "finalize_artifact", return_value=artifact) as finalize,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    canary.main(["--output-dir", tmp, "--iterations", "1"]), 3
                )
        published = finalize.call_args.args[1]
        self.assertFalse(published["expectation"]["passed"])
        self.assertEqual(published["expectation"]["expected_label"], "premium_granted")


class TestArtifactStateAxis(unittest.TestCase):
    """⛔ 세 상태를 **같은 필드**로 읽을 수 있어야 한다 — 성공본에만 status 가 없으면
    완료 판정이 '필드 부재' 추론이 되고 consumer 가 KeyError 를 맞는다."""

    def test_success_failure_and_reservation_share_one_state_axis(self):
        import tempfile

        failure = canary.build_failure_summary(
            uid="u", expected_label="premium_granted", started_utc="20260814T000000Z",
            args=argparse.Namespace(iterations=3, cadence_seconds=1.0,
                                    call_timeout_seconds=1.0, max_runtime_seconds=10.0),
            exc=RuntimeError("boom"),
        )
        self.assertEqual(failure["status"], "failed")
        self.assertIs(failure["measurement_complete"], False)
        self.assertIsNone(failure["observations_recorded"])

        with tempfile.TemporaryDirectory() as tmp:
            reserved = canary.reserve_artifact(tmp, "20260814T000001Z")
            self.assertEqual(json.loads(reserved.read_text())["status"], "in_progress")

    def test_completed_summary_declares_its_state_and_call_count(self):
        from app.topic_authorization import Denied

        summary = _run(canary.run_canary(
            uid="canary-uid", iterations=2, cadence_seconds=1.0, call_timeout_seconds=0.5,
            max_runtime_seconds=100.0,
            authorize=_fake_authorize([Denied("premium_required")] * 2),
            sleeper=lambda seconds: asyncio.sleep(0),
        ))
        self.assertEqual(summary["status"], "completed")
        self.assertIs(summary["measurement_complete"], True)
        self.assertEqual(summary["observations_recorded"], len(summary["calls"]))

    def test_finalize_refuses_payloads_that_break_the_state_axis(self):
        """⛔ fixture 두 곳이 status 없는 payload 를 썼다 — 합의가 아니라 코드가 막아야
        어떤 호출자도 `/4` 의 필수 상태 축을 빠뜨릴 수 없다."""
        import tempfile

        for bad in ({"schema": canary.ARTIFACT_SCHEMA},
                    {"schema": canary.ARTIFACT_SCHEMA, "status": "in_progress"},
                    {"schema": "rc-canary/3", "status": "completed"}):
            with self.subTest(payload=bad), tempfile.TemporaryDirectory() as tmp:
                reserved = canary.reserve_artifact(tmp, "20260101T000000Z")
                with self.assertRaises(canary.CanaryContractError):
                    canary.finalize_artifact(reserved, bad)
                self.assertIn("in_progress", reserved.read_text())  # 예약본 보존
                self.assertFalse((reserved.parent / f"{reserved.name}.sha256").exists())

    def test_every_shape_declares_schema_four(self):
        """⛔ `/3` 성공본에는 status 가 없었다 — 새 필드를 직접 인덱싱 계약으로 올리려면
        버전이 달라야 한다. 같은 버전에 두 모양이 섞이면 부재 추론이 남는다."""
        import tempfile
        from app.topic_authorization import Denied

        summary = _run(canary.run_canary(
            uid="u", iterations=1, cadence_seconds=1.0, call_timeout_seconds=0.5,
            max_runtime_seconds=100.0, authorize=_fake_authorize([Denied("premium_required")]),
            sleeper=lambda seconds: asyncio.sleep(0),
        ))
        failure = canary.build_failure_summary(
            uid="u", expected_label="premium_granted", started_utc="20260814T000000Z",
            args=argparse.Namespace(iterations=1, cadence_seconds=1.0,
                                    call_timeout_seconds=1.0, max_runtime_seconds=10.0),
            exc=RuntimeError("boom"))
        with tempfile.TemporaryDirectory() as tmp:
            stub = json.loads(canary.reserve_artifact(tmp, "20260814T000002Z").read_text())
        for shape in (summary, failure, stub):
            self.assertEqual(shape["schema"], "rc-canary/4")

    def test_incomplete_run_is_not_declared_complete(self):
        """⛔ runtime abort 는 루프를 break 하고 **정상 return** 한다 — 그래도 완료로 적으면
        요청보다 적게 돈 측정이 완주로 인용된다."""
        from app.topic_authorization import Denied

        ticks = iter([0.0, 0.0, 0.1, 0.2, 999.0, 999.1, 999.2, 999.3, 999.4])
        summary = _run(canary.run_canary(
            uid="u", iterations=3, cadence_seconds=1.0, call_timeout_seconds=0.5,
            max_runtime_seconds=10.0,
            authorize=_fake_authorize([Denied("premium_required")] * 3),
            sleeper=lambda seconds: asyncio.sleep(0),
            mono=lambda: next(ticks),
        ))
        self.assertIsNotNone(summary["aborted_reason"])
        self.assertLess(len(summary["calls"]), 3)
        self.assertIs(summary["measurement_complete"], False)

    def test_observations_count_timeouts_not_only_provider_completions(self):
        """⛔ `calls` 는 try/except **뒤에서 무조건** append 된다 — timeout 도 한 건이다.
        따라서 이 수치를 'provider 완료 호출 수' 로 부르면 거짓이다."""
        summary = _run(canary.run_canary(
            uid="u", iterations=2, cadence_seconds=1.0, call_timeout_seconds=0.5,
            max_runtime_seconds=100.0,
            authorize=_fake_authorize([asyncio.TimeoutError(), asyncio.TimeoutError()]),
            sleeper=lambda seconds: asyncio.sleep(0),
        ))
        self.assertEqual(summary["counts_by_label"], {"timeout": 2})
        self.assertEqual(summary["observations_recorded"], 2)


class TestRunLoop(unittest.TestCase):
    def _summary(self, results, **overrides):
        from app.topic_authorization import Denied

        kwargs = dict(
            uid="canary-uid",
            iterations=len(results),
            cadence_seconds=1.0,
            call_timeout_seconds=0.5,
            max_runtime_seconds=100.0,
            authorize=_fake_authorize(results),
            sleeper=lambda seconds: asyncio.sleep(0),
        )
        kwargs.update(overrides)
        return _run(canary.run_canary(**kwargs))

    def _denied(self):
        from app.topic_authorization import Denied

        outcome_denied = Denied("premium_required")
        from app.topic_authorization import AuthorizationOutcome

        plan = canary.build_premium_only_plan("canary-uid")
        return AuthorizationOutcome(plan=plan, premium=outcome_denied, entitlement=None)

    def test_labels_latency_and_counts_are_recorded(self):
        summary = self._summary([self._denied(), RuntimeError("secret http://user:pw@host")])
        self.assertEqual(len(summary["calls"]), 2)
        self.assertEqual(summary["counts_by_label"]["denied_premium_required"], 1)
        self.assertEqual(summary["counts_by_label"]["error_RuntimeError"], 1)
        self.assertEqual(summary["latency_ms"]["count"], 2)
        # ⛔ 예외 **문자열**은 산출물 어디에도 없어야 한다 — 타입 이름만.
        self.assertNotIn("secret", json.dumps(summary))
        self.assertNotIn("http://", json.dumps(summary))

    def test_hung_call_is_classified_as_timeout(self):
        async def hang():
            await asyncio.sleep(30)

        summary = self._summary([hang])
        self.assertEqual(summary["counts_by_label"], {"timeout": 1})

    def test_runtime_cap_aborts_before_further_calls(self):
        ticks = iter([0.0, 0.0, 0.1, 0.2, 999.0, 999.1, 999.2, 999.3, 999.4])
        summary = self._summary(
            [self._denied()] * 3,
            iterations=3,
            max_runtime_seconds=10.0,
            mono=lambda: next(ticks),
        )
        self.assertEqual(summary["aborted_reason"], "max_runtime_exceeded")
        self.assertLess(len(summary["calls"]), 3)

    def test_iteration_and_cadence_bounds_are_hard(self):
        with self.assertRaises(canary.CanaryContractError):
            self._summary([], iterations=canary.MAX_ITERATIONS + 1)
        with self.assertRaises(canary.CanaryContractError):
            self._summary([self._denied()], cadence_seconds=0.01)
        with self.assertRaises(canary.CanaryContractError):
            self._summary(
                [self._denied()], max_runtime_seconds=canary.MAX_RUNTIME_SECONDS_CAP + 1
            )

    def test_non_finite_time_parameters_are_refused(self):
        """⛔ NaN 은 모든 비교식을 조용히 통과한다(실측 — NaN cadence 가 그대로 기록됐다)."""
        for kwargs in (
            {"cadence_seconds": float("nan")},
            {"call_timeout_seconds": float("inf")},
            {"call_timeout_seconds": canary.MAX_CALL_TIMEOUT_SECONDS + 1},
            {"max_runtime_seconds": float("nan")},
        ):
            with self.subTest(**kwargs), self.assertRaises(canary.CanaryContractError):
                self._summary([self._denied()], **kwargs)

    def test_runtime_cap_is_a_real_deadline_for_the_inflight_call(self):
        """⛔ 반복 시작 전 검사만으로는 상한이 아니다 — 실측: cap 0.01s 인데 호출이
        121ms 돌고 aborted_reason=None 이었다. 남은 deadline 이 per-call timeout 을
        줄여야 상한을 넘겨 실행되는 호출이 구조적으로 없다."""

        async def hang():
            await asyncio.sleep(30)

        # mono: run_start=0 → 첫 호출 전 remaining=0.2 → effective_timeout=min(20, 0.2)
        #       (wait_for 는 실시간이므로 틱을 좁혀 실대기를 0.2s 로 묶는다)
        ticks = iter([0.0, 9.8, 9.8, 9.9, 9.95, 9.96, 9.97, 9.98])
        import time as _time

        real_start = _time.monotonic()
        summary = self._summary(
            [hang, self._denied()],
            iterations=2,
            call_timeout_seconds=20.0,
            max_runtime_seconds=10.0,
            mono=lambda: next(ticks),
        )
        # ⛔ fake mono 는 wall-clock 차이를 못 본다 — deadline 이 timeout 을 줄이지
        #    않으면 wait_for 가 실시간 20초를 기다린다(변이 생존 실측). 여유 2초.
        self.assertLess(_time.monotonic() - real_start, 2.0,
                        "deadline 이 per-call timeout 을 줄이지 않았다")
        self.assertEqual(summary["calls"][0]["label"], "timeout")
        self.assertEqual(summary["aborted_reason"], "max_runtime_exceeded")
        self.assertEqual(len(summary["calls"]), 1)

    def test_percentiles_are_median_and_nearest_rank(self):
        """실측 반례: 구 구현은 [10,100] 의 p50 을 100 으로 냈다."""
        summary = self._summary([self._denied()] * 2)
        lat = sorted(c["latency_ms"] for c in summary["calls"])
        import statistics

        self.assertEqual(summary["latency_ms"]["p50_median"], statistics.median(lat))
        self.assertEqual(summary["latency_ms"]["p95_nearest_rank"], lat[-1])
        self.assertEqual(canary._nearest_rank([10.0, 100.0], 50), 10.0)
        self.assertEqual(canary._nearest_rank([10.0, 100.0], 95), 100.0)

    def test_uid_is_fingerprinted_not_raw(self):
        summary = self._summary([self._denied()])
        blob = json.dumps(summary)
        self.assertNotIn("canary-uid", blob)
        import hashlib

        self.assertEqual(
            summary["uid_fingerprint"],
            hashlib.sha256(b"canary-uid").hexdigest()[:16],
        )
        self.assertEqual(len(summary["program_sha256"]), 64)

    def test_cancel_swallowing_callee_is_self_reported_as_overrun(self):
        """⛔ wait_for 는 취소를 삼키는 callee 를 강제 종료하지 못한다(실측 1.37s > cap 1s).
        초과는 산출물이 자기보고해야 한다 — 강제 kill 은 runner 의 process timeout 몫."""

        async def stubborn(plan, *, mono):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.25)   # 취소를 삼키고 더 돈다
                return object()

        summary = _run(
            canary.run_canary(
                uid="canary-uid", iterations=1, cadence_seconds=1.0,
                call_timeout_seconds=60.0, max_runtime_seconds=1.0,
                authorize=stubborn, sleeper=lambda s: asyncio.sleep(0),
            )
        )
        self.assertEqual(summary["aborted_reason"], "cooperative_deadline_overrun")
        self.assertGreater(summary["elapsed_s"], 1.0)

    def test_image_provenance_comes_from_the_runner_env(self):
        import os
        from unittest.mock import patch

        runner_env = {
            "CANARY_IMAGE_ID": "sha256:pinned",
            "CANARY_RUNNER_SOURCE_HEAD": "a" * 40,
            "CANARY_RUNNER_SHA256": "b" * 64,
            "CANARY_OUTER_TIMEOUT_SECONDS": "660",
        }
        with patch.dict(os.environ, runner_env):
            summary = self._summary([self._denied()])
        self.assertEqual(summary["image_id"], "sha256:pinned")
        self.assertEqual(summary["runner_source_head"], "a" * 40)
        self.assertEqual(summary["runner_sha256"], "b" * 64)
        self.assertEqual(summary["outer_timeout_seconds"], "660")
        summary = self._summary([self._denied()])
        self.assertEqual(summary["image_id"], "unknown")

    def test_main_execution_context_requires_runner_provenance(self):
        from unittest.mock import patch

        complete = {
            "CANARY_IMAGE_ID": "sha256:" + "a" * 64,
            "CANARY_RUNNER_SOURCE_HEAD": "b" * 40,
            "CANARY_RUNNER_SHA256": "c" * 64,
            "CANARY_OUTER_TIMEOUT_SECONDS": "660",
        }
        canary.require_runner_context(600, getenv=complete.get)
        for missing in complete:
            broken = dict(complete)
            broken.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(canary.CanaryContractError):
                canary.require_runner_context(600, getenv=broken.get)
        too_short = dict(complete, CANARY_OUTER_TIMEOUT_SECONDS="600")
        with self.assertRaises(canary.CanaryContractError):
            canary.require_runner_context(600, getenv=too_short.get)

    def test_scope_disclaimer_travels_with_the_data(self):
        summary = self._summary([self._denied()])
        self.assertEqual(summary["measures_only"], "revenuecat_round_trip")
        self.assertIn("reconnect_storm", summary["not_measured"])
        self.assertEqual(summary["params"]["concurrency"], 1)


class TestArtifactLifecycle(unittest.TestCase):
    def test_reserve_before_any_call_and_finalize_atomically(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            reserved = canary.reserve_artifact(tmp, "20260101T000000Z")
            self.assertTrue(reserved.name.startswith("20260101T000000Z-"))
            self.assertTrue(reserved.name.endswith(".rc-canary.json"))
            self.assertEqual(reserved.stat().st_mode & 0o777, 0o600)
            self.assertIn("in_progress", reserved.read_text())

            final = canary.finalize_artifact(
                reserved, {"schema": canary.ARTIFACT_SCHEMA, "status": "completed", "x": 1}
            )
            self.assertEqual(final, reserved)
            self.assertNotIn("in_progress", reserved.read_text())
            sidecar = reserved.parent / f"{reserved.name}.sha256"
            digest, name = sidecar.read_text().split()
            self.assertEqual(name, reserved.name)  # 상대경로 — 이식 가능
            import hashlib

            self.assertEqual(digest, hashlib.sha256(reserved.read_bytes()).hexdigest())

    def test_reservation_nonce_is_not_the_container_pid(self):
        """⛔ one-shot 컨테이너는 PID 가 사실상 상수다(8개 병렬 중 7개가 PID 7 — 실측).
        같은 초에 시작한 두 예약이 서로 충돌하지 않아야 한다."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            first = canary.reserve_artifact(tmp, "20260101T000000Z")
            second = canary.reserve_artifact(tmp, "20260101T000000Z")
            self.assertNotEqual(first.name, second.name)
            self.assertNotIn(f"-{os.getpid()}.", first.name)

    def test_reservation_collision_refuses_before_rc_calls(self):
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(canary.secrets, "token_hex", return_value="feedbeef"):
                canary.reserve_artifact(tmp, "20260101T000000Z")
                with self.assertRaises(canary.CanaryContractError):
                    canary.reserve_artifact(tmp, "20260101T000000Z")

    def test_unwritable_output_refuses_before_rc_calls(self):
        """⛔ 예약이 없으면 quota 를 다 쓴 뒤에야 쓰기 실패를 발견한다(검토 실측 —
        docker 가 root:root 로 만든 /out 에 appuser 가 못 썼다)."""
        import os
        import tempfile

        if os.geteuid() == 0:
            self.skipTest("root 는 쓰기 거부를 재현할 수 없다")
        with tempfile.TemporaryDirectory() as tmp:
            # ⛔ 대상 디렉터리 자체를 0500 으로 두면 reserve 의 chmod(0o700) 이 되돌려
            #    시뮬레이션이 무효가 된다(실측). **부모**를 잠가 mkdir 를 죽인다 —
            #    운영의 root:root 자동 생성 디렉터리와 같은 실패 지점.
            parent = pathlib.Path(tmp) / "locked"
            parent.mkdir()
            parent.chmod(0o500)
            try:
                with self.assertRaises(canary.CanaryContractError):
                    canary.reserve_artifact(str(parent / "out"), "20260101T000000Z")
            finally:
                parent.chmod(0o700)

    def test_preexisting_sidecar_is_never_overwritten_and_artifact_survives(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            reserved = canary.reserve_artifact(tmp, "20260101T000000Z")
            sidecar = reserved.parent / f"{reserved.name}.sha256"
            sidecar.write_text("DO NOT OVERWRITE\n")
            with self.assertRaises(canary.CanaryContractError):
                canary.finalize_artifact(
                    reserved, {"schema": canary.ARTIFACT_SCHEMA, "status": "completed"}
                )
            self.assertEqual(sidecar.read_text(), "DO NOT OVERWRITE\n")
            # raw-first — artifact(증거)는 남는다
            self.assertIn(canary.ARTIFACT_SCHEMA, reserved.read_text())

    def test_unexpected_failure_after_reservation_is_finalized_without_secrets(self):
        """실측 회귀: import 실패가 영구 `in_progress` 파일과 sidecar 부재를 남겼다."""
        import contextlib
        import hashlib
        import io
        import os
        import tempfile
        from unittest.mock import patch

        raw_uid = "canary-uid-must-not-leak"
        secret_message = f"No module for {raw_uid} at https://provider.invalid/{raw_uid}"

        async def fail_after_reservation(**kwargs):
            raise ModuleNotFoundError(secret_message)

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "CANARY_UID": raw_uid,
                        "CANARY_EXPECTED_LABEL": "denied_premium_required",
                    },
                    clear=True,
                ),
                patch.object(canary, "suppress_runtime_logging"),
                patch.object(canary, "require_rc_config"),
                patch.object(canary, "require_runner_context"),
                patch.object(canary, "run_canary", new=fail_after_reservation),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    canary.main(["--output-dir", tmp, "--iterations", "1"]), 1
                )

            artifacts = list(pathlib.Path(tmp).glob("*.rc-canary.json"))
            self.assertEqual(len(artifacts), 1)
            artifact = artifacts[0]
            body = artifact.read_text()
            summary = json.loads(body)
            self.assertEqual(summary["status"], "failed")
            self.assertFalse(summary["measurement_complete"])
            self.assertIsNone(summary["observations_recorded"])
            self.assertEqual(summary["failure"]["exception_type"], "ModuleNotFoundError")
            self.assertFalse(summary["failure"]["exception_message_recorded"])
            self.assertNotIn("in_progress", body)
            self.assertNotIn(raw_uid, body)
            self.assertNotIn("provider.invalid", body)
            self.assertNotIn(raw_uid, stdout.getvalue())
            self.assertNotIn("provider.invalid", stdout.getvalue())

            sidecar = artifact.parent / f"{artifact.name}.sha256"
            digest, name = sidecar.read_text().split()
            self.assertEqual(name, artifact.name)
            self.assertEqual(digest, hashlib.sha256(artifact.read_bytes()).hexdigest())

    def test_contract_failure_after_reservation_is_finalized_and_returns_two(self):
        import contextlib
        import io
        import os
        import tempfile
        from unittest.mock import patch

        async def refuse_after_reservation(**kwargs):
            raise canary.CanaryContractError("sensitive contract detail")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    os.environ,
                    {
                        "CANARY_UID": "canary-uid",
                        "CANARY_EXPECTED_LABEL": "premium_granted",
                    },
                    clear=True,
                ),
                patch.object(canary, "suppress_runtime_logging"),
                patch.object(canary, "require_rc_config"),
                patch.object(canary, "require_runner_context"),
                patch.object(canary, "run_canary", new=refuse_after_reservation),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    canary.main(["--output-dir", tmp, "--iterations", "1"]), 2
                )
            artifact = next(pathlib.Path(tmp).glob("*.rc-canary.json"))
            summary = json.loads(artifact.read_text())
            self.assertEqual(summary["status"], "refused")
            self.assertEqual(summary["failure"]["exception_type"], "CanaryContractError")
            self.assertNotIn("sensitive contract detail", artifact.read_text())


class TestIsolationSurface(unittest.TestCase):
    """⛔ canary 가 dispatcher·rollout·wire 를 만질 수 있는 import 자체를 잠근다."""

    FORBIDDEN_MODULES = (
        "app.topic_dispatcher",
        "app.topic_auth_rollout",
        "app.topic_wire",
        "app.topic_initial_snapshot",
        "app.topic_lease",
        "app.main",
        "firebase_admin",
        "websockets",
    )

    def test_import_surface_never_reaches_the_dispatcher_or_wire(self):
        """AST 로 **모든** import(지연 포함)를 모아 금지 모듈 부재를 잠근다.

        ⚠️ raw substring 검사가 아니다 — `not_measured` 문서의 "firebase" 같은 정당한
        언급까지 잡아 공허한 빨강을 만든다(실측). 표면은 import 가 정의한다.
        """
        imported = set()
        for node in ast.walk(ast.parse(MODULE_PATH.read_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertTrue(imported, "import 를 하나도 못 찾았다 — 추출기 자체가 공허")
        for module in imported:
            for forbidden in self.FORBIDDEN_MODULES:
                self.assertFalse(
                    module == forbidden or module.startswith(forbidden + "."),
                    f"금지 모듈 import: {module}",
                )

    def test_app_imports_are_deferred_not_module_level(self):
        tree = ast.parse(MODULE_PATH.read_text())
        for node in tree.body:  # module 최상위만 — 지연 import 는 함수 안에 있다
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
                for name in names:
                    self.assertFalse(
                        (name or "").startswith("app"),
                        f"app import 가 module 최상위에 있다: {name}",
                    )

    def test_cli_never_accepts_raw_uid(self):
        source = MODULE_PATH.read_text()
        self.assertNotIn('add_argument("--uid"', source)
        self.assertIn('os.getenv("CANARY_UID"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
