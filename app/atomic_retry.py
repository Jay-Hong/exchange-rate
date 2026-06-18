"""P1b B3 — retry/backoff + R1 budget + partial_pending policy (§19:348-352, pure dormant).

B2b-4b coordinator 위에 얹는 **순수 retry 정책 계층**. mechanism이 아니라 policy/classification만 — 실제
watermark-only retry mechanism은 이미 B2b-4b `_recover_marker`에 land됐고, 실제 scheduling(timer/sleep/loop/
polling/event wakeup)은 **C6 runtime**. B3는 결과를 lane으로 분류하고 backoff/R1 budget을 **injected now**로
계산하고 partial repair 대상을 판정하는 pure 함수 집합이다.

구성:
- `classify_attempt(PublishResult) -> AttemptClassification`: PublishResult(decision XOR outcome)를 retry
  lane + R1 accounting + retriable로 분류. **exhaustive** over PublishOutcome(10) + DecisionAction(4 non-PUBLISH).
  핵심 불변: WATERMARK_WRITE_FAILED → WATERMARK_ONLY (절대 SEND_RETRY 아님 — double-publish 방지).
- `backoff_delay`/`next_fire_at`: capped-exponential(0.5→1→2→4→4…, no jitter, no max-attempt). pure, no sleep.
- `R1Budget` + `r1_elapsed/remaining/status`: end-to-end budget(§19:349, anchor는 C6 주입). **SOFT only**
  (§12.1 — over 15s는 telemetry alert, hard abort 아님).
- `classify_partial_repair`: derive_pending(B2b-1) + build.missing 교차 → AVAILABILITY_REPAIR / NO_RETRY /
  STRUCTURAL_ALERT. identical-partial 재발행 금지(§19:351)는 **B2b-3 SKIP_DEDUP_IDENTICAL이 이미 처리** —
  B3가 재구현 안 함(dead code 회피).
- `RetryState` + `advance_retry_state`: frozen value + pure advancer (C6가 instance 소유).
- `decide_retry`: non-terminal lane → ELIGIBLE_NOW / WAIT / SUSPEND_EVENT_DRIVEN. terminal/blocked는 입력 X.

**dormant**: live caller 0. stdlib + atomic_* island import만. **직접 asyncio/sleep/scheduler/live-I/O import·call
0**(positive AST trip-wire 강제). 모든 time은 injected now(monotonic float seconds). atomic_coordinator.py 미수정.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from app.atomic_build import BuildResult
from app.atomic_coordinator import PublishOutcome, PublishResult
from app.atomic_reconcile import DecisionAction, PendingReason
from app.fx_membership import FX_MEMBERSHIP_SOURCES


# ────────────────────────────── enums ──────────────────────────────


class RetryLane(enum.Enum):
    """retry lane (§19:350). classify_attempt이 산출하는 건 SEND_RETRY/WATERMARK_ONLY/EVENT_DRIVEN_WAIT/
    TERMINAL_NONE/BLOCKED. **BACKSTOP은 C6 주기 sweep trigger 라벨**(결과 분류 아님 — classify_attempt 미산출,
    decide_retry 미처리; §19:350 vocabulary 완성 + C6가 backstop 발화 태깅에 사용)."""
    SEND_RETRY = "send_retry"              # 전체 publish_asset 재진입(re-build + re-send). send-stage 실패 + build 실패.
    WATERMARK_ONLY = "watermark_only"      # sent_but_uncommitted — 재전송 X, watermark write만 재시도(§12.3:218).
    BACKSTOP = "backstop"                  # C6 주기 sweep trigger 라벨(B3 미산출/미처리).
    EVENT_DRIVEN_WAIT = "event_driven_wait"  # subscriber-0/disabled/gate — polling X, event 대기(§12.3:220-221).
    TERMINAL_NONE = "terminal_none"        # 완료/superseded — retry 대상 아님.
    BLOCKED = "blocked"                    # conflict/structural/divergent/lineage — alert, retry 아님.


class WaitTrigger(enum.Enum):
    """EVENT_DRIVEN_WAIT의 해소 event (C6가 구독·발화). lane만으로는 어떤 event를 기다리는지 모호해 분리."""
    SUBSCRIBER_APPEARANCE = "subscriber_appearance"  # NO_SUBSCRIBERS / SKIP_SUBSCRIBER_ZERO
    FF_RE_ENABLE = "ff_re_enable"                     # DISABLED (FF off)
    GATE_OPEN = "gate_open"                           # GATE_WOULD_BLOCK (publisher gate CLOSED)


class R1AccountingKind(enum.Enum):
    """R1 budget 관점 분류 (§19:349/352)."""
    COUNTS = "counts"                                  # send-retry — R1 publish budget에 계수
    WATERMARK_LAG_NOT_COUNTED = "watermark_lag_not_counted"  # sent_but_uncommitted — publish 됨, R1 위반 아님(§19:352)
    OUT_OF_SCOPE = "out_of_scope"                       # terminal/blocked/event-wait — R1 미적용


class R1Status(enum.Enum):
    """R1 budget 상태. **SOFT only** — over 15s는 ON_TRACK→SOFT_BREACH로 alert이지 abort 아님(§12.1)."""
    ON_TRACK = "on_track"
    SOFT_BREACH = "soft_breach"


class RetryVerdict(enum.Enum):
    """decide_retry 판정 (non-terminal lane만). GIVE_UP 없음 — 종료는 lane(TERMINAL_NONE/BLOCKED)이, over-budget은
    RetryDecision.over_soft_budget telemetry flag이 담당(§12.1 retry-until-success)."""
    ELIGIBLE_NOW = "eligible_now"                # backoff 경과 — 지금 재시도 가능
    WAIT = "wait"                                # backoff 미경과 — wait_until까지 대기
    SUSPEND_EVENT_DRIVEN = "suspend_event_driven"  # timer retry 중단, event 대기(subscriber/FF/gate)


class PartialVerdict(enum.Enum):
    """partial_pending per-source 판정 (§19:351)."""
    NO_RETRY = "no_retry"                        # no-DB-value(genuinely absent) 또는 build에 포함됨 — repair 불요
    AVAILABILITY_REPAIR = "availability_repair"  # DB-valued인데 build에서 빠짐 — availability 복구 후 rebuild
    STRUCTURAL_ALERT = "structural_alert"        # watermark revision_key 파싱 불가 — alert(repair-loop 아님)


# ────────────────────────────── classify_attempt (lane + accounting) ──────────────────────────────


@dataclass(frozen=True)
class AttemptClassification:
    """publish 시도 결과 1건의 retry 분류. invariant: wait_trigger ⟺ EVENT_DRIVEN_WAIT / retriable ⟺
    lane ∈ {SEND_RETRY, WATERMARK_ONLY}."""
    lane: RetryLane
    accounting: R1AccountingKind
    retriable: bool
    reason: str
    wait_trigger: Optional[WaitTrigger] = None

    def __post_init__(self) -> None:
        if (self.wait_trigger is not None) != (self.lane is RetryLane.EVENT_DRIVEN_WAIT):
            raise ValueError("AttemptClassification: wait_trigger ⟺ EVENT_DRIVEN_WAIT 위반")
        if self.retriable != (self.lane in (RetryLane.SEND_RETRY, RetryLane.WATERMARK_ONLY)):
            raise ValueError("AttemptClassification: retriable ⟺ lane∈{SEND_RETRY,WATERMARK_ONLY} 위반")
        # accounting ⟺ lane 결합 (§19:352 — 테이블 편집/신규 행이 R1 budget을 silently under-count하지 않게):
        # COUNTS⟺SEND_RETRY / WATERMARK_LAG_NOT_COUNTED⟺WATERMARK_ONLY / 그 외 OUT_OF_SCOPE. import 시 강제.
        expected_acct = (
            R1AccountingKind.COUNTS if self.lane is RetryLane.SEND_RETRY
            else R1AccountingKind.WATERMARK_LAG_NOT_COUNTED if self.lane is RetryLane.WATERMARK_ONLY
            else R1AccountingKind.OUT_OF_SCOPE
        )
        if self.accounting is not expected_acct:
            raise ValueError(
                f"AttemptClassification: accounting⟺lane 위반 (lane={self.lane}, accounting={self.accounting}, "
                f"expected={expected_acct})"
            )


def _ac(lane, accounting, retriable, reason, wait_trigger=None) -> AttemptClassification:
    return AttemptClassification(lane=lane, accounting=accounting, retriable=retriable, reason=reason,
                                 wait_trigger=wait_trigger)


# post-send PublishOutcome(10) → 분류. WATERMARK_WRITE_FAILED은 WATERMARK_ONLY(절대 SEND_RETRY 아님 —
# double-publish 방지). ALL_SEND_FAILED/SEND_EXCEPTION만 SEND_RETRY(state-safe: seq/write 전, marker 미생성).
_OUTCOME_CLASSIFICATION: Dict[PublishOutcome, AttemptClassification] = {
    PublishOutcome.COMMITTED:
        _ac(RetryLane.TERMINAL_NONE, R1AccountingKind.OUT_OF_SCOPE, False, "committed"),
    PublishOutcome.NO_SUBSCRIBERS:
        _ac(RetryLane.EVENT_DRIVEN_WAIT, R1AccountingKind.OUT_OF_SCOPE, False,
            "no_subscribers_post_send", WaitTrigger.SUBSCRIBER_APPEARANCE),
    PublishOutcome.ALL_SEND_FAILED:
        _ac(RetryLane.SEND_RETRY, R1AccountingKind.COUNTS, True, "all_send_failed"),
    PublishOutcome.SEND_EXCEPTION:
        _ac(RetryLane.SEND_RETRY, R1AccountingKind.COUNTS, True, "send_exception"),
    PublishOutcome.GATE_WOULD_BLOCK:
        _ac(RetryLane.EVENT_DRIVEN_WAIT, R1AccountingKind.OUT_OF_SCOPE, False,
            "gate_would_block", WaitTrigger.GATE_OPEN),
    PublishOutcome.DISABLED:
        _ac(RetryLane.EVENT_DRIVEN_WAIT, R1AccountingKind.OUT_OF_SCOPE, False,
            "ff_disabled", WaitTrigger.FF_RE_ENABLE),
    PublishOutcome.WATERMARK_WRITE_FAILED:
        _ac(RetryLane.WATERMARK_ONLY, R1AccountingKind.WATERMARK_LAG_NOT_COUNTED, True,
            "watermark_write_failed"),
    PublishOutcome.WATERMARK_STALE:
        _ac(RetryLane.TERMINAL_NONE, R1AccountingKind.OUT_OF_SCOPE, False, "watermark_stale_superseded"),
    PublishOutcome.WATERMARK_DIVERGENT_ALERT:
        _ac(RetryLane.BLOCKED, R1AccountingKind.OUT_OF_SCOPE, False, "watermark_divergent_alert"),
    PublishOutcome.WATERMARK_LINEAGE_MISMATCH:
        _ac(RetryLane.BLOCKED, R1AccountingKind.OUT_OF_SCOPE, False, "watermark_lineage_mismatch"),
}

# pre-send DecisionAction relay(4 non-PUBLISH) → 분류. PUBLISH은 result.decision에 안 실림(send로 진행) → ValueError.
_DECISION_CLASSIFICATION: Dict[DecisionAction, AttemptClassification] = {
    DecisionAction.SKIP_DEDUP_IDENTICAL:
        _ac(RetryLane.TERMINAL_NONE, R1AccountingKind.OUT_OF_SCOPE, False, "skip_dedup_identical"),
    DecisionAction.SKIP_SUBSCRIBER_ZERO:
        _ac(RetryLane.EVENT_DRIVEN_WAIT, R1AccountingKind.OUT_OF_SCOPE, False,
            "skip_subscriber_zero_pre_send", WaitTrigger.SUBSCRIBER_APPEARANCE),
    DecisionAction.BLOCK:
        _ac(RetryLane.BLOCKED, R1AccountingKind.OUT_OF_SCOPE, False, "decision_block"),
    DecisionAction.RETRY:
        _ac(RetryLane.SEND_RETRY, R1AccountingKind.COUNTS, True, "decision_retry_rebuild"),
}


def classify_attempt(result: PublishResult) -> AttemptClassification:
    """PublishResult(decision XOR outcome) → retry lane + R1 accounting + retriable (pure, exhaustive).

    decision-tagged(pre-send relay): SKIP_DEDUP_IDENTICAL/SKIP_SUBSCRIBER_ZERO/BLOCK/RETRY. PUBLISH은
    result로 안 옴(send 진행) → ValueError. outcome-tagged(post-send): PublishOutcome 10종.
    미매핑 enum → ValueError(write_action_for_relation 패턴 — 새 멤버가 silently 누락되지 않게).
    """
    if (result.decision is None) == (result.outcome is None):
        raise ValueError("classify_attempt: PublishResult는 decision XOR outcome 정확히 하나여야")
    if result.decision is not None:
        action = result.decision.action
        try:
            return _DECISION_CLASSIFICATION[action]
        except KeyError:
            # PUBLISH(result에 안 실려야) 또는 미래 신규 action — fail-closed
            raise ValueError(f"classify_attempt: 분류 불가 DecisionAction — {action!r}")
    outcome = result.outcome
    try:
        return _OUTCOME_CLASSIFICATION[outcome]
    except KeyError:
        raise ValueError(f"classify_attempt: 미매핑 PublishOutcome — {outcome!r}")


# ────────────────────────────── backoff (pure, no sleep) ──────────────────────────────

# §12.5: backoff 간격은 구현 spec 이월 → B3가 확정. capped-exponential, 15s soft budget 안에 detection+build+
# first-send와 함께 들어가도록 front-loaded(빠른 첫 재시도) + bounded. no jitter(single-coordinator/worker==1/
# 3 FX asset = thundering-herd 없음). no max-attempt(§12.1 retry-until-success, over-budget은 alert).
_DEFAULT_BACKOFF_BASE = 0.5
_DEFAULT_BACKOFF_CAP = 4.0
_BACKOFF_EXP_CLAMP = 64  # 2^64 * base >> 어떤 cap이든 → overflow 회피용 지수 상한


@dataclass(frozen=True)
class BackoffParams:
    base_seconds: float = _DEFAULT_BACKOFF_BASE
    cap_seconds: float = _DEFAULT_BACKOFF_CAP

    def __post_init__(self) -> None:
        # finiteness 우선 (inf base/cap는 backoff_delay=inf → 영구 WAIT로 retry-until-success 무력화; NaN도 차단)
        if not math.isfinite(self.base_seconds) or not math.isfinite(self.cap_seconds):
            raise ValueError(f"BackoffParams: base/cap finite여야 — got base={self.base_seconds!r}, cap={self.cap_seconds!r}")
        if not (self.base_seconds > 0):
            raise ValueError(f"BackoffParams.base_seconds: > 0 — got {self.base_seconds!r}")
        if self.cap_seconds < self.base_seconds:
            raise ValueError(f"BackoffParams.cap_seconds: >= base_seconds — got {self.cap_seconds!r}")


_DEFAULT_BACKOFF = BackoffParams()


def backoff_delay(attempt_index: int, params: BackoffParams = _DEFAULT_BACKOFF) -> float:
    """attempt_index(0-based) → backoff delay 초. base*2^idx, cap_seconds로 clamp. pure, no sleep.

    0-based: idx0=base(0.5), idx1=1, idx2=2, idx3=4, idx≥3=cap(4). 지수 overflow 방지(idx clamp 64).
    """
    if not isinstance(attempt_index, int) or isinstance(attempt_index, bool) or attempt_index < 0:
        raise ValueError(f"backoff_delay: attempt_index non-negative int — got {attempt_index!r}")
    raw = params.base_seconds * (2 ** min(attempt_index, _BACKOFF_EXP_CLAMP))
    return min(raw, params.cap_seconds)


def next_fire_at(last_fire_at: float, attempt_index: int, params: BackoffParams = _DEFAULT_BACKOFF) -> float:
    """last_fire_at + backoff_delay(attempt_index) — 다음 재시도 eligible 시각 (pure)."""
    return last_fire_at + backoff_delay(attempt_index, params)


# ────────────────────────────── R1 budget (injected now, SOFT only) ──────────────────────────────

R1_SOFT_TARGET_SECONDS = 15.0  # §19:349 / §12.1 soft target (hard deadline 아님)


@dataclass(frozen=True)
class R1Budget:
    """end-to-end R1 budget. started_at = C6 주입 anchor(detection/pending-created 시각, §19:349 — B3는
    anchor를 **산출하지 않음**). in-process only — restart 시 marker/retry state와 함께 유실(§12.3:218)."""
    asset: str
    started_at: float
    soft_target_seconds: float = R1_SOFT_TARGET_SECONDS

    def __post_init__(self) -> None:
        # sibling dataclass invariant discipline 일관 — soft_target<=0/비유한은 SOFT_BREACH 판정 왜곡(C6 footgun).
        if not math.isfinite(self.started_at):
            raise ValueError(f"R1Budget.started_at: finite여야 — got {self.started_at!r}")
        if not math.isfinite(self.soft_target_seconds) or self.soft_target_seconds <= 0:
            raise ValueError(f"R1Budget.soft_target_seconds: 유한 양수여야 — got {self.soft_target_seconds!r}")


def r1_elapsed(budget: R1Budget, now: float) -> float:
    return now - budget.started_at


def r1_remaining(budget: R1Budget, now: float) -> float:
    """남은 soft budget(초). 음수 = soft target 초과(SOFT_BREACH)."""
    return budget.soft_target_seconds - r1_elapsed(budget, now)


def r1_status(budget: R1Budget, now: float) -> R1Status:
    """ON_TRACK / SOFT_BREACH. **abort 없음** — SOFT_BREACH는 alert이지 retry 중단 아님(§12.1)."""
    return R1Status.SOFT_BREACH if r1_elapsed(budget, now) > budget.soft_target_seconds else R1Status.ON_TRACK


# ────────────────────────────── retry state + decide_retry ──────────────────────────────


@dataclass(frozen=True)
class RetryState:
    """per-asset retry 진행 상태. C6가 instance 소유(B1/B2a/B2b dormant-value 선례). attempt_index = 이미
    발화한 retry 수(0=아직 없음). last_fire_at = 마지막 발화 시각(직전 시도 시각, None=첫 retry 결정)."""
    asset: str
    attempt_index: int = 0
    last_fire_at: Optional[float] = None
    lane: Optional[RetryLane] = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_index, int) or isinstance(self.attempt_index, bool) or self.attempt_index < 0:
            raise ValueError(f"RetryState.attempt_index: non-negative int — got {self.attempt_index!r}")


def advance_retry_state(state: RetryState, *, now: float, lane: RetryLane) -> RetryState:
    """retry 1회 발화 후 상태 전진(pure value advancer). attempt_index+1, last_fire_at=now."""
    return RetryState(asset=state.asset, attempt_index=state.attempt_index + 1, last_fire_at=now, lane=lane)


@dataclass(frozen=True)
class RetryDecision:
    """decide_retry 결과. wait_until ⟺ verdict==WAIT. over_soft_budget = R1 SOFT_BREACH telemetry flag
    (retry 중단 아님 — §12.1)."""
    verdict: RetryVerdict
    wait_until: Optional[float]
    reason: str
    over_soft_budget: bool

    def __post_init__(self) -> None:
        if (self.wait_until is not None) != (self.verdict is RetryVerdict.WAIT):
            raise ValueError("RetryDecision: wait_until ⟺ verdict==WAIT 위반")


_NON_DECIDABLE_LANES = frozenset({RetryLane.TERMINAL_NONE, RetryLane.BLOCKED, RetryLane.BACKSTOP})


def is_decidable(classification: AttemptClassification) -> bool:
    """decide_retry에 넘겨도 되는가 — **C6가 gate에 쓸 정확한 술어**. `lane ∈ {SEND_RETRY, WATERMARK_ONLY,
    EVENT_DRIVEN_WAIT}` (= non-terminal/non-blocked/non-backstop).

    ⚠️ `classification.retriable`로 gate하지 말 것: retriable은 {SEND_RETRY, WATERMARK_ONLY}만 True라
    EVENT_DRIVEN_WAIT(retriable=False)을 빠뜨려 SUSPEND_EVENT_DRIVEN 라우팅을 잃는다(post-send subscriber-0/
    FF-off/gate-closed가 영구 미처리). decide_retry는 EVENT_DRIVEN_WAIT을 SUSPEND로 처리해야 하므로 lane gate가 옳다.
    """
    return classification.lane not in _NON_DECIDABLE_LANES


def _timed_retry(state: RetryState, params: BackoffParams, now: float, reason: str,
                 over: bool) -> RetryDecision:
    anchor = state.last_fire_at if state.last_fire_at is not None else now
    fire_at = next_fire_at(anchor, state.attempt_index, params)
    if now >= fire_at:
        return RetryDecision(RetryVerdict.ELIGIBLE_NOW, None, reason, over)
    return RetryDecision(RetryVerdict.WAIT, fire_at, reason, over)


def decide_retry(
    classification: AttemptClassification,
    state: RetryState,
    budget: R1Budget,
    *,
    now: float,
    subscriber_present: bool,
    params: BackoffParams = _DEFAULT_BACKOFF,
) -> RetryDecision:
    """decidable lane → ELIGIBLE_NOW / WAIT / SUSPEND_EVENT_DRIVEN (pure). over_soft_budget은 telemetry.

    - EVENT_DRIVEN_WAIT → SUSPEND_EVENT_DRIVEN (timer 아닌 event 대기).
    - SEND_RETRY → subscriber_present False면 SUSPEND_EVENT_DRIVEN(send 실패 중 구독자 0 전환 = event 대기,
      §12.3:223), 아니면 backoff 기반 ELIGIBLE_NOW/WAIT.
    - WATERMARK_ONLY → subscriber-independent(send 이미 발생) — backoff 기반.
    - TERMINAL_NONE / BLOCKED / BACKSTOP → ValueError. **호출자는 `is_decidable(classification)`로 gate**해야
      (classification.retriable로 gate 금지 — EVENT_DRIVEN_WAIT 누락; 위 is_decidable docstring 참조).
    """
    over = r1_status(budget, now) is R1Status.SOFT_BREACH
    lane = classification.lane
    if lane is RetryLane.EVENT_DRIVEN_WAIT:
        # __post_init__ invariant: EVENT_DRIVEN_WAIT ⟹ wait_trigger non-None (None 분기 불가).
        return RetryDecision(RetryVerdict.SUSPEND_EVENT_DRIVEN, None,
                             f"event_wait:{classification.wait_trigger.value}", over)
    if lane is RetryLane.SEND_RETRY:
        if not subscriber_present:
            return RetryDecision(RetryVerdict.SUSPEND_EVENT_DRIVEN, None, "send_retry_subscriber_gone", over)
        return _timed_retry(state, params, now, "send_retry", over)
    if lane is RetryLane.WATERMARK_ONLY:
        return _timed_retry(state, params, now, "watermark_only", over)
    raise ValueError(f"decide_retry: non-retriable lane {lane!r} — 호출자가 classification으로 gate해야")


# ────────────────────────────── partial_pending repair classification ──────────────────────────────


def classify_partial_repair(
    pending: Mapping[str, PendingReason], build_result: BuildResult
) -> Dict[str, PartialVerdict]:
    """per-source partial repair 판정 (§19:351, pure). derive_pending(B2b-1) + build.missing 교차.

    AVAILABILITY_REPAIR = source ∈ build.missing_sources AND derive_pending reason이 'DB has value'(=
    PENDING_NOT_IN_PRESENT / PENDING_DB_AHEAD / **NOT_PENDING_CURRENT**). NOT_PENDING_CURRENT 포함이 핵심:
    db_rev<=present라도 watermark가 그 source를 가졌는데 이번 build가 빠뜨렸으면 coverage regression =
    availability 문제 → repair 대상. STRUCTURAL → STRUCTURAL_ALERT(repair-loop 아님). NOT_PENDING_DB_ABSENT
    → NO_RETRY(진짜 값 없음). build에 포함된 source(missing 아님) → NO_RETRY.

    repair MECHANISM(Redis re-read / mirror / targeted reconciliation)은 C6 — B3는 **WHICH** source가 repair
    대상인지만 판정. identical-partial 재발행 금지(§19:351)는 B2b-3 SKIP_DEDUP_IDENTICAL이 이미 처리(중복 X).

    **coherence precondition**: pending과 build_result는 **같은 reconciliation cycle**에서 도출돼야 한다. db_revisions
    read(③)와 build(④)가 다른 snapshot이라, 진짜로 사라진 source(30d retention age-out 등)가 일시적으로 DB-has-value로
    보여 false AVAILABILITY_REPAIR가 날 수 있다. C6가 매 iteration derive_pending을 fresh 재도출하면 다음 cycle엔
    NOT_PENDING_DB_ABSENT로 재분류돼 repair lane을 벗어난다(self-correct, loop 아님). pending keys는
    FX_MEMBERSHIP_SOURCES 전체여야(derive_pending 계약) — 불일치는 호출 버그(ValueError).

    Raises: ValueError — pending keys != FX_MEMBERSHIP_SOURCES / 미인식 PendingReason(fail-closed).
    """
    if set(pending) != FX_MEMBERSHIP_SOURCES:
        raise ValueError(
            f"classify_partial_repair: pending keys != FX_MEMBERSHIP_SOURCES "
            f"(missing={FX_MEMBERSHIP_SOURCES - set(pending)}, extra={set(pending) - FX_MEMBERSHIP_SOURCES})"
        )
    _DB_HAS_VALUE = (PendingReason.PENDING_NOT_IN_PRESENT, PendingReason.PENDING_DB_AHEAD,
                     PendingReason.NOT_PENDING_CURRENT)
    missing = set(build_result.missing_sources)
    result: Dict[str, PartialVerdict] = {}
    for source, reason in pending.items():
        if reason is PendingReason.STRUCTURAL:
            result[source] = PartialVerdict.STRUCTURAL_ALERT
        elif reason is PendingReason.NOT_PENDING_DB_ABSENT:
            result[source] = PartialVerdict.NO_RETRY
        elif reason in _DB_HAS_VALUE:
            # DB has value: build이 잃었으면(missing) repair / 포함했으면 정상(NO_RETRY)
            result[source] = PartialVerdict.AVAILABILITY_REPAIR if source in missing else PartialVerdict.NO_RETRY
        else:  # 미래 신규 PendingReason — fail-closed(silently AVAILABILITY/NO_RETRY 오분류 차단, classify_attempt 패턴)
            raise ValueError(f"classify_partial_repair: 미인식 PendingReason — {reason!r}")
    return result
