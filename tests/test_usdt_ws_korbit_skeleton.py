"""USDT WS Korbit canary 단위 테스트 — Phase B.5 Stage K2-K3.

USDT_WS_DESIGN_PLAN §12.7 Phase B.5.
Coinone Stage C2-C3 테스트 패턴 minimal 복제 (K2/K3 skeleton 테스트는 이후
K4~K7 stage 진입 시 깨질 가능성 최소화 위해 핵심 시나리오만).

K3 client behavior:
    - Constants: WS URL / symbol / recv timeout / subscribe requestId
    - _build_subscribe_payload: Korbit list-wrap form (Coinone single-dict와 다름)
    - _parse_ticker_message: ticker → normalized tick (close string → float,
      lastTradedAt 우선 + top-level timestamp fallback, snapshot key 무시)
    - _handle_message: 분기 3+1 (status unified ACK/ERROR + ticker + unknown safe)
    - _run_one_session: connect + subscribe + recv loop
    - start: _run_one_session 단일 실행 (reconnect 없음, K4로 분리)

테스트 시나리오 (K2 6개 + K3 추가):
    [K2]
    1. flag=false → KorbitWsClient 생성 안 함
    2-3. flag=true → task 생성 + 중복 start 방지
    4. shutdown 시 client/task 정리
    5. __init__ state
    6. start()이 _run_one_session 호출 + stop()으로 빠져나옴
       (K3 정정: 원래 "start = stop_event 대기만" → "start = _run_one_session 호출")
    [K3]
    7. _build_subscribe_payload: list-wrap form (1 element list)
    8. _parse_ticker_message: valid (snapshot:true 첫 frame / snapshot key 누락) + invalid
    9. _handle_message: status=success ACK / status=fail ERROR / status=unknown
    10. _handle_message: ticker first INFO + subsequent DEBUG
    11. _handle_message: invalid JSON / unknown frame / malformed safe
    12. _run_one_session: subscribe send + recv → handle_message + stop_event 빠짐
    13. Scope guard: Redis/DB/Alert/REST writer import 0건
    14. K2 flag=false invariant 유지 (K3 회귀)

실행:
    python -m unittest tests.test_usdt_ws_korbit_skeleton -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.crawlers.usdt_ws.korbit import (
    KORBIT_SYMBOL,
    KORBIT_WS_URL,
    RECV_TIMEOUT_SEC,
    SUBSCRIBE_REQUEST_ID,
    KorbitWsClient,
)


def _reset_scheduler_usdt_ws_korbit_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_korbit_client = None
    scheduler.usdt_ws_korbit_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_korbit_client` mock — client.stop()까지 stop_event 대기."""
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# K2 Scenario 1: flag=false → KorbitWsClient 생성 안 함 (K2 핵심 acceptance)
# ---------------------------------------------------------------------------

class TestFlagFalseInvariant(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    async def test_no_korbit_client_constructed_when_flag_false(self):
        """flag=false → start 함수 즉시 return + KorbitWsClient 생성 X + task 생성 X."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", False), \
             patch("app.crawlers.usdt_ws.korbit.KorbitWsClient") as mock_client_cls, \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler.start_usdt_ws_korbit_client()
        mock_client_cls.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_korbit_client)
        self.assertIsNone(scheduler.usdt_ws_korbit_task)
        self.assertTrue(any("USDT_WS_KORBIT_ENABLED=false" in m for m in cm.output))


# ---------------------------------------------------------------------------
# K2 Scenario 2-3: flag=true → task 생성, 중복 start 방지
# ---------------------------------------------------------------------------

class TestStartUsdtWsKorbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    async def test_creates_task_when_enabled(self):
        """flag=true → KorbitWsClient + task 생성."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_korbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_korbit_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_korbit_client)
                self.assertIsNotNone(scheduler.usdt_ws_korbit_task)
                self.assertFalse(scheduler.usdt_ws_korbit_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_korbit_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_korbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_korbit_client()
            first_task = scheduler.usdt_ws_korbit_task
            first_client = scheduler.usdt_ws_korbit_client
            self.assertIsNotNone(first_task)

            try:
                await scheduler.start_usdt_ws_korbit_client()
                self.assertIs(scheduler.usdt_ws_korbit_task, first_task)
                self.assertIs(scheduler.usdt_ws_korbit_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_korbit_client()


# ---------------------------------------------------------------------------
# K2 Scenario 4: shutdown 시 client/task 정리
# ---------------------------------------------------------------------------

class TestShutdownUsdtWsKorbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_korbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_korbit_client()
            task = scheduler.usdt_ws_korbit_task
            self.assertIsNotNone(task)

            await scheduler.shutdown_usdt_ws_korbit_client()
            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_korbit_client)
            self.assertIsNone(scheduler.usdt_ws_korbit_task)


# ---------------------------------------------------------------------------
# K2 Scenario 5-6: KorbitWsClient skeleton + K3 정정 (start → _run_one_session)
# ---------------------------------------------------------------------------

class TestKorbitWsClientSkeleton(unittest.IsolatedAsyncioTestCase):
    """K2/K3 client lifecycle behavior."""

    async def test_init_state(self):
        """__init__ → stop_event (not set) + running False + K3 session state defaults."""
        client = KorbitWsClient()
        self.assertFalse(client._stop_event.is_set())
        self.assertFalse(client._running)
        # K3 session state
        self.assertIsNone(client._ws)
        self.assertFalse(client._first_tick_logged)

    async def test_start_calls_run_one_session_then_stop(self):
        """K3 정정 (Coinone Codex Point 1 mirror): start → _run_one_session 호출 + stop()으로 종료.

        K2 원본 test (start = stop_event 대기)에서 K3 정정 — start가 실제
        _run_one_session을 호출하므로 mock으로 stop_event.wait() 대체.
        """
        client = KorbitWsClient()

        async def fake_run_one_session():
            await client._stop_event.wait()

        with patch.object(client, "_run_one_session", side_effect=fake_run_one_session):
            task = asyncio.create_task(client.start())
            # start 호출 후 _run_one_session 진행 중 확인
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertTrue(client._running)
            # stop 호출 → _stop_event set → fake _run_one_session 종료
            await client.stop()
            await asyncio.wait_for(task, timeout=1.0)
            self.assertTrue(task.done())
            self.assertFalse(client._running)


# ---------------------------------------------------------------------------
# K3 Scenario 7: _build_subscribe_payload
# ---------------------------------------------------------------------------

class TestBuildSubscribePayload(unittest.TestCase):
    """K3 acceptance: subscribe payload는 Korbit list-wrap form (1-element list)."""

    def test_payload_shape(self):
        """payload는 list of dict (Coinone dict 단일 / Bithumb dict와 다름)."""
        payload = KorbitWsClient._build_subscribe_payload()
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 1)
        sub = payload[0]
        self.assertIsInstance(sub, dict)
        self.assertEqual(sub["requestId"], SUBSCRIBE_REQUEST_ID)
        self.assertEqual(sub["method"], "subscribe")
        self.assertEqual(sub["type"], "ticker")
        self.assertEqual(sub["symbols"], [KORBIT_SYMBOL])


# ---------------------------------------------------------------------------
# K3 Scenario 8: _parse_ticker_message valid + invalid
# ---------------------------------------------------------------------------

class TestParseTickerMessage(unittest.TestCase):
    """K3 acceptance: ticker parser — close string → float, ts fallback, snapshot 무시."""

    def setUp(self):
        self.client = KorbitWsClient()

    def _valid_ticker_message(self, *, snapshot: bool = False) -> dict:
        """K-2 smoke 실측 frame 패턴 mirror."""
        msg = {
            "type": "ticker",
            "timestamp": 1779194622984,
            "symbol": "usdt_krw",
            "data": {
                "open": "1486", "high": "1490", "low": "1483",
                "close": "1488",
                "lastTradedAt": 1779194593306,
                "bestAskPrice": "1489", "bestBidPrice": "1488",
            },
        }
        if snapshot:
            msg["snapshot"] = True
        return msg

    def test_valid_ticker_with_snapshot_true_returns_normalized_tick(self):
        """첫 frame snapshot:true → tick dict (snapshot 키 무시)."""
        message = self._valid_ticker_message(snapshot=True)
        tick = self.client._parse_ticker_message(message)
        self.assertEqual(tick, {
            "source": "korbit",
            "asset": "usdt-krw",
            "rate": 1488.0,
            "timestamp_ms": 1779194593306,
        })
        self.assertIsInstance(tick["timestamp_ms"], int)
        self.assertIsInstance(tick["rate"], float)

    def test_valid_ticker_without_snapshot_key_returns_normalized_tick(self):
        """K-2 smoke 검증: 이후 ticker는 snapshot key 누락. 가격값 동일 추출."""
        message = self._valid_ticker_message(snapshot=False)
        self.assertNotIn("snapshot", message)
        tick = self.client._parse_ticker_message(message)
        self.assertEqual(tick["rate"], 1488.0)
        self.assertEqual(tick["timestamp_ms"], 1779194593306)

    def test_non_ticker_type_returns_none(self):
        message = self._valid_ticker_message()
        message["type"] = "trade"
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_symbol_mismatch_returns_none(self):
        message = self._valid_ticker_message()
        message["symbol"] = "btc_krw"
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_data_dict_missing_returns_none(self):
        message = {"type": "ticker", "symbol": "usdt_krw"}
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_close_missing_returns_none(self):
        message = self._valid_ticker_message()
        del message["data"]["close"]
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_close_zero_returns_none(self):
        message = self._valid_ticker_message()
        message["data"]["close"] = "0"
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_close_negative_returns_none(self):
        message = self._valid_ticker_message()
        message["data"]["close"] = "-100"
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_close_not_parsable_returns_none(self):
        message = self._valid_ticker_message()
        message["data"]["close"] = "not-a-number"
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_lasttradedat_missing_falls_back_to_top_level_timestamp(self):
        """K-2 검증: 활발 시간대 lastTradedAt 100% 존재. sparse-time 보수적 fallback 검증."""
        message = self._valid_ticker_message()
        del message["data"]["lastTradedAt"]
        # top-level timestamp는 유지
        tick = self.client._parse_ticker_message(message)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["timestamp_ms"], 1779194622984)  # top-level

    def test_both_timestamps_missing_returns_none(self):
        message = self._valid_ticker_message()
        del message["data"]["lastTradedAt"]
        del message["timestamp"]
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_timestamp_not_parsable_returns_none(self):
        message = self._valid_ticker_message()
        message["data"]["lastTradedAt"] = "not-an-int"
        # top-level timestamp도 invalid로 설정
        message["timestamp"] = "not-an-int"
        self.assertIsNone(self.client._parse_ticker_message(message))


# ---------------------------------------------------------------------------
# K3 Scenario 9-11: _handle_message 분기 (status / ticker / unknown)
# ---------------------------------------------------------------------------

class TestHandleMessage(unittest.IsolatedAsyncioTestCase):
    """K3 acceptance: 분기 3+1 + ticker tick return / 그 외 None + status unified."""

    def setUp(self):
        self.client = KorbitWsClient()

    def test_status_success_ack_log_only(self):
        """status=success (subscribe ACK) → INFO log + return None."""
        message = {"status": "success", "requestId": 1}
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="INFO") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("subscribe ACK" in m for m in cm.output))

    def test_status_fail_error_warning_log(self):
        """status=fail (ERROR) → WARNING log + return None."""
        message = {
            "status": "fail",
            "code": "INVALID_REQUEST",
            "message": "unknown_type",
            "requestId": 999,
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="WARNING") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("ERROR" in m and "INVALID_REQUEST" in m for m in cm.output))

    def test_status_unknown_safe_warning(self):
        """status=unknown_value → safe WARNING + None."""
        message = {"status": "weird_status"}
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="WARNING") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("unknown status" in m for m in cm.output))

    def test_ticker_first_tick_info_log_and_return_tick(self):
        """ticker first frame → INFO log + tick dict 반환 + first_tick_logged True."""
        message = {
            "type": "ticker",
            "timestamp": 1779194622984,
            "symbol": "usdt_krw",
            "snapshot": True,
            "data": {"close": "1488", "lastTradedAt": 1779194593306},
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="INFO") as cm:
            tick = self.client._handle_message(json.dumps(message))
        self.assertIsNotNone(tick)
        self.assertEqual(tick["rate"], 1488.0)
        self.assertEqual(tick["timestamp_ms"], 1779194593306)
        self.assertTrue(self.client._first_tick_logged)
        self.assertTrue(any("first tick" in m for m in cm.output))

    def test_ticker_subsequent_tick_debug_only(self):
        """ticker 2번째 frame → DEBUG only (INFO 없음) + tick dict 반환."""
        message = {
            "type": "ticker",
            "timestamp": 1779194622984,
            "symbol": "usdt_krw",
            "data": {"close": "1488", "lastTradedAt": 1779194593306},
        }
        # first tick
        self.client._handle_message(json.dumps(message))
        # second tick — INFO log 없어야 함
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="DEBUG") as cm:
            tick = self.client._handle_message(json.dumps(message))
        self.assertIsNotNone(tick)
        info_messages = [m for m in cm.output if m.startswith("INFO:")]
        self.assertEqual(info_messages, [])

    def test_ticker_parse_failed_safe_warning(self):
        """type=ticker이지만 parse 실패 (close 없음) → WARNING + None."""
        message = {
            "type": "ticker",
            "symbol": "usdt_krw",
            "data": {},  # close 누락
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="WARNING") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("ticker parse failed" in m for m in cm.output))

    def test_unknown_frame_safe_warning(self):
        """status 없음 + type 모름 → safe WARNING + None."""
        message = {"foo": "bar", "type": "unexpected_type"}
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="WARNING") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("unknown frame" in m for m in cm.output))

    def test_invalid_json_safe_warning(self):
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="WARNING") as cm:
            result = self.client._handle_message("not-a-json")
        self.assertIsNone(result)
        self.assertTrue(any("invalid JSON" in m for m in cm.output))

    def test_non_dict_message_safe_warning(self):
        """JSON이지만 dict가 아닌 경우 (list/string 등) → safe WARNING + None."""
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="WARNING") as cm:
            result = self.client._handle_message(json.dumps(["not", "a", "dict"]))
        self.assertIsNone(result)
        self.assertTrue(any("non-dict" in m for m in cm.output))


# ---------------------------------------------------------------------------
# K3 Scenario 12: _run_one_session — minimal (Coinone Codex Point 4 mirror)
# ---------------------------------------------------------------------------

class TestRunOneSession(unittest.IsolatedAsyncioTestCase):
    """K3 acceptance: connect/send/recv/handle까지만 (reconnect/PING 없음).

    최소 acceptance 3개만 검증 (Coinone C3 패턴):
        - subscribe payload list-wrap이 send() 됨
        - 받은 raw가 _handle_message()로 전달됨
        - stop_event set으로 빠짐
    """

    async def test_subscribe_sent_and_handle_called_then_stop(self):
        client = KorbitWsClient()
        sent_payloads = []
        recv_count = {"calls": 0}
        recv_raws = [
            json.dumps({"status": "success", "requestId": 1}),  # ACK
            json.dumps({
                "type": "ticker", "timestamp": 1779194622984, "symbol": "usdt_krw",
                "data": {"close": "1488", "lastTradedAt": 1779194593306},
            }),
        ]

        async def fake_send(payload):
            sent_payloads.append(payload)

        async def fake_recv():
            i = recv_count["calls"]
            recv_count["calls"] += 1
            if i < len(recv_raws):
                return recv_raws[i]
            # 모든 raw 소진 → stop_event set + asyncio.TimeoutError 흉내
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock(side_effect=fake_send)
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)

        handle_calls = []
        original_handle = client._handle_message

        def spy_handle(raw):
            handle_calls.append(raw)
            return original_handle(raw)

        with patch("app.crawlers.usdt_ws.korbit.websockets.connect", return_value=mock_ws), \
             patch.object(client, "_handle_message", side_effect=spy_handle):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # subscribe payload list-wrap 1회 send
        self.assertEqual(len(sent_payloads), 1)
        sent = json.loads(sent_payloads[0])
        self.assertIsInstance(sent, list)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "subscribe")
        self.assertEqual(sent[0]["type"], "ticker")
        self.assertEqual(sent[0]["symbols"], ["usdt_krw"])
        # 받은 raw 2건이 _handle_message로 전달됨
        self.assertEqual(len(handle_calls), 2)
        # stop_event set → recv loop 빠짐


# ---------------------------------------------------------------------------
# K3 Scenario 13: Scope guard — Redis/DB/Alert/REST import 0건
# ---------------------------------------------------------------------------

class TestScopeGuard(unittest.TestCase):
    """K3 acceptance: korbit.py에 Redis/DB/Alert/REST writer import 0건."""

    def test_no_forbidden_imports(self):
        """korbit.py module이 Redis/DB/Alert/REST/topic trigger import하지 않음."""
        import app.crawlers.usdt_ws.korbit as korbit_module
        module_attrs = dir(korbit_module)
        forbidden = [
            "latest_rates_cache",
            "tether_topic_trigger",
            "UsdtAlertEvaluator",
            "AlertObservation",
            "UsdtLivenessMonitor",
            "get_db_context",
            "insert_source_rate_if_changed",
            "fetch_korbit_usdt_tick",
        ]
        for name in forbidden:
            self.assertNotIn(
                name, module_attrs,
                f"K3 scope 위반: '{name}' import됨 (K5~K7 영역)",
            )

    def test_module_imports_minimal(self):
        """module imports 최소 — asyncio + json + logging + websockets + typing only."""
        import app.crawlers.usdt_ws.korbit as korbit_module
        self.assertTrue(hasattr(korbit_module, "KorbitWsClient"))
        self.assertTrue(hasattr(korbit_module, "KORBIT_WS_URL"))
        self.assertTrue(hasattr(korbit_module, "KORBIT_SYMBOL"))


# ---------------------------------------------------------------------------
# K3 Scenario 14: K2 flag=false invariant 유지 (K3 회귀)
# ---------------------------------------------------------------------------

class TestFlagFalseInvariantK3Regression(unittest.IsolatedAsyncioTestCase):
    """K3 land 후에도 K2 flag=false invariant 유지."""

    def setUp(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_korbit_globals()

    async def test_flag_false_still_no_client(self):
        """K3 land 후에도 flag=false 시 client/task 생성 X (회귀)."""
        with patch.object(config, "USDT_WS_KORBIT_ENABLED", False):
            await scheduler.start_usdt_ws_korbit_client()
        self.assertIsNone(scheduler.usdt_ws_korbit_client)
        self.assertIsNone(scheduler.usdt_ws_korbit_task)


if __name__ == "__main__":
    unittest.main()
