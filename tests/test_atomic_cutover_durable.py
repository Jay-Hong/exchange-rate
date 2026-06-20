"""P1b C6-2 — cutover durable read/derive/CAS 단위 테스트 (§15/§18, dormant).

read DTO + derive_cutover_state(writer×control×asset 전 분기 + ready_vector 구조검증 fail-closed) +
control_plane_readiness_ok + CAS bricks(fenced conditional-update, APPLIED/CAS_LOST/PRECONDITION_FAILED,
caller-commits, whitelist) + dormancy(live caller 0).
"""
from __future__ import annotations

import ast
import json
import pathlib
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import atomic_cutover_durable as acd
from app.atomic_cutover import CutoverState
from app.atomic_cutover_durable import (
    CasResult,
    CutoverAssetView,
    CutoverControlView,
    cas_advance_status,
    cas_begin_cutover,
    cas_flip_asset_ready,
    cas_reset_to_idle,
    control_plane_readiness_ok,
    derive_cutover_state,
    read_cutover_assets,
    read_cutover_control,
    validate_global_transition,
)
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


def _ctl(status, *, session="s:1", generation=2):
    return CutoverControlView(1, session, generation, status, None, None)


def _assets(state="ready", vec=_VEC, mv=1):
    if state == "ready":
        return {a: CutoverAssetView(a, "ready", vec, mv) for a in _ASSETS}
    return {a: CutoverAssetView(a, "blocked", None, None) for a in _ASSETS}


class TestReadHelpers(unittest.TestCase):

    def test_read_control_none_when_absent(self):
        self.assertIsNone(read_cutover_control(_session()))

    def test_read_control_present(self):
        db = _session(); _seed(db, status="running", session_id="s:9", generation=3)
        v = read_cutover_control(db)
        self.assertEqual((v.status, v.session_id, v.generation), ("running", "s:9", 3))

    def test_read_assets(self):
        db = _session(); _seed(db)
        a = read_cutover_assets(db)
        self.assertEqual(set(a), set(_ASSETS))
        self.assertTrue(all(v.publish_state == "blocked" for v in a.values()))


class TestDeriveCutoverState(unittest.TestCase):

    def _d(self, writer, control=None, assets=None, mv=1):
        return derive_cutover_state(writer, control, assets if assets is not None else {}, mv)

    def test_legacy(self):
        self.assertEqual(self._d(WriterMode.LEGACY), CutoverState.LEGACY_READY)

    def test_halt(self):
        self.assertEqual(self._d(WriterMode.HALT), CutoverState.HALT_BLOCKED)

    def test_unknown_writer_fail_closed(self):
        self.assertEqual(self._d("bogus"), CutoverState.HALT_BLOCKED)

    def test_atomic_no_control_fail_closed(self):
        self.assertEqual(self._d(WriterMode.ATOMIC, None, _assets()), CutoverState.HALT_BLOCKED)

    def test_atomic_invalid_status_fail_closed(self):
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("bogus"), _assets()), CutoverState.HALT_BLOCKED)

    def test_atomic_running_blocked(self):
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("running"), _assets()), CutoverState.ATOMIC_BLOCKED)

    def test_atomic_verified_blocked(self):
        # §15-4: verified는 crash-resume marker, publish authority 아님 → ATOMIC_BLOCKED
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("verified"), _assets()), CutoverState.ATOMIC_BLOCKED)

    def test_atomic_completed_all_ready_valid_ready(self):
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("completed"), _assets()), CutoverState.ATOMIC_READY)

    def test_atomic_completed_membership_mismatch_fail_closed(self):
        # HIGH: completed인데 asset membership_version != expected(=stale) → fail-closed(gate fail-open 방지)
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("completed"), _assets(mv=1), mv=2),
                         CutoverState.HALT_BLOCKED)

    def test_atomic_completed_session_none_fail_closed(self):
        # completed인데 session_id None = torn/corrupt → fail-closed
        ctl = CutoverControlView(1, None, 5, "completed", None, None)
        self.assertEqual(self._d(WriterMode.ATOMIC, ctl, _assets()), CutoverState.HALT_BLOCKED)

    def test_atomic_completed_any_blocked_fail_closed(self):
        mixed = _assets()
        mixed["usd-krw"] = CutoverAssetView("usd-krw", "blocked", None, None)
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("completed"), mixed), CutoverState.HALT_BLOCKED)

    def test_atomic_completed_corrupt_vector_fail_closed(self):
        bad = {a: CutoverAssetView(a, "ready", "not-json", 1) for a in _ASSETS}
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("completed"), bad), CutoverState.HALT_BLOCKED)

    def test_atomic_completed_missing_asset_fail_closed(self):
        partial = {a: CutoverAssetView(a, "ready", _VEC, 1) for a in ("usd-krw", "jpy-krw")}
        self.assertEqual(self._d(WriterMode.ATOMIC, _ctl("completed"), partial), CutoverState.HALT_BLOCKED)


class TestReadyVectorValid(unittest.TestCase):

    def test_valid(self):
        self.assertTrue(acd._ready_vector_valid(_VEC))

    def test_none_empty(self):
        self.assertFalse(acd._ready_vector_valid(None))
        self.assertFalse(acd._ready_vector_valid(""))

    def test_non_json(self):
        self.assertFalse(acd._ready_vector_valid("not json"))

    def test_non_object(self):
        self.assertFalse(acd._ready_vector_valid("[1,2]"))
        self.assertFalse(acd._ready_vector_valid("{}"))  # 빈 dict

    def test_key_not_in_membership(self):
        self.assertFalse(acd._ready_vector_valid(json.dumps({"ghost": make_revision_key(1, 1)})))

    def test_non_str_value(self):
        self.assertFalse(acd._ready_vector_valid(json.dumps({"kb": 123})))

    def test_unparseable_revision_key(self):
        self.assertFalse(acd._ready_vector_valid(json.dumps({"kb": "not-a-rev-key"})))


class TestControlPlaneReadiness(unittest.TestCase):

    def test_ready_when_completed_all_match(self):
        self.assertTrue(control_plane_readiness_ok(_ctl("completed"), _assets(), 1))

    def test_ready_when_verified(self):
        self.assertTrue(control_plane_readiness_ok(_ctl("verified"), _assets(), 1))

    def test_not_ready_running(self):
        self.assertFalse(control_plane_readiness_ok(_ctl("running"), _assets(), 1))

    def test_not_ready_membership_mismatch(self):
        self.assertFalse(control_plane_readiness_ok(_ctl("completed"), _assets(mv=2), 1))

    def test_not_ready_blocked(self):
        self.assertFalse(control_plane_readiness_ok(_ctl("completed"), _assets("blocked"), 1))

    def test_not_ready_none_control(self):
        self.assertFalse(control_plane_readiness_ok(None, _assets(), 1))


class TestValidateGlobalTransition(unittest.TestCase):

    def test_allowed(self):
        self.assertTrue(validate_global_transition(CutoverState.HALT_BLOCKED, CutoverState.ATOMIC_BLOCKED))
        self.assertTrue(validate_global_transition(CutoverState.ATOMIC_BLOCKED, CutoverState.ATOMIC_READY))

    def test_disallowed(self):
        self.assertFalse(validate_global_transition(CutoverState.ATOMIC_READY, CutoverState.ATOMIC_BLOCKED))

    def test_quiesce_edge_any_to_halt(self):
        # incident/quiesce: 어느 state든 →HALT_BLOCKED 허용 (§15 line 213)
        self.assertTrue(validate_global_transition(CutoverState.ATOMIC_READY, CutoverState.HALT_BLOCKED))
        self.assertTrue(validate_global_transition(CutoverState.LEGACY_READY, CutoverState.HALT_BLOCKED))

    def test_self_transition_disallowed(self):
        self.assertFalse(validate_global_transition(CutoverState.ATOMIC_BLOCKED, CutoverState.ATOMIC_BLOCKED))


class TestCasBricks(unittest.TestCase):

    def test_begin_cutover_applied(self):
        db = _session(); _seed(db, status="idle", generation=0)
        self.assertEqual(cas_begin_cutover(db, new_session="s:1", expected_generation=0), CasResult.APPLIED)
        db.commit()
        v = read_cutover_control(db)
        self.assertEqual((v.status, v.session_id, v.generation), ("running", "s:1", 1))

    def test_begin_cutover_cas_lost_on_gen_mismatch(self):
        db = _session(); _seed(db, status="idle", generation=5)
        self.assertEqual(cas_begin_cutover(db, new_session="s:1", expected_generation=0), CasResult.CAS_LOST)

    def test_begin_cutover_precondition_when_not_idle(self):
        db = _session(); _seed(db, status="running", session_id="s:0", generation=0)
        # gen matches but status!=idle/session!=None → fence fails, gen unchanged → PRECONDITION_FAILED
        self.assertEqual(cas_begin_cutover(db, new_session="s:1", expected_generation=0),
                         CasResult.PRECONDITION_FAILED)

    def test_advance_status_running_to_verified(self):
        db = _session(); _seed(db, status="running", session_id="s:1", generation=1)
        self.assertEqual(
            cas_advance_status(db, expected_session="s:1", expected_status="running",
                               new_status="verified", expected_generation=1),
            CasResult.APPLIED,
        )
        db.commit()
        v = read_cutover_control(db)
        self.assertEqual((v.status, v.generation), ("verified", 2))

    def test_advance_status_whitelist_rejects_skip(self):
        db = _session(); _seed(db, status="idle", session_id="s:1", generation=0)
        # (idle, completed) not in progression → PRECONDITION_FAILED (early, no UPDATE)
        self.assertEqual(
            cas_advance_status(db, expected_session="s:1", expected_status="idle",
                               new_status="completed", expected_generation=0),
            CasResult.PRECONDITION_FAILED,
        )

    def test_advance_status_precondition_on_wrong_session(self):
        db = _session(); _seed(db, status="running", session_id="s:1", generation=1)
        self.assertEqual(
            cas_advance_status(db, expected_session="WRONG", expected_status="running",
                               new_status="verified", expected_generation=1),
            CasResult.PRECONDITION_FAILED,
        )

    def test_flip_asset_ready_applied(self):
        db = _session(); _seed(db, asset_state="blocked")
        self.assertEqual(
            cas_flip_asset_ready(db, "usd-krw", ready_revision_vector=_VEC, membership_version=1),
            CasResult.APPLIED,
        )
        db.commit()
        row = db.query(AtomicCutoverAsset).filter(AtomicCutoverAsset.asset == "usd-krw").one()
        self.assertEqual(row.publish_state, "ready")

    def test_flip_asset_already_ready_precondition(self):
        db = _session(); _seed(db, asset_state="ready")
        self.assertEqual(
            cas_flip_asset_ready(db, "usd-krw", ready_revision_vector=_VEC, membership_version=1),
            CasResult.PRECONDITION_FAILED,
        )

    def test_flip_asset_invalid_vector_rejected(self):
        db = _session(); _seed(db, asset_state="blocked")
        self.assertEqual(
            cas_flip_asset_ready(db, "usd-krw", ready_revision_vector="not-json", membership_version=1),
            CasResult.PRECONDITION_FAILED,
        )

    def test_flip_asset_bad_asset_rejected(self):
        db = _session(); _seed(db, asset_state="blocked")
        self.assertEqual(
            cas_flip_asset_ready(db, "usdt-krw", ready_revision_vector=_VEC, membership_version=1),
            CasResult.PRECONDITION_FAILED,
        )

    def test_reset_to_idle_from_failed(self):
        # §18 operator retry: failed→idle + session→NULL + gen++
        db = _session(); _seed(db, status="failed", session_id="s:1", generation=3)
        self.assertEqual(cas_reset_to_idle(db, expected_session="s:1", expected_generation=3),
                         CasResult.APPLIED)
        db.commit()
        v = read_cutover_control(db)
        self.assertEqual((v.status, v.session_id, v.generation), ("idle", None, 4))
        # reset 후 begin 재발화 가능
        self.assertEqual(cas_begin_cutover(db, new_session="s:2", expected_generation=4), CasResult.APPLIED)

    def test_reset_to_idle_precondition_when_not_failed(self):
        db = _session(); _seed(db, status="running", session_id="s:1", generation=1)
        self.assertEqual(cas_reset_to_idle(db, expected_session="s:1", expected_generation=1),
                         CasResult.PRECONDITION_FAILED)

    def test_disambiguate_precondition_when_control_absent(self):
        # control row 부재 → _disambiguate None branch → PRECONDITION_FAILED
        db = _session()  # control row 미seed (asset도 없음)
        self.assertEqual(cas_begin_cutover(db, new_session="s:1", expected_generation=0),
                         CasResult.PRECONDITION_FAILED)


class TestCasResultNaming(unittest.TestCase):

    def test_applied_not_committed_no_error(self):
        # caller-commits라 APPLIED(staged), COMMITTED 아님. ERROR 제거(DB 예외는 전파, codex).
        names = {m.name for m in CasResult}
        self.assertEqual(names, {"APPLIED", "CAS_LOST", "PRECONDITION_FAILED"})


class TestDisambiguateControlFreshRead(unittest.TestCase):
    """C6-2 follow-up — _disambiguate_control이 identity-map stale 객체 대신 fresh read(populate_existing)로
    CAS_LOST를 정확 분류 (C6-8a TestDisambiguateFreshRead 대칭).

    ⚠️ read_cutover_control은 frozen CutoverControlView DTO를 반환해 ORM row가 GC됨(weakref identity-map) →
    staleness를 재현하려면 ORM AtomicCutoverControl row에 strong ref를 직접 유지해야 함(C6-8a read_control_row는
    ORM 객체를 반환해 '공짜로' 얻지만 cutover read helper는 DTO라 명시 보강 필요 — 이 차이를 빠뜨리면 테스트가
    fix 없이도 PASS해 회귀 가드로 무력). per-session 공유를 위해 file-backed engine 사용(_session()의 :memory:는
    세션 간 공유 불가).
    """

    def _shared_engine(self):
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix="_acd.db")
        os.close(fd)
        engine = create_engine(f"sqlite:///{path}")
        AtomicCutoverControl.__table__.create(bind=engine)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return engine

    def test_concurrent_advance_classified_cas_lost(self):
        engine = self._shared_engine()
        Session = sessionmaker(bind=engine)
        sa, sb = Session(), Session()
        self.addCleanup(sa.close); self.addCleanup(sb.close)
        # idle control 시드 (gen=0)
        sa.add(AtomicCutoverControl(id=1, cutover_row_format_version=1, bootstrap_generation=0,
                                    bootstrap_status="idle", bootstrap_session_id=None))
        sa.commit()
        # A가 ORM row를 strong ref로 선load → identity-map에 gen=0 stale 캐시 유발.
        # (DTO 반환 read_cutover_control로는 ORM이 GC돼[weakref map] staleness 미재현 — Lens A 발견.)
        stale_ref = sa.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).first()
        self.assertEqual(stale_ref.bootstrap_generation, 0)
        # B가 begin_cutover로 전진 + commit (gen 0→1, status running, session 점유)
        self.assertIs(cas_begin_cutover(sb, new_session="s:B", expected_generation=0), CasResult.APPLIED)
        sb.commit()
        # A가 같은 expected=0 재시도 → fence(idle AND session IS NULL) mismatch로 rowcount 0 →
        # _disambiguate_control fresh read가 gen 1>0 관측 → CAS_LOST (stale이면 gen=0이라 PRECONDITION_FAILED 오분류).
        self.assertIs(cas_begin_cutover(sa, new_session="s:A", expected_generation=0), CasResult.CAS_LOST)
        # populate_existing이 identity-map의 stale_ref도 fresh로 refresh(side effect) → gen 1 (stale_ref 생존 보장 겸).
        self.assertEqual(stale_ref.bootstrap_generation, 1)


class TestDormancy(unittest.TestCase):
    """C6-2 dormant — app/ 어떤 live 모듈도 atomic_cutover_durable import 0 (island 멤버끼리만)."""

    _ISLAND = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py", "atomic_write_outcome.py",
        "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py", "atomic_reconcile.py",
        "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py", "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py", "atomic_write_durable.py", "atomic_quiesce_durable.py", "atomic_quiesce_startup.py",
    })

    def test_no_live_module_imports_durable(self):
        app_dir = pathlib.Path(acd.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "atomic_cutover_durable" in node.module:
                    self.fail(f"{rel}: from atomic_cutover_durable import — dormant 위반")
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_cutover_durable" in a.name:
                            self.fail(f"{rel}: import atomic_cutover_durable — dormant 위반")

    def test_no_import_time_scheduling(self):
        # durable 소스에 self-scheduling needle 0 (미래 lease-renew thread 등 추가 차단 — runtime과 대칭)
        src = pathlib.Path(acd.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"durable에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
