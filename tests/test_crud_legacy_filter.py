"""crud._filter_source_entries_by_legacy_policy 단위 테스트 (PR Z-2d Step 2).

검증:
    - source_rates → legacy shape entries에서 topic-only source 제거
    - USDT 거래소 entries 모두 제외
    - KRX usd-krw-futures entry 제외
    - 빈 list 입력 → 빈 list 반환
    - allowed source entries(미래 시나리오)는 유지
    - get_source_rates_as_legacy_format 호출자 체인의 의미 — 현재 source_rates 데이터
      (USDT/KRX만)에서 항상 빈 list 반환 (legacy 노출 차단)
"""
from __future__ import annotations

import unittest

from app.crud import _filter_source_entries_by_legacy_policy


def _e(bank: str, currency: str, rate: float = 1000.0,
       ts: str = "2026-05-12T15:00:00+09:00") -> dict:
    """source_rates → legacy adapter 출력 shape helper."""
    return {"bank": bank, "currency": currency, "rate": rate, "timestamp": ts}


class TestFilterSourceEntriesByLegacyPolicy(unittest.TestCase):

    # ---------------------------------------------------------------
    # 현재 운영 시나리오 — source_rates에 USDT/KRX만 존재
    # ---------------------------------------------------------------

    def test_all_usdt_exchanges_filtered_out(self):
        """USDT 5거래소 entries 모두 제외 (legacy 노출 차단)."""
        entries = [
            _e("upbit", "usdt-krw", 1485.0),
            _e("bithumb", "usdt-krw", 1486.0),
            _e("coinone", "usdt-krw", 1487.0),
            _e("korbit", "usdt-krw", 1488.0),
            _e("gopax", "usdt-krw", 1489.0),
        ]
        result = _filter_source_entries_by_legacy_policy(entries)
        self.assertEqual(result, [])

    def test_krx_futures_filtered_out(self):
        """KRX usd-krw-futures entry 제외 (legacy 노출 차단)."""
        entries = [_e("krx", "usd-krw-futures", 1371.5)]
        result = _filter_source_entries_by_legacy_policy(entries)
        self.assertEqual(result, [])

    def test_mixed_usdt_and_krx_all_filtered(self):
        """USDT + KRX 혼합 입력도 모두 제외 — 현재 source_rates 상태."""
        entries = [
            _e("upbit", "usdt-krw", 1485.0),
            _e("krx", "usd-krw-futures", 1371.5),
            _e("bithumb", "usdt-krw", 1486.0),
        ]
        result = _filter_source_entries_by_legacy_policy(entries)
        self.assertEqual(result, [])

    # ---------------------------------------------------------------
    # 빈 입력
    # ---------------------------------------------------------------

    def test_empty_input_returns_empty(self):
        self.assertEqual(_filter_source_entries_by_legacy_policy([]), [])

    # ---------------------------------------------------------------
    # 미래 시나리오 — LEGACY_RATE_SOURCES 통과 source가 source_rates에 들어가는 경우
    # ---------------------------------------------------------------

    def test_allowed_source_passes_through(self):
        """미래 시나리오: 어떤 이유로 (kb, usd-krw)가 source_rates에 들어오면
        policy 통과해서 결과에 포함됨. 중복 위험은 docstring에 명시 — 호출자(예:
        get_all_rates_flat)에서 bank_exchange_rates와의 dedup 검토 필요.
        """
        entries = [
            _e("kb", "usd-krw", 1370.0),
            _e("upbit", "usdt-krw", 1485.0),
        ]
        result = _filter_source_entries_by_legacy_policy(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bank"], "kb")
        self.assertEqual(result[0]["currency"], "usd-krw")

    def test_allowed_investing_passes_through(self):
        """미래 시나리오: (investing, usd-krw) source_rates 진입 — policy 통과."""
        entries = [
            _e("investing", "usd-krw", 1371.0),
            _e("krx", "usd-krw-futures", 1372.0),
        ]
        result = _filter_source_entries_by_legacy_policy(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bank"], "investing")

    def test_investing_usdt_combination_filtered(self):
        """이상 조합 (investing, usdt-krw): source allow지만 asset disallow → 제외."""
        entries = [_e("investing", "usdt-krw", 1485.0)]
        result = _filter_source_entries_by_legacy_policy(entries)
        self.assertEqual(result, [])

    # ---------------------------------------------------------------
    # 입력 순서 보존
    # ---------------------------------------------------------------

    def test_preserves_input_order(self):
        """filter는 정렬 X — 순서 보존. 정렬은 호출자(get_source_rates_as_legacy_format)
        가 별도 sort_key로 수행."""
        entries = [
            _e("kb", "usd-krw"),       # allowed
            _e("upbit", "usdt-krw"),   # filtered
            _e("hana", "jpy-krw"),     # allowed
            _e("krx", "usd-krw-futures"),  # filtered
            _e("investing", "eur-krw"),  # allowed
        ]
        result = _filter_source_entries_by_legacy_policy(entries)
        banks = [e["bank"] for e in result]
        self.assertEqual(banks, ["kb", "hana", "investing"])

    # ---------------------------------------------------------------
    # 동일 source 다중 asset
    # ---------------------------------------------------------------

    def test_source_with_multiple_assets_filtered_per_asset(self):
        """같은 source라도 asset에 따라 통과/제외 — 두 set AND 조건."""
        entries = [
            _e("kb", "usd-krw"),       # allowed
            _e("kb", "usdt-krw"),      # filtered (asset disallow)
            _e("kb", "cny-krw"),       # filtered (asset disallow — 미등록)
        ]
        result = _filter_source_entries_by_legacy_policy(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["currency"], "usd-krw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
