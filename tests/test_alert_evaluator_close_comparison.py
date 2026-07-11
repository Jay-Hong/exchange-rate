"""close()/flush_pending 경로의 비교알림 hook 대칭 (codex 2026-07-11).

결함: tick 경로(``schedule``)는 coalescer flush 후 ``_emit_comparison``을 호출하지만,
``close()``의 ``flush_pending()`` 경로는 단일 가격알림만 재평가하고 비교 hook을 빠뜨렸다.
KRX ``_drain_alert_tick_handlers``(CF 15:45 종료 / CF→CM 갭)가 마지막 coalescer bucket을
flush할 때 — 바로 그 마지막 bucket 손실을 막으려 존재하는 drain인데 — 비교알림 crossing이
누락됐다. tick 경로와 동일하게 close flush도 (source, asset)당 1회 비교 hook을 발화해야 한다.

DB 격리: close()가 만드는 ``_evaluate_price_input_async``(단일알림 → SourceAlertBackend →
DB)는 이 테스트 관심 밖이라 AsyncMock으로 대체(codex Blocker — 실 DB thread teardown 회피).
dedup/distinct-pair 계약은 fake coalescer로 정확히 잠근다(실 coalescer는 pair당 summary 1개라
emitted_pairs branch 미도달).
"""
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.notifications.alert_evaluator import AlertObservation, UsdtAlertEvaluator
from app.notifications.price_alert_coalescer import PriceAlertEvaluationInput

_HOOK = "app.notifications.alert_evaluator._emit_comparison"


def _input(source: str, asset: str, rate: float = 1500.0) -> PriceAlertEvaluationInput:
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    r = Decimal(str(rate))
    return PriceAlertEvaluationInput(
        source=source, asset=asset, observed_at=now,
        window_start=now, window_end=now,
        min_rate=r, max_rate=r, last_rate=r, tick_count=1,
    )


class _FakeCoalescer:
    """flush_pending 반환을 직접 통제 — dedup/distinct-pair emit 계약 격리 검증용."""

    def __init__(self, flush_result: list[PriceAlertEvaluationInput]):
        self._flush_result = flush_result

    def add(self, observation):
        return []                      # 항상 pending (add 경로 flush 없음)

    def flush_pending(self):
        return list(self._flush_result)


class TestCloseFlushComparisonHook(unittest.IsolatedAsyncioTestCase):

    async def test_integration_real_coalescer_pending_bucket_emits(self):
        """실 coalescer + schedule 1 tick → close flush가 비교 hook 대칭 발화 (integration)."""
        evaluator = UsdtAlertEvaluator()   # 실 PriceAlertCoalescer(window_sec=5)
        with patch.object(evaluator, "_evaluate_price_input_async", new_callable=AsyncMock), \
             patch(_HOOK) as mock_emit:
            evaluator.schedule(AlertObservation(
                source="bithumb", asset="usdt-krw", rate=1500.0,
                timestamp_ms=1_700_000_000_000, kind="tick"))   # 첫 tick → pending, flush 0
            mock_emit.assert_not_called()                       # tick 경로 flush 0 → emit 0
            await evaluator.close(timeout=1.0)                  # flush_pending → 비교 hook (수정 지점)
            mock_emit.assert_called_once_with("bithumb", "usdt-krw")

    async def test_close_no_pending_no_emit(self):
        """pending bucket 없으면 flush 0 → 비교 hook 미발화 (오발화 회귀 가드)."""
        evaluator = UsdtAlertEvaluator(coalescer=_FakeCoalescer([]))
        with patch.object(evaluator, "_evaluate_price_input_async", new_callable=AsyncMock), \
             patch(_HOOK) as mock_emit:
            await evaluator.close(timeout=1.0)
            mock_emit.assert_not_called()

    async def test_close_dedups_duplicate_summaries_single_emit(self):
        """flush가 같은 (source,asset) summary를 여러 개 반환해도 emit 1회 (emitted_pairs 잠금)."""
        evaluator = UsdtAlertEvaluator(coalescer=_FakeCoalescer([
            _input("bithumb", "usdt-krw", 1500.0),
            _input("bithumb", "usdt-krw", 1500.1),
        ]))
        with patch.object(evaluator, "_evaluate_price_input_async", new_callable=AsyncMock), \
             patch(_HOOK) as mock_emit:
            await evaluator.close(timeout=1.0)
            mock_emit.assert_called_once_with("bithumb", "usdt-krw")

    async def test_close_distinct_pairs_emit_each(self):
        """서로 다른 (source,asset)는 각각 1회 emit (flush_pending 일반 계약 잠금)."""
        evaluator = UsdtAlertEvaluator(coalescer=_FakeCoalescer([
            _input("bithumb", "usdt-krw"),
            _input("krx", "usd-krw-futures"),
        ]))
        with patch.object(evaluator, "_evaluate_price_input_async", new_callable=AsyncMock), \
             patch(_HOOK) as mock_emit:
            await evaluator.close(timeout=1.0)
            self.assertEqual(mock_emit.call_count, 2)
            mock_emit.assert_any_call("bithumb", "usdt-krw")
            mock_emit.assert_any_call("krx", "usd-krw-futures")

    async def test_krx_asset_close_flush_emits(self):
        """KRX 달러선물도 동일 — 세션 경계 drain 시 비교(김프) hook 대칭."""
        evaluator = UsdtAlertEvaluator(coalescer=_FakeCoalescer([
            _input("krx", "usd-krw-futures", 1500.4),
        ]))
        with patch.object(evaluator, "_evaluate_price_input_async", new_callable=AsyncMock), \
             patch(_HOOK) as mock_emit:
            await evaluator.close(timeout=1.0)
            mock_emit.assert_called_once_with("krx", "usd-krw-futures")


if __name__ == "__main__":
    unittest.main()
