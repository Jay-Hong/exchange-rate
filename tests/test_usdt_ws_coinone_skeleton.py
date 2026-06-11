"""USDT WS Coinone canary 단위 테스트 — Phase B.4 Stage C2-C4.

USDT_WS_DESIGN_PLAN §12.6 Phase B.4.
Bithumb Stage U2-U4 테스트 패턴 minimal 복제 (Codex 권장 — skeleton test는
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
    - start: _run_one_session 단일 실행 (C3 단독은 reconnect 없음)

C4 client behavior:
    - Constants 추가: PING_INTERVAL_SEC=300 / PING_TIMEOUT_SEC=5 / STALE_AFTER_SEC=360 /
      TICKER_FRESHNESS_WARNING_SEC=60 / RECONNECT_BACKOFF_SEQ
    - UsdtLivenessMonitor 재사용 (source-neutral, last_activity_at = max(tick, heartbeat))
    - 2-signal status 분리: _connection_status / _ticker_freshness_status
    - _ping_loop: application-level PING/PONG event-based (clear → send → wait_for)
    - _set_connection_status / _set_ticker_freshness_status: 전이 기반 1회 log (log flood 방지)
    - _run_one_session 확장: ping_task + is_stale check + ticker freshness transition
    - start: reconnect loop + backoff (Bithumb mirror)

테스트 시나리오 (C2 6개 + C3 8개 + C4 8개 = 22 class / 50 tests):
    [C2]
    1. flag=false → CoinoneWsClient 생성 안 함
    2. flag=true → task 생성됨
    3. 중복 start 방지
    4. shutdown 시 client/task 정리
    5. CoinoneWsClient.__init__ state
    6. CoinoneWsClient.start()이 _run_one_session 호출 + stop()으로 빠져나옴
    [C3]
    7. _build_subscribe_payload: Coinone single-dict form
    8. _parse_data_message: valid + invalid cases
    9. _handle_message: CONNECTED + session_id 캡처
    10. _handle_message: SUBSCRIBED log
    11. _handle_message: DATA first tick INFO + 이후 DEBUG
    12. _handle_message: PONG / ERROR / unknown safe log
    13. _run_one_session: subscribe send + recv → handle_message + stop_event 빠짐
    14. Scope guard: Redis/DB/Alert/REST writer import 0건
    [C4]
    15. ComputeBackoff: reconnect backoff sequence
    16. SetConnectionStatus: normal/reconnecting/stale 전이 + counter + log
    17. SetTickerFreshnessStatus: normal/warning 전이 + log throttle (transition 기반)
    18. PongHandlerSetsEvent: _handle_message PONG → _pong_event.set()
    19. PingLoopEventSequence: clear → send → wait_for (success + timeout)
    20. TickerSilenceNoReconnect: UsdtLivenessMonitor is_stale heartbeat fresh False
    21. ScopeGuardC4: Redis/DB/Alert/REST 미진입 + UsdtLivenessMonitor 의도적 import 허용
    22. C4ConstantsExist: 5개 constants + STALE_AFTER_SEC > PING_INTERVAL_SEC

실행:
    python -m unittest tests.test_usdt_ws_coinone_skeleton -v
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from app import config, scheduler
from app.latest_rates_cache import UsdtLatestWriteOutcome
from app.crawlers.usdt_ws.coinone import (
    COINONE_QUOTE_CURRENCY,
    COINONE_TARGET_CURRENCY,
    COINONE_WS_URL,
    MAX_PENDING_WRITES,
    PING_INTERVAL_SEC,
    PING_TIMEOUT_SEC,
    RECONNECT_BACKOFF_SEQ,
    RECONNECT_BACKOFF_TAIL,
    RECV_TIMEOUT_SEC,
    STALE_AFTER_SEC,
    TICKER_FRESHNESS_DEGRADED_SEC,
    TICKER_FRESHNESS_WARNING_SEC,
    CoinoneRedisWriter,
    CoinoneRestFallbackController,
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
# Scope guard — stage별 누적 허용 + C6b~C7 영역 forbidden
# ---------------------------------------------------------------------------

class TestScopeGuard(unittest.TestCase):
    """C6a 기준 scope guard: REST fallback / Alert evaluator만 forbidden.

    C3 신설 시 Redis/DB/Alert/REST 모두 forbidden. C4에서 UsdtLivenessMonitor 의도적 재사용,
    C5에서 latest_rates_cache / tether_topic_trigger 의도적 재사용, C6a에서 CoinoneDbWriter
    의도적 추가 (DB symbols는 _sync_db_write 함수 내부 lazy import이라 module-level 비노출) —
    모두 forbidden list에서 제외.
    C6b~C7 영역 (REST fallback helper / Alert evaluator)만 forbidden.
    """

    def test_no_forbidden_imports(self):
        """coinone.py module의 lazy import 항목 검증 (Phase B.4 complete 후).

        Stage별 누적 허용:
        - C4: UsdtLivenessMonitor (source-neutral 재사용)
        - C5: latest_rates_cache, tether_topic_trigger (Redis writer + topic trigger)
        - C6a: CoinoneDbWriter (DB symbols는 _sync_db_write 함수 내부 lazy import — module-level 비노출)
        - C6b: CoinoneRestFallbackController (fetch_coinone_usdt_tick는 lazy import — module-level 비노출)
        - C7: UsdtAlertEvaluator + AlertObservation (의도적 module-level import — Bithumb U7 mirror)

        Phase B.4 complete 후 forbidden은 lazy import로 처리되는 항목만 (module-level 노출 금지):
        - fetch_coinone_usdt_tick: _fetch_coinone_tick static에서 lazy import
        """
        import app.crawlers.usdt_ws.coinone as coinone_module
        module_attrs = dir(coinone_module)
        # lazy import 항목만 forbidden (module-level 비노출 검증)
        forbidden = [
            "fetch_coinone_usdt_tick",
        ]
        for name in forbidden:
            self.assertNotIn(
                name, module_attrs,
                f"scope 위반: '{name}' module-level 노출 (lazy import 영역)",
            )

    def test_module_imports_minimal(self):
        """C6a module imports: asyncio + datetime + json + logging + time + typing + websockets
        + UsdtLivenessMonitor + latest_rates_cache + tether_topic_trigger + CoinoneDbWriter.

        C4 update: time + UsdtLivenessMonitor (source-neutral 재사용) 추가됨.
        C5 update: datetime + latest_rates_cache + tether_topic_trigger 추가됨 (Redis writer + topic trigger 재사용).
        C6a update: CoinoneDbWriter class + DB_WRITE_WINDOW_SEC 추가됨 (DB symbols는 lazy import이라 module-level 비노출).
        C6b~C7 영역 import는 forbidden (TestScopeGuard.test_no_forbidden_imports에서 검증).
        본 test는 module 정상 import + 핵심 symbols 존재만 검증.
        """
        import app.crawlers.usdt_ws.coinone as coinone_module
        # 허용된 module-level names (constants + class + 재사용 helper)
        self.assertTrue(hasattr(coinone_module, "CoinoneWsClient"))
        self.assertTrue(hasattr(coinone_module, "COINONE_WS_URL"))
        # C4 추가: UsdtLivenessMonitor source-neutral 재사용
        self.assertTrue(hasattr(coinone_module, "UsdtLivenessMonitor"))


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


# ===========================================================================
# C4 — Connection liveness (PING/PONG) + Ticker freshness telemetry +
# 2-signal status 분리 + Reconnect loop
# ===========================================================================

class TestComputeBackoff(unittest.TestCase):
    """C4 acceptance: reconnect backoff sequence (Bithumb/Upbit 동일)."""

    def test_attempt_zero_returns_first(self):
        self.assertEqual(CoinoneWsClient._compute_backoff(0), RECONNECT_BACKOFF_SEQ[0])

    def test_within_sequence_range(self):
        for i, expected in enumerate(RECONNECT_BACKOFF_SEQ, start=1):
            self.assertEqual(CoinoneWsClient._compute_backoff(i), expected)

    def test_beyond_sequence_returns_tail(self):
        self.assertEqual(
            CoinoneWsClient._compute_backoff(len(RECONNECT_BACKOFF_SEQ) + 5),
            RECONNECT_BACKOFF_TAIL,
        )


class TestSetConnectionStatus(unittest.TestCase):
    """C4 acceptance 11: _connection_status 전이 + counter + log."""

    def setUp(self):
        self.client = CoinoneWsClient()

    def test_initial_state_normal(self):
        self.assertEqual(self.client._connection_status, "normal")

    def test_transition_changes_status_and_counter(self):
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            self.client._set_connection_status("reconnecting")
        self.assertEqual(self.client._connection_status, "reconnecting")
        self.assertEqual(self.client._status_transition_count["connection_reconnecting"], 1)
        self.assertTrue(any("connection_status normal → reconnecting" in m for m in cm.output))

    def test_no_transition_when_same_status(self):
        """동일 status 재호출 시 counter ++ 안 됨, log emit 안 됨."""
        # 첫 전이
        self.client._set_connection_status("stale")
        count_before = self.client._status_transition_count["connection_stale"]
        # 동일 호출
        self.client._set_connection_status("stale")
        self.assertEqual(self.client._status_transition_count["connection_stale"], count_before)


class TestSetTickerFreshnessStatus(unittest.TestCase):
    """C4 acceptance 12: _ticker_freshness_status 분리 + transition 1회 log (Codex Point 1, 2)."""

    def setUp(self):
        self.client = CoinoneWsClient()

    def test_initial_state_normal(self):
        self.assertEqual(self.client._ticker_freshness_status, "normal")

    def test_transition_to_warning_logs_once(self):
        """normal → warning 전이 시 1회 log + counter."""
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            self.client._set_ticker_freshness_status("warning")
        self.assertEqual(self.client._ticker_freshness_status, "warning")
        self.assertEqual(self.client._status_transition_count["ticker_warning"], 1)
        self.assertTrue(any("ticker_freshness_status normal → warning" in m for m in cm.output))

    def test_no_log_flood_when_already_warning(self):
        """warning 상태에서 재호출 시 log emit 안 됨 (Codex Point 1 — flood 방지)."""
        # 첫 전이 (log 1회)
        self.client._set_ticker_freshness_status("warning")
        count_before = self.client._status_transition_count["ticker_warning"]
        # 동일 호출 100회 — log 안 찍히고 counter 안 늘어남
        for _ in range(100):
            self.client._set_ticker_freshness_status("warning")
        self.assertEqual(self.client._status_transition_count["ticker_warning"], count_before)

    def test_warning_to_normal_logs_once(self):
        """warning → normal 복귀 시에도 1회 log (DATA 수신 시점)."""
        self.client._set_ticker_freshness_status("warning")
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            self.client._set_ticker_freshness_status("normal")
        self.assertEqual(self.client._ticker_freshness_status, "normal")
        self.assertTrue(any("ticker_freshness_status warning → normal" in m for m in cm.output))


class TestPongHandlerSetsEvent(unittest.IsolatedAsyncioTestCase):
    """C4 acceptance 2: _handle_message PONG → _pong_event.set() (Codex Point 3)."""

    async def test_pong_sets_event(self):
        client = CoinoneWsClient()
        self.assertFalse(client._pong_event.is_set())
        client._handle_message(json.dumps({"response_type": "PONG"}))
        self.assertTrue(client._pong_event.is_set())


class TestPingLoopEventSequence(unittest.IsolatedAsyncioTestCase):
    """C4 acceptance 1-3: PING/PONG clear → send → wait_for sequence (Codex Point 3).

    sleep 패치 — 5분 cycle 대기 없이 즉시 진행.
    """

    async def test_ping_send_then_pong_event_wait_success(self):
        """PING send 후 PONG event set → observe_heartbeat 호출.

        PING_INTERVAL_SEC을 짧은 값(0.01)으로 patch — asyncio.sleep mock 회피 (race ↓).
        polling 패턴으로 ping_loop 진행 확인.
        """
        client = CoinoneWsClient()
        sent_payloads = []

        async def fake_send(payload):
            sent_payloads.append(payload)
            # PONG 응답 simulate (handle_message 호출 안 하고 직접 event set)
            client._pong_event.set()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock(side_effect=fake_send)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.coinone.PING_INTERVAL_SEC", 0.01), \
             patch("app.crawlers.usdt_ws.coinone.PING_TIMEOUT_SEC", 0.1):
            ping_task = asyncio.create_task(client._ping_loop(mock_ws))
            # Polling: PING 1회 이상 보낼 때까지 (race 방지)
            for _ in range(200):  # ~1s max (5ms × 200)
                if len(sent_payloads) >= 1:
                    break
                await asyncio.sleep(0.005)
            client._stop_event.set()
            client._pong_event.set()  # ping_loop의 wait_for 풀어주기
            await asyncio.wait_for(ping_task, timeout=2.0)

        # PING payload 보냈는지 + observe_heartbeat 호출됐는지
        self.assertGreaterEqual(len(sent_payloads), 1)
        first = json.loads(sent_payloads[0])
        self.assertEqual(first, {"request_type": "PING"})
        # observe_heartbeat 호출되면 last_heartbeat_at 갱신됨
        self.assertIsNotNone(client._liveness.last_heartbeat_at)
        # ws.close 호출 안 됨 (정상 PONG)
        mock_ws.close.assert_not_called()

    async def test_pong_timeout_triggers_ws_close(self):
        """PONG event timeout (5s) 안 옴 → ws.close() (recv loop가 ConnectionClosed → reconnect)."""
        client = CoinoneWsClient()

        async def fake_send(payload):
            # PONG 응답 simulate 안 함 — event set 안 함 → wait_for timeout
            return None

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock(side_effect=fake_send)
        mock_ws.close = AsyncMock()

        # PING_INTERVAL_SEC 짧게 + PING_TIMEOUT_SEC 짧게 → 빠른 timeout 발화
        with patch("app.crawlers.usdt_ws.coinone.PING_INTERVAL_SEC", 0.01), \
             patch("app.crawlers.usdt_ws.coinone.PING_TIMEOUT_SEC", 0.05):
            ping_task = asyncio.create_task(client._ping_loop(mock_ws))
            await asyncio.wait_for(ping_task, timeout=1.0)

        # PONG timeout → ws.close 호출됨
        mock_ws.close.assert_called()


class TestTickerSilenceNoReconnect(unittest.IsolatedAsyncioTestCase):
    """C4 acceptance 14: ticker silence alone → reconnect never (Codex 핵심 강조).

    UsdtLivenessMonitor.is_stale은 heartbeat fresh면 ticker silence 무관 False.
    """

    async def test_is_stale_false_when_heartbeat_fresh(self):
        """heartbeat이 최근이면 ticker silence 길어도 is_stale=False (재사용 monitor 검증)."""
        client = CoinoneWsClient()
        client._liveness.observe_heartbeat(time.time())
        # 1시간 전 tick (ticker silence 매우 길음)
        client._liveness.last_tick_at = time.time() - 3600.0
        # heartbeat fresh이므로 stale 아님 (last_activity_at = max(tick, heartbeat))
        self.assertFalse(client._liveness.is_stale(time.time(), STALE_AFTER_SEC))


class TestScopeGuardC4(unittest.TestCase):
    """C4 acceptance regression: DB/Alert/REST writer/REST probe 미진입.

    C4 신설 시: freshness telemetry only, Redis/DB/Alert/REST 모두 미진입.
    C5 update: Redis writer + topic trigger는 C5 의도적 진입 (forbidden 제외). DB/Alert/REST만 forbidden.
    """

    def test_no_forbidden_imports_after_c4(self):
        """coinone.py module의 lazy import 항목 검증 (Phase B.4 complete 후).

        C5~C7 누적: latest_rates_cache / tether_topic_trigger / CoinoneRestFallbackController /
        UsdtAlertEvaluator / AlertObservation 모두 의도적 module-level import.
        lazy import (module-level 비노출)만 forbidden — fetch_coinone_usdt_tick.
        """
        import app.crawlers.usdt_ws.coinone as coinone_module
        module_attrs = dir(coinone_module)
        forbidden = [
            "fetch_coinone_usdt_tick",
        ]
        for name in forbidden:
            self.assertNotIn(
                name, module_attrs,
                f"scope 위반: '{name}' module-level 노출 (lazy import 영역)",
            )

    def test_usdt_liveness_monitor_imported_for_reuse(self):
        """UsdtLivenessMonitor는 C4에서 source-neutral 재사용 import 추가."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertTrue(hasattr(coinone_module, "UsdtLivenessMonitor"))


class TestC4ConstantsExist(unittest.TestCase):
    """C4 acceptance: constants 5개 정의."""

    def test_ping_interval_300(self):
        self.assertEqual(PING_INTERVAL_SEC, 300.0)

    def test_ping_timeout_5(self):
        self.assertEqual(PING_TIMEOUT_SEC, 5.0)

    def test_stale_after_360(self):
        """STALE_AFTER_SEC > PING_INTERVAL_SEC — Codex 강조 (false stale 방지)."""
        self.assertGreater(STALE_AFTER_SEC, PING_INTERVAL_SEC)
        self.assertEqual(STALE_AFTER_SEC, 360.0)

    def test_ticker_freshness_warning_60(self):
        """telemetry warning threshold 60s (action X, Codex Point 1)."""
        self.assertEqual(TICKER_FRESHNESS_WARNING_SEC, 60.0)


# time module 사용 (C4 _run_one_session test에서 필요)
import time  # noqa: E402


# ===========================================================================
# C5 — CoinoneRedisWriter + topic trigger + _run_one_session valid tick wiring
# ===========================================================================

class TestCoinoneRedisWriterScheduleSuccess(unittest.IsolatedAsyncioTestCase):
    """C5 acceptance 1-2, 5: schedule → _write_async → to_thread(helper) → timestamp_ms KST ISO."""

    async def test_schedule_calls_helper_with_kst_iso(self):
        """schedule(tick) → set_latest_usdt_rate_from_sync_job 호출 + timestamp_ms KST ISO 변환 검증.

        Codex Point 2: timestamp_ms=1779106625946 → KST ISO 문자열 전달 확인.
        """
        from app.crawlers.usdt_ws.coinone import CoinoneRedisWriter
        writer = CoinoneRedisWriter()
        tick = {
            "source": "coinone",
            "asset": "usdt-krw",
            "rate": 1487.0,
            "timestamp_ms": 1779106625946,
        }
        captured_kwargs = {}

        def fake_helper(*, source, asset, rate, timestamp):
            captured_kwargs["source"] = source
            captured_kwargs["asset"] = asset
            captured_kwargs["rate"] = rate
            captured_kwargs["timestamp"] = timestamp
            return UsdtLatestWriteOutcome.SET

        # tether_topic_trigger는 success 시 호출됨 — spy
        trigger_calls = []

        def fake_trigger(*, source, asset, reason):
            trigger_calls.append({"source": source, "asset": asset, "reason": reason})

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=fake_helper,
        ), patch(
            "app.crawlers.usdt_ws.coinone.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=fake_trigger,
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        # helper 호출 인자 검증
        self.assertEqual(captured_kwargs["source"], "coinone")
        self.assertEqual(captured_kwargs["asset"], "usdt-krw")
        self.assertEqual(captured_kwargs["rate"], 1487.0)
        # timestamp_ms 1779106625946 (ms) → KST ISO. ms → s = 1779106625.946
        # datetime.fromtimestamp(1779106625.946, tz=KST).isoformat() 형식 검증
        ts = captured_kwargs["timestamp"]
        self.assertIsInstance(ts, str)
        self.assertIn("+09:00", ts)  # KST ISO
        # 정확한 변환 결과 검증 (round-trip)
        from datetime import datetime, timezone, timedelta
        expected_kst = datetime.fromtimestamp(
            1779106625.946, tz=timezone(timedelta(hours=9)),
        ).isoformat()
        self.assertEqual(ts, expected_kst)

        # topic trigger 호출 검증 (success 후)
        self.assertEqual(len(trigger_calls), 1)
        self.assertEqual(trigger_calls[0]["source"], "coinone")
        self.assertEqual(trigger_calls[0]["asset"], "usdt-krw")


class TestCoinoneRedisWriterHelperFailureIsolated(unittest.IsolatedAsyncioTestCase):
    """C5 acceptance 7: helper False / exception → log only (WS session 영향 X) + topic trigger 미호출."""

    async def test_helper_returns_false_no_trigger(self):
        """helper False 반환 → log warning + topic trigger 미호출."""
        from app.crawlers.usdt_ws.coinone import CoinoneRedisWriter
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}
        trigger_calls = []

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=UsdtLatestWriteOutcome.FAILED,
        ), patch(
            "app.crawlers.usdt_ws.coinone.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=lambda **kw: trigger_calls.append(kw),
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        # helper False → trigger 호출 안 됨
        self.assertEqual(len(trigger_calls), 0)

    async def test_helper_exception_isolated(self):
        """helper exception → log only (WS/writer 영향 X) + trigger 미호출."""
        from app.crawlers.usdt_ws.coinone import CoinoneRedisWriter
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}
        trigger_calls = []

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=RuntimeError("Redis down"),
        ), patch(
            "app.crawlers.usdt_ws.coinone.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=lambda **kw: trigger_calls.append(kw),
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        self.assertEqual(len(trigger_calls), 0)


class TestCoinoneRedisWriterTriggerExceptionIsolated(unittest.IsolatedAsyncioTestCase):
    """C5 acceptance 8: trigger 예외 → log only (writer/WS 영향 X). helper success 가정."""

    async def test_trigger_exception_writer_continues(self):
        from app.crawlers.usdt_ws.coinone import CoinoneRedisWriter
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}
        helper_calls = []

        def fake_helper(**kw):
            helper_calls.append(kw)
            return UsdtLatestWriteOutcome.SET

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=fake_helper,
        ), patch(
            "app.crawlers.usdt_ws.coinone.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=RuntimeError("trigger queue full"),
        ):
            # trigger exception 발생해도 writer/await 정상 종료
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        # helper는 정상 호출됨
        self.assertEqual(len(helper_calls), 1)


class TestCoinoneRedisWriterSaturation(unittest.IsolatedAsyncioTestCase):
    """C5 acceptance 4: saturation check — MAX_PENDING_WRITES 초과 시 skip + warning."""

    async def test_saturation_skips_new_schedule(self):
        from app.crawlers.usdt_ws.coinone import CoinoneRedisWriter, MAX_PENDING_WRITES
        writer = CoinoneRedisWriter()

        # MAX_PENDING_WRITES 만큼 fake task 채움 (실제 _write_async가 끝나기 전 상태 simulate)
        async def slow_helper(**kw):
            await asyncio.sleep(10.0)  # 일부러 hang
            return UsdtLatestWriteOutcome.SET

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=slow_helper,
        ):
            tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}
            # MAX_PENDING_WRITES 회 schedule — 모두 진행 중
            for _ in range(MAX_PENDING_WRITES):
                writer.schedule(tick)
            self.assertEqual(len(writer._tasks), MAX_PENDING_WRITES)

            # 다음 schedule → saturated, skip
            with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="WARNING") as cm:
                writer.schedule(tick)
            self.assertEqual(len(writer._tasks), MAX_PENDING_WRITES)  # 변화 없음
            self.assertTrue(any("saturated" in m for m in cm.output))

            # cleanup — task cancel
            for task in list(writer._tasks):
                task.cancel()
            await asyncio.gather(*writer._tasks, return_exceptions=True)


class TestCoinoneRedisWriterClose(unittest.IsolatedAsyncioTestCase):
    """C5 acceptance 9: close drain + timeout 후 cancel + _tasks.clear()."""

    async def test_close_drains_pending_then_clears(self):
        from app.crawlers.usdt_ws.coinone import CoinoneRedisWriter
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=UsdtLatestWriteOutcome.SET,
        ), patch(
            "app.crawlers.usdt_ws.coinone.tether_topic_trigger.request_tether_topic_trigger",
        ):
            writer.schedule(tick)
            writer.schedule(tick)
            self.assertGreater(len(writer._tasks), 0)
            await writer.close(timeout=2.0)
            self.assertEqual(len(writer._tasks), 0)

    async def test_close_timeout_cancels_pending(self):
        """timeout 안에 drain 안 되면 cancel + _tasks.clear() 보장."""
        from app.crawlers.usdt_ws.coinone import CoinoneRedisWriter
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}

        async def slow_helper(**kw):
            await asyncio.sleep(10.0)
            return UsdtLatestWriteOutcome.SET

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=slow_helper,
        ):
            writer.schedule(tick)
            self.assertGreater(len(writer._tasks), 0)
            # timeout 0.1s — slow_helper 끝나지 않음
            await writer.close(timeout=0.1)
            self.assertEqual(len(writer._tasks), 0)  # cancel 후 clear


class TestRunOneSessionRedisWiring(unittest.IsolatedAsyncioTestCase):
    """C5 acceptance 11 (Codex 추가): _run_one_session valid DATA path에서 observe_tick 후 _redis_writer.schedule 호출."""

    async def test_valid_data_calls_schedule_on_redis_writer(self):
        client = CoinoneWsClient()
        schedule_calls = []

        # _redis_writer.schedule mock — 호출 검증
        client._redis_writer.schedule = MagicMock(
            side_effect=lambda tick: schedule_calls.append(tick),
        )
        # C6a regression: _db_writer.schedule도 mock — 실제 timer/DB 호출 회피.
        # C7 regression: _alert_evaluator.schedule도 mock — 실제 task 생성 회피.
        # 본 test는 Redis writer wiring만 검증, DB/Alert는 별도 영역.
        client._db_writer.schedule = MagicMock()
        client._alert_evaluator.schedule = MagicMock()

        recv_raws = [
            json.dumps({"response_type": "CONNECTED", "data": {"session_id": "abc"}}),
            json.dumps({"response_type": "SUBSCRIBED", "channel": "TICKER",
                        "data": {"quote_currency": "KRW", "target_currency": "USDT"}}),
            json.dumps({
                "response_type": "DATA",
                "data": {
                    "quote_currency": "KRW", "target_currency": "USDT",
                    "last": "1487", "timestamp": 1779106625946,
                },
            }),
        ]
        recv_count = {"calls": 0}

        async def fake_recv():
            i = recv_count["calls"]
            recv_count["calls"] += 1
            if i < len(recv_raws):
                return recv_raws[i]
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.coinone.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # DATA 1건만 schedule 호출됨 (CONNECTED/SUBSCRIBED는 schedule 호출 X)
        self.assertEqual(len(schedule_calls), 1)
        self.assertEqual(schedule_calls[0]["source"], "coinone")
        self.assertEqual(schedule_calls[0]["asset"], "usdt-krw")
        self.assertEqual(schedule_calls[0]["rate"], 1487.0)
        self.assertEqual(schedule_calls[0]["timestamp_ms"], 1779106625946)


class TestScopeGuardC5(unittest.TestCase):
    """C5 acceptance: C5 의도적 import 허용 + C6b~C7 영역 forbidden.

    C6a update: CoinoneDbWriter 추가 — DB symbols는 _sync_db_write 함수 내부 import이라
    module-level forbidden 점검 영향 X.
    """

    def test_redis_topic_imports_allowed(self):
        """C5: latest_rates_cache, tether_topic_trigger, CoinoneRedisWriter 의도적 import 허용."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertTrue(hasattr(coinone_module, "latest_rates_cache"))
        self.assertTrue(hasattr(coinone_module, "tether_topic_trigger"))
        self.assertTrue(hasattr(coinone_module, "CoinoneRedisWriter"))
        self.assertTrue(hasattr(coinone_module, "TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS"))

    def test_rest_alert_forbidden(self):
        """Phase B.4 complete 후 lazy import (module-level 비노출)만 forbidden.

        C6b/C7 의도적 module-level: CoinoneRestFallbackController / UsdtAlertEvaluator /
        AlertObservation. lazy: fetch_coinone_usdt_tick.
        """
        import app.crawlers.usdt_ws.coinone as coinone_module
        module_attrs = dir(coinone_module)
        forbidden = [
            "fetch_coinone_usdt_tick",
        ]
        for name in forbidden:
            self.assertNotIn(
                name, module_attrs,
                f"scope 위반: '{name}' module-level 노출 (lazy import 영역)",
            )


# ===========================================================================
# C6a — CoinoneDbWriter + 1s window debounce + _run_one_session DB wiring
# ===========================================================================

class TestCoinoneDbWriterScheduleAndFlush(unittest.IsolatedAsyncioTestCase):
    """C6a acceptance 1-2: schedule → window timer → _flush_after_window → _sync_db_write."""

    async def test_schedule_then_flush_calls_helper(self):
        """schedule(tick) → window 후 _sync_db_write 호출 + crud.insert_source_rate_if_changed."""
        from app.crawlers.usdt_ws.coinone import CoinoneDbWriter
        # window 짧게 (0.05s)
        writer = CoinoneDbWriter(window_sec=0.05)
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}
        helper_calls = []

        def fake_insert(*, db, source, asset, rate, timestamp=None):
            helper_calls.append({"source": source, "asset": asset, "rate": rate})

        # crud.insert_source_rate_if_changed + get_db_context mock
        from unittest.mock import MagicMock
        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=fake_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            writer.schedule(tick)
            # window 지나가도록 대기
            await asyncio.sleep(0.15)

        self.assertEqual(len(helper_calls), 1)
        self.assertEqual(helper_calls[0]["source"], "coinone")
        self.assertEqual(helper_calls[0]["asset"], "usdt-krw")
        self.assertEqual(helper_calls[0]["rate"], 1487.0)

    async def test_multiple_ticks_in_window_only_last_flushed(self):
        """window 내 여러 tick → 마지막 1건만 helper 호출 (debounce 의미)."""
        from app.crawlers.usdt_ws.coinone import CoinoneDbWriter
        writer = CoinoneDbWriter(window_sec=0.1)
        helper_calls = []

        def fake_insert(*, db, source, asset, rate, timestamp=None):
            helper_calls.append(rate)

        from unittest.mock import MagicMock
        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=fake_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            # 빠르게 3 tick — window 내
            writer.schedule({"source": "coinone", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            writer.schedule({"source": "coinone", "asset": "usdt-krw", "rate": 2.0, "timestamp_ms": 2})
            writer.schedule({"source": "coinone", "asset": "usdt-krw", "rate": 3.0, "timestamp_ms": 3})
            await asyncio.sleep(0.2)

        # window 후 1회만 helper 호출 + 마지막 tick (rate=3.0)
        self.assertEqual(len(helper_calls), 1)
        self.assertEqual(helper_calls[0], 3.0)


class TestCoinoneDbWriterRaceWithNewTick(unittest.IsolatedAsyncioTestCase):
    """C6a acceptance 4: write 진행 중 새 tick → finally에서 새 timer 예약 (누락 방지).

    Codex hang 정정: asyncio.Event는 event loop 종속이라 asyncio.to_thread 내부
    다른 thread에서 set()/is_set() 호출 시 thread-safe X. threading.Event 사용 +
    event loop 쪽에서는 asyncio.to_thread(event.wait, timeout)으로 기다림.
    """

    async def test_new_tick_during_write_schedules_next_window(self):
        """slow helper 진행 중 새 tick → finally에서 새 timer → 다음 window 종료 후 2번째 helper 호출."""
        import threading
        from app.crawlers.usdt_ws.coinone import CoinoneDbWriter
        writer = CoinoneDbWriter(window_sec=0.05)
        helper_calls = []

        # asyncio.Event 대신 threading.Event (thread-safe)
        first_write_started = threading.Event()
        first_write_can_finish = threading.Event()

        def slow_insert(*, db, source, asset, rate, timestamp=None):
            helper_calls.append(rate)
            # thread 내부 — threading.Event는 thread-safe
            first_write_started.set()
            # 첫 write hang — 다음 tick schedule 시점 race 시뮬레이션
            if rate == 1.0:
                # threading.Event.wait — bounded (test timeout 방지)
                first_write_can_finish.wait(timeout=2.0)

        from unittest.mock import MagicMock
        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=slow_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            # 첫 tick
            writer.schedule({"source": "coinone", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            # threading.Event.wait를 thread에서 — event loop 안 막음
            await asyncio.wait_for(
                asyncio.to_thread(first_write_started.wait, 2.0),
                timeout=3.0,
            )
            # 첫 write 진행 중 두 번째 tick 도착
            writer.schedule({"source": "coinone", "asset": "usdt-krw", "rate": 2.0, "timestamp_ms": 2})
            # 첫 write 완료
            first_write_can_finish.set()
            # 다음 window 완료 대기
            await asyncio.sleep(0.2)

        # 2번 호출됨 (race 방지 — 두 번째 tick 누락 X)
        self.assertEqual(len(helper_calls), 2)
        self.assertIn(1.0, helper_calls)
        self.assertIn(2.0, helper_calls)


class TestCoinoneDbWriterHelperExceptionIsolated(unittest.IsolatedAsyncioTestCase):
    """C6a acceptance 5: helper exception → log only (WS session 영향 X)."""

    async def test_helper_exception_isolated(self):
        from app.crawlers.usdt_ws.coinone import CoinoneDbWriter
        writer = CoinoneDbWriter(window_sec=0.05)

        def failing_insert(*, db, source, asset, rate, timestamp=None):
            raise RuntimeError("DB connection lost")

        from unittest.mock import MagicMock
        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=failing_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            # 예외 발생해도 schedule + 다음 close 정상
            writer.schedule({"source": "coinone", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            await asyncio.sleep(0.15)
            # writer 상태 정상 — close 호출 가능
            await writer.close()


class TestCoinoneDbWriterCloseImmediateFlush(unittest.IsolatedAsyncioTestCase):
    """C6a acceptance 6: close 시 pending tick 즉시 flush (1초 window 안 기다림)."""

    async def test_close_flushes_pending_immediately(self):
        """schedule 직후 close → window 안 기다리고 pending 즉시 flush."""
        from app.crawlers.usdt_ws.coinone import CoinoneDbWriter
        writer = CoinoneDbWriter(window_sec=10.0)  # 의도적으로 긴 window
        helper_calls = []

        def fake_insert(*, db, source, asset, rate, timestamp=None):
            helper_calls.append(rate)

        from unittest.mock import MagicMock
        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=fake_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            writer.schedule({"source": "coinone", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            # 10s window 기다리지 않고 즉시 close
            await asyncio.wait_for(writer.close(), timeout=1.0)

        # window 무시하고 즉시 flush — 1회 호출
        self.assertEqual(len(helper_calls), 1)
        self.assertEqual(helper_calls[0], 1.0)


class TestRunOneSessionDbWiring(unittest.IsolatedAsyncioTestCase):
    """C6a acceptance 7-8: _run_one_session valid DATA path에서 observe_tick → Redis → DB wiring + finally 순서."""

    async def test_valid_data_calls_db_writer_schedule(self):
        client = CoinoneWsClient()
        redis_calls = []
        db_calls = []

        client._redis_writer.schedule = MagicMock(
            side_effect=lambda tick: redis_calls.append(tick),
        )
        client._db_writer.schedule = MagicMock(
            side_effect=lambda tick: db_calls.append(tick),
        )
        # C7 regression: _alert_evaluator.schedule도 mock — 실제 task 생성 회피.
        client._alert_evaluator.schedule = MagicMock()

        recv_raws = [
            json.dumps({"response_type": "CONNECTED", "data": {"session_id": "abc"}}),
            json.dumps({"response_type": "SUBSCRIBED", "channel": "TICKER",
                        "data": {"quote_currency": "KRW", "target_currency": "USDT"}}),
            json.dumps({
                "response_type": "DATA",
                "data": {
                    "quote_currency": "KRW", "target_currency": "USDT",
                    "last": "1487", "timestamp": 1779106625946,
                },
            }),
        ]
        recv_count = {"calls": 0}

        async def fake_recv():
            i = recv_count["calls"]
            recv_count["calls"] += 1
            if i < len(recv_raws):
                return recv_raws[i]
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.coinone.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # DATA 1건만 schedule 호출 — Redis + DB 둘 다
        self.assertEqual(len(redis_calls), 1)
        self.assertEqual(len(db_calls), 1)
        # 동일 tick 전달 검증
        self.assertEqual(redis_calls[0], db_calls[0])

    # NOTE: test_finally_order_db_before_redis (C6a 시점, db/redis 2개 순서만 검증)는
    # C7 superset인 TestFinallyOrderAlertBeforeRedis (4개 순서: fallback→db→alert→redis)로 대체됨.


class TestScopeGuardC6a(unittest.TestCase):
    """C6a acceptance: CoinoneDbWriter 의도적 + C6b~C7 영역 forbidden 유지.

    C6b update: CoinoneRestFallbackController 의도적 + fetch_coinone_usdt_tick lazy import (module-level 비노출).
    C7 영역 (Alert)만 forbidden.
    """

    def test_db_writer_class_present(self):
        """C6a: CoinoneDbWriter class module-level 노출."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertTrue(hasattr(coinone_module, "CoinoneDbWriter"))
        self.assertTrue(hasattr(coinone_module, "DB_WRITE_WINDOW_SEC"))

    def test_db_symbols_not_module_level(self):
        """get_db_context, insert_source_rate_if_changed는 _sync_db_write 함수 내부 import."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        module_attrs = dir(coinone_module)
        self.assertNotIn("get_db_context", module_attrs)
        self.assertNotIn("insert_source_rate_if_changed", module_attrs)

    def test_rest_alert_still_forbidden(self):
        """Phase B.4 complete 후 lazy import (module-level 비노출)만 forbidden.

        C7 update: UsdtAlertEvaluator / AlertObservation 의도적 module-level (Bithumb U7 mirror).
        lazy: fetch_coinone_usdt_tick.
        """
        import app.crawlers.usdt_ws.coinone as coinone_module
        module_attrs = dir(coinone_module)
        # fetch_coinone_usdt_tick은 lazy import이라 module-level 비노출
        self.assertNotIn("fetch_coinone_usdt_tick", module_attrs)


# ===========================================================================
# C6b — fetch_coinone_usdt_tick + CoinoneRestFallbackController + DEGRADED hook
# ===========================================================================

class TestFetchCoinoneUsdtTick(unittest.TestCase):
    """C6b acceptance: REST normalized helper (Bithumb fetch_*_usdt_tick mirror)."""

    def test_valid_response_returns_normalized_tick(self):
        from app.crawlers.usdt_sources import fetch_coinone_usdt_tick

        # 실측 (2026-05-19) 기반 mock response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": "success",
            "tickers": [{
                "quote_currency": "krw",  # lowercase (실측)
                "target_currency": "usdt",
                "timestamp": 1779179058028,
                "last": "1488.0",
            }],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            tick = fetch_coinone_usdt_tick()

        self.assertEqual(tick, {
            "source": "coinone",
            "asset": "usdt-krw",
            "rate": 1488.0,
            "timestamp_ms": 1779179058028,
        })
        self.assertIsInstance(tick["rate"], float)
        self.assertIsInstance(tick["timestamp_ms"], int)

    def test_uppercase_currency_also_accepted(self):
        """case-insensitive 검증 — WS는 대문자, REST는 소문자라 둘 다 허용."""
        from app.crawlers.usdt_sources import fetch_coinone_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": "success",
            "tickers": [{
                "quote_currency": "KRW",
                "target_currency": "USDT",
                "timestamp": 1779179058028,
                "last": "1488.0",
            }],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            tick = fetch_coinone_usdt_tick()

        self.assertIsNotNone(tick)
        self.assertEqual(tick["rate"], 1488.0)

    def test_result_not_success_returns_none(self):
        from app.crawlers.usdt_sources import fetch_coinone_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": "error",
            "error_code": "100",
            "tickers": [],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            self.assertIsNone(fetch_coinone_usdt_tick())

    def test_currency_mismatch_returns_none(self):
        from app.crawlers.usdt_sources import fetch_coinone_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": "success",
            "tickers": [{
                "quote_currency": "krw",
                "target_currency": "btc",  # USDT 아님
                "timestamp": 1779179058028,
                "last": "1488.0",
            }],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            self.assertIsNone(fetch_coinone_usdt_tick())

    def test_zero_rate_returns_none(self):
        from app.crawlers.usdt_sources import fetch_coinone_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": "success",
            "tickers": [{
                "quote_currency": "krw",
                "target_currency": "usdt",
                "timestamp": 1779179058028,
                "last": "0",
            }],
        }
        mock_response.raise_for_status = MagicMock()

        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            self.assertIsNone(fetch_coinone_usdt_tick())

    def test_request_exception_returns_none(self):
        from app.crawlers.usdt_sources import fetch_coinone_usdt_tick
        import requests

        with patch(
            "app.crawlers.usdt_sources.requests.get",
            side_effect=requests.RequestException("network"),
        ):
            self.assertIsNone(fetch_coinone_usdt_tick())


class TestCoinoneRestFallbackControllerScheduleProbe(unittest.IsolatedAsyncioTestCase):
    """C6b acceptance: schedule_probe → REST → fanout (Redis + DB)."""

    async def test_schedule_probe_calls_writers_on_success(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        redis_writer = CoinoneRedisWriter()
        db_writer = CoinoneDbWriter()
        controller = CoinoneRestFallbackController(
            redis_writer=redis_writer,
            db_writer=db_writer,
            alert_evaluator=MagicMock(),
            cooldown_sec=0.1,
            probe_timeout_sec=2.0,
        )

        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1}
        redis_writer.schedule = MagicMock()
        db_writer.schedule = MagicMock()

        with patch(
            "app.crawlers.usdt_sources.fetch_coinone_usdt_tick",
            return_value=tick,
        ):
            controller.schedule_probe(reason="test")
            # probe task 실행 대기
            for _ in range(100):
                if not controller._in_flight:
                    break
                await asyncio.sleep(0.01)

        redis_writer.schedule.assert_called_once_with(tick)
        db_writer.schedule.assert_called_once_with(tick)

    async def test_schedule_probe_no_writers_on_helper_none(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        redis_writer = CoinoneRedisWriter()
        db_writer = CoinoneDbWriter()
        controller = CoinoneRestFallbackController(
            redis_writer=redis_writer,
            db_writer=db_writer,
            alert_evaluator=MagicMock(),
            cooldown_sec=0.1,
            probe_timeout_sec=2.0,
        )

        redis_writer.schedule = MagicMock()
        db_writer.schedule = MagicMock()

        with patch(
            "app.crawlers.usdt_sources.fetch_coinone_usdt_tick",
            return_value=None,  # REST 실패 simulate
        ):
            controller.schedule_probe(reason="test")
            for _ in range(100):
                if not controller._in_flight:
                    break
                await asyncio.sleep(0.01)

        redis_writer.schedule.assert_not_called()
        db_writer.schedule.assert_not_called()


class TestCoinoneRestFallbackControllerCooldown(unittest.IsolatedAsyncioTestCase):
    """C6b acceptance: cooldown active 시 schedule_probe skip."""

    async def test_cooldown_skips_schedule(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        redis_writer = CoinoneRedisWriter()
        db_writer = CoinoneDbWriter()
        controller = CoinoneRestFallbackController(
            redis_writer=redis_writer,
            db_writer=db_writer,
            alert_evaluator=MagicMock(),
            cooldown_sec=10.0,
            probe_timeout_sec=2.0,
        )
        # 인위적으로 cooldown 설정
        controller._cooldown_until = time.time() + 10.0

        redis_writer.schedule = MagicMock()
        db_writer.schedule = MagicMock()

        with patch("app.crawlers.usdt_sources.fetch_coinone_usdt_tick") as mock_fetch:
            controller.schedule_probe(reason="cooldown_test")
            await asyncio.sleep(0.05)

        # cooldown skip → REST 호출 안 됨
        mock_fetch.assert_not_called()
        redis_writer.schedule.assert_not_called()
        self.assertFalse(controller._in_flight)


class TestCoinoneRestFallbackControllerInFlight(unittest.IsolatedAsyncioTestCase):
    """C6b acceptance: in-flight 시 schedule_probe skip."""

    async def test_in_flight_skips_schedule(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        redis_writer = CoinoneRedisWriter()
        db_writer = CoinoneDbWriter()
        controller = CoinoneRestFallbackController(
            redis_writer=redis_writer,
            db_writer=db_writer,
            alert_evaluator=MagicMock(),
            cooldown_sec=0.1,
            probe_timeout_sec=2.0,
        )

        # 첫 probe — slow fetch로 in-flight 상태 유지
        async def slow_fetch():
            await asyncio.sleep(0.3)
            return None

        async def slow_run_probe(reason):
            controller._in_flight = True
            try:
                await slow_fetch()
            finally:
                controller._in_flight = False
                controller._cooldown_until = time.time() + controller._cooldown_sec

        controller._run_probe = slow_run_probe

        controller.schedule_probe(reason="first")
        # 즉시 두 번째 schedule — in-flight skip
        await asyncio.sleep(0.05)
        self.assertTrue(controller._in_flight)
        # 두 번째 schedule 호출
        with self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="DEBUG"):
            controller.schedule_probe(reason="second")
        # pending_task는 첫 호출만 — 두 번째는 skip되어 새 task 안 만들어짐
        await asyncio.sleep(0.4)


class TestCoinoneRestFallbackControllerResetCooldown(unittest.TestCase):
    """C6b acceptance: reset_cooldown → cooldown_until=0."""

    def test_reset_cooldown_clears(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        controller = CoinoneRestFallbackController(
            redis_writer=CoinoneRedisWriter(),
            db_writer=CoinoneDbWriter(),
            alert_evaluator=MagicMock(),
        )
        controller._cooldown_until = time.time() + 100.0
        controller.reset_cooldown()
        self.assertEqual(controller._cooldown_until, 0.0)


class TestCoinoneRestFallbackControllerProbeFailure(unittest.IsolatedAsyncioTestCase):
    """C6b acceptance: probe 실패도 cooldown 적용."""

    async def test_probe_timeout_applies_cooldown(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        controller = CoinoneRestFallbackController(
            redis_writer=CoinoneRedisWriter(),
            db_writer=CoinoneDbWriter(),
            alert_evaluator=MagicMock(),
            cooldown_sec=5.0,
            probe_timeout_sec=0.05,  # 즉시 timeout
        )

        # fetch_coinone_usdt_tick을 hang시켜 asyncio.wait_for timeout 발화
        def hang_helper():
            time.sleep(2.0)
            return None

        with patch("app.crawlers.usdt_sources.fetch_coinone_usdt_tick", side_effect=hang_helper):
            controller.schedule_probe(reason="timeout_test")
            await asyncio.sleep(0.2)

        # cooldown 적용됨 (timeout 이후)
        self.assertGreater(controller._cooldown_until, time.time())


class TestSetTickerFreshnessStatusFallbackHook(unittest.IsolatedAsyncioTestCase):
    """C6b acceptance: _set_ticker_freshness_status hook (degraded → schedule_probe / normal → reset_cooldown)."""

    async def test_degraded_transition_schedules_probe(self):
        client = CoinoneWsClient()
        # _fallback_controller.schedule_probe mock
        client._fallback_controller.schedule_probe = MagicMock()
        client._fallback_controller.reset_cooldown = MagicMock()

        # normal → warning (probe 호출 X)
        client._set_ticker_freshness_status("warning")
        client._fallback_controller.schedule_probe.assert_not_called()

        # warning → degraded (probe 호출됨)
        client._set_ticker_freshness_status("degraded")
        client._fallback_controller.schedule_probe.assert_called_once()
        call_kwargs = client._fallback_controller.schedule_probe.call_args
        self.assertIn("ticker_degraded", str(call_kwargs))

    async def test_normal_recovery_resets_cooldown(self):
        client = CoinoneWsClient()
        client._fallback_controller.schedule_probe = MagicMock()
        client._fallback_controller.reset_cooldown = MagicMock()

        # warning → normal 복귀
        client._set_ticker_freshness_status("warning")
        client._set_ticker_freshness_status("normal")
        client._fallback_controller.reset_cooldown.assert_called_once()

    async def test_degraded_to_normal_resets_cooldown(self):
        client = CoinoneWsClient()
        client._fallback_controller.schedule_probe = MagicMock()
        client._fallback_controller.reset_cooldown = MagicMock()

        # normal → warning → degraded → normal (degraded 복귀)
        client._set_ticker_freshness_status("warning")
        client._set_ticker_freshness_status("degraded")
        client._fallback_controller.schedule_probe.assert_called_once()
        client._fallback_controller.reset_cooldown.reset_mock()
        client._set_ticker_freshness_status("normal")
        client._fallback_controller.reset_cooldown.assert_called_once()


class TestScopeGuardC6b(unittest.TestCase):
    """C6b acceptance: CoinoneRestFallbackController 의도적 + C7 영역 (Alert) forbidden 유지."""

    def test_fallback_controller_class_present(self):
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertTrue(hasattr(coinone_module, "CoinoneRestFallbackController"))
        self.assertTrue(hasattr(coinone_module, "TICKER_FRESHNESS_DEGRADED_SEC"))
        self.assertTrue(hasattr(coinone_module, "FALLBACK_COOLDOWN_SEC"))
        self.assertTrue(hasattr(coinone_module, "FALLBACK_PROBE_TIMEOUT_SEC"))

    def test_fetch_coinone_usdt_tick_lazy_import(self):
        """fetch_coinone_usdt_tick은 _fetch_coinone_tick static에서 lazy import — module-level 비노출."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertNotIn("fetch_coinone_usdt_tick", dir(coinone_module))

    def test_alert_evaluator_module_level_after_c7(self):
        """C7: UsdtAlertEvaluator + AlertObservation 의도적 module-level import (Bithumb U7 mirror)."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertTrue(hasattr(coinone_module, "UsdtAlertEvaluator"))
        self.assertTrue(hasattr(coinone_module, "AlertObservation"))


class TestC6bConstants(unittest.TestCase):
    """C6b acceptance: provisional constants."""

    def test_ticker_freshness_degraded_300(self):
        self.assertEqual(TICKER_FRESHNESS_DEGRADED_SEC, 300.0)

    def test_fallback_cooldown_equals_degraded(self):
        """COOLDOWN_SEC = DEGRADED_SEC: probe 빈도 최대 1회/cycle."""
        from app.crawlers.usdt_ws.coinone import FALLBACK_COOLDOWN_SEC
        self.assertEqual(FALLBACK_COOLDOWN_SEC, TICKER_FRESHNESS_DEGRADED_SEC)

    def test_degraded_threshold_greater_than_warning(self):
        """degraded > warning — transition 순서 정합."""
        self.assertGreater(TICKER_FRESHNESS_DEGRADED_SEC, TICKER_FRESHNESS_WARNING_SEC)


# ---------------------------------------------------------------------------
# C6b additional — Codex 보강
# ---------------------------------------------------------------------------

class TestRunOneSessionDegradedTransition(unittest.IsolatedAsyncioTestCase):
    """C6b acceptance: _run_one_session에서 ticker_update_age > 300s 시 degraded transition + schedule_probe (Codex 보강).

    실제 loop 경로 검증 — TestSetTickerFreshnessStatusFallbackHook (직접 호출)와 별도로
    timing 기반 transition (last_tick_at + age check) 검증.
    """

    async def test_loop_triggers_degraded_then_schedule_probe(self):
        client = CoinoneWsClient()
        schedule_probe_calls = []
        client._fallback_controller.schedule_probe = MagicMock(
            side_effect=lambda reason: schedule_probe_calls.append(reason),
        )
        client._fallback_controller.reset_cooldown = MagicMock()
        # downstream writer mocks (DB/Redis는 valid DATA path만 — 본 test는 미진입)
        client._redis_writer.schedule = MagicMock()
        client._db_writer.schedule = MagicMock()

        # _run_one_session connect 직후 _liveness.reset_active_session()이 last_tick_at을
        # None으로 reset하므로, reset_active_session을 no-op로 patch하고 observe_tick을
        # ws.send (subscribe) 시점에 호출 (connect 후 + recv loop 전).
        # _run_one_session loop의 if/elif 구조상 1 iteration당 1단계 전이:
        # 1st iter: normal → warning, 2nd iter: warning → degraded (schedule_probe 호출).
        client._liveness.reset_active_session = MagicMock()

        async def fake_send(payload):
            # subscribe send 시점에 last_tick_at을 400s 전으로 설정 — recv loop 진입 시
            # ticker_update_age > 300s 조건 충족.
            client._liveness.observe_tick(time.time() - 400.0)

        # recv loop 2 iteration 후 stop_event set (1st: warning, 2nd: degraded)
        recv_count = {"calls": 0}
        async def fake_recv():
            recv_count["calls"] += 1
            if recv_count["calls"] >= 2:
                client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock(side_effect=fake_send)
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.coinone.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # warning → degraded transition 발생 + schedule_probe 호출 (reason="ticker_degraded")
        self.assertEqual(client._ticker_freshness_status, "degraded")
        self.assertEqual(len(schedule_probe_calls), 1)
        self.assertEqual(schedule_probe_calls[0], "ticker_degraded")


class TestFetchCoinoneRateOnlyWrapper(unittest.TestCase):
    """C6b acceptance: _fetch_coinone() rate-only wrapping (Codex 보강).

    FETCHERS registry 동작 변경 (rewrite) → polling path 회귀 방지.
    """

    def test_returns_rate_when_helper_returns_tick(self):
        """fetch_coinone_usdt_tick → tick 반환 → _fetch_coinone는 rate float만 반환."""
        from app.crawlers.usdt_sources import _fetch_coinone
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779179058028}
        with patch("app.crawlers.usdt_sources.fetch_coinone_usdt_tick", return_value=tick):
            result = _fetch_coinone()
        self.assertEqual(result, 1488.0)

    def test_returns_none_when_helper_returns_none(self):
        """fetch_coinone_usdt_tick → None (REST 실패) → _fetch_coinone도 None."""
        from app.crawlers.usdt_sources import _fetch_coinone
        with patch("app.crawlers.usdt_sources.fetch_coinone_usdt_tick", return_value=None):
            result = _fetch_coinone()
        self.assertIsNone(result)


# ===========================================================================
# C7 — UsdtAlertEvaluator wiring + AlertObservation schedule + finally Alert close
# ===========================================================================

class TestAlertScheduleOnValidTick(unittest.IsolatedAsyncioTestCase):
    """C7 acceptance 1-2: WS valid DATA tick → AlertObservation(kind="tick") schedule."""

    async def test_valid_data_schedules_alert_with_kind_tick(self):
        from app.notifications.alert_evaluator import AlertObservation
        client = CoinoneWsClient()
        alert_schedules = []

        client._alert_evaluator.schedule = MagicMock(
            side_effect=lambda obs: alert_schedules.append(obs),
        )
        # downstream writers mock — DB/Redis는 실제 호출 회피 (test 범위는 alert만)
        client._redis_writer.schedule = MagicMock()
        client._db_writer.schedule = MagicMock()

        recv_raws = [
            json.dumps({
                "response_type": "DATA",
                "data": {
                    "quote_currency": "KRW", "target_currency": "USDT",
                    "last": "1488", "timestamp": 1779179058028,
                },
            }),
        ]
        recv_count = {"calls": 0}

        async def fake_recv():
            i = recv_count["calls"]
            recv_count["calls"] += 1
            if i < len(recv_raws):
                return recv_raws[i]
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.coinone.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # 1건 schedule + kind="tick"
        self.assertEqual(len(alert_schedules), 1)
        obs = alert_schedules[0]
        self.assertIsInstance(obs, AlertObservation)
        self.assertEqual(obs.source, "coinone")
        self.assertEqual(obs.asset, "usdt-krw")
        self.assertEqual(obs.rate, 1488.0)
        self.assertEqual(obs.timestamp_ms, 1779179058028)
        self.assertEqual(obs.kind, "tick")


class TestAlertScheduleOnRestProbe(unittest.IsolatedAsyncioTestCase):
    """C7 acceptance 3: REST probe success → AlertObservation(kind="rest_probe") schedule."""

    async def test_probe_success_schedules_alert_with_kind_rest_probe(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        from app.notifications.alert_evaluator import AlertObservation
        alert_schedules = []
        alert_mock = MagicMock()
        alert_mock.schedule = MagicMock(side_effect=lambda obs: alert_schedules.append(obs))

        controller = CoinoneRestFallbackController(
            redis_writer=CoinoneRedisWriter(),
            db_writer=CoinoneDbWriter(),
            alert_evaluator=alert_mock,
            cooldown_sec=0.1,
            probe_timeout_sec=2.0,
        )

        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779179058028}
        controller._redis_writer.schedule = MagicMock()
        controller._db_writer.schedule = MagicMock()

        with patch(
            "app.crawlers.usdt_sources.fetch_coinone_usdt_tick",
            return_value=tick,
        ):
            controller.schedule_probe(reason="degraded_test")
            for _ in range(100):
                if not controller._in_flight:
                    break
                await asyncio.sleep(0.01)

        # 1건 schedule + kind="rest_probe"
        self.assertEqual(len(alert_schedules), 1)
        obs = alert_schedules[0]
        self.assertIsInstance(obs, AlertObservation)
        self.assertEqual(obs.kind, "rest_probe")
        self.assertEqual(obs.source, "coinone")
        self.assertEqual(obs.rate, 1488.0)


class TestAlertNotScheduledOnInvalidOrFailure(unittest.IsolatedAsyncioTestCase):
    """C7 acceptance 4-5: invalid frame → alert 0 / probe None or failure → alert 0."""

    async def test_invalid_frame_no_alert(self):
        client = CoinoneWsClient()
        client._alert_evaluator.schedule = MagicMock()

        # invalid DATA: last 부재
        client._handle_message(json.dumps({
            "response_type": "DATA",
            "data": {"quote_currency": "KRW", "target_currency": "USDT", "timestamp": 1},
        }))
        # _handle_message는 invalid 시 None 반환 — schedule 호출 안 됨

        # _run_one_session에 들어가지 않으므로 schedule 미호출 검증
        client._alert_evaluator.schedule.assert_not_called()

    async def test_probe_returns_none_no_alert(self):
        from app.crawlers.usdt_ws.coinone import (
            CoinoneRestFallbackController,
            CoinoneDbWriter,
            CoinoneRedisWriter,
        )
        alert_mock = MagicMock()
        controller = CoinoneRestFallbackController(
            redis_writer=CoinoneRedisWriter(),
            db_writer=CoinoneDbWriter(),
            alert_evaluator=alert_mock,
            cooldown_sec=0.1,
            probe_timeout_sec=2.0,
        )
        controller._redis_writer.schedule = MagicMock()
        controller._db_writer.schedule = MagicMock()

        with patch(
            "app.crawlers.usdt_sources.fetch_coinone_usdt_tick",
            return_value=None,
        ):
            controller.schedule_probe(reason="failure_test")
            for _ in range(100):
                if not controller._in_flight:
                    break
                await asyncio.sleep(0.01)

        # probe None → Redis/DB/Alert 모두 미호출
        controller._redis_writer.schedule.assert_not_called()
        controller._db_writer.schedule.assert_not_called()
        alert_mock.schedule.assert_not_called()


class TestFinallyOrderAlertBeforeRedis(unittest.IsolatedAsyncioTestCase):
    """C7 acceptance 6: close 순서 fallback → DB → Alert → Redis."""

    async def test_close_order(self):
        client = CoinoneWsClient()
        close_order = []

        async def fake_fallback_close():
            close_order.append("fallback")

        async def fake_db_close():
            close_order.append("db")

        async def fake_alert_close():
            close_order.append("alert")

        async def fake_redis_close(timeout=1.0):
            close_order.append("redis")

        client._fallback_controller.close = AsyncMock(side_effect=fake_fallback_close)
        client._db_writer.close = AsyncMock(side_effect=fake_db_close)
        client._alert_evaluator.close = AsyncMock(side_effect=fake_alert_close)
        client._redis_writer.close = AsyncMock(side_effect=fake_redis_close)

        async def fake_recv():
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.coinone.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        self.assertEqual(close_order, ["fallback", "db", "alert", "redis"])


class TestFinallyCloseExceptionIsolation(unittest.IsolatedAsyncioTestCase):
    """C7 acceptance 8 (Codex Point 2): 각 close 실패해도 뒤 close 실행."""

    async def test_alert_close_failure_does_not_block_redis_close(self):
        client = CoinoneWsClient()
        close_order = []

        async def fake_fallback_close():
            close_order.append("fallback")

        async def fake_db_close():
            close_order.append("db")

        async def failing_alert_close():
            close_order.append("alert_attempt")
            raise RuntimeError("FCM connection lost")

        async def fake_redis_close(timeout=1.0):
            close_order.append("redis")

        client._fallback_controller.close = AsyncMock(side_effect=fake_fallback_close)
        client._db_writer.close = AsyncMock(side_effect=fake_db_close)
        client._alert_evaluator.close = AsyncMock(side_effect=failing_alert_close)
        client._redis_writer.close = AsyncMock(side_effect=fake_redis_close)

        async def fake_recv():
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.coinone.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # alert close 실패해도 redis close까지 실행됨
        self.assertEqual(close_order, ["fallback", "db", "alert_attempt", "redis"])


class TestAlertEvaluatorInitInClient(unittest.TestCase):
    """C7 acceptance 7: CoinoneWsClient __init__에 _alert_evaluator 인스턴스 생성."""

    def test_client_has_alert_evaluator_instance(self):
        from app.notifications.alert_evaluator import UsdtAlertEvaluator
        client = CoinoneWsClient()
        self.assertIsInstance(client._alert_evaluator, UsdtAlertEvaluator)

    def test_fallback_controller_has_alert_evaluator(self):
        """C7 mandatory inject: fallback_controller가 alert_evaluator 보유."""
        client = CoinoneWsClient()
        self.assertIs(
            client._fallback_controller._alert_evaluator,
            client._alert_evaluator,
        )


class TestScopeGuardC7(unittest.TestCase):
    """C7 acceptance 9: Phase B.4 complete — Alert evaluator module-level (intended)."""

    def test_alert_evaluator_module_level(self):
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertTrue(hasattr(coinone_module, "UsdtAlertEvaluator"))
        self.assertTrue(hasattr(coinone_module, "AlertObservation"))

    def test_fetch_coinone_usdt_tick_still_lazy(self):
        """fetch_coinone_usdt_tick은 여전히 lazy import (module-level 비노출)."""
        import app.crawlers.usdt_ws.coinone as coinone_module
        self.assertNotIn("fetch_coinone_usdt_tick", dir(coinone_module))


class TestC7FlagFalseInvariantRegression(unittest.IsolatedAsyncioTestCase):
    """C7 acceptance 7: flag=false invariant — CoinoneWsClient 생성 0 → UsdtAlertEvaluator 생성 0."""

    def setUp(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_coinone_globals()

    async def test_flag_false_no_alert_evaluator_created(self):
        """flag=false 시 CoinoneWsClient 자체 생성 X — UsdtAlertEvaluator도 자동으로 생성 0."""
        with patch.object(config, "USDT_WS_COINONE_ENABLED", False), \
             patch("app.crawlers.usdt_ws.coinone.UsdtAlertEvaluator") as mock_evaluator_cls:
            await scheduler.start_usdt_ws_coinone_client()
        # CoinoneWsClient 생성자가 호출 안 됨 → UsdtAlertEvaluator 생성자도 호출 안 됨
        mock_evaluator_cls.assert_not_called()


# ===========================================================================
# PR 2d (Coinone bundle) — summary log + redis_saturation_count + fallback_probe_scheduled_count
# ===========================================================================


class TestCoinoneRedisWriterSaturationCount(unittest.IsolatedAsyncioTestCase):
    """PR 2d — CoinoneRedisWriter._saturation_count counter 동작 검증.

    의미: MAX_PENDING_WRITES skip 발생 횟수. write 성공/실패 무관.
    """

    def test_initial_value_zero(self):
        """writer 생성 직후 counter == 0."""
        writer = CoinoneRedisWriter()
        self.assertEqual(writer.saturation_count, 0)

    async def test_increments_on_saturation_skip(self):
        """_tasks >= MAX_PENDING_WRITES → skip 시 counter +1."""
        writer = CoinoneRedisWriter()
        loop = asyncio.get_running_loop()
        dummy_futures = [loop.create_future() for _ in range(MAX_PENDING_WRITES)]
        writer._tasks = set(dummy_futures)

        self.assertEqual(writer.saturation_count, 0)

        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}
        with patch("app.crawlers.usdt_ws.coinone.asyncio.to_thread"):
            writer.schedule(tick)

        self.assertEqual(writer.saturation_count, 1)

        for fut in dummy_futures:
            fut.set_result(None)
        writer._tasks.clear()

    async def test_no_increment_on_normal_schedule(self):
        """_tasks < MAX_PENDING_WRITES → 정상 schedule → counter 0 유지."""
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}
        with patch("app.crawlers.usdt_ws.coinone.asyncio.create_task") as mock_create:
            mock_create.return_value = MagicMock()
            writer.schedule(tick)

        self.assertEqual(writer.saturation_count, 0)


class TestCoinoneRedisWriterSaturationSemantics(unittest.IsolatedAsyncioTestCase):
    """PR 2d (Codex 대칭 test) — saturation_count는 schedule() saturation branch에서만 증가.

    의미 잠금: helper False / helper exception 같은 write 실패는 saturation과 무관.
    saturation_count는 "skip 발생 횟수"이지 "write 실패 횟수"가 아니다.
    """

    async def test_helper_false_does_not_increment_saturation(self):
        """helper False 반환 (write 거부) → _write_async 안 실패. saturation_count 0 유지."""
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}

        # helper False 반환 → _write_async 안 success=False path, saturation branch 미진입
        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=UsdtLatestWriteOutcome.FAILED,
        ), patch(
            "app.crawlers.usdt_ws.coinone.tether_topic_trigger.request_tether_topic_trigger",
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        # _write_async 안 helper False는 saturation branch와 무관 — counter 0 유지
        self.assertEqual(writer.saturation_count, 0)

    async def test_helper_exception_does_not_increment_saturation(self):
        """helper exception → _write_async 안 실패. saturation_count 0 유지."""
        writer = CoinoneRedisWriter()
        tick = {"source": "coinone", "asset": "usdt-krw", "rate": 1487.0, "timestamp_ms": 1779106625946}

        with patch(
            "app.crawlers.usdt_ws.coinone.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=RuntimeError("Redis 장애 simulated"),
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        # helper exception은 saturation branch와 무관 — counter 0 유지
        self.assertEqual(writer.saturation_count, 0)


class TestCoinoneFallbackScheduledProbeCount(unittest.IsolatedAsyncioTestCase):
    """PR 2d — CoinoneRestFallbackController.scheduled_probe_count counter 동작 검증.

    의미: schedule_probe()가 in-flight/cooldown/no-loop skip 통과 후
    loop.create_task() 성공 시점에만 +1. skip은 미증가.
    """

    def _make_controller(self) -> CoinoneRestFallbackController:
        """Test용 controller — schedule만 검증, 실제 fanout 미실행."""
        redis_writer = MagicMock()
        db_writer = MagicMock()
        alert_evaluator = MagicMock()
        return CoinoneRestFallbackController(
            redis_writer=redis_writer,
            db_writer=db_writer,
            alert_evaluator=alert_evaluator,
        )

    def test_initial_value_zero(self):
        """controller 생성 직후 counter == 0."""
        controller = self._make_controller()
        self.assertEqual(controller.scheduled_probe_count, 0)

    async def test_increments_on_successful_schedule(self):
        """schedule_probe() 성공 (in-flight/cooldown/no-loop skip 통과) → +1."""
        controller = self._make_controller()

        async def fake_run_probe(reason):
            return None

        with patch.object(controller, "_run_probe", side_effect=fake_run_probe):
            controller.schedule_probe(reason="test")
            self.assertEqual(controller.scheduled_probe_count, 1)
            if controller._pending_task is not None:
                await controller._pending_task

    def test_no_increment_on_in_flight_skip(self):
        """in-flight skip 발화 → counter 0 유지."""
        controller = self._make_controller()
        controller._in_flight = True
        controller.schedule_probe(reason="test")
        self.assertEqual(controller.scheduled_probe_count, 0)

    def test_no_increment_on_cooldown_skip(self):
        """cooldown skip 발화 → counter 0 유지."""
        controller = self._make_controller()
        controller._cooldown_until = time.time() + 1000.0
        controller.schedule_probe(reason="test")
        self.assertEqual(controller.scheduled_probe_count, 0)


class TestCoinoneSummaryLogLoop(unittest.IsolatedAsyncioTestCase):
    """PR 2d — Coinone summary log emit 검증 (10 fields, 2-dim status + 2 counters).

    Coinone은 Korbit state shape (connection_status + ticker_freshness_status) +
    Bithumb counter scope (saturation + probe 둘 다) union.
    """

    async def test_emit_format_contains_all_10_metrics(self):
        """emit log에 state 8 + counter 2 = 10 metric key=value 포맷 포함."""
        client = CoinoneWsClient()
        client._liveness.frame_count_total = 100
        client._liveness.last_tick_at = time.time() - 2.0
        client._liveness.last_heartbeat_at = time.time() - 30.0
        client._liveness.max_frame_gap_sec = 8.2

        async def fake_sleep(_):
            client._stop_event.set()

        with patch("app.crawlers.usdt_ws.coinone.asyncio.sleep", side_effect=fake_sleep), \
             self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "metrics" in m), None)
        self.assertIsNotNone(metric_log, "metric INFO log 없음")
        for keyword in [
            "frames_per_min=",
            "last_tick_age=",
            "last_heartbeat_age=",
            "max_frame_gap=",
            "connection_status=",                # 2-dim (Korbit 동일)
            "ticker_freshness_status=",          # 2-dim (Korbit 동일)
            "reconnect_attempts=",
            "status_transitions=",
            "redis_saturation_count=",           # PR 2d
            "fallback_probe_scheduled_count=",   # PR 2d
        ]:
            self.assertIn(keyword, metric_log, f"metric key 누락: {keyword}")

        # Coinone-specific schema 검증: 1-dim status= 형태 부재 (2-dim이라 status= 단독 없음)
        # Note: 'connection_status=' / 'ticker_freshness_status='에는 'status='가 포함되므로
        #       정확히 ' status=' (공백 prefix)로 1-dim status field 부재 확인
        self.assertNotIn(" status=", metric_log)

    async def test_sentinel_for_none_age(self):
        """last_tick_at / last_heartbeat_at이 None이면 -1.0 numeric sentinel emit."""
        client = CoinoneWsClient()
        self.assertIsNone(client._liveness.last_tick_at)
        self.assertIsNone(client._liveness.last_heartbeat_at)

        async def fake_sleep(_):
            client._stop_event.set()

        with patch("app.crawlers.usdt_ws.coinone.asyncio.sleep", side_effect=fake_sleep), \
             self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "metrics" in m), None)
        self.assertIsNotNone(metric_log)
        self.assertIn("last_tick_age=-1.0", metric_log)
        self.assertIn("last_heartbeat_age=-1.0", metric_log)

    async def test_frames_per_min_calculation(self):
        """frame_count_total 차이 / elapsed * 60 = frames_per_min 정확성 검증.

        time.time() patch: 100.0 → 160.0 (elapsed=60s), frame_count 0 → 30
        → frames_per_min = (30 - 0) * 60 / 60.0 = 30
        """
        client = CoinoneWsClient()
        client._liveness.frame_count_total = 0

        time_values = iter([100.0, 160.0])

        async def fake_sleep(duration):
            client._liveness.frame_count_total = 30
            client._stop_event.set()

        with patch("app.crawlers.usdt_ws.coinone.time.time", side_effect=lambda: next(time_values)), \
             patch("app.crawlers.usdt_ws.coinone.asyncio.sleep", side_effect=fake_sleep), \
             self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "frames_per_min=" in m), None)
        self.assertIsNotNone(metric_log)
        self.assertIn("frames_per_min=30", metric_log)

    async def test_cancelled_silently_returns(self):
        """CancelledError 시 silently return (no exception propagate)."""
        client = CoinoneWsClient()

        async def fake_sleep(_):
            raise asyncio.CancelledError()

        with patch("app.crawlers.usdt_ws.coinone.asyncio.sleep", side_effect=fake_sleep):
            await asyncio.wait_for(client._summary_log_loop(), timeout=1.0)

    async def test_stop_event_before_first_emit_skips_emit(self):
        """stop_event 사전 set 시 emit 0 (loop entry 못 함). assertNoLogs (Python 3.10+)."""
        client = CoinoneWsClient()
        client._stop_event.set()

        with self.assertNoLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO"):
            await asyncio.wait_for(client._summary_log_loop(), timeout=1.0)


class TestCoinoneSummaryLogEmitsCounters(unittest.IsolatedAsyncioTestCase):
    """PR 2d — _summary_log_loop counter field emit value 검증 (PropertyMock pattern).

    Bithumb PR 2c / Korbit PR 2a / Upbit PR 2b 패턴 mirror — counter property mock으로
    emit log 안 numeric 값 직접 검증.
    """

    async def test_emit_contains_saturation_count_value(self):
        """redis_saturation_count=N emit 검증 (PropertyMock)."""
        client = CoinoneWsClient()

        async def fake_sleep(_):
            client._stop_event.set()

        with patch.object(
            CoinoneRedisWriter, "saturation_count",
            new_callable=PropertyMock, return_value=13,
        ), patch("app.crawlers.usdt_ws.coinone.asyncio.sleep", side_effect=fake_sleep), \
           self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "metrics" in m), None)
        self.assertIsNotNone(metric_log)
        self.assertIn("redis_saturation_count=13", metric_log)

    async def test_emit_contains_scheduled_probe_count_value(self):
        """fallback_probe_scheduled_count=N emit 검증 (PropertyMock)."""
        client = CoinoneWsClient()

        async def fake_sleep(_):
            client._stop_event.set()

        with patch.object(
            CoinoneRestFallbackController, "scheduled_probe_count",
            new_callable=PropertyMock, return_value=4,
        ), patch("app.crawlers.usdt_ws.coinone.asyncio.sleep", side_effect=fake_sleep), \
           self.assertLogs("exchange_rate.crawler.usdt_ws.coinone", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "metrics" in m), None)
        self.assertIsNotNone(metric_log)
        self.assertIn("fallback_probe_scheduled_count=4", metric_log)


class TestCoinoneStartCancelsSummaryTask(unittest.IsolatedAsyncioTestCase):
    """PR 2d — start() finally에서 summary_task cancel/await 검증.

    Korbit/Bithumb start() finally cancel pattern mirror (reconnect loop 예외와 독립).
    """

    async def test_start_cancels_summary_task_on_stop(self):
        """start 종료 시 summary_task가 cancel + await됨."""
        client = CoinoneWsClient()
        cancelled = {"value": False}

        async def fake_summary_loop():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled["value"] = True
                return

        async def fake_run_one_session():
            await client._stop_event.wait()

        with patch.object(client, "_summary_log_loop", side_effect=fake_summary_loop), \
             patch.object(client, "_run_one_session", side_effect=fake_run_one_session):
            task = asyncio.create_task(client.start())
            await asyncio.sleep(0.05)
            await client.stop()
            await asyncio.wait_for(task, timeout=2.0)

        self.assertTrue(cancelled["value"], "summary_task가 cancel되지 않음")
        self.assertFalse(client._running)


if __name__ == "__main__":
    unittest.main()
