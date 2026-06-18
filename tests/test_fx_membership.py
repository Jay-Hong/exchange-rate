"""FX membership contract 단위 테스트 (PR D / P1b B2a).

set ↔ version lock — FX_MEMBERSHIP_SOURCES 변경 시 FX_MEMBERSHIP_VERSION을 함께 bump하도록 강제(§5.1
"명시적 membership revision 변경"). 이 테스트가 깨지면: 은행 추가/제거를 했으면 version도 bump하라는 신호.
"""
from __future__ import annotations

import unittest

from app import fx_membership as fm
from app.crud import BANK_DISPLAY_ORDER


class TestFxMembership(unittest.TestCase):

    def test_sources_are_9_banks_plus_investing(self):
        self.assertEqual(fm.FX_MEMBERSHIP_SOURCES, frozenset(BANK_DISPLAY_ORDER) | {"investing"})
        self.assertEqual(len(fm.FX_MEMBERSHIP_SOURCES), 10)
        self.assertIn("investing", fm.FX_MEMBERSHIP_SOURCES)

    def test_version_locked_to_set(self):
        # ⚠️ set 변경 시 이 expected와 version을 함께 갱신 (§5.1 명시 membership revision).
        expected = frozenset({
            "kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi", "investing",
        })
        self.assertEqual(fm.FX_MEMBERSHIP_SOURCES, expected)
        self.assertEqual(fm.FX_MEMBERSHIP_VERSION, 1)

    def test_helper_returns_version(self):
        self.assertEqual(fm.fx_membership_version(), fm.FX_MEMBERSHIP_VERSION)


if __name__ == "__main__":
    unittest.main()
