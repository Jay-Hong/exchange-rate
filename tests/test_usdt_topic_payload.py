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
from unittest.mock import MagicMock, patch

from app import usdt_topic_payload as utp
from app.source_registry import SourceDefinition
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
        """investing None → 키 누락."""
        payload = build_tether_tab_payload(
            usdt_rates=[],
            bank_rates=[],
            investing_rate=None,
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
        )
        self.assertEqual(len(payload["data"]["usdt_krw"]), 5)
        self.assertEqual(len(payload["data"]["usd_krw_banks"]), 2)
        self.assertIn("usd_krw_reference", payload["data"])
        # ADR-038 D2 — usdt:krw payload에 usd_krw_futures 없음 (KRX는 독립 topic)
        self.assertNotIn("usd_krw_futures", payload["data"])


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

    def test_entry_has_no_display_name_field(self):
        """display_name 제거 (2026-05-11) — payload entry는 display_name 미포함.

        단말은 자체 registry로 source→display_name lookup. 서버는 (source, asset)
        만 식별자로 전송. 회귀 방지 negative 검증.
        """
        payload = build_tether_tab_payload(
            usdt_rates=[_u("upbit", 1485.0)],
            bank_rates=[_bank_legacy("kb", 1380.0)],
            investing_rate=_investing_legacy(1380.2),
        )
        # 모든 entry 그룹에서 display_name 부재 확인 (futures group은 ADR-038 D2로 제거)
        self.assertNotIn("display_name", payload["data"]["usdt_krw"][0])
        self.assertNotIn("display_name", payload["data"]["usd_krw_banks"][0])
        self.assertNotIn("display_name", payload["data"]["usd_krw_reference"])

    def test_entry_keys_exactly_match_contract(self):
        """payload entry key set은 정확히 {source, asset, rate, timestamp}."""
        payload = build_tether_tab_payload(
            usdt_rates=[_u("upbit", 1485.0)],
            bank_rates=[_bank_legacy("kb", 1380.0)],
            investing_rate=_investing_legacy(1380.2),
        )
        expected_keys = {"source", "asset", "rate", "timestamp"}
        self.assertEqual(set(payload["data"]["usdt_krw"][0].keys()), expected_keys)
        self.assertEqual(set(payload["data"]["usd_krw_banks"][0].keys()), expected_keys)
        self.assertEqual(set(payload["data"]["usd_krw_reference"].keys()), expected_keys)

    def test_unknown_source_keeps_source_field_without_display_name(self):
        """SourceRegistry 미등록 source도 display_name 없이 source/asset만 유지."""
        payload = build_tether_tab_payload(
            usdt_rates=[_u("newcoin", 1500.0)],
            bank_rates=[],
        )
        entry = payload["data"]["usdt_krw"][0]
        self.assertEqual(entry["source"], "newcoin")
        self.assertEqual(entry["asset"], "usdt-krw")
        self.assertNotIn("display_name", entry)


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

    def test_futures_group_never_present(self):
        """ADR-038 D2 — builder는 usd_krw_futures 키를 만들지 않음 (KRX 독립 topic 회귀 잠금).

        KRX가 usdt_rates 목록에 섞여 들어와도 usdt_krw group은 exchange asset(usdt-krw)만
        수용하므로 futures group이 생기지 않는다."""
        payload = build_tether_tab_payload(
            usdt_rates=[{"source": "krx", "asset": "usd-krw-futures",
                         "rate": 1382.0, "timestamp": "2026-05-10T15:00:00+09:00"}],
            bank_rates=[],
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


# ---------------------------------------------------------------------------
# load_and_build_tether_tab_payload — DB 통합 helper (mock-based)
# ---------------------------------------------------------------------------


def _usdt_def(source: str, sort_order: int) -> SourceDefinition:
    """USDT 거래소 SourceDefinition mock helper."""
    return SourceDefinition(
        source=source,
        asset="usdt-krw",
        display_name=source,
        category="exchange",
        sort_order=sort_order,
    )


_FIVE_USDT_DEFS = [
    _usdt_def("upbit", 50),
    _usdt_def("bithumb", 60),
    _usdt_def("coinone", 70),
    _usdt_def("korbit", 80),
    _usdt_def("gopax", 90),
]


def _source_rate(source: str, asset: str, rate: float, ts: str = "2026-05-10T15:00:00+09:00") -> dict:
    """legacy bank/currency 키 shape (source_rate DB row 변환 전 형태)."""
    return {"currency": asset, "bank": source, "rate": rate, "timestamp": ts}


def _bank_row(bank: str, rate: float, ts: str = "2026-05-10T15:00:00+09:00") -> dict:
    """select_latest_bank_rates_from_db 반환 shape."""
    return {"currency": "usd-krw", "bank": bank, "rate": rate, "timestamp": ts}


def _investing_row(rate: float, ts: str = "2026-05-10T15:00:00+09:00") -> dict:
    """select_a_latest_investing_rate_from_db 반환 shape."""
    return {"currency": "usd-krw", "bank": "investing", "rate": rate, "timestamp": ts}


class TestLoadAndBuildTetherTabPayload(unittest.TestCase):
    """DB 통합 helper — crud 함수 mock으로 dispatch / 정렬 검증 (KRX 없음, ADR-038 D2)."""

    def _patches(
        self,
        *,
        usdt_rates_per_source: dict,
        bank_rates: list,
        investing: dict | None,
    ):
        """공통 mock setup — context manager list 반환.

        PR Z-2e B-Step 2 후: USDT 5거래소는 Redis-first (get_latest_usdt_rate_from_sync_job)
        → 1개라도 None이면 전체 DB fallback (get_latest_source_rates_for_topic).
        usdt_rates_per_source(legacy shape dict)를 두 mock 모두에 자동 변환 dispatch.
        KRX는 ADR-038 D2로 이 helper에서 제거 (krx:usd-krw-futures 독립 topic —
        tests/test_krx_redis_integration.py TestKrxTopicEntryRedisFirst가 커버).
        """

        def _to_topic_native(raw: dict | None, source: str, asset: str) -> dict | None:
            if raw is None:
                return None
            return {
                "source": raw.get("bank") or source,
                "asset": raw.get("currency") or asset,
                "rate": raw["rate"],
                "timestamp": raw["timestamp"],
            }

        def fake_redis_read(source, asset):
            # Z-2e B-Step 2: USDT Redis-first read
            return _to_topic_native(usdt_rates_per_source.get(source), source, asset)

        def fake_db_fallback(db, asset, sources):
            # Z-2e B-Step 2: Redis 1개라도 miss 시 호출되는 topic 전용 DB fallback
            return [
                _to_topic_native(usdt_rates_per_source[s], s, asset)
                for s in sources
                if usdt_rates_per_source.get(s) is not None
            ]

        return [
            patch.object(utp, "get_usdt_exchange_entries", return_value=_FIVE_USDT_DEFS),
            patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                         side_effect=fake_redis_read),
            patch.object(utp, "get_latest_source_rates_for_topic",
                         side_effect=fake_db_fallback),
            patch.object(utp, "select_latest_bank_rates_from_db", return_value=bank_rates),
            patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=investing),
            # PR Z-2e Step 3a: bank/investing Redis-first helper도 mock —
            # 기존 테스트는 명시적으로 DB fallback 경로를 쓰게 격리 (실제 redis:6379
            # 접속 시도로 인한 비결정적 동작 회피, Codex 권고).
            patch.object(utp, "get_latest_bank_rate_from_sync_job", return_value=None),
            patch.object(utp, "get_latest_investing_rate_from_sync_job", return_value=None),
        ]

    def _run_with(self, **kwargs):
        """patches 적용 + helper 호출."""
        ps = self._patches(**kwargs)
        for p in ps:
            p.start()
        try:
            return utp.load_and_build_tether_tab_payload(MagicMock())
        finally:
            for p in ps:
                p.stop()

    # 1) 정상 (모든 데이터) → futures 키 부재 (ADR-038 D2 회귀 잠금)
    def test_full_data_has_no_futures_key(self):
        payload = self._run_with(
            usdt_rates_per_source={
                "upbit":   _source_rate("upbit", "usdt-krw", 1485.0),
                "bithumb": _source_rate("bithumb", "usdt-krw", 1486.5),
                "coinone": _source_rate("coinone", "usdt-krw", 1485.5),
                "korbit":  _source_rate("korbit", "usdt-krw", 1484.0),
                "gopax":   _source_rate("gopax", "usdt-krw", 1486.0),
            },
            bank_rates=[_bank_row("kb", 1380.0), _bank_row("hana", 1380.5)],
            investing=_investing_row(1380.2),
        )
        self.assertEqual(len(payload["data"]["usdt_krw"]), 5)
        self.assertEqual(len(payload["data"]["usd_krw_banks"]), 2)
        self.assertIn("usd_krw_reference", payload["data"])
        self.assertNotIn("usd_krw_futures", payload["data"])

    # 3) USDT 일부 None (gopax 누락) → 4 entry
    def test_usdt_partial_missing(self):
        payload = self._run_with(
            usdt_rates_per_source={
                "upbit":   _source_rate("upbit", "usdt-krw", 1.0),
                "bithumb": _source_rate("bithumb", "usdt-krw", 2.0),
                "coinone": _source_rate("coinone", "usdt-krw", 3.0),
                "korbit":  _source_rate("korbit", "usdt-krw", 4.0),
                # gopax missing
            },
            bank_rates=[],
            investing=None,
        )
        sources = [e["source"] for e in payload["data"]["usdt_krw"]]
        self.assertEqual(sources, ["upbit", "bithumb", "coinone", "korbit"])

    # 4) 은행 일부 None (kb 누락) → hana만
    def test_bank_partial_missing(self):
        payload = self._run_with(
            usdt_rates_per_source={},
            bank_rates=[_bank_row("hana", 1380.0)],  # kb missing
            investing=None,
        )
        banks = [e["source"] for e in payload["data"]["usd_krw_banks"]]
        self.assertEqual(banks, ["hana"])

    # 5) Investing None → 키 누락
    def test_investing_none_drops_reference_key(self):
        payload = self._run_with(
            usdt_rates_per_source={},
            bank_rates=[],
            investing=None,
        )
        self.assertNotIn("usd_krw_reference", payload["data"])

    # 7) ADR-038 D2 구조 회귀: KRX query path가 이 모듈에서 완전 소멸
    #    (include_krx 파라미터 + get_latest_source_rate import + krx Redis helper 모두 제거 —
    #     krx:usd-krw-futures 독립 topic은 app/krx_topic_publisher.load_krx_topic_entry 담당)
    def test_krx_query_path_removed_from_module(self):
        import inspect
        self.assertFalse(hasattr(utp, "get_latest_source_rate"))
        self.assertFalse(hasattr(utp, "get_latest_krx_rate_from_sync_job"))
        sig = inspect.signature(utp.load_and_build_tether_tab_payload)
        self.assertNotIn("include_krx", sig.parameters)
        sig_build = inspect.signature(utp.build_tether_tab_payload)
        self.assertNotIn("krx_futures_rate", sig_build.parameters)

    # 8) crud 함수 호출 횟수 검증 (Step 3a / ADR-038 D2 후):
    #    USDT Redis 5 + DB fallback 1 + bank Redis miss → bank DB 1회 (lazy) +
    #    investing Redis miss → investing DB 1회 (KRX query는 이 경로에 없음)
    def test_crud_call_counts(self):
        # USDT Redis read 모두 None (DB fallback 호출 유도)
        redis_mock = MagicMock(return_value=None)
        db_fallback_mock = MagicMock(return_value=[])
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job", redis_mock), \
             patch.object(utp, "get_latest_source_rates_for_topic", db_fallback_mock), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]) as bank_mock, \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None) as inv_mock, \
             patch.object(utp, "get_latest_bank_rate_from_sync_job", return_value=None), \
             patch.object(utp, "get_latest_investing_rate_from_sync_job", return_value=None):
            utp.load_and_build_tether_tab_payload(MagicMock())

        # USDT: Redis 5번 시도 + 1개라도 miss라 DB fallback 1번
        self.assertEqual(redis_mock.call_count, 5)
        self.assertEqual(db_fallback_mock.call_count, 1)
        # Bank: Redis 2개(kb, hana) 모두 miss → DB lazy 1회
        bank_mock.assert_called_once()
        # Investing: Redis miss → DB fallback 1회
        inv_mock.assert_called_once()

    # 9) 은행 filter — shinhan 등 다른 은행이 응답에 있어도 무시 (kb/hana만 통과)
    def test_bank_filter_ignores_non_tether_tab_banks(self):
        payload = self._run_with(
            usdt_rates_per_source={},
            bank_rates=[
                _bank_row("kb", 1380.0),
                _bank_row("hana", 1381.0),
                _bank_row("shinhan", 1382.0),  # filter out
                _bank_row("woori", 1383.0),    # filter out
            ],
            investing=None,
        )
        banks = [e["source"] for e in payload["data"]["usd_krw_banks"]]
        self.assertEqual(banks, ["kb", "hana"])

    # 10) 혼합 순서 입력 (hana, shinhan, kb) → 출력 (kb, hana, 정렬 + 필터)
    def test_mixed_order_input_produces_kb_then_hana(self):
        """select_latest_bank_rates_from_db가 무작위 순서 반환해도 helper 출력은 안정.

        TETHER_TAB_BANK_SOURCES 순서로 명시 build + builder 재정렬 이중 안전장치.
        """
        payload = self._run_with(
            usdt_rates_per_source={},
            bank_rates=[
                _bank_row("hana", 1.0),
                _bank_row("shinhan", 2.0),  # filter out
                _bank_row("kb", 3.0),
            ],
            investing=None,
        )
        banks = [e["source"] for e in payload["data"]["usd_krw_banks"]]
        self.assertEqual(banks, ["kb", "hana"])


# ---------------------------------------------------------------------------
# PR Z-2e B-Step 2: Redis-first read 계약 잠금 (Codex 권고 5개)
# ---------------------------------------------------------------------------

class TestUsdtKrwRedisFirstReadContract(unittest.TestCase):
    """usdt_krw group Redis-first read 계약 검증:

    1. Redis 5개 모두 hit → DB fallback 미호출
    2. Redis 1개 miss → 전체 fallback 호출
    3. Redis parse fail/exception → None 취급 → 전체 fallback
    4. fallback 결과도 Redis hit 결과와 동일 entry shape (topic-native)
    5. get_source_rates_as_legacy_format은 이 경로에서 절대 사용 X
    """

    def setUp(self):
        # PR Z-2e Step 3a: bank/investing helper 격리 mock — 이 class는 usdt_krw
        # group 검증 중심이라 helper가 실제 redis:6379 접속 시도하지 않게 None 고정.
        self._bank_patch = patch.object(
            utp, "get_latest_bank_rate_from_sync_job", return_value=None)
        self._inv_patch = patch.object(
            utp, "get_latest_investing_rate_from_sync_job", return_value=None)
        self._bank_patch.start()
        self._inv_patch.start()

    def tearDown(self):
        self._bank_patch.stop()
        self._inv_patch.stop()

    def test_all_redis_hit_skips_db_fallback(self):
        """5거래소 모두 Redis hit → DB fallback 호출 0회."""
        def fake_redis(source, asset):
            return {"source": source, "asset": asset, "rate": 1.0,
                    "timestamp": "2026-05-12T15:00:00+09:00"}

        db_mock = MagicMock(return_value=[])
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=fake_redis) as redis_mock, \
             patch.object(utp, "get_latest_source_rates_for_topic", db_mock), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        self.assertEqual(redis_mock.call_count, 5)
        db_mock.assert_not_called()
        # payload data에 5거래소 모두 포함
        sources = sorted(e["source"] for e in payload["data"]["usdt_krw"])
        self.assertEqual(sources, sorted(utp.TETHER_TAB_EXCHANGE_SOURCES))

    def test_one_redis_miss_triggers_full_db_fallback(self):
        """1거래소 Redis miss → 전체 5거래소 DB fallback 호출."""
        def fake_redis(source, asset):
            if source == "gopax":
                return None  # 1개 miss
            return {"source": source, "asset": asset, "rate": 1.0,
                    "timestamp": "2026-05-12T15:00:00+09:00"}

        # DB fallback이 정상 응답 4개 (gopax 누락)
        db_fallback_result = [
            {"source": s, "asset": "usdt-krw", "rate": 2.0,
             "timestamp": "2026-05-12T16:00:00+09:00"}
            for s in ("upbit", "bithumb", "coinone", "korbit")
        ]
        db_mock = MagicMock(return_value=db_fallback_result)
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=fake_redis) as redis_mock, \
             patch.object(utp, "get_latest_source_rates_for_topic", db_mock), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        # Redis 5번 시도, DB fallback 1번 호출 (전체 5거래소 list 전달)
        self.assertEqual(redis_mock.call_count, 5)
        db_mock.assert_called_once()
        # DB fallback 호출 인자: asset + 5거래소 list
        call_kwargs = db_mock.call_args.kwargs
        self.assertEqual(call_kwargs["asset"], "usdt-krw")
        self.assertEqual(call_kwargs["sources"], list(utp.TETHER_TAB_EXCHANGE_SOURCES))
        # 결과는 DB fallback 4개 (Redis 결과 X)
        rates = sorted(e["rate"] for e in payload["data"]["usdt_krw"])
        self.assertEqual(rates, [2.0, 2.0, 2.0, 2.0])

    def test_redis_exception_treated_as_miss_triggers_fallback(self):
        """Redis exception → helper가 None 반환 → 전체 DB fallback."""
        call_count = [0]

        def fake_redis(source, asset):
            call_count[0] += 1
            if source == "korbit":
                return None  # exception 시 helper가 None 반환하는 동작 시뮬
            return {"source": source, "asset": asset, "rate": 1.0,
                    "timestamp": "2026-05-12T15:00:00+09:00"}

        db_mock = MagicMock(return_value=[
            {"source": s, "asset": "usdt-krw", "rate": 3.0,
             "timestamp": "2026-05-12T17:00:00+09:00"}
            for s in utp.TETHER_TAB_EXCHANGE_SOURCES
        ])
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=fake_redis), \
             patch.object(utp, "get_latest_source_rates_for_topic", db_mock), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            utp.load_and_build_tether_tab_payload(MagicMock())

        # exception/parse fail 케이스도 None과 동일하게 fallback 트리거
        db_mock.assert_called_once()

    def test_fallback_entries_match_redis_shape(self):
        """DB fallback 결과도 Redis hit 결과와 동일 topic-native entry shape.

        builder normalization 입장에서 두 경로 결과 구분 불가 — 단일 shape 보장.
        """
        # 모두 Redis miss → DB fallback 사용
        db_result = [
            {"source": s, "asset": "usdt-krw", "rate": 1500.0,
             "timestamp": "2026-05-12T18:00:00+09:00"}
            for s in utp.TETHER_TAB_EXCHANGE_SOURCES
        ]
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job", return_value=None), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=db_result), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        # 모든 entry는 topic-native shape (source, asset, rate, timestamp), display_name 없음
        for entry in payload["data"]["usdt_krw"]:
            self.assertEqual(set(entry.keys()), {"source", "asset", "rate", "timestamp"})
            self.assertNotIn("display_name", entry)
            self.assertNotIn("bank", entry)
            self.assertNotIn("currency", entry)

    def test_does_not_use_get_source_rates_as_legacy_format(self):
        """topic builder source에 legacy adapter(get_source_rates_as_legacy_format) 미포함.

        정적 source 검사 — Z-2d legacy_policy 우회 의도라 module이 legacy adapter
        이름을 import하거나 참조하면 안 됨. (import 시점 hook으로 모든 호출 차단.)
        """
        import inspect
        src = inspect.getsource(utp)
        self.assertNotIn(
            "get_source_rates_as_legacy_format", src,
            "usdt_topic_payload는 Z-2d legacy_policy 우회 위해 topic 전용 fetcher "
            "(get_latest_source_rates_for_topic)만 사용해야 함"
        )


# ---------------------------------------------------------------------------
# TETHER_TAB_EXCHANGE_SOURCES 상수 검증
# ---------------------------------------------------------------------------

class TestExchangeSourcesConstant(unittest.TestCase):
    """TETHER_TAB_EXCHANGE_SOURCES 상수 정합성:
    - 5개 거래소 정확 list
    - SourceRegistry get_usdt_exchange_entries()와 동일 set (sync 검증)
    """

    def test_exchange_sources_exact_match(self):
        self.assertEqual(
            utp.TETHER_TAB_EXCHANGE_SOURCES,
            ("upbit", "bithumb", "coinone", "korbit", "gopax"),
        )

    def test_exchange_sources_matches_source_registry(self):
        """SourceRegistry와 sync 깨지면 운영 inconsistency. 명시 상수 단일 진실 소스."""
        from app.source_registry import get_usdt_exchange_entries
        registry_sources = {e.source for e in get_usdt_exchange_entries()}
        constant_sources = set(utp.TETHER_TAB_EXCHANGE_SOURCES)
        self.assertEqual(constant_sources, registry_sources)


# ---------------------------------------------------------------------------
# PR Z-2e B-Step Telemetry: DB fallback counter 호출 site 검증
# ---------------------------------------------------------------------------

class TestDbFallbackStatsCounter(unittest.TestCase):
    """load_and_build_tether_tab_payload의 DB fallback 분기에서 stats counter
    record_db_fallback이 정확히 호출되는지 잠금.
    """

    def setUp(self):
        from app import usdt_redis_stats
        usdt_redis_stats.reset_stats()
        # Step 3a bank/investing helper 격리 (redis:6379 실제 접속 회피)
        self._bank_patch = patch.object(
            utp, "get_latest_bank_rate_from_sync_job", return_value=None)
        self._inv_patch = patch.object(
            utp, "get_latest_investing_rate_from_sync_job", return_value=None)
        self._bank_patch.start()
        self._inv_patch.start()

    def tearDown(self):
        self._bank_patch.stop()
        self._inv_patch.stop()

    def test_all_redis_hit_no_db_fallback_counter(self):
        """5개 모두 Redis hit → db_fallback_count 증가 X."""
        from app import usdt_redis_stats

        def fake_redis(source, asset):
            return {"source": source, "asset": asset, "rate": 1.0,
                    "timestamp": "2026-05-12T15:00:00+09:00"}

        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=fake_redis), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            utp.load_and_build_tether_tab_payload(MagicMock())

        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["aggregate"]["db_fallback_count"], 0)
        self.assertEqual(stats["aggregate"]["db_fallback_by_asset"], {})

    def test_one_redis_miss_increments_db_fallback_counter(self):
        """1개라도 Redis miss → db_fallback_count 1 + by_asset["usdt-krw"] 1."""
        from app import usdt_redis_stats

        def fake_redis(source, asset):
            if source == "gopax":
                return None
            return {"source": source, "asset": asset, "rate": 1.0,
                    "timestamp": "2026-05-12T15:00:00+09:00"}

        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=fake_redis), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            utp.load_and_build_tether_tab_payload(MagicMock())

        stats = usdt_redis_stats.get_stats()
        self.assertEqual(stats["aggregate"]["db_fallback_count"], 1)
        self.assertEqual(stats["aggregate"]["db_fallback_by_asset"]["usdt-krw"], 1)


# ---------------------------------------------------------------------------
# PR Z-2e Step 3a: bank/investing Redis-first read 분기 (per-source fallback)
# ---------------------------------------------------------------------------

class TestBankInvestingRedisFirstReadContract(unittest.TestCase):
    """usdt:krw topic builder의 banks/reference 부분이 Redis-first로 동작:
    1. KB/Hana 모두 Redis hit → bank DB fallback 호출 X
    2. KB hit / Hana miss → Hana만 DB fallback (per-source)
    3. KB stale → KB만 DB fallback (per-source, is_stale 적용)
    4. Investing hit → investing DB fallback 호출 X
    5. Investing miss → investing DB fallback
    6. KRX는 여전히 DB query 유지 (별도 phase)
    """

    def _native(self, source: str, asset: str, rate: float) -> dict:
        return {"source": source, "asset": asset, "rate": rate,
                "timestamp": "2026-05-13T15:00:00+09:00"}

    def _patches_for_step3a(
        self,
        *,
        bank_redis: dict,        # {bank: native dict or None}
        bank_db_rows: list,      # DB fallback 결과 (legacy shape)
        investing_redis: dict | None,
        investing_db: dict | None,
        usdt_redis_results: list | None = None,
    ):
        """Step 3a Redis-first 분기 mock. USDT는 모두 Redis hit 가정."""
        if usdt_redis_results is None:
            usdt_redis_results = [
                self._native(s, "usdt-krw", 1485.0)
                for s in utp.TETHER_TAB_EXCHANGE_SOURCES
            ]

        def fake_usdt_redis(source, asset):
            for r in usdt_redis_results:
                if r and r["source"] == source:
                    return r
            return None

        def fake_bank_redis(bank, asset):
            return bank_redis.get(bank)

        def fake_investing_redis(asset):
            return investing_redis

        return [
            patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                         side_effect=fake_usdt_redis),
            patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]),
            patch.object(utp, "get_latest_bank_rate_from_sync_job",
                         side_effect=fake_bank_redis),
            patch.object(utp, "get_latest_investing_rate_from_sync_job",
                         side_effect=fake_investing_redis),
            patch.object(utp, "select_latest_bank_rates_from_db", return_value=bank_db_rows),
            patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=investing_db),
        ]

    def test_all_bank_redis_hit_no_db_call(self):
        """KB/Hana 모두 Redis hit → bank DB fallback 호출 X."""
        bank_db_mock = MagicMock(return_value=[])
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=lambda s, a: self._native(s, "usdt-krw", 1.0)), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]), \
             patch.object(utp, "get_latest_bank_rate_from_sync_job",
                          side_effect=lambda b, a: self._native(b, "usd-krw", 1370.0)), \
             patch.object(utp, "get_latest_investing_rate_from_sync_job",
                          return_value=self._native("investing", "usd-krw", 1371.0)), \
             patch.object(utp, "select_latest_bank_rates_from_db", bank_db_mock), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None) as inv_db_mock:
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        bank_db_mock.assert_not_called()
        inv_db_mock.assert_not_called()
        banks = [e["source"] for e in payload["data"]["usd_krw_banks"]]
        self.assertEqual(banks, ["kb", "hana"])

    def test_one_bank_redis_miss_only_that_source_db_fallback(self):
        """KB hit / Hana miss → bank DB는 1회만 (lazy), 결과는 KB(Redis) + Hana(DB)."""
        def fake_bank_redis(bank, asset):
            if bank == "kb":
                return self._native("kb", "usd-krw", 1370.0)  # Redis
            return None  # hana miss

        bank_db_mock = MagicMock(return_value=[
            {"bank": "kb", "currency": "usd-krw", "rate": 9999.0, "timestamp": "..."},  # 안 사용
            {"bank": "hana", "currency": "usd-krw", "rate": 1380.5, "timestamp": "..."},
        ])
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=lambda s, a: self._native(s, "usdt-krw", 1.0)), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]), \
             patch.object(utp, "get_latest_bank_rate_from_sync_job",
                          side_effect=fake_bank_redis), \
             patch.object(utp, "get_latest_investing_rate_from_sync_job",
                          return_value=self._native("investing", "usd-krw", 1371.0)), \
             patch.object(utp, "select_latest_bank_rates_from_db", bank_db_mock), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        # bank DB는 1회만 (lazy load — hana miss 시점에 1회)
        bank_db_mock.assert_called_once()
        banks = payload["data"]["usd_krw_banks"]
        # 결과는 KB(Redis rate 1370) + Hana(DB rate 1380.5)
        kb_entry = next(e for e in banks if e["source"] == "kb")
        hana_entry = next(e for e in banks if e["source"] == "hana")
        self.assertEqual(kb_entry["rate"], 1370.0)  # Redis 값
        self.assertEqual(hana_entry["rate"], 1380.5)  # DB 값

    def test_kb_stale_only_kb_db_fallback(self):
        """KB Redis stale (helper가 None 반환) → KB만 DB fallback. Hana Redis hit 유지."""
        def fake_bank_redis(bank, asset):
            if bank == "kb":
                return None  # stale → helper가 None
            return self._native("hana", "usd-krw", 1380.5)

        bank_db_mock = MagicMock(return_value=[
            {"bank": "kb", "currency": "usd-krw", "rate": 1370.0, "timestamp": "..."},
        ])
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=lambda s, a: self._native(s, "usdt-krw", 1.0)), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]), \
             patch.object(utp, "get_latest_bank_rate_from_sync_job",
                          side_effect=fake_bank_redis), \
             patch.object(utp, "get_latest_investing_rate_from_sync_job",
                          return_value=self._native("investing", "usd-krw", 1371.0)), \
             patch.object(utp, "select_latest_bank_rates_from_db", bank_db_mock), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", return_value=None):
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        bank_db_mock.assert_called_once()
        banks = payload["data"]["usd_krw_banks"]
        kb_entry = next(e for e in banks if e["source"] == "kb")
        hana_entry = next(e for e in banks if e["source"] == "hana")
        self.assertEqual(kb_entry["rate"], 1370.0)  # DB
        self.assertEqual(hana_entry["rate"], 1380.5)  # Redis

    def test_investing_hit_no_db_fallback(self):
        """Investing Redis hit → investing DB 호출 X."""
        inv_db_mock = MagicMock(return_value=None)
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=lambda s, a: self._native(s, "usdt-krw", 1.0)), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]), \
             patch.object(utp, "get_latest_bank_rate_from_sync_job",
                          side_effect=lambda b, a: self._native(b, "usd-krw", 1370.0)), \
             patch.object(utp, "get_latest_investing_rate_from_sync_job",
                          return_value=self._native("investing", "usd-krw", 1371.0)), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", inv_db_mock):
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        inv_db_mock.assert_not_called()
        self.assertEqual(payload["data"]["usd_krw_reference"]["rate"], 1371.0)

    def test_investing_miss_triggers_db_fallback(self):
        """Investing Redis miss → DB fallback 호출."""
        inv_db_mock = MagicMock(return_value={
            "bank": "investing", "currency": "usd-krw", "rate": 1371.2, "timestamp": "..."
        })
        with patch.object(utp, "get_latest_usdt_rate_from_sync_job",
                          side_effect=lambda s, a: self._native(s, "usdt-krw", 1.0)), \
             patch.object(utp, "get_latest_source_rates_for_topic", return_value=[]), \
             patch.object(utp, "get_latest_bank_rate_from_sync_job",
                          side_effect=lambda b, a: self._native(b, "usd-krw", 1370.0)), \
             patch.object(utp, "get_latest_investing_rate_from_sync_job",
                          return_value=None), \
             patch.object(utp, "select_latest_bank_rates_from_db", return_value=[]), \
             patch.object(utp, "select_a_latest_investing_rate_from_db", inv_db_mock):
            payload = utp.load_and_build_tether_tab_payload(MagicMock())

        # 호출 인자 정확 매칭은 MagicMock db 객체 비교 어려움 — 호출 횟수만 검증
        self.assertEqual(inv_db_mock.call_count, 1)
        self.assertEqual(payload["data"]["usd_krw_reference"]["rate"], 1371.2)

    # (제거됨) test_krx_still_uses_db_query_when_include_krx_true — ADR-038 D2로 KRX Redis-first
    # + DB fallback 계약은 app/krx_topic_publisher.load_krx_topic_entry로 이동.
    # tests/test_krx_redis_integration.py::TestKrxTopicEntryRedisFirst가 동일 계약 커버.


# ---------------------------------------------------------------------------
# rate_changed_at 노출 (OPEN 2 해소 — USDT same-bucket ordering, REALTIME_V2 §5 race merge)
# codex 019efe0b: usdt_krw 거래소 entry만 carry(asset 가드) + Redis/DB 양 경로 노출(Option B)
# ---------------------------------------------------------------------------
class TestRateChangedAtCarryScope(unittest.TestCase):
    """build_tether_tab_payload: rate_changed_at는 asset==usdt-krw entry만 carry."""

    def test_usdt_entry_carries_rate_changed_at_when_present(self):
        raw = {
            "source": "upbit", "asset": "usdt-krw", "rate": 1485.0,
            "timestamp": "2026-06-25T15:00:05+09:00",            # seen_at bucket
            "rate_changed_at": "2026-06-25T15:00:03.500000+09:00",  # 정밀
        }
        payload = build_tether_tab_payload(usdt_rates=[raw], bank_rates=[])
        entry = payload["data"]["usdt_krw"][0]
        self.assertEqual(entry["rate_changed_at"], "2026-06-25T15:00:03.500000+09:00")
        self.assertEqual(entry["timestamp"], "2026-06-25T15:00:05+09:00")  # 하위호환 유지

    def test_usdt_entry_omits_rate_changed_at_when_absent(self):
        """additive — raw에 rate_changed_at 없으면 entry에도 없음(기존 동작 보존)."""
        payload = build_tether_tab_payload(usdt_rates=[_u("upbit", 1485.0)], bank_rates=[])
        self.assertNotIn("rate_changed_at", payload["data"]["usdt_krw"][0])

    def test_bank_investing_never_carry_rate_changed_at(self):
        """scope guard: bank/investing(asset=usd-krw)은 raw에 rate_changed_at 있어도 미부착(timestamp 정밀)."""
        bank = {"bank": "kb", "currency": "usd-krw", "rate": 1380.0,
                "timestamp": "2026-06-25T15:00:00+09:00", "rate_changed_at": "X"}
        inv = {"bank": "investing", "currency": "usd-krw", "rate": 1380.2,
               "timestamp": "2026-06-25T15:00:00+09:00", "rate_changed_at": "X"}
        payload = build_tether_tab_payload(
            usdt_rates=[], bank_rates=[bank], investing_rate=inv)
        self.assertNotIn("rate_changed_at", payload["data"]["usd_krw_banks"][0])
        self.assertNotIn("rate_changed_at", payload["data"]["usd_krw_reference"])

    def test_krx_futures_normalize_carries_rate_changed_at_when_present(self):
        """usd-krw-futures(KRX Stage E tick = seen_at alias)도 rate_changed_at carry (USDT 대칭,
        codex 019efe14). ADR-038 D2 후 이 계약의 소비자는 krx_topic_publisher.load_krx_topic_entry
        (_normalize_entry 재사용) — builder 파라미터가 아닌 normalize 레벨에서 잠금."""
        krx = {"source": "krx", "asset": "usd-krw-futures", "rate": 1382.0,
               "timestamp": "2026-06-25T15:00:05+09:00",
               "rate_changed_at": "2026-06-25T15:00:03+09:00"}
        entry = utp._normalize_entry(
            krx, fallback_source="krx", fallback_asset="usd-krw-futures")
        self.assertEqual(entry["rate_changed_at"], "2026-06-25T15:00:03+09:00")
        self.assertEqual(entry["timestamp"], "2026-06-25T15:00:05+09:00")

    def test_krx_futures_normalize_omits_rate_changed_at_when_absent(self):
        """KRX non-tick(generic 3-field) / DB fallback은 rate_changed_at 없음(additive)."""
        krx = {"source": "krx", "asset": "usd-krw-futures", "rate": 1382.0,
               "timestamp": "2026-06-25T15:00:00+09:00"}
        entry = utp._normalize_entry(
            krx, fallback_source="krx", fallback_asset="usd-krw-futures")
        self.assertNotIn("rate_changed_at", entry)


class TestUsdtRedisReadRateChangedAt(unittest.TestCase):
    """get_latest_usdt_rate_from_sync_job: deserialize_usdt_value 전환 + rate_changed_at 노출.

    blocker(codex 019efe0b): Decimal/datetime → float/ISO str 변환 검증(JSON-serializable).
    """

    def _serialize(self, *, rate, rate_changed_at, seen_at, mirrored_at):
        import datetime as _dt
        from decimal import Decimal
        from app import latest_rates_cache as lrc
        return lrc.serialize_usdt_value(
            Decimal(str(rate)),
            _dt.datetime.fromisoformat(rate_changed_at),
            _dt.datetime.fromisoformat(seen_at),
            _dt.datetime.fromisoformat(mirrored_at),
        )

    def test_returns_rate_changed_at_json_serializable(self):
        import json
        from app import latest_rates_cache as lrc
        value = self._serialize(
            rate=1485.0,
            rate_changed_at="2026-06-25T15:00:03+09:00",
            seen_at="2026-06-25T15:00:05+09:00",
            mirrored_at="2026-06-25T15:00:05+09:00",
        )
        client = MagicMock()
        client.get.return_value = value
        with patch.object(lrc, "_get_sync_client", return_value=client):
            result = lrc.get_latest_usdt_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNotNone(result)
        # timestamp=seen_at(bucket), rate_changed_at=정밀 — 서로 다름
        self.assertIn("15:00:05", result["timestamp"])
        self.assertIn("15:00:03", result["rate_changed_at"])
        # JSON-serializable (Decimal/datetime 누출 없음 — blocker fix)
        json.dumps(result)
        self.assertIsInstance(result["rate"], float)
        self.assertIsInstance(result["timestamp"], str)
        self.assertIsInstance(result["rate_changed_at"], str)

    def test_old_schema_rate_changed_at_defaults_to_timestamp(self):
        import json
        from app import latest_rates_cache as lrc
        # old schema (rate_changed_at/seen_at 없음) — deserialize_usdt_value가 timestamp default
        old_value = json.dumps({
            "rate": 1485.0,
            "timestamp": "2026-06-25T15:00:05+09:00",
            "mirrored_at": "2026-06-25T15:00:05+09:00",
        })
        client = MagicMock()
        client.get.return_value = old_value
        with patch.object(lrc, "_get_sync_client", return_value=client):
            result = lrc.get_latest_usdt_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNotNone(result)
        self.assertEqual(result["rate_changed_at"], result["timestamp"])

    def test_malformed_rate_returns_none_db_fallback(self):
        """malformed rate → deserialize_usdt_value가 None(parse fail 계약) → 호출자 DB fallback.

        codex 019efe14: deserialize_value(generic, float+ValueError)는 None 처리했는데
        deserialize_usdt_value는 Decimal+InvalidOperation이 except에 없어 예외 전파했음 → 수정 검증.
        """
        import json
        from app import latest_rates_cache as lrc
        bad_value = json.dumps({
            "rate": "not-a-number",
            "timestamp": "2026-06-25T15:00:05+09:00",
            "rate_changed_at": "2026-06-25T15:00:03+09:00",
            "seen_at": "2026-06-25T15:00:05+09:00",
            "mirrored_at": "2026-06-25T15:00:05+09:00",
        })
        client = MagicMock()
        client.get.return_value = bad_value
        with patch.object(lrc, "_get_sync_client", return_value=client):
            result = lrc.get_latest_usdt_rate_from_sync_job("upbit", "usdt-krw")
        self.assertIsNone(result)  # 예외 전파 아닌 None


class TestKrxRedisReadRateChangedAt(unittest.TestCase):
    """get_latest_krx_rate_from_sync_job: Stage E tick(USDT 5-field schema) rate_changed_at 노출."""

    def test_tick_schema_exposes_rate_changed_at(self):
        import json
        from app import latest_rates_cache as lrc
        from decimal import Decimal
        import datetime as _dt
        # Stage E tick writer가 쓰는 serialize_usdt_value 값 (timestamp=seen_at, rate_changed_at=정밀)
        value = lrc.serialize_usdt_value(
            Decimal("1382.0"),
            _dt.datetime.fromisoformat("2026-06-25T15:00:03+09:00"),  # rate_changed_at
            _dt.datetime.fromisoformat("2026-06-25T15:00:05+09:00"),  # seen_at
            _dt.datetime.fromisoformat("2026-06-25T15:00:05+09:00"),  # mirrored_at
        )
        client = MagicMock()
        client.get.return_value = value
        with patch.object(lrc, "_get_sync_client", return_value=client):
            result = lrc.get_latest_krx_rate_from_sync_job("usd-krw-futures")
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "krx")
        self.assertIn("15:00:05", result["timestamp"])       # seen_at bucket
        self.assertIn("15:00:03", result["rate_changed_at"])  # 정밀
        json.dumps(result)  # JSON-serializable (Decimal/datetime 누출 없음)
        self.assertIsInstance(result["rate"], float)

    def test_generic_schema_rate_changed_at_defaults_to_timestamp(self):
        """non-tick writer(generic 3-field serialize_value) 값도 처리 — rate_changed_at=timestamp."""
        import json
        from app import latest_rates_cache as lrc
        old_value = json.dumps({
            "rate": 1382.0,
            "timestamp": "2026-06-25T15:00:00+09:00",
            "mirrored_at": "2026-06-25T15:00:00+09:00",
        })
        client = MagicMock()
        client.get.return_value = old_value
        with patch.object(lrc, "_get_sync_client", return_value=client):
            result = lrc.get_latest_krx_rate_from_sync_job("usd-krw-futures")
        self.assertIsNotNone(result)
        self.assertEqual(result["rate_changed_at"], result["timestamp"])


class TestSourceRatesForTopicRateChangedAt(unittest.TestCase):
    """get_latest_source_rates_for_topic(DB fallback): rate_changed_at = timestamp(정밀)."""

    def test_db_fallback_includes_rate_changed_at_equal_timestamp(self):
        import datetime as _dt
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app import crud, models

        engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add(models.SourceRate(
                source="upbit", asset="usdt-krw", rate=1485.0,
                timestamp=_dt.datetime(2026, 6, 25, 6, 0, 0),  # naive UTC
            ))
            db.commit()
            rows = crud.get_latest_source_rates_for_topic(db, "usdt-krw", ["upbit"])
        finally:
            db.close()
        self.assertEqual(len(rows), 1)
        self.assertIn("rate_changed_at", rows[0])
        self.assertEqual(rows[0]["rate_changed_at"], rows[0]["timestamp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
