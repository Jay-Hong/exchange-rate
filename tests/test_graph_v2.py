"""Graph API v2 (Phase 2e MVP) 테스트 — app/graph_v2.py.

8종 (Codex/Claude 수렴):
  1. catalog exposes only 3m/1y
  2. unsupported 1d/1w period
  3. tab composition: usd/jpy/eur/tether
  4. Hana mixed provenance + per-point close_basis/source_method
  5. single close_basis provenance (per-point 없음)
  6. KRX per-point contract_code
  7. DXY market_index daily mapping
  8. insufficient_history edge

build_tab(db, ...)은 db 주입이라 patch 불필요 — in-memory SQLite session 직접 전달.
"""
import unittest
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.models import SourceDailyRate, MarketIndexRate
from app import graph_v2 as G

TODAY = date(2026, 6, 6)  # period_range 결정용 (고정)


def _sdr(source, asset, d, close, close_basis, source_method,
         ohlc_quality="observed_rollup", contract_code=None, high=None, low=None):
    return SourceDailyRate(
        source=source, asset=asset, date_kst=d,
        rate=close, high=high if high is not None else close,
        low=low if low is not None else close, close=close,
        ohlc_quality=ohlc_quality, close_basis=close_basis, source_method=source_method,
        contract_code=contract_code,
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _add(self, *rows):
        for r in rows:
            self.db.add(r)
        self.db.commit()


# ---------------------------------------------------------------------------
# 1-3. catalog (DB 무관)
# ---------------------------------------------------------------------------

class TestCatalog(unittest.TestCase):

    def test_catalog_only_3m_1y(self):
        cat = G.build_catalog()
        self.assertEqual(cat["supported_periods"], ["3m", "1y"])
        for t in cat["tabs"]:
            self.assertEqual(set(t["periods"].keys()), {"3m", "1y"})  # 1d/1w 미노출

    def test_unsupported_period(self):
        self.assertTrue(G.is_supported_period("3m"))
        self.assertTrue(G.is_supported_period("1y"))
        self.assertFalse(G.is_supported_period("1d"))
        self.assertFalse(G.is_supported_period("1w"))

    def test_tab_composition(self):
        tabs = {t["id"]: t for t in G.build_catalog()["tabs"]}
        self.assertEqual(set(tabs), {"usd", "jpy", "eur", "tether"})
        self.assertEqual(tabs["usd"]["periods"]["3m"]["all_series"],
                         ["investing.usd", "hana.usd", "dxy"])
        self.assertEqual(tabs["jpy"]["periods"]["3m"]["all_series"],
                         ["investing.jpy", "hana.jpy"])
        self.assertNotIn("index", tabs["jpy"]["axis_groups"])   # JPY는 DXY 없음
        self.assertIn("krx.usd-krw-futures", tabs["tether"]["periods"]["3m"]["all_series"])
        self.assertIn("bithumb.usdt-krw", tabs["tether"]["periods"]["3m"]["all_series"])
        self.assertIn("index", tabs["tether"]["axis_groups"])   # tether는 DXY 있음


# ---------------------------------------------------------------------------
# 4-5. provenance (single/mixed)
# ---------------------------------------------------------------------------

class TestProvenance(_Base):

    def _usd_3m(self):
        return G.build_tab(self.db, "usd", "3m", today_kst=TODAY)

    def test_hana_mixed_per_point(self):
        """Hana usd: official_backfill + observed_eod 2 close_basis → mixed + per-point."""
        self._add(
            _sdr("hana", "usd-krw", date(2026, 5, 1), 1380.0,
                 "hana_official_historical_backfill", "external_backfill", "close_only"),
            _sdr("hana", "usd-krw", date(2026, 6, 1), 1390.0,
                 "hana_observed_eod", "observed_rollup"),
        )
        hana = next(s for s in self._usd_3m()["series"] if s["id"] == "hana.usd")
        prov = hana["provenance"]
        self.assertEqual(prov["close_basis_mode"], "mixed")
        self.assertEqual(prov["default_close_basis"], "hana_observed_eod")  # 최신(going-forward)
        self.assertEqual(prov["close_basis_values"],
                         ["hana_observed_eod", "hana_official_historical_backfill"])
        self.assertIn("close_basis", prov["per_point_metadata"])
        self.assertIn("source_method", prov["per_point_metadata"])
        # per-point에 실제 포함
        self.assertEqual(hana["data"][0]["close_basis"], "hana_official_historical_backfill")
        self.assertEqual(hana["data"][1]["close_basis"], "hana_observed_eod")

    def test_single_close_basis_no_per_point(self):
        """Investing usd: 단일 close_basis → single + per-point 없음 (mixed 분기 대칭)."""
        self._add(
            _sdr("investing", "usd-krw", date(2026, 5, 1), 1380.0,
                 "investing_observed_eod", "observed_rollup"),
            _sdr("investing", "usd-krw", date(2026, 6, 1), 1390.0,
                 "investing_observed_eod", "observed_rollup"),
        )
        inv = next(s for s in self._usd_3m()["series"] if s["id"] == "investing.usd")
        prov = inv["provenance"]
        self.assertEqual(prov["close_basis_mode"], "single")
        self.assertEqual(prov["per_point_metadata"], [])
        self.assertNotIn("close_basis_values", prov)       # single은 values 미포함
        self.assertNotIn("close_basis", inv["data"][0])    # per-point 없음
        # data point 기본 필드
        self.assertEqual(inv["data"][1]["rate"], 1390.0)
        self.assertEqual(inv["data"][1]["source"], "investing")
        self.assertTrue(inv["data"][1]["ts"].endswith("+09:00"))  # KST 포맷


# ---------------------------------------------------------------------------
# 6. KRX per-point contract_code
# ---------------------------------------------------------------------------

class TestKrxContractCode(_Base):

    def test_krx_contract_code_per_point(self):
        self._add(
            _sdr("krx", "usd-krw-futures", date(2026, 5, 1), 1490.0,
                 "krx_openapi_daily", "krx_openapi_daily", "source_ohlc", contract_code="A75605"),
            _sdr("krx", "usd-krw-futures", date(2026, 6, 1), 1530.0,
                 "krx_cf_close_1545", "close_finalizer", contract_code="A75606"),
        )
        krx = next(s for s in G.build_tab(self.db, "tether", "3m", today_kst=TODAY)["series"]
                   if s["id"] == "krx.usd-krw-futures")
        self.assertIn("contract_code", krx["provenance"]["per_point_metadata"])
        self.assertEqual(krx["data"][0]["contract_code"], "A75605")
        self.assertEqual(krx["data"][1]["contract_code"], "A75606")
        # KRX는 close_basis 2개라 mixed로도 잡힘 (정상 — contract_code + close_basis 둘 다)
        self.assertEqual(krx["data"][1]["rate"], 1530.0)


# ---------------------------------------------------------------------------
# 7. DXY market_index daily mapping
# ---------------------------------------------------------------------------

class TestDxyMapping(_Base):

    def test_dxy_market_index_daily(self):
        # 3m(start≈2026-03-08) span: 첫 daily 2026-03-10 → insufficient=false
        self._add(
            MarketIndexRate(instrument="dxy", source="investing", rate=99.5,
                            timestamp=datetime(2026, 3, 10, 1, 0), granularity="daily"),
            MarketIndexRate(instrument="dxy", source="investing", rate=100.2,
                            timestamp=datetime(2026, 6, 1, 1, 0), granularity="daily"),
            # realtime/hourly는 daily reader가 무시해야 함
            MarketIndexRate(instrument="dxy", source="investing", rate=88.8,
                            timestamp=datetime(2026, 6, 1, 2, 0), granularity="realtime"),
        )
        dxy = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"]
                   if s["id"] == "dxy")
        self.assertEqual(dxy["axis_group"], "index")
        self.assertEqual(dxy["unit"], "INDEX")
        self.assertEqual(len(dxy["data"]), 2)              # daily 2개만 (realtime 무시)
        self.assertEqual(dxy["data"][0]["rate"], 99.5)
        self.assertEqual(dxy["data"][1]["rate"], 100.2)
        self.assertEqual(dxy["provenance"]["close_basis_mode"], "single")
        self.assertEqual(dxy["provenance"]["per_point_metadata"], [])
        self.assertFalse(dxy["provenance"]["insufficient_history"])

    def test_dxy_source_priority_investing_wins(self):
        """같은 KST date에 investing + yahoo → investing 우선 (crud.py priority 유지, last-row 회귀 방지)."""
        self._add(
            MarketIndexRate(instrument="dxy", source="investing", rate=99.5,
                            timestamp=datetime(2026, 6, 1, 1, 0), granularity="daily"),
            # yahoo가 더 늦은 timestamp(05:00)지만 priority 낮음 → investing 선택돼야
            MarketIndexRate(instrument="dxy", source="yahoo", rate=77.7,
                            timestamp=datetime(2026, 6, 1, 5, 0), granularity="daily"),
        )
        dxy = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"]
                   if s["id"] == "dxy")
        d = next(p for p in dxy["data"] if p["ts"].startswith("2026-06-01"))
        self.assertEqual(d["rate"], 99.5)         # yahoo 77.7 아님
        self.assertEqual(d["source"], "investing")


# ---------------------------------------------------------------------------
# 8. insufficient_history edge
# ---------------------------------------------------------------------------

class TestInsufficientHistory(_Base):

    def test_empty_series_insufficient_history(self):
        """데이터 없는 series → insufficient_history=true + data 빈 배열."""
        tab = G.build_tab(self.db, "usd", "3m", today_kst=TODAY)
        hana = next(s for s in tab["series"] if s["id"] == "hana.usd")
        self.assertTrue(hana["provenance"]["insufficient_history"])
        self.assertEqual(hana["data"], [])
        self.assertEqual(hana["provenance"]["close_basis_mode"], "single")  # 빈 series 기본

    def test_partial_coverage_insufficient(self):
        """coverage < period — 데이터 있지만 첫 row가 period 시작보다 한참 늦음 → insufficient=true (신규 자산)."""
        # 3m(start≈2026-03-08)인데 데이터가 2026-05-20부터만 (73일 늦음 > tolerance 7)
        self._add(
            _sdr("investing", "usd-krw", date(2026, 5, 20), 1380.0, "investing_observed_eod", "observed_rollup"),
            _sdr("investing", "usd-krw", date(2026, 6, 1), 1390.0, "investing_observed_eod", "observed_rollup"),
        )
        inv = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"]
                   if s["id"] == "investing.usd")
        self.assertTrue(inv["provenance"]["insufficient_history"])  # 데이터 있어도 period 미충족
        self.assertEqual(len(inv["data"]), 2)                       # data 자체는 비어있지 않음

    def test_full_coverage_sufficient(self):
        """첫 row가 period 시작 부근 → insufficient=false (full coverage)."""
        self._add(
            _sdr("investing", "usd-krw", date(2026, 3, 10), 1370.0, "investing_observed_eod", "observed_rollup"),
            _sdr("investing", "usd-krw", date(2026, 6, 1), 1390.0, "investing_observed_eod", "observed_rollup"),
        )
        inv = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"]
                   if s["id"] == "investing.usd")
        self.assertFalse(inv["provenance"]["insufficient_history"])  # 2026-03-10 ≈ 3m start(2026-03-08)

    def test_out_of_range_excluded(self):
        """period range 밖 row는 제외 (3m = 90일)."""
        self._add(
            _sdr("investing", "usd-krw", date(2025, 1, 1), 1300.0,   # 3m 범위 밖
                 "investing_observed_eod", "observed_rollup"),
            _sdr("investing", "usd-krw", date(2026, 6, 1), 1390.0,   # 범위 안
                 "investing_observed_eod", "observed_rollup"),
        )
        inv = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"]
                   if s["id"] == "investing.usd")
        self.assertEqual(len(inv["data"]), 1)  # 2026-06-01만
        self.assertEqual(inv["data"][0]["rate"], 1390.0)


if __name__ == "__main__":
    unittest.main()
