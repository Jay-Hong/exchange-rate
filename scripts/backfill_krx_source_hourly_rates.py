#!/usr/bin/env python3
"""KRX 미국달러선물 source_rates → source_hourly_rates(1w) CF-only validator/writer (ADR-035 D3 Step 2/3a).

`source_rates`(krx/usd-krw-futures raw tick)를 KST 1h bucket으로 rollup한 hourly canonical
candidate row를 만들고 schema/provenance 정합성을 검증한다. **default dry-run (write 0), `--write` 시
transaction 적재 (Step 3a — production guard + in-transaction KRX post-write 검증).**

Bithumb hourly(`backfill_bithumb_source_hourly_rates.py`)와 차이:
  - **CF-only coverage**: KRX는 CF 정규장(KST 08:30:00~15:45:00)과 CM 야간(17:50~익일 06:00) 2세션.
    본 PR은 **CF 세션 tick만** rollup (KST 08:30~15:45 inclusive). CM 야간 tick은 제외.
    → CM 야간 hourly는 별 PR (close_basis는 session-agnostic `krx_observed_hourly`라 CM 추가 시 재사용).
  - **contract_code per bucket**: source_daily_rates의 이미 resolve된 KRX daily row에서 그 bucket
    date_kst의 contract_code를 재사용 (KIS API 미호출 — daily가 권위 source). daily row 부재 시
    unresolved-contract issue로 validation이 flag.
  - **metadata_json에 session 필드**: {point_count, first_ts_kst, last_ts_kst, session:"CF"}.
generic helper(floor_bucket_ts_kst / kst_date_range_to_utc / compute_gap_report / evaluate_dry_run)는
Bithumb hourly script 재사용.

provenance: close_basis=`krx_observed_hourly`(신규 — session-agnostic) / source_method=`observed_rollup`
            / ohlc_quality=`observed_rollup`.

rollup 규칙 (ADR-035 D3 provenance):
  - bucket key = floor_bucket_ts_kst(ts.replace(tzinfo=utc))  ← source_rates.timestamp는 UTC naive
  - close = bucket 마지막 관측 tick / high = max / low = min / rate == close (invariant)
  - metadata_json = {point_count, first_ts_kst, last_ts_kst, session:"CF"}

gap 정책:
  - source_rates는 change-only INSERT → 무변동 hour는 bucket 없음 (정상일 수 있음).
  - **carry-forward 안 함** — 존재 bucket만 만들고 gap은 진단으로 출력.
  - CF 세션만 cover하므로 CF 영업일 외(주말/공휴일/야간)는 자연 gap.

KRX write path은 generic source(Bithumb/Investing/Hana)와 2가지 차이로 B.write_with_transaction을
**확장 재사용** (default param이라 generic caller는 불변):
  - `allow_contract_code=True`: KRX는 front contract code를 hourly row에 보존(generic은 None 강제)
  - `post_write_validator=krx_post_write_validator`: commit 전 같은 transaction 안에서 모든 written row가
    contract_code NOT NULL + metadata session=="CF"인지 재확인 (실패 시 rollback — corrupt provenance 차단)

사용법:
  # dry-run (read-only, default):
  python scripts/backfill_krx_source_hourly_rates.py --start-date 2026-06-01 --end-date 2026-06-07
  # write (Step 3a — production DB는 --allow-production-write 추가):
  python scripts/backfill_krx_source_hourly_rates.py --start-date 2026-06-01 --end-date 2026-06-07 --write --require-empty-target
"""

# 표준 라이브러리
import argparse
import os
import sys
from datetime import date, datetime, time

# 프로젝트 루트 + scripts (B 재사용)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _SCRIPT_DIR)

# 로컬 애플리케이션
import backfill_bithumb_source_hourly_rates as B  # noqa: E402  generic gap/verdict 재사용
# CF-only rollup/validation core는 app.krx_hourly로 이동 (backfill 스크립트 + close-finalizer hook 공유).
# 아래 re-export로 모듈-레벨 `K.<name>` 참조(rollup_cf_ticks_to_hourly / fetch_daily_contract_map /
# validate_session / krx_post_write_validator / SOURCE 등)는 불변 — CLI/main/write-path 100% 동일.
from app.krx_hourly import (  # noqa: E402,F401
    ASSET,
    CLOSE_BASIS,
    OHLC_QUALITY,
    SOURCE,
    SOURCE_METHOD,
    _ALLOWED_CLOSE_BASIS,
    _ALLOWED_OHLC_QUALITY,
    _ALLOWED_SOURCE_METHOD,
    _CF_SESSION_END,
    _CF_SESSION_LABEL,
    _CF_SESSION_START,
    _KST_TZ,
    _ts_in_cf_session,
    _utc_naive_to_kst_iso,
    compute_cf_coverage,
    fetch_daily_contract_map,
    fetch_krx_ticks,
    krx_post_write_validator,
    rollup_cf_ticks_to_hourly,
    validate_contract,
    validate_decimal_precision,
    validate_enum,
    validate_session,
)

VALIDATIONS = [
    ("invariant (rate==close)", B.validate_invariant),
    ("OHLC ordering (low<=close<=high)", B.validate_ohlc_ordering),
    ("OHLC positive", B.validate_ohlc_positive),
    ("Numeric(14,6) precision (소수부 6자리)", validate_decimal_precision),
    ("duplicate bucket", B.validate_duplicates),
    ("bucket alignment (KST 정각 floor)", B.validate_bucket_alignment),
    ("enum (close_basis/source_method/ohlc_quality)", validate_enum),
    ("session (CF-only: session=CF + hour∈[8,15])", validate_session),
    ("contract (source_daily_rates resolve)", validate_contract),
    ("metadata (point_count/first/last)", B.validate_metadata),
]


# krx_post_write_validator / compute_cf_coverage 정의는 app.krx_hourly로 이동 (상단 re-export).


# ─────────────────────────────────────────────────────────────
# main (DB read-only → CF rollup → validate → 진단). DRY-RUN ONLY
# ─────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KRX 미국달러선물 source_rates → source_hourly_rates CF-only validator/writer "
                    "(ADR-035 D3 Step 2/3a — default dry-run, --write 시 적재)"
    )
    parser.add_argument("--start-date", required=True, help="KST 시작일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", required=True, help="KST 종료일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--write", action="store_true",
                        help="[Step 3a] dry-run 통과 시 transaction 적재 (default: dry-run only)")
    parser.add_argument("--allow-production-write", action="store_true",
                        help="[Step 3a guard] non-SQLite DB(RDS 등)에 --write 허용")
    parser.add_argument("--require-empty-target", action="store_true",
                        help="[Step 3a] write 전 target KRX bucket 비어있어야 함 (gap-only 강제)")
    parser.add_argument("--include-today", action="store_true",
                        help="[Step 3a] end_date에 오늘 포함 허용 (default: 오늘 미완성 hour 회피로 reject)")
    args = parser.parse_args()

    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if start_date > end_date:
        print(f"[ERR] start_date({start_date}) > end_date({end_date})")
        sys.exit(2)

    # write 진입 가드 (early — DB 조회 전, KIS API 호출 없음이라 heavy work는 DB read뿐)
    if args.write:
        today_kst = datetime.now(_KST_TZ).date()
        if not args.include_today and end_date >= today_kst:
            print(f"[CONFIG 실패] --write인데 end_date={end_date} >= today={today_kst} "
                  "(오늘 미완성 hour 위험). --include-today 명시 또는 today-1 이하로 제한")
            sys.exit(2)
        guard_err = B.check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[GUARD 차단] {guard_err}")
            sys.exit(2)
        B.ensure_source_hourly_rates_table_created()

    mode_label = "WRITE (dry-run 통과 시 적재)" if args.write else "DRY-RUN (write 0)"
    print(f"모드: {mode_label} — KRX {SOURCE}/{ASSET} CF-only hourly rollup")
    print(f"범위 (KST): {start_date} ~ {end_date}")
    start_utc, end_utc = B.kst_date_range_to_utc(start_date, end_date)
    print(f"조회 (UTC naive): {start_utc} ~ {end_utc}")
    print(f"CF 세션 window (KST): {_CF_SESSION_START} ~ {_CF_SESSION_END} (CM 야간 제외)")
    print()

    # 함수 내부 import — DB 연결 (--help 등에서 회피)
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        ticks = fetch_krx_ticks(db, start_utc, end_utc)
        contract_map = fetch_daily_contract_map(db, start_date, end_date)
    finally:
        db.close()
    print(f"raw tick (CF+CM): {len(ticks)}건 / daily contract row: {len(contract_map)}건")

    rows = rollup_cf_ticks_to_hourly(ticks, contract_map)
    print(f"CF hourly bucket: {len(rows)}건 (CM 야간 tick 제외)")
    print()

    total_issues = 0
    for name, fn in VALIDATIONS:
        issues = fn(rows)
        total_issues += len(issues)
        mark = "OK" if not issues else f"FAIL ({len(issues)})"
        print(f"  [{mark}] {name}")
        for issue in issues[:10]:
            print(f"        - {issue}")

    print()
    # CF coverage 진단 (CF 영업일 분포 — informational)
    cov = compute_cf_coverage(rows)
    print("=== CF coverage 진단 (informational) ===")
    print(f"  distinct CF dates: {cov['distinct_dates']}")
    print(f"  buckets per date: {cov['buckets_per_date']}")

    print()
    # gap 진단 = requested window 기준 (leading/trailing 포함, CF 외 시간/주말은 자연 gap)
    window_start = datetime.combine(start_date, time.min)   # KST start_date 00:00
    window_end = datetime.combine(end_date, time(23, 0))    # KST end_date 23:00
    gap = B.compute_gap_report(rows, window_start, window_end)
    print("=== gap 진단 (requested window 기준 — CF-only라 야간/주말 gap 정상) ===")
    print(f"  window: {gap['window_start']} ~ {gap['window_end']} ({gap.get('span_hours')} hours)")
    print(f"  bucket={gap['bucket_count']} / gap_count={gap['gap_count']}")

    print()
    ok, msg = B.evaluate_dry_run(rows, total_issues)
    # evaluate_dry_run의 0-bucket 메시지는 Bithumb 24/7 가정 — KRX CF-only 맥락으로 보강
    if not rows:
        msg = ("CF hourly bucket 0건 — 날짜/DB/source·asset 확인 필요 "
               "(KRX CF 영업일이 window에 포함돼야 정상)")
    if args.write:
        # --write: validation 통과 → 바로 write 진입. B의 "write는 별 GO (Step 3)" dry-run 문구 회피
        # (실제로는 write가 이어지므로 운영 로그 혼동 방지 — Codex 지적).
        print(f"[VALIDATION {'OK' if ok else 'FAIL'}] {len(rows)} bucket / {total_issues} issue (--write 진입)")
    else:
        print(f"[DRY-RUN {'OK' if ok else 'FAIL'}] {msg}")
    if not ok:
        sys.exit(1)

    if not args.write:
        print("[다음] Step 3a 적재는 --write 명시 시 진입 (production은 --allow-production-write).")
        return

    # Step 3a write (dry-run 통과 후 — atomic transaction + KRX in-transaction post-write 검증).
    # B.write_with_transaction 확장 재사용: allow_contract_code=True(contract_code 보존) +
    # post_write_validator(commit 전 contract_code NOT NULL + session=="CF" 재확인 → 실패 시 rollback).
    print()
    print(f"=== WRITE (require_empty={args.require_empty_target}) — KRX CF hourly (contract_code 보존) ===")
    success, write_issues, outcome = B.write_with_transaction(
        rows, args.require_empty_target,
        source=SOURCE, asset=ASSET, close_basis=CLOSE_BASIS,
        source_method=SOURCE_METHOD, ohlc_quality=OHLC_QUALITY,
        allow_contract_code=True,
        post_write_validator=krx_post_write_validator,
    )
    if not success:
        print(f"[WRITE 실패] {len(write_issues)}건 issue — transaction rollback 완료 (0 row 적재)")
        for issue in write_issues[:10]:
            print(f"  - {issue}")
        sys.exit(1)
    print(f"[WRITE OK] inserted={outcome['inserted']} updated={outcome['updated']} (transaction commit)")
    print()
    print("=== rollback anchor (동일 작업 창 내 즉시 rollback 시에만) ===")
    print("  from datetime import datetime")
    print("  from app.database import SessionLocal; from app.source_hourly_rates import delete_range")
    print(f'  db = SessionLocal(); delete_range(db, "{SOURCE}", "{ASSET}", '
          f'datetime.fromisoformat("{window_start.isoformat()}"), '
          f'datetime.fromisoformat("{window_end.isoformat()}"))')
    print("  주의: range delete 안전성 = gap-only 사전 확인 + 교집합 historical/manual write 부재 + 동일 작업 창 내 즉시 rollback일 때만 성립.")
    print("  작업 종료 후에는 snapshot 복구 또는 교집합 write 부재 재검증 후에만 rollback.")


if __name__ == "__main__":
    main()
