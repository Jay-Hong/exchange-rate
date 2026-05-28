#!/usr/bin/env python3
"""
KIS daily chain → source_daily_rates dry-run validator.

ADR-034 Phase 2d Step 2-3 (KIS daily chain dry-run, Step 2/3 경계 보존 — DB write X).

목적:
  - KIS inquire-daily-fuopchartprice REST endpoint 호출
  - A75YMM contract chain 3 contracts (previous + current + next) fetch
  - rollover boundary 검증 (만기일 07:00 KST swap point, GRAPH §7-new)
  - source_daily_rates row dict 생성 + 13 validation suite
  - Step 2/3 경계 보존 (DB write X, upsert() 호출 X)

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
  python scripts/backfill_kis_source_daily_rates.py [--token-cache-path PATH]

주의:
  - KIS_APP_KEY / KIS_APP_SECRET 필요 (.env)
  - 기본 token cache: .cache/kis_access_token.json (운영 + smoke 공유)
  - KIS는 접근 토큰 1일 1회 발급 원칙 — fresh cache 재사용으로 추가 발급 0
  - 본 script는 dry-run only. DB write 절대 X.
"""

# 표준 라이브러리
import argparse
import asyncio
import json
import os
import sys
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
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
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
    print()
    print("[다음 단계] Step 3 partial backfill 실측 적재는 별도 PR (ADR-034 §14 §3 잠정 KRX 먼저).")

    if total_issues > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
