# app/subscribe_load_metrics.py
"""subscribe-load 계측 — WS subscribe 경로의 외부축 관측 (process-local, dormant).

⛔ **이름이 계약이다.** 이것은 *subscribe-load* 이지 *reconnect-load* 가 아니다 —
   `send_initial_snapshots` 도 `handle_client_message` 도 연결 이력을 인자로 받지 않으므로,
   재연결 / 최초 연결 / 일반 재구독을 **구분할 수 없다**. "재연결" 귀속은 통제된 시나리오의
   전후 delta 로만 주장한다.

⛔ **leaf 가 아니라 WS 전용 seam 에 붙인다.** `app/subscription.py` 의
   `fetch_revenuecat_result` 는 WS(`topic_authorization.py`)와 REST(`subscription.py` 의
   `_check_revenuecat_entitlement`)가 공유한다 — 거기 계측하면 REST 왕복이 섞인다.

## caller 축 ⊥ worker 축

`asyncio.to_thread` 는 **취소를 전파하지 않는다**(`app/topic_dispatcher.py:584-586` 이 명시).
caller 쪽만 재면 양방향으로 어긋난다 — 포기 뒤에도 도는 스레드는 과소, 큐에서 취소돼 실행조차
안 된 건은 과대. 그래서 두 축을 나눈다:
  - caller 축: `callers_awaiting` (점유가 아니라 **대기**), `duration_ms_*`
  - worker 축: `worker_started_total` / `worker_finished_total` / `worker_in_flight`
⚠️ **단순 차이는 잔여가 아니다** — 대기 중이지만 worker 가 아직 시작하지 않은 caller 도 그
차이에 들어가므로 대략 `버려진 채 도는 worker − 큐에서 대기 중인 caller` 다.
`callers_awaiting == 0` 인 정지 상태 캡처에서만 `worker_in_flight` 를 잔여 worker 로 읽을 수 있다.

⚠️ `premium_rc` 에는 worker 축이 **없다**(비대칭은 의도) — async HTTP 라 취소가 실제 전파된다.
   항상 0 인 필드를 만들면 "정상인데 0" 과 "고장나서 0" 이 섞인다.

⛔ **취소 하위분류는 관측 사실일 뿐이다.** `ThreadPoolExecutor` 는 wrapper 호출 **전에** future
   를 running 으로 바꾸므로 worker-start 이벤트 set 이전에 경합 창이 있고, `asyncio.to_thread`
   는 그 underlying future 를 노출하지 않는다(`app/auth_executor.py:196-245` 가 done-callback
   `future.cancelled()` 로 이 문제를 푼 이유). 따라서 두 취소 키 중 **어느 쪽도 "DB 세션 0" 을
   함의하지 않는다** — 실제 작업량의 진실은 worker 축이 말한다.

## exactly-once 를 규율이 아니라 구조로

`finish(outcome)` 은 **후보를 저장만** 하고, 실제 terminal 전이는 `__aexit__` 가 정확히 한 번
수행한다. `finish()` 뒤에도 scope 안에서 예외가 날 수 있어 "finish 가 마지막" 이라는 규율은
깨지기 쉽다.

## dormancy

호출자가 없으면 아무 것도 돌지 않는다. 배선 뒤에도 wire 동작은 불변이다 — task/thread/env flag/
외부 I/O 0, in-memory int·float 갱신만. ⚠️ 단 **"동작 변화 0" 이라고 쓰면 거짓**이다: event-loop
caller 와 worker 가 같은 `threading.Lock` 을 잡으므로 loop 스레드에 lock 획득이 생긴다.
SLO 가 없으므로 그 비용을 사전 승인하지 않는다 — 배포 후 observer effect 확인 대상.

⚠️ **운영 반영은 관찰 창 종료(2026-08-20) 후**다. `--force-recreate` 가 process-local counter 를
   리셋하고 그건 곧 새 관찰 창이다.
"""

# 표준 라이브러리
import asyncio
import contextlib
import logging
import sys
import threading
import time
from typing import Any, Callable, Iterator, Optional, TypeVar

logger = logging.getLogger("exchange_rate.subscribe_load")

T = TypeVar("T")

# ⛔ 필드 의미가 바뀔 때만 **사람이** 올린다 — delta 도구가 KeyError 대신 "계약이 다르다" 로
#    빨리 실패하게 하는 값이다.
CONTRACT_VERSION = "subscribe-load/1"

PREMIUM_RC = "premium_rc"
KRX_ENTITLEMENT = "krx_entitlement"
SNAPSHOT_BUILD = "snapshot_build"

# ⛔ 고정 cardinality — 요청 문자열이 key 를 늘릴 수 없다. allowlist 밖 outcome 은 동적 key 를
#    만들지 않고 `unclassified` 로 접는다(`app/topic_auth_rollout.py` 의 규율과 동형).
_UNCLASSIFIED = "unclassified"
_CANCEL_NOT_OBSERVED = "cancel_worker_start_not_observed"
_CANCEL_OBSERVED = "cancel_worker_start_observed"

AXIS_OUTCOMES: dict[str, tuple[str, ...]] = {
    PREMIUM_RC: (
        "granted", "denied", "unavailable_transient", "unavailable_persistent",
        "cancelled", "raised", _UNCLASSIFIED,
    ),
    KRX_ENTITLEMENT: (
        "granted", "denied", "unavailable_transient", "unavailable_persistent",
        _CANCEL_NOT_OBSERVED, _CANCEL_OBSERVED, "raised", _UNCLASSIFIED,
    ),
    SNAPSHOT_BUILD: (
        "built", "none_payload", "build_failed",
        _CANCEL_NOT_OBSERVED, _CANCEL_OBSERVED, _UNCLASSIFIED,
    ),
}

# worker 축을 갖는 축 — `to_thread` 로 도는 두 개만.
WORKER_AXES: frozenset[str] = frozenset({KRX_ENTITLEMENT, SNAPSHOT_BUILD})

# 예외 종료 시 쓰는 축별 outcome. snapshot 은 build 실패가 곧 도메인 결과라 이름이 다르다.
_RAISED_OUTCOME: dict[str, str] = {
    PREMIUM_RC: "raised",
    KRX_ENTITLEMENT: "raised",
    SNAPSHOT_BUILD: "build_failed",
}

CHANNELS: tuple[str, ...] = ("anonymous", "token_bearing", "unattributed")
SEND_OUTCOMES: tuple[str, ...] = ("sent", "lease_skipped", "connection_closed", "raised")

CAVEAT = (
    "subscribe-load 이며 reconnect 귀속이 아니다 · max/gauge 는 두 캡처 사이에 빼지 말 것 · "
    "프로세스 재기동 시 0 · REST twin 은 포함하지 않는다 · callers_awaiting 은 대기이지 점유가 아니다"
)

_lock = threading.Lock()
_metrics: dict[str, Any] = {}


def _blank() -> dict[str, Any]:
    axes: dict[str, Any] = {}
    for axis, outcomes in AXIS_OUTCOMES.items():
        block: dict[str, Any] = {
            "started_total": 0,
            "by_outcome": {name: 0 for name in outcomes},
            "duration_ms_sum": 0.0,
            "duration_ms_max": 0.0,
            "callers_awaiting": 0,
            "callers_awaiting_max": 0,
        }
        if axis in WORKER_AXES:
            block.update({
                "worker_started_total": 0,
                "worker_finished_total": 0,
                "worker_in_flight_max": 0,
            })
        axes[axis] = block
    axes["snapshot_send"] = {
        "calls_by_channel": {name: 0 for name in CHANNELS},
        "topics_deduped_total": 0,
        "sends_by_outcome": {name: 0 for name in SEND_OUTCOMES},
    }
    axes["metrics_internal_errors_total"] = 0
    return axes


_metrics = _blank()


class SubscribeLoadContractError(RuntimeError):
    """계약 위반 — 호출부 배선 실수(축 이름 오타 등)를 조용히 삼키지 않는다."""


def _require_axis(axis: str) -> None:
    if axis not in AXIS_OUTCOMES:
        raise SubscribeLoadContractError(f"알 수 없는 축: {axis!r}")


def _warn(message: str, *, axis: str, exc_info=None) -> None:
    """⛔ **진단 방출도 no-throw 다.** 로깅 실패가 서비스 결과를 결정하면 그건 관측이 아니라
    정책이다(실측: handler 가 raise 하면 본문이 실행되지 않거나 본문 예외가 대체됐다)."""
    try:
        logger.warning(message, extra={"axis": axis}, exc_info=exc_info)
    except Exception:  # noqa: BLE001 — 진단 실패로 본문을 흔들지 않는다
        pass


def _note_internal_error() -> None:
    """자기 부기 실패는 **조용히 degrade 하지 않는다** — 같은 문서 안에 남긴다.

    ⚠️ 호출자는 lock 을 잡지 않은 상태여야 한다(WARNING 은 lock 밖에서 낸다).
    """
    try:
        with _lock:
            _metrics["metrics_internal_errors_total"] += 1
    except Exception:  # noqa: BLE001 — best-effort
        pass


class WorkHandle:
    """한 관측의 상태. 후보 저장 + worker-start 관측 플래그만 갖는다."""

    __slots__ = ("_axis", "_candidate", "_has_candidate", "_closed", "_worker_started",
                 "_worker_started_fallback", "_duplicate")

    def __init__(self, axis: str) -> None:
        self._axis = axis
        self._candidate: Optional[str] = None
        self._has_candidate = False
        self._closed = False
        # ⛔ `threading.Event` — worker 스레드가 set 하고 event loop 가 읽는다.
        #    ⚠️ **생성도 계측 동작**이다 — 보호 없이 두면 실패가 그대로 전파돼 본문이 0회
        #    실행된다(실측). 실패하면 bool fallback 으로 격하한다(GIL 하에서 set/read 는 원자적).
        self._worker_started_fallback = False
        try:
            self._worker_started = threading.Event()
        except Exception:  # noqa: BLE001
            self._worker_started = None
            # ⛔ 격하해도 **조용히** 하지 않는다 — 가용한 채널에 둘 다 남긴다(채널 자체가
            #    고장이면 유실될 수 있고, 그건 서비스 결과를 바꾸지 않는다).
            _note_internal_error()
            _warn("subscribe-load: worker-start Event 생성 실패 — bool fallback 으로 격하",
                  axis=axis, exc_info=sys.exc_info())
        self._duplicate = False

    @property
    def axis(self) -> str:
        return self._axis

    def finish(self, outcome: str) -> None:
        """terminal outcome **후보**를 기록한다 — 여기서 카운터를 올리지 않는다.

        중복 호출은 **첫 후보를 유지**하고 internal error 만 센다. context 종료 뒤의 늦은
        호출(CLOSED)도 재전이 없이 internal error 만 올린다.
        """
        if self._closed or self._has_candidate:
            self._duplicate = True
            _note_internal_error()
            _warn("subscribe-load: terminal outcome 이 두 번 확정됐다 — 첫 후보를 유지한다",
                  axis=self._axis)
            return
        self._candidate = outcome
        self._has_candidate = True

    def mark_worker_started(self) -> None:
        if self._worker_started is None:
            self._worker_started_fallback = True
            return
        self._worker_started.set()

    @property
    def worker_start_observed(self) -> bool:
        if self._worker_started is None:
            return self._worker_started_fallback
        return self._worker_started.is_set()


def _resolve_outcome(handle: WorkHandle, exc: Optional[BaseException]) -> tuple[str, bool]:
    """terminal outcome 을 하나로 정한다. 반환: (outcome, internal_error 여부).

    ⛔ 예외·취소는 **후보 유무와 무관하게 우선**한다 — 후보를 남기고도 예외로 빠져나갈 수 있다.
    """
    axis = handle.axis
    if isinstance(exc, asyncio.CancelledError):
        # ⛔ **`CancelledError` 만** 취소다. `not isinstance(exc, Exception)` 로 넓히면
        #    `SystemExit`·`KeyboardInterrupt`·`GeneratorExit` 까지 취소로 기록된다(실측).
        if axis in WORKER_AXES:
            return (_CANCEL_OBSERVED if handle.worker_start_observed else _CANCEL_NOT_OBSERVED), False
        return "cancelled", False
    if exc is not None:
        # 취소가 아닌 모든 종료(=`Exception` 과 그 밖의 `BaseException`)는 결함 축이다.
        return _RAISED_OUTCOME[axis], False
    if not handle._has_candidate:
        return _UNCLASSIFIED, True
    candidate = handle._candidate
    if not isinstance(candidate, str) or candidate not in AXIS_OUTCOMES[axis]:
        # ⛔ 비문자열·unhashable·allowlist 밖 — 동적 key 도 `TypeError` 도 만들지 않는다.
        return _UNCLASSIFIED, True
    return candidate, False


@contextlib.asynccontextmanager
async def observe(axis: str):
    """한 외부축 호출을 관측한다. **본문 예외를 절대 삼키지 않는다.**

    진입 전이(`started_total` +1 · `callers_awaiting` +1 · max 갱신)와 종료 전이(outcome 1회 ·
    duration 1회 · `callers_awaiting` −1)는 각각 **한 lock 임계구역**에서 일어난다 — 그래야
    스냅샷이 `started == sum(by_outcome) + callers_awaiting` 를 만족한다.
    """
    _require_axis(axis)
    handle = WorkHandle(axis)
    # ⛔ **진입 부기도 본문을 막지 않는다.** 예전에는 첫 `time.monotonic()` 이나 진입 lock 이
    #    실패하면 본문이 **한 번도 실행되지 않고** 그 예외가 그대로 나갔다(실측). 계측이
    #    서비스 경로를 결정하면 그건 관측이 아니라 정책이다.
    # ⚠️ 진입이 실패하면 이 관측은 장부에 **아예 없다** — 그래서 종료 전이도 건너뛴다
    #    (건너뛰지 않으면 `callers_awaiting` 이 음수가 된다).
    started: Optional[float] = None
    entry_failure: Optional[tuple] = None
    try:
        started = time.monotonic()
        with _lock:
            block = _metrics[axis]
            block["started_total"] += 1
            block["callers_awaiting"] += 1
            if block["callers_awaiting"] > block["callers_awaiting_max"]:
                block["callers_awaiting_max"] = block["callers_awaiting"]
    except Exception:  # noqa: BLE001 — 계측 실패는 삼키고 본문을 진행시킨다
        entry_failure = sys.exc_info()
        started = None
        try:
            with _lock:
                _metrics["metrics_internal_errors_total"] += 1
        except Exception:  # noqa: BLE001 — best-effort
            pass
    if entry_failure is not None:
        # ⚠️ lock 밖에서, **잡은 예외**를 명시적으로 싣는다.
        _warn("subscribe-load: 진입 부기 실패 — 이 관측은 기록되지 않는다",
              axis=axis, exc_info=entry_failure)
    exc: Optional[BaseException] = None
    try:
        yield handle
    except BaseException as raised:  # noqa: BLE001 — 재전파한다(아래 raise)
        exc = raised
        raise
    finally:
        # ⛔ **부기 실패가 본문 결과를 덮지 않는다.** `_resolve_outcome` 이나 카운터 갱신이
        #    예외를 내면 그게 본문 예외를 **대체**했다(실측). 계측은 관측이지 정책이 아니다.
        # ⛔ 실패해도 **전이를 건너뛰지 않는다** — 건너뛰면 `callers_awaiting` 이 영구 누수돼
        #    이후 모든 캡처의 게이지가 틀어진다(실측).
        # ⛔ **duration 계산도 보호 구역 안**이다 — 종료 clock 이 실패하면 같은 두 사고가
        #    한꺼번에 났다(실측: 전파 예외가 바뀌고 게이지가 누수됐다).
        duration_ms = 0.0
        internal_error = False
        degraded: Optional[tuple] = None
        # ⛔ **`finally` 안에서 `return` 하지 않는다** — 대기 중인 본문 예외·취소를 통째로
        #    삼킨다(실측: 진입 실패 + 본문 `ValueError` 조합이 예외 없이 `None` 을 반환했다).
        #    진입이 실패했으면 균형 맞출 장부가 없으므로 **조건부로 건너뛴다**.
        handle._closed = True
        if started is not None:
          try:
              duration_ms = (time.monotonic() - started) * 1000.0
              outcome, internal_error = _resolve_outcome(handle, exc)
          except Exception:  # noqa: BLE001 — 분류·계시 실패는 unclassified 로 격하한다
              outcome, internal_error, degraded = _UNCLASSIFIED, True, sys.exc_info()
          handle._closed = True
          transition_failure: Optional[tuple] = None
          try:
              with _lock:
                  block = _metrics[axis]
                  block["by_outcome"][outcome] += 1
                  block["duration_ms_sum"] += duration_ms
                  if duration_ms > block["duration_ms_max"]:
                      block["duration_ms_max"] = duration_ms
                  block["callers_awaiting"] -= 1
                  if internal_error:
                      _metrics["metrics_internal_errors_total"] += 1
          except Exception:  # noqa: BLE001 — 여기까지 실패하면 **half-open** 으로 남는다
              # ⚠️ "장부에서 사라진다" 가 아니다 — 진입 기록이 이미 있어 **half-open/부분 전이**
              #    로 남는다. lock **획득**이 실패하면 `started=1 / awaiting=1 / terminal 0` 이고,
              #    임계구역 **안**에서 깨지면 일부 필드만 먼저 바뀔 수 있다. 어느 쪽이든 공개
              #    reset 이 거부한다.
              transition_failure = sys.exc_info()
              # ⚠️ counter 도 **가용하면** 올린다 — 이 경로만 WARNING 만 내고 있었다(실측).
              _note_internal_error()
          # ⚠️ WARNING 은 **lock 밖**에서 낸다. `exc_info=True` 로 두면 본문 예외가 찍혀
          #    **정작 잡은 계측 예외가 사라진다**(실측) — 잡은 것을 명시적으로 넘긴다.
          if transition_failure is not None:
              _warn("subscribe-load: terminal 전이 실패 (본문 결과는 보존)",
                    axis=axis, exc_info=transition_failure)
          elif degraded is not None:
              _warn("subscribe-load: outcome 분류·계시 실패 — unclassified 로 기록",
                    axis=axis, exc_info=degraded)
          elif internal_error:
              _warn("subscribe-load: terminal outcome 이 확정되지 않았다 — unclassified 로 기록",
                    axis=axis)


def timed_call(axis: str, handle: WorkHandle, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """worker 스레드 안에서 `fn` 을 부르며 worker 축을 센다.

    ⛔ 호출 형태는 **`await asyncio.to_thread(timed_call, AXIS, handle, fn, *args, **kwargs)`** 다.
       loop 스레드에서 직접 부르면 DB 쿼리·build 가 loop 를 블로킹하고 to_thread 격리도 사라진다.
    ⛔ `fn` 앞이 **positional-only** 인 이유: 대상 함수가 `axis`/`handle`/`fn` 이라는 이름의
       키워드를 쓸 수 있어야 한다. 열어 두면 wrapper 의 동명 파라미터와 충돌해
       `TypeError: multiple values for argument` 가 난다(`app/auth_executor.py:188-194` 동류).
    """
    _require_axis(axis)
    if axis not in WORKER_AXES:
        raise SubscribeLoadContractError(f"{axis} 는 worker 축이 없다 — to_thread 경로가 아니다")
    if handle.axis != axis:
        # ⛔ 배선 오타를 조용히 통과시키면 worker counter 는 A 축에, worker-start 관측은 B 축
        #    handle 에 남아 **두 축이 갈린다**(실측).
        raise SubscribeLoadContractError(
            f"handle 의 축({handle.axis})과 인자 축({axis})이 다르다")
    # ⛔ 여기까지(축·결속 검증)는 **계약 위반**이라 예외가 맞다 — 배선 오타를 삼키면 안 된다.
    #    아래 부기부터는 **관측**이므로 실패해도 target 실행과 그 결과를 흔들지 않는다
    #    (실측: 진입 lock 실패 → target 0회, 종료 lock 실패 → 반환값이 metrics 예외로 대체).
    # ⛔ 이것도 **계측 동작**이다 — 보호 밖에 두니 실패가 그대로 전파되고 target 이 0회
    #    실행됐다(실측). `Event.set()` 이 보통 안 던진다는 건 계약이 아니라 구현 세부다.
    try:
        handle.mark_worker_started()
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn("subscribe-load: worker-start 관측 실패", axis=axis, exc_info=sys.exc_info())
    counted = False
    try:
        with _lock:
            block = _metrics[axis]
            block["worker_started_total"] += 1
            in_flight = block["worker_started_total"] - block["worker_finished_total"]
            if in_flight > block["worker_in_flight_max"]:
                block["worker_in_flight_max"] = in_flight
        counted = True
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn("subscribe-load: worker 진입 부기 실패", axis=axis, exc_info=sys.exc_info())
    try:
        return fn(*args, **kwargs)
    finally:
        # ⚠️ 진입을 못 셌으면 종료도 세지 않는다 — `worker_in_flight` 가 음수가 된다.
        if counted:
            try:
                with _lock:
                    _metrics[axis]["worker_finished_total"] += 1
            except Exception:  # noqa: BLE001
                _note_internal_error()
                _warn("subscribe-load: worker 종료 부기 실패", axis=axis, exc_info=sys.exc_info())


def record_snapshot_call(channel: str) -> None:
    """`send_initial_snapshots` 진입 1회. 미지정 channel 은 `unattributed` 로 접는다."""
    try:
        key = channel if channel in CHANNELS else "unattributed"
        with _lock:
            _metrics["snapshot_send"]["calls_by_channel"][key] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: snapshot 기록 실패", axis="snapshot_send", exc_info=sys.exc_info())


def record_snapshot_topic() -> None:
    """요청 내 **중복 제거 뒤** topic 1건 — 요청 1건이 몇 배로 퍼지는지의 분자."""
    try:
        with _lock:
            _metrics["snapshot_send"]["topics_deduped_total"] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: snapshot 기록 실패", axis="snapshot_send", exc_info=sys.exc_info())


def record_snapshot_send(outcome: str) -> None:
    try:
        key = outcome if outcome in SEND_OUTCOMES else "raised"
        with _lock:
            _metrics["snapshot_send"]["sends_by_outcome"][key] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: snapshot 기록 실패", axis="snapshot_send", exc_info=sys.exc_info())


def subscribe_load_metrics() -> dict[str, Any]:
    """읽기 전용 스냅샷 — **전체를 하나의 lock 안에서** 뜬다.

    ⚠️ 이 블록 안에서는 일관되지만, endpoint 가 executor·rollout 블록과 합쳐 보내는 **body 전체는
       여러 lock 의 순차 snapshot** 이라 원자적이지 않다.
    """
    with _lock:
        out: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "scope": "process",
            "caveat": CAVEAT,
            "metrics_internal_errors_total": _metrics["metrics_internal_errors_total"],
        }
        for axis in AXIS_OUTCOMES:
            block = _metrics[axis]
            snap = {
                "started_total": block["started_total"],
                "by_outcome": dict(block["by_outcome"]),
                "duration_ms_sum": round(block["duration_ms_sum"], 3),
                "duration_ms_max": round(block["duration_ms_max"], 3),
                "callers_awaiting": block["callers_awaiting"],
                "callers_awaiting_max": block["callers_awaiting_max"],
            }
            if axis in WORKER_AXES:
                snap["worker_started_total"] = block["worker_started_total"]
                snap["worker_finished_total"] = block["worker_finished_total"]
                # ⛔ 파생 가능한 것은 저장하지 않는다 — 두 번째 진실을 만들지 않기 위해.
                snap["worker_in_flight"] = block["worker_started_total"] - block["worker_finished_total"]
                snap["worker_in_flight_max"] = block["worker_in_flight_max"]
            out[axis] = snap
        send = _metrics["snapshot_send"]
        out["snapshot_send"] = {
            "calls_by_channel": dict(send["calls_by_channel"]),
            "topics_deduped_total": send["topics_deduped_total"],
            "sends_by_outcome": dict(send["sends_by_outcome"]),
        }
    return out


def reset_subscribe_load_metrics() -> None:
    """테스트·진단 전용. 운영 route 없음.

    ⛔ **정지 상태에서만** 허용한다 — 진행 중에 0 으로 밀면 이후 종료 전이에서 게이지가 음수가
       된다. 단 게이지 검사는 **필요조건일 뿐 충분조건이 아니다**: caller 취소 → worker-start
       이벤트 미set → 게이지 0 → reset 허용 → **이미 running 이던 wrapper 가 뒤늦게 시작** 하는
       경합이 남는다(취소 키를 관측 사실로 격하한 바로 그 이유). 따라서 **외부 전제**가 있다 —
       호출자가 관련 coroutine 과 default-executor 작업을 **전부 drain·join** 했어야 한다.
    ⛔ executor 의 reset 과 **묶지 않는다**(묶으면 코드로 분리한 두 모듈이 다시 결합된다).
    ⛔ **clear-only 금지** — 같은 lock 안에서 즉시 고정 키를 다시 심는다(아래 구현).
    """
    with _lock:
        busy = [
            axis for axis in AXIS_OUTCOMES
            if _metrics[axis]["callers_awaiting"] != 0
            or (axis in WORKER_AXES
                and _metrics[axis]["worker_started_total"] != _metrics[axis]["worker_finished_total"])
        ]
        if busy:
            raise SubscribeLoadContractError(
                f"진행 중인 관측이 있어 reset 을 거부한다: {sorted(busy)}"
            )
        _metrics.clear()
        _metrics.update(_blank())
