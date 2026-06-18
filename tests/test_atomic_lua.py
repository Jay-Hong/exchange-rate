"""P1b A3-2 — atomic Lua compare/write + migration raw-CAS 테스트 (§17).

3 층:
1. **reference port (logic, 항상 실행)**: evaluate_compare_write / evaluate_migrate_cas 결정 테이블.
2. **env-gated 실 Redis (syntax/cjson/raw-CAS/atomicity + SET side-effect + reference cross-check)**:
   REDIS_TEST_URL(기본 redis://localhost:6379/15) 연결 가능할 때만 — 없으면 skip(기본 테스트 Redis 무의존).
3. **dormancy trip-wire**: live 모듈이 atomic_lua import / eval·evalsha·script_load·register_script /
   AtomicLatestWriter 인스턴스화 0 (behavior-change-0).
"""
from __future__ import annotations

import json
import os
import pathlib
import unittest
from datetime import datetime, timezone

from app import atomic_lua
from app.atomic_revision import to_canonical_epoch_us
from app.atomic_value_schema import serialize_v2_value

# ── 공통 v2/v1 value 빌더 ──
_MIRRORED = datetime(2026, 6, 17, 1, 0, 0, tzinfo=timezone.utc)
_MIRRORED_NEW = datetime(2026, 6, 17, 2, 0, 0, tzinfo=timezone.utc)  # refreshed_equal: 새 mirrored_at


def _v2(epoch_us: int, row_id: int, rate: float, mirrored_at: datetime = _MIRRORED) -> str:
    return serialize_v2_value(
        rate=rate,
        timestamp="2026-06-17T10:00:00+09:00",
        mirrored_at=mirrored_at,
        revision=(epoch_us, row_id),
    )


def _v1(rate: float) -> str:
    # schema_version 부재 = v1
    return json.dumps({"rate": rate, "timestamp": "2026-06-17T10:00:00+09:00",
                       "mirrored_at": _MIRRORED.isoformat()}, ensure_ascii=False)


_EPOCH = to_canonical_epoch_us(_MIRRORED)


class TestReferencePortCompareWrite(unittest.TestCase):

    def _rev_rate(self, epoch_us, row_id, rate):
        from app.atomic_value_schema import make_revision_key, make_rate_key
        return make_revision_key(epoch_us, row_id), make_rate_key(rate)

    def test_nil_advance(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        self.assertEqual(atomic_lua.evaluate_compare_write(None, rk, rtk), atomic_lua.OUTCOME_ADVANCE)

    def test_v1_migration_required(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        self.assertEqual(
            atomic_lua.evaluate_compare_write(_v1(1300.0), rk, rtk),
            atomic_lua.OUTCOME_MIGRATION_REQUIRED,
        )

    def test_v2_advance_higher_revision(self):
        rk, rtk = self._rev_rate(_EPOCH + 1, 5, 1301.0)
        self.assertEqual(
            atomic_lua.evaluate_compare_write(_v2(_EPOCH, 5, 1300.0), rk, rtk),
            atomic_lua.OUTCOME_ADVANCE,
        )

    def test_v2_skipped_newer_lower_revision(self):
        rk, rtk = self._rev_rate(_EPOCH - 1, 5, 1300.0)
        self.assertEqual(
            atomic_lua.evaluate_compare_write(_v2(_EPOCH, 5, 1300.0), rk, rtk),
            atomic_lua.OUTCOME_SKIPPED_NEWER,
        )

    def test_v2_refreshed_equal_same_revision_same_rate(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        self.assertEqual(
            atomic_lua.evaluate_compare_write(_v2(_EPOCH, 5, 1300.0), rk, rtk),
            atomic_lua.OUTCOME_REFRESHED_EQUAL,
        )

    def test_v2_conflict_same_revision_diff_rate(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1399.0)
        self.assertEqual(
            atomic_lua.evaluate_compare_write(_v2(_EPOCH, 5, 1300.0), rk, rtk),
            atomic_lua.OUTCOME_CONFLICT,
        )

    def test_malformed_invalid_schema(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        self.assertEqual(
            atomic_lua.evaluate_compare_write("{not json", rk, rtk),
            atomic_lua.OUTCOME_INVALID_SCHEMA,
        )

    def test_scalar_invalid_schema(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        self.assertEqual(
            atomic_lua.evaluate_compare_write("5", rk, rtk), atomic_lua.OUTCOME_INVALID_SCHEMA
        )

    def test_schema_version_not_2_invalid(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        bad = json.dumps({"schema_version": 3, "revision_key": "x", "rate_key": "1300"})
        self.assertEqual(atomic_lua.evaluate_compare_write(bad, rk, rtk), atomic_lua.OUTCOME_INVALID_SCHEMA)

    def test_v2_missing_fields_invalid(self):
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        bad = json.dumps({"schema_version": 2, "rate": 1300.0})  # revision_key/rate_key 누락
        self.assertEqual(atomic_lua.evaluate_compare_write(bad, rk, rtk), atomic_lua.OUTCOME_INVALID_SCHEMA)


class TestReferencePortMigrateCas(unittest.TestCase):

    def test_match_migrated(self):
        raw = _v1(1300.0)
        self.assertEqual(atomic_lua.evaluate_migrate_cas(raw, raw), atomic_lua.MIGRATE_OUTCOME_MIGRATED)

    def test_mismatch_changed(self):
        self.assertEqual(
            atomic_lua.evaluate_migrate_cas(_v1(1300.0), _v1(1301.0)),
            atomic_lua.MIGRATE_OUTCOME_CHANGED,
        )

    def test_missing_key_changed(self):
        self.assertEqual(
            atomic_lua.evaluate_migrate_cas(None, _v1(1300.0)), atomic_lua.MIGRATE_OUTCOME_CHANGED
        )


# ── env-gated 실 Redis ──
# REDIS_TEST_URL이 **명시되면** 연결 실패는 skip 아니라 fail (CI는 redis service 필수 — silent
# skip로 Lua 미검증 false-green 차단). 미설정(로컬 기본)일 때만 미가동 → skip.
_REDIS_TEST_URL_ENV = os.environ.get("REDIS_TEST_URL")
_REDIS_TEST_URL = _REDIS_TEST_URL_ENV or "redis://localhost:6379/15"


class TestRealRedisLua(unittest.TestCase):
    """실 Redis EVAL — syntax/cjson/raw-CAS/SET side-effect + reference port cross-check."""

    def setUp(self):
        import redis
        try:
            client = redis.Redis.from_url(_REDIS_TEST_URL)
            client.ping()
        except Exception as e:
            if _REDIS_TEST_URL_ENV:  # 명시 요청 → 반드시 동작해야 함 (CI)
                raise RuntimeError(
                    f"REDIS_TEST_URL={_REDIS_TEST_URL} 연결 실패 — CI는 redis service 필수 "
                    f"(env 명시 시 skip 금지): {e}"
                )
            self.skipTest(f"로컬 Redis 미가동 (REDIS_TEST_URL 미설정): {e} — reference port가 logic 검증")
        self.client = client
        self.writer = atomic_lua.AtomicLatestWriter(self.client)
        self.key = f"test:atomic_lua:{self._testMethodName}"
        self.client.delete(self.key)

    def tearDown(self):
        if self.client is not None:
            self.client.delete(self.key)

    def _rev_rate(self, epoch_us, row_id, rate):
        from app.atomic_value_schema import make_revision_key, make_rate_key
        return make_revision_key(epoch_us, row_id), make_rate_key(rate)

    def _run(self, current_raw, epoch_us, row_id, rate, incoming_mirrored=_MIRRORED):
        """current 세팅 후 compare_write 실행 → (outcome, 결과 raw, incoming). reference port cross-check."""
        if current_raw is not None:
            self.client.set(self.key, current_raw)
        rk, rtk = self._rev_rate(epoch_us, row_id, rate)
        incoming = _v2(epoch_us, row_id, rate, incoming_mirrored)
        outcome = self.writer.compare_write(self.key, incoming, rk, rtk)
        ref = atomic_lua.evaluate_compare_write(current_raw, rk, rtk)
        self.assertEqual(outcome, ref, "실 Lua outcome != reference port")
        result_raw = self.client.get(self.key)
        return outcome, (result_raw.decode() if isinstance(result_raw, bytes) else result_raw), incoming

    def test_nil_advance_sets(self):
        outcome, result, incoming = self._run(None, _EPOCH, 5, 1300.0)
        self.assertEqual(outcome, atomic_lua.OUTCOME_ADVANCE)
        self.assertEqual(result, incoming)

    def test_v2_advance_sets(self):
        outcome, result, incoming = self._run(_v2(_EPOCH, 5, 1300.0), _EPOCH + 1, 5, 1301.0)
        self.assertEqual(outcome, atomic_lua.OUTCOME_ADVANCE)
        self.assertEqual(result, incoming)

    def test_v2_skipped_newer_no_set(self):
        current = _v2(_EPOCH, 5, 1300.0)
        outcome, result, _ = self._run(current, _EPOCH - 1, 5, 1300.0)
        self.assertEqual(outcome, atomic_lua.OUTCOME_SKIPPED_NEWER)
        self.assertEqual(result, current)  # 미변경

    def test_v2_refreshed_equal_sets_new_mirrored_at(self):
        # Medium4: current(old mirrored) vs incoming(new mirrored) — 실제 SET 발생을 검증
        # (둘이 같으면 result==incoming이 SET 없이도 참인 false positive).
        current = _v2(_EPOCH, 5, 1300.0, _MIRRORED)
        outcome, result, incoming = self._run(current, _EPOCH, 5, 1300.0, incoming_mirrored=_MIRRORED_NEW)
        self.assertEqual(outcome, atomic_lua.OUTCOME_REFRESHED_EQUAL)
        self.assertNotEqual(result, current)  # 실제 변경됨 (old → new mirrored_at)
        self.assertEqual(result, incoming)    # SET = incoming (새 mirrored_at)

    def test_real_redis_edge_cases_cross_check(self):
        # Medium3: scalar/array/empty-object/schema≠2/missing-fields/malformed 도 실 Lua가
        # reference port와 동일 outcome + no-SET(미변경)인지 cross-check.
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        incoming = _v2(_EPOCH, 5, 1300.0)
        edge_currents = [
            "5",                                                              # scalar → invalid_schema
            "[]",                                                            # array → migration_required
            "{}",                                                            # empty obj → migration_required
            json.dumps({"schema_version": 2}),                               # v2 필드 누락 → invalid_schema
            json.dumps({"schema_version": 3, "revision_key": "x", "rate_key": "1300"}),  # ≠2 → invalid_schema
            "{not json",                                                      # decode 실패 → invalid_schema
        ]
        for current in edge_currents:
            self.client.set(self.key, current)
            outcome = self.writer.compare_write(self.key, incoming, rk, rtk)
            ref = atomic_lua.evaluate_compare_write(current, rk, rtk)
            self.assertEqual(outcome, ref, f"current={current!r}: 실 Lua {outcome} != ref {ref}")
            self.assertNotIn(outcome, (atomic_lua.OUTCOME_ADVANCE, atomic_lua.OUTCOME_REFRESHED_EQUAL))
            result = self.client.get(self.key)
            self.assertEqual(
                result.decode() if isinstance(result, bytes) else result, current,
                f"current={current!r}: no-SET 위반(값 변경됨)",
            )
            self.client.delete(self.key)

    def test_v2_conflict_no_set(self):
        current = _v2(_EPOCH, 5, 1300.0)
        outcome, result, _ = self._run(current, _EPOCH, 5, 1399.0)
        self.assertEqual(outcome, atomic_lua.OUTCOME_CONFLICT)
        self.assertEqual(result, current)  # 미변경

    def test_v1_migration_required_no_set(self):
        current = _v1(1300.0)
        outcome, result, _ = self._run(current, _EPOCH, 5, 1300.0)
        self.assertEqual(outcome, atomic_lua.OUTCOME_MIGRATION_REQUIRED)
        self.assertEqual(result, current)  # 미변경

    def test_malformed_invalid_schema_no_set(self):
        self.client.set(self.key, "{not json")
        rk, rtk = self._rev_rate(_EPOCH, 5, 1300.0)
        outcome = self.writer.compare_write(self.key, _v2(_EPOCH, 5, 1300.0), rk, rtk)
        self.assertEqual(outcome, atomic_lua.OUTCOME_INVALID_SCHEMA)

    def test_migrate_cas_match_migrated(self):
        v1 = _v1(1300.0)
        v2 = _v2(_EPOCH, 5, 1300.0)
        self.client.set(self.key, v1)
        outcome = self.writer.migrate_cas(self.key, v1, v2)
        self.assertEqual(outcome, atomic_lua.MIGRATE_OUTCOME_MIGRATED)
        result = self.client.get(self.key)
        self.assertEqual(result.decode() if isinstance(result, bytes) else result, v2)

    def test_migrate_cas_mismatch_changed_no_set(self):
        actual = _v1(1301.0)
        self.client.set(self.key, actual)
        outcome = self.writer.migrate_cas(self.key, _v1(1300.0), _v2(_EPOCH, 5, 1300.0))
        self.assertEqual(outcome, atomic_lua.MIGRATE_OUTCOME_CHANGED)
        result = self.client.get(self.key)
        self.assertEqual(result.decode() if isinstance(result, bytes) else result, actual)  # 미변경


# dormant island — 서로 정당 교차 import (atomic_migration/atomic_write_outcome이 atomic_lua import 등)만
# skip. live atomic 모듈(atomic_write_control/runtime/refresh/revision)은 scan 대상 — generic Lua-call
# (register_script/evalsha/script_load/.eval)은 app/ 전체에서 atomic_lua.py만 사용(grep 확인)이라 전체 적용
# 안전 + 미래 회귀까지 잡음 (codex holistic cross-check).
_DORMANT_ISLAND = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
    "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
    "atomic_build.py", "atomic_reconcile.py",  # B2a/B2b-1 dormant — uniform dormant skip set
})


class TestDormancy(unittest.TestCase):
    """A3-2 dormant — **app/ 전체** 어떤 live 모듈도 atomic_lua import/Lua 호출 0 (crawler live-writer +
    live atomic 모듈 포함 — dormant island만 skip, codex holistic cross-check)."""

    def test_no_app_module_uses_atomic_lua_or_eval(self):
        import ast

        app_dir = pathlib.Path(atomic_lua.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _DORMANT_ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_lua" in node.module.split("."):
                        self.fail(f"{rel}: from atomic_lua import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_lua" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_lua — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_lua" in a.name.split("."):
                            self.fail(f"{rel}: import atomic_lua — dormant 위반")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in ("register_script", "evalsha", "script_load", "AtomicLatestWriter",
                                "compare_write", "migrate_cas"):
                        self.fail(f"{rel}: {name}() 호출 — Lua dormant 위반")
                    if name == "eval" and isinstance(node.func, ast.Attribute):
                        self.fail(f"{rel}: .eval() 호출 — Lua dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "atomic_lua" in node.value:
                        self.fail(f"{rel}: 문자열 '{node.value}'에 atomic_lua — dynamic 호출 의심")


if __name__ == "__main__":
    unittest.main()
