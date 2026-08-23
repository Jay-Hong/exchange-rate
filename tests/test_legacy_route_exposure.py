"""Public legacy endpoints enforce the central FX allowlist before any DB read."""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


class TestLegacyRouteExposure(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def assert_hidden(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Currency pair not found"})
        body = response.text.lower()
        self.assertNotIn("krx", body)
        self.assertNotIn("futures", body)
        self.assertNotIn("topic", body)

    def test_investing_rejects_krx_before_db_lookup(self):
        reader = MagicMock(return_value={
            "currency": "usd-krw-futures", "bank": "investing",
            "rate": 1400.0, "timestamp": "2026-08-23T12:00:00+09:00",
        })
        with patch("app.main.crud.select_a_latest_investing_rate_from_db", reader):
            response = self.client.get("/api/investing/usd-krw-futures")
        self.assert_hidden(response)
        reader.assert_not_called()

    def test_banks_reject_krx_before_db_lookup(self):
        reader = MagicMock(return_value=[{
            "currency": "usd-krw-futures", "bank": "hana",
            "rate": 1400.0, "timestamp": "2026-08-23T12:00:00+09:00",
        }])
        with patch("app.main.crud.select_latest_bank_rates_from_db", reader):
            response = self.client.get("/api/banks/usd-krw-futures")
        self.assert_hidden(response)
        reader.assert_not_called()

    def test_rates_rejects_krx_without_topic_hint_or_db_lookup(self):
        reader = MagicMock(return_value=[{
            "currency": "usd-krw-futures", "bank": "krx",
            "rate": 1400.0, "timestamp": "2026-08-23T12:00:00+09:00",
        }])
        with patch("app.main.crud.get_rates_by_currency", reader):
            response = self.client.get("/api/rates/usd-krw-futures")
        self.assert_hidden(response)
        reader.assert_not_called()

    def test_unknown_asset_is_fail_closed_before_db_lookup(self):
        reader = MagicMock(return_value=[{
            "currency": "usd-krw-options", "bank": "future-source",
            "rate": 1.0, "timestamp": "2026-08-23T12:00:00+09:00",
        }])
        with patch("app.main.crud.get_rates_by_currency", reader):
            response = self.client.get("/api/rates/usd-krw-options")
        self.assert_hidden(response)
        reader.assert_not_called()

    def test_public_usdt_migration_hint_remains_410(self):
        response = self.client.get("/api/rates/usdt-krw")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["detail"], {
            "error": "legacy_rate_removed",
            "currency": "usdt-krw",
            "use_topic": "usdt:krw",
        })

    def test_registered_fx_asset_still_reaches_reader(self):
        row = {
            "currency": "usd-krw", "bank": "investing",
            "rate": 1400.0, "timestamp": "2026-08-23T12:00:00+09:00",
        }
        reader = MagicMock(return_value=row)
        with patch("app.main.crud.select_a_latest_investing_rate_from_db", reader):
            response = self.client.get("/api/investing/usd-krw")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), row)
        reader.assert_called_once()

    def test_public_db_failure_does_not_echo_internal_exception(self):
        reader = MagicMock(side_effect=RuntimeError("postgres://secret-host/internal"))
        with patch("app.main.crud.get_rates_by_currency", reader):
            response = self.client.get("/api/rates/usd-krw")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Rates temporarily unavailable"})
        self.assertNotIn("secret-host", response.text)


if __name__ == "__main__":
    unittest.main()
