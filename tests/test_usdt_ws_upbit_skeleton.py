"""USDT WS Upbit canary 단위 테스트 (PR1 lifecycle + PR2 connect/parse).

USDT_WS_DESIGN_PLAN §12 PR1/PR2 검증.

PR1 lifecycle (scheduler glue): network 의존성 제거 — `_run_usdt_ws_upbit_client`
mock 패턴 (KRX `_bootstrap_*` mock 패턴 mirror).

PR2 client behavior:
    - parser: bytes/str/dict 입력, valid/invalid 케이스, timestamp fallback
    - session: subscribe payload, send/recv mock, stop closes ws
    - no downstream side-effect: Redis/DB/alert/fallback import 없음

실행:
    python -m unittest tests.test_usdt_ws_upbit_skeleton -v
"""
from __future__ import annotations

import asyncio
import inspect
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.crawlers.usdt_ws import upbit as upbit_mod
from app.crawlers.usdt_ws.upbit import (
    UPBIT_SUBSCRIBE_TICKET,
    UPBIT_TARGET_CODE,
    UPBIT_WS_URL,
    UpbitWsClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_scheduler_usdt_ws_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_upbit_client = None
    scheduler.usdt_ws_upbit_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_upbit_client` mock — client.stop()까지 stop_event 대기.

    PR1 lifecycle test가 network 의존성 없이 task 생성/cleanup만 검증하도록.
    PR2 이후 client.start()가 실제 connect를 시도하므로 본 mock 필수.
    """
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# PR1 — scheduler lifecycle (network 무관, _run_*  mock)
# ---------------------------------------------------------------------------

class TestStartUsdtWsUpbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_globals()

    async def test_skips_when_disabled(self):
        """USDT_WS_UPBIT_ENABLED=false → client/task 미생성."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", False), \
             self.assertLogs("exchange_rate.scheduler", level="INFO"):
            await scheduler.start_usdt_ws_upbit_client()
        self.assertIsNone(scheduler.usdt_ws_upbit_client)
        self.assertIsNone(scheduler.usdt_ws_upbit_task)

    async def test_creates_task_when_enabled(self):
        """USDT_WS_UPBIT_ENABLED=true → UpbitWsClient + task 생성."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_upbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_upbit_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_upbit_client)
                self.assertIsNotNone(scheduler.usdt_ws_upbit_task)
                self.assertFalse(scheduler.usdt_ws_upbit_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_upbit_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_upbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_upbit_client()
            first_task = scheduler.usdt_ws_upbit_task
            first_client = scheduler.usdt_ws_upbit_client
            self.assertIsNotNone(first_task)

            try:
                await scheduler.start_usdt_ws_upbit_client()
                self.assertIs(scheduler.usdt_ws_upbit_task, first_task)
                self.assertIs(scheduler.usdt_ws_upbit_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_upbit_client()


class TestShutdownUsdtWsUpbitClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_UPBIT_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_upbit_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_upbit_client()
            task = scheduler.usdt_ws_upbit_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())

            await scheduler.shutdown_usdt_ws_upbit_client()

            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_upbit_client)
            self.assertIsNone(scheduler.usdt_ws_upbit_task)

    async def test_shutdown_is_noop_when_not_started(self):
        """start 안 한 상태에서 shutdown → exception 없음, globals 그대로 None."""
        await scheduler.shutdown_usdt_ws_upbit_client()
        self.assertIsNone(scheduler.usdt_ws_upbit_client)
        self.assertIsNone(scheduler.usdt_ws_upbit_task)


# ---------------------------------------------------------------------------
# PR2 — UpbitWsClient parser
# ---------------------------------------------------------------------------

def _make_valid_ticker_dict(**overrides):
    base = {
        "type": "ticker",
        "code": "KRW-USDT",
        "trade_price": 1486.0,
        "trade_timestamp": 1777370239843,
        "timestamp": 1777370240080,
        "stream_type": "REALTIME",
    }
    base.update(overrides)
    return base


class TestUpbitParser(unittest.TestCase):

    def setUp(self):
        self.client = UpbitWsClient()

    def test_parse_valid_dict(self):
        message = _make_valid_ticker_dict()
        tick = self.client._parse_ticker_message(message)
        self.assertEqual(tick, {
            "source": "upbit",
            "asset": "usdt-krw",
            "rate": 1486.0,
            "timestamp_ms": 1777370239843,
        })

    def test_parse_valid_json_str(self):
        message = _make_valid_ticker_dict()
        tick = self.client._parse_ticker_message(json.dumps(message))
        self.assertIsNotNone(tick)
        self.assertEqual(tick["rate"], 1486.0)
        self.assertEqual(tick["timestamp_ms"], 1777370239843)

    def test_parse_valid_json_bytes(self):
        message = _make_valid_ticker_dict()
        raw = json.dumps(message).encode("utf-8")
        tick = self.client._parse_ticker_message(raw)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["source"], "upbit")
        self.assertEqual(tick["asset"], "usdt-krw")

    def test_parse_trade_timestamp_fallback_to_timestamp(self):
        """trade_timestamp 없음, timestamp 있음 → valid (guide §3 spec)."""
        message = _make_valid_ticker_dict()
        del message["trade_timestamp"]
        tick = self.client._parse_ticker_message(message)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["timestamp_ms"], 1777370240080)

    def test_parse_both_timestamps_missing_returns_none(self):
        message = _make_valid_ticker_dict()
        del message["trade_timestamp"]
        del message["timestamp"]
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_non_ticker_type_returns_none(self):
        message = _make_valid_ticker_dict(type="status")
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_non_krw_usdt_code_returns_none(self):
        message = _make_valid_ticker_dict(code="KRW-BTC")
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_missing_trade_price_returns_none(self):
        message = _make_valid_ticker_dict()
        del message["trade_price"]
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_non_numeric_trade_price_returns_none(self):
        message = _make_valid_ticker_dict(trade_price="abc")
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_zero_price_returns_none(self):
        message = _make_valid_ticker_dict(trade_price=0)
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_negative_price_returns_none(self):
        message = _make_valid_ticker_dict(trade_price=-1)
        self.assertIsNone(self.client._parse_ticker_message(message))

    def test_parse_invalid_json_returns_none(self):
        self.assertIsNone(self.client._parse_ticker_message("not json"))

    def test_parse_non_dict_json_returns_none(self):
        """JSON valid이지만 dict 아님 (list, scalar)."""
        self.assertIsNone(self.client._parse_ticker_message("[1, 2, 3]"))
        self.assertIsNone(self.client._parse_ticker_message("42"))

    def test_parse_invalid_input_type_returns_none(self):
        self.assertIsNone(self.client._parse_ticker_message(12345))
        self.assertIsNone(self.client._parse_ticker_message(None))


# ---------------------------------------------------------------------------
# PR2 — UpbitWsClient session (websockets mock)
# ---------------------------------------------------------------------------

class TestUpbitSession(unittest.IsolatedAsyncioTestCase):

    def _make_mock_ws(self, recv_side_effect):
        """websockets ClientConnection mock — async context manager + send/recv/close.

        KRX `test_krx_kis.py` 패턴 mirror.
        """
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)
        mock_ws.close = AsyncMock()
        mock_connect = AsyncMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect.__aexit__ = AsyncMock(return_value=None)
        return mock_ws, mock_connect

    async def test_session_sends_subscribe_payload(self):
        """session 시작 시 정확한 3-frame subscribe payload 전송."""
        # recv는 즉시 stop 신호 — 빠르게 종료
        client = UpbitWsClient()
        client._stop_event.set()  # 첫 recv 진입 전 stop

        async def recv_blocks():
            await asyncio.sleep(0)
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_blocks)

        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect):
            await client._run_one_session()

        # send 1회 호출 — subscribe payload
        self.assertEqual(mock_ws.send.call_count, 1)
        sent_raw = mock_ws.send.call_args[0][0]
        sent = json.loads(sent_raw)
        self.assertEqual(sent[0], {"ticket": UPBIT_SUBSCRIBE_TICKET})
        self.assertEqual(sent[1], {"type": "ticker", "codes": [UPBIT_TARGET_CODE]})
        self.assertEqual(sent[2], {"format": "DEFAULT"})

    async def test_session_processes_messages_and_stops(self):
        """N개 valid message recv → parse → stop 시 정상 종료."""
        client = UpbitWsClient()

        msg1 = json.dumps(_make_valid_ticker_dict(trade_price=1486.0))
        msg2 = json.dumps(_make_valid_ticker_dict(trade_price=1487.0))
        recv_calls = [0]

        async def recv_seq():
            recv_calls[0] += 1
            if recv_calls[0] == 1:
                return msg1
            if recv_calls[0] == 2:
                return msg2
            # 두 메시지 후 stop 신호 → wait_for timeout 흉내 + stop 트리거
            client._stop_event.set()
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_seq)

        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect):
            await client._run_one_session()

        # 두 message 처리 + 1번 TimeoutError → recv 3회 호출
        self.assertGreaterEqual(mock_ws.recv.call_count, 3)
        self.assertTrue(client._first_tick_logged)

    async def test_stop_closes_ws(self):
        """stop() 호출 → ws.close() + stop_event set."""
        client = UpbitWsClient()
        mock_ws = MagicMock()
        mock_ws.close = AsyncMock()
        client._ws = mock_ws

        await client.stop()

        self.assertTrue(client._stop_event.is_set())
        mock_ws.close.assert_awaited_once()

    async def test_stop_with_no_ws_is_safe(self):
        """ws None 상태에서 stop() → exception 없이 stop_event만 set."""
        client = UpbitWsClient()
        await client.stop()
        self.assertTrue(client._stop_event.is_set())

    async def test_start_double_call_skipped_when_running(self):
        """start() 호출 중 다시 start() → 중복 무시 (log debug + return)."""
        client = UpbitWsClient()
        client._running = True  # 이미 실행 중 상태 시뮬레이션
        # _run_one_session이 호출되면 안 됨
        with patch.object(client, "_run_one_session", new=AsyncMock()) as mock_run:
            await client.start()
            mock_run.assert_not_called()

    async def test_connection_closed_during_stop_is_normal(self):
        """stop_event 설정 후 ConnectionClosed raise → 정상 종료, 예외 propagate X.

        실제 race: stop()이 ws.close()를 부르면 in-flight recv()가
        ConnectionClosedOK raise. 본 경로가 crashed log로 잘못 분류되면 안 됨.
        """
        from websockets.exceptions import ConnectionClosedOK

        client = UpbitWsClient()
        client._stop_event.set()  # 첫 recv 진입 전부터 stop 상태

        async def recv_raises_closed():
            raise ConnectionClosedOK(None, None)

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_raises_closed)

        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect):
            # raise되면 테스트 fail (assertRaises 없음 = 정상 종료 기대)
            await client._run_one_session()

    async def test_connection_closed_without_stop_propagates(self):
        """stop 미요청 상태에서 ConnectionClosed → 예외 propagate.

        wrapper `_run_usdt_ws_upbit_client`가 crashed log → PR3 reconnect 신호.
        """
        from websockets.exceptions import ConnectionClosedError

        client = UpbitWsClient()
        # stop_event 미설정 — 비요청 disconnect 시뮬레이션

        async def recv_raises_error():
            raise ConnectionClosedError(None, None)

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_raises_error)

        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect):
            with self.assertRaises(ConnectionClosedError):
                await client._run_one_session()


# ---------------------------------------------------------------------------
# PR2 — No downstream side-effect guard
# (PR1 TestNoNetworkGuarantee 후속 — network 코드는 들어왔지만
#  Redis/DB/alert/fallback은 여전히 X)
# ---------------------------------------------------------------------------

class TestNoDownstreamSideEffects(unittest.TestCase):
    """PR2는 connect/parse/log만. Redis/DB/alert/fallback은 PR4-PR7 범위.

    docstring/comment에서 미래 PR을 참조할 수 있으므로 substring 매칭이 아닌
    `import` 라인 + 함수 호출 패턴(`<name>(`)으로 좁힘.
    """

    @staticmethod
    def _extract_import_lines(source: str) -> list[str]:
        import re
        return re.findall(r'^\s*(?:from|import)\s+.*$', source, re.MULTILINE)

    def test_upbit_module_has_no_downstream_imports(self):
        source = inspect.getsource(upbit_mod)
        imports = "\n".join(self._extract_import_lines(source))
        # Redis writer (PR4)
        self.assertNotIn("latest_rates_cache", imports)
        # DB writer (PR5)
        self.assertNotIn("SourceRate", imports)
        self.assertNotIn("SessionLocal", imports)
        self.assertNotIn("from app.crud", imports)
        # alert (PR6)
        self.assertNotIn("process_source_rate_alerts", imports)
        self.assertNotIn("AlertObservation", imports)
        self.assertNotIn("from app.notifications", imports)
        # REST fallback (PR7)
        self.assertNotIn("aiohttp", imports)
        self.assertNotIn("requests", imports)
        self.assertNotIn("httpx", imports)

    def test_upbit_module_has_no_downstream_invocations(self):
        """call/instantiation 패턴 — docstring 언급은 무시, 실제 호출만 검출."""
        source = inspect.getsource(upbit_mod)
        forbidden_calls = [
            # Redis writer
            "set_latest_usdt_rate(",
            # DB writer
            "insert_source_rate_if_changed(",
            "SourceRate(",
            # alert
            "process_source_rate_alerts(",
            "send_fcm(",
            "AlertObservation(",
        ]
        for pattern in forbidden_calls:
            self.assertNotIn(pattern, source, f"Forbidden PR4-PR7 invocation: {pattern}")


if __name__ == "__main__":
    unittest.main()
