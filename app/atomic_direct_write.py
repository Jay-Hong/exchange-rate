"""P1b C6-5b-3a — bank/investing direct writer v2 atomic compare/write helper (dormant island).

C6-5b-3의 첫 단계(smallest-safe): live writer가 atomic mode에서 v1 SET 대신 수행할 **단일 key v2
compare/write**를 A3 primitive 합성(serialize_v2_value + AtomicLatestWriter.compare_write +
write_outcome_from_lua)으로 제공한다. **writer-side only** — `WriteOutcome`만 산출(telemetry +
SET-only trigger gating용). PendingCandidate 생성 / coordinator 전달 / publish는 C6-5b-3 범위 밖
(C6-7 / C6-FLIP, codex 합의).

**dormant island**: app/ live 모듈이 본 모듈을 import 0 (no-importer trip-wire, tests/test_atomic_direct_write).
유일 caller = C6-5b-3b/3c가 crud atomic 분기에서 wiring할 때. 본 모듈은 atomic primitive
(atomic_lua / atomic_value_schema / atomic_write_outcome)를 import하므로 그들의 dormant island allowlist
(_DORMANT_ISLAND)에 추가됨(sanctioned importer). "pure" 아님 — compare_write가 Redis write I/O 소유.

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

from app.atomic_lua import AtomicLatestWriter
from app.atomic_revision import Revision
from app.atomic_value_schema import (
    make_rate_key,
    make_revision_key_from_revision,
    serialize_v2_value,
)
from app.atomic_write_outcome import FailureKind, WriteOutcome, write_outcome_from_lua

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
