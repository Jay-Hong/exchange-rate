#!/usr/bin/env python3
"""Hana per-currency hourly rollup dry-run validator (ADR-035 D3, Step 2 level).

`bank_exchange_rates`(bank="hana", 우리 DB 은행 고시값) per-currency 관측을 KST 1h bucket으로
rollup한 source_hourly_rates candidate를 만들고 schema/provenance 정합성을 검증. **write 0 (dry-run)**.

Investing hourly(`backfill_investing_source_hourly_rates.py`)와 차이:
  - **provider**: `BankExchangeRate`(bank="hana", currency) — 은행 고시값(change-only)
  - **ohlc_quality 분기**: hour 내 change >1 → `observed_rollup` / ==1(single-tick) → `close_only`
    (Hana는 source OHLC 없음 — bank_exchange_rates 단일 rate값 시계열. Investing/Bithumb은 항상 dense라 observed_rollup만)
  - **quantize 불필요**: 은행 고시값은 clean(daily Hana도 `Decimal(str())` no-quantize). precision은 validation으로 확인
  - **carry-forward 후보 surface (적용 보류)**: 무변동 hour(within-span 빈 hour)는 "고시 step function"
    의미론상 carry-forward 후보일 수 있으나 **이 validator는 집계만** — 적용 여부는 dry-run 분포 + 의미론 검토 후 별도 결정
generic validate suite / compute_gap_report / evaluate_dry_run / kst_date_range_to_utc는 Bithumb hourly script 재사용.

provenance: close_basis=`hana_observed_hourly` / source_method=`observed_rollup` / ohlc_quality=observed_rollup|close_only.

사용법 (read-only, write 0):
  python scripts/backfill_hana_source_hourly_rates.py --currency all --start-date 2026-06-01 --end-date 2026-06-07
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
from app.models import BankExchangeRate  # noqa: E402
from app.source_hourly_rates import floor_bucket_ts_kst  # noqa: E402

SOURCE = "hana"
BANK_NAME = "hana"
CLOSE_BASIS = "hana_observed_hourly"
SOURCE_METHOD = "observed_rollup"
OHLC_QUALITY_MULTI = "observed_rollup"   # hour 내 change >1
OHLC_QUALITY_SINGLE = "close_only"       # single-tick hour (high=low=close)
_KST_TZ = timezone(timedelta(hours=9))
_CURRENCY_MAP = {"usd": "usd-krw", "jpy": "jpy-krw", "eur": "eur-krw"}
_ALLOWED_CLOSE_BASIS = frozenset({CLOSE_BASIS})
_ALLOWED_SOURCE_METHOD = frozenset({SOURCE_METHOD})
_ALLOWED_OHLC_QUALITY = frozenset({OHLC_QUALITY_MULTI, OHLC_QUALITY_SINGLE})


def _utc_naive_to_kst_iso(ts: datetime) -> str:
    return ts.replace(tzinfo=timezone.utc).astimezone(_KST_TZ).isoformat()


def resolve_currencies(arg: str) -> list[str]:
    if arg == "all":
        return ["usd", "jpy", "eur"]
    if arg not in _CURRENCY_MAP:
        raise ValueError(f"--currency는 usd|jpy|eur|all 중 하나여야 함 (got {arg})")
    return [arg]


# ─────────────────────────────────────────────────────────────
# Fetch + rollup (per currency)
# ─────────────────────────────────────────────────────────────

def fetch_hana_observations(db, start_utc: datetime, end_utc: datetime, asset: str) -> list[tuple]:
    """bank_exchange_rates(bank="hana", currency=asset) [start_utc, end_utc] 조회 → (rate, ts) timestamp ASC."""
    rows = (
        db.query(BankExchangeRate.rate, BankExchangeRate.timestamp)
        .filter(
            BankExchangeRate.bank == BANK_NAME,
            BankExchangeRate.currency == asset,
            BankExchangeRate.timestamp >= start_utc,
            BankExchangeRate.timestamp <= end_utc,
        )
        .order_by(BankExchangeRate.timestamp.asc(), BankExchangeRate.id.asc())
        .all()
    )
    return [(r.rate, r.timestamp) for r in rows]


def rollup_to_hourly(observations: list[tuple], asset: str) -> list[dict]:
    """(rate, ts_utc_naive) ASC 관측 → KST 1h bucket candidate rows.

    close = bucket 마지막 관측 / high·low = max·min. **ohlc_quality 분기**: change >1 → observed_rollup /
    ==1 → close_only(high=low=close). quantize 없음(고시값 clean — daily Hana 동형). carry-forward 없음(hour 내 관측만).
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
        close_dec = Decimal(str(b["last_rate"]))
        high_dec = max(Decimal(str(r)) for r in b["rates"])
        low_dec = min(Decimal(str(r)) for r in b["rates"])
        ohlc_q = OHLC_QUALITY_MULTI if len(b["rates"]) > 1 else OHLC_QUALITY_SINGLE
        rows.append({
            "source": SOURCE,
            "asset": asset,
            "bucket_ts_kst": bucket,
            "rate": close_dec,
            "close": close_dec,
            "high": high_dec,
            "low": low_dec,
            "ohlc_quality": ohlc_q,
            "close_basis": CLOSE_BASIS,
            "source_method": SOURCE_METHOD,
            "metadata_json": {
                "source_table": "bank_exchange_rates",
                "point_count": len(b["rates"]),
                "first_ts_kst": _utc_naive_to_kst_iso(b["first_ts"]),
                "last_ts_kst": _utc_naive_to_kst_iso(b["last_ts"]),
            },
        })
    return rows


def validate_enum(rows: list[dict]) -> list[str]:
    """Hana close_basis / source_method / ohlc_quality allowlist 잠금 (ohlc_quality 2값 허용)."""
    issues = []
    for r in rows:
        if r["close_basis"] not in _ALLOWED_CLOSE_BASIS:
            issues.append(f"close_basis 위반 @ {r['bucket_ts_kst']}: {r['close_basis']}")
        if r["source_method"] not in _ALLOWED_SOURCE_METHOD:
            issues.append(f"source_method 위반 @ {r['bucket_ts_kst']}: {r['source_method']}")
        if r["ohlc_quality"] not in _ALLOWED_OHLC_QUALITY:
            issues.append(f"ohlc_quality 위반 @ {r['bucket_ts_kst']}: {r['ohlc_quality']}")
    return issues


def validate_decimal_precision(rows: list[dict]) -> list[str]:
    """Numeric(14, 6) precision (소수부 6자리 초과 검출) — daily Hana 동형. no-quantize라 dry-run 방어."""
    issues = []
    for r in rows:
        for field in ("rate", "high", "low", "close"):
            value = r[field]
            if value is None:
                continue
            exponent = value.as_tuple().exponent
            if not isinstance(exponent, int):
                issues.append(f"{field}={value} non-finite @ {r['bucket_ts_kst']}")
                continue
            if exponent < -6:
                issues.append(f"{field} 소수부 {-exponent}자리 (>6) @ {r['bucket_ts_kst']}: {value}")
    return issues


def compute_carry_forward_stats(rows: list[dict]) -> dict:
    """carry-forward 정책 결정용 분포 집계 (적용 안 함 — surface only).

    within_span_gaps = 첫 bucket~마지막 bucket 사이 빈 hour (무변동 = carry-forward 후보).
    single_tick = point_count==1 (close_only) bucket 수.
    """
    if not rows:
        return {"buckets": 0, "span_hours": 0, "within_span_gaps": 0, "single_tick": 0}
    ts_sorted = sorted(r["bucket_ts_kst"] for r in rows)
    span_hours = int((ts_sorted[-1] - ts_sorted[0]).total_seconds() // 3600) + 1
    return {
        "buckets": len(rows),
        "span_hours": span_hours,
        "within_span_gaps": span_hours - len(rows),   # carry-forward 후보 (active span 내 무변동 hour)
        "single_tick": sum(1 for r in rows if r["metadata_json"]["point_count"] == 1),
    }


# generic validate suite는 Bithumb hourly script 재사용 (rows dict 기반, source-agnostic)
VALIDATIONS = [
    ("invariant (rate==close)", B.validate_invariant),
    ("OHLC ordering (low<=close<=high)", B.validate_ohlc_ordering),
    ("OHLC positive", B.validate_ohlc_positive),
    ("Numeric(14,6) precision (소수부 6자리)", validate_decimal_precision),
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
        description="Hana per-currency hourly rollup dry-run validator (ADR-035 D3 Step 2, write 0)"
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

    print(f"모드: DRY-RUN (write 0) — Hana per-currency hourly rollup")
    print(f"통화: {currencies} / 범위 (KST): {start_date} ~ {end_date}")
    print(f"조회 (UTC naive): {start_utc} ~ {end_utc}")

    from app.database import SessionLocal
    total_issues = 0
    any_empty = False
    for cur in currencies:
        asset = _CURRENCY_MAP[cur]
        db = SessionLocal()
        try:
            obs = fetch_hana_observations(db, start_utc, end_utc, asset)
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
        cf = compute_carry_forward_stats(rows)
        density = len(obs) / cf["buckets"] if cf["buckets"] else 0
        print(f"  gap (requested window): {gap.get('span_hours')}h / bucket {gap['bucket_count']} "
              f"/ gap_count {gap['gap_count']}")
        print(f"  carry-forward 분포 (적용 보류): active span {cf['span_hours']}h / bucket {cf['buckets']} "
              f"/ within-span 빈 hour(=carry-forward 후보) {cf['within_span_gaps']} "
              f"/ single-tick(close_only) {cf['single_tick']} / density avg {density:.1f} changes/h")
        ok, msg = B.evaluate_dry_run(rows, cur_issues)
        print(f"  [{cur} {'OK' if ok else 'FAIL'}] {msg}")
        total_issues += cur_issues
        if not rows:
            any_empty = True

    print()
    if total_issues > 0 or any_empty:
        print(f"[DRY-RUN FAIL] validation issue {total_issues}건 / 빈 통화 {any_empty} — 적재 전 정정")
        sys.exit(1)
    print(f"[DRY-RUN OK] 모든 통화 validation 통과. carry-forward 정책은 위 분포 + 의미론 검토 후 결정. write/cron은 별 GO.")


if __name__ == "__main__":
    main()
