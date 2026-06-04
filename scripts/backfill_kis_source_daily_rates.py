#!/usr/bin/env python3
"""
KIS daily chain → source_daily_rates backfill.

ADR-034 Phase 2d: Step 2-3 (dry-run validator) → Step 3 (--write/--contract 적재)
→ Step 4B (N-contract 1년 chain + manifest partition + 기존 seed 비교 helper).

목적:
  - KIS inquire-daily-fuopchartprice REST endpoint 호출
  - contract chain: Step 3 = 3 contracts (previous + current + next),
    Step 4B = N-contract (coverage window를 덮는 front-month sequence — build_contract_sequence)
  - rollover boundary 검증 (만기일 07:00 KST swap point, GRAPH §7-new)
  - source_daily_rates row dict 생성 + validation suite + manifest partition gate (build_manifest)
  - --write로 DB 적재 (production guard + gap-only overlap). Step 4B main wiring은 단위 5 예정.

문서 anchor:
  - GRAPH_API_V2_CONTRACT.md §7-new: endpoint / TR_ID / A75YMM chain / rollover policy
    line 372-379 검증 사례: A75606 20260518 close=1496.5 (next contract user-facing)
  - DECISIONS.md ADR-033 Amendment 2: KIS daily chain 채택
  - app/sources/kis_master.py: `select_active_usd_futures_contract` (07:00 KST resolver)
  - app/crawlers/krx_kis.py: `KisAccessTokenManager` (REST Bearer, 1 day 1 token cache)

Chain 사용 구간 (rollover boundary 기준, Codex 정정):
  - C_i 사용 시작 = C_{i-1}.expiry_date (이전 contract 만기일, swap 시점)
  - C_i 사용 종료 = C_i.expiry_date - 1 day (만기 전일까지 — 만기일은 next contract)
  - validation: `previous_contract.expiry_date <= row.date_kst < current_contract.expiry_date`

Row mapping (KRX 패턴, Hana/Bithumb과 분리):
  - source = "krx", asset = "usd-krw-futures"
  - ohlc_quality = "source_ohlc" (KIS daily OHLC 완전)
  - close_basis = "krx_cf_close_1545"
  - source_method = "kis_daily_backfill"
  - contract_code = A75YMM (필수, Hana/Bithumb과 반대)
  - basis_date = None / published_at = None (Hana official 전용)

사용법:
  # dry-run validator (Step 2-3 탐색 — DB write X)
  python scripts/backfill_kis_source_daily_rates.py
  # 적재 (Step 3+ — --write + production guard; 상세 옵션은 --help)
  python scripts/backfill_kis_source_daily_rates.py --write --allow-production-write

주의:
  - KIS_APP_KEY / KIS_APP_SECRET 필요 (.env)
  - 기본 token cache: .cache/kis_access_token.json (운영 + smoke 공유)
  - KIS는 접근 토큰 1일 1회 발급 원칙 — fresh cache 재사용으로 추가 발급 0
  - dry-run은 DB write X. --write 진입 시 production guard (--allow-production-write 필요).
"""

# 표준 라이브러리
import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# 서드파티 라이브러리
import requests
from dotenv import load_dotenv

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 로컬 애플리케이션 (운영 코드 재사용으로 drift 최소화)
from app.crawlers.krx_kis import (  # noqa: E402
    ACCESS_TOKEN_CACHE_PATH,
    KIS_PROD_HOST,
    KisAccessTokenManager,
)
from app.sources.kis_master import (  # noqa: E402
    ContractInfo,
    _compute_expiry_date,
    fetch_commodity_future_master,
    parse_commodity_future_master,
    select_active_usd_futures_contract,
)


KST = ZoneInfo("Asia/Seoul")
DAILY_ENDPOINT = (
    f"{KIS_PROD_HOST}/uapi/domestic-futureoption/v1/quotations/inquire-daily-fuopchartprice"
)
TR_ID_DAILY = "FHKIF03020100"

# GRAPH §7-new anchor: previous expired contract (master에 없음, hardcoded)
PREVIOUS_HARDCODED_SHORT_CODE = "A75605"
PREVIOUS_HARDCODED_CONTRACT_MONTH = "202605"

# 100건 cap 안전 range (영업일 ~75일 = calendar ~105일)
SAFE_FETCH_DAYS = 105
KIS_DAILY_ROWS_CAP = 100  # 5/27 실측 — wide range 시 정확히 100 rows cap


# ─────────────────────────────────────────────────────────────
# Chain 구성
# ─────────────────────────────────────────────────────────────

def _build_previous_hardcoded() -> ContractInfo:
    """GRAPH §7-new anchor 기반 hardcoded previous contract.

    A75605 = 2026-05 만기 (셋째 월요일 = 2026-05-18). master에는 없으나 KIS daily
    endpoint로 historical fetch 가능. GRAPH §7-new line 372-379 검증 사례 재현용.
    """
    expiry = _compute_expiry_date(PREVIOUS_HARDCODED_CONTRACT_MONTH)
    if expiry is None:
        raise RuntimeError("previous hardcoded expiry compute 실패")
    return ContractInfo(
        short_code=PREVIOUS_HARDCODED_SHORT_CODE,
        standard_code="",  # 미사용
        name=f"미국달러 F {PREVIOUS_HARDCODED_CONTRACT_MONTH}",
        contract_month=PREVIOUS_HARDCODED_CONTRACT_MONTH,
        expiry_date=expiry,
    )


def _build_previous_dynamic(current: ContractInfo) -> ContractInfo:
    """current 기준 previous contract 동적 계산 (운영 영속성, Codex Blocker 4 정정).

    current.contract_month YYYYMM → previous YYYYMM 계산 (1월이면 전년 12월).
    short_code 패턴: A75 + (year % 10) + month_2digit (GRAPH §7-new line 336 명시).
    expiry_date: _compute_expiry_date 재사용 (셋째 월요일).

    master에 없는 만기 contract (지난 contract)도 KIS daily endpoint로 fetch 가능.
    """
    year = int(current.contract_month[:4])
    month = int(current.contract_month[4:6])
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    prev_contract_month = f"{prev_year:04d}{prev_month:02d}"
    year_last_digit = prev_year % 10
    prev_short_code = f"A75{year_last_digit}{prev_month:02d}"
    expiry = _compute_expiry_date(prev_contract_month)
    if expiry is None:
        raise RuntimeError(f"previous expiry compute 실패: {prev_contract_month}")
    return ContractInfo(
        short_code=prev_short_code,
        standard_code="",  # master 미포함
        name=f"미국달러 F {prev_contract_month}",
        contract_month=prev_contract_month,
        expiry_date=expiry,
    )


def _build_before_previous_dynamic(previous: ContractInfo) -> ContractInfo:
    """previous 기준 before-previous (previous - 1 month) 동적 계산.

    previous A75605의 user-facing 사용 시작 anchor = before_previous(A75604).expiry_date.
    SAFE_FETCH_DAYS 임의 range는 100건 cap 회피만 보장, user-facing chain mapping
    정확성 보장 X — Codex Blocker 정정 (Round 6).
    """
    year = int(previous.contract_month[:4])
    month = int(previous.contract_month[4:6])
    if month == 1:
        bp_year, bp_month = year - 1, 12
    else:
        bp_year, bp_month = year, month - 1
    bp_contract_month = f"{bp_year:04d}{bp_month:02d}"
    year_last_digit = bp_year % 10
    bp_short_code = f"A75{year_last_digit}{bp_month:02d}"
    expiry = _compute_expiry_date(bp_contract_month)
    if expiry is None:
        raise RuntimeError(f"before-previous expiry compute 실패: {bp_contract_month}")
    return ContractInfo(
        short_code=bp_short_code,
        standard_code="",
        name=f"미국달러 F {bp_contract_month}",
        contract_month=bp_contract_month,
        expiry_date=expiry,
    )


def _select_next_contract(contracts, current: ContractInfo) -> ContractInfo:
    """master에서 current 다음 expiry contract 선택."""
    future_after = sorted(
        [c for c in contracts if c.is_usd_krw_futures() and c.expiry_date > current.expiry_date],
        key=lambda c: c.expiry_date,
    )
    if not future_after:
        raise RuntimeError("next future contract 없음 (current 만료된 master?)")
    return future_after[0]


def build_chain_dynamic(now_kst: datetime) -> tuple[ContractInfo, ContractInfo, ContractInfo]:
    """**기본 모드** — current 기준 dynamic chain (운영 영속성).

    previous는 current.contract_month - 1 동적 계산. 시간 흐름 시 자동 갱신.
    """
    raw = fetch_commodity_future_master()
    contracts = parse_commodity_future_master(raw)
    current = select_active_usd_futures_contract(contracts, now_kst)
    if current is None:
        raise RuntimeError("master에서 active USD futures contract 없음")
    previous = _build_previous_dynamic(current)
    next_c = _select_next_contract(contracts, current)
    return previous, current, next_c


def build_chain_static_anchor(now_kst: datetime) -> tuple[ContractInfo, ContractInfo, ContractInfo]:
    """**--known-boundary-smoke 모드** — GRAPH §7-new line 372-379 검증 사례 재현 전용.

    previous = A75605 hardcoded (2026-05-18 만기). current=A75606인 시점에만 정합.
    문서 anchor 재현 용도이며 시간이 지나면 dynamic 모드로 전환 필요.
    """
    raw = fetch_commodity_future_master()
    contracts = parse_commodity_future_master(raw)
    current = select_active_usd_futures_contract(contracts, now_kst)
    if current is None:
        raise RuntimeError("master에서 active USD futures contract 없음")
    if current.short_code != "A75606":
        raise RuntimeError(
            f"--known-boundary-smoke는 current=A75606 시점 전용 (현재 current={current.short_code}). "
            "시간 흐름 시 기본 dynamic 모드 사용 권장."
        )
    previous = _build_previous_hardcoded()  # A75605
    next_c = _select_next_contract(contracts, current)
    return previous, current, next_c


# ─────────────────────────────────────────────────────────────
# Coverage window + N-contract sequence (Step 4B — calendar 순수 계산)
# ─────────────────────────────────────────────────────────────
#
# Step 4B는 1년 coverage window를 덮는 front-month contract sequence를 만든다.
# 핵심 invariant (KRX_STEP4B_PLAN.md §12 Resolved):
#   - segment = [prev.expiry, this.expiry) 반열린 (calendar date, 거래일 무관)
#   - window_start가 속한 front-month resolve (만기일 당일 = next contract)
#   - 포함 조건: segment ∩ [window_start, window_end] != ∅
# master / fetch / DB 의존 0 — _compute_expiry_date(셋째 월요일)만 재사용.

def _parse_contract_month(contract: ContractInfo) -> tuple[int, int]:
    """ContractInfo.contract_month 'YYYYMM' → (year, month)."""
    return int(contract.contract_month[:4]), int(contract.contract_month[4:6])


def _prev_month(year: int, month: int) -> tuple[int, int]:
    """이전 월 (1월 → 전년 12월)."""
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _next_month(year: int, month: int) -> tuple[int, int]:
    """다음 월 (12월 → 다음해 1월)."""
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _make_usd_futures_contract(year: int, month: int) -> ContractInfo:
    """year/month → 미국달러선물 ContractInfo (calendar 계산, master 불필요).

    short_code 패턴: A75 + (year % 10) + month_2digit (GRAPH §7-new line 336).
    expiry_date: _compute_expiry_date 재사용 (셋째 월요일).
    standard_code는 "" (미사용) — 만기 지난 contract도 KIS daily endpoint로 fetch 가능.
    기존 _build_previous_dynamic / _build_before_previous_dynamic과 동일 short_code 규칙
    (후속에서 그 helper들을 본 함수 기반으로 통합 가능 — 이번 단위는 변경 최소).
    """
    contract_month = f"{year:04d}{month:02d}"
    short_code = f"A75{year % 10}{month:02d}"
    expiry = _compute_expiry_date(contract_month)
    if expiry is None:
        raise RuntimeError(f"expiry compute 실패: {contract_month}")
    return ContractInfo(
        short_code=short_code,
        standard_code="",
        name=f"미국달러 F {contract_month}",
        contract_month=contract_month,
        expiry_date=expiry,
    )


def _resolve_front_month(target_date: date) -> ContractInfo:
    """target_date가 속한 front-month contract resolve (calendar 기준).

    front-month = target_date 이후 첫 만기 월물:
      - target_date <  expiry(M) → M월물
      - target_date >= expiry(M) (만기일 당일 포함) → 다음 월물 (boundary=next, invariant 2)

    target_date가 휴장일이어도 segment는 calendar date 기준으로 resolve된다
    (실제 row 존재는 KIS trading-day set이 결정 — 2-layer 분리, §12 Resolved).
    """
    year, month = target_date.year, target_date.month
    candidate = _make_usd_futures_contract(year, month)
    if target_date < candidate.expiry_date:
        return candidate
    ny, nm = _next_month(year, month)
    return _make_usd_futures_contract(ny, nm)


def _compute_coverage_window(now_kst: datetime) -> tuple[date, date]:
    """today-1 기준 rolling 1년 window 산정 (§12 Resolved item 1).

    window_end   = today_kst - 1 day (today 제외 — intraday close 오염 회피).
    window_start = window_end 의 1년 전 (year-1 동일 월일, 2/29는 2/28 보정).
    "rolling"은 산정 방식이며, 호출자가 반환된 (start, end)를 manifest/log에 고정 기록한다.

    window 길이 = **calendar-year lookback** (window_start ~ window_end inclusive,
    ~366일). "정확히 N daily bucket"이 아니다 — KRX는 거래일만 적재하므로 실제 row 수는
    KIS 거래일 집합이 결정한다 (calendar 366 vs 365 차이는 시작 경계 거래일 0~1개에만 영향).
    """
    today = now_kst.date()
    window_end = today - timedelta(days=1)
    try:
        window_start = window_end.replace(year=window_end.year - 1)
    except ValueError:  # window_end == 2/29 → 전년 2/28
        window_start = window_end.replace(year=window_end.year - 1, day=28)
    return window_start, window_end


def build_contract_sequence(
    window_start: date, window_end: date
) -> list[tuple[ContractInfo, date, date]]:
    """coverage window를 덮는 front-month contract sequence (calendar 순수 계산).

    반환 각 항목: (contract, segment_start, segment_end)
      - segment = [segment_start, segment_end) 반열린 (calendar date).
      - segment_start = 이전 월물 expiry (= 이 contract 사용 시작).
      - segment_end   = 이 contract expiry (= 다음 contract 사용 시작, 미포함).

    window_start가 속한 front-month부터 forward로 생성하며,
    `segment ∩ [window_start, window_end] != ∅`인 contract만 포함한다
    (forward라 segment_start 단조 증가 → 종료조건 = segment_start <= window_end).

    DB write / overlap 비교 / production guard와 무관한 순수 계산 (Step 4B 단위 1).
    """
    if window_start > window_end:
        raise ValueError(
            f"window_start({window_start}) > window_end({window_end}) — 역전 range"
        )

    sequence: list[tuple[ContractInfo, date, date]] = []
    current = _resolve_front_month(window_start)
    cy, cm = _parse_contract_month(current)
    # C_0 segment_start = 이전 월물 expiry (window_start ∈ [segment_start, current.expiry))
    py, pm = _prev_month(cy, cm)
    prev_expiry = _compute_expiry_date(f"{py:04d}{pm:02d}")
    if prev_expiry is None:
        raise RuntimeError(f"prev expiry compute 실패: {py:04d}{pm:02d}")
    segment_start = prev_expiry

    while segment_start <= window_end:
        segment_end = current.expiry_date  # 반열린 — segment_end는 다음 contract 소속
        sequence.append((current, segment_start, segment_end))
        # forward: 다음 월물, 다음 segment_start = 이번 contract expiry
        ny, nm = _next_month(cy, cm)
        next_contract = _make_usd_futures_contract(ny, nm)
        segment_start = current.expiry_date
        current, cy, cm = next_contract, ny, nm

    return sequence


# ─────────────────────────────────────────────────────────────
# Manifest builder (Step 4B 단위 2 — segment-clip fetch + partition gate)
# ─────────────────────────────────────────────────────────────
#
# build_contract_sequence() 결과를 받아 각 segment를 fetch range로 clip해 fetch하고,
# manifest rows + hard_issues(fail-close 대상) + warnings(surface) + boundary_samples를 만든다.
# fetch_fn 주입 → network 의존 격리 (테스트는 mock payload).
# DB write / overlap 비교 / production guard는 변경하지 않는다 (단위 4·5 영역).

GAP_SURFACE_THRESHOLD_DAYS = 4  # 연휴(설/추석)는 정상 4일+ gap → hard 아닌 surface (§12 item 3)


@dataclass
class ManifestResult:
    """build_manifest 산출물.

    rows: 검증 통과한 manifest row dict (date_kst 오름차순). hard_issues가 비어 있을 때
        dup 0 = strictly increasing (hard issue 있으면 dup row가 rows에 남아 있을 수 있음).
    hard_issues: fail-close 대상 (호출자 — 단위 5 --range-dry-run — 이 비어야 write 진입).
    warnings: gap surface (gap ≥4일 등 — 연휴 정상, 항상 surface).
    boundary_samples: segment 경계(±1일) 주변 row (만기일=next 등 boundary 검증용 surface).
    cap_warnings: cap 100 도달 — write-intended path에서 hard 승격 대상
        (promote_write_intended_manifest_warnings). 탐색 dry-run은 surface 유지.
    """
    rows: list[dict]
    hard_issues: list[str]
    warnings: list[str]
    boundary_samples: list[dict]
    cap_warnings: list[str] = field(default_factory=list)


def _segment_fetch_range(
    segment_start: date, segment_end: date, window_start: date, window_end: date
) -> tuple[date, date]:
    """segment ∩ window → fetch range (inclusive).

    fetch_start = max(segment_start, window_start)
    fetch_end   = min(segment_end - 1day, window_end)
      └ segment_end(만기일)는 next contract 소속이므로 -1day로 제외 (boundary=next 자연 유지).

    sequence 포함 조건(segment ∩ window != ∅)이면 fetch_start <= fetch_end 항상 성립.
    """
    fetch_start = max(segment_start, window_start)
    fetch_end = min(segment_end - timedelta(days=1), window_end)
    return fetch_start, fetch_end


def build_manifest(
    sequence: list[tuple[ContractInfo, date, date]],
    window_start: date,
    window_end: date,
    fetch_fn,
) -> ManifestResult:
    """sequence를 segment-clip fetch → manifest (Step 4B 단위 2).

    fetch_fn(contract, fetch_start, fetch_end) -> list[row dict]
      (실제: parse_daily_rows(fetch_daily(...)) wrapping / 테스트: mock payload).

    Hard fail (issues 수집 → 호출자 fail-close):
      - 만기일 row가 자기(previous) segment에 존재 (boundary=next 위반)
      - row가 segment [seg_start, seg_end) 밖
      - row가 window [window_start, window_end] 밖
      - manifest date_kst duplicate (validate_duplicates_mapped 재사용)
    Surface (warnings):
      - gap >= GAP_SURFACE_THRESHOLD_DAYS (연휴/KIS 결손 확인용)
      - fetched rows >= KIS_DAILY_ROWS_CAP (단위 4에서 write-intended fail-close 격상)
    """
    rows: list[dict] = []
    hard_issues: list[str] = []
    warnings: list[str] = []
    boundary_samples: list[dict] = []
    cap_warnings: list[str] = []

    for contract, seg_start, seg_end in sequence:
        fetch_start, fetch_end = _segment_fetch_range(seg_start, seg_end, window_start, window_end)
        if fetch_start > fetch_end:  # sequence 포함 조건상 발생 안 함 (방어)
            hard_issues.append(
                f"{contract.short_code}: empty fetch range [{fetch_start}, {fetch_end}]"
            )
            continue

        fetched = fetch_fn(contract, fetch_start, fetch_end)
        if len(fetched) >= KIS_DAILY_ROWS_CAP:
            cap_warnings.append(
                f"{contract.short_code}: {len(fetched)} rows >= cap {KIS_DAILY_ROWS_CAP} "
                "(range split 필요 — surface; write-intended path에서 promote_*로 fail-close 격상)"
            )

        for row in fetched:
            d = row["date_kst"]
            rc = row.get("contract_code")
            if rc != contract.short_code:  # (date_kst → contract_code) invariant — fetch_fn 주입 방어
                hard_issues.append(
                    f"{contract.short_code} date_kst={d} contract_code mismatch: row has {rc!r}"
                )
                continue
            if d == seg_end:  # 만기일 = next contract 소속 (boundary=next 위반)
                hard_issues.append(
                    f"{contract.short_code} date_kst={d} == segment_end "
                    "(boundary 위반: 만기일은 next contract 소속)"
                )
                continue
            if not (seg_start <= d < seg_end):
                hard_issues.append(
                    f"{contract.short_code} date_kst={d} segment [{seg_start}, {seg_end}) 밖"
                )
                continue
            if not (window_start <= d <= window_end):
                hard_issues.append(
                    f"{contract.short_code} date_kst={d} window [{window_start}, {window_end}] 밖"
                )
                continue
            rows.append(row)
            if (d - seg_start).days <= 1 or (seg_end - d).days <= 1:  # segment 경계 ±1일
                boundary_samples.append(
                    {"contract": contract.short_code, "date_kst": d, "close": row.get("close")}
                )

    rows.sort(key=lambda r: r["date_kst"])
    # sorted + dup 0 = strictly increasing (별도 정렬 검증 불요 — DRY)
    hard_issues.extend(validate_duplicates_mapped(rows))

    dates = [r["date_kst"] for r in rows]
    for i in range(len(dates) - 1):
        gap = (dates[i + 1] - dates[i]).days
        if gap >= GAP_SURFACE_THRESHOLD_DAYS:
            warnings.append(
                f"gap {gap}d: {dates[i]} -> {dates[i + 1]} (연휴/KIS 결손 확인 — surface)"
            )

    return ManifestResult(rows, hard_issues, warnings, boundary_samples, cap_warnings)


def promote_write_intended_manifest_warnings(result: ManifestResult) -> ManifestResult:
    """write-intended path(`--range-dry-run` 게이트 + `--write`): cap_warnings를 hard_issues로 승격.

    cap 100 도달은 usage segment(≈20거래일) 기준 정상 불가 → 설계/endpoint 이상 신호이므로
    write-intended path에서는 fail-close (하나라도 도달 시 전체 abort 대상).
    - 기존 hard_issues 보존 + cap_warnings를 hard로 추가
    - gap warnings(surface)는 그대로 유지 (연휴 정상)
    - cap_warnings도 audit용으로 유지 (어떤 게 격상됐는지 추적)
    순수 탐색 dry-run(Step 2-3 probe)은 본 함수를 호출하지 않음 → cap이 surface로 남음 (동작 불변).
    """
    if not result.cap_warnings:
        return result
    return ManifestResult(
        rows=result.rows,
        hard_issues=result.hard_issues
        + [f"[write-intended cap 격상] {w}" for w in result.cap_warnings],
        warnings=result.warnings,
        boundary_samples=result.boundary_samples,
        cap_warnings=result.cap_warnings,
    )


# ─────────────────────────────────────────────────────────────
# Existing seed comparison (Step 4B 단위 3 — DB read-only 비교 + write target 산출)
# ─────────────────────────────────────────────────────────────
#
# full manifest와 기존 KRX seed를 비교해 분류 (KRX_STEP4B_PLAN.md §6·§7 + ADR-034 §11 source 전환):
#   ① manifest only             → rows_to_write
#   ② manifest + DB 완전 일치    → matched_existing (skip write, source_method 동일)
#   ②' manifest + DB 값 동일하나 source_method만 krx_openapi_daily(manifest)↔kis_daily_backfill(db)
#                               → transitional_source_method_matches (skip write, migration 대기)
#   ③ manifest + DB 충돌(값/contract/null/metadata, 또는 그 외/역방향 source_method)
#                               → hard (기존 seed = ground truth, manifest 의심)
#   ④ DB(window 안) only         → hard (manifest가 검증된 seed 미설명 = coverage 불완전)
# write / upsert / transaction / main wiring 없음 (단위 4·5 영역).

# source_method는 literal 동일 비교에서 빼고 전환 분기로 별도 처리 (값충돌이 sm 전환에 가려지지 않도록).
_COMPARE_LITERAL_KEYS = ("contract_code", "close_basis", "ohlc_quality")
_COMPARE_DECIMAL_KEYS = ("rate", "high", "low", "close")
_COMPARE_NULL_KEYS = ("basis_date", "published_at")
_COMPARE_METADATA_IDENTITY = ("contract_short_code", "contract_month", "contract_expiry_date")
# 전환쌍 (manifest/new, existing/db) — kis→krx 마이그레이션 대기. 이 방향만 transitional, 역방향·그 외는 hard.
_TRANSITION_SOURCE_METHOD = ("krx_openapi_daily", "kis_daily_backfill")


@dataclass
class CompareResult:
    """compare_manifest_with_existing 산출물.

    rows_to_write: manifest only (DB에 없음) → 단위 5가 gap-only write.
    matched_existing: manifest + DB 완전 일치 (exact, source_method 동일) → skip.
    transitional_source_method_matches: 값 전부 동일하나 source_method만
        krx_openapi_daily(manifest)↔kis_daily_backfill(db) (kis→krx 전환 대기) → skip write.
        exact과 분리해 migration 전/후 상태를 dry-run 출력에 보존 (둘을 섞으면 count가 동일해져 신호 소실).
    hard_issues: ③ 충돌(값/contract/null/metadata, 그 외·역방향 sm) / ④ orphan / window 밖 (fail-close).
    warnings: metadata.open 누락·mismatch 등 surface.
    """
    rows_to_write: list[dict]
    matched_existing: list[dict]
    transitional_source_method_matches: list[dict]
    hard_issues: list[str]
    warnings: list[str]


def _norm_decimal(value):
    """float(row_to_dict) / Decimal(manifest) / str → Decimal 정규화 (None 보존).

    row_to_dict는 Numeric(14,6)을 float로 변환하므로, manifest Decimal과 비교하려면
    양쪽을 str 경유 Decimal로 정규화한다 (1496.5 == 1496.500000, 문자열 비교 금지 §7).
    """
    if value is None:
        return None
    return Decimal(str(value))


def _compare_existing_row(manifest_row: dict, existing_row: dict) -> list[str]:
    """단일 (date_kst 동일) row 비교 → 충돌 issue list (빈 = 일치). open은 surface 별도."""
    issues: list[str] = []
    d = manifest_row["date_kst"]
    for k in _COMPARE_LITERAL_KEYS:
        if manifest_row.get(k) != existing_row.get(k):
            issues.append(
                f"date_kst={d} {k} mismatch: manifest={manifest_row.get(k)!r} db={existing_row.get(k)!r}"
            )
    for k in _COMPARE_DECIMAL_KEYS:
        if _norm_decimal(manifest_row.get(k)) != _norm_decimal(existing_row.get(k)):
            issues.append(
                f"date_kst={d} {k} numeric mismatch: manifest={manifest_row.get(k)} db={existing_row.get(k)}"
            )
    for k in _COMPARE_NULL_KEYS:
        if manifest_row.get(k) is not None or existing_row.get(k) is not None:
            issues.append(
                f"date_kst={d} {k} must be None: manifest={manifest_row.get(k)!r} db={existing_row.get(k)!r}"
            )
    m_meta = manifest_row.get("metadata_json") or {}
    e_meta = existing_row.get("metadata_json") or {}
    for k in _COMPARE_METADATA_IDENTITY:
        mv, ev = m_meta.get(k), e_meta.get(k)
        if mv is None or ev is None:
            issues.append(f"date_kst={d} metadata.{k} 누락 (manifest={mv!r} db={ev!r}) — identity hard")
        elif mv != ev:
            issues.append(f"date_kst={d} metadata.{k} mismatch: manifest={mv!r} db={ev!r}")
    return issues


def _compare_open_warning(manifest_row: dict, existing_row: dict) -> list[str]:
    """metadata.open 비교 (누락·mismatch 모두 surface — §7 open 약한 비교)."""
    d = manifest_row["date_kst"]
    m_open = (manifest_row.get("metadata_json") or {}).get("open")
    e_open = (existing_row.get("metadata_json") or {}).get("open")
    if m_open is None or e_open is None:
        return [f"date_kst={d} metadata.open 누락 (surface): manifest={m_open!r} db={e_open!r}"]
    if _norm_decimal(m_open) != _norm_decimal(e_open):
        return [f"date_kst={d} metadata.open mismatch (surface): manifest={m_open} db={e_open}"]
    return []


def compare_manifest_with_existing(
    manifest_rows: list[dict],
    existing_rows: list[dict],
    window_start: date,
    window_end: date,
) -> CompareResult:
    """full manifest × 기존 KRX seed → 분류 (①/②/②'transitional/③/④, 순수 비교, DB 의존 0).

    existing_rows는 source=krx / asset=usd-krw-futures / date_kst ∈ window로 제한된
    결과 전제 (query_existing_krx_rows). compare는 window를 defensive 재확인한다.
    """
    rows_to_write: list[dict] = []
    matched_existing: list[dict] = []
    transitional_source_method_matches: list[dict] = []
    hard_issues: list[str] = []
    warnings: list[str] = []

    # dict 색인 시 duplicate date 방어 (입력 전제 신뢰 X — Crash Early, 단위 2 contract_code 방어와 동일).
    m_by_date: dict = {}
    for r in manifest_rows:
        d = r["date_kst"]
        if d in m_by_date:
            hard_issues.append(f"manifest duplicate date_kst={d} (입력 전제 위반 — defensive hard)")
        m_by_date[d] = r
    e_by_date: dict = {}
    for r in existing_rows:
        d = r["date_kst"]
        if d in e_by_date:
            hard_issues.append(f"existing duplicate date_kst={d} (DB unique 위반 의심 — defensive hard)")
        e_by_date[d] = r

    for d, m_row in m_by_date.items():
        e_row = e_by_date.get(d)
        if e_row is None:
            rows_to_write.append(m_row)  # ① manifest only
            continue
        conflicts = _compare_existing_row(m_row, e_row)  # source_method 제외 (전환 분기로 별도)
        warnings.extend(_compare_open_warning(m_row, e_row))
        if conflicts:
            hard_issues.extend(conflicts)  # ③ 값/contract/null/metadata 충돌 → hard
            continue
        # 나머지 전 필드 일치 (conflicts 없음) — source_method 관계로만 분기.
        # 전환쌍이 값충돌을 가리는 통로 차단: 다른 필드가 달랐다면 위 conflicts에서 이미 hard로 빠짐.
        m_sm, e_sm = m_row.get("source_method"), e_row.get("source_method")
        if m_sm == e_sm:
            matched_existing.append(e_row)  # ② exact 일치 → skip
        elif (m_sm, e_sm) == _TRANSITION_SOURCE_METHOD:
            transitional_source_method_matches.append(e_row)  # ②' kis→krx 전환 대기 → skip (write 아님)
        else:
            hard_issues.append(  # 역방향(kis manifest / krx db)·그 외 sm mismatch → hard
                f"date_kst={d} unexpected source_method mismatch (전환쌍 아님 — 허용은 "
                f"manifest=krx_openapi_daily ∧ db=kis_daily_backfill만): manifest={m_sm!r} db={e_sm!r}"
            )

    for d, e_row in e_by_date.items():
        if not (window_start <= d <= window_end):  # query 제한 위반 방어 (Crash Early)
            hard_issues.append(
                f"existing date_kst={d} window [{window_start}, {window_end}] 밖 "
                "— query 제한(source/asset/date_kst) 확인"
            )
            continue
        if d not in m_by_date:  # ④ DB only within window → orphan hard
            hard_issues.append(
                f"date_kst={d} DB(window 안)에 존재하나 manifest에 없음 "
                "— manifest coverage가 기존 seed 미설명 (orphan, hard)"
            )

    return CompareResult(
        rows_to_write, matched_existing, transitional_source_method_matches, hard_issues, warnings
    )


def query_existing_krx_rows(db, window_start: date, window_end: date) -> list[dict]:
    """기존 KRX rows read-only 조회 (source/asset/date_kst 제한 — 비교 오염 방지).

    DB read-only. row_to_dict(Numeric→float) 변환. write/transaction 없음.
    """
    # 함수 내 import — 무거운 ORM + 단위 1·2 순수 테스트 격리 (app.models 미로드)
    from app.models import SourceDailyRate
    from app.source_daily_rates import row_to_dict

    rows = (
        db.query(SourceDailyRate)
        .filter(
            SourceDailyRate.source == "krx",
            SourceDailyRate.asset == "usd-krw-futures",
            SourceDailyRate.date_kst >= window_start,
            SourceDailyRate.date_kst <= window_end,
        )
        .order_by(SourceDailyRate.date_kst)
        .all()
    )
    # row_to_dict는 date_kst를 v2 JSON용 isoformat str로 변환 → compare는 date object 필요.
    # date_kst만 date로 복원 (window 비교 + dict key 매칭). Numeric→float 등 나머지는 재사용.
    result = []
    for r in rows:
        rd = row_to_dict(r)
        rd["date_kst"] = r.date_kst
        result.append(rd)
    return result


# ─────────────────────────────────────────────────────────────
# Range dry-run 게이트 (Step 4B 단위 5 — write-intended, DB write 0)
# ─────────────────────────────────────────────────────────────
#
# sequence → manifest → promote(cap hard) → (manifest hard 없으면) compare.
# manifest hard 있으면 compare skip (오염 manifest 비교 = secondary noise 회피, fail-close).
# DB write/upsert/transaction 0 — compare의 query_existing SELECT만. 출력은 운영자 파싱용 sentinel.

def _make_fetch_fn(token: str, app_key: str, app_secret: str):
    """build_manifest 주입용 fetch_fn — parse_daily_rows(fetch_daily(...)) wrapping."""
    def fetch_fn(contract: ContractInfo, fetch_start: date, fetch_end: date) -> list[dict]:
        payload = fetch_daily(
            short_code=contract.short_code, start_date=fetch_start, end_date=fetch_end,
            token=token, app_key=app_key, app_secret=app_secret,
        )
        return parse_daily_rows(payload, contract)
    return fetch_fn


def _emit_compare_and_gate(manifest_rows: list[dict], window_start: date, window_end: date, db) -> int:
    """manifest rows × 기존 KRX seed compare 출력 + 게이트 (KIS·KRX dry-run 공유).

    query_existing_krx_rows(source=krx)는 KIS·KRX manifest 모두 동일 대상 → 단일화로
    PASS_WITH_TRANSITIONAL 게이트 정책이 한 곳에만 존재(drift 차단). 호출자(run_*_range_dry_run)는
    manifest hard 없을 때만 진입. 출력 순서는 기존 KIS 로그와 동일(운영 diff 0).
    return: exit code (compare hard → 1 / else 0). DB write 0 — query SELECT만.
    """
    existing = query_existing_krx_rows(db, window_start, window_end)
    compare = compare_manifest_with_existing(manifest_rows, existing, window_start, window_end)
    print("COMPARE_STATUS=ran")
    print(f"EXISTING_ROW_COUNT={len(existing)}")
    print(f"ROWS_TO_WRITE_COUNT={len(compare.rows_to_write)}")
    print(f"MATCHED_EXISTING_COUNT={len(compare.matched_existing)}")  # exact-only (source_method 동일)
    print(f"TRANSITIONAL_SOURCE_METHOD_MATCH_COUNT={len(compare.transitional_source_method_matches)}")
    print(f"COMPARE_HARD_COUNT={len(compare.hard_issues)}")
    print(
        "ROWS_TO_WRITE_DATES="
        + json.dumps([r["date_kst"].isoformat() for r in compare.rows_to_write])
    )
    for w in compare.warnings:
        print(f"  [compare_warning] {w}")
    for h in compare.hard_issues:
        print(f"  [COMPARE_HARD] {h}")

    if compare.hard_issues:
        print("RANGE_DRY_RUN_RESULT=FAIL")
        return 1
    if compare.transitional_source_method_matches:
        # hard 0이지만 provenance 정리(kis→krx migration) 대기 — silent PASS로 묻지 않음.
        print("RANGE_DRY_RUN_RESULT=PASS_WITH_TRANSITIONAL")
        return 0
    print("RANGE_DRY_RUN_RESULT=PASS")
    return 0


def run_range_dry_run(window_start: date, window_end: date, fetch_fn, db) -> int:
    """write-intended dry-run 게이트 (Step 4B 단위 5). return: exit code (hard 있으면 1).

    manifest hard(boundary/segment/window/dup/cap promote 등) 있으면 compare skip + exit 1
    (오염 manifest를 DB seed와 비교하면 secondary noise — fail-close).
    DB write 0 — compare의 query_existing SELECT만.
    """
    print(f"WINDOW_START={window_start.isoformat()}")
    print(f"WINDOW_END={window_end.isoformat()}")
    sequence = build_contract_sequence(window_start, window_end)
    print(f"SEQUENCE_CONTRACTS={json.dumps([c.short_code for c, _, _ in sequence])}")
    for c, ss, se in sequence:
        print(f"  segment {c.short_code}: [{ss.isoformat()}, {se.isoformat()})")

    try:
        manifest = build_manifest(sequence, window_start, window_end, fetch_fn)
    except (requests.RequestException, RuntimeError, ValueError, InvalidOperation) as e:
        print(f"FETCH_ERROR={type(e).__name__}: {e}")
        print("RANGE_DRY_RUN_RESULT=FAIL")
        return 1
    manifest = promote_write_intended_manifest_warnings(manifest)
    print(f"MANIFEST_ROW_COUNT={len(manifest.rows)}")
    print(f"MANIFEST_HARD_COUNT={len(manifest.hard_issues)}")
    print(f"WARNINGS_COUNT={len(manifest.warnings)}")
    print(f"CAP_WARNINGS_COUNT={len(manifest.cap_warnings)}")
    print(f"BOUNDARY_SAMPLES_COUNT={len(manifest.boundary_samples)}")
    for w in manifest.warnings:
        print(f"  [warning] {w}")
    for w in manifest.cap_warnings:
        print(f"  [cap_warning] {w}")
    for h in manifest.hard_issues:
        print(f"  [HARD] {h}")

    if manifest.hard_issues:
        print("COMPARE_STATUS=skipped_due_to_manifest_hard")
        print("RANGE_DRY_RUN_RESULT=FAIL")
        return 1

    return _emit_compare_and_gate(manifest.rows, window_start, window_end, db)


def _resolve_range_dry_run_window(args):
    """window 산정: --start/--end 고정 or today-1 rolling 1년 (§12 Resolved item 1).

    args.start_date / args.end_date는 type=_date_arg라 **이미 date 객체** (str 아님 —
    date.fromisoformat 적용 금지). return: (window_start, window_end) or None (한쪽만 지정 → fail-close).
    """
    if args.start_date and args.end_date:
        return args.start_date, args.end_date
    if args.start_date or args.end_date:
        return None
    now_kst = datetime.now(tz=KST).replace(tzinfo=None)
    return _compute_coverage_window(now_kst)


def _run_range_dry_run_main(args) -> int:
    """--range-dry-run main wiring: window/token/db 준비 → run_range_dry_run (테스트는 run_range_dry_run 직접).

    window: --start-date/--end-date 있으면 고정 (재현성), 없으면 today-1 rolling 1년 (§12 Resolved item 1).
    DB write 0.
    """
    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        print("[CONFIG 실패] KIS_APP_KEY / KIS_APP_SECRET 미설정 (.env 확인)")
        return 1

    window = _resolve_range_dry_run_window(args)
    if window is None:
        print("[CONFIG 실패] --start-date/--end-date는 함께 지정 (또는 둘 다 생략 → today-1 rolling 1년)")
        return 2
    window_start, window_end = window

    if window_start > window_end:
        print(f"[CONFIG 실패] window_start({window_start}) > window_end({window_end})")
        return 2

    print(f"모드: RANGE-DRY-RUN (write-intended 게이트, DB write 0)")
    try:
        token = get_access_token(app_key, app_secret, args.token_cache_path)
    except Exception as e:
        print(f"[TOKEN 실패] {type(e).__name__}: {e}")
        return 1
    fetch_fn = _make_fetch_fn(token, app_key, app_secret)

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        return run_range_dry_run(window_start, window_end, fetch_fn, db)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# Fetch + Parse
# ─────────────────────────────────────────────────────────────

def fetch_daily(
    *,
    short_code: str,
    start_date: date,
    end_date: date,
    token: str,
    app_key: str,
    app_secret: str,
    timeout: float = 15.0,
) -> dict:
    """KIS inquire-daily-fuopchartprice REST GET → JSON payload.

    Returns: response.json() (rt_cd / output1 / output2 등 포함)
    """
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": TR_ID_DAILY,
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "CF",
        "FID_INPUT_ISCD": short_code,
        "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
    }
    response = requests.get(DAILY_ENDPOINT, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    rt_cd = payload.get("rt_cd")
    if rt_cd != "0":
        raise RuntimeError(
            f"KIS 비정상 rt_cd={rt_cd} msg_cd={payload.get('msg_cd')} msg1={payload.get('msg1')}"
        )
    return payload


def parse_daily_rows(payload: dict, contract: ContractInfo) -> list[dict]:
    """payload.output2 array → source_daily_rates row dict list.

    output2 fields: stck_bsop_date / futs_oprc / futs_hgpr / futs_lwpr / futs_prpr / mod_yn
    """
    output2 = payload.get("output2") or []
    if not isinstance(output2, list):
        raise ValueError(f"output2 형식 이상: type={type(output2).__name__}")
    rows = []
    for idx, item in enumerate(output2):
        try:
            date_kst = datetime.strptime(item["stck_bsop_date"], "%Y%m%d").date()
            open_dec = Decimal(item["futs_oprc"])
            high_dec = Decimal(item["futs_hgpr"])
            low_dec = Decimal(item["futs_lwpr"])
            close_dec = Decimal(item["futs_prpr"])
        except (KeyError, ValueError, InvalidOperation) as e:
            raise ValueError(f"output2[{idx}] parse 실패: {type(e).__name__}: {e}") from e
        rows.append({
            "source": "krx",
            "asset": "usd-krw-futures",
            "date_kst": date_kst,
            "rate": close_dec,  # invariant: rate == close
            "high": high_dec,
            "low": low_dec,
            "close": close_dec,
            "ohlc_quality": "source_ohlc",
            "close_basis": "krx_cf_close_1545",
            "source_method": "kis_daily_backfill",
            "contract_code": contract.short_code,
            "basis_date": None,
            "published_at": None,
            "metadata_json": {
                "contract_short_code": contract.short_code,
                "contract_month": contract.contract_month,
                "contract_expiry_date": contract.expiry_date.isoformat(),
                "open": str(open_dec),
                "mod_yn": item.get("mod_yn"),
                "acml_vol": item.get("acml_vol"),
                "fetched_at_utc": datetime.now(tz=timezone.utc).isoformat(),
            },
        })
    return rows


# ─────────────────────────────────────────────────────────────
# Validations
# ─────────────────────────────────────────────────────────────

def validate_invariant(rows: list[dict]) -> list[str]:
    """rate == close invariant."""
    issues = []
    for idx, row in enumerate(rows):
        if row["rate"] != row["close"]:
            issues.append(
                f"idx={idx} date_kst={row['date_kst']} rate != close: "
                f"rate={row['rate']}, close={row['close']}"
            )
    return issues


def validate_source_ohlc(rows: list[dict]) -> list[str]:
    """source_ohlc OHLC 완전 + high >= low."""
    issues = []
    for idx, row in enumerate(rows):
        if row["ohlc_quality"] != "source_ohlc":
            issues.append(
                f"idx={idx} date_kst={row['date_kst']} "
                f"ohlc_quality must be 'source_ohlc' for KIS: got {row['ohlc_quality']!r}"
            )
        if row["high"] is None or row["low"] is None or row["close"] is None:
            issues.append(
                f"idx={idx} date_kst={row['date_kst']} OHLC missing: "
                f"high={row['high']}, low={row['low']}, close={row['close']}"
            )
            continue
        if row["high"] < row["low"]:
            issues.append(
                f"idx={idx} date_kst={row['date_kst']} high < low: "
                f"high={row['high']}, low={row['low']}"
            )
    return issues


def validate_metadata_policy(rows: list[dict]) -> list[str]:
    """KRX-방향 metadata policy:
    - contract_code = not None (KRX 필수, Hana/Bithumb과 반대)
    - basis_date = None (Hana 전용)
    - published_at = None (Hana official 전용)
    """
    issues = []
    for idx, row in enumerate(rows):
        if row["contract_code"] is None:
            issues.append(
                f"idx={idx} date_kst={row['date_kst']} contract_code must NOT be None for KRX"
            )
        if row["basis_date"] is not None:
            issues.append(
                f"idx={idx} date_kst={row['date_kst']} basis_date must be None for KRX: "
                f"got {row['basis_date']!r}"
            )
        if row["published_at"] is not None:
            issues.append(
                f"idx={idx} date_kst={row['date_kst']} published_at must be None for KRX: "
                f"got {row['published_at']!r}"
            )
    return issues


def validate_close_basis_method(rows: list[dict]) -> list[str]:
    """close_basis / source_method enum 잠금."""
    issues = []
    for idx, row in enumerate(rows):
        if row["close_basis"] != "krx_cf_close_1545":
            issues.append(
                f"idx={idx} close_basis must be 'krx_cf_close_1545': got {row['close_basis']!r}"
            )
        if row["source_method"] != "kis_daily_backfill":
            issues.append(
                f"idx={idx} source_method must be 'kis_daily_backfill': "
                f"got {row['source_method']!r}"
            )
    return issues


def validate_usage_segment(
    rows: list[dict],
    contract: ContractInfo,
    previous_expiry: Optional[date],
) -> list[str]:
    """Rollover boundary 사용 구간 검증 (Codex 정정 핵심).

    각 contract C_i의 사용 구간 = [previous_contract.expiry_date, C_i.expiry_date)
      - 시작 (inclusive): previous_expiry (이전 contract 만기일 = swap 시점)
      - 종료 (exclusive): contract.expiry_date (현 contract 만기일 = next contract 사용 시작)

    GRAPH §7-new line 369-372: "expiry_date 당일 및 이후 날짜 → next contract".
    """
    issues = []
    for idx, row in enumerate(rows):
        d = row["date_kst"]
        if d >= contract.expiry_date:
            issues.append(
                f"idx={idx} date_kst={d} >= {contract.short_code}.expiry={contract.expiry_date} "
                f"(이 row는 next contract 사용 구간 — GRAPH §7-new line 369-372 위반)"
            )
        if previous_expiry is not None and d < previous_expiry:
            issues.append(
                f"idx={idx} date_kst={d} < previous_expiry={previous_expiry} "
                f"({contract.short_code} 사용 시작 이전 — 이전 contract row)"
            )
    return issues


def validate_decimal_precision(rows: list[dict]) -> list[str]:
    """Numeric(14, 6) precision."""
    issues = []
    for idx, row in enumerate(rows):
        for field in ("rate", "high", "low", "close"):
            value = row[field]
            if value is None:
                continue
            exp = value.as_tuple().exponent
            if not isinstance(exp, int):
                issues.append(f"idx={idx} {field}={value} non-finite")
                continue
            if exp < -6:
                issues.append(f"idx={idx} {field} 소수부 {-exp}자리 (>6): {value}")
    return issues


def validate_ohlc_positive(rows: list[dict]) -> list[str]:
    """non-positive OHLC."""
    issues = []
    for idx, row in enumerate(rows):
        for field in ("rate", "high", "low", "close"):
            value = row[field]
            if value is None:
                continue
            if value <= 0:
                issues.append(f"idx={idx} date_kst={row['date_kst']} {field}={value} (non-positive)")
    return issues


def validate_fetch_range_compliance(
    rows: list[dict],
    fetch_start: date,
    fetch_end: date,
    label: str,
) -> list[str]:
    """fetch range 외 row 검출 (Codex Blocker 2).

    KIS daily endpoint가 미래 range 요청 시 listing 활성 contract의 최신 row를
    out-of-range로 반환하는 동작 (5/28 dry-run에서 실측 — next probe range
    [2026-06-08, 2026-06-15] 요청에 2026-05-28 row 반환).

    Step 3 적재 시 잘못된 date row 저장 위험 surface.
    """
    issues = []
    for idx, row in enumerate(rows):
        d = row["date_kst"]
        if d < fetch_start or d > fetch_end:
            issues.append(
                f"{label} idx={idx} date_kst={d} OUT-OF-RANGE "
                f"(fetch [{fetch_start}, {fetch_end}]) — KIS stale/out-of-range 동작"
            )
    return issues


def validate_duplicates_mapped(mapped_rows: list[dict]) -> list[str]:
    """mapped_rows 내 duplicate date_kst 검출 (Codex Non-blocker 2).

    mapped = previous + current. 같은 date_kst가 둘 다 있으면 rollover boundary
    mapping이 모호 — Step 3 적재 시 unique (source, asset, date_kst) constraint 위반.
    """
    from collections import Counter
    counts = Counter(row["date_kst"] for row in mapped_rows)
    issues = []
    for d, cnt in counts.items():
        if cnt > 1:
            # 어느 contract들에 중복 있는지 표시
            contracts_with_dup = [
                r["contract_code"] for r in mapped_rows if r["date_kst"] == d
            ]
            issues.append(
                f"duplicate date_kst={d}: {cnt}회 (contracts={contracts_with_dup}) — "
                f"rollover boundary mapping 모호"
            )
    return issues


def validate_cap_warning(rows: list[dict], contract: ContractInfo) -> list[str]:
    """100건 cap 감지 — response rows == 100이면 cap 의심 (5/27 실측 정확히 100 cap)."""
    if len(rows) >= KIS_DAILY_ROWS_CAP:
        return [
            f"{contract.short_code}: {len(rows)} rows (>={KIS_DAILY_ROWS_CAP}) — "
            f"KIS 100건 cap 도달 가능성, range split 필요 surface"
        ]
    return []


# ─────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────

def _printable(row: dict) -> dict:
    """row dict → JSON 직렬화 가능 형태."""
    result = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            result[k] = str(v)
        elif isinstance(v, date):
            result[k] = v.isoformat()
        elif isinstance(v, dict):
            result[k] = {kk: (vv.isoformat() if isinstance(vv, date) else vv) for kk, vv in v.items()}
        else:
            result[k] = v
    return result


# ─────────────────────────────────────────────────────────────
# Step 3 — Write helpers (Codex 6 review rounds + Stage 1 보강)
# ─────────────────────────────────────────────────────────────

EXPECTED_BOUNDARY_DATE = date(2026, 5, 18)
EXPECTED_BOUNDARY_CLOSE = Decimal("1496.500")


def _date_arg(s: str) -> date:
    """argparse type validator: ISO YYYY-MM-DD."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"date format은 YYYY-MM-DD (입력: {s!r})")


def validate_write_range(
    start: date,
    end: date,
    chain_start_expiry: date,
    chain_end_expiry: date,
    today: date,
    include_today: bool,
    chain_label: str,
) -> Optional[str]:
    """Step 3 write 진입 전 사전 가드 (rollover boundary + today 가드).

    multi-contract 지원 (옵션 B Round 9):
      - current 모드: chain_start = previous.expiry, chain_end = current.expiry
      - previous 모드: chain_start = before_previous.expiry, chain_end = previous.expiry
      - both 모드: chain_start = before_previous.expiry, chain_end = current.expiry

    Returns: error message (str) or None.
    """
    if start > end:
        return f"start_date={start} > end_date={end}"
    if start < chain_start_expiry:
        return (
            f"start_date={start} < chain_start_expiry={chain_start_expiry} — "
            f"{chain_label} 사용 시작 anchor 위반"
        )
    if end >= chain_end_expiry:
        return (
            f"end_date={end} >= chain_end_expiry={chain_end_expiry} — "
            f"{chain_label} 사용 종료 anchor 위반 (rollover 후 next contract 영역)"
        )
    if not include_today and end >= today:
        return (
            f"end_date={end} >= today={today} (intraday close 미확정 위험). "
            "--include-today 명시 또는 today-1 이하로 제한"
        )
    return None


def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """Step 3 write 진입 전 production DB guard (Codex Blocker 1, Round 7).

    DB URL dialect 검사 (sqlite는 안전, postgresql/mysql 등은 default reject).
    URL 전체 출력 회피 — dialect + host redacted (CLAUDE.md 보안 원칙).

    이 함수는 DB 연결 발생하는 모든 write-path 함수 (table create / session / fetch)
    보다 먼저 호출되어야 함. 이전 사고 (network unreachable이 우연히 보호)의 anchor.

    Returns: error message (str) or None.
    """
    from app.database import engine
    dialect_name = engine.url.get_dialect().name  # 'sqlite' / 'postgresql' / ...
    if dialect_name == "sqlite":
        return None  # local 안전
    if not allow_production:
        host = engine.url.host or "(unknown)"
        redacted_host = "***" if host and host != "(unknown)" else "(unknown)"
        return (
            f"non-SQLite DB detected (dialect={dialect_name} host={redacted_host}). "
            "--allow-production-write 명시 안 됨 — production write 차단. "
            "local smoke: DATABASE_URL=sqlite:///$(pwd)/data/exchange_rates.db env override 권장."
        )
    return None


def ensure_source_daily_rates_table_created() -> None:
    """SourceDailyRate table 존재 보장 (Codex Stage 1 보강).

    local SQLite smoke 시 table 없을 수 있음. idempotent — 존재하면 skip.
    Production에서도 안전 (Step 1 migration과 동일 패턴).
    """
    from app.database import engine
    from app.models import SourceDailyRate
    SourceDailyRate.__table__.create(bind=engine, checkfirst=True)


def write_with_transaction(
    rows_to_write: list[dict],
    contract_codes: set[str],
    start_date: date,
    end_date: date,
) -> tuple[bool, list[str]]:
    """단일 transaction write: upsert(commit=False) loop → post-write validation → commit/rollback.

    Round 9 (옵션 B): single contract → multi-contract 지원 (current / previous / both).
    contract_codes = {current.short_code} | {previous.short_code} | {둘 다}.

    Codex Blocker 2 (Round 7): post-write SELECT에 date range filter + exact count +
    expected/written date set 정확 일치 검증.

    Returns: (success: bool, issues: list[str])
    """
    from app.database import SessionLocal
    from app.source_daily_rates import upsert as upsert_fn
    from app.models import SourceDailyRate

    session = SessionLocal()
    issues: list[str] = []
    try:
        # 1. upsert (commit=False) loop — 명시적 keyword mapping
        # (upsert 시그니처는 invariant rate=close라 'rate' 키 안 받음. row dict 추가 키 무시)
        for row in rows_to_write:
            upsert_fn(
                session,
                commit=False,
                source=row["source"],
                asset=row["asset"],
                date_kst=row["date_kst"],
                close=row["close"],
                ohlc_quality=row["ohlc_quality"],
                close_basis=row["close_basis"],
                source_method=row["source_method"],
                high=row.get("high"),
                low=row.get("low"),
                contract_code=row.get("contract_code"),
                basis_date=row.get("basis_date"),
                published_at=row.get("published_at"),
                metadata_json=row.get("metadata_json"),
            )

        # 2. post-write validation (same transaction, pre-commit, date range + contract_code IN 제한)
        written = (
            session.query(SourceDailyRate)
            .filter(
                SourceDailyRate.source == "krx",
                SourceDailyRate.asset == "usd-krw-futures",
                SourceDailyRate.contract_code.in_(contract_codes),
                SourceDailyRate.date_kst >= start_date,
                SourceDailyRate.date_kst <= end_date,
            )
            .order_by(SourceDailyRate.date_kst.asc())
            .all()
        )

        # (a) expected row count — 정확 일치 (Codex Round 7 정정: < → ==)
        expected_count = len(rows_to_write)
        if len(written) != expected_count:
            issues.append(
                f"row count: written {len(written)} != expected {expected_count} "
                f"(contract_codes={sorted(contract_codes)} + date range 한정 query)"
            )

        # (a-1) expected (date_kst, contract_code) tuples vs written tuples 정확 일치
        # (Codex Blocker 2 강화 + Round 9 multi-contract — same date가 다른 contract에 매핑 가능성 차단)
        expected_pairs = {(row["date_kst"], row["contract_code"]) for row in rows_to_write}
        written_pairs = {(row.date_kst, row.contract_code) for row in written}
        missing = expected_pairs - written_pairs
        extra = written_pairs - expected_pairs
        if missing:
            sample = sorted([(d.isoformat(), c) for d, c in missing])[:5]
            issues.append(f"missing (date, contract) pairs ({len(missing)}): {sample}")
        if extra:
            sample = sorted([(d.isoformat(), c) for d, c in extra])[:5]
            issues.append(f"unexpected (date, contract) pairs in range ({len(extra)}): {sample}")

        # (b) rate == close drift 0
        for row in written:
            if row.rate != row.close:
                issues.append(
                    f"drift at date_kst={row.date_kst}: rate={row.rate} != close={row.close}"
                )

        # (c) all contract_code ∈ contract_codes set (Round 9 multi-contract)
        for row in written:
            if row.contract_code not in contract_codes:
                issues.append(
                    f"contract_code mismatch at date_kst={row.date_kst}: "
                    f"got {row.contract_code!r}, expected ∈ {sorted(contract_codes)}"
                )

        # (d) basis_date IS NULL, published_at IS NULL
        for row in written:
            if row.basis_date is not None:
                issues.append(
                    f"basis_date NOT NULL at date_kst={row.date_kst}: got {row.basis_date}"
                )
            if row.published_at is not None:
                issues.append(
                    f"published_at NOT NULL at date_kst={row.date_kst}: got {row.published_at}"
                )

        # (e) duplicate date_kst — same contract 내 unique
        seen_dates = set()
        for row in written:
            if row.date_kst in seen_dates:
                issues.append(f"duplicate date_kst={row.date_kst} in same contract")
            seen_dates.add(row.date_kst)

        # (f) enum/literal 회귀 가드
        for row in written:
            if row.source != "krx":
                issues.append(f"source mismatch at date_kst={row.date_kst}: got {row.source!r}")
            if row.asset != "usd-krw-futures":
                issues.append(f"asset mismatch at date_kst={row.date_kst}: got {row.asset!r}")
            if row.source_method != "kis_daily_backfill":
                issues.append(
                    f"source_method mismatch at date_kst={row.date_kst}: got {row.source_method!r}"
                )
            if row.close_basis != "krx_cf_close_1545":
                issues.append(
                    f"close_basis mismatch at date_kst={row.date_kst}: got {row.close_basis!r}"
                )
            if row.ohlc_quality != "source_ohlc":
                issues.append(
                    f"ohlc_quality mismatch at date_kst={row.date_kst}: got {row.ohlc_quality!r}"
                )

        # (g) boundary sample — range includes 2026-05-18 → close == 1496.500
        boundary_rows = [r for r in written if r.date_kst == EXPECTED_BOUNDARY_DATE]
        if boundary_rows:
            r = boundary_rows[0]
            # Numeric → Decimal 비교 (precision 안전)
            if Decimal(str(r.close)) != EXPECTED_BOUNDARY_CLOSE:
                issues.append(
                    f"boundary sample mismatch at {EXPECTED_BOUNDARY_DATE}: "
                    f"got close={r.close}, expected {EXPECTED_BOUNDARY_CLOSE} (GRAPH §7-new B-B probe)"
                )

        if issues:
            session.rollback()
            return False, issues
        session.commit()
        return True, []
    except Exception as e:
        session.rollback()
        issues.append(f"transaction exception: {type(e).__name__}: {e}")
        return False, issues
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────
# Token (KisAccessTokenManager 재사용 — drift 회피)
# ─────────────────────────────────────────────────────────────

async def _get_token_async(app_key: str, app_secret: str, cache_path: Path) -> str:
    manager = KisAccessTokenManager(
        app_key=app_key,
        app_secret=app_secret,
        cache_path=cache_path,
    )
    return await manager.get_access_token()


def get_access_token(app_key: str, app_secret: str, cache_path: Path) -> str:
    """sync wrapper — KisAccessTokenManager (async) 재사용."""
    return asyncio.run(_get_token_async(app_key, app_secret, cache_path))


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def _process_contract(
    *,
    label: str,
    contract: ContractInfo,
    previous_expiry: Optional[date],
    fetch_start: date,
    fetch_end: date,
    is_future_unused: bool,
    token: str,
    app_key: str,
    app_secret: str,
) -> tuple[list[dict], int]:
    """단일 contract fetch + parse + per-contract validation.

    Returns: (rows, contract_issue_count)
    """
    print(f"\n----- [{label}] {contract.short_code} -----")
    print(f"  contract_month: {contract.contract_month}")
    print(f"  expiry_date: {contract.expiry_date.isoformat()}")
    print(f"  usage segment: [{previous_expiry}, {contract.expiry_date}) (Codex 정정)")
    print(f"  fetch range: {fetch_start.isoformat()} ~ {fetch_end.isoformat()}")
    if is_future_unused:
        print(f"  [STATUS] future contract — current.expiry 미도래, mapping상 미사용 (fetch 가능성만 surface)")

    try:
        payload = fetch_daily(
            short_code=contract.short_code,
            start_date=fetch_start,
            end_date=fetch_end,
            token=token,
            app_key=app_key,
            app_secret=app_secret,
        )
    except requests.RequestException as e:
        print(f"  [FETCH 실패] {type(e).__name__}: {e}")
        return [], 1
    except RuntimeError as e:
        print(f"  [FETCH 비정상 rt_cd] {e}")
        return [], 1

    try:
        rows = parse_daily_rows(payload, contract)
    except (ValueError, InvalidOperation) as e:
        print(f"  [PARSE 실패] {type(e).__name__}: {e}")
        return [], 1

    print(f"  fetched rows: {len(rows)}")
    if rows:
        sorted_dates = sorted(r["date_kst"] for r in rows)
        print(f"  date range: {sorted_dates[0]} ~ {sorted_dates[-1]}")
        print(f"  first row: {json.dumps(_printable(rows[0]), ensure_ascii=False)[:240]}...")

    # contract-specific validations
    contract_issues = 0
    cap_issues = validate_cap_warning(rows, contract)
    if cap_issues:
        contract_issues += len(cap_issues)
        for i in cap_issues:
            print(f"  [cap warning] {i}")

    if not is_future_unused:
        seg_issues = validate_usage_segment(rows, contract, previous_expiry)
        if seg_issues:
            contract_issues += len(seg_issues)
            print(f"  [usage segment] {len(seg_issues)}건")
            for i in seg_issues[:3]:
                print(f"    - {i}")
            if len(seg_issues) > 3:
                print(f"    ... 외 {len(seg_issues) - 3}건")
        else:
            print(f"  [usage segment] OK (all rows ∈ [{previous_expiry}, {contract.expiry_date}))")
    else:
        if rows:
            print(f"  [usage segment] SKIP — future contract (rows={len(rows)} but mapping 미사용)")
        else:
            print(f"  [usage segment] OK — future contract empty (예상)")

    return rows, contract_issues


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "KIS daily chain → source_daily_rates dry-run validator "
            "(ADR-034 Phase 2d Step 2-3)"
        )
    )
    parser.add_argument(
        "--token-cache-path",
        type=Path,
        default=ACCESS_TOKEN_CACHE_PATH,
        help=(
            f"KIS access_token cache 경로 (default: {ACCESS_TOKEN_CACHE_PATH}, "
            "운영 KisAccessTokenManager + smoke 공유). KIS는 1일 1회 발급 원칙 — "
            "shared cache가 추가 발급 회피로 가장 안전. 운영 분리 필요 시 별도 경로 명시."
        ),
    )
    parser.add_argument(
        "--known-boundary-smoke",
        action="store_true",
        help=(
            "[GRAPH §7-new 검증 사례 재현 전용 모드] previous=A75605 hardcoded + "
            "2026-05-18 boundary 검증. current=A75606 시점에만 정합 (시간 흐름 시 fail). "
            "운영 검증은 기본 dynamic 모드 사용."
        ),
    )
    parser.add_argument(
        "--include-today",
        action="store_true",
        help=(
            "current contract fetch_end에 today 포함 (default: today-1). "
            "today fetch는 intraday futs_prpr 반환 가능 (15:45 close 미확정) → "
            "기본 제외가 안전. operator가 close 도래 후 명시 시에만 사용."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "[Step 3 write mode] dry-run 통과 후 --contract mode 대상 row를 source_daily_rates에 적재. "
            "default: dry-run only (safe). --start-date / --end-date 함께 명시 필수. "
            "transaction 패턴 — upsert(commit=False) loop + post-write validation + commit/rollback."
        ),
    )
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        default=None,
        help=(
            "[--write 시 필수] YYYY-MM-DD. selected chain 사용 시작 anchor 이상 "
            "(--contract current=previous.expiry / previous=before_previous.expiry / both=before_previous.expiry)."
        ),
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        default=None,
        help=(
            "[--write 시 필수] YYYY-MM-DD. selected chain 사용 종료 anchor 미만 + today-1 이하 "
            "(--contract current=current.expiry / previous=previous.expiry / both=current.expiry)."
        ),
    )
    parser.add_argument(
        "--allow-production-write",
        action="store_true",
        help=(
            "[Step 3 production guard] non-SQLite DB (RDS PostgreSQL 등)에 --write 진입 허용. "
            "default off — local SQLite smoke만 허용. production execution은 별도 명시 + "
            "RDS backup 직전 + Stage 3 GO 필수."
        ),
    )
    parser.add_argument(
        "--contract",
        choices=["current", "previous", "both"],
        default="current",
        help=(
            "[Step 3 옵션 B] write 대상 contract chain (default: current). "
            "current = 현재 active (예: A75606). previous = 직전 만기 (예: A75605). "
            "both = previous + current 둘 다 single transaction. "
            "사용 구간: current=[prev.expiry, cur.expiry) / previous=[bef_prev.expiry, prev.expiry) / "
            "both=[bef_prev.expiry, cur.expiry)."
        ),
    )
    parser.add_argument(
        "--range-dry-run",
        action="store_true",
        help=(
            "[Step 4B 단위 5] write-intended dry-run 게이트. N-contract coverage window "
            "(today-1 rolling 1년 또는 --start-date/--end-date 고정) → sequence → manifest → "
            "promote(cap hard) → 기존 seed 비교. DB write 0. hard 있으면 exit 1. --write 상호 배타."
        ),
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    # Step 4B 단위 5 — --range-dry-run (write-intended 게이트). --write 상호 배타 + early branch.
    if args.range_dry_run and args.write:
        print("[CONFIG 실패] --range-dry-run과 --write는 상호 배타")
        sys.exit(2)
    if args.range_dry_run:
        sys.exit(_run_range_dry_run_main(args))

    # *** PRODUCTION GUARD EARLY (Codex Round 8) ***
    # KIS API call (token/master/daily fetch) 진입 전 + DB connection 전.
    # production .env로 --write 잘못 실행 시 DB write뿐 아니라 KIS token rotation /
    # master fetch / daily fetch 모두 사전 차단. KIS 1일 1회 발급 원칙 보호.
    if args.write:
        if args.start_date is None or args.end_date is None:
            print("[CONFIG 실패] --write 시 --start-date / --end-date 필수")
            sys.exit(1)
        guard_err = check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[PRODUCTION 가드] {guard_err}")
            sys.exit(1)

    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    if not app_key or not app_secret:
        print("[CONFIG 실패] KIS_APP_KEY / KIS_APP_SECRET 미설정 (.env 확인)")
        sys.exit(1)

    mode_label = "known-boundary-smoke (anchor 재현)" if args.known_boundary_smoke else "dynamic (운영 영속)"
    print(f"모드: DRY-RUN ({mode_label}, DB write X, upsert() 호출 X)")
    print(f"Endpoint: {DAILY_ENDPOINT}")
    print(f"TR_ID: {TR_ID_DAILY}")
    print(f"Token cache: {args.token_cache_path}")
    print(f"include_today: {args.include_today} (default False — intraday close 오염 회피)")
    print()

    # 1. Token (KisAccessTokenManager 재사용, drift 회피)
    print("[1] Token 발급/캐시 로드 (KisAccessTokenManager)...")
    try:
        token = get_access_token(app_key, app_secret, args.token_cache_path)
    except Exception as e:
        print(f"[TOKEN 실패] {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"  token loaded (length {len(token)})")
    print()

    # 2. Master + chain 구성 (mode 분기)
    print(f"[2] KIS master fetch + chain 구성 ({mode_label})...")
    try:
        now_kst = datetime.now(tz=KST).replace(tzinfo=None)
        if args.known_boundary_smoke:
            previous, current, next_c = build_chain_static_anchor(now_kst)
        else:
            previous, current, next_c = build_chain_dynamic(now_kst)
    except (RuntimeError, requests.RequestException) as e:
        print(f"[MASTER 실패] {type(e).__name__}: {e}")
        sys.exit(1)
    # before_previous 동적 계산 (previous user-facing 사용 시작 anchor — Codex Round 6 정정)
    try:
        before_previous = _build_before_previous_dynamic(previous)
    except RuntimeError as e:
        print(f"[BEFORE-PREVIOUS 실패] {e}")
        sys.exit(1)
    prev_source = "hardcoded A75605" if args.known_boundary_smoke else "dynamic (current - 1 month)"
    print(f"  before-previous: {before_previous.short_code} expiry={before_previous.expiry_date} (previous 사용 시작 anchor)")
    print(f"  previous: {previous.short_code} expiry={previous.expiry_date} ({prev_source})")
    print(f"  current:  {current.short_code} expiry={current.expiry_date} (master resolver)")
    print(f"  next:     {next_c.short_code} expiry={next_c.expiry_date} (master)")
    today = now_kst.date()
    print(f"  today (KST): {today}")
    print()

    # 3. 각 contract fetch + per-contract validation
    print("[3] Chain fetch (user-facing 사용 구간 = [previous contract.expiry, this.expiry))...")

    # ── previous (mapped): 사용 시작 = before_previous.expiry / 사용 종료 = previous.expiry - 1
    # Codex Round 6 정정: SAFE_FETCH_DAYS 임의 range → before_previous.expiry anchor 사용
    prev_fetch_start = before_previous.expiry_date
    prev_fetch_end = previous.expiry_date - timedelta(days=1)
    prev_rows, prev_contract_issues = _process_contract(
        label="previous (mapped)",
        contract=previous,
        previous_expiry=before_previous.expiry_date,  # 시작 boundary 검증 활성
        fetch_start=prev_fetch_start, fetch_end=prev_fetch_end,
        is_future_unused=False,
        token=token, app_key=app_key, app_secret=app_secret,
    )

    # ── current (mapped): 사용 시작 = previous.expiry / 사용 종료 = current.expiry-1 또는 today (intraday 오염 회피)
    cur_fetch_start = previous.expiry_date
    cur_fetch_end_cap = current.expiry_date - timedelta(days=1)
    today_or_yesterday = today if args.include_today else (today - timedelta(days=1))
    cur_fetch_end = min(cur_fetch_end_cap, today_or_yesterday)
    cur_rows, cur_contract_issues = _process_contract(
        label="current (mapped)",
        contract=current,
        previous_expiry=previous.expiry_date,
        fetch_start=cur_fetch_start, fetch_end=cur_fetch_end,
        is_future_unused=False,
        token=token, app_key=app_key, app_secret=app_secret,
    )

    # ── next (probe): 사용 시작 = current.expiry (미도래 가능)
    next_fetch_start_calc = current.expiry_date
    next_fetch_end_calc = min(next_c.expiry_date - timedelta(days=1), today_or_yesterday)
    if next_fetch_start_calc > next_fetch_end_calc:
        # 미도래 — probe range로 대체 (마지막 1주일)
        next_fetch_start = current.expiry_date - timedelta(days=7)
        next_fetch_end = current.expiry_date
        next_is_future = True
        print(f"\n----- [next] {next_c.short_code} -----")
        print(f"  current.expiry={current.expiry_date} 미도래 — 사용 시작 anchor 미래")
        print(f"  probe range adjusted: {next_fetch_start} ~ {next_fetch_end} (out-of-range row surface 목적)")
    else:
        next_fetch_start = next_fetch_start_calc
        next_fetch_end = next_fetch_end_calc
        next_is_future = False

    next_rows, next_contract_issues = _process_contract(
        label="next (probe)" if next_is_future else "next (mapped)",
        contract=next_c,
        previous_expiry=current.expiry_date,
        fetch_start=next_fetch_start, fetch_end=next_fetch_end,
        is_future_unused=next_is_future,
        token=token, app_key=app_key, app_secret=app_secret,
    )

    # ── mapped / probe 분리 (Codex Blocker 3)
    mapped_rows = prev_rows + cur_rows
    probe_rows = next_rows if next_is_future else []
    if not next_is_future:
        mapped_rows = mapped_rows + next_rows

    chain_issues = prev_contract_issues + cur_contract_issues + next_contract_issues

    print()
    print("=" * 60)
    print(
        f"mapped rows: {len(mapped_rows)} (previous + current"
        + (" + next" if not next_is_future else "")
        + f") / probe rows: {len(probe_rows)}"
    )
    print("=" * 60)

    # ── Fetch range compliance (Codex Blocker 2) — 모든 contract
    print()
    range_issues = 0
    for label, rows, fs, fe, is_probe in (
        ("previous", prev_rows, prev_fetch_start, prev_fetch_end, False),
        ("current", cur_rows, cur_fetch_start, cur_fetch_end, False),
        (
            "next probe" if next_is_future else "next",
            next_rows,
            next_fetch_start,
            next_fetch_end,
            next_is_future,
        ),
    ):
        oor = validate_fetch_range_compliance(rows, fs, fe, label)
        if oor:
            if is_probe:
                # probe out-of-range는 KIS stale 동작 known — warning surface only
                # (exit 1 트리거 X, Step 3 적재 시 별도 차단 필요)
                print(
                    f"[fetch range — {label}] {len(oor)}건 WARNING "
                    f"(KIS stale row surface — Step 3 적재 시 차단 필요, dry-run pass)"
                )
            else:
                # mapped contract OUT-OF-RANGE는 데이터 오염 → exit 1 트리거
                range_issues += len(oor)
                print(f"[fetch range — {label}] {len(oor)}건 (mapped contract OUT-OF-RANGE — 데이터 오염)")
            for i in oor[:3]:
                print(f"  - {i}")
            if len(oor) > 3:
                print(f"  ... 외 {len(oor) - 3}건")
        else:
            print(f"[fetch range — {label}] OK (모든 row ∈ [{fs}, {fe}])")

    # 4. Global validation — mapped_rows 기준 (Codex Blocker 3)
    print()
    print("=" * 60)
    print(f"Global Validations (mapped rows {len(mapped_rows)} 기준)")
    print("=" * 60)

    checks = [
        ("rate == close invariant", validate_invariant),
        ("source_ohlc OHLC 완전 / high>=low", validate_source_ohlc),
        ("metadata policy (KRX: contract_code 필수 / basis_date=None / published_at=None)", validate_metadata_policy),
        ("close_basis / source_method enum", validate_close_basis_method),
        ("OHLC non-positive", validate_ohlc_positive),
        ("Decimal(14, 6) precision", validate_decimal_precision),
        ("duplicate date_kst (mapped 내 중복)", validate_duplicates_mapped),
    ]
    global_issues = 0
    for name, fn in checks:
        issues = fn(mapped_rows)
        if issues:
            global_issues += len(issues)
            print(f"\n[{name}] {len(issues)}건")
            for i in issues[:3]:
                print(f"  - {i}")
            if len(issues) > 3:
                print(f"  ... 외 {len(issues) - 3}건")
        else:
            print(f"\n[{name}] OK (0건)")

    # 5. Rollover boundary 검증 — known-boundary-smoke 모드에서만 명시 anchor 검증
    print()
    print("=" * 60)
    print("Rollover boundary 검증")
    print("=" * 60)
    boundary_date = previous.expiry_date
    current_rows_on_boundary = [r for r in cur_rows if r["date_kst"] == boundary_date]
    previous_rows_on_boundary = [r for r in prev_rows if r["date_kst"] == boundary_date]
    if current_rows_on_boundary:
        r = current_rows_on_boundary[0]
        anchor_note = (
            "GRAPH §7-new line 372-379 사례 재현" if args.known_boundary_smoke
            else f"dynamic mode — {boundary_date} = previous.expiry boundary"
        )
        print(f"\n[OK] {boundary_date} row가 current contract {current.short_code}에 mapping됨")
        print(f"  close={r['close']} contract_code={r['contract_code']} ({anchor_note})")
    else:
        # boundary date에 row가 없을 수도 있음 (영업일 아닌 만기일, 또는 fetch range 외)
        print(
            f"\n[INFO] {boundary_date} row가 current contract에 없음 "
            f"(영업일 아니거나 fetch range 외 — 위반 단정 X)"
        )
    if previous_rows_on_boundary:
        global_issues += 1
        print(
            f"[FAIL] {boundary_date} row가 previous contract {previous.short_code}에도 존재 — "
            f"GRAPH §7-new 위반 (만기일 row는 next contract 사용)"
        )

    # 6. Probe section (Codex Blocker 3 분리) — global validation과 별도
    if probe_rows:
        print()
        print("=" * 60)
        print(f"Probe section (next future contract, mapping 미사용) — {len(probe_rows)} rows")
        print("=" * 60)
        print(f"  contract: {next_c.short_code} expiry={next_c.expiry_date}")
        print(f"  current.expiry={current.expiry_date} 미도래 — fetch 가능성 surface only")
        if probe_rows:
            sorted_d = sorted(r["date_kst"] for r in probe_rows)
            print(f"  probe row date range: {sorted_d[0]} ~ {sorted_d[-1]}")
            print(
                "  (out-of-range row는 fetch range compliance section에서 WARNING surface only — "
                "range count 미포함, KIS stale 동작 known)"
            )

    total_issues = chain_issues + range_issues + global_issues
    print()
    print("=" * 60)
    print(
        f"DRY-RUN 요약: mapped {len(mapped_rows)} / probe {len(probe_rows)} / "
        f"chain {chain_issues} / range {range_issues} / global {global_issues}"
    )
    if total_issues == 0:
        print(f"[DRY-RUN 완료] 모든 validation 통과 (DB write 안 됨)")
    else:
        print(f"[DRY-RUN 실패] 총 {total_issues}건 이슈 (DB write 안 됨)")

    if total_issues > 0:
        # dry-run validation fail 시 write 진입 차단
        if args.write:
            print(f"\n[WRITE SKIP] dry-run validation 실패 — write 진입 차단")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # Step 3 write section (--write 명시 시 진입)
    # ─────────────────────────────────────────────────────────────
    if not args.write:
        print()
        print("[다음 단계] Step 3 partial backfill 실측 적재는 --write 명시 시 진입.")
        return

    print()
    print("=" * 60)
    print(f"Step 3 write section (--contract {args.contract})")
    print("=" * 60)
    # production guard + start/end 필수 검사는 main 진입 시점에서 처리됨 (Codex Round 8).

    # --contract 분기 (Round 9 옵션 B): mode별 rows / contract_codes / chain range
    if args.contract == "current":
        rows_source = cur_rows
        contract_codes = {current.short_code}
        chain_start_expiry = previous.expiry_date
        chain_end_expiry = current.expiry_date
        chain_label = f"current ({current.short_code})"
    elif args.contract == "previous":
        rows_source = prev_rows
        contract_codes = {previous.short_code}
        chain_start_expiry = before_previous.expiry_date
        chain_end_expiry = previous.expiry_date
        chain_label = f"previous ({previous.short_code})"
    else:  # "both"
        rows_source = prev_rows + cur_rows
        contract_codes = {previous.short_code, current.short_code}
        chain_start_expiry = before_previous.expiry_date
        chain_end_expiry = current.expiry_date
        chain_label = f"both ({previous.short_code} + {current.short_code})"

    # rollover boundary + today 가드 (multi-contract 지원)
    range_err = validate_write_range(
        start=args.start_date,
        end=args.end_date,
        chain_start_expiry=chain_start_expiry,
        chain_end_expiry=chain_end_expiry,
        today=today,
        include_today=args.include_today,
        chain_label=chain_label,
    )
    if range_err:
        print(f"[CONFIG 실패] write range: {range_err}")
        sys.exit(1)

    print(f"  contract mode: {args.contract}")
    print(f"  chain: {chain_label}")
    print(f"  contract_codes: {sorted(contract_codes)}")
    print(f"  write range: {args.start_date} ~ {args.end_date}")
    print(f"  rollover boundary: [{chain_start_expiry}, {chain_end_expiry})")
    print()

    # rows source 중 args range 내에 있는 row만 추출 (write 대상)
    rows_to_write = [
        r for r in rows_source
        if args.start_date <= r["date_kst"] <= args.end_date
    ]
    print(f"  write 대상 row 수: {len(rows_to_write)} (rows_source {len(rows_source)} 중 range 내)")

    if not rows_to_write:
        print("[WRITE SKIP] write 대상 row 0개 — args range 안에 fetched row 없음")
        sys.exit(1)

    # contract_code 정합성 사전 확인 (rows_to_write 안의 contract_code가 모두 contract_codes set 안에 있음)
    invalid_contract = [
        r for r in rows_to_write if r.get("contract_code") not in contract_codes
    ]
    if invalid_contract:
        print(f"[WRITE 실패] write 대상 row 중 contract_code 위반 {len(invalid_contract)}건 — hard reject")
        sys.exit(1)

    # OOR hard reject (dry-run에서 surface된 항목 write 진입 차단)
    oor_in_write = [
        r for r in rows_to_write
        if r["date_kst"] < args.start_date or r["date_kst"] > args.end_date
    ]
    if oor_in_write:
        print(f"[WRITE 실패] write 대상 중 out-of-range {len(oor_in_write)}건 — hard reject")
        sys.exit(1)

    # Ensure table created (Codex Stage 1 보강)
    print("  ensure source_daily_rates table created (checkfirst=True, idempotent)...")
    try:
        ensure_source_daily_rates_table_created()
    except Exception as e:
        print(f"[TABLE 실패] {type(e).__name__}: {e}")
        sys.exit(1)
    print("  OK")
    print()

    # Transaction write (multi-contract)
    print(f"[Step 3 write] {len(rows_to_write)} rows → source_daily_rates (contracts={sorted(contract_codes)}) ...")
    success, write_issues = write_with_transaction(
        rows_to_write, contract_codes, args.start_date, args.end_date
    )
    if success:
        print(f"[Step 3 write 완료] {len(rows_to_write)} rows committed + 7 post-write validations passed")
        print()
        print("[Rollback anchor] cleanup 시:")
        print(
            f"  from app.database import SessionLocal; from app.source_daily_rates import delete_range; "
            f'db = SessionLocal(); '
            f'delete_range(db, "krx", "usd-krw-futures", date({args.start_date.year}, {args.start_date.month}, {args.start_date.day}), '
            f'date({args.end_date.year}, {args.end_date.month}, {args.end_date.day}))'
        )
        print()
        print("[Stage 2] commit 별 GO / Stage 3 production execution 별 GO (RDS backup 직전).")
    else:
        print(f"[Step 3 write 실패] {len(write_issues)}건 issue — transaction rollback 완료")
        for issue in write_issues[:5]:
            print(f"  - {issue}")
        if len(write_issues) > 5:
            print(f"  ... 외 {len(write_issues) - 5}건")
        sys.exit(1)


if __name__ == "__main__":
    main()
