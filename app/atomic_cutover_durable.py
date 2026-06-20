"""P1b C6-2 — cutover durable read + state derivation + CAS bricks (§15/§18, dormant).

C6-1 cutover tables(AtomicCutoverControl/AtomicCutoverAsset) 위의 **durable 계층**: (1) read helper +
ORM-decoupled DTO, (2) pure derive_cutover_state(→ A5 CutoverState, fail-closed + ready_revision_vector
구조 검증), (3) control-plane readiness predicate, (4) **CAS bricks**(§15 transition별 단일 fenced
conditional-update, caller-commits — C6-8이 §15-5 finalize를 1-tx로 합성).

**dormant**: live caller 0 (AST trip-wire). injected db로 read/CAS하나 어떤 live 모듈도 import/호출 안 함
("db param 있음" ≠ "wired"). 호출자 연결(snapshot refresh / activation command)은 C6-runtime / C6-8.
A5 atomic_cutover(pure enum/validator)는 무접촉 재사용. §8 one-shot(AtomicWriteControl writer-mode)은
**C6-2 범위 밖**(C6-8) — 본 모듈은 cutover 2 table만 건드림.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, Mapping, Optional

from sqlalchemy import or_

from app.atomic_cutover import CutoverState, is_allowed_transition
from app.atomic_value_schema import parse_revision_key
from app.atomic_write_control import WriterMode
from app.fx_membership import FX_MEMBERSHIP_SOURCES
from app.models import AtomicCutoverAsset, AtomicCutoverControl

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# cutover per-asset domain (C6-1 CHECK enum과 일치 — §15 first cutover all-at-once 3 asset)
FX_CUTOVER_ASSETS = ("usd-krw", "jpy-krw", "eur-krw")
# bootstrap_status 유효값 (C6-1 CHECK enum과 일치)
_VALID_STATUSES = frozenset({"idle", "running", "failed", "verified", "completed"})


# ────────────────────────────── read DTOs (ORM-decoupled) ──────────────────────────────


@dataclass(frozen=True)
class CutoverControlView:
    """AtomicCutoverControl 읽기 스냅(ORM lifecycle 분리, A2-4/B2a 선례)."""
    format_version: int
    session_id: Optional[str]
    generation: int
    status: str
    lease_owner: Optional[str]
    lease_expiry: object  # datetime | None (TYPE 결합 회피)


@dataclass(frozen=True)
class CutoverAssetView:
    asset: str
    publish_state: str
    ready_revision_vector: Optional[str]
    membership_version: Optional[int]


def read_cutover_control(db: "Session") -> Optional[CutoverControlView]:
    """singleton(id=1) → CutoverControlView | None (부재). read-only."""
    row = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one_or_none()
    if row is None:
        return None
    return CutoverControlView(
        format_version=row.cutover_row_format_version, session_id=row.bootstrap_session_id,
        generation=row.bootstrap_generation, status=row.bootstrap_status,
        lease_owner=row.lease_owner, lease_expiry=row.lease_expiry,
    )


def read_cutover_assets(db: "Session") -> Dict[str, CutoverAssetView]:
    """per-asset row → {asset: CutoverAssetView}. 부재 asset은 omit(fabricate 금지). read-only."""
    out: Dict[str, CutoverAssetView] = {}
    for row in db.query(AtomicCutoverAsset).all():
        out[row.asset] = CutoverAssetView(
            asset=row.asset, publish_state=row.publish_state,
            ready_revision_vector=row.ready_revision_vector, membership_version=row.membership_version,
        )
    return out


# ────────────────────────────── derivation (pure, fail-closed) ──────────────────────────────


def _ready_vector_valid(text: Optional[str]) -> bool:
    """ready_revision_vector 구조 검증 (codex blocker 2 — C6-1 CHECK는 non-empty만 보장).

    유효 = JSON object{str:str} + keys ⊆ FX_MEMBERSHIP_SOURCES + 각 value가 revision_key로 parse.
    malformed/non-object/non-str/미파싱/빈-dict → False (→ ATOMIC_READY 차단, fail-closed).
    """
    if not text:
        return False
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict) or not obj:
        return False
    for k, v in obj.items():
        if not isinstance(k, str) or k not in FX_MEMBERSHIP_SOURCES:
            return False
        if not isinstance(v, str):
            return False
        try:
            parse_revision_key(v)
        except ValueError:
            return False
    return True


def derive_cutover_state(
    writer_enforced_action: str,
    control: Optional[CutoverControlView],
    assets: Mapping[str, CutoverAssetView],
    expected_membership_version: int,
) -> CutoverState:
    """writer enforced_action × cutover rows → **GLOBAL** A5 CutoverState (pure, fail-closed).

    **이 함수가 유일한 publish-gate authority**(C6-runtime snapshot → publisher_gate_disposition).

    writer axis first(A1 fail-closed): legacy→LEGACY_READY / halt→HALT_BLOCKED.
    atomic: control 부재·status invalid → HALT_BLOCKED / status≠completed → ATOMIC_BLOCKED(mid-cutover,
    verified는 crash-resume marker라 여기 — §15-4) / status==completed면 session_id 존재 + 3 asset 전부
    ready + **membership_version==expected** + vector 구조유효여야 ATOMIC_READY(아니면 HALT_BLOCKED fail-closed).
    **membership match 필수**(§15-4 migration_complete + §18 membership-mismatch drop) — completed 이후
    FX_MEMBERSHIP_VERSION bump 시 stale readiness로 gate가 fail-open되지 않게. expected_membership_version은
    호출자(C6-runtime)가 FX_MEMBERSHIP_VERSION으로 주입.

    ⚠️ **writer LEGACY ⇒ LEGACY_READY 불변**(cutover rows 무시) — pre-cutover passthrough. §3가 atomic→legacy
    금지하므로 정상엔 legacy+completed-rows 공존 불가. C6 rollback 절차는 asset publish_state→blocked CAS-reset
    필수(later writer→ATOMIC flip이 stale ready를 상속 못 하게).
    """
    if writer_enforced_action == WriterMode.LEGACY:
        return CutoverState.LEGACY_READY
    if writer_enforced_action == WriterMode.HALT:
        return CutoverState.HALT_BLOCKED
    if writer_enforced_action != WriterMode.ATOMIC:
        return CutoverState.HALT_BLOCKED  # 미지 writer 값 → fail-closed

    # writer == ATOMIC
    if control is None or control.status not in _VALID_STATUSES:
        return CutoverState.HALT_BLOCKED  # atomic인데 cutover control 부재/corrupt → fail-closed
    if control.status != "completed":
        return CutoverState.ATOMIC_BLOCKED  # idle/running/verified/failed = 아직 publish 권한 없음
    # status == completed → session 존재 + 3 asset 전부 ready + membership match + vector 유효 (아니면 fail-closed)
    if control.session_id is None:
        return CutoverState.HALT_BLOCKED  # completed인데 session 부재 = torn/corrupt
    if set(assets.keys()) != set(FX_CUTOVER_ASSETS):
        return CutoverState.HALT_BLOCKED
    for a in assets.values():
        if a.publish_state != "ready":
            return CutoverState.HALT_BLOCKED  # any-blocked-wins
        if a.membership_version != expected_membership_version:
            return CutoverState.HALT_BLOCKED  # stale membership → fail-closed (§18 drop)
        if not _ready_vector_valid(a.ready_revision_vector):
            return CutoverState.HALT_BLOCKED  # corrupt readiness
    return CutoverState.ATOMIC_READY


def control_plane_readiness_ok(
    control: Optional[CutoverControlView],
    assets: Mapping[str, CutoverAssetView],
    expected_membership_version: int,
) -> bool:
    """migration_complete(asset)의 **control-plane half**만 (§15-4): control verified/completed +
    3 asset 전부 ready + membership match + vector 유효. Redis-v2/revision-verification half은 C6-8.

    ⚠️ **publish-gate 아님 — gating엔 derive_cutover_state를 쓸 것.** 이 함수는 §15-5 finalize의
    **precondition**(verified|completed면 finalize CAS가 발화 가능?)을 답한다. **verified를 허용**(derive는
    verified→ATOMIC_BLOCKED로 publish 차단)하므로, 이걸 publish gate로 오용하면 verified-but-not-completed
    row에서 fail-open. 두 함수는 같은 row에 의도적으로 다른 답을 낸다(precondition vs gate verdict).

    expected_membership_version은 주입(live fx import 회피)."""
    if control is None or control.status not in {"verified", "completed"}:
        return False
    if set(assets.keys()) != set(FX_CUTOVER_ASSETS):
        return False
    for a in assets.values():
        if a.publish_state != "ready":
            return False
        if a.membership_version != expected_membership_version:
            return False
        if not _ready_vector_valid(a.ready_revision_vector):
            return False
    return True


def validate_global_transition(from_state: CutoverState, to_state: CutoverState) -> bool:
    """A5 is_allowed_transition thin wrapper — **global CutoverState edge** 검증용(C6-8 orchestration).

    C6-2 CAS bricks는 low-level mutation(status/asset/session)이라 대부분 global state 불변 →
    is_allowed_transition으로 검증하지 않음(self-transition 거부됨). global edge가 바뀌는 지점만 C6-8이 이걸로.
    """
    return is_allowed_transition(from_state, to_state)


# ────────────────────────────── CAS bricks (fenced conditional-update, caller-commits) ──────────────────────────────


class CasResult(enum.Enum):
    """CAS brick 결과. **caller-commits라 APPLIED=staged**(tx 미commit). 호출자가 commit/rollback.

    ⚠️ DB 예외는 **swallow하지 않고 전파** — caller-commits 모델에선 tx 소유자(C6-8)가 catch+rollback해야
    하므로(brick이 ERROR로 삼키면 poisoned session을 호출자가 모름). brick은 정상 흐름의 CAS verdict만 반환.
    """
    APPLIED = "applied"                          # rowcount==1 (UPDATE staged)
    CAS_LOST = "cas_lost"                         # generation 전진(concurrent) — 재시도 대상
    PRECONDITION_FAILED = "precondition_failed"   # status/session/state fence 불일치


# 허용 status 진행 (low-level whitelist — codex blocker 1: is_allowed_transition 대신 명시 progression).
# ('idle','failed') 제외(dead — idle row는 session NULL이라 session-fenced advance 불가; failure는 session
# 보유 후 running/verified에서만). failed 복구는 cas_reset_to_idle(advance 아닌 별 brick).
_STATUS_PROGRESSION = frozenset({
    ("idle", "running"), ("running", "verified"), ("verified", "completed"),
    ("running", "failed"), ("verified", "failed"),
})


def _disambiguate_control(db: "Session", expected_generation: int) -> CasResult:
    """rowcount==0 시 CAS_LOST(gen 전진) vs PRECONDITION_FAILED(status/session 불일치) 구분 — re-SELECT.

    **populate_existing()** = identity-map의 stale 객체 대신 DB fresh state(다른 세션이 commit한
    bootstrap_generation 전진을 정확히 관측, READ COMMITTED 전제 — CAS 브릭의 bulk
    .update(synchronize_session=False)는 identity-map 미동기라 일반 re-read는 stale 반환). C6-8a
    _disambiguate(atomic_write_durable.py:60) 대칭. 공유 read_cutover_control(:57)은 LIVE path(runtime
    refresh_from_db / status endpoint / publisher gate 소비)라 pure-read 유지 — 여기서만 전용 fresh 쿼리.
    ⚠️ C6-8a와 달리 format-precedence는 의도적 미포함(cutover CAS는 format을 fence하지 않아 format-mismatch
    CAS_LOST 오분류 hazard가 없음) — '미러 완성' 목적으로 format 분기를 추가하지 말 것.
    """
    cur = (
        db.query(AtomicCutoverControl)
        .populate_existing()
        .filter(AtomicCutoverControl.id == 1)
        .first()
    )
    if cur is None:
        return CasResult.PRECONDITION_FAILED
    if cur.bootstrap_generation > expected_generation:
        return CasResult.CAS_LOST
    return CasResult.PRECONDITION_FAILED


def cas_begin_cutover(db: "Session", *, new_session: str, expected_generation: int) -> CasResult:
    """cutover 세션 시작: status idle→running + session=new + generation++ (§18 begin).

    fence: id=1 AND generation=expected AND status='idle' AND session IS NULL. caller-commits.
    DB 예외 전파(caller가 rollback).
    """
    result = (
        db.query(AtomicCutoverControl)
        .filter(
            AtomicCutoverControl.id == 1,
            AtomicCutoverControl.bootstrap_generation == expected_generation,
            AtomicCutoverControl.bootstrap_status == "idle",
            AtomicCutoverControl.bootstrap_session_id.is_(None),
        )
        .update(
            {
                "bootstrap_status": "running",
                "bootstrap_session_id": new_session,
                "bootstrap_generation": expected_generation + 1,
            },
            synchronize_session=False,
        )
    )
    if result == 1:
        return CasResult.APPLIED
    return _disambiguate_control(db, expected_generation)


def cas_advance_status(
    db: "Session", *, expected_session: str, expected_status: str, new_status: str,
    expected_generation: int,
) -> CasResult:
    """status 진행(running→verified→completed / running·verified→failed) + generation++ (§15-4/5/abort).

    low-level whitelist(_STATUS_PROGRESSION) 검증 — global CutoverState 무변 transition 포함이라
    is_allowed_transition 안 씀(codex blocker 1). fence: id=1 AND generation=expected AND
    status=expected AND session=expected. caller-commits. DB 예외 전파.
    """
    if (expected_status, new_status) not in _STATUS_PROGRESSION:
        return CasResult.PRECONDITION_FAILED
    result = (
        db.query(AtomicCutoverControl)
        .filter(
            AtomicCutoverControl.id == 1,
            AtomicCutoverControl.bootstrap_generation == expected_generation,
            AtomicCutoverControl.bootstrap_status == expected_status,
            AtomicCutoverControl.bootstrap_session_id == expected_session,
        )
        .update(
            {"bootstrap_status": new_status, "bootstrap_generation": expected_generation + 1},
            synchronize_session=False,
        )
    )
    if result == 1:
        return CasResult.APPLIED
    return _disambiguate_control(db, expected_generation)


def cas_reset_to_idle(db: "Session", *, expected_session: str, expected_generation: int) -> CasResult:
    """failed→idle 복구: status='failed'→'idle' + session→NULL + generation++ (§18 operator retry).

    failed cutover를 재시작 가능 상태(idle/session NULL)로 되돌려 cas_begin_cutover가 다시 발화하게 한다.
    fence: id=1 AND generation=expected AND status='failed' AND session=expected. caller-commits. DB 예외 전파.
    ⚠️ crash-takeover(running→reset, lease expiry 기반)는 lease orchestration이라 C6-8 범위(여긴 명시 failed만).
    """
    result = (
        db.query(AtomicCutoverControl)
        .filter(
            AtomicCutoverControl.id == 1,
            AtomicCutoverControl.bootstrap_generation == expected_generation,
            AtomicCutoverControl.bootstrap_status == "failed",
            AtomicCutoverControl.bootstrap_session_id == expected_session,
        )
        .update(
            {
                "bootstrap_status": "idle",
                "bootstrap_session_id": None,
                "bootstrap_generation": expected_generation + 1,
            },
            synchronize_session=False,
        )
    )
    if result == 1:
        return CasResult.APPLIED
    return _disambiguate_control(db, expected_generation)


def cas_flip_asset_ready(
    db: "Session", asset: str, *, ready_revision_vector: str, membership_version: int,
) -> CasResult:
    """per-asset publish_state blocked→ready + vector + membership (§15-5 asset flip, caller-commits).

    fence: asset=:asset AND publish_state='blocked'. vector 구조유효 방어(invalid → PRECONDITION_FAILED).
    DB 예외 전파.

    ⚠️ **asset row엔 generation/session fence 없음** — 안전은 C6-8이 보장: 이 brick은 §15-5 finalize
    control-CAS tx **안에서만** 호출돼야(control을 (session,generation,status=verified)로 fence한 같은 tx에서
    3 asset flip + status=completed를 합성). standalone 호출은 stale session에서 임의 flip 가능 → C6-8이
    control-CAS와 묶어 fencing 컨텍스트를 제공.
    """
    if asset not in FX_CUTOVER_ASSETS:
        return CasResult.PRECONDITION_FAILED
    if not _ready_vector_valid(ready_revision_vector) or not isinstance(membership_version, int) \
            or isinstance(membership_version, bool) or membership_version <= 0:
        return CasResult.PRECONDITION_FAILED
    result = (
        db.query(AtomicCutoverAsset)
        .filter(
            AtomicCutoverAsset.asset == asset,
            AtomicCutoverAsset.publish_state == "blocked",
        )
        .update(
            {
                "publish_state": "ready",
                "ready_revision_vector": ready_revision_vector,
                "membership_version": membership_version,
            },
            synchronize_session=False,
        )
    )
    if result == 1:
        return CasResult.APPLIED
    return CasResult.PRECONDITION_FAILED  # 이미 ready거나 row 부재 (asset엔 generation fence 없음)


# ────────────────────────────── migration lease CAS (C6-PRE-build-a, §18) ──────────────────────────────
# concurrent migration runner 방지 + crash-takeover. **app-clock(Option A, codex thread 019ee30e)**: lease_expiry
# 비교/설정 모두 caller-passed `now`(app-clock, cas_activate_atomic now= idiom) — cross-dialect testable
# (sqlite/PostgreSQL, func.now()+interval 회피) + Dockerfile --workers 1 단일 EC2 host라 migration runner가
# 같은 wall clock 공유 → skew 무시 가능. ⚠️ **단일 clock source 가정**(multi-host 확장 시 DB-clock 재검토).
# crash-takeover = `lease_expiry < now`(만료 strict 후에만 인수; ==now은 still-held, renew `>= now`과 boundary 일관).
# format fence(codex endorse, 방어적 — corrupt/future format row 위에서 migration lease 잡지 않음; sibling cutover
# CAS는 format 미fence[B4 command-level]이나 lease는 migration 권위라 직접 fence).
_CUTOVER_ROW_FORMAT_VERSION = 1


def _naive_utc(dt: datetime) -> datetime:
    """tz-aware → UTC naive 정규화 (lease_expiry 컬럼 naive 정합; get_utc_now()는 naive라 통상 no-op)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _valid_owner(v: object) -> bool:
    return isinstance(v, str) and bool(v)


def _valid_ttl(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def cas_acquire_migration_lease(
    db: "Session", *, owner: str, now: datetime, ttl_seconds: int
) -> CasResult:
    """migration lease 획득: unleased(owner NULL) / expired(crash-takeover, lease_expiry < now) /
    same-owner re-acquire(idempotent — partial retry 안전). caller-commits.

    fence: id=1 AND format ok AND (lease_owner IS NULL OR lease_expiry < :now OR lease_owner=:owner)
    → SET lease_owner=:owner, lease_expiry=:now+ttl. affected==1 → APPLIED / 0 → PRECONDITION_FAILED
    (타 owner non-expired lease 보유 / format mismatch / row 부재). DB 예외 전파.
    """
    if not _valid_owner(owner) or not _valid_ttl(ttl_seconds) or not isinstance(now, datetime):
        return CasResult.PRECONDITION_FAILED
    now = _naive_utc(now)
    expiry = now + timedelta(seconds=ttl_seconds)
    result = (
        db.query(AtomicCutoverControl)
        .filter(
            AtomicCutoverControl.id == 1,
            AtomicCutoverControl.cutover_row_format_version == _CUTOVER_ROW_FORMAT_VERSION,
            or_(
                AtomicCutoverControl.lease_owner.is_(None),
                AtomicCutoverControl.lease_expiry < now,
                AtomicCutoverControl.lease_owner == owner,
            ),
        )
        .update({"lease_owner": owner, "lease_expiry": expiry}, synchronize_session=False)
    )
    return CasResult.APPLIED if result == 1 else CasResult.PRECONDITION_FAILED


def cas_renew_migration_lease(
    db: "Session", *, owner: str, now: datetime, ttl_seconds: int
) -> CasResult:
    """own **valid** lease 갱신(strict — 만료된 own lease는 renew 불가, acquire로 재획득). caller-commits.

    fence: id=1 AND format ok AND lease_owner=:owner AND lease_expiry >= :now → SET lease_expiry=:now+ttl.
    affected==1 → APPLIED / 0 → PRECONDITION_FAILED (lease 상실/만료/타 owner/row 부재). DB 예외 전파.
    """
    if not _valid_owner(owner) or not _valid_ttl(ttl_seconds) or not isinstance(now, datetime):
        return CasResult.PRECONDITION_FAILED
    now = _naive_utc(now)
    expiry = now + timedelta(seconds=ttl_seconds)
    result = (
        db.query(AtomicCutoverControl)
        .filter(
            AtomicCutoverControl.id == 1,
            AtomicCutoverControl.cutover_row_format_version == _CUTOVER_ROW_FORMAT_VERSION,
            AtomicCutoverControl.lease_owner == owner,
            AtomicCutoverControl.lease_expiry >= now,
        )
        .update({"lease_expiry": expiry}, synchronize_session=False)
    )
    return CasResult.APPLIED if result == 1 else CasResult.PRECONDITION_FAILED


def cas_release_migration_lease(db: "Session", *, owner: str) -> CasResult:
    """own lease 해제 → lease_owner=NULL, lease_expiry=NULL(paired-null CHECK 정합). caller-commits.

    fence: id=1 AND format ok AND lease_owner=:owner. affected==1 → APPLIED / 0 → PRECONDITION_FAILED
    (미보유/타 owner/row 부재). DB 예외 전파. (만료 여부 무관 — 자기 owner면 해제 가능.)
    """
    if not _valid_owner(owner):
        return CasResult.PRECONDITION_FAILED
    result = (
        db.query(AtomicCutoverControl)
        .filter(
            AtomicCutoverControl.id == 1,
            AtomicCutoverControl.cutover_row_format_version == _CUTOVER_ROW_FORMAT_VERSION,
            AtomicCutoverControl.lease_owner == owner,
        )
        .update({"lease_owner": None, "lease_expiry": None}, synchronize_session=False)
    )
    return CasResult.APPLIED if result == 1 else CasResult.PRECONDITION_FAILED
