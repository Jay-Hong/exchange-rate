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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, scheduler
from app.crawlers.usdt_ws import upbit as upbit_mod
from app.crawlers.usdt_ws.upbit import (
    DB_WRITE_WINDOW_SEC,
    MAX_PENDING_WRITES,
    PING_INTERVAL_SEC,
    PING_TIMEOUT_SEC,
    RECONNECT_BACKOFF_SEQ,
    RECONNECT_BACKOFF_TAIL,
    STALE_AFTER_SEC,
    UPBIT_SUBSCRIBE_TICKET,
    UPBIT_TARGET_CODE,
    UPBIT_WS_URL,
    UpbitDbWriter,
    UpbitRedisWriter,
    UpbitWsClient,
    UsdtLivenessMonitor,
)
from app.notifications.alert_evaluator import (
    ALERT_CACHE_TTL_SEC,
    ALERT_CLOSE_TIMEOUT_SEC,
    PENDING_TASKS_WARNING_THRESHOLD,
    AlertObservation,
    AlertSettingsCache,
    CachedAlertSetting,
    CachedBucket,
    FreshSettingSnapshot,
    UsdtAlertEvaluator,
    condition_matches,
    delivery_allowed,
    get_default_alert_settings_cache,
    invalidate_alert_settings_cache,
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
        # PR5/PR6: valid tick은 db_writer + alert_evaluator 트리거 → DB 연결 회피.
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(UpbitDbWriter, "_sync_db_write"), \
             patch.object(UsdtAlertEvaluator, "_load_settings_from_db", return_value=tuple()):
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

        # PR5/PR6: valid tick은 db_writer.schedule + alert_evaluator.schedule
        # 트리거 → real _sync_db_write / _load_settings_from_db가 DB 연결
        # 시도하지 않도록 patch.
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(UpbitDbWriter, "_sync_db_write"), \
             patch.object(UsdtAlertEvaluator, "_load_settings_from_db", return_value=tuple()):
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
    """PR4: Redis writer 허용 / PR5: DB writer (insert_if_changed) 허용 /
    PR6: alert_evaluator (AlertObservation, UsdtAlertEvaluator) 허용.

    REST fallback (PR7)만 여전히 금지.

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
        # Redis writer (PR4) — latest_rates_cache 허용
        # DB writer (PR5) — crud + database 허용
        # Alert evaluator (PR6) — alert_evaluator import 허용
        #   단 SourceRate 모델 직접 import는 금지 (helper 내부 사용만)
        self.assertNotIn("SourceRate", imports)
        # alert (PR6) — fcm/process_source_rate_alerts는 evaluator 내부에서 호출
        # upbit.py에서는 alert_evaluator import만 (위 허용)
        self.assertNotIn("process_source_rate_alerts", imports)
        self.assertNotIn("from app.notifications.fcm", imports)
        # REST fallback (PR7)
        self.assertNotIn("aiohttp", imports)
        self.assertNotIn("requests", imports)
        self.assertNotIn("httpx", imports)

    def test_upbit_module_has_no_downstream_invocations(self):
        """call/instantiation 패턴 — docstring 언급은 무시, 실제 호출만 검출."""
        source = inspect.getsource(upbit_mod)
        forbidden_calls = [
            # DB writer (PR5) — SourceRate model 직접 생성 X (helper 내부만)
            "SourceRate(",
            # alert (PR6) — evaluator 내부에서 호출, upbit.py에서는 X
            "process_source_rate_alerts(",
            "send_fcm(",
            "send_fcm_multicast_sync(",
            "mark_source_setting_triggered(",
            # REST fallback (PR7) — helper 호출 (HTTP client invocation)
            "aiohttp.ClientSession(",
            "requests.get(",
            "requests.post(",
            "httpx.get(",
            "httpx.post(",
        ]
        for pattern in forbidden_calls:
            self.assertNotIn(pattern, source, f"Forbidden PR6-PR7 invocation: {pattern}")

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
        # PR5/PR6: db_writer + alert_evaluator도 함께 mock — 이 test는 redis_writer만 검증.
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(client._redis_writer, "schedule") as mock_schedule, \
             patch.object(client._db_writer, "schedule"), \
             patch.object(client._alert_evaluator, "schedule"):
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


# ---------------------------------------------------------------------------
# PR5 — UpbitDbWriter (1초 window debounce + insert_source_rate_if_changed)
# ---------------------------------------------------------------------------

class TestUpbitDbWriter(unittest.IsolatedAsyncioTestCase):

    async def test_schedule_returns_immediately(self):
        """schedule()은 sync, 즉시 반환 (recv loop blocking 방지)."""
        writer = UpbitDbWriter(window_sec=0.01)
        # patch 안에서 schedule + close 모두 처리 — close 시 timer 발화가
        # 실제 _sync_db_write로 fallback해서 DB 연결 시도하는 것 방지.
        with patch.object(writer, "_sync_db_write"):
            start = time.time()
            writer.schedule(_make_tick())
            elapsed = time.time() - start
            self.assertLess(elapsed, 0.01)
            self.assertIsNotNone(writer._timer)
            await writer.close()

    async def test_window_flushes_last_tick_only(self):
        """window 안 여러 tick → 마지막 tick만 helper 호출."""
        writer = UpbitDbWriter(window_sec=0.05)
        call_args = []

        def spy(tick):
            call_args.append(tick["rate"])

        with patch.object(writer, "_sync_db_write", side_effect=spy):
            writer.schedule(_make_tick(rate=1.0))
            writer.schedule(_make_tick(rate=2.0))
            writer.schedule(_make_tick(rate=3.0))
            # window 만료 대기
            await asyncio.sleep(0.1)
            await writer.close()

        # 마지막 tick (3.0)만 helper 호출
        self.assertEqual(call_args, [3.0])

    async def test_flush_during_write_schedules_new_timer(self):
        """Codex 필수 test: flush 진행 중 새 tick 도착 → 새 timer 예약 → 마지막 tick 유실 X.

        시나리오:
            t=0: schedule(rate=1.0)
            t=0.05: window 만료, _sync_db_write(rate=1.0) 시작
            t=0.05~0.10: write 진행 중 (50ms 행)
            t=0.07: schedule(rate=2.0) → _pending_tick 갱신, 새 timer 안 만듦
                    (현 _timer.done() == False)
            t=0.10: 첫 write 종료, finally에서 _pending_tick 확인 → 새 timer 예약
            t=0.15: 새 window 만료, _sync_db_write(rate=2.0) 호출
        """
        writer = UpbitDbWriter(window_sec=0.05)
        call_args = []

        def slow_write(tick):
            call_args.append(tick["rate"])
            time.sleep(0.05)  # write 50ms 행

        with patch.object(writer, "_sync_db_write", side_effect=slow_write):
            writer.schedule(_make_tick(rate=1.0))
            # 첫 window 만료 + write 시작 대기
            await asyncio.sleep(0.07)
            # 첫 write 진행 중 → 새 tick 도착
            writer.schedule(_make_tick(rate=2.0))
            # 첫 write 종료 + 두 번째 window flush 대기
            await asyncio.sleep(0.15)
            await writer.close()

        # 1.0 (첫 window) + 2.0 (race handling 신규 timer) 둘 다 write
        self.assertEqual(call_args, [1.0, 2.0])

    async def test_db_exception_does_not_propagate(self):
        """DB exception → log only, WS session 영향 X."""
        writer = UpbitDbWriter(window_sec=0.01)
        with patch.object(
            writer, "_sync_db_write", side_effect=RuntimeError("DB fault")
        ):
            writer.schedule(_make_tick())
            await asyncio.sleep(0.05)
            await writer.close()  # should not raise

    async def test_close_flushes_pending_tick_immediately(self):
        """close() 시 1초 window 기다리지 않고 마지막 pending tick 즉시 flush."""
        writer = UpbitDbWriter(window_sec=10.0)  # 긴 window
        call_args = []

        def spy(tick):
            call_args.append(tick["rate"])

        with patch.object(writer, "_sync_db_write", side_effect=spy):
            writer.schedule(_make_tick(rate=99.0))
            # window 만료 전 close
            start = time.time()
            await writer.close()
            elapsed = time.time() - start

        # 10s window이지만 close가 즉시 flush — 1초 미만
        self.assertLess(elapsed, 1.0)
        self.assertEqual(call_args, [99.0])

    async def test_close_noop_when_no_pending(self):
        """pending tick 없을 때 close()는 안전하게 종료."""
        writer = UpbitDbWriter()
        await writer.close()  # should not raise

    async def test_sync_db_write_calls_helper_with_correct_args(self):
        """_sync_db_write가 crud.insert_source_rate_if_changed에 정확한 args 전달."""
        # session context는 mock — 실제 DB 의존성 차단
        mock_session = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.database.get_db_context", return_value=mock_ctx), \
             patch("app.crud.insert_source_rate_if_changed") as mock_insert:
            UpbitDbWriter._sync_db_write(_make_tick(rate=1486.0))

        mock_insert.assert_called_once_with(
            db=mock_session,
            source="upbit",
            asset="usdt-krw",
            rate=1486.0,
        )


class TestUpbitSessionDbIntegration(unittest.IsolatedAsyncioTestCase):

    def _make_mock_ws(self, recv_side_effect):
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)
        mock_ws.close = AsyncMock()
        mock_connect = AsyncMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect.__aexit__ = AsyncMock(return_value=None)
        return mock_ws, mock_connect

    async def test_session_schedules_db_writer_on_valid_tick(self):
        """valid tick → _db_writer.schedule 호출."""
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
        # PR6: alert_evaluator도 함께 mock — 이 test는 db_writer만 검증.
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(client._db_writer, "schedule") as mock_schedule, \
             patch.object(client._alert_evaluator, "schedule"):
            await client._run_one_session()

        mock_schedule.assert_called_once()
        scheduled_tick = mock_schedule.call_args.args[0]
        self.assertEqual(scheduled_tick["source"], "upbit")
        self.assertEqual(scheduled_tick["asset"], "usdt-krw")

    async def test_session_does_not_schedule_db_on_invalid_tick(self):
        """invalid/non-ticker frame → _db_writer.schedule X."""
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
             patch.object(client._db_writer, "schedule") as mock_schedule:
            await client._run_one_session()

        mock_schedule.assert_not_called()

    async def test_session_closes_db_writer_in_finally(self):
        """session 종료 시 _db_writer.close 호출."""
        client = UpbitWsClient()
        client._stop_event.set()

        async def recv_blocks():
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_blocks)
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(client._db_writer, "close", new=AsyncMock()) as mock_close:
            await client._run_one_session()

        mock_close.assert_awaited_once()

    async def test_session_closes_db_alert_redis_in_order(self):
        """Codex review 회귀 가드: finally 블록에서 close 순서 = DB → Alert → Redis.

        근거:
        1. DB는 source of truth — alert (DB triggered state) 직후 → Redis cache 마지막.
        2. await to_thread가 event loop 양보 → 대기 background tasks 동시 진행 →
           후속 drain reach 확률 ↑.
        """
        client = UpbitWsClient()
        client._stop_event.set()
        call_order = []

        async def db_close_spy():
            call_order.append("db")

        async def alert_close_spy(timeout=ALERT_CLOSE_TIMEOUT_SEC):
            call_order.append("alert")

        async def redis_close_spy(timeout=1.0):
            call_order.append("redis")

        async def recv_blocks():
            raise asyncio.TimeoutError

        mock_ws, mock_connect = self._make_mock_ws(recv_side_effect=recv_blocks)
        with patch("app.crawlers.usdt_ws.upbit.websockets.connect", return_value=mock_connect), \
             patch.object(client._db_writer, "close", side_effect=db_close_spy), \
             patch.object(client._alert_evaluator, "close", side_effect=alert_close_spy), \
             patch.object(client._redis_writer, "close", side_effect=redis_close_spy):
            await client._run_one_session()

        self.assertEqual(
            call_order, ["db", "alert", "redis"],
            "Close order: DB (source of truth) → Alert (DB triggered state) → Redis (cache)",
        )


# ---------------------------------------------------------------------------
# PR6 — alert_evaluator pure helpers + cache
# ---------------------------------------------------------------------------

def _make_cached_setting(
    setting_id: int = 1,
    user_id: str = "user-A",
    source: str = "upbit",
    asset: str = "usdt-krw",
    condition: str = "above",
    threshold: float = 1500.0,
    device_tokens: tuple[str, ...] = ("token-1",),
) -> CachedAlertSetting:
    return CachedAlertSetting(
        setting_id=setting_id,
        user_id=user_id,
        source=source,
        asset=asset,
        condition=condition,
        threshold=threshold,
        device_tokens=device_tokens,
    )


def _make_observation(
    source: str = "upbit",
    asset: str = "usdt-krw",
    rate: float = 1500.0,
    timestamp_ms: int = 1777370239843,
) -> AlertObservation:
    return AlertObservation(
        source=source, asset=asset, rate=rate,
        timestamp_ms=timestamp_ms, kind="tick",
    )


def _make_snapshot(
    setting_id: int = 1,
    enabled: bool = True,
    triggered: bool = False,
    source: str = "upbit",
    asset: str = "usdt-krw",
    condition: str = "above",
    threshold: float = 1500.0,
) -> FreshSettingSnapshot:
    return FreshSettingSnapshot(
        setting_id=setting_id, enabled=enabled, triggered=triggered,
        source=source, asset=asset, condition=condition, threshold=threshold,
    )


class TestConditionMatches(unittest.TestCase):

    def test_above_strictly_below_threshold(self):
        s = _make_cached_setting(condition="above", threshold=1500.0)
        o = _make_observation(rate=1499.99)
        self.assertFalse(condition_matches(s, o))

    def test_above_exactly_at_threshold(self):
        s = _make_cached_setting(condition="above", threshold=1500.0)
        o = _make_observation(rate=1500.0)
        self.assertTrue(condition_matches(s, o))  # >= 경계

    def test_above_strictly_above_threshold(self):
        s = _make_cached_setting(condition="above", threshold=1500.0)
        o = _make_observation(rate=1500.01)
        self.assertTrue(condition_matches(s, o))

    def test_below_strictly_above_threshold(self):
        s = _make_cached_setting(condition="below", threshold=1500.0)
        o = _make_observation(rate=1500.01)
        self.assertFalse(condition_matches(s, o))

    def test_below_exactly_at_threshold(self):
        s = _make_cached_setting(condition="below", threshold=1500.0)
        o = _make_observation(rate=1500.0)
        self.assertTrue(condition_matches(s, o))  # <= 경계

    def test_below_strictly_below_threshold(self):
        s = _make_cached_setting(condition="below", threshold=1500.0)
        o = _make_observation(rate=1499.99)
        self.assertTrue(condition_matches(s, o))


class TestDeliveryAllowed(unittest.TestCase):
    """PR6 once 정책: enabled and not triggered."""

    def test_enabled_not_triggered_allows(self):
        s = _make_snapshot(enabled=True, triggered=False)
        self.assertTrue(delivery_allowed(s, datetime.now(timezone.utc)))

    def test_disabled_blocks(self):
        s = _make_snapshot(enabled=False, triggered=False)
        self.assertFalse(delivery_allowed(s, datetime.now(timezone.utc)))

    def test_triggered_blocks(self):
        s = _make_snapshot(enabled=True, triggered=True)
        self.assertFalse(delivery_allowed(s, datetime.now(timezone.utc)))

    def test_disabled_and_triggered_blocks(self):
        s = _make_snapshot(enabled=False, triggered=True)
        self.assertFalse(delivery_allowed(s, datetime.now(timezone.utc)))


class TestAlertSettingsCache(unittest.TestCase):

    def test_get_if_fresh_returns_none_on_miss(self):
        cache = AlertSettingsCache()
        self.assertIsNone(cache.get_if_fresh("upbit", "usdt-krw", time.time()))

    def test_put_then_get_within_ttl(self):
        cache = AlertSettingsCache()
        now = time.time()
        s = _make_cached_setting()
        cache.put("upbit", "usdt-krw", (s,), now)
        bucket = cache.get_if_fresh("upbit", "usdt-krw", now + 1.0)
        self.assertIsNotNone(bucket)
        self.assertEqual(bucket.settings, (s,))

    def test_get_returns_none_after_ttl(self):
        cache = AlertSettingsCache()
        now = time.time()
        cache.put("upbit", "usdt-krw", tuple(), now)
        # ALERT_CACHE_TTL_SEC + 1 후 expired
        self.assertIsNone(
            cache.get_if_fresh("upbit", "usdt-krw", now + ALERT_CACHE_TTL_SEC + 1.0)
        )

    def test_source_asset_keys_independent(self):
        cache = AlertSettingsCache()
        s1 = _make_cached_setting(setting_id=1, source="upbit")
        s2 = _make_cached_setting(setting_id=2, source="bithumb")
        now = time.time()
        cache.put("upbit", "usdt-krw", (s1,), now)
        cache.put("bithumb", "usdt-krw", (s2,), now)
        self.assertEqual(cache.get_if_fresh("upbit", "usdt-krw", now).settings, (s1,))
        self.assertEqual(cache.get_if_fresh("bithumb", "usdt-krw", now).settings, (s2,))

    def test_cache_stores_frozen_dataclass_not_orm(self):
        """ORM-free guard: cache value는 CachedAlertSetting frozen dataclass만."""
        cache = AlertSettingsCache()
        s = _make_cached_setting()
        cache.put("upbit", "usdt-krw", (s,), time.time())
        bucket = cache.get_if_fresh("upbit", "usdt-krw", time.time())
        for item in bucket.settings:
            self.assertIsInstance(item, CachedAlertSetting)
            # frozen: 수정 시도 → FrozenInstanceError
            with self.assertRaises(Exception):
                item.threshold = 999.0

    def test_invalidate_specific_key(self):
        cache = AlertSettingsCache()
        cache.put("upbit", "usdt-krw", tuple(), time.time())
        cache.put("bithumb", "usdt-krw", tuple(), time.time())
        cache.invalidate("upbit", "usdt-krw")
        self.assertIsNone(cache.get_if_fresh("upbit", "usdt-krw", time.time()))
        self.assertIsNotNone(cache.get_if_fresh("bithumb", "usdt-krw", time.time()))

    def test_invalidate_all(self):
        cache = AlertSettingsCache()
        cache.put("upbit", "usdt-krw", tuple(), time.time())
        cache.put("bithumb", "usdt-krw", tuple(), time.time())
        cache.invalidate()
        self.assertIsNone(cache.get_if_fresh("upbit", "usdt-krw", time.time()))
        self.assertIsNone(cache.get_if_fresh("bithumb", "usdt-krw", time.time()))


# ---------------------------------------------------------------------------
# PR6 — UsdtAlertEvaluator
# ---------------------------------------------------------------------------

class TestUsdtAlertEvaluatorBasics(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # singleton cache leak 방지 — 각 test가 fresh cache로 시작
        get_default_alert_settings_cache().invalidate()

    def tearDown(self):
        get_default_alert_settings_cache().invalidate()

    async def test_schedule_returns_immediately(self):
        evaluator = UsdtAlertEvaluator()
        with patch.object(
            UsdtAlertEvaluator, "_load_settings_from_db",
            return_value=tuple(),
        ):
            start = time.time()
            evaluator.schedule(_make_observation())
            elapsed = time.time() - start
            self.assertLess(elapsed, 0.01)
            self.assertEqual(len(evaluator._tasks), 1)
            await evaluator.close()

    async def test_no_drop_when_pending_above_threshold(self):
        """observation drop 금지 (Codex Finding 1). 100+ pending이어도 모두 task 생성."""
        evaluator = UsdtAlertEvaluator()

        # _evaluate_async를 hang 시킴 — pending tasks 누적
        hang_event = asyncio.Event()
        async def hang_eval(observation):
            await hang_event.wait()

        with patch.object(evaluator, "_evaluate_async", side_effect=hang_eval):
            # 105개 observation schedule (warning threshold 100 초과)
            for _ in range(105):
                evaluator.schedule(_make_observation())

            self.assertEqual(len(evaluator._tasks), 105)  # 모두 생성

            # cleanup
            hang_event.set()
            await evaluator.close()

    async def test_cache_hit_avoids_db_load(self):
        """cache hit → _load_settings_from_db 호출 X (event loop dict lookup만)."""
        evaluator = UsdtAlertEvaluator()
        # 미리 cache populate
        evaluator._cache.put("upbit", "usdt-krw", tuple(), time.time())

        with patch.object(
            UsdtAlertEvaluator, "_load_settings_from_db",
            return_value=tuple(),
        ) as mock_load:
            evaluator.schedule(_make_observation())
            await asyncio.sleep(0.05)  # task 실행 대기
            await evaluator.close()
        mock_load.assert_not_called()

    async def test_cache_miss_calls_db_load(self):
        """cache miss → _load_settings_from_db 1번 호출 (via to_thread)."""
        evaluator = UsdtAlertEvaluator()

        with patch.object(
            UsdtAlertEvaluator, "_load_settings_from_db",
            return_value=tuple(),
        ) as mock_load:
            evaluator.schedule(_make_observation())
            await asyncio.sleep(0.05)
            await evaluator.close()
        mock_load.assert_called_once_with("upbit", "usdt-krw")

    async def test_per_key_loading_guard_dedupes_concurrent_misses(self):
        """TTL 만료 순간 동시 cache miss → DB loader 1번만 호출 (thundering herd 방지)."""
        evaluator = UsdtAlertEvaluator()
        call_count = [0]

        def slow_load(source, asset):
            call_count[0] += 1
            time.sleep(0.05)  # 50ms slow load
            return tuple()

        with patch.object(
            UsdtAlertEvaluator, "_load_settings_from_db",
            side_effect=slow_load,
        ):
            # 5개 동시 schedule (모두 cache miss, 같은 key)
            for _ in range(5):
                evaluator.schedule(_make_observation())
            await asyncio.sleep(0.15)  # 모두 완료 대기
            await evaluator.close()

        # 동시 5개 schedule → loader 1번만 (per-key loading guard)
        self.assertEqual(call_count[0], 1)


class TestUsdtAlertEvaluatorSendOne(unittest.IsolatedAsyncioTestCase):
    """_send_one flow — refetch / FCM / mark / log session 분리."""

    async def test_refetch_stale_skips_fcm(self):
        """refetch에서 disabled or triggered → FCM 발송 X + skip log."""
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting()
        stale_snapshot = _make_snapshot(enabled=False, triggered=False)  # disabled

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=stale_snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
            return_value={"success_count": 1, "failure_count": 0, "failed_tokens": []},
        ) as mock_fcm, patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ) as mock_persist:
            await evaluator._send_one(candidate, _make_observation())

        mock_fcm.assert_not_called()
        mock_persist.assert_not_called()

    async def test_fcm_success_calls_persist(self):
        """FCM 성공 → mark_triggered + log success (persist 호출)."""
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting()
        # snapshot은 cached와 동일한 condition/threshold (no change)
        ok_snapshot = _make_snapshot(enabled=True, triggered=False)
        fcm_result = {"success_count": 1, "failure_count": 0, "failed_tokens": []}

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=ok_snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
            return_value=fcm_result,
        ), patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ) as mock_persist:
            await evaluator._send_one(candidate, _make_observation())

        # fresh_candidate (cached와 fields 동일이면 == 성립)
        mock_persist.assert_called_once_with(candidate, 1500.0, fcm_result)

    async def test_fcm_failure_still_calls_persist_for_log(self):
        """FCM 실패 → log failure (persist 호출). setting은 변경 X (persist 내부 처리)."""
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting()
        ok_snapshot = _make_snapshot(enabled=True, triggered=False)
        fcm_result = {"success_count": 0, "failure_count": 1, "failed_tokens": ["token-bad"]}

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=ok_snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
            return_value=fcm_result,
        ), patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ) as mock_persist:
            await evaluator._send_one(candidate, _make_observation())

        mock_persist.assert_called_once_with(candidate, 1500.0, fcm_result)

    async def test_refetch_threshold_change_blocks_fcm(self):
        """Codex Finding (Medium) 회귀 가드: cached threshold match이어도 refetch
        threshold가 변경됐고 더 이상 매치 안 하면 발송 차단.

        시나리오:
          - cached: above 1500, observation rate=1510 → cached match
          - refetch: above 1600 (사용자가 PUT으로 변경)
          - rate 1510 < refetch 1600 → no match → skip
        """
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting(condition="above", threshold=1500.0)
        # refetch가 threshold=1600 반환 (사용자가 변경)
        snapshot = _make_snapshot(
            enabled=True, triggered=False,
            condition="above", threshold=1600.0,
        )
        observation = _make_observation(rate=1510.0)

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
        ) as mock_fcm, patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ) as mock_persist:
            await evaluator._send_one(candidate, observation)

        mock_fcm.assert_not_called()
        mock_persist.assert_not_called()

    async def test_refetch_condition_change_blocks_fcm(self):
        """Codex Finding (Medium) 회귀 가드: cached condition (above) match이어도
        refetch condition이 below로 바뀌면 발송 차단.

        시나리오:
          - cached: above 1500, rate=1510 → cached match (1510 >= 1500)
          - refetch: below 1500 (사용자 변경)
          - below 평가: 1510 > 1500 → no match → skip
        """
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting(condition="above", threshold=1500.0)
        snapshot = _make_snapshot(
            enabled=True, triggered=False,
            condition="below", threshold=1500.0,
        )
        observation = _make_observation(rate=1510.0)

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
        ) as mock_fcm, patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ) as mock_persist:
            await evaluator._send_one(candidate, observation)

        mock_fcm.assert_not_called()
        mock_persist.assert_not_called()

    async def test_refetch_source_change_blocks_fcm(self):
        """Codex Finding (Medium) 회귀 가드: 사용자가 source 변경 시 발송 차단."""
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting(source="upbit", asset="usdt-krw")
        snapshot = _make_snapshot(
            enabled=True, triggered=False,
            source="bithumb", asset="usdt-krw",  # source 변경
        )
        observation = _make_observation(source="upbit", asset="usdt-krw")

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
        ) as mock_fcm, patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ) as mock_persist:
            await evaluator._send_one(candidate, observation)

        mock_fcm.assert_not_called()
        mock_persist.assert_not_called()

    async def test_fcm_uses_fresh_threshold_in_payload(self):
        """Codex Finding (Medium): cached와 refetch가 threshold만 다르고 둘 다
        match이면 fresh threshold가 payload에 사용됨.

        시나리오:
          - cached: above 1500
          - refetch: above 1400 (사용자가 threshold 낮춤)
          - rate=1510 → cached match + refetch match
          - FCM payload는 fresh threshold(1400) 기준이어야 함
        """
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting(condition="above", threshold=1500.0)
        snapshot = _make_snapshot(
            enabled=True, triggered=False,
            condition="above", threshold=1400.0,
        )
        observation = _make_observation(rate=1510.0)
        fcm_result = {"success_count": 1, "failure_count": 0, "failed_tokens": []}

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
            return_value=fcm_result,
        ), patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ) as mock_persist:
            await evaluator._send_one(candidate, observation)

        # persist는 fresh_candidate 기준 (threshold=1400)
        mock_persist.assert_called_once()
        called_candidate = mock_persist.call_args.args[0]
        self.assertEqual(called_candidate.threshold, 1400.0)
        self.assertEqual(called_candidate.condition, "above")


class TestUsdtAlertEvaluatorInFlightGuard(unittest.IsolatedAsyncioTestCase):

    async def test_in_flight_guard_prevents_duplicate_fcm(self):
        """동일 setting_id 동시 observation 2번 → FCM 1번 (in-process atomic claim)."""
        evaluator = UsdtAlertEvaluator()
        candidate = _make_cached_setting(setting_id=42)

        # cache populate으로 DB load 회피
        evaluator._cache.put("upbit", "usdt-krw", (candidate,), time.time())

        fcm_count = [0]
        send_event = asyncio.Event()

        def slow_fcm(tokens, title, body, data):
            fcm_count[0] += 1
            time.sleep(0.05)  # 50ms slow FCM
            return {"success_count": 1, "failure_count": 0, "failed_tokens": []}

        ok_snapshot = _make_snapshot(setting_id=42, enabled=True, triggered=False)

        with patch.object(
            UsdtAlertEvaluator, "_refetch_setting_snapshot",
            return_value=ok_snapshot,
        ), patch.object(
            UsdtAlertEvaluator, "_send_fcm_multicast",
            side_effect=slow_fcm,
        ), patch.object(
            UsdtAlertEvaluator, "_persist_result",
        ):
            # 5개 동시 schedule (같은 setting_id 매칭 예상)
            for rate in [1501.0, 1502.0, 1503.0, 1504.0, 1505.0]:
                evaluator.schedule(_make_observation(rate=rate))
            await asyncio.sleep(0.2)  # 모든 task 완료 대기
            await evaluator.close()

        # 5번 schedule이지만 in-flight guard로 FCM 1번만
        self.assertEqual(fcm_count[0], 1)


class TestUsdtAlertEvaluatorClose(unittest.IsolatedAsyncioTestCase):

    async def test_close_drains_pending_tasks(self):
        """close drain-first: pending alert tasks 완료 후 반환."""
        evaluator = UsdtAlertEvaluator()
        completed = []

        async def slow_eval(observation):
            await asyncio.sleep(0.05)
            completed.append(observation.rate)

        with patch.object(evaluator, "_evaluate_async", side_effect=slow_eval):
            evaluator.schedule(_make_observation(rate=1.0))
            evaluator.schedule(_make_observation(rate=2.0))
            await evaluator.close(timeout=1.0)

        # 둘 다 완료 후 close 반환
        self.assertEqual(sorted(completed), [1.0, 2.0])

    async def test_close_cancels_after_timeout(self):
        """close timeout 초과 → cancel."""
        evaluator = UsdtAlertEvaluator()

        async def hang_eval(observation):
            await asyncio.sleep(10)

        with patch.object(evaluator, "_evaluate_async", side_effect=hang_eval):
            evaluator.schedule(_make_observation())
            start = time.time()
            await evaluator.close(timeout=0.05)
            elapsed = time.time() - start

        self.assertLess(elapsed, 0.3)
        self.assertEqual(len(evaluator._tasks), 0)

    async def test_close_noop_when_no_pending(self):
        evaluator = UsdtAlertEvaluator()
        await evaluator.close()


# ---------------------------------------------------------------------------
# PR6 — load_settings_from_db excludes empty device tokens
# ---------------------------------------------------------------------------

class TestLoadSettingsFromDbExcludesEmptyDevices(unittest.TestCase):

    def test_empty_device_tokens_excluded(self):
        """cache populate 시점에 device_tokens 없는 settings 제외."""
        from app.notifications.alert_evaluator import UsdtAlertEvaluator as Evaluator
        from unittest.mock import MagicMock

        # mock SQLAlchemy session + query result
        mock_setting_with_devices = MagicMock(
            id=1, user_id="user-A", source="upbit", asset="usdt-krw",
            condition="above", threshold=1500.0, enabled=True, triggered=False,
        )
        mock_setting_without_devices = MagicMock(
            id=2, user_id="user-B", source="upbit", asset="usdt-krw",
            condition="below", threshold=1400.0, enabled=True, triggered=False,
        )

        mock_device = MagicMock(user_id="user-A", device_token="token-A")
        # user-B는 device 없음

        mock_session = MagicMock()
        # 1st query: settings
        # 2nd query: devices
        query_results = [
            [mock_setting_with_devices, mock_setting_without_devices],
            [mock_device],
        ]
        query_idx = [0]
        def make_query(model):
            q = MagicMock()
            q.filter.return_value = q
            q.all.return_value = query_results[query_idx[0]]
            query_idx[0] += 1
            return q
        mock_session.query.side_effect = make_query

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.__exit__ = MagicMock(return_value=None)

        with patch("app.database.get_db_context", return_value=mock_ctx):
            result = Evaluator._load_settings_from_db("upbit", "usdt-krw")

        # user-A만 포함 (device_tokens 있음), user-B 제외
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].setting_id, 1)
        self.assertEqual(result[0].device_tokens, ("token-A",))


# ---------------------------------------------------------------------------
# PR6 follow-up — Settings CRUD cache invalidation (singleton + helper)
# ---------------------------------------------------------------------------

class TestAlertSettingsCacheSingleton(unittest.TestCase):

    def setUp(self):
        # 각 test 시작 시 singleton clear
        get_default_alert_settings_cache().invalidate()

    def tearDown(self):
        get_default_alert_settings_cache().invalidate()

    def test_default_cache_is_singleton(self):
        """get_default_alert_settings_cache는 같은 instance 반환."""
        c1 = get_default_alert_settings_cache()
        c2 = get_default_alert_settings_cache()
        self.assertIs(c1, c2)

    def test_default_evaluator_uses_singleton_cache(self):
        """UsdtAlertEvaluator() 기본 cache = singleton."""
        evaluator = UsdtAlertEvaluator()
        self.assertIs(evaluator._cache, get_default_alert_settings_cache())

    def test_custom_cache_injection_overrides_singleton(self):
        """test 시 cache inject로 격리 가능."""
        custom_cache = AlertSettingsCache()
        evaluator = UsdtAlertEvaluator(cache=custom_cache)
        self.assertIs(evaluator._cache, custom_cache)
        self.assertIsNot(evaluator._cache, get_default_alert_settings_cache())

    def test_invalidate_helper_clears_specific_key(self):
        """invalidate_alert_settings_cache(source, asset)는 해당 key만 제거."""
        cache = get_default_alert_settings_cache()
        cache.put("upbit", "usdt-krw", tuple(), time.time())
        cache.put("bithumb", "usdt-krw", tuple(), time.time())

        invalidate_alert_settings_cache("upbit", "usdt-krw")

        self.assertIsNone(cache.get_if_fresh("upbit", "usdt-krw", time.time()))
        self.assertIsNotNone(cache.get_if_fresh("bithumb", "usdt-krw", time.time()))

    def test_invalidate_via_helper_visible_to_evaluator(self):
        """API endpoint (helper) → evaluator (default cache) 즉시 반영 검증."""
        evaluator = UsdtAlertEvaluator()  # default singleton 사용
        s = _make_cached_setting()
        evaluator._cache.put("upbit", "usdt-krw", (s,), time.time())

        # cache hit 확인
        self.assertIsNotNone(
            evaluator._cache.get_if_fresh("upbit", "usdt-krw", time.time())
        )

        # API endpoint이 helper 호출
        invalidate_alert_settings_cache("upbit", "usdt-krw")

        # evaluator의 cache에서도 즉시 제거 (singleton 공유)
        self.assertIsNone(
            evaluator._cache.get_if_fresh("upbit", "usdt-krw", time.time())
        )


if __name__ == "__main__":
    unittest.main()
