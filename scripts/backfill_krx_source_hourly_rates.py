#!/usr/bin/env python3
"""KRX 미국달러선물 source_rates → source_hourly_rates(1w) CF-only validator (ADR-035 D3 Step 2).

`source_rates`(krx/usd-krw-futures raw tick)를 KST 1h bucket으로 rollup한 hourly canonical
candidate row를 만들고 schema/provenance 정합성을 검증한다. **DRY-RUN ONLY** (write 0 — write path는 별 PR).

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

사용법:
  # dry-run (read-only, default — write path 없음):
  python scripts/backfill_krx_source_hourly_rates.py --start-date 2026-06-01 --end-date 2026-06-07
"""

# 표준 라이브러리
import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

# 프로젝트 루트 + scripts (B 재사용)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))
sys.path.insert(0, _SCRIPT_DIR)

# 로컬 애플리케이션
import backfill_bithumb_source_hourly_rates as B  # noqa: E402  generic gap/verdict 재사용
from app.models import SourceDailyRate, SourceRate  # noqa: E402
from app.source_hourly_rates import floor_bucket_ts_kst  # noqa: E402

SOURCE = "krx"
ASSET = "usd-krw-futures"
CLOSE_BASIS = "krx_observed_hourly"   # 신규 enum — session-agnostic (CF/CM 공통, 본 PR은 CF coverage)
SOURCE_METHOD = "observed_rollup"
OHLC_QUALITY = "observed_rollup"

_KST_TZ = timezone(timedelta(hours=9))
# CF 정규장 세션 window (KST) — get_krx_cf_session_rollup(app/source_daily_rates.py)과 동일 경계
_CF_SESSION_START = time(8, 30, 0)
_CF_SESSION_END = time(15, 45, 0)
_CF_SESSION_LABEL = "CF"

_ALLOWED_CLOSE_BASIS = frozenset({CLOSE_BASIS})
_ALLOWED_SOURCE_METHOD = frozenset({SOURCE_METHOD})
_ALLOWED_OHLC_QUALITY = frozenset({OHLC_QUALITY})


# ─────────────────────────────────────────────────────────────
# 시간 변환 / 조회
# ─────────────────────────────────────────────────────────────

def _utc_naive_to_kst_iso(ts: datetime) -> str:
    """source_rates UTC naive datetime → KST isoformat."""
    return ts.replace(tzinfo=timezone.utc).astimezone(_KST_TZ).isoformat()


def _ts_in_cf_session(ts: datetime) -> bool:
    """UTC naive tick이 KST CF 정규장 window(08:30:00~15:45:00 inclusive) 안인지.

    source_rates.timestamp는 UTC naive 저장 → KST로 변환 후 시각만 비교.
    CM 야간 세션(17:50~익일 06:00) tick은 이 KST window 밖이라 False.
    """
    kst_t = ts.replace(tzinfo=timezone.utc).astimezone(_KST_TZ).time()
    return _CF_SESSION_START <= kst_t <= _CF_SESSION_END


def fetch_krx_ticks(db, start_utc: datetime, end_utc: datetime) -> list[tuple]:
    """source_rates(krx/usd-krw-futures) [start_utc, end_utc] 조회 → (rate, ts) timestamp ASC.

    CF/CM 세션 모두 포함해 fetch (KST window 필터는 rollup에서 CF-only로 적용).
    """
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


def fetch_daily_contract_map(db, start_date: date, end_date: date) -> dict:
    """source_daily_rates(krx/usd-krw-futures) [start_date, end_date] → {date_kst: contract_code}.

    daily가 이미 resolve한 KRX front contract를 hourly bucket이 재사용 (KIS API 미호출).
    date_kst별 단일 row(unique key) 전제 — contract_code None이면 None 그대로 (validation이 flag).
    """
    rows = (
        db.query(SourceDailyRate.date_kst, SourceDailyRate.contract_code)
        .filter(
            SourceDailyRate.source == SOURCE,
            SourceDailyRate.asset == ASSET,
            SourceDailyRate.date_kst >= start_date,
            SourceDailyRate.date_kst <= end_date,
        )
        .all()
    )
    return {r.date_kst: r.contract_code for r in rows}


# ─────────────────────────────────────────────────────────────
# Rollup (pure — testable). CF-only 필터 + contract resolve
# ─────────────────────────────────────────────────────────────

def rollup_cf_ticks_to_hourly(ticks: list[tuple], contract_map: dict) -> list[dict]:
    """(rate, ts_utc_naive) timestamp-ASC ticks → KST 1h bucket candidate rows (CF 세션만).

    CF 세션(KST 08:30~15:45) tick만 포함 (CM 야간 drop). close = bucket 마지막 tick (ASC라 마지막
    append) / high = max / low = min. rate == close invariant. metadata_json에 point_count +
    first/last ts(KST) + session:"CF". contract_code는 contract_map[bucket date]에서 lookup
    (부재 시 None — validation이 unresolved로 flag).
    """
    buckets: dict = {}
    for rate, ts in ticks:
        if not _ts_in_cf_session(ts):   # CF-only: CM 야간 tick drop
            continue
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
        # bucket.date()는 naive KST bucket_ts_kst의 KST 날짜 → daily row date_kst와 1:1
        contract_code = contract_map.get(bucket.date())
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
            "contract_code": contract_code,
            "metadata_json": {
                "point_count": len(b["rates"]),
                "first_ts_kst": _utc_naive_to_kst_iso(b["first_ts"]),
                "last_ts_kst": _utc_naive_to_kst_iso(b["last_ts"]),
                "session": _CF_SESSION_LABEL,
            },
        })
    return rows


# ─────────────────────────────────────────────────────────────
# Validation suite (pure — 각 함수는 issue 문자열 리스트 반환)
# ─────────────────────────────────────────────────────────────

def validate_decimal_precision(rows: list[dict]) -> list[str]:
    """Numeric(14, 6) precision — 소수부 6자리 초과(exponent < -6) 검출.

    KRX 호가 단위는 0.1이라 보통 clean하나, source_rates float read 시 artifact 방어.
    Decimal이 아니면 float → Decimal(str(v))로 자릿수 판정.
    """
    from decimal import Decimal
    issues = []
    for r in rows:
        for field in ("rate", "high", "low", "close"):
            value = r[field]
            if value is None:
                continue
            dec = value if isinstance(value, Decimal) else Decimal(str(value))
            exponent = dec.as_tuple().exponent
            if not isinstance(exponent, int):
                issues.append(f"{field}={value} non-finite @ {r['bucket_ts_kst']}")
                continue
            if exponent < -6:
                issues.append(f"{field} 소수부 {-exponent}자리 (>6) @ {r['bucket_ts_kst']}: {value}")
    return issues


def validate_enum(rows: list[dict]) -> list[str]:
    """KRX close_basis / source_method / ohlc_quality allowlist 잠금 (계약 enum 외 값 차단)."""
    issues = []
    for r in rows:
        if r["close_basis"] not in _ALLOWED_CLOSE_BASIS:
            issues.append(f"close_basis 위반 @ {r['bucket_ts_kst']}: {r['close_basis']}")
        if r["source_method"] not in _ALLOWED_SOURCE_METHOD:
            issues.append(f"source_method 위반 @ {r['bucket_ts_kst']}: {r['source_method']}")
        if r["ohlc_quality"] not in _ALLOWED_OHLC_QUALITY:
            issues.append(f"ohlc_quality 위반 @ {r['bucket_ts_kst']}: {r['ohlc_quality']}")
    return issues


def validate_session(rows: list[dict]) -> list[str]:
    """CF-only 계약: 모든 bucket metadata.session=="CF" AND bucket_ts_kst hour ∈ 8..15.

    CF 정규장 08:30~15:45 → floor 시 hour bucket은 8,9,...,15 (15:00 bucket은 15:00~15:45 부분).
    8 미만 / 15 초과 hour가 나오면 CM 야간 tick이 누출됐다는 신호.
    """
    issues = []
    for r in rows:
        m = r["metadata_json"]
        if m.get("session") != _CF_SESSION_LABEL:
            issues.append(f"session != CF @ {r['bucket_ts_kst']}: {m.get('session')}")
        hour = r["bucket_ts_kst"].hour
        if not (8 <= hour <= 15):
            issues.append(f"CF-only인데 bucket hour {hour} ∉ [8,15] @ {r['bucket_ts_kst']} (CM 누출 의심)")
    return issues


def validate_contract(rows: list[dict]) -> list[str]:
    """모든 bucket이 source_daily_rates에서 contract_code resolve 됐는지 (non-null).

    None이면 그 bucket date에 KRX daily row가 없다는 의미 → daily backfill/append 누락.
    issue 메시지에 누락 date를 surface (중복 date는 1회만).
    """
    issues = []
    missing_dates = set()
    for r in rows:
        if not r.get("contract_code"):
            d = r["bucket_ts_kst"].date()
            if d not in missing_dates:
                missing_dates.add(d)
                issues.append(f"contract_code 미resolve @ date {d} "
                              "(source_daily_rates KRX row 부재 — daily backfill/append 확인)")
    return issues


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


# ─────────────────────────────────────────────────────────────
# Coverage 진단 (validation 아님 — CF 영업일 분포 정보)
# ─────────────────────────────────────────────────────────────

def compute_cf_coverage(rows: list[dict]) -> dict:
    """CF 세션 date 분포 + date별 bucket 개수 (informational).

    CF 영업일은 보통 hour 8~15 = 최대 8 bucket. date별 bucket 수가 적으면 그 날 거래/관측 희소.
    """
    per_date = Counter(r["bucket_ts_kst"].date() for r in rows)
    return {
        "distinct_dates": len(per_date),
        "buckets_per_date": {str(d): per_date[d] for d in sorted(per_date)},
    }


# ─────────────────────────────────────────────────────────────
# main (DB read-only → CF rollup → validate → 진단). DRY-RUN ONLY
# ─────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KRX 미국달러선물 source_rates → source_hourly_rates CF-only validator (ADR-035 D3 Step 2 — DRY-RUN only)"
    )
    parser.add_argument("--start-date", required=True, help="KST 시작일 YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", required=True, help="KST 종료일 YYYY-MM-DD (inclusive)")
    args = parser.parse_args()

    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if start_date > end_date:
        print(f"[ERR] start_date({start_date}) > end_date({end_date})")
        sys.exit(2)

    print(f"모드: DRY-RUN (write 0) — KRX {SOURCE}/{ASSET} CF-only hourly rollup")
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
    print(f"[DRY-RUN {'OK' if ok else 'FAIL'}] {msg}")
    if not ok:
        sys.exit(1)
    print("[다음] write path / append / cron 연결은 별 PR (본 PR은 Step 2 validator only).")


if __name__ == "__main__":
    main()
