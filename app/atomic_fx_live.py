"""P1b C6-6 — live coordinator adapters (FxLiveCoordinatorAdapter, dormant, behavior-change-0).

B2b AtomicFxCoordinator의 13 CoordinatorHooks(atomic_coordinator.py:182-204)를 real live I/O +
C6-3/C6-4/C6-5 building block으로 조립하고 coordinator를 생성하는 **live-bridge adapter**.
coordinator는 constructible이나 **live caller 0** — legacy fx_topic_publisher._publish_fx_snapshot
경로는 byte-identical 유지. publish_asset을 live publish에 배선하는 것은 **C6-7**(gate wrapper), C6-6 아님.

dormancy (C6-3/4/5 island과 다른 posture):
- atomic_fx_live는 island(atomic_coordinator/atomic_fx_publisher/atomic_watermark_store/atomic_build/
  atomic_watermark/atomic_write_outcome/atomic_cutover)와 live(latest_rates_cache._get_sync_client/
  topic_dispatcher/fx_topic_publisher/crud/atomic_fx_v2_loader) 둘 다 import → positive trip-wire 불가.
- **negative-only**: (a) live 모듈이 atomic_fx_live import 0 (AST trip-wire), (b) coordinator.publish_asset
  호출자 0. atomic_fx_live를 island skip-list(_DORMANT_MODULES)에 추가해 atomic_coordinator import 허용.

설계 핵심 (Workflow 3-lens + codex high 의견일치 + 2 blocker):
- **same-read (§15:215)**: build_fn이 C6-3 loader(load_fx_topic_payload_with_revisions)를 **1회** 호출 →
  FxV2LoadResult를 per-asset memo(self._last_load)에 stash → BuildResult를 **직접 구성**(atomic_build.
  build_fx_result는 load_and_build_fx_topic_payload re-read라 회피). effective_resolver는 memo 읽기(Redis 0).
  coordinator가 build_fn(④)→effective_resolver를 per-asset lock 안 연속 호출(atomic_coordinator.py:299-300)
  + worker==1 → single memo slot race-free.
- **completeness gate (C6-6 소유)**: C6-3는 effective⊆present(subset 가능)이나 decide는 effective keys==
  present **EXACT**(atomic_reconcile.py:195 ValueError). build_fn에서 effective≠present이면 **MALFORMED**
  BuildResult(payload None) 반환 → decide MALFORMED branch(177-181, exact-key 검사 전)→RETRY. migration
  완료 전까지 안전 defer.
- **publisher_fn (Blocker 1)**: publish_topic_detailed는 **async** → await 직접(to_thread 금지). asset→topic
  (FX_TOPICS) + **payload['topic'] 주입**(dict copy — legacy fx_topic_publisher.py:197 parity, builder는
  topic-agnostic, wrapper 책임) → send_counts_to_send_result로 SendResult 매핑.
- **write_outcomes_provider (Blocker 2)**: WriteOutcome은 write-time(source-writer Lua) 산출이라 publish-time
  read로 재구성 불가. build_coordinator(*, write_outcomes_provider=None)로 **injectable**, default는
  **fail-closed RETRY stub**(present source마다 FAILED+non-structural → decide RETRY → 발행 안 함). 즉
  dormant coordinator가 실수로 불려도 publish 안 함(fail-closed). happy-path 테스트는 explicit candidate
  provider 주입. **C6-7은 live 배선 전 real WriteOutcome provider로 교체 필수**(else conflict/structural
  BLOCK 탐지 무성 비활성).
- **sync→async**: C6-3 loader / store.read·write / crud selector는 sync → asyncio.to_thread. publisher
  (async)·subscriber_counter·gate·feature_flags·clock·seq는 to_thread 불요.

C6-7 precondition (이 모듈이 live 되기 전 필수): (1) real write_outcomes provider, (2) FX topic telemetry
parity(_record_topic_event 경로 — C6-6 direct dispatcher publish는 우회), (3) cutover snapshot
refresh_from_db scheduling(미스케줄 시 _INITIAL legacy-passthrough gate), (4) caller-owned SQLAlchemy
Session의 to_thread handoff 재검토(thread 내 session 생성 또는 single-thread read 제한), (5) publish_asset
gate enforcement를 _publish_fx_snapshot에 설치(C6-7 characterization), (6) membership_version 변경 시 lineage
재생성(bootstrap_generation bump 또는 is_watermark_compatible 참조 — 현재 lineage_provider는 cutover
snapshot의 session/gen만 본다).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from app import config, crud
from app.atomic_build import BuildCompleteness, BuildResult
from app.atomic_coordinator import AtomicFxCoordinator, CoordinatorHooks, SendResult
from app.atomic_cutover import PublisherGateDisposition
from app.atomic_cutover_runtime import snapshot as cutover_snapshot
from app.atomic_fx_publisher import send_counts_to_send_result
from app.atomic_fx_v2_loader import FxV2LoadResult, load_fx_topic_payload_with_revisions
from app.atomic_revision import Revision
from app.atomic_watermark import Watermark
from app.atomic_watermark_store import AtomicWatermarkStore
from app.atomic_write_outcome import (
    RedisWritePerformed,
    RevisionAdvanced,
    WriteOutcome,
    WriteState,
)
from app import topic_dispatcher  # 모듈 속성 접근(registry/publish_topic_detailed) — 싱글톤 stale 바인딩 회피
from app.fx_membership import FX_MEMBERSHIP_SOURCES, fx_membership_version
from app.fx_topic_publisher import FX_TOPICS
from app.latest_rates_cache import _get_sync_client

_KST = timezone(timedelta(hours=9))
_SENTINEL_REVISION: Revision = (0, 0)  # fail-closed stub 전용 — RETRY는 candidate build 전 종결이라 미사용
_FAIL_CLOSED_REASON = "c6-6 fail-closed stub (no real write_outcomes provider — C6-7 must inject)"


class _InMemorySeqSource:
    """lineage-scoped publish_sequence 단조 발급원 (coordinator in-memory, eviction-alive, §19:337-338).

    coordinator가 floor=max(next, current.seq+1)를 계산하므로 next는 단조 제안값. observe_assigned가
    실제 발급값을 max로 저장 → Redis watermark eviction(process 생존) 후에도 regression 방지.
    """

    def __init__(self) -> None:
        self._last: Dict[tuple, int] = {}

    def next(self, asset: str, lineage_id: str) -> int:
        return self._last.get((asset, lineage_id), 0) + 1

    def observe_assigned(self, asset: str, lineage_id: str, assigned: int) -> None:
        key = (asset, lineage_id)
        self._last[key] = max(self._last.get(key, 0), assigned)


class FxLiveCoordinatorAdapter:
    """13 CoordinatorHooks를 real I/O + C6-3/4/5로 조립하는 live-bridge (dormant — live caller 0).

    의존성은 injectable(live default) — 테스트가 fake 주입. build_coordinator()가 AtomicFxCoordinator 생성.
    """

    def __init__(
        self,
        db: Any,
        *,
        client: Any = None,
        loader: Optional[Callable[[Any, str], FxV2LoadResult]] = None,
        snapshot_fn: Optional[Callable[[], Any]] = None,
        bank_revisions_selector: Optional[Callable[[Any, str], Any]] = None,
        investing_revision_selector: Optional[Callable[[Any, str], Any]] = None,
    ) -> None:
        self._db = db
        self._client = client if client is not None else _get_sync_client()
        self._store = AtomicWatermarkStore(self._client)
        self._loader = loader if loader is not None else load_fx_topic_payload_with_revisions
        self._snapshot_fn = snapshot_fn if snapshot_fn is not None else cutover_snapshot
        self._bank_rev_selector = (
            bank_revisions_selector if bank_revisions_selector is not None
            else crud._select_latest_bank_rates_with_revision
        )
        self._investing_rev_selector = (
            investing_revision_selector if investing_revision_selector is not None
            else crud._select_latest_investing_rate_with_revision
        )
        self._seq = _InMemorySeqSource()
        self._last_load: Dict[str, FxV2LoadResult] = {}

    # ── build_fn + same-read memo + completeness gate ─────────────────────────
    def _malformed(self, asset: str, error: str) -> BuildResult:
        return BuildResult(
            asset=asset, payload=None, present_sources=(), missing_sources=(),
            membership_version=fx_membership_version(),
            completeness=BuildCompleteness.MALFORMED, build_error=error,
        )

    def _build_sync(self, asset: str) -> BuildResult:
        # build-stage 예외(loader Redis/DB-fallback 실패, build_fx_tab_payload/FxV2LoadResult.__post_init__
        # ValueError 등)는 MALFORMED로 흡수 → decide RETRY (build_fx_result + legacy safe_publish_* per-asset
        # 격리 정합). raise하면 coordinator가 build_fn(atomic_coordinator.py:299, 미guard)에서 publish_asset
        # 밖으로 propagate. BaseException(CancelledError)은 미흡수(except Exception).
        # asset 일관성은 BuildResult.__post_init__(present==_extract_present_sources(payload)) +
        # effective_resolver present-mismatch guard가 강제 — 별도 dead assert 불요.
        try:
            load = self._loader(self._db, asset)  # ONE Redis read (payload + effective)
            self._last_load[asset] = load
            present = set(load.present_sources)
            effective_keys = set(load.effective_revision_vector)
            if effective_keys != present:
                # completeness gate: FxV2LoadResult invariant상 effective⊆present → != ⟺ strict subset.
                # MALFORMED → decide RETRY (exact-key ValueError crash 회피, atomic_reconcile.py:195)
                return self._malformed(
                    asset,
                    f"effective⊊present: migration incomplete, missing={sorted(present - effective_keys)}",
                )
            missing = tuple(sorted(FX_MEMBERSHIP_SOURCES - present))
            return BuildResult(
                asset=asset, payload=load.payload,
                present_sources=load.present_sources, missing_sources=missing,
                membership_version=fx_membership_version(),
                completeness=BuildCompleteness.COMPLETE if not missing else BuildCompleteness.PARTIAL,
                build_error=None,
            )
        except Exception as e:  # noqa: BLE001 — build-stage 예외 → MALFORMED(RETRY), BaseException 제외
            return self._malformed(asset, f"build exception: {e!r}")

    async def _build_fn(self, asset: str) -> BuildResult:
        return await asyncio.to_thread(self._build_sync, asset)

    async def _effective_resolver(self, asset: str, build_result: BuildResult) -> Mapping[str, str]:
        # MALFORMED는 present=()이므로 effective도 {} (coverage eager 검사 — atomic_reconcile.py:195)
        if build_result.completeness is BuildCompleteness.MALFORMED:
            return {}
        load = self._last_load.get(asset)
        if load is None:
            raise ValueError(f"effective_resolver: {asset} memo 없음 (build_fn 선행 필수)")
        if tuple(sorted(load.present_sources)) != tuple(sorted(build_result.present_sources)):
            raise ValueError(f"effective_resolver: {asset} memo present mismatch")
        return dict(load.effective_revision_vector)  # gate 통과 → keys == present

    # ── write_outcomes (injectable, default fail-closed RETRY) ────────────────
    async def _fail_closed_write_outcomes(
        self, asset: str, build_result: BuildResult
    ) -> Mapping[str, WriteOutcome]:
        if build_result.completeness is BuildCompleteness.MALFORMED:
            return {}
        return {
            src: WriteOutcome(
                WriteState.FAILED, _SENTINEL_REVISION, None,
                RedisWritePerformed.NOT_APPLIED, RevisionAdvanced.NO,
                reason=_FAIL_CLOSED_REASON, structural=False,
            )
            for src in build_result.present_sources
        }

    # ── watermark (C6-5 store, to_thread) ─────────────────────────────────────
    async def _watermark_reader(self, asset: str) -> Optional[Watermark]:
        return await asyncio.to_thread(self._store.read, asset)  # outage propagate, miss/corrupt→None

    async def _watermark_writer(self, asset: str, wm: Watermark) -> bool:
        return await asyncio.to_thread(self._store.write, asset, wm)  # outage→False

    # ── gate / lineage (C6-2 cutover runtime snapshot) ────────────────────────
    async def _gate_fn(self, asset: str) -> PublisherGateDisposition:
        snap = self._snapshot_fn()  # no-throw last-good (asset-agnostic GLOBAL cutover_state)
        return (
            PublisherGateDisposition.PASS_THROUGH if snap.publisher_gate_open
            else PublisherGateDisposition.WOULD_BLOCK_DRY_RUN
        )

    async def _lineage_provider(
        self, asset: str, current: Optional[Watermark], build_result: BuildResult
    ) -> str:
        # current/build_result는 의도적 미사용 — lineage는 GLOBAL cutover control plane state(session:gen).
        # membership_version 변경 시 lineage 재생성(bootstrap_generation bump 또는 is_watermark_compatible)은
        # C6-7 precondition (현재는 snapshot의 session/gen만 본다).
        snap = self._snapshot_fn()
        sid = snap.bootstrap_session_id or "bootstrap"  # None → non-empty sentinel (Watermark invariant)
        return f"{sid}:{snap.bootstrap_generation}"

    # ── feature flags / subscribers / db revisions / publisher / clock ────────
    async def _feature_flags(self, asset: str) -> tuple:
        # order load-bearing (coordinator step②: flags[0] AND flags[1]). config 모듈 속성을 매 호출 read
        # (patch/reassign 반영) — env는 import-time 1회 read라 런타임 env 변경엔 process 재시작 필요.
        return (config.FX_TOPIC_ENABLED, config.TOPIC_DISPATCHER_ENABLED)

    async def _subscriber_counter(self, asset: str) -> int:
        # topic_dispatcher.registry 모듈 속성 접근(legacy fx_topic_publisher.py:190 정합) — 직접 import면
        # 싱글톤 stale 바인딩이라 런타임 교체 미반영
        return topic_dispatcher.registry.subscriber_count(FX_TOPICS[asset])  # in-memory (no to_thread)

    def _read_db_revisions(self, asset: str) -> Mapping[str, Revision]:
        out: Dict[str, Revision] = {
            rr.source: rr.revision for rr in self._bank_rev_selector(self._db, asset)
        }
        inv = self._investing_rev_selector(self._db, asset)
        if inv is not None:
            out[inv.source] = inv.revision
        return out

    async def _db_revisions_reader(self, asset: str) -> Mapping[str, Revision]:
        return await asyncio.to_thread(self._read_db_revisions, asset)

    async def _publisher_fn(self, asset: str, payload: dict) -> SendResult:
        topic = FX_TOPICS[asset]
        p = dict(payload)  # shallow copy — build_result.payload 불변 유지
        p["topic"] = topic  # Blocker 1: legacy parity (fx_topic_publisher.py:197 wrapper 책임)
        counts = await topic_dispatcher.publish_topic_detailed(topic, p)  # async — await 직접 (to_thread 금지)
        return send_counts_to_send_result(
            attempted=counts.attempted, sent=counts.sent, enabled=counts.enabled
        )

    def _clock(self) -> str:
        return datetime.now(_KST).isoformat()

    # ── assembly ──────────────────────────────────────────────────────────────
    def build_hooks(
        self,
        *,
        write_outcomes_provider: Optional[
            Callable[[str, BuildResult], Awaitable[Mapping[str, WriteOutcome]]]
        ] = None,
    ) -> CoordinatorHooks:
        return CoordinatorHooks(
            watermark_reader=self._watermark_reader,
            watermark_writer=self._watermark_writer,
            gate_fn=self._gate_fn,
            feature_flags=self._feature_flags,
            db_revisions_reader=self._db_revisions_reader,
            build_fn=self._build_fn,
            effective_resolver=self._effective_resolver,
            write_outcomes_provider=write_outcomes_provider or self._fail_closed_write_outcomes,
            subscriber_counter=self._subscriber_counter,
            publisher_fn=self._publisher_fn,
            seq_source=self._seq,
            lineage_provider=self._lineage_provider,
            clock=self._clock,
        )

    def build_coordinator(
        self,
        *,
        write_outcomes_provider: Optional[
            Callable[[str, BuildResult], Awaitable[Mapping[str, WriteOutcome]]]
        ] = None,
    ) -> AtomicFxCoordinator:
        """dormant AtomicFxCoordinator 생성. write_outcomes_provider 미주입 시 fail-closed RETRY stub
        (C6-7 live 배선 전 real provider 주입 필수)."""
        return AtomicFxCoordinator(self.build_hooks(write_outcomes_provider=write_outcomes_provider))
