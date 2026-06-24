"""fanout step 4 S5a/S6 — GET /admin/api/fx-shadow-counts endpoint smoke (route + auth + shape + never-crash).

counter 로직은 test_fx_alert_shadow가 검증 — 여기선 route(path/auth/await) + 200 + shape +
would_fire/legacy_match(tuple key→list) JSON 직렬화 + shadow_stats 반영만. conftest.py가 firebase
stub + DATABASE_URL=sqlite를 import 전에 설정. (C7-a test_atomic_write_outcomes_endpoint 패턴 mirror)
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app import crud
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
        crud._fx_legacy_match_counts.clear()

    def tearDown(self):
        fx_alert_shadow._reset_fx_shadow_state()
        crud._fx_legacy_match_counts.clear()

    def test_returns_200_with_shape_empty(self):
        resp = self.client.get("/admin/api/fx-shadow-counts")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        for key in ("shadow_enabled", "would_fire", "total", "legacy_match", "shadow_stats"):
            self.assertIn(key, body)
        self.assertEqual(body["would_fire"], [])  # 빈 상태
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["legacy_match"], [])
        self.assertIsInstance(body["shadow_enabled"], bool)
        # shadow_stats 5 counter 키 존재
        for k in ("batch_seen", "settings_loaded", "matched_candidates",
                  "refetch_skipped_triggered", "would_send"):
            self.assertIn(k, body["shadow_stats"])

    def test_reflects_counts_and_baseline(self):
        # would_fire(matched, tuple key) + legacy_match baseline + shadow_stats 직렬화 검증
        fx_alert_shadow._fx_would_fire_counts[("kb", "usd-krw")] = 2
        fx_alert_shadow._fx_would_fire_counts[("investing", "jpy-krw")] = 1
        fx_alert_shadow._fx_shadow_stats["matched_candidates"] = 3
        fx_alert_shadow._fx_shadow_stats["refetch_skipped_triggered"] = 3
        crud._fx_legacy_match_counts[("kb", "usd-krw")] = 2

        resp = self.client.get("/admin/api/fx-shadow-counts")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 3)
        wf = {(r["source"], r["asset"]): r["count"] for r in body["would_fire"]}
        self.assertEqual(wf[("kb", "usd-krw")], 2)
        self.assertEqual(wf[("investing", "jpy-krw")], 1)
        # legacy baseline (bank/currency 키)
        lm = {(r["bank"], r["currency"]): r["count"] for r in body["legacy_match"]}
        self.assertEqual(lm[("kb", "usd-krw")], 2)
        # shadow_stats — matched(diagnostic) vs would_send(race 진단) 분리
        self.assertEqual(body["shadow_stats"]["matched_candidates"], 3)
        self.assertEqual(body["shadow_stats"]["refetch_skipped_triggered"], 3)


if __name__ == "__main__":
    unittest.main()
