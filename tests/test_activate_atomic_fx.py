"""P1b C6-8b-2 — atomic FX activation command 단위 테스트 (scripts/activate_atomic_fx.py, dormant).

crash-resume matrix(pure) + phase fns(halt/begin-atomic 1-tx/verify gate/finalize 1-tx flip+complete/
incident-halt) + QuiesceBoundary fail-closed(begin-atomic machine dormancy) + B2 image-capability hard gate +
B3 asset-state fence + B4 format fence + A4 expected-* guard + accidental-exec acks + dry-run mutation 0 +
dormancy AST(no live publish/broadcast call + default boundary fail-closed + no app/ importer).
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# scripts/ 경로 추가 (test_rehearse_fx_cutover.py 패턴)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app.atomic_cutover_durable import FX_CUTOVER_ASSETS  # noqa: E402
from app.atomic_fx_v2_loader import FxV2LoadResult  # noqa: E402
from app.atomic_value_schema import make_revision_key  # noqa: E402
from app.atomic_write_control import WriterMode  # noqa: E402
from app.models import AtomicCutoverAsset, AtomicCutoverControl, AtomicWriteControl  # noqa: E402

import activate_atomic_fx as A  # noqa: E402

_REV = make_revision_key(1_700_000_000_000_000, 5)
_VEC = json.dumps({"kb": _REV})


# ────────────────────────────── fixtures ──────────────────────────────
def _session():
    engine = create_engine("sqlite:///:memory:")
    AtomicWriteControl.__table__.create(bind=engine)
    AtomicCutoverControl.__table__.create(bind=engine)
    AtomicCutoverAsset.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_writer(db, *, requested_mode="legacy", mode_generation=0, activation_epoch=0,
                 target_write_schema_version=1, required_writer_protocol=1,
                 control_row_format_version=1):
    db.add(AtomicWriteControl(
        id=1, control_row_format_version=control_row_format_version,
        target_write_schema_version=target_write_schema_version,
        required_writer_protocol=required_writer_protocol, activation_epoch=activation_epoch,
        mode_generation=mode_generation, requested_mode=requested_mode,
    ))
    db.commit()


def _seed_cutover(db, *, status="idle", session_id=None, generation=0,
                  cutover_row_format_version=1, asset_state="blocked", mixed_ready=None,
                  ready_membership=1, ready_vector=_VEC):
    db.add(AtomicCutoverControl(
        id=1, cutover_row_format_version=cutover_row_format_version,
        bootstrap_generation=generation, bootstrap_status=status, bootstrap_session_id=session_id,
    ))
    for a in FX_CUTOVER_ASSETS:
        ready = asset_state == "ready" or (mixed_ready is not None and a in mixed_ready)
        if ready:
            db.add(AtomicCutoverAsset(asset=a, publish_state="ready",
                                      ready_revision_vector=ready_vector, membership_version=ready_membership))
        else:
            db.add(AtomicCutoverAsset(asset=a, publish_state="blocked"))
    db.commit()


def _parse(argv):
    return A._build_arg_parser().parse_args(argv)


def _complete(db, asset):
    return FxV2LoadResult(payload={}, effective_revision_vector={"kb": _REV, "investing": _REV},
                          present_sources=("investing", "kb"))


def _incomplete(db, asset):
    return FxV2LoadResult(payload={}, effective_revision_vector={"kb": _REV},
                          present_sources=("investing", "kb"))


def _empty(db, asset):
    return FxV2LoadResult(payload={}, effective_revision_vector={}, present_sources=())


class _PassBoundary:
    def confirm_quiesced(self) -> bool:
        return True


# ────────────────────────────── resolve_resume_action (pure) ──────────────────────────────
class TestResolveResume(unittest.TestCase):
    def _r(self, mode, status, blocked, ready, *, epoch, session, schema=2):
        return A.resolve_resume_action(writer_mode=mode, writer_epoch=epoch,
                                       writer_schema_version=schema, cutover_status=status,
                                       cutover_session_present=session, assets_all_blocked=blocked,
                                       assets_all_ready=ready)

    # canonical happy arms (idle ⟹ epoch0/no-session, atomic ⟹ epoch1/session)
    def test_legacy_idle_blocked_halt(self):
        self.assertIs(self._r(WriterMode.LEGACY, "idle", True, False, epoch=0, session=False),
                      A.ResumeAction.HALT)

    def test_halt_idle_blocked_begin(self):
        self.assertIs(self._r(WriterMode.HALT, "idle", True, False, epoch=0, session=False),
                      A.ResumeAction.BEGIN_ATOMIC)

    def test_atomic_running_blocked_verify(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "running", True, False, epoch=1, session=True),
                      A.ResumeAction.VERIFY)

    def test_atomic_verified_blocked_finalize(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "verified", True, False, epoch=1, session=True),
                      A.ResumeAction.FINALIZE)

    def test_atomic_completed_ready_noop(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "completed", False, True, epoch=1, session=True),
                      A.ResumeAction.NOOP)

    def test_legacy_idle_not_blocked_failclosed(self):
        # B3: legacy+idle인데 asset이 blocked 아님 → stale ready 가능 → fail-closed
        self.assertIs(self._r(WriterMode.LEGACY, "idle", False, False, epoch=0, session=False),
                      A.ResumeAction.FAIL_CLOSED)

    def test_atomic_running_ready_failclosed(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "running", False, True, epoch=1, session=True),
                      A.ResumeAction.FAIL_CLOSED)

    def test_failed_status_failclosed(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "failed", True, False, epoch=1, session=True),
                      A.ResumeAction.FAIL_CLOSED)

    def test_atomic_completed_not_ready_failclosed(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "completed", True, False, epoch=1, session=True),
                      A.ResumeAction.FAIL_CLOSED)

    def test_unknown_mode_failclosed(self):
        self.assertIs(self._r("bogus", "idle", True, False, epoch=0, session=False),
                      A.ResumeAction.FAIL_CLOSED)

    # session/epoch invariant (review #1/#3/#7)
    def test_running_null_session_failclosed(self):
        # #1: running + NULL session → FAIL_CLOSED (expected_session=None IS NULL fence 무력화 차단)
        self.assertIs(self._r(WriterMode.ATOMIC, "running", True, False, epoch=1, session=False),
                      A.ResumeAction.FAIL_CLOSED)

    def test_verified_null_session_failclosed(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "verified", True, False, epoch=1, session=False),
                      A.ResumeAction.FAIL_CLOSED)

    def test_idle_with_session_failclosed(self):
        # #3: idle + non-null session → FAIL_CLOSED (idle ⟹ session 부재)
        self.assertIs(self._r(WriterMode.HALT, "idle", True, False, epoch=0, session=True),
                      A.ResumeAction.FAIL_CLOSED)

    def test_halt_idle_epoch_positive_failclosed(self):
        # #7: halt+idle인데 epoch>0 (post-incident re-cutover, deferred) → FAIL_CLOSED
        self.assertIs(self._r(WriterMode.HALT, "idle", True, False, epoch=1, session=False),
                      A.ResumeAction.FAIL_CLOSED)

    def test_atomic_running_epoch_zero_failclosed(self):
        # atomic+epoch==0 corruption → FAIL_CLOSED
        self.assertIs(self._r(WriterMode.ATOMIC, "running", True, False, epoch=0, session=True),
                      A.ResumeAction.FAIL_CLOSED)

    def test_legacy_idle_epoch_positive_failclosed(self):
        # legacy+epoch>0 corruption (§3 un-activate 금지) → FAIL_CLOSED
        self.assertIs(self._r(WriterMode.LEGACY, "idle", True, False, epoch=1, session=False),
                      A.ResumeAction.FAIL_CLOSED)

    def test_none_epoch_failclosed(self):
        # writer_epoch None (corrupt) → 안전 (TypeError 없이 fail-closed)
        self.assertIs(self._r(WriterMode.ATOMIC, "running", True, False, epoch=None, session=True),
                      A.ResumeAction.FAIL_CLOSED)

    def test_atomic_schema_below_floor_failclosed(self):
        # cross-check #2: atomic + schema<floor (corrupt) → FAIL_CLOSED (writer runtime은 halt)
        self.assertIs(self._r(WriterMode.ATOMIC, "running", True, False, epoch=1, session=True, schema=1),
                      A.ResumeAction.FAIL_CLOSED)

    def test_none_schema_failclosed(self):
        self.assertIs(self._r(WriterMode.ATOMIC, "verified", True, False, epoch=1, session=True, schema=None),
                      A.ResumeAction.FAIL_CLOSED)


# ────────────────────────────── asset helpers + read_control_state ──────────────────────────────
class TestStateRead(unittest.TestCase):
    def test_all_blocked_true(self):
        db = _session(); _seed_writer(db); _seed_cutover(db, asset_state="blocked")
        st = A.read_control_state(db)
        self.assertTrue(st.assets_all_blocked)
        self.assertFalse(st.assets_all_ready)
        self.assertEqual(st.writer_mode, WriterMode.LEGACY)
        self.assertEqual(st.cutover_status, "idle")

    def test_all_ready_true(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="completed", session_id="s", generation=3, asset_state="ready")
        st = A.read_control_state(db)
        self.assertTrue(st.assets_all_ready)
        self.assertFalse(st.assets_all_blocked)

    def test_mixed_neither(self):
        db = _session(); _seed_writer(db); _seed_cutover(db, mixed_ready={"usd-krw"})
        st = A.read_control_state(db)
        self.assertFalse(st.assets_all_blocked)
        self.assertFalse(st.assets_all_ready)

    def test_missing_rows(self):
        db = _session()  # 아무것도 seed 안 함
        st = A.read_control_state(db)
        self.assertFalse(st.writer_present)
        self.assertFalse(st.cutover_present)

    def test_stale_membership_not_all_ready(self):
        # cross-check #1: membership != FX_MEMBERSHIP_VERSION → gate-valid 아님 → all_ready False
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="completed", session_id="s", generation=3,
                      asset_state="ready", ready_membership=99)
        st = A.read_control_state(db)
        self.assertFalse(st.assets_all_ready)

    def test_invalid_vector_not_all_ready(self):
        # cross-check #1: 구조 무효 vector(membership 밖 key, 비어있지 않아 CHECK는 통과) → all_ready False
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="completed", session_id="s", generation=3,
                      asset_state="ready", ready_vector=json.dumps({"ghost": _REV}))
        st = A.read_control_state(db)
        self.assertFalse(st.assets_all_ready)


# ────────────────────────────── run() precondition / format / fail-closed ──────────────────────────────
class TestRunPreconditions(unittest.TestCase):
    def test_unseeded_refused(self):
        db = _session()
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("not seeded", out.message)

    def test_bad_writer_format_refused(self):
        db = _session(); _seed_writer(db, control_row_format_version=2); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("control_row_format_version", out.message)

    def test_bad_cutover_format_refused(self):
        db = _session(); _seed_writer(db); _seed_cutover(db, cutover_row_format_version=2)
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("cutover_row_format_version", out.message)

    def test_failclosed_state_refused(self):
        # writer legacy + cutover idle + asset 하나 ready(stale) → resolve FAIL_CLOSED
        db = _session(); _seed_writer(db); _seed_cutover(db, mixed_ready={"usd-krw"})
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("incident", out.message)

    def test_explicit_phase_mismatch_refused(self):
        db = _session(); _seed_writer(db); _seed_cutover(db)  # resolves HALT
        out = A.AtomicFxActivator(db).run(_parse(["--phase", "finalize"]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("state mismatch", out.message)


# ────────────────────────────── dry-run = mutation 0 ──────────────────────────────
class TestDryRun(unittest.TestCase):
    def test_plan_no_mutation(self):
        db = _session(); _seed_writer(db, mode_generation=2); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse([]))  # no --apply
        self.assertEqual(out.exit_code, A._EXIT_OK)
        self.assertEqual(out.status, "plan")
        # 변경 없음
        row = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(row.requested_mode, WriterMode.LEGACY)
        self.assertEqual(row.mode_generation, 2)

    def test_plan_lists_blocking(self):
        db = _session(); _seed_writer(db); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertIn("--i-understand-this-flips-production", out.message)


# ────────────────────────────── halt phase ──────────────────────────────
class TestHaltPhase(unittest.TestCase):
    def _argv(self, *extra):
        return ["--apply", "--i-understand-this-flips-production", "--rds-snapshot-confirmed", *extra]

    def test_halt_applied(self):
        db = _session(); _seed_writer(db, mode_generation=4); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        self.assertEqual(out.status, "applied")
        row = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(row.requested_mode, WriterMode.HALT)
        self.assertEqual(row.mode_generation, 5)

    def test_halt_missing_ack_refused(self):
        db = _session(); _seed_writer(db); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse(["--apply", "--rds-snapshot-confirmed"]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("--i-understand-this-flips-production", out.message)

    def test_halt_expected_generation_mismatch_refused(self):
        db = _session(); _seed_writer(db, mode_generation=4); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse(self._argv("--expected-writer-generation", "9")))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("expected-state guard mismatch", out.message)
        # mutation 0
        row = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(row.requested_mode, WriterMode.LEGACY)

    def test_halt_expected_generation_match_applied(self):
        db = _session(); _seed_writer(db, mode_generation=4); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse(self._argv("--expected-writer-generation", "4")))
        self.assertEqual(out.exit_code, A._EXIT_OK)


# ────────────────────────────── begin-atomic phase + B2 + quiesce ──────────────────────────────
class TestBeginAtomic(unittest.TestCase):
    def _argv(self, *extra):
        return ["--apply", "--i-understand-this-flips-production", "--rds-snapshot-confirmed",
                "--quiesce-confirmed", "--ack-global-writer-mode-scope", "--session-id", "s1", *extra]

    def test_b2_capability_hardfail_current_image(self):
        # 현재 image IMAGE_MAX=1 → required-protocol 2(범위밖) 또는 1(<=seed) 모두 불충족
        db = _session(); _seed_writer(db, requested_mode="halt"); _seed_cutover(db)
        out = A.AtomicFxActivator(db).run(_parse(self._argv("--required-protocol", "2", "--target-schema", "2")))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("image", out.message)

    def test_b2_protocol_at_seed_refused(self):
        db = _session(); _seed_writer(db, requested_mode="halt"); _seed_cutover(db)
        with patch.object(A, "IMAGE_MAX_WRITER_PROTOCOL", 2):
            out = A.AtomicFxActivator(db).run(
                _parse(self._argv("--required-protocol", "1", "--target-schema", "2")))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("legacy seed", out.message)

    def test_b2_target_schema_below_floor_refused(self):
        db = _session(); _seed_writer(db, requested_mode="halt"); _seed_cutover(db)
        with patch.object(A, "IMAGE_MAX_WRITER_PROTOCOL", 2):
            out = A.AtomicFxActivator(db).run(
                _parse(self._argv("--required-protocol", "2", "--target-schema", "1")))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("atomic schema floor", out.message)

    def test_quiesce_machine_gate_blocks_default_boundary(self):
        # capability/acks 전부 통과해도 default fail-closed boundary → begin-atomic 거부 (machine dormancy)
        db = _session(); _seed_writer(db, requested_mode="halt"); _seed_cutover(db)
        with patch.object(A, "IMAGE_MAX_WRITER_PROTOCOL", 2):
            out = A.AtomicFxActivator(db).run(  # default boundary
                _parse(self._argv("--required-protocol", "2", "--target-schema", "2")))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("QuiesceBoundary", out.message)
        row = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(row.requested_mode, WriterMode.HALT)  # mutation 0

    def test_begin_atomic_applied_one_tx(self):
        # pass boundary + future image → begin-atomic 실제 적용 (writer atomic + cutover running, 1 tx)
        db = _session(); _seed_writer(db, requested_mode="halt", mode_generation=5, activation_epoch=0)
        _seed_cutover(db, status="idle", generation=2)
        with patch.object(A, "IMAGE_MAX_WRITER_PROTOCOL", 2):
            out = A.AtomicFxActivator(db, quiesce_boundary=_PassBoundary()).run(
                _parse(self._argv("--required-protocol", "2", "--target-schema", "2")))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        self.assertEqual(out.status, "applied")
        w = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(w.requested_mode, WriterMode.ATOMIC)
        self.assertEqual(w.activation_epoch, 1)
        self.assertEqual(w.mode_generation, 6)
        self.assertEqual(w.required_writer_protocol, 2)
        self.assertEqual(w.target_write_schema_version, 2)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "running")
        self.assertEqual(c.bootstrap_session_id, "s1")
        self.assertEqual(c.bootstrap_generation, 3)

    def test_begin_atomic_missing_session_refused(self):
        db = _session(); _seed_writer(db, requested_mode="halt"); _seed_cutover(db)
        with patch.object(A, "IMAGE_MAX_WRITER_PROTOCOL", 2):
            argv = ["--apply", "--i-understand-this-flips-production", "--rds-snapshot-confirmed",
                    "--quiesce-confirmed", "--ack-global-writer-mode-scope",
                    "--required-protocol", "2", "--target-schema", "2"]
            out = A.AtomicFxActivator(db, quiesce_boundary=_PassBoundary()).run(_parse(argv))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("--session-id", out.message)

    def test_begin_atomic_plan_surfaces_machine_gate(self):
        # review #8: dry-run plan(default fail-closed boundary)이 QuiesceBoundary machine gate를 blocking에 표시
        db = _session(); _seed_writer(db, requested_mode="halt"); _seed_cutover(db)
        with patch.object(A, "IMAGE_MAX_WRITER_PROTOCOL", 2):
            argv = ["--required-protocol", "2", "--target-schema", "2", "--session-id", "s1",
                    "--quiesce-confirmed", "--ack-global-writer-mode-scope"]  # no --apply
            out = A.AtomicFxActivator(db).run(_parse(argv))  # default fail-closed boundary
        self.assertEqual(out.exit_code, A._EXIT_OK)
        self.assertEqual(out.status, "plan")
        self.assertIn("QuiesceBoundary machine-gate", out.message)


class TestExpectedGuards(unittest.TestCase):
    """review #10: 4개 expected-* fence + incident-halt guard 전부 커버 (mismatch refuse + mutation 0)."""

    def test_expected_writer_epoch_mismatch_refused(self):
        db = _session(); _seed_writer(db, requested_mode="halt", activation_epoch=0)
        _seed_cutover(db, status="idle", generation=2)
        out = A.AtomicFxActivator(db).run(_parse(["--expected-writer-epoch", "5"]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("expected-state guard mismatch", out.message)
        self.assertIn("--expected-writer-epoch", out.message)

    def test_expected_cutover_generation_mismatch_refused(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        out = A.AtomicFxActivator(db).run(_parse(["--expected-cutover-generation", "9"]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("--expected-cutover-generation", out.message)

    def test_expected_cutover_status_mismatch_refused(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        out = A.AtomicFxActivator(db).run(_parse(["--expected-cutover-status", "idle"]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("--expected-cutover-status", out.message)

    def test_incident_halt_expected_generation_mismatch_refused(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2, mode_generation=6)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        argv = ["--apply", "--phase", "incident-halt", "--i-understand-this-flips-production",
                "--confirm-incident-halt", "--expected-writer-generation", "9"]
        out = A.AtomicFxActivator(db).run(_parse(argv))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("--expected-writer-generation", out.message)
        w = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(w.requested_mode, WriterMode.ATOMIC)  # mutation 0


# ────────────────────────────── verify phase ──────────────────────────────
class TestVerify(unittest.TestCase):
    def _argv(self):
        return ["--apply", "--i-understand-this-flips-production", "--rds-snapshot-confirmed"]

    def test_verify_pass_advances(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        with patch.object(A, "load_fx_topic_payload_with_revisions", _complete):
            out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "verified")
        self.assertEqual(c.bootstrap_generation, 4)

    def test_verify_incomplete_fails_no_advance(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        with patch.object(A, "load_fx_topic_payload_with_revisions", _incomplete):
            out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_FAILED)
        self.assertIn("self-verify", out.message)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "running")  # no advance

    def test_verify_empty_present_fails(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        with patch.object(A, "load_fx_topic_payload_with_revisions", _empty):
            out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_FAILED)

    def test_verify_loader_exception_failed_no_advance(self):
        # review #11: loader 예외 → _failed (raw traceback 아님) + status 불변
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)

        def boom(db_, asset):
            raise RuntimeError("redis down")

        with patch.object(A, "load_fx_topic_payload_with_revisions", boom):
            out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_FAILED)
        self.assertIn("incident-halt", out.message)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "running")  # no advance

    def test_verify_null_session_torn_failclosed(self):
        # review #1 integration: running + NULL session(torn) → resolve FAIL_CLOSED (advance 안 함)
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id=None, generation=3)
        out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "running")  # no mutation


# ────────────────────────────── finalize phase ──────────────────────────────
class TestFinalize(unittest.TestCase):
    def _argv(self):
        return ["--apply", "--i-understand-this-flips-production", "--rds-snapshot-confirmed"]

    def test_finalize_completes_one_tx(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="verified", session_id="s1", generation=4)
        with patch.object(A, "load_fx_topic_payload_with_revisions", _complete):
            out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "completed")
        self.assertEqual(c.bootstrap_generation, 5)
        for a in db.query(AtomicCutoverAsset).all():
            self.assertEqual(a.publish_state, "ready")
            self.assertEqual(a.membership_version, 1)
            self.assertIsNotNone(a.ready_revision_vector)

    def test_finalize_completeness_fail_no_mutation(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="verified", session_id="s1", generation=4)
        with patch.object(A, "load_fx_topic_payload_with_revisions", _incomplete):
            out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_FAILED)
        self.assertIn("completeness", out.message)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "verified")  # no advance
        for a in db.query(AtomicCutoverAsset).all():
            self.assertEqual(a.publish_state, "blocked")

    def test_finalize_rollback_on_late_flip_failure(self):
        # review #2: eur-krw(마지막)을 stale-ready로 → usd/jpy flip이 staged 성공 후 eur 실패 → 전체 rollback.
        # 직접 _do_finalize 호출(resolve 우회 — torn state 모의). load-bearing: staged 성공 flip의 원복 검증.
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="verified", session_id="s1", generation=4, mixed_ready={"eur-krw"})
        st = A.read_control_state(db)
        with patch.object(A, "load_fx_topic_payload_with_revisions", _complete):
            out = A.AtomicFxActivator(db)._do_finalize(st)
        self.assertEqual(out.exit_code, A._EXIT_FAILED)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "verified")  # status rollback
        # usd/jpy는 staged flip이 rollback돼 blocked로 원복 (단일-tx atomicity 실증)
        usd = db.query(AtomicCutoverAsset).filter(AtomicCutoverAsset.asset == "usd-krw").one()
        jpy = db.query(AtomicCutoverAsset).filter(AtomicCutoverAsset.asset == "jpy-krw").one()
        self.assertEqual(usd.publish_state, "blocked")
        self.assertEqual(jpy.publish_state, "blocked")

    def test_finalize_loader_exception_failed_no_mutation(self):
        # review #11: build_ready_revision_vector loader 예외 → _failed (raw traceback 아님) + mutation 0
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="verified", session_id="s1", generation=4)

        def boom(db_, asset):
            raise RuntimeError("redis down")

        with patch.object(A, "load_fx_topic_payload_with_revisions", boom):
            out = A.AtomicFxActivator(db).run(_parse(self._argv()))
        self.assertEqual(out.exit_code, A._EXIT_FAILED)
        self.assertIn("incident-halt", out.message)
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "verified")
        for a in db.query(AtomicCutoverAsset).all():
            self.assertEqual(a.publish_state, "blocked")


# ────────────────────────────── incident-halt ──────────────────────────────
class TestIncidentHalt(unittest.TestCase):
    def test_incident_halt_applied(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2, mode_generation=6)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        argv = ["--apply", "--phase", "incident-halt", "--i-understand-this-flips-production",
                "--confirm-incident-halt"]
        out = A.AtomicFxActivator(db).run(_parse(argv))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        w = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(w.requested_mode, WriterMode.HALT)
        self.assertEqual(w.mode_generation, 7)
        self.assertEqual(w.activation_epoch, 1)  # 보존 (§3 atomic·halt→legacy 금지)

    def test_incident_halt_requires_atomic(self):
        db = _session(); _seed_writer(db, requested_mode="legacy"); _seed_cutover(db)
        argv = ["--apply", "--phase", "incident-halt", "--i-understand-this-flips-production",
                "--confirm-incident-halt"]
        out = A.AtomicFxActivator(db).run(_parse(argv))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("writer=atomic", out.message)

    def test_incident_halt_missing_confirm_refused(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        argv = ["--apply", "--phase", "incident-halt", "--i-understand-this-flips-production"]
        out = A.AtomicFxActivator(db).run(_parse(argv))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("--confirm-incident-halt", out.message)

    def test_incident_halt_dryrun_plan(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="running", session_id="s1", generation=3)
        out = A.AtomicFxActivator(db).run(_parse(["--phase", "incident-halt"]))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        self.assertEqual(out.status, "plan")
        w = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).one()
        self.assertEqual(w.requested_mode, WriterMode.ATOMIC)  # mutation 0


# ────────────────────────────── verify-only (read-only) ──────────────────────────────
class TestVerifyOnly(unittest.TestCase):
    def test_verify_only_no_mutation(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="completed", session_id="s1", generation=5, asset_state="ready")
        with patch.object(A, "load_fx_topic_payload_with_revisions", _complete):
            out = A.AtomicFxActivator(db).run(_parse(["--phase", "verify-only"]))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        self.assertEqual(out.status, "verify-only")
        self.assertTrue(out.detail["all_ok"])
        c = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one()
        self.assertEqual(c.bootstrap_status, "completed")  # 불변


# ────────────────────────────── NOOP ──────────────────────────────
class TestNoop(unittest.TestCase):
    def test_noop_when_completed_ready(self):
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="completed", session_id="s1", generation=5, asset_state="ready")
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertEqual(out.exit_code, A._EXIT_OK)
        self.assertEqual(out.status, "noop")

    def test_completed_stale_membership_failclosed_not_noop(self):
        # cross-check #1: completed + ready지만 stale membership → NOOP 아님 → FAIL_CLOSED(refused)
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=2)
        _seed_cutover(db, status="completed", session_id="s1", generation=5,
                      asset_state="ready", ready_membership=99)
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)
        self.assertIn("incident", out.message)

    def test_completed_schema_below_floor_failclosed(self):
        # cross-check #2: completed atomic이지만 schema<floor(corrupt) → FAIL_CLOSED
        db = _session(); _seed_writer(db, requested_mode="atomic", activation_epoch=1,
                                      target_write_schema_version=1)
        _seed_cutover(db, status="completed", session_id="s1", generation=5, asset_state="ready")
        out = A.AtomicFxActivator(db).run(_parse([]))
        self.assertEqual(out.exit_code, A._EXIT_REFUSED)


# ────────────────────────────── QuiesceBoundary default ──────────────────────────────
class TestQuiesceBoundary(unittest.TestCase):
    def test_default_boundary_fail_closed(self):
        db = _session(); _seed_writer(db); _seed_cutover(db)
        act = A.AtomicFxActivator(db)
        self.assertFalse(act.quiesce_boundary.confirm_quiesced())

    def test_failclosed_stub_returns_false(self):
        self.assertFalse(A._FailClosedQuiesceBoundary().confirm_quiesced())


# ────────────────────────────── dormancy / script safety ──────────────────────────────
class TestScriptSafetyAST(unittest.TestCase):
    """scripts/는 app/ dormancy trip-wire 범위 밖 → 이 파일 자체에 구조적 safety guard.

    activation command가 (a) live publish/broadcast 함수를 **직접 호출**하지 않고(오직 control table CAS만),
    (b) 어떤 app/ 모듈도 이 script를 import하지 않음(scripts/ 전용 caller)을 잠근다.
    """

    _FORBIDDEN_CALLS = frozenset({
        "publish_topic", "publish_topic_detailed", "broadcast_rates_once",
        "safe_publish_all_fx_snapshots", "safe_publish_fx_snapshot",
        "_publish_fx_snapshot", "_default_publish_fx_snapshot",
    })

    def _script_tree(self):
        src = (Path(__file__).resolve().parent.parent / "scripts" / "activate_atomic_fx.py").read_text(
            encoding="utf-8")
        return ast.parse(src)

    def test_no_live_publish_or_broadcast_call(self):
        tree = self._script_tree()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                self.assertNotIn(name, self._FORBIDDEN_CALLS,
                                 f"activation command가 live publish/broadcast({name})를 직접 호출 — "
                                 "command는 control table CAS만 해야 함")

    _DENY_IMPORT_SUBSTR = ("fx_topic_publisher", "fx_topic_trigger", "topic_dispatcher",
                           "broadcast", "app.main")

    def test_script_imports_no_publish_or_broadcast_module(self):
        # review #6: leaf-call-name 검사 보완 — script가 publish/broadcast 모듈을 **import**조차 안 함.
        # symbol rename / dynamic dispatch(getattr)에도 robust한 import-level dormancy 잠금
        # (no-importer trip-wire와 대칭). control table CAS + read-only loader + SessionLocal만 import해야.
        tree = self._script_tree()
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(s in alias.name for s in self._DENY_IMPORT_SUBSTR):
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and any(s in node.module for s in self._DENY_IMPORT_SUBSTR):
                    offenders.append(node.module)
        self.assertEqual(offenders, [],
                         f"activation command가 publish/broadcast 모듈을 import: {offenders}")

    def test_no_app_module_imports_script(self):
        # 어떤 app/*.py도 activate_atomic_fx를 **import**하지 않음 (scripts/ 전용 dormant caller).
        # AST import 노드만 검사 — atomic_write_durable.py docstring의 "scripts/activate_atomic_fx.py"
        # 같은 문서 언급(sanctioned caller 표기)은 import가 아니므로 통과.
        app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
        offenders = []
        for py in app_dir.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any("activate_atomic_fx" in alias.name for alias in node.names):
                        offenders.append(py.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module and "activate_atomic_fx" in node.module:
                        offenders.append(py.name)
        self.assertEqual(offenders, [], f"app/ 모듈이 activation command를 import: {offenders}")


if __name__ == "__main__":
    unittest.main()
