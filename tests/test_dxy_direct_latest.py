"""mirror-retirement Slice 1 — DXY direct latest writer.

검증:
- set_latest_dxy_rate_from_sync_job (latest_rates_cache): serialize_dxy_value 포맷으로 LATEST_DXY_KEY SET,
  client None / SET 예외 시 False(no-crash, best-effort).
- _emit_dxy_direct_latest (dxy_spot): flag off no-op / flag on get_latest_dxy_rate→writer / record None /
  예외 격리. mirror와 동일 함수(get_latest_dxy_rate) 사용 = byte-identical value 공존.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import app.latest_rates_cache as lrc
import app.crawlers.dxy_spot as dxy_spot


class TestDxyDirectWriter(unittest.TestCase):
    """set_latest_dxy_rate_from_sync_job — KRX bool writer 패턴(v1, telemetry 미부착)."""

    def test_writes_serialized_dxy_value(self):
        client = MagicMock()
        with patch.object(lrc, "_get_sync_client", return_value=client):
            ok = lrc.set_latest_dxy_rate_from_sync_job(
                104.52, "2026-03-10T14:30:00+09:00", "investing")
        self.assertTrue(ok)
        client.set.assert_called_once()
        key, value = client.set.call_args.args
        self.assertEqual(key, lrc.LATEST_DXY_KEY)
        d = json.loads(value)
        self.assertEqual(d["rate"], 104.52)
        self.assertEqual(d["timestamp"], "2026-03-10T14:30:00+09:00")
        self.assertEqual(d["source"], "investing")  # serialize_dxy_value 필수 필드
        self.assertIn("mirrored_at", d)  # freshness stamp

    def test_value_roundtrips_via_deserialize(self):
        # mirror read path(deserialize_dxy_value)가 파싱 가능해야 = 공존 안전
        client = MagicMock()
        with patch.object(lrc, "_get_sync_client", return_value=client):
            lrc.set_latest_dxy_rate_from_sync_job(99.1, "2026-03-10T14:30:00+09:00", "cnbc")
        _, value = client.set.call_args.args
        parsed = lrc.deserialize_dxy_value(value)
        self.assertIsNotNone(parsed)  # source 누락/naive mirrored_at 아님
        self.assertEqual(parsed["rate"], 99.1)
        self.assertEqual(parsed["source"], "cnbc")

    def test_client_none_returns_false(self):
        with patch.object(lrc, "_get_sync_client", return_value=None):
            self.assertFalse(
                lrc.set_latest_dxy_rate_from_sync_job(1.0, "t", "investing"))

    def test_set_exception_returns_false(self):
        client = MagicMock()
        client.set.side_effect = RuntimeError("redis down")
        with patch.object(lrc, "_get_sync_client", return_value=client):
            self.assertFalse(
                lrc.set_latest_dxy_rate_from_sync_job(1.0, "t", "investing"))


class TestEmitDxyDirectLatest(unittest.TestCase):
    """_emit_dxy_direct_latest — flag gate + get_latest_dxy_rate 재사용(mirror와 동일) + 격리."""

    def test_flag_off_noop(self):
        with patch("app.config.DXY_DIRECT_LATEST_ENABLED", False), \
             patch("app.crud.get_latest_dxy_rate") as mock_get:
            dxy_spot._emit_dxy_direct_latest(MagicMock())
        mock_get.assert_not_called()  # flag off = behavior-change-0 (DB read도 안 함)

    def test_flag_on_writes_from_get_latest(self):
        rec = {"rate": 104.5, "timestamp": "2026-03-10T14:30:00+09:00", "source": "cnbc"}
        with patch("app.config.DXY_DIRECT_LATEST_ENABLED", True), \
             patch("app.crud.get_latest_dxy_rate", return_value=rec), \
             patch("app.latest_rates_cache.set_latest_dxy_rate_from_sync_job") as mock_set:
            dxy_spot._emit_dxy_direct_latest(MagicMock())
        # mirror와 동일하게 get_latest_dxy_rate 반환값 그대로 전달 (byte-identical 공존)
        mock_set.assert_called_once_with(104.5, "2026-03-10T14:30:00+09:00", "cnbc")

    def test_flag_on_no_record_no_write(self):
        with patch("app.config.DXY_DIRECT_LATEST_ENABLED", True), \
             patch("app.crud.get_latest_dxy_rate", return_value=None), \
             patch("app.latest_rates_cache.set_latest_dxy_rate_from_sync_job") as mock_set:
            dxy_spot._emit_dxy_direct_latest(MagicMock())
        mock_set.assert_not_called()

    def test_exception_isolated(self):
        # get_latest_dxy_rate 예외 → _emit이 삼킴(크롤러 저장 경로 보호), raise 없으면 통과
        with patch("app.config.DXY_DIRECT_LATEST_ENABLED", True), \
             patch("app.crud.get_latest_dxy_rate", side_effect=RuntimeError("db down")):
            dxy_spot._emit_dxy_direct_latest(MagicMock())


if __name__ == "__main__":
    unittest.main()
