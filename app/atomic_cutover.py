"""P1b A5 — cutover state + publisher gate **skeleton** (§15, pure, dormant).

§15 cutover state machine의 **순수 골격만** (codex plan-review scope A 확정):
- `CutoverState` enum + `is_allowed_transition` (planned 전이 validation, **durable mutation 0**).
- `publisher_gate_disposition(cutover_state)` (**publish_state(CutoverState) 기반** → pass-through /
  would-block **dry-run**). writer gate(A2 write_mode)와 **별개**(§15 "별 token-pool" — gate/drain
  패턴만 공유, 상태 소스 아님; codex+Workflow reconcile). atomic_ready=open, atomic_blocked=closed.

**dormant — A5는 다음을 절대 하지 않음** (전부 C6 activation):
- live publisher hook(`main.py` `safe_publish_all_fx_snapshots`) retrofit (실제 gate 배선)
- durable control DB schema(§15 global/per-asset) / 6-step CAS 전이 / drain / timeout / token-pool
- B-layer(BuildResult/watermark/publish_sequence/retry/candidate sink) 접촉

즉 A5 산출물은 "planned cutover state" + "dry-run would-block disposition"일 뿐, 어떤 런타임 동작도
바꾸지 않는다(behavior-change-0). 실제 activation authority는 C6.
"""
from __future__ import annotations

import enum


class CutoverState(enum.Enum):
    """§15 cutover state (write_mode × publish_state). planned state — durable 저장은 C6."""

    LEGACY_READY = "legacy_ready"
    HALT_BLOCKED = "halt_blocked"
    ATOMIC_BLOCKED = "atomic_blocked"
    ATOMIC_READY = "atomic_ready"


# §15 forward 전이(quiesce 제외): halt_blocked→atomic_blocked(전이 2) / atomic_blocked→atomic_ready
# (**전이 5** 최종 단일 tx, publish_state=ready). 전이 4(verified)는 atomic/blocked 내부 durable
# status marker라 write_mode×publish_state state 전이 아님(self). legacy_ready→halt_blocked(전이 1)은
# 아래 quiesce 규칙이 커버.
_FORWARD_TRANSITIONS = frozenset({
    (CutoverState.HALT_BLOCKED, CutoverState.ATOMIC_BLOCKED),
    (CutoverState.ATOMIC_BLOCKED, CutoverState.ATOMIC_READY),
})


def is_allowed_transition(from_state: CutoverState, to_state: CutoverState) -> bool:
    """planned cutover 전이 허용 여부 (pure — 실제 전이/CAS/mutation 없음, validation only).

    §15: legacy_ready→halt_blocked→atomic_blocked→atomic_ready(forward) + **any→halt_blocked = quiesce/
    incident**(line 213). self(from==to)는 전이 아님(reconciliation 같은 내부 활동은 state 불변) → False.
    그 외(skip/backward, 예: legacy_ready→atomic_blocked, atomic_ready→atomic_blocked)는 False.
    """
    if not isinstance(from_state, CutoverState) or not isinstance(to_state, CutoverState):
        raise TypeError("is_allowed_transition: CutoverState 인자 필요")
    if from_state == to_state:
        return False
    if to_state == CutoverState.HALT_BLOCKED:
        return True  # any non-halt → halt (quiesce/incident, §15 line 213)
    return (from_state, to_state) in _FORWARD_TRANSITIONS


class PublisherGateDisposition(enum.Enum):
    """A5 publisher gate **dry-run** disposition — 실제 차단/drain/token-pool 없음(C6)."""

    PASS_THROUGH = "pass_through"                  # gate OPEN — publisher 정상 진행
    WOULD_BLOCK_DRY_RUN = "would_block_dry_run"    # gate CLOSED — dry-run(실제 차단 X)


# §15 publisher gate OPEN인 publish_state: LEGACY_READY(cutover 전 정상 발행) /
# ATOMIC_READY(전이 5 후 publish_state=ready). HALT_BLOCKED·ATOMIC_BLOCKED(cutover 중)은 CLOSED.
_GATE_OPEN_STATES = frozenset({CutoverState.LEGACY_READY, CutoverState.ATOMIC_READY})


def publisher_gate_disposition(cutover_state: CutoverState) -> PublisherGateDisposition:
    """§15 publisher gate disposition (**publish_state(CutoverState) 기반, dry-run skeleton**).

    OPEN(PASS_THROUGH): LEGACY_READY / ATOMIC_READY. CLOSED(WOULD_BLOCK_DRY_RUN): HALT_BLOCKED /
    ATOMIC_BLOCKED. **writer gate(A2 write_mode)와 별개** — writer ATOMIC이라고 publisher gate가
    열린 게 아님(§15 별 token-pool; atomic/blocked는 writer atomic + publisher CLOSED). **enforcement/
    drain/token-pool 없음** — 실제 차단·retrofit·durable publish_state는 C6. dormant(live caller 0,
    cutover_state는 호출자[C6]가 durable publish_state로 제공).
    """
    if not isinstance(cutover_state, CutoverState):
        raise TypeError("publisher_gate_disposition: CutoverState 인자 필요")
    if cutover_state in _GATE_OPEN_STATES:
        return PublisherGateDisposition.PASS_THROUGH
    return PublisherGateDisposition.WOULD_BLOCK_DRY_RUN
