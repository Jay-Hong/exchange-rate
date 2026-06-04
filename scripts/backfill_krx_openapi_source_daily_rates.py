#!/usr/bin/env python3
"""KRX OPEN API `fut_bydd_trd` → source_daily_rates backfill (Step 4B source 전환).

KRX_STEP4B_PLAN.md §0 / DECISIONS.md ADR-033 Amendment 2 Step 4B 정정.
KIS year-series 한계(A755xx=2025 미조회) → KRX 공식 OPEN API date-based로 전환.

이 모듈 (단위 1 — KRX 응답 parser + front-month row filter, 순수·mock 테스트):
  - parse_krx_fut_response: 응답 → OutBlock_1 rows
  - select_usd_front_month_row: 그날 selected front-month(미국달러 F {YYYYMM} 정규 주간) 1개

다음 단위:
  - row → source_daily_rates dict 변환 (source_method enum 결정 후)
  - date-based manifest builder (build_contract_sequence 재사용 + basDd fetch + 이 filter)

endpoint (참고, 키는 코드/문서 미기재):
  GET https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd?basDd=YYYYMMDD  (헤더 AUTH_KEY)
  응답: {"OutBlock_1": [{BAS_DD, PROD_NM, MKT_NM, ISU_CD, ISU_NM, TDD_CLSPRC, SETL_PRC, ...}, ...]}
"""
from __future__ import annotations

import re

# Row filter 상수 (KRX_STEP4B_PLAN §0.3)
PROD_NM_USD = "미국달러 선물"
MKT_NM_REGULAR = "정규"
# 단일물(F) 정규장(주간) exact 매칭: "미국달러 F {YYYYMM} (주간)".
# anchor(^/$)로 스프레드("미국달러 SP ..."), (야간), suffix 변형("202606X")을 모두 차단.
_ISU_USD_F_REGULAR = re.compile(r"^미국달러 F (\d{6}) \(주간\)$")


def parse_krx_fut_response(response: dict) -> list[dict]:
    """KRX `fut_bydd_trd` 응답 → OutBlock_1 rows (raw dict list).

    KRX 정상 응답은 `OutBlock_1` 배열. 누락/형식 이상은 fail (Crash Early).
    """
    block = response.get("OutBlock_1")
    if block is None:
        raise ValueError("KRX fut_bydd_trd 응답에 OutBlock_1 없음 (인증 실패/형식 이상?)")
    if not isinstance(block, list):
        raise ValueError(f"OutBlock_1 형식 이상: {type(block).__name__}")
    return block


def select_usd_front_month_row(rows: list[dict], contract_month: str) -> dict | None:
    """그날 rows 중 selected front-month 미국달러선물 정규장 주간 row 1개 (순수 filter).

    필터 (KRX_STEP4B_PLAN §0.3):
      PROD_NM == "미국달러 선물" ∧ MKT_NM == "정규"
      ∧ ISU_NM이 `^미국달러 F (\\d{6}) \\(주간\\)$`에 exact 매칭 (SP·야간·suffix 변형 차단)
      ∧ 추출 YYYYMM == contract_month (exact)
      ∧ TDD_CLSPRC != ""

    contract_month: "202606" — `build_contract_sequence`(만기일=next)가 결정한 그날 front-month.
      → 만기일 경계에서 만기월물(202605)이 같은 응답에 있어도, contract_month=202606이면 next만 선택.

    return:
      - 매칭 row 1개 (정상)
      - None: front-month row가 없거나 TDD_CLSPRC 빈 경우 → 호출자(manifest builder)가 coverage hard 처리.
      - raise: 동일 contract_month에 유효 row 2개+ (KRX 데이터/필터 이상 — Crash Early).
    """
    matches: list[dict] = []
    for r in rows:
        if r.get("PROD_NM") != PROD_NM_USD or r.get("MKT_NM") != MKT_NM_REGULAR:
            continue
        m = _ISU_USD_F_REGULAR.match(r.get("ISU_NM", ""))
        if m is None or m.group(1) != contract_month:  # exact YYYYMM (SP/야간/suffix 차단)
            continue
        matches.append(r)

    valid = [r for r in matches if r.get("TDD_CLSPRC", "") != ""]
    if len(valid) > 1:
        raise ValueError(
            f"front-month {contract_month} 유효 row 중복: {len(valid)}개 "
            f"({[r.get('ISU_NM') for r in valid]})"
        )
    return valid[0] if valid else None
