"""부모가 정한 IBK 실행 ID/시각. 자식이 자기 날짜만 맞추는 것을 막는다."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.ibk_result_protocol import IbkProtocolError, IbkResult, validate_ibk_run_id

KST = ZoneInfo("Asia/Seoul")
CLOCK_TOLERANCE = timedelta(seconds=2)  # 같은 호스트의 부모/자식 관측시각 허용 오차

# 조회기준일 rollover 경계. **이 모듈이 단일 소유한다** — 부모가 계산하는
# expected_service_date 와 수집 측 판정이 갈라지면 validate_result 가 결과를
# SERVICE_DATE_MISMATCH 로 전량 거부한다. 08:00 인 이유는 app/crawlers/ibk.py 참조.
SERVICE_DATE_ROLLOVER_TIME = time(8, 0)


@dataclass(frozen=True)
class IbkRunContext:
    run_id: str
    reference_time: datetime

    def __post_init__(self):
        validate_ibk_run_id(self.run_id)
        if not isinstance(self.reference_time, datetime) or self.reference_time.utcoffset() is None:
            raise IbkProtocolError("INVALID_REFERENCE_TIME")

    @property
    def expected_service_date(self) -> str:
        local = self.reference_time.astimezone(KST)
        day = local.date() - timedelta(days=int(local.time() < SERVICE_DATE_ROLLOVER_TIME))
        return day.isoformat()

    @classmethod
    def from_arguments(cls, args: tuple[str, ...]):
        if (not isinstance(args, tuple) or len(args) != 5
                or any(not isinstance(arg, str) for arg in args)
                or args[0] != "ibk" or args[1] != "--run-id"
                or args[3] != "--reference-time"):
            raise IbkProtocolError("INVALID_RUN_ARGUMENTS")
        try:
            reference_time = datetime.fromisoformat(args[4])
        except (TypeError, ValueError):
            raise IbkProtocolError("INVALID_REFERENCE_TIME") from None
        return cls(args[2], reference_time)

    def arguments(self) -> tuple[str, ...]:
        return ("ibk", "--run-id", self.run_id, "--reference-time", self.reference_time.isoformat())

    def validate_result(self, result: IbkResult, *, received_at: datetime) -> None:
        # Call after codec validation. This verifies the parent's context, NOT DB/HTML.
        if received_at.utcoffset() is None or received_at < self.reference_time:
            raise IbkProtocolError("INVALID_PARENT_CLOCK")
        if result.run_id != self.run_id:
            raise IbkProtocolError("RUN_ID_MISMATCH")
        if result.expected_service_date != self.expected_service_date:
            raise IbkProtocolError("SERVICE_DATE_MISMATCH")
        observed_at = datetime.fromisoformat(result.observed_at)
        if not (self.reference_time - CLOCK_TOLERANCE <= observed_at <= received_at + CLOCK_TOLERANCE):
            raise IbkProtocolError("OBSERVATION_TIME_MISMATCH")
