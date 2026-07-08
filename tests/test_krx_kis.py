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

from app import config
from app.crawlers.krx_kis import (
    ACCESS_TOKEN_REFRESH_MARGIN_SEC,
    APPROVAL_REFRESH_MARGIN_SEC,
    CONTRACT_EXPIRES_ON_STATIC,
    CONTRACT_MONTH_STATIC,
    KIS_PROD_HOST,
    KIS_REST_QUOTE_PATH,
    KIS_REST_QUOTE_MARKET_DIV_CODE,
    KIS_REST_QUOTE_TR_ID,
    KisAccessTokenManager,
    KisApprovalManager,
    KisFuturesClient,
    KrxDbWriter,
    PARSER_DISPATCH,
    RECONNECT_BACKOFF_SEQ,
    RECONNECT_BACKOFF_TAIL,
    SESSION_TR_MAP,
    SILENT_RECONNECT_SEC,
    STALE_AFTER_SEC,
    TR_KEY_STATIC,
    _KrxSilentSessionError,
    fetch_kis_futures_quote,
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

    # PR6e — rate 정규화 (KIS payload 8자리 정밀도 → KRX tick 0.1 KRW)

    def test_sync_db_write_normalizes_8digit_to_one_decimal_a(self):
        """1457.40007441 → DB write rate=1457.4 (Decimal.quantize 정규화)."""
        payload = self._make_payload(price="1457.40007441")
        with patch("app.crud.insert_source_rate_if_changed") as mock_insert, \
             patch("app.database.get_db_context"):
            KrxDbWriter._sync_db_write(payload)
            mock_insert.assert_called_once()
            kwargs = mock_insert.call_args.kwargs
            self.assertEqual(kwargs["rate"], 1457.4)
            self.assertEqual(kwargs["source"], "krx")
            self.assertEqual(kwargs["asset"], "usd-krw-futures")

    def test_sync_db_write_normalizes_8digit_to_one_decimal_b(self):
        """1455.80009883 → DB write rate=1455.8."""
        payload = self._make_payload(price="1455.80009883")
        with patch("app.crud.insert_source_rate_if_changed") as mock_insert, \
             patch("app.database.get_db_context"):
            KrxDbWriter._sync_db_write(payload)
            kwargs = mock_insert.call_args.kwargs
            self.assertEqual(kwargs["rate"], 1455.8)


# ---------------------------------------------------------------------------
# Phase B.2 PR3 — KrxDbWriter → tether topic trigger hook
# ---------------------------------------------------------------------------

class TestKrxDbWriterTetherTrigger(unittest.IsolatedAsyncioTestCase):
    """PR3 regression guards — finally 밖에서 trigger 호출 (PR2 mirror).

    smoke 기준 6종 검증:
      (1) inserted=True AND redis_ok=True → trigger 호출 + 인자 검증
      (2) inserted=False → trigger 미호출 (`_sync_db_write` False 분기)
      (3) inserted=True AND redis_ok=False → trigger 미호출
      (4) `_sync_db_write` exception → trigger 미호출 + writer loop 격리
      (5) trigger 예외 → writer loop 영향 X (close 정상)
      (6) legacy_piggyback mode 안전 (controller noop)
    """

    def _make_payload(self, price: str = "1468.50") -> dict:
        return {
            "source": "krx",
            "asset": "usd-krw-futures",
            "session": "CM",
            "contract_code": "A75605",
            "contract_month": "202605",
            "expires_on": "2026-05-18",
            "price": price,
            "market_time": "180332",
            "received_at": "2026-05-04T18:03:32",
            "tr_id": "H0MFCNT0",
            "status": "normal",
        }

    async def test_trigger_called_when_inserted_and_redis_ok(self):
        """(1) _sync_db_write True 반환 → trigger 호출 + 인자 검증."""
        writer = KrxDbWriter(window_sec=0.05)
        payload = self._make_payload()

        with patch(
            "app.crawlers.krx_kis.KrxDbWriter._sync_db_write",
            return_value=True,
        ), patch(
            "app.krx_topic_publisher.request_krx_topic_publish"
        ) as mock_trigger:
            await writer(payload)
            await writer._timer

        mock_trigger.assert_called_once()
        # ADR-038 D2: krx 독립 topic publish — reason만 (source/asset은 topic 고정)
        self.assertEqual(mock_trigger.call_args.kwargs["reason"], "krx_redis_write_success")

    async def test_trigger_not_called_when_inserted_false(self):
        """(2) _sync_db_write False (inserted=False) → trigger 미호출."""
        writer = KrxDbWriter(window_sec=0.05)
        payload = self._make_payload()

        with patch(
            "app.crawlers.krx_kis.KrxDbWriter._sync_db_write",
            return_value=False,
        ), patch(
            "app.krx_topic_publisher.request_krx_topic_publish"
        ) as mock_trigger:
            await writer(payload)
            await writer._timer

        mock_trigger.assert_not_called()

    async def test_trigger_not_called_when_redis_write_fails(self):
        """(3) inserted=True AND redis_ok=False → _sync_db_write가 False → 미호출.

        실제 경로: write_after_db_insert가 False return 시 _sync_db_write도 False.
        본 test는 직접 _sync_db_write False mock으로 동일 분기 검증.
        """
        writer = KrxDbWriter(window_sec=0.05)
        payload = self._make_payload()

        with patch(
            "app.crawlers.krx_kis.KrxRedisLatestWriter.write_after_db_insert",
            return_value=False,
        ), patch(
            "app.crud.insert_source_rate_if_changed",
            return_value=True,
        ), patch(
            "app.database.get_db_context"
        ), patch(
            "app.krx_topic_publisher.request_krx_topic_publish"
        ) as mock_trigger:
            await writer(payload)
            await writer._timer

        mock_trigger.assert_not_called()

    async def test_trigger_not_called_when_sync_db_write_raises(self):
        """(4) _sync_db_write exception → trigger 미호출 + writer 격리 (logger.warning)."""
        writer = KrxDbWriter(window_sec=0.05)
        payload = self._make_payload()

        with patch(
            "app.crawlers.krx_kis.KrxDbWriter._sync_db_write",
            side_effect=RuntimeError("DB connection lost"),
        ), patch(
            "app.krx_topic_publisher.request_krx_topic_publish"
        ) as mock_trigger, self.assertLogs("app.crawlers.krx_kis", level="WARNING"):
            await writer(payload)
            await writer._timer  # 예외 격리 — raise X

        mock_trigger.assert_not_called()

    async def test_trigger_exception_does_not_propagate_to_writer(self):
        """(5) trigger 자체 예외 → writer loop 영향 X (logger.exception, _timer 정상 종료)."""
        writer = KrxDbWriter(window_sec=0.05)
        payload = self._make_payload()

        with patch(
            "app.crawlers.krx_kis.KrxDbWriter._sync_db_write",
            return_value=True,
        ), patch(
            "app.krx_topic_publisher.request_krx_topic_publish",
            side_effect=RuntimeError("trigger boom"),
        ) as mock_trigger:
            await writer(payload)
            # writer._timer는 예외 없이 종료되어야 함
            await writer._timer

        mock_trigger.assert_called_once()
        self.assertTrue(writer._timer.done())

    async def test_legacy_piggyback_mode_safety(self):
        """(6→ADR-038 D2 재정의) KRX writer는 tether trigger controller를 더 이상 경유하지 않음.

        구 계약: legacy_piggyback mode에서 controller가 noop임을 검증.
        신 계약: KRX Redis write 성공은 krx 독립 topic publish 요청만 발화 —
        tether controller의 어떤 카운터도 증가하지 않아야 함 (usdt:krw 재발행 완전 분리).
        """
        from app.tether_topic_trigger import (
            MODE_LEGACY_PIGGYBACK,
            TetherTopicTriggerController,
            reset_tether_topic_trigger_for_tests,
        )

        publish_mock = AsyncMock(return_value=True)
        controller = TetherTopicTriggerController(
            mode=MODE_LEGACY_PIGGYBACK,
            coalesce_ms=5,
            publish_func=publish_mock,
        )
        reset_tether_topic_trigger_for_tests(controller)
        try:
            writer = KrxDbWriter(window_sec=0.05)
            payload = self._make_payload()
            with patch(
                "app.crawlers.krx_kis.KrxDbWriter._sync_db_write",
                return_value=True,
            ), patch(
                "app.krx_topic_publisher.request_krx_topic_publish"
            ) as mock_krx_publish:
                await writer(payload)
                await writer._timer
            await asyncio.sleep(0.02)

            # tether controller 완전 미경유 (ADR-038 D2)
            self.assertEqual(controller.stats.publish_skipped_legacy, 0)
            self.assertEqual(controller.stats.trigger_count, 0)
            self.assertEqual(controller.pending_count, 0)
            publish_mock.assert_not_awaited()
            # krx 독립 topic publish 요청은 발화
            mock_krx_publish.assert_called_once()
        finally:
            await controller.close(timeout=0.05)
            reset_tether_topic_trigger_for_tests(None)


# ---------------------------------------------------------------------------
# PR6e — stale carry-over 차단 (subscribe 후 _last_tick_at reset)
# ---------------------------------------------------------------------------

class TestStaleCarryOverReset(unittest.IsolatedAsyncioTestCase):
    """_connect_and_listen subscribe 직후 _last_tick_at = time.time() reset 검증.

    배경: 5/6 08:30 KST CF 첫 진입 시 5/5 06:00 마지막 tick의 _last_tick_at
    (~26h 전)이 carry-over → 첫 stale check가 즉시 stale 전이 → 2초 후
    첫 tick 도착으로 normal 자동 복구. baseline 서비스 영향 0이지만 의미적
    으로 stale 아님. PR6e fix로 carry-over 차단.
    """

    async def test_carry_over_does_not_trigger_immediate_stale(self):
        """ancient _last_tick_at(26h 전)도 subscribe 후 reset → 첫 stale check stale 안 됨."""
        client = KisFuturesClient(approval_manager=MagicMock(spec=KisApprovalManager))

        # carry-over 시나리오: 26h 전 마지막 tick
        ancient = time.time() - 26 * 3600
        client._last_tick_at = ancient

        # recv 한 번 timeout → 그 후 stop으로 loop 빠져나오게
        recv_calls = [0]

        async def recv_side_effect():
            recv_calls[0] += 1
            if recv_calls[0] >= 2:
                client._stop.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)
        mock_connect = AsyncMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect.__aexit__ = AsyncMock(return_value=None)

        with patch("app.crawlers.krx_kis.websockets.connect", return_value=mock_connect), \
             patch("app.crawlers.krx_kis.get_active_session", return_value="CM"), \
             self.assertLogs("app.crawlers.krx_kis", level="INFO"):
            await client._connect_and_listen("CM", "test_key")

        # _last_tick_at이 ancient에서 fresh로 reset됨 (subscribe 직후)
        self.assertGreater(
            client._last_tick_at, ancient + 25 * 3600,
            "subscribe 후 _last_tick_at이 reset되어야 (carry-over 차단)",
        )
        self.assertLess(time.time() - client._last_tick_at, 5)
        # status는 normal 유지 — carry-over로 stale 가지 않음
        self.assertEqual(
            client._status, "normal",
            "carry-over _last_tick_at이 reset되어 stale 전이 발생 X",
        )

    async def test_60s_no_tick_after_subscribe_still_goes_stale(self):
        """subscribe 후 60s+ tick 무수신 → stale 정상 감지 (회귀 방지).

        PR6e의 _last_tick_at = time.time() reset이 기존 stale 감지(line 384-389)를
        깨지 않는지 검증. 시간 흐름은 _last_tick_at 강제 변경으로 시뮬레이션.
        get_active_session은 항상 "CM" 반환하도록 patch (실제 wall-clock이
        active session 외 시점이어도 테스트 안정).
        """
        client = KisFuturesClient(approval_manager=MagicMock(spec=KisApprovalManager))

        recv_calls = [0]

        async def recv_side_effect():
            recv_calls[0] += 1
            if recv_calls[0] == 1:
                # 첫 timeout 후 시간 흐름 시뮬레이션 — _last_tick_at을 STALE 임계 초과로
                client._last_tick_at = time.time() - (STALE_AFTER_SEC + 40)
            elif recv_calls[0] >= 2:
                client._stop.set()
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)
        mock_connect = AsyncMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect.__aexit__ = AsyncMock(return_value=None)

        with patch("app.crawlers.krx_kis.websockets.connect", return_value=mock_connect), \
             patch("app.crawlers.krx_kis.get_active_session", return_value="CM"), \
             self.assertLogs("app.crawlers.krx_kis", level="INFO"):
            await client._connect_and_listen("CM", "test_key")

        # 두 번째 iteration의 stale check가 트리거 → status stale
        self.assertEqual(
            client._status, "stale",
            "subscribe 후 60s+ tick 무수신 시 stale 정상 감지되어야 (회귀 방지)",
        )


# ---------------------------------------------------------------------------
# PR6d-1 — KisAccessTokenManager (REST access_token 발급/캐시)
# ---------------------------------------------------------------------------

class TestAccessTokenManagerCache(unittest.TestCase):
    """KisAccessTokenManager cache 로딩/검증/저장 — KisApprovalManager 패턴."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "kis_access_token.json"
        self.mgr = KisAccessTokenManager(
            app_key="test_key", app_secret="test_secret", cache_path=self.cache_path,
        )

    def tearDown(self):
        if self.cache_path.exists():
            self.cache_path.unlink()
        Path(self.tmpdir).rmdir()

    def test_load_cache_missing(self):
        self.assertIsNone(self.mgr._load_cache())

    def test_load_cache_corrupt(self):
        self.cache_path.write_text("{invalid json")
        with self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            self.assertIsNone(self.mgr._load_cache())
        self.assertTrue(any("캐시 읽기 실패" in m for m in cm.output))

    def test_save_cache_chmod_600(self):
        payload = {
            "access_token": "tok123",
            "expires_at_epoch": time.time() + 86400,
            "created_at_epoch": time.time(),
        }
        self.mgr._save_cache(payload)
        self.assertTrue(self.cache_path.exists())
        mode = stat.S_IMODE(self.cache_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        loaded = json.loads(self.cache_path.read_text())
        self.assertEqual(loaded["access_token"], "tok123")

    def test_is_fresh_within_margin_returns_false(self):
        cached = {"expires_at_epoch": time.time() + ACCESS_TOKEN_REFRESH_MARGIN_SEC - 10}
        self.assertFalse(self.mgr._is_fresh(cached))

    def test_is_fresh_well_before_margin_returns_true(self):
        cached = {"expires_at_epoch": time.time() + 7200}
        self.assertTrue(self.mgr._is_fresh(cached))

    def test_is_still_valid_not_expired(self):
        cached = {"expires_at_epoch": time.time() + 60}
        self.assertTrue(self.mgr._is_still_valid(cached))

    def test_is_still_valid_expired(self):
        cached = {"expires_at_epoch": time.time() - 10}
        self.assertFalse(self.mgr._is_still_valid(cached))


class TestAccessTokenManagerGetToken(unittest.IsolatedAsyncioTestCase):
    """KisAccessTokenManager get_access_token — 발급 / 캐시 hit / fallback."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "kis_access_token.json"
        self.mgr = KisAccessTokenManager(
            app_key="test_key", app_secret="test_secret", cache_path=self.cache_path,
        )

    def tearDown(self):
        if self.cache_path.exists():
            self.cache_path.unlink()
        Path(self.tmpdir).rmdir()

    async def test_returns_cached_when_fresh(self):
        """fresh cache 있으면 새 발급 안 함."""
        self.cache_path.write_text(json.dumps({
            "access_token": "cached_tok",
            "expires_at_epoch": time.time() + 86400,
            "created_at_epoch": time.time(),
        }))
        with patch.object(self.mgr, "_issue_new") as mock_issue:
            result = await self.mgr.get_access_token()
        self.assertEqual(result, "cached_tok")
        mock_issue.assert_not_called()

    async def test_issues_new_when_near_expiry(self):
        """만료 임박 cache → 새 발급."""
        self.cache_path.write_text(json.dumps({
            "access_token": "old_tok",
            "expires_at_epoch": time.time() + 60,  # 만료 1분 남음 (마진 5분 미만)
            "created_at_epoch": time.time(),
        }))
        with patch.object(
            self.mgr, "_issue_new",
            return_value={
                "access_token": "new_tok",
                "expires_at_epoch": time.time() + 86400,
                "created_at_epoch": time.time(),
            },
        ):
            result = await self.mgr.get_access_token()
        self.assertEqual(result, "new_tok")

    async def test_falls_back_to_cache_when_issue_fails(self):
        """발급 실패 + valid cache → cache fallback (KRX optional 격리)."""
        self.cache_path.write_text(json.dumps({
            "access_token": "fallback_tok",
            "expires_at_epoch": time.time() + 60,  # not fresh, 하지만 still_valid
            "created_at_epoch": time.time(),
        }))
        with patch.object(
            self.mgr, "_issue_new", side_effect=RuntimeError("KIS API down"),
        ), self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            result = await self.mgr.get_access_token()
        self.assertEqual(result, "fallback_tok")
        self.assertTrue(any("발급 실패" in m for m in cm.output))


# ---------------------------------------------------------------------------
# PR6d-1 — fetch_kis_futures_quote (REST snapshot helper)
# ---------------------------------------------------------------------------

class TestFetchKisFuturesQuote(unittest.IsolatedAsyncioTestCase):
    """REST inquire-price helper — token 사용 / output parse / 실패 처리."""

    def setUp(self):
        self.contract = ContractInfo(
            short_code="A75605",
            standard_code="KR4A75650007",
            name="미국달러 F 202605",
            contract_month="202605",
            expiry_date=date(2026, 5, 18),
        )
        self.token_mgr = MagicMock(spec=KisAccessTokenManager)
        self.token_mgr.get_access_token = AsyncMock(return_value="test_token")
        self.token_mgr._app_key = "test_key"
        self.token_mgr._app_secret = "test_secret"

    async def test_normal_returns_normalized_payload(self):
        """rt_cd=0 + output 정상 → normalized dict 반환 (CF market code)."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "정상처리되었습니다.",
            "output": {
                "futs_prpr": "1457.40007441",
                "futs_shrn_iscd": "A75605",
            },
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.requests.get", return_value=mock_response):
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr, session="CF",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "krx")
        self.assertEqual(result["asset"], "usd-krw-futures")
        self.assertEqual(result["contract_code"], "A75605")
        self.assertEqual(result["contract_month"], "202605")
        self.assertEqual(result["expires_on"], "2026-05-18")
        self.assertEqual(result["session"], "CF")
        self.assertEqual(result["market_div_code"], "CF")
        # raw price 그대로 (호출자가 Decimal 정규화)
        self.assertEqual(result["price"], "1457.40007441")
        self.assertIn("received_at", result)

    async def test_cm_session_uses_cm_market_code(self):
        """야간세션(CM)은 FID_COND_MRKT_DIV_CODE=CM으로 조회."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "futs_prpr": "1447.70",
                "hts_kor_isnm": "미국달러 F 202605",
            },
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.requests.get", return_value=mock_response) as mock_get:
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr, session="CM",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["price"], "1447.70")
        self.assertEqual(result["session"], "CM")
        self.assertEqual(result["market_div_code"], "CM")
        self.assertEqual(mock_get.call_args.kwargs["params"]["FID_COND_MRKT_DIV_CODE"], "CM")

    async def test_none_session_uses_active_session(self):
        """session 미지정 시 현재 active session 판정으로 market code 선택."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"rt_cd": "0", "output1": {"futs_prpr": "1450.0"}}
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.get_active_session", return_value="CM"), \
             patch("app.crawlers.krx_kis.requests.get", return_value=mock_response) as mock_get:
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["session"], "CM")
        self.assertEqual(mock_get.call_args.kwargs["params"]["FID_COND_MRKT_DIV_CODE"], "CM")

    async def test_no_active_session_returns_none_without_token_or_http(self):
        """휴장/break 중에는 REST snapshot을 호출하지 않는다."""
        with patch("app.crawlers.krx_kis.get_active_session", return_value=None), \
             patch("app.crawlers.krx_kis.requests.get") as mock_get, \
             self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr,
            )
        self.assertIsNone(result)
        self.token_mgr.get_access_token.assert_not_awaited()
        mock_get.assert_not_called()
        self.assertTrue(any("active session 부재" in m for m in cm.output))

    async def test_rt_cd_error_returns_none(self):
        """rt_cd != 0 → None + warning 로그."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "Some error",
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.requests.get", return_value=mock_response), \
             self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr, session="CF",
            )
        self.assertIsNone(result)
        self.assertTrue(any("inquire-price rt_cd=1" in m for m in cm.output))

    async def test_missing_output_returns_none(self):
        """output dict 부재 → None + warning."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"rt_cd": "0"}  # no output
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.requests.get", return_value=mock_response), \
             self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr, session="CF",
            )
        self.assertIsNone(result)
        self.assertTrue(any("output dict 부재" in m for m in cm.output))

    async def test_missing_price_field_returns_none(self):
        """output dict는 있지만 futs_prpr/prpr 없음 → None + warning."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": {"futs_shrn_iscd": "A75605"},  # no price field
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.requests.get", return_value=mock_response), \
             self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr, session="CF",
            )
        self.assertIsNone(result)
        self.assertTrue(any("futs_prpr/prpr 필드 부재" in m for m in cm.output))

    async def test_index_market_response_is_not_usd_futures_quote(self):
        """운영 smoke 형태의 지수 출력(bstp_nmix_prpr)은 KRX USD futures로 쓰지 않는다."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "정상처리되었습니다.",
            "output1": {},
            "output2": {
                "bstp_cls_code": "0001",
                "hts_kor_isnm": "종합",
                "bstp_nmix_prpr": "7384.56",
            },
            "output3": {
                "bstp_cls_code": "2001",
                "hts_kor_isnm": "KOSPI200",
                "bstp_nmix_prpr": "1129.63",
            },
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.requests.get", return_value=mock_response), \
             self.assertLogs("app.crawlers.krx_kis", level="WARNING") as cm:
            result = await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr, session="CF",
            )
        self.assertIsNone(result)
        self.assertTrue(any("futs_prpr/prpr 필드 부재" in m for m in cm.output))
        self.assertTrue(any("bstp_nmix_prpr" in m for m in cm.output))

    async def test_uses_correct_endpoint_and_headers(self):
        """endpoint / Bearer token / appkey / tr_id / params 올바르게 전달."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": {"futs_prpr": "1450.0"},
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.crawlers.krx_kis.requests.get", return_value=mock_response) as mock_get:
            await fetch_kis_futures_quote(
                contract=self.contract, token_manager=self.token_mgr, session="CF",
            )
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args.kwargs
        # URL: KIS_PROD_HOST + KIS_REST_QUOTE_PATH
        self.assertEqual(mock_get.call_args.args[0], f"{KIS_PROD_HOST}{KIS_REST_QUOTE_PATH}")
        # Bearer token + appkey + appsecret + tr_id
        headers = call_kwargs["headers"]
        self.assertEqual(headers["authorization"], "Bearer test_token")
        self.assertEqual(headers["appkey"], "test_key")
        self.assertEqual(headers["appsecret"], "test_secret")
        self.assertEqual(headers["tr_id"], KIS_REST_QUOTE_TR_ID)
        # params: 상품선물 정규세션 market code + contract.short_code
        params = call_kwargs["params"]
        self.assertEqual(params["FID_COND_MRKT_DIV_CODE"], "CF")
        self.assertEqual(params["FID_INPUT_ISCD"], "A75605")
        self.assertEqual(KIS_REST_QUOTE_MARKET_DIV_CODE, {"CF": "CF", "CM": "CM"})


# ---------------------------------------------------------------------------
# PR6d-2a — raw frame metric / status observability
# ---------------------------------------------------------------------------

class TestKisFuturesClientMetrics(unittest.IsolatedAsyncioTestCase):
    """KisFuturesClient metric state — frame counter / gap bucket / status transition.

    PR6d-2a (ADR-027) 회귀 방지. REST fallback / Redis / Stage 2 변경 X —
    측정만.
    """

    def setUp(self):
        self.client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager),
        )

    async def test_dispatch_tick_separates_trade_and_quote_counters(self):
        """체결 tick → trade_frame_count / 호가 tick → quote_frame_count 분리."""
        # 체결 tick — H0MFCNT0
        cnt_raw = "^".join([
            "A75605", "180332", "6.30", "2", "0.43", "1468.50",
        ] + ["0"] * 43)
        await self.client._dispatch_tick("H0MFCNT0", cnt_raw, "CM")
        # 호가 tick — H0MFASP0
        asp_raw = "^".join(["A75605", "180332"] + ["0"] * 36)
        await self.client._dispatch_tick("H0MFASP0", asp_raw, "CM")

        m = self.client.get_metrics()
        self.assertEqual(m["counters"]["trade_frame"], 1)
        self.assertEqual(m["counters"]["quote_frame"], 1)
        self.assertEqual(m["counters"]["frame_total"], 2)
        # last_*_at 분리
        self.assertIsNotNone(self.client._liveness.last_trade_frame_at)
        self.assertIsNotNone(self.client._liveness.last_quote_frame_at)

    def test_status_transition_count_increments(self):
        """_set_status normal→stale→normal 순서로 transition counter 증가."""
        # 초기: 모두 0
        m0 = self.client.get_metrics()
        self.assertEqual(m0["counters"]["status_transitions"]["stale"], 0)

        with self.assertLogs("app.crawlers.krx_kis", level="INFO"):
            self.client._set_status("stale")
            self.client._set_status("normal")
            self.client._set_status("stale")

        m = self.client.get_metrics()
        self.assertEqual(m["counters"]["status_transitions"]["stale"], 2)
        # normal 카운터: setUp 시 default normal에서 stale로 전이 후 normal로
        # 1회 전이됨 (시작 시 normal은 transition 아님)
        self.assertEqual(m["counters"]["status_transitions"]["normal"], 1)

    def test_gap_bucket_assignment_correctness(self):
        """gap_sec → bucket 매핑 invariant. 5s gap → <=10s bucket."""
        cases = [
            (0.5, "<=1s"), (1.0, "<=1s"),
            (1.5, "<=2s"), (2.0, "<=2s"),
            (3.0, "<=5s"), (5.0, "<=5s"),
            (5.001, "<=10s"), (10.0, "<=10s"),
            (15.0, "<=30s"), (30.0, "<=30s"),
            (45.0, "<=60s"), (60.0, "<=60s"),
            (60.001, ">60s"), (300.0, ">60s"),
        ]
        # _bucket_for는 KRX_FANOUT_REFACTOR_PLAN 5.1.A에서 KrxLivenessMonitor로 이전.
        from app.crawlers.krx_kis import KrxLivenessMonitor
        for gap, expected in cases:
            self.assertEqual(
                KrxLivenessMonitor._bucket_for(gap), expected,
                f"gap={gap} should map to {expected}",
            )

    async def test_get_metrics_shape_and_lifecycle(self):
        """get_metrics() 반환 dict shape + lifecycle / contract / counters / buckets."""
        m = self.client.get_metrics()
        # 최상위 키
        self.assertIn("status", m)
        self.assertIn("active_session", m)
        self.assertIn("contract", m)
        self.assertIn("lifecycle", m)
        self.assertIn("last_frame_age_sec", m)
        self.assertIn("last_trade_frame_age_sec", m)
        self.assertIn("last_quote_frame_age_sec", m)
        self.assertIn("counters", m)
        self.assertIn("gap_buckets", m)
        self.assertIn("max_gap_sec", m)

        # lifecycle
        self.assertIn("started_at", m["lifecycle"])
        self.assertIn("uptime_sec", m["lifecycle"])
        # contract metadata
        self.assertEqual(m["contract"]["code"], TR_KEY_STATIC)
        # 초기 상태: tick 0건
        self.assertIsNone(m["last_frame_age_sec"])
        self.assertEqual(m["counters"]["frame_total"], 0)
        # gap bucket 키 invariant
        for layer in ("total", "trade", "quote"):
            self.assertEqual(
                sorted(m["gap_buckets"][layer].keys()),
                sorted(["<=1s", "<=2s", "<=5s", "<=10s", "<=30s", "<=60s", ">60s"]),
            )

    @unittest.skip(
        "main.py import는 firebase_admin 의존 + lifespan side effect로 단위 테스트 부담. "
        "/admin/api/krx-status 분기 검증은 운영 배포 후 admin 페이지 직접 호출로 cover (ADR-027 운영 검증 기준)."
    )
    async def test_admin_endpoint_branches(self):
        """ADR-027 PR6d-2a Test matrix 5번째 (admin endpoint 분기) — 운영 검증으로 분리."""
        pass

    def test_get_metrics_includes_iso_timestamps(self):
        """PR6d-2a follow-up — lifecycle / last_*_at에 epoch + KST ISO 둘 다."""
        m = self.client.get_metrics()
        # lifecycle: started_at은 항상 존재 (init 시점 epoch float)
        self.assertIsInstance(m["lifecycle"]["started_at"], float)
        self.assertIsInstance(m["lifecycle"]["started_at_kst"], str)
        # ISO 형식 sanity check (datetime.fromisoformat 파싱 가능)
        from datetime import datetime as dt
        parsed = dt.fromisoformat(m["lifecycle"]["started_at_kst"])
        self.assertIsNotNone(parsed.tzinfo)

        # connected_at은 None (아직 connect 안 함)
        self.assertIsNone(m["lifecycle"]["connected_at"])
        self.assertIsNone(m["lifecycle"]["connected_at_kst"])

        # last_*_at도 None (tick 0건)
        self.assertIsNone(m["last_frame_at"])
        self.assertIsNone(m["last_frame_at_kst"])
        self.assertIsNone(m["last_trade_frame_at"])
        self.assertIsNone(m["last_trade_frame_at_kst"])
        self.assertIsNone(m["last_quote_frame_at"])
        self.assertIsNone(m["last_quote_frame_at_kst"])

    async def test_get_metrics_iso_after_tick(self):
        """tick 1건 도착 후 last_frame_at + ISO 둘 다 채워짐."""
        cnt_raw = "^".join([
            "A75605", "180332", "0", "0", "0", "1468.0",
        ] + ["0"] * 43)
        await self.client._dispatch_tick("H0MFCNT0", cnt_raw, "CM")

        m = self.client.get_metrics()
        self.assertIsNotNone(m["last_frame_at"])
        self.assertIsNotNone(m["last_frame_at_kst"])
        self.assertIsNotNone(m["last_trade_frame_at"])
        self.assertIsNotNone(m["last_trade_frame_at_kst"])
        # 호가 tick 안 옴 → quote_at은 None
        self.assertIsNone(m["last_quote_frame_at"])
        self.assertIsNone(m["last_quote_frame_at_kst"])
        # ISO 파싱 가능
        from datetime import datetime as dt
        parsed = dt.fromisoformat(m["last_frame_at_kst"])
        self.assertIsNotNone(parsed.tzinfo)

    async def test_summary_log_skipped_during_no_active_session(self):
        """active_session=None일 때 _summary_log_loop emit 안 함 (noise 방지).

        실제 `_summary_log_loop()` 호출 + asyncio.sleep patch (60s → 0.05s)로
        wrap 흉내 없이 회귀 방지 — Codex 외부 검토 정정 (PR6d-2a follow-up).
        """
        self.client._active_session = None
        self.client._liveness.frame_count_total = 100

        original_sleep = asyncio.sleep

        async def fast_sleep(t):
            await original_sleep(0.05 if t == 60 else t)

        with patch("app.crawlers.krx_kis.asyncio.sleep", side_effect=fast_sleep), \
             self.assertNoLogs("app.crawlers.krx_kis", level="INFO"):
            task = asyncio.create_task(self.client._summary_log_loop())
            # 첫 iteration 후 cancel — active_session=None 분기에서 emit 없이 continue
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_summary_log_emits_during_active_session(self):
        """active_session 설정된 상태에서 summary log loop이 INFO emit."""
        # _summary_log_loop의 sleep을 0.05s로 축소 + 1회 iteration 후 cancel
        self.client._active_session = "CM"
        self.client._liveness.frame_count_total = 50

        original_sleep = asyncio.sleep

        async def fast_sleep(t):
            # 60s → 0.05s
            await original_sleep(0.05 if t == 60 else t)

        with patch("app.crawlers.krx_kis.asyncio.sleep", side_effect=fast_sleep), \
             self.assertLogs("app.crawlers.krx_kis", level="INFO") as cm:
            task = asyncio.create_task(self.client._summary_log_loop())
            # 첫 iteration 후 cancel
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # metrics summary 1줄 출력 확인
        self.assertTrue(any("[kis_ws] metrics session=CM" in m for m in cm.output))

    async def test_max_gap_sec_tracks_largest_inter_tick_gap(self):
        """max_gap_sec — 두 tick 사이 가장 긴 gap 추적 (전체/체결/호가)."""
        cnt_raw = "^".join([
            "A75605", "180332", "0", "0", "0", "1468.0",
        ] + ["0"] * 43)
        # 첫 tick → gap 측정 X (이전 tick 없음)
        await self.client._dispatch_tick("H0MFCNT0", cnt_raw, "CM")
        # _last_tick_at을 5초 전으로 강제 → 다음 tick에서 ~5s gap 측정
        self.client._last_tick_at = time.time() - 5
        self.client._liveness.last_trade_frame_at = time.time() - 5
        await self.client._dispatch_tick("H0MFCNT0", cnt_raw, "CM")

        m = self.client.get_metrics()
        # 체결 5s gap → <=5s bucket 또는 <=10s bucket (시간 정확도 ±)
        # max_trade_gap_sec ≈ 5
        self.assertGreater(m["max_gap_sec"]["trade"], 4.5)
        self.assertLess(m["max_gap_sec"]["trade"], 6.0)
        # gap bucket entry 1 추가됨 (5s 근처 → <=5s 또는 <=10s)
        bucket_total = sum(m["gap_buckets"]["trade"].values())
        self.assertEqual(bucket_total, 1)


# ---------------------------------------------------------------------------
# PR6d-2a follow-up (2026-05-07) — active-session gap metric reset
# session break(예: CM 06:00 종료 → CF 08:30 진입, 2.5h)가 max_*_gap_sec에
# 9000초대 오염값으로 잡히는 문제 fix. lifetime counter는 유지.
# ---------------------------------------------------------------------------

class TestActiveSessionGapMetricReset(unittest.IsolatedAsyncioTestCase):
    """_reset_active_session_gap_metrics() — boundary 오염 제거 + PR6e 호환."""

    def setUp(self):
        self.client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager),
        )

    def test_session_reset_clears_active_metrics(self):
        """reset 후 max_*_gap=0 / last_*_at=None / buckets 초기화."""
        # 오염값 시뮬레이션
        self.client._last_tick_at = time.time()
        self.client._liveness.last_trade_frame_at = time.time() - 100
        self.client._liveness.last_quote_frame_at = time.time() - 50
        self.client._liveness.max_frame_gap_sec = 9001.0  # 세션 break 오염값
        self.client._liveness.max_trade_gap_sec = 9899.0
        self.client._liveness.max_quote_gap_sec = 9001.0
        self.client._liveness.gap_buckets_total = {"<=1s": 5, "<=2s": 0, "<=5s": 0,
                                          "<=10s": 0, "<=30s": 0, "<=60s": 0,
                                          ">60s": 3}

        self.client._reset_active_session_gap_metrics()

        # 모두 reset
        self.assertIsNone(self.client._last_tick_at)
        self.assertIsNone(self.client._liveness.last_trade_frame_at)
        self.assertIsNone(self.client._liveness.last_quote_frame_at)
        self.assertEqual(self.client._liveness.max_frame_gap_sec, 0.0)
        self.assertEqual(self.client._liveness.max_trade_gap_sec, 0.0)
        self.assertEqual(self.client._liveness.max_quote_gap_sec, 0.0)
        # buckets 초기화 — 모두 0
        for layer in (self.client._liveness.gap_buckets_total,
                      self.client._liveness.gap_buckets_trade,
                      self.client._liveness.gap_buckets_quote):
            self.assertEqual(sum(layer.values()), 0)

    def test_session_reset_preserves_lifetime_counters(self):
        """lifetime counters는 reset 안 됨 (frame_total, status_transitions 등)."""
        # 누적 카운터 시뮬레이션
        self.client._liveness.frame_count_total = 12345
        self.client._liveness.trade_frame_count = 678
        self.client._liveness.quote_frame_count = 11000
        self.client._liveness.system_frame_count = 100
        self.client._liveness.malformed_frame_count = 2
        self.client._liveness.unknown_tr_id_count = 1
        self.client._status_transition_count = {
            "normal": 5, "reconnecting": 3, "stale": 2,
        }
        self.client._reconnect_attempt_count = 4

        self.client._reset_active_session_gap_metrics()

        # lifetime은 그대로
        self.assertEqual(self.client._liveness.frame_count_total, 12345)
        self.assertEqual(self.client._liveness.trade_frame_count, 678)
        self.assertEqual(self.client._liveness.quote_frame_count, 11000)
        self.assertEqual(self.client._liveness.system_frame_count, 100)
        self.assertEqual(self.client._liveness.malformed_frame_count, 2)
        self.assertEqual(self.client._liveness.unknown_tr_id_count, 1)
        self.assertEqual(self.client._status_transition_count["stale"], 2)
        self.assertEqual(self.client._status_transition_count["normal"], 5)
        self.assertEqual(self.client._reconnect_attempt_count, 4)

    async def test_subscribe_resets_then_pr6e_grace_set(self):
        """⚠️ 핵심 회귀 방지: subscribe 후 reset → _last_tick_at = time.time() 순서.

        만약 reset만 하고 grace 설정 안 하면 PR6e carry-over fix가 깨진다
        (_last_tick_at=None은 falsy → stale check skip → 60s grace 의미 없음).
        실제 _connect_and_listen 흐름이 이 순서를 지키는지 검증.
        """
        # ancient _last_tick_at 시뮬레이션 (이전 세션 carry-over)
        ancient = time.time() - 26 * 3600
        self.client._last_tick_at = ancient
        self.client._liveness.max_frame_gap_sec = 9001.0  # 오염값

        client = self.client
        client._stop.set()  # subscribe만 하고 즉시 빠져나오게

        recv_calls = [0]

        async def recv_side_effect():
            recv_calls[0] += 1
            raise asyncio.TimeoutError()

        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=recv_side_effect)
        mock_connect = AsyncMock()
        mock_connect.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_connect.__aexit__ = AsyncMock(return_value=None)

        with patch("app.crawlers.krx_kis.websockets.connect", return_value=mock_connect), \
             patch("app.crawlers.krx_kis.get_active_session", return_value="CM"), \
             self.assertLogs("app.crawlers.krx_kis", level="INFO"):
            await client._connect_and_listen("CM", "test_key")

        # reset 후 _last_tick_at은 time.time()으로 grace 설정됨 (None 아님)
        self.assertIsNotNone(client._last_tick_at, "PR6e grace가 깨졌음")
        self.assertGreater(
            client._last_tick_at, ancient + 25 * 3600,
            "ancient carry-over가 reset되지 않음",
        )
        self.assertLess(time.time() - client._last_tick_at, 5)
        # max_gap 오염값 제거됨
        self.assertEqual(client._liveness.max_frame_gap_sec, 0.0)

    async def test_session_boundary_clears_gap_for_next_session(self):
        """첫 session에서 9000s gap이 다음 session max에 남지 않음 (시뮬레이션)."""
        # 첫 session 활동 시뮬레이션 — 큰 gap 누적
        self.client._liveness.max_frame_gap_sec = 9001.0
        self.client._liveness.max_trade_gap_sec = 9899.0
        self.client._liveness.max_quote_gap_sec = 9001.0
        self.client._liveness.gap_buckets_total[">60s"] = 5  # 오염값 bucket

        # session boundary 진입 (휴장) → reset
        self.client._reset_active_session_gap_metrics()

        # 다음 session 시뮬레이션 — 5초 gap 한 번
        self.client._last_tick_at = time.time() - 5
        cnt_raw = "^".join([
            "A75605", "180332", "0", "0", "0", "1468.0",
        ] + ["0"] * 43)
        await self.client._dispatch_tick("H0MFCNT0", cnt_raw, "CM")

        # 다음 session max는 5s 근처 (9000s 잔존 X)
        self.assertGreater(self.client._liveness.max_frame_gap_sec, 4.5)
        self.assertLess(self.client._liveness.max_frame_gap_sec, 6.0)
        # >60s bucket은 reset 후 0이어야 함 (5s gap이라 <=5s 또는 <=10s)
        self.assertEqual(self.client._liveness.gap_buckets_total[">60s"], 0)

    async def test_no_active_session_resets_metrics(self):
        """start() 휴장 분기 실제 경로 검증 — get_active_session=None 시 reset 호출.

        Codex 외부 검토 정정 (2026-05-07): helper 직접 호출은 회귀 방지 0
        (start() 분기에서 reset 호출이 빠져도 통과). 실제 start() 흐름에서
        휴장 분기 진입 시 reset이 호출되는지 검증해야 회귀 방지.

        구현: get_active_session patch + asyncio.sleep 빠르게 + _stop으로
        한 iteration 후 종료.
        """
        # 오염값 + 카운터 누적 (휴장 진입 전 상태 시뮬레이션)
        self.client._liveness.max_frame_gap_sec = 9001.0
        self.client._liveness.max_trade_gap_sec = 9899.0
        self.client._liveness.frame_count_total = 100

        original_sleep = asyncio.sleep

        async def fast_sleep(t):
            # start()의 30s sleep을 짧게, summary_log_loop의 60s도 짧게
            await original_sleep(0.05 if t in (30, 60) else t)

        async def stop_after_iteration():
            # 첫 iteration 후 stop 신호
            await original_sleep(0.15)
            self.client._stop.set()

        with patch("app.crawlers.krx_kis.get_active_session", return_value=None), \
             patch("app.crawlers.krx_kis.asyncio.sleep", side_effect=fast_sleep):
            stop_task = asyncio.create_task(stop_after_iteration())
            await self.client.start()
            await stop_task

        # 휴장 분기에서 reset 호출됨 → max_gap 0
        self.assertEqual(
            self.client._liveness.max_frame_gap_sec, 0.0,
            "start() 휴장 분기에서 _reset_active_session_gap_metrics() 호출 안 됨",
        )
        self.assertEqual(self.client._liveness.max_trade_gap_sec, 0.0)
        # lifetime은 유지
        self.assertEqual(self.client._liveness.frame_count_total, 100)
        # active_session도 None으로 정리됨
        self.assertIsNone(self.client._active_session)


# ---------------------------------------------------------------------------
# Fix (2026-06-10) — silent-session reconnect (_check_silent_session)
# 6/9 사고 회귀 잠금: data frame 침묵 + PINGPONG TCP 유지 → reconnect 0 고착
# ---------------------------------------------------------------------------

class TestCheckSilentSession(unittest.TestCase):
    """_check_silent_session 판정 단위 — raise/skip 경계."""

    def setUp(self):
        self.client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager)
        )
        # 장중 (CF, close grace 아님)
        self.intraday_kst = datetime(2026, 6, 10, 11, 0, 0)

    def test_constants_invariant(self):
        # default 150s + stale 라벨 임계(60s)보다 큼 (config 가드와 동일 invariant)
        self.assertEqual(SILENT_RECONNECT_SEC, 150)
        self.assertGreater(SILENT_RECONNECT_SEC, STALE_AFTER_SEC)

    def test_no_last_tick_no_raise(self):
        self.client._last_tick_at = None
        self.client._check_silent_session(
            now_epoch=time.time(), now_kst=self.intraday_kst, session="CF"
        )

    def test_fresh_tick_no_raise(self):
        now = time.time()
        self.client._last_tick_at = now - 5
        self.client._check_silent_session(
            now_epoch=now, now_kst=self.intraday_kst, session="CF"
        )

    def test_stale_label_range_no_raise(self):
        # 60s(stale 라벨) 초과 ~ 150s 이하: 라벨만, reconnect 액션 없음 (2단 분리)
        now = time.time()
        self.client._last_tick_at = now - (STALE_AFTER_SEC + 10)
        self.client._check_silent_session(
            now_epoch=now, now_kst=self.intraday_kst, session="CF"
        )

    def test_exactly_threshold_no_raise(self):
        # 경계값: silence == SILENT_RECONNECT_SEC는 raise 안 함 (strict >)
        now = time.time()
        self.client._last_tick_at = now - SILENT_RECONNECT_SEC
        self.client._check_silent_session(
            now_epoch=now, now_kst=self.intraday_kst, session="CF"
        )

    def test_silent_over_threshold_raises(self):
        now = time.time()
        self.client._last_tick_at = now - (SILENT_RECONNECT_SEC + 1)
        with self.assertRaises(_KrxSilentSessionError):
            self.client._check_silent_session(
                now_epoch=now, now_kst=self.intraday_kst, session="CF"
            )

    def test_system_frame_does_not_refresh_last_tick(self):
        # 6/9 사고 핵심: PINGPONG/system frame은 data freshness가 아님 —
        # _handle_frame system 경로는 observe_tick 미호출 → _last_tick_at 불변.
        self.client._last_tick_at = 12345.0
        raw = json.dumps({"header": {"tr_id": "PINGPONG"}, "body": {}})
        asyncio.run(self.client._handle_frame(raw, "CF"))
        self.assertEqual(self.client._last_tick_at, 12345.0)

    def test_close_grace_window_skips_raise(self):
        # CF close grace (15:45:00~15:45:59) — silent여도 skip (close capture 보호)
        now = time.time()
        self.client._last_tick_at = now - (SILENT_RECONNECT_SEC + 100)
        grace_kst = datetime(2026, 6, 10, 15, 45, 30)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True):
            self.client._check_silent_session(
                now_epoch=now, now_kst=grace_kst, session="CF"
            )

    def test_close_grace_finalizer_disabled_raises(self):
        # finalizer off면 grace 의미 없음 — boundary 분기와 대칭으로 raise
        now = time.time()
        self.client._last_tick_at = now - (SILENT_RECONNECT_SEC + 100)
        grace_kst = datetime(2026, 6, 10, 15, 45, 30)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", False):
            with self.assertRaises(_KrxSilentSessionError):
                self.client._check_silent_session(
                    now_epoch=now, now_kst=grace_kst, session="CF"
                )

    def test_after_grace_window_raises(self):
        # grace 종료(15:46:00+) 후 helper 판정은 raise로 복귀 — helper-level 경계
        # 잠금. (실제 recv loop에선 boundary return이 silent check보다 먼저라
        # 이 시각엔 보통 도달하지 않음 — Codex 검토 관찰 반영)
        now = time.time()
        self.client._last_tick_at = now - (SILENT_RECONNECT_SEC + 100)
        after_grace_kst = datetime(2026, 6, 10, 15, 46, 30)
        with patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True):
            with self.assertRaises(_KrxSilentSessionError):
                self.client._check_silent_session(
                    now_epoch=now, now_kst=after_grace_kst, session="CF"
                )


class TestRunSessionSilentReconnect(unittest.IsolatedAsyncioTestCase):
    """_run_session이 _KrxSilentSessionError를 기존 reconnect 경로로 처리."""

    async def test_silent_error_triggers_backoff_reconnect(self):
        client = KisFuturesClient(
            approval_manager=MagicMock(spec=KisApprovalManager)
        )
        client._approval.get_approval_key = AsyncMock(return_value="key")
        calls = {"n": 0}

        async def fake_connect(session, approval_key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _KrxSilentSessionError("data silent 151s (test)")
            client._stop.set()  # 2번째 연결(재접속) 진입 확인 후 종료

        client._connect_and_listen = fake_connect
        with patch("app.crawlers.krx_kis.get_active_session", return_value="CF"):
            await asyncio.wait_for(client._run_session("CF"), timeout=10)

        # 예외 1회 → reconnect attempt 1 증가 + backoff 후 재접속 진입 (총 2회 호출)
        self.assertEqual(client._reconnect_attempt_count, 1)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(
            client._status_transition_count["reconnecting"], 1,
            "silent raise 후 status reconnecting 전이 1회",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
