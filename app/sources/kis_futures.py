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
  - 영업일 / 만기일 판정 (검증된 최소 데이터)

검증 완료 (2026-05-04 KST smoke):
  - 주간 09:38: H0CFCNT0/H0CFASP0 + A75605 → 588 msg/15s tick
  - 야간 18:03: H0MFCNT0/H0MFASP0 + A75605 → 63 msg/15s tick (거래량 9× 낮음, 정상)
  - 가격 broker 앱 cross-check 일치 (주간 1483.30 / 야간 1474.50 어제, 오늘 야간 1468.50)

검증 미정 (PR6 운영 연결 시점에 보강):
  - 만기일 정규세션 11:30 종료 처리 — 캘린더 데이터 보강 후 실증
  - 한국 정규 공휴일 전체 매핑 — holidays 라이브러리 또는 KRX 공식 캘린더 도입 후
  - 임시휴장 추적 — KRX 공시 기반

KIS field mapping 출처:
  주간: examples_llm/domestic_futureoption/commodity_futures_realtime_conclusion/
        examples_llm/domestic_futureoption/commodity_futures_realtime_quote/
  야간: examples_llm/domestic_futureoption/krx_ngt_futures_ccnl/
        examples_llm/domestic_futureoption/krx_ngt_futures_asking_price/
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, FrozenSet, List, Literal, Optional


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
# Calendar — 검증된 최소 데이터 (TODO 명시)
# ---------------------------------------------------------------------------

# 2026 KRX 휴장일 (외부 cross-check 완료된 항목만).
# TODO: 신정/설/삼일절/부처님 오신날/현충일/광복절/추석/개천절/한글날/크리스마스
#       정확 매핑은 holidays 라이브러리 또는 KRX 공식 캘린더 도입 후 보강.
# TODO: 임시휴장 (KRX 공시 기반).
KRX_2026_KNOWN_HOLIDAYS: FrozenSet[date] = frozenset({
    date(2026, 5, 5),  # 어린이날 (외부 cross-check 완료)
})

# 미국달러선물 (A75x) 만기일. 만기월 셋째 월요일.
# 검증된 1건만 — 다른 월은 KIS master 또는 KRX 공식 캘린더로 보강.
KRX_2026_USDF_EXPIRY_DAYS: FrozenSet[date] = frozenset({
    date(2026, 5, 18),  # A75605 (KIS master 확인)
    # TODO: 6월 / 7월 / 8월 / 9월 / 10월 / 11월 / 12월 만기일 보강
})


def is_krx_business_day(d: date) -> bool:
    """KRX 영업일 여부.

    주말 (토/일) 또는 KRX_2026_KNOWN_HOLIDAYS 포함이면 False.
    검증되지 않은 다른 휴일이 있을 수 있음 — 운영 코드에서는
    holidays 라이브러리 또는 KRX 공식 캘린더로 보강 필수.
    """
    if d.weekday() >= 5:  # 5=토, 6=일
        return False
    if d in KRX_2026_KNOWN_HOLIDAYS:
        return False
    return True


def is_expiry_day(d: date) -> bool:
    """미국달러선물 만기일 여부 (검증된 최소 데이터).

    True 반환 시 정규세션이 11:30에 종료. False 반환은 "만기일 아님" 또는
    "캘린더 데이터 미보강" 둘 다 포함하므로, 운영 코드에서는 캘린더 보강 필수.
    """
    return d in KRX_2026_USDF_EXPIRY_DAYS


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


def get_active_session(
    now: datetime,
    contract_expiry_date: Optional[date] = None,
) -> Optional[Literal["CF", "CM"]]:
    """현재 시점에 active한 KRX 미국달러선물 세션 반환.

    Args:
        now: 현재 시각 (KST naive datetime)
        contract_expiry_date: **운영 중인 contract의 만기일** (PR6c-2d-1 amend, 2026-05-07).
            - None: legacy 호환. is_expiry_day(today) 캘린더 기반 판정 (보수적 fallback).
            - today와 같음: 만기일 정규세션 11:30 종료 적용 (expiring 월물).
              + Codex Issue 3 fix: 11:30 이후는 정규/야간 모두 차단 (만기 종목 야간 거래 없음).
            - today와 다름 (next month 등): 정규세션 15:45 + 정상 야간세션.
            - today보다 과거 (expired): 모든 세션 차단 (Codex Issue 3 fix).

        contract-aware 추가 동기 (PR6c-2d-1):
        - manual rollover로 next month로 swap한 client에 대해 calendar-based
          `is_expiry_day(today)`가 True여서 11:30 종료 잘못 적용 → 11:30~15:45
          A75606 disconnect 버그 차단 (Issue 1).
        - reconcile 누락/실패 시 expiring contract가 잔존하면 만기일 야간장(17:50~)에
          subscribe 시도해 만기 종목 spurious frame 위험 → 11:30 이후 전 세션 차단 (Issue 3).

    Returns:
        "CF" — 주간 정규세션 active (만기일 종료 시각은 contract_expiry_date 기준)
        "CM" — 야간세션 active (시작일 기준 정책 + contract_expiry_date 차단:
               expiring contract는 만기일 11:30 이후 차단, 만기 지난 종목은 항상 차단.
               next month / 미래 만기 / legacy None은 시작일 기준만 적용)
        None — 휴장 또는 contract 만료 후
    """
    today = now.date()
    t = now.time()

    # 0. expiring contract 전체 세션 차단 (PR6c-2d-1 amend, Codex Issue 3 fix)
    #    만기 후 / 만기일 11:30 이후의 expiring 종목은 정규/야간 모두 거래 없음
    #    (만기일 05:30 같은 만기일 새벽 야간장은 차단 X — 만기일 11:30 이전이고
    #    실제로는 전 영업일 시작 야간장이 이어진 구간)
    if contract_expiry_date is not None:
        if contract_expiry_date < today:
            # 만기 지난 종목 (master 잔존 또는 reconcile 누락 시) — 모든 세션 차단
            return None
        if contract_expiry_date == today and t > _REGULAR_EXPIRY_END:
            # 만기일 11:30 이후 — 정규세션 종료 + 야간장 거래 없음 (만기 종목)
            return None

    # 1. 주간 정규세션 (영업일 + 정규시간)
    if is_krx_business_day(today):
        # 만기일 종료 시각 결정 — contract-aware (PR6c-2d-1 amend)
        if contract_expiry_date is None:
            # legacy: 캘린더 기반 (next month 운영 시 부정확하지만 보수적)
            regular_end = _REGULAR_EXPIRY_END if is_expiry_day(today) else _REGULAR_END
        elif contract_expiry_date == today:
            # 만기 당일 contract → 11:30 종료 (위 0번에서 11:30 이후는 이미 차단)
            regular_end = _REGULAR_EXPIRY_END
        else:
            # next month (또는 미래 만기) → 정상 15:45 종료
            regular_end = _REGULAR_END
        if _REGULAR_START <= t <= regular_end:
            return "CF"

    # 2. 야간세션 (시작일 기준)
    #    18:00-23:59: now.date()가 영업일이면 active
    #    00:00-06:00: (now.date() - 1일)이 영업일이면 active
    if t >= _NIGHT_START:
        # 야간 시작일 = today
        if is_krx_business_day(today):
            return "CM"
    elif t <= _NIGHT_END:
        # 야간 시작일 = today - 1
        start_date = today - timedelta(days=1)
        if is_krx_business_day(start_date):
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
