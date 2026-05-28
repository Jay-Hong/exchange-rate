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
        help="YYYY-MM-DD (default: 오늘 KST). 휴일 요청 시 응답 기준일이 직전 영업일로 fallback",
    )
    args = parser.parse_args()

    request_date = args.date or datetime.now(tz=KST).date()

    print(f"모드: DRY-RUN (DB write X, upsert() 호출 X)")
    print(f"Endpoint: {ENDPOINT}")
    print(f"Currency: {args.currency} (asset={ASSET_MAP[args.currency]})")
    print(f"Request date: {request_date.isoformat()}")
    print()

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
