#!/usr/bin/env python3
"""KRX OPEN API `fut_bydd_trd` → source_daily_rates backfill (Step 4B source 전환).

KRX_STEP4B_PLAN.md §0 / DECISIONS.md ADR-033 Amendment 2 Step 4B 정정.
KIS year-series 한계(A755xx=2025 미조회) → KRX 공식 OPEN API date-based로 전환.

이 모듈 (Step 4B — KRX parser + filter + 변환 + date-based manifest builder, 순수·mock 테스트):
  - parse_krx_fut_response: 응답 → OutBlock_1 rows
  - select_usd_front_month_row: 그날 selected front-month(미국달러 F {YYYYMM} 정규 주간) 1개
  - krx_row_to_source_daily: 선택 row → source_daily_rates dict (source_method=krx_openapi_daily)
  - build_krx_manifest: weekday-only date loop + BAS_DD==요청일 가드 → manifest (date-based)

다음 단위:
  - range dry-run wiring (build_contract_sequence + fetch_fn(date) + build_krx_manifest + compare_manifest_with_existing)
  - 기존 25 KIS rows 전수 transitional match 확인 (PASS_WITH_TRANSITIONAL 게이트)

endpoint (참고, 키는 코드/문서 미기재):
  GET https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd?basDd=YYYYMMDD  (헤더 AUTH_KEY)
  응답: {"OutBlock_1": [{BAS_DD, PROD_NM, MKT_NM, ISU_CD, ISU_NM, TDD_CLSPRC, SETL_PRC, ...}, ...]}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
      - None: front-month row가 없거나 TDD_CLSPRC 빈 경우 → 호출자(manifest builder)가 missing_dates(no_front_month)로 surface (hard 아님; coverage hard는 변환 실패 경로).
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


# ─────────────────────────────────────────────────────────────
# Date-based manifest builder (Step 4B 단위 — weekday loop + BAS_DD 가드)
# ─────────────────────────────────────────────────────────────
#
# KIS build_manifest(contract-segment range fetch — KIS 응답이 거래일 set)와 달리,
# KRX OpenAPI fut_bydd_trd는 date-based(basDd=YYYYMMDD, 그날 ~385 선물 전체 반환)라
# **우리가 날짜를 enumerate**한다. build_contract_sequence segment를 그대로 받아
# 각 segment의 weekday를 순회하며 그날 front-month row 1개를 뽑아 변환한다.
#
# 핵심 안전 가드 (ADR-034 §11 / KRX_CANARY 2026-05-25 stale 사고 동형 차단):
#   selected row의 BAS_DD != 요청 basDd → ingest 금지 (변환 전 거부). KRX가 비거래일에
#   직전 거래일 stale 데이터를 반환해도 잘못된 날짜의 종가가 들어가지 않음.


@dataclass
class KrxManifestResult:
    """build_krx_manifest 산출물 (date-based).

    rows: 변환 통과 manifest row dict (date_kst 오름차순, hard 없으면 dup 0 = strictly increasing).
    hard_issues: fail-close (변환 실패=coverage / duplicate / select 이상 / 0 rows). 호출자(dry-run)가 비어야 write.
    warnings: gap surface (연휴/휴장/결손 — 항상 surface, hard 아님).
    missing_dates: weekday인데 row 미생성 — {"date": date, "reason": "no_front_month"|"bas_dd_mismatch:<bas>"}.
        leading/trailing 포함 surface (window 가장자리가 비거래일에 걸리는 건 정상).
    boundary_samples: segment 경계(±1일) row (만기일=next boundary 검증 surface).
    """
    rows: list[dict]
    hard_issues: list[str]
    warnings: list[str]
    missing_dates: list[dict]
    boundary_samples: list[dict]


def _duplicate_date_issues(rows: list[dict]) -> list[str]:
    """manifest rows 중복 date_kst → hard issue list (구조적으로 불가하나 로직 버그 방어).

    weekday 1회 순회 + BAS_DD==요청일 가드 → date_kst는 enumerate한 날짜와 동일·유일.
    중복이 나오면 enumerate/guard 로직 버그 신호이므로 Crash Early.
    """
    seen: set = set()
    issues: list[str] = []
    for r in rows:
        dk = r["date_kst"]
        if dk in seen:
            issues.append(f"manifest duplicate date_kst={dk} (defensive — enumerate/guard 버그 신호)")
        seen.add(dk)
    return issues


def build_krx_manifest(sequence, window_start: date, window_end: date, fetch_fn) -> KrxManifestResult:
    """weekday-only date loop으로 KRX fut_bydd_trd manifest 조립 (Step 4B, date-based).

    sequence: build_contract_sequence 결과 [(contract, seg_start, seg_end)] (재사용).
      contract는 duck typing (.short_code / .contract_month / .expiry_date).
    fetch_fn(d: date) -> list[raw OutBlock_1 rows]  (그날 선물 전체 — network 격리: 테스트는 mock).

    per-date (weekday만, seg_end=만기일 제외 → boundary=next 유지):
      select None              → missing_dates(no_front_month) surface
      BAS_DD != 요청일          → missing_dates(bas_dd_mismatch) surface, **row 생성 금지** (stale 가드)
      select 이상(동일월물 2개+) → hard
      변환 실패(CLS·high·low 빈) → hard (coverage)
      정상                      → krx_row_to_source_daily(row, contract)
    """
    # 함수-레벨 import — 무거운 cross-module(app deps) 로드를 parser 순수 테스트에서 격리.
    # gap 임계(연휴 4일+ 정상)는 KIS와 동일 business rule이라 재사용 (DRY).
    from backfill_kis_source_daily_rates import GAP_SURFACE_THRESHOLD_DAYS

    rows: list[dict] = []
    hard_issues: list[str] = []
    warnings: list[str] = []
    missing_dates: list[dict] = []
    boundary_samples: list[dict] = []

    for contract, seg_start, seg_end in sequence:
        d_start = max(seg_start, window_start)
        d_end = min(seg_end - timedelta(days=1), window_end)  # 만기일(seg_end) 제외 = boundary=next
        d = d_start
        while d <= d_end:
            if d.weekday() >= 5:  # weekday-only (주말 미호출 — KRX 주말 거동 비의존)
                d += timedelta(days=1)
                continue
            req = d.strftime("%Y%m%d")
            raw = fetch_fn(d)
            try:
                selected = select_usd_front_month_row(raw, contract.contract_month)
            except ValueError as e:  # 같은 날 동일 contract_month 유효 row 2개+ (KRX 데이터 이상)
                hard_issues.append(f"date={d} select 이상: {e}")
                d += timedelta(days=1)
                continue
            if selected is None:
                missing_dates.append({"date": d, "reason": "no_front_month"})
                d += timedelta(days=1)
                continue
            bas = selected.get("BAS_DD")
            if bas != req:  # stale/cross-date 가드 — 변환 전 거부 (2026-05-25 사고 동형 차단)
                missing_dates.append({"date": d, "reason": f"bas_dd_mismatch:{bas}"})
                d += timedelta(days=1)
                continue
            try:
                row = krx_row_to_source_daily(selected, contract)
            except ValueError as e:  # CLS/high/low 빈 → source_ohlc 불완전 = coverage hard
                hard_issues.append(f"date={d} 변환 실패(coverage): {e}")
                d += timedelta(days=1)
                continue
            rows.append(row)
            if (d - seg_start).days <= 1 or (seg_end - d).days <= 1:  # segment 경계 ±1일
                boundary_samples.append(
                    {"contract": contract.short_code, "date_kst": d, "close": row.get("close")}
                )
            d += timedelta(days=1)

    rows.sort(key=lambda r: r["date_kst"])
    hard_issues.extend(_duplicate_date_issues(rows))

    dates = [r["date_kst"] for r in rows]
    for i in range(len(dates) - 1):
        gap = (dates[i + 1] - dates[i]).days
        if gap >= GAP_SURFACE_THRESHOLD_DAYS:
            warnings.append(f"gap {gap}d: {dates[i]} -> {dates[i + 1]} (연휴/휴장/결손 surface)")

    if not rows and not hard_issues:
        hard_issues.append("manifest 0 rows (window 전체 미수집 — coverage hard)")

    return KrxManifestResult(rows, hard_issues, warnings, missing_dates, boundary_samples)
