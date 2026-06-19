"""P1b C6-5b-3a — bank/investing direct writer v2 atomic compare/write helper (dormant island).

C6-5b-3의 첫 단계(smallest-safe): live writer가 atomic mode에서 v1 SET 대신 수행할 **단일 key v2
compare/write**를 A3 primitive 합성(serialize_v2_value + AtomicLatestWriter.compare_write +
write_outcome_from_lua)으로 제공한다. **writer-side only** — `WriteOutcome`만 산출(telemetry +
SET-only trigger gating용). PendingCandidate 생성 / coordinator 전달 / publish는 C6-5b-3 범위 밖
(C6-7 / C6-FLIP, codex 합의).

**sanctioned island importer (C6-5b-3b / C6-5b-4)**: app/ live 모듈 중 본 모듈을 import하는 건 `crud.py`
(bank/investing direct atomic 분기 `_atomic_write_changes_v2`) + `latest_rates_cache.py`(C6-5b-4 mirror
atomic 분기 — `build_atomic_writer`/`atomic_compare_write_v2`/`present_for_index`/`outcome_label`)뿐 — 그 외 0
(tests/test_atomic_direct_write no-importer trip-wire, crud+latest_rates_cache만 sanctioned). **atomic 분기
자체는 C6-FLIP(must-confirm)까지 prod 미발화**(atomic mode dormant). 본 모듈은
atomic primitive(atomic_lua / atomic_value_schema / atomic_write_outcome)를 import하므로 그들의 dormant
island allowlist(_DORMANT_ISLAND)에 포함됨. "pure" 아님 — compare_write가 Redis write I/O 소유.

**C4 (commit precedes Redis)**: 본 helper는 Redis write만 수행 — DB commit은 caller(crud atomic 분기)가
**먼저** 한다. 어떤 outcome/예외도 DB rollback 신호가 아니며, caller가 best-effort로 격리한다(§16:261
commit 성공 후 downstream 실패는 rollback 대상 아님).

**예외 분류 (codex C4-safe, §14 SET-relative tri-state)**:
- compare_write **전** 실패(writer 부재 / serialize / key·revision 파생) → `DEFINITE_NOT_APPLIED`
  (SET 미도달 확실).
- `compare_write(...)` 자체 raise → `UNCERTAIN_AFTER_SEND` (보수적 — reply-lost 가능, pre-send 증명 불가).
redis-py 내부 예외를 더 잘게 pre/post-send로 나누는 건 위험 → uncertain이 안전한 C4 선택(codex Q5).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.atomic_lua import (
    AtomicLatestWriter,
    OUTCOME_INVALID_SCHEMA,
    OUTCOME_MIGRATION_REQUIRED,
)
from app.atomic_revision import Revision
from app.atomic_value_schema import (
    make_rate_key,
    make_revision_key_from_revision,
    serialize_v2_value,
)
from app.atomic_write_outcome import (
    FailureKind,
    RedisWritePerformed,
    WriteOutcome,
    WriteState,
    write_outcome_from_lua,
)

_KST = ZoneInfo("Asia/Seoul")


def build_atomic_writer(client: object = None) -> Optional[AtomicLatestWriter]:
    """sync Redis client로 `AtomicLatestWriter` 생성 (client·_get_sync_client 모두 None → None).

    client 미주입 시 `latest_rates_cache._get_sync_client()` 재사용 — v1 direct writer와 **동일** sync
    client(decode_responses=False, async circuit과 격리). register_script는 lazy(첫 호출 전 server 무접촉).
    caller(3b)는 batch 시작 시 1회 생성 후 per-change `atomic_compare_write_v2`에 주입(재생성 회피).
    """
    if client is None:
        # lazy import — module-load 시 latest_rates_cache 끌어오지 않음(dormant 경량 + cycle 회피)
        from app.latest_rates_cache import _get_sync_client
        client = _get_sync_client()
    if client is None:
        return None
    return AtomicLatestWriter(client)


def atomic_compare_write_v2(
    writer: Optional[AtomicLatestWriter],
    key: str,
    *,
    rate: float,
    timestamp: str,
    revision: Revision,
    source: Optional[str] = None,
    asset: Optional[str] = None,
    mirrored_at: Optional[datetime] = None,
) -> WriteOutcome:
    """단일 latest:* key에 v2 compare/write → `WriteOutcome` (writer-side only, no-throw).

    Args:
        writer: `AtomicLatestWriter` (None → writer_unavailable = DEFINITE_NOT_APPLIED).
        key: latest:* Redis key (v1과 **동일 key** — v2 schema는 additive, 별도 v2 key 금지).
        rate/timestamp: public 필드(v1 serialize_value와 동일 의미 — timestamp는 ISO 문자열).
        revision: `(canonical_epoch_us, id)` flush-row-ref (caller가 flush 후 StagedRateChange.revision로 확보).
        source/asset: debug optional (serialize_v2_value internal).
        mirrored_at: tz-aware (기본 now(KST)). naive면 serialize_v2_value가 거부 → DEFINITE_NOT_APPLIED.

    Returns:
        `WriteOutcome` — caller가 `redis_write_performed == APPLIED`(advance/refreshed_equal)로 SET-only
        trigger gating, `structural`/state로 telemetry. **rollback 신호 아님**.
    """
    if writer is None:
        return write_outcome_from_lua(
            None, revision, failure_kind=FailureKind.DEFINITE_NOT_APPLIED, reason="writer_unavailable"
        )
    # ── pre-compare_write: serialize + key/revision 파생 (실패 = SET 미도달 확실 → DEFINITE_NOT_APPLIED) ──
    try:
        if mirrored_at is None:
            mirrored_at = datetime.now(_KST)
        v2_value = serialize_v2_value(
            rate=rate, timestamp=timestamp, mirrored_at=mirrored_at,
            revision=revision, source=source, asset=asset,
        )
        revision_key = make_revision_key_from_revision(revision)
        rate_key = make_rate_key(rate)
    except Exception as e:
        return write_outcome_from_lua(
            None, revision, failure_kind=FailureKind.DEFINITE_NOT_APPLIED,
            reason=f"pre_set:{type(e).__name__}",
        )
    # ── compare_write: 자체 raise = reply-lost 가능 → UNCERTAIN_AFTER_SEND (보수적 C4) ──
    try:
        outcome_str = writer.compare_write(key, v2_value, revision_key, rate_key)
    except Exception as e:
        return write_outcome_from_lua(
            None, revision, failure_kind=FailureKind.UNCERTAIN_AFTER_SEND,
            reason=f"after_send:{type(e).__name__}",
        )
    return write_outcome_from_lua(outcome_str, revision)


def applied_for_trigger(outcome: WriteOutcome) -> bool:
    """SET-only topic trigger 대상 여부 — `redis_write_performed == APPLIED`(advance/refreshed_equal)만.

    ⚠️ **candidate_disposition 아님**(C7): skipped_newer는 candidate_disposition상 PUBLISH_CANDIDATE이나
    redis_write_performed=NOT_APPLIED(SET 미수행)라 trigger 제외해야 한다 — disposition으로 gate하면
    "Redis SET 없이 trigger 발사" 버그. caller(crud atomic 분기)가 atomic_write_outcome를 직접 import하지
    않도록 island이 이 판정을 소유(trip-wire 경계 보존).
    """
    return outcome.redis_write_performed == RedisWritePerformed.APPLIED


def present_for_index(outcome: WriteOutcome) -> bool:
    """mirror(`_mirror_all_latest`) index 멤버십 — 해당 key가 Redis에 present+valid+fresh인가 (C6-5b-4).

    True = advance(이번 cycle write로 SET) / refreshed_equal(같은 revision, mirrored_at만 refresh) /
    **skipped_newer**(concurrent direct writer가 더 fresh한 값 이미 기록 — 이 key는 present+fresh).
    False = conflict / FAILED(structural[migration_required·invalid_schema] + general) — 이 key는
    신뢰 불가라 loaded_keys/latest:index에서 제외 → mirror가 failed++ → index 게이트(failed==0) 차단.

    ⚠️ **applied_for_trigger 아님**(C7 lesson): trigger는 SET 실제 발생(APPLIED=advance/refreshed_equal)
    만이라 skipped_newer 제외 — 하지만 index는 "key가 present인가"라 skipped_newer **포함**해야 한다
    (제외 시 그 자산이 index['keys']에서 빠져 read-path MGET에서 사라짐 = payload drop). ⚠️
    candidate_disposition(§14 publish 축)도 아님 — state 집합이 우연히 PUBLISH_CANDIDATE와 겹치지만
    의미 축이 다름. caller(mirror)가 atomic_write_outcome를 직접 import하지 않도록 island이 본 판정을
    소유(dormancy trip-wire 경계 보존, applied_for_trigger와 동일 원칙).
    """
    return outcome.state in (
        WriteState.ADVANCE, WriteState.REFRESHED_EQUAL, WriteState.SKIPPED_NEWER,
    )


def outcome_label(outcome: WriteOutcome) -> str:
    """telemetry per-outcome 카운터 라벨 (bounded) — mirror가 WriteState/atomic_write_outcome를 직접
    import하지 않고 per-outcome 관찰 카운터를 만들 수 있게 island이 라벨 소유 (C6-5b-4).

    값 집합: {advance, refreshed_equal, skipped_newer, conflict, migration_required, invalid_schema,
    structural_failed, general_failed}. migration_required/invalid_schema는 §17 structural alert 신호
    (전자=v1 재출현=C6-PRE migration 미완 / 후자=corruption)라 FLIP go/no-go용으로 구분 노출.
    """
    if outcome.state != WriteState.FAILED:
        return outcome.state.value
    if outcome.reason in (OUTCOME_MIGRATION_REQUIRED, OUTCOME_INVALID_SCHEMA):
        return outcome.reason
    return "structural_failed" if outcome.structural else "general_failed"
