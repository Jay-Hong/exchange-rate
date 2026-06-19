"""P1b C6-5b-3c — investing atomic-branch writer 단위 테스트 (crud._insert_investing_rates_atomic).

bank(3b)와 동일 패턴(_atomic_write_changes_v2 재사용)이라 outcome matrix 전수는 bank 파일이 잠금. 여기선
**investing-specific 델타** + 핵심 invariant만: key_kind="investing"→latest_key_investing(asset) 라우팅 /
source label "investing" telemetry / advance commit+alert+trigger / skipped_newer no-trigger(C7) /
isolation(build raise → alert·return 생존). legacy/halt 불변은 a2_2 + test_source_direct_write.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, models
from app.atomic_lua import OUTCOME_ADVANCE, OUTCOME_CONFLICT, OUTCOME_SKIPPED_NEWER
from app.atomic_write_control import WriterMode
from app.atomic_write_runtime import WriteModeSnapshot
from app.latest_rates_cache import latest_key_investing


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
    models.InvestingExchangeRate.__table__.create(bind=engine)
    return sessionmaker(bind=engine)()


def _run(db, *, writer=None, build_exc=None):
    build = patch("app.atomic_direct_write.build_atomic_writer", side_effect=build_exc) if build_exc \
        else patch("app.atomic_direct_write.build_atomic_writer", return_value=writer)
    with patch("app.atomic_write_runtime.snapshot", return_value=_snap(WriterMode.ATOMIC)), build, \
         patch("app.crud.process_rate_alerts", return_value=0) as alerts, \
         patch("app.crud._emit_topic_triggers") as emit:
        result = crud.insert_investing_rates_into_db(db, {"usd-krw": 1300.0})
    return result, alerts, emit


class TestInvestingAtomicBranch(unittest.TestCase):

    def _assert_committed(self, db):
        row = db.query(models.InvestingExchangeRate).filter_by(currency="usd-krw").one()
        self.assertEqual(row.rate, 1300.0)

    def test_advance_uses_investing_key_commits_alerts_triggers(self):
        db = _session(); fake = _FakeWriter(OUTCOME_ADVANCE)
        result, alerts, emit = _run(db, writer=fake)
        self.assertEqual(result, 1)
        self._assert_committed(db)
        # investing-specific: key_kind="investing" → latest_key_investing(asset) (bank key 아님)
        self.assertEqual(fake.calls, [latest_key_investing("usd-krw")])
        alerts.assert_called_once()
        self.assertEqual(len(alerts.call_args[0][1]), 1)  # alert all changes
        emit.assert_called_once()
        self.assertEqual(len(emit.call_args[0][0]), 1)    # APPLIED → trigger 1

    def test_skipped_newer_commits_alerts_no_trigger(self):
        db = _session(); fake = _FakeWriter(OUTCOME_SKIPPED_NEWER)
        result, alerts, emit = _run(db, writer=fake)
        self.assertEqual(result, 1)
        self._assert_committed(db)
        alerts.assert_called_once()                        # DB-authoritative
        self.assertEqual(len(emit.call_args[0][0]), 0)    # NOT_APPLIED → no trigger (C7)

    def test_build_raise_isolation_preserves_alert_and_return(self):
        db = _session()
        result, alerts, emit = _run(db, build_exc=RuntimeError("writer build fail"))
        self.assertEqual(result, 1)                        # return 유지
        self._assert_committed(db)                          # commit 유지 (rollback 아님)
        alerts.assert_called_once()                         # alert 유지
        self.assertEqual(len(emit.call_args[0][0]), 0)     # APPLIED 없음 → trigger 0

    def test_telemetry_source_label_investing(self):
        crud._atomic_write_outcome_counts.clear()
        _run(_session(), writer=_FakeWriter(OUTCOME_CONFLICT))
        # source label "investing" (telemetry shape 일관) + state/structural 키
        self.assertEqual(crud._atomic_write_outcome_counts.get(("investing", "conflict", False)), 1)


if __name__ == "__main__":
    unittest.main()
