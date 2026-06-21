#!/usr/bin/env python3
"""P1b C6-8b-2 — atomic FX activation command (scripts/, code-only, DORMANT — NOT executed).

§8/§9/§15 first-activation(legacy→halt→atomic composite + cutover begin→verify→finalize)을 C6-8a writer-axis
CAS bricks(atomic_write_durable) + C6-2 cutover CAS bricks(atomic_cutover_durable) + C6-3 migration self-verify
loader(atomic_fx_v2_loader)로 **합성하는 operator command**. 실제 실행은 C6-FLIP(must-confirm, irreversible).
이 unit은 명령을 **정확·안전하게 만들어** 두기만 한다(behavior-change-0).

⚠️ **이 command는 운영에서 실행하면 production write path + FX publish 거동을 바꾼다**(legacy→atomic). 그래서
dormancy를 **다층**으로 보장한다:
1. **scripts/ 배치**: app/ AST trip-wire(no-importer scan)는 app/만 순회 → 이 파일은 sanction 0(C6-9b 선례).
   어떤 live 모듈도 이 파일을 import/호출하지 않는다.
2. **QuiesceBoundary machine gate**: begin-atomic은 `quiesce_boundary.confirm_quiesced()`가 True여야 진행.
   constructor default는 `_FailClosedQuiesceBoundary`(항상 False) 유지지만 **W3에서 main()이 RealQuiesceBoundary
   주입**(arming) — RealBoundary는 open quiesce session + halt ACK(§9 step6 drain proof)를 검증해 True/False.
   prod behavior-change-0: W3 후에도 capability gate(IMAGE_MAX=1) + quiesce table 부재(G2a 전)로 begin-atomic은
   RELEASE/G2a/EXEC 전까지 차단(arming ≠ flip). 모든 dangerous flag가 있어도 drain 미확인 시 거부(§9:92 race 방어).
3. **image-capability hard gate**(§6 preflight): begin-atomic은 `--required-protocol`이 실행 image
   [IMAGE_MIN, IMAGE_MAX] 범위 AND > REQUIRED_WRITER_PROTOCOL_SEED 여야 한다. 현재 image는 IMAGE_MAX=1
   이라 둘을 동시 만족 불가 → begin-atomic apply가 **현재 image에서 hard-fail**. C6-FLIP release가 IMAGE_MAX를
   올린 뒤에야 통과. ⚠️ **atomic writer는 이미 실제 v2 write다**(C6-5b-3b/3c bank/investing crud.py:489/676
   → `_insert_*_atomic` = DB commit + Redis v2 compare_write, mirror C6-5b-4) — 잘못 flip하면 'write 0'(무해)이
   아니라 **실 v2 write가 발생**한다. capability gate(IMAGE_MAX)가 그 사고를 **구조적으로** 차단(write 0이라
   무해해서가 아님 — flip은 항상 real write를 낸다고 가정하고 게이트 추론할 것, critic#7).
4. **accidental-exec 강가드**: --apply(default dry-run) + --i-understand-this-flips-production +
   --rds-snapshot-confirmed + --expected-* state fence.

phasing (codex B correction + handoff, §15:202 — begin-atomic이 writer atomic = atomic/blocked reconciliation
구간 진입이지 finalize 아님; begin-atomic은 cas_activate_atomic + cas_begin_cutover를 **한 tx**로 묶어 stale
completed/ready row 상속 방지):
  preflight(read-only plan) → halt(legacy→halt, durable pre-quiesce §9:88) → [operator: quiesce + recreate] →
  begin-atomic(halt→atomic + cutover idle→running, 1 tx, atomic/blocked) → [operator: recreate-atomic §9:90] →
  verify(migration self-verify effective==present 3 asset → running→verified) → finalize(asset blocked→ready ×3
  + verified→completed, 1 tx → ATOMIC_READY gate open).
+ incident-halt(atomic→halt, C6-8b-1 dead-end recovery) + verify-only(read-only diagnostic).

crash-resume: writer mode는 **DB row(atomic_write_control)에서 직접** read(codex B1 — `read_cutover_snapshot_fresh`
의 writer 축은 process-local cache라 standalone script에선 _INITIAL legacy 오판). cutover 축은
read_cutover_control/read_cutover_assets. 양축 + asset state로 진입점 판정.

safety invariants:
- 모든 CAS는 caller-commits → command가 tx 소유(commit/rollback). 어떤 brick이든 non-APPLIED → abort+rollback
  +report(auto-retry 없음 — cutover `_disambiguate_control`이 populate_existing 미보유라 CAS_LOST/PRECONDITION
  구분에 의존 안 함, codex non-blocker).
- finalize: asset flip ×3 **먼저**, status verified→completed **마지막**, 단일 commit(§15-5 torn-read:
  completed⟹assets ready). 임의 non-APPLIED → rollback.
- format fence(codex B4): cutover CAS는 cutover_row_format_version을 fence하지 않으므로 command가 명시 거부.
  writer는 CAS가 fence하나 command도 조기 거부(명확한 에러).
- asset state fence(codex B3): begin-atomic 직전 + 모든 forward action이 3 asset blocked+null(또는 completed면
  all-ready) 강제 — cas_begin_cutover가 asset row를 보지 않아 stale ready가 finalize dead-end를 만들기 때문.

usage (dry-run plan):
    python scripts/activate_atomic_fx.py
    python scripts/activate_atomic_fx.py --phase verify-only            # read-only migration self-verify
apply (C6-FLIP, must-confirm — 이 unit에선 실행 안 함):
    python scripts/activate_atomic_fx.py --apply --i-understand-this-flips-production \\
        --rds-snapshot-confirmed --phase halt --session-id <id>
    python scripts/activate_atomic_fx.py --apply --i-understand-this-flips-production \\
        --rds-snapshot-confirmed --phase begin-atomic --quiesce-confirmed \\
        --ack-global-writer-mode-scope --required-protocol 2 --target-schema 2 --session-id <id>
        # ⚠️ 현재 image(IMAGE_MAX=1)에선 **capability gate**로 거부됨 — W3 후 main()은 실 RealQuiesceBoundary를
        #    주입(arming)하나, capability gate(IMAGE_MAX=1) + quiesce table 부재(G2a 전)로 begin-atomic은 여전히
        #    차단. C6-FLIP-RELEASE(IMAGE_MAX bump) + G2a + EXEC 후에만 실제 통과(arming ≠ flip).
incident recovery (writer atomic 고착 시):
    python scripts/activate_atomic_fx.py --apply --i-understand-this-flips-production \\
        --phase incident-halt --confirm-incident-halt
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Optional

# scripts는 pytest 밖에서도 실행 — repo root importable 보장
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.atomic_cutover_durable import (  # noqa: E402
    FX_CUTOVER_ASSETS,
    CasResult,
    CutoverAssetView,
    _ready_vector_valid,
    cas_advance_status,
    cas_begin_cutover,
    cas_flip_asset_ready,
    read_cutover_assets,
    read_cutover_control,
)
from app.atomic_fx_v2_loader import load_fx_topic_payload_with_revisions  # noqa: E402
from app.atomic_write_control import (  # noqa: E402
    ATOMIC_SCHEMA_FLOOR,
    CONTROL_ROW_FORMAT_VERSION,
    IMAGE_MAX_WRITER_PROTOCOL,
    IMAGE_MIN_WRITER_PROTOCOL,
    REQUIRED_WRITER_PROTOCOL_SEED,
    WriterMode,
    read_control_row,
)
from app.atomic_write_durable import (  # noqa: E402
    cas_activate_atomic,
    cas_request_halt,
    cas_request_incident_halt,
)
from app.atomic_quiesce_durable import (  # noqa: E402  (W1 halt open / W2 discover+consume / b1 boundary drain proof)
    cas_consume_quiesce_session,
    cas_open_quiesce_session,
    confirm_quiesce_drained,
    find_open_quiesce_session,
)
from app.fx_membership import FX_MEMBERSHIP_VERSION  # noqa: E402
from app.models import get_utc_now  # noqa: E402  (W1 halt quiesce open — barrier ts)

# cutover row format은 cutover CAS가 fence하지 않음(codex B4) → command가 이 값과 일치할 때만 진행.
# (writer 측 CONTROL_ROW_FORMAT_VERSION와 별개 — cutover table은 자체 format 컬럼.)
_EXPECTED_CUTOVER_ROW_FORMAT_VERSION = 1

# exit codes
_EXIT_OK = 0          # applied / plan / noop / verify-only ok
_EXIT_FAILED = 1      # action attempted but failed (CAS non-APPLIED, migration/completeness fail)
_EXIT_REFUSED = 2     # refused before attempt (guards/precondition/format/fail-closed/bad args)


# ────────────────────────────── QuiesceBoundary (machine gate, fail-closed default) ──────────────────────────────
class _FailClosedQuiesceBoundary:
    """begin-atomic machine gate — **항상 fail-closed**(no-op 아님).

    §9:92 activation race: legacy-cached writer가 atomic flip 순간에 unconditional write를 계속하면 invariant가
    깨진다. 안전 진입은 halt commit → 모든 writer가 halt를 관측(**recreate + fresh-process halt ACK** = drain,
    recreate-is-the-drain pivot) → 그제서야 begin-atomic. 그 'drain 완료'를 확인하는 게 QuiesceBoundary다.

    실 boundary(RealQuiesceBoundary: open quiesce session + halt ACK 검증, §9 step6)는 **W3에서 main()이** 같은
    kwarg로 주입(arming). 이 stub은 constructor default(injection 없는 호출/test)에서 항상 False →
    begin-atomic 구조적 거부 = machine-level fail-closed floor.
    """

    def confirm_quiesced(self) -> bool:
        return False


class RealQuiesceBoundary:
    """C6-quiesce Q4b — durable ACK 기반 quiesce 검증 (begin-atomic machine gate).

    `_FailClosedQuiesceBoundary`(항상 False)의 **일반화** — recreate된 fresh app이 durable halt를 관측해 남긴
    ACK(Q4a)가 §9 step6 4조건을 만족할 때만 True. read-only(자체 SessionLocal), **never-raise → False**(stub의
    안전 floor 보존 — 어떤 read/parse/table-absent 실패도 False). consume는 begin-atomic 몫(boundary 아님).

    ⚠️ **W3 arming(C6-FLIP) 완료** — `AtomicFxActivator.__init__` default kwarg는 `_FailClosedQuiesceBoundary`
    유지(constructor는 안전), 단 **`main()`은 W3에서 RealQuiesceBoundary를 주입**해 begin-atomic이 실 drain
    proof를 consult. prod behavior-change-0: capability gate(IMAGE_MAX=1) + quiesce table 부재로 begin-atomic은
    RELEASE/G2a/EXEC 전까지 차단(arming ≠ flip). app/ 아닌 scripts/라 app dormancy trip-wire 무관.

    **b1**: 판정 로직은 공유 `confirm_quiesce_drained(db)`(atomic_quiesce_durable)에 위임 — prod migration
    gate(C6-PRE-build-b)와 **동일 기준**(재인라인 방지). expected_generation=None = begin-atomic은 open
    session의 halt_mode_generation을 권위로(operator pin 없음).

    confirm_quiesced() True 조건 (전부 AND, 공유 helper가 강제):
      STEP1 open quiesce session 존재(partial-unique라 최대 1개) ∧ quiesce_row_format_version==1
        (b1 추가 — corrupt/future quiesce evidence fail-closed).
      FRESH AtomicWriteControl read (defense-in-depth — ACK write 이후 generation bump 포착):
        format==CONTROL_ROW_FORMAT_VERSION / requested_mode==HALT(cond1 live) /
        mode_generation==session.halt_mode_generation(**exact pin** — ACK의 >= 보다 tight, superseded halt
        fail-close) / activation_epoch==0(activation quiesce 한정).
      STEP2 latest qualifying ACK (value-join session_id, observed_at DESC):
        observed_enforced_action=='halt'(cond2) / observed_writer_generation>=session.halt_mode_generation(cond3) /
        process_started_at>session.halt_committed_at(cond4).
    """

    def confirm_quiesced(self) -> bool:
        # b1: §9 step6 drain proof 판정은 공유 helper confirm_quiesce_drained(atomic_quiesce_durable)가 소유 —
        # prod migration gate(C6-PRE-build-b)와 **동일 기준**(open session + fresh control HALT/exact-pin/epoch0/
        # format + quiesce format fence + qualifying ACK)을 쓴다. expected_generation=None = session 신뢰
        # (begin-atomic는 operator pin 없이 open session의 halt_mode_generation을 권위로). never-raise→False.
        db = None
        try:
            from app.database import SessionLocal  # lazy (main() 패턴)

            db = SessionLocal()
            return confirm_quiesce_drained(db)
        except Exception:
            return False  # never-raise: SessionLocal 실패 등도 fail-closed floor (helper 자체도 never-raise)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass


# ────────────────────────────── resume action (pure) ──────────────────────────────
class ResumeAction(Enum):
    HALT = "halt"
    BEGIN_ATOMIC = "begin-atomic"
    VERIFY = "verify"
    FINALIZE = "finalize"
    NOOP = "noop"
    FAIL_CLOSED = "fail-closed"


def resolve_resume_action(
    *, writer_mode: Optional[str], writer_epoch: Optional[int],
    writer_schema_version: Optional[int], cutover_status: Optional[str],
    cutover_session_present: bool, assets_all_blocked: bool, assets_all_ready: bool,
) -> ResumeAction:
    """양축(writer mode + cutover status) + asset state + session/epoch invariant → forward action (pure, fail-closed).

    finalize tx가 단일 atomic(asset flip ×3 + completed)이라 partial state가 없다 → 각 forward 상태는
    asset 구성이 결정적(running/verified = all-blocked, completed = all-ready). 매칭 안 되는 조합은 FAIL_CLOSED
    (수동 incident 평가, 필요 시 --incident-halt). codex B3: asset state를 반드시 포함(stale ready가
    cas_begin_cutover idle fence를 통과해 finalize dead-end 만드는 것 차단).

    session/epoch/schema invariant (adversarial review #1/#3/#7 + cross-check #2):
    - **idle arm**(HALT/BEGIN_ATOMIC) ⟹ first-activation(writer_epoch==0) + cutover session **부재**.
    - **atomic arm**(VERIFY/FINALIZE/NOOP) ⟹ activated(writer_epoch>=1) + schema>=ATOMIC_SCHEMA_FLOOR +
      cutover session **존재**.
    위반(torn/corrupt: NULL-session running·verified, non-null-session idle, legacy+epoch>0, atomic+epoch==0,
    atomic+schema<floor) → FAIL_CLOSED. 핵심 둘: (1) cas_advance_status는 expected_session=None을 `IS NULL`로
    렌더해 NULL-session 행을 **매치**하므로(session fence 무력화) session 부재 atomic을 forward로 보내면 안 됨.
    (2) raw requested_mode='atomic'이라도 schema<floor면 compute_effective_mode가 HALT로 떨궈(writer runtime이
    실제론 halt) — schema 미검증 시 command가 writer-halt 행을 atomic으로 오인해 cutover 전진.
    """
    first = writer_epoch == 0                                       # None==0 → False (safe)
    activated = (isinstance(writer_epoch, int) and not isinstance(writer_epoch, bool)
                 and writer_epoch >= 1)
    schema_ok = (isinstance(writer_schema_version, int) and not isinstance(writer_schema_version, bool)
                 and writer_schema_version >= ATOMIC_SCHEMA_FLOOR)   # compute_effective_mode atomic 조건 정합
    if writer_mode == WriterMode.LEGACY and cutover_status == "idle" and assets_all_blocked \
            and first and not cutover_session_present:
        return ResumeAction.HALT
    if writer_mode == WriterMode.HALT and cutover_status == "idle" and assets_all_blocked \
            and first and not cutover_session_present:
        return ResumeAction.BEGIN_ATOMIC
    if writer_mode == WriterMode.ATOMIC and cutover_status == "running" and assets_all_blocked \
            and activated and schema_ok and cutover_session_present:
        return ResumeAction.VERIFY
    if writer_mode == WriterMode.ATOMIC and cutover_status == "verified" and assets_all_blocked \
            and activated and schema_ok and cutover_session_present:
        return ResumeAction.FINALIZE
    if writer_mode == WriterMode.ATOMIC and cutover_status == "completed" and assets_all_ready \
            and activated and schema_ok and cutover_session_present:
        return ResumeAction.NOOP
    return ResumeAction.FAIL_CLOSED


# ────────────────────────────── state read (B1: writer from DB row, not process cache) ──────────────────────────────
def _assets_all_blocked(assets: Mapping[str, CutoverAssetView]) -> bool:
    """3 asset 전부 존재 + publish_state='blocked' + readiness payload NULL(pre-cutover canonical)."""
    if set(assets.keys()) != set(FX_CUTOVER_ASSETS):
        return False
    return all(
        a.publish_state == "blocked"
        and a.ready_revision_vector is None
        and a.membership_version is None
        for a in assets.values()
    )


def _assets_all_ready(assets: Mapping[str, CutoverAssetView]) -> bool:
    """3 asset 전부 존재 + publish_state='ready' + **gate-valid** readiness payload (post-finalize canonical).

    cross-check #1: derive_cutover_state의 ATOMIC_READY 기준과 일치시킨다 — membership_version==FX_MEMBERSHIP_
    VERSION + ready_revision_vector 구조유효. non-null만 보면 stale-membership/invalid-vector 행이 NOOP으로
    오판(runtime gate는 HALT_BLOCKED)되므로, NOOP은 gate-valid ready만 의미해야 함(아니면 resolver FAIL_CLOSED).
    """
    if set(assets.keys()) != set(FX_CUTOVER_ASSETS):
        return False
    return all(
        a.publish_state == "ready"
        and a.membership_version == FX_MEMBERSHIP_VERSION
        and _ready_vector_valid(a.ready_revision_vector)
        for a in assets.values()
    )


@dataclass(frozen=True)
class ControlState:
    """fresh 양축 DB read (resume/guard 기반). writer 축은 DB row 직접(codex B1)."""
    writer_present: bool
    writer_mode: Optional[str]
    writer_generation: Optional[int]
    writer_epoch: Optional[int]
    writer_schema_version: Optional[int]
    writer_format_version: Optional[int]
    cutover_present: bool
    cutover_status: Optional[str]
    cutover_generation: Optional[int]
    cutover_session_id: Optional[str]
    cutover_format_version: Optional[int]
    assets: Mapping[str, CutoverAssetView]
    assets_all_blocked: bool
    assets_all_ready: bool


def read_control_state(db) -> ControlState:
    """atomic_write_control + atomic_cutover_control + per-asset row를 fresh read (read-only)."""
    w = read_control_row(db)               # AtomicWriteControl | None (DB 직접)
    c = read_cutover_control(db)           # CutoverControlView | None
    assets = read_cutover_assets(db)       # {asset: CutoverAssetView}
    return ControlState(
        writer_present=w is not None,
        writer_mode=(w.requested_mode if w is not None else None),
        writer_generation=(w.mode_generation if w is not None else None),
        writer_epoch=(w.activation_epoch if w is not None else None),
        writer_schema_version=(w.target_write_schema_version if w is not None else None),
        writer_format_version=(w.control_row_format_version if w is not None else None),
        cutover_present=c is not None,
        cutover_status=(c.status if c is not None else None),
        cutover_generation=(c.generation if c is not None else None),
        cutover_session_id=(c.session_id if c is not None else None),
        cutover_format_version=(c.format_version if c is not None else None),
        assets=assets,
        assets_all_blocked=_assets_all_blocked(assets),
        assets_all_ready=_assets_all_ready(assets),
    )


# ────────────────────────────── migration self-verify (C6-3 loader) ──────────────────────────────
@dataclass(frozen=True)
class AssetVerify:
    asset: str
    present: List[str]
    effective: List[str]
    ok: bool


def migration_self_verify(db, asset: str) -> AssetVerify:
    """C6-3 loader로 effective==present AND present non-empty (migration 완료 self-check, read-only).

    effective ⊆ present이고 ==는 'present한 모든 source가 valid v2 revision 보유' = migration 완료. present
    non-empty(codex Q3): ∅==∅도 ==이지만 빈 vector는 ready flip 불가(_ready_vector_valid 거부)이므로 명시 차단.
    실 v1→v2 value migration은 C6-PRE(must-confirm prerequisite); verify는 gate일 뿐.
    """
    result = load_fx_topic_payload_with_revisions(db, asset)
    present = set(result.present_sources)
    effective = set(result.effective_revision_vector)
    ok = bool(present) and effective == present
    return AssetVerify(asset=asset, present=sorted(present), effective=sorted(effective), ok=ok)


def build_ready_revision_vector(db, asset: str) -> Optional[str]:
    """finalize용 ready_revision_vector JSON — effective==present non-empty면 직렬화, 아니면 None(completeness fail).

    finalize tx 안에서 fresh re-load(TOCTOU re-check). _ready_vector_valid/cas_flip_asset_ready 검증을 통과하는
    {source: revision_key} 직렬화(sort_keys 결정적).
    """
    result = load_fx_topic_payload_with_revisions(db, asset)
    present = set(result.present_sources)
    effective = set(result.effective_revision_vector)
    if not present or effective != present:
        return None
    return json.dumps(result.effective_revision_vector, sort_keys=True)


# ────────────────────────────── phase outcome ──────────────────────────────
@dataclass
class PhaseOutcome:
    exit_code: int
    action: str
    status: str          # applied | plan | noop | refused | failed | verify-only
    message: str
    detail: dict = field(default_factory=dict)


# ────────────────────────────── activator ──────────────────────────────
class AtomicFxActivator:
    """양축 CAS brick + loader self-verify를 phase별로 합성. quiesce_boundary는 injectable-with-default
    (constructor default = _FailClosedQuiesceBoundary stub; **main()은 W3에서 RealQuiesceBoundary 주입**).
    live caller 0(scripts/ 전용)."""

    def __init__(self, db, *, quiesce_boundary: Optional[_FailClosedQuiesceBoundary] = None) -> None:
        self.db = db
        self.quiesce_boundary = quiesce_boundary if quiesce_boundary is not None else _FailClosedQuiesceBoundary()

    # ── apply dispatch ──
    def run(self, args: argparse.Namespace) -> PhaseOutcome:
        state = read_control_state(self.db)

        # precondition: control rows seeded
        if not state.writer_present or not state.cutover_present:
            return self._refuse(
                "activate",
                "control rows not seeded — run scripts/migrate_atomic_write_control.py + "
                "scripts/migrate_atomic_cutover.py first "
                f"(writer_present={state.writer_present}, cutover_present={state.cutover_present})",
            )

        # verify-only = read-only 진단 (review #5): control row format에 의존하지 않는 loader self-verify이므로
        # format fence보다 먼저 dispatch — 포맷 mismatch(부패/미래 포맷) 조사 시에도 안전한 진단을 제공.
        if args.phase == "verify-only":
            return self._verify_only(state)

        # B4 format fence (cutover CAS는 format을 fence 안 함 → command가 명시 거부). mutating/forward path 전용.
        if state.writer_format_version != CONTROL_ROW_FORMAT_VERSION:
            return self._refuse("activate", f"writer control_row_format_version="
                                f"{state.writer_format_version} != {CONTROL_ROW_FORMAT_VERSION} — refuse")
        if state.cutover_format_version != _EXPECTED_CUTOVER_ROW_FORMAT_VERSION:
            return self._refuse("activate", f"cutover_row_format_version="
                                f"{state.cutover_format_version} != {_EXPECTED_CUTOVER_ROW_FORMAT_VERSION} — refuse")

        # incident-halt = apply path (format fence 후)
        if args.phase == "incident-halt":
            return self._incident_halt_flow(state, args)

        # forward flow
        resolved = resolve_resume_action(
            writer_mode=state.writer_mode, writer_epoch=state.writer_epoch,
            writer_schema_version=state.writer_schema_version,
            cutover_status=state.cutover_status,
            cutover_session_present=(state.cutover_session_id is not None),
            assets_all_blocked=state.assets_all_blocked, assets_all_ready=state.assets_all_ready,
        )
        # explicit --phase는 resolved와 일치해야(state 오인 방지)
        if args.phase != "auto" and args.phase != resolved.value:
            return self._refuse(
                resolved.value,
                f"--phase={args.phase} 이지만 현재 state는 {resolved.value}를 함의 — state mismatch, refuse "
                f"(writer={state.writer_mode}, cutover={state.cutover_status}, "
                f"blocked={state.assets_all_blocked}, ready={state.assets_all_ready})",
            )

        if resolved is ResumeAction.NOOP:
            return PhaseOutcome(_EXIT_OK, "noop", "noop",
                                "already atomic+completed+all-ready — nothing to do "
                                "(re-run migration self-verify via --phase verify-only)")
        if resolved is ResumeAction.FAIL_CLOSED:
            return self._refuse(
                "fail-closed",
                "unexpected control-plane state — manual incident assessment required "
                f"(writer={state.writer_mode}, cutover={state.cutover_status}, "
                f"blocked={state.assets_all_blocked}, ready={state.assets_all_ready}). "
                "writer atomic 고착 시 --phase incident-halt.",
            )

        # expected-* mismatch = hard refuse (operator 가정 오류 — 항상 거부, dry-run 포함)
        mismatches = self._expected_mismatches(resolved, state, args)
        if mismatches:
            return self._refuse(resolved.value, "expected-state guard mismatch:\n  - " + "\n  - ".join(mismatches))

        # apply-only 요건 (ack + capability + machine gate) — dry-run에선 표시만
        blocking = self._apply_acks(resolved, args)
        if resolved is ResumeAction.BEGIN_ATOMIC:
            blocking += self._begin_atomic_capability(args)
            # review #8: machine gate(boundary)를 plan blocking에 surface — dry-run이 apply 결과를 정직하게 예측.
            # (boundary machine-gate는 _do_begin_atomic:516에도 backstop; **capability(IMAGE_MAX) gate는 run()
            #  전용** — _do_begin_atomic 미backstop[cas_activate_atomic는 protocol/schema precondition만], run()
            #  path가 항상 거치므로 충분.)
            if not self.quiesce_boundary.confirm_quiesced():
                blocking.append("QuiesceBoundary machine-gate — drain 미확인 (RealQuiesceBoundary[main()이 W3에서 "
                                "주입]가 open quiesce session + halt ACK 요구; halt+recreate-drain 선행 필요, §9:92 race)")
            else:
                # W2 parity(codex): boundary 통과 시 discovered quiesce session 일관성을 plan에 surface —
                # _do_begin_atomic이 find-None/--session-id mismatch 시 refuse하므로 dry-run이 미리 노출(apply 예측).
                # boundary 통과 후에만 find 호출(table-absent 시 RealBoundary가 이미 False → 이 분기 미진입).
                qsession = find_open_quiesce_session(self.db)
                if qsession is None:
                    blocking.append("open quiesce session 없음 — halt(W1) 선행 필요(drain 미완)")
                elif args.session_id is not None and args.session_id != qsession.session_id:
                    blocking.append(f"--session-id={args.session_id} != open quiesce session"
                                    f"({qsession.session_id}) — operator mismatch(DB-authoritative)")
        if not args.apply:
            return self._plan(state, resolved, blocking)
        if blocking:
            return self._refuse(resolved.value, "cannot apply — unmet requirements:\n  - " + "\n  - ".join(blocking))

        return self._dispatch_apply(resolved, state, args)

    def _dispatch_apply(self, resolved: ResumeAction, state: ControlState,
                        args: argparse.Namespace) -> PhaseOutcome:
        if resolved is ResumeAction.HALT:
            return self._do_halt(state, args)
        if resolved is ResumeAction.BEGIN_ATOMIC:
            return self._do_begin_atomic(state, args)
        if resolved is ResumeAction.VERIFY:
            return self._do_verify(state)
        if resolved is ResumeAction.FINALIZE:
            return self._do_finalize(state)
        return self._refuse(resolved.value, f"no apply handler for {resolved.value}")  # 도달 불가

    # ── phases (each owns its tx; caller-commits → command commits/rolls back) ──
    def _do_halt(self, state: ControlState, args: argparse.Namespace) -> PhaseOutcome:
        """legacy→halt durable pre-quiesce + activation quiesce session open (§9:88, W0 계약 — ACK보다 먼저 commit).

        W1: cas_request_halt APPLIED 직후 **같은 tx**에서 cas_open_quiesce_session(activation lineage id 확립)
        → 1 commit. **원자성**: cas_open 실패 시 halt도 rollback(부분 적용 방지). halt_mode_generation =
        state.writer_generation+1(staged halt gen — cas_open이 populate_existing 재read로 pin 검증). halt_committed_at
        = durable halt barrier ts(get_utc_now; DB commit ts 아님 — recreate된 fresh process의 process_started_at >
        barrier로 ACK cond4 충족, 구 pre-halt process는 거부). incident-halt는 별 phase(cas_request_incident_halt,
        epoch>=1)라 cas_open 미경유 — cas_open의 activation_epoch==0 fence와 정합.
        """
        db = self.db
        try:
            r = cas_request_halt(db, expected_generation=state.writer_generation)
            if r is not CasResult.APPLIED:
                db.rollback()
                return self._failed("halt", f"cas_request_halt → {r.value}")
            q = cas_open_quiesce_session(
                db, session_id=args.session_id,
                halt_mode_generation=state.writer_generation + 1,
                halt_committed_at=get_utc_now(),
            )
            if q is not CasResult.APPLIED:
                db.rollback()
                return self._failed("halt", f"cas_open_quiesce_session → {q.value}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        return PhaseOutcome(
            _EXIT_OK, "halt", "applied",
            "writer legacy→halt + activation quiesce session open 적용(durable, 1 tx — W0 lineage id). 다음: 모든 "
            "writer가 halt 관측하도록 app recreate/quiesce 후 begin-atomic. ⚠️ quiesce drain 확인 전 begin-atomic "
            "금지(§9:92 activation race).",
            {"writer_generation": state.writer_generation + 1, "session_id": args.session_id},
        )

    def _do_begin_atomic(self, state: ControlState, args: argparse.Namespace) -> PhaseOutcome:
        """halt→atomic + cutover idle→running + quiesce consume, **단일 tx**(codex: stale completed/ready 상속 방지).
        결과 = §15 atomic/blocked(writer atomic live, publish gate closed). W2(W0 계약): cutover bootstrap session
        + consume 모두 **DB-authoritative discovered** quiesce session_id(single shared id) — args 불신."""
        # machine gate. main()은 W3에서 RealQuiesceBoundary 주입(constructor default는 _FailClosedQuiesceBoundary).
        # human ack는 _apply_acks에서 이미 검사됨.
        if not self.quiesce_boundary.confirm_quiesced():
            return self._refuse(
                "begin-atomic",
                "QuiesceBoundary machine-gate FAILED — drain 미확인. RealQuiesceBoundary(main()이 W3에서 주입)가 "
                "open quiesce session + halt ACK(§9 step6 drain proof)를 요구 — halt+recreate-drain 선행 필요. "
                "begin-atomic 거부(§9:92 activation race 방어).",
            )
        db = self.db
        # W0: DB-authoritative discover — halt(W1)가 open한 quiesce session을 begin tx의 single shared id로 사용.
        session = find_open_quiesce_session(db)
        if session is None:
            return self._refuse(
                "begin-atomic",
                "open quiesce session 없음 — halt(W1)가 session open 안 했거나 이미 consume됨. drain 미완 → 거부.",
            )
        # W0 #3: --session-id는 optional cross-check (주면 discovered와 일치해야 — operator-error 검출, DB 권위 불변).
        if args.session_id is not None and args.session_id != session.session_id:
            return self._refuse(
                "begin-atomic",
                f"--session-id={args.session_id} != open quiesce session({session.session_id}) — operator "
                "mismatch, 거부(DB-authoritative).",
            )
        lineage = session.session_id
        try:
            r1 = cas_activate_atomic(
                db, expected_generation=state.writer_generation, expected_epoch=state.writer_epoch,
                required_writer_protocol=args.required_protocol, target_write_schema_version=args.target_schema,
            )
            if r1 is not CasResult.APPLIED:
                db.rollback()
                return self._failed("begin-atomic", f"cas_activate_atomic → {r1.value}")
            r2 = cas_begin_cutover(db, new_session=lineage, expected_generation=state.cutover_generation)
            if r2 is not CasResult.APPLIED:
                db.rollback()
                return self._failed("begin-atomic", f"cas_begin_cutover → {r2.value}")
            r3 = cas_consume_quiesce_session(db, session_id=lineage)
            if r3 is not CasResult.APPLIED:
                db.rollback()
                return self._failed("begin-atomic", f"cas_consume_quiesce_session → {r3.value}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        return PhaseOutcome(
            _EXIT_OK, "begin-atomic", "applied",
            "writer halt→atomic + cutover idle→running + quiesce consume 적용(1 tx, atomic/blocked). 다음: §9:90 "
            "app recreate-atomic(writer가 atomic mode + cache refresh) → verify(migration self-verify). "
            "publish gate는 finalize까지 닫혀 있음.",
            {"writer_generation": state.writer_generation + 1, "writer_epoch": state.writer_epoch + 1,
             "cutover_generation": state.cutover_generation + 1, "session_id": lineage},
        )

    def _do_verify(self, state: ControlState) -> PhaseOutcome:
        """migration self-verify(3 asset effective==present) 통과 시 running→verified."""
        db = self.db
        # loader(live Redis GET + DB fallback) 예외를 raw traceback 대신 actionable outcome으로 (review #11)
        try:
            verifies = [migration_self_verify(db, a) for a in FX_CUTOVER_ASSETS]
        except Exception as e:
            db.rollback()
            return self._failed("verify", f"migration self-verify 중 loader 예외(Redis/DB): "
                                f"{type(e).__name__}. mutation 0. writer atomic 고착 시 --phase incident-halt.")
        detail = {"verify": [v.__dict__ for v in verifies]}
        failed = [v for v in verifies if not v.ok]
        if failed:
            return self._failed(
                "verify",
                "migration self-verify 실패(effective≠present 또는 present 비어있음): "
                + ", ".join(f"{v.asset}(present={v.present}, effective={v.effective})" for v in failed)
                + ". 실 v1→v2 migration(C6-PRE) 미완 가능. writer atomic 고착 시 --phase incident-halt.",
                detail,
            )
        try:
            r = cas_advance_status(
                db, expected_session=state.cutover_session_id, expected_status="running",
                new_status="verified", expected_generation=state.cutover_generation,
            )
            if r is not CasResult.APPLIED:
                db.rollback()
                return self._failed("verify", f"cas_advance_status(running→verified) → {r.value}", detail)
            db.commit()
        except Exception:
            db.rollback()
            raise
        detail["cutover_generation"] = state.cutover_generation + 1
        return PhaseOutcome(_EXIT_OK, "verify", "applied",
                            "migration self-verify 통과 → cutover running→verified. 다음: finalize.", detail)

    def _do_finalize(self, state: ControlState) -> PhaseOutcome:
        """asset blocked→ready ×3 **먼저**, status verified→completed **마지막**, 단일 tx(§15-5).
        per-asset completeness gate(TOCTOU fresh re-load). 임의 non-APPLIED → rollback."""
        db = self.db
        # TOCTOU re-check + vector build (commit 전, completeness gate). loader 예외도 actionable outcome (review #11).
        vectors: Dict[str, str] = {}
        try:
            for asset in FX_CUTOVER_ASSETS:
                vec = build_ready_revision_vector(db, asset)
                if vec is None:
                    return self._failed(
                        "finalize",
                        f"finalize completeness gate 실패 — {asset} effective≠present 또는 present 비어있음 "
                        "(verify 이후 source 후퇴 가능). finalize 중단(mutation 0). writer atomic 고착 시 "
                        "--phase incident-halt.",
                    )
                vectors[asset] = vec
        except Exception as e:
            db.rollback()
            return self._failed("finalize", f"finalize vector build 중 loader 예외(Redis/DB): "
                                f"{type(e).__name__}. mutation 0. writer atomic 고착 시 --phase incident-halt.")
        try:
            # asset flip 먼저 (no status/gen fence — 같은 tx의 control fence가 안전 제공)
            for asset in FX_CUTOVER_ASSETS:
                r = cas_flip_asset_ready(
                    db, asset, ready_revision_vector=vectors[asset], membership_version=FX_MEMBERSHIP_VERSION,
                )
                if r is not CasResult.APPLIED:
                    db.rollback()
                    return self._failed("finalize", f"cas_flip_asset_ready[{asset}] → {r.value}")
            # status 마지막 (verified→completed = commit marker)
            r = cas_advance_status(
                db, expected_session=state.cutover_session_id, expected_status="verified",
                new_status="completed", expected_generation=state.cutover_generation,
            )
            if r is not CasResult.APPLIED:
                db.rollback()
                return self._failed("finalize", f"cas_advance_status(verified→completed) → {r.value}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        return PhaseOutcome(
            _EXIT_OK, "finalize", "applied",
            "3 asset blocked→ready + cutover verified→completed(1 tx) → ATOMIC_READY. FX publish gate OPEN. "
            "cutover 완료.",
            {"cutover_generation": state.cutover_generation + 1, "membership_version": FX_MEMBERSHIP_VERSION},
        )

    def _incident_halt_flow(self, state: ControlState, args: argparse.Namespace) -> PhaseOutcome:
        """atomic→halt 응급 복구(C6-8b-1). begin-atomic 이후 verify/finalize 실패로 writer atomic 고착 시."""
        if state.writer_mode != WriterMode.ATOMIC:
            return self._refuse("incident-halt",
                                f"incident-halt는 writer=atomic에서만 (현재 writer={state.writer_mode})")
        # expected-* guard 먼저 (review #4: forward flow 대칭 — dry-run plan도 mismatch를 반영하도록 split 전에)
        if args.expected_writer_generation is not None \
                and args.expected_writer_generation != state.writer_generation:
            return self._refuse("incident-halt",
                                f"--expected-writer-generation={args.expected_writer_generation} != "
                                f"observed {state.writer_generation}")
        # acks
        missing = []
        if not args.i_understand_this_flips_production:
            missing.append("--i-understand-this-flips-production")
        if not args.confirm_incident_halt:
            missing.append("--confirm-incident-halt")
        if not args.apply:
            return self._plan_incident(state, missing)
        if missing:
            return self._refuse("incident-halt", "cannot apply — unmet requirements:\n  - " + "\n  - ".join(missing))
        db = self.db
        try:
            r = cas_request_incident_halt(db, expected_generation=state.writer_generation)
            if r is not CasResult.APPLIED:
                db.rollback()
                return self._failed("incident-halt", f"cas_request_incident_halt → {r.value}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        return PhaseOutcome(
            _EXIT_OK, "incident-halt", "applied",
            "writer atomic→halt 적용(incident). publish gate closed(writer halt가 derive_cutover_state 지배). "
            "baseline 서비스(legacy WS broadcast)는 독립. activation_epoch 보존(§3 atomic·halt→legacy 금지). "
            "원인 조사 후 재개(post-finalize re-cutover는 별도 unit).",
            {"writer_generation": state.writer_generation + 1},
        )

    def _verify_only(self, state: ControlState) -> PhaseOutcome:
        """read-only 진단 — migration self-verify만 출력(mutation 0)."""
        verifies = [migration_self_verify(self.db, a) for a in FX_CUTOVER_ASSETS]
        all_ok = all(v.ok for v in verifies)
        detail = {"verify": [v.__dict__ for v in verifies]}
        msg = "migration self-verify (read-only):\n" + "\n".join(
            f"  {v.asset}: {'OK' if v.ok else 'FAIL'} present={v.present} effective={v.effective}"
            for v in verifies
        )
        return PhaseOutcome(_EXIT_OK, "verify-only", "verify-only", msg, detail | {"all_ok": all_ok})

    # ── guard helpers ──
    def _expected_mismatches(self, resolved: ResumeAction, state: ControlState,
                             args: argparse.Namespace) -> List[str]:
        """operator --expected-*(제공 시) vs fresh observed 비교 (codex A4: guard only, brick엔 observed 전달)."""
        out: List[str] = []
        if args.expected_writer_generation is not None \
                and args.expected_writer_generation != state.writer_generation:
            out.append(f"--expected-writer-generation={args.expected_writer_generation} != "
                       f"observed {state.writer_generation}")
        if args.expected_writer_epoch is not None and args.expected_writer_epoch != state.writer_epoch:
            out.append(f"--expected-writer-epoch={args.expected_writer_epoch} != observed {state.writer_epoch}")
        if args.expected_cutover_generation is not None \
                and args.expected_cutover_generation != state.cutover_generation:
            out.append(f"--expected-cutover-generation={args.expected_cutover_generation} != "
                       f"observed {state.cutover_generation}")
        if args.expected_cutover_status is not None and args.expected_cutover_status != state.cutover_status:
            out.append(f"--expected-cutover-status={args.expected_cutover_status} != "
                       f"observed {state.cutover_status}")
        return out

    def _apply_acks(self, resolved: ResumeAction, args: argparse.Namespace) -> List[str]:
        """forward apply 공통 ack + begin-atomic 추가 ack (apply 시에만 필요)."""
        out: List[str] = []
        if not args.i_understand_this_flips_production:
            out.append("--i-understand-this-flips-production")
        if not args.rds_snapshot_confirmed:
            out.append("--rds-snapshot-confirmed")
        if resolved is ResumeAction.HALT:
            if not args.session_id:
                out.append("--session-id (activation lineage id — halt가 quiesce session open, W0)")
        if resolved is ResumeAction.BEGIN_ATOMIC:
            if not args.quiesce_confirmed:
                out.append("--quiesce-confirmed (§9 quiesce handshake 완료 human ack)")
            if not args.ack_global_writer_mode_scope:
                out.append("--ack-global-writer-mode-scope (requested_mode=atomic은 GLOBAL — USDT/KRX/mirror "
                           "writer가 BLOCKED fail-closed; C6-5b atomicization 확인)")
            # W0 #3: begin-atomic --session-id는 optional cross-check(DB-authoritative discover) — 필수 아님.
        return out

    def _begin_atomic_capability(self, args: argparse.Namespace) -> List[str]:
        """B2 image-capability hard gate — 현재 image(IMAGE_MAX=1)에선 불충족 → begin-atomic hard-fail."""
        out: List[str] = []
        if args.required_protocol is None:
            out.append("--required-protocol")
        if args.target_schema is None:
            out.append("--target-schema")
        if args.required_protocol is not None:
            if not (IMAGE_MIN_WRITER_PROTOCOL <= args.required_protocol <= IMAGE_MAX_WRITER_PROTOCOL):
                out.append(f"--required-protocol={args.required_protocol}이 실행 image 범위 "
                           f"[{IMAGE_MIN_WRITER_PROTOCOL}, {IMAGE_MAX_WRITER_PROTOCOL}] 밖 — image가 이 writer "
                           "protocol 미지원(§6 preflight). C6-FLIP release가 IMAGE_MAX bump + atomic writer 배선 "
                           "후 통과.")
            if args.required_protocol <= REQUIRED_WRITER_PROTOCOL_SEED:
                out.append(f"--required-protocol은 > {REQUIRED_WRITER_PROTOCOL_SEED}(legacy seed)여야 — atomic "
                           "activation은 writer protocol을 legacy 이상으로 전진시켜야 함")
        if args.target_schema is not None and args.target_schema < ATOMIC_SCHEMA_FLOOR:
            out.append(f"--target-schema은 >= {ATOMIC_SCHEMA_FLOOR}(atomic schema floor)여야 함")
        return out

    # ── outcome builders ──
    def _refuse(self, action: str, message: str) -> PhaseOutcome:
        return PhaseOutcome(_EXIT_REFUSED, action, "refused", message)

    def _failed(self, action: str, message: str, detail: Optional[dict] = None) -> PhaseOutcome:
        return PhaseOutcome(_EXIT_FAILED, action, "failed", message, detail or {})

    def _plan(self, state: ControlState, resolved: ResumeAction, blocking: List[str]) -> PhaseOutcome:
        msg = render_plan(state, resolved, blocking)
        return PhaseOutcome(_EXIT_OK, resolved.value, "plan", msg,
                            {"resolved": resolved.value, "blocking": blocking})

    def _plan_incident(self, state: ControlState, missing: List[str]) -> PhaseOutcome:
        lines = ["=" * 78, "  ATOMIC FX ACTIVATION — DRY-RUN PLAN (incident-halt)", "=" * 78,
                 f"writer: mode={state.writer_mode} generation={state.writer_generation} epoch={state.writer_epoch}",
                 "action: cas_request_incident_halt (atomic→halt, activation_epoch 보존)"]
        if missing:
            lines.append("apply하려면 필요: " + ", ".join(missing) + " + --apply")
        lines.append("=" * 78)
        return PhaseOutcome(_EXIT_OK, "incident-halt", "plan", "\n".join(lines))


# ────────────────────────────── plan rendering ──────────────────────────────
def render_plan(state: ControlState, resolved: ResumeAction, blocking: List[str]) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("  ATOMIC FX ACTIVATION — DRY-RUN PLAN (mutation 0; --apply로만 실행)")
    lines.append("=" * 78)
    lines.append(f"writer : mode={state.writer_mode} generation={state.writer_generation} "
                 f"epoch={state.writer_epoch} format={state.writer_format_version}")
    lines.append(f"cutover: status={state.cutover_status} generation={state.cutover_generation} "
                 f"session={state.cutover_session_id} format={state.cutover_format_version}")
    lines.append("assets :")
    for asset in FX_CUTOVER_ASSETS:
        a = state.assets.get(asset)
        if a is None:
            lines.append(f"  {asset}: (없음)")
        else:
            lines.append(f"  {asset}: publish_state={a.publish_state} "
                         f"membership_version={a.membership_version}")
    lines.append("")
    lines.append(f"resolved action: {resolved.value}")
    lines.append(_action_summary(resolved))
    if blocking:
        lines.append("")
        lines.append("⚠️ apply하려면 먼저 제공:")
        for b in blocking:
            lines.append(f"  - {b}")
        lines.append("  - --apply")
    lines.append("")
    lines.append("runbook:")
    for ln in _runbook_lines():
        lines.append(f"  {ln}")
    lines.append("=" * 78)
    return "\n".join(lines)


def _action_summary(resolved: ResumeAction) -> str:
    return {
        ResumeAction.HALT: "  → cas_request_halt (legacy→halt, durable pre-quiesce §9:88)",
        ResumeAction.BEGIN_ATOMIC: "  → cas_activate_atomic + cas_begin_cutover (1 tx, halt→atomic + "
                                   "idle→running = atomic/blocked)",
        ResumeAction.VERIFY: "  → migration self-verify(3 asset) → cas_advance_status(running→verified)",
        ResumeAction.FINALIZE: "  → cas_flip_asset_ready ×3 + cas_advance_status(verified→completed), 1 tx "
                               "→ ATOMIC_READY",
        ResumeAction.NOOP: "  → 이미 완료 (nothing to do)",
        ResumeAction.FAIL_CLOSED: "  → fail-closed (수동 incident 평가)",
    }.get(resolved, "")


def _runbook_lines() -> List[str]:
    return [
        "0. (precondition) requested_mode=atomic은 GLOBAL — halt 전에 USDT/KRX/mirror writer가 atomic(C6-5b) "
        "또는 frozen 상태인지 확인. 미충족 시 해당 writer가 BLOCKED fail-closed(FX publisher scope는 "
        "bank+investing뿐이나 writer-mode는 전역). --ack-global-writer-mode-scope로 ack.",
        "1. halt 적용 후: app recreate (= drain — fresh process가 halt 관측 후 halt ACK 기록, §9 quiesce; "
        "recreate-is-the-drain).",
        "2. quiesce drain 확인 후에만 begin-atomic (§9:92 activation race — 미드레인 시 invariant 깨짐).",
        "3. begin-atomic 후: app recreate-atomic으로 writer가 atomic mode + runtime cache refresh (§9:90).",
        "4. verify → finalize 순으로 진행. 각 phase는 fresh state 재read 후 CAS fence.",
        "5. 실패/고착 시: --phase incident-halt (atomic→halt). rollback=halt이지 atomic→legacy 아님(§3 금지).",
    ]


# ────────────────────────────── CLI ──────────────────────────────
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="C6-8b-2 atomic FX activation command (dormant — 실행은 C6-FLIP must-confirm)"
    )
    p.add_argument("--apply", action="store_true",
                   help="실제 적용 (없으면 dry-run plan만, mutation 0)")
    p.add_argument("--phase", choices=["auto", "halt", "begin-atomic", "verify", "finalize",
                                       "incident-halt", "verify-only"], default="auto",
                   help="auto=crash-resume 판정; forward phase 명시 시 resolved와 일치해야; "
                        "incident-halt/verify-only는 독립 모드")
    p.add_argument("--i-understand-this-flips-production", action="store_true",
                   help="irreversibility ack (forward apply + incident-halt 필수)")
    p.add_argument("--rds-snapshot-confirmed", action="store_true",
                   help="RDS snapshot 생성 확인 ack (forward apply 필수 — 복구 anchor)")
    p.add_argument("--quiesce-confirmed", action="store_true",
                   help="§9 quiesce handshake 완료 human ack (begin-atomic 필수; machine gate와 별개)")
    p.add_argument("--ack-global-writer-mode-scope", action="store_true",
                   help="requested_mode=atomic이 GLOBAL(USDT/KRX/mirror 영향)임을 ack (begin-atomic 필수)")
    p.add_argument("--confirm-incident-halt", action="store_true",
                   help="incident-halt(atomic→halt) 확인 ack")
    p.add_argument("--required-protocol", type=int, default=None,
                   help="begin-atomic이 설정할 required_writer_protocol (image 범위 + > legacy seed)")
    p.add_argument("--target-schema", type=int, default=None,
                   help="begin-atomic이 설정할 target_write_schema_version (>= atomic schema floor)")
    p.add_argument("--session-id", type=str, default=None,
                   help="activation lineage id — halt가 quiesce session open(W0) + begin-atomic cutover bootstrap "
                        "(single shared id)")
    p.add_argument("--expected-writer-generation", type=int, default=None,
                   help="guard: 현재 writer mode_generation (불일치 시 거부)")
    p.add_argument("--expected-writer-epoch", type=int, default=None,
                   help="guard: 현재 writer activation_epoch")
    p.add_argument("--expected-cutover-generation", type=int, default=None,
                   help="guard: 현재 cutover bootstrap_generation")
    p.add_argument("--expected-cutover-status", type=str, default=None,
                   help="guard: 현재 cutover bootstrap_status")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    # lazy import (app.database는 conftest의 DATABASE_URL override 후 import — 직접 실행 시는 .env)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        # W3 arming (C6-FLIP): 실 RealQuiesceBoundary 주입 — begin-atomic이 §9 step6 drain proof(open
        # quiesce session + ACK)를 실제 consult. constructor default는 _FailClosedQuiesceBoundary 유지(main만 주입).
        # prod behavior-change-0: capability gate(IMAGE_MAX=1) + quiesce table 부재로 begin-atomic은
        # RELEASE/G2a/EXEC 전까지 여전히 차단(arming ≠ flip).
        outcome = AtomicFxActivator(db, quiesce_boundary=RealQuiesceBoundary()).run(args)
    finally:
        try:
            db.close()
        except Exception:
            pass

    stream = sys.stdout if outcome.exit_code == _EXIT_OK else sys.stderr
    print(f"[{outcome.status.upper()}] action={outcome.action}", file=stream)
    print(outcome.message, file=stream)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
