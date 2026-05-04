"""KIS 상품선물 마스터 파일 (fo_com_code.mst) — front-month resolver (PR6b-1).

운영 미연결 상태 (스케줄러/DB 연결 X). PR6c canary 진입 시 매일 새벽
한 번 갱신해서 KRX 미국달러선물의 근월물 종목코드를 자동 선택.

PR6a static canary (A75605, 2026-05-18 만기) 대체 — 만기 다음날
자동으로 다음 만기 종목 (예: A75606, 2026-06-15)으로 전환.

KIS 공식 패턴:
  examples_llm/stocks_info/domestic_commodity_future_code.py 참조
  파일 형식: cp949 fixed-width text in zip
  컬럼:
    [0:1]   상품구분 (1자)
    [1:2]   상품종류 (1자)
    [2:11]  단축코드 (9자, strip)
    [11:23] 표준코드 (12자, strip)
    [23:55] 한글종목명 (32자, strip)
    [55:]   월물구분코드 + 기초자산 (PR6b-1 미사용)

USD/KRW 선물 식별:
  종목명 prefix "미국달러 F" — 다른 통화선물 (엔 / 유로 / 위안)과 구분
  종목명 끝 YYYYMM (6자) — contract_month
  만기일: YYYYMM의 셋째 월요일 (KRX 미국달러선물 최종거래일)
"""
from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

KIS_MASTER_URL = "https://new.real.download.dws.co.kr/common/master/fo_com_code.mst.zip"
USD_FUTURES_NAME_PREFIX = "미국달러 F"


@dataclass(frozen=True)
class ContractInfo:
    """KIS 상품선물 마스터 단일 종목 메타데이터.

    PR6b-1 한정으로 USD/KRW futures 식별 + front-month 선택에 사용.
    추가 필드 (월물구분코드 등)은 필요 시 PR6b-3 또는 후속에서 확장.
    """

    short_code: str        # "A75605" — KIS WebSocket tr_key 및 REST 종목코드
    standard_code: str     # "KR4A75650007" — 12자 ISIN-like
    name: str              # "미국달러 F 202605"
    contract_month: str    # "202605" — YYYYMM 6자
    expiry_date: date      # date(2026, 5, 18) — 셋째 월요일

    def is_usd_krw_futures(self) -> bool:
        """미국달러 선물 여부 — 종목명 prefix 매칭."""
        return self.name.startswith(USD_FUTURES_NAME_PREFIX)


def fetch_commodity_future_master(*, timeout: int = 30) -> bytes:
    """KIS 마스터 ZIP 다운로드 → 압축 해제된 .mst 파일 bytes 반환.

    네트워크 호출 — 단위 테스트는 mock으로 우회. 운영에서는 매일
    1회만 호출 (PR6c scheduler 등록 시점).

    Returns:
        cp949 인코딩 bytes (fixed-width text).
    """
    logger.info("[kis_master] downloading %s", KIS_MASTER_URL)
    r = requests.get(KIS_MASTER_URL, timeout=timeout)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        for name in names:
            if name.endswith(".mst"):
                return zf.read(name)
        raise RuntimeError(f"[kis_master] .mst file not found in zip: {names}")


def parse_commodity_future_master(raw: bytes) -> List[ContractInfo]:
    """KIS fo_com_code.mst → ContractInfo 리스트.

    cp949 디코딩 후 char-position fixed-width 파싱. 종목명에서 YYYYMM
    추출 + 만기일 계산 (셋째 월요일). YYYYMM 추출 실패 시 해당 row 스킵.

    Args:
        raw: KIS 마스터 .mst bytes (cp949 인코딩)

    Returns:
        ContractInfo 리스트 (USD futures 외 다른 상품선물도 모두 포함 —
        호출자가 .is_usd_krw_futures()로 필터)
    """
    text = raw.decode("cp949")
    contracts: List[ContractInfo] = []
    for line in text.splitlines():
        if len(line) < 55:
            continue
        short_code = line[2:11].strip()
        standard_code = line[11:23].strip()
        name = line[23:55].strip()
        if not short_code or not name:
            continue
        contract_month = _extract_contract_month(name)
        if not contract_month:
            continue
        expiry = _compute_expiry_date(contract_month)
        if expiry is None:
            continue
        contracts.append(
            ContractInfo(
                short_code=short_code,
                standard_code=standard_code,
                name=name,
                contract_month=contract_month,
                expiry_date=expiry,
            )
        )
    return contracts


def _extract_contract_month(name: str) -> Optional[str]:
    """종목명 끝의 YYYYMM 추출. '미국달러 F 202605' → '202605'."""
    parts = name.split()
    if not parts:
        return None
    last = parts[-1]
    if len(last) == 6 and last.isdigit():
        return last
    return None


def _compute_expiry_date(contract_month: str) -> Optional[date]:
    """YYYYMM → 셋째 월요일 (KRX 미국달러선물 최종거래일).

    Args:
        contract_month: "YYYYMM" 6자

    Returns:
        date 또는 None (입력 형식 잘못된 경우).

    Note:
        실제 KRX 휴장일이 셋째 월요일과 겹치면 KRX 공시로 만기일 변경
        가능 (예: 어린이날). 본 함수는 표준 셋째 월요일만 계산 — 정확한
        만기일은 KRX 공식 캘린더 또는 마스터 파일의 별도 필드 (PR6b-3
        에서 보강) 참조 필요.
    """
    if len(contract_month) != 6 or not contract_month.isdigit():
        return None
    year = int(contract_month[:4])
    month = int(contract_month[4:6])
    if not (1 <= month <= 12):
        return None
    first = date(year, month, 1)
    # 0=Mon, 6=Sun. 1일이 월요일이면 days_to_first_monday=0
    days_to_first_monday = (0 - first.weekday()) % 7
    first_monday_day = 1 + days_to_first_monday
    third_monday_day = first_monday_day + 14
    try:
        return date(year, month, third_monday_day)
    except ValueError:
        # 30/31일 경계 — 일반적으로 셋째 월요일은 15-21일 사이라 발생 X
        return None


def select_front_month_usd_futures(
    contracts: List[ContractInfo],
    *,
    today: Optional[date] = None,
) -> Optional[ContractInfo]:
    """USD/KRW 선물 중 today 기준 만기 가장 가까운 미만료 종목 (front-month).

    **date-level resolver** — 만기일 11:30 정규세션 종료 시점은 처리 X.
    만기일 당일 전체를 만기 종목으로 봄. 즉 만기일 5/18 18:00 야간장은
    이미 다음 월물로 넘어가야 하지만 본 함수는 5/19부터만 rollover.

    운영 사용 시 caveat:
      - 매일 00:30 KST 갱신 + 본 함수 사용 → 만기일 오후/야간 잘못된 종목 위험
      - PR6c에서 `select_active_usd_futures_contract(contracts, now_kst)`
        추가 권장 (만기일 11:30 이후는 다음 월물 선택)

    Args:
        contracts: parse_commodity_future_master() 결과
        today: 기준일 (default: date.today())

    Returns:
        ContractInfo 또는 None (USD futures 자체 없거나 모두 만료).
    """
    # TODO(PR6c): intraday rollover — 만기일 11:30 정규세션 종료 후 다음
    # 월물 선택 함수 select_active_usd_futures_contract(contracts, now_kst)
    # 추가. 운영 scheduler에서 본 함수 대신 호출하면 만기일 야간장 정상.
    if today is None:
        today = date.today()
    candidates = [
        c for c in contracts
        if c.is_usd_krw_futures() and c.expiry_date >= today
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c.expiry_date)


def resolve_front_month_usd_futures(
    *,
    today: Optional[date] = None,
    timeout: int = 30,
) -> Optional[ContractInfo]:
    """fetch + parse + select 통합 함수.

    **PR6c intraday 보강 전까지는 운영 최종 선택 함수 아님** —
    select_front_month_usd_futures()의 date-level limit 그대로 상속.
    만기일 11:30 이후 / 18:00 야간장에 잘못된 종목 잡을 위험.

    PR6c scheduler에서 본 함수를 직접 사용 X. 대신 select_active_
    usd_futures_contract(contracts, now_kst) (PR6c에서 추가) 또는
    동일 시간 분기 wrapper 경유 권장. 본 함수는 마스터 fetch + parse
    + date-level select 단계까지의 통합만 제공.

    네트워크 실패 시 None 반환 + caller가 fallback (이전 캐시) 결정.

    Returns:
        date-level front-month ContractInfo 또는 None.
    """
    try:
        raw = fetch_commodity_future_master(timeout=timeout)
    except Exception as e:
        logger.warning(
            "[kis_master] fetch failed: %s: %s", type(e).__name__, e
        )
        return None
    try:
        contracts = parse_commodity_future_master(raw)
    except Exception as e:
        logger.warning(
            "[kis_master] parse failed: %s: %s", type(e).__name__, e
        )
        return None
    selected = select_front_month_usd_futures(contracts, today=today)
    if selected:
        logger.info(
            "[kis_master] front-month USD futures: %s (%s, expiry=%s)",
            selected.short_code, selected.name, selected.expiry_date.isoformat(),
        )
    else:
        logger.warning("[kis_master] no front-month USD futures found")
    return selected
