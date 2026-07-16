"""ADR-039 Step 3 — /api/v2/free/snapshot endpoint wiring 테스트.

harness = conftest(firebase stub + sqlite). lifespan 미진입(context manager 미사용)이라 cron 미실행.
serve는 cron canonical만 반환(DB 재생성 안 함). canonical 없음(Redis/local 비어있음) → 503. Redis mock으로 canonical 주입 검증.
"""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.main as main_module
from app import models
from app.database import engine
from app.main import app

models.Base.metadata.create_all(engine)


class TestFreeSnapshotEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        main_module._free_snapshot_local.clear()   # 테스트 간 last-good 격리(순서 의존 방지)

    def test_auth_failure_propagates(self):
        # 인증 실패(401)면 핸들러가 데이터/검증보다 먼저 401 전파 — 무인증 데이터 유출 없음.
        # (실 verify_firebase_token은 test 환경에 google.auth 미설치라 exercise 불가 → 401 raise를 주입해
        #  게이트가 데이터보다 앞선다는 계약만 결정적으로 검증.)
        from fastapi import HTTPException

        async def _raise_401(request, check_revoked=False):
            raise HTTPException(status_code=401, detail="unauthorized")

        with patch("app.main.verify_firebase_token", new=_raise_401):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 401)

    def test_unknown_free_tab_404(self):
        # jpy는 아직 무료 미허용(MVP=usd) → 404 (auth 통과 후)
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "jpy", "period": "3m"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "unknown_tab")
        self.assertIn("usd", r.json()["free_tabs"])

    def test_unsupported_period_400(self):
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "5y"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "unsupported_period")

    def test_no_canonical_returns_503_never_fabricates(self):
        # cron canonical 없음(Redis/local 비어있음) → serve는 DB 최신값으로 fabricate하지 않고 503.
        # build를 raise로 패치해도 503이면 serve가 DB build를 호출하지 않음(무료=1시간 고정 불변식)을 잠근다.
        from app import free_snapshot

        def _boom(*a, **k):
            raise AssertionError("serve가 DB build를 호출하면 안 됨 (1시간 고정 불변식)")

        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(free_snapshot, "build_free_snapshot_payload", _boom):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_serves_redis_canonical_verbatim(self):
        # serve는 cron canonical을 그대로 반환(DB rebuild 아님) → 같은 시간대 DB가 바뀌어도 응답 불변.
        canonical = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:00:00+09:00",
            "generated_at": "2026-07-17T14:20:03+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), canonical)   # verbatim — rebuild 아님
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_redis_success_then_miss_serves_last_good(self):
        # Redis 성공 → process-local last-good 보존 → 이후 Redis miss(None)여도 마지막 canonical 반환.
        # keep-last-good(availability) + 1시간 고정(값 불변) 동시 확인.
        canonical = {
            "tab": "usd", "period": "3m",
            "as_of": "2026-07-17T14:00:00+09:00", "generated_at": "2026-07-17T14:20:03+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [{"bucket_date": "2026-07-16", "rate": 1385.0}]}],
                      "bucket_size": "1d", "range": {"start": "2026-04-18", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
                r1 = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
            self.assertEqual(r1.status_code, 200)   # Redis 성공 → local seeded
            with patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=None)):
                r2 = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r2.status_code, 200)          # Redis miss여도 last-good
        self.assertEqual(r2.json(), canonical)         # 마지막 canonical 그대로(값 불변)

    def test_malformed_redis_no_local_returns_503(self):
        # 필수 필드 누락(as_of/generated_at 없음) canonical → validate 거부 → local 없음 → 503 (오염 200 방지).
        bad = {
            "tab": "usd", "period": "3m",
            "rate": {"asset": "usd-krw", "entries": [{"currency": "usd-krw"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [1]}], "range": {}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(bad))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"], "snapshot_unavailable")

    def test_serves_1d_canonical(self):
        # 1d가 이제 무료 지원 period → 400 아님. 10min bucket canonical이 validate 통과 + 서빙.
        canonical = {
            "tab": "usd", "period": "1d",
            "as_of": "2026-07-17T14:00:00+09:00", "generated_at": "2026-07-17T14:20:03+09:00",
            "rate": {"asset": "usd-krw", "entries": [
                {"bank": "kb", "currency": "usd-krw", "rate": 1385.0, "timestamp": "2026-07-17T14:19:00+09:00"}]},
            "graph": {"series": [{"id": "investing.usd", "data": [[123, 1.0, 2.0, 1385.0]]}],
                      "bucket_size": "10min", "range": {"start": "2026-07-16", "end": "2026-07-17"}},
        }
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")), \
             patch.object(main_module.redis_cache, "get", new=AsyncMock(return_value=json.dumps(canonical))):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "1d"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["graph"]["bucket_size"], "10min")


if __name__ == "__main__":
    unittest.main()
