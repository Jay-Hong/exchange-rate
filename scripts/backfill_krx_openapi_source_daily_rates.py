#!/usr/bin/env python3
"""KRX OPEN API `fut_bydd_trd` → source_daily_rates backfill (Step 4B source 전환).

KRX_STEP4B_PLAN.md §0 / DECISIONS.md ADR-033 Amendment 2 Step 4B 정정.
KIS year-series 한계(A755xx=2025 미조회) → KRX 공식 OPEN API date-based로 전환.

이 모듈 (Step 4B — KRX 응답 parser + filter + source_daily 변환, 순수·mock 테스트):
  - parse_krx_fut_response: 응답 → OutBlock_1 rows
  - select_usd_front_month_row: 그날 selected front-month(미국달러 F {YYYYMM} 정규 주간) 1개
  - krx_row_to_source_daily: 선택 row → source_daily_rates dict (source_method=krx_openapi_daily)

다음 단위:
  - date-based manifest builder (build_contract_sequence 재사용 + basDd fetch + 이 filter + 변환)
  - range dry-run (기존 25 KIS rows 전수 transitional match 확인)

endpoint (참고, 키는 코드/문서 미기재):
  GET https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd?basDd=YYYYMMDD  (헤더 AUTH_KEY)
  응답: {"OutBlock_1": [{BAS_DD, PROD_NM, MKT_NM, ISU_CD, ISU_NM, TDD_CLSPRC, SETL_PRC, ...}, ...]}
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

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


# ─────────────────────────────────────────────────────────────
# row → source_daily_rates 변환 (단위 — 순수, source_method=krx_openapi_daily)
# ─────────────────────────────────────────────────────────────

SOURCE = "krx"
ASSET = "usd-krw-futures"
CLOSE_BASIS = "krx_cf_close_1545"
SOURCE_METHOD = "krx_openapi_daily"
OHLC_QUALITY = "source_ohlc"


def _parse_krx_decimal(s: str) -> Decimal:
    """KRX 가격 string → Decimal. KRX는 보통 comma 없으나 string 타입이라 comma 방어."""
    return Decimal(s.replace(",", "").strip())


def krx_row_to_source_daily(row: dict, contract) -> dict:
    """선택된 front-month KRX row → source_daily_rates dict (순수 변환).

    정책 (KRX_STEP4B_PLAN §0.3, source_ohlc 일관):
      - TDD_CLSPRC 빈 → raise (close 필수, rate=close)
      - TDD_HGPRC / TDD_LWPRC 빈 → raise (source_ohlc 불완전 = coverage hard. close fallback 안 함)
      - TDD_OPNPRC: metadata open = Decimal parse 후 str 저장 (OHLC family라 high/low처럼 정규화·comma 방어), 빈이면 None (top-level 컬럼 없음)
      - source_method=krx_openapi_daily / close_basis=krx_cf_close_1545 / ohlc_quality=source_ohlc
      - SETL_PRC/SPOT_PRC/ACC_*/ISU_CD/ISU_NM: metadata audit (raw passthrough — OHLC 아닌 보조라 해석 안 함)

    contract: build_contract_sequence의 ContractInfo (short_code / contract_month / expiry_date, duck typing).
    호출자(manifest builder)는 이 raise를 catch해 coverage hard로 surface.
    """
    cls = row.get("TDD_CLSPRC", "")
    if cls == "":
        raise ValueError(f"TDD_CLSPRC 빈 — 변환 실패 (select filter 누락?): {row.get('ISU_NM')!r}")
    hi = row.get("TDD_HGPRC", "")
    lo = row.get("TDD_LWPRC", "")
    if hi == "" or lo == "":
        raise ValueError(
            f"TDD_HGPRC/LWPRC 빈 — source_ohlc 불완전 (coverage hard): "
            f"{row.get('ISU_NM')!r} H={hi!r} L={lo!r}"
        )

    close = _parse_krx_decimal(cls)
    opn = row.get("TDD_OPNPRC", "")
    open_dec = _parse_krx_decimal(opn) if opn != "" else None
    date_kst = datetime.strptime(row["BAS_DD"], "%Y%m%d").date()

    return {
        "source": SOURCE,
        "asset": ASSET,
        "date_kst": date_kst,
        "rate": close,   # invariant: rate == close
        "close": close,
        "high": _parse_krx_decimal(hi),
        "low": _parse_krx_decimal(lo),
        "ohlc_quality": OHLC_QUALITY,
        "close_basis": CLOSE_BASIS,
        "source_method": SOURCE_METHOD,
        "contract_code": contract.short_code,
        "basis_date": None,
        "published_at": None,
        "metadata_json": {
            "contract_short_code": contract.short_code,
            "contract_month": contract.contract_month,
            "contract_expiry_date": contract.expiry_date.isoformat(),
            "open": str(open_dec) if open_dec is not None else None,  # OHLC family → Decimal parse 후 str (정규화), 빈 None
            "settlement_price": row.get("SETL_PRC"),
            "spot_price": row.get("SPOT_PRC"),
            "acc_trdvol": row.get("ACC_TRDVOL"),
            "acc_opnint_qty": row.get("ACC_OPNINT_QTY"),
            "isu_cd": row.get("ISU_CD"),
            "isu_nm": row.get("ISU_NM"),
        },
    }
