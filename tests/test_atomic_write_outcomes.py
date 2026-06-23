"""C7-a — crud.get_atomic_write_outcome_counts() read accessor 단위 검증.

write-only이던 `_atomic_write_outcome_counts`를 aggregate/per_source/critical/health로 구조화하는
accessor 로직. recorder는 duck-typed .state.value/.structural만 보므로 fake outcome으로 주입.
"""
from __future__ import annotations

import unittest

from app import crud


class _FakeState:
    def __init__(self, value: str):
        self.value = value


class _FakeOutcome:
    """recorder가 읽는 surface(.state.value/.structural)만 갖춘 최소 fake."""

    def __init__(self, state_value: str, structural: bool = False):
        self.state = _FakeState(state_value)
        self.structural = structural


class TestGetAtomicWriteOutcomeCounts(unittest.TestCase):

    def setUp(self):
        crud._atomic_write_outcome_counts.clear()  # process-local global — 테스트 간 격리

    def tearDown(self):
        crud._atomic_write_outcome_counts.clear()

    def test_empty_is_ok_and_g3_ok(self):
        out = crud.get_atomic_write_outcome_counts()
        self.assertEqual(out["aggregate"]["total"], 0)
        self.assertEqual(out["health"], "ok")
        self.assertTrue(out["g3_ok"])
        self.assertTrue(out["process_local"])
        self.assertIn("started_at", out)
        self.assertEqual(out["per_source"], {})

    def test_normal_states_no_critical(self):
        for _ in range(3):
            crud._record_atomic_write_outcome("kb", _FakeOutcome("advance"))
        crud._record_atomic_write_outcome("kb", _FakeOutcome("refreshed_equal"))
        crud._record_atomic_write_outcome("investing", _FakeOutcome("skipped_newer"))
        out = crud.get_atomic_write_outcome_counts()
        agg = out["aggregate"]
        self.assertEqual(agg["total"], 5)
        self.assertEqual(agg["by_state"]["advance"], 3)
        self.assertEqual(agg["by_state"]["refreshed_equal"], 1)
        self.assertEqual(agg["by_state"]["skipped_newer"], 1)
        self.assertEqual(agg["critical"]["total"], 0)
        self.assertEqual(out["health"], "ok")
        self.assertTrue(out["g3_ok"])

    def test_conflict_and_structural_are_critical(self):
        crud._record_atomic_write_outcome("kb", _FakeOutcome("advance"))
        crud._record_atomic_write_outcome("investing", _FakeOutcome("conflict"))
        crud._record_atomic_write_outcome("kb", _FakeOutcome("failed", structural=True))
        crud._record_atomic_write_outcome("investing", _FakeOutcome("failed", structural=False))
        crud._record_atomic_write_outcome("investing", _FakeOutcome("failed", structural=False))
        out = crud.get_atomic_write_outcome_counts()
        agg = out["aggregate"]
        self.assertEqual(agg["total"], 5)
        self.assertEqual(agg["by_state"]["conflict"], 1)
        self.assertEqual(agg["by_state"]["failed"], {"structural": 1, "general": 2})
        # critical = conflict(1) + failed_structural(1) = 2
        self.assertEqual(agg["critical"], {"total": 2, "conflict": 1, "failed_structural": 1})
        self.assertEqual(out["health"], "critical")
        self.assertFalse(out["g3_ok"])

    def test_per_source_isolation(self):
        crud._record_atomic_write_outcome("kb", _FakeOutcome("advance"))
        crud._record_atomic_write_outcome("kb", _FakeOutcome("advance"))
        crud._record_atomic_write_outcome("kb", _FakeOutcome("failed", structural=True))
        crud._record_atomic_write_outcome("investing", _FakeOutcome("conflict"))
        out = crud.get_atomic_write_outcome_counts()
        kb = out["per_source"]["kb"]
        inv = out["per_source"]["investing"]
        self.assertEqual(kb["total"], 3)
        self.assertEqual(kb["by_state"]["advance"], 2)
        self.assertEqual(kb["critical"], {"total": 1, "conflict": 0, "failed_structural": 1})
        self.assertEqual(inv["total"], 1)
        self.assertEqual(inv["critical"], {"total": 1, "conflict": 1, "failed_structural": 0})

    def test_unknown_state_bucketed_not_dropped(self):
        # 미래 state 추가 시 silent drop 방지 (defensive 'other' bucket)
        crud._record_atomic_write_outcome("kb", _FakeOutcome("future_state"))
        out = crud.get_atomic_write_outcome_counts()
        self.assertEqual(out["aggregate"]["total"], 1)
        self.assertEqual(out["aggregate"]["by_state"]["other"], 1)
        # unknown state는 critical 아님
        self.assertEqual(out["health"], "ok")


if __name__ == "__main__":
    unittest.main()
