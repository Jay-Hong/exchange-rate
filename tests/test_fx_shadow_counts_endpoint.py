"""fanout step 4 S5a — GET /admin/api/fx-shadow-counts endpoint smoke (route + auth + shape + never-crash).

counter 로직은 test_fx_alert_shadow가 검증 — 여기선 route(path/auth/await) + 200 + shape +
would_fire(tuple key→list) JSON 직렬화 반영만. conftest.py가 firebase stub + DATABASE_URL=sqlite를
import 전에 설정. (C7-a test_atomic_write_outcomes_endpoint 패턴 mirror)
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app, verify_admin
from app.notifications import fx_alert_shadow


class TestFxShadowCountsEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[verify_admin] = lambda: "admin"
        cls.client = TestClient(app)  # lifespan 미진입 (A1 패턴)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(verify_admin, None)

    def setUp(self):
        fx_alert_shadow._reset_fx_shadow_state()

    def tearDown(self):
        fx_alert_shadow._reset_fx_shadow_state()

    def test_returns_200_with_shape_empty(self):
        resp = self.client.get("/admin/api/fx-shadow-counts")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        for key in ("shadow_enabled", "would_fire", "total"):
            self.assertIn(key, body)
        self.assertEqual(body["would_fire"], [])  # 빈 상태
        self.assertEqual(body["total"], 0)
        self.assertIsInstance(body["shadow_enabled"], bool)

    def test_reflects_would_fire_counts(self):
        # _shadow_noop_sender가 counter 누적 (tuple key (bank,currency))
        fx_alert_shadow._shadow_noop_sender(["t1"], "t", "b", {"bank": "kb", "currency": "usd-krw"})
        fx_alert_shadow._shadow_noop_sender(["t1"], "t", "b", {"bank": "kb", "currency": "usd-krw"})
        fx_alert_shadow._shadow_noop_sender(["t1"], "t", "b", {"bank": "investing", "currency": "jpy-krw"})
        resp = self.client.get("/admin/api/fx-shadow-counts")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 3)
        # tuple key → {source, asset, count} list 직렬화 검증
        wf = {(r["source"], r["asset"]): r["count"] for r in body["would_fire"]}
        self.assertEqual(wf[("kb", "usd-krw")], 2)
        self.assertEqual(wf[("investing", "jpy-krw")], 1)


if __name__ == "__main__":
    unittest.main()
