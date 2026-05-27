"""source_daily_rates canonical daily table CRUD helper (ADR-034 Phase 2d Step 1).

v2 장기 그래프 (3m/1y) hot path가 읽는 daily canonical row.
초기 backfill + 매일 append + rebuild 모두 idempotent upsert로 통일.

Key 정책 (ADR-034):
- Unique key: (source, asset, date_kst)
- invariant: rate == close (app-level enforce, §10 + §13 monitoring)
- Numeric(14, 6) read path는 Decimal 반환 → 본 helper에서 float() 변환
- Upsert null overwrite 방지: published_at / basis_date / contract_code /
  metadata_json은 COALESCE(EXCLUDED.col, source_daily_rates.col) pattern.
  incoming non-null이면 update, null이면 기존값 유지.
- captured_at update는 func.now() 명시 (row 마지막 update 시각 의미)
- batch_upsert: caller가 commit (단일 transaction 묶음)
- Dialect 분기: db.bind.dialect.name 기준 PostgreSQL / SQLite (둘 다 ON CONFLICT 지원)

설계 참조: DECISIONS.md ADR-034 §3, §10.
"""

# 표준 라이브러리
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

# 서드파티 라이브러리
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app.models import SourceDailyRate


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


def row_to_dict(row: SourceDailyRate) -> dict:
    """SourceDailyRate ORM row → dict (Numeric → float 변환).

    v2 endpoint response 직접 사용 가능 형태.
    """
    return {
        "source": row.source,
        "asset": row.asset,
        "date_kst": row.date_kst.isoformat() if row.date_kst else None,
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
    date_kst: date,
) -> Optional[SourceDailyRate]:
    """Unique key 단일 row 조회."""
    return (
        db.query(SourceDailyRate)
        .filter(
            SourceDailyRate.source == source,
            SourceDailyRate.asset == asset,
            SourceDailyRate.date_kst == date_kst,
        )
        .first()
    )


def get_range(
    db: Session,
    source: str,
    asset: str,
    start_date: date,
    end_date: date,
) -> list[SourceDailyRate]:
    """Date range 조회 (start/end inclusive, date_kst ASC)."""
    return (
        db.query(SourceDailyRate)
        .filter(
            SourceDailyRate.source == source,
            SourceDailyRate.asset == asset,
            SourceDailyRate.date_kst >= start_date,
            SourceDailyRate.date_kst <= end_date,
        )
        .order_by(SourceDailyRate.date_kst.asc())
        .all()
    )


# ─────────────────────────────────────────────────────────────
# Upsert (idempotent + dialect 분기 + COALESCE null overwrite 방지)
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
        f"source_daily_rates upsert는 PostgreSQL / SQLite만 지원 (현재: {dialect_name})"
    )


def upsert(
    db: Session,
    *,
    source: str,
    asset: str,
    date_kst: date,
    close: float,
    ohlc_quality: str,
    close_basis: str,
    source_method: str,
    high: Optional[float] = None,
    low: Optional[float] = None,
    contract_code: Optional[str] = None,
    basis_date: Optional[date] = None,
    published_at: Optional[datetime] = None,
    metadata_json: Optional[dict] = None,
    commit: bool = True,
) -> None:
    """단일 row idempotent upsert.

    invariant: rate = close (app-level enforce).

    null overwrite 방지 (COALESCE pattern):
      - contract_code / basis_date / published_at / metadata_json은
        incoming non-null이면 update, null이면 기존값 유지.

    close_only 시 high/low fallback:
      - ohlc_quality="close_only"이면 high/low가 None일 때 close 값으로 채움.

    captured_at은 func.now()로 명시 update (row 마지막 update 시각 의미).

    Args:
        commit: True면 호출 시점 commit (기본). False면 caller가 commit 책임
                (batch_upsert에서 transaction 묶음 용도).
    """
    # invariant: rate == close
    rate = close

    # close_only 시 high/low fallback (ADR-034 §7)
    if ohlc_quality == "close_only":
        if high is None:
            high = close
        if low is None:
            low = close

    insert = _dialect_insert(db)
    stmt = insert(SourceDailyRate).values(
        source=source,
        asset=asset,
        date_kst=date_kst,
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
    # COALESCE 대신 set_ 조건부 추가 패턴 사용 이유:
    # - dialect-agnostic (SQLite JSON column COALESCE 동작 차이 회피)
    # - SQL-level에서 update 발생 X (COALESCE는 update이지만 같은 값으로)
    # - 명시적 의도 표현
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

    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "asset", "date_kst"],
        set_=set_dict,
    )

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
# Rebuild (date range 단위)
# ─────────────────────────────────────────────────────────────

def delete_range(
    db: Session,
    source: str,
    asset: str,
    start_date: date,
    end_date: date,
) -> int:
    """Date range 안 모든 row 삭제 (rebuild 직전 cleanup 용도).

    Returns: 삭제된 row 개수.

    주의: 일반 운영에서는 사용 X. Rebuild flow에서만 사용 (idempotent 재계산 후 upsert).
    """
    result = (
        db.query(SourceDailyRate)
        .filter(
            SourceDailyRate.source == source,
            SourceDailyRate.asset == asset,
            SourceDailyRate.date_kst >= start_date,
            SourceDailyRate.date_kst <= end_date,
        )
        .delete()
    )
    db.commit()
    return result


# ─────────────────────────────────────────────────────────────
# Monitoring (ADR-034 §13)
# ─────────────────────────────────────────────────────────────

def find_rate_close_drift(
    db: Session,
    source: Optional[str] = None,
    asset: Optional[str] = None,
) -> list[SourceDailyRate]:
    """rate != close drift row 검색 (app-level invariant 위반 catch).

    Args:
        source: 특정 source만 필터 (None이면 전체).
        asset: 특정 asset만 필터 (None이면 전체).

    Returns: drift row 리스트 (정상이면 empty).
    """
    query = db.query(SourceDailyRate).filter(
        SourceDailyRate.rate != SourceDailyRate.close
    )
    if source is not None:
        query = query.filter(SourceDailyRate.source == source)
    if asset is not None:
        query = query.filter(SourceDailyRate.asset == asset)
    return query.order_by(SourceDailyRate.date_kst.desc()).all()
