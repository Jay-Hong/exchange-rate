"""P1b B2b-1 — DERIVED pending detection 단위 테스트 (§19:347, pure dormant).

derive_pending 결정표 전수(5 reason) + None watermark + present>DB + STRUCTURAL + dormancy.
detection-only 계약 확인(출력은 reason 분류만; publish/effective/watermark write/subscriber 판단 없음).
"""
from __future__ import annotations

import pathlib
import unittest

from app import atomic_reconcile as ar
from app.atomic_reconcile import PendingReason, derive_pending
from app.atomic_value_schema import make_revision_key
from app.atomic_watermark import Watermark
from app.fx_membership import FX_MEMBERSHIP_SOURCES

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


class TestDormancy(unittest.TestCase):
    """B2b-1 dormant — app/ 어떤 live 모듈도 atomic_reconcile import/derive_pending 호출 0.
    atomic_watermark/atomic_value_schema(island) import → dormant set 멤버(allowlist skip)."""

    _DORMANT_MODULES = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
        "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
        "atomic_build.py", "atomic_reconcile.py",
    })
    _CALL_NEEDLES = ("derive_pending",)

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
