"""fanout step 2 — SourceLatestWriteOutcome enum 통합 invariant 잠금 (behavior-change-0).

UsdtLatestWriteOutcome + KrxLatestWriteOutcome를 source-neutral SourceLatestWriteOutcome로 병합.
멤버·value·identity 보존 + atomic v2 WriteState와는 별개 enum 유지.
"""
from __future__ import annotations

import unittest

from app.atomic_write_outcome import WriteState
from app.latest_rates_cache import (
    KrxLatestWriteOutcome,
    SourceLatestWriteOutcome,
    UsdtLatestWriteOutcome,
)


class TestSourceLatestWriteOutcomeUnification(unittest.TestCase):

    def test_legacy_names_are_the_shared_enum(self):
        # 후방 호환 alias = 동일 enum 객체
        self.assertIs(UsdtLatestWriteOutcome, SourceLatestWriteOutcome)
        self.assertIs(KrxLatestWriteOutcome, SourceLatestWriteOutcome)

    def test_members_unified_across_names(self):
        # 같은 멤버 → 동일 identity (통합 핵심 — fanout 공통층이 단일 타입으로 라우팅 가능)
        self.assertIs(UsdtLatestWriteOutcome.SET, KrxLatestWriteOutcome.SET)
        self.assertIs(UsdtLatestWriteOutcome.FAILED, KrxLatestWriteOutcome.FAILED)
        self.assertIs(UsdtLatestWriteOutcome.SKIPPED, KrxLatestWriteOutcome.SKIPPED)
        self.assertIs(UsdtLatestWriteOutcome.BLOCKED, KrxLatestWriteOutcome.BLOCKED)

    def test_union_members_and_values_preserved(self):
        # 병합 enum = union 멤버, .value 문자열 보존 (직렬화 behavior-change-0)
        expected = {
            "FAILED": "failed",
            "SKIPPED": "skipped",
            "SKIPPED_REGRESSION": "skipped_regression",
            "SET": "set",
            "BLOCKED": "blocked",
        }
        actual = {m.name: m.value for m in SourceLatestWriteOutcome}
        self.assertEqual(actual, expected)

    def test_skipped_regression_accessible_via_both_names(self):
        # SKIPPED_REGRESSION은 USDT만 반환하나, 통합 후 멤버 접근은 양쪽 이름 모두 가능
        self.assertIs(
            KrxLatestWriteOutcome.SKIPPED_REGRESSION,
            SourceLatestWriteOutcome.SKIPPED_REGRESSION,
        )

    def test_distinct_from_atomic_write_state(self):
        # atomic v2 WriteState와는 별개 enum (의도적 distinct — 통합 대상 아님)
        self.assertIsNot(WriteState.FAILED, SourceLatestWriteOutcome.FAILED)
        self.assertNotEqual(WriteState.FAILED, SourceLatestWriteOutcome.FAILED)


if __name__ == "__main__":
    unittest.main()
