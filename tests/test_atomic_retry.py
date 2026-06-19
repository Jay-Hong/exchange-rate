"""P1b B3 — retry/backoff + R1 budget + partial_pending policy 단위 테스트 (§19:348-352, pure dormant).

FIRST: classify_attempt exhaustive lane table(모든 PublishOutcome + 모든 non-PUBLISH DecisionAction이
정확히 하나의 lane). + double-publish 불변(WATERMARK_WRITE_FAILED/DIVERGENT/LINEAGE/GATE/DISABLED ≠ SEND_RETRY)
+ R1 accounting split + backoff(capped-exp/no-jitter) + R1 budget(SOFT only) + decide_retry(전 verdict) +
partial_pending(NOT_PENDING_CURRENT 포함) + dataclass invariants + dormancy(skip-list + positive AST self-arming).
"""
from __future__ import annotations

import ast
import math
import pathlib
import unittest

from app import atomic_retry as ar
from app.atomic_retry import (
    AttemptClassification,
    BackoffParams,
    PartialVerdict,
    R1AccountingKind,
    R1Budget,
    R1Status,
    RetryDecision,
    RetryLane,
    RetryState,
    RetryVerdict,
    WaitTrigger,
    advance_retry_state,
    backoff_delay,
    classify_attempt,
    classify_partial_repair,
    decide_retry,
    is_decidable,
    next_fire_at,
    r1_elapsed,
    r1_remaining,
    r1_status,
)
from app.atomic_build import BuildCompleteness, BuildResult
from app.atomic_coordinator import PublishOutcome, PublishResult
from app.atomic_reconcile import B2bDecision, DecisionAction, PendingReason
from app.fx_membership import FX_MEMBERSHIP_SOURCES, FX_MEMBERSHIP_VERSION


def _outcome_result(outcome):
    return PublishResult(asset="usd-krw", outcome=outcome)


def _decision_result(action):
    return PublishResult(asset="usd-krw",
                         decision=B2bDecision(action=action, watermark_candidate=None, reason="x"))


_CLASSIFY_LANES = {RetryLane.SEND_RETRY, RetryLane.WATERMARK_ONLY, RetryLane.EVENT_DRIVEN_WAIT,
                   RetryLane.TERMINAL_NONE, RetryLane.BLOCKED}


class TestClassifyAttemptExhaustive(unittest.TestCase):
    """codex 1순위: 모든 PublishOutcome + 모든 non-PUBLISH DecisionAction → 정확히 하나의 lane."""

    def test_all_outcomes_classified(self):
        for oc in PublishOutcome:
            c = classify_attempt(_outcome_result(oc))
            self.assertIsInstance(c, AttemptClassification, oc)
            self.assertIn(c.lane, _CLASSIFY_LANES, oc)   # BACKSTOP은 classify 산출 X

    def test_all_nonpublish_decisions_classified(self):
        for act in DecisionAction:
            if act is DecisionAction.PUBLISH:
                continue
            c = classify_attempt(_decision_result(act))
            self.assertIsInstance(c, AttemptClassification, act)
            self.assertIn(c.lane, _CLASSIFY_LANES, act)

    def test_decision_publish_raises(self):
        # PUBLISH은 result.decision에 안 실림(send로 진행) → fail-closed
        with self.assertRaises(ValueError):
            classify_attempt(_decision_result(DecisionAction.PUBLISH))

    def test_classify_never_emits_backstop(self):
        # BACKSTOP = C6 trigger 라벨 — classify_attempt가 산출하면 안 됨
        for oc in PublishOutcome:
            self.assertNotEqual(classify_attempt(_outcome_result(oc)).lane, RetryLane.BACKSTOP)
        for act in DecisionAction:
            if act is DecisionAction.PUBLISH:
                continue
            self.assertNotEqual(classify_attempt(_decision_result(act)).lane, RetryLane.BACKSTOP)

    def test_xor_violation_raises(self):
        # PublishResult 자체가 XOR 강제하나 classify도 방어
        with self.assertRaises(ValueError):
            PublishResult(asset="usd-krw")  # both None → __post_init__ ValueError


class TestClassifyAttemptMapping(unittest.TestCase):

    def test_watermark_write_failed_is_watermark_only_never_send_retry(self):
        # 핵심 double-publish 불변
        c = classify_attempt(_outcome_result(PublishOutcome.WATERMARK_WRITE_FAILED))
        self.assertEqual(c.lane, RetryLane.WATERMARK_ONLY)
        self.assertNotEqual(c.lane, RetryLane.SEND_RETRY)
        self.assertEqual(c.accounting, R1AccountingKind.WATERMARK_LAG_NOT_COUNTED)
        self.assertTrue(c.retriable)

    def test_send_failures_are_send_retry(self):
        for oc in (PublishOutcome.ALL_SEND_FAILED, PublishOutcome.SEND_EXCEPTION):
            c = classify_attempt(_outcome_result(oc))
            self.assertEqual(c.lane, RetryLane.SEND_RETRY, oc)
            self.assertEqual(c.accounting, R1AccountingKind.COUNTS, oc)

    def test_decision_retry_is_send_retry(self):
        c = classify_attempt(_decision_result(DecisionAction.RETRY))
        self.assertEqual(c.lane, RetryLane.SEND_RETRY)
        self.assertEqual(c.accounting, R1AccountingKind.COUNTS)

    def test_blocked_outcomes_never_send_retry(self):
        # codex lock-point 3: 이것들이 send retry로 가면 안 됨
        for oc in (PublishOutcome.WATERMARK_DIVERGENT_ALERT, PublishOutcome.WATERMARK_LINEAGE_MISMATCH):
            c = classify_attempt(_outcome_result(oc))
            self.assertEqual(c.lane, RetryLane.BLOCKED, oc)
            self.assertFalse(c.retriable, oc)
        self.assertEqual(classify_attempt(_decision_result(DecisionAction.BLOCK)).lane, RetryLane.BLOCKED)

    def test_event_wait_outcomes_with_trigger(self):
        cases = {
            PublishOutcome.NO_SUBSCRIBERS: WaitTrigger.SUBSCRIBER_APPEARANCE,
            PublishOutcome.DISABLED: WaitTrigger.FF_RE_ENABLE,
            PublishOutcome.GATE_WOULD_BLOCK: WaitTrigger.GATE_OPEN,
        }
        for oc, trig in cases.items():
            c = classify_attempt(_outcome_result(oc))
            self.assertEqual(c.lane, RetryLane.EVENT_DRIVEN_WAIT, oc)
            self.assertEqual(c.wait_trigger, trig, oc)
            self.assertNotEqual(c.lane, RetryLane.SEND_RETRY, oc)
        # pre-send subscriber-zero도 같은 lane(다른 reason)
        c = classify_attempt(_decision_result(DecisionAction.SKIP_SUBSCRIBER_ZERO))
        self.assertEqual(c.lane, RetryLane.EVENT_DRIVEN_WAIT)
        self.assertEqual(c.wait_trigger, WaitTrigger.SUBSCRIBER_APPEARANCE)
        # pre-send(SKIP_SUBSCRIBER_ZERO) vs post-send(NO_SUBSCRIBERS) reason 구분(codex lock-point 2)
        self.assertNotEqual(c.reason, classify_attempt(_outcome_result(PublishOutcome.NO_SUBSCRIBERS)).reason)

    def test_terminal_outcomes(self):
        for oc in (PublishOutcome.COMMITTED, PublishOutcome.WATERMARK_STALE):
            self.assertEqual(classify_attempt(_outcome_result(oc)).lane, RetryLane.TERMINAL_NONE, oc)
        self.assertEqual(classify_attempt(_decision_result(DecisionAction.SKIP_DEDUP_IDENTICAL)).lane,
                         RetryLane.TERMINAL_NONE)

    def test_no_outcome_or_decision_maps_to_send_retry_wrongly(self):
        # 종합: SEND_RETRY는 ALL_SEND_FAILED/SEND_EXCEPTION/decision RETRY 3개뿐
        send_retry = [oc for oc in PublishOutcome
                      if classify_attempt(_outcome_result(oc)).lane is RetryLane.SEND_RETRY]
        self.assertEqual(set(send_retry), {PublishOutcome.ALL_SEND_FAILED, PublishOutcome.SEND_EXCEPTION})


class TestBackoff(unittest.TestCase):

    def test_schedule(self):
        self.assertEqual([backoff_delay(i) for i in range(6)], [0.5, 1.0, 2.0, 4.0, 4.0, 4.0])

    def test_custom_params(self):
        p = BackoffParams(base_seconds=1.0, cap_seconds=8.0)
        self.assertEqual([backoff_delay(i, p) for i in range(5)], [1.0, 2.0, 4.0, 8.0, 8.0])

    def test_huge_attempt_no_overflow(self):
        self.assertEqual(backoff_delay(100000), 4.0)

    def test_negative_attempt_raises(self):
        with self.assertRaises(ValueError):
            backoff_delay(-1)

    def test_bool_attempt_raises(self):
        with self.assertRaises(ValueError):
            backoff_delay(True)

    def test_bad_params_raise(self):
        with self.assertRaises(ValueError):
            BackoffParams(base_seconds=0)
        with self.assertRaises(ValueError):
            BackoffParams(base_seconds=5.0, cap_seconds=1.0)

    def test_non_finite_params_raise(self):
        # inf base/cap → backoff inf → 영구 WAIT(retry-until-success 무력화) 차단. NaN도.
        for kw in ({"base_seconds": math.inf}, {"cap_seconds": math.inf},
                   {"base_seconds": math.nan}, {"cap_seconds": math.nan}):
            with self.assertRaises(ValueError):
                BackoffParams(**kw)

    def test_next_fire_at(self):
        self.assertEqual(next_fire_at(100.0, 0), 100.5)
        self.assertEqual(next_fire_at(100.0, 3), 104.0)


class TestR1Budget(unittest.TestCase):

    def _b(self):
        return R1Budget(asset="usd-krw", started_at=1000.0)

    def test_elapsed_remaining(self):
        b = self._b()
        self.assertEqual(r1_elapsed(b, 1005.0), 5.0)
        self.assertEqual(r1_remaining(b, 1005.0), 10.0)

    def test_on_track(self):
        self.assertEqual(r1_status(self._b(), 1010.0), R1Status.ON_TRACK)

    def test_soft_breach_over_15s(self):
        self.assertEqual(r1_status(self._b(), 1016.0), R1Status.SOFT_BREACH)
        self.assertLess(r1_remaining(self._b(), 1016.0), 0)   # 음수 = 초과

    def test_exactly_at_target_is_on_track(self):
        # > target일 때만 breach (== 은 on track)
        self.assertEqual(r1_status(self._b(), 1015.0), R1Status.ON_TRACK)

    def test_no_abort_status_exists(self):
        # SOFT only — R1Status에 ABORT/HARD 없음(§12.1)
        self.assertEqual({m.name for m in R1Status}, {"ON_TRACK", "SOFT_BREACH"})

    def test_bad_budget_raises(self):
        with self.assertRaises(ValueError):
            R1Budget(asset="usd-krw", started_at=1000.0, soft_target_seconds=0)
        with self.assertRaises(ValueError):
            R1Budget(asset="usd-krw", started_at=1000.0, soft_target_seconds=-5.0)
        with self.assertRaises(ValueError):
            R1Budget(asset="usd-krw", started_at=math.inf)
        with self.assertRaises(ValueError):
            R1Budget(asset="usd-krw", started_at=1000.0, soft_target_seconds=math.inf)


class TestRetryState(unittest.TestCase):

    def test_advance(self):
        s = RetryState(asset="usd-krw")
        self.assertEqual(s.attempt_index, 0)
        s2 = advance_retry_state(s, now=500.0, lane=RetryLane.SEND_RETRY)
        self.assertEqual(s2.attempt_index, 1)
        self.assertEqual(s2.last_fire_at, 500.0)
        self.assertEqual(s2.lane, RetryLane.SEND_RETRY)
        # 원본 불변(frozen)
        self.assertEqual(s.attempt_index, 0)

    def test_negative_attempt_raises(self):
        with self.assertRaises(ValueError):
            RetryState(asset="usd-krw", attempt_index=-1)


class TestDecideRetry(unittest.TestCase):

    def _budget(self, now_anchor=1000.0):
        return R1Budget(asset="usd-krw", started_at=now_anchor)

    def _send_retry_class(self):
        return classify_attempt(_outcome_result(PublishOutcome.ALL_SEND_FAILED))

    def _wm_only_class(self):
        return classify_attempt(_outcome_result(PublishOutcome.WATERMARK_WRITE_FAILED))

    def test_send_retry_first_waits_backoff(self):
        # last_fire_at None → anchor=now, fire_at=now+0.5 → WAIT
        st = RetryState(asset="usd-krw", attempt_index=0, last_fire_at=None)
        d = decide_retry(self._send_retry_class(), st, self._budget(), now=1000.0, subscriber_present=True)
        self.assertEqual(d.verdict, RetryVerdict.WAIT)
        self.assertEqual(d.wait_until, 1000.5)

    def test_send_retry_eligible_after_backoff(self):
        st = RetryState(asset="usd-krw", attempt_index=0, last_fire_at=1000.0)
        # fire_at = 1000.0 + 0.5 = 1000.5; now=1001 >= → ELIGIBLE_NOW
        d = decide_retry(self._send_retry_class(), st, self._budget(), now=1001.0, subscriber_present=True)
        self.assertEqual(d.verdict, RetryVerdict.ELIGIBLE_NOW)
        self.assertIsNone(d.wait_until)

    def test_send_retry_subscriber_gone_suspends(self):
        st = RetryState(asset="usd-krw", attempt_index=0, last_fire_at=1000.0)
        d = decide_retry(self._send_retry_class(), st, self._budget(), now=1001.0, subscriber_present=False)
        self.assertEqual(d.verdict, RetryVerdict.SUSPEND_EVENT_DRIVEN)

    def test_watermark_only_subscriber_independent(self):
        # WATERMARK_ONLY는 subscriber_present=False여도 backoff 기반(send 이미 발생)
        st = RetryState(asset="usd-krw", attempt_index=0, last_fire_at=1000.0)
        d = decide_retry(self._wm_only_class(), st, self._budget(), now=1001.0, subscriber_present=False)
        self.assertEqual(d.verdict, RetryVerdict.ELIGIBLE_NOW)

    def test_event_driven_wait_suspends(self):
        c = classify_attempt(_outcome_result(PublishOutcome.NO_SUBSCRIBERS))
        st = RetryState(asset="usd-krw")
        d = decide_retry(c, st, self._budget(), now=1001.0, subscriber_present=False)
        self.assertEqual(d.verdict, RetryVerdict.SUSPEND_EVENT_DRIVEN)
        self.assertIn("subscriber_appearance", d.reason)

    def test_over_soft_budget_flag_but_keeps_retrying(self):
        # now anchor +16s → SOFT_BREACH지만 verdict은 여전히 retry(abort 아님)
        st = RetryState(asset="usd-krw", attempt_index=0, last_fire_at=1000.0)
        d = decide_retry(self._send_retry_class(), st, self._budget(now_anchor=1000.0),
                         now=1016.0, subscriber_present=True)
        self.assertTrue(d.over_soft_budget)
        self.assertEqual(d.verdict, RetryVerdict.ELIGIBLE_NOW)   # 계속 retry

    def test_terminal_lane_raises(self):
        c = classify_attempt(_outcome_result(PublishOutcome.COMMITTED))
        with self.assertRaises(ValueError):
            decide_retry(c, RetryState(asset="usd-krw"), self._budget(), now=1.0, subscriber_present=True)

    def test_blocked_lane_raises(self):
        c = classify_attempt(_outcome_result(PublishOutcome.WATERMARK_DIVERGENT_ALERT))
        with self.assertRaises(ValueError):
            decide_retry(c, RetryState(asset="usd-krw"), self._budget(), now=1.0, subscriber_present=True)

    def test_backstop_lane_raises(self):
        # BACKSTOP = C6 trigger 라벨 — decide_retry 입력 아님(미래 caller 방어, classify 미산출이라 hand-construct)
        c = AttemptClassification(RetryLane.BACKSTOP, R1AccountingKind.OUT_OF_SCOPE, False, "backstop")
        with self.assertRaises(ValueError):
            decide_retry(c, RetryState(asset="usd-krw"), self._budget(), now=1.0, subscriber_present=True)

    def test_wait_plus_over_soft_budget(self):
        # backoff 미경과(WAIT) + 15s 초과(SOFT_BREACH) 동시 — WAIT verdict 유지 + over flag True
        st = RetryState(asset="usd-krw", attempt_index=0, last_fire_at=1100.0)
        d = decide_retry(self._send_retry_class(), st, self._budget(now_anchor=1000.0),
                         now=1100.2, subscriber_present=True)  # fire_at=1100.5 > now=1100.2 → WAIT; elapsed 100.2 > 15
        self.assertEqual(d.verdict, RetryVerdict.WAIT)
        self.assertEqual(d.wait_until, 1100.5)
        self.assertTrue(d.over_soft_budget)

    def test_is_decidable(self):
        # decidable = SEND_RETRY/WATERMARK_ONLY/EVENT_DRIVEN_WAIT (retriable로 gate 금지 — EVENT_DRIVEN_WAIT 누락 함정)
        self.assertTrue(is_decidable(self._send_retry_class()))
        self.assertTrue(is_decidable(self._wm_only_class()))
        ev = classify_attempt(_outcome_result(PublishOutcome.NO_SUBSCRIBERS))
        self.assertTrue(is_decidable(ev))
        self.assertFalse(ev.retriable)   # retriable=False지만 decidable=True (함정 증명)
        self.assertFalse(is_decidable(classify_attempt(_outcome_result(PublishOutcome.COMMITTED))))
        self.assertFalse(is_decidable(classify_attempt(_outcome_result(PublishOutcome.WATERMARK_DIVERGENT_ALERT))))
        self.assertFalse(is_decidable(AttemptClassification(RetryLane.BACKSTOP, R1AccountingKind.OUT_OF_SCOPE,
                                                            False, "backstop")))

    def test_event_driven_wait_decidable_does_not_raise(self):
        # is_decidable True인 EVENT_DRIVEN_WAIT은 decide_retry가 raise 안 함(retriable-gate였다면 누락됐을 케이스)
        ev = classify_attempt(_outcome_result(PublishOutcome.DISABLED))
        d = decide_retry(ev, RetryState(asset="usd-krw"), self._budget(), now=1.0, subscriber_present=True)
        self.assertEqual(d.verdict, RetryVerdict.SUSPEND_EVENT_DRIVEN)


class TestClassifyPartialRepair(unittest.TestCase):

    def _full_pending(self, **overrides):
        # derive_pending 계약 = FX_MEMBERSHIP_SOURCES 전체. 기본 NOT_PENDING_DB_ABSENT + 특정 source override.
        pending = {s: PendingReason.NOT_PENDING_DB_ABSENT for s in FX_MEMBERSHIP_SOURCES}
        for src, reason in overrides.items():
            assert src in FX_MEMBERSHIP_SOURCES, src
            pending[src] = reason
        return pending

    def _build(self, *, missing):
        present = tuple(sorted(FX_MEMBERSHIP_SOURCES - set(missing)))
        # payload는 partial 분류에 안 쓰이나 BuildResult invariant 충족 위해 최소 구성
        banks = [{"source": s, "asset": "usd-krw", "rate": 1300.0, "timestamp": "2026-06-19T09:00:00+09:00"}
                 for s in present if s != "investing"]
        data = {"banks": banks}
        if "investing" in present:
            data["reference"] = {"source": "investing", "asset": "usd-krw", "rate": 1301.0,
                                 "timestamp": "2026-06-19T09:00:00+09:00"}
        comp = BuildCompleteness.COMPLETE if not missing else BuildCompleteness.PARTIAL
        return BuildResult(asset="usd-krw", payload={"type": "snapshot", "version": 1, "data": data},
                           present_sources=present, missing_sources=tuple(sorted(missing)),
                           membership_version=FX_MEMBERSHIP_VERSION, completeness=comp, build_error=None)

    def test_db_ahead_missing_is_repair(self):
        out = classify_partial_repair(self._full_pending(kb=PendingReason.PENDING_DB_AHEAD),
                                      self._build(missing={"kb"}))
        self.assertEqual(out["kb"], PartialVerdict.AVAILABILITY_REPAIR)

    def test_not_in_present_missing_is_repair(self):
        out = classify_partial_repair(self._full_pending(kb=PendingReason.PENDING_NOT_IN_PRESENT),
                                      self._build(missing={"kb"}))
        self.assertEqual(out["kb"], PartialVerdict.AVAILABILITY_REPAIR)

    def test_not_pending_current_missing_is_repair(self):
        # codex item-5 핵심: NOT_PENDING_CURRENT여도 build가 잃으면 coverage regression → repair
        out = classify_partial_repair(self._full_pending(kb=PendingReason.NOT_PENDING_CURRENT),
                                      self._build(missing={"kb"}))
        self.assertEqual(out["kb"], PartialVerdict.AVAILABILITY_REPAIR)

    def test_db_absent_is_no_retry(self):
        out = classify_partial_repair(self._full_pending(kb=PendingReason.NOT_PENDING_DB_ABSENT),
                                      self._build(missing={"kb"}))
        self.assertEqual(out["kb"], PartialVerdict.NO_RETRY)

    def test_structural_is_alert(self):
        out = classify_partial_repair(self._full_pending(kb=PendingReason.STRUCTURAL),
                                      self._build(missing={"kb"}))
        self.assertEqual(out["kb"], PartialVerdict.STRUCTURAL_ALERT)

    def test_db_value_in_build_is_no_retry(self):
        # DB has value(NOT_PENDING_CURRENT)이나 build에 포함됨(missing 아님) → NO_RETRY
        out = classify_partial_repair(self._full_pending(kb=PendingReason.NOT_PENDING_CURRENT),
                                      self._build(missing=set()))
        self.assertEqual(out["kb"], PartialVerdict.NO_RETRY)

    def test_structural_alert_regardless_of_missing(self):
        # STRUCTURAL은 build 포함 여부 무관 alert(watermark corruption)
        out = classify_partial_repair(self._full_pending(kb=PendingReason.STRUCTURAL), self._build(missing=set()))
        self.assertEqual(out["kb"], PartialVerdict.STRUCTURAL_ALERT)

    def test_bad_keyset_raises(self):
        # pending keys != FX_MEMBERSHIP_SOURCES → 호출 버그(sibling decide_and_build_next 일관 fail-closed)
        with self.assertRaises(ValueError):
            classify_partial_repair({"kb": PendingReason.NOT_PENDING_DB_ABSENT}, self._build(missing=set()))
        extra = self._full_pending()
        extra["ghost"] = PendingReason.NOT_PENDING_DB_ABSENT
        with self.assertRaises(ValueError):
            classify_partial_repair(extra, self._build(missing=set()))

    def test_full_membership_classified(self):
        out = classify_partial_repair(self._full_pending(), self._build(missing=set()))
        self.assertEqual(set(out), set(FX_MEMBERSHIP_SOURCES))


class TestDataclassInvariants(unittest.TestCase):

    def test_attempt_classification_wait_trigger_iff_event_wait(self):
        with self.assertRaises(ValueError):   # event-wait인데 trigger 없음
            AttemptClassification(RetryLane.EVENT_DRIVEN_WAIT, R1AccountingKind.OUT_OF_SCOPE, False, "x")
        with self.assertRaises(ValueError):   # non-event인데 trigger 있음
            AttemptClassification(RetryLane.SEND_RETRY, R1AccountingKind.COUNTS, True, "x",
                                  wait_trigger=WaitTrigger.GATE_OPEN)

    def test_attempt_classification_retriable_invariant(self):
        with self.assertRaises(ValueError):   # SEND_RETRY인데 retriable False
            AttemptClassification(RetryLane.SEND_RETRY, R1AccountingKind.COUNTS, False, "x")
        with self.assertRaises(ValueError):   # TERMINAL_NONE인데 retriable True
            AttemptClassification(RetryLane.TERMINAL_NONE, R1AccountingKind.OUT_OF_SCOPE, True, "x")

    def test_retry_decision_wait_until_iff_wait(self):
        with self.assertRaises(ValueError):
            RetryDecision(RetryVerdict.WAIT, None, "x", False)        # WAIT인데 wait_until None
        with self.assertRaises(ValueError):
            RetryDecision(RetryVerdict.ELIGIBLE_NOW, 5.0, "x", False)  # non-WAIT인데 wait_until 있음
        # 정상
        RetryDecision(RetryVerdict.WAIT, 5.0, "x", False)
        RetryDecision(RetryVerdict.ELIGIBLE_NOW, None, "x", True)

    def test_accounting_lane_coupling_enforced(self):
        # COUNTS⟺SEND_RETRY / WATERMARK_LAG⟺WATERMARK_ONLY / 그 외 OUT_OF_SCOPE (테이블 편집 회귀 방지)
        with self.assertRaises(ValueError):  # SEND_RETRY인데 OUT_OF_SCOPE
            AttemptClassification(RetryLane.SEND_RETRY, R1AccountingKind.OUT_OF_SCOPE, True, "x")
        with self.assertRaises(ValueError):  # WATERMARK_ONLY인데 COUNTS
            AttemptClassification(RetryLane.WATERMARK_ONLY, R1AccountingKind.COUNTS, True, "x")
        with self.assertRaises(ValueError):  # TERMINAL_NONE인데 COUNTS
            AttemptClassification(RetryLane.TERMINAL_NONE, R1AccountingKind.COUNTS, False, "x")

    def test_classification_tables_coupling_holds(self):
        # 양 분류 테이블이 coupling 만족(이미 __post_init__가 import 시 강제하나 명시 회귀)
        for c in list(ar._OUTCOME_CLASSIFICATION.values()) + list(ar._DECISION_CLASSIFICATION.values()):
            if c.lane is RetryLane.SEND_RETRY:
                self.assertEqual(c.accounting, R1AccountingKind.COUNTS)
            elif c.lane is RetryLane.WATERMARK_ONLY:
                self.assertEqual(c.accounting, R1AccountingKind.WATERMARK_LAG_NOT_COUNTED)
            else:
                self.assertEqual(c.accounting, R1AccountingKind.OUT_OF_SCOPE)


# ────────────────────────── dormancy ──────────────────────────

_LIVE_IO_FORBIDDEN = frozenset({
    "cache", "latest_rates_cache", "topic_dispatcher", "fx_topic_publisher",
    "fx_topic_payload", "crud", "database", "redis", "asyncio", "importlib",
})
_LIVE_CALL_NEEDLES = frozenset({
    "publish_topic", "safe_publish_fx_snapshot", "_publish_fx_snapshot", "send_json",
    "sleep", "create_task",
})


def _is_type_checking_if(node):
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return ((isinstance(t, ast.Name) and t.id == "TYPE_CHECKING")
            or (isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"))


def _walk_skip_type_checking(node):
    for child in ast.iter_child_nodes(node):
        if _is_type_checking_if(child):
            # body(type-check 전용)만 skip — else: 절은 런타임 실행이라 검사 대상
            for sub in child.orelse:
                yield sub
                yield from _walk_skip_type_checking(sub)
            continue
        yield child
        yield from _walk_skip_type_checking(child)


def _scan_retry_purity(src):
    """atomic_retry 직접 live-I/O/asyncio import + sleep/create_task/publish/redis call 위반 목록 (pure)."""
    violations = []
    for node in _walk_skip_type_checking(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            tail = (node.module or "").split(".")[-1]
            if tail in _LIVE_IO_FORBIDDEN:
                violations.append(f"from {node.module} import")
            if node.module == "app":
                for a in node.names:
                    if a.name in _LIVE_IO_FORBIDDEN:
                        violations.append(f"from app import {a.name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] in _LIVE_IO_FORBIDDEN:
                    violations.append(f"import {a.name}")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _LIVE_CALL_NEEDLES:
                violations.append(f"{name}()")
    return violations


def _scan_module_usage(src, module_name):
    violations = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            if node.module and module_name in node.module.split("."):
                violations.append(f"from {node.module} import")
            if node.module == "app" and any(a.name == module_name for a in node.names):
                violations.append(f"from app import {module_name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if module_name in a.name.split("."):
                    violations.append(f"import {a.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if module_name in node.value:
                violations.append(f"str {node.value!r}")
    return violations


_DORMANT_MODULES = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py", "atomic_write_outcome.py",
    "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py", "atomic_reconcile.py",
    "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py", "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py", "atomic_write_durable.py",
})


class TestDormancy(unittest.TestCase):
    """B3 dormant — app/ 어떤 live 모듈도 atomic_retry import 0."""

    def test_no_live_module_uses_atomic_retry(self):
        app_dir = pathlib.Path(ar.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _DORMANT_MODULES:
                continue
            rel = py.relative_to(app_dir)
            viol = _scan_module_usage(py.read_text(encoding="utf-8"), "atomic_retry")
            self.assertEqual(viol, [], f"{rel}: atomic_retry 사용 — dormant 위반 ({viol})")

    def test_negative_scanner_self_arms(self):
        for planted in ("from app.atomic_retry import classify_attempt\n",
                        "from app import atomic_retry\n", "import app.atomic_retry\n"):
            self.assertTrue(_scan_module_usage(planted, "atomic_retry"), planted)


class TestPositiveDormancyAst(unittest.TestCase):
    """B3는 pure policy — 직접 asyncio/sleep/create_task/live-I/O import·call 0(positive trip-wire)."""

    def test_atomic_retry_is_pure(self):
        src = pathlib.Path(ar.__file__).read_text(encoding="utf-8")
        self.assertEqual(_scan_retry_purity(src), [],
                         "atomic_retry.py에 직접 asyncio/sleep/live-I/O import·call — dormant 위반")

    def test_positive_scanner_self_arms(self):
        for planted in (
            "import asyncio\n",
            "from app.cache import x\n",
            "from app import latest_rates_cache\n",
            "import time\nasync def f():\n    await asyncio.sleep(1)\n",
            "def f(loop):\n    loop.create_task(g())\n",
            "def f():\n    publish_topic('t', {})\n",
            "import importlib\n",  # importlib.import_module 우회 차단
            # TYPE_CHECKING else: 절의 런타임 import은 잡혀야(orelse blind spot)
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    import asyncio\n",
        ):
            self.assertTrue(_scan_retry_purity(planted), f"planted 미검출: {planted!r}")

    def test_positive_scanner_allows_type_checking_body(self):
        # TYPE_CHECKING **body**의 import은 허용(런타임 미실행)
        clean = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import asyncio\n"
        self.assertEqual(_scan_retry_purity(clean), [])


if __name__ == "__main__":
    unittest.main()
