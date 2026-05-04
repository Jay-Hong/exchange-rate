"""PR6b-2a — KRX_BROADCAST_INCLUDE 토글 + mirror skip 단위 테스트.

검증 대상:
- should_include_source_in_latest helper (asset 단위 정확한 scope)
- _mirror_all_latest의 source loop skip 분기 + invariant
  (source_skipped는 attempted_total에 포함 X)

운영 미연결 — DB/Redis 호출은 mock.

실행:
    python -m unittest tests.test_krx_broadcast_filter -v
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config
from app.latest_rates_cache import (
    _mirror_all_latest,
    should_include_source_in_latest,
)


# ---------------------------------------------------------------------------
# helper unit tests — asset 단위 정확한 scope
# ---------------------------------------------------------------------------

class TestShouldIncludeSourceInLatest(unittest.TestCase):

    def test_krx_usd_futures_skipped_when_toggle_false(self):
        """KRX_BROADCAST_INCLUDE=false → krx/usd-krw-futures skip."""
        with patch.object(config, "KRX_BROADCAST_INCLUDE", False):
            self.assertFalse(should_include_source_in_latest("krx", "usd-krw-futures"))

    def test_krx_usd_futures_included_when_toggle_true(self):
        """KRX_BROADCAST_INCLUDE=true → krx/usd-krw-futures 포함."""
        with patch.object(config, "KRX_BROADCAST_INCLUDE", True):
            self.assertTrue(should_include_source_in_latest("krx", "usd-krw-futures"))

    def test_other_source_unaffected_by_toggle(self):
        """다른 source는 토글 무관. KRX_BROADCAST_INCLUDE=false여도 정상 mirror."""
        with patch.object(config, "KRX_BROADCAST_INCLUDE", False):
            for source, asset in [
                ("upbit", "usdt-krw"),
                ("bithumb", "usdt-krw"),
                ("coinone", "usdt-krw"),
                ("kb", "usd-krw"),
                ("hana", "jpy-krw"),
                ("investing", "usd-krw"),
            ]:
                self.assertTrue(
                    should_include_source_in_latest(source, asset),
                    f"{source}/{asset} should not be affected by KRX toggle",
                )

    def test_other_krx_asset_unaffected_by_toggle(self):
        """KRX 다른 asset (가상)은 토글 무관. scope는 usd-krw-futures만."""
        with patch.object(config, "KRX_BROADCAST_INCLUDE", False):
            # 향후 추가 가능한 KRX asset (예: usd-krw-options, krx 다른 derivative)
            self.assertTrue(
                should_include_source_in_latest("krx", "usd-krw-options"),
                "다른 KRX asset은 별도 토글 필요 (현재 helper scope는 futures만)",
            )
            self.assertTrue(
                should_include_source_in_latest("krx", "kospi200-futures"),
            )


# ---------------------------------------------------------------------------
# mirror loop integration — source_skipped invariant
# ---------------------------------------------------------------------------

class TestMirrorSourceSkippedInvariant(unittest.IsolatedAsyncioTestCase):
    """_mirror_all_latest의 source loop skip 분기 + invariant 검증.

    invariant:
      - attempted_total = loaded_total + failed (skip 제외)
      - source_skipped는 별도 관찰 카운트 (attempted_total에 미포함)
    """

    async def _run_mirror_with_records(
        self,
        source_records,
        krx_toggle: bool,
    ):
        """mirror loop를 mock하여 stats 반환.

        crud / Redis / 다른 source path는 모두 mock — source loop만 격리 검증.
        """
        # crud.SUPPORTED_CURRENCY_PAIRS / select_a_latest_investing_rate_from_db /
        # get_all_latest_bank_rates_from_db / get_source_rates_as_legacy_format /
        # get_latest_dxy_rate / _set_latest 모두 mock
        with patch.object(config, "KRX_BROADCAST_INCLUDE", krx_toggle), \
             patch("app.latest_rates_cache.crud") as mock_crud, \
             patch("app.latest_rates_cache._set_latest", new_callable=AsyncMock) as mock_set:
            mock_crud.SUPPORTED_CURRENCY_PAIRS = []  # bank/investing loop skip
            mock_crud.get_all_latest_bank_rates_from_db.return_value = []
            mock_crud.select_a_latest_investing_rate_from_db.return_value = None
            mock_crud.get_source_rates_as_legacy_format.return_value = source_records
            mock_crud.get_latest_dxy_rate.return_value = None
            mock_set.return_value = True  # 모든 SET 성공

            db_session = MagicMock()
            stats = await _mirror_all_latest(db_session)
            return stats, mock_set

    async def test_krx_skipped_when_toggle_false(self):
        """KRX_BROADCAST_INCLUDE=false: KRX record는 skip, attempted에 미포함.

        다른 source는 정상 mirror.
        """
        records = [
            {"bank": "upbit", "currency": "usdt-krw", "rate": 1485.0, "timestamp": "2026-05-04T20:00:00+09:00"},
            {"bank": "krx", "currency": "usd-krw-futures", "rate": 1468.5, "timestamp": "2026-05-04T20:00:00+09:00"},
            {"bank": "bithumb", "currency": "usdt-krw", "rate": 1486.0, "timestamp": "2026-05-04T20:00:00+09:00"},
        ]
        stats, mock_set = await self._run_mirror_with_records(records, krx_toggle=False)

        self.assertEqual(stats["source_skipped"], 1)
        self.assertEqual(stats["attempted_total"], 2)  # KRX 제외 — skip은 attempted에 포함 X
        self.assertEqual(stats["loaded_total"], 2)
        self.assertEqual(stats["source"], 2)
        self.assertEqual(stats["failed"], 0)
        # invariant: attempted = loaded + failed
        self.assertEqual(stats["attempted_total"], stats["loaded_total"] + stats["failed"])
        # KRX key는 SET 호출 X
        called_keys = [call_args[0][0] for call_args in mock_set.call_args_list]
        self.assertNotIn("latest:source:krx:usd-krw-futures", called_keys)

    async def test_krx_included_when_toggle_true(self):
        """KRX_BROADCAST_INCLUDE=true: KRX record도 정상 mirror."""
        records = [
            {"bank": "upbit", "currency": "usdt-krw", "rate": 1485.0, "timestamp": "2026-05-04T20:00:00+09:00"},
            {"bank": "krx", "currency": "usd-krw-futures", "rate": 1468.5, "timestamp": "2026-05-04T20:00:00+09:00"},
        ]
        stats, mock_set = await self._run_mirror_with_records(records, krx_toggle=True)

        self.assertEqual(stats["source_skipped"], 0)
        self.assertEqual(stats["attempted_total"], 2)
        self.assertEqual(stats["loaded_total"], 2)
        self.assertEqual(stats["source"], 2)
        self.assertEqual(stats["failed"], 0)
        # KRX key SET 호출됨
        called_keys = [call_args[0][0] for call_args in mock_set.call_args_list]
        self.assertIn("latest:source:krx:usd-krw-futures", called_keys)

    async def test_only_other_source_unaffected(self):
        """KRX record 자체 없을 때: source_skipped=0, 토글 무관."""
        records = [
            {"bank": "upbit", "currency": "usdt-krw", "rate": 1485.0, "timestamp": "2026-05-04T20:00:00+09:00"},
            {"bank": "bithumb", "currency": "usdt-krw", "rate": 1486.0, "timestamp": "2026-05-04T20:00:00+09:00"},
        ]
        for toggle in (True, False):
            with self.subTest(toggle=toggle):
                stats, _ = await self._run_mirror_with_records(records, krx_toggle=toggle)
                self.assertEqual(stats["source_skipped"], 0)
                self.assertEqual(stats["attempted_total"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
