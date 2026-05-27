#!/usr/bin/env python3
"""
Bithumb USDT/KRW 24h candlestick → source_daily_rates dry-run validator.

ADR-034 Phase 2d Step 2 (각 source 별 backfill job 작성 + dry-run 모드, 실제 INSERT X).

목적:
  - Bithumb /public/candlestick/USDT_KRW/24h API 호출
  - OCHL raw schema parse: [ts_ms, open, close, high, low, volume]
    (ccxt parse_ohlcv 참조, ADR-033 Amendment 1 Decision 2-1 명시)
  - source_daily_rates row dict 생성 (upsert() 호출 X — Step 2/3 경계 보존)
  - 9개 validation 수행

Validations (Codex 7개 + Claude 보완 2개):
  1. anchor (first/last candle KST timestamp가 00:00:00+09:00인지)
  2. rate == close invariant
  3. duplicate date_kst
  4. date gap (expected = (last - first).days + 1)
  5. OHLC non-positive / high < low
  6. Decimal(14, 6) precision (소수부 6자리 초과)
  7. sort order (ascending / descending / mixed)
  8. published_at=None, candle timestamp는 metadata_json에만
  9. raw API shape (first/last raw row)

ADR-033 Amendment 1 Decision 2-1:
  - URL: api.bithumb.com/public/candlestick/USDT_KRW/24h
  - 무인증, 902일 coverage (2023-12-07 KST 시작)
  - raw schema 비표준 OCHL: [ts_ms, open, close, high, low, volume]
  - close_basis="bithumb_24h_kst_close"
  - source_method="bithumb_candlestick_backfill"
  - ohlc_quality="source_ohlc"

사용법:
  python scripts/backfill_bithumb_source_daily_rates.py [--limit N]

주의:
  - 본 script는 dry-run only. DB write 절대 X.
  - app.source_daily_rates.upsert()를 호출하지 않음 (Step 2/3 경계 보존).
  - row dict 생성 + validation 후 결과 출력만.
  - Step 3 (partial backfill 실측 적재)은 별도 PR.
"""

# 표준 라이브러리
import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

# 서드파티 라이브러리
import requests

# 프로젝트 루트를 sys.path에 추가 (현재는 import 없지만 일관성 유지)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


KST = ZoneInfo("Asia/Seoul")
ENDPOINT = "https://api.bithumb.com/public/candlestick/USDT_KRW/24h"


# ─────────────────────────────────────────────────────────────
# Fetch + Parse
# ─────────────────────────────────────────────────────────────

def fetch_candles(timeout: float = 30.0) -> list[list]:
    """Bithumb 24h candlestick API fetch → raw OCHL list 반환.

    Response shape: {"status": "0000", "data": [[ts_ms, open, close, high, low, volume], ...]}
    """
    response = requests.get(ENDPOINT, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    status = payload.get("status")
    if status != "0000":
        raise RuntimeError(f"Bithumb API 비정상 응답: status={status}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError(f"Bithumb data 형식 이상: type={type(data).__name__}")
    return data


def parse_candle(candle: list) -> dict:
    """raw OCHL row → source_daily_rates row dict.

    OCHL schema: [ts_ms, open, close, high, low, volume] (정확히 6개).

    주의: upsert() 호출하지 않음. row dict 생성만 (Step 2/3 경계 보존).
    """
    if not isinstance(candle, list):
        raise ValueError(f"OCHL row이 list 아님: type={type(candle).__name__}")
    if len(candle) != 6:
        raise ValueError(f"OCHL row 길이 != 6: len={len(candle)} raw={candle}")

    ts_ms_raw = candle[0]
    open_, close, high, low, volume = candle[1], candle[2], candle[3], candle[4], candle[5]

    # ts_ms는 정수 epoch ms 가정 (str/float도 int() 변환 시도)
    ts_ms = int(ts_ms_raw)

    # Bithumb 응답은 string 또는 float — Decimal(str(...))로 일관 변환
    close_dec = Decimal(str(close))
    high_dec = Decimal(str(high))
    low_dec = Decimal(str(low))
    open_dec = Decimal(str(open_))
    volume_dec = Decimal(str(volume))

    # ts_ms (UTC epoch ms) → KST datetime
    ts_kst = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(KST)
    date_kst = ts_kst.date()

    return {
        "source": "bithumb",
        "asset": "usdt-krw",
        "date_kst": date_kst,
        "rate": close_dec,  # invariant: rate == close
        "high": high_dec,
        "low": low_dec,
        "close": close_dec,
        "ohlc_quality": "source_ohlc",
        "close_basis": "bithumb_24h_kst_close",
        "source_method": "bithumb_candlestick_backfill",
        "contract_code": None,
        "basis_date": None,
        # published_at은 ADR-034 §3 schema 정의로는 Hana official 발표 timestamp 한정.
        # Bithumb candle close timestamp는 metadata_json으로 격리 (schema 의미 오염 방지).
        "published_at": None,
        "metadata_json": {
            "candle_ts_ms": ts_ms,
            "candle_ts_kst": ts_kst.isoformat(),
            "open": str(open_dec),
            "volume": str(volume_dec),
            "endpoint": ENDPOINT,
            "fetched_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        },
    }


def parse_all_candles(candles: list[list]) -> tuple[list[dict], list[str]]:
    """모든 candle parse 시도, 성공 row + 실패 issue 분리 반환.

    parse_candle 자체는 raise 정책 유지 (raw shape spec 위반은 명확한 fail).
    여기서 try/except로 감싸서 한 row 실패해도 전체 통계는 보존 (dry-run validator 패턴).
    """
    rows = []
    parse_issues = []
    for idx, candle in enumerate(candles):
        try:
            rows.append(parse_candle(candle))
        except (ValueError, TypeError, ArithmeticError, KeyError, IndexError) as e:
            raw_repr = candle if isinstance(candle, list) else type(candle).__name__
            parse_issues.append(
                f"idx={idx} {type(e).__name__}: {e} (raw={raw_repr})"
            )
    return rows, parse_issues


# ─────────────────────────────────────────────────────────────
# Validations
# ─────────────────────────────────────────────────────────────

def validate_anchor(rows: list[dict]) -> list[str]:
    """모든 row의 KST timestamp가 00:00:00+09:00인지 확인.

    ADR-034 §6 bithumb_24h_kst_close 정책: KST 24:00 close anchor.
    Bithumb 24h candle ts_ms가 KST 00:00 anchor면 date_kst 변환 자연 일치.
    first/last만이 아니라 중간 row가 UTC 00:00 등으로 섞여도 catch.
    """
    issues = []
    for idx, row in enumerate(rows):
        ts_kst = datetime.fromisoformat(row["metadata_json"]["candle_ts_kst"])
        if ts_kst.hour != 0 or ts_kst.minute != 0 or ts_kst.second != 0:
            issues.append(
                f"idx={idx} date_kst={row['date_kst'].isoformat()} "
                f"KST timestamp가 00:00:00이 아님: {ts_kst.isoformat()}"
            )
    return issues


def validate_metadata_policy(rows: list[dict]) -> list[str]:
    """nullable field policy 회귀 가드.

    Bithumb candle row dict 규칙 (ADR-034 §3 + parse_candle 정책):
      - contract_code = None (KRX 전용)
      - basis_date = None (Hana official 전용)
      - published_at = None (Hana official 발표 timestamp 한정, candle ts는 metadata_json에)
    """
    issues = []
    for idx, row in enumerate(rows):
        if row["contract_code"] is not None:
            issues.append(
                f"idx={idx} date_kst={row['date_kst'].isoformat()} "
                f"contract_code={row['contract_code']} (Bithumb은 None이어야 함)"
            )
        if row["basis_date"] is not None:
            issues.append(
                f"idx={idx} date_kst={row['date_kst'].isoformat()} "
                f"basis_date={row['basis_date']} (Bithumb은 None이어야 함)"
            )
        if row["published_at"] is not None:
            issues.append(
                f"idx={idx} date_kst={row['date_kst'].isoformat()} "
                f"published_at={row['published_at']} (Bithumb은 None — candle ts는 metadata_json에)"
            )
    return issues


def validate_invariant(rows: list[dict]) -> list[str]:
    """rate == close invariant 검증 (운영 후 find_rate_close_drift 기준선)."""
    issues = []
    for row in rows:
        if row["rate"] != row["close"]:
            issues.append(
                f"rate != close at date_kst={row['date_kst']}: "
                f"rate={row['rate']}, close={row['close']}"
            )
    return issues


def validate_duplicates(rows: list[dict]) -> list[str]:
    """duplicate date_kst 검증."""
    issues = []
    counts = Counter(row["date_kst"] for row in rows)
    for date_kst, count in counts.items():
        if count > 1:
            issues.append(f"duplicate date_kst {date_kst.isoformat()}: {count}회")
    return issues


def validate_gap(rows: list[dict]) -> list[str]:
    """date gap 검증 (expected = (last - first).days + 1)."""
    issues = []
    dates = sorted({row["date_kst"] for row in rows})
    if not dates:
        return ["row 없음 — gap 검증 skip"]
    first, last = dates[0], dates[-1]
    expected = (last - first).days + 1
    actual = len(dates)
    if expected != actual:
        all_expected = {first + timedelta(days=i) for i in range(expected)}
        missing = sorted(all_expected - set(dates))
        sample = missing[:10]
        issues.append(
            f"date gap: expected {expected}일 ({first.isoformat()} ~ {last.isoformat()}), "
            f"actual {actual}일, 누락 {expected - actual}일. 일부: "
            f"{[d.isoformat() for d in sample]}"
            + (f" 외 {len(missing) - 10}일" if len(missing) > 10 else "")
        )
    return issues


def validate_ohlc_positive(rows: list[dict]) -> list[str]:
    """non-positive OHLC + high < low 검증 (schema bug signal)."""
    issues = []
    for row in rows:
        for field in ("rate", "high", "low", "close"):
            value = row[field]
            if value is None:
                continue
            if value <= 0:
                issues.append(
                    f"non-positive {field}={value} at date_kst={row['date_kst'].isoformat()}"
                )
        high = row.get("high")
        low = row.get("low")
        if high is not None and low is not None and high < low:
            issues.append(
                f"high < low at date_kst={row['date_kst'].isoformat()}: "
                f"high={high}, low={low}"
            )
    return issues


def validate_decimal_precision(rows: list[dict]) -> list[str]:
    """Numeric(14, 6) precision: 소수부 6자리 초과 row 검출.

    Decimal('1.5').as_tuple() = (sign=0, digits=(1, 5), exponent=-1) → 소수부 1자리.
    exponent < -6 이면 소수부 7자리 이상 → Numeric(14, 6) cast 시 정밀도 손실.
    """
    issues = []
    for row in rows:
        for field in ("rate", "high", "low", "close"):
            value = row[field]
            if value is None:
                continue
            exponent = value.as_tuple().exponent
            if not isinstance(exponent, int):
                # Decimal('NaN'), Decimal('Infinity') 등 special — schema 위반
                issues.append(
                    f"{field}={value} non-finite at date_kst={row['date_kst'].isoformat()}"
                )
                continue
            if exponent < -6:
                issues.append(
                    f"{field} 소수부 {-exponent}자리 (>6) at "
                    f"date_kst={row['date_kst'].isoformat()}: {value}"
                )
    return issues


def validate_sort_order(rows: list[dict]) -> tuple[str, list[str]]:
    """sort order 라벨 + 이슈 반환.

    Returns: (order_label, issues)
      ascending / descending / mixed
    """
    dates = [row["date_kst"] for row in rows]
    if not dates:
        return "empty", []
    if dates == sorted(dates):
        return "ascending", []
    if dates == sorted(dates, reverse=True):
        return "descending", ["sort order = descending (Step 3에서 sort 재정렬 필요)"]
    return "mixed", ["sort order = mixed (unsorted) — 즉시 sort 필요"]


# ─────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────

def _printable(row: dict) -> dict:
    """row dict → JSON 직렬화 가능 형태 (Decimal/date → str)."""
    result = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            result[k] = str(v)
        elif isinstance(v, date):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


def print_summary(rows: list[dict], order_label: str) -> None:
    print(f"[총 candle 수] {len(rows)}")
    if not rows:
        return
    sorted_dates = sorted({row["date_kst"] for row in rows})
    print(f"[unique date_kst 수] {len(sorted_dates)}")
    print(f"[date range] {sorted_dates[0].isoformat()} ~ {sorted_dates[-1].isoformat()}")
    print(f"[sort order] {order_label}")
    print()


def print_sample_rows(rows: list[dict]) -> None:
    """first/middle/last sample row 출력."""
    if not rows:
        return
    n = len(rows)
    indices = [0, n // 2, n - 1] if n >= 3 else list(range(n))
    labels = ["first", "middle", "last"][: len(indices)]
    for label, idx in zip(labels, indices):
        print(f"[{label} row] (idx={idx})")
        print(json.dumps(_printable(rows[idx]), ensure_ascii=False, indent=2))
        print()


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def _positive_int(value: str) -> int:
    """argparse type validator: positive int (0/음수 거부)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--limit는 int여야 함 (입력: {value!r})")
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"--limit는 1 이상이어야 함 (입력: {parsed})")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Bithumb USDT/KRW 24h candlestick → source_daily_rates dry-run validator "
            "(ADR-034 Phase 2d Step 2)"
        )
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="최근 N개 candle만 검증 (positive int only, default: 전체)",
    )
    args = parser.parse_args()

    print(f"모드: DRY-RUN (DB write 절대 X, upsert() 호출 X)")
    print(f"Endpoint: {ENDPOINT}")
    print()

    # 1. API fetch
    print("[1] Bithumb candlestick API fetch...")
    candles = fetch_candles()
    print(f"    raw candle 수: {len(candles)}")
    if not candles:
        print("    [STOP] candle 0개 — 검증 진입 중단")
        return
    print(f"    raw[0]:  {candles[0]}")
    print(f"    raw[-1]: {candles[-1]}")
    print()

    # 2. Parse (실패한 candle은 issue로 수집, 전체 통계 보존)
    print("[2] OCHL parse → row dict 생성...")
    rows, parse_issues = parse_all_candles(candles)
    print(f"    parsed row 수: {len(rows)}")
    if parse_issues:
        print(f"    parse 실패: {len(parse_issues)}건")
    print()

    # 3. limit (date_kst sort 후 tail)
    if args.limit is not None:
        rows_sorted = sorted(rows, key=lambda r: r["date_kst"])
        rows = rows_sorted[-args.limit:]
        print(f"[limit] 최근 {args.limit}개로 축소 → {len(rows)}개")
        print()

    # 4. Summary + sort order label
    order_label, sort_issues = validate_sort_order(rows)
    print_summary(rows, order_label)

    # 5. Sample rows
    print_sample_rows(rows)

    # 6. Validations
    print("=" * 60)
    print("Validations")
    print("=" * 60)

    checks = [
        ("anchor (KST 00:00:00+09:00)", validate_anchor),
        ("rate == close invariant", validate_invariant),
        ("duplicate date_kst", validate_duplicates),
        ("date gap", validate_gap),
        ("OHLC non-positive / high < low", validate_ohlc_positive),
        ("Decimal(14, 6) precision", validate_decimal_precision),
        ("metadata policy (contract_code/basis_date/published_at = None)", validate_metadata_policy),
    ]

    total_issues = 0

    # parse 실패 (issue로 수집된 경우)
    if parse_issues:
        total_issues += len(parse_issues)
        print(f"\n[parse 실패] {len(parse_issues)}건")
        for issue in parse_issues[:5]:
            print(f"  - {issue}")
        if len(parse_issues) > 5:
            print(f"  ... 외 {len(parse_issues) - 5}건")
    else:
        print(f"\n[parse 실패] OK (0건)")

    # sort order는 별도 출력 (라벨 + 이슈 분리)
    if sort_issues:
        total_issues += len(sort_issues)
        print(f"\n[sort order]")
        for issue in sort_issues:
            print(f"  - {issue}")
    else:
        print(f"\n[sort order] OK ({order_label})")

    for name, fn in checks:
        issues = fn(rows)
        if issues:
            total_issues += len(issues)
            print(f"\n[{name}] {len(issues)}건 발견")
            for issue in issues[:5]:
                print(f"  - {issue}")
            if len(issues) > 5:
                print(f"  ... 외 {len(issues) - 5}건")
        else:
            print(f"\n[{name}] OK (0건)")

    print()
    print("=" * 60)
    if total_issues == 0:
        print(f"[DRY-RUN 완료] 모든 validation 통과. row {len(rows)}개 (DB write 안 됨)")
    else:
        print(f"[DRY-RUN 실패] 총 {total_issues}건 이슈 발견. row {len(rows)}개 (DB write 안 됨)")
    print()
    print("[다음 단계] Step 3 partial backfill 실측 적재는 별도 PR (예: KRX 먼저).")

    # validation issue 있으면 exit 1 (CI/cron/shell pipe sentinel)
    if total_issues > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
