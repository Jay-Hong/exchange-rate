"""P1b C6-8a — writer-mode durable CAS bricks 단위 테스트 (atomic_write_durable.py, §8/§9, dormant).

cas_request_halt(legacy→halt) + cas_activate_atomic(halt→atomic §8 one-shot) — APPLIED/CAS_LOST/
PRECONDITION_FAILED + monotonic fence + param 검증 + caller-commits(staged, 미commit) + 전체 시퀀스 +
dormancy(no live importer + no scheduling).
"""
from __future__ import annotations

import ast
import pathlib
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_write_durable as awd
from app.atomic_cutover_durable import CasResult
from app.atomic_write_control import (
    ATOMIC_SCHEMA_FLOOR,
    CONTROL_ROW_FORMAT_VERSION,
    WriterMode,
    read_control_row,
)
from app.atomic_write_durable import cas_activate_atomic, cas_request_halt
from app.models import AtomicWriteControl


def _session():
    engine = create_engine("sqlite:///:memory:")
    AtomicWriteControl.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


def _seed(db, *, requested_mode="legacy", mode_generation=0, activation_epoch=0,
          target_write_schema_version=1, required_writer_protocol=1,
          control_row_format_version=CONTROL_ROW_FORMAT_VERSION):
    db.add(AtomicWriteControl(
        id=1, control_row_format_version=control_row_format_version,
        target_write_schema_version=target_write_schema_version,
        required_writer_protocol=required_writer_protocol, activation_epoch=activation_epoch,
        mode_generation=mode_generation, requested_mode=requested_mode, activated_at=None,
    ))
    db.commit()


class TestRequestHalt(unittest.TestCase):

    def test_legacy_to_halt_applied(self):
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=4)
        self.assertIs(cas_request_halt(db, expected_generation=4), CasResult.APPLIED)
        db.commit()
        row = read_control_row(db)
        self.assertEqual(row.requested_mode, WriterMode.HALT)
        self.assertEqual(row.mode_generation, 5)            # ++

    def test_wrong_generation_cas_lost(self):
        # 관측 gen보다 row가 앞서 있음 → CAS_LOST (concurrent/이미 적용)
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=7)
        self.assertIs(cas_request_halt(db, expected_generation=3), CasResult.CAS_LOST)

    def test_not_legacy_precondition_failed(self):
        # 이미 halt(또는 atomic) → requested_mode!=legacy → PRECONDITION_FAILED (gen 일치라도)
        db = _session(); _seed(db, requested_mode="halt", mode_generation=4)
        self.assertIs(cas_request_halt(db, expected_generation=4), CasResult.PRECONDITION_FAILED)

    def test_no_row_precondition_failed(self):
        db = _session()  # seed 안 함
        self.assertIs(cas_request_halt(db, expected_generation=0), CasResult.PRECONDITION_FAILED)


class TestActivateAtomic(unittest.TestCase):

    def test_halt_to_atomic_applied(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5, activation_epoch=0)
        r = cas_activate_atomic(
            db, expected_generation=5, expected_epoch=0,
            required_writer_protocol=2, target_write_schema_version=2,
        )
        self.assertIs(r, CasResult.APPLIED)
        db.commit()
        row = read_control_row(db)
        self.assertEqual(row.requested_mode, WriterMode.ATOMIC)
        self.assertEqual(row.activation_epoch, 1)           # ++
        self.assertEqual(row.mode_generation, 6)            # ++
        self.assertEqual(row.required_writer_protocol, 2)
        self.assertEqual(row.target_write_schema_version, 2)
        self.assertIsNotNone(row.activated_at)              # set

    def test_target_schema_below_floor_precondition(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=1),
            CasResult.PRECONDITION_FAILED,
        )
        # row 불변 (UPDATE 전 차단)
        self.assertEqual(read_control_row(db).requested_mode, WriterMode.HALT)

    def test_protocol_below_seed_precondition(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                                required_writer_protocol=0, target_write_schema_version=ATOMIC_SCHEMA_FLOOR),
            CasResult.PRECONDITION_FAILED,
        )

    def test_bool_params_rejected(self):
        # bool은 int 서브클래스 — 명시 배제
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=True),
            CasResult.PRECONDITION_FAILED,
        )

    def test_not_halt_precondition_failed(self):
        # legacy에서 직접 activate 시도 → requested_mode!=halt → PRECONDITION_FAILED (legacy→halt 선행 강제)
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=5)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=2),
            CasResult.PRECONDITION_FAILED,
        )

    def test_generation_advanced_cas_lost(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=9, activation_epoch=0)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=3, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=2),
            CasResult.CAS_LOST,
        )

    def test_epoch_advanced_cas_lost(self):
        # 이미 한 번 activate된 상태(epoch=1, atomic) → 재실행 시 epoch 전진 → CAS_LOST (idempotent already-done)
        db = _session(); _seed(db, requested_mode="halt", mode_generation=6, activation_epoch=1)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=6, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=2),
            CasResult.CAS_LOST,
        )

    def test_now_param_used(self):
        from datetime import datetime, timezone
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5)
        fixed = datetime(2026, 6, 19, 0, 0, 0, tzinfo=timezone.utc)
        cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                            required_writer_protocol=2, target_write_schema_version=2, now=fixed)
        db.commit()
        # sqlite는 tz-naive로 저장 — 값 존재 + 날짜 일치만 확인
        self.assertEqual(read_control_row(db).activated_at.year, 2026)


class TestCallerCommits(unittest.TestCase):

    def test_applied_is_staged_not_committed(self):
        # APPLIED는 staged(미commit) — rollback하면 원복 (caller-commits 계약)
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=4)
        self.assertIs(cas_request_halt(db, expected_generation=4), CasResult.APPLIED)
        db.rollback()
        row = read_control_row(db)
        self.assertEqual(row.requested_mode, WriterMode.LEGACY)   # 원복
        self.assertEqual(row.mode_generation, 4)

    def test_full_sequence_legacy_halt_atomic(self):
        # legacy → halt(commit) → atomic(commit): mode_generation 2회 증가(§9:87-89)
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=0, activation_epoch=0)
        self.assertIs(cas_request_halt(db, expected_generation=0), CasResult.APPLIED)
        db.commit()
        self.assertEqual(read_control_row(db).mode_generation, 1)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=1, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=2),
            CasResult.APPLIED,
        )
        db.commit()
        row = read_control_row(db)
        self.assertEqual(row.requested_mode, WriterMode.ATOMIC)
        self.assertEqual(row.mode_generation, 2)            # halt +1, atomic +1
        self.assertEqual(row.activation_epoch, 1)


class TestFormatFence(unittest.TestCase):
    """codex F1: format mismatch row는 mutate 금지 (compute_effective_mode line 117 fail-closed 정신)."""

    def test_halt_format_mismatch_precondition(self):
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=4, control_row_format_version=99)
        self.assertIs(cas_request_halt(db, expected_generation=4), CasResult.PRECONDITION_FAILED)
        self.assertEqual(read_control_row(db).requested_mode, WriterMode.LEGACY)   # 불변

    def test_activate_format_mismatch_precondition(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5, control_row_format_version=99)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=2),
            CasResult.PRECONDITION_FAILED,
        )
        self.assertEqual(read_control_row(db).requested_mode, WriterMode.HALT)      # 불변

    def test_format_mismatch_precedence_over_gen_advance(self):
        # codex P2: format mismatch + gen 전진 동시 → format이 non-retryable이라 PRECONDITION_FAILED (CAS_LOST 아님)
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=9, control_row_format_version=99)
        self.assertIs(cas_request_halt(db, expected_generation=3), CasResult.PRECONDITION_FAILED)


class TestFenceParamValidation(unittest.TestCase):
    """codex F3: expected_generation/expected_epoch non-bool int>=0 검증 (bool 슬립 + None/str TypeError 차단)."""

    def test_halt_none_generation(self):
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=4)
        self.assertIs(cas_request_halt(db, expected_generation=None), CasResult.PRECONDITION_FAILED)

    def test_halt_str_generation(self):
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=4)
        self.assertIs(cas_request_halt(db, expected_generation="4"), CasResult.PRECONDITION_FAILED)

    def test_halt_bool_generation_rejected(self):
        # True==1: 검증 없으면 gen=1 row에 슬립 → 명시 배제
        db = _session(); _seed(db, requested_mode="legacy", mode_generation=1)
        self.assertIs(cas_request_halt(db, expected_generation=True), CasResult.PRECONDITION_FAILED)
        self.assertEqual(read_control_row(db).requested_mode, WriterMode.LEGACY)   # 불변

    def test_activate_bool_epoch_rejected(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5, activation_epoch=1)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=5, expected_epoch=True,
                                required_writer_protocol=2, target_write_schema_version=2),
            CasResult.PRECONDITION_FAILED,
        )

    def test_activate_negative_generation(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=-1, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=2),
            CasResult.PRECONDITION_FAILED,
        )


class TestActivatedAtNormalization(unittest.TestCase):
    """codex F4: explicit now(aware)를 naive UTC로 정규화 (컬럼 tz-naive 정합) + non-datetime 거부."""

    def test_aware_now_stored_naive(self):
        from datetime import datetime, timezone, timedelta
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5)
        # KST aware 09:00 → UTC naive 00:00
        kst = timezone(timedelta(hours=9))
        aware = datetime(2026, 6, 19, 9, 0, 0, tzinfo=kst)
        cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                            required_writer_protocol=2, target_write_schema_version=2, now=aware)
        db.commit()
        stored = read_control_row(db).activated_at
        self.assertIsNone(stored.tzinfo)                 # naive
        self.assertEqual((stored.year, stored.month, stored.day, stored.hour), (2026, 6, 19, 0))  # UTC

    def test_non_datetime_now_rejected(self):
        db = _session(); _seed(db, requested_mode="halt", mode_generation=5)
        self.assertIs(
            cas_activate_atomic(db, expected_generation=5, expected_epoch=0,
                                required_writer_protocol=2, target_write_schema_version=2, now="2026-06-19"),
            CasResult.PRECONDITION_FAILED,
        )


class TestDisambiguateFreshRead(unittest.TestCase):
    """codex F2: _disambiguate가 identity-map stale 대신 fresh read(populate_existing)로 CAS_LOST 정확 분류.

    한 engine + 두 session: A가 row 선load(identity map 캐시) → B가 gen 전진 commit → A의 brick이
    rowcount 0 → _disambiguate가 fresh read로 gen 전진 관측 → CAS_LOST (stale면 PRECONDITION 오분류).
    """

    def _shared_engine(self):
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix="_awd.db")
        os.close(fd)
        engine = create_engine(f"sqlite:///{path}")
        AtomicWriteControl.__table__.create(bind=engine)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return engine

    def test_concurrent_advance_classified_cas_lost(self):
        engine = self._shared_engine()
        Session = sessionmaker(bind=engine)
        sa, sb = Session(), Session()
        self.addCleanup(sa.close); self.addCleanup(sb.close)
        _seed(sa, requested_mode="legacy", mode_generation=0)   # commit
        # A가 row 선load → identity map에 gen=0 캐시 (stale 유발 조건)
        loaded = read_control_row(sa)
        self.assertEqual(loaded.mode_generation, 0)
        # B가 halt로 전진 + commit (gen 0→1)
        self.assertIs(cas_request_halt(sb, expected_generation=0), CasResult.APPLIED)
        sb.commit()
        # A가 같은 expected=0으로 시도 → DB gen=1이라 rowcount 0 → fresh read로 CAS_LOST (stale면 PRECONDITION)
        self.assertIs(cas_request_halt(sa, expected_generation=0), CasResult.CAS_LOST)


class TestDormancy(unittest.TestCase):
    """C6-8a dormant — app/ 어떤 live 모듈도 atomic_write_durable import 0 (island 멤버끼리만)."""

    _ISLAND = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py", "atomic_write_outcome.py",
        "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py", "atomic_reconcile.py",
        "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py",
        "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py",
        "atomic_write_durable.py",
    })

    def test_no_live_module_imports_durable(self):
        app_dir = pathlib.Path(awd.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # exact-tail 매칭 (C6-9a 교훈 — substring sibling false-match 회피)
                if isinstance(node, ast.ImportFrom) and node.module \
                        and node.module.split(".")[-1] == "atomic_write_durable":
                    self.fail(f"{rel}: from atomic_write_durable import — dormant 위반")
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[-1] == "atomic_write_durable":
                            self.fail(f"{rel}: import atomic_write_durable — dormant 위반")

    def test_no_import_time_scheduling(self):
        src = pathlib.Path(awd.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"durable에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
