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
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.crawlers.usdt_ws.bithumb import (
    BITHUMB_SUBSCRIBE_TICKET,
    BITHUMB_TARGET_CODE,
    BITHUMB_WS_URL,
    BithumbWsClient,
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
        """__init__ → stop_event (not set) + running False."""
        client = BithumbWsClient()
        self.assertFalse(client._stop_event.is_set())
        self.assertFalse(client._running)

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


if __name__ == "__main__":
    unittest.main()
