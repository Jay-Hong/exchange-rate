"""app/crawlers/krx_kis.py 단위 테스트 (PR6a-2).

운영 미연결 골격 한정 — 실제 KIS API/WebSocket 연결 X.
순수 로직과 mock 기반 검증만.

실행:
    python -m unittest tests.test_krx_kis -v
또는:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.crawlers.krx_kis import (
    APPROVAL_REFRESH_MARGIN_SEC,
    CONTRACT_EXPIRES_ON_STATIC,
    CONTRACT_MONTH_STATIC,
    KisApprovalManager,
    KisFuturesClient,
    KrxDbWriter,
    PARSER_DISPATCH,
    RECONNECT_BACKOFF_SEQ,
    RECONNECT_BACKOFF_TAIL,
    SESSION_TR_MAP,
    STALE_AFTER_SEC,
    TR_KEY_STATIC,
    make_normalized_payload,
)
from app.sources.kis_master import ContractInfo
from app.sources.kis_futures import (
    parse_h0cfcnt0_payload,
    parse_h0mfasp0_payload,
    parse_h0mfcnt0_payload,
)


# ---------------------------------------------------------------------------
# Constants — PR6 합의 양식 invariant
# ---------------------------------------------------------------------------

class TestConstants(unittest.TestCase):

    def test_session_tr_map(self):
        self.assertEqual(SESSION_TR_MAP["CF"], ["H0CFCNT0", "H0CFASP0"])
        self.assertEqual(SESSION_TR_MAP["CM"], ["H0MFCNT0", "H0MFASP0"])

    def test_parser_dispatch_keys(self):
        self.assertEqual(
            sorted(PARSER_DISPATCH.keys()),
            ["H0CFASP0", "H0CFCNT0", "H0MFASP0", "H0MFCNT0"],
        )

    def test_static_canary(self):
        # PR6a static — front-month resolver 도입 전까지 고정
        self.assertEqual(TR_KEY_STATIC, "A75605")
        self.assertEqual(CONTRACT_MONTH_STATIC, "202605")
        self.assertEqual(CONTRACT_EXPIRES_ON_STATIC.isoformat(), "2026-05-18")

    def test_stale_threshold(self):
        self.assertEqual(STALE_AFTER_SEC, 60)  # Codex 권고

    def test_reconnect_backoff(self):
        self.assertEqual(RECONNECT_BACKOFF_SEQ, (1, 2, 4, 8, 16, 30))
        self.assertEqual(RECONNECT_BACKOFF_TAIL, 30)

    def test_approval_refresh_margin(self):
        self.assertEqual(APPROVAL_REFRESH_MARGIN_SEC, 300)


# ---------------------------------------------------------------------------
# _compute_backoff — 1/2/4/8/16/30/30/...
# ---------------------------------------------------------------------------

class TestComputeBackoff(unittest.TestCase):

    def test_attempt_1_to_6_uses_seq(self):
        for i, exp in enumerate([1, 2, 4, 8, 16, 30], start=1):
            self.assertEqual(KisFuturesClient._compute_backoff(i), exp)

    def test_attempt_7_plus_uses_tail(self):
        for attempt in (7, 8, 50, 1000):
            self.assertEqual(KisFuturesClient._compute_backoff(attempt), 30)


# ---------------------------------------------------------------------------
# make_normalized_payload — 체결만, 호가는 price 빈 값
# ---------------------------------------------------------------------------

class TestMakeNormalizedPayload(unittest.TestCase):

    def setUp(self):
        self.now_kst = datetime(2026, 5, 4, 18, 3, 32)

    def test_h0mfcnt0_full_fields(self):
        """야간 체결 (H0MFCNT0) raw → 11 필드 + price=futs_prpr."""
        raw = "^".join([
            "A75605", "180332", "6.30", "2", "0.43", "1468.50",
            "1468.90", "1469.30", "1468.50", "1", "2674", "39275780",
        ] + ["0"] * 37)
        parsed = parse_h0mfcnt0_payload(raw)
        payload = make_normalized_payload(
            parsed, session="CM", tr_id="H0MFCNT0", received_at_kst=self.now_kst,
        )
        # 11 필드 모두 검증
        self.assertEqual(payload["source"], "krx")
        self.assertEqual(payload["asset"], "usd-krw-futures")
        self.assertEqual(payload["session"], "CM")
        self.assertEqual(payload["contract_code"], "A75605")
        self.assertEqual(payload["contract_month"], "202605")
        self.assertEqual(payload["expires_on"], "2026-05-18")
        self.assertEqual(payload["price"], "1468.50")  # futs_prpr 그대로
        self.assertEqual(payload["market_time"], "180332")
        self.assertEqual(payload["received_at"], "2026-05-04T18:03:32")
        self.assertEqual(payload["tr_id"], "H0MFCNT0")
        self.assertEqual(payload["status"], "normal")
        self.assertEqual(len(payload), 11)

    def test_h0cfcnt0_session_cf(self):
        """주간 체결 (H0CFCNT0) raw → session CF."""
        raw = "^".join([
            "A75605", "093832", "-12.70", "5", "-0.86", "1470.60",
            "1473.10", "1473.30", "1469.50", "10", "122927", "1807719093000",
        ] + ["0"] * 38)
        parsed = parse_h0cfcnt0_payload(raw)
        payload = make_normalized_payload(
            parsed, session="CF", tr_id="H0CFCNT0", received_at_kst=self.now_kst,
        )
        self.assertEqual(payload["price"], "1470.60")
        self.assertEqual(payload["session"], "CF")
        self.assertEqual(payload["tr_id"], "H0CFCNT0")

    def test_h0mfasp0_quote_no_price(self):
        """호가 (H0MFASP0) → futs_prpr 없으니 price 빈 값.

        defensive — make_normalized_payload가 잘못 호출되어도 매도호가1로
        덮어쓰기 되지 않음. 실제 fanout은 _dispatch_tick에서 차단.
        """
        raw = "^".join([
            "A75605", "180332",
            "1468.70", "1468.80", "1468.90", "1469.00", "1469.10",
            "1468.50", "1468.40", "1468.30", "1468.20", "1468.10",
        ] + ["0"] * 26)
        parsed = parse_h0mfasp0_payload(raw)
        payload = make_normalized_payload(
            parsed, session="CM", tr_id="H0MFASP0", received_at_kst=self.now_kst,
        )
        self.assertEqual(payload["price"], "")


# ---------------------------------------------------------------------------
# KisApprovalManager — cache (sync)
# ---------------------------------------------------------------------------

class TestApprovalManagerCache(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "kis_ws_approval.json"
        self.mgr = KisApprovalManager(
            app_key="dummy_key",
            app_secret="dummy_secret",
            cache_path=self.cache_path,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_cache_missing(self):
        self.assertIsNone(self.mgr._load_cache())

    def test_load_cache_corrupt(self):
        """JSON 손상 → None + warning (예외 X)."""
        self.cache_path.write_text("{ not json")
        with self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            result = self.mgr._load_cache()
        self.assertIsNone(result)
        self.assertTrue(any("캐시 읽기 실패" in m for m in cm.output))

    def test_load_cache_valid(self):
        payload = {"approval_key": "abc", "expires_at_epoch": time.time() + 3600}
        self.cache_path.write_text(json.dumps(payload))
        loaded = self.mgr._load_cache()
        self.assertEqual(loaded["approval_key"], "abc")

    def test_save_cache_chmod_600(self):
        """저장 시 chmod 0o600."""
        payload = {"approval_key": "abc", "expires_at_epoch": time.time() + 3600}
        self.mgr._save_cache(payload)
        mode = stat.S_IMODE(self.cache_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_is_fresh_within_margin_returns_false(self):
        """만료 5분 미만 → False (refresh 필요)."""
        cached = {"expires_at_epoch": time.time() + 60}  # 1분
        self.assertFalse(self.mgr._is_fresh(cached))

    def test_is_fresh_well_before_margin_returns_true(self):
        """만료 5분 이상 남음 → True."""
        cached = {"expires_at_epoch": time.time() + 3600}
        self.assertTrue(self.mgr._is_fresh(cached))

    def test_is_still_valid_not_expired(self):
        """만료 안 지남 → True (발급 실패 fallback 용)."""
        cached = {"expires_at_epoch": time.time() + 60}
        self.assertTrue(self.mgr._is_still_valid(cached))

    def test_is_still_valid_expired(self):
        """만료 지남 → False."""
        cached = {"expires_at_epoch": time.time() - 60}
        self.assertFalse(self.mgr._is_still_valid(cached))


# ---------------------------------------------------------------------------
# KisApprovalManager — get_approval_key (async)
# ---------------------------------------------------------------------------

class TestApprovalManagerGetKey(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "kis_ws_approval.json"
        self.mgr = KisApprovalManager(
            app_key="dummy_key",
            app_secret="dummy_secret",
            cache_path=self.cache_path,
        )

    def tearDown(self):
        self.tmp.cleanup()

    async def test_returns_cached_when_fresh(self):
        """cache fresh → 발급 호출 X."""
        payload = {"approval_key": "cached_key", "expires_at_epoch": time.time() + 3600}
        self.cache_path.write_text(json.dumps(payload))
        with patch.object(self.mgr, "_issue_new") as mock_issue:
            key = await self.mgr.get_approval_key()
            self.assertEqual(key, "cached_key")
            mock_issue.assert_not_called()

    async def test_issues_new_when_near_expiry(self):
        """cache 만료 임박 → 새 발급."""
        old = {"approval_key": "old_key", "expires_at_epoch": time.time() + 60}
        self.cache_path.write_text(json.dumps(old))
        new_payload = {
            "approval_key": "new_key",
            "expires_at_epoch": time.time() + 3600 * 23,
            "created_at_epoch": time.time(),
        }
        with patch.object(self.mgr, "_issue_new", return_value=new_payload) as mock_issue:
            key = await self.mgr.get_approval_key()
            self.assertEqual(key, "new_key")
            mock_issue.assert_called_once()

    async def test_falls_back_to_cache_when_issue_fails(self):
        """발급 실패 + cache 만료 안 지남 → 기존 cache 반환."""
        cached = {"approval_key": "valid_old", "expires_at_epoch": time.time() + 60}
        self.cache_path.write_text(json.dumps(cached))
        with patch.object(self.mgr, "_issue_new", side_effect=RuntimeError("network error")):
            with self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
                key = await self.mgr.get_approval_key()
        self.assertEqual(key, "valid_old")
        self.assertTrue(any("발급 실패" in m and "fallback" in m for m in cm.output))

    async def test_raises_when_issue_fails_and_no_valid_cache(self):
        """발급 실패 + cache 없음 → 예외 raise.

        cache 없거나 만료 지난 상태에서 _issue_new 실패 시:
        1. fallback 시도 warning 발생 (logger.warning)
        2. valid 캐시 없으니 예외 raise
        """
        with patch.object(self.mgr, "_issue_new", side_effect=RuntimeError("network error")):
            with self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
                with self.assertRaises(RuntimeError):
                    await self.mgr.get_approval_key()
        self.assertTrue(any("발급 실패" in m for m in cm.output))


# ---------------------------------------------------------------------------
# KisFuturesClient._set_status
# ---------------------------------------------------------------------------

class TestSetStatus(unittest.TestCase):

    def setUp(self):
        self.client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager)
        )

    def test_initial_status_normal(self):
        self.assertEqual(self.client.status, "normal")

    def test_status_transitions(self):
        with self.assertLogs("app.crawlers.krx_kis", level="INFO") as cm:
            self.client._set_status("reconnecting")
            self.assertEqual(self.client.status, "reconnecting")
            self.client._set_status("stale")
            self.assertEqual(self.client.status, "stale")
            self.client._set_status("normal")
            self.assertEqual(self.client.status, "normal")
        # 3회 전이 모두 로그됨
        self.assertTrue(any("normal → reconnecting" in m for m in cm.output))
        self.assertTrue(any("reconnecting → stale" in m for m in cm.output))
        self.assertTrue(any("stale → normal" in m for m in cm.output))

    def test_same_status_idempotent(self):
        self.client._set_status("normal")
        self.client._set_status("normal")
        self.assertEqual(self.client.status, "normal")


# ---------------------------------------------------------------------------
# KisFuturesClient._dispatch_tick — quote 차단, 체결 fanout
# ---------------------------------------------------------------------------

class TestDispatchTick(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager)
        )

    def _quote_raw(self) -> str:
        return "^".join([
            "A75605", "180332",
            "1468.70", "1468.80", "1468.90", "1469.00", "1469.10",
            "1468.50", "1468.40", "1468.30", "1468.20", "1468.10",
        ] + ["0"] * 26)

    def _conclusion_raw_cm(self) -> str:
        return "^".join([
            "A75605", "180332", "6.30", "2", "0.43", "1468.50",
            "1468.90", "1469.30", "1468.50", "1", "2674", "39275780",
        ] + ["0"] * 37)

    def _conclusion_raw_cf(self) -> str:
        return "^".join([
            "A75605", "093832", "-12.70", "5", "-0.86", "1470.60",
            "1473.10", "1473.30", "1469.50", "10", "122927", "1807719093000",
        ] + ["0"] * 38)

    async def test_quote_h0cfasp0_skipped(self):
        called = []

        async def handler(p):
            called.append(p)

        self.client.add_tick_handler(handler)
        await self.client._dispatch_tick("H0CFASP0", self._quote_raw(), "CF")
        self.assertEqual(called, [])

    async def test_quote_h0mfasp0_skipped(self):
        called = []

        async def handler(p):
            called.append(p)

        self.client.add_tick_handler(handler)
        await self.client._dispatch_tick("H0MFASP0", self._quote_raw(), "CM")
        self.assertEqual(called, [])

    async def test_conclusion_h0cfcnt0_fanout(self):
        called = []

        async def handler(p):
            called.append(p)

        self.client.add_tick_handler(handler)
        await self.client._dispatch_tick("H0CFCNT0", self._conclusion_raw_cf(), "CF")
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0]["price"], "1470.60")
        self.assertEqual(called[0]["session"], "CF")
        self.assertEqual(called[0]["tr_id"], "H0CFCNT0")

    async def test_conclusion_h0mfcnt0_fanout(self):
        called = []

        async def handler(p):
            called.append(p)

        self.client.add_tick_handler(handler)
        await self.client._dispatch_tick("H0MFCNT0", self._conclusion_raw_cm(), "CM")
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0]["price"], "1468.50")
        self.assertEqual(called[0]["session"], "CM")

    async def test_unknown_tr_id_no_fanout(self):
        called = []

        async def handler(p):
            called.append(p)

        self.client.add_tick_handler(handler)
        with self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            await self.client._dispatch_tick("H9XXXXX0", "garbage", "CF")
        self.assertEqual(called, [])
        self.assertTrue(any("unknown tr_id" in m for m in cm.output))

    async def test_dispatch_updates_last_tick_at_for_quote_too(self):
        """tick 수신 시 last_tick_at 갱신 (호가/체결 무관, 연결 정상 신호)."""
        self.assertIsNone(self.client._last_tick_at)
        before = time.time()
        await self.client._dispatch_tick("H0MFASP0", self._quote_raw(), "CM")
        self.assertIsNotNone(self.client._last_tick_at)
        self.assertGreaterEqual(self.client._last_tick_at, before)


# ---------------------------------------------------------------------------
# KisFuturesClient._fanout — handler 예외 격리
# ---------------------------------------------------------------------------

class TestFanout(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager)
        )
        self.payload = {"source": "krx", "asset": "usd-krw-futures", "price": "1470.0"}

    async def test_no_handlers(self):
        await self.client._fanout(self.payload)  # 예외 X

    async def test_single_handler_called(self):
        called = []

        async def handler(p):
            called.append(p)

        self.client.add_tick_handler(handler)
        await self.client._fanout(self.payload)
        self.assertEqual(called, [self.payload])

    async def test_handler_exception_isolated(self):
        """한 handler 예외 → 다른 handler 영향 X (logger.warning 가시화)."""
        called_ok = []

        async def bad_handler(p):
            raise RuntimeError("intentional")

        async def good_handler(p):
            called_ok.append(p)

        self.client.add_tick_handler(bad_handler)
        self.client.add_tick_handler(good_handler)
        # _fanout 자체는 예외 raise X (return_exceptions=True)
        with self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            await self.client._fanout(self.payload)
        self.assertEqual(called_ok, [self.payload])
        # bad_handler 예외가 logger.warning으로 가시화됐는지
        self.assertTrue(any("handler" in m and "exception" in m for m in cm.output))

    async def test_multiple_handlers_concurrent(self):
        called = []

        async def h1(p):
            await asyncio.sleep(0.01)
            called.append("h1")

        async def h2(p):
            await asyncio.sleep(0.01)
            called.append("h2")

        self.client.add_tick_handler(h1)
        self.client.add_tick_handler(h2)
        await self.client._fanout(self.payload)
        self.assertEqual(set(called), {"h1", "h2"})


# ---------------------------------------------------------------------------
# _sub_message — KIS WebSocket subscribe 메시지 형식
# ---------------------------------------------------------------------------

class TestSubMessage(unittest.TestCase):

    def test_subscribe_message_structure(self):
        msg = KisFuturesClient._sub_message("test_key", "H0CFCNT0", "A75605")
        parsed = json.loads(msg)
        self.assertEqual(parsed["header"]["approval_key"], "test_key")
        self.assertEqual(parsed["header"]["custtype"], "P")
        self.assertEqual(parsed["header"]["tr_type"], "1")
        self.assertEqual(parsed["body"]["input"]["tr_id"], "H0CFCNT0")
        self.assertEqual(parsed["body"]["input"]["tr_key"], "A75605")


# ---------------------------------------------------------------------------
# Contract injection (PR6c-2a) — KisFuturesClient에 동적 contract 주입
# ---------------------------------------------------------------------------

class TestContractInjection(unittest.IsolatedAsyncioTestCase):
    """KisFuturesClient에 custom ContractInfo 주입 시 tr_key/payload 동적 사용.

    PR6a static fallback (TR_KEY_STATIC = A75605)에서 PR6c-2b 운영 진입
    시점에 select_active_usd_futures_contract 결과 주입 가능하게 보강.
    """

    def setUp(self):
        # 다음 만기 월물 (rollover 시나리오) 가정
        self.next_month_contract = ContractInfo(
            short_code="A75606",
            standard_code="KR4A75660006",
            name="미국달러 F 202606",
            contract_month="202606",
            expiry_date=date(2026, 6, 15),
        )

    def test_default_contract_is_pr6a_static(self):
        """contract 미주입 → PR6a static fallback (A75605, 5/18 만기)."""
        client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager)
        )
        self.assertEqual(client._contract.short_code, TR_KEY_STATIC)
        self.assertEqual(client._contract.contract_month, CONTRACT_MONTH_STATIC)
        self.assertEqual(client._contract.expiry_date, CONTRACT_EXPIRES_ON_STATIC)

    def test_custom_contract_used_in_subscribe(self):
        """custom contract 주입 → _sub_message에서 tr_key 동적 사용.

        실제 _run_session은 WebSocket connect 필요 — _sub_message만 직접
        호출하여 tr_key 인자가 contract.short_code인지 검증 (구조 단위 검증).
        """
        client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager),
            contract=self.next_month_contract,
        )
        # client._contract가 주입된 그대로
        self.assertEqual(client._contract.short_code, "A75606")
        self.assertEqual(client._contract.contract_month, "202606")

        # _sub_message는 staticmethod라 직접 호출 + 동적 tr_key 검증
        msg = client._sub_message("test_key", "H0CFCNT0", client._contract.short_code)
        parsed = json.loads(msg)
        self.assertEqual(parsed["body"]["input"]["tr_key"], "A75606")

    async def test_dispatch_tick_uses_custom_contract_metadata(self):
        """custom contract 주입 → _dispatch_tick fanout payload 동적 metadata."""
        client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager),
            contract=self.next_month_contract,
        )
        called = []

        async def handler(payload):
            called.append(payload)

        client.add_tick_handler(handler)
        # 야간 체결 raw (어제 smoke 샘플)
        raw = "^".join([
            "A75606", "180332", "6.30", "2", "0.43", "1500.00",
            "1500.50", "1501.00", "1499.50", "1", "100", "1000000",
        ] + ["0"] * 37)
        await client._dispatch_tick("H0MFCNT0", raw, "CM")

        self.assertEqual(len(called), 1)
        payload = called[0]
        # contract metadata가 주입된 contract와 일치
        self.assertEqual(payload["contract_code"], "A75606")
        self.assertEqual(payload["contract_month"], "202606")
        self.assertEqual(payload["expires_on"], "2026-06-15")
        # parsed 가격은 raw 그대로
        self.assertEqual(payload["price"], "1500.00")

    async def test_connect_and_listen_passes_contract_short_code_to_sub_message(self):
        """회귀 방지: _connect_and_listen이 _sub_message에 self._contract.short_code 전달.

        Codex 권고 — "내가 넘긴 값이 메시지에 들어간다"가 아닌 "실제 connect 흐름에서
        contract.short_code가 사용되는지" 검증. 누군가 _run_session에서 client._contract
        대신 다른 source 잘못 쓰면 본 테스트가 잡음.
        """
        client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager),
            contract=self.next_month_contract,
        )
        # stop 미리 설정 — subscribe만 하고 while loop에서 즉시 return
        client._stop.set()

        # WebSocket connect를 async context manager mock
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_connect = AsyncMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect.__aexit__ = AsyncMock(return_value=None)

        # _sub_message spy
        sub_msg_spy = MagicMock(return_value='{"test": "msg"}')

        with patch("app.crawlers.krx_kis.websockets.connect", return_value=mock_connect), \
             patch.object(KisFuturesClient, "_sub_message", side_effect=sub_msg_spy), \
             self.assertLogs("app.crawlers.krx_kis", level="INFO"):
            await client._connect_and_listen("CM", "test_approval")

        # _sub_message 호출 검증 — CM session에서 H0MFCNT0/H0MFASP0 두 번
        self.assertEqual(sub_msg_spy.call_count, 2)
        # 모든 호출의 tr_key 인자가 "A75606" (주입된 contract.short_code)
        for call in sub_msg_spy.call_args_list:
            args = call[0]  # (approval_key, tr_id, tr_key)
            self.assertEqual(
                args[2], "A75606",
                f"_sub_message tr_key={args[2]} expected A75606 (contract.short_code)",
            )


# ---------------------------------------------------------------------------
# KrxDbWriter (PR6b-2b) — 1초 window debounce + insert-if-changed + to_thread
# ---------------------------------------------------------------------------

class TestKrxDbWriter(unittest.IsolatedAsyncioTestCase):
    """KRX DB writer handler — 1초 window의 last tick만 insert-if-changed."""

    def _make_payload(self, price: str = "1468.50", market_time: str = "180332") -> dict:
        return {
            "source": "krx",
            "asset": "usd-krw-futures",
            "session": "CM",
            "contract_code": "A75605",
            "contract_month": "202605",
            "expires_on": "2026-05-18",
            "price": price,
            "market_time": market_time,
            "received_at": "2026-05-04T18:03:32",
            "tr_id": "H0MFCNT0",
            "status": "normal",
        }

    async def test_single_tick_flushes_after_window(self):
        """단일 tick → window 만료 후 1회 DB write."""
        writer = KrxDbWriter(window_sec=0.05)  # 짧은 window for fast test
        payload = self._make_payload()

        with patch("app.crawlers.krx_kis.KrxDbWriter._sync_db_write") as mock_db:
            await writer(payload)
            self.assertIsNotNone(writer._timer)
            await writer._timer  # window 만료 대기
            mock_db.assert_called_once_with(payload)

    async def test_multiple_ticks_in_window_flushes_last_only(self):
        """window 안 여러 tick → last 1건만 flush (debounce)."""
        writer = KrxDbWriter(window_sec=0.1)
        first = self._make_payload(price="1468.30")
        second = self._make_payload(price="1468.40")
        third = self._make_payload(price="1468.50")  # last

        with patch("app.crawlers.krx_kis.KrxDbWriter._sync_db_write") as mock_db:
            await writer(first)
            await writer(second)
            await writer(third)
            await writer._timer
            mock_db.assert_called_once()
            # last tick (third)만 flush
            called_payload = mock_db.call_args[0][0]
            self.assertEqual(called_payload["price"], "1468.50")

    async def test_window_renewal_after_flush(self):
        """window 만료 후 새 tick → 새 timer 시작."""
        writer = KrxDbWriter(window_sec=0.05)
        first = self._make_payload(price="1468.30")
        second = self._make_payload(price="1468.40")

        with patch("app.crawlers.krx_kis.KrxDbWriter._sync_db_write") as mock_db:
            await writer(first)
            await writer._timer  # 첫 window flush
            await writer(second)
            await writer._timer  # 두 번째 window flush
            self.assertEqual(mock_db.call_count, 2)
            second_call_payload = mock_db.call_args_list[1][0][0]
            self.assertEqual(second_call_payload["price"], "1468.40")

    async def test_db_write_failure_isolated(self):
        """DB write 예외 → logger.warning + propagate X (KRX optional 격리)."""
        writer = KrxDbWriter(window_sec=0.05)
        payload = self._make_payload()

        with patch(
            "app.crawlers.krx_kis.KrxDbWriter._sync_db_write",
            side_effect=RuntimeError("DB connection lost"),
        ), self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            await writer(payload)
            await writer._timer  # 예외 발생 대기 (raise X — 격리)
        self.assertTrue(any("DB write failed" in m for m in cm.output))

    async def test_to_thread_used(self):
        """sync DB 호출이 asyncio.to_thread로 격리되는지 — event loop 차단 방지."""
        writer = KrxDbWriter(window_sec=0.05)
        payload = self._make_payload()

        # asyncio.to_thread를 patch — 호출 검증
        with patch(
            "app.crawlers.krx_kis.asyncio.to_thread",
            new_callable=AsyncMock,
        ) as mock_to_thread:
            await writer(payload)
            await writer._timer
            mock_to_thread.assert_called_once()
            # 첫 인자가 _sync_db_write 함수
            self.assertEqual(
                mock_to_thread.call_args[0][0],
                KrxDbWriter._sync_db_write,
            )

    async def test_no_tick_no_flush(self):
        """tick 없으면 flush 호출 X."""
        writer = KrxDbWriter(window_sec=0.05)
        # _last_tick None 상태에서 직접 _flush_after_window 호출
        with patch("app.crawlers.krx_kis.KrxDbWriter._sync_db_write") as mock_db:
            await writer._flush_after_window()
            mock_db.assert_not_called()

    async def test_tick_during_db_write_creates_new_timer(self):
        """DB write 진행 중 들어온 tick → finally에서 새 timer 예약 (race 방지).

        Codex PR6b-2b race 보정 검증.
        """
        writer = KrxDbWriter(window_sec=0.01)
        first = self._make_payload(price="1468.30")
        second = self._make_payload(price="1468.40")
        db_calls = []

        def db_write_simulating_concurrent_tick(tick):
            """DB write 함수 — 첫 호출 시 새 tick이 들어왔다고 시뮬레이션."""
            db_calls.append(tick)
            if len(db_calls) == 1:
                # 첫 write 진행 중 새 tick 도착 (race 시나리오)
                writer._last_tick = second

        with patch(
            "app.crawlers.krx_kis.KrxDbWriter._sync_db_write",
            side_effect=db_write_simulating_concurrent_tick,
        ):
            await writer(first)
            await writer._timer  # 첫 flush 완료 (finally에서 새 timer 예약됨)
            # 새 timer는 self._timer에 할당되어 있음
            self.assertIsNotNone(writer._timer)
            await writer._timer  # 새 timer로 second flush 완료
            # second tick이 누락되지 않고 저장됨
            self.assertEqual(len(db_calls), 2)
            self.assertEqual(db_calls[1]["price"], "1468.40")

    async def test_no_new_timer_when_no_tick_during_write(self):
        """DB write 종료 시 _last_tick None이면 새 timer 예약 X."""
        writer = KrxDbWriter(window_sec=0.01)
        payload = self._make_payload()
        old_timer_done = []

        with patch("app.crawlers.krx_kis.KrxDbWriter._sync_db_write"):
            await writer(payload)
            old_timer = writer._timer
            await writer._timer  # flush 완료, _last_tick None 유지
            old_timer_done.append(old_timer.done())
            # finally 후 _timer가 같은 (done) timer 또는 None
            # 정확히는 같은 timer 유지 (finally 블록이 새 task 안 만듦)
            self.assertTrue(writer._timer.done())
        self.assertTrue(old_timer_done[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
