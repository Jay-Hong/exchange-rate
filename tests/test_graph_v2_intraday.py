"""Graph API v2 테더 1d (10min closed-bucket precompute) — builder/catalog 테스트.

conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
검증 (codex 최소 기준):
  - builder가 11 series 반환 (§4 line 54).
  - 진행 중 10분 버킷 제외 (_trim_in_progress closed-bucket 경계).
  - source_rates change-only 데이터에서 carry-forward (빈 버킷=직전 close).
  - build_catalog: 테더 1d만 11 series, usd 등 다른 탭은 1d 미노출 (장기 5 series와 미혼합).
  - 1d 본문 조회 시작 = carry-in 경계 (정렬 시작 ~ now-24h 사이 행 누락 회귀).
"""
import unittest
from unittest.mock import MagicMock, call, patch
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import models
from app.admin.graph_cache import build_dxy_graph_series, build_graph_series, fetch_last_before
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
    cache_key_1d,
    precompute_intraday_1d,
)

models.Base.metadata.create_all(engine)



# ADR-038 — 이 모듈의 계약 테스트는 KRX 노출(게이트 오픈) 전제로 작성됨.
# 게이트 닫힘(G2/G3 off) 동작은 tests/test_krx_entitlement_gate.py에서 별도 검증.
# ADR-039 §6.1(2026-07-26): krx.* 포함 여부는 이제 **호출자가 넘기는 `krx_visible`**이 정한다
# (default False). 아래 테스트가 승인 사용자 구성을 기대하는 곳은 `krx_visible=True`를 명시한다.
# 미승인 응답의 기본 동작은 tests/test_graph_v2_krx_exposure.py에서 검증한다.
_KRX_GATES_OPEN = patch.multiple("app.config",
                                 KRX_FUTURES_ENABLED=True,
                                 KRX_CLIENT_DISTRIBUTION_ENABLED=True)


def setUpModule():
    _KRX_GATES_OPEN.start()


def tearDownModule():
    _KRX_GATES_OPEN.stop()


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

        payload = build_tab_1d_payload("tether", krx_visible=True)
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
        """전 탭 1d catalog가 §3/§4 계약 구성과 일치 — 테더 11 / usd 11(krx) / jpy·eur 9.
        usd: 8 banks(Citi 제외) + investing + dxy(dxy_futures는 테더 전용).
        jpy/eur: 8 banks + investing — DXY 계열 미노출(§9:521).

        krx 포함 구성은 `krx_visible=True`(= per-user 게이트 land 후 entitled 사용자)에서 검증한다 —
        미승인 사용자의 krx 제외 구성은 tests/test_graph_v2_krx_exposure.py (ADR-039 §6.1)."""
        catalog = build_catalog(krx_visible=True)
        tabs = {t["id"]: t for t in catalog["tabs"]}

        tether = tabs["tether"]
        self.assertEqual(len(tether["periods"]["1d"]["all_series"]), 11)
        self.assertEqual(set(tether["periods"]["1d"]["all_series"]), set(TETHER_1D_ALL_SERIES))
        # 장기는 1d와 분리 (5 series, 거래소는 Bithumb 대표만)
        self.assertNotEqual(len(tether["periods"]["3m"]["all_series"]), 11)
        self.assertNotIn("1d", catalog["supported_periods"])  # 전역은 MVP 유지(1d=tab-specific)

        # usd 1d: 인베스팅 → krx(ADR-038 D4 ②, 시세 행 순서 일치) → 8 banks(Citi 없음) → dxy
        # (dxy_futures는 테더 전용). krx는 default OFF.
        self.assertEqual(tabs["usd"]["periods"]["1d"]["all_series"], [
            "investing.usd", "krx.usd-krw-futures", "kb.usd", "hana.usd", "shinhan.usd",
            "woori.usd", "ibk.usd", "nh.usd", "sc.usd", "bs.usd", "dxy",
        ])
        self.assertNotIn("krx.usd-krw-futures",
                         tabs["usd"]["periods"]["1d"]["default_visible_series"])
        # default = 인베스팅 + 하나만 (사용자 지정 2026-07-03 — dxy/kb 기본 OFF, 소스多 최소 시작)
        self.assertEqual(tabs["usd"]["periods"]["1d"]["default_visible_series"],
                         ["investing.usd", "hana.usd"])

        # jpy/eur 1d: 9 series — DXY 계열 미노출 + Citi 없음, default = 인베스팅+하나
        for tab_id in ("jpy", "eur"):
            all_series = tabs[tab_id]["periods"]["1d"]["all_series"]
            self.assertEqual(len(all_series), 9)
            self.assertFalse(any("dxy" in s for s in all_series), f"{tab_id} 1d에 DXY 계열 미노출")
            self.assertFalse(any(s.startswith("citi.") for s in all_series))
            self.assertIn(f"investing.{tab_id}", all_series)
            self.assertIn(f"kb.{tab_id}", all_series)
            self.assertEqual(tabs[tab_id]["periods"]["1d"]["default_visible_series"],
                             [f"investing.{tab_id}", f"hana.{tab_id}"])
        # jpy/eur 장기(3m)는 investing+hana 2개 유지 (1d와 분리)
        self.assertEqual(len(tabs["jpy"]["periods"]["3m"]["all_series"]), 2)


class TestPrecomputeSuperset(unittest.TestCase):

    def test_cron_builds_every_tab_as_krx_superset(self):
        """공용 1d cron 캐시는 승인 사용자에 맞춰 KRX 포함 payload를 굽는다."""
        from app.graph_v2_intraday import CACHE_TTL_SECONDS, INTRADAY_TABS

        redis_client = MagicMock()

        def payload(tab, *, krx_visible):
            self.assertTrue(krx_visible)
            return {"tab": tab, "series": [{"id": f"series.{tab}"}]}

        with patch("redis.from_url", return_value=redis_client), \
             patch("app.graph_v2_intraday.build_tab_1d_payload", side_effect=payload) as build:
            precompute_intraday_1d()

        self.assertEqual(
            build.call_args_list,
            [call(tab, krx_visible=True) for tab in INTRADAY_TABS],
        )
        written = {args[0]: args[1:] for args, _kwargs in redis_client.setex.call_args_list}
        self.assertEqual(set(written), {cache_key_1d(tab) for tab in INTRADAY_TABS})
        self.assertTrue(all(ttl == CACHE_TTL_SECONDS for ttl, _payload in written.values()))
        redis_client.close.assert_called_once_with()

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
        payload = build_tab_1d_payload("usd", krx_visible=True)
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


def _utc(*kst_args):
    """KST 시각 → DB 저장 형식(naive UTC)."""
    return datetime(*kst_args, tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)


class TestOneDayBodyStartsAtCarryBoundary(unittest.TestCase):
    """1d 본문 조회는 carry-in 경계(align(now-24h))에서 시작해야 한다.

    회귀(2026-09-20 신한 1d 오른쪽 끝): 본문은 now-24h부터, carry-in은 align(now-24h) 이하만 읽어
    (align(now-24h), now-24h) 사이 행이 양쪽에서 빠졌다. 마지막 값이 그 구간에 있는 소스(주말 은행)는
    다음 10분 경계까지 더 오래된 값으로 그려졌다 — 진행 중 봉 seed가 15초마다 이 상태로 계산됨.
    값은 격리 fixture용이다(9/19 19:22:32 마지막 행 값만 당시 신한 값과 같게 둠).
    """

    # 9/19(토) 신한 마지막 행 19:22:32. 그 이전 행은 fixture용 다른 값.
    _OLDER = {"usd-krw": 1392.50, "jpy-krw": 887.10, "eur-krw": 1598.00}
    _LAST = {"usd-krw": 1390.00, "jpy-krw": 886.02, "eur-krw": 1596.27}
    # 9/20 재현 시각: precompute(:12) / 틈 시작 / 틈 중간 / 틈 끝 / 다음 경계
    _NOWS = [(19, 20, 12), (19, 22, 33), (19, 25, 0), (19, 29, 59), (19, 30, 0)]

    def setUp(self):
        for tbl in (models.SourceRate, models.BankExchangeRate, models.InvestingExchangeRate, models.MarketIndexRate):
            db = SessionLocal()
            db.query(tbl).delete()
            db.commit()
            db.close()

    tearDown = setUp

    def _add(self, *rows):
        db = SessionLocal()
        db.add_all(rows)
        db.commit()
        db.close()

    def _add_shinhan(self, values_by_kst):
        rows = []
        for kst_args, values in values_by_kst:
            for currency, rate in values.items():
                rows.append(models.BankExchangeRate(bank="shinhan", currency=currency, rate=rate,
                                                    timestamp=_utc(*kst_args)))
        self._add(*rows)

    def _seed_weekend_shinhan(self):
        self._add_shinhan([((2026, 9, 19, 18, 50, 0), self._OLDER),
                           ((2026, 9, 19, 19, 22, 32), self._LAST)])

    def test_fx_series_keeps_last_row_across_gap(self):
        """재현 5개 시각 × 3통화 — 모든 버킷 close가 마지막 값, 마지막 버킷 = 진행 중 봉."""
        self._seed_weekend_shinhan()
        for hms in self._NOWS:
            now_kst = datetime(2026, 9, 20, *hms, tzinfo=KST)
            for currency, expected in self._LAST.items():
                with self.subTest(now=hms, currency=currency):
                    series, latest_ts = build_graph_series("shinhan", currency, now_kst)
                    self.assertTrue(series)
                    self.assertEqual({p[3] for p in series}, {expected})
                    self.assertEqual(latest_ts, _bucket_align(int(now_kst.timestamp())))

    def test_in_progress_seed_and_closed_payload_keep_last_row(self):
        """증상 경로 — 진행 중 봉 seed와 closed payload 마지막 점이 같은 마지막 값."""
        self._seed_weekend_shinhan()
        for hms in self._NOWS:
            now_kst = datetime(2026, 9, 20, *hms, tzinfo=KST)
            for tab, currency in (("usd", "usd-krw"), ("jpy", "jpy-krw"), ("eur", "eur-krw")):
                expected = self._LAST[currency]
                with self.subTest(now=hms, tab=tab):
                    seed = build_tab_1d_in_progress(tab, now_kst=now_kst)[f"shinhan.{tab}"]
                    self.assertEqual((seed["high"], seed["low"], seed["close"]),
                                     (expected, expected, expected))
                    payload = build_tab_1d_payload(tab, now_kst=now_kst)
                    shinhan = next(s for s in payload["series"] if s["id"] == f"shinhan.{tab}")
                    self.assertEqual(shinhan["data"][-1]["rate"], expected)

    def test_row_exactly_at_bucket_start_overlaps_carry_in_harmlessly(self):
        """정렬 경계와 같은 시각의 행 — carry-in(<=)과 본문(>=)이 함께 잡아도 첫 버킷 OHLC는 본문 값만.

        ORM 저장값('…:00.000000')은 SQLite 문자열 비교에서 carry-in에 안 잡히므로,
        초 단위 문자열로 직접 넣어 PostgreSQL과 같은 겹침을 만든다.
        """
        db = SessionLocal()
        for kst_args, rate in (((2026, 9, 19, 18, 50, 0), 1392.50),
                               ((2026, 9, 19, 19, 20, 0), 1389.00),
                               ((2026, 9, 19, 19, 21, 0), 1391.00)):
            db.execute(text("INSERT INTO bank_exchange_rates (bank, currency, rate, timestamp) "
                            "VALUES ('shinhan', 'usd-krw', :rate, :ts)"),
                       {"rate": rate, "ts": _utc(*kst_args).strftime("%Y-%m-%d %H:%M:%S")})
        db.commit()
        db.close()

        bucket_start = int(datetime(2026, 9, 19, 19, 20, tzinfo=KST).timestamp())
        self.assertEqual(fetch_last_before("shinhan", "usd-krw", bucket_start), [bucket_start, 1389.00])
        series, _ = build_graph_series("shinhan", "usd-krw", datetime(2026, 9, 20, 19, 25, tzinfo=KST))
        self.assertEqual(series[0], [bucket_start, 1391.00, 1389.00, 1391.00])
        self.assertEqual({p[3] for p in series}, {1391.00})

    def test_row_exactly_at_window_start(self):
        """now-24h와 같은 시각의 행 — 수정 전에도 포함되던 경계, 그대로 유지."""
        self._seed_weekend_shinhan()
        series, _ = build_graph_series("shinhan", "usd-krw", datetime(2026, 9, 20, 19, 22, 32, tzinfo=KST))
        self.assertEqual({p[3] for p in series}, {1390.00})

    def test_first_bucket_high_low_include_rows_before_window_start(self):
        """첫 버킷은 정렬 시작부터 집계 — now-24h 이전 행도 high/low/close에 반영."""
        self._add_shinhan([((2026, 9, 19, 18, 50, 0), {"usd-krw": 1392.50}),
                           ((2026, 9, 19, 19, 21, 0), {"usd-krw": 1391.00}),
                           ((2026, 9, 19, 19, 22, 32), {"usd-krw": 1390.00})])
        series, _ = build_graph_series("shinhan", "usd-krw", datetime(2026, 9, 20, 19, 25, tzinfo=KST))
        bucket_start = int(datetime(2026, 9, 19, 19, 20, tzinfo=KST).timestamp())
        self.assertEqual(series[0], [bucket_start, 1391.00, 1390.00, 1390.00])

    def test_precompute_second_delay_keeps_row_after_boundary(self):
        """cron이 경계 몇 초 뒤 돌 때 — 경계 직후 행(19:30:01)이 closed payload에서 빠지지 않음."""
        self._add_shinhan([((2026, 9, 19, 18, 50, 0), {"usd-krw": 1392.50}),
                           ((2026, 9, 19, 19, 30, 1), {"usd-krw": 1388.50})])
        payload = build_tab_1d_payload("usd", now_kst=datetime(2026, 9, 20, 19, 30, 12, tzinfo=KST))
        shinhan = next(s for s in payload["series"] if s["id"] == "shinhan.usd")
        self.assertEqual({p["rate"] for p in shinhan["data"]}, {1388.50})

    def test_carry_in_only_and_empty(self):
        """윈도우 안 행 없음 → 이전 값 carry / 데이터 0 → 빈 series (기존 동작 유지)."""
        now_kst = datetime(2026, 9, 20, 19, 25, tzinfo=KST)
        self.assertEqual(build_graph_series("shinhan", "usd-krw", now_kst), ([], 0))
        self._add_shinhan([((2026, 9, 19, 18, 50, 0), {"usd-krw": 1392.50})])
        series, _ = build_graph_series("shinhan", "usd-krw", now_kst)
        self.assertEqual({p[3] for p in series}, {1392.50})

    def test_source_and_market_index_readers_keep_last_row_across_gap(self):
        """같은 경계 패턴의 거래소/KRX(source_rates)·DXY 선물 reader도 틈 행을 유지."""
        self._add(
            models.SourceRate(source="upbit", asset="usdt-krw", rate=1480.0, timestamp=_utc(2026, 9, 19, 18, 50, 0)),
            models.SourceRate(source="upbit", asset="usdt-krw", rate=1479.0, timestamp=_utc(2026, 9, 19, 19, 22, 32)),
            models.MarketIndexRate(instrument="dxy_futures", source="investing", rate=99.5,
                                   timestamp=_utc(2026, 9, 19, 18, 50, 0), granularity="realtime"),
            models.MarketIndexRate(instrument="dxy_futures", source="investing", rate=99.4,
                                   timestamp=_utc(2026, 9, 19, 19, 22, 32), granularity="realtime"),
        )
        now_kst = datetime(2026, 9, 20, 19, 25, tzinfo=KST)
        self.assertEqual({p[3] for p in _build_source_series_1d("upbit", "usdt-krw", 2, now_kst)}, {1479.0})
        self.assertEqual({p[3] for p in _build_market_index_series_1d("dxy_futures", 3, now_kst)}, {99.4})

    def test_dxy_reader_has_no_gap(self):
        """DXY 현물 reader는 본문/carry-in 모두 now-24h 기준이라 틈이 없음 — 수정 대상 아님을 고정."""
        self._add(
            models.MarketIndexRate(instrument="dxy", source="investing", rate=99.5,
                                   timestamp=_utc(2026, 9, 19, 18, 50, 0), granularity="realtime"),
            models.MarketIndexRate(instrument="dxy", source="investing", rate=99.4,
                                   timestamp=_utc(2026, 9, 19, 19, 22, 32), granularity="realtime"),
        )
        series, _ = build_dxy_graph_series(datetime(2026, 9, 20, 19, 25, tzinfo=KST))
        self.assertEqual({p[3] for p in series}, {99.4})


if __name__ == "__main__":
    unittest.main(verbosity=2)
