"""P1b C6-2 — CutoverReadinessSnapshot runtime 단위 테스트 (§15, dormant, A2-1 pattern).

_INITIAL legacy-safe + snapshot() no-throw + refresh_from_db(legacy→LEGACY_READY gate_open / atomic 경로
writer-axis 배선 / read-fail fail-closed HALT_BLOCKED gate 닫음 + gen 단조 보존) + dormancy.
derivation 자체는 durable 테스트가 검증 — 여기선 snapshot 메커닉.
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import patch

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_cutover_runtime as acr
from app import atomic_write_runtime
from app.atomic_cutover import CutoverState
from app.atomic_cutover_runtime import CutoverReadinessSnapshot, refresh_from_db, snapshot
from app.atomic_value_schema import make_revision_key
from app.atomic_write_control import WriterMode
from app.models import AtomicCutoverAsset, AtomicCutoverControl

_ASSETS = ("usd-krw", "jpy-krw", "eur-krw")
_VEC = json.dumps({"kb": make_revision_key(1_700_000_000_000_000, 5)})


def _session():
    engine = create_engine("sqlite:///:memory:")
    AtomicCutoverControl.__table__.create(bind=engine)
    AtomicCutoverAsset.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db, *, status="idle", session_id=None, generation=0, asset_state="blocked"):
    db.add(AtomicCutoverControl(id=1, cutover_row_format_version=1, bootstrap_generation=generation,
                               bootstrap_status=status, bootstrap_session_id=session_id))
    for a in _ASSETS:
        if asset_state == "ready":
            db.add(AtomicCutoverAsset(asset=a, publish_state="ready", ready_revision_vector=_VEC,
                                      membership_version=1))
        else:
            db.add(AtomicCutoverAsset(asset=a, publish_state="blocked"))
    db.commit()


def _writer_snap(enforced):
    return atomic_write_runtime.WriteModeSnapshot(
        diagnostic_effective_mode=enforced, activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced, mode_generation=0,
    )


class TestSnapshotRuntime(unittest.TestCase):

    def setUp(self):
        acr._reset_for_test()

    def tearDown(self):
        acr._reset_for_test()

    def test_initial_legacy_safe(self):
        s = snapshot()
        self.assertEqual(s.cutover_state, CutoverState.LEGACY_READY)
        self.assertTrue(s.publisher_gate_open)   # pre-cutover 정상 발행
        self.assertFalse(s.read_ok)
        self.assertEqual(s.bootstrap_generation, 0)

    def test_refresh_legacy_writer_gate_open(self):
        db = _session(); _seed(db, status="idle", generation=3)
        with patch.object(atomic_write_runtime, "snapshot", return_value=_writer_snap(WriterMode.LEGACY)):
            s = refresh_from_db(db)
        self.assertEqual(s.cutover_state, CutoverState.LEGACY_READY)  # writer axis first
        self.assertTrue(s.publisher_gate_open)
        self.assertTrue(s.read_ok)
        self.assertEqual(s.bootstrap_generation, 3)
        self.assertEqual(len(s.per_asset_publish_state), 3)

    def test_refresh_atomic_running_gate_closed(self):
        # writer=atomic + cutover running → ATOMIC_BLOCKED → gate 닫힘
        db = _session(); _seed(db, status="running", session_id="s:1", generation=2)
        with patch.object(atomic_write_runtime, "snapshot", return_value=_writer_snap(WriterMode.ATOMIC)):
            s = refresh_from_db(db)
        self.assertEqual(s.cutover_state, CutoverState.ATOMIC_BLOCKED)
        self.assertFalse(s.publisher_gate_open)
        self.assertEqual(s.writer_enforced_action, WriterMode.ATOMIC)

    def test_refresh_atomic_completed_ready_gate_open(self):
        # writer=atomic + completed + 3 asset ready(mv match) → ATOMIC_READY → gate OPEN (snapshot layer)
        db = _session(); _seed(db, status="completed", session_id="s:1", generation=5, asset_state="ready")
        with patch.object(atomic_write_runtime, "snapshot", return_value=_writer_snap(WriterMode.ATOMIC)):
            s = refresh_from_db(db)
        self.assertEqual(s.cutover_state, CutoverState.ATOMIC_READY)
        self.assertTrue(s.publisher_gate_open)
        self.assertTrue(s.read_ok)
        self.assertTrue(all(st == "ready" for _, st in s.per_asset_publish_state))

    def test_refresh_read_fail_closed(self):
        db = _session(); _seed(db, status="running", generation=7)
        # 먼저 정상 refresh로 gen=7 관측
        with patch.object(atomic_write_runtime, "snapshot", return_value=_writer_snap(WriterMode.LEGACY)):
            refresh_from_db(db)
        # read 실패 주입 → fail-closed + gen 보존
        with patch.object(acr, "read_cutover_control", side_effect=RuntimeError("db down")):
            s = refresh_from_db(db)
        self.assertEqual(s.cutover_state, CutoverState.HALT_BLOCKED)  # fail-closed
        self.assertFalse(s.publisher_gate_open)
        self.assertFalse(s.read_ok)
        self.assertEqual(s.bootstrap_generation, 7)   # 회귀 금지(단조 보존)

    def test_generation_monotonic_never_regress(self):
        db = _session(); _seed(db, status="idle", generation=10)
        with patch.object(atomic_write_runtime, "snapshot", return_value=_writer_snap(WriterMode.LEGACY)):
            refresh_from_db(db)
        self.assertEqual(snapshot().bootstrap_generation, 10)
        # control이 더 낮은 gen으로 바뀌어도(이론상 불가) 관측은 회귀 안 함
        db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).update({"bootstrap_generation": 2})
        db.commit()
        with patch.object(atomic_write_runtime, "snapshot", return_value=_writer_snap(WriterMode.LEGACY)):
            s = refresh_from_db(db)
        self.assertEqual(s.bootstrap_generation, 10)   # max(10, 2)

    def test_snapshot_no_throw(self):
        # snapshot()은 어떤 상황에서도 예외 안 냄
        self.assertIsInstance(snapshot(), CutoverReadinessSnapshot)


class TestDormancy(unittest.TestCase):
    """C6-2 dormant — app/ 어떤 live 모듈도 atomic_cutover_runtime import 0 + scheduling 0."""

    _ISLAND = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py", "atomic_write_outcome.py",
        "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py", "atomic_reconcile.py",
        "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py", "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py",
    })

    # C6-7: fx_topic_publisher가 atomic_cutover_runtime.snapshot()의 첫 **sanctioned live consumer**
    # (publish gate shadow read, dry-run). 그 외 live 모듈은 여전히 import 0.
    _SANCTIONED_LIVE_CONSUMERS = frozenset({"fx_topic_publisher.py"})

    def test_no_live_module_imports_runtime(self):
        app_dir = pathlib.Path(acr.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._ISLAND or py.name in self._SANCTIONED_LIVE_CONSUMERS:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "atomic_cutover_runtime" in node.module:
                    self.fail(f"{rel}: from atomic_cutover_runtime import — dormant 위반")
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_cutover_runtime" in a.name:
                            self.fail(f"{rel}: import atomic_cutover_runtime — dormant 위반")

    def test_no_import_time_scheduling(self):
        # 모듈 소스에 APScheduler/add_job/create_task/threading.Thread start 없음 (refresh 자동발화 금지)
        src = pathlib.Path(acr.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"runtime에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
