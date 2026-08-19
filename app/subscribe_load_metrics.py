# app/subscribe_load_metrics.py
"""subscribe-load 계측 — WS subscribe 경로의 외부축 관측 (process-local).

⛔ **이름이 계약이다.** 이것은 *subscribe-load* 이지 *reconnect-load* 가 아니다 —
   `send_initial_snapshots` 도 `handle_client_message` 도 연결 이력을 인자로 받지 않으므로,
   재연결 / 최초 연결 / 일반 재구독을 **구분할 수 없다**. "재연결" 귀속은 통제된 시나리오의
   전후 delta 로만 주장한다.

⛔ **leaf 가 아니라 WS 전용 seam 에 붙인다.** `app/subscription.py` 의
   `fetch_revenuecat_result` 는 WS(`topic_authorization.py`)와 REST(`subscription.py` 의
   `_check_revenuecat_entitlement`)가 공유한다 — 거기 계측하면 REST 왕복이 섞인다.

## caller 축 ⊥ worker 축

`asyncio.to_thread` 는 **취소를 전파하지 않는다**(`app/topic_dispatcher.py:585-587` 이 명시).
caller 쪽만 재면 양방향으로 어긋난다 — 포기 뒤에도 도는 스레드는 과소, 큐에서 취소돼 실행조차
안 된 건은 과대. 그래서 두 축을 나눈다:
  - caller 축: `callers_awaiting` (점유가 아니라 **대기**), `duration_ms_*`
  - worker 축: `worker_started_total` / `worker_finished_total` / `worker_in_flight`
⚠️ **단순 차이는 잔여가 아니다** — 대기 중이지만 worker 가 아직 시작하지 않은 caller 도 그
차이에 들어가므로 대략 `버려진 채 도는 worker − 큐에서 대기 중인 caller` 다.
`callers_awaiting == 0` 인 정지 상태 캡처에서만 `worker_in_flight` 를 잔여 worker 로 읽을 수 있다.

⚠️ `premium_rc` 에는 worker 축이 **없다**(비대칭은 의도) — async HTTP 라 취소가 실제 전파된다.
   항상 0 인 필드를 만들면 "정상인데 0" 과 "고장나서 0" 이 섞인다.

## 캡처 판독 경계 (S2 배선 후 확정 — attribution probe 실측)

- `premium_rc.started_total` 은 **RC 실호출 수가 아니라 CM 진입 기준 관측 시도 수**다 —
  mono()/system_clock() 같은 clock 배선 결함도 RC 0회 상태로 `raised` 에 포함된다.
- 취소는 terminal 후보보다 **우선**한다 — worker 가 결과를 만들어도 caller 취소면 `granted`
  로 세지 않으므로 **granted 는 DB-allowed 응답 수를 undercount** 한다. deadline 만료와
  caller 포기는 counter 만으로 구분할 수 없다(통제 시나리오 delta 로만 주장).
- (S3) snapshot 루프의 **중단 편향은 connection_closed 만이 아니다** — `raised`·취소도
  루프를 중단시켜 잔여 topic 이 세 축(deduped/build/send) 모두에서 미관측된다. `raised>0`
  인 창도 하향 편향으로 읽는다.
- (S3) `built − Σ(sends_by_outcome)` 는 **send outcome 이 남지 않은 post-build 종료
  잔차**다(취소뿐 아니라 BaseException 류·기록 실패도 포함, 계측 내부 오류 조합에선 음수도
  가능) — **취소 건수로 단정할 수 없다**. 취소 귀속은 통제 시나리오에서만 한다.
- (LOAD-S5) `snapshot_build.started_total`은 **실제 shared build 수**다. 요청 수는
  `snapshot_singleflight.requests_total`, 합쳐진 수는 `joined_total`, 완료 cache 재사용은
  `cache_hits_total`이 소유한다. 따라서 `topics_deduped_total == snapshot_build.started_total`
  등식은 더 이상 성립하지 않는다.
- (S4) deadline stage 는 **만료가 관측된 단계**이지 예산을 소비한 단계가 아니다 — 공유 절대
  deadline 이라 identity 가 예산 대부분을 쓰고 성공하면 만료는 authorization 에 계상된다
  (suspension 없는 동기 소진은 관측 자체가 불가). 실무상 identity 소비는 대부분 await I/O 라
  근사가 성립하지만, 귀속 단정은 통제 시나리오에서만 한다.
- (S4) `subscribe_auth_failed_by_error` 는 **SubscribeAuthFailed 경유만** 계측한다 —
  dispatcher 가 직접 보내는 invalid_request 프레임(파싱·형식 오류 4곳)과 emitter 없는 예약
  코드(request_too_large)는 여기 잡히지 않는다. **두 버킷의 0 은 그런 종결의 부재를 의미하지
  않는다**(key 는 어휘 완결성 유지 — 미래 경유 코드 대비).

⛔ **취소 하위분류는 관측 사실일 뿐이다.** `ThreadPoolExecutor` 는 wrapper 호출 **전에** future
   를 running 으로 바꾸므로 worker-start 이벤트 set 이전에 경합 창이 있고, `asyncio.to_thread`
   는 그 underlying future 를 노출하지 않는다(`app/auth_executor.py:196-245` 가 done-callback
   `future.cancelled()` 로 이 문제를 푼 이유). 따라서 두 취소 키 중 **어느 쪽도 "DB 세션 0" 을
   함의하지 않는다** — 실제 작업량의 진실은 worker 축이 말한다.

## exactly-once 를 규율이 아니라 구조로

`finish(outcome)` 은 **후보를 저장만** 하고, 실제 terminal 전이는 `__aexit__` 가 정확히 한 번
수행한다. `finish()` 뒤에도 scope 안에서 예외가 날 수 있어 "finish 가 마지막" 이라는 규율은
깨지기 쉽다.

## 배선 상태 (S2~)

`app/topic_authorization.py` 의 premium/krx seam 이 이 모듈을 소비한다(S2). **wire 의
verdict·payload·전파 예외 계약은 불변**이다 — task/thread/env flag/외부 I/O 0, in-memory
int·float 갱신만. ⚠️ 단 **"동작 변화 0" 이라고 쓰면 거짓**이다: event-loop caller 와 worker 가
같은 `threading.Lock` 을 잡으므로 loop 스레드에 lock 획득이 생기고 계측 상태가 갱신된다.
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
CONTRACT_VERSION = "subscribe-load/9"

PREMIUM_RC = "premium_rc"
KRX_ENTITLEMENT = "krx_entitlement"
SNAPSHOT_BUILD = "snapshot_build"

# ⛔ 고정 cardinality — 요청 문자열이 key 를 늘릴 수 없다. allowlist 밖 outcome 은 동적 key 를
#    만들지 않고 `unclassified` 로 접는다(`app/topic_auth_rollout.py` 의 규율과 동형).
_UNCLASSIFIED = "unclassified"
_CANCEL_NOT_OBSERVED = "cancel_worker_start_not_observed"
_CANCEL_OBSERVED = "cancel_worker_start_observed"

# ⛔ **저장 스키마 ≠ 제출 allowlist.** 아래 `AXIS_OUTCOMES` 는 **저장** key 집합이다. 취소 2종·
#    raised·build_failed 는 `__aexit__` 의 실제 예외·취소 종료에서만 파생되고, `unclassified` 는
#    terminal resolution 이 생성한다(outcome 미지정·제출 불가 값·분류 격하 — 정상 종료도 만든다).
#    어느 쪽도 제출로는 만들 수 없다. 한 집합이 두 역할을 겸하면
#    정상 종료 본문의 `finish("cancelled")` 가 진짜 취소로 기록되고, 정확한 sentinel 문자열이
#    fold 진단을 우회했다(실측: 파생 5종 제출 전부 해당 버킷 기록 + internal error 0).
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

# 호출자가 `finish()` 로 제출할 수 있는 **도메인 결과**만. 파생 키(취소 2종·raised·build_failed·
# unclassified)는 여기 없다. snapshot 의 `build_failed` 는 `send_initial_snapshots` 의 관측 CM
# 안에서 실제 builder 예외가 전파될 때만 생긴다. `SubscribeLoadContractError` 는 외부축 실패가
# 아니므로 `unclassified` + 내부 진단으로 기록한 뒤 그대로 재전파한다(설계 §5 파생 매핑).
SUBMITTABLE_OUTCOMES: dict[str, frozenset[str]] = {
    PREMIUM_RC: frozenset({"granted", "denied", "unavailable_transient", "unavailable_persistent"}),
    KRX_ENTITLEMENT: frozenset({"granted", "denied", "unavailable_transient", "unavailable_persistent"}),
    SNAPSHOT_BUILD: frozenset({"built", "none_payload"}),
}

# worker 축을 갖는 축 — `to_thread` 로 도는 두 개만.
WORKER_AXES: frozenset[str] = frozenset({KRX_ENTITLEMENT, SNAPSHOT_BUILD})

# 예외 종료 시 쓰는 축별 outcome. snapshot 은 build 실패가 곧 도메인 결과라 이름이 다르다.
_RAISED_OUTCOME: dict[str, str] = {
    PREMIUM_RC: "raised",
    KRX_ENTITLEMENT: "raised",
    SNAPSHOT_BUILD: "build_failed",
}

# ⛔ 아래 두 tuple 은 **제출** allowlist 다. allowlist 밖 값을 의미 버킷으로 접으면 그 버킷이
#    오염된다(실측: typo → `raised`=1 은 실제 전송 예외 신호를, typo → `unattributed`=1 은
#    channel 배선 gap 신호를 각각 오염시켰고 진단은 0이었다). `unclassified` 는 **storage-only** —
#    제출에 넣으면 정확한 sentinel 문자열이 fold 진단을 우회한다(실측). send 의 `raised` 는
#    배선이 send 예외를 잡아 **분류·제출**하는 값이라 제출 allowlist 에 남는다(설계 §4).
CHANNELS: tuple[str, ...] = ("anonymous", "token_bearing", "unattributed")
SEND_OUTCOMES: tuple[str, ...] = ("sent", "lease_skipped", "connection_closed", "raised")
CHANNEL_KEYS: tuple[str, ...] = CHANNELS + (_UNCLASSIFIED,)
SEND_OUTCOME_KEYS: tuple[str, ...] = SEND_OUTCOMES + (_UNCLASSIFIED,)

# ── terminal 축 (S4) — dispatcher 의 요청-종결 counter 2종 ────────────────────
# ⛔ deadline stage 는 **우리 리터럴**이라 allowlist 밖 = 배선 오류 → `unclassified` + 진단
#    (channel 과 같은 부류). 반면 auth-fail 의 error 코드는 **런타임 관측값**이고 topic_wire 가
#    진화하면 새 코드가 정당하게 나타난다 → `other` 로 접고 WARNING 1줄만(내부 오류 아님 —
#    `app/topic_auth_rollout.py` 의 other-fold 규율과 동형). 두 fold 는 의미가 다르다.
DEADLINE_STAGES: tuple[str, ...] = ("identity", "authorization")
DEADLINE_STAGE_KEYS: tuple[str, ...] = DEADLINE_STAGES + (_UNCLASSIFIED,)
# ⚠️ topic_wire.WHOLE_REQUEST_ERRORS 의 사본 — 이 모듈은 stdlib 만 import 한다(순환·결합 회피).
#    drift 는 테스트가 topic_wire 원본과 대조해 잠근다.
AUTH_FAIL_CODES: tuple[str, ...] = (
    "invalid_token", "temporarily_unavailable", "invalid_request", "request_too_large",
)
AUTH_FAIL_KEYS: tuple[str, ...] = AUTH_FAIL_CODES + ("other",)
SNAPSHOT_FAILURE_CLASSES: tuple[str, ...] = ("deadline", "transient_db", "redis")
SNAPSHOT_FAILURE_CLASS_KEYS: tuple[str, ...] = SNAPSHOT_FAILURE_CLASSES + (_UNCLASSIFIED,)

CAVEAT = (
    "subscribe-load 이며 reconnect 귀속이 아니다 · max/gauge 는 두 캡처 사이에 빼지 말 것 · "
    "프로세스 재기동 시 0 · "
    "**snapshot_build 는 WS 와 REST twin 의 합산**(같은 default executor·I/O 를 쓰므로 용량 산정에 합산이 필요) · "
    "snapshot_singleflight.waiters 는 shared task를 기다리는 caller 수(cache hit 제외) · "
    "snapshot_admission 은 새 shared flight task만 세며 기존 flight join·cache hit는 제외 · "
    "snapshot_failure_cooldown 은 key별 transient build 실패만 세며 auth·fatal·programming error는 제외 · "
    "admission wait 은 앱 FIFO slot 대기, snapshot_build.queue_wait 은 executor 제출→worker 시작 대기 · "
    "admission queued 는 시간만 S3 deadline으로 유계이고 개수 hard cap은 없음 · "
    "snapshot_send·terminal 은 WS 전용(REST 는 channel/wire 개념이 없다) · "
    "queue_wait 은 제출→worker 시작이며 **worker 시작 시점에** 기록한다(caller 반환 시점 기록은 취소된 caller 의 대기를 통째로 잃는다 — 폭주에서 가장 흔한 경로) · "
    "mark_submitted 미호출이면 queue_wait 미관측(count 에 안 들어간다) · "
    "callers_awaiting 은 대기이지 점유가 아니다 · "
    "started_total 은 관측 시도 수(leaf 실호출 수 아님) · 취소 우선이라 granted 는 undercount · "
    "snapshot 루프 중단(connection_closed·raised·취소)은 잔여 topic 을 전 축 미관측으로 남긴다 · "
    "built−Σ(sends) 는 post-build 종료 잔차(취소 단정 금지) · "
    "deadline stage 는 만료-관측 단계(예산-소비 단계 아님) · auth_failed 는 SubscribeAuthFailed 경유만"
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
                # ⛔ 제출→worker 시작. **worker 시작 시점에** 기록한다 — caller 반환 시점에
                #    기록하면 폭주 중 취소된 caller 의 대기가 통째로 유실된다(취소는 폭주의
                #    기본 경로다). `auth_executor` 의 `dequeued` 와 같은 자리다.
                "queue_wait_ms_sum": 0.0,
                "queue_wait_ms_max": 0.0,
                "queue_wait_observed_total": 0,
                # worker 본문 시작→종료. caller duration이나 executor queue wait와 섞지 않는다.
                "execution_ms_sum": 0.0,
                "execution_ms_max": 0.0,
                "execution_observed_total": 0,
            })
        axes[axis] = block
    axes["snapshot_send"] = {
        "calls_by_channel": {name: 0 for name in CHANNEL_KEYS},
        "topics_deduped_total": 0,
        "sends_by_outcome": {name: 0 for name in SEND_OUTCOME_KEYS},
    }
    axes["snapshot_singleflight"] = {
        "requests_total": 0,
        "leaders_total": 0,
        "joined_total": 0,
        "cache_hits_total": 0,
        "successes_cached_total": 0,
        "waiters_now": 0,
        "waiters_max": 0,
    }
    axes["snapshot_admission"] = {
        "queued_now": 0,
        "queued_max": 0,
        "wait_observed_total": 0,
        "wait_ms_sum": 0.0,
        "wait_ms_max": 0.0,
        "in_flight": 0,
        "in_flight_max": 0,
    }
    axes["snapshot_failure_cooldown"] = {
        "armed_total": 0,
        "suppressed_total": 0,
        "expired_total": 0,
        "by_failure_class": {
            name: 0 for name in SNAPSHOT_FAILURE_CLASS_KEYS
        },
        "last_suppression": None,
    }
    axes["terminal"] = {
        "auth_wire_deadline_expired_by_stage": {name: 0 for name in DEADLINE_STAGE_KEYS},
        "subscribe_auth_failed_by_error": {name: 0 for name in AUTH_FAIL_KEYS},
    }
    axes["metrics_internal_errors_total"] = 0
    return axes


_metrics = _blank()


class SubscribeLoadContractError(RuntimeError):
    """계약 위반 — 호출부 배선 실수(축 이름 오타 등)를 조용히 삼키지 않는다."""


def _validate_schema() -> None:
    """저장 스키마 ↔ 제출 allowlist ↔ 파생 키의 정합을 import 시 1회 강제한다 (Crash Early).

    ⛔ SUBMITTABLE 에 있는데 storage 에 없는 키가 생기면: 축 경로는 종료 전이 `KeyError` 로
       **half-open + `callers_awaiting` 호출당 영구 누수**가 되고(불변식 `1==0+1` 은 유지라
       corrupt 검사가 못 잡는다), send 경로는 관측이 통째로 소실된다(실측 probe). 편집 실수는
       런타임 관측 손상이 아니라 import 실패로 잡는다 — 배선 전 모듈이라 안전하고, 배선 후엔
       테스트·기동이 즉시 빨갛게 된다.
    """
    # ⛔ 검사는 **최소 완전** 집합이다 — "제출 ⊆ 저장" 류 부분 검사는 아래 partition 검사에
    #    논리적으로 포섭돼(⊄ 이면 합집합 ≠ 저장) 등가 변이만 만든다(실측: 제거해도 전 스위트
    #    green). 저장 = 제출 ⊎ 파생 (서로소 합) — 이 두 검사가 그 자체다.
    if set(SUBMITTABLE_OUTCOMES) != set(AXIS_OUTCOMES) or set(_RAISED_OUTCOME) != set(AXIS_OUTCOMES):
        raise SubscribeLoadContractError("축 집합이 세 map 에서 일치하지 않는다")
    for axis, outcomes in AXIS_OUTCOMES.items():
        submittable = SUBMITTABLE_OUTCOMES[axis]
        derived = {_UNCLASSIFIED, _RAISED_OUTCOME[axis]}
        derived |= ({_CANCEL_NOT_OBSERVED, _CANCEL_OBSERVED} if axis in WORKER_AXES else {"cancelled"})
        if submittable & derived:
            raise SubscribeLoadContractError(f"{axis}: 제출과 파생이 겹친다 — 분리 계약 위반")
        if set(outcomes) != submittable | derived:
            raise SubscribeLoadContractError(f"{axis}: 저장 스키마가 제출 ⊎ 파생과 다르다")
    if _UNCLASSIFIED in CHANNELS or set(CHANNEL_KEYS) != set(CHANNELS) | {_UNCLASSIFIED}:
        raise SubscribeLoadContractError("snapshot_send: channel 저장 key 가 제출 ⊎ unclassified 와 다르다")
    if _UNCLASSIFIED in SEND_OUTCOMES or set(SEND_OUTCOME_KEYS) != set(SEND_OUTCOMES) | {_UNCLASSIFIED}:
        raise SubscribeLoadContractError("snapshot_send: send 저장 key 가 제출 ⊎ unclassified 와 다르다")
    if _UNCLASSIFIED in DEADLINE_STAGES or set(DEADLINE_STAGE_KEYS) != set(DEADLINE_STAGES) | {_UNCLASSIFIED}:
        raise SubscribeLoadContractError("terminal: stage 저장 key 가 제출 ⊎ unclassified 와 다르다")
    if "other" in AUTH_FAIL_CODES or set(AUTH_FAIL_KEYS) != set(AUTH_FAIL_CODES) | {"other"}:
        raise SubscribeLoadContractError("terminal: auth-fail 저장 key 가 알려진 코드 ⊎ other 와 다르다")
    if _UNCLASSIFIED in SNAPSHOT_FAILURE_CLASSES or set(SNAPSHOT_FAILURE_CLASS_KEYS) != set(SNAPSHOT_FAILURE_CLASSES) | {_UNCLASSIFIED}:
        raise SubscribeLoadContractError("snapshot cooldown: failure class 저장 key 가 제출 ⊎ unclassified 와 다르다")


_validate_schema()


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
                 "_worker_started_fallback", "_duplicate", "_submitted_at")

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
        # ⛔ caller(loop) 가 **to_thread 직전에** 찍는다. 여기서 찍으면 CM 진입~제출 사이의
        #    caller 작업이 queue_wait 에 섞인다 — 그건 큐 대기가 아니다.
        self._submitted_at: Optional[float] = None

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

    def mark_submitted(self) -> None:
        """caller 스레드에서 제출 직전 1회. ⛔ 계측이므로 실패해도 본문을 흔들지 않는다."""
        try:
            self._submitted_at = time.monotonic()
        except Exception:  # noqa: BLE001
            _note_internal_error()
            _warn("subscribe-load: 제출 시각 기록 실패", axis=self._axis, exc_info=sys.exc_info())

    @property
    def submitted_at(self) -> Optional[float]:
        return self._submitted_at

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
    if isinstance(exc, SubscribeLoadContractError):
        # 계측 배선 오류는 외부축 실패가 아니다. 호출자에게는 그대로 전파하되 장부에서는
        # `raised`/`build_failed`를 오염시키지 않고 내부 진단과 짝지어 남긴다.
        return _UNCLASSIFIED, True
    if exc is not None:
        # 취소가 아닌 모든 종료(=`Exception` 과 그 밖의 `BaseException`)는 결함 축이다.
        return _RAISED_OUTCOME[axis], False
    if not handle._has_candidate:
        return _UNCLASSIFIED, True
    candidate = handle._candidate
    if not isinstance(candidate, str) or candidate not in SUBMITTABLE_OUTCOMES[axis]:
        # ⛔ 비문자열·unhashable·**제출 allowlist** 밖 — 동적 key 도 `TypeError` 도 만들지 않는다.
        #    저장 스키마(AXIS_OUTCOMES)로 검사하면 정상 종료 본문의 `finish("cancelled")` 가
        #    진짜 취소로 기록된다(실측) — 파생 키는 실제 예외에서만 생긴다.
        return _UNCLASSIFIED, True
    return candidate, False


@contextlib.asynccontextmanager
async def observe(axis: str):
    """한 외부축 호출을 관측한다. **본문 예외를 절대 삼키지 않는다.**

    진입 전이(`started_total` +1 · `callers_awaiting` +1 · max 갱신)와 종료 전이(outcome 1회 ·
    duration 1회 · `callers_awaiting` −1)는 각각 **한 lock 임계구역**에서 일어난다 — 그래야
    스냅샷이 `started == sum(by_outcome) + callers_awaiting` 를 만족한다. 진입은 거기에 더해
    **예외-원자적**이다(복사본 계산 → 단일 대입 commit point) — 한 lock 은 동시성 원자성만 줄
    뿐이라, 두 번째 쓰기만 실패하면 `started=1 / awaiting=0` 부분 전이가 장부에 남았다(실측).
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
            # ⛔ **단일 대입이 commit point** 다. in-place 로 세 번 쓰면 중간 실패가 부분 전이
            #    (`started=1 / awaiting=0 / terminal 0`)를 남기고, 그 상태는 게이지만 보는
            #    reset 이 정상으로 수용·삭제했다(실측). 복사본에 전부 계산한 뒤 한 번에 건다 —
            #    어디서 실패하든 장부는 전무 아니면 전부다.
            old_block = _metrics[axis]
            new_block = dict(old_block)  # 얕은 복사 — `by_outcome` 은 진입이 안 건드린다
            new_block["started_total"] = old_block["started_total"] + 1
            awaiting = old_block["callers_awaiting"] + 1
            new_block["callers_awaiting"] = awaiting
            if awaiting > old_block["callers_awaiting_max"]:
                new_block["callers_awaiting_max"] = awaiting
            _metrics[axis] = new_block
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
                  # ⛔ unclassified fold 진단을 terminal 관측보다 먼저 쓴다. 반대
                  #    순서면 internal counter 쓰기가 실패했을 때
                  #    `unclassified=1 / internal_errors=0` 이 남고, awaiting 도 0이라
                  #    reset 이 그 거짓 pairing 을 정상 장부로 삭제했다(실측).
                  if internal_error:
                      _metrics["metrics_internal_errors_total"] += 1
                  block["by_outcome"][outcome] += 1
                  block["duration_ms_sum"] += duration_ms
                  if duration_ms > block["duration_ms_max"]:
                      block["duration_ms_max"] = duration_ms
                  block["callers_awaiting"] -= 1
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
              if isinstance(exc, SubscribeLoadContractError):
                  # ⚠️ 계약 오류는 "미지정/제출 불가" 가 아니다 — 문구를 나누고 원본 예외를
                  #    명시 전달한다(generic 문구는 오진이었다 — 실측).
                  _warn("subscribe-load: 계측 계약 오류 — unclassified 로 기록 후 재전파",
                        axis=axis, exc_info=exc)
              else:
                  _warn("subscribe-load: terminal outcome 이 없거나 제출 allowlist 밖 — unclassified 로 기록",
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
    # ⛔ queue_wait 은 **여기서** 잰다 — worker 가 막 시작한 지점이다. 값 계산을 lock 밖에서
    #    끝내 임계구역을 늘리지 않는다. 제출 시각이 없으면(=caller 가 mark_submitted 를 안 함)
    #    **관측하지 않는다** — 0 으로 채우면 "대기 없음"과 "미관측"이 구분되지 않는다.
    queue_wait_ms: Optional[float] = None
    try:
        submitted = handle.submitted_at
        if submitted is not None:
            queue_wait_ms = max(0.0, (time.monotonic() - submitted) * 1000.0)
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn("subscribe-load: queue_wait 계산 실패", axis=axis, exc_info=sys.exc_info())
    try:
        with _lock:
            block = _metrics[axis]
            block["worker_started_total"] += 1
            in_flight = block["worker_started_total"] - block["worker_finished_total"]
            if in_flight > block["worker_in_flight_max"]:
                block["worker_in_flight_max"] = in_flight
            if queue_wait_ms is not None:
                block["queue_wait_observed_total"] += 1
                block["queue_wait_ms_sum"] += queue_wait_ms
                if queue_wait_ms > block["queue_wait_ms_max"]:
                    block["queue_wait_ms_max"] = queue_wait_ms
        counted = True
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn("subscribe-load: worker 진입 부기 실패", axis=axis, exc_info=sys.exc_info())
    execution_started: Optional[float] = None
    try:
        execution_started = time.monotonic()
    except Exception:  # noqa: BLE001 — 실행시간 미관측이 target을 막지 않는다
        _note_internal_error()
        _warn("subscribe-load: worker 실행 시작 시각 기록 실패",
              axis=axis, exc_info=sys.exc_info())
    try:
        return fn(*args, **kwargs)
    finally:
        execution_ms: Optional[float] = None
        execution_clock_failure: Optional[tuple] = None
        if execution_started is not None:
            try:
                execution_ms = max(
                    0.0, (time.monotonic() - execution_started) * 1000.0
                )
            except Exception:  # noqa: BLE001 — worker_finished는 계속 기록한다
                execution_clock_failure = sys.exc_info()
        # ⚠️ 진입을 못 셌으면 종료도 세지 않는다 — `worker_in_flight` 가 음수가 된다.
        if counted:
            try:
                with _lock:
                    block = _metrics[axis]
                    block["worker_finished_total"] += 1
                    if execution_ms is not None:
                        block["execution_observed_total"] += 1
                        block["execution_ms_sum"] += execution_ms
                        block["execution_ms_max"] = max(
                            block["execution_ms_max"], execution_ms
                        )
            except Exception:  # noqa: BLE001
                _note_internal_error()
                _warn("subscribe-load: worker 종료 부기 실패", axis=axis, exc_info=sys.exc_info())
            else:
                if execution_clock_failure is not None:
                    _note_internal_error()
                    _warn(
                        "subscribe-load: worker 실행 종료 시각 기록 실패 — 종료 gauge만 반환",
                        axis=axis,
                        exc_info=execution_clock_failure,
                    )


def record_snapshot_call(channel: str) -> None:
    """`send_initial_snapshots` 진입 1회. allowlist 밖 channel 은 `unclassified` + 진단.

    ⛔ `unattributed` 로 접지 않는다 — 그건 **channel 배선 없이 들어온 default 경로**의 신호라
       (호출부가 channel 을 안 넘기면 시그니처 기본값으로 오른다), 배선 오타(allowlist 밖 값)가
       그 신호를 오염시키고 조용히 사라졌다(실측: typo → unattributed=1 / 진단 0).
    """
    folded = False
    try:
        key = channel
        if key not in CHANNELS:
            key = _UNCLASSIFIED
            folded = True
        with _lock:
            # ⛔ fold 의 internal error 는 **같은 임계구역**에서 짝으로 올린다 — 나누면 동시
            #    스냅샷이 `unclassified > internal_errors` 를 일시적으로 관측한다(실측: race
            #    probe 에서 22,700회/12만, 최대 lead 2 — 자기 치유되지만 짝 계약이 흔들린다).
            # ⛔ 그리고 **진단을 먼저** 쓴다 — 한 lock 은 예외-원자성을 주지 않는다(진입 전이와
            #    같은 교훈). `_metrics` object identity 를 유지하는 subtree-local update 에는
            #    공통 단일 commit point 가 없다 — 전역 rebind 나 스키마 재설계 대신 진단-먼저
            #    순서로 **단방향 예외 안전성**만 보장한다. 관측을 먼저 쓰면 두 번째 쓰기 실패가
            #    **진단 없는 unclassified** 를 남겼다(실측). 진단 먼저면 두 사건이
            #    따로 남는다: allowlist 밖 입력 진단 +1, 후속 unclassified 기록 실패
            #    진단 +1. 즉 실패 잔여는 `internal_errors=2 / unclassified=0` + WARNING 이다.
            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            _metrics["snapshot_send"]["calls_by_channel"][key] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: snapshot 기록 실패", axis="snapshot_send", exc_info=sys.exc_info())
        return
    if folded:
        # ⚠️ 값은 싣지 않는다 — `repr()` 호출 자체가 던질 수 있고, 여기는 진단 경로다.
        _warn("subscribe-load: allowlist 밖 channel — unclassified 로 접는다", axis="snapshot_send")


def record_snapshot_topic() -> None:
    """요청 내 **중복 제거 뒤** topic 1건 — 요청 1건이 몇 배로 퍼지는지의 분자."""
    try:
        with _lock:
            _metrics["snapshot_send"]["topics_deduped_total"] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: snapshot 기록 실패", axis="snapshot_send", exc_info=sys.exc_info())


def record_snapshot_send(outcome: str) -> None:
    """topic 1건 전송 시도의 결과 1회. allowlist 밖 outcome 은 `unclassified` + 진단.

    ⛔ `raised` 로 접지 않는다 — 그건 **실제 전송 예외**의 의미라, 오타 하나가 결함 신호를
       만들고 배선 버그는 조용히 사라졌다(실측: typo → raised=1 / 진단 0).
    """
    folded = False
    try:
        key = outcome
        if key not in SEND_OUTCOMES:
            key = _UNCLASSIFIED
            folded = True
        with _lock:
            # ⛔ 같은 임계구역 + 진단-먼저 순서 — 이유는 `record_snapshot_call` 의 주석과 동일.
            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            _metrics["snapshot_send"]["sends_by_outcome"][key] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: snapshot 기록 실패", axis="snapshot_send", exc_info=sys.exc_info())
        return
    if folded:
        # ⚠️ 값은 싣지 않는다 — `repr()` 호출 자체가 던질 수 있고, 여기는 진단 경로다.
        _warn("subscribe-load: allowlist 밖 send outcome — unclassified 로 접는다", axis="snapshot_send")


def record_snapshot_singleflight(disposition: str) -> bool:
    """요청 분류를 기록하고 shared task waiter면 release용 tracked=True를 반환한다."""
    field_by_disposition = {
        "leader": "leaders_total",
        "joined": "joined_total",
        "cache_hit": "cache_hits_total",
    }
    try:
        field = field_by_disposition[disposition]
        with _lock:
            block = _metrics["snapshot_singleflight"]
            block["requests_total"] += 1
            block[field] += 1
            waiter = disposition in {"leader", "joined"}
            if waiter:
                block["waiters_now"] += 1
                block["waiters_max"] = max(block["waiters_max"], block["waiters_now"])
        return waiter
    except Exception:  # noqa: BLE001 — 관측이 snapshot 결과를 결정하지 않는다
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot single-flight 기록 실패",
            axis="snapshot_singleflight",
            exc_info=sys.exc_info(),
        )
        return False


def record_snapshot_waiter_released(*, tracked: bool) -> None:
    """Shared task 대기의 모든 종결(timeout·cancel·build 결과)에서 waiter gauge를 내린다."""
    if not tracked:
        return
    try:
        with _lock:
            _metrics["snapshot_singleflight"]["waiters_now"] -= 1
    except Exception:  # noqa: BLE001 — 관측이 caller 결과를 바꾸지 않는다
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot waiter 반환 기록 실패",
            axis="snapshot_singleflight",
            exc_info=sys.exc_info(),
        )


def begin_snapshot_admission_wait() -> Optional[float]:
    """FIFO slot을 실제로 기다리기 시작한 leader 후보 1건."""
    try:
        started = time.monotonic()
        with _lock:
            block = _metrics["snapshot_admission"]
            block["queued_now"] += 1
            block["queued_max"] = max(block["queued_max"], block["queued_now"])
        return started
    except Exception:  # noqa: BLE001 — 관측 실패가 admission을 막지 않는다
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot admission 대기 시작 기록 실패",
            axis="snapshot_admission",
            exc_info=sys.exc_info(),
        )
        return None


def finish_snapshot_admission_wait(started_at: Optional[float]) -> None:
    """admit·deadline·cancel 어느 종결이든 queued gauge를 한 번 내린다."""
    if started_at is None:
        return
    wait_ms: Optional[float] = None
    clock_failure: Optional[tuple] = None
    try:
        wait_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
    except Exception:  # noqa: BLE001 — gauge 반환은 계속해야 한다
        clock_failure = sys.exc_info()
    try:
        with _lock:
            block = _metrics["snapshot_admission"]
            block["queued_now"] -= 1
            if wait_ms is not None:
                block["wait_observed_total"] += 1
                block["wait_ms_sum"] += wait_ms
                block["wait_ms_max"] = max(block["wait_ms_max"], wait_ms)
    except Exception:  # noqa: BLE001 — 관측 실패가 admission 결과를 바꾸지 않는다
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot admission 대기 종결 기록 실패",
            axis="snapshot_admission",
            exc_info=sys.exc_info(),
        )
        return
    if clock_failure is not None:
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot admission 대기 시간 기록 실패 — gauge만 반환",
            axis="snapshot_admission",
            exc_info=clock_failure,
        )


def record_snapshot_admission_acquired() -> bool:
    """Shared build가 slot을 실제 점유했다. 반환값은 release 부기 여부를 결속한다."""
    try:
        with _lock:
            block = _metrics["snapshot_admission"]
            block["in_flight"] += 1
            block["in_flight_max"] = max(block["in_flight_max"], block["in_flight"])
        return True
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot admission 점유 기록 실패",
            axis="snapshot_admission",
            exc_info=sys.exc_info(),
        )
        return False


def record_snapshot_admission_released(*, tracked: bool) -> None:
    """Permit을 반환한다. acquire 부기가 실패했으면 음수 gauge를 만들지 않는다."""
    if not tracked:
        return
    try:
        with _lock:
            _metrics["snapshot_admission"]["in_flight"] -= 1
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot admission 반환 기록 실패",
            axis="snapshot_admission",
            exc_info=sys.exc_info(),
        )


def record_snapshot_failure_cooldown_armed(*, failure_class: str) -> None:
    """Transient shared-build 실패로 key별 cooldown을 새로 만든 1건."""
    folded = failure_class not in SNAPSHOT_FAILURE_CLASSES
    key = failure_class if not folded else _UNCLASSIFIED
    try:
        with _lock:
            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            block = _metrics["snapshot_failure_cooldown"]
            block["armed_total"] += 1
            block["by_failure_class"][key] += 1
    except Exception:  # noqa: BLE001 — cooldown 정책을 관측이 결정하지 않는다
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot failure cooldown arm 기록 실패",
            axis="snapshot_failure_cooldown",
            exc_info=sys.exc_info(),
        )
        return
    if folded:
        _warn(
            "subscribe-load: allowlist 밖 cooldown failure class — unclassified 로 접는다",
            axis="snapshot_failure_cooldown",
        )


def record_snapshot_failure_cooldown_suppressed(
    *,
    topic: str,
    generation: int,
    supported: bool,
    enabled: bool,
    failure_class: str,
    remaining_seconds: float,
    suppressed_builds: int,
) -> None:
    """마지막 suppression의 안전한 key·원인·남은 시간과 누계를 남긴다."""
    folded = failure_class not in SNAPSHOT_FAILURE_CLASSES
    key = failure_class if not folded else _UNCLASSIFIED
    try:
        last = {
            "topic": topic,
            "generation": generation,
            "supported": supported,
            "enabled": enabled,
            "failure_class": key,
            "cooldown_remaining_ms": round(
                max(0.0, remaining_seconds) * 1000.0, 3
            ),
            "suppressed_builds": suppressed_builds,
        }
        with _lock:
            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            block = _metrics["snapshot_failure_cooldown"]
            block["suppressed_total"] += 1
            block["last_suppression"] = last
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot failure cooldown suppression 기록 실패",
            axis="snapshot_failure_cooldown",
            exc_info=sys.exc_info(),
        )
        return
    if folded:
        _warn(
            "subscribe-load: allowlist 밖 cooldown failure class — unclassified 로 접는다",
            axis="snapshot_failure_cooldown",
        )


def record_snapshot_failure_cooldown_expired(*, failure_class: str) -> None:
    """만료된 key state를 제거한 1건. failure_class는 동적 key 생성 방지 검사용이다."""
    folded = failure_class not in SNAPSHOT_FAILURE_CLASSES
    try:
        with _lock:
            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            _metrics["snapshot_failure_cooldown"]["expired_total"] += 1
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot failure cooldown expiry 기록 실패",
            axis="snapshot_failure_cooldown",
            exc_info=sys.exc_info(),
        )


def record_snapshot_success_cached() -> None:
    """generation이 여전히 같아 성공 payload를 cache에 넣은 실제 build 1건."""
    try:
        with _lock:
            _metrics["snapshot_singleflight"]["successes_cached_total"] += 1
    except Exception:  # noqa: BLE001 — 관측이 snapshot 결과를 결정하지 않는다
        _note_internal_error()
        _warn(
            "subscribe-load: snapshot success cache 기록 실패",
            axis="snapshot_singleflight",
            exc_info=sys.exc_info(),
        )


def record_auth_wire_deadline_expired(stage: str) -> None:
    """wire deadline 이 **실제로 만료**된 분기 1회 — 사용자에게 보인 과부하 결과.

    ⛔ 검증자 내부 TimeoutError(미만료 재전파)는 세지 않는다 — 그 계약은 배선이 지킨다
       (`expired()` True 분기 안에서만 호출). stage 는 우리 리터럴이라 allowlist 밖 =
       배선 오류 → unclassified + 진단.
    """
    folded = False
    try:
        key = stage
        if key not in DEADLINE_STAGES:
            key = _UNCLASSIFIED
            folded = True
        with _lock:
            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            _metrics["terminal"]["auth_wire_deadline_expired_by_stage"][key] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: terminal 기록 실패", axis="terminal", exc_info=sys.exc_info())
        return
    if folded:
        _warn("subscribe-load: allowlist 밖 deadline stage — unclassified 로 접는다", axis="terminal")


def record_subscribe_auth_failed(code: str) -> None:
    """`SubscribeAuthFailed`(Firebase 전이 등 전체-요청 오류) 1회 — 어느 축에도 안 잡히던 gap.

    ⛔ error 코드는 **런타임 관측값** — topic_wire 가 진화하면 새 코드가 정당하게 나타난다.
       allowlist 밖은 `other` 로 접고 WARNING 1줄만(배선 오류가 아니라 관측 fold — internal
       error 를 세지 않는다. deadline stage 의 unclassified fold 와 의미가 다르다).
    """
    folded = False
    try:
        # ⛔ "other" 는 fold **target** 이지 제출 가능한 코드가 아니다 — wire 어휘에 없고
        #    frame builder 도 거부한다. 리터럴 "other" 제출을 면제하면 sentinel 문자열이
        #    진단을 우회한다(S1c unclassified 우회와 동형 — 세 번째 재발을 여기서 잠근다).
        key = code if code in AUTH_FAIL_CODES else "other"
        folded = key == "other"
        with _lock:
            _metrics["terminal"]["subscribe_auth_failed_by_error"][key] += 1
    except Exception:  # noqa: BLE001 — 관측이 서비스 경로를 흔들지 않는다
        _note_internal_error()
        _warn("subscribe-load: terminal 기록 실패", axis="terminal", exc_info=sys.exc_info())
        return
    if folded:
        _warn("subscribe-load: 알려지지 않은 auth-fail 코드 — other 로 접는다", axis="terminal")


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
                # ⛔ 노출 dict 는 `_blank()` 를 그대로 흘리지 않고 **여기서 조립**한다 —
                #    저장 스키마에 필드를 더해도 이 줄이 없으면 영원히 안 보인다(실측).
                snap["queue_wait_observed_total"] = block["queue_wait_observed_total"]
                snap["queue_wait_ms_sum"] = block["queue_wait_ms_sum"]
                snap["queue_wait_ms_max"] = block["queue_wait_ms_max"]
                snap["execution_observed_total"] = block["execution_observed_total"]
                snap["execution_ms_sum"] = block["execution_ms_sum"]
                snap["execution_ms_max"] = block["execution_ms_max"]
            out[axis] = snap
        send = _metrics["snapshot_send"]
        out["snapshot_send"] = {
            "calls_by_channel": dict(send["calls_by_channel"]),
            "topics_deduped_total": send["topics_deduped_total"],
            "sends_by_outcome": dict(send["sends_by_outcome"]),
        }
        out["snapshot_singleflight"] = dict(_metrics["snapshot_singleflight"])
        admission = _metrics["snapshot_admission"]
        out["snapshot_admission"] = {
            **admission,
            "wait_ms_sum": round(admission["wait_ms_sum"], 3),
            "wait_ms_max": round(admission["wait_ms_max"], 3),
        }
        cooldown = _metrics["snapshot_failure_cooldown"]
        out["snapshot_failure_cooldown"] = {
            "armed_total": cooldown["armed_total"],
            "suppressed_total": cooldown["suppressed_total"],
            "expired_total": cooldown["expired_total"],
            "by_failure_class": dict(cooldown["by_failure_class"]),
            "last_suppression": (
                None
                if cooldown["last_suppression"] is None
                else dict(cooldown["last_suppression"])
            ),
        }
        terminal = _metrics["terminal"]
        out["terminal"] = {
            "auth_wire_deadline_expired_by_stage": dict(terminal["auth_wire_deadline_expired_by_stage"]),
            "subscribe_auth_failed_by_error": dict(terminal["subscribe_auth_failed_by_error"]),
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
    ⛔ 게이지에 더해 **불변식(`started == sum(by_outcome) + awaiting`)도 검사**한다 — 게이지만
       보면 부분 전이로 손상된 장부(예: `started=1 / awaiting=0 / terminal 0`)를 정상으로
       수용·삭제한다(실측). 손상 장부는 증거다 — reset 으로 지우지 않는다.
    """
    with _lock:
        busy = [
            axis for axis in AXIS_OUTCOMES
            if _metrics[axis]["callers_awaiting"] != 0
            or (axis in WORKER_AXES
                and _metrics[axis]["worker_started_total"] != _metrics[axis]["worker_finished_total"])
        ]
        admission = _metrics["snapshot_admission"]
        if admission["queued_now"] != 0 or admission["in_flight"] != 0:
            busy.append("snapshot_admission")
        if _metrics["snapshot_singleflight"]["waiters_now"] != 0:
            busy.append("snapshot_singleflight")
        if busy:
            raise SubscribeLoadContractError(
                f"진행 중인 관측이 있어 reset 을 거부한다: {sorted(busy)}"
            )
        corrupt = [
            axis for axis in AXIS_OUTCOMES
            if _metrics[axis]["started_total"]
            != sum(_metrics[axis]["by_outcome"].values()) + _metrics[axis]["callers_awaiting"]
        ]
        if corrupt:
            raise SubscribeLoadContractError(
                f"장부 불변식이 깨져 있다 — reset 으로 증거를 지우지 않는다: {sorted(corrupt)}"
            )
        _metrics.clear()
        _metrics.update(_blank())
