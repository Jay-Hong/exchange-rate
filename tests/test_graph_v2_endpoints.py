"""Graph API v2 endpoint 테스트 (Phase 2e MVP) — main.py thin wiring 검증.

main.py import harness는 tests/conftest.py가 처리 (firebase stub + DATABASE_URL=sqlite를
collection 시작 시 모든 import 전에 설정 — memory: project_main_py_helper_placement).
lifespan(scheduler/crawler)은 TestClient context manager 미사용으로 미진입.

검증: catalog 200 / tab unsupported 400(1d) / tab 1w 200(hourly) / unknown tab 404 / tab 3m 200(빈 DB→insufficient) / v1 무변경.
"""
import unittest

from fastapi.testclient import TestClient

# conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 이미 설정.
from app import models
from app.database import engine
from app.main import app

models.Base.metadata.create_all(engine)  # 빈 테이블 (source_daily_rates / market_index_rates 등)


class TestGraphV2Endpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # context manager 미사용 → lifespan(scheduler) 미진입
        cls.client = TestClient(app)

    def test_catalog_200(self):
        r = self.client.get("/api/v2/graph/catalog")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["supported_periods"], ["3m", "1y", "1w"])
        self.assertEqual({t["id"] for t in body["tabs"]}, {"usd", "jpy", "eur", "tether"})

    def test_tab_unsupported_period_400(self):
        r = self.client.get("/api/v2/graph/tab", params={"tab": "usd", "period": "1d"})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["error"], "unsupported_period")
        self.assertEqual(body["supported_periods"], ["3m", "1y", "1w"])
        self.assertEqual(body["fallback"]["type"], "legacy_graph_api")

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


if __name__ == "__main__":
    unittest.main()
