#!/usr/bin/env python3
"""Investing per-currency hourly rollup dry-run validator (ADR-035 D3, Step 2 level).

`investing_exchange_rates`(우리 DB 장기 raw, 기준 환율) per-currency 관측을 KST 1h bucket으로
rollup한 source_hourly_rates candidate를 만들고 schema/provenance 정합성을 검증. **write 0 (dry-run)**.

Bithumb hourly(`backfill_bithumb_source_hourly_rates.py`)와 차이:
  - **per-currency 3 asset**: usd-krw / jpy-krw / eur-krw (`--currency usd|jpy|eur|all`)
  - **quantize**: investing_exchange_rates는 Float라 JPY per-100 연산 artifact(921.6100000000001 류) →
    `Decimal(str(rate)).quantize(0.000001)` (daily Investing writer 동형, OHLC ordering 보존: max/min raw 비교 후 결과만 quantize)
  - **주말/FX 휴장 gap** 정상 발생 (Bithumb 24/7 gap 0과 다른 첫 사례) → requested-window gap 진단이 surface
generic validate suite / compute_gap_report / evaluate_dry_run / kst_date_range_to_utc는 Bithumb hourly script 재사용.

provenance: close_basis=`investing_observed_hourly` / source_method=`observed_rollup` / ohlc_quality=`observed_rollup`.

사용법 (read-only, write 0):
  python scripts/backfill_investing_source_hourly_rates.py --currency all --start-date 2026-06-01 --end-date 2026-06-07
"""

# 표준 라이브러리
import argparse
import os
import sys
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

# 프로젝트 루트 + scripts (B 재사용)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _SCRIPT_DIR)

# 로컬 애플리케이션
import backfill_bithumb_source_hourly_rates as B  # noqa: E402  generic validate/gap/verdict 재사용
from app.models import InvestingExchangeRate  # noqa: E402
from app.source_hourly_rates import floor_bucket_ts_kst  # noqa: E402

SOURCE = "investing"
CLOSE_BASIS = "investing_observed_hourly"
SOURCE_METHOD = "observed_rollup"
OHLC_QUALITY = "observed_rollup"
_QUANTIZE = Decimal("0.000001")
_KST_TZ = timezone(timedelta(hours=9))
_CURRENCY_MAP = {"usd": "usd-krw", "jpy": "jpy-krw", "eur": "eur-krw"}
_ALLOWED_CLOSE_BASIS = frozenset({CLOSE_BASIS})
_ALLOWED_SOURCE_METHOD = frozenset({SOURCE_METHOD})
_ALLOWED_OHLC_QUALITY = frozenset({OHLC_QUALITY})


def _utc_naive_to_kst_iso(ts: datetime) -> str:
    return ts.replace(tzinfo=timezone.utc).astimezone(_KST_TZ).isoformat()


def resolve_currencies(arg: str) -> list[str]:
    """--currency → currency 코드 리스트. all → [usd, jpy, eur]."""
    if arg == "all":
        return ["usd", "jpy", "eur"]
    if arg not in _CURRENCY_MAP:
        raise ValueError(f"--currency는 usd|jpy|eur|all 중 하나여야 함 (got {arg})")
    return [arg]


# ─────────────────────────────────────────────────────────────
# Fetch + rollup (per currency)
# ─────────────────────────────────────────────────────────────

def fetch_investing_observations(db, start_utc: datetime, end_utc: datetime, asset: str) -> list[tuple]:
    """investing_exchange_rates(currency=asset) [start_utc, end_utc] 조회 → (rate, ts) timestamp ASC."""
    rows = (
        db.query(InvestingExchangeRate.rate, InvestingExchangeRate.timestamp)
        .filter(
            InvestingExchangeRate.currency == asset,
            InvestingExchangeRate.timestamp >= start_utc,
            InvestingExchangeRate.timestamp <= end_utc,
        )
        .order_by(InvestingExchangeRate.timestamp.asc(), InvestingExchangeRate.id.asc())
        .all()
    )
    return [(r.rate, r.timestamp) for r in rows]


def rollup_to_hourly(observations: list[tuple], asset: str) -> list[dict]:
    """(rate, ts_utc_naive) ASC 관측 → KST 1h bucket candidate rows.

    close = bucket 마지막 관측 / high·low = max·min. **quantize(6자리)** (Investing Float artifact).
    bucket key = floor_bucket_ts_kst(ts.replace(tzinfo=utc)) — UTC naive → KST 변환 후 floor.
    """
    buckets: dict = {}
    for rate, ts in observations:
        bucket = floor_bucket_ts_kst(ts.replace(tzinfo=timezone.utc))
        b = buckets.get(bucket)
        if b is None:
            b = {"rates": [], "first_ts": ts, "last_ts": ts, "last_rate": rate}
            buckets[bucket] = b
        b["rates"].append(rate)
        b["last_ts"] = ts
        b["last_rate"] = rate

    rows = []
    for bucket in sorted(buckets):
        b = buckets[bucket]
        # max/min은 raw Decimal 비교 후 결과만 quantize (monotonic — OHLC ordering 보존)
        close_dec = Decimal(str(b["last_rate"])).quantize(_QUANTIZE)
        high_dec = max(Decimal(str(r)) for r in b["rates"]).quantize(_QUANTIZE)
        low_dec = min(Decimal(str(r)) for r in b["rates"]).quantize(_QUANTIZE)
        rows.append({
            "source": SOURCE,
            "asset": asset,
            "bucket_ts_kst": bucket,
            "rate": close_dec,
            "close": close_dec,
            "high": high_dec,
            "low": low_dec,
            "ohlc_quality": OHLC_QUALITY,
            "close_basis": CLOSE_BASIS,
            "source_method": SOURCE_METHOD,
            "metadata_json": {
                "source_table": "investing_exchange_rates",
                "point_count": len(b["rates"]),
                "first_ts_kst": _utc_naive_to_kst_iso(b["first_ts"]),
                "last_ts_kst": _utc_naive_to_kst_iso(b["last_ts"]),
            },
        })
    return rows


def validate_enum(rows: list[dict]) -> list[str]:
    """Investing close_basis / source_method / ohlc_quality allowlist 잠금."""
    issues = []
    for r in rows:
        if r["close_basis"] not in _ALLOWED_CLOSE_BASIS:
            issues.append(f"close_basis 위반 @ {r['bucket_ts_kst']}: {r['close_basis']}")
        if r["source_method"] not in _ALLOWED_SOURCE_METHOD:
            issues.append(f"source_method 위반 @ {r['bucket_ts_kst']}: {r['source_method']}")
        if r["ohlc_quality"] not in _ALLOWED_OHLC_QUALITY:
            issues.append(f"ohlc_quality 위반 @ {r['bucket_ts_kst']}: {r['ohlc_quality']}")
    return issues


# generic validate suite는 Bithumb hourly script 재사용 (rows dict 기반, source-agnostic)
VALIDATIONS = [
    ("invariant (rate==close)", B.validate_invariant),
    ("OHLC ordering (low<=close<=high)", B.validate_ohlc_ordering),
    ("OHLC positive", B.validate_ohlc_positive),
    ("duplicate bucket", B.validate_duplicates),
    ("bucket alignment (KST 정각 floor)", B.validate_bucket_alignment),
    ("enum (close_basis/source_method/ohlc_quality)", validate_enum),
    ("metadata (point_count/first/last)", B.validate_metadata),
]


# ─────────────────────────────────────────────────────────────
# main (per-currency loop, read-only — write 0)
# ─────────────────────────────────────────────────────────────

def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Investing per-currency hourly rollup dry-run validator (ADR-035 D3 Step 2, write 0)"
    )
    parser.add_argument("--currency", default="all", help="usd|jpy|eur|all (기본 all)")
    parser.add_argument("--start-date", required=True, help="KST 시작일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", required=True, help="KST 종료일 YYYY-MM-DD (inclusive)")
    args = parser.parse_args()

    try:
        currencies = resolve_currencies(args.currency)
    except ValueError as e:
        print(f"[CONFIG 실패] {e}")
        sys.exit(2)

    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if start_date > end_date:
        print(f"[ERR] start_date({start_date}) > end_date({end_date})")
        sys.exit(2)

    start_utc, end_utc = B.kst_date_range_to_utc(start_date, end_date)
    window_start = datetime.combine(start_date, time.min)   # KST start 00:00
    window_end = datetime.combine(end_date, time(23, 0))    # KST end 23:00

    print(f"모드: DRY-RUN (write 0) — Investing per-currency hourly rollup")
    print(f"통화: {currencies} / 범위 (KST): {start_date} ~ {end_date}")
    print(f"조회 (UTC naive): {start_utc} ~ {end_utc}")

    from app.database import SessionLocal
    total_issues = 0
    any_empty = False
    for cur in currencies:
        asset = _CURRENCY_MAP[cur]
        db = SessionLocal()
        try:
            obs = fetch_investing_observations(db, start_utc, end_utc, asset)
        finally:
            db.close()
        rows = rollup_to_hourly(obs, asset)

        print(f"\n=== {cur} ({asset}) ===")
        print(f"  관측: {len(obs)}건 / hourly bucket: {len(rows)}건")
        cur_issues = 0
        for name, fn in VALIDATIONS:
            issues = fn(rows)
            cur_issues += len(issues)
            mark = "OK" if not issues else f"FAIL ({len(issues)})"
            print(f"  [{mark}] {name}")
            for issue in issues[:5]:
                print(f"        - {issue}")
        gap = B.compute_gap_report(rows, window_start, window_end)
        print(f"  gap (requested window): {gap.get('span_hours')}h / bucket {gap['bucket_count']} "
              f"/ gap_count {gap['gap_count']} (주말/FX 휴장 gap 정상)")
        ok, msg = B.evaluate_dry_run(rows, cur_issues)
        print(f"  [{cur} {'OK' if ok else 'FAIL'}] {msg}")
        total_issues += cur_issues
        if not rows:
            any_empty = True

    print()
    if total_issues > 0 or any_empty:
        print(f"[DRY-RUN FAIL] validation issue {total_issues}건 / 빈 통화 {any_empty} — 적재 전 정정")
        sys.exit(1)
    print(f"[DRY-RUN OK] 모든 통화 validation 통과. write/cron은 별 GO (Step 3~).")


if __name__ == "__main__":
    main()
