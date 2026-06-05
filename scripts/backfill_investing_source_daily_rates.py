#!/usr/bin/env python3
"""
Investing observed_eod (investing_exchange_rates 내부 관측) → source_daily_rates writer.

ADR-035 D1 — Investing daily canonical (3m/1y hot path 마지막 gap 해소).

목적:
  - investing_exchange_rates(장기 보관, 기준 환율)의 KST 해당일 관측값을 daily canonical row로 적재
  - Hana observed_eod writer 패턴 차용하되 **더 단순** (carry-in baseline 없음, business-day-error 없음)
  - 외부 endpoint 미사용 — 우리 DB (investing_exchange_rates) 내부 read only

문서 anchor (ADR-035 D1 + GRAPH §catalog):
  - close_basis = "investing_observed_eod" — KST 해당일 마지막 관측 기준 환율
  - source_method = "observed_rollup" / ohlc_quality = "observed_rollup"
  - calendar = 글로벌 FX 24/5 — 일요일 obs 0 정상, 한국 공휴일 데이터 有 (Hana 영업일 calendar 안 씀)

핵심 설계 (Investing ≠ Hana — 실측 검증):
  - investing_exchange_rates는 데이터 풍부 (usd 수천/day) → **carry-in baseline 불필요**
    (Hana는 change-only 은행 테이블이라 prev 필요. Investing은 당일 관측만으로 high/low/close 충분)
  - **calendar = "0 obs인 날 skip" (business-day-error 개념 없음)** — Investing gap은 시장 주도
    (일요일/시장 휴장/sparse), 크롤러 장애 아님 → Hana의 "영업일 무변동=장애(skip_error)"와 다름.
    → 따라서 atomicity abort 없음 (모든 skip은 정상), dry-run이 skip 날짜를 summary로 surface.
  - EUR은 sparse (~100/day, 과거 더 sparse 가능) → range-dry-run에서 빈 날 확인 필수.

Row mapping:
  - source = "investing" / asset = currency (usd-krw / jpy-krw / eur-krw)
  - rate = close = 당일 마지막 관측값 / high = max / low = min (당일 관측만, baseline 없음)
  - ohlc_quality / source_method = "observed_rollup" / close_basis = "investing_observed_eod"
  - contract_code = basis_date = published_at = None (observed 방향)
  - metadata_json = {point_count, first_ts_*, last_ts_*, source_table, close_raw_row_id}

사용법:
  # 단일일 dry-run — investing_exchange_rates read only
  python scripts/backfill_investing_source_daily_rates.py --date 2026-06-04 --currency usd-krw

  # range dry-run — 전 구간 fetch + skip summary (DB write/connect 0)
  python scripts/backfill_investing_source_daily_rates.py --range-dry-run \\
      --currency usd-krw --start-date 2025-06-02 --end-date 2026-06-04

  # write mode — calendar-day range 적재 (production은 --allow-production-write + 별도 GO)
  python scripts/backfill_investing_source_daily_rates.py --write \\
      --currency usd-krw --start-date 2025-06-02 --end-date 2026-06-04 --allow-production-write

주의:
  - dry-run은 investing_exchange_rates read only (production read 안전 — feedback_db_read_permission).
  - write mode 진입 시 production guard (dialect 검사) — --allow-production-write 없으면 non-SQLite 차단.
  - upsert는 app.source_daily_rates.upsert() (idempotent ON CONFLICT, rate==close 강제).
"""

# 표준 라이브러리
import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


KST = ZoneInfo("Asia/Seoul")

# investing_exchange_rates 조회 키 = source_daily_rates asset (동일 통화값)
SOURCE = "investing"
DEFAULT_CURRENCY = "usd-krw"
SUPPORTED_CURRENCIES = ("usd-krw", "jpy-krw", "eur-krw")

CLOSE_BASIS = "investing_observed_eod"
SOURCE_METHOD = "observed_rollup"
OHLC_QUALITY = "observed_rollup"


class InvestingWriteOutcome(NamedTuple):
    """write_with_transaction_investing 결과 — rollback anchor용 inserted/updated 구분 (Hana official 패턴).

    one-shot 단독 실행 전제 — rollback anchor는 inserted_dates만 삭제 (기존 row 보존).
    """
    success: bool
    issues: list
    inserted_dates: list  # guard 시점 부재 → 신규 insert. rollback delete 대상.
    updated_dates: list   # guard 시점 기존 → idempotent re-upsert. 전체 삭제 anchor 금지 대상.


# ─────────────────────────────────────────────────────────────
# KST → UTC boundary (investing_exchange_rates.timestamp는 naive UTC)
# ─────────────────────────────────────────────────────────────

def kst_day_bounds_utc(target_date_kst: date) -> tuple[datetime, datetime]:
    """KST 해당일 [00:00, 24:00) → naive UTC [start, end_exclusive).

    investing_exchange_rates.timestamp는 naive UTC (app/models.get_utc_now).
    예: 2026-06-04 KST → UTC [2026-06-03 15:00, 2026-06-04 15:00).
    """
    kst_start = datetime.combine(target_date_kst, time(0, 0), tzinfo=KST)
    kst_end_excl = datetime.combine(target_date_kst + timedelta(days=1), time(0, 0), tzinfo=KST)
    utc_start = kst_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end_excl = kst_end_excl.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_start, utc_end_excl


def _utc_naive_to_kst_iso(ts: datetime) -> str:
    """naive UTC datetime → KST aware isoformat."""
    return ts.replace(tzinfo=timezone.utc).astimezone(KST).isoformat()


# ─────────────────────────────────────────────────────────────
# Fetch (investing_exchange_rates 내부 read) — carry-in 없음
# ─────────────────────────────────────────────────────────────

def fetch_day_observations(db, utc_start: datetime, utc_end_excl: datetime, currency: str):
    """investing_exchange_rates에서 KST 해당일 [utc_start, utc_end_excl) 관측 row 조회 (timestamp asc).

    Investing은 데이터 풍부 → carry-in baseline 불필요 (당일 관측만 rollup).
    Returns: list[InvestingExchangeRate] (timestamp asc, id asc tiebreak).
    """
    from app.models import InvestingExchangeRate

    return (
        db.query(InvestingExchangeRate)
        .filter(
            InvestingExchangeRate.currency == currency,
            InvestingExchangeRate.timestamp >= utc_start,
            InvestingExchangeRate.timestamp < utc_end_excl,
        )
        .order_by(InvestingExchangeRate.timestamp.asc(), InvestingExchangeRate.id.asc())
        .all()
    )


# ─────────────────────────────────────────────────────────────
# Build row + classify
# ─────────────────────────────────────────────────────────────

def build_investing_eod_row(target_date_kst: date, day_rows: list, currency: str) -> dict:
    """day_rows >= 1 전제 — 당일 관측 rollup row dict 생성.

    invariant: rate == close == day_rows[-1].rate.
    high/low = 당일 관측값 max/min (carry-in baseline 없음 — Investing 데이터 풍부).
    """
    close_row = day_rows[-1]
    first_row = day_rows[0]

    # Decimal(str(float)) — binary float artifact 회피 (Hana writer anchor)
    close_dec = Decimal(str(close_row.rate))
    high_dec = max(Decimal(str(r.rate)) for r in day_rows)
    low_dec = min(Decimal(str(r.rate)) for r in day_rows)

    return {
        "source": SOURCE,
        "asset": currency,
        "date_kst": target_date_kst,
        "rate": close_dec,
        "high": high_dec,
        "low": low_dec,
        "close": close_dec,
        "ohlc_quality": OHLC_QUALITY,
        "close_basis": CLOSE_BASIS,
        "source_method": SOURCE_METHOD,
        "contract_code": None,
        "basis_date": None,
        "published_at": None,
        "metadata_json": {
            "source_table": "investing_exchange_rates",
            "point_count": len(day_rows),
            "first_ts_utc": first_row.timestamp.isoformat(),
            "last_ts_utc": close_row.timestamp.isoformat(),
            "first_ts_kst": _utc_naive_to_kst_iso(first_row.timestamp),
            "last_ts_kst": _utc_naive_to_kst_iso(close_row.timestamp),
            "close_raw_row_id": close_row.id,
        },
    }


def process_date(
    db,
    target_date_kst: date,
    prefetched: Optional[list] = None,
    currency: str = DEFAULT_CURRENCY,
) -> tuple[str, Optional[dict], Optional[str]]:
    """단일 date 처리 — fetch + build (DB write X).

    Returns: (action, row_or_None, skip_code_or_None)
      action ∈ {"write", "skip"}
      - "write": row dict (당일 관측 >= 1)
      - "skip": 당일 관측 0 (일요일/시장 휴장/sparse — Investing gap은 시장 주도, 장애 아님)

    Hana와 차이: business_day_no_changes(skip_error) 개념 없음 — 모든 빈 날은 정상 skip.
    """
    utc_start, utc_end_excl = kst_day_bounds_utc(target_date_kst)
    if prefetched is not None:
        day_rows = prefetched
    else:
        day_rows = fetch_day_observations(db, utc_start, utc_end_excl, currency)

    if not day_rows:
        return "skip", None, "no_observation"

    row = build_investing_eod_row(target_date_kst, day_rows, currency)
    return "write", row, None


# ─────────────────────────────────────────────────────────────
# Validations (row dict 단위 — dry-run + write 공용. Hana writer 재사용 + investing enum)
# ─────────────────────────────────────────────────────────────

def validate_invariant(row: dict) -> list[str]:
    """rate == close invariant."""
    if row["rate"] != row["close"]:
        return [f"rate != close: rate={row['rate']}, close={row['close']}"]
    return []


def validate_ohlc_ordering(row: dict) -> list[str]:
    """OHLC 순서: low <= rate == close <= high."""
    issues = []
    high, low, close = row["high"], row["low"], row["close"]
    if low > close:
        issues.append(f"low > close: low={low}, close={close}")
    if close > high:
        issues.append(f"close > high: close={close}, high={high}")
    if high < low:
        issues.append(f"high < low: high={high}, low={low}")
    return issues


def validate_ohlc_positive(row: dict) -> list[str]:
    """non-positive OHLC."""
    issues = []
    for field in ("rate", "high", "low", "close"):
        value = row[field]
        if value is not None and value <= 0:
            issues.append(f"non-positive {field}={value}")
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


def validate_enum(row: dict) -> list[str]:
    """close_basis / source_method / ohlc_quality enum 잠금 (investing 값)."""
    issues = []
    if row["close_basis"] != CLOSE_BASIS:
        issues.append(f"close_basis must be {CLOSE_BASIS!r}: got {row['close_basis']!r}")
    if row["source_method"] != SOURCE_METHOD:
        issues.append(f"source_method must be {SOURCE_METHOD!r}: got {row['source_method']!r}")
    if row["ohlc_quality"] != OHLC_QUALITY:
        issues.append(f"ohlc_quality must be {OHLC_QUALITY!r}: got {row['ohlc_quality']!r}")
    return issues


def validate_nullable_policy(row: dict) -> list[str]:
    """observed 방향 metadata policy — contract_code/basis_date/published_at = None."""
    issues = []
    if row["contract_code"] is not None:
        issues.append(f"contract_code must be None: got {row['contract_code']!r}")
    if row["basis_date"] is not None:
        issues.append(f"basis_date must be None for observed: got {row['basis_date']!r}")
    if row["published_at"] is not None:
        issues.append(f"published_at must be None for observed: got {row['published_at']!r}")
    return issues


def validate_point_count(row: dict) -> list[str]:
    """point_count >= 1 (write인데 당일 관측 0이면 모순). Hana의 baseline 항등식 대신 단순 검증."""
    md = row["metadata_json"]
    pc = md.get("point_count")
    if not isinstance(pc, int) or pc < 1:
        return [f"point_count={pc!r} invalid (write는 당일 관측 >= 1)"]
    return []


DRY_RUN_CHECKS = [
    ("rate == close invariant", validate_invariant),
    ("OHLC ordering (low <= close <= high)", validate_ohlc_ordering),
    ("OHLC non-positive", validate_ohlc_positive),
    ("Decimal(14, 6) precision", validate_decimal_precision),
    ("enum (close_basis / source_method / ohlc_quality)", validate_enum),
    ("nullable policy (contract_code / basis_date / published_at = None)", validate_nullable_policy),
    ("point_count >= 1", validate_point_count),
]


# ─────────────────────────────────────────────────────────────
# Write helpers (Hana/KIS official writer 4 anchor 재사용 — verbatim)
# ─────────────────────────────────────────────────────────────

def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """write 진입 전 production DB guard (dialect 검사, host redacted — CLAUDE.md 보안)."""
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
    """write 진입 전 range + today 가드 (Investing은 contract chain 없음 → 단순 range)."""
    if start > end:
        return f"start_date={start} > end_date={end}"
    if not include_today and end >= today:
        return (
            f"end_date={end} >= today={today} (intraday close 미확정 위험). "
            "--include-today 명시 또는 today-1 이하로 제한"
        )
    return None


def ensure_source_daily_rates_table_created() -> None:
    """SourceDailyRate table 존재 보장 (idempotent checkfirst)."""
    from app.database import engine
    from app.models import SourceDailyRate

    SourceDailyRate.__table__.create(bind=engine, checkfirst=True)


def write_with_transaction_investing(
    rows_to_write: list[dict],
    require_empty_target: bool = False,
) -> InvestingWriteOutcome:
    """단일 transaction write: pre-write existing query → upsert(commit=False) loop → post-write validation → commit/rollback.

    inserted/updated 구분 (Hana official 패턴) — rollback anchor가 기존 row를 덮어쓰지 않도록 inserted_dates만 출력.
    require_empty_target: expected_dates에 기존 row 있으면 reject (gap-only 강제 → all-insert → rollback 안전).
    Returns: InvestingWriteOutcome(success, issues, inserted_dates, updated_dates).
    """
    from app.database import SessionLocal
    from app.source_daily_rates import upsert as upsert_fn
    from app.models import SourceDailyRate

    session = SessionLocal()
    issues: list[str] = []
    if not rows_to_write:
        session.close()
        return InvestingWriteOutcome(True, [], [], [])
    # 단일 통화 invocation 전제 — rows의 asset 도출 (post-write filter/check 기준)
    assets = {row["asset"] for row in rows_to_write}
    if len(assets) != 1:
        session.close()
        return InvestingWriteOutcome(
            False, [f"rows_to_write 혼합 asset: {sorted(assets)} (단일 통화 invocation 위반)"], [], [])
    asset = assets.pop()
    # persister 경계 allowlist guard (Hana writer 패턴 — 수동/미래 caller 방어)
    if asset not in SUPPORTED_CURRENCIES:
        session.close()
        return InvestingWriteOutcome(
            False, [f"unsupported asset: {asset!r} (지원: {SUPPORTED_CURRENCIES})"], [], [])
    expected_dates = {row["date_kst"] for row in rows_to_write}
    try:
        # 0. pre-write existing query — inserted/updated 구분 + require_empty_target gap-only 강제.
        #    Investing은 single provenance(investing_observed_eod)라 official-vs-observed overlap guard 불필요.
        #    PostgreSQL은 기존 row FOR UPDATE (concurrent write 보호).
        guard_q = session.query(SourceDailyRate).filter(
            SourceDailyRate.source == SOURCE,
            SourceDailyRate.asset == asset,
            SourceDailyRate.date_kst.in_(expected_dates),
        )
        if session.bind.dialect.name == "postgresql":
            guard_q = guard_q.with_for_update()
        existing_dates = {r.date_kst for r in guard_q.all()}
        if require_empty_target and existing_dates:
            session.rollback()
            sample = sorted(d.isoformat() for d in existing_dates)[:5]
            return InvestingWriteOutcome(
                False,
                [f"--require-empty-target: expected_dates에 기존 row {len(existing_dates)}건 존재 "
                 f"{sample} — gap-only range만 허용 (재실행/overlap 시 rollback 안전성 위해)"],
                [], [],
            )
        # inserted (guard 시점 부재) vs updated (기존 row, idempotent re-upsert) — rollback inserted-only.
        inserted_dates = sorted(expected_dates - existing_dates)
        updated_dates = sorted(existing_dates)

        # 1. upsert (commit=False) loop
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
        written = (
            session.query(SourceDailyRate)
            .filter(
                SourceDailyRate.source == SOURCE,
                SourceDailyRate.asset == asset,
                SourceDailyRate.date_kst.in_(expected_dates),
            )
            .order_by(SourceDailyRate.date_kst.asc())
            .all()
        )

        # (a) row count + dates set
        if len(written) != len(rows_to_write):
            issues.append(
                f"row count: written {len(written)} != expected {len(rows_to_write)} "
                f"(source=investing asset={asset} date_kst IN expected_dates)"
            )
        written_dates = {row.date_kst for row in written}
        missing = expected_dates - written_dates
        extra = written_dates - expected_dates
        if missing:
            issues.append(f"missing dates ({len(missing)}): {sorted(d.isoformat() for d in missing)[:5]}")
        if extra:
            issues.append(f"unexpected dates ({len(extra)}): {sorted(d.isoformat() for d in extra)[:5]}")

        for row in written:
            d = row.date_kst
            if row.rate != row.close:
                issues.append(f"drift at {d}: rate={row.rate} != close={row.close}")
            if row.low is not None and row.low > row.close:
                issues.append(f"low > close at {d}: low={row.low}, close={row.close}")
            if row.high is not None and row.close > row.high:
                issues.append(f"close > high at {d}: close={row.close}, high={row.high}")
            if row.source != SOURCE:
                issues.append(f"source mismatch at {d}: got {row.source!r}")
            if row.asset != asset:
                issues.append(f"asset mismatch at {d}: got {row.asset!r}")
            if row.close_basis != CLOSE_BASIS:
                issues.append(f"close_basis mismatch at {d}: got {row.close_basis!r}")
            if row.source_method != SOURCE_METHOD:
                issues.append(f"source_method mismatch at {d}: got {row.source_method!r}")
            if row.ohlc_quality != OHLC_QUALITY:
                issues.append(f"ohlc_quality mismatch at {d}: got {row.ohlc_quality!r}")
            if row.contract_code is not None:
                issues.append(f"contract_code NOT NULL at {d}: got {row.contract_code!r}")
            if row.basis_date is not None:
                issues.append(f"basis_date NOT NULL at {d}: got {row.basis_date!r}")
            if row.published_at is not None:
                issues.append(f"published_at NOT NULL at {d}: got {row.published_at!r}")
            md = row.metadata_json or {}
            pc = md.get("point_count")
            if not isinstance(pc, int) or pc < 1:
                issues.append(f"point_count invalid at {d}: {pc!r}")

        # (g) duplicate date_kst
        seen = set()
        for row in written:
            if row.date_kst in seen:
                issues.append(f"duplicate date_kst={row.date_kst}")
            seen.add(row.date_kst)

        if issues:
            session.rollback()
            return InvestingWriteOutcome(False, issues, [], [])
        session.commit()
        return InvestingWriteOutcome(True, [], inserted_dates, updated_dates)
    except Exception as e:
        session.rollback()
        issues.append(f"transaction exception: {type(e).__name__}: {e}")
        return InvestingWriteOutcome(False, issues, [], [])
    finally:
        session.close()


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
        else:
            result[k] = v
    return result


def expected_dates_json(rows: list[dict]) -> str:
    """write 대상 date_kst sorted·unique JSON (runbook DB pre-query audit)."""
    dates = sorted({r["date_kst"].isoformat() for r in rows})
    return "EXPECTED_DATES_JSON=" + json.dumps(dates, ensure_ascii=False)


def _date_arg(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"date format은 YYYY-MM-DD (입력: {s!r})")


# ─────────────────────────────────────────────────────────────
# Run modes
# ─────────────────────────────────────────────────────────────

def _run_dry_run(target_date_kst: date, currency: str = DEFAULT_CURRENCY) -> int:
    """단일일 dry-run (investing_exchange_rates read only). Returns exit code."""
    from app.database import SessionLocal

    print(f"Request date: {target_date_kst.isoformat()} (KST, weekday={target_date_kst.strftime('%a')})")
    utc_start, utc_end_excl = kst_day_bounds_utc(target_date_kst)
    print(f"window (UTC naive): [{utc_start.isoformat()}, {utc_end_excl.isoformat()})")
    print()

    db = SessionLocal()
    try:
        day_rows = fetch_day_observations(db, utc_start, utc_end_excl, currency)
        action, row, skip_code = process_date(db, target_date_kst, prefetched=day_rows, currency=currency)
    finally:
        db.close()

    print(f"[fetch] day observations: {len(day_rows)}")
    print(f"[classify] action={action}" + (f" skip_code={skip_code}" if skip_code else ""))
    print()

    if action == "skip":
        print(f"[SKIP] {skip_code} — 당일 관측 0 (일요일/시장 휴장/sparse, Investing gap은 시장 주도). row 미적재.")
        return 0

    print("[3] row dict (DB write X — dry-run):")
    print(json.dumps(_printable(row), ensure_ascii=False, indent=2))
    print()

    print("=" * 60)
    print("Validations")
    print("=" * 60)
    total_issues = 0
    for name, fn in DRY_RUN_CHECKS:
        found = fn(row)
        if found:
            total_issues += len(found)
            print(f"\n[{name}] {len(found)}건")
            for issue in found:
                print(f"  - {issue}")
        else:
            print(f"\n[{name}] OK (0건)")
    print()
    print("=" * 60)
    if total_issues == 0:
        print("[DRY-RUN 완료] 모든 validation 통과 (DB write 안 됨)")
        return 0
    print(f"[DRY-RUN 실패] 총 {total_issues}건 이슈 (DB write 안 됨)")
    return 1


def _run_range_dry_run(currency: str, start: date, end: date, include_today: bool) -> int:
    """range dry-run (DB write/connect read-only): 전 구간 classify + skip summary + row validation.

    1년 single full-range backfill 전 pre-write 검증용 (EUR sparse 빈 날 surface).
    """
    from app.database import SessionLocal

    today_kst = datetime.now(tz=KST).date()
    range_err = validate_write_range(start, end, today_kst, include_today)
    if range_err:
        print(f"[CONFIG 실패] range: {range_err}")
        return 1

    print(f"모드: RANGE DRY-RUN (DB write X — read only)")
    print(f"Currency: {currency} (source=investing asset={currency})")
    print(f"Range: {start.isoformat()} ~ {end.isoformat()}")
    print()

    write_rows: list[dict] = []
    skip_dates: list[date] = []

    db = SessionLocal()
    try:
        cur = start
        while cur <= end:
            action, row, _ = process_date(db, cur, currency=currency)
            if action == "write":
                write_rows.append(row)
            else:
                skip_dates.append(cur)
            cur += timedelta(days=1)
    finally:
        db.close()

    calendar_days = (end - start).days + 1
    print(f"  calendar days: {calendar_days}")
    print(f"  write rows: {len(write_rows)}")
    print(f"  skip (관측 0) days: {len(skip_dates)}")
    # skip 요일 분포 (일요일 위주 정상 / 평일 skip은 anomaly 단서)
    from collections import Counter
    dow_counter = Counter(d.strftime("%a") for d in skip_dates)
    print(f"  skip day-of-week 분포: {dict(dow_counter)}")
    weekday_skips = [d for d in skip_dates if d.strftime("%a") not in ("Sun",)]
    if weekday_skips:
        print(f"  ⚠️ 일요일 외 skip {len(weekday_skips)}건 (시장 휴장 또는 sparse — 확인 권장):")
        for d in weekday_skips[:15]:
            print(f"    - {d.isoformat()} ({d.strftime('%a')})")
    print()
    print(expected_dates_json(write_rows))
    print()

    if not write_rows:
        print("[RANGE DRY-RUN 실패] write rows 0 — range가 비었거나 전부 관측 0 (gate fail-close, exit 1)")
        return 1

    print("=" * 60)
    print(f"Row validations ({len(write_rows)} rows × DRY_RUN_CHECKS)")
    print("=" * 60)
    total_issues = 0
    for row in write_rows:
        for name, fn in DRY_RUN_CHECKS:
            for issue in fn(row):
                total_issues += 1
                if total_issues <= 15:
                    print(f"  {row['date_kst'].isoformat()} [{name}]: {issue}")
    if total_issues == 0:
        print(f"[RANGE DRY-RUN 완료] {len(write_rows)} rows validation 통과 (DB write 안 됨)")
        return 0
    print(f"[RANGE DRY-RUN 실패] 총 {total_issues}건 이슈 (DB write 안 됨)")
    return 1


def _run_write(args) -> int:
    """write mode (calendar-day range 적재). Returns exit code."""
    from app.database import SessionLocal

    today_kst = datetime.now(tz=KST).date()
    currency = getattr(args, "currency", DEFAULT_CURRENCY)

    range_err = validate_write_range(
        start=args.start_date, end=args.end_date, today=today_kst, include_today=args.include_today,
    )
    if range_err:
        print(f"[CONFIG 실패] write range: {range_err}")
        return 1

    print("[1] ensure source_daily_rates table created (checkfirst=True, idempotent)...")
    try:
        ensure_source_daily_rates_table_created()
    except Exception as e:
        print(f"[TABLE 실패] {type(e).__name__}: {e}")
        return 1
    print("  OK")
    print()

    print(f"[2] Calendar-day loop ({args.start_date} ~ {args.end_date}) classify...")
    write_rows: list[dict] = []
    skip_dates: list[date] = []

    db = SessionLocal()
    try:
        cur = args.start_date
        while cur <= args.end_date:
            action, row, _ = process_date(db, cur, currency=currency)
            if action == "write":
                write_rows.append(row)
            else:
                skip_dates.append(cur)
            cur += timedelta(days=1)
    finally:
        db.close()

    print(f"  write rows: {len(write_rows)}")
    print(f"  skip (관측 0): {len(skip_dates)}")
    # Investing skip은 모두 정상 (시장 주도) — atomicity abort 없음 (Hana와 차이)
    print()

    # *** 0-row silent PASS 방지 (bulk backfill에서 잘못된 range/currency/query mask 차단) ***
    days_in_range = (args.end_date - args.start_date).days + 1
    expected_min = getattr(args, "expected_min_rows", None)
    if expected_min is not None and len(write_rows) < expected_min:
        print(f"[FAIL] write rows {len(write_rows)} < --expected-min-rows {expected_min} "
              f"(range/currency/query 확인 — bulk backfill silent 부족 차단, exit 1)")
        return 1
    if not write_rows:
        if days_in_range > 1:
            print(f"[FAIL] multi-day range({days_in_range}일)인데 write rows 0개 — range/currency/query 오류 의심 "
                  f"(Investing은 FX 24/5라 평일 데이터 존재 정상). 단일일 Sunday skip이면 start==end로 실행. exit 1.")
            return 1
        print("[PASS] write rows 0개 (단일일, 관측 0 — Sunday/시장휴장 정상). source_daily_rates write 안 됨.")
        return 0

    print(f"[3] pre-write row validation ({len(write_rows)} rows × DRY_RUN_CHECKS)...")
    pre_issues: list[str] = []
    for row in write_rows:
        for name, fn in DRY_RUN_CHECKS:
            for issue in fn(row):
                pre_issues.append(f"{row['date_kst'].isoformat()} [{name}]: {issue}")
    if pre_issues:
        print(f"[FAIL] pre-write validation {len(pre_issues)}건 — transaction 미진입, 미적재 (exit 1)")
        for issue in pre_issues[:10]:
            print(f"  - {issue}")
        if len(pre_issues) > 10:
            print(f"  ... 외 {len(pre_issues) - 10}건")
        return 1
    print("  OK (0건)")
    print()

    print(f"[4] write {len(write_rows)} rows → source_daily_rates (source=investing asset={currency})...")
    outcome = write_with_transaction_investing(
        write_rows, require_empty_target=getattr(args, "require_empty_target", False))
    if not outcome.success:
        print(f"[Investing write 실패] {len(outcome.issues)}건 issue — transaction rollback 완료")
        for issue in outcome.issues[:5]:
            print(f"  - {issue}")
        if len(outcome.issues) > 5:
            print(f"  ... 외 {len(outcome.issues) - 5}건")
        return 1
    print(f"[Investing write 완료] {len(write_rows)} rows committed "
          f"(inserted {len(outcome.inserted_dates)} / updated {len(outcome.updated_dates)}) "
          f"+ post-write validations passed")
    print()

    # rollback anchor — inserted_dates만 (updated된 기존 row 삭제 금지, Hana official 패턴).
    if outcome.inserted_dates:
        ins = outcome.inserted_dates
        dates_repr = ", ".join(f"date({d.year}, {d.month}, {d.day})" for d in ins[:5])
        if len(ins) > 5:
            dates_repr += f", ... (총 {len(ins)})"
        print(f"[Rollback anchor] inserted {len(ins)}건만 삭제 (updated {len(outcome.updated_dates)}건 기존 row 보존):")
        print(
            f"  db.query(SourceDailyRate).filter("
            f"SourceDailyRate.source == 'investing', "
            f"SourceDailyRate.asset == '{currency}', "
            f"SourceDailyRate.date_kst.in_([{dates_repr}])"
            f").delete(synchronize_session=False); db.commit()"
        )
    else:
        print(f"[Rollback anchor] inserted 0 (전부 updated/idempotent re-upsert) — 삭제 anchor 없음.")
    print()
    print(f"[PASS] 모든 date 처리 완료 ({len(write_rows)} rows committed).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Investing observed_eod (investing_exchange_rates 내부 관측) → source_daily_rates writer "
            "(ADR-035 D1 Investing daily canonical)"
        )
    )
    parser.add_argument(
        "--currency",
        choices=SUPPORTED_CURRENCIES,
        default=DEFAULT_CURRENCY,
        help=f"Investing 통화 (asset=source_daily_rates asset). default {DEFAULT_CURRENCY}.",
    )
    parser.add_argument(
        "--date",
        type=_date_arg,
        default=None,
        help="[단일일 dry-run] YYYY-MM-DD (default: 오늘 KST). investing_exchange_rates read-only.",
    )
    parser.add_argument(
        "--range-dry-run",
        action="store_true",
        help=(
            "[range dry-run] --start-date~--end-date 전 구간 classify + skip summary + row validation "
            "(DB write/connect read-only). 1년 backfill 전 pre-write 검증 (EUR sparse 빈 날 surface)."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="[write mode] calendar-day range 적재. --start-date / --end-date 필수.",
    )
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        default=None,
        help="[--write / --range-dry-run 시 필수] YYYY-MM-DD. calendar-day loop 시작.",
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        default=None,
        help="[--write / --range-dry-run 시 필수] YYYY-MM-DD. today-1 이하 권장 (intraday close 오염 회피).",
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
            "[production guard] non-SQLite DB (RDS) write 허용. default off — local SQLite smoke만. "
            "production execution은 별도 GO 필수."
        ),
    )
    parser.add_argument(
        "--require-empty-target",
        action="store_true",
        help=(
            "[--write 안전] expected_dates에 기존 row 있으면 reject (gap-only 강제 → all-insert → rollback 안전). "
            "1년 backfill 권장 (재실행/overlap 시 rollback anchor가 기존 row 안 덮음)."
        ),
    )
    parser.add_argument(
        "--expected-min-rows",
        type=int,
        default=None,
        help=(
            "[--write bulk backfill 안전] write rows가 이 값 미만이면 fail-close "
            "(잘못된 range/currency/query silent PASS 차단). range-dry-run 결과 row 수 기준 권장."
        ),
    )
    args = parser.parse_args()

    # CLI combo fail-close (Hana official _validate_cli_combo 패턴 — 조용히 무시되는 조합 차단)
    if args.range_dry_run and args.write:
        print("[CONFIG 실패] --range-dry-run과 --write 동시 사용 금지")
        sys.exit(1)
    if args.expected_min_rows is not None and args.expected_min_rows < 1:
        print(f"[CONFIG 실패] --expected-min-rows는 >= 1 (입력: {args.expected_min_rows})")
        sys.exit(1)
    if args.require_empty_target and not args.write:
        print("[CONFIG 실패] --require-empty-target는 --write 전용 (dry-run에서 무의미)")
        sys.exit(1)
    if args.expected_min_rows is not None and not args.write:
        print("[CONFIG 실패] --expected-min-rows는 --write 전용 (dry-run에서 무의미)")
        sys.exit(1)

    if args.range_dry_run:
        if args.start_date is None or args.end_date is None:
            print("[CONFIG 실패] --range-dry-run 시 --start-date / --end-date 필수")
            sys.exit(1)
        sys.exit(_run_range_dry_run(args.currency, args.start_date, args.end_date, args.include_today))

    if args.write:
        if args.start_date is None or args.end_date is None:
            print("[CONFIG 실패] --write 시 --start-date / --end-date 필수")
            sys.exit(1)
        # *** PRODUCTION GUARD EARLY (write mode 진입 시, DB write 전) ***
        guard_err = check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[PRODUCTION 가드] {guard_err}")
            sys.exit(1)

    mode_label = (
        "WRITE (calendar-day loop)" if args.write
        else "RANGE DRY-RUN" if args.range_dry_run
        else "DRY-RUN (단일일, investing_exchange_rates read only)"
    )
    print(f"모드: {mode_label}")
    print(f"Source: investing_exchange_rates (currency={args.currency}) → source_daily_rates (source=investing asset={args.currency})")
    print(f"close_basis={CLOSE_BASIS} / source_method={SOURCE_METHOD} / ohlc_quality={OHLC_QUALITY}")
    print()

    if args.write:
        sys.exit(_run_write(args))
    else:
        target_date_kst = args.date or datetime.now(tz=KST).date()
        sys.exit(_run_dry_run(target_date_kst, args.currency))


if __name__ == "__main__":
    main()
