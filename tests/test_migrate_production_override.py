"""P1b C6-PRE-build-b2 — prod migration override (--allow-production-migration) 단위 테스트.

scripts/migrate_atomic_latest_values.py의 _authorize_production / _release_production_lease + main() combo
guards + app/atomic_migration.py의 _check_apply_target(authorized) / _revalidate_production_authorization
backstop. fail-closed matrix: flag는 필요조건일 뿐 — drain proof(confirm_quiesce_drained) ∧ lease 충족 시만
prod 허용, 어떤 모호함도 BLOCK. dormant(operator-run only) — 모든 경로 in-process(실 prod 무접촉).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_migration as AM
from app.atomic_cutover_durable import (
    CasResult,
    cas_acquire_migration_lease,
    read_cutover_control,
)
from app.atomic_migration import ProductionAuthToken
from app.atomic_quiesce_durable import cas_open_quiesce_session, cas_record_quiesce_app_ack
from app.atomic_write_control import CONTROL_ROW_FORMAT_VERSION, WriterMode
from app.models import (
    AtomicCutoverControl,
    AtomicQuiesceAppAck,
    AtomicQuiesceSession,
    AtomicWriteControl,
    get_utc_now,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate_atomic_latest_values as M  # noqa: E402

_HALT_AT = datetime(2026, 6, 21, 6, 0, 0)
_BOOT_AT = _HALT_AT + timedelta(seconds=30)


def _session():
    engine = create_engine("sqlite:///:memory:")
    for t in (AtomicWriteControl, AtomicQuiesceSession, AtomicQuiesceAppAck, AtomicCutoverControl):
        t.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_drained(db, *, gen=5, with_cutover_row=True):
    """HALT@gen control + open quiesce session + qualifying ACK (+ idle cutover row, lease NULL). 전부 commit."""
    db.add(AtomicWriteControl(
        id=1, control_row_format_version=CONTROL_ROW_FORMAT_VERSION, target_write_schema_version=1,
        required_writer_protocol=1, activation_epoch=0, mode_generation=gen,
        requested_mode=WriterMode.HALT, activated_at=None))
    db.commit()
    assert cas_open_quiesce_session(
        db, session_id="q1", halt_mode_generation=gen, halt_committed_at=_HALT_AT) is CasResult.APPLIED
    db.commit()
    assert cas_record_quiesce_app_ack(
        db, session_id="q1", boot_id="b", process_started_at=_BOOT_AT,
        observed_writer_generation=gen, observed_enforced_action=WriterMode.HALT) is CasResult.APPLIED
    db.commit()
    if with_cutover_row:
        db.add(AtomicCutoverControl(id=1, cutover_row_format_version=1, bootstrap_generation=0,
                                    bootstrap_status="idle", bootstrap_session_id=None))
        db.commit()


def _args(**over):
    base = dict(allow_production_migration=True, apply=True, expected_writer_generation=5,
                lease_ttl_seconds=3600, scope="all", timeout_sec=60.0, max_retries=3)
    base.update(over)
    return argparse.Namespace(**base)


class TestAuthorizeProduction(unittest.TestCase):
    """_authorize_production: 성공 → ProductionAuthToken / BLOCK → int(2). flag necessary-but-not-sufficient."""

    def test_authorize_success(self):
        db = _session(); _seed_drained(db)
        rc = M._authorize_production(db, _args())
        self.assertIsInstance(rc, ProductionAuthToken)
        self.assertTrue(rc.owner.startswith("migrate-"))
        self.assertEqual(rc.expected_generation, 5)
        self.assertEqual(read_cutover_control(db).lease_owner, rc.owner)  # lease 획득됨

    def test_generation_zero_success(self):
        db = _session(); _seed_drained(db, gen=0)
        rc = M._authorize_production(db, _args(expected_writer_generation=0))
        self.assertIsInstance(rc, ProductionAuthToken)
        self.assertEqual(rc.expected_generation, 0)

    def test_ttl_below_ceiling_blocked(self):
        db = _session(); _seed_drained(db)
        rc = M._authorize_production(db, _args(lease_ttl_seconds=100))  # < max_keys×60+120
        self.assertEqual(rc, 2)
        self.assertIsNone(read_cutover_control(db).lease_owner)  # lease 미획득

    def test_drain_not_confirmed_blocked(self):
        # control HALT + cutover row 있으나 quiesce session/ACK 없음 → confirm_quiesce_drained False
        db = _session()
        db.add(AtomicWriteControl(
            id=1, control_row_format_version=CONTROL_ROW_FORMAT_VERSION, target_write_schema_version=1,
            required_writer_protocol=1, activation_epoch=0, mode_generation=5,
            requested_mode=WriterMode.HALT, activated_at=None))
        db.add(AtomicCutoverControl(id=1, cutover_row_format_version=1, bootstrap_generation=0,
                                    bootstrap_status="idle", bootstrap_session_id=None))
        db.commit()
        rc = M._authorize_production(db, _args())
        self.assertEqual(rc, 2)
        self.assertIsNone(read_cutover_control(db).lease_owner)

    def test_generation_mismatch_blocked(self):
        db = _session(); _seed_drained(db, gen=5)
        rc = M._authorize_production(db, _args(expected_writer_generation=4))  # session gen=5
        self.assertEqual(rc, 2)
        self.assertIsNone(read_cutover_control(db).lease_owner)

    def test_lease_held_by_other_blocked(self):
        db = _session(); _seed_drained(db)
        # 다른 owner가 non-expired lease 선점
        self.assertIs(cas_acquire_migration_lease(db, owner="other", now=get_utc_now(), ttl_seconds=3600),
                      CasResult.APPLIED); db.commit()
        rc = M._authorize_production(db, _args())
        self.assertEqual(rc, 2)
        self.assertEqual(read_cutover_control(db).lease_owner, "other")  # 불변

    def test_cutover_row_absent_blocked(self):
        # drain proof OK지만 AtomicCutoverControl row 부재 → cas_acquire PRECONDITION_FAILED (C1 chicken-egg)
        db = _session(); _seed_drained(db, with_cutover_row=False)
        rc = M._authorize_production(db, _args())
        self.assertEqual(rc, 2)


class TestRevalidateProductionAuthorization(unittest.TestCase):
    """_revalidate_production_authorization: control+lease fresh re-read 재검증(TOCTOU backstop). None=통과."""

    def _authed(self, db, *, gen=5, owner="op"):
        _seed_drained(db, gen=gen)
        self.assertIs(cas_acquire_migration_lease(db, owner=owner, now=get_utc_now(), ttl_seconds=3600),
                      CasResult.APPLIED); db.commit()
        return ProductionAuthToken(owner=owner, expected_generation=gen)

    def test_valid_passes(self):
        db = _session(); tok = self._authed(db)
        self.assertIsNone(AM._revalidate_production_authorization(db, tok))

    def test_control_absent_block(self):
        db = _session(); tok = ProductionAuthToken(owner="op", expected_generation=5)  # control 없음
        self.assertIsNotNone(AM._revalidate_production_authorization(db, tok))

    def test_halt_flipped_block(self):
        db = _session(); tok = self._authed(db)
        db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).update(
            {"requested_mode": WriterMode.LEGACY}); db.commit()
        self.assertIsNotNone(AM._revalidate_production_authorization(db, tok))  # TOCTOU halt-flip

    def test_generation_mismatch_block(self):
        db = _session(); self._authed(db, gen=5)
        bad = ProductionAuthToken(owner="op", expected_generation=4)
        self.assertIsNotNone(AM._revalidate_production_authorization(db, bad))

    def test_epoch_nonzero_block(self):
        db = _session(); tok = self._authed(db)
        db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).update(
            {"activation_epoch": 1}); db.commit()
        self.assertIsNotNone(AM._revalidate_production_authorization(db, tok))

    def test_wrong_owner_block(self):
        db = _session(); self._authed(db, owner="op")
        bad = ProductionAuthToken(owner="intruder", expected_generation=5)
        self.assertIsNotNone(AM._revalidate_production_authorization(db, bad))  # TOCTOU lease-steal

    def test_lease_expired_block(self):
        db = _session(); tok = self._authed(db)
        db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).update(
            {"lease_expiry": get_utc_now() - timedelta(seconds=10)}); db.commit()
        self.assertIsNotNone(AM._revalidate_production_authorization(db, tok))  # TOCTOU lease-expiry


class TestCheckApplyTargetRouting(unittest.TestCase):
    """_check_apply_target(authorized) 라우팅 — authorized=None byte-identical / prod+authorized→revalidate /
    local→None / undetermined→block. (_revalidate는 위에서 직접 테스트 → 여기선 patch로 라우팅만)."""

    def _prod_db(self):
        db = MagicMock(); db.get_bind.return_value.dialect.name = "postgresql"; return db

    def _local_db(self):
        db = MagicMock(); db.get_bind.return_value.dialect.name = "sqlite"; return db

    def _client(self, host):
        c = MagicMock(); c.connection_pool.connection_kwargs = {"host": host}; return c

    def test_authorized_none_prod_block_byte_identical(self):
        block = AM._check_apply_target(self._prod_db(), self._client("prod-redis"), authorized=None)
        self.assertIsNotNone(block); self.assertIn("non-sqlite", block)  # 기존 동작 보존

    def test_authorized_prod_calls_revalidate(self):
        tok = ProductionAuthToken(owner="op", expected_generation=5)
        with patch.object(AM, "_revalidate_production_authorization", return_value=None) as m:
            self.assertIsNone(AM._check_apply_target(self._prod_db(), self._client("prod-redis"), authorized=tok))
        m.assert_called_once()

    def test_authorized_local_sqlite_no_revalidate(self):
        tok = ProductionAuthToken(owner="op", expected_generation=5)
        with patch.object(AM, "_revalidate_production_authorization") as m:
            self.assertIsNone(AM._check_apply_target(self._local_db(), self._client("localhost"), authorized=tok))
        m.assert_not_called()  # local path → 재검증 불요

    def test_dialect_undetermined_blocks_even_authorized(self):
        db = MagicMock(); db.get_bind.side_effect = RuntimeError("no bind")
        tok = ProductionAuthToken(owner="op", expected_generation=5)
        block = AM._check_apply_target(db, self._client("localhost"), authorized=tok)
        self.assertIsNotNone(block); self.assertIn("dialect 판정 불가", block)

    def test_authorized_prod_host_raises_blocks_no_revalidate(self):
        # codex b2 blocker fix: authorized라도 Redis host 미판정(예외)이면 revalidate 전 block (config!=client hole)
        tok = ProductionAuthToken(owner="op", expected_generation=5)
        c = MagicMock(); c.connection_pool.connection_kwargs.get.side_effect = RuntimeError("boom")
        with patch.object(AM, "_revalidate_production_authorization") as m:
            block = AM._check_apply_target(self._prod_db(), c, authorized=tok)
        self.assertIsNotNone(block); self.assertIn("host 판정 불가", block)
        m.assert_not_called()

    def test_authorized_prod_host_none_blocks_no_revalidate(self):
        # authorized + non-sqlite + host None → block (write 대상 미판정), revalidate 미호출
        tok = ProductionAuthToken(owner="op", expected_generation=5)
        with patch.object(AM, "_revalidate_production_authorization") as m:
            block = AM._check_apply_target(self._prod_db(), self._client(None), authorized=tok)
        self.assertIsNotNone(block); self.assertIn("None", block)
        m.assert_not_called()

    def test_authorized_sqlite_db_nonlocal_redis_env_mismatch_blocks(self):
        # codex b2 blocker2: local sqlite DB(control/lease=test data) + non-local Redis + authorized →
        # env-mismatch block (local DB authorization으로 prod Redis write 차단), revalidate 미호출
        tok = ProductionAuthToken(owner="op", expected_generation=5)
        with patch.object(AM, "_revalidate_production_authorization") as m:
            block = AM._check_apply_target(self._local_db(), self._client("prod-redis"), authorized=tok)
        self.assertIsNotNone(block); self.assertIn("env-mismatch", block)
        m.assert_not_called()


class TestReleaseProductionLease(unittest.TestCase):
    def test_releases_own(self):
        db = _session(); _seed_drained(db)
        self.assertIs(cas_acquire_migration_lease(db, owner="op", now=get_utc_now(), ttl_seconds=3600),
                      CasResult.APPLIED); db.commit()
        M._release_production_lease(db, ProductionAuthToken(owner="op", expected_generation=5))
        self.assertIsNone(read_cutover_control(db).lease_owner)

    def test_non_applied_no_raise(self):
        db = _session(); _seed_drained(db)
        self.assertIs(cas_acquire_migration_lease(db, owner="op", now=get_utc_now(), ttl_seconds=3600),
                      CasResult.APPLIED); db.commit()
        # 다른 owner로 release → non-APPLIED, raise 없음, 기존 lease 불변
        M._release_production_lease(db, ProductionAuthToken(owner="intruder", expected_generation=5))
        self.assertEqual(read_cutover_control(db).lease_owner, "op")

    def test_release_exception_swallowed(self):
        db = _session()
        with patch("app.atomic_cutover_durable.cas_release_migration_lease", side_effect=RuntimeError("boom")):
            M._release_production_lease(db, ProductionAuthToken(owner="op", expected_generation=5))  # no raise


class TestComboGuards(unittest.TestCase):
    """main() combo fail-close (I/O 전). flag는 --apply + --expected-writer-generation + positive ttl 필요."""

    def _run(self, argv):
        out = io.StringIO()
        with patch.object(sys, "argv", ["prog", *argv]), contextlib.redirect_stdout(out):
            rc = M.main()
        return rc, out.getvalue()

    def test_flag_without_apply(self):
        rc, out = self._run(["--allow-production-migration"])
        self.assertEqual(rc, 1); self.assertIn("--apply", out)

    def test_flag_without_generation(self):
        rc, out = self._run(["--allow-production-migration", "--apply"])
        self.assertEqual(rc, 1); self.assertIn("--expected-writer-generation", out)

    def test_negative_generation(self):
        rc, out = self._run(["--allow-production-migration", "--apply", "--expected-writer-generation", "-1"])
        self.assertEqual(rc, 1); self.assertIn("0 이상", out)

    def test_nonpositive_ttl(self):
        rc, out = self._run(["--allow-production-migration", "--apply",
                             "--expected-writer-generation", "5", "--lease-ttl-seconds", "0"])
        self.assertEqual(rc, 1); self.assertIn("양수", out)

    def test_generation_zero_passes_combo(self):
        # gen 0은 is None 체크라 combo 통과 → Redis 단계 도달(_get_sync_client None → exit 1, combo block 아님)
        with patch("app.latest_rates_cache._get_sync_client", return_value=None) as m:
            rc, out = self._run(["--allow-production-migration", "--apply", "--expected-writer-generation", "0"])
        self.assertEqual(rc, 1)
        m.assert_called_once()  # combo 통과 후 Redis 단계 도달 (gen 0 미차단 증명)
        self.assertIn("Redis sync client", out)


if __name__ == "__main__":
    unittest.main()
