"""P1b C6-quiesce Q2b — quiesce-evidence durable CAS bricks 단위 테스트 (atomic_quiesce_durable.py, dormant).

cas_open_quiesce_session(activation halt evidence) + cas_record_quiesce_app_ack(fresh-process halt ACK,
fail-closed §9 step6 4조건) + cas_consume_quiesce_session(open→consumed) — APPLIED/PRECONDITION_FAILED/
CAS_LOST + Q9 activation_epoch==0 fence(incident-halt 거부) + single-open + caller-commits(staged) +
dormancy(no live importer + no scheduling).
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_quiesce_durable as aqd
from app.atomic_cutover_durable import CasResult
from app.atomic_quiesce_durable import (
    cas_consume_quiesce_session,
    cas_open_quiesce_session,
    cas_record_quiesce_app_ack,
)
from app.atomic_write_control import CONTROL_ROW_FORMAT_VERSION, WriterMode
from app.models import AtomicQuiesceAppAck, AtomicQuiesceSession, AtomicWriteControl

_HALT_AT = datetime(2026, 6, 20, 6, 45, 0)          # naive UTC (halt commit)
_BOOT_AT = _HALT_AT + timedelta(seconds=30)         # fresh process boot > halt (cond4 만족)


def _session():
    engine = create_engine("sqlite:///:memory:")
    AtomicWriteControl.__table__.create(bind=engine)
    AtomicQuiesceSession.__table__.create(bind=engine)
    AtomicQuiesceAppAck.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_control(db, *, requested_mode=WriterMode.HALT, mode_generation=5, activation_epoch=0,
                  control_row_format_version=CONTROL_ROW_FORMAT_VERSION):
    db.add(AtomicWriteControl(
        id=1, control_row_format_version=control_row_format_version,
        target_write_schema_version=1, required_writer_protocol=1,
        activation_epoch=activation_epoch, mode_generation=mode_generation,
        requested_mode=requested_mode, activated_at=None,
    ))
    db.commit()


def _open_session(db, **over):
    base = dict(session_id="q1", halt_mode_generation=5, halt_committed_at=_HALT_AT)
    base.update(over)
    return cas_open_quiesce_session(db, **base)


def _record_ack(db, **over):
    base = dict(session_id="q1", boot_id="boot-1", process_started_at=_BOOT_AT,
                observed_writer_generation=5, observed_enforced_action=WriterMode.HALT)
    base.update(over)
    return cas_record_quiesce_app_ack(db, **base)


class TestOpenSession(unittest.TestCase):

    def test_activation_halt_epoch0_applied(self):
        db = _session(); _seed_control(db, requested_mode=WriterMode.HALT, mode_generation=5, activation_epoch=0)
        self.assertIs(_open_session(db), CasResult.APPLIED)
        db.commit()
        row = db.query(AtomicQuiesceSession).one()
        self.assertEqual((row.session_id, row.state, row.halt_mode_generation), ("q1", "open", 5))
        self.assertEqual(row.halt_committed_at, _HALT_AT)

    def test_incident_halt_epoch_nonzero_rejected(self):
        # Q9: incident-halt(atomic→halt)는 activation_epoch>=1 → cas_open 거부 (activation quiesce 전용).
        db = _session(); _seed_control(db, requested_mode=WriterMode.HALT, mode_generation=5, activation_epoch=1)
        self.assertIs(_open_session(db), CasResult.PRECONDITION_FAILED)
        self.assertEqual(db.query(AtomicQuiesceSession).count(), 0)

    def test_not_halt_mode_rejected(self):
        db = _session(); _seed_control(db, requested_mode=WriterMode.LEGACY, mode_generation=5)
        self.assertIs(_open_session(db), CasResult.PRECONDITION_FAILED)

    def test_generation_mismatch_rejected(self):
        db = _session(); _seed_control(db, mode_generation=7)
        self.assertIs(_open_session(db, halt_mode_generation=5), CasResult.PRECONDITION_FAILED)

    def test_format_mismatch_rejected(self):
        db = _session(); _seed_control(db, control_row_format_version=CONTROL_ROW_FORMAT_VERSION + 1)
        self.assertIs(_open_session(db), CasResult.PRECONDITION_FAILED)

    def test_no_control_row_rejected(self):
        db = _session()  # control seed 안 함
        self.assertIs(_open_session(db), CasResult.PRECONDITION_FAILED)

    def test_single_open_second_rejected(self):
        db = _session(); _seed_control(db)
        self.assertIs(_open_session(db, session_id="s1"), CasResult.APPLIED)
        db.commit()
        self.assertIs(_open_session(db, session_id="s2"), CasResult.PRECONDITION_FAILED)  # 이미 open 존재

    def test_param_fences(self):
        db = _session(); _seed_control(db)
        self.assertIs(_open_session(db, session_id=""), CasResult.PRECONDITION_FAILED)
        self.assertIs(_open_session(db, halt_mode_generation=-1), CasResult.PRECONDITION_FAILED)
        self.assertIs(_open_session(db, halt_mode_generation=True), CasResult.PRECONDITION_FAILED)  # bool 배제
        self.assertIs(_open_session(db, halt_committed_at="2026-06-20"), CasResult.PRECONDITION_FAILED)

    def test_aware_halt_committed_at_stored_naive(self):
        db = _session(); _seed_control(db)
        aware = datetime(2026, 6, 20, 6, 45, 0, tzinfo=timezone.utc)
        self.assertIs(_open_session(db, halt_committed_at=aware), CasResult.APPLIED)
        db.commit()
        self.assertIsNone(db.query(AtomicQuiesceSession).one().halt_committed_at.tzinfo)

    def test_applied_is_staged_not_committed(self):
        db = _session(); _seed_control(db)
        self.assertIs(_open_session(db), CasResult.APPLIED)
        db.rollback()  # caller-commits 계약 — staged라 rollback 시 원복
        self.assertEqual(db.query(AtomicQuiesceSession).count(), 0)


class TestRecordAppAck(unittest.TestCase):

    def _open(self, db):
        _seed_control(db); self.assertIs(_open_session(db), CasResult.APPLIED); db.commit()

    def test_all_conditions_applied(self):
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db), CasResult.APPLIED)
        db.commit()
        row = db.query(AtomicQuiesceAppAck).one()
        self.assertEqual((row.session_id, row.boot_id, row.observed_enforced_action), ("q1", "boot-1", "halt"))
        self.assertEqual(row.observed_writer_generation, 5)

    def test_non_halt_observed_rejected(self):
        # cond2 fail-closed: legacy/atomic 관측은 ACK write 안 됨 (stale process false ACK 차단).
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, observed_enforced_action=WriterMode.LEGACY), CasResult.PRECONDITION_FAILED)
        self.assertIs(_record_ack(db, observed_enforced_action=WriterMode.ATOMIC), CasResult.PRECONDITION_FAILED)
        self.assertEqual(db.query(AtomicQuiesceAppAck).count(), 0)

    def test_control_not_halt_rejected(self):
        # cond1: live requested_mode != halt → 거부 (open session 있어도).
        db = _session(); self._open(db)
        db.query(AtomicWriteControl).filter_by(id=1).update({"requested_mode": WriterMode.ATOMIC})
        db.commit()
        self.assertIs(_record_ack(db), CasResult.PRECONDITION_FAILED)

    def test_no_open_session_rejected(self):
        db = _session(); _seed_control(db)  # open session 없음
        self.assertIs(_record_ack(db), CasResult.PRECONDITION_FAILED)

    def test_consumed_session_not_open_rejected(self):
        db = _session(); self._open(db)
        self.assertIs(cas_consume_quiesce_session(db, session_id="q1"), CasResult.APPLIED); db.commit()
        self.assertIs(_record_ack(db), CasResult.PRECONDITION_FAILED)  # state=consumed라 open 아님

    def test_stale_generation_rejected(self):
        # cond3: observed_writer_generation < halt_mode_generation → stale pre-halt process 거부.
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, observed_writer_generation=4), CasResult.PRECONDITION_FAILED)

    def test_newer_generation_ok(self):
        # cond3: observed >= halt_gen (>= 이므로 더 큰 generation도 허용).
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, observed_writer_generation=6), CasResult.APPLIED)

    def test_process_not_after_halt_rejected(self):
        # cond4: process_started_at <= halt_committed_at → recreate 전 process 거부.
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, process_started_at=_HALT_AT), CasResult.PRECONDITION_FAILED)  # 동일 시각(>아님)
        self.assertIs(_record_ack(db, process_started_at=_HALT_AT - timedelta(seconds=1)),
                      CasResult.PRECONDITION_FAILED)

    def test_dup_session_boot_cas_lost(self):
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, boot_id="b1"), CasResult.APPLIED); db.commit()
        self.assertIs(_record_ack(db, boot_id="b1"), CasResult.CAS_LOST)  # 같은 (session, boot)

    def test_multi_boot_same_session_applied(self):
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, boot_id="b1"), CasResult.APPLIED); db.commit()
        self.assertIs(_record_ack(db, boot_id="b2"), CasResult.APPLIED); db.commit()
        self.assertEqual(db.query(AtomicQuiesceAppAck).count(), 2)

    def test_param_fences(self):
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, boot_id=""), CasResult.PRECONDITION_FAILED)
        self.assertIs(_record_ack(db, observed_writer_generation=True), CasResult.PRECONDITION_FAILED)  # bool 배제
        self.assertIs(_record_ack(db, process_started_at="x"), CasResult.PRECONDITION_FAILED)
        self.assertIs(_record_ack(db, queue_size=-1), CasResult.PRECONDITION_FAILED)

    def test_queue_size_optional(self):
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db, queue_size=7), CasResult.APPLIED)
        db.commit()
        self.assertEqual(db.query(AtomicQuiesceAppAck).one().queue_size, 7)

    def test_applied_is_staged_not_committed(self):
        db = _session(); self._open(db)
        self.assertIs(_record_ack(db), CasResult.APPLIED)
        db.rollback()
        self.assertEqual(db.query(AtomicQuiesceAppAck).count(), 0)


class TestConsumeSession(unittest.TestCase):

    def test_open_to_consumed_applied(self):
        db = _session(); _seed_control(db); _open_session(db); db.commit()
        self.assertIs(cas_consume_quiesce_session(db, session_id="q1"), CasResult.APPLIED)
        db.commit()
        self.assertEqual(db.query(AtomicQuiesceSession).one().state, "consumed")

    def test_not_open_rejected(self):
        db = _session(); _seed_control(db); _open_session(db); db.commit()
        cas_consume_quiesce_session(db, session_id="q1"); db.commit()
        # 이미 consumed → 재consume PRECONDITION_FAILED
        self.assertIs(cas_consume_quiesce_session(db, session_id="q1"), CasResult.PRECONDITION_FAILED)

    def test_unknown_session_rejected(self):
        db = _session(); _seed_control(db)
        self.assertIs(cas_consume_quiesce_session(db, session_id="nope"), CasResult.PRECONDITION_FAILED)

    def test_empty_session_id_rejected(self):
        db = _session()
        self.assertIs(cas_consume_quiesce_session(db, session_id=""), CasResult.PRECONDITION_FAILED)


class TestDormancy(unittest.TestCase):
    """C6-quiesce Q2b/Q4a — atomic_quiesce_durable를 import하는 app/ live 모듈은 sanctioned set뿐.

    Q2b 시점 caller 0. Q4a에서 atomic_quiesce_startup.py(startup halt-ACK writer)가 sanctioned importer로
    추가됨 (crud→atomic_direct_write C6-5b-3b 선례 — offenders-allowlist 전환). main.py는 atomic_quiesce_startup만
    import(island 직접 아님)이라 sanctioned set에 불포함. self(atomic_quiesce_durable.py)도 skip.
    """

    _SANCTIONED_IMPORTERS = frozenset({"atomic_quiesce_durable.py", "atomic_quiesce_startup.py"})

    def test_only_sanctioned_modules_import_quiesce_durable(self):
        app_dir = pathlib.Path(aqd.__file__).resolve().parent
        offenders = []
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._SANCTIONED_IMPORTERS:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module \
                        and node.module.split(".")[-1] == "atomic_quiesce_durable":
                    offenders.append(py.name)
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[-1] == "atomic_quiesce_durable":
                            offenders.append(py.name)
        self.assertEqual(offenders, [], f"unsanctioned import of atomic_quiesce_durable: {offenders}")

    def test_no_import_time_scheduling(self):
        src = pathlib.Path(aqd.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"quiesce_durable에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
