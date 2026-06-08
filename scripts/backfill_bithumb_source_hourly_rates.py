#!/usr/bin/env python3
"""Bithumb USDT/KRW source_rates → source_hourly_rates(1w) validator/writer (ADR-035 D3 Step 2/3).

목적:
  - `source_rates`(bithumb/usdt-krw raw tick)를 KST 1h bucket으로 rollup한 hourly
    canonical candidate row를 만들고, schema/provenance 정합성을 검증한다.
  - default dry-run (write 0). **`--write` 시 transaction 적재 (Step 3, production guard + post-write validation).** production migration/append/endpoint는 별 GO.

rollup 규칙 (ADR-035 D3 provenance):
  - bucket key = floor_bucket_ts_kst(ts.replace(tzinfo=utc))  ← source_rates.timestamp는 UTC naive
    (Step 1 helper 계약: UTC naive 직접 전달 시 9h 오차 → 반드시 aware 변환)
  - close = bucket 마지막 관측 tick / high = max / low = min / rate == close (invariant)
  - close_basis = "bithumb_observed_hourly" (신규 enum — 24h candle close와 다른 granularity)
  - source_method = "observed_rollup" / ohlc_quality = "observed_rollup"
  - metadata_json = {point_count, first_ts_kst, last_ts_kst}

gap 정책:
  - source_rates는 change-only INSERT → 무변동 hour는 bucket 없음 (정상일 수 있음).
  - **carry-forward 안 함** — 존재 bucket만 만들고 gap은 진단으로 출력.
  - carry-forward 여부는 실제 gap 패턴 보고 별도 결정.

사용법:
  # dry-run (read-only, default):
  python scripts/backfill_bithumb_source_hourly_rates.py --start-date 2026-06-01 --end-date 2026-06-07
  # write (Step 3 — production DB는 --allow-production-write 추가):
  python scripts/backfill_bithumb_source_hourly_rates.py --start-date 2026-06-01 --end-date 2026-06-07 --write --require-empty-target
"""

# 표준 라이브러리
import argparse
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 로컬 애플리케이션
from app.models import SourceRate
from app.source_hourly_rates import floor_bucket_ts_kst

SOURCE = "bithumb"
ASSET = "usdt-krw"
CLOSE_BASIS = "bithumb_observed_hourly"
SOURCE_METHOD = "observed_rollup"
OHLC_QUALITY = "observed_rollup"

_KST_TZ = timezone(timedelta(hours=9))
_ALLOWED_CLOSE_BASIS = frozenset({CLOSE_BASIS})
_ALLOWED_SOURCE_METHOD = frozenset({SOURCE_METHOD})
_ALLOWED_OHLC_QUALITY = frozenset({OHLC_QUALITY})


# ─────────────────────────────────────────────────────────────
# 시간 변환 / 조회
# ─────────────────────────────────────────────────────────────

def _utc_naive_to_kst_iso(ts: datetime) -> str:
    """source_rates UTC naive datetime → KST isoformat."""
    return ts.replace(tzinfo=timezone.utc).astimezone(_KST_TZ).isoformat()


def kst_date_range_to_utc(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """KST [start_date 00:00:00, end_date 23:59:59.999999] → UTC naive 경계 (source_rates 조회용)."""
    start_kst = datetime.combine(start_date, time.min, tzinfo=_KST_TZ)
    end_kst = datetime.combine(end_date, time.max, tzinfo=_KST_TZ)
    start_utc = start_kst.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_kst.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def fetch_bithumb_ticks(db, start_utc: datetime, end_utc: datetime) -> list[tuple]:
    """source_rates(bithumb/usdt-krw) [start_utc, end_utc] 조회 → (rate, ts) timestamp ASC."""
    rows = (
        db.query(SourceRate.rate, SourceRate.timestamp)
        .filter(
            SourceRate.source == SOURCE,
            SourceRate.asset == ASSET,
            SourceRate.timestamp >= start_utc,
            SourceRate.timestamp <= end_utc,
        )
        .order_by(SourceRate.timestamp.asc(), SourceRate.id.asc())
        .all()
    )
    return [(r.rate, r.timestamp) for r in rows]


# ─────────────────────────────────────────────────────────────
# Rollup (pure — testable)
# ─────────────────────────────────────────────────────────────

def rollup_ticks_to_hourly(ticks: list[tuple]) -> list[dict]:
    """(rate, ts_utc_naive) timestamp-ASC ticks → KST 1h bucket candidate rows.

    close = bucket 마지막 tick (ticks가 ASC라 마지막 append) / high = max / low = min.
    rate == close invariant. metadata_json에 point_count + first/last ts(KST).
    """
    buckets: dict = {}
    for rate, ts in ticks:
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
        close = b["last_rate"]
        rows.append({
            "source": SOURCE,
            "asset": ASSET,
            "bucket_ts_kst": bucket,
            "rate": close,
            "close": close,
            "high": max(b["rates"]),
            "low": min(b["rates"]),
            "ohlc_quality": OHLC_QUALITY,
            "close_basis": CLOSE_BASIS,
            "source_method": SOURCE_METHOD,
            "metadata_json": {
                "point_count": len(b["rates"]),
                "first_ts_kst": _utc_naive_to_kst_iso(b["first_ts"]),
                "last_ts_kst": _utc_naive_to_kst_iso(b["last_ts"]),
            },
        })
    return rows


# ─────────────────────────────────────────────────────────────
# Validation suite (pure — 각 함수는 issue 문자열 리스트 반환)
# ─────────────────────────────────────────────────────────────

def validate_invariant(rows: list[dict]) -> list[str]:
    return [f"rate != close @ {r['bucket_ts_kst']}: {r['rate']} != {r['close']}"
            for r in rows if r["rate"] != r["close"]]


def validate_ohlc_ordering(rows: list[dict]) -> list[str]:
    issues = []
    for r in rows:
        if not (r["low"] <= r["close"] <= r["high"]):
            issues.append(f"OHLC ordering 위반 @ {r['bucket_ts_kst']}: "
                          f"low={r['low']} close={r['close']} high={r['high']}")
    return issues


def validate_ohlc_positive(rows: list[dict]) -> list[str]:
    issues = []
    for r in rows:
        for k in ("rate", "high", "low", "close"):
            if r[k] is None or r[k] <= 0:
                issues.append(f"{k} <= 0 @ {r['bucket_ts_kst']}: {r[k]}")
    return issues


def validate_duplicates(rows: list[dict]) -> list[str]:
    seen = set()
    issues = []
    for r in rows:
        key = r["bucket_ts_kst"]
        if key in seen:
            issues.append(f"중복 bucket: {key}")
        seen.add(key)
    return issues


def validate_bucket_alignment(rows: list[dict]) -> list[str]:
    """bucket_ts_kst는 KST 시 정각 floor naive여야 함 (minute/second/microsecond 0, tz 없음)."""
    issues = []
    for r in rows:
        b = r["bucket_ts_kst"]
        if b.minute != 0 or b.second != 0 or b.microsecond != 0 or b.tzinfo is not None:
            issues.append(f"bucket 정각 floor 아님: {b!r}")
    return issues


def validate_enum(rows: list[dict]) -> list[str]:
    """close_basis / source_method / ohlc_quality allowlist 잠금 (계약 enum 외 값 차단)."""
    issues = []
    for r in rows:
        if r["close_basis"] not in _ALLOWED_CLOSE_BASIS:
            issues.append(f"close_basis 위반 @ {r['bucket_ts_kst']}: {r['close_basis']}")
        if r["source_method"] not in _ALLOWED_SOURCE_METHOD:
            issues.append(f"source_method 위반 @ {r['bucket_ts_kst']}: {r['source_method']}")
        if r["ohlc_quality"] not in _ALLOWED_OHLC_QUALITY:
            issues.append(f"ohlc_quality 위반 @ {r['bucket_ts_kst']}: {r['ohlc_quality']}")
    return issues


def validate_metadata(rows: list[dict]) -> list[str]:
    issues = []
    for r in rows:
        m = r["metadata_json"]
        if m.get("point_count", 0) < 1:
            issues.append(f"point_count < 1 @ {r['bucket_ts_kst']}")
        if not m.get("first_ts_kst") or not m.get("last_ts_kst"):
            issues.append(f"first/last ts 누락 @ {r['bucket_ts_kst']}")
    return issues


VALIDATIONS = [
    ("invariant (rate==close)", validate_invariant),
    ("OHLC ordering (low<=close<=high)", validate_ohlc_ordering),
    ("OHLC positive", validate_ohlc_positive),
    ("duplicate bucket", validate_duplicates),
    ("bucket alignment (KST 정각 floor)", validate_bucket_alignment),
    ("enum (close_basis/source_method/ohlc_quality)", validate_enum),
    ("metadata (point_count/first/last)", validate_metadata),
]


# ─────────────────────────────────────────────────────────────
# Gap 진단 (validation 아님 — carry-forward 결정용 정보)
# ─────────────────────────────────────────────────────────────

def compute_gap_report(
    rows: list[dict],
    window_start: datetime = None,
    window_end: datetime = None,
) -> dict:
    """빈(무변동) hour 진단.

    window_start/window_end(KST 정각 bucket) 주면 **그 requested window 기준**으로
    leading/trailing gap까지 계산 (1w coverage 진단 목적). 없으면 data span(첫~마지막 bucket) 기준.
    change-only source라 gap은 정상일 수 있음 — fail 아님, 정보만.
    """
    present = {r["bucket_ts_kst"] for r in rows}
    if window_start is not None and window_end is not None:
        win_start, win_end = window_start, window_end
    elif rows:
        win_start, win_end = rows[0]["bucket_ts_kst"], rows[-1]["bucket_ts_kst"]
    else:
        return {"bucket_count": 0, "gap_count": 0, "gap_hours": [],
                "window_start": None, "window_end": None}

    span_hours = int((win_end - win_start).total_seconds() // 3600) + 1
    gaps = []
    cur = win_start
    while cur <= win_end:
        if cur not in present:
            gaps.append(cur)
        cur += timedelta(hours=1)
    return {
        "bucket_count": len(rows),
        "window_start": win_start.isoformat(),
        "window_end": win_end.isoformat(),
        "span_hours": span_hours,
        "gap_count": len(gaps),
        "gap_hours": [g.isoformat() for g in gaps[:20]],
    }


def evaluate_dry_run(rows: list[dict], total_issues: int) -> tuple[bool, str]:
    """dry-run 최종 판정 (pure — fail-close gate).

    (ok, message). 0 bucket → fail-close (Bithumb 24/7 1w인데 데이터 없으면
    날짜/DB/source·asset 오류 신호) / validation issue > 0 → fail-close.
    """
    if not rows:
        return False, ("hourly bucket 0건 — 날짜/DB/source·asset 확인 필요 "
                       "(Bithumb 24/7은 1w window에 데이터 존재해야 정상)")
    if total_issues > 0:
        return False, f"validation issue {total_issues}건 — 적재 전 정정 필요"
    return True, f"validation 통과 ({len(rows)} bucket). write는 별 GO (Step 3)"


# ─────────────────────────────────────────────────────────────
# Step 3 write path (production guard + transaction + post-write validation)
# ─────────────────────────────────────────────────────────────

def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """production DB write guard (daily writer 패턴 재사용).

    SQLite는 안전, non-SQLite(RDS PostgreSQL 등)는 --allow-production-write 명시 안 되면 reject.
    URL 전체 출력 회피 — dialect + host redacted (CLAUDE.md 보안 원칙). Returns: error str or None.
    """
    from app.database import engine
    dialect_name = engine.url.get_dialect().name
    if dialect_name == "sqlite":
        return None
    if not allow_production:
        host = engine.url.host or "(unknown)"
        redacted = "***" if host and host != "(unknown)" else "(unknown)"
        return (
            f"non-SQLite DB (dialect={dialect_name} host={redacted}). "
            "--allow-production-write 명시 안 됨 — production write 차단. "
            "local smoke: DATABASE_URL=sqlite override 권장."
        )
    return None


def ensure_source_hourly_rates_table_created() -> None:
    """SourceHourlyRate table 존재 보장 (idempotent checkfirst)."""
    from app.database import engine
    from app.models import SourceHourlyRate
    SourceHourlyRate.__table__.create(bind=engine, checkfirst=True)


def write_with_transaction(
    rows: list[dict],
    require_empty: bool,
) -> tuple[bool, list[str], dict]:
    """단일 transaction write: (require_empty pre-check) → upsert(commit=False) loop →
    post-write SELECT 검증 → commit/rollback.

    Returns: (success, issues, outcome). outcome = {inserted, updated}.
    require_empty면 pre-write에 target bucket이 비어있어야 함 (gap-only 강제). post-write SELECT는
    bucket_ts_kst.in_(expected_buckets) (partial rerun / 다른 path overlap 안전 — daily 패턴).
    """
    from app.database import SessionLocal
    from app.models import SourceHourlyRate
    from app.source_hourly_rates import upsert as upsert_fn

    session = SessionLocal()
    issues: list[str] = []
    outcome = {"inserted": 0, "updated": 0}
    try:
        expected_buckets = {r["bucket_ts_kst"] for r in rows}
        if not expected_buckets:
            session.rollback()
            return False, ["write 대상 0 bucket — fail-close"], outcome

        # pre-write 기존 count (require_empty 가드 + inserted/updated 산출)
        existing = (
            session.query(SourceHourlyRate)
            .filter(
                SourceHourlyRate.source == SOURCE,
                SourceHourlyRate.asset == ASSET,
                SourceHourlyRate.bucket_ts_kst.in_(expected_buckets),
            )
            .count()
        )
        if require_empty and existing > 0:
            session.rollback()
            return False, [
                f"--require-empty-target인데 target에 기존 {existing} bucket 존재 (gap-only write 위반)"
            ], outcome

        # upsert loop (commit=False — caller transaction)
        for r in rows:
            upsert_fn(
                session, commit=False,
                source=r["source"], asset=r["asset"], bucket_ts_kst=r["bucket_ts_kst"],
                close=r["close"], ohlc_quality=r["ohlc_quality"],
                close_basis=r["close_basis"], source_method=r["source_method"],
                high=r.get("high"), low=r.get("low"), metadata_json=r.get("metadata_json"),
            )

        # post-write SELECT (in_(expected_buckets))
        written = (
            session.query(SourceHourlyRate)
            .filter(
                SourceHourlyRate.source == SOURCE,
                SourceHourlyRate.asset == ASSET,
                SourceHourlyRate.bucket_ts_kst.in_(expected_buckets),
            )
            .order_by(SourceHourlyRate.bucket_ts_kst.asc())
            .all()
        )

        # (a) count / (b) bucket set
        if len(written) != len(rows):
            issues.append(f"row count: written {len(written)} != expected {len(rows)}")
        written_buckets = {w.bucket_ts_kst for w in written}
        if expected_buckets - written_buckets:
            issues.append(f"missing buckets ({len(expected_buckets - written_buckets)})")
        if written_buckets - expected_buckets:
            issues.append(f"unexpected buckets ({len(written_buckets - expected_buckets)})")

        seen = set()
        for w in written:
            # (c) invariant
            if w.rate != w.close:
                issues.append(f"drift @ {w.bucket_ts_kst}: rate={w.rate} != close={w.close}")
            # (d) metadata policy: hourly observed는 contract_code/basis_date/published_at 전부 None
            if w.contract_code is not None or w.basis_date is not None or w.published_at is not None:
                issues.append(f"top-level metadata NOT None @ {w.bucket_ts_kst} (hourly observed는 전부 None)")
            # (e) enum/literal
            if (w.source != SOURCE or w.asset != ASSET or w.close_basis != CLOSE_BASIS
                    or w.source_method != SOURCE_METHOD or w.ohlc_quality != OHLC_QUALITY):
                issues.append(f"enum/literal mismatch @ {w.bucket_ts_kst}")
            # (f) OHLC completeness + ordering
            if w.high is None or w.low is None or w.close is None or not (w.low <= w.close <= w.high):
                issues.append(f"OHLC 위반 @ {w.bucket_ts_kst}: low={w.low} close={w.close} high={w.high}")
            # (g) bucket alignment
            b = w.bucket_ts_kst
            if b.minute != 0 or b.second != 0 or b.microsecond != 0:
                issues.append(f"bucket 정각 floor 아님 @ {b!r}")
            # (h) metadata_json 필드
            md = w.metadata_json or {}
            if md.get("point_count") is None or md.get("first_ts_kst") is None or md.get("last_ts_kst") is None:
                issues.append(f"metadata_json 필드 누락 @ {w.bucket_ts_kst}")
            # (i) duplicate
            if w.bucket_ts_kst in seen:
                issues.append(f"duplicate bucket @ {w.bucket_ts_kst}")
            seen.add(w.bucket_ts_kst)

        if issues:
            session.rollback()
            return False, issues, outcome

        outcome = {"inserted": len(rows) - existing, "updated": existing}
        session.commit()
        return True, [], outcome
    except Exception as e:
        session.rollback()
        return False, [f"transaction exception: {type(e).__name__}: {e}"], outcome
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────
# main (DB read-only → rollup → validate → 진단 → (옵션) write)
# ─────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bithumb source_rates → source_hourly_rates validator/writer (ADR-035 D3 Step 2/3 — default dry-run, --write 시 적재)"
    )
    parser.add_argument("--start-date", required=True, help="KST 시작일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", required=True, help="KST 종료일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--write", action="store_true",
                        help="[Step 3] dry-run 통과 시 transaction 적재 (default: dry-run only)")
    parser.add_argument("--allow-production-write", action="store_true",
                        help="[Step 3 guard] non-SQLite DB(RDS 등)에 --write 허용")
    parser.add_argument("--require-empty-target", action="store_true",
                        help="[Step 3] write 전 target bucket 비어있어야 함 (gap-only 강제)")
    parser.add_argument("--include-today", action="store_true",
                        help="[Step 3] end_date에 오늘 포함 허용 (default: 오늘 미완성 hour 회피로 reject)")
    args = parser.parse_args()

    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if start_date > end_date:
        print(f"[ERR] start_date({start_date}) > end_date({end_date})")
        sys.exit(2)

    # write 진입 가드 (early — DB 조회 전)
    if args.write:
        today_kst = datetime.now(_KST_TZ).date()
        if not args.include_today and end_date >= today_kst:
            print(f"[CONFIG 실패] --write인데 end_date={end_date} >= today={today_kst} "
                  "(오늘 미완성 hour 위험). --include-today 명시 또는 today-1 이하로 제한")
            sys.exit(2)
        guard_err = check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[GUARD 차단] {guard_err}")
            sys.exit(2)
        ensure_source_hourly_rates_table_created()

    mode_label = "WRITE (dry-run 통과 시 적재)" if args.write else "DRY-RUN (write 0)"
    print(f"모드: {mode_label} — Bithumb {SOURCE}/{ASSET} hourly rollup")
    print(f"범위 (KST): {start_date} ~ {end_date}")
    start_utc, end_utc = kst_date_range_to_utc(start_date, end_date)
    print(f"조회 (UTC naive): {start_utc} ~ {end_utc}")
    print()

    # 함수 내부 import — DB 연결 (--help 등에서 회피)
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        ticks = fetch_bithumb_ticks(db, start_utc, end_utc)
    finally:
        db.close()
    print(f"raw tick: {len(ticks)}건")

    rows = rollup_ticks_to_hourly(ticks)
    print(f"hourly bucket: {len(rows)}건")
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
    # gap 진단 = requested window(start_date 00:00 ~ end_date 23:00 KST) 기준 (leading/trailing 포함)
    window_start = datetime.combine(start_date, time.min)   # KST start_date 00:00
    window_end = datetime.combine(end_date, time(23, 0))    # KST end_date 23:00
    gap = compute_gap_report(rows, window_start, window_end)
    print(f"=== gap 진단 (requested window 기준, carry-forward 안 함 — gap 정상 가능) ===")
    print(f"  window: {gap['window_start']} ~ {gap['window_end']} ({gap.get('span_hours')} hours)")
    print(f"  bucket={gap['bucket_count']} / gap_count={gap['gap_count']}")
    if gap["gap_hours"]:
        print(f"  gap hours (최대 20): {gap['gap_hours']}")

    print()
    ok, msg = evaluate_dry_run(rows, total_issues)
    print(f"[DRY-RUN {'OK' if ok else 'FAIL'}] {msg}")
    if not ok:
        sys.exit(1)

    if not args.write:
        print("[다음] Step 3 적재는 --write 명시 시 진입 (production은 --allow-production-write).")
        return

    # Step 3 write (dry-run 통과 후 — atomic transaction + post-write validation)
    print()
    print(f"=== WRITE (require_empty={args.require_empty_target}) ===")
    success, write_issues, outcome = write_with_transaction(rows, args.require_empty_target)
    if not success:
        print(f"[WRITE 실패] {len(write_issues)}건 issue — transaction rollback 완료")
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
