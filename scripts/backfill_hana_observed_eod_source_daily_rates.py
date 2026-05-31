#!/usr/bin/env python3
"""
Hana observed_eod (내부 DB 관측값) → source_daily_rates writer.

ADR-034 Phase 2d Step 3 — Hana observed_eod 별 PR (canonical append path).

목적:
  - bank_exchange_rates의 KST 해당일 마지막 관측 Hana 고시값을 daily canonical row로 적재
  - 기존 backfill_hana_source_daily_rates.py (official_historical_backfill)와 **별 path**
    (close_basis / source_method / ohlc_quality 모두 다름 — schema 의미 분리)
  - 외부 endpoint 미사용 — 우리 DB (bank_exchange_rates) 내부 read only

문서 anchor (GRAPH_API_V2_CONTRACT.md §7 + ADR-034 §6/§7/§13):
  - close_basis = "hana_observed_eod" — KST 해당일 24:00 이전 마지막 관측 Hana 고시값
  - source_method = "observed_rollup" — DB tick/bank_exchange_rates 기반 daily rollup
  - ohlc_quality = "observed_rollup" — 관측값 rollup (source 자체 OHLC 없음, source_ohlc 불가)
  - §13 expected calendar = 한국 은행 영업일 (주말 미적재 정상)

핵심 설계 (설계 검토 6 round 수렴 — Codex 협업):
  - bank_exchange_rates는 **change-only table** (insert_bank_rates_into_db: rate 변경 시에만 INSERT).
    따라서 하루 종일 값 불변이면 그날 row 0개일 수 있음.
  - **liveness gate ⊥ baseline quality 직교 분리**:
    - changes 존재 여부 = write 가능 여부 (liveness gate)
    - prev (00:00 직전 마지막 관측) 신선도 = high/low rollup 포함 여부만 (write gate 아님)
  - **rollup**: rollup_rows = ([prev] if baseline_ok else []) + changes
    - close = rate = changes[-1].rate (당일 마지막 관측, invariant rate == close)
    - high = max(rollup_rows.rate), low = min(rollup_rows.rate)
    - baseline_ok = prev 존재 AND (utc_start - prev.timestamp) <= 7일 (stale baseline 제외, 당일 close는 보존)

case 매트릭스:
  | 조건                                | action      | exit | skip_code           |
  | changes >= 1 (평일/주말 무관)       | write       | 0    | -                   |
  | 평일 + changes == 0                 | skip_error  | 1    | weekday_no_changes  |
  | 주말 + changes == 0                 | skip_ok     | 0    | weekend_no_changes  |

Row mapping:
  - source = "hana" / asset = "usd-krw"
  - date_kst = target_date_kst
  - rate = close = changes[-1].rate (Decimal(str(float)) — binary float artifact 회피)
  - high / low = rollup max / min
  - ohlc_quality = "observed_rollup" / close_basis = "hana_observed_eod" / source_method = "observed_rollup"
  - contract_code = basis_date = published_at = None (observed_eod 방향 — 외부 발표/기준일 개념 없음)
  - metadata_json = rollup provenance (아래 build_observed_eod_row 참조)

첫 PR 단순화 (후속 PR defer):
  - calendar: weekday() only (공휴일 calendar 별 PR — 공휴일 평일은 weekday_no_changes로 surface)
  - 7일 baseline age threshold: calendar-age (holiday calendar PR에서 business-day age 검토)
  - heartbeat 없음 → carry-in-only row 미발동 (changes 없으면 항상 skip)
  - cron 통합 / orchestrator --source 확장: 별 PR
  - 주말 series shape (official_backfill 주말 dedup vs observed 주말 적재): Phase 2e merge PR

사용법:
  # dry-run (default) — bank_exchange_rates read only, source_daily_rates write X
  python scripts/backfill_hana_observed_eod_source_daily_rates.py --date 2026-05-28

  # write mode — calendar-day range 적재
  python scripts/backfill_hana_observed_eod_source_daily_rates.py \\
      --write --start-date 2026-05-28 --end-date 2026-05-28

  # production execution — non-SQLite DB write 허용 (별 GO 필수)
  python scripts/backfill_hana_observed_eod_source_daily_rates.py \\
      --write --start-date 2026-05-28 --end-date 2026-05-28 --allow-production-write

주의:
  - dry-run은 bank_exchange_rates read only (production read 안전 — feedback_db_read_permission).
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
from typing import Optional
from zoneinfo import ZoneInfo

# 프로젝트 루트를 sys.path에 추가 (일관성 유지)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 로컬 애플리케이션 (sys.path 설정 후 import)
from app.calendars.hana_business_days import classify_hana_calendar_day  # noqa: E402
from app.daily_append_verdict import (  # noqa: E402
    SENTINEL_PREFIX,
    emit_verdict,
)


KST = ZoneInfo("Asia/Seoul")

# bank_exchange_rates 조회 키 (app/crawlers/hana.py: BANK_NAME='hana', currency='usd-krw')
BANK_NAME = "hana"
BANK_CURRENCY = "usd-krw"
ASSET = "usd-krw"

# baseline (prev) 최대 허용 age — 초과 시 high/low rollup에서 제외 (write 자체는 차단 X)
# 첫 PR: calendar-age. holiday calendar PR에서 business-day age 전환 검토.
AGE_LIMIT_DAYS = 7
AGE_LIMIT_SECONDS = AGE_LIMIT_DAYS * 86400

CLOSE_BASIS = "hana_observed_eod"
SOURCE_METHOD = "observed_rollup"
OHLC_QUALITY = "observed_rollup"

def _emit_verdict(date_kst: date, status: str, reason: Optional[str], rows: int) -> None:
    """daily append verdict sentinel — 공유 계약(app.daily_append_verdict)에 source/asset 채워 위임.

    status 매핑 (writer 내부 action → 외부 계약):
      write → written / skip_ok → skipped / skip_error → error
    """
    emit_verdict("hana", ASSET, date_kst, status, reason, rows)


# ─────────────────────────────────────────────────────────────
# KST → UTC boundary (bank_exchange_rates.timestamp는 naive UTC — app/models.get_utc_now)
# ─────────────────────────────────────────────────────────────

def kst_day_bounds_utc(target_date_kst: date) -> tuple[datetime, datetime]:
    """KST 해당일 [00:00, 24:00) → naive UTC [start, end_exclusive).

    bank_exchange_rates.timestamp는 naive UTC (app/models.get_utc_now =
    datetime.now(utc).replace(tzinfo=None)). 비교를 위해 동일 naive UTC로 변환.

    예: target_date_kst=2026-05-28
      KST 2026-05-28 00:00 = UTC 2026-05-27 15:00 (utc_start)
      KST 2026-05-29 00:00 = UTC 2026-05-28 15:00 (utc_end_exclusive)
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
# Fetch (bank_exchange_rates 내부 read)
# ─────────────────────────────────────────────────────────────

def fetch_prev_and_changes(db, utc_start: datetime, utc_end_excl: datetime):
    """bank_exchange_rates에서 carry-in baseline (prev) + 당일 변경 row (changes) 조회.

    bank_exchange_rates는 change-only table이므로:
      - prev: utc_start 직전 마지막 관측값 (KST 00:00 시점 latest known Hana state)
      - changes: [utc_start, utc_end_excl) 윈도우 안 변경 row (당일 실 관측)

    Returns: (prev: Optional[BankExchangeRate], changes: list[BankExchangeRate])
    """
    from app.models import BankExchangeRate

    prev = (
        db.query(BankExchangeRate)
        .filter(
            BankExchangeRate.bank == BANK_NAME,
            BankExchangeRate.currency == BANK_CURRENCY,
            BankExchangeRate.timestamp < utc_start,
        )
        .order_by(BankExchangeRate.timestamp.desc(), BankExchangeRate.id.desc())
        .first()
    )
    changes = (
        db.query(BankExchangeRate)
        .filter(
            BankExchangeRate.bank == BANK_NAME,
            BankExchangeRate.currency == BANK_CURRENCY,
            BankExchangeRate.timestamp >= utc_start,
            BankExchangeRate.timestamp < utc_end_excl,
        )
        .order_by(BankExchangeRate.timestamp.asc(), BankExchangeRate.id.asc())
        .all()
    )
    return prev, changes


# ─────────────────────────────────────────────────────────────
# Classify + Build row
# ─────────────────────────────────────────────────────────────

def build_observed_eod_row(
    target_date_kst: date,
    prev,
    changes: list,
    utc_start: datetime,
    utc_end_excl: datetime,
) -> dict:
    """changes >= 1 전제 — rollup 계산 + source_daily_rates row dict 생성.

    invariant: rate == close == changes[-1].rate.
    baseline (prev)은 신선(<=7일)할 때만 high/low rollup에 포함, stale/missing이면 제외하되
    당일 close는 보존 (liveness gate ⊥ baseline quality 분리).
    """
    # calendar_class: business_day | weekend | holiday (단일 진실 소스 재사용)
    # 공휴일에 changes가 있으면 write하되 provenance는 "holiday"로 정확히 기록.
    calendar_class = classify_hana_calendar_day(target_date_kst)

    # baseline quality (rollup 포함 여부만 — write gate 아님)
    baseline_ok = False
    baseline_exclusion_reason: Optional[str] = None
    carry_in_age_seconds: Optional[float] = None
    if prev is None:
        baseline_exclusion_reason = "missing"
    else:
        carry_in_age_seconds = (utc_start - prev.timestamp).total_seconds()
        if carry_in_age_seconds <= AGE_LIMIT_SECONDS:
            baseline_ok = True
        else:
            baseline_exclusion_reason = "older_than_7d"

    rollup_rows = ([prev] if baseline_ok else []) + changes
    close_row = changes[-1]

    # Decimal(str(float)) — binary float artifact 회피 (Codex anchor)
    close_dec = Decimal(str(close_row.rate))
    high_dec = max(Decimal(str(r.rate)) for r in rollup_rows)
    low_dec = min(Decimal(str(r.rate)) for r in rollup_rows)

    return {
        "source": "hana",
        "asset": ASSET,
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
            "source_table": "bank_exchange_rates",
            "rollup_mode": "changes_present",
            "calendar_class": calendar_class,
            "liveness_evidence": "change_row_present",
            "baseline_included": baseline_ok,
            "baseline_exclusion_reason": baseline_exclusion_reason,
            "carry_in_age_at_start_seconds": carry_in_age_seconds,
            "carry_in_raw_row_id": prev.id if prev is not None else None,
            "close_raw_row_id": close_row.id,
            "close_observed_at_utc": close_row.timestamp.isoformat(),
            "close_observed_at_kst": _utc_naive_to_kst_iso(close_row.timestamp),
            "day_change_count": len(changes),
            "rollup_point_count": len(rollup_rows),
            "rollup_start_utc": utc_start.isoformat(),
            "rollup_end_exclusive_utc": utc_end_excl.isoformat(),
        },
    }


def process_date(
    db,
    target_date_kst: date,
    prefetched: Optional[tuple] = None,
) -> tuple[str, Optional[dict], Optional[str]]:
    """단일 date 처리 — fetch + classify + build (DB write X).

    Args:
        prefetched: (prev, changes) 미리 조회한 경우 재사용 (dry-run 중복 query 회피).
                    None이면 내부에서 fetch_prev_and_changes 호출.

    Returns: (action, row_or_None, skip_code_or_None)
      action ∈ {"write", "skip_ok", "skip_error"}
      - "write": row dict 반환 (changes >= 1)
      - "skip_ok": 주말 무변동 (정상) — skip_code="weekend_no_changes"
      - "skip_error": 평일 무변동 (장애 의심) — skip_code="weekday_no_changes"
    """
    utc_start, utc_end_excl = kst_day_bounds_utc(target_date_kst)
    if prefetched is not None:
        prev, changes = prefetched
    else:
        prev, changes = fetch_prev_and_changes(db, utc_start, utc_end_excl)

    if not changes:
        # liveness gate: 당일 변경 row 없음 → calendar 분류로 skip 종류 결정
        cal = classify_hana_calendar_day(target_date_kst)
        if cal == "weekend":
            return "skip_ok", None, "weekend_no_changes"
        if cal == "holiday":
            # 공휴일 무변동 = 정상 (은행 휴무, Hana 미고시)
            return "skip_ok", None, "holiday_no_changes"
        # business_day: 평일 + 비공휴일 무변동 = 크롤러 장애 의심
        return "skip_error", None, "business_day_no_changes"

    row = build_observed_eod_row(target_date_kst, prev, changes, utc_start, utc_end_excl)
    return "write", row, None


# ─────────────────────────────────────────────────────────────
# Validations (row dict 단위 — dry-run + write 공용)
# ─────────────────────────────────────────────────────────────

def validate_invariant(row: dict) -> list[str]:
    """rate == close invariant."""
    if row["rate"] != row["close"]:
        return [f"rate != close: rate={row['rate']}, close={row['close']}"]
    return []


def validate_ohlc_ordering(row: dict) -> list[str]:
    """OHLC 순서: low <= rate == close <= high (자기 보완 anchor)."""
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
    """close_basis / source_method / ohlc_quality enum 잠금."""
    issues = []
    if row["close_basis"] != CLOSE_BASIS:
        issues.append(f"close_basis must be {CLOSE_BASIS!r}: got {row['close_basis']!r}")
    if row["source_method"] != SOURCE_METHOD:
        issues.append(f"source_method must be {SOURCE_METHOD!r}: got {row['source_method']!r}")
    if row["ohlc_quality"] != OHLC_QUALITY:
        issues.append(f"ohlc_quality must be {OHLC_QUALITY!r}: got {row['ohlc_quality']!r}")
    return issues


def validate_nullable_policy(row: dict) -> list[str]:
    """observed_eod 방향 metadata policy — Hana official_backfill과 반대:
    - contract_code = None (KRX 전용)
    - basis_date = None (external endpoint 응답 기준일 개념 없음)
    - published_at = None (외부 발표 timestamp 개념 없음 — 내부 관측)
    """
    issues = []
    if row["contract_code"] is not None:
        issues.append(f"contract_code must be None: got {row['contract_code']!r}")
    if row["basis_date"] is not None:
        issues.append(f"basis_date must be None for observed_eod: got {row['basis_date']!r}")
    if row["published_at"] is not None:
        issues.append(f"published_at must be None for observed_eod: got {row['published_at']!r}")
    return issues


def validate_rollup_point_count(row: dict) -> list[str]:
    """rollup_point_count == day_change_count + int(baseline_included) 항등식 (Codex lock-in)."""
    md = row["metadata_json"]
    expected = md["day_change_count"] + int(md["baseline_included"])
    actual = md["rollup_point_count"]
    if actual != expected:
        return [
            f"rollup_point_count={actual} != day_change_count({md['day_change_count']}) "
            f"+ baseline_included({int(md['baseline_included'])}) = {expected}"
        ]
    return []


DRY_RUN_CHECKS = [
    ("rate == close invariant", validate_invariant),
    ("OHLC ordering (low <= close <= high)", validate_ohlc_ordering),
    ("OHLC non-positive", validate_ohlc_positive),
    ("Decimal(14, 6) precision", validate_decimal_precision),
    ("enum (close_basis / source_method / ohlc_quality)", validate_enum),
    ("nullable policy (contract_code / basis_date / published_at = None)", validate_nullable_policy),
    ("rollup_point_count 항등식", validate_rollup_point_count),
]


# ─────────────────────────────────────────────────────────────
# Write helpers (KIS/Hana official writer 4 anchor 재사용)
# ─────────────────────────────────────────────────────────────

def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """write 진입 전 production DB guard (KIS/Hana official writer 패턴 재사용).

    DB URL dialect 검사 (sqlite는 안전, 그 외 default reject).
    URL 전체 출력 회피 — dialect + host redacted (CLAUDE.md 보안 원칙).

    DB write 발생 함수 (table create / session) 보다 먼저 호출.
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
    """write 진입 전 range + today 가드 (Hana는 contract chain 없음 → 단순 range)."""
    if start > end:
        return f"start_date={start} > end_date={end}"
    if not include_today and end >= today:
        return (
            f"end_date={end} >= today={today} (intraday close 미확정 위험). "
            "--include-today 명시 또는 today-1 이하로 제한"
        )
    return None


def ensure_source_daily_rates_table_created() -> None:
    """SourceDailyRate table 존재 보장 (KIS/Hana official writer 패턴 재사용)."""
    from app.database import engine
    from app.models import SourceDailyRate
    SourceDailyRate.__table__.create(bind=engine, checkfirst=True)


def write_with_transaction_observed_eod(
    rows_to_write: list[dict],
) -> tuple[bool, list[str]]:
    """단일 transaction write: upsert(commit=False) loop → post-write validation → commit/rollback.

    post-write SELECT는 expected_dates IN filter (KRX/Hana official writer 패턴 equiv —
    비연속 expected_dates 휴일/skip gap 안 false detect 차단).

    Returns: (success: bool, issues: list[str])
    """
    from app.database import SessionLocal
    from app.source_daily_rates import upsert as upsert_fn
    from app.models import SourceDailyRate

    session = SessionLocal()
    issues: list[str] = []
    try:
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
        expected_dates = {row["date_kst"] for row in rows_to_write}
        written = (
            session.query(SourceDailyRate)
            .filter(
                SourceDailyRate.source == "hana",
                SourceDailyRate.asset == ASSET,
                SourceDailyRate.date_kst.in_(expected_dates),
            )
            .order_by(SourceDailyRate.date_kst.asc())
            .all()
        )

        # (a) row count + dates set
        if len(written) != len(rows_to_write):
            issues.append(
                f"row count: written {len(written)} != expected {len(rows_to_write)} "
                f"(source=hana asset={ASSET} date_kst IN expected_dates)"
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
            # (b) rate == close invariant
            if row.rate != row.close:
                issues.append(f"drift at {d}: rate={row.rate} != close={row.close}")
            # (c) OHLC ordering low <= rate == close <= high
            if row.low is not None and row.low > row.close:
                issues.append(f"low > close at {d}: low={row.low}, close={row.close}")
            if row.high is not None and row.close > row.high:
                issues.append(f"close > high at {d}: close={row.close}, high={row.high}")
            # (d) enum 잠금
            if row.source != "hana":
                issues.append(f"source mismatch at {d}: got {row.source!r}")
            if row.asset != ASSET:
                issues.append(f"asset mismatch at {d}: got {row.asset!r}")
            if row.close_basis != CLOSE_BASIS:
                issues.append(f"close_basis mismatch at {d}: got {row.close_basis!r}")
            if row.source_method != SOURCE_METHOD:
                issues.append(f"source_method mismatch at {d}: got {row.source_method!r}")
            if row.ohlc_quality != OHLC_QUALITY:
                issues.append(f"ohlc_quality mismatch at {d}: got {row.ohlc_quality!r}")
            # (e) nullable policy (observed_eod 방향: 모두 None)
            if row.contract_code is not None:
                issues.append(f"contract_code NOT NULL at {d}: got {row.contract_code!r}")
            if row.basis_date is not None:
                issues.append(f"basis_date NOT NULL at {d}: got {row.basis_date!r}")
            if row.published_at is not None:
                issues.append(f"published_at NOT NULL at {d}: got {row.published_at!r}")
            # (f) rollup_point_count 항등식 (Codex lock-in, persisted metadata 기준)
            # N1 fix: 필수 key 누락도 fail (fail-closed — 이전 fail-open 보정)
            md = row.metadata_json or {}
            required_keys = ("rollup_point_count", "day_change_count", "baseline_included")
            missing_keys = [k for k in required_keys if k not in md]
            if missing_keys:
                issues.append(f"metadata 필수 key 누락 at {d}: {missing_keys}")
            else:
                expected_pts = md["day_change_count"] + int(md["baseline_included"])
                if md["rollup_point_count"] != expected_pts:
                    issues.append(
                        f"rollup_point_count 항등식 위반 at {d}: "
                        f"{md['rollup_point_count']} != {expected_pts}"
                    )

        # (g) duplicate date_kst
        seen = set()
        for row in written:
            if row.date_kst in seen:
                issues.append(f"duplicate date_kst={row.date_kst}")
            seen.add(row.date_kst)

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


# ─────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────

def _printable(row: dict) -> dict:
    """row dict → JSON 직렬화 가능 형태."""
    result = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            result[k] = str(v)
        elif isinstance(v, (date, datetime)):
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
        raise argparse.ArgumentTypeError(f"date format은 YYYY-MM-DD (입력: {s!r})")


def _run_dry_run(target_date_kst: date) -> int:
    """dry-run mode (bank_exchange_rates read only). Returns exit code."""
    from app.database import SessionLocal

    print(f"Request date: {target_date_kst.isoformat()} (KST, weekday={target_date_kst.strftime('%a')})")
    utc_start, utc_end_excl = kst_day_bounds_utc(target_date_kst)
    print(f"rollup window (UTC naive): [{utc_start.isoformat()}, {utc_end_excl.isoformat()})")
    print()

    db = SessionLocal()
    try:
        # 단일 fetch 후 process_date에 prefetch 전달 (N2: 이중 query 회피)
        prev, changes = fetch_prev_and_changes(db, utc_start, utc_end_excl)
        action, row, skip_code = process_date(db, target_date_kst, prefetched=(prev, changes))
    finally:
        db.close()

    print(f"[fetch] prev: {'present (id=%d, ts=%s)' % (prev.id, prev.timestamp.isoformat()) if prev else 'None'}")
    print(f"[fetch] changes (당일 변경 row): {len(changes)}")
    print(f"[classify] action={action}" + (f" skip_code={skip_code}" if skip_code else ""))
    print()

    if action == "skip_ok":
        print(f"[SKIP OK] {skip_code} — 주말/공휴일 무변동 정상 (ADR-034 §13 한국 은행 영업일 calendar). row 미적재.")
        return 0
    if action == "skip_error":
        print(f"[SKIP ERROR] {skip_code} — business_day 무변동 (크롤러 장애 의심). row 미적재, exit 1로 surface.")
        return 1

    # action == "write" → row 검증
    print("[3] row dict (DB write X — dry-run):")
    print(json.dumps(_printable(row), ensure_ascii=False, indent=2))
    print()

    print("=" * 60)
    print("Validations")
    print("=" * 60)
    total_issues = 0
    for name, fn in DRY_RUN_CHECKS:
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
        print("[DRY-RUN 완료] 모든 validation 통과 (DB write 안 됨)")
        return 0
    print(f"[DRY-RUN 실패] 총 {total_issues}건 이슈 (DB write 안 됨)")
    return 1


def _run_write(args) -> int:
    """write mode (calendar-day range). Returns exit code."""
    from app.database import SessionLocal

    today_kst = datetime.now(tz=KST).date()

    # range guard
    range_err = validate_write_range(
        start=args.start_date, end=args.end_date, today=today_kst, include_today=args.include_today,
    )
    if range_err:
        print(f"[CONFIG 실패] write range: {range_err}")
        return 1

    # ensure table
    print("[1] ensure source_daily_rates table created (checkfirst=True, idempotent)...")
    try:
        ensure_source_daily_rates_table_created()
    except Exception as e:
        print(f"[TABLE 실패] {type(e).__name__}: {e}")
        return 1
    print("  OK")
    print()

    # calendar-day loop: classify each date
    print(f"[2] Calendar-day loop ({args.start_date} ~ {args.end_date}) classify...")
    write_rows: list[dict] = []
    skip_ok_events: list[tuple[date, str]] = []
    skip_error_events: list[tuple[date, str]] = []

    db = SessionLocal()
    try:
        cur = args.start_date
        while cur <= args.end_date:
            action, row, skip_code = process_date(db, cur)
            if action == "write":
                write_rows.append(row)
            elif action == "skip_ok":
                skip_ok_events.append((cur, skip_code))
            else:  # skip_error
                skip_error_events.append((cur, skip_code))
            cur += timedelta(days=1)
    finally:
        db.close()

    print(f"  write rows: {len(write_rows)}")
    print(f"  skip_ok (주말/공휴일 무변동): {len(skip_ok_events)}")
    for d, code in skip_ok_events[:10]:
        print(f"    - {d.isoformat()} ({code})")
    print(f"  skip_error (business_day 무변동 — 장애 의심): {len(skip_error_events)}")
    for d, code in skip_error_events[:10]:
        print(f"    - {d.isoformat()} ({code})")
    print()

    # *** ATOMICITY GATE (B1): skip_error 있으면 transaction 전 즉시 중단 ***
    # business_day 무변동(business_day_no_changes)이 range에 하나라도 있으면 부분 commit 금지 —
    # 전체 range를 all-or-nothing으로 처리 (valid rows도 write 안 함, exit 1).
    # 주말/공휴일 skip_ok는 정상이므로 write 진행 허용.
    if skip_error_events:
        d_err, code_err = skip_error_events[0]
        print(
            f"[FAIL] business_day 무변동 {len(skip_error_events)}건 (business_day_no_changes) — 크롤러 장애 의심."
        )
        print("  → range atomicity: transaction 미진입, write_rows 미적재 (부분 commit 차단, exit 1).")
        if args.emit_daily_append_verdict:
            _emit_verdict(d_err, "error", code_err, 0)
        return 1

    if not write_rows:
        print("[PASS] write rows 0개 (모두 주말/공휴일 skip_ok 또는 빈 range). source_daily_rates write 안 됨.")
        if args.emit_daily_append_verdict and skip_ok_events:
            d_ok, code_ok = skip_ok_events[0]
            _emit_verdict(d_ok, "skipped", code_ok, 0)
        return 0

    # *** PRE-WRITE ROW VALIDATION (B2): DRY_RUN_CHECKS를 transaction 전 적용 ***
    # write path가 dry-run 검증을 우회하지 않도록 — malformed raw (음수/precision 등) abort.
    # issue 있으면 write 없이 exit 1 (no partial write).
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

    # write transaction (모든 write_rows valid + skip_error 0 — atomic)
    print(f"[4] write {len(write_rows)} rows → source_daily_rates (source=hana asset={ASSET})...")
    success, write_issues = write_with_transaction_observed_eod(write_rows)
    if not success:
        print(f"[Hana observed_eod write 실패] {len(write_issues)}건 issue — transaction rollback 완료")
        for issue in write_issues[:5]:
            print(f"  - {issue}")
        if len(write_issues) > 5:
            print(f"  ... 외 {len(write_issues) - 5}건")
        return 1
    print(f"[Hana observed_eod write 완료] {len(write_rows)} rows committed + post-write validations passed")
    print()
    # rollback anchor (비연속 가능 — 개별 dates IN delete)
    sorted_dates = sorted(r["date_kst"] for r in write_rows)
    dates_repr = ", ".join(f"date({d.year}, {d.month}, {d.day})" for d in sorted_dates)
    print("[Rollback anchor] (observed_eod는 주말/skip로 비연속 가능 — 개별 dates IN delete 권장):")
    print(
        f"  from app.database import SessionLocal; from app.models import SourceDailyRate; "
        f"from datetime import date; db = SessionLocal(); "
        f"db.query(SourceDailyRate).filter("
        f"SourceDailyRate.source == 'hana', "
        f"SourceDailyRate.asset == '{ASSET}', "
        f"SourceDailyRate.date_kst.in_([{dates_repr}])"
        f").delete(synchronize_session=False); db.commit()"
    )
    print()
    if args.emit_daily_append_verdict:
        _emit_verdict(write_rows[0]["date_kst"], "written", None, len(write_rows))
    print("[PASS] 모든 date 처리 완료. Stage 2 commit 별 GO / Stage 3 production execution 별 GO.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Hana observed_eod (bank_exchange_rates 내부 관측) → source_daily_rates writer "
            "(ADR-034 Phase 2d Step 3 Hana observed_eod 별 PR)"
        )
    )
    parser.add_argument(
        "--date",
        type=_date_arg,
        default=None,
        help="[dry-run 모드] YYYY-MM-DD (default: 오늘 KST). 단일 date read-only 검증.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "[write mode] calendar-day range 적재. --start-date / --end-date 필수. "
            "transaction 패턴 — upsert(commit=False) loop + post-write validation + commit/rollback."
        ),
    )
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        default=None,
        help="[--write 시 필수] YYYY-MM-DD. calendar-day loop 시작 (date_kst 기준).",
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        default=None,
        help="[--write 시 필수] YYYY-MM-DD. calendar-day loop 종료 (date_kst 기준). today-1 이하 권장.",
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
            "[production guard] non-SQLite DB (RDS PostgreSQL 등)에 --write 진입 허용. "
            "default off — local SQLite smoke만 허용. production execution은 별 GO 필수."
        ),
    )
    parser.add_argument(
        "--emit-daily-append-verdict",
        action="store_true",
        help=(
            "[orchestrator 전용] 처리 결과를 DAILY_APPEND_VERDICT_JSON= sentinel 1줄로 출력. "
            "--write + 단일 날짜(start==end)에서만 허용. 일반 backfill 실행은 미사용."
        ),
    )
    args = parser.parse_args()

    # --emit-daily-append-verdict: --write 동반 필수 (verdict는 append 결과 계약)
    if args.emit_daily_append_verdict and not args.write:
        print("[CONFIG 실패] --emit-daily-append-verdict는 --write 동반 필수")
        sys.exit(1)

    # *** PRODUCTION GUARD EARLY (write mode 진입 시, DB write 전) ***
    # production .env로 --write 잘못 실행 시 DB write 사전 차단 + 운영 영향 0 보장.
    if args.write:
        if args.start_date is None or args.end_date is None:
            print("[CONFIG 실패] --write 시 --start-date / --end-date 필수")
            sys.exit(1)
        # verdict는 1일 의미 — 단일 날짜만 허용 (multi-day range + verdict 차단)
        if args.emit_daily_append_verdict and args.start_date != args.end_date:
            print("[CONFIG 실패] --emit-daily-append-verdict는 단일 날짜(start==end)만 허용")
            sys.exit(1)
        guard_err = check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[PRODUCTION 가드] {guard_err}")
            sys.exit(1)

    mode_label = "WRITE (calendar-day loop)" if args.write else "DRY-RUN (bank_exchange_rates read only)"
    print(f"모드: {mode_label}")
    print(f"Source: bank_exchange_rates (bank={BANK_NAME} currency={BANK_CURRENCY}) → source_daily_rates (source=hana asset={ASSET})")
    print(f"close_basis={CLOSE_BASIS} / source_method={SOURCE_METHOD} / ohlc_quality={OHLC_QUALITY}")
    print()

    if args.write:
        sys.exit(_run_write(args))
    else:
        target_date_kst = args.date or datetime.now(tz=KST).date()
        sys.exit(_run_dry_run(target_date_kst))


if __name__ == "__main__":
    main()
