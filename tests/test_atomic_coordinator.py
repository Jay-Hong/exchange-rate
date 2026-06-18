"""P1b B2b-4a — coordinator pure helpers 단위 테스트 (§19 B2b-4, dormant pure).

write_action_for_relation exhaustive 매핑(WatermarkRelation 전수 — 추가 시 깨짐) + materialize_watermark
(content 보존 / lineage·seq·sent_at만 교체 / input placeholder 확인 / output sentinel 차단) + dormancy.
no async/IO (4a). coordinator shell(async)·PublishResult는 B2b-4b.
"""
from __future__ import annotations

import pathlib
import unittest

from app import atomic_coordinator as ac
from app.atomic_coordinator import (
    PublishOutcome,
    WriteAction,
    materialize_watermark,
    write_action_for_relation,
)
from app.atomic_reconcile import (
    CANDIDATE_PLACEHOLDER_LINEAGE,
    CANDIDATE_PLACEHOLDER_SENT_AT,
    CANDIDATE_PLACEHOLDER_SEQ,
)
from app.atomic_value_schema import make_revision_key
from app.atomic_watermark import Watermark, WatermarkRelation
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
        import ast

        app_dir = pathlib.Path(ac.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._DORMANT_MODULES:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_coordinator" in node.module.split("."):
                        self.fail(f"{rel}: from atomic_coordinator import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_coordinator" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_coordinator — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_coordinator" in a.name.split("."):
                            self.fail(f"{rel}: import atomic_coordinator — dormant 위반")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in self._CALL_NEEDLES:
                        self.fail(f"{rel}: {name}() 호출 — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "atomic_coordinator" in node.value or "materialize_watermark" in node.value:
                        self.fail(f"{rel}: 문자열 '{node.value}'에 atomic_coordinator — dynamic 호출 의심")


if __name__ == "__main__":
    unittest.main()
