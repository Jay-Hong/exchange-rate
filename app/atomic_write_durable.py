"""P1b C6-8a — writer-mode durable CAS bricks (atomic_write_durable.py, dormant island).

§8/§9 atomic activation의 writer-mode 축(AtomicWriteControl) 전이를 위한 conditional-update CAS bricks. C6-2
cutover bricks(atomic_cutover_durable.py)의 writer-axis 대응 — cutover는 publish_state/session/
bootstrap_generation, 여기는 requested_mode/activation_epoch/mode_generation. caller-commits(APPLIED=staged,
tx 미commit, DB 예외 전파 — tx 소유자 C6-8 command가 commit/rollback). C6-2 `CasResult` 재사용(command가
writer+cutover brick verdict를 한 타입으로 합성).

**위치 = 신규 island 모듈(atomic_write_control.py 아님)**: atomic_write_control.py는 A1 read-only(read/seed/
compute/preflight)이고 writer hot path가 import한다 — 거기 mutator를 넣으면 (a) A1 self-scope 위반 (b) 같은
모듈을 live writer가 이미 import해 negative no-importer trip-wire를 걸 수 없어 dormancy 보장 약화. 별 island로
분리해 no-importer AST trip-wire로 잠근다(C6-2 선례).

**dormant**: app/ live 모듈이 본 모듈 import 0(no-importer AST trip-wire) — 유일 caller는
scripts/activate_atomic_fx.py(C6-8b, 미실행). atomic_write_control에서 **상수만** import(WriterMode/
CONTROL_ROW_FORMAT_VERSION/ATOMIC_SCHEMA_FLOOR/REQUIRED_WRITER_PROTOCOL_SEED) — A1 SYMBOLS(read_control_row/
compute_effective_mode) 미호출이라 A1 dormancy tripwire sanction 불요.

fail-closed 설계:
- **format fence**: 두 brick 모두 control_row_format_version==CONTROL_ROW_FORMAT_VERSION fence (compute_
  effective_mode line 117의 format-mismatch→HALT 정신 — corrupt/future format row를 atomic으로 mutate 금지).
- **fence param 검증**: expected_generation/expected_epoch는 non-bool int>=0 (bool True==1 슬립 + None/str
  TypeError 차단 → PRECONDITION_FAILED, target/protocol 검증과 일관).
- **_disambiguate fresh read**: populate_existing()로 identity-map stale 객체 회피(다른 세션 gen 전진을
  CAS_LOST로 정확 분류 — READ COMMITTED 전제). caller가 같은 세션에 row 선load했어도 정확.

§9 first-activation 시퀀스에서의 위치:
- `cas_request_halt`: legacy→halt (§9-6c durable halt — quiesce ACK보다 먼저 commit, §9:88).
- `cas_activate_atomic`: halt→atomic (§8 one-shot). ⚠️ §15상 이 단계 = **begin-atomic = atomic/blocked
  reconciliation 구간 진입**(writer atomic live + publish gate blocked) — finalize(verified→completed +
  asset ready)가 아님. command(C6-8b)가 cas_begin_cutover와 같은 tx로 묶어 atomic/blocked를 만든다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from app.atomic_cutover_durable import CasResult
from app.atomic_write_control import (
    ATOMIC_SCHEMA_FLOOR,
    CONTROL_ROW_FORMAT_VERSION,
    REQUIRED_WRITER_PROTOCOL_SEED,
    WriterMode,
)
from app.models import AtomicWriteControl, get_utc_now

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _valid_fence_int(v: object) -> bool:
    """fence/expected 값이 non-bool int >= 0 (bool는 int 서브클래스라 명시 배제, None/str 차단)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _disambiguate(
    db: "Session", *, expected_generation: int, expected_epoch: Optional[int] = None
) -> CasResult:
    """rowcount==0 → CAS_LOST(mode_generation/activation_epoch 전진=concurrent·이미 적용) vs
    PRECONDITION_FAILED(requested_mode/format/param 불일치) 구분.

    **populate_existing()** = identity-map의 stale 객체 대신 DB fresh state(다른 세션이 commit한 gen 전진을
    정확히 관측, READ COMMITTED 전제 — 일반 first 쿼리는 선load 시 stale 반환). C6-2 _disambiguate_control
    대칭이되 stale-safe.
    """
    cur = (
        db.query(AtomicWriteControl)
        .populate_existing()
        .filter(AtomicWriteControl.id == 1)
        .first()
    )
    if cur is None:
        return CasResult.PRECONDITION_FAILED
    # format mismatch = non-retryable precondition (corrupt/future format, 재시도 무의미) — gen/epoch 전진보다
    # 먼저 분류(codex P2: format mismatch + gen 전진 동시에 CAS_LOST 오분류 회피).
    if cur.control_row_format_version != CONTROL_ROW_FORMAT_VERSION:
        return CasResult.PRECONDITION_FAILED
    if isinstance(cur.mode_generation, int) and not isinstance(cur.mode_generation, bool) \
            and cur.mode_generation > expected_generation:
        return CasResult.CAS_LOST
    if expected_epoch is not None and isinstance(cur.activation_epoch, int) \
            and not isinstance(cur.activation_epoch, bool) and cur.activation_epoch > expected_epoch:
        return CasResult.CAS_LOST
    return CasResult.PRECONDITION_FAILED


def cas_request_halt(db: "Session", *, expected_generation: int) -> CasResult:
    """legacy→halt: requested_mode=halt + mode_generation++ (§9-6c durable pre-quiesce halt).

    fence: id=1 AND control_row_format_version=CONTROL_ROW_FORMAT_VERSION AND mode_generation=expected AND
    requested_mode='legacy'. caller-commits. DB 예외 전파.
    ⚠️ activation 경로 전용(legacy→halt). admin atomic→halt(incident)는 별 brick(C6-8 범위 밖) —
    여기 fence를 legacy로 한정해 atomic을 실수로 halt downgrade하지 않게(§3 atomic→legacy 금지 정신).
    """
    if not _valid_fence_int(expected_generation):
        return CasResult.PRECONDITION_FAILED
    result = (
        db.query(AtomicWriteControl)
        .filter(
            AtomicWriteControl.id == 1,
            AtomicWriteControl.control_row_format_version == CONTROL_ROW_FORMAT_VERSION,
            AtomicWriteControl.mode_generation == expected_generation,
            AtomicWriteControl.requested_mode == WriterMode.LEGACY,
        )
        .update(
            {
                "requested_mode": WriterMode.HALT,
                "mode_generation": expected_generation + 1,
            },
            synchronize_session=False,
        )
    )
    if result == 1:
        return CasResult.APPLIED
    return _disambiguate(db, expected_generation=expected_generation)


def cas_activate_atomic(
    db: "Session",
    *,
    expected_generation: int,
    expected_epoch: int,
    required_writer_protocol: int,
    target_write_schema_version: int,
    now: Optional[datetime] = None,
) -> CasResult:
    """halt→atomic §8 one-shot: requested_mode=atomic + activation_epoch++ + mode_generation++ +
    required_writer_protocol + target_write_schema_version + activated_at (단일 UPDATE).

    fence(equality): id=1 AND control_row_format_version=CONTROL_ROW_FORMAT_VERSION AND
    mode_generation=expected_generation AND activation_epoch=expected_epoch AND requested_mode='halt'.
    param 검증(expected_* non-bool int>=0 / target>=ATOMIC_SCHEMA_FLOOR / protocol>=seed / now datetime)은
    PRECONDITION_FAILED — floor 미만 atomic은 compute_effective_mode가 halt로 떨궈 silent 무력화하므로 무의미한
    activation을 차단(crash-early). caller-commits. DB 예외 전파. activated_at = now(naive UTC 정규화) or
    get_utc_now().

    ⚠️ 이 brick은 writer-axis만 — §15 begin-atomic(atomic/blocked)은 command가 cas_begin_cutover와 같은 tx로
    합성. mode_generation 2회 증가(halt +1, 본 one-shot +1)는 §9:87-89 every-transition fencing 의도.
    """
    if not _valid_fence_int(expected_generation) or not _valid_fence_int(expected_epoch):
        return CasResult.PRECONDITION_FAILED
    if (isinstance(target_write_schema_version, bool)
            or not isinstance(target_write_schema_version, int)
            or target_write_schema_version < ATOMIC_SCHEMA_FLOOR):
        return CasResult.PRECONDITION_FAILED
    if (isinstance(required_writer_protocol, bool)
            or not isinstance(required_writer_protocol, int)
            or required_writer_protocol < REQUIRED_WRITER_PROTOCOL_SEED):
        return CasResult.PRECONDITION_FAILED
    if now is not None and not isinstance(now, datetime):
        return CasResult.PRECONDITION_FAILED
    # activated_at = naive UTC (컬럼이 tz-naive DateTime — aware 입력은 UTC naive로 정규화, get_utc_now 정합)
    if now is None:
        activated_at = get_utc_now()
    elif now.tzinfo is not None:
        activated_at = now.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        activated_at = now
    result = (
        db.query(AtomicWriteControl)
        .filter(
            AtomicWriteControl.id == 1,
            AtomicWriteControl.control_row_format_version == CONTROL_ROW_FORMAT_VERSION,
            AtomicWriteControl.mode_generation == expected_generation,
            AtomicWriteControl.activation_epoch == expected_epoch,
            AtomicWriteControl.requested_mode == WriterMode.HALT,
        )
        .update(
            {
                "requested_mode": WriterMode.ATOMIC,
                "activation_epoch": expected_epoch + 1,
                "mode_generation": expected_generation + 1,
                "required_writer_protocol": required_writer_protocol,
                "target_write_schema_version": target_write_schema_version,
                "activated_at": activated_at,
            },
            synchronize_session=False,
        )
    )
    if result == 1:
        return CasResult.APPLIED
    return _disambiguate(db, expected_generation=expected_generation, expected_epoch=expected_epoch)
