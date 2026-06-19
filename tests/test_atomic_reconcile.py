"""P1b B2b-1 — DERIVED pending detection 단위 테스트 (§19:347, pure dormant).

derive_pending 결정표 전수(5 reason) + None watermark + present>DB + STRUCTURAL + dormancy.
detection-only 계약 확인(출력은 reason 분류만; publish/effective/watermark write/subscriber 판단 없음).
"""
from __future__ import annotations

import pathlib
import unittest

from app import atomic_reconcile as ar
from app.atomic_build import BuildCompleteness, BuildResult
from app.atomic_reconcile import (
    B2bDecision,
    DecisionAction,
    PendingReason,
    decide_and_build_next,
    derive_pending,
)
from app.atomic_value_schema import make_revision_key
from app.atomic_watermark import Watermark
from app.atomic_write_outcome import FailureKind, write_outcome_from_lua
from app.fx_membership import FX_MEMBERSHIP_SOURCES, FX_MEMBERSHIP_VERSION

_REV_LO = (1_700_000_000_000_000, 10)
_REV_HI = (1_700_000_000_500_000, 99)


def _wm(present_vector, *, missing=()):
    # present에 없는 membership source는 missing으로 (partition 강제 — B1 __post_init__).
    present_keys = set(present_vector)
    miss = tuple(sorted((set(FX_MEMBERSHIP_SOURCES) - present_keys) if not missing else missing))
    return Watermark(
        asset="usd-krw", lineage_id="sess-1:1", publish_sequence=1, membership_version=1,
        present_revision_vector=dict(present_vector), missing_sources=miss,
        sent_at="2026-06-18T09:00:00+09:00",
    )


class TestDerivePending(unittest.TestCase):

    def test_all_membership_covered(self):
        out = derive_pending({}, None)
        self.assertEqual(set(out), set(FX_MEMBERSHIP_SOURCES))

    def test_db_absent_not_pending(self):
        # db_revisions 빈값 → 전부 DB-absent → not pending
        out = derive_pending({}, None)
        self.assertTrue(all(r is PendingReason.NOT_PENDING_DB_ABSENT for r in out.values()))

    def test_not_in_present_pending(self):
        # DB에 rev 있고 watermark None(present 전부 부재) → PENDING_NOT_IN_PRESENT
        out = derive_pending({"kb": _REV_LO}, None)
        self.assertEqual(out["kb"], PendingReason.PENDING_NOT_IN_PRESENT)

    def test_db_ahead_pending(self):
        wm = _wm({"kb": make_revision_key(*_REV_LO)})
        out = derive_pending({"kb": _REV_HI}, wm)  # DB(_REV_HI) > present(_REV_LO)
        self.assertEqual(out["kb"], PendingReason.PENDING_DB_AHEAD)

    def test_current_not_pending(self):
        wm = _wm({"kb": make_revision_key(*_REV_HI)})
        out = derive_pending({"kb": _REV_HI}, wm)  # DB == present
        self.assertEqual(out["kb"], PendingReason.NOT_PENDING_CURRENT)

    def test_watermark_ahead_not_pending(self):
        # present > DB (watermark가 앞섬) → NOT_PENDING_CURRENT (DB 앞설 때만 pending)
        wm = _wm({"kb": make_revision_key(*_REV_HI)})
        out = derive_pending({"kb": _REV_LO}, wm)
        self.assertEqual(out["kb"], PendingReason.NOT_PENDING_CURRENT)

    def test_structural_on_bad_present_key(self):
        # watermark는 __post_init__이 vector 형식 강제하므로, 깨진 key를 직접 dict로 주입(파싱 실패 경로 검증)
        class _FakeWm:
            present_revision_vector = {"kb": "not-a-revision-key"}
        out = derive_pending({"kb": _REV_HI}, _FakeWm())
        self.assertEqual(out["kb"], PendingReason.STRUCTURAL)

    def test_mixed(self):
        wm = _wm({"kb": make_revision_key(*_REV_LO), "hana": make_revision_key(*_REV_HI)})
        out = derive_pending({"kb": _REV_HI, "hana": _REV_HI, "shinhan": _REV_LO}, wm)
        self.assertEqual(out["kb"], PendingReason.PENDING_DB_AHEAD)       # DB > present
        self.assertEqual(out["hana"], PendingReason.NOT_PENDING_CURRENT)  # DB == present
        self.assertEqual(out["shinhan"], PendingReason.PENDING_NOT_IN_PRESENT)  # not in present
        self.assertEqual(out["citi"], PendingReason.NOT_PENDING_DB_ABSENT)      # DB 부재


_ALL_BANKS = ["kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi"]
_WO_REV = (1_700_000_000_000_000, 1)


def _build_result(*, bank_sources=None, reference=True, malformed=False, asset="usd-krw"):
    if malformed:
        return BuildResult(asset=asset, payload=None, present_sources=(), missing_sources=(),
                           membership_version=FX_MEMBERSHIP_VERSION,
                           completeness=BuildCompleteness.MALFORMED, build_error="boom")
    bank_sources = _ALL_BANKS if bank_sources is None else bank_sources
    banks = [{"source": s, "asset": asset, "rate": 1300.0, "timestamp": "2026-06-18T09:00:00+09:00"}
             for s in bank_sources]
    data = {"banks": banks}
    present = set(bank_sources)
    if reference:
        data["reference"] = {"source": "investing", "asset": asset, "rate": 1301.0,
                             "timestamp": "2026-06-18T09:00:00+09:00"}
        present.add("investing")
    payload = {"type": "snapshot", "version": 1, "data": data}
    missing = tuple(sorted(FX_MEMBERSHIP_SOURCES - present))
    comp = BuildCompleteness.COMPLETE if not missing else BuildCompleteness.PARTIAL
    return BuildResult(asset=asset, payload=payload, present_sources=tuple(sorted(present)),
                       missing_sources=missing, membership_version=FX_MEMBERSHIP_VERSION,
                       completeness=comp, build_error=None)


def _eff(present_sources):
    return {s: make_revision_key(1_700_000_000_000_000 + i, i + 1) for i, s in enumerate(sorted(present_sources))}


def _wo(lua, **kw):
    return write_outcome_from_lua(lua, _WO_REV, **kw)


class TestDecideAndBuildNext(unittest.TestCase):

    def _publish_inputs(self, **over):
        br = over.get("build_result", _build_result())
        present = br.present_sources
        base = dict(
            last_watermark=None,
            build_result=br,
            effective_revision_vector=_eff(present),
            write_outcomes={s: _wo("advance") for s in present},
            subscriber_count=3,
        )
        base.update(over)
        return base

    def test_publish(self):
        d = decide_and_build_next(**self._publish_inputs())
        self.assertEqual(d.action, DecisionAction.PUBLISH)
        self.assertIsNotNone(d.watermark_candidate)

    def test_publish_candidate_has_placeholders(self):
        # placeholder lineage/seq/sent_at — B2b-4가 교체, write/classify에 그대로 쓰면 안 됨
        d = decide_and_build_next(**self._publish_inputs())
        self.assertEqual(d.watermark_candidate.lineage_id, "__candidate__")
        self.assertEqual(d.watermark_candidate.publish_sequence, 0)
        self.assertEqual(d.watermark_candidate.sent_at, "__candidate__")

    def test_candidate_partition(self):
        # candidate present(effective keys) ∪ missing(build.missing) == membership
        br = _build_result(bank_sources=["kb", "hana"])  # PARTIAL
        d = decide_and_build_next(**self._publish_inputs(build_result=br))
        cand = d.watermark_candidate
        self.assertEqual(set(cand.present_revision_vector) | set(cand.missing_sources), set(FX_MEMBERSHIP_SOURCES))

    def test_block_on_conflict(self):
        d = decide_and_build_next(**self._publish_inputs(
            write_outcomes={**{s: _wo("advance") for s in _build_result().present_sources}, "kb": _wo("conflict")}))
        self.assertEqual(d.action, DecisionAction.BLOCK)
        self.assertIsNone(d.watermark_candidate)

    def test_block_precedence_over_subscriber_zero(self):
        # conflict + subscriber 0 → BLOCK (conflict 안 가려짐)
        present = _build_result().present_sources
        d = decide_and_build_next(**self._publish_inputs(
            write_outcomes={**{s: _wo("advance") for s in present}, "kb": _wo("conflict")},
            subscriber_count=0))
        self.assertEqual(d.action, DecisionAction.BLOCK)

    def test_retry_on_malformed(self):
        d = decide_and_build_next(last_watermark=None, build_result=_build_result(malformed=True),
                                  effective_revision_vector={}, write_outcomes={}, subscriber_count=3)
        self.assertEqual(d.action, DecisionAction.RETRY)

    def test_retry_on_general_failed(self):
        present = _build_result().present_sources
        d = decide_and_build_next(**self._publish_inputs(
            write_outcomes={**{s: _wo("advance") for s in present},
                            "kb": _wo(None, failure_kind=FailureKind.DEFINITE_NOT_APPLIED)}))
        self.assertEqual(d.action, DecisionAction.RETRY)

    def test_skip_subscriber_zero(self):
        d = decide_and_build_next(**self._publish_inputs(subscriber_count=0))
        self.assertEqual(d.action, DecisionAction.SKIP_SUBSCRIBER_ZERO)
        self.assertIsNone(d.watermark_candidate)

    def test_skip_dedup_identical(self):
        br = _build_result()
        eff = _eff(br.present_sources)
        last = Watermark(asset="usd-krw", lineage_id="sess-1:1", publish_sequence=5,
                         membership_version=FX_MEMBERSHIP_VERSION, present_revision_vector=eff,
                         missing_sources=br.missing_sources, sent_at="2026-06-18T08:00:00+09:00")
        d = decide_and_build_next(last_watermark=last, build_result=br, effective_revision_vector=eff,
                                  write_outcomes={s: _wo("advance") for s in br.present_sources}, subscriber_count=3)
        self.assertEqual(d.action, DecisionAction.SKIP_DEDUP_IDENTICAL)

    def test_publish_when_last_differs(self):
        br = _build_result()
        eff = _eff(br.present_sources)
        diff = dict(eff); diff["kb"] = make_revision_key(1_799_999_999_999_999, 999)  # 다른 revision
        last = Watermark(asset="usd-krw", lineage_id="sess-1:1", publish_sequence=5,
                         membership_version=FX_MEMBERSHIP_VERSION, present_revision_vector=diff,
                         missing_sources=br.missing_sources, sent_at="2026-06-18T08:00:00+09:00")
        d = decide_and_build_next(last_watermark=last, build_result=br, effective_revision_vector=eff,
                                  write_outcomes={s: _wo("advance") for s in br.present_sources}, subscriber_count=3)
        self.assertEqual(d.action, DecisionAction.PUBLISH)

    def test_exact_key_guard(self):
        # effective_vector keys != present_sources → ValueError (skipped_newer None / drift 노출)
        br = _build_result()
        eff = _eff(br.present_sources)
        del eff["kb"]  # present인데 effective 누락
        with self.assertRaises(ValueError):
            decide_and_build_next(last_watermark=None, build_result=br, effective_revision_vector=eff,
                                  write_outcomes={s: _wo("advance") for s in br.present_sources}, subscriber_count=3)

    def test_asset_mismatch_guard(self):
        last = Watermark(asset="jpy-krw", lineage_id="x:1", publish_sequence=1,
                         membership_version=FX_MEMBERSHIP_VERSION,
                         present_revision_vector={}, missing_sources=tuple(sorted(FX_MEMBERSHIP_SOURCES)),
                         sent_at="t")
        with self.assertRaises(ValueError):
            decide_and_build_next(**self._publish_inputs(last_watermark=last))

    def test_subscriber_count_negative_guard(self):
        with self.assertRaises(ValueError):
            decide_and_build_next(**self._publish_inputs(subscriber_count=-1))

    def test_write_outcomes_dropped_source_guard(self):
        # codex P1: present source가 write_outcomes에서 누락 → ValueError (source-drop 차단).
        # 누락된 게 conflict였다면 BLOCK 우회 + silent PUBLISH가 되므로 coverage guard로 fail-closed.
        inputs = self._publish_inputs()
        wo = dict(inputs["write_outcomes"])
        del wo["kb"]
        inputs["write_outcomes"] = wo
        with self.assertRaises(ValueError):
            decide_and_build_next(**inputs)

    def test_write_outcomes_extra_source_guard(self):
        inputs = self._publish_inputs()
        wo = dict(inputs["write_outcomes"])
        wo["ghost"] = _wo("advance")  # present에 없는 source
        inputs["write_outcomes"] = wo
        with self.assertRaises(ValueError):
            decide_and_build_next(**inputs)

    def test_malformed_before_coverage_guard(self):
        # MALFORMED는 coverage guard보다 먼저 — write_outcomes 비어있지 않아도(build만 실패) RETRY
        d = decide_and_build_next(
            last_watermark=None, build_result=_build_result(malformed=True),
            effective_revision_vector={}, write_outcomes={"kb": _wo("advance")}, subscriber_count=3)
        self.assertEqual(d.action, DecisionAction.RETRY)

    def test_block_precedence_over_malformed(self):
        # codex: conflict/structural는 build MALFORMED보다 우선(구조 corruption alert가 더 보수적)
        d = decide_and_build_next(
            last_watermark=None, build_result=_build_result(malformed=True),
            effective_revision_vector={}, write_outcomes={"kb": _wo("conflict")}, subscriber_count=3)
        self.assertEqual(d.action, DecisionAction.BLOCK)

    def test_non_malformed_extra_conflict_is_valueerror_not_block(self):
        # codex: non-MALFORMED에서 extra-source(라우팅 버그) conflict는 coverage가 BLOCK보다 먼저라
        # spurious BLOCK이 아니라 ValueError로 노출돼야 함(exact precondition).
        inputs = self._publish_inputs()
        wo = dict(inputs["write_outcomes"])
        wo["ghost"] = _wo("conflict")  # present에 없는 source의 conflict
        inputs["write_outcomes"] = wo
        with self.assertRaises(ValueError):
            decide_and_build_next(**inputs)


class TestDormancy(unittest.TestCase):
    """B2b-1 dormant — app/ 어떤 live 모듈도 atomic_reconcile import/derive_pending 호출 0.
    atomic_watermark/atomic_value_schema(island) import → dormant set 멤버(allowlist skip)."""

    _DORMANT_MODULES = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
        "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
        "atomic_build.py", "atomic_reconcile.py", "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py", "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py",
    })
    _CALL_NEEDLES = ("derive_pending", "decide_and_build_next")

    def test_no_live_module_uses_atomic_reconcile(self):
        import ast

        app_dir = pathlib.Path(ar.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._DORMANT_MODULES:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_reconcile" in node.module.split("."):
                        self.fail(f"{rel}: from atomic_reconcile import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_reconcile" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_reconcile — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_reconcile" in a.name.split("."):
                            self.fail(f"{rel}: import atomic_reconcile — dormant 위반")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in self._CALL_NEEDLES:
                        self.fail(f"{rel}: {name}() 호출 — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "atomic_reconcile" in node.value or "derive_pending" in node.value:
                        self.fail(f"{rel}: 문자열 '{node.value}'에 atomic_reconcile — dynamic 호출 의심")


if __name__ == "__main__":
    unittest.main()
