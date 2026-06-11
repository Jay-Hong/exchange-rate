"""§12.9.8 ① — Gopax heartbeat-alive·ticker-dead detector 단위 테스트.

6/9 KRX silent-stale fix의 USDT판. 현 Gopax fix(`is_stale=max(tick,heartbeat) silence`)는
heartbeat fresh + ticker만 dead인 모드를 못 잡음. detector는 REST `/tickers` lastTraded(체결
시각)와 WS 저장 lastTraded를 비교 → REST 앞서면 "시장 거래했는데 WS dead" 확정 → reconnect.

테스트 매트릭스:
  1. REST > WS → request_reconnect 호출 (flag set)
  2. REST == WS (조용한 시장) → 무동작
  3. REST None (HTTP/parse 실패) → 무동작 — fanout과 독립(외부 검토 #1)이라 detector 자체 격리
  4. WS None (첫 tick 전) → None guard 무동작 (외부 검토 #2)
  5. detector/probe 미설정(callback None) → schedule no-op (기존 caller 불변)
  6. cooldown → 2번째 schedule skip
  7. invariant: probe(_run_probe, /ticker fanout)는 _last_ws_traded_ms 미오염 (자기 무력화 차단)
  8. _request_ticker_dead_reconnect → flag set
  9. recv loop: flag set 시 _SilentSessionError("ticker_dead") raise
  10. race: valid WS tick이 flag clear → reconnect 무력화
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.crawlers.usdt_ws import gopax
from app.crawlers.usdt_ws.gopax import (
    GopaxRestFallbackController,
    GopaxWsClient,
    _SilentSessionError,
)


def _mock_connect(mock_ws):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_ws)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch("app.crawlers.usdt_ws.gopax.websockets.connect", return_value=ctx)


def _valid_ticker_event(rate="1480", last_traded_ms=1780000000000):
    return json.dumps(
        {"n": "TickerEvent", "o": {"USDT-KRW": {"last": rate, "lastTraded": last_traded_ms}}}
    )


def _make_detector_controller(*, ws_last_traded, request_mock):
    return GopaxRestFallbackController(
        redis_writer=MagicMock(),
        db_writer=MagicMock(),
        alert_evaluator=MagicMock(),
        ws_last_traded_getter=lambda: ws_last_traded,
        request_ticker_dead_reconnect=request_mock,
    )


class TestTickerDeadDetectorLogic(unittest.IsolatedAsyncioTestCase):
    """_run_ticker_dead_check 판정 — REST vs WS lastTraded 비교."""

    async def test_rest_ahead_of_ws_requests_reconnect(self):
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=1_000, request_mock=request)
        with patch.object(c, "_fetch_gopax_last_traded", return_value=2_000):
            await c._run_ticker_dead_check("test")
        request.assert_called_once()  # REST(2000) > WS(1000) → reconnect 요청
        self.assertFalse(c._tdc_in_flight)
        self.assertGreater(c._tdc_cooldown_until, 0.0)

    async def test_rest_equals_ws_no_request(self):
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=2_000, request_mock=request)
        with patch.object(c, "_fetch_gopax_last_traded", return_value=2_000):
            await c._run_ticker_dead_check("test")
        request.assert_not_called()  # 조용한 시장 — 무동작

    async def test_rest_behind_ws_no_request(self):
        # REST가 WS보다 과거(있을 수 없으나 방어) — strict >라 무동작
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=2_000, request_mock=request)
        with patch.object(c, "_fetch_gopax_last_traded", return_value=1_500):
            await c._run_ticker_dead_check("test")
        request.assert_not_called()

    async def test_rest_none_no_request(self):
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=1_000, request_mock=request)
        with patch.object(c, "_fetch_gopax_last_traded", return_value=None):
            await c._run_ticker_dead_check("test")
        request.assert_not_called()  # REST 실패 — detector 자체 격리, reconnect 0

    async def test_ws_none_guard_no_request(self):
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=None, request_mock=request)  # 첫 tick 전
        with patch.object(c, "_fetch_gopax_last_traded", return_value=2_000):
            await c._run_ticker_dead_check("test")
        request.assert_not_called()  # None guard — 비교 skip

    async def test_fetch_exception_isolated(self):
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=1_000, request_mock=request)
        with patch.object(c, "_fetch_gopax_last_traded", side_effect=RuntimeError("boom")):
            await c._run_ticker_dead_check("test")  # 격리 — 예외 전파 X
        request.assert_not_called()
        self.assertFalse(c._tdc_in_flight)


class TestTickerDeadScheduleGating(unittest.IsolatedAsyncioTestCase):
    """schedule_ticker_dead_check — callback 미설정 / cooldown / in-flight 게이팅."""

    async def test_disabled_when_callbacks_none(self):
        # getter/request 미주입(기존 caller) → schedule no-op
        c = GopaxRestFallbackController(
            redis_writer=MagicMock(), db_writer=MagicMock(), alert_evaluator=MagicMock(),
        )
        c.schedule_ticker_dead_check("test")
        self.assertEqual(c.ticker_dead_check_count, 0)
        self.assertIsNone(c._pending_ticker_dead_task)

    async def test_cooldown_skips_second_schedule(self):
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=1_000, request_mock=request)
        with patch.object(c, "_fetch_gopax_last_traded", return_value=900):
            c.schedule_ticker_dead_check("first")
            self.assertEqual(c.ticker_dead_check_count, 1)
            if c._pending_ticker_dead_task:
                await c._pending_ticker_dead_task  # cooldown 설정됨
            c.schedule_ticker_dead_check("second")  # cooldown → skip
        self.assertEqual(c.ticker_dead_check_count, 1)

    async def test_reset_cooldown_clears_detector_cooldown(self):
        request = MagicMock()
        c = _make_detector_controller(ws_last_traded=1_000, request_mock=request)
        c._tdc_cooldown_until = time.time() + 999
        c.reset_cooldown()
        self.assertEqual(c._tdc_cooldown_until, 0.0)


class TestTickerDeadClientState(unittest.IsolatedAsyncioTestCase):
    """client 측 — invariant(probe 미오염) + flag set."""

    async def test_probe_does_not_pollute_last_ws_traded(self):
        # 외부 검토 invariant: /ticker probe(fanout)는 _last_ws_traded_ms 미갱신
        #   (오염되면 detector가 자기 무력화). probe는 writer만 호출, recv tick path 미경유.
        client = GopaxWsClient()
        client._last_ws_traded_ms = 1234
        tick = {"source": "gopax", "asset": "usdt-krw", "rate": 1480.0,
                "timestamp_ms": 9_999_999}
        with patch.object(client._fallback_controller, "_fetch_gopax_tick", return_value=tick):
            await client._fallback_controller._run_probe("test")
        self.assertEqual(client._last_ws_traded_ms, 1234)  # 불변

    def test_request_sets_flag(self):
        client = GopaxWsClient()
        self.assertFalse(client._ticker_dead_reconnect_requested)
        client._request_ticker_dead_reconnect()
        self.assertTrue(client._ticker_dead_reconnect_requested)

    def test_controller_wired_with_client_callbacks(self):
        # client 생성 시 controller에 getter/request가 주입됐는지 (detector 활성)
        client = GopaxWsClient()
        client._last_ws_traded_ms = 555
        self.assertEqual(client._fallback_controller._ws_last_traded_getter(), 555)
        self.assertIsNotNone(client._fallback_controller._request_ticker_dead_reconnect)


class TestTickerDeadRecvLoop(unittest.IsolatedAsyncioTestCase):
    """recv loop 통합 — flag → raise / valid tick → race clear."""

    async def test_flag_raises_silent_session_error(self):
        """session 중 detector가 flag set(heartbeat만 오는 ticker-dead) → recv loop raise.

        session 시작 시 flag 리셋되므로, recv await 중 set되는 상황을 시뮬
        (heartbeat 반환 → flag clear 안 됨 → 다음 iter top에서 raise).
        """
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()

        def recv_side():
            # detector가 await 중 flag set한 상황 + heartbeat 반환 (valid tick 아님 → clear X)
            client._ticker_dead_reconnect_requested = True
            return "primus::ping::x"

        mock_ws.recv = AsyncMock(side_effect=recv_side)
        with _mock_connect(mock_ws), patch.object(
            client._liveness, "is_stale", return_value=False
        ):
            with self.assertRaises(_SilentSessionError) as cm:
                await asyncio.wait_for(client._run_one_session(), timeout=2.0)
        self.assertEqual(str(cm.exception), "ticker_dead")
        self.assertEqual(client._connection_status, "stale")

    async def test_race_valid_tick_clears_flag_no_reconnect(self):
        """flag set과 동시에 valid WS tick 도착 → tick이 clear → reconnect 무력화 (race guard)."""
        client = GopaxWsClient()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        state = {"n": 0}

        def recv_side():
            state["n"] += 1
            if state["n"] == 1:
                client._ticker_dead_reconnect_requested = True  # detector가 await 중 set
                return _valid_ticker_event(last_traded_ms=1780000000999)  # 동시에 valid tick
            client._stop_event.set()
            return "primus::ping::x"

        mock_ws.recv = AsyncMock(side_effect=recv_side)
        with _mock_connect(mock_ws), patch.object(
            client._liveness, "is_stale", return_value=False
        ):
            # raise 없이 정상 종료 (valid tick이 flag clear)
            await asyncio.wait_for(client._run_one_session(), timeout=2.0)
        self.assertFalse(client._ticker_dead_reconnect_requested)  # tick이 clear
        self.assertEqual(client._last_ws_traded_ms, 1780000000999)  # WS lastTraded 저장


if __name__ == "__main__":
    unittest.main(verbosity=2)
