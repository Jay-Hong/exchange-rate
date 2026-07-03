"""Graph API v2 테더 1d (10min closed-bucket precompute) — builder/catalog 테스트.

conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
검증 (codex 최소 기준):
  - builder가 11 series 반환 (§4 line 54).
  - 진행 중 10분 버킷 제외 (_trim_in_progress closed-bucket 경계).
  - source_rates change-only 데이터에서 carry-forward (빈 버킷=직전 close).
  - build_catalog: 테더 1d만 11 series, usd 등 다른 탭은 1d 미노출 (장기 5 series와 미혼합).
"""
import unittest
from datetime import datetime, timedelta, timezone

from app import models
from app.database import SessionLocal, engine
from app.graph_v2 import build_catalog
from app.graph_v2_intraday import (
    KST,
    TAB_1D_ALL_SERIES,
    TAB_1D_DEFAULT_VISIBLE,
    TETHER_1D_ALL_SERIES,
    _bucket_align,
    _build_market_index_series_1d,
    _build_source_series_1d,
    _trim_in_progress,
    build_tab_1d_in_progress,
    build_tab_1d_payload,
)

models.Base.metadata.create_all(engine)


class TestBucketHelpers(unittest.TestCase):

    def test_bucket_align_floors_to_10min(self):
        # 600의 배수로 내림
        self.assertEqual(_bucket_align(1_700_000_523), 1_700_000_523 - (1_700_000_523 % 600))
        aligned = 1_700_000_400  # 600 배수
        self.assertEqual(_bucket_align(aligned), aligned)
        self.assertEqual(_bucket_align(aligned + 599), aligned)
        self.assertEqual(_bucket_align(aligned + 600), aligned + 600)

    def test_trim_in_progress_drops_current_bucket(self):
        """now=09:23 → 진행 중 버킷=09:20 제외, 마지막 완료봉=09:10 (10분봉 핵심)."""
        base = 1_700_000_400  # 600 배수 = 가상의 09:00
        b0900 = base
        b0910 = base + 600
        b0920 = base + 1200       # now=09:23이면 진행 중(09:20~09:29:59)
        series = [
            [b0900, 1490.0, 1489.0, 1490.0],
            [b0910, 1491.0, 1490.0, 1491.0],
            [b0920, 1492.0, 1491.0, 1492.0],
        ]
        # in_progress_start_ts = 09:20 → ts >= 09:20 drop
        trimmed = _trim_in_progress(series, in_progress_start_ts=b0920)
        self.assertEqual([p[0] for p in trimmed], [b0900, b0910])  # 09:20 제외, 09:10이 마지막
        # 경계: 빈 입력 / 전부 진행 중
        self.assertEqual(_trim_in_progress([], b0920), [])
        self.assertEqual(_trim_in_progress([[b0920, 1, 1, 1]], b0920), [])


class TestSourceSeriesCarryForward(unittest.TestCase):

    def setUp(self):
        # 격리: source_rates 비우기
        db = SessionLocal()
        db.query(models.SourceRate).delete()
        db.commit()
        db.close()

    def tearDown(self):
        db = SessionLocal()
        db.query(models.SourceRate).delete()
        db.commit()
        db.close()

    def test_carry_forward_fills_empty_buckets(self):
        """윈도우 시작 이전 1건만 있어도 모든 빈 10분 버킷이 직전 close로 채워짐(carry-forward) + dense."""
        now_kst = datetime(2026, 1, 1, 9, 23, tzinfo=KST)
        # 윈도우(now-24h) 시작 '이전'에 1건 → fetch_last_before가 prev_close로 잡아 전 구간 carry-forward
        before_window_utc = (now_kst - timedelta(hours=25)).astimezone(timezone.utc).replace(tzinfo=None)
        db = SessionLocal()
        db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1490.0, timestamp=before_window_utc))
        db.commit()
        db.close()

        series = _build_source_series_1d("upbit", "usdt-krw", 2, now_kst)
        self.assertTrue(series, "carry-forward로 빈 버킷도 채워져 series 비면 안 됨")
        # 전부 직전 close(1490) carry-forward
        self.assertTrue(all(p[3] == 1490.0 for p in series), "모든 버킷이 직전 close 유지")
        self.assertTrue(all(p[1] == p[2] == p[3] for p in series), "데이터 없는 버킷은 max=min=close")
        # dense: 연속 버킷 ts 간격 정확히 600
        diffs = [series[i + 1][0] - series[i][0] for i in range(len(series) - 1)]
        self.assertTrue(all(d == 600 for d in diffs), "carry-forward로 gap 없이 600초 간격")


class TestBuildTether1dPayload(unittest.TestCase):

    def setUp(self):
        for tbl in (models.SourceRate, models.BankExchangeRate, models.InvestingExchangeRate, models.MarketIndexRate):
            db = SessionLocal()
            db.query(tbl).delete()
            db.commit()
            db.close()

    def test_returns_11_series_with_ids(self):
        """§4 line 54: 11 series(거래소 5 + KRX + investing/KB/Hana + DXY + DXY_futures) 전부 포함."""
        # upbit 1건만 seed (나머지 series는 데이터 없어도 series 자체는 존재해야 함)
        recent_utc = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(tzinfo=None)
        db = SessionLocal()
        db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1490.0, timestamp=recent_utc))
        db.commit()
        db.close()

        payload = build_tab_1d_payload("tether")
        self.assertEqual(payload["tab"], "tether")
        self.assertEqual(payload["period"], "1d")
        self.assertEqual(payload["metadata"]["bucket_size"], "10min")
        ids = [s["id"] for s in payload["series"]]
        self.assertEqual(len(ids), 11)
        self.assertEqual(set(ids), set(TETHER_1D_ALL_SERIES))
        # seed한 upbit는 data 존재
        upbit = next(s for s in payload["series"] if s["id"] == "upbit.usdt-krw")
        self.assertTrue(upbit["data"], "seed한 upbit series는 data 있어야 함")
        # data point schema: ts/rate/source/high/low
        pt = upbit["data"][-1]
        self.assertIn("ts", pt)
        self.assertIn("rate", pt)
        self.assertIn("high", pt)
        self.assertIn("low", pt)
        self.assertEqual(pt["source"], "upbit")
        # 데이터 없는 series(예: gopax)도 존재 + insufficient_history=True
        gopax = next(s for s in payload["series"] if s["id"] == "gopax.usdt-krw")
        self.assertEqual(gopax["data"], [])
        self.assertTrue(gopax["provenance"]["insufficient_history"])


class TestMarketIndexFuturesSeries(unittest.TestCase):
    """dxy_futures 신규 reader — carry-forward + all-empty (codex 제안)."""

    def setUp(self):
        db = SessionLocal()
        db.query(models.MarketIndexRate).delete()
        db.commit()
        db.close()

    def tearDown(self):
        db = SessionLocal()
        db.query(models.MarketIndexRate).delete()
        db.commit()
        db.close()

    def test_all_empty_returns_empty(self):
        """데이터 0건 → [] (crash 없음)."""
        now_kst = datetime(2026, 1, 1, 9, 23, tzinfo=KST)
        self.assertEqual(_build_market_index_series_1d("dxy_futures", 3, now_kst), [])

    def test_carry_forward_from_before_window(self):
        """윈도우 이전 1건만 있어도 전 구간 carry-forward + dense."""
        now_kst = datetime(2026, 1, 1, 9, 23, tzinfo=KST)
        before_window_utc = (now_kst - timedelta(hours=25)).astimezone(timezone.utc).replace(tzinfo=None)
        db = SessionLocal()
        db.add(models.MarketIndexRate(
            instrument="dxy_futures", source="investing", rate=99.5,
            timestamp=before_window_utc, granularity="realtime",
        ))
        db.commit()
        db.close()

        series = _build_market_index_series_1d("dxy_futures", 3, now_kst)
        self.assertTrue(series)
        self.assertTrue(all(p[3] == 99.5 for p in series), "직전 close carry-forward")
        diffs = [series[i + 1][0] - series[i][0] for i in range(len(series) - 1)]
        self.assertTrue(all(d == 600 for d in diffs), "dense 600초 간격")


class TestCatalog1dIsolation(unittest.TestCase):

    def test_all_tabs_1d_series_match_contract(self):
        """전 탭 1d catalog가 §3/§4 계약 구성과 일치 — 테더 11 / usd 10 / jpy·eur 9.
        usd: 8 banks(Citi 제외) + investing + dxy(dxy_futures는 테더 전용).
        jpy/eur: 8 banks + investing — DXY 계열 미노출(§9:521)."""
        catalog = build_catalog()
        tabs = {t["id"]: t for t in catalog["tabs"]}

        tether = tabs["tether"]
        self.assertEqual(len(tether["periods"]["1d"]["all_series"]), 11)
        self.assertEqual(set(tether["periods"]["1d"]["all_series"]), set(TETHER_1D_ALL_SERIES))
        # 장기는 1d와 분리 (5 series, 거래소는 Bithumb 대표만)
        self.assertNotEqual(len(tether["periods"]["3m"]["all_series"]), 11)
        self.assertNotIn("1d", catalog["supported_periods"])  # 전역은 MVP 유지(1d=tab-specific)

        # usd 1d: §4:83 정확 목록 (순서 포함) — Citi 없음, dxy_futures 없음
        self.assertEqual(tabs["usd"]["periods"]["1d"]["all_series"], [
            "investing.usd", "bs.usd", "hana.usd", "ibk.usd", "kb.usd",
            "nh.usd", "sc.usd", "shinhan.usd", "woori.usd", "dxy",
        ])
        self.assertEqual(tabs["usd"]["periods"]["1d"]["default_visible_series"],
                         ["investing.usd", "kb.usd", "hana.usd", "dxy"])   # §4:82

        # jpy/eur 1d: 9 series — DXY 계열 미노출 + Citi 없음
        for tab_id in ("jpy", "eur"):
            all_series = tabs[tab_id]["periods"]["1d"]["all_series"]
            self.assertEqual(len(all_series), 9)
            self.assertFalse(any("dxy" in s for s in all_series), f"{tab_id} 1d에 DXY 계열 미노출")
            self.assertFalse(any(s.startswith("citi.") for s in all_series))
            self.assertIn(f"investing.{tab_id}", all_series)
            self.assertIn(f"kb.{tab_id}", all_series)
        # jpy/eur 장기(3m)는 investing+hana 2개 유지 (1d와 분리)
        self.assertEqual(len(tabs["jpy"]["periods"]["3m"]["all_series"]), 2)

    def test_default_visible_subset_of_all_series(self):
        """전 탭·전 period에서 default_visible ⊆ all_series (catalog 무결성)."""
        catalog = build_catalog()
        for tab in catalog["tabs"]:
            for period, cfg in tab["periods"].items():
                self.assertTrue(
                    set(cfg["default_visible_series"]) <= set(cfg["all_series"]),
                    f"{tab['id']}/{period}: default_visible이 all_series 밖",
                )

    def test_usd_1d_payload_builds_with_bank_series(self):
        """usd 1d payload — 10 series 조립 + 은행 kind=fx reader 경로 동작(빈 DB여도 series 존재)."""
        payload = build_tab_1d_payload("usd")
        self.assertEqual(payload["tab"], "usd")
        ids = [s["id"] for s in payload["series"]]
        self.assertEqual(ids, TAB_1D_ALL_SERIES["usd"])
        self.assertIn("_in_progress_start_ts", payload)   # stale-boundary rebuild 계약 승계


class TestBuildTether1dInProgress(unittest.TestCase):
    """진행 중(현재) 10분봉 seed — cold-open/resync 시 클라 현재 봉 partial 해소용."""

    def setUp(self):
        for tbl in (models.SourceRate, models.BankExchangeRate, models.InvestingExchangeRate, models.MarketIndexRate):
            db = SessionLocal()
            db.query(tbl).delete()
            db.commit()
            db.close()

    tearDown = setUp

    def _add_upbit(self, now_kst, hms_rates):
        db = SessionLocal()
        for (h, m, s), rate in hms_rates:
            ts_utc = datetime(2026, 1, 1, h, m, s, tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)
            db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=rate, timestamp=ts_utc))
        db.commit()
        db.close()

    def test_extracts_current_bucket_high_low_close(self):
        """현재 봉(09:20~09:30) 관측 3건 → seed high=max/low=min/close=last."""
        now_kst = datetime(2026, 1, 1, 9, 23, tzinfo=KST)   # 진행 중 봉 = 09:20
        self._add_upbit(now_kst, [((9, 20, 30), 1500.0), ((9, 21, 0), 1510.0), ((9, 22, 0), 1505.0)])
        ip = build_tab_1d_in_progress("tether", now_kst=now_kst)
        self.assertIn("upbit.usdt-krw", ip)
        seed = ip["upbit.usdt-krw"]
        self.assertEqual(seed["high"], 1510.0)
        self.assertEqual(seed["low"], 1500.0)
        self.assertEqual(seed["close"], 1505.0)   # 마지막 관측
        self.assertTrue(seed["bucket_start"].startswith("2026-01-01T09:20:00"))   # align(now)=09:20 KST
        self.assertTrue(seed["sampled_at"].startswith("2026-01-01T09:23:00"))

    def test_omitted_when_no_data(self):
        """데이터 0 → seed 생략(클라 client-only 누적 fallback)."""
        now_kst = datetime(2026, 1, 1, 9, 23, tzinfo=KST)
        ip = build_tab_1d_in_progress("tether", now_kst=now_kst)
        self.assertNotIn("upbit.usdt-krw", ip)

    def test_carry_forward_zero_width_when_only_prior_data(self):
        """현재 봉 관측 없고 이전 데이터만 → carry-forward zero-width(high==low==prev close). 변동 없음 표현."""
        now_kst = datetime(2026, 1, 1, 9, 23, tzinfo=KST)
        before = (now_kst - timedelta(hours=25)).astimezone(timezone.utc).replace(tzinfo=None)
        db = SessionLocal()
        db.add(models.SourceRate(source="upbit", asset="usdt-krw", rate=1490.0, timestamp=before))
        db.commit()
        db.close()
        ip = build_tab_1d_in_progress("tether", now_kst=now_kst)
        self.assertIn("upbit.usdt-krw", ip)
        seed = ip["upbit.usdt-krw"]
        self.assertEqual(seed["high"], seed["low"])   # zero-width(무변동)
        self.assertEqual(seed["close"], 1490.0)       # carry-forward


if __name__ == "__main__":
    unittest.main(verbosity=2)
