"""RevenueCat webhook is fail-closed and authenticates before processing payloads."""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


class TestRevenueCatWebhookAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_missing_authorization_is_401(self):
        response = self.client.post("/webhooks/revenuecat", json={"event": {}})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Missing Authorization"})

    def test_missing_server_secret_fails_closed(self):
        with patch("app.webhooks.REVENUECAT_WEBHOOK_AUTH_KEY", ""):
            response = self.client.post(
                "/webhooks/revenuecat",
                headers={"Authorization": "presented"},
                json={"event": {}},
            )
        self.assertEqual(response.status_code, 401)

    def test_wrong_authorization_is_401(self):
        with patch("app.webhooks.REVENUECAT_WEBHOOK_AUTH_KEY", "expected"):
            response = self.client.post(
                "/webhooks/revenuecat",
                headers={"Authorization": "wrong"},
                json={"event": {}},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Invalid Authorization"})

    def test_valid_authorization_invalidates_all_event_user_ids(self):
        invalidate = MagicMock()
        payload = {
            "event": {
                "type": "TRANSFER",
                "app_user_id": "current",
                "original_app_user_id": "original",
                "aliases": ["alias"],
                "transferred_from": ["from-user"],
                "transferred_to": ["to-user"],
            }
        }
        with patch("app.webhooks.REVENUECAT_WEBHOOK_AUTH_KEY", "secret"), \
             patch("app.webhooks.invalidate_user_cache", invalidate):
            response = self.client.post(
                "/webhooks/revenuecat",
                headers={"Authorization": "secret"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(
            {call.args[0] for call in invalidate.call_args_list},
            {"current", "original", "alias", "from-user", "to-user"},
        )


if __name__ == "__main__":
    unittest.main()
