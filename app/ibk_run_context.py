"""부모가 정한 IBK 실행 ID/시각/기한. 자식이 자기 날짜와 자기 예산을 정하는 것을 막는다."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import math
import time as _time
from zoneinfo import ZoneInfo

from app.ibk_result_protocol import IbkProtocolError, IbkResult, validate_ibk_run_id

KST = ZoneInfo("Asia/Seoul")
CLOCK_TOLERANCE = timedelta(seconds=2)  # 같은 호스트의 부모/자식 관측시각 허용 오차

# 부모가 기한을 주지 않았을 때의 기본 작업 예산(초). 부모 기본 실행 제한 45초를
# **작업 34 / 정리 6 / 출력·종료 5** 로 나눈 것 중 작업 몫이다.
# ⛔ 이 값은 fallback 이다. 운영 경로에서는 부모가 자기 timeout 에서 계산해 넘긴다 —
#    그래야 부모 timeout 을 바꿀 때 자식 예산도 함께 줄어든다.
DEFAULT_WORK_BUDGET_SECONDS = 34.0
# 계약 상한(초). 넘겨받은 기한이 이보다 큰 잔여를 낸다면 **거부한다**.
# ⛔ 숫자만 이 값으로 바꾸면(클램프) 방어가 아니라 위장이다 — 한 시간이 지나도 잔여가 계속
#    300초로 나와 소비자가 새 작업을 무한히 시작한다(실측). 그래서 자른 값을 돌려주지 않는다.
# ⛔ 이것은 **설정 오류 방어**이지 시계 점프 방어가 아니다 — 작업 기한이 monotonic 기준이라
#    시스템 시각 변경으로는 잔여가 늘지 않는다.
MAX_WORK_BUDGET_SECONDS = 300.0

# 조회기준일 rollover 경계. **이 모듈이 단일 소유한다** — 부모가 계산하는
# expected_service_date 와 수집 측 판정이 갈라지면 validate_result 가 결과를
# SERVICE_DATE_MISMATCH 로 전량 거부한다. 08:00 인 이유는 app/crawlers/ibk.py 참조.
SERVICE_DATE_ROLLOVER_TIME = time(8, 0)


@dataclass(frozen=True)
class IbkRunContext:
    """`work_deadline` 은 **작업 기한**이다 — 정리·출력 몫은 여기서 이미 빠져 있다.

    ⛔ **`time.monotonic()` 기준**이지 달력 시각이 아니다. 달력 시각으로 빼면 시스템 시각이
       뒤로 밀릴 때 잔여 예산이 **늘어난다**(실측: 60초 역행 시 24초 → 84초). 부모의
       asyncio timeout 도 단조 시계 기준이므로 같은 축이어야 한다. 같은 호스트의 부모·자식은
       같은 CLOCK_MONOTONIC 을 보므로 이 값이 프로세스를 건너 비교 가능하다(실측 확인).
       `reference_time` 은 서비스일 판정용으로 그대로 둔다 — 그건 달력 시각이어야 한다.

    ⛔ 기한을 지났다는 판정과 **멈춘 외부 호출이 실제로 끝난다**는 보장은 다르다. 이 값은
       "새 작업을 시작해도 되는가" 를 묻는 데 쓰고, 이미 멈춘 호출의 종료를 약속하지 않는다.
    """

    run_id: str
    reference_time: datetime
    work_deadline: float | None = None

    def __post_init__(self):
        validate_ibk_run_id(self.run_id)
        if not isinstance(self.reference_time, datetime) or self.reference_time.utcoffset() is None:
            raise IbkProtocolError("INVALID_REFERENCE_TIME")
        if self.work_deadline is None:
            # 확정해 둔다. 그래야 인자 왕복이 같은 값으로 돌아온다.
            object.__setattr__(self, "work_deadline",
                               _time.monotonic() + DEFAULT_WORK_BUDGET_SECONDS)
        if type(self.work_deadline) not in (int, float) or not math.isfinite(self.work_deadline):
            raise IbkProtocolError("INVALID_WORK_DEADLINE")
        object.__setattr__(self, "work_deadline", float(self.work_deadline))

    def remaining_work_seconds(self, monotonic_now: float) -> float:
        """작업 기한까지 남은 초. **음수를 그대로 돌려준다.**

        ⛔ 0 으로 잘라도 `remaining <= 0` 으로 만료 판정은 된다. 음수를 남기는 이유는
           **얼마나 초과했는지**까지 보이기 위해서다 — 그 값이 계측과 원인 분석에 쓰인다.

        ⛔ 상한을 넘는 잔여는 **거부한다**. 부모·자식의 monotonic 이 비교 가능하다는 전제가
           깨진 경우(다른 호스트·다른 커널)다. 자른 값을 돌려주면 소비자가 계속 새 작업을
           시작하므로 방어가 아니라 위장이 된다.
        """
        if type(monotonic_now) not in (int, float) or not math.isfinite(monotonic_now):
            raise IbkProtocolError("INVALID_CLOCK")
        remaining = self.work_deadline - monotonic_now
        if remaining > MAX_WORK_BUDGET_SECONDS:
            raise IbkProtocolError("IMPLAUSIBLE_WORK_DEADLINE")
        return remaining

    @property
    def expected_service_date(self) -> str:
        local = self.reference_time.astimezone(KST)
        day = local.date() - timedelta(days=int(local.time() < SERVICE_DATE_ROLLOVER_TIME))
        return day.isoformat()

    @classmethod
    def from_arguments(cls, args: tuple[str, ...]):
        if (not isinstance(args, tuple) or len(args) != 7
                or any(not isinstance(arg, str) for arg in args)
                or args[0] != "ibk" or args[1] != "--run-id"
                or args[3] != "--reference-time" or args[5] != "--work-deadline"):
            raise IbkProtocolError("INVALID_RUN_ARGUMENTS")
        try:
            reference_time = datetime.fromisoformat(args[4])
        except (TypeError, ValueError):
            raise IbkProtocolError("INVALID_REFERENCE_TIME") from None
        try:
            work_deadline = float(args[6])
        except (TypeError, ValueError):
            raise IbkProtocolError("INVALID_WORK_DEADLINE") from None
        return cls(args[2], reference_time, work_deadline)

    def arguments(self) -> tuple[str, ...]:
        return ("ibk", "--run-id", self.run_id,
                "--reference-time", self.reference_time.isoformat(),
                "--work-deadline", repr(self.work_deadline))

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
