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
        cls.access_patcher = patch(
            "app.main._resolve_graph_v2_krx_visible", new=AsyncMock(return_value=False)
        )
        cls.access_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.access_patcher.stop()

    def test_cache_miss_builds_and_sets(self):
        """miss(get None) → build_tab 호출 + set(key/TTL) 호출."""
        fake = {"tab": "tether", "period": "3m", "series": [], "metadata": {}}
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2.build_tab", return_value=fake) as mbuild:
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=3m")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_called_once()
        self.assertTrue(mbuild.call_args.kwargs["krx_visible"])
        mset.assert_awaited_once()
        args, kwargs = mset.await_args
        self.assertEqual(args[0], "graph_v2:superset:v1:tab:tether:3m")
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
        self.assertTrue(mbuild.call_args.kwargs["krx_visible"])
        mset.assert_awaited_once()
        args, kwargs = mset.await_args
        self.assertEqual(kwargs.get("ex"), 300)  # 1w TTL

    def test_unsupported_period_bypasses_cache(self):
        """400(미지원 period 5y) → read-through 캐시 path 미진입 (get/set 미호출).

        (1d는 이제 전 탭 intraday 지원 — usd 1d도 200. 미지원 케이스는 임의 period로 검증.)
        """
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)) as mget, \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset:
            r = self.client.get("/api/v2/graph/tab?tab=usd&period=5y")

        self.assertEqual(r.status_code, 400)
        mget.assert_not_awaited()
        mset.assert_not_awaited()

    def test_tether_1d_cache_hit_skips_build(self):
        """테더 1d hit(캐시 경계 fresh) — closed key + in_progress key 각각 유효 JSON → 둘 다 build 미호출 +
        set 미호출, 응답에 closed(cached) + additive in_progress seed 부착. (key-aware mock으로 계약 정확 검증.)
        `_in_progress_start_ts`가 현재 경계 이상(먼 미래값)이라 stale rebuild 미발동 = fresh cache 경로."""
        closed = {"tab": "tether", "period": "1d", "series": [], "metadata": {"cached": True},
                  "_in_progress_start_ts": 9999999999}   # 현재 경계 이상 = fresh (rebuild 안 함)
        seed = {"bithumb.usdt-krw": {"bucket_start": "b", "high": 2.0, "low": 1.0, "close": 1.5, "sampled_at": "s"}}

        def fake_get(key):
            if key == "graph_v2:superset:v1:tab:tether:1d":
                return json.dumps(closed)
            if key == "graph_v2:superset:v1:tab:tether:1d:in_progress":
                return json.dumps(seed)
            return None

        with patch("app.main.redis_cache.get", new=AsyncMock(side_effect=fake_get)), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2_intraday.build_tab_1d_payload") as mbuild, \
             patch("app.graph_v2_intraday.build_tab_1d_in_progress") as mseed:
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=1d")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_not_called()
        mseed.assert_not_called()
        mset.assert_not_awaited()
        body = r.json()
        self.assertEqual(body["metadata"].get("cached"), True)
        self.assertEqual(body["in_progress"], seed)   # additive seed 부착

    def test_tether_1d_stale_boundary_rebuilds_closed(self):
        """테더 1d hit이지만 캐시 경계 stale(_in_progress_start_ts=0 < 현재 경계) → 경계 통과 후 precompute(:12)
        전 창으로 판단, closed를 온디맨드 rebuild(build_tab_1d_payload 호출 + set) → cold-open ~12초 gap 제거.
        구 캐시(_in_progress_start_ts 부재)도 default 0이라 동일하게 rebuild(배포 직후 self-heal)."""
        stale_closed = {"tab": "tether", "period": "1d", "series": [], "metadata": {"cached": True},
                        "_in_progress_start_ts": 0}   # 현재 경계 미만 = stale
        rebuilt = {"tab": "tether", "period": "1d", "series": [], "metadata": {"rebuilt": True},
                   "_in_progress_start_ts": 9999999999}
        seed = {"upbit.usdt-krw": {"bucket_start": "b", "high": 2.0, "low": 1.0, "close": 1.5, "sampled_at": "s"}}

        def fake_get(key):
            if key == "graph_v2:superset:v1:tab:tether:1d":
                return json.dumps(stale_closed)
            if key == "graph_v2:superset:v1:tab:tether:1d:in_progress":
                return json.dumps(seed)
            return None

        with patch("app.main.redis_cache.get", new=AsyncMock(side_effect=fake_get)), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2_intraday.build_tab_1d_payload", return_value=rebuilt) as mbuild, \
             patch("app.graph_v2_intraday.build_tab_1d_in_progress", return_value=seed):
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=1d")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_called_once_with("tether", krx_visible=True)
        self.assertEqual(r.json()["metadata"].get("rebuilt"), True)   # 캐시 아닌 rebuild 값 반환
        # closed rebuild set(TTL 1200) 포함
        set_ttl_by_key = {c.args[0]: c.kwargs.get("ex") for c in mset.await_args_list}
        self.assertEqual(set_ttl_by_key.get("graph_v2:superset:v1:tab:tether:1d"), 1200)

    def test_usd_1d_cache_miss_rebuilds_with_per_tab_key(self):
        """usd 1d(FX 탭 intraday 신규 지원) miss → build_tab_1d_payload('usd') 호출 + per-tab 캐시 키
        (graph_v2:superset:v1:tab:usd:1d[,:in_progress]) SET — 테더와 키 분리 확인."""
        closed = {"tab": "usd", "period": "1d", "series": [], "metadata": {}}
        seed = {"kb.usd": {"bucket_start": "b", "high": 2.0, "low": 1.0, "close": 1.5, "sampled_at": "s"}}
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2_intraday.build_tab_1d_payload", return_value=closed) as mbuild, \
             patch("app.graph_v2_intraday.build_tab_1d_in_progress", return_value=seed) as mseed:
            r = self.client.get("/api/v2/graph/tab?tab=usd&period=1d")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_called_once_with("usd", krx_visible=True)
        mseed.assert_called_once_with("usd", krx_visible=True)
        set_ttl_by_key = {c.args[0]: c.kwargs.get("ex") for c in mset.await_args_list}
        self.assertEqual(set_ttl_by_key.get("graph_v2:superset:v1:tab:usd:1d"), 1200)
        self.assertEqual(set_ttl_by_key.get("graph_v2:superset:v1:tab:usd:1d:in_progress"), 15)
        self.assertEqual(r.json()["in_progress"], seed)

    def test_tether_1d_cache_miss_rebuilds_closed_and_in_progress(self):
        """테더 1d miss(둘 다 None) → closed rebuild(build_tab_1d_payload, set TTL 1200) +
        in_progress rebuild(build_tab_1d_in_progress, set in_progress key TTL 15). 응답에 seed 부착."""
        closed = {"tab": "tether", "period": "1d", "series": [], "metadata": {}}
        seed = {"upbit.usdt-krw": {"bucket_start": "b", "high": 2.0, "low": 1.0, "close": 1.5, "sampled_at": "s"}}
        with patch("app.main.redis_cache.get", new=AsyncMock(return_value=None)), \
             patch("app.main.redis_cache.set", new=AsyncMock()) as mset, \
             patch("app.graph_v2_intraday.build_tab_1d_payload", return_value=closed) as mbuild, \
             patch("app.graph_v2_intraday.build_tab_1d_in_progress", return_value=seed) as mseed:
            r = self.client.get("/api/v2/graph/tab?tab=tether&period=1d")

        self.assertEqual(r.status_code, 200)
        mbuild.assert_called_once_with("tether", krx_visible=True)
        mseed.assert_called_once_with("tether", krx_visible=True)
        self.assertEqual(mset.await_count, 2)   # closed + in_progress
        set_ttl_by_key = {c.args[0]: c.kwargs.get("ex") for c in mset.await_args_list}
        self.assertEqual(set_ttl_by_key.get("graph_v2:superset:v1:tab:tether:1d"), 1200)
        self.assertEqual(set_ttl_by_key.get("graph_v2:superset:v1:tab:tether:1d:in_progress"), 15)
        self.assertEqual(r.json()["in_progress"], seed)

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
