# app/market_mode.py

"""
시장 모드 / DXY 정책 상태 판별 유틸리티

scheduler.py와 dxy_spot.py 등에서 공용으로 사용.
순환 참조 방지를 위해 별도 모듈로 분리.

⚠️ **두 시계는 의도적으로 분리되어 있다** (2026-08-28):
  - `get_market_mode()`  : 은행 크롤러 등록/해제용. BREAK1 시작이 21:00 → 19:00으로 당겨졌다
                           (SC change-only DB에서 30일 22영업일 동안 18:00 이후 포착 변경 0,
                            최종 포착 17:49. 공식 고시 종료의 증명은 아님).
  - `get_dxy_policy_state()`: DXY 외부 fallback 정책용. **구 모드 경계(21:00)를 그대로 보존**한다.
                           은행 경계를 19:00으로 당기면서 DXY까지 따라가면 19:00~21:00 구간의
                           장애 복구가 20초(실패 임계 3→5회) 또는 최대 15분(grace 15분→30분)
                           늦어지는데, 그 변경을 뒷받침할 DXY 고유 근거가 없다.

⛔ `get_dxy_policy_state()`를 `get_market_mode()`로 되돌리지 말 것. 두 시계가 다시 묶이면
   위 회귀가 조용히 재발한다. 등가성은 tests/test_market_mode_policy.py가 **구 모드 로직의
   동결 사본**과 일주일 전 구간에서 대조해 잠근다.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# DXY 정책 상태 (구 market mode에서 분리된 값)
DXY_ACTIVE = "ACTIVE"                      # 구 IN
DXY_QUIET = "QUIET"                        # 구 BREAK1 / BREAK2
DXY_WEEKEND_PRESERVE = "WEEKEND_PRESERVE"  # 구 OUT


def _is_weekend_window(weekday: int, hour: int) -> bool:
    """주말 구간 판정 (토 07:00 ~ 월 06:00 KST).

    은행 모드의 OUT과 DXY의 WEEKEND_PRESERVE가 공유하는 단일 정의.
    """
    return (weekday == 5 and hour >= 7) or (weekday == 6) or (weekday == 0 and hour < 6)


def get_market_mode(now: datetime) -> str:
    """
    은행 크롤러 등록용 시장 모드 (4단계)

    Args:
        now: KST 기준 datetime

    Returns:
        "OUT": 주말 (토 07:00 ~ 월 06:00)
        "BREAK1": 야간 (월~금 19:00 ~ 익일 06:00)
        "BREAK2": 고시 마무리 (06:00~07:59)
        "IN": 영업시간 (월~금 08:00~18:59)

    2026-08-28 변경:
        - BREAK1 시작 21:00 → 19:00 (SC 18시 이후 포착 변경 0건 실측 반영)
        - BREAK1 종료 03:00 → 06:00 (우리 05:00 / IBK 06:00 연장 고시 — 사용자 은행 페이지 실측)
        - BREAK2 03:00~07:59 → 06:00~07:59
    """
    weekday = now.weekday()  # 월=0, 화=1 ... 일=6
    hour = now.hour

    # OUT: 토요일 07:00 이후, 일요일 전체, 월요일 06:00 전
    if _is_weekend_window(weekday, hour):
        return "OUT"

    # BREAK1: 월~금 19:00 ~ 익일 06:00
    if 0 <= weekday <= 4 and 19 <= hour:
        return "BREAK1"
    if 1 <= weekday <= 5 and hour < 6:
        return "BREAK1"

    # BREAK2: 06:00~07:59
    if 6 <= hour < 8:
        return "BREAK2"

    # IN: 나머지 (08:00~18:59)
    return "IN"


def get_dxy_policy_state(now: datetime) -> str:
    """
    DXY 외부 fallback 정책 상태.

    구 `get_market_mode()`의 IN/BREAK/OUT 경계를 **그대로 보존**한다
    (은행 모드가 19:00으로 당겨진 것과 무관하게 DXY는 21:00 경계 유지).

    Args:
        now: KST 기준 datetime

    Returns:
        DXY_ACTIVE            (평일 08:00~20:59): grace 15분 / 연속 실패 3회 / diff guard 15분
        DXY_QUIET             (그 외 평일 야간·새벽): grace 30분 / 연속 실패 5회 / diff guard 30분
        DXY_WEEKEND_PRESERVE  (토 07:00~월 06:00): silent-stale 외부 fallback 미진입 +
                              direct-latest heartbeat 호출 경로 유지(flag off면 no-op)
    """
    weekday = now.weekday()
    hour = now.hour

    if _is_weekend_window(weekday, hour):
        return DXY_WEEKEND_PRESERVE

    if 0 <= weekday <= 4 and 8 <= hour < 21:
        return DXY_ACTIVE

    return DXY_QUIET
