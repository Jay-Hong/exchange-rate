"""/api/rates Redis-first (ADR-026) endpoint 테스트 — DB 커넥션 풀 고갈 격리.

배경: /api/rates가 5-커넥션 풀 고갈 시 30초 대기 → nginx `/api/` proxy_read_timeout 30s → 504
(2026-07-15 실측). Redis-first로 DB-free hit + Redis off/miss/실패/timeout 시에만 DB fallback.

conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정. TestClient(app)은 context
manager 미사용 → lifespan(scheduler/crawler) 미진입. Redis/DB는 patch로 주입(실 연결 불요).

검증(codex 목록): Redis hit=DB 미접촉 / Redis None→DB fallback 1회 / REDIS off→DB / DB 실패→503(빈 200 아님)
/ Redis timeout→DB fallback / Redis 오류→DB fallback / shape·parity.
"""
import asyncio
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import app


def _rate(currency, bank, rate, ts):
    return {"currency": currency, "bank": bank, "rate": rate, "timestamp": ts}


REDIS_RATES = [
    _rate("usd-krw", "kb", 1390.0, "2026-07-15T15:00:00+09:00"),
    _rate("usd-krw", "investing", 1389.5, "2026-07-15T15:00:01+09:00"),
]
DB_RATES = [_rate("usd-krw", "kb", 1391.0, "2026-07-15T15:00:02+09:00")]


class TestApiRatesRedisFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_redis_hit_does_not_touch_db(self):
        """Redis hit → DB helper 0회 호출 + shape/metadata 정상."""
        async def fake_redis():
            return (REDIS_RATES, None, {})
        db_helper = MagicMock(return_value=DB_RATES)
        with patch("app.main.REDIS_LATEST_ENABLED", True), \
             patch("app.main.fetch_rates_from_redis", fake_redis), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_helper):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["rates"]), 2)
        self.assertEqual(body["metadata"]["total_count"], 2)
        self.assertEqual(body["rates"][0]["bank"], "kb")
        db_helper.assert_not_called()

    def test_redis_miss_falls_back_to_db_once(self):
        """Redis None(miss) → DB fallback 정확히 1회."""
        async def fake_redis_miss():
            return (None, None, {})
        db_helper = MagicMock(return_value=DB_RATES)
        with patch("app.main.REDIS_LATEST_ENABLED", True), \
             patch("app.main.fetch_rates_from_redis", fake_redis_miss), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_helper):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["rates"]), 1)
        db_helper.assert_called_once()

    def test_redis_disabled_uses_db(self):
        """REDIS_LATEST_ENABLED=false → Redis 미시도, DB path 유지."""
        db_helper = MagicMock(return_value=DB_RATES)
        redis_mock = MagicMock()   # 호출되면 안 됨
        with patch("app.main.REDIS_LATEST_ENABLED", False), \
             patch("app.main.fetch_rates_from_redis", redis_mock), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_helper):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 200)
        db_helper.assert_called_once()
        redis_mock.assert_not_called()

    def test_db_failure_returns_503_not_empty_200(self):
        """Redis miss + DB 실패 → 503 (빈 200 아님 — 클라 캐시 fallback 보존, codex)."""
        async def fake_redis_miss():
            return (None, None, {})
        def db_raise():
            raise RuntimeError("pool timeout")
        with patch("app.main.REDIS_LATEST_ENABLED", True), \
             patch("app.main.fetch_rates_from_redis", fake_redis_miss), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_raise):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 503)

    def test_redis_timeout_falls_back_to_db(self):
        """Redis hang → wait_for timeout → DB fallback (엔드포인트 hang 방지)."""
        async def fake_redis_hang():
            await asyncio.sleep(10)
            return (REDIS_RATES, None, {})
        db_helper = MagicMock(return_value=DB_RATES)
        with patch("app.main.REDIS_LATEST_ENABLED", True), \
             patch("app.main._API_RATES_REDIS_TIMEOUT_S", 0.05), \
             patch("app.main.fetch_rates_from_redis", fake_redis_hang), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_helper):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 200)
        db_helper.assert_called_once()

    def test_redis_error_falls_back_to_db(self):
        """Redis 오류(circuit/연결) → DB fallback."""
        async def fake_redis_error():
            raise ConnectionError("redis down")
        db_helper = MagicMock(return_value=DB_RATES)
        with patch("app.main.REDIS_LATEST_ENABLED", True), \
             patch("app.main.fetch_rates_from_redis", fake_redis_error), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_helper):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 200)
        db_helper.assert_called_once()

    def test_empty_redis_falls_back_to_db(self):
        """Redis 빈 리스트([]) → 비정상/불완전 mirror로 보고 DB fallback (codex blocker).
        빈 200은 클라가 캐시를 빈 .connected로 덮으므로 금지. DB가 진짜 비면 그때 authoritative 빈 200."""
        async def fake_redis_empty():
            return ([], None, {})
        db_helper = MagicMock(return_value=DB_RATES)
        with patch("app.main.REDIS_LATEST_ENABLED", True), \
             patch("app.main.fetch_rates_from_redis", fake_redis_empty), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_helper):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["rates"]), 1)   # DB 결과
        db_helper.assert_called_once()   # 빈 리스트 → DB fallback

    def test_db_empty_is_authoritative_200(self):
        """Redis miss + DB도 빈 리스트 → authoritative 빈 200 (DB가 진짜 비어있으면 정상)."""
        async def fake_redis_miss():
            return (None, None, {})
        db_helper = MagicMock(return_value=[])
        with patch("app.main.REDIS_LATEST_ENABLED", True), \
             patch("app.main.fetch_rates_from_redis", fake_redis_miss), \
             patch("app.main._fetch_all_rates_flat_owning_session", db_helper):
            r = self.client.get("/api/rates")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["metadata"]["total_count"], 0)
        db_helper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
