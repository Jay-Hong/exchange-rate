"""P1b C6-quiesce Q4b — RealQuiesceBoundary 단위 테스트 (scripts/activate_atomic_fx.py).

confirm_quiesced() §9 step6 4조건 verdict matrix(open session + FRESH A1[cond1 halt / cond3 exact-pin /
epoch0] + latest qualifying ACK[cond2/3/4]) + never-raise→False(table absent / session error) +
**default-stays-fail-closed lock**(AtomicFxActivator default = _FailClosedQuiesceBoundary, main() 무주입 —
RealBoundary 주입은 C6-FLIP only). read-only, no live race(seed rows + in-memory sqlite).
"""
from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.atomic_write_control import CONTROL_ROW_FORMAT_VERSION, WriterMode
from app.models import AtomicQuiesceAppAck, AtomicQuiesceSession, AtomicWriteControl
from scripts import activate_atomic_fx as afx
from scripts.activate_atomic_fx import (
    AtomicFxActivator,
    RealQuiesceBoundary,
    _FailClosedQuiesceBoundary,
)

_HALT_AT = datetime(2026, 6, 20, 6, 45, 0)
_BOOT_AT = _HALT_AT + timedelta(seconds=30)
_ACK_AT = _BOOT_AT + timedelta(seconds=5)


def _build(*, control_mode=WriterMode.HALT, mode_gen=5, epoch=0, format_ok=True,
           open_session=True, session_gen=5, halt_at=_HALT_AT,
           ack=True, ack_action=WriterMode.HALT, ack_gen=5, ack_started=_BOOT_AT,
           with_tables=True):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    AtomicWriteControl.__table__.create(bind=engine)
    if with_tables:
        AtomicQuiesceSession.__table__.create(bind=engine)
        AtomicQuiesceAppAck.__table__.create(bind=engine)
    SF = sessionmaker(bind=engine)
    db = SF()
    db.add(AtomicWriteControl(
        id=1,
        control_row_format_version=(CONTROL_ROW_FORMAT_VERSION if format_ok else CONTROL_ROW_FORMAT_VERSION + 1),
        target_write_schema_version=1, required_writer_protocol=1,
        activation_epoch=epoch, mode_generation=mode_gen, requested_mode=control_mode, activated_at=None,
    ))
    if with_tables and open_session:
        db.add(AtomicQuiesceSession(
            session_id="q1", halt_mode_generation=session_gen, halt_committed_at=halt_at, state="open",
        ))
    if with_tables and ack:
        db.add(AtomicQuiesceAppAck(
            session_id="q1", boot_id="b1", process_started_at=ack_started,
            observed_writer_generation=ack_gen, observed_enforced_action=ack_action, observed_at=_ACK_AT,
        ))
    db.commit(); db.close()
    return SF


def _confirm(SF):
    with patch("app.database.SessionLocal", SF):
        return RealQuiesceBoundary().confirm_quiesced()


class TestRealQuiesceBoundary(unittest.TestCase):

    def test_all_conditions_true(self):
        self.assertTrue(_confirm(_build()))

    def test_no_open_session_false(self):
        self.assertFalse(_confirm(_build(open_session=False, ack=False)))

    def test_control_not_halt_false(self):
        self.assertFalse(_confirm(_build(control_mode=WriterMode.LEGACY)))  # cond1
        self.assertFalse(_confirm(_build(control_mode=WriterMode.ATOMIC)))

    def test_generation_bump_false(self):
        # cond3-fresh exact pin: live mode_generation != session.halt_mode_generation → superseded halt fail-close.
        self.assertFalse(_confirm(_build(mode_gen=6, session_gen=5)))

    def test_activation_epoch_nonzero_false(self):
        self.assertFalse(_confirm(_build(epoch=1)))  # activation quiesce 한정

    def test_format_mismatch_false(self):
        self.assertFalse(_confirm(_build(format_ok=False)))

    def test_ack_non_halt_false(self):
        self.assertFalse(_confirm(_build(ack_action=WriterMode.LEGACY)))  # cond2

    def test_ack_stale_generation_false(self):
        self.assertFalse(_confirm(_build(ack_gen=4, session_gen=5)))  # cond3 ACK (4 < 5)

    def test_ack_process_not_after_halt_false(self):
        self.assertFalse(_confirm(_build(ack_started=_HALT_AT)))  # cond4 (동일 시각, > 아님)
        self.assertFalse(_confirm(_build(ack_started=_HALT_AT - timedelta(seconds=1))))

    def test_no_ack_false(self):
        self.assertFalse(_confirm(_build(ack=False)))

    def test_newer_ack_generation_ok(self):
        # cond3 ACK는 >= 이므로 더 큰 generation도 qualifying (boundary cond3-fresh는 ctrl==session이라 별개).
        self.assertTrue(_confirm(_build(ack_gen=6, session_gen=5, mode_gen=5)))

    def test_latest_qualifying_ack_picked(self):
        # 2 ACK: 하나는 non-qualifying(legacy), 하나는 qualifying(halt) → qualifying 있으면 True.
        SF = _build(ack=True, ack_action=WriterMode.HALT)
        db = SF()
        db.add(AtomicQuiesceAppAck(
            session_id="q1", boot_id="b-old", process_started_at=_BOOT_AT,
            observed_writer_generation=5, observed_enforced_action=WriterMode.LEGACY,  # non-qualifying
            observed_at=_ACK_AT + timedelta(seconds=10),  # 더 최신이지만 cond2 실패
        ))
        db.commit(); db.close()
        self.assertTrue(_confirm(SF))  # qualifying halt ACK가 있으면 True (non-qualifying은 filter out)

    def test_table_absent_never_raise_false(self):
        self.assertFalse(_confirm(_build(with_tables=False)))  # no quiesce tables → except → False

    def test_session_error_never_raise_false(self):
        with patch("app.database.SessionLocal", side_effect=RuntimeError("boom")):
            self.assertFalse(RealQuiesceBoundary().confirm_quiesced())


class TestDefaultStaysFailClosed(unittest.TestCase):
    """C6-FLIP 전: default boundary는 _FailClosedQuiesceBoundary, main()은 RealBoundary 미주입."""

    def test_activator_default_is_fail_closed(self):
        act = AtomicFxActivator(MagicMock())
        self.assertIsInstance(act.quiesce_boundary, _FailClosedQuiesceBoundary)
        self.assertFalse(act.quiesce_boundary.confirm_quiesced())

    def test_main_does_not_inject_real_boundary(self):
        # main()이 RealQuiesceBoundary를 주입하면 dormant 깨짐 — 주입은 C6-FLIP only.
        main_src = inspect.getsource(afx.main)
        self.assertNotIn("RealQuiesceBoundary", main_src,
                         "main()이 RealQuiesceBoundary 주입 — C6-FLIP 전엔 default fail-closed 유지해야")


if __name__ == "__main__":
    unittest.main()
