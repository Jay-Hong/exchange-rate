"""P1b C6-quiesce Q2a — quiesce-evidence schema 단위 테스트 (§9 step6 개정, dormant).

AtomicQuiesceSession/AtomicQuiesceAppAck CHECK 강제(전수) + partial-unique single-open + UNIQUE(session,boot)
+ dry-run DDL(양 dialect, CHECK+index) + **no-seed**(A1/C6-1 singleton seed와 의도적 divergence) +
dormancy(live module이 두 model 미참조). CAS brick은 Q2b. boundary verdict는 Q4.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import AtomicQuiesceAppAck, AtomicQuiesceSession

migrate_quiesce = importlib.import_module("scripts.migrate_atomic_quiesce")

_TS = datetime(2026, 6, 20, 6, 45, 0)  # naive UTC


def _session():
    # in-memory sqlite + 두 quiesce table만 생성 (CHECK + partial-unique index 포함). sqlite는 CHECK enforce.
    engine = create_engine("sqlite:///:memory:")
    AtomicQuiesceSession.__table__.create(bind=engine)
    AtomicQuiesceAppAck.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


class TestQuiesceSessionChecks(unittest.TestCase):

    def _add(self, db, **over):
        base = dict(session_id="q1", quiesce_row_format_version=1, halt_mode_generation=1,
                    halt_committed_at=_TS, state="open")
        base.update(over)
        db.add(AtomicQuiesceSession(**base))
        db.commit()

    def test_valid_row_ok(self):
        db = _session()
        self._add(db)
        self.assertEqual(db.query(AtomicQuiesceSession).count(), 1)

    def test_format_version_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, quiesce_row_format_version=0)

    def test_bad_state_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, state="bogus")

    def test_negative_halt_generation_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, halt_mode_generation=-1)

    def test_session_id_unique(self):
        db = _session()
        self._add(db, session_id="dup")
        with self.assertRaises(IntegrityError):
            self._add(db, session_id="dup", state="consumed")  # 다른 state라도 session_id 충돌

    def test_single_open_partial_unique(self):
        # 동시 open 1개만 — partial-unique(state='open'). 2번째 open(다른 session_id)도 거부.
        db = _session()
        self._add(db, session_id="s1", state="open")
        with self.assertRaises(IntegrityError):
            self._add(db, session_id="s2", state="open")

    def test_consumed_not_in_partial_index(self):
        # consumed/aborted는 partial index 밖 — 여러 개 공존 가능 (open 1개와 무관).
        db = _session()
        self._add(db, session_id="s1", state="consumed")
        self._add(db, session_id="s2", state="consumed")
        self._add(db, session_id="s3", state="aborted")
        self._add(db, session_id="s4", state="open")  # open은 여전히 1개 가능
        self.assertEqual(db.query(AtomicQuiesceSession).count(), 4)


class TestQuiesceAppAckChecks(unittest.TestCase):

    def _add(self, db, **over):
        base = dict(session_id="q1", boot_id="boot-1", process_started_at=_TS,
                    observed_writer_generation=1, observed_enforced_action="halt")
        base.update(over)
        db.add(AtomicQuiesceAppAck(**base))
        db.commit()

    def test_valid_row_ok(self):
        db = _session()
        self._add(db)
        self.assertEqual(db.query(AtomicQuiesceAppAck).one().observed_enforced_action, "halt")

    def test_observed_action_enum(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, observed_enforced_action="weird")

    def test_observed_action_legacy_atomic_allowed_at_schema(self):
        # schema는 enum-of-3 허용 (corrupt label도 typed). 'halt'만 write하는 fail-closed는 brick(Q2b).
        db = _session()
        self._add(db, boot_id="b-legacy", observed_enforced_action="legacy")
        self._add(db, boot_id="b-atomic", observed_enforced_action="atomic")
        self.assertEqual(db.query(AtomicQuiesceAppAck).count(), 2)

    def test_negative_observed_generation_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, observed_writer_generation=-1)

    def test_negative_queue_size_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, queue_size=-1)

    def test_queue_size_nullable_ok(self):
        db = _session()
        self._add(db, queue_size=None)
        self.assertIsNone(db.query(AtomicQuiesceAppAck).one().queue_size)

    def test_session_boot_unique(self):
        db = _session()
        self._add(db, session_id="q1", boot_id="boot-1")
        with self.assertRaises(IntegrityError):
            self._add(db, session_id="q1", boot_id="boot-1")

    def test_multi_boot_same_session_ok(self):
        # per-boot multi-row: 같은 session, 다른 boot_id는 공존 (recreate retry 이력).
        db = _session()
        self._add(db, session_id="q1", boot_id="boot-1")
        self._add(db, session_id="q1", boot_id="boot-2")
        self.assertEqual(db.query(AtomicQuiesceAppAck).count(), 2)


class TestDryRunDDL(unittest.TestCase):

    def test_ddl_contains_both_tables_checks_and_partial_index(self):
        for dialect in ("postgresql", "sqlite"):
            ddl = migrate_quiesce.get_dry_run_ddl(dialect)
            self.assertIn("atomic_quiesce_session", ddl, dialect)
            self.assertIn("atomic_quiesce_app_ack", ddl, dialect)
            self.assertIn("ck_atomic_quiesce_session_state", ddl, dialect)
            self.assertIn("ck_atomic_quiesce_app_ack_observed_action", ddl, dialect)
            # partial-unique single-open index + where 절
            self.assertIn("uq_atomic_quiesce_session_single_open", ddl, dialect)
            self.assertIn("uq_atomic_quiesce_app_ack_session_boot", ddl, dialect)
            self.assertIn("WHERE state = 'open'", ddl, dialect)


class TestNoSeedDivergence(unittest.TestCase):
    """quiesce table은 event/history surface — A1/C6-1 singleton seed와 의도적 divergence (lock)."""

    def test_migrate_has_no_seed_function(self):
        # migrate_atomic_cutover는 seed_cutover_* 보유. quiesce migrate는 seed 없음(create-only).
        seed_attrs = [n for n in dir(migrate_quiesce) if n.startswith("seed") or n == "_seed"]
        self.assertEqual(seed_attrs, [], f"quiesce migrate에 seed 함수 존재 — event table은 seed 없어야: {seed_attrs}")

    def test_migrate_exposes_create_and_dryrun(self):
        self.assertTrue(hasattr(migrate_quiesce, "apply_live"))
        self.assertTrue(hasattr(migrate_quiesce, "get_dry_run_ddl"))

    def test_dry_run_ddl_has_no_insert(self):
        for dialect in ("postgresql", "sqlite"):
            self.assertNotIn("INSERT", migrate_quiesce.get_dry_run_ddl(dialect).upper(), dialect)


class TestIndexesCreated(unittest.TestCase):
    """partial-unique single-open + UNIQUE index가 실제 생성됨 (migrate _verify_indexes drift 검출 대상)."""

    def test_session_indexes_present(self):
        from sqlalchemy import create_engine, inspect
        engine = create_engine("sqlite:///:memory:")
        AtomicQuiesceSession.__table__.create(bind=engine)
        names = {ix["name"] for ix in inspect(engine).get_indexes("atomic_quiesce_session")}
        self.assertIn("uq_atomic_quiesce_session_id", names)
        self.assertIn("uq_atomic_quiesce_session_single_open", names)

    def test_app_ack_indexes_present(self):
        from sqlalchemy import create_engine, inspect
        engine = create_engine("sqlite:///:memory:")
        AtomicQuiesceAppAck.__table__.create(bind=engine)
        names = {ix["name"] for ix in inspect(engine).get_indexes("atomic_quiesce_app_ack")}
        self.assertIn("uq_atomic_quiesce_app_ack_session_boot", names)

    def test_verify_indexes_detects_missing(self):
        # migrate._verify_indexes: 누락 index 검출 (sqlite는 warn, abort 안 함 → no raise).
        from sqlalchemy import create_engine, inspect
        engine = create_engine("sqlite:///:memory:")
        AtomicQuiesceSession.__table__.create(bind=engine)
        inspector = inspect(engine)
        # 존재하는 index 집합 → OK (no raise)
        migrate_quiesce._verify_indexes(
            inspector, "atomic_quiesce_session",
            {"uq_atomic_quiesce_session_single_open"}, "sqlite",
        )
        # 없는 index → sqlite는 warn만 (raise 안 함)
        migrate_quiesce._verify_indexes(
            inspector, "atomic_quiesce_session", {"ix_does_not_exist"}, "sqlite",
        )


class TestDormancy(unittest.TestCase):
    """C6-quiesce Q2a dormant — models.py(정의 site) 외 어떤 live app/ 모듈도 두 quiesce model 미참조.

    Q2b가 atomic_quiesce_durable.py(CAS brick)를 추가하면 _ALLOWED에 더해진다. scripts/는 app/ scan 밖.
    """

    _MODELS = ("AtomicQuiesceSession", "AtomicQuiesceAppAck")
    # 정의 site(models.py) + Q2b dormant CAS island(atomic_quiesce_durable, live caller 0 — 자체 no-importer
    # trip-wire가 보장). live reader는 여전히 차단.
    _ALLOWED = frozenset({"models.py", "atomic_quiesce_durable.py"})

    def test_no_live_module_references_quiesce_models(self):
        import app.models as m
        app_dir = pathlib.Path(m.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._ALLOWED:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("models"):
                    for a in node.names:
                        self.assertNotIn(a.name, self._MODELS, f"{py.name}: imports {a.name} — Q2a dormant 위반")
                elif isinstance(node, ast.Attribute) and node.attr in self._MODELS:
                    self.fail(f"{py.name}: refs {node.attr} — Q2a dormant 위반")
                elif isinstance(node, ast.Name) and node.id in self._MODELS:
                    self.fail(f"{py.name}: refs {node.id} — Q2a dormant 위반")


if __name__ == "__main__":
    unittest.main()
