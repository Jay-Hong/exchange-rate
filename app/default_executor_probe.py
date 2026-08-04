"""default executor **queue 지연** sentinel — 격리의 *보호 대상* 을 재는 유일한 수단.

왜 필요한가: 인증 전용 executor 의 목적은 **동거 `to_thread` 작업을 지키는 것**이다(측정 근거였던
p99 3967ms → 31ms 가 그 축이다). 그런데 `ws-auth-executor-metrics` 는 **전용 pool 만** 본다 —
그것만으로 canary 를 돌리면 *"인증이 분리됐다"* 는 알아도 *"동거인이 보호된다"* 는 **끝내 모른다**.
⛔ `job_duration_ms`(broadcast job 벽시계)는 대체물이 **아니다** — DB·네트워크 시간이 섞여 있어
default executor 의 **큐 대기**를 분리해 내지 못한다.

무엇을 재나: `asyncio.to_thread` 로 **사소한 작업 하나**를 던지고 **제출→worker 시작** 지연을 잰다.
그게 곧 "지금 새로 들어온 동거 작업이 얼마나 기다리는가" 다.

⛔ **측정은 worker 가 소유한다.** 한때 caller 가 `await asyncio.to_thread(...)` 가 **돌아온 뒤에야**
기록했는데, 그러면 pool 이 **완전히 막힌 최악의 상태**가 `count=0` 으로 보인다 — "아직 표본 없음"과
**구분되지 않는다**. 그래서 제출은 제출대로 세고(`submitted_count`), 지연은 worker 가 **시작하는
순간** 스스로 기록한다(`started_count`). probe 는 **순차** 라서 `outstanding = submitted - started`
가 1로 **머물러 있으면 그 자체가 곧 default pool 포화**다.

⚠️ **canary 기간에만 켠다**(`DEFAULT_EXECUTOR_PROBE_ENABLED`, 기본 off). 상시로 두면 스스로
default pool 을 조금씩 점유한다 — 재려는 대상을 관측이 흔드는 형태다.
⚠️ 집계는 **bounded** 다: 누계·최댓값 + **고정 경계 히스토그램**(무제한 시계열을 만들지 않는다).
⚠️ **process-local** — worker 를 늘리면 합산 없이는 전체값이 아니다.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import Any

logger = logging.getLogger("exchange_rate.default_executor_probe")

# 꼬리를 **어느 구간에 있는지**만 알면 된다(정확한 p99 대신). 경계는 고정 — 무제한으로 늘리지 않는다.
BUCKET_BOUNDARIES_MS: tuple[float, ...] = (1, 5, 10, 50, 100, 500, 1000, 5000)

# ⛔ sentinel 자신이 부하원이 되지 않게 하는 **하한**. 0·음수면 hot loop, `nan` 이면 sleep 이
#    즉시 반환되어 역시 hot loop, `inf` 면 첫 probe 뒤 **영원히 잠들어** 조용히 죽는다 —
#    셋 다 "켰는데 관측이 없다/서버가 더 느려졌다"로 나타나므로 **켜는 순간 fail-fast** 한다.
MIN_PROBE_INTERVAL_SECONDS = 1.0

_lock = threading.Lock()
_metrics: dict[str, Any] = {
    "submitted_count": 0,     # loop 가 제출한 횟수
    "started_count": 0,       # worker 가 **실제로 시작**한 횟수 (= 지연 표본 수)
    "queue_delay_ms_sum": 0.0,
    "queue_delay_ms_max": 0.0,
    "histogram": {},          # "<=1" / "<=5" / ... / ">5000"
}

_probe_task: asyncio.Task | None = None


def _bucket_label(delay_ms: float) -> str:
    for boundary in BUCKET_BOUNDARIES_MS:
        if delay_ms <= boundary:
            return f"<={boundary:g}"
    return f">{BUCKET_BOUNDARIES_MS[-1]:g}"


def default_executor_probe_metrics() -> dict[str, Any]:
    """⚠️ `outstanding` 을 **표본 없음과 헷갈리지 말 것**: `started_count == 0` 이어도
    `submitted_count == 1, outstanding == 1` 이면 그건 "아직 안 켬"이 아니라 **큐에 갇힘**이다.
    """
    with _lock:
        snapshot = dict(_metrics)
        snapshot["histogram"] = dict(_metrics["histogram"])
    snapshot["outstanding"] = snapshot["submitted_count"] - snapshot["started_count"]
    return snapshot


def reset_default_executor_probe_metrics() -> None:
    with _lock:
        _metrics.update(submitted_count=0, started_count=0,
                        queue_delay_ms_sum=0.0, queue_delay_ms_max=0.0)
        _metrics["histogram"] = {}


def is_default_executor_probe_running() -> bool:
    """실제 probe task 상태. config=true 만으로 "관측 중"이라고 보고하지 않는다."""
    return _probe_task is not None and not _probe_task.done()


def _record_submitted() -> None:
    with _lock:
        _metrics["submitted_count"] += 1


def _record_started(delay_ms: float) -> None:
    """⛔ **worker 스레드에서** 불린다 — caller 가 취소되거나 loop 로의 결과 전달이 늦어도
    측정값이 사라지지 않아야 한다."""
    with _lock:
        _metrics["started_count"] += 1
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
    measured: list[float] = []

    def _worker() -> None:
        delay_ms = (time.monotonic() - submitted) * 1000.0
        measured.append(delay_ms)
        _record_started(delay_ms)          # ← 소유권이 worker 에 있다(위 모듈 주석 참조)

    _record_submitted()
    await asyncio.to_thread(_worker)
    return measured[0]


def validate_probe_interval(seconds: float) -> float:
    """⛔ **켜는 순간** fail-fast. 잘못된 값을 안고 기동하면 sentinel 이 관측이 아니라 **부하원**이 된다."""
    value = float(seconds)
    if not math.isfinite(value) or value < MIN_PROBE_INTERVAL_SECONDS:
        raise ValueError(
            "DEFAULT_EXECUTOR_PROBE_INTERVAL_SECONDS 는 유한하고 "
            f"{MIN_PROBE_INTERVAL_SECONDS}초 이상이어야 한다 (받은 값: {seconds!r})"
        )
    return value


async def _probe_loop(interval_seconds: float) -> None:
    while True:
        try:
            await probe_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 관측이 서비스를 멈추게 하지 않는다
            logger.warning("default executor probe 실패", exc_info=True)
        await asyncio.sleep(interval_seconds)


def start_default_executor_probe(enabled: bool, interval_seconds: float) -> asyncio.Task | None:
    """lifespan 이 부르는 **프로덕션 진입점**. off 면 task 자체를 만들지 않는다."""
    global _probe_task
    if not enabled:
        return None
    if is_default_executor_probe_running():
        return _probe_task
    interval = validate_probe_interval(interval_seconds)     # ⛔ 기동 전 fail-fast
    # 새 수명주기는 새 관측 창이다. 이전 수명주기에서 큐 취소로 남은 outstanding 을
    # 현재 포화로 오독하지 않도록 집계를 함께 초기화한다.
    reset_default_executor_probe_metrics()
    _probe_task = asyncio.create_task(_probe_loop(interval))
    logger.info("✅ default executor probe 기동", extra={"interval_sec": interval})
    return _probe_task


async def stop_default_executor_probe() -> None:
    """⚠️ **cancel 만으로는 종료가 보장되지 않는다** — 합류까지 해야 한다.

    ⛔ 인증 executor 와 혼동 금지: 저기서는 실행 중 worker 를 **기다리면 안 된다**(인증은 SDK 재시도
    까지 얹혀 길다). 여기 합류는 **유계**다 — probe 가 `sleep` 중이면 취소가 즉시고,
    `to_thread` 대기 중이어도 asyncio 쪽 future 취소는 worker 완료를 **기다리지 않는다**.
    """
    global _probe_task
    task = _probe_task
    _probe_task = None
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
