"""P1b A3-3a — migration decision logic 단위 테스트 (§10/§17, pure).

conftest가 firebase stub + sqlite. decide_migration_action은 I/O 0 (Redis/DB 미접근) — DB는
RevisionedRate fixture, Redis는 raw 문자열. 전 분류 + §10 4-case + DB-derived write 값 + dormancy.
"""
from __future__ import annotations

import json
import pathlib
import unittest
from datetime import datetime, timezone

from app import atomic_migration as am
from app.atomic_revision import RevisionedRate, to_canonical_epoch_us
from app.atomic_value_schema import make_rate_key, make_revision_key_from_revision, serialize_v2_value
from app.crud import to_kst_isoformat

_MIRRORED = datetime(2026, 6, 17, 5, 0, 0, tzinfo=timezone.utc)
_TS = datetime(2026, 6, 17, 1, 0, 0)        # naive UTC (DB 형식)
_TS_LATER = datetime(2026, 6, 17, 2, 0, 0)
_TS_EARLIER = datetime(2026, 6, 17, 0, 0, 0)


def _db(rate=1300.0, ts=_TS, row_id=42, source="kb", asset="usd-krw") -> RevisionedRate:
    return RevisionedRate(
        source=source, asset=asset, rate=rate, timestamp=ts,
        revision=(to_canonical_epoch_us(ts), row_id),
    )


def _v1(rate, ts_dt=_TS) -> str:
    return json.dumps({
        "rate": rate, "timestamp": to_kst_isoformat(ts_dt), "mirrored_at": _MIRRORED.isoformat(),
    }, ensure_ascii=False)


def _v2(rate, revision, source="kb", asset="usd-krw") -> str:
    return serialize_v2_value(
        rate=rate, timestamp=to_kst_isoformat(_TS), mirrored_at=_MIRRORED,
        revision=revision, source=source, asset=asset,
    )


class TestClassify(unittest.TestCase):

    def test_absent(self):
        self.assertEqual(am.classify_redis_value(None), "absent")

    def test_v1(self):
        self.assertEqual(am.classify_redis_value(_v1(1300.0)), "v1")

    def test_v2(self):
        self.assertEqual(am.classify_redis_value(_v2(1300.0, (to_canonical_epoch_us(_TS), 42))), "v2")

    def test_invalid_scalar_array_malformed_schema3(self):
        for raw in ("5", "[]", "{not json", json.dumps({"schema_version": 3})):
            self.assertEqual(am.classify_redis_value(raw), "invalid", raw)

    def test_present_null_schema_version_is_invalid_not_v1(self):
        # High: {"schema_version": null}은 absent(v1)이 아니라 invalid(fail-closed)
        raw = json.dumps({"schema_version": None, "rate": 1300.0, "timestamp": "x"})
        self.assertEqual(am.classify_redis_value(raw), "invalid")


class TestDecideAbsent(unittest.TestCase):

    def test_seed_from_db(self):
        d = am.decide_migration_action(None, _db(rate=1300.0), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_SEED_FROM_DB)
        self.assertTrue(d.writes)
        self.assertEqual(d.write_method, am.WRITE_METHOD_COMPARE_WRITE)
        v2 = json.loads(d.v2_value)
        self.assertEqual(v2["schema_version"], 2)
        self.assertEqual(v2["rate_key"], make_rate_key(1300.0))
        self.assertEqual(d.revision_key, make_revision_key_from_revision(_db().revision))
        self.assertEqual(d.rate_key, make_rate_key(1300.0))


class TestDecideV1(unittest.TestCase):

    def test_upgrade_db_newer(self):
        d = am.decide_migration_action(_v1(1250.0, _TS_EARLIER), _db(rate=1300.0, ts=_TS), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_MIGRATE_UPGRADE)
        self.assertEqual(d.write_method, am.WRITE_METHOD_MIGRATE_CAS)
        self.assertEqual(d.expected_raw, _v1(1250.0, _TS_EARLIER))
        # Medium2: v2는 DB-derived (db.rate 1300, v1 rate 1250 아님)
        self.assertEqual(json.loads(d.v2_value)["rate_key"], make_rate_key(1300.0))

    def test_seed_same_ts_rate(self):
        d = am.decide_migration_action(_v1(1300.0, _TS), _db(rate=1300.0, ts=_TS), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_MIGRATE_SEED)
        self.assertEqual(d.write_method, am.WRITE_METHOD_MIGRATE_CAS)

    def test_redis_ahead_v1_newer(self):
        d = am.decide_migration_action(_v1(1300.0, _TS_LATER), _db(rate=1300.0, ts=_TS), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_REDIS_AHEAD)
        self.assertFalse(d.writes)

    def test_conflict_same_ts_diff_rate(self):
        d = am.decide_migration_action(_v1(1301.0, _TS), _db(rate=1300.0, ts=_TS), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_CONFLICT)
        self.assertFalse(d.writes)

    def test_conflict_v1_parse_fail(self):
        bad = json.dumps({"rate": 1300.0, "timestamp": "not-a-date", "mirrored_at": _MIRRORED.isoformat()})
        d = am.decide_migration_action(bad, _db(), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_CONFLICT)

    def test_conflict_v1_bad_rate(self):
        bad = json.dumps({"rate": "xyz", "timestamp": to_kst_isoformat(_TS), "mirrored_at": _MIRRORED.isoformat()})
        d = am.decide_migration_action(bad, _db(ts=_TS), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_CONFLICT)

    def test_conflict_v1_bool_rate(self):
        # M2: JSON true가 float(True)→1.0으로 우회되지 않고 fail-closed
        bad = json.dumps({"rate": True, "timestamp": to_kst_isoformat(_TS), "mirrored_at": _MIRRORED.isoformat()})
        d = am.decide_migration_action(bad, _db(ts=_TS), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_CONFLICT)

    def test_conflict_v1_naive_timestamp(self):
        # M3: naive ts(KST aware 기대)는 corruption → conflict (silent UTC 해석 금지)
        bad = json.dumps({"rate": 1300.0, "timestamp": _TS.isoformat(), "mirrored_at": _MIRRORED.isoformat()})
        d = am.decide_migration_action(bad, _db(ts=_TS), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_CONFLICT)


class TestDecideV2(unittest.TestCase):

    def test_already_current(self):
        db = _db(rate=1300.0, ts=_TS, row_id=42)
        d = am.decide_migration_action(_v2(1300.0, db.revision), db, _MIRRORED)
        self.assertEqual(d.action, am.ACTION_ALREADY_CURRENT)
        self.assertFalse(d.writes)

    def test_lagging_v2(self):
        db = _db(rate=1300.0, ts=_TS, row_id=42)
        lower = (db.revision[0] - 1_000_000, 1)  # epoch 더 작음
        d = am.decide_migration_action(_v2(1300.0, lower), db, _MIRRORED)
        self.assertEqual(d.action, am.ACTION_LAGGING_V2)
        self.assertFalse(d.writes)

    def test_redis_ahead_v2(self):
        db = _db(rate=1300.0, ts=_TS, row_id=42)
        higher = (db.revision[0] + 1_000_000, 1)
        d = am.decide_migration_action(_v2(1300.0, higher), db, _MIRRORED)
        self.assertEqual(d.action, am.ACTION_REDIS_AHEAD)

    def test_conflict_same_revision_diff_rate(self):
        db = _db(rate=1300.0, ts=_TS, row_id=42)
        d = am.decide_migration_action(_v2(9999.0, db.revision), db, _MIRRORED)
        self.assertEqual(d.action, am.ACTION_CONFLICT)

    def test_invalid_v2_missing_fields(self):
        bad = json.dumps({"schema_version": 2, "rate": 1300.0})
        d = am.decide_migration_action(bad, _db(), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_INVALID_SCHEMA)

    def test_invalid_v2_malformed_revision_key(self):
        # M1: revision_key="x"(형식 오류)는 redis_ahead로 숨지 않고 invalid (fail-closed)
        bad = json.dumps({"schema_version": 2, "revision_key": "x", "rate_key": "1300",
                          "rate": 1300.0, "timestamp": "t", "mirrored_at": "m"})
        d = am.decide_migration_action(bad, _db(), _MIRRORED)
        self.assertEqual(d.action, am.ACTION_INVALID_SCHEMA)


class TestInvariantGuard(unittest.TestCase):

    def test_bad_revisioned_rate_raises(self):
        # L1: revision[0] != canonical(timestamp) DTO는 entry guard로 fail-closed
        bad_db = RevisionedRate(source="kb", asset="usd-krw", rate=1300.0, timestamp=_TS,
                                revision=(to_canonical_epoch_us(_TS) + 999, 42))
        with self.assertRaises(ValueError):
            am.decide_migration_action(_v1(1300.0), bad_db, _MIRRORED)


class TestDecideInvalid(unittest.TestCase):

    def test_scalar_array_malformed(self):
        for raw in ("5", "[]", "{not json", json.dumps({"schema_version": 3, "revision_key": "x", "rate_key": "1300"})):
            d = am.decide_migration_action(raw, _db(), _MIRRORED)
            self.assertEqual(d.action, am.ACTION_INVALID_SCHEMA, raw)
            self.assertFalse(d.writes)


class TestDormancy(unittest.TestCase):
    """A3-3a dormant — live writer 경로가 atomic_migration import/호출 0."""

    _LIVE_MODULES = ("crud.py", "latest_rates_cache.py", "scheduler.py", "main.py")

    def test_live_modules_do_not_import_atomic_migration(self):
        import ast

        app_dir = pathlib.Path(am.__file__).resolve().parent
        for mod in self._LIVE_MODULES:
            tree = ast.parse((app_dir / mod).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_migration" in node.module:
                        self.fail(f"{mod}: from atomic_migration import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_migration" for a in node.names):
                        self.fail(f"{mod}: from app import atomic_migration — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_migration" in a.name:
                            self.fail(f"{mod}: import atomic_migration — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "atomic_migration" in node.value:
                        self.fail(f"{mod}: 문자열 '{node.value}'에 atomic_migration — dynamic 의심")


if __name__ == "__main__":
    unittest.main()
