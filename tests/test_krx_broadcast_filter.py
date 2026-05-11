"""Latest mirror exposure policy 단위 테스트.

Pre-Z-2d 이름은 "krx_broadcast_filter"였으나 Z-2d Step 3 통합 후 의미는
**legacy mirror exposure policy = legacy_policy**로 일반화. Z-2d cleanup
(2026-05-12)에서 `KRX_BROADCAST_INCLUDE` env 제거 — 더 이상 토글 관련 patch X.
파일명은 git history 보존 위해 유지 (별도 rename PR 가능).

검증:
    - should_include_source_in_latest는 legacy_policy.should_include_source_in_legacy_rates
      위임 — Cartesian product 양방향 일치 (invariant)
    - KRX/USDT 모두 차단 (legacy allowlist 미포함)
    - bank/investing FX (LEGACY allowlist 통과)는 True 유지
    - _mirror_all_latest source loop: topic-only source 모두 skip
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.latest_rates_cache import (
    _mirror_all_latest,
    should_include_source_in_latest,
)
from app.legacy_policy import (
    LEGACY_RATE_ASSETS,
    LEGACY_RATE_SOURCES,
    should_include_source_in_legacy_rates,
)


# ---------------------------------------------------------------------------
# Helper unit tests — Z-2d allowlist 정책
# ---------------------------------------------------------------------------

class TestShouldIncludeSourceInLatest(unittest.TestCase):
    """Z-2d 정책:
    - legacy_policy allowlist (bank 9 + investing × FX 3) AND.
    - 그 외 모두 False (USDT 거래소, KRX, 미등록 source/asset).
    """

    def test_krx_usd_futures_blocked(self):
        """KRX usd-krw-futures는 allowlist 미포함이라 False."""
        self.assertFalse(should_include_source_in_latest("krx", "usd-krw-futures"))

    def test_krx_any_asset_blocked(self):
        """KRX source 자체가 LEGACY_RATE_SOURCES 미포함이라 asset 무관 False."""
        for asset in ("usd-krw-futures", "usd-krw-options", "kospi200-futures",
                       "usd-krw", "jpy-krw"):
            with self.subTest(asset=asset):
                self.assertFalse(should_include_source_in_latest("krx", asset))

    def test_usdt_exchanges_blocked(self):
        """USDT 거래소 (upbit/bithumb/coinone/korbit/gopax)는 모두 False.

        KRX뿐 아니라 USDT도 Redis latest mirror에서 차단.
        """
        for source in ("upbit", "bithumb", "coinone", "korbit", "gopax"):
            with self.subTest(source=source):
                self.assertFalse(should_include_source_in_latest(source, "usdt-krw"))

    def test_bank_and_investing_fx_passes(self):
        """LEGACY_RATE_SOURCES × LEGACY_RATE_ASSETS Cartesian product 모두 True."""
        for source in LEGACY_RATE_SOURCES:
            for asset in LEGACY_RATE_ASSETS:
                with self.subTest(source=source, asset=asset):
                    self.assertTrue(
                        should_include_source_in_latest(source, asset),
                        f"({source}, {asset}) should pass legacy allowlist"
                    )

    def test_known_source_unknown_asset_blocked(self):
        """KB는 LEGACY 통과 but 미등록 asset(cny-krw 등)은 False."""
        self.assertFalse(should_include_source_in_latest("kb", "cny-krw"))

    def test_unknown_source_blocked(self):
        self.assertFalse(should_include_source_in_latest("unknown_source", "usd-krw"))


# ---------------------------------------------------------------------------
# Invariant — should_include_source_in_latest ≡ should_include_source_in_legacy_rates
# ---------------------------------------------------------------------------

class TestMirrorEqualsLegacyPolicyInvariant(unittest.TestCase):
    """핵심 invariant — Redis mirror policy = legacy policy.

    미래에 누군가 should_include_source_in_latest에 별도 로직 추가하면 즉시
    회귀 감지. KRX_BROADCAST_INCLUDE는 Z-2d cleanup에서 제거됨 — 토글 patch 불요.
    """

    # Allowed combinations (legacy_policy True)
    _ALLOWED_COMBOS = [
        (source, asset)
        for source in LEGACY_RATE_SOURCES
        for asset in LEGACY_RATE_ASSETS
    ]

    # Blocked combinations (topic-only sources + various assets)
    _BLOCKED_COMBOS = [
        # USDT 거래소
        ("upbit", "usdt-krw"),
        ("bithumb", "usdt-krw"),
        ("coinone", "usdt-krw"),
        ("korbit", "usdt-krw"),
        ("gopax", "usdt-krw"),
        # KRX
        ("krx", "usd-krw-futures"),
        ("krx", "usd-krw-options"),  # 미래 가설
        # 이상 조합 (source allow but asset disallow)
        ("kb", "usdt-krw"),
        ("investing", "usdt-krw"),
        ("hana", "usd-krw-futures"),
        # unknown
        ("unknown_source", "usd-krw"),
        ("unknown_source", "unknown_asset"),
        ("kb", "cny-krw"),
    ]

    def test_invariant_allowed_combos(self):
        """allowed 조합: latest == legacy == True."""
        for source, asset in self._ALLOWED_COMBOS:
            with self.subTest(source=source, asset=asset):
                latest = should_include_source_in_latest(source, asset)
                legacy = should_include_source_in_legacy_rates(source, asset)
                self.assertEqual(latest, legacy)
                self.assertTrue(latest)

    def test_invariant_blocked_combos(self):
        """blocked 조합: latest == legacy == False."""
        for source, asset in self._BLOCKED_COMBOS:
            with self.subTest(source=source, asset=asset):
                latest = should_include_source_in_latest(source, asset)
                legacy = should_include_source_in_legacy_rates(source, asset)
                self.assertEqual(latest, legacy)
                self.assertFalse(latest)


# ---------------------------------------------------------------------------
# Mirror loop integration — Z-2d 정책 반영
# ---------------------------------------------------------------------------

class TestMirrorSourceSkippedInvariant(unittest.IsolatedAsyncioTestCase):
    """_mirror_all_latest source loop skip 분기 + invariant 검증.

    Pre-Z-2d: KRX만 skip, USDT는 통과.
    Post-Z-2d: legacy allowlist 적용 — topic-only source(USDT + KRX) 모두 skip.

    invariant:
      - attempted_total = loaded_total + failed (skip 제외)
      - source_skipped는 별도 관찰 카운트 (attempted_total에 미포함)
    """

    async def _run_mirror_with_records(self, source_records):
        """mirror loop를 mock하여 stats 반환."""
        with patch("app.latest_rates_cache.crud") as mock_crud, \
             patch("app.latest_rates_cache._set_latest", new_callable=AsyncMock) as mock_set:
            mock_crud.SUPPORTED_CURRENCY_PAIRS = []  # bank/investing loop skip
            mock_crud.get_all_latest_bank_rates_from_db.return_value = []
            mock_crud.select_a_latest_investing_rate_from_db.return_value = None
            mock_crud.get_source_rates_as_legacy_format.return_value = source_records
            mock_crud.get_latest_dxy_rate.return_value = None
            mock_set.return_value = True

            db_session = MagicMock()
            stats = await _mirror_all_latest(db_session)
            return stats, mock_set

    async def test_all_topic_only_sources_skipped(self):
        """USDT + KRX 모두 skip — Z-2d allowlist 적용 결과."""
        records = [
            {"bank": "upbit", "currency": "usdt-krw", "rate": 1485.0,
             "timestamp": "2026-05-12T20:00:00+09:00"},
            {"bank": "krx", "currency": "usd-krw-futures", "rate": 1468.5,
             "timestamp": "2026-05-12T20:00:00+09:00"},
            {"bank": "bithumb", "currency": "usdt-krw", "rate": 1486.0,
             "timestamp": "2026-05-12T20:00:00+09:00"},
        ]
        stats, mock_set = await self._run_mirror_with_records(records)

        # 3 records 모두 topic-only → 모두 skip
        self.assertEqual(stats["source_skipped"], 3)
        self.assertEqual(stats["attempted_total"], 0)
        self.assertEqual(stats["loaded_total"], 0)
        self.assertEqual(stats["source"], 0)
        self.assertEqual(stats["failed"], 0)
        # invariant: attempted = loaded + failed
        self.assertEqual(stats["attempted_total"], stats["loaded_total"] + stats["failed"])
        # 모든 topic-only key는 SET 호출 X
        called_keys = [call_args[0][0] for call_args in mock_set.call_args_list]
        self.assertNotIn("latest:source:krx:usd-krw-futures", called_keys)
        self.assertNotIn("latest:source:upbit:usdt-krw", called_keys)
        self.assertNotIn("latest:source:bithumb:usdt-krw", called_keys)

    async def test_empty_records_no_skip(self):
        """records 빈 list — skip 카운트도 0."""
        stats, _ = await self._run_mirror_with_records([])
        self.assertEqual(stats["source_skipped"], 0)
        self.assertEqual(stats["attempted_total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
