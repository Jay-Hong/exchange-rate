#!/usr/bin/env python3
"""
히스토리 데이터 백필 스크립트

Yahoo Finance에서 1년치 데이터를 다운로드하여 DB에 삽입.
기존 데이터와 겹치는 기간은 미리 조회하여 SKIP.

대상:
  - DXY (DX-Y.NYB) → market_index_rates (instrument='dxy', source='yahoo')
  - USD/KRW (KRW=X) → investing_exchange_rates (currency='usd-krw')
  - JPY/KRW (JPYKRW=X) → investing_exchange_rates (currency='jpy-krw', ×100 스케일링)
  - EUR/KRW (EURKRW=X) → investing_exchange_rates (currency='eur-krw')

granularity 컬럼으로 데이터 구분:
  - DXY 일봉: granularity='daily' (3달/1년 그래프용)
  - DXY 시간봉: granularity='hourly' (1주 그래프 부트스트랩)
  - 실시간 크롤링: granularity='realtime' (크롤러가 자동 설정)

UNIQUE 제약: (instrument, source, timestamp, granularity)
  → 같은 00:00에 일봉과 시간봉 공존 가능 (granularity가 다름)

사용법:
  python scripts/backfill_history.py [--dry-run] [--target dxy|exchange|all]
"""

# 표준 라이브러리
import argparse
import sys
import os
from datetime import datetime, timezone

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 서드파티 라이브러리
import yfinance as yf

# 로컬 애플리케이션
from app.database import SessionLocal, engine, Base, create_all_app_tables
from app import models

# ═════════════════════════════════════════════════════════════
# 백필 설정
# ═════════════════════════════════════════════════════════════

# DXY 백필 설정
DXY_TICKER = "DX-Y.NYB"
DXY_RATE_RANGE = (80.0, 130.0)

# 환율 백필 설정
EXCHANGE_TICKERS = {
    "usd-krw": {"ticker": "KRW=X", "scale": 1, "range": (800.0, 2000.0)},
    "jpy-krw": {"ticker": "JPYKRW=X", "scale": 100, "range": (5.0, 20.0)},  # Yahoo는 1엔 기준
    "eur-krw": {"ticker": "EURKRW=X", "scale": 1, "range": (1000.0, 2500.0)},
}

# JPY/KRW range는 스케일링 전 기준 (Yahoo 원본)
# 스케일링 후 범위: 500 ~ 2000


# ═════════════════════════════════════════════════════════════
# 공통 유틸
# ═════════════════════════════════════════════════════════════

def _get_existing_dxy_timestamps(db, granularity: str) -> set:
    """market_index_rates에서 DXY의 기존 timestamp 조회 (granularity별)"""
    rows = (
        db.query(models.MarketIndexRate.timestamp)
        .filter(
            models.MarketIndexRate.instrument == "dxy",
            models.MarketIndexRate.granularity == granularity,
        )
        .all()
    )
    return {row.timestamp for row in rows if row.timestamp}


def _get_existing_exchange_dates(db, currency: str) -> set:
    """investing_exchange_rates에서 해당 통화의 기존 날짜 조회"""
    rows = (
        db.query(models.InvestingExchangeRate.timestamp)
        .filter(models.InvestingExchangeRate.currency == currency)
        .all()
    )
    # 날짜만 추출 (시간 무시)
    return {row.timestamp.date() for row in rows if row.timestamp}


def _flatten_columns(data):
    """yfinance 0.2.x+ MultiIndex 컬럼 처리"""
    if hasattr(data.columns, 'levels') and len(data.columns.levels) > 1:
        data.columns = data.columns.droplevel(1)
    return data


# ═════════════════════════════════════════════════════════════
# DXY 백필
# ═════════════════════════════════════════════════════════════

def backfill_dxy(db, dry_run: bool) -> dict:
    """DXY 1년치 일별 데이터 백필"""
    print(f"\n{'='*60}")
    print("DXY 일봉 백필 (DX-Y.NYB → market_index_rates)")
    print(f"{'='*60}")

    data = yf.download(DXY_TICKER, period="1y", interval="1d", progress=False)

    if data.empty:
        print("  [ERROR] Yahoo에서 DXY 데이터를 가져올 수 없음")
        return {"inserted": 0, "skipped": 0, "invalid": 0}

    data = _flatten_columns(data)
    print(f"  다운로드: {len(data)}행")

    # 기존 DXY daily timestamp 조회 (중복 방지)
    existing_ts = _get_existing_dxy_timestamps(db, "daily")
    print(f"  기존 데이터: {len(existing_ts)}건")

    inserted = 0
    skipped = 0
    invalid = 0

    for date_idx, row in data.iterrows():
        close = float(row["Close"])

        # 유효 범위 검증
        if not (DXY_RATE_RANGE[0] <= close <= DXY_RATE_RANGE[1]):
            print(f"  [INVALID] {date_idx.date()} DXY={close:.3f} (범위 초과)")
            invalid += 1
            continue

        # Timezone 정규화: UTC 00:00:00 (일봉)
        ts = datetime(date_idx.year, date_idx.month, date_idx.day, tzinfo=None)

        # 중복 스킵
        if ts in existing_ts:
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY-RUN] {ts.date()} DXY={close:.3f}")
            inserted += 1
            continue

        entry = models.MarketIndexRate(
            instrument="dxy",
            source="yahoo",
            rate=round(close, 3),
            timestamp=ts,
            granularity="daily",
        )
        db.add(entry)
        inserted += 1

    if not dry_run and inserted > 0:
        db.commit()

    print(f"  결과: 삽입={inserted}, 스킵(중복)={skipped}, 무효={invalid}")
    return {"inserted": inserted, "skipped": skipped, "invalid": invalid}


def backfill_dxy_hourly(db, dry_run: bool) -> dict:
    """
    DXY 7일 1시간봉 백필 (1주 그래프 부트스트랩용)

    일봉(backfill_dxy) 실행 후 호출 — 일봉의 00:00 timestamp와
    겹치는 시간봉은 자동 스킵됩니다.
    """
    print(f"\n{'='*60}")
    print("DXY 시간봉 백필 (7일, 1주 그래프 부트스트랩)")
    print(f"{'='*60}")

    data = yf.download(DXY_TICKER, period="7d", interval="1h", progress=False)

    if data.empty:
        print("  [ERROR] Yahoo에서 DXY 1시간봉 데이터를 가져올 수 없음")
        return {"inserted": 0, "skipped": 0, "invalid": 0}

    data = _flatten_columns(data)
    print(f"  다운로드: {len(data)}행")

    # 기존 DXY hourly timestamp 조회 (중복 방지)
    existing_ts = _get_existing_dxy_timestamps(db, "hourly")
    print(f"  기존 데이터: {len(existing_ts)}건")

    inserted = 0
    skipped = 0
    invalid = 0

    for date_idx, row in data.iterrows():
        close = float(row["Close"])

        if not (DXY_RATE_RANGE[0] <= close <= DXY_RATE_RANGE[1]):
            invalid += 1
            continue

        # Timezone 정규화: UTC 정시 (tz-aware → naive UTC)
        if date_idx.tzinfo is not None:
            ts_utc = date_idx.astimezone(timezone.utc)
            ts = ts_utc.replace(tzinfo=None)
        else:
            ts = date_idx.to_pydatetime().replace(tzinfo=None)

        # 정시 절단 (분/초 제거)
        ts = ts.replace(minute=0, second=0, microsecond=0)

        # 중복 스킵
        if ts in existing_ts:
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY-RUN] {ts} DXY={close:.3f}")
            inserted += 1
            continue

        entry = models.MarketIndexRate(
            instrument="dxy",
            source="yahoo",
            rate=round(close, 3),
            timestamp=ts,
            granularity="hourly",
        )
        db.add(entry)
        inserted += 1

    if not dry_run and inserted > 0:
        db.commit()

    print(f"  결과: 삽입={inserted}, 스킵(중복)={skipped}, 무효={invalid}")
    return {"inserted": inserted, "skipped": skipped, "invalid": invalid}


# ═════════════════════════════════════════════════════════════
# 환율 백필
# ═════════════════════════════════════════════════════════════

def backfill_exchange_rates(db, dry_run: bool) -> dict:
    """환율 1년치 일별 데이터 백필 (USD/KRW, JPY/KRW, EUR/KRW)"""
    print(f"\n{'='*60}")
    print("환율 백필 (Yahoo → investing_exchange_rates)")
    print(f"{'='*60}")

    total = {"inserted": 0, "skipped": 0, "invalid": 0, "scaled": 0}

    for currency, config in EXCHANGE_TICKERS.items():
        ticker = config["ticker"]
        scale = config["scale"]
        rate_range = config["range"]

        print(f"\n  --- {currency} ({ticker}) ---")

        data = yf.download(ticker, period="1y", interval="1d", progress=False)

        if data.empty:
            print(f"  [ERROR] Yahoo에서 {currency} 데이터를 가져올 수 없음")
            continue

        data = _flatten_columns(data)
        print(f"  다운로드: {len(data)}행")

        # 기존 데이터 날짜 조회 (UNIQUE 제약 없으므로 직접 중복 체크)
        existing_dates = _get_existing_exchange_dates(db, currency)
        print(f"  기존 데이터: {len(existing_dates)}일")

        inserted = 0
        skipped = 0
        invalid = 0
        scaled = 0

        for date_idx, row in data.iterrows():
            close = float(row["Close"])

            # 유효 범위 검증 (스케일링 전 원본 기준)
            if not (rate_range[0] <= close <= rate_range[1]):
                print(f"  [INVALID] {date_idx.date()} {currency}={close:.4f} (범위 초과)")
                invalid += 1
                continue

            # 기존 데이터와 겹치는 날짜 스킵
            if date_idx.date() in existing_dates:
                skipped += 1
                continue

            # JPY/KRW 스케일링: 1엔 → 100엔 기준
            rate = close * scale
            if scale != 1:
                scaled += 1

            # Timezone 정규화: UTC 00:00:00 (일봉)
            ts = datetime(date_idx.year, date_idx.month, date_idx.day, tzinfo=None)

            if dry_run:
                rate_display = f"{rate:.2f}" if scale == 1 else f"{close:.4f}×{scale}={rate:.2f}"
                print(f"  [DRY-RUN] {ts.date()} {currency}={rate_display}")
                inserted += 1
                continue

            entry = models.InvestingExchangeRate(
                currency=currency,
                rate=round(rate, 2),
                timestamp=ts,
            )
            db.add(entry)
            inserted += 1

        if not dry_run and inserted > 0:
            db.commit()

        scale_note = f", 스케일링={scaled}" if scaled > 0 else ""
        print(f"  결과: 삽입={inserted}, 스킵(중복)={skipped}, 무효={invalid}{scale_note}")

        total["inserted"] += inserted
        total["skipped"] += skipped
        total["invalid"] += invalid
        total["scaled"] += scaled

    return total


# ═════════════════════════════════════════════════════════════
# 메인
# ═════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance 히스토리 데이터 백필")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 DB에 저장하지 않고 결과만 출력",
    )
    parser.add_argument(
        "--target",
        choices=["dxy", "exchange", "all"],
        default="all",
        help="백필 대상 (기본: all)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Yahoo Finance 히스토리 백필")
    print(f"모드: {'DRY-RUN (저장 안 함)' if args.dry_run else 'LIVE (DB 저장)'}")
    print(f"대상: {args.target}")
    print("=" * 60)

    # 테이블이 없으면 생성 (로컬 SQLite 등). atomic_write_control은 제외 (migration script 전용).
    create_all_app_tables(engine)

    db = SessionLocal()

    try:
        results = {}

        if args.target in ("dxy", "all"):
            # 일봉(daily)과 시간봉(hourly)은 granularity가 다르므로 독립 저장
            results["dxy_daily"] = backfill_dxy(db, args.dry_run)
            results["dxy_hourly"] = backfill_dxy_hourly(db, args.dry_run)

        if args.target in ("exchange", "all"):
            results["exchange"] = backfill_exchange_rates(db, args.dry_run)

        # 최종 리포트
        print(f"\n{'='*60}")
        print("최종 리포트")
        print(f"{'='*60}")
        for key, stats in results.items():
            print(f"  {key}: {stats}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
