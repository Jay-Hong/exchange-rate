"""P1b B2b-1 + B2b-3 — reconciliation detection + decision (§19 line 344-351, pure dormant).

reconciliation coordinator(B2b)의 **pure 계층 2개**: (B2b-1) `derive_pending` DETECTION + (B2b-3)
`decide_and_build_next` DECISION. 둘 다 pure(I/O 0) — coordinator runtime(B2b-4)/effective resolution/
live routing은 C6. (B2b-3 상세 계약은 decide_and_build_next 위 주석.)

== B2b-1 (derive_pending) ==
**pending DETECTION 계층**. DB latest revisions를 last-success watermark(B1)와 비교해 '어느 source가
미발행 작업을 갖나'만 분류한다.

**계약 (codex B2b-1 tight contract)**:
- 입력: db_revisions(현재 DB latest per source) + last-success watermark + membership(FX_MEMBERSHIP_SOURCES).
- 출력: per-source pending **분류(reason)만**.
- **금지**: publish 결정 / effective vector 생성 / watermark write 판단 / subscriber·send 상태 판단.
  즉 "무엇이 불일치인가"만 답하고, "발행할 것인가/어떻게 복구하나"는 B2b-3/B3, send/subscriber는 B2b-4.

§19 line 347 DERIVED pending: DB-not-in-present→pending / DB-rev>present[src]→pending /
DB-absent+in-missing→not pending / malformed·schema→structural.

**published vector source 아님**: 출력은 'work 있나' 판정이지 watermark.present_revision_vector(effective,
B2b-3/C6 소관)를 만들지 않는다 — B2a 'DB candidate vector ≠ payload(Redis-first) snapshot' 교훈 동일.

**비교 기준**: db_rev(Revision tuple)과 watermark present_revision_vector[src](revision_key string)를
parse_revision_key로 Revision tuple 변환 후 비교(인코딩 single-source = atomic_value_schema). present[src]
파싱 실패 = STRUCTURAL.

**dormant**: live caller 0 (B2b-3/B2b-4/C6 wiring 후속). atomic_watermark/atomic_value_schema(island) import
→ dormant set 합류(dormancy allowlist 추가). pure(I/O 0) — db_revisions는 caller가 읽어 주입.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from app.atomic_build import BuildCompleteness, BuildResult
from app.atomic_revision import Revision
from app.atomic_value_schema import parse_revision_key
from app.atomic_watermark import Watermark, watermark_content_equal
from app.atomic_write_outcome import CandidateDisposition, WriteOutcome, candidate_disposition
from app.fx_membership import FX_MEMBERSHIP_SOURCES


class PendingReason(enum.Enum):
    """per-source pending 분류 (§19:347). action(publish/retry/alert)은 B2b-3/B3. plain Enum(A4 패턴)."""
    PENDING_NOT_IN_PRESENT = "pending_not_in_present"   # DB에 revision 있는데 watermark present 부재 (미발행)
    PENDING_DB_AHEAD = "pending_db_ahead"               # DB-rev > watermark present[src] (마지막 성공 이후 변경)
    NOT_PENDING_CURRENT = "not_pending_current"         # DB-rev <= present[src] (최신 반영됨)
    NOT_PENDING_DB_ABSENT = "not_pending_db_absent"     # DB에 값 없음 → 발행할 것 없음 (DB-absent)
    STRUCTURAL = "structural"                            # watermark present[src] revision_key 파싱 불가


def derive_pending(
    db_revisions: Mapping[str, Revision], watermark: Optional[Watermark]
) -> Dict[str, PendingReason]:
    """DB latest revisions ↔ last-success watermark → per-source pending 분류 (pure detection).

    Args:
        db_revisions: {source: Revision(canonical_epoch_us, id)} — 현재 DB latest. caller가 읽어 주입
            (pure 함수). membership source 중 DB에 값 있는 것만 포함(없으면 키 부재 = DB-absent).
        watermark: last-success Watermark(B1) 또는 None(아직 발행 이력 없음 → present 전부 부재).
    Returns:
        {source: PendingReason} — FX_MEMBERSHIP_SOURCES 전 source. action 판단은 호출자(B2b-3/B3).

    Note: 출력은 detection만. DB-absent는 watermark.missing 포함 여부와 무관하게 not-pending(발행할 DB
    값이 없음 — §19:347 'DB-absent+in-missing→not pending'의 operative 조건은 DB-absent). present > DB
    (watermark가 DB보다 앞섬)은 NOT_PENDING_CURRENT로 흡수(DB가 앞설 때만 pending).
    """
    present = watermark.present_revision_vector if watermark is not None else {}
    result: Dict[str, PendingReason] = {}
    for source in FX_MEMBERSHIP_SOURCES:
        db_rev = db_revisions.get(source)
        if db_rev is None:
            result[source] = PendingReason.NOT_PENDING_DB_ABSENT
            continue
        present_key = present.get(source)
        if present_key is None:
            result[source] = PendingReason.PENDING_NOT_IN_PRESENT
            continue
        try:
            present_rev = parse_revision_key(present_key)
        except ValueError:
            result[source] = PendingReason.STRUCTURAL
            continue
        result[source] = (
            PendingReason.PENDING_DB_AHEAD if db_rev > present_rev
            else PendingReason.NOT_PENDING_CURRENT
        )
    return result


# ── B2b-3: decide_and_build_next (pure decision, §19 B2b / PR_D §5.1·§5.3, dormant) ──
#
# coordinator(B2b)의 **pure decision 계층**. last-success watermark + BuildResult(B2a) + 외부 주입
# effective revision vector + write_outcomes(A4) + subscriber_count로 publish/skip/block/retry를 결정하고,
# PUBLISH 시 dedup-only watermark candidate를 만든다.
#
# **계약 (codex B2b-3 tight contract + Workflow 3-lens reconcile)**:
# - **asset-atomic** (§5.1 line 61 / §5.3 line 86): conflict/structural → BLOCK(전체 asset) / 일반 failed →
#   RETRY(전체) / 전 source publish-candidate일 때만 PUBLISH. **conflict/failed source-drop 후 발행 금지**.
#   partial publish는 build에서 빠진 것(=BuildResult.missing_sources, DB-absent + fallback-fail)만이지
#   conflict/failed 제외가 아님.
# - **금지**: send / sent_count 입력·판단 / Redis·DB I/O / watermark write / publish_sequence 발급 /
#   lock·coordinator runtime / completion(sent>0) 판단. subscriber-zero=pre-send skip(B2b-3) /
#   sent-zero=post-send(B2b-4 gate, B2b-3 무관).
# - **dedup**: watermark_content_equal(last, candidate) — vector+missing+membership 동일하면 재발행 금지(§19:351).

class DecisionAction(enum.Enum):
    """B2b-3 결정 (asset-level). plain Enum(A4 패턴)."""
    PUBLISH = "publish"                          # 발행 (watermark_candidate 동반)
    SKIP_DEDUP_IDENTICAL = "skip_dedup_identical"  # content==last watermark → 재발행 불필요
    SKIP_SUBSCRIBER_ZERO = "skip_subscriber_zero"  # subscriber 0 → 보류(pending 유지, send 안 함)
    BLOCK = "block"                              # conflict/structural → 전체 asset 차단(alert)
    RETRY = "retry"                              # 일반 failed / MALFORMED build → 전체 asset 재시도


# dedup-only candidate의 placeholder (B2b-4가 실제 발급 lineage/seq/sent_at으로 교체 후에만 write).
# ⚠️ 이 placeholder candidate를 classify_watermark_relation/serialize/Redis write에 **넣지 말 것** —
# lineage_id/publish_sequence를 실제 의미로 보는 경로엔 부적합. watermark_content_equal dedup 전용.
_CANDIDATE_PLACEHOLDER_LINEAGE = "__candidate__"
_CANDIDATE_PLACEHOLDER_SEQ = 0
_CANDIDATE_PLACEHOLDER_SENT_AT = "__candidate__"


@dataclass(frozen=True)
class B2bDecision:
    """B2b-3 pure decision 결과.

    watermark_candidate는 **dedup-only**(placeholder lineage/seq/sent_at) — PUBLISH 시 content(vector/
    missing/membership)를 운반하나 lineage_id/publish_sequence/sent_at은 sentinel이라 B2b-4가 실제 발급으로
    **교체 후에만 write**. PUBLISH 외 action은 candidate None. pending 판단은 derive_pending(B2b-1) 소관
    (B2bDecision엔 pending 필드 없음 — decision-only).
    """
    action: DecisionAction
    watermark_candidate: Optional[Watermark]
    reason: str


def decide_and_build_next(
    last_watermark: Optional[Watermark],
    build_result: BuildResult,
    effective_revision_vector: Mapping[str, str],
    write_outcomes: Mapping[str, WriteOutcome],
    subscriber_count: int,
) -> B2bDecision:
    """publish/skip/block/retry 결정 + PUBLISH 시 dedup-only watermark candidate (pure, §19 B2b).

    Args:
        last_watermark: last-success Watermark(B1) 또는 None(첫 발행). build_result.asset과 일치해야.
        build_result: B2a BuildResult (payload/present_sources/missing_sources/membership/completeness).
        effective_revision_vector: {source: revision_key} — **외부 주입 precondition**. §15 line 215의
            skipped_newer effective=None을 상위 wiring(재조회/Lua 다중반환)이 이미 닫은 effective-only vector.
            keys는 build_result.present_sources와 **exact match**여야 함(불일치 = precondition 위반 → ValueError).
        write_outcomes: {source: A4 WriteOutcome} — asset-atomic disposition 집계용(vector 구성용 아님).
            keys는 build_result.present_sources와 **exact match**여야 함(MALFORMED 제외 후, eager) —
            conflict/failed source를 누락하면 BLOCK 우회되므로 source-drop 차단(불일치 → ValueError).
        subscriber_count: pre-send 구독자 수(non-negative int). sent_count 아님(post-send=B2b-4).

    Returns:
        B2bDecision. **금지**: send/watermark write/seq 발급/I/O/completion 판단 없음(전부 B2b-4).

    Raises:
        ValueError: asset mismatch / subscriber_count 음수·non-int / effective_vector keys != present_sources.
    """
    if last_watermark is not None and last_watermark.asset != build_result.asset:
        raise ValueError(
            f"decide_and_build_next: asset mismatch (last={last_watermark.asset!r} vs build={build_result.asset!r})"
        )
    if isinstance(subscriber_count, bool) or not isinstance(subscriber_count, int) or subscriber_count < 0:
        raise ValueError(f"decide_and_build_next: subscriber_count non-negative int — got {subscriber_count!r}")

    dispositions = {src: candidate_disposition(o) for src, o in write_outcomes.items()}
    has_block = any(d is CandidateDisposition.BLOCK_ALERT for d in dispositions.values())

    # ① MALFORMED build(payload None): conflict/structural면 BLOCK(corruption alert 우선), 아니면 RETRY(build
    #    실패). MALFORMED는 present=()라 coverage 적용 불가 → 이 분기에서 raw write_outcomes 기준으로 종결.
    if build_result.completeness is BuildCompleteness.MALFORMED:
        if has_block:
            return B2bDecision(DecisionAction.BLOCK, None,
                               "conflict/structural write outcome (build MALFORMED) — asset block")
        return B2bDecision(DecisionAction.RETRY, None, f"build MALFORMED — {build_result.build_error}")

    # ② coverage precondition (eager, fail-closed, non-MALFORMED) — caller가 published source(present)마다
    #    write_outcome + effective revision을 정확히 제공. **BLOCK 판정 전**이라 extra/dropped source(라우팅
    #    버그·source-drop)는 spurious BLOCK이 아니라 ValueError로 노출(codex). dropped conflict도 여기서 잡힘
    #    (P1). effective도 eager — drift/skipped_newer-None(§15:215) 조기 노출(P2).
    present = set(build_result.present_sources)
    wo_keys = set(write_outcomes)
    if wo_keys != present:
        raise ValueError(
            "decide_and_build_next: write_outcomes keys != build_result.present_sources "
            f"(missing {sorted(present - wo_keys)}, extra {sorted(wo_keys - present)}) — source-drop/extra 차단"
        )
    eff_keys = set(effective_revision_vector)
    if eff_keys != present:
        raise ValueError(
            "decide_and_build_next: effective_revision_vector keys != build_result.present_sources "
            f"(missing {sorted(present - eff_keys)}, extra {sorted(eff_keys - present)}) — "
            "§15:215 effective 미해결/drift 노출"
        )

    # ③ asset-atomic BLOCK: conflict/structural(BLOCK_ALERT) → 전체 asset BLOCK (coverage로 write_outcomes==
    #    present 보장 — 전 published source 기준, source-drop 0. subscriber-zero/dedup보다 우선).
    if has_block:
        return B2bDecision(DecisionAction.BLOCK, None, "conflict/structural write outcome — asset block")

    # ④ 일반 failed(RETRY disposition) → 전체 RETRY (coverage로 write_outcomes==present 보장)
    if any(d is CandidateDisposition.RETRY for d in dispositions.values()):
        return B2bDecision(DecisionAction.RETRY, None, "general failed write outcome — asset retry")

    # ⑤ subscriber 0 → skip + pending 유지 (pre-send, send 안 함)
    if subscriber_count == 0:
        return B2bDecision(DecisionAction.SKIP_SUBSCRIBER_ZERO, None, "subscriber count 0 — pending retained")

    # 여기 도달 = 전 published source가 PUBLISH_CANDIDATE (BLOCK/RETRY 없음, coverage 보장).
    # ⑥ dedup-only candidate (placeholder lineage/seq/sent_at — B2b-4가 실제 발급 교체)
    candidate = Watermark(
        asset=build_result.asset,
        lineage_id=_CANDIDATE_PLACEHOLDER_LINEAGE,
        publish_sequence=_CANDIDATE_PLACEHOLDER_SEQ,
        membership_version=build_result.membership_version,
        present_revision_vector=dict(effective_revision_vector),
        missing_sources=build_result.missing_sources,
        sent_at=_CANDIDATE_PLACEHOLDER_SENT_AT,
    )
    # dedup: content(vector+missing+membership) 동일하면 재발행 불필요 (§19:351). lineage/seq/sent_at 무관.
    if last_watermark is not None and watermark_content_equal(last_watermark, candidate):
        return B2bDecision(DecisionAction.SKIP_DEDUP_IDENTICAL, None, "content identical to last watermark — no republish")

    # ⑥ PUBLISH
    return B2bDecision(DecisionAction.PUBLISH, candidate, "publishable")
