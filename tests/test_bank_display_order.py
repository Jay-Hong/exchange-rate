"""은행 최신 환율 표시순 정렬 테스트."""
from __future__ import annotations

import unittest

from app import crud


class TestBankDisplaySortKey(unittest.TestCase):

    def test_registered_banks_follow_client_display_order(self):
        self.assertLess(
            crud._bank_display_sort_key("kb"),
            crud._bank_display_sort_key("hana"),
        )
        self.assertLess(
            crud._bank_display_sort_key("hana"),
            crud._bank_display_sort_key("shinhan"),
        )
        self.assertLess(
            crud._bank_display_sort_key("bs"),
            crud._bank_display_sort_key("citi"),
        )

    def test_unknown_bank_goes_after_registered_banks(self):
        self.assertGreater(
            crud._bank_display_sort_key("newbank"),
            crud._bank_display_sort_key("citi"),
        )

    def test_unknown_banks_fallback_to_code_order(self):
        banks = ["zzbank", "newbank", "aabank"]
        self.assertEqual(
            sorted(banks, key=crud._bank_display_sort_key),
            ["aabank", "newbank", "zzbank"],
        )

    def test_mixed_sort_order(self):
        banks = ["newbank", "hana", "citi", "kb", "aabank", "woori"]
        self.assertEqual(
            sorted(banks, key=crud._bank_display_sort_key),
            ["kb", "hana", "woori", "citi", "aabank", "newbank"],
        )

    def test_bank_display_order_matches_ios_android_enum(self):
        self.assertEqual(
            crud.BANK_DISPLAY_ORDER,
            ["kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
