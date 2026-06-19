"""P1b A4 — WriteOutcome + PendingCandidate 단위 테스트 (§14, pure dormant).

5-state↔2축 표 전수 + 전 A3-2 Lua outcome 매핑(migration_required/invalid_schema 포함) + failure_kind
2종 + effective_revision 파생 + candidate desired==incoming + disposition(structural→BLOCK_ALERT) + dormancy.
"""
from __future__ import annotations

import pathlib
import unittest

from app import atomic_write_outcome as awo
from app.atomic_write_outcome import (
    CandidateDisposition,
    FailureKind,
    RedisWritePerformed,
    RevisionAdvanced,
    WriteState,
)

_REV = (1_700_000_000_000_000, 42)


class TestWriteOutcomeFromLua(unittest.TestCase):
    """§14 5-state ↔ 2축 표 + 전 Lua outcome 매핑 + effective 파생."""

    def test_advance(self):
        o = awo.write_outcome_from_lua("advance", _REV)
        self.assertEqual(o.state, WriteState.ADVANCE)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.APPLIED)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.YES)
        self.assertEqual(o.effective_revision, _REV)
        self.assertTrue(o.write_healthy)

    def test_refreshed_equal(self):
        o = awo.write_outcome_from_lua("refreshed_equal", _REV)
        self.assertEqual(o.state, WriteState.REFRESHED_EQUAL)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.APPLIED)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.NO)
        self.assertEqual(o.effective_revision, _REV)
        self.assertTrue(o.write_healthy)

    def test_skipped_newer(self):
        o = awo.write_outcome_from_lua("skipped_newer", _REV)
        self.assertEqual(o.state, WriteState.SKIPPED_NEWER)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.NO)
        self.assertIsNone(o.effective_revision)
        self.assertTrue(o.write_healthy)

    def test_conflict(self):
        o = awo.write_outcome_from_lua("conflict", _REV)
        self.assertEqual(o.state, WriteState.CONFLICT)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.NO)
        self.assertEqual(o.effective_revision, _REV)  # same rev, diff rate
        self.assertFalse(o.write_healthy)

    def test_migration_required_is_failed_structural(self):
        o = awo.write_outcome_from_lua("migration_required", _REV)
        self.assertEqual(o.state, WriteState.FAILED)
        self.assertEqual(o.reason, "migration_required")
        self.assertTrue(o.structural)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.NO)
        self.assertIsNone(o.effective_revision)
        self.assertFalse(o.write_healthy)

    def test_invalid_schema_is_failed_structural(self):
        o = awo.write_outcome_from_lua("invalid_schema", _REV)
        self.assertEqual(o.state, WriteState.FAILED)
        self.assertEqual(o.reason, "invalid_schema")
        self.assertTrue(o.structural)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.NO)
        self.assertIsNone(o.effective_revision)

    def test_failed_eval_definite_not_applied(self):
        o = awo.write_outcome_from_lua(None, _REV, failure_kind=FailureKind.DEFINITE_NOT_APPLIED)
        self.assertEqual(o.state, WriteState.FAILED)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.NO)

    def test_failed_eval_uncertain_after_send(self):
        o = awo.write_outcome_from_lua(None, _REV, failure_kind=FailureKind.UNCERTAIN_AFTER_SEND)
        self.assertEqual(o.state, WriteState.FAILED)
        self.assertEqual(o.redis_write_performed, RedisWritePerformed.UNKNOWN)
        self.assertEqual(o.revision_advanced, RevisionAdvanced.UNKNOWN)

    def test_failed_eval_requires_failure_kind(self):
        # codex: None(EVAL) + failure_kind 누락/invalid → fail-closed (낙관적 NOT_APPLIED 기본 금지)
        with self.assertRaises(ValueError):
            awo.write_outcome_from_lua(None, _REV)

    def test_failed_eval_invalid_failure_kind_raises(self):
        # codex final-gate: non-None invalid failure_kind(문자열 등)도 else 흡수 말고 ValueError
        with self.assertRaises(ValueError):
            awo.write_outcome_from_lua(None, _REV, failure_kind="uncertain_after_send")  # enum 아님

    def test_unrecognized_lua_outcome_fail_closed(self):
        o = awo.write_outcome_from_lua("weird_status", _REV)
        self.assertEqual(o.state, WriteState.FAILED)
        self.assertIn("unsupported_lua_status", o.reason)


class TestPendingCandidate(unittest.TestCase):

    def test_desired_revision_is_incoming(self):
        o = awo.write_outcome_from_lua("advance", _REV)
        c = awo.pending_candidate_from_outcome("kb", "usd-krw", o)
        self.assertEqual(c.desired_revision, _REV)            # = incoming (committed DB revision)
        self.assertEqual(c.observed_effective_revision, _REV)
        self.assertEqual(c.write_state, WriteState.ADVANCE)
        self.assertEqual((c.source, c.asset), ("kb", "usd-krw"))

    def test_skipped_newer_observed_none(self):
        o = awo.write_outcome_from_lua("skipped_newer", _REV)
        c = awo.pending_candidate_from_outcome("kb", "usd-krw", o)
        self.assertEqual(c.desired_revision, _REV)
        self.assertIsNone(c.observed_effective_revision)


class TestCandidateDisposition(unittest.TestCase):

    def _disp(self, lua, **kw):
        return awo.candidate_disposition(awo.write_outcome_from_lua(lua, _REV, **kw))

    def test_publish_candidate(self):
        for lua in ("advance", "refreshed_equal", "skipped_newer"):
            self.assertEqual(self._disp(lua), CandidateDisposition.PUBLISH_CANDIDATE, lua)

    def test_conflict_block_alert(self):
        self.assertEqual(self._disp("conflict"), CandidateDisposition.BLOCK_ALERT)

    def test_structural_block_alert(self):
        # §17 v1 재출현 / schema corruption → block+alert (retry 아님)
        self.assertEqual(self._disp("migration_required"), CandidateDisposition.BLOCK_ALERT)
        self.assertEqual(self._disp("invalid_schema"), CandidateDisposition.BLOCK_ALERT)
        self.assertEqual(self._disp("weird_status"), CandidateDisposition.BLOCK_ALERT)  # unsupported

    def test_general_failed_retry(self):
        self.assertEqual(self._disp(None, failure_kind=FailureKind.DEFINITE_NOT_APPLIED), CandidateDisposition.RETRY)
        self.assertEqual(self._disp(None, failure_kind=FailureKind.UNCERTAIN_AFTER_SEND), CandidateDisposition.RETRY)

    def test_eval_caller_reason_cannot_force_block_alert(self):
        # M2: EVAL-예외 경로에서 caller가 structural 예약 reason을 주입해도 typed structural=False라
        # RETRY 유지 (reason 문자열이 disposition을 흔들지 못함).
        for bad in ("migration_required", "invalid_schema", "unsupported_lua_status:x"):
            o = awo.write_outcome_from_lua(None, _REV, failure_kind=FailureKind.DEFINITE_NOT_APPLIED, reason=bad)
            self.assertFalse(o.structural, bad)
            self.assertEqual(awo.candidate_disposition(o), CandidateDisposition.RETRY, bad)


class TestNoValueComparison(unittest.TestCase):
    """L7: WriteState는 enum identity로 비교 — legacy 'failed' 문자열과 충돌 안 함."""

    def test_writestate_failed_not_equal_string(self):
        self.assertNotEqual(WriteState.FAILED, "failed")
        # 레거시 enum(Usdt/Krx) 둘 다와 멤버 identity 다름 (.value 'failed' 겹치나 identity 분리)
        from app.latest_rates_cache import KrxLatestWriteOutcome, UsdtLatestWriteOutcome
        self.assertIsNot(WriteState.FAILED, UsdtLatestWriteOutcome.FAILED)
        self.assertIsNot(WriteState.FAILED, KrxLatestWriteOutcome.FAILED)


# dormant island — 서로 정당 교차 import (atomic_write_outcome이 atomic_lua/value_schema 참조)만 skip.
# live atomic 모듈(atomic_write_control/runtime/refresh/revision)은 scan 대상으로 남김 (codex holistic
# cross-check — startswith("atomic_") 광역 skip은 live atomic까지 가려 약함).
_DORMANT_ISLAND = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
    "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
    "atomic_build.py", "atomic_reconcile.py", "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py", "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py", "atomic_write_durable.py", "atomic_direct_write.py",  # B2a/B2b-1/B2b-4a + C6-5b-3a dormant — uniform dormant skip set
})


class TestDormancy(unittest.TestCase):
    """A4 dormant — **app/ 전체** 어떤 live 모듈도 atomic_write_outcome import 0 (live atomic 모듈 포함 —
    dormant island만 skip, codex: future live path 차단)."""

    def test_no_app_module_imports_atomic_write_outcome(self):
        import ast

        app_dir = pathlib.Path(awo.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            # dormant island만 skip — live atomic 모듈(atomic_write_control/runtime/refresh/revision)은
            # scan 대상으로 남겨 미래 회귀까지 잡음 (codex holistic cross-check — startswith("atomic_")
            # 광역 skip은 live atomic까지 가려 약함). island 멤버는 B coordinator가 정당 import 가능.
            if py.name in _DORMANT_ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_write_outcome" in node.module:
                        self.fail(f"{rel}: from atomic_write_outcome import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_write_outcome" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_write_outcome — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_write_outcome" in a.name:
                            self.fail(f"{rel}: import atomic_write_outcome — dormant 위반")


if __name__ == "__main__":
    unittest.main()
