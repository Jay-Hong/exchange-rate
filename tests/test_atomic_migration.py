"""P1b A3-3a — migration decision logic 단위 테스트 (§10/§17, pure).

conftest가 firebase stub + sqlite. decide_migration_action은 I/O 0 (Redis/DB 미접근) — DB는
RevisionedRate fixture, Redis는 raw 문자열. 전 분류 + §10 4-case + DB-derived write 값 + dormancy.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import atomic_migration as am
from app import models
from app.atomic_revision import RevisionedRate, to_canonical_epoch_us
from app.atomic_value_schema import make_rate_key, make_revision_key_from_revision, serialize_v2_value
from app.crud import to_kst_isoformat
from app.latest_rates_cache import latest_key_bank, latest_key_investing

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


class TestRunMigrationDryRun(unittest.TestCase):
    """A3-3b runner dry-run (mocked) — write 0, 결정 보고, per-key 격리."""

    def test_dry_run_reports_decision_no_write(self):
        db = MagicMock()
        bank_rr = _db(rate=1300.0, ts=_TS, source="kb")
        client = MagicMock()
        client.get.return_value = _v1(1300.0, _TS).encode()  # 동일 ts·rate → seed
        with patch("app.crud._select_latest_bank_rates_with_revision", return_value=[bank_rr]), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=None), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, client, apply=False, scope="bank")
        self.assertTrue(result.dry_run)
        self.assertEqual(len(result.results), 1)
        r = result.results[0]
        self.assertEqual(r.action, am.ACTION_MIGRATE_SEED)
        self.assertFalse(r.wrote)
        self.assertFalse(r.applied)
        self.assertFalse(client.set.called)  # dry-run → write 0

    def test_per_key_isolation_one_failure_continues(self):
        db = MagicMock()
        rrs = [_db(source="kb", ts=_TS), _db(source="hana", ts=_TS)]
        client = MagicMock()
        # 첫 key GET 예외, 둘째 정상 → 첫 error result + 둘째 처리 (batch 중단 X)
        client.get.side_effect = [RuntimeError("redis boom"), _v1(1300.0, _TS).encode()]
        with patch("app.crud._select_latest_bank_rates_with_revision", return_value=rrs), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=None), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, client, apply=False, scope="bank")
        self.assertEqual(len(result.results), 2)
        self.assertEqual(result.error_count, 1)
        self.assertTrue(any(r.error and "redis boom" in r.error for r in result.results))


class TestCheckApplySafety(unittest.TestCase):

    def test_sqlite_localhost_ok(self):
        with patch("app.config.REDIS_URL", "redis://localhost:6379/0"):
            self.assertIsNone(am.check_apply_safety())

    def test_non_sqlite_blocked(self):
        fake_engine = MagicMock()
        fake_engine.url.get_dialect.return_value.name = "postgresql"
        with patch("app.database.engine", fake_engine):
            block = am.check_apply_safety()
        self.assertIsNotNone(block)
        self.assertIn("non-sqlite", block)

    def test_non_local_redis_blocked(self):
        with patch("app.config.REDIS_URL", "redis://prod-host:6379/0"):
            self.assertIsNotNone(am.check_apply_safety())

    def test_fail_closed_unparseable_host(self):
        # M4: hostname 판정 불가(unix/scheme-less/빈 URL) → fail-closed 차단 (localhost fall-open 금지)
        for url in ("unix:///tmp/redis.sock", "prod-redis:6379", ""):
            with patch("app.config.REDIS_URL", url):
                self.assertIsNotNone(am.check_apply_safety(), f"url={url!r} 차단돼야")


class TestCheckApplyTarget(unittest.TestCase):
    """_check_apply_target — 실제 write 대상(db dialect + client host) fail-closed (codex final-gate)."""

    def _db(self, dialect):
        d = MagicMock()
        d.get_bind.return_value.dialect.name = dialect
        return d

    def _client(self, host):
        c = MagicMock()
        c.connection_pool.connection_kwargs = {"host": host}
        return c

    def test_local_sqlite_ok(self):
        self.assertIsNone(am._check_apply_target(self._db("sqlite"), self._client("localhost")))

    def test_non_local_client_blocked(self):
        self.assertIsNotNone(am._check_apply_target(self._db("sqlite"), self._client("prod-host")))

    def test_non_sqlite_db_blocked(self):
        self.assertIsNotNone(am._check_apply_target(self._db("postgresql"), self._client("localhost")))

    def test_run_migration_raises_on_non_local_client(self):
        # config!=client hole 차단 — sqlite DB여도 non-local client면 apply 차단
        with self.assertRaises(RuntimeError):
            am.run_migration(self._db("sqlite"), self._client("prod-host"), apply=True, scope="bank")


def _run_apply_mocked(*, bank_rrs, get_values, compare_outcome=None, migrate_outcomes=None,
                      max_retries=3, timeout_sec=60.0, monotonic=None, scope="bank", inv_rr=None):
    """mocked writer/client로 run_migration(apply=True) — 실Redis 없이 runner 분기 검증."""
    db = MagicMock()
    client = MagicMock()
    if isinstance(get_values, list):
        client.get.side_effect = [v.encode() if isinstance(v, str) else v for v in get_values]
    else:
        client.get.return_value = get_values.encode() if isinstance(get_values, str) else get_values
    mock_writer = MagicMock()
    if compare_outcome is not None:
        mock_writer.compare_write.return_value = compare_outcome
    if migrate_outcomes is not None:
        mock_writer.migrate_cas.side_effect = migrate_outcomes
    patches = [
        patch("app.crud._select_latest_bank_rates_with_revision", return_value=bank_rrs),
        patch("app.crud._select_latest_investing_rate_with_revision", return_value=inv_rr),
        patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]),
        patch("app.atomic_lua.AtomicLatestWriter", return_value=mock_writer),
        patch("app.atomic_migration._check_apply_target", return_value=None),  # MagicMock db/client
    ]
    if monotonic is not None:
        patches.append(patch("app.atomic_migration.time.monotonic", side_effect=monotonic))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = am.run_migration(db, client, apply=True, scope=scope,
                                  max_retries=max_retries, timeout_sec=timeout_sec)
    return result, mock_writer, client


class TestRunMigrationRetry(unittest.TestCase):
    """H2: migrate_cas 'changed' bounded retry — refetch / retry_exhausted / max_retries=0 / timeout."""

    def test_changed_then_migrated_refetches(self):
        rr = _db(source="kb", ts=_TS)
        result, w, client = _run_apply_mocked(
            bank_rrs=[rr], get_values=[_v1(1300.0, _TS)] * 3,  # 매 iteration 재GET
            migrate_outcomes=["changed", "changed", "migrated"], max_retries=3)
        r = result.results[0]
        self.assertEqual(r.action, "migrated")
        self.assertEqual(r.retries, 2)
        self.assertEqual(w.migrate_cas.call_count, 3)
        self.assertEqual(client.get.call_count, 3)  # 매 retry Redis 재fetch 불변

    def test_retry_exhausted(self):
        rr = _db(source="kb", ts=_TS)
        result, w, _ = _run_apply_mocked(
            bank_rrs=[rr], get_values=_v1(1300.0, _TS),
            migrate_outcomes=["changed"] * 10, max_retries=3)
        r = result.results[0]
        self.assertEqual(r.action, "retry_exhausted")
        self.assertIsNotNone(r.error)
        self.assertEqual(r.retries, 4)  # max_retries+1

    def test_max_retries_zero(self):
        rr = _db(source="kb", ts=_TS)
        result, _, _ = _run_apply_mocked(
            bank_rrs=[rr], get_values=_v1(1300.0, _TS),
            migrate_outcomes=["changed", "migrated"], max_retries=0)
        self.assertEqual(result.results[0].action, "retry_exhausted")

    def test_retry_refetches_db_newer_revision(self):
        # codex Low: retry가 DB도 재fetch — 1st changed 후 selector가 newer revision 반환 →
        # 2nd migrate_cas가 newer v2(revision)를 사용하는지 검증.
        rr_old = _db(source="kb", ts=_TS, row_id=42)
        rr_new = _db(source="kb", ts=_TS_LATER, row_id=43)
        db = MagicMock()
        client = MagicMock()
        client.get.return_value = _v1(1300.0, _TS).encode()
        mock_writer = MagicMock()
        mock_writer.migrate_cas.side_effect = ["changed", "migrated"]
        # selector: _iter_targets 1회 + db_fetch(iter1)=[rr_old], db_fetch(iter2 retry)=[rr_new]
        with patch("app.crud._select_latest_bank_rates_with_revision",
                   side_effect=[[rr_old], [rr_old], [rr_new]]), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=None), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]), \
             patch("app.atomic_lua.AtomicLatestWriter", return_value=mock_writer), \
             patch("app.atomic_migration._check_apply_target", return_value=None):
            result = am.run_migration(db, client, apply=True, scope="bank", max_retries=3)
        self.assertEqual(result.results[0].action, "migrated")
        # 2nd migrate_cas의 v2_value(3번째 positional)가 rr_new revision 포함
        second_v2 = mock_writer.migrate_cas.call_args_list[1].args[2]
        self.assertIn(make_revision_key_from_revision(rr_new.revision), second_v2)
        self.assertNotIn(make_revision_key_from_revision(rr_old.revision), second_v2)

    def test_timeout(self):
        rr = _db(source="kb", ts=_TS)
        # monotonic: [deadline 계산=0, retry 체크=100>60] → timeout
        result, _, _ = _run_apply_mocked(
            bank_rrs=[rr], get_values=_v1(1300.0, _TS),
            migrate_outcomes=["changed"] * 5, max_retries=100, timeout_sec=60.0,
            monotonic=[0.0, 100.0, 100.0, 100.0])
        self.assertEqual(result.results[0].action, "timeout")


class TestRunMigrationSeedRace(unittest.TestCase):
    """M5: seed_from_db compare_write 실제 outcome 처리 — silent false-success 금지."""

    def test_skipped_newer_non_error(self):
        rr = _db(source="kb", ts=_TS)
        result, _, _ = _run_apply_mocked(bank_rrs=[rr], get_values=None, compare_outcome="skipped_newer")
        r = result.results[0]
        self.assertEqual(r.action, "skipped_newer")
        self.assertFalse(r.wrote)
        self.assertIsNone(r.error)  # benign (더 신선한 값 존재)

    def test_conflict_fail_closed(self):
        rr = _db(source="kb", ts=_TS)
        result, _, _ = _run_apply_mocked(bank_rrs=[rr], get_values=None, compare_outcome="conflict")
        r = result.results[0]
        self.assertEqual(r.action, "conflict")
        self.assertIsNotNone(r.error)  # fail-closed

    def test_invalid_schema_fail_closed(self):
        rr = _db(source="kb", ts=_TS)
        result, _, _ = _run_apply_mocked(bank_rrs=[rr], get_values=None, compare_outcome="invalid_schema")
        self.assertIsNotNone(result.results[0].error)

    def test_migration_required_retries_to_migrate(self):
        # seed(absent)→compare_write migration_required(v1 출현)→retry→v1 GET→migrate_cas
        rr = _db(source="kb", ts=_TS)
        result, w, _ = _run_apply_mocked(
            bank_rrs=[rr], get_values=[None, _v1(1300.0, _TS)],
            compare_outcome="migration_required", migrate_outcomes=["migrated"], max_retries=3)
        r = result.results[0]
        self.assertEqual(r.action, "migrated")
        self.assertTrue(r.wrote)
        self.assertEqual(r.retries, 1)


class TestRunMigrationIsolationAndScope(unittest.TestCase):

    def test_selector_failure_isolated_batch_continues(self):
        # H1: bank selector 예외 → fetch_error result, investing은 계속 처리
        db = MagicMock()
        client = MagicMock()
        client.get.return_value = None
        inv_rr = _db(source="investing", asset="usd-krw", ts=_TS)
        with patch("app.crud._select_latest_bank_rates_with_revision", side_effect=RuntimeError("db boom")), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=inv_rr), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, client, apply=False, scope="all")
        actions = {r.kind: r.action for r in result.results}
        self.assertEqual(actions.get("bank"), "fetch_error")
        self.assertEqual(result.error_count, 1)
        self.assertEqual(actions.get("investing"), am.ACTION_SEED_FROM_DB)  # batch 계속

    def test_db_absent_when_row_vanishes(self):
        rr = _db(source="kb", ts=_TS)
        db = MagicMock()
        client = MagicMock()
        client.get.return_value = None
        # _iter_targets 1st call=[rr] yield, db_fetch 2nd call=[] → None → db_absent
        with patch("app.crud._select_latest_bank_rates_with_revision", side_effect=[[rr], []]), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=None), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, client, apply=False, scope="bank")
        self.assertEqual(result.results[0].action, "db_absent")

    def test_decision_conflict_is_error(self):
        # codex High: decision-level conflict(동일 ts 다른 rate)도 error → CLI exit 1 (dry-run에서도 surface)
        db = MagicMock()
        client = MagicMock()
        client.get.return_value = _v1(1399.0, _TS).encode()  # 동일 ts, 다른 rate → CONFLICT
        with patch("app.crud._select_latest_bank_rates_with_revision",
                   return_value=[_db(rate=1300.0, ts=_TS)]), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=None), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, client, apply=False, scope="bank")
        r = result.results[0]
        self.assertEqual(r.action, am.ACTION_CONFLICT)
        self.assertIsNotNone(r.error)  # fail-closed
        self.assertEqual(result.error_count, 1)

    def test_decision_invalid_schema_is_error(self):
        db = MagicMock()
        client = MagicMock()
        client.get.return_value = b"{not json"  # → invalid_schema decision
        with patch("app.crud._select_latest_bank_rates_with_revision",
                   return_value=[_db(rate=1300.0, ts=_TS)]), \
             patch("app.crud._select_latest_investing_rate_with_revision", return_value=None), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, client, apply=False, scope="bank")
        self.assertEqual(result.results[0].action, am.ACTION_INVALID_SCHEMA)
        self.assertIsNotNone(result.results[0].error)

    def test_scope_all_mixed_bank_investing(self):
        db = MagicMock()
        client = MagicMock()
        client.get.return_value = None
        with patch("app.crud._select_latest_bank_rates_with_revision",
                   return_value=[_db(source="kb", asset="usd-krw", ts=_TS)]), \
             patch("app.crud._select_latest_investing_rate_with_revision",
                   return_value=_db(source="investing", asset="usd-krw", ts=_TS)), \
             patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, client, apply=False, scope="all")
        self.assertEqual({r.kind for r in result.results}, {"bank", "investing"})


class TestCLI(unittest.TestCase):
    """CLI 인자검증 + apply prod hard-fail (subprocess — BLOCKED 경로는 Redis/DB 전에 종료)."""

    _ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

    def _run(self, args, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "scripts/migrate_atomic_latest_values.py", *args],
            capture_output=True, text=True, env=env, cwd=self._ROOT, timeout=60,
        )

    def test_max_retries_negative_blocked(self):
        p = self._run(["--max-retries=-1"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("BLOCKED", p.stdout)

    def test_timeout_zero_blocked(self):
        p = self._run(["--timeout-sec=0"])
        self.assertEqual(p.returncode, 1)

    def test_apply_prod_hard_fail(self):
        # 드라이버는 프로젝트 실제 드라이버 psycopg(v3) 명시 — bare `postgresql://`는 SQLAlchemy 기본
        # psycopg2를 create_engine 시점에 import하는데 lock엔 psycopg2 부재(psycopg v3만) → CI clean
        # 설치에서 ModuleNotFoundError로 subprocess가 [BLOCKED] 전에 크래시(로컬은 psycopg2 leak로 가려짐).
        # dialect.name은 "postgresql" 동일이라 check_apply_safety의 non-sqlite 차단 의도 보존.
        p = self._run(["--apply"], env_extra={"DATABASE_URL": "postgresql+psycopg://u:p@prod-host/db"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("BLOCKED", p.stdout)


def _sqlite_session(rows):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.BankExchangeRate.__table__.create(engine)
    models.InvestingExchangeRate.__table__.create(engine)
    s = sessionmaker(bind=engine)()
    for r in rows:
        s.add(r)
    s.commit()
    return s


_REDIS_TEST_URL_ENV = os.environ.get("REDIS_TEST_URL")
_REDIS_TEST_URL = _REDIS_TEST_URL_ENV or "redis://localhost:6379/15"


class TestRunMigrationApplyRealRedis(unittest.TestCase):
    """A3-3b apply — env-gated 실Redis + sqlite DB. raw-CAS round-trip / idempotent / seed."""

    def setUp(self):
        import redis
        try:
            self.client = redis.Redis.from_url(_REDIS_TEST_URL)
            self.client.ping()
        except Exception as e:
            if _REDIS_TEST_URL_ENV:
                raise RuntimeError(f"REDIS_TEST_URL={_REDIS_TEST_URL} 연결 실패 (CI redis service 필수): {e}")
            self.skipTest(f"로컬 Redis 미가동: {e}")
        # run_migration apply 가드(_check_apply_target)는 실제 db bind(sqlite) + client host(이 테스트의
        # local REDIS_TEST_URL)를 검사 → 자연 통과 (config patch 불요).
        self.key = latest_key_bank("kb", "usd-krw")
        self.client.delete(self.key)

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.delete(self.key)

    def _db_session(self):
        return _sqlite_session([models.BankExchangeRate(bank="kb", currency="usd-krw", rate=1300.0, timestamp=_TS)])

    def test_apply_migrates_v1_to_v2(self):
        self.client.set(self.key, _v1(1300.0, _TS))  # 동일 ts·rate → seed → migrate_cas
        db = self._db_session()
        with patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, self.client, apply=True, scope="bank")
        kb = [r for r in result.results if r.source == "kb"][0]
        self.assertEqual(kb.action, "migrated")
        self.assertTrue(kb.wrote)
        raw = self.client.get(self.key)
        self.assertEqual(json.loads(raw.decode())["schema_version"], 2)

    def test_idempotent_rerun(self):
        self.client.set(self.key, _v1(1300.0, _TS))
        db = self._db_session()
        with patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            am.run_migration(db, self.client, apply=True, scope="bank")
            result2 = am.run_migration(db, self.client, apply=True, scope="bank")
        kb = [r for r in result2.results if r.source == "kb"][0]
        self.assertEqual(kb.action, am.ACTION_ALREADY_CURRENT)
        self.assertFalse(kb.wrote)  # 재실행 no-op

    def test_seed_from_absent(self):
        # Redis 부재 → seed_from_db → compare_write advance
        db = self._db_session()
        with patch("app.crud.SUPPORTED_CURRENCY_PAIRS", ["usd-krw"]):
            result = am.run_migration(db, self.client, apply=True, scope="bank")
        kb = [r for r in result.results if r.source == "kb"][0]
        self.assertEqual(kb.action, "advance")  # compare_write nil→SET 실제 outcome
        self.assertTrue(kb.wrote)
        self.assertEqual(json.loads(self.client.get(self.key).decode())["schema_version"], 2)


# dormant island — 서로 정당 교차 import (atomic_lua가 atomic_migration 참조 등)만 skip. live atomic
# 모듈(atomic_write_control/runtime/refresh/revision)은 scan 대상으로 남겨 미래 회귀까지 잡음
# (codex holistic cross-check — startswith("atomic_") 광역 skip은 live atomic까지 가려 약함).
_DORMANT_ISLAND = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
    "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
    "atomic_build.py", "atomic_reconcile.py", "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py", "atomic_fx_v2_loader.py",  # B2a/B2b-1/B2b-4a dormant — uniform dormant skip set
})


class TestDormancy(unittest.TestCase):
    """A3-3 dormant — **app/ 전체** 어떤 live 모듈도 atomic_migration import 0 (crawler live-writer +
    live atomic 모듈 포함 — dormant island만 skip, codex holistic cross-check)."""

    def test_no_app_module_imports_atomic_migration(self):
        import ast

        app_dir = pathlib.Path(am.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _DORMANT_ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_migration" in node.module.split("."):
                        self.fail(f"{rel}: from atomic_migration import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_migration" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_migration — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_migration" in a.name.split("."):
                            self.fail(f"{rel}: import atomic_migration — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "atomic_migration" in node.value:
                        self.fail(f"{rel}: 문자열 '{node.value}'에 atomic_migration — dynamic 의심")


if __name__ == "__main__":
    unittest.main()
