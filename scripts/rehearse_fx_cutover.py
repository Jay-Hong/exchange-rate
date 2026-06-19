#!/usr/bin/env python3
"""P1b C6-9b — FX cutover shadow rehearsal harness (scripts/, read-only, no-op send/write, dormant).

C6-6 AtomicFxCoordinator(atomic_fx_live.FxLiveCoordinatorAdapter)를 **실제 read** 위에서 dry-run하여
asset별 would-decision을 관측하고, legacy _publish_fx_snapshot의 would-action과 대조하는 운영 rehearsal.
C6-9a의 read-only status surface(/admin/api/atomic-cutover-status)를 **실제 coordinator 거동 실증**으로 보완 —
C6-8 activation command가 의존할 go/no-go를 사전 검증.

**structural send/write 0** (flag-conditional 아님 — subclass override라 우회 불가):
- `_RehearsalAdapter._publisher_fn` override: topic_dispatcher.publish_topic_detailed **미호출**. 실제
  subscriber_count로 would-send 분류만(send 가정 성공 — 직렬화/전송실패/eviction 미검증, label).
- `_RehearsalAdapter._watermark_writer` override: store.write **미호출**. would-write 기록 + True 반환
  (coordinator 완주용 — Redis CAS/store failure 미검증, label).
- 나머지 13 hook 중 write/send는 위 둘뿐 — 둘 다 override되어 구조적으로 live mutation 0.
  나머지(watermark_reader/build_fn loader/db_revisions/subscriber/gate/lineage/clock/seq)는 read·pure.

**codex high 의견일치 보강 4건**:
1. (point 6) `_db_revisions_reader` override — `asyncio.to_thread` 우회. 부모는 caller-owned SQLAlchemy
   Session을 worker thread로 넘김(atomic_fx_live.py:39-40 documented precondition). rehearsal은 session을
   event-loop thread에서만 접근해 thread-affinity를 구조적으로 닫음(read-only지만 안전 우선).
2. (point 8) default snapshot_fn=cutover_snapshot은 새 script process에서 `_INITIAL`(미refresh) →
   gate 항상 PASS_THROUGH/lineage `bootstrap:0`. 그래서 `read_cutover_snapshot_fresh(db)`를 **1회 capture**해
   `snapshot_fn=lambda: snap` 주입 — 실제 cutover control plane state 반영(faithful gate).
3. (point 2) no-op publisher는 "send 가정 성공" label — partial send/dispatcher 직렬화/eviction 미검증.
4. (point 8) coordinator outcome vs legacy guard-only action은 **1:1 mismatch 아님** — classify가
   gate_divergence / atomic_defers(synthetic·data-dependent) / match / review로 분리(오판 방지).

**synthetic write_outcomes** (atomic_fx_live._fail_closed_write_outcomes 대체): WriteOutcome은 write-time
(source-writer Lua) 산출이라 publish-time read로 재구성 불가. fail-closed default(전 source RETRY)는 항상
RETRY라 uninformative → rehearsal은 present source를 REFRESHED_EQUAL(PUBLISH_CANDIDATE)로 주입해 write-axis가
인위 block 안 하게. ⚠️ **real conflict/structural BLOCK은 write-time-only라 미검증**(report label).

**dormant 거동**: 운영 dormant phase엔 v2 topic payload가 Redis에 없음(writer mode=legacy) → loader
present=() → build MALFORMED → decide RETRY (fail-safe). 즉 이 rehearsal은 "coordinator가 실데이터로 절대
오발행하지 않음"을 실증하는 go/no-go probe.

**구독자 0 현실 (faithfulness 핵심)**: rehearsal은 별도 script process라 WS 구독자 0(topic_dispatcher.registry는
per-process singleton, WS 연결로만 채워짐). 따라서 실제 run에선 decide가 PUBLISH 전 SKIP_SUBSCRIBER_ZERO로
종결 → **PUBLISH/send 경로 미도달**(no-op publisher도 미호출). COMMITTED/post-send 경로는 테스트에서 registry를
patch했을 때만 도달. rehearsal의 go/no-go 가치 = gate/FF/build/decide 축까지의 안전성 실증(send는 0-sub로 자연
차단). classify는 coordinator outcome 중심으로 분류(legacy 비교는 publish/skip/disabled 축의 보조 sanity).

**dormancy**: scripts/ 배치 → app/ AST trip-wire(atomic_fx_live no-importer / publish_topic_detailed caller
scan은 app/만 순회) 범위 밖 → sanction 0. live caller 추가 아님.

usage:
    python scripts/rehearse_fx_cutover.py --confirm-read-prod
    python scripts/rehearse_fx_cutover.py --confirm-read-prod --force-gate-open   # gate 우회 진단(go signal 아님)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Mapping, Optional

# scripts는 pytest 밖에서도 실행 — repo root importable 보장
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, topic_dispatcher  # noqa: E402
from app.atomic_build import FX_TOPIC_ASSETS, BuildResult  # noqa: E402
from app.atomic_coordinator import (  # noqa: E402
    PublishOutcome,
    PublishResult,
    SendDisposition,
    SendResult,
)
from app.atomic_cutover import CutoverState  # noqa: E402
from app.atomic_cutover_runtime import (  # noqa: E402
    CutoverReadinessSnapshot,
    read_cutover_snapshot_fresh,
)
from app.atomic_fx_live import FxLiveCoordinatorAdapter  # noqa: E402
from app.atomic_reconcile import DecisionAction  # noqa: E402
from app.atomic_revision import Revision  # noqa: E402
from app.atomic_watermark import Watermark  # noqa: E402
from app.atomic_write_control import WriterMode  # noqa: E402
from app.atomic_write_outcome import (  # noqa: E402
    RedisWritePerformed,
    RevisionAdvanced,
    WriteOutcome,
    WriteState,
)
from app.fx_topic_publisher import FX_TOPICS  # noqa: E402

# decide는 candidate를 effective vector로 구성하고 write_outcome.revision은 미사용(atomic_reconcile.py:222) —
# synthetic outcome의 revision은 disposition 산출용 placeholder.
_SYNTHETIC_REVISION: Revision = (1, 0)


# ────────────────────────────── defense-in-depth read-only client ──────────────────────────────
class _ReadOnlyClient:
    """Redis write 계열을 구조적으로 거부하는 wrapper (no-op override 우회 backstop, defense-in-depth).

    **scope**: rehearse()가 adapter에 주입하는 client(= AtomicWatermarkStore write path)의 2nd guard.
    safety 주장('rehearsal write 0')이 _RehearsalAdapter._watermark_writer override 단일 층에만 의존하지
    않게 set/delete/eval/pipeline/execute_command을 raise(read=get/mget 등은 inner 위임). ⚠️ C6-3 loader는
    자체 `_get_sync_client()`를 잡으므로(atomic_fx_v2_loader.py:165) 이 wrapper로 안 감싸짐 — 단 loader는
    GET/SELECT만 하는 read-only 동작이라 무해. 즉 1차 안전 근거는 no-op override + 0-subscriber, 본 wrapper는
    watermark write 경로에 대한 추가 방어.
    """

    _BLOCKED = frozenset({
        "set", "mset", "msetnx", "setnx", "setex", "psetex", "getset", "append", "delete", "unlink",
        "expire", "pexpire", "hset", "hmset", "hdel", "lpush", "rpush", "sadd", "zadd", "incr", "decr",
        "flushdb", "flushall", "rename", "eval", "evalsha", "pipeline",
        "execute_command",  # 우회형 mutator (예: execute_command("SET", ...)) 차단 — inner.get은 inner 자체
                            # execute_command를 쓰므로 wrapper 위임 read엔 영향 없음
    })

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED:
            def _raise(*_a: Any, **_k: Any) -> Any:
                raise RuntimeError(f"_ReadOnlyClient: rehearsal write 금지 — {name}() 차단")
            return _raise
        return getattr(self._inner, name)


# ────────────────────────────── no-op adapter ──────────────────────────────
class _RehearsalAdapter(FxLiveCoordinatorAdapter):
    """structural send/write 0 + cross-thread session affinity 닫기. would-send/write 기록(보고·검증용)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.would_sends: List[dict] = []
        self.would_writes: List[dict] = []

    async def _publisher_fn(self, asset: str, payload: dict) -> SendResult:
        # NO real send (topic_dispatcher.publish_topic_detailed 미호출). 실제 subscriber_count로 would-send
        # 분류만 — ⚠️ "send 가정 성공"(partial send/dispatcher 직렬화/eviction 미검증).
        topic = FX_TOPICS[asset]
        count = topic_dispatcher.registry.subscriber_count(topic)
        self.would_sends.append(
            {"asset": asset, "topic": topic, "subscriber_count": count,
             "payload_keys": sorted(payload.keys())}
        )
        if count > 0:
            return SendResult(SendDisposition.SENT, count)
        return SendResult(SendDisposition.NO_SUBSCRIBERS, 0)

    async def _watermark_writer(self, asset: str, wm: Watermark) -> bool:
        # NO real store.write. would-write 기록 + True(coordinator COMMITTED 완주용) — ⚠️ Redis CAS/store
        # failure 미검증.
        self.would_writes.append(
            {"asset": asset, "lineage_id": wm.lineage_id, "publish_sequence": wm.publish_sequence}
        )
        return True

    async def _db_revisions_reader(self, asset: str) -> Mapping[str, Revision]:
        # codex point 6: to_thread 우회 — caller-owned session을 event-loop thread에서만 접근(thread-affinity
        # 구조적으로 닫음). rehearsal은 read-only·순차라 동기 read로 충분.
        return self._read_db_revisions(asset)


async def synthetic_publish_candidate_outcomes(
    asset: str, build_result: BuildResult
) -> Mapping[str, WriteOutcome]:
    """present source를 REFRESHED_EQUAL(PUBLISH_CANDIDATE)로 — write-axis 인위 block 회피.

    ⚠️ synthetic: real conflict/structural BLOCK은 write-time(source-writer Lua) 산출이라 미검증.
    MALFORMED build는 present=()라 자연히 {} (decide MALFORMED 분기는 raw write_outcomes 사용 — 빈 dict면
    has_block False → RETRY 종결, fail-safe).
    """
    return {
        src: WriteOutcome(
            WriteState.REFRESHED_EQUAL, _SYNTHETIC_REVISION, _SYNTHETIC_REVISION,
            RedisWritePerformed.APPLIED, RevisionAdvanced.NO,
        )
        for src in build_result.present_sources
    }


# ────────────────────────────── legacy re-derive + classify ──────────────────────────────
def legacy_would_action(asset: str) -> str:
    """legacy _publish_fx_snapshot guard mirror (effect-free — builder/publish 미호출).

    ⚠️ 'would invoke builder+publish'이지 'would succeed' 아님 — guard 통과 후 실제 빌드/전송은 미실행.
    """
    if not (config.FX_TOPIC_ENABLED and config.TOPIC_DISPATCHER_ENABLED):
        return "skipped_disabled"
    if topic_dispatcher.registry.subscriber_count(FX_TOPICS[asset]) == 0:
        return "skipped_no_subscribers"
    return "would_publish"


def coordinator_decision_label(result: PublishResult) -> str:
    """PublishResult tagged XOR → 단일 label (outcome.value 또는 decision.action.value)."""
    if result.outcome is not None:
        return result.outcome.value
    assert result.decision is not None  # XOR invariant (PublishResult.__post_init__)
    return result.decision.action.value


# render에서 운영자 주의가 필요한 classification (attention set) — 나머지는 expected/safe.
ATTENTION_CLASSES = frozenset({
    "review", "atomic_block", "send_issue",
    "disabled_divergent", "skip_no_subscribers_divergent", "would_publish_divergent",
})


def classify(coord_label: str, legacy_action: str) -> str:
    """coordinator would-decision을 1차로 분류(disposition 중심) + legacy 불일치는 _divergent로 표기.

    legacy/coordinator는 **다른 축**에서 동작(gate=coordinator 전용, v2 데이터 유무=coordinator 전용,
    subscriber=둘 다 — standalone script는 항상 0)이라 1:1 mismatch 비교는 부적절(codex point 8 + 0-sub 현실).
    그래서 coordinator outcome을 기준으로 expected/safe vs attention으로 나누고, publish/skip/disabled 축에서만
    legacy와 비교해 불일치를 _divergent로 표기. "review"는 **진짜 미매핑** 조합에만 남김(known outcome 오분류 X).

    - gate_blocked: GATE_WOULD_BLOCK — cutover gate(coordinator 전용). cutover ready 전엔 정상.
    - atomic_defers: RETRY — MALFORMED(dormant=v2 데이터 부재)/general-failed write. coordinator 안전 defer.
    - atomic_block: BLOCK — conflict/structural write outcome. 조사 필요(synthetic stub에선 미발생).
    - skip_dedup: SKIP_DEDUP_IDENTICAL — content 동일 멱등 skip.
    - skip_no_subscribers[_divergent]: SKIP_SUBSCRIBER_ZERO/NO_SUBSCRIBERS — legacy도 no-subscriber면 일치.
    - disabled[_divergent]: DISABLED — legacy도 skipped_disabled면 일치.
    - would_publish[_divergent]: COMMITTED — legacy도 would_publish면 일치(standalone에선 0-sub라 미도달).
    - send_issue: ALL_SEND_FAILED/SEND_EXCEPTION/WATERMARK_* — post-send 이상(no-op publisher라 rehearsal 희소).
    - review: 위 어디에도 안 잡힌 진짜 예상 밖 조합.
    """
    # 매핑 대상: PublishOutcome 10개(terminal outcome) + terminal DecisionAction 4개(RETRY/BLOCK/
    # SKIP_DEDUP_IDENTICAL/SKIP_SUBSCRIBER_ZERO). DecisionAction.PUBLISH는 coordinator가 terminal decision으로
    # 반환 안 하고 send/commit으로 넘기므로(atomic_coordinator.py:309) coord_label에 "publish" 미출현 →
    # 의도적으로 미매핑(혹 도달해도 review로 안전 처리).

    # coordinator 전용 축 (legacy 대응 없음)
    if coord_label == PublishOutcome.GATE_WOULD_BLOCK.value:
        return "gate_blocked"
    if coord_label == DecisionAction.RETRY.value:
        return "atomic_defers"
    if coord_label == DecisionAction.BLOCK.value:
        return "atomic_block"
    if coord_label == DecisionAction.SKIP_DEDUP_IDENTICAL.value:
        return "skip_dedup"
    # publish/skip/disabled 축 — legacy와 비교(불일치 = _divergent)
    if coord_label in (DecisionAction.SKIP_SUBSCRIBER_ZERO.value, PublishOutcome.NO_SUBSCRIBERS.value):
        return "skip_no_subscribers" if legacy_action == "skipped_no_subscribers" else "skip_no_subscribers_divergent"
    if coord_label == PublishOutcome.DISABLED.value:
        return "disabled" if legacy_action == "skipped_disabled" else "disabled_divergent"
    if coord_label == PublishOutcome.COMMITTED.value:
        return "would_publish" if legacy_action == "would_publish" else "would_publish_divergent"
    # post-send 이상 (no-op publisher라 rehearsal에선 거의 안 나옴 — 방어적 매핑)
    if coord_label in (
        PublishOutcome.ALL_SEND_FAILED.value, PublishOutcome.SEND_EXCEPTION.value,
        PublishOutcome.WATERMARK_WRITE_FAILED.value, PublishOutcome.WATERMARK_STALE.value,
        PublishOutcome.WATERMARK_DIVERGENT_ALERT.value, PublishOutcome.WATERMARK_LINEAGE_MISMATCH.value,
    ):
        return "send_issue"
    return "review"


# ────────────────────────────── report model + run ──────────────────────────────
@dataclass
class AssetRehearsal:
    asset: str
    coord_kind: str          # "outcome" | "decision"
    coord_label: str
    gate_disposition: Optional[str]
    legacy_action: str
    classification: str
    would_send: bool
    would_write: bool


@dataclass
class RehearsalReport:
    gate_note: str
    snapshot_state: str
    snapshot_read_ok: bool
    assets: List[AssetRehearsal] = field(default_factory=list)
    would_sends: List[dict] = field(default_factory=list)
    would_writes: List[dict] = field(default_factory=list)


def _gate_open_snapshot() -> CutoverReadinessSnapshot:
    """--force-gate-open 진단용 synthetic snapshot(gate OPEN) — ⚠️ go signal 아님, gate 우회."""
    return CutoverReadinessSnapshot(
        writer_enforced_action=WriterMode.LEGACY,
        cutover_state=CutoverState.LEGACY_READY,
        bootstrap_session_id=None,
        bootstrap_generation=0,
        bootstrap_status="idle",
        per_asset_publish_state=(),
        publisher_gate_open=True,
        read_ok=False,
    )


def _to_asset_rehearsal(asset: str, result: PublishResult) -> AssetRehearsal:
    coord_kind = "outcome" if result.outcome is not None else "decision"
    coord_label = coordinator_decision_label(result)
    legacy = legacy_would_action(asset)
    gate = result.gate_disposition.value if result.gate_disposition is not None else None
    return AssetRehearsal(
        asset=asset, coord_kind=coord_kind, coord_label=coord_label,
        gate_disposition=gate, legacy_action=legacy,
        classification=classify(coord_label, legacy),
        would_send=result.send_performed,
        would_write=(result.write_action is not None and result.write_action.value == "write"),
    )


async def rehearse(db: Any, *, force_gate_open: bool = False, client: Any = None) -> RehearsalReport:
    """coordinator dry-run을 FX asset 전체에 대해 실행하고 report 반환 (read-only, no-op send/write)."""
    if force_gate_open:
        snap = _gate_open_snapshot()
        gate_note = "FORCED OPEN (진단 — go signal 아님; cutover gate 우회)"
    else:
        snap = read_cutover_snapshot_fresh(db)
        gate_note = (
            f"faithful (cutover_state={snap.cutover_state.value}, "
            f"gate_open={snap.publisher_gate_open}, read_ok={snap.read_ok})"
        )

    # defense-in-depth: real 경로(client 미주입)는 _ReadOnlyClient로 감싸 set/delete를 client 층에서도 거부
    # (no-op override 우회 backstop). 테스트는 explicit client 주입(set 추적용)이라 미wrap.
    if client is None:
        from app.latest_rates_cache import _get_sync_client
        client = _ReadOnlyClient(_get_sync_client())

    adapter = _RehearsalAdapter(db, client=client, snapshot_fn=lambda: snap)
    coordinator = adapter.build_coordinator(
        write_outcomes_provider=synthetic_publish_candidate_outcomes
    )

    assets: List[AssetRehearsal] = []
    for asset in sorted(FX_TOPIC_ASSETS):
        result = await coordinator.publish_asset(asset)
        assets.append(_to_asset_rehearsal(asset, result))

    return RehearsalReport(
        gate_note=gate_note,
        snapshot_state=snap.cutover_state.value,
        snapshot_read_ok=snap.read_ok,
        assets=assets,
        would_sends=adapter.would_sends,
        would_writes=adapter.would_writes,
    )


def render_report(report: RehearsalReport) -> str:
    """report를 사람이 읽는 텍스트로 — DRY-RUN banner + per-asset 대조 + label + aggregate."""
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("  FX CUTOVER SHADOW REHEARSAL — DRY-RUN (read-only, send/write 구조적 0)")
    lines.append("=" * 78)
    lines.append(f"cutover gate: {report.gate_note}")
    lines.append("")
    lines.append("labels:")
    lines.append("  • standalone script라 WS 구독자 0 → 실제 run에선 PUBLISH/send 미도달(decide가 subscriber 0)")
    lines.append("  • COMMITTED/would-send는 'send 가정 성공' — partial send/dispatcher 직렬화/eviction 미검증")
    lines.append("  • would-write는 'write 결정' — Redis CAS/store write 성공 가정(실 write 미수행)")
    lines.append("  • synthetic write_outcomes — real conflict/structural BLOCK은 write-time-only라 미검증")
    lines.append("  • legacy = 'would invoke builder+publish'(guard 통과)이지 'would succeed' 아님")
    if not report.snapshot_read_ok and "faithful" in report.gate_note and report.snapshot_state == "halt_blocked":
        lines.append("  • gate closed(fresh read 실패/HALT) → 전 asset gate_blocked, 비-gate 축 미관측 "
                     "→ --force-gate-open으로 decide 축 비교")
    lines.append("")
    header = f"  {'asset':<14}{'coordinator':<26}{'legacy':<22}{'class'}"
    lines.append(header)
    lines.append("  " + "-" * 72)
    for a in report.assets:
        coord = f"{a.coord_label}({a.coord_kind})"
        lines.append(f"  {a.asset:<14}{coord:<26}{a.legacy_action:<22}{a.classification}")
    lines.append("")

    # aggregate
    buckets: dict = {}
    for a in report.assets:
        buckets[a.classification] = buckets.get(a.classification, 0) + 1
    lines.append("aggregate (classification → count):")
    for k in sorted(buckets):
        lines.append(f"  {k}: {buckets[k]}")
    lines.append("")
    lines.append(
        f"publisher_fn 진입(would-send 기록): {len(report.would_sends)} | "
        f"watermark write 결정(would-write 기록, 성공 가정): {len(report.would_writes)}"
    )

    attention = [a.asset for a in report.assets if a.classification in ATTENTION_CLASSES]
    if attention:
        attn_pairs = [(a.asset, a.classification) for a in report.assets if a.classification in ATTENTION_CLASSES]
        lines.append("")
        lines.append(f"⚠️ 확인 필요: {attn_pairs} — divergent/review/send_issue/atomic_block은 사람 검토")
    lines.append("=" * 78)
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="C6-9b FX cutover shadow rehearsal (read-only, no-op send/write)"
    )
    p.add_argument(
        "--confirm-read-prod", action="store_true",
        help="설정된(DATABASE_URL/REDIS) backend를 READ(쓰기 0)함을 명시 ack — 우발 실행 방지용 "
             "acknowledgment(prod 자동 감지 아님; 대상은 env 설정 따름)",
    )
    p.add_argument(
        "--force-gate-open", action="store_true",
        help="cutover gate 우회(synthetic open) — decide/build 경로를 강제 노출하는 진단용(go signal 아님)",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not args.confirm_read_prod:
        print(
            "거부: 이 스크립트는 설정된(DATABASE_URL/REDIS) backend를 READ(쓰기 0)합니다. "
            "우발 실행 방지를 위해 --confirm-read-prod ack 필수 (prod 자동 감지 아님 — 대상은 env 설정 따름).",
            file=sys.stderr,
        )
        return 2

    # lazy import (app.database는 conftest의 DATABASE_URL override 후 import되어야 — 직접 실행 시는 .env)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        report = asyncio.run(rehearse(db, force_gate_open=args.force_gate_open))
    finally:
        try:
            db.close()
        except Exception:
            pass
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
