"""P1b C6-5b-3b — bank atomic-branch writer 단위 테스트 (crud._insert_bank_rates_atomic).

outcome matrix(6 + compare_write exception) → commit 1회 / alert ALL changes(DB-authoritative) / trigger
APPLIED subset만(C7) / return count / no rollback + telemetry. + post-commit isolation(build raise →
alert·return 생존, codex guard) + commit-fail → downstream 미도달 + flush-row-ref revision.
legacy/halt 불변은 a2_2 + test_source_direct_write가 잠금(여기선 atomic 분기만).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models
from app.atomic_lua import (
    OUTCOME_ADVANCE,
    OUTCOME_CONFLICT,
    OUTCOME_INVALID_SCHEMA,
    OUTCOME_MIGRATION_REQUIRED,
    OUTCOME_REFRESHED_EQUAL,
    OUTCOME_SKIPPED_NEWER,
)
from app.atomic_revision import revision_from_row
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot

_APPLIED_OUTCOMES = (OUTCOME_ADVANCE, OUTCOME_REFRESHED_EQUAL)
_NOT_APPLIED_OUTCOMES = (
    OUTCOME_SKIPPED_NEWER, OUTCOME_CONFLICT, OUTCOME_MIGRATION_REQUIRED, OUTCOME_INVALID_SCHEMA,
)


def _snap(enforced: str) -> WriteModeSnapshot:
    return WriteModeSnapshot(
        diagnostic_effective_mode=enforced, activation_latched=(enforced != WriterMode.LEGACY),
        enforced_action=enforced, mode_generation=0,
    )


class _FakeWriter:
    def __init__(self, outcome: str = OUTCOME_ADVANCE, raise_exc: Exception = None) -> None:
        self.outcome = outcome
        self.raise_exc = raise_exc
        self.calls = []

    def compare_write(self, key, v2_value, revision_key, rate_key):
        self.calls.append(key)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.outcome


def _session():
    engine = create_engine("sqlite:///:memory:")
    models.BankExchangeRate.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


def _run_atomic(db, *, writer=None, build_exc=None, alerts_return=0):
    """atomic mode로 insert_bank_rates_into_db 1 change 실행 — Redis/alert/trigger 경계 patch."""
    build = patch("app.atomic_direct_write.build_atomic_writer",
                  side_effect=build_exc) if build_exc else \
        patch("app.atomic_direct_write.build_atomic_writer", return_value=writer)
    with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
         build, \
         patch("app.crud.process_rate_alerts", return_value=alerts_return) as alerts, \
         patch("app.crud._emit_topic_triggers") as emit:
        result = crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
    return result, alerts, emit


class TestBankAtomicOutcomeMatrix(unittest.TestCase):

    def _assert_committed(self, db):
        row = db.query(models.BankExchangeRate).filter_by(bank="kb", currency="usd-krw").one()
        self.assertEqual(row.rate, 1300.0)

    def test_applied_outcomes_trigger(self):
        for outcome in _APPLIED_OUTCOMES:
            with self.subTest(outcome=outcome):
                db = _session(); fake = _FakeWriter(outcome)
                result, alerts, emit = _run_atomic(db, writer=fake)
                self.assertEqual(result, 1)                  # staged count
                self._assert_committed(db)                   # commit 발생
                self.assertEqual(len(fake.calls), 1)         # compare_write 1회
                alerts.assert_called_once()                  # alert DB-authoritative
                self.assertEqual(len(alerts.call_args[0][1]), 1)
                emit.assert_called_once()
                self.assertEqual(len(emit.call_args[0][0]), 1)  # APPLIED → trigger subset 1

    def test_not_applied_outcomes_commit_alert_no_trigger(self):
        for outcome in _NOT_APPLIED_OUTCOMES:
            with self.subTest(outcome=outcome):
                db = _session(); fake = _FakeWriter(outcome)
                result, alerts, emit = _run_atomic(db, writer=fake)
                self.assertEqual(result, 1)
                self._assert_committed(db)                   # commit 발생(rollback 아님)
                alerts.assert_called_once()                  # alert STILL fires (C7 — DB-authoritative)
                self.assertEqual(len(alerts.call_args[0][1]), 1)
                emit.assert_called_once()
                self.assertEqual(len(emit.call_args[0][0]), 0)  # NOT_APPLIED → trigger 0 (skipped_newer 포함, C7)

    def test_compare_write_exception_commit_alert_no_trigger(self):
        # compare_write raise → UNCERTAIN_AFTER_SEND(NOT APPLIED) → commit/alert 유지, trigger 0, rollback 아님
        db = _session(); fake = _FakeWriter(raise_exc=RuntimeError("redis lost"))
        result, alerts, emit = _run_atomic(db, writer=fake)
        self.assertEqual(result, 1)
        self._assert_committed(db)
        alerts.assert_called_once()
        self.assertEqual(len(emit.call_args[0][0]), 0)

    def test_telemetry_counter_records_state_and_structural(self):
        crud._atomic_write_outcome_counts.clear()
        # conflict → (kb, "conflict", False); migration_required → (kb, "failed", True) [structural 세분]
        _run_atomic(_session(), writer=_FakeWriter(OUTCOME_CONFLICT))
        _run_atomic(_session(), writer=_FakeWriter(OUTCOME_MIGRATION_REQUIRED))
        _run_atomic(_session(), writer=_FakeWriter(OUTCOME_INVALID_SCHEMA))
        self.assertEqual(crud._atomic_write_outcome_counts.get(("kb", "conflict", False)), 1)
        # migration_required + invalid_schema 모두 structural FAILED → (kb, "failed", True) 2건 (general failed와 구분)
        self.assertEqual(crud._atomic_write_outcome_counts.get(("kb", "failed", True)), 2)
        self.assertIsNone(crud._atomic_write_outcome_counts.get(("kb", "failed", False)))  # structural과 분리

    def test_telemetry_general_failed_distinct_from_structural(self):
        # compare_write raise → UNCERTAIN_AFTER_SEND = FAILED + structural False → (kb, "failed", False)
        crud._atomic_write_outcome_counts.clear()
        _run_atomic(_session(), writer=_FakeWriter(raise_exc=RuntimeError("lost")))
        self.assertEqual(crud._atomic_write_outcome_counts.get(("kb", "failed", False)), 1)
        self.assertIsNone(crud._atomic_write_outcome_counts.get(("kb", "failed", True)))  # G3 structural과 구분


class TestBankAtomicIsolation(unittest.TestCase):
    """codex guard — post-commit Redis(build 포함) 실패가 alert/return을 막지 않음(DB 이미 commit)."""

    def test_build_writer_raise_after_commit_preserves_alert_and_return(self):
        db = _session()
        result, alerts, emit = _run_atomic(db, build_exc=RuntimeError("writer build fail"))
        self.assertEqual(result, 1)                      # return 유지
        row = db.query(models.BankExchangeRate).filter_by(bank="kb").one()
        self.assertEqual(row.rate, 1300.0)               # commit 유지(rollback 아님)
        alerts.assert_called_once()                      # alert 유지(DB-authoritative)
        emit.assert_called_once()
        self.assertEqual(len(emit.call_args[0][0]), 0)   # APPLIED 없음 → trigger 0

    def test_writer_none_per_change_not_applied(self):
        # build_atomic_writer None(client 부재) → atomic_compare_write_v2(None) → writer_unavailable(NOT APPLIED)
        db = _session()
        result, alerts, emit = _run_atomic(db, writer=None)
        self.assertEqual(result, 1)
        alerts.assert_called_once()
        self.assertEqual(len(emit.call_args[0][0]), 0)


class TestBankAtomicCommitFailure(unittest.TestCase):
    """commit 실패 → compare_write/alert/emit 미도달 (commit raise 전파, downstream 보호 안 됨이 정상)."""

    def test_commit_failure_skips_downstream(self):
        db = _session()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch.object(db, "commit", side_effect=RuntimeError("commit fail")), \
             patch("app.atomic_direct_write.build_atomic_writer") as build, \
             patch("app.crud.process_rate_alerts") as alerts, \
             patch("app.crud._emit_topic_triggers") as emit:
            with self.assertRaises(RuntimeError):
                crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        build.assert_not_called()     # commit 전 raise → Redis 미도달
        alerts.assert_not_called()
        emit.assert_not_called()

    def test_flush_failure_skips_downstream(self):
        # atomic 분기 신규 raise site db.flush()가 raise → commit/Redis/alert/emit 미도달 + 전파 (pre-commit fail-closed)
        db = _session()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch.object(db, "flush", side_effect=RuntimeError("flush fail")), \
             patch.object(db, "commit") as commit, \
             patch("app.atomic_direct_write.build_atomic_writer") as build, \
             patch("app.crud.process_rate_alerts") as alerts, \
             patch("app.crud._emit_topic_triggers") as emit:
            with self.assertRaises(RuntimeError):
                crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        commit.assert_not_called()    # flush raise → commit 미도달
        build.assert_not_called()
        alerts.assert_not_called()
        emit.assert_not_called()


class TestBankAtomicCallOrder(unittest.TestCase):
    """call-order: compare_write(redis) → process_rate_alerts → _emit_topic_triggers (axis #2/#3 sequencing).

    commit-before-Redis는 TestBankAtomicCommitFailure(commit raise → redis 미도달)가 잠금 — 여기선 redis 후
    alerts 후 emit 순서를 직접 고정(미래 reorder 회귀 차단).
    """

    def test_redis_then_alerts_then_emit(self):
        order = []

        class _OrderWriter:
            def compare_write(self, *a, **k):
                order.append("redis")
                return OUTCOME_ADVANCE

        db = _session()
        with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), \
             patch("app.atomic_direct_write.build_atomic_writer", return_value=_OrderWriter()), \
             patch("app.crud.process_rate_alerts", side_effect=lambda *a, **k: order.append("alerts") or 0), \
             patch("app.crud._emit_topic_triggers", side_effect=lambda *a, **k: order.append("emit")):
            crud.insert_bank_rates_into_db(db, {"usd-krw": 1300.0}, "kb")
        self.assertEqual(order, ["redis", "alerts", "emit"])


class TestFlushRowRefRevision(unittest.TestCase):
    """flush-row-ref revision (StagedRateChange.revision) — re-read selector 아님, flush 후 row.id 기반."""

    def test_revision_from_staged_row_after_flush(self):
        db = _session()
        staged = crud._stage_bank_rate_changes(db, {"usd-krw": 1300.0}, "kb")
        self.assertEqual(len(staged), 1)
        db.flush()  # id 할당
        sc = staged[0]
        self.assertEqual(sc.revision, revision_from_row(sc.row))   # 공유 구성
        self.assertIsInstance(sc.row.id, int)                      # flush 후 id 할당됨
        self.assertEqual(sc.change.source, "kb")


if __name__ == "__main__":
    unittest.main()
