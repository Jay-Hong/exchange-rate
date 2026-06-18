"""P1b B2b-4a — coordinator pure helpers (§19 B2b-4 / PR_D §5.4·§12.3, dormant pure).

B2b reconciliation coordinator의 **pure helper 계층**(B2b-4a). enum + placeholder→real materialization +
relation→write-action 매핑. **no async / no I/O** — async coordinator shell(per-asset asyncio.Lock + injected
I/O hooks + orchestration order)과 PublishResult/marker 처리 흐름은 **B2b-4b**.

- `PublishOutcome`: coordinator terminal outcome (post-send + watermark-write tier). PR_D §5.4 raw outcome
  정렬(no_subscribers / all_send_failed / disabled / exception 분리 — telemetry·retry 판단 흐리지 않게).
  pre-send 결정(B2bDecision: BLOCK/RETRY/SKIP_*)은 여기 없음 — B2b-4b가 B2bDecision pass-through.
- `WriteAction`: B1 `classify_watermark_relation` 결과 → watermark write 결정(monotonic CAS) pure 매핑.
  unconditional SET 아님(§12.3:219) — OLDER skip / DIVERGENT alert / LINEAGE_MISMATCH defer.
- `materialize_watermark`: B2b-3 placeholder candidate(lineage=CANDIDATE_PLACEHOLDER_*)를 실제 발급값으로
  교체하는 **유일 통로**. input placeholder 확인 + output sentinel 차단(placeholder 누출 방지).

**dormant**: live caller 0 (B2b-4b/C6 wiring 후속). atomic_watermark(B1)/atomic_reconcile(B2b-3, island)
import → dormant island 합류. **4a는 async/IO 없음**(pure) — live redis/dispatcher/publisher import 0.
"""
from __future__ import annotations

import enum

from app.atomic_reconcile import (
    CANDIDATE_PLACEHOLDER_LINEAGE,
    CANDIDATE_PLACEHOLDER_SENT_AT,
)
from app.atomic_watermark import Watermark, WatermarkRelation


class PublishOutcome(enum.Enum):
    """coordinator terminal outcome (post-send + watermark-write tier). PR_D §5.4 정렬. plain Enum(A4 패턴).

    pre-send 결정(B2bDecision: PUBLISH/SKIP_DEDUP_IDENTICAL/SKIP_SUBSCRIBER_ZERO/BLOCK/RETRY)은 여기 없음 —
    B2b-4b가 B2bDecision pass-through. 아래는 PUBLISH 진입 후(send/write) + gate/FF 결과.
    """
    COMMITTED = "committed"                                   # send>0 + watermark write 성공
    NO_SUBSCRIBERS = "no_subscribers"                         # send 시점 구독자 0 (§5.4, post-send 관측)
    ALL_SEND_FAILED = "all_send_failed"                       # 구독자 있으나 전 send 실패 (§5.4)
    SEND_EXCEPTION = "send_exception"                         # send 호출 예외 (§5.4, stage=publish)
    DISABLED = "disabled"                                     # FF off (두 flag 상태는 result payload에, §5.4/§12.3:221)
    WATERMARK_WRITE_FAILED = "watermark_write_failed"         # send>0 + watermark write 실패 (sent_but_uncommitted)
    WATERMARK_STALE = "watermark_stale"                       # classify OLDER — 직전 더 새 watermark, write skip 정상
    WATERMARK_DIVERGENT_ALERT = "watermark_divergent_alert"   # SAME_SEQ_CONTENT_DIVERGENT — invariant 위반 alert
    WATERMARK_LINEAGE_MISMATCH = "watermark_lineage_mismatch" # LINEAGE_MISMATCH — arbitration defer(C6)


class WriteAction(enum.Enum):
    """B1 WatermarkRelation → watermark write 결정 (monotonic CAS). plain Enum."""
    WRITE = "write"                                          # NEWER / NO_CURRENT
    SKIP_STALE = "skip_stale"                                # OLDER (concurrent 더 새 watermark 도착)
    SKIP_IDEMPOTENT = "skip_idempotent"                      # SAME (동일 발행 재확인)
    ALERT_DIVERGENT = "alert_divergent"                      # SAME_SEQ_CONTENT_DIVERGENT (coordinator invariant 위반)
    DEFER_LINEAGE_ARBITRATION = "defer_lineage_arbitration"  # LINEAGE_MISMATCH (control-plane 대조 = C6)


_RELATION_TO_WRITE_ACTION = {
    WatermarkRelation.NO_CURRENT: WriteAction.WRITE,
    WatermarkRelation.NEWER: WriteAction.WRITE,
    WatermarkRelation.OLDER: WriteAction.SKIP_STALE,
    WatermarkRelation.SAME: WriteAction.SKIP_IDEMPOTENT,
    WatermarkRelation.SAME_SEQ_CONTENT_DIVERGENT: WriteAction.ALERT_DIVERGENT,
    WatermarkRelation.LINEAGE_MISMATCH: WriteAction.DEFER_LINEAGE_ARBITRATION,
}


def write_action_for_relation(relation: WatermarkRelation) -> WriteAction:
    """B1 classify_watermark_relation 결과 → WriteAction (pure, exhaustive).

    WatermarkRelation에 새 멤버가 추가되면 미매핑 → ValueError(exhaustive 잠금; test가 enum 전수 매핑 검증).
    """
    try:
        return _RELATION_TO_WRITE_ACTION[relation]
    except KeyError:
        raise ValueError(f"write_action_for_relation: 미매핑 WatermarkRelation — {relation!r}")


def materialize_watermark(
    candidate: Watermark, *, lineage_id: str, publish_sequence: int, sent_at: str
) -> Watermark:
    """B2b-3 placeholder candidate → 실제 발급값 Watermark (placeholder→real **유일 통로**).

    content(present_revision_vector/missing_sources/membership_version) 보존 + lineage_id/publish_sequence/
    sent_at만 실제 발급값으로 교체.

    **input placeholder 확인**: candidate가 B2b-3 placeholder shape(lineage/sent_at==sentinel)이어야 —
    이미 real이면 misuse(ValueError). **output sentinel 차단**: real lineage_id/sent_at가 sentinel이면 거부
    (placeholder 누출 방지). publish_sequence는 placeholder=0이 valid int라 값으로 검증 불가 → lineage+sent_at로.

    Raises: ValueError — input이 placeholder 아님 / real lineage_id·sent_at가 sentinel.
    """
    if (candidate.lineage_id != CANDIDATE_PLACEHOLDER_LINEAGE
            or candidate.sent_at != CANDIDATE_PLACEHOLDER_SENT_AT):
        raise ValueError(
            "materialize_watermark: input이 B2b-3 placeholder candidate가 아님 "
            f"(lineage={candidate.lineage_id!r}, sent_at={candidate.sent_at!r})"
        )
    if lineage_id == CANDIDATE_PLACEHOLDER_LINEAGE:
        raise ValueError("materialize_watermark: real lineage_id가 sentinel — placeholder 누출")
    if sent_at == CANDIDATE_PLACEHOLDER_SENT_AT:
        raise ValueError("materialize_watermark: real sent_at가 sentinel — placeholder 누출")
    return Watermark(
        asset=candidate.asset,
        lineage_id=lineage_id,
        publish_sequence=publish_sequence,
        membership_version=candidate.membership_version,
        present_revision_vector=dict(candidate.present_revision_vector),
        missing_sources=candidate.missing_sources,
        sent_at=sent_at,
    )
