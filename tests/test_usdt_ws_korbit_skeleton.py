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
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from websockets.exceptions import ConnectionClosed

from app import config, scheduler
from app.crawlers.usdt_ws.korbit import (
    DB_WRITE_WINDOW_SEC,
    KORBIT_SYMBOL,
    KORBIT_WS_URL,
    MAX_PENDING_WRITES,
    PING_INTERVAL_SEC,
    PING_TIMEOUT_SEC,
    RECONNECT_BACKOFF_SEQ,
    RECONNECT_BACKOFF_TAIL,
    RECV_TIMEOUT_SEC,
    STALE_AFTER_SEC,
    SUBSCRIBE_REQUEST_ID,
    TICKER_FRESHNESS_WARNING_SEC,
    KorbitDbWriter,
    KorbitRedisWriter,
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
        """__init__ → stop_event + running + K3 session state + K4 liveness/2-status defaults."""
        client = KorbitWsClient()
        self.assertFalse(client._stop_event.is_set())
        self.assertFalse(client._running)
        # K3 session state
        self.assertIsNone(client._ws)
        self.assertFalse(client._first_tick_logged)
        # K4 liveness + 2 status defaults
        self.assertIsNotNone(client._liveness)
        self.assertIsNone(client._liveness.last_tick_at)
        self.assertIsNone(client._liveness.last_heartbeat_at)
        self.assertEqual(client._connection_status, "normal")
        self.assertEqual(client._ticker_freshness_status, "normal")
        self.assertEqual(client._reconnect_attempt_count, 0)
        self.assertEqual(client._status_transition_count["connection_normal"], 0)
        self.assertEqual(client._status_transition_count["ticker_warning"], 0)

    async def test_start_runs_reconnect_loop_until_stop(self):
        """K4 reconnect loop: start → _run_one_session 호출 + stop()으로 loop 종료.

        K3 시점 의미 ("single session"): K4에서 reconnect loop 도입 후 정정.
        fake _run_one_session이 stop_event 대기 → return → reconnect loop는
        stop_event.is_set() 체크 후 break → start finally 정리.
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
            # stop 호출 → _stop_event set → fake _run_one_session 종료 → reconnect loop break
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

        # K5+K6a downstream writers mock — K3 test는 wire format/parse path만 검증
        client._redis_writer.schedule = MagicMock()
        client._redis_writer.close = AsyncMock()
        client._db_writer.schedule = MagicMock()
        client._db_writer.close = AsyncMock()

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
        """korbit.py module이 DB/Alert/REST import하지 않음 (K5 누적 후 docstring 정정).

        Scope guard 진화:
            K3: 8개 forbidden (UsdtLivenessMonitor 포함)
            K4: 7개 forbidden (UsdtLivenessMonitor 허용, K4 의도적 import)
            K5 (현재): 5개 forbidden (latest_rates_cache, tether_topic_trigger 추가 허용)
        """
        import app.crawlers.usdt_ws.korbit as korbit_module
        module_attrs = dir(korbit_module)
        forbidden = [
            "UsdtAlertEvaluator",
            "AlertObservation",
            "get_db_context",
            "insert_source_rate_if_changed",
            "fetch_korbit_usdt_tick",
        ]
        for name in forbidden:
            self.assertNotIn(
                name, module_attrs,
                f"K5 scope 위반: '{name}' import됨 (K6~K7 영역)",
            )

    def test_module_imports_minimal(self):
        """module imports — K5 누적 (UsdtLivenessMonitor + latest_rates_cache + tether_topic_trigger)."""
        import app.crawlers.usdt_ws.korbit as korbit_module
        self.assertTrue(hasattr(korbit_module, "KorbitWsClient"))
        self.assertTrue(hasattr(korbit_module, "KORBIT_WS_URL"))
        self.assertTrue(hasattr(korbit_module, "KORBIT_SYMBOL"))
        # K4: UsdtLivenessMonitor source-neutral 재사용 (의도적 import)
        self.assertTrue(hasattr(korbit_module, "UsdtLivenessMonitor"))
        # K5: Redis writer + topic trigger 의도적 import
        self.assertTrue(hasattr(korbit_module, "latest_rates_cache"))
        self.assertTrue(hasattr(korbit_module, "tether_topic_trigger"))
        self.assertTrue(hasattr(korbit_module, "KorbitRedisWriter"))
        self.assertTrue(hasattr(korbit_module, "TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS"))


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


# ===========================================================================
# K4 — liveness + ws.ping() Bithumb mirror + 2 status 분리 + reconnect loop
# ===========================================================================


class TestK4ConstantsExist(unittest.TestCase):
    """K4 acceptance: constants 5개 정의."""

    def test_ping_interval_300(self):
        """DATA primary + PING backstop, Coinone 통일 default."""
        self.assertEqual(PING_INTERVAL_SEC, 300.0)

    def test_ping_timeout_5(self):
        """K-2 PONG latency 13-15ms 대비 ~300배 margin."""
        self.assertEqual(PING_TIMEOUT_SEC, 5.0)

    def test_stale_after_360(self):
        """STALE_AFTER_SEC > PING_INTERVAL_SEC (false stale 방지)."""
        self.assertGreater(STALE_AFTER_SEC, PING_INTERVAL_SEC)
        self.assertEqual(STALE_AFTER_SEC, 360.0)

    def test_ticker_freshness_warning_30(self):
        """K-2 10s × 3 = 30s warning threshold (log only)."""
        self.assertEqual(TICKER_FRESHNESS_WARNING_SEC, 30.0)

    def test_reconnect_backoff_sequence(self):
        """Upbit/KRX/Bithumb 통일 sequence."""
        self.assertEqual(RECONNECT_BACKOFF_SEQ, (1.0, 2.0, 4.0, 8.0, 16.0, 30.0))
        self.assertEqual(RECONNECT_BACKOFF_TAIL, 30.0)


class TestComputeBackoff(unittest.TestCase):
    """K4 acceptance: reconnect backoff sequence (Bithumb/Upbit 동일)."""

    def test_attempt_zero_returns_first(self):
        self.assertEqual(KorbitWsClient._compute_backoff(0), RECONNECT_BACKOFF_SEQ[0])

    def test_within_sequence_range(self):
        for i, expected in enumerate(RECONNECT_BACKOFF_SEQ, start=1):
            self.assertEqual(KorbitWsClient._compute_backoff(i), expected)

    def test_beyond_sequence_returns_tail(self):
        self.assertEqual(
            KorbitWsClient._compute_backoff(len(RECONNECT_BACKOFF_SEQ) + 5),
            RECONNECT_BACKOFF_TAIL,
        )


class TestSetConnectionStatus(unittest.TestCase):
    """K4 acceptance: _connection_status 전이 + counter + log (Coinone C4 mirror)."""

    def setUp(self):
        self.client = KorbitWsClient()

    def test_initial_state_normal(self):
        self.assertEqual(self.client._connection_status, "normal")

    def test_transition_changes_status_and_counter(self):
        """normal → stale 전이 시 status + counter ++ + log 1회."""
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="INFO") as cm:
            self.client._set_connection_status("stale")
        self.assertEqual(self.client._connection_status, "stale")
        self.assertEqual(self.client._status_transition_count["connection_stale"], 1)
        self.assertTrue(any("connection_status normal → stale" in m for m in cm.output))

    def test_no_transition_when_same_status(self):
        """동일 status 재호출 시 counter ++ 안 됨."""
        self.client._set_connection_status("stale")
        count_before = self.client._status_transition_count["connection_stale"]
        self.client._set_connection_status("stale")
        self.assertEqual(self.client._status_transition_count["connection_stale"], count_before)

    def test_stale_to_normal_recovery_logs(self):
        """stale → normal 양방향 전이 (Codex 강조: 양방향)."""
        self.client._set_connection_status("stale")
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="INFO") as cm:
            self.client._set_connection_status("normal")
        self.assertEqual(self.client._connection_status, "normal")
        self.assertTrue(any("connection_status stale → normal" in m for m in cm.output))


class TestSetTickerFreshnessStatus(unittest.TestCase):
    """K4 acceptance: _ticker_freshness_status 분리 + transition 1회 log."""

    def setUp(self):
        self.client = KorbitWsClient()

    def test_initial_state_normal(self):
        self.assertEqual(self.client._ticker_freshness_status, "normal")

    def test_transition_to_warning_logs_once(self):
        """normal → warning 전이 시 1회 log + counter."""
        with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="INFO") as cm:
            self.client._set_ticker_freshness_status("warning")
        self.assertEqual(self.client._ticker_freshness_status, "warning")
        self.assertEqual(self.client._status_transition_count["ticker_warning"], 1)
        self.assertTrue(any("ticker_freshness_status normal → warning" in m for m in cm.output))

    def test_no_log_flood_when_already_warning(self):
        """warning 상태에서 재호출 시 log emit 안 됨 (flood 방지)."""
        self.client._set_ticker_freshness_status("warning")
        count_before = self.client._status_transition_count["ticker_warning"]
        for _ in range(100):
            self.client._set_ticker_freshness_status("warning")
        self.assertEqual(self.client._status_transition_count["ticker_warning"], count_before)


class TestPingLoop(unittest.IsolatedAsyncioTestCase):
    """K4 acceptance #1, #2: _ping_loop Bithumb mirror — heartbeat observe / timeout → ws.close."""

    async def test_ping_pong_observes_heartbeat_liveness_only(self):
        """ping → pong 성공 → _liveness.observe_heartbeat 호출. status 직접 변경 X."""
        client = KorbitWsClient()
        initial_connection_status = client._connection_status
        initial_freshness_status = client._ticker_freshness_status

        ping_call_count = {"n": 0}

        async def fake_ping():
            ping_call_count["n"] += 1
            # ping 호출 후 stop_event set — 다음 iteration 진입 차단
            client._stop_event.set()
            pong_waiter = asyncio.Future()
            pong_waiter.set_result(None)
            return pong_waiter

        async def fake_sleep(duration):
            return  # PING_INTERVAL_SEC sleep 즉시 통과

        ws_mock = AsyncMock()
        ws_mock.ping = fake_ping

        with patch("app.crawlers.usdt_ws.korbit.asyncio.sleep", side_effect=fake_sleep), \
             patch.object(client._liveness, "observe_heartbeat") as mock_observe:
            await client._ping_loop(ws_mock)

        self.assertEqual(ping_call_count["n"], 1)
        mock_observe.assert_called_once()
        # status 직접 변경 X (status 변경은 _set_*_status에서만)
        self.assertEqual(client._connection_status, initial_connection_status)
        self.assertEqual(client._ticker_freshness_status, initial_freshness_status)

    async def test_ping_failure_closes_ws_and_returns(self):
        """ws.ping() 호출 자체 TimeoutError → ws.close() + return (recv loop가 ConnectionClosed → reconnect)."""
        client = KorbitWsClient()

        async def fake_sleep(duration):
            return

        ws_mock = AsyncMock()
        ws_mock.ping = AsyncMock(side_effect=asyncio.TimeoutError)
        ws_mock.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.korbit.asyncio.sleep", side_effect=fake_sleep):
            await client._ping_loop(ws_mock)

        ws_mock.close.assert_called()

    async def test_pong_waiter_timeout_closes_ws(self):
        """K4 acceptance core path (Codex Point 1): pong_waiter timeout → ws.close().

        실제 운영 시나리오: ws.ping()은 pong_waiter Future를 정상 반환,
        await asyncio.wait_for(pong_waiter, timeout=PING_TIMEOUT_SEC)에서 timeout 발생.
        (vs test_ping_failure_closes_ws_and_returns의 ws.ping() 자체 timeout과 다름)
        """
        client = KorbitWsClient()

        async def fake_sleep(duration):
            return

        async def fake_ping():
            # pong_waiter는 절대 set되지 않는 Future — wait_for에서 timeout 유도
            return asyncio.Future()

        ws_mock = AsyncMock()
        ws_mock.ping = fake_ping
        ws_mock.close = AsyncMock()

        # PING_TIMEOUT_SEC 짧게 patch → wait_for(pong_waiter, 0.05) timeout 빠르게 발화
        with patch("app.crawlers.usdt_ws.korbit.asyncio.sleep", side_effect=fake_sleep), \
             patch("app.crawlers.usdt_ws.korbit.PING_TIMEOUT_SEC", 0.05):
            await client._ping_loop(ws_mock)

        # wait_for(pong_waiter) timeout → ws.close() 호출됨
        ws_mock.close.assert_called()


class TestTickerSilenceNoReconnect(unittest.IsolatedAsyncioTestCase):
    """K4 acceptance #7 (핵심 invariant): ticker silence + heartbeat fresh → stale/reconnect 안 됨.

    last_activity_at = max(tick, heartbeat). Coinone C4 invariant mirror.
    """

    async def test_is_stale_false_when_heartbeat_fresh(self):
        """heartbeat이 최근이면 ticker silence 길어도 is_stale=False."""
        client = KorbitWsClient()
        client._liveness.observe_heartbeat(time.time())
        # 1시간 전 tick (ticker silence 매우 길음)
        client._liveness.last_tick_at = time.time() - 3600.0
        # heartbeat fresh이므로 stale 아님 (last_activity_at = max(tick, heartbeat))
        self.assertFalse(client._liveness.is_stale(time.time(), STALE_AFTER_SEC))


class TestRunOneSessionFreshnessWiring(unittest.IsolatedAsyncioTestCase):
    """K4 acceptance #5 (Codex Point 3): _run_one_session age 로직 wiring test.

    `_set_ticker_freshness_status` 직접 호출이 아니라 실제 loop 안에서
    last_tick_at age > 30s 조건이 trigger되어 normal → warning 전이되는지 검증.
    """

    async def test_age_based_warning_transition_in_session_loop(self):
        """recv loop에서 last_tick_at이 30s 이상 old → freshness warning 전이.

        `_run_one_session`이 `_liveness.reset_active_session()` 호출하므로
        sent_payloads 단계 (subscribe 직후, recv loop 진입 전)에서 last_tick_at을
        old로 의도 설정 — 첫 recv iteration에서 freshness check trigger.
        """
        client = KorbitWsClient()

        sent_payloads = []

        async def fake_send(payload):
            sent_payloads.append(payload)
            # subscribe 직후 last_tick_at을 의도적으로 old로 설정.
            # reset_active_session() 이후 시점이라 보존됨.
            client._liveness.last_tick_at = time.time() - 60.0

        async def fake_recv():
            # 첫 iteration에서 freshness check 후 stop_event set
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock(side_effect=fake_send)
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        # ping_loop은 stop_event 즉시 감지하도록 짧은 interval
        with patch("app.crawlers.usdt_ws.korbit.websockets.connect", return_value=mock_ws), \
             patch("app.crawlers.usdt_ws.korbit.PING_INTERVAL_SEC", 0.01):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # last_tick_at age > 30s → warning 전이 (transition count 검증)
        self.assertEqual(client._ticker_freshness_status, "warning")
        self.assertGreaterEqual(client._status_transition_count["ticker_warning"], 1)

    async def test_freshness_warning_to_normal_on_valid_tick(self):
        """K4 acceptance #6 (Codex Point 2): warning 상태에서 valid ticker 수신 → normal 복귀 wiring.

        2-signal state machine 복구 경로 검증 — silence 감지 (normal → warning)과
        복구 (warning → normal)는 서로 다른 리스크라 wiring 분리 검증.

        `_run_one_session` 진입 시 line 437에서 `_set_ticker_freshness_status("normal")`
        호출되어 사전 status가 reset됨. 따라서 fake_recv 안에서 warning force set
        (session 시작 normal reset **이후** 시점) → 다음 valid ticker로 복귀 검증.
        """
        client = KorbitWsClient()

        # valid ticker raw frame (K-2 smoke shape mirror)
        valid_ticker_raw = json.dumps({
            "type": "ticker",
            "timestamp": 1779194622984,
            "symbol": "usdt_krw",
            "data": {"close": "1488", "lastTradedAt": 1779194593306},
        })

        async def fake_send(payload):
            return

        recv_count = {"calls": 0}

        async def fake_recv():
            i = recv_count["calls"]
            recv_count["calls"] += 1
            if i == 0:
                # 첫 recv 시점 = session 시작 normal reset 이후 = warning force set 가능
                client._ticker_freshness_status = "warning"
                # transition_count도 직접 갱신 (실제 _set_*_status 우회 — wiring path 검증 목적)
                client._status_transition_count["ticker_warning"] = 1
                # valid ticker 반환 → handle_message → tick not None →
                # _liveness.observe_tick + warning → normal 복귀 path
                return valid_ticker_raw
            # 두 번째 호출: stop_event set → 빠짐
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock(side_effect=fake_send)
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        # K5+K6a downstream writers mock — freshness wiring path만 검증
        client._redis_writer.schedule = MagicMock()
        client._redis_writer.close = AsyncMock()
        client._db_writer.schedule = MagicMock()
        client._db_writer.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.korbit.websockets.connect", return_value=mock_ws), \
             patch("app.crawlers.usdt_ws.korbit.PING_INTERVAL_SEC", 0.01):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # 결과: warning → normal 복귀 path 통과.
        # valid tick 수신 후 _set_ticker_freshness_status("normal") 호출 (warning → normal 1회 log).
        self.assertEqual(client._ticker_freshness_status, "normal")
        # last_tick_at이 valid tick 수신으로 갱신됨 (observe_tick 호출 검증)
        self.assertIsNotNone(client._liveness.last_tick_at)
        # transition count: warning 1회 (force set) + normal 1회 (session 시작 1회 + 복귀 시 normal→normal skip).
        # 핵심 검증: warning → normal transition 발생 후 결과 normal 유지.
        self.assertGreaterEqual(client._status_transition_count["ticker_normal"], 1)


class TestStartReconnectLoop(unittest.IsolatedAsyncioTestCase):
    """K4 acceptance #8: start reconnect loop with backoff (Bithumb mirror)."""

    async def test_connection_closed_triggers_reconnect_with_backoff(self):
        """ConnectionClosed → reconnect attempt ++ + _set_connection_status('reconnecting') + backoff.

        backoff sequence patch (0.01s)로 빠른 진행. wait_for(stop_event.wait, timeout)는
        timeout 발생 → 다음 iteration 진입.
        """
        client = KorbitWsClient()
        run_call_count = {"n": 0}

        async def fake_run_one_session():
            run_call_count["n"] += 1
            if run_call_count["n"] == 1:
                # 첫 session: ConnectionClosed 발생
                raise ConnectionClosed(None, None)
            # 두 번째 session: stop_event 대기 → 정상 종료
            await client._stop_event.wait()

        # backoff sequence 짧게 patch — wait_for(stop_event, 0.01) timeout 빠르게 발생
        short_backoff = (0.01,)
        with patch.object(client, "_run_one_session", side_effect=fake_run_one_session), \
             patch("app.crawlers.usdt_ws.korbit.RECONNECT_BACKOFF_SEQ", short_backoff), \
             patch("app.crawlers.usdt_ws.korbit.RECONNECT_BACKOFF_TAIL", 0.01):
            task = asyncio.create_task(client.start())
            # 두 번째 session 진입 후 stop
            for _ in range(200):  # ~2s max
                if run_call_count["n"] >= 2:
                    break
                await asyncio.sleep(0.01)
            await client.stop()
            await asyncio.wait_for(task, timeout=3.0)

        self.assertGreaterEqual(run_call_count["n"], 2)
        self.assertGreaterEqual(client._reconnect_attempt_count, 1)
        self.assertGreaterEqual(client._status_transition_count["connection_reconnecting"], 1)

    async def test_stop_event_during_backoff_breaks_loop(self):
        """backoff 중 stop_event set → 즉시 loop break."""
        client = KorbitWsClient()

        async def fake_run_one_session():
            raise ConnectionClosed(None, None)

        # asyncio.wait_for가 stop_event.wait()를 정상 await → stop_event set 시 즉시 return
        with patch.object(client, "_run_one_session", side_effect=fake_run_one_session):
            task = asyncio.create_task(client.start())
            # session 1회 실패 후 backoff 진입할 시간 확보
            await asyncio.sleep(0.05)
            # backoff 도중 stop
            await client.stop()
            await asyncio.wait_for(task, timeout=5.0)

        self.assertTrue(task.done())
        self.assertFalse(client._running)


# ===========================================================================
# K5 — KorbitRedisWriter + topic trigger + _run_one_session valid tick wiring
# ===========================================================================


class TestKorbitRedisWriterScheduleSuccess(unittest.IsolatedAsyncioTestCase):
    """K5 acceptance: schedule → _write_async → to_thread(helper) → timestamp_ms KST ISO."""

    async def test_schedule_calls_helper_with_kst_iso(self):
        """schedule(tick) → set_latest_usdt_rate_from_sync_job 호출 + timestamp_ms KST ISO 변환 검증."""
        writer = KorbitRedisWriter()
        tick = {
            "source": "korbit",
            "asset": "usdt-krw",
            "rate": 1488.0,
            "timestamp_ms": 1779194593306,
        }
        captured_kwargs = {}

        def fake_helper(*, source, asset, rate, timestamp):
            captured_kwargs["source"] = source
            captured_kwargs["asset"] = asset
            captured_kwargs["rate"] = rate
            captured_kwargs["timestamp"] = timestamp
            return True

        trigger_calls = []

        def fake_trigger(*, source, asset, reason):
            trigger_calls.append({"source": source, "asset": asset, "reason": reason})

        with patch(
            "app.crawlers.usdt_ws.korbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=fake_helper,
        ), patch(
            "app.crawlers.usdt_ws.korbit.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=fake_trigger,
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        self.assertEqual(captured_kwargs["source"], "korbit")
        self.assertEqual(captured_kwargs["asset"], "usdt-krw")
        self.assertEqual(captured_kwargs["rate"], 1488.0)
        ts = captured_kwargs["timestamp"]
        self.assertIsInstance(ts, str)
        self.assertIn("+09:00", ts)
        from datetime import datetime, timezone, timedelta
        expected_kst = datetime.fromtimestamp(
            1779194593.306, tz=timezone(timedelta(hours=9)),
        ).isoformat()
        self.assertEqual(ts, expected_kst)

        # topic trigger 호출 검증 (success 후)
        self.assertEqual(len(trigger_calls), 1)
        self.assertEqual(trigger_calls[0]["source"], "korbit")
        self.assertEqual(trigger_calls[0]["asset"], "usdt-krw")


class TestKorbitRedisWriterHelperFailureIsolated(unittest.IsolatedAsyncioTestCase):
    """K5 acceptance: helper False / exception → log only + topic trigger 미호출."""

    async def test_helper_returns_false_no_trigger(self):
        writer = KorbitRedisWriter()
        tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779194593306}
        trigger_calls = []

        with patch(
            "app.crawlers.usdt_ws.korbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=False,
        ), patch(
            "app.crawlers.usdt_ws.korbit.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=lambda **kw: trigger_calls.append(kw),
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        self.assertEqual(len(trigger_calls), 0)

    async def test_helper_exception_isolated(self):
        writer = KorbitRedisWriter()
        tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779194593306}
        trigger_calls = []

        with patch(
            "app.crawlers.usdt_ws.korbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=RuntimeError("Redis down"),
        ), patch(
            "app.crawlers.usdt_ws.korbit.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=lambda **kw: trigger_calls.append(kw),
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        self.assertEqual(len(trigger_calls), 0)


class TestKorbitRedisWriterTriggerExceptionIsolated(unittest.IsolatedAsyncioTestCase):
    """K5 acceptance: trigger 예외 → log only (writer/WS 영향 X). helper success 가정."""

    async def test_trigger_exception_writer_continues(self):
        writer = KorbitRedisWriter()
        tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779194593306}
        helper_calls = []

        def fake_helper(**kw):
            helper_calls.append(kw)
            return True

        with patch(
            "app.crawlers.usdt_ws.korbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=fake_helper,
        ), patch(
            "app.crawlers.usdt_ws.korbit.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=RuntimeError("trigger queue full"),
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        self.assertEqual(len(helper_calls), 1)


class TestKorbitRedisWriterSaturation(unittest.IsolatedAsyncioTestCase):
    """K5 acceptance: saturation check — MAX_PENDING_WRITES 초과 시 skip + warning."""

    async def test_saturation_skips_new_schedule(self):
        writer = KorbitRedisWriter()

        async def slow_helper(**kw):
            await asyncio.sleep(10.0)
            return True

        with patch(
            "app.crawlers.usdt_ws.korbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=slow_helper,
        ):
            tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779194593306}
            for _ in range(MAX_PENDING_WRITES):
                writer.schedule(tick)
            self.assertEqual(len(writer._tasks), MAX_PENDING_WRITES)

            with self.assertLogs("exchange_rate.crawler.usdt_ws.korbit", level="WARNING") as cm:
                writer.schedule(tick)
            self.assertEqual(len(writer._tasks), MAX_PENDING_WRITES)
            self.assertTrue(any("saturated" in m for m in cm.output))

            for task in list(writer._tasks):
                task.cancel()
            await asyncio.gather(*writer._tasks, return_exceptions=True)


class TestKorbitRedisWriterClose(unittest.IsolatedAsyncioTestCase):
    """K5 acceptance: close drain + timeout 후 cancel + _tasks.clear()."""

    async def test_close_drains_pending_then_clears(self):
        writer = KorbitRedisWriter()
        tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779194593306}

        with patch(
            "app.crawlers.usdt_ws.korbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=True,
        ), patch(
            "app.crawlers.usdt_ws.korbit.tether_topic_trigger.request_tether_topic_trigger",
        ):
            writer.schedule(tick)
            writer.schedule(tick)
            self.assertGreater(len(writer._tasks), 0)
            await writer.close(timeout=2.0)
            self.assertEqual(len(writer._tasks), 0)

    async def test_close_timeout_cancels_pending(self):
        writer = KorbitRedisWriter()
        tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779194593306}

        async def slow_helper(**kw):
            await asyncio.sleep(10.0)
            return True

        with patch(
            "app.crawlers.usdt_ws.korbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=slow_helper,
        ):
            writer.schedule(tick)
            self.assertGreater(len(writer._tasks), 0)
            await writer.close(timeout=0.1)
            self.assertEqual(len(writer._tasks), 0)


class TestRunOneSessionRedisWiring(unittest.IsolatedAsyncioTestCase):
    """K5 acceptance: _run_one_session valid ticker path에서 observe_tick 후 _redis_writer.schedule 호출."""

    async def test_valid_ticker_calls_schedule_on_redis_writer(self):
        client = KorbitWsClient()
        schedule_calls = []

        client._redis_writer.schedule = MagicMock(
            side_effect=lambda tick: schedule_calls.append(tick),
        )
        client._redis_writer.close = AsyncMock()
        # K6a downstream writer mock — K5 test는 Redis wiring path만 검증
        client._db_writer.schedule = MagicMock()
        client._db_writer.close = AsyncMock()

        recv_raws = [
            json.dumps({"status": "success", "requestId": 1}),  # ACK
            json.dumps({
                "type": "ticker", "timestamp": 1779194622984, "symbol": "usdt_krw",
                "data": {"close": "1488", "lastTradedAt": 1779194593306},
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

        with patch("app.crawlers.usdt_ws.korbit.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # ticker 1건만 schedule 호출됨 (ACK는 schedule 호출 X)
        self.assertEqual(len(schedule_calls), 1)
        self.assertEqual(schedule_calls[0]["source"], "korbit")
        self.assertEqual(schedule_calls[0]["asset"], "usdt-krw")
        self.assertEqual(schedule_calls[0]["rate"], 1488.0)
        self.assertEqual(schedule_calls[0]["timestamp_ms"], 1779194593306)


class TestScopeGuardK5(unittest.TestCase):
    """K5 acceptance: K5 의도적 import 허용 + K6~K7 영역 forbidden."""

    def test_redis_topic_imports_allowed(self):
        """K5: latest_rates_cache, tether_topic_trigger, KorbitRedisWriter 의도적 import 허용."""
        import app.crawlers.usdt_ws.korbit as korbit_module
        self.assertTrue(hasattr(korbit_module, "latest_rates_cache"))
        self.assertTrue(hasattr(korbit_module, "tether_topic_trigger"))
        self.assertTrue(hasattr(korbit_module, "KorbitRedisWriter"))
        self.assertTrue(hasattr(korbit_module, "TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS"))

    def test_db_alert_rest_forbidden(self):
        """K6~K7 영역 (DB/Alert/REST) 여전히 forbidden."""
        import app.crawlers.usdt_ws.korbit as korbit_module
        module_attrs = dir(korbit_module)
        forbidden = [
            "UsdtAlertEvaluator",
            "AlertObservation",
            "get_db_context",
            "insert_source_rate_if_changed",
            "fetch_korbit_usdt_tick",
        ]
        for name in forbidden:
            self.assertNotIn(
                name, module_attrs,
                f"K5 scope 위반: '{name}' import됨 (K6~K7 영역)",
            )


# ===========================================================================
# K6a — KorbitDbWriter + 1s window debounce + _run_one_session DB wiring + close order
# ===========================================================================


class TestKorbitDbWriterScheduleAndFlush(unittest.IsolatedAsyncioTestCase):
    """K6a acceptance: schedule → window timer → _flush_after_window → _sync_db_write."""

    async def test_schedule_then_flush_calls_helper(self):
        writer = KorbitDbWriter(window_sec=0.05)
        tick = {"source": "korbit", "asset": "usdt-krw", "rate": 1488.0, "timestamp_ms": 1779194593306}
        helper_calls = []

        def fake_insert(*, db, source, asset, rate):
            helper_calls.append({"source": source, "asset": asset, "rate": rate})

        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=fake_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            writer.schedule(tick)
            await asyncio.sleep(0.15)

        self.assertEqual(len(helper_calls), 1)
        self.assertEqual(helper_calls[0]["source"], "korbit")
        self.assertEqual(helper_calls[0]["asset"], "usdt-krw")
        self.assertEqual(helper_calls[0]["rate"], 1488.0)

    async def test_multiple_ticks_in_window_only_last_flushed(self):
        """window 내 여러 tick → 마지막 1건만 helper 호출 (debounce)."""
        writer = KorbitDbWriter(window_sec=0.1)
        helper_calls = []

        def fake_insert(*, db, source, asset, rate):
            helper_calls.append(rate)

        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=fake_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            writer.schedule({"source": "korbit", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            writer.schedule({"source": "korbit", "asset": "usdt-krw", "rate": 2.0, "timestamp_ms": 2})
            writer.schedule({"source": "korbit", "asset": "usdt-krw", "rate": 3.0, "timestamp_ms": 3})
            await asyncio.sleep(0.2)

        self.assertEqual(len(helper_calls), 1)
        self.assertEqual(helper_calls[0], 3.0)


class TestKorbitDbWriterRaceWithNewTick(unittest.IsolatedAsyncioTestCase):
    """K6a acceptance: write 진행 중 새 tick → finally에서 새 timer 예약 (누락 방지).

    threading.Event 사용 (asyncio.to_thread thread-safety).
    """

    async def test_new_tick_during_write_schedules_next_window(self):
        import threading
        writer = KorbitDbWriter(window_sec=0.05)
        helper_calls = []

        first_write_started = threading.Event()
        first_write_can_finish = threading.Event()

        def slow_insert(*, db, source, asset, rate):
            helper_calls.append(rate)
            first_write_started.set()
            if rate == 1.0:
                first_write_can_finish.wait(timeout=2.0)

        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=slow_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            writer.schedule({"source": "korbit", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            await asyncio.wait_for(
                asyncio.to_thread(first_write_started.wait, 2.0),
                timeout=3.0,
            )
            writer.schedule({"source": "korbit", "asset": "usdt-krw", "rate": 2.0, "timestamp_ms": 2})
            first_write_can_finish.set()
            await asyncio.sleep(0.2)

        # 2번 호출됨 (race 방지 — 두 번째 tick 누락 X)
        self.assertEqual(len(helper_calls), 2)
        self.assertIn(1.0, helper_calls)
        self.assertIn(2.0, helper_calls)


class TestKorbitDbWriterHelperExceptionIsolated(unittest.IsolatedAsyncioTestCase):
    """K6a acceptance: helper exception → log only (WS session 영향 X)."""

    async def test_helper_exception_isolated(self):
        writer = KorbitDbWriter(window_sec=0.05)

        def failing_insert(*, db, source, asset, rate):
            raise RuntimeError("DB connection lost")

        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=failing_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            writer.schedule({"source": "korbit", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            await asyncio.sleep(0.15)
            # writer 상태 정상 — close 호출 가능
            await writer.close()


class TestKorbitDbWriterCloseImmediateFlush(unittest.IsolatedAsyncioTestCase):
    """K6a acceptance: close 시 pending tick 즉시 flush (1초 window 안 기다림)."""

    async def test_close_flushes_pending_immediately(self):
        writer = KorbitDbWriter(window_sec=10.0)  # 의도적으로 긴 window
        helper_calls = []

        def fake_insert(*, db, source, asset, rate):
            helper_calls.append(rate)

        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.crud.insert_source_rate_if_changed", side_effect=fake_insert), \
             patch("app.database.get_db_context", return_value=mock_ctx):
            writer.schedule({"source": "korbit", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            await asyncio.wait_for(writer.close(), timeout=1.0)

        # window 무시하고 즉시 flush — 1회 호출
        self.assertEqual(len(helper_calls), 1)
        self.assertEqual(helper_calls[0], 1.0)


class TestRunOneSessionDbWiring(unittest.IsolatedAsyncioTestCase):
    """K6a acceptance: _run_one_session valid ticker path에서 observe_tick → Redis → DB wiring + finally 순서."""

    async def test_valid_ticker_calls_db_writer_schedule(self):
        client = KorbitWsClient()
        redis_calls = []
        db_calls = []

        client._redis_writer.schedule = MagicMock(
            side_effect=lambda tick: redis_calls.append(tick),
        )
        client._db_writer.schedule = MagicMock(
            side_effect=lambda tick: db_calls.append(tick),
        )

        recv_raws = [
            json.dumps({"status": "success", "requestId": 1}),
            json.dumps({
                "type": "ticker", "timestamp": 1779194622984, "symbol": "usdt_krw",
                "data": {"close": "1488", "lastTradedAt": 1779194593306},
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

        with patch("app.crawlers.usdt_ws.korbit.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # ticker 1건만 schedule 호출 — Redis + DB 둘 다
        self.assertEqual(len(redis_calls), 1)
        self.assertEqual(len(db_calls), 1)
        self.assertEqual(redis_calls[0], db_calls[0])

    async def test_finally_order_db_before_redis(self):
        """finally 순서: ping_task → db_writer.close() → redis_writer.close()."""
        client = KorbitWsClient()
        close_order = []

        async def fake_db_close():
            close_order.append("db")

        async def fake_redis_close(timeout=1.0):
            close_order.append("redis")

        client._db_writer.close = AsyncMock(side_effect=fake_db_close)
        client._redis_writer.close = AsyncMock(side_effect=fake_redis_close)

        recv_count = {"calls": 0}

        async def fake_recv():
            recv_count["calls"] += 1
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=fake_recv)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.korbit.websockets.connect", return_value=mock_ws):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        self.assertEqual(close_order, ["db", "redis"])


class TestScopeGuardK6a(unittest.TestCase):
    """K6a acceptance: KorbitDbWriter 의도적 + K6b~K7 영역 forbidden 유지."""

    def test_db_writer_class_present(self):
        """K6a: KorbitDbWriter class module-level 노출."""
        import app.crawlers.usdt_ws.korbit as korbit_module
        self.assertTrue(hasattr(korbit_module, "KorbitDbWriter"))
        self.assertTrue(hasattr(korbit_module, "DB_WRITE_WINDOW_SEC"))

    def test_db_symbols_not_module_level(self):
        """get_db_context, insert_source_rate_if_changed는 _sync_db_write 함수 내부 import.

        module-level에 노출되면 안 됨 (lazy import — DB 의존성 모듈 로드 시점 격리).
        """
        import app.crawlers.usdt_ws.korbit as korbit_module
        module_attrs = dir(korbit_module)
        self.assertNotIn("get_db_context", module_attrs)
        self.assertNotIn("insert_source_rate_if_changed", module_attrs)

    def test_rest_alert_still_forbidden(self):
        """K6b~K7 영역 (REST helper / Alert) 여전히 forbidden."""
        import app.crawlers.usdt_ws.korbit as korbit_module
        module_attrs = dir(korbit_module)
        for name in ["UsdtAlertEvaluator", "AlertObservation", "fetch_korbit_usdt_tick"]:
            self.assertNotIn(name, module_attrs)


if __name__ == "__main__":
    unittest.main()
