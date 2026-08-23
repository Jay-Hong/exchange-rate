"""Graph API v2 endpoint 테스트 (Phase 2e MVP) — main.py thin wiring 검증.

main.py import harness는 tests/conftest.py가 처리 (firebase stub + DATABASE_URL=sqlite를
collection 시작 시 모든 import 전에 설정 — memory: project_main_py_helper_placement).
lifespan(scheduler/crawler)은 TestClient context manager 미사용으로 미진입.

검증: catalog 200 / tab unsupported 400(1d) / tab 1w 200(hourly) / unknown tab 404 / tab 3m 200(빈 DB→insufficient) / v1 무변경.
"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 이미 설정.
import app.main as main_module
from app import models
from app.database import engine
from app.main import app

models.Base.metadata.create_all(engine)  # 빈 테이블 (source_daily_rates / market_index_rates 등)


class TestGraphV2Endpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # context manager 미사용 → lifespan(scheduler) 미진입
        cls.client = TestClient(app)
        cls.access_patcher = patch(
            "app.main._resolve_graph_v2_krx_visible", new=AsyncMock(return_value=False)
        )
        cls.access_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.access_patcher.stop()

    def test_catalog_200(self):
        r = self.client.get("/api/v2/graph/catalog")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["supported_periods"], ["3m", "1y", "1w"])
        self.assertEqual({t["id"] for t in body["tabs"]}, {"usd", "jpy", "eur", "tether"})

    def test_tab_unsupported_period_400(self):
        # (usd 1d는 이제 intraday 지원 → 미지원 케이스는 임의 period로 검증)
        r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "5y"})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["error"], "unsupported_period")
        self.assertEqual(body["supported_periods"], ["3m", "1y", "1w"])
        self.assertEqual(body["fallback"]["type"], "legacy_graph_api")

    def test_usd_1d_200_intraday(self):
        """usd 1d — FX 탭 intraday 신규 지원. 빈 DB여도 200 + 10 series(8 banks+investing+dxy) + in_progress 키.
        (redis 없음 → cache-aside가 실제 build — build_tab_1d_payload 경로 e2e.)"""
        r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "1d"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["tab"], "usd")
        self.assertEqual(body["metadata"]["bucket_size"], "10min")
        ids = [s["id"] for s in body["series"]]
        self.assertEqual(len(ids), 10)
        self.assertNotIn("citi.usd", ids)
        self.assertNotIn("dxy_futures", ids)
        self.assertIn("in_progress", body)
        self.assertEqual(r.headers.get("cache-control"), "private, no-store")

    def test_tab_1w_200_hourly(self):
        # 1w 이제 지원 (hourly). 빈 DB → 200 + bucket_size 1h + insufficient.
        r = self.client.get("/api/v2/graph/tab", params={"tab": "tether", "period": "1w"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["period"], "1w")
        self.assertEqual(body["metadata"]["bucket_size"], "1h")
        for s in body["series"]:
            self.assertTrue(s["provenance"]["insufficient_history"])  # 빈 DB

    def test_tab_unknown_tab_404(self):
        r = self.client.get("/api/v2/graph/tab", params={"tab": "xxx", "period": "3m"})
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertEqual(body["error"], "unknown_tab")
        self.assertIn("usd", body["known_tabs"])

    def test_tab_3m_empty_db_insufficient(self):
        """빈 DB → 200 + series structure (전부 insufficient_history=true, data 빈 배열)."""
        r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["tab"], "usd")
        self.assertEqual(body["period"], "3m")
        self.assertEqual([s["id"] for s in body["series"]], ["investing.usd", "hana.usd", "dxy"])
        for s in body["series"]:
            self.assertTrue(s["provenance"]["insufficient_history"])
            self.assertEqual(s["data"], [])

    def test_v1_endpoint_unchanged(self):
        """v1 /api/graph/{currency} 변경 0 — invalid currency → 400 (구현 유지 확인)."""
        r = self.client.get("/api/graph/invalid-pair")
        self.assertEqual(r.status_code, 400)

    def test_domain_attached_to_metadata_fixed_start(self):
        """serve-time X축 domain(ADR-039)이 metadata에 부착(프리미엄 envelope). 3m=fixed_start, 출력 +09:00."""
        r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        md = r.json()["metadata"]
        self.assertEqual(md["live_domain_mode"], "fixed_start")
        self.assertTrue(md["domain_start_at"].endswith("T00:00:00+09:00"))   # 00:00 KST 날짜 경계
        self.assertTrue(md["domain_end_at"].endswith("+09:00"))

    def test_domain_1d_rolling(self):
        r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "1d"})
        self.assertEqual(r.status_code, 200)
        md = r.json()["metadata"]
        self.assertEqual(md["live_domain_mode"], "rolling")
        self.assertTrue(md["domain_start_at"].endswith("+09:00"))
        self.assertTrue(md["domain_end_at"].endswith("+09:00"))

    def test_same_period_cross_tab_same_domain(self):
        """근본 fix — 같은 기간이면 탭 무관 동일 X축 domain(빗썸 유무 등 데이터 커버리지에 흔들리지 않음)."""
        r_usd = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "1w"})
        r_teth = self.client.get("/api/v2/graph/tab", params={"tab": "tether", "period": "1w"})
        self.assertEqual(r_usd.status_code, 200)
        self.assertEqual(r_teth.status_code, 200)
        # fixed_start = KST 날짜 경계라 같은 날 두 탭 domain_start_at 동일(end는 now라 ms 차 → 미비교).
        self.assertEqual(r_usd.json()["metadata"]["domain_start_at"],
                         r_teth.json()["metadata"]["domain_start_at"])

    def test_non_dict_cache_rebuilds_not_500(self):
        # JSON-valid non-dict 캐시([]) → miss로 처리하고 rebuild(200) — attach가 mapping 전제라 crash(500) 방지.
        with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value="[]")):
            r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("domain_start_at", r.json()["metadata"])

    def test_cache_set_stores_no_domain(self):
        # Redis에 굽는 건 domain 없는 원본만(캐시 date-less라 자정 넘어 hit해도 stale domain 방지). set body에 domain 부재.
        captured = {}

        async def _capture_set(key, val, ex=None):
            captured["val"] = val

        with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=None)), \
             patch.object(main_module.redis_cache, "set", new=_capture_set):
            r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        cached_body = json.loads(captured["val"])
        self.assertNotIn("domain_start_at", cached_body["metadata"])   # 캐시 원본엔 domain 없음
        self.assertIn("domain_start_at", r.json()["metadata"])         # 응답엔 domain 있음


if __name__ == "__main__":
    unittest.main()
