"""Price alert coalescer — A-3 wall-clock 5s grain window aggregation.

§12.8.3 12 결정 사항 정합:
- Pattern A-3: ``floor(timestamp / 5s) * 5s`` wall-clock grain
- Half-open interval ``[window_start, window_end)``
- ``min_rate`` / ``max_rate`` / ``last_rate`` / ``tick_count`` 보존
- Per-(source, asset) state isolation
- ``window_sec=0`` trivial pass-through (Bank/Investing 대비)
- Tick-only contract (``rest_probe``는 evaluator dispatch에서 우회)

4 forward-compat 원칙 #2 (단일 가격 전용 컴포넌트 분리) 정합.
순환 import 회피를 위해 :class:`PriceObservation` Protocol을 사용 — coalescer는
``app.notifications.alert_evaluator``를 import하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Protocol

_KST = timezone(timedelta(hours=9))


class PriceObservation(Protocol):
    """Coalescer 입력 contract — :class:`AlertObservation` 필요 필드 subset."""

    source: str
    asset: str
    rate: float
    timestamp_ms: int
    kind: str


@dataclass(frozen=True)
class PriceAlertEvaluationInput:
    """Window summary observation — evaluator로 전달되는 평가 입력.

    ``input_kind="price_window"``는 tick coalescing flush 결과,
    ``"repeat_due"``는 B2 미래 due path forward-compat slot (현 단계 미사용).
    """

    source: str
    asset: str
    observed_at: datetime
    window_start: datetime
    window_end: datetime
    min_rate: Decimal
    max_rate: Decimal
    last_rate: Decimal
    tick_count: int
    input_kind: Literal["price_window", "repeat_due"] = "price_window"


@dataclass
class _Bucket:
    window_start: datetime
    window_end: datetime
    min_rate: Decimal
    max_rate: Decimal
    last_rate: Decimal
    last_observed_at: datetime
    tick_count: int


class PriceAlertCoalescer:
    """A-3 wall-clock grain coalescer (per source/asset).

    ``window_sec=0``이면 pass-through (Bank/Investing trivial mode).
    """

    def __init__(self, window_sec: int = 5) -> None:
        if window_sec < 0:
            raise ValueError(f"window_sec must be >= 0 (got {window_sec})")
        self._window_sec = window_sec
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def add(self, observation: PriceObservation) -> list[PriceAlertEvaluationInput]:
        """Tick observation 수신 — ``kind="tick"``만 허용.

        Bucket 변경 시 이전 bucket summary 반환. ``window_sec=0``이면 매 tick
        pass-through summary 즉시 반환. 동일 bucket 안 추가 tick은 누적만
        하고 빈 list 반환.
        """
        if observation.kind != "tick":
            raise ValueError(
                f"PriceAlertCoalescer accepts tick only (got {observation.kind!r})"
            )

        ts = datetime.fromtimestamp(observation.timestamp_ms / 1000, tz=_KST)
        rate = Decimal(str(observation.rate))
        key = (observation.source, observation.asset)

        if self._window_sec == 0:
            return [
                PriceAlertEvaluationInput(
                    source=observation.source,
                    asset=observation.asset,
                    observed_at=ts,
                    window_start=ts,
                    window_end=ts,
                    min_rate=rate,
                    max_rate=rate,
                    last_rate=rate,
                    tick_count=1,
                )
            ]

        bucket_epoch = (int(observation.timestamp_ms // 1000) // self._window_sec) * self._window_sec
        window_start = datetime.fromtimestamp(bucket_epoch, tz=_KST)
        window_end = window_start + timedelta(seconds=self._window_sec)

        flushed: list[PriceAlertEvaluationInput] = []
        existing = self._buckets.get(key)

        if existing is not None and existing.window_start != window_start:
            flushed.append(
                self._summary_from_bucket(observation.source, observation.asset, existing)
            )
            existing = None

        if existing is None:
            self._buckets[key] = _Bucket(
                window_start=window_start,
                window_end=window_end,
                min_rate=rate,
                max_rate=rate,
                last_rate=rate,
                last_observed_at=ts,
                tick_count=1,
            )
        else:
            if rate < existing.min_rate:
                existing.min_rate = rate
            if rate > existing.max_rate:
                existing.max_rate = rate
            existing.last_rate = rate
            existing.last_observed_at = ts
            existing.tick_count += 1

        return flushed

    def flush_pending(self) -> list[PriceAlertEvaluationInput]:
        """모든 pending bucket 강제 flush (process shutdown / cleanup)."""
        summaries = [
            self._summary_from_bucket(source, asset, bucket)
            for (source, asset), bucket in self._buckets.items()
        ]
        self._buckets.clear()
        return summaries

    @staticmethod
    def _summary_from_bucket(
        source: str, asset: str, bucket: _Bucket
    ) -> PriceAlertEvaluationInput:
        return PriceAlertEvaluationInput(
            source=source,
            asset=asset,
            observed_at=bucket.last_observed_at,
            window_start=bucket.window_start,
            window_end=bucket.window_end,
            min_rate=bucket.min_rate,
            max_rate=bucket.max_rate,
            last_rate=bucket.last_rate,
            tick_count=bucket.tick_count,
        )
