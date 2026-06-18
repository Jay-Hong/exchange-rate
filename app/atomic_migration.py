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
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

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
