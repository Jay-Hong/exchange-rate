"""Hana 은행 영업일 분류 — ADR-034 Phase 2d Hana observed_eod.

**단일 진실 소스**: `classify_hana_calendar_day()`. Hana observed_eod writer의
skip 분기 + calendar_class provenance + is_hana_business_day 모두 이 함수를 재사용한다
(calendar 로직 중복 방지).

분류 우선순위: **weekend > holiday > business_day**.
(주말이면서 공휴일인 경우 weekend로 분류 — skip 결과는 동일하므로 provenance 단순화.)
"""

# 표준 라이브러리
from datetime import date

# 로컬 애플리케이션
from app.calendars.kr_holidays import is_kr_holiday


def classify_hana_calendar_day(d: date) -> str:
    """Hana calendar 분류 → "weekend" | "holiday" | "business_day".

    - weekend: 토(5)/일(6) — 공휴일과 겹쳐도 weekend 우선
    - holiday: 평일 + 한국 공휴일 (PUBLIC ∪ BANK, 근로자의날 포함)
    - business_day: 그 외 (평일 + 비공휴일)
    """
    if d.weekday() >= 5:  # 5=토, 6=일
        return "weekend"
    if is_kr_holiday(d):
        return "holiday"
    return "business_day"


def is_hana_business_day(d: date) -> bool:
    """Hana 영업일 여부 (= 평일 AND 비공휴일)."""
    return classify_hana_calendar_day(d) == "business_day"
