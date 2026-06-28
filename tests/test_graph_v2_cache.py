"""Graph API v2 read-through 캐시 테스트 — /api/v2/graph/tab 캐시 동작.

conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
테스트 환경엔 Redis 없으므로 redis_cache.get/set을 AsyncMock으로 patch,
build_tab 호출 여부로 hit/miss를 검증한다.

검증(codex 잠금 4 + unknown):
  - miss(get None) → build_tab 호출 + set 호출(key/TTL 확인)
  - hit(유효 JSON) → build_tab 미호출 + 캐시값 반환
  - corrupt JSON → miss로 처리, build_tab rebuild (자연 복구)
  - unsupported period(1d) 400 → 캐시 path 미진입
  - unknown tab 404 → 캐시 path 미진입
"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import models
from app.database import engine
from app.main import app

models.Base.metadata.create_all(engine)


class TestGraphV2Cache(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # context manager 미사용 → lifespan(scheduler) 미진입
        cls.client = TestClient(app)

    def test_cache_miss_builds_and_sets(self):
        """miss(get None) → build_tab 호출 + set(key/TTL) 호출."""
        fake = {"tab": "tether", "period": "3m", "series": [], "metadata": {}}
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2.build_tab", return_value=fake) as mbuild:
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=3m")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_called_once()
        mset.assert_awaited_once()
        args, kwargs = mset.await_args
        self.assertEqual(args[0], "graph_v2:tab:tether:3m")
        self.assertEqual(kwargs.get("ex"), 1800)  # 3m TTL

    def test_cache_hit_skips_build(self):
        """hit(유효 JSON) → build_tab 미호출 + set 미호출 + 캐시값 반환."""
        cached = {"tab": "tether", "period": "3m", "series": [], "metadata": {"cached": True}}
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=json.dumps(cached))), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2.build_tab") as mbuild:
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=3m")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_not_called()
        mset.assert_not_awaited()
        self.assertEqual(r.json()["metadata"].get("cached"), True)

    def test_corrupt_cache_falls_back_to_build(self):
        """corrupt JSON → miss로 처리, build_tab rebuild + set(자연 복구, delete 안 함)."""
        fake = {"tab": "tether", "period": "1w", "series": [], "metadata": {}}
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value="{not valid json")), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2.build_tab", return_value=fake) as mbuild:
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=1w")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_called_once()
        mset.assert_awaited_once()
        args, kwargs = mset.await_args
        self.assertEqual(kwargs.get("ex"), 300)  # 1w TTL

    def test_unsupported_period_bypasses_cache(self):
        """400(1d) → 캐시 path 미진입 (get/set 미호출)."""
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)) as mget, \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset:
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=1d")

        self.assertEqual(r.status_code, 400)
        mget.assert_not_awaited()
        mset.assert_not_awaited()

    def test_unknown_tab_bypasses_cache(self):
        """404(unknown tab) → 캐시 path 미진입."""
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)) as mget, \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset:
            r = self.client.get("/api/v2/graph/tab?tab=__nope__&period=3m")

        self.assertEqual(r.status_code, 404)
        mget.assert_not_awaited()
        mset.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
