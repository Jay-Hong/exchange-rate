"""KRX 파생상품시장 캘린더 — 정규장/야간장 판정 + 미국달러선물 만기일 (단일 진실 소스).

2026-08-16 신설. 이전에는 "만기" 개념이 **두 곳에 흩어져 있었고 둘 다 틀렸다**:

  1. `kis_master._compute_expiry_date` — 표준 셋째 월요일만 계산 (휴장 보정 없음).
     docstring이 한계를 명시해 뒀으나 보정은 끝내 들어오지 않았다.
  2. `kis_futures.KRX_2026_USDF_EXPIRY_DAYS` — 하드코딩 `{2026-05-18}` 1건 + TODO 방치.
     5월을 뺀 모든 월에서 만기일 판정이 False였다.

**2026-08-14 운영 사고로 실증**: 2026-08 셋째 월요일 8/17이 광복절(8/15 토)
대체공휴일이라 실제 최종거래일이 **8/14(금)로 앞당겨졌는데** (1)이 8/17을 유지 →
만기 후 월물을 계속 구독 → `SUBSCRIBE SUCCESS` 인데 프레임 0 → 150초 침묵 →
재접속 루프(10시간+) + 8/14 일봉 누락. KIS REST도 선물 필드 없이 지수형 응답을
반환했다.
피해 범위(정확히): **일봉 close write는 차단**됐다 — close write gate가 contract
identity에서 REJECT. 그러나 **raw tick 466건과 hourly 4버킷에는 잘못된 월물
(A75608)이 기록**됐다(정책상 07:00부터 차월물이어야 함). 두 store 모두 retention
(각 30일·14일)으로 소멸하고 A75609 intraday를 되살릴 소스가 없어 정정 불가다.
"오염 write 0"은 일봉 축에 한정한 서술이다.

**정규장/야간장을 나누는 이유** — 두 축은 판정 기준일부터 다르다:

  - 정규장: **당일**이 영업일인가.
  - 야간장: **세션 시작일**이 기준. 금요일 18:00 시작 세션은 토요일 06:00까지
    이어지므로, 토요일 새벽은 "토요일이 휴일"인 것과 무관하게 열려 있다.
    또 정규장이 열린 날에도 **야간장만 따로 휴장**하는 공식 공지가 존재한다
    (연말: 12/30 시작 야간장 정상 / 12/31 시작 야간장 휴장).

단일 predicate로는 이 비대칭을 표현할 수 없다. 만기 walk-back은 **정규장 캘린더만**
사용한다 — 최종거래일은 정규장 개념이고, 야간 override가 만기 계산으로 새면
2026-02-13처럼 정규장이 정상 개장한 날을 휴장으로 오판해 만기를 2/12로 잘못
당긴다(테스트로 잠금).

의존: `app.calendars.kr_holidays`(leaf, holidays 라이브러리)만. `app.sources`·
`app.crawlers`를 import하지 않는다 — 역방향 의존(sources → calendars)이 정상이고,
순환이 생기면 이 모듈이 먼저 깨져야 한다.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import FrozenSet

from app.calendars.kr_holidays import is_kr_holiday

__all__ = [
    "is_krx_regular_business_day",
    "is_krx_night_session_open",
    "third_monday",
    "usdf_expiry_date",
    "EXPIRY_WALK_BACK_LIMIT_DAYS",
]


# ---------------------------------------------------------------------------
# 야간장 전용 override — **공시 원문 확인을 거친 날짜만** 등재한다
# ---------------------------------------------------------------------------
#
# ⛔ 규칙 (5/25 사고의 역방향 재연 방지):
#    - KRX 공식 공지(acptno 등 출처)를 **직접 확인한 날짜만** 넣는다.
#    - "매년 X월 Y일", "설 연휴 전 금요일" 같은 **반복 규칙 금지**. 설은 음력이라
#      양력 날짜가 매년 이동하고, 야간장 특별휴장은 KRX가 매번 개별 공지로 정한다.
#      규칙화하면 추측 기반 캘린더가 되어 5/25(대체공휴일 누락으로 stale 종가를
#      정상 응답으로 받아 기록)와 같은 계열의 사고가 된다.
#    - **미등재** 시 열화 범위 (codex 리뷰로 정정 — "소음뿐"은 과소 서술이었다):
#        · WS 무프레임 재접속 소음
#        · REST fallback **호출 발생** (stale 판정 → probe)
#        · general REST guard가 `PASS`로 기록될 수 있음 — 지금은 telemetry-only라
#          데이터 오염이 0이지만, **Stage C 실 반영 전에는 반드시 닫아야 한다**
#          (session evidence 요구 또는 등가 안전장치. ADR-027 Stage C 선행조건).
#      close-write 경로의 일봉 보호는 gate 3(session evidence — 우리 WS 마지막
#      tick의 최근성)이 담당하므로 그쪽은 미등재여도 안전하다.
#
_KRX_NIGHT_SESSION_FORCE_CLOSED: FrozenSet[date] = frozenset({
    # 2026-02-13(금) 시작 야간장 휴장.
    #   출처: KRX 파생상품시장본부 "야간파생상품시장 공지" (2026.02.06),
    #         KIND acptno=20260206002546 — 원문 직접 확인 (2026-08-16).
    #   휴장 거래: '26.2.13(금) 18시 ~ '26.2.14(토) 06시 실시 예정인 파생상품 야간거래
    #   휴장 상품: 야간거래 대상 全상품 — **미국달러선물 명시**
    #             (코스피200선물·미니코스피200선물·코스닥150선물·코스피200옵션·
    #              미니코스피200옵션·코스닥150옵션·코스피200위클리옵션(월,목)·
    #              코스닥150위클리옵션(월,목)·미국달러선물·3년국채선물·10년국채선물)
    #   휴장 사유: 장기연휴(설 2/16~18)로 인한 시장참여자 리스크관리 부담 완화
    #   근거규정: 파생상품시장 업무규정 제5조 제1항
    #
    #   ⛔ **연례 규칙이 아니다.** 설은 음력이라 양력 날짜가 매년 이동하고, 야간장
    #      특별휴장은 KRX가 매번 개별 공지로 정한다. "매년 2/13" 또는 "설 연휴 전
    #      금요일" 같은 파생 규칙으로 일반화하면 추측 기반 캘린더가 된다.
    #      2026-02-13 **단건**이며, 이 날 정규장은 정상 개장했다(만기 계산과 무관).
    date(2026, 2, 13),
})

# 반대 방향 override — 휴일 판정이지만 야간장은 열린 경우. 현재 알려진 사례 없음.
_KRX_NIGHT_SESSION_FORCE_OPEN: FrozenSet[date] = frozenset()


# 만기일 walk-back 상한(일). 실측(holidays==0.97, 2020~2030 132개월)상 보정은
# 10건이고 **전부 정확히 3일**(월요일 휴장 → 주말 → 직전 금요일)이라 4배 이상
# 여유다. 상한을 두는 이유는 정상 동작 제한이 아니라 **캘린더가 이상해졌을 때
# 조용히 엉뚱한 날짜를 내놓지 않게** 하기 위함이다(Crash Early).
EXPIRY_WALK_BACK_LIMIT_DAYS = 14


def _krx_year_end_closure_day(year: int) -> date:
    """KRX 연말 폐장일(휴장) — 12/31 기준, 휴일이면 직전 매매거래일로 당김.

    KRX 규칙: 12월 31일 휴장, 단 12/31이 주말/공휴일이면 직전 매매거래일을 휴장.
    `is_kr_holiday`(공휴일)에 없는 KRX 고유 규칙이라 별도 처리.
    예) 2022→12/30(Fri) · 2023→12/29(Fri) · 2024·2025·2026→12/31.
    """
    d = date(year, 12, 31)
    while d.weekday() >= 5 or is_kr_holiday(d):
        d -= timedelta(days=1)
    return d


def is_krx_regular_business_day(d: date) -> bool:
    """KRX **정규장** 영업일 여부.

    False 조건: 주말 / 한국 공휴일(`is_kr_holiday` — PUBLIC∪BANK + observed,
    대체공휴일·근로자의날·제헌절 재지정 포함) / KRX 연말 폐장일.
    holidays 라이브러리 기반이라 **연도 무관 동적 계산**.
    잔여 한계: 라이브러리 미반영 임시공휴일 (운영 발견 시 대응).

    ⚠️ 야간장 판정에 이 함수를 그대로 쓰면 안 된다 — `is_krx_night_session_open`
    참조(시작일 기준 + 야간 전용 override).
    """
    if d.weekday() >= 5:  # 5=토, 6=일
        return False
    if is_kr_holiday(d):
        return False
    if d == _krx_year_end_closure_day(d.year):
        return False
    return True


def is_krx_night_session_open(start_date: date) -> bool:
    """KRX **야간장** 개장 여부 — 인자는 세션 **시작일**(달력 당일 아님).

    금요일 18:00 시작 세션은 토요일 06:00까지 이어지므로, 토요일 새벽을 판정할
    때는 `start_date = 금요일`을 넘겨야 한다. 호출자가 `today - 1일` 변환을
    책임진다(이 함수는 시작일만 본다).

    판정 순서:
      1. 두 테이블 교집합 → **RuntimeError** (아래)
      2. force_open 등재 → True (정규장 휴일이어도 야간장은 열림)
      3. force_closed 등재 → False (정규장은 열렸으나 야간장만 휴장)
      4. 그 외 → 정규장 영업일 여부를 따름

    Raises:
        RuntimeError: 같은 날짜가 두 override 테이블에 모두 등재된 경우.
            둘 다 **공식 공지 기반 데이터**이므로 교집합은 우선순위로 해소할
            사안이 아니라 **데이터 오류**다. 조용히 한쪽을 택하면(구 구현은
            force_open 우선) 잘못된 등재가 개장으로 뭉개져 드러나지 않는다.
            이 리포의 fail-close 정책에 맞춰 실패시킨다 (codex 리뷰 지적).
    """
    if start_date in _KRX_NIGHT_SESSION_FORCE_OPEN:
        if start_date in _KRX_NIGHT_SESSION_FORCE_CLOSED:
            raise RuntimeError(
                f"야간 override 충돌: {start_date}가 force_open·force_closed 양쪽에 "
                "등재됨 — 공시 출처를 확인해 한쪽을 제거할 것"
            )
        return True
    if start_date in _KRX_NIGHT_SESSION_FORCE_CLOSED:
        return False
    return is_krx_regular_business_day(start_date)


def third_monday(year: int, month: int) -> date:
    """해당 연월의 셋째 월요일 (휴장 보정 **없는** 표준 계산).

    KRX 미국달러선물 최종거래일의 기준일이다. 실제 만기일은 이 날이 휴장이면
    직전 영업일로 앞당겨지므로, 운영 코드는 `usdf_expiry_date`를 쓸 것.
    """
    first = date(year, month, 1)
    # 0=Mon … 6=Sun. 1일이 월요일이면 days_to_first_monday=0
    days_to_first_monday = (0 - first.weekday()) % 7
    return first + timedelta(days=days_to_first_monday + 14)


def usdf_expiry_date(year: int, month: int) -> date:
    """KRX 미국달러선물 최종거래일 — 셋째 월요일, 휴장이면 **직전 영업일로 앞당김**.

    KRX 규정: 최종거래일이 휴장일이면 순차적으로 앞당긴다. 연속 휴장(설·추석
    연휴 + 주말)이 있으므로 단일 스텝이 아니라 **루프**여야 한다.
    예) 2026-02: 셋째 월요일 2/16(설 연휴) → 2/15(일) → 2/14(토) → **2/13(금)**.

    walk-back은 `is_krx_regular_business_day`만 사용한다 — 야간 override가 여기로
    새면 정규장이 정상 개장한 날을 휴장으로 오판해 만기를 하루 더 당긴다.

    Raises:
        RuntimeError: `EXPIRY_WALK_BACK_LIMIT_DAYS`를 넘도록 영업일을 못 찾은 경우.
            정상 운영에서는 최대 3일이라 도달 불가 — 도달했다면 캘린더 데이터가
            깨진 것이므로 **조용히 틀린 날짜를 반환하지 않고 실패**한다.
    """
    d = third_monday(year, month)
    for _ in range(EXPIRY_WALK_BACK_LIMIT_DAYS + 1):
        if is_krx_regular_business_day(d):
            return d
        d -= timedelta(days=1)
    raise RuntimeError(
        f"USD futures 만기일 walk-back 한도 초과: {year}-{month:02d} "
        f"셋째 월요일 {third_monday(year, month)}에서 "
        f"{EXPIRY_WALK_BACK_LIMIT_DAYS}일 이상 영업일 없음 — 캘린더 데이터 확인 필요"
    )
