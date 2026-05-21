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
    """G4 client — G1~G3 attribute + G4 liveness/2-signal status 보유.

    Codex 권고 (G4 dual-write 유지): _last_heartbeat_at은 G4에서도 유지 (cleanup은
    G5 또는 별도 PR). G5-G7 attribute (writers / fallback / alert)는 본 stage 제외.
    """

    async def test_init_state_g4_scope(self):
        """__init__ 직후: G1~G4 attribute 보유, G5-G7 attribute 부재."""
        client = GopaxWsClient()
        # G1 state
        self.assertFalse(client._running)
        self.assertFalse(client._stop_event.is_set())
        # G2 state
        self.assertIsNone(client._ws)
        self.assertFalse(client._first_tick_logged)
        # G3 state — Codex dual-write 유지 (cleanup은 G5/별도 PR)
        self.assertIsNone(client._last_heartbeat_at)
        # G4 state — 2-signal status + liveness
        self.assertEqual(client._connection_status, "normal")
        self.assertEqual(client._ticker_freshness_status, "normal")
        self.assertEqual(client._reconnect_attempt_count, 0)
        self.assertIsNotNone(client._liveness)
        # status_transition_count 6 keys (connection 3 + ticker 3, Coinone/Korbit mirror)
        self.assertEqual(set(client._status_transition_count.keys()), {
            "connection_normal", "connection_reconnecting", "connection_stale",
            "ticker_normal", "ticker_warning", "ticker_degraded",
        })
        for v in client._status_transition_count.values():
            self.assertEqual(v, 0)
        # G5-G7 attribute 부재 검증 (선반영 회피)
        self.assertFalse(hasattr(client, "_redis_writer"))
        self.assertFalse(hasattr(client, "_db_writer"))
        self.assertFalse(hasattr(client, "_fallback_controller"))
        self.assertFalse(hasattr(client, "_alert_evaluator"))

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
# G1 Module scope guard: GOPAX_WS_URL 노출 + G2~ symbol 부재
# ---------------------------------------------------------------------------


class TestScopeGuard(unittest.TestCase):
    """G3 module-level scope guard — G4~G7 미구현 symbol 부재 검증."""

    def test_gopax_ws_url_exported(self):
        """G1: GOPAX_WS_URL constant module-level 노출."""
        self.assertEqual(GOPAX_WS_URL, "wss://wsapi.gopax.co.kr")

    def test_no_g5_g7_symbols_at_module_level(self):
        """G4: G5~G7에서 추가될 symbol module-level 부재 검증.

        G4에서 STALE_AFTER_SEC / TICKER_FRESHNESS_WARNING_SEC / TICKER_FRESHNESS_DEGRADED_SEC /
        RECONNECT_BACKOFF_SEQ / RECONNECT_BACKOFF_TAIL은 허용 (G4 constants).
        G5 (Redis writer), G6a (DB writer), G6b (REST helper + fallback controller),
        G7 (alert)은 module-level 부재해야 함 (선제 작성 회피).
        """
        from app.crawlers.usdt_ws import gopax as gopax_module

        # G4 constants 허용 (검증 — 존재해야 함)
        for attr in [
            "STALE_AFTER_SEC", "TICKER_FRESHNESS_WARNING_SEC",
            "TICKER_FRESHNESS_DEGRADED_SEC", "RECONNECT_BACKOFF_SEQ",
            "RECONNECT_BACKOFF_TAIL",
        ]:
            self.assertTrue(
                hasattr(gopax_module, attr),
                f"G4 constant 누락: {attr}",
            )

        # G5-G7 forbidden
        forbidden_attrs = [
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
            # G7 — Alert
            "AlertObservation",  # 일반 import는 OK이나 module-level alias는 X
        ]
        for attr in forbidden_attrs:
            self.assertFalse(
                hasattr(gopax_module, attr),
                f"G4 scope 위반: {attr} symbol이 module-level에 존재함 — G5~G7 stage에서 추가 예정",
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

        # heartbeat 미갱신 (send 실패라 dual-write 안 됨)
        self.assertIsNone(client._last_heartbeat_at)


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


class TestTickerFreshnessDegradedNoFallback(unittest.TestCase):
    """G4 scope 명시 (Codex 정정): degraded 전이는 status 갱신만, fallback hook은 G6b."""

    def test_degraded_transition_no_fallback_call(self):
        """degraded 전이 → status 갱신만, _fallback_controller attribute 자체 부재.

        G6b 진입 전까지는 _fallback_controller 자체가 client에 없으므로 hook 호출이
        구조적으로 불가능. 이 invariant를 잠가서 G5+ 진입 시 의도된 추가만 들어가도록.
        """
        client = GopaxWsClient()
        client._set_ticker_freshness_status("warning")
        client._set_ticker_freshness_status("degraded")
        self.assertEqual(client._ticker_freshness_status, "degraded")
        # G6b _fallback_controller attribute 자체가 부재 — fallback hook 호출 불가
        self.assertFalse(hasattr(client, "_fallback_controller"))


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


class TestPrimusPingDualWrite(unittest.IsolatedAsyncioTestCase):
    """G4 dual-write (Codex 정정): _handle_primus_ping → _last_heartbeat_at + _liveness.observe_heartbeat 둘 다 호출."""

    async def test_dual_write_on_pong_success(self):
        """pong send 성공 시 _last_heartbeat_at + _liveness.last_heartbeat_at 모두 갱신."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        # 사전: 둘 다 None
        self.assertIsNone(client._last_heartbeat_at)
        self.assertIsNone(client._liveness.last_heartbeat_at)

        result = await client._handle_primus_ping(mock_ws, "primus::ping::1234")

        self.assertTrue(result)
        # G4 dual-write: 둘 다 갱신
        self.assertIsNotNone(client._last_heartbeat_at)
        self.assertIsNotNone(client._liveness.last_heartbeat_at)
        # 두 값이 동일한 timestamp인지 (같은 now 시점)
        self.assertEqual(client._last_heartbeat_at, client._liveness.last_heartbeat_at)


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
    """G4 (Codex 강조): G4 land 후에도 activation 보류 docstring 명시.

    "production activation은 최소 G5 Redis writer + PR 2e telemetry 이후 검토" +
    "G4는 local/staging smoke 가능 단계" 명시.
    """

    def test_module_docstring_g4_activation_constraint(self):
        """module docstring에 G4 단계도 activation 보류 명시."""
        import app.crawlers.usdt_ws.gopax as gopax_module
        doc = gopax_module.__doc__ or ""
        self.assertIn("G4 단계도 유지", doc)
        # G5 Redis writer + PR 2e telemetry 이후 검토
        self.assertIn("G5", doc)
        self.assertIn("telemetry", doc)
        # local/staging smoke 가능 단계
        self.assertIn("local/staging smoke", doc)

    def test_class_docstring_g4_activation_constraint(self):
        """class docstring에도 G4 단계 activation 보류 명시."""
        doc = GopaxWsClient.__doc__ or ""
        self.assertIn("G4 단계도 유지", doc)
        self.assertIn("G5", doc)


if __name__ == "__main__":
    unittest.main()
