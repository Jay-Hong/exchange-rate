#!/usr/bin/env python3
"""KRX OPEN API `fut_bydd_trd` → source_daily_rates backfill (Step 4B source 전환).

KRX_STEP4B_PLAN.md §0 / DECISIONS.md ADR-033 Amendment 2 Step 4B 정정.
KIS year-series 한계(A755xx=2025 미조회) → KRX 공식 OPEN API date-based로 전환.

이 모듈 (Step 4B — KRX parser + filter + 변환 + manifest builder + range dry-run + HTTP helper + CLI, mock 테스트):
  - parse_krx_fut_response: 응답 → OutBlock_1 rows
  - select_usd_front_month_row: 그날 selected front-month(미국달러 F {YYYYMM} 정규 주간) 1개
  - krx_row_to_source_daily: 선택 row → source_daily_rates dict (source_method=krx_openapi_daily)
  - build_krx_manifest: weekday-only date loop + BAS_DD==요청일 가드 → manifest (date-based)
  - run_krx_range_dry_run: sequence + build_krx_manifest + _emit_compare_and_gate(KIS 공유) 게이트 (DB read-only)
  - fetch_krx_fut_bydd_trd / make_krx_fetch_fn: 실제 fut_bydd_trd HTTP (주입 가능, AUTH_KEY env, throttle + 429 abort)
  - main / _run_krx_range_dry_run_main: --range-dry-run CLI 진입점 (KRX_OPENAPI_AUTH_KEY env, window 산정, DB read-only)

다음 단위:
  - 운영 dry-run 실행 (별도 GO) — 실제 KRX 호출로 기존 25 KIS rows 전수 transitional match 확인
  - 전수 transitional 확인 후 migration/reingest (별도 GO)

endpoint (참고, 키는 코드/문서 미기재):
  GET https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd?basDd=YYYYMMDD  (헤더 AUTH_KEY)
  응답: {"OutBlock_1": [{BAS_DD, PROD_NM, MKT_NM, ISU_CD, ISU_NM, TDD_CLSPRC, SETL_PRC, ...}, ...]}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from dotenv import load_dotenv

# 프로젝트 루트를 sys.path에 추가 (standalone 실행 시 app.* / backfill_kis 재사용 import)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
            except (ValueError, InvalidOperation) as e:  # CLS/high/low 빈(ValueError) 또는 숫자 파싱 불가(InvalidOperation) → coverage hard
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


# ─────────────────────────────────────────────────────────────
# Range dry-run wiring (Step 4B 단위 — KRX 전용 게이트, DB read-only)
# ─────────────────────────────────────────────────────────────
#
# KIS run_range_dry_run과 분리: fetch_fn(date) + build_krx_manifest + cap 개념 없음 + missing_dates sentinel.
# compare 출력+게이트(PASS_WITH_TRANSITIONAL)는 _emit_compare_and_gate를 공유 (정책 단일화, drift 차단).
# manifest hard(변환실패/0rows/dup/select 이상) → compare skip + FAIL (오염 manifest 비교 회피, fail-close).
# DB write 0 — compare의 query SELECT만. fetch_fn 주입(이번 단위 mock; 실제 fut_bydd_trd HTTP는 다음 sub-step).


def run_krx_range_dry_run(window_start: date, window_end: date, fetch_fn, db) -> int:
    """KRX date-based write-intended dry-run 게이트. return: exit code (hard 있으면 1).

    fetch_fn(d: date) -> list[raw OutBlock_1 rows] (주입 — mock/실제 HTTP).
    출력 sentinel: WINDOW / MANIFEST_ROW_COUNT / MANIFEST_DATE_MIN·MAX(rows 0이면 None) /
      MANIFEST_HARD_COUNT / MISSING_DATES_COUNT / MISSING_DATES_JSON / BOUNDARY_SAMPLES_COUNT
      → (manifest hard면 skip+FAIL) → _emit_compare_and_gate (EXISTING/ROWS_TO_WRITE/MATCHED(exact)/
      TRANSITIONAL/COMPARE_HARD + PASS|PASS_WITH_TRANSITIONAL|FAIL).
    """
    # 함수-레벨 import — KIS 스크립트의 compare/sequence 재사용 (parser/manifest 순수 테스트는 KIS 미로드).
    from backfill_kis_source_daily_rates import build_contract_sequence, _emit_compare_and_gate

    print(f"WINDOW_START={window_start.isoformat()}")
    print(f"WINDOW_END={window_end.isoformat()}")
    sequence = build_contract_sequence(window_start, window_end)
    print(f"SEQUENCE_CONTRACTS={json.dumps([c.short_code for c, _, _ in sequence])}")
    for c, ss, se in sequence:
        print(f"  segment {c.short_code}: [{ss.isoformat()}, {se.isoformat()})")

    try:
        manifest = build_krx_manifest(sequence, window_start, window_end, fetch_fn)
    except (requests.RequestException, RuntimeError, ValueError, InvalidOperation) as e:
        print(f"FETCH_ERROR={type(e).__name__}: {e}")
        print("RANGE_DRY_RUN_RESULT=FAIL")
        return 1

    dates = [r["date_kst"] for r in manifest.rows]
    print(f"MANIFEST_ROW_COUNT={len(manifest.rows)}")
    print(f"MANIFEST_DATE_MIN={min(dates).isoformat() if dates else None}")  # rows 0이면 None (Crash 방지)
    print(f"MANIFEST_DATE_MAX={max(dates).isoformat() if dates else None}")
    print(f"MANIFEST_HARD_COUNT={len(manifest.hard_issues)}")
    print(f"WARNINGS_COUNT={len(manifest.warnings)}")
    print(f"MISSING_DATES_COUNT={len(manifest.missing_dates)}")
    print(f"BOUNDARY_SAMPLES_COUNT={len(manifest.boundary_samples)}")
    print(
        "MISSING_DATES_JSON="
        + json.dumps([{"date": m["date"].isoformat(), "reason": m["reason"]}
                      for m in manifest.missing_dates])
    )
    for w in manifest.warnings:
        print(f"  [warning] {w}")
    for h in manifest.hard_issues:
        print(f"  [HARD] {h}")

    if manifest.hard_issues:
        print("COMPARE_STATUS=skipped_due_to_manifest_hard")
        print("RANGE_DRY_RUN_RESULT=FAIL")
        return 1

    return _emit_compare_and_gate(manifest.rows, window_start, window_end, db)


# ─────────────────────────────────────────────────────────────
# HTTP helper (Step 4B 단위 — 실제 fut_bydd_trd 호출, 주입 가능)
# ─────────────────────────────────────────────────────────────
#
# fetch_krx_fut_bydd_trd: 단일 basDd HTTP 호출 + status 직접 분기 + parse_krx_fut_response.
# make_krx_fetch_fn: throttle + 429 abort를 감싼 fetch_fn(date) 생성 (build_krx_manifest 주입용).
# 보안: AUTH_KEY는 헤더로만 전달, 예외/로그/응답에 절대 미기재 (CLAUDE.md secret 원칙).
# 테스트: get_fn/sleep_fn/now_fn 주입으로 network·시간 0 (실제 KRX 호출은 운영 dry-run 별도 GO).

KRX_FUT_BYDD_TRD_URL = "https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd"
KRX_DEFAULT_TIMEOUT = 15.0
# KRX OpenAPI는 live crawler와 공유 endpoint 아님 + 월 한도 큼 → Hana 3.0s 불필요.
# 보수 default 1.0s (~252 weekday calls ≈ 4분). 실제 한도는 운영 dry-run서 관찰 후 튜닝.
KRX_DEFAULT_MIN_INTERVAL_SEC = 1.0


class KrxRateLimitAbort(RuntimeError):
    """HTTP 429 감지 시 endpoint 보호 위해 즉시 abort (Retry-After 보존, auth_key 미포함).

    RuntimeError 서브클래스 — run_krx_range_dry_run의 except가 잡아 FETCH_ERROR + FAIL로 graceful.
    """


def fetch_krx_fut_bydd_trd(
    bas_dd: date, auth_key: str, *, get_fn=requests.get, timeout: float = KRX_DEFAULT_TIMEOUT
) -> list[dict]:
    """단일 basDd KRX fut_bydd_trd 호출 → OutBlock_1 rows (raw dict list).

    https 직접 호출(redirect 무의존). AUTH_KEY는 헤더로만. status 직접 분기(raise_for_status 미사용):
      429    → KrxRateLimitAbort (Retry-After 포함, auth_key 미포함)
      != 200 → RuntimeError (status만 — body/headers/auth_key 미포함)
      == 200 → response.json() (JSON 실패 → RuntimeError, body snippet 미포함)
    → parse_krx_fut_response로 OutBlock_1 검증.

    보안: auth_key를 예외 메시지/로그에 절대 포함하지 않는다 (CLAUDE.md secret 원칙).
    get_fn 주입 → network 0 테스트. 실제 KRX 호출은 운영 dry-run 별도 GO.
    """
    response = get_fn(
        KRX_FUT_BYDD_TRD_URL,
        params={"basDd": bas_dd.strftime("%Y%m%d")},
        headers={"AUTH_KEY": auth_key},
        timeout=timeout,
    )
    status = response.status_code
    if status == 429:  # 먼저 — 429도 != 200이므로 순서 중요
        retry_after = response.headers.get("Retry-After")
        raise KrxRateLimitAbort(
            f"KRX fut_bydd_trd HTTP 429 basDd={bas_dd.isoformat()} (Retry-After={retry_after})"
        )
    if status != 200:
        raise RuntimeError(f"KRX fut_bydd_trd HTTP {status} basDd={bas_dd.isoformat()}")
    try:
        data = response.json()
    except ValueError as e:  # json.JSONDecodeError·requests JSONDecodeError 모두 ValueError 서브클래스
        raise RuntimeError(
            f"KRX fut_bydd_trd JSON parse 실패 basDd={bas_dd.isoformat()}: {type(e).__name__}"
        )
    return parse_krx_fut_response(data)


def make_krx_fetch_fn(
    auth_key: str, *, get_fn=requests.get, sleep_fn=time.sleep, now_fn=time.monotonic,
    min_interval_sec: float = KRX_DEFAULT_MIN_INTERVAL_SEC, timeout: float = KRX_DEFAULT_TIMEOUT,
):
    """throttle + 429 abort를 감싼 fetch_fn(date) 생성 (build_krx_manifest 주입용).

    throttle: 첫 요청 무throttle / 이후 요청 시작 간격 >= min_interval_sec (now_fn monotonic 기준).
      last_start는 fetch 직전 갱신 → 실패 요청도 간격에 포함(rapid retry 방지).
    429 → KrxRateLimitAbort 전파 (build_krx_manifest 미catch → run_krx_range_dry_run FETCH_ERROR + FAIL).
    get_fn/sleep_fn/now_fn 주입 → network·시간 0 테스트 (실제 KRX 호출은 운영 dry-run 별도 GO).
    """
    state = {"last_start": None}

    def fetch_fn(d: date) -> list[dict]:
        if state["last_start"] is not None:
            elapsed = now_fn() - state["last_start"]
            if elapsed < min_interval_sec:
                sleep_fn(min_interval_sec - elapsed)
        state["last_start"] = now_fn()  # fetch 직전 갱신 → 실패도 간격 포함
        return fetch_krx_fut_bydd_trd(d, auth_key, get_fn=get_fn, timeout=timeout)

    return fetch_fn


# ─────────────────────────────────────────────────────────────
# main / CLI wiring (Step 4B 단위 — range dry-run 진입점, DB read-only)
# ─────────────────────────────────────────────────────────────
#
# --range-dry-run 명시 플래그로만 실제 KRX HTTP 호출 (bare run = help, 실수 발사 방지).
# window/_date_arg/_resolve_range_dry_run_window는 KIS 스크립트 재사용 (DRY).
# read-only dry-run이라 write guard 없음 (KIS _run_range_dry_run_main과 동일 — guard는 --write 전용).
# AUTH_KEY는 KRX_OPENAPI_AUTH_KEY env에서만 로드 (로그/예외 미노출). 실제 호출은 운영 dry-run 별도 GO.

KRX_OPENAPI_AUTH_KEY_ENV = "KRX_OPENAPI_AUTH_KEY"


def _run_krx_range_dry_run_main(args) -> int:
    """--range-dry-run main wiring: env/window/db 준비 → run_krx_range_dry_run.

    DB write 0 (read-only dry-run, write guard 없음). 테스트는 run_krx_range_dry_run 직접 +
    이 함수의 config-fail 분기(env 미설정/one-sided window). return: exit code.
    """
    from backfill_kis_source_daily_rates import _resolve_range_dry_run_window

    auth_key = os.getenv(KRX_OPENAPI_AUTH_KEY_ENV)
    if not auth_key:
        print(f"[CONFIG 실패] {KRX_OPENAPI_AUTH_KEY_ENV} 미설정 (.env 확인)")
        return 1

    window = _resolve_range_dry_run_window(args)
    if window is None:
        print("[CONFIG 실패] --start-date/--end-date는 함께 지정 (또는 둘 다 생략 → today-1 rolling 1년)")
        return 2
    window_start, window_end = window
    if window_start > window_end:
        print(f"[CONFIG 실패] window_start({window_start}) > window_end({window_end})")
        return 2

    print("모드: KRX RANGE-DRY-RUN (DB write 0, read-only)")
    fetch_fn = make_krx_fetch_fn(auth_key, min_interval_sec=args.min_interval_sec)

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        return run_krx_range_dry_run(window_start, window_end, fetch_fn, db)
    finally:
        db.close()


def main() -> int:
    # 함수-레벨 import — KIS argparse validator 재사용 (parser 순수 테스트는 KIS 미로드).
    from backfill_kis_source_daily_rates import _date_arg

    parser = argparse.ArgumentParser(
        description="KRX OpenAPI fut_bydd_trd → source_daily_rates range dry-run (Step 4B, DB read-only).",
    )
    parser.add_argument(
        "--range-dry-run", action="store_true",
        help="KRX range dry-run 실행 (실제 fut_bydd_trd HTTP + DB read-only compare). "
             "미지정 시 help만 출력 (HTTP 호출 없음 — 실수 발사 방지).",
    )
    parser.add_argument(
        "--start-date", type=_date_arg, default=None,
        help="YYYY-MM-DD. --end-date와 함께 지정 (재현성). 둘 다 생략 시 today-1 rolling 1년.",
    )
    parser.add_argument(
        "--end-date", type=_date_arg, default=None,
        help="YYYY-MM-DD. --start-date와 함께 지정.",
    )
    parser.add_argument(
        "--min-interval-sec", type=float, default=KRX_DEFAULT_MIN_INTERVAL_SEC,
        help=f"throttle 요청 시작 간격 초 (default {KRX_DEFAULT_MIN_INTERVAL_SEC}). 운영 dry-run서 튜닝.",
    )

    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    if not args.range_dry_run:
        parser.print_help()
        return 0
    return _run_krx_range_dry_run_main(args)


if __name__ == "__main__":
    sys.exit(main())
