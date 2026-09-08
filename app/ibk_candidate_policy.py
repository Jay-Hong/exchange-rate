"""IBK 공식 날짜 지정 조회의 **후보 날짜 계획**. HTTP·DB·판정은 담지 않는다.

legacy 경로(`try_crawl_with_dated_requests`)와 새 결과 생성기가 같은 날짜 순서를 쓰게
하려고 분리했다. 두 경로가 각자 계산하면 조용히 갈라지고, 그 차이는 "왜 그 날짜를
조회했나" 를 사후에 설명할 수 없게 만든다.

⛔ `app.crawlers` 를 import 하지 않는다 — `app/crawlers/__init__.py` 가 모든 크롤러를
   끌어와 순환이 생긴다. rollover 경계는 `app/ibk_run_context.py` 가 소유한다.
"""

import datetime
from dataclasses import dataclass
from enum import Enum

from app.ibk_run_context import KST, SERVICE_DATE_ROLLOVER_TIME


def plan_candidate_dates(reference_time: datetime.datetime, *, max_days_back: int):
    """조회할 서비스 기준일을 **최신 → 과거** 순으로 낸다.

    ⛔ `max_days_back` 은 **달력 범위**다 — 오늘로부터 거슬러 볼 일수(inclusive)이고
       주말도 그 범위를 소비한다. 후보 개수는 달력 일수(`max_days_back + 1`)를 넘지
       않으며, 범위에 주말이 섞이면 그보다 줄어든다. 2026-08-28 08:00 이후면 달력 11일에서
       평일 9개다. "평일 후보 N개" 로 재정의하면 탐색 범위가 조용히 넓어진다.

    ⛔ 여기서 현재 시각을 읽지 않는다. 부모가 고정한 `reference_time` 만 쓰므로, 실행
       도중 자정이나 08:00 을 지나도 후보 목록이 움직이지 않는다. 경과시간 예산은
       별도 축이며 호출자가 monotonic clock 으로 판정한다.

    주말에 새 조회기준일 세션은 없다. 금요일 세션의 토요일 새벽 고시는 금요일 날짜를
    조회하면 나오므로 이 skip 으로 손실되지 않는다.

    Args:
        reference_time: tz-aware 여야 한다. naive 는 어느 지역 시각인지 알 수 없다.
        max_days_back: 오늘로부터 거슬러 볼 **달력** 일수(inclusive).
    """
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("NAIVE_REFERENCE_TIME")
    if not isinstance(max_days_back, int) or isinstance(max_days_back, bool) or max_days_back < 0:
        raise ValueError("INVALID_MAX_DAYS_BACK")

    local = reference_time.astimezone(KST)
    reference_date = local.date()
    first_days_back = 0 if local.time() >= SERVICE_DATE_ROLLOVER_TIME else 1
    return tuple(
        candidate
        for candidate in (
            reference_date - datetime.timedelta(days=days_back)
            for days_back in range(first_days_back, max_days_back + 1)
        )
        if candidate.weekday() < 5
    )


class CandidateSearchStop(Enum):
    """후보 검색이 끝난 이유. **Selenium 허용 여부와는 별개 축**이다.

    ⛔ 이 값을 곧바로 "안전망을 돌려라" 로 읽으면 안 된다. 기술 오류와 의미적 거부가 둘 다
       "더 과거를 조회하지 않는다" 는 결론을 낼 수 있지만 후속 처리는 다를 수 있다.
       각 실행 경로가 fallback 여부를 스스로 정한다.
    """

    ACCEPTED = "ACCEPTED"                    # 유효 후보를 얻어 즉시 멈췄다
    HORIZON_EXHAUSTED = "HORIZON_EXHAUSTED"  # 허용 범위를 정상적으로 다 확인했다
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"    # 다음 요청을 시작할 예산이 없다
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"  # 진행 중 요청 자체가 실패했다


@dataclass(frozen=True)
class CandidateSearchResult:
    """검색이 확인한 **사실**만 담는다. 판정·저장·등급은 호출자 몫이다."""

    stop: CandidateSearchStop
    service_date: datetime.date | None = None   # ACCEPTED 일 때 실제로 관측한 날짜
    payload: object = None                      # ACCEPTED 일 때 fetch 반환값
    failure_reason: object = None               # TECHNICAL_FAILURE 일 때 원인
    saw_no_session: bool = False                # HTTP 무고시 응답을 실제로 받았다
    saw_preopen_pending: bool = False           # 개장 전 표 준비중을 실제로 받았다
    attempted: int = 0                          # 실제로 요청한 후보 수


def search_candidates(
    reference_time,
    *,
    max_days_back,
    fetch,
    has_budget,
    classify,
) -> CandidateSearchResult:
    """계획한 후보를 순서대로 조회해 **첫 유효 후보**에서 멈춘다.

    HTTP·DB·로깅을 담지 않는다. `fetch`/`has_budget`/`classify` 는 호출자가 주입한다 —
    그래야 이 정책을 네트워크 없이 시험할 수 있다.

    Args:
        fetch: `(service_date) -> payload | None`. `None` 은 그 날짜에 고시가 없다는 뜻.
            예외를 던지면 `classify` 가 그것을 해석한다.
        has_budget: `() -> bool`. **다음 요청을 시작해도 되는가.** 매 요청 직전에 묻는다.
            ⛔ 정상 종료 뒤에 소급해서 묻지 않는다 — 마지막 후보까지 다 확인하고 나서
               시간이 지났다고 예산 소진으로 판정하면 legacy 와 달라진다.
        classify: `(exc, query_date) -> IbkReason | None`. `None` 이면 "다음 후보로 계속"
            (의미적 거부),
            값이 있으면 기술적 실패로 검색을 끝낸다.

    ⛔ 주말 skip 은 HTTP 응답 없이 일어난다. 그 경로로 과거 후보에 닿아도
       `saw_no_session` 을 켜지 않는다 — 받지 않은 응답을 지어내는 것이기 때문이다.
       호출자는 관측일과 기대일을 대조해 역사 후보 여부를 판단한다.
    """
    planned = plan_candidate_dates(reference_time, max_days_back=max_days_back)
    if not planned:
        return CandidateSearchResult(CandidateSearchStop.HORIZON_EXHAUSTED)

    saw_no_session = saw_preopen = False
    attempted = 0

    for query_date in planned:
        if not has_budget():
            return CandidateSearchResult(
                CandidateSearchStop.BUDGET_EXHAUSTED,
                saw_no_session=saw_no_session, saw_preopen_pending=saw_preopen, attempted=attempted,
            )
        attempted += 1
        try:
            payload = fetch(query_date)
        except Exception as exc:  # noqa: BLE001 — 해석은 주입된 classify 가 한다
            reason = classify(exc, query_date)
            if reason is not None:
                return CandidateSearchResult(
                    CandidateSearchStop.TECHNICAL_FAILURE, failure_reason=reason,
                    saw_no_session=saw_no_session, saw_preopen_pending=saw_preopen, attempted=attempted,
                )
            saw_preopen = True
            continue

        if payload is None:
            saw_no_session = True
            continue

        return CandidateSearchResult(
            CandidateSearchStop.ACCEPTED, service_date=query_date, payload=payload,
            saw_no_session=saw_no_session, saw_preopen_pending=saw_preopen, attempted=attempted,
        )

    return CandidateSearchResult(
        CandidateSearchStop.HORIZON_EXHAUSTED,
        saw_no_session=saw_no_session, saw_preopen_pending=saw_preopen, attempted=attempted,
    )
