"""P1b C6-9a — GET /admin/api/atomic-cutover-status endpoint smoke (route wiring + never-crash + shape).

build_cutover_status_dict 로직은 test_atomic_cutover_status가 검증 — 여기선 route 배선(path/auth/await) +
always-200 + 4 block shape만. conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app, verify_admin


class TestAtomicCutoverStatusEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[verify_admin] = lambda: "admin"
        cls.client = TestClient(app)  # lifespan 미진입 (context manager 미사용, A1 패턴)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.pop(verify_admin, None)

    def test_returns_200_with_blocks(self):
        resp = self.client.get("/admin/api/atomic-cutover-status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # status는 ok/degraded 모두 valid (block read 결과에 의존 — Redis/DB state order-dependent라 robust하게).
        self.assertIn(body["status"], ("ok", "degraded"))
        for block in ("config", "cutover", "gate_shadow", "future"):
            self.assertIn(block, body)
        # never-crash 200 유지 + read_ok present(값은 공유 sqlite의 cutover table 존재 여부에 의존 —
        # order-dependent라 값 단정 회피. fail-closed read_ok=False는 unit test_read_fresh_fail_closed가 잠금).
        self.assertIn("cutover_state", body["cutover"])
        self.assertIn("read_ok", body["cutover"])
        # future 신호는 available:false (no fabrication)
        self.assertFalse(body["future"]["watermark_lag"]["available"])


if __name__ == "__main__":
    unittest.main()
