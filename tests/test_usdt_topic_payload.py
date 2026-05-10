"""테더 탭 topic payload builder 단위 테스트 (PR Z-2b Stage 3 1차).

검증 대상:
    - build_tether_tab_payload (순수 함수, DB 의존 X)
    - schema (type/version/data 4 그룹)
    - 정렬 (SourceRegistry sort_order / BANK_DISPLAY_ORDER fallback)
    - legacy shape 정규화 (bank → source, currency → asset)
    - singleton 그룹의 expected source/asset 검증
    - None optional 키 누락
    - topic 필드 부재 (topic-agnostic 원칙)
"""
from __future__ import annotations

import unittest

from app.usdt_topic_payload import build_tether_tab_payload


def _u(source: str, rate: float, ts: str = "2026-05-10T15:00:00+09:00") -> dict:
    """USDT 입력 helper (topic-native shape)."""
    return {"source": source, "asset": "usdt-krw", "rate": rate, "timestamp": ts}


def _bank_legacy(bank: str, rate: float, ts: str = "2026-05-10T15:00:00+09:00") -> dict:
    """은행 입력 helper (legacy shape — crud.select_latest_bank_rates_from_db 반환과 일치)."""
    return {"bank": bank, "currency": "usd-krw", "rate": rate, "timestamp": ts}


def _investing_legacy(rate: float, ts: str = "2026-05-10T15:00:00+09:00") -> dict:
    """Investing 입력 helper (legacy shape — crud.select_a_latest_investing_rate_from_db 반환과 일치)."""
    return {"bank": "investing", "currency": "usd-krw", "rate": rate, "timestamp": ts}


def _krx(rate: float, ts: str = "2026-05-10T15:00:00+09:00") -> dict:
    """KRX 입력 helper (topic-native shape)."""
    return {"source": "krx", "asset": "usd-krw-futures", "rate": rate, "timestamp": ts}


# ---------------------------------------------------------------------------
# Schema / 최상위 구조
# ---------------------------------------------------------------------------

class TestPayloadSchema(unittest.TestCase):

    def test_minimal_payload_has_type_version_data_only(self):
        """최소 입력 — empty list 2개. 최상위 키는 정확히 type/version/data만."""
        payload = build_tether_tab_payload(usdt_rates=[], bank_rates=[])
        self.assertEqual(set(payload.keys()), {"type", "version", "data"})

    def test_type_is_snapshot(self):
        payload = build_tether_tab_payload(usdt_rates=[], bank_rates=[])
        self.assertEqual(payload["type"], "snapshot")

    def test_version_is_1(self):
        payload = build_tether_tab_payload(usdt_rates=[], bank_rates=[])
        self.assertEqual(payload["version"], 1)

    def test_topic_field_absent(self):
        """topic-agnostic — 'topic' 필드는 builder가 만들지 않는다."""
        payload = build_tether_tab_payload(usdt_rates=[], bank_rates=[])
        self.assertNotIn("topic", payload)

    def test_data_required_keys_always_present(self):
        """usdt_krw, usd_krw_banks는 빈 list라도 항상 존재."""
        payload = build_tether_tab_payload(usdt_rates=[], bank_rates=[])
        self.assertIn("usdt_krw", payload["data"])
        self.assertIn("usd_krw_banks", payload["data"])
        self.assertEqual(payload["data"]["usdt_krw"], [])
        self.assertEqual(payload["data"]["usd_krw_banks"], [])

    def test_optional_keys_absent_when_none(self):
        """investing/krx None → 키 누락."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            investing_rate=None,
            krx_futures_rate=None,
        )
        self.assertNotIn("usd_krw_reference", payload["data"])
        self.assertNotIn("usd_krw_futures", payload["data"])


# ---------------------------------------------------------------------------
# 정상 case (9 항목 모두 존재)
# ---------------------------------------------------------------------------

class TestFullSnapshot(unittest.TestCase):

    def test_full_9_items_correct_groups(self):
        payload = build_tether_tab_payload(
            usdt_rates=[
                _u("upbit", 1485.0),
                _u("bithumb", 1486.5),
                _u("coinone", 1485.5),
                _u("korbit", 1484.0),
                _u("gopax", 1486.0),
            ],
            bank_rates=[
                _bank_legacy("kb", 1380.0),
                _bank_legacy("hana", 1380.5),
            ],
            investing_rate=_investing_legacy(1380.2),
            krx_futures_rate=_krx(1382.0),
        )
        self.assertEqual(len(payload["data"]["usdt_krw"]), 5)
        self.assertEqual(len(payload["data"]["usd_krw_banks"]), 2)
        self.assertIn("usd_krw_reference", payload["data"])
        self.assertIn("usd_krw_futures", payload["data"])


# ---------------------------------------------------------------------------
# 정렬 (SourceRegistry sort_order)
# ---------------------------------------------------------------------------

class TestSorting(unittest.TestCase):

    def test_usdt_sorted_by_source_registry(self):
        """입력 무작위 순서 → SourceRegistry sort_order로 재정렬.
        SourceRegistry: upbit(50) → bithumb(60) → coinone(70) → korbit(80) → gopax(90).
        """
        payload = build_tether_tab_payload(
            usdt_rates=[
                _u("gopax", 1.0),
                _u("upbit", 2.0),
                _u("korbit", 3.0),
                _u("bithumb", 4.0),
                _u("coinone", 5.0),
            ],
            bank_rates=[],
        )
        sources = [e["source"] for e in payload["data"]["usdt_krw"]]
        self.assertEqual(sources, ["upbit", "bithumb", "coinone", "korbit", "gopax"])

    def test_banks_sorted_kb_then_hana(self):
        """SourceRegistry sort_order: kb(20) → hana(30)."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[
                _bank_legacy("hana", 1.0),
                _bank_legacy("kb", 2.0),
            ],
        )
        banks = [e["source"] for e in payload["data"]["usd_krw_banks"]]
        self.assertEqual(banks, ["kb", "hana"])

    def test_unknown_usdt_source_goes_last(self):
        """SourceRegistry 미등록 source는 그룹 2로 (registered 뒤로) + source 코드순."""
        payload = build_tether_tab_payload(
            usdt_rates=[
                _u("zzbit", 1.0),
                _u("upbit", 2.0),
                _u("aabit", 3.0),
            ],
            bank_rates=[],
        )
        sources = [e["source"] for e in payload["data"]["usdt_krw"]]
        # upbit (registered) → 미등록 alphabet (aabit, zzbit)
        self.assertEqual(sources, ["upbit", "aabit", "zzbit"])

    def test_unknown_bank_uses_bank_display_order_fallback(self):
        """은행이 SourceRegistry 미등록이지만 BANK_DISPLAY_ORDER에 등록된 경우 fallback.
        SourceRegistry는 kb, hana만. shinhan은 BANK_DISPLAY_ORDER에 있음.
        kb(SourceRegistry 그룹0) → hana(SourceRegistry 그룹0) → shinhan(BANK_DISPLAY_ORDER 그룹1).
        """
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[
                _bank_legacy("shinhan", 1.0),
                _bank_legacy("hana", 2.0),
                _bank_legacy("kb", 3.0),
            ],
        )
        banks = [e["source"] for e in payload["data"]["usd_krw_banks"]]
        self.assertEqual(banks, ["kb", "hana", "shinhan"])


# ---------------------------------------------------------------------------
# Legacy shape 정규화
# ---------------------------------------------------------------------------

class TestLegacyNormalization(unittest.TestCase):

    def test_bank_legacy_shape_to_topic_native(self):
        """legacy `bank/currency` → topic-native `source/asset`."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[_bank_legacy("kb", 1380.0)],
        )
        entry = payload["data"]["usd_krw_banks"][0]
        # 출력은 topic-native 키만
        self.assertIn("source", entry)
        self.assertIn("asset", entry)
        self.assertNotIn("bank", entry)
        self.assertNotIn("currency", entry)
        self.assertEqual(entry["source"], "kb")
        self.assertEqual(entry["asset"], "usd-krw")

    def test_investing_legacy_shape_to_topic_native(self):
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            investing_rate=_investing_legacy(1380.2),
        )
        entry = payload["data"]["usd_krw_reference"]
        self.assertEqual(entry["source"], "investing")
        self.assertEqual(entry["asset"], "usd-krw")
        self.assertNotIn("bank", entry)
        self.assertNotIn("currency", entry)

    def test_display_name_added_from_source_registry(self):
        """SourceRegistry display_name이 항목에 자동 첨부."""
        payload = build_tether_tab_payload(
            usdt_rates=[_u("upbit", 1485.0)],
            bank_rates=[_bank_legacy("kb", 1380.0)],
            investing_rate=_investing_legacy(1380.2),
            krx_futures_rate=_krx(1382.0),
        )
        self.assertEqual(payload["data"]["usdt_krw"][0]["display_name"], "업비트")
        self.assertEqual(payload["data"]["usd_krw_banks"][0]["display_name"], "국민은행")
        self.assertEqual(payload["data"]["usd_krw_reference"]["display_name"], "인베스팅")
        self.assertEqual(payload["data"]["usd_krw_futures"]["display_name"], "미국달러F")

    def test_unknown_source_display_name_falls_back_to_source(self):
        """SourceRegistry 미등록 source — display_name은 source 그대로."""
        payload = build_tether_tab_payload(
            usdt_rates=[_u("newcoin", 1500.0)],
            bank_rates=[],
        )
        self.assertEqual(payload["data"]["usdt_krw"][0]["display_name"], "newcoin")


# ---------------------------------------------------------------------------
# Singleton expected 검증 (코덱스 BLOCKING)
# ---------------------------------------------------------------------------

class TestSingletonExpected(unittest.TestCase):
    """singleton 슬롯은 schema 의미 고정이라 expected source/asset만 허용."""

    def test_reference_with_unexpected_source_drops_key(self):
        """investing 슬롯에 random source 들어오면 → 키 누락."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            investing_rate={"source": "random_xyz", "asset": "usd-krw",
                            "rate": 1.0, "timestamp": "2026-05-10T15:00:00+09:00"},
        )
        self.assertNotIn("usd_krw_reference", payload["data"])

    def test_reference_with_unexpected_asset_drops_key(self):
        """investing source인데 asset이 다르면 → 키 누락."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            investing_rate={"source": "investing", "asset": "jpy-krw",
                            "rate": 1.0, "timestamp": "2026-05-10T15:00:00+09:00"},
        )
        self.assertNotIn("usd_krw_reference", payload["data"])

    def test_futures_with_unexpected_source_drops_key(self):
        """KRX 슬롯에 random source 들어오면 → 키 누락."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            krx_futures_rate={"source": "random_xyz", "asset": "usd-krw-futures",
                              "rate": 1.0, "timestamp": "2026-05-10T15:00:00+09:00"},
        )
        self.assertNotIn("usd_krw_futures", payload["data"])

    def test_futures_with_unexpected_asset_drops_key(self):
        """krx source인데 asset이 다르면 → 키 누락."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            krx_futures_rate={"source": "krx", "asset": "eur-krw-futures",
                              "rate": 1.0, "timestamp": "2026-05-10T15:00:00+09:00"},
        )
        self.assertNotIn("usd_krw_futures", payload["data"])

    def test_reference_minimal_input_uses_fallback_source_asset(self):
        """singleton 입력에 source/asset 누락 — fallback (investing/usd-krw) 자동 적용."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            investing_rate={"rate": 1380.0, "timestamp": "2026-05-10T15:00:00+09:00"},
        )
        self.assertIn("usd_krw_reference", payload["data"])
        self.assertEqual(payload["data"]["usd_krw_reference"]["source"], "investing")


# ---------------------------------------------------------------------------
# 부분 데이터 (일부 거래소/은행 누락)
# ---------------------------------------------------------------------------

class TestPartialData(unittest.TestCase):

    def test_usdt_subset_only(self):
        """5거래소 중 3개만 있는 경우 — 그대로 3개 entry."""
        payload = build_tether_tab_payload(
            usdt_rates=[_u("upbit", 1.0), _u("bithumb", 2.0), _u("coinone", 3.0)],
            bank_rates=[],
        )
        self.assertEqual(len(payload["data"]["usdt_krw"]), 3)

    def test_only_kb_in_banks(self):
        """은행 1개만 — 그대로 1개 entry."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[_bank_legacy("kb", 1380.0)],
        )
        self.assertEqual(len(payload["data"]["usd_krw_banks"]), 1)
        self.assertEqual(payload["data"]["usd_krw_banks"][0]["source"], "kb")

    def test_entry_with_missing_rate_is_dropped(self):
        """rate 부재 entry는 무시 (drop)."""
        payload = build_tether_tab_payload(
            usdt_rates=[
                _u("upbit", 1485.0),
                {"source": "bithumb", "asset": "usdt-krw", "timestamp": "..."},  # rate 부재
            ],
            bank_rates=[],
        )
        sources = [e["source"] for e in payload["data"]["usdt_krw"]]
        self.assertEqual(sources, ["upbit"])  # bithumb drop


if __name__ == "__main__":
    unittest.main(verbosity=2)
