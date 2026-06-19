"""P1b C6-6 — FxLiveCoordinatorAdapter 단위 테스트 (live coordinator adapters, dormant).

dormancy(negative no-importer self-arm + construct-no-publish + route-unchanged) + same-read memo +
completeness gate(effective⊊present→MALFORMED→RETRY) + sync→async to_thread + 13 hook 정합 +
publisher topic 주입(Blocker 1) + write_outcomes injectable fail-closed(Blocker 2) + end-to-end COMMITTED.
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import atomic_fx_live as m
from app import config, topic_dispatcher
from app.atomic_build import BuildCompleteness
from app.atomic_coordinator import PublishOutcome, SendDisposition
from app.atomic_cutover import PublisherGateDisposition
from app.atomic_reconcile import DecisionAction
from app.atomic_fx_live import FxLiveCoordinatorAdapter
from app.atomic_fx_v2_loader import FxV2LoadResult
from app.atomic_value_schema import make_revision_key, parse_revision_key
from app.atomic_write_outcome import (
    RedisWritePerformed,
    RevisionAdvanced,
    WriteOutcome,
    WriteState,
)
from app.fx_topic_publisher import FX_TOPICS

_REV = make_revision_key(1_700_000_000_000_000, 5)


def _payload(bank_sources, *, asset="usd-krw", with_reference=False):
    banks = [{"source": s, "asset": asset, "rate": 1300.0 + i, "timestamp": "t"}
             for i, s in enumerate(bank_sources)]
    data = {"banks": banks}
    if with_reference:
        data["reference"] = {"source": "investing", "asset": asset, "rate": 1301.0, "timestamp": "t"}
    return {"type": "snapshot", "version": 1, "data": data}


def _load(present_revs, *, asset="usd-krw"):
    """present_revs: {source: revision_key|None}. None=v1/DB-fallback(payload엔 있되 effective 제외)."""
    bank_sources = [s for s in present_revs if s != "investing"]
    payload = _payload(bank_sources, asset=asset, with_reference=("investing" in present_revs))
    effective = {s: r for s, r in present_revs.items() if r is not None}
    present = tuple(sorted(present_revs.keys()))
    return FxV2LoadResult(payload=payload, effective_revision_vector=effective, present_sources=present)


class _FakeStoreClient:
    def __init__(self, *, raise_on=()):
        self.kv = {}
        self.raise_on = set(raise_on)

    def get(self, key):
        if "get" in self.raise_on:
            raise ConnectionError("redis down")
        return self.kv.get(key)

    def set(self, key, value):
        if "set" in self.raise_on:
            raise ConnectionError("redis down")
        self.kv[key] = value.encode("utf-8") if isinstance(value, str) else value
        return True


class _FakeSnap:
    def __init__(self, *, gate_open=True, session_id="s", generation=1):
        self.publisher_gate_open = gate_open
        self.bootstrap_session_id = session_id
        self.bootstrap_generation = generation


class _RevRow:
    def __init__(self, source, revision):
        self.source = source
        self.revision = revision


def _adapter(*, load=None, client=None, snap=None, banks=None, investing=None):
    return FxLiveCoordinatorAdapter(
        db=object(),
        client=client if client is not None else _FakeStoreClient(),
        loader=(lambda db, asset: load) if load is not None else (lambda db, asset: _load({"kb": _REV})),
        snapshot_fn=(lambda: snap) if snap is not None else (lambda: _FakeSnap()),
        bank_revisions_selector=(lambda db, asset: banks if banks is not None else []),
        investing_revision_selector=(lambda db, asset: investing),
    )


def _candidate_provider():
    # explicit all-PUBLISH_CANDIDATE (happy-path 테스트용 — fail-closed default 대체)
    rev = parse_revision_key(_REV)
    async def _provide(asset, build_result):
        return {
            src: WriteOutcome(WriteState.REFRESHED_EQUAL, rev, rev,
                              RedisWritePerformed.APPLIED, RevisionAdvanced.NO)
            for src in build_result.present_sources
        }
    return _provide


class TestSameReadAndBuild(unittest.IsolatedAsyncioTestCase):

    async def test_build_fn_full_v2_complete(self):
        ad = _adapter(load=_load({"kb": _REV, "investing": _REV}))
        br = await ad._build_fn("usd-krw")
        self.assertEqual(br.asset, "usd-krw")
        self.assertEqual(set(br.present_sources), {"kb", "investing"})
        self.assertIsNotNone(br.payload)

    async def test_same_read_single_loader_call(self):
        calls = {"n": 0}
        load = _load({"kb": _REV})
        def counting_loader(db, asset):
            calls["n"] += 1
            return load
        ad = FxLiveCoordinatorAdapter(db=object(), client=_FakeStoreClient(),
                                      loader=counting_loader, snapshot_fn=lambda: _FakeSnap(),
                                      bank_revisions_selector=lambda d, a: [], investing_revision_selector=lambda d, a: None)
        br = await ad._build_fn("usd-krw")
        eff = await ad._effective_resolver("usd-krw", br)
        self.assertEqual(calls["n"], 1)              # loader 1회 (effective_resolver는 memo)
        self.assertEqual(set(eff), set(br.present_sources))

    async def test_effective_resolver_missing_slot_raises(self):
        other = _adapter(load=_load({"kb": _REV, "investing": _REV}))
        br = await other._build_fn("usd-krw")   # valid non-MALFORMED BuildResult (other의 memo)
        ad = _adapter()                          # THIS adapter memo 비어있음
        with self.assertRaises(ValueError):
            await ad._effective_resolver("usd-krw", br)

    async def test_effective_subset_routes_malformed(self):
        # effective ⊊ present (kb는 v1=None) → MALFORMED
        ad = _adapter(load=_load({"kb": None, "investing": _REV}))
        br = await ad._build_fn("usd-krw")
        self.assertIs(br.completeness, BuildCompleteness.MALFORMED)
        self.assertIsNone(br.payload)
        self.assertIn("migration incomplete", br.build_error)
        # MALFORMED → effective_resolver {} (coverage)
        self.assertEqual(await ad._effective_resolver("usd-krw", br), {})

    async def test_effective_equals_present_not_malformed(self):
        ad = _adapter(load=_load({"kb": _REV, "investing": _REV}))
        br = await ad._build_fn("usd-krw")
        self.assertIsNot(br.completeness, BuildCompleteness.MALFORMED)
        eff = await ad._effective_resolver("usd-krw", br)
        self.assertEqual(set(eff), set(br.present_sources))

    async def test_build_fn_absorbs_loader_exception_to_malformed(self):
        # HIGH(Workflow): loader/DB 예외 → MALFORMED (build_fx_result/legacy per-asset 격리 정합, raise 안 함)
        def boom_loader(db, asset):
            raise RuntimeError("RDS failover")
        ad = FxLiveCoordinatorAdapter(db=object(), client=_FakeStoreClient(), loader=boom_loader,
                                      snapshot_fn=lambda: _FakeSnap(),
                                      bank_revisions_selector=lambda d, a: [], investing_revision_selector=lambda d, a: None)
        br = await ad._build_fn("usd-krw")
        self.assertIs(br.completeness, BuildCompleteness.MALFORMED)
        self.assertIn("build exception", br.build_error)


class TestHooks(unittest.IsolatedAsyncioTestCase):

    async def test_watermark_read_outage_propagates(self):
        ad = _adapter(client=_FakeStoreClient(raise_on=("get",)))
        with self.assertRaises(ConnectionError):
            await ad._watermark_reader("usd-krw")   # to_thread re-raises (pre-send)

    async def test_watermark_write_outage_returns_false(self):
        from app.atomic_watermark import Watermark
        ad = _adapter(client=_FakeStoreClient(raise_on=("set",)))
        wm = Watermark(asset="usd-krw", lineage_id="s:1", publish_sequence=1, membership_version=1,
                       present_revision_vector={"kb": _REV}, missing_sources=(), sent_at="t")
        with self.assertLogs("exchange_rate.atomic_watermark_store", level="WARNING"):
            self.assertFalse(await ad._watermark_writer("usd-krw", wm))

    async def test_gate_open_pass_through(self):
        ad = _adapter(snap=_FakeSnap(gate_open=True))
        self.assertIs(await ad._gate_fn("usd-krw"), PublisherGateDisposition.PASS_THROUGH)

    async def test_gate_closed_would_block(self):
        ad = _adapter(snap=_FakeSnap(gate_open=False))
        self.assertIs(await ad._gate_fn("usd-krw"), PublisherGateDisposition.WOULD_BLOCK_DRY_RUN)

    async def test_lineage_from_snapshot(self):
        ad = _adapter(snap=_FakeSnap(session_id="sess", generation=7))
        self.assertEqual(await ad._lineage_provider("usd-krw", None, None), "sess:7")

    async def test_lineage_none_session_sentinel(self):
        ad = _adapter(snap=_FakeSnap(session_id=None, generation=3))
        self.assertEqual(await ad._lineage_provider("usd-krw", None, None), "bootstrap:3")

    async def test_feature_flags_order(self):
        ad = _adapter()
        with patch.object(config, "FX_TOPIC_ENABLED", True), patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            self.assertEqual(await ad._feature_flags("usd-krw"), (True, False))

    async def test_db_revisions_folds_sources(self):
        ad = _adapter(banks=[_RevRow("kb", (1, 10)), _RevRow("hana", (2, 20))],
                      investing=_RevRow("investing", (3, 30)))
        revs = await ad._db_revisions_reader("usd-krw")
        self.assertEqual(revs, {"kb": (1, 10), "hana": (2, 20), "investing": (3, 30)})

    async def test_db_revisions_no_investing(self):
        ad = _adapter(banks=[_RevRow("kb", (1, 10))], investing=None)
        self.assertEqual(await ad._db_revisions_reader("usd-krw"), {"kb": (1, 10)})

    async def test_publisher_injects_topic_and_maps(self):
        # Blocker 1: payload['topic'] 주입 + publish_topic_detailed await(async) + SendResult 매핑
        ad = _adapter()
        captured = {}
        async def fake_detailed(topic, payload):
            captured["topic"] = topic
            captured["payload"] = payload
            from app.topic_dispatcher import TopicSendCounts
            return TopicSendCounts(attempted=2, sent=2, enabled=True)
        with patch.object(topic_dispatcher, "publish_topic_detailed", fake_detailed):
            res = await ad._publisher_fn("usd-krw", {"type": "snapshot", "data": {}})
        self.assertEqual(captured["topic"], FX_TOPICS["usd-krw"])
        self.assertEqual(captured["payload"]["topic"], FX_TOPICS["usd-krw"])   # 주입됨
        self.assertIs(res.disposition, SendDisposition.SENT)
        self.assertEqual(res.sent_count, 2)

    async def test_publisher_does_not_mutate_input(self):
        ad = _adapter()
        payload = {"type": "snapshot", "data": {}}
        async def fake_detailed(topic, p):
            from app.topic_dispatcher import TopicSendCounts
            return TopicSendCounts(attempted=0, sent=0, enabled=True)
        with patch.object(topic_dispatcher, "publish_topic_detailed", fake_detailed):
            await ad._publisher_fn("usd-krw", payload)
        self.assertNotIn("topic", payload)   # 원본 불변 (dict copy)

    async def test_fail_closed_write_outcomes_retry(self):
        # Blocker 2 default: 모든 present → FAILED non-structural → candidate_disposition RETRY
        from app.atomic_write_outcome import candidate_disposition, CandidateDisposition
        ad = _adapter(load=_load({"kb": _REV, "investing": _REV}))
        br = await ad._build_fn("usd-krw")
        wo = await ad._fail_closed_write_outcomes("usd-krw", br)
        self.assertEqual(set(wo), set(br.present_sources))
        for o in wo.values():
            self.assertIs(o.state, WriteState.FAILED)
            self.assertFalse(o.structural)
            self.assertIs(candidate_disposition(o), CandidateDisposition.RETRY)


class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    """build_coordinator로 전 hook 조립 → publish_asset 실행 (fail-closed default vs explicit candidate)."""

    async def asyncSetUp(self):
        self._orig = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._orig

    async def test_default_fail_closed_retries_no_publish(self):
        ws = MagicMock(); ws.send_json = AsyncMock()
        topic_dispatcher.registry.register(ws, [FX_TOPICS["usd-krw"]])
        ad = _adapter(load=_load({"kb": _REV, "investing": _REV}))
        coord = ad.build_coordinator()   # default fail-closed
        with patch.object(config, "FX_TOPIC_ENABLED", True), patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            res = await coord.publish_asset("usd-krw")
        # fail-closed → RETRY → 발행 안 함
        self.assertIsNotNone(res.decision)
        ws.send_json.assert_not_called()

    async def test_explicit_candidate_publishes_committed(self):
        ws = MagicMock(); ws.send_json = AsyncMock()
        topic_dispatcher.registry.register(ws, [FX_TOPICS["usd-krw"]])
        ad = _adapter(load=_load({"kb": _REV, "investing": _REV}))
        coord = ad.build_coordinator(write_outcomes_provider=_candidate_provider())
        with patch.object(config, "FX_TOPIC_ENABLED", True), patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            res = await coord.publish_asset("usd-krw")
        self.assertIs(res.outcome, PublishOutcome.COMMITTED)
        ws.send_json.assert_awaited_once()

    async def test_loader_exception_retries_not_propagates(self):
        # HIGH(Workflow) e2e: build-stage 예외가 publish_asset 밖으로 propagate 안 하고 RETRY decision
        ws = MagicMock(); ws.send_json = AsyncMock()
        topic_dispatcher.registry.register(ws, [FX_TOPICS["usd-krw"]])
        def boom_loader(db, asset):
            raise RuntimeError("RDS failover")
        ad = FxLiveCoordinatorAdapter(db=object(), client=_FakeStoreClient(), loader=boom_loader,
                                      snapshot_fn=lambda: _FakeSnap(),
                                      bank_revisions_selector=lambda d, a: [], investing_revision_selector=lambda d, a: None)
        coord = ad.build_coordinator(write_outcomes_provider=_candidate_provider())
        with patch.object(config, "FX_TOPIC_ENABLED", True), patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            res = await coord.publish_asset("usd-krw")   # raise 안 함 (여기 도달 = no propagation)
        self.assertIs(res.decision.action, DecisionAction.RETRY)
        ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# Dormancy — negative-only (live-bridge: island+live 둘 다 import → positive trip-wire 없음)
# ---------------------------------------------------------------------------

def _imports_module(src, target):
    """src가 target 모듈을 import하는지 — 3 form 전수 검출(공유 predicate, real test+self-arm 동일 경로):
    `from app.<target> import X` / `import app.<target>` / `from app import <target>`(idiomatic, MED6 blind
    spot — test_atomic_coordinator.py:765 패턴). 위반 목록 반환(빈 list = clean)."""
    hits = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            if node.module and target in node.module.split("."):
                hits.append(f"from {node.module} import")
            if node.module == "app" and any(a.name == target for a in node.names):
                hits.append(f"from app import {target}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if target in a.name.split("."):
                    hits.append(f"import {a.name}")
    return hits


class TestDormancy(unittest.TestCase):

    def test_importable(self):
        self.assertTrue(hasattr(m, "FxLiveCoordinatorAdapter"))

    def test_no_live_module_imports_atomic_fx_live(self):
        # negative-only dormancy + C6-4 transitive chain(publish_topic_detailed←atomic_fx_live←nothing)의
        # 유일 enforcement — 3 form 전수 검출
        app_dir = pathlib.Path(m.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name == "atomic_fx_live.py":
                continue
            hits = _imports_module(py.read_text(encoding="utf-8"), "atomic_fx_live")
            self.assertEqual(hits, [], f"{py.name}: atomic_fx_live import — dormant 위반: {hits}")

    def test_no_importer_scan_self_arms(self):
        # real test와 동일 predicate(_imports_module)를 3 form 전수 plant로 검증 (tautology 회피)
        for planted in (
            "from app.atomic_fx_live import FxLiveCoordinatorAdapter\n",
            "import app.atomic_fx_live\n",
            "from app import atomic_fx_live\n",
        ):
            self.assertTrue(_imports_module(planted, "atomic_fx_live"),
                            f"planted importer 미검출: {planted!r}")

    def test_route_unchanged(self):
        # legacy _publish_fx_snapshot는 여전히 load_and_build_fx_topic_payload + publish_topic 사용
        # (coordinator/atomic_fx_live 미사용) — C6-6이 live caller 추가 안 함
        src = pathlib.Path(m.__file__).resolve().parent.joinpath("fx_topic_publisher.py").read_text(encoding="utf-8")
        self.assertIn("load_and_build_fx_topic_payload", src)
        self.assertIn("publish_topic(", src)            # legacy byte-identical send path 유지(C7까지)
        self.assertNotIn("publish_topic_detailed", src)  # C6-4 detailed는 C7 전까지 legacy 미사용
        self.assertNotIn("atomic_fx_live", src)
        self.assertNotIn("AtomicFxCoordinator", src)

    def test_no_scheduling(self):
        src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"adapter에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
