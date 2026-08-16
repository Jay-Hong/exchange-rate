"""KIS Open API 미국달러선물 (USD/KRW futures) — 순수 protocol adapter.

PR6 진입 전 사전 검증된 KIS Open API 기반 KRX 미국달러선물 수집을 위한
parser + session calendar helper. **운영 연결 없음** (no DB write,
no Redis write, no scheduler) — 런타임 상태와 분리해 테스트 가능.

영역:
  - WebSocket payload parser (주간/야간 TR 분리):
    * 주간 (CF 정규세션): H0CFCNT0 체결 50필드 / H0CFASP0 호가 38필드
    * 야간 (CM 야간세션): H0MFCNT0 체결 49필드 / H0MFASP0 호가 38필드
    * 호가는 주간/야간 columns 동일, 체결은 야간에 dscs_bltr_acml_qty 미포함
  - REST inquire-price 응답 형태는 별도 (output1.futs_prpr 단일 값)
  - 시장 세션 판정 (CF 주간 / CM 야간) — KRX "시작일 기준" 정책
  - 캘린더 판정은 `app.calendars.krx_calendar`에 위임 (정규장/야간장 분리).
    구 "검증된 최소 데이터"(하드코딩 만기 1건)는 2026-08-16에 삭제됐다.

검증 완료 (2026-05-04 KST smoke):
  - 주간 09:38: H0CFCNT0/H0CFASP0 + A75605 → 588 msg/15s tick
  - 야간 18:03: H0MFCNT0/H0MFASP0 + A75605 → 63 msg/15s tick (거래량 9× 낮음, 정상)
  - 가격 broker 앱 cross-check 일치 (주간 1483.30 / 야간 1474.50 어제, 오늘 야간 1468.50)

캘린더 보강 완료 (2026-08-16):
  - 만기일 정규세션 11:30 종료 — `contract_expiry_date` **필수 인자**로 판정
    (구 `is_expiry_day` 하드코딩 축 삭제). 2026-08-14 사고 대응.
  - 한국 정규 공휴일 — `holidays` 라이브러리(PUBLIC∪BANK, observed=True) 기반
    동적 계산. 야간장 전용 휴장은 별도 override 테이블(공시 확인 후 등재).
  - 임시휴장 추적 — KRX 공시 기반

KIS field mapping 출처:
  주간: examples_llm/domestic_futureoption/commodity_futures_realtime_conclusion/
        examples_llm/domestic_futureoption/commodity_futures_realtime_quote/
  야간: examples_llm/domestic_futureoption/krx_ngt_futures_ccnl/
        examples_llm/domestic_futureoption/krx_ngt_futures_asking_price/
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List, Literal, Optional
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Field columns — KIS 공식 examples 기반 (50 + 38)
# ---------------------------------------------------------------------------

H0CFCNT0_COLUMNS: List[str] = [
    "futs_shrn_iscd",
    "bsop_hour",
    "futs_prdy_vrss",
    "prdy_vrss_sign",
    "futs_prdy_ctrt",
    "futs_prpr",
    "futs_oprc",
    "futs_hgpr",
    "futs_lwpr",
    "last_cnqn",
    "acml_vol",
    "acml_tr_pbmn",
    "hts_thpr",
    "mrkt_basis",
    "dprt",
    "nmsc_fctn_stpl_prc",
    "fmsc_fctn_stpl_prc",
    "spead_prc",
    "hts_otst_stpl_qty",
    "otst_stpl_qty_icdc",
    "oprc_hour",
    "oprc_vrss_prpr_sign",
    "oprc_vrss_nmix_prpr",
    "hgpr_hour",
    "hgpr_vrss_prpr_sign",
    "hgpr_vrss_nmix_prpr",
    "lwpr_hour",
    "lwpr_vrss_prpr_sign",
    "lwpr_vrss_nmix_prpr",
    "shnu_rate",
    "cttr",
    "esdg",
    "otst_stpl_rgbf_qty_icdc",
    "thpr_basis",
    "futs_askp1",
    "futs_bidp1",
    "askp_rsqn1",
    "bidp_rsqn1",
    "seln_cntg_csnu",
    "shnu_cntg_csnu",
    "ntby_cntg_csnu",
    "seln_cntg_smtn",
    "shnu_cntg_smtn",
    "total_askp_rsqn",
    "total_bidp_rsqn",
    "prdy_vol_vrss_acml_vol_rate",
    "dscs_bltr_acml_qty",
    "dynm_mxpr",
    "dynm_llam",
    "dynm_prc_limt_yn",
]

H0CFASP0_COLUMNS: List[str] = [
    "futs_shrn_iscd",
    "bsop_hour",
    "futs_askp1",
    "futs_askp2",
    "futs_askp3",
    "futs_askp4",
    "futs_askp5",
    "futs_bidp1",
    "futs_bidp2",
    "futs_bidp3",
    "futs_bidp4",
    "futs_bidp5",
    "askp_csnu1",
    "askp_csnu2",
    "askp_csnu3",
    "askp_csnu4",
    "askp_csnu5",
    "bidp_csnu1",
    "bidp_csnu2",
    "bidp_csnu3",
    "bidp_csnu4",
    "bidp_csnu5",
    "askp_rsqn1",
    "askp_rsqn2",
    "askp_rsqn3",
    "askp_rsqn4",
    "askp_rsqn5",
    "bidp_rsqn1",
    "bidp_rsqn2",
    "bidp_rsqn3",
    "bidp_rsqn4",
    "bidp_rsqn5",
    "total_askp_csnu",
    "total_bidp_csnu",
    "total_askp_rsqn",
    "total_bidp_rsqn",
    "total_askp_rsqn_icdc",
    "total_bidp_rsqn_icdc",
]


# 야간 (KRX night session) TR — 2026-05-04 18:00 KST smoke 검증.
# columns 출처: examples_llm/domestic_futureoption/krx_ngt_futures_ccnl/
#                examples_llm/domestic_futureoption/krx_ngt_futures_asking_price/
#
# 호가 H0MFASP0 columns은 주간 H0CFASP0와 완전 동일.
# 체결 H0MFCNT0 columns은 주간 H0CFCNT0 (50필드)에서 47번째 필드
# `dscs_bltr_acml_qty` (협의대량 누적수량)가 빠진 49필드 — 야간엔 협의대량 거래 없음.
H0MFCNT0_COLUMNS: List[str] = [
    "futs_shrn_iscd",
    "bsop_hour",
    "futs_prdy_vrss",
    "prdy_vrss_sign",
    "futs_prdy_ctrt",
    "futs_prpr",
    "futs_oprc",
    "futs_hgpr",
    "futs_lwpr",
    "last_cnqn",
    "acml_vol",
    "acml_tr_pbmn",
    "hts_thpr",
    "mrkt_basis",
    "dprt",
    "nmsc_fctn_stpl_prc",
    "fmsc_fctn_stpl_prc",
    "spead_prc",
    "hts_otst_stpl_qty",
    "otst_stpl_qty_icdc",
    "oprc_hour",
    "oprc_vrss_prpr_sign",
    "oprc_vrss_nmix_prpr",
    "hgpr_hour",
    "hgpr_vrss_prpr_sign",
    "hgpr_vrss_nmix_prpr",
    "lwpr_hour",
    "lwpr_vrss_prpr_sign",
    "lwpr_vrss_nmix_prpr",
    "shnu_rate",
    "cttr",
    "esdg",
    "otst_stpl_rgbf_qty_icdc",
    "thpr_basis",
    "futs_askp1",
    "futs_bidp1",
    "askp_rsqn1",
    "bidp_rsqn1",
    "seln_cntg_csnu",
    "shnu_cntg_csnu",
    "ntby_cntg_csnu",
    "seln_cntg_smtn",
    "shnu_cntg_smtn",
    "total_askp_rsqn",
    "total_bidp_rsqn",
    "prdy_vol_vrss_acml_vol_rate",
    # NOTE: dscs_bltr_acml_qty (주간 47번째 필드) 야간엔 없음 — 협의대량 비거래
    "dynm_mxpr",
    "dynm_llam",
    "dynm_prc_limt_yn",
]

# 호가 columns은 주간/야간 동일 — 별도 alias로 명확성.
H0MFASP0_COLUMNS: List[str] = list(H0CFASP0_COLUMNS)


# ---------------------------------------------------------------------------
# Parser — payload data 영역만 받음 (caller가 frame prefix 분리 책임)
# ---------------------------------------------------------------------------

def _parse_payload(data: str, columns: List[str]) -> Optional[Dict[str, str]]:
    """Common ^-separated KIS WebSocket payload parser.

    Returns None if field count is shorter than expected columns (caller
    treats as malformed frame). Trailing extra fields are tolerated for
    forward-compat — KIS may add fields to existing TRs without bumping
    version, and existing column mappings should still work. To enforce
    strict equality (e.g. for spec validation), check `len(data.split("^"))`
    in caller before calling.

    All values returned as str — caller decides numeric conversion per field.
    """
    if not data:
        return None
    parts = data.split("^")
    if len(parts) < len(columns):
        return None
    return {col: parts[i] for i, col in enumerate(columns)}


def parse_h0cfcnt0_payload(data: str) -> Optional[Dict[str, str]]:
    """체결 H0CFCNT0 raw payload → field dict (50개)."""
    return _parse_payload(data, H0CFCNT0_COLUMNS)


def parse_h0cfasp0_payload(data: str) -> Optional[Dict[str, str]]:
    """호가 H0CFASP0 raw payload → field dict (38개)."""
    return _parse_payload(data, H0CFASP0_COLUMNS)


def parse_h0mfcnt0_payload(data: str) -> Optional[Dict[str, str]]:
    """야간 체결 H0MFCNT0 raw payload → field dict (49개).

    주간 H0CFCNT0 (50개)에서 dscs_bltr_acml_qty 빠진 형태.
    """
    return _parse_payload(data, H0MFCNT0_COLUMNS)


def parse_h0mfasp0_payload(data: str) -> Optional[Dict[str, str]]:
    """야간 호가 H0MFASP0 raw payload → field dict (38개).

    주간 H0CFASP0와 columns 동일.
    """
    return _parse_payload(data, H0MFASP0_COLUMNS)


# ---------------------------------------------------------------------------
# Calendar — app.calendars.krx_calendar 단일 진실 소스에 위임 (2026-08-16)
# ---------------------------------------------------------------------------
#
# 구 `KRX_2026_USDF_EXPIRY_DAYS` + `is_expiry_day` **삭제**:
#   하드코딩 `{2026-05-18}` 1건 + TODO 방치라 5월을 뺀 모든 월에서 만기 판정이
#   False였다. 그런데 이 축을 "정확하게" 고치면 오히려 회귀가 난다 — 유일한
#   소비자가 `get_active_session`의 `contract_expiry_date is None` 분기였고,
#   만기일 07:00에 차월물로 swap한 뒤에는 그 fallback이 참조해야 할 계약이
#   **항상 차월물**이라 11:30 종료를 적용하면 정상 거래 구간을 끊는다
#   (아래 Issue 1 주석 참조). 그래서 축을 정확하게 만드는 대신 **축을 없애고**
#   `contract_expiry_date`를 필수 인자로 승격해 모든 호출자가 실제 만기를
#   명시하게 했다. 삭제만 하고 None 허용을 남기면 fail-open이 된다.
#
# `is_krx_business_day`는 이름을 유지한 채 re-export — 기존 호출부와
# `patch("app.sources.kis_futures.is_krx_business_day")` 테스트가 그대로 동작한다.
# 야간장은 별도 predicate(`is_krx_night_session_open`, 시작일 기준)를 쓴다.
from app.calendars.krx_calendar import (  # noqa: E402
    is_krx_night_session_open,
    is_krx_regular_business_day as is_krx_business_day,
)


# ---------------------------------------------------------------------------
# Session 판정 — KRX "시작일 기준" 정책
# ---------------------------------------------------------------------------

# 정규시간 (단일가 포함):
#   08:30 정규 개장 단일가 시작
#   08:45 정규거래 시작
#   15:35 정규 종료 단일가 시작
#   15:45 정규거래 종료
# 야간시간:
#   17:50 야간 개장 단일가 시작
#   18:00 야간거래 시작
#   05:50 야간 종료 단일가 시작
#   06:00 야간거래 종료
# 시뮬레이션 단순화: 단일가 시간을 정규시간에 통합 (CF: 08:30-15:45, CM: 17:50-06:00).
# Codex 권고 — 미세한 단일가 구분은 운영 신호 보고 결정.

_REGULAR_START = time(8, 30)
_REGULAR_END = time(15, 45)
_REGULAR_EXPIRY_END = time(11, 30)  # 만기일 정규세션 종료
_NIGHT_START = time(17, 50)
_NIGHT_END = time(6, 0)


def night_session_start_date(now: datetime) -> date:
    """`now`가 속할 수 있는 야간 세션의 **시작일**.

    야간장은 달력 당일이 아니라 시작일 기준으로 열린다:
      - 00:00~06:00 → 시작일은 **전일** (금요일밤 세션이 토요일 06:00까지)
      - 그 외       → 시작일은 당일

    `get_active_session`의 CM 분기와 REST guard gate 1이 같은 규칙을 쓰도록
    추출한 helper다. 두 곳이 각자 `today - 1` 산술을 복제하면 한쪽만 고쳐지는
    사고가 난다(구 gate 1이 정확히 그 상태였다 — 토요일 새벽 CM을 매주 오거부).
    """
    return now.date() - timedelta(days=1) if now.time() <= _NIGHT_END else now.date()


def get_active_session(
    now: datetime,
    contract_expiry_date: date,
) -> Optional[Literal["CF", "CM"]]:
    """현재 시점에 active한 KRX 미국달러선물 세션 반환.

    Args:
        now: 현재 시각 (KST naive datetime)
        contract_expiry_date: **운영 중인 contract의 만기일** (필수, 2026-08-16 승격).
            - today와 같음: 만기일 정규세션 11:30 종료 적용 (expiring 월물).
              + Codex Issue 3 fix: 11:30 이후는 정규/야간 모두 차단 (만기 종목 야간 거래 없음).
            - today와 다름 (next month 등): 정규세션 15:45 + 정상 야간세션.
            - today보다 과거 (expired): 모든 세션 차단 (Codex Issue 3 fix).

        **필수 인자로 승격한 이유 (2026-08-16)**: 구 signature는 `Optional[date] = None`
        이었고, None이면 `is_expiry_day(today)` 캘린더 기반으로 추정했다. 그 추정 경로가
        (a) 만기 정보를 모르는 종목에도 세션을 열어 주는 **fail-open**이고
        (b) 유일한 비테스트 소비자가 만기 지난 `A75605`를 하드코딩한 WS smoke라
        8/14 사고와 같은 "subscribe success + 무프레임"을 재생산할 수 있었다.
        `ContractInfo.expiry_date`가 이미 필수 필드라 모든 호출자가 넘길 재료를 갖고
        있으므로, 추정을 없애고 명시를 강제한다. 삭제만 하고 None을 남기면 fail-open이다.

        contract-aware 동기 (PR6c-2d-1, 유지):
        - manual rollover로 next month로 swap한 client에 대해 calendar-based
          만기 판정이 True여서 11:30 종료 잘못 적용 → 11:30~15:45 A75606 disconnect
          버그 차단 (Issue 1). ← `is_expiry_day`를 "정확하게" 고치면 되살아나는 버그.
        - reconcile 누락/실패 시 expiring contract가 잔존하면 만기일 야간장(17:50~)에
          subscribe 시도해 만기 종목 spurious frame 위험 → 11:30 이후 전 세션 차단 (Issue 3).

    Returns:
        "CF" — 주간 정규세션 active (만기일 종료 시각은 contract_expiry_date 기준)
        "CM" — 야간세션 active (**시작일 기준** + contract_expiry_date 차단:
               expiring contract는 만기일 11:30 이후 차단, 만기 지난 종목은 항상 차단)
        None — 휴장 또는 contract 만료 후
    """
    today = now.date()
    t = now.time()

    # 0. expiring contract 전체 세션 차단 (PR6c-2d-1 amend, Codex Issue 3 fix)
    #    만기 후 / 만기일 11:30 이후의 expiring 종목은 정규/야간 모두 거래 없음
    #    (만기일 05:30 같은 만기일 새벽 야간장은 차단 X — 만기일 11:30 이전이고
    #    실제로는 전 영업일 시작 야간장이 이어진 구간)
    if contract_expiry_date < today:
        # 만기 지난 종목 (master 잔존 또는 reconcile 누락 시) — 모든 세션 차단
        return None
    if contract_expiry_date == today and t > _REGULAR_EXPIRY_END:
        # 만기일 11:30 이후 — 정규세션 종료 + 야간장 거래 없음 (만기 종목)
        return None

    # 1. 주간 정규세션 (영업일 + 정규시간)
    if is_krx_business_day(today):
        # 만기일 종료 시각 결정 — contract-aware (PR6c-2d-1 amend)
        if contract_expiry_date == today:
            # 만기 당일 contract → 11:30 종료 (위 0번에서 11:30 이후는 이미 차단)
            regular_end = _REGULAR_EXPIRY_END
        else:
            # next month (또는 미래 만기) → 정상 15:45 종료
            regular_end = _REGULAR_END
        if _REGULAR_START <= t <= regular_end:
            return "CF"

    # 2. 야간세션 (**시작일 기준** — 달력 당일이 아니다)
    #    17:50-23:59: 시작일 = now.date()
    #    00:00-06:00: 시작일 = now.date() - 1일 (금요일밤 세션이 토요일 06:00까지)
    #    2026-08-16: 정규장 predicate → 야간 전용 predicate로 배선. 정규장이 열린
    #    날에도 야간장만 휴장하는 공식 공지가 있어 두 축이 분리돼야 한다.
    if t >= _NIGHT_START:
        # 야간 시작일 = today
        if is_krx_night_session_open(today):
            return "CM"
    elif t <= _NIGHT_END:
        # 야간 시작일 = today - 1
        start_date = today - timedelta(days=1)
        if is_krx_night_session_open(start_date):
            return "CM"

    # 3. 그 외 (06:00-08:30 break, 15:45-17:50 break 또는 휴장일)
    return None


def is_in_session_end_grace(
    now: datetime,
    session: Optional[Literal["CF", "CM"]],
    grace_min: int,
    contract_expiry_date: Optional[date] = None,
) -> bool:
    """현재 시점이 session 종료 grace_min 분 이내인지.

    PR6d-2b: REST fallback 트리거 차단 grace 구간 판정.
    CM 종료 전 -40분(default) 자연 silence cluster (5/8 baseline 4건 모두 -32분 이내) +
    종가 단일가 10분 cover.

    Args:
        now: KST naive datetime
        session: "CF" / "CM" / None (휴장 시 always False)
        grace_min: grace 분 (예: 40)
        contract_expiry_date: 만기일 처리 (현재 v1은 무시 — 일반 거래일만)

    Returns:
        True면 grace 구간. fallback caller가 차단해야 함.
    """
    if session is None:
        return False
    if session == "CF":
        end = datetime.combine(now.date(), _REGULAR_END)
    elif session == "CM":
        if now.time() >= _NIGHT_START:
            end = datetime.combine(now.date() + timedelta(days=1), _NIGHT_END)
        else:
            end = datetime.combine(now.date(), _NIGHT_END)
    else:
        return False
    delta = (end - now).total_seconds()
    return 0 <= delta <= grace_min * 60


# ---------------------------------------------------------------------------
# Close snapshot boundary — KRX_CLOSE_SNAPSHOT_PLAN §4.2
# ---------------------------------------------------------------------------

# `now.replace(...)` 패턴 금지 — `datetime(...)`으로 명시 생성.
# CM은 calendar day(timestamp 기준)와 business day check(today - 1) 분리.

KST = ZoneInfo("Asia/Seoul")


def compute_close_boundary_kst(
    session: Literal["CF", "CM"],
    today_kst: date,
) -> datetime:
    """Close snapshot boundary timestamp (KST aware).

    Args:
        session: "CF" (정규세션 종료 15:45) / "CM" (야간세션 종료 06:00).
        today_kst: calendar day. CM의 경우 06:00이 찍히는 날짜 (today),
                   business day check (today - 1)와는 분리 책임.

    Returns:
        KST aware datetime. boundary 의미적 시각 (retry 실행 시각 아님).

    Notes:
        - KRX_CLOSE_SNAPSHOT_PLAN §4.2 안전 패턴
        - DB 저장 시 `astimezone(timezone.utc).replace(tzinfo=None)`으로 UTC naive 변환
        - Redis 저장 시 `isoformat()`으로 KST ISO string 변환
    """
    if session == "CF":
        return datetime(
            today_kst.year, today_kst.month, today_kst.day,
            15, 45, 0, 0,
            tzinfo=KST,
        )
    if session == "CM":
        return datetime(
            today_kst.year, today_kst.month, today_kst.day,
            6, 0, 0, 0,
            tzinfo=KST,
        )
    raise ValueError(f"unknown session: {session!r}")


def is_close_snapshot_eligible(
    session: Literal["CF", "CM"],
    today_kst: date,
) -> bool:
    """Close snapshot 실행 가능 여부 (휴장일 판정).

    Args:
        session: "CF" / "CM"
        today_kst: boundary가 찍히는 calendar day

    Returns:
        True: 정상 영업일 (snapshot 실행).
        False: 휴장일 (snapshot skip).

    Notes:
        - CF: today 자체가 **정규장** 영업일이어야 함
        - CM: 야간장 **시작일(today - 1)**이 개장이어야 함
              예: 토요일 06:00 = 금요일 야간장 종료 → today=토요일이지만 정상
        - 2026-08-16: CM 분기를 야간 전용 predicate로 배선. 정규장이 열린 날에도
          야간장만 휴장하는 공지가 있으므로, 그 밤의 close snapshot도 함께 막혀야
          한다(구 구현은 정규장 predicate라 그런 밤을 정상 세션으로 오판).
    """
    if session == "CF":
        return is_krx_business_day(today_kst)
    if session == "CM":
        return is_krx_night_session_open(today_kst - timedelta(days=1))
    raise ValueError(f"unknown session: {session!r}")


# ---------------------------------------------------------------------------
# Close finalizer window helpers — KRX_CLOSE_SNAPSHOT_PLAN §5.2 (2026-05-17)
# ---------------------------------------------------------------------------
# Stage 1 (2026-05-17): helper functions only — pure deterministic.
# 후속 stages에서 KrxCloseWindowWriter / WS grace drain / scheduler 통합 시 사용.

# Close decision-point grace window (60s): boundary 직후 KIS frame capture 보장
# CF close: 15:45:00 ~ 15:45:59 KST (15:46:00 exclusive)
# CM close: 06:00:00 ~ 06:00:59 KST (06:01:00 exclusive)
_CF_CLOSE_GRACE_START = time(15, 45, 0)
_CF_CLOSE_GRACE_END = time(15, 46, 0)
_CM_CLOSE_GRACE_START = time(6, 0, 0)
_CM_CLOSE_GRACE_END = time(6, 1, 0)

# Single-price auction window (close 확정 제외, telemetry only)
# CF: 15:35:00 ~ 15:44:59 KST (10분, KRX 종가 단일가)
# CM: 05:50:00 ~ 05:59:59 KST (10분, KRX 야간 종가 단일가)
_CF_SINGLE_PRICE_START = time(15, 35, 0)
_CM_SINGLE_PRICE_START = time(5, 50, 0)


def is_in_close_grace_window(
    now: datetime,
    session: Optional[Literal["CF", "CM"]],
) -> bool:
    """Close decision-point grace window(60초)인지.

    KRX_CLOSE_SNAPSHOT_PLAN §5.2 — boundary 직후 KIS frame capture 보장.
    CF close: 15:45:00 ~ 15:45:59 KST
    CM close: 06:00:00 ~ 06:00:59 KST

    Args:
        now: KST datetime (aware 또는 naive 모두 허용 — `.time()` 사용)
        session: "CF" / "CM" / None (휴장 → always False)

    Returns:
        True면 close grace window 안.
    """
    if session is None:
        return False
    t = now.time()
    if session == "CF":
        return _CF_CLOSE_GRACE_START <= t < _CF_CLOSE_GRACE_END
    if session == "CM":
        return _CM_CLOSE_GRACE_START <= t < _CM_CLOSE_GRACE_END
    return False


def is_in_single_price_window(
    now: datetime,
    session: Optional[Literal["CF", "CM"]],
) -> bool:
    """Single-price auction window인지 (close 확정 제외, telemetry only).

    KRX_CLOSE_SNAPSHOT_PLAN §5.2 — 단일가 진행 시간 frame은 잔여 echo로 분류.
    CF: 15:35:00 ~ 15:44:59 KST
    CM: 05:50:00 ~ 05:59:59 KST

    Args:
        now: KST datetime
        session: "CF" / "CM" / None

    Returns:
        True면 single-price auction window 안.
    """
    if session is None:
        return False
    t = now.time()
    if session == "CF":
        return _CF_SINGLE_PRICE_START <= t < _CF_CLOSE_GRACE_START
    if session == "CM":
        return _CM_SINGLE_PRICE_START <= t < _CM_CLOSE_GRACE_START
    return False


def compute_close_grace_end_kst(
    now: datetime,
    session: Literal["CF", "CM"],
) -> datetime:
    """Close grace window 종료 시각 (KST aware) — 후속 stages 사용.

    CF: 15:46:00 KST (15:45:59까지 listen, 15:46:00 정각에 flush)
    CM: 06:01:00 KST

    Args:
        now: 현재 KST datetime (date 추출용; aware/naive 모두 허용)
        session: "CF" / "CM"

    Returns:
        KST aware datetime (window 종료 정각, exclusive boundary).
    """
    if session == "CF":
        return datetime(
            now.year, now.month, now.day,
            15, 46, 0, 0,
            tzinfo=KST,
        )
    if session == "CM":
        return datetime(
            now.year, now.month, now.day,
            6, 1, 0, 0,
            tzinfo=KST,
        )
    raise ValueError(f"unknown session: {session!r}")
