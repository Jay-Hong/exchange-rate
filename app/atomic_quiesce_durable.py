"""P1b C6-quiesce Q2b — quiesce-evidence durable CAS bricks (atomic_quiesce_durable.py, dormant island).

§9 step 6(개정) live-app quiesce handshake의 evidence 축(AtomicQuiesceSession / AtomicQuiesceAppAck) CAS bricks.
drain = recreate + fresh-process halt ACK (in-process counter 폐기, §19 C6-quiesce). C6-8a writer-axis
(atomic_write_durable) / C6-2 cutover-axis(atomic_cutover_durable)에 이은 **세 번째 axis(quiesce evidence)** —
own island. caller-commits(APPLIED=staged, tx 미commit, DB 예외 전파 — tx 소유자가 commit/rollback).
C6-2 `CasResult` 재사용. atomic_write_control에서 **상수만** import(WriterMode/CONTROL_ROW_FORMAT_VERSION) —
A1 SYMBOLS(read_control_row/compute_effective_mode) 미호출(test_atomic_write_control call-form scan 무관),
control fence는 자체 `db.query(AtomicWriteControl).populate_existing()`로.

**dormant**: app/ live 모듈이 본 모듈 import 0(no-importer AST trip-wire) — 유일 caller는 Q4(activation
script `_do_halt`가 cas_open / fresh app startup ACK-writer가 cas_record / begin-atomic이 cas_consume),
실 발화는 C6-FLIP(must-confirm). atomic_cutover_durable import(CasResult) 때문에 본 모듈은
test_atomic_cutover_durable의 _ISLAND에 등재된다.

§9 quiesce 시퀀스에서의 위치 (activate_atomic_fx 기준):
- `cas_open_quiesce_session`: halt 명령(_do_halt)이 **cas_request_halt와 같은 tx**에서 호출 — 어떤 halt를
  drain 중인지 durable 기록(state='open'). ⚠️ **composition invariant**: cas_open은 같은 tx에서
  `cas_request_halt`(legacy→halt) APPLIED 직후에만 호출해야 한다. brick 단독 fence(requested_mode==HALT &&
  mode_generation==supplied)는 incident-halt(atomic→halt, cas_request_incident_halt)도 통과시킬 수 있어
  caller-composition이 1차 보증이고, brick은 추가로 **`activation_epoch==0`**(activation quiesce는 first-flip
  전이라 epoch 0 / incident-halt는 이미 atomic이라 epoch>=1)으로 incident-halt를 brick-level에서도 거부한다.
- `cas_record_quiesce_app_ack`: recreate된 fresh app startup ACK-writer(조건부)가 호출. fail-closed write —
  §9 step6 ACK 4조건(requested_mode==halt / observed_enforced_action==halt / observed_gen>=halt_gen /
  process_started_at>halt_committed_at)을 write-precondition으로 강제(stale/legacy process가 false ACK를
  durable write 못 하게). Q4 RealQuiesceBoundary가 read 시 재검증(defense-in-depth).
- `cas_consume_quiesce_session`: begin-atomic이 boundary verdict 후 open→consumed(hygiene close).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from app.atomic_cutover_durable import CasResult
from app.atomic_write_control import CONTROL_ROW_FORMAT_VERSION, WriterMode
from app.models import AtomicQuiesceAppAck, AtomicQuiesceSession, AtomicWriteControl

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_OPEN = "open"
_CONSUMED = "consumed"
# AtomicQuiesceSession.quiesce_row_format_version 기대값 (format-fence — corrupt/future quiesce evidence fail-closed)
_QUIESCE_ROW_FORMAT_VERSION = 1


def _valid_fence_int(v: object) -> bool:
    """fence 값이 non-bool int >= 0 (bool는 int 서브클래스라 명시 배제, None/str 차단). C6-8a 패턴."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _nonempty_str(v: object) -> bool:
    return isinstance(v, str) and bool(v)


def _naive_utc(dt: datetime) -> datetime:
    """tz-aware → UTC naive 정규화 (컬럼이 tz-naive DateTime — cas_activate_atomic:195-201 정합)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _read_control(db: "Session") -> Optional["AtomicWriteControl"]:
    """A1 control row fresh read (populate_existing — identity-map stale 회피, READ COMMITTED)."""
    return (
        db.query(AtomicWriteControl)
        .populate_existing()
        .filter(AtomicWriteControl.id == 1)
        .first()
    )


def cas_open_quiesce_session(
    db: "Session", *, session_id: str, halt_mode_generation: int, halt_committed_at: datetime
) -> CasResult:
    """activation halt 시점에 quiesce session INSERT (state='open'). caller-commits.

    ⚠️ **caller composition (1차 보증)**: 같은 tx에서 `cas_request_halt`(legacy→halt) APPLIED 직후에만 호출.
    fence (brick-level, fail-closed):
      - param: session_id non-empty str / halt_mode_generation non-bool int>=0 / halt_committed_at datetime.
      - control(populate_existing): format==CONTROL_ROW_FORMAT_VERSION / requested_mode==HALT /
        mode_generation==halt_mode_generation(THAT halt를 pin) / **activation_epoch==0**(activation quiesce
        한정 — incident-halt[atomic→halt]는 epoch>=1이라 거부).
      - single-open pre-check: 이미 state='open' session 있으면 PRECONDITION_FAILED (partial-unique index가
        구조 backstop — 단일 프로세스 sequential activation이라 TOCTOU 비현실적, race 시 commit에서 IntegrityError 전파).
    Returns: APPLIED(staged) / PRECONDITION_FAILED. (CAS_LOST는 open INSERT에 N/A — 선행 generation 없음.)
    """
    if not _nonempty_str(session_id) or not _valid_fence_int(halt_mode_generation):
        return CasResult.PRECONDITION_FAILED
    if not isinstance(halt_committed_at, datetime):
        return CasResult.PRECONDITION_FAILED
    ctrl = _read_control(db)
    if ctrl is None or ctrl.control_row_format_version != CONTROL_ROW_FORMAT_VERSION:
        return CasResult.PRECONDITION_FAILED
    if ctrl.requested_mode != WriterMode.HALT:
        return CasResult.PRECONDITION_FAILED
    if ctrl.mode_generation != halt_mode_generation:
        return CasResult.PRECONDITION_FAILED
    if ctrl.activation_epoch != 0:  # Q9: activation quiesce only — incident-halt(atomic→halt)는 epoch>=1
        return CasResult.PRECONDITION_FAILED
    existing_open = (
        db.query(AtomicQuiesceSession)
        .filter(AtomicQuiesceSession.state == _OPEN)
        .first()
    )
    if existing_open is not None:
        return CasResult.PRECONDITION_FAILED
    db.add(AtomicQuiesceSession(
        session_id=session_id,
        halt_mode_generation=halt_mode_generation,
        halt_committed_at=_naive_utc(halt_committed_at),
        state=_OPEN,
    ))
    return CasResult.APPLIED


def cas_record_quiesce_app_ack(
    db: "Session",
    *,
    session_id: str,
    boot_id: str,
    process_started_at: datetime,
    observed_writer_generation: int,
    observed_enforced_action: str,
    queue_size: Optional[int] = None,
) -> CasResult:
    """recreate된 fresh app이 halt 관측을 ACK (per-boot INSERT). caller-commits. **fail-closed write**.

    §9 step6 ACK 4조건을 write-precondition으로 강제 — stale/legacy-cached process가 false ACK를 durable
    write 못 하게(_INITIAL.enforced_action=LEGACY라 recreate 성공·row 존재만으론 부족):
      (cond2) observed_enforced_action == 'halt' (param fail-closed).
      (cond1) live AtomicWriteControl.requested_mode == HALT.
      (cond3) observed_writer_generation >= session.halt_mode_generation.
      (cond4) process_started_at > session.halt_committed_at.
    + open session(session_id, state='open') 존재. + dup(session_id, boot_id) → CAS_LOST(멱등 re-ACK 무해).
    Returns: APPLIED(staged) / PRECONDITION_FAILED(조건 miss·open session 없음) / CAS_LOST(dup).
    """
    if not _nonempty_str(session_id) or not _nonempty_str(boot_id):
        return CasResult.PRECONDITION_FAILED
    if not isinstance(process_started_at, datetime) or not _valid_fence_int(observed_writer_generation):
        return CasResult.PRECONDITION_FAILED
    if observed_enforced_action != WriterMode.HALT:  # fail-closed: halt 관측만 ACK write
        return CasResult.PRECONDITION_FAILED
    if queue_size is not None and not _valid_fence_int(queue_size):
        return CasResult.PRECONDITION_FAILED
    ctrl = _read_control(db)
    if ctrl is None or ctrl.control_row_format_version != CONTROL_ROW_FORMAT_VERSION:
        return CasResult.PRECONDITION_FAILED
    if ctrl.requested_mode != WriterMode.HALT:  # cond1
        return CasResult.PRECONDITION_FAILED
    session = (
        db.query(AtomicQuiesceSession)
        .populate_existing()
        .filter(AtomicQuiesceSession.session_id == session_id, AtomicQuiesceSession.state == _OPEN)
        .first()
    )
    if session is None:
        return CasResult.PRECONDITION_FAILED
    if observed_writer_generation < session.halt_mode_generation:  # cond3
        return CasResult.PRECONDITION_FAILED
    if not (_naive_utc(process_started_at) > session.halt_committed_at):  # cond4
        return CasResult.PRECONDITION_FAILED
    dup = (
        db.query(AtomicQuiesceAppAck)
        .filter(AtomicQuiesceAppAck.session_id == session_id, AtomicQuiesceAppAck.boot_id == boot_id)
        .first()
    )
    if dup is not None:
        return CasResult.CAS_LOST
    db.add(AtomicQuiesceAppAck(
        session_id=session_id,
        boot_id=boot_id,
        process_started_at=_naive_utc(process_started_at),
        observed_writer_generation=observed_writer_generation,
        observed_enforced_action=observed_enforced_action,
        queue_size=queue_size,
    ))
    return CasResult.APPLIED


def cas_consume_quiesce_session(db: "Session", *, session_id: str) -> CasResult:
    """open→consumed (begin-atomic hygiene close). fence: session_id AND state='open'. caller-commits.

    Returns: APPLIED(전이됨) / PRECONDITION_FAILED(open session 없음 — already consumed/aborted/부재).
    (aborted 전이는 Q4 — 재-halt supersession 시점 결정.)
    """
    if not _nonempty_str(session_id):
        return CasResult.PRECONDITION_FAILED
    result = (
        db.query(AtomicQuiesceSession)
        .filter(AtomicQuiesceSession.session_id == session_id, AtomicQuiesceSession.state == _OPEN)
        .update({"state": _CONSUMED}, synchronize_session=False)
    )
    if result == 1:
        return CasResult.APPLIED
    return CasResult.PRECONDITION_FAILED


def find_open_quiesce_session(db: "Session") -> Optional[AtomicQuiesceSession]:
    """현재 open 상태 quiesce session 1개 read (없으면 None). partial-unique(state='open')라 최대 1개.

    Q4-A startup ACK-writer가 session_id 확보용으로 호출(cas_record_quiesce_app_ack는 (session_id, state='open')
    lookup이라 caller가 session_id를 먼저 알아야 함). pure read(populate_existing — identity-map stale 회피).
    table 부재(create_all 제외, pre-migrate) 시 query가 raise → caller(startup module)가 no-throw로 흡수.
    """
    return (
        db.query(AtomicQuiesceSession)
        .populate_existing()
        .filter(AtomicQuiesceSession.state == _OPEN)
        .first()
    )


def confirm_quiesce_drained(db: "Session", *, expected_generation: Optional[int] = None) -> bool:
    """§9 step6 quiesce drain proof 판정 (RealQuiesceBoundary[begin-atomic gate] + prod migration gate 공유).

    drain = recreate + fresh-process halt ACK. "control row가 HALT"만으론 부족 — halt 전에 떠 있던
    stale legacy-cached writer가 halt를 아직 관측 못 했으면 v1을 계속 쓸 수 있음. 그래서 **open quiesce
    session ∧ fresh control HALT(exact-pin/epoch0/format) ∧ qualifying ACK**(recreate된 fresh process가
    halt 관측을 durable 기록)을 전부 요구한다. `expected_generation` 주면(prod migration의
    `--expected-writer-generation`) session.halt_mode_generation과 교차검증(operator pin).

    조건 (전부 AND, 하나라도 실패/모호 → False):
      - open quiesce session 존재(partial-unique → 최대 1) ∧ quiesce_row_format_version == 기대값
        (corrupt/future quiesce evidence fail-closed).
      - fresh control(populate_existing): 부재 / format≠CONTROL_ROW_FORMAT_VERSION / requested_mode≠HALT(cond1) /
        mode_generation≠session.halt_mode_generation(cond3 exact-pin) / activation_epoch≠0(activation 한정) → False.
      - expected_generation is not None → non-bool int ∧ session.halt_mode_generation==expected_generation
        (아니면 False — 잘못된 pin/타입, fail-closed).
      - qualifying ACK(session_id 일치 / observed_enforced_action==HALT[cond2] /
        observed_writer_generation>=session.halt_mode_generation[cond3] /
        process_started_at>session.halt_committed_at[cond4]).

    **never-raise → False**: 어떤 read/parse/table-absent 실패도 False(fail-closed floor — RealQuiesceBoundary
    stub 계약 보존). read-only(자체 commit/write 없음). caller가 db lifecycle 소유.
    """
    try:
        session = find_open_quiesce_session(db)
        if session is None:
            return False
        if session.quiesce_row_format_version != _QUIESCE_ROW_FORMAT_VERSION:
            return False
        ctrl = _read_control(db)
        if ctrl is None or ctrl.control_row_format_version != CONTROL_ROW_FORMAT_VERSION:
            return False
        if ctrl.requested_mode != WriterMode.HALT:  # cond1 live
            return False
        if ctrl.mode_generation != session.halt_mode_generation:  # cond3 exact-pin
            return False
        if ctrl.activation_epoch != 0:  # activation quiesce 한정 (incident-halt epoch>=1)
            return False
        if expected_generation is not None:  # prod migration operator pin 교차검증
            if not _valid_fence_int(expected_generation):
                return False
            if session.halt_mode_generation != expected_generation:
                return False
        ack = (
            db.query(AtomicQuiesceAppAck)
            .filter(
                AtomicQuiesceAppAck.session_id == session.session_id,
                AtomicQuiesceAppAck.observed_enforced_action == WriterMode.HALT,  # cond2
                AtomicQuiesceAppAck.observed_writer_generation >= session.halt_mode_generation,  # cond3
                AtomicQuiesceAppAck.process_started_at > session.halt_committed_at,  # cond4
            )
            .order_by(AtomicQuiesceAppAck.observed_at.desc(), AtomicQuiesceAppAck.id.desc())
            .first()
        )
        return ack is not None
    except Exception:
        return False  # never-raise: fail-closed floor (table-absent/read/parse 실패도 False)
