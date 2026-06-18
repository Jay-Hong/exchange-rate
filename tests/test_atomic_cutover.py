"""P1b A5 — cutover state + publisher gate skeleton 단위 테스트 (§15, pure dormant).

전이 validation(forward + quiesce + disallowed) + publisher gate disposition(legacy/halt/atomic) +
dormancy(app/ 전체 import 0). durable mutation·live 배선·DB 없음 검증.
"""
from __future__ import annotations

import pathlib
import unittest

from app import atomic_cutover as ac
from app.atomic_cutover import CutoverState, PublisherGateDisposition


class TestTransitionValidation(unittest.TestCase):

    def test_forward_path_allowed(self):
        self.assertTrue(ac.is_allowed_transition(CutoverState.LEGACY_READY, CutoverState.HALT_BLOCKED))
        self.assertTrue(ac.is_allowed_transition(CutoverState.HALT_BLOCKED, CutoverState.ATOMIC_BLOCKED))
        self.assertTrue(ac.is_allowed_transition(CutoverState.ATOMIC_BLOCKED, CutoverState.ATOMIC_READY))

    def test_quiesce_any_to_halt_allowed(self):
        # §15 line 213: any non-halt → halt (quiesce/incident)
        for frm in (CutoverState.LEGACY_READY, CutoverState.ATOMIC_BLOCKED, CutoverState.ATOMIC_READY):
            self.assertTrue(ac.is_allowed_transition(frm, CutoverState.HALT_BLOCKED), frm)

    def test_self_transition_disallowed(self):
        for s in CutoverState:
            self.assertFalse(ac.is_allowed_transition(s, s), s)

    def test_skip_and_backward_disallowed(self):
        # halt 거치지 않는 skip / backward 금지
        disallowed = [
            (CutoverState.LEGACY_READY, CutoverState.ATOMIC_BLOCKED),   # skip halt
            (CutoverState.LEGACY_READY, CutoverState.ATOMIC_READY),     # skip
            (CutoverState.HALT_BLOCKED, CutoverState.ATOMIC_READY),     # skip atomic_blocked
            (CutoverState.HALT_BLOCKED, CutoverState.LEGACY_READY),     # backward
            (CutoverState.ATOMIC_BLOCKED, CutoverState.LEGACY_READY),   # backward
            (CutoverState.ATOMIC_READY, CutoverState.ATOMIC_BLOCKED),   # backward (rollback은 →halt 경유)
            (CutoverState.ATOMIC_READY, CutoverState.LEGACY_READY),     # backward
        ]
        for frm, to in disallowed:
            self.assertFalse(ac.is_allowed_transition(frm, to), f"{frm}->{to}")

    def test_non_enum_raises(self):
        with self.assertRaises(TypeError):
            ac.is_allowed_transition("legacy_ready", CutoverState.HALT_BLOCKED)


class TestPublisherGateDisposition(unittest.TestCase):
    """§15 publish_state(CutoverState) 기반 — writer-mode 아님(codex+Workflow reconcile)."""

    def test_gate_open_states_pass_through(self):
        # cutover 전 정상(legacy_ready) + cutover 완료(atomic_ready, 전이 5 후) → gate OPEN
        for s in (CutoverState.LEGACY_READY, CutoverState.ATOMIC_READY):
            self.assertEqual(ac.publisher_gate_disposition(s), PublisherGateDisposition.PASS_THROUGH, s)

    def test_gate_closed_states_would_block(self):
        # cutover 중(halt_blocked, atomic_blocked) → gate CLOSED. 특히 atomic_blocked는 writer atomic
        # 이어도 publisher CLOSED (writer-mode와 분리되는 핵심 케이스).
        for s in (CutoverState.HALT_BLOCKED, CutoverState.ATOMIC_BLOCKED):
            self.assertEqual(ac.publisher_gate_disposition(s), PublisherGateDisposition.WOULD_BLOCK_DRY_RUN, s)

    def test_non_enum_raises(self):
        with self.assertRaises(TypeError):
            ac.publisher_gate_disposition("atomic_ready")


# dormant island만 skip — live atomic 모듈(atomic_write_control/runtime/refresh/revision)은 scan 대상으로
# 남겨 미래 회귀까지 잡음 (codex holistic cross-check — startswith("atomic_") 광역 skip은 live atomic까지
# 가려 약함). atomic_cutover는 현재 island 어느 멤버도 import 안 하지만 일관성 위해 동일 allowlist 사용.
_DORMANT_ISLAND = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
    "atomic_write_outcome.py", "atomic_cutover.py",
})


class TestDormancy(unittest.TestCase):
    """A5 dormant — app/ 전체 어떤 live 모듈도 atomic_cutover import 0 (main/fx publisher/trigger +
    live atomic 모듈 포함 — dormant island만 skip, codex holistic cross-check)."""

    def test_no_app_module_imports_atomic_cutover(self):
        import ast

        app_dir = pathlib.Path(ac.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _DORMANT_ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_cutover" in node.module:
                        self.fail(f"{rel}: from atomic_cutover import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_cutover" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_cutover — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_cutover" in a.name:
                            self.fail(f"{rel}: import atomic_cutover — dormant 위반")


if __name__ == "__main__":
    unittest.main()
