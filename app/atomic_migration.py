"""P1b A3-3a — v1→v2 latest value migration decision logic (§10/§17, pure, dormant).

controlled migration command(bootstrap, C6 cutover에서 실행)의 **순수 결정 로직**. I/O 0 —
Redis raw + DB RevisionedRate(A2-4 selector) + mirrored_at을 받아 §10 4-case + 분류로
`MigrationDecision`(action + write할 v2 + 방법)을 반환. runner/CLI/safety guard는 A3-3b.

**DB가 authoritative** (§15/§16 — v1 latest엔 row id 없음 → DB row id 직독 revision). write
케이스의 v2 값은 **DB-derived**(db.rate / to_kst_isoformat(db.timestamp) / db.revision); Redis v1
값은 §10 판정 + migrate_cas expected로만 (Redis 값 복사 금지, codex Medium).

**dormant**: live writer path가 본 모듈을 import/호출하지 않음 (A3-3b runner도 prod 실행 dormant).
scope = **bank/investing only** (A2-4 selector 한정 — USDT/KRX latest:source:*는 별도 PR/C6).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

from app.atomic_revision import RevisionedRate, to_canonical_epoch_us
from app.atomic_value_schema import (
    SCHEMA_VERSION_V2,
    make_rate_key,
    make_revision_key_from_revision,
    serialize_v2_value,
)

# ── migration action (decision outcome) ──
ACTION_SEED_FROM_DB = "seed_from_db"        # Redis 부재 → DB seed (compare_write nil→SET)
ACTION_MIGRATE_UPGRADE = "migrate_upgrade"  # v1, DB ts > Redis ts → DB upgrade (migrate_cas)
ACTION_MIGRATE_SEED = "migrate_seed"        # v1, 동일 ts·rate → revision seed (migrate_cas)
ACTION_ALREADY_CURRENT = "already_current"  # v2, revision·rate == DB → no-op
ACTION_LAGGING_V2 = "lagging_v2"            # v2, revision < DB → D reconciliation defer (no write)
ACTION_REDIS_AHEAD = "redis_ahead"          # Redis ts/revision > DB → overwrite 금지 (no write)
ACTION_CONFLICT = "conflict"                # 동일 ts·다른 rate / v1 parse 실패 → fail-closed
ACTION_INVALID_SCHEMA = "invalid_schema"    # malformed/scalar/array/schema≠2/필드누락 → fail-closed

_WRITE_ACTIONS = frozenset({ACTION_SEED_FROM_DB, ACTION_MIGRATE_UPGRADE, ACTION_MIGRATE_SEED})

WRITE_METHOD_COMPARE_WRITE = "compare_write"  # seed_from_db (nil→SET advance)
WRITE_METHOD_MIGRATE_CAS = "migrate_cas"      # v1 raw-CAS

# v2 revision_key 고정폭 형식 (atomic_value_schema.make_revision_key = '{:020d}:{:020d}').
# malformed revision_key("x" 등)가 lex compare에서 redis_ahead로 숨는 것 차단 (codex Medium).
_REVISION_KEY_RE = re.compile(r"^\d{20}:\d{20}$")


@dataclass(frozen=True)
class ProductionAuthToken:
    """C6-PRE-build-b2 — prod migration apply 인가 증명 (re-validatable, bool 아님).

    script가 authorize 시(confirm_quiesce_drained drain proof + cas_acquire_migration_lease) 생성해
    run_migration에 넘긴다. run_migration→_check_apply_target backstop이 이 token으로 control(HALT @
    expected_generation)+lease(owner) fresh re-read 재검증(TOCTOU 차단). owner=fresh per-run token,
    expected_generation=operator --expected-writer-generation pin.
    """
    owner: str
    expected_generation: int


@dataclass(frozen=True)
class MigrationDecision:
    """한 latest key의 migration 결정. write action만 v2_value/write_method 동반."""

    action: str
    v2_value: Optional[str] = None      # write 시 SET할 v2 JSON (DB-derived)
    write_method: Optional[str] = None  # compare_write / migrate_cas
    expected_raw: Optional[str] = None  # migrate_cas의 expected (= 현재 Redis raw)
    revision_key: Optional[str] = None  # compare_write incoming revision_key
    rate_key: Optional[str] = None      # compare_write incoming rate_key
    detail: str = ""

    @property
    def writes(self) -> bool:
        return self.action in _WRITE_ACTIONS


def classify_redis_value(raw: Optional[str]) -> str:
    """Redis raw → 'absent'/'v1'/'v2'/'invalid'.

    A3-2 Lua discriminator와 정합하되 **migration 입장의 semantic 분류**: array/scalar는 v1으로
    parse 불가라 invalid(fail-closed) — Lua는 array를 migration_required로 라우팅하지만 본 command가
    semantic parse 단계에서 invalid 처리(같은 fail-closed 종착).
    """
    if raw is None:
        return "absent"
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return "invalid"
    if not isinstance(decoded, dict):
        return "invalid"  # scalar / array
    # "in" 검사 — key 부재(=v1)와 present-null({"schema_version": null}) 구분. get()는 둘 다 None이라
    # present-null이 v1로 오분류돼 migrate되는 fail-open 발생 (codex High). present-null → invalid.
    if "schema_version" not in decoded:
        return "v1"
    if decoded["schema_version"] == SCHEMA_VERSION_V2:
        return "v2"
    return "invalid"  # present-null / ≠2


def decide_migration_action(
    redis_raw: Optional[str], db: RevisionedRate, mirrored_at: datetime
) -> MigrationDecision:
    """§10 4-case + 분류 → MigrationDecision (pure, I/O 0). DB authoritative.

    Args:
        redis_raw: 현재 Redis latest 값 (None=부재). §10 판정 + migrate_cas expected.
        db: A2-4 DB selector RevisionedRate (rate/timestamp/revision=(epoch_us,id)).
        mirrored_at: write할 v2의 fresh mirrored_at (runner가 현재 시각 주입 — testability).
    """
    # 함수 내부 import — crud는 무거움, decision은 key당 1회만 호출 (hot-path 아님).
    from app.crud import to_kst_isoformat

    # L1 defensive invariant — db.revision[0]는 to_canonical_epoch_us(db.timestamp)여야(A2-4
    # revision_from_row 계약). 어긋난 DTO면 v2의 public timestamp와 internal revision이 불일치 →
    # fail-closed (Crash Early). A2-4 selector caller엔 항상 성립.
    if db.revision[0] != to_canonical_epoch_us(db.timestamp):
        raise ValueError(
            f"RevisionedRate invariant 위반: revision epoch {db.revision[0]} != "
            f"canonical(timestamp) {to_canonical_epoch_us(db.timestamp)}"
        )

    db_revision_key = make_revision_key_from_revision(db.revision)
    db_rate_key = make_rate_key(db.rate)

    def _build_v2() -> str:
        return serialize_v2_value(
            rate=db.rate,
            timestamp=to_kst_isoformat(db.timestamp),
            mirrored_at=mirrored_at,
            revision=db.revision,
            source=db.source,
            asset=db.asset,
        )

    kind = classify_redis_value(redis_raw)

    if kind == "invalid":
        return MigrationDecision(
            action=ACTION_INVALID_SCHEMA, detail="malformed/scalar/array/schema≠2/필드누락 → fail-closed"
        )

    if kind == "absent":
        return MigrationDecision(
            action=ACTION_SEED_FROM_DB,
            v2_value=_build_v2(),
            write_method=WRITE_METHOD_COMPARE_WRITE,
            revision_key=db_revision_key,
            rate_key=db_rate_key,
            detail="Redis 부재 → DB seed (compare_write nil→SET)",
        )

    if kind == "v2":
        decoded = json.loads(redis_raw)
        cur_rev = decoded.get("revision_key")
        cur_rate = decoded.get("rate_key")
        if not isinstance(cur_rev, str) or not isinstance(cur_rate, str):
            return MigrationDecision(action=ACTION_INVALID_SCHEMA, detail="v2 revision_key/rate_key 누락")
        # M1: malformed revision_key는 lex compare에서 redis_ahead로 숨으므로 형식 검증 → invalid.
        if not _REVISION_KEY_RE.match(cur_rev):
            return MigrationDecision(action=ACTION_INVALID_SCHEMA, detail="v2 revision_key 형식 오류 → fail-closed")
        if cur_rev == db_revision_key:
            if cur_rate == db_rate_key:
                return MigrationDecision(action=ACTION_ALREADY_CURRENT, detail="v2 revision·rate == DB")
            return MigrationDecision(action=ACTION_CONFLICT, detail="v2 동일 revision 다른 rate → fail-closed")
        if cur_rev < db_revision_key:
            return MigrationDecision(action=ACTION_LAGGING_V2, detail="v2 revision < DB → D reconciliation defer")
        return MigrationDecision(action=ACTION_REDIS_AHEAD, detail="v2 revision > DB → overwrite 금지")

    # kind == "v1" — §10 4-case (Redis v1 ts/rate vs DB ts/rate, canonical 비교)
    decoded = json.loads(redis_raw)
    v1_ts_raw = decoded.get("timestamp")
    v1_rate = decoded.get("rate")
    # M2: bool/non-numeric rate를 float() coercion 전 reject (float(True)→1.0이 make_rate_key bool
    # guard를 우회하는 fail-open 차단).
    if isinstance(v1_rate, bool) or not isinstance(v1_rate, (int, float)):
        return MigrationDecision(action=ACTION_CONFLICT, detail="v1 rate 비-숫자 → fail-closed")
    try:
        if not isinstance(v1_ts_raw, str):
            raise ValueError("v1 timestamp 누락/비문자열")
        v1_dt = datetime.fromisoformat(v1_ts_raw)
        # M3: Redis v1 ts는 to_kst_isoformat(KST aware)라 naive는 corruption → conflict
        # (naive를 to_canonical_epoch_us가 UTC로 silent 해석하는 것 차단).
        if v1_dt.tzinfo is None or v1_dt.tzinfo.utcoffset(v1_dt) is None:
            raise ValueError("v1 timestamp naive (KST aware 기대)")
        v1_epoch = to_canonical_epoch_us(v1_dt)
        v1_rate_key = make_rate_key(float(v1_rate))
    except (ValueError, TypeError):
        return MigrationDecision(action=ACTION_CONFLICT, detail="v1 parse 실패 → fail-closed")

    db_epoch = db.revision[0]  # = to_canonical_epoch_us(db.timestamp)
    if db_epoch > v1_epoch:
        return MigrationDecision(
            action=ACTION_MIGRATE_UPGRADE, v2_value=_build_v2(),
            write_method=WRITE_METHOD_MIGRATE_CAS, expected_raw=redis_raw,
            detail="DB ts > v1 ts → upgrade",
        )
    if db_epoch == v1_epoch:
        if db_rate_key == v1_rate_key:
            return MigrationDecision(
                action=ACTION_MIGRATE_SEED, v2_value=_build_v2(),
                write_method=WRITE_METHOD_MIGRATE_CAS, expected_raw=redis_raw,
                detail="동일 ts·rate → revision seed",
            )
        return MigrationDecision(action=ACTION_CONFLICT, detail="동일 ts 다른 rate → fail-closed")
    return MigrationDecision(action=ACTION_REDIS_AHEAD, detail="v1 ts > DB ts → overwrite 금지")


# ===========================================================================
# A3-3b — runner (decision logic 실행: per-key DB+Redis fetch → decide → write →
#   bounded retry). dormant — prod 실행 차단(local/staging only, override 없음 C6 lease 전까지).
#   live writer path 미호출. CLI는 scripts/migrate_atomic_latest_values.py.
# ===========================================================================

# write 후 latest로 인정되는 compare_write outcome (seed_from_db 경로의 race-safe 판정).
# atomic_lua import는 함수 내부 (Redis 의존 모듈 — decision logic의 stdlib 경계 보존).
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_TIMEOUT_SEC = 60.0


@dataclass(frozen=True)
class MigrationKeyResult:
    """한 latest key migration 결과 (per-key 격리 — 예외도 result로 기록, batch 중단 X)."""

    kind: str               # "bank" | "investing"
    key: str                # latest:bank:kb:usd-krw 등
    source: str
    asset: str
    action: str             # decision action 또는 compare_write/migrate_cas 실제 outcome (seed-race)
    wrote: bool             # 실제 Redis write 발생?
    applied: bool           # apply 모드(True) vs dry-run(False)
    retries: int = 0
    detail: str = ""
    error: Optional[str] = None  # 예외 발생 시 (per-key 격리)


@dataclass
class MigrationRunResult:
    dry_run: bool
    results: List[MigrationKeyResult] = field(default_factory=list)

    @property
    def wrote_count(self) -> int:
        return sum(1 for r in self.results if r.wrote)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.error is not None)

    def count(self, action: str) -> int:
        return sum(1 for r in self.results if r.action == action)


@dataclass(frozen=True)
class _MigrationTarget:
    """순회 단위. fetch_error != None이면 per-pair selector 실패 — run_migration이 error result로
    가시화(batch 중단 X). 정상이면 db_fetch로 retry 시 재fetch."""

    kind: str
    source: str
    asset: str
    key: str
    db_fetch: Optional[Callable[[], Optional[RevisionedRate]]] = None
    fetch_error: Optional[str] = None


def _redis_get_str(client, key: str) -> Optional[str]:
    """Redis GET → str (bytes decode) / None."""
    raw = client.get(key)
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def _migrate_one_key(
    writer,
    client,
    *,
    kind: str,
    key: str,
    source: str,
    asset: str,
    db_fetch: Callable[[], Optional[RevisionedRate]],
    mirrored_at_factory: Callable[[], datetime],
    apply: bool,
    max_retries: int,
    timeout_sec: float,
) -> MigrationKeyResult:
    """한 key: GET Redis + decide + (apply 시) write + bounded retry.

    - retry(migrate_cas 'changed' / seed→migration_required)마다 **DB selector + Redis GET 둘 다
      재fetch**(stale snapshot 금지) — db_fetch()와 _redis_get_str()가 while 루프 안.
    - seed_from_db(compare_write)는 compare_write **실제 outcome**이 source of truth(blind SET 아님):
      advance/refreshed_equal=성공 / skipped_newer=더 신선한 값(seed 불필요, 정상) /
      **migration_required=v1 출현→bounded retry**(재fetch 시 v1→migrate_cas 경로) /
      **conflict·invalid_schema=fail-closed(error)** — silent 성공 보고 금지(migrate_cas와 대칭).
    - bounded: max_retries 초과 또는 timeout_sec 경과 → fail-closed(error). timeout은 iteration 작업
      뒤 검사라 **1-iteration granularity soft bound**(엄격 상한 아님) — 주 bound는 max_retries.
    """
    from app.atomic_lua import (
        MIGRATE_OUTCOME_MIGRATED,
        OUTCOME_ADVANCE,
        OUTCOME_CONFLICT,
        OUTCOME_INVALID_SCHEMA,
        OUTCOME_MIGRATION_REQUIRED,
        OUTCOME_REFRESHED_EQUAL,
        OUTCOME_SKIPPED_NEWER,
    )

    deadline = time.monotonic() + timeout_sec
    attempts = 0

    def _r(action, wrote, *, error=None, detail=""):
        return MigrationKeyResult(
            kind=kind, key=key, source=source, asset=asset, action=action, wrote=wrote,
            applied=apply, retries=attempts, error=error, detail=detail,
        )

    def _bound_or_none(label):
        """bounded retry 판정 — 멈춰야 하면 MigrationKeyResult, 계속이면 None. attempts 증가."""
        nonlocal attempts
        attempts += 1
        if attempts > max_retries:
            return _r("retry_exhausted", False,
                      error=f"{label} {attempts}회 (max_retries={max_retries}) — fail-closed")
        if time.monotonic() > deadline:  # post-iteration soft bound
            return _r("timeout", False, error=f"timeout {timeout_sec}s 경과 (retry 중) — fail-closed")
        return None

    while True:
        db_rr = db_fetch()
        if db_rr is None:
            return _r("db_absent", False, detail="DB latest 부재 — migration 대상 아님(skip)")
        redis_raw = _redis_get_str(client, key)
        decision = decide_migration_action(redis_raw, db_rr, mirrored_at_factory())

        if not decision.writes:
            # decision-level conflict/invalid_schema = corruption → fail-closed(error → CLI exit 1).
            # already_current/lagging_v2/redis_ahead는 benign(error=None). (seed-race compare_write의
            # conflict/invalid는 아래 별도 분기, 여기는 decide_migration_action 직접 반환분.)
            if decision.action in (ACTION_CONFLICT, ACTION_INVALID_SCHEMA):
                return _r(decision.action, False, error=f"decision {decision.action} → fail-closed: {decision.detail}")
            return _r(decision.action, False, detail=decision.detail)
        if not apply:
            return _r(decision.action, False, detail=f"dry-run: would {decision.action} ({decision.write_method})")

        if decision.write_method == WRITE_METHOD_COMPARE_WRITE:
            # seed_from_db — compare_write 실제 outcome이 source of truth (seed-race)
            outcome = writer.compare_write(key, decision.v2_value, decision.revision_key, decision.rate_key)
            if outcome in (OUTCOME_ADVANCE, OUTCOME_REFRESHED_EQUAL):
                return _r(outcome, True, detail=f"seed_from_db → compare_write={outcome}")
            if outcome == OUTCOME_SKIPPED_NEWER:
                return _r(outcome, False, detail="seed_from_db → 더 신선한 값 존재(seed 불필요)")
            if outcome == OUTCOME_MIGRATION_REQUIRED:
                # GET None 이후 v1 출현 → retry(재fetch → v1 → migrate_cas)
                stop = _bound_or_none("seed→migration_required(v1 출현)")
                if stop is not None:
                    return stop
                continue
            # conflict / invalid_schema → fail-closed (silent 성공 보고 금지)
            return _r(outcome, False, error=f"seed_from_db → compare_write={outcome} → fail-closed")

        # migrate_cas (v1 raw-CAS)
        outcome = writer.migrate_cas(key, decision.expected_raw, decision.v2_value)
        if outcome == MIGRATE_OUTCOME_MIGRATED:
            return _r("migrated", True, detail=f"{decision.action} → migrate_cas=migrated")
        # 'changed' — concurrent change → bounded retry (DB+Redis 재fetch)
        stop = _bound_or_none("migrate_cas changed")
        if stop is not None:
            return stop


def _iter_targets(db, scope: str):
    """`_MigrationTarget` 순회 — scope ∈ {bank, investing, all}, bank/investing only (A2-4 한정).

    **per-pair selector 호출을 try/except로 격리** — 한 pair selector 실패가 batch 전체를 중단
    시키지 않도록 fetch_error sentinel target을 yield(run_migration이 error result로 가시화 →
    error_count → exit 1). 정상 target의 db_fetch는 retry 시 재fetch용 closure (selector 재실행).
    """
    from app import crud
    from app.latest_rates_cache import latest_key_bank, latest_key_investing

    want_bank = scope in ("bank", "all")
    want_investing = scope in ("investing", "all")

    for pair in crud.SUPPORTED_CURRENCY_PAIRS:
        if want_bank:
            try:
                bank_rrs = crud._select_latest_bank_rates_with_revision(db, pair)
            except Exception as e:
                yield _MigrationTarget("bank", "?", pair, f"latest:bank:*:{pair}",
                                       fetch_error=f"bank selector 실패: {type(e).__name__}: {e}")
                bank_rrs = []
            for rr in bank_rrs:
                bank = rr.source
                yield _MigrationTarget(
                    "bank", bank, pair, latest_key_bank(bank, pair),
                    db_fetch=(lambda b=bank, p=pair: next(
                        (x for x in crud._select_latest_bank_rates_with_revision(db, p) if x.source == b), None)),
                )
        if want_investing:
            try:
                inv_rr = crud._select_latest_investing_rate_with_revision(db, pair)
            except Exception as e:
                yield _MigrationTarget("investing", "investing", pair, latest_key_investing(pair),
                                       fetch_error=f"investing selector 실패: {type(e).__name__}: {e}")
                inv_rr = None
            if inv_rr is not None:
                yield _MigrationTarget(
                    "investing", "investing", pair, latest_key_investing(pair),
                    db_fetch=(lambda p=pair: crud._select_latest_investing_rate_with_revision(db, p)),
                )


def run_migration(
    db,
    client,
    *,
    apply: bool = False,
    scope: str = "all",
    max_retries: int = _DEFAULT_MAX_RETRIES,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    mirrored_at_factory: Optional[Callable[[], datetime]] = None,
    production_authorized: Optional[ProductionAuthToken] = None,
) -> MigrationRunResult:
    """bank/investing latest v1→v2 migration runner (dry-run 기본).

    apply=False → write 0 (결정만 보고). apply=True → Lua atomic write. **idempotent**: 재실행 시
    이미 v2면 already_current → no-op. **per-key 격리**: selector 실패=fetch_error result,
    _migrate_one_key 예외=exception result — 둘 다 batch 중단 X. apply 시 **_check_apply_target(db,
    client) 내부 재확인**(실제 write 대상 db dialect + client host — config!=client hole 차단,
    CLI의 check_apply_safety config preflight와 이중 방어, codex defense-in-depth).
    """
    from app.atomic_lua import AtomicLatestWriter

    if apply:
        # **실제 write 대상**(인자 db bind + client host)으로 fail-closed 검사 — config.REDIS_URL
        # posture(check_apply_safety, CLI preflight)와 별개로 config≠client인 직접 caller도 차단
        # (codex Medium). dialect/host 판정 불가 → 차단. production_authorized 있으면 prod posture에서
        # control+lease fresh re-read 재검증(b2 TOCTOU backstop) → None이면 통과, 실패면 block.
        block = _check_apply_target(db, client, authorized=production_authorized)
        if block is not None:
            raise RuntimeError(f"run_migration apply 차단 (target guard, defense-in-depth): {block}")

    if mirrored_at_factory is None:
        mirrored_at_factory = lambda: datetime.now(timezone.utc)

    writer = AtomicLatestWriter(client) if apply else None
    result = MigrationRunResult(dry_run=not apply)

    for t in _iter_targets(db, scope):
        if t.fetch_error is not None:
            result.results.append(MigrationKeyResult(
                kind=t.kind, key=t.key, source=t.source, asset=t.asset,
                action="fetch_error", wrote=False, applied=apply, error=t.fetch_error,
            ))
            continue
        try:
            result.results.append(_migrate_one_key(
                writer, client, kind=t.kind, key=t.key, source=t.source, asset=t.asset,
                db_fetch=t.db_fetch, mirrored_at_factory=mirrored_at_factory,
                apply=apply, max_retries=max_retries, timeout_sec=timeout_sec,
            ))
        except Exception as e:  # per-key 격리 — 기록 후 계속
            result.results.append(MigrationKeyResult(
                kind=t.kind, key=t.key, source=t.source, asset=t.asset,
                action="exception", wrote=False, applied=apply, error=f"{type(e).__name__}: {e}",
            ))
    return result


def _check_apply_target(db, client, *, authorized: Optional[ProductionAuthToken] = None) -> Optional[str]:
    """run_migration apply 시 **실제 write 대상** fail-closed 검사 — db bind dialect + client host.

    check_apply_safety(config.REDIS_URL posture, CLI preflight)와 별개. 직접 caller가 config와 다른
    client/db를 주입해도 실제 대상으로 차단(codex Medium — config!=client hole). 판정 불가 → 차단.
    sqlite DB + local Redis client만 허용(C6 lease 전까지). host redact(보안).

    **b2**: authorized(ProductionAuthToken) 있으면 prod posture(non-sqlite/non-local)를 곧장 block하지 않고
    `_revalidate_production_authorization`로 control(HALT @ expected_generation/format/epoch0) + lease(owner/
    expiry) **fresh re-read 재검증**(TOCTOU backstop) — None이면 통과(prod apply 허용), 실패면 block string.
    authorized=None(기존 모든 caller/test): **byte-identical** — sqlite+local만 허용. dialect/host **판정 불가**는
    authorized 여부 무관 항상 block(fail-closed).
    """
    try:
        dialect = db.get_bind().dialect.name
    except Exception:
        return "DB bind dialect 판정 불가 — apply 차단 (fail-closed)"
    # authorized=None은 기존 순서 보존(non-sqlite면 host 안 읽고 즉시 block) — byte-identical.
    if dialect != "sqlite" and authorized is None:
        return f"non-sqlite DB (dialect={dialect}) — A3-3b apply는 local 전용 (override 없음). prod=C6."
    # **host는 authorized 여부 무관 항상 판정**(codex b2 blocker — authorized라도 write 대상 Redis client가
    # 미판정/malformed면 config!=client hole). 예외/None → block.
    try:
        host = client.connection_pool.connection_kwargs.get("host")
    except Exception:
        return "Redis client host 판정 불가 — apply 차단 (fail-closed)"
    host_local = host is not None and str(host).lower() in ("localhost", "127.0.0.1", "::1")
    if dialect == "sqlite" and host_local:
        return None  # 기존 local path (authorized 무관 — local sqlite+local Redis는 prod 위험 0, 항상 허용)
    # 여기 도달 = non-sqlite OR non-local posture
    if authorized is not None:
        # authorized override는 **prod DB(non-sqlite) + 판정된 host** 일치 시에만 revalidate. sqlite DB +
        # non-local Redis는 **env-mismatch** → block (codex b2 blocker2): token이 읽은 control/lease는 그 DB
        # 것인데(local sqlite=test 데이터) write 대상은 prod Redis → host 판정만으론 환경 일치를 보장 못 함.
        if dialect == "sqlite":
            return ("authorized override는 prod DB(non-sqlite) 필요 — sqlite DB + non-local Redis는 "
                    "env-mismatch (control/lease는 local인데 write 대상은 non-local, fail-closed).")
        if host is None:
            return "Redis client host 판정 불가(None) — apply 차단 (fail-closed)"
        # prod DB + 판정된 host + authorized: DB control+lease fresh re-read 재검증.
        return _revalidate_production_authorization(db, authorized)
    # authorized=None + (non-sqlite는 위에서 처리) → sqlite + non-local Redis (byte-identical block)
    return "non-local Redis client — A3-3b apply는 local 전용 (override 없음, fail-closed)."


def _revalidate_production_authorization(db, authorized: ProductionAuthToken) -> Optional[str]:
    """b2 prod apply backstop (TOCTOU): control + lease **fresh re-read**(populate_existing) 재검증.

    None=통과(인가 유효), str=block 사유. ACK/drain proof는 **재검증 안 함** — durable append-only라
    TOCTOU surface 아님(writer는 halt window에서 un-drain 불가; script가 authorize 시 confirm_quiesce_drained로
    full drain proof 검증). 그래서 atomic_migration은 atomic_quiesce_durable을 import하지 않는다(디커플 —
    no-importer wire 회피). 재검증 항목: control HALT @ authorized.expected_generation / format / epoch==0
    (TOCTOU halt-flip·gen-bump) + lease owner==authorized.owner / lease_expiry >= now(naive) (TOCTOU
    lease-steal·expiry). 어떤 read 예외도 block(fail-closed). caller(run_migration)가 db 소유.
    """
    try:
        from app.atomic_write_control import CONTROL_ROW_FORMAT_VERSION, WriterMode
        from app.models import AtomicCutoverControl, AtomicWriteControl, get_utc_now

        ctrl = (
            db.query(AtomicWriteControl).populate_existing().filter(AtomicWriteControl.id == 1).first()
        )
        if ctrl is None or ctrl.control_row_format_version != CONTROL_ROW_FORMAT_VERSION:
            return "authorized prod 재검증 실패: control row 부재/format mismatch (fail-closed)"
        if ctrl.requested_mode != WriterMode.HALT:
            return "authorized prod 재검증 실패: writer not HALT (TOCTOU halt-flip)"
        if ctrl.mode_generation != authorized.expected_generation:
            return "authorized prod 재검증 실패: mode_generation != expected (stale/superseded halt)"
        if ctrl.activation_epoch != 0:
            return "authorized prod 재검증 실패: activation_epoch != 0 (pre-activation only)"
        lease = (
            db.query(AtomicCutoverControl).populate_existing().filter(AtomicCutoverControl.id == 1).first()
        )
        if lease is None or lease.lease_owner != authorized.owner:
            return "authorized prod 재검증 실패: lease 미보유/타 owner (TOCTOU lease-steal)"
        if lease.lease_expiry is None or lease.lease_expiry < get_utc_now():
            return "authorized prod 재검증 실패: lease 만료 (TOCTOU lease-expiry)"
        return None
    except Exception as e:
        return f"authorized prod 재검증 예외 — 차단 (fail-closed): {type(e).__name__}"


def check_apply_safety() -> Optional[str]:
    """A3-3b apply 안전 가드 (CLI preflight) — prod hard-fail, **override 없음** (codex H4).

    C6 lease/controlled bootstrap 전까지 apply는 local/staging 전용. DB가 sqlite 아니거나 Redis host가
    명확한 local(localhost/127.0.0.1/::1)이 아니면 차단(둘 다 prod 지표). backfill 스크립트의
    `--allow-production-write` 같은 override 플래그를 **두지 않음** — prod cutover 실행은 C6 영역.

    **fail-closed**(Workflow review): host 판정 불가(scheme-less/unix/빈 URL → hostname None)도 차단 —
    redis-py from_url 검증에 우연히 의존하지 말고 가드 스스로 막는다. host는 redact(보안).

    Returns: None(통과) / 차단 사유(str).
    """
    from urllib.parse import urlparse

    from app import config
    from app.database import engine

    dialect = engine.url.get_dialect().name
    if dialect != "sqlite":
        return (
            f"non-sqlite DB (dialect={dialect}) — A3-3b apply는 C6 lease 전까지 local 전용 "
            "(override 없음). prod cutover 실행은 C6. local smoke: DATABASE_URL=sqlite override."
        )
    host = urlparse(config.REDIS_URL).hostname
    if host is None or host.lower() not in ("localhost", "127.0.0.1", "::1"):
        return "Redis host 판정 불가 또는 non-local — A3-3b apply 차단 (override 없음, fail-closed)."
    return None
