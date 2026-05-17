"""KRX close finalizer Stage 2 — Redis TTL captured flag helpers tests.

KRX_CLOSE_SNAPSHOT_PLAN §5.3 (2026-05-17). Best-effort race-prevention flag —
WS close grace path (KrxCloseWindowWriter, Stage 3)가 SET, REST fallback
(KrxCloseSnapshotController, Stage 4)이 GET 후 captured 시 skip.

Mock-only — sync redis client (`_get_sync_client`) mock으로 외부 Redis 의존성 0.

설계 핵심 (`insert_source_rate_unconditional`와 contract 다름):
    - 예외 격리 + False 반환 (best-effort)
    - GET 실패 시 False default → REST 호출 진행 (catastrophic backup 안전)
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import latest_rates_cache as cache


class TestSetKrxCloseCapturedFlag(unittest.TestCase):
    """set_krx_close_captured_flag — SETEX TTL 1h, 예외 격리."""

    def test_set_calls_setex_with_expected_key_and_ttl(self):
        """정상 호출 — client.setex(key, 3600, "1") 정확 인자 검증."""
        mock_client = MagicMock()
        with patch.object(cache, "_get_sync_client", return_value=mock_client):
            result = cache.set_krx_close_captured_flag("CF", "2026-05-19")
        self.assertTrue(result)
        mock_client.setex.assert_called_once_with(
            "close_captured:krx:CF:2026-05-19", 3600, "1",
        )

    def test_set_returns_false_when_client_none(self):
        """`_get_sync_client` returns None → False (Redis 미초기화)."""
        with patch.object(cache, "_get_sync_client", return_value=None):
            self.assertFalse(cache.set_krx_close_captured_flag("CF", "2026-05-19"))

    def test_set_returns_false_when_setex_raises(self):
        """SETEX 예외 → 격리 + False (best-effort)."""
        mock_client = MagicMock()
        mock_client.setex.side_effect = RuntimeError("redis down")
        with patch.object(cache, "_get_sync_client", return_value=mock_client):
            self.assertFalse(cache.set_krx_close_captured_flag("CM", "2026-05-19"))


class TestGetKrxCloseCapturedFlag(unittest.TestCase):
    """get_krx_close_captured_flag — best-effort GET, 예외 시 False default 안전."""

    def test_get_returns_true_when_key_exists(self):
        """client.get → b"1" → True."""
        mock_client = MagicMock()
        mock_client.get.return_value = b"1"
        with patch.object(cache, "_get_sync_client", return_value=mock_client):
            result = cache.get_krx_close_captured_flag("CF", "2026-05-19")
        self.assertTrue(result)
        mock_client.get.assert_called_once_with("close_captured:krx:CF:2026-05-19")

    def test_get_returns_false_when_key_absent(self):
        """client.get → None → False."""
        mock_client = MagicMock()
        mock_client.get.return_value = None
        with patch.object(cache, "_get_sync_client", return_value=mock_client):
            self.assertFalse(cache.get_krx_close_captured_flag("CM", "2026-05-19"))

    def test_get_returns_false_when_client_none(self):
        """`_get_sync_client` returns None → False."""
        with patch.object(cache, "_get_sync_client", return_value=None):
            self.assertFalse(cache.get_krx_close_captured_flag("CF", "2026-05-19"))

    def test_get_returns_false_when_get_raises(self):
        """GET 예외 → 격리 + False (catastrophic backup 안전 default).

        Redis 장애 시 False default → REST 호출 진행 (종가 누락 회피 우선).
        """
        mock_client = MagicMock()
        mock_client.get.side_effect = RuntimeError("redis down")
        with patch.object(cache, "_get_sync_client", return_value=mock_client):
            self.assertFalse(cache.get_krx_close_captured_flag("CF", "2026-05-19"))


class TestKrxCloseCapturedKeyFormat(unittest.TestCase):
    """key format / TTL 상수 검증 — operational/runbook 정합용."""

    def test_key_format_uses_session_and_kst_date(self):
        """key = `close_captured:krx:{session}:{kst_date}` 정확 patterns."""
        self.assertEqual(
            cache._krx_close_captured_key("CF", "2026-05-19"),
            "close_captured:krx:CF:2026-05-19",
        )
        self.assertEqual(
            cache._krx_close_captured_key("CM", "2026-05-20"),
            "close_captured:krx:CM:2026-05-20",
        )

    def test_ttl_is_one_hour(self):
        """TTL 3600초 (1h) — 다음날 자연 expire 보장."""
        self.assertEqual(cache._KRX_CLOSE_CAPTURED_TTL_SEC, 3600)


if __name__ == "__main__":
    unittest.main()
