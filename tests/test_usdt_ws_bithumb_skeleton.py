"""USDT WS Bithumb canary 단위 테스트 — Phase B.3 Stage U2-U3.

USDT_WS_DESIGN_PLAN §12.5 Phase B.3 Stage U2 + U3 (2026-05-17).
Upbit Phase B.1 PR1+PR2 테스트 패턴 작은 복제.

U2 핵심 acceptance (Codex 강조, plan §12.5.2):
    USDT_WS_BITHUMB_ENABLED=false 시 scheduler start 함수 즉시 return +
    BithumbWsClient 생성 X + network connect X + Redis/DB writer X.
    이 invariant가 U2부터 들어가야 후속 U3-U6 운영 영향 0 주장 유지.

U2 client behavior:
    - __init__: stop_event + running flag만 (network/Redis/DB import 없음)
    - start: stop_event 대기만 (중복 start 방지)
    - stop: stop_event set (idempotent)

U3 client behavior (현재):
    - constants: BITHUMB_WS_URL / BITHUMB_SUBSCRIBE_TICKET / BITHUMB_TARGET_CODE
    - _build_subscribe_payload: Upbit-compatible 3-element list
    - _parse_ticker_message: valid/invalid 입력, 0/negative guard, timestamp fallback
    - _handle_message: parse + first_tick log + tick log (downstream IO 없음)
    - _run_one_session: connect + subscribe + recv mock, log only
    - flag=false invariant 재검증 (U2 unchanged)

실행:
    python -m unittest tests.test_usdt_ws_bithumb_skeleton -v
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.crawlers.usdt_ws.bithumb import (
    BITHUMB_SUBSCRIBE_TICKET,
    BITHUMB_TARGET_CODE,
    BITHUMB_WS_URL,
    DB_WRITE_WINDOW_SEC,
    FALLBACK_COOLDOWN_SEC,
    FALLBACK_PROBE_TIMEOUT_SEC,
    MAX_PENDING_WRITES,
    PING_INTERVAL_SEC,
    PING_TIMEOUT_SEC,
    RECONNECT_BACKOFF_SEQ,
    RECONNECT_BACKOFF_TAIL,
    REDIS_CLOSE_TIMEOUT_SEC,
    STALE_AFTER_SEC,
    BithumbDbWriter,
    BithumbRedisWriter,
    BithumbRestFallbackController,
    BithumbWsClient,
)
from app.tether_topic_trigger import (
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_scheduler_usdt_ws_bithumb_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_bithumb_client = None
    scheduler.usdt_ws_bithumb_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_bithumb_client` mock — client.stop()까지 stop_event 대기."""
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# Stage U2 acceptance — flag=false invariant 핵심 검증 (Codex 강조)
# ---------------------------------------------------------------------------

class TestFlagFalseInvariant(unittest.IsolatedAsyncioTestCase):
    """Stage U2 핵심 acceptance — flag=false 시 운영 영향 0 검증.

    이 invariant가 보장되어야 U3-U6 stage가 누적되어도 운영 영향 0 유지.
    """

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_start_returns_immediately_when_flag_false(self):
        """flag=false → start 함수 즉시 return + client/task 생성 X (skip log only)."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler.start_usdt_ws_bithumb_client()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)
        # skip log emit 확인
        self.assertTrue(any("USDT_WS_BITHUMB_ENABLED=false" in m for m in cm.output))

    async def test_no_bithumb_client_constructed_when_flag_false(self):
        """flag=false → BithumbWsClient.__init__이 호출되지 않음 (생성 X invariant)."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             patch("app.crawlers.usdt_ws.bithumb.BithumbWsClient") as mock_client_cls:
            await scheduler.start_usdt_ws_bithumb_client()
        # BithumbWsClient 생성자가 호출되지 않음 — import 자체도 발생 안 함이 이상이지만
        # 함수 내부 import라 module load는 가능. 인스턴스 생성 0이 핵심 invariant.
        mock_client_cls.assert_not_called()

    async def test_no_network_redis_db_when_flag_false(self):
        """flag=false → network connect / Redis SET / DB write 0건 invariant.

        scheduler.start_usdt_ws_bithumb_client()가 즉시 return하므로 후속 stage에서
        추가될 BithumbRedisWriter / BithumbDbWriter / WS connect 호출 path 미진입.

        본 test는 U2 단계에서 client 생성 자체가 안 됨을 보장하면 충분 (writers는 client
        instance 안 attribute라 client 미생성 = writers 미생성 = 외부 IO 0).
        """
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False):
            await scheduler.start_usdt_ws_bithumb_client()
        # globals 모두 None — 어떤 client/writer/task도 생성 X
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)

    async def test_shutdown_safe_when_never_started(self):
        """start 호출 안 한 상태에서 shutdown → no-op (globals 그대로 None)."""
        # globals already None (setUp)
        await scheduler.shutdown_usdt_ws_bithumb_client()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)


# ---------------------------------------------------------------------------
# Stage U2 lifecycle — flag=true 경로 (network 무관, _run_* mock)
# ---------------------------------------------------------------------------

class TestStartUsdtWsBithumbClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_creates_task_when_enabled(self):
        """flag=true → BithumbWsClient + task 생성."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_bithumb_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_bithumb_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_bithumb_client)
                self.assertIsNotNone(scheduler.usdt_ws_bithumb_task)
                self.assertFalse(scheduler.usdt_ws_bithumb_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_bithumb_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_bithumb_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_bithumb_client()
            first_task = scheduler.usdt_ws_bithumb_task
            first_client = scheduler.usdt_ws_bithumb_client
            self.assertIsNotNone(first_task)

            try:
                await scheduler.start_usdt_ws_bithumb_client()
                self.assertIs(scheduler.usdt_ws_bithumb_task, first_task)
                self.assertIs(scheduler.usdt_ws_bithumb_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_bithumb_client()


class TestShutdownUsdtWsBithumbClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_bithumb_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_bithumb_client()
            task = scheduler.usdt_ws_bithumb_task
            self.assertIsNotNone(task)

            await scheduler.shutdown_usdt_ws_bithumb_client()
            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_bithumb_client)
            self.assertIsNone(scheduler.usdt_ws_bithumb_task)


# ---------------------------------------------------------------------------
# BithumbWsClient skeleton behavior
# ---------------------------------------------------------------------------

class TestBithumbWsClientSkeleton(unittest.IsolatedAsyncioTestCase):
    """U2 skeleton: __init__ + start (stop_event 대기) + stop (set)."""

    async def test_init_state(self):
        """__init__ → stop_event (not set) + running False + U4 state defaults."""
        client = BithumbWsClient()
        self.assertFalse(client._stop_event.is_set())
        self.assertFalse(client._running)
        # U4 state — liveness + status + transition_count + reconnect_attempt_count
        self.assertIsNotNone(client._liveness)
        self.assertEqual(client._status, "normal")
        self.assertEqual(client._status_transition_count, {
            "normal": 0, "reconnecting": 0, "stale": 0,
        })
        self.assertEqual(client._reconnect_attempt_count, 0)

    async def test_start_waits_for_stop_event(self):
        """start → stop_event 대기 (running=True, no network).

        U3 누적 후 start()가 _run_one_session()을 호출하므로 unit test에서는
        _run_one_session을 patch해서 stop_event 대기만 검증 (no network).
        Codex Finding (Stage U3 정정): websockets.connect 호출 0 명시 검증.
        """
        client = BithumbWsClient()

        async def fake_run_session():
            await client._stop_event.wait()

        with patch.object(
            BithumbWsClient, "_run_one_session", side_effect=fake_run_session,
        ), patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect:
            task = asyncio.create_task(client.start())
            await asyncio.sleep(0.01)  # start loop 진입 보장
            self.assertTrue(client._running)
            await client.stop()
            await asyncio.wait_for(task, timeout=1.0)
            self.assertFalse(client._running)
            # no-network unit test acceptance — websockets.connect 호출 0
            mock_connect.assert_not_called()

    async def test_double_start_skip(self):
        """이미 _running 시 두 번째 start 즉시 return (중복 방지, no network).

        U3 누적 후 _run_one_session을 mock으로 격리.
        """
        client = BithumbWsClient()

        async def fake_run_session():
            await client._stop_event.wait()

        with patch.object(
            BithumbWsClient, "_run_one_session", side_effect=fake_run_session,
        ), patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect:
            task1 = asyncio.create_task(client.start())
            await asyncio.sleep(0.01)
            self.assertTrue(client._running)

            # 두 번째 start → 즉시 return (debug log)
            await client.start()
            self.assertFalse(task1.done())

            await client.stop()
            await asyncio.wait_for(task1, timeout=1.0)
            # no-network — websockets.connect 호출 0
            mock_connect.assert_not_called()

    async def test_stop_idempotent(self):
        """stop 두 번 호출 → 두 번째도 안전 (이미 set인 event는 set 가능)."""
        client = BithumbWsClient()
        await client.stop()
        await client.stop()  # idempotent
        self.assertTrue(client._stop_event.is_set())


# ---------------------------------------------------------------------------
# Stage U3 — subscribe payload + parser
# ---------------------------------------------------------------------------

class TestBuildSubscribePayload(unittest.TestCase):
    """Subscribe payload Upbit-compatible 3-element list."""

    def test_payload_structure(self):
        payload = BithumbWsClient._build_subscribe_payload()
        self.assertEqual(len(payload), 3)
        self.assertEqual(payload[0], {"ticket": BITHUMB_SUBSCRIBE_TICKET})
        self.assertEqual(payload[1], {"type": "ticker", "codes": [BITHUMB_TARGET_CODE]})
        self.assertEqual(payload[2], {"format": "DEFAULT"})

    def test_constants(self):
        self.assertEqual(BITHUMB_WS_URL, "wss://ws-api.bithumb.com/websocket/v1")
        self.assertEqual(BITHUMB_SUBSCRIBE_TICKET, "fxi-usdt-bithumb")
        self.assertEqual(BITHUMB_TARGET_CODE, "KRW-USDT")


class TestParseTickerMessage(unittest.TestCase):
    """_parse_ticker_message — valid/invalid 입력 + guards."""

    def setUp(self):
        self.client = BithumbWsClient()

    def test_parse_valid_dict(self):
        """Valid ticker dict → normalized tick."""
        message = {
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
            "trade_timestamp": 1777370239843,
            "timestamp": 1777370240080,
            "stream_type": "REALTIME",
        }
        result = self.client._parse_ticker_message(message)
        self.assertEqual(result, {
            "source": "bithumb",
            "asset": "usdt-krw",
            "rate": 1486.0,
            "timestamp_ms": 1777370239843,
        })

    def test_parse_valid_str_json(self):
        """JSON string 입력도 정상 parse."""
        message_str = json.dumps({
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1500.5,
            "trade_timestamp": 1777370239843,
        })
        result = self.client._parse_ticker_message(message_str)
        self.assertIsNotNone(result)
        self.assertEqual(result["rate"], 1500.5)

    def test_parse_valid_bytes_json(self):
        """UTF-8 bytes 입력도 정상 parse."""
        message_bytes = json.dumps({
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1490,
            "trade_timestamp": 1777370239843,
        }).encode("utf-8")
        result = self.client._parse_ticker_message(message_bytes)
        self.assertIsNotNone(result)
        self.assertEqual(result["rate"], 1490.0)

    def test_parse_timestamp_fallback(self):
        """trade_timestamp 없으면 timestamp fallback."""
        message = {
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
            "timestamp": 1777370240080,
        }
        result = self.client._parse_ticker_message(message)
        self.assertEqual(result["timestamp_ms"], 1777370240080)

    def test_parse_invalid_json_returns_none(self):
        self.assertIsNone(self.client._parse_ticker_message("not json"))

    def test_parse_non_ticker_type_returns_none(self):
        """type != 'ticker' → None (status frame 등 무시)."""
        message = {"type": "status", "code": "KRW-USDT"}
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_wrong_code_returns_none(self):
        """code != KRW-USDT → None (다른 마켓 무시)."""
        message = {
            "type": "ticker",
            "code": "KRW-BTC",
            "trade_price": 50000000,
            "trade_timestamp": 1777370239843,
        }
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_zero_rate_returns_none(self):
        """rate=0 guard (잘못된 fanout 차단)."""
        message = {
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 0,
            "trade_timestamp": 1777370239843,
        }
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_negative_rate_returns_none(self):
        """rate<0 guard."""
        message = {
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": -1.5,
            "trade_timestamp": 1777370239843,
        }
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_missing_timestamp_returns_none(self):
        """trade_timestamp + timestamp 모두 없으면 None."""
        message = {
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
        }
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_invalid_input_type_returns_none(self):
        """int/list/None 입력 → None (def parse 입력 type guard)."""
        self.assertIsNone(self.client._parse_ticker_message(123))
        self.assertIsNone(self.client._parse_ticker_message([]))
        self.assertIsNone(self.client._parse_ticker_message(None))


class TestHandleMessage(unittest.IsolatedAsyncioTestCase):
    """_handle_message — parse + first_tick log + tick log."""

    async def test_handle_first_tick_logs_info(self):
        """첫 valid tick → INFO log, _first_tick_logged=True."""
        client = BithumbWsClient()
        message = {
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
            "trade_timestamp": 1777370239843,
        }
        with self.assertLogs("exchange_rate.crawler.usdt_ws.bithumb", level="INFO") as cm:
            tick = client._handle_message(message)
        self.assertIsNotNone(tick)
        self.assertTrue(client._first_tick_logged)
        self.assertTrue(any("first tick" in m for m in cm.output))

    async def test_handle_second_tick_logs_debug(self):
        """두 번째 tick → DEBUG log (first_tick 이미 emit됨)."""
        client = BithumbWsClient()
        message = {
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
            "trade_timestamp": 1777370239843,
        }
        # 첫 tick (INFO)
        client._handle_message(message)
        self.assertTrue(client._first_tick_logged)
        # 두 번째 tick (DEBUG, INFO 미발생)
        with self.assertNoLogs("exchange_rate.crawler.usdt_ws.bithumb", level="INFO"):
            tick = client._handle_message(message)
        self.assertIsNotNone(tick)

    async def test_handle_invalid_returns_none(self):
        """invalid frame → None, log 없음."""
        client = BithumbWsClient()
        self.assertIsNone(client._handle_message("not json"))
        self.assertFalse(client._first_tick_logged)


class TestRunOneSession(unittest.IsolatedAsyncioTestCase):
    """_run_one_session — connect + subscribe + recv mock + log only."""

    async def test_session_sends_subscribe_payload(self):
        """connect 성공 → subscribe payload send + log. stop_event 미리 set으로 recv loop entry 0."""
        client = BithumbWsClient()
        client._stop_event.set()  # while loop 진입 X (subscribe send만 실행 후 return)
        ws_mock = AsyncMock()
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock()  # 호출 안 됨
        connect_mock = AsyncMock()
        connect_mock.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_mock.__aexit__ = AsyncMock(return_value=None)

        with patch("app.crawlers.usdt_ws.bithumb.websockets.connect", return_value=connect_mock):
            await client._run_one_session()

        # subscribe payload send 확인 (while loop 진입 전 1회)
        ws_mock.send.assert_called_once()
        sent_payload = json.loads(ws_mock.send.call_args.args[0])
        self.assertEqual(sent_payload[0], {"ticket": BITHUMB_SUBSCRIBE_TICKET})
        self.assertEqual(sent_payload[1], {"type": "ticker", "codes": [BITHUMB_TARGET_CODE]})
        # recv는 호출 안 됨 (loop entry 0)
        ws_mock.recv.assert_not_called()

    async def test_session_processes_valid_tick(self):
        """recv valid ticker → _handle_message로 처리 (log only). stop_event를 recv 안에서 set."""
        client = BithumbWsClient()
        valid_message = json.dumps({
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
            "trade_timestamp": 1777370239843,
        })
        recv_responses = [valid_message]

        async def fake_recv():
            if recv_responses:
                return recv_responses.pop(0)
            # 응답 소진 후 stop_event set + TimeoutError (continue → next iteration → break)
            client._stop_event.set()
            await asyncio.sleep(0.001)  # event 반영
            raise asyncio.TimeoutError

        ws_mock = AsyncMock()
        ws_mock.recv = fake_recv
        ws_mock.send = AsyncMock()
        connect_mock = AsyncMock()
        connect_mock.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_mock.__aexit__ = AsyncMock(return_value=None)

        with patch("app.crawlers.usdt_ws.bithumb.websockets.connect", return_value=connect_mock):
            with self.assertLogs("exchange_rate.crawler.usdt_ws.bithumb", level="INFO") as cm:
                await client._run_one_session()

        # first tick INFO emit 확인
        self.assertTrue(any("first tick" in m for m in cm.output))
        self.assertTrue(client._first_tick_logged)


# ---------------------------------------------------------------------------
# U2 invariant 재검증 (U3 누적 후에도 유지) — Codex 강조
# ---------------------------------------------------------------------------

class TestFlagFalseInvariantU3Regression(unittest.IsolatedAsyncioTestCase):
    """U3 누적 후에도 flag=false invariant 유지 검증.

    Codex 강조: connect/subscribe/parse 코드 추가돼도 flag=false에서는
    BithumbWsClient 생성 자체가 없어야 함.
    """

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_flag_false_skips_even_after_u3_added(self):
        """U3 코드 누적 후에도 flag=false → BithumbWsClient 생성 X."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             patch("app.crawlers.usdt_ws.bithumb.BithumbWsClient") as mock_client_cls:
            await scheduler.start_usdt_ws_bithumb_client()
        mock_client_cls.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)


# ---------------------------------------------------------------------------
# Stage U4 — _set_status / _compute_backoff / status transitions
# ---------------------------------------------------------------------------

class TestSetStatus(unittest.TestCase):
    """_set_status — counter + log only (Redis/DB/fallback X)."""

    def test_set_status_updates_counter_and_log(self):
        """status 전이 시 counter ++ + log emit."""
        client = BithumbWsClient()
        # normal → reconnecting
        client._set_status("reconnecting")
        self.assertEqual(client._status, "reconnecting")
        self.assertEqual(client._status_transition_count["reconnecting"], 1)
        # reconnecting → stale
        client._set_status("stale")
        self.assertEqual(client._status, "stale")
        self.assertEqual(client._status_transition_count["stale"], 1)
        # stale → normal
        client._set_status("normal")
        self.assertEqual(client._status, "normal")
        self.assertEqual(client._status_transition_count["normal"], 1)

    def test_set_status_skip_when_same(self):
        """현재 status와 같으면 counter ++ skip."""
        client = BithumbWsClient()
        client._set_status("normal")  # already normal
        self.assertEqual(client._status_transition_count["normal"], 0)

    def test_set_status_unknown_status_no_counter(self):
        """알 수 없는 status — _status 갱신은 되지만 counter 증가 X."""
        client = BithumbWsClient()
        client._set_status("unknown")
        self.assertEqual(client._status, "unknown")
        # 'unknown' is not in transition_count dict → 증가 안 함
        self.assertEqual(
            client._status_transition_count,
            {"normal": 0, "reconnecting": 0, "stale": 0},
        )


class TestComputeBackoff(unittest.TestCase):
    """_compute_backoff — Upbit/KRX 동일 sequence."""

    def test_compute_backoff_sequence(self):
        """attempt=1..6 → SEQ, 7+ → TAIL."""
        for i, expected in enumerate(RECONNECT_BACKOFF_SEQ, start=1):
            self.assertEqual(BithumbWsClient._compute_backoff(i), expected)
        # 7+ → TAIL
        self.assertEqual(BithumbWsClient._compute_backoff(7), RECONNECT_BACKOFF_TAIL)
        self.assertEqual(BithumbWsClient._compute_backoff(100), RECONNECT_BACKOFF_TAIL)

    def test_compute_backoff_zero_or_negative(self):
        """attempt <= 0 → SEQ[0]."""
        self.assertEqual(BithumbWsClient._compute_backoff(0), RECONNECT_BACKOFF_SEQ[0])
        self.assertEqual(BithumbWsClient._compute_backoff(-1), RECONNECT_BACKOFF_SEQ[0])


# ---------------------------------------------------------------------------
# Stage U4 — reconnect loop (acceptance 2, 6, 7) — bounded fixture
# ---------------------------------------------------------------------------

class TestReconnectLoop(unittest.IsolatedAsyncioTestCase):
    """start() reconnect loop — bounded fixture (sleep mock + connect mock)."""

    async def test_connection_closed_increments_attempt_count(self):
        """ConnectionClosed → _reconnect_attempt_count ++ (acceptance 6).

        bounded: _run_one_session이 ConnectionClosed raise → backoff (mock) →
        stop_event set → break. attempt 1.
        """
        from websockets.exceptions import ConnectionClosed
        client = BithumbWsClient()

        call_count = {"n": 0}

        async def fake_run_session():
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 첫 호출: ConnectionClosed raise
                raise ConnectionClosed(None, None)
            # 두 번째 호출 진입 직전 stop → break
            client._stop_event.set()
            await asyncio.sleep(0)

        # backoff sleep mock (asyncio.wait_for inside backoff)
        with patch.object(
            BithumbWsClient, "_run_one_session", side_effect=fake_run_session,
        ), patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect:
            await asyncio.wait_for(client.start(), timeout=5.0)

        # ConnectionClosed → attempt ++
        self.assertEqual(client._reconnect_attempt_count, 1)
        # status reconnecting 전이 1회
        self.assertEqual(client._status_transition_count["reconnecting"], 1)
        # no-network — connect mock unused (run_one_session 자체가 mock)
        mock_connect.assert_not_called()

    async def test_stop_during_backoff_breaks_immediately(self):
        """backoff 중 stop_event 도달 → 즉시 break (acceptance 7).

        backoff sleep을 wait_for(stop_event.wait(), timeout=backoff)로 구현 →
        stop_event set 시 즉시 반응.
        """
        from websockets.exceptions import ConnectionClosed
        client = BithumbWsClient()

        async def fake_run_session():
            raise ConnectionClosed(None, None)  # 매번 raise → backoff 진입

        async def stop_soon():
            await asyncio.sleep(0.01)
            client._stop_event.set()

        with patch.object(
            BithumbWsClient, "_run_one_session", side_effect=fake_run_session,
        ), patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect:
            stop_task = asyncio.create_task(stop_soon())
            await asyncio.wait_for(client.start(), timeout=5.0)
            await stop_task

        # stop_event 도달로 backoff 중 break — running False
        self.assertFalse(client._running)
        # attempt 1 (첫 ConnectionClosed)
        self.assertGreaterEqual(client._reconnect_attempt_count, 1)
        mock_connect.assert_not_called()

    async def test_double_start_skipped(self):
        """이미 running이면 두 번째 start 즉시 return (no-network)."""
        client = BithumbWsClient()

        async def fake_run_session():
            await client._stop_event.wait()

        with patch.object(
            BithumbWsClient, "_run_one_session", side_effect=fake_run_session,
        ), patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect:
            task1 = asyncio.create_task(client.start())
            await asyncio.sleep(0.01)
            self.assertTrue(client._running)
            # 두 번째 start — 즉시 return
            await client.start()
            self.assertFalse(task1.done())
            # cleanup
            await client.stop()
            await asyncio.wait_for(task1, timeout=1.0)
        mock_connect.assert_not_called()


# ---------------------------------------------------------------------------
# Stage U4 — ping loop (acceptance 3, 5)
# ---------------------------------------------------------------------------

class TestPingLoop(unittest.IsolatedAsyncioTestCase):
    """_ping_loop — heartbeat observation (liveness only), session 종료 시 cancel/await."""

    async def test_ping_pong_observes_heartbeat_liveness_only(self):
        """ping → pong 성공 → _liveness.observe_heartbeat 호출. status 직접 변경 X.

        acceptance 5: heartbeat/pong → liveness만.
        """
        client = BithumbWsClient()
        initial_status = client._status

        # ping 1회 성공 후 stop_event set (2번째 iteration entry guard에서 exit)
        ping_call_count = {"n": 0}

        async def fake_ping():
            ping_call_count["n"] += 1
            # ping 호출 후 stop_event set — 다음 iteration 진입 차단
            client._stop_event.set()
            pong_waiter = asyncio.Future()
            pong_waiter.set_result(None)
            return pong_waiter

        async def fake_sleep(duration):
            # 첫 PING_INTERVAL_SEC sleep은 즉시 통과해서 ping 진입.
            # 두 번째는 entry guard에서 stop_event 감지로 exit.
            return

        ws_mock = AsyncMock()
        ws_mock.ping = fake_ping

        with patch("app.crawlers.usdt_ws.bithumb.asyncio.sleep", side_effect=fake_sleep), \
             patch.object(client._liveness, "observe_heartbeat") as mock_observe:
            await client._ping_loop(ws_mock)

        # ping 호출 + observe_heartbeat 호출 (status 변경은 _set_status에서만)
        self.assertEqual(ping_call_count["n"], 1)
        mock_observe.assert_called_once()
        # status 직접 변경 X
        self.assertEqual(client._status, initial_status)

    async def test_ping_failure_closes_ws_and_returns(self):
        """ping timeout → ws.close() + return (reconnect trigger은 recv loop가).

        Codex L1: asyncio.wait_for global patch 제거. ws.ping이 직접 TimeoutError
        raise하므로 `except (asyncio.TimeoutError, ConnectionClosed)` catch path
        진입 — wait_for(pong) 단계까지 도달하지 않음.
        """
        client = BithumbWsClient()

        async def fake_sleep(duration):
            return  # 즉시 진행

        ws_mock = AsyncMock()
        ws_mock.ping = AsyncMock(side_effect=asyncio.TimeoutError)
        ws_mock.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.bithumb.asyncio.sleep", side_effect=fake_sleep):
            await client._ping_loop(ws_mock)

        # ws.close 호출됨 (ping 실패 path)
        ws_mock.close.assert_called()


# ---------------------------------------------------------------------------
# Stage U4 — stale transition (acceptance 4)
# ---------------------------------------------------------------------------

class TestStaleTransition(unittest.TestCase):
    """stale 전이 — Redis/DB direct write X, fallback schedule_probe만 (U6 A7).

    Stage 진화:
        U4: client에 redis/db/fallback attribute 자체 없음 → side-effect 자연 차단
        U5: _redis_writer 추가, but _set_status에서 직접 schedule 호출 X
        U6 (현재): _db_writer + _fallback_controller 추가. _set_status는
            redis/db direct schedule X (A7), normal→stale 시 fallback schedule_probe (A7),
            normal 복귀 시 fallback reset_cooldown (A8).
        U7 영역: _alert_evaluator 여전히 부재.
    """

    def test_set_status_stale_no_direct_redis_db_write(self):
        """_set_status('stale') → Redis/DB direct write X, fallback schedule_probe만 (A7).

        U6 진화: _set_status는 _fallback_controller.schedule_probe만 호출. Redis/DB는
        직접 writer.schedule을 통하지 않고 fallback → probe → writer.schedule로 우회.
        """
        client = BithumbWsClient()
        # U5/U6: redis + db + fallback 추가됨
        self.assertTrue(hasattr(client, "_redis_writer"))
        self.assertTrue(hasattr(client, "_db_writer"))
        self.assertTrue(hasattr(client, "_fallback_controller"))
        # U7 영역: alert evaluator 여전히 부재 (acceptance 6 U6 scope)
        self.assertFalse(hasattr(client, "_alert_evaluator"))

        # _set_status가 redis/db에 직접 schedule하지 않음 (A7)
        with patch.object(client._redis_writer, "schedule") as mock_redis, \
             patch.object(client._db_writer, "schedule") as mock_db, \
             patch.object(client._fallback_controller, "schedule_probe") as mock_probe:
            client._set_status("stale")
        self.assertEqual(client._status, "stale")
        self.assertEqual(client._status_transition_count["stale"], 1)
        mock_redis.assert_not_called()
        mock_db.assert_not_called()
        # normal → stale 전이 시 fallback probe만 호출 (A7)
        mock_probe.assert_called_once_with(reason="stale_transition")

    def test_set_status_normal_recovery_resets_cooldown(self):
        """normal 복귀 → cooldown reset 호출 (A8). Redis/DB direct write X."""
        client = BithumbWsClient()
        # normal → stale (probe schedule)
        client._set_status("stale")
        # stale → normal (cooldown reset)
        with patch.object(client._redis_writer, "schedule") as mock_redis, \
             patch.object(client._db_writer, "schedule") as mock_db, \
             patch.object(client._fallback_controller, "reset_cooldown") as mock_reset, \
             patch.object(client._fallback_controller, "schedule_probe") as mock_probe:
            client._set_status("normal")
        self.assertEqual(client._status, "normal")
        self.assertEqual(client._status_transition_count["normal"], 1)
        mock_redis.assert_not_called()
        mock_db.assert_not_called()
        # normal 복귀 → reset_cooldown 호출 (A8)
        mock_reset.assert_called_once()
        # normal 복귀는 schedule_probe 호출 X (A7 stale 전이에서만)
        mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# Stage U4 — flag=false invariant 재검증 (acceptance 1, Codex 강조)
# ---------------------------------------------------------------------------

class TestFlagFalseInvariantU4Regression(unittest.IsolatedAsyncioTestCase):
    """U4 누적 후에도 flag=false invariant 유지 검증.

    Codex 강조 (Stage U4 acceptance 1): liveness/reconnect/ping 코드 추가돼도
    flag=false에서는 BithumbWsClient 생성 자체가 없어야 함.
    """

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_flag_false_skips_even_after_u4_added(self):
        """U4 코드 누적 후에도 flag=false → BithumbWsClient 생성 X + connect 0."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             patch("app.crawlers.usdt_ws.bithumb.BithumbWsClient") as mock_client_cls, \
             patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect:
            await scheduler.start_usdt_ws_bithumb_client()
        mock_client_cls.assert_not_called()
        mock_connect.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)


# ---------------------------------------------------------------------------
# Stage U5 — BithumbRedisWriter (13 acceptance criteria)
# ---------------------------------------------------------------------------

def _make_valid_tick(rate: float = 1486.0, ts_ms: int = 1777370239843) -> dict:
    """Bithumb normalized tick shape (source="bithumb")."""
    return {
        "source": "bithumb",
        "asset": "usdt-krw",
        "rate": rate,
        "timestamp_ms": ts_ms,
    }


class TestBithumbRedisWriterSchedule(unittest.IsolatedAsyncioTestCase):
    """schedule() — fire-and-forget background task 생성 (acceptance 2)."""

    async def test_schedule_creates_background_task(self):
        """schedule() → task 생성 + _tasks set add + done_callback discard."""
        writer = BithumbRedisWriter()
        tick = _make_valid_tick()

        # to_thread + helper mock → 즉시 성공 반환
        async def fake_to_thread(func, *args, **kwargs):
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ) as mock_trigger:
            writer.schedule(tick)
            self.assertEqual(len(writer._tasks), 1)
            await writer.close()  # drain
            self.assertEqual(len(writer._tasks), 0)
            mock_trigger.assert_called_once()


class TestRedisWriterSaturation(unittest.IsolatedAsyncioTestCase):
    """A8 — MAX_PENDING_WRITES guard (Redis 장애 시 task 폭증 차단)."""

    async def test_skip_when_saturated(self):
        """_tasks >= MAX_PENDING_WRITES → schedule skip + warning."""
        writer = BithumbRedisWriter()
        # _tasks를 인위적으로 saturation 한도까지 채움 (dummy futures)
        loop = asyncio.get_running_loop()
        dummy_futures = [loop.create_future() for _ in range(MAX_PENDING_WRITES)]
        writer._tasks = set(dummy_futures)

        with self.assertLogs(
            "exchange_rate.crawler.usdt_ws.bithumb", level="WARNING"
        ) as cm, patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread"
        ) as mock_to_thread:
            writer.schedule(_make_valid_tick())
        # to_thread 호출 0 — saturation guard로 skip
        mock_to_thread.assert_not_called()
        # _tasks size 그대로 (새 task 미생성)
        self.assertEqual(len(writer._tasks), MAX_PENDING_WRITES)
        self.assertTrue(any("saturated" in m for m in cm.output))

        # cleanup
        for fut in dummy_futures:
            fut.set_result(None)
        writer._tasks.clear()


class TestRedisWriteLockOrdering(unittest.IsolatedAsyncioTestCase):
    """A9 — _write_lock으로 Redis SET 순서 = schedule 순서 직렬화."""

    async def test_writes_serialized_by_lock(self):
        """concurrent schedule N개 → helper 호출 순서 = schedule 순서."""
        writer = BithumbRedisWriter()
        call_order: list[int] = []

        async def fake_to_thread(func, *, source, asset, rate, timestamp):
            # rate를 ordering ID로 사용 — 호출 진입 순서 기록
            call_order.append(int(rate))
            # 짧은 yield로 다른 task가 진입 시도하더라도 lock에 의해 차단됨
            await asyncio.sleep(0.001)
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ):
            # 3개 tick 순서대로 schedule (rate=1, 2, 3)
            for i in (1, 2, 3):
                writer.schedule(_make_valid_tick(rate=float(i), ts_ms=i))
            await writer.close()

        # lock 직렬화 → schedule 순서 보존
        self.assertEqual(call_order, [1, 2, 3])


class TestRedisHelperUsesToThread(unittest.IsolatedAsyncioTestCase):
    """A10 — sync helper를 asyncio.to_thread로 격리 (event loop non-blocking)."""

    async def test_helper_called_via_to_thread(self):
        """schedule → to_thread(set_latest_usdt_rate_from_sync_job, ...) 호출."""
        writer = BithumbRedisWriter()
        tick = _make_valid_tick()

        async def fake_to_thread(func, *args, **kwargs):
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ) as mock_to_thread, patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ):
            writer.schedule(tick)
            await writer.close()

        # to_thread 호출 1회, 첫 인자가 set_latest_usdt_rate_from_sync_job
        self.assertEqual(mock_to_thread.call_count, 1)
        from app import latest_rates_cache
        self.assertIs(
            mock_to_thread.call_args.args[0],
            latest_rates_cache.set_latest_usdt_rate_from_sync_job,
        )
        # kwargs source/asset/rate/timestamp 전달
        kwargs = mock_to_thread.call_args.kwargs
        self.assertEqual(kwargs["source"], "bithumb")
        self.assertEqual(kwargs["asset"], "usdt-krw")
        self.assertEqual(kwargs["rate"], 1486.0)
        self.assertIn("timestamp", kwargs)


class TestTopicTriggerOnSuccessOnly(unittest.IsolatedAsyncioTestCase):
    """3, 4 — set_latest_usdt_rate_from_sync_job 성공 시에만 trigger.

    helper False / 예외 → trigger 미호출.
    """

    async def test_trigger_fires_only_when_helper_returns_true(self):
        writer = BithumbRedisWriter()

        async def fake_to_thread(func, *args, **kwargs):
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ) as mock_trigger:
            writer.schedule(_make_valid_tick())
            await writer.close()
        mock_trigger.assert_called_once()

    async def test_trigger_not_fired_when_helper_returns_false(self):
        writer = BithumbRedisWriter()

        async def fake_to_thread(func, *args, **kwargs):
            return False

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ) as mock_trigger, self.assertLogs(
            "exchange_rate.crawler.usdt_ws.bithumb", level="WARNING"
        ):
            writer.schedule(_make_valid_tick())
            await writer.close()
        mock_trigger.assert_not_called()

    async def test_trigger_not_fired_when_helper_raises(self):
        writer = BithumbRedisWriter()

        async def fake_to_thread(func, *args, **kwargs):
            raise RuntimeError("redis down")

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ) as mock_trigger, self.assertLogs(
            "exchange_rate.crawler.usdt_ws.bithumb", level="ERROR"
        ):
            writer.schedule(_make_valid_tick())
            await writer.close()
        mock_trigger.assert_not_called()


class TestTopicTriggerReasonReused(unittest.IsolatedAsyncioTestCase):
    """A12 — TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS 재사용 (신규 reason X).

    Bithumb도 Upbit와 동일 reason 상수 사용 — telemetry 분기 폭증 방지. source만
    "bithumb"으로 차이.
    """

    async def test_trigger_uses_existing_reason_constant(self):
        writer = BithumbRedisWriter()

        async def fake_to_thread(func, *args, **kwargs):
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ) as mock_trigger:
            writer.schedule(_make_valid_tick())
            await writer.close()

        mock_trigger.assert_called_once_with(
            source="bithumb",
            asset="usdt-krw",
            reason=TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
        )


class TestTriggerExceptionIsolated(unittest.IsolatedAsyncioTestCase):
    """5 — trigger 호출 자체의 예외는 writer/WS loop에 전파 X."""

    async def test_trigger_exception_swallowed(self):
        """trigger raise → exception log, writer task 정상 완료."""
        writer = BithumbRedisWriter()

        async def fake_to_thread(func, *args, **kwargs):
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=RuntimeError("trigger boom"),
        ), self.assertLogs(
            "exchange_rate.crawler.usdt_ws.bithumb", level="ERROR"
        ) as cm:
            writer.schedule(_make_valid_tick())
            await writer.close()

        # exception은 task 내부에서 swallow됨 (writer/WS 영향 X)
        self.assertTrue(any("trigger 호출 실패" in m for m in cm.output))


class TestRedisCloseDrain(unittest.IsolatedAsyncioTestCase):
    """A11 — close() drain (1s) + timeout cancel + _tasks.clear()."""

    async def test_close_drains_pending_writes(self):
        """schedule된 task들을 drain → _tasks 비워짐."""
        writer = BithumbRedisWriter()

        async def fake_to_thread(func, *args, **kwargs):
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ):
            for _ in range(3):
                writer.schedule(_make_valid_tick())
            self.assertEqual(len(writer._tasks), 3)
            await writer.close()
        self.assertEqual(len(writer._tasks), 0)

    async def test_close_timeout_cancels_hung_tasks(self):
        """to_thread가 hung → close timeout → cancel + _tasks.clear()."""
        writer = BithumbRedisWriter()

        async def hung_to_thread(func, *args, **kwargs):
            # close timeout보다 훨씬 길게 대기
            await asyncio.sleep(60)
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=hung_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ), self.assertLogs(
            "exchange_rate.crawler.usdt_ws.bithumb", level="WARNING"
        ) as cm:
            writer.schedule(_make_valid_tick())
            # 짧은 timeout으로 close → cancel path 진입
            await writer.close(timeout=0.05)

        self.assertEqual(len(writer._tasks), 0)
        self.assertTrue(any("close timeout" in m for m in cm.output))

    async def test_close_idempotent_when_empty(self):
        """_tasks 비어있으면 close()는 즉시 return."""
        writer = BithumbRedisWriter()
        self.assertEqual(len(writer._tasks), 0)
        await writer.close()
        self.assertEqual(len(writer._tasks), 0)


class TestInvalidFrameNoSchedule(unittest.IsolatedAsyncioTestCase):
    """7 — valid tick만 writer.schedule(), invalid frame은 schedule 0.

    _run_one_session의 recv path에서 _handle_message로 parse → None이면 schedule X.
    """

    async def test_invalid_frame_does_not_schedule(self):
        """invalid frame mix → schedule 호출 횟수 = valid frame 수만."""
        client = BithumbWsClient()
        valid_msg = json.dumps({
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
            "trade_timestamp": 1777370239843,
        })
        invalid_msgs = [
            "not json",
            json.dumps({"type": "status", "code": "KRW-USDT"}),
            json.dumps({  # wrong code
                "type": "ticker", "code": "KRW-BTC",
                "trade_price": 100, "trade_timestamp": 1,
            }),
        ]
        recv_responses = [*invalid_msgs, valid_msg]

        async def fake_recv():
            if recv_responses:
                return recv_responses.pop(0)
            client._stop_event.set()
            await asyncio.sleep(0.001)
            raise asyncio.TimeoutError

        ws_mock = AsyncMock()
        ws_mock.recv = fake_recv
        ws_mock.send = AsyncMock()
        ws_mock.ping = AsyncMock(return_value=asyncio.Future())
        ws_mock.close = AsyncMock()
        connect_mock = AsyncMock()
        connect_mock.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_mock.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.bithumb.websockets.connect",
            return_value=connect_mock,
        ), patch.object(
            client._redis_writer, "schedule"
        ) as mock_schedule, patch.object(
            client._redis_writer, "close",
            new_callable=AsyncMock,
        ):
            await client._run_one_session()

        # invalid 3개 + valid 1개 → schedule 1회만
        self.assertEqual(mock_schedule.call_count, 1)
        tick_arg = mock_schedule.call_args.args[0]
        self.assertEqual(tick_arg["source"], "bithumb")
        self.assertEqual(tick_arg["rate"], 1486.0)


class TestNoAlertInU6(unittest.TestCase):
    """U6 acceptance 6 — alert evaluator는 U6에서 0 (U7 영역).

    Stage 진화: U5에서 redis만, U6에서 db + fallback 추가, U7에서 alert 추가 예정.
    """

    def test_u7_attributes_absent_in_u6(self):
        client = BithumbWsClient()
        # U5/U6 추가됨
        self.assertTrue(hasattr(client, "_redis_writer"))
        self.assertIsInstance(client._redis_writer, BithumbRedisWriter)
        # U6 추가됨
        self.assertTrue(hasattr(client, "_db_writer"))
        self.assertTrue(hasattr(client, "_fallback_controller"))
        # U7 영역 — 여전히 부재
        self.assertFalse(hasattr(client, "_alert_evaluator"))


class TestSessionLevelCloseAndReconnectReuse(unittest.IsolatedAsyncioTestCase):
    """A13 lifecycle invariant — session-level close + reconnect 재사용.

    writer instance는 __init__에서 1회 생성, 매 _run_one_session finally에서
    close() drain + _tasks.clear(), 다음 reconnect session에서 같은 instance가
    빈 pending set으로 재시작.
    """

    async def test_run_one_session_finally_calls_writer_close(self):
        """_run_one_session 종료 시 writer.close 호출."""
        client = BithumbWsClient()
        client._stop_event.set()  # while loop entry 0
        ws_mock = AsyncMock()
        ws_mock.recv = AsyncMock()
        ws_mock.send = AsyncMock()
        connect_mock = AsyncMock()
        connect_mock.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_mock.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.bithumb.websockets.connect",
            return_value=connect_mock,
        ), patch.object(
            client._redis_writer, "close", new_callable=AsyncMock,
        ) as mock_close:
            await client._run_one_session()

        mock_close.assert_called_once()

    async def test_writer_instance_preserved_across_sessions(self):
        """reconnect 사이 writer instance 동일 (assertIs)."""
        client = BithumbWsClient()
        first_writer = client._redis_writer

        # 첫 session — stop_event set으로 즉시 종료
        client._stop_event.set()
        ws_mock = AsyncMock()
        ws_mock.recv = AsyncMock()
        ws_mock.send = AsyncMock()
        connect_mock = AsyncMock()
        connect_mock.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_mock.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.bithumb.websockets.connect",
            return_value=connect_mock,
        ):
            await client._run_one_session()

        # writer instance 동일성 — reconnect 시 재할당 없음
        self.assertIs(client._redis_writer, first_writer)
        # _tasks 비어 있음 (close 후)
        self.assertEqual(len(client._redis_writer._tasks), 0)

        # 두 번째 session 진입 (stop_event reset)
        client._stop_event.clear()
        client._stop_event.set()
        with patch(
            "app.crawlers.usdt_ws.bithumb.websockets.connect",
            return_value=connect_mock,
        ):
            await client._run_one_session()
        # 여전히 같은 instance
        self.assertIs(client._redis_writer, first_writer)

    async def test_writer_reusable_after_close_with_new_schedule(self):
        """close 후 같은 writer instance로 schedule 재호출 가능."""
        writer = BithumbRedisWriter()

        async def fake_to_thread(func, *args, **kwargs):
            return True

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), patch(
            "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
        ):
            # cycle 1
            writer.schedule(_make_valid_tick(rate=1.0, ts_ms=1))
            await writer.close()
            self.assertEqual(len(writer._tasks), 0)

            # cycle 2 — 같은 instance, 빈 pending set으로 재시작
            writer.schedule(_make_valid_tick(rate=2.0, ts_ms=2))
            self.assertEqual(len(writer._tasks), 1)
            await writer.close()
            self.assertEqual(len(writer._tasks), 0)


class TestFlagFalseInvariantU5Regression(unittest.IsolatedAsyncioTestCase):
    """1 — flag=false → Redis write 0, trigger 0, client 생성 0 (U5 누적 후).

    Codex 강조: BithumbRedisWriter 추가돼도 flag=false에서는 BithumbWsClient
    생성 자체가 0 → writer 인스턴스화도 0 → Redis write/trigger 도달 불가능.
    """

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_flag_false_skips_even_after_u5_added(self):
        """U5 코드 누적 후에도 flag=false → 모든 외부 IO 0."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             patch("app.crawlers.usdt_ws.bithumb.BithumbWsClient") as mock_client_cls, \
             patch("app.crawlers.usdt_ws.bithumb.BithumbRedisWriter") as mock_writer_cls, \
             patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect, \
             patch(
                 "app.crawlers.usdt_ws.bithumb.latest_rates_cache.set_latest_usdt_rate_from_sync_job"
             ) as mock_redis, \
             patch(
                 "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
             ) as mock_trigger:
            await scheduler.start_usdt_ws_bithumb_client()
        mock_client_cls.assert_not_called()
        mock_writer_cls.assert_not_called()
        mock_connect.assert_not_called()
        mock_redis.assert_not_called()
        mock_trigger.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)


# ---------------------------------------------------------------------------
# Stage U6 — BithumbDbWriter + BithumbRestFallbackController + helper
# (11 acceptance criteria, Codex 합의)
# ---------------------------------------------------------------------------


class TestFetchBithumbUsdtTick(unittest.TestCase):
    """A1, A2, A11 — normalized REST helper + wrapper 호환 + invalid guard."""

    def test_normalized_shape_a1(self):
        """A1: fetch_bithumb_usdt_tick → {source, asset, rate, timestamp_ms}."""
        from app.crawlers.usdt_sources import fetch_bithumb_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = [{
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486.0,
            "trade_timestamp": 1777370239843,
            "timestamp": 1777370240080,
        }]
        mock_response.raise_for_status = MagicMock()
        with patch(
            "app.crawlers.usdt_sources.requests.get", return_value=mock_response,
        ):
            tick = fetch_bithumb_usdt_tick()
        self.assertEqual(tick, {
            "source": "bithumb",
            "asset": "usdt-krw",
            "rate": 1486.0,
            "timestamp_ms": 1777370239843,
        })

    def test_fetch_bithumb_rate_only_wrapper_a2(self):
        """A2: _fetch_bithumb()은 rate만 반환 (호환 유지)."""
        from app.crawlers.usdt_sources import _fetch_bithumb

        mock_response = MagicMock()
        mock_response.json.return_value = [{
            "trade_price": 1490.5,
            "trade_timestamp": 1,
        }]
        mock_response.raise_for_status = MagicMock()
        with patch(
            "app.crawlers.usdt_sources.requests.get", return_value=mock_response,
        ):
            rate = _fetch_bithumb()
        self.assertEqual(rate, 1490.5)

    def test_timestamp_fallback(self):
        """trade_timestamp 없으면 timestamp 폴백."""
        from app.crawlers.usdt_sources import fetch_bithumb_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = [{
            "trade_price": 1486,
            "timestamp": 1777370240080,
        }]
        mock_response.raise_for_status = MagicMock()
        with patch(
            "app.crawlers.usdt_sources.requests.get", return_value=mock_response,
        ):
            tick = fetch_bithumb_usdt_tick()
        self.assertEqual(tick["timestamp_ms"], 1777370240080)

    def test_invalid_returns_none_a11_zero(self):
        """A11: rate=0 → None (Upbit guard mirror)."""
        from app.crawlers.usdt_sources import fetch_bithumb_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = [{"trade_price": 0, "trade_timestamp": 1}]
        mock_response.raise_for_status = MagicMock()
        with patch(
            "app.crawlers.usdt_sources.requests.get", return_value=mock_response,
        ):
            self.assertIsNone(fetch_bithumb_usdt_tick())

    def test_invalid_returns_none_a11_negative(self):
        """A11: rate<0 → None."""
        from app.crawlers.usdt_sources import fetch_bithumb_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = [{"trade_price": -1.5, "trade_timestamp": 1}]
        mock_response.raise_for_status = MagicMock()
        with patch(
            "app.crawlers.usdt_sources.requests.get", return_value=mock_response,
        ):
            self.assertIsNone(fetch_bithumb_usdt_tick())

    def test_invalid_returns_none_a11_request_exception(self):
        """A11: requests.RequestException → None."""
        import requests as _requests
        from app.crawlers.usdt_sources import fetch_bithumb_usdt_tick

        with patch(
            "app.crawlers.usdt_sources.requests.get",
            side_effect=_requests.RequestException("network down"),
        ):
            self.assertIsNone(fetch_bithumb_usdt_tick())

    def test_invalid_returns_none_a11_parse_error(self):
        """A11: JSON parse error / KeyError → None."""
        from app.crawlers.usdt_sources import fetch_bithumb_usdt_tick

        mock_response = MagicMock()
        mock_response.json.return_value = [{}]  # KeyError on trade_price
        mock_response.raise_for_status = MagicMock()
        with patch(
            "app.crawlers.usdt_sources.requests.get", return_value=mock_response,
        ):
            self.assertIsNone(fetch_bithumb_usdt_tick())


class TestBithumbDbWriterDebounce(unittest.IsolatedAsyncioTestCase):
    """A3 — DB writer 1초 window debounce + close pending tick flush."""

    async def test_schedule_within_window_only_last_tick_written(self):
        """window 안 multiple schedule → 마지막 tick만 helper 호출."""
        writer = BithumbDbWriter(window_sec=0.05)
        written_ticks: list[dict] = []

        async def fake_to_thread(func, tick):
            written_ticks.append(tick)

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            writer.schedule(_make_valid_tick(rate=1.0, ts_ms=1))
            writer.schedule(_make_valid_tick(rate=2.0, ts_ms=2))
            writer.schedule(_make_valid_tick(rate=3.0, ts_ms=3))
            await asyncio.sleep(0.1)  # window expire

        self.assertEqual(len(written_ticks), 1)
        self.assertEqual(written_ticks[0]["rate"], 3.0)

    async def test_close_flushes_pending_tick(self):
        """close → pending tick 즉시 flush (1초 기다리지 않음)."""
        writer = BithumbDbWriter(window_sec=10.0)  # 의도적으로 길게
        written_ticks: list[dict] = []

        async def fake_to_thread(func, tick):
            written_ticks.append(tick)

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            writer.schedule(_make_valid_tick(rate=42.0, ts_ms=1))
            await writer.close()

        # close에서 timer cancel + pending tick 즉시 flush
        self.assertEqual(len(written_ticks), 1)
        self.assertEqual(written_ticks[0]["rate"], 42.0)
        self.assertIsNone(writer._pending_tick)


class TestBithumbDbWriterFailureIsolation(unittest.IsolatedAsyncioTestCase):
    """A4 — DB write 예외는 WS loop 전파 X."""

    async def test_db_write_exception_isolated(self):
        """to_thread raise → log only, raise propagation X."""
        writer = BithumbDbWriter(window_sec=0.05)

        async def fake_to_thread(func, tick):
            raise RuntimeError("DB down")

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), self.assertLogs(
            "exchange_rate.crawler.usdt_ws.bithumb", level="WARNING",
        ) as cm:
            writer.schedule(_make_valid_tick())
            await asyncio.sleep(0.1)

        self.assertTrue(any("DB write failed" in m for m in cm.output))

    async def test_db_write_race_prevention_pending_re_scheduled(self):
        """write 진행 중 새 tick 도착 → finally에서 새 timer 예약 (누락 방지)."""
        writer = BithumbDbWriter(window_sec=0.05)
        written_ticks: list[dict] = []
        call_count = {"n": 0}

        async def fake_to_thread(func, tick):
            call_count["n"] += 1
            # write 진행 중 새 tick 추가
            if call_count["n"] == 1:
                writer.schedule(_make_valid_tick(rate=999.0, ts_ms=999))
            written_ticks.append(tick)

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            writer.schedule(_make_valid_tick(rate=1.0, ts_ms=1))
            await asyncio.sleep(0.2)  # 두 window cycle

        self.assertEqual(call_count["n"], 2)
        self.assertEqual(written_ticks[0]["rate"], 1.0)
        self.assertEqual(written_ticks[1]["rate"], 999.0)


class TestRestFallbackInFlightCooldown(unittest.IsolatedAsyncioTestCase):
    """A5 — in-flight + cooldown guard."""

    async def test_in_flight_skip(self):
        """이미 in-flight 시 두 번째 schedule_probe 즉시 skip."""
        redis_writer = MagicMock()
        db_writer = MagicMock()
        controller = BithumbRestFallbackController(
            redis_writer=redis_writer, db_writer=db_writer,
        )
        controller._in_flight = True

        with patch.object(asyncio, "get_running_loop") as mock_loop:
            controller.schedule_probe(reason="test")
        # in_flight True → create_task 호출 0
        mock_loop.assert_not_called()

    async def test_cooldown_skip(self):
        """cooldown_until > now → schedule_probe skip."""
        redis_writer = MagicMock()
        db_writer = MagicMock()
        controller = BithumbRestFallbackController(
            redis_writer=redis_writer, db_writer=db_writer,
        )
        controller._cooldown_until = time.time() + 60.0  # 미래

        with patch.object(asyncio, "get_running_loop") as mock_loop:
            controller.schedule_probe(reason="test")
        mock_loop.assert_not_called()

    async def test_reset_cooldown_a8(self):
        """A8: reset_cooldown → _cooldown_until = 0."""
        redis_writer = MagicMock()
        db_writer = MagicMock()
        controller = BithumbRestFallbackController(
            redis_writer=redis_writer, db_writer=db_writer,
        )
        controller._cooldown_until = time.time() + 60.0
        controller.reset_cooldown()
        self.assertEqual(controller._cooldown_until, 0.0)


class TestRestFallbackProbeFanoutA6(unittest.IsolatedAsyncioTestCase):
    """A6 — probe success → Redis + DB schedule, alert schedule 0 (U7 영역)."""

    async def test_probe_success_fanout_to_redis_and_db_only(self):
        """probe success → redis + db schedule. alert evaluator 자체 없음."""
        redis_writer = MagicMock()
        db_writer = MagicMock()
        controller = BithumbRestFallbackController(
            redis_writer=redis_writer, db_writer=db_writer,
        )
        tick = _make_valid_tick(rate=1500.0, ts_ms=12345)

        async def fake_to_thread(func):
            return tick

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            await controller._run_probe(reason="test")

        redis_writer.schedule.assert_called_once_with(tick)
        db_writer.schedule.assert_called_once_with(tick)
        # alert evaluator는 controller에 없음 (U7 영역, acceptance 6)
        self.assertFalse(hasattr(controller, "_alert_evaluator"))

    async def test_probe_none_no_fanout(self):
        """probe None 반환 (REST 실패/A11 invalid) → fanout 0."""
        redis_writer = MagicMock()
        db_writer = MagicMock()
        controller = BithumbRestFallbackController(
            redis_writer=redis_writer, db_writer=db_writer,
        )

        async def fake_to_thread(func):
            return None  # A11 guard

        with patch(
            "app.crawlers.usdt_ws.bithumb.asyncio.to_thread",
            side_effect=fake_to_thread,
        ), self.assertLogs(
            "exchange_rate.crawler.usdt_ws.bithumb", level="WARNING",
        ):
            await controller._run_probe(reason="test")

        redis_writer.schedule.assert_not_called()
        db_writer.schedule.assert_not_called()


class TestU6CloseOrderA9(unittest.IsolatedAsyncioTestCase):
    """A9 — close 순서: fallback → DB → Redis (3개)."""

    async def test_close_order_in_run_one_session_finally(self):
        """_run_one_session finally → fallback → DB → Redis 순서로 close 호출."""
        client = BithumbWsClient()
        client._stop_event.set()  # while loop entry 0
        ws_mock = AsyncMock()
        ws_mock.recv = AsyncMock()
        ws_mock.send = AsyncMock()
        connect_mock = AsyncMock()
        connect_mock.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_mock.__aexit__ = AsyncMock(return_value=None)

        call_order: list[str] = []

        async def fallback_close():
            call_order.append("fallback")

        async def db_close():
            call_order.append("db")

        async def redis_close(*args, **kwargs):
            call_order.append("redis")

        with patch(
            "app.crawlers.usdt_ws.bithumb.websockets.connect",
            return_value=connect_mock,
        ), patch.object(
            client._fallback_controller, "close", side_effect=fallback_close,
        ), patch.object(
            client._db_writer, "close", side_effect=db_close,
        ), patch.object(
            client._redis_writer, "close", side_effect=redis_close,
        ):
            await client._run_one_session()

        # 순서 검증: fallback → DB → Redis (alert 없음, 3개)
        self.assertEqual(call_order, ["fallback", "db", "redis"])


class TestU6ValidTickFanout(unittest.IsolatedAsyncioTestCase):
    """U6 fanout 통합 — valid tick → Redis + DB schedule (alert 없음)."""

    async def test_valid_tick_schedules_redis_and_db(self):
        client = BithumbWsClient()
        valid_message = json.dumps({
            "type": "ticker",
            "code": "KRW-USDT",
            "trade_price": 1486,
            "trade_timestamp": 1777370239843,
        })
        recv_responses = [valid_message]

        async def fake_recv():
            if recv_responses:
                return recv_responses.pop(0)
            client._stop_event.set()
            await asyncio.sleep(0.001)
            raise asyncio.TimeoutError

        ws_mock = AsyncMock()
        ws_mock.recv = fake_recv
        ws_mock.send = AsyncMock()
        ws_mock.ping = AsyncMock(return_value=asyncio.Future())
        ws_mock.close = AsyncMock()
        connect_mock = AsyncMock()
        connect_mock.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_mock.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.bithumb.websockets.connect",
            return_value=connect_mock,
        ), patch.object(
            client._redis_writer, "schedule",
        ) as mock_redis_sched, patch.object(
            client._db_writer, "schedule",
        ) as mock_db_sched, patch.object(
            client._redis_writer, "close", new_callable=AsyncMock,
        ), patch.object(
            client._db_writer, "close", new_callable=AsyncMock,
        ), patch.object(
            client._fallback_controller, "close", new_callable=AsyncMock,
        ):
            await client._run_one_session()

        # valid tick → Redis + DB 둘 다 schedule (1회씩)
        self.assertEqual(mock_redis_sched.call_count, 1)
        self.assertEqual(mock_db_sched.call_count, 1)


class TestFlagFalseInvariantU6Regression(unittest.IsolatedAsyncioTestCase):
    """A10 — flag=false → client 생성 0, network/Redis/DB/REST 0 (U6 누적 후)."""

    def setUp(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_bithumb_globals()

    async def test_flag_false_skips_all_u6_paths(self):
        """U6 누적 후에도 flag=false → 모든 외부 IO 0."""
        with patch.object(config, "USDT_WS_BITHUMB_ENABLED", False), \
             patch("app.crawlers.usdt_ws.bithumb.BithumbWsClient") as mock_client_cls, \
             patch("app.crawlers.usdt_ws.bithumb.BithumbRedisWriter") as mock_redis_cls, \
             patch("app.crawlers.usdt_ws.bithumb.BithumbDbWriter") as mock_db_cls, \
             patch("app.crawlers.usdt_ws.bithumb.BithumbRestFallbackController") as mock_fb_cls, \
             patch("app.crawlers.usdt_ws.bithumb.websockets.connect") as mock_connect, \
             patch(
                 "app.crawlers.usdt_ws.bithumb.latest_rates_cache.set_latest_usdt_rate_from_sync_job"
             ) as mock_redis, \
             patch(
                 "app.crawlers.usdt_ws.bithumb.tether_topic_trigger.request_tether_topic_trigger"
             ) as mock_trigger:
            await scheduler.start_usdt_ws_bithumb_client()
        mock_client_cls.assert_not_called()
        mock_redis_cls.assert_not_called()
        mock_db_cls.assert_not_called()
        mock_fb_cls.assert_not_called()
        mock_connect.assert_not_called()
        mock_redis.assert_not_called()
        mock_trigger.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_bithumb_client)
        self.assertIsNone(scheduler.usdt_ws_bithumb_task)


if __name__ == "__main__":
    unittest.main()
