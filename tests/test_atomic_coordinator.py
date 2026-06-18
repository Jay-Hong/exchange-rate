"""P1b B2b-4a + B2b-4b — coordinator pure helpers + async shell 단위 테스트 (§19 B2b-4, dormant).

4a: write_action_for_relation exhaustive 매핑 + materialize_watermark(content 보존/sentinel 차단).
4b: PublishResult XOR + SendResult invariant + AtomicFxCoordinator orchestration(⓪~⑦ 전 분기) +
seq floor/lineage-scoping + sent_but_uncommitted marker recovery + per-asset lock 직렬화 + same-read
precondition + **positive AST dormancy trip-wire**(coordinator 자체 live import/call 0 — skip-list blind spot).
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest

from app import atomic_coordinator as ac
from app.atomic_coordinator import (
    AtomicFxCoordinator,
    CoordinatorHooks,
    PublishOutcome,
    PublishResult,
    SendDisposition,
    SendResult,
    WriteAction,
    materialize_watermark,
    write_action_for_relation,
)
from app.atomic_build import BuildCompleteness, BuildResult
from app.atomic_cutover import PublisherGateDisposition
from app.atomic_reconcile import (
    CANDIDATE_PLACEHOLDER_LINEAGE,
    CANDIDATE_PLACEHOLDER_SENT_AT,
    CANDIDATE_PLACEHOLDER_SEQ,
    DecisionAction,
)
from app.atomic_value_schema import make_revision_key
from app.atomic_watermark import Watermark, WatermarkRelation
from app.atomic_write_outcome import write_outcome_from_lua
from app.fx_membership import FX_MEMBERSHIP_SOURCES, FX_MEMBERSHIP_VERSION


def _candidate(*, lineage_id=CANDIDATE_PLACEHOLDER_LINEAGE, sent_at=CANDIDATE_PLACEHOLDER_SENT_AT,
               publish_sequence=CANDIDATE_PLACEHOLDER_SEQ):
    present = sorted(FX_MEMBERSHIP_SOURCES)
    vec = {s: make_revision_key(1_700_000_000_000_000 + i, i + 1) for i, s in enumerate(present)}
    return Watermark(
        asset="usd-krw", lineage_id=lineage_id, publish_sequence=publish_sequence,
        membership_version=FX_MEMBERSHIP_VERSION, present_revision_vector=vec, missing_sources=(),
        sent_at=sent_at,
    )


class TestWriteActionForRelation(unittest.TestCase):

    def test_mapping(self):
        self.assertEqual(write_action_for_relation(WatermarkRelation.NO_CURRENT), WriteAction.WRITE)
        self.assertEqual(write_action_for_relation(WatermarkRelation.NEWER), WriteAction.WRITE)
        self.assertEqual(write_action_for_relation(WatermarkRelation.OLDER), WriteAction.SKIP_STALE)
        self.assertEqual(write_action_for_relation(WatermarkRelation.SAME), WriteAction.SKIP_IDEMPOTENT)
        self.assertEqual(write_action_for_relation(WatermarkRelation.SAME_SEQ_CONTENT_DIVERGENT),
                         WriteAction.ALERT_DIVERGENT)
        self.assertEqual(write_action_for_relation(WatermarkRelation.LINEAGE_MISMATCH),
                         WriteAction.DEFER_LINEAGE_ARBITRATION)

    def test_exhaustive(self):
        # WatermarkRelation 전 멤버가 매핑돼야 — 새 relation 추가 시 미매핑 → ValueError로 이 test가 깨짐.
        for rel in WatermarkRelation:
            self.assertIsInstance(write_action_for_relation(rel), WriteAction, rel)

    def test_lineage_mismatch_is_defer_not_write_or_skip(self):
        # lineage mismatch는 write/skip이 아니라 arbitration defer (C6 control-plane 대조)
        action = write_action_for_relation(WatermarkRelation.LINEAGE_MISMATCH)
        self.assertEqual(action, WriteAction.DEFER_LINEAGE_ARBITRATION)
        self.assertNotIn(action, (WriteAction.WRITE, WriteAction.SKIP_STALE, WriteAction.SKIP_IDEMPOTENT))


class TestMaterializeWatermark(unittest.TestCase):

    def test_content_preserved_and_fields_replaced(self):
        cand = _candidate()
        real = materialize_watermark(cand, lineage_id="sess-7:3", publish_sequence=42,
                                     sent_at="2026-06-18T09:00:00+09:00")
        # content 보존
        self.assertEqual(real.present_revision_vector, cand.present_revision_vector)
        self.assertEqual(real.missing_sources, cand.missing_sources)
        self.assertEqual(real.membership_version, cand.membership_version)
        self.assertEqual(real.asset, cand.asset)
        # lineage/seq/sent_at 교체
        self.assertEqual(real.lineage_id, "sess-7:3")
        self.assertEqual(real.publish_sequence, 42)
        self.assertEqual(real.sent_at, "2026-06-18T09:00:00+09:00")

    def test_output_no_sentinel(self):
        real = materialize_watermark(_candidate(), lineage_id="sess-7:3", publish_sequence=42,
                                     sent_at="2026-06-18T09:00:00+09:00")
        self.assertNotEqual(real.lineage_id, CANDIDATE_PLACEHOLDER_LINEAGE)
        self.assertNotEqual(real.sent_at, CANDIDATE_PLACEHOLDER_SENT_AT)

    def test_input_not_placeholder_raises(self):
        # 이미 real lineage인 watermark를 materialize에 넣으면 misuse → ValueError
        real_wm = _candidate(lineage_id="already-real:1", sent_at="2026-06-18T08:00:00+09:00")
        with self.assertRaises(ValueError):
            materialize_watermark(real_wm, lineage_id="sess-7:3", publish_sequence=42,
                                  sent_at="2026-06-18T09:00:00+09:00")

    def test_output_lineage_sentinel_raises(self):
        with self.assertRaises(ValueError):
            materialize_watermark(_candidate(), lineage_id=CANDIDATE_PLACEHOLDER_LINEAGE,
                                  publish_sequence=42, sent_at="2026-06-18T09:00:00+09:00")

    def test_output_sent_at_sentinel_raises(self):
        with self.assertRaises(ValueError):
            materialize_watermark(_candidate(), lineage_id="sess-7:3", publish_sequence=42,
                                  sent_at=CANDIDATE_PLACEHOLDER_SENT_AT)

    def test_real_sequence_zero_accepted(self):
        # LOW14: publish_sequence=0은 valid int(placeholder 값과 동일)이라 value로 검증 불가 — sentinel은
        # lineage/sent_at만. real lineage/sent_at + seq=0 (bootstrap 첫 seq)은 정상 수락돼야.
        real = materialize_watermark(_candidate(), lineage_id="sess-7:3", publish_sequence=0,
                                     sent_at="2026-06-18T09:00:00+09:00")
        self.assertEqual(real.publish_sequence, 0)


class TestOutcomeForWriteAction(unittest.TestCase):
    """HIGH9: send-path SAME/OLDER/DIVERGENT 분기는 floor 때문에 publish_asset로 unreachable이나, outcome
    wiring은 이 공유 pure helper로 exhaustive 검증(STALE/DIVERGENT swap 등 회귀 차단)."""

    def test_write_ok_committed(self):
        self.assertEqual(ac._outcome_for_write_action(WriteAction.WRITE, write_ok=True), PublishOutcome.COMMITTED)

    def test_write_fail_uncommitted(self):
        self.assertEqual(ac._outcome_for_write_action(WriteAction.WRITE, write_ok=False),
                         PublishOutcome.WATERMARK_WRITE_FAILED)

    def test_fixed_mapping(self):
        self.assertEqual(ac._outcome_for_write_action(WriteAction.SKIP_IDEMPOTENT, write_ok=True),
                         PublishOutcome.COMMITTED)
        self.assertEqual(ac._outcome_for_write_action(WriteAction.SKIP_STALE, write_ok=True),
                         PublishOutcome.WATERMARK_STALE)
        self.assertEqual(ac._outcome_for_write_action(WriteAction.ALERT_DIVERGENT, write_ok=True),
                         PublishOutcome.WATERMARK_DIVERGENT_ALERT)
        self.assertEqual(ac._outcome_for_write_action(WriteAction.DEFER_LINEAGE_ARBITRATION, write_ok=True),
                         PublishOutcome.WATERMARK_LINEAGE_MISMATCH)

    def test_exhaustive(self):
        # WriteAction 전 멤버가 매핑돼야 — 새 멤버 추가 시 미매핑 → ValueError로 깨짐.
        for act in WriteAction:
            self.assertIsInstance(ac._outcome_for_write_action(act, write_ok=True), PublishOutcome, act)


class TestDormancy(unittest.TestCase):
    """B2b-4a dormant — app/ 어떤 live 모듈도 atomic_coordinator import/helper 호출 0. pure(async/IO 없음).
    atomic_watermark/atomic_reconcile(island) import → dormant set 멤버."""

    _DORMANT_MODULES = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
        "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
        "atomic_build.py", "atomic_reconcile.py", "atomic_coordinator.py",
    })
    _CALL_NEEDLES = ("materialize_watermark", "write_action_for_relation")

    def test_no_live_module_uses_atomic_coordinator(self):
        app_dir = pathlib.Path(ac.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._DORMANT_MODULES:
                continue
            rel = py.relative_to(app_dir)
            viol = _scan_module_usage(py.read_text(encoding="utf-8"), "atomic_coordinator", self._CALL_NEEDLES)
            self.assertEqual(viol, [], f"{rel}: atomic_coordinator 사용 — dormant 위반 ({viol})")

    def test_negative_scanner_self_arms(self):
        # HIGH8: skip-list detector가 실제 위반(planted source)에 trip하는지 증명.
        for planted in (
            "from app.atomic_coordinator import AtomicFxCoordinator\n",
            "from app import atomic_coordinator\n",
            "import app.atomic_coordinator\n",
            "x = materialize_watermark(c)\n",
        ):
            self.assertTrue(_scan_module_usage(planted, "atomic_coordinator", self._CALL_NEEDLES),
                            f"planted 위반인데 detector 미검출: {planted!r}")


# ────────────────────────── B2b-4b: async coordinator shell test harness ──────────────────────────

_ALL_BANKS = ["kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi"]
_WO_REV = (1_700_000_000_000_000, 1)
_CLOCK = "2026-06-18T09:00:00+09:00"


def _build_result(*, bank_sources=None, reference=True, malformed=False, asset="usd-krw"):
    if malformed:
        return BuildResult(asset=asset, payload=None, present_sources=(), missing_sources=(),
                           membership_version=FX_MEMBERSHIP_VERSION,
                           completeness=BuildCompleteness.MALFORMED, build_error="boom")
    bank_sources = _ALL_BANKS if bank_sources is None else bank_sources
    banks = [{"source": s, "asset": asset, "rate": 1300.0, "timestamp": _CLOCK} for s in bank_sources]
    data = {"banks": banks}
    present = set(bank_sources)
    if reference:
        data["reference"] = {"source": "investing", "asset": asset, "rate": 1301.0, "timestamp": _CLOCK}
        present.add("investing")
    payload = {"type": "snapshot", "version": 1, "data": data}
    missing = tuple(sorted(FX_MEMBERSHIP_SOURCES - present))
    comp = BuildCompleteness.COMPLETE if not missing else BuildCompleteness.PARTIAL
    return BuildResult(asset=asset, payload=payload, present_sources=tuple(sorted(present)),
                       missing_sources=missing, membership_version=FX_MEMBERSHIP_VERSION,
                       completeness=comp, build_error=None)


def _eff(present_sources):
    return {s: make_revision_key(1_700_000_000_000_000 + i, i + 1) for i, s in enumerate(sorted(present_sources))}


def _real_wm(*, asset="usd-krw", lineage_id="sess-1:1", publish_sequence=1, vector=None, missing=None):
    """real(non-placeholder) Watermark — marker/current 용."""
    if vector is None:
        vector = {"kb": make_revision_key(1_600_000_000_000_000, 7)}  # candidate effective와 다른 content
    if missing is None:
        missing = tuple(sorted(FX_MEMBERSHIP_SOURCES - set(vector)))
    return Watermark(asset=asset, lineage_id=lineage_id, publish_sequence=publish_sequence,
                     membership_version=FX_MEMBERSHIP_VERSION, present_revision_vector=vector,
                     missing_sources=missing, sent_at=_CLOCK)


class _Seq:
    def __init__(self, base=1000):
        self._n = base
        self.observed = []

    def next(self, asset, lineage_id):
        self._n += 1
        return self._n

    def observe_assigned(self, asset, lineage_id, assigned):
        self.observed.append((asset, lineage_id, assigned))


def _make_hooks(*, current=None, writer_ok=True, gate=PublisherGateDisposition.PASS_THROUGH,
                flags=(True, True), db_revisions=None, subscriber=3,
                send=None, send_fn=None, lineage="sess-9:1", seq=None,
                build_fn=None, effective_keys_drop=None, write_outcomes_lua="advance"):
    state = {"writer_calls": [], "build_calls": [], "publisher_calls": [], "seq": seq or _Seq()}
    db_revisions = {} if db_revisions is None else db_revisions

    async def watermark_reader(asset):
        return current

    async def watermark_writer(asset, wm):
        state["writer_calls"].append((asset, wm))
        return writer_ok

    async def gate_fn(asset):
        return gate

    async def feature_flags(asset):
        return flags

    async def db_revisions_reader(asset):
        return db_revisions

    async def _default_build(asset):
        state["build_calls"].append(asset)
        return _build_result(asset=asset)

    _build = build_fn or _default_build

    async def effective_resolver(asset, build_result):
        vec = _eff(build_result.present_sources)
        if effective_keys_drop:  # same-read precondition 위반 시뮬 (keys != present)
            for k in effective_keys_drop:
                vec.pop(k, None)
        return vec

    async def write_outcomes_provider(asset, build_result):
        return {s: write_outcome_from_lua(write_outcomes_lua, _WO_REV) for s in build_result.present_sources}

    async def subscriber_counter(asset):
        return subscriber

    async def publisher_fn(asset, payload):
        state["publisher_calls"].append(asset)
        if send_fn is not None:
            return await send_fn(asset, payload)
        return send if send is not None else SendResult(SendDisposition.SENT, 5)

    async def lineage_provider(asset, cur, build_result):
        return lineage

    hooks = CoordinatorHooks(
        watermark_reader=watermark_reader, watermark_writer=watermark_writer, gate_fn=gate_fn,
        feature_flags=feature_flags, db_revisions_reader=db_revisions_reader, build_fn=_build,
        effective_resolver=effective_resolver, write_outcomes_provider=write_outcomes_provider,
        subscriber_counter=subscriber_counter, publisher_fn=publisher_fn, seq_source=state["seq"],
        lineage_provider=lineage_provider, clock=lambda: _CLOCK,
    )
    return hooks, state


class TestPublishResultXor(unittest.TestCase):

    def test_both_none_raises(self):
        with self.assertRaises(ValueError):
            PublishResult(asset="usd-krw")

    def test_both_set_raises(self):
        from app.atomic_reconcile import B2bDecision
        d = B2bDecision(action=DecisionAction.RETRY, watermark_candidate=None, reason="x")
        with self.assertRaises(ValueError):
            PublishResult(asset="usd-krw", decision=d, outcome=PublishOutcome.COMMITTED)

    def test_outcome_only_ok(self):
        r = PublishResult(asset="usd-krw", outcome=PublishOutcome.DISABLED)
        self.assertIsNone(r.decision)

    def test_decision_only_ok(self):
        from app.atomic_reconcile import B2bDecision
        d = B2bDecision(action=DecisionAction.RETRY, watermark_candidate=None, reason="x")
        r = PublishResult(asset="usd-krw", decision=d)
        self.assertIsNone(r.outcome)


class TestSendResult(unittest.TestCase):

    def test_sent_zero_count_raises(self):
        with self.assertRaises(ValueError):
            SendResult(SendDisposition.SENT, 0)

    def test_no_subscribers_positive_count_raises(self):
        with self.assertRaises(ValueError):
            SendResult(SendDisposition.NO_SUBSCRIBERS, 3)

    def test_negative_count_raises(self):
        with self.assertRaises(ValueError):
            SendResult(SendDisposition.SENT, -1)

    def test_sent_ok(self):
        self.assertEqual(SendResult(SendDisposition.SENT, 5).sent_count, 5)

    def test_no_subscribers_zero_ok(self):
        self.assertEqual(SendResult(SendDisposition.NO_SUBSCRIBERS, 0).disposition,
                         SendDisposition.NO_SUBSCRIBERS)


class TestPublishAssetOrchestration(unittest.IsolatedAsyncioTestCase):

    async def test_happy_publish_committed(self):
        hooks, st = _make_hooks()
        coord = AtomicFxCoordinator(hooks)
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.COMMITTED)
        self.assertTrue(r.send_performed)
        self.assertIsNone(r.decision)
        self.assertIsNotNone(r.watermark)
        # materialize: lineage/sent_at 실제값(sentinel 아님)
        self.assertEqual(r.watermark.lineage_id, "sess-9:1")
        self.assertNotEqual(r.watermark.lineage_id, CANDIDATE_PLACEHOLDER_LINEAGE)
        self.assertEqual(len(st["writer_calls"]), 1)
        self.assertEqual(len(st["seq"].observed), 1)

    async def test_gate_would_block_short_circuits_before_build(self):
        hooks, st = _make_hooks(gate=PublisherGateDisposition.WOULD_BLOCK_DRY_RUN)
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.GATE_WOULD_BLOCK)
        self.assertEqual(r.gate_disposition, PublisherGateDisposition.WOULD_BLOCK_DRY_RUN)
        self.assertEqual(st["build_calls"], [])       # build 미호출
        self.assertEqual(st["publisher_calls"], [])   # send 미발생

    async def test_ff_disabled(self):
        hooks, st = _make_hooks(flags=(True, False))
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.DISABLED)
        self.assertEqual(r.feature_flags, (True, False))  # 두 flag 상태 보존
        self.assertEqual(st["publisher_calls"], [])

    async def test_ff_both_off_disabled(self):
        hooks, _ = _make_hooks(flags=(False, False))
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.DISABLED)

    async def test_bad_flags_raises(self):
        hooks, _ = _make_hooks(flags=(True,))  # 잘못된 형태
        with self.assertRaises(ValueError):
            await AtomicFxCoordinator(hooks).publish_asset("usd-krw")

    async def test_subscriber_zero_relays_decision(self):
        hooks, st = _make_hooks(subscriber=0)
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertIsNotNone(r.decision)
        self.assertEqual(r.decision.action, DecisionAction.SKIP_SUBSCRIBER_ZERO)
        self.assertIsNone(r.outcome)
        self.assertEqual(st["publisher_calls"], [])   # send 안 함

    async def test_dedup_relays_decision_no_send_no_write(self):
        # last watermark content == candidate content → SKIP_DEDUP_IDENTICAL
        br = _build_result()
        eff = _eff(br.present_sources)
        last = Watermark(asset="usd-krw", lineage_id="sess-9:1", publish_sequence=5,
                         membership_version=FX_MEMBERSHIP_VERSION, present_revision_vector=eff,
                         missing_sources=br.missing_sources, sent_at=_CLOCK)
        hooks, st = _make_hooks(current=last)
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertIsNotNone(r.decision)
        self.assertEqual(r.decision.action, DecisionAction.SKIP_DEDUP_IDENTICAL)
        self.assertEqual(st["publisher_calls"], [])
        self.assertEqual(st["writer_calls"], [])   # watermark byte-identical (no write)

    async def test_block_relays_decision(self):
        hooks, st = _make_hooks(write_outcomes_lua="conflict")
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertIsNotNone(r.decision)
        self.assertEqual(r.decision.action, DecisionAction.BLOCK)
        self.assertEqual(st["publisher_calls"], [])

    async def test_malformed_build_relays_retry(self):
        async def bad_build(asset):
            return _build_result(asset=asset, malformed=True)
        hooks, st = _make_hooks(build_fn=bad_build)
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertIsNotNone(r.decision)
        self.assertEqual(r.decision.action, DecisionAction.RETRY)
        self.assertEqual(st["publisher_calls"], [])

    async def test_send_no_subscribers(self):
        hooks, st = _make_hooks(send=SendResult(SendDisposition.NO_SUBSCRIBERS, 0))
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.NO_SUBSCRIBERS)
        self.assertEqual(st["writer_calls"], [])   # watermark 미갱신 (pending)

    async def test_send_all_failed(self):
        hooks, _ = _make_hooks(send=SendResult(SendDisposition.ALL_FAILED, 0))
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.ALL_SEND_FAILED)

    async def test_send_exception(self):
        hooks, _ = _make_hooks(send=SendResult(SendDisposition.EXCEPTION, 0))
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.SEND_EXCEPTION)

    async def test_watermark_write_failed_stores_marker(self):
        hooks, st = _make_hooks(writer_ok=False)
        coord = AtomicFxCoordinator(hooks)
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.WATERMARK_WRITE_FAILED)
        self.assertTrue(r.send_performed)
        self.assertIn("usd-krw", coord._markers)   # sent_but_uncommitted marker 보관
        self.assertEqual(coord._markers["usd-krw"], r.watermark)

    async def test_invalid_asset_raises(self):
        hooks, _ = _make_hooks()
        with self.assertRaises(ValueError):
            await AtomicFxCoordinator(hooks).publish_asset("usdt-krw")

    async def test_malformed_gate_return_fails_closed(self):
        # MED1: gate_fn이 disposition 외 반환 → fail-closed(ValueError), send으로 fall-open 안 함
        async def bad_gate(asset):
            return "OPEN"   # non-disposition
        hooks, st = _make_hooks()
        hooks = CoordinatorHooks(**{**hooks.__dict__, "gate_fn": bad_gate})
        with self.assertRaises(ValueError):
            await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(st["publisher_calls"], [])   # send 미발생(fail-open 차단)

    async def test_publisher_raises_funnels_to_send_exception(self):
        # LOW2/J1: publisher_fn raise(self-report 아님) → SEND_EXCEPTION + send_error 캡처(swallow 아님), state-safe
        async def boom_pub(asset, payload):
            raise RuntimeError("dispatcher down")
        hooks, st = _make_hooks(send_fn=boom_pub)
        coord = AtomicFxCoordinator(hooks)
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.SEND_EXCEPTION)
        self.assertIn("dispatcher down", r.send_error)
        self.assertEqual(st["writer_calls"], [])       # watermark 미기록(seq/write 전 raise)
        self.assertNotIn("usd-krw", coord._markers)    # marker 미생성

    async def test_publisher_cancelled_propagates(self):
        # CancelledError(BaseException)는 미포획 — task cancel 전파 보존
        async def cancel_pub(asset, payload):
            raise asyncio.CancelledError()
        hooks, _ = _make_hooks(send_fn=cancel_pub)
        with self.assertRaises(asyncio.CancelledError):
            await AtomicFxCoordinator(hooks).publish_asset("usd-krw")

    async def test_pending_telemetry_attached_on_publish(self):
        # MED12: pending이 derive_pending(in-lock current)으로 산출되어 결과에 첨부됨
        from app.atomic_reconcile import PendingReason
        from app.atomic_value_schema import make_revision_key as _mk
        db_revs = {"kb": (1_700_000_000_900_000, 5)}   # DB에 kb revision 존재, watermark None → PENDING_NOT_IN_PRESENT
        hooks, _ = _make_hooks(db_revisions=db_revs)
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.COMMITTED)
        self.assertIsNotNone(r.pending)
        self.assertEqual(set(r.pending), set(FX_MEMBERSHIP_SOURCES))
        self.assertEqual(r.pending["kb"], PendingReason.PENDING_NOT_IN_PRESENT)

    async def test_pending_telemetry_attached_on_relay(self):
        # MED12: 결정 relay(subscriber 0) 결과에도 pending 첨부
        from app.atomic_reconcile import PendingReason
        db_revs = {"hana": (1_700_000_000_900_000, 5)}
        hooks, _ = _make_hooks(subscriber=0, db_revisions=db_revs)
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertIsNotNone(r.decision)
        self.assertIsNotNone(r.pending)
        self.assertEqual(r.pending["hana"], PendingReason.PENDING_NOT_IN_PRESENT)

    async def test_same_read_precondition_violation_propagates(self):
        # effective keys != present_sources → decide_and_build_next ValueError 전파
        hooks, _ = _make_hooks(effective_keys_drop=["kb"])
        with self.assertRaises(ValueError):
            await AtomicFxCoordinator(hooks).publish_asset("usd-krw")


class TestSeqFloorAndLineage(unittest.IsolatedAsyncioTestCase):

    async def test_new_lineage_uses_proposal(self):
        # current None → floor 없음, proposal 그대로
        hooks, st = _make_hooks(seq=_Seq(base=1000))
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.watermark.publish_sequence, 1001)
        self.assertEqual(st["seq"].observed, [("usd-krw", "sess-9:1", 1001)])

    async def test_same_lineage_floors_to_current_plus_one(self):
        # current 같은 lineage + 높은 seq, 낮은 proposal → assigned = current.seq+1 (regression 방지)
        current = _real_wm(lineage_id="sess-9:1", publish_sequence=5000)
        hooks, st = _make_hooks(current=current, seq=_Seq(base=1000), lineage="sess-9:1")
        r = await AtomicFxCoordinator(hooks).publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.COMMITTED)   # NEWER → WRITE
        self.assertEqual(r.watermark.publish_sequence, 5001)
        self.assertEqual(st["seq"].observed, [("usd-krw", "sess-9:1", 5001)])

    async def test_eviction_alive_no_regression_across_calls(self):
        # LOW13: observe_assigned feedback이 eviction(watermark None) 후에도 seq regression 방지.
        # history-honoring seq(다음 = 관측 최대+1)를 쓰면, 1차 발급 N 후 watermark 증발해도 2차 > N.
        class _FloorSeq:
            def __init__(self):
                self.observed_max = {}
            def next(self, asset, lineage_id):
                return self.observed_max.get((asset, lineage_id), 0) + 1
            def observe_assigned(self, asset, lineage_id, assigned):
                k = (asset, lineage_id)
                self.observed_max[k] = max(self.observed_max.get(k, 0), assigned)

        seq = _FloorSeq()
        # 1차: current 같은 lineage seq=500 → floor 501 발급 + observe
        current1 = _real_wm(lineage_id="sess-9:1", publish_sequence=500)
        hooks1, _ = _make_hooks(current=current1, seq=seq, lineage="sess-9:1")
        coord = AtomicFxCoordinator(hooks1)
        r1 = await coord.publish_asset("usd-krw")
        self.assertEqual(r1.watermark.publish_sequence, 501)
        # 2차: watermark 증발(current None=eviction) — floor 대상 없음, proposal만. history 덕에 502 > 501
        hooks2, _ = _make_hooks(current=None, seq=seq, lineage="sess-9:1")
        coord._hooks = hooks2   # 같은 seq source 유지(in-memory 연속성)
        r2 = await coord.publish_asset("usd-krw")
        self.assertGreater(r2.watermark.publish_sequence, r1.watermark.publish_sequence)  # regression 없음
        self.assertEqual(r2.watermark.publish_sequence, 502)

    async def test_cross_lineage_no_floor_then_mismatch(self):
        # current lineage != provider lineage → floor 안 함(proposal) + classify LINEAGE_MISMATCH
        current = _real_wm(lineage_id="other:1", publish_sequence=5000)
        hooks, st = _make_hooks(current=current, seq=_Seq(base=1000), lineage="sess-9:1")
        coord = AtomicFxCoordinator(hooks)
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.watermark.publish_sequence, 1001)    # cross-lineage floor 금지
        self.assertEqual(r.outcome, PublishOutcome.WATERMARK_LINEAGE_MISMATCH)
        self.assertEqual(r.write_action, WriteAction.DEFER_LINEAGE_ARBITRATION)
        self.assertNotIn("usd-krw", coord._markers)   # J2: send-path LINEAGE_MISMATCH은 marker 미생성


class TestMarkerRecovery(unittest.IsolatedAsyncioTestCase):

    async def test_marker_write_success_clears_and_committed(self):
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=7)
        hooks, st = _make_hooks(current=None)   # NO_CURRENT → WRITE
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.COMMITTED)
        self.assertTrue(r.recovered_from_marker)
        self.assertFalse(r.send_performed)             # 재전송 없음
        self.assertNotIn("usd-krw", coord._markers)    # 해소
        self.assertEqual(st["publisher_calls"], [])    # send 미호출

    async def test_marker_short_circuits_gate_ff_build(self):
        # marker HIT은 gate WOULD_BLOCK + FF off + build 예외라도 우회 (send 이미 발생)
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=7)
        async def boom_build(asset):
            raise AssertionError("build_fn은 marker 경로에서 호출되면 안 됨")
        hooks, st = _make_hooks(current=None, gate=PublisherGateDisposition.WOULD_BLOCK_DRY_RUN,
                                flags=(False, False), build_fn=boom_build)
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.COMMITTED)
        self.assertTrue(r.recovered_from_marker)

    async def test_marker_write_fail_retained(self):
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=7)
        hooks, _ = _make_hooks(current=None, writer_ok=False)
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.WATERMARK_WRITE_FAILED)
        self.assertEqual(coord._markers["usd-krw"], marker)   # 유지(다음 재시도)

    async def test_marker_same_idempotent_clears_no_write(self):
        # current == marker (SAME) → 멱등 recovery, write 시도 안 함, 해소
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=7)
        hooks, st = _make_hooks(current=marker)
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.COMMITTED)
        self.assertTrue(r.recovered_from_marker)
        self.assertEqual(st["writer_calls"], [])       # SKIP_IDEMPOTENT — write 안 함
        self.assertNotIn("usd-krw", coord._markers)

    async def test_marker_older_stale_clears(self):
        # current가 더 새 seq(같은 lineage) → marker OLDER → stale, 해소
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=7)
        newer = _real_wm(lineage_id="sess-3:2", publish_sequence=99,
                         vector={"hana": make_revision_key(1_650_000_000_000_000, 3)})
        hooks, st = _make_hooks(current=newer)
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.WATERMARK_STALE)
        self.assertEqual(st["writer_calls"], [])
        self.assertNotIn("usd-krw", coord._markers)    # stale → 해소

    async def test_marker_lineage_mismatch_dropped(self):
        # J2: lineage takeover가 옛 pending write supersede → marker drop(starvation 방지). C6는 outcome telemetry로 인지.
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=7)
        other = _real_wm(lineage_id="different:1", publish_sequence=7,
                         vector={"hana": make_revision_key(1_650_000_000_000_000, 3)})
        hooks, _ = _make_hooks(current=other)
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.WATERMARK_LINEAGE_MISMATCH)
        self.assertNotIn("usd-krw", coord._markers)   # drop (retain 시 fresh publish 영구 starvation)

    async def test_marker_newer_same_lineage_write(self):
        # MED10: 현실적 recovery — marker.seq > current.seq(같은 lineage) → NEWER → WRITE(marker 기록).
        current = _real_wm(lineage_id="sess-3:2", publish_sequence=3,
                           vector={"kb": make_revision_key(1_600_000_000_000_000, 7)})
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=9,
                          vector={"kb": make_revision_key(1_600_000_000_000_000, 7)})
        hooks, st = _make_hooks(current=current)
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.COMMITTED)
        self.assertEqual(st["writer_calls"], [("usd-krw", marker)])   # marker(not real)로 write
        self.assertNotIn("usd-krw", coord._markers)

    async def test_marker_divergent_clears(self):
        # MED11: same lineage+seq, divergent content → ALERT_DIVERGENT → clear + alert.
        marker = _real_wm(lineage_id="sess-3:2", publish_sequence=7,
                          vector={"kb": make_revision_key(1_600_000_000_000_000, 7)})
        divergent = _real_wm(lineage_id="sess-3:2", publish_sequence=7,
                             vector={"kb": make_revision_key(1_600_000_000_000_000, 999)})
        hooks, st = _make_hooks(current=divergent)
        coord = AtomicFxCoordinator(hooks)
        coord._markers["usd-krw"] = marker
        r = await coord.publish_asset("usd-krw")
        self.assertEqual(r.outcome, PublishOutcome.WATERMARK_DIVERGENT_ALERT)
        self.assertEqual(st["writer_calls"], [])
        self.assertNotIn("usd-krw", coord._markers)   # invariant 위반 — alert 후 해소(고착 방지)


class TestLockSerialization(unittest.IsolatedAsyncioTestCase):

    async def test_same_asset_serialized(self):
        overlap = {"max": 0, "cur": 0}

        async def slow_pub(asset, payload):
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
            await asyncio.sleep(0.02)
            overlap["cur"] -= 1
            return SendResult(SendDisposition.SENT, 5)

        hooks, _ = _make_hooks(send_fn=slow_pub)
        coord = AtomicFxCoordinator(hooks)
        await asyncio.gather(coord.publish_asset("usd-krw"), coord.publish_asset("usd-krw"))
        self.assertEqual(overlap["max"], 1)   # 같은 asset 동시 실행 0 (lock 직렬화)

    async def test_diff_asset_concurrent(self):
        overlap = {"max": 0, "cur": 0}

        async def slow_pub(asset, payload):
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
            await asyncio.sleep(0.02)
            overlap["cur"] -= 1
            return SendResult(SendDisposition.SENT, 5)

        hooks, _ = _make_hooks(send_fn=slow_pub)
        coord = AtomicFxCoordinator(hooks)
        await asyncio.gather(coord.publish_asset("usd-krw"), coord.publish_asset("jpy-krw"))
        self.assertEqual(overlap["max"], 2)   # 다른 asset 병행 가능


_LIVE_IO_FORBIDDEN = frozenset({
    "cache", "latest_rates_cache", "topic_dispatcher", "fx_topic_publisher",
    "fx_topic_payload", "crud", "database", "redis",
})
_LIVE_CALL_NEEDLES = frozenset({
    "publish_topic", "safe_publish_fx_snapshot", "_publish_fx_snapshot", "send_json",
})


def _is_type_checking_if(node):
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return ((isinstance(t, ast.Name) and t.id == "TYPE_CHECKING")
            or (isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"))


def _walk_skip_type_checking(node):
    """TYPE_CHECKING if 본문은 건너뛰며 노드 yield (import은 TYPE_CHECKING 안에선 허용)."""
    for child in ast.iter_child_nodes(node):
        if _is_type_checking_if(child):
            continue
        yield child
        yield from _walk_skip_type_checking(child)


def _scan_direct_live_io(src):
    """src 텍스트에서 직접 live-I/O import / publish·redis call 위반 목록 반환 (pure — self-arming 가능).

    `from app.cache import x`(module tail) + `from app import cache`(app 하위 name, MED6 blind spot) + `import
    app.cache` + live call node 모두 검출. TYPE_CHECKING-guarded import은 허용(제외)."""
    violations = []
    for node in _walk_skip_type_checking(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            tail = (node.module or "").split(".")[-1]
            if tail in _LIVE_IO_FORBIDDEN:
                violations.append(f"from {node.module} import — live I/O")
            if node.module == "app":  # MED6: `from app import cache` 형태
                for a in node.names:
                    if a.name in _LIVE_IO_FORBIDDEN:
                        violations.append(f"from app import {a.name} — live I/O")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] in _LIVE_IO_FORBIDDEN:
                    violations.append(f"import {a.name} — live I/O")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _LIVE_CALL_NEEDLES:
                violations.append(f"{name}() — live publish/redis call")
    return violations


def _scan_module_usage(src, module_name, call_needles):
    """src에서 특정 module import/call/문자열 참조 검출 (negative skip-list detector, self-arming 가능)."""
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
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in call_needles:
                violations.append(f"{name}()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if module_name in node.value or any(n in node.value for n in call_needles):
                violations.append(f"str {node.value!r}")
    return violations


class TestPositiveDormancyAst(unittest.TestCase):
    """B2b-4b는 첫 async/IO unit — skip-list 가드는 island 멤버(coordinator 포함)를 skip하므로 coordinator
    **자체**의 live import/call을 못 잡는 blind spot. 이 positive trip-wire가 그 구멍을 막는다."""

    def test_coordinator_has_no_direct_live_io(self):
        src = pathlib.Path(ac.__file__).read_text(encoding="utf-8")
        self.assertEqual(_scan_direct_live_io(src), [],
                         "atomic_coordinator.py에 직접 live I/O import/call 발견 — dormant 위반")

    def test_positive_scanner_self_arms(self):
        # HIGH8: detector가 실제 위반에 trip하는지 증명(planted violation). green-only 가드 방지.
        for planted in (
            "from app.cache import redis_client\n",
            "from app import cache\n",                      # MED6 blind spot 형태
            "from app import latest_rates_cache as lrc\n",
            "import app.topic_dispatcher\n",
            "def f():\n    publish_topic('t', {})\n",
            "async def f(c):\n    await c.send_json({})\n",
        ):
            self.assertTrue(_scan_direct_live_io(planted),
                            f"planted 위반인데 detector 미검출: {planted!r}")

    def test_positive_scanner_allows_type_checking_import(self):
        # TYPE_CHECKING-guarded live import은 허용(런타임 import 아님)
        clean = ("from typing import TYPE_CHECKING\n"
                 "if TYPE_CHECKING:\n    from app import cache\n")
        self.assertEqual(_scan_direct_live_io(clean), [])


if __name__ == "__main__":
    unittest.main()
