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
import unittest
from unittest.mock import AsyncMock, patch

from websockets.exceptions import ConnectionClosed

from app import config, scheduler
from app.crawlers.usdt_ws.gopax import (
    GOPAX_WS_URL,
    GopaxWsClient,
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
    """G3 client — G1 lifecycle + G2 connect/parse + G3 heartbeat timestamp 보유.

    Codex 최종 권고: G4 attribute (_connection_status / _ticker_freshness_status /
    _reconnect_attempt_count / _liveness)는 본 stage 제외.
    """

    async def test_init_state_g3_scope(self):
        """__init__ 직후: G1 + G2 + G3 attribute만 보유, G4 attribute 부재."""
        client = GopaxWsClient()
        # G1 state
        self.assertFalse(client._running)
        self.assertFalse(client._stop_event.is_set())
        # G2 state
        self.assertIsNone(client._ws)
        self.assertFalse(client._first_tick_logged)
        # G3 state — heartbeat timestamp (Codex 권고 — G4 liveness에서 활용)
        self.assertIsNone(client._last_heartbeat_at)
        # G4 attribute 부재 검증 (선반영 회피)
        self.assertFalse(hasattr(client, "_connection_status"))
        self.assertFalse(hasattr(client, "_ticker_freshness_status"))
        self.assertFalse(hasattr(client, "_reconnect_attempt_count"))
        self.assertFalse(hasattr(client, "_liveness"))
        self.assertFalse(hasattr(client, "_status_transition_count"))

    async def test_start_stop_lifecycle(self):
        """start() → _running=True → stop() → _running=False.

        G2 start()는 _run_one_session 1회 호출 후 stop_event 대기. 본 test는
        _run_one_session을 mock으로 즉시 return 처리.
        """
        client = GopaxWsClient()

        async def stop_after_start():
            await asyncio.sleep(0.01)
            self.assertTrue(client._running)
            await client.stop()

        # _run_one_session은 mock으로 즉시 return (network 호출 회피)
        async def fake_run_session():
            return

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
# G1 Module scope guard: GOPAX_WS_URL 노출 + G2~ symbol 부재
# ---------------------------------------------------------------------------


class TestScopeGuard(unittest.TestCase):
    """G3 module-level scope guard — G4~G7 미구현 symbol 부재 검증."""

    def test_gopax_ws_url_exported(self):
        """G1: GOPAX_WS_URL constant module-level 노출."""
        self.assertEqual(GOPAX_WS_URL, "wss://wsapi.gopax.co.kr")

    def test_no_g4_g7_symbols_at_module_level(self):
        """G3: G4~G7에서 추가될 symbol module-level 부재 검증.

        G3에서 Primus ping/pong helpers는 GopaxWsClient class method로 추가됨
        (module-level X). G4 (Liveness/reconnect), G5 (Redis writer), G6a (DB
        writer), G6b (REST helper + fallback controller), G7 (alert)은 module-level
        부재해야 함 (선제 작성 회피).
        """
        from app.crawlers.usdt_ws import gopax as gopax_module

        forbidden_attrs = [
            # G4 — Liveness + reconnect
            "RECONNECT_BACKOFF_SEQ",
            "STALE_AFTER_SEC",
            "PING_INTERVAL_SEC",
            "TICKER_FRESHNESS_WARNING_SEC",
            # G5 — Redis writer
            "GopaxRedisWriter",
            "MAX_PENDING_WRITES",
            # G6a — DB writer
            "GopaxDbWriter",
            "DB_WRITE_WINDOW_SEC",
            # G6b — REST helper + fallback
            "GopaxRestFallbackController",
            "fetch_gopax_usdt_tick",
            "FALLBACK_COOLDOWN_SEC",
        ]
        for attr in forbidden_attrs:
            self.assertFalse(
                hasattr(gopax_module, attr),
                f"G3 scope 위반: {attr} symbol이 module-level에 존재함 — G4~G7 stage에서 추가 예정",
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
        """gopax.py module docstring에 'Production activation 제약' + idle 의미 명시."""
        import app.crawlers.usdt_ws.gopax as gopax_module
        doc = gopax_module.__doc__ or ""
        self.assertIn("Production activation 제약", doc)
        self.assertIn("G3", doc)
        self.assertIn("G4", doc)
        # 핵심 제약 — 30s 후 disconnect
        self.assertIn("30", doc)
        # Codex 정정 — silent termination이 아니라 silent idle/stuck 명시
        self.assertIn("idle", doc)
        # "silent termination이 아니라"를 분명히 적었는지 검증 (정확한 의미 잠금)
        self.assertIn("silent termination이 아니", doc)

    def test_class_docstring_warns_activation_constraint(self):
        """GopaxWsClient class docstring에도 activation 제약 명시."""
        doc = GopaxWsClient.__doc__ or ""
        self.assertIn("Production activation 제약", doc)
        self.assertIn("G3", doc)

    def test_run_one_session_docstring_warns_activation_constraint(self):
        """_run_one_session docstring에 현재 stage 단독 activation 금지 + idle 명시."""
        doc = GopaxWsClient._run_one_session.__doc__ or ""
        # 현재 stage (G3)는 단독 activation 금지 명시. 향후 G4 진입 시 동일 검증
        # 패턴으로 갱신.
        self.assertIn("G3 단독 flag=true", doc)
        # Codex 정정 — silent idle/stuck 명시
        self.assertIn("idle", doc)

    def test_start_docstring_describes_idle_after_session_end(self):
        """start() docstring에 session 종료 후 idle 상태 (running=True 유지) 명시."""
        doc = GopaxWsClient.start.__doc__ or ""
        self.assertIn("idle", doc)
        # silent termination이 아니라 silent idle/stuck
        self.assertIn("silent termination이 아니", doc)


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

        before = client._last_heartbeat_at
        self.assertIsNone(before)

        result = await client._handle_primus_ping(mock_ws, '"primus::ping::1234"')

        self.assertTrue(result)
        mock_ws.send.assert_called_once_with('"primus::pong::1234"')
        # Codex 검토 포인트 #3: heartbeat은 send 성공 시점에만 갱신
        self.assertIsNotNone(client._last_heartbeat_at)
        self.assertIsInstance(client._last_heartbeat_at, float)

    async def test_plain_text_form_pong_sent(self):
        """Plain text ping → plain text pong 송신, True return, heartbeat 갱신."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        result = await client._handle_primus_ping(mock_ws, "primus::ping::5678")

        self.assertTrue(result)
        mock_ws.send.assert_called_once_with("primus::pong::5678")
        self.assertIsNotNone(client._last_heartbeat_at)

    async def test_bytes_form_pong_sent(self):
        """bytes ping → str decode + pong 송신, True return."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        result = await client._handle_primus_ping(mock_ws, b"primus::ping::9999")

        self.assertTrue(result)
        mock_ws.send.assert_called_once_with("primus::pong::9999")
        self.assertIsNotNone(client._last_heartbeat_at)


class TestHandlePrimusPingFailureSessionEnd(unittest.IsolatedAsyncioTestCase):
    """G3 (Codex 정정): pong send 실패 → False return + heartbeat 미갱신.

    _run_one_session에서 False return 시 session 종료해야 함 (G4 reconnect 부재).
    """

    async def test_send_failure_returns_false_and_no_heartbeat(self):
        """ws.send 실패 → False + heartbeat None 유지."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock(side_effect=ConnectionClosed(None, None))

        result = await client._handle_primus_ping(mock_ws, "primus::ping::1234")

        self.assertFalse(result)
        # Codex 검토 포인트 #3: send 실패 시 heartbeat 미갱신
        self.assertIsNone(client._last_heartbeat_at)


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

    async def test_pong_send_failure_ends_session(self):
        """Codex 정정: pong send 실패 → _run_one_session return (session 종료)."""
        client = GopaxWsClient()

        mock_ws = AsyncMock()
        # subscribe send는 성공, 그 후 pong send 실패
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

        with patch(
            "app.crawlers.usdt_ws.gopax.websockets.connect",
            return_value=mock_connect_ctx,
        ):
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)

        # session 종료 — ws cleanup
        self.assertIsNone(client._ws)
        # heartbeat 미갱신
        self.assertIsNone(client._last_heartbeat_at)


class TestG3ActivationConstraint(unittest.TestCase):
    """G3 (Codex 강조): G3 land 후에도 production env false 유지 docstring 명시.

    G4 reconnect/liveness 부재라 pong send 실패 또는 ConnectionClosed 시 여전히
    silent idle/stuck. docstring으로 이 제약 잠금.
    """

    def test_module_docstring_g3_activation_constraint(self):
        """module docstring에 G3 단계도 activation 금지 명시."""
        import app.crawlers.usdt_ws.gopax as gopax_module
        doc = gopax_module.__doc__ or ""
        self.assertIn("G3 단계도 유지", doc)
        # G4 land 후에만 activation 명시
        self.assertIn("G4", doc)
        # silent idle 여전 명시
        self.assertIn("idle", doc)

    def test_class_docstring_g3_activation_constraint(self):
        """class docstring에도 G3 단계 유지 명시."""
        doc = GopaxWsClient.__doc__ or ""
        self.assertIn("G3 단계도 유지", doc)


class TestG2StartIdleAfterSessionEnd(unittest.IsolatedAsyncioTestCase):
    """G2 — _run_one_session 종료 후 start() task는 idle 상태로 남는다 (Codex 권고).

    silent termination이 아니라 silent idle/stuck이라는 사실을 동작으로 잠근다.
    """

    async def test_session_end_keeps_task_idle_with_running_true(self):
        """_run_one_session 즉시 return → start() task는 done 아님, _running=True 유지.

        stop() 호출 후에만 task done + _running=False.
        """
        client = GopaxWsClient()

        # _run_one_session 즉시 return (network 없이 session 즉시 종료)
        async def fake_run_session():
            return

        with patch.object(client, "_run_one_session", side_effect=fake_run_session):
            start_task = asyncio.create_task(client.start())
            # _run_one_session 종료 + stop_event.wait() 진입 보장
            await asyncio.sleep(0.05)

            # ⚠️ 핵심 검증: silent idle 상태
            self.assertFalse(start_task.done(), "session 종료 후 task가 done됨 (idle 유지 expected)")
            self.assertTrue(client._running, "session 종료 후 _running=False (idle 상태에서 True 유지 expected)")
            self.assertFalse(client._stop_event.is_set())

            # stop() 호출 → idle wait 풀림 → task done
            await client.stop()
            await asyncio.wait_for(start_task, timeout=1.0)
            self.assertTrue(start_task.done())
            self.assertFalse(client._running)


if __name__ == "__main__":
    unittest.main()
