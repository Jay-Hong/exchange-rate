"""공식 관측 사실과 DB 적용 결과를 최종 IbkResult로 바꾸는 판정 어댑터.

이 모듈은 HTTP·Selenium을 실행하지 않고 DB에 쓰지도 않는다. 저장은 기존 crud가 수행하고
여기서는 그 반환값과 적용 후 DB 상태만 읽어 판정한다. 운영 경로에는 아직 연결하지 않는다.

`app.crawlers`는 import하지 않는다. `app/crawlers/__init__.py`가 ibk를 포함한 모든 크롤러를
import하므로, 여기서 crawlers를 참조하면 이후 ibk.py가 이 모듈을 쓰는 순간 순환이 된다.
은행명·통화 범위·공식 완료시각은 호출자가 사실로 넘긴다. 특히 완료시각은 화면 문자열이 아니라
호출자가 기존 조회기준일 규칙으로 만든 timezone-aware datetime이어야 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Callable, Mapping

from app import atomic_write_runtime, crud
from app.atomic_write_control import WriterMode
from app.ibk_result_protocol import (
    PAIRS,
    SCHEMA_VERSION,
    IbkReason,
    IbkResult,
    IbkSource,
    IbkStatus,
    encode_ibk_result,
)

REQUIRED_PAIRS: tuple[str, ...] = tuple(sorted(PAIRS))
PRESERVATION_REASONS = (IbkReason.PREOPEN_PENDING, IbkReason.OFFICIAL_NO_SESSION)
OFFICIAL_SOURCES = (IbkSource.OFFICIAL_GET, IbkSource.OFFICIAL_POST, IbkSource.OFFICIAL_SELENIUM)


class IbkResultBuildError(ValueError):
    """호출자 계약 위반. 운영 상태가 아니라 프로그래밍 오류를 뜻한다."""


class IbkWriteMode(str, Enum):
    """쓰기를 시도한 시점에 관측한 writer 게이트 상태."""

    LEGACY = "legacy"
    ATOMIC = "atomic"
    BLOCKED_UNINITIALIZED = "blocked_uninitialized"
    BLOCKED_ENFORCED = "blocked_enforced"

    @property
    def blocked(self) -> bool:
        return self in (IbkWriteMode.BLOCKED_UNINITIALIZED, IbkWriteMode.BLOCKED_ENFORCED)


def capture_write_mode() -> IbkWriteMode:
    """쓰기 직전에 호출한다. 나중에 다시 읽으면 쓰기 시점과 어긋난다.

    ATOMIC은 차단이 아니다. crud가 별 경로로 저장하므로 그 반환 0은 정상 unchanged다.
    """
    if not atomic_write_runtime.is_initialized():
        return IbkWriteMode.BLOCKED_UNINITIALIZED
    enforced = atomic_write_runtime.snapshot().enforced_action
    if enforced == WriterMode.ATOMIC:
        return IbkWriteMode.ATOMIC
    if enforced == WriterMode.LEGACY:
        return IbkWriteMode.LEGACY
    return IbkWriteMode.BLOCKED_ENFORCED


@dataclass(frozen=True)
class IbkDbSnapshot:
    """종료 시 DB에서 확인한 사실. checked=False면 어떤 통화도 단정하지 않는다."""

    checked: bool
    rates: Mapping[str, float] | None = None
    usable: tuple[str, ...] | None = None
    missing: tuple[str, ...] | None = None

    @classmethod
    def unknown(cls) -> "IbkDbSnapshot":
        return cls(False)

    @property
    def complete(self) -> bool | None:
        """확인하지 못했으면 완전성도 알 수 없다. 관측 통화로 대신 채우지 않는다."""
        if not self.checked:
            return None
        return set(self.usable or ()) == set(REQUIRED_PAIRS)


def _normalized_ranges(ranges: Any) -> dict[str, tuple[float, float]]:
    """필수 3통화의 범위가 모두 유효해야 한다. 설정 누락을 검증 생략으로 바꾸지 않는다."""
    if not isinstance(ranges, Mapping):
        raise IbkResultBuildError("RATE_RANGES_REQUIRED")
    normalized: dict[str, tuple[float, float]] = {}
    for pair in REQUIRED_PAIRS:
        try:
            low, high = ranges[pair]
            low, high = float(low), float(high)
        except (KeyError, TypeError, ValueError):
            raise IbkResultBuildError("RATE_RANGES_REQUIRED") from None
        if not (math.isfinite(low) and math.isfinite(high) and low <= high):
            raise IbkResultBuildError("RATE_RANGES_REQUIRED")
        normalized[pair] = (low, high)
    return normalized


def _usable_rate(entry: Any, bounds: tuple[float, float]) -> float | None:
    """행이 실제로 쓸 수 있는 값인지 검사한다. 행 존재만으로 인정하지 않는다."""
    if not isinstance(entry, Mapping):
        return None
    rate, stamp = entry.get("rate"), entry.get("timestamp")
    # 기존 DB는 UTC-naive datetime을 돌려주므로 tz는 요구하지 않되 타입은 확인한다.
    # 통화별 시각이 서로 다른 것은 change-only 저장의 정상이라 시각 일치는 요구하지 않는다.
    if not isinstance(stamp, datetime) or rate is None or isinstance(rate, bool):
        return None
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    low, high = bounds
    return value if low <= value <= high else None


def read_ibk_db_snapshot(
    db: Any,
    *,
    bank_name: str,
    ranges: Mapping[str, Any],
    reader: Callable[..., Any] | None = None,
) -> IbkDbSnapshot:
    """적용 후 DB를 다시 읽어 사용 가능한 통화를 확정한다.

    조회 예외는 '빈 DB'가 아니라 '확인 불가'다. 기존 crud 조회는 행이 없어도 예외 없이
    rate/timestamp를 None으로 채워 돌려주므로, 두 경우는 여기서 명시적으로 갈린다.
    """
    bounds = _normalized_ranges(ranges)
    fetch = reader if reader is not None else crud.get_last_bank_rates_with_ts
    try:
        rows = fetch(db, bank_name, list(REQUIRED_PAIRS))
    except Exception:
        return IbkDbSnapshot.unknown()
    if not isinstance(rows, Mapping):
        return IbkDbSnapshot.unknown()
    usable: dict[str, float] = {}
    for pair in REQUIRED_PAIRS:
        value = _usable_rate(rows.get(pair), bounds[pair])
        if value is not None:
            usable[pair] = value
    missing = tuple(pair for pair in REQUIRED_PAIRS if pair not in usable)
    return IbkDbSnapshot(True, dict(usable), tuple(sorted(usable)), missing)


@dataclass(frozen=True)
class IbkOfficialObservation:
    """공식 응답에서 검증한 사실. 그 자체로 판정이 아니며 DB 반영을 뜻하지도 않는다."""

    source: IbkSource
    service_date: date
    rates: Mapping[str, float]
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.source not in OFFICIAL_SOURCES:
            raise IbkResultBuildError("OFFICIAL_SOURCE_REQUIRED")
        if not isinstance(self.service_date, date) or isinstance(self.service_date, datetime):
            raise IbkResultBuildError("INVALID_SERVICE_DATE")
        if not isinstance(self.rates, Mapping) or set(self.rates) != set(REQUIRED_PAIRS):
            raise IbkResultBuildError("OFFICIAL_PAIRS_INCOMPLETE")
        for value in self.rates.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise IbkResultBuildError("INVALID_OFFICIAL_RATE")
        if not isinstance(self.completed_at, datetime) or self.completed_at.utcoffset() is None:
            raise IbkResultBuildError("NAIVE_COMPLETED_AT")


@dataclass(frozen=True)
class IbkPreservationGrant:
    """기존 값 유지가 정답인 상황임을 호출자가 확인했다는 사실.

    허용된 보존 상황 밖에서 회귀만 막은 경우에 이것을 주면 안 된다. 개장 전 판정에서
    이전 후보 검증까지 실패했다면 보존이 아니라 실패 원인을 넘겨야 한다.
    """

    reason: IbkReason

    def __post_init__(self) -> None:
        if self.reason not in PRESERVATION_REASONS:
            raise IbkResultBuildError("INVALID_PRESERVATION_REASON")


class IbkRetentionReason(str, Enum):
    """제출하지 않고 기존 값을 유지하기로 한 이유."""

    REGRESSION_GUARD = "regression_guard"  # 후보가 있었지만 명시적 회귀 가드로 제외
    NOT_OBSERVED = "not_observed"          # 이번 실행이 그 통화의 공식 후보를 얻지 못함


@dataclass(frozen=True)
class IbkIntendedState:
    """이번 실행이 끝났을 때 DB가 가져야 한다고 판단한 3통화 전체 상태.

    제출한 값과 유지하기로 한 값을 합쳐 필수 통화를 모두 설명해야 한다. 설명하지 못한
    통화가 있으면 정상 판정을 만들지 않는다. 유지하기로 한 값도 재조회로 확인한다.
    """

    submitted: Mapping[str, float] = field(default_factory=dict)
    retained: Mapping[str, float] = field(default_factory=dict)
    retention: Mapping[str, "IbkRetentionReason"] = field(default_factory=dict)
    unavailable: tuple[str, ...] = ()  # 후보도 기존 값도 없어 설명하지 못한 통화

    def __post_init__(self) -> None:
        for values in (self.submitted, self.retained):
            if not isinstance(values, Mapping):
                raise IbkResultBuildError("INVALID_INTENDED_STATE")
            for pair, value in values.items():
                if (pair not in REQUIRED_PAIRS or isinstance(value, bool)
                        or not isinstance(value, (int, float)) or not math.isfinite(float(value))):
                    raise IbkResultBuildError("INVALID_INTENDED_STATE")
        if not isinstance(self.unavailable, tuple) or any(
                pair not in REQUIRED_PAIRS for pair in self.unavailable):
            raise IbkResultBuildError("INVALID_INTENDED_STATE")
        groups = (set(self.submitted), set(self.retained), set(self.unavailable))
        if sum(len(group) for group in groups) != len(set().union(*groups)):
            raise IbkResultBuildError("INTENDED_STATE_OVERLAP")
        if set().union(*groups) != set(REQUIRED_PAIRS):
            raise IbkResultBuildError("INTENDED_STATE_INCOMPLETE")
        if set(self.retention) != set(self.retained) or not all(
                isinstance(reason, IbkRetentionReason) for reason in self.retention.values()):
            raise IbkResultBuildError("RETENTION_REASON_REQUIRED")

    @property
    def values(self) -> dict[str, float]:
        merged = {pair: float(value) for pair, value in self.retained.items()}
        merged.update({pair: float(value) for pair, value in self.submitted.items()})
        return merged

    @property
    def accounted(self) -> bool:
        """필수 3통화를 모두 값으로 설명했는가. 정상 판정의 선행 조건이다."""
        return not self.unavailable

    @property
    def guard_retained(self) -> tuple[str, ...]:
        return tuple(sorted(pair for pair, reason in self.retention.items()
                            if reason is IbkRetentionReason.REGRESSION_GUARD))


@dataclass(frozen=True)
class IbkObservationFailure:
    """공식 관측을 확정하지 못한 사실과 그 분류. 원인은 호출자가 정한다."""

    reason: IbkReason

    def __post_init__(self) -> None:
        if self.reason is IbkReason.NORMAL or self.reason in PRESERVATION_REASONS:
            raise IbkResultBuildError("INVALID_FAILURE_REASON")


@dataclass(frozen=True)
class IbkWriteAttempt:
    """기존 crud 호출 사실. 이 모듈은 쓰기를 수행하지 않는다."""

    attempted: bool = False
    write_mode: IbkWriteMode | None = None
    returned_count: int | None = None
    failed: bool = False

    def __post_init__(self) -> None:
        if self.attempted and self.write_mode is None:
            raise IbkResultBuildError("WRITE_MODE_REQUIRED")
        if not self.attempted and (self.returned_count is not None or self.failed):
            raise IbkResultBuildError("UNATTEMPTED_WRITE_HAS_RESULT")
        count = self.returned_count
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise IbkResultBuildError("INVALID_WRITE_COUNT")

    @classmethod
    def none(cls) -> "IbkWriteAttempt":
        return cls()

    @property
    def blocked(self) -> bool:
        return bool(self.attempted and self.write_mode is not None and self.write_mode.blocked)


def _matches(expected: Mapping[str, float] | None, stored: Mapping[str, float] | None) -> bool:
    """기대한 값이 실제 DB에 있는지 대조한다. 통화 부분집합도 받는다.

    기대집합이 비어 있으면 대조할 것이 없다는 뜻이므로 참이다. 부재를 불일치로 바꾸면
    '전부 설명 불가' 같은 경계가 엉뚱한 분기로 샌다.
    rate 컬럼이 SQLAlchemy Float(=float8)이라 float 왕복이 정확하므로 정확 비교를 쓴다.
    허용오차를 두면 미세한 오적용을 놓친다.
    """
    if not isinstance(stored, Mapping):
        return False
    return all(pair in stored and stored[pair] == float(value)
               for pair, value in (expected or {}).items())


def _result(
    *,
    status: IbkStatus,
    reason: IbkReason,
    run_id: str,
    observed_at: datetime,
    expected_service_date: date,
    db: IbkDbSnapshot,
    source: IbkSource | None = None,
    observed_pairs: tuple[str, ...] | None = None,
    observed_service_date: date | None = None,
    official_completed_at: datetime | None = None,
    changed_count: int | None = None,
) -> IbkResult:
    """DB 관련 필드는 오직 최종 DB 확인 결과에서만 채운다."""
    return IbkResult(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        status=status,
        reason=reason,
        observed_at=observed_at.isoformat(),
        expected_service_date=expected_service_date.isoformat(),
        observed_service_date=observed_service_date.isoformat() if observed_service_date is not None else None,
        official_completed_at=official_completed_at.isoformat() if official_completed_at is not None else None,
        source=source,
        observed_pairs=observed_pairs,
        preserved_pairs=db.usable if db.checked else None,
        missing_pairs=db.missing if db.checked else None,
        changed_count=changed_count,
        db_snapshot_complete=db.complete,
    )


def _check_intent(intended: IbkIntendedState, official: IbkOfficialObservation | None,
                  write: IbkWriteAttempt) -> None:
    """의도한 최종 상태가 관측 사실·쓰기 사실과 앞뒤가 맞는지 확인한다."""
    if bool(intended.submitted) != bool(write.attempted):
        raise IbkResultBuildError("WRITE_ATTEMPT_MISMATCH")
    if official is None:
        # 공식 후보가 없으면 넘길 값도 없고, 제외 사유는 '후보 없음'뿐이다.
        if intended.submitted or any(reason is not IbkRetentionReason.NOT_OBSERVED
                                     for reason in intended.retention.values()):
            raise IbkResultBuildError("RETENTION_REASON_CONFLICT")
        return
    if intended.unavailable:
        # 검증된 후보가 3통화를 모두 제공했으므로 '설명 불가'는 성립하지 않는다.
        raise IbkResultBuildError("CANDIDATE_COVERS_ALL_PAIRS")
    for pair, value in intended.submitted.items():
        if float(value) != float(official.rates[pair]):
            raise IbkResultBuildError("SUBMITTED_NOT_FROM_CANDIDATE")
    # 후보가 3통화를 모두 담고 있으므로 제외는 명시적 회귀 가드로만 설명된다.
    if any(reason is not IbkRetentionReason.REGRESSION_GUARD
           for reason in intended.retention.values()):
        raise IbkResultBuildError("RETENTION_REASON_CONFLICT")


def build_ibk_result(
    *,
    run_id: str,
    expected_service_date: date,
    observed_at: datetime,
    db_after: IbkDbSnapshot,
    intended: IbkIntendedState | None = None,
    official: IbkOfficialObservation | None = None,
    preservation: IbkPreservationGrant | None = None,
    failure: IbkObservationFailure | None = None,
    write: IbkWriteAttempt | None = None,
    validate: bool = True,
) -> IbkResult:
    """확인한 사실만으로 최종 상태를 만든다. 관측 성공을 저장 성공으로 승격하지 않는다.

    공식 관측과 보존 승인은 별 축이다. 개장 전 보충은 두 사실이 동시에 성립하므로,
    과거 후보의 날짜·환율·완료시각을 유지한 채 보존으로 판정한다. failure는 단독으로 온다.

    정상 판정 전에 필수 3통화의 의도한 최종값을 모두 설명하고 재조회와 대조한다. 제출한 값뿐
    아니라 유지하기로 한 값의 변경도 잡는다. 쓰기 차단 자체는 판정을 뒤집지 않으며, 의도한
    상태가 이미 DB에 있으면 현재 공식 관측을 부정하지 않는다. 차단 사실은 기존 write-mode
    skip 계측이 남긴다. 공식 메타데이터는 IbkOfficialObservation 에서만 온다.
    """
    write = write if write is not None else IbkWriteAttempt.none()
    if failure is not None and (official is not None or preservation is not None):
        raise IbkResultBuildError("FAILURE_IS_EXCLUSIVE")
    if failure is None and official is None and preservation is None:
        raise IbkResultBuildError("OBSERVATION_FACT_REQUIRED")
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise IbkResultBuildError("NAIVE_OBSERVED_AT")
    if not isinstance(expected_service_date, date) or isinstance(expected_service_date, datetime):
        raise IbkResultBuildError("INVALID_EXPECTED_SERVICE_DATE")
    if not isinstance(db_after, IbkDbSnapshot):
        raise IbkResultBuildError("DB_SNAPSHOT_REQUIRED")
    if failure is not None:
        if intended is not None:
            raise IbkResultBuildError("FAILURE_HAS_NO_INTENDED_STATE")
    elif not isinstance(intended, IbkIntendedState):
        raise IbkResultBuildError("INTENDED_STATE_REQUIRED")
    if official is not None and preservation is not None:
        if official.service_date >= expected_service_date:
            raise IbkResultBuildError("PRESERVATION_WITH_CURRENT_OBSERVATION")
    elif official is not None and official.service_date != expected_service_date:
        raise IbkResultBuildError("SERVICE_DATE_NOT_CURRENT")
    if intended is not None:
        _check_intent(intended, official, write)

    complete = db_after.complete
    changed = write.returned_count
    completed_at: datetime | None = None
    if official is not None:
        source, observed_pairs = official.source, REQUIRED_PAIRS
        observed_day, completed_at = official.service_date, official.completed_at
    elif preservation is not None:
        # 공식 후보 없이 기존 DB만 보존한다. 공식 관측 메타데이터를 만들어 넣지 않는다.
        source, observed_pairs, observed_day = IbkSource.DB_SNAPSHOT, None, None
    else:
        source, observed_pairs, observed_day = None, None, None

    common = dict(
        run_id=run_id,
        observed_at=observed_at,
        expected_service_date=expected_service_date,
        db=db_after,
        source=source,
        observed_pairs=observed_pairs,
        observed_service_date=observed_day,
        official_completed_at=completed_at,
    )
    degraded_or_failed = IbkStatus.DEGRADED if complete else IbkStatus.FAILED

    if write.failed or not db_after.checked:
        # 저장 결과나 최종 DB를 확정하지 못했다. 저장 완료로 판정하지 않고 개수도 만들지 않는다.
        result = _result(status=IbkStatus.FAILED, reason=IbkReason.DB_ERROR,
                         changed_count=None, **common)
    elif intended is not None and not intended.accounted:
        # 설명하지 못한 통화가 남았다. DB에 값이 있어도 적용 검증을 완성하지 못했다는 뜻이며,
        # 실제 db_snapshot_complete·missing_pairs 는 DB 확인 결과 그대로 남는다.
        # 공식 후보가 있는 입력은 위에서 계약 오류로 걸러지므로 보존 사유만 도달한다.
        result = _result(status=IbkStatus.FAILED, reason=preservation.reason,
                         changed_count=changed, **common)
    elif intended is not None and not _matches(intended.retained, db_after.rates):
        # 유지하기로 한 값이 바뀌었다. 쓰기 차단으로는 설명되지 않는다.
        result = _result(status=degraded_or_failed, reason=IbkReason.DB_APPLY_MISMATCH,
                         changed_count=changed, **common)
    elif intended is not None and not _matches(intended.submitted, db_after.rates):
        # 제출한 값만 미반영이다. 차단이 확인된 경우에만 차단을 원인으로 쓴다.
        result = _result(status=degraded_or_failed,
                         reason=(IbkReason.WRITE_POLICY_BLOCKED if write.blocked
                                 else IbkReason.DB_APPLY_MISMATCH),
                         changed_count=changed, **common)
    elif preservation is not None:
        result = _result(status=IbkStatus.PRESERVED if complete else IbkStatus.FAILED,
                         reason=preservation.reason, changed_count=changed, **common)
    elif official is not None:
        if intended.guard_retained:
            # 명시적 회귀 가드로 후보와 다른 값을 유지했다. 정상 관측이 아니다.
            result = _result(status=degraded_or_failed, reason=IbkReason.REGRESSION_GUARD,
                             changed_count=changed, **common)
        elif changed is None:
            raise IbkResultBuildError("WRITE_RETURN_REQUIRED")
        elif complete:
            result = _result(status=IbkStatus.OBSERVED, reason=IbkReason.NORMAL,
                             changed_count=changed, **common)
        else:
            result = _result(status=IbkStatus.FAILED, reason=IbkReason.DB_APPLY_MISMATCH,
                             changed_count=changed, **common)
    else:
        result = _result(status=degraded_or_failed, reason=failure.reason,
                         changed_count=changed, **common)

    if validate:
        # codec 자기검사. 허용되지 않는 조합은 배포가 아니라 여기서 즉시 드러난다.
        encode_ibk_result(result)
    return result
