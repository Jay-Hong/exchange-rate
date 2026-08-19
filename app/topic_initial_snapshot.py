"""Topic snapshot-on-subscribe — realtime topic release readiness 1st slice.

신규 topic-consuming 앱(legacy WS broadcast 대신 topic 구독)이 subscribe 직후
현재 상태(snapshot)를 즉시 받도록 한다. 없으면 다음 publish(값 변경)까지 빈 화면 —
조용한 시간대/주말엔 무한 대기 가능 = 신규 앱 출시 blocker (PREFLIGHT wf_0f5f9dcf 1순위 gap).

scope: 실제 구현된 topic만 — fx:usd-krw / fx:jpy-krw / fx:eur-krw + usdt:krw
+ krx:usd-krw-futures(ADR-038 D2 독립 topic — KRX_CLIENT_DISTRIBUTION_EFFECTIVE=true일 때만
supported list 포함, G2 off면 snapshot 404 = 발행 중단과 동일 gate).
DXY/news/graph는 publisher 미구현이라 범위 밖.
⚠️ 구 서술("매핑에 없으면 register 는 유지하되 snapshot skip")은 **더 이상 맞지 않는다**
(2026-08-02 `4a45173`): 무토큰 경로는 `supported_snapshot_topics()` 안이면서 gated 가 아닌
topic 만 등록하고, 식별된 요청은 미지원 topic 을 `rejected_topics` 로 접는다 — 어느 경로도
**미지원 topic 을 registry 에 넣지 않는다**.

설계 (codex 019efdb3 검토 — 2 blocker 반영):
- SessionLocal 생성+builder+close를 to_thread 내부 sync 함수에서 전부 처리. SQLAlchemy
  Session은 thread-unsafe라 절대 thread 경계를 넘기지 않는다 (codex blocker 1).
- subscribe handler는 WebSocket receive loop라 sync builder 직접 호출 시 모든 connection의
  event loop 반응성을 해침 → asyncio.to_thread로 offload (DB fallback 시 특히).
- send 실패 = connection 실패 → registry 정리 + 남은 snapshot 중단 (publish_topic 패턴,
  codex blocker 2). ACK 후 build 실패는 transient=1013 / fatal=1011로 연결을 종결한다.
- 빈 payload도 전송(builders는 빈 list/optional omission으로도 schema-valid snapshot 생성 —
  "빈 화면 방지" 목적상 skip 금지).
- enable gate: fx는 FX_TOPIC_ENABLED(publisher와 일관) + TOPIC_DISPATCHER_ENABLED(caller가 이미
  체크). usdt:krw는 TOPIC_DISPATCHER_ENABLED만(별도 TETHER flag 없음). KRX는 독립 topic(ADR-038 D2).
- behavior-change-0: prod subscriber=0(테더 탭 미출시) + TOPIC_DISPATCHER_ENABLED default false
  → live 단말 영향 0. dev/test 구독자만 영향.

⚠️ 알려진 한계 — snapshot↔live-publish race (codex 019efdbc): register(dispatcher) 직후
to_thread build 중 concurrent publish_topic가 이 ws에 newer update를 먼저 보낼 수 있고,
이후 (build 시점 값) snapshot이 도착하면 client가 잠시 구값으로 회귀 가능. register-first는
의도(build 전 publish 누락 방지)라 race는 inherent. **landing엔 무해**(subscriber=0+FF off
dormant)이나 **live subscriber 활성(신규 앱 출시) 전 해소 필요** = client (source,asset)
timestamp-merge 계약(구값으로 신값 덮지 않기, V2 client guide 항목) + 필요 시 server seq(deferred).
"""
import asyncio
import copy
import logging
import math
import threading
import time
import weakref
from dataclasses import dataclass, field

from app import subscribe_load_metrics as subscribe_load

from starlette.websockets import WebSocketDisconnect

from app.topic_wire import (
    InitialSnapshotConnectionClosed,
    InitialSnapshotDeadlineExceeded,
    InitialSnapshotFatalFailure,
    InitialSnapshotTransientFailure,
)
from typing import Any, Callable, Dict, List, NamedTuple, Optional, TYPE_CHECKING

from app import config

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger("exchange_rate.topic_initial_snapshot")

SNAPSHOT_FAILURE_DEADLINE = "deadline"
SNAPSHOT_FAILURE_TRANSIENT_DB = "transient_db"
SNAPSHOT_FAILURE_REDIS = "redis"
SNAPSHOT_FAILURE_CLASSES = (
    SNAPSHOT_FAILURE_DEADLINE,
    SNAPSHOT_FAILURE_TRANSIENT_DB,
    SNAPSHOT_FAILURE_REDIS,
)


def _snapshot_transient_failure_class(exc: BaseException) -> Optional[str]:
    """Cooldown에 넣어도 되는 snapshot build 실패만 좁게 분류한다."""
    if isinstance(exc, SnapshotDeadlineExceeded):
        return SNAPSHOT_FAILURE_DEADLINE

    from app.db_errors import TRANSIENT_DB_ERRORS, is_transient_db_error

    if isinstance(exc, TRANSIENT_DB_ERRORS):
        return (
            SNAPSHOT_FAILURE_TRANSIENT_DB
            if is_transient_db_error(exc)
            else None
        )
    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError
    except ImportError:
        return None
    if isinstance(exc, (RedisConnectionError, RedisTimeoutError)):
        return SNAPSHOT_FAILURE_REDIS
    return None


def _is_transient_snapshot_error(exc: BaseException) -> bool:
    return _snapshot_transient_failure_class(exc) is not None


async def _close_snapshot_connection(websocket: "WebSocket", *, code: int) -> None:
    from app.topic_dispatcher import registry

    registry.remove_websocket(websocket)
    try:
        async with asyncio.timeout(1.0):
            await websocket.close(code=code)
    except Exception:
        logger.debug("snapshot close 실패", extra={"code": code}, exc_info=True)


class SnapshotDeadlineExceeded(TimeoutError):
    """topic snapshot 요청의 absolute deadline이 끝났다."""


class SnapshotFailureCooldownActive(RuntimeError):
    """같은 key의 transient build 실패 cooldown이 아직 유효하다."""

    def __init__(
        self,
        topic: str,
        *,
        failure_class: str,
        remaining_seconds: float,
        suppressed_builds: int,
    ) -> None:
        super().__init__(f"snapshot transient failure cooldown active: {topic}")
        self.topic = topic
        self.failure_class = failure_class
        self.remaining_seconds = remaining_seconds
        self.suppressed_builds = suppressed_builds


class SnapshotWorkerStopped(RuntimeError):
    """caller 취소/deadline을 worker checkpoint에서 관측한 협력 중단 신호."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SnapshotRequestBudget:
    """ACK와 snapshot worker가 공유하는 단일 monotonic 요청 예산.

    `asyncio.to_thread` 취소는 실행 중인 함수를 멈추지 않는다. caller는 deadline에 맞춰
    반환하되, worker는 각 Redis/DB 호출 사이에 `checkpoint()`를 호출해 다음 I/O를 시작하지
    않는다. 실행 중인 I/O 자체의 종료는 LOAD-S2 DB/Redis timeout이 맡는다.
    """

    def __init__(
        self,
        *,
        started_at_mono: Optional[float] = None,
        ack_deadline_seconds: Optional[float] = None,
        snapshot_budget_seconds: Optional[float] = None,
        request_deadline_seconds: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self.started_at_mono = clock() if started_at_mono is None else started_at_mono
        self.ack_deadline_at_mono = self.started_at_mono + (
            config.WS_TOPIC_ACK_DEADLINE_SECONDS
            if ack_deadline_seconds is None else ack_deadline_seconds
        )
        self.request_deadline_at_mono = self.started_at_mono + (
            config.WS_TOPIC_REQUEST_DEADLINE_SECONDS
            if request_deadline_seconds is None else request_deadline_seconds
        )
        self.snapshot_budget_seconds = (
            config.WS_TOPIC_SNAPSHOT_BUDGET_SECONDS
            if snapshot_budget_seconds is None else snapshot_budget_seconds
        )
        ack_seconds = self.ack_deadline_at_mono - self.started_at_mono
        request_seconds = self.request_deadline_at_mono - self.started_at_mono
        for name, value in (
            ("ack_deadline_seconds", ack_seconds),
            ("snapshot_budget_seconds", self.snapshot_budget_seconds),
            ("request_deadline_seconds", request_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0 (got {value!r})")
        if ack_seconds >= request_seconds:
            raise ValueError("ACK deadline must be less than request deadline")
        if self.snapshot_budget_seconds > request_seconds:
            raise ValueError("snapshot budget must not exceed request deadline")
        self.snapshot_deadline_at_mono: Optional[float] = None
        self._stop = threading.Event()
        self._stop_reason: Optional[str] = None

    def start_snapshot_phase(self) -> float:
        """ACK 뒤 snapshot 총예산을 한 번만 시작한다(토픽별 리셋 금지)."""
        if self.snapshot_deadline_at_mono is None:
            self.snapshot_deadline_at_mono = min(
                self.request_deadline_at_mono,
                self._clock() + self.snapshot_budget_seconds,
            )
        return self.snapshot_deadline_at_mono

    def remaining_until(self, deadline_at_mono: float) -> float:
        return max(0.0, deadline_at_mono - self._clock())

    def remaining_snapshot_seconds(self) -> float:
        return self.remaining_until(self.start_snapshot_phase())

    def stop(self, reason: str) -> None:
        if not self._stop.is_set():
            self._stop_reason = reason
            self._stop.set()

    @property
    def stop_reason(self) -> Optional[str]:
        return self._stop_reason

    def checkpoint(self) -> None:
        """새 Redis/DB phase 진입 직전에 호출하는 worker 협력 중단점."""
        if self._stop.is_set():
            raise SnapshotWorkerStopped(self._stop_reason or "caller_stopped")
        deadline = self.snapshot_deadline_at_mono or self.request_deadline_at_mono
        if self._clock() >= deadline:
            self.stop("deadline")
            raise SnapshotWorkerStopped("deadline")

    def fork_for_shared_build(self) -> "SnapshotRequestBudget":
        """caller 수명과 분리된 shared build용 S3 budget을 만든다.

        leader caller의 취소/짧은 잔여시간을 그대로 공유하면 다른 waiter까지 중단된다. shared
        worker는 같은 snapshot 총예산을 새 build 시작점부터 한 번만 쓰고, 각 waiter는 자기 요청
        budget으로 `shield(task)`를 기다린다.
        """
        ack_duration = self.ack_deadline_at_mono - self.started_at_mono
        request_duration = self.request_deadline_at_mono - self.started_at_mono
        return SnapshotRequestBudget(
            started_at_mono=self._clock(),
            ack_deadline_seconds=ack_duration,
            snapshot_budget_seconds=self.snapshot_budget_seconds,
            request_deadline_seconds=request_duration,
            clock=self._clock,
        )


_snapshot_worker_state = threading.local()


def _worker_checkpoint() -> None:
    budget = getattr(_snapshot_worker_state, "budget", None)
    if budget is not None:
        budget.checkpoint()


def _active_worker_checkpoint() -> Optional[Callable[[], None]]:
    if getattr(_snapshot_worker_state, "budget", None) is None:
        return None
    return _worker_checkpoint


def _run_snapshot_worker(
    topic: str, budget: SnapshotRequestBudget
) -> Optional[Dict[str, Any]]:
    """default executor 안에서 budget을 thread-local로 결속한 뒤 기존 builder를 호출한다."""
    budget.checkpoint()  # queue에서 이미 만료됐으면 SessionLocal조차 열지 않는다.
    _snapshot_worker_state.budget = budget
    try:
        return _build_snapshot_sync(topic)
    finally:
        _snapshot_worker_state.budget = None


def _run_budgeted_sync_call(
    function: Callable[..., Any],
    args: tuple,
    kwargs: Dict[str, Any],
    budget: SnapshotRequestBudget,
) -> Any:
    budget.checkpoint()
    return function(*args, **kwargs)


async def run_sync_with_request_budget(
    function: Callable[..., Any],
    *args: Any,
    budget: SnapshotRequestBudget,
    **kwargs: Any,
) -> Any:
    """REST preflight 같은 sync phase를 요청 absolute deadline 안에서 실행한다."""
    remaining = budget.remaining_until(budget.request_deadline_at_mono)
    if remaining <= 0:
        budget.stop("deadline")
        raise SnapshotDeadlineExceeded(getattr(function, "__name__", "sync_phase"))
    deadline_cm = asyncio.timeout(remaining)
    try:
        async with deadline_cm:
            return await asyncio.to_thread(
                _run_budgeted_sync_call, function, args, kwargs, budget
            )
    except asyncio.CancelledError:
        budget.stop("caller_cancelled")
        raise
    except asyncio.TimeoutError as exc:
        if not deadline_cm.expired():
            raise
        budget.stop("deadline")
        raise SnapshotDeadlineExceeded(
            getattr(function, "__name__", "sync_phase")
        ) from exc
    except SnapshotWorkerStopped as exc:
        raise SnapshotDeadlineExceeded(
            getattr(function, "__name__", "sync_phase")
        ) from exc


def supported_snapshot_topics() -> tuple:
    """snapshot 빌드 가능한 topic 목록 (구현상 지원 set — availability gate와 무관).

    REST bootstrap endpoint(`/api/v2/topics/snapshot`)의 unknown_topic 검증 + client 노출용
    단일 소스. `_build_snapshot_sync`의 dispatch(fx:* + usdt:krw)와 일치. lazy import로
    모듈 경량 유지(FX_TOPICS/TETHER_TOPIC).
    """
    from app.fx_topic_publisher import FX_TOPICS  # {asset: "fx:{asset}"}
    from app.krx_topic_publisher import KRX_TOPIC  # "krx:usd-krw-futures"
    from app.tether_topic_publisher import TETHER_TOPIC  # "usdt:krw"

    topics = tuple(FX_TOPICS.values()) + (TETHER_TOPIC,)
    # ADR-038 Decision 2 — KRX 독립 topic은 G2/G3 열려 있을 때만 지원 목록에 포함
    # (off면 REST 404 unknown_topic + WS snapshot skip — 발행/snapshot 자체 중단 계약).
    if config.KRX_CLIENT_DISTRIBUTION_EFFECTIVE:
        topics += (KRX_TOPIC,)
    return topics


def is_snapshot_topic_enabled(topic: str) -> bool:
    """개별 availability flag 가 지금 켜져 있는가 — `supported_snapshot_topics()` 와 **직교**하다.

    ⛔ 두 축을 섞으면 §8-C 의 `unknown_topic`(미지원)과 `topic_unavailable`(flag off)을 구분할 수
    없다. `supported_snapshot_topics()` 는 docstring 그대로 **구현상 지원 집합**이고 availability
    gate 와 무관하다 — 그래서 그것만 보고 판정하면 **flag off 인 topic 이 accept 된다**
    (실측 재현: `FX_TOPIC_ENABLED=false` 인데 ack·registry 모두 fx 를 활성으로 기록).
    §8-C 가 경고한 "accepted 인데 데이터가 영원히 안 오는 상태"가 정확히 그것이다.

    ⚠️ **이 함수가 그 지식의 단일 소스다.** `_build_snapshot_sync` 도 이것을 쓴다 — 두 곳에
    같은 flag 검사를 두면 갈리는 순간 subscribe 판정과 실제 발사가 어긋난다.

    - fx:* → `FX_TOPIC_ENABLED`
    - usdt:krw → builder 에 flag 검사가 없다(항상 enabled)
    - krx → 배포 flag 가 이미 `supported_snapshot_topics()` 에 반영돼 있어 여기서 중복 판정하지 않는다
    """
    from app.fx_topic_publisher import FX_TOPICS

    if topic in set(FX_TOPICS.values()):
        return config.FX_TOPIC_ENABLED
    return True


def per_user_gated_snapshot_topics() -> frozenset:
    """per-user 판정(entitlement)이 **필요한** topic 집합.

    `visible_snapshot_topics_sync`의 필터와 이 집합은 **같은 지식**이라 반드시 함께 움직여야 한다.
    갈리면 `resolve_snapshot_topic_access_sync`의 shortcut이 새 게이팅 topic을 무조건 허용해
    **판정 우회 + 존재 노출**이 동시에 난다 → 집합대수 trip-wire 테스트가 잠근다
    (`set(supported) - set(visible) ⊆ per_user_gated_snapshot_topics()`).

    ⚠️ 정본은 **정책표**(`topic_policy.TOPIC_POLICY`)다 — 같은 사실("KRX 는 entitlement 필요")이
    표와 여기 두 곳에 있으면 갈린다. 여기서는 파생만 한다.
    """
    from app.topic_policy import entitlement_gated_topics

    return entitlement_gated_topics()


class SnapshotTopicAccess(NamedTuple):
    """topic 접근 판정 결과.

    `supported_topics`는 **거부일 때만** 채운다 — 허용 응답(200)은 목록을 싣지 않기 때문이다.
    이 결합을 반환 타입에 넣어, 호출부가 200에 목록을 실으려면 여기부터 바꾸게 만든다.
    """
    allowed: bool
    supported_topics: Optional[tuple]


def _assert_snapshot_entitlement_policy_supported() -> None:
    """REST 가시성 구현이 이해하는 entitlement 정책인지 확인한다.

    현재 evaluator 는 KRX 하나를 하드코딩한다. startup 검증만 믿으면 이 모듈을 직접 사용하는
    테스트·스크립트가 그 경계를 우회할 수 있으므로, 두 public 판정 함수도 같은 공유 가드를 쓴다.
    """
    from app.topic_policy import assert_single_entitlement_topic

    assert_single_entitlement_topic()


def resolve_snapshot_topic_access_sync(
    topic: str,
    user_id: str,
    *,
    premium_active: bool,
    checkpoint: Optional[Callable[[], None]] = None,
) -> SnapshotTopicAccess:
    """topic 접근 판정 — `asyncio.to_thread` 전용 (ADR-039 §8.1 E3).

    **per-user 판정이 결과를 바꿀 수 없는 요청은 entitlement를 조회하지 않는다.**
    이건 최적화가 아니라 **견고성 속성**이다: FX/USDT builder는 Redis-first라 warm이면 DB 커넥션을
    0개 쓰는데, 무조건 entitlement를 조회하면 그 요청이 **DB 필수 요청으로 승격**된다 —
    entitlement DB 순단 하나로 entitlement와 무관한 FX bootstrap이 죽고, 콜드런치 4건(FX 3 동시 +
    tether 1)이 좁은 풀(3+2)을 동시에 문다.

    등가성: `visible ⊆ supported` ∧ `supported \\ visible ⊆ per_user_gated` 이므로
    `topic ∈ supported ∧ topic ∉ per_user_gated ⟹ topic ∈ visible`. 즉 판정 결과가 항상 같고,
    허용 경로는 목록을 싣지 않으므로 §3.2도 그대로다.

    ⛔ **더 나가지 말 것**: "미지원 topic은 어차피 거부니 조회 없이 전역 목록으로 반환"은
    (a) 에코에 KRX가 실려 존재가 노출되고 (b) 비-entitled KRX(조회 1회)와 미지원 topic(0회)이
    지연으로 갈린다 — §3.2 두 축이 동시에 깨진다. `test_unknown_topic_still_consults_entitlement`가
    이 회귀를 red로 만든다.

    ℹ️ `premium_active`는 premium **게이트가 아니다** — premium 강제는 호출부(`require_premium`)
    소관이고 이 값은 `compute_krx_visible`에 그대로 전달될 뿐이다. shortcut이 이 값을 보지 않는 건
    비-KRX topic의 판정이 premium과 무관하기 때문이다(어느 값이든 결과 동일).
    """
    if checkpoint is not None:
        checkpoint()
    _assert_snapshot_entitlement_policy_supported()

    global_topics = supported_snapshot_topics()
    if topic in global_topics and topic not in per_user_gated_snapshot_topics():
        return SnapshotTopicAccess(True, None)

    visible_kwargs = {"checkpoint": checkpoint} if checkpoint is not None else {}
    visible = visible_snapshot_topics_sync(
        user_id, premium_active=premium_active, **visible_kwargs
    )
    if topic in visible:
        return SnapshotTopicAccess(True, None)
    return SnapshotTopicAccess(False, visible)


def visible_snapshot_topics_sync(
    user_id: str,
    *,
    premium_active: bool,
    checkpoint: Optional[Callable[[], None]] = None,
) -> tuple:
    """**per-user** 노출 topic 목록 = 전역 지원 집합 ∧ KRX 가시성 — `asyncio.to_thread` 전용
    (ADR-039 §8.1 E3).

    `supported_snapshot_topics()`는 전역 게이트(G2∧G3)까지만 좁힌다. REST bootstrap은 그 위에
    per-user G1∧premium을 한 번 더 적용해야 §3.1 매트릭스("KRX WS·REST snapshot = Firebase +
    premium + KRX entitlement")를 만족한다.

    §3.2(KRX 존재 완전 비노출): entitlement 없는 사용자에게 KRX는 **미지원 topic과 구분 불가**여야
    하므로 unknown_topic 판정과 `supported_topics` 에코가 **이 함수 하나**를 공유한다 — 둘이 갈리면
    404 코드는 맞는데 목록으로 존재가 새는 회귀가 난다.

    **세션 수명이 계약이다** (`_build_snapshot_sync`와 동일 규율):
    SessionLocal 생성 → 조회 → close를 이 함수(=worker thread) 안에서 **완결**한다.
    - 호출자(핸들러)가 request-scoped 세션을 들고 있으면, 그 커넥션을 쥔 채 뒤이어 builder가
      **두 번째** SessionLocal을 연다. 운영 풀은 `pool_size=3 + max_overflow=2` = 5뿐이라
      bootstrap 동시 요청 3건이면 서로의 builder 커넥션을 기다리다 pool timeout에 걸린다.
    - 동기 SELECT를 이벤트 루프에서 직접 돌리면 RDS 순단 시 **worker 전체**(WS pong 포함)가 멈춘다.
    - SQLAlchemy Session은 thread-unsafe라 thread 경계를 넘기지 않는다.

    전역 게이트가 닫혀 있으면 세션을 **아예 열지 않는다** — 결과가 같은데 장애 표면만 늘기 때문.
    반대로 열려 있으면 반드시 조회한다(fail-closed: 조회 실패는 예외로 전파 → 5xx, 조용한 False 아님).
    """
    if checkpoint is not None:
        checkpoint()
    _assert_snapshot_entitlement_policy_supported()

    from app import entitlements
    from app.krx_topic_publisher import KRX_TOPIC

    topics = supported_snapshot_topics()
    if KRX_TOPIC not in topics:
        return topics

    from app.database import SessionLocal
    if checkpoint is not None:
        checkpoint()
    db = SessionLocal()
    try:
        if checkpoint is not None:
            checkpoint()
        visible = entitlements.compute_krx_visible(
            db, user_id, premium_active=premium_active)
    except BaseException:
        try:
            db.rollback()
        except Exception:
            logger.warning("snapshot entitlement session rollback 실패", exc_info=True)
        raise
    finally:
        db.close()
    if visible:
        return topics
    return tuple(t for t in topics if t != KRX_TOPIC)


@dataclass(frozen=True)
class SnapshotBuildKey:
    """payload를 바꿀 수 있는 process-local 입력을 모두 포함한 LOAD-S5 key."""

    topic: str
    generation: int
    supported: bool
    enabled: bool


@dataclass
class _SnapshotFlight:
    task: "asyncio.Task[Optional[Dict[str, Any]]]"
    waiters: int = 0


class _SnapshotAdmissionPermit:
    """Shared build에 소유권을 넘기는 LOAD-S6 slot."""

    def __init__(self, semaphore: asyncio.Semaphore, *, metric_tracked: bool) -> None:
        self._semaphore = semaphore
        self._metric_tracked = metric_tracked
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()
        subscribe_load.record_snapshot_admission_released(
            tracked=self._metric_tracked
        )


class _SnapshotAdmission:
    """새 shared build만 제한하는 FIFO admission.

    `asyncio.Semaphore.acquire()`는 지원 Python 3.13에서 기존 waiter를 FIFO로 깨운다. locked 상태를
    즉시거절에 쓰지 않고 항상 caller의 남은 S3 예산 안에서 기다린다. 대기열 개수에는 hard cap이
    없으므로 이 클래스가 보장하는 것은 admission permit을 가진 shared task 수와 대기 **시간**의
    상한뿐이다. 취소된 sync worker는 다음 협력 checkpoint까지 executor에서 잠시 남을 수 있다.
    """

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent <= 0:
            raise ValueError("snapshot admission max_concurrent must be positive")
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def acquire(
        self, topic: str, budget: SnapshotRequestBudget
    ) -> _SnapshotAdmissionPermit:
        remaining = budget.remaining_snapshot_seconds()
        if remaining <= 0:
            budget.stop("deadline")
            raise SnapshotDeadlineExceeded(topic)

        queued = self._semaphore.locked()
        wait_started = (
            subscribe_load.begin_snapshot_admission_wait() if queued else None
        )
        deadline_cm = asyncio.timeout(remaining)
        try:
            try:
                async with deadline_cm:
                    await self._semaphore.acquire()
            except asyncio.TimeoutError as exc:
                if not deadline_cm.expired():
                    raise
                budget.stop("deadline")
                raise SnapshotDeadlineExceeded(topic) from exc
        finally:
            if queued:
                subscribe_load.finish_snapshot_admission_wait(wait_started)

        # acquire 이후 다음 await 전에는 cancellation이 주입되지 않으므로 permit 소유권이 갈리지 않는다.
        metric_tracked = subscribe_load.record_snapshot_admission_acquired()
        return _SnapshotAdmissionPermit(
            self._semaphore, metric_tracked=metric_tracked
        )


@dataclass(frozen=True)
class _SnapshotSuccessCacheEntry:
    # cache-owned dict는 caller에게 직접 노출하지 않는다. 모든 반환은 deepcopy다.
    payload: Dict[str, Any]
    expires_at_mono: float


@dataclass
class _SnapshotFailureCooldownEntry:
    """성공 cache와 분리된 LOAD-S7 transient failure 상태."""

    failure_class: str
    expires_at_mono: float
    suppressed_builds: int = 0


@dataclass
class _SnapshotSingleflightState:
    """asyncio 객체와 성공 cache를 event loop 경계 안에 가둔다."""

    flights: Dict[SnapshotBuildKey, _SnapshotFlight] = field(default_factory=dict)
    success_cache: Dict[SnapshotBuildKey, _SnapshotSuccessCacheEntry] = field(
        default_factory=dict
    )
    failure_cooldowns: Dict[SnapshotBuildKey, _SnapshotFailureCooldownEntry] = field(
        default_factory=dict
    )
    admission: _SnapshotAdmission = field(
        default_factory=lambda: _SnapshotAdmission(
            config.WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS
        )
    )


_snapshot_states: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _SnapshotSingleflightState]" = (
    weakref.WeakKeyDictionary()
)
_snapshot_states_lock = threading.Lock()


def _snapshot_state() -> _SnapshotSingleflightState:
    loop = asyncio.get_running_loop()
    with _snapshot_states_lock:
        state = _snapshot_states.get(loop)
        if state is None:
            state = _SnapshotSingleflightState()
            _snapshot_states[loop] = state
        return state


def _snapshot_build_key(topic: str) -> SnapshotBuildKey:
    from app.topic_dispatcher import topic_payload_generation

    return SnapshotBuildKey(
        topic=topic,
        generation=topic_payload_generation(topic),
        supported=topic in set(supported_snapshot_topics()),
        enabled=is_snapshot_topic_enabled(topic),
    )


def _prune_snapshot_success_cache(
    state: _SnapshotSingleflightState, now_mono: float
) -> None:
    expired = [
        key for key, entry in state.success_cache.items()
        if entry.expires_at_mono <= now_mono
    ]
    for key in expired:
        state.success_cache.pop(key, None)


def _prune_snapshot_failure_cooldowns(
    state: _SnapshotSingleflightState, now_mono: float
) -> None:
    expired = [
        key
        for key, entry in state.failure_cooldowns.items()
        if entry.expires_at_mono <= now_mono
    ]
    for key in expired:
        entry = state.failure_cooldowns.pop(key)
        subscribe_load.record_snapshot_failure_cooldown_expired(
            failure_class=entry.failure_class
        )


def _active_snapshot_failure_cooldown(
    state: _SnapshotSingleflightState,
    key: SnapshotBuildKey,
    now_mono: float,
) -> Optional[SnapshotFailureCooldownActive]:
    entry = state.failure_cooldowns.get(key)
    if entry is None:
        return None
    remaining = max(0.0, entry.expires_at_mono - now_mono)
    if remaining <= 0:
        return None

    entry.suppressed_builds += 1
    subscribe_load.record_snapshot_failure_cooldown_suppressed(
        topic=key.topic,
        generation=key.generation,
        supported=key.supported,
        enabled=key.enabled,
        failure_class=entry.failure_class,
        remaining_seconds=remaining,
        suppressed_builds=entry.suppressed_builds,
    )
    return SnapshotFailureCooldownActive(
        key.topic,
        failure_class=entry.failure_class,
        remaining_seconds=remaining,
        suppressed_builds=entry.suppressed_builds,
    )


def _arm_snapshot_failure_cooldown(
    state: _SnapshotSingleflightState,
    key: SnapshotBuildKey,
    failure_class: str,
) -> None:
    state.failure_cooldowns[key] = _SnapshotFailureCooldownEntry(
        failure_class=failure_class,
        expires_at_mono=(
            time.monotonic() + config.WS_TOPIC_SNAPSHOT_FAILURE_COOLDOWN_SECONDS
        ),
    )
    subscribe_load.record_snapshot_failure_cooldown_armed(
        failure_class=failure_class
    )


def _cached_snapshot(
    state: _SnapshotSingleflightState, key: SnapshotBuildKey
) -> Optional[Dict[str, Any]]:
    now_mono = time.monotonic()
    _prune_snapshot_success_cache(state, now_mono)
    entry = state.success_cache.get(key)
    if entry is None:
        return None
    return copy.deepcopy(entry.payload)


def _consume_shared_task_exception(task: "asyncio.Task") -> None:
    """마지막 waiter 취소와 task 실패가 경합해도 미회수 task 예외를 남기지 않는다."""
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def _run_shared_snapshot_build(
    state: _SnapshotSingleflightState,
    key: SnapshotBuildKey,
    budget: SnapshotRequestBudget,
) -> Optional[Dict[str, Any]]:
    """registry entry의 유일한 정리 소유자. 실패/취소는 성공 cache에 넣지 않는다."""
    task = asyncio.current_task()
    permit: Optional[_SnapshotAdmissionPermit] = None
    try:
        # Flight를 registry에 먼저 게시한 뒤 shared task가 slot을 기다린다. 반대 순서면 같은 key의
        # 후속 요청도 각각 admission에 줄을 서서 이미 생긴 flight를 build 완료 전까지 join 못 한다.
        permit = await state.admission.acquire(key.topic, budget)
        try:
            payload = await _build_snapshot_once_observed(key.topic, budget=budget)
        except Exception as exc:
            failure_class = _snapshot_transient_failure_class(exc)
            if failure_class is not None:
                _arm_snapshot_failure_cooldown(state, key, failure_class)
            raise
        state.failure_cooldowns.pop(key, None)
        if payload is not None and _snapshot_build_key(key.topic) == key:
            state.success_cache[key] = _SnapshotSuccessCacheEntry(
                payload=copy.deepcopy(payload),
                expires_at_mono=(
                    time.monotonic() + config.WS_TOPIC_SNAPSHOT_CACHE_TTL_SECONDS
                ),
            )
            subscribe_load.record_snapshot_success_cached()
        return payload
    finally:
        if permit is not None:
            permit.release()
        current = state.flights.get(key)
        if current is not None and current.task is task:
            state.flights.pop(key, None)


async def build_snapshot_observed(
    topic: str, *, budget: Optional[SnapshotRequestBudget] = None
) -> Optional[Dict[str, Any]]:
    """WS/REST가 공유하는 LOAD-S5 single-flight + 짧은 성공 cache 진입점.

    authorization은 이 함수 **전에** 끝나며 key에 사용자 정보가 없다. 공유 범위는 payload build뿐이다.
    각 waiter는 자기 S3 budget으로 기다리고, shared task는 caller와 분리된 S3 budget을 쓴다.
    """
    request_budget = budget or SnapshotRequestBudget()
    remaining = request_budget.remaining_snapshot_seconds()
    if remaining <= 0:
        request_budget.stop("deadline")
        raise SnapshotDeadlineExceeded(topic)

    state = _snapshot_state()
    key = _snapshot_build_key(topic)
    _prune_snapshot_failure_cooldowns(state, time.monotonic())
    cached = _cached_snapshot(state, key)
    if cached is not None:
        subscribe_load.record_snapshot_singleflight("cache_hit")
        return cached

    cooldown_error = _active_snapshot_failure_cooldown(
        state, key, time.monotonic()
    )
    if cooldown_error is not None:
        raise cooldown_error

    flight = state.flights.get(key)
    if flight is not None and (flight.task.done() or flight.task.cancelling()):
        # 마지막 waiter가 cancel을 요청했지만 shared task의 finally가 아직 registry를 지우기 전인
        # 짧은 창이다. 새 caller를 취소 중 task에 붙이지 않고 새 entry로 교체한다. 이전 task의
        # finally는 task identity가 다르므로 새 entry를 지우지 못한다.
        flight = None
    if flight is None:
        build_budget = request_budget.fork_for_shared_build()
        build_budget.start_snapshot_phase()
        task = asyncio.create_task(
            _run_shared_snapshot_build(state, key, build_budget)
        )
        task.add_done_callback(_consume_shared_task_exception)
        flight = _SnapshotFlight(task=task)
        state.flights[key] = flight
        disposition = "leader"
    else:
        disposition = "joined"
    flight.waiters += 1
    waiter_metric_tracked = subscribe_load.record_snapshot_singleflight(disposition)

    deadline_cm = asyncio.timeout(remaining)
    try:
        try:
            async with deadline_cm:
                payload = await asyncio.shield(flight.task)
        except asyncio.TimeoutError as exc:
            if not deadline_cm.expired():
                raise
            request_budget.stop("deadline")
            raise SnapshotDeadlineExceeded(topic) from exc
        return None if payload is None else copy.deepcopy(payload)
    except asyncio.CancelledError:
        request_budget.stop("caller_cancelled")
        raise
    finally:
        flight.waiters -= 1
        subscribe_load.record_snapshot_waiter_released(tracked=waiter_metric_tracked)
        if flight.waiters == 0 and not flight.task.done():
            # waiter는 취소 요청만 한다. registry 제거는 shared task의 finally 한 곳이 소유한다.
            flight.task.cancel()


async def _build_snapshot_once_observed(
    topic: str, *, budget: Optional[SnapshotRequestBudget] = None
) -> Optional[Dict[str, Any]]:
    """실제 shared snapshot build 1회의 관측 경로.

    ⛔ 두 진입점이 각자 배선하면 축이 갈린다 — 같은 default executor 와 같은 I/O 를 쓰므로
       용량 산정(S6)에는 **합산**이 필요하다. 그래서 호출부가 아니라 여기서 한 번 감싼다.
    ⛔ 계측은 `_build_snapshot_sync` **본문이 아니라** 이 async 래퍼에 둔다. queue_wait 은
       (worker 시작 − caller 제출)이라 caller 쪽 시각이 있어야 하고, 본문은 worker 시작
       이후만 볼 수 있다. 본문 무계측은 별도 trip-wire 가 잠근다.
    ⛔ 예외를 삼키지 않는다 — 종결 정책은 호출부가 소유한다(WS 는 transient/deadline 1013,
       fatal 1011; REST twin 은 retryable deadline/DB 장애를 503으로 변환). 여기서 잡으면 두
       계약이 뭉개진다.
    """
    request_budget = budget or SnapshotRequestBudget()
    remaining = request_budget.remaining_snapshot_seconds()
    if remaining <= 0:
        request_budget.stop("deadline")
        raise SnapshotDeadlineExceeded(topic)

    deadline_cm = None
    try:
        async with subscribe_load.observe(subscribe_load.SNAPSHOT_BUILD) as load:
            # ⛔ **to_thread 직전에** 찍는다. CM 진입 시점에 찍으면 그 사이 caller 작업이
            #    큐 대기로 잘못 계상된다.
            load.mark_submitted()
            deadline_cm = asyncio.timeout(remaining)
            try:
                async with deadline_cm:
                    payload = await asyncio.to_thread(
                        subscribe_load.timed_call, subscribe_load.SNAPSHOT_BUILD, load,
                        _run_snapshot_worker, topic, request_budget,
                    )
            except asyncio.CancelledError:
                # 계측 context를 정리하기 **전에** worker에 알린다. 반대 순서면 현재 I/O가
                # 그 짧은 창에서 끝나 다음 phase checkpoint를 통과할 수 있다.
                request_budget.stop("caller_cancelled")
                raise
            except asyncio.TimeoutError as exc:
                if not deadline_cm.expired():
                    raise
                request_budget.stop("deadline")
                raise SnapshotDeadlineExceeded(topic) from exc
            load.finish("none_payload" if payload is None else "built")
            return payload
    except asyncio.CancelledError:
        # caller task 취소와 worker 중단은 별개다. worker가 다음 I/O를 시작하지 않도록 신호만
        # 보내고, Session/connection은 worker 자신이 rollback/close한다.
        request_budget.stop("caller_cancelled")
        raise
    except asyncio.TimeoutError:
        # worker/socket이 스스로 던진 TimeoutError는 deadline으로 오분류하지 않는다.
        raise
    except SnapshotWorkerStopped as exc:
        raise SnapshotDeadlineExceeded(topic) from exc


def _build_snapshot_sync(topic: str) -> Optional[Dict[str, Any]]:
    """주어진 topic의 현재 snapshot payload 빌드 — asyncio.to_thread 내부 sync 실행 전용.

    SessionLocal 생성 / builder 호출 / close를 모두 이 함수(=worker thread) 안에서 처리한다.
    SQLAlchemy Session은 thread-unsafe라 절대 thread 경계를 넘기지 않는다 (codex 019efdb3 blocker 1).

    Returns:
        payload dict (publisher와 동일하게 ``topic`` 필드 inject) — 빈 데이터도 schema-valid면 반환.
        None — 미지원 topic(매핑 없음) 또는 enable-gate off.
    """
    _worker_checkpoint()

    # 토픽 이름 single source: publisher 상수 재사용(하드코딩 "fx:"/"usdt:krw" 회피). lazy import로
    # dispatcher import 그래프 경량 유지(첫 subscribe 시에만 builder 체인 로드).
    from app.fx_topic_publisher import FX_TOPICS  # {asset: "fx:{asset}"}
    from app.tether_topic_publisher import TETHER_TOPIC  # "usdt:krw"

    fx_asset_by_topic = {channel: asset for asset, channel in FX_TOPICS.items()}

    if topic in fx_asset_by_topic:
        # fx snapshot은 FX_TOPIC_ENABLED 존중(publisher _publish_fx_snapshot과 일관 — off면 미발사).
        # ⚠️ 판정은 `is_snapshot_topic_enabled` 가 소유한다 — subscribe 분류와 같은 소스여야 한다.
        if not is_snapshot_topic_enabled(topic):
            return None
        asset = fx_asset_by_topic[topic]
        from app.database import SessionLocal
        from app.fx_topic_payload import (
            FX_TOPIC_BANK_ORDER,
            load_and_build_fx_topic_payload,
        )

        _worker_checkpoint()
        db = SessionLocal()
        try:
            # public fx:* snapshot도 Citi 제외 8-bank (publish 경로와 동일). atomic/legacy는 기본 9.
            checkpoint = _active_worker_checkpoint()
            kwargs = {"checkpoint": checkpoint} if checkpoint is not None else {}
            payload = load_and_build_fx_topic_payload(
                db, asset, bank_order=FX_TOPIC_BANK_ORDER, **kwargs
            )
        except BaseException:
            try:
                db.rollback()
            except Exception:
                logger.warning("FX snapshot session rollback 실패", exc_info=True)
            raise
        finally:
            db.close()
        payload["topic"] = topic
        return payload

    if topic == TETHER_TOPIC:
        from app.database import SessionLocal
        from app.usdt_topic_payload import load_and_build_tether_tab_payload

        _worker_checkpoint()
        db = SessionLocal()
        try:
            # ADR-038 Decision 2 — usd_krw_futures group 제거: KRX는 독립 topic 전용
            checkpoint = _active_worker_checkpoint()
            kwargs = {"checkpoint": checkpoint} if checkpoint is not None else {}
            payload = load_and_build_tether_tab_payload(db, **kwargs)
        except BaseException:
            try:
                db.rollback()
            except Exception:
                logger.warning("USDT snapshot session rollback 실패", exc_info=True)
            raise
        finally:
            db.close()
        payload["topic"] = topic
        return payload

    from app.krx_topic_publisher import KRX_TOPIC, build_krx_topic_payload, load_krx_topic_entry
    if topic == KRX_TOPIC:
        # ADR-038 — G2/G3 off면 미지원 취급 (supported 목록과 일관)
        if not config.KRX_CLIENT_DISTRIBUTION_EFFECTIVE:
            return None
        from app.database import SessionLocal
        _worker_checkpoint()
        db = SessionLocal()
        try:
            checkpoint = _active_worker_checkpoint()
            kwargs = {"checkpoint": checkpoint} if checkpoint is not None else {}
            entry = load_krx_topic_entry(db, **kwargs)  # Redis-first + DB fallback
        except BaseException:
            try:
                db.rollback()
            except Exception:
                logger.warning("KRX snapshot session rollback 실패", exc_info=True)
            raise
        finally:
            db.close()
        if entry is None:
            return None   # 데이터 없음 — snapshot skip (구독 register는 유지)
        return build_krx_topic_payload(entry)

    return None  # 미지원 topic(dxy/news/graph 등) — snapshot skip, register는 호출자가 유지


async def send_initial_snapshots(
    websocket: "WebSocket",
    topics: List[str],
    *,
    channel: str = "unattributed",
    budget: Optional[SnapshotRequestBudget] = None,
) -> int:
    """subscribe 직후 요청 topic들의 현재 snapshot을 이 ws에 즉시 전송.

    `channel` 은 subscribe-load 관측 전용이다(anonymous / token_bearing) — **wire 의
    verdict·payload·전파 예외 계약은 불변**이다(계측 상태 갱신·lock 획득은 생긴다).
    미지정 기본값 `unattributed` 는 "배선 없이 추가된 신규 호출부" 신호다.

    handle_client_message subscribe 분기에서 registry.register 직후 호출 — 이 시점은
    TOPIC_DISPATCHER_ENABLED를 이미 통과한 상태.

    종결 정책 (R-HAND-2 / LOAD-S3):
    - build deadline·transient 실패 → terminal payload 없이 close 1013.
    - 프로그래밍·직렬화 fatal 실패 → terminal payload 없이 close 1011.
    - send 실패 = connection 실패 → registry에서 ws 제거 + 남은 snapshot 중단(반환).
      (publish_topic의 stale-entry 정리 패턴과 일관.)
    - 요청 내 중복 topic은 de-dupe(같은 subscribe에서 같은 topic 1회만). 재구독(별 요청)은
      resync로 유용하므로 막지 않음.
    - **전송 직전 lease 재검증** — 아래 주석 참조. 게이트에 걸린 topic 은 skip 이고
      나머지 topic 은 계속한다(연결 실패가 아니다).

    Returns:
        성공적으로 send 완료한 snapshot 수.
    """
    # ⛔ 발행 게이트를 **여기서 다시 구현하지 않는다.** 판정을 복제하면 두 게이트가 갈린다.
    #    `leased_subscribers` 의 "모든 발행 경로가 공유하는 단일 게이트"라는 주석은 이
    #    호출이 생기기 전까지 **거짓이었다** — snapshot 경로가 그것을 우회했다.
    # ⚠️ import 를 함수 안에 두는 이유는 **순환 방지**다: `topic_dispatcher` 는 이 모듈을
    #    subscribe 분기에서 lazy import 한다. 여기서 top-level 로 되받으면 두 모듈이
    #    서로를 module scope 에서 참조하게 되어, 훗날 dispatcher 쪽이 top-level 로
    #    바뀌는 순간 부분 초기화 오류가 된다.
    from app.topic_dispatcher import leased_subscribers

    request_budget = budget or SnapshotRequestBudget()
    request_budget.start_snapshot_phase()
    subscribe_load.record_snapshot_call(channel)
    sent = 0
    seen: set = set()
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)
        # ⛔ dedupe **통과분만** 센다 — 요청 1건이 몇 배 topic 으로 퍼지는지의 분자.
        #    build 실패 topic 도 수요였으므로 여기(=build 앞)서 센다.
        subscribe_load.record_snapshot_topic()

        try:
            # ⛔ 관측 CM 은 종결 분류 **안**이다 — build 예외는 `__aexit__` 를 먼저 통과해
            #    `build_failed` 로 파생 기록된 뒤 1013/1011 경계에서 분류된다.
            #    `_build_snapshot_sync` 본문은 무계측이다(REST twin 이 공유 — trip-wire 로 잠금).
            payload = await build_snapshot_observed(topic, budget=request_budget)
        except SnapshotFailureCooldownActive as exc:
            await _close_snapshot_connection(websocket, code=1013)
            raise InitialSnapshotTransientFailure(topic) from exc
        except SnapshotDeadlineExceeded as exc:
            await _close_snapshot_connection(websocket, code=1013)
            raise InitialSnapshotDeadlineExceeded(topic) from exc
        except Exception as exc:
            if _is_transient_snapshot_error(exc):
                logger.warning(
                    "initial snapshot 일시 장애 — 1013 종료",
                    extra={"topic": topic},
                    exc_info=True,
                )
                await _close_snapshot_connection(websocket, code=1013)
                raise InitialSnapshotTransientFailure(topic) from exc
            logger.error(
                "initial snapshot fatal build 오류 — 1011 종료",
                extra={"topic": topic},
                exc_info=True,
            )
            await _close_snapshot_connection(websocket, code=1011)
            raise InitialSnapshotFatalFailure(topic) from exc

        if payload is None:
            continue  # 미지원 topic / enable-gate off

        # ⛔ **build 뒤, send 직전**에 본다. 위험한 창이 그 구간이기 때문이다 —
        #    `_build_snapshot_sync` 는 `to_thread` 로 돌고 인증 wire deadline 밖이다. LOAD-S3가
        #    snapshot 총예산을 두더라도 build 사이에 lease가 만료될 수 있으므로 발급 시점 검사만으론
        #    S5의 15분 revoke 상한이 보증되지 않는다(발행 경로처럼 전송 직전에 다시 본다).
        # ⛔ `registry.get_lease(...) is None` 으로 자체 판정하지 말 것 — 그 `None` 은
        #    **"무토큰(§E1) 구독"과 "등록이 사라짐"을 구분하지 못한다**. 그렇게 짜면 방금
        #    취소된 구독에 데이터를 보내는 fail-open 이 된다. `leased_subscribers` 는
        #    구독자 집합에서 출발하므로 두 경우가 갈린다.
        # ⚠️ 프로덕션 정상 경로에서는 항상 통과한다 — 호출자가 등록 직후 넘기기 때문이다
        #    (익명 `free_topics` / 식별 `accepted_names` 모두 register 된 것만). 이 게이트는
        #    **동시 unsubscribe·축출·만료**에서만 발화한다.
        if websocket not in leased_subscribers(topic):
            # ⛔ build 를 **다 하고 버린** 낭비 — 폭주가 스스로를 키우는 구간의 신호.
            subscribe_load.record_snapshot_send("lease_skipped")
            logger.info(
                "initial snapshot 전송 직전 구독이 사라졌거나 lease 가 만료됐다 — skip",
                extra={"topic": topic, "sent": sent},
            )
            continue

        try:
            remaining = request_budget.remaining_snapshot_seconds()
            if remaining <= 0:
                request_budget.stop("deadline")
                raise SnapshotDeadlineExceeded(topic)
            send_cm = asyncio.timeout(remaining)
            try:
                async with send_cm:
                    await websocket.send_json(payload)
            except asyncio.TimeoutError as exc:
                if not send_cm.expired():
                    raise
                request_budget.stop("deadline")
                raise SnapshotDeadlineExceeded(topic) from exc
            sent += 1
            subscribe_load.record_snapshot_send("sent")
        except asyncio.CancelledError:
            request_budget.stop("caller_cancelled")
            raise
        except SnapshotDeadlineExceeded:
            await _close_snapshot_connection(websocket, code=1013)
            raise InitialSnapshotDeadlineExceeded(topic)
        except WebSocketDisconnect as exc:
            # ⛔ **삼키고 반환하지 않는다.** 여기서 정상 반환하면 endpoint 의 `while True` 가
            #    **닫힌 소켓에 `receive_text()` 를 다시 호출**해 `RuntimeError` → `except
            #    Exception` → **ERROR + traceback** 이 된다. 정상적인 클라 종료가 ERROR 채널을
            #    오염시키고, 운영 canary 가 실제로 그 때문에 중단됐다.
            #    → **연결 제어 신호로 전파**한다(endpoint 가 정상 종료 축으로 처리, INFO 1건).
            # ⛔ **`except Exception` 이면 안 된다.** 그러면 JSON 직렬화 `TypeError` 같은
            #    프로그래밍 오류까지 "정상 종료"로 접혀 INFO 로 사라진다 — 진짜 결함이 조용해진다.
            #    starlette 는 실제 전송 단절에 `WebSocketDisconnect` 를 쓴다(운영 traceback 로
            #    확인: `starlette.websockets.WebSocketDisconnect`). 그것만 신호로 바꾼다.
            from app.topic_dispatcher import registry

            registry.remove_websocket(websocket)
            subscribe_load.record_snapshot_send("connection_closed")
            logger.debug(
                "initial snapshot send 실패 — 연결 종료 신호로 전파",
                extra={"topic": topic, "sent": sent},
            )
            raise InitialSnapshotConnectionClosed(topic) from exc
        except Exception:
            # ⛔ **`Exception` 한정** — `BaseException` 으로 넓히면 취소(CancelledError)가
            #    `raised`(실제 전송 예외 신호)를 오염시킨다. 기록만 하고 **그대로 전파** —
            #    위 주석의 규율(프로그래밍 오류를 접지 않는다)은 불변이다.
            subscribe_load.record_snapshot_send("raised")
            raise

    return sent
