"""P1 atomic-write control plane — read/seed/compute helpers (PR D / P1b — A1).

P1_COMMON_BASE_DESIGN.md §3/§4/§6/§7/§9의 control plane을 위한 도메인 모듈.

A1 범위 (behavior-change-0, dormant):
    - control table(AtomicWriteControl) seed/read + effective-mode 계산(§3/§7) +
      preflight 비교(§6) 정의만.
    - **compute_effective_mode / read_control_row는 어떤 writer/broadcast/mirror도
      호출하지 않는다.** 유일한 호출처 = admin status endpoint + tests. writer가
      effective-mode를 consume하는 enforcement는 A2+ (이 모듈은 순수 계산만 제공).
    - main.py에 두지 않고 도메인 모듈로 분리 (memory: project_main_py_helper_placement
      — firebase_admin import chain을 단위 테스트에서 배제).

상수는 env toggle이 아니라 코드 상수 (image protocol range는 코드 property이지 환경
property가 아님; A1엔 enforcement가 없으므로 unused env toggle은 의미만 흐림).

이름 'atomic_write_control': 기존 broadcast hot path 개념과 구분 (broadcast가 아니라
atomic write mode control plane).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import AtomicWriteControl


# ── 코드 상수 (§5/§6) ──────────────────────────────────────────────────────────
# control table row 형식 버전. row.control_row_format_version와 불일치 = halt(§7).
CONTROL_ROW_FORMAT_VERSION = 1

# singleton seed 값 (§9 step 2). 셋 다 1 = legacy/v1 (§5: 단일 "version" 아님 —
# activation 시 §8 one-shot이 required_writer_protocol + target_write_schema를
# 함께 bump하고 control_row_format_version은 row 형식만 독립 추적).
TARGET_WRITE_SCHEMA_VERSION_SEED = 1
REQUIRED_WRITER_PROTOCOL_SEED = 1

# 현재 실행 이미지가 지원하는 writer protocol 범위 (§6 preflight 비교용).
# 코드 property → release마다 bump. C6-FLIP-RELEASE: MAX 1→2 (image가 atomic v2 writer 지원).
# MIN=1 유지 — image는 legacy v1도 지원해야 함(halt/legacy phase + rollback-to-legacy).
# ⚠️ behavior-change-0: compute_effective_mode는 IMAGE_MAX/MIN 미참조(:101-139) → 이 bump은 writer
# effective-mode 불변. capability gate(activate_atomic_fx.py:761) + 진단 preflight(:141)만 영향.
IMAGE_MIN_WRITER_PROTOCOL = 1
IMAGE_MAX_WRITER_PROTOCOL = 2

# atomic 실효 진입에 필요한 target_write_schema_version 하한 (§3 'schema 검사').
# atomic write schema = v2. A1엔 dormant (seed target_write_schema_version=1).
ATOMIC_SCHEMA_FLOOR = 2


class WriterMode:
    """3-state writer mode (§3). String 상수 (DB는 String 컬럼 — 코드베이스 관례)."""

    LEGACY = "legacy"
    ATOMIC = "atomic"
    HALT = "halt"


VALID_MODES = (WriterMode.LEGACY, WriterMode.ATOMIC, WriterMode.HALT)


def bootstrap_atomic_write_control(db: "Session") -> "AtomicWriteControl":
    """singleton row id=1을 idempotent seed (§9 step 2).

    이미 있으면 그대로 반환(값 미변경). 없으면 legacy / activation_epoch=0 /
    mode_generation=0 / required_writer_protocol=초기값으로 INSERT + commit.

    migration script(scripts/migrate_atomic_write_control.py)가 호출. 내부 commit —
    호출측은 별도 commit 불요 (migration-safety: 명시 db.close()는 호출측 책임).
    """
    # lazy import — dormant 모듈 경량 유지 (compute/preflight만 쓰는 path는 models 미pull)
    from app.models import AtomicWriteControl

    existing = db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).first()
    if existing is not None:
        return existing

    row = AtomicWriteControl(
        id=1,
        control_row_format_version=CONTROL_ROW_FORMAT_VERSION,
        target_write_schema_version=TARGET_WRITE_SCHEMA_VERSION_SEED,
        required_writer_protocol=REQUIRED_WRITER_PROTOCOL_SEED,
        activation_epoch=0,
        mode_generation=0,
        requested_mode=WriterMode.LEGACY,
    )
    db.add(row)
    db.commit()
    return row


def read_control_row(db: "Session") -> Optional["AtomicWriteControl"]:
    """singleton row id=1 조회. 없으면 None (§9: 부재 = 호출측에서 halt 취급)."""
    from app.models import AtomicWriteControl

    return db.query(AtomicWriteControl).filter(AtomicWriteControl.id == 1).first()


def compute_effective_mode(row: Optional["AtomicWriteControl"]) -> str:
    """실효 모드 계산 (§3 + §7 fail-closed). 순수 함수 (side-effect 0).

    **DORMANT (A1)**: 정의만 — admin status endpoint + tests에서만 호출.
    어떤 writer/broadcast/mirror도 이 함수를 호출하지 않는다 (behavior-change-0 경계).
    A2에서 writer가 cached effective-mode를 읽는 wiring이 추가됨.

    fail-closed 규칙:
        - row 부재(None) → halt (caller가 control_read_error로도 surface)
        - requested_mode enum corruption → halt
        - control_row_format_version 불일치 → halt
        - requested_mode=halt → halt
        - requested_mode=legacy: activation 이력(activation_epoch>0) 있으면 halt, 없으면 legacy
        - requested_mode=atomic: activation 이력 + schema floor 충족 시 atomic, 아니면 halt
    """
    if row is None:
        return WriterMode.HALT
    if row.requested_mode not in VALID_MODES:
        return WriterMode.HALT
    if row.control_row_format_version != CONTROL_ROW_FORMAT_VERSION:
        return WriterMode.HALT
    # numeric corruption fail-closed (defense-in-depth — DB CHECK/NOT NULL이 1차 방어이나,
    # §7 fail-closed 함수로서 None/음수/non-int row도 halt. 아래 > / >= 비교 전 검증).
    if not isinstance(row.activation_epoch, int) or isinstance(row.activation_epoch, bool) or row.activation_epoch < 0:
        return WriterMode.HALT
    if (not isinstance(row.target_write_schema_version, int)
            or isinstance(row.target_write_schema_version, bool)
            or row.target_write_schema_version < 1):  # schema version은 1(legacy)부터 — < 1 = corruption
        return WriterMode.HALT
    if row.requested_mode == WriterMode.HALT:
        return WriterMode.HALT
    if row.requested_mode == WriterMode.LEGACY:
        # §3: activation 이력 있는데 legacy 요청 → halt (un-activate 불가)
        return WriterMode.HALT if row.activation_epoch > 0 else WriterMode.LEGACY
    # requested_mode == ATOMIC
    if row.activation_epoch > 0 and row.target_write_schema_version >= ATOMIC_SCHEMA_FLOOR:
        return WriterMode.ATOMIC
    return WriterMode.HALT


def evaluate_preflight(row: Optional["AtomicWriteControl"]) -> Dict[str, Any]:
    """§6 preflight 비교 — **A1 진단-only (report만, 차단 안 함)**.

    실제 recreate-blocking preflight(§6)는 배포 스크립트/신규-이미지 one-shot에 있고
    (구 이미지 rollback도 차단해야 하므로 old binary 안이 아님), A2+/bootstrap 영역.
    이 함수는 admin status endpoint가 비교 결과를 보여주기만 한다.

    조건(§6): image_min_protocol ≤ required_writer_protocol ≤ image_max_protocol.
    """
    image_min = IMAGE_MIN_WRITER_PROTOCOL
    image_max = IMAGE_MAX_WRITER_PROTOCOL
    required = None if row is None else row.required_writer_protocol

    if required is None:
        passed = False
        reason = "control row 없음 — required_writer_protocol 미상"
    else:
        passed = image_min <= required <= image_max
        reason = None if passed else (
            f"image protocol range [{image_min}, {image_max}]가 "
            f"required_writer_protocol={required} 미충족"
        )

    return {
        "image_min_protocol": image_min,
        "image_max_protocol": image_max,
        "required_writer_protocol": required,
        "passed": passed,
        "reason": reason,
    }


def get_control_state_dict(row: "AtomicWriteControl") -> Dict[str, Any]:
    """control row를 admin/status JSON projection으로 변환 (read-only).

    activated_at/updated_at은 to_kst_isoformat (naive UTC → KST ISO).
    """
    # lazy import — crud(writer-heavy)를 compute/preflight path에서 pull하지 않도록
    from app.crud import to_kst_isoformat

    return {
        "id": row.id,
        "control_row_format_version": row.control_row_format_version,
        "target_write_schema_version": row.target_write_schema_version,
        "required_writer_protocol": row.required_writer_protocol,
        "activation_epoch": row.activation_epoch,
        "mode_generation": row.mode_generation,
        "requested_mode": row.requested_mode,
        "activated_at": to_kst_isoformat(row.activated_at),
        "updated_at": to_kst_isoformat(row.updated_at),
    }
