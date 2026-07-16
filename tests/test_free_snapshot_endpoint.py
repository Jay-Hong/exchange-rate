"""ADR-039 Step 3 — /api/v2/free/snapshot endpoint wiring 테스트.

harness = conftest(firebase stub + sqlite). lifespan 미진입(context manager 미사용)이라 cron 미실행.
redis 없음 → miss-rebuild가 빈 sqlite로 build → 200 (rate 빈 배열 / graph insufficient).
"""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import models
from app.database import engine
from app.main import app

models.Base.metadata.create_all(engine)


class TestFreeSnapshotEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

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

    def test_usd_3m_200_miss_rebuild(self):
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="uid")):
            r = self.client.get("/api/v2/free/snapshot", params={"tab": "usd", "period": "3m"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["tab"], "usd")
        self.assertEqual(body["period"], "3m")
        self.assertEqual(body["rate"]["asset"], "usd-krw")
        self.assertIn("as_of", body)
        self.assertIn("generated_at", body)
        # KRX series 절대 없음 (무료 보안 핵심)
        self.assertNotIn("krx.usd-krw-futures", [s["id"] for s in body["graph"]["series"]])
        self.assertEqual(r.headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
