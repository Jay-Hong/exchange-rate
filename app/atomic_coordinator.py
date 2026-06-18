"""P1b B2b-4a + B2b-4b — coordinator pure helpers + async shell (§19 B2b-4 / PR_D §5.4·§12.3, dormant).

B2b reconciliation coordinator. **4a = pure helper 계층**(enum + placeholder→real materialization +
relation→write-action 매핑, no async/IO). **4b = async coordinator shell**(per-asset asyncio.Lock + injected
I/O hooks + orchestration order ⓪~⑦ + PublishResult + sent_but_uncommitted marker 처리). 둘 다 dormant.

4a (pure):
- `PublishOutcome`: coordinator terminal outcome. PR_D §5.4 raw outcome 정렬(no_subscribers / all_send_failed /
  disabled / exception 분리 — telemetry·retry 판단 흐리지 않게). gate/FF/marker early-exit도 여기(GATE_WOULD_BLOCK
  / DISABLED / WATERMARK_*). pre-send 결정(B2bDecision: BLOCK/RETRY/SKIP_*)은 여기 없음 — 4b가 pass-through.
- `WriteAction`: B1 `classify_watermark_relation` 결과 → watermark write 결정(monotonic CAS) pure 매핑.
  unconditional SET 아님(§12.3:219) — OLDER skip / DIVERGENT alert / LINEAGE_MISMATCH defer.
- `materialize_watermark`: B2b-3 placeholder candidate(lineage=CANDIDATE_PLACEHOLDER_*)를 실제 발급값으로
  교체하는 **유일 통로**. input placeholder 확인 + output sentinel 차단(placeholder 누출 방지).

4b (async shell):
- `CoordinatorHooks`: ~13 injected I/O hooks (C6 wiring vs dormant-fake). 비독립 — decide_and_build_next
  eager exact-key precondition이 call 순서를 묶음. **same-read precondition**(effective_resolver는 payload
  만든 같은 read의 effective-only vector, keys==present_sources, §15:215 닫힘).
- `PublishResult`: tagged XOR (B2bDecision[pre-send relay] XOR PublishOutcome[early-exit/post-send]) + payload.
- `AtomicFxCoordinator`: per-asset asyncio.Lock(공유 pool, **worker==1 HARD precondition** — asyncio.Lock은
  process-local. Dockerfile --workers 1 + 단일 event loop). lock은 critical section 전체(build→send→sent_count
  →seq→materialize→classify→write, §19:346) 보유. 단일 in-lock current read가 **seq floor + classify** 둘 다
  공급(TOCTOU 0). seq = lineage-scoped, floor = max(seq_source.next, current.seq+1) + observe_assigned
  feedback(eviction-alive regression 방지, §19:337-338). marker = real Watermark in-memory non-durable
  (restart drop, ≤1, §12.3:218) — HIT 시 gate/FF/decide 우회 watermark-only retry.

**dormant**: live caller 0 (C6 wiring 후속). 모든 I/O는 injected hook(테스트는 fake). atomic_* island(B1/
B2a/B2b-1/B2b-3/A4/A5) import만 — **live redis/dispatcher/publisher 직접 import·호출 0**(positive AST
trip-wire가 4b 자체 purity 강제 — skip-list는 island 멤버를 skip해 coordinator 자기 import를 못 잡는 blind spot).
"""
from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Mapping, Optional, Protocol, Tuple

from app.atomic_build import FX_TOPIC_ASSETS, BuildResult
from app.atomic_cutover import PublisherGateDisposition
from app.atomic_reconcile import (
    CANDIDATE_PLACEHOLDER_LINEAGE,
    CANDIDATE_PLACEHOLDER_SENT_AT,
    B2bDecision,
    DecisionAction,
    PendingReason,
    decide_and_build_next,
    derive_pending,
)
from app.atomic_revision import Revision
from app.atomic_watermark import (
    Watermark,
    WatermarkRelation,
    classify_watermark_relation,
)
from app.atomic_write_outcome import WriteOutcome


class PublishOutcome(enum.Enum):
    """coordinator terminal outcome (post-send + watermark-write tier). PR_D §5.4 정렬. plain Enum(A4 패턴).

    pre-send 결정(B2bDecision: PUBLISH/SKIP_DEDUP_IDENTICAL/SKIP_SUBSCRIBER_ZERO/BLOCK/RETRY)은 여기 없음 —
    B2b-4b가 B2bDecision pass-through. 아래는 PUBLISH 진입 후(send/write) + gate/FF 결과.
    """
    COMMITTED = "committed"                                   # send>0 + watermark write 성공
    NO_SUBSCRIBERS = "no_subscribers"                         # send 시점 구독자 0 (§5.4, post-send 관측)
    ALL_SEND_FAILED = "all_send_failed"                       # 구독자 있으나 전 send 실패 (§5.4)
    SEND_EXCEPTION = "send_exception"                         # send 호출 예외 (§5.4, stage=publish)
    GATE_WOULD_BLOCK = "gate_would_block"                     # A5 publisher gate CLOSED (dry-run) — send 전 skip
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


# ────────────────────────────── B2b-4b: async coordinator shell ──────────────────────────────
#
# per-asset asyncio.Lock + injected I/O hooks + orchestration order ⓪~⑦ + PublishResult + marker.
# **dormant**: live caller 0. 모든 I/O는 hook 경유(테스트 fake). live redis/dispatcher/publisher 직접 접촉 0.


class SendDisposition(enum.Enum):
    """publisher_fn 전송 결과 분류 (§5.5 — live publish_topic[int]/_publish_fx_snapshot[bool]은 합쳐지므로
    C6 publisher가 richer 반환. coordinator는 합성 X, 분류만 forward)."""
    SENT = "sent"                      # sent_count > 0 (완료 경로 진입)
    NO_SUBSCRIBERS = "no_subscribers"  # 구독자 0 (send 0)
    ALL_FAILED = "all_failed"          # 구독자 있으나 전 send 실패
    EXCEPTION = "exception"            # send 호출 예외


@dataclass(frozen=True)
class SendResult:
    """publisher_fn 반환. SENT ⟺ sent_count>0 invariant."""
    disposition: SendDisposition
    sent_count: int

    def __post_init__(self) -> None:
        if isinstance(self.sent_count, bool) or not isinstance(self.sent_count, int) or self.sent_count < 0:
            raise ValueError(f"SendResult.sent_count: non-negative int — got {self.sent_count!r}")
        if (self.disposition is SendDisposition.SENT) != (self.sent_count > 0):
            raise ValueError(
                f"SendResult: SENT ⟺ sent_count>0 위반 (disposition={self.disposition}, count={self.sent_count})"
            )


class SeqSource(Protocol):
    """lineage-scoped publish_sequence 단조 발급원 (coordinator in-memory, eviction-alive 연속성, §19:337-338).

    coordinator가 floor(=max(next, current.seq+1))를 계산하므로 next는 단순 단조 제안값. observe_assigned로
    실제 발급값을 feed back해 Redis watermark eviction(process 생존) 후에도 regression 방지.
    """
    def next(self, asset: str, lineage_id: str) -> int: ...
    def observe_assigned(self, asset: str, lineage_id: str, assigned: int) -> None: ...


@dataclass(frozen=True)
class CoordinatorHooks:
    """B2b-4b가 주입받는 I/O/상태 hook 묶음 (~13). C6가 live adapter로, 테스트가 fake로 공급.

    **hooks 비독립**: decide_and_build_next의 eager exact-key precondition(effective/write_outcomes keys ==
    build.present_sources)이 call 순서를 묶음. **same-read precondition**: effective_resolver는 build_fn이
    만든 payload와 **같은 Redis read**의 effective-only vector(keys==present_sources, §15:215)를 반환해야 —
    별 read면 watermark가 실제 전송 vector와 divergence(PR_D:58/70). db_revisions_reader/build_fn은 두 read이라
    snapshot 순서 = db_revisions 먼저(③ before ④) → pending은 build 시점 이전-or-동등 snapshot(보수적 telemetry).
    """
    watermark_reader: Callable[[str], Awaitable[Optional[Watermark]]]
    watermark_writer: Callable[[str, Watermark], Awaitable[bool]]
    gate_fn: Callable[[str], Awaitable[PublisherGateDisposition]]
    feature_flags: Callable[[str], Awaitable[Tuple[bool, bool]]]
    db_revisions_reader: Callable[[str], Awaitable[Mapping[str, Revision]]]
    build_fn: Callable[[str], Awaitable[BuildResult]]
    effective_resolver: Callable[[str, BuildResult], Awaitable[Mapping[str, str]]]
    write_outcomes_provider: Callable[[str, BuildResult], Awaitable[Mapping[str, WriteOutcome]]]
    subscriber_counter: Callable[[str], Awaitable[int]]
    publisher_fn: Callable[[str, dict], Awaitable[SendResult]]
    seq_source: SeqSource
    lineage_provider: Callable[[str, Optional[Watermark], BuildResult], Awaitable[str]]
    clock: Callable[[], str]


@dataclass(frozen=True)
class PublishResult:
    """coordinator publish_asset 결과. **tagged XOR**: decision(pre-send B2bDecision relay) XOR outcome
    (early-exit/post-send PublishOutcome) — 정확히 하나(§5.4/§12.3 두 vocabulary 분리 보존).

    payload(telemetry): feature_flags(DISABLED 두-flag, §5.4) / relation·write_action(watermark 결정) /
    send_result / watermark(materialized 또는 recovered marker) / pending(③ detection, lane routing은 C6/B3) /
    gate_disposition / recovered_from_marker·send_performed(marker recovery) / unexpected_same_on_normal_path
    (codex non-blocking: SAME이 비-marker post-send에 나오면 alert 아닌 unexpected-but-idempotent telemetry).
    """
    asset: str
    decision: Optional[B2bDecision] = None
    outcome: Optional[PublishOutcome] = None
    feature_flags: Optional[Tuple[bool, bool]] = None
    gate_disposition: Optional[PublisherGateDisposition] = None
    relation: Optional[WatermarkRelation] = None
    write_action: Optional[WriteAction] = None
    send_result: Optional[SendResult] = None
    watermark: Optional[Watermark] = None
    pending: Optional[Mapping[str, PendingReason]] = None
    recovered_from_marker: bool = False
    send_performed: bool = False
    unexpected_same_on_normal_path: bool = False
    send_error: Optional[str] = None   # publisher_fn raise repr (SEND_EXCEPTION 시) — 관측용, swallow 아님

    def __post_init__(self) -> None:
        if (self.decision is None) == (self.outcome is None):
            raise ValueError("PublishResult: decision XOR outcome — 정확히 하나여야")


class AtomicFxCoordinator:
    """per-asset 직렬화 FX topic publish coordinator (B2b-4b shell, dormant).

    **동시성 모델 = 단일 coordinator 직렬화**(§12.3:219, multi-process CAS 아님). worker==1 HARD precondition
    (asyncio.Lock은 process-local — Dockerfile --workers 1 + 단일 event loop가 두 진입 경로[main batch hook /
    C1 direct create_task]를 같은 인스턴스·같은 lock으로 직렬화). lock은 critical section 전체 보유.

    seq floor + observe_assigned + lineage-scoping으로 같은 lineage 정상 경로 classify는 NEWER/NO_CURRENT만
    도달. OLDER/SAME/DIVERGENT는 4a write_action_for_relation 그대로 적용(SAME=marker recovery 멱등 정당 /
    OLDER=stale skip / DIVERGENT=invariant alert) — coordinator는 unreachability 특수처리 안 함, forward만.
    """

    def __init__(self, hooks: CoordinatorHooks) -> None:
        self._hooks = hooks
        self._locks: Dict[str, asyncio.Lock] = {}
        # sent_but_uncommitted marker: send 성공 + watermark write 실패 시 real Watermark 보관(in-memory,
        # non-durable — restart drop, 재시작당 ≤1, §12.3:218). HIT 시 watermark-only retry(재전송 X).
        self._markers: Dict[str, Watermark] = {}

    def _lock_for(self, asset: str) -> asyncio.Lock:
        # 단일 event loop라 get→set 사이 await 없음 = atomic(동시 생성 race 없음).
        lock = self._locks.get(asset)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[asset] = lock
        return lock

    async def publish_asset(self, asset: str) -> PublishResult:
        """asset 1건 publish 시도 (per-asset lock 직렬화). 진입점."""
        if asset not in FX_TOPIC_ASSETS:
            raise ValueError(f"publish_asset: asset '{asset}' not in {FX_TOPIC_ASSETS}")
        async with self._lock_for(asset):
            return await self._publish_locked(asset)

    async def _publish_locked(self, asset: str) -> PublishResult:
        # ⓪ sent_but_uncommitted marker short-circuit — send 이미 발생, watermark-only retry. gate/FF/decide 우회
        #    (FF-off가 watermark를 strand하면 §12.3:218 ≤1 bound 위반).
        marker = self._markers.get(asset)
        if marker is not None:
            return await self._recover_marker(asset, marker)

        # ① A5 publisher gate (dry-run skeleton; enforcement source는 C6 durable publish_state)
        gate = await self._hooks.gate_fn(asset)
        _validate_gate(gate)  # malformed return은 fail-closed(ValueError) — FF 대칭, block을 fail-open으로 우회 차단
        if gate is PublisherGateDisposition.WOULD_BLOCK_DRY_RUN:
            return PublishResult(asset=asset, outcome=PublishOutcome.GATE_WOULD_BLOCK, gate_disposition=gate)

        # ② FF disabled — watermark 미갱신, pending은 inaction으로 보존(derive_pending 미참조). 두 flag 보존.
        flags = await self._hooks.feature_flags(asset)
        _validate_flags(flags)
        if not (flags[0] and flags[1]):
            return PublishResult(asset=asset, outcome=PublishOutcome.DISABLED, feature_flags=flags)

        # 단일 in-lock current read — derive_pending / decide last_watermark / seq floor / classify 모두 공급.
        current = await self._hooks.watermark_reader(asset)

        # ③ derive_pending (detection; snapshot 순서 = build 이전). lane routing(backstop/startup/subscriber/
        #    FF-re-enable)은 C6/B3 — dormant 4b는 candidate fast-path만, pending은 telemetry로 첨부.
        db_revisions = await self._hooks.db_revisions_reader(asset)
        pending = derive_pending(db_revisions, current)

        # ④ build + effective(same read) + write_outcomes + subscriber (decide eager precondition 충족 순서)
        build_result = await self._hooks.build_fn(asset)
        effective = await self._hooks.effective_resolver(asset, build_result)
        write_outcomes = await self._hooks.write_outcomes_provider(asset, build_result)
        subscriber_count = await self._hooks.subscriber_counter(asset)

        # ⑤ decide (pure, B2b-3)
        decision = decide_and_build_next(current, build_result, effective, write_outcomes, subscriber_count)

        # ⑥ pre-send relay — PUBLISH 외(SKIP_DEDUP_IDENTICAL/SKIP_SUBSCRIBER_ZERO/BLOCK/RETRY)는 B2bDecision 그대로.
        #    SKIP_DEDUP_IDENTICAL은 watermark byte-identical(no refresh/seq/write, R2).
        if decision.action is not DecisionAction.PUBLISH:
            return PublishResult(asset=asset, decision=decision, pending=pending)

        # ⑦ PUBLISH — send → (sent>0) seq 발급 → materialize → classify → write
        return await self._send_and_commit(asset, current, decision, build_result, pending)

    async def _send_and_commit(
        self,
        asset: str,
        current: Optional[Watermark],
        decision: B2bDecision,
        build_result: BuildResult,
        pending: Mapping[str, PendingReason],
    ) -> PublishResult:
        candidate = decision.watermark_candidate
        if candidate is None or build_result.payload is None:
            # decide 계약상 PUBLISH ⟹ candidate 동반 + payload 존재(MALFORMED는 RETRY/BLOCK) — 위반은 호출 버그.
            raise ValueError("_send_and_commit: PUBLISH인데 candidate/payload 부재 — decide 계약 위반")

        # publisher_fn 계약: send-stage 결과를 SendResult로 분류·반환. partial send(sent_count>0)를 알면
        # 반드시 SENT 반환(raise 금지 — unknown partial이 success watermark 미기록되게). raise는 contract slip
        # 으로 보고 SEND_EXCEPTION으로 funnel(state-safe: seq/write 전, marker 미생성). CancelledError(BaseException)는
        # 미포획 — task cancel 전파 보존.
        try:
            send = await self._hooks.publisher_fn(asset, build_result.payload)
        except Exception as e:  # noqa: BLE001 — send raise → SEND_EXCEPTION funnel (관측: send_error)
            return PublishResult(asset=asset, outcome=PublishOutcome.SEND_EXCEPTION,
                                 send_error=repr(e), pending=pending)
        if send.disposition is SendDisposition.NO_SUBSCRIBERS:
            return PublishResult(asset=asset, outcome=PublishOutcome.NO_SUBSCRIBERS, send_result=send, pending=pending)
        if send.disposition is SendDisposition.ALL_FAILED:
            return PublishResult(asset=asset, outcome=PublishOutcome.ALL_SEND_FAILED, send_result=send, pending=pending)
        if send.disposition is SendDisposition.EXCEPTION:
            return PublishResult(asset=asset, outcome=PublishOutcome.SEND_EXCEPTION, send_result=send, pending=pending)

        # completion (sent_count>0): lineage-scoped seq floor + observe_assigned feedback.
        lineage = await self._hooks.lineage_provider(asset, current, build_result)
        proposal = self._hooks.seq_source.next(asset, lineage)
        if current is not None and current.lineage_id == lineage:
            assigned = max(proposal, current.publish_sequence + 1)  # same-lineage floor (regression 방지)
        else:
            assigned = proposal  # 새 lineage(bootstrap)/NO_CURRENT — lineage-scoped, cross-lineage floor 금지
        # observe = 최고 **발급(issued)** seq (written 아님). sent seq는 write skip/defer여도 재사용 금지(eviction-alive
        # 후 SAME_SEQ_CONTENT_DIVERGENT 방지) → write 분기 전 unconditional feed back(§19:337-338).
        self._hooks.seq_source.observe_assigned(asset, lineage, assigned)

        sent_at = self._hooks.clock()
        real = materialize_watermark(candidate, lineage_id=lineage, publish_sequence=assigned, sent_at=sent_at)
        relation = classify_watermark_relation(current, real)
        action = write_action_for_relation(relation)

        # floor + lineage-scoping 결과 정상 send-path classify는 NEWER(WRITE)/NO_CURRENT(WRITE)/LINEAGE_MISMATCH만
        # 도달. SAME/OLDER/DIVERGENT는 구조적 unreachable(방어) — outcome은 _outcome_for_write_action 공유 매핑.
        write_ok = True
        if action is WriteAction.WRITE:
            write_ok = await self._hooks.watermark_writer(asset, real)
            if not write_ok:
                # send 성공 + write 실패 → sent_but_uncommitted: real watermark marker 보관(재전송 X, watermark-only
                # 재시도, §12.3:218 ≤1). ⚠️ writer가 지속 실패하면 이 asset의 fresh publish가 marker recovery에
                # 막힘(bounded escape/backoff = B3). LINEAGE_MISMATCH는 marker 미생성(J2 — takeover supersede).
                self._markers[asset] = real
        outcome = _outcome_for_write_action(action, write_ok=write_ok)
        return PublishResult(asset=asset, outcome=outcome, send_result=send, watermark=real,
                             relation=relation, write_action=action, send_performed=True, pending=pending,
                             unexpected_same_on_normal_path=(action is WriteAction.SKIP_IDEMPOTENT))

    async def _recover_marker(self, asset: str, marker: Watermark) -> PublishResult:
        """sent_but_uncommitted marker(real watermark) watermark-only retry. 재전송/seq 재발급 없음(§12.3:218).

        marker는 이미 real(lineage/seq/sent_at 실제값)이라 classify 직접 적용. marker lifecycle: WRITE 성공/
        SKIP_IDEMPOTENT(멱등)/SKIP_STALE(superseded)/ALERT_DIVERGENT(invariant)/LINEAGE_MISMATCH(takeover) →
        **clear**(해소). **유일 retain = WRITE 실패**(다음 watermark-only 재시도). LINEAGE_MISMATCH는 J2 — lineage
        takeover가 옛 pending write를 supersede(orphan send = at-least-once 중복), C6는 outcome telemetry로 인지 →
        retain하면 fresh publish가 영구 starvation이라 drop. outcome은 _outcome_for_write_action 공유 매핑.
        """
        current = await self._hooks.watermark_reader(asset)
        relation = classify_watermark_relation(current, marker)
        action = write_action_for_relation(relation)

        write_ok = True
        if action is WriteAction.WRITE:
            write_ok = await self._hooks.watermark_writer(asset, marker)
            if write_ok:
                self._markers.pop(asset, None)
            # else: marker 유지(WRITE 실패만 retain)
        else:
            self._markers.pop(asset, None)  # SKIP_IDEMPOTENT/SKIP_STALE/ALERT_DIVERGENT/DEFER(J2) → clear
        outcome = _outcome_for_write_action(action, write_ok=write_ok)
        return PublishResult(asset=asset, outcome=outcome, watermark=marker, relation=relation,
                             write_action=action, recovered_from_marker=True, send_performed=False)


def _validate_flags(flags: Tuple[bool, bool]) -> None:
    """feature_flags hook 반환 형태 검증(2-tuple of bool) — §5.4 두 flag(fx_topic / topic_dispatcher)."""
    if (not isinstance(flags, tuple) or len(flags) != 2
            or not all(isinstance(f, bool) for f in flags)):
        raise ValueError(f"feature_flags: (bool, bool) 2-tuple — got {flags!r}")


def _validate_gate(gate) -> None:
    """gate_fn 반환 형태 검증 — malformed return을 fail-closed(ValueError, crash-early). FF 대칭.

    gate는 block 메커니즘이라 malformed가 silently 통과(fail-open)하면 안 됨. BLOCK으로 coerce하지 않음
    (wiring 버그를 숨김) — raise해서 C6 adapter contract slip을 조기 노출.
    """
    if not isinstance(gate, PublisherGateDisposition):
        raise ValueError(f"gate_fn: PublisherGateDisposition 반환 필요 — got {gate!r}")


def _outcome_for_write_action(action: WriteAction, *, write_ok: bool) -> PublishOutcome:
    """WriteAction → terminal PublishOutcome (send-path / marker-recovery 공유 pure 매핑, exhaustive).

    WRITE는 watermark write 성공 여부로 분기(COMMITTED / WATERMARK_WRITE_FAILED). 나머지는 고정 매핑 —
    side-effect(marker store/clear, send_performed, recovered flag)는 호출자가 처리, 여기선 outcome만.
    write_action_for_relation과 함께 enum 전수 잠금(새 멤버 → ValueError, test가 검증).
    """
    if action is WriteAction.WRITE:
        return PublishOutcome.COMMITTED if write_ok else PublishOutcome.WATERMARK_WRITE_FAILED
    if action is WriteAction.SKIP_IDEMPOTENT:
        return PublishOutcome.COMMITTED
    if action is WriteAction.SKIP_STALE:
        return PublishOutcome.WATERMARK_STALE
    if action is WriteAction.ALERT_DIVERGENT:
        return PublishOutcome.WATERMARK_DIVERGENT_ALERT
    if action is WriteAction.DEFER_LINEAGE_ARBITRATION:
        return PublishOutcome.WATERMARK_LINEAGE_MISMATCH
    raise ValueError(f"_outcome_for_write_action: 미매핑 WriteAction — {action!r}")
