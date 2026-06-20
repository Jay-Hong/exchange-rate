"""P1b C6-quiesce Q4a — live-app startup halt-ACK writer 단위 테스트 (atomic_quiesce_startup.py).

record_app_ack_if_quiescing(): no-op-legacy(no open session / control legacy) + writes-on-halt+open-session +
no-throw(session error / table absent) + cond-miss(stale gen) + dup boot CAS_LOST + import-time-side-effect-0.
behavior-change-0 legacy = no open session 시 0 write. real scheduler/thread 없음(injected fakes + in-memory sqlite).
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.atomic_quiesce_startup as aqs
from app.atomic_quiesce_startup import record_app_ack_if_quiescing
from app.atomic_write_control import CONTROL_ROW_FORMAT_VERSION, WriterMode
from app.atomic_write_runtime import WriteModeSnapshot
from app.models import AtomicQuiesceAppAck, AtomicQuiesceSession, AtomicWriteControl

_HALT_AT = datetime(2026, 6, 20, 6, 45, 0)
_BOOT_AT = _HALT_AT + timedelta(seconds=30)


def _snap(enforced=WriterMode.HALT, gen=5):
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced, activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced, mode_generation=gen,
    )


def _build(*, control_mode=WriterMode.HALT, mode_gen=5, epoch=0,
           with_quiesce_tables=True, open_session=True):
    """in-memory engine + sessionmaker (StaticPool로 여러 SessionLocal() 공유). control/open-session seed."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    AtomicWriteControl.__table__.create(bind=engine)
    if with_quiesce_tables:
        AtomicQuiesceSession.__table__.create(bind=engine)
        AtomicQuiesceAppAck.__table__.create(bind=engine)
    SF = sessionmaker(bind=engine)
    db = SF()
    db.add(AtomicWriteControl(
        id=1, control_row_format_version=CONTROL_ROW_FORMAT_VERSION, target_write_schema_version=1,
        required_writer_protocol=1, activation_epoch=epoch, mode_generation=mode_gen,
        requested_mode=control_mode, activated_at=None,
    ))
    if with_quiesce_tables and open_session:
        db.add(AtomicQuiesceSession(
            session_id="q1", halt_mode_generation=5, halt_committed_at=_HALT_AT, state="open",
        ))
    db.commit(); db.close()
    return SF


def _ack_rows(SF):
    db = SF()
    try:
        return db.query(AtomicQuiesceAppAck).all()
    finally:
        db.close()


def _run(SF, *, enforced=WriterMode.HALT, gen=5, process_started=_BOOT_AT):
    with patch("app.database.SessionLocal", SF), \
         patch("app.atomic_write_runtime.snapshot", return_value=_snap(enforced, gen)), \
         patch("app.atomic_quiesce_startup._process_started_at", return_value=process_started):
        return record_app_ack_if_quiescing()


class TestAckWriter(unittest.TestCase):

    def test_no_op_when_no_open_session(self):
        # behavior-change-0 legacy: open session 없음 → None, 0 ACK row (brick 미도달).
        SF = _build(open_session=False)
        self.assertIsNone(_run(SF))
        self.assertEqual(len(_ack_rows(SF)), 0)

    def test_no_op_when_control_legacy(self):
        # open session 있어도 control legacy + observed legacy → brick fail-closed → 0 row.
        from app.atomic_cutover_durable import CasResult
        SF = _build(control_mode=WriterMode.LEGACY, open_session=True)
        result = _run(SF, enforced=WriterMode.LEGACY)
        self.assertIs(result, CasResult.PRECONDITION_FAILED)
        self.assertEqual(len(_ack_rows(SF)), 0)

    def test_writes_ack_on_halt_and_open(self):
        from app.atomic_cutover_durable import CasResult
        SF = _build(control_mode=WriterMode.HALT, mode_gen=5, open_session=True)
        with patch("app.atomic_quiesce_startup._BOOT_ID", "boot-test"):
            result = _run(SF, enforced=WriterMode.HALT, gen=5)
        self.assertIs(result, CasResult.APPLIED)
        rows = _ack_rows(SF)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].session_id, rows[0].boot_id, rows[0].observed_enforced_action), ("q1", "boot-test", "halt"))
        self.assertEqual(rows[0].observed_writer_generation, 5)

    def test_stale_generation_no_write(self):
        # cond3: observed gen 4 < halt_gen 5 → PRECONDITION, 0 row.
        from app.atomic_cutover_durable import CasResult
        SF = _build(open_session=True)
        self.assertIs(_run(SF, gen=4), CasResult.PRECONDITION_FAILED)
        self.assertEqual(len(_ack_rows(SF)), 0)

    def test_dup_boot_cas_lost(self):
        from app.atomic_cutover_durable import CasResult
        SF = _build(open_session=True)
        with patch("app.atomic_quiesce_startup._BOOT_ID", "boot-x"):
            self.assertIs(_run(SF), CasResult.APPLIED)
            self.assertIs(_run(SF), CasResult.CAS_LOST)  # 같은 boot 재-ACK
        self.assertEqual(len(_ack_rows(SF)), 1)

    def test_no_throw_on_session_error(self):
        # SessionLocal raise → no-throw(None), 예외 전파 0.
        with patch("app.database.SessionLocal", side_effect=RuntimeError("boom")):
            self.assertIsNone(record_app_ack_if_quiescing())

    def test_no_throw_table_absent(self):
        # quiesce table 부재(pre-migrate, create_all 제외) → find_open raise → broad except 흡수 → None.
        SF = _build(with_quiesce_tables=False)
        self.assertIsNone(_run(SF))  # no crash


class TestImportTimeSideEffectFree(unittest.TestCase):
    """app import는 전부 함수 내부 lazy — module-level에 SessionLocal/island/scheduling 0."""

    _FORBIDDEN_TOP_IMPORT = {"database", "atomic_quiesce_durable", "atomic_cutover_durable", "atomic_write_runtime"}

    def test_no_module_level_app_imports(self):
        tree = ast.parse(pathlib.Path(aqs.__file__).read_text(encoding="utf-8"))
        for node in tree.body:  # TOP-LEVEL only (함수 body 제외)
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[-1], self._FORBIDDEN_TOP_IMPORT,
                                 f"module-level import {node.module} — lazy(함수 내부)여야 import-time side effect 0")
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotIn(a.name.split(".")[-1], self._FORBIDDEN_TOP_IMPORT)

    def test_no_scheduling_needle(self):
        src = pathlib.Path(aqs.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"startup module에 scheduling needle '{needle}'")


if __name__ == "__main__":
    unittest.main()
