"""F-1 (2026-05-26) — KrxAlertEvaluator + KrxAlertTickHandler 단위/통합 테스트.

Scope:
    - `KrxAlertEvaluator`가 `UsdtAlertEvaluator` thin subclass인지 (구조 확인).
    - `KrxAlertTickHandler` payload → `AlertObservation` 변환 정확성:
        - source/asset passthrough
        - price 0.1 KRW tick 정규화 (Decimal.quantize)
        - received_at KST naive ISO → epoch ms 변환 (KST→UTC)
        - aware ISO도 epoch ms 동일 (forward-compat)
        - 누락/malformed payload → defensive skip (격리)
        - flag=false defensive guard
    - Close grace tick **도** 평가됨 (mirror layer와 차이 잠금 — 종가 crossing 보존).
    - lifecycle: `KrxAlertTickHandler.close()` → evaluator.close() 위임.
    - `KisFuturesClient.stop()`이 `KrxAlertTickHandler.close()` 호출 (drain hook).
    - Scheduler: flag=true 시 handler 등록 / flag=false 시 미등록.

테스트 패턴은 `test_krx_stage_e.py` mirror — patch.object(config, ...) +
AsyncMock + IsolatedAsyncioTestCase.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from app import config
from app.crawlers.krx_kis import KrxAlertTickHandler
from app.notifications.alert_evaluator import (
    AlertObservation,
    KrxAlertEvaluator,
    UsdtAlertEvaluator,
)

KST = ZoneInfo("Asia/Seoul")


# ---------------------------------------------------------------------------
# KrxAlertEvaluator subclass 구조 (thin wrapper)
# ---------------------------------------------------------------------------

class TestKrxAlertEvaluatorSubclass(unittest.TestCase):
    """`KrxAlertEvaluator`가 `UsdtAlertEvaluator` thin subclass인지 확인."""

    def test_is_subclass_of_usdt_alert_evaluator(self):
        """`KrxAlertEvaluator`는 `UsdtAlertEvaluator` thin subclass."""
        self.assertTrue(issubclass(KrxAlertEvaluator, UsdtAlertEvaluator))

    def test_default_init_no_args(self):
        """no-arg init 가능 (default cache + coalescer)."""
        evaluator = KrxAlertEvaluator()
        self.assertIsInstance(evaluator, UsdtAlertEvaluator)


# ---------------------------------------------------------------------------
# KrxAlertTickHandler.__call__ — payload → AlertObservation 변환
# ---------------------------------------------------------------------------

class TestKrxAlertTickHandlerCall(unittest.IsolatedAsyncioTestCase):
    """Adapter 단위 — observation 변환 정확성 + defensive guard."""

    def _make_payload(
        self,
        *,
        price="1500.5",
        session="CF",
        received_at="2026-05-26T10:00:00",
        source="krx",
        asset="usd-krw-futures",
    ) -> dict:
        return {
            "source": source,
            "asset": asset,
            "price": price,
            "session": session,
            "received_at": received_at,
        }

    def _make_handler_with_mock_evaluator(self) -> KrxAlertTickHandler:
        """evaluator를 MagicMock으로 교체한 handler 생성."""
        handler = KrxAlertTickHandler()
        handler._evaluator = MagicMock(spec=KrxAlertEvaluator)
        return handler

    async def test_flag_false_defensive_early_return(self):
        """flag=false → defensive early return, evaluator.schedule 미호출."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", False):
            await handler(self._make_payload())

        handler._evaluator.schedule.assert_not_called()

    async def test_flag_true_schedules_observation(self):
        """flag=true → evaluator.schedule(AlertObservation) 호출."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler(self._make_payload())

        handler._evaluator.schedule.assert_called_once()
        obs = handler._evaluator.schedule.call_args.args[0]
        self.assertIsInstance(obs, AlertObservation)
        self.assertEqual(obs.source, "krx")
        self.assertEqual(obs.asset, "usd-krw-futures")
        self.assertEqual(obs.rate, 1500.5)
        self.assertEqual(obs.kind, "tick")

    async def test_kst_naive_received_at_converts_to_correct_epoch_ms(self):
        """KST naive ISO `2026-05-26T10:00:00` → KST 10:00 → UTC 01:00 epoch ms."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler(self._make_payload(received_at="2026-05-26T10:00:00"))

        obs = handler._evaluator.schedule.call_args.args[0]
        expected_dt = datetime(2026, 5, 26, 10, 0, 0, tzinfo=KST)
        expected_ms = int(expected_dt.timestamp() * 1000)
        self.assertEqual(obs.timestamp_ms, expected_ms)

    async def test_kst_aware_received_at_converts_correctly(self):
        """+09:00 aware → 같은 epoch ms (forward-compat)."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler(self._make_payload(received_at="2026-05-26T10:00:00+09:00"))

        obs = handler._evaluator.schedule.call_args.args[0]
        expected_dt = datetime(2026, 5, 26, 10, 0, 0, tzinfo=KST)
        expected_ms = int(expected_dt.timestamp() * 1000)
        self.assertEqual(obs.timestamp_ms, expected_ms)

    async def test_utc_aware_received_at_converts_correctly(self):
        """UTC aware → KST 기준 epoch ms 동일 (timezone 변환 없이 절대 시각 보존)."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            # 2026-05-26 01:00 UTC == 2026-05-26 10:00 KST
            await handler(self._make_payload(received_at="2026-05-26T01:00:00+00:00"))

        obs = handler._evaluator.schedule.call_args.args[0]
        expected_dt = datetime(2026, 5, 26, 10, 0, 0, tzinfo=KST)
        expected_ms = int(expected_dt.timestamp() * 1000)
        self.assertEqual(obs.timestamp_ms, expected_ms)

    async def test_price_normalization_0_1_tick(self):
        """KIS payload string `1457.40007441` → Decimal.quantize(0.1) = 1457.4."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler(self._make_payload(price="1457.40007441"))

        obs = handler._evaluator.schedule.call_args.args[0]
        self.assertEqual(obs.rate, 1457.4)

    async def test_missing_price_skips(self):
        """price 누락 → schedule 미호출 (defensive)."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler({"source": "krx", "asset": "usd-krw-futures",
                           "received_at": "2026-05-26T10:00:00"})

        handler._evaluator.schedule.assert_not_called()

    async def test_missing_received_at_skips(self):
        """received_at 누락 → schedule 미호출 (defensive)."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler({"source": "krx", "asset": "usd-krw-futures",
                           "price": "1500.5"})

        handler._evaluator.schedule.assert_not_called()

    async def test_malformed_received_at_skips(self):
        """received_at 파싱 실패 → schedule 미호출 (defensive)."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler(self._make_payload(received_at="not-an-iso-timestamp"))

        handler._evaluator.schedule.assert_not_called()

    async def test_malformed_price_skips(self):
        """price 파싱 실패 → schedule 미호출 + logger.warning."""
        handler = self._make_handler_with_mock_evaluator()
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            await handler(self._make_payload(price="not-a-decimal"))

        handler._evaluator.schedule.assert_not_called()

    async def test_close_grace_tick_still_evaluated(self):
        """**Regression lock**: close grace window 안 tick도 alert 평가 대상.

        mirror layer (`KrxRedisLatestWriter.__call__`)는 close grace tick skip
        하지만, alert는 사용자 알림 누락 방지 위해 **모든 tick 평가** (종가
        crossing 보존). 본 테스트가 close grace skip 도입 회귀 차단.
        """
        handler = self._make_handler_with_mock_evaluator()
        # CF close grace 15:45:00 ~ 15:45:59 안 tick
        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True):
            await handler(self._make_payload(
                session="CF", received_at="2026-05-26T15:45:30",
            ))

        handler._evaluator.schedule.assert_called_once()
        obs = handler._evaluator.schedule.call_args.args[0]
        # close grace tick도 정상 변환
        self.assertEqual(obs.source, "krx")
        self.assertEqual(obs.asset, "usd-krw-futures")
        self.assertEqual(obs.kind, "tick")

    async def test_evaluator_exception_isolated(self):
        """evaluator.schedule 예외 → 격리 (propagate X, WS session 유지)."""
        handler = self._make_handler_with_mock_evaluator()
        handler._evaluator.schedule.side_effect = RuntimeError("boom")

        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True):
            # 예외 propagate 안 됨 — handler가 잡고 logger.exception
            await handler(self._make_payload())

        # schedule은 시도되었음을 확인
        handler._evaluator.schedule.assert_called_once()


# ---------------------------------------------------------------------------
# KrxAlertTickHandler.close() — lifecycle drain
# ---------------------------------------------------------------------------

class TestKrxAlertTickHandlerClose(unittest.IsolatedAsyncioTestCase):
    """Lifecycle drain — KisFuturesClient.stop()에서 호출."""

    async def test_close_drains_evaluator(self):
        """handler.close() → evaluator.close(timeout=5.0) 위임."""
        handler = KrxAlertTickHandler()
        handler._evaluator = MagicMock(spec=KrxAlertEvaluator)
        handler._evaluator.close = AsyncMock()

        await handler.close(timeout=5.0)

        handler._evaluator.close.assert_awaited_once_with(timeout=5.0)

    async def test_close_exception_isolated(self):
        """evaluator.close() 예외 → 격리 (propagate X, shutdown 흐름 유지)."""
        handler = KrxAlertTickHandler()
        handler._evaluator = MagicMock(spec=KrxAlertEvaluator)
        handler._evaluator.close = AsyncMock(side_effect=RuntimeError("drain boom"))

        # 예외 propagate 안 됨 — logger.exception만 남고 shutdown 계속
        await handler.close(timeout=5.0)

        handler._evaluator.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Scheduler — flag 분기 등록 (test_krx_stage_e mirror)
# ---------------------------------------------------------------------------

class TestKrxAlertTickHandlerSchedulerRegistration(unittest.IsolatedAsyncioTestCase):
    """Scheduler bootstrap이 KRX_ALERT_EVALUATOR_ENABLED flag 분기 정확히 등록."""

    async def test_flag_false_no_alert_handler(self):
        """flag=false: KrxAlertTickHandler 미등록 (default 동작)."""
        from app import scheduler as scheduler_mod

        added_handlers = []

        class _FakeClient:
            def add_tick_handler(self, h):
                added_handlers.append(h)

        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", False), \
             patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.dict("os.environ", {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}), \
             patch("app.crawlers.krx_kis.KisFuturesClient",
                   return_value=_FakeClient()), \
             patch("app.crawlers.krx_kis.KisApprovalManager"), \
             patch("app.crawlers.krx_kis.KisAccessTokenManager"), \
             patch("app.scheduler.resolve_active_krx_futures_contract",
                   new=AsyncMock(return_value=MagicMock(
                       short_code="A75605",
                       expiry_date=__import__("datetime").date(2026, 6, 15),
                   ))), \
             patch("asyncio.create_task",
                   side_effect=lambda coro: (coro.close(), MagicMock())[1]):
            await scheduler_mod._bootstrap_krx_futures_client()

        handler_types = [type(h).__name__ for h in added_handlers]
        self.assertIn("KrxDbWriter", handler_types)
        self.assertNotIn("KrxAlertTickHandler", handler_types)

    async def test_flag_true_registers_alert_handler(self):
        """flag=true: KrxAlertTickHandler 추가 등록."""
        from app import scheduler as scheduler_mod

        added_handlers = []

        class _FakeClient:
            def add_tick_handler(self, h):
                added_handlers.append(h)

        with patch.object(config, "KRX_ALERT_EVALUATOR_ENABLED", True), \
             patch.object(config, "KRX_REDIS_TICK_WRITE_ENABLED", False), \
             patch.object(config, "KRX_CLOSE_FINALIZER_ENABLED", True), \
             patch.object(config, "KRX_FUTURES_ENABLED", True), \
             patch.dict("os.environ", {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}), \
             patch("app.crawlers.krx_kis.KisFuturesClient",
                   return_value=_FakeClient()), \
             patch("app.crawlers.krx_kis.KisApprovalManager"), \
             patch("app.crawlers.krx_kis.KisAccessTokenManager"), \
             patch("app.scheduler.resolve_active_krx_futures_contract",
                   new=AsyncMock(return_value=MagicMock(
                       short_code="A75605",
                       expiry_date=__import__("datetime").date(2026, 6, 15),
                   ))), \
             patch("asyncio.create_task",
                   side_effect=lambda coro: (coro.close(), MagicMock())[1]):
            await scheduler_mod._bootstrap_krx_futures_client()

        handler_types = [type(h).__name__ for h in added_handlers]
        self.assertIn("KrxDbWriter", handler_types)
        self.assertIn("KrxAlertTickHandler", handler_types)


# ---------------------------------------------------------------------------
# KisFuturesClient.stop() — drain hook (close 호출 위임)
# ---------------------------------------------------------------------------

class TestKisFuturesClientStopDrainsAlert(unittest.IsolatedAsyncioTestCase):
    """`KisFuturesClient.stop()`이 KrxAlertTickHandler.close() 호출."""

    async def test_stop_drains_alert_tick_handler(self):
        """stop() → 등록된 KrxAlertTickHandler.close() 호출."""
        from app.crawlers.krx_kis import KisApprovalManager, KisFuturesClient

        approval = MagicMock(spec=KisApprovalManager)
        contract = MagicMock(short_code="A75605",
                             expiry_date=__import__("datetime").date(2026, 6, 15),
                             contract_month="202605")
        client = KisFuturesClient(approval, contract=contract)

        alert_handler = KrxAlertTickHandler()
        alert_handler.close = AsyncMock()
        client.add_tick_handler(alert_handler)

        await client.stop()

        alert_handler.close.assert_awaited_once_with(timeout=5.0)


# ---------------------------------------------------------------------------
# Drain helper — F-1 외부 검토 #1 보강 (coalescer pending flush 보장)
# ---------------------------------------------------------------------------

class TestDrainAlertTickHandlersHelper(unittest.IsolatedAsyncioTestCase):
    """`KisFuturesClient._drain_alert_tick_handlers` helper 단위 검증.

    외부 검토 #1 핵심: ``UsdtAlertEvaluator.PriceAlertCoalescer``는 마지막
    5초 bucket을 다음 tick 또는 close()까지 보류하므로, KRX session boundary
    명시 drain이 없으면 close grace crossing이 영영 누락될 위험. 본 helper가
    boundary + stop() 양쪽에서 공통 drain 책임.
    """

    def _make_client(self):
        from app.crawlers.krx_kis import KisApprovalManager, KisFuturesClient

        approval = MagicMock(spec=KisApprovalManager)
        contract = MagicMock(short_code="A75605",
                             expiry_date=__import__("datetime").date(2026, 6, 15),
                             contract_month="202605")
        return KisFuturesClient(approval, contract=contract)

    async def test_helper_drains_only_alert_handlers(self):
        """등록된 KrxAlertTickHandler만 close; KrxDbWriter / KrxRedisLatestWriter
        등 다른 handler는 건드리지 X.

        외부 검토 요구사항 — handler list 안에 여러 종류가 섞여 있을 때 alert
        handler만 선택 drain. 다른 writer는 자체 drain timing 정책 존중.
        """
        from app.crawlers.krx_kis import KrxDbWriter, KrxRedisLatestWriter

        client = self._make_client()

        alert_handler = KrxAlertTickHandler()
        alert_handler.close = AsyncMock()

        db_writer = KrxDbWriter()
        # KrxDbWriter는 close() 메서드 없음 — 호출되면 AttributeError로
        # 잡힘. 본 테스트는 호출 자체가 없어야 정상.
        db_writer.close = AsyncMock()  # type: ignore[attr-defined]

        redis_writer = KrxRedisLatestWriter()
        redis_writer.close = AsyncMock()  # type: ignore[attr-defined]

        client.add_tick_handler(db_writer)
        client.add_tick_handler(alert_handler)
        client.add_tick_handler(redis_writer)

        await client._drain_alert_tick_handlers(timeout=5.0)

        # KrxAlertTickHandler만 close 호출
        alert_handler.close.assert_awaited_once_with(timeout=5.0)
        # 다른 handler는 건드리지 X
        db_writer.close.assert_not_awaited()
        redis_writer.close.assert_not_awaited()

    async def test_helper_drains_multiple_alert_handlers(self):
        """alert handler가 여러 개 등록되면 모두 close() 호출 (forward-compat)."""
        client = self._make_client()

        h1 = KrxAlertTickHandler()
        h1.close = AsyncMock()
        h2 = KrxAlertTickHandler()
        h2.close = AsyncMock()

        client.add_tick_handler(h1)
        client.add_tick_handler(h2)

        await client._drain_alert_tick_handlers(timeout=5.0)

        h1.close.assert_awaited_once_with(timeout=5.0)
        h2.close.assert_awaited_once_with(timeout=5.0)

    async def test_helper_isolates_close_exception(self):
        """KrxAlertTickHandler.close() 예외 → propagate X (logger.exception only).

        외부 검토 요구사항 — drain 실패가 session loop / shutdown 흐름을
        깨뜨리지 못하도록 격리. 한 handler 실패가 다른 handler drain도 막지
        않아야 함.
        """
        client = self._make_client()

        h1 = KrxAlertTickHandler()
        h1.close = AsyncMock(side_effect=RuntimeError("drain boom"))
        h2 = KrxAlertTickHandler()
        h2.close = AsyncMock()  # 정상 — h1 예외에도 호출되어야 함

        client.add_tick_handler(h1)
        client.add_tick_handler(h2)

        # 예외 propagate 안 됨
        await client._drain_alert_tick_handlers(timeout=5.0)

        # h1 호출됨 (예외 발생) + h2도 호출됨 (h1 실패가 차단 안 함)
        h1.close.assert_awaited_once()
        h2.close.assert_awaited_once()

    async def test_helper_no_op_when_no_alert_handlers(self):
        """alert handler 미등록 시 helper는 no-op (다른 handler 영향 X)."""
        from app.crawlers.krx_kis import KrxDbWriter

        client = self._make_client()
        db_writer = KrxDbWriter()
        db_writer.close = AsyncMock()  # type: ignore[attr-defined]
        client.add_tick_handler(db_writer)

        # 예외 없이 완료
        await client._drain_alert_tick_handlers(timeout=5.0)

        db_writer.close.assert_not_awaited()


# ---------------------------------------------------------------------------
# _run_session boundary — drain helper 호출 잠금 (외부 검토 #1)
# ---------------------------------------------------------------------------

class TestRunSessionBoundaryDrainsAlert(unittest.IsolatedAsyncioTestCase):
    """`_run_session`이 session boundary return 전 alert handler drain 호출."""

    async def test_session_boundary_calls_drain_helper(self):
        """current_session != session → _drain_alert_tick_handlers + return.

        외부 검토 #1 회귀 잠금: KRX session 갭 동안 close grace crossing 누락
        방지. boundary에서 helper 호출이 빠지면 본 테스트가 실패해 회귀 차단.
        """
        from app.crawlers.krx_kis import KisApprovalManager, KisFuturesClient

        approval = MagicMock(spec=KisApprovalManager)
        contract = MagicMock(short_code="A75605",
                             expiry_date=__import__("datetime").date(2026, 6, 15),
                             contract_month="202605")
        client = KisFuturesClient(approval, contract=contract)

        # alert handler 등록 (drain target)
        alert_handler = KrxAlertTickHandler()
        alert_handler.close = AsyncMock()
        client.add_tick_handler(alert_handler)

        # _run_session("CF") 호출 시 get_active_session이 "CM"을 반환 →
        # current_session != session 분기 진입.
        with patch("app.crawlers.krx_kis.get_active_session", return_value="CM"), \
             patch.object(client, "_maybe_schedule_close_snapshot") as mock_snapshot:
            await client._run_session("CF")

        # drain 호출 검증 (boundary return 전)
        alert_handler.close.assert_awaited_once_with(timeout=5.0)
        # close_snapshot도 호출되어야 함 (기존 동작 보존)
        mock_snapshot.assert_called_once()
        kwargs = mock_snapshot.call_args.kwargs
        self.assertEqual(kwargs["ended_session"], "CF")


if __name__ == "__main__":
    unittest.main(verbosity=2)
