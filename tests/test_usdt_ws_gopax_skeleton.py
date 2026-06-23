"""Phase B.6 Stage G1 — Gopax USDT/KRW WebSocket skeleton tests.

USDT_WS_DESIGN_PLAN §12.9 (2026-05-21).

G1 scope: flag + scheduler + lifecycle skeleton만. Subscribe/Parse/Primus
heartbeat/Liveness/Writers/Telemetry는 G2~ 별도 stage이므로 본 파일에서 검증 X.

G1 acceptance (테스트 핵심):
    - flag=false → GopaxWsClient 생성 X + task 생성 X + network connect X
    - flag=true → client/task 생성 + 중복 start 방지
    - shutdown → stop_event set + task 종료 + globals 초기화
    - GopaxWsClient minimal state (_stop_event, _running만 보유 — Codex 최종 권고)
    - start-stop lifecycle placeholder (no network)
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from websockets.exceptions import ConnectionClosed

from app import config, scheduler
from app.latest_rates_cache import UsdtLatestWriteOutcome
from app.crawlers.usdt_ws.gopax import (
    GOPAX_WS_URL,
    MAX_PENDING_WRITES,
    REDIS_CLOSE_TIMEOUT_SEC,
    GopaxRedisWriter,
    GopaxRestFallbackController,
    GopaxWsClient,
)
from app.tether_topic_trigger import (
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
)


def _reset_scheduler_usdt_ws_gopax_globals():
    """Test isolation — module globals 초기화."""
    scheduler.usdt_ws_gopax_client = None
    scheduler.usdt_ws_gopax_task = None


def _make_fake_run_runner():
    """`_run_usdt_ws_gopax_client` mock — client.stop()까지 stop_event 대기."""
    async def fake_run(client):
        await client._stop_event.wait()
    return fake_run


# ---------------------------------------------------------------------------
# G1 Scenario 1: flag=false → GopaxWsClient 생성 안 함 (G1 핵심 acceptance)
# ---------------------------------------------------------------------------


class TestFlagFalseInvariant(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    async def test_no_gopax_client_constructed_when_flag_false(self):
        """flag=false → start 함수 즉시 return + GopaxWsClient 생성 X + task 생성 X."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", False), \
             patch("app.crawlers.usdt_ws.gopax.GopaxWsClient") as mock_client_cls, \
             self.assertLogs("exchange_rate.scheduler", level="INFO") as cm:
            await scheduler.start_usdt_ws_gopax_client()
        mock_client_cls.assert_not_called()
        self.assertIsNone(scheduler.usdt_ws_gopax_client)
        self.assertIsNone(scheduler.usdt_ws_gopax_task)
        self.assertTrue(any("USDT_WS_GOPAX_ENABLED=false" in m for m in cm.output))


# ---------------------------------------------------------------------------
# G1 Scenario 2-3: flag=true → task 생성, 중복 start 방지
# ---------------------------------------------------------------------------


class TestStartUsdtWsGopaxClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    async def test_creates_task_when_enabled(self):
        """flag=true → GopaxWsClient + task 생성."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_gopax_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_gopax_client()
            try:
                self.assertIsNotNone(scheduler.usdt_ws_gopax_client)
                self.assertIsNotNone(scheduler.usdt_ws_gopax_task)
                self.assertFalse(scheduler.usdt_ws_gopax_task.done())
            finally:
                await scheduler.shutdown_usdt_ws_gopax_client()

    async def test_double_start_skipped_when_task_running(self):
        """이미 task 실행 중에 start 재호출 → 동일 task 유지 (중복 방지)."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_gopax_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_gopax_client()
            first_task = scheduler.usdt_ws_gopax_task
            first_client = scheduler.usdt_ws_gopax_client
            self.assertIsNotNone(first_task)
            try:
                # 2번째 start 호출 — 동일 task 유지
                await scheduler.start_usdt_ws_gopax_client()
                self.assertIs(scheduler.usdt_ws_gopax_task, first_task)
                self.assertIs(scheduler.usdt_ws_gopax_client, first_client)
            finally:
                await scheduler.shutdown_usdt_ws_gopax_client()


# ---------------------------------------------------------------------------
# G1 Shutdown: stop_event set + task 종료 + globals 초기화
# ---------------------------------------------------------------------------


class TestShutdownUsdtWsGopaxClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    def tearDown(self):
        _reset_scheduler_usdt_ws_gopax_globals()

    async def test_shutdown_cleans_up_running_task(self):
        """shutdown 호출 → client.stop() + task 종료 + globals=None."""
        with patch.object(config, "USDT_WS_GOPAX_ENABLED", True), \
             patch.object(
                 scheduler, "_run_usdt_ws_gopax_client",
                 side_effect=_make_fake_run_runner(),
             ):
            await scheduler.start_usdt_ws_gopax_client()
            task = scheduler.usdt_ws_gopax_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())

            await scheduler.shutdown_usdt_ws_gopax_client()

            self.assertTrue(task.done())
            self.assertIsNone(scheduler.usdt_ws_gopax_client)
            self.assertIsNone(scheduler.usdt_ws_gopax_task)


# ---------------------------------------------------------------------------
# G1 Client skeleton: minimal state + start/stop lifecycle
# ---------------------------------------------------------------------------


class TestGopaxWsClientSkeleton(unittest.IsolatedAsyncioTestCase):
    """G7 client — G1~G6b attribute + G7 UsdtAlertEvaluator instance 보유.

    heartbeat 추적은 _liveness.last_heartbeat_at로 일원화.
    G7까지 fanout 누적: Redis + DB + Fallback + Alert.
    """

    async def test_init_state_g7_scope(self):
        """__init__ 직후: G1~G7 attribute 모두 보유."""
        from app.notifications.alert_evaluator import UsdtAlertEvaluator

        client = GopaxWsClient()
        # G1 state
        self.assertFalse(client._running)
        self.assertFalse(client._stop_event.is_set())
        # G2 state
        self.assertIsNone(client._ws)
        self.assertFalse(client._first_tick_logged)
        # G4 state
        self.assertEqual(client._connection_status, "normal")
        self.assertEqual(client._ticker_freshness_status, "normal")
        self.assertEqual(client._reconnect_attempt_count, 0)
        self.assertIsNotNone(client._liveness)
        self.assertEqual(set(client._status_transition_count.keys()), {
            "connection_normal", "connection_reconnecting", "connection_stale",
            "ticker_normal", "ticker_warning", "ticker_degraded",
        })
        for v in client._status_transition_count.values():
            self.assertEqual(v, 0)
        # G5 state
        self.assertIsNotNone(client._redis_writer)
        self.assertEqual(client._redis_writer.saturation_count, 0)
        # G6a state
        self.assertIsNotNone(client._db_writer)
        # G6b state — fallback controller (Coinone C6b mirror)
        self.assertIsNotNone(client._fallback_controller)
        self.assertEqual(client._fallback_controller.scheduled_probe_count, 0)
        # G7 state — Alert evaluator (Coinone C7 mirror, single instance)
        self.assertIsNotNone(client._alert_evaluator)
        self.assertIsInstance(client._alert_evaluator, UsdtAlertEvaluator)
        # fallback controller에 inject된 evaluator는 client 보유와 동일 instance
        self.assertIs(
            client._fallback_controller._alert_evaluator,
            client._alert_evaluator,
        )

    async def test_start_stop_lifecycle(self):
        """start() → _running=True → stop() → _running=False.

        G4 reconnect loop: _run_one_session이 stop_event 대기로 mock되어 stop()
        호출 시 풀림. 즉시 return mock은 G4 reconnect loop의 무한 호출 유발 (hang).
        """
        client = GopaxWsClient()

        async def stop_after_start():
            await asyncio.sleep(0.01)
            self.assertTrue(client._running)
            await client.stop()

        # G4 reconnect loop과 호환: stop_event 대기로 1회만 호출되도록 처리
        async def fake_run_session():
            await client._stop_event.wait()

        with patch.object(client, "_run_one_session", side_effect=fake_run_session):
            await asyncio.gather(client.start(), stop_after_start())

        self.assertFalse(client._running)
        self.assertTrue(client._stop_event.is_set())

    async def test_double_start_idempotent(self):
        """start() 호출 중 중복 start → 즉시 return (no duplicate lifecycle entry)."""
        client = GopaxWsClient()

        async def fake_run_session():
            # session은 stop_event까지 대기
            await client._stop_event.wait()

        with patch.object(client, "_run_one_session", side_effect=fake_run_session):
            start_task = asyncio.create_task(client.start())
            await asyncio.sleep(0.01)

            # 중복 start — 즉시 return
            await client.start()
            self.assertTrue(client._running)

            await client.stop()
            await start_task

        self.assertFalse(client._running)


# ---------------------------------------------------------------------------
# Module scope guard: GOPAX_WS_URL + G5 constants/class 노출 + G6~ symbol 부재
# (stage 진행에 따라 갱신 — 현재 stage G5)
# ---------------------------------------------------------------------------


class TestScopeGuard(unittest.TestCase):
    """G7 module-level scope guard — G1~G7 expected symbol 노출 검증.

    Stage 진행에 따라 갱신 (G3 → G4 → G5 → ... → G7). G7 land 시 alert evaluator
    관련 symbol (AlertObservation/UsdtAlertEvaluator)은 의도적 module-level
    import (Coinone C7 / Bithumb U7 / Korbit K7 mirror).
    """

    def test_gopax_ws_url_exported(self):
        """G1: GOPAX_WS_URL constant module-level 노출."""
        self.assertEqual(GOPAX_WS_URL, "wss://wsapi.gopax.co.kr")

    def test_g7_module_level_symbols_present(self):
        """G7: G1~G7 stage symbol module-level 노출 검증.

        G6b에서 GopaxRestFallbackController + FALLBACK_COOLDOWN_SEC +
        FALLBACK_PROBE_TIMEOUT_SEC. G7 추가: AlertObservation / UsdtAlertEvaluator
        의도적 module-level import (Coinone C7 mirror — Bithumb U7 패턴).
        """
        from app.crawlers.usdt_ws import gopax as gopax_module

        # G4 + G5 + G6a + G6b + G7 constants/class 허용 (검증 — 존재해야 함)
        for attr in [
            "STALE_AFTER_SEC", "TICKER_FRESHNESS_WARNING_SEC",
            "TICKER_FRESHNESS_DEGRADED_SEC", "RECONNECT_BACKOFF_SEQ",
            "RECONNECT_BACKOFF_TAIL",
            "MAX_PENDING_WRITES", "REDIS_CLOSE_TIMEOUT_SEC",
            "GopaxRedisWriter",
            "DB_WRITE_WINDOW_SEC",
            "GopaxDbWriter",
            # G6b 신규
            "FALLBACK_COOLDOWN_SEC",
            "FALLBACK_PROBE_TIMEOUT_SEC",
            "GopaxRestFallbackController",
            # G7 신규 — 의도적 module-level (fanout step 3: observation_from_tick adapter)
            "observation_from_tick",
            "UsdtAlertEvaluator",
            # PR 2e 신규 — 60s summary log cycle (Coinone PR 2d mirror)
            "SUMMARY_LOG_INTERVAL_SEC",
        ]:
            self.assertTrue(
                hasattr(gopax_module, attr),
                f"G4/G5/G6a/G6b/G7 symbol 누락: {attr}",
            )


# ===========================================================================
# G2 Subscribe + Parse + USDT-KRW 필터링
# ===========================================================================


class TestBuildSubscribePayload(unittest.TestCase):
    """G2: SubscribeToTickers payload — pair 지정 불가, 전체 ticker 구독."""

    def test_subscribe_payload_shape(self):
        """guide §7 spec: {"n": "SubscribeToTickers", "o": {}}"""
        payload = GopaxWsClient._build_subscribe_payload()
        self.assertEqual(payload, {"n": "SubscribeToTickers", "o": {}})


class TestParseInitialMessage(unittest.TestCase):
    """G2: SubscribeToTickers initial response — array에서 USDT-KRW 매칭."""

    def test_parse_initial_with_usdt_krw(self):
        """initial array → USDT-KRW 추출 → normalized tick."""
        client = GopaxWsClient()
        message = {
            "n": "SubscribeToTickers",
            "o": {
                "data": [
                    {"tradingPairName": "BTC-KRW", "last": 100000000, "lastTraded": 1777112971900},
                    {"tradingPairName": "USDT-KRW", "last": 1490.5, "lastTraded": 1777112971993},
                    {"tradingPairName": "ETH-KRW", "last": 5000000, "lastTraded": 1777112971800},
                ]
            },
        }
        tick = client._parse_ticker_message(json.dumps(message))
        self.assertEqual(tick, {
            "source": "gopax",
            "asset": "usdt-krw",
            "rate": 1490.5,
            "timestamp_ms": 1777112971993,
        })

    def test_parse_initial_without_usdt_krw(self):
        """initial array에 USDT-KRW 없음 → None."""
        client = GopaxWsClient()
        message = {
            "n": "SubscribeToTickers",
            "o": {
                "data": [
                    {"tradingPairName": "BTC-KRW", "last": 100000000, "lastTraded": 1777112971900},
                ]
            },
        }
        self.assertIsNone(client._parse_ticker_message(json.dumps(message)))

    def test_parse_initial_empty_data(self):
        """initial data 부재 → None."""
        client = GopaxWsClient()
        message = {"n": "SubscribeToTickers", "o": {}}
        self.assertIsNone(client._parse_ticker_message(json.dumps(message)))


class TestParseTickerEventMessage(unittest.TestCase):
    """G2: TickerEvent delta — dict에서 USDT-KRW key 직접 추출."""

    def test_parse_ticker_event_with_usdt_krw(self):
        """TickerEvent o["USDT-KRW"] → normalized tick."""
        client = GopaxWsClient()
        message = {
            "i": -1,
            "n": "TickerEvent",
            "o": {
                "USDT-KRW": {
                    "tradingPairName": "USDT-KRW",
                    "last": 1491.2,
                    "lastTraded": 1777112972500,
                }
            },
        }
        tick = client._parse_ticker_message(json.dumps(message))
        self.assertEqual(tick, {
            "source": "gopax",
            "asset": "usdt-krw",
            "rate": 1491.2,
            "timestamp_ms": 1777112972500,
        })

    def test_parse_ticker_event_without_usdt_krw(self):
        """TickerEvent o에 USDT-KRW key 부재 → None."""
        client = GopaxWsClient()
        message = {
            "i": -1,
            "n": "TickerEvent",
            "o": {"BTC-KRW": {"last": 100000000, "lastTraded": 1777112972500}},
        }
        self.assertIsNone(client._parse_ticker_message(json.dumps(message)))


class TestParsePrimusSkip(unittest.TestCase):
    """G2: Primus ping skip — Codex 정정 반영 3가지 form 모두 None.

    G3에서 pong 응답 추가 예정. G2는 skip만.
    """

    def test_primus_ping_json_string_form(self):
        """JSON string form: '"primus::ping::..."' (with outer quotes) → None."""
        client = GopaxWsClient()
        raw = '"primus::ping::1777112968768"'
        self.assertIsNone(client._parse_ticker_message(raw))

    def test_primus_ping_plain_text_form(self):
        """Plain text form: 'primus::ping::...' (no quotes) → None."""
        client = GopaxWsClient()
        raw = "primus::ping::1777112968768"
        self.assertIsNone(client._parse_ticker_message(raw))

    def test_primus_ping_json_decoded_str(self):
        """JSON-decoded str: json.loads('"primus::ping::..."') = "primus::..." → None.

        실제로는 raw text level (step 2) 또는 JSON-decoded str (step 4) 둘 중
        하나에서 매칭됨. 이 test는 raw가 이미 decoded str로 들어온 case 가정 —
        그러나 _parse_ticker_message는 string으로 받아 step 2에서 매칭 (실제 동작).
        의도: 어떤 경로로 들어와도 primus::는 None이라는 것을 잠근다.
        """
        client = GopaxWsClient()
        # 실제 ws.recv()는 raw string으로 옴 — 이미 step 2에서 잡힘.
        # raw가 step 4 진입 시나리오는 거의 없으나 의미 검증 목적.
        raw_with_leading_whitespace = '  "primus::ping::1234"  '
        self.assertIsNone(client._parse_ticker_message(raw_with_leading_whitespace))


class TestParseInvalidFrames(unittest.TestCase):
    """G2: 잘못된 frame 격리 — bytes decode 실패 / JSON 파싱 실패 / invalid types / 잘못된 값."""

    def test_bytes_decode_failure(self):
        """invalid UTF-8 bytes → None."""
        client = GopaxWsClient()
        self.assertIsNone(client._parse_ticker_message(b"\xff\xfe invalid"))

    def test_json_decode_failure(self):
        """JSON 파싱 실패 → None (Primus skip 통과 후 JSONDecodeError 캡처)."""
        client = GopaxWsClient()
        self.assertIsNone(client._parse_ticker_message("this is not json"))

    def test_unknown_frame_type(self):
        """알 수 없는 `n` → None."""
        client = GopaxWsClient()
        message = {"n": "UnknownFrameType", "o": {}}
        self.assertIsNone(client._parse_ticker_message(json.dumps(message)))

    def test_last_zero_or_negative(self):
        """last <= 0 → None (timestamp valid이어도 rate invalid)."""
        client = GopaxWsClient()
        message = {
            "n": "TickerEvent",
            "o": {
                "USDT-KRW": {"last": 0, "lastTraded": 1777112972500},
            },
        }
        self.assertIsNone(client._parse_ticker_message(json.dumps(message)))

    def test_last_traded_zero(self):
        """lastTraded == 0 → None (timestamp 0은 invalid)."""
        client = GopaxWsClient()
        message = {
            "n": "TickerEvent",
            "o": {"USDT-KRW": {"last": 1490.5, "lastTraded": 0}},
        }
        self.assertIsNone(client._parse_ticker_message(json.dumps(message)))


class TestHandleMessage(unittest.IsolatedAsyncioTestCase):
    """G2: _handle_message — first tick INFO 1회 + 이후 DEBUG (Bithumb 패턴 mirror)."""

    async def test_first_tick_logged_once_at_info(self):
        """valid tick 첫 수신 시 INFO 1회 emit, 이후 DEBUG."""
        client = GopaxWsClient()
        self.assertFalse(client._first_tick_logged)

        message = {
            "n": "TickerEvent",
            "o": {"USDT-KRW": {"last": 1490.5, "lastTraded": 1777112972500}},
        }

        with self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
            tick = client._handle_message(json.dumps(message))
        self.assertIsNotNone(tick)
        self.assertTrue(client._first_tick_logged)
        self.assertTrue(any("first tick" in m for m in cm.output))

        # 2번째 tick — DEBUG로 격하 (assertNoLogs at INFO+ 검증 어려워서 first_tick_logged 유지로 잠금)
        with self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="DEBUG") as cm:
            tick2 = client._handle_message(json.dumps(message))
        self.assertIsNotNone(tick2)
        # first tick INFO는 1회만 (이후는 DEBUG)
        info_logs = [m for m in cm.output if "first tick" in m]
        self.assertEqual(len(info_logs), 0, "first tick INFO가 2회 emit됨 (1회만 허용)")

    async def test_invalid_message_returns_none_no_log(self):
        """invalid raw → None + first_tick_logged 변경 없음."""
        client = GopaxWsClient()
        self.assertIsNone(client._handle_message("not json"))
        self.assertFalse(client._first_tick_logged)


class TestRunOneSession(unittest.IsolatedAsyncioTestCase):
    """G2: _run_one_session — connect + subscribe + recv loop + parse only.

    G5-G7 fanout (Redis/DB/Alert)은 본 stage 외.
    """

    async def test_connect_and_subscribe_send(self):
        """connect → SubscribeToTickers payload send 검증."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        # recv: 첫 호출 시 stop_event set으로 즉시 loop 빠짐
        async def stop_then_recv():
            client._stop_event.set()
            return await asyncio.sleep(10)  # 도달 안 함
        mock_ws.recv = AsyncMock(side_effect=stop_then_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # SubscribeToTickers payload send 검증
        mock_ws.send.assert_called_once()
        sent_arg = mock_ws.send.call_args[0][0]
        self.assertEqual(json.loads(sent_arg), {"n": "SubscribeToTickers", "o": {}})

    async def test_stop_event_breaks_recv_loop(self):
        """stop_event set 시 recv loop 즉시 break + ws cleanup."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        # recv: 첫 호출 시 stop_event set + Primus skip frame return.
        # → handle_message None → loop next iteration → stop_event check → break.
        recv_count = {"n": 0}
        async def fake_recv():
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                client._stop_event.set()
                return "primus::ping::skip"  # skip-only frame
            await asyncio.sleep(10)  # 도달 안 함

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # ws cleanup 검증
        self.assertIsNone(client._ws)


class TestG2ActivationConstraint(unittest.TestCase):
    """G2 production activation 금지 — Codex 강조 (G3/G4 land 전 flag=true 금지).

    docstring에 명시되어 있어야 미래 maintainer가 인지 가능.
    Codex 정정 반영: silent termination이 아니라 silent idle/stuck — 이 의미도 잠금.
    """

    def test_module_docstring_warns_activation_constraint(self):
        """gopax.py module docstring에 'Production activation 제약' 명시 (G4 갱신)."""
        import app.crawlers.usdt_ws.gopax as gopax_module
        doc = gopax_module.__doc__ or ""
        self.assertIn("Production activation 제약", doc)
        self.assertIn("G4", doc)
        # G5 + telemetry 이후 검토 명시 (Codex 강조)
        self.assertIn("G5", doc)
        self.assertIn("telemetry", doc)


# ===========================================================================
# G3 Primus pong handler + heartbeat
# ===========================================================================


class TestIsPrimusPing(unittest.TestCase):
    """G3: _is_primus_ping — ping 전용 매칭 (Codex 정정: pong/open/close 제외)."""

    def test_json_string_form_ping(self):
        """JSON string form: '"primus::ping::..."' → True."""
        self.assertTrue(GopaxWsClient._is_primus_ping('"primus::ping::1234"'))

    def test_plain_text_form_ping(self):
        """Plain text form: 'primus::ping::...' → True."""
        self.assertTrue(GopaxWsClient._is_primus_ping("primus::ping::1234"))

    def test_whitespace_prefixed_ping(self):
        """Leading whitespace → strip 후 매칭 → True."""
        self.assertTrue(GopaxWsClient._is_primus_ping("  primus::ping::1234  "))

    def test_bytes_form_ping(self):
        """bytes → decode 후 매칭 → True."""
        self.assertTrue(GopaxWsClient._is_primus_ping(b"primus::ping::1234"))


class TestIsPrimusPingNonPingRejected(unittest.TestCase):
    """G3 (Codex 정정): non-ping Primus control frame은 False — ping 전용 매칭.

    pong/open/close 등은 _handle_primus_ping이 처리하면 안 된다.
    """

    def test_primus_pong_rejected(self):
        """primus::pong::... → False (ping 아님, pong을 client가 다시 응답 안 함)."""
        self.assertFalse(GopaxWsClient._is_primus_ping('"primus::pong::1234"'))
        self.assertFalse(GopaxWsClient._is_primus_ping("primus::pong::1234"))

    def test_primus_open_rejected(self):
        """primus::open::... → False (control frame)."""
        self.assertFalse(GopaxWsClient._is_primus_ping("primus::open::1234"))

    def test_normal_json_message_rejected(self):
        """일반 JSON ticker frame → False."""
        self.assertFalse(GopaxWsClient._is_primus_ping('{"n":"TickerEvent","o":{}}'))
        self.assertFalse(GopaxWsClient._is_primus_ping("any other text"))


class TestHandlePrimusPing(unittest.IsolatedAsyncioTestCase):
    """G3: _handle_primus_ping — pong send + heartbeat 갱신 + bool return."""

    async def test_json_string_form_pong_sent(self):
        """JSON string ping → JSON string pong 송신, True return, heartbeat 갱신."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        self.assertIsNone(client._liveness.last_heartbeat_at)

        result = await client._handle_primus_ping(mock_ws, '"primus::ping::1234"')

        self.assertTrue(result)
        mock_ws.send.assert_called_once_with('"primus::pong::1234"')
        # heartbeat은 send 성공 시점에만 갱신
        self.assertIsNotNone(client._liveness.last_heartbeat_at)
        self.assertIsInstance(client._liveness.last_heartbeat_at, float)

    async def test_plain_text_form_pong_sent(self):
        """Plain text ping → plain text pong 송신, True return, heartbeat 갱신."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        result = await client._handle_primus_ping(mock_ws, "primus::ping::5678")

        self.assertTrue(result)
        mock_ws.send.assert_called_once_with("primus::pong::5678")
        self.assertIsNotNone(client._liveness.last_heartbeat_at)

    async def test_bytes_form_pong_sent(self):
        """bytes ping → str decode + pong 송신, True return."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        result = await client._handle_primus_ping(mock_ws, b"primus::ping::9999")

        self.assertTrue(result)
        mock_ws.send.assert_called_once_with("primus::pong::9999")
        self.assertIsNotNone(client._liveness.last_heartbeat_at)


class TestHandlePrimusPingFailureSessionEnd(unittest.IsolatedAsyncioTestCase):
    """G3: pong send 실패 → False return + heartbeat 미갱신.

    _run_one_session에서 False return 시 session 종료/reconnect 처리.
    """

    async def test_send_failure_returns_false_and_no_heartbeat(self):
        """ws.send 실패 → False + heartbeat None 유지."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock(side_effect=ConnectionClosed(None, None))

        result = await client._handle_primus_ping(mock_ws, "primus::ping::1234")

        self.assertFalse(result)
        # send 실패 시 heartbeat 미갱신
        self.assertIsNone(client._liveness.last_heartbeat_at)


class TestRunOneSessionPrimusFlow(unittest.IsolatedAsyncioTestCase):
    """G3: _run_one_session 안 Primus 분기 — pong 송신 + parse 호출 안 됨."""

    async def test_primus_ping_triggers_pong_send(self):
        """recv loop에서 Primus ping 도착 → pong 송신 + parse 호출 0."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        recv_count = {"n": 0}
        async def fake_recv():
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                return "primus::ping::1234"
            if recv_count["n"] == 2:
                # 2번째 recv 시 stop_event set으로 loop 종료
                client._stop_event.set()
                return "primus::ping::5678"
            await asyncio.sleep(10)

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        # _handle_message는 parse 진입 검증 (Primus는 진입 안 됨)
        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client, "_handle_message") as mock_handle:
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # subscribe 1회 + 2 pong replacement = 3회 send
        self.assertEqual(mock_ws.send.call_count, 3)
        # parse layer (_handle_message)는 호출 안 됨 (모두 Primus였음)
        mock_handle.assert_not_called()

    async def test_pong_send_failure_raises_runtime_error(self):
        """Codex v2 정정: pong send 실패 → RuntimeError raise (return X).

        return 시 start()는 정상 종료로 인식 → else: continue → tight loop.
        raise 시 start() except 경로 → attempt++ + backoff.
        """
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        send_count = {"n": 0}
        async def fake_send(payload):
            send_count["n"] += 1
            if send_count["n"] == 1:
                return  # subscribe 성공
            raise ConnectionClosed(None, None)  # pong 실패

        mock_ws.send = AsyncMock(side_effect=fake_send)
        mock_ws.recv = AsyncMock(return_value="primus::ping::1234")

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        # G4 정정: pong send 실패 → RuntimeError raise (start()에서 except 처리)
        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), self.assertRaises(RuntimeError):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # heartbeat 미갱신 (send 실패)
        self.assertIsNone(client._liveness.last_heartbeat_at)


# Note: TestG3ActivationConstraint (G3 시점 docstring 검증) 삭제됨.
# G4에서 docstring이 "G4 단계도 유지" + "G5/telemetry 이후 검토"로 갱신됨에 따라
# TestG2ActivationConstraint가 G4 keyword로 갱신되어 통합 검증 — TestG3ActivationConstraint
# 는 중복.

# Note: TestG2StartIdleAfterSessionEnd (G2 시점 idle 동작 잠금) 삭제됨.
# G4 reconnect loop 진입으로 "silent idle" 의미 사라짐 — _run_one_session 종료 시
# attempt++ 후 backoff + 재시도 (idle 상태 부재). G2/G3 시점 idle 의미는 G4 reconnect
# loop으로 대체. TestReconnectLoop가 G4 reconnect 동작 대신 검증.


# ===========================================================================
# G4 UsdtLivenessMonitor + reconnect loop + 2-signal status
# ===========================================================================


class TestSetConnectionStatus(unittest.TestCase):
    """G4: _set_connection_status 전이 + counter + log (Coinone/Korbit 패턴 mirror)."""

    def test_initial_state_normal(self):
        """__init__ 직후 connection_status = normal."""
        client = GopaxWsClient()
        self.assertEqual(client._connection_status, "normal")

    def test_transition_changes_status_and_counter(self):
        """normal → stale 전이 시 status + counter ++ + log 1회."""
        client = GopaxWsClient()
        prev_count = client._status_transition_count["connection_stale"]
        with self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
            client._set_connection_status("stale")
        self.assertEqual(client._connection_status, "stale")
        self.assertEqual(client._status_transition_count["connection_stale"], prev_count + 1)
        self.assertTrue(any("connection_status normal → stale" in m for m in cm.output))

    def test_no_transition_when_same_status(self):
        """동일 status 재호출 시 counter 미증가 + log 미발화."""
        client = GopaxWsClient()
        client._set_connection_status("normal")  # init 동일
        prev_count = client._status_transition_count["connection_normal"]
        # 동일 status 재호출 — counter ++ X
        client._set_connection_status("normal")
        self.assertEqual(client._status_transition_count["connection_normal"], prev_count)


class TestSetTickerFreshnessStatus(unittest.TestCase):
    """G4: _set_ticker_freshness_status 전이 + counter + log."""

    def test_initial_state_normal(self):
        """__init__ 직후 ticker_freshness_status = normal."""
        client = GopaxWsClient()
        self.assertEqual(client._ticker_freshness_status, "normal")

    def test_transition_to_warning_logs_once(self):
        """normal → warning 전이 시 1회 log + counter."""
        client = GopaxWsClient()
        prev = client._status_transition_count["ticker_warning"]
        with self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
            client._set_ticker_freshness_status("warning")
        self.assertEqual(client._ticker_freshness_status, "warning")
        self.assertEqual(client._status_transition_count["ticker_warning"], prev + 1)
        self.assertTrue(any("ticker_freshness_status normal → warning" in m for m in cm.output))

    def test_transition_warning_to_degraded(self):
        """warning → degraded 전이."""
        client = GopaxWsClient()
        client._set_ticker_freshness_status("warning")
        prev = client._status_transition_count["ticker_degraded"]
        with self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO"):
            client._set_ticker_freshness_status("degraded")
        self.assertEqual(client._ticker_freshness_status, "degraded")
        self.assertEqual(client._status_transition_count["ticker_degraded"], prev + 1)


# Note: TestTickerFreshnessDegradedNoFallback (G4 시점 fallback 부재 검증) 삭제됨.
# G6b 진입으로 의미 반전 — degraded 전이 시 fallback_controller.schedule_probe 호출.
# TestTickerFreshnessDegradedTriggersFallback + TestNormalRecoveryResetsCooldown로 대체.


class TestComputeBackoff(unittest.TestCase):
    """G4: _compute_backoff seq lookup (Bithumb mirror)."""

    def test_attempt_zero_returns_first(self):
        """attempt=0 시 seq[0] = 1.0."""
        self.assertEqual(GopaxWsClient._compute_backoff(0), 1.0)

    def test_attempt_in_seq(self):
        """attempt in [1..6] → seq[attempt-1]."""
        self.assertEqual(GopaxWsClient._compute_backoff(1), 1.0)
        self.assertEqual(GopaxWsClient._compute_backoff(3), 4.0)
        self.assertEqual(GopaxWsClient._compute_backoff(6), 30.0)

    def test_attempt_exhausted_returns_tail(self):
        """attempt > seq length → tail 30.0."""
        self.assertEqual(GopaxWsClient._compute_backoff(7), 30.0)
        self.assertEqual(GopaxWsClient._compute_backoff(100), 30.0)


class TestStaleTransition(unittest.IsolatedAsyncioTestCase):
    """G4: stale ↔ normal 전이 (liveness.is_stale 기반).

    last_activity_at silence > STALE_AFTER_SEC → stale / 회복 시 normal.
    """

    def test_is_stale_true_when_silence_exceeds_threshold(self):
        """last_activity_at None or 매우 오래된 → is_stale True."""
        from app.crawlers.usdt_ws.gopax import STALE_AFTER_SEC
        client = GopaxWsClient()
        now = time.time()
        # silence > threshold
        client._liveness.last_tick_at = now - (STALE_AFTER_SEC + 10)
        self.assertTrue(client._liveness.is_stale(now, STALE_AFTER_SEC))

    def test_is_stale_false_when_fresh(self):
        """최근 tick 있으면 is_stale False."""
        from app.crawlers.usdt_ws.gopax import STALE_AFTER_SEC
        client = GopaxWsClient()
        now = time.time()
        client._liveness.last_tick_at = now - 10  # fresh
        self.assertFalse(client._liveness.is_stale(now, STALE_AFTER_SEC))


class TestReconnectLoop(unittest.IsolatedAsyncioTestCase):
    """G4: start() reconnect loop — Bithumb 패턴 mirror."""

    async def test_connection_closed_triggers_reconnect_with_backoff(self):
        """ConnectionClosed → reconnect attempt ++ + status reconnecting + backoff."""
        client = GopaxWsClient()
        call_count = {"n": 0}

        async def fake_run_one_session():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionClosed(None, None)
            # 2회차에 stop_event set으로 loop break
            client._stop_event.set()

        # backoff을 0으로 patch — asyncio.wait_for(stop_event.wait(), timeout=0) → 즉시 처리
        # (asyncio.wait_for 자체를 patch하면 outer wait_for(client.start(), ...)도 영향받음)
        with patch.object(client, "_run_one_session", side_effect=fake_run_one_session), \
             patch.object(client, "_compute_backoff", return_value=0.0):
            await asyncio.wait_for(client.start(), timeout=2.0)

        # ConnectionClosed 1회 → attempt++ + _reconnect_attempt_count++
        self.assertEqual(client._reconnect_attempt_count, 1)
        # connection_status reconnecting 전이 검증
        self.assertEqual(
            client._status_transition_count["connection_reconnecting"], 1
        )

    async def test_stop_event_during_backoff_breaks_loop(self):
        """backoff 중 stop_event set → 즉시 loop break."""
        client = GopaxWsClient()

        async def fake_run_one_session():
            raise ConnectionClosed(None, None)

        async def fake_wait_for(coro, timeout):
            # backoff sleep 중 stop_event 도착 시뮬레이션 — coro 정상 종료
            await coro  # stop_event.wait() — 즉시 풀리도록 set 가정
            return

        # 미리 stop_event set
        client._stop_event.set()

        with patch.object(client, "_run_one_session", side_effect=fake_run_one_session):
            await asyncio.wait_for(client.start(), timeout=2.0)

        # session 진입 안 함 (stop_event 사전 set)
        self.assertEqual(client._reconnect_attempt_count, 0)

    async def test_exception_triggers_reconnect_with_attempt_counter(self):
        """일반 Exception → reconnect attempt counter ++."""
        client = GopaxWsClient()
        call_count = {"n": 0}

        async def fake_run_one_session():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated session error")
            client._stop_event.set()

        with patch.object(client, "_run_one_session", side_effect=fake_run_one_session), \
             patch.object(client, "_compute_backoff", return_value=0.0):
            await asyncio.wait_for(client.start(), timeout=2.0)

        self.assertEqual(client._reconnect_attempt_count, 1)
        self.assertEqual(
            client._status_transition_count["connection_reconnecting"], 1
        )


class TestHandleMessageUpdatesLiveness(unittest.IsolatedAsyncioTestCase):
    """G4: _handle_message valid tick 시 _liveness.observe_tick 호출."""

    async def test_valid_tick_observes_liveness(self):
        """valid tick → _liveness.last_tick_at 갱신 + frame_count_total ++."""
        client = GopaxWsClient()
        message = {
            "n": "TickerEvent",
            "o": {"USDT-KRW": {"last": 1490.5, "lastTraded": 1777112972500}},
        }
        prev_total = client._liveness.frame_count_total
        prev_tick_at = client._liveness.last_tick_at

        tick = client._handle_message(json.dumps(message))

        self.assertIsNotNone(tick)
        # observe_tick 호출 검증
        self.assertEqual(client._liveness.frame_count_total, prev_total + 1)
        self.assertIsNotNone(client._liveness.last_tick_at)
        if prev_tick_at is not None:
            self.assertGreater(client._liveness.last_tick_at, prev_tick_at)

    async def test_invalid_message_no_liveness_update(self):
        """invalid → _liveness 미갱신."""
        client = GopaxWsClient()
        prev_total = client._liveness.frame_count_total
        self.assertIsNone(client._handle_message("not json"))
        self.assertEqual(client._liveness.frame_count_total, prev_total)


class TestG4ActivationConstraint(unittest.TestCase):
    """PR 2e (Codex 보강 표현): PR 2e land 후에도 activation 보류 docstring 명시.

    "PR 2e land 후에도 USDT_WS_GOPAX_ENABLED=false 유지. activation은 별도 canary
    조건 검토 및 짧은 안정 관찰 후 결정 (PR 2e 자체만으로 자동 activation 아님)" 명시.
    """

    def test_module_docstring_pr2e_activation_constraint(self):
        """module docstring에 현재 stage (PR 2e) activation 보류 명시.

        Codex 보강 표현: PR 2e land 후에도 USDT_WS_GOPAX_ENABLED=false 유지.
        activation은 별도 canary 조건 검토 및 짧은 안정 관찰 후 결정.
        """
        import app.crawlers.usdt_ws.gopax as gopax_module
        doc = gopax_module.__doc__ or ""
        self.assertIn("PR 2e", doc)
        self.assertIn("canary", doc)
        self.assertIn("짧은 안정 관찰", doc)
        # G7 attribute는 누적 표현 유지
        self.assertIn("G7", doc)

    def test_class_docstring_pr2e_activation_constraint(self):
        """class docstring에 현재 stage (PR 2e) activation 보류 명시."""
        doc = GopaxWsClient.__doc__ or ""
        self.assertIn("PR 2e", doc)
        self.assertIn("canary", doc)

    def test_start_docstring_pr2e_activation_constraint(self):
        """start() docstring에도 현재 stage (PR 2e) activation 보류 명시 (Codex 보강)."""
        doc = GopaxWsClient.start.__doc__ or ""
        self.assertIn("PR 2e", doc)
        self.assertIn("canary", doc)
        # G4/G5/G6a/G6b/G7 stale 표현 부재 검증
        self.assertNotIn("G4 단독", doc)
        self.assertNotIn("G6a 단계도 유지", doc)
        self.assertNotIn("G6b 단계도 유지", doc)
        self.assertNotIn("G7 단계도 유지", doc)


# ===========================================================================
# G5 GopaxRedisWriter — tick-level Redis fanout + saturation counter + topic trigger
# ===========================================================================


def _make_valid_tick() -> dict:
    """Test용 normalized tick (Gopax 형식)."""
    return {
        "source": "gopax",
        "asset": "usdt-krw",
        "rate": 1490.5,
        "timestamp_ms": 1779106625946,
    }


class TestGopaxRedisWriterScheduleSuccess(unittest.IsolatedAsyncioTestCase):
    """G5: schedule → _write_async → to_thread(helper) → KST ISO timestamp."""

    async def test_schedule_calls_helper_with_kst_iso(self):
        """schedule(tick) → set_latest_usdt_rate_from_sync_job 호출 + KST ISO 변환."""
        writer = GopaxRedisWriter()
        tick = _make_valid_tick()
        captured = {}

        def fake_helper(*, source, asset, rate, timestamp):
            captured.update({
                "source": source, "asset": asset,
                "rate": rate, "timestamp": timestamp,
            })
            return UsdtLatestWriteOutcome.SET

        with patch(
            "app.crawlers.usdt_ws.gopax.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=fake_helper,
        ), patch(
            "app.crawlers.usdt_ws.gopax.tether_topic_trigger.request_tether_topic_trigger",
        ):
            writer.schedule(tick)
            await writer.close(timeout=2.0)

        self.assertEqual(captured["source"], "gopax")
        self.assertEqual(captured["asset"], "usdt-krw")
        self.assertEqual(captured["rate"], 1490.5)
        # KST ISO 변환 검증 (timestamp_ms=1779106625946 → KST 시간)
        self.assertIn("T", captured["timestamp"])
        # KST timezone offset +09:00 포함 검증
        self.assertIn("+09:00", captured["timestamp"])


class TestGopaxRedisWriterSaturationCount(unittest.IsolatedAsyncioTestCase):
    """G5 (Codex 권고): saturation_count counter — MAX_PENDING_WRITES skip 누적."""

    def test_initial_value_zero(self):
        """writer 생성 직후 saturation_count == 0."""
        writer = GopaxRedisWriter()
        self.assertEqual(writer.saturation_count, 0)

    async def test_increments_on_saturation_skip(self):
        """_tasks >= MAX_PENDING_WRITES → skip 시 counter +1."""
        writer = GopaxRedisWriter()
        loop = asyncio.get_running_loop()
        dummy = [loop.create_future() for _ in range(MAX_PENDING_WRITES)]
        writer._tasks = set(dummy)

        self.assertEqual(writer.saturation_count, 0)

        with patch("app.crawlers.usdt_ws.gopax.asyncio.to_thread"):
            writer.schedule(_make_valid_tick())

        self.assertEqual(writer.saturation_count, 1)

        for fut in dummy:
            fut.set_result(None)
        writer._tasks.clear()

    async def test_no_increment_on_normal_schedule(self):
        """_tasks < MAX_PENDING_WRITES → 정상 schedule → counter 0 유지."""
        writer = GopaxRedisWriter()
        with patch("app.crawlers.usdt_ws.gopax.asyncio.create_task") as mock_create:
            # schedule()이 인자로 만든 _write_async 코루틴을 close해 "never awaited"
            # 경고 차단 (테스트 위생, app 무변경). saturation_count 검증 의도 불변.
            mock_create.side_effect = lambda coro: coro.close() or MagicMock()
            writer.schedule(_make_valid_tick())
        self.assertEqual(writer.saturation_count, 0)


class TestGopaxRedisWriterSaturationSemantics(unittest.IsolatedAsyncioTestCase):
    """G5 (Codex 대칭 test): helper False/exception은 saturation_count 미증가."""

    async def test_helper_false_does_not_increment_saturation(self):
        """helper False 반환 → _write_async 안 실패. saturation_count 0 유지."""
        writer = GopaxRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.gopax.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=UsdtLatestWriteOutcome.FAILED,
        ), patch(
            "app.crawlers.usdt_ws.gopax.tether_topic_trigger.request_tether_topic_trigger",
        ):
            writer.schedule(_make_valid_tick())
            await writer.close(timeout=2.0)

        self.assertEqual(writer.saturation_count, 0)

    async def test_helper_exception_does_not_increment_saturation(self):
        """helper exception → _write_async 안 실패. saturation_count 0 유지."""
        writer = GopaxRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.gopax.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            side_effect=RuntimeError("Redis 장애 simulated"),
        ):
            writer.schedule(_make_valid_tick())
            await writer.close(timeout=2.0)

        self.assertEqual(writer.saturation_count, 0)


class TestGopaxRedisHelperUsesToThread(unittest.IsolatedAsyncioTestCase):
    """G5: asyncio.to_thread로 sync helper 호출 (event loop non-blocking)."""

    async def test_to_thread_called_with_helper(self):
        writer = GopaxRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.gopax.asyncio.to_thread",
            new=AsyncMock(return_value=True),
        ) as mock_to_thread, patch(
            "app.crawlers.usdt_ws.gopax.tether_topic_trigger.request_tether_topic_trigger",
        ):
            writer.schedule(_make_valid_tick())
            await writer.close(timeout=2.0)

        mock_to_thread.assert_called_once()


class TestGopaxTopicTriggerOnSuccessOnly(unittest.IsolatedAsyncioTestCase):
    """G5: Redis write 성공 시에만 tether topic trigger 호출 (Codex 리뷰 #2)."""

    async def test_trigger_called_on_helper_success(self):
        """helper True → trigger 1회 호출."""
        writer = GopaxRedisWriter()
        trigger_calls = []

        def fake_trigger(*, source, asset, reason):
            trigger_calls.append({"source": source, "asset": asset, "reason": reason})

        with patch(
            "app.crawlers.usdt_ws.gopax.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=UsdtLatestWriteOutcome.SET,
        ), patch(
            "app.crawlers.usdt_ws.gopax.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=fake_trigger,
        ):
            writer.schedule(_make_valid_tick())
            await writer.close(timeout=2.0)

        self.assertEqual(len(trigger_calls), 1)
        self.assertEqual(trigger_calls[0]["source"], "gopax")
        self.assertEqual(trigger_calls[0]["asset"], "usdt-krw")
        self.assertEqual(
            trigger_calls[0]["reason"],
            TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
        )

    async def test_trigger_not_called_on_helper_false(self):
        """helper False → trigger 호출 0."""
        writer = GopaxRedisWriter()
        trigger_calls = []

        def fake_trigger(**kwargs):
            trigger_calls.append(kwargs)

        with patch(
            "app.crawlers.usdt_ws.gopax.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=UsdtLatestWriteOutcome.FAILED,
        ), patch(
            "app.crawlers.usdt_ws.gopax.tether_topic_trigger.request_tether_topic_trigger",
            side_effect=fake_trigger,
        ):
            writer.schedule(_make_valid_tick())
            await writer.close(timeout=2.0)

        self.assertEqual(len(trigger_calls), 0)


class TestGopaxRedisCloseDrain(unittest.IsolatedAsyncioTestCase):
    """G5: close() drain timeout 후 cancel + _tasks.clear."""

    async def test_close_clears_tasks_after_drain(self):
        """schedule 후 close → _tasks.clear (Bithumb mirror)."""
        writer = GopaxRedisWriter()
        with patch(
            "app.crawlers.usdt_ws.gopax.latest_rates_cache.set_latest_usdt_rate_from_sync_job",
            return_value=UsdtLatestWriteOutcome.SET,
        ), patch(
            "app.crawlers.usdt_ws.gopax.tether_topic_trigger.request_tether_topic_trigger",
        ):
            writer.schedule(_make_valid_tick())
            self.assertEqual(len(writer._tasks), 1)
            await writer.close(timeout=2.0)
        self.assertEqual(len(writer._tasks), 0)


class TestGopaxRunOneSessionRedisFanout(unittest.IsolatedAsyncioTestCase):
    """G5: _run_one_session valid tick path → _redis_writer.schedule 호출."""

    async def test_valid_tick_triggers_redis_schedule(self):
        """recv → valid TickerEvent → _redis_writer.schedule 호출 검증."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        ticker_message = json.dumps({
            "n": "TickerEvent",
            "o": {"USDT-KRW": {"last": 1490.5, "lastTraded": 1779106625946}},
        })

        recv_count = {"n": 0}
        async def fake_recv():
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                return ticker_message
            if recv_count["n"] == 2:
                client._stop_event.set()
                return ticker_message
            await asyncio.sleep(10)

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._redis_writer, "schedule") as mock_schedule, \
           patch.object(client._redis_writer, "close", new=AsyncMock()), \
           patch.object(client._db_writer, "schedule"), \
           patch.object(client._db_writer, "close", new=AsyncMock()), \
           patch.object(client._alert_evaluator, "schedule"), \
           patch.object(client._alert_evaluator, "close", new=AsyncMock()):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # 2회 valid tick → schedule 2회 호출
        self.assertEqual(mock_schedule.call_count, 2)
        # tick shape 검증
        for call in mock_schedule.call_args_list:
            tick = call.args[0]
            self.assertEqual(tick["source"], "gopax")
            self.assertEqual(tick["asset"], "usdt-krw")
            self.assertEqual(tick["rate"], 1490.5)


class TestGopaxPrimusFrameNoSchedule(unittest.IsolatedAsyncioTestCase):
    """G5 (Codex 포인트 #5): Primus ping/control frame → Redis schedule 호출 0."""

    async def test_primus_ping_does_not_trigger_schedule(self):
        """recv Primus ping → _handle_primus_ping만 호출, schedule 호출 0."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        recv_count = {"n": 0}
        async def fake_recv():
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                client._stop_event.set()
                return "primus::ping::1234"
            await asyncio.sleep(10)

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._redis_writer, "schedule") as mock_schedule, \
           patch.object(client._redis_writer, "close", new=AsyncMock()), \
           patch.object(client._db_writer, "close", new=AsyncMock()), \
           patch.object(client._alert_evaluator, "schedule"), \
           patch.object(client._alert_evaluator, "close", new=AsyncMock()):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # Primus ping → schedule 호출 0
        mock_schedule.assert_not_called()


class TestGopaxFinallyCloseExceptionIsolated(unittest.IsolatedAsyncioTestCase):
    """G5: finally redis_writer.close() 예외 격리 (Codex 리뷰 #4)."""

    async def test_close_exception_does_not_propagate(self):
        """close() 예외 → log only, _run_one_session 외부로 전파 X."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        # subscribe 직후 stop_event set
        async def stop_after_subscribe(payload):
            client._stop_event.set()

        mock_ws.send = AsyncMock(side_effect=stop_after_subscribe)
        mock_ws.recv = AsyncMock(side_effect=asyncio.TimeoutError())

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        async def fake_close(*args, **kwargs):
            raise RuntimeError("close failure simulated")

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._redis_writer, "close", side_effect=fake_close), \
           patch.object(client._db_writer, "close", new=AsyncMock()), \
           patch.object(client._alert_evaluator, "schedule"), \
           patch.object(client._alert_evaluator, "close", new=AsyncMock()):
            # close 예외는 _run_one_session 외부로 전파되지 않음 (격리)
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # ws cleanup도 정상
        self.assertIsNone(client._ws)


# ===========================================================================
# G6a GopaxDbWriter — 1s window debounce + insert_source_rate_if_changed
# ===========================================================================


class TestGopaxDbWriterDebounce(unittest.IsolatedAsyncioTestCase):
    """G6a: 1s window debounce — window 안 여러 tick → last만 flush."""

    async def test_multiple_ticks_in_window_only_last_flushed(self):
        """window 안 3 tick → 마지막 tick만 helper 호출."""
        from app.crawlers.usdt_ws.gopax import GopaxDbWriter

        writer = GopaxDbWriter(window_sec=0.05)
        captured = []

        def fake_sync_write(tick):
            captured.append(tick)

        with patch.object(GopaxDbWriter, "_sync_db_write", side_effect=fake_sync_write):
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 2.0, "timestamp_ms": 2})
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 3.0, "timestamp_ms": 3})
            await asyncio.sleep(0.15)

        # 1회만 helper 호출 (debounce)
        self.assertEqual(len(captured), 1)
        # 마지막 tick (rate=3.0) flush
        self.assertEqual(captured[0]["rate"], 3.0)

    async def test_window_expires_then_flushes(self):
        """window 만료 후 helper 호출 검증."""
        from app.crawlers.usdt_ws.gopax import GopaxDbWriter

        writer = GopaxDbWriter(window_sec=0.05)
        captured = []

        def fake_sync_write(tick):
            captured.append(tick)

        with patch.object(GopaxDbWriter, "_sync_db_write", side_effect=fake_sync_write):
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            # window 만료 전 helper 호출 0
            await asyncio.sleep(0.02)
            self.assertEqual(len(captured), 0)
            # window 만료 후 helper 호출 1
            await asyncio.sleep(0.06)
            self.assertEqual(len(captured), 1)


class TestGopaxDbWriterRacePrevention(unittest.IsolatedAsyncioTestCase):
    """G6a: write 후 _pending_tick 잔존 시 finally에서 새 timer 예약 (Bithumb race 방지).

    write 진행 중 새 tick 도착 시 race 방지 동작 검증 — _pending_tick을 직접 갱신
    하는 방식 (event-based sync는 asyncio.to_thread 안에서 thread-safety 문제).
    """

    async def test_pending_tick_after_write_schedules_next_timer(self):
        """write 끝난 후 _pending_tick 잔존 → finally에서 새 timer 자동 예약."""
        from app.crawlers.usdt_ws.gopax import GopaxDbWriter

        writer = GopaxDbWriter(window_sec=0.02)
        captured = []
        call_count = {"n": 0}

        def fake_sync_write(tick):
            captured.append(tick)
            call_count["n"] += 1
            # 첫 호출 후 _pending_tick에 새 tick "잔존" 시뮬레이션 (race)
            if call_count["n"] == 1:
                writer._pending_tick = {
                    "source": "gopax", "asset": "usdt-krw", "rate": 99.0, "timestamp_ms": 99,
                }

        with patch.object(GopaxDbWriter, "_sync_db_write", side_effect=fake_sync_write):
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            # 첫 timer + race-prevention 두 번째 timer 둘 다 fire될 시간 대기
            await asyncio.sleep(0.15)

        # 2회 helper 호출 — 첫 timer (rate=1.0) + race-prevention (rate=99.0)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["rate"], 1.0)
        self.assertEqual(captured[1]["rate"], 99.0)


class TestGopaxDbWriterFailureIsolation(unittest.IsolatedAsyncioTestCase):
    """G6a: DB exception → log only, WS session 영향 X."""

    async def test_db_exception_logged_but_not_propagated(self):
        from app.crawlers.usdt_ws.gopax import GopaxDbWriter

        writer = GopaxDbWriter(window_sec=0.05)

        def fail_sync_write(tick):
            raise RuntimeError("DB connection lost simulated")

        with patch.object(GopaxDbWriter, "_sync_db_write", side_effect=fail_sync_write), \
             self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="WARNING") as cm:
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            await asyncio.sleep(0.15)

        # 예외 propagate X — log only
        self.assertTrue(any("DB write failed" in m for m in cm.output))


class TestGopaxDbWriterClose(unittest.IsolatedAsyncioTestCase):
    """G6a: close() — timer cancel + pending tick 즉시 flush (Bithumb mirror)."""

    async def test_close_flushes_pending_immediately(self):
        """schedule 후 close → pending tick 즉시 flush (window 무시)."""
        from app.crawlers.usdt_ws.gopax import GopaxDbWriter

        writer = GopaxDbWriter(window_sec=10.0)  # 큰 window
        captured = []

        def fake_sync_write(tick):
            captured.append(tick)

        with patch.object(GopaxDbWriter, "_sync_db_write", side_effect=fake_sync_write):
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 5.0, "timestamp_ms": 5})
            # window (10s) 만료 전 close — pending 즉시 flush
            await writer.close()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["rate"], 5.0)

    async def test_close_with_no_pending_is_noop(self):
        """close → pending 없으면 helper 호출 0."""
        from app.crawlers.usdt_ws.gopax import GopaxDbWriter

        writer = GopaxDbWriter()
        with patch.object(GopaxDbWriter, "_sync_db_write") as mock_write:
            await writer.close()
        mock_write.assert_not_called()


class TestGopaxDbWriterUsesToThread(unittest.IsolatedAsyncioTestCase):
    """G6a: sync DB write가 asyncio.to_thread로 격리."""

    async def test_to_thread_called_with_sync_write(self):
        from app.crawlers.usdt_ws.gopax import GopaxDbWriter

        writer = GopaxDbWriter(window_sec=0.05)
        with patch(
            "app.crawlers.usdt_ws.gopax.asyncio.to_thread",
            new=AsyncMock(return_value=None),
        ) as mock_to_thread:
            writer.schedule({"source": "gopax", "asset": "usdt-krw", "rate": 1.0, "timestamp_ms": 1})
            await asyncio.sleep(0.1)

        # to_thread 1회 호출 (debounce 만료 후)
        self.assertEqual(mock_to_thread.call_count, 1)


class TestGopaxRunOneSessionDbFanout(unittest.IsolatedAsyncioTestCase):
    """G6a: valid tick → _db_writer.schedule 호출 검증."""

    async def test_valid_tick_triggers_db_schedule(self):
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        ticker_message = json.dumps({
            "n": "TickerEvent",
            "o": {"USDT-KRW": {"last": 1490.5, "lastTraded": 1779106625946}},
        })

        recv_count = {"n": 0}
        async def fake_recv():
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                return ticker_message
            client._stop_event.set()
            return ticker_message

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._redis_writer, "schedule"), \
           patch.object(client._redis_writer, "close", new=AsyncMock()), \
           patch.object(client._db_writer, "schedule") as mock_db_schedule, \
           patch.object(client._db_writer, "close", new=AsyncMock()), \
           patch.object(client._alert_evaluator, "schedule"), \
           patch.object(client._alert_evaluator, "close", new=AsyncMock()):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # 2 valid tick → DB schedule 2회
        self.assertEqual(mock_db_schedule.call_count, 2)


class TestGopaxInvalidFrameNoDbSchedule(unittest.IsolatedAsyncioTestCase):
    """G6a (Codex 보강): invalid frame → DB schedule 호출 0."""

    async def test_invalid_json_no_db_schedule(self):
        """invalid JSON → tick=None → _db_writer.schedule 호출 0."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        recv_count = {"n": 0}
        async def fake_recv():
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                client._stop_event.set()
                return "this is not valid json"

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._redis_writer, "close", new=AsyncMock()), \
           patch.object(client._db_writer, "schedule") as mock_db_schedule, \
           patch.object(client._db_writer, "close", new=AsyncMock()), \
           patch.object(client._alert_evaluator, "schedule"), \
           patch.object(client._alert_evaluator, "close", new=AsyncMock()):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # invalid frame → DB schedule 호출 0
        mock_db_schedule.assert_not_called()


class TestGopaxPrimusFrameNoDbSchedule(unittest.IsolatedAsyncioTestCase):
    """G6a (Codex 보강): Primus ping → DB schedule 호출 0."""

    async def test_primus_ping_no_db_schedule(self):
        """Primus ping → _handle_primus_ping만, _db_writer.schedule 호출 0."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        recv_count = {"n": 0}
        async def fake_recv():
            recv_count["n"] += 1
            if recv_count["n"] == 1:
                client._stop_event.set()
                return "primus::ping::1234"

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._redis_writer, "close", new=AsyncMock()), \
           patch.object(client._db_writer, "schedule") as mock_db_schedule, \
           patch.object(client._db_writer, "close", new=AsyncMock()), \
           patch.object(client._alert_evaluator, "schedule"), \
           patch.object(client._alert_evaluator, "close", new=AsyncMock()):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # Primus ping → DB schedule 0
        mock_db_schedule.assert_not_called()


class TestGopaxFinallyDbCloseOrder(unittest.IsolatedAsyncioTestCase):
    """G6a: finally close 순서 — DB close 먼저 → Redis close 마지막 (Bithumb mirror)."""

    async def test_db_close_before_redis_close(self):
        """finally 안 _db_writer.close()가 _redis_writer.close() 보다 먼저 호출."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=asyncio.TimeoutError())

        async def stop_after_subscribe(payload):
            client._stop_event.set()

        mock_ws.send = AsyncMock(side_effect=stop_after_subscribe)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        close_order = []

        async def fake_db_close():
            close_order.append("db")

        async def fake_redis_close(*args, **kwargs):
            close_order.append("redis")

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._db_writer, "close", side_effect=fake_db_close), \
           patch.object(client._alert_evaluator, "close", new=AsyncMock()), \
           patch.object(client._redis_writer, "close", side_effect=fake_redis_close):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        self.assertEqual(close_order, ["db", "redis"])


# ===========================================================================
# G6b GopaxRestFallbackController — degraded threshold trigger + REST probe +
# Redis/DB fanout + scheduled_probe_count
# ===========================================================================


class TestTickerFreshnessDegradedTriggersFallback(unittest.IsolatedAsyncioTestCase):
    """G6b: _set_ticker_freshness_status("degraded") → schedule_probe 호출 (Coinone C6b mirror).

    의미 반전 (G4 → G6b): G4 시점은 status 갱신만, G6b부터 schedule_probe hook 진입.
    """

    def test_degraded_transition_schedules_probe(self):
        """warning → degraded → schedule_probe("ticker_degraded") 호출."""
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        client = GopaxWsClient()
        client._set_ticker_freshness_status("warning")

        with patch.object(GopaxRestFallbackController, "schedule_probe") as mock_schedule:
            client._set_ticker_freshness_status("degraded")

        self.assertEqual(client._ticker_freshness_status, "degraded")
        mock_schedule.assert_called_once()
        # reason kwarg 또는 args 확인
        call = mock_schedule.call_args
        if call.kwargs:
            self.assertEqual(call.kwargs.get("reason"), "ticker_degraded")
        else:
            self.assertEqual(call.args[0] if call.args else None, "ticker_degraded")


class TestNormalRecoveryResetsCooldown(unittest.IsolatedAsyncioTestCase):
    """G6b: warning/degraded → normal 복귀 시 reset_cooldown 호출."""

    def test_warning_to_normal_resets_cooldown(self):
        """warning → normal 복귀 → reset_cooldown 호출."""
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        client = GopaxWsClient()
        client._set_ticker_freshness_status("warning")

        with patch.object(GopaxRestFallbackController, "reset_cooldown") as mock_reset:
            client._set_ticker_freshness_status("normal")

        self.assertEqual(client._ticker_freshness_status, "normal")
        mock_reset.assert_called_once()

    def test_degraded_to_normal_resets_cooldown(self):
        """degraded → normal 복귀 → reset_cooldown 호출 (Codex outage recovery)."""
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        client = GopaxWsClient()
        client._set_ticker_freshness_status("warning")
        client._set_ticker_freshness_status("degraded")

        with patch.object(GopaxRestFallbackController, "reset_cooldown") as mock_reset:
            client._set_ticker_freshness_status("normal")

        mock_reset.assert_called_once()


class TestRestFallbackInFlightSkip(unittest.IsolatedAsyncioTestCase):
    """G6b: _in_flight=True 상태에서 schedule_probe → skip."""

    def test_in_flight_skips_schedule(self):
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        controller = GopaxRestFallbackController(
            redis_writer=MagicMock(),
            db_writer=MagicMock(),
            alert_evaluator=MagicMock(),
        )
        controller._in_flight = True
        controller.schedule_probe("test")
        # in-flight skip → counter 미증가
        self.assertEqual(controller.scheduled_probe_count, 0)


class TestRestFallbackCooldownSkip(unittest.IsolatedAsyncioTestCase):
    """G6b: cooldown 활성 상태 schedule_probe → skip."""

    def test_cooldown_skips_schedule(self):
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        controller = GopaxRestFallbackController(
            redis_writer=MagicMock(),
            db_writer=MagicMock(),
            alert_evaluator=MagicMock(),
        )
        controller._cooldown_until = time.time() + 1000.0  # 미래 cooldown 활성
        controller.schedule_probe("test")
        self.assertEqual(controller.scheduled_probe_count, 0)


class TestRestFallbackProbeFanout(unittest.IsolatedAsyncioTestCase):
    """G6b: REST probe success → Redis + DB schedule 호출."""

    async def test_probe_success_triggers_redis_and_db_schedule(self):
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        redis_mock = MagicMock()
        db_mock = MagicMock()
        controller = GopaxRestFallbackController(
            redis_writer=redis_mock,
            db_writer=db_mock,
            alert_evaluator=MagicMock(),
        )

        tick = {"source": "gopax", "asset": "usdt-krw", "rate": 1490.5, "timestamp_ms": 1779106625946}

        with patch.object(
            GopaxRestFallbackController, "_fetch_gopax_tick",
            return_value=tick,
        ):
            controller.schedule_probe("test")
            if controller._pending_task is not None:
                await controller._pending_task

        redis_mock.schedule.assert_called_once_with(tick)
        db_mock.schedule.assert_called_once_with(tick)


class TestRestFallbackProbeFailure(unittest.IsolatedAsyncioTestCase):
    """G6b: REST probe None → Redis/DB schedule 0."""

    async def test_probe_returns_none_no_fanout(self):
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        redis_mock = MagicMock()
        db_mock = MagicMock()
        controller = GopaxRestFallbackController(
            redis_writer=redis_mock,
            db_writer=db_mock,
            alert_evaluator=MagicMock(),
        )

        with patch.object(
            GopaxRestFallbackController, "_fetch_gopax_tick",
            return_value=None,
        ):
            controller.schedule_probe("test")
            if controller._pending_task is not None:
                await controller._pending_task

        # None probe → fanout 0
        redis_mock.schedule.assert_not_called()
        db_mock.schedule.assert_not_called()


class TestRestFallbackScheduledProbeCount(unittest.IsolatedAsyncioTestCase):
    """G6b (Codex 보강): _scheduled_probe_count counter — schedule 성공 시점만 +1."""

    def _make_controller(self):
        from app.crawlers.usdt_ws.gopax import GopaxRestFallbackController
        return GopaxRestFallbackController(
            redis_writer=MagicMock(),
            db_writer=MagicMock(),
            alert_evaluator=MagicMock(),
        )

    def test_initial_value_zero(self):
        controller = self._make_controller()
        self.assertEqual(controller.scheduled_probe_count, 0)

    async def test_increments_on_successful_schedule(self):
        """schedule_probe() 성공 → +1."""
        controller = self._make_controller()

        async def fake_run_probe(reason):
            return None

        with patch.object(controller, "_run_probe", side_effect=fake_run_probe):
            controller.schedule_probe("test")
            self.assertEqual(controller.scheduled_probe_count, 1)
            if controller._pending_task is not None:
                await controller._pending_task

    def test_no_increment_on_in_flight_skip(self):
        controller = self._make_controller()
        controller._in_flight = True
        controller.schedule_probe("test")
        self.assertEqual(controller.scheduled_probe_count, 0)

    def test_no_increment_on_cooldown_skip(self):
        controller = self._make_controller()
        controller._cooldown_until = time.time() + 1000.0
        controller.schedule_probe("test")
        self.assertEqual(controller.scheduled_probe_count, 0)


# ===========================================================================
# G6b fetch_gopax_usdt_tick helper — ISO timestamp parsing + guards
# ===========================================================================


class TestFetchGopaxUsdtTickHappyPath(unittest.TestCase):
    """G6b: production curl 실측 응답 (Codex) → normalized tick."""

    def test_happy_path_parses_iso_timestamp(self):
        """{"price": 1487, "time": "2026-05-21T10:53:14.604Z"} → normalized tick."""
        from app.crawlers.usdt_sources import fetch_gopax_usdt_tick

        fake_payload = {"price": 1487, "time": "2026-05-21T10:53:14.604Z"}
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=fake_payload)

        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            tick = fetch_gopax_usdt_tick()

        self.assertIsNotNone(tick)
        self.assertEqual(tick["source"], "gopax")
        self.assertEqual(tick["asset"], "usdt-krw")
        self.assertEqual(tick["rate"], 1487.0)
        # ISO "2026-05-21T10:53:14.604Z" → epoch ms (UTC)
        # datetime(2026,5,21,10,53,14,604000,tz=UTC).timestamp() * 1000
        from datetime import datetime
        expected_ms = int(
            datetime.fromisoformat("2026-05-21T10:53:14.604+00:00").timestamp() * 1000
        )
        self.assertEqual(tick["timestamp_ms"], expected_ms)


class TestFetchGopaxUsdtTickIsoTimestamp(unittest.TestCase):
    """G6b (Codex 보강): ISO 8601 Z suffix 정확한 epoch ms 변환."""

    def test_iso_z_suffix_to_epoch_ms(self):
        """'2026-05-21T10:53:14.604Z' → 정확한 epoch ms."""
        from app.crawlers.usdt_sources import fetch_gopax_usdt_tick

        fake_payload = {"price": 1500.0, "time": "2026-05-21T10:53:14.604Z"}
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=fake_payload)

        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            tick = fetch_gopax_usdt_tick()

        # 2026-05-21T10:53:14.604Z UTC → epoch ms
        # ms portion (604) 정확 보존 검증
        self.assertEqual(tick["timestamp_ms"] % 1000, 604)


class TestFetchGopaxUsdtTickGuards(unittest.TestCase):
    """G6b: guard 검증 — rate<=0 / time missing / invalid ISO / request 실패."""

    def test_rate_zero_returns_none(self):
        from app.crawlers.usdt_sources import fetch_gopax_usdt_tick
        fake_payload = {"price": 0, "time": "2026-05-21T10:53:14.604Z"}
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=fake_payload)
        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            self.assertIsNone(fetch_gopax_usdt_tick())

    def test_time_missing_returns_none(self):
        from app.crawlers.usdt_sources import fetch_gopax_usdt_tick
        fake_payload = {"price": 1487}
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=fake_payload)
        with patch("app.crawlers.usdt_sources.requests.get", return_value=mock_response):
            self.assertIsNone(fetch_gopax_usdt_tick())

    def test_request_exception_returns_none(self):
        from app.crawlers.usdt_sources import fetch_gopax_usdt_tick
        import requests as req_mod
        with patch(
            "app.crawlers.usdt_sources.requests.get",
            side_effect=req_mod.RequestException("simulated"),
        ):
            self.assertIsNone(fetch_gopax_usdt_tick())


class TestGopaxFinallyFallbackCloseFirst(unittest.IsolatedAsyncioTestCase):
    """G7: finally close 순서 — fallback → DB → Alert → Redis (Coinone C7 mirror)."""

    async def test_fallback_close_before_db_alert_and_redis(self):
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        async def stop_after_subscribe(payload):
            client._stop_event.set()

        mock_ws.send = AsyncMock(side_effect=stop_after_subscribe)
        mock_ws.recv = AsyncMock(side_effect=asyncio.TimeoutError())

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        close_order = []

        async def fake_fallback_close():
            close_order.append("fallback")

        async def fake_db_close():
            close_order.append("db")

        async def fake_alert_close(*args, **kwargs):
            close_order.append("alert")

        async def fake_redis_close(*args, **kwargs):
            close_order.append("redis")

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._fallback_controller, "close", side_effect=fake_fallback_close), \
           patch.object(client._db_writer, "close", side_effect=fake_db_close), \
           patch.object(client._alert_evaluator, "close", side_effect=fake_alert_close), \
           patch.object(client._redis_writer, "close", side_effect=fake_redis_close):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        self.assertEqual(close_order, ["fallback", "db", "alert", "redis"])


# ===========================================================================
# G7 — UsdtAlertEvaluator wiring + AlertObservation schedule (Coinone C7 mirror)
# ===========================================================================


class TestGopaxValidTickSchedulesAlertObservation(unittest.IsolatedAsyncioTestCase):
    """G7 acceptance: WS valid tick → AlertObservation(kind="tick") schedule."""

    async def test_valid_tick_schedules_alert_observation_with_kind_tick(self):
        from app.notifications.alert_evaluator import AlertObservation

        client = GopaxWsClient()
        captured = []
        client._alert_evaluator.schedule = MagicMock(
            side_effect=lambda obs: captured.append(obs),
        )
        # Redis/DB schedule mock — 실제 task 생성/DB 접근 회피
        client._redis_writer.schedule = MagicMock()
        client._db_writer.schedule = MagicMock()

        # _handle_message가 valid tick을 return하도록 mock
        sample_tick = {
            "source": "gopax",
            "asset": "usdt-krw",
            "rate": 1487.5,
            "timestamp_ms": 1700000000000,
        }
        client._handle_message = MagicMock(return_value=sample_tick)

        mock_ws = AsyncMock()
        send_count = [0]

        async def send_side_effect(payload):
            send_count[0] += 1

        mock_ws.send = AsyncMock(side_effect=send_side_effect)

        # 1회 frame → stop
        recv_count = [0]

        async def recv_side_effect():
            recv_count[0] += 1
            if recv_count[0] == 1:
                return '{"n":"TickerEvent","o":{}}'
            client._stop_event.set()
            raise asyncio.TimeoutError()

        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)
        # primus ping 매칭 회피 — frame은 plain dict ticker로 처리
        client._is_primus_ping = MagicMock(return_value=False)

        mock_connect_ctx = AsyncMock()
        mock_connect_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ), patch.object(client._alert_evaluator, "close", new=AsyncMock()):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # valid tick 1회 → AlertObservation schedule 1회 (kind="tick")
        self.assertEqual(len(captured), 1)
        obs = captured[0]
        self.assertIsInstance(obs, AlertObservation)
        self.assertEqual(obs.source, "gopax")
        self.assertEqual(obs.asset, "usdt-krw")
        self.assertEqual(obs.rate, 1487.5)
        self.assertEqual(obs.timestamp_ms, 1700000000000)
        self.assertEqual(obs.kind, "tick")
        # invariant: Redis/DB도 같이 schedule (fanout 일관)
        client._redis_writer.schedule.assert_called_once_with(sample_tick)
        client._db_writer.schedule.assert_called_once_with(sample_tick)


class TestGopaxRestProbeSuccessSchedulesAlertObservation(unittest.IsolatedAsyncioTestCase):
    """G7 acceptance: REST probe success → AlertObservation(kind="rest_probe") schedule."""

    async def test_probe_success_schedules_alert_observation_with_kind_rest_probe(self):
        from app.notifications.alert_evaluator import AlertObservation

        client = GopaxWsClient()
        captured = []
        client._alert_evaluator.schedule = MagicMock(
            side_effect=lambda obs: captured.append(obs),
        )
        # downstream writer mock — 실제 task/DB 접근 회피
        client._redis_writer.schedule = MagicMock()
        client._db_writer.schedule = MagicMock()

        # fetch_gopax_usdt_tick 성공 시뮬레이션
        sample_tick = {
            "source": "gopax",
            "asset": "usdt-krw",
            "rate": 1488.2,
            "timestamp_ms": 1700000001000,
        }
        with patch(
            "app.crawlers.usdt_sources.fetch_gopax_usdt_tick",
            return_value=sample_tick,
        ):
            client._fallback_controller.schedule_probe(reason="ticker_degraded")
            pending = client._fallback_controller._pending_task
            self.assertIsNotNone(pending)
            await pending

        # probe success 1회 → AlertObservation schedule 1회 (kind="rest_probe")
        self.assertEqual(len(captured), 1)
        obs = captured[0]
        self.assertIsInstance(obs, AlertObservation)
        self.assertEqual(obs.source, "gopax")
        self.assertEqual(obs.asset, "usdt-krw")
        self.assertEqual(obs.rate, 1488.2)
        self.assertEqual(obs.timestamp_ms, 1700000001000)
        self.assertEqual(obs.kind, "rest_probe")
        # invariant: Redis/DB도 같이 schedule (fanout 일관)
        client._redis_writer.schedule.assert_called_once_with(sample_tick)
        client._db_writer.schedule.assert_called_once_with(sample_tick)


# ===========================================================================
# PR 2e — _summary_log_loop 60s cycle 10 fields emit (Coinone PR 2d 1:1 mirror)
# ===========================================================================


class TestGopaxSummaryLogLoop(unittest.IsolatedAsyncioTestCase):
    """PR 2e — Gopax summary log emit 검증 (10 fields, 2-signal status + 2 counters).

    Gopax는 Coinone형 state shape (connection_status + ticker_freshness_status) +
    Bithumb counter scope (saturation + probe 둘 다) union — Coinone PR 2d 1:1 mirror.
    """

    async def test_emit_format_contains_all_10_metrics(self):
        """emit log에 state 8 + counter 2 = 10 metric key=value 포맷 포함."""
        client = GopaxWsClient()
        client._liveness.frame_count_total = 100
        client._liveness.last_tick_at = time.time() - 2.0
        client._liveness.last_heartbeat_at = time.time() - 30.0
        client._liveness.max_frame_gap_sec = 8.2

        async def fake_sleep(_):
            client._stop_event.set()

        with patch("app.crawlers.usdt_ws.gopax.asyncio.sleep", side_effect=fake_sleep), \
             self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "metrics" in m), None)
        self.assertIsNotNone(metric_log, "metric INFO log 없음")
        for keyword in [
            "frames_per_min=",
            "last_tick_age=",
            "last_heartbeat_age=",
            "max_frame_gap=",
            "connection_status=",                # 2-signal (Coinone 동일)
            "ticker_freshness_status=",          # 2-signal (Coinone 동일)
            "reconnect_attempts=",
            "status_transitions=",
            "redis_saturation_count=",           # PR 2e (G5 land)
            "fallback_probe_scheduled_count=",   # PR 2e (G6b land)
        ]:
            self.assertIn(keyword, metric_log, f"metric key 누락: {keyword}")

        # Gopax-specific schema: 1-dim status= 형태 부재 (2-signal이라 status= 단독 없음).
        # 'connection_status=' / 'ticker_freshness_status='에는 'status='가 포함되므로
        # 정확히 ' status=' (공백 prefix)로 1-dim status field 부재 확인.
        self.assertNotIn(" status=", metric_log)

    async def test_sentinel_for_none_age(self):
        """last_tick_at / last_heartbeat_at이 None이면 -1.0 numeric sentinel emit."""
        client = GopaxWsClient()
        self.assertIsNone(client._liveness.last_tick_at)
        self.assertIsNone(client._liveness.last_heartbeat_at)

        async def fake_sleep(_):
            client._stop_event.set()

        with patch("app.crawlers.usdt_ws.gopax.asyncio.sleep", side_effect=fake_sleep), \
             self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
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
        client = GopaxWsClient()
        client._liveness.frame_count_total = 0

        time_values = iter([100.0, 160.0])

        async def fake_sleep(duration):
            client._liveness.frame_count_total = 30
            client._stop_event.set()

        with patch("app.crawlers.usdt_ws.gopax.time.time", side_effect=lambda: next(time_values)), \
             patch("app.crawlers.usdt_ws.gopax.asyncio.sleep", side_effect=fake_sleep), \
             self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "frames_per_min=" in m), None)
        self.assertIsNotNone(metric_log)
        self.assertIn("frames_per_min=30", metric_log)

    async def test_cancelled_silently_returns(self):
        """CancelledError 시 silently return (no exception propagate)."""
        client = GopaxWsClient()

        async def fake_sleep(_):
            raise asyncio.CancelledError()

        with patch("app.crawlers.usdt_ws.gopax.asyncio.sleep", side_effect=fake_sleep):
            await asyncio.wait_for(client._summary_log_loop(), timeout=1.0)

    async def test_stop_event_before_first_emit_skips_emit(self):
        """stop_event 사전 set 시 emit 0 (loop entry 못 함). assertNoLogs (Python 3.10+)."""
        client = GopaxWsClient()
        client._stop_event.set()

        with self.assertNoLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO"):
            await asyncio.wait_for(client._summary_log_loop(), timeout=1.0)


class TestGopaxSummaryLogEmitsCounters(unittest.IsolatedAsyncioTestCase):
    """PR 2e — _summary_log_loop counter field emit value 검증 (PropertyMock pattern).

    Coinone PR 2d / Korbit PR 2a / Bithumb PR 2c / Upbit PR 2b 패턴 mirror —
    counter property mock으로 emit log 안 numeric 값 직접 검증.
    """

    async def test_emit_contains_saturation_count_value(self):
        """redis_saturation_count=N emit 검증 (PropertyMock)."""
        client = GopaxWsClient()

        async def fake_sleep(_):
            client._stop_event.set()

        with patch.object(
            GopaxRedisWriter, "saturation_count",
            new_callable=PropertyMock, return_value=13,
        ), patch("app.crawlers.usdt_ws.gopax.asyncio.sleep", side_effect=fake_sleep), \
           self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "metrics" in m), None)
        self.assertIsNotNone(metric_log)
        self.assertIn("redis_saturation_count=13", metric_log)

    async def test_emit_contains_scheduled_probe_count_value(self):
        """fallback_probe_scheduled_count=N emit 검증 (PropertyMock)."""
        client = GopaxWsClient()

        async def fake_sleep(_):
            client._stop_event.set()

        with patch.object(
            GopaxRestFallbackController, "scheduled_probe_count",
            new_callable=PropertyMock, return_value=4,
        ), patch("app.crawlers.usdt_ws.gopax.asyncio.sleep", side_effect=fake_sleep), \
           self.assertLogs("exchange_rate.crawler.usdt_ws.gopax", level="INFO") as cm:
            await asyncio.wait_for(client._summary_log_loop(), timeout=2.0)

        metric_log = next((m for m in cm.output if "metrics" in m), None)
        self.assertIsNotNone(metric_log)
        self.assertIn("fallback_probe_scheduled_count=4", metric_log)


class TestGopaxStartCancelsSummaryTask(unittest.IsolatedAsyncioTestCase):
    """PR 2e — start() finally에서 summary_task cancel/await 검증.

    Coinone PR 2d / Korbit/Bithumb start() finally cancel pattern mirror
    (reconnect loop 예외와 독립).
    """

    async def test_start_cancels_summary_task_on_stop(self):
        """start 종료 시 summary_task가 cancel + await됨."""
        client = GopaxWsClient()
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
