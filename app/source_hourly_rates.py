"""source_hourly_rates canonical hourly table CRUD helper (ADR-035 D3 Step 1).

v2 1w 그래프 hot path가 읽는 hourly canonical row.
source_daily_rates의 1w 대응 — date_kst(Date) → bucket_ts_kst(DateTime, KST 시 정각 floor).
초기 backfill + 매시간 append + rebuild 모두 idempotent upsert로 통일.

Key 정책 (source_daily_rates 동형):
- Unique key: (source, asset, bucket_ts_kst)
- invariant: rate == close (app-level enforce)
- Numeric(14, 6) read path는 Decimal 반환 → 본 helper에서 float() 변환
- Upsert null overwrite 방지: published_at / basis_date / contract_code /
  metadata_json은 incoming non-null일 때만 set_ dict에 추가 (null이면 기존값 유지).
- captured_at update는 func.now() 명시 (row 마지막 update 시각 의미)
- batch_upsert: caller가 commit (단일 transaction 묶음)
- Dialect 분기: db.bind.dialect.name 기준 PostgreSQL / SQLite (둘 다 ON CONFLICT 지원)

설계 참조: DECISIONS.md ADR-035 D3 (source_daily_rates §3/§10 동형).

NOTE(후속 Step): source별 hourly rollup/backfill/append(Bithumb canary → Investing/Hana → KRX)는
별도 PR. 본 모듈은 schema CRUD core만 — source_daily_rates의 KRX CF rollup/append 같은
source-specific 로직은 backfill/append PR에서 추가.
"""

# 표준 라이브러리
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

# 서드파티 라이브러리
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app.models import SourceHourlyRate


_KST_TZ = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────
# Bucket helpers
# ─────────────────────────────────────────────────────────────

def floor_bucket_ts_kst(ts: datetime) -> datetime:
    """KST timestamp를 source_hourly_rates 1h bucket key로 floor.

    source_hourly_rates는 naive KST 시각을 bucket key로 저장한다.
    - timezone-aware 입력: KST로 변환 후 tzinfo 제거
    - naive 입력: 이미 KST로 해석
    """
    if ts.tzinfo is not None:
        ts = ts.astimezone(_KST_TZ)
    return ts.replace(minute=0, second=0, microsecond=0, tzinfo=None)


# ─────────────────────────────────────────────────────────────
# Decimal → float 변환 (Numeric(14, 6) read path 정책)
# ─────────────────────────────────────────────────────────────

def _to_float(value: Any) -> Optional[float]:
    """Decimal/Numeric → float 변환 (None 보존)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def row_to_dict(row: SourceHourlyRate) -> dict:
    """SourceHourlyRate ORM row → dict (Numeric → float 변환).

    v2 endpoint response 직접 사용 가능 형태.
    """
    return {
        "source": row.source,
        "asset": row.asset,
        "bucket_ts_kst": row.bucket_ts_kst.isoformat() if row.bucket_ts_kst else None,
        "rate": _to_float(row.rate),
        "high": _to_float(row.high),
        "low": _to_float(row.low),
        "close": _to_float(row.close),
        "ohlc_quality": row.ohlc_quality,
        "close_basis": row.close_basis,
        "source_method": row.source_method,
        "contract_code": row.contract_code,
        "basis_date": row.basis_date.isoformat() if row.basis_date else None,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
        "metadata_json": row.metadata_json,
    }


# ─────────────────────────────────────────────────────────────
# 조회
# ─────────────────────────────────────────────────────────────

def get_by_key(
    db: Session,
    source: str,
    asset: str,
    bucket_ts_kst: datetime,
) -> Optional[SourceHourlyRate]:
    """Unique key 단일 row 조회."""
    return (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == source,
            SourceHourlyRate.asset == asset,
            SourceHourlyRate.bucket_ts_kst == bucket_ts_kst,
        )
        .first()
    )


def get_range(
    db: Session,
    source: str,
    asset: str,
    start_ts: datetime,
    end_ts: datetime,
) -> list[SourceHourlyRate]:
    """Bucket range 조회 (start/end inclusive, bucket_ts_kst ASC)."""
    return (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == source,
            SourceHourlyRate.asset == asset,
            SourceHourlyRate.bucket_ts_kst >= start_ts,
            SourceHourlyRate.bucket_ts_kst <= end_ts,
        )
        .order_by(SourceHourlyRate.bucket_ts_kst.asc())
        .all()
    )


def get_last_before(
    db: Session,
    source: str,
    asset: str,
    before_ts: datetime,
) -> "SourceHourlyRate | None":
    """window 시작 bucket 직전(strictly before) 최신 hourly row 1건 (v2 carry_in seed).

    before_ts = window 시작 bucket key(naive KST). STRICT '<' — start_ts는 get_range가 이미 포함
    (ADR-039 carry_in slice 1).
    """
    return (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == source,
            SourceHourlyRate.asset == asset,
            SourceHourlyRate.bucket_ts_kst < before_ts,
        )
        .order_by(SourceHourlyRate.bucket_ts_kst.desc())
        .first()
    )


# ─────────────────────────────────────────────────────────────
# Upsert (idempotent + dialect 분기 + null overwrite 방지)
# ─────────────────────────────────────────────────────────────

def _dialect_insert(db: Session):
    """db.bind.dialect.name 기준 dialect별 insert statement factory 반환.

    PostgreSQL / SQLite (둘 다 ON CONFLICT 지원, SQLAlchemy 1.4+).
    그 외 dialect는 NotImplementedError.

    helper가 app.database.engine 직접 의존 X — Session.bind 기준이라
    test용 SQLite in-memory engine 등에서도 동일 helper 검증 가능.
    """
    dialect_name = db.bind.dialect.name
    if dialect_name == "postgresql":
        return pg_insert
    if dialect_name == "sqlite":
        return sqlite_insert
    raise NotImplementedError(
        f"source_hourly_rates upsert는 PostgreSQL / SQLite만 지원 (현재: {dialect_name})"
    )


def upsert(
    db: Session,
    *,
    source: str,
    asset: str,
    bucket_ts_kst: datetime,
    close: float,
    ohlc_quality: str,
    close_basis: str,
    source_method: str,
    high: Optional[float] = None,
    low: Optional[float] = None,
    contract_code: Optional[str] = None,
    basis_date=None,
    published_at: Optional[datetime] = None,
    metadata_json: Optional[dict] = None,
    commit: bool = True,
    update_only_if_existing_close_basis: Optional[str] = None,
) -> None:
    """단일 row idempotent upsert.

    invariant: rate = close (app-level enforce).

    null overwrite 방지:
      - contract_code / basis_date / published_at / metadata_json은
        incoming non-null이면 update, null이면 기존값 유지 (set_ dict 조건부 추가).

    close_only 시 high/low fallback:
      - ohlc_quality="close_only"이면 high/low가 None일 때 close 값으로 채움.

    captured_at은 func.now()로 명시 update (row 마지막 update 시각 의미).

    Args:
        commit: True면 호출 시점 commit (기본). False면 caller가 commit 책임
                (batch_upsert에서 transaction 묶음 용도).
        update_only_if_existing_close_basis: 지정 시 ON CONFLICT DO UPDATE에 WHERE 추가 —
                기존 row close_basis가 이 값일 때만 update (불일치면 skip → 기존 row 보존).
                기본 None → 무조건 update (기존 caller 동작 유지).
    """
    # invariant: rate == close
    rate = close

    # close_only 시 high/low fallback
    if ohlc_quality == "close_only":
        if high is None:
            high = close
        if low is None:
            low = close

    insert = _dialect_insert(db)
    stmt = insert(SourceHourlyRate).values(
        source=source,
        asset=asset,
        bucket_ts_kst=bucket_ts_kst,
        rate=rate,
        high=high,
        low=low,
        close=close,
        ohlc_quality=ohlc_quality,
        close_basis=close_basis,
        source_method=source_method,
        contract_code=contract_code,
        basis_date=basis_date,
        published_at=published_at,
        metadata_json=metadata_json,
    )

    # null overwrite 방지: incoming non-null일 때만 set_ dict에 포함
    # (set_ dict에서 제외하면 ON CONFLICT 시 해당 column update 자체가 발생 X → 기존 값 자연 보존)
    set_dict = {
        # Always update
        "rate": stmt.excluded.rate,
        "high": stmt.excluded.high,
        "low": stmt.excluded.low,
        "close": stmt.excluded.close,
        "ohlc_quality": stmt.excluded.ohlc_quality,
        "close_basis": stmt.excluded.close_basis,
        "source_method": stmt.excluded.source_method,
        "captured_at": func.now(),  # row 마지막 update 시각 명시
    }
    # Nullable fields: incoming non-null일 때만 update (set_ dict에 추가)
    if contract_code is not None:
        set_dict["contract_code"] = stmt.excluded.contract_code
    if basis_date is not None:
        set_dict["basis_date"] = stmt.excluded.basis_date
    if published_at is not None:
        set_dict["published_at"] = stmt.excluded.published_at
    if metadata_json is not None:
        set_dict["metadata_json"] = stmt.excluded.metadata_json

    conflict_kwargs = {
        "index_elements": ["source", "asset", "bucket_ts_kst"],
        "set_": set_dict,
    }
    # conditional conflict update: 지정 시 기존 row의 close_basis가 일치할 때만 update.
    if update_only_if_existing_close_basis is not None:
        conflict_kwargs["where"] = (
            SourceHourlyRate.close_basis == update_only_if_existing_close_basis
        )
    stmt = stmt.on_conflict_do_update(**conflict_kwargs)

    db.execute(stmt)
    if commit:
        db.commit()


def batch_upsert(
    db: Session,
    rows: Iterable[dict],
) -> int:
    """다수 row idempotent upsert (단일 transaction 묶음).

    각 row dict는 upsert() 시그니처와 동일 keys 필요 (commit 제외).
    Returns: 처리된 row 개수.

    row별 commit하지 않고 마지막에 1회 commit — backfill 대량 처리 시 성능 우위.
    """
    count = 0
    for row in rows:
        # row dict의 commit 키 override (caller가 commit 책임)
        row_args = {k: v for k, v in row.items() if k != "commit"}
        upsert(db, commit=False, **row_args)
        count += 1
    db.commit()
    return count


# ─────────────────────────────────────────────────────────────
# Rebuild / retention (bucket range 단위)
# ─────────────────────────────────────────────────────────────

def delete_range(
    db: Session,
    source: str,
    asset: str,
    start_ts: datetime,
    end_ts: datetime,
) -> int:
    """Bucket range 안 모든 row 삭제 (rebuild 직전 cleanup / retention prune 용도).

    Returns: 삭제된 row 개수.

    주의: 일반 운영에서는 rebuild / retention cleanup에서만 사용.
    """
    result = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == source,
            SourceHourlyRate.asset == asset,
            SourceHourlyRate.bucket_ts_kst >= start_ts,
            SourceHourlyRate.bucket_ts_kst <= end_ts,
        )
        .delete()
    )
    db.commit()
    return result


# ─────────────────────────────────────────────────────────────
# Monitoring (rate == close invariant)
# ─────────────────────────────────────────────────────────────

def find_rate_close_drift(
    db: Session,
    source: Optional[str] = None,
    asset: Optional[str] = None,
) -> list[SourceHourlyRate]:
    """rate != close drift row 검색 (app-level invariant 위반 catch).

    Args:
        source: 특정 source만 필터 (None이면 전체).
        asset: 특정 asset만 필터 (None이면 전체).

    Returns: drift row 리스트 (정상이면 empty).
    """
    query = db.query(SourceHourlyRate).filter(
        SourceHourlyRate.rate != SourceHourlyRate.close
    )
    if source is not None:
        query = query.filter(SourceHourlyRate.source == source)
    if asset is not None:
        query = query.filter(SourceHourlyRate.asset == asset)
    return query.order_by(SourceHourlyRate.bucket_ts_kst.desc()).all()
