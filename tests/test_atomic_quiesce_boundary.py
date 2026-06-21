"""P1b C6-quiesce Q4b — RealQuiesceBoundary 단위 테스트 (scripts/activate_atomic_fx.py).

confirm_quiesced() §9 step6 4조건 verdict matrix(open session + FRESH A1[cond1 halt / cond3 exact-pin /
epoch0] + latest qualifying ACK[cond2/3/4]) + never-raise→False(table absent / session error) +
**constructor-default-stays-fail-closed lock**(AtomicFxActivator default kwarg = _FailClosedQuiesceBoundary)
+ **W3 arming**(main()은 RealQuiesceBoundary 주입 — test_main_injects_real_boundary). read-only, no live
race(seed rows + in-memory sqlite).
"""
from __future__ import annotations

import ast
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


class TestRealBoundaryDelegatesToHelper(unittest.TestCase):
    """b1 — RealQuiesceBoundary.confirm_quiesced()는 공유 confirm_quiesce_drained에 위임(재인라인 회귀 방지).

    동일 semantics 증명은 TestRealQuiesceBoundary 16 case(실 row seed)가 이미 커버 — 이 클래스는 '실제로
    helper를 호출(중복 구현 아님)'을 patch로 잠근다. expected_generation=None(begin-atomic은 session 신뢰).
    """

    def test_delegates_true(self):
        with patch("scripts.activate_atomic_fx.confirm_quiesce_drained", return_value=True) as m, \
                patch("app.database.SessionLocal", MagicMock()):
            self.assertTrue(RealQuiesceBoundary().confirm_quiesced())
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs, {})  # expected_generation 미전달 = None(session 권위)

    def test_delegates_false(self):
        with patch("scripts.activate_atomic_fx.confirm_quiesce_drained", return_value=False), \
                patch("app.database.SessionLocal", MagicMock()):
            self.assertFalse(RealQuiesceBoundary().confirm_quiesced())

    def test_helper_raise_still_false(self):
        # helper가 (이론상) raise해도 confirm_quiesced의 try/except floor가 False 보장
        with patch("scripts.activate_atomic_fx.confirm_quiesce_drained", side_effect=RuntimeError("x")), \
                patch("app.database.SessionLocal", MagicMock()):
            self.assertFalse(RealQuiesceBoundary().confirm_quiesced())


class TestDefaultStaysFailClosed(unittest.TestCase):
    """constructor default boundary는 **여전히** _FailClosedQuiesceBoundary. **W3(arming) 후 main()만**
    RealQuiesceBoundary를 주입(아래 test_main_injects_real_boundary)."""

    def test_activator_default_is_fail_closed(self):
        act = AtomicFxActivator(MagicMock())
        self.assertIsInstance(act.quiesce_boundary, _FailClosedQuiesceBoundary)
        self.assertFalse(act.quiesce_boundary.confirm_quiesced())

    def test_main_injects_real_boundary(self):
        # W3 arming(C6-FLIP): main()이 AtomicFxActivator(..., quiesce_boundary=RealQuiesceBoundary()) 주입 →
        # begin-atomic이 실 drain proof를 consult. **AST 구조 검사**(getsource substring은 주석의
        # 'RealQuiesceBoundary'로 false-pass — codex; 주입 call이 제거되면 trip). 이 marker가 arming audit
        # point(critic#9). prod behavior-change-0: capability gate(IMAGE_MAX=1) + quiesce table 부재로
        # begin-atomic은 RELEASE/G2a/EXEC 전까지 여전히 차단 — arming ≠ flip.
        def _name(fn):
            return fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
        tree = ast.parse(inspect.getsource(afx.main))
        injected = any(
            isinstance(n, ast.Call) and _name(n.func) == "AtomicFxActivator"
            and any(kw.arg == "quiesce_boundary" and isinstance(kw.value, ast.Call)
                    and _name(kw.value.func) == "RealQuiesceBoundary"
                    for kw in n.keywords)
            for n in ast.walk(tree)
        )
        self.assertTrue(
            injected, "W3: main()이 AtomicFxActivator(quiesce_boundary=RealQuiesceBoundary()) 주입해야 (arming)")


if __name__ == "__main__":
    unittest.main()
