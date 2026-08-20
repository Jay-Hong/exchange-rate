"""Authenticated entitlement response exposes the server-authoritative premium bit."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import main
from app.subscription import PremiumStatus


class TestEntitlementsEndpoint(unittest.IsolatedAsyncioTestCase):
    async def _get(
        self,
        status: PremiumStatus,
        *,
        krx_visible: bool,
        fresh_premium: bool = False,
    ):
        request = MagicMock()
        db = MagicMock()
        cached = AsyncMock(return_value=status)
        fresh = AsyncMock(return_value=status)
        with patch(
            "app.main.verify_firebase_token",
            new=AsyncMock(return_value="user-1"),
        ), patch(
            "app.main.verify_premium_status",
            new=cached,
        ), patch(
            "app.main.verify_premium_status_fresh",
            new=fresh,
        ), patch(
            "app.main.entitlements.compute_krx_visible",
            return_value=krx_visible,
        ) as compute:
            response = await main.get_entitlements(
                request,
                db,
                fresh_premium=fresh_premium,
            )
        return response, compute, db, cached, fresh

    async def test_active_is_reported_even_when_krx_is_hidden(self):
        response, compute, db, cached, fresh = await self._get(
            PremiumStatus.ACTIVE,
            krx_visible=False,
        )

        self.assertTrue(response.premium_active)
        self.assertFalse(response.krx_visible)
        self.assertFalse(response.premium_pending)
        compute.assert_called_once_with(db, "user-1", premium_active=True)
        cached.assert_awaited_once_with("user-1")
        fresh.assert_not_awaited()

    async def test_inactive_is_stable_nonpremium(self):
        response, compute, db, _, _ = await self._get(
            PremiumStatus.INACTIVE,
            krx_visible=False,
        )

        self.assertFalse(response.premium_active)
        self.assertFalse(response.premium_pending)
        compute.assert_called_once_with(db, "user-1", premium_active=False)

    async def test_pending_is_not_misreported_as_active(self):
        response, compute, _, _, _ = await self._get(
            PremiumStatus.PENDING,
            krx_visible=False,
        )

        self.assertFalse(response.premium_active)
        self.assertTrue(response.premium_pending)
        self.assertEqual(response.retry_after_seconds, 5)
        compute.assert_not_called()

    async def test_fresh_recovery_bypasses_the_availability_cache(self):
        response, compute, db, cached, fresh = await self._get(
            PremiumStatus.ACTIVE,
            krx_visible=False,
            fresh_premium=True,
        )

        self.assertTrue(response.premium_active)
        compute.assert_called_once_with(db, "user-1", premium_active=True)
        fresh.assert_awaited_once_with("user-1")
        cached.assert_not_awaited()
