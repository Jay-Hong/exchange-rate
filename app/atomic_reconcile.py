"""P1b B2b-1 — DERIVED pending detection (§19 line 347, pure dormant).

reconciliation coordinator(B2b)의 **pending DETECTION 계층**. DB latest revisions를 last-success
watermark(B1)와 비교해 '어느 source가 미발행 작업을 갖나'만 분류한다.

**계약 (codex B2b-1 tight contract)**:
- 입력: db_revisions(현재 DB latest per source) + last-success watermark + membership(FX_MEMBERSHIP_SOURCES).
- 출력: per-source pending **분류(reason)만**.
- **금지**: publish 결정 / effective vector 생성 / watermark write 판단 / subscriber·send 상태 판단.
  즉 "무엇이 불일치인가"만 답하고, "발행할 것인가/어떻게 복구하나"는 B2b-3/B3, send/subscriber는 B2b-4.

§19 line 347 DERIVED pending: DB-not-in-present→pending / DB-rev>present[src]→pending /
DB-absent+in-missing→not pending / malformed·schema→structural.

**published vector source 아님**: 출력은 'work 있나' 판정이지 watermark.present_revision_vector(effective,
B2b-3/C6 소관)를 만들지 않는다 — B2a 'DB candidate vector ≠ payload(Redis-first) snapshot' 교훈 동일.

**비교 기준**: db_rev(Revision tuple)과 watermark present_revision_vector[src](revision_key string)를
parse_revision_key로 Revision tuple 변환 후 비교(인코딩 single-source = atomic_value_schema). present[src]
파싱 실패 = STRUCTURAL.

**dormant**: live caller 0 (B2b-3/B2b-4/C6 wiring 후속). atomic_watermark/atomic_value_schema(island) import
→ dormant set 합류(dormancy allowlist 추가). pure(I/O 0) — db_revisions는 caller가 읽어 주입.
"""
from __future__ import annotations

import enum
from typing import Dict, Mapping, Optional

from app.atomic_revision import Revision
from app.atomic_value_schema import parse_revision_key
from app.atomic_watermark import Watermark
from app.fx_membership import FX_MEMBERSHIP_SOURCES


class PendingReason(enum.Enum):
    """per-source pending 분류 (§19:347). action(publish/retry/alert)은 B2b-3/B3. plain Enum(A4 패턴)."""
    PENDING_NOT_IN_PRESENT = "pending_not_in_present"   # DB에 revision 있는데 watermark present 부재 (미발행)
    PENDING_DB_AHEAD = "pending_db_ahead"               # DB-rev > watermark present[src] (마지막 성공 이후 변경)
    NOT_PENDING_CURRENT = "not_pending_current"         # DB-rev <= present[src] (최신 반영됨)
    NOT_PENDING_DB_ABSENT = "not_pending_db_absent"     # DB에 값 없음 → 발행할 것 없음 (DB-absent)
    STRUCTURAL = "structural"                            # watermark present[src] revision_key 파싱 불가


def derive_pending(
    db_revisions: Mapping[str, Revision], watermark: Optional[Watermark]
) -> Dict[str, PendingReason]:
    """DB latest revisions ↔ last-success watermark → per-source pending 분류 (pure detection).

    Args:
        db_revisions: {source: Revision(canonical_epoch_us, id)} — 현재 DB latest. caller가 읽어 주입
            (pure 함수). membership source 중 DB에 값 있는 것만 포함(없으면 키 부재 = DB-absent).
        watermark: last-success Watermark(B1) 또는 None(아직 발행 이력 없음 → present 전부 부재).
    Returns:
        {source: PendingReason} — FX_MEMBERSHIP_SOURCES 전 source. action 판단은 호출자(B2b-3/B3).

    Note: 출력은 detection만. DB-absent는 watermark.missing 포함 여부와 무관하게 not-pending(발행할 DB
    값이 없음 — §19:347 'DB-absent+in-missing→not pending'의 operative 조건은 DB-absent). present > DB
    (watermark가 DB보다 앞섬)은 NOT_PENDING_CURRENT로 흡수(DB가 앞설 때만 pending).
    """
    present = watermark.present_revision_vector if watermark is not None else {}
    result: Dict[str, PendingReason] = {}
    for source in FX_MEMBERSHIP_SOURCES:
        db_rev = db_revisions.get(source)
        if db_rev is None:
            result[source] = PendingReason.NOT_PENDING_DB_ABSENT
            continue
        present_key = present.get(source)
        if present_key is None:
            result[source] = PendingReason.PENDING_NOT_IN_PRESENT
            continue
        try:
            present_rev = parse_revision_key(present_key)
        except ValueError:
            result[source] = PendingReason.STRUCTURAL
            continue
        result[source] = (
            PendingReason.PENDING_DB_AHEAD if db_rev > present_rev
            else PendingReason.NOT_PENDING_CURRENT
        )
    return result
