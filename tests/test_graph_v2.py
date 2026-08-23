"""Graph API v2 (Phase 2e MVP) 테스트 — app/graph_v2.py.

12종 (Codex/Claude 수렴):
  1. catalog exposes 3m/1y/1w (1d만 v1 realtime)
  2. supported 3m/1y/1w / unsupported 1d
  3. tab composition: usd/jpy/eur/tether
  4. Hana mixed provenance + per-point close_basis/source_method
  5. single close_basis provenance (per-point 없음)
  6. KRX per-point contract_code
  7. DXY market_index daily mapping
  8. insufficient_history edge
  9-12. 1w hourly: bucket_size 1h + KRX insufficient + Hana 실관측 gap 유지 + DXY hourly

build_tab(db, ...)은 db 주입이라 patch 불필요 — in-memory SQLite session 직접 전달.
"""
import unittest
from unittest.mock import patch
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.models import SourceDailyRate, SourceHourlyRate, MarketIndexRate
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


# ADR-038 — 이 모듈의 계약 테스트는 KRX 노출(게이트 오픈) 전제로 작성됨.
# 게이트 닫힘(G2/G3 off) 동작은 tests/test_krx_entitlement_gate.py에서 별도 검증.
# ADR-038 — 이 모듈의 계약 테스트는 KRX 노출(게이트 오픈) 전제로 작성됨.
# 게이트 닫힘(G2/G3 off) 동작은 tests/test_krx_entitlement_gate.py에서 별도 검증.
# ADR-039 §6.1(2026-07-26): krx.* 포함 여부는 이제 **호출자가 넘기는 `krx_visible`**이 정한다
# (default False). 아래 테스트가 승인 사용자 구성을 기대하는 곳은 `krx_visible=True`를 명시한다.
# 미승인 기본 동작은 tests/test_graph_v2_krx_exposure.py에서 검증한다.
_KRX_GATES_OPEN = patch.multiple("app.config",
                                 KRX_FUTURES_ENABLED=True,
                                 KRX_CLIENT_DISTRIBUTION_ENABLED=True)


def setUpModule():
    _KRX_GATES_OPEN.start()


def tearDownModule():
    _KRX_GATES_OPEN.stop()


class TestCatalog(unittest.TestCase):

    def test_catalog_3m_1y_1w(self):
        cat = G.build_catalog(krx_visible=True)
        self.assertEqual(cat["supported_periods"], ["3m", "1y", "1w"])  # 전역 MVP (1d=tab-specific)
        tabs = {t["id"]: t for t in cat["tabs"]}
        # 전 탭: 1d(10min intraday precompute) + 장기 노출 (FX 1d = Slice A 확장, §3:49-54)
        for tab in ("usd", "jpy", "eur", "tether"):
            self.assertEqual(set(tabs[tab]["periods"].keys()), {"1d", "3m", "1y", "1w"})

    def test_supported_periods(self):
        self.assertTrue(G.is_supported_period("3m"))
        self.assertTrue(G.is_supported_period("1y"))
        self.assertTrue(G.is_supported_period("1w"))   # 1w 지원 (hourly)
        self.assertFalse(G.is_supported_period("1d"))  # 1d만 v1 realtime → 미지원

    def test_tab_composition(self):
        tabs = {t["id"]: t for t in G.build_catalog(krx_visible=True)["tabs"]}
        self.assertEqual(set(tabs), {"usd", "jpy", "eur", "tether"})
        # ADR-038 D4 ② — usd 전 기간 krx 편입 (investing 다음, default OFF)
        self.assertEqual(tabs["usd"]["periods"]["3m"]["all_series"],
                         ["investing.usd", "krx.usd-krw-futures", "hana.usd", "dxy"])
        self.assertNotIn("krx.usd-krw-futures",
                         tabs["usd"]["periods"]["3m"]["default_visible_series"])
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
        # high/low 노출 (단일 소스 음영 밴드용) — close_only는 high=low=close
        self.assertEqual(inv["data"][1]["high"], 1390.0)
        self.assertEqual(inv["data"][1]["low"], 1390.0)

    def test_high_low_exposed_for_ohlc(self):
        """source_ohlc row의 high/low가 point에 노출 (단일 소스 음영, close와 구별)."""
        self._add(
            _sdr("investing", "usd-krw", date(2026, 6, 1), 1390.0,
                 "investing_observed_eod", "observed_rollup", high=1395.0, low=1385.0),
        )
        inv = next(s for s in self._usd_3m()["series"] if s["id"] == "investing.usd")
        self.assertEqual(inv["data"][0]["rate"], 1390.0)
        self.assertEqual(inv["data"][0]["high"], 1395.0)
        self.assertEqual(inv["data"][0]["low"], 1385.0)


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
        krx = next(s for s in G.build_tab(self.db, "tether", "3m", today_kst=TODAY,
                                           krx_visible=True)["series"]
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


class TestCarryIn(_Base):
    """carry_in — window 시작 직전 실관측 seed (ADR-039 slice 1, 좌측 gap 채움)."""

    KST = ZoneInfo("Asia/Seoul")

    def _daily_obs(self, d):
        return datetime.combine(d, time(0, 0), tzinfo=self.KST).isoformat()

    def _shr(self, source, asset, ts_kst, close):
        from app.models import SourceHourlyRate
        return SourceHourlyRate(source=source, asset=asset, bucket_ts_kst=ts_kst,
                                rate=close, high=close, low=close, close=close,
                                ohlc_quality="observed_rollup", close_basis="investing_observed_hourly",
                                source_method="observed_rollup")

    def test_daily_carry_in_present(self):
        start, _ = G.period_range("3m", TODAY)              # 2026-03-08
        prior = start - timedelta(days=2)
        self._add(
            _sdr("investing", "usd-krw", prior, 1380.0, "investing_observed_eod", "observed_rollup"),          # carry_in
            _sdr("investing", "usd-krw", start + timedelta(days=1), 1390.0, "investing_observed_eod", "observed_rollup"),
        )
        inv = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"] if s["id"] == "investing.usd")
        self.assertEqual(inv["carry_in"], {"rate": 1380.0, "observed_at": self._daily_obs(prior)})

    def test_hourly_carry_in_present(self):
        start, _ = G.period_range("1w", TODAY)              # 2026-05-30
        prior = datetime.combine(start - timedelta(days=1), time(10, 0))   # 이전날 10:00 (naive KST)
        self._add(
            self._shr("investing", "usd-krw", prior, 1360.0),                                                  # carry_in
            self._shr("investing", "usd-krw", datetime.combine(start, time(9, 0)), 1365.0),
        )
        inv = next(s for s in G.build_tab(self.db, "usd", "1w", today_kst=TODAY)["series"] if s["id"] == "investing.usd")
        self.assertEqual(inv["carry_in"], {"rate": 1360.0, "observed_at": prior.replace(tzinfo=self.KST).isoformat()})

    def test_carry_in_null_when_no_prior(self):
        start, _ = G.period_range("3m", TODAY)
        self._add(_sdr("investing", "usd-krw", start + timedelta(days=1), 1390.0, "investing_observed_eod", "observed_rollup"))
        inv = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"] if s["id"] == "investing.usd")
        self.assertIsNone(inv["carry_in"])

    def test_carry_in_strict_before_no_overlap_with_start_day(self):
        # start 당일 row + 그 이전 row → carry_in = 이전 row(≠ data[0]). strict '<' 잠금.
        start, _ = G.period_range("3m", TODAY)
        self._add(
            _sdr("investing", "usd-krw", start - timedelta(days=3), 1370.0, "investing_observed_eod", "observed_rollup"),  # carry_in
            _sdr("investing", "usd-krw", start, 1385.0, "investing_observed_eod", "observed_rollup"),                       # data[0]
        )
        inv = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"] if s["id"] == "investing.usd")
        self.assertEqual(inv["carry_in"]["rate"], 1370.0)
        self.assertEqual(inv["data"][0]["rate"], 1385.0)
        self.assertEqual(self._daily_obs(start - timedelta(days=3)), inv["carry_in"]["observed_at"])

    def test_carry_in_orthogonal_to_provenance(self):
        # carry_in 유무가 coverage/insufficient/data[]에 영향 없음.
        start, _ = G.period_range("3m", TODAY)
        self._add(_sdr("investing", "usd-krw", start + timedelta(days=1), 1390.0, "investing_observed_eod", "observed_rollup"))
        p_no = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"] if s["id"] == "investing.usd")["provenance"]
        self._add(_sdr("investing", "usd-krw", start - timedelta(days=2), 1380.0, "investing_observed_eod", "observed_rollup"))
        s2 = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"] if s["id"] == "investing.usd")
        self.assertIsNotNone(s2["carry_in"])
        self.assertEqual(s2["provenance"]["coverage_days"], p_no["coverage_days"])
        self.assertEqual(s2["provenance"]["insufficient_history"], p_no["insufficient_history"])
        self.assertEqual(len(s2["data"]), 1)   # carry_in은 data[]에 안 들어감

    def test_dxy_carry_in_present(self):
        # D10 override — DXY(market_index)도 carry_in (좌측 gap seed 회귀 방지).
        self._add(
            MarketIndexRate(instrument="dxy", source="investing", rate=98.0,
                            timestamp=datetime(2026, 3, 5, 1, 0), granularity="daily"),   # start(3/8) 이전 → carry_in
            MarketIndexRate(instrument="dxy", source="investing", rate=100.2,
                            timestamp=datetime(2026, 6, 1, 1, 0), granularity="daily"),
        )
        dxy = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"] if s["id"] == "dxy")
        self.assertIsNotNone(dxy["carry_in"])
        self.assertEqual(dxy["carry_in"]["rate"], 98.0)

    def test_dxy_carry_in_source_priority_investing_wins(self):
        # codex blocker fix — carry_in도 in-window reader와 동일 _rank(source priority). pre-window 같은 KST
        # date(3/5)에 investing + 더 늦은 yahoo → 최신 bucket 안에서 investing 우선(늦은 yahoo 아님).
        self._add(
            MarketIndexRate(instrument="dxy", source="investing", rate=98.0,
                            timestamp=datetime(2026, 3, 5, 1, 0), granularity="daily"),
            MarketIndexRate(instrument="dxy", source="yahoo", rate=55.5,
                            timestamp=datetime(2026, 3, 5, 5, 0), granularity="daily"),   # 더 늦지만 priority 낮음
            MarketIndexRate(instrument="dxy", source="investing", rate=100.2,
                            timestamp=datetime(2026, 6, 1, 1, 0), granularity="daily"),   # in-window
        )
        dxy = next(s for s in G.build_tab(self.db, "usd", "3m", today_kst=TODAY)["series"] if s["id"] == "dxy")
        self.assertIsNotNone(dxy["carry_in"])
        self.assertEqual(dxy["carry_in"]["rate"], 98.0, "yahoo 55.5(늦음) 아니라 investing 98.0")


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


# ---------------------------------------------------------------------------
# 9-12. 1w period — hourly reader (source_hourly_rates + market_index.hourly)
# ---------------------------------------------------------------------------

def _shr(source, asset, ts_kst, close, close_basis, source_method,
         ohlc_quality="observed_rollup", contract_code=None, high=None, low=None):
    """SourceHourlyRate 헬퍼 (bucket_ts_kst = KST naive 시 정각)."""
    return SourceHourlyRate(
        source=source, asset=asset, bucket_ts_kst=ts_kst,
        rate=close, high=high if high is not None else close,
        low=low if low is not None else close, close=close,
        ohlc_quality=ohlc_quality, close_basis=close_basis, source_method=source_method,
        contract_code=contract_code,
    )


class TestPeriod1wHourly(_Base):
    """1w = source_hourly_rates hourly reader (bucket_ts_kst 시 단위) + DXY market_index.hourly."""

    def test_1w_hourly_buckets_and_bucket_size(self):
        # TODAY=2026-06-06 → 1w start=2026-05-30. start+1(05-31) 첫 bucket → sufficient(2d tolerance).
        self._add(
            _shr("bithumb", "usdt-krw", datetime(2026, 5, 31, 10, 0), 1400.0,
                 "bithumb_observed_hourly", "bithumb_candlestick_api"),
            _shr("bithumb", "usdt-krw", datetime(2026, 5, 31, 11, 0), 1402.0,
                 "bithumb_observed_hourly", "bithumb_candlestick_api"),
        )
        tab = G.build_tab(self.db, "tether", "1w", today_kst=TODAY)
        self.assertEqual(tab["metadata"]["bucket_size"], "1h")            # hourly granularity
        bith = next(s for s in tab["series"] if s["id"] == "bithumb.usdt-krw")
        self.assertEqual(len(bith["data"]), 2)
        self.assertEqual(bith["data"][0]["rate"], 1400.0)
        self.assertTrue(bith["data"][0]["ts"].startswith("2026-05-31T10:00:00"))  # 시 단위 ts
        self.assertTrue(bith["data"][0]["ts"].endswith("+09:00"))                 # KST
        self.assertFalse(bith["provenance"]["insufficient_history"])    # start+1 ≤ 2d

    def test_1w_krx_no_hourly_insufficient(self):
        # KRX는 source_hourly_rates 미적재 → 빈 series → insufficient_history (자연 처리)
        krx = next(s for s in G.build_tab(self.db, "tether", "1w", today_kst=TODAY,
                                           krx_visible=True)["series"]
                   if s["id"] == "krx.usd-krw-futures")
        self.assertEqual(krx["data"], [])
        self.assertTrue(krx["provenance"]["insufficient_history"])

    def test_1w_hana_actual_buckets_no_carry_forward(self):
        # Hana 1w = 실관측 bucket만, gap 유지 (carry-forward/step render 미적용). close_only mixed 허용.
        self._add(
            _shr("hana", "usd-krw", datetime(2026, 6, 5, 10, 0), 1380.0,
                 "hana_observed_hourly", "observed_rollup"),
            _shr("hana", "usd-krw", datetime(2026, 6, 5, 12, 0), 1382.0,
                 "hana_observed_hourly", "observed_rollup", ohlc_quality="close_only"),
        )
        hana = next(s for s in G.build_tab(self.db, "usd", "1w", today_kst=TODAY)["series"]
                    if s["id"] == "hana.usd")
        self.assertEqual(len(hana["data"]), 2)   # 11:00 gap 안 채움 (실관측만)
        self.assertEqual([p["ts"][11:16] for p in hana["data"]], ["10:00", "12:00"])

    def test_1w_dxy_hourly(self):
        # DXY 1w = market_index granularity=hourly (daily는 hourly reader가 무시)
        self._add(
            MarketIndexRate(instrument="dxy", source="investing", rate=99.5,
                            timestamp=datetime(2026, 6, 5, 1, 0), granularity="hourly"),   # KST 10:00
            MarketIndexRate(instrument="dxy", source="investing", rate=100.0,
                            timestamp=datetime(2026, 6, 5, 2, 0), granularity="hourly"),   # KST 11:00
            MarketIndexRate(instrument="dxy", source="investing", rate=88.8,
                            timestamp=datetime(2026, 6, 5, 1, 0), granularity="daily"),    # 무시돼야
        )
        dxy = next(s for s in G.build_tab(self.db, "usd", "1w", today_kst=TODAY)["series"]
                   if s["id"] == "dxy")
        self.assertEqual(len(dxy["data"]), 2)            # hourly 2개 (daily 무시)
        self.assertEqual(dxy["data"][0]["rate"], 99.5)
        self.assertTrue(dxy["data"][0]["ts"].startswith("2026-06-05T10:00:00"))  # KST 시각

    def test_1w_insufficient_tolerance_2d(self):
        # 1w tolerance=2d (daily 7d와 분리, Codex finding). start=2026-05-30.
        # start+1(주말성 지연) → sufficient / start+6(partial coverage) → insufficient.
        self._add(
            _shr("hana", "usd-krw", datetime(2026, 5, 31, 10, 0), 1380.0,
                 "hana_observed_hourly", "observed_rollup"),               # usd 탭: start+1 → sufficient
            _shr("bithumb", "usdt-krw", datetime(2026, 6, 5, 10, 0), 1400.0,
                 "bithumb_observed_hourly", "bithumb_candlestick_api"),    # tether 탭: start+6 → insufficient
        )
        hana = next(s for s in G.build_tab(self.db, "usd", "1w", today_kst=TODAY)["series"]
                    if s["id"] == "hana.usd")
        self.assertFalse(hana["provenance"]["insufficient_history"])   # start+1 ≤ 2d (주말 시작 허용)
        bith = next(s for s in G.build_tab(self.db, "tether", "1w", today_kst=TODAY)["series"]
                    if s["id"] == "bithumb.usdt-krw")
        self.assertTrue(bith["provenance"]["insufficient_history"])    # start+6 > 2d (partial coverage)
        self.assertEqual(len(bith["data"]), 1)                         # partial이어도 data 반환


class TestPeriodDomain(unittest.TestCase):
    """serve-time X축 domain 정책 + 프리미엄 attach (ADR-039 그래프 X축 통일)."""

    KST = ZoneInfo("Asia/Seoul")

    def test_1d_rolling_24h(self):
        anchor = datetime(2026, 7, 19, 10, 50, tzinfo=self.KST)
        d = G.period_domain("1d", anchor)
        self.assertEqual(d["live_domain_mode"], "rolling")
        self.assertEqual(d["domain_start_at"], "2026-07-18T10:50:00+09:00")   # anchor-24h
        self.assertEqual(d["domain_end_at"], "2026-07-19T10:50:00+09:00")

    def test_long_fixed_start_period_boundary(self):
        anchor = datetime(2026, 7, 19, 10, 50, tzinfo=self.KST)
        cases = {   # start = (KST 날짜 - N) 00:00 KST
            "1w": "2026-07-12T00:00:00+09:00",
            "3m": "2026-04-20T00:00:00+09:00",
            "1y": "2025-07-19T00:00:00+09:00",
        }
        for period, expected_start in cases.items():
            d = G.period_domain(period, anchor)
            self.assertEqual(d["live_domain_mode"], "fixed_start", period)
            self.assertEqual(d["domain_start_at"], expected_start, period)
            self.assertEqual(d["domain_end_at"], "2026-07-19T10:50:00+09:00", period)

    def test_forces_plus9_from_utc_anchor(self):
        # anchor가 UTC여도 출력·날짜 경계는 +09:00(같은 순간) — 서버 출력 timestamp는 항상 KST.
        anchor_utc = datetime(2026, 7, 19, 1, 50, tzinfo=ZoneInfo("UTC"))   # = 10:50 KST
        d = G.period_domain("1w", anchor_utc)
        self.assertEqual(d["domain_start_at"], "2026-07-12T00:00:00+09:00")
        self.assertEqual(d["domain_end_at"], "2026-07-19T10:50:00+09:00")

    def test_naive_anchor_rejected(self):
        with self.assertRaises(ValueError):
            G.period_domain("1w", datetime(2026, 7, 19, 10, 50))   # naive → 계약상 거부

    def test_utcoffset_none_anchor_rejected(self):
        # tzinfo는 있으나 utcoffset()이 None인 degenerate도 Python 정의상 naive → 거부(조용한 로컬 해석 방지).
        from datetime import tzinfo as _tzinfo

        class _NoOffset(_tzinfo):
            def utcoffset(self, dt): return None
            def tzname(self, dt): return "X"

        with self.assertRaises(ValueError):
            G.period_domain("1w", datetime(2026, 7, 19, 10, 50, tzinfo=_NoOffset()))

    def test_previous_day_cache_gets_today_domain(self):
        # 전날 빌드된 캐시 payload도 serve 시점(오늘) anchor로 오늘 domain을 받는다(캐시 date-less 안전).
        yesterday_payload = {"metadata": {"range": {"start": "2026-07-11", "end": "2026-07-18"}, "bucket_size": "1h"}}
        today_anchor = datetime(2026, 7, 19, 9, 0, tzinfo=self.KST)
        out = G.attach_graph_v2_domain(yesterday_payload, G.period_domain("1w", today_anchor))
        self.assertEqual(out["metadata"]["domain_start_at"], "2026-07-12T00:00:00+09:00")   # 오늘-7
        self.assertEqual(out["metadata"]["domain_end_at"], "2026-07-19T09:00:00+09:00")

    def test_attach_premium_metadata_immutable(self):
        pay = {"metadata": {"range": {"start": "2026-07-12", "end": "2026-07-19"}}, "series": []}
        anchor = datetime(2026, 7, 19, 10, 50, tzinfo=self.KST)
        out = G.attach_graph_v2_domain(pay, G.period_domain("1w", anchor))
        self.assertNotIn("domain_start_at", pay["metadata"])          # 입력 불변
        self.assertEqual(out["metadata"]["range"], {"start": "2026-07-12", "end": "2026-07-19"})   # 기존 보존
        self.assertIn("domain_start_at", out["metadata"])             # domain 공존
        self.assertEqual(out["series"], [])

    def test_same_anchor_tab_independent_domain(self):
        # 같은 anchor면 탭 무관 동일 domain(빗썸 유무 등 데이터 커버리지에 X축이 흔들리지 않음 — 근본 fix).
        anchor = datetime(2026, 7, 19, 10, 50, tzinfo=self.KST)
        self.assertEqual(G.period_domain("1w", anchor), G.period_domain("1w", anchor))

    def test_domain_start_matches_data_window_for_same_anchor_date(self):
        # 자정 race fix(codex): premium endpoint가 build_tab(today_kst=anchor.date())를 넘겨 domain(period_domain)과
        # data window(period_range)가 같은 anchor 날짜를 쓴다. fixed_start 3종에서 domain_start_at.date() ==
        # period_range start여야 carry_in(window 시작 strict '<')이 domain frameStart보다도 이르러 backdating 없음.
        anchor = datetime(2026, 7, 19, 10, 50, tzinfo=self.KST)
        for period in ("1w", "3m", "1y"):
            d = G.period_domain(period, anchor)
            dom_start_date = datetime.fromisoformat(d["domain_start_at"]).date()
            win_start, _ = G.period_range(period, anchor.date())
            self.assertEqual(dom_start_date, win_start, period)


if __name__ == "__main__":
    unittest.main()
