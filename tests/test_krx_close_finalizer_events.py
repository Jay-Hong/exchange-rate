"""KRX close finalizer structured event persist 테스트 (2026-05-26).

Scope:
    - `emit_krx_close_event` no-throw 보장 (Redis 장애 / JSON 실패 / ZADD/ZREM 실패)
    - flag false 시 즉시 False (no-op)
    - event_id uniqueness (attempt + emit_at_epoch_ms)
    - `get_krx_close_events` no-throw + 빈 list fallback

Design 원칙 잠금 (코덱스 + Claude 합의):
    - best-effort / no-throw — close finalizer 본 동작 영향 0
    - 저장 시점 case 분류 X — query 시점에 aggregation
    - dedup_skipped catalog 제외 (실제 분기 부재)
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from app import config
from app.latest_rates_cache import (
    emit_krx_close_event,
    get_krx_close_events,
    _KRX_CLOSE_EVENT_KEY,
    _KRX_CLOSE_EVENT_TTL_SEC,
)


class TestEmitKrxCloseEventBasics(unittest.TestCase):
    """Helper 기본 동작 + flag 분기."""

    def test_flag_false_returns_false_immediately(self):
        """KRX_CLOSE_EVENT_LOG_ENABLED=false 시 즉시 False (no-op)."""
        with patch.object(config, "KRX_CLOSE_EVENT_LOG_ENABLED", False):
            result = emit_krx_close_event(
                "ws_close_saved", session="CF", date_kst="2026-05-26",
            )
        self.assertFalse(result)

    def test_client_none_returns_false(self):
        """Redis client init 실패 시 False (no-throw)."""
        with patch.object(config, "KRX_CLOSE_EVENT_LOG_ENABLED", True), \
             patch("app.latest_rates_cache._get_sync_client", return_value=None):
            result = emit_krx_close_event(
                "ws_close_saved", session="CF", date_kst="2026-05-26",
            )
        self.assertFalse(result)


class TestEmitKrxCloseEventZADD(unittest.TestCase):
    """ZADD + ZREMRANGEBYSCORE 동작 + error isolation."""

    def _mock_client(self):
        client = MagicMock()
        client.zadd.return_value = 1
        client.zremrangebyscore.return_value = 0
        return client

    def test_zadd_with_event_id_in_json(self):
        """ZADD member JSON에 event_id + event_type 포함."""
        mock_client = self._mock_client()
        with patch.object(config, "KRX_CLOSE_EVENT_LOG_ENABLED", True), \
             patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            result = emit_krx_close_event(
                "ws_close_saved", session="CF", date_kst="2026-05-26",
                boundary_at_kst="2026-05-26T15:45:00+09:00", rate=1502.8,
            )
        self.assertTrue(result)
        # ZADD 호출 검증
        mock_client.zadd.assert_called_once()
        call_args = mock_client.zadd.call_args
        self.assertEqual(call_args.args[0], _KRX_CLOSE_EVENT_KEY)
        mapping = call_args.args[1]
        member_json = list(mapping.keys())[0]
        score = list(mapping.values())[0]
        # event JSON parse + 필드 검증
        event = json.loads(member_json)
        self.assertEqual(event["event_type"], "ws_close_saved")
        self.assertEqual(event["session"], "CF")
        self.assertEqual(event["date_kst"], "2026-05-26")
        self.assertEqual(event["rate"], 1502.8)
        self.assertTrue(event["event_id"].startswith("2026-05-26:CF:ws_close_saved:"))
        self.assertIn("emit_at_kst", event)
        # score = emit_at_epoch_ms (양수, 현재 시각 근처)
        self.assertGreater(score, 1_700_000_000_000)

    def test_event_id_includes_attempt_for_uniqueness(self):
        """attempt 명시 시 event_id에 포함 — ZSET member uniqueness."""
        mock_client = self._mock_client()
        with patch.object(config, "KRX_CLOSE_EVENT_LOG_ENABLED", True), \
             patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            emit_krx_close_event(
                "rest_fallback_attempted", session="CF", date_kst="2026-05-26",
                attempt=2,
            )
        member_json = list(mock_client.zadd.call_args.args[1].keys())[0]
        event = json.loads(member_json)
        self.assertIn("attempt=2", event["event_id"])
        self.assertEqual(event["attempt"], 2)

    def test_zremrangebyscore_called_with_14d_cutoff(self):
        """ZADD 후 ZREMRANGEBYSCORE로 14d 이전 trim."""
        mock_client = self._mock_client()
        with patch.object(config, "KRX_CLOSE_EVENT_LOG_ENABLED", True), \
             patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            emit_krx_close_event(
                "ws_close_saved", session="CF", date_kst="2026-05-26",
            )
        # ZREMRANGEBYSCORE 호출 검증
        mock_client.zremrangebyscore.assert_called_once()
        call_args = mock_client.zremrangebyscore.call_args
        self.assertEqual(call_args.args[0], _KRX_CLOSE_EVENT_KEY)
        self.assertEqual(call_args.args[1], "-inf")
        # cutoff = now_ms - 14d_ms (양수 + 합리 범위)
        cutoff = call_args.args[2]
        self.assertGreater(cutoff, 1_700_000_000_000 - _KRX_CLOSE_EVENT_TTL_SEC * 1000)

    def test_zadd_exception_returns_false_no_throw(self):
        """ZADD 예외 시 False return (no-throw)."""
        mock_client = MagicMock()
        mock_client.zadd.side_effect = Exception("Redis 장애")
        with patch.object(config, "KRX_CLOSE_EVENT_LOG_ENABLED", True), \
             patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            # 예외 propagate X
            result = emit_krx_close_event(
                "ws_close_saved", session="CF", date_kst="2026-05-26",
            )
        self.assertFalse(result)

    def test_zremrangebyscore_exception_still_returns_true(self):
        """ZADD 성공 + ZREMRANGEBYSCORE 예외 시에도 True (ZADD 성공 보존)."""
        mock_client = MagicMock()
        mock_client.zadd.return_value = 1
        mock_client.zremrangebyscore.side_effect = Exception("trim 실패")
        with patch.object(config, "KRX_CLOSE_EVENT_LOG_ENABLED", True), \
             patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            result = emit_krx_close_event(
                "ws_close_saved", session="CF", date_kst="2026-05-26",
            )
        # ZADD 성공이라 True (trim 실패는 자연 누적, 다음 emit에서 retry)
        self.assertTrue(result)


class TestGetKrxCloseEvents(unittest.TestCase):
    """Query helper no-throw."""

    def test_client_none_returns_empty_list(self):
        """Redis client init 실패 시 빈 list (catastrophic backup 안전)."""
        with patch("app.latest_rates_cache._get_sync_client", return_value=None):
            result = get_krx_close_events(since_epoch_ms=1_700_000_000_000)
        self.assertEqual(result, [])

    def test_zrangebyscore_exception_returns_empty_list(self):
        """ZRANGEBYSCORE 예외 시 빈 list (no-throw)."""
        mock_client = MagicMock()
        mock_client.zrangebyscore.side_effect = Exception("Redis 장애")
        with patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            result = get_krx_close_events(since_epoch_ms=1_700_000_000_000)
        self.assertEqual(result, [])

    def test_parse_success_returns_event_list(self):
        """Member JSON parse 성공 시 dict list 반환."""
        mock_client = MagicMock()
        members = [
            json.dumps({"event_id": "id1", "event_type": "ws_close_saved", "session": "CF",
                        "date_kst": "2026-05-26"}),
            json.dumps({"event_id": "id2", "event_type": "rest_fallback_attempted",
                        "session": "CF", "date_kst": "2026-05-26", "attempt": 1}),
        ]
        mock_client.zrangebyscore.return_value = members
        with patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            result = get_krx_close_events(since_epoch_ms=1_700_000_000_000)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["event_type"], "ws_close_saved")
        self.assertEqual(result[1]["event_type"], "rest_fallback_attempted")

    def test_invalid_json_member_skipped_no_throw(self):
        """JSON parse 실패 member는 skip + 나머지 정상 반환."""
        mock_client = MagicMock()
        members = [
            json.dumps({"event_id": "id1", "event_type": "ws_close_saved"}),
            "not valid json {{{",  # parse 실패
            json.dumps({"event_id": "id2", "event_type": "rest_fallback_attempted"}),
        ]
        mock_client.zrangebyscore.return_value = members
        with patch("app.latest_rates_cache._get_sync_client", return_value=mock_client):
            result = get_krx_close_events(since_epoch_ms=1_700_000_000_000)
        # 2 valid + 1 skip = 2 valid 반환 (no-throw)
        self.assertEqual(len(result), 2)


class TestCaseAggregation(unittest.TestCase):
    """Aggregation 로직 — query 시점 case 분류 (저장 시점 X).

    admin endpoint `get_krx_finalizer_stats`의 case 분류 로직을 단위 검증.
    실제 구현은 main.py에 있으므로 inline replication으로 logic 검증.
    """

    @staticmethod
    def _classify(types: set) -> str:
        """main.py endpoint logic mirror."""
        if "ws_close_saved" in types and not (types & {"rest_write_blocked", "rest_write_saved"}):
            return "A"
        elif types & {"rest_write_blocked", "rest_write_saved"}:
            return "B"
        elif "dedup_skipped" in types:
            return "C"
        else:
            return "unclassified"

    def test_case_a_ws_close_saved_only(self):
        """ws_close_saved 단독 → Case A."""
        self.assertEqual(self._classify({"ws_close_saved"}), "A")

    def test_case_a_with_rest_skipped_confirmation(self):
        """ws_close_saved + rest_skipped_ws_captured (confirmation) → 여전히 Case A."""
        self.assertEqual(
            self._classify({"ws_close_saved", "rest_skipped_ws_captured"}),
            "A",
        )

    def test_case_a_with_rest_attempted_but_no_write(self):
        """ws_close_saved + rest_fallback_attempted but no write → Case A.

        REST 시도됐지만 close source가 안 됐으면 (returned_none/sanity_aborted/failed)
        WS가 여전히 final → Case A.
        """
        self.assertEqual(
            self._classify({"ws_close_saved", "rest_fallback_attempted", "rest_returned_none"}),
            "A",
        )

    def test_case_b_rest_write_blocked(self):
        """rest_write_blocked → Case B (현 정책 핵심 신호)."""
        self.assertEqual(
            self._classify({"rest_fallback_attempted", "rest_write_blocked"}),
            "B",
        )

    def test_case_b_rest_write_saved(self):
        """rest_write_saved (future) → Case B."""
        self.assertEqual(
            self._classify({"rest_fallback_attempted", "rest_write_saved"}),
            "B",
        )

    def test_case_b_overrides_ws_saved(self):
        """ws_close_saved + rest_write_blocked 동시 존재 시 Case B (REST가 source 됐거나 될 뻔)."""
        self.assertEqual(
            self._classify({"ws_close_saved", "rest_write_blocked"}),
            "B",
        )

    def test_unclassified_rest_returned_none_only(self):
        """rest_fallback_attempted + rest_returned_none 단독 (ws_close_saved 없음) → unclassified."""
        self.assertEqual(
            self._classify({"rest_fallback_attempted", "rest_returned_none"}),
            "unclassified",
        )

    def test_unclassified_sanity_aborted_only(self):
        """sanity abort 단독 → unclassified (새 관찰 신호)."""
        self.assertEqual(
            self._classify({"rest_fallback_attempted", "rest_sanity_aborted"}),
            "unclassified",
        )

    def test_malformed_event_type_none_normalized(self):
        """event_type=None인 malformed event도 normalize 처리 — sorted() TypeError 차단.

        Defensive: Redis에 malformed-but-valid JSON ({}, {"foo": "bar"} 등)이
        들어가도 types set에 None 섞이지 않게 str(... or "unknown") normalize.
        """
        # main.py endpoint logic mirror
        events = [
            {"event_type": "ws_close_saved", "date_kst": "2026-05-26", "session": "CF"},
            {"date_kst": "2026-05-26", "session": "CF"},  # event_type 누락
        ]
        types = {str(e.get("event_type") or "unknown") for e in events}
        # None이 set에 들어가지 않고 "unknown"으로 normalize됨
        self.assertNotIn(None, types)
        self.assertIn("unknown", types)
        self.assertIn("ws_close_saved", types)
        # sorted() TypeError 없이 호출 가능
        sorted_types = sorted(types)
        self.assertEqual(sorted_types, ["unknown", "ws_close_saved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
