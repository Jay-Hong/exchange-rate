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
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.crawlers.usdt_ws import upbit as upbit_mod
from app.crawlers.usdt_ws.upbit import (
    MAX_PENDING_WRITES,
    PING_INTERVAL_SEC,
    PING_TIMEOUT_SEC,
    RECONNECT_BACKOFF_SEQ,
    RECONNECT_BACKOFF_TAIL,
    STALE_AFTER_SEC,
    UPBIT_SUBSCRIBE_TICKET,
    UPBIT_TARGET_CODE,
    UPBIT_WS_URL,
    UpbitRedisWriter,
    UpbitWsClient,
    UsdtLivenessMonitor,
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

    async def test_invalid_frame_does_not_increment_tick_count(self):
        """Codex Finding 2 회귀 가드: invalid/non-ticker frame은 observe_tick X.

        Liveness activity와 valid ticker metric 분리. tick_count는 valid
        ticker frame만 집계 (gap bucket, max_frame_gap_sec 동일).
        """
        client = UpbitWsClient()

        non_ticker = json.dumps({
            "type": "status",  # non-ticker → ignored
            "code": "KRW-USDT",
            "trade_price": 1486,
            "trade_timestamp": 1777370239843,
        })
        valid_ticker = json.dumps(_make_valid_ticker_dict())
        recv_calls = [0]

        async def recv_seq():
            recv_calls[0] += 1
            if recv_calls[0] == 1:
                return non_ticker
            if recv_calls[0] == 2:
                return valid_ticker
            client._stop_event.set()
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_seq)
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect):
            await client._run_one_session()

        # non-ticker (1번째) + valid (2번째) 둘 다 recv 했지만
        # tick_count는 valid 1개만
        self.assertEqual(client._liveness.tick_count, 1)
        # gap bucket도 valid 첫 tick뿐이라 0 (gap 측정은 두 번째 tick부터)
        for key, count in client._liveness.gap_buckets.items():
            self.assertEqual(count, 0, f"non-ticker should not populate gap bucket {key}")

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
    """PR4: Redis writer (`set_latest_usdt_rate_from_sync_job`) 허용.
    DB/alert/fallback은 PR5-PR7 범위라 여전히 금지.

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
        # Redis writer (PR4) — latest_rates_cache 허용. set_latest_usdt_rate
        # helper만 사용, 다른 helper나 mirror cycle은 X.
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
            # DB writer (PR5)
            "insert_source_rate_if_changed(",
            "SourceRate(",
            # alert (PR6)
            "process_source_rate_alerts(",
            "send_fcm(",
            "AlertObservation(",
            # REST fallback (PR7) — helper 호출 (HTTP client invocation)
            "aiohttp.ClientSession(",
            "requests.get(",
            "requests.post(",
            "httpx.get(",
            "httpx.post(",
        ]
        for pattern in forbidden_calls:
            self.assertNotIn(pattern, source, f"Forbidden PR5-PR7 invocation: {pattern}")

    def test_upbit_module_uses_only_allowed_redis_helper(self):
        """PR4 Redis writer는 set_latest_usdt_rate_from_sync_job만 호출.

        다른 latest_rates_cache helper (set_latest_krx_*, set_latest_bank_*,
        mirror cycle helper 등) 미사용 — USDT 외 source 오염 방지.
        """
        source = inspect.getsource(upbit_mod)
        # USDT 전용 helper만 허용
        self.assertIn("set_latest_usdt_rate_from_sync_job", source)
        # 다른 helper는 호출 X
        forbidden_helpers = [
            "set_latest_krx_rate_from_sync_job(",
            "set_latest_bank_rate_from_sync_job(",
            "set_latest_investing_rate_from_sync_job(",
            "warmup_latest_rates(",
            "mirror_latest_to_redis(",
        ]
        for pattern in forbidden_helpers:
            self.assertNotIn(pattern, source, f"Forbidden cross-source helper: {pattern}")


# ---------------------------------------------------------------------------
# PR3 — UsdtLivenessMonitor
# ---------------------------------------------------------------------------

class TestUsdtLivenessMonitor(unittest.TestCase):

    def setUp(self):
        self.monitor = UsdtLivenessMonitor()

    def test_initial_state(self):
        self.assertEqual(self.monitor.frame_count_total, 0)
        self.assertEqual(self.monitor.tick_count, 0)
        self.assertEqual(self.monitor.heartbeat_count, 0)
        self.assertIsNone(self.monitor.last_tick_at)
        self.assertIsNone(self.monitor.last_heartbeat_at)
        self.assertIsNone(self.monitor.last_activity_at)
        self.assertEqual(self.monitor.max_frame_gap_sec, 0.0)

    def test_observe_tick_updates_counters_and_gap(self):
        self.monitor.observe_tick(100.0)
        self.assertEqual(self.monitor.tick_count, 1)
        self.assertEqual(self.monitor.frame_count_total, 1)
        self.assertEqual(self.monitor.last_tick_at, 100.0)
        # 첫 tick은 gap 없음
        self.assertEqual(self.monitor.max_frame_gap_sec, 0.0)

        self.monitor.observe_tick(103.5)
        self.assertEqual(self.monitor.tick_count, 2)
        self.assertEqual(self.monitor.last_tick_at, 103.5)
        # gap = 3.5s → "<=5s" bucket
        self.assertEqual(self.monitor.max_frame_gap_sec, 3.5)
        self.assertEqual(self.monitor.gap_buckets["<=5s"], 1)

    def test_observe_heartbeat_updates_separately(self):
        self.monitor.observe_heartbeat(200.0)
        self.assertEqual(self.monitor.heartbeat_count, 1)
        self.assertEqual(self.monitor.frame_count_total, 1)
        self.assertEqual(self.monitor.last_heartbeat_at, 200.0)
        # heartbeat은 tick gap에 영향 없음
        self.assertEqual(self.monitor.tick_count, 0)
        self.assertIsNone(self.monitor.last_tick_at)

    def test_bucket_for_boundaries(self):
        cases = [
            (0.5, "<=1s"), (1.0, "<=1s"),
            (1.5, "<=2s"), (2.0, "<=2s"),
            (3.0, "<=5s"), (5.0, "<=5s"),
            (7.0, "<=10s"), (10.0, "<=10s"),
            (20.0, "<=30s"), (30.0, "<=30s"),
            (45.0, "<=60s"), (60.0, "<=60s"),
            (61.0, ">60s"), (300.0, ">60s"),
        ]
        for gap, expected in cases:
            self.assertEqual(
                UsdtLivenessMonitor._bucket_for(gap), expected,
                f"gap={gap} expected={expected}",
            )

    def test_reset_active_session_clears_active_state_keeps_lifetime(self):
        self.monitor.observe_tick(100.0)
        self.monitor.observe_tick(105.0)
        self.monitor.observe_heartbeat(110.0)
        # lifetime counters
        self.assertEqual(self.monitor.tick_count, 2)
        self.assertEqual(self.monitor.heartbeat_count, 1)
        self.assertEqual(self.monitor.frame_count_total, 3)

        self.monitor.reset_active_session()

        # active session reset
        self.assertIsNone(self.monitor.last_tick_at)
        self.assertIsNone(self.monitor.last_heartbeat_at)
        self.assertEqual(self.monitor.max_frame_gap_sec, 0.0)
        self.assertEqual(self.monitor.gap_buckets["<=5s"], 0)
        # lifetime 유지
        self.assertEqual(self.monitor.tick_count, 2)
        self.assertEqual(self.monitor.heartbeat_count, 1)
        self.assertEqual(self.monitor.frame_count_total, 3)

    def test_last_activity_at_max_of_tick_and_heartbeat(self):
        # 둘 다 None
        self.assertIsNone(self.monitor.last_activity_at)

        # tick만
        self.monitor.observe_tick(100.0)
        self.assertEqual(self.monitor.last_activity_at, 100.0)

        # heartbeat만 (tick보다 늦음)
        self.monitor.observe_heartbeat(200.0)
        self.assertEqual(self.monitor.last_activity_at, 200.0)

        # tick이 다시 늦게
        self.monitor.observe_tick(300.0)
        self.assertEqual(self.monitor.last_activity_at, 300.0)

        # heartbeat이 더 늦지 않음 (max 유지)
        self.monitor.observe_heartbeat(250.0)
        self.assertEqual(self.monitor.last_activity_at, 300.0)


# ---------------------------------------------------------------------------
# PR3 — Stale detection (§5 핵심 compliance)
# ---------------------------------------------------------------------------

class TestUpbitStaleDetection(unittest.TestCase):
    """§5: stale 기준은 ticker silence가 아니라 frame/heartbeat silence."""

    def setUp(self):
        self.monitor = UsdtLivenessMonitor()

    def test_is_stale_false_when_no_activity_yet(self):
        # 초기 상태 — last_activity_at None → stale 아님 (grace 효과)
        self.assertFalse(self.monitor.is_stale(now=100.0, threshold=STALE_AFTER_SEC))

    def test_no_stale_when_heartbeat_fresh_but_ticker_silent(self):
        """§5 핵심: ticker 5분 silent + heartbeat 10초 전 → NOT stale.

        저유동성 정상 구간을 stale로 오인하면 PR7 REST fallback false positive.
        본 케이스가 회귀 차단의 핵심.
        """
        self.monitor.last_tick_at = 1000.0  # tick 5분 전
        self.monitor.last_heartbeat_at = 1290.0  # heartbeat 10초 전
        # last_activity_at = max(1000, 1290) = 1290
        # is_stale(1300, 30) → 1300 - 1290 = 10 ≤ 30 → False
        self.assertFalse(self.monitor.is_stale(now=1300.0, threshold=STALE_AFTER_SEC))

    def test_stale_when_both_tick_and_heartbeat_silent(self):
        """둘 다 STALE_AFTER_SEC 초과 silence → stale."""
        self.monitor.last_tick_at = 1000.0
        self.monitor.last_heartbeat_at = 1000.0
        # last_activity_at = 1000, now=1100 → 100s silence > 30s → stale
        self.assertTrue(self.monitor.is_stale(now=1100.0, threshold=STALE_AFTER_SEC))

    def test_stale_when_only_tick_present_and_silent(self):
        """heartbeat 안 와도 tick만으로 stale 판정 가능."""
        self.monitor.last_tick_at = 1000.0
        self.assertTrue(self.monitor.is_stale(now=1100.0, threshold=STALE_AFTER_SEC))

    def test_stale_when_only_heartbeat_present_and_silent(self):
        """tick 안 와도 heartbeat만으로 stale 판정 가능."""
        self.monitor.last_heartbeat_at = 1000.0
        self.assertTrue(self.monitor.is_stale(now=1100.0, threshold=STALE_AFTER_SEC))

    def test_not_stale_exactly_at_threshold(self):
        """경계값 — silence == threshold는 stale 아님 (strict >)."""
        self.monitor.last_tick_at = 1000.0
        self.monitor.last_heartbeat_at = 1000.0
        self.assertFalse(self.monitor.is_stale(now=1030.0, threshold=STALE_AFTER_SEC))


# ---------------------------------------------------------------------------
# PR3 — UpbitWsClient reconnect + status state machine
# ---------------------------------------------------------------------------

class TestUpbitReconnect(unittest.IsolatedAsyncioTestCase):

    def test_compute_backoff_follows_sequence(self):
        client = UpbitWsClient()
        self.assertEqual(client._compute_backoff(1), RECONNECT_BACKOFF_SEQ[0])
        self.assertEqual(client._compute_backoff(2), RECONNECT_BACKOFF_SEQ[1])
        self.assertEqual(client._compute_backoff(6), RECONNECT_BACKOFF_SEQ[5])
        # 시퀀스 초과 → tail
        self.assertEqual(client._compute_backoff(7), RECONNECT_BACKOFF_TAIL)
        self.assertEqual(client._compute_backoff(100), RECONNECT_BACKOFF_TAIL)

    def test_set_status_transitions_and_counter(self):
        client = UpbitWsClient()
        self.assertEqual(client._status, "normal")

        client._set_status("reconnecting")
        self.assertEqual(client._status, "reconnecting")
        self.assertEqual(client._status_transition_count["reconnecting"], 1)

        client._set_status("normal")
        self.assertEqual(client._status, "normal")
        self.assertEqual(client._status_transition_count["normal"], 1)

        client._set_status("stale")
        self.assertEqual(client._status_transition_count["stale"], 1)

        # 같은 status 재호출은 counter 증가 X
        client._set_status("stale")
        self.assertEqual(client._status_transition_count["stale"], 1)

    async def test_start_reconnects_on_connection_closed(self):
        """unexpected disconnect → backoff 후 재연결 → 두 번째 session 성공."""
        from websockets.exceptions import ConnectionClosedError

        client = UpbitWsClient()
        session_call_count = [0]

        async def fake_run_session():
            session_call_count[0] += 1
            if session_call_count[0] == 1:
                # 첫 세션 — connect 실패 시뮬레이션
                raise ConnectionClosedError(None, None)
            # 두 번째 세션 — stop 신호 받고 정상 종료
            client._stop_event.set()
            return

        with patch.object(client, "_run_one_session", side_effect=fake_run_session), \
             patch.object(client, "_compute_backoff", return_value=0.01):
            await client.start()

        self.assertEqual(session_call_count[0], 2)
        self.assertEqual(client._reconnect_attempt_count, 1)

    async def test_start_stop_during_backoff_exits_cleanly(self):
        """backoff sleep 중 stop_event 도착 → 즉시 루프 종료."""
        from websockets.exceptions import ConnectionClosedError

        client = UpbitWsClient()

        async def fake_run_session():
            raise ConnectionClosedError(None, None)

        async def trigger_stop():
            await asyncio.sleep(0.05)
            client._stop_event.set()

        # backoff 길게 (1s) 잡고 0.05s 후 stop → 즉시 종료해야
        with patch.object(client, "_run_one_session", side_effect=fake_run_session), \
             patch.object(client, "_compute_backoff", return_value=1.0):
            stop_task = asyncio.create_task(trigger_stop())
            try:
                await asyncio.wait_for(client.start(), timeout=0.5)
            except asyncio.TimeoutError:
                self.fail("start() didn't exit when stop_event set during backoff")
            await stop_task

    async def test_status_reconnecting_before_backoff_starts(self):
        """Codex Finding 1 회귀 가드: backoff sleep 진입 전 status=reconnecting.

        이전 버그: except에서 attempt++/backoff 계산만 하고 status는 다음
        iteration 진입 시 갱신 → backoff 동안 status="normal"로 남음.
        Fix: except 진입 즉시 _set_status("reconnecting").
        """
        from websockets.exceptions import ConnectionClosedError

        client = UpbitWsClient()
        status_at_backoff = []
        session_calls = [0]

        async def fake_run_session():
            session_calls[0] += 1
            if session_calls[0] == 1:
                raise ConnectionClosedError(None, None)
            client._stop_event.set()

        def spy_backoff(attempt):
            # _compute_backoff 호출 시점 = except 블록 안 = status 전이 후
            status_at_backoff.append(client._status)
            return 0.01

        with patch.object(client, "_run_one_session", side_effect=fake_run_session), \
             patch.object(client, "_compute_backoff", side_effect=spy_backoff):
            await client.start()

        self.assertEqual(len(status_at_backoff), 1)
        self.assertEqual(
            status_at_backoff[0], "reconnecting",
            "status should be 'reconnecting' when backoff is computed (before sleep)",
        )

    async def test_set_status_has_no_side_effects(self):
        """PR3 guardrail (Codex): _set_status("stale")는 counter + log만.

        REST probe / Redis / DB / alert call X. side effect 가능 메서드들이
        호출되지 않는지 명시적 확인.
        """
        client = UpbitWsClient()
        # 모든 client 메서드에 spy 부착 (있을 법한 side effect 메서드 탐색)
        forbidden_attrs = [
            "_invoke_rest_fallback", "_evaluate_rest_fallback",  # KRX 명명 — USDT엔 없어야
            "_write_redis", "_write_db",
            "_send_alert", "_evaluate_alert",
        ]
        for attr in forbidden_attrs:
            self.assertFalse(
                hasattr(client, attr),
                f"PR3 client에는 {attr} 메서드가 없어야 함 (PR4-PR7 영역)",
            )

        # status 전이만 일어남
        client._set_status("stale")
        self.assertEqual(client._status, "stale")
        self.assertEqual(client._status_transition_count["stale"], 1)


# ---------------------------------------------------------------------------
# PR3 — Heartbeat (explicit ping/pong observation)
# ---------------------------------------------------------------------------

class TestUpbitHeartbeat(unittest.IsolatedAsyncioTestCase):

    async def test_ping_loop_observes_heartbeat_on_pong_success(self):
        """ping → pong 도착 → observe_heartbeat 호출 (§5 준수)."""
        client = UpbitWsClient()

        pong_future: asyncio.Future = asyncio.Future()
        pong_future.set_result(None)

        mock_ws = MagicMock()
        mock_ws.ping = AsyncMock(return_value=pong_future)
        mock_ws.close = AsyncMock()

        # PING_INTERVAL_SEC를 매우 짧게 patch (test 빠르게)
        with patch("app.crawlers.usdt_ws.upbit.PING_INTERVAL_SEC", 0.01):
            ping_task = asyncio.create_task(client._ping_loop(mock_ws))
            await asyncio.sleep(0.05)  # 몇 번 ping 발생 보장
            client._stop_event.set()
            await ping_task

        self.assertGreaterEqual(mock_ws.ping.call_count, 1)
        self.assertGreaterEqual(client._liveness.heartbeat_count, 1)
        self.assertIsNotNone(client._liveness.last_heartbeat_at)

    async def test_ping_loop_closes_ws_on_pong_timeout(self):
        """pong 못 받음 → ws.close() 호출 (reconnect 트리거)."""
        client = UpbitWsClient()

        # pong이 영원히 오지 않음 → wait_for timeout
        unresolved_pong: asyncio.Future = asyncio.Future()
        mock_ws = MagicMock()
        mock_ws.ping = AsyncMock(return_value=unresolved_pong)
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.upbit.PING_INTERVAL_SEC", 0.01), \
             patch("app.crawlers.usdt_ws.upbit.PING_TIMEOUT_SEC", 0.02):
            await client._ping_loop(mock_ws)

        mock_ws.close.assert_awaited_once()
        # heartbeat 관측 X (pong 실패)
        self.assertEqual(client._liveness.heartbeat_count, 0)

    async def test_ping_loop_closes_ws_on_connection_closed(self):
        """ping 자체가 ConnectionClosed raise → ws.close() 호출."""
        from websockets.exceptions import ConnectionClosedError

        client = UpbitWsClient()
        mock_ws = MagicMock()
        mock_ws.ping = AsyncMock(side_effect=ConnectionClosedError(None, None))
        mock_ws.close = AsyncMock()

        with patch("app.crawlers.usdt_ws.upbit.PING_INTERVAL_SEC", 0.01):
            await client._ping_loop(mock_ws)

        mock_ws.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# PR4 — UpbitRedisWriter (tick-level Redis latest writer)
# ---------------------------------------------------------------------------

def _make_tick(rate: float = 1486.0, timestamp_ms: int = 1777370239843) -> dict:
    return {
        "source": "upbit",
        "asset": "usdt-krw",
        "rate": rate,
        "timestamp_ms": timestamp_ms,
    }


class TestUpbitRedisWriter(unittest.IsolatedAsyncioTestCase):

    async def test_schedule_returns_immediately(self):
        """schedule()은 await 없이 즉시 반환 — recv loop blocking 방지."""
        writer = UpbitRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=True,
        ):
            start = time.time()
            writer.schedule(_make_tick())
            elapsed = time.time() - start

        try:
            self.assertLess(elapsed, 0.01, "schedule() should return synchronously")
            self.assertEqual(len(writer._tasks), 1)
        finally:
            await writer.close()

    async def test_write_calls_helper_with_correct_args(self):
        """source/asset/rate/timestamp_iso 정확 전달."""
        writer = UpbitRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=True,
        ) as mock_helper:
            writer.schedule(_make_tick(rate=1500.5, timestamp_ms=1777370239843))
            await writer.close()

        mock_helper.assert_called_once()
        kwargs = mock_helper.call_args.kwargs
        self.assertEqual(kwargs["source"], "upbit")
        self.assertEqual(kwargs["asset"], "usdt-krw")
        self.assertEqual(kwargs["rate"], 1500.5)
        # timestamp_ms = 1777370239843 → KST: 2026-04-29 03:37:19.843+09:00
        self.assertIn("2026-", kwargs["timestamp"])
        self.assertIn("+09:00", kwargs["timestamp"])

    async def test_timestamp_ms_to_kst_iso_conversion(self):
        """epoch ms → KST ISO 8601 정확성 (sub-second 포함)."""
        from datetime import datetime, timedelta, timezone

        writer = UpbitRedisWriter()
        # 1777370239843 ms = 1777370239.843 s = 2026-04-29 03:37:19.843 KST
        ts_ms = 1777370239843
        expected_dt = datetime.fromtimestamp(
            ts_ms / 1000, tz=timezone(timedelta(hours=9))
        )
        expected_iso = expected_dt.isoformat()

        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=True,
        ) as mock_helper:
            writer.schedule(_make_tick(timestamp_ms=ts_ms))
            await writer.close()

        self.assertEqual(mock_helper.call_args.kwargs["timestamp"], expected_iso)

    async def test_redis_failure_does_not_raise(self):
        """helper False 반환 → exception 없음, WS session 영향 X."""
        writer = UpbitRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=False,
        ):
            writer.schedule(_make_tick())
            # gather가 raise 안 함 (return_exceptions=True)
            await writer.close()

    async def test_redis_exception_does_not_raise(self):
        """helper exception → 격리 (log만), close 정상 완료."""
        writer = UpbitRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=RuntimeError("redis fault"),
        ):
            writer.schedule(_make_tick())
            await writer.close()  # should not raise

    async def test_close_drains_pending_tasks(self):
        """tasks가 완료될 때까지 close가 기다림 (Codex 권장 — 마지막 tick 보존)."""
        writer = UpbitRedisWriter()
        completed = []

        def slow_helper(**kwargs):
            time.sleep(0.05)  # 50ms
            completed.append(kwargs["rate"])
            return True

        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=slow_helper,
        ):
            writer.schedule(_make_tick(rate=1486.0))
            writer.schedule(_make_tick(rate=1487.0))
            await writer.close(timeout=1.0)

        # 둘 다 완료됨 (drain 성공)
        self.assertEqual(sorted(completed), [1486.0, 1487.0])
        self.assertEqual(len(writer._tasks), 0)

    async def test_close_cancels_after_timeout(self):
        """drain timeout 초과 시 cancel → log warning."""
        writer = UpbitRedisWriter()

        def hang_helper(**kwargs):
            # 0.5s만 행 — thread pool에서 background 잔존 영향 최소화.
            # timeout 0.05s이라 cancel은 일어남 (Python sync sleep은 interrupt
            # 안 되지만 asyncio.wait_for + return_exceptions로 즉시 종료).
            time.sleep(0.5)
            return True

        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=hang_helper,
        ):
            writer.schedule(_make_tick())
            start = time.time()
            await writer.close(timeout=0.05)
            elapsed = time.time() - start

        # timeout 50ms 후 cancel 경로 진입 → 즉시 반환 (sync sleep 0.5s는
        # background, asyncio는 더 기다리지 않음)
        self.assertLess(elapsed, 0.3, "close should timeout within ~50ms")
        self.assertEqual(len(writer._tasks), 0)

    async def test_close_noop_when_no_pending(self):
        """tasks 없을 때 close는 즉시 반환."""
        writer = UpbitRedisWriter()
        await writer.close()  # should not raise
        self.assertEqual(len(writer._tasks), 0)

    async def test_write_order_preserved_when_helper_is_slow(self):
        """Codex Finding 회귀 가드: schedule 순서 = Redis SET 순서.

        Race scenario:
            T1 (rate=1.0) schedule → task starts, helper 50ms 소요
            T2 (rate=2.0) schedule → task starts
            Without lock: T2 helper (fast) completes first → Redis = 2.0,
                then T1 (slow) completes → Redis = 1.0 (older value!)
            With lock: T1 → T2 직렬, completion_order = [1.0, 2.0]
        """
        writer = UpbitRedisWriter()
        completion_order = []
        call_count = [0]

        def helper(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # T1만 느리게
                time.sleep(0.05)
            completion_order.append(kwargs["rate"])
            return True

        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=helper,
        ):
            writer.schedule(_make_tick(rate=1.0))
            writer.schedule(_make_tick(rate=2.0))
            await writer.close(timeout=1.0)

        self.assertEqual(
            completion_order, [1.0, 2.0],
            "Redis SET order must match schedule order (lock serializes)",
        )

    async def test_max_pending_guard_skips_when_saturated(self):
        """MAX_PENDING_WRITES 초과 시 schedule 거부 + log warning."""
        writer = UpbitRedisWriter()
        # MAX_PENDING_WRITES 만큼 hang task 채움
        hang_event = asyncio.Event()

        async def hang_write():
            await hang_event.wait()

        # 직접 _tasks에 fake task 주입 (helper mock 없이 saturation 시뮬레이션)
        for _ in range(MAX_PENDING_WRITES):
            writer._tasks.add(asyncio.create_task(hang_write()))

        # 추가 schedule 시도 → skip
        with patch(
            "app.crawlers.usdt_ws.upbit.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=True,
        ) as mock_helper:
            writer.schedule(_make_tick())
            self.assertEqual(mock_helper.call_count, 0)

        # cleanup
        hang_event.set()
        await asyncio.gather(*list(writer._tasks), return_exceptions=True)


# ---------------------------------------------------------------------------
# PR4 — Session-level Redis writer integration
# ---------------------------------------------------------------------------

class TestUpbitSessionRedisIntegration(unittest.IsolatedAsyncioTestCase):

    def _make_mock_ws(self, recv_side_effect):
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)
        mock_ws.close = AsyncMock()
        mock_connect = AsyncMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect.__aexit__ = AsyncMock(return_value=None)
        return mock_ws, mock_connect

    async def test_session_schedules_writer_on_valid_tick(self):
        """valid tick 도착 → _redis_writer.schedule 호출."""
        client = UpbitWsClient()
        valid = json.dumps(_make_valid_ticker_dict())
        recv_calls = [0]

        async def recv_seq():
            recv_calls[0] += 1
            if recv_calls[0] == 1:
                return valid
            client._stop_event.set()
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_seq)
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(client._redis_writer, "schedule") as mock_schedule:
            await client._run_one_session()

        mock_schedule.assert_called_once()
        scheduled_tick = mock_schedule.call_args.args[0]
        self.assertEqual(scheduled_tick["source"], "upbit")
        self.assertEqual(scheduled_tick["asset"], "usdt-krw")

    async def test_session_does_not_schedule_on_invalid_tick(self):
        """invalid/non-ticker frame → schedule 호출 X."""
        client = UpbitWsClient()
        non_ticker = json.dumps({"type": "status", "code": "KRW-USDT"})
        recv_calls = [0]

        async def recv_seq():
            recv_calls[0] += 1
            if recv_calls[0] == 1:
                return non_ticker
            client._stop_event.set()
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_seq)
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(client._redis_writer, "schedule") as mock_schedule:
            await client._run_one_session()

        mock_schedule.assert_not_called()

    async def test_session_closes_writer_in_finally(self):
        """session 종료 시 _redis_writer.close 호출 (정상/예외 모두)."""
        client = UpbitWsClient()
        client._stop_event.set()

        async def recv_blocks():
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_blocks)
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(client._redis_writer, "close", new=AsyncMock()) as mock_close:
            await client._run_one_session()

        mock_close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
