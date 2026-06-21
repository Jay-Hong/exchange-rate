"""P1 atomic-write runtime cache — writer가 읽는 effective-mode snapshot layer (A2-1).

P1_COMMON_BASE_DESIGN.md §3/§7 + §19 "A2 상세 설계". A1(`app/atomic_write_control.py`)이
순수 계산/read helper를 제공하고, 본 모듈은 그 위에 **process-memory cache + writer용
immutable snapshot**을 얹는다.

A2-1 범위 (완전 dormant — A1과 동일하게 "정의 + 테스트, live 효과 0"):
    - snapshot/refresh/derive 정의만. **어떤 writer/broadcast도 호출하지 않음.**
    - **live poll scheduling 없음** — import/lifespan에서 자동 refresh/thread/job 시작 안 함.
      (writer 미연결이어도 live DB-polling을 등록하면 새 side effect = behavior-change-0 위반.)
      refresh_from_db 호출자 연결(startup refresh / managed poll)은 A2-2.
    - 단일 프로세스 전제(Dockerfile --workers 1).

설계 invariant (codex 4 review point):
    1. **import-time side effect 0**: 모듈 로드 시 thread/job/DB read 없음 (객체 정의만).
    2. **snapshot() no-throw**: 어떤 상황에서도 예외 안 냄 (last-good 또는 안전 default 반환).
    3. **activation_latched 단조(monotonic)**: 한 번 True면 프로세스 생애 동안 False 금지.
    4. **refresh lock 분리**: DB read/compute는 lock 밖, lock 안에서는 snapshot swap만.

phase-gate (§19 A2 crux b):
    - `enforced_action`(writer가 실제 분기하는 단일 값)을 `diagnostic_effective_mode`
      (compute_effective_mode 결과 — None→HALT 진단 라벨 불변)와 분리.
    - pre-activation(activation_latched=False): 부재/corruption/legacy 전부 **legacy passthrough**
      (A2 배포≠halt).
    - post-activation(activation_latched=True): atomic이면 atomic, 그 외는 **fail-closed halt**.
    - durable activation marker = DB control row `activation_epoch>0` (재시작 생존, 읽히는 한).
      post-activation 재시작+control 못읽음 ambiguity는 C6 precondition (본 모듈 범위 밖).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.atomic_write_control import (
    CONTROL_ROW_FORMAT_VERSION,
    WriterMode,
    compute_effective_mode,
    read_control_row,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import AtomicWriteControl


@dataclass(frozen=True)
class WriteModeSnapshot:
    """writer hot path가 1-read하는 immutable snapshot (torn read 방지)."""

    diagnostic_effective_mode: str  # compute_effective_mode 결과 (None→HALT 진단 라벨)
    activation_latched: bool        # activation_epoch>0를 한 번이라도 본 적 있나 (in-process monotonic)
    enforced_action: str            # writer가 실제 분기하는 단일 값 (legacy/atomic/halt)
    mode_generation: int            # §7 fencing token (in-process 관측 — A2-1엔 분기 미사용)


def _derive_enforced_action(diagnostic: str, activation_latched: bool, explicit_halt: bool) -> str:
    """diagnostic + latch + explicit_halt → enforced_action (§19 A2 crux b phase-gate).

    우선순위:
    - diagnostic==ATOMIC → atomic (compute_effective_mode가 activation+schema 충족 시만 반환).
    - **explicit_halt**(valid row + format 일치 + requested_mode==halt) → halt — pre-activation에도
      honor (§3/§9 legacy→halt pre-quiesce durable step; crash/restart 후에도 quiesce 유지).
    - activation_latched(post-activation 비-atomic·비-explicit) → **fail-closed halt**.
    - 그 외 (pre-activation 부재/corruption/atomic-미활성, involuntary) → **legacy passthrough** (A2 배포 안전).

    핵심: diagnostic==HALT 하나로는 "명시 halt"와 "부재/corruption"을 구분 못 함 → explicit_halt로 분리.
    """
    if diagnostic == WriterMode.ATOMIC:
        return WriterMode.ATOMIC
    if explicit_halt:
        return WriterMode.HALT
    if activation_latched:
        return WriterMode.HALT
    return WriterMode.LEGACY


# 초기 안전 default — refresh 전(또는 read 실패 시 fallback). pre-activation legacy passthrough.
# diagnostic=HALT는 "아직 control 미확인" 진단 라벨; enforced=LEGACY는 pre-activation 안전 동작.
_INITIAL = WriteModeSnapshot(
    diagnostic_effective_mode=WriterMode.HALT,
    activation_latched=False,
    enforced_action=WriterMode.LEGACY,
    mode_generation=0,
)

_LOCK = threading.RLock()  # 객체 생성만 (thread 시작 아님 — import side effect 0)
_current: WriteModeSnapshot = _INITIAL


def snapshot() -> WriteModeSnapshot:
    """현재 write-mode snapshot (no-throw, last-good). writer hot path 단일 accessor.

    invariant 2: 어떤 상황에서도 예외 안 냄. lock read 실패(거의 불가)도 _INITIAL로 흡수.
    """
    try:
        with _LOCK:
            return _current
    except Exception:
        return _INITIAL


def is_initialized() -> bool:
    """startup refresh가 _INITIAL을 실제 snapshot으로 교체했는지 (write-mode 확정 여부).

    False = 아직 write-mode 미확정(_current is _INITIAL) → mirror-WRITE 금지(post-flip v1 downgrade
    방지, incident 2026-06-21). snapshot() 기준이라 test가 snapshot()을 patch하면 일관. no-throw.
    """
    return snapshot() is not _INITIAL


def _control_table_present(db: "Session") -> bool:
    """atomic_write_control 테이블 존재 여부 (best-effort, no-throw).

    refresh read 실패 시 'table-absent(pre-G2a legacy world)' vs 'transient(mode 미상)'을 구분하려
    호출. 불확실(probe 자체 실패) 시 True 반환 = transient로 간주 → 미초기화 유지(skip, downgrade 방지).
    """
    from sqlalchemy import inspect as _sa_inspect  # 함수 내부 import — 모듈 import 경량 유지
    try:
        return _sa_inspect(db.get_bind()).has_table("atomic_write_control")
    except Exception:
        return True


def halt_enforced(snap: WriteModeSnapshot) -> bool:
    """writer가 halt로 차단해야 하는지 (enforced_action 단일 판정)."""
    return snap.enforced_action == WriterMode.HALT


def _row_activated(row: "AtomicWriteControl | None") -> bool:
    """row.activation_epoch>0 (int·non-bool·양수 방어)."""
    if row is None:
        return False
    epoch = row.activation_epoch
    return isinstance(epoch, int) and not isinstance(epoch, bool) and epoch > 0


def _row_generation(row: "AtomicWriteControl | None", current: int) -> int:
    """관측 mode_generation — fencing token이라 **단조**(현재값 미만으로 회귀 금지)."""
    if row is None:
        return current
    gen = row.mode_generation
    if isinstance(gen, int) and not isinstance(gen, bool):
        return gen if gen > current else current
    return current


def _row_explicit_halt(row: "AtomicWriteControl | None") -> bool:
    """운영자 명시 halt = **구조적으로 valid한 row** + requested_mode==halt (corruption과 구분).

    format/numeric(epoch/schema) corruption이면 requested_mode를 신뢰하지 않음 → explicit halt
    아님 → phase-gate(pre-activation legacy / post-activation halt). compute_effective_mode가
    HALT를 내는 사유(format·numeric·enum corruption) 중 corruption을 explicit halt에서 배제 —
    corrupt row + requested=halt가 legacy passthrough를 우회해 halt로 실효되는 것 차단.
    """
    if row is None:
        return False
    if row.requested_mode != WriterMode.HALT:
        return False
    fmt = row.control_row_format_version
    if not (isinstance(fmt, int) and not isinstance(fmt, bool) and fmt == CONTROL_ROW_FORMAT_VERSION):
        return False
    epoch = row.activation_epoch
    if not (isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0):
        return False
    schema = row.target_write_schema_version
    if not (isinstance(schema, int) and not isinstance(schema, bool) and schema >= 1):
        return False
    return True


def refresh_from_db(db: "Session") -> WriteModeSnapshot:
    """control row를 읽어 snapshot 갱신 후 반환 (no-throw).

    invariant 4: DB read + compute는 **lock 밖**, lock 안에서는 **swap만**.
    invariant 3: activation_latched는 `이전값 OR 이번읽음`으로 **단조 증가** (True면 영구).
    read/compute 실패: diagnostic=HALT 새 snapshot으로 swap + enforced는 prev 기반 fail-close
    (atomic→halt[§7 못읽음→halt], legacy/halt 보존) + latch/gen 보존 (pre-activation legacy
    안전 + post-activation·explicit halt 유지).

    **A2-1엔 호출자 없음** (live poll/writer 미연결). A2-2가 startup/poll에서 호출.
    """
    global _current
    # ── DB read + compute (lock 밖) ──
    table_present = True   # default: 불확실 시 transient로 간주(미초기화 유지 → skip, downgrade 방지)
    row_is_none = False
    try:
        row = read_control_row(db)
        diagnostic = compute_effective_mode(row)
        row_activated = _row_activated(row)
        explicit_halt = _row_explicit_halt(row)
        read_ok = True
        row_is_none = row is None   # table 존재 + row 부재 (corrupt/partial G2a) — read_control_row은 부재 시 None
    except Exception:
        row = None
        diagnostic = WriterMode.HALT  # 못 읽음 = HALT 진단 라벨
        row_activated = False
        explicit_halt = False
        read_ok = False
        table_present = _control_table_present(db)  # table-absent(pre-G2a) vs transient 구분용

    # ── swap (lock 안 — monotonic latch/gen, read-fail fail-closed) ──
    try:
        with _LOCK:
            prev = _current
            # ── 첫 refresh(prev is _INITIAL) + write-mode 미확정 → _INITIAL 유지(swap 안 함) ──
            # mirror가 미확정 상태에서 LEGACY write로 v2를 v1 덮는 사고 방지(incident 2026-06-21).
            #   (1) read 실패 + table 존재 = transient(mode 미상)
            #   (2) read 성공이나 row 부재 + table 존재 = corrupt/partial G2a (codex 보강)
            # table-absent(pre-G2a legacy world)는 제외 — 아래 fail-close swap으로 legacy 진행(dormant 정상).
            if prev is _INITIAL and (
                (not read_ok and table_present) or (read_ok and row_is_none)
            ):
                return prev  # _INITIAL 유지 → is_initialized()=False → mirror skip
            latched = prev.activation_latched or row_activated  # 단조: True면 영구
            if read_ok:
                gen = _row_generation(row, prev.mode_generation)
                enforced = _derive_enforced_action(diagnostic, latched, explicit_halt)
            else:
                # read 실패: enforced는 last-good fail-close — atomic이면 halt(§7 못읽음→halt),
                # legacy/halt는 보존(pre-activation legacy 안전 + explicit halt 유지). gen 보존.
                gen = prev.mode_generation
                enforced = (
                    WriterMode.HALT if prev.enforced_action == WriterMode.ATOMIC
                    else prev.enforced_action
                )
            new = WriteModeSnapshot(
                diagnostic_effective_mode=diagnostic,
                activation_latched=latched,
                enforced_action=enforced,
                mode_generation=gen,
            )
            _current = new
            return new
    except Exception:
        return snapshot()  # swap 실패(거의 불가)도 last-good


def _reset_for_test() -> None:
    """테스트 전용 — 모듈 cache를 _INITIAL로 리셋 (monotonic latch 때문에 필요).

    운영 코드에서 호출 금지.
    """
    global _current
    with _LOCK:
        _current = _INITIAL
