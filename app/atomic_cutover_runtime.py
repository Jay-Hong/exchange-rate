"""P1b C6-2 — cutover readiness runtime cache (CutoverReadinessSnapshot, §15, dormant).

A2-1(`atomic_write_runtime.py`) 패턴 그대로 cutover 측에. C6-2 durable read/derive 위에 process-memory
immutable snapshot을 얹는다. C6-7 publisher gate / C6-8 command가 나중에 consume.

A2-1과 동일 invariant: (1) import-time side effect 0, (2) snapshot() no-throw last-good, (3) bootstrap_
generation 관측 단조(read-fail 회귀 금지), (4) refresh lock 분리(DB read/compute lock 밖, swap만 안).
**live poll scheduling 없음** — import/lifespan 자동 refresh 금지(C6-8/C6-runtime wiring 영역). writer
axis는 atomic_write_runtime.snapshot().enforced_action 재사용(단일 mode 경로, cross-table torn read 회피).

fail-closed: read/compute 실패 → publisher_gate_open=False + cutover_state=HALT_BLOCKED + read_ok=False,
단 bootstrap_generation은 보존(fencing token 회귀 금지). _INITIAL = pre-cutover legacy-safe(LEGACY_READY,
gate_open=True) — A2 phase-gate mirror(배포≠block).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

from app import atomic_write_runtime
from app.atomic_cutover import CutoverState, PublisherGateDisposition, publisher_gate_disposition
from app.atomic_write_control import WriterMode
from app.atomic_cutover_durable import (
    CutoverControlView,
    derive_cutover_state,
    read_cutover_assets,
    read_cutover_control,
)
from app.fx_membership import FX_MEMBERSHIP_VERSION

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CutoverReadinessSnapshot:
    """C6-7 gate / C6-8 command가 1-read하는 immutable cutover snapshot (torn read 방지)."""

    writer_enforced_action: str               # atomic_write_runtime enforced_action (writer axis)
    cutover_state: CutoverState               # derive_cutover_state 결과 (GLOBAL verdict)
    bootstrap_session_id: Optional[str]
    bootstrap_generation: int                 # 관측 단조 (fencing token, read-fail 회귀 금지)
    bootstrap_status: str
    per_asset_publish_state: Tuple[Tuple[str, str], ...]  # sorted (asset, publish_state) — hashable diagnostics
    publisher_gate_open: bool                 # publisher_gate_disposition(cutover_state)==PASS_THROUGH
    read_ok: bool


# 초기 안전 default — refresh 전(또는 read 실패 fallback). pre-cutover legacy passthrough(gate open).
_INITIAL = CutoverReadinessSnapshot(
    writer_enforced_action=WriterMode.LEGACY,
    cutover_state=CutoverState.LEGACY_READY,
    bootstrap_session_id=None,
    bootstrap_generation=0,
    bootstrap_status="idle",
    per_asset_publish_state=(),
    publisher_gate_open=True,   # pre-cutover legacy 정상 발행
    read_ok=False,
)

_LOCK = threading.RLock()  # 객체 생성만 (thread 시작 아님 — import side effect 0)
_current: CutoverReadinessSnapshot = _INITIAL


def snapshot() -> CutoverReadinessSnapshot:
    """현재 cutover readiness snapshot (no-throw, last-good). gate/command 단일 accessor."""
    try:
        with _LOCK:
            return _current
    except Exception:
        return _INITIAL


def _row_generation(control: Optional[CutoverControlView], current: int) -> int:
    """관측 bootstrap_generation — fencing token이라 단조(현재값 미만 회귀 금지)."""
    if control is None:
        return current
    gen = control.generation
    if isinstance(gen, int) and not isinstance(gen, bool):
        return gen if gen > current else current
    return current


@dataclass(frozen=True)
class _DeriveResult:
    """_read_and_derive 결과 — refresh_from_db(swap) + read_cutover_snapshot_fresh(pure) 공유."""
    writer_enforced: str
    control: Optional[CutoverControlView]
    cutover_state: CutoverState
    per_asset: Tuple[Tuple[str, str], ...]
    session_id: Optional[str]
    status: str
    read_ok: bool


def _read_and_derive(db: "Session") -> _DeriveResult:
    """DB read + derive (lock 밖, no-throw). read/compute 실패 → fail-closed(HALT_BLOCKED, read_ok=False).

    control-first(torn fail-closed) + writer axis는 atomic_write_runtime.snapshot() 재사용. _current 미접촉
    — caller(refresh_from_db는 swap, read_cutover_snapshot_fresh는 pure build)가 처리.
    """
    try:
        writer_enforced = atomic_write_runtime.snapshot().enforced_action
        control = read_cutover_control(db)
        assets = read_cutover_assets(db)
        cutover_state = derive_cutover_state(writer_enforced, control, assets, FX_MEMBERSHIP_VERSION)
        per_asset = tuple(sorted((a, v.publish_state) for a, v in assets.items()))
        return _DeriveResult(
            writer_enforced=writer_enforced, control=control, cutover_state=cutover_state,
            per_asset=per_asset,
            session_id=control.session_id if control is not None else None,
            status=control.status if control is not None else "idle",
            read_ok=True,
        )
    except Exception:
        return _DeriveResult(
            writer_enforced=WriterMode.HALT, control=None, cutover_state=CutoverState.HALT_BLOCKED,
            per_asset=(), session_id=None, status="idle", read_ok=False,
        )


def read_cutover_snapshot_fresh(db: "Session") -> CutoverReadinessSnapshot:
    """**pure** fresh cutover snapshot — read+derive 후 즉시 build, **_current 미변경**(observability 전용,
    C6-9 go/no-go endpoint). gate가 읽는 _current cache를 advance하지 않아 observer side-effect 0. no-throw.

    refresh_from_db와 달리 monotonic gen cache 없음(one-shot raw DB gen). gen=control.generation(read_ok),
    read 실패 시 fail-closed(HALT_BLOCKED, gate 닫음, read_ok=False).
    """
    r = _read_and_derive(db)
    try:
        if r.read_ok:
            gate_open = publisher_gate_disposition(r.cutover_state) is PublisherGateDisposition.PASS_THROUGH
            return CutoverReadinessSnapshot(
                writer_enforced_action=r.writer_enforced, cutover_state=r.cutover_state,
                bootstrap_session_id=r.session_id, bootstrap_generation=_row_generation(r.control, 0),
                bootstrap_status=r.status, per_asset_publish_state=r.per_asset,
                publisher_gate_open=gate_open, read_ok=True,
            )
    except Exception:
        pass  # build 예외(미래 derive non-enum 등) → 아래 fail-closed (구조적 no-throw — go/no-go authority)
    return CutoverReadinessSnapshot(   # read_ok=False or build 예외 → fail-closed
        writer_enforced_action=WriterMode.HALT, cutover_state=CutoverState.HALT_BLOCKED,
        bootstrap_session_id=None, bootstrap_generation=0, bootstrap_status="idle",
        per_asset_publish_state=(), publisher_gate_open=False, read_ok=False,
    )


def refresh_from_db(db: "Session") -> CutoverReadinessSnapshot:
    """cutover rows + write-mode snapshot을 읽어 cutover snapshot 갱신 후 반환 (no-throw).

    invariant 4: DB read + derive는 lock 밖, lock 안에서는 swap만. invariant 3: bootstrap_generation은
    관측 단조(read-fail여도 prev 보존). read/compute 실패 → fail-closed(gate_open=False, HALT_BLOCKED).
    writer axis는 atomic_write_runtime.snapshot()(이미 cache된 값) 재사용 — 별 mode read 안 함.

    **torn-read**: control을 **먼저** 읽고 assets를 나중에 읽는다(2 sequential read). §15-5 finalize는 단일
    atomic tx({status=completed} + 3 asset flip)라, control이 completed로 보이면 finalize가 이미 커밋됐다는
    뜻 → 이후 asset read는 ready를 본다(completed⟹assets ready). control=running 시점에 finalize가 끼어
    asset이 ready여도 derive는 status≠completed라 ATOMIC_BLOCKED(fail-closed, gate 안 열림). 즉 control-first
    순서가 torn을 **fail-closed**로 흡수(ATOMIC_READY 오발 불가). 별 isolation 불요.

    **C6-2엔 호출자 없음**(live poll/gate 미연결). C6-runtime/C6-8 wiring이 startup/command에서 호출.
    """
    global _current
    r = _read_and_derive(db)  # DB read + derive (lock 밖, no-throw) — control-first torn fail-closed
    # ── swap (lock 안 — gen 단조, read-fail fail-closed) ──
    try:
        with _LOCK:
            prev = _current
            gen = _row_generation(r.control, prev.bootstrap_generation) if r.read_ok else prev.bootstrap_generation
            if r.read_ok:
                gate_open = publisher_gate_disposition(r.cutover_state) is PublisherGateDisposition.PASS_THROUGH
                new = CutoverReadinessSnapshot(
                    writer_enforced_action=r.writer_enforced, cutover_state=r.cutover_state,
                    bootstrap_session_id=r.session_id, bootstrap_generation=gen, bootstrap_status=r.status,
                    per_asset_publish_state=r.per_asset, publisher_gate_open=gate_open, read_ok=True,
                )
            else:
                # read 실패: fail-closed(gate 닫음, HALT_BLOCKED) + gen 보존(회귀 금지). session/status는 prev 보존.
                new = CutoverReadinessSnapshot(
                    writer_enforced_action=r.writer_enforced, cutover_state=CutoverState.HALT_BLOCKED,
                    bootstrap_session_id=prev.bootstrap_session_id, bootstrap_generation=gen,
                    bootstrap_status=prev.bootstrap_status, per_asset_publish_state=prev.per_asset_publish_state,
                    publisher_gate_open=False, read_ok=False,
                )
            _current = new
            return new
    except Exception:
        return snapshot()  # swap 실패(거의 불가)도 last-good


def _reset_for_test() -> None:
    """테스트 전용 — 모듈 cache를 _INITIAL로 리셋. 운영 코드 호출 금지."""
    global _current
    with _LOCK:
        _current = _INITIAL
