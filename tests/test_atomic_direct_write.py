"""P1b C6-5b-3a — bank/investing direct v2 compare/write helper 단위 테스트 (atomic_direct_write.py, dormant).

atomic_compare_write_v2 outcome matrix(6 Lua outcome → WriteOutcome) + 예외 분류(writer None / pre-set /
after-send) + v2 serialization(같은 key, schema_version 2, revision_key/rate_key) + build_atomic_writer +
dormancy no-importer trip-wire. C7 핵심: skipped_newer → NOT_APPLIED(SET-only trigger 제외).
"""
from __future__ import annotations

import ast
import json
import pathlib
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import atomic_direct_write as adw
from app.atomic_lua import (
    AtomicLatestWriter,
    OUTCOME_ADVANCE,
    OUTCOME_CONFLICT,
    OUTCOME_INVALID_SCHEMA,
    OUTCOME_MIGRATION_REQUIRED,
    OUTCOME_REFRESHED_EQUAL,
    OUTCOME_SKIPPED_NEWER,
)
from app.atomic_value_schema import make_rate_key, make_revision_key_from_revision
from app.atomic_write_outcome import (
    FailureKind,
    RedisWritePerformed,
    WriteState,
    write_outcome_from_lua,
)

_REV = (1_700_000_000_000_000, 5)
_KEY = "latest:bank:kb:usd-krw"
_RATE = 1500.0
_TS = "2026-06-20T10:00:00+09:00"
_KST = ZoneInfo("Asia/Seoul")


class _FakeWriter:
    """AtomicLatestWriter.compare_write 시그니처 mirror — 고정 outcome 반환 / 예외 주입 + call 기록."""

    def __init__(self, outcome: str = OUTCOME_ADVANCE, raise_exc: Exception = None) -> None:
        self.outcome = outcome
        self.raise_exc = raise_exc
        self.calls = []

    def compare_write(self, key, v2_value, revision_key, rate_key):
        self.calls.append((key, v2_value, revision_key, rate_key))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.outcome


def _write(writer, **over):
    kw = dict(rate=_RATE, timestamp=_TS, revision=_REV, source="kb", asset="usd-krw")
    kw.update(over)
    return adw.atomic_compare_write_v2(writer, _KEY, **kw)


class TestOutcomeMatrix(unittest.TestCase):
    """6 Lua outcome → WriteOutcome (write_outcome_from_lua 경유). C7: APPLIED는 advance/refreshed_equal만."""

    def test_advance_applied(self):
        out = _write(_FakeWriter(OUTCOME_ADVANCE))
        self.assertIs(out.state, WriteState.ADVANCE)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.APPLIED)

    def test_refreshed_equal_applied(self):
        out = _write(_FakeWriter(OUTCOME_REFRESHED_EQUAL))
        self.assertIs(out.state, WriteState.REFRESHED_EQUAL)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.APPLIED)

    def test_skipped_newer_not_applied(self):
        # C7 핵심 — skipped_newer는 SET 미수행 → NOT_APPLIED (SET-only trigger 제외 대상)
        out = _write(_FakeWriter(OUTCOME_SKIPPED_NEWER))
        self.assertIs(out.state, WriteState.SKIPPED_NEWER)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.NOT_APPLIED)

    def test_conflict_not_applied(self):
        out = _write(_FakeWriter(OUTCOME_CONFLICT))
        self.assertIs(out.state, WriteState.CONFLICT)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertFalse(out.structural)  # conflict는 structural 아님(별도 BLOCK_ALERT 분기)

    def test_migration_required_failed_structural(self):
        out = _write(_FakeWriter(OUTCOME_MIGRATION_REQUIRED))
        self.assertIs(out.state, WriteState.FAILED)
        self.assertTrue(out.structural)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.NOT_APPLIED)

    def test_invalid_schema_failed_structural(self):
        out = _write(_FakeWriter(OUTCOME_INVALID_SCHEMA))
        self.assertIs(out.state, WriteState.FAILED)
        self.assertTrue(out.structural)


class TestExceptionClassification(unittest.TestCase):
    """codex C4-safe: writer None / pre-set → DEFINITE_NOT_APPLIED, compare_write raise → UNCERTAIN_AFTER_SEND."""

    def test_writer_none_definite_not_applied(self):
        out = _write(None)
        self.assertIs(out.state, WriteState.FAILED)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertEqual(out.reason, "writer_unavailable")

    def test_pre_set_failure_naive_mirrored_at(self):
        # naive mirrored_at → serialize_v2_value 거부(pre-compare_write) → DEFINITE_NOT_APPLIED, compare_write 미호출
        fake = _FakeWriter(OUTCOME_ADVANCE)
        out = _write(fake, mirrored_at=datetime(2026, 1, 1))  # naive
        self.assertIs(out.state, WriteState.FAILED)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.NOT_APPLIED)
        self.assertTrue(out.reason.startswith("pre_set:"))
        self.assertEqual(fake.calls, [])  # SET 미도달

    def test_compare_write_raise_uncertain_after_send(self):
        fake = _FakeWriter(raise_exc=RuntimeError("redis reply lost"))
        out = _write(fake)
        self.assertIs(out.state, WriteState.FAILED)
        self.assertIs(out.redis_write_performed, RedisWritePerformed.UNKNOWN)  # reply-lost 가능
        self.assertTrue(out.reason.startswith("after_send:"))
        self.assertEqual(len(fake.calls), 1)  # compare_write까지 도달


class TestV2Serialization(unittest.TestCase):
    """compare_write에 전달되는 값이 v2 schema(additive) + 같은 key + 올바른 revision_key/rate_key."""

    def test_writes_v2_value_same_key(self):
        fake = _FakeWriter(OUTCOME_ADVANCE)
        mirrored = datetime.now(_KST)
        out = _write(fake, mirrored_at=mirrored)
        self.assertIs(out.state, WriteState.ADVANCE)
        self.assertEqual(len(fake.calls), 1)
        key, v2_value, revision_key, rate_key = fake.calls[0]
        self.assertEqual(key, _KEY)  # 같은 latest:* key (v2 별도 key 금지)
        parsed = json.loads(v2_value)
        self.assertEqual(parsed["schema_version"], 2)        # v2 additive
        self.assertEqual(parsed["rate"], _RATE)              # public 불변
        self.assertEqual(parsed["timestamp"], _TS)
        self.assertEqual(revision_key, make_revision_key_from_revision(_REV))
        self.assertEqual(rate_key, make_rate_key(_RATE))
        self.assertEqual(parsed["revision_key"], revision_key)  # serialize 내부와 일치

    def test_default_mirrored_at_is_tz_aware(self):
        # mirrored_at 미주입 → now(KST) tz-aware → serialize 성공(naive 거부 회피)
        out = _write(_FakeWriter(OUTCOME_ADVANCE))
        self.assertIs(out.state, WriteState.ADVANCE)


class _FakeRedisClient:
    def register_script(self, lua):
        return object()  # AtomicLatestWriter.__init__가 호출 (lazy script)


class TestBuildAtomicWriter(unittest.TestCase):

    def test_none_client_returns_none(self):
        # client 미주입 + _get_sync_client None → None
        with patch("app.latest_rates_cache._get_sync_client", return_value=None):
            self.assertIsNone(adw.build_atomic_writer())

    def test_injected_client_builds_writer(self):
        writer = adw.build_atomic_writer(client=_FakeRedisClient())
        self.assertIsInstance(writer, AtomicLatestWriter)


class TestPresentForIndex(unittest.TestCase):
    """C6-5b-4 present_for_index — mirror index 멤버십 (advance/refreshed_equal/skipped_newer=True)."""

    def _mk(self, lua):
        return write_outcome_from_lua(lua, _REV)

    def test_present_outcomes(self):
        for lua in (OUTCOME_ADVANCE, OUTCOME_REFRESHED_EQUAL, OUTCOME_SKIPPED_NEWER):
            self.assertTrue(adw.present_for_index(self._mk(lua)), f"{lua} → present")

    def test_skipped_newer_present_but_not_trigger(self):
        # C7 경계: skipped_newer는 index present(True)이나 trigger 대상은 아님(applied_for_trigger=False).
        outcome = self._mk(OUTCOME_SKIPPED_NEWER)
        self.assertTrue(adw.present_for_index(outcome))      # index 포함 (payload drop 방지)
        self.assertFalse(adw.applied_for_trigger(outcome))   # SET 미수행 → trigger 제외

    def test_conflict_and_structural_not_present(self):
        for lua in (OUTCOME_CONFLICT, OUTCOME_MIGRATION_REQUIRED, OUTCOME_INVALID_SCHEMA):
            self.assertFalse(adw.present_for_index(self._mk(lua)), f"{lua} → not present")

    def test_general_failed_not_present(self):
        for fk in (FailureKind.DEFINITE_NOT_APPLIED, FailureKind.UNCERTAIN_AFTER_SEND):
            outcome = write_outcome_from_lua(None, _REV, failure_kind=fk, reason="x")
            self.assertFalse(adw.present_for_index(outcome))


class TestOutcomeLabel(unittest.TestCase):
    """C6-5b-4 outcome_label — bounded telemetry 라벨 (mirror가 WriteState import 회피)."""

    def test_non_failed_labels(self):
        self.assertEqual(adw.outcome_label(write_outcome_from_lua(OUTCOME_ADVANCE, _REV)), "advance")
        self.assertEqual(adw.outcome_label(write_outcome_from_lua(OUTCOME_REFRESHED_EQUAL, _REV)), "refreshed_equal")
        self.assertEqual(adw.outcome_label(write_outcome_from_lua(OUTCOME_SKIPPED_NEWER, _REV)), "skipped_newer")
        self.assertEqual(adw.outcome_label(write_outcome_from_lua(OUTCOME_CONFLICT, _REV)), "conflict")

    def test_structural_failed_labels_distinguished(self):
        # migration_required(v1 재출현=C6-PRE 미완) / invalid_schema(corruption) 구분 노출 (FLIP 신호).
        self.assertEqual(adw.outcome_label(write_outcome_from_lua(OUTCOME_MIGRATION_REQUIRED, _REV)), "migration_required")
        self.assertEqual(adw.outcome_label(write_outcome_from_lua(OUTCOME_INVALID_SCHEMA, _REV)), "invalid_schema")

    def test_unsupported_status_structural_failed(self):
        # 예상 밖 lua status → structural=True + reason="unsupported_lua_status:.." → "structural_failed"
        self.assertEqual(adw.outcome_label(write_outcome_from_lua("weird", _REV)), "structural_failed")

    def test_general_failed_label(self):
        for fk in (FailureKind.DEFINITE_NOT_APPLIED, FailureKind.UNCERTAIN_AFTER_SEND):
            outcome = write_outcome_from_lua(None, _REV, failure_kind=fk, reason="x")
            self.assertEqual(adw.outcome_label(outcome), "general_failed")


# C6-5b-3b/C6-5b-4: atomic_direct_write를 import하는 sanctioned live 모듈 — crud.py(direct bank/investing
# atomic 분기) + latest_rates_cache.py(C6-5b-4 mirror atomic 분기 — build_atomic_writer/atomic_compare_write_v2/
# present_for_index/outcome_label). 그 외 app/ live 모듈은 여전히 import 0 (island 경계 유지).
_SANCTIONED_IMPORTERS = frozenset({"atomic_direct_write.py", "crud.py", "latest_rates_cache.py"})


class TestDormancy(unittest.TestCase):
    """app/ live 모듈 중 atomic_direct_write를 import하는 건 crud.py + latest_rates_cache.py(sanctioned)뿐 — 그 외 0."""

    def test_only_crud_imports_atomic_direct_write(self):
        app_dir = pathlib.Path(adw.__file__).resolve().parent
        offenders = []
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _SANCTIONED_IMPORTERS:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_direct_write" in node.module:
                        offenders.append(py.name)
                    elif node.module == "app" and any(a.name == "atomic_direct_write" for a in node.names):
                        offenders.append(py.name)
                elif isinstance(node, ast.Import):
                    if any("atomic_direct_write" in a.name for a in node.names):
                        offenders.append(py.name)
        self.assertEqual(offenders, [], f"live module imports atomic_direct_write — dormant 위반: {offenders}")


if __name__ == "__main__":
    unittest.main()
