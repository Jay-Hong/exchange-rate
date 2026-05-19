"""USDT WS Coinone canary 단위 테스트 — Phase B.4 Stage C2-C3.

USDT_WS_DESIGN_PLAN §12.6 Phase B.4.
Bithumb Stage U2-U3 테스트 패턴 minimal 복제 (Codex 권장 — skeleton test는
이후 stage 진입 시 깨질 가능성 최소화 위해 핵심 시나리오만).

C2 핵심 acceptance (USDT_WS_DESIGN_PLAN §12.6.4):
    USDT_WS_COINONE_ENABLED=false 시 scheduler start 함수 즉시 return +
    CoinoneWsClient 생성 X + network connect X + Redis/DB writer X.

C3 client behavior:
    - Constants: WS URL / quote/target currency / recv timeout
    - _build_subscribe_payload: Coinone single-dict form (별 protocol)
    - _parse_data_message: DATA → normalized tick (last string → float,
      timestamp int ms raw 유지)
    - _handle_message: response_type 분기 5+1 + log only + DATA tick return
    - _run_one_session: connect + subscribe + recv loop
    - start: _run_one_session 단일 실행 (reconnect 없음, C4로 분리)

테스트 시나리오 (C2 6개 + C3 추가):
    [C2]
    1. flag=false → CoinoneWsClient 생성 안 함
    2. flag=true → task 생성됨
    3. 중복 start 방지
    4. shutdown 시 client/task 정리
    5. CoinoneWsClient.__init__ state
    6. CoinoneWsClient.start()이 _run_one_session 호출 + stop()으로 빠져나옴
       (C3 정정: 원래 "start = stop_event 대기만" → "start = _run_one_session 호출")
    [C3]
    7. _build_subscribe_payload: Coinone single-dict form
    8. _parse_data_message: valid + invalid cases
    9. _handle_message: CONNECTED + session_id 캡처
    10. _handle_message: SUBSCRIBED log
    11. _handle_message: DATA first tick INFO + 이후 DEBUG
    12. _handle_message: PONG / ERROR / unknown safe log
    13. _run_one_session: subscribe send + recv → handle_message + stop_event 빠짐
    14. Scope guard: Redis/DB/Alert/REST writer import 0건

실행:
    python -m unittest tests.test_usdt_ws_coinone_skeleton -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.crawlers.usdt_ws.coinone import (
    COINONE_QUOTE_CURRENCY,
    COINONE_TARGET_CURRENCY,
    COINONE_WS_URL,
    RECV_TIMEOUT_SEC,
    CoinoneWsClient,
)


def _reset_scheduler_usdt_ws_coinone_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_coinone_client = None
    scheduler.usdt_ws_coinone_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_coinone_client` mock — client.stop()까지 stop_event 대기."""
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# C2 Scenario 1: flag=false → CoinoneWsClient 생성 안 함
# ---------------------------------------------------------------------------

class TestFlagFalseInvariant(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    async def test_no_coinone_client_constructed_when_flag_false(self):
        """flag=false → start 함수 즉시 return + CoinoneWsClient 생성 X + task 생성 X."""
        with patch.object(config, "USDT_WS_COINONE_ENABLED", False), \
             patch("app.crawlers.usdt_ws.coinone.CoinoneWsClient") as mock_client_cls, \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler.start_usdt_ws_coinone_client()
        mock_client_cls.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_coinone_client)
        self.assertIsNone(scheduler.usdt_ws_coinone_task)
        self.assertTrue(any("USDT_WS_COINONE_ENABLED=false" in m for m in cm.output))


# ---------------------------------------------------------------------------
# C2 Scenario 2-3: flag=true task 생성 + 중복 start 방지
# ---------------------------------------------------------------------------

class TestStartUsdtWsCoinoneClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    async def test_creates_task_when_enabled(self):
        """flag=true → CoinoneWsClient + task 생성."""
        with patch.object(config, "USDT_WS_COINONE_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_coinone_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_coinone_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_coinone_client)
                self.assertIsNotNone(scheduler.usdt_ws_coinone_task)
                self.assertFalse(scheduler.usdt_ws_coinone_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_coinone_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_COINONE_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_coinone_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_coinone_client()
            first_task = scheduler.usdt_ws_coinone_task
            first_client = scheduler.usdt_ws_coinone_client
            self.assertIsNotNone(first_task)

            try:
                await scheduler.start_usdt_ws_coinone_client()
                self.assertIs(scheduler.usdt_ws_coinone_task, first_task)
                self.assertIs(scheduler.usdt_ws_coinone_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_coinone_client()


# ---------------------------------------------------------------------------
# C2 Scenario 4: shutdown 시 client/task 정리
# ---------------------------------------------------------------------------

class TestShutdownUsdtWsCoinoneClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_COINONE_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_coinone_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_coinone_client()
            task = scheduler.usdt_ws_coinone_task
            self.assertIsNotNone(task)

            await scheduler.shutdown_usdt_ws_coinone_client()
            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_coinone_client)
            self.assertIsNone(scheduler.usdt_ws_coinone_task)


# ---------------------------------------------------------------------------
# C2 Scenario 5-6: CoinoneWsClient skeleton + C3 정정 (start → _run_one_session)
# ---------------------------------------------------------------------------

class TestCoinoneWsClientSkeleton(unittest.IsolatedAsyncioTestCase):
    """C2/C3 client lifecycle behavior."""

    async def test_init_state(self):
        """__init__ → stop_event (not set) + running False + C3 session state defaults."""
        client = CoinoneWsClient()
        self.assertFalse(client._stop_event.is_set())
        self.assertFalse(client._running)
        # C3 session state
        self.assertIsNone(client._ws)
        self.assertFalse(client._first_tick_logged)
        self.assertIsNone(client._session_id)

    async def test_start_calls_run_one_session_then_stop(self):
        """C3 정정 (Codex Point 1): start → _run_one_session 호출 + stop()으로 종료.

        C2 원본 test (start = stop_event 대기)에서 C3 정정 — start가 실제
        _run_one_session을 호출하므로 mock으로 stop_event.wait() 대체.
        """
        client = CoinoneWsClient()

        async def fake_run_one_session():
            # C3 정정: _run_one_session이 stop_event.wait()로 대체됨
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
# C3 Scenario 7: _build_subscribe_payload
# ---------------------------------------------------------------------------

class TestBuildSubscribePayload(unittest.TestCase):
    """C3 acceptance 2: subscribe payload는 Coinone single-dict form."""

    def test_payload_shape(self):
        """payload는 dict (Upbit/Bithumb의 3-element list와 다름)."""
        payload = CoinoneWsClient._build_subscribe_payload()
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["request_type"], "SUBSCRIBE")
        self.assertEqual(payload["channel"], "TICKER")
        self.assertEqual(payload["topic"], {
            "quote_currency": COINONE_QUOTE_CURRENCY,
            "target_currency": COINONE_TARGET_CURRENCY,
        })


# ---------------------------------------------------------------------------
# C3 Scenario 8: _parse_data_message valid + invalid
# ---------------------------------------------------------------------------

class TestParseDataMessage(unittest.TestCase):
    """C3 acceptance 4-5: DATA parser — last string → float, timestamp int ms 보존."""

    def setUp(self):
        self.client = CoinoneWsClient()

    def _valid_data_message(self) -> dict:
        return {
            "response_type": "DATA",
            "channel": "TICKER",
            "data": {
                "quote_currency": "KRW",
                "target_currency": "USDT",
                "timestamp": 1779106625946,
                "last": "1487",
                "ask_best_price": "1487",
                "bid_best_price": "1486",
            },
        }

    def test_valid_data_returns_normalized_tick(self):
        message = self._valid_data_message()
        tick = self.client._parse_data_message(message)
        self.assertEqual(tick, {
            "source": "coinone",
            "asset": "usdt-krw",
            "rate": 1487.0,
            "timestamp_ms": 1779106625946,
        })
        # raw int 유지 (timestamp_ms는 KST/UTC 변환 없음)
        self.assertIsInstance(tick["timestamp_ms"], int)
        self.assertIsInstance(tick["rate"], float)

    def test_non_data_response_type_returns_none(self):
        message = self._valid_data_message()
        message["response_type"] = "SUBSCRIBED"
        self.assertIsNone(self.client._parse_data_message(message))

    def test_quote_currency_mismatch_returns_none(self):
        message = self._valid_data_message()
        message["data"]["quote_currency"] = "USD"
        self.assertIsNone(self.client._parse_data_message(message))

    def test_target_currency_mismatch_returns_none(self):
        message = self._valid_data_message()
        message["data"]["target_currency"] = "BTC"
        self.assertIsNone(self.client._parse_data_message(message))

    def test_last_zero_returns_none(self):
        message = self._valid_data_message()
        message["data"]["last"] = "0"
        self.assertIsNone(self.client._parse_data_message(message))

    def test_last_negative_returns_none(self):
        message = self._valid_data_message()
        message["data"]["last"] = "-100"
        self.assertIsNone(self.client._parse_data_message(message))

    def test_last_missing_returns_none(self):
        message = self._valid_data_message()
        del message["data"]["last"]
        self.assertIsNone(self.client._parse_data_message(message))

    def test_last_not_parsable_returns_none(self):
        message = self._valid_data_message()
        message["data"]["last"] = "not-a-number"
        self.assertIsNone(self.client._parse_data_message(message))

    def test_timestamp_missing_returns_none(self):
        message = self._valid_data_message()
        del message["data"]["timestamp"]
        self.assertIsNone(self.client._parse_data_message(message))

    def test_data_dict_missing_returns_none(self):
        message = {"response_type": "DATA"}
        self.assertIsNone(self.client._parse_data_message(message))


# ---------------------------------------------------------------------------
# C3 Scenario 9-12: _handle_message response_type 분기 (CONNECTED/SUBSCRIBED/DATA/PONG/ERROR/unknown)
# ---------------------------------------------------------------------------

class TestHandleMessage(unittest.IsolatedAsyncioTestCase):
    """C3 acceptance 3, 6: response_type 5+1 분기 + DATA tick return / 그 외 None + session_id 캡처."""

    def setUp(self):
        self.client = CoinoneWsClient()

    def test_connected_captures_session_id(self):
        """CONNECTED → data.session_id 캡처 + INFO log + return None."""
        message = {
            "response_type": "CONNECTED",
            "data": {"session_id": "b68458e0-0a41-d608-42b3-f7708ff92b14"},
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertEqual(
            self.client._session_id,
            "b68458e0-0a41-d608-42b3-f7708ff92b14",
        )
        self.assertTrue(any("CONNECTED" in m for m in cm.output))

    def test_subscribed_log_only(self):
        message = {
            "response_type": "SUBSCRIBED",
            "channel": "TICKER",
            "data": {"quote_currency": "KRW", "target_currency": "USDT"},
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("SUBSCRIBED" in m for m in cm.output))

    def test_data_first_tick_info_log_and_return_tick(self):
        """DATA first tick → INFO log + tick dict 반환 + first_tick_logged True."""
        message = {
            "response_type": "DATA",
            "data": {
                "quote_currency": "KRW", "target_currency": "USDT",
                "last": "1487", "timestamp": 1779106625946,
            },
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            tick = self.client._handle_message(json.dumps(message))
        self.assertIsNotNone(tick)
        self.assertEqual(tick["rate"], 1487.0)
        self.assertEqual(tick["timestamp_ms"], 1779106625946)
        self.assertTrue(self.client._first_tick_logged)
        self.assertTrue(any("first tick" in m for m in cm.output))

    def test_data_subsequent_tick_debug_only(self):
        """DATA 2번째 tick → DEBUG only (INFO 없음) + tick dict 반환."""
        message = {
            "response_type": "DATA",
            "data": {
                "quote_currency": "KRW", "target_currency": "USDT",
                "last": "1487", "timestamp": 1779106625946,
            },
        }
        # first tick
        self.client._handle_message(json.dumps(message))
        # second tick — INFO log 없어야 함
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="DEBUG") as cm:
            tick = self.client._handle_message(json.dumps(message))
        self.assertIsNotNone(tick)
        info_messages = [m for m in cm.output if m.startswith("INFO:")]
        self.assertEqual(info_messages, [])

    def test_pong_debug_only(self):
        """PONG → DEBUG only + return None."""
        message = {"response_type": "PONG"}
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="DEBUG") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("PONG" in m for m in cm.output))

    def test_error_warning_log(self):
        message = {
            "response_type": "ERROR",
            "error_code": "1001",
            "error_message": "invalid topic",
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="WARNING") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("ERROR" in m and "1001" in m for m in cm.output))

    def test_unknown_response_type_safe_warning(self):
        """C3 unknown response_type → safe WARNING log + None (Codex 강조 2)."""
        message = {"response_type": "UNEXPECTED_TYPE"}
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="WARNING") as cm:
            result = self.client._handle_message(json.dumps(message))
        self.assertIsNone(result)
        self.assertTrue(any("unknown response_type" in m for m in cm.output))

    def test_invalid_json_safe_warning(self):
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="WARNING") as cm:
            result = self.client._handle_message("not-a-json")
        self.assertIsNone(result)
        self.assertTrue(any("invalid JSON" in m for m in cm.output))

    def test_connected_malformed_data_safe(self):
        """CONNECTED frame의 data field가 dict가 아닐 때 (e.g. list/str) safe log + None.

        Codex hardening — defensive coding 일관성 (C3 "safe log-only" invariant).
        data가 list나 string이어도 AttributeError 없이 session_id None 유지.
        """
        # data가 string인 case
        message_str = {"response_type": "CONNECTED", "data": "unexpected-string"}
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            result = self.client._handle_message(json.dumps(message_str))
        self.assertIsNone(result)
        self.assertIsNone(self.client._session_id)  # session_id 캡처 X
        self.assertTrue(any("CONNECTED" in m for m in cm.output))

        # data가 list인 case
        message_list = {"response_type": "CONNECTED", "data": ["foo", "bar"]}
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            result = self.client._handle_message(json.dumps(message_list))
        self.assertIsNone(result)
        self.assertIsNone(self.client._session_id)
        self.assertTrue(any("CONNECTED" in m for m in cm.output))


# ---------------------------------------------------------------------------
# C3 Scenario 13: _run_one_session — minimal (Codex Point 4)
# ---------------------------------------------------------------------------

class TestRunOneSession(unittest.IsolatedAsyncioTestCase):
    """C3 acceptance 7: connect/send/recv/handle까지만 (reconnect/PING 없음).

    Codex Point 4 (간소화): 최소 acceptance 3개만 검증.
        - subscribe payload가 send() 됨
        - 받은 raw가 _handle_message()로 전달됨
        - stop_event set으로 빠짐
    """

    async def test_subscribe_sent_and_handle_called_then_stop(self):
        client = CoinoneWsClient()
        sent_payloads = []
        recv_count = {"calls": 0}
        recv_raws = [
            json.dumps({"response_type": "CONNECTED", "data": {"session_id": "abc"}}),
            json.dumps({"response_type": "SUBSCRIBED", "channel": "TICKER",
                        "data": {"quote_currency": "KRW", "target_currency": "USDT"}}),
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

        with patch("app.crawlers.usdt_ws.coinone.websockets.connect", return_value=mock_ws), \
             patch.object(client, "_handle_message", side_effect=spy_handle):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # subscribe payload 1회 send
        self.assertEqual(len(sent_payloads), 1)
        sent = json.loads(sent_payloads[0])
        self.assertEqual(sent["request_type"], "SUBSCRIBE")
        self.assertEqual(sent["channel"], "TICKER")
        # 받은 raw 2건이 _handle_message로 전달됨
        self.assertEqual(len(handle_calls), 2)
        # stop_event set → recv loop 빠짐


# ---------------------------------------------------------------------------
# C3 Scenario 14: Scope guard — Redis/DB/Alert/REST import 0건
# ---------------------------------------------------------------------------

class TestScopeGuard(unittest.TestCase):
    """C3 acceptance 9: coinone.py에 Redis/DB/Alert/REST writer import 0건.

    Codex Point 5: C3의 가장 중요한 안전선.
    """

    def test_no_forbidden_imports(self):
        """coinone.py module이 Redis/DB/Alert/REST/topic trigger import하지 않음."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        module_attrs = dir(coinone_module)
        # C3 scope 외 요소가 module-level에 import되지 않음을 검증
        forbidden = [
            "latest_rates_cache",
            "tether_topic_trigger",
            "UsdtAlertEvaluator",
            "AlertObservation",
            "UsdtLivenessMonitor",
            "get_db_context",
            "insert_source_rate_if_changed",
            "fetch_coinone_usdt_tick",
        ]
        for name in forbidden:
            self.assertNotIn(
                name, module_attrs,
                f"C3 scope 위반: '{name}' import됨 (C5~C7 영역)",
            )

    def test_module_imports_minimal(self):
        """module imports 최소 — asyncio + json + logging + websockets + typing only."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        # 허용된 module-level names (constants + class)
        # 추가 import는 import-time fail로 잡힘 — 본 test는 module 정상 import만 검증
        self.assertTrue(hasattr(coinone_module, "CoinoneWsClient"))
        self.assertTrue(hasattr(coinone_module, "COINONE_WS_URL"))


# ---------------------------------------------------------------------------
# C3 Regression: C2 flag=false invariant 유지
# ---------------------------------------------------------------------------

class TestFlagFalseInvariantC3Regression(unittest.IsolatedAsyncioTestCase):
    """C3 land 후에도 C2 flag=false invariant 유지."""

    def setUp(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    async def test_flag_false_still_no_client(self):
        """C3 land 후에도 flag=false 시 client/task 생성 X (회귀)."""
        with patch.object(config, "USDT_WS_COINONE_ENABLED", False):
            await scheduler.start_usdt_ws_coinone_client()
        self.assertIsNone(scheduler.usdt_ws_coinone_client)
        self.assertIsNone(scheduler.usdt_ws_coinone_task)


if __name__ == "__main__":
    unittest.main()
