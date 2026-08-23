"""GraphV2 Firebase+premium+KRX per-user 접근 매트릭스."""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app


class TestGraphV2AccessMatrix(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_anonymous_is_401_before_route_enumeration(self):
        async def reject(_request):
            raise HTTPException(status_code=401, detail="missing token")

        premium = AsyncMock(return_value=True)
        with patch("app.main.verify_firebase_token", new=reject), \
             patch("app.main.require_premium", new=premium):
            response = self.client.get(
                "/api/v2/graph/tab", params={"tab": "__hidden__", "period": "3m"}
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")
        premium.assert_not_awaited()

    def test_nonpremium_is_403_and_not_cached(self):
        async def reject(_uid, allow_empty):
            self.assertFalse(allow_empty)
            raise HTTPException(status_code=403, detail="premium required")

        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="free")), \
             patch("app.main.require_premium", new=reject):
            response = self.client.get("/api/v2/graph/catalog")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")

    def test_pending_preserves_retry_after_and_adds_no_store(self):
        async def pending(_uid, allow_empty):
            self.assertFalse(allow_empty)
            raise HTTPException(
                status_code=503,
                detail="pending",
                headers={"Retry-After": "5"},
            )

        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="pending")), \
             patch("app.main.require_premium", new=pending):
            response = self.client.get("/api/v2/graph/catalog")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("retry-after"), "5")
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")

    def test_transient_entitlement_db_failure_is_503(self):
        failure = OperationalError("SELECT 1", {}, Exception("connection lost"))
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="paid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
             patch("app.main.entitlements.krx_gates_open", return_value=True), \
             patch("app.main.entitlements.compute_krx_visible", side_effect=failure):
            response = self.client.get("/api/v2/graph/catalog")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")
        self.assertNotIn("retry-after", response.headers)

    def test_permanent_entitlement_db_failure_is_not_masked_as_503(self):
        original = type("DBFailure", (), {"sqlstate": "28P01"})()
        failure = OperationalError("SELECT 1", {}, original)
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="paid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
             patch("app.main.entitlements.krx_gates_open", return_value=True), \
             patch("app.main.entitlements.compute_krx_visible", side_effect=failure):
            with self.assertRaises(OperationalError):
                self.client.get("/api/v2/graph/catalog")

    def _catalog(self, *, gates_open: bool, krx_visible: bool):
        compute = MagicMock(return_value=krx_visible)
        with patch("app.main.verify_firebase_token", new=AsyncMock(return_value="paid")), \
             patch("app.main.require_premium", new=AsyncMock(return_value=True)), \
             patch("app.main.entitlements.krx_gates_open", return_value=gates_open), \
             patch("app.main.entitlements.compute_krx_visible", new=compute):
            response = self.client.get("/api/v2/graph/catalog")
        return response, compute

    def test_global_gate_closed_skips_g1_lookup_and_hides_krx(self):
        response, compute = self._catalog(gates_open=False, krx_visible=True)
        self.assertEqual(response.status_code, 200)
        compute.assert_not_called()
        self.assertNotIn("krx", json.dumps(response.json()).lower())

    def test_approved_premium_sees_krx_catalog(self):
        response, compute = self._catalog(gates_open=True, krx_visible=True)
        self.assertEqual(response.status_code, 200)
        compute.assert_called_once()
        self.assertIn("krx.usd-krw-futures", json.dumps(response.json()).lower())
        self.assertEqual(response.headers.get("cache-control"), "private, no-store")

    def test_unapproved_premium_cannot_enumerate_krx_catalog(self):
        response, compute = self._catalog(gates_open=True, krx_visible=False)
        self.assertEqual(response.status_code, 200)
        compute.assert_called_once()
        self.assertNotIn("krx", json.dumps(response.json()).lower())


if __name__ == "__main__":
    unittest.main()
