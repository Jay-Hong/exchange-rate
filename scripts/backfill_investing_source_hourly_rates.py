#!/usr/bin/env python3
"""Investing per-currency hourly rollup validator/writer (ADR-035 D3, Step 2/3).

`investing_exchange_rates`(우리 DB 장기 raw, 기준 환율) per-currency 관측을 KST 1h bucket으로
rollup한 source_hourly_rates candidate를 만들고 schema/provenance 정합성을 검증. **default dry-run, `--write` 시 통화별 적재**(B의 파라미터화된 write_with_transaction 재사용).

Bithumb hourly(`backfill_bithumb_source_hourly_rates.py`)와 차이:
  - **per-currency 3 asset**: usd-krw / jpy-krw / eur-krw (`--currency usd|jpy|eur|all`)
  - **quantize**: investing_exchange_rates는 Float라 JPY per-100 연산 artifact(921.6100000000001 류) →
    `Decimal(str(rate)).quantize(0.000001)` (daily Investing writer 동형, OHLC ordering 보존: max/min raw 비교 후 결과만 quantize)
  - **주말/FX 휴장 gap** 정상 발생 (Bithumb 24/7 gap 0과 다른 첫 사례) → requested-window gap 진단이 surface
generic validate suite / compute_gap_report / evaluate_dry_run / kst_date_range_to_utc는 Bithumb hourly script 재사용.

provenance: close_basis=`investing_observed_hourly` / source_method=`observed_rollup` / ohlc_quality=`observed_rollup`.

사용법:
  # dry-run (read-only, default):
  python scripts/backfill_investing_source_hourly_rates.py --currency all --start-date 2026-06-01 --end-date 2026-06-07
  # write (Step 3 — production DB는 --allow-production-write):
  python scripts/backfill_investing_source_hourly_rates.py --currency all --start-date 2026-06-01 --end-date 2026-06-07 --write --require-empty-target --allow-production-write
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
# main (per-currency loop — dry-run / --write 적재)
# ─────────────────────────────────────────────────────────────

def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Investing per-currency hourly rollup validator/writer (ADR-035 D3 Step 2/3 — default dry-run, --write 적재)"
    )
    parser.add_argument("--currency", default="all", help="usd|jpy|eur|all (기본 all)")
    parser.add_argument("--start-date", required=True, help="KST 시작일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", required=True, help="KST 종료일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--write", action="store_true",
                        help="[Step 3] dry-run 통과 시 통화별 transaction 적재 (default: dry-run only)")
    parser.add_argument("--allow-production-write", action="store_true",
                        help="[guard] non-SQLite DB(RDS 등)에 --write 허용")
    parser.add_argument("--require-empty-target", action="store_true",
                        help="[Step 3] write 전 통화별 target bucket 비어있어야 함 (initial backfill gap-only)")
    parser.add_argument("--include-today", action="store_true",
                        help="[Step 3] end_date에 오늘 포함 허용 (default: 오늘 미완성 hour 회피로 reject)")
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

    # write 진입 가드 (DB read 전)
    if args.write:
        today_kst = datetime.now(_KST_TZ).date()
        if not args.include_today and end_date >= today_kst:
            print(f"[CONFIG 실패] --write인데 end_date={end_date} >= today={today_kst} "
                  "(오늘 미완성 hour 위험). --include-today 또는 today-1 이하로 제한")
            sys.exit(2)
        guard_err = B.check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[GUARD 차단] {guard_err}")
            sys.exit(2)
        B.ensure_source_hourly_rates_table_created()

    start_utc, end_utc = B.kst_date_range_to_utc(start_date, end_date)
    window_start = datetime.combine(start_date, time.min)   # KST start 00:00
    window_end = datetime.combine(end_date, time(23, 0))    # KST end 23:00

    mode_label = "WRITE (통화별 적재)" if args.write else "DRY-RUN (write 0)"
    print(f"모드: {mode_label} — Investing per-currency hourly rollup")
    print(f"통화: {currencies} / 범위 (KST): {start_date} ~ {end_date}")
    print(f"조회 (UTC naive): {start_utc} ~ {end_utc}")

    from app.database import SessionLocal
    total_issues = 0
    any_empty = False
    per_currency_rows = {}   # asset → rows (plan=write 동일 snapshot — double-fetch 회피)
    for cur in currencies:
        asset = _CURRENCY_MAP[cur]
        db = SessionLocal()
        try:
            obs = fetch_investing_observations(db, start_utc, end_utc, asset)
        finally:
            db.close()
        rows = rollup_to_hourly(obs, asset)
        per_currency_rows[asset] = rows

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
    if not args.write:
        print(f"[DRY-RUN OK] 모든 통화 validation 통과. write/cron은 별 GO (Step 3~).")
        return

    # WRITE: 통화별 transaction (한 통화 실패가 다른 통화 rollback 안 함 — per-currency 독립)
    # rollback anchor 출력 (이미 commit된 asset만 — 동일 작업 창 내 즉시 rollback 시에만)
    def _emit_rollback_anchor(assets):
        if not assets:
            return
        print("=== rollback anchor (이미 commit된 통화, 동일 작업 창 내 즉시 rollback 시에만) ===")
        print("  from datetime import datetime; from app.database import SessionLocal")
        print("  from app.source_hourly_rates import delete_range; db = SessionLocal()")
        for asset in assets:
            print(f'  delete_range(db, "{SOURCE}", "{asset}", '
                  f'datetime.fromisoformat("{window_start.isoformat()}"), '
                  f'datetime.fromisoformat("{window_end.isoformat()}"))')
        print("  주의: range delete 안전성 = 동일 작업 창 + 교집합 write 부재일 때만. 종료 후엔 snapshot 복구.")

    print(f"\n=== WRITE (require_empty={args.require_empty_target}) — 통화별 transaction ===")
    committed_assets = []   # per-currency 독립 commit — 실패 시 이미 commit된 통화 복구 anchor 필요
    for cur in currencies:
        asset = _CURRENCY_MAP[cur]
        rows = per_currency_rows[asset]
        success, issues, outcome = B.write_with_transaction(
            rows, args.require_empty_target,
            source=SOURCE, asset=asset, close_basis=CLOSE_BASIS,
            source_method=SOURCE_METHOD, ohlc_quality=OHLC_QUALITY)
        if not success:
            print(f"  [{cur} WRITE 실패] {len(issues)}건 issue — 이 통화 transaction rollback")
            for issue in issues[:10]:
                print(f"    - {issue}")
            if committed_assets:
                print(f"\n  ⚠️ 이미 commit된 통화 {committed_assets} — 아래 anchor로 복구 필요:")
                _emit_rollback_anchor(committed_assets)
            sys.exit(1)
        committed_assets.append(asset)
        print(f"  [{cur} UPSERT OK] inserted={outcome['inserted']} updated={outcome['updated']} (commit)")

    print()
    _emit_rollback_anchor(committed_assets)
    print()
    print("[WRITE 완료] retention prune은 append 단계 / cron 연결은 별 GO.")


if __name__ == "__main__":
    main()
