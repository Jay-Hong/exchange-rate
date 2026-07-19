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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

# 서드파티 라이브러리
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app.models import SourceDailyRate, SourceRate


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


def get_last_before(
    db: Session,
    source: str,
    asset: str,
    before_date: date,
) -> "SourceDailyRate | None":
    """window 시작 직전(strictly before) 최신 daily row 1건 (v2 carry_in seed).

    graph_cache.fetch_last_before의 source_daily_rates 대응. 단 STRICT '<' — before_date(window
    시작일)는 get_range가 이미 [start,end]에 포함하므로 '<='면 data[0]와 중복(ADR-039 carry_in slice 1).
    """
    return (
        db.query(SourceDailyRate)
        .filter(
            SourceDailyRate.source == source,
            SourceDailyRate.asset == asset,
            SourceDailyRate.date_kst < before_date,
        )
        .order_by(SourceDailyRate.date_kst.desc())
        .first()
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
    update_only_if_existing_close_basis: Optional[str] = None,
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
        update_only_if_existing_close_basis: 지정 시 ON CONFLICT DO UPDATE에 WHERE 추가 —
                기존 row close_basis가 이 값일 때만 update (불일치면 skip → 기존 row 보존).
                Hana official이 concurrent observed insert로부터 보호용 ("hana_official_historical_backfill").
                기본 None → 무조건 update (기존 caller 동작 유지).
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

    conflict_kwargs = {
        "index_elements": ["source", "asset", "date_kst"],
        "set_": set_dict,
    }
    # conditional conflict update (Step 4A Stage 1 — Hana official 전용):
    # update_only_if_existing_close_basis 지정 시 기존 row의 close_basis가 일치할 때만 update.
    # 불일치(예: absent key에 concurrent observed insert가 먼저 들어온 경우) → conflict update SKIP
    # → 기존(observed) row 보존 → caller의 post-write validation이 mismatch 잡아 rollback (race-safe).
    # 기본 None → 무조건 update (KRX/Bithumb/Hana observed 기존 동작 유지).
    if update_only_if_existing_close_basis is not None:
        conflict_kwargs["where"] = (
            SourceDailyRate.close_basis == update_only_if_existing_close_basis
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


# ─────────────────────────────────────────────────────────────
# KRX CF 정규장 세션 rollup (ADR-034 §9 — KRX daily append Unit 1)
# ─────────────────────────────────────────────────────────────

# CF 정규장 세션 window (KST) — high/low rollup 범위 (DECISIONS §9 / line 4419)
_KST_TZ = timezone(timedelta(hours=9))
_KRX_SOURCE = "krx"
_KRX_ASSET = "usd-krw-futures"
_CF_SESSION_START = time(8, 30, 0)
_CF_SESSION_END = time(15, 45, 0)


@dataclass(frozen=True)
class CfSessionRollup:
    """KRX CF 정규장 세션 source_rates rollup 결과 (high/low + completeness 진단).

    **close 미포함** — finalizer authoritative close를 Unit 2 row builder에서 주입.
    point_count=0이면 empty(세션 tick 0건) — caller가 completeness gate(close_only/fail-close) 판정.
    first_ts/last_ts는 source_rates 저장 형식(UTC naive).
    """
    high: Optional[float]
    low: Optional[float]
    point_count: int
    first_ts: Optional[datetime]
    last_ts: Optional[datetime]


def get_krx_cf_session_rollup(db: Session, date_kst: date) -> CfSessionRollup:
    """date_kst의 KRX CF 정규장(KST 08:30:00~15:45:00) source_rates rollup → high/low + 진단.

    source_rates.timestamp는 UTC naive 저장 → KST CF window를 UTC naive 경계로 변환해 조회.
    high=max(rate) / low=min(rate). **close 미포함**(finalizer authoritative 주입, Unit 2).
    empty(세션 tick 0건)면 point_count=0 + high/low/ts=None — caller가 completeness gate 판정.

    경계: KST 08:30:00 <= ts <= 15:45:00 (inclusive). CM 야간 세션 tick은 이 KST window 밖이라 자연 제외.
    """
    start_utc = datetime.combine(date_kst, _CF_SESSION_START, tzinfo=_KST_TZ).astimezone(
        timezone.utc).replace(tzinfo=None)
    end_utc = datetime.combine(date_kst, _CF_SESSION_END, tzinfo=_KST_TZ).astimezone(
        timezone.utc).replace(tzinfo=None)

    rows = (
        db.query(SourceRate.rate, SourceRate.timestamp)
        .filter(
            SourceRate.source == _KRX_SOURCE,
            SourceRate.asset == _KRX_ASSET,
            SourceRate.timestamp >= start_utc,
            SourceRate.timestamp <= end_utc,
        )
        .order_by(SourceRate.timestamp.asc(), SourceRate.id.asc())
        .all()
    )
    if not rows:
        return CfSessionRollup(high=None, low=None, point_count=0,
                               first_ts=None, last_ts=None)
    rates = [r.rate for r in rows]
    return CfSessionRollup(
        high=max(rates), low=min(rates), point_count=len(rows),
        first_ts=rows[0].timestamp, last_ts=rows[-1].timestamp,
    )


# ─────────────────────────────────────────────────────────────
# KRX CF append row builder (ADR-034 §9 — KRX daily append Unit 2)
# ─────────────────────────────────────────────────────────────

_KRX_CF_CLOSE_BASIS = "krx_cf_close_1545"
_KRX_CF_SOURCE_METHOD = "close_finalizer"
_KRX_TICK = Decimal("0.1")  # KRX 미국달러선물 호가 단위


def _quantize_krx(value) -> Decimal:
    """float/Decimal → KRX 0.1 tick Decimal. float은 Decimal(str(v)) 경유 (정밀도 artifact 회피)."""
    return Decimal(str(value)).quantize(_KRX_TICK)


def _utc_naive_to_kst_iso(ts: Optional[datetime]) -> Optional[str]:
    """source_rates UTC naive datetime → KST isoformat (None 보존)."""
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc).astimezone(_KST_TZ).isoformat()


def build_krx_cf_append_row(
    date_kst: date, close, rollup: CfSessionRollup, contract_code: str,
    *, metadata_extra: Optional[dict] = None,
) -> dict:
    """KRX CF close finalizer daily-append row builder (pure — logging/side-effect 없음).

    close = finalizer authoritative close (Unit 2 주입). high/low:
      - point_count >= 1 → rollup high/low를 close까지 clamp (low<=close<=high invariant)
      - point_count == 0 → close_only (high=low=close)
    clamp 발생 시 metadata_json에 방향/pre-clamp 값 영속 (Unit 4 hook이 로그/event — builder는 미로그).
    Decimal quantize(0.1), rate == close invariant.
    metadata_extra: provenance 마커 추가 (예: REST-origin close write의
    {"origin": "rest_close_write"} — 2026-06-10 #4 설계). 기본 None = 기존 호출자 불변.
    extra를 먼저 깔고 cf_session_* 3종 예약 키를 나중에 써서 그 3종은 항상 이김.
    clamp 진단 키(close_outside_rollup 등)는 clamp 발동 시에만 쓰이므로 extra로
    같은 이름을 넣지 말 것 (스푸핑 잔존 가능 — 호출자 책임, 현 호출자는 origin만 사용).

    Returns: source_daily_rates upsert용 row dict (rate/high/low/close = Decimal).
    """
    if close is None:
        raise ValueError("close(finalizer authoritative)는 필수 — None 불가")
    if not contract_code:
        raise ValueError("contract_code는 필수 (KRX front contract)")

    close_d = _quantize_krx(close)

    metadata: dict = dict(metadata_extra) if metadata_extra else {}
    metadata.update({
        "cf_session_point_count": rollup.point_count,
        "cf_session_first_ts": _utc_naive_to_kst_iso(rollup.first_ts),
        "cf_session_last_ts": _utc_naive_to_kst_iso(rollup.last_ts),
    })

    if rollup.point_count == 0:
        ohlc_quality = "close_only"
        high_d = low_d = close_d
    else:
        if rollup.high is None or rollup.low is None:
            raise ValueError("rollup high/low는 point_count > 0일 때 필수")
        ohlc_quality = "observed_rollup"
        rollup_high_d = _quantize_krx(rollup.high)
        rollup_low_d = _quantize_krx(rollup.low)
        high_d = max(rollup_high_d, close_d)
        low_d = min(rollup_low_d, close_d)
        if close_d > rollup_high_d or close_d < rollup_low_d:
            metadata["close_outside_rollup"] = True
            metadata["close_above_rollup_high"] = bool(close_d > rollup_high_d)
            metadata["close_below_rollup_low"] = bool(close_d < rollup_low_d)
            metadata["rollup_high_before_clamp"] = float(rollup_high_d)
            metadata["rollup_low_before_clamp"] = float(rollup_low_d)

    return {
        "source": _KRX_SOURCE,
        "asset": _KRX_ASSET,
        "date_kst": date_kst,
        "rate": close_d,
        "high": high_d,
        "low": low_d,
        "close": close_d,
        "ohlc_quality": ohlc_quality,
        "close_basis": _KRX_CF_CLOSE_BASIS,
        "source_method": _KRX_CF_SOURCE_METHOD,
        "contract_code": contract_code,
        "basis_date": None,
        "published_at": None,
        "metadata_json": metadata,
    }


# ─────────────────────────────────────────────────────────────
# KRX CF append write guard (ADR-034 §9 — KRX daily append Unit 3)
# ─────────────────────────────────────────────────────────────

KRX_APPEND_INSERT = "INSERT"
KRX_APPEND_SKIP = "SKIP"
KRX_APPEND_HARD = "HARD"

# conflict 허용 existing provenance — close 일치 시 skip 허용 (그 외 조합은 HARD)
#  - krx_openapi_daily: backfill row (source_ohlc) — incoming 덮지 않고 보존
#  - close_finalizer: 이전 append (idempotent 재실행)
_KRX_APPEND_ALLOWED_EXISTING_METHODS = frozenset({"krx_openapi_daily", _KRX_CF_SOURCE_METHOD})


def decide_krx_cf_append_action(existing, incoming: dict) -> tuple[str, str]:
    """KRX CF daily-append conflict 정책 (pure — DB touch 없음).

    existing: 기존 source_daily_rates ORM row (None이면 신규). incoming: build_krx_cf_append_row dict.
    Returns: (action, reason). action ∈ {INSERT, SKIP, HARD}.

    정책: incoming validity(KRX close_finalizer) → existing None INSERT → key/method/close 판정.
    close match는 허용 pair(krx_openapi_daily / close_finalizer)에만 skip. 그 외 provenance는 HARD.
    """
    # incoming validity (이 guard는 KRX close_finalizer append 전용)
    if incoming.get("source") != _KRX_SOURCE or incoming.get("asset") != _KRX_ASSET:
        return KRX_APPEND_HARD, (
            f"incoming source/asset != KRX ({incoming.get('source')}/{incoming.get('asset')})")
    if incoming.get("source_method") != _KRX_CF_SOURCE_METHOD:
        return KRX_APPEND_HARD, (
            f"incoming source_method != {_KRX_CF_SOURCE_METHOD} ({incoming.get('source_method')})")

    if existing is None:
        return KRX_APPEND_INSERT, "신규 date — insert"

    # existing vs incoming unique key 방어 (caller가 key로 조회하지만 pure 함수 방어)
    if (existing.source, existing.asset, existing.date_kst) != (
            incoming["source"], incoming["asset"], incoming["date_kst"]):
        return KRX_APPEND_HARD, "existing/incoming key mismatch (source/asset/date)"

    if existing.source_method not in _KRX_APPEND_ALLOWED_EXISTING_METHODS:
        return KRX_APPEND_HARD, f"예상 밖 existing provenance (source_method={existing.source_method})"

    if _quantize_krx(existing.close) != _quantize_krx(incoming["close"]):
        return KRX_APPEND_HARD, (
            f"close mismatch (existing={existing.close}, incoming={incoming['close']}, "
            f"existing_method={existing.source_method})")

    # close 일치 → skip (backfill 보존 / idempotent). high/low 차이는 정상 — reason에 surface (hard 아님)
    reason = f"close match (existing={existing.source_method}) — skip"
    if existing.high is not None and existing.low is not None and (
            _quantize_krx(existing.high) != _quantize_krx(incoming["high"])
            or _quantize_krx(existing.low) != _quantize_krx(incoming["low"])):
        reason += " (high/low differ — existing 보존)"
    return KRX_APPEND_SKIP, reason


def write_krx_cf_append_row(db: Session, row: dict) -> tuple[str, str]:
    """KRX CF daily-append guarded single-row write core (commit/rollback은 caller).

    unique key로 existing 조회 → decide_krx_cf_append_action 정책 → **INSERT일 때만 db.add**.
    plain upsert ❌ (conflict update로 backfill row 덮지 않음). SKIP/HARD는 DB touch 0.
    caller(Unit 4 hook): INSERT→commit / SKIP→no-op / HARD→rollback + structured event/log.
    """
    existing = (
        db.query(SourceDailyRate)
        .filter(
            SourceDailyRate.source == row["source"],
            SourceDailyRate.asset == row["asset"],
            SourceDailyRate.date_kst == row["date_kst"],
        )
        .one_or_none()
    )
    action, reason = decide_krx_cf_append_action(existing, row)
    if action == KRX_APPEND_INSERT:
        db.add(SourceDailyRate(**row))
    return action, reason


# ─────────────────────────────────────────────────────────────
# KRX CF daily-append orchestration (ADR-034 §9 — KRX daily append Unit 4a)
# ─────────────────────────────────────────────────────────────

def append_krx_cf_daily_row(
    db: Session, date_kst: date, close, contract_code: str,
    *, metadata_extra: Optional[dict] = None,
) -> tuple[str, str]:
    """KRX CF close finalizer daily-append orchestration + transaction.

    Unit 1(get_krx_cf_session_rollup) → Unit 2(build_krx_cf_append_row) → Unit 3(write_krx_cf_append_row).
    session=="CF" 전용 (caller(4b hook)가 gate). close = finalizer authoritative CF close.
    INSERT → db.commit() / SKIP·HARD → db.rollback(). **logging은 caller(4b)** — 4a는 (action, reason)만 반환.
    builder 오류(close None / contract_code 빈값)는 ValueError 전파 → caller try/except 격리.

    Returns: (action, reason). action ∈ {KRX_APPEND_INSERT, KRX_APPEND_SKIP, KRX_APPEND_HARD}.
    """
    rollup = get_krx_cf_session_rollup(db, date_kst)
    row = build_krx_cf_append_row(
        date_kst, close, rollup, contract_code, metadata_extra=metadata_extra,
    )
    action, reason = write_krx_cf_append_row(db, row)
    if action == KRX_APPEND_INSERT:
        db.commit()
    else:
        db.rollback()
    return action, reason
