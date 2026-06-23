"""C7-a — GET /admin/api/atomic-write-outcomes endpoint smoke (route 배선 + auth + shape + never-crash).

accessor 로직은 test_atomic_write_outcomes가 검증 — 여기선 route(path/auth/await) + 200 + shape만.
conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app import crud
from app.main import app, verify_admin


class TestAtomicWriteOutcomesEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[verify_admin] = lambda: "admin"
        cls.client = TestClient(app)  # lifespan 미진입 (A1 패턴)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(verify_admin, None)

    def setUp(self):
        crud._atomic_write_outcome_counts.clear()

    def tearDown(self):
        crud._atomic_write_outcome_counts.clear()

    def test_returns_200_with_outcome_shape(self):
        resp = self.client.get("/admin/api/atomic-write-outcomes")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "success")
        outcomes = body["outcomes"]
        for key in ("started_at", "process_local", "aggregate", "per_source", "health", "g3_ok"):
            self.assertIn(key, outcomes)
        self.assertEqual(outcomes["health"], "ok")  # 빈 상태
        self.assertTrue(outcomes["g3_ok"])

    def test_reflects_recorded_outcomes(self):
        class _S:
            def __init__(self, v):
                self.value = v

        class _O:
            def __init__(self, v, structural=False):
                self.state = _S(v)
                self.structural = structural

        crud._record_atomic_write_outcome("kb", _O("conflict"))
        resp = self.client.get("/admin/api/atomic-write-outcomes")
        self.assertEqual(resp.status_code, 200)
        outcomes = resp.json()["outcomes"]
        self.assertEqual(outcomes["health"], "critical")
        self.assertFalse(outcomes["g3_ok"])
        self.assertEqual(outcomes["aggregate"]["critical"]["conflict"], 1)


if __name__ == "__main__":
    unittest.main()
