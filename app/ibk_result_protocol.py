"""IBK 전용 결과 프로토콜 — 선택적 runner/부모 연결 후보에서 사용한다.

이 모듈은 메시지의 형식·기본 일관성과 재등록 정책만 검사한다. 운영 기본 경로는
아직 legacy다. 공식 HTML/DB 검증과 실제 경보 발송은 이 codec의 보장이 아니다.
Legacy result를 최종 outcome으로 자동 승격하지 않는다.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum

SENTINEL = b"IBK_RESULT_JSON="
SCHEMA_VERSION = 1
MAX_RESULT_BYTES = 8192  # JSON payload만; sentinel/newline 제외
MAX_STDOUT_BYTES = 256 * 1024  # capture 모듈도 수집 중 같은 기본 보관 상한 적용
PAIRS = frozenset({"usd-krw", "jpy-krw", "eur-krw"})
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class IbkStatus(str, Enum):
    OBSERVED = "OBSERVED"
    PRESERVED = "PRESERVED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class IbkReason(str, Enum):
    NORMAL = "NORMAL"
    PREOPEN_PENDING = "PREOPEN_PENDING"
    OFFICIAL_NO_SESSION = "OFFICIAL_NO_SESSION"
    REGRESSION_GUARD = "REGRESSION_GUARD"
    AMBIGUOUS_TABLE_ABSENT = "AMBIGUOUS_TABLE_ABSENT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    DB_ERROR = "DB_ERROR"  # DB 처리·조회 자체가 실패해 최종 상태를 확정하지 못함
    DB_APPLY_MISMATCH = "DB_APPLY_MISMATCH"  # 재조회는 됐으나 의도한 최종값과 다름
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    WRITE_POLICY_BLOCKED = "WRITE_POLICY_BLOCKED"
    SELENIUM_STRICT_REJECTED = "SELENIUM_STRICT_REJECTED"


class IbkSource(str, Enum):
    OFFICIAL_GET = "official_get"
    OFFICIAL_POST = "official_post"
    OFFICIAL_SELENIUM = "official_selenium"
    DB_SNAPSHOT = "db_snapshot"  # 기존 DB에는 provenance가 없다.


@dataclass(frozen=True)
class IbkResult:
    """관측 통화는 공식 후보, 보존/누락 통화는 최종 DB snapshot 기준이다.

    preserved_pairs는 이번 실행이 끝날 때 DB에서 확인한 사용 가능한 모든 통화다.
    이번에 새로 저장한 통화도 포함하며, '이번에 쓰지 않은 통화'라는 뜻이 아니다.
    공식 후보를 읽었지만 쓰기 차단으로 DB에 못 넣었다면 observed와 missing이
    겹칠 수 있다. 관측 여부만으로 저장 성공이나 DB 완전성을 추론하지 않는다.
    """

    schema_version: int
    run_id: str
    status: IbkStatus
    reason: IbkReason
    observed_at: str
    expected_service_date: str | None
    observed_service_date: str | None
    official_completed_at: str | None
    source: IbkSource | None
    observed_pairs: tuple[str, ...] | None
    preserved_pairs: tuple[str, ...] | None
    missing_pairs: tuple[str, ...] | None
    changed_count: int | None
    db_snapshot_complete: bool | None


class IbkProtocolError(ValueError):
    """고정 코드만 노출한다. JSON/로그/잘못된 필드 값은 예외에 넣지 않는다."""


def validate_ibk_run_id(value: object) -> None:
    """부모 생성/runner argv/결과 검사에서 같은 실행 ID 계약을 사용한다."""
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise IbkProtocolError("INVALID_EXPECTED_RUN_ID")


def _date_field(value: object, *, timestamp: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise IbkProtocolError("INVALID_DATE")
    try:
        if timestamp:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
                raise ValueError
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError
        elif date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise IbkProtocolError("INVALID_DATE") from None


def _pairs(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) > 3
            or any(not isinstance(pair, str) or pair not in PAIRS for pair in value)
            or len(set(value)) != len(value)):
        raise IbkProtocolError("INVALID_PAIRS")
    return tuple(sorted(value))


def _validate(payload: object, expected_run_id: str) -> IbkResult:
    if type(payload) is not dict or set(payload) != set(IbkResult.__dataclass_fields__):
        raise IbkProtocolError("INVALID_FIELDS")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise IbkProtocolError("INVALID_VERSION")
    if (not isinstance(payload["run_id"], str) or not _RUN_ID.fullmatch(payload["run_id"])
            or payload["run_id"] != expected_run_id):
        raise IbkProtocolError("RUN_ID_MISMATCH")
    try:
        status = IbkStatus(payload["status"])
        reason = IbkReason(payload["reason"])
        source = None if payload["source"] is None else IbkSource(payload["source"])
    except (ValueError, TypeError):
        raise IbkProtocolError("INVALID_ENUM") from None
    if payload["observed_at"] is None:
        raise IbkProtocolError("MISSING_OBSERVED_AT")
    for key in ("observed_at", "official_completed_at"):
        _date_field(payload[key], timestamp=True)
    for key in ("expected_service_date", "observed_service_date"):
        _date_field(payload[key])
    changed = payload["changed_count"]
    if changed is not None and (type(changed) is not int or not 0 <= changed <= 3):
        raise IbkProtocolError("INVALID_CHANGED_COUNT")
    complete = payload["db_snapshot_complete"]
    if complete is not None and type(complete) is not bool:
        raise IbkProtocolError("INVALID_DB_COMPLETE")
    observed = _pairs(payload["observed_pairs"])
    preserved = _pairs(payload["preserved_pairs"])
    missing = _pairs(payload["missing_pairs"])
    # 공식 관측은 쓰기 성공과 다르다. DB의 preserved/missing만 상호 배타적이다.
    db_pairs = set(preserved or ())
    if db_pairs & set(missing or ()):
        raise IbkProtocolError("INCONSISTENT_PAIRS")
    if preserved is not None and missing is not None and (db_pairs | set(missing)) != PAIRS:
        raise IbkProtocolError("INCONSISTENT_DB_COMPLETE")
    if complete is True and (missing != () or db_pairs != PAIRS):
        raise IbkProtocolError("INCONSISTENT_DB_COMPLETE")
    if complete is False and (db_pairs == PAIRS or missing == ()):
        raise IbkProtocolError("INCONSISTENT_DB_COMPLETE")
    if status is not IbkStatus.FAILED and complete is not True:
        raise IbkProtocolError("INCOMPLETE_NONFAILED_RESULT")
    if status is IbkStatus.OBSERVED:
        if (reason is not IbkReason.NORMAL or source in (None, IbkSource.DB_SNAPSHOT)
                or payload["expected_service_date"] is None
                or payload["observed_service_date"] != payload["expected_service_date"]
                or payload["official_completed_at"] is None
                or set(observed or ()) != PAIRS or changed is None):
            raise IbkProtocolError("INVALID_OBSERVED")
    elif status is IbkStatus.PRESERVED:
        if (reason not in (IbkReason.PREOPEN_PENDING, IbkReason.OFFICIAL_NO_SESSION)
                or payload["expected_service_date"] is None):
            raise IbkProtocolError("INVALID_PRESERVED")
    elif reason is IbkReason.NORMAL:
        raise IbkProtocolError("INVALID_FAILURE_REASON")
    if (status is IbkStatus.FAILED and reason is IbkReason.SELENIUM_STRICT_REJECTED
            and complete is True):
        raise IbkProtocolError("INVALID_REJECTED_STATUS")
    expected_day, observed_day = payload["expected_service_date"], payload["observed_service_date"]
    if expected_day is not None and observed_day is not None and observed_day > expected_day:
        raise IbkProtocolError("FUTURE_SERVICE_DATE")
    return IbkResult(**{
        **payload, "status": status, "reason": reason, "source": source,
        "observed_pairs": observed, "preserved_pairs": preserved, "missing_pairs": missing,
    })


def _unique_object(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise IbkProtocolError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise IbkProtocolError("NONFINITE_JSON")


def decode_ibk_result(stdout: bytes, expected_run_id: str, *, truncated: bool = False) -> IbkResult:
    """종료된 프로세스의 유한 stdout 검사. stream drain/kill 자체는 구현하지 않는다.

    일반 로그는 버리고 행 시작 sentinel이 정확히 1개인 경우에만 수락한다.
    collector가 잘라낸 출력은 정상 sentinel이 남아 있어도 거부한다.
    """
    validate_ibk_run_id(expected_run_id)
    if type(stdout) is not bytes:
        raise IbkProtocolError("INVALID_STDOUT_TYPE")
    if truncated or len(stdout) > MAX_STDOUT_BYTES:
        raise IbkProtocolError("OUTPUT_LIMIT")
    # Wire delimiter is LF (optional preceding CR). Bare CR in progress/log text
    # is not a boundary and must not promote embedded text to a result frame.
    lines = stdout.split(b"\n")
    frames = [(index, line) for index, line in enumerate(lines) if line.startswith(SENTINEL)]
    if len(frames) != 1:
        raise IbkProtocolError("RESULT_COUNT")
    index, frame = frames[0]
    if index == len(lines) - 1:
        raise IbkProtocolError("INCOMPLETE_FRAME")
    data = frame[len(SENTINEL):].removesuffix(b"\r")
    if len(data) > MAX_RESULT_BYTES:
        raise IbkProtocolError("RESULT_LIMIT")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, IbkProtocolError):
            raise
        raise IbkProtocolError("INVALID_JSON") from None
    return _validate(payload, expected_run_id)


def encode_ibk_result(result: IbkResult) -> bytes:
    """runner용 1줄을 반환하되 직접 출력하지 않는다. 발신 측도 같은 검사 적용.

    emitter는 전용 출력 경로·행 경계·전송 완료를 보장해야 한다.
    이 bytes를 반환하는 것만으로 동시 출력의 원자성까지 보장하지 않는다.
    """
    try:
        frame = SENTINEL + json.dumps(asdict(result), allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError):
        raise IbkProtocolError("INVALID_RESULT_OBJECT") from None
    decode_ibk_result(frame, result.run_id)
    return frame


class IbkExecutionFailure(str, Enum):
    RESULT_PROTOCOL_ERROR = "RESULT_PROTOCOL_ERROR"
    PROCESS_ERROR = "PROCESS_ERROR"
    PROCESS_TIMEOUT = "PROCESS_TIMEOUT"


@dataclass(frozen=True)
class IbkParentDecision:
    result: IbkResult | None
    failure: IbkExecutionFailure | None
    protocol_error: str | None
    should_retry: bool

    def __post_init__(self) -> None:
        valid = type(self.should_retry) is bool
        if self.result is not None:
            valid = (valid and isinstance(self.result, IbkResult) and self.failure is None
                     and self.protocol_error is None and not self.should_retry)
        elif self.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR:
            valid = (valid and isinstance(self.protocol_error, str)
                     and re.fullmatch(r"[A-Z_]{1,64}", self.protocol_error) is not None
                     and not self.should_retry)
        else:
            valid = (valid and isinstance(self.failure, IbkExecutionFailure)
                     and self.protocol_error is None)
        if not valid:
            raise ValueError("INVALID_PARENT_DECISION")

    @property
    def needs_attention(self) -> bool:
        """경보 대상 여부일 뿐 발송 성공을 뜻하지 않는다."""
        return self.failure is not None or self.result.status in (IbkStatus.DEGRADED, IbkStatus.FAILED)


def decide_ibk_process_result(
    stdout: bytes, *, expected_run_id: str, returncode: int | None,
    timed_out: bool = False, is_retry: bool = False, stdout_truncated: bool = False,
) -> IbkParentDecision:
    """계측과 재등록을 분리한다. 실제 Queue/계수기/Telegram은 건드리지 않는다.

    timeout/비정상 종료가 있으면 stdout의 성공 결과보다 우선하며 기존 1회 재시도.
    exit 0의 프로토콜 오류와 모든 유효 semantic 결과는 재등록하지 않는다.
    """
    if ((returncode is not None and type(returncode) is not int)
            or any(type(flag) is not bool for flag in (timed_out, is_retry, stdout_truncated))):
        return IbkParentDecision(None, IbkExecutionFailure.RESULT_PROTOCOL_ERROR,
                                 "INVALID_PROCESS_METADATA", False)
    if timed_out or returncode != 0:
        failure = IbkExecutionFailure.PROCESS_TIMEOUT if timed_out else IbkExecutionFailure.PROCESS_ERROR
        return IbkParentDecision(None, failure, None, not is_retry)
    try:
        result = decode_ibk_result(stdout, expected_run_id, truncated=stdout_truncated)
    except IbkProtocolError as exc:
        return IbkParentDecision(None, IbkExecutionFailure.RESULT_PROTOCOL_ERROR, str(exc), False)
    return IbkParentDecision(result, None, None, False)
