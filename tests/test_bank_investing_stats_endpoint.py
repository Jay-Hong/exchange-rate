"""admin /admin/api/bank-investing-redis-stats endpoint 계약 (item 4).

route registration + read-only(reset route 부재)를 route introspection으로 잠근다.
반환 shape는 test_bank_investing_redis_stats.py(모듈 단위)가 이미 잠금 — 여기선
endpoint 존재와 "reset route 없음" 설계 의도만 회귀 방지한다.

firebase stub + sqlite override는 tests/conftest.py가 collection 시점에 처리 →
app.main import 안전 (TestClient/auth 불요, app.routes만 검사).
"""
from __future__ import annotations

import unittest

_ROUTE = "/admin/api/bank-investing-redis-stats"


class TestBankInvestingStatsEndpointContract(unittest.TestCase):

    def setUp(self):
        from app.main import app
        self.routes = list(app.routes)
        self.paths = {getattr(r, "path", None) for r in self.routes}

    def test_get_route_registered_with_get_method(self):
        # path 존재뿐 아니라 GET 메서드까지 잠금 (실수로 POST로 바뀌면 실패).
        methods = set()
        for r in self.routes:
            if getattr(r, "path", None) == _ROUTE:
                methods |= set(getattr(r, "methods", None) or [])
        self.assertIn("GET", methods)

    def test_no_reset_route(self):
        # reset route는 의도적으로 없음 (운영 실수 회피, reset_stats는 테스트 전용).
        self.assertNotIn(f"{_ROUTE}/reset", self.paths)
        self.assertFalse(
            any(p and p.startswith(_ROUTE) and p.endswith("/reset") for p in self.paths)
        )


if __name__ == "__main__":
    unittest.main()
