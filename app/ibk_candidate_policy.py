"""IBK 공식 날짜 지정 조회의 **후보 날짜 계획**. HTTP·DB·판정은 담지 않는다.

legacy 경로(`try_crawl_with_dated_requests`)와 새 결과 생성기가 같은 날짜 순서를 쓰게
하려고 분리했다. 두 경로가 각자 계산하면 조용히 갈라지고, 그 차이는 "왜 그 날짜를
조회했나" 를 사후에 설명할 수 없게 만든다.

⛔ `app.crawlers` 를 import 하지 않는다 — `app/crawlers/__init__.py` 가 모든 크롤러를
   끌어와 순환이 생긴다. rollover 경계는 `app/ibk_run_context.py` 가 소유한다.
"""

import datetime

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
