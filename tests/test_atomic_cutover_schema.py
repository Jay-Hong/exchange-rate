"""P1b C6-1 — cutover-control schema 단위 테스트 (§15/§18, dormant).

AtomicCutoverControl/AtomicCutoverAsset CHECK 강제(전수) + insert-if-missing seed 멱등(readiness 후
rerun이 state 리셋 안 함) + dry-run DDL + dormancy(live module이 두 model 미참조). C6-1은 schema+seed만.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import unittest

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import AtomicCutoverAsset, AtomicCutoverControl

migrate_cutover = importlib.import_module("scripts.migrate_atomic_cutover")


def _session():
    # in-memory sqlite + 두 cutover table만 생성 (CHECK 포함). SQLite는 CHECK 기본 enforce.
    engine = create_engine("sqlite:///:memory:")
    AtomicCutoverControl.__table__.create(bind=engine)
    AtomicCutoverAsset.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


class TestCutoverControlChecks(unittest.TestCase):

    def _add(self, db, **over):
        base = dict(id=1, cutover_row_format_version=1, bootstrap_generation=0, bootstrap_status="idle")
        base.update(over)
        db.add(AtomicCutoverControl(**base))
        db.commit()

    def test_valid_row_ok(self):
        db = _session()
        self._add(db)
        self.assertEqual(db.query(AtomicCutoverControl).count(), 1)

    def test_valid_leased_row_ok(self):
        db = _session()
        from datetime import datetime, timezone
        self._add(db, lease_owner="worker-1", lease_expiry=datetime(2026, 6, 19, tzinfo=timezone.utc))
        self.assertEqual(db.query(AtomicCutoverControl).one().lease_owner, "worker-1")

    def test_singleton_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, id=2)

    def test_bad_status_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, bootstrap_status="bogus")

    def test_negative_generation_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, bootstrap_generation=-1)

    def test_format_version_check(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, cutover_row_format_version=0)

    def test_lease_unpaired_owner_only(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, lease_owner="w", lease_expiry=None)

    def test_lease_unpaired_expiry_only(self):
        db = _session()
        from datetime import datetime, timezone
        with self.assertRaises(IntegrityError):
            self._add(db, lease_owner=None, lease_expiry=datetime(2026, 6, 19, tzinfo=timezone.utc))


class TestCutoverAssetChecks(unittest.TestCase):

    def _add(self, db, **over):
        base = dict(asset="usd-krw", publish_state="blocked")
        base.update(over)
        db.add(AtomicCutoverAsset(**base))
        db.commit()

    def test_valid_blocked_ok(self):
        db = _session()
        self._add(db)
        self.assertEqual(db.query(AtomicCutoverAsset).one().publish_state, "blocked")

    def test_valid_ready_ok(self):
        db = _session()
        self._add(db, publish_state="ready", ready_revision_vector='{"kb":"x"}', membership_version=1)
        self.assertEqual(db.query(AtomicCutoverAsset).one().publish_state, "ready")

    def test_bad_publish_state(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, publish_state="weird")

    def test_bad_asset(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, asset="usdt-krw")

    def test_membership_nonpositive(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, publish_state="ready", ready_revision_vector='{"kb":"x"}', membership_version=0)

    def test_blocked_with_vector_inconsistent(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, publish_state="blocked", ready_revision_vector='{"kb":"x"}', membership_version=1)

    def test_ready_without_vector_inconsistent(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, publish_state="ready", ready_revision_vector=None, membership_version=1)

    def test_ready_empty_vector_inconsistent(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, publish_state="ready", ready_revision_vector="", membership_version=1)

    def test_ready_without_membership_inconsistent(self):
        db = _session()
        with self.assertRaises(IntegrityError):
            self._add(db, publish_state="ready", ready_revision_vector='{"kb":"x"}', membership_version=None)


class TestSeedInsertIfMissing(unittest.TestCase):

    def test_control_seed_idempotent_no_reset(self):
        db = _session()
        self.assertEqual(migrate_cutover.seed_cutover_control(db), "inserted")
        # 수동으로 status 전진 (readiness 시뮬)
        row = db.query(AtomicCutoverControl).one()
        row.bootstrap_status = "verified"
        db.commit()
        # 재seed → exists + 값 보존(리셋 금지)
        self.assertEqual(migrate_cutover.seed_cutover_control(db), "exists")
        self.assertEqual(db.query(AtomicCutoverControl).one().bootstrap_status, "verified")

    def test_assets_seed_idempotent_no_reset(self):
        db = _session()
        r1 = migrate_cutover.seed_cutover_assets(db)
        self.assertEqual(set(r1.values()), {"inserted"})
        self.assertEqual(set(r1), {"usd-krw", "jpy-krw", "eur-krw"})
        # usd-krw를 ready로 전진
        row = db.query(AtomicCutoverAsset).filter(AtomicCutoverAsset.asset == "usd-krw").one()
        row.publish_state = "ready"
        row.ready_revision_vector = '{"kb":"x"}'
        row.membership_version = 1
        db.commit()
        # 재seed → 전부 exists + ready 보존
        r2 = migrate_cutover.seed_cutover_assets(db)
        self.assertEqual(set(r2.values()), {"exists"})
        self.assertEqual(
            db.query(AtomicCutoverAsset).filter(AtomicCutoverAsset.asset == "usd-krw").one().publish_state,
            "ready",
        )


class TestDryRunDDL(unittest.TestCase):

    def test_ddl_contains_both_tables_and_checks(self):
        for dialect in ("postgresql", "sqlite"):
            ddl = migrate_cutover.get_dry_run_ddl(dialect)
            self.assertIn("atomic_cutover_control", ddl, dialect)
            self.assertIn("atomic_cutover_asset", ddl, dialect)
            self.assertIn("ck_atomic_cutover_control_singleton", ddl, dialect)
            self.assertIn("ck_atomic_cutover_asset_payload_consistency", ddl, dialect)


class TestDormancy(unittest.TestCase):
    """C6-1 dormant — models.py(정의 site) 외 어떤 live app/ 모듈도 두 cutover model 미참조."""

    _MODELS = ("AtomicCutoverControl", "AtomicCutoverAsset")
    # 정의 site(models.py) + C6-2+ dormant island(미작성) 제외. 현재는 models.py만.
    _ALLOWED = frozenset({"models.py"})

    def test_no_live_module_references_cutover_models(self):
        import app.models as m
        app_dir = pathlib.Path(m.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._ALLOWED:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("models"):
                    for a in node.names:
                        self.assertNotIn(a.name, self._MODELS, f"{py.name}: imports {a.name} — C6-1 dormant 위반")
                elif isinstance(node, ast.Attribute) and node.attr in self._MODELS:
                    self.fail(f"{py.name}: refs {node.attr} — C6-1 dormant 위반")
                elif isinstance(node, ast.Name) and node.id in self._MODELS:
                    self.fail(f"{py.name}: refs {node.id} — C6-1 dormant 위반")


if __name__ == "__main__":
    unittest.main()
