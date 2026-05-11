"""Legacy exposure policy 단위 테스트 (PR Z-2d Step 1).

검증:
    - allowlist 통과 — 9 banks + investing × 3 assets
    - topic-only source 제외 — USDT 거래소, KRX, 미등록 source
    - asset mismatch 제외 — usdt-krw, usd-krw-futures
    - source/asset 이상 조합 — (investing, usdt-krw), (kb, usdt-krw) 등
    - constants exact match (회귀 보호)
"""
from __future__ import annotations

import unittest

from app.legacy_policy import (
    LEGACY_RATE_ASSETS,
    LEGACY_RATE_SOURCES,
    should_include_source_in_legacy_rates,
)


class TestShouldIncludeSourceInLegacyRates(unittest.TestCase):

    # ---------------------------------------------------------------
    # True cases — allowlist 통과
    # ---------------------------------------------------------------

    def test_bank_kb_usd_krw_true(self):
        self.assertTrue(should_include_source_in_legacy_rates("kb", "usd-krw"))

    def test_bank_hana_jpy_krw_true(self):
        self.assertTrue(should_include_source_in_legacy_rates("hana", "jpy-krw"))

    def test_investing_eur_krw_true(self):
        self.assertTrue(should_include_source_in_legacy_rates("investing", "eur-krw"))

    def test_all_legacy_banks_and_investing_pass_all_assets(self):
        """LEGACY_RATE_SOURCES × LEGACY_RATE_ASSETS 모두 통과 — Cartesian product 검증."""
        for source in LEGACY_RATE_SOURCES:
            for asset in LEGACY_RATE_ASSETS:
                with self.subTest(source=source, asset=asset):
                    self.assertTrue(
                        should_include_source_in_legacy_rates(source, asset),
                        f"({source}, {asset}) should be allowed"
                    )

    # ---------------------------------------------------------------
    # False cases — topic-only source 제외
    # ---------------------------------------------------------------

    def test_upbit_usdt_krw_false(self):
        """USDT 거래소는 legacy 노출 X (topic API 전용)."""
        self.assertFalse(should_include_source_in_legacy_rates("upbit", "usdt-krw"))

    def test_krx_futures_false(self):
        """KRX 미국달러선물은 legacy 노출 X (usdt:krw topic의 optional group)."""
        self.assertFalse(should_include_source_in_legacy_rates("krx", "usd-krw-futures"))

    def test_investing_usdt_krw_false(self):
        """이상 조합: investing source / usdt-krw asset — 양쪽 allowlist 모두 통과해야 함.

        investing은 LEGACY_RATE_SOURCES에 있지만 usdt-krw는 LEGACY_RATE_ASSETS에 없음
        → AND 조건 미통과 → False. 만약 investing이 미래 USDT 추적 시작해도 자동 차단.
        """
        self.assertFalse(should_include_source_in_legacy_rates("investing", "usdt-krw"))

    def test_kb_usdt_krw_false(self):
        """KB는 LEGACY_RATE_SOURCES에 있지만 usdt-krw는 LEGACY_RATE_ASSETS 미포함 → False."""
        self.assertFalse(should_include_source_in_legacy_rates("kb", "usdt-krw"))

    # ---------------------------------------------------------------
    # False cases — unknown source / asset
    # ---------------------------------------------------------------

    def test_unknown_source_usd_krw_false(self):
        self.assertFalse(should_include_source_in_legacy_rates("unknown_source", "usd-krw"))

    def test_unknown_source_unknown_asset_false(self):
        self.assertFalse(
            should_include_source_in_legacy_rates("unknown_source", "unknown_asset")
        )

    def test_known_source_unknown_asset_false(self):
        """isolated asset 검증 — kb는 통과하지만 새 asset 미등록이면 차단."""
        self.assertFalse(should_include_source_in_legacy_rates("kb", "cny-krw"))

    # ---------------------------------------------------------------
    # Constants exact match — 회귀 보호
    # ---------------------------------------------------------------

    def test_legacy_rate_assets_constant_exact(self):
        """LEGACY_RATE_ASSETS 변경 시 단말 contract도 영향 — 회귀 보호."""
        self.assertEqual(LEGACY_RATE_ASSETS, ("usd-krw", "jpy-krw", "eur-krw"))

    def test_legacy_rate_sources_constant_exact(self):
        """LEGACY_RATE_SOURCES 변경 시 legacy 노출 범위 영향 — 회귀 보호."""
        self.assertEqual(
            LEGACY_RATE_SOURCES,
            (
                "investing",
                "kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi",
            ),
        )

    def test_legacy_rate_sources_matches_bank_display_order_plus_investing(self):
        """은행 list가 BANK_DISPLAY_ORDER와 정합 (운영 표시순과 같은 set, 단말 enum과 동기화)."""
        from app.crud import BANK_DISPLAY_ORDER
        banks_in_sources = [s for s in LEGACY_RATE_SOURCES if s != "investing"]
        self.assertEqual(banks_in_sources, BANK_DISPLAY_ORDER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
