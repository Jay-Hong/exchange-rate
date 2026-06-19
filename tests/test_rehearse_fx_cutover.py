"""P1b C6-9b — FX cutover shadow rehearsal harness 단위 테스트 (scripts/rehearse_fx_cutover.py).

structural send/write 0(no-op override) + cross-thread session 닫기(to_thread 우회) + synthetic
write_outcomes(PUBLISH_CANDIDATE) + legacy re-derive + classify(1:1 아님) + end-to-end(dormant MALFORMED→RETRY /
COMPLETE→COMMITTED no-op) + rehearse 배선.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# scripts/ 경로 추가 (test_hana_observed_eod_writer.py 패턴)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import crud  # noqa: E402  (config/topic_dispatcher는 R.config/R.topic_dispatcher로 patch)
from app import atomic_fx_live as m  # noqa: E402
from app.atomic_build import BuildCompleteness, BuildResult  # noqa: E402
from app.atomic_coordinator import (  # noqa: E402
    PublishOutcome,
    PublishResult,
    SendDisposition,
)
from app.atomic_cutover import PublisherGateDisposition  # noqa: E402
from app.atomic_fx_v2_loader import FxV2LoadResult  # noqa: E402
from app.atomic_reconcile import B2bDecision, DecisionAction  # noqa: E402
from app.atomic_value_schema import make_revision_key  # noqa: E402
from app.atomic_watermark import Watermark  # noqa: E402
from app.atomic_write_outcome import CandidateDisposition, candidate_disposition  # noqa: E402
from app.fx_topic_publisher import FX_TOPICS  # noqa: E402

import rehearse_fx_cutover as R  # noqa: E402

_REV = make_revision_key(1_700_000_000_000_000, 5)


# ── 재사용 헬퍼 (test_atomic_fx_live.py 패턴) ──
def _payload(bank_sources, *, asset="usd-krw", with_reference=False):
    banks = [{"source": s, "asset": asset, "rate": 1300.0 + i, "timestamp": "t"}
             for i, s in enumerate(bank_sources)]
    data = {"banks": banks}
    if with_reference:
        data["reference"] = {"source": "investing", "asset": asset, "rate": 1301.0, "timestamp": "t"}
    return {"type": "snapshot", "version": 1, "data": data}


def _load(present_revs, *, asset="usd-krw"):
    bank_sources = [s for s in present_revs if s != "investing"]
    payload = _payload(bank_sources, asset=asset, with_reference=("investing" in present_revs))
    effective = {s: r for s, r in present_revs.items() if r is not None}
    present = tuple(sorted(present_revs.keys()))
    return FxV2LoadResult(payload=payload, effective_revision_vector=effective, present_sources=present)


class _FakeStoreClient:
    def __init__(self):
        self.kv = {}
        self.set_calls = 0

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):  # store.write가 호출하면 카운트 — no-op override면 0이어야
        self.set_calls += 1
        self.kv[key] = value
        return True


class _FakeRegistry:
    def __init__(self, count):
        self._count = count

    def subscriber_count(self, topic):
        return self._count


class _FakeSnap:
    def __init__(self, *, gate_open=True, session_id="s", generation=1):
        self.publisher_gate_open = gate_open
        self.bootstrap_session_id = session_id
        self.bootstrap_generation = generation


def _rehearsal_adapter(*, load=None, client=None, snap=None, banks=None, investing=None):
    return R._RehearsalAdapter(
        db=object(),
        client=client if client is not None else _FakeStoreClient(),
        loader=(lambda db, asset: load) if load is not None else (lambda db, asset: _load({"kb": _REV})),
        snapshot_fn=(lambda: snap) if snap is not None else (lambda: _FakeSnap()),
        bank_revisions_selector=(lambda db, asset: banks if banks is not None else []),
        investing_revision_selector=(lambda db, asset: investing),
    )


def _wm(asset="usd-krw"):
    return Watermark(
        asset=asset, lineage_id="s:1", publish_sequence=3, membership_version=1,
        present_revision_vector={"kb": _REV}, missing_sources=(), sent_at="2026-06-19T00:00:00+09:00",
    )


# ────────────────────────────── no-op adapter ──────────────────────────────
class TestNoopAdapter(unittest.IsolatedAsyncioTestCase):

    async def test_publisher_never_sends_returns_sent_when_subscribers(self):
        ad = _rehearsal_adapter()
        with patch.object(R.topic_dispatcher, "registry", _FakeRegistry(7)), \
             patch.object(R.topic_dispatcher, "publish_topic_detailed") as spy:
            res = await ad._publisher_fn("usd-krw", {"type": "snapshot"})
        spy.assert_not_called()                                   # 실제 send 0 (구조적)
        self.assertIs(res.disposition, SendDisposition.SENT)
        self.assertEqual(res.sent_count, 7)
        self.assertEqual(len(ad.would_sends), 1)
        self.assertEqual(ad.would_sends[0]["subscriber_count"], 7)
        self.assertEqual(ad.would_sends[0]["topic"], FX_TOPICS["usd-krw"])

    async def test_publisher_no_subscribers(self):
        ad = _rehearsal_adapter()
        with patch.object(R.topic_dispatcher, "registry", _FakeRegistry(0)), \
             patch.object(R.topic_dispatcher, "publish_topic_detailed") as spy:
            res = await ad._publisher_fn("jpy-krw", {"type": "snapshot"})
        spy.assert_not_called()
        self.assertIs(res.disposition, SendDisposition.NO_SUBSCRIBERS)
        self.assertEqual(res.sent_count, 0)

    async def test_watermark_writer_never_writes(self):
        client = _FakeStoreClient()
        ad = _rehearsal_adapter(client=client)
        ok = await ad._watermark_writer("usd-krw", _wm())
        self.assertTrue(ok)                                      # coordinator 완주용 True
        self.assertEqual(client.set_calls, 0)                   # 실제 store.write 0 (구조적)
        self.assertEqual(len(ad.would_writes), 1)
        self.assertEqual(ad.would_writes[0]["lineage_id"], "s:1")

    async def test_db_revisions_no_to_thread(self):
        # codex point 6: override가 asyncio.to_thread 미사용 — to_thread를 raise로 막아도 동작.
        from app.atomic_revision import Revision

        class _Row:
            def __init__(self, source, revision):
                self.source = source
                self.revision = revision

        ad = _rehearsal_adapter(banks=[_Row("kb", (1, 2))], investing=_Row("investing", (3, 4)))
        with patch.object(m.asyncio, "to_thread", side_effect=AssertionError("to_thread 사용됨")):
            out = await ad._db_revisions_reader("usd-krw")
        self.assertEqual(out, {"kb": (1, 2), "investing": (3, 4)})


# ────────────────────────────── synthetic write_outcomes ──────────────────────────────
class TestSyntheticOutcomes(unittest.IsolatedAsyncioTestCase):

    def _valid_br(self, present):
        # partition invariant(present∪missing==FX_MEMBERSHIP_SOURCES, atomic_build.py:96) + payload의 실제
        # present == present_sources(_extract_present_sources 대조) 둘 다 충족 — _payload로 유효 payload 구성
        from app.fx_membership import FX_MEMBERSHIP_SOURCES
        bank_sources = [s for s in present if s != "investing"]
        payload = _payload(bank_sources, with_reference=("investing" in present))
        missing = tuple(sorted(FX_MEMBERSHIP_SOURCES - set(present)))
        comp = BuildCompleteness.COMPLETE if not missing else BuildCompleteness.PARTIAL
        return BuildResult(
            asset="usd-krw", payload=payload, present_sources=tuple(sorted(present)), missing_sources=missing,
            membership_version=1, completeness=comp, build_error=None,
        )

    def _malformed_br(self):
        return BuildResult(
            asset="usd-krw", payload=None, present_sources=(), missing_sources=(), membership_version=1,
            completeness=BuildCompleteness.MALFORMED, build_error="e",
        )

    async def test_synthetic_all_publish_candidate(self):
        br = self._valid_br(("kb", "investing"))
        wo = await R.synthetic_publish_candidate_outcomes("usd-krw", br)
        self.assertEqual(set(wo), {"kb", "investing"})
        for o in wo.values():
            self.assertIs(candidate_disposition(o), CandidateDisposition.PUBLISH_CANDIDATE)

    async def test_synthetic_malformed_empty(self):
        br = self._malformed_br()
        wo = await R.synthetic_publish_candidate_outcomes("usd-krw", br)
        self.assertEqual(wo, {})


# ────────────────────────────── legacy re-derive ──────────────────────────────
class TestLegacyReDerive(unittest.TestCase):

    def test_disabled(self):
        with patch.object(R.config, "FX_TOPIC_ENABLED", False), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True):
            self.assertEqual(R.legacy_would_action("usd-krw"), "skipped_disabled")

    def test_no_subscribers(self):
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(0)):
            self.assertEqual(R.legacy_would_action("usd-krw"), "skipped_no_subscribers")

    def test_would_publish(self):
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(3)):
            self.assertEqual(R.legacy_would_action("usd-krw"), "would_publish")


# ────────────────────────────── classify (coordinator-disposition 중심) ──────────────────────────────
class TestClassify(unittest.TestCase):

    def test_gate_blocked(self):
        # gate은 coordinator 전용 축 — legacy 무관 gate_blocked
        self.assertEqual(R.classify(PublishOutcome.GATE_WOULD_BLOCK.value, "would_publish"), "gate_blocked")
        self.assertEqual(R.classify(PublishOutcome.GATE_WOULD_BLOCK.value, "skipped_disabled"), "gate_blocked")

    def test_atomic_defers_retry_regardless_of_legacy(self):
        # HIGH #4 + 0-sub 현실: RETRY는 coordinator 전용 축(MALFORMED/general-failed) → legacy 무관 atomic_defers
        # (이전엔 legacy!=would_publish면 review로 떨어졌음 — 0-sub standalone에서 전 asset review 오판)
        for legacy in ("would_publish", "skipped_no_subscribers", "skipped_disabled"):
            self.assertEqual(R.classify(DecisionAction.RETRY.value, legacy), "atomic_defers")

    def test_atomic_block(self):
        self.assertEqual(R.classify(DecisionAction.BLOCK.value, "would_publish"), "atomic_block")

    def test_skip_dedup(self):
        self.assertEqual(R.classify(DecisionAction.SKIP_DEDUP_IDENTICAL.value, "would_publish"), "skip_dedup")

    def test_skip_no_subscribers_agree_vs_divergent(self):
        self.assertEqual(
            R.classify(DecisionAction.SKIP_SUBSCRIBER_ZERO.value, "skipped_no_subscribers"), "skip_no_subscribers"
        )
        self.assertEqual(
            R.classify(DecisionAction.SKIP_SUBSCRIBER_ZERO.value, "would_publish"), "skip_no_subscribers_divergent"
        )
        # post-send NO_SUBSCRIBERS도 동일 버킷 (이전엔 review로 오분류 — HIGH #4)
        self.assertEqual(
            R.classify(PublishOutcome.NO_SUBSCRIBERS.value, "skipped_no_subscribers"), "skip_no_subscribers"
        )

    def test_disabled_agree_vs_divergent(self):
        self.assertEqual(R.classify(PublishOutcome.DISABLED.value, "skipped_disabled"), "disabled")
        self.assertEqual(R.classify(PublishOutcome.DISABLED.value, "would_publish"), "disabled_divergent")

    def test_would_publish_agree_vs_divergent(self):
        self.assertEqual(R.classify(PublishOutcome.COMMITTED.value, "would_publish"), "would_publish")
        self.assertEqual(R.classify(PublishOutcome.COMMITTED.value, "skipped_disabled"), "would_publish_divergent")

    def test_send_issue(self):
        # post-send 이상 — 이전엔 review로 떨어짐 (HIGH #4). no-op publisher라 rehearsal에선 희소하나 방어적 매핑.
        for coord in (PublishOutcome.ALL_SEND_FAILED.value, PublishOutcome.SEND_EXCEPTION.value,
                      PublishOutcome.WATERMARK_WRITE_FAILED.value, PublishOutcome.WATERMARK_LINEAGE_MISMATCH.value):
            self.assertEqual(R.classify(coord, "would_publish"), "send_issue")

    def test_review_only_for_unmapped(self):
        # 진짜 미매핑(알 수 없는 coord_label)만 review
        self.assertEqual(R.classify("totally_unknown_outcome", "would_publish"), "review")

    def test_attention_set_membership(self):
        # divergent/review/send_issue/atomic_block은 attention, 나머지는 expected/safe
        self.assertIn("review", R.ATTENTION_CLASSES)
        self.assertIn("would_publish_divergent", R.ATTENTION_CLASSES)
        self.assertIn("send_issue", R.ATTENTION_CLASSES)
        self.assertIn("atomic_block", R.ATTENTION_CLASSES)
        self.assertNotIn("atomic_defers", R.ATTENTION_CLASSES)
        self.assertNotIn("gate_blocked", R.ATTENTION_CLASSES)
        self.assertNotIn("would_publish", R.ATTENTION_CLASSES)


# ────────────────────────────── _to_asset_rehearsal ──────────────────────────────
class TestToAssetRehearsal(unittest.TestCase):

    def test_decision_case(self):
        pr = PublishResult(asset="usd-krw", decision=B2bDecision(DecisionAction.RETRY, None, "x"))
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(3)):
            ar = R._to_asset_rehearsal("usd-krw", pr)
        self.assertEqual(ar.coord_kind, "decision")
        self.assertEqual(ar.coord_label, "retry")
        self.assertEqual(ar.legacy_action, "would_publish")
        self.assertEqual(ar.classification, "atomic_defers")
        self.assertFalse(ar.would_send)
        self.assertFalse(ar.would_write)

    def test_outcome_gate_case(self):
        pr = PublishResult(asset="usd-krw", outcome=PublishOutcome.GATE_WOULD_BLOCK,
                           gate_disposition=PublisherGateDisposition.WOULD_BLOCK_DRY_RUN)
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(3)):
            ar = R._to_asset_rehearsal("usd-krw", pr)
        self.assertEqual(ar.coord_kind, "outcome")
        self.assertEqual(ar.coord_label, "gate_would_block")
        self.assertEqual(ar.gate_disposition, "would_block_dry_run")
        self.assertEqual(ar.classification, "gate_blocked")

    def test_committed_true_mapping(self):
        # #12: would_send/would_write TRUE 매핑 (COMMITTED + WriteAction.WRITE + send_performed=True)
        from app.atomic_coordinator import WriteAction
        pr = PublishResult(asset="usd-krw", outcome=PublishOutcome.COMMITTED,
                           write_action=WriteAction.WRITE, send_performed=True)
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(3)):
            ar = R._to_asset_rehearsal("usd-krw", pr)
        self.assertEqual(ar.coord_label, "committed")
        self.assertTrue(ar.would_send)
        self.assertTrue(ar.would_write)
        self.assertEqual(ar.classification, "would_publish")

    def test_skip_stale_not_would_write(self):
        # write_action != WRITE → would_write False (정확 enum 비교)
        from app.atomic_coordinator import WriteAction
        pr = PublishResult(asset="usd-krw", outcome=PublishOutcome.WATERMARK_STALE,
                           write_action=WriteAction.SKIP_STALE, send_performed=True)
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(3)):
            ar = R._to_asset_rehearsal("usd-krw", pr)
        self.assertTrue(ar.would_send)
        self.assertFalse(ar.would_write)


# ────────────────────────────── end-to-end (직접 coordinator) ──────────────────────────────
class TestEndToEnd(unittest.IsolatedAsyncioTestCase):

    async def test_dormant_malformed_retries_no_send(self):
        # dormant: loader raise → build MALFORMED → decide RETRY, send/write 0
        def boom(db, asset):
            raise RuntimeError("no v2 data")
        ad = _rehearsal_adapter(load=None, snap=_FakeSnap(gate_open=True))
        ad._loader = boom  # type: ignore[assignment]
        coord = ad.build_coordinator(write_outcomes_provider=R.synthetic_publish_candidate_outcomes)
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(5)):
            res = await coord.publish_asset("usd-krw")
        self.assertIsNotNone(res.decision)
        self.assertIs(res.decision.action, DecisionAction.RETRY)
        self.assertEqual(ad.would_sends, [])
        self.assertEqual(ad.would_writes, [])

    async def test_complete_publishes_committed_noop(self):
        # COMPLETE v2 + synthetic candidate + gate open + subscriber>0 → COMMITTED (no-op publisher/writer)
        client = _FakeStoreClient()
        ad = _rehearsal_adapter(
            load=_load({"kb": _REV, "investing": _REV}), client=client, snap=_FakeSnap(gate_open=True)
        )
        coord = ad.build_coordinator(write_outcomes_provider=R.synthetic_publish_candidate_outcomes)
        with patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(4)), \
             patch.object(R.topic_dispatcher, "publish_topic_detailed") as spy:
            res = await coord.publish_asset("usd-krw")
        self.assertIs(res.outcome, PublishOutcome.COMMITTED)
        spy.assert_not_called()                  # 실제 send 0
        self.assertEqual(client.set_calls, 0)    # 실제 store.write 0
        self.assertEqual(len(ad.would_sends), 1)
        self.assertEqual(len(ad.would_writes), 1)


# ────────────────────────────── rehearse 배선 + render ──────────────────────────────
class TestRehearseWiring(unittest.IsolatedAsyncioTestCase):

    async def test_rehearse_force_gate_open_runs_all_assets(self):
        from app.atomic_build import FX_TOPIC_ASSETS

        def boom(db, asset):
            raise RuntimeError("dormant — no v2")

        client = _FakeStoreClient()
        with patch.object(m, "load_fx_topic_payload_with_revisions", boom), \
             patch.object(crud, "_select_latest_bank_rates_with_revision", lambda db, a: []), \
             patch.object(crud, "_select_latest_investing_rate_with_revision", lambda db, a: None), \
             patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(2)):
            report = await R.rehearse(object(), force_gate_open=True, client=client)

        self.assertEqual(len(report.assets), len(FX_TOPIC_ASSETS))
        # force gate open + FF on + subs>0 + loader raise(MALFORMED→RETRY) → 전부 atomic_defers
        for a in report.assets:
            self.assertEqual(a.classification, "atomic_defers")
        self.assertEqual(report.would_sends, [])     # RETRY라 send 0
        self.assertIn("FORCED OPEN", report.gate_note)
        # render_report는 예외 없이 문자열 생성
        text = R.render_report(report)
        self.assertIn("DRY-RUN", text)
        self.assertIn("atomic_defers", text)

    def test_main_requires_confirm_flag(self):
        # --confirm-read-prod 없으면 거부(exit 2) — backend READ 우발 실행 방지
        rc = R.main([])
        self.assertEqual(rc, 2)

    async def test_rehearse_faithful_gate_closed(self):
        # #11: faithful(non-forced) 경로 — read_cutover_snapshot_fresh 사용. gate closed → 전 asset gate_blocked
        from app.atomic_cutover import CutoverState

        def boom(db, asset):
            raise RuntimeError("dormant")

        closed = R.CutoverReadinessSnapshot(
            writer_enforced_action=R.WriterMode.LEGACY, cutover_state=CutoverState.HALT_BLOCKED,
            bootstrap_session_id="s", bootstrap_generation=1, bootstrap_status="idle",
            per_asset_publish_state=(), publisher_gate_open=False, read_ok=False,
        )
        with patch.object(R, "read_cutover_snapshot_fresh", lambda db: closed), \
             patch.object(m, "load_fx_topic_payload_with_revisions", boom), \
             patch.object(crud, "_select_latest_bank_rates_with_revision", lambda db, a: []), \
             patch.object(crud, "_select_latest_investing_rate_with_revision", lambda db, a: None), \
             patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(2)):
            report = await R.rehearse(object(), force_gate_open=False, client=_FakeStoreClient())
        self.assertIn("faithful", report.gate_note)
        self.assertEqual(report.snapshot_state, "halt_blocked")
        self.assertFalse(report.snapshot_read_ok)
        for a in report.assets:
            self.assertEqual(a.coord_label, "gate_would_block")
            self.assertEqual(a.classification, "gate_blocked")

    async def test_rehearse_faithful_gate_open_defers(self):
        # faithful + gate open + MALFORMED(dormant) → RETRY → atomic_defers (legacy 무관)
        from app.atomic_cutover import CutoverState

        def boom(db, asset):
            raise RuntimeError("dormant")

        opened = R.CutoverReadinessSnapshot(
            writer_enforced_action=R.WriterMode.LEGACY, cutover_state=CutoverState.ATOMIC_READY,
            bootstrap_session_id="s", bootstrap_generation=1, bootstrap_status="completed",
            per_asset_publish_state=(), publisher_gate_open=True, read_ok=True,
        )
        with patch.object(R, "read_cutover_snapshot_fresh", lambda db: opened), \
             patch.object(m, "load_fx_topic_payload_with_revisions", boom), \
             patch.object(crud, "_select_latest_bank_rates_with_revision", lambda db, a: []), \
             patch.object(crud, "_select_latest_investing_rate_with_revision", lambda db, a: None), \
             patch.object(R.config, "FX_TOPIC_ENABLED", True), \
             patch.object(R.config, "TOPIC_DISPATCHER_ENABLED", True), \
             patch.object(R.topic_dispatcher, "registry", _FakeRegistry(2)):
            report = await R.rehearse(object(), force_gate_open=False, client=_FakeStoreClient())
        self.assertIn("faithful", report.gate_note)
        self.assertEqual(report.snapshot_state, "atomic_ready")
        for a in report.assets:
            self.assertEqual(a.classification, "atomic_defers")


class TestRenderAttention(unittest.TestCase):

    def test_render_surfaces_attention_classes(self):
        # #10: review/divergent 등 attention classification이 render에 surface (operator go/no-go 신호)
        assets = [
            R.AssetRehearsal(asset="usd-krw", coord_kind="decision", coord_label="retry",
                             gate_disposition=None, legacy_action="would_publish",
                             classification="atomic_defers", would_send=False, would_write=False),
            R.AssetRehearsal(asset="jpy-krw", coord_kind="outcome", coord_label="committed",
                             gate_disposition=None, legacy_action="skipped_disabled",
                             classification="would_publish_divergent", would_send=True, would_write=True),
            R.AssetRehearsal(asset="eur-krw", coord_kind="outcome", coord_label="totally_unknown",
                             gate_disposition=None, legacy_action="would_publish",
                             classification="review", would_send=False, would_write=False),
        ]
        report = R.RehearsalReport(
            gate_note="FORCED OPEN (진단)", snapshot_state="legacy_ready", snapshot_read_ok=False, assets=assets,
        )
        text = R.render_report(report)
        self.assertIn("⚠️ 확인 필요", text)
        self.assertIn("jpy-krw", text)        # divergent asset
        self.assertIn("eur-krw", text)        # review asset
        self.assertIn("would_publish_divergent", text)
        self.assertIn("review", text)

    def test_render_no_attention_clean(self):
        # 전부 expected/safe면 attention 라인 없음
        assets = [
            R.AssetRehearsal(asset="usd-krw", coord_kind="decision", coord_label="retry",
                             gate_disposition=None, legacy_action="would_publish",
                             classification="atomic_defers", would_send=False, would_write=False),
        ]
        report = R.RehearsalReport(
            gate_note="FORCED OPEN", snapshot_state="legacy_ready", snapshot_read_ok=False, assets=assets,
        )
        text = R.render_report(report)
        self.assertNotIn("⚠️ 확인 필요", text)


class TestReadOnlyClient(unittest.TestCase):
    """defense-in-depth: _ReadOnlyClient가 read는 위임, write 계열은 거부 (override 우회 backstop)."""

    def test_read_delegates(self):
        from unittest.mock import MagicMock
        inner = MagicMock()
        inner.get.return_value = b"v"
        c = R._ReadOnlyClient(inner)
        self.assertEqual(c.get("k"), b"v")
        inner.get.assert_called_once_with("k")

    def test_write_blocked(self):
        from unittest.mock import MagicMock
        c = R._ReadOnlyClient(MagicMock())
        # 우회형 execute_command/pipeline 포함 (codex LOW2)
        for op in ("set", "delete", "expire", "hset", "eval", "flushall", "execute_command", "pipeline"):
            with self.assertRaises(RuntimeError):
                getattr(c, op)("k", "v")


class TestScriptSafetyAST(unittest.TestCase):
    """#9: scripts/는 app/ dormancy trip-wire 범위 밖 → 이 파일 자체에 구조적 safety guard.

    rehearsal harness가 (a) publish_topic_detailed를 **호출**하지 않고 (b) BASE FxLiveCoordinatorAdapter를
    **직접 instantiate**하지 않음(no-op subclass _RehearsalAdapter만)을 AST로 잠금 — 미래 refactor가 real
    send/write 경로를 끌어들이면 이 테스트가 trip.
    """

    def _tree(self):
        import ast
        src = (Path(__file__).resolve().parent.parent / "scripts" / "rehearse_fx_cutover.py").read_text(
            encoding="utf-8")
        return ast.parse(src), ast

    def test_no_publish_topic_detailed_call(self):
        tree, ast = self._tree()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                self.assertNotEqual(name, "publish_topic_detailed",
                                    "rehearsal이 publish_topic_detailed를 호출 — real send 위반")

    def test_no_base_adapter_instantiation(self):
        tree, ast = self._tree()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                self.assertNotEqual(name, "FxLiveCoordinatorAdapter",
                                    "rehearsal이 BASE adapter를 직접 instantiate — no-op override 우회 위험")


if __name__ == "__main__":
    unittest.main()
