"""KRX 미국달러선물 source_rates → source_hourly_rates(1w) CF-only rollup/validation core
(ADR-035 D3 — backfill script + in-process close-finalizer hook 공유 모듈).

`scripts/backfill_krx_source_hourly_rates.py`가 쓰던 pure/query 함수 + 상수를 이 모듈로 이동해
(1) backfill 스크립트와 (2) KRX CF close finalizer in-process hourly-append hook이 동일 로직을
공유한다. backfill 스크립트는 본 모듈에서 re-export하므로 `K.<name>` 모듈-레벨 참조는 불변.

CF-only coverage:
  - KRX는 CF 정규장(KST 08:30:00~15:45:00)과 CM 야간(17:50~익일 06:00) 2세션.
    본 모듈은 **CF 세션 tick만** rollup (KST 08:30~15:45 inclusive). CM 야간 tick은 제외.
  - contract_code per bucket은 source_daily_rates의 이미 resolve된 KRX daily row에서 재사용
    (KIS API 미호출 — daily가 권위 source). daily row 부재 시 contract None → validation flag.
  - metadata_json에 session 필드: {point_count, first_ts_kst, last_ts_kst, session:"CF"}.

provenance: close_basis=`krx_observed_hourly`(session-agnostic) / source_method=`observed_rollup`
            / ohlc_quality=`observed_rollup`.

in-process append hook (`append_krx_cf_hourly_rows`):
  - KRX CF daily-append(append_krx_cf_daily_row)가 daily row를 commit한 직후 close finalizer가 호출.
    그 시점 source_daily_rates KRX row가 존재 → contract_code resolve 가능 (daily 권위 source 재사용).
  - backfill의 write path(B.write_with_transaction + krx_post_write_validator)와 동일 검증을
    그대로 재사용 (corrupt provenance 차단). daily append와는 **별도 transaction** (격리).
"""

# 표준 라이브러리
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

# 로컬 애플리케이션
from app.models import SourceDailyRate, SourceRate
from app.source_hourly_rates import floor_bucket_ts_kst

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


# ─────────────────────────────────────────────────────────────
# Post-write validator (write path 전용 — ORM row 기반, in-transaction)
# ─────────────────────────────────────────────────────────────

def krx_post_write_validator(written) -> list[str]:
    """B.write_with_transaction post-write hook — 적재된 KRX ORM row 검증 (commit 전, rollback 가능).

    dry-run의 validate_contract/validate_session과 symmetric하게 **write된 실 row**에서 재확인:
      (a) EVERY row contract_code IS NOT NULL (KRX는 front contract code 필수 — daily 미resolve면 차단)
      (b) EVERY row metadata_json["session"] == "CF" (CF-only 계약 — CM 누출/누락 차단)
    issue 반환 시 write_with_transaction이 같은 transaction을 rollback (corrupt provenance commit 금지).
    """
    issues = []
    for w in written:
        if w.contract_code is None:
            issues.append(f"contract_code IS NULL @ {w.bucket_ts_kst} "
                          "(post-write — KRX hourly는 contract_code 필수)")
        md = w.metadata_json or {}
        if md.get("session") != _CF_SESSION_LABEL:
            issues.append(f"metadata session != CF @ {w.bucket_ts_kst} "
                          f"(post-write — got {md.get('session')})")
    return issues


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
# CF window 시간 경계 (UTC naive — source_rates 조회용)
# ─────────────────────────────────────────────────────────────

def cf_window_utc_bounds(date_kst: date) -> tuple[datetime, datetime]:
    """date_kst CF 정규장 window(KST 08:30:00~15:45:00 inclusive) → UTC naive [start, end] 경계.

    fetch_krx_ticks의 SQL 범위 축소용 (CF-only filter는 rollup에서 _ts_in_cf_session으로 한 번 더 적용).
    """
    start_kst = datetime.combine(date_kst, _CF_SESSION_START, tzinfo=_KST_TZ)
    end_kst = datetime.combine(date_kst, _CF_SESSION_END, tzinfo=_KST_TZ)
    start_utc = start_kst.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_kst.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def retention_cutoff_kst(now_kst: datetime, retention_days: int) -> datetime:
    """retention prune 경계 (이 시각 미만 bucket 삭제 대상). now.floor(hour) - retention_days.

    hourly_append_source_hourly_rates.py와 동일 정책 (KST naive bucket key 기준).
    """
    return now_kst.replace(
        minute=0, second=0, microsecond=0, tzinfo=None
    ) - timedelta(days=retention_days)


# ─────────────────────────────────────────────────────────────
# In-process CF close-finalizer hourly-append hook (ADR-035 D3)
# ─────────────────────────────────────────────────────────────

def append_krx_cf_hourly_rows(
    db,
    date_kst: date,
    *,
    retention_days: Optional[int] = None,
) -> dict:
    """KRX CF 종가 확정 직후 그 날 CF hourly bucket을 source_hourly_rates에 idempotent append.

    close finalizer가 KRX daily append를 commit한 직후(daily row 존재 → contract resolvable) 호출.
    daily append와는 **별도 transaction** (격리 — caller가 db session을 넘기지만 별 write 단위).

    동작:
      1. date_kst CF window(KST 08:30~15:45) UTC 경계로 source_rates tick fetch
      2. fetch_daily_contract_map(date_kst..date_kst) — 방금 commit된 daily row에서 contract_code 재사용
      3. rollup_cf_ticks_to_hourly → CF bucket(보통 8개) candidate
      4. upsert(commit=False) loop → post-write SELECT 검증(generic a~i + krx_post_write_validator
         semantics: contract_code NOT NULL + session=="CF") → 통과 시 commit / 실패 시 rollback
      5. retention prune: 14d(또는 config) 밖 KRX bucket 삭제 (atomic 단일 delete+commit)

    require_empty 미사용 (append는 idempotent 갱신 허용 — daily SKIP 재실행 시 updated). candidate가
    0 bucket이면(무거래/무관측) write는 skip(빈 counts), prune은 계속 수행(hook이 하루 1회 도는 김에
    retention 유지 — rows 유무와 무관). fail이 아님 — 정상 gap.

    Args:
        db: caller가 넘기는 Session (이 helper 안에서 별도 transaction commit/rollback 책임).
        date_kst: CF 종가가 확정된 KST 영업일.
        retention_days: prune 경계 (None이면 config.SOURCE_HOURLY_RETENTION_DAYS).

    Returns: {"inserted": int, "updated": int, "pruned": int, "buckets": int}.

    Raises: write transaction 검증 실패 시 RuntimeError (caller(close finalizer)가 격리 try/except로
            catch — finalizer 흐름 영향 0). DB 예외도 그대로 전파 → caller 격리.
    """
    # 함수 내부 import — config는 함수-레벨 참조 (test patch.object 호환 + 운영 env hot-read 회피)
    from app import config as _config
    from app.source_hourly_rates import upsert as upsert_fn
    from app.models import SourceHourlyRate

    if retention_days is None:
        retention_days = _config.SOURCE_HOURLY_RETENTION_DAYS

    start_utc, end_utc = cf_window_utc_bounds(date_kst)
    ticks = fetch_krx_ticks(db, start_utc, end_utc)
    contract_map = fetch_daily_contract_map(db, date_kst, date_kst)
    rows = rollup_cf_ticks_to_hourly(ticks, contract_map)

    counts = {"inserted": 0, "updated": 0, "pruned": 0, "buckets": len(rows)}

    if rows:
        expected_buckets = {r["bucket_ts_kst"] for r in rows}
        # pre-write existing count → inserted/updated 산출 (require_empty 미사용 — append 갱신 허용)
        existing = (
            db.query(SourceHourlyRate)
            .filter(
                SourceHourlyRate.source == SOURCE,
                SourceHourlyRate.asset == ASSET,
                SourceHourlyRate.bucket_ts_kst.in_(expected_buckets),
            )
            .count()
        )

        for r in rows:
            upsert_fn(
                db, commit=False,
                source=r["source"], asset=r["asset"], bucket_ts_kst=r["bucket_ts_kst"],
                close=r["close"], ohlc_quality=r["ohlc_quality"],
                close_basis=r["close_basis"], source_method=r["source_method"],
                high=r.get("high"), low=r.get("low"), metadata_json=r.get("metadata_json"),
                contract_code=r.get("contract_code"),   # KRX는 front contract code 보존
            )

        # post-write SELECT (in_(expected_buckets)) → KRX post-write 검증 (commit 전, rollback 가능)
        written = (
            db.query(SourceHourlyRate)
            .filter(
                SourceHourlyRate.source == SOURCE,
                SourceHourlyRate.asset == ASSET,
                SourceHourlyRate.bucket_ts_kst.in_(expected_buckets),
            )
            .order_by(SourceHourlyRate.bucket_ts_kst.asc())
            .all()
        )
        issues = krx_post_write_validator(written)
        # generic invariant 보강 (rate==close) — backfill write path와 동일 semantics
        for w in written:
            if w.rate != w.close:
                issues.append(f"drift @ {w.bucket_ts_kst}: rate={w.rate} != close={w.close}")

        if issues:
            db.rollback()
            raise RuntimeError(
                f"KRX hourly append post-write 검증 실패 ({len(issues)}건) — rollback: "
                + "; ".join(issues[:5])
            )

        db.commit()
        counts["inserted"] = len(rows) - existing
        counts["updated"] = existing

    # retention prune (별 transaction — recent window와 disjoint, atomic 단일 delete+commit)
    now_kst = datetime.now(_KST_TZ)
    cutoff = retention_cutoff_kst(now_kst, retention_days)
    pruned = (
        db.query(SourceHourlyRate)
        .filter(
            SourceHourlyRate.source == SOURCE,
            SourceHourlyRate.asset == ASSET,
            SourceHourlyRate.bucket_ts_kst < cutoff,
        )
        .delete()
    )
    db.commit()
    counts["pruned"] = pruned

    return counts
