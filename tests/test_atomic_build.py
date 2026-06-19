"""P1b B2a — FX BuildResult + build_fx_result 단위 테스트 (§19 B2a, dormant).

build_fx_result(complete/partial/malformed, present/missing 정확, payload schema-v1, **revision vector
부재**) + BuildResult __post_init__ invariant + dormancy(app/ live 모듈 import 0). load_and_build_fx_topic_payload는
patch(B2a 로직만 검증, loader는 fx_topic_payload 책임). I/O·arbitration·seq 없음 검증.
"""
from __future__ import annotations

import pathlib
import unittest
from unittest.mock import patch

from app import atomic_build as ab
from app.atomic_build import BuildCompleteness, BuildResult, build_fx_result
from app.fx_membership import FX_MEMBERSHIP_SOURCES, FX_MEMBERSHIP_VERSION

_ALL_BANKS = ["kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi"]


def _payload(asset, bank_sources, *, reference=True):
    banks = [{"source": s, "asset": asset, "rate": 1300.0, "timestamp": "2026-06-18T09:00:00+09:00"}
             for s in bank_sources]
    data = {"banks": banks}
    if reference:
        data["reference"] = {"source": "investing", "asset": asset, "rate": 1301.0,
                             "timestamp": "2026-06-18T09:00:00+09:00"}
    return {"type": "snapshot", "version": 1, "data": data}


class TestBuildFxResult(unittest.TestCase):

    def _run(self, payload):
        with patch("app.atomic_build.load_and_build_fx_topic_payload", return_value=payload):
            return build_fx_result(db=None, asset="usd-krw")

    def test_complete(self):
        r = self._run(_payload("usd-krw", _ALL_BANKS, reference=True))
        self.assertEqual(r.completeness, BuildCompleteness.COMPLETE)
        self.assertEqual(set(r.present_sources), FX_MEMBERSHIP_SOURCES)
        self.assertEqual(r.missing_sources, ())
        self.assertIsNotNone(r.payload)
        self.assertIsNone(r.build_error)
        self.assertEqual(r.membership_version, FX_MEMBERSHIP_VERSION)

    def test_partial_missing_banks(self):
        r = self._run(_payload("usd-krw", ["kb", "hana"], reference=True))
        self.assertEqual(r.completeness, BuildCompleteness.PARTIAL)
        self.assertEqual(set(r.present_sources), {"kb", "hana", "investing"})
        # missing = membership − present (정렬)
        self.assertEqual(set(r.missing_sources), FX_MEMBERSHIP_SOURCES - {"kb", "hana", "investing"})
        self.assertEqual(r.missing_sources, tuple(sorted(r.missing_sources)))

    def test_partial_missing_reference(self):
        r = self._run(_payload("usd-krw", _ALL_BANKS, reference=False))
        self.assertEqual(r.completeness, BuildCompleteness.PARTIAL)
        self.assertNotIn("investing", r.present_sources)
        self.assertIn("investing", r.missing_sources)

    def test_all_missing_still_partial(self):
        # 전 source missing(빈 build) — 정상 발행 가능 PARTIAL (MALFORMED 아님)
        r = self._run(_payload("usd-krw", [], reference=False))
        self.assertEqual(r.completeness, BuildCompleteness.PARTIAL)
        self.assertEqual(r.present_sources, ())
        self.assertEqual(set(r.missing_sources), FX_MEMBERSHIP_SOURCES)

    def test_no_revision_vector_field(self):
        # B2a는 revision vector를 만들지 않음 (B2b 전담)
        r = self._run(_payload("usd-krw", _ALL_BANKS))
        for forbidden in ("present_revision_vector", "revision_vector", "revision_vector_status"):
            self.assertFalse(hasattr(r, forbidden), forbidden)

    def test_malformed_on_loader_exception(self):
        with patch("app.atomic_build.load_and_build_fx_topic_payload", side_effect=RuntimeError("redis down")):
            r = build_fx_result(db=None, asset="usd-krw")
        self.assertEqual(r.completeness, BuildCompleteness.MALFORMED)
        self.assertIsNone(r.payload)
        self.assertIn("redis down", r.build_error)
        self.assertEqual(r.present_sources, ())
        self.assertEqual(r.missing_sources, ())

    def test_malformed_on_bad_payload_structure(self):
        # payload 구조 불량(data 키 부재) → _extract_present_sources KeyError → MALFORMED
        with patch("app.atomic_build.load_and_build_fx_topic_payload", return_value={"type": "snapshot"}):
            r = build_fx_result(db=None, asset="usd-krw")
        self.assertEqual(r.completeness, BuildCompleteness.MALFORMED)
        self.assertIsNone(r.payload)

    def test_invalid_asset_raises(self):
        # invalid asset = 호출 계약 위반 → ValueError (MALFORMED 아님)
        with self.assertRaises(ValueError):
            build_fx_result(db=None, asset="usdt-krw")


class TestBuildResultInvariants(unittest.TestCase):
    """직접 BuildResult(...) 생성도 __post_init__ 강제 (codex #7 Optional payload + #4 payload 일치)."""

    def _complete(self, **over):
        base = dict(
            asset="usd-krw",
            payload=_payload("usd-krw", _ALL_BANKS, reference=True),
            present_sources=tuple(sorted(FX_MEMBERSHIP_SOURCES)),
            missing_sources=(),
            membership_version=FX_MEMBERSHIP_VERSION,
            completeness=BuildCompleteness.COMPLETE,
            build_error=None,
        )
        base.update(over)
        return BuildResult(**base)

    def test_complete_ok(self):
        self.assertEqual(self._complete().completeness, BuildCompleteness.COMPLETE)

    def test_malformed_with_payload_raises(self):
        with self.assertRaises(ValueError):
            BuildResult(asset="usd-krw", payload=_payload("usd-krw", _ALL_BANKS),
                        present_sources=(), missing_sources=(), membership_version=1,
                        completeness=BuildCompleteness.MALFORMED, build_error="x")

    def test_malformed_without_error_raises(self):
        with self.assertRaises(ValueError):
            BuildResult(asset="usd-krw", payload=None, present_sources=(), missing_sources=(),
                        membership_version=1, completeness=BuildCompleteness.MALFORMED, build_error=None)

    def test_malformed_ok(self):
        r = BuildResult(asset="usd-krw", payload=None, present_sources=(), missing_sources=(),
                        membership_version=1, completeness=BuildCompleteness.MALFORMED, build_error="boom")
        self.assertIsNone(r.payload)

    def test_complete_but_missing_nonempty_raises(self):
        with self.assertRaises(ValueError):
            self._complete(missing_sources=("citi",))

    def test_present_missing_overlap_raises(self):
        with self.assertRaises(ValueError):
            self._complete(completeness=BuildCompleteness.PARTIAL,
                           present_sources=("kb",), missing_sources=("kb",))

    def test_present_not_subset_raises(self):
        with self.assertRaises(ValueError):
            self._complete(present_sources=tuple(sorted(FX_MEMBERSHIP_SOURCES)) + ("ghost",))

    def test_present_mismatch_payload_raises(self):
        # present_sources가 payload 실제 source와 불일치 (codex #4)
        with self.assertRaises(ValueError):
            self._complete(
                payload=_payload("usd-krw", ["kb", "hana"], reference=True),
                present_sources=tuple(sorted(FX_MEMBERSHIP_SOURCES)),
                missing_sources=(),
                completeness=BuildCompleteness.COMPLETE,
            )

    def test_partition_violation_raises(self):
        # present∪missing != membership
        with self.assertRaises(ValueError):
            self._complete(completeness=BuildCompleteness.PARTIAL,
                           payload=_payload("usd-krw", ["kb"], reference=False),
                           present_sources=("kb",), missing_sources=("hana",))


class TestDormancy(unittest.TestCase):
    """B2a dormant — app/ 어떤 live 모듈도 atomic_build import/build_fx_result 호출 0. atomic_build는
    non-island(atomic_value_schema 미import)이나, 미래 dormant 모듈(B2b)이 import할 수 있어 dormant 전체 skip."""

    _DORMANT_MODULES = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
        "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py",
        "atomic_reconcile.py", "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py", "atomic_fx_v2_loader.py", "atomic_fx_publisher.py",
    })
    _CALL_NEEDLES = ("build_fx_result",)

    def test_no_live_module_uses_atomic_build(self):
        import ast

        app_dir = pathlib.Path(ab.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._DORMANT_MODULES:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_build" in node.module.split("."):
                        self.fail(f"{rel}: from atomic_build import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_build" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_build — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_build" in a.name.split("."):
                            self.fail(f"{rel}: import atomic_build — dormant 위반")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in self._CALL_NEEDLES:
                        self.fail(f"{rel}: {name}() 호출 — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "atomic_build" in node.value or "build_fx_result" in node.value:
                        self.fail(f"{rel}: 문자열 '{node.value}'에 atomic_build — dynamic 호출 의심")


if __name__ == "__main__":
    unittest.main()
