"""P1b A2-4 — revision-ID 확보 primitives (§16, pure dormant).

§11 monotonic compare가 쓰는 canonical revision `(canonical_epoch_us, id)`의 **확보·정규화**
방식. direct write(flush-row-ref) + mirror/bootstrap(internal selector) 두 공급원이
**같은 row → 같은 revision**을 산출해야 함 (§11 conflict/skipped_newer false-positive 방지).

이 모듈은 **stdlib only** — crud/models import 0 (계층 역전/순환 회피, codex plan-review).
StagedRateChange(ChangedRate + ORM row 결합)는 crud.py에 둠.

**dormant**: atomic gate 뒤에서만 쓰임 (A3 orchestrator 재구성 + C6 mirror/bootstrap wiring).
legacy/halt live path는 본 모듈을 호출하지 않음 (behavior-change-0).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Tuple, runtime_checkable

# revision = (canonical μs epoch, integer row id). 정수만 — ISO/float 금지 (§11/§16).
Revision = Tuple[int, int]

_UNIX_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


@runtime_checkable
class RevisionRow(Protocol):
    """revision 산출에 필요한 최소 row 계약 (id + timestamp).

    BankExchangeRate / InvestingExchangeRate / SourceRate 모두 만족. ORM Base 전체가
    아니라 이 두 속성만 노출 (codex plan-review — `object`는 너무 넓음).
    """

    id: int
    timestamp: datetime


def to_canonical_epoch_us(dt: datetime) -> int:
    """datetime → signed integer μs epoch (§16 canonical 함수).

    naive는 UTC로 간주(프로젝트 모델은 naive UTC — models.get_utc_now), aware는 UTC로 변환.
    float `.timestamp()` 미사용 — μs 스케일 rounding 회피. 음수 epoch(1970 이전)도
    timedelta 정규화로 안전.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    delta = dt - _UNIX_EPOCH_UTC
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def revision_from_row(row: RevisionRow) -> Revision:
    """row → canonical revision `(canonical_epoch_us(timestamp), id)`.

    direct write(flush 후 StagedRateChange.row) + internal selector가 **공유**하는 단일
    구성 함수 — 같은 row면 같은 revision 보장 (§16 핵심 invariant).

    **fail-closed (Crash Early)**: `id`가 정수 아니면(특히 flush 전 None) ValueError.
    §11 compare는 integer id를 전제하므로 `(epoch, None)`을 조용히 반환하면 안 됨.
    `isinstance(int)` 체크라 유효 id 0은 통과(`not row.id`로 0을 falsy 오거부 금지).
    """
    if not isinstance(row.id, int):
        raise ValueError(
            f"revision_from_row: row.id가 정수 아님({row.id!r}) — flush() 전 호출 의심 "
            "(§16: revision은 (canonical_epoch_us, integer id), None/non-int 금지)"
        )
    return (to_canonical_epoch_us(row.timestamp), row.id)


@dataclass(frozen=True)
class RevisionedRate:
    """internal revision-aware selector의 반환 DTO (mirror/bootstrap source용).

    ORM row를 downstream에 흘리지 않기 위한 plain 값 — raw timestamp(KST iso 변환 전) +
    revision 동반. 공개 selector(crud.select_*)의 dict shape와 분리 (id/revision 미노출 유지).
    """

    source: str
    asset: str
    rate: float
    timestamp: datetime
    revision: Revision
