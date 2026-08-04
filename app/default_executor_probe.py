"""default executor **queue 지연** sentinel — 격리의 *보호 대상* 을 재는 유일한 수단.

왜 필요한가: 인증 전용 executor 의 목적은 **동거 `to_thread` 작업을 지키는 것**이다(측정 근거였던
p99 3967ms → 31ms 가 그 축이다). 그런데 `ws-auth-executor-metrics` 는 **전용 pool 만** 본다 —
그것만으로 canary 를 돌리면 *"인증이 분리됐다"* 는 알아도 *"동거인이 보호된다"* 는 **끝내 모른다**.
⛔ `job_duration_ms`(broadcast job 벽시계)는 대체물이 **아니다** — DB·네트워크 시간이 섞여 있어
default executor 의 **큐 대기**를 분리해 내지 못한다.

무엇을 재나: `asyncio.to_thread` 로 **사소한 작업 하나**를 던지고 **제출→worker 시작** 지연을 잰다.
그게 곧 "지금 새로 들어온 동거 작업이 얼마나 기다리는가" 다.

⚠️ **canary 기간에만 켠다**(`DEFAULT_EXECUTOR_PROBE_ENABLED`, 기본 off). 상시로 두면 스스로
default pool 을 조금씩 점유한다 — 재려는 대상을 관측이 흔드는 형태다.
⚠️ 집계는 **bounded** 다: 누계·최댓값 + **고정 경계 히스토그램**(무제한 시계열을 만들지 않는다).
⚠️ **process-local** — worker 를 늘리면 합산 없이는 전체값이 아니다.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

# 꼬리를 **어느 구간에 있는지**만 알면 된다(정확한 p99 대신). 경계는 고정 — 무제한으로 늘리지 않는다.
BUCKET_BOUNDARIES_MS: tuple[float, ...] = (1, 5, 10, 50, 100, 500, 1000, 5000)

_lock = threading.Lock()
_metrics: dict[str, Any] = {
    "count": 0,
    "queue_delay_ms_sum": 0.0,
    "queue_delay_ms_max": 0.0,
    "histogram": {},          # "<=1" / "<=5" / ... / ">5000"
}


def _bucket_label(delay_ms: float) -> str:
    for boundary in BUCKET_BOUNDARIES_MS:
        if delay_ms <= boundary:
            return f"<={boundary:g}"
    return f">{BUCKET_BOUNDARIES_MS[-1]:g}"


def default_executor_probe_metrics() -> dict[str, Any]:
    with _lock:
        snapshot = dict(_metrics)
        snapshot["histogram"] = dict(_metrics["histogram"])
    return snapshot


def reset_default_executor_probe_metrics() -> None:
    with _lock:
        _metrics.update(count=0, queue_delay_ms_sum=0.0, queue_delay_ms_max=0.0)
        _metrics["histogram"] = {}


def _record(delay_ms: float) -> None:
    with _lock:
        _metrics["count"] += 1
        _metrics["queue_delay_ms_sum"] += delay_ms
        _metrics["queue_delay_ms_max"] = max(_metrics["queue_delay_ms_max"], delay_ms)
        label = _bucket_label(delay_ms)
        _metrics["histogram"][label] = _metrics["histogram"].get(label, 0) + 1


async def probe_once() -> float:
    """default executor 에 사소한 작업을 던져 **제출→시작** 지연(ms)을 잰다.

    ⛔ 여기서 `run_in_auth_executor` 를 쓰면 **재려는 대상이 뒤바뀐다** — 이 sentinel 의 전부는
    `asyncio.to_thread` 가 쓰는 **기본** pool 을 보는 것이다.
    """
    submitted = time.monotonic()
    started: list[float] = []

    def _mark() -> None:
        started.append(time.monotonic())

    await asyncio.to_thread(_mark)
    delay_ms = (started[0] - submitted) * 1000.0
    _record(delay_ms)
    return delay_ms
