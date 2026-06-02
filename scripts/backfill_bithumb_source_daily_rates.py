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
  - source_method="bithumb_candlestick_api" (backfill·daily refresh 동일 방법 — ADR-034 §6/§7)
  - ohlc_quality="source_ohlc"

사용법:
  # dry-run (default) — fetch + validation, DB write X
  python scripts/backfill_bithumb_source_daily_rates.py [--limit N]
  # write (range 적재)
  python scripts/backfill_bithumb_source_daily_rates.py --write --start-date YYYY-MM-DD --end-date YYYY-MM-DD [--allow-production-write]
  # daily append (orchestrator 호출) — 단일 날짜 + verdict sentinel
  python scripts/backfill_bithumb_source_daily_rates.py --write --start-date D --end-date D --emit-daily-append-verdict

주의:
  - dry-run mode: app.source_daily_rates.upsert() 호출 X (fetch + validation + 결과 출력만).
  - write mode: --write 시 transaction 적재 (production guard + post-write validation + idempotent upsert).
"""

# 표준 라이브러리
import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

# 서드파티 라이브러리
import requests

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 로컬 애플리케이션 (sys.path 설정 후) — daily append verdict 공유 계약
from app.daily_append_verdict import emit_verdict  # noqa: E402


KST = ZoneInfo("Asia/Seoul")
ENDPOINT = "https://api.bithumb.com/public/candlestick/USDT_KRW/24h"

# canonical 획득 방식 = 공식 24h candlestick API (backfill + daily refresh 동일 방법).
# build_row와 post-write validator가 같은 상수 사용 (literal divergence 방지).
# ADR-034 §6/§7: Bithumb은 backfill·append 모두 candlestick → source_method 단일 값.
SOURCE_METHOD = "bithumb_candlestick_api"


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
        "source_method": SOURCE_METHOD,
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


# ─────────────────────────────────────────────────────────────
# Step 3 — Write helpers (Bithumb 방향, KRX/Hana writer 패턴 재사용)
# ─────────────────────────────────────────────────────────────


def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """Step 3 write 진입 전 production DB guard (KRX/Hana writer 패턴 재사용).

    DB URL dialect 검사 (sqlite는 안전, postgresql/mysql 등은 default reject).
    URL 전체 출력 회피 — dialect + host redacted (CLAUDE.md 보안 원칙).

    Returns: error message (str) or None.
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
    """Step 3 write 진입 전 사전 가드 (range + today 가드).

    Bithumb은 24/7 거래라 휴일 처리 없음 → 단순 range + today 가드.
    today candle은 마감 시점 후 안정 — `--include-today` 명시 시에만 허용.
    """
    if start > end:
        return f"start_date={start} > end_date={end}"
    if not include_today and end >= today:
        return (
            f"end_date={end} >= today={today} (Bithumb 24h candle 마감 전 미확정 위험). "
            "--include-today 명시 또는 today-1 이하로 제한"
        )
    return None


def ensure_source_daily_rates_table_created() -> None:
    """SourceDailyRate table 존재 보장 (KRX/Hana writer 패턴 재사용)."""
    from app.database import engine
    from app.models import SourceDailyRate
    SourceDailyRate.__table__.create(bind=engine, checkfirst=True)


def write_with_transaction_bithumb(
    rows_to_write: list[dict],
    start_date: date,
    end_date: date,
) -> tuple[bool, list[str]]:
    """Bithumb 단일 transaction write: upsert(commit=False) loop → post-write validation → commit/rollback.

    KRX/Hana writer와 다른 점:
      - Bithumb은 24/7 거래라 dedup 없음 (calendar 7일 = expected 7 rows)
      - 모든 top-level metadata None (contract_code/basis_date/published_at) — 가장 단순 path
      - candle_ts_ms/candle_ts_kst는 metadata_json 격리 (schema published_at 의미 보존)
      - post-write SELECT: date_kst.in_(expected_dates) (Hana 패턴 — future overlap/partial rerun 안전)

    Returns: (success: bool, issues: list[str])
    """
    from app.database import SessionLocal
    from app.source_daily_rates import upsert as upsert_fn
    from app.models import SourceDailyRate

    session = SessionLocal()
    issues: list[str] = []
    try:
        # 1. upsert (commit=False) loop — 명시적 keyword mapping
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

        # 2. post-write validation (same transaction, pre-commit, date_kst.in_(expected_dates))
        # Codex Round 1 Hana 패턴 재사용 — future overlap/partial rerun 안전 (range query는 같은 asset에 다른 path
        # 적재 row가 range 안에 있으면 false failure 가능, in_() filter로 정확 매칭).
        expected_dates = {row["date_kst"] for row in rows_to_write}
        if not expected_dates:
            session.rollback()
            return True, []

        written = (
            session.query(SourceDailyRate)
            .filter(
                SourceDailyRate.source == "bithumb",
                SourceDailyRate.asset == "usdt-krw",
                SourceDailyRate.date_kst.in_(expected_dates),
            )
            .order_by(SourceDailyRate.date_kst.asc())
            .all()
        )

        # (a) expected row count == written count
        expected_count = len(rows_to_write)
        if len(written) != expected_count:
            issues.append(
                f"row count: written {len(written)} != expected {expected_count} "
                f"(source=bithumb asset=usdt-krw date_kst IN expected_dates)"
            )

        # (b) expected_dates set == written_dates set
        written_dates = {row.date_kst for row in written}
        missing = expected_dates - written_dates
        extra = written_dates - expected_dates
        if missing:
            sample = sorted([d.isoformat() for d in missing])[:5]
            issues.append(f"missing dates ({len(missing)}): {sample}")
        if extra:
            sample = sorted([d.isoformat() for d in extra])[:5]
            issues.append(f"unexpected dates in range ({len(extra)}): {sample}")

        # (c) rate == close invariant
        for row in written:
            if row.rate != row.close:
                issues.append(
                    f"drift at date_kst={row.date_kst}: rate={row.rate} != close={row.close}"
                )

        # (d) Bithumb 방향 metadata policy: 모든 top-level metadata None
        # - contract_code IS NULL (KRX 전용)
        # - basis_date IS NULL (Hana 전용)
        # - published_at IS NULL (Hana 전용, candle ts는 metadata_json 격리)
        for row in written:
            if row.contract_code is not None:
                issues.append(
                    f"contract_code NOT NULL at date_kst={row.date_kst}: got {row.contract_code!r}"
                )
            if row.basis_date is not None:
                issues.append(
                    f"basis_date NOT NULL at date_kst={row.date_kst}: got {row.basis_date!r}"
                )
            if row.published_at is not None:
                issues.append(
                    f"published_at NOT NULL at date_kst={row.date_kst}: got {row.published_at!r}"
                )

        # (e) enum/literal 회귀 가드
        for row in written:
            if row.source != "bithumb":
                issues.append(f"source mismatch at date_kst={row.date_kst}: got {row.source!r}")
            if row.asset != "usdt-krw":
                issues.append(f"asset mismatch at date_kst={row.date_kst}: got {row.asset!r}")
            if row.source_method != SOURCE_METHOD:
                issues.append(
                    f"source_method mismatch at date_kst={row.date_kst}: got {row.source_method!r}"
                )
            if row.close_basis != "bithumb_24h_kst_close":
                issues.append(
                    f"close_basis mismatch at date_kst={row.date_kst}: got {row.close_basis!r}"
                )
            if row.ohlc_quality != "source_ohlc":
                issues.append(
                    f"ohlc_quality mismatch at date_kst={row.date_kst}: got {row.ohlc_quality!r}"
                )

        # (f) duplicate date_kst (same asset 내 unique)
        seen = set()
        for row in written:
            if row.date_kst in seen:
                issues.append(f"duplicate date_kst={row.date_kst} in same asset")
            seen.add(row.date_kst)

        # (g) source_ohlc completeness — high/low/close all not null, high >= low
        for row in written:
            if row.high is None or row.low is None or row.close is None:
                issues.append(
                    f"source_ohlc OHLC missing at date_kst={row.date_kst}: "
                    f"high={row.high}, low={row.low}, close={row.close}"
                )
                continue
            if row.high < row.low:
                issues.append(
                    f"high < low at date_kst={row.date_kst}: high={row.high}, low={row.low}"
                )

        # (h) metadata_json contains candle_ts_ms + candle_ts_kst (candle timestamp 격리 정책)
        # Codex Round 1 Non-blocker: candle_ts_kst도 검증 (Bithumb KST 00:00 anchor 정책 핵심)
        for row in written:
            md = row.metadata_json or {}
            if md.get("candle_ts_ms") is None:
                issues.append(
                    f"metadata_json.candle_ts_ms IS NULL at date_kst={row.date_kst} "
                    "(candle ts 격리 정책 위반)"
                )
            if md.get("candle_ts_kst") is None:
                issues.append(
                    f"metadata_json.candle_ts_kst IS NULL at date_kst={row.date_kst} "
                    "(KST 00:00 anchor 정책 핵심)"
                )

        # (i) write range expected count == (end - start) + 1
        # Codex Round 1 Non-blocker: Bithumb 24/7 연속성 회귀 가드 — 7일 range = 7 rows 명시 검증
        expected_range_count = (end_date - start_date).days + 1
        if expected_count != expected_range_count:
            issues.append(
                f"Bithumb 24/7 연속성 위반: expected_count={expected_count} != "
                f"(end-start)+1={expected_range_count} (range [{start_date}, {end_date}])"
            )

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


def _date_arg(s: str) -> date:
    """argparse type validator: ISO YYYY-MM-DD."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"date format은 YYYY-MM-DD (입력: {s!r})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Bithumb USDT/KRW 24h candlestick → source_daily_rates dry-run + write "
            "(ADR-034 Phase 2d Step 2-3)"
        )
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="[dry-run 모드] 최근 N개 candle만 검증 (positive int only, default: 전체)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "[Step 3 write mode] dry-run 통과 후 date range filter로 적재. "
            "default: dry-run only (safe). --start-date / --end-date 함께 명시 필수. "
            "transaction 패턴 — upsert(commit=False) loop + post-write validation + commit/rollback."
        ),
    )
    parser.add_argument(
        "--start-date",
        type=_date_arg,
        default=None,
        help="[--write 시 필수] YYYY-MM-DD. date_kst range 시작 (Bithumb은 24/7이라 휴일 dedup 없음).",
    )
    parser.add_argument(
        "--end-date",
        type=_date_arg,
        default=None,
        help="[--write 시 필수] YYYY-MM-DD. date_kst range 종료. today-1 이하 권장 (24h candle 마감 후 안정, --include-today 명시 시 today 허용).",
    )
    parser.add_argument(
        "--include-today",
        action="store_true",
        help="--end-date에 today 포함 (default off, 24h candle 마감 전 미확정 위험 회피).",
    )
    parser.add_argument(
        "--allow-production-write",
        action="store_true",
        help=(
            "[Step 3 production guard] non-SQLite DB (RDS PostgreSQL 등)에 --write 진입 허용. "
            "default off — local SQLite smoke만 허용. production execution은 별도 명시 + Stage 3 GO 필수."
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

    # --emit-daily-append-verdict: --write 동반 필수
    if args.emit_daily_append_verdict and not args.write:
        print("[CONFIG 실패] --emit-daily-append-verdict는 --write 동반 필수")
        sys.exit(1)

    # *** PRODUCTION GUARD EARLY (Bithumb writer, KRX Round 8 / Hana 패턴 재사용) ***
    if args.write:
        if args.start_date is None or args.end_date is None:
            print("[CONFIG 실패] --write 시 --start-date / --end-date 필수")
            sys.exit(1)
        # Codex Round 1 Blocker: --write + --limit hard reject (partial write 위험 회피)
        if args.limit is not None:
            print("[CONFIG 실패] --write와 --limit는 함께 사용할 수 없음 (partial write 위험)")
            sys.exit(1)
        # verdict는 1일 의미 — 단일 날짜만 허용
        if args.emit_daily_append_verdict and args.start_date != args.end_date:
            print("[CONFIG 실패] --emit-daily-append-verdict는 단일 날짜(start==end)만 허용")
            sys.exit(1)
        guard_err = check_production_write_guard(args.allow_production_write)
        if guard_err:
            print(f"[PRODUCTION 가드] {guard_err}")
            sys.exit(1)

    KST = ZoneInfo("Asia/Seoul")
    today_kst = datetime.now(tz=KST).date()

    mode_label = "WRITE (fetch all + date range filter)" if args.write else "DRY-RUN (DB write 절대 X, upsert() 호출 X)"
    print(f"모드: {mode_label}")
    print(f"Endpoint: {ENDPOINT}")
    if args.write:
        print(f"Write range: {args.start_date.isoformat()} ~ {args.end_date.isoformat()} (date_kst filter)")
        print(f"include_today: {args.include_today}")
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

    # validation issue 있으면 exit 1 + write 진입 차단
    if total_issues > 0:
        if args.write:
            print(f"\n[WRITE SKIP] dry-run validation 실패 — write 진입 차단")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────
    # Step 3 write section (--write 명시 시 진입)
    # ─────────────────────────────────────────────────────────────
    if not args.write:
        print()
        print("[다음 단계] Step 3 partial backfill 실측 적재는 --write 명시 시 진입.")
        return

    print()
    print("=" * 60)
    print("Step 3 write section (Bithumb USDT/KRW)")
    print("=" * 60)

    # range guard (production guard + start/end 필수는 main 진입 시점에서 처리됨)
    range_err = validate_write_range(
        start=args.start_date,
        end=args.end_date,
        today=today_kst,
        include_today=args.include_today,
    )
    if range_err:
        print(f"[CONFIG 실패] write range: {range_err}")
        sys.exit(1)

    # ensure table created
    print("[Step 3-1] ensure source_daily_rates table created (checkfirst=True, idempotent)...")
    try:
        ensure_source_daily_rates_table_created()
    except Exception as e:
        print(f"[TABLE 실패] {type(e).__name__}: {e}")
        sys.exit(1)
    print("  OK")
    print()

    # date range filter — fetch all rows 중 args range 내만 추출
    # --write 진입 시 --limit 이미 hard reject됨 (early check, Codex Round 1 Blocker 정정)
    rows_to_write = [
        r for r in rows
        if args.start_date <= r["date_kst"] <= args.end_date
    ]
    print(f"[Step 3-2] date range filter: {len(rows_to_write)} rows in [{args.start_date}, {args.end_date}]")
    if not rows_to_write:
        print("[WRITE SKIP] write 대상 row 0개 — args range 안에 fetched row 없음")
        sys.exit(1)

    # OOR hard reject (dry-run에서 통과한 row만 filter — 추가 안전 확인)
    oor_in_write = [
        r for r in rows_to_write
        if r["date_kst"] < args.start_date or r["date_kst"] > args.end_date
    ]
    if oor_in_write:
        print(f"[WRITE 실패] write 대상 중 out-of-range {len(oor_in_write)}건 — hard reject")
        sys.exit(1)
    print()

    # Transaction write
    print(f"[Step 3-3] write {len(rows_to_write)} rows → source_daily_rates (source=bithumb asset=usdt-krw)...")
    success, write_issues = write_with_transaction_bithumb(
        rows_to_write, args.start_date, args.end_date,
    )
    if success:
        print(f"[Bithumb write 완료] {len(rows_to_write)} rows committed + post-write validations passed")
        # daily append verdict (단일 날짜 success 경로에만 — 빈 candle/실패 경로는 sentinel 없음 → orchestrator FAIL)
        if args.emit_daily_append_verdict:
            emit_verdict("bithumb", "usdt-krw", args.start_date, "written", None, len(rows_to_write))
        print()
        print("[Rollback anchor] cleanup 시:")
        print(
            f"  from app.database import SessionLocal; from app.source_daily_rates import delete_range; "
            f'db = SessionLocal(); '
            f'delete_range(db, "bithumb", "usdt-krw", '
            f'date({args.start_date.year}, {args.start_date.month}, {args.start_date.day}), '
            f'date({args.end_date.year}, {args.end_date.month}, {args.end_date.day}))'
        )
        print()
        print("  Bithumb은 24/7 거래라 expected_dates가 연속 (휴일 dedup 없음).")
        print("  단, range delete 안전성은 연속성이 아니라 gap-only 사전 확인 + 교집합 historical/manual write 부재 + 동일 작업 창 내 즉시 rollback일 때만 성립.")
        print("  작업 종료 후에는 이 delete_range를 바로 실행하지 말 것 — snapshot 기반 복구 또는 교집합 historical/manual write 부재를 별도 재검증한 뒤에만 rollback (snapshot = 삭제 전까지 durable recovery anchor).")
        print()
        print("[Stage 2] commit 별도 GO / Stage 3 production execution 별도 GO.")
    else:
        print(f"[Bithumb write 실패] {len(write_issues)}건 issue — transaction rollback 완료")
        for issue in write_issues[:5]:
            print(f"  - {issue}")
        if len(write_issues) > 5:
            print(f"  ... 외 {len(write_issues) - 5}건")
        sys.exit(1)


if __name__ == "__main__":
    main()
