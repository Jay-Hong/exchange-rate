"""P1b A4 — WriteOutcome + PendingCandidate interface (§14, dormant).

§14 outcome 계약(P1↔D 인터페이스)의 **타입 + 파생 로직**. writer(atomic mode)가 A3-2 Lua
compare/write outcome을 publish-agnostic `WriteOutcome`으로 변환하고, committed 변경마다
`PendingCandidate`를 D로 핸드오프. **dormant** — atomic-mode-only(legacy writer는 bool/legacy enum
그대로). 실제 writer 배선 + candidate sink/queue + D 소비는 A5/C6/B2b.

§14 5-state ↔ 2축(redis_write_performed / revision_advanced)은 state 단독 파생 불가 — 본 모듈이
A3-2 Lua outcome + failure context를 받아 결정적으로 채움. 레거시 `UsdtLatestWriteOutcome`/
`KrxLatestWriteOutcome`(A2-3)는 별개 legacy-path 계약 — 본 atomic-path enum과 무관 공존.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from app.atomic_lua import (
    OUTCOME_ADVANCE,
    OUTCOME_CONFLICT,
    OUTCOME_INVALID_SCHEMA,
    OUTCOME_MIGRATION_REQUIRED,
    OUTCOME_REFRESHED_EQUAL,
    OUTCOME_SKIPPED_NEWER,
)
from app.atomic_revision import Revision


class WriteState(enum.Enum):
    """§14 writer 5-state. **enum identity로만 비교**(.value 문자열은 legacy enum 'failed'와 겹침 — L7)."""

    ADVANCE = "advance"
    REFRESHED_EQUAL = "refreshed_equal"
    SKIPPED_NEWER = "skipped_newer"
    CONFLICT = "conflict"
    FAILED = "failed"


class RedisWritePerformed(enum.Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


class RevisionAdvanced(enum.Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class FailureKind(enum.Enum):
    """failed의 SET-적용 확실성(§14 SET-relative / §16 tri-state)."""

    DEFINITE_NOT_APPLIED = "definite_not_applied"   # client_unavailable / SET 전 writer_exception
    UNCERTAIN_AFTER_SEND = "uncertain_after_send"   # set_exception (reply-lost)


class CandidateDisposition(enum.Enum):
    """§14 candidate 처리 분류 (pure — 실제 publish/retry/alert action은 D)."""

    PUBLISH_CANDIDATE = "publish_candidate"  # advance/refreshed_equal/skipped_newer
    RETRY = "retry"                          # failed(general)/UNKNOWN → 재조회 후 확정
    BLOCK_ALERT = "block_alert"              # conflict / structural(migration_required·invalid_schema)


@dataclass(frozen=True)
class WriteOutcome:
    """§14 WriteOutcome (writer 소유, publish-agnostic). effective_revision은 파생(입력 아님)."""

    state: WriteState
    incoming_revision: Revision
    effective_revision: Optional[Revision]
    redis_write_performed: RedisWritePerformed
    revision_advanced: RevisionAdvanced
    reason: Optional[str] = None
    # failed 중 **structural**(migration_required/invalid_schema/unsupported) 여부 — disposition이
    # reason 문자열이 아니라 이 typed 필드로 BLOCK_ALERT 분기(EVAL-예외 caller reason 주입에 불변, M2).
    structural: bool = False

    @property
    def write_healthy(self) -> bool:
        """state 파생(저장 안 함) — conflict/failed = unhealthy, 그 외 healthy."""
        return self.state not in (WriteState.CONFLICT, WriteState.FAILED)


@dataclass(frozen=True)
class PendingCandidate:
    """§14 PendingCandidate (committed 변경마다 생성 — fast-path hint, correctness 유일 근거 아님)."""

    source: str
    asset: str
    desired_revision: Revision               # committed DB revision (복구 anchor) = incoming
    observed_effective_revision: Optional[Revision]
    write_state: WriteState


def write_outcome_from_lua(
    lua_outcome: Optional[str],
    incoming_revision: Revision,
    *,
    failure_kind: Optional[FailureKind] = None,
    reason: Optional[str] = None,
) -> WriteOutcome:
    """A3-2 Lua compare/write outcome(또는 None=EVAL 예외) → WriteOutcome (§14 5-state↔2축 결정적 파생).

    effective_revision은 **함수가 파생**(advance/refreshed_equal/conflict=incoming, 그 외 None) — 입력 안 받음.
    migration_required/invalid_schema = WriteState.failed + structural reason(§14 5-state 고정, §17 block).
    인식 못 한 non-None lua_outcome = fail-closed(unsupported_lua_status).
    """
    if lua_outcome == OUTCOME_ADVANCE:
        return WriteOutcome(WriteState.ADVANCE, incoming_revision, incoming_revision,
                            RedisWritePerformed.APPLIED, RevisionAdvanced.YES)
    if lua_outcome == OUTCOME_REFRESHED_EQUAL:
        return WriteOutcome(WriteState.REFRESHED_EQUAL, incoming_revision, incoming_revision,
                            RedisWritePerformed.APPLIED, RevisionAdvanced.NO)
    if lua_outcome == OUTCOME_SKIPPED_NEWER:
        return WriteOutcome(WriteState.SKIPPED_NEWER, incoming_revision, None,
                            RedisWritePerformed.NOT_APPLIED, RevisionAdvanced.NO)
    if lua_outcome == OUTCOME_CONFLICT:
        # same revision, different rate → current rev == incoming rev (effective=incoming), no SET
        return WriteOutcome(WriteState.CONFLICT, incoming_revision, incoming_revision,
                            RedisWritePerformed.NOT_APPLIED, RevisionAdvanced.NO, reason=reason or "conflict")
    if lua_outcome == OUTCOME_MIGRATION_REQUIRED:
        # §17 v1 재출현 → structural (block+alert, retry 아님). reason은 OUTCOME_* DRY.
        return WriteOutcome(WriteState.FAILED, incoming_revision, None,
                            RedisWritePerformed.NOT_APPLIED, RevisionAdvanced.NO,
                            reason=OUTCOME_MIGRATION_REQUIRED, structural=True)
    if lua_outcome == OUTCOME_INVALID_SCHEMA:
        return WriteOutcome(WriteState.FAILED, incoming_revision, None,
                            RedisWritePerformed.NOT_APPLIED, RevisionAdvanced.NO,
                            reason=OUTCOME_INVALID_SCHEMA, structural=True)
    if lua_outcome is None:
        # EVAL 예외 — caller가 SET 적용 확실성(failure_kind)을 **반드시** 분류(pre/post-SET 위치는
        # caller만 앎). 누락 시 fail-closed — 낙관적 NOT_APPLIED 기본 금지(codex: set_exception을 pre-SET로
        # 오분류 차단). structural=False(general failure, caller reason 무관 → disposition RETRY).
        # failure_kind ∈ {UNCERTAIN_AFTER_SEND, DEFINITE_NOT_APPLIED} 필수 — None/invalid(문자열 등)
        # 모두 fail-closed(else 흡수로 UNKNOWN→NOT_APPLIED 오분류 차단, codex final-gate).
        if failure_kind == FailureKind.UNCERTAIN_AFTER_SEND:
            return WriteOutcome(WriteState.FAILED, incoming_revision, None,
                                RedisWritePerformed.UNKNOWN, RevisionAdvanced.UNKNOWN,
                                reason=reason or "uncertain_after_send", structural=False)
        if failure_kind == FailureKind.DEFINITE_NOT_APPLIED:
            return WriteOutcome(WriteState.FAILED, incoming_revision, None,
                                RedisWritePerformed.NOT_APPLIED, RevisionAdvanced.NO,
                                reason=reason or "pre_set_failure", structural=False)
        raise ValueError(
            "write_outcome_from_lua: lua_outcome=None(EVAL 예외)는 failure_kind ∈ "
            f"{{DEFINITE_NOT_APPLIED, UNCERTAIN_AFTER_SEND}} 필수 — got {failure_kind!r}"
        )
    # 인식 못 한 lua_outcome → fail-closed structural (예상 밖 status는 block+alert)
    return WriteOutcome(WriteState.FAILED, incoming_revision, None,
                        RedisWritePerformed.NOT_APPLIED, RevisionAdvanced.NO,
                        reason=f"unsupported_lua_status:{lua_outcome}", structural=True)


def pending_candidate_from_outcome(source: str, asset: str, outcome: WriteOutcome) -> PendingCandidate:
    """WriteOutcome → PendingCandidate. desired_revision = outcome.incoming_revision(committed) 파생.

    §14 "모든 committed 변경에서 생성" = **DB commit** 기준(candidate가 committed DB revision 운반) —
    Redis SET 성공 여부 아님. 따라서 conflict/failed(SET 미적용) outcome도 candidate 생성 정당(§14
    처리: conflict→block+alert, failed→retry — D가 write_state로 분기). state별 가드 두지 않음.
    """
    return PendingCandidate(
        source=source,
        asset=asset,
        desired_revision=outcome.incoming_revision,
        observed_effective_revision=outcome.effective_revision,
        write_state=outcome.state,
    )


def candidate_disposition(outcome: WriteOutcome) -> CandidateDisposition:
    """§14 처리 규칙 분류 (pure). advance/refreshed_equal/skipped_newer→candidate /
    conflict·structural failed(migration_required·invalid_schema·unsupported)→block+alert /
    general failed(EVAL 예외)→retry. structural 판정은 **typed outcome.structural**(reason 문자열 아님,
    EVAL-예외 caller reason 주입에 불변)."""
    if outcome.state in (WriteState.ADVANCE, WriteState.REFRESHED_EQUAL, WriteState.SKIPPED_NEWER):
        return CandidateDisposition.PUBLISH_CANDIDATE
    if outcome.state == WriteState.CONFLICT:
        return CandidateDisposition.BLOCK_ALERT
    # FAILED — structural(typed) → block+alert / general → retry
    return CandidateDisposition.BLOCK_ALERT if outcome.structural else CandidateDisposition.RETRY
