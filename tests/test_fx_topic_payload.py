"""FX topic payload builder 단위 테스트 (PR Z-2c Step 1).

검증 대상:
    - build_fx_tab_payload (pure builder)
    - load_and_build_fx_topic_payload (DB 의존 entry point)
    - Schema invariant (type/version/data + banks/reference)
    - asset whitelist (FX_TOPIC_ASSETS) — fail-fast
    - BANK_DISPLAY_ORDER whitelist + 명시 build (DB 순서 격리)
    - reference Optional (None / mismatch 시 key 누락)
    - usd-krw / jpy-krw / eur-krw 모두 동일 builder
    - topic 필드 부재 (topic-agnostic 원칙)
    - display_name 부재 (단말 자체 registry 원칙)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.fx_topic_payload import (
    FX_TOPIC_ASSETS,
    build_fx_tab_payload,
    load_and_build_fx_topic_payload,
)


def _bank_legacy(bank: str, asset: str = "usd-krw", rate: float = 1370.0,
                  ts: str = "2026-05-11T15:00:00+09:00") -> dict:
    """은행 legacy shape — crud.select_latest_bank_rates_from_db 반환과 일치."""
    return {"bank": bank, "currency": asset, "rate": rate, "timestamp": ts}


def _investing_legacy(asset: str = "usd-krw", rate: float = 1371.0,
                       ts: str = "2026-05-11T15:00:00+09:00") -> dict:
    """Investing legacy shape — crud.select_a_latest_investing_rate_from_db 반환과 일치."""
    return {"bank": "investing", "currency": asset, "rate": rate, "timestamp": ts}


# ---------------------------------------------------------------------------
# Test 1 — 정상 payload shape
# ---------------------------------------------------------------------------

class TestPayloadShape(unittest.TestCase):

    def test_normal_payload_top_level_keys(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [_bank_legacy("kb", rate=1370.0)],
            _investing_legacy(rate=1371.0),
        )
        self.assertEqual(set(payload.keys()), {"type", "version", "data"})
        self.assertEqual(payload["type"], "snapshot")
        self.assertEqual(payload["version"], 1)

    def test_data_contains_banks_and_reference_when_both_present(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [_bank_legacy("kb")],
            _investing_legacy(),
        )
        self.assertIn("banks", payload["data"])
        self.assertIn("reference", payload["data"])


# ---------------------------------------------------------------------------
# Test 2 — top-level에 topic 없음 (topic-agnostic)
# ---------------------------------------------------------------------------

class TestTopicAgnostic(unittest.TestCase):

    def test_payload_has_no_topic_field(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [_bank_legacy("kb")],
            _investing_legacy(),
        )
        self.assertNotIn("topic", payload)


# ---------------------------------------------------------------------------
# Test 3 — entry에 display_name 없음
# ---------------------------------------------------------------------------

class TestEntryShapeNoDisplayName(unittest.TestCase):

    def test_bank_entry_has_only_four_keys(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [_bank_legacy("kb", rate=1370.5)],
            None,
        )
        entry = payload["data"]["banks"][0]
        self.assertEqual(set(entry.keys()), {"source", "asset", "rate", "timestamp"})
        self.assertNotIn("display_name", entry)
        self.assertNotIn("bank", entry)
        self.assertNotIn("currency", entry)

    def test_reference_entry_has_only_four_keys(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [],
            _investing_legacy(rate=1371.2),
        )
        entry = payload["data"]["reference"]
        self.assertEqual(set(entry.keys()), {"source", "asset", "rate", "timestamp"})
        self.assertNotIn("display_name", entry)


# ---------------------------------------------------------------------------
# Test 4 — banks key 항상 존재
# ---------------------------------------------------------------------------

class TestBanksAlwaysPresent(unittest.TestCase):

    def test_banks_key_exists_with_input(self):
        payload = build_fx_tab_payload("usd-krw", [_bank_legacy("kb")], None)
        self.assertIn("banks", payload["data"])

    def test_banks_key_exists_without_input(self):
        payload = build_fx_tab_payload("usd-krw", [], None)
        self.assertIn("banks", payload["data"])

    def test_banks_key_exists_with_only_reference(self):
        payload = build_fx_tab_payload("usd-krw", [], _investing_legacy())
        self.assertIn("banks", payload["data"])


# ---------------------------------------------------------------------------
# Test 5 — 은행 없음 → banks: []
# ---------------------------------------------------------------------------

class TestEmptyBanks(unittest.TestCase):

    def test_empty_bank_rates_gives_empty_list(self):
        payload = build_fx_tab_payload("usd-krw", [], _investing_legacy())
        self.assertEqual(payload["data"]["banks"], [])


# ---------------------------------------------------------------------------
# Test 6 — 모든 은행 + reference 없음 → data = {"banks": []}
# ---------------------------------------------------------------------------

class TestFullyEmpty(unittest.TestCase):

    def test_no_banks_no_reference_data_only_banks(self):
        payload = build_fx_tab_payload("usd-krw", [], None)
        self.assertEqual(payload["data"], {"banks": []})
        # publish는 계속 가능 (schema invariant 유지)
        self.assertEqual(payload["type"], "snapshot")
        self.assertEqual(payload["version"], 1)


# ---------------------------------------------------------------------------
# Test 7 — DB 반환 순서 섞여도 BANK_DISPLAY_ORDER 출력 (핵심 invariant)
# ---------------------------------------------------------------------------

class TestBankOrderingIndependentOfDb(unittest.TestCase):

    def test_scrambled_input_outputs_bank_display_order(self):
        # DB가 의도적으로 뒤섞인 순서로 반환했다고 가정
        scrambled = [
            _bank_legacy("citi"),
            _bank_legacy("kb"),
            _bank_legacy("nh"),
            _bank_legacy("hana"),
            _bank_legacy("shinhan"),
            _bank_legacy("woori"),
            _bank_legacy("sc"),
            _bank_legacy("ibk"),
            _bank_legacy("bs"),
        ]
        payload = build_fx_tab_payload("usd-krw", scrambled, None)
        sources = [e["source"] for e in payload["data"]["banks"]]
        # BANK_DISPLAY_ORDER 순서 그대로
        self.assertEqual(
            sources,
            ["kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi"],
        )


# ---------------------------------------------------------------------------
# Test 8 — 미등록 은행 제외 (BANK_DISPLAY_ORDER whitelist)
# ---------------------------------------------------------------------------

class TestUnknownBankExcluded(unittest.TestCase):

    def test_unknown_bank_source_filtered_out(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [
                _bank_legacy("kb"),
                _bank_legacy("kakao"),   # 미등록
                _bank_legacy("hana"),
                _bank_legacy("toss"),    # 미등록
            ],
            None,
        )
        sources = [e["source"] for e in payload["data"]["banks"]]
        self.assertEqual(sources, ["kb", "hana"])
        self.assertNotIn("kakao", sources)
        self.assertNotIn("toss", sources)


# ---------------------------------------------------------------------------
# Test 9 — asset mismatch 은행 제외
# ---------------------------------------------------------------------------

class TestAssetMismatchExcluded(unittest.TestCase):

    def test_bank_with_different_asset_excluded(self):
        # builder에 asset="usd-krw"인데 row의 currency가 jpy-krw인 경우
        # (정상 DB query에선 불가능하지만 호출자 실수 시뮬레이션)
        payload = build_fx_tab_payload(
            "usd-krw",
            [
                _bank_legacy("kb", asset="usd-krw"),
                _bank_legacy("hana", asset="jpy-krw"),  # mismatch
            ],
            None,
        )
        sources = [e["source"] for e in payload["data"]["banks"]]
        self.assertEqual(sources, ["kb"])


# ---------------------------------------------------------------------------
# Test 10 — reference 정상 포함
# ---------------------------------------------------------------------------

class TestReferenceIncluded(unittest.TestCase):

    def test_reference_included_when_source_asset_match(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [],
            _investing_legacy(rate=1371.5),
        )
        ref = payload["data"]["reference"]
        self.assertEqual(ref["source"], "investing")
        self.assertEqual(ref["asset"], "usd-krw")
        self.assertEqual(ref["rate"], 1371.5)


# ---------------------------------------------------------------------------
# Test 11 — reference None → key 누락
# ---------------------------------------------------------------------------

class TestReferenceNoneOmitted(unittest.TestCase):

    def test_reference_none_key_absent(self):
        payload = build_fx_tab_payload("usd-krw", [_bank_legacy("kb")], None)
        self.assertNotIn("reference", payload["data"])


# ---------------------------------------------------------------------------
# Test 12 — reference source/asset mismatch → key 누락
# ---------------------------------------------------------------------------

class TestReferenceMismatchOmitted(unittest.TestCase):

    def test_reference_wrong_source_key_absent(self):
        # source가 investing이 아닌 경우
        bogus = {"source": "bogus_source", "asset": "usd-krw",
                 "rate": 1371.0, "timestamp": "2026-05-11T15:00:00+09:00"}
        payload = build_fx_tab_payload("usd-krw", [], bogus)
        self.assertNotIn("reference", payload["data"])

    def test_reference_wrong_asset_key_absent(self):
        # asset이 builder asset과 다른 경우
        wrong_asset = _investing_legacy(asset="jpy-krw")
        payload = build_fx_tab_payload("usd-krw", [], wrong_asset)
        self.assertNotIn("reference", payload["data"])


# ---------------------------------------------------------------------------
# Test 13 — usd-krw / jpy-krw / eur-krw 모두 처리
# ---------------------------------------------------------------------------

class TestAllAssetsSupported(unittest.TestCase):

    def test_usd_krw_builds(self):
        payload = build_fx_tab_payload(
            "usd-krw",
            [_bank_legacy("kb", asset="usd-krw")],
            _investing_legacy(asset="usd-krw"),
        )
        self.assertEqual(payload["data"]["banks"][0]["asset"], "usd-krw")
        self.assertEqual(payload["data"]["reference"]["asset"], "usd-krw")

    def test_jpy_krw_builds(self):
        payload = build_fx_tab_payload(
            "jpy-krw",
            [_bank_legacy("kb", asset="jpy-krw", rate=9.85)],
            _investing_legacy(asset="jpy-krw", rate=9.83),
        )
        self.assertEqual(payload["data"]["banks"][0]["asset"], "jpy-krw")
        self.assertEqual(payload["data"]["reference"]["asset"], "jpy-krw")

    def test_eur_krw_builds(self):
        payload = build_fx_tab_payload(
            "eur-krw",
            [_bank_legacy("kb", asset="eur-krw", rate=1490.0)],
            _investing_legacy(asset="eur-krw", rate=1489.0),
        )
        self.assertEqual(payload["data"]["banks"][0]["asset"], "eur-krw")
        self.assertEqual(payload["data"]["reference"]["asset"], "eur-krw")


# ---------------------------------------------------------------------------
# Test 14 — invalid asset → ValueError (두 entry point 모두)
# ---------------------------------------------------------------------------

class TestInvalidAssetRejected(unittest.TestCase):

    def test_build_rejects_usdt_krw(self):
        with self.assertRaises(ValueError):
            build_fx_tab_payload("usdt-krw", [], None)

    def test_build_rejects_futures(self):
        with self.assertRaises(ValueError):
            build_fx_tab_payload("usd-krw-futures", [], None)

    def test_build_rejects_typo(self):
        with self.assertRaises(ValueError):
            build_fx_tab_payload("usd-kwr", [], None)  # typo

    def test_build_rejects_empty(self):
        with self.assertRaises(ValueError):
            build_fx_tab_payload("", [], None)

    def test_load_and_build_rejects_invalid_asset(self):
        # DB 호출 entry point도 같은 검증
        db = MagicMock()
        with self.assertRaises(ValueError):
            load_and_build_fx_topic_payload(db, "usdt-krw")
        # DB query 호출 X (검증 시 fail-fast)
        db.query.assert_not_called()

    def test_fx_topic_assets_constant_exact(self):
        """FX_TOPIC_ASSETS 상수가 변경되면 단말 contract도 영향 — 회귀 보호."""
        self.assertEqual(FX_TOPIC_ASSETS, ("usd-krw", "jpy-krw", "eur-krw"))


# ---------------------------------------------------------------------------
# load_and_build_fx_topic_payload — DB integration via mock
# ---------------------------------------------------------------------------

class TestLoadAndBuildIntegration(unittest.TestCase):
    """Step 3c (2026-05-13): load_and_build_fx_topic_payload는 Redis-first로 전환됨.
    기존 DB 경로 검증은 Redis miss(None) 강제 + DB fallback 호출로 잠금.
    Redis helper(get_latest_bank/investing_rate_from_sync_job) mock으로 실제
    redis:6379 접속 시도 차단 (Step 3a test pollution 학습 반영)."""

    def setUp(self):
        # Redis helper 격리 — 기본 None 반환 → DB fallback path
        self._bank_redis_patch = patch(
            "app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
            return_value=None,
        )
        self._inv_redis_patch = patch(
            "app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
            return_value=None,
        )
        self._bank_redis_patch.start()
        self._inv_redis_patch.start()

    def tearDown(self):
        self._bank_redis_patch.stop()
        self._inv_redis_patch.stop()

    def test_load_and_build_calls_crud_with_asset(self):
        db = MagicMock()
        with patch(
            "app.fx_topic_payload.select_latest_bank_rates_from_db"
        ) as mock_banks, patch(
            "app.fx_topic_payload.select_a_latest_investing_rate_from_db"
        ) as mock_ref:
            mock_banks.return_value = [_bank_legacy("kb", asset="jpy-krw", rate=9.85)]
            mock_ref.return_value = _investing_legacy(asset="jpy-krw", rate=9.83)

            payload = load_and_build_fx_topic_payload(db, "jpy-krw")

            # Redis miss 강제 → bank DB lazy 1회 + investing DB 1회
            mock_banks.assert_called_once_with(db, "jpy-krw")
            mock_ref.assert_called_once_with(db, "jpy-krw")

        self.assertEqual(payload["data"]["banks"][0]["source"], "kb")
        self.assertEqual(payload["data"]["reference"]["rate"], 9.83)

    def test_load_and_build_handles_db_returning_none_reference(self):
        db = MagicMock()
        with patch(
            "app.fx_topic_payload.select_latest_bank_rates_from_db"
        ) as mock_banks, patch(
            "app.fx_topic_payload.select_a_latest_investing_rate_from_db"
        ) as mock_ref:
            mock_banks.return_value = [_bank_legacy("kb")]
            mock_ref.return_value = None

            payload = load_and_build_fx_topic_payload(db, "usd-krw")

        self.assertNotIn("reference", payload["data"])
        self.assertEqual(payload["data"]["banks"][0]["source"], "kb")

    def test_load_and_build_handles_db_returning_empty_banks(self):
        db = MagicMock()
        with patch(
            "app.fx_topic_payload.select_latest_bank_rates_from_db"
        ) as mock_banks, patch(
            "app.fx_topic_payload.select_a_latest_investing_rate_from_db"
        ) as mock_ref:
            mock_banks.return_value = []
            mock_ref.return_value = _investing_legacy()

            payload = load_and_build_fx_topic_payload(db, "usd-krw")

        self.assertEqual(payload["data"]["banks"], [])
        self.assertIn("reference", payload["data"])


# ---------------------------------------------------------------------------
# PR Z-2e Step 3c: FX topic Redis-first read 계약 (Step 3a 패턴 재사용)
# ---------------------------------------------------------------------------

class TestFxRedisFirstReadContract(unittest.TestCase):
    """fx:* topic builder가 bank/investing helper를 Redis-first로 사용:

    1. 9 banks 모두 Redis hit → bank DB 호출 X
    2. 1 bank miss → 그 bank만 DB (lazy 1회 + per-source)
    3. 1 bank stale (helper가 None) → 그 bank만 DB
    4. Investing hit → investing DB 호출 X
    5. Investing miss → investing DB fallback
    6. 3 asset (usd-krw, jpy-krw, eur-krw) 모두 같은 helper 호출 패턴
    """

    def _native(self, source: str, asset: str, rate: float) -> dict:
        return {"source": source, "asset": asset, "rate": rate,
                "timestamp": "2026-05-13T15:00:00+09:00"}

    def test_all_banks_redis_hit_no_db_call(self):
        from app.crud import BANK_DISPLAY_ORDER

        def fake_bank_redis(bank, asset):
            return self._native(bank, asset, 1370.0)

        bank_db_mock = MagicMock(return_value=[])
        with patch("app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                   side_effect=fake_bank_redis), \
             patch("app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
                   return_value=self._native("investing", "usd-krw", 1371.0)), \
             patch("app.fx_topic_payload.select_latest_bank_rates_from_db", bank_db_mock), \
             patch("app.fx_topic_payload.select_a_latest_investing_rate_from_db",
                   return_value=None) as inv_db_mock:
            payload = load_and_build_fx_topic_payload(MagicMock(), "usd-krw")

        bank_db_mock.assert_not_called()
        inv_db_mock.assert_not_called()
        banks = [e["source"] for e in payload["data"]["banks"]]
        self.assertEqual(set(banks), set(BANK_DISPLAY_ORDER))

    def test_one_bank_redis_miss_lazy_db_fallback(self):
        """1개 bank Redis miss → bank DB 1회 (lazy) — 미수신 source만 DB값 사용."""
        def fake_bank_redis(bank, asset):
            if bank == "shinhan":
                return None  # miss
            return self._native(bank, asset, 1370.0)

        bank_db_mock = MagicMock(return_value=[
            {"bank": "shinhan", "currency": "usd-krw", "rate": 1369.5, "timestamp": "..."},
        ])
        with patch("app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                   side_effect=fake_bank_redis), \
             patch("app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
                   return_value=self._native("investing", "usd-krw", 1371.0)), \
             patch("app.fx_topic_payload.select_latest_bank_rates_from_db", bank_db_mock), \
             patch("app.fx_topic_payload.select_a_latest_investing_rate_from_db",
                   return_value=None):
            payload = load_and_build_fx_topic_payload(MagicMock(), "usd-krw")

        bank_db_mock.assert_called_once()
        banks = {e["source"]: e["rate"] for e in payload["data"]["banks"]}
        self.assertEqual(banks["shinhan"], 1369.5)  # DB
        self.assertEqual(banks["kb"], 1370.0)  # Redis

    def test_one_bank_stale_only_that_bank_db(self):
        """1개 bank Redis stale (helper가 None 반환) → 그 bank만 DB."""
        def fake_bank_redis(bank, asset):
            if bank == "woori":
                return None  # stale
            return self._native(bank, asset, 1370.0)

        bank_db_mock = MagicMock(return_value=[
            {"bank": "woori", "currency": "usd-krw", "rate": 1371.2, "timestamp": "..."},
        ])
        with patch("app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                   side_effect=fake_bank_redis), \
             patch("app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
                   return_value=self._native("investing", "usd-krw", 1371.0)), \
             patch("app.fx_topic_payload.select_latest_bank_rates_from_db", bank_db_mock), \
             patch("app.fx_topic_payload.select_a_latest_investing_rate_from_db",
                   return_value=None):
            payload = load_and_build_fx_topic_payload(MagicMock(), "usd-krw")

        bank_db_mock.assert_called_once()
        banks = {e["source"]: e["rate"] for e in payload["data"]["banks"]}
        self.assertEqual(banks["woori"], 1371.2)  # DB

    def test_investing_hit_no_db_fallback(self):
        with patch("app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                   side_effect=lambda b, a: self._native(b, a, 1370.0)), \
             patch("app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
                   return_value=self._native("investing", "usd-krw", 1371.5)), \
             patch("app.fx_topic_payload.select_latest_bank_rates_from_db",
                   return_value=[]), \
             patch("app.fx_topic_payload.select_a_latest_investing_rate_from_db",
                   return_value=None) as inv_db_mock:
            payload = load_and_build_fx_topic_payload(MagicMock(), "usd-krw")

        inv_db_mock.assert_not_called()
        self.assertEqual(payload["data"]["reference"]["rate"], 1371.5)

    def test_investing_miss_triggers_db_fallback(self):
        inv_db_mock = MagicMock(return_value={
            "bank": "investing", "currency": "usd-krw", "rate": 1371.2,
            "timestamp": "2026-05-13T15:00:00+09:00",
        })
        with patch("app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                   side_effect=lambda b, a: self._native(b, a, 1370.0)), \
             patch("app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
                   return_value=None), \
             patch("app.fx_topic_payload.select_latest_bank_rates_from_db",
                   return_value=[]), \
             patch("app.fx_topic_payload.select_a_latest_investing_rate_from_db",
                   inv_db_mock):
            payload = load_and_build_fx_topic_payload(MagicMock(), "usd-krw")

        self.assertEqual(inv_db_mock.call_count, 1)
        self.assertEqual(payload["data"]["reference"]["rate"], 1371.2)

    def test_redis_first_works_for_all_three_assets(self):
        """3 asset 각각 — helper 호출 시 asset 인자 정확 전달."""
        bank_redis_calls: List[tuple] = []

        def fake_bank_redis(bank, asset):
            bank_redis_calls.append((bank, asset))
            return self._native(bank, asset, 1.0)

        inv_redis_calls: List[str] = []

        def fake_inv_redis(asset):
            inv_redis_calls.append(asset)
            return self._native("investing", asset, 1.0)

        for asset in ("usd-krw", "jpy-krw", "eur-krw"):
            bank_redis_calls.clear()
            inv_redis_calls.clear()
            with patch("app.fx_topic_payload.get_latest_bank_rate_from_sync_job",
                       side_effect=fake_bank_redis), \
                 patch("app.fx_topic_payload.get_latest_investing_rate_from_sync_job",
                       side_effect=fake_inv_redis), \
                 patch("app.fx_topic_payload.select_latest_bank_rates_from_db",
                       return_value=[]), \
                 patch("app.fx_topic_payload.select_a_latest_investing_rate_from_db",
                       return_value=None):
                payload = load_and_build_fx_topic_payload(MagicMock(), asset)
            # 9 banks × asset Redis 호출 + 1 investing 호출
            self.assertEqual(len(bank_redis_calls), 9)
            self.assertTrue(all(call[1] == asset for call in bank_redis_calls))
            self.assertEqual(inv_redis_calls, [asset])
            # payload entries asset 정확
            self.assertTrue(all(e["asset"] == asset for e in payload["data"]["banks"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
