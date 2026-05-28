#!/usr/bin/env python3
"""
Hana official_historical USD/JPY/EUR → source_daily_rates dry-run validator.

ADR-034 Phase 2d Step 2-2 (Hana dry-run, Step 2/3 경계 보존 — DB write X).

목적:
  - Hana official endpoint (wpfxd651_01i_01.do) historical row fetch
  - HTML response → source_daily_rates row dict 변환 (upsert() 호출 X)
  - Hana-방향 metadata policy 검증 (basis_date / published_at / pbldSqn 모두 필수)

문서 anchor (ADR-033 Amendment 2 + GRAPH_API_V2_CONTRACT.md §7):
  - endpoint: GET wpfxd651_01i_01.do (Referer 헤더 필수)
  - params: curCd={USD|JPY|EUR} / tmpInqStrDt=YYYY-MM-DD / pbldDvCd=0 / pbldSqn=빈값 / inqStrDt=YYYYMMDD
  - canonical date = 응답 기준일 (휴일 요청 시 직전 영업일 자동 fallback)
  - 매매기준율 = `<td class="txtAr">` 인덱스 7
  - 고시일시 (published_at): 다음날 새벽 발표 timestamp (KST)
  - 회차 pbldSqn: response에 "(NNNN회차)" — provenance metadata로 저장, 공식 단정 X

Row mapping:
  - source = "hana"
  - asset = ASSET_MAP[currency]
  - date_kst = basis_date  (canonical date 정책, 휴일 fallback dedup)
  - rate = close = 매매기준율 (invariant)
  - high = low = close (close_only fallback)
  - ohlc_quality = "close_only"
  - close_basis = "hana_official_historical_backfill"
  - source_method = "external_backfill"
  - contract_code = None (KRX 전용)
  - basis_date = basis_date (Hana 필수)
  - published_at = 고시일시 (Hana 필수, top-level)
  - metadata_json = {pbldSqn, request_date, fallback, currency, endpoint, fetched_at_utc}

사용법:
  python scripts/backfill_hana_source_daily_rates.py [--currency USD] [--date YYYY-MM-DD]

주의:
  - 본 script는 dry-run only. DB write 절대 X.
  - app.source_daily_rates.upsert() 호출하지 않음.
  - Step 3 (partial backfill 실측 적재)은 별도 PR.
"""

# 표준 라이브러리
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup

# 프로젝트 루트를 sys.path에 추가 (일관성 유지)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


KST = ZoneInfo("Asia/Seoul")
ENDPOINT = "https://www.hanabank.com/cms/rate/wpfxd651_01i_01.do"
REFERER = "https://www.kebhana.com/cont/mall/mall15/mall1501/index.jsp"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15"

ASSET_MAP = {
    "USD": "usd-krw",
    "JPY": "jpy-krw",
    "EUR": "eur-krw",
}


# ─────────────────────────────────────────────────────────────
# Fetch + Parse
# ─────────────────────────────────────────────────────────────

def fetch_html(currency: str, request_date: date, timeout: float = 15.0) -> str:
    """Hana endpoint fetch → HTML fragment."""
    params = {
        "ajax": "true",
        "curCd": currency,
        "tmpInqStrDt": request_date.isoformat(),
        "pbldDvCd": "0",
        "pbldSqn": "",
        "hid_key_data": "",
        "inqStrDt": request_date.strftime("%Y%m%d"),
    }
    headers = {"Referer": REFERER, "User-Agent": USER_AGENT}
    response = requests.get(ENDPOINT, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


_RE_KR_DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_RE_KR_TIME = re.compile(r"(\d{1,2})\s*시\s*(\d{1,2})\s*분\s*(\d{1,2})\s*초")
_RE_PBLD_SQN = re.compile(r"\((\d+)\s*회차\)")


def _parse_kr_date(s: str) -> date:
    m = _RE_KR_DATE.search(s)
    if not m:
        raise ValueError(f"한국어 날짜 parse 실패: {s!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_kr_datetime(date_text: str, time_text: str) -> datetime:
    bd = _parse_kr_date(date_text)
    m = _RE_KR_TIME.search(time_text)
    if not m:
        raise ValueError(f"한국어 시각 parse 실패: {time_text!r}")
    return datetime(
        bd.year, bd.month, bd.day,
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        tzinfo=KST,
    )


def parse_response(html: str) -> dict:
    """HTML response → parsed dict.

    Returns: {basis_date, published_at, pbld_sqn, rate_dec, txt_ar_count}
    """
    soup = BeautifulSoup(html, "html.parser")

    # 기준일 (canonical date)
    basis_em = soup.find("em", string=lambda s: s and "기준일" in s)
    if not basis_em:
        raise ValueError("기준일 element를 못 찾음")
    basis_strong = basis_em.find_next("strong")
    if not basis_strong:
        raise ValueError("기준일 다음 strong을 못 찾음")
    basis_date = _parse_kr_date(basis_strong.text)

    # 고시일시 (published_at): 날짜 strong + 시각 strong
    gosi_em = soup.find("em", string=lambda s: s and "고시일시" in s)
    if not gosi_em:
        raise ValueError("고시일시 element를 못 찾음")
    gosi_date_strong = gosi_em.find_next("strong")
    if not gosi_date_strong:
        raise ValueError("고시일시 다음 strong (날짜) 못 찾음")
    gosi_time_strong = gosi_date_strong.find_next("strong")
    if not gosi_time_strong:
        raise ValueError("고시일시 시각 strong 못 찾음")
    published_at = _parse_kr_datetime(gosi_date_strong.text, gosi_time_strong.text)

    # 회차 pbldSqn: gosi_time_strong 다음 strong에 "(NNNN회차)"
    sqn_strong = gosi_time_strong.find_next("strong")
    if not sqn_strong:
        raise ValueError("회차 strong 못 찾음")
    sqn_match = _RE_PBLD_SQN.search(sqn_strong.text)
    if not sqn_match:
        raise ValueError(f"회차 parse 실패: {sqn_strong.text!r}")
    pbld_sqn = int(sqn_match.group(1))

    # 매매기준율 = txtAr 인덱스 7
    txt_ar_cells = soup.find_all("td", class_="txtAr")
    if len(txt_ar_cells) <= 7:
        raise ValueError(f"txtAr cell 수 부족 ({len(txt_ar_cells)} <= 7)")
    rate_text = txt_ar_cells[7].text.strip().replace(",", "")
    rate_dec = Decimal(rate_text)

    return {
        "basis_date": basis_date,
        "published_at": published_at,
        "pbld_sqn": pbld_sqn,
        "rate_dec": rate_dec,
        "txt_ar_count": len(txt_ar_cells),
    }


def build_row(parsed: dict, request_date: date, currency: str) -> dict:
    """parsed → source_daily_rates row dict (DB write X — dry-run only).

    invariant: rate = close. close_only fallback: high = low = close.
    """
    rate_dec = parsed["rate_dec"]
    basis_date = parsed["basis_date"]
    fallback = (request_date != basis_date)

    return {
        "source": "hana",
        "asset": ASSET_MAP[currency],
        "date_kst": basis_date,  # canonical date = basis_date
        "rate": rate_dec,
        "high": rate_dec,  # close_only fallback
        "low": rate_dec,
        "close": rate_dec,
        "ohlc_quality": "close_only",
        "close_basis": "hana_official_historical_backfill",
        "source_method": "external_backfill",
        "contract_code": None,
        "basis_date": basis_date,
        "published_at": parsed["published_at"],
        "metadata_json": {
            "pbldSqn": parsed["pbld_sqn"],
            "request_date": request_date.isoformat(),
            "fallback": fallback,
            "currency": currency,
            "endpoint": ENDPOINT,
            "fetched_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        },
    }


# ─────────────────────────────────────────────────────────────
# Validations
# ─────────────────────────────────────────────────────────────

def validate_invariant(row: dict) -> list[str]:
    """rate == close invariant."""
    if row["rate"] != row["close"]:
        return [f"rate != close: rate={row['rate']}, close={row['close']}"]
    return []


def validate_close_only(row: dict) -> list[str]:
    """close_only fallback (high=low=close + ohlc_quality)."""
    issues = []
    if row["ohlc_quality"] != "close_only":
        issues.append(f"ohlc_quality must be 'close_only' for Hana: got {row['ohlc_quality']!r}")
    if row["high"] != row["close"] or row["low"] != row["close"]:
        issues.append(
            f"close_only fallback 위반: high={row['high']}, low={row['low']}, close={row['close']}"
        )
    return issues


def validate_date_kst_basis_date(row: dict) -> list[str]:
    """date_kst == basis_date (canonical date 정책)."""
    if row["date_kst"] != row["basis_date"]:
        return [
            f"date_kst != basis_date: date_kst={row['date_kst']}, basis_date={row['basis_date']}"
        ]
    return []


def validate_metadata_policy(row: dict) -> list[str]:
    """Hana-방향 metadata policy (Bithumb과 반대):
    - contract_code = None (KRX 전용)
    - basis_date = not None (Hana 필수)
    - published_at = not None (Hana 필수)
    - metadata_json.pbldSqn = not None (Hana 필수)
    """
    issues = []
    if row["contract_code"] is not None:
        issues.append(f"contract_code must be None for Hana: got {row['contract_code']!r}")
    if row["basis_date"] is None:
        issues.append("basis_date must NOT be None for Hana")
    if row["published_at"] is None:
        issues.append("published_at must NOT be None for Hana (다음날 새벽 고시 timestamp)")
    if row["metadata_json"].get("pbldSqn") is None:
        issues.append("metadata_json.pbldSqn must NOT be None for Hana")
    return issues


def validate_close_basis_method(row: dict) -> list[str]:
    """close_basis / source_method enum 잠금."""
    issues = []
    if row["close_basis"] != "hana_official_historical_backfill":
        issues.append(
            f"close_basis must be 'hana_official_historical_backfill': got {row['close_basis']!r}"
        )
    if row["source_method"] != "external_backfill":
        issues.append(
            f"source_method must be 'external_backfill': got {row['source_method']!r}"
        )
    return issues


def validate_decimal_precision(row: dict) -> list[str]:
    """Numeric(14, 6) precision (소수부 6자리 초과 검출)."""
    issues = []
    for field in ("rate", "high", "low", "close"):
        value = row[field]
        if value is None:
            continue
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int):
            issues.append(f"{field}={value} non-finite")
            continue
        if exponent < -6:
            issues.append(f"{field} 소수부 {-exponent}자리 (>6): {value}")
    return issues


def validate_ohlc_positive(row: dict) -> list[str]:
    """non-positive OHLC + high<low."""
    issues = []
    for field in ("rate", "high", "low", "close"):
        value = row[field]
        if value is None:
            continue
        if value <= 0:
            issues.append(f"non-positive {field}={value}")
    high, low = row.get("high"), row.get("low")
    if high is not None and low is not None and high < low:
        issues.append(f"high < low: high={high}, low={low}")
    return issues


def validate_published_at_kst(row: dict) -> list[str]:
    """published_at은 Hana 발표 timestamp — tzinfo + KST offset (+09:00) 잠금.

    `parse_response()`가 항상 KST tzinfo로 생성하나, parsing logic 변경 시 회귀 가드.
    """
    issues = []
    pa = row.get("published_at")
    if pa is None:
        return ["published_at is None — KST validation skip (metadata_policy에서 catch)"]
    if pa.tzinfo is None:
        issues.append(f"published_at tzinfo missing (naive datetime): {pa.isoformat()}")
        return issues
    offset = pa.utcoffset()
    expected = timedelta(hours=9)
    if offset != expected:
        issues.append(
            f"published_at offset != +09:00 (KST): got {offset}, expected {expected}"
        )
    return issues


def validate_txt_ar_count(parsed: dict) -> list[str]:
    """DOM 변경 monitoring — txtAr cell count.

    매매기준율은 인덱스 7 (8th cell). count < 8이면 index 7 추출 자체가 fail 또는
    Hana DOM 변경 signal. GRAPH_API_V2_CONTRACT.md §14 Open: "DOM 변경 monitoring".
    정확히 10 잠금은 너무 엄격 — Hana DOM 미세 변경 허용 위해 >= 8 ceiling.
    """
    count = parsed.get("txt_ar_count", 0)
    if count < 8:
        return [
            f"txtAr cell count={count} (<8 — index 7 매매기준율 위치 위험, Hana DOM 변경 가능성)"
        ]
    return []


# ─────────────────────────────────────────────────────────────
# Step 3 — Write helpers (Hana 방향, KIS writer 패턴 재사용)
# ─────────────────────────────────────────────────────────────


def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """Step 3 write 진입 전 production DB guard (KIS writer 패턴 재사용).

    DB URL dialect 검사 (sqlite는 안전, postgresql/mysql 등은 default reject).
    URL 전체 출력 회피 — dialect + host redacted (CLAUDE.md 보안 원칙).

    DB 연결 발생하는 모든 write-path 함수 (table create / session / fetch) 보다
    먼저 호출되어야 함.

    Returns: error message (str) or None.
    """
    from app.database import engine
    dialect_name = engine.url.get_dialect().name
    if dialect_name == "sqlite":
        return None
    if not allow_production:
        host = engine.url.host or "(unknown)"
        redacted_host = "***" if host and host != "(unknown)" else "(unknown)"
        return (
            f"non-SQLite DB detected (dialect={dialect_name} host={redacted_host}). "
            "--allow-production-write 명시 안 됨 — production write 차단. "
            "local smoke: DATABASE_URL=sqlite:///$(pwd)/data/exchange_rates.db env override 권장."
        )
    return None


def validate_write_range(start: date, end: date, today: date, include_today: bool) -> Optional[str]:
    """Step 3 write 진입 전 사전 가드 (range + today 가드).

    Hana는 contract chain 없음 → 단순 calendar range validation.
    """
    if start > end:
        return f"start_date={start} > end_date={end}"
    if not include_today and end >= today:
        return (
            f"end_date={end} >= today={today} (intraday close 미확정 위험). "
            "--include-today 명시 또는 today-1 이하로 제한"
        )
    return None


def ensure_source_daily_rates_table_created() -> None:
    """SourceDailyRate table 존재 보장 (KIS writer 패턴 재사용)."""
    from app.database import engine
    from app.models import SourceDailyRate
    SourceDailyRate.__table__.create(bind=engine, checkfirst=True)


def fetch_and_dedup_calendar_range(
    currency: str,
    start: date,
    end: date,
) -> tuple[list[dict], list[str], list[tuple[date, date]]]:
    """Calendar-day loop + basis_date dedup (Hana-specific).

    각 calendar day마다 fetch + parse + build_row. 휴일 fallback으로 같은 basis_date가
    여러 번 나오면 dedup (first occurrence만 유지, fallback events는 log로 surface).

    Returns: (rows_to_write, fetch_errors, fallback_events)
      - rows_to_write: dedup된 row list (basis_date unique)
      - fetch_errors: fetch/parse 실패 list (str)
      - fallback_events: (request_date, basis_date) 튜플 — request_date != basis_date인 경우
    """
    rows_to_write: list[dict] = []
    fetch_errors: list[str] = []
    fallback_events: list[tuple[date, date]] = []
    written_basis_dates: set[date] = set()

    cur = start
    while cur <= end:
        try:
            html = fetch_html(currency, cur)
            parsed = parse_response(html)
            row = build_row(parsed, request_date=cur, currency=currency)
        except requests.RequestException as e:
            fetch_errors.append(f"FETCH 실패 request_date={cur}: {type(e).__name__}: {e}")
            cur += timedelta(days=1)
            continue
        except (ValueError, AttributeError, InvalidOperation) as e:
            fetch_errors.append(f"PARSE 실패 request_date={cur}: {type(e).__name__}: {e}")
            cur += timedelta(days=1)
            continue

        basis = row["basis_date"]
        if basis != cur:
            fallback_events.append((cur, basis))

        if basis in written_basis_dates:
            # 휴일 fallback dedup — 이미 같은 basis_date 처리됨
            pass
        else:
            written_basis_dates.add(basis)
            rows_to_write.append(row)

        cur += timedelta(days=1)

    return rows_to_write, fetch_errors, fallback_events


def write_with_transaction_hana(
    rows_to_write: list[dict],
    currency: str,
    start_date: date,
    end_date: date,
) -> tuple[bool, list[str]]:
    """Hana 단일 transaction write: upsert(commit=False) loop → post-write validation → commit/rollback.

    KIS writer와 다른 점:
      - Hana는 contract_code 없음 — post-write SELECT는 source/asset/date_kst range filter
      - post-write validation Hana 방향 (contract_code IS NULL / basis_date+published_at NOT NULL / pbldSqn NOT NULL)
      - basis_date는 row의 date_kst와 동일 (canonical date 정책)

    rows_to_write은 이미 fetch_and_dedup_calendar_range에서 basis_date 기준 dedup됨.

    Returns: (success: bool, issues: list[str])
    """
    from app.database import SessionLocal
    from app.source_daily_rates import upsert as upsert_fn
    from app.models import SourceDailyRate

    asset = ASSET_MAP[currency]
    session = SessionLocal()
    issues: list[str] = []
    try:
        # 1. upsert (commit=False) loop — 명시적 keyword mapping
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

        # 2. post-write validation (same transaction, pre-commit, expected_dates IN filter)
        # Codex Round 1 Blocker: Hana calendar-day dedup → expected_dates 비연속이라
        # range query (date_kst >= min, <= max)는 휴일 gap 안 다른 적재 row를 false detect 가능.
        # date_kst.in_(expected_dates)로 정확 매칭 (KRX의 contract_code.in_() 패턴 equiv).
        expected_dates = {row["date_kst"] for row in rows_to_write}
        if not expected_dates:
            # 빈 rows_to_write → 정상 (모든 휴일 또는 fetch 실패) — skip but log
            session.rollback()
            return True, []

        written = (
            session.query(SourceDailyRate)
            .filter(
                SourceDailyRate.source == "hana",
                SourceDailyRate.asset == asset,
                SourceDailyRate.date_kst.in_(expected_dates),
            )
            .order_by(SourceDailyRate.date_kst.asc())
            .all()
        )

        # (a) expected row count == written count
        expected_count = len(rows_to_write)
        if len(written) != expected_count:
            issues.append(
                f"row count: written {len(written)} != expected {expected_count} "
                f"(source=hana asset={asset} date_kst IN expected_dates)"
            )

        # (b) expected_dates set == written_dates set
        written_dates = {row.date_kst for row in written}
        missing = expected_dates - written_dates
        extra = written_dates - expected_dates
        if missing:
            sample = sorted([d.isoformat() for d in missing])[:5]
            issues.append(f"missing dates ({len(missing)}): {sample}")
        if extra:
            sample = sorted([d.isoformat() for d in extra])[:5]
            issues.append(f"unexpected dates in range ({len(extra)}): {sample}")

        # (c) rate == close invariant
        for row in written:
            if row.rate != row.close:
                issues.append(
                    f"drift at date_kst={row.date_kst}: rate={row.rate} != close={row.close}"
                )

        # (d) Hana 방향 metadata policy:
        # - contract_code IS NULL
        # - basis_date IS NOT NULL (== date_kst)
        # - published_at IS NOT NULL
        # - metadata_json.pbldSqn IS NOT NULL
        for row in written:
            if row.contract_code is not None:
                issues.append(
                    f"contract_code NOT NULL at date_kst={row.date_kst}: got {row.contract_code!r}"
                )
            if row.basis_date is None:
                issues.append(f"basis_date IS NULL at date_kst={row.date_kst}")
            elif row.basis_date != row.date_kst:
                issues.append(
                    f"basis_date != date_kst at {row.date_kst}: got {row.basis_date}"
                )
            if row.published_at is None:
                issues.append(f"published_at IS NULL at date_kst={row.date_kst}")
            md = row.metadata_json or {}
            if md.get("pbldSqn") is None:
                issues.append(f"metadata_json.pbldSqn IS NULL at date_kst={row.date_kst}")

        # (e) enum/literal 회귀 가드
        for row in written:
            if row.source != "hana":
                issues.append(f"source mismatch at date_kst={row.date_kst}: got {row.source!r}")
            if row.asset != asset:
                issues.append(f"asset mismatch at date_kst={row.date_kst}: got {row.asset!r}")
            if row.source_method != "external_backfill":
                issues.append(
                    f"source_method mismatch at date_kst={row.date_kst}: got {row.source_method!r}"
                )
            if row.close_basis != "hana_official_historical_backfill":
                issues.append(
                    f"close_basis mismatch at date_kst={row.date_kst}: got {row.close_basis!r}"
                )
            if row.ohlc_quality != "close_only":
                issues.append(
                    f"ohlc_quality mismatch at date_kst={row.date_kst}: got {row.ohlc_quality!r}"
                )

        # (f) duplicate date_kst (same Hana asset 내 unique)
        seen = set()
        for row in written:
            if row.date_kst in seen:
                issues.append(f"duplicate date_kst={row.date_kst} in same asset")
            seen.add(row.date_kst)

        # (g) close_only fallback (high == low == close)
        for row in written:
            if row.high != row.close or row.low != row.close:
                issues.append(
                    f"close_only fallback 위반 at date_kst={row.date_kst}: "
                    f"high={row.high}, low={row.low}, close={row.close}"
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


def validate_fallback_signal(row: dict, request_date: date) -> list[str]:
    """metadata_json.fallback bool과 request_date vs basis_date 차이 정합성."""
    md = row["metadata_json"]
    expected = (request_date != row["basis_date"])
    actual = md.get("fallback")
    if actual != expected:
        return [
            f"metadata_json.fallback={actual} but "
            f"request_date={request_date} vs basis_date={row['basis_date']} expected fallback={expected}"
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
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, date):
            result[k] = v.isoformat()
        elif isinstance(v, dict):
            result[k] = {
                kk: (vv.isoformat() if isinstance(vv, (date, datetime)) else vv)
                for kk, vv in v.items()
            }
        else:
            result[k] = v
    return result


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def _date_arg(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"--date format은 YYYY-MM-DD (입력: {s!r})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Hana official_historical → source_daily_rates dry-run validator "
            "(ADR-034 Phase 2d Step 2-2)"
        )
    )
    parser.add_argument(
        "--currency",
        choices=list(ASSET_MAP.keys()),
        default="USD",
        help="USD / JPY / EUR (default: USD)",
    )
    parser.add_argument(
        "--date",
        type=_date_arg,
        default=None,
        help="[dry-run 모드] YYYY-MM-DD (default: 오늘 KST). 단일 date 검증.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "[Step 3 write mode] dry-run 통과 후 calendar-day range + basis_date dedup 적재. "
            "default: dry-run only (safe). --start-date / --end-date 함께 명시 필수. "
            "transaction 패턴 — upsert(commit=False) loop + post-write validation + commit/rollback."
        ),
    )
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        default=None,
        help="[--write 시 필수] YYYY-MM-DD. calendar-day loop 시작 (request_date 기준).",
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        default=None,
        help="[--write 시 필수] YYYY-MM-DD. calendar-day loop 종료 (request_date 기준). today-1 이하 권장 (intraday close 오염 회피, --include-today 명시 시 today 허용).",
    )
    parser.add_argument(
        "--include-today",
        action="store_true",
        help="--end-date에 today 포함 (default off, intraday close 오염 회피).",
    )
    parser.add_argument(
        "--allow-production-write",
        action="store_true",
        help=(
            "[Step 3 production guard] non-SQLite DB (RDS PostgreSQL 등)에 --write 진입 허용. "
            "default off — local SQLite smoke만 허용. production execution은 별도 명시 + Stage 3 GO 필수."
        ),
    )
    args = parser.parse_args()

    # *** PRODUCTION GUARD EARLY (Hana writer, KIS Round 8 패턴 재사용) ***
    # KIS와 다르게 Hana는 외부 인증 없음 (공개 endpoint)이지만, DB write 가드는 동일.
    # production .env로 --write 잘못 실행 시 DB write 사전 차단 + 운영 영향 0 보장.
    if args.write:
        if args.start_date is None or args.end_date is None:
            print("[CONFIG 실패] --write 시 --start-date / --end-date 필수")
            sys.exit(1)
        guard_err = check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[PRODUCTION 가드] {guard_err}")
            sys.exit(1)

    request_date = args.date or datetime.now(tz=KST).date()
    today_kst = datetime.now(tz=KST).date()

    mode_label = "WRITE (calendar-day loop + basis_date dedup)" if args.write else "DRY-RUN (DB write X)"
    print(f"모드: {mode_label}")
    print(f"Endpoint: {ENDPOINT}")
    print(f"Currency: {args.currency} (asset={ASSET_MAP[args.currency]})")
    if args.write:
        print(f"Write range: {args.start_date.isoformat()} ~ {args.end_date.isoformat()} (calendar-day)")
        print(f"include_today: {args.include_today}")
    else:
        print(f"Request date: {request_date.isoformat()}")
    print()

    # ─────────────────────────────────────────────────────────────
    # WRITE mode 분기 (--write 시 진입, dry-run section은 skip)
    # ─────────────────────────────────────────────────────────────
    if args.write:
        # range guard
        range_err = validate_write_range(
            start=args.start_date,
            end=args.end_date,
            today=today_kst,
            include_today=args.include_today,
        )
        if range_err:
            print(f"[CONFIG 실패] write range: {range_err}")
            sys.exit(1)

        # ensure table created
        print("[1] ensure source_daily_rates table created (checkfirst=True, idempotent)...")
        try:
            ensure_source_daily_rates_table_created()
        except Exception as e:
            print(f"[TABLE 실패] {type(e).__name__}: {e}")
            sys.exit(1)
        print("  OK")
        print()

        # calendar-day loop + basis_date dedup
        print(f"[2] Calendar-day loop ({args.start_date} ~ {args.end_date}) + basis_date dedup...")
        rows_to_write, fetch_errors, fallback_events = fetch_and_dedup_calendar_range(
            args.currency, args.start_date, args.end_date,
        )
        calendar_days = (args.end_date - args.start_date).days + 1
        print(f"  calendar days: {calendar_days}")
        print(f"  fetch errors: {len(fetch_errors)}")
        print(f"  fallback events (request_date != basis_date): {len(fallback_events)}")
        for req_d, basis_d in fallback_events[:10]:
            print(f"    - {req_d.isoformat()} → basis_date={basis_d.isoformat()}")
        print(f"  rows_to_write (basis_date dedup): {len(rows_to_write)}")
        print()

        if fetch_errors:
            print(f"[FETCH/PARSE 실패] {len(fetch_errors)}건 — write 진입 차단")
            for err in fetch_errors[:5]:
                print(f"  - {err}")
            sys.exit(1)

        if not rows_to_write:
            print("[WRITE SKIP] rows_to_write 0개")
            sys.exit(1)

        # transaction write
        print(f"[3] write {len(rows_to_write)} rows → source_daily_rates (source=hana asset={ASSET_MAP[args.currency]})...")
        success, write_issues = write_with_transaction_hana(
            rows_to_write, args.currency, args.start_date, args.end_date,
        )
        if success:
            print(f"[Hana write 완료] {len(rows_to_write)} rows committed + post-write validations passed")
            print()
            print("[Rollback anchor] cleanup (Hana는 calendar-day dedup으로 expected_dates 비연속 — 개별 삭제가 안전):")
            asset_str = ASSET_MAP[args.currency]
            sorted_dates = sorted(r["date_kst"] for r in rows_to_write)
            dates_repr = ", ".join(f"date({d.year}, {d.month}, {d.day})" for d in sorted_dates)
            print(
                f"  from app.database import SessionLocal; from app.models import SourceDailyRate; "
                f"from datetime import date; db = SessionLocal(); "
                f"db.query(SourceDailyRate).filter("
                f"SourceDailyRate.source == 'hana', "
                f"SourceDailyRate.asset == '{asset_str}', "
                f"SourceDailyRate.date_kst.in_([{dates_repr}])"
                f").delete(synchronize_session=False); db.commit()"
            )
            print()
            print("  주의: range delete (delete_range) 사용 시 비연속 expected_dates 사이 휴일 gap 안")
            print("  다른 Hana row가 있으면 같이 삭제될 수 있음. 개별 dates IN delete가 정확.")
            print()
            print("[Stage 2] commit 별 GO / Stage 3 production execution 별 GO.")
        else:
            print(f"[Hana write 실패] {len(write_issues)}건 issue — transaction rollback 완료")
            for issue in write_issues[:5]:
                print(f"  - {issue}")
            if len(write_issues) > 5:
                print(f"  ... 외 {len(write_issues) - 5}건")
            sys.exit(1)
        return  # write mode end

    # 1. Fetch (RequestException → [FETCH 실패] + exit 1)
    print("[1] Hana endpoint fetch...")
    try:
        html = fetch_html(args.currency, request_date)
    except requests.RequestException as e:
        print(f"\n[FETCH 실패] {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"    response size: {len(html)} bytes")
    print()

    # 2. Parse (ValueError/AttributeError/InvalidOperation → [PARSE 실패] + exit 1)
    print("[2] HTML parse...")
    try:
        parsed = parse_response(html)
    except (ValueError, AttributeError, InvalidOperation) as e:
        print(f"\n[PARSE 실패] {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"    basis_date: {parsed['basis_date'].isoformat()}")
    print(f"    published_at: {parsed['published_at'].isoformat()}")
    print(f"    pbldSqn: {parsed['pbld_sqn']} (회차 — provenance metadata, 공식 단정 X)")
    print(f"    매매기준율 (idx 7): {parsed['rate_dec']}")
    print(f"    txtAr cell count: {parsed['txt_ar_count']}")
    print()

    # 3. Build row
    print("[3] row dict 생성 (upsert() 호출 X — Step 2/3 경계 보존)...")
    row = build_row(parsed, request_date, args.currency)
    print(json.dumps(_printable(row), ensure_ascii=False, indent=2))
    print()

    # 4. Validations
    print("=" * 60)
    print("Validations")
    print("=" * 60)

    fallback_issues = validate_fallback_signal(row, request_date)
    total_issues = 0

    # fallback signal: 단독 표시 (label + 정보)
    if fallback_issues:
        total_issues += len(fallback_issues)
        print(f"\n[fallback signal]")
        for issue in fallback_issues:
            print(f"  - {issue}")
    else:
        fb = row["metadata_json"]["fallback"]
        suffix = "(휴일 fallback)" if fb else "(영업일 — 요청일 == basis_date)"
        print(
            f"\n[fallback signal] OK fallback={fb} "
            f"(request_date={request_date.isoformat()}, basis_date={row['basis_date'].isoformat()}) {suffix}"
        )

    # txtAr cell count (DOM 변경 monitoring, parsed dict 인자)
    txt_ar_issues = validate_txt_ar_count(parsed)
    if txt_ar_issues:
        total_issues += len(txt_ar_issues)
        print(f"\n[txtAr cell count (DOM monitoring)] {len(txt_ar_issues)}건")
        for issue in txt_ar_issues:
            print(f"  - {issue}")
    else:
        print(
            f"\n[txtAr cell count (DOM monitoring)] OK ({parsed['txt_ar_count']} cells, >=8)"
        )

    checks = [
        ("rate == close invariant", validate_invariant),
        ("close_only fallback (high=low=close + ohlc_quality)", validate_close_only),
        ("date_kst == basis_date", validate_date_kst_basis_date),
        (
            "metadata policy (Hana 필수: basis_date / published_at / pbldSqn / contract_code=None)",
            validate_metadata_policy,
        ),
        ("published_at KST tzinfo + offset +09:00", validate_published_at_kst),
        ("close_basis / source_method enum", validate_close_basis_method),
        ("OHLC non-positive / high<low", validate_ohlc_positive),
        ("Decimal(14, 6) precision", validate_decimal_precision),
    ]

    for name, fn in checks:
        issues = fn(row)
        if issues:
            total_issues += len(issues)
            print(f"\n[{name}] {len(issues)}건")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print(f"\n[{name}] OK (0건)")

    print()
    print("=" * 60)
    if total_issues == 0:
        print(f"[DRY-RUN 완료] 모든 validation 통과 (DB write 안 됨)")
    else:
        print(f"[DRY-RUN 실패] 총 {total_issues}건 이슈 (DB write 안 됨)")
    print()
    print("[다음 단계] Step 3 partial backfill 실측 적재는 별도 PR (예: KRX 먼저).")

    if total_issues > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
