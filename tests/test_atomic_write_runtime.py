"""app/atomic_write_runtime.py 단위 테스트 (A2-1 cache layer).

in-memory SQLite + AtomicWriteControl row로 검증. 외부 DB/firebase 의존성 0.

검증 (codex 4 invariant + §19 A2 phase-gate):
- import-time side effect 0 (thread/job/engine/session 모듈 레벨 부재 — source scan)
- snapshot() no-throw + refresh 전 _INITIAL(legacy passthrough)
- refresh phase-gate 매트릭스 (legacy/atomic/halt/pre-activation corruption/empty)
- activation_latched 단조 (post-activation epoch 회귀에도 True 유지 → fail-closed halt)
- refresh read 실패 → diagnostic HALT + prev_enforced fail-close(atomic→halt, legacy/halt 보존), no-throw
- explicit halt는 구조적 valid row만 honor (corrupt-halt는 phase-gate)
- halt_enforced helper
"""
from __future__ import annotations

import pathlib
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import atomic_write_runtime as awr
from app.atomic_write_control import WriterMode
from app.models import AtomicWriteControl


def _session_with_row(requested_mode, activation_epoch, target_schema=1, format_version=1, mode_generation=0):
    """주어진 control row 1개를 가진 격리 in-memory 세션."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AtomicWriteControl.__table__.create(engine)
    s = sessionmaker(bind=engine)()
    s.add(AtomicWriteControl(
        id=1,
        control_row_format_version=format_version,
        target_write_schema_version=target_schema,
        required_writer_protocol=1,
        activation_epoch=activation_epoch,
        mode_generation=mode_generation,
        requested_mode=requested_mode,
    ))
    s.commit()
    return s


def _empty_session():
    """control row 없는 격리 in-memory 세션 (table만 존재)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AtomicWriteControl.__table__.create(engine)
    return sessionmaker(bind=engine)()


class TestAtomicWriteRuntime(unittest.TestCase):

    def setUp(self):
        awr._reset_for_test()  # 모듈 global cache 리셋 (monotonic latch 때문)

    def tearDown(self):
        awr._reset_for_test()

    # ── 초기 상태 / no-throw ──
    def test_initial_snapshot_is_legacy_passthrough(self):
        snap = awr.snapshot()
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.HALT)  # 미확인 진단 라벨
        self.assertFalse(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.LEGACY)          # pre-activation 안전
        self.assertEqual(snap.mode_generation, 0)

    def test_snapshot_never_throws(self):
        for _ in range(5):
            snap = awr.snapshot()
            self.assertIsInstance(snap, awr.WriteModeSnapshot)

    # ── refresh phase-gate 매트릭스 ──
    def test_refresh_legacy_row(self):
        snap = awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0))
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.LEGACY)
        self.assertFalse(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.LEGACY)

    def test_refresh_atomic_activated(self):
        snap = awr.refresh_from_db(_session_with_row(WriterMode.ATOMIC, 1, target_schema=2))
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.ATOMIC)
        self.assertTrue(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.ATOMIC)

    def test_refresh_halt_post_activation(self):
        snap = awr.refresh_from_db(_session_with_row(WriterMode.HALT, 1))
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.HALT)
        self.assertTrue(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.HALT)

    def test_pre_activation_corruption_is_legacy(self):
        # format mismatch(epoch0) → diagnostic HALT, 하지만 pre-activation이라 enforced=legacy
        snap = awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0, format_version=99))
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.HALT)
        self.assertFalse(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.LEGACY)  # A2 배포 안전

    def test_empty_row_pre_activation_legacy(self):
        snap = awr.refresh_from_db(_empty_session())
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.HALT)  # None→HALT 진단
        self.assertFalse(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.LEGACY)

    def test_pre_activation_explicit_halt_honored(self):
        # pre-activation(epoch0)이라도 운영자 명시 requested_mode=halt는 halt로 honor
        # (§3/§9 legacy→halt quiesce — 부재/corruption과 달리 explicit halt는 존중).
        snap = awr.refresh_from_db(_session_with_row(WriterMode.HALT, 0))
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.HALT)
        self.assertFalse(snap.activation_latched)               # 아직 미activation
        self.assertEqual(snap.enforced_action, WriterMode.HALT)  # legacy로 삼키지 않음

    def test_pre_activation_corrupt_halt_not_honored(self):
        # corrupt row(target_schema=0) + requested=halt → explicit halt 아님(신뢰 불가) →
        # pre-activation legacy passthrough (corruption은 halt로 우회 못 함).
        snap = awr.refresh_from_db(_session_with_row(WriterMode.HALT, 0, target_schema=0))
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.HALT)  # schema<1 → compute HALT
        self.assertFalse(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.LEGACY)  # corrupt-halt → legacy (not halt)

    def test_post_activation_corrupt_halt_fails_closed(self):
        # 같은 corrupt-halt라도 post-activation(latched)이면 fail-closed halt
        awr.refresh_from_db(_session_with_row(WriterMode.ATOMIC, 1, target_schema=2))  # latch True
        snap = awr.refresh_from_db(_session_with_row(WriterMode.HALT, 1, target_schema=0))
        self.assertTrue(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.HALT)

    def test_mode_generation_monotonic(self):
        # 관측 gen은 fencing token이라 단조 — 낮은 값으로 회귀 금지
        s1 = awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0, mode_generation=5))
        self.assertEqual(s1.mode_generation, 5)
        s2 = awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0, mode_generation=2))
        self.assertEqual(s2.mode_generation, 5)   # 2로 회귀 안 함
        s3 = awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0, mode_generation=9))
        self.assertEqual(s3.mode_generation, 9)   # 증가는 반영

    # ── invariant 3: activation_latched 단조 ──
    def test_activation_latch_monotonic_post_activation_corruption_fails_closed(self):
        # 1) atomic 활성화 → latched True
        awr.refresh_from_db(_session_with_row(WriterMode.ATOMIC, 1, target_schema=2))
        self.assertTrue(awr.snapshot().activation_latched)
        # 2) epoch가 0으로 회귀(corruption/일시) → latched는 True 유지, enforced=halt(fail-closed)
        snap = awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0))
        self.assertTrue(snap.activation_latched)                  # 단조 — False로 후퇴 금지
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.LEGACY)
        self.assertEqual(snap.enforced_action, WriterMode.HALT)   # post-activation fail-closed

    # ── invariant 2: refresh read 실패 → no-throw + fail-close ──
    def test_refresh_read_failure_post_activation_fails_closed(self):
        # post-activation(atomic) read 실패 → diagnostic=HALT + enforced=halt(fail-closed), latch 보존
        awr.refresh_from_db(_session_with_row(WriterMode.ATOMIC, 1, target_schema=2))
        with patch("app.atomic_write_runtime.read_control_row", side_effect=RuntimeError("boom")):
            snap = awr.refresh_from_db(_empty_session())
        self.assertEqual(snap.diagnostic_effective_mode, WriterMode.HALT)
        self.assertTrue(snap.activation_latched)                 # latch 보존(단조)
        self.assertEqual(snap.enforced_action, WriterMode.HALT)  # atomic→halt fail-closed (§7)

    def test_refresh_read_failure_pre_activation_keeps_legacy(self):
        # pre-activation(legacy) read 실패 → enforced=legacy 보존 (A2 배포 안전, halt 아님)
        awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0))
        with patch("app.atomic_write_runtime.read_control_row", side_effect=RuntimeError("boom")):
            snap = awr.refresh_from_db(_empty_session())
        self.assertFalse(snap.activation_latched)
        self.assertEqual(snap.enforced_action, WriterMode.LEGACY)

    def test_explicit_halt_read_failure_stays_halt(self):
        # 명시 halt 후 read 실패 → halt 보존 (durable halt가 transient 실패에 안 풀림)
        awr.refresh_from_db(_session_with_row(WriterMode.HALT, 0))
        with patch("app.atomic_write_runtime.read_control_row", side_effect=RuntimeError("boom")):
            snap = awr.refresh_from_db(_empty_session())
        self.assertEqual(snap.enforced_action, WriterMode.HALT)

    # ── halt_enforced helper ──
    def test_halt_enforced_helper(self):
        halt = awr.refresh_from_db(_session_with_row(WriterMode.HALT, 1))
        self.assertTrue(awr.halt_enforced(halt))
        awr._reset_for_test()
        legacy = awr.refresh_from_db(_session_with_row(WriterMode.LEGACY, 0))
        self.assertFalse(awr.halt_enforced(legacy))
        awr._reset_for_test()
        atomic = awr.refresh_from_db(_session_with_row(WriterMode.ATOMIC, 1, target_schema=2))
        self.assertFalse(awr.halt_enforced(atomic))


class TestImportSideEffectFree(unittest.TestCase):
    """invariant 1: import-time side effect 0 — thread/job/engine/session 모듈 부재."""

    def test_no_lifecycle_or_db_construction_in_module(self):
        src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "atomic_write_runtime.py").read_text(encoding="utf-8")
        for forbidden in (".start(", "Thread(", "add_job", "create_engine", "SessionLocal", "IntervalTrigger"):
            self.assertNotIn(
                forbidden, src,
                f"atomic_write_runtime.py에 '{forbidden}' 존재 — import-time side effect 위험 (A2-1 dormant 위반)",
            )

    def test_mirror_cycle_not_write_mode_aware_yet(self):
        # A2-3: usdt/krx tick writer(set_latest_usdt/krx_tick)는 연결됨. 단 mirror(_mirror_all_latest)는
        # C6 atomicization dependency라 아직 write-mode 미연결 (latest:index/bank/investing/source
        # unconditional writer). **AST 노드**로 검증 — comment/docstring 문자열 false-positive 회피
        # (codex Low). Name/Attribute/Import 노드만 검사하므로 실제 코드 참조만 잡음.
        import ast
        import inspect
        import textwrap

        from app import latest_rates_cache
        tree = ast.parse(textwrap.dedent(inspect.getsource(latest_rates_cache._mirror_all_latest)))
        # write-mode 모듈/심볼 — 모든 import 형태(from app import X / from app.X import Y /
        # alias as awr / import app.X) + Name/Attribute 사용 검출 (codex Low: alias false-negative).
        wm_modules = {"atomic_write_runtime", "atomic_write_control"}  # app.<mod> 하위
        forbidden_names = {"atomic_write_runtime", "WriterMode", "halt_enforced"}
        forbidden_attrs = {"enforced_action", "halt_enforced"}
        hits = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                hits.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                hits.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                # from app.atomic_write_runtime import ... / from app import atomic_write_runtime [as awr]
                if mod.split(".")[-1] in wm_modules:
                    hits.add(mod)
                if mod == "app":
                    hits.update(a.name for a in node.names if a.name in wm_modules)
            elif isinstance(node, ast.Import):
                # import app.atomic_write_runtime [as awr]
                hits.update(a.name for a in node.names if a.name.split(".")[-1] in wm_modules)
        self.assertEqual(
            hits, set(),
            f"_mirror_all_latest에 write-mode 참조 노드 {hits} — mirror는 C6까지 미연결이어야 함",
        )


if __name__ == "__main__":
    unittest.main()
