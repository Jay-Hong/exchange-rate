# app/market_mode.py

"""
시장 모드 판별 유틸리티

scheduler.py와 dxy_spot.py 등에서 공용으로 사용.
순환 참조 방지를 위해 별도 모듈로 분리.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def get_market_mode(now: datetime) -> str:
    """
    현재 시간대의 시장 모드 반환 (4단계 세분화)

    Args:
        now: KST 기준 datetime

    Returns:
        "OUT": 주말 (토 07:00 ~ 월 06:00)
        "BREAK1": 심야 (월~금 21:00 ~ 익일 03:00)
        "BREAK2": 고시 마무리 (월 06:00~07:59, 화~금 03:00~07:59, 토 03:00~06:59)
        "IN": 영업시간 (월~금 08:00~20:59)
    """
    weekday = now.weekday()  # 월=0, 화=1 ... 일=6
    hour = now.hour

    # OUT: 토요일 07:00 이후, 일요일 전체, 월요일 06:00 전
    if (weekday == 5 and hour >= 7) or (weekday == 6) or (weekday == 0 and hour < 6):
        return "OUT"

    # BREAK1: 월~금 21:00 ~ 익일 03:00
    if 0 <= weekday <= 4 and 21 <= hour:
        return "BREAK1"
    elif 1 <= weekday <= 5 and hour < 3:
        return "BREAK1"

    # BREAK2: 03:00~07:59
    if 3 <= hour < 8:
        return "BREAK2"

    # IN: 나머지 (08:00~20:59)
    return "IN"
