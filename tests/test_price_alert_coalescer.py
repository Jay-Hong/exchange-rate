"""5b-1 — :class:`PriceAlertCoalescer` unit tests.

§12.8.3.2 PR scope 1번 검증 — pure unit tests, 실행 경로 미연결이라 운영 영향 0.

Codex 7 핵심 tests:
1. 같은 5초 bucket 안 tick 여러 개 → 반환 없음
2. bucket 변경 시 이전 bucket summary 1개 반환
3. ``min/max/last/tick_count`` 정확
4. source/asset 섞이면 독립 bucket
5. ``flush_pending()`` 모든 pending bucket 반환
6. ``window_sec=0`` pass-through 즉시 반환
7. ``kind != "tick"`` → ``ValueError``
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.notifications.price_alert_coalescer import (
    PriceAlertCoalescer,
    PriceAlertEvaluationInput,
)

_KST = timezone(timedelta(hours=9))


@dataclass
class _FakeObs:
    """AlertObservation의 PriceObservation subset — Protocol duck typing."""

    source: str
    asset: str
    rate: float
    timestamp_ms: int
    kind: str = "tick"


def _ms(year: int, mo: int, d: int, h: int, mi: int, s: int, ms: int = 0) -> int:
    """Construct epoch ms — ``ms`` argument is *additive* milliseconds (any size)."""
    base = datetime(year, mo, d, h, mi, s, tzinfo=_KST)
    return int(base.timestamp() * 1000) + ms


# Test 1 ---------------------------------------------------------------------
def test_same_bucket_multiple_ticks_no_flush() -> None:
    coalescer = PriceAlertCoalescer(window_sec=5)
    # bucket = [06:00:00, 06:00:05) — all ticks fit
    for ms_offset in [0, 1000, 2000, 4999]:
        obs = _FakeObs(
            "upbit", "usdt-krw", 1473.5, _ms(2026, 5, 24, 6, 0, 0, ms_offset)
        )
        result = coalescer.add(obs)
        assert result == []


# Test 2 ---------------------------------------------------------------------
def test_bucket_change_flushes_previous() -> None:
    coalescer = PriceAlertCoalescer(window_sec=5)
    # bucket1 = [06:00:00, 06:00:05)
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1473.0, _ms(2026, 5, 24, 6, 0, 0)))
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1473.5, _ms(2026, 5, 24, 6, 0, 4, 999)))
    # bucket2 = [06:00:05, 06:00:10) — bucket boundary 넘음
    result = coalescer.add(_FakeObs("upbit", "usdt-krw", 1474.0, _ms(2026, 5, 24, 6, 0, 5)))
    assert len(result) == 1
    summary = result[0]
    assert isinstance(summary, PriceAlertEvaluationInput)
    assert summary.source == "upbit"
    assert summary.asset == "usdt-krw"
    assert summary.window_start == datetime(2026, 5, 24, 6, 0, 0, tzinfo=_KST)
    assert summary.window_end == datetime(2026, 5, 24, 6, 0, 5, tzinfo=_KST)
    assert summary.tick_count == 2
    assert summary.min_rate == Decimal("1473.0")
    assert summary.max_rate == Decimal("1473.5")
    assert summary.last_rate == Decimal("1473.5")
    assert summary.input_kind == "price_window"


# Test 3 ---------------------------------------------------------------------
def test_min_max_last_tick_count_accuracy() -> None:
    """Codex 예시: 1470 → 1482 → 1471 (above 1480 crossing 보존 시나리오)."""
    coalescer = PriceAlertCoalescer(window_sec=5)
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1470.0, _ms(2026, 5, 24, 6, 0, 0)))
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1482.0, _ms(2026, 5, 24, 6, 0, 1)))
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1471.0, _ms(2026, 5, 24, 6, 0, 2)))
    result = coalescer.add(_FakeObs("upbit", "usdt-krw", 1480.0, _ms(2026, 5, 24, 6, 0, 5)))
    assert len(result) == 1
    summary = result[0]
    assert summary.min_rate == Decimal("1470.0")
    assert summary.max_rate == Decimal("1482.0")  # crossing 보존
    assert summary.last_rate == Decimal("1471.0")  # 마지막 tick
    assert summary.tick_count == 3


# Test 4 ---------------------------------------------------------------------
def test_source_asset_isolation() -> None:
    coalescer = PriceAlertCoalescer(window_sec=5)
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1473.0, _ms(2026, 5, 24, 6, 0, 0)))
    coalescer.add(_FakeObs("bithumb", "usdt-krw", 1474.0, _ms(2026, 5, 24, 6, 0, 0)))
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1475.0, _ms(2026, 5, 24, 6, 0, 1)))

    # bucket 변경 — upbit만 flush
    result = coalescer.add(_FakeObs("upbit", "usdt-krw", 1476.0, _ms(2026, 5, 24, 6, 0, 5)))
    assert len(result) == 1
    assert result[0].source == "upbit"
    assert result[0].max_rate == Decimal("1475.0")
    assert result[0].tick_count == 2

    # bithumb 아직 pending — bucket 변경 시 독립적으로 flush
    result2 = coalescer.add(_FakeObs("bithumb", "usdt-krw", 1477.0, _ms(2026, 5, 24, 6, 0, 5)))
    assert len(result2) == 1
    assert result2[0].source == "bithumb"
    assert result2[0].max_rate == Decimal("1474.0")
    assert result2[0].tick_count == 1


# Test 5 ---------------------------------------------------------------------
def test_flush_pending_returns_all_buckets() -> None:
    coalescer = PriceAlertCoalescer(window_sec=5)
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1473.0, _ms(2026, 5, 24, 6, 0, 0)))
    coalescer.add(_FakeObs("bithumb", "usdt-krw", 1474.0, _ms(2026, 5, 24, 6, 0, 0)))
    coalescer.add(_FakeObs("coinone", "usdt-krw", 1475.0, _ms(2026, 5, 24, 6, 0, 0)))

    flushed = coalescer.flush_pending()
    assert len(flushed) == 3
    sources = {s.source for s in flushed}
    assert sources == {"upbit", "bithumb", "coinone"}

    # flush 후 상태 초기화 — 다음 flush_pending() 빈 list
    assert coalescer.flush_pending() == []


# Test 6 ---------------------------------------------------------------------
def test_window_zero_pass_through() -> None:
    """Bank/Investing trivial pass-through 모드 — 매 tick summary 즉시 반환."""
    coalescer = PriceAlertCoalescer(window_sec=0)
    obs = _FakeObs("kb", "usd-krw", 1473.5, _ms(2026, 5, 24, 6, 0, 0))
    result = coalescer.add(obs)
    assert len(result) == 1
    summary = result[0]
    assert summary.tick_count == 1
    assert summary.min_rate == Decimal("1473.5")
    assert summary.max_rate == Decimal("1473.5")
    assert summary.last_rate == Decimal("1473.5")
    assert summary.window_start == summary.observed_at
    assert summary.window_end == summary.observed_at

    # 다음 tick도 즉시 pass-through (rate 다름)
    obs2 = _FakeObs("kb", "usd-krw", 1474.0, _ms(2026, 5, 24, 6, 0, 1))
    result2 = coalescer.add(obs2)
    assert len(result2) == 1
    assert result2[0].last_rate == Decimal("1474.0")

    # window_sec=0이라 flush_pending()은 항상 비어 있음 (state 미보유)
    assert coalescer.flush_pending() == []


# Test 7 ---------------------------------------------------------------------
def test_non_tick_kind_raises_value_error() -> None:
    coalescer = PriceAlertCoalescer(window_sec=5)
    obs = _FakeObs(
        "upbit", "usdt-krw", 1473.5, _ms(2026, 5, 24, 6, 0, 0), kind="rest_probe"
    )
    with pytest.raises(ValueError, match="tick only"):
        coalescer.add(obs)


# 추가 sanity — negative window_sec 거부 ---------------------------------------
def test_negative_window_sec_rejected() -> None:
    with pytest.raises(ValueError, match="window_sec must be >= 0"):
        PriceAlertCoalescer(window_sec=-1)


# Test 8: out-of-order tick drop + warning (5b-3a) ------------------------------
def test_out_of_order_tick_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """과거 timestamp tick → drop + warning. 기존 bucket state 미변경 검증.

    Codex 5b-3a 정밀화:
    - 반환값: 빈 list
    - 상태: out-of-order tick은 active bucket에 누적 안 됨 (min/max 왜곡 차단)
    - log: "out-of-order" 핵심 문자열 (logger 이름 과도 의존 회피)
    """
    coalescer = PriceAlertCoalescer(window_sec=5)
    # active bucket = [06:00:05, 06:00:10)
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1473.0, _ms(2026, 5, 24, 6, 0, 5)))
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1474.0, _ms(2026, 5, 24, 6, 0, 6)))

    # Out-of-order: tick_ws=[06:00:00, 06:00:05) < active [06:00:05, 06:00:10)
    caplog.set_level(logging.WARNING, logger="exchange_rate.notifications.price_alert_coalescer")
    result = coalescer.add(_FakeObs("upbit", "usdt-krw", 1500.0, _ms(2026, 5, 24, 6, 0, 4)))

    # 반환값 검증 — 빈 list
    assert result == []

    # 핵심 문자열 검증 — logger 이름 의존 회피 (메시지 자체 확인)
    assert any("out-of-order" in rec.getMessage() for rec in caplog.records)

    # 상태 검증 — out-of-order tick의 rate=1500.0이 active bucket에 누적되지 않음
    final = coalescer.flush_pending()
    assert len(final) == 1
    summary = final[0]
    assert summary.tick_count == 2  # 처음 2 tick만 (out-of-order 제외)
    assert summary.min_rate == Decimal("1473.0")
    assert summary.max_rate == Decimal("1474.0")  # 1500.0 무시 — 정확 drop 증거


def test_same_window_start_treated_as_same_bucket() -> None:
    """``window_start == existing.window_start``는 out-of-order가 아니라 정상 같은 bucket."""
    coalescer = PriceAlertCoalescer(window_sec=5)
    # 같은 bucket = [06:00:05, 06:00:10) — 모두 정상 누적
    coalescer.add(_FakeObs("upbit", "usdt-krw", 1473.0, _ms(2026, 5, 24, 6, 0, 5)))
    result = coalescer.add(_FakeObs("upbit", "usdt-krw", 1474.0, _ms(2026, 5, 24, 6, 0, 5, 500)))
    assert result == []  # 같은 bucket, flush 없음
    # 양쪽 tick 모두 누적 확인
    final = coalescer.flush_pending()
    assert len(final) == 1
    assert final[0].tick_count == 2
    assert final[0].min_rate == Decimal("1473.0")
    assert final[0].max_rate == Decimal("1474.0")
